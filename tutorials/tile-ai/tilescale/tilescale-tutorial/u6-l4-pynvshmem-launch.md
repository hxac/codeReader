# 分布式运行时：pynvshmem 与启动

## 1. 本讲目标

本讲承接 u6-l2（NVSHMEM 设备端原语）和 u6-l3（CP-engine 设备端原语）。前面两讲讲的都是 **kernel 内部（device 侧）** 的远程通信原语，本讲往上走一层，回答一个更前置的问题：

> 一个 TileScale 分布式程序在 **启动时** 要做哪些事？多个 GPU 进程是怎么被拉起来、怎么互相发现、怎么为远程通信准备好显存的？

学完后你应当能够：

1. 说清 `init_distributed` 读取哪些环境变量、返回什么、它和「老版本」`init_dist` 的区别。
2. 理解 **NVSHMEM 路线** 与 **CP-engine 路线** 在「主机端运行时准备」上的根本差异——这是本讲最关键的认知。
3. 读懂 `pynvshmem` 这个主机端扩展：它如何用 uniqueid 引导 NVSHMEM、如何创建「对称堆张量」、如何做主机侧 barrier / 信号。
4. 看懂 `launch.sh`：它用 `torchrun` 干了什么、设置了哪些关键环境变量。
5. 用 `perf_fn` 做分布式延迟测量，并按 `example_allgather.py` 的范式做端到端正确性校验。

## 2. 前置知识

在进入本讲前，请确保理解以下概念（前几讲已建立，这里只做最小回顾）：

- **PE / rank**：在 TileScale 里二者同义，指「一个参与分布式通信的处理单元」，实际就是一个被拉起的 GPU 进程。一个进程管一张（或一组）卡。
- **SPMD（单程序多数据）**：所有进程跑的是 **同一份代码**，靠 `rank` 不同走不同的数据分片。TileScale 的 SPMD 发生在「进程之间」，而不是单次 `T.Kernel` 启动内部（这是 u6-l1 的核心结论）。
- **对称堆（symmetric heap）**：所有 PE 分配 **同构、同偏移** 的显存堆，使得「本地偏移 + 目标 PE 基址」就能算出远程地址（u6-l2）。
- **两条设备原语路线**：
  - NVSHMEM 路线：`T.get_pe()` / `T.putmem_nbi_block` / `T.barrier_all`（u6-l2）。
  - CP-engine 路线：`T.get_rank()` / `T.put_block` / `T.wait_eq`（u6-l3）。

本讲要建立的核心新认知是：**你在 device 侧（kernel 里）用哪一族原语，决定了你在 host 侧（启动脚本里）必须做哪一套运行时准备。** 二者必须配对，配错了 kernel 就拿不到正确的远程地址。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tilelang/distributed/utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py) | 分布式主机运行时工具集：`init_distributed` / `init_dist`、IPC 张量、`dist_print`、`perf_fn`、信号辅助函数。 |
| [tilelang/distributed/launch.sh](https://github.com/tile-ai/tilescale/blob/4704282a7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh) | 多进程启动脚本，封装 `torchrun` 并设置一整套分布式环境变量。 |
| [tilelang/distributed/pynvshmem/python/pynvshmem/\_\_init\_\_.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py) | pynvshmem 的 Python 包入口：`init_nvshmem_by_uniqueid`、信号函数、NVSHMEM 枚举（Team/CmpType/Amo）。 |
| [tilelang/distributed/pynvshmem/src/pynvshmem.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc) | pynvshmem 的 C++ 扩展（pybind11）：`nvshmem_create_tensor`、`nvshmem_barrier_all`、uniqueid、RMA 等。 |
| [examples/distributed/example_allgather.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py) | 端到端范例：allgather 的 TileLang 实现 + torch 参考对比 + 正确性校验。 |
| [tilelang/utils/allocator.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py) | CP-engine 路线的分配器 `get_allocator`，用于对照两条路线。 |
| [tilelang/distributed/testing/sync/test_barrierall_sys.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/testing/sync/test_barrierall_sys.py) | CP-engine 路线的端到端测试范例，用于对照。 |

---

## 4. 核心概念与源码讲解

### 4.1 init_distributed：进程组初始化与两套运行时路线

#### 4.1.1 概念说明

任何一个 TileScale 分布式程序，第一件事都是 **建立进程组**。这一步借助 PyTorch 的 `torch.distributed`（后端用 NCCL）完成两件事：

1. **互相发现**：各进程通过一个共同地址（rendezvous）交换信息，确认「我们一共 N 个进程，我是第 rank 个」。
2. **建立通信子组**：通常是覆盖全部进程的 `TP_GROUP`（tensor-parallel group），必要时再建一个只覆盖本机进程的 `LC_GROUP`（local group）。

但仅有 `torch.distributed` 还不够——device 侧的远程原语需要的是 **GPU 显存层面的远程寻址能力**，这要靠 NVSHMEM 或 IPC 机制额外初始化。于是产生了两条「主机端运行时路线」，它们对应 device 侧的两族原语：

| 维度 | NVSHMEM 路线（u6-l2） | CP-engine 路线（u6-l3） |
| --- | --- | --- |
| 启动函数 | `init_distributed(init_nvshmem=True)`（默认） | `init_dist(local_rank, num_local_ranks)`（不初始化 NVSHMEM） |
| 远程寻址机制 | NVSHMEM **对称堆**：同偏移即同逻辑地址 | **远程基址表**：`remote_addr = base[peer] + (addr − base[me])` |
| 远程基址表如何注入 | 不需要——对称堆自动寻址 | `tilelang.get_allocator(...)` 建表 → `kernel.initialize(allocator=...)` 写入 device `meta_data` |
| 张量来源 | `pynvshmem.nvshmem_create_tensor(...)`（对称堆） | `tilelang.tensor(..., allocator=allocator)`（cudaMalloc + IPC handle） |
| device 原语 | `T.get_pe()` / `T.get_pe_num()` / `T.putmem_*` | `T.get_rank()` / `T.get_num_ranks()` / `T.put_block` / `T.get_block` / `T.wait_*` |
| 同步 | `pynvshmem.nvshmem_barrier_all()` 或 device `T.barrier_all()` | `kernel.initialize` 注入的表 + device `T.wait_*` |
| 代表示例 | `example_allgather.py` | `test_barrierall_sys.py` |

> **一句话记忆**：用 `get_pe`/`putmem` 走对称堆（要 init NVSHMEM），用 `get_rank`/`put_block` 走基址表（要 allocator + initialize）。两者选其一，不要混用。

#### 4.1.2 核心流程

`init_distributed`（torchrun 友好版，配合 `launch.sh`）的启动流程：

```text
读取环境变量 WORLD_SIZE / RANK / LOCAL_RANK
        │
        ▼
torch.distributed.init_process_group(backend="nccl", device_id=cuda:LOCAL_RANK, ...)
        │  （rendezvous：所有进程在此互相发现）
        ▼
torch.cuda.set_device(LOCAL_RANK)
        │
        ▼
TP_GROUP = new_group(全部 rank)            ← 覆盖所有进程的 NCCL 子组
        │
        ▼
(可选) pynvshmem.init_nvshmem_by_uniqueid(TP_GROUP)   ← 仅 NVSHMEM 路线需要
        │
        ▼
(可选) LC_GROUP = new_group(本机 rank)     ← 仅 return_lc_group=True 时
        │
        ▼
返回 (WORLD_SIZE, RANK, LOCAL_RANK[, TP_GROUP[, LC_GROUP]])
```

返回值个数由参数控制：默认返回三元组，`return_tp_group=True` 返回四元组（多了 `TP_GROUP`），`return_lc_group=True` 返回五元组（多了 `LC_GROUP`）。

#### 4.1.3 源码精读

`init_distributed` 的完整定义在 [tilelang/distributed/utils.py:66-97](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L66-L97)，关键点逐段说明：

- **读环境变量**（[L67-69](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L67-L69)）：`WORLD_SIZE`/`RANK`/`LOCAL_RANK` 全部来自环境变量——这正是 `launch.sh` 用 `torchrun` 注入的。注意 `torchrun` 会自动给每个子进程设好这三个变量，所以代码里直接 `os.environ.get` 即可。
- **初始化进程组**（[L71-77](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L71-L77)）：后端固定 NCCL，`device_id` 绑到本卡，超时 1800 秒。
- **建 TP_GROUP**（[L80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L80)）：覆盖 `range(WORLD_SIZE)` 全部 rank，这是后续 NVSHMEM 引导与 `dist.barrier` 用的主子组。
- **可选初始化 NVSHMEM**（[L82-86](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L82-L86)）：`init_nvshmem` 默认 `True`，调 `pynvshmem.init_nvshmem_by_uniqueid(TP_GROUP)`（4.2 详述）。**这是 NVSHMEM 路线与 CP-engine 路线的分水岭**。
- **可选 LC_GROUP**（[L88-93](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L88-L93)）：当 `return_lc_group=True` 时，按 `LOCAL_WORLD_SIZE` 算出本机 rank 区间 `[base, base+local_world_size)`，建一个只含本机进程的子组——多机训练里用于节点内通信。

而「老版本」`init_dist` 在 [tilelang/distributed/utils.py:42-63](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L42-L63)，区别在于：

- 它 **不读 `WORLD_SIZE`/`RANK` 当全局 rank**，而是接收 `local_rank`、`num_local_ranks` 作为参数，再用 `MASTER_ADDR`/`MASTER_PORT`/`NODES`/`NODE_RANK` 算出全局 `world_size = num_nodes * num_local_ranks`、`rank = node_rank * num_local_ranks + local_rank`（[L52-53](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L52-L53)）。
- 用 `tcp://ip:port` 作为 `init_method`（[L51](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L51)），而不是 torchrun 自动管理的 rendezvous。
- 不初始化 NVSHMEM，常配合 `torch.multiprocessing.spawn` 用，是 CP-engine 路线测试（如 `test_barrierall_sys.py`）的启动方式。

#### 4.1.4 代码实践

**实践目标**：亲手对比两条路线的「启动 + 张量准备」差异，建立肌肉记忆。

**操作步骤**（源码阅读型，无需多卡）：

1. 打开 `examples/distributed/example_allgather.py`，定位 [L47](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L47) 的 `init_distributed(return_tp_group=True)`，确认它没有调用 `get_allocator` / `kernel.initialize`——因为走对称堆。
2. 打开 `tilelang/distributed/testing/sync/test_barrierall_sys.py`，定位 [L42-47](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/testing/sync/test_barrierall_sys.py#L42-L47)：它用 `init_dist` + `tilelang.get_allocator` + `kernel.initialize(allocator=allocator)`。
3. 列一张对照表：两个示例各自用了哪个 `init_*`、是否建 allocator、是否调 `kernel.initialize`、device 原语是 `get_pe` 还是 `get_rank`。

**需要观察的现象**：NVSHMEM 路线「启动更重（要 init NVSHMEM）但张量准备更简单（一个 `nvshmem_create_tensor` 搞定）」；CP-engine 路线「启动更轻（不 init NVSHMEM）但要手动建 allocator、注入基址表」。

**预期结果**：你能不查资料地说出「看到 `T.get_pe` 就知道 host 侧一定调过 `init_nvshmem_by_uniqueid`，看到 `T.get_rank` 就知道 host 侧一定调过 `kernel.initialize(allocator=...)`」。

#### 4.1.5 小练习与答案

**练习 1**：`init_distributed` 默认返回三元组 `(WORLD_SIZE, RANK, LOCAL_RANK)`，但 `example_allgather.py` 里写的是 `WORLD_SIZE, RANK, LOCAL_RANK, TP_GROUP = init_distributed(...)`。它传了哪个参数？

> **答案**：传了 `return_tp_group=True`，所以返回四元组，多出 `TP_GROUP`，用于后续 `dist.all_gather_into_tensor(..., group=TP_GROUP)` 和 `dist.barrier(TP_GROUP)`。

**练习 2**：为什么 `init_distributed` 里 `init_nvshmem` 默认是 `True`，而 CP-engine 路线根本不用 NVSHMEM？

> **答案**：因为 `init_distributed` 是为「torchrun + 对称堆」这条主路线设计的，绝大多数分布式示例（allgather/all2all/summa）都走 NVSHMEM 路线。CP-engine 路线用的是另一套启动函数 `init_dist`（不触发 NVSHMEM 初始化），二者各管一摊。

**练习 3**：`return_lc_group=True` 时，`LC_GROUP` 覆盖哪些 rank？

> **答案**：只覆盖本机（本节点）的进程，区间为 `[base, base + local_world_size)`，其中 `base = (RANK // LOCAL_WORLD_SIZE) * LOCAL_WORLD_SIZE`（[utils.py:89-91](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L89-L91)）。多机场景下用于区分「节点内」与「跨节点」通信。

---

### 4.2 pynvshmem 主机端 API 与对称堆张量

#### 4.2.1 概念说明

`pynvshmem` 是 TileScale 自带的 NVSHMEM Python 绑定（C++ 扩展 + Python 包），提供 **主机侧** API。它解决三件事：

1. **引导 NVSHMEM 运行时**：让 N 个 GPU 进程组成一个 NVSHMEM 「世界」，确立每个进程的 PE 号。
2. **在 symmetric heap 上分配张量**：用 `nvshmem_malloc` 分配的显存天然是对称的，device 线程据此做 one-sided 远程访存。
3. **主机侧同步与信号**：在 host 上发 barrier、写信号字，配合 device 侧的 `putmem_signal` / `signal_wait_until`。

为什么要单独做一层 `pynvshmem` 而不直接用 NVSHMEM 的 C API？因为 TileScale 的张量容器是 **PyTorch tensor**，需要把 `nvshmem_malloc` 返回的裸指针包装成 `torch.Tensor`，并在 tensor 析构时自动 `nvshmem_free`——这就是 `nvshmem_create_tensor` 的核心工作。

#### 4.2.2 核心流程

**引导流程**（`init_nvshmem_by_uniqueid`）：

```text
rank 0:  nvshmemx_get_uniqueid()        → 得到 128 字节唯一 ID
         (其他 rank 预留同等大小缓冲)
                │
                ▼  broadcast_cpu（经 NCCL 把 uniqueid 广播给所有 rank）
所有 rank 拿到同一份 uniqueid
                │
                ▼
nvshmemx_init_attr_with_uniqueid(rank, nranks, unique_id)
                │  （NVSHMEM 内部完成 bootstrap：所有 PE 互联）
                ▼
nvshmem_barrier_all()  →  引导完成，此后 nvshmem_my_pe() 可用
```

**对称堆张量创建**（`nvshmem_create_tensor`）：

```text
nvshmem_malloc(size)  →  在本 PE 的 symmetric heap 分配，返回对称指针 ptr
                │
                ▼
at::from_blob(ptr, shape, deleter=λ{ nvshmem_free(ptr); }, options=cuda)
                │  （包装成 torch.Tensor，析构时自动 nvshmem_free）
                ▼
返回的 tensor 看起来是普通 GPU tensor，但它「落在对称堆上」
→ 任意 PE 的 device 线程都能用 (ptr, peer) 远程寻址访问
```

#### 4.2.3 源码精读

**Python 侧入口** `init_nvshmem_by_uniqueid` 在 [tilelang/distributed/pynvshmem/python/pynvshmem/\_\_init\_\_.py:52-65](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L52-L65)：

- rank 0 调 `nvshmemx_get_uniqueid()` 拿到唯一 ID（[L54-56](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L54-L56)），其余 rank 预留 128 字节空 buffer（[L57-58](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L57-L58)）。
- 经 `broadcast_cpu`（[L42-49](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L42-L49)，借 NCCL 把 cpu tensor 广播出去）让所有 rank 拿到同一份 uniqueid（[L60](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L60)）。
- 调 `nvshmemx_init_attr_with_uniqueid(rank, nranks, unique_id)` 完成真正的 NVSHMEM 初始化（[L63](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L63)），再 `nvshmem_barrier_all()` 确保所有 PE 就绪（[L64](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L64)）。
- 这里的 `rank`/`nranks` 来自传入的 `torch.distributed.ProcessGroup`（`group.rank()` / `group.size()`），所以 NVSHMEM 的 PE 号与 torch 的 rank 号一致——这就是为什么 device 侧 `T.get_pe()` 与 host 侧 `RANK` 对得上。

**C++ 侧** `nvshmem_create_tensor` 在 [tilelang/distributed/pynvshmem/src/pynvshmem.cc:85-113](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L85-L113)：

- 先 `check_nvshmem_init()`（[L87](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L87)，[L71-73](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L71-L73)），保证 NVSHMEM 已初始化。
- 算字节数 `size`（[L91-93](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L91-L93)），调 `nvshmem_malloc(size)` 在对称堆分配（[L96](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L96)）。
- 用 `at::from_blob(ptr, shape, deleter, options)` 把裸指针包装成 torch tensor（[L98-112](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L98-L112)）。**关键**是那个 deleter lambda：tensor 被 Python GC 回收时，会自动同步设备并 `nvshmem_free(ptr)`——所以你像用普通 tensor 一样用对称堆 tensor，生命周期由 torch 管理。

C++ 侧还注册了 [nvshmem_barrier_all（L246-249）](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L246-L249)、`nvshmem_my_pe`、`nvshmem_n_pes`、`nvshmem_malloc/free`、RMA（putmem/getmem 及其 on_stream 变体）、uniqueid 等一组主机侧函数（见 [PYBIND11_MODULE L171+](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L171-L303)）。

Python 包还在 [\_\_init\_\_.py:117-175](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L117-L175) 暴露了三个枚举，对应 NVSHMEM 的 C 枚举：

- `Team`（[L120-135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L120-L135)）：NVSHMEM 的通信子组（WORLD/NODE/SAME_GPU…），device 侧 team 类操作要用。
- `CmpType`（[L138-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L138-L145)）：signal 的比较类型（EQ/NE/GT/…），对应 device 侧 `signal_wait_until` 的判定。
- `Amo`（[L148-175](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L148-L175)）：原子内存操作类型（SIGNAL_SET/SIGNAL_ADD/FETCH_ADD…），对应 device 侧 `signal_op` 的 `sig_op` 参数。

此外，[\_\_init\_\_.py:71-114](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L71-L114) 的 `write32_on_stream` / `write64_on_stream` 是 **主机侧信号写入**：在指定 stream 上原子地往一个 32/64 位 tensor 写值，是 device 侧 `signal_wait_until` 的「生产者」配套（host 写信号 → device 自旋等信号）。

> **构建前提**：`pynvshmem` 不是开箱即用的。按 [examples/distributed/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/README.md) 的说明，需先用 `build_nvshmem.sh` 编译 NVSHMEM 库（默认从 `3rdparty/nvshmem_src` 取源码、下载 `nvshmem_src_3.2.5-1`，见 [build_nvshmem.sh:21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/build_nvshmem.sh#L21)，并开启 `NVSHMEM_TORCH_SUPPORT=1` 即 [L83](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/build_nvshmem.sh#L83)），再到 `pynvshmem/` 目录 `python setup.py install`，并把库路径加进 `LD_LIBRARY_PATH`。`import _pynvshmem` 失败时会提示「请把 NVSHMEM 库路径加到 LD_LIBRARY_PATH」（[\_\_init\_\_.py:20-28](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L20-L28)）。

#### 4.2.4 代码实践

**实践目标**：验证 NVSHMEM 引导后 `PE 号 == torch rank`，并理解对称堆张量的「普通 tensor 外观」。

**操作步骤**（需多卡，参考 [tilelang/distributed/pynvshmem/testing/python/test_nvshmem_query.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/testing/python/test_nvshmem_query.py)）：

1. 用 `launch.sh` 启动该测试（≥2 卡）。
2. 阅读其 [L26-29](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/testing/python/test_nvshmem_query.py#L26-L29)：`init_nvshmem_by_uniqueid(TP_GROUP)` 之后断言 `nvshmem_my_pe() == RANK`、`nvshmem_n_pes() == WORLD_SIZE`。
3. 在自己的脚本里加一行 `t = pynvshmem.nvshmem_create_tensor([1024], torch.float16)`，打印 `t.dtype`、`t.device`、`t.shape`——它看起来与普通 tensor 无异。

**需要观察的现象**：`nvshmem_my_pe()` 的返回值与 `os.environ["RANK"]` 完全相等。

**预期结果**：测试输出 `Test for basic queries passed!✅`。如果 `nvshmem_my_pe() != RANK`，说明引导时传入的 ProcessGroup 与实际进程布局不一致。

> 若无多卡环境，本步骤为「待本地验证」。可退化为源码阅读：解释为什么 `init_nvshmem_by_uniqueid` 里 `group.rank()` 直接当 NVSHMEM 的 rank 用——因为 NCCL 进程组与 NVSHMEM 世界共用同一套 rank 编号。

#### 4.2.5 小练习与答案

**练习 1**：`nvshmem_create_tensor` 返回的 tensor，在 Python 层被 `del` 时会发生什么？

> **答案**：torch 的引用计数归零触发 `at::from_blob` 注册的 deleter lambda（[pynvshmem.cc:100-111](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/src/pynvshmem.cc#L100-L111)）：先 `device_synchronize`，再 `nvshmem_free(ptr)`，再同步一次。对称堆内存被正确归还，无需手动 free。

**练习 2**：为什么 `init_nvshmem_by_uniqueid` 要先广播 uniqueid，而不是每个进程各自生成？

> **答案**：NVSHMEM bootstrap 要求 **所有 PE 持有同一份 uniqueid** 才能互相认证、建立连接。uniqueid 由 rank 0 生成（`nvshmemx_get_uniqueid`），必须广播给其他 rank，这样大家用的是同一个「会话凭证」。靠 NCCL 的 `broadcast_cpu` 完成这次 CPU 侧广播。

**练习 3**：`write32_on_stream` 与 device 侧 `signal_wait_until` 是什么关系？

> **答案**：前者是 **主机侧生产者**——在某个 stream 上原子地把一个 32 位值写进信号 tensor；后者是 **device 侧消费者**——GPU 线程自旋等待该信号满足比较条件（由 `CmpType` 决定）。二者配合实现「host 通知 device / 远程 PE 通知本地 device」的同步。注意 `write32_on_stream` 要求 tensor 是 `int32`/`uint32` 单元素（[\_\_init\_\_.py:79-82](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/pynvshmem/python/pynvshmem/__init__.py#L79-L82)）。

---

### 4.3 launch.sh：多进程启动与环境变量编排

#### 4.3.1 概念说明

`launch.sh` 是 TileScale 分布式程序的「一键启动器」。它的核心只有一句话：**用 `torch.distributed.run`（即 `torchrun`）按你指定的卡数，给每张卡拉起一个进程，并注入一整套分布式环境变量。**

为什么需要 torchrun？因为 SPMD 模型要求「N 个进程跑同一份脚本」，而每个进程需要知道自己是谁（`RANK`）、一共多少兄弟（`WORLD_SIZE`）、用哪张卡（`LOCAL_RANK`）。torchrun 负责这套 rendezvous 与环境变量注入；`launch.sh` 在此之上再补齐 NVSHMEM/NCCL 需要的运行时开关。

#### 4.3.2 核心流程

```text
launch.sh your_script.py [args...]
        │
        ├─ 设置环境变量（NVSHMEM/NCCL/TileScale 开关）
        │
        ├─ 解析启动规模：nproc_per_node(=GPUS)、nnodes(=NODES)、node_rank(=NODE_RANK)
        │
        ├─ 解析 rendezvous 地址：master_addr(:master_port)
        │
        ▼
torchrun --node_rank --nproc_per_node --nnodes [--rdzv_endpoint] your_script.py [args...]
        │  torchrun 给每个子进程注入 RANK/WORLD_SIZE/LOCAL_RANK/LOCAL_WORLD_SIZE
        ▼
每个子进程 import your_script → init_distributed() 读到这些环境变量 → 建进程组 → init NVSHMEM
```

关键：`launch.sh` 不直接传 `--nnode_rank` 这类参数给 `init_distributed`，而是 **全部经环境变量中转**——torchrun 设 `RANK/WORLD_SIZE/LOCAL_RANK`，`launch.sh` 设 `GPUS/NODES/NODE_RANK/MASTER_ADDR` 等，`init_distributed` 与 `init_dist` 各取所需。

#### 4.3.3 源码精读

**环境变量设置**（[launch.sh:4-8](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L4-L8)）：

| 变量 | 作用 |
| --- | --- |
| `TILELANG_USE_NVSHMEM=1` | 打开 TileLang 的分布式/NVSHMEM 编译模式（device codegen 会 emit nvshmem 调用、链接 device 库） |
| `TILELANG_USE_DISTRIBUTED=1` | 同上，分布式总开关 |
| `NVSHMEM_BOOTSTRAP_MPI_PLUGIN=nvshmem_bootstrap_torch.so` | 让 NVSHMEM 用 **torch bootstrap 插件**（复用 torch rendezvous，免去单独配 MPI/SSH） |
| `NVSHMEM_DISABLE_CUDA_VMM=1` | 关闭 NVSHMEM 的 CUDA VMM（虚拟内存管理），从 C++ 侧挪到 shell 侧设置 |
| `CUDA_DEVICE_MAX_CONNECTIONS=1` | 限制每设备只用 1 条连接——NVSHMEM 推荐配置，避免多连接交错带来的性能抖动（具体收益待本地验证） |

**启动规模解析**（[L19-21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L19-L21)）：

- `nproc_per_node` 默认取本机 GPU 数（`nvidia-smi --list-gpus | wc -l`），可用 `GPUS` 覆盖。
- `nnodes` 默认 1，可用 `NODES` 覆盖。
- `node_rank` 默认 0，可用 `NODE_RANK` 覆盖。

**rendezvous 地址**（[L23-29](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L23-L29)）：`master_addr` 默认 `127.0.0.1`（单机），若设了 `ARNOLD_WORKER_0_HOST`（某训练平台的 worker 主机变量）则用它；端口默认 `8361`。最终拼成 `--rdzv_endpoint=${master_addr}:${master_port}`。

**torchrun 命令**（[L40-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L40-L45)）：

```bash
python -m torch.distributed.run \
  --node_rank=${node_rank} \
  --nproc_per_node=${nproc_per_node} \
  --nnodes=${nnodes} \
  ${TILELANG_EXTRA_TORCHRUN_ARGS} ${additional_args} $@
```

注意 `$@` 把你要跑的脚本及其参数原样透传；`TILELANG_EXTRA_TORCHRUN_ARGS` 留作注入额外 torchrun 参数的逃生口。

**可选内存检查**（[L47-49](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L47-L49)）：设 `MEMCHECK=1` 时，用 `compute-sanitizer --tool memcheck` 包住整条命令，调试 CUDA 越界/未对齐（注释里专门提到对 TMA 相关问题尤其有用）。

NCCL 侧的变量（[L11-15](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L11-L15)、[L33-34](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L33-L34)）多为 InfiniBand/RoCE 调优（`NCCL_IB_TIMEOUT`、`NCCL_IB_GID_INDEX`、`NVSHMEM_IB_GID_INDEX`），单机多卡可先忽略。

#### 4.3.4 代码实践

**实践目标**：在不真正多卡运行的前提下，看清 `launch.sh` 会把哪条命令交给 shell。

**操作步骤**：

1. 在仓库根目录执行（**dry run**，仅看命令，不真跑）：
   ```bash
   GPUS=2 NODES=1 bash -x tilelang/distributed/launch.sh examples/distributed/example_allgather.py --M 8192 --N 12288 2>&1 | grep -A3 "torch.distributed.run"
   ```
   （`bash -x` 会打印每条命令；这里我们只关心最终那条 `torchrun`。）
2. 对照 [L40-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L40-L45)，确认 `--nproc_per_node` 等于你设的 `GPUS`。
3. 设想 `torchrun` 启动了 2 个子进程，每个子进程的环境里 `LOCAL_RANK` 分别是 0 和 1，`RANK` 分别是 0 和 1，`WORLD_SIZE` 是 2——这正是 `init_distributed` 要读的三个变量。

**需要观察的现象**：打印出的命令形如 `python -m torch.distributed.run --node_rank=0 --nproc_per_node=2 --nnodes=1 --rdzv_endpoint=127.0.0.1:8361 examples/distributed/example_allgather.py --M 8192 --N 12288`。

**预期结果**：你能解释「`launch.sh` 的全部启动规模信息都靠环境变量传递，脚本本身不感知 rank」。

> 注意：本步只观察命令拼接，不实际启动 kernel。实际多卡运行见 4.4 综合实践。

#### 4.3.5 小练习与答案

**练习 1**：`launch.sh` 里 `nproc_per_node=${GPUS:=$(nvidia-smi --list-gpus | wc -l)}` 这行用了 shell 的 `${VAR:=default}` 语法，它是什么含义？

> **答案**：`:=` 表示「若 `GPUS` 未设或为空，则把它赋为默认值（本机 GPU 数）并使用」。所以不设 `GPUS` 时默认用全部卡；`export GPUS=2` 可覆盖。`NODES`、`NODE_RANK` 同理。

**练习 2**：为什么 `NVSHMEM_BOOTSTRAP_MPI_PLUGIN` 要设成 `nvshmem_bootstrap_torch.so`？

> **答案**：NVSHMEM 启动时需要一个 bootstrap 机制让各 PE 互联。默认可能是 MPI 或 SSH，但 TileScale 已经用 torchrun + NCCL 完成了 rendezvous。`nvshmem_bootstrap_torch.so` 这个插件让 NVSHMEM **复用 torch 的进程组/bootstrap**，避免再单独配一套 MPI 或 SSH——这也是 `init_nvshmem_by_uniqueid` 能直接拿 `torch.distributed.ProcessGroup` 当参数的底层原因。

**练习 3**：`init_distributed` 读 `RANK/WORLD_SIZE/LOCAL_RANK`，但 `launch.sh` 里并没有显式 `export RANK=...`。这些值是谁设的？

> **答案**：是 `torchrun`（`torch.distributed.run`）在 fork 每个子进程时 **自动注入** 的。`launch.sh` 只负责把规模信息（`--nproc_per_node` 等）告诉 torchrun，具体的 per-rank 环境变量由 torchrun 计算。所以脚本里直接 `os.environ.get("RANK")` 就能拿到。

---

### 4.4 perf_fn 与端到端正确性校验

#### 4.4.1 概念说明

分布式 kernel 写完，要做两件事：**测延迟** 和 **验正确性**。TileScale 在 [utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py) 里提供了 `perf_fn` 这个「DeepEP 风格」的测速函数，而 `example_allgather.py` 则示范了「与 PyTorch 参考实现逐元素对比」的正确性校验范式。

`perf_fn` 与单机 `get_profiler().do_bench()`（u1-l3）思路一致但更朴素：**手动 warmup → 用 256MB 数据刷 L2 cache → CUDA event 计时多次取平均**。区别在于它面向「分布式函数」——你把整个 `fn()`（含通信）塞进去，它在当前进程的卡上测。

#### 4.4.2 核心流程

`perf_fn(fn, warmup, rep, post_fn)` 的执行流程：

```text
1. torch.cuda.synchronize()
2. 分配 256MB 的 cache tensor（用来污染/刷 L2）
3. warmup: 循环 warmup 次调用 fn()
4. cache.zero_()              ← 刷 L2，避免数据驻留 cache 影响测量
5. for i in range(rep):
       start_events[i].record()
       fn()
       end_events[i].record()
       (可选) post_fn()        ← 例如每次迭代后补一次 barrier
6. torch.cuda.synchronize()
7. 去掉第一次计时，对剩余求平均 → 返回平均延迟 (ms)
```

去掉第一次（`[1:]`）是为了排除首次调用的冷启动开销（如 TMA 初始化）。`post_fn` 常用于在每次 `fn()` 后补一个 `pynvshmem.nvshmem_barrier_all()`，确保异步通信在下一次计时开始前真正完成。

**正确性校验范式**（`example_allgather.py`）：

```text
torch_ag()      ← PyTorch 参考实现（dist.all_gather_into_tensor）
tilelang_ag()   ← 你的 TileLang 分布式 kernel + nvshmem_barrier_all
out  = torch_ag()
ref  = tilelang_ag()
assert torch.allclose(out, ref, atol=1e-3, rtol=1e-3)   ← 逐元素对比
```

#### 4.4.3 源码精读

**`perf_fn`** 在 [tilelang/distributed/utils.py:230-267](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L230-L267)：

- L2 刷 cache：`cache = torch.empty(int(256e6 // 4), dtype=torch.int, device="cuda")`（[L245](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L245)），256MB int 张量；测量前 `cache.zero_()`（[L252](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L252)）。
- CUDA event 计时（[L255-263](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L255-L263)）：每轮 `fn()` 前后各 record 一个 event，可选 `post_fn()`。
- 求平均：`np.average(times[1:])`（[L266-267](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L266-L267)），单位 ms（`s.elapsed_time(e)`）。

**端到端范例** [examples/distributed/example_allgather.py:46-97](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L46-L97)，把本讲所有要素串起来：

1. **启动**（[L47](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L47)）：`init_distributed(return_tp_group=True)`——NVSHMEM 路线，默认 init NVSHMEM。
2. **编译 kernel**（[L58-59](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L58-L59)）：`tilelang.compile(func, pass_configs={"tl.disable_tma_lower": True})`。注意这里 **没有** `kernel.initialize(allocator=...)`——对称堆路线不需要。
3. **kernel 内部用 NVSHMEM 原语**（[L20-29](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L20-L29)）：`T.get_pe()` / `T.get_pe_num()` / `T.putmem_nbi_block(T.address_of(B[...]), T.address_of(A_shared[...]), bytes, peer)`——典型 NVSHMEM 路线 device 原语。
4. **torch 参考**（[L68-71](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L68-L71)）：`dist.all_gather_into_tensor(out, local_data, group=TP_GROUP)`。
5. **TileLang 实现**（[L78-85](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L78-L85)）：用 `pynvshmem.nvshmem_create_tensor` 建对称堆的输入/输出 buffer（[L79](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L79)、[L81](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L81)），调 `kernel(ag_buffer, out)`，再 `pynvshmem.nvshmem_barrier_all()`（[L84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L84)）确保所有 PE 写完。
6. **测速 + 校验**（[L73-95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L73-L95)）：分别用 `perf_fn` 测 torch 版与 tilelang 版的延迟，最后 `torch.allclose(out, ref, atol=1e-3, rtol=1e-3)` 逐元素对比。

#### 4.4.4 代码实践

**实践目标**：在单机多卡上完整跑通 allgather，记录每个 rank 的延迟与正确性。

**操作步骤**（需 ≥2 张同型号 GPU，且已按 4.2 构建 NVSHMEM 与 pynvshmem）：

1. 确认 `import pynvshmem` 与 `import tilescale_ext` 均成功（后者见 u6-l5）。
2. 用 `launch.sh` 启动（默认用全部卡；先用 2 卡试）：
   ```bash
   GPUS=2 bash tilelang/distributed/launch.sh examples/distributed/example_allgather.py --M 8192 --N 12288 --warmup 5 --repeat 10
   ```
3. 观察每个 rank 打印的两行：`rank N torch all_gather avg time: ... ms` 和 `rank N tilelang all_gather avg time: ... ms`，以及最后的 `rank N check passed.✅`。
4. 把两个延迟相除，得到 TileLang 相对 torch 的加速比。

**需要观察的现象**：所有 rank 都打印 `check passed.✅`，说明 `torch.allclose` 通过——TileLang 的 `putmem_nbi_block` 实现的 allgather 与 `dist.all_gather_into_tensor` 数值一致。

**预期结果**：

- 正确性：全部 `✅`。
- 性能：在 NVLink 全连接的卡上，TileLang 版通常与 torch/NCCL 版相当或更优（具体数字待本地验证，受卡型、拓扑、`CUDA_DEVICE_MAX_CONNECTIONS` 等影响）。

> 若无多卡环境：退化为「源码阅读 + 调用链跟踪」。画出 `tilelang_ag()` 内部从 `nvshmem_create_tensor` → `kernel(ag_buffer, out)`（内部走 `T.putmem_nbi_block`）→ `nvshmem_barrier_all` 的完整数据流，标注每步「数据在哪一级显存 / 是本地还是远程写」。

#### 4.4.5 小练习与答案

**练习 1**：`perf_fn` 为什么要 `times = np.array(...)[1:]` 去掉第一次？

> **答案**：第一次调用通常包含一次性初始化开销（如 NVSHMEM/TMA 的惰性初始化、驱动路径建立），不代表稳态性能。去掉首次让测量更贴近真实运行延迟。

**练习 2**：`example_allgather.py` 里 `tilelang_ag()` 末尾的 `pynvshmem.nvshmem_barrier_all()` 能去掉吗？去掉会怎样？

> **答案**：不能随便去掉。`T.putmem_nbi_block` 是 **非阻塞（nbi）** 远程写，函数返回时数据未必真正送达对端 PE。`perf_fn` 测的是「`fn()` 调用返回」的延迟，若不在每次迭代后 barrier，下一轮 `fn()` 可能在上一轮的远程写未完成时就开始，测出的延迟不准、甚至读到半新半旧的数据。`post_fn` 参数也是为这类「每轮补同步」设计的。

**练习 3**：`example_allgather.py` 为什么用 `dist.all_gather_into_tensor` 作为参考，而不是手写一个 allgather？

> **答案**：因为 `dist.all_gather_into_tensor` 是 PyTorch 官方、经 NCCL 高度优化、数值正确的 allgather 实现，是天然的「黄金参考」。用它做对比，可以隔离出「TileLang 实现本身的正确性/性能」，而不用同时怀疑参考实现有 bug。这是分布式 kernel 校验的标准范式：**永远用成熟的集体通信库作参考**。

---

## 5. 综合实践

把本讲四块知识串起来，完成一个「最小分布式 put + 校验」程序的搭建与阅读。

**任务**：阅读并补全对 `example_allgather.py` 启动链路的完整解释，然后回答一个串接性问题。

1. **追启动链路**：从 `launch.sh` 的 `torchrun` 命令出发，写下「环境变量 → torchrun 子进程 → `init_distributed` → `init_nvshmem_by_uniqueid` → `nvshmem_create_tensor` → `kernel(...)` → `nvshmem_barrier_all`」这条完整时序，每一步标注：谁设的变量、谁读的变量、这一步在 host 还是 device。
2. **改写路线对照**：如果把 `example_allgather.py` 从 NVSHMEM 路线改写成 CP-engine 路线，需要改哪几处？请列出（提示：启动函数、张量来源、是否 `kernel.initialize`、device 原语族）。
3. **设计一个微基准**（选做）：仿照 `perf_fn` 的结构，写一个函数测量「单次 `nvshmem_barrier_all`」的延迟，要求带 warmup 和 L2 flush，并用它对比 2 卡与 4 卡下 barrier 的延迟差异。

**参考答案要点**：

1. 时序要点：`launch.sh` 设 `GPUS/NODES/MASTER_ADDR` 与 NVSHMEM 开关 → `torchrun` 注入 `RANK/WORLD_SIZE/LOCAL_RANK` → 子进程 `init_distributed` 读这三个变量建 NCCL 进程组与 `TP_GROUP`（host）→ `init_nvshmem_by_uniqueid` 广播 uniqueid 并初始化 NVSHMEM、确立 PE 号（host）→ `nvshmem_create_tensor` 在对称堆分配输入输出（host，但内存在 device symmetric heap）→ `kernel(...)` 执行 device 代码，内部 `T.putmem_nbi_block` 做远程写（device）→ `nvshmem_barrier_all` 等所有 PE 的远程写落盘（host 触发、device 执行）。
2. 改 CP-engine 路线需改：① 启动从 `init_distributed` 换成 `init_dist`（不 init NVSHMEM）；② 张量从 `nvshmem_create_tensor` 换成 `tilelang.get_allocator(...)` + `tilelang.tensor(..., allocator=)`；③ 在 `kernel = compile(...)` 后加 `kernel.initialize(allocator=allocator)`；④ device 原语从 `T.get_pe/T.get_pe_num/T.putmem_nbi_block` 换成 `T.get_rank/T.get_num_ranks/T.put_block`，并把 `T.address_of(B[...])` 的目标地址语义从「对称偏移」理解为「远程基址 + 偏移」。
3. 微基准：复用 `perf_fn` 的 warmup + `cache.zero_()` + event 计时骨架，令 `fn = pynvshmem.nvshmem_barrier_all`，分别在 `GPUS=2` 和 `GPUS=4` 下跑，记录平均延迟。预期 barrier 延迟随 PE 数增加而上升（具体待本地验证）。

## 6. 本讲小结

- TileScale 分布式程序的第一步是用 `torch.distributed`（NCCL 后端）建进程组，`init_distributed`（torchrun 友好）读 `WORLD_SIZE/RANK/LOCAL_RANK`，`init_dist`（spawn 友好）按节点×本地 rank 计算；二者是两套启动风格的入口。
- **两条运行时路线必须与 device 原语族配对**：NVSHMEM 路线（`get_pe/putmem`）靠对称堆自动寻址，host 侧要 `init_nvshmem_by_uniqueid` + `nvshmem_create_tensor`，**无需** allocator；CP-engine 路线（`get_rank/put_block`）靠远程基址表，host 侧要 `get_allocator` + `kernel.initialize(allocator=...)`。
- `pynvshmem` 是 NVSHMEM 的主机端 Python 绑定：`init_nvshmem_by_uniqueid` 用 uniqueid 广播引导 NVSHMEM（PE 号 == torch rank），`nvshmem_create_tensor` 把 `nvshmem_malloc` 的对称指针包装成自动 `nvshmem_free` 的 torch tensor，并提供 `barrier_all` / 信号写入 / Team·CmpType·Amo 枚举。
- `launch.sh` 用 `torchrun` 按 `GPUS/NODES` 拉起每卡一个进程，注入 NVSHMEM/NCCL/TileScale 开关（关键是 `NVSHMEM_BOOTSTRAP_MPI_PLUGIN=nvshmem_bootstrap_torch.so` 复用 torch rendezvous），所有规模信息经环境变量中转。
- `perf_fn` 提供 DeepEP 风格测速（warmup → 256MB 刷 L2 → CUDA event 多轮取平均，去首次），`example_allgather.py` 示范了「与 `dist.all_gather_into_tensor` 参考逐元素 allclose 校验」的分布式 kernel 验证范式。

## 7. 下一步学习建议

- **u6-l5（IPC 张量与 tilescale_ext 内存管理）**：本讲的 CP-engine 路线提到了 `tilelang.get_allocator` 与 `kernel.initialize`，它们底层的 IPC handle 创建/同步、`tensor_from_ptr` 所有权模型就在 `tilescale_ext` 这个 C++ 扩展里——下一讲深入它，你就能彻底理解「远程基址表是怎么靠 IPC handle 跨进程建起来的」。
- **u6-l6（DeepEP 集成）**：DeepEP 的 dispatch/combine 是更复杂的 all-to-all 场景，本讲的 `init_distributed` + `perf_fn` + 正确性校验范式正是运行 DeepEP 示例的运行时基础。
- **动手验证**：若有多卡环境，把 `example_allgather.py`、`example_all_to_all.py`、`example_summa.py`（u6-l7）依次用 `launch.sh` 跑一遍，对比每条示例用的是哪条路线、用了哪些 device 原语——这是巩固「host 运行时 ↔ device 原语」配对关系的最佳练习。
