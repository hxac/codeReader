# vllm-sr CLI 命令体系

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `vllm-sr` 这个命令行工具共有哪些子命令、每个子命令干什么。
- 看懂 `vllm-sr` 是如何用 Python 的 **click** 库把众多子命令组织成一个「命令组（group）」的。
- 跟踪 `vllm-sr serve` 这一条最核心的命令，从「敲下回车」一路追到「容器栈被拉起」，理解它背后的「命令层 → 后端抽象层 → 生命周期层」三层结构。
- 解读 `vllm-sr status` 的输出字段，理解「服务生命周期」是怎么被管理的（启动、探活、查日志、停止）。

本讲是入门层的最后一讲。它把你前几讲学到的「安装 / make 目标 / 本地镜像流」收束成一组**可以直接敲的命令**，让你拥有亲手把 vLLM Semantic Router 跑起来的能力。

## 2. 前置知识

在正式进入源码前，先用大白话解释三个概念。

**1）CLI（Command-Line Interface，命令行界面）**
你在一个叫终端（terminal）的黑框框里敲的那行文字，比如 `vllm-sr serve`，就是一个 CLI。`vllm-sr` 是程序名，`serve` 是它支持的「子命令」之一。本项目的 CLI 是用 Python 写的，名字叫 `vllm-sr`。

**2）click**
[click](https://click.palletsprojects.com/) 是 Python 里写 CLI 最流行的库之一。它的核心思路有两个关键词：

- **group（命令组）**：一个程序可以挂很多子命令。`vllm-sr` 本身是一个 group，它下面挂着 `serve`、`status`、`logs` 等十几个子命令。当你只敲 `vllm-sr` 不带子命令时，它会打印 logo 加帮助信息。
- **option / argument（选项 / 参数）**：子命令后面可以跟选项（如 `--config my-config.yaml`）和参数（如 `vllm-sr logs router` 里的 `router`）。click 帮你把这些文本解析成 Python 函数的参数。

你不需要提前精通 click，只需要知道：**一个被 `@click.command()` 装饰的 Python 函数，就是一个可以在终端敲的子命令**。

**3）容器栈（container stack）**
「跑起来 Semantic Router」并不是只启动一个进程，而是要同时启动好几个互相配合的容器：Envoy（负责收发流量）、router（负责做路由决策，这是 Go 写的那个内核）、dashboard（管理面板）、可能还有可观测性组件（Jaeger / Prometheus / Grafana）和一个 fleet-sim（舰队仿真）sidecar。这一整组容器合起来叫一个「栈」。本讲的 CLI 就是这个栈的「开关」。

> 承接 u1-l3：你已经知道用 `install.sh` 把 `vllm-sr` 装进一个隔离的 venv、用 `make` 本地构建镜像、并遵循「本地镜像流」（一律本地构建、用 `--image-pull-policy never` 启动）。本讲要回答的是：装好之后，**你在终端里到底敲哪些命令、这些命令背后跑了什么**。

## 3. 本讲源码地图

本讲涉及的源码都位于 Python CLI 包 `src/vllm-sr/` 下，根模块路径前缀是 `cli`（即 `src/vllm-sr/cli/`）。

| 文件 | 角色 |
|------|------|
| [src/vllm-sr/cli/main.py](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/main.py) | CLI 总入口。定义 `vllm-sr` 这个顶层命令组，把所有子命令注册进来。 |
| [src/vllm-sr/cli/commands/runtime.py](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py) | 「运行时」类命令的入口。`serve` / `status` / `logs` / `stop` / `dashboard` 五个最常用的命令都定义在这里。 |
| [src/vllm-sr/cli/commands/general.py](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/general.py) | 「配置与查询」类命令的入口。`config` / `validate` / `model` / `rag` 定义在这里。 |
| [src/vllm-sr/cli/deployment_backend.py](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/deployment_backend.py) | **后端抽象层**。用 Python Protocol 定义 `DeploymentBackend` 接口，统一 Docker 与 Kubernetes 两种部署目标。 |
| [src/vllm-sr/cli/container_backend.py](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/container_backend.py) | Docker 后端实现。把命令转交给 `core.py` 里真正干活的函数。 |
| [src/vllm-sr/cli/core.py](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py) | **生命周期层**。`start_vllm_sr` / `show_status` / `show_logs` / `stop_vllm_sr` 等真正操作容器的核心函数都在这里。 |
| [src/vllm-sr/cli/consts.py](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/consts.py) | 常量集中地。默认端口、默认镜像名、镜像拉取策略等。 |

整本 CLI 的代码分层可以用一句话概括：

```
终端命令（commands/*.py）  →  后端抽象（deployment_backend.py + container_backend.py）  →  生命周期（core.py）
```

## 4. 核心概念与源码讲解

### 4.1 CLI 命令注册：一个 group，十二个子命令

#### 4.1.1 概念说明

`vllm-sr` 不是一个大文件，而是一个**顶层命令组（group）**加上一批**子命令（command）**。

- 顶层组叫 `main`，对应你敲的 `vllm-sr`。
- 每个子命令对应一个被 `@click.command()` 或 `@click.group()` 装饰的 Python 函数，分散在 `cli/commands/` 下的不同文件里。
- `main.py` 的职责很单一：把这些子命令「挂」到 `main` 组上，组装出完整的 CLI。

这种「定义在多处、在入口处汇总注册」的写法，是大型 CLI 项目（用 click 或 Typer）的标准模式。好处是：加一个新命令只需要写一个新文件、在 `main.py` 里加一行，其余地方完全不用动。

#### 4.1.2 核心流程

`main.py` 组装 CLI 的流程：

1. 从各个 `cli.commands.*` 模块 import 进来已经定义好的子命令对象（每个都是 click 的 `Command` 或 `Group` 实例）。
2. 把它们放进一个元组 `REGISTERED_COMMANDS`，作为「注册清单」。
3. 定义顶层 `main` group。
4. 用一个 `for` 循环，把清单里的每个命令通过 `main.add_command(...)` 注册到 group 上。
5. 文件末尾的 `if __name__ == "__main__": main()` 让它也能被 `python -m` 直接运行。

最终，当你在终端敲 `vllm-sr <子命令>` 时，click 会根据名字找到对应的命令函数并调用它。

#### 4.1.3 源码精读

先看 import 与注册清单。`main.py` 顶部从各命令模块拉进所有子命令，再汇总成一个元组：

```python
from cli.commands.chat import chat
from cli.commands.completion import completion
from cli.commands.eval import eval
from cli.commands.general import config, model, rag, validate
from cli.commands.runtime import dashboard, logs, serve, status, stop
```

完整清单见 [main.py:24-37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/main.py#L24-L37)（`REGISTERED_COMMANDS` 元组，共 12 个命令）——这一段中文说明：把所有要暴露给用户的子命令按固定顺序登记在一个元组里，顺序就是 `--help` 里子命令的展示顺序。

接着是顶层 group 的定义，见 [main.py:40-51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/main.py#L40-L51)：

```python
@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx: click.Context, version: bool) -> None:
    """vLLM Semantic Router CLI - Intelligent routing and caching for vLLM endpoints."""
    if version:
        click.echo(f"vllm-sr version: {__version__}")
        ctx.exit()

    if ctx.invoked_subcommand is None:
        click.echo(logo)
        click.echo(ctx.get_help())
```

要点逐条解释：

- `@click.group(invoke_without_command=True)`：声明 `main` 是一个命令组；`invoke_without_command=True` 表示「即使没带子命令也允许调用本函数」——这正是只敲 `vllm-sr` 回车时打印 logo + 帮助的前提。
- `ctx.invoked_subcommand is None`：判断用户有没有给子命令。没有就打印 logo 和帮助；有就交给 click 去分发，本函数什么都不用做。
- `--version` 是组级别选项，所以 `vllm-sr --version` 在任何子命令前都有效。

最后，把清单里的命令真正注册上去，见 [main.py:54-55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/main.py#L54-L55)：

```python
for command in REGISTERED_COMMANDS:
    main.add_command(command)
```

这一段中文说明：遍历注册清单，调用 click group 自带的 `add_command` 把每个子命令挂上去。一行循环完成了整个 CLI 的组装。

> 小贴士：`add_command` 是 click.Group 的标准方法。你也可以在 `@click.group()` 后用 `@main.command()` 的方式直接定义子命令，但当子命令散落在多个文件时，import + 循环注册的方式更清爽。

**12 个子命令总览**（对照源码整理）：

| 子命令 | 定义文件 | 一句话作用 |
|--------|---------|-----------|
| `serve` | `commands/runtime.py` | 启动整套服务栈（本地 Docker 或 Kubernetes） |
| `status` | `commands/runtime.py` | 查看各服务运行状态与探活结果 |
| `logs` | `commands/runtime.py` | 查看某个服务的日志（支持 `-f` 跟随） |
| `stop` | `commands/runtime.py` | 停止并清理服务栈 |
| `dashboard` | `commands/runtime.py` | 在浏览器打开管理面板 |
| `config` | `commands/general.py` | 打印 / 迁移 / 导入配置（自身是 group，含 `envoy`/`router`/`migrate`/`import` 子命令） |
| `validate` | `commands/general.py` | 校验 `config.yaml` 合法性 |
| `model` | `commands/general.py` | 列出配置里的模型（含 `list` 子命令） |
| `rag` | `commands/general.py` | 列出 RAG 向量库（含 `list` 子命令） |
| `eval` | `commands/eval.py` | 把一段 prompt 发给路由器的 `/api/v1/eval` 端点，看信号评估结果 |
| `chat` | `commands/chat.py` | 一次性对话补全，走 Envoy 路由后的 HTTP API |
| `completion` | `commands/completion.py` | 生成 / 安装 shell 补全脚本（bash/zsh/fish） |

#### 4.1.4 代码实践

**实践目标**：用 `--help` 把整个命令树摸清楚。

**操作步骤**：

1. 确认 `vllm-sr` 已安装（回顾 u1-l3 的 `install.sh`）。
2. 运行 `vllm-sr --help`，观察输出：开头是 ASCII logo，下面是「Commands:」列表。
3. 核对列表里的命令名和数量，是否和上面表格里的 12 个一致。
4. 运行 `vllm-sr --version`，记录版本号。
5. 对几个 group 型命令再深入一层：`vllm-sr config --help`、`vllm-sr model --help`、`vllm-sr completion --help`，看它们的二级子命令。

**需要观察的现象**：

- `vllm-sr --help` 的 Commands 区块里，命令的排列顺序应与 [main.py:24-37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/main.py#L24-L37) 里 `REGISTERED_COMMANDS` 的顺序一致（serve 在最前）。
- `vllm-sr config --help` 应该列出 `envoy`、`router`、`migrate`、`import` 四个子命令，对应 [general.py:40-130](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/general.py#L40-L130)。

**预期结果**：你能拿到一张完整的命令地图，并且明白「想加一个新命令」需要改的最小位置就是 `main.py` 的 import 和 `REGISTERED_COMMANDS`。

> 待本地验证：如果你尚未跑过 `install.sh`，`vllm-sr` 命令不存在，需要先完成 u1-l3 的安装步骤。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` group 要加 `invoke_without_command=True`？如果去掉会怎样？
**参考答案**：不加这个参数时，click 的 group 在没有子命令的情况下会直接报错退出。加上后，无子命令时会进入函数体，于是可以打印 logo 和帮助信息，体验更友好。见 [main.py:49-51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/main.py#L49-L51)。

**练习 2**：如果要新增一个 `vllm-sr hello` 命令，最少要改哪些地方？
**参考答案**：(a) 在 `cli/commands/` 下某文件里写一个 `@click.command()` 装饰的 `hello` 函数；(b) 在 `main.py` 里 `import hello`；(c) 把 `hello` 加进 `REGISTERED_COMMANDS` 元组。见 [main.py:24-55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/main.py#L24-L55)。

---

### 4.2 serve 编排：从一条命令到「部署后端」

#### 4.2.1 概念说明

`vllm-sr serve` 是整个 CLI 里最重要的命令——它负责把 Semantic Router 的整套容器栈跑起来。

它要解决的问题是：Semantic Router 既能部署在**本地 Docker**（开发者自测最常用），也能部署在 **Kubernetes**（生产/Helm）。如果让 `serve` 命令自己去 `if docker ... else k8s ...`，代码会又长又乱。项目用一个叫 **DeploymentBackend（部署后端）** 的抽象来隔离这两种环境：

- 一份「部署后端接口」（`DeploymentBackend` Protocol）规定了任何后端都必须实现 `deploy` / `teardown` / `logs` / `status` 等方法。
- `ContainerBackend`（Docker 实现）和 `K8sBackend`（Kubernetes 实现）各自满足这个接口。
- `serve` 命令只负责「解析参数 + 选后端 + 调 `deploy()`」，它根本不关心底层是 Docker 还是 K8s。

这就是经典的「依赖倒置 / 策略模式」：高层命令依赖抽象接口，不依赖具体实现。

#### 4.2.2 核心流程

`vllm-sr serve` 被敲下后的完整路径：

```
vllm-sr serve [--config ...] [--target docker|k8s] [--algorithm ...] ...
        │
        ▼
runtime.py: serve()                ← click 命令，负责解析所有 --option
        │
        ▼
runtime.py: _execute_serve()       ← 业务编排：引导工作区、解析配置、收集环境变量
        │
        ├── ensure_bootstrap_workspace()     ← 准备配置文件、判定是否 setup 模式
        ├── append_passthrough_env_vars()    ← 透传环境变量
        ├── apply_runtime_mode_env_vars()    ← minimal/readonly/platform/algorithm 等写入 env
        ├── resolve_effective_config_path()  ← 确定最终生效的配置文件
        │
        ▼
runtime.py: _build_backend(target) ← 选后端：docker→ContainerBackend，k8s→K8sBackend
        │
        ▼
backend.deploy(...)                ← 接口调用，与具体环境无关
        │  （docker 分支）
        ▼
container_backend.py: deploy()     ← Docker 后端：直接转交
        │
        ▼
core.py: start_vllm_sr()           ← 真正拉起容器栈（下一节细讲）
```

关键在于：`_execute_serve` 是「与环境无关」的编排逻辑，`_build_backend` 是唯一的环境分叉点，`backend.deploy()` 之后命令层就不再关心是哪种环境了。

#### 4.2.3 源码精读

**(1) 后端抽象接口**

`deployment_backend.py` 用 Python 的 `Protocol`（结构化子类型）定义了所有部署后端必须满足的形状，见 [deployment_backend.py:8-42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/deployment_backend.py#L8-L42)：

```python
class DeploymentBackend(Protocol):
    """Interface that all deployment targets (Docker, Kubernetes) must implement."""

    def deploy(self, config_file, env_vars=None, *, image=None,
               pull_policy=None, enable_observability=True, **kwargs) -> None: ...
    def teardown(self) -> None: ...
    def logs(self, service: str, follow: bool = False) -> None: ...
    def status(self, service: str = "all") -> None: ...
    def get_dashboard_url(self) -> str | None: ...
    def is_running(self) -> bool: ...
```

这一段中文说明：用 Protocol 声明六个方法，`serve`/`stop`/`logs`/`status`/`dashboard` 这些命令全部只认这六个方法签名，不认具体实现类。紧接着 [deployment_backend.py:45-46](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/deployment_backend.py#L45-L46) 定义了合法目标：`VALID_TARGETS = ("docker", "k8s")`，默认 `docker`。

> 术语提示：Python 的 `Protocol` 和 Java/Go 的 interface 类似——只要一个类「长得像」（有这些方法），就算实现了协议，不需要显式 `implements`。这是一种「鸭子类型」的接口。

**(2) serve 命令本身**

`serve` 命令定义在 [runtime.py:144-288](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L144-L288)，它由一大串 `@click.option` 装饰器和一个极薄的函数体组成。函数体只有一行——把所有参数原样转给 `_execute_serve`：

```python
def serve(config, image, router_image, ...many options..., runtime) -> None:
    _execute_serve(config, image, router_image, ..., runtime)
```

这一段中文说明：`serve` 命令本身只做「参数声明」，真正的逻辑全在 `_execute_serve` 里。这种「薄命令 + 厚函数」的拆分让命令既容易被 click 装饰器堆叠，又方便单元测试（可以直接调函数，不用启动 click）。

注意它的选项里和 u1-l3 强相关的两个：

- `--image-pull-policy`（[runtime.py:176-187](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L176-L187)）：取值 `always` / `ifnotpresent` / `never`，默认 `always`。本地开发走「本地镜像流」时要传 `never`，绝不隐式拉远程镜像——这正是 u1-l3 讲过的约定。
- `--platform`（[runtime.py:210-218](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L210-L218)）：取 `amd` 或 `nvidia`，对应 u1-l3 的本地环境三元组，会切到 ROCm / CUDA 镜像并打开 GPU 透传。

**(3) 业务编排 `_execute_serve`**

这是 `serve` 的核心大脑，见 [runtime.py:73-141](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L73-L141)。它做的事按顺序是：

1. `apply_container_runtime_override(runtime)`：应用 `--runtime`（docker/podman）覆盖。
2. `ensure_bootstrap_workspace(Path(config))`：引导工作区，确定真正要用的配置文件路径，并判断是否处于 setup（首次安装）模式。
3. 收集 `env_vars`：透传环境变量 + 把 minimal/readonly/platform/algorithm/log_level 等模式写进环境变量。
4. `resolve_effective_config_path(...)`：算出最终生效的配置文件（可能因 algorithm/platform 而改写）。
5. `_build_backend(target, ...)`：根据 `--target` 选 Docker 或 K8s 后端。
6. `backend.deploy(...)`：把配置、镜像、环境变量、拉取策略一股脑交给后端。

最后这一步是接口调用，对应 [runtime.py:122-141](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L122-L141)。

**(4) 后端工厂 `_build_backend`**

整个 CLI 唯一的「环境分叉点」，见 [runtime.py:60-70](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L60-L70)：

```python
def _build_backend(target: str | None, **k8s_kwargs):
    """Instantiate the right DeploymentBackend for *target*."""
    resolved = resolve_target(target)
    if resolved == "k8s":
        from cli.k8s_backend import K8sBackend
        return K8sBackend(**{k: v for k, v in k8s_kwargs.items() if v is not None})

    from cli.container_backend import ContainerBackend
    return ContainerBackend()
```

这一段中文说明：先 `resolve_target` 把 `None` 归一化为默认 `docker` 并校验合法性；然后按目标 `import` 对应后端类并实例化。注意 import 是写在函数内部的「惰性导入」——这样用 Docker 的用户就不必为了一个 `import` 去装 K8s 依赖，反之亦然。

#### 4.2.4 代码实践

**实践目标**：读懂 `serve` 都能接受哪些参数，特别是和「部署目标」与「选择算法」相关的。

**操作步骤**：

1. 运行 `vllm-sr serve --help`。
2. 在输出里找到并记录这几组选项的含义：
   - `--config`、`--image`、`--image-pull-policy`（镜像相关）
   - `--target`、`--namespace`、`--context`、`--profile`、`--chart-dir`（K8s 相关）
   - `--algorithm`（选择算法覆盖）
   - `--minimal`、`--readonly`（运行模式）
   - `--platform`（GPU 平台）
3. 对照 [SERVE_HELP](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime_help.py#L3-L71)（`runtime_help.py` 里 `serve` 的帮助文本），它把 `--algorithm` 的所有取值和语义都列了出来（static / router_dc / automix / hybrid / workflows / latency_aware / knn / kmeans / svm / mlp / multi_factor）。

**需要观察的现象**：`--help` 的 Options 区块会列出所有 `@click.option`；其中 `--algorithm` 是 `click.Choice(...)`，所以它只接受限定值，敲错会直接报错。

**预期结果**：你应当能解释「为什么 `vllm-sr serve` 默认走 Docker、要切到 K8s 只需加 `--target k8s`」——因为默认 target 是 `docker`，而 `_build_backend` 会据此实例化 `ContainerBackend`。

> 待本地验证：实际拉起栈需要可用的容器运行时和配置文件；本实践只要求你读 `--help`，不需要真正 `serve` 成功。

#### 4.2.5 小练习与答案

**练习 1**：`serve` 命令的函数体几乎为空（只调用 `_execute_serve`），为什么要这样拆？
**参考答案**：click 的 `@click.option` 装饰器很多，堆在一个函数上已经够长；把真正的业务逻辑放进不被装饰器包围的 `_execute_serve`，既能复用、又便于直接单测，还让「参数声明」和「业务逻辑」职责分离。见 [runtime.py:249-288](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L249-L288) 与 [runtime.py:73-141](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L73-L141)。

**练习 2**：`_build_backend` 为什么把 `import` 写在函数内部而不是文件顶部？
**参考答案**：惰性导入。`k8s_backend` 可能依赖 Helm/kubectl 相关的东西，把它的 import 放进 `if resolved == "k8s"` 分支，可以保证只用 Docker 的用户不会被 K8s 依赖拖累，也加快了非 K8s 路径的启动。见 [runtime.py:60-70](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L60-L70)。

---

### 4.3 服务生命周期：start / status / logs / stop / dashboard

#### 4.3.1 概念说明

上一节讲到 `serve` 最终调用 `backend.deploy()`。在 Docker 路径下，`ContainerBackend.deploy()` 直接转交给 `core.py` 里的 `start_vllm_sr()`——这才是真正「把一堆容器拉起来」的地方。

`core.py` 是 CLI 的**生命周期层**，它承担一个完整服务生命周期里四件事：

- **start（启动）**：`start_vllm_sr`——建网络、起存储后端、起可观测性栈、起 fleet-sim、起 router/envoy/dashboard 容器、探活、收尾汇报。
- **status（探活）**：`show_status`——逐个服务做健康检查并打印。
- **logs（日志）**：`show_logs`——从对应容器抓日志，按服务类型用正则过滤。
- **stop（停止）**：`stop_vllm_sr`——按依赖反序停止并清理所有容器和网络。

`ContainerBackend`（[container_backend.py:15-75](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/container_backend.py#L15-L75)）就是这四个函数的一层极薄包装，让它们「恰好满足」`DeploymentBackend` 接口。所以你可以这样理解分层：

> `commands/runtime.py`（命令层）→ `container_backend.py`（接口适配层）→ `core.py`（真正干活的生命周期层）

#### 4.3.2 核心流程

**启动流程 `start_vllm_sr`**（[core.py:137-227](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L137-L227)）大致顺序：

```
start_vllm_sr(config_file, env_vars, image, ...)
  │
  ├── resolve_runtime_stack()           # 解析栈布局：容器名、端口、网络名
  ├── resolve_runtime_topology()        # 解析拓扑（如 split）
  ├── _load_runtime_config()            # 读 config.yaml，拿到 listeners
  ├── 清理旧的同名运行时容器
  ├── _prepare_runtime_network()        # 建共享网络 + 校验镜像存在
  ├── _start_support_services()         # 起存储后端 + 可观测性栈 + fleet-sim
  ├── _start_runtime_containers()       # 起 router/envoy/dashboard 容器
  ├── connect_runtime_container()       # 把运行时容器接入共享网络
  ├── _wait_and_verify_runtime()        # 等待 router 健康 + 校验容器未退出
  └── log_runtime_summary()             # 打印最终的服务清单与端口
```

**探活流程 `show_status`**（[core.py:496-528](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L496-L528)）：先取整栈状态快照；如果根本没起过就提示「用 `vllm-sr serve` 启动」；如果在跑，就逐个服务做健康检查并打印一行汇总。

每个服务的健康检查方式不同，由 `_report_service_status` 分发（[core.py:580-610](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L580-L610)）。例如 router 的检查是进容器里 `curl` 它的 `/health` 端点，见 [core.py:612-617](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L612-L617)：

```python
def _check_router_status(container_name: str) -> bool:
    return_code, _stdout, _stderr = container_exec(
        container_name,
        ["curl", "-f", "-s", f"http://localhost:{DEFAULT_API_PORT}/health"],
    )
    return return_code == 0
```

这一段中文说明：router 的健康检查是在 router 容器内部执行 `curl` 访问本机 `8080/health`（`DEFAULT_API_PORT=8080`，见 [consts.py:45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/consts.py#L45)），返回码 0 视为健康。

**停止流程 `stop_vllm_sr`**（[core.py:310-357](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L310-L357)）：先取所有受管容器的状态；如果全部不存在就「无可停止」直接返回；否则按「运行时容器 → fleet-sim → 可观测性 → 存储」的顺序逐个 stop + remove，最后删掉共享网络。

#### 4.3.3 源码精读

**(1) 适配层：ContainerBackend**

[container_backend.py:15-75](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/container_backend.py#L15-L75) 把接口方法和 core.py 函数一一对应起来：

```python
class ContainerBackend:
    def deploy(self, config_file, env_vars=None, *, source_config_file=None, ...):
        start_vllm_sr(config_file, env_vars=env_vars, source_config_file=..., ...)
    def teardown(self):       stop_vllm_sr()
    def logs(self, service, follow=False): show_logs(service, follow=follow)
    def status(self, service="all"):       show_status(service)
    def get_dashboard_url(self): ...
    def is_running(self): ...   # 任一运行时容器在跑即视为在跑
```

这一段中文说明：`ContainerBackend` 不写业务逻辑，只做「接口 → 函数」的转接。`is_running` 的实现（[container_backend.py:69-74](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/container_backend.py#L69-L74)）只要栈里任意一个运行时容器状态是 `running`，就返回 True——这正是 `chat` / `dashboard` 命令判断「栈是否在跑」的依据。

**(2) status / logs / stop / dashboard 四个命令**

它们都定义在 `runtime.py`，结构高度一致：解析 `--target` → `_build_backend` → 调对应接口方法。以 `status` 为例，见 [runtime.py:311-332](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L311-L332)：

```python
def status(service, target, namespace, context, runtime) -> None:
    apply_container_runtime_override(runtime)
    backend = _build_backend(target, namespace=namespace, context=context)
    backend.status(service)
```

这一段中文说明：四个命令共用同一个套路——「应用 runtime 覆盖 → 选后端 → 调接口」。`logs`（[runtime.py:353-377](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L353-L377)）、`stop`（[runtime.py:394-409](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L394-L409)）、`dashboard`（[runtime.py:428-459](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/runtime.py#L428-L459)）都遵循这个模式。`dashboard` 多一步：先 `backend.is_running()`，没跑就报错；跑着就取 `get_dashboard_url()` 并用 `webbrowser.open()` 打开。

**(3) 端口与容器命名**

栈布局由 `resolve_runtime_stack()` 返回的 `RuntimeStackLayout` 决定（见 [runtime_stack.py:33-60](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/runtime_stack.py#L33-L60)）。它集中了所有容器名和端口，并支持用环境变量 `VLLM_SR_PORT_OFFSET` 整体偏移端口、用 `VLLM_SR_STACK_NAME` 改栈名。基础默认端口见 [consts.py:43-49](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/consts.py#L43-L49)：

| 常量 | 默认值 | 含义 |
|------|--------|------|
| `DEFAULT_API_PORT` | 8080 | router 管理 API（`/health`、`/api/v1/eval`） |
| `DEFAULT_ENVOY_PORT` | 9901 | Envoy admin（`/ready` 探活用） |
| `DEFAULT_LISTENER_PORT` | 8899 | 业务监听口（实际取值来自 config.yaml 的 `listeners`） |
| `DEFAULT_DASHBOARD_PORT` | 8700 | 管理面板 |
| `DEFAULT_FLEET_SIM_PORT` | 8810 | fleet-sim sidecar |
| `DEFAULT_METRICS_PORT` | 9190 | Prometheus 指标 |

> 注意：`DEFAULT_ENVOY_PORT=9901` 是 Envoy 的 **admin/ready 端口**，不是业务流量端口；业务监听口来自 `config.yaml` 的 `listeners` 段。`_check_envoy_status` 探的就是 `/ready`，见 [core.py:620-637](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L620-L637)。

#### 4.3.4 代码实践

**实践目标**：用 `vllm-sr status` 看一次本地服务状态，并逐字段解读。

**操作步骤**：

1. 先跑一次 `vllm-sr serve`（或确认栈已在跑）。如果不想真起栈，也可以直接跳到第 3 步观察「未运行」的输出。
2. 运行 `vllm-sr status`（等价于 `vllm-sr status all`）。
3. 也试一下 `vllm-sr status router`、`vllm-sr status envoy` 等单服务查询。
4. 对照源码 [core.py:496-528](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L496-L528) 与 [core.py:580-610](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L580-L610)，把每行输出对应到一个 `_report_service_status` 检查项。

**需要观察的现象与字段解读**：

- 栈未运行时：会看到 `Status: Not running` 与提示 `Start with: vllm-sr serve`（来自 [core.py:508-510](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L508-L510)）。
- 容器已退出（异常）：会看到 `Status: Container exited (error)`（[core.py:511-514](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L511-L514)）。
- 正常运行时：会看到分隔线 + `Container Status: Running`，随后逐行服务状态，例如：
  - `Router: Running` —— 来自 `_check_router_status`，靠容器内 `curl 8080/health` 判定（[core.py:612-617](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L612-L617)）。
  - `Envoy: Running` —— 来自 `_check_envoy_status`，靠 `9901/ready` 返回 200 判定（[core.py:620-637](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L620-L637)）。
  - `Dashboard: Running (http://localhost:8700)` —— `_check_dashboard_status` 探 `:8700`，详情附 URL（[core.py:661-675](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L661-L675)）。
  - `Fleet Sim: Running` —— 探容器内 `8000/healthz`（[core.py:678-691](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L678-L691)）。
- 某服务检查抛异常时：会看到 `WARNING <Label>: Status unknown`（[core.py:694-703](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L694-L703)）。

**预期结果**：你能把 `vllm-sr status` 输出的每一行，精确指回源码里产生它的那个检查函数，从而理解「这个 Running / WARNING 是怎么判定出来的」。

> 待本地验证：实际输出取决于你本机是否成功 `serve`。若 Docker 守护进程不可达，会走 [core.py:531-545](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L531-L545) 的兜底分支，提示「Docker daemon is not reachable」。

#### 4.3.5 小练习与答案

**练习 1**：`stop_vllm_sr` 按什么顺序停止容器？为什么是这个顺序？
**参考答案**：顺序是「运行时容器（router/envoy/dashboard）→ fleet-sim → 可观测性（grafana/prometheus/jaeger）→ 存储」。先停最上层的业务容器，再停依赖的支撑组件，最后才拆网络，避免「先拆网络导致上层容器异常退出时来不及清理」。见 [core.py:310-357](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L310-L357)。

**练习 2**：`ContainerBackend.is_running()` 为什么只要「任意一个」运行时容器在跑就算 running，而不是要求「全部」在跑？
**参考答案**：它是一个保守的「栈是否存在」判断，用于 `chat`/`dashboard` 等命令的前置校验——只要还有 router/envoy 在跑，就说明用户确实 serve 过、栈还在；若要求全部在跑，那么某个非关键容器（如 dashboard）短暂未就绪就会误判为「没起」，反而挡住用户。见 [container_backend.py:69-74](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/container_backend.py#L69-L74)。

**练习 3**：`show_status` 里 router 和 envoy 的健康检查为什么都用「进容器里 curl」而不是从宿主机 curl？
**参考答案**：因为这些端口默认只在该容器所属的 Docker 网络内可达（或按 listeners 映射），从容器内 `localhost` 探最稳，能避免端口偏移、网络隔离带来的误判。`DEFAULT_API_PORT`/`DEFAULT_ENVOY_PORT` 是容器内监听口，见 [core.py:612-637](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L612-L637)。

## 5. 综合实践

**任务**：把本讲的三层结构串起来，亲手完成一次「启动 → 观察 → 停止」的完整生命周期，并用源码解释你看到的每一步。

1. **启动并定位分层**：运行 `vllm-sr serve`（默认 Docker target）。在它打印的日志里，尝试对应到 `start_vllm_sr` 的子步骤（建网络、起支撑服务、起运行时容器、探活、汇报）。
2. **查询状态**：运行 `vllm-sr status`，逐行标注每条输出来自哪个 `_check_*_status` 函数；若想更细，用 `vllm-sr logs router` 看 router 日志（关注被 `SERVICE_LOG_PATTERNS` 正则命中的行，见 [core.py:42-49](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/core.py#L42-L49)）。
3. **追踪一次调用链**：在 `runtime.py` 的 `status` 命令处打断点（或加 `print`），确认调用顺序是 `status()` → `_build_backend("docker")` → `ContainerBackend.status()` → `core.show_status()`。这验证了「命令层 → 适配层 → 生命周期层」的三层模型。
4. **切换后端（可选）**：如果你有可用的 K8s 集群，运行 `vllm-sr serve --target k8s --namespace my-ns --profile dev`，观察 `_build_backend` 这次实例化的是 `K8sBackend` 而不是 `ContainerBackend`，而命令层代码一行都没变——这就是后端抽象的价值。
5. **停止并验证**：运行 `vllm-sr stop`，再 `vllm-sr status` 应当显示 `Not running`。

> 这是一个「贯穿三层结构 + 生命周期四阶段」的实践。如果你无法真正起栈，也可以降级为「源码阅读型实践」：只做第 3 步的调用链追踪，对照本讲给出的源码行号把链路画出来即可。

## 6. 本讲小结

- `vllm-sr` 是一个用 **click** 写的顶层命令组，共 12 个子命令；新增命令只需在 `main.py` 的 `REGISTERED_COMMANDS` 里登记。
- CLI 呈清晰的三层结构：**命令层**（`commands/*.py`）→ **后端抽象层**（`deployment_backend.py` 的 `DeploymentBackend` Protocol + `container_backend.py` / `k8s_backend.py` 实现）→ **生命周期层**（`core.py`）。
- `vllm-sr serve` 是最核心命令；它的业务大脑是 `_execute_serve`，唯一的「环境分叉点」是 `_build_backend`，真正拉起容器栈的是 `core.start_vllm_sr`。
- `status` / `logs` / `stop` / `dashboard` 四个命令共用「应用 runtime → 选后端 → 调接口」的同一套模板。
- 服务的「健康」由各不相同的探活函数判定：router 探 `8080/health`、envoy 探 `9901/ready`、dashboard 探 `:8700`、fleet-sim 探 `8000/healthz`。
- 默认端口、镜像名、拉取策略等常量集中放在 `consts.py`；栈布局（容器名 + 端口）由 `runtime_stack.py` 的 `RuntimeStackLayout` 统一管理，支持端口偏移与改名。

## 7. 下一步学习建议

到此入门层（U1–U3）的命令与运行部分就结束了。建议接下来：

- 如果你还没读过 `config.yaml`，先去 **U3（配置体系与 Recipe）**，特别是 **u3-l1（config.yaml v0.3 整体结构）**——`serve` 命令的第一个参数 `--config` 指向的就是它，理解配置结构能让你知道 `serve` 到底「serve 了什么」。
- 想理解「栈里的 router 容器内部到底怎么处理请求」，进入 **U4（路由器启动与 ExtProc 服务）**，从 **u4-l1（main.go 启动序列）** 开始——那是 Go 内核的入口，与本讲的 Python CLI 入口遥相呼应。
- 想深入「serve 在 K8s 路径下做了什么」，可以直接看 **u12-l2（Helm / Kubernetes / 本地部署）** 与 `k8s_backend.py` 的 `deploy` 实现。
- 继续阅读源码建议顺序：`main.py` → `commands/runtime.py` → `deployment_backend.py` → `container_backend.py` → `core.py` → `runtime_stack.py`，正好是本讲的三层调用链自上而下。
