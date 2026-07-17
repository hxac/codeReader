# CLI 命令装饰器与使用分析 analytic.py

## 1. 本讲目标

本讲是专家层（u3）的第三篇，承接 [u1-l3](u1-l3-cli-entry-and-commands.md)。在 u1-l3 里你已经知道：`__main__.py` 里那个 `app` 不是普通的 `typer.Typer`，而是 `OpenLLMTyper`，它来自 `analytic.py`。本讲就来拆开 `analytic.py`（仅约 106 行），回答一个被搁置至今的问题：

> **为什么 OpenLLM 里每一条用 `@app.command` 注册的命令，都会"自动"带上使用统计（埋点）和计时？这层魔法是谁加的、怎么加的、能不能关掉？**

学完本讲你应该能够：

1. 理解 `OpenLLMTyper` 如何通过**重写 `command` 装饰器**（配合 `typing.TYPE_CHECKING` 保留原生类型提示），把每条命令的业务函数"包"进一层埋点外壳——即所谓"装饰器劫持（decorator hijacking）"技巧。
2. 看懂 `EventMeta.event_name` 如何用一条正则把类名（如 `OpenllmCliEvent`）转成事件名（如 `openllm_cli`），以及 `CliEvent` / `OpenllmCliEvent` 这两个事件模型记录了哪些字段。
3. 掌握 `track(...)` 埋点在成功 / 失败两条路径上分别记录什么，以及用 `--do-not-track` 或环境变量 `BENTOML_DO_NOT_TRACK` 关闭追踪的完整链路。

---

## 2. 前置知识

阅读本讲前，建议你已具备以下认知（不熟悉的术语这里一并解释）：

- **装饰器（decorator）**：Python 里 `@something` 本质是 `func = something(func)`。本讲的核心就是"重写一个装饰器工厂"：`OpenLLMTyper` 把 `command` 方法换成自己的版本，从而在"返回新函数"这一步偷偷加料。
- **`functools.wraps`**：让包装后的函数保留原函数的 `__name__`、`__doc__` 等元信息，Typer/Click 才能据此生成命令名与帮助文本——这是埋点能"透明"的关键之一。
- **Click 的 Context 对象**：Typer 底层是 Click。每条命令执行时 Click 都会构造一个 `click.Context`，记录"我是谁、父命令是谁、根命令是谁"。埋点正是从 `ctx` 里读出 `command_name` 与 `command_group`。
- **埋点（analytics / telemetry）**：程序主动上报自己的使用情况（执行了哪条命令、耗时多久、是否报错）用于改进产品；隐私上必须给用户提供**关闭开关**，这正是 `DO_NOT_TRACK` 的职责。
- **attrs（`@attr.define`）**：声明式地定义数据类的库，自动生成 `__init__`、`__repr__` 等，并支持带默认值的字段。OpenLLM 用它定义事件模型 `CliEvent`。
- **`time.time_ns()`**：返回当前时间的**纳秒**整数，比 `time.time()`（秒，浮点）精度高、无浮点误差，适合做耗时差值。

| 术语 | 通俗解释 |
| --- | --- |
| 埋点 | 在关键动作发生时偷偷记一条"事件"（谁、做了什么、花了多久、成功没），用于产品改进。 |
| 装饰器劫持 | 不修改业务函数，而是替换"注册命令用的那个装饰器"，从而让所有命令自动获得统一行为。 |
| `DO_NOT_TRACK` | 一个环境变量名，值为 `true` 时表示用户拒绝被追踪，OpenLLM 必须尊重它。 |

---

## 3. 本讲源码地图

本讲只涉及两个文件，它们正好构成"定义处 + 使用处"一对：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/openllm/analytic.py` | **定义处**。定义 `DO_NOT_TRACK` 常量、`EventMeta` / `CliEvent` / `OpenllmCliEvent`、`OrderedCommands`，以及最重要的 `OpenLLMTyper`（重写 `command` 实现透明埋点）。 | 几乎全部内容都要精读。 |
| `src/openllm/__main__.py` | **使用处**。`app = OpenLLMTyper(...)`，所有命令用 `@app.command` 注册；`typer_callback` 提供 `--do-not-track` 开关。 | 只看它如何"无感地"用上 `analytic.py`。 |

一句话定位：`analytic.py` 是"横切（cross-cutting）"的——它不属于任何一条具体业务命令，却悄悄作用于**每一条**命令。阅读时把这两个文件并排打开：`analytic.py` 给出"机制"，`__main__.py` 给出"机制被谁使用"。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按"骨架 → 灵魂 → 开关"的顺序讲：

- **4.1 OpenLLMTyper 与 command 装饰器**：骨架——埋点外壳是怎么装上去的。
- **4.2 事件元类与事件模型**：灵魂——埋点到底记录了什么、事件名怎么来。
- **4.3 使用埋点与隐私开关**：开关——埋点在什么时机触发、用户如何拒绝。

---

### 4.1 OpenLLMTyper 与 command 装饰器

#### 4.1.1 概念说明

OpenLLM 想同时做到两件**看似矛盾**的事：

1. 每一条命令（`hello / serve / run / deploy`，以及 `repo / model / clean` 子命令组里的每一条）都要自动上报使用埋点。
2. 命令的写法**完全不变**——作者只写一个普通函数并加 `@app.command`，不需要在函数体里手动塞 `track(...)`。

解决这对矛盾的手法就是**装饰器劫持**：自定义 `OpenLLMTyper` 继承 `typer.Typer`，并把它的 `command` 方法替换成自己的版本。自己的 `command` 仍把函数注册成 Typer 命令，但在注册前先用一个 `wrapped` 函数把原函数包起来——埋点逻辑全在 `wrapped` 里。于是所有 `@app.command` 都"免费"获得了埋点。源码注释也用了 *hijacking*（劫持）这个词。

> 关键直觉：装饰器劫持 = "我不改你的函数，我只改那个负责装饰你函数的工具"。控制权上移一层。这是把"横切关注点（cross-cutting concern）"统一处理的典型范式。

#### 4.1.2 核心流程

`OpenLLMTyper` 做两件事：① 在 `__init__` 里统一化 CLI 的外观与行为；② 重写 `command` 注入埋点。

`__init__` 的统一化用伪代码概括：

```
OpenLLMTyper.__init__:
  no_args_is_help      ← 默认 True（没给子命令时打印帮助）
  context_settings:
    help_option_names  ← 默认加上 ('-h','--help')，让 -h 也能触发帮助
    max_content_width  ← 取环境变量 COLUMNS，否则 120（控制帮助文本换行宽度）
  cls                  ← 默认 OrderedCommands（让命令按定义顺序、而非字母序展示）
  → 调用 typer.Typer.__init__ 完成真正初始化
```

`command` 的劫持流程：

```
@app.command 装饰某函数 f
  → 返回 decorator(f)
     → 定义 wrapped(ctx, ...):        # @functools.wraps(f) + @click.pass_context
         读 DO_NOT_TRACK → 若关闭追踪，直接 return f(...)
         确定 command_name / command_group（从 ctx 推断）
         记录 start_time
         try:
             返回值 = f(...)
             track( 成功事件(cmd_group, cmd_name, 耗时) )
             return 返回值
         except BaseException as e:
             track( 失败事件(cmd_group, cmd_name, 耗时, error_type, return_code) )
             raise                    # 重新抛出，绝不吞异常
     → return typer.Typer.command(self, ...)(wrapped)   # 把 wrapped 注册成真正的命令
```

要点是最后一步：劫持版 `command` **并没有自己实现"注册命令"**，而是把 `wrapped` 交给父类原始的 `typer.Typer.command` 去注册。所以 Typer 看到的命令签名、参数解析、帮助文本全部照旧，只是真正运行时跑的是 `wrapped`。

#### 4.1.3 源码精读

先看 `__init__` 的统一化：

- [src/openllm/analytic.py:L40-L52](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L40-L52)：`OpenLLMTyper.__init__`。用 `kwargs.pop` 取出并设置三件默认行为（`-h` 别名、120 宽度、`OrderedCommands`），再调父类 `__init__`。`pop` + 自定义默认值意味着调用方仍可显式覆盖。

再看 `OrderedCommands`，它解释了为什么 `--help` 里命令按源码顺序展示：

- [src/openllm/analytic.py:L35-L37](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L35-L37)：重写 Click `TyperGroup.list_commands` 为 `return list(self.commands)`——**按定义顺序**而非字母序展示命令。

接着是被劫持的 `command`，注意那个"只在类型检查时生效"的分支：

- [src/openllm/analytic.py:L54-L60](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L54-L60)：在 `typing.TYPE_CHECKING` 下把 `command` 指回 `typer.Typer.command`，用于**骗过 IDE/类型检查器**——让 `@app.command` 的类型提示保持和原生 Typer 一致（劫持不改变签名）。真正运行时（`TYPE_CHECKING` 为 `False`）走 `else` 里的自定义 `command`。

注册那一步是"劫持但不重造轮子"的精髓：

- [src/openllm/analytic.py:L60-L105](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L60-L105)：完整的 `command` 方法。第 62 行 `@functools.wraps(f)` 让 `wrapped` 冒充 `f`（Typer 才能正确生成帮助）；第 63 行 `@click.pass_context` 注入 Click 上下文；第 103 行 `return typer.Typer.command(self, *args, **kwargs)(wrapped)` 把包裹后的 `wrapped` 登记进 Typer。

使用处非常朴素——`__main__.py` 像用普通 Typer 一样用它，完全看不出埋点的存在：

- [src/openllm/__main__.py:L8](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L8)：`from openllm.analytic import DO_NOT_TRACK, OpenLLMTyper`——同时引入"骨架"和"开关常量"。
- [src/openllm/__main__.py:L19-L23](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L19-L23)：`app = OpenLLMTyper(help=...)`。
- [src/openllm/__main__.py:L206](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L206)：`@app.command` 修饰 `hello`，`serve`/`run`/`deploy` 同理。这些函数体内**没有任何** `track` 调用——埋点是装饰器偷偷加的。

#### 4.1.4 代码实践

**实践目标**：亲眼确认"埋点是 `app.command` 自动加的，而非业务函数主动调的"。

**操作步骤**：

1. 打开 `src/openllm/__main__.py`，用编辑器搜索 `track`，确认全文**没有**任何 `track(` 调用。
2. 再打开 `src/openllm/analytic.py`，定位到 [src/openllm/analytic.py:L65](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L65)，这里才是唯一的 `from bentoml._internal.utils.analytics import track`，且它在 `wrapped` **内部**才导入。
3. 在本地开发副本（按 [DEVELOPMENT.md](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/DEVELOPMENT.md) 用 `pip install -e .` 安装）里，于 `app` 定义之后新增一条最小命令（**示例代码，仅用于本地观察，请勿提交**）：

   ```python
   # 示例代码：验证装饰器劫持
   @app.command(help='echo for testing analytics')
   def echo(text: str = 'hi') -> None:
       output(f'echo: {text}')
   ```

4. 运行 `openllm echo hello`。

**需要观察的现象**：命令正常执行、打印 `echo: hello`，说明它已被 Typer 正确注册；该函数体内没有任何埋点代码，但只要 `BENTOML_DO_NOT_TRACK` 未设为 `true`，它就与 `serve`/`run` 走同一条 `wrapped` 路径。

**预期结果**：自定义命令与官方命令在"是否被埋点"上**一视同仁**——这正是装饰器劫持的威力：加命令即加埋点，零额外成本。**待本地验证**：若不想改源码，也可仅阅读 [src/openllm/analytic.py:L60-L105](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L60-L105) 确认 `wrapped` 对 `f` 完全无知，只依赖 `ctx` 与 `f` 的返回/异常，结论对任何被装饰函数都成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接在每个命令函数里手写 `track(...)`，而要搞一套装饰器劫持？

> **参考答案**：手动写要修改每一条命令（顶层 4 条外加三个子命令组里的若干条），易遗漏、易写错、且让业务函数混入埋点噪音。装饰器劫持把埋点集中在一处（`OpenLLMTyper.command`），新增命令自动获得埋点，删除/修改命令也无需关心埋点。

**练习 2**：如果把第 103 行改成 `return typer.Typer.command(self, *args, **kwargs)(f)`（直接登记原始 `f` 而非 `wrapped`），会发生什么？

> **参考答案**：命令仍能正常运行，但所有埋点都失效——因为再没有任何代码调用 `track`，也没有计时与 `do_not_track` 短路。这正是"劫持 `command`"的意义：把行为绑在"注册"这一步。

---

### 4.2 事件元类与事件模型

#### 4.2.1 概念说明

埋点要"记一条事件"，就必须回答两个问题：**这条事件叫什么名字？这条事件里装了哪些字段？** 在 OpenLLM 里，这两件事分别由 `EventMeta` 和 `CliEvent` / `OpenllmCliEvent` 负责。

设计意图很清晰：作者不想每次新增事件都手写字符串名字（容易拼错、难以统一）。于是把"事件名"从**类名自动派生**——你只要给事件类起个好名字，事件名就自动算出来。

- `EventMeta`：所有事件的基类。它不存数据，只提供一个派生属性 `event_name`——事件名**自动从类名推出**。
- `CliEvent`：描述"一条 CLI 命令的执行"，用 `@attr.define` 声明字段。
- `OpenllmCliEvent`：继承 `CliEvent`，是 OpenLLM 实际上报的具体事件类型（目前只是 `pass`）。"基类 + 空子类"的写法，目的是用**不同的类名**派生出**不同的事件名**，从而和 bentoml 自身的 `CliEvent`（事件名 `cli`）区分开。

> 说明：bentoml 内部如何根据事件名上报、发往哪个端点，属于 bentoml 的实现，本讲不展开；对 OpenLLM 而言，它只负责"造一个名字正确、字段齐全的事件对象，交给 `track`"。

#### 4.2.2 核心流程

事件名的派生分两步——CamelCase 转 snake_case，再去掉 `_event` 后缀。以 `OpenllmCliEvent` 为例：

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

两者都是"零宽"的，匹配不消耗字符，`re.sub` 只在这些位置插入 `_`。

事件字段方面，`CliEvent` 记录一次命令执行的五项信息：

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `cmd_group` | `str` | 必填 | 命令所属分组，顶层命令为 `'openllm'`，子命令为组名（如 `'model'`） |
| `cmd_name` | `str` | 必填 | 命令名（`serve` / `list` / `add` …） |
| `duration_in_ms` | `float` | `0` | 本次执行耗时（毫秒） |
| `error_type` | `Optional[str]` | `None` | 失败时异常类名（如 `KeyboardInterrupt`）；成功时为 `None` |
| `return_code` | `Optional[int]` | `None` | 失败时 `1`（普通异常）或 `2`（Ctrl+C）；成功时为 `None` |

耗时的单位换算是纯算术——纳秒除以 \(10^{6}\) 得毫秒：

\[
\text{duration\_in\_ms} = \frac{t_{\text{end, ns}} - t_{\text{start, ns}}}{10^{6}}
\]

#### 4.2.3 源码精读

- [src/openllm/analytic.py:L9-L18](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L9-L18)：`EventMeta` 与 `event_name` 属性。第 13 行用正则把类名转成 snake_case；第 16-17 行截掉 `_event` 后缀。注意它读的是 `self.__class__.__name__`，所以**子类**（如 `OpenllmCliEvent`）拿到的是自己的名字而非基类 `CliEvent` 的名字——这就是 `event_name` 必须用 `@property`（运行时按实例的类求值）而非类属性的原因。

- [src/openllm/analytic.py:L21-L27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L21-L27)：`CliEvent`，五个字段即上表。`@attr.define` 自动生成 `__init__`/`__repr__`，所以 `CliEvent(cmd_group='x', cmd_name='y')` 可直接构造，未传字段取默认值。

- [src/openllm/analytic.py:L30-L32](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L30-L32)：`OpenllmCliEvent(CliEvent)`，空体 `pass`。它的存在**仅为派生事件名 `openllm_cli`**，并和 bentoml 的 `CliEvent` 区分开。

#### 4.2.4 代码实践

**实践目标**：脱离 OpenLLM 运行环境，单独验证 `event_name` 的命名转换规则，确保你真正理解了那条正则。

**操作步骤**：在任意目录执行下面这段**示例代码**（纯标准库，无需安装 openllm）：

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

对照 [src/openllm/analytic.py:L13-L17](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L13-L17) 验证：首字母不被插入下划线、每个大写字母前插一个、末尾 `_event` 被截掉。本实践可在本机直接跑通，无需 GPU 或模型仓库。

#### 4.2.5 小练习与答案

**练习 1**：若新增一个类 `OpenllmRepoEvent(CliEvent): pass`，它的事件名是什么？

> **参考答案**：`openllm_repo`。推导：`OpenllmRepoEvent` → 插下划线 `Openllm_Repo_Event` → 小写 `openllm_repo_event` → 去掉 `_event` → `openllm_repo`。

**练习 2**：为什么不直接把事件名写成一个字符串常量，而要从类名派生？

> **参考答案**：从类名派生能保证"事件类 ↔ 事件名"一一对应、不会拼错，也方便后人通过类名一眼读出事件名；新增事件只需起个类名，零额外配置。代价是事件名的唯一性要靠作者命名保证（框架不做全局查重），但这正是 `EventMeta` 想强制的约定。

---

### 4.3 使用埋点与隐私开关

#### 4.3.1 概念说明

前两节造好了"骨架"（`wrapped`）和"灵魂"（事件对象），本节看它们如何在运行时协作，以及用户如何拒绝被追踪。

`track` 来自 bentoml 内部，在 `wrapped` 里**惰性导入**：

```python
from bentoml._internal.utils.analytics import track
```

它是 BentoML 提供的事件上报函数，接收一个事件对象（这里是 `OpenllmCliEvent`）并负责把事件发送出去。OpenLLM 复用 bentoml 的上报通道，避免自造一套网络/序列化逻辑。

隐私方面，OpenLLM 提供了**两种**触发"请勿追踪"的方式：

1. 直接设环境变量：`BENTOML_DO_NOT_TRACK=true openllm serve ...`
2. 用全局选项：`openllm --do-not-track serve ...`（该选项同时声明 `envvar=DO_NOT_TRACK`，即也能读环境变量）

两者最终都会让 `wrapped` 读到 `true`，从而跳过埋点。

#### 4.3.2 核心流程

`wrapped` 的执行流程（对应 [src/openllm/analytic.py:L64-L101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L64-L101)）可分"分流 → 计时 → 上报"三段：

```
wrapped(ctx, *args, **kwargs):
  读 do_not_track = (env BENTOML_DO_NOT_TRACK 的小写值 == 'true')   # 关键开关
  确定 command_name = ctx.info_name                                  # 如 'serve' / 'list'
  确定 command_group（两路分发）:
      若 ctx.parent.parent is not None  → ctx.parent.info_name      # 'openllm model list' → 'model'
      否则（顶层命令）                    → 'openllm'                 # 'openllm run' → 'openllm'

  if do_not_track:
      return f(*args, **kwargs)        # 关闭追踪：直接跑，完全不碰 track（早退短路）

  start = time.time_ns()
  try:
      ret = f(*args, **kwargs)         # 执行真正的命令
      track( OpenllmCliEvent(group, name, duration_in_ms=(now-start)/1e6) )   # 成功事件
      return ret
  except BaseException as e:
      track( OpenllmCliEvent(group, name, duration_in_ms=(now-start)/1e6,
                             error_type=type(e).__name__,
                             return_code=2 if isinstance(e, KeyboardInterrupt) else 1) )  # 失败事件
      raise                            # 重新抛出，绝不吞掉异常
```

**命令归属判断的细节**（本节最易踩坑处）。Click 把一条命令组织成一棵上下文树，`ctx` 是当前命令的上下文，`ctx.parent` 是上一层（命令组），`ctx.parent.parent` 是再上一层：

- `openllm run`（顶层命令）：树只有两层——根（app）→ `run`。`ctx.parent` 就是根，`ctx.parent.parent` 为 `None`，于是走 `elif`，`command_group='openllm'`。
- `openllm model list`（子命令）：树有三层——根（app）→ `model` 组 → `list`。`ctx.parent.parent` 是根（非 `None`），于是 `command_group = ctx.parent.info_name = 'model'`。

所以最终事件会记录：顶层命令归类为 `openllm`，子命令归类为各自所在的组（`repo` / `model` / `clean`）。

**`do_not_track` 的判定只认字符串 `'true'`**（不区分大小写）：

```
do_not_track = os.environ.get('BENTOML_DO_NOT_TRACK', 'False').lower() == 'true'
```

即只有 `BENTOML_DO_NOT_TRACK=true`（`True`/`TRUE` 亦可）才关闭追踪；`1`、`yes`、`false` 等其它值都**不会**关闭。

#### 4.3.3 源码精读

先看 `wrapped` 的开关判定与命令分组：

- [src/openllm/analytic.py:L62-L79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L62-L79)：第 65 行惰性导入 `track`（放函数内而非模块顶部，避免无 bentoml 时导入即崩）；第 67 行读 `DO_NOT_TRACK`；第 70-76 行用 `ctx` 推断 `command_name` / `command_group`；第 78-79 行是"关闭追踪就早退"分支——`do_not_track` 为真时直接 `return f(*args, **kwargs)`，**根本走不到**后面的 `start_time` 与 `track(...)`。

再看成功/失败两条上报路径：

- [src/openllm/analytic.py:L80-L101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L80-L101)：`try` 成功则上报带 `duration_in_ms` 的事件（[src/openllm/analytic.py:L84-L88](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L84-L88)）；`except BaseException`（[src/openllm/analytic.py:L90-L101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L90-L101)）捕获**一切**异常（连 `KeyboardInterrupt` 也记一条），补上 `error_type=type(e).__name__` 与 `return_code`（Ctrl+C 为 `2`、其它为 `1`），最后 `raise`。
  - 注意一个结构事实：成功路径的 `track`（第 84-88 行）**也在 `try` 块内**。这意味着如果 `track` 自身抛错，也会被同一条 `except BaseException` 捕获——此时会再用错误路径的 `track` 记录一个 `error_type` 事件再 `raise`。实践中 `track` 内部通常会吞掉自身错误，所以这条嵌套路径极少触发，但理解 try/except 的作用域有助于避免误判。

最后看"隐私开关"在 `__main__.py` 的两个落点：

- [src/openllm/__main__.py:L352-L368](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L352-L368)：全局回调 `typer_callback`。第 355-357 行声明 `--do-not-track` 选项，`envvar=DO_NOT_TRACK` 表示它也能从环境变量读初值。

- [src/openllm/__main__.py:L367-L368](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L367-L368)：一旦该选项为真，就 `os.environ[DO_NOT_TRACK] = str(True)` 写回环境变量。这步至关重要：因为 `wrapped`（[src/openllm/analytic.py:L67](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67)）是**读环境变量**决定是否追踪的，全局回调必须把"命令行选项"翻译成"环境变量"，命令层的埋点逻辑才能感知到。这是 OpenLLM 多处可见的"用环境变量跨层通信"手法。

#### 4.3.4 代码实践

**实践目标**：① 验证 `BENTOML_DO_NOT_TRACK=true` 时 `track` 不被调用；② 验证新加的 `@app.command` 会自动获得埋点。

**操作步骤**：准备一个能观察 `track` 是否被调用的脚本。借助 Python 直接替换 `bentoml._internal.utils.analytics.track` 为一个会打印的桩（**示例代码，仅用于本地观察**）：

```python
# 示例代码：观察 wrapped 是否调用 track
import os
# 1) 先决定开/关追踪：注释下一行即恢复追踪
os.environ['BENTOML_DO_NOT_TRACK'] = 'true'

import bentoml._internal.utils.analytics as an
calls = []
def spy(event):
    calls.append(event)
    print('>> track called:', type(event).__name__,
          getattr(event, 'cmd_group', None), getattr(event, 'cmd_name', None))
an.track = spy

# 2) 驱动 OpenLLMTyper 的 command 包装逻辑：注册一条命令并调用
from typer.testing import CliRunner
from openllm.analytic import OpenLLMTyper
app = OpenLLMTyper()

@app.command()
def hi(name: str = 'world'):
    print(f'hi {name}')

CliRunner().invoke(app, ['hi', '--name', 'claude'])
print('track was called?', len(calls) > 0,
      '| DO_NOT_TRACK =', os.environ.get('BENTOML_DO_NOT_TRACK'))
```

在开发环境运行，记下输出；然后把 `os.environ['BENTOML_DO_NOT_TRACK'] = 'true'` 改成 `'false'`（或删掉）再跑一次，对比两次输出。

**需要观察的现象 / 预期结果**（待本地验证，取决于本机是否已装好 openllm + bentoml）：

- `BENTOML_DO_NOT_TRACK=true`：脚本打印 `track was called? False`，且看不到 `>> track called:`。
- `BENTOML_DO_NOT_TRACK=false`：脚本打印 `>> track called: OpenllmCliEvent openllm hi` 以及 `track was called? True`——这条 `hi` 命令**没有任何手写埋点代码**，却仍被自动埋点。

**对照源码解释"为何 `true` 时不再触发 track"**：在 [src/openllm/analytic.py:L67-L79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67-L79)，`do_not_track` 为真时，`wrapped` 在第 78-79 行**直接 `return f(*args, **kwargs)`**，根本走不到第 80 行以后的 `start_time` 与 `track(...)`，所以 `track` 一定不会被调用。这是"早退短路"，而非"调了 track 但内部不发"。

> 若本机没装好 bentoml，本实践依然有价值：`hi` 命令不依赖任何模型，只验证埋点机制本身。

#### 4.3.5 小练习与答案

**练习 1**：失败路径里用 `except BaseException` 而不是 `except Exception`，有什么好处和代价？

> **参考答案**：好处是连 `KeyboardInterrupt`（Ctrl+C，属 `BaseException` 不属 `Exception`）也能被统计到——这正是 `return_code` 里要专门判 `KeyboardInterrupt` 的原因。代价是连 `SystemExit` 之类也会被拦截上报；为此代码在 `except` 末尾**立即 `raise`** 重新抛出，保证不改变程序原本的退出行为（该崩还是崩、该退出还是退出），只是"顺便"记一笔。

**练习 2**：用户用 `openllm --do-not-track serve ...` 关闭追踪，这条信息是怎么传到 `wrapped` 里的？

> **参考答案**：`--do-not-track` 由 `typer_callback` 接收（[src/openllm/__main__.py:L355-L357](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L355-L357)），回调内执行 `os.environ[DO_NOT_TRACK] = str(True)`（[src/openllm/__main__.py:L367-L368](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L367-L368)），把命令行选项"翻译"成环境变量；随后 `wrapped` 在 [src/openllm/analytic.py:L67](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67) 读这个环境变量决定是否短路。环境变量是回调层与命令层之间约定的通信通道。

**练习 3**：成功路径和失败路径上报的事件，字段上最大的区别是什么？

> **参考答案**：成功路径只填 `cmd_group / cmd_name / duration_in_ms`，`error_type` 与 `return_code` 取默认 `None`；失败路径额外填上 `error_type`（异常类名）和 `return_code`（`1` 或 `2`）。下游可据此区分"正常完成"与"出错退出"。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个"**给 OpenLLM 做一个最小可观测、可静默的埋点探针**"小任务。

**任务**：给 OpenLLM 增加一条 `openllm ping` 命令，它延迟 0.5 秒后打印 `pong`，并要求：

1. 用 `@app.command` 注册，函数体内**不得**出现任何 `track` 调用——验证它自动获得埋点。
2. 能被 `BENTOML_DO_NOT_TRACK=true openllm ping` 和 `openllm --do-not-track ping` 两种方式静默。
3. 人为制造一次失败（如抛 `RuntimeError`），对照 [src/openllm/analytic.py:L90-L101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L90-L101) 解释：失败事件里 `error_type` 应为 `'RuntimeError'`、`return_code` 应为 `1`；若用 Ctrl+C 中断，则 `error_type='KeyboardInterrupt'`、`return_code=2`。

**参考实现（示例代码，放在 `src/openllm/__main__.py` 的 `app` 定义之后，请勿提交）**：

```python
# 示例代码：综合实践
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
- [ ] `BENTOML_DO_NOT_TRACK=true openllm ping` 行为不变但不产生事件（命中 [src/openllm/analytic.py:L78-L79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L78-L79) 早退）。
- [ ] `openllm ping --fail` 仍会报错退出（`raise` 不吞异常），且产生带 `error_type='RuntimeError'`、`return_code=1` 的事件。

> 提示：观察事件最省事的方式是 4.3.4 里的 `spy` 桩，把 `an.track` 替换成会打印的函数，无需真正联网上报。**待本地验证**：具体命令能否成功执行取决于环境，但埋点是否触发由源码逻辑保证，与命令本身成败无关。

---

## 6. 本讲小结

- `OpenLLMTyper` 继承 `typer.Typer`，在 `__init__` 统一了 `-h` 别名、120 宽度、按定义顺序展示命令（`OrderedCommands`），并通过**重写 `command`** 实现"装饰器劫持"：所有 `@app.command` 自动裹上埋点，业务函数零侵入。
- 劫持版 `command` 用 `functools.wraps(f)` 冒充原函数、用 `@click.pass_context` 拿到 Click 上下文，最后把 `wrapped`（而非 `f`）交给 `typer.Typer.command` 登记——配合 `typing.TYPE_CHECKING` 保留原生类型提示。
- `EventMeta.event_name` 用正则 `(?<!^)(?=[A-Z])` 把类名 CamelCase 转 snake_case 再去掉 `_event` 后缀；`OpenllmCliEvent` → `openllm_cli`。事件名随子类自动派生，永不与类脱节。
- `CliEvent` 携带 `cmd_group` / `cmd_name` / `duration_in_ms` / `error_type` / `return_code` 五项；`wrapped` 在成功与失败两条路径上分别上报，耗时由纳秒差除以 \(10^{6}\) 得毫秒。
- 失败路径用 `except BaseException` 连 Ctrl+C 一起统计（`return_code` 为 `1`/`2`），并在末尾 `raise` 保持原退出语义；成功路径的 `track` 也在 `try` 内，理论上若 `track` 自身抛错会被同一条 except 捕获（实践中极少触发）。
- 隐私开关 `BENTOML_DO_NOT_TRACK=true` 命中 `wrapped` 的早退分支，**完全不调用** `track`；全局选项 `--do-not-track` 则通过"写回环境变量"把意图传递给命令层，是 OpenLLM"跨层用环境变量通信"的又一例。
- 埋点最终复用 `bentoml._internal.utils.analytics.track` 上报，OpenLLM 不自建上报通道。

---

## 7. 下一步学习建议

- **横向对比另一处"跨层通信"**：本讲看到 `--do-not-track` → `os.environ` → `wrapped`；回顾 [u2-l1](u2-l1-common-config-output.md) 的 `VERBOSE_LEVEL` 与 [u1-l3](u1-l3-cli-entry-and-commands.md) 的全局回调，你会发现 OpenLLM 大量用"上下文变量 / 环境变量"让指挥层与执行层解耦，建议把这套模式总结成一页笔记。
- **顺藤摸瓜读 `track` 实现**（可选进阶）：本讲到 `from bentoml._internal.utils.analytics import track` 为止。若想了解事件"如何序列化、发往何处、如何聚合"，可进入已安装的 bentoml 包读 `bentoml/_internal/utils/analytics.py`——但这是上游 BentoML 的内部 API，不在 OpenLLM 仓库内，版本不同细节可能有差异。
- **下一讲 [u3-l4 磁盘清理与缓存管理 clean.py](u3-l4-disk-cleanup.md)**：从"埋点/元信息"转向"磁盘实打实的占用"，读 `clean.py` 如何统计并清理 HF 模型缓存、venv、repos 三类缓存，其中 `_du` 用 inode 去重统计真实占用的思路，与本讲"精确度量（耗时统计）"一脉相承。
- **二次开发实践 [u3-l5](u3-l5-custom-repo-and-bento.md)**：当你准备自定义仓库与 Bento 时，回想本讲的教训——**所有新命令只要用 `@app.command` 就自动合规（含埋点与隐私开关）**，这是 OpenLLM 留给二次开发者的一个"安全默认"。
