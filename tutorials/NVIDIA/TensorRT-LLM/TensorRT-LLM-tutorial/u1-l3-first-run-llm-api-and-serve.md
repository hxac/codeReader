# 首次运行：LLM API 与 trtllm-serve

> 所属单元：u1「走进 TensorRT-LLM 与首次运行」
> 依赖讲义：u1-l1（项目定位与整体架构）、u1-l2（安装、容器与从源码构建）

## 1. 本讲目标

在 u1-l1 我们画出了「HF 模型 → LLM API → Executor → Scheduler → 模型前向 → Sampling → 生成 Token」的高空流程图，在 u1-l2 我们把环境装好了。本讲要把这张图**落到地**：用两种入口真正跑通一个模型。

学完本讲，你应该能够：

- 用 Python **LLM API** 写一段最短的离线推理脚本，输入文本、输出文本。
- 用 `trtllm-serve` 命令启动一个 **OpenAI 兼容**的在线服务，并用 `curl` 发送请求。
- 理解 tokenizer / detokenizer **由 LLM 实例托管**——你传进字符串、拿回字符串，token 化/反 token 化在内部完成。
- 在源码里定位 `trtllm-serve` 的命令入口 `commands/serve.py`，说清「命令行参数 → LLM 实例 → OpenAI 服务」的拼装过程。

一句话总结：**离线用 `LLM`，在线用 `trtllm-serve`，二者最终都构造同一个 `LLM` 类。**

## 2. 前置知识

本讲是「第一次跑模型」，不要求你懂调度器或 KV cache。只需具备以下概念（不熟悉的术语会随讲随解释）：

- **LLM 推理的输入输出**：给模型一段文本（prompt），它接着往后写一段文本（completion）。模型本身只认 token id（整数序列），所以中间需要 token 化与反 token 化。
- **tokenizer / detokenizer**：tokenizer 把字符串切成 token id（编码），detokenizer 把 token id 还原成字符串（解码）。本讲你会看到，这套翻译工作由 `LLM` 实例自动托管，使用者无需手动处理。
- **离线推理 vs 在线服务**：
  - *离线*：写一段 Python 脚本，构造 `LLM`、调用 `.generate()`、拿结果。适合批处理、调试、实验。
  - *在线*：起一个常驻 HTTP 服务，任何客户端（`curl`、前端、SDK）都能按网络协议发请求。适合部署给真实用户。
- **OpenAI API 协议**：业界事实标准的 HTTP 接口约定，例如用 `POST /v1/chat/completions` 做对话补全。TRT-LLM 的服务端**完全兼容**这套协议，所以任何为 OpenAI 写的客户端都能直接连。
- **环境**：按 u1-l2 装好 TRT-LLM（容器或 pip），需要至少一块 NVIDIA GPU。本讲示例若无法在本地 GPU 上运行，会明确标注「待本地验证」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `examples/llm-api/quickstart_example.py` | 最短的 LLM API 离线推理示例，本讲的「样板工程」 |
| `docs/source/quick-start-guide.md` | 官方快速上手文档，含 `trtllm-serve` 启动命令与 `curl` 示例 |
| `tensorrt_llm/llmapi/__init__.py` | llmapi 子包的导出表，声明 `LLM`、`SamplingParams` 等公共对象从哪来 |
| `tensorrt_llm/__init__.py` | 顶层包导出，`from tensorrt_llm import LLM, SamplingParams` 的真正出处 |
| `tensorrt_llm/llmapi/llm.py` | `LLM` 类与 `generate()` 方法的实现 |
| `tensorrt_llm/commands/serve.py` | `trtllm-serve` 命令的全部实现（CLI 参数解析 → 构造 LLM → 起 OpenAI 服务） |
| `tensorrt_llm/serve/openai_server.py` | OpenAI 兼容服务器，注册 `/v1/chat/completions` 等路由 |
| `setup.py` | 把 `trtllm-serve` 注册为控制台命令（entry point）的地方 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 LLM API 用法（离线）**、**4.2 trtllm-serve 命令（在线）**、**4.3 OpenAI 协议**。

### 4.1 LLM API 用法

#### 4.1.1 概念说明

**LLM API** 是 TRT-LLM 提供的一套 Python 接口，目标是「**一个 `LLM` 对象搞定一切**」：你只需指定一个 HuggingFace 仓库名或本地模型路径，`LLM` 就会替你完成模型加载、优化、token 化、推理、反 token 化。

它的典型用法只有三步：

1. 构造：`llm = LLM(model="...")`
2. 配置采样参数：`SamplingParams(temperature=..., top_p=...)`
3. 生成：`llm.generate(prompts, sampling_params)` → 拿到文本结果

为什么需要 `SamplingParams`？因为「下一个 token 选哪个」不是确定性的——模型给出的是每个候选 token 的概率分布（logits），采样参数决定了如何从这个分布里挑 token：

- `temperature` 越高，分布越平，输出越随机；为 0 时退化为贪心（argmax）。
- `top_p`（核采样）只从累计概率达到 p 的候选集合里采样，过滤掉长尾低概率 token。

> 关于采样的完整机制（top-k、罚分、guided decoding 等）会在 u8-l3「Decoder 与 Sampling」深入，本讲只需把它当成「生成行为的旋钮」即可。

#### 4.1.2 核心流程

离线推理的一次完整调用，从使用者的视角是这样的：

```text
用户脚本
  │  文本 prompts + SamplingParams
  ▼
LLM(model=...)          # ① 加载模型 + 托管 tokenizer
  │
  ▼
LLM.generate(prompts, sampling_params)
  │  ② 内部对每个 prompt 调 generate_async，再阻塞等待全部完成
  ▼
Executor → Scheduler → 模型前向 → Sampling   # （u3 会逐段拆解，本讲当成黑盒）
  ▼
RequestOutput           # ③ 已反 token 化的文本结果
  │  output.prompt          -> 原始输入文本
  │  output.outputs[0].text -> 生成的文本
  ▼
print(...)               # 拿到字符串，直接用
```

关键认知：**整个链路两头都是字符串，token id 的编解码被 `LLM` 藏在中间**。这就是「tokenizer/detokenizer 由 LLM 托管」的含义。

#### 4.1.3 源码精读

先看官方最短示例 [examples/llm-api/quickstart_example.py:1-L34](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/llm-api/quickstart_example.py#L1-L34)，它就是本讲的样板。关键几行：

```python
from tensorrt_llm import LLM, SamplingParams          # 第 1 行：导入
...
llm = LLM(model="TinyLlama/TinyLlama-1.1B-Chat-v1.0") # 第 8 行：构造
...
sampling_params = SamplingParams(temperature=0.8, top_p=0.95)  # 第 18 行：采样参数
for output in llm.generate(prompts, sampling_params):          # 第 20 行：生成
    print(f"Prompt: {output.prompt!r}, Generated text: {output.outputs[0].text!r}")
```

注意构造时**只传了 `model`**，没有传 tokenizer。`LLM` 会自动从该 HF 仓库加载对应的 tokenizer。结果对象 `output` 直接给出 `.outputs[0].text`（已是字符串），证明反 token 化也由 `LLM` 完成。

这两行导入为什么能写成 `from tensorrt_llm import ...`？因为顶层包把它们重新导出。看 [tensorrt_llm/__init__.py:120-L125](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L120-L125)：

```python
from .llmapi import LLM, AsyncLLM, MultimodalEncoder   # 第 120 行
...
from .sampling_params import SamplingParams             # 第 125 行
```

而 `llmapi` 子包又在 [tensorrt_llm/llmapi/__init__.py:5-L7](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py#L5-L7) 声明 `SamplingParams` 与 `LLM` 的真正出处：

```python
from ..sampling_params import GuidedDecodingParams, SamplingParams   # 第 5 行
...
from .llm import LLM, RequestOutput                                   # 第 7 行
```

> 顺带一提：`llmapi/__init__.py` 还导出了一大批配置类（`TorchLlmArgs`、`KvCacheConfig`、`SchedulerConfig`、`CudaGraphConfig` …）。这些是「调参旋钮」，本讲用不到，会在 u4「配置体系」集中讲。

`LLM` 类本身在哪、`generate` 做了什么？看 [tensorrt_llm/llmapi/llm.py:1648-L1664](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L1648-L1664)：

```python
class LLM(_TorchLLM):
    def __init__(self, model, tokenizer=None, tokenizer_mode='auto',
                 skip_tokenizer_init=False, trust_remote_code=False,
                 tensor_parallel_size=1, dtype="auto", revision=None,
                 tokenizer_revision=None, **kwargs) -> None:
        super().__init__(model, tokenizer, tokenizer_mode, skip_tokenizer_init,
                         trust_remote_code, tensor_parallel_size, dtype,
                         revision, tokenizer_revision, **kwargs)
```

可以看到 `LLM` 是个**薄壳**，真正逻辑在父类 `_TorchLLM`；构造参数里有 `tokenizer`、`tokenizer_mode`、`skip_tokenizer_init`——这正是「tokenizer 由 LLM 托管」的直接证据：你可以覆盖它，但默认会按模型自动加载。

`generate` 的签名在 [tensorrt_llm/llmapi/llm.py:420-L440](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L420-L440)。它是**同步**接口，接受单个或一批 prompt，返回单个或一批 `RequestOutput`：

```python
def generate(self, inputs, sampling_params=None, use_tqdm=True, ...):
    """Generate output for the given prompts in the synchronous mode."""
```

它内部（[llm.py:495-L522](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L495-L522)）对每个 prompt 调 `generate_async` 拿到 future，再用 `tqdm` 进度条逐个 `future.result()` 阻塞等待，最后返回。所以「同步 `generate`」≈「异步 `generate_async` + 等待」。

#### 4.1.4 代码实践

**实践目标**：基于官方 `quickstart_example.py` 改写一段脚本，把模型换成更明确的提示，并观察「字符串进、字符串出」。

**操作步骤**：

1. 确认环境（u1-l2）已装好 TRT-LLM，且能访问 HuggingFace（首次会自动下载 `TinyLlama/TinyLlama-1.1B-Chat-v1.0` 的权重与 tokenizer）。
2. 复制 `examples/llm-api/quickstart_example.py` 为本地脚本 `my_first_run.py`。
3. 把 prompts 改成你感兴趣的句子，例如：

```python
# 示例代码（基于 examples/llm-api/quickstart_example.py 改写）
from tensorrt_llm import LLM, SamplingParams

llm = LLM(model="TinyLlama/TinyLlama-1.1B-Chat-v1.0")

prompts = [
    "The capital of France is",
    "Q: 2 + 2 = ? A:",
]
sampling_params = SamplingParams(temperature=0.0, max_tokens=16)  # temperature=0 贪心，结果可复现
for output in llm.generate(prompts, sampling_params):
    print(f"[{output.prompt}] -> {output.outputs[0].text!r}")
```

4. 运行：`python my_first_run.py`

**需要观察的现象**：

- 控制台出现 `tqdm` 进度条（`Processed requests`），说明 `generate` 在逐个等待 future。
- 每条结果同时打印了输入文本 `output.prompt` 和生成文本 `output.outputs[0].text`，二者都是**字符串**。

**预期结果**：由于 `temperature=0`，同一提示每次输出应一致。`"The capital of France is"` 这类常识提示通常会被正确续写（官方示例注释见 [quickstart_example.py:25-L29](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/llm-api/quickstart_example.py#L25-L29)，例如 `'The capital of France is'` → `'Paris.'`）。具体文本**待本地验证**（取决于模型版本与是否完整下载）。

#### 4.1.5 小练习与答案

**练习 1**：示例里没传 tokenizer，模型怎么知道用哪种分词？
**答案**：`LLM` 构造时根据 `model` 指向的 HF 仓库，自动加载其内置的 `tokenizer_config.json` 对应的 tokenizer（见 `llm.py` 构造参数 `tokenizer=None, tokenizer_mode='auto'`）。所以你只需给模型，tokenizer 由 `LLM` 托管。

**练习 2**：把 `temperature` 从 0.8 改成 0，运行两次同一 prompt，输出会有什么不同？
**答案**：`temperature=0` 退化为贪心采样（argmax），每次运行结果一致、可复现；`temperature=0.8` 引入随机性，每次结果通常不同。

**练习 3**：`generate` 返回的 `output.outputs[0].text` 是 token id 还是字符串？为什么？
**答案**：是字符串。因为 detokenizer 由 `LLM` 托管，`generate` 在返回前已把模型生成的 token id 反编码成文本，使用者直接拿到可读结果。

---

### 4.2 trtllm-serve 命令

#### 4.2.1 概念说明

`trtllm-serve` 是 TRT-LLM 的**命令行入口**，用来启动一个常驻的、**OpenAI 兼容**的 HTTP 服务。和 4.1 的离线脚本相比：

| 维度 | LLM API（离线） | trtllm-serve（在线） |
|------|----------------|---------------------|
| 调用方式 | Python 脚本内调函数 | 命令行起进程，网络请求 |
| 生命周期 | 脚本结束即销毁 | 常驻，持续接请求 |
| 客户端 | 自己写的 Python | 任何 HTTP 客户端（curl、SDK、前端） |
| 底层 | 直接构造 `LLM` | **同样**构造 `LLM`，再包一层 OpenAI 服务 |

最后一行是关键洞察：**两个入口最终都构造同一个 `LLM` 类**，`trtllm-serve` 只是在它外面多套了一个 HTTP 服务壳。

#### 4.2.2 核心流程

从敲下命令到服务就绪的链路（本讲只追到「LLM + 服务壳」，内部调度留到 u3）：

```text
$ trtllm-serve MODEL [--host ...] [--port 8000] [--backend pytorch] ...
        │
        ▼
控制台命令 trtllm-serve   （setup.py 注册的 entry point）
        │
        ▼
serve.py: main (DefaultGroup)   # 第 1 站：命令分发
        │  第一个参数若不是子命令，默认走 serve
        ▼
serve.py: serve(...)            # 第 2 站：解析所有 --xxx CLI 参数
        │
        ▼
serve.py: get_llm_args(...)     # 第 3 站：把 CLI 参数整理成 llm_args 字典
        │
        ▼
serve.py: launch_server(...)    # 第 4 站：构造 LLM + 构造 OpenAIServer
        │
        ├─ llm = LLM(**llm_args)            # 与离线入口同一个类！
        ├─ server = OpenAIServer(generator=llm, ...)
        └─ uvloop.run(server(host, port))   # 第 5 站：事件循环启动 HTTP 服务
        ▼
服务就绪，监听 http://host:port
```

#### 4.2.3 源码精读

**① 命令是怎么注册成 `trtllm-serve` 的？** 在打包配置 [setup.py:467-L472](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/setup.py#L467-L472) 里：

```python
entry_points={
    'console_scripts': [
        'trtllm-bench=tensorrt_llm.commands.bench:main',
        'trtllm-serve=tensorrt_llm.commands.serve:main',   # 第 470 行
        'trtllm-eval=tensorrt_llm.commands.eval:main'
    ],
},
```

这行声明：装好包后，终端里的 `trtllm-serve` 就是调用 `tensorrt_llm.commands.serve` 模块里的 `main` 对象。

**② `main` 是什么？** 它是一个 Click 命令组 [serve.py:2479-L2486](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L2479-L2486)：

```python
main = DefaultGroup(
    commands={
        "serve": serve,
        "disaggregated": disaggregated,
        "disaggregated_mpi_worker": disaggregated_mpi_worker,
        "mm_embedding_serve": serve_encoder,
        "embeddings": serve_embedding
    })
```

`DefaultGroup` 的巧思在 [serve.py:2472-L2476](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L2472-L2476)：如果第一个参数不是已注册的子命令，就**默认当作 `serve` 子命令**处理。所以 `trtllm-serve MODEL` 等价于 `trtllm-serve serve MODEL`。

**③ `serve` 子命令的参数。** 它用 Click 装饰器声明了一长串 CLI 选项，模型是必填位置参数 [serve.py:935-L936](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L935-L936)。最常用的几个默认值：

- `--host` 默认 `localhost`、`--port` 默认 `8000`（[serve.py:964-L973](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L964-L973)）。
- `--backend` 默认 `pytorch`，可选 `_autodeploy`（[serve.py:974-L979](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L974-L979)）——这正是 u1-l1 讲的「PyTorch 默认后端 / AutoDeploy beta 后端」在命令行的开关。

**④ 从参数到 LLM。** `serve` 函数体（[serve.py:1266-L1293](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L1266-L1293)）做完若干解析后，在末尾分叉 [serve.py:1493-L1498](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L1493-L1498)：如果是 VisualGen（扩散模型）走 `_serve_visual_gen()`，否则走 `_serve_llm()`。普通文本模型走 `_serve_llm()`，它调用 `get_llm_args(...)` 把 CLI 参数归并成字典，再调用 `launch_server(...)`（[serve.py:1465-L1481](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L1465-L1481)）。

**⑤ `launch_server`：构造 LLM + 起服务。** 这是整条链的核心 [serve.py:517-L619](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L517-L619)。先绑定端口（验证可用性），然后按后端构造 `llm`：

```python
if backend == 'pytorch':
    llm_args.pop("build_config", None)
    llm = PyTorchLLM(**llm_args)        # 第 575 行
elif backend == '_autodeploy':
    from tensorrt_llm._torch.auto_deploy import LLM as AutoDeployLLM
    llm_args.pop("build_config", None)
    llm = AutoDeployLLM(**llm_args)     # 第 581 行
```

注意第 26 行的导入：`from tensorrt_llm import LLM as PyTorchLLM`——所以这里的 `PyTorchLLM` **正是** 4.1 节那个 `LLM` 类！离线和在线两条路在这里汇合。

随后用这个 `llm` 构造 OpenAI 服务并启动（[serve.py:597-L616](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L597-L616)）：

```python
server = OpenAIServer(generator=llm, model=model, tool_parser=tool_parser, ...)
_apply_fastapi_middlewares(server.app, middleware)
...
uvloop.run(server(host, port, sockets=[s]))   # 第 616 行：进入事件循环
```

`uvloop.run` 会阻塞当前进程，持续监听 `host:port`，直到收到关闭信号。

#### 4.2.4 代码实践

**实践目标**：用 `trtllm-serve` 启动服务，确认进程常驻、监听端口。

**操作步骤**：

1. 在一个终端执行（官方文档 [quick-start-guide.md:18-L20](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/quick-start-guide.md#L18-L20) 的最小命令）：

```bash
trtllm-serve "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
```

2. 若在 Docker 容器内，按 [quick-start-guide.md:31-L37](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/quick-start-guide.md#L31-L37) 提示，要么用 `-p 8000:8000` 暴露端口，要么另开终端 `docker exec -it <container_id> bash` 进入容器。
3. （可选）显式指定端口与日志级别，观察 Click 参数生效：

```bash
trtllm-serve "TinyLlama/TinyLlama-1.1B-Chat-v1.0" --host 0.0.0.0 --port 8000 --log_level info
```

**需要观察的现象**：

- 日志依次出现「加载模型」「绑定端口」「服务就绪（listening）」之类的信息。
- 进程**不会退出**（常驻），`Ctrl+C` 才会终止。
- 用 `curl http://localhost:8000/v1/models` 能拿到模型列表（见 4.3）。

**预期结果**：服务在 `http://localhost:8000` 就绪。首次启动需要加载权重与编译，耗时较长；具体启动耗时与显存占用**待本地验证**（取决于 GPU 型号与模型大小）。

> 提示：若端口被占用，`launch_server` 不会静默失败，而是用 `_diagnose_port_in_use`（[serve.py:336-L364](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L336-L364)）打印当前占用该端口的进程，方便排查。

#### 4.2.5 小练习与答案

**练习 1**：`trtllm-serve MODEL` 为什么不需要写子命令名 `serve`？
**答案**：因为入口 `main` 是 `DefaultGroup`，其 `resolve_command`（serve.py:2472）规定：第一个参数若不是已注册子命令（如 `disaggregated`/`embeddings`），就默认路由到 `serve` 子命令。

**练习 2**：`--backend _autodeploy` 会改变哪一步？两个后端共享什么？
**答案**：它改变 `launch_server` 里构造 `llm` 的那一步——`pytorch` 构造 `LLM`（即 `PyTorchLLM`），`_autodeploy` 构造 `AutoDeployLLM`。两者随后都被包进同一个 `OpenAIServer`，所以**共享 OpenAI 服务壳**；差异仅在模型执行路径（u1-l1 讲的 AutoDeploy 做 FX 图变换 + torch.export）。

**练习 3**：在线服务的 `llm` 和 4.1 离线脚本的 `LLM` 是同一个类吗？
**答案**：是。`serve.py` 第 26 行 `from tensorrt_llm import LLM as PyTorchLLM`，`launch_server` 里 `llm = PyTorchLLM(**llm_args)` 用的就是 `tensorrt_llm.llmapi.llm.LLM`。两条入口在此汇合。

---

### 4.3 OpenAI 协议

#### 4.3.1 概念说明

**OpenAI API 协议**是业界广泛采用的 HTTP 接口约定：用 JSON 请求体描述「对话/补全」，用 JSON 响应体返回「生成结果 + token 用量」。TRT-LLM 的 `OpenAIServer` **完全兼容**这套约定，因此任何为 OpenAI 写的客户端（OpenAI Python SDK、LangChain、curl、前端组件）都能不改代码地连上 `trtllm-serve`。

初学者需要认识两件事：

- **路由（endpoint）**：不同需求走不同 URL。最常用的是 `POST /v1/chat/completions`（对话补全，带 `messages` 角色）；还有 `/v1/completions`（纯文本补全）、`/v1/models`（列出可用模型）、`/v1/embeddings`（向量）等。
- **请求/响应字段**：请求里典型有 `model`、`messages`（`role`+`content`）、`max_tokens`、`temperature`；响应里典型有 `choices[].message.content`（生成文本）和 `usage`（`prompt_tokens`/`completion_tokens`/`total_tokens`）。

#### 4.3.2 核心流程

一次 `/v1/chat/completions` 请求在服务端的旅程（黑盒视角）：

```text
curl POST /v1/chat/completions  {model, messages, max_tokens, temperature}
        │
        ▼
FastAPI 路由（OpenAIServer.register_routes 注册）
        │  把 messages 应用 chat template → 拼成一段 prompt 文本
        ▼
chat_tokenization / 输入预处理   # 把对话文本交给 tokenizer
        │
        ▼
generator (= llm) 的异步生成接口
        │
        ▼
LLM → Executor → Scheduler → 前向 → Sampling   # （u3 拆解）
        │
        ▼
detokenize → 组装成 OpenAI 响应 JSON
        │  choices[].message.content, usage{prompt_tokens,...}
        ▼
返回给 curl
```

注意：聊天场景下，客户端发来的是结构化 `messages`，服务端要先用模型的 **chat template** 把它拼成模型认识的纯文本，再交给 tokenizer——这一步是 `trtllm-serve` 相对「裸 LLM API」额外承担的协议适配工作。

#### 4.3.3 源码精读

路由在 `OpenAIServer.register_routes` 里集中注册 [openai_server.py:791-L864](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/serve/openai_server.py#L791-L864)。与文本生成直接相关的几行：

```python
self.app.add_api_route("/v1/completions",
                       self.openai_completion, methods=["POST"])           # 第 849 行
self.app.add_api_route(
    "/v1/chat/completions",
    self.openai_chat if not self.use_harmony else self.chat_harmony,
    methods=["POST"])                                                     # 第 852-855 行
```

可以看到 `/v1/chat/completions` 由 `self.openai_chat` 处理，`/v1/completions` 由 `self.openai_completion` 处理；此外还注册了 `/health`、`/v1/models`、`/v1/responses` 等。这些都是标准 OpenAI 兼容端点。

官方文档给了一个可直接复制的 `curl` 例子 [quick-start-guide.md:42-L53](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/quick-start-guide.md#L42-L53)：

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -d '{
        "model": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        "messages":[{"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Where is New York? Tell me in a single sentence."}],
        "max_tokens": 32,
        "temperature": 0
    }'
```

对应的响应（[quick-start-guide.md:57-L82](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/quick-start-guide.md#L57-L82)）就是标准 OpenAI 结构：

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
  "choices": [
    { "index": 0,
      "message": { "role": "assistant", "content": "New York is a city in ..." },
      "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 43, "total_tokens": 69, "completion_tokens": 26 }
}
```

`choices[0].message.content` 是生成文本，`usage` 给出 token 计数——这恰好对应 4.1 里被 `LLM` 托管的 token 化/反 token 化在协议层的体现。

#### 4.3.4 代码实践

**实践目标**：向 4.2 启动的服务发一次 `/v1/chat/completions` 请求，确认协议兼容。

**操作步骤**：

1. 确保 4.2 的 `trtllm-serve` 已在 `localhost:8000` 就绪（另开一个终端）。
2. 直接复制上面的 `curl` 命令运行；或用 OpenAI Python SDK（不改 `base_url` 之外的东西）：

```python
# 示例代码：用 OpenAI SDK 连本地 trtllm-serve
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")
resp = client.chat.completions.create(
    model="TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    messages=[{"role": "user", "content": "Where is New York? Tell me in a single sentence."}],
    max_tokens=32, temperature=0)
print(resp.choices[0].message.content)
print(resp.usage)
```

3. 再试一个 `GET /v1/models`：`curl http://localhost:8000/v1/models`，确认能列出已加载模型。

**需要观察的现象**：

- `curl` 返回的 JSON 含 `choices`、`message.content`、`usage` 字段。
- OpenAI SDK 能无障碍解析响应（说明协议真的兼容）。
- `/v1/models` 返回的模型名与启动时传入的 `model` 一致。

**预期结果**：响应结构与官方文档示例一致；具体生成文本**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`/v1/chat/completions` 和 `/v1/completions` 的主要区别是什么？
**答案**：前者接受结构化的 `messages`（带 `role`，如 system/user/assistant），服务端会用 chat template 拼成模型输入；后者接受一段纯文本 prompt 直接补全。聊天场景用前者，原始补全场景用后者。

**练习 2**：响应里的 `usage.completion_tokens` 是怎么来的？
**答案**：它是模型实际生成的 token 数量。因为 `LLM` 内部既做 token 化（输入）也做反 token 化（输出），它能精确统计两边的 token 数，并在响应里按 OpenAI 协议上报。

**练习 3**：为什么说「任何 OpenAI 客户端都能直接连 trtllm-serve」？
**答案**：因为 `OpenAIServer.register_routes` 注册的端点路径（`/v1/chat/completions` 等）和请求/响应字段都与 OpenAI 官方一致。客户端只需把 `base_url` 指向 `trtllm-serve` 的地址即可。

---

## 5. 综合实践

把三个模块串起来：**同一个模型，分别用离线 LLM API 和在线 trtllm-serve 跑，对比两边的输出**。

**任务**：

1. **离线侧**：运行 4.1.4 改写的 `my_first_run.py`，记录提示 `"The capital of France is"`（`temperature=0`）的生成文本。
2. **在线侧**：按 4.2.4 启动 `trtllm-serve "TinyLlama/TinyLlama-1.1B-Chat-v1.0"`，再用 4.3.4 的 `curl`/SDK 发一条 `temperature=0`、`max_tokens` 相近的请求，记录 `choices[0].message.content`。
3. **对比与分析**（写一段简短结论）：
   - 两次输出是否接近？为什么可能**不完全相同**？（提示：离线是「裸 prompt 续写」，在线是「套了 chat template 的对话」，即便 `temperature=0`，输入给模型的实际文本也可能不同。）
   - 在源码里指出两条入口汇合的那一行（答案：`serve.py` 的 `llm = PyTorchLLM(**llm_args)`），说明「殊途同归」。
4. **进阶观察**：在 `serve.py:launch_server` 处（[serve.py:517-L619](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/commands/serve.py#L517-L619)）对照源码标注：构造 `llm`、构造 `OpenAIServer`、`uvloop.run` 三步分别在哪一行。

> 全程若受限于本地 GPU/网络，离线脚本与在线服务都**待本地验证**；但「源码阅读型」的第 3、4 步（指认汇合点、标注行号）不依赖运行，现在就能完成。

## 6. 本讲小结

- **离线入口**用 `LLM` API：`LLM(model=...)` → `SamplingParams(...)` → `llm.generate(prompts, ...)`，字符串进、字符串出。
- **在线入口**用 `trtllm-serve` 命令：经 `main`（DefaultGroup）→ `serve()` → `get_llm_args()` → `launch_server()`，最终构造**同一个 `LLM` 类**，再包一层 `OpenAIServer`。
- **两条入口殊途同归**：`serve.py` 里 `llm = PyTorchLLM(**llm_args)` 用的就是 `tensorrt_llm.llmapi.llm.LLM`，差异只在外壳（脚本函数调用 vs HTTP 服务）。
- **tokenizer/detokenizer 由 LLM 托管**：构造时默认按模型自动加载 tokenizer，`generate` 返回已反 token 化的文本；`trtllm-serve` 还额外用 `--tokenizer`/`--custom_tokenizer` 暴露覆盖入口。
- **OpenAI 协议兼容**：`OpenAIServer.register_routes` 注册 `/v1/chat/completions`、`/v1/completions`、`/v1/models` 等标准端点，响应含 `choices[].message.content` 与 `usage`，任何 OpenAI 客户端可直接连。
- 控制台命令 `trtllm-serve` 由 `setup.py` 的 `console_scripts` entry point 注册到 `serve.py:main`。

## 7. 下一步学习建议

本讲把「请求」从外部送到了 `LLM` 门口，但 `LLM.generate()` 内部到底怎么把请求变成 token，仍是黑盒。建议：

- **u2-l2「tensorrt_llm Python 包与公共 API」**：系统认识 `LLM`/`AsyncLLM`/`SamplingParams`/`Mapping` 等公共对象的导出与典型用法，把本讲的导入行扩成完整 API 地图。
- **u3-l1「请求全链路」**：打开 `llm.generate()` 的黑盒，追踪请求一路走到 Scheduler、模型前向、Decoder、Sampling 的完整调用链——本讲的「高空流程图」在那里变成逐段源码。
- **u4-l1「TorchLlmArgs 与配置层级」**：本讲遇到的 `--max_tokens`/`--port`/`--backend` 等 CLI 参数最终都汇入 `TorchLlmArgs`，下一站系统理解这套配置层级。
- 若你对**部署**更感兴趣，可先跳读 `docs/source/quick-start-guide.md` 末尾推荐的部署指南与 `trtllm-serve` CLI 参考，再回到 u3 补内部原理。
