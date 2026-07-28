# 前端解析与 TIR 表示

## 1. 本讲目标

本讲紧接 [u3-l1 编译总览](u3-l1-compile-overview.md)。u3-l1 告诉我们 `tilelang.lower` 的输入是一个 `PrimFunc`，本讲就回答：**这个 `PrimFunc` 是从哪里来的？**

读者写的是普通 Python：

```python
@T.prim_func
def matmul_relu_kernel(A, B, C):
    with T.Kernel(...) as (bx, by):
        ...
        T.copy(A_shared, A_local)
        T.gemm(A_local, B_local, C_local)
```

装饰器一加，这段 Python 就变成了一个 TVM 的 `tir.PrimFunc` 对象（即 TIR，Tensor IR）。本讲要讲清楚这条"Python → TIR"的转换链。

学完本讲你应该能够：

1. 说清楚 **TIR / PrimFunc** 是什么，`@T.prim_func` 做了什么。
2. 区分 TileLang 仓库里**两代前端**：v1（复用上游 TVM TVMScript parser + overrides）与 v2（自研 AST 改写 + IRBuilder），并知道当前 `T.prim_func` 走的是哪一条。
3. 理解 `T.copy` / `T.gemm` 这些 `T.*` 原语在 TIR 里只是"高层 intrin"，真正的硬件指令要到后续 pass 才生成。
4. 用 `func.show()` 看到一个 kernel 的 TIR 结构，并标注出 Kernel / copy / gemm 对应的 IR 节点。

## 2. 前置知识

- **TIR（Tensor IR）**：TVM 的中间表示。一个 `tir.PrimFunc` 由参数（buffer 或标量 var）、buffer_map、以及一棵由 `For` / `If` / `BufferStore` / `BufferLoad` / `Call` 等"语句节点（Stmt）"组成的树构成。TIR 既是"语言"也是"数据结构"。
- **TVMScript**：TVM 提供的一种"用 Python 语法写 TIR"的前端。你写 Python，它解析成 TIR。`@T.prim_func` 这个装饰器风格就来自 TVMScript。
- **`call_intrin`**：TIR 里调用"内置算子（Op）"的形式。`T.copy` 在前端阶段就是一条 `tir.call_intrin(..., "tl.tileop.copy", ...)`，它不是真正的拷贝指令，而是一个**待降级的高层记号**。
- **AST（抽象语法树）**：Python 解释器把源码解析成的树形结构（`ast` 模块）。v2 前端的核心技巧就是用 `ast.NodeTransformer` 改写这棵树。
- 承接 u3-l1：编译分 `PreLowerSemanticCheck → LowerAndLegalize → OptimizeForTarget` 三阶段，本讲只关心**这些阶段之前**的、由 `@T.prim_func` 产生的那份"原始 TIR"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py) | DSL 命名空间 `T` 的总入口，决定了 `T.prim_func` 最终指向 v1 还是 v2 |
| [tilelang/language/overrides/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/overrides/__init__.py) / [overrides/parser.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/overrides/parser.py) | v1 路线的 TileLang 专属"补丁"，向上游 TVMScript 注册自定义节点处理器 |
| [tilelang/language/tir/entry.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/tir/entry.py) | 从 TVM vendor 来的 `prim_func` 副本，内部调用 `tvm.script.parser._core.parse`，代表 v1 解析方式 |
| [tilelang/language/parser/](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/parser/__init__.py) | v1 的 TVMScript 解析器副本（参考/兼容用），当前非活跃导出路径 |
| [tilelang/language/v2/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/__init__.py) | v2 前端导出 `prim_func / macro / PrimFunc / LazyJITFunc / Ref / const` |
| [tilelang/language/v2/builder.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py) | v2 核心：`prim_func` 装饰器、`Builder`、`LazyJITFunc` |
| [tilelang/language/v2/ast.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/ast.py) | v2 核心：`mutate()` 与 `DSLMutator`，把 Python AST 改写成"驱动 Builder 构建 TIR"的形式 |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py) / [gemm_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py) | 证明 `T.copy` / `T.gemm` 在前端阶段就是 `call_intrin` |

## 4. 核心概念与源码讲解

### 4.1 `@T.prim_func` 与 TVM TIR 的关系

#### 4.1.1 概念说明

很多初学者第一次看到 TileLang 代码会困惑：**这到底是 Python 还是一门新语言？**

答案是：**写法是 Python，但语义被装饰器"劫持"了。** 你写的是一个普通 Python 函数，`@T.prim_func` 这个装饰器在**定义时**（不是调用时）就把它的源码读出来、解析、转换，最终产出一个 `tvm.tir.PrimFunc` 对象。装饰之后，原来的 Python 函数对象已经被替换成了一个 TIR 对象：

```python
@T.prim_func
def matmul_kernel(A, B, C):   # 定义这一刻，matmul_kernel 就已经是 PrimFunc 了
    ...

type(matmul_kernel)            # -> tvm.tir.PrimFunc（不再是 function）
```

这个 `PrimFunc` 就是 u3-l1 里 `tilelang.lower` 的输入。

另一个关键认知：**前端阶段，所有 `T.*` 计算原语都只是"占位 intrin"。** 比如 `T.copy` 在 TIR 里就是一条 `tir.call_intrin(..., "tl.tileop.copy", ...)`，它只记录"这里要搬一份数据"，至于用 TMA、cp.async 还是普通 SIMT 拷贝，要等到 u3-l3 的 `LowerTileOp` 才决定。同理 `T.gemm` 是 `tl.tileop.gemm`，真正的 mma/wgmma 指令也是后面才生成。这条认知承接 [u2-l3](u2-l3-compute-primitives.md) 的结论。

#### 4.1.2 核心流程

从用户代码到原始 TIR 的高层流程：

```
用户写的 @T.prim_func def kernel(...)
        │
        │  (1) 装饰器在定义时触发
        ▼
读取函数源码 / AST
        │
        │  (2) 解析：Python AST  →  TIR 语句树
        │      这一步有两种实现（见 4.2 / 4.3）
        ▼
tvm.tir.PrimFunc 对象
   ├─ params / buffer_map
   ├─ body（Stmt 树）
   └─ attrs（含 T.Kernel 转出的 device kernel 标记）
        │
        │  (3) 之后才进入 u3-l1 的 lower() 三阶段
        ▼
LowerAndLegalize ...  （本讲不展开）
```

注意第 (2) 步有两种实现并存，这是本讲最重要的"地图"信息。

#### 4.1.3 源码精读

先看 `T.*` 原语在前端到底生成了什么。`T.copy` 的实现就在 [tilelang/language/copy_op.py:107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L107)：

```python
return tir.call_intrin("handle", tir.op.Op.get("tl.tileop.copy"), src, dst, annotations=ann if ann else None)
```

——一条 `call_intrin`，op 名是 `tl.tileop.copy`。`T.gemm` 同理，见 [tilelang/language/gemm_op.py:104-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L104-L145)：

```python
return tir.call_intrin(
    ...
    "tl.tileop.gemm",
    ...
)
```

这印证了"前端只是拼 intrin"。这些 `tl.tileop.*` Op 的真实计算语义全部定义在 C++ 侧（[src/op/](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc) 等），将在 u3-l3 的 `LowerTileOp` 中被展开。本讲只关心前端把它们写进 TIR 这一步。

#### 4.1.4 代码实践

**目标**：亲手看到一份原始 TIR，确认 `T.copy`/`T.gemm` 是 intrin。

**操作步骤**（这是后续综合实践的预热，仅需 CPU，不需要 GPU）：

```python
# 示例代码
import tilelang.language as T

@T.prim_func
def tiny(A: T.Tensor((4, 4), "float16"),
         B: T.Tensor((4, 4), "float16"),
         C: T.Tensor((4, 4), "float32")):
    with T.Kernel(1, threads=32) as (bx):
        A_s = T.alloc_shared((4, 4), "float16")
        B_s = T.alloc_shared((4, 4), "float16")
        C_f = T.alloc_fragment((4, 4), "float32")
        T.copy(A, A_s)
        T.copy(B, B_s)
        T.clear(C_f)
        T.gemm(A_s, B_s, C_f)
        T.copy(C_f, C)

tiny.show()   # 打印这份 PrimFunc 的 TIR
```

**需要观察的现象**：`tiny.show()` 打印出来的文本里，应能看到形如 `T.call_intrin("tl.tileop.copy", ...)` 与 `T.call_intrin("tl.tileop.gemm", ...)` 的节点（TVM 用 `T.` 前缀渲染 intrin 调用），以及 `T.Kernel` 转换出的 device kernel 函数属性。

**预期结果**：你会确认"`T.copy`/`T.gemm` 在原始 TIR 里确实只是 intrin 占位"，没有任何 mma/TMA 指令。

> 若 `.show()` 输出格式因 TVM 版本略有差异，以看到 `tl.tileop.copy` / `tl.tileop.gemm` 字样为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@T.prim_func` 去掉，`tiny` 还会是 `PrimFunc` 吗？
**答**：不会。去掉装饰器后 `tiny` 就是一个普通 Python 函数，调用它时 `T.copy` 等会真的去执行 Python（多半报错，因为它们返回的是 TIR 对象而非可执行结果）。装饰器的作用就是在定义时完成"Python → TIR"的转换并替换原对象。

**练习 2**：原始 TIR 里能看到 CUDA 的 `wgmma` / `cp.async` 指令吗？为什么？
**答**：看不到。前端只产生 `tl.tileop.gemm` / `tl.tileop.copy` 这类高层 intrin，硬件指令要到 `LowerAndLegalize` 阶段（u3-l3）的 `LowerTileOp` 等 pass 之后才会出现。

---

### 4.2 v1 解析链：`language/parser` 与 overrides

#### 4.2.1 概念说明

TileLang 建在 TVM 之上，最自然的前端做法是**直接复用上游 TVM 的 TVMScript 解析器**（`tvm.script.parser.tir`）。这就是 v1 路线：`@T.prim_func` 直接用上游的 `prim_func`，它内部用 `tvm.script.parser._core.parse` 把 Python AST 逐节点遍历、分发（dispatch）到对应的 `visit_*` 处理器来构造 TIR。

但 TileLang 有一些 TVMScript 不支持的写法（最典型的是 `T.alloc_var` 产生的"可变标量"`local.var` buffer 的赋值），于是 TileLang 又提供了一套 **overrides**：在 import 时向上游的 dispatch 表注册自己的节点处理器，"打补丁"式地扩展 TVMScript。

仓库里 [tilelang/language/parser/](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/parser/__init__.py) 和 [tilelang/language/tir/](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/tir/entry.py) 目录是从 TVM vendor 来的解析器副本（含一个同样调用 `tvm.script.parser._core.parse` 的 `prim_func`），代表这条 v1 解析方式的历史实现，**当前不是 `T.prim_func` 的活跃导出**（见 4.3 开头的导入顺序），但保留作兼容与参考。

#### 4.2.2 核心流程

v1 路线的完整链路：

```
import tilelang.language as T
        │
        │  ① language/__init__.py:  from tvm.script.parser.tir import *
        │     → T.prim_func = 上游 TVMScript 的 prim_func
        │
        │  ② language/__init__.py:  from . import overrides
        │     → overrides/__init__.py 触发 overrides/parser.py 导入
        │     → @dispatch.register("tir", "Assign" / "AugAssign" / "AnnAssign")
        │       向上游 dispatch 注册 TileLang 专属处理器
        ▼
@T.prim_func def kernel(...):
        │
        │  ③ 上游 prim_func 调用 tvm.script.parser._core.parse(func, ...)
        │     → 遍历 Python AST，遇到 Assign 节点 → 分派到
        │       tilelang_visit_assign（被 override 过的版本）
        ▼
tvm.tir.PrimFunc
```

关键点：overrides 是**按 AST 节点类型**挂钩的。上游解析器遇到 `Assign`/`AugAssign`/`AnnAssign` 时，会调用 TileLang 注册的版本而非 TVM 原版。

#### 4.2.3 源码精读

先看 v1 的总开关——[tilelang/language/__init__.py:5-14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L5-L14)：

```python
# from .parser import *
# now is fully compatible with the upstream tir script
# TODO(lei): remove this import once the upstream tir script is fully compatible
from tvm.script.parser.tir import *
from . import overrides as _overrides  # noqa: F401
...
from .v2 import *  # noqa: F401
```

这段注释说明了一切：旧的 `from .parser import *`（自维护解析器）已被注释掉，改为"完全兼容上游 tir script"，并通过 `overrides` 打补丁。注意最后一行 `from .v2 import *` 在 v1 之后导入——它会**覆盖** `prim_func` 等同名符号（见 4.3）。

overrides 的注册发生在 import 时，见 [tilelang/language/overrides/__init__.py:1-12](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/overrides/__init__.py#L1-L12)：

```python
"""TileLang-specific runtime overrides.
Importing this package registers custom handlers that extend or override
behavior from upstream TVMScript for TileLang semantics.
"""
# Register parser overrides upon import.
from . import parser  # noqa: F401
```

具体补丁在 [tilelang/language/overrides/parser.py:19-71](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/overrides/parser.py#L19-L71)，它 override 了 `Assign` 节点的处理，核心增量是支持写入 `local.var` buffer（即 `T.alloc_var` 产生的可变标量）：

```python
@dispatch.register(token="tir", type_name="Assign")
def tilelang_visit_assign(self, node: doc.Assign) -> None:
    """Override `Assign` to support chained writes and `local.var` buffers."""
    ...
    if (isinstance(lhs_value, BufferLoad)
        and lhs_value.buffer.scope() == "local.var" ...):
        T.buffer_store(lhs_value.buffer, rhs, indices=[0])
        continue
    ...
```

`local.var` 是 TileLang 特有的 scope（[u2-l2](u2-l2-tile-alloc.md) 讲过 `alloc_var`），上游 TVMScript 不认识它，所以必须在这里打补丁。`AugAssign`/`AnnAssign` 的 override 同理（[parser.py:76-155](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/overrides/parser.py#L76-L155)）。

v1 原版的 `prim_func` 长什么样？看 vendor 副本 [tilelang/language/tir/entry.py:33-78](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/tir/entry.py#L33-L78)，它就是个薄装饰器，核心一行是调用上游 `parse`：

```python
f = parse(func, utils.inspect_function_capture(func), check_well_formed=check_well_formed)
```

这就是"v1 = 上游 TVMScript parse + overrides 补丁"的全部秘密。

#### 4.2.4 代码实践

**目标**：用 grep 验证"v1 路线的 prim_func 本质是上游 parse"，并找到 overrides 注册的节点类型。

**操作步骤**：

1. 在仓库根目录运行：`grep -n "tvm.script.parser._core import parse" tilelang/language/tir/entry.py`，确认 v1 `prim_func` 用的是上游 `parse`。
2. 运行：`grep -n "dispatch.register" tilelang/language/overrides/parser.py`，列出所有被 override 的节点类型。

**需要观察的现象**：第 1 步应命中 `from tvm.script.parser._core import parse, scan_macro, utils`；第 2 步应列出 `Assign`、`AugAssign`、`AnnAssign` 三种节点。

**预期结果**：你将直观看到 v1 路线"复用上游 + 局部 override"的结构，以及 overrides 只改写了与赋值相关的三类节点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 overrides 只 override 了 `Assign`/`AugAssign`/`AnnAssign`，而没有 override `For`、`If`？
**答**：因为 TileLang 的循环与分支语义和 TVMScript 一致（都直接映射到 TIR 的 `For`/`If` 节点），上游处理已经够用；只有赋值语义因为 `local.var` buffer 而 和上游不同，才需要打补丁。这是"最小侵入"地扩展上游。

**练习 2**：如果直接 `from tilelang.language.tir.entry import prim_func`，绕过 `__init__.py`，会走 v1 还是 v2？
**答**：会走 v1（上游 parse 路线）。因为这样拿到的是 tir/entry.py 里那个调用 `parse` 的 `prim_func`，没有经过 `__init__.py` 末尾 `from .v2 import *` 的覆盖。正常 `import tilelang.language as T` 拿到的 `T.prim_func` 才是 v2 版本。

---

### 4.3 v2 新前端：`DSLMutator` + `Builder` + `LazyJITFunc`

#### 4.3.1 概念说明

v2 是 TileLang 自研的新一代前端，也是**当前 `T.prim_func` 实际走的那一条**。它不再依赖上游 TVMScript 的 `parse`，而是采用一种很巧妙的"**编译期 AST 改写**"思路：

> 用户写的是普通 Python（`if`/`for`/赋值/`return`），但 `@T.prim_func` 在定义时用一个 `ast.NodeTransformer`（`DSLMutator`）把函数体**整棵改写**成"调用一组 `Builder` 方法"的形式。改写后的函数被执行时，不是在算数，而是在**驱动 `tvm.script.ir_builder.tir` 往 IR 树里"堆"节点**，最后 `builder.get()` 吐出一个 `PrimFunc`。

换句话说，v2 把"写 Python"变成了一种"用 Python 语法调用 IR 构建器"的元编程。Python 的 `if` 不再是运行时分支，而是被改写成 `Builder.ctx_if(cond)` 来生成 TIR 的 `If` 节点。

v2 还带来了三个新能力：

- **`PrimFunc`**：v2 给 `tvm.tir.PrimFunc` 挂了额外字段（`orig_func`、`ir_gen`、`out_idx_override`），方便 JIT 时回溯原始函数。
- **`LazyJITFunc`**：`@T.prim_func(lazy_jit=True)` 时不立即生成 TIR，而是返回一个延迟对象，调用时再按实际参数（尤其是 shape 中的 `T.const` 变量）特化出 TIR。它是 `tilelang.lazy_jit` 的前端基础。
- **`T.const` / `constexpr`**：声明"编译期可特化"的形状变量，配合 `LazyJITFunc` 做按 shape 的缓存。

#### 4.3.2 核心流程

v2 的两阶段：**改写（compile-time）** 与 **构建（definition-time）**。

```
@T.prim_func                         # v2 版，见 builder.py:969
def kernel(A, B, C):
    with T.Kernel(...) as (bx):
        for k in T.Pipelined(...):
            T.copy(...)
        if cond:
            ...

═══ 阶段 A：AST 改写（mutate，定义时一次性完成）═══
DSLMutator(ast.NodeTransformer) 遍历 kernel 的 AST：
  visit_FunctionDef  → 包成 make_closure，每个参数改写成 __tb.arg(...)
  visit_For          → for _tmp in __tb.ctx_for(range): ...
  visit_If           → for br in __tb.ctx_if(cond): __tb.ctx_then(br) ...
  visit_Assign       → name = __tb.bind('name', value)
  visit_Return       → return __tb.ret(value)
  visit_Expr         → __tb.eval(value)
  ...（每个 Python 语句都映射到一个 __tb 方法）
产出：一个"调用 __tb 方法"的新函数 + source 字符串

═══ 阶段 B：构建（驱动 IRBuilder 生成 TIR）═══
prim_func 装饰器：
  builder = Builder()
  with builder.prim_func(name):          # 进入 tvm.script.ir_builder.tir 上下文
      ir_gen.gen(builder)(**annot)       # 执行改写后的函数 → 一路调用 __tb.* 堆 TIR 节点
  prim_func = builder.get()              # 收尾，得到 tvm.tir.PrimFunc
```

`__tb` 就是 `Builder` 实例。它的每个方法（`ctx_for`/`bind`/`ctx_if`/`ret`/...）对应一类 Python 语句，内部都委托给 `tvm.script.ir_builder.tir`（TVM 官方的"命令式构建 TIR"的 API）来真正产生 TIR 节点。

#### 4.3.3 源码精读

v2 导出在 [tilelang/language/v2/__init__.py:1-2](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/__init__.py#L1-L2)：

```python
from .builder import prim_func, macro, PrimFunc, LazyJITFunc, Ref, const  # noqa: F401
from .dtypes import *
```

由于 [language/__init__.py:14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L14) 的 `from .v2 import *` 在 v1 之后执行，v2 的 `prim_func` **覆盖**了上游版本——这就是"当前走 v2"的由来。

**改写的入口** `mutate`，见 [tilelang/language/v2/ast.py:586-640](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/ast.py#L586-L640)：

```python
def mutate(func):
    tree = utils.get_ast(func)
    nonlocals = utils.get_func_nonlocals(func)
    mut = DSLMutator(nonlocals, func.__globals__)
    tree = mut.visit(tree)                       # ← 整棵 AST 被改写
    make_closure = utils.get_compiled_object(tree, "make_closure", ...)
    fn = make_closure(**nonlocals)
    return IRGenerator(gen=fn, source=ast.unparse(tree), ...)
```

改写规则的核心是 `DSLMutator`，例如它把 `for` 循环改成 `__tb.ctx_for`，见 [tilelang/language/v2/ast.py:293-306](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/ast.py#L293-L306)：

```python
def visit_For(self, node: ast.For):
    ...
    return quote(
        f"for {tmp} in __tb.ctx_for(range):\n  pass\n",
        target=node.target, range=node.iter,
        passes=[stmts + node.body],
        span=node,
    )
```

把整个函数包成 `make_closure` 并在每个参数前插入 `__tb.arg(...)`，见 [tilelang/language/v2/ast.py:450-480](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/ast.py#L450-L480)：

```python
def visit_FunctionDef(self, node):
    ...
    for arg in all_args:
        arg_stmt = quote1(f'{name} = __tb.arg("{name}", {name})', span=arg)
        ...
        stmts.append(arg_stmt)
    ...
    return quote1(
        f"def make_closure(...):\n"
        f"  def {node.name}(__tb):\n"
        "    range = __tb.override('range')\n" ...
    )
```

注意每个被改写的函数都接收一个 `__tb` 参数——它就是 `Builder`。

**构建侧**：v2 的 `prim_func` 装饰器，见 [tilelang/language/v2/builder.py:969-1006](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L969-L1006)，核心是把 `mutate` 出来的 `ir_gen` 在一个 `Builder` 上下文里跑一遍：

```python
def prim_func(func=None, *, lazy_jit=False):
    def impl(func):
        ir_gen = mutate(func)                         # 阶段 A：改写
        ...
        if lazy_jit:
            return LazyJITFunc(func, arg_names, tensor_args, ..., ir_gen)
        else:
            builder = Builder()
            with builder.prim_func(func.__name__):    # 阶段 B：构建上下文
                ir_gen.gen(builder)(**annot)          # 执行改写后函数 → 堆 TIR 节点
            prim_func = builder.get()                 # 得到 PrimFunc
            prim_func.orig_func = func
            return prim_func
    return impl(func) if func is not None else impl
```

`Builder.prim_func` 上下文管理器负责进入 `tvm.script.ir_builder.tir`，见 [tilelang/language/v2/builder.py:181-189](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L181-L189)：

```python
@contextmanager
def prim_func(self, name):
    thread_local_storage.builder = self
    with self.ir_builder, self.with_frame(tir.prim_func()):
        tir.func_name(name)
        yield
    ...
```

**延迟 JIT**：`LazyJITFunc` 用两阶段缓存实现了"按 shape 特化"，见 [tilelang/language/v2/builder.py:899-944](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L899-L944)。phase1 按"非 tensor 参数"（如 `T.const` 之外的结构参数）缓存一个 `TirTemplate`，phase2 按 tensor 的实际 shape 替换掉 `T.const` 变量并代换出最终 TIR：

```python
def parse_args(self, *args, **kwargs):
    p1_key, tensor_args, kwargs = self._parse_phase1_key(*args, **kwargs)
    tir_temp = self.p1_cache.get(p1_key, None)
    if tir_temp is None:
        builder = Builder(); builder.lazy_jit = True
        with builder.prim_func(self.orig_func.__name__):
            self.ir_gen.gen(builder)(**self.tensor_args, **kwargs)
        pf = builder.get()
        tir_temp = TirTemplate.create(pf, builder.constexpr_var)
        self.p1_cache[p1_key] = tir_temp
    p2_key = tir_temp._parse_phase2_key(**tensor_args)
    return (p1_key, p2_key), tensor_args
```

`T.const` 声明的变量会被收进 `builder.constexpr_var`，`TirTemplate.create` 校验每个 constexpr 变量都必须**直接**出现在某个 buffer 的 shape/stride 里（[builder.py:852-874](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L852-L874)），否则报错——这是 lazy_jit 能正确按 shape 特化的前提。

#### 4.3.4 代码实践

**目标**：亲眼看到 v2 的"AST 改写"产物，理解它确实把 Python 改成了"调用 `__tb` 方法"。

**操作步骤**：

1. 写一个最小脚本：

   ```python
   # 示例代码
   import tilelang.language as T
   from tilelang.language.v2.ast import mutate

   @T.prim_func
   def k(A: T.Tensor((4,), "float32"), B: T.Tensor((4,), "float32")):
       with T.Kernel(1, threads=32) as (bx):
           for i in T.Parallel(4):
               B[i] = A[i] + 1.0

   gen = mutate(k.orig_func)   # k.orig_func 保留了原始 Python 函数
   print(gen.source)           # 打印改写后的源码
   ```

2. 阅读打印出来的 `gen.source`。

**需要观察的现象**：改写后的源码里应出现 `make_closure`、`def k(__tb)`、`__tb.arg(...)`、`for ... in __tb.ctx_for(...)`、`__tb.bind(...)`、`__tb.ctx_with(...)` 等调用，原来的 `for i in T.Parallel(4)` 和 `B[i] = A[i] + 1.0` 都被改写成了对 `__tb`（即 `Builder`）的方法调用。

**预期结果**：你会直观看到"用户的 Python 被整棵改写成了 IR 构建指令"，从而理解 v2 并不依赖上游 `parse`，而是自研的 AST→Builder 方案。

> 说明：`k.orig_func` 是 v2 在 [builder.py:998](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L998) 挂回的原始函数引用；改写是基于 `orig_func` 的 AST 做的。具体打印文本随版本略有差异，以看到 `__tb.` 调用为准。

#### 4.3.5 小练习与答案

**练习 1**：v2 里，用户写的 `if cond:` 在最终 TIR 里为什么会变成一个 `If` 节点，而不是在 Python 运行时二选一？
**答**：因为 `DSLMutator.visit_If` 把 `if cond:` 改写成了 `for br in __tb.ctx_if(cond): __tb.ctx_then(br): ...`。`Builder.ctx_if` 发现 `cond` 是 `PrimExpr` 时，会用 `tir.If(cond)` 往 IR 树里堆一个 `If` 节点（[builder.py:246-253](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L246-L253)），而不是执行 Python 分支。所以分支结构被"翻译"进了 TIR。

**练习 2**：`@T.prim_func(lazy_jit=True)` 与普通 `@T.prim_func` 在定义那一刻的行为有何不同？
**答**：普通 `@T.prim_func` 在定义时就立即跑一遍 `Builder` 生成完整 `PrimFunc`；`lazy_jit=True` 在定义时只做 `mutate`（改写），返回一个 `LazyJITFunc`，真正的 TIR 生成推迟到它被调用、且按实际 tensor shape 特化时才发生，并按 (phase1_key, phase2_key) 缓存。

**练习 3**：为什么 `TirTemplate.create` 要求每个 `T.const` 变量必须"直接"出现在某个 buffer 的 shape 或 stride 里？
**答**：因为 lazy_jit 的 phase2 靠"在 buffer shape/stride 中定位 constexpr 变量"来建立"变量 → 实际值"的替换映射（`matcher`），只能处理直接出现的情况；像 `T.const` 出现在 `n*2` 这种间接表达式里就无法唯一定位，所以会被拒绝并提示用户拆成单独的 constexpr 变量。

---

## 5. 综合实践

把本讲三个模块串起来，完成"**写一个 kernel，从 Python 一路看到 TIR，并标注关键节点**"。

**任务**：

1. 写一个 matmul 的 `@T.prim_func`（仿 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) 的结构，但直接用 `@T.prim_func` 而非外层 `@tilelang.jit`）：

   ```python
   # 示例代码
   import tilelang
   import tilelang.language as T

   @T.prim_func
   def matmul_kernel(
       A: T.Tensor((128, 128), "float16"),
       B: T.Tensor((128, 128), "float16"),
       C: T.Tensor((128, 128), "float32"),
   ):
       with T.Kernel(1, 1, threads=128) as (bx, by):
           A_s = T.alloc_shared((128, 128), "float16")
           B_s = T.alloc_shared((128, 128), "float16")
           C_f = T.alloc_fragment((128, 128), "float32")
           T.copy(A, A_s)
           T.copy(B, B_s)
           T.clear(C_f)
           T.gemm(A_s, B_s, C_f)
           T.copy(C_f, C)
   ```

2. **看前端原始 TIR**（核心，不需要 GPU）：

   ```python
   matmul_kernel.show()
   ```

   在打印结果中找到并用注释/笔记标注：
   - **Kernel 节点**：`T.Kernel` 转成的 device kernel 函数（函数属性里有 device kernel launch 标记，对应 u3-l1 讲的 `calling_conv = DEVICE_KERNEL_LAUNCH`）。
   - **copy 节点**：`tl.tileop.copy` 的 `call_intrin`（来自 `T.copy`）。
   - **gemm 节点**：`tl.tileop.gemm` 的 `call_intrin`（来自 `T.gemm`）。

3. **（进阶，待本地验证 / 需要 CUDA 环境）看降级后的 device TIR**，体会"前端 intrin → 后续 pass 展开"的差别：

   ```python
   artifact = tilelang.lower(matmul_kernel, target="cuda")
   artifact.device_mod.show()   # device_mod 是一个 IRModule
   ```

   对比 `matmul_kernel.show()` 与 `artifact.device_mod.show()`：前者还能看到 `tl.tileop.copy`/`tl.tileop.gemm` 这类高层 intrin，后者（经过 `LowerAndLegalize` 的 `LowerTileOp` 等 pass 之后）这些 intrin 已被展开成具体的循环与 buffer 操作。注意 `tilelang.lower` 返回的是 `CompiledArtifact`（见 [tilelang/engine/lower.py:243-313](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L243-L313)），要看的 IRModule 在它的 `device_mod` 字段里。

**预期结果**：

- 你能清楚说出：`@T.prim_func`（v2）在定义时已经把 Python 变成了 `PrimFunc`；这份原始 TIR 里 `T.copy`/`T.gemm` 只是 intrin；真正的硬件相关变换在 `lower` 之后。
- 你能区分三种"看 TIR"的入口：`func.show()`（前端原始）、`artifact.device_mod.show()`（降级后 device）、以及 `mutate(func).source`（v2 改写后的 Python 源码，用于理解前端机制本身）。

> 若本地无 CUDA，第 3 步可跳过；第 1、2 步在纯 CPU 下即可完成，足以覆盖本讲全部目标。降级后 TIR 的具体形态将在 [u3-l3 LowerAndLegalize](u3-l3-lower-legalize.md) 详讲。

## 6. 本讲小结

- `@T.prim_func` 是一个**定义时**触发的装饰器：它把普通 Python 函数转换成 `tvm.tir.PrimFunc`（TIR），转换完成后原函数对象已被替换。
- 前端阶段，所有 `T.*` 计算原语都只是**高层 intrin**：`T.copy` → `tl.tileop.copy`、`T.gemm` → `tl.tileop.gemm`（[copy_op.py:107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L107)、[gemm_op.py:104-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L104-L145)），真正的硬件指令要到后续 pass 才生成。
- 仓库里有**两代前端**：v1 = 复用上游 TVMScript `parse` + `overrides` 补丁（[language/__init__.py:10-14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L10-L14)）；v2 = 自研 `DSLMutator` AST 改写 + `Builder` 驱动 `ir_builder.tir`（[v2/ast.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/ast.py)、[v2/builder.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py)）。
- 由于 `from .v2 import *` 在 v1 之后导入，**当前 `T.prim_func` 走的是 v2**；v1 的 `language/parser`、`language/tir` 是 vendor 副本，`overrides` 只对 v1 路线生效（主要补 `local.var` buffer 的赋值语义）。
- v2 的"魔法"是**编译期 AST 改写**：`DSLMutator` 把 `if`/`for`/赋值/`return` 整棵改写成对 `Builder`（`__tb`）方法的调用，执行这些调用就是在往 IR 树堆节点。
- v2 新增 `LazyJITFunc`（`@T.prim_func(lazy_jit=True)`）与 `T.const`：支持按 shape 两阶段特化与缓存，是 `tilelang.lazy_jit` 的前端基础。

## 7. 下一步学习建议

本讲止步于"原始 TIR 生成"。接下来：

- **[u3-l3 LowerAndLegalize](u3-l3-lower-legalize.md)**：看本讲产生的 `tl.tileop.copy`/`tl.tileop.gemm` 这些 intrin 是如何被 `LowerTileOp` 展开成真正的循环与 buffer 操作的，以及 `LayoutInference` 如何推导 fragment 布局。
- **[u3-l6 JIT 适配器](u3-l6-jit-adapters.md)**：本讲的 `LazyJITFunc` 是前端侧的延迟机制，JIT 适配器则是把 `PrimFunc`/`CompiledArtifact` 封装成可调用对象的那一层，二者共同构成 `tilelang.lazy_jit` 的全貌。
- **想深入 v2 改写细节**：通读 [tilelang/language/v2/ast.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/ast.py) 里 `DSLMutator` 的每一个 `visit_*` 方法，对照 [v2/builder.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py) 里 `Builder` 的对应方法，理解"每种 Python 语句 → 哪个 TIR 节点"的完整映射。
- **想理解 `T.*` 原语的 C++ 语义**：跳到 [u7-l1 C++ 算子实现机制](u7-l1-cpp-ops.md)，看 `src/op/gemm.cc`、`src/op/copy.cc` 如何定义 `tl.tileop.*` 这些 Op 的真实计算。
