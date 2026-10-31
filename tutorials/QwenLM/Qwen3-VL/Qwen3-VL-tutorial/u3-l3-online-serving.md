# vLLM/SGLang 在线服务部署

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `vllm serve` 独立启动一个 Qwen3-VL 常驻服务，并说清楚每个多模态关键启动参数（`--tensor-parallel-size`、`--mm-encoder-tp-mode`、`--enable-expert-parallel`、`--async-scheduling`、`--media-io-kwargs`）的作用与适用条件。
2. 用 SGLang 启动同款服务，并指出它与 vLLM 在能力上的关键差异（尤其视频采样的按请求控制）。
3. 用 OpenAI SDK 向本地服务发送 `image_url` 与 `video_url` 两类请求，理解服务化后 messages 结构相比本地推理的写法变化。
4. 通过 `extra_body={"mm_processor_kwargs": {...}}` 按请求控制视频帧采样（`fps`、`do_sample_frames`），并能解释它离线路径的 `mm_processor_kwargs` 三件套字段是同一个机制的两种入口。

## 2. 前置知识

### 2.1 从离线推理到在线服务：差在哪里

上一讲（u3-l2）的离线推理是「**脚本进程内**」用法：`from vllm import LLM`，构造引擎、跑完 `llm.generate(...)`、进程退出。它适合批量任务，但有三个天然短板：

- 引擎加载权重要几十秒到几分钟，每次脚本运行都要付一遍这个成本；
- 一次只有一个调用方，没有排队、并发、鉴权这些「服务」概念；
- 调用方必须是 Python，且与引擎同机。

在线服务（online serving）把引擎变成**常驻进程**：命令行 `vllm serve ...` 启动后，引擎在后台一直活着，对外暴露一组 HTTP 接口，任何语言、任何机器的客户端都能通过请求访问它。两讲的对照表：

| 维度 | 离线推理（u3-l2） | 在线服务（本讲） |
| --- | --- | --- |
| 入口 | `from vllm import LLM` → `llm.generate(...)` | 命令行 `vllm serve ...` |
| 生命周期 | 随脚本进程生灭 | 常驻，服务多个客户端 |
| 输入格式 | Python 字典三件套（`prompt`/`multi_modal_data`/`mm_processor_kwargs`） | OpenAI 风格 HTTP 请求体（messages + `image_url`/`video_url`） |
| 视觉素材 | 调用方用 `qwen-vl-utils` 下载解码成张量再传入 | 只传 URL 字符串，**下载解码全部由服务端完成** |
| 帧采样 | 客户端采样完成，传 `do_sample_frames=False` | 服务端采样，默认 `fps=2, do_sample_frames=True`，可按请求覆盖 |
| 取结果 | `output.outputs[0].text` | `response.choices[0].message.content` |

最后一行的差异后面会反复出现，请先记住结论：**vLLM 两种模式的输出都只含新生成文本，不需要 transformers 式的前缀裁剪**。

### 2.2 OpenAI 兼容 API：一套事实标准

「OpenAI 兼容」指服务端实现了与 OpenAI 官方 Chat Completions 一致的 HTTP 协议：同样的路由（`/v1/chat/completions`）、同样的请求字段（`model`、`messages`、`max_tokens`...）、同样的响应结构（`choices[0].message.content`）。这样一来，**任何为 OpenAI API 写的客户端代码，只要改一下 `base_url` 就能访问你的本地服务**。

OpenAI SDK 里有两个本讲会用到的机制：

- `base_url`：服务地址。本地 vLLM 服务是 `http://127.0.0.1:22002/v1`，注意末尾的 `/v1` 不能少——SDK 会在它后面拼 `/chat/completions`。
- `extra_body`：`client.chat.completions.create(...)` 的参数表里没有的字段，可以通过 `extra_body={...}` 原样并入请求体 JSON。vLLM 私有的 `mm_processor_kwargs` 就是靠这个口子传进来的。

### 2.3 服务端接手了你的预处理工作

回顾 u3-l2 的分工：调用方负责用 `qwen-vl-utils` 把 URL/路径解码成张量并按像素预算缩放，vLLM 引擎负责占位符展开与前向解码。服务化之后分工再次改变：

```text
离线:  调用方(qwen-vl-utils 下载/解码/缩放/抽帧) ──张量──► vLLM 引擎
在线:  客户端(只写 URL 字符串) ──HTTP──► vLLM 服务端(下载/解码/缩放/抽帧/展开/解码)
```

这带来两个直接后果：

1. 客户端不再需要 `qwen-vl-utils`（发请求只要 `openai` 这一个包）；
2. 「帧在哪里采样」从客户端挪到了服务端，于是控制采样的参数入口也从 `process_vision_info` 的调用参数变成了**服务启动参数 + 请求体字段**——这正是本讲 4.4 节的主角。

### 2.4 术语速查

- **张量并行（tensor parallel）**：把单层权重切开分到多张卡上并行计算，适合单卡放不下的模型。`--tensor-parallel-size 8` 表示用 8 卡。
- **专家并行（expert parallel）**：MoE 模型（如 235B-**A22B**）的众多「专家」子网络分布到多卡，而不是每卡复制一份。Dense 模型（如 2B）没有专家，用不到。
- **FP8**：8 位浮点量化格式，权重体积和显存约为 BF16 的一半，但需要 H100 级以上显卡与 CUDA 12+（README 启动示例的注释里明确标注了这条要求）。
- **`--mm-encoder-tp-mode data`**：多卡张量并行下，视觉编码器不按张量切分、而按数据切分（每卡完整处理一部分图像），大模型高并行度下更省显存与通信。u3-l2 的 Python 参数 `mm_encoder_tp_mode="data"` 就是它。

## 3. 本讲源码地图

本讲的「源码」主要是仓库中的**部署配置与官方示例**，而非 Python 库代码：

| 文件 | 角色 |
| --- | --- |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | 「Deployment → Online Serving」一节是本讲的权威参照：vLLM/SGLang 启动命令与图像/视频请求示例；「Docker」一节给出官方镜像用法 |
| [docker/Dockerfile-qwen3vl-cu128](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128) | 官方镜像的构建文件：基于 `vllm/vllm-openai:v0.11.0`，说明官方推荐的运行时环境与依赖清单 |
| [docker/docker_web_demo.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh) | 用同一镜像起 web 演示的脚本（u1-l4 已精读，本讲只作对照） |
| [web_demo_mm.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py) | 离线 `LLM(...)` 引擎参数的 Python 写法出处，用于建立「CLI 参数 ↔ Python 参数」的对应关系 |
| [qwen-vl-utils/src/qwen_vl_utils/vision_process.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py) | `do_sample_frames=False` 的离线来源，用于与在线默认值 `True` 对照 |

## 4. 核心概念与源码讲解

### 4.1 vLLM server 启动参数与官方运行时

#### 4.1.1 概念说明

`vllm serve` 是 vLLM 的服务化入口命令：读入模型与一组启动参数，完成权重加载、KV 缓存池分配，然后在指定端口挂载 OpenAI 兼容路由并常驻监听。本模块要解决的问题是：**面对一长串启动参数，哪个管并行、哪个管多模态、哪个是给大 MoE 模型专用的**。

先看仓库给出的官方启动命令（面向 235B-A22B MoE + FP8 + 8 卡 H100/H200 的「满配」场景），再逐个拆参数。

#### 4.1.2 核心流程

`vllm serve` 的启动时序可以理想化地描述为：

```text
vllm serve <模型名或路径> [参数...]
   1. 解析 CLI 参数（与 LLM(...) 的 Python 引擎参数同名：下划线 ↔ 连字符）
   2. 加载权重，按 --tensor-parallel-size 切分到各卡（MoE 另按 --enable-expert-parallel 分专家）
   3. 按 --gpu-memory-utilization 比例划分显存，建立 KV 缓存池
   4. 在 --host:--port 挂载 OpenAI 兼容路由（/v1/chat/completions、/v1/models 等）
   5. 常驻监听；多请求经连续批处理（continuous batching）混跑
```

官方满配命令的参数逐个对照：

| 参数 | 作用 | 适用条件 |
| --- | --- | --- |
| `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | 位置参数，模型名或本地路径；此处是 FP8 量化 checkpoint | FP8 需 H100+ 与 CUDA 12+ |
| `--tensor-parallel-size 8` | 张量并行度 8：单层权重切 8 卡 | 按卡数与模型规模定；2B 单卡填 1 |
| `--mm-encoder-tp-mode data` | 视觉编码器按「数据」而非「张量」切分 | 多卡服务大模型时推荐 |
| `--enable-expert-parallel` | MoE 专家并行：专家分布到各卡 | 仅 MoE 模型（名字带 `-AxxB`）需要 |
| `--async-scheduling` | 异步调度：请求调度与 GPU 计算重叠执行，提升吞吐 | vLLM 的性能开关，可选 |
| `--media-io-kwargs '{"video": {"num_frames": -1}}'` | 媒体 I/O 层参数：视频在**加载解码阶段**不按帧数截断，采样决定权后移到处理器阶段 | 想按请求控制 `fps` 时需要（见 4.4） |
| `--host 0.0.0.0 --port 22002` | 监听地址与端口 | `0.0.0.0` 表示对外 reachable |

把这些参数与上一讲 web 演示里的 Python 写法对照，命名规律一目了然：

```text
Python 引擎参数 (web_demo_mm.py)          vllm serve CLI 参数
─────────────────────────────────────    ─────────────────────────────
tensor_parallel_size=...          ↔      --tensor-parallel-size
gpu_memory_utilization=...        ↔      --gpu-memory-utilization
seed=0                            ↔      --seed
mm_encoder_tp_mode="data" (README离线示例) ↔ --mm-encoder-tp-mode data
enable_expert_parallel=True (README离线示例) ↔ --enable-expert-parallel
```

所以**学过 u3-l2 的引擎参数，就等于学过本讲的服务参数**——只是把下划线换成连字符、从构造函数挪到命令行。

#### 4.1.3 源码精读

[README.md:856-868](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L856-L868) —— 部署总纲与安装清单：明确要求 `vllm>=0.11.0` 才支持 Qwen3-VL（低于该版本不认识 Qwen3-VL 模型结构），并给出 `pip install accelerate`、`pip install qwen-vl-utils==0.0.14`、`uv pip install -U vllm` 三条安装命令。版本下限是本讲的硬约束，也是官方 Docker 镜像选版的依据。

[README.md:875-887](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L875-L887) —— vLLM server 官方启动命令全文：注释两行说明这是 FP8 checkpoint、需要 H100+ 与 CUDA 12+；随后是 `vllm serve Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` 加上表中七个参数。命令上方的 [README.md:873](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L873) 说明该命令面向 H100/H200，其他 GPU 应参考 vLLM 社区指南——**不要把满配命令当成万能模板**，2B 模型只需要其中一小部分参数。

[docker/Dockerfile-qwen3vl-cu128:1-3](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L1-L3) —— 官方镜像的基底：`FROM vllm/vllm-openai:v0.11.0`。这一行信息量很大——基底正是 **vLLM 官方的 OpenAI 服务镜像**，版本号 `v0.11.0` 恰好钉在 README 要求的最低版本上。也就是说，这个容器里 `vllm serve` 开箱即用，官方镜像天然就是在线服务的运行时。第 3 行 `ENTRYPOINT []` 清空了基底镜像的默认入口，容器因此既能跑 `bash`、跑 web 演示（u1-l4 的用法），也能直接执行 `vllm serve`。

[docker/Dockerfile-qwen3vl-cu128:5-14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L5-L14) —— 官方运行时的依赖清单：`git+https://github.com/huggingface/transformers`（直接装 transformers 主分支，保证最新的 Qwen3-VL 支持）、`qwen-vl-utils==0.0.14`（与 README 钉死的版本一致）、`torchcodec==0.7`（u2-l3 讲过的推荐视频解码后端）、`gradio==5.49.1` 与 `transformers-stream-generator==0.0.5`（web 演示依赖）、`accelerate`；最后把 `web_demo_mm.py` 拷进镜像。这份清单等于官方替你回答了「跑 Qwen3-VL 服务需要装什么」。

[web_demo_mm.py:71-88](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L71-L88) —— Python 侧对照组：`VLLM_WORKER_MULTIPROC_METHOD='spawn'`、未指定时 `tensor_parallel_size` 默认取 `torch.cuda.device_count()`、`LLM(model=..., trust_remote_code=True, gpu_memory_utilization=..., enforce_eager=False, tensor_parallel_size=..., seed=0)`。逐项都能在 CLI 参数表里找到同名对应物（下划线 ↔ 连字符），印证 4.1.2 的映射规律。

[README.md:1167-1173](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1167-L1173) —— Docker 一节的用法：`docker run --gpus all --ipc=host --network=host --rm -it qwenllm/qwenvl:qwen3vl-cu128 bash` 进入容器后，环境即已就绪（vLLM 0.11.0 + 主分支 transformers + qwen-vl-utils 0.0.14 + torchcodec），可以直接在容器里执行 `vllm serve`。对照 u1-l4 精读过的 [docker/docker_web_demo.sh:56-60](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh#L56-L60)：同一镜像、不同入口命令，就是 web 演示与在线服务两种形态的全部差别。

#### 4.1.4 代码实践

1. **实践目标**：建立「CLI 参数 ↔ Python 引擎参数」的映射能力，让你能看懂任意一篇 vLLM 部署文章并改写出适配自己机器的启动命令。
2. **操作步骤**：
   - 阅读 [README.md:875-887](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L875-L887) 的满配命令与 [web_demo_mm.py:71-88](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L71-L88) 的 Python 构造；
   - 在笔记里抄下满配命令，给每个参数标注三列：**含义 / 是否多模态相关 / 2B 模型单卡是否需要**；
   - 最后写出你自己的 2B 单卡启动命令（答案应形如：`vllm serve Qwen/Qwen3-VL-2B-Instruct --tensor-parallel-size 1 --host 0.0.0.0 --port 22002`，可按需追加 4.4 节的 `--media-io-kwargs`）。
3. **需要观察的现象**：满配命令的 7 个参数里，与「并行」相关的有 3 个（tensor-parallel-size、mm-encoder-tp-mode、enable-expert-parallel），其中 2 个是 MoE/大模型专属；2B 单卡真正必需的只有模型名。
4. **预期结果**：得到一张参数对照表和一条精简启动命令。本实践为纯阅读任务，无需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：README 的满配命令为什么要加 `--enable-expert-parallel`，而 2B 模型的启动命令不需要？

**答案**：`Qwen3-VL-235B-A22B-Instruct-FP8` 是 MoE 模型（A22B 表示每 token 激活 22B 参数，总参数 235B），`--enable-expert-parallel` 把它的众多专家子网络分布到多卡，避免每卡复制全部专家。2B 是 Dense 模型，没有专家结构，该参数无从谈起。

**练习 2**：web 演示中 Python 写的 `gpu_memory_utilization=0.70`，换成 `vllm serve` 应该怎么写？

**答案**：`--gpu-memory-utilization 0.70`。vLLM 的 CLI 参数与 `LLM(...)` 构造参数同名，仅把下划线换成连字符（参照 [web_demo_mm.py:77-84](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L77-L84) 与 [README.md:879-886](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L879-L886) 的对照）。

**练习 3**：不看 vLLM 文档，你如何确认官方 Docker 镜像里能不能直接跑 `vllm serve`？

**答案**：看 [docker/Dockerfile-qwen3vl-cu128:2](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L2)：基底是 `vllm/vllm-openai:v0.11.0`，即 vLLM 官方的 OpenAI 服务镜像，版本满足 README 的 `vllm>=0.11.0` 要求；第 3 行 `ENTRYPOINT []` 又清空了默认入口，容器内可以执行任意命令，因此 `vllm serve` 开箱可用。

### 4.2 SGLang server：另一种引擎，同一套 API

#### 4.2.1 概念说明

SGLang 是与 vLLM 同类的高性能推理引擎（同样主打高吞吐服务化），也提供 OpenAI 兼容接口。仓库在部署一节把两者并列给出：**「起一个 vLLM 或 SGLang 服务，然后用 OpenAI 风格 API 访问」**。本模块要解决的问题是：SGLang 的启动方式与 vLLM 有何异同、Qwen3-VL 官方示例里它缺了什么能力。

对初学者而言最重要的认知是：**客户端代码与引擎解耦**。只要服务是 OpenAI 兼容的，4.3 节的请求脚本不需要改动一行就能同时访问 vLLM 和 SGLang 服务——换引擎只是换启动命令和端口。

#### 4.2.2 核心流程

SGLang 服务启动的三步：

```text
1. python -m sglang.launch_server     # 以模块方式拉起服务进程
2. --model-path <模型名或路径>          # 对应 vllm serve 的位置参数
3. --host/--port/--tp                 # 监听地址与张量并行度（--tp ≈ --tensor-parallel-size）
```

启动后同样挂载 OpenAI 兼容路由，客户端无感知差别。

值得注意的对照点：README 给 SGLang 的启动命令只有 5 个参数，没有 `--mm-encoder-tp-mode`、`--media-io-kwargs` 这些多模态专用旋钮；并且 README 在视频请求示例的注释里明确写了「按请求控制视频采样**目前仅 vLLM 支持**」（见 4.4.3）。所以在「每个请求动态调 `fps`」这个场景下，当前 README 的推荐路径是 vLLM。

#### 4.2.3 源码精读

[README.md:870-871](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L870-L871) —— Online Serving 一节的总起句：可以起 vLLM **或** SGLang 服务，然后用 OpenAI 风格 API 访问。这是仓库层面对两种引擎并列支持的官方表态。

[README.md:888-895](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L888-L895) —— SGLang 启动命令全文：`python -m sglang.launch_server --model-path Qwen/Qwen3-VL-235B-A22B-Instruct --host 0.0.0.0 --port 22002 --tp 4`。与 vLLM 命令的对应关系：`--model-path` ≈ 位置参数模型名，`--tp 4` ≈ `--tensor-parallel-size 4`，`--host/--port` 完全同名。注意它服务的是 BF16 原始 checkpoint（模型名不带 `-FP8` 后缀）。

[README.md:1082-1132](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1082-L1132) —— SGLang 的**离线**示例（作为能力参照）：`Engine(model_path=..., enable_multimodal=True, mem_fraction_static=0.8, tp_size=4, attention_backend="fa3", context_length=10240, disable_cuda_graph=True)`，最后 `llm.generate(prompt=text, image_data=image_inputs, sampling_params=...)`。两点值得记：其一，`tp_size` 就是 CLI `--tp` 的 Python 形态，再次印证「同名参数、双形态」的规律跨引擎成立；其二，输入准备仍是 `apply_chat_template(tokenize=False)` + `process_vision_info`（[README.md:1109-1115](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1109-L1115)）——**u3-l2 的三件套模式与引擎无关**，客户端准备好 prompt 与张量，哪个引擎都认。

[README.md:967](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L967) —— 视频采样按请求控制的适用范围声明：「This feature is currently supported only in vLLM」。这是官方对两引擎能力差异的最直接表述，选型时以此为据。

#### 4.2.4 代码实践

1. **实践目标**：通过「改一个词换引擎」体会 OpenAI 兼容 API 的解耦价值。
2. **操作步骤**（示例代码，无需 GPU）：假设 4.3 节的请求脚本保存为 `client.py`，服务地址通过环境变量传入：

```python
# client.py（示例代码）
import os
from openai import OpenAI

client = OpenAI(api_key="EMPTY",
                base_url=os.environ.get("QWEN_BASE_URL", "http://127.0.0.1:22002/v1"),
                timeout=3600)
# 后续请求代码与引擎无关……
```

   分别用 vLLM 与 SGLang 启动服务（端口错开），再以 `QWEN_BASE_URL=http://127.0.0.1:22002/v1 python client.py` 与 `QWEN_BASE_URL=http://127.0.0.1:22003/v1 python client.py` 各跑一次。
3. **需要观察的现象**：同一份客户端代码不改动任何请求逻辑，两个服务都能返回结果；差异只体现在服务端启动日志与吞吐上。
4. **预期结果**：两个引擎均可完成同一请求（视频按请求调 `fps` 的能力除外，该项以 README 声明为准仅 vLLM 支持）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：SGLang 启动命令里的 `--tp 4` 对应 vLLM 的哪个参数？

**答案**：`--tensor-parallel-size 4`。两者都控制张量并行度，只是参数名缩写不同；SGLang 离线示例中的 `tp_size=4`（README 第 1121 行）是同一个概念的 Python 形态。

**练习 2**：如果你的业务需要「同一条视频，先按 fps=2 快看一遍、再按 fps=8 细看一遍」，按 README 的说法应该选哪个引擎？

**答案**：vLLM。README 在视频请求示例注释中明确说明按请求配置帧采样「currently supported only in vLLM」（[README.md:965-967](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L965-L967)）。用 SGLang 时则需要在服务端启动参数或客户端预处理层面解决，不能按请求覆盖。

**练习 3**：SGLang 离线示例的输入准备与 vLLM 离线示例有何共同点？这说明什么？

**答案**：两者都先 `processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)` 渲染文本，再用 `qwen_vl_utils.process_vision_info` 解码视觉素材（vLLM 版见 [README.md:998-1005](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L998-L1005)，SGLang 版见 [README.md:1109-1115](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1109-L1115)）。这说明 u3-l2 的三件套模式是「客户端准备输入」的通用范式，与具体引擎无关——引擎只负责接收 prompt 与现成视觉数据。

### 4.3 OpenAI 兼容请求：image_url 与 video_url

#### 4.3.1 概念说明

服务起来之后，客户端的一切交互都通过 `client.chat.completions.create(...)` 完成。本模块解决两个问题：

1. **写法变化**：服务化 messages 与本地推理 messages 长得像但不一样——视觉元素的类型名从 `image`/`video` 变成 `image_url`/`video_url`，内容从「直接给路径/URL 字符串」变成「嵌套在 `{"url": ...}` 里」。
2. **客户端配置**：`api_key` 为什么填 `"EMPTY"`、`base_url` 的 `/v1` 为什么不能省、`model` 字段为什么要与服务启动时的模型名一致。

两种写法的逐字段对照：

| 本地推理（u1-l3 / u3-l2） | 在线服务（本讲） |
| --- | --- |
| `{"type": "image", "image": "<路径或URL>"}` | `{"type": "image_url", "image_url": {"url": "<URL>"}}` |
| `{"type": "video", "video": "<路径或URL>"}` | `{"type": "video_url", "video_url": {"url": "<URL>"}}` |
| `{"type": "text", "text": "..."}` | 完全相同 |
| 视觉素材由客户端解码成张量 | URL 原样发出，服务端下载解码 |

#### 4.3.2 核心流程

一次图像请求的完整时序：

```text
客户端 (openai SDK)                         vLLM 服务端 (常驻进程)
────────────────────                        ─────────────────────
client = OpenAI(api_key="EMPTY",
                base_url=".../v1")
messages = [{role:"user", content:[
    {type:"image_url",
     image_url:{url:"https://..."}},
    {type:"text", text:"..."}]}]
        │  POST /v1/chat/completions
        ├───────────────────────────────►  解析请求体
                                            下载 URL → 解码图像（I/O 层，
                                              受 --media-io-kwargs 影响）
                                            渲染 chat template → <|image_pad|>
                                            processor: 缩放 + 占位符展开
                                              （受 mm_processor_kwargs 影响）
                                            引擎: PagedAttention 解码生成
        ◄───────────────────────────────┤  返回 JSON
response.choices[0]
        .message.content                   （response.usage 里含 token 统计）
```

客户端配置的四个要点：

- `api_key="EMPTY"`：本地 vLLM 服务默认不校验鉴权（除非启动时显式配置 API key），SDK 端该字段必填，于是用占位字符串 `"EMPTY"`；
- `base_url` 必须带 `/v1` 后缀：SDK 会自动在其后拼接 `/chat/completions`；
- `model` 必须与服务启动时的模型名逐字一致：`vllm serve Qwen/Qwen3-VL-2B-Instruct` 对应 `model="Qwen/Qwen3-VL-2B-Instruct"`；README 示例里是 `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8`，与其启动命令的位置参数完全一致；
- `timeout=3600`：长视频的下载与解码可能很慢，客户端超时要给足。

#### 4.3.3 源码精读

[README.md:896-933](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L896-L933) —— 官方图像请求示例全文。四个组成部分：构造 `OpenAI(api_key="EMPTY", base_url="http://127.0.0.1:22002/v1", timeout=3600)`（第 901-905 行）；组装 messages——`content` 列表里先 `{"type": "image_url", "image_url": {"url": "https://...receipt.png"}}` 再 `{"type": "text", "text": "Read all the text in the image."}}`（第 907-923 行）；调用 `client.chat.completions.create(model=..., messages=..., max_tokens=2048)`（第 926-930 行）；从 `response.choices[0].message.content` 取生成文本并打印耗时（第 931-932 行）。注意 `model` 字段正是启动命令里的 `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8`。

[README.md:934-963](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L934-L963) —— 官方视频请求示例的请求构造部分：与图像请求唯一的结构差别是元素类型换成 `{"type": "video_url", "video_url": {"url": "https://...space_woaudio.mp4"}}`（第 949-954 行），提问是「How long is this video?」——这类时长问题恰好依赖 Qwen3-VL 的文本-时间戳对齐能力（u2-l4），服务端解码采样后由 metadata 携带时间信息，模型才能算出秒数。

[README.md:800-820](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L800-L820) —— 商用 API 对照组：README「Try Qwen3-VL-235B-A22 with API」一节用**同一个 OpenAI SDK、同样的 `image_url` 写法**访问阿里云 DashScope 服务，只有 `base_url`（`https://dashscope.aliyuncs.com/compatible-mode/v1`）、`api_key`（真实密钥）和 `model` 名（`qwen3-vl-235b-a22b-instruct`）三处不同。这就是 OpenAI 兼容协议的价值：**本地服务与商用 API 的客户端代码只差三行配置**，业务代码可以从本地原型无缝迁到云端。

#### 4.3.4 代码实践

1. **实践目标**：验证「同一份客户端代码、只改 base_url」即可切换本地服务与商用 API。
2. **操作步骤**（示例代码；需要先完成第 5 节综合实践的服务启动，或任一 OpenAI 兼容端点）：

```python
# one_client.py（示例代码）
from openai import OpenAI

def ask_vl(base_url, api_key, model, image_url, question):
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=3600)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": question},
        ]}],
        max_tokens=512,
    )
    return resp.choices[0].message.content

# 本地 vLLM 服务
print(ask_vl("http://127.0.0.1:22002/v1", "EMPTY",
             "Qwen/Qwen3-VL-2B-Instruct",
             "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg",
             "Describe this image."))

# 商用 DashScope API（需自备密钥）
# print(ask_vl("https://dashscope.aliyuncs.com/compatible-mode/v1", DASHSCOPE_API_KEY,
#              "qwen3-vl-235b-a22b-instruct", "...", "Describe this image."))
```

3. **需要观察的现象**：两次调用走同一函数，仅三处配置不同；本地服务的回答来自 2B 模型，云端回答来自 235B 模型，质量差异明显但调用方式零差异。
4. **预期结果**：本地调用返回对 demo.jpeg（一个女人和一只狗坐在沙滩上）的描述。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `api_key` 填字符串 `"EMPTY"` 也能通过？

**答案**：本地 vLLM 服务默认不开启鉴权，不会校验请求头里的密钥；但 OpenAI SDK 要求 `api_key` 必填，否则构造客户端就报错。`"EMPTY"` 只是一个满足 SDK 语法的占位符（官方示例 [README.md:901-905](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L901-L905) 即如此填写）。

**练习 2**：把 `base_url` 写成 `http://127.0.0.1:22002`（漏掉 `/v1`）会发生什么？

**答案**：SDK 会在 `base_url` 后拼接 `/chat/completions`，实际请求打到 `/chat/completions`；而 vLLM 的 OpenAI 兼容路由挂在 `/v1/chat/completions` 下，请求将命中不存在的路径而失败（典型表现为 404）。所以示例里 `base_url` 必须以 `/v1` 结尾。

**练习 3**：把本地推理的写法 `{"type": "image", "image": "https://..."}` 原样发给服务端，可行吗？

**答案**：不可行。OpenAI 协议的 content 元素类型是 `text`/`image_url`/`video_url` 等，`image` 不是合法类型，服务端无法从中取出 URL（具体报错行为以 vLLM 实现为准）。正确写法是 `{"type": "image_url", "image_url": {"url": "https://..."}}`，见 [README.md:911-916](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L911-L916)。记住口诀：**本地 `image`，服务 `image_url`**。

### 4.4 视频采样控制：--media-io-kwargs 与 extra_body

#### 4.4.1 概念说明

这是本讲的核心模块，也是与 u3-l2 呼应最紧的一处。离线路径中，`qwen-vl-utils` 在客户端完成帧采样，并通过 `video_kwargs` 告诉引擎 `do_sample_frames=False`（「帧我采好了，别再动」）。在线路径恰好**翻转**：客户端只发 URL，**服务端**下载解码并采样，默认 `fps=2`、`do_sample_frames=True`。

由此得到一条贯穿离线与在线的规则：

> **谁采样，谁决定 `do_sample_frames`：采样方是自己就传/默认 `True`（允许采样），采样方是对方就传 `False`（禁止二次采样）。**

按请求覆盖采样率的入口是 `extra_body={"mm_processor_kwargs": {...}}`。注意这个字段名与 u3-l2 离线三件套里的 `mm_processor_kwargs` **同名同义**——都是「传给引擎内部 processor 的关键字参数」，只是载体从 Python 字典换成了 HTTP 请求体。

#### 4.4.2 核心流程

在线视频请求的两级控制：

```text
服务启动级（全局默认）
  --media-io-kwargs '{"video": {"num_frames": -1}}'
      └─ I/O 层：视频解码阶段不按帧数截断 → 采样决定权后移到 processor

请求级（每条请求可不同）
  extra_body={"mm_processor_kwargs": {"fps": 4, "do_sample_frames": True}}
      └─ processor 层：按 fps 抽帧（默认 fps=2）
```

帧数与视觉 token 的估算承接 u2-l3 的采样公式。对时长为 \(T\) 秒、请求采样率为 \(f\) 的视频，帧数先按 \(T \times f\) 计算理想值，再夹入 \([4, 768]\) 区间并向下取偶：

\[
N_{\text{frames}} = \operatorname{floorToEven}\Big(\min\big(\max(T \cdot f,\ 4),\ 768\big)\Big)
\]

设缩放后单帧尺寸为 \(H' \times W'\)，视频的视觉 token 数为（32× 空间压缩叠加 2× 时间压缩）：

\[
N_{\text{tokens}} \approx \frac{H'}{32} \times \frac{W'}{32} \times \frac{N_{\text{frames}}}{2}
\]

于是 `fps` 从 2 提到 4 的预期效果链条是：**帧数约翻倍 → 视觉 token 约翻倍 → prompt token 数上涨 → 延迟与显存占用上升、细节 richer**。服务端的采样遵循与 `qwen-vl-utils` 一致的约定（`mm_processor_kwargs` 最终交给模型自带的 processor），具体实现细节以 vLLM 文档为准。

#### 4.4.3 源码精读

[README.md:965-970](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L965-L970) —— 本模块最关键的四行官方注释：① 当以 `--media-io-kwargs '{"video": {"num_frames": -1}}'` 启动时，视频帧采样可以通过 `extra_body`（例如设置 `fps`）配置；② 该能力**目前仅 vLLM 支持**；③ 服务端默认 `fps=2`、`do_sample_frames=True`；④ 在 `do_sample_frames=True` 前提下可自定义 `fps`。这段注释同时把「启动参数（I/O 层）」与「请求参数（processor 层）」两层控制的关系讲清了：I/O 层不做帧数截断（`num_frames: -1`），采样决定权才落在 processor 手里，按请求的 `fps` 因此生效。对 `num_frames: -1` 内部语义的更细解释（如是否等于「解码全部帧」）属 vLLM 实现细节，本仓库未展开，以官方文档为准。

[README.md:971-976](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L971-L976) —— 按请求控制采样的调用现场：`client.chat.completions.create(model=..., messages=..., max_tokens=2048, extra_body={"mm_processor_kwargs": {"fps": 2, "do_sample_frames": True}})`。`create()` 的标准参数表里没有 `mm_processor_kwargs`，所以必须借助 `extra_body` 把它并入请求体 JSON；服务端读到后转发给内部的 processor。

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:528-530](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L528-L530) —— 离线路径的对照组：`video_kwargs` 恒以 `{'do_sample_frames': False}` 起步（utils 已在客户端采样完），仅在 Qwen2.5-VL 兼容分支追加 `fps` 列表。把它与在线默认值 `{'fps': 2, 'do_sample_frames': True}` 并排看，「谁采样，谁决定 do_sample_frames」的规则一目了然：离线是 utils 采样 → 传 `False` 防引擎重采；在线是服务端采样 → 默认 `True` 且 `fps` 可调。

[README.md:884](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L884) —— 启动级参数的落点：满配命令里的 `--media-io-kwargs '{"video": {"num_frames": -1}}'`。它与请求级的 `extra_body` 是同一机制的两级开关：一个管服务全局的 I/O 行为，一个管单条请求的 processor 行为。要按请求调 `fps`，启动时就要带上这一项（依据即上面第 965 行注释）。

#### 4.4.4 代码实践

1. **实践目标**：用同一视频、不同 `fps` 发请求，观察「帧数 → 视觉 token → prompt token 数」的传导链条。
2. **操作步骤**（示例代码，需先启动带 `--media-io-kwargs` 的服务，见第 5 节）：

```python
# fps_probe.py（示例代码）
import time
from openai import OpenAI

client = OpenAI(api_key="EMPTY", base_url="http://127.0.0.1:22002/v1", timeout=3600)

messages = [{"role": "user", "content": [
    {"type": "video_url",
     "video_url": {"url": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen2-VL/space_woaudio.mp4"}},
    {"type": "text", "text": "How long is this video?"},
]}]

for fps in (2, 4):
    t0 = time.time()
    resp = client.chat.completions.create(
        model="Qwen/Qwen3-VL-2B-Instruct",
        messages=messages,
        max_tokens=512,
        extra_body={"mm_processor_kwargs": {"fps": fps, "do_sample_frames": True}},
    )
    usage = resp.usage  # vLLM 默认在响应中携带 token 统计
    print(f"fps={fps}: prompt_tokens={usage.prompt_tokens}, "
          f"耗时={time.time()-t0:.1f}s, 回答={resp.choices[0].message.content[:50]}...")
```

3. **需要观察的现象**：`fps=4` 的 `prompt_tokens` 明显大于 `fps=2`（视频 token 约翻倍，文本部分不变）；耗时随帧数上升；模型对时长的回答应基本一致（时长由 metadata 的「总帧数 ÷ fps」决定，与采样率无关，参见 u2-l4）。
4. **预期结果**：两组请求均成功返回，`prompt_tokens(fps=4) > prompt_tokens(fps=2)`，差值与「约翻倍的视频 token」量级相符。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：离线路径为什么要传 `do_sample_frames=False`，而在线服务默认是 `True`？

**答案**：离线路径的采样发生在客户端的 `qwen-vl-utils`（`process_vision_info` 内部抽帧），帧已采好，必须传 `False` 阻止引擎内的 processor 二次抽帧（否则帧数错乱、时间轴损坏）；在线路径客户端只发 URL，采样由服务端 processor 执行，因此默认 `True` 且采样率 `fps` 可配。规则口诀：谁采样，谁把开关拨到正确的一侧。

**练习 2**：某视频时长 30 秒，分别以 `fps=2` 与 `fps=4` 请求，帧数各是多少（套用 u2-l3 的采样公式）？

**答案**：`fps=2` 时理想帧数 \(30 \times 2 = 60\)，在 \([4,768]\) 内且为偶数，得 60 帧；`fps=4` 时 \(30 \times 4 = 120\)，同理得 120 帧。帧数翻倍，视频视觉 token 数也随之约翻倍（\(\frac{H'}{32}\cdot\frac{W'}{32}\cdot\frac{N_{\text{frames}}}{2}\)）。

**练习 3**：为什么按请求调 `fps` 之前，启动命令里要先加 `--media-io-kwargs '{"video": {"num_frames": -1}}'`？

**答案**：依据 [README.md:965-966](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L965-L966) 的官方注释：该启动参数作用于 I/O 层，使视频在加载解码阶段不按帧数截断，把「抽多少帧」的决定权后移到 processor 阶段；这样每条请求经 `extra_body.mm_processor_kwargs` 传入的 `fps` 才有实际作用对象。若 I/O 层已经截断了帧数，按请求的采样率便无从谈起。

## 5. 综合实践：起一个 Qwen3-VL-2B 在线服务并完成图像、视频与 fps 对比请求

把本讲四个模块串成一个端到端流程：启动服务（4.1）→ 图像请求（4.3）→ 视频请求与按请求采样控制（4.4）。全程使用 2B 小模型，单张消费级 GPU 即可完成（FP8 与多卡参数全部不需要）。

**实践目标**：独立交付一个可复现的「一条启动命令 + 一个客户端脚本 + 一份观察记录」最小服务化方案。

**操作步骤**：

1. 安装依赖（版本要求见 [README.md:862-868](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L862-L868)）：

```bash
pip install "vllm>=0.11.0" "transformers>=4.57.0" qwen-vl-utils==0.0.14 accelerate openai
```

2. 启动服务（新终端，保持前台运行以观察日志）：

```bash
vllm serve Qwen/Qwen3-VL-2B-Instruct \
  --tensor-parallel-size 1 \
  --media-io-kwargs '{"video": {"num_frames": -1}}' \
  --host 0.0.0.0 \
  --port 22002
```

   等待日志出现服务就绪信息。可用 `curl http://127.0.0.1:22002/v1/models` 确认路由已挂载、模型名正确（该端点是 vLLM OpenAI 服务的标准探测方式）。

3. 编写客户端脚本 `serving_probe.py`（示例代码，综合 [README.md:896-933](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L896-L933) 与 [README.md:934-980](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L934-L980) 两个官方示例改写）：

```python
# -*- coding: utf-8 -*-
# serving_probe.py（示例代码）
import time
from openai import OpenAI

client = OpenAI(api_key="EMPTY", base_url="http://127.0.0.1:22002/v1", timeout=3600)
MODEL = "Qwen/Qwen3-VL-2B-Instruct"   # 必须与 vllm serve 的模型名逐字一致

def create(content, **kw):
    t0 = time.time()
    resp = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": content}], **kw)
    print(f"[{time.time()-t0:6.1f}s | prompt_tokens={resp.usage.prompt_tokens}]")
    print(resp.choices[0].message.content, "\n" + "=" * 60)

# ① 图像请求（README "Image Request Example" 同款结构）
create([
    {"type": "image_url",
     "image_url": {"url": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg"}},
    {"type": "text", "text": "Describe this image."},
], max_tokens=512)

# ② 视频请求·默认采样（不传 extra_body，服务端默认 fps=2、do_sample_frames=True）
VIDEO = [{"type": "video_url",
          "video_url": {"url": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen2-VL/space_woaudio.mp4"}}]
create(VIDEO + [{"type": "text", "text": "How long is this video?"}], max_tokens=512)

# ③ 视频请求·fps=4 按请求覆盖（需启动时带 --media-io-kwargs，仅 vLLM 支持）
create(VIDEO + [{"type": "text", "text": "How long is this video?"}],
       max_tokens=512,
       extra_body={"mm_processor_kwargs": {"fps": 4, "do_sample_frames": True}})
```

4. 运行：`python serving_probe.py`，并在笔记中填写观察记录表：

| 请求 | prompt_tokens | 耗时 | 回答要点 |
| --- | --- | --- | --- |
| ① 图像 |  |  | （应描述出女人与狗的海滩场景） |
| ② 视频 fps=2 |  |  | （应给出视频时长） |
| ③ 视频 fps=4 |  |  | （时长应与②一致，细节更丰富） |

**需要观察的现象**：

1. 三个请求全部经同一个常驻服务完成，进程不重启、权重只加载一次——对比 u3-l2 每次脚本运行都要重建引擎；
2. ② 与 ③ 的 `prompt_tokens` 差值应接近「视频视觉 token 约翻倍」的量级（文本部分完全相同），直接印证 4.4 的公式链条；
3. 服务端日志里能看到视频下载与解码发生在服务侧，客户端进程没有任何媒体处理；
4. ② 与 ③ 对时长的回答应一致——时长来自 metadata 的总帧数 ÷ 原始 fps，与采样率无关（u2-l4 的结论在服务链路上的体现）。

**预期结果**：三段符合各输入内容的回答，且 ③ 的 `prompt_tokens` 明显大于 ②。若手头没有 GPU 环境，可用官方 Docker 镜像替代步骤 1-2（`docker run --gpus all --ipc=host --network=host --rm -it qwenllm/qwenvl:qwen3vl-cu128 bash` 后在容器内执行同一条 `vllm serve`，见 [README.md:1167-1173](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1167-L1173)；镜像内 vLLM/transformers/qwen-vl-utils 均已装好，见 [docker/Dockerfile-qwen3vl-cu128:5-11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L5-L11)）。**本实践未在编写本讲义时实际运行，待本地验证。**

## 6. 本讲小结

- `vllm serve` 把 u3-l2 的离线引擎变成常驻 OpenAI 兼容服务；CLI 参数与 `LLM(...)` 的 Python 参数同名同义（下划线 ↔ 连字符），学过离线参数即掌握服务参数。
- 多模态关键启动参数四件套：`--tensor-parallel-size`（张量并行）、`--mm-encoder-tp-mode data`（视觉编码器按数据切分）、`--enable-expert-parallel`（MoE 专属）、`--media-io-kwargs`（视频 I/O 层，按请求调 fps 的前提）。
- SGLang 以 `python -m sglang.launch_server --model-path ... --tp N` 提供同类 OpenAI 兼容服务，但按 README 声明，视频帧采样的按请求控制「目前仅 vLLM 支持」。
- 请求侧两大变化：视觉元素类型换成 `image_url`/`video_url` + 嵌套 `{"url": ...}`；URL 的下载、解码、缩放、抽帧全部移交服务端，客户端不再需要 `qwen-vl-utils`。
- `extra_body={"mm_processor_kwargs": {...}}` 与离线三件套中的 `mm_processor_kwargs` 同名同义；贯穿规则是「谁采样，谁决定 `do_sample_frames`」——离线客户端采样传 `False`，在线服务端采样默认 `True`（`fps=2` 可覆盖）。
- 官方 Docker 镜像基于 `vllm/vllm-openai:v0.11.0` 构建，`vllm serve` 开箱即用，是免配置的官方运行时。

## 7. 下一步学习建议

- **下一讲 u3-l4（长上下文 YaRN 与 FlashAttention 加速）**：本讲的启动参数还有一族没讲——`vllm serve --rope-scaling '{"rope_type":"yarn",...}' --max-model-len 1000000`（见 [README.md:789-793](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L789-L793)），它把原生 256K 上下文扩展到 1M，是长视频/长文档场景的进阶部署姿势。
- **单元六（评测体系）**：五个评测脚本全部基于 u3-l2 的**离线**引擎（`LLM` + `prepare_inputs_for_vllm`）。学完本讲可以回答一个架构问题：为什么批量评测偏爱离线引擎（免 HTTP 往返、免媒体序列化、一次加载跑完全集），而产品服务才用在线模式。
- **延伸阅读**：README 部署一节引用的 [vLLM 多模态输入官方文档](https://docs.vllm.ai/en/latest/serving/multimodal_inputs.html)与 [vLLM Qwen3-VL 部署指南](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3-VL.html)（[README.md:860](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L860)、[README.md:873](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L873)），覆盖更多硬件的参数调优与 `--media-io-kwargs` 的完整字段说明。
