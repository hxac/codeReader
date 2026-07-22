# 安装与首次运行：sglang serve

## 1. 本讲目标

上一讲（u1-l1）我们建立了 SGLang 的全局视图：它是一个高性能 LLM/多模态推理服务框架，前端 `sglang.lang`、后端 `sglang.srt`。本讲目标非常具体——**亲手把它跑起来，并发出第一个请求**。

学完本讲，你应当能够：

- 读懂 `pyproject.toml`，说清 SGLang 的安装方式、Python 版本要求与关键依赖；
- 追踪 `sglang` 这个命令是如何从 `[project.scripts]` 映射到 `sglang.cli.main:main` 的；
- 解释 `sglang serve` 在 `cli/main.py` → `cli/serve.py` → `launch_server.run_server` 这条分发链路上的每一跳；
- 用 `sglang serve --model-path <小模型>` 启动一个推理服务，并用 curl、openai SDK 或前端 DSL 三种方式之一发出请求并打印返回。

## 2. 前置知识

- **Python 包与控制台脚本**：当你 `pip install` 一个包后，能在终端直接敲的命令（比如 `sglang`、`black`、`pytest`）叫「控制台脚本（console script）」。它的来源在包配置里声明，pip 安装时会在 `bin/` 下生成一个可执行入口。本讲你会看到 SGLang 的命令就来自这里。
- **argparse 子命令**：`git serve`、`docker run` 这种「主命令 + 子命令」结构，在 Python 里通常用 `argparse` 的 `add_subparsers` 实现。`sglang serve`、`sglang version` 就是子命令。
- **OpenAI 兼容 API**：很多推理服务（SGLang、vLLM 等）都模仿 OpenAI 的 HTTP 接口，提供 `/v1/chat/completions`、`/v1/completions` 等端点。这样任何用 openai SDK 写的客户端，只要换个 `base_url`，就能连到本地的 SGLang 服务上。
- **前台进程 vs HTTP 服务**：`sglang serve` 会启动一个常驻 HTTP 服务进程（默认监听 30000 端口），你可以从另一个终端反复发请求；这与「脚本里 import 一次跑一次」的进程内用法不同。本讲聚焦前者，进程内用法（`Engine` 类）留到 u1-l4。

> 名词速查：**prefill / decode** 是大模型推理的两阶段（先算 prompt、再逐 token 生成）；本讲不会深入，你只需知道请求最终会进入这个流水线即可，u3 会专门讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/pyproject.toml` | 声明依赖、Python 版本、可选依赖分组（extras），以及最重要的 `[project.scripts]`——它定义了 `sglang` 这个命令。 |
| `python/sglang/cli/main.py` | 命令行总入口，用 argparse 注册 `serve / generate / version` 子命令并分发。 |
| `python/sglang/cli/serve.py` | `serve` 子命令的真正实现：解析模型类型、区分语言模型与扩散模型、最终调用 `run_server`。 |
| `python/sglang/launch_server.py` | `run_server()` 所在，真正拉起 HTTP 服务进程。 |
| `python/sglang/srt/server_args.py` | `ServerArgs` 配置类，定义了上百个启动参数（端口默认 30000 等）。 |
| `python/sglang/lang/api.py` + `lang/backend/runtime_endpoint.py` | 前端 DSL 的公开 API（`function / Runtime / set_default_backend / RuntimeEndpoint`），示例脚本靠它们连服务。 |
| `examples/frontend_language/quick_start/local_example_chat.py` | 官方「最快上手」示例：用 DSL 写多轮问答并打印返回。 |

## 4. 核心概念与源码讲解

### 4.1 安装 SGLang：从 pyproject 看依赖与命令入口

#### 4.1.1 概念说明

要跑通 SGLang，第一件事是搞清楚「装什么、装完能得到什么命令、依赖有多重」。这一切都在 `python/pyproject.toml` 里：

- **Python 版本**：`requires-python = ">=3.10"`。
- **依赖很重**：核心依赖里直接锁定了 `torch==2.11.0`、`transformers==5.12.1`、`flashinfer_python[cu13]`、`sglang-kernel`、`cuda-python>=13.0` 等——也就是说，**SGLang 的主安装面向的是带 NVIDIA GPU（CUDA）的环境**，CPU-only 不是主线。
- **可选依赖分组（extras）**：`pip install "sglang[all]"` 会额外装上 `diffusion / http2 / tracing` 三组能力；`[test]` 是开发测试用的。
- **命令入口**：`[project.scripts]` 里写着 `sglang = "sglang.cli.main:main"`——这正是你能在终端敲 `sglang serve` 的根本原因。

#### 4.1.2 核心流程

安装到能敲命令的链路：

1. `pip install "sglang[all]"`（从 PyPI）或源码 `pip install -e "python[all]"`。
2. pip 解析 `pyproject.toml` 的 `dependencies`，拉取 torch / flashinfer / sgl-kernel 等重依赖。
3. pip 读取 `[project.scripts]`，在 Python 环境的 `bin/`（Windows 下是 `Scripts\`）生成 `sglang` 可执行入口。
4. 该入口指向 `sglang.cli.main:main`——你敲 `sglang` 时实际调用的就是它。

#### 4.1.3 源码精读

Python 版本与定位说明：[python/pyproject.toml:6-11](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L6-L11)——注意第 10 行 `requires-python = ">=3.10"`。

核心依赖（节选几条最关键的，体会技术栈）：[python/pyproject.toml:18-91](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L18-L91)。其中 `torch==2.11.0`、`flashinfer_python[cu13]==0.6.14`、`sglang-kernel==0.4.5`、`transformers==5.12.1`、`fastapi` + `uvicorn`、`pyzmq>=25.1.2` + `msgspec`、以及结构化输出三件套 `xgrammar==0.2.1` / `outlines==0.1.11` / `llguidance>=1.7.6`。这些依赖直接呼应了 u1-l1 介绍的各特性（HTTP 服务、多进程 ZMQ 通信、高性能算子、文法约束解码）。

可选依赖分组与 `all`：[python/pyproject.toml:98-178](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L98-L178)，其中 `all = ["sglang[diffusion]", "sglang[http2]", "sglang[tracing]"]` 见 [python/pyproject.toml:174-178](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L174-L178)。

命令入口的定义：[python/pyproject.toml:188-190](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L188-L190)。这两行说明：`sglang` 命令 = 调用 `sglang.cli.main` 模块里的 `main` 函数；旁边还注册了 `killall_sglang`，用来批量结束残留的 sglang 进程。

#### 4.1.4 代码实践

**目标**：确认安装成功，并亲手验证命令入口的映射。

**步骤**：

1. 安装（二选一，按你的环境）：
   ```bash
   # 方式 A：从 PyPI 安装（推荐先用这个）
   pip install --upgrade pip
   pip install "sglang[all]"
   ```
   ```bash
   # 方式 B：从本仓库源码可编辑安装（适合后续改源码阅读）
   pip install -e "python[all]"
   ```
2. 验证命令可用：`sglang version`。
3. 在 `python/pyproject.toml` 第 188-190 行找到 `sglang = "sglang.cli.main:main"`，用 `pip show sglang` 或查看 `which sglang` 确认入口文件确实被生成。

**需要观察的现象 / 预期结果**：

- `sglang version` 会打印版本号与 git revision（这个子命令实现在 `cli/main.py:7-9`，下一节会看到）。
- 安装耗时较长且会拉取较大的 GPU 相关 wheel——这是正常的。

**待本地验证**：由于依赖 torch 2.11 与 CUDA 13 系 wheel，具体的 CUDA 驱动版本要求、是否能纯 CPU 跑，需要你对照本机环境确认。若 `flashinfer / sgl-kernel` 安装失败，通常是 GPU/驱动/CUDA 版本不匹配，而非 SGLang 本身的问题。

#### 4.1.5 小练习与答案

- **练习 1**：不安装 `[all]`，直接 `pip install sglang`（不带 extras）会发生什么？基础 `dependencies` 里已经包含 torch / fastapi / xgrammar 等吗？
  - **答案**：会安装成功，但**不会**额外装 `diffusion`（扩散模型）、`http2`（granian）、`tracing`（OpenTelemetry）。核心推理所需的 torch、flashinfer、fastapi、xgrammar 等都在基础 `dependencies` 里，所以纯 LLM 推理服务能跑；只有用到扩散或高级特性时才需要对应 extra。
- **练习 2**：`sglang` 命令对应的可调用对象是？
  - **答案**：`sglang.cli.main:main`（见 `[project.scripts]`）。

---

### 4.2 命令分发：cli/main.py 如何识别 `serve`

#### 4.2.1 概念说明

`main()` 是 `sglang` 命令的总入口。它的职责很轻：用 argparse 注册几个子命令，把 `serve` 之后的所有参数原样透传给对应的处理函数。这里有一个关键设计——**懒加载（lazy import）**：处理 `serve` 的代码只在用户真的敲了 `sglang serve` 时才 import。这能让 `sglang version` 这类轻命令不必加载一整套 GPU 运行时。

#### 4.2.2 核心流程

```
终端: sglang serve --model-path Qwen/Qwen2.5-0.5B-Instruct --port 30000
  │
  ▼
sglang.cli.main.main()          # argparse 解析出 subcommand="serve"
  │  args.subcommand == "serve"
  ▼
from sglang.cli.serve import serve
serve(args, extra_argv)         # extra_argv 携带 --model-path ... --port ...
```

`serve` 与 `generate` 用 `add_help=False` 创建，并配合 `parse_known_args`——这样未识别的参数会被收进 `extra_argv` 一起带走，交给下游（`ServerArgs`）再解析。

#### 4.2.3 源码精读

`main()` 的完整定义：[python/sglang/cli/main.py:12-46](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/main.py#L12-L46)。要点：

- `serve` 子命令注册：[python/sglang/cli/main.py:17-21](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/main.py#L17-L21)——注意 `add_help=False`，因为帮助信息要等服务类型确定后由 `serve()` 自己打印。
- `parse_known_args` 收集多余参数：[python/sglang/cli/main.py:35](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/main.py#L35)。
- 分发逻辑：[python/sglang/cli/main.py:37-46](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/main.py#L37-L46)——当 `args.subcommand == "serve"` 时，`from sglang.cli.serve import serve` 然后 `serve(args, extra_argv)`。这里的 `import` 写在分支内部，正是懒加载。
- `version` 子命令直接在本文件实现：[python/sglang/cli/main.py:7-9](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/main.py#L7-L9)。

#### 4.2.4 代码实践

**目标**：亲眼确认分发逻辑，并体会懒加载。

**步骤**：

1. 在 `main()` 的 `serve(args, extra_argv)` 调用前临时加一行打印（仅为观察，记得事后还原，本讲不修改源码作为最终结果）：
   ```python
   print("[debug] dispatching to serve with extra_argv =", extra_argv)
   ```
2. 运行：`sglang serve --model-path <某个已下载的小模型> --port 30000`（若手头无模型，可只跑 `sglang serve --help`，分发链路到 `serve()` 内部即会返回）。
3. 单独跑 `sglang version`，体会它**不会**走到 `serve()` 分支，也不需要加载 GPU 运行时。

**需要观察的现象 / 预期结果**：

- 步骤 1 的 `[debug]` 打印会先于真正的服务启动出现，证明执行确实进入了 `serve` 分支。
- `sglang version` 输出形如 `sglang version: x.y.z` 与 `git revision: 4a55fdb`。

**待本地验证**：步骤 2 是否能成功启动取决于模型与 GPU 是否就绪，这里只验证分发链路本身。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `serve` 子命令要用 `add_help=False` 并把参数透传，而不是在 `main()` 里把 `--model-path` 等都定义清楚？
  - **答案**：因为 `sglang serve` 既能启语言模型服务、也能启扩散模型服务，两者的参数集合完全不同，要等到 `serve()` 识别模型类型后才能决定用哪套参数。所以 `main()` 只做最薄的分发，具体参数交给 `ServerArgs`（语言模型）或扩散模型 parser 去解析。
- **练习 2**：`parse_known_args` 与 `parse_args` 的区别对 `sglang serve` 为什么重要？
  - **答案**：`parse_known_args` 允许出现「未识别参数」而不报错，把它们收进 `extra_argv`。`sglang serve` 后面会跟几十上百个 `ServerArgs` 参数，这些在 `main()` 阶段不必认识，必须透传下去。

---

### 4.3 `sglang serve` 的起点：serve() 与模型类型分发

#### 4.3.1 概念说明

`serve()` 是 `sglang serve` 的实质入口，也是整个 CLI 中信息最密集的一段。它做三件事：

1. **处理帮助**：`-h/--help` 时打印通用用法，并分别展示语言模型服务与扩散模型服务的帮助。
2. **解析模型类型**：通过 `--model-type {auto,llm,diffusion}`（默认 `auto`）决定走哪条服务路径；`auto` 时还会探测 `--model-path` 是不是扩散模型。
3. **拉起服务**：语言模型路径调用 `sglang.launch_server.run_server`；扩散模型路径走 `sglang.multimodal_gen`。

它还贴心地支持**位置式写模型路径**：`sglang serve Qwen/Qwen2.5-0.5B-Instruct` 等价于 `sglang serve --model-path Qwen/Qwen2.5-0.5B-Instruct`。

#### 4.3.2 核心流程

```
serve(args, extra_argv)
  │
  ├─ 命中 -h/--help？ → 打印双重帮助后 return
  │
  ├─ load_plugins()
  ├─ model_type, dispatch_argv = _extract_model_type_override(extra_argv)
  ├─ dispatch_argv, _ = _normalize_positional_model_path(dispatch_argv)
  ├─ model_path = get_model_path(dispatch_argv)
  │
  ├─ model_type == "diffusion" 或 auto 探测到扩散？
  │     └─ 是 → add_multimodal_gen_serve_args + execute_serve_cmd
  │     └─ 否 → 进入语言模型分支 ↓
  │
  └─ 语言模型分支：
        server_args = prepare_server_args(dispatch_argv)   # 构造 ServerArgs
        run_server(server_args)                            # 拉起 HTTP 服务（默认端口 30000）
  finally:
        kill_process_tree(os.getpid(), include_parent=False)  # 清理子进程
```

语言模型分支里默认 HTTP 端口是 30000（来自 `ServerArgs`：[python/sglang/srt/server_args.py:1063](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L1063)）。

#### 4.3.3 源码精读

`serve()` 全函数：[python/sglang/cli/serve.py:56-143](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L56-L143)。

- 帮助分支：[python/sglang/cli/serve.py:57-95](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L57-L95)——它先打印通用用法，再调用 `prepare_server_args(["--help"])` 展示语言模型服务参数。
- 模型类型解析与位置式路径规范化：[python/sglang/cli/serve.py:16-53](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L16-L53)，其中 `_normalize_positional_model_path` 在 [第 49-53 行](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L49-L53) 把 `serve <model>` 改写成 `serve --model-path <model>`。
- 插件加载与模型类型探测：[python/sglang/cli/serve.py:97-116](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L97-L116)。
- **语言模型分支（本讲的主路径）**：[python/sglang/cli/serve.py:134-141](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L134-L141)——`prepare_server_args(dispatch_argv)` 把一长串命令行参数变成 `ServerArgs` 对象，再交给 `run_server`。
- 收尾清理：[python/sglang/cli/serve.py:142-143](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L142-L143)，`finally` 块用 `kill_process_tree` 收掉所有子进程（SGLang 会派生 tokenizer / scheduler / detokenizer 等多个子进程，u2 会详讲）。

`run_server` 真正在此：[python/sglang/launch_server.py:15](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/launch_server.py#L15)，它会启动 FastAPI + uvicorn 构成的 HTTP 服务（`launch_server` 实现在 [python/sglang/srt/entrypoints/http_server.py:2638](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2638)）。HTTP server 与进程内 `Engine` 入口的对比，留到 u1-l4。

#### 4.3.4 代码实践

**目标**：把 `serve()` 的两条分支看清，并确认默认端口。

**步骤**：

1. 跑帮助，观察「双重帮助」结构：
   ```bash
   sglang serve --help
   ```
   预期会先打印 `Usage: sglang serve <model-name-or-path> ...`，再分别展示语言模型与扩散模型服务的参数。
2. 在 `serve.py` 第 134-141 行附近定位语言模型分支，确认它调用的是 `prepare_server_args` + `run_server`。
3. 在 `server_args.py:1063` 确认 `port` 默认值为 `30000`。

**需要观察的现象 / 预期结果**：

- `--help` 输出里能看到 `--tp / --dp / --chunked-prefill-size / --mem-fraction-static / --port` 等大量参数（u2-l2 会精讲这些字段）。
- 若不带 `--port` 启动，服务监听 30000。

**待本地验证**：`--help` 能否成功展示扩散模型帮助，取决于是否安装了 `sglang[diffusion]`；未安装时会打印提示而非崩溃（见 [serve.py:90-94](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L90-L94)）。

#### 4.3.5 小练习与答案

- **练习 1**：`--model-type diffusion` 与 `--model-type auto` 走的代码路径有何不同？
  - **答案**：`diffusion` 直接进入扩散模型分支，跳过自动探测（见 [serve.py:111-116](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L111-L116)）；`auto` 会先调用 `get_is_diffusion_model(model_path)` 探测，探测失败则回退到语言模型（LLM）分支。
- **练习 2**：为什么 `serve()` 用 `try/finally` 包裹并在 `finally` 里 `kill_process_tree`？
  - **答案**：SGLang 是多进程架构，`run_server` 会派生多个子进程。无论启动成功、报错还是被 Ctrl-C 中断，都需要彻底回收这些子进程，否则会留下僵尸进程占用显存/端口。

---

### 4.4 发送第一个请求：前端 DSL 示例与 OpenAI 兼容调用

#### 4.4.1 概念说明

服务跑起来后，怎么发请求？SGLang 提供**三种**姿势，对应不同场景：

1. **前端 DSL（`sgl.function` + `sgl.gen`）**：用 Python 描述结构化生成程序，最贴近 `local_example_chat.py` 这个官方示例。它需要一个 backend：要么用 `sgl.Runtime(...)` **自启一个服务子进程**，要么用 `sgl.RuntimeEndpoint(base_url=...)` **连接已运行的服务**。
2. **curl**：直接打 OpenAI 兼容端点 `/v1/chat/completions`，最原始、最适合调试。
3. **openai SDK**：把 `base_url` 指向本地服务，复用现有 OpenAI 客户端代码。

本节先把示例脚本逐行读懂，再给出连接到 `sglang serve` 的两种写法。

#### 4.4.2 核心流程

示例脚本内部的两条路线：

```
路线 A：脚本自启服务（local_example_chat.py 原样）
  sgl.Runtime(model_path=...)          # 在子进程里 launch_server
  sgl.set_default_backend(runtime)     # 把它设为默认 backend
  @sgl.function 定义的函数 .run(...)    # 经 HTTP 打到自启的服务
  runtime.shutdown()

路线 B：连接外部已运行的 sglang serve
  # 终端 1: sglang serve --model-path <小模型> --port 30000
  # 终端 2 脚本里:
  sgl.set_default_backend(sgl.RuntimeEndpoint(base_url="http://localhost:30000"))
  同一个 @sgl.function 函数 .run(...)    # 这次打到外部服务
```

无论哪条路线，DSL 函数 `multi_turn_question` 都把多轮对话写成 `sgl.user(...) / sgl.assistant(sgl.gen(...))` 的串接。

#### 4.4.3 源码精读

示例主体：[examples/frontend_language/quick_start/local_example_chat.py:1-76](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L1-L76)。

- 用 `@sgl.function` 定义一个生成函数：[local_example_chat.py:9-14](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L9-L14)。`s += sgl.user(...)` 表示把 user 消息拼进对话；`sgl.gen("answer_1", max_tokens=256)` 表示在 assistant 角色下生成一段，并存到变量 `answer_1`。
- 单次运行 + 读取结果：[local_example_chat.py:17-26](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L17-L26)。`state.messages()` 返回完整对话；`state["answer_1"]` 取出生成的文本。
- 入口：自启 backend 再运行：[local_example_chat.py:59-75](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L59-L75)，其中 `sgl.Runtime(model_path="...")` 在 [第 60 行](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L60)，`sgl.set_default_backend(runtime)` 在 [第 61 行](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L61)，最后 `runtime.shutdown()` 在 [第 75 行](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L75)。

支撑示例的前端公开 API：

- `sgl.function` 是装饰器，把普通函数包成 `SglFunction`：[python/sglang/lang/api.py:23-32](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/lang/api.py#L23-L32)。
- `sgl.Runtime` 实际是延迟导入 `Runtime` 类：[python/sglang/lang/api.py:35-39](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/lang/api.py#L35-L39)。
- `sgl.set_default_backend` 把 backend 写进全局配置：[python/sglang/lang/api.py:49-50](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/lang/api.py#L49-L50)。
- `Runtime` 类的定位（**它会在子进程里启动 HTTP 服务，专为前端语言设计；纯离线处理请用 `Engine`**）：[python/sglang/lang/backend/runtime_endpoint.py:356-364](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/lang/backend/runtime_endpoint.py#L356-L364)。
- 若想连接**外部已运行的服务**（即路线 B），用 `RuntimeEndpoint`，构造时就会请求 `/get_model_info` 校验：[python/sglang/lang/backend/runtime_endpoint.py:26-47](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/lang/backend/runtime_endpoint.py#L26-L47)。

#### 4.4.4 代码实践

**目标**：分别用「curl」和「openai SDK」两种方式，向一个已运行的 `sglang serve` 发请求并打印返回。

**步骤**：

1. 终端 1：启动服务（请把模型换成你本机可访问的小模型，例如 `Qwen/Qwen2.5-0.5B-Instruct`；示例里写死的 `meta-llama/Llama-2-7b-chat-hf` 是受限且较大的模型，不建议初学直接用）：
   ```bash
   sglang serve --model-path Qwen/Qwen2.5-0.5B-Instruct --port 30000
   ```
2. 终端 2：用 curl 打 OpenAI 兼容端点：
   ```bash
   curl http://localhost:30000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "Qwen/Qwen2.5-0.5B-Instruct",
       "messages": [{"role":"user","content":"What is the capital of the United States?"}],
       "max_tokens": 64
     }'
   ```
3. 终端 3：用 openai SDK（依赖已在 `pyproject.toml` 中，`openai==2.6.1`）：
   ```python
   # 示例代码：连接本地 SGLang 服务的最小客户端
   from openai import OpenAI

   client = OpenAI(base_url="http://localhost:30000/v1", api_key="None")
   resp = client.chat.completions.create(
       model="Qwen/Qwen2.5-0.5B-Instruct",
       messages=[{"role": "user", "content": "List two local attractions in Washington D.C."}],
       max_tokens=64,
   )
   print(resp.choices[0].message.content)
   ```

**需要观察的现象 / 预期结果**：

- curl 返回一个 JSON，其中 `choices[0].message.content` 是模型回答。
- 服务首次启动会下载/加载模型权重并预热，期间日志会打印端口、TP/DP 配置、`The server is fired up and ready to roll!` 之类的就绪信息。

**待本地验证**：模型的下载、显存是否足够、`api_key="None"` 是否被你的部署接受（有些部署校验鉴权），都需要在本机确认；以上为典型用法，未在本次环境实跑。

#### 4.4.5 小练习与答案

- **练习 1**：`sgl.Runtime` 与 `sgl.RuntimeEndpoint` 的区别是什么？分别适合什么场景？
  - **答案**：`Runtime` 会在脚本所在进程里**额外派生一个服务子进程**（见其 docstring），适合「一个脚本独立跑完」的演示；`RuntimeEndpoint` 只是**连接一个已经在跑的服务**，适合「服务与客户端分离」的生产用法。两者都能作为前端 DSL 的 backend。
- **练习 2**：openai SDK 里 `base_url` 为什么写成 `http://localhost:30000/v1` 而不是 OpenAI 官网地址？
  - **答案**：因为本地 SGLang 服务实现了 OpenAI 兼容 API，把 `base_url` 指向它，就能复用 openai SDK 的全部客户端代码，只是请求实际打到本地推理服务。这正是「OpenAI 兼容」的价值。
- **练习 3**：示例脚本里 `state["answer_1"]` 是怎么得到的？
  - **答案**：`sgl.gen("answer_1", ...)` 把生成结果以名字 `answer_1` 存入 `ProgramState`，随后即可用 `state["answer_1"]` 取出（见 [local_example_chat.py:12 与 26](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/frontend_language/quick_start/local_example_chat.py#L12-L26)）。

---

## 5. 综合实践

把本讲知识串起来，完成「启动服务 + 用示例风格发一条消息并打印返回」这个完整闭环。这是本讲的贯穿任务。

**任务**：用 `sglang serve` 启动服务，再用前端 DSL 风格（仿照 `local_example_chat.py`）向它发一条消息并打印返回。

**操作步骤**：

1. 确认已安装（4.1 实践）：`sglang version` 能正常输出版本号。
2. 终端 1 启动服务（用小模型，避免受限/过大模型）：
   ```bash
   sglang serve --model-path Qwen/Qwen2.5-0.5B-Instruct --port 30000
   ```
   等到日志出现服务就绪信息（待本地确认具体文案）。
3. 终端 2 编写脚本 `my_first_request.py`（连接已运行的服务，复用示例的 DSL 风格）：
   ```python
   # 示例代码：连接外部 sglang serve，复用 local_example_chat 的 DSL 风格
   import sglang as sgl

   @sgl.function
   def qa(s, question):
       s += sgl.user(question)
       s += sgl.assistant(sgl.gen("answer", max_tokens=64))

   # 关键：连接已运行的服务，而不是 sgl.Runtime 自启
   sgl.set_default_backend(sgl.RuntimeEndpoint(base_url="http://localhost:30000"))

   state = qa.run(question="What is the capital of the United States?")
   print("-- answer --")
   print(state["answer"])
   ```
4. 运行：`python3 my_first_request.py`。
5. 对照阅读：在 `cli/serve.py` 标注这条请求背后服务是从哪一行被拉起的（语言模型分支 `run_server`）；在 `runtime_endpoint.py:26-47` 确认 `RuntimeEndpoint` 是如何校验服务并连上的。

**需要观察的现象 / 预期结果**：

- 脚本打印出 `-- answer --` 以及一段关于「华盛顿特区」的回答文本。
- 终端 1 的服务日志会显示收到了 `/generate` 或 `/v1/chat/completions` 请求。

**待本地验证**：完整运行依赖 GPU、模型下载成功、端口未被占用等本机条件。若 `RuntimeEndpoint` 构造时报连接错误，通常是终端 1 的服务还没就绪或 `--port` 不一致。若想跳过外部服务、最快验证 DSL，也可直接把第 3 步的 backend 换成 `sgl.Runtime(model_path="Qwen/Qwen2.5-0.5B-Instruct")`（即 `local_example_chat.py` 原样的自启方式）。

> 说明：本讲为入门篇，故意不深入请求在多进程间如何流转（TokenizerManager→Scheduler→Detokenizer）。那正是 u2 的主题。

## 6. 本讲小结

- SGLang 安装重、强依赖 GPU 栈：核心 `dependencies` 已锁定 torch/flashinfer/sgl-kernel，`pip install "sglang[all]"` 额外带上 diffusion/http2/tracing；`sglang` 命令来自 `pyproject.toml` 的 `[project.scripts]`。
- `sglang serve` 的分发链路是：`sglang.cli.main.main` →（懒加载）→ `sglang.cli.serve.serve` → `prepare_server_args` → `sglang.launch_server.run_server`。
- `main()` 只做最薄分发，靠 `parse_known_args` 把所有 `ServerArgs` 参数透传；`serve()` 再按 `--model-type` 分流到语言模型或扩散模型路径。
- `serve()` 用 `try/finally + kill_process_tree` 保证多子进程在任何退出路径下都被回收。
- 默认 HTTP 端口是 `30000`；服务暴露 OpenAI 兼容端点，可用 curl、openai SDK 或前端 DSL（`Runtime` 自启 / `RuntimeEndpoint` 连接）三种方式访问。
- 官方示例 `local_example_chat.py` 用 `@sgl.function + sgl.gen` 描述多轮对话，是理解前端 DSL 的最佳起点。

## 7. 下一步学习建议

- **u1-l3 仓库目录结构总览**：本讲你已接触 `python/sglang/cli`、`python/sglang/launch_server.py`、`python/sglang/lang`、`python/sglang/srt`，下一讲会系统梳理整个仓库布局，帮你定位后续每个子系统。
- **u1-l4 两种使用入口：HTTP 服务 vs 进程内 Engine**：本讲用了 HTTP 服务（`sglang serve` / `Runtime`），u1-l4 会把它与进程内 `Engine` 类做正式对比，讲清何时用哪个。
- **u2 服务架构与请求生命周期**：本讲刻意回避了「请求进来之后到底发生什么」。u2 会带你走完 TokenizerManager→Scheduler→Detokenizer 的多进程流转，并讲清 ZMQ + msgspec 的消息协议。
- 建议同步阅读的源码：先把 `python/sglang/launch_server.py`（很短）和 `python/sglang/srt/server_args.py` 中 `ServerArgs` 的字段（如 `port / tp_size / dp_size / chunked_prefill_size / mem_fraction_static`）浏览一遍，为 u2-l2 的「启动流程与 ServerArgs 配置」做准备。
