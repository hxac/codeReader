# 前端 DSL 基础：function、gen、select、image

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `@sgl.function`（即 `@function` 装饰器）编写一段**可复用**的生成程序，并理解它的第一个参数 `s` 是什么。
- 写出 `gen` / `gen_int` / `gen_string` 三种生成原语，知道它们返回的不是一个字符串，而是一个**表达式对象**。
- 用 `select`（或带 `choices` 的 `gen`）在若干候选项里挑出答案，并能说清「为什么是挑而不是生成」。
- 把 `image` / `video` 这样的多模态输入原语拼进程序，理解它们同样是表达式。
- 牢记贯穿本讲的一句话：**`gen`、`select`、`image` 返回的都是 `SglExpr` 表达式，而不是立即执行的结果**——这是 SGLang 前端「惰性求值」的核心。

本讲是第 2 单元（前端语言层 `lang/`）的第一篇。在 u1-l4 里你已经知道请求可以通过 HTTP 或 `sglang.Engine` 进入运行时；本讲带你换一个视角：当你需要**用代码精确描述一段「先拼 prompt、再生成、再在候选项里二选一」的复杂流程**时，SGLang 给你提供了一套专门的领域特定语言（DSL），而 `function / gen / select / image` 就是这套语言的最基本词汇。

## 2. 前置知识

在阅读源码前，先建立三个直觉。

**直觉一：什么是「前端 DSL」？**
DSL（Domain-Specific Language，领域特定语言）是为一类特定任务设计的「小语言」。SGLang 的前端 DSL 并不是一套独立的语法，而是**寄居在 Python 里的一组函数与约定**：你照常写 Python 函数，但函数体里用 `gen`、`select` 这些「原语」来描述生成流程。可以这样理解——普通 Python 函数「立刻执行每一条语句」，而 SGLang 的 `@function` 函数「先把流程记录成一张表达式图，再交给后端去执行」。

**直觉二：什么是「表达式（expression）」和「惰性求值（lazy evaluation）」？**
在普通 Python 里，`x = 1 + 2` 会立刻算出 `3`。而在 SGLang 前端里，`sgl.gen("answer")` **不会**立刻去调模型生成，它只是「造出一个表示『这里要生成一段文本，结果存到 answer』的对象」。只有当这个对象被加进程序的执行流（`s += ...`）并真正运行时，才会触发模型调用。这种「先造对象、后执行」的模式就叫惰性求值。它的好处是：整段程序可以被分析、批处理、缓存、并行化（这些是第 2 单元后续讲义的主题）。

**直觉三：前端和运行时的关系（承接 u1-l4）。**
前端 DSL 写出的程序最终要靠某个「后端（backend）」来真正跑模型——可以是同进程的 `sglang.Runtime` / `sglang.Engine`，也可以是一个远程的 HTTP 服务（`sglang.RuntimeEndpoint`），甚至可以是 OpenAI、Anthropic 等第三方接口。本讲聚焦「怎么写程序」，后端抽象留到 u2-l3。

> 名词约定：本讲反复出现「表达式」「原语」「程序」「后端」四个词。表达式 = 一条 SGLang 指令的对象表示；原语 = 构造表达式的工厂函数（`gen`/`select`/`image`）；程序 = 被 `@function` 装饰的函数；后端 = 真正执行表达式、调用模型的地方。

## 3. 本讲源码地图

本讲主要围绕两个文件展开，它们一个负责「词汇表」（构造表达式），一个负责「表达式本身的数据结构」。

| 文件 | 作用 |
| --- | --- |
| [lang/api.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py) | 前端 DSL 的公共 API，提供 `function`、`gen`、`gen_int`、`gen_string`、`select`、`image`、`video` 等原语，是把用户意图**翻译成表达式对象**的地方。 |
| [lang/ir.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py) | IR（Intermediate Representation，中间表示）。定义 `SglExpr` 及其子类（`SglGen`/`SglSelect`/`SglImage`/`SglFunction` 等），是前端所有原语最终构造出的数据结构。 |

为了把「惰性求值」讲透，本讲还会少量引用下面三个真实文件作为佐证（均在本仓库中）：

| 文件 | 用途 |
| --- | --- |
| [lang/choices.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py) | `select` 的候选项打分策略（`choices_method`），如 `token_length_normalized`。 |
| [lang/interpreter.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py) | 负责真正「执行」表达式对象的解释器，证明 `gen/select/image` 是「先记录、后执行」。 |
| [global_config.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py) | 存放 `default_backend`，即「不显式指定后端时，用谁跑程序」。 |

> 提示：本讲所有永久链接都固定到 HEAD `d0b9689805`，可直接点击跳转。下文每个代码点都会标注「这段代码做了什么」。

---

## 4. 核心概念与源码讲解

本讲按「先建直觉、再讲源码」的顺序，从最底层的 `SglExpr` 表达式讲起，再依次讲 `function`、`gen`、`select`、`image`。这五个最小模块构成前端 DSL 的地基。

### 4.1 SglExpr 表达式：前端 DSL 的统一数据结构

#### 4.1.1 概念说明

`SglExpr` 是前端所有「指令」的基类。无论你要生成文本（`gen`）、挑选候选（`select`）、插入图片（`image`），最终构造出来的对象都是 `SglExpr` 的某个子类实例。

为什么要把「要做的事」封装成对象，而不是直接执行？因为前端想做到三件事，而这三件事都要求「程序先变成数据，再被执行」：

1. **惰性求值**：`gen("answer")` 只造对象、不调模型，方便把多条指令攒成一张图。
2. **可拼接**：用 `+` 把文本和表达式拼起来，得到一个更大的表达式（`SglExprList`）。
3. **可分析**：解释器和 tracer 可以遍历这张图，做批处理、前缀缓存、并行编码等优化。

一句话总结：**`SglExpr` 是「指令的对象化」，它让一段生成流程变成可以被 Python 自由传递、拼接、分析的数据。**

#### 4.1.2 核心流程

一个表达式从被构造到被执行，经历三步：

```text
1. 构造  gen("answer") ──►  SglGen 对象（含 name、sampling_params）
                       （此刻不调模型，只分配 node_id）

2. 拼接  "Q:..." + gen("answer") ──► SglExprList（用 __add__/__radd__）

3. 提交  s += 表达式  ──►  ProgramState.__iadd__ ──► StreamExecutor.submit
                          ──► 进入执行队列 ──► 解释器 _execute 分发
                          ──► 真正调用后端（generate/select）
```

关键点：步骤 1、2 完全不涉及模型；只有步骤 3 才会触发真正的模型调用。这就是「惰性」的含义。

#### 4.1.3 源码精读

先看 `SglExpr` 基类本身。每个表达式在构造时都会拿到一个全局自增的 `node_id`，并初始化两个链接字段 `prev_node`（前驱节点）和 `pid`（所属程序 id），用于把表达式串成图：

```python
# ir.py
class SglExpr:
    node_ct = 0

    def __init__(self):
        self.node_id = SglExpr.node_ct
        self.prev_node = None
        self.pid = None
        SglExpr.node_ct += 1
```

👉 [lang/ir.py:327-334](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L327-L334)：定义 `SglExpr` 基类，`node_ct` 是类级计数器，给每个表达式分配唯一编号，方便调试与画图。

表达式支持用 `+` 拼接。`__add__` 把「右侧是字符串」的情况自动包成 `SglConstantText`（常量文本表达式），再交给 `concatenate_ir` 合并：

```python
# ir.py
def __add__(self, other):
    if isinstance(other, str):
        other = SglConstantText(other)
    assert isinstance(other, SglExpr)
    return self.concatenate_ir(self, other)
```

👉 [lang/ir.py:336-341](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L336-L341)：`SglExpr.__add__`。这就是为什么你能写 `"前缀" + sgl.gen("x")`——字符串会被自动转成 `SglConstantText` 表达式，再和 `gen` 拼到一起。

`concatenate_ir` 负责把两个表达式合并成一个列表表达式 `SglExprList`，并尽量「展平」嵌套列表：

👉 [lang/ir.py:350-359](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L350-L359)：`concatenate_ir`。若两边都是 `SglExprList`，就拼接内部列表，避免层层嵌套。

为了佐证「表达式先记录、后执行」，看解释器里的分发函数。注意它接收的是 `SglExpr`（已经是对象），按类型决定怎么执行：

```python
# interpreter.py
def _execute(self, other):
    if isinstance(other, str):
        other = SglConstantText(other)
    assert isinstance(other, SglExpr), f"{other}"

    if isinstance(other, SglConstantText):
        self._execute_fill(other.value)
    elif isinstance(other, SglGen):
        self._execute_gen(other)
    elif isinstance(other, SglSelect):
        self._execute_select(other)
    ...
    elif isinstance(other, SglImage):
        self._execute_image(other)
```

👉 [lang/interpreter.py:461-503](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L461-L503)：`StreamExecutor._execute`，按 `SglExpr` 子类分发到 `_execute_gen` / `_execute_select` / `_execute_image` 等。这证明：构造（api.py）和执行（interpreter.py）是分离的两个阶段。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`gen` 返回的是一个表达式对象，而不是生成的文本」。

**操作步骤**：

1. 在装好 sglang 的环境里，写一个小脚本 `expr_probe.py`（**示例代码**，非项目原有文件）：

   ```python
   import sglang as sgl

   e = sgl.gen("answer", stop="\n")
   print("type:", type(e))
   print("repr:", repr(e))
   ```

2. 注意：**不要**把它加进任何 `@function` 程序，单独运行即可。

**需要观察的现象**：脚本会立刻打印出类型与 repr，**全程没有任何模型调用**、不需要起服务、不需要 GPU。

**预期结果**：打印类似 `type: <class 'sglang.lang.ir.SglGen'>` 与 `repr: Gen('answer')`。这正好对应 ir.py 里 `SglGen.__repr__` 的 `f"Gen('{self.name}')"`（[lang/ir.py:502-503](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L502-L503)）。如果你看到的是一个字符串，那说明你误解了它——它就是对象本身。若运行时报缺依赖等，则**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`"A:" + sgl.gen("x")` 的结果是什么类型？为什么不会报错？
**答案**：结果是 `SglExprList`。不报错是因为 `SglExpr.__add__` 会把左侧字符串 `"A:"` 自动包成 `SglConstantText`，再与 `SglGen` 合并（[lang/ir.py:336-341](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L336-L341)）。

**练习 2**：`SglExpr` 在构造时设置了哪两个字段用于「把表达式串成图」？
**答案**：`prev_node`（前驱节点）和 `pid`（程序 id），加上全局自增的 `node_id`（[lang/ir.py:330-334](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L330-L334)）。

---

### 4.2 function 装饰器：把普通函数变成可复用的生成程序

#### 4.2.1 概念说明

光有表达式还不够，我们需要一个「容器」把一段生成流程封装起来，让它能被复用、被传参、被批量调用。`@sgl.function`（即 `api.py` 里的 `function` 装饰器）就是这个容器：它把你写的普通 Python 函数包装成一个 `SglFunction` 对象。

`SglFunction` 解决三个问题：

1. **复用**：同一段「先 few-shot、再提问、再生成」的流程写一次，换不同输入反复跑。
2. **传参**：像普通函数一样接受参数，但这些参数会成为程序里的变量（`SglArgument`）。
3. **统一入口**：提供 `.run()` / `.run_batch()` / `.trace()` 等方法，决定「现在就执行」还是「先追踪成 IR」。

约定（强约束）：被装饰的函数，**第一个参数必须叫 `s`**。这个 `s` 是 `ProgramState`（程序状态），你往它身上 `+=` 表达式，就是在往程序里追加指令。

#### 4.2.2 核心流程

```text
@sgl.function
def my_prog(s, question):        # 第一个参数必须是 s
    s += "Q: " + question        # 追加文本表达式
    s += sgl.gen("answer")       # 追加生成表达式（此刻不执行）

# —— 此时 my_prog 是 SglFunction，函数体一次都没跑 ——

state = my_prog.run(question="...")   # 真正触发：run_program → 解释器逐条执行
print(state["answer"])                # 取出名为 answer 的生成结果
```

`run()` 内部会创建一个 `StreamExecutor`（流式执行器）和 `ProgramState`，然后在**一个线程里**真正调用你写的函数体；函数体里每一次 `s += expr` 都把表达式送进执行队列，由解释器逐条执行（[lang/interpreter.py:57-90](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L57-L90)）。

#### 4.2.3 源码精读

`function` 装饰器本身非常薄——它的全部职责就是返回一个 `SglFunction`：

```python
# api.py
def function(
    func: Optional[Callable] = None, num_api_spec_tokens: Optional[int] = None
):
    if func:
        return SglFunction(func, num_api_spec_tokens=num_api_spec_tokens)

    def decorator(func):
        return SglFunction(func, num_api_spec_tokens=num_api_spec_tokens)

    return decorator
```

👉 [lang/api.py:23-32](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L23-L32)：`function` 装饰器。同时支持 `@sgl.function`（无参）和 `@sgl.function(num_api_spec_tokens=...)`（带参）两种写法——这就是 `if func:` 分支的存在意义。

真正的工作在 `SglFunction.__init__`。它会用 `inspect.getfullargspec` 解析被装饰函数的参数，并**断言第一个参数必须是 `"s"`**：

```python
# ir.py
def __init__(self, func, num_api_spec_tokens=None, bind_arguments=None):
    self.func = func
    self.num_api_spec_tokens = num_api_spec_tokens
    self.bind_arguments = bind_arguments or {}
    self.pin_prefix_rid = None

    # Parse arguments
    argspec = inspect.getfullargspec(func)
    assert argspec.args[0] == "s", 'The first argument must be "s"'
    self.arg_names = argspec.args[1:]
    self.arg_defaults = argspec.defaults if argspec.defaults is not None else []
```

👉 [lang/ir.py:142-152](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L142-L152)：`SglFunction.__init__`。`assert argspec.args[0] == "s"` 就是「第一个参数必须叫 s」这条约定的强制检查；`arg_names`/`arg_defaults` 记下其余参数名与默认值，供 `run`/`run_batch` 校验入参。

`SglFunction` 还提供了 `.bind(**kwargs)`，可以预先固定部分参数，返回一个新的 `SglFunction`（不可变风格，不改原对象）：

👉 [lang/ir.py:154-158](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L154-L158)：`bind` 用 `{**self.bind_arguments, **kwargs}` 合并参数后构造新对象，体现「prefer immutable」。

最关键的「什么时候执行」逻辑在 `__call__`：如果当前不在追踪作用域里，就调用 `self.run(...)` 真正执行；否则走 `trace`（追踪成 IR，不执行）：

```python
# ir.py
def __call__(self, *args, **kwargs):
    from sglang.lang.tracer import TracingScope

    tracing_scope = TracingScope.get_current_scope()
    if tracing_scope is None:
        return self.run(*args, **kwargs)
    else:
        kwargs["backend"] = tracing_scope.tracer_state.backend
        return self.trace(*args, **kwargs)
```

👉 [lang/ir.py:316-324](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L316-L324)：`SglFunction.__call__`。这解释了「为什么直接 `my_prog(...)` 会触发执行」——默认没有追踪作用域，于是走 `run`。

#### 4.2.4 代码实践

**实践目标**：写一个最简的 `@sgl.function` 程序，跑通「few-shot 问答」并验证 `s` 必须是第一个参数。

**操作步骤**：

1. 复制官方示例 [examples/frontend_language/quick_start/local_example_complete.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/examples/frontend_language/quick_start/local_example_complete.py)，重点看其函数体（**这是项目原有示例**）：

   ```python
   @sgl.function
   def few_shot_qa(s, question):
       s += """The following are questions with answers.
   Q: What is the capital of France?
   A: Paris
   ...
   """
       s += "Q: " + question + "\n"
       s += "A:" + sgl.gen("answer", stop="\n", temperature=0)
   ```

   👉 该示例 [第 9-20 行](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/examples/frontend_language/quick_start/local_example_complete.py#L9-L20)：注意 `s` 是第一个参数，函数体里全部用 `s +=` 追加表达式。

2. 故意把第一个参数改名（如改成 `state`），重新运行，观察报错。

**需要观察的现象**：第 2 步应抛出 `AssertionError: The first argument must be "s"`。

**预期结果**：改名后直接在装饰阶段（构造 `SglFunction` 时）断言失败，对应 [lang/ir.py:150](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L150) 的断言。完整跑通原示例需要本地有可用模型与 GPU，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`@sgl.function` 装饰后得到的对象，是普通函数还是 `SglFunction`？它的 `.run()` 方法内部最终调用了哪个模块的函数？
**答案**：得到的是 `SglFunction` 实例。`.run()` 内部 `from sglang.lang.interpreter import run_program` 后调用 `run_program`（[lang/ir.py:160-221](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L160-L221)）。

**练习 2**：为什么 `SglFunction.__call__` 要先判断 `TracingScope.get_current_scope()`？
**答案**：为了区分「立即执行」与「被追踪成 IR」。没有追踪作用域时走 `run` 真正执行；在追踪作用域内（如被另一个 `@function` 调用、或显式 trace）则走 `trace` 只记录不执行（[lang/ir.py:316-324](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L316-L324)）。追踪机制详见 u2-l2。

---

### 4.3 gen / gen_int / gen_string：生成原语

#### 4.3.1 概念说明

`gen` 是最常用的原语：它表示「在这里让模型生成一段文本」。它接受一堆采样参数（`temperature`、`top_p`、`stop`、`max_tokens`…），返回一个 `SglGen` 表达式。

`gen_int` 和 `gen_string` 是 `gen` 的两个**特化快捷方式**：

- `gen_int`：生成一个整数（内部把 `dtype` 设为 `int`）。
- `gen_string`：生成一个字符串（内部把 `dtype` 设为 `str`）。

「dtype」有什么用？它会被用于**约束生成（constrained generation）**——比如 `gen_int` 会让模型只输出匹配整数正则的 token，从而保证拿到的是合法整数。ir.py 顶部就预定义了这些正则：

```python
# ir.py
REGEX_INT = r"[-+]?[0-9]+[ \n]*"
REGEX_FLOAT = r"[-+]?[0-9]*\.?[0-9]+[ \n]*"
REGEX_BOOL = r"(True|False)"
REGEX_STR = r"\"[\w\d\s]*\""
```

👉 [lang/ir.py:11-14](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L11-L14)：预定义的类型正则，约束解码时会用到（约束解码细节在第 8 单元 u8-l4 讲）。

一个特别重要的细节：**当你给 `gen` 传了 `choices` 参数时，它不再是「自由生成」，而会变成一个 `select`**（候选项挑选）。这一点会在 4.4 节展开，但根源就在 `gen` 的实现里。

#### 4.3.2 核心流程

```text
sgl.gen(name="answer", stop="\n", temperature=0)
        │
        ├─ 传了 choices？ ──► 是：返回 SglSelect（退化为 select）
        │                  否：继续
        ├─ 校验 regex（若给了）能否编译
        └─ 构造 SglGen(name, max_tokens, ..., dtype, regex, json_schema)
```

`SglGen` 在构造时，会把所有采样参数打包进一个 `SglSamplingParams` 对象（[lang/ir.py:451-500](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L451-L500)）。这个 `SglSamplingParams` 才是真正承载「怎么采样」的数据结构，它还能转换成不同后端需要的格式（`to_openai_kwargs` / `to_srt_kwargs` / …）。

#### 4.3.3 源码精读

先看 `gen` 的签名与最关键的「choices 分支」：

```python
# api.py
def gen(
    name: Optional[str] = None,
    max_tokens: Optional[int] = None,
    ...
    dtype: Optional[Union[type, str]] = None,
    choices: Optional[List[str]] = None,
    choices_method: Optional[ChoicesSamplingMethod] = None,
    regex: Optional[str] = None,
    json_schema: Optional[str] = None,
):
    """Call the model to generate. ..."""

    if choices:
        return SglSelect(
            name,
            choices,
            0.0 if temperature is None else temperature,
            token_length_normalized if choices_method is None else choices_method,
        )

    # check regex is valid
    if regex is not None:
        try:
            re.compile(regex)
        except re.error as e:
            raise e

    return SglGen(name, max_tokens, ..., dtype, regex, json_schema)
```

👉 [lang/api.py:75-139](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L75-L139)：`gen` 的完整实现。注意三件事：(1) `if choices:` 命中时直接返回 `SglSelect`——**`gen` 带 choices 等价于 `select`**；(2) `regex` 会先用 `re.compile` 校验合法性；(3) 不带 choices 才构造 `SglGen`。

`gen_int` 和 `gen_string` 则是薄薄的包装，差别只在 `dtype` 实参分别是 `int` 与 `str`（注意它们不支持 regex/json_schema，只走纯生成）：

```python
# api.py —— gen_int 末尾
    return SglGen(name, max_tokens, None, n, stop, ..., int, None)
                                                  ^^^^  dtype=int
```

👉 [lang/api.py:142-182](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L142-L182)：`gen_int`，倒数第二个实参是 `int`（第 180 行）。
👉 [lang/api.py:185-225](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L185-L225)：`gen_string`，对应位置实参是 `str`（第 223 行）。

再看 `SglGen` 构造时如何把参数收拢进 `SglSamplingParams`：

```python
# ir.py
class SglGen(SglExpr):
    def __init__(self, name=None, max_new_tokens=None, ..., dtype=None, regex=None, json_schema=None):
        super().__init__()
        self.name = name
        self.sampling_params = SglSamplingParams(
            max_new_tokens=max_new_tokens,
            ...,
            dtype=dtype,
            regex=regex,
            json_schema=json_schema,
        )
```

👉 [lang/ir.py:451-500](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L451-L500)：`SglGen.__init__`。`gen` 的所有参数最终都落到 `self.sampling_params` 上；执行时解释器会读取这个对象去调后端。

> 提示：`SglGen` 的 `__repr__` 是 `Gen('{self.name}')`（[lang/ir.py:502-503](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L502-L503)），调试时打印表达式会看到这种简洁形式。

#### 4.3.4 代码实践

**实践目标**：对比「`gen` 带 choices」与「不带 choices」返回的对象类型，直观感受 `gen` 的两种身份。

**操作步骤**：

1. 写脚本 `gen_probe.py`（**示例代码**）：

   ```python
   import sglang as sgl

   a = sgl.gen("free", max_tokens=8)
   b = sgl.gen("picked", choices=["A", "B", "C"])

   print("free   ->", type(a).__name__, repr(a))
   print("picked ->", type(b).__name__, repr(b))
   ```

2. 运行（无需起服务、无需模型）。

**需要观察的现象**：两个对象类型不同——`a` 是 `SglGen`，`b` 是 `SglSelect`。

**预期结果**：输出大致为
`free -> SglGen Gen('free')`
`picked -> SglSelect Select('picked', choices=['A','B','C'], choices_method=<...TokenLengthNormalized...>)`
后者正是 [lang/api.py:102-108](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L102-L108) 与 [lang/ir.py:548-549](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L548-L549) 的直接体现。**待本地验证**实际 repr 文本。

#### 4.3.5 小练习与答案

**练习 1**：`sgl.gen_int("n")` 相比 `sgl.gen("n")`，多设置了什么？它有什么效果？
**答案**：多设置了 `dtype=int`（[lang/api.py:180](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L180)）。效果是触发整数约束生成，结合 `REGEX_INT`（[lang/ir.py:11](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L11)）保证输出是合法整数。

**练习 2**：为什么 `gen` 里要 `try: re.compile(regex)`？
**答案**：提前校验正则合法性，把「非法正则」这个错误从「运行到一半才暴露」提前到「构造表达式时立即报错」（[lang/api.py:110-115](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L110-L115)）。

**练习 3**：`gen` 的所有采样参数最终存在 `SglGen` 的哪个属性里？
**答案**：存在 `self.sampling_params`（一个 `SglSamplingParams` 对象，[lang/ir.py:479-500](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L479-L500)）。

---

### 4.4 select 与 choices：候选项挑选

#### 4.4.1 概念说明

`select` 解决的是「**在若干给定候选答案里挑一个**」的问题。它不是让模型自由生成，而是把每个候选都当作一段「续写」，比较模型对每个候选的「续写似然」，挑出最可能的那一个。

典型场景：多选一分类（情感是正面/负面/中性）、工具选择（用计算器还是搜索引擎）、布尔判断（True/False）。用 `select` 比让模型自由生成再解析要稳得多——因为候选是封闭集合，不会跑偏。

`select` 的签名：

```python
def select(name=None, choices=None, temperature=0.0,
           choices_method=token_length_normalized):
    assert choices is not None
    return SglSelect(name, choices, temperature, choices_method)
```

👉 [lang/api.py:236-243](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L236-L243)：`select` 原语。注意 `assert choices is not None`——候选项是必填的；默认打分方法是 `token_length_normalized`。

`choices_method` 决定「怎么比较候选」。默认的 `token_length_normalized` 是「按 token 长度归一化的平均对数概率」取胜者。它的直觉是：不同候选 token 数不同，直接比总对数概率会偏袒短候选；改成「平均每个 token 的对数概率」更公平。

#### 4.4.2 核心流程

设候选集合 \(C=\{c_1,\dots,c_K\}\)，对每个候选 \(c\)，模型给出它在当前前缀下的逐 token 对数概率。归一化得分定义为：

\[
\bar{\ell}(c)=\frac{1}{|c|}\sum_{t=1}^{|c|}\log P(w_t\mid w_{<t})
\]

获胜者取 argmax：

\[
c^{\star}=\arg\max_{c\in C}\bar{\ell}(c)
\]

执行层面，`select` 表达式被解释器分发到 `_execute_select`，调用后端的 `backend.select(...)`，把决策结果写回变量：

```python
# interpreter.py
def _execute_select(self, expr: SglSelect):
    choices_decision = self.backend.select(
        self, expr.choices, expr.temperature, expr.choices_method
    )
    if expr.name is not None:
        name = expr.name
        self.variables[name] = choices_decision.decision
        self.meta_info[name] = choices_decision.meta_info
        self.variable_event[name].set()
    self.text_ += choices_decision.decision
```

👉 [lang/interpreter.py:647-658](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L647-L658)：`_execute_select`。决策文本写入 `self.variables[name]`，并通过 `variable_event[name].set()` 通知「这个变量已就绪」——这正是后续 `state["tool"]` 能取到值的机制（见 4.4.4）。

#### 4.4.3 源码精读

打分策略的基类与默认实现：

```python
# choices.py
class ChoicesSamplingMethod(ABC):
    @abstractmethod
    def __call__(self, *, choices, normalized_prompt_logprobs,
                 input_token_logprobs, output_token_logprobs,
                 unconditional_token_logprobs=None) -> ChoicesDecision: ...


class TokenLengthNormalized(ChoicesSamplingMethod):
    def __call__(self, *, choices, normalized_prompt_logprobs, ...):
        """Select the option with the highest token length normalized prompt logprob."""
        best_choice = choices[np.argmax(normalized_prompt_logprobs)]
        ...
        return ChoicesDecision(decision=best_choice, meta_info=meta_info)


token_length_normalized = TokenLengthNormalized()
```

👉 [lang/choices.py:14-53](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L14-L53)：`ChoicesSamplingMethod` 抽象基类与默认实现 `TokenLengthNormalized`。注意真正的「归一化」是在后端算好后传进来的 `normalized_prompt_logprobs`，这里只是 `np.argmax` 选最大——把数学和工程清晰分层。`token_length_normalized` 是模块级单例，被 `gen`/`select` 当默认值复用。

`SglSelect` 表达式本身只是个数据容器，存 `name / choices / temperature / choices_method` 四样东西：

👉 [lang/ir.py:533-549](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L533-L549)：`SglSelect` 定义与 `__repr__`。它不参与打分计算，打分由 `choices_method` 在执行期完成——再次体现「数据（表达式）与行为（后端/方法）分离」。

`select` 的结果怎么取出来？靠 `ProgramState` 的 `__getitem__` 转发到 `get_var`，而 `get_var` 会在事件上阻塞等待，直到变量就绪：

```python
# interpreter.py
def get_var(self, name):
    if name in self.variable_event:
        self.variable_event[name].wait()    # 阻塞，直到 _execute_select 里 set()
    return self.variables[name]
```

👉 [lang/interpreter.py:354-357](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L354-L357)：`StreamExecutor.get_var`。配合 [lang/interpreter.py:1029-1030](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L1029-L1030) 的 `ProgramState.__getitem__`，就解释了为什么 `state["tool"]` 能拿到 select 的结果。

#### 4.4.4 代码实践（本讲核心实践，对应规格里的 practice_task）

**实践目标**：用 `@sgl.function` 写一个「三选一」分类程序，用 `select` 在 `['A','B','C']` 里挑答案，并打印 `state` 的类型，体会惰性求值。

**操作步骤**：

1. 先启动一个本地服务（参考 u1-l2）：

   ```bash
   python -m sglang.launch_server --model-path Qwen/Qwen2.5-0.5B --port 30000
   ```

2. 写脚本 `three_way_select.py`（**示例代码**，改编自官方 [choices_logprob.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/examples/frontend_language/usage/choices_logprob.py)）：

   ```python
   import sglang as sgl

   @sgl.function
   def multi_choice(s, question):
       s += "Question: " + question + "\n"
       s += "Answer (A/B/C): " + sgl.select("choice", choices=["A", "B", "C"])

   sgl.set_default_backend(sgl.RuntimeEndpoint("http://localhost:30000"))

   state = multi_choice.run(question="The sky is usually ___ in daytime. A) blue  B) red  C) black")
   print("state type:", type(state))     # ProgramState
   print("choice:", state["choice"])     # 期望 A
   ```

3. 运行脚本。

**需要观察的现象**：
- `type(state)` 是 `ProgramState`，不是字符串、不是字典。
- `state["choice"]` 是 `'A'`/`'B'`/`'C'` 中的一个。
- 在 `run(...)` 执行**之前**，函数体里的 `sgl.select(...)` 只造了对象，没调模型——可在 `multi_choice` 函数体第一行加 `print(type(sgl.select("choice", choices=["A","B","C"])))` 验证它打印 `SglSelect`。

**预期结果**：`state type: <class 'sglang.lang.interpreter.ProgramState'>`，`choice: A`（蓝色天空）。这正对应 [lang/api.py:236-243](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L236-L243) 的 `select` 与 [lang/interpreter.py:647-658](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L647-L658) 的执行写入。能否跑通取决于本地是否起好服务，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`sgl.gen("x", choices=["A","B"])` 和 `sgl.select("x", choices=["A","B"])` 返回的对象是否相同？
**答案**：相同，都是 `SglSelect` 实例。`gen` 在带 `choices` 时直接 `return SglSelect(...)`（[lang/api.py:102-108](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L102-L108)），二者等价。

**练习 2**：默认 `choices_method` 是什么？它用什么数学量来比较候选？
**答案**：默认是 `token_length_normalized`（`TokenLengthNormalized` 单例）。它比较「按 token 长度归一化的平均对数概率」`normalized_prompt_logprobs`，取 `np.argmax`（[lang/choices.py:44](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L44)）。

**练习 3**：为什么 `state["choice"]` 能拿到 select 的结果，而不是拿到一个表达式对象？
**答案**：因为 `run()` 真正执行了程序，`_execute_select` 把决策文本写进了 `variables["choice"]` 并 `set()` 事件；`state["choice"]` 经 `ProgramState.__getitem__` → `get_var` → `variable_event[name].wait()` 拿到的是已就绪的字符串（[lang/interpreter.py:354-357](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L354-L357)、[lang/interpreter.py:647-658](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L647-L658)）。

---

### 4.5 image / video：多模态输入原语

#### 4.5.1 概念说明

`image` 和 `video` 是多模态输入原语：它们把一张图片或一段视频「注入」到程序里，让视觉模型不仅能读文字，还能「看到」图像。

与 `gen`/`select` 一样，它们返回的也是 `SglExpr` 子类（`SglImage` / `SglVideo`），同样**不立即执行**——只有在程序运行时，解释器才会真正把图片读出来、编码成 base64、交给后端。

典型用法是把 `image` 和文本拼在一起，包在 `sgl.user(...)` 角色原语里：

```python
s += sgl.user(sgl.image(image_path) + question)
s += sgl.assistant(sgl.gen("answer"))
```

（`sgl.user`/`sgl.assistant` 等角色原语在 u2-l4 细讲，这里只需知道它们给一段表达式加上对话角色标记。）

#### 4.5.2 核心流程

```text
sgl.image("cat.jpeg")
    │  构造 SglImage(path="cat.jpeg")   （不读文件、不编码）
    ▼
s += sgl.user(sgl.image(...) + question)   拼成带角色的表达式
    ▼
run() 执行 ──► _execute_image ──► encode_image_base64(path)
            ──► 把 (path, base64) 存进 cur_images
            ──► 在文本流里占一个 image_token 占位符
```

执行时（[lang/interpreter.py:524-531](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L524-L531)），图片被 base64 编码并收集到 `cur_images`，同时在文本里插入一个 `chat_template.image_token` 占位符——这样后续 token 化时，模型就知道这个位置要填图像 embedding。

#### 4.5.3 源码精读

`image` / `video` 原语同样是薄包装：

```python
# api.py
def image(expr: SglExpr):
    return SglImage(expr)


def video(path: str, num_frames: int):
    return SglVideo(path, num_frames)
```

👉 [lang/api.py:228-233](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L228-L233)：`image` 与 `video`。注意 `image` 的形参名虽叫 `expr`、注解为 `SglExpr`，但它实际被当作图片**路径字符串**传给 `SglImage(path)`；`video` 则需要额外指定抽取的帧数 `num_frames`。

对应的表达式类只存数据：

```python
# ir.py
class SglImage(SglExpr):
    def __init__(self, path: str):
        self.path = path

class SglVideo(SglExpr):
    def __init__(self, path: str, num_frames: int):
        self.path = path
        self.num_frames = num_frames
```

👉 [lang/ir.py:434-448](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L434-L448)：`SglImage` / `SglVideo`。注意 `SglImage.__init__` **没有调用 `super().__init__()`**（不像 `SglGen`/`SglSelect`），因此它不会分配 `node_id`——这是源码现状的一个小细节，调试时留意。`SglVideo` 多存一个 `num_frames`。

执行期的处理在解释器：

```python
# interpreter.py
def _execute_image(self, expr: SglImage):
    path = expr.path
    base64_data = encode_image_base64(path)
    self.images_.append((path, base64_data))
    self.cur_images.append((path, base64_data))
    self.text_ += self.chat_template.image_token
```

👉 [lang/interpreter.py:524-531](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L524-L531)：`_execute_image`。真正的「读图 + base64 编码」发生在执行期而非构造期——这又一次印证惰性求值：构造 `SglImage` 时文件还没被读取。

> 配套的真实示例见 [examples/frontend_language/quick_start/local_example_llava_next.py:9-12](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/examples/frontend_language/quick_start/local_example_llava_next.py#L9-L12)，它把 `sgl.image(image_path) + question` 包进 `sgl.user(...)`，再用 `sgl.gen("answer")` 生成回答。

#### 4.5.4 代码实践

**实践目标**：验证 `sgl.image(...)` 返回的是表达式对象，且构造时不读取文件。

**操作步骤**：

1. 写脚本 `image_probe.py`（**示例代码**）：

   ```python
   import sglang as sgl

   img = sgl.image("/this/path/does/not/exist.jpeg")  # 故意写一个不存在的路径
   print("type:", type(img).__name__)
   print("path:", img.path)
   ```

2. 运行（无需模型、无需真实图片）。

**需要观察的现象**：脚本**不会报「文件不存在」**，而是正常打印类型与 path。

**预期结果**：输出 `type: SglImage` 与 `path: /this/path/does/not/exist.jpeg`。因为构造 `SglImage` 只存路径（[lang/ir.py:434-436](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L434-L436)），真正读图在 `_execute_image` 里（[lang/interpreter.py:524-531](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L524-L531)）。把不存在的路径真正跑进 `@function` 程序时才会报错，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`sgl.image(path)` 和 `sgl.video(path, num_frames)` 的表达式类分别是什么？构造时会读取文件吗？
**答案**：分别是 `SglImage` 和 `SglVideo`（[lang/ir.py:434-448](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L434-L448)）。构造时只存路径/帧数，不读文件；读取与 base64 编码发生在执行期 `_execute_image`（[lang/interpreter.py:524-531](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L524-L531)）。

**练习 2**：图片被「注入」到文本流时，用什么占位？
**答案**：用 `self.chat_template.image_token` 占位符（[lang/interpreter.py:531](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L531)），后续 token 化时该位置会被替换成图像 embedding。

---

## 5. 综合实践

把本讲的四个最小模块（`function`、`gen`、`select`、`image`）串成一个完整的小任务：**写一个「看图三选一」的多模态分类程序**。

**任务描述**：给一张图片和一个问题，模型先自由生成一句简短描述（`gen`），再在 `['A','B','C']` 三个候选答案里挑一个（`select`）。要求全程体会「构造表达式 ≠ 执行」。

**参考实现**（**示例代码**，融合 [local_example_llava_next.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/examples/frontend_language/quick_start/local_example_llava_next.py) 与 [choices_logprob.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/examples/frontend_language/usage/choices_logprob.py)）：

```python
import sglang as sgl
from sglang.lang.chat_template import get_chat_template

get_chat_template("llava-next")     # 选一个支持视觉的 chat 模板

@sgl.function
def image_multi_choice(s, image_path, question):
    # 1) 注入图片 + 问题（image 原语，惰性）
    s += sgl.user(sgl.image(image_path) + "\n" + question)
    # 2) 让模型先自由描述一句（gen 原语）
    s += sgl.assistant(sgl.gen("desc", max_tokens=16, stop="\n"))
    # 3) 在三选一里挑答案（select 原语；等价于带 choices 的 gen）
    s += sgl.user("Now pick A, B, or C.")
    s += sgl.assistant(sgl.select("choice", choices=["A", "B", "C"]))

sgl.set_default_backend(sgl.RuntimeEndpoint("http://localhost:30000"))

state = image_multi_choice.run(
    image_path="images/cat.jpeg",
    question="Which animal is this? A) cat  B) dog  C) bird",
)
print("desc  :", state["desc"])
print("choice:", state["choice"])
print("full text:\n", state.text())
```

**操作步骤**：
1. 用一个支持视觉的模型起服务（如 `--model-path` 指向一个 LLaVA / Qwen-VL 系列模型），端口 30000。
2. 准备 `images/cat.jpeg` 等图片（参考 llava 示例目录）。
3. 运行上面的脚本。

**需要观察的现象 / 预期结果**：
- `state` 是 `ProgramState`；`state["desc"]` 是一段自由文本，`state["choice"]` 是 `'A'`/`'B'`/`'C'` 之一。
- 在 `run(...)` 之前，函数体里的 `sgl.image / sgl.gen / sgl.select` 都只是表达式对象（可在函数体首行分别 `print(type(...))` 验证）；只有 `run()` 之后才有模型调用。
- 若把 `sgl.select(...)` 换成 `sgl.gen("choice", choices=["A","B","C"])`，行为应完全一致（因为 `gen` 带 choices 会退化为 `SglSelect`，[lang/api.py:102-108](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L102-L108)）。

能否跑通取决于本地是否有可用视觉模型与图片，**待本地验证**。即便无法跑通，你也可以完成「源码阅读型」部分：在 `image_multi_choice` 函数体里给每个原语加 `print(type(...))`，对照本讲 4.1–4.5 的源码精读，确认它们打印的分别是 `SglImage`/`SglGen`/`SglSelect`，从而验证「惰性求值」。

## 6. 本讲小结

- **`SglExpr` 是统一数据结构**：`gen`/`select`/`image`/`video` 返回的都是 `SglExpr` 子类对象，构造时不执行（[lang/ir.py:327-334](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L327-L334)）。
- **`@sgl.function` 把函数包成 `SglFunction`**：第一个参数必须叫 `s`（`ProgramState`）；`.run()` 才真正执行（[lang/api.py:23-32](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L23-L32)、[lang/ir.py:142-152](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L142-L152)）。
- **`gen` 有两种身份**：带 `choices` 时退化为 `SglSelect`，否则构造 `SglGen`；`gen_int`/`gen_string` 只是设了 `dtype` 的快捷方式（[lang/api.py:75-225](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L75-L225)）。
- **`select` 用似然挑候选**：默认 `token_length_normalized` 按归一化平均对数概率取 argmax，结果写入变量供 `state["name"]` 读取（[lang/choices.py:44](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L44)、[lang/interpreter.py:647-658](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L647-L658)）。
- **`image`/`video` 也是惰性表达式**：构造只存路径，读图与 base64 编码延迟到执行期（[lang/ir.py:434-448](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L434-L448)、[lang/interpreter.py:524-531](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L524-L531)）。
- **构造与执行是两阶段**：`api.py`/`ir.py` 负责「造对象」，`interpreter.py` 负责「执行对象」，这是前端可被分析、批处理、缓化的根基。

## 7. 下一步学习建议

本讲你学会了「怎么写前端程序」，但还有两个关键问题悬而未决：

1. **程序到底是怎么被一步步执行的？** `SglExpr` 对象进入 `StreamExecutor` 队列后，解释器如何调度？tracer 又是如何把 `@function` 追踪成一张可分析的 IR 图？→ 继续学 **u2-l2《IR、Tracer 与解释器：前端程序如何执行》**，它正好接着本讲的 `SglFunction.__call__` 的「run vs trace」分支往下讲。
2. **程序背后真正调用的「后端」是什么？** 本讲的 `sgl.RuntimeEndpoint` / `sgl.Runtime` 只是名字，它们如何把前端表达式翻译成对运行时或第三方 API 的调用？→ 学 **u2-l3《后端抽象：RuntimeEndpoint 与第三方后端》**。

如果你更关心「对话角色与 choices 归一化策略」的细节，可以先跳到 **u2-l4《Chat 模板与 choices 选择策略》**，深入了解 `system/user/assistant` 角色原语与 `token_length_normalized` 等多种 `choices_method` 的差异。
