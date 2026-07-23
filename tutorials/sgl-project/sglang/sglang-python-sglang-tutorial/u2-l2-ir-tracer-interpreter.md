# IR、Tracer 与解释器：前端程序如何执行

> 本讲是第 2 单元（前端语言层 `lang/`）第 2 篇，承接 u2-l1。
> u2-l1 讲清了「`gen`/`select`/`image` 返回的是 `SglExpr` 表达式对象，而不是立即执行的结果」这一惰性求值根基。
> 本讲要回答下一个自然问题：**这些表达式对象，到底是怎么被组织、又是在什么时候、按什么顺序真正跑起来的？**

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清 SGLang 前端的「**先追踪成 IR，再解释执行**」两阶段模型。
- 区分 `tracer`（静态收集表达式，构建 IR 图，**不调用后端**）与 `interpreter`（按顺序调度后端，**真正发起生成**）的职责边界。
- 看懂 `ir.py` 中的 `SglFunction` / `SglExpr` / `SglGen` / `SglSelect` 数据结构，以及它们如何用 `prev_node` 串成一张图。
- 解释 `s.function.run()` 与 `s.function.trace()` 为什么走完全不同的代码路径，以及 `TracingScope` 在其中起到的「开关」作用。
- 亲手打印出一段 `@function` 的 IR 节点顺序，并在解释器里加一行日志观察真实执行顺序。

## 2. 前置知识

本讲假设你已经了解（来自 u2-l1 / u1-l4）：

- **SglExpr（表达式）**：`gen`、`select`、`image` 等前端原语返回的不是结果文本，而是一个表达式对象。表达式可以被拼接、被收集、被延迟求值。
- **后端（backend）**：前端通过 `BaseBackend` 抽象对接推理提供方（自研 `RuntimeEndpoint`、OpenAI、Anthropic 等）。后端暴露 `generate` / `generate_stream` / `select` 等方法。
- **`@sgl.function`**：把一个普通 Python 函数包成 `SglFunction`，其第一个参数必须叫 `s`（即 `ProgramState`），用 `s += ...` 往程序里追加内容。

下面用三个类比建立直觉：

| 概念 | 直觉类比 | 关键点 |
| --- | --- | --- |
| IR（中间表示） | 一份「待办清单 / 配方」 | 只记录「要做什么」，不真正去做 |
| Tracer | 抄写员，把口述的配方抄成清单 | 抄写时不下厨（不调后端） |
| Interpreter | 厨师，照清单逐条下厨 | 每遇到 `gen` 才真正发起一次推理 |

为什么要把「抄清单」和「下厨」分开？因为分开之后，同一份配方可以**离线分析**（例如找出所有请求共享的公共前缀去做前缀缓存）、**批处理**、**可视化**，而不必真的去跑模型。这正是 `tracer` 存在的核心动机。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `python/sglang/lang/` 下：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [lang/ir.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py) | **IR 定义** | `SglFunction`、`SglExpr` 及其子类（`SglGen`/`SglSelect`/`SglConstantText` 等）、`prev_node` 图结构、`print_graph_dfs` |
| [lang/tracer.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py) | **追踪器** | `trace_program`、`TracerProgramState`、`TracingScope` |
| [lang/interpreter.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py) | **解释器** | `run_program`、`StreamExecutor`、`ProgramState` |
| [lang/api.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py) | **公共 API** | `function`、`gen`、`select` 如何造出 `SglExpr` |
| [lang/choices.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py) | **select 决策** | `token_length_normalized` 等候选项评分方法 |

一个一句话总览：`api.py` 负责造表达式，`ir.py` 负责定义表达式，`tracer.py` 负责把表达式收集成图，`interpreter.py` 负责把图逐条执行掉。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 IR 中间表示：`SglExpr` 图与 `SglFunction`**——表达式是什么形状。
- **4.2 Tracer：把 `@function` 静态追踪成 IR**——第一阶段。
- **4.3 Interpreter：`StreamExecutor` 调度 backend 逐条执行**——第二阶段。
- **4.4 run 与 trace 的分流：`TracingScope` 与两阶段协作**——把两个阶段粘起来的开关。

### 4.1 IR 中间表示：SglExpr 图与 SglFunction

#### 4.1.1 概念说明

「IR」即 Intermediate Representation（中间表示）。在 SGLang 前端，IR 就是**一棵/一张由 `SglExpr` 节点组成的图**：每个 `gen`、`select`、一段常量文本、一次角色切换，都是图里的一个节点；节点之间通过 `prev_node` 指针连起来，表达「我先发生，你再发生」的顺序依赖。

`SglFunction` 则是「程序的容器」：它包住了用户写的那个 Python 函数（`self.func`），并负责决定这个函数被调用时到底是「立刻执行」（走 interpreter）还是「只追踪」（走 tracer）。注意，`SglFunction` **不持有 IR 图本身**——IR 图是在执行/追踪过程中、由 `ProgramState` / `TracerProgramState` 动态累积出来的。这一点是初学者最容易混淆的地方：`SglFunction` 只是「配方函数」，配方被抄成清单（IR）是另一步。

#### 4.1.2 核心流程

一个 `SglExpr` 节点有三件套（[lang/ir.py:L330-L334](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L330-L334)）：

- `node_id`：全局自增编号，相当于节点的身份证号。
- `prev_node`：指向前一个节点，构成顺序链。
- `pid`：所属程序（program）的 id。

表达式还重载了 `__add__` / `__radd__`（[lang/ir.py:L336-L348](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L336-L348)），所以你可以写 `s += "Q:" + sgl.gen("a")`：字符串 `"Q:"` 会被自动包成 `SglConstantText`，然后和 `SglGen` 拼成一个 `SglExprList`（一个「表达式列表」节点）。`SglExprList` 是一种容器节点，里面装着多个子表达式（[lang/ir.py:L397-L403](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L397-L403)）。

关键表达式子类速览：

| 类 | 含义 | 由哪个 API 造出 |
| --- | --- | --- |
| `SglConstantText` | 一段固定文本 | 字符串自动包装 / `s += "..."` |
| `SglGen` | 一次生成调用（含采样参数） | `sgl.gen(...)` |
| `SglSelect` | 在候选项里挑一个 | `sgl.select(...)` / `sgl.gen(choices=...)` |
| `SglRoleBegin` / `SglRoleEnd` | 对话角色（system/user/assistant）边界 | `sgl.user(...)` 等 |
| `SglImage` / `SglVideo` | 多模态输入 | `sgl.image(...)` / `sgl.video(...)` |
| `SglVariable` | 对某个 gen/select 结果的引用 | tracer 内部生成 |
| `SglFork` / `SglGetForkItem` | 分叉出多个并行分支 | `s.fork(...)` |

#### 4.1.3 源码精读

**`SglFunction` 的构造与「第一个参数必须是 `s`」的强约束**（[lang/ir.py:L142-L152](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L142-L152)）：

```python
def __init__(self, func, num_api_spec_tokens=None, bind_arguments=None):
    self.func = func
    ...
    # Parse arguments
    argspec = inspect.getfullargspec(func)
    assert argspec.args[0] == "s", 'The first argument must be "s"'
    self.arg_names = argspec.args[1:]
    self.arg_defaults = argspec.defaults if argspec.defaults is not None else []
```

这段用 `inspect` 反射地读出函数的形参名，校验第一个参数叫 `s`，并把后续参数名存进 `self.arg_names`。`arg_names` 后面会被 tracer 用来给「哑参数」取名（见 4.2）。

**`SglGen` 把一长串采样参数收拢进 `SglSamplingParams`**（[lang/ir.py:L451-L500](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L451-L500)）：注意 `SglGen` 自己**几乎不存逻辑**，它只是把调用 `sgl.gen(...)` 时传进来的 `temperature`、`top_p`、`stop` 等参数打包成一个 `SglSamplingParams` 数据对象挂在自己身上（`self.sampling_params`）。真正的「怎么采样」是在解释器执行到它、交给后端时才发生的。

**`SglSelect` 存了候选项与评分方法**（[lang/ir.py:L533-L549](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L533-L549)）：它持有 `choices`（候选项字符串列表）、`temperature` 和 `choices_method`（一个 `ChoicesSamplingMethod`）。select 的语义是「让模型对每个候选项打分，选分最高的」。默认评分法 `token_length_normalized` 的判据是取**归一化平均对数概率**最大的候选项（[lang/choices.py:L32-L50](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L32-L50)）：

\[
\hat{c} \;=\; \arg\max_{c \in \text{choices}} \;\frac{1}{|c|}\sum_{t} \log p\!\left(\text{token}_{t}^{(c)} \,\big|\, \text{context}\right)
\]

也就是「每个 token 的平均对数概率」，用它来消除「长候选天然概率低」的偏差。

> 补充：`SglSamplingParams` 用了 `@dataclasses.dataclass`（[lang/ir.py:L17-L40](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L17-L40)）。注意这是前端历史代码，仓库的新规范（见 `.claude/rules/no-dataclasses.md`）要求新代码用 `msgspec.Struct` 而非 `@dataclass`；读这段历史代码时无需改动，但**自己写新数据容器时不要再用 `@dataclass`**。

#### 4.1.4 代码实践（源码阅读型，无需 GPU）

**目标**：用 `print_graph_dfs` 或 `flatten_nodes` 可视化一段程序的 IR 节点顺序，直观感受「图」长什么样。

**步骤**：

1. 新建 `trace_demo.py`（这是你本地的示例文件，不是 sglang 源码）：

```python
import sglang as sgl

@sgl.function
def classify(s, statement):
    s += "Statement: " + statement + "\n"
    s += "Answer:" + sgl.select("answer", ["True", "False", "Unknown"])
    s += "\nBecause:" + sgl.gen("reason", max_new_tokens=32)

# 注意：trace() 只追踪、不调用任何后端，因此不需要起服务、也不需要 GPU
tracer = classify.trace(statement="The sky is blue.")

print("=== flatten_nodes 顺序 ===")
for expr in tracer.flatten_nodes():
    print(type(expr).__name__, "->", repr(expr))
```

2. 直接 `python trace_demo.py` 运行。

**需要观察的现象**：输出会按顺序列出 IR 节点，大致形如 `SglConstantText -> SglConstantText -> SglSelect -> SglConstantText -> SglGen`。

**预期结果**：你能看到「常量文本 → select → 常量文本 → gen」的顺序，这正是程序里 `s += ...` 的书写顺序。注意 `select` 和 `gen` 此刻**没有任何模型调用发生**——它们只是被「抄」进了清单。

**说明**：因为 `trace()` 在没有 `default_backend` 时会创建一个空的 `BaseBackend`（见 4.2），所以这个实践**无需启动任何推理服务**即可跑通。若运行时报错，请确认 `import sglang` 能正常导入。

#### 4.1.5 小练习与答案

**练习 1**：`SglFunction` 自身保存了 IR 图吗？为什么？

> **答案**：没有。`SglFunction` 只保存用户写的 `self.func` 以及参数名等元信息。IR 图是在追踪/执行过程中，由 `TracerProgramState.nodes` 或 `StreamExecutor` 动态累积出来的。`SglFunction` 是「配方函数」，不是「抄好的清单」。

**练习 2**：为什么 `sgl.gen(choices=[...])` 和 `sgl.select(...)` 在 IR 层面是等价的？

> **答案**：看 [lang/api.py:L102-L108](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L102-L108)：当 `gen` 传入 `choices` 时，它直接 `return SglSelect(...)`，根本不构造 `SglGen`。所以在 IR 层两者是同一个节点类型，`gen` 带 `choices` 只是 `select` 的语法糖。

### 4.2 Tracer：把 @function 静态追踪成 IR

#### 4.2.1 概念说明

Tracer（追踪器）的任务是：**运行一遍用户的 `@function` 函数体，但把其中所有 `s += ...` 收集成 IR 节点，而不真正调用后端**。你可以把它理解成「用一台不联网的假后端，把程序走一遍，只为了把步骤抄下来」。

它解决的典型问题是「**提前找出公共前缀**」：如果 1000 条请求共享同一段长 system prompt，tracer 可以一次性把这段前缀算出来，交给后端做前缀缓存（`backend.cache_prefix`），后续每条请求都能命中缓存、省掉重复 prefill。这正是 `run_program_batch` 在批处理前会先调一次 `cache_program` 的原因（[lang/interpreter.py:L106-L107](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L106-L107)）。

#### 4.2.2 核心流程

追踪流程伪代码：

```
trace_program(program, arguments, backend):
    1. 若 backend 为空，造一个空壳 BaseBackend（不联网）
    2. 给 program.arg_names 中没传值的参数，造「哑参数」SglArgument(name, None)
    3. new TracerProgramState(backend, arguments)   # 这是追踪期的 s
    4. with TracingScope(tracer):                    # 打开追踪开关
           program.func(tracer, **arguments)         # 跑函数体
    5. return tracer  # tracer.nodes 里就是 IR 节点序列
```

关键在于第 4 步：`TracerProgramState`（追踪期的 `s`）重写了 `+=`（`__iadd__` → `_execute`），所以函数体里每一句 `s += expr` 都不会去调后端，而是走到 `_execute` 的「收集」分支（[lang/tracer.py:L144-L173](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L144-L173)）。

#### 4.2.3 源码精读

**`trace_program`：哑参数 + 打开 TracingScope**（[lang/tracer.py:L54-L72](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L54-L72)）：

```python
def trace_program(program, arguments, backend):
    if backend is None:
        backend = BaseBackend()                       # 空壳后端，不联网

    dummy_arguments = {
        name: SglArgument(name, None)
        for name in program.arg_names
        if name not in arguments
    }
    arguments.update(dummy_arguments)
    arguments.update(program.bind_arguments)

    tracer = TracerProgramState(backend, arguments, only_trace_prefix=False)
    with TracingScope(tracer):
        tracer.ret_value = program.func(tracer, **arguments)
    return tracer
```

注意「哑参数」`SglArgument(name, None)`：追踪时我们并没有真实的入参取值（比如 `question` 的内容），但仍要让函数体跑得下去，于是用一个占位对象顶上。这也是为什么 `SglArgument` 重载了 `__format__` 并**禁止放进 f-string**（[lang/ir.py:L427-L431](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L427-L431)）——追踪期参数没有真实值，强行格式化会得到无意义结果，不如直接报错。

**`TracerProgramState._execute`：按类型收集，绝不调后端**（[lang/tracer.py:L144-L173](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L144-L173)）：

```python
def _execute(self, other: SglExpr):
    ...
    if isinstance(other, SglConstantText):
        self._execute_fill(other)
    elif isinstance(other, SglGen):
        self._execute_gen(other)
    elif isinstance(other, SglSelect):
        self._execute_select(other)
    elif isinstance(other, SglExprList):
        for x in other.expr_list:
            self._execute(x)
    ...
    else:
        if self.only_trace_prefix:
            raise StopTracing()
        else:
            self._append_node(other)
    return self
```

对比 4.3 的 `StreamExecutor._execute`，你会发现**两者的分发结构几乎一模一样**（都是一长串 `isinstance` 判断），但区别在分支内部：tracer 的 `_execute_gen` 只是 `_append_node(expr)` + 记一个 `SglVariable`（[lang/tracer.py:L184-L188](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L184-L188)），而 interpreter 的 `_execute_gen` 会真正调 `self.backend.generate(...)`（见 4.3）。这是「抄清单」与「下厨」最直接的对照。

**`extract_prefix_by_tracing`：只抄前缀就停**（[lang/tracer.py:L29-L51](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L29-L51)）：这是 `cache_program` 实际调用的函数。它以 `only_trace_prefix=True` 创建 tracer，一旦遇到第一个非 `SglConstantText` 的节点（即第一个真正会调用模型的地方）就抛 `StopTracing` 提前结束，然后把已经收集到的常量文本拼成「公共前缀」。这是一种聪明的短路：**前缀缓存只需要知道开头那段固定文本，后面的动态生成部分根本不用追踪**。

#### 4.2.4 代码实践（源码阅读型，无需 GPU）

**目标**：观察「前缀提取」如何遇到第一个 `gen` 就停止。

**步骤**：在 4.1.4 的 `trace_demo.py` 基础上追加：

```python
from sglang.lang.tracer import extract_prefix_by_tracing

prefix = extract_prefix_by_tracing(classify, sgl.lang.backend.base_backend.BaseBackend())
print("=== 提取到的公共前缀 ===")
print(repr(prefix))
```

**需要观察的现象**：前缀会在 `"Answer:"` 之前停下——因为 `sgl.select` 是第一个非纯文本节点。

**预期结果**：`prefix` 大致为 `"Statement: " + ... "\nAnswer:"` 之前的常量文本（不含 select/gen）。注意 `extract_prefix_by_tracing` 用哑参数，所以 `statement` 位置是占位、不展开真实文本。

**说明**：若导入路径报错，可改成 `from sglang.lang.backend.base_backend import BaseBackend`。结果是否完全如上「待本地验证」，取决于你环境里 `sglang.lang` 的导出情况。

#### 4.2.5 小练习与答案

**练习 1**：tracer 的 `_execute` 和 interpreter 的 `_execute` 长得几乎一样，为什么不抽成一个公共函数？

> **答案**：因为两者在**每个分支内部做的事完全不同**——一个只 `append_node`，另一个真的调后端。同样的「类型分发骨架」配上截然不同的「分支动作」，是典型的「结构相同、语义不同」。强行抽象反而会让两边的差异被藏起来，可读性变差。这是工程取舍，不是不能抽。

**练习 2**：`only_trace_prefix=True` 时，tracer 遇到 `SglFork` 会怎样？

> **答案**：直接抛 `StopTracing()`（[lang/tracer.py:L111-L112](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L111-L112)）。因为 fork 之后程序会分叉，不再有单一的线性前缀，继续追踪没有意义。外层 `extract_prefix_by_tracing` 把 `StopTracing` 当作正常结束捕获（[lang/tracer.py:L40-L42](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L40-L42)）。

### 4.3 Interpreter：StreamExecutor 调度 backend 逐条执行

#### 4.3.1 概念说明

Interpreter（解释器）的任务是：**真正把 IR 节点一条条执行掉**。在 SGLang 前端，这个角色由 `StreamExecutor` 扮演。它持有一个后端 `backend`，遇到 `SglGen` 就调 `backend.generate`、遇到 `SglSelect` 就调 `backend.select`，把结果写回自己的 `variables` 字典，供 `s["answer"]` 之类读取。

一个关键设计：**`StreamExecutor` 跑在一个独立的后台线程里**。用户函数体里的 `s += expr` 只是把 `expr` 塞进一个队列（`queue.Queue`），真正执行在后台线程异步进行。这样做是为了支持**流式输出**和**变量就绪事件**（`variable_event`）：当主线程读 `s["answer"]` 时，如果后台还没生成完，`get_var` 会阻塞等待事件被 set（[lang/interpreter.py:L354-L357](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L354-L357)）。

#### 4.3.2 核心流程

执行流程伪代码：

```
run_program(program, backend, args, ...):
    stream_executor = StreamExecutor(backend, ...)   # 内部启动后台 worker 线程
    state = ProgramState(stream_executor)            # 这就是执行期的 s
    run_internal(state, program, ...):               # 在线程里跑函数体
        program.func(state, ...)                     # 函数体里 s += expr → 入队

# 后台 worker 线程：
_thread_worker_func:
    while True:
        expr = queue.get()
        if expr is None: break                       # 哨兵，结束
        _execute(expr)                               # 真正执行
        queue.task_done()

# 类型分发：
_execute(expr):
    if SglConstantText: _execute_fill   → text_ += value
    if SglGen:          _execute_gen    → backend.generate(...) → 写 variables
    if SglSelect:       _execute_select → backend.select(...)  → 写 variables
    if SglExprList:     逐个 _execute
    ...
```

#### 4.3.3 源码精读

**`run_program`：组装 StreamExecutor 与 ProgramState**（[lang/interpreter.py:L57-L90](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L57-L90)）：

```python
def run_program(program, backend, func_args, func_kwargs, default_sampling_para, stream, ...):
    ...
    stream_executor = StreamExecutor(backend, func_kwargs, default_sampling_para, ...)
    state = ProgramState(stream_executor)
    if stream:
        t = threading.Thread(target=run_internal, args=(...))
        t.start()
        return state
    else:
        run_internal(state, program, func_args, func_kwargs, sync)
        return state
```

注意 `run_internal` 真正调用用户函数体：`state.ret_value = program.func(state, *func_args, **func_kwargs)`（[lang/interpreter.py:L42-L48](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L42-L48)）。流式模式下它另起一个线程，非流式模式下在当前线程同步跑。

**`ProgramState.__iadd__`：`s += expr` 的真正入口**（[lang/interpreter.py:L1023-L1027](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L1023-L1027)）：

```python
def __iadd__(self, other):
    if other is None:
        raise ValueError("Tried to append None to state.")
    self.stream_executor.submit(other)
    return self
```

`submit` 把 expr 丢进队列（`use_thread=True` 时）或直接执行（[lang/interpreter.py:L342-L348](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L342-L348)）。后台线程在 `_thread_worker_func` 里循环 `queue.get()` 再 `_execute`（[lang/interpreter.py:L422-L437](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L422-L437)）。`end()` 会往队列塞一个 `None` 哨兵通知线程退出（[lang/interpreter.py:L416-L420](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L416-L420)）。

**`_execute_gen`：真正发起生成、写回变量、触发事件**（[lang/interpreter.py:L593-L625](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L593-L625)）：

```python
def _execute_gen(self, expr: SglGen):
    sampling_params = self._resolve_sampling_params(expr.sampling_params)
    name = expr.name
    if not self.stream:
        if self.num_api_spec_tokens is None:
            comp, meta_info = self.backend.generate(self, sampling_params=sampling_params)
        ...
        self.text_ += comp
        self.variables[name] = comp
        self.meta_info[name] = meta_info
        self.variable_event[name].set()        # 通知等待者：结果就绪
```

这正是「下厨」的核心：调 `self.backend.generate(...)` 拿到生成文本，拼到 `text_`，存进 `variables[name]`，最后 `variable_event[name].set()` 唤醒可能在 `get_var` 里阻塞的主线程。`_resolve_sampling_params`（[lang/interpreter.py:L799-L846](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L799-L846)）负责把「`run()` 传的默认采样参数」与「`gen()` 里覆盖的采样参数」合并——默认值打底，`gen` 里给定的非 None 值覆盖。

**`_execute_select`：调后端做候选项打分**（[lang/interpreter.py:L647-L658](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L647-L658)）：把 `choices`、`temperature`、`choices_method` 一股脑交给 `backend.select`，拿到 `ChoicesDecision`，把 `.decision` 写进 `variables`。

> 对比记忆：tracer 的 `_execute_gen` 是 `_append_node + SglVariable`（不联网）；interpreter 的 `_execute_gen` 是 `backend.generate + 写 variables + set event`（联网）。**同样的方法名，截然相反的语义**——这就是「抄清单 vs 下厨」。

#### 4.3.4 代码实践（源码阅读 + 本地改动型）

> 说明：本实践要求你在**自己的本地工作副本**里临时加一行日志用于学习，这属于阅读源码的常规手段；本讲义本身不修改任何 sglang 源码，实践后你应自行还原该改动。

**目标**：观察一个带 `gen` 和 `select` 的 `@function` 在**解释执行**时，每条 `SglExpr` 的真实求值顺序，验证它和追踪出的 IR 顺序一致。

**步骤**：

1. 在 [lang/interpreter.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py) 的 `StreamExecutor._execute` 开头（约 L461 处）加一行日志：

```python
def _execute(self, other):
    if isinstance(other, str):
        other = SglConstantText(other)
    print(f"[execute] {type(other).__name__}")   # <-- 你加的这一行
    assert isinstance(other, SglExpr), f"{other}"
    ...
```

2. 准备一个跑得起的后端（例如先用 `sglang.Engine` 或起一个本地 `sglang serve`，并 `sgl.set_default_backend(...)`；具体启动方式见 u1-l2/u1-l4），然后运行：

```python
import sglang as sgl
# 此处省略：把 default_backend 指向你本地跑起来的 sglang 服务

@sgl.function
def classify(s, statement):
    s += "Statement: " + statement + "\n"
    s += "Answer:" + sgl.select("answer", ["True", "False", "Unknown"])
    s += "\nBecause:" + sgl.gen("reason", max_new_tokens=32)

ret = classify.run(statement="The sky is blue.")
print("answer:", ret["answer"])
```

**需要观察的现象**：控制台会按顺序打印 `[execute] SglConstantText` / `[execute] SglConstantText` / `[execute] SglSelect` / `[execute] SglConstantText` / `[execute] SglGen`（`SglExprList` 会被展开成内部子节点）。

**预期结果**：执行顺序与 4.1.4 里 `flatten_nodes` 打印的 IR 顺序**一致**。这验证了「追踪抄出来的清单顺序」就是「解释器实际下厨的顺序」。

**若无法本地起服务**：则把这一步标注为「待本地验证」，仅完成 4.1.4（纯追踪、无需后端）的部分即可体会顺序关系。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `StreamExecutor` 要用一个后台线程 + 队列，而不是在主线程里同步执行所有 `s += expr`？

> **答案**：为了支持**流式输出**和**变量就绪等待**。后台线程边生成边往 `variables` 写、边触发 `variable_event`，主线程可以通过 `text_iter` / `get_var` 边等边取，实现「生成一个 token 就能读到一个 token」的流式体验。同步执行则必须等整个函数跑完才能读到任何结果。

**练习 2**：`s += None` 会发生什么？为什么代码要显式拦截？

> **答案**：会抛 `ValueError("Tried to append None to state.")`（[lang/interpreter.py:L1023-L1026](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L1023-L1026)）。因为 `None` 不是 `SglExpr`，若不拦截会一路传到 `_execute` 的 `assert isinstance(other, SglExpr)` 才报错，信息更难懂。提前拦截能给用户更清晰的提示——通常是某个 `gen` 忘了写、或表达式拼接出了 `None`。

### 4.4 run 与 trace 的分流：TracingScope 与两阶段协作

#### 4.4.1 概念说明

现在两个阶段都有了，但还差一个「开关」：当你写 `my_func(...)` 时，它怎么知道该走解释器（`run`）还是走追踪器（`trace`）？这个开关就是 `TracingScope`。

`TracingScope` 是一个**线程内的全局上下文**（用类变量 `cur_scope` 模拟），通过 `with TracingScope(tracer):` 进入、退出时恢复。它的核心作用是处理**嵌套的 `@function` 调用**：当函数 A 在追踪过程中又调用了函数 B（`B()`），B 的 `__call__` 会检查「当前是否处在某个追踪作用域里」，如果是，B 也应当被追踪而不是被真正执行。

#### 4.4.2 核心流程

分流决策在 `SglFunction.__call__` 里（[lang/ir.py:L316-L324](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L316-L324)）：

```
__call__(*args, **kwargs):
    if 当前没有 TracingScope:
        → self.run(...)      # 解释执行（下厨）
    else:
        → self.trace(...)    # 继续追踪（抄清单）
```

而 `run` 和 `trace` 分别落到 `run_program`（interpreter）与 `trace_program`（tracer），即 4.2、4.3 讲的两个入口。

两阶段协作的典型场景（批处理）：

```
run_program_batch(...):
    if 启用前缀预缓存 and 批次大小 > 1:
        cache_program(program, backend)          # 阶段一：trace 出前缀并缓存
    # 阶段二：对每条入参 run_program 真正执行（可并发）
```

#### 4.4.3 源码精读

**`SglFunction.__call__`：分流枢纽**（[lang/ir.py:L316-L324](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L316-L324)）：

```python
def __call__(self, *args, **kwargs):
    from sglang.lang.tracer import TracingScope

    tracing_scope = TracingScope.get_current_scope()
    if tracing_scope is None:
        return self.run(*args, **kwargs)
    else:
        kwargs["backend"] = tracing_scope.tracer_state.backend
        return self.trace(*args, **kwargs)
```

这就是整个分流的全部逻辑——非常简洁。`run` 与 `trace` 各自的实现见 [lang/ir.py:L160-L221](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L160-L221) 和 [lang/ir.py:L304-L308](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L304-L308)。注意 `run` 里把一长串采样参数打包成 `default_sampling_para`，再交给 `run_program`（[lang/ir.py:L213-L221](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L213-L221)）。

**`TracingScope`：进出作用域**（[lang/tracer.py:L257-L279](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L257-L279)）：

```python
class TracingScope:
    cur_scope = None

    def __init__(self, tracer_state: TracerProgramState):
        self.tracer_state = tracer_state
        self.last_scope = TracingScope.cur_scope

    def __enter__(self):
        TracingScope.cur_scope = self
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        TracingScope.cur_scope = self.last_scope
```

它用「保存旧值 → 设新值 → 退出时还原旧值」的方式实现可嵌套的作用域栈（`last_scope` 链）。`get_current_scope()` 返回当前栈顶（[lang/tracer.py:L271-L273](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/tracer.py#L271-L273)）。因为 `cur_scope` 是类变量，所以它本质上是**进程级**的，SGLang 靠「追踪只在单个线程内同步进行」来保证安全。

**两阶段在批处理里的协作**（[lang/interpreter.py:L106-L107](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L106-L107) 与 [lang/interpreter.py:L242-L247](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L242-L247)）：`run_program_batch` 在执行批次前先调 `cache_program`，后者用 `extract_prefix_by_tracing`（阶段一，trace）算出公共前缀并 `backend.cache_prefix(prefix)`，然后才并发地 `run_program` 每条入参（阶段二，interpret）。这就是「先追踪、后执行」最直接的现实收益。

#### 4.4.4 代码实践（源码阅读型，无需 GPU）

**目标**：用一段「A 调用 B」的嵌套程序，验证 `TracingScope` 让被嵌套调用的 B 也走 trace 而非 run。

**步骤**：编写如下示例（本地示例文件）：

```python
import sglang as sgl

@sgl.function
def inner(s, x):
    s += "inner sees " + x + "\n"
    s += "result:" + sgl.gen("r")

@sgl.function
def outer(s, x):
    s += "outer start\n"
    inner(x)              # 在 outer 里调用另一个 @function
    s += "outer end\n"

# 只追踪 outer；观察 inner 是否也被追踪（flatten_nodes 里应能看到 inner 的节点）
tracer = outer.trace(x="hello")
print("=== outer 追踪到的全部节点 ===")
for expr in tracer.flatten_nodes():
    print(type(expr).__name__, "->", repr(expr)[:60])
```

**需要观察的现象**：追踪 `outer` 时，`inner(x)` 这一句不会触发解释执行（不会报「没有后端」），而是被并入 outer 的 IR。如果你看到 `inner` 内部的 `SglGen` 节点出现在 `tracer.flatten_nodes()` 里，就说明 `TracingScope` 让 inner 走了 `trace` 分支。

**预期结果**：节点序列里包含 outer 与 inner 两者的常量文本和 `SglGen`，且全程没有发起任何模型调用。**实际是否完全如此「待本地验证**」，取决于 `child_states` 与节点归属的细节。

#### 4.4.5 小练习与答案

**练习 1**：如果在 `trace` 的函数体里直接写 `my_func.run(...)`（而不是 `my_func(...)`），会发生什么？

> **答案**：会**绕过** `TracingScope` 分流，强制走解释执行（`run` → `run_program`）。也就是说，即便外层在追踪，被显式 `.run()` 调用的函数也会真的去调后端。这通常不是你想要的——嵌套函数应该用 `my_func(...)` 让分流自动生效，而不是硬调 `.run()`。

**练习 2**：`TracingScope.cur_scope` 是类变量（进程级），这会带来什么隐患？SGLang 如何规避？

> **答案**：隐患是「多线程同时追踪会互相串扰作用域」。SGLang 的规避方式是：追踪（`trace_program` / `extract_prefix_by_tracing`）是**同步、短时**的操作，且不在用户并发请求的热路径上并发执行（批处理里只在批次开始前调一次）。所以在实际使用中不会有多线程同时进入 `TracingScope` 的情况。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**对比追踪与执行**」的小任务：

1. **定义程序**：参考 [test/test_programs.py:L66-L97](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/test/test_programs.py#L66-L97) 的 `test_select`，写一个带 `select` 和 `gen` 的 `@function`：

```python
import sglang as sgl

@sgl.function
def true_or_false(s, statement):
    s += "Determine whether the statement is True, False, or Unknown.\n"
    s += "Statement: " + statement + "\n"
    s += "Answer:" + sgl.select("answer", ["True", "False", "Unknown"])
    s += "\nReason:" + sgl.gen("reason", max_new_tokens=32, stop="\n")
```

2. **阶段一（追踪）**：用 `tracer = true_or_false.trace(statement="...")`，打印 `tracer.flatten_nodes()`，记录 IR 节点顺序与类型。再用 `extract_prefix_by_tracing` 看它提取到的公共前缀在哪里截断，**解释为什么截断点在那**（提示：第一个非 `SglConstantText` 节点）。

3. **阶段二（解释）**：在你本地工作副本的 `StreamExecutor._execute` 开头加一行 `print(f"[execute] {type(other).__name__}")`，把 `default_backend` 指向本地 sglang 服务，运行 `true_or_false.run(statement="...")`，记录执行顺序。

4. **对照**：把阶段一的 IR 顺序与阶段二的执行顺序并排比较，写一段话说明：
   - 两者顺序是否一致？为什么应当一致？
   - 哪些节点在追踪期「什么都没做」，在执行期却触发了真正的模型调用？
   - 如果把 `select` 换成 `gen(choices=[...])`（见 [lang/api.py:L102-L108](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L102-L108)），IR 和执行结果会不会变？

5. **收尾**：记得删掉你加的那行 `print`，恢复源码原貌。

> 若本地无法起服务，第 3、4 步标注为「待本地验证」，但第 1、2 步（纯追踪）应当能独立完成并得出结论。

## 6. 本讲小结

- SGLang 前端是「**先追踪成 IR，再解释执行**」的两阶段模型：`tracer` 抄清单（建图、不联网），`interpreter` 下厨（调后端、真生成）。
- IR 的基本单位是 `SglExpr` 节点，靠 `prev_node` 串成顺序链；`gen`/`select`/常量文本/角色边界等都是不同子类。`SglFunction` 是「配方容器」，本身不持有 IR 图。
- `TracerProgramState._execute` 与 `StreamExecutor._execute` 的**类型分发骨架几乎相同**，但分支内部一个 `append_node`、一个调后端——这是「抄清单 vs 下厨」最直接的代码对照。
- `StreamExecutor` 跑在后台线程 + 队列上，用 `variable_event` 支持流式输出与变量就绪等待；`s += expr` 只是入队。
- `SglFunction.__call__` 是分流枢纽：根据 `TracingScope.get_current_scope()` 决定走 `run`（解释）还是 `trace`（追踪），从而正确处理嵌套 `@function` 调用。
- 两阶段协作的现实收益是**前缀预缓存**：`run_program_batch` 先 `trace` 出公共前缀并缓存，再并发 `run` 每条入参，省掉重复 prefill。

## 7. 下一步学习建议

- **本单元收尾**：本讲讲完了前端「程序如何执行」。下一讲 **u2-l3（后端抽象：RuntimeEndpoint 与第三方后端）** 会下钻到 interpreter 反复调用的 `backend.generate` / `backend.select` 背后，看 `BaseBackend` 抽象如何对接自研运行时与 OpenAI/Anthropic 等第三方。
- **横向对照**：读完本讲后，建议回看 [lang/interpreter.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py) 里 `fork` / `ProgramStateGroup.join`（[L1045-L1098](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L1045-L1098)）相关代码，那是「分叉并行执行」的进阶机制，可作为本讲的延伸阅读。
- **回到运行时主线**：前端的 `backend.generate` 最终会把请求汇聚到运行时层的 `GenerateReqInput`（见 u1-l4）。学完本单元后，第 3 单元将正式进入 `srt/` 服务端架构，那时你会看到前端这层「追踪—解释」机制产出的请求，是如何被运行时调度器接收的。
