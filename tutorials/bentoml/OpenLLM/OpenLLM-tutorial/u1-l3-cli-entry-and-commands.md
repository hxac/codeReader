# CLI 入口与命令体系

## 1. 本讲目标

本讲是入门层的第三篇。前两讲我们已经知道 OpenLLM 是什么、怎么安装、源码目录如何组织。这一篇我们要回答一个更具体的问题：

> 当你在终端敲下 `openllm serve llama3.2:1b` 并回车，这行命令到底「落」在了源码里的哪个函数？

学完本讲你应该能够：

- 看懂 `src/openllm/__main__.py` 里 `app` 是如何被组装出来的，以及 `repo`/`model`/`clean` 这三个子命令组是怎么挂上去的。
- 认识 `hello`/`serve`/`run`/`deploy` 四个顶层命令各自的参数和职责，能从函数签名读懂一条命令支持哪些选项。
- 理解 `@app.callback` 全局回调如何控制 `VERBOSE_LEVEL`（详细度）、`--version`、`--do-not-track`（使用追踪开关）这三个全局行为。
- 顺带理解 `app` 为什么用的是 `OpenLLMTyper` 而不是普通的 `typer.Typer`，以及它和「使用埋点」的关系（深入版放在 u3-l3）。

## 2. 前置知识

本讲会用到几个概念，先做通俗铺垫。

- **CLI（命令行接口）**：在终端里以文字形式输入命令、由程序解析执行的方式。`openllm` 就是一个 CLI 程序。
- **Typer**：一个 Python 库，你只要写普通的带类型注解的函数，它就能自动帮你生成命令行参数解析逻辑和 `--help` 文档。OpenLLM 的整个命令体系都建立在 Typer 之上。
- **Click**：Typer 的底层依赖。Typer 其实是对 Click 的一层封装，所以你会看到 OpenLLM 里偶尔直接用到 `click.Context`、`click.pass_context` 这类底层对象。
- **子命令（subcommand）与子命令组**：像 `openllm repo list` 里，`repo` 是子命令组，`list` 是它下面的子命令。这种「主命令 + 组 + 子命令」的结构，和 `git remote add`、`docker container ls` 是同一套思路。
- **回调（callback）**：在 Typer 里，回调是「每次调用都会先执行」的一段函数。OpenLLM 用它来处理 `--verbose`、`--version` 这种不属于任何具体子命令、但对所有命令都生效的全局选项。
- **ContextVar（上下文变量）**：Python 标准库提供的一种「按调用上下文存取」的变量，可以理解为「带作用域的全局变量」。OpenLLM 用它保存当前命令的详细度级别（`VERBOSE_LEVEL`）和是否处于交互模式（`INTERACTIVE`）。这两个变量定义在 `common.py`，u2-l1 会深入讲解，本讲只需知道它们的存在与作用。

> 本讲承接 u1-l2：你已经知道 `openllm` 这个控制台脚本入口指向 `openllm.__main__:app`，本讲就带你走进这个 `app` 内部。

## 3. 本讲源码地图

本讲只涉及两个核心源码文件：

| 文件 | 行数级别 | 作用 |
| --- | --- | --- |
| [src/openllm/__main__.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py) | ~370 行 | CLI 总入口。在这里创建顶层 `app`，注册子命令组，定义 `hello/serve/run/deploy` 四个顶层命令，以及全局回调。 |
| [src/openllm/analytic.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py) | ~106 行 | 定义 `OpenLLMTyper`（`app` 的真正类型）以及使用埋点相关的事件类和 `DO_NOT_TRACK` 开关。 |

此外，`__main__.py` 顶部从若干模块 import 了子命令的 `app` 和工具函数，这些模块本身不是本讲重点，但你需要知道它们的存在：

- `repo_app`、`model_app`、`clean_app` 分别来自 `repo.py`、`model.py`、`clean.py`，它们各自也是一个 `OpenLLMTyper` 实例（见 [src/openllm/repo.py:21](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L21)、[src/openllm/model.py:12](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L12)、[src/openllm/clean.py:9](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L9)）。
- `local_serve`/`local_run` 来自 `local.py`，`cloud_deploy`/`get_cloud_machine_spec` 来自 `cloud.py`，`ensure_bento`/`list_bento` 来自 `model.py`，`can_run`/`get_local_machine_spec` 来自 `accelerator_spec.py`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. Typer 应用与子命令注册（`app` 是怎么搭起来的）
2. 顶层命令签名与含义（`hello/serve/run/deploy`）
3. 全局回调与公共选项（`--verbose/--version/--do-not-track`）

### 4.1 Typer 应用与子命令注册

#### 4.1.1 概念说明

一个 Typer 程序的「骨架」非常简单：创建一个 `Typer` 实例（OpenLLM 里叫 `app`），然后用装饰器 `@app.command()` 把普通函数注册成命令。当脚本运行时，调用 `app()` 就会让 Typer 解析 `sys.argv`、找到对应函数并执行。

OpenLLM 做了一件稍微特殊的事：它没有直接用 `typer.Typer`，而是用自己的子类 `OpenLLMTyper`。这个子类除了提供默认帮助宽度和 `-h` 别名外，最重要的功能是「重写 `command` 装饰器」，让每条命令在执行前后自动上报一次使用埋点。这个埋点机制的细节放在 u3-l3，本讲你只需要记住：**`app` 的类型是 `OpenLLMTyper`，它给每条命令自动裹了一层「埋点 + 计时」逻辑**。

#### 4.1.2 核心流程

`app` 的组装流程可以画成：

```text
创建 app = OpenLLMTyper(help=...)      # 顶层应用
   │
   ├── app.add_typer(repo_app, name='repo')    # 挂载子命令组
   ├── app.add_typer(model_app, name='model')
   ├── app.add_typer(clean_app, name='clean')
   │
   ├── @app.command  def hello(...)            # 注册顶层命令
   ├── @app.command  def serve(...)
   ├── @app.command  def run(...)
   ├── @app.command  def deploy(...)
   │
   └── @app.callback def typer_callback(...)   # 全局回调

if __name__ == '__main__':
    app()                                       # 解析 argv 并执行
```

关键点：

- `add_typer` 是 Typer 提供的「把另一个 Typer 应用当作子命令组挂上来」的方法。被挂上来的 `repo_app`/`model_app`/`clean_app` 各自又是一个独立的 `OpenLLMTyper`，它们内部再用 `@xxx_app.command` 注册自己的子命令（比如 `repo list`、`model get`）。
- 顶层命令则直接用 `@app.command` 装饰普通函数，函数名就是命令名（`hello` 函数 → `openllm hello` 命令）。
- 文件末尾的 `if __name__ == '__main__': app()` 是直接 `python -m openllm` 运行时的入口；而 `pip install` 后生成的 `openllm` 控制台脚本则直接指向 `app` 对象（见 u1-l2 讲过的 `[project.scripts]`）。

#### 4.1.3 源码精读

创建顶层应用，并附带帮助文案（注意 `app` 的类型是 `OpenLLMTyper`）：

[创建顶层 app — src/openllm/__main__.py:19-23](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L19-L23)

```python
app = OpenLLMTyper(
  help='`openllm hello` to get started. '
  'OpenLLM is a CLI tool to manage and deploy open source LLMs and'
  ' get an OpenAI API compatible chat server in seconds.'
)
```

挂载三个子命令组：

[add_typer 注册子命令组 — src/openllm/__main__.py:25-27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L25-L27)

```python
app.add_typer(repo_app, name='repo')
app.add_typer(model_app, name='model')
app.add_typer(clean_app, name='clean')
```

来看 `OpenLLMTyper` 本身。它在 `__init__` 里设置了几个对 CLI 体验很重要的默认值：无参数时显示帮助（`no_args_is_help=True`）、`-h` 作为 `--help` 的别名、帮助文本最大宽度 120（可被环境变量 `COLUMNS` 覆盖），并把 Click 的分组类替换成 `OrderedCommands`（保持命令按定义顺序而非字母序展示）：

[OpenLLMTyper 构造 — src/openllm/analytic.py:40-52](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L40-L52)

```python
class OpenLLMTyper(typer.Typer):
  def __init__(self, *args: typing.Any, **kwargs: typing.Any):
    no_args_is_help: bool = kwargs.pop('no_args_is_help', True)
    context_settings: dict[str, typing.Any] = kwargs.pop('context_settings', {})
    if 'help_option_names' not in context_settings:
      context_settings['help_option_names'] = ('-h', '--help')
    if 'max_content_width' not in context_settings:
      context_settings['max_content_width'] = int(os.environ.get('COLUMNS', str(120)))
    klass = kwargs.pop('cls', OrderedCommands)
    super().__init__(
      *args, cls=klass, no_args_is_help=no_args_is_help, context_settings=context_settings, **kwargs
    )
```

`OrderedCommands` 继承自 Click 的 `TyperGroup`，覆盖 `list_commands` 让命令按注册顺序输出，这样你看到 `openllm --help` 时命令顺序和源码定义顺序一致：

[OrderedCommands 保持命令顺序 — src/openllm/analytic.py:35-37](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L35-L37)

```python
class OrderedCommands(typer.core.TyperGroup):
  def list_commands(self, ctx: click.Context) -> list[str]:
    return list(self.commands)
```

最后是脚本运行入口。直接 `python -m openllm` 时走到这里，调用 `app()` 触发 Typer 解析：

[脚本入口 — src/openllm/__main__.py:371-372](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L371-L372)

```python
if __name__ == '__main__':
  app()
```

#### 4.1.4 代码实践

**实践目标**：验证「命令树」与源码结构一一对应。

**操作步骤**：

1. 确认 `openllm` 已安装（见 u1-l2）。
2. 运行 `openllm --help`，观察 Commands 一栏列出的命令。
3. 对照 [src/openllm/__main__.py:25-27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L25-L27) 确认 `repo`、`model`、`clean` 正是 `add_typer` 挂上去的三个组。
4. 运行 `python -m openllm --help`，观察输出是否与 `openllm --help` 一致——这验证了 `if __name__ == '__main__': app()` 这条入口的作用。

**需要观察的现象**：`--help` 输出里的命令顺序应该和源码里定义的顺序一致（先 `hello`、`serve`、`run`、`deploy`，再 `repo`、`model`、`clean`），而不是按字母排序。这是 `OrderedCommands` 的效果。

**预期结果**：你能把帮助文本里的每个命令名，在 `__main__.py` 里找到对应的 `@app.command` 函数或 `add_typer` 调用。

> 如果本地未安装，可只做源码阅读：在 `__main__.py` 顶部 import 处，把 `repo_app`/`model_app`/`clean_app` 的来源和 `add_typer` 三行手动对应起来即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 OpenLLM 用自己的 `OpenLLMTyper` 而不是直接用 `typer.Typer`？

**参考答案**：因为 `OpenLLMTyper` 重写了 `command` 装饰器，给每条命令自动包了一层「使用埋点 + 耗时统计」的逻辑（见 u3-l3），同时在构造时统一设置了 `-h` 别名、帮助宽度 120、按定义顺序展示命令等默认值。这些都是普通 `typer.Typer` 不会自动提供的。

**练习 2**：如果把 `app.add_typer(clean_app, name='clean')` 这一行删掉，重新安装运行，`openllm clean --help` 会发生什么？

**参考答案**：Typer 不再认识 `clean` 这个子命令组，运行 `openllm clean --help` 会报错或提示「No such command 'clean'」，因为顶层 `app` 没有挂载它（除非 `no_args_is_help` 把它兜底成帮助输出）。

---

### 4.2 顶层命令签名与含义

#### 4.2.1 概念说明

顶层命令指的是直接挂在 `app` 上、不带子命令组的命令，也就是你用得最多的四个：`hello`、`serve`、`run`、`deploy`。它们共同承担「把一个开源大模型变成可用的对话服务」这条主链路的不同阶段：

- `hello`：交互式引导，适合第一次上手。它会探测硬件、列出可运行模型、让你选版本和动作。
- `serve`：在本地起一个 OpenAI 兼容的 HTTP 服务（带浏览器 Chat UI）。
- `run`：在本地起服务后，直接在终端里进行流式多轮对话（不起浏览器）。
- `deploy`：把模型部署到 BentoCloud，得到一个可扩展的远端服务。

这四个命令在源码里都是「瘦」函数：它们只做参数收集、调用 `cmd_update()` 更新仓库、调用 `ensure_bento()` 把模型名解析成 Bento，然后把真正的活儿交给 `local_serve`/`local_run`/`cloud_deploy`。换句话说，**`__main__.py` 是指挥层，重活在 `local.py`/`cloud.py` 里**（u3-l1、u3-l2 会讲）。

#### 4.2.2 核心流程

四个顶层命令的共同骨架：

```text
@app.command
def xxx(model, repo, verbose, env, arg, ...):
    cmd_update()                       # 确保模型仓库是最新的
    if verbose: VERBOSE_LEVEL.set(20)  # 打开详细输出
    target = get_local_machine_spec()  # 探测本地硬件
    bento = ensure_bento(model, ...)   # 模型名 → BentoInfo
    local_serve / local_run / cloud_deploy(bento, ...)  # 真正执行
```

几个值得注意的「公共选项」：

| 选项 | 含义 | 出现的命令 |
| --- | --- | --- |
| `model`（位置参数） | 要运行的模型名，如 `llama3.2:1b` | `serve` 必填；`run`/`deploy` 可空 |
| `--repo` | 指定从哪个模型仓库查找 | 全部 |
| `--env` | 透传环境变量（`NAME` 或 `NAME=value`，可多次） | 全部 |
| `--arg` | 透传给 Bento 的 `key=value` 参数，可多次 | 全部 |
| `--verbose` | 把 `VERBOSE_LEVEL` 设为 20，打印更多日志 | `serve`/`run`/`deploy` |
| `--port` | 本地服务端口 | `serve`(默认 3000)、`run`(随机) |
| `--context` | BentoCloud 上下文名 | `deploy`、`hello` |

#### 4.2.3 源码精读

先看最简单的 `serve`，它最能代表「顶层命令 = 收集参数 + 调用下层」这一模式：

[serve 命令 — src/openllm/__main__.py:246-268](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L246-L268)

```python
@app.command(help='start an OpenAI API compatible chat server and chat in browser')
def serve(
  model: typing.Annotated[str, typer.Argument()],
  repo: typing.Optional[str] = None,
  port: int = 3000,
  verbose: bool = False,
  env: typing.Optional[list[str]] = typer.Option(None, '--env', help='...'),
  arg: typing.Optional[list[str]] = typer.Option(None, '--arg', help='...'),
) -> None:
  cmd_update()
  if verbose:
    VERBOSE_LEVEL.set(20)
  target = get_local_machine_spec()
  bento = ensure_bento(model, target=target, repo_name=repo)
  local_serve(bento, port=port, cli_envs=env, cli_args=arg)
```

要点解读：

- `model: typing.Annotated[str, typer.Argument()]`：`model` 是必填的位置参数（不带默认值），所以 `openllm serve` 不给模型名会直接报错。
- `port: int = 3000`：普通带默认值的参数会被 Typer 当成 `--port` 选项，默认 3000。
- `verbose: bool = False`：布尔型参数会变成 `--verbose` 开关。当它为真时，把全局 `VERBOSE_LEVEL` 设成 20（即 `logging.INFO` 级别），后续 `output(...)` 会打印更详细的信息。
- `env`/`arg` 用 `typer.Option(None, '--env', ...)` 显式声明成长选项，且因为类型是 `list[str]`，可以在命令行里重复指定（如 `--env A --env B`）。

`run` 与 `serve` 几乎对称，区别是它面向终端对话，端口默认随机分配，并多了一个 `--timeout`：

[run 命令 — src/openllm/__main__.py:271-296](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L271-L296)

```python
def run(
  model: typing.Annotated[str, typer.Argument()] = '',
  repo: typing.Optional[str] = None,
  port: typing.Optional[int] = None,
  timeout: int = 600,
  verbose: bool = False,
  env: ...,
  arg: ...,
) -> None:
  cmd_update()
  if verbose:
    VERBOSE_LEVEL.set(20)
  target = get_local_machine_spec()
  bento = ensure_bento(model, target=target, repo_name=repo)
  if port is None:
    port = random.randint(30000, 40000)
  local_run(bento, port=port, timeout=timeout, cli_envs=env, cli_args=arg)
```

注意 `model` 这里默认值是空串 `''`（可缺省），`port` 默认 `None` 时会在 30000–40000 之间随机取一个端口，避免和本机其他服务冲突。

`deploy` 比 `serve`/`run` 多了「选择云端实例类型」的逻辑：如果用户显式给了 `--instance-type`，直接用它；否则拉取云端可用实例列表，过滤出能跑该 Bento 的，挑分数最高的，或在交互模式下让用户选：

[deploy 命令（实例选择片段） — src/openllm/__main__.py:299-349](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L319-L349)

```python
  cmd_update()
  if verbose:
    VERBOSE_LEVEL.set(20)
  bento = ensure_bento(model, repo_name=repo)
  if instance_type is not None:
    return cloud_deploy(bento, DeploymentTarget(accelerators=[], name=instance_type), ...)
  targets = get_cloud_machine_spec(context=context)
  runnable_targets = sorted(
    filter(lambda x: can_run(bento, x) > 0, targets), key=lambda x: can_run(bento, x), reverse=True
  )
  if not runnable_targets:
    output('No available instance type, check your bentocloud account', style='red')
    raise typer.Exit(1)
  if INTERACTIVE.get() and instance_type is None:
    target = _select_target(bento, targets)
  else:
    target = runnable_targets[0]
    output(f'Recommended instance type: {target.name}', style='green')
  cloud_deploy(bento, target, cli_envs=env, context=context, cli_args=arg, interactive=INTERACTIVE.get())
```

`hello` 是「引导版」，它不接收 `model`，而是通过 `questionary` 交互式地选模型、选版本、选动作，最后还是落到 `run/serve/deploy` 三者之一：

[hello 命令 — src/openllm/__main__.py:206-243](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L206-L243)

```python
def hello(repo=None, envs=..., arg=..., context=...) -> None:
  cmd_update()
  INTERACTIVE.set(True)
  target = get_local_machine_spec()
  output(f'  Detected Platform: {target.platform}', style='green')
  ...
  models = list_bento(repo_name=repo)
  ...
  bento_name, repo = _select_bento_name(models, target)
  bento, score = _select_bento_version(models, target, bento_name, repo)
  _select_action(bento, score, context=context, envs=envs, arg=arg, interactive=INTERACTIVE.get())
```

注意 `hello` 在一开始就 `INTERACTIVE.set(True)`，这个上下文变量会一路传递给后续的 `cloud_deploy(..., interactive=INTERACTIVE.get())`，决定部署时是否走交互式选择。

#### 4.2.4 代码实践

**实践目标**：把帮助文本里的每条命令、每个选项，映射到源码里的函数与参数。

**操作步骤**：

1. 运行 `openllm serve --help`，记录下列出的所有参数（`Arguments` 与 `Options`）。
2. 打开 [src/openllm/__main__.py:246-268](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L246-L268) 的 `serve` 函数签名，把帮助文本里每个参数名，对应到函数里的一个形参。
3. 同样地运行 `openllm run --help`、`openllm deploy --help`、`openllm hello --help`，分别对照 `run`/`deploy`/`hello` 函数。
4. 运行 `openllm repo --help`、`openllm model --help`、`openllm clean --help`，确认它们是 `add_typer` 挂上来的子命令组（这些命令的定义不在 `__main__.py`，而在 `repo.py`/`model.py`/`clean.py`）。
5. 把以上结果整理成一棵命令树（见下方「预期结果」）。

**需要观察的现象**：Typer 帮助文本里的参数顺序、是否必填、默认值，应当与函数签名完全一致。例如 `serve` 的 `model` 在帮助里标为必填 argument，`run` 的 `model` 则可选（因为默认值是 `''`）。

**预期结果**：画出如下命令树（仅示意，请按实际帮助文本补全选项）：

```text
openllm
├── hello        # 交互引导
├── serve MODEL  # 本地起 HTTP 服务，默认端口 3000
├── run [MODEL]  # 终端对话，端口随机
├── deploy [MODEL]  # 部署到 BentoCloud
├── repo ...     # 子命令组（list/add/remove/update/default）—— 定义在 repo.py
├── model ...    # 子命令组（list/get）—— 定义在 model.py
└── clean ...    # 子命令组（model_cache/venvs/repos/configs/all）—— 定义在 clean.py
```

> 若本地暂未安装 `openllm`，可改为纯源码阅读：依次打开四个 `@app.command` 函数，把每个形参记成一行「参数名 / 类型 / 默认值 / 含义」的表格。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `serve` 的 `model` 是 `typing.Annotated[str, typer.Argument()]`（无默认值），而 `run`/`deploy` 的 `model` 默认值是 `''`？

**参考答案**：`serve` 必须知道要起哪个模型才能提供服务，所以设为必填位置参数；`run`/`deploy` 允许缺省模型名（例如配合交互式选择，或在 `hello` 流程里由用户后续选出），因此给了空串默认值，使其成为可选参数。

**练习 2**：`serve`、`run`、`deploy` 三个命令都先调用了 `cmd_update()` 和 `ensure_bento(...)`。请说明这两步分别解决什么问题。

**参考答案**：`cmd_update()`（来自 `repo.py`）确保本地的模型仓库目录是最新的，避免列出过期的模型清单；`ensure_bento(...)`（来自 `model.py`）把用户传入的模型名（可能带别名）解析成一个具体的 `BentoInfo` 对象，后续 `local_serve`/`local_run`/`cloud_deploy` 都基于这个 `BentoInfo` 工作。

---

### 4.3 全局回调与公共选项

#### 4.3.1 概念说明

除了各命令自己的参数，OpenLLM 还有三个「全局选项」：`--verbose`、`--version`、`--do-not-track`。它们不属于任何具体子命令，而是对整条 `openllm` 调用都生效。

Typer 用「回调」来实现这种全局选项。给 `app` 加一个 `@app.callback` 装饰的函数，它会在每次运行（且在任何具体命令之前）被调用一次。OpenLLM 把 `invoke_without_command=True` 打开，意味着即使你只敲 `openllm` 不带任何子命令，回调也会执行——这就是为什么 `openllm --version` 能直接打印版本号然后退出。

这三个全局选项分别控制：

- `--verbose N`：把全局上下文变量 `VERBOSE_LEVEL` 设为 `N`，控制 `output()` 打印多少日志（值越大越详细，命令内的 `--verbose` 开关等价于设成 20）。
- `--version` / `-v`：打印 `openllm` 版本号和 Python 实现信息，然后 `sys.exit(0)` 直接退出。
- `--do-not-track`：关闭使用埋点。它对应的环境变量名是 `BENTOML_DO_NOT_TRACK`（定义在 `analytic.py`），既可以从命令行传，也可以从环境变量读。

#### 4.3.2 核心流程

```text
用户敲入: openllm --verbose 20 serve llama3.2:1b
        │
        ▼
Typer 先解析全局选项 → 调用 typer_callback(verbose=20, ...)
        │   ├── verbose 非 0 → VERBOSE_LEVEL.set(20)
        │   ├── --version?    → 打印版本并退出
        │   └── --do-not-track? → os.environ[BENTOML_DO_NOT_TRACK] = 'True'
        ▼
Typer 再解析子命令 → 调用 serve(model='llama3.2:1b', verbose=False, ...)
        │   （serve 体内的 --verbose 是命令级开关，与全局 --verbose 互不冲突）
        ▼
真正执行服务化逻辑
```

埋点侧的联动：`--do-not-track` 把环境变量 `BENTOML_DO_NOT_TRACK` 设成 `'True'`，而 `OpenLLMTyper.command` 里的包装函数每次都会读取这个环境变量（[src/openllm/analytic.py:67](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67)），一旦为真就直接跳过埋点上报。这就是「回调里改环境变量 → 命令包装层读环境变量」的协作方式。

#### 4.3.3 源码精读

全局回调本体：

[typer_callback — src/openllm/__main__.py:352-368](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L352-L368)

```python
@app.callback(invoke_without_command=True)
def typer_callback(
  verbose: int = 0,
  do_not_track: bool = typer.Option(
    False, '--do-not-track', help='Whether to disable usage tracking', envvar=DO_NOT_TRACK
  ),
  version: bool = typer.Option(False, '--version', '-v', help='Show version'),
) -> None:
  if verbose:
    VERBOSE_LEVEL.set(verbose)
  if version:
    output(
      f'openllm, {importlib.metadata.version("openllm")}\nPython ({platform.python_implementation()}) {platform.python_version()}'
    )
    sys.exit(0)
  if do_not_track:
    os.environ[DO_NOT_TRACK] = str(True)
```

要点：

- `invoke_without_command=True`：允许「光跑回调、不跑任何子命令」，这是 `openllm --version`、`openllm -v` 能工作的前提。
- `verbose: int = 0`：注意它是整数（`openllm --verbose 20 ...`），与命令级的布尔 `--verbose` 不同名同义但各自独立。
- `do_not_track` 用 `envvar=DO_NOT_TRACK` 绑定环境变量，意味着 `BENTOML_DO_NOT_TRACK=true openllm serve ...` 和 `openllm --do-not-track serve ...` 效果相同。
- `--version` 用 `importlib.metadata.version("openllm")` 读已安装包的版本——这呼应了 u1-l2 讲过的「版本来自包元数据而非构建期生成的 `_version.py`」。
- `--version` 命中后调用 `sys.exit(0)`，直接终止程序，不会再往下走任何子命令。

`DO_NOT_TRACK` 这个常量字符串就定义在 `analytic.py` 顶部：

[DO_NOT_TRACK 常量 — src/openllm/analytic.py:6](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L6)

```python
DO_NOT_TRACK = 'BENTOML_DO_NOT_TRACK'
```

埋点包装层如何消费这个环境变量（这是「全局回调 → 命令执行」的衔接点，细节留到 u3-l3）：

[command 包装读取 do_not_track — src/openllm/analytic.py:60-89](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L60-L89)

```python
def command(self, *args: typing.Any, **kwargs: typing.Any):
  def decorator(f):
    @functools.wraps(f)
    @click.pass_context
    def wrapped(ctx: click.Context, *args, **kwargs):
      from bentoml._internal.utils.analytics import track
      do_not_track = os.environ.get(DO_NOT_TRACK, str(False)).lower() == 'true'
      ...
      if do_not_track:
        return f(*args, **kwargs)      # 关闭埋点，直接执行原函数
      start_time = time.time_ns()
      try:
        return_value = f(*args, **kwargs)
        duration_in_ns = time.time_ns() - start_time
        track(OpenllmCliEvent(cmd_group=..., cmd_name=..., duration_in_ms=duration_in_ns / 1e6))
        return return_value
      except BaseException as e:
        ...
        raise
    return typer.Typer.command(self, *args, **kwargs)(wrapped)
  return decorator
```

可以看到，埋点把耗时从纳秒换算成毫秒：`duration_in_ms = duration_in_ns / 1e6`，即

\[
\text{duration\_in\_ms} = \frac{\text{duration\_in\_ns}}{10^{6}}
\]

并且只要环境变量 `BENTOML_DO_NOT_TRACK` 为 `true`，就完全跳过 `track(...)`。这就解释了为什么在回调里设 `os.environ[DO_NOT_TRACK] = str(True)` 能立刻对随后执行的命令生效。

> 关于 `OpenllmCliEvent`、`EventMeta` 如何把类名转成事件名、埋点上报了哪些字段，这些是 u3-l3 的主题，本讲暂不展开。

#### 4.3.4 代码实践

**实践目标**：亲手验证三个全局选项的行为，并把它们和源码对应起来。

**操作步骤**：

1. **版本**：运行 `openllm --version` 和 `openllm -v`，对照 [typer_callback](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L352-L368) 里的 `version` 分支，确认打印的版本号来自 `importlib.metadata.version("openllm")`。
2. **无命令兜底**：只运行 `openllm`（不带任何子命令），观察是否会打印帮助——这验证了 `invoke_without_command=True` 与 `OpenLLMTyper` 里 `no_args_is_help=True` 的协作。
3. **详细度**：运行 `openllm --verbose 20 serve --help`（或任意子命令的 `--help`），体会全局 `--verbose` 是「应用级」选项，出现在子命令名之前。
4. **追踪开关**：设置环境变量 `BENTOML_DO_NOT_TRACK=true`，运行任意命令（如 `openllm repo list`），对照 [analytic.py:67](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67) 解释为何此时不会触发埋点。

**需要观察的现象**：`--version` 打印完即退出，不会再要求你提供子命令；`--verbose` 必须写在子命令之前才能被回调识别为全局选项；设置 `BENTOML_DO_NOT_TRACK=true` 后命令照常执行，但不会上报使用数据。

**预期结果**：你能用一句话说清「全局 `--verbose`（整数，回调里设置 `VERBOSE_LEVEL`）」和「命令级 `--verbose`（布尔，函数体内 `VERBOSE_LEVEL.set(20)`）」的区别，并能指出 `--do-not-track` 是通过环境变量 `BENTOML_DO_NOT_TRACK` 与埋点层通信的。

> 若无法运行，至少完成源码阅读：在 `typer_callback` 里标注出三个 `if` 分支各自修改了哪个全局状态（`VERBOSE_LEVEL` / `sys.exit` / `os.environ[DO_NOT_TRACK]`），并追踪每个状态被谁消费。

#### 4.3.5 小练习与答案

**练习 1**：`openllm --version serve llama3.2:1b` 这条命令，`serve` 会真的执行吗？为什么？

**参考答案**：不会。`--version` 命中后回调里调用了 `sys.exit(0)`，程序在解析完全局选项、执行回调后就直接退出了，根本不会走到 `serve`。

**练习 2**：用户既没有传 `--do-not-track`，也没有设环境变量，但希望临时关闭某一次的埋点。除了命令行 `--do-not-track` 外，还有哪种方式？为什么有效？

**参考答案**：可以在运行前设置环境变量 `BENTOML_DO_NOT_TRACK=true`（例如 `BENTOML_DO_NOT_TRACK=true openllm serve ...`）。有效的原因是 `do_not_track` 选项用 `envvar=DO_NOT_TRACK` 绑定了该环境变量，同时埋点包装层 [analytic.py:67](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/analytic.py#L67) 也独立读取同一个环境变量来决定是否跳过 `track(...)`。

**练习 3**：为什么 `typer_callback` 里关闭埋点用的是「写环境变量」而不是直接改某个 Python 变量？

**参考答案**：因为真正执行埋点的是 `OpenLLMTyper.command` 里那层包装函数，它和回调函数不在同一个作用域，无法共享局部变量；而环境变量是进程级共享状态，回调写入后，紧随其后执行的命令包装层就能读到，是最简单可靠的跨层通信方式。

## 5. 综合实践

把本讲三个模块串起来，完成一份「OpenLLM 命令地图」。

**任务**：

1. 运行 `openllm --help`，把顶层命令（`hello/serve/run/deploy`）和子命令组（`repo/model/clean`）全部抄下来。
2. 对每个顶层命令运行 `xxx --help`，记录它的位置参数与所有 `--option`，然后到 `__main__.py` 里找到对应函数，把每个选项对应到一个形参，整理成一张表（列：命令 / 选项 / 形参名 / 类型 / 默认值 / 含义）。
3. 对 `repo/model/clean` 三个组，运行 `openllm <group> --help`，并指出它们的命令定义在哪个源码文件（提示：跟着 `__main__.py` 顶部的 `add_typer` 往回找 import）。
4. 用一张树状图把上面所有信息画出来，并在树旁标注：哪些是「指挥层（`__main__.py`）」、哪些是「执行层（`local.py`/`cloud.py`/`repo.py`/`model.py`/`clean.py`）」。
5. 最后写一段话：当用户运行 `openllm serve llama3.2:1b --verbose` 时，从「Typer 解析 → 回调 → serve 函数 → 下层执行」的完整调用顺序是怎样的。

**验收标准**：你的命令树和表格应该能让一个没读过源码的人，仅凭它就能找到任意一条 `openllm` 命令对应的源码函数和大致行为。

> 无运行环境时，可降级为「纯源码阅读版」：直接读 `__main__.py` 的四个 `@app.command` 函数签名与 `add_typer` 三行，手工拼出命令树与参数表。

## 6. 本讲小结

- OpenLLM 的 CLI 总入口是 `__main__.py` 里的 `app`，它是一个 `OpenLLMTyper`（`typer.Typer` 的子类）实例，统一了 `-h` 别名、帮助宽度、按定义顺序展示命令，并给每条命令自动裹上埋点逻辑。
- 子命令组 `repo`/`model`/`clean` 通过 `app.add_typer(...)` 挂载，它们各自定义在 `repo.py`/`model.py`/`clean.py`，本身也是 `OpenLLMTyper`。
- 四个顶层命令 `hello/serve/run/deploy` 都是「瘦」函数：收集参数 → `cmd_update()` → `ensure_bento()` → 调用 `local_serve`/`local_run`/`cloud_deploy`，真正的执行逻辑在 `local.py`/`cloud.py`。
- 全局回调 `typer_callback` 用 `invoke_without_command=True` 支持 `openllm --version`，并管理三个全局选项：`--verbose`（设 `VERBOSE_LEVEL`）、`--version`（打印并退出）、`--do-not-track`（写 `BENTOML_DO_NOT_TRACK` 环境变量）。
- `--do-not-track` 通过环境变量 `BENTOML_DO_NOT_TRACK` 与埋点层通信，体现了「回调写共享状态、命令包装层读共享状态」的跨层协作模式。

## 7. 下一步学习建议

- 想深入了解 `OpenLLMTyper` 如何劫持 `command` 装饰器、`EventMeta` 如何自动生成事件名、埋点到底上报了哪些字段，请阅读 u3-l3（CLI 命令装饰器与使用分析）。
- 想知道 `VERBOSE_LEVEL`、`INTERACTIVE`、`output()` 这些「全局上下文」到底是怎么用 `ContextVar` 实现的，请进入 u2-l1（公共基础设施：配置、输出与上下文变量）。
- 想看清 `serve`/`run` 背后真正的服务化链路（拼装 `bentoml serve`、准备 venv、轮询 `/readyz`），请直接跳到 u3-l1（本地 serve 与 run 的完整链路）。
- 推荐按学习顺序先读 u1-l4（hello 交互式流程），把本讲的顶层命令与交互式选择串成完整体验，再进入进阶层。
