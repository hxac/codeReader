# eager builder 与 prim_func 转换

## 1. 本讲目标

上一讲（u4-l1）我们看清了编译总流程：`tilelang.lower()` 把一个 `PrimFunc` 经 Pass 流水线变成设备源码。但有一个关键问题被刻意跳过了——**用户写的那个 Python 函数，是怎么变成 `PrimFunc` 的？** 本讲就回答这个问题。

学完本讲，你应当能够：

- 说清 **eager builder（渴望式构建器）** 的工作方式：它如何用一个 **frame 栈** 把「执行 Python 语句」变成「往 TVM IRBuilder 里追加 TIR 节点」。
- 理解 `@T.prim_func` 装饰器背后发生的事：`mutate()` 如何用 Python AST 改写，把每一行用户代码翻译成对构建器 `__tb` 的方法调用，最终产出 `IRGenerator`。
- 区分 `JITFunc` 的 lazy / eager 两种风格，掌握 eager 模式的 **两阶段（phase1 / phase2）模板机制**，以及 `set_mode` / `parse_args` 的职责。
- 能够追踪 `T.Kernel`、赋值、`for`、`T.copy` 这几类典型语句分别落到哪个构建器钩子、生成哪种 TIR 节点。

本讲覆盖两个最小模块：`tilelang.language.eager`、`tilelang.language.frame`。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：用户函数体是「搭建 IR 的指令」，不是「运行时的计算流程」。**
当你写下

```python
with T.Kernel(128, threads=128) as bx:
    for i in T.serial(128):
        A[bx, i] = A[bx, i] + B[bx, i]
```

这段代码在 **编译期被执行一次**，目的不是算出 `A` 的值，而是「告诉构建器：请生成一个 grid=128、block=128、循环 128 次、循环体里做一次加法 store 的 TIR」。所以函数体里出现的 `bx`、`i` 不是普通 Python 变量，而是 TIR 里的循环变量（`Var`）。

**直觉二：构建器模式（Builder Pattern）。**
tilelang 复用了 TVM 的 `tvm.script.ir_builder.tirx`（简称 `tirx`）这一套「IR 构建器」。构建器内部维护一个 **栈**：进入 `for` 就压入一个 ForFrame，退出就弹出；进入 `if` 就压入 IfFrame……每压一帧就向正在搭建的 TIR 树里挂一个 `For`/`If`/`BufferStore` 节点。等函数体跑完，整棵 TIR 树就长好了，调用 `builder.get()` 得到一个完整的 `PrimFunc`。tilelang 的 `Builder`（在 `eager/builder.py`）就是站在 TVM IRBuilder 肩膀上、补齐 tile 级语义的那一层。

**直觉三：用 Python AST 改写来「劫持」语句。**
但有个麻烦：Python 的 `for i in T.serial(128):` 里，`i` 是被 Python 自己绑定的；`a = b + c` 也是 Python 自己赋值的。构建器想要全程插手这些绑定。tilelang 的解法很巧妙：在装饰器里用 `ast.NodeTransformer` 把用户函数的 AST **改写**一遍——把每个 `if` 改写成 `for br in __tb.ctx_if(cond):`，每个赋值改写成 `name = __tb.bind('name', value)`，每个表达式语句改写成 `__tb.eval(value)`……改写后的函数第一个参数永远是构建器 `__tb`。执行这个改写后的函数，就是在按顺序调用构建器的各个方法，TIR 也就被一砖一瓦地搭出来了。这个改写器就是 `DSLMutator`。

> 名词速查：
> - **TIR**：TVM Tensor IR，张量中间表示，`PrimFunc` 是 TIR 里的函数节点。
> - **frame（帧）**：构建器栈上的一个元素，对应一段作用域（一个 `for`、一个 `if`、一个 prim_func 体……）。
> - **钩子（hook）**：构建器上供改写后代码调用的方法，如 `bind` / `ctx_for` / `eval`。
> - **`tirx`**：`tvm.script.ir_builder.tirx`，TVM 官方的 TIR 构建器前端，tilelang 直接复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) | 核心。定义 `Builder`（搭 TIR 的工作台）、`JITFunc`（lazy/eager 包装）、`TirTemplate`（两阶段模板）、`prim_func`/`const` 等用户面装饰器与函数。 |
| [tilelang/language/eager/ast.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py) | AST 改写。定义 `BaseBuilder`（Python 语义默认实现）、`DSLMutator`（把用户函数改写成 `__tb.xxx` 调用）、`IRGenerator`、入口 `mutate()`。 |
| [tilelang/language/eager/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/__init__.py) | 包导出。把 `prim_func`、`JITFunc`、`const`、`Ref`、`annotate_*` 等重新导出，再经 `language/common.py` 进入 `tilelang.language` 命名空间。 |
| [tilelang/language/frame.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/frame.py) | 帧栈工具。定义 `FrameStack`、`LetFrame`，以及 `register_let_value` / `get_let_value`，用于跟踪 `let` 绑定与 BufferRegion 别名。 |
| [tilelang/language/eager/utils.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/utils.py) | 辅助。`get_ast`（取函数 AST）、`get_func_nonlocals`（取闭包变量）、`get_compiled_object`（编译并 exec 改写后的代码）。 |
| [tilelang/language/kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py) | `T.Kernel` 的实现与 `KernelLaunchFrame`。本讲用它说明「DSL 语句如何找到当前构建器」。 |

## 4. 核心概念与源码讲解

### 4.1 Builder：搭 TIR 的工作台与 frame 栈

#### 4.1.1 概念说明

`Builder` 是「渴望式（eager）」构建器——你在函数体里每写一行，它就 **立刻** 把对应的 TIR 节点追加进正在构建的 IR 树。「eager」是相对于「lazy 风格」（用户自己 `return` 一个现成的 `PrimFunc`）而言的，后者根本不需要构建器。

`Builder` 要同时管两件事：

1. **TIR 的生长**：委托给内部的 TVM `IRBuilder`（`self.ir_builder`），由 `tirx.*` 系列函数实际发射 TIR 节点。
2. **作用域（frame）的栈**：用一个 Python 列表 `self.frames` 维护「当前处于哪些嵌套作用域里」。这层栈是 tilelang 自己加的，TVM 原生 IRBuilder 没有这么细的查询能力。它用来做变量作用域检查（`name_inside_frame`）、`continue/break` 合法性检查、以及「某个名字是否还在它的定义域内」。

此外，`Builder` 通过 **线程局部存储（thread-local）** 暴露一个 `current()` 类方法，让 `T.Kernel`、`T.const` 这些「不带构建器参数」的 DSL 函数能找到当前正在干活儿的构建器。这一点非常关键——它解释了为什么 `T.Kernel(...)` 只能在 `@T.prim_func` / `@tilelang.jit` 函数体里调用，在外面调会抛 `JITNoBuilderError`。

#### 4.1.2 核心流程

一个 eager 风格的 `PrimFunc` 是这样被搭出来的：

```
Builder()                                   # 1. 建一个空构建器
with builder.prim_func(name):               # 2. 进入 prim_func 上下文：
    │   thread_local.builder = self         #    - 把自己登记为「当前构建器」
    │   clear_let_values()                  #    - 清空 let 绑定表
    │   with self.ir_builder:               #    - 打开 TVM IRBuilder
    │     with self.with_frame(tirx.prim_func()):   # - 压入 PrimFuncFrame
    │       tirx.func_name(name)
    │       yield                           #    ★ 在这里执行改写后的用户函数体
    │                                          每条语句 → 调用 __tb.<hook>
    │                                          每个 hook → 压/弹 frame + tirx.* 发射 TIR
    del thread_local.builder                # 3. 退出，注销「当前构建器」
builder.get()  -> PrimFunc                  # 4. 收尾：TVM IRBuilder 吐出 PrimFunc
```

frame 栈的压入/弹出由两个方法负责：`enter_frame`（压一个 frame 并调用它的 `__enter__`）与 `with_frame`（上下文管理器，退出时把栈顶回卷到进入时的位置）。

#### 4.1.3 源码精读

先看 `Builder` 的字段与「当前构建器」机制。[builder.py:201-218] 定义了 `Builder`，字段里既有 TVM 的 `ir_builder`，也有 tilelang 自有的 `frames` 栈、`name_inside_frame`（名字→定义它的 frame）作用域表，以及 eager 专用的 `eager_jit` 阶段标志与 `constexpr_var` 集合：

[Builder 字段与初始化 — builder.py:201-218](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L201-L218)

`current()` 用 `threading.local()` 取当前线程的构建器，没有就返回 `None`。DSL 原语（如 `T.Kernel`）正是靠它判断「我现在在不在一个 kernel 构造过程里」：

[Builder.current() — builder.py:220-223](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L220-L223)

`prim_func` 是 `Builder` 上最顶层的上下文管理器。注意它三件事：登记线程局部 builder、清空 let 表、用 `with self.ir_builder, self.with_frame(tirx.prim_func())` 同时打开 TVM IRBuilder 与 prim_func 帧；退出时无论成功失败都清理：

[Builder.prim_func 上下文 — builder.py:225-237](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L225-L237)

frame 栈的压弹核心。`enter_frame` 把 frame 追加到 `self.frames` 并触发其 `__enter__`（这一步通常会让 TVM IRBuilder 开始记录后续语句到这个 frame 名下）；`with_frame` 在退出时把所有比进入点更新的 frame 全部 `__exit__` 弹掉，保证异常路径下栈也能回卷：

[enter_frame / with_frame — builder.py:278-292](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L278-L292)

栈查询靠 `find_frame_idx`，从栈顶往栈底找第一个指定类型的 frame。它被 `bind`、`rval` 等用来判断「这个名字在不在某个控制流帧里」「当前是不是在宏里」：

[find_frame_idx — builder.py:272-276](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L272-L276)

frame 栈的另一半在 [frame.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/frame.py)。这里有一个通用的 `FrameStack`（deque + 变量值映射），以及为 `let` 绑定服务的线程局部栈。`Builder.bind_immutable` 在把一个 `PrimExpr` 绑定成 `tirx.bind(value)` 后，会调用 `register_let_value(var, value)` 把「这个 Var 对应哪个表达式」记到栈上，供后续 layout 推理 / 别名恢复时反查：

[FrameStack — frame.py:12-103](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/frame.py#L12-L103)

[register_let_value / get_let_value — frame.py:117-130](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/frame.py#L117-L130)

> 注意区分两套栈：`Builder.frames`（tilelang 自有的「作用域」栈，元素是各种 frame 对象）与 `frame.py` 里的 `FrameStack`/`LetFrame`（专门跟踪 `let` 绑定的值映射）。前者管结构，后者管值别名。

#### 4.1.4 代码实践

**目标**：直观看到「执行函数体 = 往 IRBuilder 里追加节点」。

**步骤**（源码阅读型，无需 GPU）：

1. 写一个最小 lazy 风格 kernel（lazy 风格直接返回 `PrimFunc`，路径最短，便于观察）：

```python
# file: trace_builder.py
import tilelang.language as T

@T.prim_func
def add(A: T.Tensor((128,), "float32"), B: T.Tensor((128,), "float32"), C: T.Tensor((128,), "float32")):
    with T.Kernel(1, threads=128) as bx:
        for i in T.Parallel(128):
            C[i] = A[i] + B[i]

print(type(add))            # <class 'tvm.tirx.prim_func.PrimFunc'>
print(add.script())         # 打印搭出来的 TIR
```

2. 运行 `python trace_builder.py`。

**观察现象**：

- `@T.prim_func` 装饰后 `add` 已经是一个 TVM `PrimFunc` 对象，说明函数体在装饰阶段就被执行、TIR 已搭好。
- `add.script()` 输出形如（具体文本以本地为准）：

```
@T.prim_func
def add(A: T.Buffer(128, "float32"), ...):
    # body
    with T.block():
        with T.launch_thread(threadIdx.x, 128):
            for i in T.parallel(128):
                C[i] = A[i] + B[i]
```

**预期结果**：你能看到一个 `for ... in parallel` 与一个 `BufferStore`，这正是 `T.Parallel` → ForFrame、`C[i] = ...` → `bind`/`assign_slice` → `buffer_store` 搭出来的。具体 IR 文本与 TVM 版本有关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `T.Parallel(128)` 换成 `T.serial(128)`，重新打印 `add.script()`，指出 For 节点的种类（kind）变化。

> **答**：`T.Parallel` 生成的 `For` 节点 kind 为 `Parallel`（后续由 LayoutInfer 映射到线程）；`T.serial` 的 kind 为 `Serial`。两者都由 4.4 节的 `ctx_for` 经 `tirx.serial`/对应 frame 发射。

**练习 2**：在 `@T.prim_func` 函数体 **之外** 调用 `T.Kernel(1)`，会发生什么？为什么？

> **答**：抛 `JITNoBuilderError`。因为 `T.Kernel` 内部会取 `Builder.current()`，函数体之外没有 `builder.prim_func(...)` 上下文去登记线程局部 builder，`current()` 返回 `None`。详见 4.4 节对 `kernel.py` 的引用。

---

### 4.2 prim_func 装饰器与 AST 改写（mutate / DSLMutator）

#### 4.2.1 概念说明

`@T.prim_func` 是用户最熟悉的入口。它的本质是一个「编译期翻译器」：把一个普通 Python 函数翻译成一个 **IR 生成器（IRGenerator）**，再用 4.1 节的 `Builder` 把它跑出来。

翻译的核心是 `mutate(func)`，它做的事是 **Python AST 改写**：用一个 `ast.NodeTransformer`（`DSLMutator`）遍历函数 AST，把每种语句改写成对构建器 `__tb` 的方法调用。改写后的函数被编译成一个闭包 `make_closure`，接收所有闭包（nonlocal）变量，返回形如 `def kernel(__tb): ...` 的新函数。`IRGenerator` 就是 `{gen: 该闭包, source: 改写后的源码字符串}`。

为什么要这么做，而不是写一个 Python 解释器去解释用户代码？因为复用 CPython 自己来「执行」改写后的代码最省事——循环、异常、闭包都由 Python 负责，tilelang 只需要在每个「关键语句」处插一个钩子。这正是 `DSLMutator` 的设计哲学：**劫持语句，而不是解释语句**。

#### 4.2.2 核心流程

```
@T.prim_func 装饰 func
   │
   ▼
prim_func.impl(func)                      # builder.py:1505
   │  sig = inspect.signature(func)
   │  ir_gen = mutate(func)               # ← AST 改写，得到 IRGenerator
   │  annot = 收集参数类型标注
   ▼
mutate(func)                              # ast.py:658
   │  tree = get_ast(func)                # inspect.getsource + ast.parse
   │  nonlocals = get_func_nonlocals(func)
   │  tree = DSLMutator(...).visit(tree)  # ★ 改写每种语句
   │  make_closure = get_compiled_object(tree, "make_closure", ...)  # compile + exec
   │  fn = make_closure(**nonlocals)      # 得到 def kernel(__tb): ...
   ▼
IRGenerator(gen=fn, source=ast.unparse(tree))
   │
   ▼ （回到 prim_func.impl 的非 eager_jit 分支）
builder = Builder()
with builder.prim_func(func.__name__):
    ir_gen.gen(builder)(**annot)          # ★ 执行改写后的函数体，__tb=builder
prim_func = builder.get()                 # 收出 PrimFunc
```

`DSLMutator` 的改写规则（节选）：

| 用户写法 | 改写后（伪代码） | 触发的构建器钩子 |
| --- | --- | --- |
| `x = expr` | `x = __tb.bind("x", expr)` | `Builder.bind` |
| `x = expr  # 带类型标注` | `x = __tb.bind("x", expr, annot)` | `Builder.bind`（annot 分支） |
| `buf[i] = expr` | `__tb.assign_slice(buf, i, expr)` | `Builder.assign_slice` |
| `x += expr` | `x = __tb.aug_assign("Add", x, expr, name="x")` | `Builder.aug_assign` |
| `for i in rng:` | `for _t in __tb.ctx_for(rng): i = __tb.bind("i", _t)` | `Builder.ctx_for` + `bind` |
| `if cond:` / `else:` | `for br in __tb.ctx_if(cond):` 内部 `ctx_then/ctx_else` | `Builder.ctx_if/then/else` |
| `expr`（表达式语句，如 `T.copy(...)`） | `__tb.eval(expr)` | `Builder.eval` |
| `return v` | `return __tb.ret(v)` | `Builder.ret` |
| `with X:` | `with __tb.ctx_with(X):` | `Builder.ctx_with` |
| `a and b` / `a == b` / `c then else` | `__tb.boolop / ifexp` | `Builder.boolop / ifexp` |
| 读名字 `a` | `__tb.rval("a", a)` | `Builder.rval`（作用域检查） |

改写还要做一件重要的事：在每条语句前插入 `__tb.set_fileline(file, lineno, fn)`（由 `SpanAttacher` 完成），这样生成的 TIR 节点能带上源码位置，报错时能指回用户源文件。

#### 4.2.3 源码精读

先看用户面装饰器 `prim_func`。它有两条分支：`eager_jit=True` 时返回一个 `JITFunc`（交给 JIT 层延迟构建，见 4.3 节）；否则立即用一个 `Builder` 把函数体跑出来，得到 `PrimFunc`，并用 `_patch_prim_func_attrs` 把 `out_idx`/pass_configs/compile_flags 挂到函数属性上。注意它捕获异常时会 `logger.fatal` 打印 `ir_gen.source`——这是排查「改写后代码跑崩了」的关键线索：

[prim_func 装饰器 — builder.py:1505-1545](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1505-L1545)

`mutate` 是改写入口。它取 AST、取闭包变量、用 `DSLMutator` 改写、编译成 `make_closure`、调用 `make_closure(**nonlocals)` 得到目标函数，最后包成 `IRGenerator`。注释解释了为什么用 `make_closure` 包一层（隔离闭包命名空间，避免复制 globals 造成内存泄漏）：

[mutate — ast.py:658-711](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L658-L711)

辅助函数：`get_ast` 用 `inspect.getsource` 取源码再 `ast.parse`（因此被改写的函数必须定义在真实文件里，不能用 `-c` 内联）；`get_func_nonlocals` 是改造版 `inspect.getclosurevars`；`get_compiled_object` 负责编译并 `exec`：

[get_ast / get_func_nonlocals / get_compiled_object — utils.py:35-86](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/utils.py#L35-L86)

现在看改写器本身。`DSLMutator` 是一个 `ast.NodeTransformer`，配合一个小工具 `quote`：`quote(template, **kws)` 把一段模板字符串解析成 AST，并用 `QuoteVisitor` 把模板里的占位名替换成真实子树。这让改写可以用「写一段示例代码 + 占位符」的可读方式生成 AST：

[quote 模板工具 — ast.py:60-75](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L60-L75)

几个最具代表性的改写规则。`visit_If` 把 `if/else` 改写成「先 `ctx_if` 产出一个哨兵 `br`，再用 `ctx_then(br)`/`ctx_else(br)` 决定进入哪一支」——这样构建器就能区分「TIR 条件（生成 If 节点）」与「Python 常量条件（编译期折叠，只保留一支）」：

[DSLMutator.visit_If — ast.py:279-294](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L279-L294)

`visit_For` 把 `for i in rng:` 改写成 `for _t in __tb.ctx_for(rng): <body with i=_t>`。注意循环变量 `i` 的绑定是放进 body 头部的（`_emit_assign_target`），这样 `ctx_for` 可以自由地决定 `_t` 到底是普通整数（Python 折叠）还是 TIR `Var`（生成 For 节点）：

[DSLMutator.visit_For — ast.py:309-322](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L309-L322)

`visit_FunctionDef` 是整个改写的「外壳」：它在函数体最前面插入每个参数的 `__tb.arg("name", name)` 绑定，清空装饰器列表，注入 `range = __tb.override('range')`（让裸 `range` 也走 tilelang 的 `serial` 语义），再用 `SpanAttacher` 给每条语句贴上 `set_fileline`。最后把整件事包成 `make_closure(...) -> def kernel(__tb): ...`：

[DSLMutator.visit_FunctionDef — ast.py:477-512](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L477-L512)

赋值的改写集中在 `visit_Assign` 与 `_emit_assign_target`。后者区分三种左值：`Name`（→ `bind`）、`Subscript`（→ `assign_slice`）、元组（→ 两阶段绑定，支持 `a, b = b, a` 的交换语义）：

[visit_Assign / _emit_assign_target — ast.py:332-438](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L332-L438)

最后，`BaseBuilder`（`Builder` 的父类）提供了所有钩子的 **Python 语义默认实现**：`bind` 直接返回值、`ctx_if` 直接 yield 条件、`boolop` 直接用 Python 的 `and/or/not`。这意味着如果用一个「不发射 TIR」的子类去跑改写后的函数，它就会退化为「普通 Python 执行」——这套设计让同一份改写代码既能用于真编译（`Builder`），也能用于纯语义求值（`BaseBuilder`）：

[BaseBuilder 默认实现 — ast.py:175-256](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L175-L256)

[IRGenerator 数据类 — ast.py:636-640](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L636-L640)

#### 4.2.4 代码实践

**目标**：亲眼看到「用户函数被改写成了什么」。

**步骤**（源码阅读型，无需 GPU）：

```python
# file: trace_mutate.py
import tilelang.language as T
from tilelang.language.eager.ast import mutate   # 直接取改写器

def my_kernel(A, B, C):
    with T.Kernel(1, threads=128) as bx:
        for i in T.serial(128):
            C[i] = A[i] + B[i]

print(mutate(my_kernel).source)
```

运行 `python trace_mutate.py`。

**观察现象**：打印出的源码不再是原文，而是一个 `def make_closure(...):` 外壳 + 内层 `def my_kernel(__tb):`，其中：

- 参数行变成 `A = __tb.arg("A", A)`；
- `with T.Kernel(...)` 变成 `with __tb.ctx_with(T.Kernel(...)):`，且前面多了一句 `if __tb.skip_kernel_ctx(): return`（eager phase1 跳过 kernel 体）；
- `for i in T.serial(128):` 变成 `for _tmp in __tb.ctx_for(T.serial(128)):` 后跟 `__tb_fl=...; __tb_fn=...; __tb.set_fileline(...)` 与 `i = __tb.bind("i", _tmp)`；
- `C[i] = A[i] + B[i]` 变成 `__tb.assign_slice(C, i, __tb.rval("A", A)[i] + __tb.rval("B", B)[i])`。

**预期结果**：你会清楚看到「每一行用户代码对应哪个 `__tb.<hook>` 调用」，这就是「Python 语句 → 构建器调用」的完整序列。改写后的确切文本与 tilelang 版本有关，**待本地验证**，但结构必定如上。

> 小贴士：当 `@T.prim_func` 报错时，错误信息里 `source=...` 那段（来自 [builder.py:1542](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1542)）正是这份改写后的源码，读懂它就能定位是哪个钩子炸了。

#### 4.2.5 小练习与答案

**练习 1**：`visit_FunctionDef` 里为什么要插入 `range = __tb.override('range')`？如果删掉会怎样？

> **答**：为了让用户在 tilelang kernel 里写裸 `range(...)` 时，走 tilelang 的 `serial` 语义（生成 TIR For 节点），而不是 Python 内置 `range`（会被 Python 直接展开成普通循环，不进入构建器）。`override('range')` 在 [builder.py:747-752](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L747-L752) 返回 `tilelang.language.serial`。删掉后，`for i in range(128)` 不会生成 TIR 循环。

**练习 2**：`BaseBuilder.bind` 的默认实现是「直接 `return value`」（[ast.py:210-211](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L210-L211)），而 `Builder.bind` 做了一大堆事。这种「基类给 Python 语义、子类给 TIR 语义」的设计有什么好处？

> **答**：同一份改写后的代码（`__tb.xxx` 调用序列）可以喂给不同构建器：给 `BaseBuilder` 就是普通 Python 求值（可用于静态分析、shape 推断），给 `Builder` 就是发射 TIR。复用了解释逻辑，只在钩子处分叉。

---

### 4.3 JITFunc：lazy / eager 与两阶段模板

#### 4.3.1 概念说明

`@T.prim_func` 是「立刻搭出 TIR」，但 `@tilelang.jit` 不能立刻搭——因为它要到 **第一次被调用** 时才知道参数（比如 `block_M=128`），甚至要知道输入张量的实际形状才能编译。所以 JIT 层需要一个「延迟搭 TIR」的抽象，这就是 `JITFunc`。

`JITFunc` 把用户函数包成两种风格之一：

- **lazy 风格**：用户函数体内嵌套定义并 `return` 一个 `@T.prim_func`。调用 `JITFunc` 就是把这个内层 prim_func 取出来，TIR 由 `@T.prim_func` 自己搭好。
- **eager 风格**：用户函数用 `T.const`、`T.Tensor` 标注、`T.empty`、`return` 这套写法（没有内层 `@T.prim_func`），TIR 要靠 `Builder` 追踪函数体来搭。

eager 风格最大的特点是 **一个函数模板要服务于多种输入 shape**（例如同一个 matmul 既能跑 1024×1024 也能跑 2048×2048）。为此 tilelang 设计了 **两阶段（phase1 / phase2）模板机制**：

- **phase1（建模板）**：用 `T.const` 占位符跑一遍函数体，得到一份「带符号维度空洞」的 `PrimFunc` 模板，并记录每个 `T.const` 出现在哪些 buffer 的 shape/stride 的第几维（`matcher`）。
- **phase2（填模板）**：拿到真实输入张量后，从其 shape/stride 抽出具体数值，**替换** 模板里的占位符，得到一份特化的 `PrimFunc`。

`Builder.eager_jit` 字段标记当前处于哪个阶段（`"phase1"` / `"phase2"` / `"none"`）。`T.const`、`T.annotate_pass_configs` 等函数会根据阶段做不同的事。

#### 4.3.2 核心流程

```
@tilelang.jit(func) -> JITImpl(func=JITFunc(...))            # jit/__init__.py
                          │
   首次调用，推断风格：JITFunc._is_lazy_style(...)            # 是否内含 @T.prim_func？
                          │  → set_mode("lazy" | "eager")
                          ▼
   JITFunc.parse_args(*args, **kwargs)                       # 产缓存键 + tensor 参数
                          │
                          │  bound = _argument_binder.bind(args, kwargs)
                          │       → p1_key（编译期值的元组）
                          │       → tensor_args（运行期张量）
                          ▼
   p1_cache 查/建 TirTemplate                                # phase1
                          │  miss → _build_tir_template：
                          │           Builder(eager_jit="phase1")
                          │           builder.prim_func(name) → 跑函数体
                          │           → TirTemplate.create(pf, constexpr_var)
                          ▼
   p2_key = TirTemplate._parse_phase2_key(**tensor_args)     # 从张量抽 shape/stride
                          │
                          ▼
   TirTemplate.get_tir(tensor_args, ...)                     # phase2
                          │  Builder(eager_jit="phase2")
                          │  builder.eager_jit_subs = {常量名: 实际值}
                          │  重跑函数体 → 用实际值替换占位符
                          │  → 特化 PrimFunc
                          ▼
   返回 PrimFunc（再交给 u4 讲过的 lower()/codegen）
```

缓存键的设计：`(p1_key, p2_key)` 二元组——`p1_key` 捕获编译期参数（block 尺寸等），`p2_key` 捕获运行期形状。两者都相同才算命中。这部分细节属于 u4-l2/l3，本讲只关注 `JITFunc` 如何驱动 phase1/phase2。

#### 4.3.3 源码精读

`JITFunc` 的字段与初始化。它持有原始函数、签名、tensor 参数字典、`IRGenerator`，以及一个 `p1_cache`（phase1 模板缓存）和参数绑定器 `_argument_binder`：

[JITFunc 类与 __post_init__ — builder.py:1345-1374](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1345-L1374)

风格判定 `_is_lazy_style`：先用 `has_internal_prim_func` 静态扫描 AST 看有没有内层 `@T.prim_func`；没有就尝试调用一次原函数，看返回值是不是 `PrimFunc`；若调用过程中抛 `JITNoBuilderError`/`EagerJITBuildError`（典型是函数体里用了 `T.const`/`T.Kernel` 但没有 builder），就判定为 eager。这套「试一次」的探测逻辑是 eager/lazy 自动推断的核心：

[_is_lazy_style — builder.py:1380-1418](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1380-L1418)

[has_internal_prim_func（AST 扫描内层 prim_func） — ast.py:643-655](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L643-L655)

`set_mode` 仅是一个setter（`"lazy"`/`"eager"`），由上层 `JITImpl` 在首次调用前注入：

[set_mode — builder.py:1466-1468](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1466-L1468)

`_build_tir_template` 是 phase1 的入口。lazy 分支直接把原函数返回的 `PrimFunc` 包成模板；eager 分支创建 `Builder`、设 `eager_jit="phase1"`、进 `prim_func` 上下文跑 `ir_gen.gen(builder)`，最后用 `TirTemplate.create` 把 `PrimFunc` 与收集到的 `constexpr_var` 一起打包：

[_build_tir_template — builder.py:1420-1435](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1420-L1435)

`parse_args` 串起缓存与模板：用 `_argument_binder.bind` 把入参拆成 `p1_key` + `tensor_args`，按 `p1_key` 查/建模板，再用 `_parse_phase2_key` 从 tensor 抽出 `p2_key`，返回 `((p1_key, p2_key), tensor_args)`：

[parse_args — builder.py:1437-1450](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1437-L1450)

`TirTemplate` 是两阶段机制的载体。`create` 会校验每个 constexpr 变量都 **直接** 出现在某个 buffer 的 shape 或 stride 里（否则报「Constexpr variable `x` is not used in any buffer shape or stride」），并构建 `matcher`（constexpr 变量 → 它出现在哪个 buffer 的 shape/stride 的第几维）：

[TirTemplate.create — builder.py:1042-1067](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1042-L1067)

`get_tir` 是 phase2 的核心：lazy 直接返回模板；eager 用 `Builder(eager_jit="phase2")` + `eager_jit_subs`（常量名→实际值）**重跑一遍函数体**——这次 `T.const(...)` 不再返回占位符，而是返回 `eager_jit_subs` 里的真实数值，于是搭出的 `PrimFunc` 就是特化版本：

[TirTemplate.get_tir — builder.py:1097-1109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1097-L1109)

`T.const` 是阶段切换的开关。phase1 调 `builder.constexpr(name)` 创建符号 `Var` 并加入 `constexpr_var` 集合；phase2 直接返回 `builder.eager_jit_subs[name]` 里的实际值；`"none"` 阶段调用则抛 `JITNoBuilderError`：

[T.const — builder.py:922-962](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L922-L962)

> 同理，`T.annotate_pass_configs` / `T.annotate_compile_flags` 也读 `builder.eager_jit`：phase1 直接 `return`（不记录），phase2/none 才真正写入 `builder.func_pass_configs` / `func_compile_flags`，最后由 `_patch_prim_func_attrs` 挂到 `PrimFunc` 属性上（[builder.py:1011-1022](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1011-L1022)）。这保证 phase1 建模板时不会因为缺形状而失败。

#### 4.3.4 代码实践

**目标**：体会「phase1 建模板、phase2 填形状」——同一份函数模板服务两种 shape。

**步骤**（需 GPU；若无可只读源码）：

```python
# file: trace_phase.py
import torch, tilelang
import tilelang.language as T

@tilelang.jit
def matmul(A, B, block_M: int = 128, block_N: int = 128, block_K: int = 32):
    M, N, K = T.const("M, N, K")          # 编译期占位符
    dtype = T.float16
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_s = T.alloc_shared((block_M, block_K), dtype)
        B_s = T.alloc_shared((block_K, block_N), dtype)
        C_l = T.alloc_fragment((block_M, block_N), T.float32)
        T.clear(C_l)
        for ko in T.serial(T.ceildiv(K, block_K)):
            T.copy(A[by*block_M, ko*block_K], A_s)
            T.copy(B[ko*block_K, bx*block_N], B_s)
            T.gemm(A_s, B_s, C_l)
        T.copy(C_l, C[by*block_M, bx*block_N])
    return C

k1 = matmul.compile(M=1024, N=1024, K=1024)   # phase1 建模板，phase2 填 1024
k2 = matmul.compile(M=2048, N=2048, K=1024)   # 复用 phase1 模板，phase2 填 2048

# 查看模板与特化 IR
print("template p1 keys:", list(matmul.func.p1_cache.keys()))
print(k1.get_kernel_source()[:200])
```

**观察现象**：

- `matmul.func.p1_cache` 里只应有 **一个** TirTemplate（不同 `M/N/K` 共享同一 phase1 模板，因为它们是 `T.const` 运行期参数）；而 `block_M/block_N/block_K` 是普通 Python 参数，改变它们会产生新的 phase1 模板。
- `k1` 与 `k2` 的 kernel source 在网格维度上不同（一个 `grid=(8,8)`，一个 `grid=(16,16)`），对应 phase2 的不同填充。

**预期结果**：证明「phase1 一次建模板、phase2 多次填形状」的复用机制确实生效。无 GPU 时该运行结果 **待本地验证**；可退化为阅读 `TirTemplate.get_tir`（[builder.py:1097-1109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1097-L1109)）源码理解。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `M, N, K = T.const("M, N, K")` 改成普通参数 `def matmul(M, N, K, A, B, block_M=128, ...)`，会发生什么？

> **答**：`M/N/K` 不再是 constexpr 占位符，而成了 phase1 的编译期键的一部分。每换一组 `M/N/K` 就会触发一次新的 phase1 编译（重新搭整个模板），失去「一份模板通吃多 shape」的复用。这也是 `T.const` 存在的意义。

**练习 2**：为什么 `TirTemplate.create` 要求每个 constexpr 变量至少 **直接** 出现在某个 buffer 的 shape 或 stride 里（[builder.py:1054-1065](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1054-L1065)）？

> **答**：因为 phase2 的 `_parse_phase2_key` 是从 **实际传入张量** 的 shape/stride 里反查常量值的（`matcher` 记录的就是「常量出现在哪个 buffer 的第几维」）。如果一个常量从不出现在任何 buffer shape/stride 里，phase2 就无从知道它的值，只能要求用户用关键字显式传入。

---

### 4.4 跟踪语句：T.Kernel / 赋值 / for / T.copy 如何落到 TIR

#### 4.4.1 概念说明

前三节分别讲了「工作台」「改写器」「调度器」。本节把它们 **缝起来**：选几类最典型的语句，逐一追踪「用户写的一行 → 改写后的 `__tb.xxx` → `Builder` 钩子 → 最终 TIR 节点」。掌握这条链路，你就能在任意 kernel 里定位「这一行对应 IR 里的什么」。

#### 4.4.2 核心流程

以这段典型 eager kernel 为例：

```python
with T.Kernel(grid, threads=128) as (bx, by):   # (a) kernel 启动上下文
    A_s = T.alloc_shared((128, 32), dtype)       # (b) 分配 shared
    for ko in T.serial(K//32):                    # (c) 串行循环
        T.copy(A[...], A_s)                       # (d) 表达式语句
    A_s[0, 0] = 0.0                               # (e) 标量写
```

| 语句 | 改写后 | `Builder` 钩子 | 最终 TIR 节点 |
| --- | --- | --- | --- |
| (a) `with T.Kernel(...)` | `with __tb.ctx_with(T.Kernel(...))` + 进 frame 前的 `skip_kernel_ctx` 判断 | `ctx_with` → 把 `KernelLaunchFrame` 压栈；`T.Kernel` 内部 `_ffi_api.KernelLaunch(...)` | grid/block 的 `thread_binding` For 循环（target 无关，后端经 `MaterializeKernelLaunch` 物化） |
| (b) `A_s = T.alloc_shared(...)` | `A_s = __tb.bind("A_s", T.alloc_shared(...))` | `bind` → `bind_immutable`（返回 Buffer，调 `IRBuilder.name`） | `allocate` 节点（scope=`shared.dyn`），后续经 Pass 合并 |
| (c) `for ko in T.serial(...)` | `for _t in __tb.ctx_for(T.serial(...)): ko = __tb.bind("ko", _t)` | `ctx_for` → `with_frame(tirx.serial(stop))` | `For` 节点（kind=`Serial`） |
| (d) `T.copy(A[...], A_s)` | `__tb.eval(T.copy(A[...], A_s))` | `eval` → 若值是 `PrimExpr`（intrinsic 调用）则 `tirx.evaluate(val)` | `Evaluate` 包裹的 `tl.tileop.copy` intrinsic（后续 Pass 展开为 cp.async/TMA/循环） |
| (e) `A_s[0,0] = 0.0` | `__tb.assign_slice(A_s, (0,0), 0.0)` | `assign_slice` → `tirx.buffer_store(A_s, 0.0, sl)` | `BufferStore` 节点 |

注意 (a) 的特殊性：`T.Kernel` 不通过改写器注入 `__tb`，而是 **自己** 在 [kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py) 里取 `Builder.current()`。这是「DSL 原语感知构建器」的典型范式。

#### 4.4.3 源码精读

先看 `T.Kernel`。它第一件事就是查 `Builder.current()`，没有就抛 `JITNoBuilderError`——这正是「`T.Kernel` 只能在构建器上下文里用」的根因；随后归一化 threads、组装 attrs、调用 `_ffi_api.KernelLaunch(...)` 返回一个 `KernelLaunchFrame`（一个 `TIRFrame`，进入 `with` 时把自己压上 TVM IRBuilder 的栈）：

[T.Kernel — kernel.py:277-340](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L277-L340)

`KernelLaunchFrame.__enter__` 把自己压上 tilelang 自维护的 `kernel_launch_frame_stack`，并返回 grid 循环变量（除去末尾 4 个 frame：3 个 threadIdx + 1 个 block 属性帧）。这就是 `as (bx, by)` 能拿到 blockIdx 绑定的原因：

[KernelLaunchFrame — kernel.py:149-180](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L149-L180)

回到 `Builder`。`ctx_with` 是 `with` 语句的钩子：若是 `IRBuilderFrame`（如 `KernelLaunchFrame`），用 `with_frame` 包起来压栈；否则走父类默认（普通 Python `with`）：

[Builder.ctx_with — builder.py:682-687](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L682-L687)

赋值的钩子 `bind` 是最复杂的一个，但它的主干是分情况转发。几个关键分支：在 prim_func 顶层、对纯 `PrimExpr` 直接返回（避免在 `match_buffer` 前产生多余 LetStmt，[builder.py:428-433](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L428-L433)）；`Var`/`Buffer` 用 `IRBuilder.name` 命名并登记作用域（[builder.py:485-494](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L485-L494)）；其余转发给 `bind_immutable`：

[Builder.bind（赋值钩子） — builder.py:416-509](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L416-L509)

`bind_immutable` 处理「不可变 tilelang 对象」的绑定：`OutTensor`（来自 `T.empty`）转成 `tirx.arg(...)` 并记 `_out_idx`（eager 返回值机制）；`PrimExpr`/`BufferRegion` 走 `tirx.bind(value)` 生成一个 `Var`（即 TIR 的 `LetStmt`），并 `register_let_value` 记录别名：

[Builder.bind_immutable — builder.py:522-555](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L522-L555)

`ctx_for` 是循环钩子。它把 `T.serial`/`T.unroll`/带步长 range 等统一处理：先算出真实的 trip count（`ceildiv`），再 `with_frame(tirx.serial/unroll(real_stop))`，并把「循环变量的仿射表达式」`start + v*step` 作为 yield 值交给改写后的 body（即 `__tb.bind("ko", _t)` 里的 `_t`）：

[Builder.ctx_for — builder.py:345-383](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L345-L383)

表达式语句钩子 `eval`。它是 `T.copy`/`T.gemm`/`T.clear` 这类「写成语句的函数调用」的落脚点：值是 `PrimExpr`（intrinsic 调用）就 `tirx.evaluate(val)` 发射一个 `Evaluate` 节点；是 `BufferStore` 就重新 `buffer_store`；是平凡值（int/str/Buffer）则忽略或警告：

[Builder.eval — builder.py:321-343](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L321-L343)

最后是 `rval`——读名字时触发。它先做 **作用域检查**：若该名字的定义 frame 已不在当前 frames 栈里，就报「Immutable variable `x` is used outside its defining region」，这正是「变量在其 for 循环外被引用」这类错误的检测点：

[Builder.rval（名字读取 + 作用域检查） — builder.py:699-707](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L699-L707)

#### 4.4.4 代码实践

**目标**：把本讲主任务做掉——跟踪一个简单 kernel 从 Python 调用到生成 `PrimFunc` 的语句序列，写出追踪笔记。

**步骤**（源码阅读型 + 轻量 monkey-patch，不修改任何源码；无需 GPU）：

```python
# file: trace_full.py
import tilelang.language as T
from tilelang.language.eager import builder as B
from tilelang.language.eager.ast import mutate

# ---- 1) 改写后的语句序列：直接读 mutate().source ----
def my_kernel(A, B, C):
    with T.Kernel(1, threads=128) as bx:
        A_s = T.alloc_shared((128,), "float32")
        for i in T.serial(128):
            C[i] = A[i] + B[i]

print("===== 改写后的源码（语句序列） =====")
print(mutate(my_kernel).source)

# ---- 2) 运行期的钩子调用序列：用猴子补丁打日志（不改源码） ----
def _log(label, fn):
    def wrapper(self, *a, **kw):
        print(f"[hook] {label}")
        return fn(self, *a, **kw)
    return wrapper

B.Builder.bind        = _log("bind",        B.Builder.bind)
B.Builder.bind_immutable = _log("bind_immutable", B.Builder.bind_immutable)
B.Builder.ctx_for     = _log("ctx_for",     B.Builder.ctx_for)
B.Builder.ctx_with    = _log("ctx_with",    B.Builder.ctx_with)
B.Builder.eval        = _log("eval",        B.Builder.eval)
B.Builder.assign_slice = _log("assign_slice", B.Builder.assign_slice)

print("\n===== 运行时钩子调用序列 =====")
@T.prim_func
def add(A: T.Tensor((128,), "float32"),
        B: T.Tensor((128,), "float32"),
        C: T.Tensor((128,), "float32")):
    with T.Kernel(1, threads=128) as bx:
        A_s = T.alloc_shared((128,), "float32")
        for i in T.serial(128):
            C[i] = A[i] + B[i]
```

运行 `python trace_full.py`。

**操作步骤**：把上面文件保存为 `trace_full.py` 后运行；第一步先单独跑（注释掉第 2 步的猴子补丁），看清改写后源码；再打开第 2 步，看运行时钩子顺序。

**需要观察的现象**：

1. 第 1 步打印的改写源码里，能逐行对应到 4.4.2 表中的 `__tb.xxx` 调用。
2. 第 2 步打印的钩子顺序大致为：`ctx_with`（进 `T.Kernel`）→ `bind`/`bind_immutable`（`A_s = T.alloc_shared(...)`）→ `ctx_for`（`for i in T.serial`）→ 循环体内 `assign_slice`（`C[i] = ...`）。注意 `T.alloc_shared` 本身是普通函数调用，其结果经 `bind` 绑定。

**预期结果**：你会得到一份「源码行 ↔ 改写调用 ↔ 运行时钩子」三栏对照的追踪笔记，证明每个语句确实经过了 4.1–4.3 描述的链路。钩子的确切顺序与 tilelang 版本有关，**待本地验证**，但上表给出的对应关系稳定成立。

> 若 `inspect.getsource` 报错（如用 `-c` 或交互式 REPL 运行），请把函数写在真实 `.py` 文件里——`mutate` 依赖 `inspect.getsource`（见 [utils.py:59-66](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/utils.py#L59-L66)）。

#### 4.4.5 小练习与答案

**练习 1**：在 4.4.4 的 kernel 里把 `C[i] = A[i] + B[i]` 改成 `C[i] += A[i] + B[i]`，重新跑，观察钩子序列变化，并指出改写用到了哪个 `visit_` 方法。

> **答**：`+=` 走 `visit_AugAssign`（[ast.py:440-466](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L440-L466)），改写为 `__tb.aug_assign_slice("Add", C, i, A[i]+B[i])`，运行时钩子从 `assign_slice` 变成 `aug_assign_slice`。子目标 `C[i]` 的读会经 `rval`。

**练习 2**：为什么 `T.copy(A[...], A_s)` 不需要出现在 `bind` 钩子里，而是出现在 `eval` 钩子里？

> **答**：`T.copy(...)` 在 Python 语法上是一个 **表达式语句**（没有赋值目标），改写器走 `visit_Expr`（[ast.py:296-298](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L296-L298)）把它包成 `__tb.eval(...)`。`T.copy` 返回一个 `tl.tileop.copy` intrinsic（PrimExpr），`eval` 检测到是 PrimExpr 就 `tirx.evaluate(val)` 发射 `Evaluate` 节点。

---

## 5. 综合实践

把本讲四节串起来，完成一份「**tilelang eager 翻译全景图**」。

**任务**：以 `examples/quickstart.py` 的 eager matmul（[examples/quickstart.py:8-48](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L8-L48)）为对象，产出一份 markdown 笔记，包含三张表：

1. **改写表**：对 matmul 函数体的每一行，给出改写后的 `__tb.xxx` 调用。提示：把 quickstart 里的 `matmul` 函数复制到一个新文件，`from tilelang.language.eager.ast import mutate; print(mutate(matmul).source)` 即可拿到改写后源码。
2. **阶段表**：标注每一行在 phase1（建模板）和 phase2（填形状）里分别如何表现。重点关注 `M, N, K = T.const("M, N, K")`（[quickstart.py:10](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L10)）——它在 phase1 返回占位符 `Var`、在 phase2 返回实际数值；以及 `with T.Kernel(...)`（[quickstart.py:18](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L18)）——在 phase1 被 `skip_kernel_ctx()` 跳过（[builder.py:769-770](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L769-L770)），phase2 才真正进入。
3. **钩子表**：用 4.4.4 的 monkey-patch 打日志，列出运行时（phase2）钩子的实际触发顺序。

**验收标准**：

- 表 1 能解释「为什么 `A: T.Tensor((M, K), dtype)` 这种带标注的「赋值」其实是 `__tb.bind(..., annot=...)`」。
- 表 2 能说清「为什么 phase1 不会因为没有真实 M/N/K 值而崩溃」——因为 `T.const` 与 `T.Kernel` 都会检查 `eager_jit` 阶段并短路。
- 表 3 的钩子顺序能对上 `T.Kernel → alloc_shared → T.clear → for/Pipelined → T.copy → T.gemm → T.copy` 的结构。

**可选进阶**（需 GPU）：用 `matmul.compile(M=1024, N=1024, K=1024, ...)` 与 `matmul.compile(M=2048, N=2048, K=1024, ...)` 两次编译，检查 `matmul.func.p1_cache` 是否只缓存了 **一个** phase1 模板，从而验证两阶段复用。

## 6. 本讲小结

- tilelang 把「执行 Python 函数体」当作「往 TVM IRBuilder 里追加 TIR 节点」；`Builder` 是工作台，用 `frames` 栈管理作用域，用线程局部 `current()` 让 `T.Kernel`/`T.const` 等 DSL 原语能找到自己。
- `@T.prim_func` 背后是 `mutate()` 的 **AST 改写**：`DSLMutator` 把每个 `if`/`for`/赋值/表达式语句改写成对构建器 `__tb` 的方法调用，编译成闭包后包成 `IRGenerator`；`BaseBuilder` 提供 Python 语义默认，`Builder` 覆盖为 TIR 语义。
- `JITFunc` 是延迟搭 TIR 的抽象，分 lazy（用户 `return` PrimFunc）与 eager（构建器追踪）两风格；eager 用 **phase1 建模板、phase2 填实际形状** 的两阶段机制，由 `Builder.eager_jit` 字段与 `T.const` 切换，实现「一份模板通吃多 shape」。
- 每类语句都有固定的落地路径：`with T.Kernel` → `ctx_with`（`T.Kernel` 自取 `Builder.current()`）；赋值 → `bind`/`bind_immutable`（或 `assign_slice`）；`for` → `ctx_for`；表达式语句（`T.copy`/`T.gemm`/`T.clear`）→ `eval` → `tirx.evaluate`。
- `frame.py` 的 `FrameStack`/`LetFrame` 与 `register_let_value` 负责 `let` 绑定的值别名跟踪，服务于 layout 推理与 BufferRegion 恢复；它和 `Builder.frames`（作用域栈）是两套互补的栈。

## 7. 下一步学习建议

- **u5-l2（Python AST 解析器与 overrides）**：本讲的 `DSLMutator` 是「函数体改写」，而 `language/parser` + `language/overrides` 处理的是更细粒度的 **表达式/语义改写**（如 buffer、`T.copy` 的特殊处理），两者互补，建议紧接着读。
- **u6-1（Pass 系统与 PassContext）**：本讲产出 `PrimFunc`，之后进入 Pass 流水线；理解 `tilelang_out_idx`/`tilelang_pass_configs`/`tilelang_compile_flags` 这几个由 `_patch_prim_func_attrs` 挂上去的属性，是如何在 Pass 里被读到的。
- **延伸阅读源码**：想看「DSL 原语如何自取构建器」的更多例子，可读 [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py)、[tilelang/language/allocate.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py)，它们与 `T.Kernel` 一样依赖 `Builder.current()`。
