# 安装与快速运行

## 1. 本讲目标

上一讲我们从宏观上认识了 Mini-SGLang 是什么、有哪些核心特性。本讲的目标是把那些「纸面上的特性」变成「能在你机器上跑起来的东西」。读完本讲，你应当能够：

1. 用 `uv`（或 Docker）从零把 Mini-SGLang 装到本机或 WSL2 里。
2. 说清楚在终端敲下 `python -m minisgl ...` 之后，代码到底从哪里开始执行、做了哪几件事。
3. 用一条命令启动一个 OpenAI 兼容的 HTTP 服务，并用 `curl` 得到一次模型回复。
4. 用 `--shell` 进入终端交互对话，并用 `/reset` 清空历史。
5. 理解 Dockerfile 是怎么把这个过程打包成一个可复现镜像的。

## 2. 前置知识

在动手之前，先建立三个直觉：

**① 什么是「OpenAI 兼容接口」。**
OpenAI 的 `/v1/chat/completions` 接口几乎是事实标准：你按固定 JSON 格式发请求（带 `messages`、`max_tokens` 等），服务端返回模型回复。只要一个推理框架实现了这个接口，你就能用任何 OpenAI 客户端（包括最朴素的 `curl`）来调用它，而不必学习框架自己的私有协议。Mini-SGLang 用的就是这套接口，默认监听端口 **1919**。

**② Python 的 `-m` 是什么。**
`python -m <包名>` 表示「把这个包当作脚本运行」。Python 会去该包目录下找一个名为 `__main__.py` 的文件并执行它。所以 `python -m minisgl` 真正跑的是源码里的 `python/minisgl/__main__.py`。这和 `pip`、`pytest` 等「带横杠的命令」是同一套机制。

**③ 为什么需要那么多进程。**
LLM 推理有 4 类工作要做：接 HTTP 请求、把文本切成 token、在 GPU 上跑模型、再把 token 拼回文本。Mini-SGLang 把它们拆成多个进程（每个 GPU 一个调度进程，外加 tokenizer/detokenizer 进程），用 ZMQ（一种消息队列）在进程间传小消息、用 NCCL 在 GPU 间传张量。本讲你不必完全搞懂这些进程，只要知道：**启动服务 = 启动主进程 + fork 一堆子进程 + 等它们都就绪 + 开始监听端口**。

> ⚠️ 平台限制：Mini-SGLang 只支持 **Linux**（x86_64 与 aarch64），因为依赖 Linux 专用的 CUDA kernel（`sgl-kernel`、`flashinfer`）。Windows 用户请用 WSL2，macOS 暂不支持。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/__main__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/__main__.py) | 模块入口：`python -m minisgl` 执行的就是它，负责转发到 `launch_server`。 |
| [python/minisgl/server/__init__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/__init__.py) | server 包的导出，对外暴露 `launch_server`。 |
| [python/minisgl/server/launch.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py) | 核心编排器：解析参数 → fork 子进程 → 等就绪 → 启动 API server。 |
| [python/minisgl/server/args.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py) | CLI 参数定义与归一化（`parse_args`），包括 `--shell-mode` 的特殊处理。 |
| [python/minisgl/server/api_server.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py) | FastAPI 前端：HTTP 路由、流式返回，以及交互式 `shell()`。 |
| [python/minisgl/env.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py) | 环境变量配置，包括 shell 模式的采样参数默认值。 |
| [Dockerfile](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/Dockerfile) | 多阶段构建脚本，把上述流程打包成镜像。 |
| [pyproject.toml](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml) | 包元数据与依赖列表。 |

---

## 4. 核心概念与源码讲解

### 4.1 `__main__` 入口：一切从 `python -m minisgl` 开始

#### 4.1.1 概念说明

当你执行 `python -m minisgl --model Qwen/Qwen3-0.6B` 时，Python 解释器会：

1. 找到 `minisgl` 这个包；
2. 在它目录下寻找 `__main__.py`；
3. 把 `__main__.py` 当作顶层脚本运行。

所以整个项目的命令行入口，物理上就是 `python/minisgl/__main__.py` 这个文件。

需要特别说明：本项目的 `pyproject.toml` 里**没有**注册 `[project.scripts]`（也没有 `console_scripts`）。也就是说，安装后并**不会**生成一个叫 `minisgl` 的可执行命令。你只能用 `python -m minisgl` 这种方式启动。Docker 镜像里也是用 `python -m minisgl`（见 4.4）。

#### 4.1.2 核心流程

```text
python -m minisgl …
        │
        ▼
  minisgl/__main__.py
        │  from .server import launch_server
        ▼
  launch_server()          # 转交给 server 包的编排器
```

`__main__.py` 本身几乎不写逻辑，只做「转发」。它的全部职责是：导入真正的启动函数、确保自己确实是作为主程序被运行、然后调用启动函数。

#### 4.1.3 源码精读

整个入口文件只有 5 行：

[python/minisgl/__main__.py:1-5](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/__main__.py#L1-L5) —— 从同包的 `server` 子包导入 `launch_server`，断言当前模块是主模块，然后调用它。

```python
from .server import launch_server

assert __name__ == "__main__"

launch_server()
```

这里有两个值得注意的写法：

- **`from .server import launch_server`**：`.server` 是相对导入，表示「从当前包（`minisgl`）下的 `server` 子包导入」。而 `server/__init__.py` 把 `launch_server` 显式导出了：
  [python/minisgl/server/__init__.py:1-3](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/__init__.py#L1-L3) 把 `launch_server` 放进 `__all__`，对外只暴露这一个名字。
- **`assert __name__ == "__main__"`**：注意是 `assert`，而不是更常见的 `if __name__ == "__main__":`。这表示作者**有意**让这个文件只能作为主程序运行——如果有人尝试 `import minisgl.__main__`，`__name__` 会变成 `"minisgl.__main__"`，断言就会失败。这是一种「禁止被当作库导入」的自我保护。

最后，`launch.py` 末尾也保留了一段传统的 `if __name__ == "__main__"`，方便直接用 `python minisgl/server/launch.py` 调试，但这不是用户常用路径。

#### 4.1.4 代码实践

**目标**：确认入口能被正确发现。

**步骤**：

1. 进入项目根目录并激活虚拟环境（详见 4.2 的安装步骤）：
   ```bash
   source .venv/bin/activate
   ```
2. 查看帮助，这会触发完整参数解析但不启动服务（需要安装完成才能跑通）：
   ```bash
   python -m minisgl --help
   ```
3. 用一行 Python 验证模块路径能被定位到（不需要 GPU）：
   ```bash
   python -c "import minisgl.__main__; print('入口模块路径正确')"
   ```

**预期结果**：第 2 步打印出所有可用 CLI 参数（`--model`、`--tp`、`--shell-mode` 等）；第 3 步打印「入口模块路径正确」。

**注意**：第 2 步需要依赖全部安装成功（含 CUDA kernel），否则可能在导入阶段报错。若本机无 GPU，第 2 步记为「待本地验证」，仅靠第 3 步验证导入路径即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么入口用 `assert __name__ == "__main__"` 而不是 `if __name__ == "__main__":`？

**参考答案**：`assert` 表达了「这个文件**只能**作为主程序运行、不应当被别的代码 import」的强约束。一旦被 import，断言立刻失败报错，比 `if` 静默跳过更能暴露误用。

**练习 2**：为什么项目不注册一个 `minisgl` 命令，而一定要写 `python -m minisgl`？

**参考答案**：`pyproject.toml` 没有定义 `[project.scripts]`，所以 pip 安装时不会生成控制台可执行命令。使用 `python -m minisgl` 能保证「永远走当前 Python 环境里的那个 `minisgl` 包」，避免 PATH 里残留旧命令造成版本错乱，对开发期 `uv pip install -e .`（可编辑安装）尤其友好。

---

### 4.2 `launch_server` 启动：编排所有子进程

#### 4.2.1 概念说明

`launch_server` 是真正的「总指挥」。它本身不跑模型，而是做三件事：

1. **解析命令行参数**（`parse_args`），把一堆 `--xxx` 归一化成一个不可变的 `ServerArgs` 配置对象；
2. **fork 出所有子进程**：每个 GPU 一个调度进程（scheduler），加上若干个 tokenizer 进程和 1 个 detokenizer 进程；
3. **启动前端 API server**（FastAPI + uvicorn）或交互式 shell。

理解它的关键是「**fork 之后用 ack（确认）来同步**」：主进程不会在子进程还没就绪时就开服，而是通过一个 `multiprocessing.Queue` 收齐所有子进程的「我准备好了」消息后，才开始对外提供服务。

#### 4.2.2 核心流程

```text
launch_server()
  │
  ├─ parse_args(sys.argv)            # ① 解析 CLI → ServerArgs
  │
  ├─ start_subprocess():             # ② fork 子进程
  │     ├─ mp.set_start_method("spawn", force=True)
  │     ├─ for i in range(tp_size):   启动 tp_size 个 _run_scheduler 进程
  │     ├─ 启动 1 个 detokenizer 进程 (tokenize_worker)
  │     └─ 启动 num_tokenizer 个 tokenizer 进程
  │
  ├─ 等待 ack_queue 收齐 (num_tokenizer + 2) 条消息   # ③ 同步就绪
  │
  └─ run_api_server(server_args, start_subprocess, run_shell)
                                      # ④ 启动 FastAPI/shell
```

为什么要分这么多进程？因为「文本↔token 转换」是 CPU 密集，而「跑模型」是 GPU 密集，把它们放到不同进程可以让 CPU 和 GPU 各自不被对方阻塞。`tp_size`（张量并行数）决定有几个 GPU，于是就有几个 scheduler 进程。这部分会在第 1 单元第 4 讲（进程架构）详细展开。

#### 4.2.3 源码精读

**① 函数签名与参数解析。**
[python/minisgl/server/launch.py:40-44](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L40-L44) 定义 `launch_server(run_shell=False)`，第一件事就是从 `sys.argv[1:]` 解析参数：

```python
def launch_server(run_shell: bool = False) -> None:
    from .api_server import run_api_server
    from .args import parse_args

    server_args, run_shell = parse_args(sys.argv[1:], run_shell)
```

注意它**延迟导入** `run_api_server` 和 `parse_args`（写在函数体内而不是文件顶部）。这是有意为之：这样在 `python -m minisgl --help` 时，可以避免加载一堆重依赖（torch、fastapi 等），让 `--help` 更快、对环境更宽容。

**② 子进程启动器与 spawn。**
[python/minisgl/server/launch.py:47-69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L47-L69) 设置进程启动方式为 `spawn`，并按 `tp_size` 循环创建 scheduler 进程：

```python
mp.set_start_method("spawn", force=True)
world_size = server_args.tp_info.size
ack_queue: mp.Queue[str] = mp.Queue()

for i in range(world_size):
    new_args = replace(server_args, tp_info=DistributedInfo(i, world_size))
    mp.Process(
        target=_run_scheduler,
        args=(new_args, ack_queue),
        name=f"minisgl-TP{i}-scheduler",
    ).start()
```

要点：

- **`spawn` 而非 `fork`**：CUDA 运行时不能跨 `fork` 安全继承，必须用 `spawn`（子进程重新解释执行、重新初始化 CUDA），这是几乎所有 GPU 多进程框架的强制选择。
- **每个 rank 一份 `new_args`**：用 `dataclasses.replace` 复制配置，只把 `tp_info`（rank 编号与总卡数）替换成 `DistributedInfo(i, world_size)`，于是每个 scheduler 进程都知道自己是第几张卡。
- 进程名 `minisgl-TP{i}-scheduler` 方便你在 `top`/`htop` 里认出它们。

[python/minisgl/server/launch.py:71-103](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L71-L103) 接着启动 tokenizer 进程：默认 `num_tokenizer=0` 时，tokenizer 与 detokenizer 共用同一个进程（`share_tokenizer=True`），因此只起 1 个 detokenizer 进程。

**③ 用 ack 同步就绪。**
[python/minisgl/server/launch.py:105-111](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L105-L111) 主进程阻塞地从这个队列里取消息，直到收齐 `num_tokenizers + 2` 条：

```python
for _ in range(num_tokenizers + 2):
    logger.info(ack_queue.get())
```

为什么是 `num_tokenizers + 2`？因为发送 ack 的有：主 rank scheduler 1 条（只有 `tp_info.is_primary()` 的 rank 会发，见 [`_run_scheduler` 里的 ack_queue.put](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L24-L25)）、detokenizer 1 条、tokenizer `num_tokenizers` 条。默认 `num_tokenizer=0` 时就是 2 条。

**④ 解析与归一化参数。**
[python/minisgl/server/args.py:54-76](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L54-L76) 定义 `parse_args`，并声明唯一的必填参数 `--model-path`（别名 `--model`）：

```python
parser.add_argument(
    "--model-path", "--model",
    type=str, required=True,
    help="... 本地文件夹或 Hugging Face repo ID。",
)
```

所以 README 里写 `--model Qwen/Qwen3-0.6B` 和 `--model-path Qwen/Qwen3-0.6B` 是等价的——它们是同一个参数的两个名字。

[python/minisgl/server/args.py:251-263](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L251-L263) 展示了「归一化」逻辑：当 `--dtype auto`（默认）时，它会去读模型的 HF 配置决定用 fp16 还是 bf16；还会把 `--tensor-parallel-size` 转换成 `tp_info` 字段：

```python
if (dtype_str := kwargs["dtype"]) == "auto":
    from minisgl.utils import cached_load_hf_config
    dtype_str = cached_load_hf_config(kwargs["model_path"]).dtype
...
kwargs["tp_info"] = DistributedInfo(0, kwargs["tensor_parallel_size"])
```

另外，[args.py:239-249](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L239-L249) 在 `--model-source modelscope` 时会调用 `snapshot_download` 从魔搭社区下载模型，方便国内网络环境。

**⑤ 启动前端。**
[python/minisgl/server/launch.py:113](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L113) 把 `start_subprocess` 作为回调交给 `run_api_server`，由后者决定「先起子进程，再起 uvicorn 还是起 shell」：

```python
run_api_server(server_args, start_subprocess, run_shell=run_shell)
```

#### 4.2.4 代码实践：启动服务并用 curl 调一次接口

**目标**：安装后启动单卡服务，用 `curl` 走 OpenAI 兼容接口拿到一次回复。

**步骤**：

1. **安装**（需要本机有 NVIDIA GPU 与匹配的 CUDA 驱动；详见 README）：
   ```bash
   git clone https://github.com/sgl-project/mini-sglang.git
   cd mini-sglang
   uv venv --python=3.12
   source .venv/bin/activate
   uv pip install -e .
   ```
   这几步对应 README 的 Quick Start。`uv venv` 建虚拟环境，`uv pip install -e .` 以「可编辑模式」安装，依赖列表见 [pyproject.toml:24-39](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L24-L39)（含 `sgl_kernel`、`flashinfer-python`、`apache-tvm-ffi`、`quack-kernels` 等外部 CUDA 库）。

2. **启动服务**（默认 host `127.0.0.1`、端口 `1919`）：
   ```bash
   python -m minisgl --model "Qwen/Qwen3-0.6B"
   ```
   观察日志：你会先看到若干 `minisgl-TP0-scheduler` / `minisgl-detokenizer-0` 的就绪消息（就是 4.2.3 ③ 里那条 ack 日志），最后看到 `API server is ready to serve on 127.0.0.1:1919`。

3. **另开一个终端**，用 curl 发一个非流式请求（`stream` 默认为 `false`）：
   ```bash
   curl -X POST http://127.0.0.1:1919/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "Qwen/Qwen3-0.6B",
       "messages": [{"role": "user", "content": "用一句话介绍你自己"}],
       "max_tokens": 64
     }'
   ```

**需要观察的现象**：服务启动时，主进程会阻塞在「等 ack」那一步，直到所有子进程就绪才开始监听端口；curl 收到的是一个完整的 JSON（`choices[0].message.content` 是模型回复），而不是逐字流式输出。

**预期结果**：返回形如下面的 JSON（`content` 内容因模型采样而异）：

```json
{
  "id": "chatcmpl-0",
  "object": "chat.completion",
  "model": "Qwen/Qwen3-0.6B",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

> 如果本机无 GPU，本实践记为「待本地验证（需要 NVIDIA GPU）」。你仍然可以阅读 [api_server.py:255-310](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L255-L310) 来理解这条 `/v1/chat/completions` 路由的请求/响应结构。

#### 4.2.5 小练习与答案

**练习 1**：`num_tokenizer=0`（默认）时，`ack_queue` 会收到几条「就绪」消息？分别来自谁？

**参考答案**：收到 2 条，即 `num_tokenizers + 2 = 2`。一条来自主 rank（rank 0）scheduler，一条来自 detokenizer（此时 detokenizer 兼任 tokenize，没有独立的 tokenizer 进程）。

**练习 2**：为什么子进程必须用 `spawn` 而不是 `fork`？

**参考答案**：CUDA 运行时和 GPU 上下文不能安全地通过 `fork` 被子进程继承，`fork` 会导致子进程拿到一个已经损坏/不可用的 CUDA 状态。`spawn` 让子进程重新启动 Python 解释器并重新初始化 CUDA，是 GPU 多进程的必要选择。

---

### 4.3 `--shell` 交互模式：在终端直接对话

#### 4.3.1 概念说明

除了 HTTP 服务，Mini-SGLang 还内置了一个终端聊天界面：加一个 `--shell` 标志，就不开 HTTP 端口，而是在终端里直接和模型一来一回地对话，并支持 `/reset` 清空历史、`/exit` 退出。

它复用了同一套前端逻辑（同一个 `FrontendManager`），只是把「HTTP 请求来源」换成了「你在终端敲的字」。所以 shell 模式不是另一套独立系统，而是「换了一种方式喂消息给前端」。

> **关于 `--shell` 这个名字**：源码里真正定义的 CLI 参数是 [`--shell-mode`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L220-L224)。README 和 `docs/features.md` 里写的 `--shell` 之所以也能用，是因为 `argparse` 默认开启「前缀匹配」：`--shell` 是 `--shell-mode` 的唯一前缀，没有其他 `--shell*` 选项与之冲突，于是被自动补全为 `--shell-mode`。两者效果完全相同。

#### 4.3.2 核心流程

```text
python -m minisgl --model ... --shell
        │
        ▼
   parse_args 解析到 shell_mode=True
        │  归一化：cuda_graph_max_bs=1, max_running_req=1, silent_output=True
        ▼
   run_api_server(run_shell=True)
        │  start_backend()   ← 仍然 fork scheduler/tokenizer
        │  但不再 uvicorn.run，而是 asyncio.run(shell())
        ▼
   shell():  prompt_toolkit 读你输入
        │  普通文本 → shell_completion() → TokenizeMsg → ZMQ → scheduler
        │  /reset   → 清空本地 history
        │  /exit    → 退出并杀掉所有子进程
```

#### 4.3.3 源码精读

**① shell 标志的解析与归一化。**
[python/minisgl/server/args.py:220-234](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L220-L234) 定义 `--shell-mode`，并在解析后做关键归一化：

```python
parser.add_argument("--shell-mode", action="store_true", help="Run the server in shell mode.")
...
run_shell |= kwargs.pop("shell_mode")
if run_shell:
    kwargs["cuda_graph_max_bs"] = 1
    kwargs["max_running_req"] = 1
    kwargs["silent_output"] = True
```

含义：shell 是「单用户、单流」场景，所以强制 `max_running_req=1`（同时只处理 1 个请求）、`cuda_graph_max_bs=1`（只为 batch=1 捕获 CUDA graph，省显存、加快启动）、`silent_output=True`（让 scheduler 进程闭嘴，避免刷屏干扰对话）。

**② 前端根据 run_shell 选择分支。**
[python/minisgl/server/api_server.py:411-452](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L411-L452) 是 `run_api_server`。它先构造全局 `FrontendManager`（同一个 ZMQ 队列），再调 `start_backend()` fork 子进程，最后二选一：

```python
if not run_shell:
    uvicorn.run(app, host=host, port=port)
else:
    asyncio.run(shell())
```

也就是说，**shell 模式下也会 fork 出 scheduler/tokenizer 子进程**，区别只是主进程不开 uvicorn，而是进入交互循环。另外它还断言了 shell 不支持 dummy 权重（`assert not config.use_dummy_weight`）。

**③ shell() 主循环。**
[python/minisgl/server/api_server.py:351-408](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L351-L408) 实现交互逻辑，要点是「在本地维护 `history` 列表」：

```python
history: List[Tuple[str, str]] = []
while True:
    cmd = (await session.prompt_async()).strip()
    ...
    if cmd == "/reset":
        history = []
        continue
    ...
    history_messages = []
    for user_msg, assistant_msg in history:
        history_messages.append(Message(role="user", content=user_msg))
        history_messages.append(Message(role="assistant", content=assistant_msg))
    req = OpenAICompletionRequest(
        messages=history_messages + [Message(role="user", content=cmd)], ...)
```

关键点：

- **历史只在主进程内存里**：`/reset` 把这个 `history` 列表清空。模型那边的 KV cache 是否被清，取决于 scheduler 侧的缓存策略（见后续 KV Cache 单元），但从「对话上下文」角度，`/reset` 之后下一轮请求就不再带历史 message 了。
- 每轮都把完整历史拼成 OpenAI 风格的 `messages` 再发送，所以模型能「记得」之前说过什么。
- 退出时（`/exit` 或 Ctrl-D），它会用 `psutil` 杀掉自己 fork 出来的所有子进程，保证干净退出。

**④ 复用前端发送通道。**
[python/minisgl/server/api_server.py:319-347](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L319-L347) 的 `shell_completion` 和 HTTP 路由一样，构造 `TokenizeMsg` 发给同一个 `FrontendManager`，再用 `stream_generate` 逐 token 拿回结果。这说明 **shell 和 HTTP 共用同一条数据通路**。

**⑤ shell 的采样参数默认值。**
[python/minisgl/env.py:61-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L61-L65) 定义了 shell 模式采样的默认值，可以通过环境变量覆盖（前缀 `MINISGL_`）：

```python
SHELL_MAX_TOKENS = EnvInt(2048)
SHELL_TOP_K = EnvInt(-1)
SHELL_TOP_P = EnvFloat(1.0)
SHELL_TEMPERATURE = EnvFloat(0.6)
```

例如想让 shell 输出更随机，可以 `MINISGL_SHELL_TEMPERATURE=1.0 python -m minisgl --model ... --shell`。

#### 4.3.4 代码实践：交互对话 + `/reset`

**目标**：用 shell 模式与模型对话，验证历史记忆与 `/reset` 的效果。

**步骤**：

1. 启动 shell 模式：
   ```bash
   python -m minisgl --model "Qwen/Qwen3-0.6B" --shell
   ```
2. 在 `$ ` 提示符下输入：
   ```
   我叫小明，请记住我的名字。
   ```
   等模型回复后，再输入：
   ```
   我刚才告诉过你我叫什么？
   ```
3. 输入 `/reset`，然后再问同样的问题：
   ```
   我刚才告诉过你我叫什么？
   ```

**需要观察的现象**：

- 第 2 步第二次提问时，模型应该能答出「小明」——因为历史 message 被一并带上了。
- 第 3 步 `/reset` 后再问，模型应当不再记得名字——因为本地 `history` 被清空，这一轮请求的 `messages` 里不再包含之前的对话。

**预期结果**：`/reset` 前后模型的「记忆」有明显差异，验证了历史是在 shell 主进程本地维护、每轮重新拼接发送的设计。

> 本机无 GPU 时记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 shell 模式要把 `max_running_req` 强制设成 1？

**参考答案**：shell 是单用户交互场景，同一时刻只会有一句话在生成。把并发请求数锁到 1，可以避免为多请求预留显存/调度开销，也契合「单条流式回复」的交互形态。

**练习 2**：`/reset` 到底改变了什么状态？

**参考答案**：它清空了 shell 主进程内存里的 `history` 列表，使得下一轮请求构造的 `messages` 不再包含历史对话。它并不会直接命令 scheduler 清空 KV cache（KV cache 的复用与回收是另一套机制，后续 KV Cache 单元会讲）。

**练习 3**：如何让 shell 模式输出的随机性更高？

**参考答案**：通过环境变量调高温度，例如 `MINISGL_SHELL_TEMPERATURE=1.0 python -m minisgl --model ... --shell`，因为 [env.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L61-L65) 里 `SHELL_TEMPERATURE` 默认是 0.6。

---

### 4.4 Docker 构建：一键可复现的运行环境

#### 4.4.1 概念说明

CUDA kernel 的安装对系统环境（CUDA 版本、驱动、Python 版本）很敏感，很容易在本机踩坑。Docker 把「操作系统 + CUDA + Python + 依赖 + 项目代码」整体打包成一个镜像，保证在任何装了 NVIDIA Container Toolkit 的机器上都能一致运行。

本项目的 `Dockerfile` 采用**多阶段构建（multi-stage build）**：先在一个 `builder` 阶段完成依赖安装，再把成果复制到一个 `runtime` 阶段，并创建非 root 用户、预设缓存目录。

#### 4.4.2 核心流程

```text
┌──────────── builder 阶段 ────────────┐
│  基础: nvidia/cuda:12.8.1-devel       │
│  装 python + uv                       │
│  uv venv → uv pip install -e .        │
│  uv pip install torch-c-dlpack-ext    │
└──────────────────┬────────────────────┘
                   │  COPY --from=builder /app /app
┌──────────── runtime 阶段 ────────────┐
│  创建非 root 用户 minisgl (uid 1001)   │
│  预建 HF / tvm-ffi / flashinfer 缓存目录│
│  设置 PATH / LD_LIBRARY_PATH / 缓存环境│
│  EXPOSE 1919                          │
│  ENTRYPOINT ["python","-m","minisgl"] │
│  CMD ["--help"]                       │
└───────────────────────────────────────┘
```

#### 4.4.3 源码精读

**① 基础镜像与版本参数。**
[Dockerfile:1-3](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/Dockerfile#L1-L3) 用 `ARG` 声明默认版本，方便覆盖：

```dockerfile
ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=24.04
ARG PYTHON_VERSION=3.12
```

**② builder 阶段：装 uv 并以可编辑模式安装项目。**
[Dockerfile:6-32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/Dockerfile#L6-L32) 基于 `cuda:...-devel`（带编译工具链），先安装 uv，再 `uv pip install -e .`，与本机安装方式完全一致：

```dockerfile
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
...
RUN uv venv --python=python${PYTHON_VERSION} /app/.venv \
    && . /app/.venv/bin/activate \
    && uv pip install -e . \
    && uv pip install torch-c-dlpack-ext
```

注意它额外装了 `torch-c-dlpack-ext`，这是某些 kernel 交互需要的辅助包。

**③ runtime 阶段：非 root 用户与缓存目录。**
[Dockerfile:34-72](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/Dockerfile#L34-L72) 把 `/app` 从 builder 复制过来，创建 `minisgl` 用户，并预建三个缓存目录：

```dockerfile
RUN useradd --create-home --shell /bin/bash --uid 1001 minisgl
COPY --from=builder --chown=minisgl:minisgl /app /app
RUN mkdir -p /app/.cache/huggingface /app/.cache/tvm-ffi /app/.cache/flashinfer \
    && chown -R minisgl:minisgl /app/.cache
```

[Dockerfile:63-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/Dockerfile#L63-L65) 把这些目录通过环境变量指给对应库，让 HuggingFace 模型、tvm-ffi JIT kernel、flashinfer 工作区都落到可挂载的位置：

```dockerfile
ENV HF_HOME=/app/.cache/huggingface
ENV TVM_FFI_CACHE_DIR=/app/.cache/tvm-ffi
ENV FLASHINFER_WORKSPACE_BASE=/app/.cache/flashinfer
```

> 细节：runtime 阶段也用了 `cuda:...-devel`（而非 `-runtime` 或 `-base`），这是因为某些 kernel 在运行期仍需要 devel 包里的头文件/编译工具。这是与「最小镜像」取舍后的选择。

**④ 入口与默认命令。**
[Dockerfile:69-72](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/Dockerfile#L69-L72) 暴露端口并固定入口：

```dockerfile
EXPOSE 1919
ENTRYPOINT ["python", "-m", "minisgl"]
CMD ["--help"]
```

`ENTRYPOINT` 固定为 `python -m minisgl`，`CMD` 给默认参数 `--help`。这意味着：

- `docker run minisgl` 等价于 `python -m minisgl --help`；
- `docker run minisgl --model Qwen/Qwen3-0.6B` 时，`--model ...` 会**替换** `CMD`，接在 `ENTRYPOINT` 后面，等价于 `python -m minisgl --model Qwen/Qwen3-0.6B`。

#### 4.4.4 代码实践：用 Docker 跑起来

**目标**：构建镜像并在容器内启动服务。

**前置**：已安装 Docker 与 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。

**步骤**：

1. 构建镜像（在项目根目录）：
   ```bash
   docker build -t minisgl .
   ```
2. 以服务方式运行（把容器 1919 端口映射到本机）：
   ```bash
   docker run --gpus all -p 1919:1919 \
       minisgl --model Qwen/Qwen3-0.6B --host 0.0.0.0
   ```
   注意 `--host 0.0.0.0`：容器内必须监听 `0.0.0.0` 而不是默认的 `127.0.0.1`，否则本机访问不到。
3. （可选）用数据卷持久化缓存，加快二次启动：
   ```bash
   docker run --gpus all -p 1919:1919 \
       -v huggingface_cache:/app/.cache/huggingface \
       -v tvm_cache:/app/.cache/tvm-ffi \
       -v flashinfer_cache:/app/.cache/flashinfer \
       minisgl --model Qwen/Qwen3-0.6B --host 0.0.0.0
   ```
4. （可选）交互 shell 模式：
   ```bash
   docker run -it --gpus all minisgl --model Qwen/Qwen3-0.6B --shell
   ```

**需要观察的现象**：第一次构建会拉取较大的 CUDA devel 基础镜像并编译安装依赖，耗时较长；首次运行还需下载模型与 JIT 编译 kernel；加上数据卷后二次启动明显变快。

**预期结果**：容器日志出现 `API server is ready to serve on 0.0.0.0:1919`，本机可用 4.2.4 的 `curl` 命令（把 `127.0.0.1` 换成实际映射地址）访问到模型。

> 无 GPU 或未装 NVIDIA Container Toolkit 时记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Dockerfile 要分 builder 和 runtime 两个阶段？

**参考答案**：builder 阶段负责完成所有编译/安装（带完整 devel 工具链），产物复制到 runtime 阶段。这样可以让镜像更聚焦于「运行」，并方便在 runtime 阶段统一设置用户、缓存目录与环境变量，构建过程也更易缓存。

**练习 2**：为什么 `docker run` 时要加 `--host 0.0.0.0`？

**参考答案**：Mini-SGLang 默认监听 `127.0.0.1`（[args.py:16](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L16)）。在容器内，`127.0.0.1` 只能被容器自己访问，宿主机即便做了 `-p 1919:1919` 端口映射也连不上。监听 `0.0.0.0` 才能让端口映射生效，从而从宿主机访问服务。

---

## 5. 综合实践：把「安装 → 启动 → 调用 → 交互」完整走一遍

把本讲的四个最小模块串起来，完成一次端到端体验。任选「本机直装」或「Docker」一条路线即可。

**任务清单**：

1. **安装**：用 `uv venv --python=3.12 && source .venv/bin/activate && uv pip install -e .`（或 `docker build -t minisgl .`）完成环境准备。
2. **启动服务**：`python -m minisgl --model "Qwen/Qwen3-0.6B"`（Docker 路线则 `docker run --gpus all -p 1919:1919 minisgl --model Qwen/Qwen3-0.6B --host 0.0.0.0`）。
3. **HTTP 调用**：用 `curl` 调 `/v1/chat/completions` 拿到一次非流式 JSON 回复。
4. **切换 shell**：停止服务，改用 `--shell` 启动，进行两轮带历史的多轮对话，再用 `/reset` 验证历史被清空。
5. **对照源码**：在终端找到日志里的 `API server is ready ...`，回到本讲 4.2.3 ③，指出这条日志来自 [launch.py 的 `logger.info`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L105-L111) 之后的 [api_server.py 里 `run_api_server` 的就绪打印](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L448)，并解释它为什么一定在「收齐所有 ack 之后」才出现。

**验收标准**：

- 能用一句话说清 `python -m minisgl` 触达的第一个函数是 `launch_server`；
- 能解释服务为什么会在「子进程就绪」之后才开始监听端口；
- 能区分 HTTP 模式与 shell 模式在 `run_api_server` 里的分支差异；
- （若完成 Docker 路线）能解释 `ENTRYPOINT`/`CMD` 与追加参数的关系。

> 全程需要 NVIDIA GPU。若无 GPU，则把步骤 1–4 标注为「待本地验证」，并重点完成步骤 5 的源码对照。

## 6. 本讲小结

- 命令行入口是 `python -m minisgl`，它执行 [`__main__.py`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/__main__.py)，后者只做一件事：调用 `launch_server()`；项目没有注册可执行命令。
- `launch_server` 的职责是「解析参数 → spawn 子进程 → 等 ack → 启动前端」，其中子进程用 `spawn` 启动、按 `tp_size` 创建 scheduler，并创建 tokenizer/detokenizer 进程。
- 启动同步靠 `ack_queue`：主进程必须收齐 `num_tokenizers + 2` 条「就绪」消息后才会开服，这保证了不会在子进程未就绪时接收请求。
- HTTP 服务监听默认 `127.0.0.1:1919`，提供 OpenAI 兼容的 `/v1/chat/completions`，可用 `curl` 直接调用并拿到非流式 JSON。
- `--shell`（即 `--shell-mode`）会强制单请求、单 batch、静默输出，复用同一前端通路，在终端交互；`/reset` 清空本地对话历史。
- Dockerfile 用多阶段构建，固定 `ENTRYPOINT ["python","-m","minisgl"]`，预设 HF/tvm-ffi/flashinfer 缓存目录，运行时需加 `--host 0.0.0.0` 才能被宿主机访问。

## 7. 下一步学习建议

现在你已经能把 Mini-SGLang 跑起来，但对「屏幕背后」还只有一个粗略印象。建议：

1. **下一讲 u1-l3《目录结构与模块地图》**：系统梳理 `python/minisgl` 下各子包的职责，建立从「文件夹」到「功能」的映射，为阅读源码打基础。
2. **接着 u1-l4《进程架构与请求生命周期》**：本讲你多次看到「fork 子进程」「ZMQ」「ack」，下一讲会把这套多进程拓扑和一条请求的完整生命周期讲透——它正好解释了本讲里「为什么要等 ack」「为什么有那么多 `minisgl-TP*` 进程」。
3. **动手延伸**：尝试用 `--tp 2` 在多卡上启动（如果有多卡），观察日志里多出来的 `minisgl-TP1-scheduler` 进程；或阅读 [docs/features.md](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md) 了解更多 CLI 开关（`--cache`、`--attn`、`--page-size` 等），它们会在后续进阶讲义中逐一展开。
