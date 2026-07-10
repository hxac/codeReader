# 环境安装与快速启动

> 本讲是 LightLLM 学习手册第 2 篇，承接 [u1-l1 项目总览](u1-l1-project-overview.md)。
> 上一篇我们知道了 LightLLM 是什么、有哪些技术特色；本篇解决一个更落地的问题：**怎么把它装上，并用一行命令跑起来一个推理服务**。

---

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LightLLM 的运行环境要求（操作系统、Python 版本、GPU 算力）。
2. 用三种方式之一（官方 Docker 镜像 / 源码构建镜像 / 源码安装）把 LightLLM 装好。
3. 理解 `setup.py` 的最小依赖与 `requirements.txt` 的完整锁版依赖的区别。
4. 用 `python -m lightllm.server.api_server --model_dir ...` 一行命令加载模型并启动 HTTP 服务。
5. 用 `curl` 向服务发送一次 `/generate` 请求，拿到模型生成的文本。

---

## 2. 前置知识

在动手之前，先建立几个最基础的概念。如果你已经熟悉，可以跳过本节。

- **LLM 推理服务（Inference Server）**：把一个大语言模型（LLM）封装成一个常驻进程，对外提供 HTTP 接口。客户端发一段文字进去，服务返回模型续写/补全的文字。LightLLM 就是干这件事的框架。
- **HTTP API**：服务启动后会监听一个端口（比如 `8000`），客户端用 HTTP 协议的 `POST` 请求把输入文本发过去。本讲会用 `curl` 这个命令行工具来发请求。
- **GPU 算力（Compute Capability，CC）**：NVIDIA 显卡的"代际编号"，例如 V100 是 7.0、A100 是 8.0、H100 是 9.0。LightLLM 的部分算子要求显卡达到一定代际才能跑。
- **共享内存（Shared Memory，shm）**：操作系统提供的一种进程间高速通信机制。LightLLM 是多进程架构（上一篇讲过），进程之间靠共享内存传递数据，所以对共享内存大小很敏感。
- **conda 环境**：一种把不同项目的 Python 依赖隔离开的工具，避免"装了 A 项目把 B 项目搞坏"。本讲推荐用它建一个干净的环境。

---

## 3. 本讲源码地图

本讲主要读三类文件：

| 文件 | 作用 |
| --- | --- |
| `docs/EN/source/getting_started/installation.rst` | 官方安装指南：环境要求、Docker 安装、源码安装三种方式 |
| `docs/EN/source/getting_started/quickstart.rst` | 官方快速启动：准备模型、启动服务、测试服务三步 |
| `requirements.txt` | 完整的、锁版本的运行时依赖清单（torch、transformers、flashinfer 等） |
| `setup.py` | 打包配置：声明项目名、版本、Python 版本要求、**最小**安装依赖 |
| `lightllm/server/api_cli.py` | 命令行参数定义：本讲用到其中的 `--model_dir`、`--host`、`--port` 默认值 |

> 说明：`setup.py` 与 `api_cli.py` 不在本讲规格的"关键源码"列表里，但它们能帮你把安装和启动讲清楚，所以作为辅助引用。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**安装**、**依赖列表**、**快速启动**、**发送请求验证**。它们对应"从装好到用上"的完整链路。

---

### 4.1 安装方式与环境要求

#### 4.1.1 概念说明

LightLLM 的官方文档开篇就点明了它的技术底色：

> Lightllm is a pure Python-based inference framework with operators written in Triton.
> （LightLLM 是一个基于纯 Python 的推理框架，算子用 Triton 编写。）

这句话很重要，它解释了 LightLLM 为什么"好装"——主体逻辑是纯 Python，性能关键路径才下沉为 Triton 算子，不需要像某些 C++ 框架那样编译一大堆原生代码。

官方提供**三种安装方式**，从易到难：

1. **拉取官方 Docker 镜像**（最简单，推荐初次体验）。
2. **从源码手动构建 Docker 镜像**。
3. **从源码安装到本机 Python 环境**（最灵活，适合二次开发）。

无论哪种方式，都要先满足**运行环境要求**。

#### 4.1.2 核心流程

安装的整体流程：

```text
确认环境（OS / Python / GPU）
        │
        ├── 方式 A：docker pull 官方镜像 → docker run（注意 shm-size）
        │
        ├── 方式 B：docker build 自建镜像 → docker run
        │
        └── 方式 C：conda 建环境 → pip install -r requirements.txt → python setup.py install
```

三种方式的"终点"都是得到一个能运行 `lightllm` 命令的环境。

#### 4.1.3 源码精读

**(1) 环境要求**——文档明确列出了三项硬性要求：

[docs/EN/source/getting_started/installation.rst:11-13](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/getting_started/installation.rst#L11-L13)

这段说明三件事：

- 操作系统：**Linux**（不支持 Windows/macOS 直接运行）。
- Python：文档推荐 **3.10**。
- GPU：**算力 ≥ 7.0**（V100、T4、RTX20xx、A100、L4、H100 等都满足）。

> 关于 Python 版本的一个细节：文档推荐 3.10，但打包脚本 `setup.py` 里写的是 `python_requires=">=3.9.16"`（[setup.py:18](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L18-L18)），也就是说**最低**允许 3.9.16。建议按文档用 3.10，避免踩到旧版本的边角问题。

**(2) Docker 方式**——官方镜像一行拉取：

[docs/EN/source/getting_started/installation.rst:23-40](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/getting_started/installation.rst#L23-L40)

这里有一个**新手极易踩坑**的点：`docker run` 时必须加 `--shm-size 2g`（甚至更大）。文档在注释里反复强调：

- 纯文本服务建议共享内存 **≥ 2GB**，内存充足时建议 **≥ 16GB**；
- 多模态服务建议 **≥ 16GB**；
- 如果共享内存实在不够，可以启动时降低 `--running_max_req_size`（减少并发请求数，从而少用共享内存）。

原因就是前置知识里讲的：LightLLM 多进程之间靠共享内存传数据，内存不够服务会启动失败。

**(3) 源码安装**——官方推荐流程：

[docs/EN/source/getting_started/installation.rst:71-89](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/getting_started/installation.rst#L71-L89)

关键三步：

```bash
# 1. 建 conda 环境（推荐 python=3.10）
conda create -n lightllm python=3.10 -y && conda activate lightllm

# 2. 装依赖（CUDA 12.x 场景，用 cu124 的 torch wheel）
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu124

# 3. 安装 lightllm 本体
python setup.py install
```

> 小提示：文档注释写的是 "cuda 12.8"，而命令里的 `--extra-index-url` 指向 `cu124`（CUDA 12.4 的 wheel 仓库）。实践中这两个版本兼容，按文档命令照抄即可；若你的驱动/显卡很新需要其他 CUDA 版本的 torch，需自行调整该 index。具体能否在你机器上跑通，**待本地验证**。

#### 4.1.4 代码实践

**实践目标**：用源码方式把 LightLLM 装进一个干净的 conda 环境。

**操作步骤**：

1. 确认本机有 NVIDIA 显卡且驱动正常：`nvidia-smi`（能看到显卡型号和驱动版本即可）。
2. 建环境并克隆代码：

   ```bash
   conda create -n lightllm python=3.10 -y
   conda activate lightllm
   git clone https://github.com/ModelTC/lightllm.git
   cd lightllm
   ```

3. 装依赖、装本体：

   ```bash
   pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu124
   python setup.py install
   ```

**需要观察的现象**：`python setup.py install` 最后应打印类似 `Successfully installed lightllm-1.1.0` 的字样（版本号见 [setup.py:6](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L6-L6)）。

**预期结果**：在终端执行 `python -c "import lightllm; print('ok')"` 能正常输出 `ok`，不报 `ModuleNotFoundError`，即安装成功。

> 如果没有 GPU 或不想配环境，可改用 4.1.3 的 Docker 方式：`docker pull ghcr.io/modeltc/lightllm:main`，免去手动装依赖。

#### 4.1.5 小练习与答案

**练习 1**：文档说 GPU 算力要 ≥ 7.0。一台装了 GTX 1050（算力 6.1）的机器能跑 LightLLM 吗？

**答案**：按官方要求不能。6.1 < 7.0，部分 Triton 算子可能无法运行或报错。需要至少 V100/T4（7.0）及以上显卡。

**练习 2**：为什么官方 Docker 示例一定要加 `--shm-size 2g`？

**答案**：LightLLM 是多进程架构，进程间通过共享内存传递数据（如请求对象、token 缓冲）。Docker 默认共享内存只有 64MB，远不够用，会导致服务启动失败或运行异常，所以必须显式调大。

---

### 4.2 依赖列表解读

#### 4.2.1 概念说明

LightLLM 有**两套依赖清单**，理解它们的分工是本模块的关键（这也是上一篇 [u1-l1](u1-l1-project-overview.md) 提到过的点，这里展开）：

- **`setup.py` 的 `install_requires`**：**最小依赖**，只列了让 LightLLM 作为库能被导入所必需的 10 个包。
- **`requirements.txt`**：**完整锁版依赖**，列了真正把推理服务跑起来需要的全部包（含 torch、flashinfer、sglang-kernel 等重依赖），并且**锁定了具体版本号**。

为什么要分两套？`install_requires` 保持精简，是为了让 LightLLM 在被别的项目当依赖引入时不强行拖入一大堆重型包；而真正部署推理服务时，必须用 `requirements.txt` 把完整运行时拉齐，并且版本要锁死以保证可复现。

#### 4.2.2 核心流程

依赖安装流程：

```text
pip install -r requirements.txt
        │
        ├── 解析 100 行锁版依赖
        ├── 通过 --extra-index-url 拉 CUDA 版本的 torch/torchvision
        └── 安装 torch / transformers / flashinfer / sglang-kernel / pyzmq / rpyc / ...
        │
python setup.py install
        │
        └── 把 lightllm 包注册到当前 Python 环境（install_requires 此时被检查）
```

#### 4.2.3 源码精读

**(1) 最小依赖（10 个）**——打包脚本里声明的：

[setup.py:19-30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L19-L30)

这 10 个包（`pyzmq`、`uvloop`、`transformers`、`einops`、`packaging`、`rpyc`、`ninja`、`safetensors`、`triton`、`orjson`）大致对应：进程间通信（pyzmq、rpyc）、事件循环（uvloop）、模型/分词（transformers）、张量操作（einops）、权重加载（safetensors）、算子编译（triton、ninja）、序列化（orjson）。注意这里**没有 torch**——torch 属于重依赖，被放到 `requirements.txt`。

**(2) 完整锁版依赖**——下面挑几个最关键的行：

[requirements.txt:62-62](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L62-L62) —— `torch==2.11.0`，深度学习框架本体，整个推理的基石。

[requirements.txt:64-64](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L64-L64) —— `transformers==5.8.0`，用来加载 HuggingFace 格式的模型权重和 tokenizer。

[requirements.txt:83-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L83-L83) —— `flashinfer-python==0.6.12`，高性能注意力库，LightLLM 的注意力后端之一（后续 [u3-l5 注意力后端](u3-l5-attention-backends.md) 会讲）。

[requirements.txt:49-49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L49-L49) 与 [requirements.txt:52-52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L52-L52) —— `pyzmq==25.1.1b2`、`rpyc==5.3.1`，多进程通信的两大件（上一篇讲过的多进程架构就靠它们）。

整份清单有 100 行，还包含 `sglang-kernel`（复用算子）、`cupy-cuda13x`、`nixl`（PD 分离的 KV 传输，见 [u7-l1](u7-l1-pd-disaggregation-kv-transfer.md)）、`hypercorn`（HTTP 服务）、`prometheus_client`（指标监控）等。初学不必全记，知道"完整运行时都锁在这份文件"即可。

#### 4.2.4 代码实践

**实践目标**：把 100 行依赖按功能归类，建立"每个包大概干嘛"的直觉。

**操作步骤**：

1. 打开 [requirements.txt:1-100](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L1-L100)。
2. 按下表分类，给每个包归位（示例答案见下方）：

   | 类别 | 你认为属于这类的包 |
   | --- | --- |
   | 深度学习核心 | ? |
   | 模型/分词 | ? |
   | 高性能算子/注意力 | ? |
   | 进程间通信 | ? |
   | Web 服务 | ? |

**需要观察的现象**：你会注意到很多包带精确版本号（`==`），少数用范围（如 [requirements.txt:100-100](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L100-L100) 的 `litellm>=1.52.0,<1.85`）。

**预期结果（参考答案）**：

- 深度学习核心：`torch`、`torchvision`
- 模型/分词：`transformers`、`tokenizers`、`sentencepiece`、`tiktoken`、`safetensors`
- 高性能算子/注意力：`flashinfer-python`、`sglang-kernel`、`xformers`、`triton`（在 setup.py）、`cupy-cuda13x`
- 进程间通信：`pyzmq`、`rpyc`、`websockets`
- Web 服务：`fastapi`、`uvicorn`、`hypercorn`、`uvloop`

#### 4.2.5 小练习与答案

**练习 1**：为什么 `torch` 不在 `setup.py` 的 `install_requires` 里，却出现在 `requirements.txt` 里？

**答案**：`install_requires` 追求最小化，避免把 LightLLM 当库引入时强制拖入巨大的 torch；而 `requirements.txt` 面向真正部署推理服务，torch 是必须的运行时，所以要锁版本列入。

**练习 2**：`requirements.txt` 里大多数包用 `==` 锁死版本，这样做的利弊是什么？

**答案**：利是**可复现**——保证每个人装出的环境一致，避免上游包升级导致 API 不兼容；弊是**不够灵活**，想升级某个包要手动改清单，且可能与系统已有的其他版本冲突。

---

### 4.3 快速启动服务

#### 4.3.1 概念说明

装好之后，启动一个推理服务**最少只需要两步**（官方原话）：

1. 准备一份 LightLLM 支持的模型权重文件。
2. 用命令行启动模型服务。

（第三步"测试服务"是可选的，见 4.4。）

LightLLM 直接读 HuggingFace 格式的模型权重，所以"准备模型"就是用 `huggingface-cli` 下载一个模型目录；"启动服务"就是一行 `python -m lightllm.server.api_server`。

#### 4.3.2 核心流程

```text
下载模型（HF 格式目录）
   huggingface-cli download Qwen/Qwen3-8B --local-dir Qwen3-8B
        │
        ▼
启动 HTTP 服务
   python -m lightllm.server.api_server --model_dir ~/models/Qwen3-8B
        │
        ▼
服务监听 127.0.0.1:8000，等待请求
```

#### 4.3.3 源码精读

**(1) 准备模型文件**——官方以 Qwen3-8B 为例：

[docs/EN/source/getting_started/quickstart.rst:15-37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/getting_started/quickstart.rst#L15-L37)

核心命令：

```bash
pip install -U huggingface_hub
huggingface-cli download Qwen/Qwen3-8B --local-dir Qwen3-8B
```

下载完成后，本地 `Qwen3-8B` 目录里会有 `config.json`、`*.safetensors`（权重）、`tokenizer.json` 等文件——LightLLM 启动时读的就是这个目录。

> 你也可以用任何 LightLLM 支持的模型（llama、qwen、deepseek 等数十个族），把 `--model_dir` 指向对应目录即可。

**(2) 启动服务**——一行命令：

[docs/EN/source/getting_started/quickstart.rst:44-49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/getting_started/quickstart.rst#L44-L49)

```bash
python -m lightllm.server.api_server --model_dir ~/models/Qwen3-8B
```

这里的 `python -m lightllm.server.api_server` 表示把 `lightllm/server/api_server.py` 当作模块入口运行（后续 [u1-l4 入口与命令行参数](u1-l4-entry-and-cli-args.md) 会详解）。`--model_dir` 是**唯一必填**的关键参数，指向你的模型目录。

服务默认监听地址和端口定义在命令行参数里：

[lightllm/server/api_cli.py:34-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L34-L35)

即默认 `host=127.0.0.1`、`port=8000`。所以本机访问地址是 `http://127.0.0.1:8000`。

> 注意一个常见混淆：4.1.3 的 Docker 示例里写的是 `-p 8080:8080`，那是把容器端口映射到宿主机的示例；而服务**进程本身**默认监听 `8000`。如果你想照搬 Docker 示例，要么把映射改成 `-p 8000:8000`，要么启动时加 `--port 8080`。本讲后续 curl 统一用默认的 `8000`。

#### 4.3.4 代码实践

**实践目标**：下载一个小模型并启动服务，看到"服务就绪"的日志。

**操作步骤**：

1. 下载模型（若网速慢，可换一个更小的 LightLLM 支持模型）：

   ```bash
   mkdir -p ~/models && cd ~/models
   pip install -U huggingface_hub
   huggingface-cli download Qwen/Qwen3-8B --local-dir Qwen3-8B
   ```

2. 启动服务：

   ```bash
   python -m lightllm.server.api_server --model_dir ~/models/Qwen3-8B
   ```

**需要观察的现象**：终端会依次打印模型权重加载、KV cache 内存分配、各子进程（httpserver/router/model backend/detokenization）就绪等日志。这一连串进程的拉起过程，正是下一篇 [u1-l5 多进程编排](u1-l5-process-orchestration.md) 要拆解的内容。

**预期结果**：日志最后出现类似 `Uvicorn running on http://127.0.0.1:8000` 或服务监听就绪的提示，说明服务已可接收请求。**实际日志文案待本地验证**（不同版本措辞可能不同）。

> 如果显存不够装不下 8B 模型，可在启动命令加 `--max_total_token_num` 限制可缓存 token 数（该参数后续会用到），或换更小的模型。

#### 4.3.5 小练习与答案

**练习 1**：启动命令里 `--model_dir` 指向的目录里，必须至少包含哪类文件，LightLLM 才能加载？

**答案**：至少要有 `config.json`（模型结构配置）和权重文件（如 `*.safetensors`），通常还需要 tokenizer 相关文件（如 `tokenizer.json`/`tokenizer.model`）。LightLLM 靠 `config.json` 判断用哪个模型实现，靠权重文件做推理。

**练习 2**：不传 `--host` 和 `--port` 时，服务监听在哪里？

**答案**：监听在 `127.0.0.1:8000`，因为 [api_cli.py:34-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L34-L35) 里这两个参数的默认值就是 `127.0.0.1` 和 `8000`。若要让局域网访问，需显式传 `--host 0.0.0.0`。

---

### 4.4 发送请求验证服务

#### 4.4.1 概念说明

服务启动后，它就是一个 HTTP 服务。验证它是否可用，最简单的办法就是用 `curl` 发一个 `POST /generate` 请求：把提示文本（`inputs`）和生成参数（`parameters`）以 JSON 发过去，服务返回模型续写的文本。

`/generate` 是 LightLLM 最基础的文本生成端点（OpenAI 兼容的 `/v1/chat/completions` 等端点会在 [u2-l2 HTTP API](u2-l2-http-api-and-dispatch.md) 讲）。

#### 4.4.2 核心流程

```text
客户端 curl POST /generate
   body = { "inputs": "...", "parameters": { "max_new_tokens": 17, ... } }
        │
        ▼
LightLLM 服务（127.0.0.1:8000）接收 → tokenize → 调度推理 → 生成 token
        │
        ▼
返回 JSON：{ "generated_text": [ { "text": "..." } ] }
```

#### 4.4.3 源码精读

官方给出的测试请求（注意端口是 `8000`，与服务默认端口一致）：

[docs/EN/source/getting_started/quickstart.rst:54-64](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/getting_started/quickstart.rst#L54-L64)

```bash
curl http://127.0.0.1:8000/generate \
     -H "Content-Type: application/json" \
     -d '{
           "inputs": "What is AI?",
           "parameters":{
             "max_new_tokens":17,
             "frequency_penalty":1
           }
          }'
```

几个字段含义：

- `inputs`：提示文本，模型会接着它往后生成。
- `parameters.max_new_tokens`：最多生成多少个新 token（这里是 17）。
- `parameters.frequency_penalty`：频率惩罚，>0 时抑制重复词（这里是 1）。采样与惩罚的内部实现见后续 [u3-l6 采样与后处理](u3-l6-sampling-postprocess.md)。

#### 4.4.4 代码实践

**实践目标**：向已启动的服务发一次 `/generate` 请求，拿到模型输出。

**操作步骤**：

1. 确保 4.3 的服务仍在运行（终端不退出）。
2. **新开一个终端**，执行上面的 `curl` 命令。

**需要观察的现象**：等待数秒后，`curl` 会打印一段 JSON 返回。

**预期结果**：返回形如下面的 JSON（`text` 内容取决于模型，**具体文案待本地验证**）：

```json
{"generated_text":[{"text":" What is AI?\n\nArtificial Intelligence (AI) is ..."}]}
```

> 如果返回连接被拒绝（`Connection refused`），先确认服务是否监听在 `8000`、是否已打印就绪日志；若服务在 Docker 内，检查端口映射是否正确。

#### 4.4.5 小练习与答案

**练习 1**：把 `max_new_tokens` 从 17 改成 100，输出会有什么变化？

**答案**：模型会尝试生成更多 token，返回的 `text` 更长。但生成更多 token 也更耗时、占更多 KV cache；若超过服务端限制可能被截断。

**练习 2**：`frequency_penalty` 设为 0 和设为较大的正数，输出风格有何不同？

**答案**：设为 0 时不施加频率惩罚，模型更容易重复用词；设为较大正数会主动压低已出现 token 的概率，输出更"多样"、更少重复。这是一个调节生成风格的常用旋钮。

---

## 5. 综合实践

把本讲四个模块串成一条**端到端**的完整任务，验证你真的把 LightLLM 跑起来了：

**任务**：从零部署一个 LightLLM 推理服务，并向它提一个问题，得到回答。

**步骤**：

1. **安装**（选一种）：
   - Docker：`docker pull ghcr.io/modeltc/lightllm:main`，然后 `docker run -it --gpus all -p 8000:8000 --shm-size 2g -v ~/models:/data ghcr.io/modeltc/lightllm:main /bin/bash`（注意这里把端口映射改成了 `8000:8000` 以匹配默认端口）。
   - 源码：按 4.1.4 用 conda + `requirements.txt` + `setup.py install`。
2. **准备模型**：`huggingface-cli download Qwen/Qwen3-8B --local-dir ~/models/Qwen3-8B`。
3. **启动服务**：`python -m lightllm.server.api_server --model_dir ~/models/Qwen3-8B`，等到日志提示就绪。
4. **发请求**：用 4.4.3 的 `curl` 向 `http://127.0.0.1:8000/generate` 发一次请求，把 `inputs` 改成你自己的问题（如 `"inputs": "用一句话解释什么是KV Cache"`）。
5. **记录**：把返回的 JSON 保存下来，并对照本讲的依赖清单（4.2）回想一下——这条请求背后，是 torch、transformers、pyzmq、rpyc 等一众依赖在协作。

**验收标准**：能稳定拿到一段模型生成的中文/英文文本，且能说清"安装→启动→请求"每一步对应的命令与文件。这就算本讲通关。

---

## 6. 本讲小结

- LightLLM 是**纯 Python + Triton 算子**的推理框架，只在 **Linux**、**Python ≥ 3.9.16（推荐 3.10）**、**GPU 算力 ≥ 7.0** 上运行。
- 安装有三种方式：**官方 Docker 镜像**（最简单）、**源码构建镜像**、**源码安装**（`requirements.txt` + `python setup.py install`）。
- 用 Docker 时务必调大 `--shm-size`（纯文本 ≥ 2GB，多模态 ≥ 16GB），否则多进程共享内存不够会启动失败。
- 依赖分两套：`setup.py` 的 `install_requires` 是 10 个**最小依赖**，`requirements.txt` 是含 torch/flashinfer 等的**完整锁版依赖**。
- 启动服务只需一行：`python -m lightllm.server.api_server --model_dir <模型目录>`，默认监听 `127.0.0.1:8000`。
- 用 `curl POST /generate` 即可验证服务可用，`inputs` 给提示、`parameters` 控制生成长度与惩罚。

---

## 7. 下一步学习建议

本讲你只接触了"怎么启动"这一层表皮。启动时终端打印的那一串子进程（httpserver/router/model/detokenization）是怎么被拉起来的？建议接着学：

- **[u1-l4 服务启动入口与命令行参数](u1-l4-entry-and-cli-args.md)**：深入 `api_server.py` 入口和 `api_cli.py` 的参数体系，搞清 `tp`、`dp`、`max_total_token_num`、`run_mode` 等关键参数。
- **[u1-l5 多进程编排启动流程](u1-l5-process-orchestration.md)**：看 `api_start.py` 如何按 `run_mode` 依次拉起各个子进程，理解你今天看到的那些启动日志背后的编排逻辑。
- 想直接了解一次请求在进程间怎么流转，可跳读 **[u2-l1 多进程架构总览](u2-l1-architecture-overview.md)**，但建议先把 u1 的入口与编排讲完再看，顺序更顺。
