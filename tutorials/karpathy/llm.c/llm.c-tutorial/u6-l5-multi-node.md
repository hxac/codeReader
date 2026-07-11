# 多节点训练：MPI / TCP / FS 三种初始化

## 1. 本讲目标

本讲承接 u6-l4（单机多卡的 ZeRO 与 NCCL），把训练扩展到**多节点**（多台机器、每台多张 GPU）。学完后你应该能够：

1. 说清楚 NCCL 在多 GPU 通信前必须解决的「引导（bootstrap）」问题，以及 `ncclUniqueId` 在其中扮演的角色。
2. 逐行读懂 `multi_gpu_config_init` 支持的 `mpi` / `tcp` / `fs` 三种初始化路径，理解它们各自如何把同一枚 `ncclUniqueId` 分发到所有进程。
3. 区分三种路径下 `process_rank` / `num_processes` / `local_device_idx` 的来源差异（MPI 自带 vs 命令行 vs SLURM 环境变量）。
4. 看懂 `scripts/multi_node` 下的三个启动脚本，知道各自依赖的运行时环境（mpirun/PMIx、SLURM/srun、共享文件系统）以及 `-pi` 参数的取值。

## 2. 前置知识

- **进程（process）、rank、节点（node）**：本讲把「一张 GPU 上跑的一个 `train_gpt2cu` 实例」称为一个**进程**；每个进程有一个全局编号 `process_rank`（简称 rank），进程总数 `num_processes`；一台物理机器叫一个**节点**，每节点上的 GPU 数叫 `gpus_per_node`。例如 2 个节点、每节点 8 张 GPU，共 16 个进程（rank 0~15）。
- **NCCL**：NVIDIA 的集合通信库，提供 `ncclAllReduce` / `ncclReduceScatter` / `ncclAllGather` 等原语，是 u6-l4 里跨卡梯度合并的底层实现。
- **NCCL 通信子（communicator, `ncclComm_t`）**：所有想互相通信的进程必须先共同「组建」一个通信子，之后才能做集合通信。组建的前提是每个进程都拿到同一枚**唯一 ID**（`ncclUniqueId`），并知道自己在这个组里的 rank。
- **MPI**：Message Passing Interface，一套经典的并行编程标准；它自带一个「世界通信子」`MPI_COMM_WORLD`，天然能把所有由 `mpirun` 拉起的进程编进同一个组并赋予 rank。OpenMPI 是它的一个主流实现。
- **SLURM**：HPC 集群常见的作业调度系统，用 `sbatch` 提交作业、`srun` 在分配到的节点上启动任务，并通过 `SLURM_NTASKS` / `SLURM_PROCID` / `SLURM_NTASKS_PER_NODE` 等环境变量告知任务拓扑。
- **PMIx**：进程间信息交换库，新版的 `slurm-wlm` 包默认关闭了 PMIx，导致 MPI 难以跨节点引导——这正是 llm.c 同时提供 `tcp` / `fs` 两条非 MPI 路径的直接原因（源码注释明说，见 4.1.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [llmc/zero.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh) | 多 GPU/多节点的全部基础设施：`MultiGpuConfig` 结构体、`multi_gpu_config_init`（本讲主角）、三种 `get_nccl_id_via_*`、`multi_gpu_get_local_device_idx`、集合通信与 ZeRO 分片工具。 |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | CUDA 主线。本讲只关注其中三处：命令行参数解析（`-pn/-pr/-pg/-pi/-ps/-pf`）、对 `multi_gpu_config_init` 的调用，以及帮助文本。 |
| [scripts/multi_node/run_gpt2_124M_mpi.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_mpi.sh) | MPI 路径启动脚本，用 `mpirun` 直接拉起，非 SLURM。 |
| [scripts/multi_node/run_gpt2_124M_tcp.sbatch](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_tcp.sbatch) | TCP 路径启动脚本，SLURM 作业（`sbatch` + `srun`），通过 TCP 连接分发 ID。 |
| [scripts/multi_node/run_gpt2_124M_fs.sbatch](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_fs.sbatch) | FS（文件系统）路径启动脚本，SLURM 作业，通过共享文件分发 ID。 |
| [Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile) | 自动探测 NCCL 与 OpenMPI，决定是否定义 `MULTI_GPU` / `USE_MPI` 编译宏——这两者直接决定 `zero.cuh` 里哪些代码会被编进去。 |

---

## 4. 核心概念与源码讲解

### 4.1 核心问题：NCCL 通信子的「引导」与 ncclUniqueId

#### 4.1.1 概念说明

无论用哪种方式，多 GPU 训练在第一次做 `ncclAllReduce` 之前，都必须先解决一个**引导问题**：让所有进程「互相认识」，组建一个 `ncclComm_t` 通信子。

NCCL 的引导采用经典的 **「一个 leader 生成 ID + 广播给所有人 + 各自凭 ID 入组」** 模式：

1. 只有 **rank 0** 调用 `ncclGetUniqueId(&id)` 生成一枚全局唯一的「邀请码」`ncclUniqueId`（一段固定大小的字节串）。
2. rank 0 必须把这枚 ID **可靠地分发**给其它所有 rank。
3. **每个**进程（含 rank 0）用同一枚 ID、自己的 rank、总进程数调用 `ncclCommInitRank`，NCCL 内部据此完成握手，返回一个可用的 `ncclComm_t`。

关键直觉是：**第 2 步「分发 ID」是唯一可以「不依赖 NCCL 自己」的环节**——因为 NCCL 通信子此刻还没建立，只能借用「外部信道」。本讲的三种初始化方式 `mpi` / `tcp` / `fs`，**本质区别就是这第 2 步用的外部信道不同**：MPI 的广播、自建 TCP 连接、共享文件系统。一旦 ID 分发完毕，第 3 步以后三条路径完全合流。

#### 4.1.2 核心流程

三条路径共用一个骨架（伪代码）：

```
function multi_gpu_config_init(num_processes, process_rank, gpus_per_node,
                               server_ip, fs_path, init_method):
    if init_method == "mpi":
        # rank/size 由 MPI 提供，覆盖外部传入值
        MPI_Init(); rank = MPI_Comm_rank(); size = MPI_Comm_size()
        local_device_idx = multi_gpu_get_local_device_idx(rank, size)  # 按主机名归类
        if rank == 0: ncclGetUniqueId(&id)
        MPI_Bcast(id)              # ← 用 MPI 广播作外部信道
    else:  # tcp 或 fs
        rank = process_rank; size = num_processes         # 用命令行传入值
        local_device_idx = rank % gpus_per_node           # 简单取模
        if init_method == "tcp":
            id = get_nccl_id_via_tcp(rank, server_ip)     # ← 用 TCP 连接
        else if init_method == "fs":
            id = get_nccl_id_via_fs(rank, fs_path)        # ← 用共享文件
    # —— 三条路径在此合流 ——
    cudaSetDevice(local_device_idx)
    ncclCommInitRank(&comm, size, id, rank)               # 凭同一枚 id 入组
    创建 nccl_stream、compute_nccl_sync 事件、unified_buffer
    return config
```

注意一个贯穿全讲的对比：

- `mpi` 路径里，`process_rank` / `num_processes` 被 `MPI_Comm_rank/size` **覆盖**，命令行的 `-pn/-pr` 被忽略；`local_device_idx` 由主机名归类算法算出。
- `tcp` / `fs` 路径里，三者全部来自命令行（脚本里再由 SLURM 环境变量填入），`local_device_idx` 用 `rank % gpus_per_node` 简单取模——这隐含假设「rank 是按节点连续分配的」，恰好是 SLURM 默认分配方式。

#### 4.1.3 源码精读

入口函数签名与三条路径的分发逻辑：

[llmc/zero.cuh:411-447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L411-L447) —— `multi_gpu_config_init` 的开头先声明返回结构、读取 `init_method` 字符串。注意第 416 行的注释，作者直接点明了 `tcp`/`fs` 存在的原因：

> `// On newer slurm versions (slurm-wlm package) PMIx is disabled so we can not use MPI for NCCL init in multi node setup`

也就是说：在新版 SLURM 集群上 MPI 跨节点引导会失败，于是 llm.c 额外提供了不依赖 MPI 的两条退路。

三条路径在拿到 `nccl_id` 之后，进入**完全相同**的收尾代码：

[llmc/zero.cuh:448-456](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L448-L456) —— 这段是「合流点」：`cudaSetDevice` 选定本进程用哪张卡，`ncclCommInitRank` 凭同一枚 `nccl_id` 与各自 rank 组建通信子，再创建 u6-l4 里讲过的 `nccl_stream`、`compute_nccl_sync` 事件（用于计算流与通信流之间的依赖同步），以及一个 1 个 float 的 `unified_buffer`（供 `multi_gpu_barrier` / `multi_gpu_cpu_float_sum` 做标量规约）。

`MultiGpuConfig` 结构体本身：

[llmc/zero.cuh:61-79](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L61-L79) —— `process_rank`、`num_processes`、`local_device_idx` 三项是本讲反复出现的「进程拓扑」字段；`zero_stage` / `shard_num_parameters` 承接 u6-l4；`nccl_comm` / `nccl_stream` / `compute_nccl_sync` / `unified_buffer` 四项在 `MULTI_GPU` 编译宏下才存在。

错误检查宏（供后文各路径调用）：

[llmc/zero.cuh:36-42](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L36-L42) —— `ncclCheck` 采用「内联检查函数 + 带 `__FILE__/__LINE__` 的包装宏」两段式，出错即打印定位并 `exit`，与 u5-l2 的 `cudaCheck`/`cublasCheck` 同款思路。`mpiCheck`（[zero.cuh:44-55](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L44-L55)）只在 `USE_MPI` 下编译。

#### 4.1.4 代码实践

**实践目标**：在不开 MPI 的情况下，验证「只有 rank 0 生成 ID」这一约定。

1. 打开 `llmc/zero.cuh`，在 `multi_gpu_config_init` 里分别找到 `mpi`、`tcp`、`fs` 三段，确认三段里 `ncclGetUniqueId` 的调用都出现在 `if (rank == 0)`（或 `process_rank == 0`）的条件下。
2. 解释：如果让每个 rank 都各自 `ncclGetUniqueId`，会拿到不同的 ID，`ncclCommInitRank` 会怎样？答：握手失败、组建不了通信子。
3. 预期结果：三条路径都满足「唯一 ID 由 rank 0 独家生成、再分发」的约定。
4. 待本地验证（无需多卡）：在单机单卡上以 `mpi` 方式跑 `num_processes=1`，观察日志里是否只出现一次 `ncclGetUniqueId` 相关的 NCCL 初始化输出。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `ncclGetUniqueId` 必须在「分发」之前由 rank 0 调用，而不能让各 rank 各自调用后再「对齐」？
  - **答案**：`ncclUniqueId` 是一段随机邀请码，各 rank 独立调用会得到不同的码；NCCL 要求所有进程使用**同一枚**码才能握手成功，因此必须「一处生成、全员共享」。
- **练习 2**：`local_device_idx` 在 `tcp`/`fs` 路径里用 `rank % gpus_per_node`，这个公式在什么 rank 分配方式下才正确？
  - **答案**：当 rank 按节点「连续分块」分配时（节点 0 拿 rank `0..gpus_per_node-1`，节点 1 拿 `gpus_per_node..2*gpus_per_node-1`，…）。SLURM 的默认分配满足此假设，故 sbatch 脚本可以这样用。

---

### 4.2 mpi 方式：复用 MPI_COMM_WORLD 引导

#### 4.2.1 概念说明

`mpi` 方式是**默认方式**（命令行默认 `-pi "mpi"`，见 [train_gpt2.cu:1456](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1456)）。它的思路最省事：既然 `mpirun` 已经把所有进程拉起、并赋予了一个现成的 `MPI_COMM_WORLD`，那就直接借用 MPI 来 (a) 提供 rank/size，(b) 广播 ID，(c) 算出本进程该用哪张 GPU。

代价是：必须编译时链接 MPI（`USE_MPI` 宏），且集群的 `mpirun` 必须能在节点间正常引导（依赖 PMIx 等机制）。在新版 `slurm-wlm` 上这恰恰会失败，于是才需要后两种方式。

#### 4.2.2 核心流程

```
if init_method == "mpi":           # 需要 USE_MPI 已编译
    MPI_Init()
    rank  = MPI_Comm_rank(MPI_COMM_WORLD)   # 覆盖命令行的 -pr
    size  = MPI_Comm_size(MPI_COMM_WORLD)   # 覆盖命令行的 -pn
    local_device_idx = multi_gpu_get_local_device_idx(rank, size)
    if rank == 0: ncclGetUniqueId(&id)
    MPI_Bcast(id, root=0)           # ← 外部信道 = MPI 广播
```

本路径专属的 `multi_gpu_get_local_device_idx`（MPI 才编译）用主机名哈希把「同一节点上的多个进程」归到一起，从而让每个进程拿到 `0..gpus_per_node-1` 之间互不相同的本地 GPU 号。

#### 4.2.3 源码精读

[llmc/zero.cuh:417-430](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L417-L430) —— `mpi` 分支。注意三件事：(1) `MPI_Comm_rank` / `MPI_Comm_size` 的结果写回 `result.process_rank` / `result.num_processes`，**覆盖**了外部传入的命令行值；(2) `ncclGetUniqueId` 仅在 `rank == 0` 调用；(3) 用 `MPI_Bcast(..., root=0, MPI_COMM_WORLD)` 把 ID 广播给所有进程。整个分支被 `#ifdef USE_MPI ... #else #endif` 包裹，若未编译 MPI 却指定 `-pi mpi`，会进入 [zero.cuh:427-430](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L427-L430) 打印「MPI support is disabled」并退出。

[llmc/zero.cuh:368-407](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L368-L407) —— `multi_gpu_get_local_device_idx`，注释说明「同机进程用不同 GPU 号、跨机进程互不影响」，并标注抄自 NCCL 官方示例。算法分三步：(1) `gethostname` 后在第一个 `.` 处截断，做 djb2 式哈希得到一个 `uint64_t`；(2) `MPI_Allgather` 把所有进程的主机名哈希收集到每个进程；(3) 从 rank 0 扫到自己，遇到「哈希等于我、但不是我」的进程就 `local_device_idx++`——即「在我之前、和我同机」的进程数，正是我应使用的本地 GPU 号。

#### 4.2.4 代码实践

**实践目标**：用纸笔跑一遍主机名归类算法。

1. 设两台机器 `node-a`（rank 0,1,2,3）、`node-b`（rank 4,5,6,7），`gpus_per_node=4`。
2. 手算 rank 5 的 `local_device_idx`：扫描 0..5，「哈希等于 node-b 且非自己」的只有 rank 4，所以 `local_device_idx = 1`。
3. 对照源码 [zero.cuh:392-402](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L392-L402) 确认你的推理与代码一致。
4. 预期结果：rank 5 → GPU 1；rank 0 → GPU 0；rank 4 → GPU 0。
5. 待本地验证：若有单机多卡 + OpenMPI 环境，可 `mpirun -np 2 ./train_gpt2cu -pi mpi ...`，观察两个进程是否分别绑定到 GPU 0 和 GPU 1。

#### 4.2.5 小练习与答案

- **练习 1**：为何 `mpi` 路径里命令行 `-pn/-pr` 被忽略？
  - **答案**：`MPI_Comm_size/rank` 返回的值覆盖了它们；MPI 自带的权威拓扑信息更可靠，命令行的值只对 `tcp`/`fs` 路径有意义。
- **练习 2**：主机名哈希为何要在第一个 `.` 处截断？
  - **答案**：集群里完整域名（FQDN）常带后缀如 `node-a.cluster.local`，截断后得到短名 `node-a`，保证同一台机器的各进程算出相同哈希；否则同一主机的不同进程名可能不一致而归错类。

---

### 4.3 tcp 方式：rank 0 自建 TCP 服务器

#### 4.3.1 概念说明

`tcp` 方式完全不依赖 MPI。它自己用 BSD socket 写了一个最小的「服务器-客户端」模型：rank 0 在固定端口 `12345` 起一个 TCP 服务器，其它 rank 作为客户端连过来，rank 0 把 `ncclUniqueId` 通过 TCP 连接发给每个客户端。

适合的场景：集群没有 MPI、或 MPI 跨节点引导失败，但节点间 IP 可达、端口可通。需要额外提供一个 `server_ip`（rank 0 所在机器的 IP），由 `-ps` 传入。

#### 4.3.2 核心流程

```
# rank == 0（服务器端）:
    ncclGetUniqueId(&id)
    socket() → bind(12745 端口) → listen() → accept() 接收 size-1 个客户端
    对每个客户端 send(id)

# rank != 0（客户端）:
    socket() → connect(server_ip:12345)（失败重试 5 次，每次 sleep 2s）
    recv(id)
```

端口 `12345` 硬编码在源码里，作者注释说明它落在「注册端口」区间（1024~49151）。客户端带重试是为了容忍「服务器还没起来」的竞态。

#### 4.3.3 源码精读

[llmc/zero.cuh:229-333](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L229-L333) —— `get_nccl_id_via_tcp`（Linux/POSIX 版；Windows 版 `_windows` 同理，见 [zero.cuh:110-227](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L110-L227)）。服务器端的标准 7 步：`socket` → `setsockopt(SO_REUSEADDR|SO_REUSEPORT)`（便于重启）→ 设地址端口 → `bind` → `listen`（队列上限 `MAX_CLIENTS = size-1`）→ 循环 `accept` 满 `size-1` 个客户端 → `send_nccl_id_to_clients` 发送 ID。客户端 4 步：`socket` → 设地址 → `connect`（重试 5 次）→ `recv`。

[llmc/zero.cuh:99-107](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L99-L107) —— `send_nccl_id_to_clients`，对每个已连接的 socket `send` 整个 `nccl_id`，发完即关。

调用入口：

[llmc/zero.cuh:431-440](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L431-L440) —— 在 `tcp`/`fs` 公共分支里，先用命令行值填好 `process_rank` / `num_processes` / `local_device_idx`，再按 `init_method` 选 `get_nccl_id_via_tcp` 或 `get_nccl_id_via_fs`。

#### 4.3.4 代码实践

**实践目标**：定位 TCP 通信的端口与角色边界。

1. 在 `get_nccl_id_via_tcp` 中找到硬编码端口（提示：`SERVER_PORT = 12345`，[zero.cuh:232](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L232)）。
2. 指出服务器端最多 `accept` 几个客户端（`MAX_CLIENTS = num_processes - 1`）。
3. 指出客户端连接失败时的退避策略（5 次重试、每次 sleep 2 秒，[zero.cuh:293-320](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L293-L320)）。
4. 预期结果：能说清「rank 0 是唯一服务器、其余全是客户端」的拓扑，以及为什么需要重试（rank 0 的服务器可能还没 `listen` 就已有客户端尝试 `connect`）。
5. 待本地验证（无需多机）：在两台互通的机器上分别手动跑 rank 0 与一个 rank 1，`server_ip` 填 rank 0 的 IP，`NCCL_DEBUG=INFO` 观察握手。

#### 4.3.5 小练习与答案

- **练习 1**：为什么客户端要重试 5 次而不是直接失败？
  - **答案**：所有进程几乎同时被拉起，但 rank 0 的 `listen` 不一定先就绪；客户端重试可容忍这种「服务器慢半拍」的竞态，提高启动成功率。
- **练习 2**：`SO_REUSEADDR | SO_REUSEPORT` 解决什么问题？
  - **答案**：让服务器在重启后能立刻重新绑定到仍处于 `TIME_WAIT` 状态的地址/端口，避免「地址已在使用」导致重启失败。

---

### 4.4 fs 方式：共享文件系统当传令兵

#### 4.4.1 概念说明

`fs`（filesystem）方式更朴素：rank 0 把 `ncclUniqueId` 写进共享文件系统上的一个文件，其它 rank 轮询读取该文件。前提是所有节点挂载了**同一个共享文件系统**（NFS、Lustre、GPFS 等），路径对每个节点都可见。

适合的场景：既不想用 MPI，也不方便开 TCP 端口，但有一个共享存储（很多 HPC 集群的 `$HOME` 或 scratch 目录天然满足）。代价是依赖文件系统的可见性与一致性，且轮询有秒级延迟。

#### 4.4.2 核心流程

```
filename = fs_path + "/ncclUniqueId.sync"

if rank != 0: sleep(2)            # 让 rank 0 先写，朴素同步

if rank == 0:
    ncclGetUniqueId(&id)
    fopen(filename, "wb"); fwrite(id); fclose()
else:
    do { sleep(1); idFile = fopen(filename, "rb"); } while (idFile == NULL)
    fread(id); fclose()
```

作者在代码注释里坦白这是一种「naive and not 100% robust」的同步方式——靠 `sleep(2)` + 轮询争取时序，对绝大多数情况够用。

#### 4.4.3 源码精读

[llmc/zero.cuh:336-366](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L336-L366) —— `get_nccl_id_via_fs`。文件名由 `fs_path` 拼上固定后缀 `ncclUniqueId.sync`；非 0 进程先 `sleep(2)` 让出时间窗；rank 0 用 `fwriteCheck` 写入 ID，其它进程在 `do/while` 里每秒 `fopen` 一次直到文件出现，再用 `freadCheck` 读出。`fwriteCheck` / `freadCheck` / `fcloseCheck` 来自 [utils.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L22)（见文件顶部 include），是带检查的文件 IO 包装，与全项目的 `*Check` 风格一致。

调用入口与 `tcp` 共用 [zero.cuh:431-446](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L431-L446)，仅 `init_method == "fs"` 时走 `get_nccl_id_via_fs`，`fs_path` 由命令行 `-pf` 传入（见 4.5.3 的提醒）。

#### 4.4.4 代码实践

**实践目标**：理解文件同步的时序与脆弱点。

1. 在 `get_nccl_id_via_fs` 中找到同步文件名（`<fs_path>/ncclUniqueId.sync`，[zero.cuh:340-341](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L340-L341)）。
2. 解释为什么非 0 进程要 `sleep(2)`、并在 `do/while` 里轮询 `fopen`：因为文件系统的创建与可见有延迟，轮询直到 rank 0 写完。
3. 指出脆弱点：若共享文件系统跨节点可见性延迟超过轮询容忍度，或上一次运行残留了旧文件，可能读到错误 ID（待本地验证：可在共享目录故意预置一个旧 `ncclUniqueId.sync`，观察是否被误读）。
4. 预期结果：能说清「文件即信道」、rank 0 写、其余读的模型。
5. 待本地验证：在有共享目录的两节点上用 `-pi fs -pf /shared/path` 跑通，并删除残留的 `.sync` 文件后再跑一次。

#### 4.4.5 小练习与答案

- **练习 1**：`fs` 方式为什么必须有共享文件系统？没有会怎样？
  - **答案**：文件是分发 ID 的唯一信道；若各节点看不到同一个文件系统，rank 0 写的文件对其它节点不可见，`fopen` 永远返回 NULL，进程死循环在轮询里（或超时失败）。
- **练习 2**：相比 `tcp`，`fs` 的优缺点各是什么？
  - **答案**：优点是不需要开端口、不依赖 IP 路由，配置简单；缺点是依赖共享存储、轮询有秒级延迟、且残留文件可能造成误读，鲁棒性弱于 TCP。

---

### 4.5 进程拓扑参数与三种启动脚本

#### 4.5.1 概念说明

无论走哪条路径，`multi_gpu_config_init` 都需要知道三件拓扑信息：**总进程数、本进程 rank、每节点 GPU 数**。这三者的「来源」因路径而异：

| 信息 | `mpi` 路径 | `tcp`/`fs` 路径 | 命令行参数 |
| --- | --- | --- | --- |
| `num_processes` | `MPI_Comm_size` 覆盖 | SLURM `SLURM_NTASKS` → `-pn` | `-pn` |
| `process_rank` | `MPI_Comm_rank` 覆盖 | SLURM `SLURM_PROCID` → `-pr` | `-pr` |
| `gpus_per_node` | 不直接用（按主机名归类） | SLURM `SLURM_NTASKS_PER_NODE` → `-pg` | `-pg` |
| `local_device_idx` | `multi_gpu_get_local_device_idx` | `rank % gpus_per_node` | —— |
| NCCL-ID 信道 | `MPI_Bcast` | `-ps server_ip` | `-ps` |
| NCCL-ID 信道 | —— | `-pf fs_path` | `-pf` |
| 选哪条路径 | 默认 | 由 `-pi` 指定 | `-pi` |

`scripts/multi_node` 下三个脚本就是这三条路径的「现成配方」，分别面向不同的集群运行时。

#### 4.5.2 核心流程

**mpi 脚本**（裸 `mpirun`，非 SLURM）：
```
make train_gpt2cu USE_CUDNN=1                      # 让 Makefile 自动探测并链接 MPI
mpirun -np 16 --host h1:8,h2:8 ./train_gpt2cu ... -pi mpi
# 不传 -pn/-pr/-pg：MPI 自己提供 rank/size
```

**tcp 脚本**（SLURM 作业）：
```
make train_gpt2cu USE_CUDNN=1 NO_USE_MPI=1         # 故意不链接 MPI
srun ... ./train_gpt2cu ... \
    -pn $SLURM_NTASKS -pr $SLURM_PROCID -pg $SLURM_NTASKS_PER_NODE \
    -ps $server_ip -pi tcp
```

**fs 脚本**（SLURM 作业）：
```
make train_gpt2cu USE_CUDNN=1 NO_USE_MPI=1
srun ... ./train_gpt2cu ... \
    -pn $SLURM_NTASKS -pr $SLURM_PROCID -pg $SLURM_NTASKS_PER_NODE \
    -pf $sync_fs_path -pi fs
```

三者还共享一组 NCCL 性能/网络环境变量（见 4.5.3）。

#### 4.5.3 源码精读

**命令行解析（train_gpt2.cu）**

[train_gpt2.cu:1452-1458](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1452-L1458) —— 多节点参数的默认值：`num_processes=1`、`process_rank=0`、`gpus_per_node=8`、`nccl_init_method="mpi"`、`server_ip=""`、`fs_path=""`。注释写明这三项「应由 slurm 环境提供」。

[train_gpt2.cu:1491-1496](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1491-L1496) —— 解析 `-pi/-pf/-ps/-pn/-pr/-pg`。注意一个**文档与代码不一致**：帮助文本 [train_gpt2.cu:1413](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1413) 把 fs_path 标成 `-pp`、默认 `/tmp`，但真正的解析分支（1492 行 `argv[i][2] == 'f'`）和 `fs.sbatch` 脚本用的都是 `-pf`，默认是空串。**实际可用的是 `-pf`**。帮助文本里同样把 init_method 标成 `-pm`（[1411](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1411)），而真正解析用的是 `-pi`（1491 行）。读脚本和代码、不要只读帮助文本。

[train_gpt2.cu:1504](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1504) —— 把六个值一次性传给 `multi_gpu_config_init`。这是本讲所有逻辑的汇聚点。

**Makefile 的两个关键编译宏**

[Makefile:197-214](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L197-L214) —— 探测系统是否装了 NCCL：用 `dpkg -l | grep nccl` 检查，找到才加 `-DMULTI_GPU` 与 `-lnccl`。`MULTI_GPU` 是 `zero.cuh` 里几乎所有多机代码的总开关（`#ifdef MULTI_GPU`）。

[Makefile:216-230](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L216-L230) —— 探测 OpenMPI：在默认路径 `/usr/lib/x86_64-linux-gnu/openmpi` 下找 include/lib，找到才加 `-DUSE_MPI` 与 `-lmpi`；可用 `NO_USE_MPI=1` 手动关闭。`USE_MPI` 控制 `mpi` 分支与 `mpiCheck`、`multi_gpu_get_local_device_idx` 是否编译。

理解这两个宏就理解了脚本里 `make` 行的差异：`tcp`/`fs` 脚本主动加 `NO_USE_MPI=1`（[tcp.sbatch:14](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_tcp.sbatch#L14)、[fs.sbatch:14](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_fs.sbatch#L14)），把 `mpi` 分支整段编译出去，既减小依赖又避免误用；`mpi` 脚本则不加（[mpi.sh:2](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_mpi.sh#L2)），让 MPI 自然链入。

**三个脚本的关键行**

[run_gpt2_124M_mpi.sh:31-49](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_mpi.sh#L31-L49) —— `mpirun -np 16 --host $host1:8,$host2:8 ...`，用 OpenMPI 的 `--host` 指定两节点各 8 槽，**不传** `-pn/-pr/-pg`（MPI 提供），末尾 `-pi "mpi"`。脚本第 15 行用 `scp` 把二进制拷到 worker 节点（共享文件系统时是 no-op）。

[run_gpt2_124M_tcp.sbatch:1-9](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_tcp.sbatch#L1-L9) —— SLURM 头：`--ntasks=16 --nodes=2 --ntasks-per-node=8 --gres=gpu:8`。[tcp.sbatch:22](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_tcp.sbatch#L22) 设 `server_ip`。[tcp.sbatch:61-84](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_tcp.sbatch#L61-L84) 用 `srun -l -u bash -c "..."` 启动，把 SLURM 环境变量灌进 `-pn/-pr/-pg/-ps/-pi tcp`（注意 `\$SLURM_*` 的反斜杠转义——让变量在**每个 srun 任务里**展开，而非在提交端展开）。

[run_gpt2_124M_fs.sbatch:21](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_fs.sbatch#L21) —— `sync_fs_path=$out_dir`，注释强调「必须是所有节点都能访问的共享文件系统路径」。[fs.sbatch:60-83](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_fs.sbatch#L60-L83) 同样用 `srun`，把 `-pf $sync_fs_path -pi fs` 传进去。

**三者共享的 NCCL/网络环境变量**（三个脚本几乎逐字一致，例如 [mpi.sh:20-29](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_mpi.sh#L20-L29)）：`CUDA_VISIBLE_DEVICES=0..7`、`NCCL_NET_GDR_LEVEL=2`（GPUDirect RDMA，跨节点 GPU 直访免过 CPU）、`NCCL_IB_DISABLE=0`（启用 InfiniBand）、`NCCL_SOCKET_IFNAME=ens17` 与 `OMPI_MCA_btl_tcp_if_include=ens17`（指定网卡，避免走管理网）、`NCCL_P2P_LEVEL=PXB`（同机 GPU 互联拓扑等级）。这些与初始化方式无关，但决定了多机通信的实际带宽。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：对比三个脚本，说清各自的运行时依赖与 `-pi` 取值。这是本讲规格指定的实践任务。

操作步骤：

1. 打开 `scripts/multi_node/` 下的三个文件，分别定位「编译行」「启动器」「拓扑参数来源」「`-pi` 取值」四列。
2. 填写下面的对比表（答案见「预期结果」）。

| 维度 | `run_gpt2_124M_mpi.sh` | `run_gpt2_124M_tcp.sbatch` | `run_gpt2_124M_fs.sbatch` |
| --- | --- | --- | --- |
| 启动器 | `mpirun -np 16 --host ...` | `sbatch` → `srun` | `sbatch` → `srun` |
| 运行时环境依赖 | OpenMPI + PMIx 可跨节点引导 | SLURM + TCP 端口 12345 可达 | SLURM + 共享文件系统 |
| 编译行 | `make ... USE_CUDNN=1` | `make ... USE_CUDNN=1 NO_USE_MPI=1` | `make ... USE_CUDNN=1 NO_USE_MPI=1` |
| rank/size 来源 | `MPI_Comm_rank/size` | `-pr $SLURM_PROCID` / `-pn $SLURM_NTASKS` | 同左 |
| 额外参数 | 无 | `-ps $server_ip` | `-pf $sync_fs_path` |
| **`-pi` 取值** | **`mpi`** | **`tcp`** | **`fs`** |

3. 观察现象：注意 `tcp`/`fs` 脚本里 `srun bash -c "..."` 中 `\$SLURM_*` 的反斜杠——它让这些变量延迟到每个任务执行时才展开，保证每个 rank 拿到自己的 `SLURM_PROCID`。若去掉反斜杠，所有 rank 会拿到提交端的同一个值。
4. 预期结果：能口述「`-pi mpi/tcp/fs` 分别对应 MPI 广播、TCP 服务器、共享文件三种 ID 分发信道，并分别依赖 mpirun、TCP 端口、共享存储」。
5. 待本地验证：若无集群，可在单机用 `mpirun -np 2` 跑 `mpi` 路径做最小验证；`tcp`/`fs` 路径需多机或多进程 + 共享目录，标注为「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `tcp`/`fs` 脚本编译时加 `NO_USE_MPI=1`，而 `mpi` 脚本不加？
  - **答案**：`tcp`/`fs` 不用 MPI，加 `NO_USE_MPI=1` 既避免链接多余的 OpenMPI、又把 `zero.cuh` 的 `mpi` 分支整段编译出去，防止误用；`mpi` 脚本必须链入 MPI 才能走 `MPI_Init/Bcast` 分支，故不加。
- **练习 2**：sbatch 脚本里 `\$SLURM_PROCID` 的反斜杠若删掉会怎样？
  - **答案**：变量会在提交脚本的当前 shell 立即展开成同一个值，导致所有 rank 都认为自己是同一个 `process_rank`，`ncclCommInitRank` 会因 rank 冲突/不匹配而失败。反斜杠确保它在每个 srun 任务里各自展开。
- **练习 3**：帮助文本里写 `-pm`/`-pp`，但实际解析的是 `-pi`/`-pf`。若你只看帮助文本照抄会怎样？
  - **答案**：传 `-pm mpi` 不会被 [train_gpt2.cu:1491](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1491) 识别，会落到 `else { error_usage(); }` 分支（[1501](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1501)）直接报用法错误退出。应始终以脚本和代码为准。

---

## 5. 综合实践

**任务**：为一个新的集群环境选择并改造合适的启动脚本。

背景：假设你拿到一个 4 节点、每节点 8 张 H100 的集群，集群装了 SLURM 但**没装** OpenMPI，且 `/scratch` 是所有节点共享的 Lustre 文件系统，节点间 IP 互通但防火墙只放行了高频端口。

请完成：

1. **选路径**：在 `mpi`/`tcp`/`fs` 中选择一条，并说明理由（提示：无 MPI 排除 `mpi`；防火墙可能挡 12345 端口使 `tcp` 有风险；有共享存储使 `fs` 最稳）。
2. **改脚本**：以 `run_gpt2_124M_fs.sbatch` 为模板，把 `#SBATCH --ntasks`、`--nodes`、`--ntasks-per-node`、`--gres=gpu:N` 改成 4 节点 ×8 卡的拓扑（`--ntasks=32 --nodes=4 --ntasks-per-node=8 --gres=gpu:8`）。
3. **核对参数**：确认 `-pn \$SLURM_NTASKS`、`-pr \$SLURM_PROCID`、`-pg \$SLURM_NTASKS_PER_NODE`、`-pf $sync_fs_path`、`-pi "fs"` 齐全且 `sync_fs_path` 指向共享 `/scratch` 下的某个子目录。
4. **解释分工**：用一句话说明 `srun` 负责「在 4 个节点各起 8 个进程」，而 `-pf/-pi fs` 负责「让这 32 个进程通过共享文件拿到同一枚 `ncclUniqueId` 组建 NCCL 通信子」。
5. **运行后观察**：启动后查看 `.err`/`.log`，确认每个 rank 都打印 `Received NCCL ID`（`fs` 路径无此字样，应看到 NCCL 初始化完成的迹象）且只有 rank 0 打印训练日志（`printf0` 宏的作用，承接 u6-l4）。若 `ncclUniqueId.sync` 残留，应在重跑前删除。
6. **待本地验证**：完整多机运行需真实集群，单机无法复现；可在单机多卡上把节点数设为 1、`-pn` 设为卡数做最小冒烟测试。

## 6. 本讲小结

- NCCL 在做任何集合通信前必须先「引导」出一个 `ncclComm_t`：rank 0 调 `ncclGetUniqueId` 生成邀请码，全员凭同一枚码 + 各自 rank 调 `ncclCommInitRank` 入组。三条初始化路径的**唯一区别**就是「把邀请码分发给所有人」用的外部信道不同。
- `mpi`（默认）复用 `MPI_COMM_WORLD`：`MPI_Comm_rank/size` 提供 rank/size、`MPI_Bcast` 分发 ID、`multi_gpu_get_local_device_idx` 用主机名哈希归类算本地 GPU 号；需编译 `USE_MPI`，且依赖集群 MPI 能跨节点引导（PMIx）。
- `tcp` 不依赖 MPI：rank 0 在端口 12345 自建 TCP 服务器，其余作为客户端连入取 ID；`local_device_idx = rank % gpus_per_node`；需提供 `-ps server_ip`。
- `fs` 最朴素：rank 0 把 ID 写进共享文件 `<fs_path>/ncclUniqueId.sync`，其余轮询读取；依赖共享文件系统，需提供 `-pf fs_path`。
- 拓扑参数 `num_processes`/`process_rank`/`gpus_per_node` 在 `mpi` 路径由 MPI 覆盖，在 `tcp`/`fs` 路径由命令行（脚本再灌入 SLURM 环境变量）提供；实际可用标志是 `-pn/-pr/-pg/-pi/-ps/-pf`（帮助文本里的 `-pm/-pp` 与代码不符，需以代码与脚本为准）。
- 三个脚本对应三种运行时：`run_gpt2_124M_mpi.sh`（裸 `mpirun`、链 MPI、`-pi mpi`）、`run_gpt2_124M_tcp.sbatch`（SLURM/srun、`NO_USE_MPI=1`、`-pi tcp`）、`run_gpt2_124M_fs.sbatch`（SLURM/srun、`NO_USE_MPI=1`、`-pi fs`）；三者共享一套 NCCL 网络/RDMA 优化环境变量。

## 7. 下一步学习建议

- 本讲是 u6「训练工程」单元的收尾。建议回到 `train_gpt2.cu` 的 `main`，把从 `multi_gpu_config_init`（[1504](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1504)）到训练循环的整段串一遍，确认多机配置如何流向 dataloader 分片（[1604-1605](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1604-L1605)）与梯度规约（u6-l4 的 `multi_gpu_async_reduce_gradient`）。
- 进入 u7 单元：先读 [dev/cuda](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda) 的多版本内核库（u7-l1），理解同一层 kernel 的多种优化写法；再读 [profile_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu)（u7-l3）学习用 Nsight Compute 剖析多机训练里的通信/计算瓶颈。
- 若对分布式细节感兴趣，可扩展阅读 NCCL 官方文档的「Bootstrap」与「Environment Variables」章节，对照本讲的三条路径与 `NCCL_NET_GDR_LEVEL`/`NCCL_SOCKET_IFNAME` 等变量加深理解。
