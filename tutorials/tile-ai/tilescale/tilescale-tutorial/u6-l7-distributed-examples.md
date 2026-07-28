# 分布式实战：allgather / all2all / summa / gemm-rs

## 1. 本讲目标

本讲是分布式单元（Unit 6）的收尾实战课。前面几讲分别讲了 NVSHMEM 多设备原语（[u6-l2](u6-l2-nvshmem-primitives.md)）、CP-engine 远程拷贝（[u6-l3](u6-l3-cpengine-remote-copy.md)）、pynvshmem 运行时与启动（[u6-l4](u6-l4-pynvshmem-launch.md)）、IPC 与 allocator（[u6-l5](u6-l5-ipc-tilescale-ext.md)）。本讲把这些零件组装成五个经典分布式算法，学完后你应当能够：

1. 读懂 `examples/distributed/` 下 allgather、all-to-all、SUMMA、Cannon、gemm+reduce-scatter 五个完整示例。
2. 理解「**一个 kernel launch 内部**」组织通信与计算 overlap 的两种范式：广播-计算流水（SUMMA）与环形移位（Cannon）。
3. 理解「**跨 CUDA stream**」组织 overlap 的第三种范式（gemm-rs）：用 device 侧原子信号把 gemm 流和 reduce-scatter 流对接起来。
4. 分清两条运行时路线在这些示例里如何配对：NVSHMEM 路线（`get_pe`/`putmem`/`signal`，配 `init_distributed` + pynvshmem 对称堆）vs CP-engine 路线（`get_rank`/allocator，配 `init_dist` + `kernel.initialize`）。

## 2. 前置知识

本讲默认你已掌握以下概念（来自前置讲义），这里只做一句话回顾：

- **PE / rank**：分布式里的「处理单元」编号。NVSHMEM 路线用 `T.get_pe()` / `T.get_pe_num()`，CP-engine 路线用 `T.get_rank()` / `T.get_num_ranks()`，二者同义。
- **对称堆（symmetric heap）**：所有 PE 分配同构显存堆，同一对称偏移在所有 PE 上指向逻辑同一地址，使 device 线程能 one-sided 远程读写。NVSHMEM 路线靠 `pynvshmem.nvshmem_create_tensor` 自动获得；CP-engine 路线靠 allocator 注入「远程基址表」后再用 `remote_addr = get_remote_base_ptr(peer) + 本地偏移` 寻址。
- **put / get 原语**：`T.putmem_nbi_block(dest, src, nbytes, pe)` 是「我把本地 src 推到 peer 的 dest」，非阻塞（nbi）。`putmem_signal_nbi_block` 在搬运完成时顺便给目的端一个原子信号。
- **signal / wait 通知模型**：`T.signal_op(addr, val, sig_op, pe)` 在 peer 上原子更新信号；`T.signal_wait_until(addr, cmp, val)` 在本地自旋等待条件成立。这是设备侧「生产者通知消费者」的标准握手。
- **持久化 kernel（persistent kernel）**：`grid_size = min(SM 数, tile 总数)`，一个 threadblock 在 `for w in serial(waves)` 里循环处理多批 tile，避免反复 launch。
- **`T.Pipelined`**：软件流水，把 K 维循环变成多缓冲，隐藏访存延迟（见 [u4-l2](u4-l2-software-pipeline.md)）。

两个枚举会反复出现，先记一下：

- `T.CmpType`：信号比较类型，`EQ=0 / NE=1 / GT=2 / LE=3 / LT=4 / GE=5`（[ir.py:328-335](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/tir/ir.py#L328-L335)）。
- `T.Amo`：原子内存操作类型，`SIGNAL_SET=9 / SIGNAL_ADD=10`（[ir.py:338-355](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/tir/ir.py#L338-L355)）。`SIGNAL_ADD` 把信号值累加，`SIGNAL_SET` 直接覆盖。

## 3. 本讲源码地图

| 文件 | 作用 | 路线 |
|---|---|---|
| [examples/distributed/example_allgather.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py) | allgather：每个 block 把本地一段推给一个 peer | NVSHMEM |
| [examples/distributed/example_all_to_all.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_all_to_all.py) | all-to-all：不等长路由 + signal/wait 完成握手 | NVSHMEM |
| [examples/distributed/example_summa.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py) | SUMMA 分布式 GEMM：行列网格广播-计算流水 | NVSHMEM |
| [examples/distributed/example_cannon.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py) | Cannon 分布式 GEMM：网格环形移位 | NVSHMEM |
| [examples/distributed/example_gemm_rs_overlapped.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py) | gemm + reduce-scatter：跨 CUDA stream overlap | CP-engine |
| [examples/distributed/reduce_scatter.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py) | gemm-rs 依赖的 2D reduce-scatter 实现 | CP-engine |
| [tilelang/language/distributed/multi_device/nvshmem.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py) | NVSHMEM 路线 Python intrin 定义（putmem/signal/wait） | — |
| [tilelang/distributed/utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py) | `init_distributed` / `init_dist` / `perf_fn` 运行时工具 | — |

运行任一示例的方式（见 [examples/distributed/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/README.md)）：先构建 NVSHMEM 设备库、装好 `pynvshmem`，再用 `./tilelang/distributed/launch.sh examples/distributed/example_xxx.py` 拉起多进程（[launch.sh:41-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L41-L45) 用 `torch.distributed.run` 每卡拉一个进程）。

---

## 4. 核心概念与源码讲解

### 4.1 allgather：把本地块推给所有 PE

#### 4.1.1 概念说明

allgather 的语义是：每个 PE 持有 \(M/\text{PE\_num}\) 行数据，结束后每个 PE 都要拿到全部 \(M\) 行。最直观的 TileLang 写法不是「我去拉别人的」，而是「**我把自己的本地块主动 put 给每一个 peer**」——这就是 one-sided 推送模型。所有 PE 同时这么做，就拼出了完整的全量矩阵。

关键技巧：用 grid 的一个维度去**枚举目标 peer**。grid 第二维取 `PE_num - 1`（除自己外的所有 peer），这样 `(bx, by)` 里的 `by` 天然对应「发给第 by 个非自身 peer」，一个 kernel launch 就覆盖了所有发送任务。

#### 4.1.2 核心流程

```
对每个 PE（并行，各跑同一份 kernel）:
  grid = (本地块数 M_per_rank//block_M, PE_num-1)
  for (bx, by) in grid:        # 每个 threadblock 负责一块 × 一个 peer
      mype  = get_pe()
      local_base  = bx * block_M            # 我本地的第 bx 块
      global_base = M_per_rank*mype + local_base  # 这块在全量矩阵里的行号
      把 A[local_base : +block_M] copy 到 shared
      peer = (mype + by + 1) % npes          # 跳过自己
      putmem_nbi_block(B[global_base] @peer, A_shared, block_M*N*字节, peer)
```

注意：每个 PE 只写全量输出 `B` 中**属于自己的行段**（`global_base` 落在 `M_per_rank*mype` 起始处），而所有 PE 的推送合起来恰好填满 `B`。本地的那一段在 host 侧预先 `copy_` 进去（见 [example_allgather.py:82](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L82)），kernel 只负责发给其余 PE。

#### 4.1.3 源码精读

kernel 主体（[example_allgather.py:20-29](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L20-L29)）：

```python
with T.Kernel(M_per_rank // block_M, PE_num - 1, threads=threads) as (bx, by):
    mype = T.get_pe()                 # NVSHMEM 路线：我是谁
    npes = T.get_pe_num()
    A_shared = T.alloc_shared((block_M, N), dtype)
    local_base = bx * block_M
    global_base = M_per_rank * mype + local_base
    T.copy(A[local_base : local_base + block_M, :], A_shared)   # 本地→shared
    peer = (mype + by + 1) % npes     # by 枚举除自己外的 peer
    T.putmem_nbi_block(               # one-sided 非阻塞推送
        T.address_of(B[global_base, 0]),
        T.address_of(A_shared[0, 0]),
        block_M * N * dtype_map[dtype].itemsize, peer)
```

host 侧用 `pynvshmem.nvshmem_create_tensor` 分配对称堆张量 `B`，先把本地段拷进对角，再调 kernel，最后 `nvshmem_barrier_all()` 等所有 PE 写完（[example_allgather.py:78-85](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L78-L85)）。`putmem_nbi_block` 对应的 intrin 定义在 [nvshmem.py:101-109](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L101-L109)，参数为 `(dest, src, nelems, pe)`，`nelems` 单位是**字节**。

#### 4.1.4 代码实践

**实践目标**：理解 grid 维度与 peer 的对应关系。

1. 打开 `example_allgather.py`，把 `PE_num` 设为 4，`M_per_rank//block_M` 设为 2，在纸上画出 4 个 PE 各自的 `(bx, by)` 二维 grid（共 \(2\times3=6\) 个 block）。
2. 对每个 block 标注 `peer = (mype + by + 1) % 4` 的取值，验证「每个 PE 恰好向其余 3 个 PE 各发一次，且每个 peer 都覆盖了所有本地块」。
3. 修改 `block_M`（如从 4 改成 8），观察 `get_kernel_source()` 里 `putmem` 调用的字节数变化（用 `--print_source` 在 rank 0 打印，[example_allgather.py:62-63](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_allgather.py#L62-L63)）。

**预期现象**：`putmem` 的字节数 = `block_M * N * itemsize`，随 `block_M` 线性增长。多卡运行结果待本地验证（需 NVSHMEM 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 grid 第二维是 `PE_num - 1` 而不是 `PE_num`？
> 答案：因为不需要把数据 put 给自己。本地段在 host 侧已 `copy_` 进 `B` 的对角，kernel 只需覆盖其余 `PE_num-1` 个 peer。`peer = (mype + by + 1) % npes` 中的 `+1` 正是为了跳过 `mype` 自身。

**练习 2**：如果把 `T.putmem_nbi_block` 换成 `T.getmem_nbi_block`（我去拉），grid 该怎么重新设计？
> 答案：get 模型下，每个 block 应当从某个 peer 拉取该 peer 的本地段。grid 第二维仍可枚举源 peer，但 `dest` 是本地 `B` 的对应行段、`src` 是 peer 的本地 `A`、`pe` 是源 peer。语义上每个 PE 主动收集所有 peer 的数据，与 put 模型等价但方向相反。

---

### 4.2 all-to-all：不等长路由与 signal/wait 完成通知

#### 4.2.1 概念说明

all-to-all 是 allgather 的「不等长」升级版：典型场景是 MoE 里每个 token 要送到它被分配到的专家所在的 PE，而每个 PE 收到的 token 数是**动态、不等长**的。因此不能用 allgather 那种「固定块大小、grid 枚举 peer」的均匀切分，而要靠一张「前缀和表」`splits_cumsum` 来描述「我要发给 peer p 的数据是 `data_src` 的哪一段」。

另一个新点是**完成握手**：因为目的缓冲 `data_dst` 要被所有 peer 写入，消费者在读之前必须确认所有入站 put 都已落地。这里用 signal/wait 实现：发送方 put 完成后给目的端写一个信号，接收方自旋等待该信号。

#### 4.2.2 核心流程

```
splits_cumsum[e]:  全局专家 e 的前缀 token 计数
EXPERTS_PER_RANK = EXPERT_NUM // PE_num
对每个 PE（并行）:
  grid = (PE_num,)            # 一个 block 对应一个目标 peer
  for bx in grid:
      peer = bx
      m_start = splits_cumsum[peer    * EXPERTS_PER_RANK]
      m_end   = splits_cumsum[(peer+1)* EXPERTS_PER_RANK]
      putmem_nbi_block(data_dst[0] @peer, data_src[m_start], (m_end-m_start)*HIDDEN*2, peer)
      fence()                 # 给本 PE 的 put 定序
      if tid==0:
          signal_op(signal[mype] @peer, 99, SIGNAL_SET, peer)   # 通知 peer：「我发完了」
          signal_wait_until(signal[peer] @本地, EQ, 99)         # 等 peer 也发完
```

信号槽 `signal[k]` 的含义：「PE k 是否已经把它的数据发到我了」。每个源 PE 在目的端自己的槽位（`signal[mype]`）上写 99，接收方就等自己的 `signal[peer]` 变 99。

#### 4.2.3 源码精读

kernel 主体（[example_all_to_all.py:25-55](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_all_to_all.py#L25-L55)）：

```python
with T.Kernel(PE_num, threads=128) as (bx):
    peer = bx
    tx = T.thread_binding(128, thread="threadIdx.x")
    mype[0] = T.get_pe(); npes[0] = T.get_pe_num()
    m_start[0] = splits_cumsum[peer * EXPERTS_PER_RANK]
    m_end[0]   = splits_cumsum[(peer + 1) * EXPERTS_PER_RANK]
    T.putmem_nbi_block(                       # 不等长段推送
        T.address_of(data_dst[0, 0]),
        T.address_of(data_src[m_start[0], 0]),
        (m_end[0] - m_start[0]) * HIDDEN * 2, peer)
    T.fence()                                 # 定序：put 先于 signal
    if tx == 0:
        T.signal_op(T.address_of(signal[mype[0]]), 99, 9, peer)   # 9 = SIGNAL_SET
        T.signal_wait_until(T.address_of(signal[peer]), 0, 99)    # 0 = EQ
```

几点要点：

- `signal_op(addr, signal, sig_op, pe)` 的第三参数 `9` 即 `SIGNAL_SET`（[ir.py:352-353](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/tir/ir.py#L352-L353)），第四参数是**目的 PE**；intrin 定义见 [nvshmem.py:163-171](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L163-L171)。
- `signal_wait_until(addr, cmp, value)` 第二参数 `0` 即 `CmpType.EQ`；intrin 见 [nvshmem.py:174-176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L174-L176)。
- host 侧用 `random.sample` 生成专家路由、`torch.bincount` 统计每专家 token 数、`splits_to_cumsum` 算前缀和（[example_all_to_all.py:75-88, 111-114](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_all_to_all.py#L75-L88)），这正是 u6-l6 DeepEP dispatch 里 `get_dispatch_layout` 的简化版。

#### 4.2.4 代码实践

**实践目标**：搞清「不等长段」如何由前缀和表驱动。

1. 读 [example_all_to_all.py:75-88](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_all_to_all.py#L75-L88)，用 `-M 8 -G 128 --topk 8` 跑一次（单进程模拟时 `PE_num=1` 退化为本地拷贝）。
2. 打印 `splits_gpu_cur_rank` 与 `split_cumsum`，验证 `m_end - m_start` 对不同 peer 取值不同（这正是「不等长」）。
3. 把 `signal_op` 的值从 `99` 改成 `1`、`signal_wait_until` 的期望值同步改成 `1`，确认握手仍成立——这说明信号值本身只是「约定」，算法只关心「是否到达」。

**预期结果**：`split_cumsum` 单调递增、相邻差即为发给对应专家组的 token 数。多卡端到端正确性待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `signal_op` 写的是 `signal[mype[0]]`（自己的槽）而不是 `signal[peer]`？
> 答案：因为这次 `signal_op` 的目标 PE 是 `peer`（第四参数），即「在 peer 上、把 `signal[mype]` 这个槽设成 99」。对 peer 而言，`signal[mype]` 表示「源 PE mype 是否已发完」。接收方随后等的是自己的 `signal[peer]`（即所有源 PE 中对应那一个的槽）。槽位按「源 PE 编号」索引。

**练习 2**：去掉 `T.fence()` 会怎样？
> 答案：`fence` 保证 putmem 的发出顺序先于 signal。没有它，signal 可能在 putmem 真正落地前就被对端看到，导致接收方读到未写完的 `data_dst`。`fence` 只定序、不等完成，是「通知发出」而非「通知完成」的弱保证；真正保证 put 完成的是 NVSHMEM 对 `putmem_signal` 类操作在数据交付时才递增信号的语义。

---

### 4.3 SUMMA：行列网格上的广播-计算流水

#### 4.3.1 概念说明

SUMMA（Scalable Universal Matrix Multiplication）是把矩阵乘 \(C=A\times B^T\) 分布到 \(\sqrt{p}\times\sqrt{p}\) 个 PE 上的经典算法。把 PE 排成 `MESH×MESH` 网格，`pe_mn = mype // MESH`（行号）、`pe_k = mype % MESH`（列号）。每个 PE 持有 \(C\) 的一个 \(M_{\text{local}}\times N_{\text{local}}\) 块，以及对应位置的 \(A\)、\(B\) 子块。

核心思想是**沿网格广播**：把内层求和

\[ C[i,j] = \sum_{k=0}^{\text{MESH}-1} A[i,k]\cdot B[k,j] \]

拆成 `MESH` 轮。第 `ko` 轮：

- 持有 \(A[i, ko]\) 的 PE（即 `pe_k == ko`）把这块**沿行广播**给同行所有 PE；
- 持有 \(B[ko, j]\) 的 PE 把这块**沿列广播**给同列所有 PE；
- 每个 PE 用收到的 \(A[i,ko]\)、\(B[ko,j]\) 做一次本地 GEMM，累加进 \(C\)。

\(MESH\) 轮过后，\(C\) 完成。

#### 4.3.2 核心流程（含双缓冲 + 信号计数 overlap）

SUMMA 示例最精彩的是**把通信与计算重叠**：用双缓冲 `A[2,...]`/`B[2,...]`，第 `ko` 轮在算 `A[ko%2]` 时，已经在后台把第 `ko+1` 轮的 `A[(ko+1)%2]` 广播出去。靠两组信号计数握手：

- `*_signal_to`（在**接收端**计数）：putmem_signal 每送达一次就 `+1`。消费者等 `*_signal_to >= (ko+1)*块数` 才开始消费。
- `*_signal_from`（在**发送端**计数）：消费者消费完一轮后给「下一轮的发送者」`+1`。发送者等 `*_signal_from >= total_tiles*MESH*ko` 才允许覆盖缓冲（即下一轮写入安全了）。

```
持久化 kernel: grid = min(132, total_tiles), 每个 block 循环 waves 处理多 tile
for ko in serial(MESH):                         # MESH 轮通信-计算
    # —— 通信阶段（仅 pe_k==ko 的 PE 做发送）——
    if pe_k == ko:                              # 我是 A 的行广播源
        wait A_signal_from >= total_tiles*MESH*ko   # 等缓冲可安全覆盖
        for peer_k in serial(MESH):             # 沿行广播给所有同行 PE
            putmem_signal_nbi_block(A[(ko+1)%2] @peer, A[ko%2], ...,
                                     A_signal_to @peer, +1, SIGNAL_ADD, 行内peer)
    # 同理广播 B（沿列）
    # —— 同步阶段 ——
    wait A_signal_to >= (ko+1)*行块数            # 等本 ko 轮 A 全部到达
    wait B_signal_to >= (ko+1)*列块数
    # —— 计算阶段 ——
    for w in serial(waves):                     # 持久化遍历所有 tile
        本地 GEMM: T.Pipelined over K_local:   A_shared,B_shared -> C_local
        算完给下一轮 A/B 发送者 +1 (A_signal_from / B_signal_from)
```

#### 4.3.3 源码精读

PE 网格分解与 ko 循环入口（[example_summa.py:46-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L46-L50)）：

```python
pe_mn = mype[0] // MESH   # 行号
pe_k = mype[0] % MESH      # 列号
T.clear(C_local)
for ko in T.serial(MESH):
```

A 的行广播（[example_summa.py:52-69](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L52-L69)）：只有 `pe_k == ko`（即持有 `A[pe_mn, ko]`）的 PE 才发送，目标是同行所有 PE `pe_mn*MESH + peer_k`：

```python
if pe_k == ko:
    if tx == 0:
        T.signal_wait_until(T.address_of(A_signal_from[0]), T.CmpType.GE,
                            total_tiles * MESH * ko)   # 等缓冲可覆盖
    if block_id < T.ceildiv(M_local, A_rows_per_block):
        for peer_k in T.serial(MESH):
            T.putmem_signal_nbi_block(               # 双缓冲：写 (ko+1)%2 槽
                T.address_of(A[(ko + 1) % 2, A_rows_per_block * block_id, 0]),
                T.address_of(A[ko % 2,   A_rows_per_block * block_id, 0]),
                A_rows_per_block * K_local * dtype_map[dtype].itemsize,
                T.address_of(A_signal_to[0]), 1, T.Amo.SIGNAL_ADD,
                pe_mn * MESH + peer_k)               # 同行广播
```

`putmem_signal_nbi_block` 的 intrin 定义在 [nvshmem.py:140-152](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L140-L152)，参数 `(dest, src, nbytes, sig_addr, signal, sig_op, pe)`——搬运与递增信号是一次 NVSHMEM 原语完成，省一次往返。

消费者等待数据到达后做本地 GEMM（[example_summa.py:92-114](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L92-L114)）：

```python
T.signal_wait_until(T.address_of(A_signal_to[0]), T.CmpType.GE,
                    (ko + 1) * T.ceildiv(M_local, A_rows_per_block))  # A 到齐
T.signal_wait_until(T.address_of(B_signal_to[0]), T.CmpType.GE,
                    (ko + 1) * T.ceildiv(N_local, B_cols_per_block))  # B 到齐
for w in T.serial(waves):                              # 持久化遍历 tile
    bx = ...; by = ...
    if bx < ... and by < ...:
        T.copy(C[bx*block_M, by*block_N], C_local)
        for ki in T.Pipelined(T.ceildiv(K_local, block_K), num_stages=4):  # 软件流水
            T.copy(A[ko % 2, bx*block_M, ki*block_K], A_shared)
            T.copy(B[ko % 2, by*block_N, ki*block_K], B_shared)
            T.gemm(A_shared, B_shared, C_local, transpose_B=True)
        T.copy(C_local, C[bx*block_M, by*block_N])
```

算完一轮后通知下一轮的发送者（[example_summa.py:115-131](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L115-L131)），`a_sender = pe_mn*MESH + (ko+1)%MESH`，让缓冲可被下一轮覆盖。注意 host 侧把 \(A\)、\(B\) 在 K 维按列号分块预分布（[example_summa.py:186-188](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L186-L188)），编译时关掉 TMA 与 warp 特化（[example_summa.py:165](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L165)）。

#### 4.3.4 代码实践（本讲指定的综合实践）

**实践目标**：画出 N 个 PE 上一次完整 SUMMA 迭代的「通信-计算时序」。以 `MESH=2`（4 个 PE，编号 0/1/2/3）为例。

1. 标出每个 PE 的 `(pe_mn, pe_k)`：PE0=(0,0)、PE1=(0,1)、PE2=(1,0)、PE3=(1,1)。
2. 对 `ko=0` 这一轮，标出：
   - **谁发 A**：`pe_k==0` 的 PE，即 PE0（行 0）和 PE2（行 1）。PE0 把 `A[0,0]` 广播给 PE1（同行），PE2 把 `A[1,0]` 广播给 PE3。
   - **谁发 B**：持有 `B[ko=0]` 的 PE。注意 host 把 `B` 的 K 块按 `(cc+rr)` 分配，发往列内 peer（`pe_mn*MESH + peer_k`，见 [example_summa.py:87-88](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L87-L88)）。
   - **本地 gemm**：所有 4 个 PE 在 `putmem` 之后、`signal_wait_until(A/B_signal_to)` 通过后，进入 `T.Pipelined` 的 GEMM。
3. 画出双缓冲重叠：`ko=0` 的 GEMM 与 `ko=1` 的 `putmem_signal`（写 `A[(0+1)%2]=A[1]`）在时间上重叠。

参考时序（横向为时间，纵向为阶段）：

```
ko=0:  [发A/B: putmem_signal → A[(1)%2]]──┐
ko=0:  [wait A/B_signal_to 到齐]          │  二者重叠
ko=0:  [本地 GEMM (Pipelined over K_local)]┘
          │ 同时 ↓
ko=1:  [发A/B: putmem_signal → A[(0)%2]]  ← ko=0 计算时，ko=1 通信已在进行
ko=1:  [wait ...] [本地 GEMM]
```

**预期结果**：能指出「putmem_signal_nbi_block 与本地 GEMM 通过双缓冲 + 两组信号计数实现重叠」，并能解释 `A_signal_to`（接收计数）与 `A_signal_from`（消费计数）各自被谁等。

#### 4.3.5 小练习与答案

**练习 1**：为什么用两组信号（`_to` 和 `_from`）而不是一组？
> 答案：`_to` 解决「消费者何时能开始用数据」（数据到齐），`_from` 解决「生产者何时能覆盖缓冲」（旧数据已被消费完）。双缓冲要求「写下一轮」与「读当前轮」同时进行，必须用两个独立的计数器分别约束读、写两侧，否则会数据竞争。

**练习 2**：SUMMA 每轮每个 PE 要给同行/同列多少个 peer 发数据？通信量量级是多少？
> 答案：每轮 A 广播发给同行 `MESH` 个 peer、B 广播发给同列 `MESH` 个 peer（代码里 `for peer_k in serial(MESH)`，[example_summa.py:60, 80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L60)）。每轮通信量 \(O(M_{\text{local}}\cdot K_{\text{local}} + N_{\text{local}}\cdot K_{\text{local}})\)，共 `MESH` 轮。这是 SUMMA 相对 Cannon（每轮只与固定邻居通信）的特点：每轮广播多播，轮数少。

---

### 4.4 Cannon：网格上的环形移位

#### 4.4.1 概念说明

Cannon 算法是另一种分布式 GEMM，同样用 `MESH×MESH` 网格，但**不广播**，而是每轮把 \(A\) 沿行**左移一位**、\(B\) 沿列**上移一位**，做固定邻居的环形移位（circular shift）。前提是初始时对 \(A\)、\(B\) 做「skew（错位）」对齐——host 侧 scatter 时按 `(cc+rr)%MESH` 取 K 块实现（[example_cannon.py:292-293](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L292-L293)）。

与 SUMMA 的关键区别：

| 维度 | SUMMA | Cannon |
|---|---|---|
| 每轮通信 | 广播给同行/同列 `MESH` 个 peer | 只发给左邻、上邻各 1 个 peer |
| 邻居 | 动态（每轮换发送源） | 固定环形邻居 |
| 初始对齐 | 直接按列号分 K 块 | 需 skew 错位 |

#### 4.4.2 核心流程

```
a_peer_to   = 同行左邻 (mype-1) ; a_peer_from = 同行右邻 (mype+1)
b_peer_to   = 同列上邻 (mype-MESH) ; b_peer_from = 同列下邻 (mype+MESH)
for ko in serial(MESH):
    wait A/B_signal_from >= total_tiles*ko     # 等当前轮 A/B 到位
    # 移位：把当前 A[ko%2] 发给左邻，存入它的 A[(ko+1)%2]
    putmem_signal_nbi_block(A[(ko+1)%2] @左邻, A[ko%2], ..., A_signal_to, +1, 左邻)
    putmem_signal_nbi_block(B[(ko+1)%2] @上邻, B[ko%2], ..., B_signal_to, +1, 上邻)
    for w in serial(waves):                     # 持久化计算
        本地 GEMM over K_local
        算完给右邻/下邻的 A/B_signal_from +1     # 通知：我消费完了，你可覆盖
    wait A/B_signal_to >= (ko+1)*块数           # 等移过来的下一轮数据到齐
```

#### 4.4.3 源码精读

邻居计算（[example_cannon.py:51-54](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L51-L54)）：

```python
a_peer_from[0] = (mype[0] + 1) % MESH + MESH * (mype[0] // MESH)   # 同行右邻
a_peer_to[0]   = (mype[0] - 1 + MESH) % MESH + MESH * (mype[0] // MESH)  # 同行左邻
b_peer_from[0] = (mype[0] + MESH) % npes[0]                         # 同列下邻
b_peer_to[0]   = (mype[0] - MESH + npes[0]) % npes[0]               # 同列上邻
```

移位 + 计算的主循环（[example_cannon.py:56-126](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L56-L126)）：结构与 SUMMA 几乎一致（双缓冲 + `_to`/`_from` 信号计数 + 持久化 GEMM），唯一不同是 `putmem_signal_nbi_block` 的目标是**固定邻居**而非遍历同行/同列：

```python
if block_id < T.ceildiv(M_local, A_rows_per_block):
    T.putmem_signal_nbi_block(                    # A 左移
        T.address_of(A[(ko + 1) % 2, ...]), T.address_of(A[ko % 2, ...]),
        A_rows_per_block * K_local * dtype_map[dtype].itemsize,
        T.address_of(A_signal_to[0]), 1, T.Amo.SIGNAL_ADD, a_peer_to[0])
# ...B 上移同理 (b_peer_to) ...
for w in T.serial(waves):                          # 计算
    ...T.gemm(A_shared, B_shared, C_local, transpose_B=True)...
    if tx == 0:
        T.signal_op(...A_signal_from..., 1, T.Amo.SIGNAL_ADD, a_peer_from[0])  # 通知右邻
```

仓库还提供了一个**特化变体** `main_specialize`（[example_cannon.py:129-237](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L129-L237)）：把 grid 拆成「计算 block」与「搬运 block」（`copy_blocks=20`），让两类工作落在不同 threadblock 上——这是 u4-l3 warp/block 特化思想在 kernel 级的体现。但该变体标注了 `# TODO: fix correctness`（[example_cannon.py:128](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L128)），默认 `specialize=False` 走标准 `main`（[example_cannon.py:237, 261](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L237)）。

#### 4.4.4 代码实践

**实践目标**：对比 Cannon 与 SUMMA 的通信模式。

1. 读 [example_cannon.py:51-54](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L51-L54)，对 `MESH=3`（9 个 PE）画出网格，标出 PE4（中心）的 `a_peer_to/from`、`b_peer_to/from` 各是哪个 PE。
2. 对比 SUMMA：SUMMA 每轮每个发送 PE 调 `MESH` 次 `putmem_signal`（[example_summa.py:60, 80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L60)），Cannon 每轮只调 1 次（固定邻居）。在表里记下两者每轮 `putmem` 次数。
3. 观察 host 侧 skew：[example_cannon.py:292-293](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_cannon.py#L292-L293) 里 `a_tile`/`b_tile` 的 K 块索引是 `(cc+rr)%MESH`，对比 SUMMA 的 `cc`（[example_summa.py:187-188](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_summa.py#L187-L188)），说明 Cannon 多了初始错位。

**预期结果**：Cannon 每轮通信量更小但需要初始 skew；SUMMA 通信量更大但无需错位。端到端正确性待本地验证（多卡 + NVSHMEM）。

#### 4.4.5 小练习与答案

**练习 1**：Cannon 的 `a_peer_to` 是左邻还是右邻？数据流向是什么？
> 答案：`a_peer_to` 是同行左邻 `(mype-1)`，即「我把 A 发给左邻」。相应地 `a_peer_from` 是右邻，即「我从右邻接收 A」。于是 A 在网格里逐轮整体左移（环形），B 逐轮整体上移。

**练习 2**：为什么 `main_specialize` 标注「待修复正确性」？它相对 `main` 改了什么？
> 答案：`main_specialize` 把 grid 拆成 `compute_blocks` 个计算 block 和 `copy_blocks` 个搬运 block，让搬运与计算落在不同 block 上以更彻底地 overlap。但搬运 block 和计算 block 共享同一组信号计数与缓冲，二者的同步（`_to`/`_from` 阈值）需要重新配平，当前实现尚未保证正确，故标注 TODO、默认不启用。

---

### 4.5 gemm + reduce-scatter：跨 CUDA stream 的 overlap

#### 4.5.1 概念说明

前面三个示例（allgather/all2all/summa/cannon）都把通信与计算 overlap 放在**同一个 kernel 内**，靠 NVSHMEM 信号协调。`example_gemm_rs_overlapped.py` 展示第三种范式：把 GEMM 和 reduce-scatter 放在**两个 CUDA stream** 上，靠 **device 侧原子信号** + **host 侧 `cuStreamWaitValue`** 把两条流对接起来。

目标是 GEMM + reduce-scatter 融合：\(Y = \text{reduce\_scatter}_\text{col}(A \cdot B)\)，其中 \(A\) 在行（M）维按 rank 切分、\(B\) 沿 K 维按 rank 切分（TP 并行 GEMM 的常见布局）。每个 rank 只算 \(M\) 的一部分行（`pid_m_offset` 偏移），算完一段就给那段打个「完成」信号，reduce-scatter 流一旦看到信号就立刻消费——**不等整个 GEMM 跑完就开始 reduce**，这就是 overlap。

> 注意路线差异：本例用 CP-engine 路线（`init_dist` + `tilelang.get_allocator(is_distributed=True)` + `kernel.initialize(allocator=...)` 注入远程基址表），**不是** `init_distributed` + pynvshmem 对称堆。这与 u6-l4/u6-l5 的结论一致：device 用哪族原语、host 就做哪套运行时准备。

#### 4.5.2 核心流程

```
# device kernel (gemm_stream):
grid = (M//block_M * N//block_N)        # 用 swizzle 提升 L2 局部性
pid_m = (swizzle(bid).m + (local_rank+1)*M_per_rank//block_M) % num_pid_m  # 按 rank 错位
GEMM (Pipelined) -> C_shared
写出 C[pid_m*block_M, ...]
# inc barrier：本 block 属于哪个行段(segment)？
val = atom_add(counter_signal_buf[segment], 1)
if val == 该段总块数 - 1:                 # 我是本段最后一块
    st(scatter_signal_buf[segment], 1)   # 置完成信号

# host (rs_stream)，对每个段 segment:
cuStreamWaitValue32(scatter_signal_buf[segment] == 1)   # 等该段 GEMM 完成
copy / ring_reduce 该段 -> output
```

关键点：`segment` 是按行（M）维按 rank 切分的段，`pid_m_offset = (local_rank+1)*M_per_rank//block_M` 让每个 rank 负责不同的行段（[example_gemm_rs_overlapped.py:49-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py#L49-L50)），于是「rank r 算完段 r」这件事能被 reduce-scatter 流及时观察到。

#### 4.5.3 源码精读

GEMM kernel 里的「inc barrier」完成信号（[example_gemm_rs_overlapped.py:62-73](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py#L62-L73)）：

```python
segment_start = pid_m * block_M // M_per_rank
segment_end   = (T.min((pid_m + 1) * block_M, M) - 1) // M_per_rank
segment = segment_start + tid
if segment <= segment_end:
    ...
    val[0] = T.atom_add(counter_signal_buf[segment], 1, scope="gpu", sem="release")
    if T.Cast("int32", val[0]) == num_pid_n * tiled_m_size - 1:   # 本段最后一块
        T.st(scatter_signal_buf[segment], 1, scope="gpu", sem="release")
```

每个 GEMM block 算完后，对自己所属 segment 的计数器 `atom_add`；最后一个 block（计数达到 `num_pid_n * 该段行块数 - 1`）把 `scatter_signal_buf[segment]` 置 1。这是一个 device 侧的「行段完成」屏障。

host 侧两条流的对接（[example_gemm_rs_overlapped.py:78-94](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py#L78-L94)）：

```python
def gemm_rs_op(...):
    rs_stream.wait_stream(gemm_stream)
    with torch.cuda.stream(gemm_stream):
        gemm_kernel(A, B, scatter_signal_bufs[local_rank], counter_bufs[local_rank], C)
    with torch.cuda.stream(rs_stream):
        output = reduce_scatter_2d_op(C, ctx, output)   # 内部 cuStreamWaitValue 等信号
    gemm_stream.wait_stream(rs_stream)
    current_stream.wait_stream(rs_stream)
    return output
```

reduce-scatter 内部用 host 侧 `cuStreamWaitValue32` 等待 device 写出的信号（[reduce_scatter.py:241-246](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py#L241-L246)，定义在 [reduce_scatter.py:202-221](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py#L202-L221)）：

```python
if overlap_with_gemm:
    _wait_eq_cuda(scatter_signal_buf_intra_node[remote_local_rank], 1, stream)
```

即：reduce 流在拷贝某段前，先用 `cuStreamWaitValue32(==1)` 卡住，直到 GEMM kernel 把对应 `scatter_signal_buf` 置 1。`ring_reduce_tma`（[reduce_scatter.py:173-199](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py#L173-L199)）是 Hopper 上用 TMA 做的节点内环形 reduce。运行时准备走 allocator 路线（[example_gemm_rs_overlapped.py:128-134](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py#L128-L134)）：`init_dist` → `get_allocator(is_distributed=True)` → `gemm_func.initialize(allocator=allocator)` 注入远程基址表。

#### 4.5.4 代码实践

**实践目标**：理清「device 原子信号 ↔ host stream wait」的对接。

1. 读 [reduce_scatter.py:224-246](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py#L224-L246) 的 `intra_node_scatter`，找出它在哪里调用 `_wait_eq_cuda`，等的是哪个信号、什么值。
2. 在 [example_gemm_rs_overlapped.py:62-73](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py#L62-L73) 找到对应的「写信号」端（`T.st(scatter_signal_buf[segment], 1, ...)`），确认读写两侧用的是同一个 `scatter_signal_buf`、值都是 1。
3. 把 `overlap_with_gemm` 设为 `False`（在 [example_gemm_rs_overlapped.py:142-144](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py#L142-L144) 的 `create_reduce_scater_2d_ctx`），说明此时 `intra_node_scatter` 不再 `_wait_eq_cuda`（[reduce_scatter.py:241](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py#L241)），reduce 会与 gemm 串行而非 overlap。

**预期结果**：能说出「gemm 写 `scatter_signal_buf=1`、reduce 流 `cuStreamWaitValue32(==1)`」这一对构成了跨流握手。`overlap_with_gemm=False` 时延迟更高（无 overlap）。多卡运行待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么用 `atom_add` + 「最后一个 block 置信号」而不是每个 block 直接置信号？
> 答案：一个 segment 由多个 GEMM block 共同产出（`num_pid_n * 该段行块数` 个）。reduce 必须等该段**全部** block 写完才能消费。用 `atom_add` 计数，只有抢到最后一次自增（`val == 总数-1`）的那个 block 置信号，保证「信号 = 1」当且仅当该段所有输出已落地。

**练习 2**：本例的 reduce-scatter 用的是 reduce 算子的「环形」实现，它依赖什么硬件特性？
> 答案：`ring_reduce_tma`（[reduce_scatter.py:292-302](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py#L292-L302)）只支持 Hopper（`target_is_hopper`），用 TMA 做节点内搬运；非 Hopper 直接 `NotImplementedError`。跨节点 p2p 也标注了 `NotImplementedError`（[reduce_scatter.py:344](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py#L344)），目前仅节点内可用。

---

## 5. 综合实践

把本讲五个示例串起来，完成一张「分布式算法对照表 + SUMMA 时序图」：

1. **对照表**：按下表填写（答案见各小节，建议先自己填再核对）：

   | 算法 | 路线 | overlap 发生在 | 同步原语 | 每轮通信模式 |
   |---|---|---|---|---|
   | allgather | NVSHMEM | 无（单次 launch） | barrier_all | 1 对多推送 |
   | all-to-all | NVSHMEM | 无 | signal/wait | 1 对 1 不等长 |
   | SUMMA | NVSHMEM | kernel 内（双缓冲） | `_to`/`_from` 信号计数 | 行/列广播 |
   | Cannon | NVSHMEM | kernel 内（双缓冲） | `_to`/`_from` 信号计数 | 固定邻居环形移位 |
   | gemm-rs | CP-engine | 跨 CUDA stream | `atom_add`+`cuStreamWaitValue` | 行段完成信号 |

2. **SUMMA 时序图**（本讲指定的实践任务）：按 §4.3.4，对 `MESH=2` 的 4 个 PE 画出 `ko=0` 到 `ko=1` 的通信-计算时序，标注 `putmem_signal_nbi_block`（广播）、`signal_wait_until`（等数据/等可覆盖）、本地 `T.gemm`（Pipelined）三者的相对位置，并指出双缓冲如何让 `ko` 的计算与 `ko+1` 的通信重叠。

3. **（可选，需多卡）运行校验**：用 `launch.sh` 分别跑 `example_summa.py` 和 `example_gemm_rs_overlapped.py`，确认打印 `✅ ... match`，并记录 `perf_fn` / `bench` 给出的延迟与 TFLOPS。该步待本地验证。

## 6. 本讲小结

- 五个示例把分布式原语组装成了三类经典算法：**集合通信**（allgather、all-to-all）、**分布式 GEMM**（SUMMA、Cannon）、**融合通信算子**（gemm+reduce-scatter）。
- allgather 用 grid 维度枚举 peer，一个 launch 完成 1-对-多推送；all-to-all 用前缀和表驱动不等长路由，靠 signal/wait 做完成握手。
- SUMMA 与 Cannon 都用**双缓冲 + 两组信号计数（`_to`/`_from`）+ 持久化 kernel** 把通信与计算 overlap 在单个 kernel 内；区别是 SUMMA 每轮广播、Cannon 每轮固定邻居移位且需初始 skew。
- gemm-rs 展示了**第三种 overlap 范式**：device 侧 `atom_add` 行段完成信号 + host 侧 `cuStreamWaitValue`，把 GEMM 流和 reduce-scatter 流对接，走的是 CP-engine/allocator 路线而非 NVSHMEM 对称堆。
- 五个示例里 device 侧用的都是 u6-l2/u6-l3 讲过的 `putmem_signal`/`signal_op`/`wait`/`atom_add` 等原语，编译期只生成查表与调用文本，真正的远程搬运由 NVSHMEM 设备库或 CP-engine 模板完成。

## 7. 下一步学习建议

- **回到工程化**：读 [u6-l6](u6-l6-deepep.md) 的 DeepEP 集成，它把本讲的 all-to-all 思路升级成带「通道 + 环形缓冲 + head/tail 队列」的 MoE 专家路由 dispatch/combine，是 these primitives 在真实大模型里的工业级应用。
- **性能与可观测**：若要做分布式性能分析，建议阅读 [tilelang/distributed/utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py) 的 `perf_fn`（[utils.py:230-267](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L230-L267)）与各示例里的 `bench`，理解「256MB 刷 L2 + CUDA event 多轮」的延迟测量约定。
- **二次开发**：参考 [u7-l5](u7-l5-testing-benchmark-contrib.md)，尝试为某个分布式示例补一个最小测试或 benchmark，练习 `launch.sh` 多进程启动与 `dist.allclose` 校验的完整流程。
- **深入通信算子**：若对 reduce-scatter 的节点内环形 reduce 感兴趣，可继续读 [reduce_scatter.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/reduce_scatter.py) 的 `ReduceScatter2DContext` 与 `ring_reduce_tma`，并对照 [u7-l2](u7-l2-cuda-gemm-templates.md) 的 TMA 设备模板。
