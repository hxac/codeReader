# 项目概览：RL 权重更新问题与 checkpoint-engine 的定位

> 本讲是整本学习手册的第一讲，不要求读者了解任何项目细节。
> 本讲涉及的最小模块：README、ParameterServer、Broadcast、P2P。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「RL 训练中推理引擎权重同步」这个痛点到底痛在哪里。
2. 说明 checkpoint-engine 作为**轻量级权重更新中间件（middleware）**的定位：它不训练、不推理，只负责「把新权重高效搬进推理引擎」。
3. 区分两种更新方式 **Broadcast** 与 **P2P** 的原理、适用场景，以及它们在源码里的同一个入口 `ParameterServer._update_per_bucket` 中如何分流。
4. 记住项目的关键性能指标（Kimi-K2 约 1 万亿参数、数千 GPU 规模下更新约 20 秒）和支持的硬件（CUDA / NPU / XPU）。
5. 学会通过 README 与 `pyproject.toml` 快速读懂一个开源项目的「自我介绍」。

## 2. 前置知识

本讲尽量从零讲起，但以下几个名词会反复出现，先用通俗语言解释：

- **强化学习（RL）训练循环**：在 RL（例如 RLHF/GRPO）中，「训练侧」不断产生新的模型权重，「推理侧」负责用当前权重高速生成 rollout（响应样本）。每训练若干步，就要把新权重同步给推理引擎，否则推理用的还是旧策略。权重同步发生在训练循环的关键路径上，越慢，GPU 闲置越严重。
- **推理引擎（inference engine）**：如 vLLM、SGLang 这类专门做高性能 LLM 推理的服务。权重平时在启动时从磁盘加载一次；训练中反复「热更新」权重并不是它们的主路径，所以需要外部工具协助。
- **权重 / checkpoint**：模型的所有可学习参数。1 万亿（1T）参数用 FP8 存储约 1 TB，用 BF16 约 2 TB——「把 1~2 TB 数据搬进数千张 GPU」就是本项目要解决的问题量级。
- **H2D（Host-to-Device）**：从 CPU 内存拷贝到 GPU 显存，走 PCIe。相对 GPU 间的高速互联（NVLink 等），PCIe 通常是瓶颈之一。
- **锁页内存（pinned / page-locked memory）**：普通 CPU 内存可能被操作系统换页；锁页后地址固定，才能启动异步 H2D 拷贝。后续单元会专门讲。
- **IPC（Inter-Process Communication）**：进程间通信。这里的重点是 **CUDA IPC**——一个进程把自己 GPU 显存里的 tensor 以「句柄」形式交给另一个进程直接访问，免去多余的拷贝。
- **RDMA**：远程直接内存访问，绕过对方 CPU 内核直接读写远端内存，是跨节点高速传数据的基础。P2P 模式依赖的 `mooncake-transfer-engine` 就是建立在 RDMA 之上的。
- **colocated（混合部署）与 disaggregated（分离部署）**：colocated 指训练与推理进程部署在同一批 GPU 上（共享机器）；disaggregated 指两者分开部署。这个区别直接决定了 Broadcast 与 P2P 的适用场景。
- **中间件（middleware）**：夹在「训练框架」和「推理引擎」之间、只做一件事（这里是搬权重）的软件层。checkpoint-engine 的自我描述就是 "a lightweight, decoupling and efficient weight update middleware"。

## 3. 本讲源码地图

本讲主要读两个文件，但会顺路「远眺」几个核心源码文件，为后续单元建立印象：

| 文件 | 作用 | 本讲用法 |
| :--- | :--- | :--- |
| `README.md` | 项目的自我介绍：定位、架构、性能基准、安装与快速上手 | 本讲主战场，几乎所有结论都出自这里 |
| `pyproject.toml` | 包元数据：依赖、可选依赖（`[p2p]`）、打包内容、pytest marker | 看清「最小安装」与「P2P 安装」的区别 |
| `checkpoint_engine/ps.py` | `ParameterServer` 类，权重更新的核心逻辑 | 只看类定义和 `update` / `_update_per_bucket` 的分流点 |
| `checkpoint_engine/worker.py` | 推理引擎侧的接收逻辑 `update_weights_from_ipc` | 只认识它的位置和职责 |
| `checkpoint_engine/device_utils.py` | `DeviceManager`：cuda / npu / xpu 的统一设备抽象 | 只看设备类型探测，证明多硬件支持 |
| `checkpoint_engine/distributed/`、`xpu_ipc/`、`p2p_store.py` | 分布式后端（NCCL/HCCL）、XPU 原生扩展、RDMA 封装 | 本讲只点名，不深入 |

整个 `checkpoint_engine` 包只有十来个源码文件——这是一个「代码量不大、涉及面很广」的项目，非常适合逐文件精读。

## 4. 核心概念与源码讲解

### 4.1 README 与项目定位：权重更新中间件

#### 4.1.1 概念说明

先想一个问题：为什么不直接让 vLLM 自己从磁盘重新加载一遍新 checkpoint？

- 磁盘 → 显存的路径慢（重复读盘、逐层加载、初始化开销），千亿/万亿模型一次可能要几分钟到几十分钟。
- RL 训练中这种同步**每个训练阶段都会发生**，累积的空闲时间非常可观。
- 训练侧的权重本来就在 CPU/GPU 内存里（或刚落盘），理应走「内存到显存」的近路。

checkpoint-engine 的定位就是补上这块短板：**一个只做「inplace 权重更新」的轻量中间件**。README 第一段话就把定位、效率和量级说清楚了：

> "Checkpoint-engine is a simple middleware to update model weights in LLM inference engines -- a critical step in reinforcement learning. ... updating our Kimi-K2 model (1 Trillion parameters) across thousands of GPUs takes about 20s."

#### 4.1.2 核心流程

从使用者视角，这个中间件介入 RL 循环的方式大致是：

```text
训练侧产生新权重（落盘为 safetensors，或直接在内存中）
        │
        ▼
checkpoint-engine: register_checkpoint   ← 把权重放进（锁页）CPU 内存
        │
        ▼
checkpoint-engine: gather_metas          ← 收集各 rank 的内存布局元数据，制定传输计划
        │
        ▼
checkpoint-engine: update                ← 按 bucket 把权重送进各推理引擎（Broadcast 或 P2P）
        │
        ▼
推理引擎热加载新权重，继续用新策略生成 rollout
```

数据量级估算：1T 参数 FP8 约 1 TB，若想在 \( 20\,\mathrm{s} \) 内搬完，聚合带宽需要达到约

\[ B_{\text{agg}} \approx \frac{1\,\mathrm{TB}}{20\,\mathrm{s}} = 50\,\mathrm{GB/s} \]

单卡 PCIe（约 25~50 GB/s）根本不够，所以必须依靠**多卡并行 + 流水线重叠 + 集合通信**，这正是后面几个模块要拆的内容。

#### 4.1.3 源码精读

项目定位与性能声明（来源：README 开头）：

- [README.md:1-4](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L1-L4)：项目一句话定位——LLM 推理引擎的权重更新中间件，是强化学习中的关键步骤；并给出 Kimi-K2（1T 参数、数千 GPU）约 20s 的更新耗时。

「架构」一节点名了核心类与两种实现：

- [README.md:13-18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L13-L18)：核心权重更新逻辑在 `ParameterServer` 类中，它是一个**与推理引擎同机部署（colocated）的服务**，提供 Broadcast 与 P2P 两种更新实现。注意两个提示：Broadcast 对应 `_update_per_bucket` 传入 `ranks == None or []`；P2P 对应 `ranks` 被指定。

当前的限制与未来工作（帮你建立合理预期）：

- [README.md:227-231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L227-L231)：目前只测试了 vLLM 与 SGLang；论文中的「完美三阶段流水线」尚未实现（在 PCIe 上 H2D 与 broadcast 不冲突的架构里才有用）。

#### 4.1.4 代码实践

**实践目标**：用 5 分钟读完 README 的前三个小节，并浏览仓库顶层结构，验证「这是一个小而聚焦的中间件项目」。

1. 操作步骤：
   - 打开 [README.md](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md)，通读到 "Benchmark" 一节为止。
   - 在本地仓库根目录执行 `ls`，确认顶层只有 `checkpoint_engine/`（源码包）、`examples/`、`tests/`、`patches/`、`docs/`、`figures/` 等少量目录。
   - 再执行 `git log --oneline -5` 感受一下项目的提交粒度（例如最新一条是 "Support current vLLM stateless process groups"）。
2. 需要观察的现象：源码包 `checkpoint_engine/` 下文件数量很少（约十个 `.py` 加两个子包）。
3. 预期结果：你能不假思索地说出「这个项目只做一件事：把权重高效更新进推理引擎」。
4. 待本地验证：`ls` / `git log` 的具体输出取决于你本地的克隆状态。

#### 4.1.5 小练习与答案

**练习 1**：README 说自己是 "middleware"。结合本讲内容，说出它「夹」在哪两者之间？
**答案**：夹在训练侧（产出新权重的 RL 训练循环 / checkpoint 文件）与推理引擎（vLLM、SGLang 等）之间，只负责把新权重高效地送进推理引擎。

**练习 2**：1T 参数 FP8 权重约 1 TB。如果用最朴素的方式「每张卡各自从磁盘读完整份权重」，主要浪费在哪里？
**答案**：同一份数据被重复读取多份（磁盘带宽和 CPU 内存都被放大 N 倍）；且磁盘→CPU→GPU 的串行路径无法利用多卡集合通信的聚合带宽，也无法与拷贝重叠流水执行。

---

### 4.2 ParameterServer：核心权重更新服务类

#### 4.2.1 概念说明

`ParameterServer`（常简称 PS）是整个项目的「大脑」。注意：虽然名字里有 "Server"，但它**不是一个独立的常驻服务器进程**，而是与推理引擎同机部署（colocated）的一组服务逻辑——典型用法是每个 GPU 上跑一个 PS 实例（一个 rank），它管理本机的锁页内存，并协调跨机分发。

它的职责可以概括为四件事：

1. **持有权重**：把 checkpoint 加载进（锁页）CPU 内存，持有分片权重的引用。
2. **收集元数据**：通过 `gather_metas` 让所有 rank 互相知道「谁手里有哪些权重、内存怎么布局」。
3. **制定计划**：决定 bucket 大小（`_detect_bucket_size`）、把参数切分成 bucket（`_gen_h2d_buckets`）。
4. **执行更新**：`update` 驱动整条传输流水线，并通过 ZeroMQ socket 控制推理引擎配合。

#### 4.2.2 核心流程

PS 的标准生命周期（后续单元会逐个精读）：

```text
__init__            读取 RANK/WORLD_SIZE 环境变量，探测设备，初始化 TCPStore
      │
register_checkpoint 把 safetensors 加载进锁页内存（可复用共享内存池）
      │
gather_metas        all_gather_object 收集全部 rank 的内存缓冲元数据
      │                   并构建全局参数表 + RDMA 拓扑
      │
update              按 bucket 执行传输（Broadcast 或 P2P），
      │             通过 ZMQ 指挥推理引擎 attach IPC 句柄、reload 权重
      ▼
unregister_checkpoint   释放锁页内存与相关注册信息
```

#### 4.2.3 源码精读

- [checkpoint_engine/ps.py:176-189](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L176-L189)：`class ParameterServer` 的定义与 `__init__` 签名——参数包括 `rank`、`world_size`、`auto_pg`（是否自动建/销毁进程组）、`gpu_count`、`mem_fraction`、`master_addr/port`，全部关键字传参。
- [checkpoint_engine/ps.py:198-208](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L198-L208)：初始化时从环境变量读取 `RANK`/`WORLD_SIZE`，用 `DeviceManager()` 探测设备，计算 `local_rank = rank % gpu_count`，并初始化本地/远端 RDMA 设备表和 `PS_MEM_FRACTION`（默认 0.9）。这说明 PS 天生就是「多进程 + 环境变量驱动」的分布式程序。
- [checkpoint_engine/ps.py:569-592](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L569-L592)：`update` 方法及其文档字符串。注意 docstring 里的一句话：**「This function should be called after gather_metas」**——更新前必须先收集元数据；同时 `ranks` 参数的注释直接说明了两种模式的分工（不设置 → 全量广播最快；设置 → P2P 灵活更新一部分 rank，适合分离式架构）。
- [checkpoint_engine/ps.py:462-467](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L462-L467)：`gather_metas` 的定义与文档——从所有 rank 收集参数元数据（memory_buffer 等），结果写入 `_current_global_parameter_metas`，可经 `get_metas` 导出。
- [checkpoint_engine/worker.py:54-61](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L54-L61)：推理引擎侧的对应入口 `update_weights_from_ipc(zmq_ctx, zmq_handle, device_id, run=..., post_hook=...)`——PS 的「对端」，通过 ZMQ REP socket 接收 IPC 句柄并从共享显存中切出权重。本讲只需记住它的位置。

#### 4.2.4 代码实践

**实践目标**：不看本讲正文，独立在源码里找到 `ParameterServer` 的三个核心方法及其调用顺序约束。

1. 操作步骤：
   - 在仓库根目录执行：`grep -n "def update\|def gather_metas\|def register_checkpoint" checkpoint_engine/ps.py`。
   - 打开 `checkpoint_engine/ps.py` 第 569 行附近，阅读 `update` 的完整 docstring（约到 592 行）。
   - 用 `grep -n "class " checkpoint_engine/ps.py` 确认整个文件只有一个核心类。
2. 需要观察的现象：三个方法的行号；`update` docstring 中关于 `ranks` 参数与 `gather_metas` 前置条件的描述。
3. 预期结果：你能在自己的笔记里写下「调用顺序必须是 `register_checkpoint` → `gather_metas` → `update`」，并注明 `ranks=None` 与 `ranks=[...]` 的语义差异。
4. 待本地验证：无（纯源码阅读，结果确定）。

#### 4.2.5 小练习与答案

**练习 1**：`ParameterServer` 是不是要先调用 `gather_metas` 才能调用 `update`？依据是什么？
**答案**：是。`update` 的 docstring 明确写着 "This function should be called after gather_metas"（[ps.py:578](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L578)），且 `_update_per_bucket` 一开始就断言 `_current_global_parameter_metas` 非空。

**练习 2**：为什么 `local_rank` 用 `rank % gpu_count` 计算？
**答案**：`rank` 是全局编号（跨所有节点），而每个节点有 `gpu_count` 张卡且各节点卡数相同；对 `gpu_count` 取模即可得到「本机内的第几张卡」，从而绑定对应的 GPU 与同机的推理引擎进程。

---

### 4.3 Broadcast：三阶段流水线广播更新

#### 4.3.1 概念说明

Broadcast 是**默认且最快**的更新方式，用于「一大批判理实例需要同步更新到同一份新权重」的场景——典型的 colocated 架构：训练和推理共用同一批 GPU，更新时所有实例一起换权重。

它面临的核心矛盾是：**checkpoint-engine 持有的是 CPU 内存中的分片权重，而各推理实例的显存分片方式（TP 切分）很可能与它不同**。因此不能简单逐 tensor 点对点发，而是把传输组织成三个阶段：

1. **H2D**：把权重从 CPU 内存搬进 GPU 显存（来源可能是磁盘文件或训练引擎）。
2. **broadcast**：在 checkpoint-engine 的各 worker 之间广播；广播结果落在一块通过 CUDA IPC 与推理引擎**共享**的显存 buffer 里。
3. **reload**：推理引擎自己决定从这块广播数据中拷出它需要的那个子集（按它的 TP 分片）。

#### 4.3.2 核心流程

```text
┌────────────────── update() 主循环（逐 bucket）──────────────────┐
│                                                                  │
│  bucket i                                                        │
│  ─────►  [H2D] 权重从锁页内存拷入 h2d_buffer（GPU）              │
│              │                                                   │
│              ▼                                                   │
│          [broadcast] dist.broadcast 到各 rank，                   │
│              数据落入共享 IPC buffer；推理引擎按 metadata reload   │
│              │                                                   │
│              ▼                                                   │
│          （下一轮 H2D 与本轮 reload/broadcast 重叠 ← 流水线）      │
└──────────────────────────────────────────────────────────────────┘
```

要点：

- PS 通过一个 ZeroMQ socket 控制推理引擎的节奏（REQ/REP 严格一问一答）。
- 流水线用**双缓冲**（2 倍 bucket 大小的 buffer）实现「一边拷下一桶、一边广播当前桶」的重叠。
- 流水线要额外吃显存；**显存不足时会退化为串行执行**（README 明确说明）。

从流水线的角度，理想情况下总耗时近似为：

\[ T_{\text{pipeline}} \approx \max(T_{\text{H2D}},\ T_{\text{broadcast}},\ T_{\text{reload}}) \times N_{\text{bucket}} \]

而不是三个阶段耗时相加的 \( (T_{\text{H2D}} + T_{\text{broadcast}} + T_{\text{reload}}) \times N_{\text{bucket}} \)。这就是「重叠通信与拷贝」的收益来源。

#### 4.3.3 源码精读

- [README.md:20-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L20-L28)：三阶段的官方定义，以及「先收集元数据制定计划（含决定 bucket 大小），执行时用 ZeroMQ 控制推理引擎，并把传输组织成通信与拷贝重叠的流水线」的总体描述；细节指向 Kimi-K2 技术报告。
- [README.md:37](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L37)：一句关键的话——"Pipelining naturally requires more GPU memory. When memory is not enough, checkpoint-engine will fallback to serial execution."（流水线天然更耗显存，不够就退化为串行。）
- [checkpoint_engine/ps.py:596-604](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L596-L604)：`update` 的执行骨架——必要时自动初始化进程组；`ranks` 为空则 `ranks_group = None`（即 Broadcast 模式）；在 `build_ipc_handler(...)` 上下文中调用 `_update_per_bucket`。注释说明 `with` 语句保证任何退出路径都会释放已导出的 IPC 句柄。
- [checkpoint_engine/ps.py:774-800](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L774-L800)：`_update_per_bucket` 开头的模式分流——`if not ranks:` 走广播分支（只打日志）；`else` 走 P2P 分支。广播分支前还有一个重要防御：若当前设备不支持跨进程设备 tensor IPC（`supports_device_ipc()` 为假），直接抛出带解释的 `RuntimeError`（[ps.py:762-772](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L762-L772)），避免在更深处报出难懂的错误。
- [checkpoint_engine/ps.py:804-826](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L804-L826)：计划与缓冲区——`_detect_bucket_size` 探测桶大小并返回 `disable_h2d_buffer` 开关；`_gen_h2d_buckets` 切桶；若未禁用则分配 `h2d_buffer`（桶大小），并**总是**分配 2 倍桶大小的 `buffer`（第 824-826 行）——这正是双缓冲流水线的物证。
- [checkpoint_engine/ps.py:842-849](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842-L849)：PS 绑定 ZMQ REQ socket、在后台线程跑 `req_func`（去请求推理引擎），然后 `socket.send_pyobj(handle)` 把 IPC 句柄一次性发给对端。注释强调「句柄对每种 handler 都是自包含的，所以一次 ZMQ send 就完成了交接」。
- [checkpoint_engine/worker.py:78-98](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L98)：推理引擎侧的状态机注释——收到 tensor metadata 就更新权重；收到 Exception 就抛出停止；第一次收到 `None` 释放资源；第二次收到 `None` 调用 `post_hook` 并结束。这就是「reload 阶段」在 worker 侧的体现。

#### 4.3.4 代码实践

**实践目标**：在源码中为「三阶段」各找到一处对应的代码位置，亲手验证 README 的描述不是空话。

1. 操作步骤：
   - **H2D**：阅读 [checkpoint_engine/ps.py:684](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684) 的 `_copy_to_buffer`（它在主循环第 856-862 行被调用，把锁页内存中的 bucket 拷进 `h2d_buffer`）。
   - **broadcast**：查看 [checkpoint_engine/ps.py:890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L890) 的 `dist.broadcast(buffer_b, src=receiver_rank, group=ranks_group)`——注意广播的源是「接收方 rank」，且落在双缓冲中的 `buffer_b` 上。
   - **reload**：在 `worker.py` 中搜索 `run(`，确认 worker 收到张量元数据后调用外部传入的 `run` 回调（vLLM 扩展里就是按元数据从共享 buffer 切权重），对应 [worker.py:78-82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L82) 的注释。
2. 需要观察的现象：三个位置分别属于 PS 侧拷贝、PS 侧集合通信、worker 侧回调。
3. 预期结果：你能在一张纸上画出「锁页内存 → h2d_buffer →（broadcast）→ 共享 IPC buffer →（reload）→ 推理引擎权重」的数据流，并标注每一步的函数名。
4. 待本地验证：无（纯源码阅读）。

#### 4.3.5 小练习与答案

**练习 1**：Broadcast 更新要求 `ranks` 参数是什么值？
**答案**：`None` 或空列表 `[]`。`_update_per_bucket` 中 `if not ranks:` 即进入广播分支（[ps.py:776](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L776)）。

**练习 2**：为什么流水线模式下要分配「2 倍 bucket 大小」的 buffer？
**答案**：双缓冲：一块正在被广播/reload（当前桶），另一块同时接收下一桶的 H2D 拷贝，两边交替前进，通信与拷贝才能重叠；这以多占一倍 bucket 的显存为代价。

**练习 3**：什么情况下 checkpoint-engine 会放弃流水线？
**答案**：显存不足时。README L37 明确说明会退化为串行执行；源码中对应 `_detect_bucket_size` 返回的 `disable_h2d_buffer` 开关（为真时不再分配独立的 `h2d_buffer`，见 [ps.py:813-817](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L813-L817)）。

---

### 4.4 P2P：面向动态扩容的点对点更新

#### 4.4.1 概念说明

P2P 模式解决的是另一个问题：**已有实例正在服务请求时，新实例动态加入**（比如某实例崩溃重启，或弹性扩容）。此时：

- 不能再做全集群广播——那会打断存量实例正在服务的请求；
- 只需把权重从「存量实例的 CPU 内存」送到「新实例的 GPU 显存」。

为此，checkpoint-engine 使用 [mooncake-transfer-engine](https://github.com/kvcache-ai/Mooncake) 基于 RDMA 做 P2P 传输。为了保证传输效率，它还专门做了 **bucket 分配优化**：为一对对「发送方-接收方」分配合适的 bucket，目标是让每个发送方和接收方的可用网络带宽都被打满。

一句话对比：

| 维度 | Broadcast | P2P |
| :--- | :--- | :--- |
| 触发条件 | `ranks=None/[]` | `ranks=[...]` 指定目标 rank |
| 典型架构 | colocated（训练推理同机） | disaggregated / 动态扩容、滚动重启 |
| 传输机制 | H2D + `dist.broadcast` + CUDA IPC 共享 | mooncake-transfer-engine（RDMA）点对点读 |
| 对存量实例影响 | 全员同步参与 | 存量实例只需作为发送方提供内存，不中断服务 |
| 速度 | 最快，默认首选 | 略慢但灵活（见基准表两列对比） |

#### 4.4.2 核心流程

```text
新实例通过 load_metas 拿到存量实例的元数据（内存布局、RDMA 拓扑）
        │
        ▼
update(ranks=[...])            只涉及目标 rank 子集
        │
        ▼
_assign_receiver_ranks         按发送方 RDMA 设备分组、贪心分配 bucket，
        │                      使收发双方带宽都打满
        ▼
P2PStore.batch_transfer_sync_read   接收方经 RDMA 直接读发送方注册过的内存
        │
        ▼
新实例 reload 权重，开始服务
```

#### 4.4.3 源码精读

- [README.md:18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L18)：P2P 的官方定义——用于「新推理实例动态加入（重启或弹性可用性）而存量实例仍在服务」的场景；为避免影响存量负载，用 mooncake-transfer-engine 把权重从存量实例的 CPU 送到新实例的 GPU；对应 `_update_per_bucket` 传入指定的 `ranks`。
- [README.md:39-43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L39-L43)：bucket 分配优化——目标是最小化总传输时间，即让每个发送方与接收方的可用带宽都被充分利用，细节指向 issue #25。
- [checkpoint_engine/ps.py:779-802](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L779-L802)：P2P 分支的实际代码——先检查 `supports_device_p2p()`（XPU 上不支持，会抛出明确错误并建议改用广播）；断言 `_p2p_store` 已初始化；`need_update = self._rank in ranks`，不在目标集合里的 rank 直接返回；随后先执行一次 `dist.barrier(group=ranks_group)` 以避免后续设备 OOM。
- [checkpoint_engine/ps.py:827-832](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L827-L832)：P2P 模式下要把接收 buffer 注册进 `self._p2p_store`（名字固定为 `__ipc_buffer__`），这样其他 rank 才能通过 RDMA 读到它——这是「接收方主动暴露内存」的 P2P 语义。
- [checkpoint_engine/p2p_store.py:11](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L11)：`class P2PStore`——对 mooncake TransferEngine 的封装类（本讲只认识它，第五单元精读）。

#### 4.4.4 代码实践

**实践目标**：从源码层面确认「P2P 与 Broadcast 共用同一个入口，只是分支不同」。

1. 操作步骤：
   - 打开 [checkpoint_engine/ps.py:774-800](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L774-L800)，逐行阅读 `p2p_update` 标志的设置逻辑。
   - 在 `ps.py` 中搜索 `p2p_update`，观察它在后续代码中改变了哪些行为（至少找到第 827-832 行的 buffer 注册处）。
   - 阅读第 782-788 行的错误信息，回答：为什么 XPU 不能用 P2P？
2. 需要观察的现象：`ranks` 是否为空是唯一的模式开关；P2P 分支多了「能力检查 + barrier + 内存注册」三个动作。
3. 预期结果：你能写出一句结论——「Broadcast 与 P2P 不是两个类，而是 `_update_per_bucket` 里由 `ranks` 决定的两条分支，共享 bucket 切分与 ZMQ 控制逻辑」。
4. 待本地验证：无。

#### 4.4.5 小练习与答案

**练习 1**：某推理实例因 OOM 崩溃后重启，应该用哪种更新方式？为什么？
**答案**：P2P（`update(ranks=[重启实例的 rank])`）。因为存量实例仍在服务请求，全量广播会打断它们；P2P 只把权重送到新实例，存量实例仅作为数据提供方。

**练习 2**：为什么 P2P 模式要先执行一次 `dist.barrier(group=ranks_group)`？
**答案**：源码注释写明 "first execute a barrier to avoid subsequent device oom"（[ps.py:801-802](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L801-L802)）——让所有相关 rank 先同步，避免有的进程还没释放/分配好显存时，其他进程的分配把显存占光。

**练习 3**：P2P 模式下，不在 `ranks` 列表里的 PS 进程做什么？
**答案**：直接 `return` 退出 `_update_per_bucket`（`need_update` 为假，见 [ps.py:793-800](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L793-L800)）——它们既不是接收方，也不需要参与这次子通信组的更新流程。

---

### 4.5 性能基准、硬件支持与安装形态

#### 4.5.1 概念说明

读开源项目时，「Benchmark 表 + 安装说明 + 依赖清单」是判断项目成熟度与适用性的三件套。这一节把 README 的基准表和 `pyproject.toml` 的依赖放在一起读。

**性能基准**（README 表格，全部由 `examples/update.py` 实测，推理引擎为 vLLM v0.10.2rc1）：

| 模型 | 设备 | GatherMetas | Update (Broadcast) | Update (P2P) |
| :--- | :--- | :--- | :--- | :--- |
| GLM-4.5-Air (BF16) | 8xH800 TP8 | 0.12s | 3.47s (3.02GiB) | 4.12s (3.02GiB) |
| Qwen3-235B-A22B-Instruct-2507 (BF16) | 8xH800 TP8 | 0.33s | 6.22s (2.67GiB) | 7.10s (2.68GiB) |
| DeepSeek-V3.1 (FP8) | 16xH20 TP16 | 1.17s | 10.19s (5.39GiB) | 11.80s (5.41GiB) |
| Kimi-K2-Instruct (FP8) | 16xH20 TP16 | 1.33s | 14.36s (5.89GiB) | 17.49s (5.91GiB) |
| DeepSeek-V3.1 (FP8) | 256xH20 TP16 | 0.80s | 11.33s (8.00GiB) | 11.81s (8.00GiB) |
| Kimi-K2-Instruct (FP8) | 256xH20 TP16 | 1.22s | 16.04s (8.00GiB) | 16.75s (8.00GiB) |

读表时注意三点（README 的三条注释）：

1. **括号里的 GiB 是 IPC bucket 大小，不是模型总量**——例如 Kimi-K2 是 1T 参数模型，表格里的 8.00GiB 只是单次传输的桶尺寸（上限默认 8GB）。
2. **256 GPU 慢不了多少**：从 16 卡到 256 卡，Broadcast 只从 14.36s 涨到 16.04s（Kimi-K2），说明扩展性来自并行度与流水线，而非串行叠加。
3. **P2P 只比 Broadcast 慢一点**（约 10%~20%），灵活性的代价很小；且表中 P2P 数据是「从集群中更新不超过两个节点（16 GPU），即 `ParameterServer.update(ranks=range(0, 16))`」的场景。

**硬件支持**：CUDA（H800/H20 等，主力平台）、NPU（昇腾，走 HCCL，见 `docs/npu_start.md` 与 `distributed/vllm_hccl.py`）、XPU（Intel GPU，仅支持 Broadcast 路径，需源码安装 + oneAPI `icpx` 运行时 JIT 编译 SYCL 扩展）。

#### 4.5.2 核心流程

安装形态与能力的关系：

```text
pip install checkpoint-engine
        └── 基础依赖：torch>=2.5、fastapi、pydantic、safetensors、pyzmq、uvicorn、
            loguru、numpy、httpx
        └── 能力：Broadcast 更新（CUDA / NPU）

pip install 'checkpoint-engine[p2p]'
        └── 额外安装 mooncake-transfer-engine>=0.3.5
        └── 能力：以上全部 + 基于 RDMA 的 P2P 更新

源码安装（XPU）：git clone + pip install -e .
        └── 需要 Intel XPU 版 PyTorch (torch>=2.9) + oneAPI 2026.0+ (icpx)
        └── 能力：仅 Broadcast（P2P 不支持 XPU 设备内存注册）
```

为什么 P2P 要拆成可选依赖？因为 `mooncake-transfer-engine` 是带原生扩展的重依赖，只有需要 RDMA 的用户才该装——这就是 `pyproject.toml` 里 `optional-dependencies` 的典型用法。

#### 4.5.3 源码精读

- [README.md:45-62](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L45-L62)：基准表及其四条注释——测试脚本、vLLM 版本、FP8 需要补丁、括号内为 bucket 大小、P2P 场景为 `update(ranks=range(0, 16))`、GPU 与 NUMA 绑定。
- [README.md:64-76](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L64-L76)：两种安装命令——基础版只求最快的 Broadcast；`[p2p]` 额外装 mooncake-transfer-engine 以支持 RDMA。
- [README.md:78-98](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L78-L98)：Intel XPU 支持——只支持 Broadcast；跨进程交接用运行时 JIT 编译的 SYCL `ipc_memory` 原生扩展；对 PyTorch（torch>=2.9，XPU 构建）和 oneAPI（2026.0+ 的 `icpx`）的版本要求；以及 `pytest tests/test_xpu_ipc.py` 的验证方式。
- [pyproject.toml:1-18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L1-L18)：项目元数据与基础依赖。注意 `description = "checkpoint-engine is a lightweight, decoupling and efficient weight update middleware"`，以及 `requires-python = ">=3.10"`；依赖里 `torch>=2.5.0` 是唯一有版本下限的重量级依赖，`pyzmq`（ZMQ 协议）、`safetensors`（权重解析）、`fastapi/uvicorn`（HTTP API）、`pydantic`（数据模型）各司其职。
- [pyproject.toml:20-24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L20-L24)：`[project.optional-dependencies]` 的 `p2p` extra——`mooncake-transfer-engine>=0.3.5`，注释说明 `batch_register_memory` 接口从 0.3.5 才引入。
- [pyproject.toml:30-35](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L30-L35)：打包配置里把 `checkpoint_engine.xpu_ipc` 的 `*.cpp` 一起发布——SYCL 扩展源码随包分发、在 XPU 主机上首次使用时才 JIT 编译，这是「XPU 需要源码安装/oneAPI 运行时」在工程上的落点。
- [checkpoint_engine/device_utils.py:199-230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L199-L230)：`DeviceManager._detect_device_type` 依次探测 npu → xpu → cuda，三者都不支持则抛 `TypeError`——「支持 CUDA/NPU/XPU」不是文档口号，而是代码里真实的三路分支。
- [README.md:179-182](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L179-L182)：三个环境变量——`PS_MAX_BUCKET_SIZE_GB`（桶大小上限，默认 8GB）、`PS_P2P_STORE_RDMA_DEVICES`（P2P 用的 RDMA 网卡，缺省回退到解析 `NCCL_IB_HCA`，再缺省则均分所有 RDMA 设备）。

#### 4.5.4 代码实践

**实践目标**：跑通项目自带的「CPU 可运行」测试子集，验证你的环境与项目契约一致；同时亲手算一笔带宽账。

1. 操作步骤：
   - 阅读并执行（需已安装 torch 等依赖）：`pytest tests/ -m "not gpu"`。依据是 [README.md:173-177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L173-L177)：只有 `test_update.py` 需要 GPU，其余测试可在 CPU 上跑。
   - 算账：用基准表第 4 行（Kimi-K2，16xH20，Broadcast 14.36s，bucket 5.89GiB）与第 6 行（256xH20，16.04s，8.00GiB）对比，回答「GPU 数量 ×16，耗时只增加 12%」说明了什么。
2. 需要观察的现象：`pytest` 输出中 `test_update.py` 被跳过/未收集，其余测试通过；`-m "not gpu"` 这个 marker 定义在 [pyproject.toml:166-169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169)。
3. 预期结果：CPU 测试全部通过；你的结论是——耗时近似由「每个 rank 串行处理的桶数 × 单桶时间」决定，rank 越多并行度越高，总时间几乎不随集群规模增长。
4. 待本地验证：`pytest` 的通过情况取决于你本地安装的依赖版本（torch、fastapi 等），若缺少 GPU 或依赖不一致，请以实际输出为准。

#### 4.5.5 小练习与答案

**练习 1**：基准表括号里的 "8.00GiB" 指什么？Kimi-K2 有 1T 参数，为什么只有 8GiB？
**答案**：指 IPC bucket（桶）大小，即单次流水线传输分块的大小，上限由 `PS_MAX_BUCKET_SIZE_GB` 控制（默认 8GB）。权重是按桶分批流水传输的，桶大小 ≠ 模型总数据量。

**练习 2**：想在无 RDMA 网卡的机器上使用 checkpoint-engine，应该装哪个包？能用哪些功能？
**答案**：装基础包 `pip install checkpoint-engine` 即可，使用 Broadcast 更新（CUDA/NPU）。P2P 依赖 `mooncake-transfer-engine`（`[p2p]` extra）走 RDMA，没有 RDMA 环境时无法发挥其作用。

**练习 3**：`requires-python = ">=3.10"`、`torch>=2.5.0`、XPU 上还要求 `torch>=2.9`——这些版本下限分别防什么问题？
**答案**：Python 3.10 是源码使用的语法/类型标注基线；torch 2.5 是基础 API 兼容线；XPU 路径需要 device 的 `.uuid` 属性（torch>=2.9 才有），SYCL 扩展构建还需 torch>=2.7（README L83 有说明）。

## 5. 综合实践

**任务：写一页《checkpoint-engine 速览笔记》，并向同事讲解 3 分钟。**

要求笔记包含以下五块内容，每块都必须附上你自己在源码中定位到的永久链接：

1. **一句话定位**：用你自己的话（不抄 README）说清「它是什么、为谁解决什么问题」，并注明性能量级（1T 参数 / 数千 GPU / 约 20s，引 [README.md:1-4](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L1-L4)）。
2. **两种更新方式对比表**：Broadcast 与 P2P 的触发条件、典型架构、传输机制、对存量实例的影响，各给出一处源码依据（例如 [ps.py:774-800](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L774-L800)）。
3. **三阶段流水线示意图**：手画 H2D → broadcast → reload 的数据流，标注「锁页内存、h2d_buffer、2 倍桶大小的双缓冲、共享 IPC buffer、推理引擎 reload」，并写明显存不足时退化为串行的依据（README L37 与 `disable_h2d_buffer`）。
4. **一条数据流追踪**：从 `ParameterServer.update`（[ps.py:569](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L569)）出发，列出到 worker 侧 `update_weights_from_ipc`（[worker.py:54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L54)）为止你目前能看懂的调用链（看不懂的部分标注「后续单元补充」即可，不要编造）。
5. **环境适配结论**：你的目标硬件属于 CUDA / NPU / XPU 哪一种？应该用哪个安装命令？能不能用 P2P？依据 [device_utils.py:222-230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L222-L230) 与 [ps.py:782-788](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L782-L788) 说明理由。

验收标准：把这页笔记拿给一个没接触过本项目的同事看，对方能在 3 分钟内复述出「两种更新方式的区别」和「为什么需要三阶段流水线」。

## 6. 本讲小结

- checkpoint-engine 是一个**轻量级权重更新中间件**：夹在 RL 训练侧与推理引擎（vLLM/SGLang）之间，专做 inplace 权重热更新；Kimi-K2（1T 参数、数千 GPU）量级下更新约 20s。
- 核心逻辑集中在 `ParameterServer` 类（`checkpoint_engine/ps.py`），生命周期为 `register_checkpoint → gather_metas → update → unregister_checkpoint`，且 `update` 必须在 `gather_metas` 之后调用。
- **Broadcast**（`ranks=None/[]`）是默认最快方式：把传输组织成 H2D → broadcast → reload 三阶段流水线，用双缓冲重叠通信与拷贝；显存不足时自动退化为串行。
- **P2P**（`ranks` 指定）面向动态扩容/重启场景：经 mooncake-transfer-engine 走 RDMA 点对点传输，不干扰存量实例，并通过 bucket 分配优化打满收发双方带宽。
- 性能特征：从 16 GPU 扩到 256 GPU，Broadcast 耗时仅增加约 12%；P2P 比 Broadcast 慢约 10%~20%；表格中的 GiB 是 bucket 大小而非模型总量。
- 硬件支持 CUDA / NPU / XPU 三条路（`DeviceManager._detect_device_type` 依次探测），P2P 在 XPU 上不可用；安装形态分基础包与 `[p2p]` extra 两种。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：动手搭建环境并完成第一次端到端权重更新——`pip install`、启动带 `--worker-extension-cls` 的 vLLM、用 `torchrun` 运行 `examples/update.py`。本讲建立的全局认知将在那一步变成肌肉记忆。
- **再下一讲（u1-l3）**：逐文件精读 `checkpoint_engine/` 包，绘制代码地图；建议提前自己先浏览一遍 `ps.py`、`worker.py`、`pin_memory.py` 的 import 部分。
- **源码预读**（可选，只需看类名和方法签名）：`checkpoint_engine/data_types.py` 中的 `ParameterMeta`、`H2DBucket`、`MemoryBuffer` 等数据模型——它们是第二单元的主角，也是 `gather_metas` 传输的「货物清单」。
- **延伸阅读**：README 中引用的 [Kimi-K2 Technical Report](https://arxiv.org/abs/2507.20534)（三阶段流水线的设计动机）与 [issue #25](https://github.com/MoonshotAI/checkpoint-engine/issues/25)（P2P bucket 分配优化）。
