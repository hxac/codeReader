# vllm CLI 与 serve 启动在线服务

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `vllm` 这个命令行工具是怎么被定义出来的、由哪些子命令组成。
- 跟着源码走一遍 `vllm serve <model>` 从「敲下命令」到「HTTP 服务就绪」的完整路径。
- 理解「子命令分发」「参数解析」「HTTP 服务启动」这三件事分别在哪个文件、由谁负责。
- 区分 `serve.py`、`launcher.py`、`api_server.py` 三者的职责边界，不再混淆「谁拉起进程、谁建 socket、谁真正处理 HTTP 请求」。
- 学会用 `--help`、`--config`、`--api-server-count` 等参数，并理解单服务/多服务/无头（headless）三种部署分支的区别。

## 2. 前置知识

本讲默认你已经读过以下讲义：

- **u1-l3 仓库目录结构**：知道 `vllm/entrypoints/` 是「进入系统的两扇门」，离线推理走 `LLM` 类、在线服务走 `vllm serve`。
- **u1-l4 公共 API 与懒加载**：知道 vLLM 喜欢把重型模块延迟加载（lazy import）。

补充几个本讲会用到的通俗概念：

- **入口脚本（console script）**：pip 安装包时，可以在 `pyproject.toml` 里声明「安装后自动生成一个命令」。vLLM 声明了 `vllm = "vllm.entrypoints.cli.main:main"`，所以你装完包后终端里就能直接敲 `vllm`。
- **子命令（subcommand）**：像 `git add`、`git commit` 这种「主命令 + 动词」的结构。`vllm` 也是子命令式工具，`vllm serve`、`vllm bench`、`vllm chat` 都是不同动词。
- **argparse**：Python 标准库的命令行解析器。vLLM 在它基础上包了一层 `FlexibleArgumentParser`，支持从 YAML 配置文件读参数。
- **OpenAI 兼容服务**：指用 HTTP 暴露出和 OpenAI 官方 API 一样的接口（`/v1/chat/completions` 等），这样任何用 OpenAI SDK 写的客户端都能直接连上来。
- **uvloop**：一个用 C 写的、比标准库 asyncio 快很多的事件循环。vLLM 服务端用它来跑异步任务。

> 名词提醒：本讲里出现的「launcher」「api_server」容易让人混淆。`launcher.py` 并不是「拉起进程」的启动器，而是 **HTTP 服务启动器**（负责启动 uvicorn）；真正「拉起多个进程」的逻辑在 `serve.py` 和 `api_server.py` 里。下面会反复强调这个区别。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [`pyproject.toml`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L43-L44) | 声明 `vllm` 命令行入口脚本，指向 `vllm.entrypoints.cli.main:main`。 |
| `vllm/entrypoints/cli/main.py` | CLI 总入口：构造顶层解析器、注册所有子命令、根据用户输入分发。 |
| `vllm/entrypoints/cli/types.py` | 定义子命令基类 `CLISubcommand`（统一的「解析 + 校验 + 执行」接口）。 |
| `vllm/entrypoints/cli/serve.py` | `serve` 子命令的全部逻辑：解析参数、决定部署分支、最终拉起服务。 |
| `vllm/entrypoints/openai/cli_args.py` | `serve` 子命令实际用到的参数解析器 `make_arg_parser`（定义 `model_tag`、`--headless`、`--api-server-count` 等）。 |
| `vllm/entrypoints/launcher.py` | HTTP 服务启动器 `serve_http`：用 uvicorn 跑 FastAPI 应用，处理信号与看门狗。 |
| `vllm/entrypoints/openai/api_server.py` | OpenAI 兼容 HTTP 服务的核心：`run_server` / `setup_server` / 构建 FastAPI app。 |
| `vllm/entrypoints/serve/utils/api_utils.py` | CLI 环境初始化（`cli_env_setup`）、帮助文本（epilog）等共用工具。 |

一句话记忆链路：

```
vllm 命令 → main.py(分发) → serve.py(serve 子命令) → api_server.py(run_server) → launcher.py(serve_http, uvicorn)
```

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 CLI 总入口与子命令分发**（`main.py`）
2. **4.2 serve 子命令与参数处理**（`serve.py` + `cli_args.py`）
3. **4.3 HTTP 服务启动器与生命周期**（`launcher.py`）
4. **4.4 serve 与底层 api_server 的关系**（`api_server.py`）

---

### 4.1 CLI 总入口与子命令分发

#### 4.1.1 概念说明

`vllm` 是一个子命令式 CLI。所谓「子命令式」，就是命令行第一段固定是动词（`serve`、`bench`、`chat`…），动词后面的参数才因命令而异。这种结构天然适合用「每个动词一个类」来组织——每个类负责「自己怎么解析参数、自己怎么执行」。vLLM 把这个约定固化成一个基类 `CLISubcommand`。

为什么这样设计？因为 vLLM 的功能在持续膨胀（推理服务、批量推理、压测、环境收集……），如果全塞进一个大 `if/elif`，文件会变得无法维护。子命令模式让每个功能模块独立演进，互不干扰，而且新增功能只要「写一个子命令类 + 在列表里登记」即可。

#### 4.1.2 核心流程

`vllm` 命令的整体处理流程可以概括为五步：

```text
1. 入口 main() 被调用（来自 pyproject.toml 声明的 console script）
2. cli_env_setup()  做环境准备（最重要的：把多进程方式设为 spawn）
3. 构造顶层 FlexibleArgumentParser，建一个 subparsers 容器
4. 遍历 CMD_MODULES，调用每个模块的 cmd_init() 拿到一批子命令对象，
   用 subparser_init() 给每个子命令挂上自己的参数解析器，
   并 set_defaults(dispatch_function=...) 记下「这个动词该执行谁」
5. parse_args() 解析用户输入；如果有匹配的子命令，先 validate()，再调用 dispatch_function(args)
```

子命令基类 `CLISubcommand` 定义了三件事，形成一个统一契约：

- `name`：动词名（如 `"serve"`）。
- `subparser_init(subparsers)`：把自己挂到 argparse 树上（声明自己的参数）。
- `cmd(args)`：实际执行逻辑（真正干活的地方）。
- `validate(args)`（可选）：解析后、执行前的校验。

#### 4.1.3 源码精读

**入口脚本的来源**。`vllm` 命令并不是凭空出现的，而是 pip 根据 `pyproject.toml` 自动生成的：

[pyproject.toml:L43-L44](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L43-L44) —— 声明 `vllm = "vllm.entrypoints.cli.main:main"`，意味着你在终端敲 `vllm`，等价于执行 `vllm.entrypoints.cli.main` 模块里的 `main()` 函数。

**子命令基类**：

[types.py:L13-L29](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/types.py#L13-L29) —— 这是所有子命令的「模板」。注意 `subparser_init` 和 `cmd` 都是必须由子类实现的（否则抛 `NotImplementedError`），而 `validate` 默认什么都不做。

**总入口 main() 的核心**：

[main.py:L17-L99](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L17-L99) —— 整个 `main()` 函数。重点看这几段：

- 文件开头注释 [main.py:L3-L6](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L3-L6) 明确说：所有子模块都必须在 `main()` 内部**懒加载**，避免过早 import 导致的破坏。这就是为什么 `import` 语句都在函数体里、而不是文件顶部。
- [main.py:L30-L37](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L30-L37) —— `CMD_MODULES` 是所有子命令模块的清单。注意这里包含 `serve`、`openai`（提供 chat/complete）、`launch`、`benchmark.main`、`collect_env`、`run_batch`。
- [main.py:L39](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L39) —— `cli_env_setup()` 调用，做环境初始化（详见 4.1.4 后的说明）。
- [main.py:L85-L91](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L85-L91) —— 这是分发的关键。对每个子命令模块调用 `cmd_init()`（返回一个或多个子命令对象），再调 `subparser_init(subparsers)` 挂参数，并用 `set_defaults(dispatch_function=cmd.cmd)` 把「动词 → 执行函数」的映射记到 namespace 上。
- [main.py:L92-L99](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L92-L99) —— 解析后：若匹配到子命令先 `validate(args)`，再 `args.dispatch_function(args)` 执行；否则打印帮助。

> 全部子命令一览（由各 `cmd_init()` 返回）：
> `serve`（serve.py）、`chat` 与 `complete`（openai.py）、`launch` 与 `render`（launch.py）、`run-batch`（run_batch.py）、`collect-env`（collect_env.py）、`bench`（benchmark/main.py）。

`cli_env_setup()` 做了什么？看实现：

[api_utils.py:L149-L167](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/serve/utils/api_utils.py#L149-L167) —— 它把多进程启动方式默认设为 `spawn`。原因是默认的 `fork` 与部分加速器（CUDA 等）不兼容。但只在 CLI 入口设，是因为改成 `spawn` 可能破坏把 vLLM 当库用的代码（`spawn` 要求代码有 `if __name__ == "__main__":` 保护）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`vllm` 命令是 console script」「子命令是懒加载分发」这两件事。

**操作步骤**：

1. 在已安装 vLLM 的环境中执行：

   ```bash
   which vllm
   vllm --version
   vllm --help
   ```

2. 观察输出里列出的子命令列表，对照本讲列出的 `serve / chat / complete / launch / render / run-batch / collect-env / bench`。

3. 再执行 `vllm serve --help`，观察它会列出一大堆参数（这是 4.2 会讲到的 `make_arg_parser` 注册的）。

**需要观察的现象**：

- `which vllm` 应指向 Python 环境的 bin 目录（说明它是 pip 生成的入口脚本，不是仓库里的某个文件）。
- `vllm --help` 的末尾会提示可用子命令；不带任何子命令时 `main.py` 走 `parser.print_help()` 分支。

**预期结果**：你能从帮助输出中确认子命令结构，并理解每个动词背后对应一个 `CLISubcommand` 子类。

> 如果环境没有 GPU 或没装好 vLLM，本步骤为「待本地验证」；可改为阅读 [main.py:L75-L99](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L75-L99) 复述分发逻辑。

#### 4.1.5 小练习与答案

**练习 1**：如果要在 vLLM 里新增一个 `vllm hello` 子命令，至少要实现 `CLISubcommand` 的哪几个方法？还要做哪一步登记？

参考答案：实现 `name`（设为 `"hello"`）、`subparser_init()`（声明参数）、`cmd(args)`（执行逻辑）；可选实现 `validate()`。登记步骤是：写一个提供 `cmd_init()` 的模块，在 `main.py` 的 `CMD_MODULES` 列表里加上它。分发逻辑由 `main()` 自动处理，无需改动 `if/elif`。

**练习 2**：为什么 `main.py` 顶部注释要求子模块必须在 `main()` 内部懒加载？

参考答案：为了避免「过早 import 破坏」。CLI 不同子命令依赖的重型模块不同，如果在模块顶层就全部 import，任何一个 import 出错或耗时长，都会拖慢/破坏整个 CLI 启动。延迟到 `main()` 内部、且只在需要时加载，能隔离故障、加快冷启动。

---

### 4.2 serve 子命令与参数处理

#### 4.2.1 概念说明

`vllm serve` 是 vLLM 在线服务的核心入口，用来拉起一个 OpenAI 兼容的 HTTP 服务。它是 `serve.py` 里的 `ServeSubcommand` 类。

`serve.py` 本身只负责「指挥」：决定走哪条部署路线（单服务？多服务？无头？多端口负载均衡？），把真正的苦力活（建 FastAPI app、跑 uvicorn、跑引擎）委托给 `api_server.py`。换句话说，`serve.py` 是一个**调度层/编排层**，它读参数、做决策、再分派。

#### 4.2.2 核心流程

`ServeSubcommand.cmd(args)` 的决策流程（伪代码）：

```text
cmd(args):
    # 1. 位置参数 model_tag 优先（vllm serve <model> 里的 <model>）
    if args.model_tag is not None:
        args.model = args.model_tag

    # 2. gRPC 模式：另走一条路
    if args.grpc:
        uvloop.run(serve_grpc(args)); return

    # 3. 计算 api_server_count（决定起几个 API 服务进程）
    #    根据 headless / 多端口 / 外部LB / 混合LB 等标志推算默认值
    compute_and_validate_api_server_count(args)

    # 4. 根据部署模式分派
    if is_multi_port:           run_dp_supervisor(args)
    elif api_server_count < 1:  run_headless(args)        # 无头模式
    elif count > 1 or rust_front: run_multi_api_server(args)
    else:                       uvloop.run(run_server(args))  # 单服务，最常见
```

最常见、最简单的路径就是最后那条 `else`：`uvloop.run(run_server(args))`，在本进程里跑一个 API 服务。多服务、无头等是进阶部署场景。

#### 4.2.3 源码精读

**serve 子命令类定义**：

[serve.py:L44-L47](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L44-L47) —— `class ServeSubcommand(CLISubcommand)`，`name = "serve"`。

**位置参数 model_tag 的处理**：

[serve.py:L49-L59](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L49-L59) —— 注意开头两行：如果用户用位置参数 `vllm serve facebook/opt-125m` 指定了 `model_tag`，就把它赋给 `args.model`（位置参数优先）。紧接着是 gRPC 分支判断。

**api_server_count 的推算与校验**（这是 serve.py 最复杂的一段）：

[serve.py:L65-L141](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L65-L141) —— 这段在根据「无头模式、多端口负载均衡、外部负载均衡、混合负载均衡」等各种数据并行部署形态，推算出该起几个 API 服务进程（`api_server_count`）。你不需要现在记住所有分支，只要记住：**这段代码的产出是一个明确的 `api_server_count` 数字**，它决定了走哪条部署路线。

**最终分派（四条路线）**：

[serve.py:L143-L152](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L143-L152) —— 这是 `cmd()` 的收尾，根据上面的推算结果选择执行函数：

| 条件 | 执行函数 | 含义 |
| --- | --- | --- |
| `is_multi_port` | `run_dp_supervisor(args)` | 多端口外部负载均衡，起一个 supervisor |
| `api_server_count < 1` | `run_headless(args)` | 无头模式，只起引擎不起 HTTP 服务 |
| `count > 1` 或 Rust 前端 | `run_multi_api_server(args)` | 多个 API 服务进程 |
| 其它（默认） | `uvloop.run(run_server(args))` | 单服务，本进程直接跑 |

**validate 与 subparser_init**：

[serve.py:L154-L170](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L154-L170) —— `validate` 调用 `validate_parsed_serve_args(args)` 做快速校验（如 chat template 是否合法、`--enable-auto-tool-choice` 是否配了 `--tool-call-parser`）。`subparser_init` 把 serve 子命令挂到 argparse 树上，关键是 [serve.py:L168](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L168) 调用 `make_arg_parser(serve_parser)`——这才是真正注册 serve 所有参数的地方。

**参数到底在哪定义？** `serve` 子命令的参数不是手写在 serve.py 里的，而是集中在 `cli_args.py` 的 `make_arg_parser`：

[cli_args.py:L358-L402](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L358-L402) —— 关键参数：

- [cli_args.py:L365-L370](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L365-L370) —— `model_tag`，位置参数（`nargs="?"`，可选）。这就是为什么 `vllm serve <model>` 不用写 `--model`。
- [cli_args.py:L371-L377](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L371-L377) —— `--headless`，无头模式开关。
- [cli_args.py:L378-L385](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L378-L385) —— `--api-server-count` / `-asc`，决定起几个 API 服务进程。
- [cli_args.py:L386-L398](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L386-L398) —— `--config`（从 YAML 读参数）和 `--grpc`（起 gRPC 服务）。
- [cli_args.py:L399-L401](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L399-L401) —— `FrontendArgs.add_cli_args` 和 `AsyncEngineArgs.add_cli_args` 注册其余海量参数（host/port、TP/DP、量化、采样默认值等）。这种「把参数定义委托给专用类」的做法，正是注释里强调的「避免重复、单一来源」。

**校验函数**：

[cli_args.py:L405-L418](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L405-L418) —— `validate_parsed_serve_args`：只在 `subparser == "serve"` 时做检查，校验 chat template、tool parser 之间的依赖关系。

#### 4.2.4 代码实践

**实践目标**：搞清楚 `vllm serve` 的参数处理流程，能复述「位置参数 model_tag → args.model → 引擎配置」这条链。

**操作步骤**：

1. 阅读 [serve.py:L49-L59](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L49-L59)，确认位置参数 `model_tag` 会被赋给 `args.model`。
2. 阅读 [cli_args.py:L358-L402](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/cli_args.py#L358-L402)，列出 serve 子命令直接定义的 6 个核心参数（`model_tag`、`--headless`、`--api-server-count`、`--config`、`--grpc`，加上委托给 `FrontendArgs`/`AsyncEngineArgs` 的）。
3. （可选）若有可用环境，尝试启动一个小模型：

   ```bash
   vllm serve facebook/opt-125m --port 8000
   ```

**需要观察的现象**：

- 启动日志会打印 `non-default args: {...}`（由 `setup_server` 里的 `log_non_default_args` 输出），你可以从中看到 `model` 字段确实被填成了位置参数的值。
- 不写 `--model` 也能跑，因为 `model_tag` 作为位置参数被解析进去了。

**预期结果**：能用一句话说明「`vllm serve <model>` 的 `<model>` 是位置参数 `model_tag`，在 `cmd()` 开头被赋给 `args.model`，之后传给引擎参数对象」。

> 无 GPU 环境下第 3 步为「待本地验证」，前两步纯源码阅读即可完成。

#### 4.2.5 小练习与答案

**练习 1**：`vllm serve` 的参数为什么不全写在 `serve.py` 里，而要交给 `make_arg_parser`？

参考答案：为了「单一来源、避免重复」。`serve`、`openai`（chat/complete）等子命令、以及 `api_server.py` 直接运行时，都需要同一套引擎/前端参数。把这些参数定义集中到 `make_arg_parser`（再委托给 `FrontendArgs`/`AsyncEngineArgs`），所有入口共享同一份定义，改一处即可。

**练习 2**：默认情况下（单卡、不带任何并行参数），`cmd()` 走的是哪条分支？为什么？

参考答案：走最后一条 `else`，即 `uvloop.run(run_server(args))`，在本进程里跑单个 API 服务。因为默认 `api_server_count` 为 None，经推算会被设为 `data_parallel_size`（默认 1），不满足 `>1`、不满足无头、不满足多端口，落到单服务分支。

---

### 4.3 HTTP 服务启动器与生命周期

#### 4.3.1 概念说明

当 `serve.py` 决定走单服务路线、调用 `run_server(args)` 后，最终会落到 `launcher.py` 的 `serve_http()`。这里的「launcher」指的是 **HTTP 服务的启动器**：它负责把一个 FastAPI 应用真正「跑」起来（用 uvicorn），并管理服务生命周期（启动、信号、优雅关闭、看门狗）。

要注意：`serve_http` 不关心模型、不关心 KV 缓存、不关心调度。它只关心「怎么把一个 FastAPI app 挂到 socket 上、用 uvicorn 跑起来、并在出错或收到 Ctrl+C 时干净地退出」。它是 vLLM 服务端**最外层的 HTTP 壳**。

#### 4.3.2 核心流程

`serve_http(app, sock, ...)` 的生命周期（伪代码）：

```text
serve_http(app, sock):
    1. 打印所有可用路由（/v1/chat/completions 等）
    2. 构造 uvicorn.Config(app, ...)，设置 HTTP header 上限等安全默认值
    3. 启动两个后台 task：
         - server_task: uvicorn server.serve(sockets=[sock])  # 真正监听端口
         - watchdog_task: 每隔 5s 检查引擎是否挂掉，挂了就让 server 退出
    4. 注册 SIGINT/SIGTERM 信号处理 → 触发 shutdown_event
    5. await server_task 直到关闭：
         - 收到信号 → handle_shutdown()：先优雅关引擎（drain/abort），再让 server 退出
         - 端口被占 → 打印占用进程信息帮助排查
```

关键点：**端口绑定（bind）发生在 `serve.py`/`api_server.py` 调用方手里**，`serve_http` 拿到的是已经绑定好的 socket，直接交给 uvicorn。这样能在拉起引擎前就抢到端口，避免和其它进程竞争（代码注释里也提到这是为了规避 Ray 的竞态）。

#### 4.3.3 源码精读

**serve_http 主体**：

[launcher.py:L26-L82](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L26-L82) —— 整个函数。重点段落：

- [launcher.py:L37-L57](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L37-L57) —— 启动时遍历 `app.routes` 打印所有路由（先打印带 methods 的 POST 端点，再打印其它端点）。这就是你在日志里看到的 `Route: /v1/chat/completions, Methods: POST` 的来源。
- [launcher.py:L60-L76](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L60-L76) —— 设置 h11 的 header 上限安全默认值，构造 `uvicorn.Config` 和 `uvicorn.Server`。
- [launcher.py:L81-L82](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L81-L82) —— 启动 watchdog task 和真正的 server task。`server.serve(sockets=[sock])` 接收的是已绑定的 socket。

**优雅关闭**：

[launcher.py:L107-L146](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L107-L146) —— 收到信号后：先 `engine_client.shutdown(timeout=...)`（按 `shutdown_timeout` 决定是 drain 还是 abort），再令 `server.should_exit = True` 并取消 server_task。这说明 vLLM 的关闭是「先停引擎、后停 HTTP」的有序过程。

**看门狗 watchdog**：

[launcher.py:L168-L190](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L168-L190) —— `watchdog_loop` 每 5 秒检查一次引擎状态；`terminate_if_errored` 在引擎进入错误态且不在运行时，令 `server.should_exit = True`。注意有个开关 `VLLM_KEEP_ALIVE_ON_ENGINE_DEATH`：默认关闭，即引擎挂了就拉整个服务一起退出（fail-fast）。

#### 4.3.4 代码实践

**实践目标**：理解 `serve_http` 是「最外层 HTTP 壳」，并能解释端口绑定与信号关闭的顺序。

**操作步骤**：

1. 阅读 [launcher.py:L81-L82](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L81-L82) 和 4.4 的 [api_server.py:L638-L642](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L638-L642)，确认「socket 在 `setup_server` 里绑定 → 传给 `serve_http` → 交给 uvicorn」。
2. 阅读 [launcher.py:L121-L146](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/launcher.py#L121-L146)，画出关闭顺序。
3. （可选）若有服务在跑，按 Ctrl+C（发送 SIGINT），观察日志中 `[shutdown] API server: ...` 系列信息。

**需要观察的现象**：

- 启动时日志先列出路由，再打印 `API server: waiting for HTTP server to start`，最后 `HTTP server started`。
- 关闭时日志显示先停引擎 client，再关 HTTP server。

**预期结果**：能复述「socket 先绑定、后交给 uvicorn；关闭时先 drain 引擎、再退出 HTTP」。

> 无 GPU 环境为「待本地验证」，源码阅读部分即可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么要在拉起引擎之前就绑定端口（而不是等引擎就绪后再绑）？

参考答案：为了规避竞态（race condition）。代码注释提到这是为了避开与 Ray 的竞争（见 issue #8204）：提前抢到端口并绑定 socket，可以让外部客户端/调度器尽早发现端口是否可用；如果等引擎（很慢）就绪后才绑，可能已经被别的进程占了，白白浪费了漫长的引擎初始化时间。

**练习 2**：`watchdog_loop` 的作用是什么？关闭它的开关是哪个环境变量？

参考答案：watchdog 每 5 秒检查引擎是否进入错误态，一旦引擎挂掉就令 uvicorn server 退出，避免「HTTP 还在但引擎已死」的僵尸服务。开关是 `VLLM_KEEP_ALIVE_ON_ENGINE_DEATH`，默认关闭（即引擎死则服务退）。

---

### 4.4 serve 与底层 api_server 的关系

#### 4.4.1 概念说明

`serve.py` 是「调度/编排」，`api_server.py` 是「真正干活」。二者通过一个关键函数 `run_server` 衔接。理解这一层，你就能回答「vllm serve 到底在哪里建 FastAPI、在哪里建引擎、在哪里监听端口」。

`api_server.py` 同时身兼两职：

- 它是被 `serve.py` 调用的库（`run_server` / `setup_server`）。
- 它自己也能作为脚本直接运行（文件末尾 `if __name__ == "__main__"`），注释明确说要和 `main.py` 的 CLI 入口保持同步。

#### 4.4.2 核心流程

单服务路线的调用链（最常见路径）：

```text
serve.py: cmd(args)
  └─ uvloop.run(run_server(args))            # serve.py:L152
       └─ run_server(args)                     # api_server.py:L751
            ├─ setup_server(args, reuse_port=False)   # 绑定 socket、打印版本与参数
            └─ run_server_worker(...)
                 └─ async with build_async_engine_client(args) as engine_client:
                      └─ build_and_serve(engine_client, listen_address, sock, args)
                           ├─ build_app(args, supported_tasks, model_config)  # 建 FastAPI app
                           ├─ init_app_state(engine_client, app.state, ...)    # 注入引擎等状态
                           └─ serve_http(app, sock, ...)                        # launcher.py 启动 uvicorn
```

`build_async_engine_client` 负责把 `args` 变成一个异步引擎客户端（`EngineClient`），它会去构造 `AsyncEngineArgs`、进而创建 V1 引擎。这一步会真正加载模型、分配 KV 缓存、起 worker 进程——这些属于后续讲义（u3/u5）的内容，本讲只要知道「引擎客户端在这里被创建并注入 app.state」。

#### 4.4.3 源码精读

**run_server：单服务入口**：

[api_server.py:L751-L764](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L751-L764) —— `run_server` 做三件事：装饰日志、注册一个初始化期的 SIGTERM 中断器、调 `setup_server` 拿到 socket，再交给 `run_server_worker`。

**setup_server：绑定端口 + 打印信息**：

[api_server.py:L621-L655](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L621-L655) —— 关键点：

- [api_server.py:L624-L625](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L624-L625) —— 打印版本/模型 logo 与非默认参数。
- [api_server.py:L633](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L633) —— `validate_api_server_args(args)`。
- [api_server.py:L638-L642](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L638-L642) —— **端口绑定就在这里**（根据是否 `--uds` 选 Unix socket 或 TCP socket），并把绑定好的 socket 返回。
- [api_server.py:L646](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L646) —— `set_ulimit()` 提高 ulimit，避免 uvicorn 在高并发时丢请求（注释明确说是为了规避 footgun）。

**run_server_worker：建引擎 + 启服务**：

[api_server.py:L767-L789](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L767-L789) —— 用 `async with build_async_engine_client(args)` 创建引擎客户端，再 `build_and_serve(...)` 构建 FastAPI app 并启动 uvicorn。注意 `finally` 里 `sock.close()`，保证 socket 被释放。

**build_async_engine_client**：

[api_server.py:L110-L137](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L110-L137) —— 把 `args` 转成 `AsyncEngineArgs`，再委托 `build_async_engine_client_from_engine_args` 创建真正的引擎。开头 [api_server.py:L116-L123](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L116-L123) 还有个优化：当多进程方式是 `forkserver` 时，预导入重型模块，加快子进程启动。

**api_server.py 也能直接运行**：

[api_server.py:L792-L804](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L792-L804) —— 文件末尾 `if __name__ == "__main__"` 段落，注释 [api_server.py:L793-L795](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L793-L795) 强调要和 CLI 入口同步（同样调 `cli_env_setup()`、`make_arg_parser`、`validate_parsed_serve_args`、`uvloop.run(run_server(args))`）。

#### 4.4.4 代码实践

**实践目标**：把「serve.py → api_server.py → launcher.py」三层串起来，画出完整调用链。

**操作步骤**：

1. 在源码里依次定位：`serve.py:152`（`uvloop.run(run_server(args))`）→ `api_server.py:751`（`run_server`）→ `api_server.py:621`（`setup_server`）→ `api_server.py:767`（`run_server_worker`）→ `api_server.py:678`/`684`（`build_app` + `serve_http`）→ `launcher.py:82`（uvicorn `server.serve`）。
2. 画一张从上到下的调用链图（参考 4.4.2 的伪代码块）。
3. 标注：哪一步绑定端口？哪一步创建引擎？哪一步真正监听端口？

**需要观察的现象**：

- 绑定端口：`setup_server`（api_server.py:L638-L642）。
- 创建引擎：`build_async_engine_client`（api_server.py:L778）。
- 真正监听端口：`serve_http` 里的 `server.serve(sockets=[sock])`（launcher.py:L82）。

**预期结果**：能清晰地用一句话回答本讲开头的易混问题——「`serve.py` 决策分派、`api_server.py` 建引擎与 FastAPI app 并绑定 socket、`launcher.py` 用 uvicorn 监听端口跑起来」。

> 本实践为纯源码阅读，无需运行环境。

#### 4.4.5 小练习与答案

**练习 1**：`api_server.py` 既能被 `serve.py` 当库调用，又能 `python api_server.py` 直接跑，它怎么保证两种方式行为一致？

参考答案：文件末尾的 `if __name__ == "__main__"` 段落复刻了 CLI 的关键步骤——`cli_env_setup()`、`make_arg_parser`、`validate_parsed_serve_args`、`uvloop.run(run_server(args))`，并在注释里要求与 `main.py` 保持同步。这样两种入口走的是同一条 `run_server` 主干。

**练习 2**：在 `run_server_worker` 里，引擎客户端是用什么语法管理生命周期的？为什么？

参考答案：用 `async with build_async_engine_client(args) as engine_client:`（上下文管理器）。原因是引擎需要申请显存、起子进程、建 ZMQ 通信等重型资源，用 `async with` 能保证无论是正常结束还是异常，都会触发清理与关闭，避免资源泄漏。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，完成一次「从命令行到 HTTP 服务的全链路追踪」，并产出一页调「用链笔记」。

**步骤**：

1. **追踪命令起源**：从 [pyproject.toml:L43-L44](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L43-L44) 找到 `vllm` 入口指向 `main:main`，确认它是 console script。
2. **追踪子命令分发**：在 [main.py:L85-L97](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/main.py#L85-L97) 确认 `serve` 子命令的 `cmd` 是如何被 `dispatch_function` 调用的。
3. **追踪参数处理**：在 [serve.py:L49-L59](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L49-L59) 确认位置参数 `model_tag` → `args.model`，并在 [serve.py:L143-L152](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L143-L152) 找到默认走 `run_server`。
4. **追踪服务启动**：顺着 `run_server` → `setup_server`（绑定端口）→ `build_async_engine_client`（建引擎）→ `build_app` + `serve_http`（uvicorn 监听）走完。
5. **（可选）实跑验证**：若有环境，执行 `vllm serve facebook/opt-125m --port 8000`，对照日志确认路由打印、参数打印、`HTTP server started` 三类信息，然后用 Ctrl+C 观察优雅关闭日志。

**产出**：一页笔记，包含一张调用链图（`vllm` → `main` → `serve.cmd` → `run_server` → `setup_server`/`build_async_engine_client` → `serve_http` → uvicorn），并标注每一步所在的「文件:行号」。

> 第 5 步无 GPU 环境为「待本地验证」；前 4 步纯源码阅读即可完成全部追踪。

## 6. 本讲小结

- `vllm` 命令是 `pyproject.toml` 声明的 console script，指向 `vllm.entrypoints.cli.main:main`。
- `main.py` 用「子命令基类 `CLISubcommand` + `CMD_MODULES` 清单」实现可扩展的分发：每个动词一个类，新增功能只需登记，不改分发逻辑。
- 所有子模块在 `main()` 内部**懒加载**，CLI 入口还会通过 `cli_env_setup()` 把多进程方式设为 `spawn`。
- `vllm serve` 由 `serve.py` 的 `ServeSubcommand` 实现：它解析参数（`model_tag` 优先）、推算 `api_server_count`、再分派到「多端口 / 无头 / 多服务 / 单服务」四条路线，默认单服务走 `uvloop.run(run_server(args))`。
- serve 的参数定义集中在 `cli_args.py` 的 `make_arg_parser`，再委托给 `FrontendArgs`/`AsyncEngineArgs`，做到单一来源。
- 三层职责清晰：`serve.py` 决策编排 → `api_server.py` 建引擎与 FastAPI app 并绑定 socket → `launcher.py` 的 `serve_http` 用 uvicorn 监听端口并管理生命周期（watchdog、优雅关闭）。

## 7. 下一步学习建议

- **学下一讲 u2-l3（OpenAI 兼容客户端调用示例）**：本讲只讲到「服务怎么起来」，下一讲教你用 OpenAI SDK 向这个服务发请求，形成「服务端 ↔ 客户端」的完整闭环。
- **进阶到 u3-l1（V1 多进程架构总览）**：本讲多次提到 `build_async_engine_client`、worker 进程、ZMQ，但要真正理解「为什么要把调度和执行分到不同进程」，需要看 V1 架构总览。
- **进阶到 u3-l4（AsyncLLM 引擎客户端）**：本讲里 `app.state.engine_client` 的真实类型就是 `AsyncLLM`，下一层讲义会讲透它如何把请求转发给 EngineCore 进程。
- **继续阅读源码**：想了解多服务/无头部署的细节，可读 [serve.py:L177-L258](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L177-L258)（`run_headless`）和 [serve.py:L261-L404](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/cli/serve.py#L261-L404)（`run_multi_api_server`）。
