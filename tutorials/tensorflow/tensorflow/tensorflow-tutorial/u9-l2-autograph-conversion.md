# AutoGraph：Python 到图的转换

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 AutoGraph **要解决的根本问题**：为什么普通的 Python `if`/`while`/`for` 在 `tf.function` 追踪（tracing）时不能直接变成图。
- 理解 AutoGraph 的**两阶段设计**——编译期由 converter 做 AST→AST 改写，运行期（追踪期）由 operator 根据实际类型做派发并真正发出 TF op。
- 掌握三层分层：入口与流水线 `impl/api.py`、converter 基类与各 converter、operator 运行时实现 `operators/`。
- 认识 converter 用「模板替换 + 静态分析」把 `if` 改写成 `ag__.if_stmt(...)`，operator 用「类型派发」在 `tf.cond` / `tf.while_loop` 与普通 Python 之间二选一。
- 对照真实生成的代码，看懂 `if`/`while`/`for` 各自被替换成了哪些图结构。

本讲承接 [u3-l4 tf.function、ConcreteFunction 与 def_function](u3-l4-tf-function-and-concretefunction.md)：那讲解释了 `tf.function` 如何把一个普通函数**追踪**成 `ConcreteFunction`（一张 `tf.Graph`）；本讲回答追踪过程中**最棘手的一步**——Python 的命令式控制流是怎么被翻译成声明式图 op 的。

## 2. 前置知识

### 2.1 为什么需要 AutoGraph

在 [u3-l4](u3-l4-tf-function-and-concretefunction.md) 中我们看到，`tf.function` 的追踪过程是「在临时图模式下把你的 Python 函数原样跑一遍」。函数里每一条 op 调用（如 `tf.add`）都会在建图中登记一个节点。问题来了：

```python
@tf.function
def f(x):
  if x > 0:        # x 是张量，x > 0 也是张量
    return x * x
  else:
    return -x
```

当 `x` 是一个 `tf.Tensor` 时，`x > 0` 不是 Python 的 `True`/`False`，而是一个**布尔张量**。Python 的 `if` 语句需要对一个值做**静态的真假判断**（调用 `__bool__`），它无法「在两条分支之间图上二选一」。直接追踪时这会抛出 `OperatorNotAllowedInGraph` 错误。

唯一正确的做法是把这种 `if` 换成图里的 `tf.cond`（条件 op），把 `while` 换成 `tf.while_loop`。但要求每个用户手写 `tf.cond`/`tf.while_loop` 既繁琐又易错——这正是 **AutoGraph** 自动完成的事：**把普通的 Python 控制流，等价地翻译成 TensorFlow 图的控制流 op**。

### 2.2 命令式 vs 声明式

- **命令式（Python `if`/`while`/`for`）**：分支和循环由 Python 解释器在**追踪当下**决定执行哪一条路径。一旦数据是张量，解释器无法决定，于是卡住。
- **声明式（`tf.cond`/`tf.while_loop`）**：两条分支都**登记进图**，运行时再由图执行器按真实数据选择路径或迭代次数。

AutoGraph 的本质就是：**在命令式和声明式之间架桥**，让用户写命令式的 Python，得到声明式的图。

### 2.3 一句话点明分层（本讲核心心智模型）

AutoGraph **并不是**把 Python「直接编译成 TF op」。它分两个阶段：

1. **改写阶段（converter，AST→AST）**：把 `if cond: ...` 改写成一次函数调用 `ag__.if_stmt(cond, body, orelse, ...)`。注意这里**没有**任何 `tf.cond`，只是换了种 Python 写法。
2. **派发阶段（operator，追踪期运行）**：追踪时这行 `ag__.if_stmt(...)` 会被真正执行，operator 函数**检查 `cond` 的运行时类型**——是张量就发 `tf.cond`，是普通 Python 布尔就走原生 `if`。

这个「先改写成 `ag__` 调用、再在运行期按类型派发」的两段式，是理解整套源码的钥匙，请先记住它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
|------|------|----------|
| `tensorflow/python/autograph/impl/api.py` | 用户/代码生成器入口：`to_graph`、`to_code`、`converted_call`、转换流水线 `transform_ast` | 第 4.2 节 |
| `tensorflow/python/autograph/core/converter.py` | converter 基类 `Base`、`ConversionOptions`、`Feature`、`ProgramContext` | 第 4.3 节 |
| `tensorflow/python/autograph/converters/control_flow.py` | 把 `if/while/for` AST 改写成 `ag__.*` 调用 | 第 4.3 节 |
| `tensorflow/python/autograph/operators/__init__.py` | 汇总导出所有 operator（构成 `ag__` 模块） | 第 4.4 节 |
| `tensorflow/python/autograph/operators/control_flow.py` | `if_stmt`/`while_stmt`/`for_stmt` 运行时实现与类型派发 | 第 4.4 节 |
| `tensorflow/python/eager/polymorphic_function/autograph_util.py` | `tf.function` 接入 AutoGraph 的薄封装 | 第 4.2 节 |

> AutoGraph 实际是一个相对独立的「源码变换器（source-to-source transpiler）」，核心算法在 `pyct/`（Python Compile Tools）子包中（解析、CFG、活跃变量分析、模板替换、命名等），本讲侧重它的**分层与流程**，`pyct` 的细节点到为止。

## 4. 核心概念与源码讲解

### 4.1 两阶段设计：改写（converter）与派发（operator）

#### 4.1.1 概念说明

很多人初学 AutoGraph 会误以为它是一个「Python→TF op 的编译器」。更准确的描述是：

- **converter（转换器）** 只做 **AST 到 AST 的等价改写**：`if` 语句变成一次 `ag__.if_stmt(...)` 函数调用。这一步**完全不碰 TF**，产出的仍然是普通 Python 代码，用 `inspect.getsource` 就能打印出来。
- **operator（算子）** 是这些 `ag__.*` 函数的**运行时实现**。它们在追踪期被调用，**此时输入的真实类型（张量还是 Python 值）已经确定**，于是 operator 据此二选一：发 TF op，或退回 Python 语义。

为什么要分两段、而不是 converter 直接发 `tf.cond`？因为「该不该发 `tf.cond`」取决于**运行期值**（条件是不是张量），而 converter 工作在**静态 AST** 上，根本拿不到运行期值。所以 converter 只能把决策**延迟**到运行期，用一个「派发函数」`ag__.if_stmt` 来承接，再由它在追踪时见机行事。这就是「converter 负责静态改写、operator 负责运行期派发」分工的根本理由。

#### 4.1.2 核心流程

追踪一个 `@tf.function` 函数时，AutoGraph 的参与可以用下面这张流程图概括：

```
用户的 Python 函数 f（含 if/while/for）
        │  ① to_graph / converted_call
        ▼
┌───────────────────────────────────────────────┐
│ 编译期：PyToTF.transform_ast                   │
│  顺序跑十几个 converter（见 4.2.3）            │
│  其中 control_flow converter 把：               │
│    if cond: body       →  ag__.if_stmt(...)    │
│    while cond: body    →  ag__.while_stmt(...) │
│    for x in it: body   →  ag__.for_stmt(...)   │
│  产出新 Python 函数 tf__f（仍是普通 Python）    │
└───────────────────────────────────────────────┘
        │  ② tf.function 追踪 tf__f（在图模式下执行它一遍）
        ▼
┌───────────────────────────────────────────────┐
│ 运行期（追踪期）：operator 被真正调用           │
│  ag__.if_stmt(cond, ...)                       │
│    └─ tensors.is_dense_tensor(cond)?           │
│         是 → _tf_if_stmt → tf.cond（发图 op）   │
│         否 → _py_if_stmt → body() if cond ...  │
└───────────────────────────────────────────────┘
        │
        ▼
  最终的 tf.Graph（含 tf.cond / tf.while_loop 节点）
```

关键点：**converter 决定「在哪里改写」，operator 决定「改成 TF 还是 Python」**。下两节分别精读这两层。

### 4.2 入口与转换流水线：`impl/api.py`

#### 4.2.1 概念说明

`impl/api.py` 是 AutoGraph 对外的门面。它提供两类入口：

- **直接入口**：`tf.autograph.to_graph(f)` / `tf.autograph.to_code(f)`，给想完全控制图生成、绕开 `tf.function` 缓存机制的高级用户。
- **`tf.function` 内部入口**：`converted_call`，由生成代码在追踪期对「每一次函数调用」发起，决定被调函数要不要也被转换。

`tf.function`（[u3-l4](u3-l4-tf-function-and-concretefunction.md)）默认开启 AutoGraph。它在准备追踪选项时，会把用户的 Python 函数包一层「走 AutoGraph」的壳。接入点很薄：

[tensorflow/python/eager/polymorphic_function/autograph_util.py:23-59](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/eager/polymorphic_function/autograph_util.py#L23-L59) —— `py_func_from_autograph` 把原函数包成一个调用 `api.converted_call(...)` 的 handler，且 `user_requested=True`（表示这是用户显式要求转换，不受 allowlist 豁免）。

这个壳在 `polymorphic_function.py` 里被启用：

[tensorflow/python/eager/polymorphic_function/polymorphic_function.py:642-644](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L642-L644) —— `if self._autograph:` 时才把函数替换成 AutoGraph 版本，这正是 `tf.function(autograph=False)` 能关掉它的开关。

#### 4.2.2 核心流程

最直接的入口是 `to_graph`：

[tensorflow/python/autograph/impl/api.py:708-776](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/impl/api.py#L708-L776) —— `to_graph(entity)` 构造一个 `ProgramContext`（携带 `ConversionOptions`），调用 `_convert_actual` 返回一个**等价的新函数**。注意文档特别说明：它「不实现缓存、不管理变量、也不真正创建 op」，只产出「会用 TF op 的 Python 代码」——真正建图要靠后续在图模式下跑一遍这函数。

`_convert_actual` 只有三行核心逻辑：

[tensorflow/python/autograph/impl/api.py:260-275](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/impl/api.py#L260-L275) —— 把实体交给单例 `_TRANSPILER.transform(...)`，拿到「转换后的函数 + 临时模块 + 源码映射表（用于错误回溯）」。

这个单例在模块底部创建：

[tensorflow/python/autograph/impl/api.py:949](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/impl/api.py#L949) —— `_TRANSPILER = PyToTF()`。`PyToTF` 是真正的变换器类。

`converted_call` 则是生成代码里最常出现的调用。它在追踪期对每个函数调用做一系列「要不要转换」的判定：allowlist 缓存命中、AutoGraph 被禁用、是 AutoGraph 自身产物（artifact）、是 builtin、源码不可访问等，都会直接「不转换地调用」；否则才真的去转换再调用：

[tensorflow/python/autograph/impl/api.py:295-446](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/impl/api.py#L295-L446) —— 这就是为什么 `tf.function` 里调用 `tf.add`、`np.array` 等「白名单/内置」函数不会被无谓地转换，而被调用的用户函数才会递归转换。

#### 4.2.3 源码精读：`PyToTF` 与转换流水线

`PyToTF` 继承自通用的 `transpiler.PyToPy`（一个「Python→Python」变换器，负责解析、缓存、把变换后的 AST 重新加载成 Python 函数对象）。它只重写了几个关键方法。

**① 函数改名**：转换后的函数统一加 `tf__` 前缀（所以 `f` → `tf__f`），避免与原函数冲突，也方便在错误栈里识别：

[tensorflow/python/autograph/impl/api.py:186-194](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/impl/api.py#L186-L194)

**② 注入 `ag__` 命名空间**：这是最巧妙的一步。生成代码里到处是 `ag__.if_stmt(...)`，这个 `ag__` 从哪来？`PyToTF.get_extra_locals` 在加载生成函数时，**临时造一个名为 `ag__` 的模块对象**，把所有 operator、special functions、`ConversionOptions` 等塞进去，作为生成函数的「额外局部变量」注入：

[tensorflow/python/autograph/impl/api.py:196-217](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/impl/api.py#L196-L217) —— 注意倒数两行：`ag_internal.__dict__.update(operators.__dict__)` 把 `operators/__init__.py` 导出的全部函数挂上，再以 `{'ag__': ag_internal}` 返回。于是生成代码里 `ag__.if_stmt`、`ag__.while_stmt`、`ag__.converted_call` 都能在这里查到。**converter 只负责写出 `ag__.*` 的调用文本，operator 的真实实现由这里注入。** 这是「改写层」与「运行层」解耦的连接点。

**③ 转换流水线 `transform_ast`**：这是整个 AutoGraph 的「装配车间」，决定了各 converter 的执行顺序：

[tensorflow/python/autograph/impl/api.py:235-257](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/impl/api.py#L235-L257)

读这段顺序非常关键，它解释了「为什么 converter 要分层」：

| 顺序 | converter | 职责 | 受 Feature 控制？ |
|------|-----------|------|------------------|
| 1 | `functions` | 处理函数定义/嵌套函数 | 否 |
| 2 | `directives` | 处理 `ag.` 指令（如 `set_loop_options`） | 否 |
| 3 | `break_statements` | `break` → 等价 `if` | 否 |
| 4 | `asserts` | 张量相关的 `assert` → `tf.Assert` | `ASSERT_STATEMENTS` |
| 5 | `continue_statements` | `continue` → 等价 `if` | 否 |
| 6 | `return_statements` | 循环/条件里的 `return` → 状态变量 | 否 |
| 7-8 | `lists` / `slices` | 列表与切片习惯用法 | `LISTS` |
| 9 | `call_trees` | `foo(args)` → `ag__.converted_call(foo, args)` | 否 |
| 10 | **`control_flow`** | `if/while/for` → `ag__.if_stmt/while_stmt/for_stmt` | 否 |
| 11 | `conditional_expressions` | 三元表达式 `a if c else b` | 否 |
| 12 | `logical_expressions` | `and/or/not` → `ag__.and_/or_/not_` | `EQUALITY_OPERATORS` |
| 13 | `variables` | 未定义符号处理 | 否 |

注意两个工程细节：

- `break`/`continue`/`return` 的改写在 `control_flow` **之前**：因为把 `while` 改写成函数调用后，`break` 在闭包里就没有「跳出」语义了，必须先把它们消解掉。
- 部分 converter 受 `Feature` 开关控制（见 4.3.1），未启用就跳过——这就是 `tf.autograph.experimental.Feature` 的作用。

每个 converter 形如 `node = control_flow.transform(node, ctx)`，**前一个的输出是后一个的输入**，串成一条流水线。每个 `transform` 内部都会先跑静态分析、再用对应 transformer 遍历 AST。

#### 4.2.4 代码实践

**实践目标**：亲手调用 `tf.autograph.to_code`，确认 AutoGraph 输出的确实是「普通 Python + `ag__` 调用」，而非 `tf.cond`，从而验证「改写层不发 TF op」。

**操作步骤**（待本地验证；`to_code` 输出是确定性的）：

```python
# 示例代码
import tensorflow as tf

def f(x):
  if x > 0:
    y = x * x
  else:
    y = -x
  return y

print(tf.autograph.to_code(f))
```

**预期结果**：打印出的源码里能看到 `def tf__f(x):`，并且 `if` 已被替换成形如

```python
def tf__f(x):
  def if_body():
    nonlocal y
    y = x * x
  def else_body():
    nonlocal y
    y = -x
  y = ag__.Undefined('y')          # 先声明未定义
  y = ag__.if_stmt(x > 0, if_body, else_body, get_state, set_state, ('y',), 1)
  return y
```

（`get_state`/`set_state` 是自动生成的存取状态的闭包，实际变量名可能略有差异。）

**需要观察的现象**：整段代码里**没有任何 `tf.cond`**，只有 `ag__.if_stmt`。这正是「converter 不发 TF op」的直接证据——`tf.cond` 要等到追踪期 `ag__.if_stmt` 被执行时，由 operator 在 `x > 0` 确实是张量时才发出。

### 4.3 converter 基类与改写层：`core/converter.py` + `converters/`

#### 4.3.1 概念说明

`core/converter.py` 提供：

- **`Base`**：所有 converter 的基类，本身是 `gast.NodeTransformer` 的特化（gast 是兼容多版本 Python AST 的封装）。它给每个 converter 挂上一个 `ctx`（上下文）。
- **`ConversionOptions`**：不可变的转换开关集合（`recursive`、`user_requested`、`internal_convert_user_code`、`optional_features`）。
- **`Feature`** 枚举：可选功能开关，如 `ASSERT_STATEMENTS`、`LISTS`、`EQUALITY_OPERATORS`、`NAME_SCOPES` 等，对应 4.2.3 表格里那些受控的 converter。

[tensorflow/python/autograph/core/converter.py:78-127](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/core/converter.py#L78-L127) —— `Feature` 枚举，被 `@tf_export('autograph.experimental.Feature')` 导出，用户可用 `@tf.function(experimental_autograph_options=...)` 选择性关闭某些转换。

[tensorflow/python/autograph/core/converter.py:133-188](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/core/converter.py#L133-L188) —— `ConversionOptions`，注意 `uses(feature)` 方法的判定逻辑：`Feature.ALL` 在集合里时视为「全开」，这正是默认行为（`STANDARD_OPTIONS` 在 L224 设 `optional_features=None`，构造时被规整为空集，配合 `ALL` 语义等于全开）。

#### 4.3.2 核心流程

每个 converter 的 `transform(node, ctx)` 模板都遵循同一个套路（以 control_flow 为例）：

[tensorflow/python/autograph/converters/control_flow.py:404-413](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/converters/control_flow.py#L404-L413)

```python
def transform(node, ctx):
  graphs = cfg.build(node)                       # 建控制流图
  node = qual_names.resolve(node)                # 解析限定名（self.x 等）
  node = activity.resolve(node, ctx, None)       # 活跃变量分析：谁被读/写
  node = reaching_definitions.resolve(...)       # 到达定义分析
  node = reaching_fndefs.resolve(...)            # 函数定义到达分析
  node = liveness.resolve(node, ctx, graphs)     # 活跃性分析
  node = ControlFlowTransformer(ctx).visit(node) # 真正遍历并改写
  return node
```

前面五步都是 `pyct/static_analysis` 提供的**静态分析**，给每个 AST 节点打上标注（anno）：哪些变量在这个块里被修改、哪些在入口/出口存活。最后一步 `ControlFlowTransformer` 就**消费这些标注**来决定要为哪些变量建 `get_state`/`set_state`。

**为什么需要静态分析？** 因为把 `if` 改写成函数调用后，分支体变成了一个**闭包**，分支里对变量的修改默认「逃不出闭包」。AutoGraph 的办法是：分析出「哪些变量在分支里被改、且在外面还要用」，把它们的状态用 `get_state`/`set_state` 显式地「搬进搬出」。这等价于人工手写循环变量列表。

`Base` 类还有一个约束值得注意——**converter 不可复用**：

[tensorflow/python/autograph/core/converter.py:306-316](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/core/converter.py#L306-L316) —— `visit` 第一次调用后置 `_used = True`，再次调用抛 `ValueError('converter objects cannot be reused')`。这是为了避免 converter 内部的可变状态在重复使用时串味。

#### 4.3.3 源码精读：`visit_If` 如何把 `if` 改写成 `ag__.if_stmt`

`ControlFlowTransformer` 为每种控制流实现一个 `visit_X`。看最有代表性的 `visit_If`：

[tensorflow/python/autograph/converters/control_flow.py:201-256](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/converters/control_flow.py#L201-L256)

逐步解读这段改写逻辑：

1. **`generic_visit(node)`**：先递归转换 body 内部（嵌套的控制流会被先改写）。
2. **`_get_block_vars(...)`**（L206）：调用前面静态分析的成果，算出三类变量：
   - `cond_vars`：在两个分支中被修改、且分支外仍要用到的变量（需要走状态管道）。
   - `undefined`：在分支里被改、但进入分支前可能未定义的变量（要先插 `ag__.Undefined(...)` 占位）。
   - `nouts`：状态里的「输出」个数（只输出分支外仍被使用的变量）。
3. **生成 `get_state`/`set_state` 闭包**（L214-217，由 `_create_state_functions` 产出 L67-103）：这两个函数负责把 `cond_vars` 的当前值打包成元组取出、或把元组解包写回，让闭包里的修改能「外化」。
4. **模板替换**（L223-254）：把 `if test: body else: orelse` 替换成下面这段（模板即 L223-240）：

```python
def if_body():            # 原来的 then 分支，包成函数
  <nonlocal 声明>
  <body>
def else_body():          # 原来的 else 分支，包成函数
  <nonlocal 声明>
  <orelse>
<未定义变量的占位赋值>
ag__.if_stmt(
    test,                 # 条件表达式（注意：尚未求值，原样保留）
    if_body, else_body,   # 两个分支作为可调用对象
    get_state, set_state, # 状态存取
    ('v1', 'v2', ...),    # 状态变量名字符串
    nouts)                # 输出个数
```

注意三点精妙之处：

- **条件 `test` 是原样保留的表达式**，不是立即求值。converter 不做求值——求值与派发是运行期 operator 的事。
- **两个分支被包成无参函数 `if_body`/`else_body`**。这样 `tf.cond` 可以把两个函数都登记进图、运行时再二选一；而 Python 模式下 operator 也能用 `body() if cond else orelse()` 照常执行。
- **`nonlocal` 声明**让闭包内对 `cond_vars` 的写能反映到外层作用域——这对 Python 路径是必须的，对 TF 路径则由 `get_state`/`set_state` 接管。

`visit_While`（L258-306）和 `visit_For`（L308-394）结构几乎相同，区别在于：`while` 把条件也包成 `test_name()` 函数（因为 `tf.while_loop` 需要可重复调用的 cond）；`for` 额外处理迭代对象（`iter`）和 `extra_test`（额外终止条件），并在 `opts` 里塞进 `iterate_names`。

`if/while/for` 三者最终都收敛到一次 `ag__.*_stmt` 调用——这就是 converter 层的全部产出。

> 小结：converter 层**不知道也不关心** TF 的存在。它只做「把块语句改成函数调用 + 把状态外化」这种纯 Python 的等价变换。正因为如此，它可以被 `to_code` 打印、可以被 `inspect` 检查、也可以在 Eager 下照常运行（走 operator 的 Python 分支）。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码，验证 converter 的「不可复用」约束与「状态外化」机制。

**操作步骤**：

1. 打开 `tensorflow/python/autograph/converters/control_flow.py`，定位 `visit_If`（L201）与 `_create_state_functions`（L67）。
2. 对照 4.2.4 中 `to_code` 打印出的生成代码，找到其中 `def get_state`/`def set_state` 与 `ag__.if_stmt` 的对应关系。
3. 思考：如果 `if` 的两个分支都给同一个变量 `y` 赋了不同 dtype 的值（如一个 `int32`、一个 `float32`），改写后会发生什么？

**需要观察的现象**：生成代码里的 `get_state()` 返回的元组，其元素正是 `_get_block_vars` 算出的 `cond_vars`；`set_state` 用 `nonlocal` 把值写回。运行时若两个分支 dtype 不一致，会在 operator 的 `_verify_tf_cond_vars` 处报错（见 4.4.3）。

**预期结果**（待本地验证）：你能口头复述「`if` 被改写成『两个闭包 + 一次 ag__.if_stmt 调用』」这条链路，并指出 `nonlocal` 与 `get_state`/`set_state` 各自为何而存在。

### 4.4 operator 运行时与类型派发：`operators/control_flow.py`

#### 4.4.1 概念说明

`operators/` 目录是这些 `ag__.*` 函数的**运行时实现**。`operators/__init__.py` 把它们汇总导出：

[tensorflow/python/autograph/operators/__init__.py:36-63](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/__init__.py#L36-L63) —— 注意这里「operator」用得很宽泛：既包括控制结构 `if_stmt`/`while_stmt`/`for_stmt`，也包括 `list_append`、`and_`/`or_`/`eq`、`len_`/`range_`/`print_` 等。它们的共同点是：**对张量输入发 TF 等价物，对 Python 输入退回原生语义**。这些函数正是被 `api.py` 的 `get_extra_locals` 注入为 `ag__.*` 的那一批。

`control_flow.py` 里的 `if_stmt`/`while_stmt`/`for_stmt` 是本模块的主角。它们的签名刻意设计成「与对应 Python AST 结构同构」（参数名 `test`/`body`/`orelse` 对齐 AST 字段），方便 converter 用模板生成调用。

#### 4.4.2 核心流程

operator 的核心动作是**类型派发（dispatch）**——根据运行期值的类型决定发 TF op 还是走 Python。三个语句的派发入口都极简：

**`if_stmt`**：

[tensorflow/python/autograph/operators/control_flow.py:1170-1217](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/control_flow.py#L1170-L1217)

```python
def if_stmt(cond, body, orelse, get_state, set_state, symbol_names, nouts):
  if tensors.is_dense_tensor(cond):          # 条件是张量？
    _tf_if_stmt(cond, body, orelse, ...)     #   → 发 tf.cond
  else:
    _py_if_stmt(cond, body, orelse)          #   → 原生 if
```

`_py_if_stmt` 只有一行，完全还原 Python 语义：

[tensorflow/python/autograph/operators/control_flow.py:1268-1270](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/control_flow.py#L1268-L1270) —— `return body() if cond else orelse()`。这保证「当条件不是张量（如超参数、Python 整数）时，AutoGraph 改写后的代码与原 Python 行为完全一致」——即 AutoGraph 文档强调的 **as-if 规则**：改写后的代码要么行为与 Eager 一致，要么报错。

`_tf_if_stmt` 才是真正发图 op 的地方：

[tensorflow/python/autograph/operators/control_flow.py:1220-1265](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/control_flow.py#L1220-L1265) —— 先 `_verify_tf_condition` 校验条件是 `tf.bool` 标量；然后用 `get_state()` 取初值，构造 `aug_body`/`aug_orelse`（它们各自跑一遍分支、用 `get_state` 收集出口状态、并用 `_verify_tf_cond_vars` 校验两个分支输出结构一致），最后在 L1261 调 `tf_cond.cond(cond, aug_body, aug_orelse, strict=True)` 发出条件 op。

**`while_stmt`** 的派发略复杂，因为它需要**先求值一次条件**来判断类型：

[tensorflow/python/autograph/operators/control_flow.py:707-754](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/control_flow.py#L707-L754) —— 在一个临时 `FuncGraph` 里调一次 `test()` 得到 `init_test`（L738-739），若 `is_dense_tensor(init_test)` 则进 `_tf_while_stmt`，否则按 Python 语义「已经消耗了一次判断」，故先手动跑一轮 body 再交给 `_py_while_stmt`（L750-754）。

`_tf_while_stmt` 最终落到 `tf.while_loop`：

[tensorflow/python/autograph/operators/control_flow.py:1075-1167](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/control_flow.py#L1075-L1167) —— 它把 `test`/`body` 包成 `aug_test`/`aug_body`（参数化为 loop_vars），用 `verify_tf_loop_vars` 校验每轮循环后变量的 dtype/结构一致（TF 循环要求循环变量形状不变或显式声明 shape invariant），最后在 L1153 调 `while_loop.while_loop(aug_test, aug_body, aug_init_vars, ...)`。

**`for_stmt`** 的派发更丰富，因为「可迭代对象」种类多：

[tensorflow/python/autograph/operators/control_flow.py:392-449](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/control_flow.py#L392-L449) —— 先查一个类型注册表 `for_loop_registry`（自定义类型可注册自己的 for 实现），未命中则按迭代对象类型选择：

- `range` 张量 → `_tf_range_for_stmt`（把 `tf.range` 展开成 `tf.while_loop`，见 L556）；
- 已知长度的张量 → `_known_len_tf_for_stmt`（用 `TensorArray` 持有元素，再转 while，见 L509）；
- `tf.data` 迭代器 → `_tf_iterator_for_stmt`；
- 普通 Python 可迭代对象 → `_py_for_stmt`（L452，就是原生 for 循环，但带一个 `_PythonLoopChecker` 检查「是否在不小心地展开一个超大循环」，见 L757-826——若 Python 循环里建了大量 op 会警告建议改用 TF 循环）。

#### 4.4.3 概念补充：为什么需要 `get_state`/`set_state`

这是 AutoGraph 最容易让人困惑的设计，值得单独点明。看 `operators/control_flow.py` 顶部这段注释：

[tensorflow/python/autograph/operators/control_flow.py:15-55](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/autograph/operators/control_flow.py#L15-L55)

注释举的例子是 `while cond: self.x += i`。把循环体包成闭包后：

- **Python 循环**：闭包里 `self.x += i` 靠 `nonlocal`/属性赋值天然生效，没问题。
- **TF 循环**：`tf.while_loop` 的循环体是一个**纯函数**，它**看不到也改不了**外层的 `self.x`。`self.x` 在循环里会一直是「错误的旧值」。

解决办法是 `get_state`/`set_state`：进入循环前用 `get_state()` 把 `self.x` 等被修改的状态**打包成循环变量**传进去，循环体里先把状态赋回 `self.x`、再执行原逻辑、最后把新值写回循环变量返回。这样 TF 循环里 `self.x` 每轮都拿到正确值。**converter 静态分析负责找出哪些变量需要这样搬运，operator 运行时负责实际搬运。**

这解释了 4.3.3 里 `visit_If`/`visit_While` 为什么一定要生成那两个看似啰嗦的 `get_state`/`set_state` 闭包——它们是让 Python 风格的「闭包副作用」能在 TF 的「纯函数 + 显式状态」模型下工作的桥。

#### 4.4.4 代码实践

**实践目标**：用一个同时含 `if` 和 `while` 的函数，跑通「转换 → 追踪 → 真正发图 op」全链路，并确认图里出现了 `cond`/`while` 节点。

**操作步骤**（待本地验证）：

```python
# 示例代码
import tensorflow as tf

@tf.function
def f(x):              # x 是张量
  if x > 0:            # if：张量条件
    y = x
  else:
    y = -x
  i = tf.constant(0)
  s = tf.constant(0.0)
  while i < 5:         # while：张量条件
    s += tf.cast(y, tf.float32)
    i += 1
  return s

print(f(tf.constant(3)).numpy())     # 预期 15.0
# 看生成的代码：
print(tf.autograph.to_code(f.python_function))
# 看追踪出的图节点：
g = f.get_concrete_function(tf.constant(3)).graph
print([n.type for n in g.get_operations() if n.type in ('Cond','StatelessIf','While','StatelessWhile')])
```

**需要观察的现象**：

1. `to_code(f.python_function)` 输出里应同时出现 `ag__.if_stmt(...)` 和 `ag__.while_stmt(...)`。
2. 追踪出的图里应出现 `StatelessIf`/`Cond`（来自 `tf.cond`）和 `While`/`StatelessWhile`（来自 `tf.while_loop`）节点——证明 operator 在追踪期**真的发出了控制流 op**。
3. 若把 `if x > 0` 的 `x` 换成普通 Python 整数（`f` 不再用 `@tf.function`、直接 Eager 调），则 `to_code` 输出不变，但运行时 `if_stmt` 走 `_py_if_stmt`、不发任何图 op——这就是「同一份生成代码、运行期按类型二选一」。

**预期结果**：`f(tf.constant(3))` 输出 `15.0`（`y=3`，累加 5 次得 15）。

**如果无法运行**：明确标注「待本地验证」，但上面三点现象可由本讲引用的源码逻辑推断得出。

#### 4.4.5 小练习与答案

**练习 1**：`if_stmt` 为什么不直接判断 `isinstance(cond, tf.Tensor)`，而用 `tensors.is_dense_tensor(cond)`？

> 参考答案：因为「能直接做条件」的张量不止 `tf.Tensor` 一种，还可能包括被视作稠密张量的其他类型；`is_dense_tensor` 是统一的判定接口。此外，稀疏张量（`SparseTensor`）不能做 `tf.cond` 条件，`if_stmt` 的文档注释（L1213）也注明了「tf.cond doesn't support SparseTensor」。

**练习 2**：若把 4.4.4 的 `while i < 5` 改成 `while i < x`（`x` 也是张量），追踪会发生什么？为什么 TF 要求循环变量每轮结构/dtype 一致？

> 参考答案：仍然会发 `tf.while_loop`，但循环变量 `i`、`s` 必须每轮 dtype 与形状不变（由 `verify_tf_loop_vars` 校验，见 L266-321）。原因是 TF 的 `tf.while_loop` 在图层面要求循环变量的形状/类型在所有迭代中固定，这样图执行器才能预先为它们分配缓冲区；若形状会变，必须用 `tf.autograph.experimental.set_loop_options` 显式声明 shape invariants。

**练习 3**：converter 的 `visit_If`（4.3.3）为什么要在生成代码里插入 `ag__.Undefined('y')`？

> 参考答案：当变量 `y` 只在 `if` 的某个分支里被赋值、且在 `if` 之后被使用时，静态分析会把它列为「可能未定义」。为了在「未走该分支」时给 `y` 一个明确的占位值（而非 Python 的 `NameError`），先插一个 `ag__.Undefined('y')`，operator 后续可据此检测并给出友好的「某分支未初始化该变量」错误（见 `_verify_tf_cond_branch_vars`，L354-364）。

## 5. 综合实践

把本讲三层（入口流水线、converter 改写、operator 派发）串起来，完成下面这个「追踪一个完整函数」的任务。

**任务**：写一个用 `tf.function` 装饰、同时包含 `if`/`for`/`return`（在循环里提前返回）的函数，例如「计算张量 `x` 的前 `n` 个元素的符号和，遇到 0 即提前返回 0」：

```python
@tf.function
def signed_partial_sum(x, n):
  acc = tf.constant(0)
  for i in tf.range(n):
    v = x[i]
    if v == 0:
      return tf.constant(0)
    acc += v
  return acc
```

完成以下步骤（待本地验证）：

1. 用 `tf.autograph.to_code(signed_partial_sum.python_function)` 打印生成代码，**逐一标注**：
   - 哪一段是 `call_trees` converter 加的 `ag__.converted_call`？
   - 哪一段是 `control_flow` converter 把 `for` 改写的 `ag__.for_stmt`？
   - `return` 为什么被 `return_statements` converter 改写成了对状态变量的赋值（提示：循环里的 `return` 在闭包化后无法直接「跳出」）？
2. 用 `f.get_concrete_function(...).graph` 查看图节点，确认出现了 `tf.while_loop` 对应的节点，以及 `for` 迭代 `tf.range` 走的是 `_tf_range_for_stmt`（4.4.2）。
3. 解释为什么 `for i in tf.range(n)` 走 `_tf_range_for_stmt` 而 `for i in range(n)`（普通 Python `range`）会走 `_py_for_stmt`，并讨论后者在 `@tf.function` 里「展开循环」的代价（联系 `_PythonLoopChecker` 的警告机制，L757-826）。

**验收标准**：你能画出从 `signed_partial_sum` 的源码 → `to_code` 输出 → 最终图节点 的三段对应关系，并说清每个控制流分别被 converter 改写成什么 `ag__.*` 调用、又由 operator 发成什么 TF op。

## 6. 本讲小结

- AutoGraph 的根本任务是**把命令式的 Python 控制流等价翻译成声明式的 TF 图控制流**，让用户在 `tf.function` 里能直接写 `if/while/for`。
- 它采用**两阶段设计**：converter 在编译期做 **AST→AST 改写**（`if` → `ag__.if_stmt(...)`），operator 在追踪期根据**运行期类型派发**（张量→`tf.cond`/`tf.while_loop`，否则→原生 Python）。
- 三层分层清晰：`impl/api.py` 是入口与流水线（`PyToTF.transform_ast` 串联十几个 converter，靠 `get_extra_locals` 注入 `ag__` 命名空间）；`core/converter.py` 给出不可复用的 `Base` 基类与 `ConversionOptions`/`Feature` 开关；`operators/` 给出 `ag__.*` 的运行时实现。
- converter 改写的核心难点是**状态外化**：通过静态分析找出被修改的变量，用 `get_state`/`set_state` 把闭包副作用搬进搬出，使 Python 风格的副作用能在 TF「纯函数 + 显式状态」模型下工作。
- `tf.function` 默认开启 AutoGraph，接入点是 `autograph_util.py_func_from_autograph`（包成 `converted_call`），可通过 `tf.function(autograph=False)` 关闭。
- 改写后的代码遵循 **as-if 规则**：要么与 Eager 行为一致，要么报错——这正是 `_py_if_stmt` 等「Python 退路」存在的意义。

## 7. 下一步学习建议

- 阅读 `tensorflow/python/autograph/g3doc/reference/` 下的官方参考文档（`control_flow.md`、`generated_code.md`、`limitations.md`），它们从用户视角补充了本讲从源码视角讲的内容，尤其「limitations」会告诉你哪些 Python 写法 AutoGraph 不支持。
- 精读 `pyct/` 子包（`parser`、`cfg`、`static_analysis/`、`templates`、`transpiler`），理解 converter 依赖的静态分析与模板替换引擎是怎么实现的——这是把 AutoGraph 从「会用」推进到「能改」的关键。
- 结合 [u5-l1 自动微分与 gradients](u5-l1-autodiff-gradients.md) 思考：AutoGraph 转换出的 `tf.while_loop`/`tf.cond` 是如何参与反向自动微分的（它们各自有对应的梯度函数）。
- 下一讲 [u9-l3 Profiler 与性能分析](u9-l3-profiler-and-tracing.md) 将转向性能：当 AutoGraph 把循环发成 `tf.while_loop` 后，如何用 Profiler 观察它是否真的比 Python 展开更快。
