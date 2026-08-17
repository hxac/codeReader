# 编译流水线与算子注册机制（u3-l5）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `compile_pipeline.py` 中一次编译从头到尾经过哪些阶段，以及每个阶段的输入输出。
2. 解释 `op_registry.py` 的注册/分发机制：一段 PIL 调用如何找到它的「编译期实现」，找不到时会发生什么。
3. 读懂 `ops.py` 中 `pypto.loop` 动态循环展开 `_dyn_for` 的边界对齐计算，并解释最近一次修复（`nstart`/`nstep` 修正，提交 `2bb883295`）为什么让各 unroll 段边界不再依赖 `loop.start`。
4. 理解 `frontend/parser/liveness.py` 的作用域活跃变量分析如何驱动「中间张量自动删除」，以及它与输出写回机制的分工。

## 2. 前置知识

本讲建立在 u3-l3（PIL 中间表示）和 u3-l4（pil2ir 转换）之上，先把几个关键概念用通俗语言串一遍：

- **PIL 与 IR 的关系**：`ast2pil` 把 Python 函数变成 PIL（一种简化的调用序列，每条指令形如 `结果 = callee(参数)`，callee 可以是 `"pil.load"` 这类字符串，也可以是 `operator.add` 这类真实函数对象）；`pil2ir` 再把 PIL「解释执行」一遍，边解释边用 `IRBuilder` 生成框架 IR。
- **「解释执行」是什么意思**：PIL 并不在设备上运行，而是在**编译期**被 Python 逐条求值。求值某条调用时需要知道「这个 callee 该干什么」——这个知识就登记在**算子注册表**里。注册表是「callee → 编译期行为」的一张字典。
- **注册表模式（Registry Pattern）**：框架提供一个装饰器 `impl(stub)`，你用它标记一个函数，函数就被登记进全局注册表；之后框架遇到 `stub` 类型的调用就去查表。你在 u2-l2 见过的「门面文件 + 转发」是 API 组织手段，本讲的注册表是**行为分发**手段，两者正交。
- **liveness（活跃性）分析**：一个变量从「被赋值」到「最后一次被使用」之间的区间叫它的活跃区间。编译器用活跃区间回答两类问题：这个变量还能不能删（内存管理）、赋值要不要向后传递（SSA/循环携带变量）。本讲遇到的 `liveness.py` 用它做第一件事：中间张量用完即删。
- **floor 除法与对齐**：Python 的 `a // b * b` 会把 `a` 向下取整到 `b` 的倍数。例如 \( 99 // 48 \times 48 = 96 \)。循环展开的边界计算大量使用这个技巧，本讲 4.3 会用到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/pypto/pil/compile_pipeline.py` | 新 IR 编译流水线总装：`compile_new_ir` 把「Python 函数 + 示例张量」变成跑完一组 IR Pass 的函数对象 |
| `python/pypto/pil/pil2ir.py` | `pil.compile`：AST→PIL→IR 的两段式入口（u3-l4 已精读，本讲只看它与流水线的衔接） |
| `python/pypto/pil/op_registry.py` | `OpRegistry` 类与全局注册表 `_op_registry`，提供 `impl`（注册）与 `dispatch`（分发） |
| `python/pypto/pil/ops.py` | PIL 算子的编译期实现集合：常量/存取、运算符、循环（含 `_dyn_for` 动态循环展开）等 |
| `python/pypto/pil/pir.py` | PIL 数据结构：`LoopRange`、`Block` 等（本讲引用其中两处） |
| `python/pypto/pil/dispatcher.py` | `dispatch_block`/`dispatch_call`：遍历 PIL 并决定每个 callee 走内联还是走注册表 |
| `python/pypto/frontend/parser/liveness.py` | 作用域活跃变量分析器 `ScopeLivenessAnalyzer`（变量删除点计算） |
| `python/pypto/frontend/parser/parser.py` | 旧 Parser 路径：调用 liveness 分析并执行自动清理（本讲只看与 liveness 相关的片段） |
| `python/tests/ut/ir/test_compile_pipeline.py`、`test_parser.py`、`test_pil.py` | 本讲实践的依据：直接调用 `compile_new_ir` / `OpRegistry` / `pil.compile` 的现成测试 |

## 4. 核心概念与源码讲解

### 4.1 编译流水线：compile_pipeline.py

#### 4.1.1 概念说明

当你写下 `@pypto.jit` 并第一次调用被装饰的函数时，框架需要把一段普通 Python 变成可在设备上执行的算子。这件事在 `frontend/parser/entry.py` 中被拆成两条路径：

- **新 IR 路径（`new_ir=True`，默认）**：`entry.py` 的 `compile_new` 调用本讲的 `compile_new_ir`，全程走 `pil` 包（ast2pil → pil2ir → IR Pass 流水线）。
- **旧 Parser 路径（`new_ir=False`）**：走 `frontend/parser/parser.py` 的 `Parser`（liveness 分析挂在这条路径上，见 4.4）。

默认值以源码为准：`jit()` 装饰器的签名是 `new_ir: bool = True`（[python/pypto/frontend/parser/entry.py:1142](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/entry.py#L1142)），`JitCallableWrapper.__init__` 同样默认 `True`（[python/pypto/frontend/parser/entry.py:235](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/entry.py#L235)）。注意类文档字符串里写着 "Defaults to False"，那是滞后的注释——再次印证 u2-l2 的结论：**文档可能滞后，以源码为准**。

`compile_pipeline.py` 很短（74 行），它是流水线的「总装车间」：把前几讲分散见到的零件按固定顺序串起来。

#### 4.1.2 核心流程

`compile_new_ir(pyfunc, *args)` 的执行顺序：

1. 记录 `create_new_logical_tensor` 开关（影响 assemble 语义，函数返回时复位）。
2. `pil.compile(pyfunc, *args)`：
   a. `ast2pil` 把 Python 函数转成 PIL（u3-l3）；
   b. 若用了 `Tensor.move`（即 `out[:] = ...` 写回），先跑一遍 `collect_store_names` 预收集「块内被写的变量名」（因为 Python 层面 move 不算赋值，需要额外一遍收集）；
   c. `pil2ir` 逐条解释 PIL，用 `IRBuilder` 生成 IR 函数（u3-l4）。
3. `builder.create_program([func], "main", ...)` 把函数装进 Program。
4. `setup_verify_data(args)` 登记用于精度校验的金标准数据。
5. 依次执行默认 Pass 序列，每步之后可选 dump。
6. 返回 `program.functions[func.name]`。

默认 Pass 序列（`_build_default_pipeline` 的顺序）：

| 顺序 | 名称 | 职责（按名字推断） |
| --- | --- | --- |
| 1 | `infer_token_pass` | 推导 token（依赖边/执行序信息） |
| 2 | `first_canonicalize_dce` | 规范化 + 激进死代码消除（第一轮） |
| 3 | `second_canonicalize_dce` | 规范化 + DCE（第二轮，处理第一轮暴露的死代码） |
| 4 | `canonicalize(merge_stmts)` | 规范化并把语句合并进 if 结构 |
| 5 | `create_root_functions` | 建立根函数边界 |
| 6 | `finalize` | `finalize_dynamic_function`，收尾动态函数 |

这些 Pass 的实现在 C++ 侧（u4 单元会展开），本讲只需把它们当成黑盒变换：吃一个 Program，吐一个 Program。

#### 4.1.3 源码精读

流水线总装函数——jit 默认路径的编译入口在此汇聚：

[python/pypto/pil/compile_pipeline.py:55-73](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/compile_pipeline.py#L55-L73)

```python
def compile_new_ir(pyfunc, *args, pipeline=None, **kwargs):
    create_new_logical_tensor = kwargs.pop("create_new_logical_tensor", False)
    pypto_impl.ir.set_assemble_new_logical_tensor(create_new_logical_tensor)
    try:
        builder = ir.IRBuilder()
        func = pil.compile(pyfunc, *args, ...)
        program = builder.create_program([func], "main", ir.Span.unknown())
        setup_verify_data(args)
        if pipeline is None:
            pipeline = _build_default_pipeline()
        program = _run_pipeline(program, pipeline)
        return program.functions[func.name]
    finally:
        pypto_impl.ir.set_assemble_new_logical_tensor(False)
```

要点：`pipeline` 参数允许调用方**替换整条 Pass 链**——测试里大量利用这一点只跑单个 Pass；`finally` 保证全局开关一定被复位，这是库代码的好习惯。

默认 Pass 链的组装——注意两轮 `dce(canonicalize(p))` 是「规范化后再删死码」的组合拳：

[python/pypto/pil/compile_pipeline.py:29-44](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/compile_pipeline.py#L29-L44)

每步之间的 dump 钩子——只有打开 `KEY_PRINT_GRAPH` 配置才落盘，目录为日志根下的 `TensorGraph/IR`：

[python/pypto/pil/compile_pipeline.py:19-26](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/compile_pipeline.py#L19-L26)、[python/pypto/pil/compile_pipeline.py:47-52](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/compile_pipeline.py#L47-L52)

`pil.compile` 中为写回做的预收集（承接 u2-l3 的 `out[:]` 语义——move 不是 Python 赋值，所以要单独一遍扫描）：

[python/pypto/pil/pil2ir.py:72-100](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/pil2ir.py#L72-L100)

其中 L87-91 在 `has_move=True` 时先 `Reset()` 再跑 `collect_store_names`；L93 再次 `Reset()` 后才真正 `pil2ir`。`Reset` 的存在说明这两遍扫描都会在 C++ 侧留下痕迹，必须清场重来。

调用方（jit 默认路径在此进入本模块）：

[python/pypto/frontend/parser/entry.py:443-462](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/entry.py#L443-L462)

`compile_new` 先把 torch 张量按类型注解转成 pto 张量（L449-453），再调 `compile_new_ir`（L457）。

#### 4.1.4 代码实践

**实践目标**：不借助 `@pypto.jit` 装饰器，直接调用 `compile_new_ir` 编译一个最小 kernel，确认「Python 函数 → IR 函数」这条流水线可以独立运转。

**操作步骤**：

1. 阅读现成测试 [python/tests/ut/ir/test_compile_pipeline.py:16-26](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/ir/test_compile_pipeline.py#L16-L26)（`test_assemble`）与 [python/tests/ut/ir/test_pil.py:458-473](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/ir/test_pil.py#L458-L473)（`test_tensor_loop_unroll`），注意它们共同的三段写法：定义函数 → 造 `pypto.Tensor` 占位（shape 用 `-1` 表示动态）→ 调用编译接口。
2. 新建脚本（示例代码，非项目原有文件）：

```python
import pypto
from pypto.pil.compile_pipeline import compile_new_ir

def add_kernel(x, y, out):
    pypto.set_vec_tile_shapes(16, 16)
    out[:] = x + y

x = pypto.Tensor((4, 4), pypto.DT_FP32, name="x")
y = pypto.Tensor((4, 4), pypto.DT_FP32, name="y")
out = pypto.Tensor((4, 4), pypto.DT_FP32, name="out")
func = compile_new_ir(add_kernel, x, y, out)
print(str(func))          # 打印生成的 IR 函数
print(func.name)
```

3. 在 pypto 已安装的环境中运行：`python my_pipeline_demo.py`（也可直接 `python tests/ut/ir/test_compile_pipeline.py`，该文件自带 `__main__`）。

**需要观察的现象**：`str(func)` 输出的 IR 文本中能看到输入张量对应的参数、`x + y` 生成的加法语句，以及结尾把 `out` 作为返回值的 return 语句（出口收集逻辑见 4.4.3 引用的 `pil2ir.py` L51-53）。

**预期结果**：脚本正常退出并打印 IR 文本；不抛 `FeError`。**具体打印内容待本地验证**（本环境未安装 pypto 运行时）。

#### 4.1.5 小练习与答案

**练习 1**：`compile_new_ir` 的 `pipeline` 参数为什么设计成可传入的？如果不传会怎样？
**答案**：传入自定义 `pipeline` 可以只跑部分 Pass 或替换 Pass 顺序，测试（如 `python/tests/ut/ir/token/test_infer_pass.py`）正是这样单独验证某个 Pass 的；不传时使用 `_build_default_pipeline()` 组装的六步默认链。

**练习 2**：为什么 `pil.compile` 里 `has_move=True` 时要跑两遍（先 `collect_store_names` 再 `pil2ir`）？
**答案**：`out[:] = ...` 底层是 `Tensor.move`，它不是 Python 赋值语句，PIL 层面看不到「变量被写」这件事；循环携带变量、分支汇合都需要知道「哪些名字在块内被写」，所以必须先用 `CollectContext` 预收集一遍 `store_names`，再正式生成 IR。注释见 [python/pypto/pil/pil2ir.py:73-76](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/pil2ir.py#L73-L76)。

### 4.2 算子注册机制：op_registry.py 与 ops.py

#### 4.2.1 概念说明

`pil2ir` 解释 PIL 时，每条调用的 callee 五花八门：`"pil.load"`、`operator.add`、`pypto.Tensor`、`min`、`pypto.loop`……框架需要一张统一的表来回答「遇到这个 callee 该执行什么编译期行为」。这张表就是 `op_registry.py` 里的 `OpRegistry`。

它要解决三个问题：

1. **注册**：`impl(stub)` 装饰器把「实现函数」登记到 `stub`（字符串或任意可哈希对象，通常是被调用的函数对象本身）名下。
2. **分发**：`dispatch(stub, ctx, *args)` 按三级优先级决定行为（见下）。
3. **透传原始算子**：像 `operator.add` 这类「一个装饰器服务多个算子」的实现，需要知道用户实际调用的是哪个算子——`partial=True` 模式会把 `stub` 本身作为第二个参数传给实现。

#### 4.2.2 核心流程

`dispatch` 的三级优先级（结合 `dispatcher.py` 的调用方一起看）：

```
遇到 PIL 调用 callee(x, ...)
  ├─ callee 是 PIL Function 或可 ast2pil 的用户函数？
  │    → 是：call_function() 内联展开（不走注册表）
  │    → 否：查全局注册表
  │         ├─ stub 已注册 → impl(ctx, *args)        （partial=False）
  │         │              → impl(ctx, stub, *args)    （partial=True）
  │         └─ stub 未注册 → 直接调用 stub(*args)（退化为普通 Python 求值）
```

理解这个优先级很重要：**注册表只拦截「不是用户函数」的调用**。你自己在 kernel 里定义的辅助函数会被内联展开，而不是查表；查表的是 `operator.add`、`pypto.loop`、内建函数这类「外部可调用对象」。

`ops.py` 是注册表最大的「供货商」：整个文件就是一堆 `@impl(...)`。它被 `pil2ir.py` 以副作用方式导入——导入即注册：

[python/pypto/pil/pil2ir.py:13](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/pil2ir.py#L13)

```python
from . import ops  # noqa: F401  side-effect import: registers PIL op handlers
```

#### 4.2.3 源码精读

注册表本体——注意 `impl` 返回原始 `func` 而不是 wrapper，所以被装饰函数仍可直接调用：

[python/pypto/pil/op_registry.py:11-34](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/op_registry.py#L11-L34)

```python
class OpRegistry:
    def impl(self, stub, partial=False):
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            self.ops[stub] = wrapper
            self.partials[stub] = partial
            return func
        return decorator

    def dispatch(self, stub, ctx, *args, **kwargs):
        if stub not in self.ops:
            return stub(*args, **kwargs)          # 未注册：退化为直接调用
        if self.partials[stub]:
            return self.ops[stub](ctx, stub, *args, **kwargs)
        return self.ops[stub](ctx, *args, **kwargs)
```

全局单例与对外别名——`ops.py`、`pil2ir.py`、`dispatcher.py` 用的 `impl`/`dispatch` 都来自这里：

[python/pypto/pil/op_registry.py:37-39](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/op_registry.py#L37-L39)

「一装饰器多算子」的 partial 模式——十几个二元运算符共用一个 `binary_impl`，实现里直接 `op(x, y)` 调回原始算子：

[python/pypto/pil/ops.py:67-81](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L67-L81)

非 partial 模式的典型——`min`/`max` 被改写为图算子：当参数含 SymbolicScalar 且恰好两个时映射到 `pypto.min`/`pypto.max`（在计算图上落节点），否则按 Python 语义求值：

[python/pypto/pil/ops.py:189-200](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L189-L200)

分发优先级的调用方代码——`_get_or_compile` 决定是否内联（L87-91），否则进入 `dispatch`（L96）：

[python/pypto/pil/dispatcher.py:55-69](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/dispatcher.py#L55-L69)、[python/pypto/pil/dispatcher.py:87-97](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/dispatcher.py#L87-L97)

现成单测——直接演示注册与分发两种模式：

[python/tests/ut/ir/test_parser.py:407-428](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/ir/test_parser.py#L407-L428)

#### 4.2.4 代码实践

**实践目标**：注册一个自定义算子 `x*y + 1` 的融合版本，先在隔离的注册表里验证机制，再（进阶）挂到全局注册表让 jit 编译路径用到它。

**操作步骤**：

1. **第一层：隔离验证（有现成测试背书）**。运行仓库的单测确认机制：

```bash
python -m pytest python/tests/ut/ir/test_parser.py::test_op_registry -q
```

2. 仿照该测试，新建脚本（示例代码，非项目原有）：

```python
from pypto.pil.op_registry import OpRegistry, impl  # impl 即全局表，本层先用独立表

registry = OpRegistry()

@registry.impl("pil.fused_mul_add_one")
def fused(ctx, x, y):
    return x * y + 1            # 编译期行为：对示例值直接求值

print(registry.dispatch("pil.fused_mul_add_one", None, 3, 4))   # 期望 13
```

3. **第二层（进阶）：接入 jit 编译路径**。模仿 `ops.py` 中 `@impl(min)` 把内建调用改写为图算子的手法，给 `math.sqrt` 注册一个「翻译成 `pypto.sqrt`」的实现（示例代码，非项目原有）：

```python
import math
import pypto
from pypto.pil.op_registry import impl, _op_registry

@impl(math.sqrt)                     # stub 是 math.sqrt 函数对象本身
def sqrt_impl(ctx, x):
    return pypto.sqrt(x)             # 返回图节点而非数值

@pypto.jit
def kernel(x, out):
    pypto.set_vec_tile_shapes(16, 16)
    out[:] = math.sqrt(x)            # 编译期被 sqrt_impl 翻译为 pypto.sqrt
```

4. 运行 kernel 并与 `torch.sqrt` 对比结果；观察完后清理全局表，避免污染进程内后续编译：`_op_registry.ops.pop(math.sqrt, None); _op_registry.partials.pop(math.sqrt, None)`。

**需要观察的现象**：第一层的 dispatch 返回 13；第二层编译不报「Tensor 不可调用 sqrt」之类的错误，且输出与 `torch.sqrt` 数值一致。

**预期结果**：第一层几乎必成（与 `test_op_registry` 同构）。第二层的可行性依据是 `dispatch_call` 的优先级：`math.sqrt` 是内建函数，`inspect.isfunction` 为假，`_get_or_compile` 返回 `None`，从而进入全局 `dispatch` 查表命中。但 `ast2pil` 对 `math.sqrt(x)` 这类模块属性调用的解析细节本讲未逐行验证，**是否一次跑通待本地验证**；若未命中注册表，报错栈应停在 `dispatch` 的 `stub(*args)` 直接调用处，可据此排查。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `@impl(operator.add, partial=True)` 必须用 partial 模式，而 `@impl("pil.const")` 不需要？
**答案**：`binary_impl` 一个函数服务 `operator.add/sub/mul/...` 十几个算子，实现内部要调用「用户实际想用的那个算子」，所以需要把 `stub`（即 `operator.add` 本身）作为参数传进来；`const_impl` 的行为与 callee 无关，一个 stub 对应一个实现，无需透传。

**练习 2**：如果在 kernel 里调用一个自定义的模块级函数 `helper(x)`，会走注册表吗？
**答案**：不会。`dispatch_call` 先查 `_get_or_compile(callee)`，用户函数能被 `ast2pil` 编译成 PIL Function，于是走 `call_function` 内联展开（[python/pypto/pil/dispatcher.py:87-93](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/dispatcher.py#L87-L93)），根本到不了 `dispatch`。

**练习 3**：`dispatch` 找不到 stub 时退化为 `stub(*args)`，这对 `pypto.add` 这类**已注册过**的图算子入口意味着什么？
**答案**：图算子入口（如 `pypto.op.math` 里的函数）本身是普通 Python 函数，即使不在 PIL 注册表里，直接调用也会在 C++ 计算图上落一个 `pypto_impl` 节点（u2-l2 讲过 op 函数「不做计算、只构图」）；所以「未注册 → 直接调用」对图算子是自然的兜底，而对纯数值内建（如未注册时的 `math.sqrt` 对 Tensor）则会抛 TypeError。

### 4.3 ops.py：pypto.loop 动态循环展开与 nstart/nstep 边界修复

#### 4.3.1 概念说明

`for i in pypto.loop(stop, unroll_list=[...])` 是 PyPTO 表达「设备上的硬件 for 循环」的方式（u2-l5 讲过写法）。编译期，它经历三步变形：

1. `@impl(pypto.loop)` 把调用参数打包成 `LoopRange` 数据对象（[python/pypto/pil/pir.py:20-30](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/pir.py#L20-L30)：`start/stop/step/unroll_list/batch/parallel/...`）。`start/stop` 允许是 SymbolicScalar——这就是「动态循环」，循环次数编译期未知。
2. `"pil.loop"` 指令由 `loop_impl` 分派：`LoopRange` → `_dyn_for`（动态，生成硬件 for，不支持 break/continue）；Python 可迭代对象 → `_static_for`（编译期完全展开）；`None` → `_static_while`。
3. `_dyn_for` 按 `unroll_list` **从大到小分段**：每段用展开因子 `factor` 生成一条 `for` 语句，循环步长放大为 `factor × step`，循环体内把 `loop_var + i*step`（i = 0..factor-1）的 `factor` 份循环体拍平——即「一次迭代干 factor 份活」，给后续调度/向量化留出空间。

本讲的重点是第 3 步里的一个**边界对齐 bug 的修复**（提交 `2bb883295`，"fix(ir): Fix loop start not correct"，issue #2559）：分段串联时，每段的终点 `nstop` 该相对谁对齐？

#### 4.3.2 核心流程

`_dyn_for` 维护一个游标 `nstart`（当前段起点），对降序的每个 `factor`：

```
nstart = loop.start
for factor in unroll_list（降序，且必含 1）:
    nstep = factor × loop.step
    nstop = nstart + (loop.stop − nstart) // nstep × nstep     # 向下对齐到 nstart 的 nstep 倍数
    生成 for(loop_var: nstart → nstop, step = nstep)，体内展开 factor 份
    nstart = nstop                                              # 段间接力
```

用公式表达第 `k` 段的覆盖区间（记该段起点为 \( s_k \)、终点为 \( e_k \)）：

\[ e_k = s_k + \left\lfloor \frac{\text{stop} - s_k}{\text{nstep}_k} \right\rfloor \cdot \text{nstep}_k, \qquad s_{k+1} = e_k \]

三个不变式保证正确性：

1. **段长是本段步长的整数倍**：\( e_k - s_k \equiv 0 \pmod{\text{nstep}_k} \)，floor 掉的余量留给更小的 factor 段；
2. **段间无缝拼接**：\( s_{k+1} = e_k \)，区间链 \( [s_0, e_0), [s_1, e_1), \dots \) 首尾相接；
3. **末段兜底**：`factor == 1` 时 `nstop = loop.stop`，剩余元素逐个处理，最终 \( e_{\text{last}} = \text{stop} \)。

于是整体**不重不漏**地覆盖 \( [\text{start}, \text{stop}) \) 中 step 上的所有取值。

**修复前后对比**（改动的正是第 1 行公式）：

- 修复前：`nstop = loop.start + (loop.stop - loop.start) // unroll_step * unroll_step` —— 每段都相对**最初的 `loop.start`** 对齐；
- 修复后：`nstop = nstart + (loop.stop - nstart) // nstep * nstep` —— 每段相对**本段实际起点 `nstart`** 对齐（变量也从 `unroll_step` 改名 `nstep`，强调它是「本段」的步长）。

**为什么旧公式是错的**：当某段的 `nstart`（= 上一段的终点）不满足 \( \text{nstart} \equiv \text{loop.start} \pmod{\text{nstep}} \) 时，旧公式算出的 \( \text{nstop} - \text{nstart} \) 不再是 nstep 的整数倍，不变式 1 被破坏。手算一个反例（`step=1`，`start=1`，`stop=101`，`unroll_list=[64, 48, 1]`）：

| 段 | factor | 起点 nstart | 旧公式 nstop | 旧段长 | 48 的倍数？ | 后果 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 64 | 1 | \(1 + \lfloor 100/64\rfloor\cdot64 = 65\) | 64 | —（本段 nstep=64，整除 ✓） | 正常 |
| 2 | 48 | 65 | \(1 + \lfloor 100/48\rfloor\cdot48 = 97\) | **32** | ✗（32 < 48） | `for(i=65; i<97; i+=48)` 只能迭代一次且配合循环条件 `i+47 < 101` 为假（65+47=112 ≥ 101），**循环体一次都不执行** |
| 3 | 1 | 97 | 101 | 4 | — | 只覆盖 97..100 |

结果：元素 65..96 共 32 个**无人处理**。新公式下第 2 段 \( \text{nstop} = 65 + \lfloor 36/48\rfloor\cdot48 = 65 \)（空段，游标原地不动），第 3 段覆盖 65..100，不重不漏。

这也解释了规格中的问题——**修复后各 unroll 段边界为什么不再依赖 `loop.start`**：每段终点只由「本段起点 `nstart` + 本段步长 `nstep` + 总终点 `loop.stop`」决定，段与段通过 `nstart = nstop` 接力，形成自洽的链；`loop.start` 只作为首段起点出现一次。对 `start` 为 SymbolicScalar 的动态循环，这条链上每段的边界表达式都锚定在确定的前驱终点上，不再有「隐含假设所有段都与首起点对齐」的错误前提。

#### 4.3.3 源码精读

本次修复的落点（L382-383 为新代码）：

[python/pypto/pil/ops.py:374-389](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L374-L389)

```python
def _dyn_for(body: Block, loop: LoopRange, ctx: BuildContext):
    nstart = loop.start
    nstop = loop.stop
    for factor in loop.unroll_list:
        if factor == 1:
            nstop = loop.stop
        else:
            nstep = factor * loop.step
            nstop = nstart + (loop.stop - nstart) // nstep * nstep   # 相对 nstart 对齐
        try:
            pypto_impl.BeginScope("loop", {}, ...)
            _loop_unroll(body, loop, factor, nstart, nstop, ctx=ctx)
        finally:
            pypto_impl.EndScope()
        nstart = nstop                                              # 段间接力
```

`unroll_list` 的规范化发生在打包阶段：去重、降序、强制并入 1（保证有兜底段），并对非法因子报错：

[python/pypto/pil/ops.py:214-224](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L214-L224)

单段展开 `_loop_unroll`——重点是循环条件（L326-331 保证 `loop_var` 不越过 `loop.start` 方向；L340-343 保证最后一个展开体的 `loop_var + (factor-1)*step` 不越过 `loop.stop`）与 for 语句的生成（L360-371：start/nstop/nstep/factor 逐项落进 `create_for_stmt`）：

[python/pypto/pil/ops.py:325-343](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L325-L343)、[python/pypto/pil/ops.py:352-371](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L352-L371)

`loop_impl` 的三分派——`LoopRange` 走 `_dyn_for`，同时把 `(start, stop, step)` 压入 `ctx.loop_stack` 供 `is_loop_begin/is_loop_end` 使用（这就是 u2-l6 提到的循环边界查询 API 的数据来源）：

[python/pypto/pil/ops.py:392-401](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L392-L401)

测试侧的同步变化：断言**旧对齐表达式**的 `test_dynamic_loop_unroll_uses_div_aligned_bounds` 在本提交中被整体删除——它检查诸如 `"/8)*8" in start`、`"/128)*128" not in ...` 这类「相对 loop.start 对齐」的特征串，与新语义不再相符。现存仍覆盖 unroll 基本行为的测试：

[python/tests/ut/ir/test_pil.py:476-491](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/ir/test_pil.py#L476-L491)（`test_tensor_loop_unroll_batch`，`unroll_list=[32, 16, 1]`）、[python/tests/ut/ir/test_pil.py:494-500](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/ir/test_pil.py#L494-L500)（`test_loop_unroll_idx_name`，用 `_for_ops_of`（定义于 [python/tests/ut/ir/test_pil.py:176](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/ir/test_pil.py#L176)）取出生成的 for 语句检查命名）。

#### 4.3.4 代码实践

**实践目标**：用纯 Python 复现 `_dyn_for` 的边界计算，量化对比新旧公式，亲手验证 4.3.2 的反例。

**操作步骤**：

1. 新建脚本，把新旧两种对齐公式各写一遍（示例代码，非项目原有；逻辑逐行对照 [python/pypto/pil/ops.py:378-389](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L378-L389)，仅把 `SymbolicScalar` 换成普通 int 以便脱离 pypto 运行）：

```python
def segments(start, stop, step, unroll_list, new=True):
    nstart, segs = start, []
    for factor in sorted(set(unroll_list) | {1}, reverse=True):
        if factor == 1:
            nstop = stop
        else:
            nstep = factor * step
            if new:
                nstop = nstart + (stop - nstart) // nstep * nstep
            else:
                nstop = start + (stop - start) // nstep * nstep
        segs.append((factor, nstart, nstop))
        nstart = nstop
    return segs

for seg in segments(1, 101, 1, [64, 48, 1], new=False):
    f, s, e = seg
    print(f"旧 factor={f:3d}  [{s:3d}, {e:3d})  段长={e-s:3d}  "
          f"整除{f}: {(e-s) % f == 0}")
print("---")
for seg in segments(1, 101, 1, [64, 48, 1], new=True):
    print("新 factor=%3d  [%3d, %3d)" % seg[:3] + f"  段长={seg[2]-seg[1]:3d}")
```

2. 运行 `python dyn_for_bounds.py`。
3. 按段累计覆盖：对每段枚举 `range(s, e, factor)`（展开体内每次迭代覆盖 `factor` 个原始步长），检查全体段的覆盖是否恰好等于 `range(1, 101)`。

**需要观察的现象**：旧公式第二段输出 `[65, 97)`，段长 32 不是 48 的倍数；按 `range(65, 97, 48)` 枚举只有起点 65 一个候选，且 65+47 ≥ 101 说明该段实际覆盖为空，元素 65..96 缺失。新公式第二段输出 `[65, 65)` 空段、第三段 `[65, 101)`，覆盖完整。

**预期结果**：新公式的各段覆盖之并恰好等于 `range(1, 101)` 且两两不相交；旧公式有 32 个元素缺失。此脚本不依赖 pypto 安装，可直接运行验证。

**进阶（待本地验证）**：在安装了 pypto 的环境里仿照 `test_loop_unroll_idx_name` 写 `for i in pypto.loop(1, 101, unroll_list=[64, 48, 1])`，`pil.compile` 后用 `_for_ops_of` 取出 for 语句，断言相邻段的 `start == 前一段的 stop`，且每段 `(stop - start) % (factor*step) == 0`。

#### 4.3.5 小练习与答案

**练习 1**：`unroll_list=[64, 48, 1]` 中为什么必须有 1？没有会怎样？
**答案**：1 是兜底段。前面各段用 floor 除法会把「装不满一个完整展开块」的余量留下，只有 `factor == 1` 时 `nstop = loop.stop` 才把余量逐个吃完；没有 1，最末段之后会残留至多 `nstep-1` 个原始步长无人处理（且 `_pypto_loop` 在 L219 已强制并入 1，用户写不写都会补上）。

**练习 2**：若 `unroll_list=[128, 64, 32, 16, 8, 4, 1]` 且 `start=0`、`step=1`，旧公式其实也不会出错，为什么这个 bug 长期没被发现？
**答案**：这些 factor 两两互为倍数，且首段起点就是 `loop.start`，于是每段终点天然满足「既相对 start 对齐、又相对前段终点对齐」，两种公式等价。只有当 `unroll_list` 含**不互为倍数**的因子、或 `start ≠ 0` 且余量造成段起点错位时，旧公式才露馅——反例中 `[64, 48, 1]` 的 48 不整除 64 的段长链条。

**练习 3**：`_loop_unroll` 生成的 for 语句步长是多少？循环变量每次迭代代表什么？
**答案**：步长是 `factor * loop.step`（[python/pypto/pil/ops.py:364](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L364)）。循环变量是本段展开块的**块首**原始索引，一次迭代通过 `loop_var + i * loop.step (i = 0..factor-1)` 覆盖 factor 个连续原始步长（非 batch 模式，L337-339）。

### 4.4 liveness.py：作用域活跃变量分析与自动内存管理

#### 4.4.1 概念说明

先澄清一个容易混淆的点——「liveness」在代码库里出现在**两个层面**：

- 本讲的 `frontend/parser/liveness.py`：作用于 **Python AST** 的作用域分析，产物是「变量删除点」，服务于**自动内存管理**（中间张量用完即删）。它挂在 `frontend/parser/parser.py` 的旧 Parser 路径（`new_ir=False`）上。
- C++ 侧还有 `test_dce_memory_liveness.py` 对应的 IR 级 DCE/内存活跃分析（`python/tests/ut/ir/token/test_dce_memory_liveness.py`），那是 4.1 流水线中 `aggressive_dce` 的职责，u4 单元再展开。

那它与「输出写回」是什么关系？诚实的回答是**分工而非隶属**：

- **输出写回**（`out[:] = ...`）在 pil 路径由另一套机制处理：`ops.py` 的 `apply_patches` 临时把 `Tensor.move` 包一层 `Block.mark_store`（[python/pypto/pil/ops.py:22-32](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L22-L32)），`mark_store` 把被写张量登记进所在 PIL Block 的 `store_names`（[python/pypto/pil/pir.py:142-150](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/pir.py#L142-L150)）；`pil2ir` 在函数出口按 `tensor_args` 的名字把输出张量收进 return 语句（[python/pypto/pil/pil2ir.py:51-53](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/pil2ir.py#L51-L53)）。
- **liveness 分析**回答的是另一个问题：一个**中间**张量变量最后一次被使用在哪条语句之后，之后就可以删除、释放其占用的内存（UB/一级缓存资源）。写回与删除协作的画面是：输出张量被 move 写回、出口被收集为返回值；中间张量则在各自删除点被回收，避免活期过长挤占核内内存。

#### 4.4.2 核心流程

`ScopeLivenessAnalyzer`（AST 访问者）的三步走：

1. **建作用域树**：`root`（函数体）→ `for`（循环体）→ `if` / `if_body` / `else_body`（分支）。`while` 直接报不支持。
2. **记录 def 与 use**：赋值目标记为 def；`Name(Load)` 记为 use，都带上 (scope_id, stmt_id) 二元组。函数参数、循环变量、显式 `del` 的变量进 `exempt_vars`（豁免删除）。
3. **套用两条统一删除规则**（模块文档原文，[python/pypto/frontend/parser/liveness.py:19-23](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/liveness.py#L19-L23)）：
   - **Rule A**：删除点与定义同层，在该作用域内最后一次使用之后；
   - **Rule B**：若使用处与定义处无嵌套关系（如 if/else 两支各自定义各自使用），把定义作用域提升到公共祖先，再按 Rule A 处理。

   典型特判：if/else 互斥分支各自定义的变量（「兄弟作用域」），删除点放在公共祖先作用域退出处；全部定义都在 for 循环里的变量则保守不删（循环每轮都可能重定义）。

最终产物 `LivenessResult.delete_points`：`{stmt_id → {var_name, ...}}`——「执行完哪条语句后可以删哪些变量」。消费方在 `parser.py`：每访问完一条语句就查一次表，把其中**张量类型**的变量标记删除（普通 Python 对象交给 GC，不用管）。

#### 4.4.3 源码精读

模块头部的规则声明与角色边界（「类型过滤由 parser.py 的 `_filter_tensor_vars` 负责」——分析器本身记录所有变量）：

[python/pypto/frontend/parser/liveness.py:12-29](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/liveness.py#L12-L29)

分析入口——一遍 `visit` 加一遍规则应用：

[python/pypto/frontend/parser/liveness.py:163-185](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/liveness.py#L163-L185)

删除规则的统一应用（算法四步写在 docstring 里；无使用点的变量直接在定义语句后删除）：

[python/pypto/frontend/parser/liveness.py:404-425](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/liveness.py#L404-L425)

消费侧——Parser 在 parse 阶段跑分析并把结果存进 `self.delete_after`：

[python/pypto/frontend/parser/parser.py:318-325](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/parser.py#L318-L325)

每条语句访问后的自动清理——注意先经 `_filter_tensor_vars` 过滤出张量变量再 `mark_for_deletion`：

[python/pypto/frontend/parser/parser.py:1706-1722](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/parser.py#L1706-L1722)

输出写回侧的对照（与 liveness 无关但常被混为一谈，见 4.4.1）：move 补丁 [python/pypto/pil/ops.py:24-32](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/ops.py#L24-L32)（`apply_patches` 上下文管理器，`Tensor.move` 前先 `Block.mark_store`）、出口收集 [python/pypto/pil/pil2ir.py:43-55](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/pil/pil2ir.py#L43-L55)（L51-53 把所有张量参数按名取出组成 return）。

#### 4.4.4 代码实践

**实践目标**：直接驱动 `ScopeLivenessAnalyzer`，观察一段含 for/if 的函数里每个变量的删除点，用 Rule A/B 解释结果。

**操作步骤**：

1. 新建脚本（示例代码，非项目原有；需能在仓库内 import pypto，即已安装或已配置好环境）：

```python
import ast
from pypto.frontend.parser.liveness import ScopeLivenessAnalyzer

def kernel(x, out):
    t0 = x + 1            # 中间张量：仅下一行使用
    t1 = t0 * 2           # 中间张量：被两个分支使用
    if x[0, 0] > 0:
        ta = t1 + 1
        out[:] = ta
    else:
        tb = t1 - 1
        out[:] = tb
    t2 = out * 1          # 之后再无使用

import inspect
src = inspect.getsource(kernel)
analyzer = ScopeLivenessAnalyzer()
result = analyzer.analyze(ast.parse(src).body[0], exempt_vars={"x", "out"})
for var, info in result.var_info.items():
    print(var, "定义于语句", info.def_stmt_id,
          "最后删除点", result.delete_points.get(info.delete_after_stmt_id, "—"))
```

2. 运行脚本，抄下每个变量的 `def_stmt_id` 与删除点归属。
3. 对照源码给两个变量写解释：`t0`（同层定义、同层最后一次使用 → Rule A）与 `ta`/`tb`（if/else 互斥分支各自定义 → Rule B，删除点提升到 if 公共祖先）。

**需要观察的现象**：`t0` 的删除点紧跟 `t1 = t0 * 2` 那条语句之后；`ta`、`tb` 的删除点不在各自分支内，而是提升到 if 语句所在层级；`x`、`out` 因在 `exempt_vars` 中不产生删除点。

**预期结果**：输出与上述三条一致。由于 `delete_points` 的键是 `id(ast节点)`（进程内地址，[python/pypto/frontend/parser/liveness.py:787-789](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/liveness.py#L787-L789)），打印出的具体数字每次运行会变，看**归属关系**而非数值。**具体输出待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么分析器记录所有变量，却在消费侧只删张量？
**答案**：分析器工作在 AST 层，那时只有名字、没有运行时类型；是否 `pypto.Tensor` 要等 Parser 执行到该语句、拿到实际值后由 `_filter_tensor_vars` 用 isinstance 判断（[python/pypto/frontend/parser/parser.py:1683-1704](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/parser.py#L1683-L1704)）。Python 对象交给 GC 即可，只有张量占用的框架资源需要显式释放。

**练习 2**：`for` 循环体内定义的变量为什么常被保守处理（不删）？
**答案**：循环体每轮执行都可能重新定义并使用，静态 AST 层无法证明「本轮定义的值下一轮不用」；`_all_def_scopes_are_for_loops` 检测到全部定义都在 for 作用域时直接返回、不设删除点（[python/pypto/frontend/parser/liveness.py:554-556](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/liveness.py#L554-L556)）。宁可多活一轮，不可误删。

**练习 3**：`out[:] = ...` 的写回依赖 liveness 吗？
**答案**：不依赖。写回走 `Tensor.move` → `Block.mark_store` → `store_names` → `pil2ir` 出口收集这条链（见 4.4.1 的两个链接）；liveness 只管「中间张量何时删除」。两者共同保障「输出被正确写回、中间内存被及时复用」。

## 5. 综合实践

把本讲三个机制串成一条链，做一个「看得见的编译」实验：

1. **准备 kernel**（示例代码，非项目原有）：

```python
import pypto

def my_kernel(x, y, out):
    pypto.set_vec_tile_shapes(32, 32)
    acc = x * 0
    for i in pypto.loop(x.shape[0] // 32, unroll_list=[64, 48, 1]):
        ta = x[i:i + 48, :] * y[i:i + 48, :]      # 中间张量：融合 x*y
        acc = acc + ta
    out[:] = acc + 1                              # 写回
```

2. **流水线视角**：调用 `compile_new_ir(my_kernel, x, y, out)`（x/y/out 用 `(-1, 32)` 动态 shape 的 `pypto.Tensor` 占位），`print(str(func))` 观察 IR。
3. **循环展开视角**：数一数生成的 for 语句条数（应为 3 条，对应 factor 64/48/1），逐条检查「本条 start == 上一条 stop」且 `(stop-start) % (factor*step) == 0`——这正是 4.3 修复保证的不变式。
4. **注册表视角**：给 `math.sqrt` 注册翻译实现（4.2 实践第二层），在 kernel 里再加一句 `tb = math.sqrt(x[i:i+48, :])`，观察 IR 中是否出现 sqrt 图节点。
5. **liveness 视角**：用 4.4 的脚本对 `my_kernel` 的 AST 跑分析，指出 `ta`、`tb`、`acc` 各自的删除点应落在哪，并解释 `acc`（循环携带）为什么不会被删。

第 2-4 步需要安装 pypto 的环境，**运行结果待本地验证**；第 5 步只依赖 AST，结论可先在纸面推导再上机核对。

## 6. 本讲小结

- `@pypto.jit` 默认走 `new_ir=True` 的新 IR 流水线：`entry.compile_new` → `compile_new_ir` → `pil.compile`（ast2pil + 写回预收集 + pil2ir）→ `create_program` → 六步默认 Pass 链（infer_token → 两轮 canonicalize+DCE → merge_stmts → create_root_functions → finalize）。
- `OpRegistry` 用 `impl(stub)` 注册、`dispatch(stub, ctx, ...)` 分发；分发优先级是「用户函数内联 > 注册表 > 直接调用」，`partial=True` 用于一个实现服务多个算子的场景。
- `ops.py` 是注册表最大的供货商（import 即注册）；`pypto.loop` 打包成 `LoopRange` 后由 `_dyn_for` 按 `unroll_list` 降序分段展开成硬件 for，段间以 `nstart = nstop` 接力。
- 本次修复（提交 `2bb883295`）把每段终点从「相对 `loop.start` 对齐」改为「相对本段起点 `nstart` 对齐」（`nstop = nstart + (loop.stop - nstart) // nstep * nstep`），保证段长恒为 `nstep` 的整数倍，消除 unroll 因子不互为倍数时整段循环体被跳过、元素漏算的 bug；断言旧表达式的测试已随之删除。
- `liveness.py` 是 AST 层的作用域活跃变量分析，产出「语句 → 可删变量」表，由旧 Parser 路径消费、只对张量生效；输出写回则由 `apply_patches`/`mark_store`/出口收集这条独立链路完成，两者是分工关系。

## 7. 下一步学习建议

本讲结束，Python 侧的编译前端（u3 单元）已经完整：语法捕获（u3-l1/u3-l2）→ PIL（u3-l3）→ IR 生成（u3-l4）→ 流水线与算子注册（本讲）。接下来两条路：

1. **主线**：进入 u4-l1「C++ 框架与 IR 核心对象」——本讲里反复出现的 `ir.IRBuilder`、`create_for_stmt`、`ir.Pass.*` 的 C++ 实现在 `framework/include/ir` 与 `framework/src/interface/ir`，去看看 `ForStmt` 上挂的 `loop_attrs`（parallel、unroll_times 等）如何被 C++ Pass 消费。
2. **支线**：若想先验证本讲实践，可在安装环境后跑 `python/tests/ut/ir/` 目录（`test_pil.py`、`test_compile_pipeline.py`、`test_parser.py`），并把 4.3.4 的边界模拟脚本与 `ops.py` 当前实现对一遍，确认你的理解与 HEAD 一致。
