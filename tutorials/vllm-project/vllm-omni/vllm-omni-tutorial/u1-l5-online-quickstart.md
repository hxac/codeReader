# 在线服务初体验：vllm serve --omni 与 OpenAI 兼容 API

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `vllm serve <model> --omni --port 8091` 启动一个 OpenAI 兼容的 vLLM-Omni 在线服务。
- 用 `curl` 调用 `/v1/chat/completions` 完成一次文生图，并解析返回的 base64 图像数据保存为 PNG。
- 说清 `--omni` 这个标志是如何在 CLI 层把一条普通的 `vllm serve` 命令「拦截」并改道到 vLLM-Omni 的启动流程的。
- 对照源码讲出 `main.py` 的拦截分支、`serve.py` 的子命令装配（`cmd_init` → `subparser_init` → `validate` → `cmd`）以及最终 `omni_run_server` 启动 HTTP 服务的链路。

本讲是入门篇的最后一讲，承接 [u1-l4 离线推理初体验](./u1-l4-offline-quickstart.md)：离线推理是「在 Python 进程里直接调 `Omni.generate`」，而本讲把同一个模型能力包成一个**常驻 HTTP 服务**，供任何语言、任何客户端通过 OpenAI 协议调用。

## 2. 前置知识

在动手之前，先用三句话建立直觉：

- **OpenAI 兼容 API**：指服务对外暴露 `/v1/chat/completions`、`/v1/models` 等端点，请求/响应格式与 OpenAI 官方一致。好处是：你已经会用 `openai` SDK 或 `curl` 调 GPT，就会用它调 vLLM-Omni。
- **CLI（命令行接口）**：你在终端敲的 `vllm serve ...` 就是一个 CLI。`serve` 是它的**子命令**（subcommand），`--omni`、`--port` 是**参数**（argument/flag）。
- **拦截（intercept）**：vLLM-Omni 并没有「另起炉灶」写一个全新的服务程序，而是在 vLLM 已有的 CLI 上加了一道「检查站」——看到命令里带 `--omni`，就走 omni 自己的启动逻辑；没带，就原样交给上游 vLLM。这样既复用了 vLLM 的全部参数体系，又能无缝注入 omni 的能力。

> 名词解释
> - **console_script（控制台脚本）**：Python 打包时声明的「命令入口」，安装后会生成一个可执行命令。本项目的命令叫 `vllm-omni`。
> - **子命令（subcommand）**：像 `git commit` 里的 `commit`，`vllm serve` 里的 `serve`。
> - **uvloop**：一个高性能的事件循环，用来跑异步 HTTP 服务（底层基于 libuv）。本讲会在源码里看到 `uvloop.run(...)`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [docs/getting_started/quickstart.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/getting_started/quickstart.md) | 官方快速上手文档，含在线服务的启动命令与 `curl` 示例。 |
| [vllm_omni/entrypoints/cli/main.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py) | CLI 总入口，负责检测 `--omni` 并装配子命令。 |
| [vllm_omni/entrypoints/cli/serve.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py) | `serve` 子命令的定义：参数解析、校验、启动。 |
| [vllm_omni/entrypoints/openai/api_server.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py) | 真正构建并运行 FastAPI/uvicorn HTTP 服务的 `omni_run_server`。 |
| [pyproject.toml](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/pyproject.toml) | 声明 `vllm-omni` 命令入口（console_script）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **在线服务全景**：一条命令、一个请求，最终变成一张图。
2. **CLI 拦截机制**：`--omni` 如何在 `main.py` 里分流。
3. **serve 子命令**：`serve.py` 的装配（`cmd_init`）与启动流程。

---

### 4.1 在线服务全景：从一条命令到一张图

#### 4.1.1 概念说明

在线服务（online serving）和离线推理（offline inference）的最大区别在于**生命周期**：

- 离线：你写一个 Python 脚本，`Omni(...)` 建好引擎、`generate(...)` 出结果、脚本退出，引擎随之销毁。适合批量、一次性的任务。
- 在线：你启动一个**常驻进程**，它一直监听某个端口（如 `8091`），随时接收来自网络的请求，逐条处理后返回。适合被网页、App、其他服务实时调用。

vLLM-Omni 的在线服务走的是 **OpenAI 兼容协议**。也就是说，文生图这种「非文本输出」的能力，被巧妙地塞进了 OpenAI 的 `/v1/chat/completions` 响应结构里——返回的 `content` 是一个列表，其中一项的 `image_url.url` 是一段 base64 编码的图片数据。

#### 4.1.2 核心流程

一次「敲命令 → 拿到图」的完整流程：

```text
1. 终端执行：vllm serve <model> --omni --port 8091
        │
        ▼
2. CLI 检测到 --omni → 走 vLLM-Omni 启动逻辑（main.py）
        │
        ▼
3. serve 子命令解析参数、校验（serve.py）
        │
        ▼
4. omni_run_server 构建 AsyncOmni 引擎 + FastAPI 应用（api_server.py）
        │
        ▼
5. uvicorn 监听 8091，服务就绪
        │  ─────── 此时服务常驻 ───────
        ▼
6. 另开终端：curl POST /v1/chat/completions（含 prompt + extra_body 采样参数）
        │
        ▼
7. 服务把请求交给 AsyncOmni → Diffusion 阶段去噪 → 解码出图像
        │
        ▼
8. 响应：choices[0].message.content[0].image_url.url = "data:image/png;base64,..."
        │
        ▼
9. curl 管道：jq 提取 url → cut 去掉前缀 → base64 -d 解码 → 写入 coffee.png
```

第 1–5 步是「启动」，只做一次；第 6–9 步是「调用」，可以反复做。本讲的重点是第 1–5 步的源码；第 6–9 步的请求处理细节（落在哪个 serving 模块、哪个 stage）属于 [u6 在线服务与 OpenAI 兼容 API](./u6-l1-api-server.md) 的内容，本讲只点到为止。

#### 4.1.3 源码精读

启动命令和调用方式都写在 quickstart 的 **Online Serving** 段：

启动服务（一行命令）：

[docs/getting_started/quickstart.md:100-102](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/getting_started/quickstart.md#L100-L102) —— 官方给出的启动命令，模型是 `Tongyi-MAI/Z-Image-Turbo`，端口 8091。

```bash
vllm serve Tongyi-MAI/Z-Image-Turbo --omni --port 8091
```

发起请求（一段 curl 管道）：

[docs/getting_started/quickstart.md:104-118](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/getting_started/quickstart.md#L104-L118) —— 完整的 `curl | jq | cut | base64` 管道。

```bash
curl -s http://localhost:8091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "a cup of coffee on the table"}
    ],
    "extra_body": {
      "height": 1024,
      "width": 1024,
      "num_inference_steps": 50,
      "guidance_scale": 4.0,
      "seed": 42
    }
  }' | jq -r '.choices[0].message.content[0].image_url.url' \
    | cut -d',' -f2 | base64 -d > coffee.png
```

逐段拆解这个管道，这是初学者最容易卡住的地方：

| 片段 | 作用 |
| --- | --- |
| `curl -s ... -d '{...}'` | 以 JSON 形式 POST 一个 chat 请求。`content` 就是文生图的 prompt。 |
| `"extra_body": {...}` | OpenAI 协议的扩展字段。这里的 `height/width/num_inference_steps/guidance_scale/seed` 对应扩散模型的采样参数（离线推理里叫 `OmniDiffusionSamplingParams`，见 [u1-l4](./u1-l4-offline-quickstart.md)）。 |
| `jq -r '.choices[0].message.content[0].image_url.url'` | 用 `jq` 从响应 JSON 里取出图片 URL。`-r` 表示输出原始字符串（不带引号）。 |
| `cut -d',' -f2` | 取出的 URL 形如 `data:image/png;base64,iVBORw0KGgo...`。以逗号分隔取第 2 段，即剥掉 `data:image/png;base64,` 前缀，只留纯 base64。 |
| `base64 -d > coffee.png` | 把纯 base64 解码成二进制，写入 `coffee.png`。 |

> 注意：`-d` 的 JSON 里嵌套了 `extra_body`。这是因为 OpenAI 标准 chat 请求并没有 `height`/`num_inference_steps` 这些字段，vLLM-Omni（沿用 vLLM 的约定）把它们放在 `extra_body` 里作为模型专属扩展参数透传。

quickstart 还有一段关于版本对齐的重要提示：

[docs/getting_started/quickstart.md:34-37](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/getting_started/quickstart.md#L34-L37) —— 强调 vLLM 与 vLLM-Omni 主次版本必须一致；并指出「vLLM-Omni 不再劫持（hijack）vLLM 入口」，若 `vllm` 命令不认 `--omni`，多半是 vLLM 版本低于 `0.26.0`，升级 vLLM 即可。

这段提示是理解下一节「拦截机制」的钥匙——它告诉我们：`--omni` 的识别依赖**上游 vLLM ≥ 0.26.0 的协作**，而不是 vLLM-Omni 单方面覆写 `vllm` 命令。

#### 4.1.4 代码实践

**实践目标**：亲手把 quickstart 的两段命令跑通，得到一张 PNG。

**操作步骤**：

1. 按 [u1-l2 安装讲义](./u1-l2-installation.md) 完成源码安装，确认 `vllm` 与 `vllm-omni` 版本对齐（导入时应无版本告警）。
2. 启动服务（需要 GPU 与网络下载模型权重）：

   ```bash
   vllm serve Tongyi-MAI/Z-Image-Turbo --omni --port 8091
   ```

3. 等待日志出现服务就绪（监听 8091）后，**另开一个终端**，执行 4.1.3 里的 `curl | jq | cut | base64` 管道。
4. 用图片查看器打开生成的 `coffee.png`。

**需要观察的现象**：

- 启动日志里会出现 vLLM-Omni 的 logo（见 4.3.3 `log_logo`），随后是各 stage 的初始化日志。
- `curl` 返回的 JSON 体积较大（因为内嵌了 base64 图片）。

**预期结果**：得到一张 1024×1024、内容为「桌上的一杯咖啡」的图片。

> 待本地验证：本讲义写作环境无 GPU 与模型权重，上述命令未实际运行；请在本地按步骤验证。若 `jq`/`base64` 未安装，先 `apt-get install jq coreutils`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cut -d',' -f2` 去掉，直接 `... | base64 -d > coffee.png`，会发生什么？

> **答案**：`base64 -d` 收到的是 `data:image/png;base64,iVBOR...` 这一整串，前缀 `data:image/png;base64,` 不是合法 base64 字符，`base64 -d` 要么报错、要么解码出损坏的文件，得到的 `coffee.png` 无法正常打开。`cut` 的作用就是剥掉这个前缀。

**练习 2**：想把图片尺寸改成 768×768、步数改成 30，该改请求里的哪里？

> **答案**：改 `extra_body` 里的 `height: 768`、`width: 768`、`num_inference_steps: 30`。这些字段与离线推理的 `OmniDiffusionSamplingParams` 一一对应。

---

### 4.2 CLI 拦截机制：--omni 如何接管 vLLM 命令

#### 4.2.1 概念说明

这一节回答一个关键问题：**你敲的是 `vllm serve ... --omni`，这个命令为什么会走到 vLLM-Omni 的代码里？**

答案分两层：

1. **命令入口**：vLLM-Omni 在打包时声明了一个名为 `vllm-omni` 的控制台命令（console_script），它指向本讲的 `main()` 函数。也就是说，`vllm-omni serve X --omni` 是一条等价的、直接走 omni 的命令。

   [pyproject.toml:133-134](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/pyproject.toml#L133-L134) —— 声明 `vllm-omni` 命令指向 `vllm_omni.entrypoints.cli.main:main`。

2. **与上游 vLLM 的协作**：文档里你看到的是 `vllm serve ... --omni`（用 `vllm` 命令而非 `vllm-omni`）。根据 quickstart 的提示，vLLM-Omni **不再劫持** vLLM 的入口，而是依赖**上游 vLLM ≥ 0.26.0** 协作地把「带 `--omni` 的调用」转发进本仓库的这个 `main()`。这一转发逻辑实现**在 vLLM 仓库内**，本仓库不含其源码，所以我们只描述它的可观察行为：`vllm serve ... --omni` 与 `vllm-omni serve ... --omni` 最终都进入同一个 `main()`。

> 设计要点：`main()` 被设计成「单一前门」。无论从哪个命令进来，它都先看 `sys.argv` 里有没有 `--omni`：有就走 omni，没有就**原样转交**给上游 vLLM 的 `main`。这样既不破坏普通 vLLM 用法（`vllm serve <纯LLM>` 依旧可用），又能按需启用 omni。

#### 4.2.2 核心流程

`main.py` 的执行流程（伪代码）：

```text
def main():
    if "--omni" 不在 sys.argv 中:
        调用上游 vLLM 的 main()     # 完全不改 vLLM 行为
        return
    else:
        设置日志颜色环境变量
        导入 omni 的 serve / bench 子命令模块
        cli_env_setup()             # 上游的 CLI 环境初始化
        _ensure_vllm_platform()     # 确保平台已识别（见 4.3）
        构造顶层 parser，注册 -v/--version
        对每个子命令模块调用 cmd_init() 拿到命令列表
        为每个命令：subparser_init(注册参数) + 记录 dispatch_function
        parse_args()
        validate(args)              # 子命令自带的校验
        dispatch_function(args)     # 执行对应子命令（如 serve 的 cmd）
```

关键点：子命令不是写死在 `main.py` 里的，而是通过一个 `CMD_MODULES` 列表**装配**进来的——这是一个很常见的可扩展模式。

#### 4.2.3 源码精读

先看拦截的「检查站」本体：

[vllm_omni/entrypoints/cli/main.py:9-16](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L9-L16) —— `main()` 的入口与 `--omni` 分流。

```python
def main():
    """Main CLI entry point that intercepts vLLM commands."""
    # Check if --omni flag is present
    if "--omni" not in sys.argv:
        from vllm.entrypoints.cli.main import main as vllm_main
        vllm_main()
        return
    else:
        ...
```

- 没有 `--omni`：延迟 `import` 上游 `vllm_main` 并直接调用，**完全透传**。延迟 import 的好处是：只有真正需要时才加载 vLLM 的 CLI，避免无谓的导入开销。
- 有 `--omni`：进入 else 分支，开始 omni 自己的装配。

接着看 else 分支里对环境的预处理与子命令模块的导入：

[vllm_omni/entrypoints/cli/main.py:21-41](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L21-L41) —— 设置日志颜色、导入两个子命令模块、`cli_env_setup()` 与 `_ensure_vllm_platform()`。

```python
        import os
        if "VLLM_LOGGING_COLOR" not in os.environ:
            os.environ["VLLM_LOGGING_COLOR"] = "1"

        from vllm.entrypoints.serve.utils.api_utils import (
            VLLM_SUBCMD_PARSER_EPILOG, cli_env_setup)
        import vllm_omni.entrypoints.cli.benchmark.main
        import vllm_omni.entrypoints.cli.serve
        from vllm_omni.utils.tracking_parser import TrackingArgumentParser

        CMD_MODULES = [
            vllm_omni.entrypoints.cli.serve,
            vllm_omni.entrypoints.cli.benchmark.main,
        ]
        cli_env_setup()
        from vllm_omni.entrypoints.cli.serve import _ensure_vllm_platform
        _ensure_vllm_platform()
```

要点：

- `VLLM_LOGGING_COLOR=1`：即使输出被管道（如 `| tee`）截获，也强制彩色日志。注释强调它**必须在任何 vLLM import 之前**设置，因为日志格式化器在 import 时就定了。
- `CMD_MODULES`：一个**模块列表**，当前含 `serve` 与 `benchmark.main` 两个。要加新子命令，往这里塞一个实现了 `cmd_init()` 的模块即可——这就是可扩展点。
- `cli_env_setup()`：复用上游 vLLM 的 CLI 环境初始化（如设置一些环境变量）。
- `_ensure_vllm_platform()`：见 4.3.3，确保「当前硬件平台」已被正确识别，否则后续参数解析会失败。

最后看子命令的装配与派发：

[vllm_omni/entrypoints/cli/main.py:43-74](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L43-L74) —— 构造 parser、循环注册子命令、解析并派发。

```python
        parser = TrackingArgumentParser(description="vLLM OMNI CLI", ...)
        ...
        subparsers = parser.add_subparsers(required=False, dest="subparser")
        cmds = {}
        for cmd_module in CMD_MODULES:
            new_cmds = cmd_module.cmd_init()          # 每个模块产出命令对象列表
            for cmd in new_cmds:
                cmd.subparser_init(subparsers).set_defaults(dispatch_function=cmd.cmd)
                cmds[cmd.name] = cmd
        args = parser.parse_args()
        if args.subparser in cmds:
            cmds[args.subparser].validate(args)       # 子命令自带的校验
        if hasattr(args, "dispatch_function"):
            args.dispatch_function(args)              # 执行 serve.cmd / bench.cmd
        else:
            parser.print_help()
```

这是典型的 argparse 子命令装配模式，理解三个动作即可：

1. `cmd_init()`：模块返回它提供的命令对象（如 `serve` 模块返回 `[OmniServeCommand()]`）。
2. `subparser_init(subparsers)`：命令对象把自己的参数注册成一个子 parser（`vllm serve --help` 看到的那些参数就是这么来的）。
3. `set_defaults(dispatch_function=cmd.cmd)`：把这个子 parser 与「真正要执行的函数」绑定；解析后通过 `args.dispatch_function(args)` 触发。

> `TrackingArgumentParser` / `TrackingNamespace`：是 vLLM-Omni 对标准 `argparse` 的薄封装，能**追踪哪些参数是用户在命令行显式写下的**（而不是默认值）。这对后续校验很关键——比如 4.3 会看到「在 `--omni` 下禁止某些 vLLM 参数」，判定依据就是「用户是否显式写了它」。

#### 4.2.4 代码实践

**实践目标**：通过观察 `--help` 与源码，验证「子命令是装配出来的」。

**操作步骤**（无需 GPU，纯 CLI 探查）：

1. 查看顶层帮助：

   ```bash
   vllm-omni --help
   ```

   预期看到 `serve`、`bench` 两个子命令，以及 `-v/--version`。

2. 查看 serve 子命令的帮助（参数很多，可加 `--help=OmniConfig` 只看 omni 相关分组）：

   ```bash
   vllm-omni serve --help
   ```

3. 对照源码：在 [vllm_omni/entrypoints/cli/main.py:32-35](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L32-L35) 的 `CMD_MODULES` 里，把 `benchmark.main` 那一行**在脑中**注释掉，预测 `--help` 输出会有什么变化（`bench` 子命令消失）。

**需要观察的现象**：`serve --help` 里会出现一个标题为 `OmniConfig` 的参数分组，里面就有 `--omni`、`--port`（port 实为上游 vLLM 参数）等。

**预期结果**：能从帮助输出里找到 4.3.3 源码中 `add_argument` 注册的每一个参数。

> 待本地验证：`vllm-omni` 命令是否可用取决于是否已完成安装；若未安装，可在源码目录用 `python -m vllm_omni.entrypoints.cli.main --help` 等价调用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from vllm.entrypoints.cli.main import main as vllm_main` 写在 `if` 分支**内部**，而不是文件顶部？

> **答案**：延迟导入（lazy import）。这样只有在「没有 `--omni`、需要透传给 vLLM」时才加载上游 vLLM 的 CLI 模块；带 `--omni` 的路径完全不需要这次导入，启动更快、依赖更轻。

**练习 2**：如果想新增一个 `vllm-omni doctor`（自检）子命令，至少要改哪两处？

> **答案**：(1) 新写一个模块，实现 `cmd_init()` 返回一个 `CLISubcommand` 子类实例（含 `name="doctor"`、`subparser_init`、`cmd`）；(2) 把该模块加入 [main.py:32-35](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L32-L35) 的 `CMD_MODULES` 列表。

---

### 4.3 serve 子命令：参数解析、校验与启动

#### 4.3.1 概念说明

上一节看到 `main.py` 通过 `CMD_MODULES` 装配子命令，其中 `serve` 模块由 [serve.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py) 提供。这个文件定义了一个类 `OmniServeCommand`，它继承自上游 vLLM 的 `CLISubcommand`，用**四个方法**把一个子命令的完整生命周期串起来：

| 方法 | 时机 | 职责 |
| --- | --- | --- |
| `cmd_init()`（模块级函数） | 启动最早期 | 把命令对象注册进 `main.py` 的装配流程。 |
| `subparser_init(subparsers)` | 装配阶段 | 创建 `serve` 子 parser，注册所有参数（含上游 vLLM 参数 + omni 专属参数）。 |
| `validate(args)` | 解析之后、执行之前 | 校验参数组合是否合法。 |
| `cmd(args)` | 执行阶段 | 真正启动服务（或 headless 模式）。 |

这四个方法的调用顺序，正是 4.2.2 流程图里 `cmd_init() → subparser_init → validate → dispatch(cmd)` 的来源。

#### 4.3.2 核心流程

`serve` 子命令的启动流程（从被装配到监听端口）：

```text
cmd_init()                         # 返回 [OmniServeCommand()]
   │
subparser_init(subparsers):        # 注册参数
   ├─ add_parser("serve", ...)
   ├─ make_arg_parser(...)         # 复用上游 vLLM 的全部参数
   └─ 新增 OmniConfig 分组：--omni / --port 由上游提供 / --deploy-config ...
   │
parse_args()  →  validate(args):    # 校验
   ├─ --stage-id 必须配 --omni-master-address/port
   ├️ --omni 下禁止 vLLM 的 DP 类参数（改由 YAML 配置）
   ├️ --omni-lb-policy 必须是合法枚举
   └️ 若是扩散模型，跳过上游 validate_parsed_serve_args
   │
cmd(args):                         # 执行
   ├─ log_logo()
   ├️ 解析 model_tag / guardrails
   ├️ if args.headless: run_headless(args)     # 单 stage 副本模式
   └️ else: uvloop.run(omni_run_server(args))   # 常规：起 HTTP 服务
           │
           └─→ api_server.omni_run_server → omni_run_server_worker
                 ├️ build_async_omni(...)        # 构建 AsyncOmni 引擎
                 ├️ build_openai_app(...)        # 构建 FastAPI 应用
                 ├️ 移除上游 /v1/chat/completions、/v1/models，挂上 omni 路由
                 └️ uvicorn 监听端口
```

本讲关注常规路径（`else` 分支 → `omni_run_server`）。`run_headless` 是分布式多 stage 部署的进阶用法，属于 [u3 多阶段运行时与编排](./u3-l3-stage-process-runtime.md)。

#### 4.3.3 源码精读

**(a) `cmd_init` —— 把命令交出去**

[vllm_omni/entrypoints/cli/serve.py:988-989](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L988-L989) —— 模块级 `cmd_init`，返回一个 `OmniServeCommand` 实例。

```python
def cmd_init() -> list[CLISubcommand]:
    return [OmniServeCommand()]
```

这就是 4.2.3 里 `cmd_module.cmd_init()` 调用的目标。`OmniServeCommand` 的 `name = "serve"`（[serve.py:80](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L80)），决定了子命令名。

**(b) `subparser_init` —— 复用上游参数 + 新增 omni 参数**

[vllm_omni/entrypoints/cli/serve.py:179-201](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L179-L201) —— 创建 serve 子 parser，调用上游 `make_arg_parser` 复用 vLLM 的全部参数，再追加一个 `OmniConfig` 分组并注册 `--omni`。

```python
    def subparser_init(self, subparsers):
        serve_parser = subparsers.add_parser(
            self.name, description=DESCRIPTION,
            usage="vllm serve [model_tag] --omni [options]")
        _ensure_vllm_platform()
        serve_parser = make_arg_parser(serve_parser)     # 上游 vLLM 全部参数
        serve_parser.epilog = VLLM_SUBCMD_PARSER_EPILOG.format(subcmd=self.name)

        omni_config_group = serve_parser.add_argument_group(
            title="OmniConfig",
            description="Configuration for vLLM-Omni multi-stage and diffusion models.")
        omni_config_group.add_argument(
            "--omni", action="store_true",
            help="Enable vLLM-Omni mode for multi-modal and diffusion models")
        ...
```

关键理解：`make_arg_parser(serve_parser)` 直接复用了上游 vLLM 的参数体系（`--port`、`--tensor-parallel-size`、`--trust-remote-code` 等都来自这里），所以 omni 的 `serve` 命令天然兼容 vLLM 的全部启动参数。omni 自己新增的参数（`--omni`、`--deploy-config`、`--cache-backend`、`--usp`、`--num-gpus` 等几十个）都加在 `OmniConfig` 这个分组下。本讲你只需记住 `--omni` 这一个；其余会在进阶/专家篇逐个讲到。

> 注意 `subparser_init` 里也调了一次 `_ensure_vllm_platform()`（与 main.py 里的那次重复）。这是为了应对「文档生成工具会直接 exec 这个方法」的场景，确保平台一定就绪。

**(c) `_ensure_vllm_platform` —— 平台兜底**

[vllm_omni/entrypoints/cli/serve.py:46-74](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L46-L74) —— 若上游 vLLM 的 `current_platform` 是「未指定」，就用 omni 自己的平台探测结果替换它，再不行就退回 CPU。

```python
def _ensure_vllm_platform():
    from vllm import platforms as vllm_platforms
    if vllm_platforms.current_platform.is_unspecified():
        from vllm_omni.platforms import current_omni_platform
        if not current_omni_platform.is_unspecified():
            vllm_platforms.current_platform = current_omni_platform
        else:
            from vllm.platforms.cpu import CpuPlatform
            vllm_platforms.current_platform = CpuPlatform()
```

为什么需要它？注释解释：上游 vLLM 的参数解析器现在会在 `make_arg_parser` 阶段就实例化 `DeviceConfig`，这要求平台已解析出非空的 `device_type`；在某些 editable 安装、包元数据损坏的环境里，vLLM 自带的探测可能失败而退回 `UnspecifiedPlatform`，导致参数解析直接报错。这里用 omni 的探测逻辑兜底。平台抽象的细节见 [u8-2 平台抽象](./u8-l2-platforms.md)。

**(d) `validate` —— 校验参数组合**

[vllm_omni/entrypoints/cli/serve.py:109-177](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L109-L177) —— `validate` 做了一系列一致性检查。挑对入门最关键的三处看：

[vllm_omni/entrypoints/cli/serve.py:170-177](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L170-L177) —— 扩散模型走「快路径」，跳过上游 `validate_parsed_serve_args`。

```python
        from vllm_omni.diffusion.utils.hf_utils import is_diffusion_model
        model = getattr(args, "model_tag", None) or getattr(args, "model", None)
        if model and is_diffusion_model(model):
            logger.info("Detected diffusion model: %s", model)
            return
        validate_parsed_serve_args(args)
```

这段解释了为什么 `vllm serve Tongyi-MAI/Z-Image-Turbo --omni` 能跑通——Z-Image-Turbo 是扩散模型，它的参数要求与 LLM 不同（比如不需要 `max_model_len` 这类文本长度约束），所以检测到扩散模型就提前 `return`，跳过上游针对 LLM 的校验。

另外两处（了解即可）：`--stage-id` 必须同时给 `--omni-master-address/-port`（[serve.py:110-111](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L110-L111)）；在 `--omni` 下禁止用 vLLM 的 `--data-parallel-size` 等 DP 参数，因为并行应来自每阶段的 YAML（[serve.py:133-153](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L133-L153)）。这两条会在分布式讲义里展开。

**(e) `cmd` —— 启动服务**

[vllm_omni/entrypoints/cli/serve.py:85-107](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L85-L107) —— `cmd` 是最终执行体。

```python
    @staticmethod
    def cmd(args: TrackingNamespace) -> None:
        if not os.environ.get("VLLM_DISABLE_LOG_LOGO"):
            os.environ["VLLM_DISABLE_LOG_LOGO"] = "1"
            log_logo()
        if hasattr(args, "model_tag") and args.model_tag is not None:
            args.model = args.model_tag            # 位置参数 model_tag 优先
        ...
        if args.headless:
            run_headless(args)
        else:
            uvloop.run(omni_run_server(args))      # 常规路径
```

- `log_logo()`：打印 vLLM-Omni 的 logo（同时把 `VLLM_DISABLE_LOG_LOGO` 置 1，防止上游再打一次）。
- `model_tag`：`vllm serve <model>` 里那个位置参数会被解析成 `model_tag`，这里赋给 `args.model`，保证后续都能从 `args.model` 取到模型名。
- 分支：`headless`（无头，单 stage 副本，用于分布式）vs 常规 `uvloop.run(omni_run_server(args))`。`uvloop.run` 启动一个异步事件循环来跑 `omni_run_server` 这个 `async` 函数。

**(f) `omni_run_server` —— 真正起 HTTP 服务**

[vllm_omni/entrypoints/openai/api_server.py:439-458](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L439-L458) —— 统一入口，屏蔽了「LLM 还是 Diffusion」的差异。

```python
async def omni_run_server(args, **uvicorn_kwargs) -> None:
    """Run a single-worker API server.
    Unified entry point that automatically handles both LLM and Diffusion models
    through AsyncOmni, which manages multi-stage pipelines.
    """
    ...
    listen_address, sock = setup_openai_server(args, reuse_port=False)
    # 统一交给 omni_run_server_worker，AsyncOmni 自动区分 LLM / Diffusion
    await omni_run_server_worker(listen_address, sock, args, **uvicorn_kwargs)
```

注意它的 docstring：**统一入口**——无论多阶段 LLM（如 Qwen2.5-Omni）还是扩散模型（如 Z-Image-Turbo），都通过 `AsyncOmni` 处理。这也是为什么同一个 `vllm serve ... --omni` 命令既能服务 LLM 又能服务扩散模型。

再往里一层（只需看懂大意）：

[vllm_omni/entrypoints/openai/api_server.py:485-516](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L485-L516) —— 构建引擎、构建应用、覆盖路由、初始化状态。

```python
    async with build_async_omni(args, client_config=client_config) as engine_client:
        supported_tasks = tuple(await engine_client.get_supported_tasks())
        ...
        app = build_openai_app(args, supported_tasks)
        # 移除上游 /v1/chat/completions 与 /v1/models，换上 omni 自己的处理器
        _remove_route_from_app(app, "/v1/chat/completions", {"POST"})
        _remove_route_from_app(app, "/v1/models", {"GET"})
        app.include_router(router)
        _register_omni_exception_handlers(app)
        await omni_init_app_state(engine_client, app.state, args)
        ...
```

这段揭示了 vLLM-Omni「在 vLLM 之上做增量」的典型手法：先用上游方法把 FastAPI 应用建出来（`build_openai_app`），再把需要定制的关键端点（`/v1/chat/completions`、`/v1/models`）**移除后换成 omni 自己的路由**（`app.include_router(router)`）。这正是你在 4.1 里能从 `/v1/chat/completions` 拿到图片的原因——那个端点已经被 omni 的多模态处理器接管了。

> 边界：`get_supported_tasks` 返回空集时（如纯 TTS 模型）会关闭对应端点（[api_server.py:514-518](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L514-L518)）。本讲的 Z-Image-Turbo 走的是「pure diffusion mode」（[api_server.py:529-537](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L529-L537)）。

#### 4.3.4 代码实践

**实践目标**：用「去掉 `--omni`」做对照实验，直观感受拦截机制的存在。

**操作步骤**（对照实验）：

1. 先确认正常 omni 路径能解析参数（不真正启动，加 `--help` 即可）：

   ```bash
   vllm-omni serve Tongyi-MAI/Z-Image-Turbo --omni --port 8091 --help
   ```

   预期：看到含 `OmniConfig` 分组的帮助，说明走了 omni 的 `subparser_init`。

2. 去掉 `--omni` 再看：

   ```bash
   vllm-omni serve Tongyi-MAI/Z-Image-Turbo --port 8091 --help
   ```

   预期：根据 [main.py:12-16](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L12-L16)，没有 `--omni` 会**透传给上游 vLLM**，帮助里**不会**出现 `OmniConfig` 分组与 `--cache-backend` 等 omni 专属参数。

3. （源码阅读型）在 [serve.py:196-200](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L196-L200) 找到 `--omni` 参数的注册处，确认它的 `action="store_true"`（即 `--omni` 是一个**开关**，不带值）。

**需要观察的现象**：两次 `--help` 的参数分组差异——这正是「拦截 vs 透传」在用户侧的可观察证据。

**预期结果**：带 `--omni` 时多出 `OmniConfig` 分组；不带时没有。这从侧面印证了 4.2 的拦截逻辑。

> 待本地验证：`--help` 行为可在任何已安装环境验证；若未安装，读源码 [main.py:9-16](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L9-L16) 即可推断同样结论。

#### 4.3.5 小练习与答案

**练习 1**：`OmniServeCommand` 的四个生命周期方法，分别在什么时机被调用？请按时间顺序排列。

> **答案**：`cmd_init()`（main.py 装配时）→ `subparser_init()`（注册参数时）→ `validate()`（`parse_args` 之后、执行之前）→ `cmd()`（派发执行时）。对应 [main.py:62-72](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/main.py#L62-L72) 的循环与派发。

**练习 2**：为什么 `serve.py` 的 `validate` 检测到扩散模型后就 `return`，不再调用上游 `validate_parsed_serve_args`？

> **答案**：上游 `validate_parsed_serve_args` 是为文本 LLM 设计的（会校验 `max_model_len` 等文本相关约束），扩散模型的参数空间不同，套用 LLM 校验会误报。所以用 `is_diffusion_model(model)` 判定后走扩散专属的宽松校验路径。

**练习 3**：`omni_run_server` 的 docstring 说它能同时处理 LLM 与 Diffusion，这句话在源码里的依据是什么？

> **答案**：[api_server.py:485-488](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L485-L488) 用 `build_async_omni(args)` 统一构建引擎客户端，`AsyncOmni` 内部会根据模型类型自动区分 LLM 多阶段流水线与扩散单阶段；后续 `get_supported_tasks()` 再据此决定开放哪些端点。统一的入口 + 引擎内部的分流，实现了「同一命令服务两类模型」。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「命令 → 源码 → 结果」的完整追踪。

**任务**：启动 Z-Image-Turbo 的 omni 服务并生成一张图，然后**用人话**为这条命令写出一份「执行档案」。

**步骤**：

1. **启动**（需 GPU）：

   ```bash
   vllm serve Tongyi-MAI/Z-Image-Turbo --omni --port 8091
   ```

2. **调用**：执行 4.1.3 的 `curl | jq | cut | base64` 管道，得到 `coffee.png`。

3. **建档**：对照源码，按下表填写这条命令在你机器上的实际执行档案（把「发生在哪」填成 `文件:行号`）：

   | 步骤 | 发生的事 | 发生在哪（文件:行号） |
   | --- | --- | --- |
   | 检测到 `--omni` | 进入 omni 分支 | `main.py:12` |
   | 平台兜底 | `_ensure_vllm_platform` | （自己填） |
   | 装配 serve 子命令 | `cmd_init` + `subparser_init` | （自己填） |
   | 识别为扩散模型、跳过 LLM 校验 | `validate` 内 `is_diffusion_model` | （自己填） |
   | 启动 HTTP 服务 | `cmd` → `uvloop.run(omni_run_server)` | （自己填） |
   | 覆盖 `/v1/chat/completions` 路由 | `_remove_route_from_app` + `include_router` | （自己填） |

4. **反思**：把命令里的 `--omni` 去掉重跑（仅看 `--help` 或启动报错信息），记录它与 omni 路径的差异，印证 4.2 的拦截机制。

**验收标准**：

- 得到一张可正常打开的 PNG。
- 执行档案表格每一行的「发生在哪」都能给出准确的 `文件:行号`，并与本讲引用的永久链接对得上。

> 待本地验证：启动与生图步骤需要 GPU 和模型权重，本讲义写作环境未运行；执行档案表格的「文件:行号」部分可纯靠源码阅读完成，不依赖运行。

## 6. 本讲小结

- **在线 vs 离线**：在线服务是常驻 HTTP 进程，走 OpenAI 兼容协议；离线是脚本里直接调 `Omni.generate`。两者背后是同一套引擎。
- **一条命令**：`vllm serve <model> --omni --port 8091` 启动服务；`curl /v1/chat/completions` + `jq|cut|base64` 管道取图。图像以 `data:image/png;base64,...` 形式藏在 `choices[0].message.content[0].image_url.url` 里。
- **拦截机制**：`main.py` 用 `"--omni" in sys.argv` 做单一检查站——有 `--omni` 走 omni，没有则透传上游 vLLM；`vllm` 命令对 `--omni` 的识别依赖上游 vLLM ≥ 0.26.0 协作转发（不再劫持入口）。
- **子命令装配**：`main.py` 通过 `CMD_MODULES` 列表 + `cmd_init()` 动态装配 `serve`/`bench` 子命令，是可扩展的 argparse 模式。
- **serve 四段式**：`cmd_init → subparser_init → validate → cmd`。`subparser_init` 复用上游 `make_arg_parser` 并追加 `OmniConfig` 分组；`validate` 对扩散模型走快路径；`cmd` 经 `uvloop.run(omni_run_server)` 起服务。
- **统一服务入口**：`omni_run_server` 用 `build_async_omni` 统一处理 LLM 与 Diffusion，并通过「移除上游路由 + 挂载 omni 路由」实现端点定制。

## 7. 下一步学习建议

- 想深入了解 `/v1/chat/completions` 请求**内部**是怎么被拆成多阶段、图像是怎么解码出来的，进入 **u6 在线服务与 OpenAI 兼容 API**（先读 [u6-l1 API Server：FastAPI 应用构建](./u6-l1-api-server.md)）。
- 想搞清「stage（阶段）」到底是什么、多个 stage 如何编排，进入 **u3 多阶段运行时与编排**（先读 [u3-l1 AsyncOmni 与 AsyncOmniEngine：多阶段架构总览](./u3-l1-async-omni-architecture.md)）。
- 想理解 `--omni` 之外那一长串 `OmniConfig` 参数（`--cache-backend`、`--usp`、`--deploy-config` 等），可先读 [u2-2 配置体系](./u2-l2-config-system.md)，再按需进入 u7（加速）与 u8（量化/平台）。
- 如果你的目标是「加一个自己的模型到这个服务里」，直接跳到 **u9 扩展开发**。
