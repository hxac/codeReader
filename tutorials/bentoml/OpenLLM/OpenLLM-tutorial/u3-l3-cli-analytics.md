# CLI 命令装饰器与使用分析 analytic.py

## 1. 本讲目标

本讲精读 `src/openllm/analytic.py`（仅约 106 行，却支撑了整个 CLI 的「命令注册 + 使用埋点」），学完后你应该能够：

- 说清 `OpenLLMTyper` 为什么继承 `typer.Typer`、它对 `__init__` 做了哪些统一化、以及它是如何通过**重写 `command` 方法**把每一条 `@app.command` 都自动裹上一层埋点的（即「装饰器劫持」）。
- 看懂 `EventMeta.event_name` 如何用一条正则把类名（CamelCase）翻译成事件名（snake_case），并去掉 `_event` 后缀；以及 `CliEvent` / `OpenllmCliEvent` 记录了哪些字段。
- 掌握 `track` 埋点在「成功 / 失败」两条路径上分别记录了什么（`cmd_group` / `cmd_name` / `duration` / `error_type` / `return_code`），以及用户如何通过 `BENTOML_DO_NOT_TRACK` 关闭追踪。

本讲是专家层内容，承接 [u1-l3](u1-l3-cli-entry-and-commands.md)：在那里你已经知道 `app` 是 `OpenLLMTyper` 的实例、`OpenLLMTyper` 来自 `analytic.py`，本讲就钻进 `analytic.py` 把这套「透明埋点」机制拆开给你看。

## 2. 前置知识

阅读本讲前，建议你先建立下面这些直觉（不熟悉的术语这里一并解释）：

- **装饰器（decorator）**：Python 里形如 `@app.command` 的写法。它本质是一个「接收函数、返回新函数」的高阶函数。`@app.command` 作用在 `def serve(...)` 上，等价于 `serve = app.command()(serve)`。本讲的核心就是：`OpenLLMTyper` 把 `command` 这个方法**换成了自己的版本**，从而在「返回新函数」这一步偷偷加料。
- **`functools.wraps`**：让包装后的函数保留原函数的 `__name__`、`__doc__` 等元信息，这样 Typer/Click 依然能从函数签名里读出参数与帮助文本。这是埋点能「透明」的关键之一。
- **Click 的 Context 上下文对象**：Typer 底层是 Click。每条命令执行时，Click 都会构造一个 `click.Context`，它记录了「我是谁、我的父命令是谁、根命令是谁」。埋点正是从 `ctx` 里读出 `command_name` 和 `command_group` 的。
- **埋点（analytics / telemetry）**：程序主动把自己的使用情况（执行了哪条命令、耗时多久、是否报错）上报出去，用于改进产品。隐私上必须给用户提供**关闭开关**，这正是 `DO_NOT_TRACK` 的职责。
- **attrs（`attr.define`）**：一个声明式地定义数据类的库，比手写 `__init__` 简洁。`@attr.define` 自动生成构造函数、`__repr__` 等，并支持带默认值的字段。OpenLLM 用它定义事件模型 `CliEvent`。
- **`time.time_ns()`**：返回当前时间的**纳秒**整数，比 `time.time()`（秒，浮点）精度高、无浮点误差，适合做耗时差值。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/openllm/analytic.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py) | 本讲主角。定义 `DO_NOT_TRACK` 常量、`EventMeta`/`CliEvent`/`OpenllmCliEvent` 事件模型、`OrderedCommands`，以及最重要的 `OpenLLMTyper`（重写 `command` 实现透明埋点）。 |
| [src/openllm/__main__.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py) | CLI 总入口。用 `OpenLLMTyper` 创建 `app`，用 `@app.command` 注册 `hello/serve/run/deploy`，并在全局回调 `typer_callback` 里提供 `--do-not-track` 开关。 |

阅读时请把这两个文件并排打开：`analytic.py` 给出「机制」，`__main__.py` 给出「机制被谁使用」。

## 4. 核心概念与源码讲解

### 4.1 OpenLLMTyper 与 command 装饰器劫持

#### 4.1.1 概念说明

OpenLLM 希望做到两件**互相矛盾**的事：

1. 每一条命令（`serve`/`run`/`deploy`/`hello`，以及 `repo`/`model`/`clean` 子命令组里的每一条）都要自动上报使用埋点。
2. 命令的写法**完全不变**——作者只需写一个普通函数并加 `@app.command`，不需要在函数体里手动塞 `track(...)`。

解决这对矛盾的手法就是**装饰器劫持（decorator hijacking）**：自定义一个 `OpenLLMTyper` 继承 `typer.Typer`，并把它的 `command` 方法替换成自己的版本。自己的 `command` 仍然把函数注册成 Typer 命令，但在注册前先用一个 `wrapped` 函数把原函数包起来——这个 `wrapped` 负责埋点。这样一来，所有 `@app.command` 都「免费」获得了埋点，源码注释里把这叫作 *hijacking*（劫持）。

> 关键直觉：装饰器劫持 = 「我不改你的函数，我只改那个负责装饰你函数的工具」。控制权上移一层。

#### 4.1.2 核心流程

`OpenLLMTyper` 做两件事：① 在 `__init__` 里统一化 CLI 的外观与行为；② 重写 `command` 注入埋点。

`__init__` 的统一化可以用下面这段伪代码概括：

```
OpenLLMTyper.__init__:
  no_args_is_help      ← 默认 True（没给子命令时打印帮助）
  context_settings:
    help_option_names  ← 默认加上 ('-h','--help')，让 -h 也能触发帮助
    max_content_width  ← 取环境变量 COLUMNS，否则 120（控制帮助文本换行宽度）
  cls                  ← 默认 OrderedCommands（让命令按定义顺序、而非字母序展示）
  → 调用 typer.Typer.__init__ 完成真正初始化
```

`command` 的劫持流程则是：

```
@app.command 装饰某函数 f
  → 返回 decorator(f)
     → 定义 wrapped(ctx, ...):   # @click.pass_context 注入 ctx
         读 DO_NOT_TRACK → 若关闭追踪，直接 return f(...)
         记录 start_time
         try:
             返回值 = f(...)
             track( 成功事件(cmd_group, cmd_name, 耗时) )
             return 返回值
         except BaseException as e:
             track( 失败事件(cmd_group, cmd_name, 耗时, error_type, return_code) )
             raise            # 重新抛出，不吞异常
     → return typer.Typer.command(self, ...)(wrapped)   # 把 wrapped 注册成真正的命令
```

要点是最后一步：劫持版 `command` **并没有自己实现「注册命令」**，而是把 `wrapped` 交给父类原始的 `typer.Typer.command` 去注册。所以 Typer 看到的命令签名、参数解析、帮助文本全部照旧，只是真正运行时跑的是 `wrapped`。

#### 4.1.3 源码精读

先看 `__init__` 的统一化：

[src/openllm/analytic.py:L40-L52](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L40-L52) —— `OpenLLMTyper.__init__`：用 `kwargs.pop` 取出并设置三件默认行为（`-h` 别名、120 宽度、`OrderedCommands`），再调父类 `__init__`。`pop` + 自定义默认值的写法，意味着调用方仍可显式覆盖这些设置。

再看劫持的 `command`。注意第 56–57 行有一个**只在类型检查时生效**的分支：

[src/openllm/analytic.py:L54-L60](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L54-L60) —— 在 `typing.TYPE_CHECKING` 下把 `command` 指回 `typer.Typer.command`，这是为了**骗过 IDE/类型检查器**：让 `@app.command` 的类型提示保持和原生 Typer 一致（劫持不改变签名）。真正运行时走 `else` 里的自定义 `command`。

最后看注册那一步：

[src/openllm/analytic.py:L103-L105](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L103-L105) —— 把 `wrapped` 交给父类原始 `command` 注册。这一行是「劫持但不重造轮子」的精髓。

`__main__.py` 里所有顶层命令都用同一个 `app` 注册，因此都自动获得埋点：

[src/openllm/__main__.py:L19-L23](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L19-L23) —— `app = OpenLLMTyper(...)`，`help` 文案提示用户从 `openllm hello` 开始。

[src/openllm/__main__.py:L206-L207](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L206-L207) —— `@app.command` 装饰 `hello`，`serve`/`run`/`deploy` 同理（[L246](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L246)、[L271](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L271)、[L299](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L299)）。这些函数体内**没有任何** `track` 调用——埋点是装饰器偷偷加的。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`@app.command` 自动获得埋点」这一论断——写一条自定义命令，看它是否无需任何额外代码就被纳入埋点。

**操作步骤**：

1. 在本地开发副本里（按 [DEVELOPMENT.md](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/DEVELOPMENT.md) 用 `pip install -e .` 安装），打开 `src/openllm/__main__.py`。
2. 在 `app` 定义之后、`hello` 之前，新增一条最小命令：

   ```python
   # 示例代码：用于验证装饰器劫持
   @app.command(help='echo for testing analytics')
   def echo(text: str = 'hi') -> None:
       output(f'echo: {text}')
   ```

3. 运行 `openllm echo hello`。

**需要观察的现象**：

- 命令正常执行、打印 `echo: hello`，说明它已被 Typer 正确注册。
- 该函数体内没有任何埋点代码，但只要 `BENTOML_DO_NOT_TRACK` 未设为 `true`，它就与 `serve`/`run` 走同一条 `wrapped` 路径。

**预期结果**：自定义命令与官方命令在「是否被埋点」上**一视同仁**——这正是装饰器劫持的威力：加命令即加埋点，零额外成本。

> 若不想改源码，也可跳过运行，仅阅读 [L60-L105](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L60-L105) 确认：`wrapped` 对 `f` 完全无知，只依赖 `ctx` 与 `f` 的返回/异常，因此对任何被装饰函数都成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接在每个命令函数里手写 `track(...)`，而要搞一套装饰器劫持？

> **参考答案**：手动写要修改每一条命令（`hello/serve/run/deploy` 外加 `repo/model/clean` 子命令组里的几十条），易遗漏、易写错、且让业务函数混入埋点噪音。装饰器劫持把埋点集中在一处（`OpenLLMTyper.command`），新增命令自动获得埋点，删除/修改命令也无需关心埋点——这是「横切关注点（cross-cutting concern）」用装饰器统一处理的典型范式。

**练习 2**：`__init__` 里为什么要用 `kwargs.pop('no_args_is_help', True)` 而不是直接写 `self.no_args_is_help = True`？

> **参考答案**：`pop` 既设了默认值 `True`，又允许调用方通过 `OpenLLMTyper(no_args_is_help=False)` 显式覆盖。直接赋值会剥夺调用方的覆盖能力。`context_settings`/`cls` 同理。

---

### 4.2 事件元类与事件模型

#### 4.2.1 概念说明

埋点要上报「一件事」，就需要一个**事件对象**来携带这件事的信息。OpenLLM 设计了一套极简的事件体系：

- `EventMeta`：所有事件的抽象基类。它不存数据，只提供一个派生属性 `event_name`——事件名**自动从类名推出**，作者不必手写字符串。
- `CliEvent`：描述「一条 CLI 命令的执行」，用 `@attr.define` 声明字段。
- `OpenllmCliEvent`：继承 `CliEvent`，是 OpenLLM 实际上报的具体事件类型（目前只是 `pass`，留作将来区分不同来源）。

把事件名设计成「从类名派生」的好处是：新增一种事件只要新建一个类（如 `RepoAddEvent`），事件名自动是 `repo_add`，**字符串与类永远不会对不上**。

#### 4.2.2 核心流程

事件名的派生分两步——CamelCase 转 snake_case，再去掉 `_event` 后缀：

```
类名 OpenllmCliEvent
  第1步 正则 (?<!^)(?=[A-Z]) 在「非开头的大写字母」前插入下划线
        → Openllm_Cli_Event
  第2步 .lower() 全转小写
        → openllm_cli_event
  第3步 若以 _event 结尾则截掉
        → openllm_cli          ← 最终事件名
```

那条正则 `(?<!^)(?=[A-Z])` 由两个**零宽断言**组成：

- `(?<!^)`：负向后顾——当前位置**不在字符串开头**（所以首字母 `O` 前不插下划线）。
- `(?=[A-Z])`：正向前瞻——当前位置**后面紧跟一个大写字母**（所以只在 `C`、`E` 前命中）。

两者都是「零宽」的，意味着匹配不消耗任何字符，`re.sub` 只在这些位置插入 `_`。

事件字段方面，`CliEvent` 记录一次命令执行的五项信息：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `cmd_group` | `str` | 命令所属分组（`openllm` / `model` / `repo` / `clean` …） |
| `cmd_name` | `str` | 命令名（`serve` / `list` / `add` …） |
| `duration_in_ms` | `float` | 本次执行耗时（毫秒），默认 `0` |
| `error_type` | `Optional[str]` | 失败时异常类名，如 `KeyboardInterrupt`；成功时为 `None` |
| `return_code` | `Optional[int]` | 失败时 `1`（普通异常）或 `2`（Ctrl+C）；成功时为 `None` |

耗时的单位换算是个纯算术：纳秒除以 \(10^6\) 得毫秒——

\[
\text{duration\_in\_ms} = \frac{t_{\text{end, ns}} - t_{\text{start, ns}}}{10^{6}}
\]

#### 4.2.3 源码精读

`EventMeta.event_name` 是整段最精巧的几行：

[src/openllm/analytic.py:L9-L18](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L9-L18) —— `event_name` 属性：第 13 行用正则把类名转成 snake_case，第 16–17 行截掉 `_event` 后缀。注意它读的是 `self.__class__.__name__`，所以**子类**（如 `OpenllmCliEvent`）拿到的是自己的名字，而非基类 `CliEvent` 的名字。

`CliEvent` 用 attrs 声明字段，`OpenllmCliEvent` 继承它：

[src/openllm/analytic.py:L21-L32](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L21-L32) —— `CliEvent` 带 3 个必填（`cmd_group`/`cmd_name`）+ 默认值的可选字段；`OpenllmCliEvent` 目前只是 `pass`，是预留给「OpenLLM 自有事件」的具体类型，便于将来在 `track` 一侧按类型分流。

把事件名与事件类型串起来看：上报时构造的是 `OpenllmCliEvent(...)`，所以 `track` 内部若调用 `event.event_name`，得到的字符串就是 `openllm_cli`。

#### 4.2.4 代码实践

**实践目标**：脱离 OpenLLM 运行环境，单独验证 `event_name` 的命名转换规则，确保你真正理解了那条正则。

**操作步骤**：

1. 在任意目录执行下面这段**示例代码**（纯标准库，无需安装 openllm）：

   ```python
   # 示例代码：复现 EventMeta.event_name 的转换逻辑
   import re

   def to_event_name(class_name: str) -> str:
       name = re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()
       suffix = '_event'
       if name.endswith(suffix):
           name = name[: -len(suffix)]
       return name

   for cls in ['OpenllmCliEvent', 'CliEvent', 'RepoAddEvent', 'ModelListEvent', 'ServeRunEvent']:
       print(f'{cls:18s} -> {to_event_name(cls)}')
   ```

**需要观察的现象 / 预期结果**：

```
OpenllmCliEvent    -> openllm_cli
CliEvent           -> cli
RepoAddEvent       -> repo_add
ModelListEvent     -> model_list
ServeRunEvent      -> serve_run
```

对照 [analytic.py:L13-L17](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L13-L17) 验证：首字母不被插入下划线、每个大写字母前插一个、末尾 `_event` 被截掉。本实践可在本机直接跑通，无需 GPU 或模型仓库。

#### 4.2.5 小练习与答案

**练习 1**：如果新增一个类 `CleanAllEvent`，它的 `event_name` 是什么？会不会和别的类撞名？

> **参考答案**：`CleanAllEvent` → `Clean_All_Event` → `clean_all_event` → 截掉 `_event` → `clean_all`。只要两个类的「去后缀 snake_case 名」不同就不会撞名；但若同时存在 `CliEvent`（→`cli`）和 `CliCommandEvent`（→`cli_command`），二者不撞。需要注意的是，**事件名的唯一性要靠作者命名保证**，框架不做全局查重。

**练习 2**：为什么 `event_name` 用 `@property` 而不是类属性 `event_name = ...`？

> **参考答案**：因为它依赖 `self.__class__.__name__`，必须在**运行时**按实际实例的类来计算。若写成类属性，基类 `EventMeta` 上算一次就固定成 `event_meta` 了，子类拿不到自己的名字。`@property` 保证每次访问都基于当前 `self.__class__` 动态求值。

---

### 4.3 使用埋点与隐私开关

#### 4.3.1 概念说明

有了事件模型和装饰器劫持，最后一块拼图是：`wrapped` 函数到底在什么时机、用什么数据调用 `track`，以及用户如何**关掉**它。

`track` 来自 bentoml 内部：

```python
from bentoml._internal.utils.analytics import track
```

它是 BentoML 提供的事件上报函数，接收一个事件对象（这里是 `OpenllmCliEvent`）并负责把事件发送出去。OpenLLM 复用 bentoml 的上报通道，避免自己再造一套网络/序列化逻辑。

隐私方面，业界约定俗成用环境变量 `DO_NOT_TRACK`（本项目中即 `BENTOML_DO_NOT_TRACK`）作为「请勿追踪」的总开关。OpenLLM 提供了**两种**触发方式：

1. 直接设环境变量：`BENTOML_DO_NOT_TRACK=true openllm serve ...`
2. 用全局选项：`openllm --do-not-track serve ...`（该选项同时支持 `envvar=DO_NOT_TRACK`，即也能读环境变量）

#### 4.3.2 核心流程

`wrapped` 的运行流程可以分成「分流 → 计时 → 上报」三段：

```
wrapped(ctx, *args, **kwargs):
  读 do_not_track = (env BENTOML_DO_NOT_TRACK == 'true')   # 关键开关
  确定 command_name  = ctx.info_name                         # 如 'serve'
  确定 command_group:                                        # 两路分发
      若 ctx.parent.parent is not None  → ctx.parent.info_name   # 'openllm model list' → 'model'
      否则（顶层命令）                    → 'openllm'              # 'openllm run' → 'openllm'

  if do_not_track:
      return f(*args, **kwargs)        # 关闭追踪：直接跑，完全不碰 track

  start = time.time_ns()
  try:
      ret = f(*args, **kwargs)         # 执行真正的命令
      track( OpenllmCliEvent(group, name, duration=(now-start)/1e6) )   # 成功事件
      return ret
  except BaseException as e:
      track( OpenllmCliEvent(group, name, duration, error_type=type(e).__name__,
                             return_code=2 if KeyboardInterrupt else 1) )  # 失败事件
      raise                            # 重新抛出，绝不吞掉异常
```

关于 `command_group` 的两路分发，对照源码注释理解最直观：

- 嵌套子命令（`openllm model list`）：`ctx.parent.parent` 非空（父之上还有根），分组取 `ctx.parent.info_name`，即 `model`。
- 顶层命令（`openllm run`）：`ctx.parent.parent` 为空，分组取常量 `'openllm'`。

`do_not_track` 的判定只认字符串 `'true'`（不区分大小写）：

```
do_not_track = os.environ.get('BENTOML_DO_NOT_TRACK', 'False').lower() == 'true'
```

即只有 `BENTOML_DO_NOT_TRACK=true`（`True`/`TRUE` 亦可）才会关闭追踪；`1`、`yes`、`false` 等其它值都**不会**关闭。

#### 4.3.3 源码精读

先看 `wrapped` 的开关判定与命令分组：

[src/openllm/analytic.py:L62-L79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L62-L79) —— 第 65 行惰性导入 `track`（放函数内而非模块顶部，避免无 bentoml 时导入即崩）；第 67 行读 `DO_NOT_TRACK`；第 70–76 行用 `ctx` 推断 `command_name`/`command_group`；第 78–79 行是「关闭追踪就早退」分支。

再看成功/失败两条上报路径：

[src/openllm/analytic.py:L80-L101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L80-L101) —— `try` 成功则上报带 `duration_in_ms` 的事件（[L84-L88](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L84-L88)）；`except BaseException` 捕获**一切**异常，补上 `error_type=type(e).__name__` 与 `return_code`（Ctrl+C 为 `2`、其它为 `1`）后再 `raise`（[L90-L101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L90-L101)）。注意 `track` 也包在 try 内但**不在 except 的保护范围内**——理论上 `track` 自身抛错会绕过这条 except（因为 except 只捕 `f(*args)` 的错误），不过实践中 `track` 内部会吞掉自身错误。

最后看「隐私开关」在 `__main__.py` 的两个落点：

[src/openllm/__main__.py:L352-L357](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L352-L357) —— 全局回调 `typer_callback` 的 `--do-not-track` 选项，`envvar=DO_NOT_TRACK` 表示它也能从环境变量读初值。

[src/openllm/__main__.py:L367-L368](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L367-L368) —— 一旦该选项为真，就 `os.environ[DO_NOT_TRACK] = str(True)` 写回环境变量。这步至关重要：因为 `wrapped`（[analytic.py:L67](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67)）是**读环境变量**来决定是否追踪的，全局回调必须把「命令行选项」翻译成「环境变量」，命令层的埋点逻辑才能感知到。这是 OpenLLM 多处可见的「用环境变量跨层通信」手法。

#### 4.3.4 代码实践

**实践目标**：验证「`BENTOML_DO_NOT_TRACK=true` 时 `track` 不被调用」这一论断，并对照源码解释原因。

**操作步骤**：

1. 准备一个能观察到 `track` 是否被调用的办法。最简单的是借助 Python 的 `mock`，临时把 `bentoml._internal.utils.analytics.track` 替换成一个会打印的桩。新建如下**示例脚本** `spy_track.py`：

   ```python
   # 示例代码：观察 wrapped 是否调用 track
   import os, sys
   from unittest.mock import patch

   # 1) 先决定开/关追踪：去掉下一行注释即关闭追踪
   os.environ['BENTOML_DO_NOT_TRACK'] = 'true'

   import bentoml._internal.utils.analytics as an
   calls = []
   real = an.track
   def spy(event):
       calls.append(event)
       print('>> track called:', type(event).__name__,
             getattr(event, 'cmd_group', None), getattr(event, 'cmd_name', None))
   an.track = spy

   # 2) 直接驱动 OpenLLMTyper 的 command 包装逻辑：注册一条命令并调用
   from typer.testing import CliRunner
   from openllm.analytic import OpenLLMTyper
   app = OpenLLMTyper()
   @app.command()
   def hi(name: str = 'world'):
       print(f'hi {name}')
   CliRunner().invoke(app, ['hi', '--name', 'claude'])

   print('track was called?', len(calls) > 0, '| DO_NOT_TRACK =', os.environ.get('BENTOML_DO_NOT_TRACK'))
   ```

2. 在开发环境运行 `python spy_track.py`，记下输出；然后把第 5 行改成 `os.environ['BENTOML_DO_NOT_TRACK'] = 'false'`（或删掉）再跑一次，对比两次输出。

**需要观察的现象 / 预期结果**（待本地验证，取决于本机是否已装好 openllm + bentoml）：

- `BENTOML_DO_NOT_TRACK=true`：脚本打印 `track was called? False`，且看不到 `>> track called:`。
- `BENTOML_DO_NOT_TRACK=false`：脚本打印 `>> track called: OpenllmCliEvent ...` 以及 `track was called? True`。

**对照源码解释「为何 `true` 时不再触发 track」**：在 [analytic.py:L67-L79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67-L79)，`do_not_track` 为真时，`wrapped` 在第 78–79 行**直接 `return f(*args, **kwargs)`**，根本走不到第 80 行以后的 `start_time` 与 `track(...)`，所以 `track` 一定不会被调用。这是「早退短路」，而非「调了 track 但内部不发」。

> 若本机没有 GPU/模型、甚至没装好 bentoml，本实践依然有价值：`hi` 命令不依赖任何模型，只验证埋点机制本身。

#### 4.3.5 小练习与答案

**练习 1**：失败路径里用 `except BaseException` 而不是 `except Exception`，有什么好处和代价？

> **参考答案**：好处是连 `KeyboardInterrupt`（Ctrl+C，属 `BaseException` 不属 `Exception`）也能被统计到——这正是 `return_code` 里要专门判 `KeyboardInterrupt` 的原因。代价是连 `SystemExit` 之类也会被拦截上报；为此代码在 `except` 末尾**立即 `raise`** 重新抛出，保证不改变程序原本的退出行为（该崩还是崩、该退出还是退出），只是「顺便」记一笔。

**练习 2**：用户用 `openllm --do-not-track serve ...` 关闭追踪，这条信息是怎么传到 `wrapped` 里的？

> **参考答案**：`--do-not-track` 由 `typer_callback` 接收（[__main__.py:L355-L357](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L355-L357)），回调内执行 `os.environ[DO_NOT_TRACK] = str(True)`（[L367-L368](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L367-L368)），把命令行选项「翻译」成环境变量；随后 `wrapped` 在 [analytic.py:L67](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67) 读这个环境变量决定是否短路。环境变量是回调层与命令层之间约定的通信通道。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「为一条新命令加上可观测、可静默的埋点」小任务。

**任务**：给 OpenLLM 增加一条 `openllm ping` 命令，它随机延迟 0~1 秒后打印 `pong`，并要求：

1. 用 `@app.command` 注册，函数体内**不得**出现任何 `track` 调用——验证它自动获得埋点。
2. 能被 `BENTOML_DO_NOT_TRACK=true` 和 `openllm --do-not-track ping` 两种方式静默。
3. 人为制造一次失败（如抛 `RuntimeError`），对照 [analytic.py:L90-L101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L90-L101) 解释：失败事件里 `error_type` 应为 `'RuntimeError'`、`return_code` 应为 `1`；若用 Ctrl+C 中断，则 `error_type='KeyboardInterrupt'`、`return_code=2`。

**参考实现（示例代码）**：

```python
# 示例代码：放在 src/openllm/__main__.py 的 app 定义之后
import time
@app.command(help='ping pong, demo of analytics')
def ping(fail: bool = False) -> None:
    time.sleep(0.5)
    if fail:
        raise RuntimeError('intentional failure')
    output('pong')
```

**自检清单**：

- [ ] `openllm ping` 打印 `pong`，且（在未关闭追踪时）产生一次 `OpenllmCliEvent`，`cmd_group='openllm'`、`cmd_name='ping'`、`duration_in_ms>0`、`error_type=None`。
- [ ] `BENTOML_DO_NOT_TRACK=true openllm ping` 行为不变但不产生事件（命中 [analytic.py:L78-L79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L78-L79) 早退）。
- [ ] `openllm ping --fail` 仍会报错退出（`raise` 不吞异常），且产生带 `error_type='RuntimeError'` 的事件。

> 提示：观察事件最省事的方式是 4.3.4 里的 `spy` 桩，把 `an.track` 替换成会打印的函数，无需真正联网上报。

## 6. 本讲小结

- `OpenLLMTyper` 继承 `typer.Typer`，在 `__init__` 统一了 `-h` 别名、120 宽度、按定义顺序展示命令（`OrderedCommands`），并通过**重写 `command`** 实现「装饰器劫持」：所有 `@app.command` 自动裹上埋点，业务函数零侵入。
- `EventMeta.event_name` 用正则 `(?<!^)(?=[A-Z])` 把类名 CamelCase 转 snake_case，再去掉 `_event` 后缀；`OpenllmCliEvent` → `openllm_cli`。事件名随子类自动派生，永不与类脱节。
- `CliEvent` 携带 `cmd_group`/`cmd_name`/`duration_in_ms`/`error_type`/`return_code` 五项；`wrapped` 在成功与失败两条路径上分别上报，耗时由纳秒差除以 \(10^6\) 得毫秒。
- 失败路径用 `except BaseException` 连 Ctrl+C 一起统计（`return_code` 为 `1`/`2`），并在末尾 `raise` 保持原退出语义。
- 隐私开关 `BENTOML_DO_NOT_TRACK=true` 命中 `wrapped` 的早退分支，**完全不调用** `track`；全局选项 `--do-not-track` 则通过「写回环境变量」把意图传递给命令层，是 OpenLLM「跨层用环境变量通信」的又一例。
- 埋点最终复用 `bentoml._internal.utils.analytics.track` 上报，OpenLLM 不自建上报通道。

## 7. 下一步学习建议

- **横向对比另一处「跨层通信」**：本讲看到 `--do-not-track` → `os.environ` → `wrapped`；回顾 [u2-l1](u2-l1-common-config-output.md) 的 `VERBOSE_LEVEL` 与 [u1-l3](u1-l3-cli-entry-and-commands.md) 的全局回调，你会发现 OpenLLM 大量用「上下文变量 / 环境变量」让指挥层与执行层解耦，建议把这套模式总结成一页笔记。
- **顺藤摸瓜读 `track` 实现**：本讲到 `from bentoml._internal.utils.analytics import track` 为止。若想了解事件「如何序列化、发往何处、如何聚合」，可进入已安装的 bentoml 包里读 `bentoml/_internal/utils/analytics.py`（注意这是上游 BentoML 的内部 API，不在 OpenLLM 仓库内，版本不同细节可能差异）。
- **下一讲 [u3-l4 磁盘清理与缓存管理 clean.py](u3-l4-disk-cleanup.md)**：从「埋点/元信息」转向「磁盘实打实的占用」，读 `clean.py` 如何统计并清理 HF 模型缓存、venv、repos 三类缓存，其中的 `_du` 用 inode 去重统计真实占用的思路与本讲的「精确度量（耗时统计）」一脉相承。
