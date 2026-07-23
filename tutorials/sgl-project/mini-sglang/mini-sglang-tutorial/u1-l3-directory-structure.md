# 目录结构与模块地图

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `python/minisgl` 下一共有哪些子包目录，每个目录大致负责什么。
- 把 `docs/structures.md` 里给出的「模块职责说明」一一对应到磁盘上的真实文件。
- 解释 `python -m minisgl` 这条命令是靠哪几个文件「串」起来启动的（入口与导出机制）。
- 自己绘制一张「子包 → 职责 → 关键文件」对照表，作为后续阅读源码的导航图。

本讲不深入任何单个模块的实现细节，它的唯一任务是帮你建立一张**整体地图**。有了地图，后面几讲进入 Scheduler、Engine、KV Cache 时，你随时知道「现在在地图的哪一块」。

## 2. 前置知识

在进入目录之前，先用大白话回顾几个本讲要用到的概念。如果你已经学过 [u1-l1 项目总览](u1-l1-project-overview.md) 和 [u1-l2 安装与快速运行](u1-l2-install-and-run.md)，这部分会很快。

- **Python 包（package）**：一个含有 `__init__.py` 的文件夹。`import minisgl.server` 时，Python 会去找 `minisgl/server/__init__.py`。
- **`__main__.py`**：当你执行 `python -m minisgl` 时，Python 会运行这个包里的 `__main__.py`。它是「用模块名当命令」时的入口。
- **`__init__.py` 的「再导出」**：`__init__.py` 里常常写一行 `from .launch import launch_server`，意思是「把子模块里的东西，提到包的门口」。这样外面就能写 `from minisgl.server import launch_server`，而不必关心它住在 `launch.py` 里。
- **TP rank / 多进程**：u1-l1 已经讲过，Mini-SGLang 是多进程系统，每个 GPU 上跑一个 Scheduler。本讲只需要知道「有些子包（如 `distributed`）专门服务于多卡」。
- **请求生命周期**：u1-l2 讲过一条请求从 API 到返回的大致路径。本讲会把这条路径**落到文件上**——每一步由哪个目录里的哪个文件负责。

> 本讲和前两讲的关系：u1-l1 给出「Mini-SGLang 有约 14 个子包」这种笼统说法；u1-l2 告诉你命令怎么敲。本讲则把这句话**精确化**到「磁盘上到底有哪些目录、哪些文件、文档和磁盘之间差在哪」。

## 3. 本讲源码地图

本讲只读 3 个关键源码（外加一些目录列举）：

| 文件 | 作用 |
|---|---|
| [docs/structures.md](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md) | 官方的「代码组织说明」，是模块职责的权威出处。 |
| [python/minisgl/core.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py) | 核心数据结构 `Req`/`Batch`/`Context`/`SamplingParams`，是贯穿全系统的「公共语言」。 |
| [pyproject.toml](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml) | 声明包的根目录在 `python/` 下，决定了 `import minisgl` 从哪里找代码。 |

另外会顺带引用入口文件与注册表：

- [python/minisgl/__main__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/__main__.py)
- [python/minisgl/server/__init__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/__init__.py)
- [python/minisgl/models/register.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py)

## 4. 核心概念与源码讲解

### 4.1 包目录划分

#### 4.1.1 概念说明

一个 Python 项目的「目录结构」决定了你 `import` 时的路径。Mini-SGLang 的源码全部住在 `python/minisgl/` 这一层下。这一层可以分成两类东西：

1. **子包目录**（带 `__init__.py` 的文件夹）：每个是一个独立职责模块，例如 `server/`、`scheduler/`、`engine/`。
2. **散落的 `.py` 文件**：直接挂在 `minisgl/` 根下，例如 `__main__.py`、`core.py`、`env.py`、`shell.py`。

理解目录结构，就是回答两个问题：**「一共有哪些子包」** 和 **「`import minisgl.xxx` 时，Python 会去哪找」**。

#### 4.1.2 核心流程

`import minisgl` 能成功，背后是 `pyproject.toml` 在告诉打包工具「源码根在 `python/` 下」。流程是：

```
pip install -e .  安装「可编辑」模式
   │
   ▼
读取 pyproject.toml：package-dir = {"" = "python"}
   │   → 告诉 setuptools：包的根目录不是仓库根，而是 python/
   ▼
读取 packages.find：where = ["python"]
   │   → 在 python/ 下扫描所有含 __init__.py 的目录
   ▼
得到可导入的包名：minisgl, minisgl.server, minisgl.scheduler ...
```

因为根目录被设成 `python/`，所以仓库里即使有 `docs/`、`benchmark/`、`tests/` 等顶层目录，它们也不会和 `minisgl` 这个包名冲突。

#### 4.1.3 源码精读

关键配置在 `pyproject.toml` 里：

[pyproject.toml:L54-L60](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L54-L60) —— 这一段把 `python/` 设为包根目录，并声明在 `python/` 下自动发现所有子包。没有这两行，`import minisgl` 会找不到代码。

实际磁盘上的 `python/minisgl/` 一共有 **15 个子包目录**和 4 个散落文件：

```
python/minisgl/
├── __main__.py        ← 入口：python -m minisgl 跑它
├── core.py            ← 核心数据结构（单文件，不是子包）
├── env.py             ← 运行期环境变量开关
├── shell.py           ← --shell 交互模式入口
├── attention/         ← 注意力后端（fa / fi / trtllm）
├── benchmark/         ← 基准测试工具
├── distributed/       ← 张量并行通信（all-reduce / all-gather）
├── engine/            ← 单 GPU 执行引擎
├── kernel/            ← 自定义 CUDA / Triton kernel
├── kvcache/           ← KV cache 池与前缀缓存
├── layers/            ← 构建 LLM 的基础算子块
├── llm/               ← Python 离线推理接口
├── message/           ← 进程间 ZMQ 消息与序列化
├── models/            ← 具体模型实现（Llama / Qwen ...）
├── moe/               ← Fused MoE 后端
├── scheduler/         ← 调度器（系统心脏）
├── server/            ← 前端服务 + 启动编排
├── tokenizer/         ← tokenize / detokenize 进程
└── utils/             ← 杂项工具（logger / zmq wrapper / Registry）
```

> ⚠️ **文档与磁盘的细微差异（重要）**：u1-l1 里笼统说「约 14 个子包」，而磁盘上实际是 **15 个子包目录**。原因是 `docs/structures.md` 在描述时，把 `minisgl.core` 当作一个模块列出（它确实是 `core.py` 单文件，不是目录），同时**没有单独列出 `moe/`**。所以：文档列了 `core` + 14 项 = 15 条说明，而磁盘是 15 个目录 + `core.py` 文件。两者都对，只是口径不同。这种「文档口径 ≠ 磁盘口径」的情况在真实项目里很常见，学会核对是源码阅读的基本功。

#### 4.1.4 代码实践

**实践目标**：亲手确认磁盘上的子包数量，建立「眼见为实」的习惯。

**操作步骤**：

1. 在仓库根目录执行 `ls -d python/minisgl/*/`，列出所有子目录。
2. 执行 `ls python/minisgl/*.py`，列出所有散落的单文件。
3. 对比上面给出的树状图，确认数量是否一致（应为 15 个目录、4 个 `.py` 文件）。

**需要观察的现象**：目录数量是 15，而不是 u1-l1 里说的「14」。`moe/` 目录确实存在，且 `docs/structures.md` 没有单独描述它。

**预期结果**：你得到一份和本讲一致的目录清单。如果数量不符，说明仓库版本与本讲 HEAD（`9a91cfa`）不同，需要重新核对。

**待本地验证**：如果你不在本仓库环境里，本步骤可跳过，直接使用本讲给出的树状图。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `docs/`、`tests/`、`benchmark/` 这些顶层目录不会和包名 `minisgl` 冲突？

> **答案**：因为 `pyproject.toml` 里 `package-dir = {"" = "python"}` 把包根设成了 `python/`，setuptools 只在 `python/` 下发现包，仓库根下的其他目录不参与打包，也不产生 `import` 名字。

**练习 2**：`core.py` 是「子包」还是「模块」？怎么判断？

> **答案**：它是**模块**（一个 `.py` 文件），不是子包（子包必须是含 `__init__.py` 的目录）。判断依据是磁盘形态：`core.py` 是文件，所以 `minisgl.core` 是模块而非包。这也解释了为什么 docs 把它和目录并列列出时容易让人误以为它是子包。

---

### 4.2 模块职责说明

#### 4.2.1 概念说明

知道了「有哪些目录」之后，下一个问题是「每个目录负责什么」。`docs/structures.md` 里有一段专门面向开发者的「Code Organization」说明，逐条解释了每个子包的职责。这是你阅读源码时最该先读的一段文档——它相当于官方给你的地图图例。

#### 4.2.2 核心流程

理解职责的最好办法，是把子包**沿着一条请求的数据流**排开。把 [u1-l2](u1-l2-install-and-run.md) 讲过的 8 步生命周期贴到目录上：

```
用户请求
  │  ① API Server 接收            → server/api_server.py
  │  ② 转发到 Tokenizer           → server/ → tokenizer/
  │  ③ 文本→token，发给 Scheduler → tokenizer/tokenize.py → message/
  │  ④ rank0 广播给其他 rank      → scheduler/io.py + distributed/
  │  ⑤ 每个 Scheduler 调度并前向   → scheduler/ + engine/
  │  ⑥ rank0 收结果发给 Detokenizer→ message/ → tokenizer/detokenize.py
  │  ⑦ token→文本，回 API         → tokenizer/detokenize.py
  │  ⑧ 流式返回用户               → server/api_server.py
  ▼
用户收到回复
```

你会看到：`server/`、`tokenizer/`、`scheduler/`、`engine/` 这四个子包正好对应数据流的四个「大站」；而 `message/`、`distributed/` 是连接它们的「管道」；`kvcache/`、`attention/`、`layers/`、`models/`、`kernel/`、`moe/` 则是 Engine 内部用到的「零件库」。

#### 4.2.3 源码精读

权威的职责说明在 `docs/structures.md`：

[docs/structures.md:L31-L49](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md#L31-L49) —— 这是「Code Organization」整段，逐条给出每个子包的职责。建议你在编辑器里打开它对照阅读。

其中对核心公共语言的描述尤其重要：

[docs/structures.md:L35](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md#L35) —— 说明 `minisgl.core` 提供 `Req`、`Batch`、`Context`、`SamplingParams`。这四个类是**所有子包共同使用的「公共数据语言」**，所以它们被放在根目录的 `core.py`，方便任何子包都能 `from minisgl.core import Req`。

而 8 步请求生命周期的原文在这里：

[docs/structures.md:L20-L29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md#L20-L29) —— 用来和 4.2.2 的数据流图对照。

下面这张表把文档职责、磁盘子包、关键文件三者合并（**本讲最重要的产出**）：

| 子包 | 职责（来自 docs/structures.md） | 关键文件 |
|---|---|---|
| `server` | CLI 参数与 `launch_server`，启动所有子进程；FastAPI 前端 `/v1/chat/completions` | `server/launch.py`、`server/api_server.py`、`server/args.py` |
| `tokenizer` | `tokenize_worker`，同时处理 tokenize 与 detokenize | `tokenizer/server.py`、`tokenizer/tokenize.py`、`tokenizer/detokenize.py` |
| `scheduler` | `Scheduler` 类，每个 TP worker 一个；rank0 收发消息 | `scheduler/scheduler.py`、`scheduler/io.py`、`scheduler/prefill.py`、`scheduler/decode.py`、`scheduler/cache.py`、`scheduler/table.py` |
| `engine` | `Engine` 类，单进程 TP worker，管 model/context/KVCache/attn/cuda graph | `engine/engine.py`、`engine/graph.py`、`engine/sample.py`、`engine/config.py` |
| `core`（文件） | `Req`/`Batch`/`Context`/`SamplingParams` 核心数据结构 | `core.py` |
| `kvcache` | KV cache 池与 manager 接口；`MHAKVCache`/Naive/Radix | `kvcache/base.py`、`kvcache/mha_pool.py`、`kvcache/naive_cache.py`、`kvcache/radix_cache.py` |
| `attention` | 注意力后端接口；flashattention/flashinfer/trtllm | `attention/base.py`、`attention/fa.py`、`attention/fi.py`、`attention/trtllm.py` |
| `layers` | 构建带 TP 的 LLM 基础块：linear/norm/embedding/rope | `layers/base.py`、`layers/linear.py`、`layers/norm.py`、`layers/embedding.py`、`layers/rotary.py`、`layers/attention.py` |
| `models` | 具体模型实现（Llama/Qwen2/Qwen3/Qwen3MoE/Mistral）；HF 权重加载与分片 | `models/llama.py`、`models/qwen3.py`、`models/config.py`、`models/weight.py`、`models/register.py` |
| `distributed` | all-reduce/all-gather 的 TP 接口；`DistributedInfo` | `distributed/impl.py`、`distributed/info.py` |
| `message` | api_server/tokenizer/scheduler 间的 ZMQ 消息；自动序列化 | `message/backend.py`、`message/frontend.py`、`message/tokenizer.py`、`message/utils.py` |
| `kernel` | 自定义 CUDA kernel，tvm-ffi 绑定与 JIT | `kernel/index.py`、`kernel/store.py`、`kernel/radix.py`、`kernel/pynccl.py`、`kernel/triton/` |
| `llm` | `LLM` 类，Python 离线推理接口 | `llm/llm.py` |
| `moe`（文档未单列） | Fused MoE 后端 | `moe/fused.py`、`moe/base.py` |
| `benchmark` | 基准测试工具 | `benchmark/client.py`、`benchmark/perf.py` |
| `utils` | logger、zmq wrapper 等杂项；`Registry` | `utils/logger.py`、`utils/mp.py`、`utils/registry.py`、`utils/hf.py` |

> 这张表里你不用记住每个文件，只要记住「要去某个子包找某类功能」即可。比如以后想改采样逻辑，就直奔 `engine/sample.py`；想看 KV cache 怎么存，就进 `kvcache/mha_pool.py`。

#### 4.2.4 代码实践

**实践目标**：把文档职责落到文件，验证表里每一行都真实存在。

**操作步骤**：

1. 打开 [docs/structures.md](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md)，通读 L31–L49。
2. 对照上面那张表，挑 3 个子包（建议 `scheduler`、`engine`、`kvcache`），用 `ls python/minisgl/<子包>/` 确认表里列出的关键文件确实存在。
3. 对 `moe/` 这个「文档没单列」的子包，打开 `docs/structures.md` 全文搜索 "moe"，确认它确实只在别处被间接提到，而没有独立条目。

**需要观察的现象**：表里的文件名都能在磁盘上找到；`moe/` 目录有 `base.py` 和 `fused.py`，但 `docs/structures.md` 没有专门一句话描述它。

**预期结果**：你对「文档说了什么 / 磁盘上有什么」建立了一一对应，并识别出 `moe` 这个文档缺口。

**待本地验证**：若无法 `ls`，可以直接在 GitHub 上浏览 `python/minisgl/` 目录来核对。

#### 4.2.5 小练习与答案

**练习 1**：如果要新增一个「采样策略」，你会改哪个子包？为什么不去 `scheduler/` 里找？

> **答案**：去 `engine/sample.py`。因为采样发生在 Engine 的前向计算之后（模型算出 logits 再选 token），属于「执行」职责，由 `engine/` 负责；`scheduler/` 只负责「决定跑哪些请求、怎么组 batch」，不碰具体的数学计算。

**练习 2**：`message/` 子包在数据流中扮演什么角色？为什么它要独立成一个子包？

> **答案**：它定义 `server`/`tokenizer`/`scheduler` 之间通过 ZMQ 传递的消息类型，并提供自动序列化/反序列化。独立成包是因为**多个进程都要共享同一套消息定义**——tokenize 进程和 scheduler 进程都必须能 `from minisgl.message import ...`，否则无法通信。

---

### 4.3 入口与导出

#### 4.3.1 概念说明

「目录结构」的最后一个关键问题是：**程序从哪里开始执行？** 这涉及两个机制：

1. **命令入口**：`python -m minisgl` 到底跑了哪个文件。
2. **全局状态导出**：`core.py` 怎么让所有子包共享同一个全局 `Context`。

理解这两点后，你就能回答「为什么我敲一行命令，就有一堆进程跑起来」。

#### 4.3.2 核心流程

命令入口的链条很短：

```
python -m minisgl
   │  Python 自动寻找 minisgl/__main__.py
   ▼
__main__.py:  from .server import launch_server; launch_server()
   │  server/__init__.py 把 launch_server「提到门口」再导出
   ▼
server/launch.py:  launch_server() 解析参数、spawn 子进程、起 FastAPI
```

这是一个典型的「门面（facade）」模式：`__main__.py` 只负责「开门」，真正的活儿被委托给 `server/launch.py`。`server/__init__.py` 起到中转作用，让导入路径更简洁。

而全局状态的导出靠 `core.py` 里的「模块级单例」：

```
core.py 定义 _GLOBAL_CTX（模块级变量，初始为 None）
   │  Engine 初始化时调用 set_global_ctx(ctx) 写入
   ▼
任何子包调用 get_global_ctx() 读到同一个 Context
   │  → 于是 layers/attention/kvcache 都能访问当前的 attn_backend、kv_cache
```

#### 4.3.3 源码精读

先看命令入口。`__main__.py` 整个文件只有 3 行有效代码：

[python/minisgl/__main__.py:L1-L5](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/__main__.py#L1-L5) —— 第 1 行从 `.server` 导入 `launch_server`，第 5 行调用它。`assert __name__ == "__main__"` 保证它只在被当作主模块运行时执行（也就是 `python -m minisgl`）。

那么 `from .server import launch_server` 是怎么找到 `launch_server` 的？靠 `server/__init__.py` 的再导出：

[python/minisgl/server/__init__.py:L1-L3](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/__init__.py#L1-L3) —— 第 1 行把 `launch.py` 里的 `launch_server` 提到包门口，第 3 行用 `__all__` 声明「对外只导出这一个名字」。所以 `launch_server` 的真正实现住在 `server/launch.py`（这一点 [u1-l2](u1-l2-install-and-run.md) 已经讲过它的四步职责）。

再看全局状态导出。`core.py` 用一个模块级变量 + 两个函数实现单例：

[python/minisgl/core.py:L125-L136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L125-L136) —— `_GLOBAL_CTX` 初始为 `None`；`set_global_ctx` 在 Engine 初始化时被调用一次，写入全局上下文并断言「只能设一次」；`get_global_ctx` 让任何子包都能取回它。这样 `Context` 里持有的 `attn_backend`、`kv_cache`、`page_table` 就成了全进程共享的全局设施。

这个被共享的 `Context` 长这样：

[python/minisgl/core.py:L100-L122](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L100-L122) —— 它持有 `page_table`、`attn_backend`、`moe_backend`、`kv_cache`，并通过 `forward_batch` 这个上下文管理器临时挂上「当前正在算的 Batch」。注意第 117 行的断言「不允许嵌套 forward_batch」——整个进程同一时刻只有一个活跃 batch。

> **关于「注册表」这种导出形式**：除了「再导出」和「全局单例」，项目里还有第三种「找东西」的机制——**注册表**。它用字符串名字动态查找类，是「按模型架构名实例化模型」的关键。[python/minisgl/models/register.py:L5-L21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L5-L21) 里的 `_MODEL_REGISTRY` 把 `"LlamaForCausalLM"` 这样的字符串映射到 `(模块路径, 类名)`，`get_model_class` 再用 `importlib.import_module` 动态导入。这样加载新模型时不用改一堆 `if/else`，只要往字典里加一行。底层通用注册表工具在 [python/minisgl/utils/registry.py:L6-L38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/registry.py#L6-L38)。

#### 4.3.4 代码实践

**实践目标**：亲手跟踪一次「命令 → 入口 → 实现」的导入链，验证 `launch_server` 住在 `launch.py` 里。

**操作步骤**：

1. 打开 [python/minisgl/__main__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/__main__.py)，确认它只调用了 `launch_server`。
2. 打开 [python/minisgl/server/__init__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/__init__.py)，确认第 1 行 `from .launch import launch_server`。
3. 打开 `python/minisgl/server/launch.py`，找到 `def launch_server` 的定义，确认它的四步职责（解析参数、spawn 子进程、ack 等待、起服务）和 [u1-l2](u1-l2-install-and-run.md) 讲的一致。

**需要观察的现象**：三个文件形成一条清晰的链：`__main__.py`（开门）→ `server/__init__.py`（中转）→ `server/launch.py`（真正实现）。

**预期结果**：你能用自己的话说出「为什么 `__main__.py` 只有 3 行」——因为它是门面，真正的编排逻辑被有意放在 `launch.py`，再通过 `__init__.py` 暴露出来。

**待本地验证**：第 3 步需要打开 `launch.py` 阅读 `launch_server` 的函数体；若环境不便，至少在 GitHub 上跳转到该文件确认函数存在。

#### 4.3.5 小练习与答案

**练习 1**：`__main__.py` 里的 `assert __name__ == "__main__"` 有什么作用？如果删掉会怎样？

> **答案**：它保证这段代码**只在 `python -m minisgl` 直接运行时执行**，而在被 `import` 时不会副作用地启动服务。删掉后，任何人 `import minisgl` 都会触发 `launch_server()`，这是危险的（会无端启动一堆进程）。这是 Python 的常见守护写法。

**练习 2**：为什么 `set_global_ctx` 要断言「全局上下文只能设一次」？

> **答案**：因为整个进程共享同一个 `Context`，如果允许重复设置，会导致旧的 `attn_backend`、`kv_cache` 等设施被悄悄替换，持有旧引用的子包会行为错乱。断言把这个「理应只发生一次」的假设变成硬约束，方便及早发现 bug。

**练习 3**：`get_model_class` 用 `importlib.import_module` 动态导入模型类，相比「在文件顶部直接 `from .llama import LlamaForCausalLM`」有什么好处？

> **答案**：好处是**延迟导入**——只有真正用到某个模型时才加载它的模块。因为 `models/` 下有很多模型文件（llama、qwen2、qwen3、qwen3_moe、mistral），每个又各自 `import` 了一堆 torch/CUDA 依赖；若在顶部一次性全导入，启动会变慢、内存占用也会升高。注册表 + 动态导入让你「只为用到的那个模型付代价」。

---

## 5. 综合实践

**任务**：绘制一份属于你自己的《Mini-SGLang 模块地图》，并用一条真实请求给它「通电」验证。

**步骤**：

1. **建表**：以本讲 4.2.3 的表为模板，但把「关键文件」一列改成**你自己读过的**——也就是说，挑出每个子包里你最该先读的「入口文件」（每个子包只选 1 个），而不是把所有文件都列上。例如 `scheduler/` 你可能只选 `scheduler.py`，`engine/` 只选 `engine.py`。

2. **通电验证**：用 [u1-l2](u1-l2-install-and-run.md) 学过的方式启动一次服务（哪怕用最小的 `Qwen/Qwen3-0.6B`），然后对照你在第 1 步画出的地图，预测这条请求会**依次经过哪些子包**。把你的预测写成一条路径，例如：

   ```
   server/api_server.py → message/ → tokenizer/tokenize.py
     → message/ → scheduler/io.py → scheduler/scheduler.py
     → engine/engine.py → layers/ + attention/ + kvcache/
     → engine/sample.py → message/ → tokenizer/detokenize.py
     → server/api_server.py
   ```

3. **标注分工**：在你的路径上，用三种颜色或记号区分「控制消息（ZMQ）」「张量数据（NCCL）」「本地计算（CUDA）」分别落在哪一段。提示：`message/` 段是 ZMQ，`distributed/` 段是 NCCL，`engine/layers/attention` 段是本地 CUDA。

4. **核对文档缺口**：在地图上单独标出 `moe/`，并写一句话说明「这个子包在 `docs/structures.md` 里没有独立条目，但磁盘上存在」。

**预期产出**：一张表 + 一条带标注的请求路径。如果你能让路径里的每一跳都对应到地图上的某个子包，说明你已经建立起 Mini-SGLang 的整体认知，后续单篇讲义只是在「放大」这张地图的某一块。

> 如果没有 GPU 环境无法真正启动服务，第 2 步可以退化为「纯源码阅读型」：在 GitHub 上依次点开路径里的每个文件，确认它们之间存在调用或消息传递关系即可。

## 6. 本讲小结

- Mini-SGLang 的源码全部在 `python/minisgl/` 下，共 **15 个子包目录** + 4 个散落文件（`__main__.py`、`core.py`、`env.py`、`shell.py`）。
- `pyproject.toml` 用 `package-dir = {"" = "python"}` 把包根设在 `python/`，这是 `import minisgl` 能找到代码的根本原因。
- `docs/structures.md` 的「Code Organization」一段是模块职责的权威出处；它和磁盘有细微差异——它把 `core`（单文件）当作模块列出，且未单独列出 `moe/`。
- 命令入口是一条短链：`python -m minisgl` → `__main__.py`（门面）→ `server/__init__.py`（再导出）→ `server/launch.py`（真正实现）。
- 全局共享状态靠 `core.py` 的 `_GLOBAL_CTX` 单例：`set_global_ctx` 写一次，所有子包用 `get_global_ctx` 读取。
- 除「再导出」「单例」外，项目还用**注册表 + 动态导入**（`models/register.py`）按字符串名实例化模型，是后续接入新模型的关键机制。

## 7. 下一步学习建议

有了这张地图，接下来可以按数据流自顶向下深入：

- **想理解数据流的「语言」**：下一步学 [u2-l1 核心数据结构 Req/Batch/Context](u2-l1-core-data-structures.md)，精读 `core.py` 里那四个类的字段与不变量。
- **想理解多进程怎么协作**：学 [u1-l4 进程架构与请求生命周期](u1-l4-process-architecture.md)，把本讲的目录图升级成「进程拓扑图」。
- **想立刻看一个子包的实现**：可以直接跳进 `server/launch.py`，对照 [u1-l2](u1-l2-install-and-run.md) 的四步职责阅读源码，作为「用地图导航真实代码」的第一次练习。

建议的阅读节奏：先把本讲的对照表打印或贴在手边，再开始任何一篇深入讲义——遇到陌生文件名，先回到表里找它属于哪个子包、负责什么，再读细节。
