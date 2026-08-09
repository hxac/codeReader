# AllGather + GEMM

## 1. 本讲目标

本讲讲解 QuACK 的分布式 AllGather+GEMM 融合内核（`quack/distributed/all_gather_gemm.py`），它在张量并行（Tensor Parallelism）场景下，把「跨 GPU 收集 A」与「在 A 上做矩阵乘」重叠在一起，让 GEMM 不必等 AllGather 完成就开始计算。

学完后你应该能够：

- 说清 `AllGatherArguments` 这个数据结构每个字段的含义，以及它如何充当「传输半」与「内核半」之间的契约。
- 理解 shard-major 旋转调度（shard-major rotated decode）：内核为什么按「本地 shard 先算、远端 shard 按环序到达」的顺序消费 tile。
- 理解 load-warp 到达门（`ag_wait_m_tile`）：为什么 `flags[shard*num_chunks+c] >= epoch` 这个门控能保证内核不会读到尚未就绪的 tile，以及为什么用单调 epoch 而不是 0/1 相位。
- 解释 AllGather+GEMM 如何在 A 仍被 copy stream 填充时就开始计算。

## 2. 前置知识

本讲建立在以下认知之上（来自依赖讲义）：

- **u3-l5 异步流水线与同步原语**：软件流水线用「full/empty 状态机」做 producer/consumer 握手，mbarrier 与计数信号量 `Semaphore`（`acquire load` / `release_store`、`scope="gpu"` 跨 CTA 可见）是基础工具。本讲会把分布式流水线的同步对象映射回 full/empty 角色。
- **u5-l1 GemmBase 共享主循环**：持久化 GEMM 内核由「load warp（用 TMA 把 A/B 灌进 smem）」与「MMA warp（消费片段发 MMA 指令）」组成，二者经 AB pipeline 的 empty/full mbarrier 握手。本讲的到达门就插在 load warp 发出「每个 tile 的第一次 TMA」之前。
- **持久化内核与 tile 调度（u3-l4）**：持久化内核用一个 grid 的 CTA 轮流消费多个输出 tile，tile 由调度器（`TileScheduler`）分配。AllGather+GEMM 强制要求持久化调度。
- **分布式训练常识**：张量并行把权重按列切分到多张卡，每个 rank 只持有一份 A 的「shard（分片）」；要把完整的 A 拼出来需要 AllGather。`world_size` = rank 数，每个 rank 有自己的 `rank` 编号。

补充几个本讲会用到的硬件术语：

- **CE（Copy Engine）**：GPU 上的 DMA 引擎，执行 `cudaMemcpyAsync` 这类拷贝，**不占用任何 SM（流多处理器）**。这与占用 SM 的「拷贝内核」（如 `tensor.copy_`、`torch.add`）形成对比——后者会和 GEMM 抢 SM。
- **NVLink / 对称内存（symmetric memory）**：GPU 间高速互联。`torch.distributed._symmetric_memory` 提供一块「所有 rank 都能直接读写对方虚拟地址」的缓冲区，让一个 rank 可以直接往对方显存写数据 + 写一个标志位，无需收发握手。
- **CUDA stream（流）**：GPU 上的任务队列，同一 stream 内按序执行，不同 stream 可并行。事件（`torch.cuda.Event`）用于在 stream 之间建立「record → wait」的依赖边。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/distributed/all_gather_gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py) | **传输半**。`AllGatherRunner` 拥有旋转缓冲、到达 flags、epoch、三条 stream 和 CE 推送调度；`gather()` 上下文管理器把任意 quack GEMM 包起来。`BlockScaledAllGatherRunner` 在其上加一条 scale-factor 通道。文件开头的模块 docstring 是完整的设计记录（含实测数据）。 |
| [quack/gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py) | **公共入口与契约**。定义 `AllGatherArguments`（内核面向的参数结构），并在 `gemm()` 里校验 AllGather+GEMM 的前置条件（dense、persistent、`split_k==1`、SM90+）。 |
| [quack/tile_scheduler.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py) | **调度半（内核侧）**。`ag_wait_m_tile` 是到达门；`AgSchedulerArguments` / `AgParams` 是内核侧参数孪生；调度器在 `create()` 与解码循环里做 shard-major 旋转解码。 |
| [quack/gemm_sm90.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py) | **设备侧内核示例**。在 load warp 的持久化调度循环里调用 `ag_wait_m_tile`，展示门控的实际接入点（SM100/SM120 同构接入）。 |
| [quack/gemm_tvm_ffi_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py) | **几何校验**。`validate_ag_geometry` 是「传输半与内核半共享的唯一几何事实」的执行点：shard/chunk 边界必须落在整数个 scheduler cluster 上。 |
| [tests/test_distributed_gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_distributed_gemm.py) | **多 rank 正确性测试**。用 `torchrun` 起多进程，构造参考 AllGather+matmul 与延迟传输场景，验证门控正确性。 |

## 4. 核心概念与源码讲解

整个 AllGather+GEMM 融合可以分成「两半」：

- **传输半（`AllGatherRunner`）**：负责把每个 rank 的本地 shard 推送到所有 rank 的本地显存，并发布「这块数据到了」的标志。
- **内核半（持久化 GEMM + 调度器 + 到达门）**：在 GEMM 内部，load warp 在读一个 tile 的 A 之前，先自旋等待该 tile 所属 shard 的标志被置位。

这两半通过一个极小的数据结构 `AllGatherArguments` 解耦。下面分四个最小模块讲解。

### 4.1 AllGatherArguments：连接两半的契约

#### 4.1.1 概念说明

AllGather+GEMM 的核心设计判断是：**让 GEMM 内核本身去等待数据到达**，而不是把 AllGather 和 GEMM 写成一个不可拆分的融合算子。这样，任意一个 quack GEMM 变体（普通 GEMM、带激活的 `gemm_act`、blockscaled 等）只要转发一个 `ag_args` 关键字参数，就能参与重叠，无需为每种模式单独写一个融合算子。

要做到这一点，传输半和内核半只需要共享一个极小的「稳定契约」：

> 常驻缓冲区 + 每个 shard（-chunk）单调递增的到达序列标志。

`AllGatherArguments` 就是这个契约在主机侧的类型化镜像，它把传输半准备好的 flags 张量、epoch 张量和几个标量几何量打包，随 GEMM 调用一起下发。

#### 4.1.2 核心流程

`AllGatherArguments` 的字段语义：

```
flags       : (num_shards * num_chunks,) int32 张量，对称内存
             flags[shard * num_chunks + c] >= *epoch 表示 shard 的第 c 块已到本地 HBM
epoch       : (1,) int32，4 字节对齐，常驻显存的「本次调用序号」快照
num_shards  : world_size，A 沿 M 维被切成几个 shard
first_shard : 本 rank 的 shard 编号——它的 tile 被最先调度（本地 shard 立即可用）
num_chunks  : 子 shard 到达粒度（默认 1），把一个 shard 再切成几块分别打标志
```

字段流转：

1. `AllGatherRunner` 在 `__init__` 时分配 `flags`（对称内存，全 rank 都能远程写）和 `epoch`（3×1 的本地张量）。
2. 每次 `gather()` 进入时，runner 构造一个 `AllGatherArguments`（见 [_ag_args](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L523-L530)），把它作为上下文 yield 出来。
3. 用户在 `with` 体内把这个 `ag_args` 透传给任意 quack GEMM。
4. GEMM 在编译/计划/启动各阶段把它转成内核侧的 `AgSchedulerArguments`（字段同名），最终送达设备内核。

为什么 `first_shard` 要传「本 rank 自己的编号」？因为每个 rank 的本地 shard 是「立即可用的」（就在自己显存里），远端 shard 要等网络传输。让每个 rank 从自己的 shard 开始算，就保证内核一启动就有数据可算，远端 shard 在「按环序」随后到达。这就是「shard-major 旋转」在主机侧的体现。

#### 4.1.3 源码精读

主机侧 `AllGatherArguments` 定义在 [quack/gemm.py:L360-L375](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L360-L375)：

```python
class AllGatherArguments(NamedTuple):
    """flags[shard * num_chunks + c] >= *epoch gates a tile's first TMA;
    num_shards/first_shard drive the shard-major rotated decode; num_chunks
    is the sub-shard arrival granularity."""
    flags: Tensor          # (num_shards * num_chunks,) int32, symmetric
    epoch: Tensor          # (1,) int32, 4B-aligned, device-resident epoch slot
    num_shards: int        # world_size: shards along M
    first_shard: int       # 本 rank —— 本地 shard 立即可用，远端按环序到达
    num_chunks: int = 1    # 子 shard 到达粒度
```

runner 构造它的工厂方法 [_ag_args](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L523-L530)，注意 `first_shard=self.rank`：

```python
def _ag_args(self, parity: int) -> AllGatherArguments:
    return AllGatherArguments(
        flags=self.flags,
        epoch=self.epoch[parity],
        num_shards=self.world_size,
        first_shard=self.rank,
        num_chunks=self.arrival_chunks,
    )
```

内核侧的孪生结构 `AgSchedulerArguments` 见 [quack/tile_scheduler.py:L180-L201](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L180-L201)，字段与主机侧一一对应，只是类型从 `Tensor`/`int` 换成 `cute.Tensor`/`Int32`。

#### 4.1.4 代码实践

**实践目标**：理解 `ag_args` 如何穿透公共 GEMM 入口。

**操作步骤**：

1. 打开 [quack/gemm.py:L442](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L442) 附近，找到 `gemm()` 签名里的 `ag_args: Optional[AllGatherArguments] = None` 参数。
2. 跟踪它如何被设为 `has_ag` 标志并参与编译键（搜索 `has_ag=ag_args is not None`）。
3. 读 [quack/gemm.py:L723-L726](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L723-L726) 的断言：AllGather+GEMM 要求 `dense`（非 varlen）、`persistent`、`split_k == 1`。

**需要观察的现象**：`ag_args is not None` 会让编译产物走一条「带 ag 字段」的调度器分支（`const_expr(params.ag is not None)`），这是一份不同的 cubin。

**预期结果**：你能解释「为什么 AllGather+GEMM 必须用持久化调度」——因为只有持久化内核的 load warp 会循环消费多个 tile，才能在 tile 之间插入到达门等待；非持久化（硬件直接派发、一 CTA 一 tile）没有这种循环点。

#### 4.1.5 小练习与答案

**练习 1**：如果 `num_chunks=1`，`flags` 数组有几个元素？门控粒度是什么？

**答案**：`num_shards` 个元素（`world_size` 个）。门控粒度是「整个 shard」——一个 shard 的数据全部到了才放行该 shard 的所有 tile。

**练习 2**：为什么 `first_shard` 由每个 rank 各自传入自己的 `rank`，而不是统一传 0？

**答案**：因为每个 rank 的「本地 shard」编号就是它自己的 `rank`，而本地 shard 是无需网络传输、立即可用的。各 rank 从自己的 shard 开始算，才能让 GEMM 一启动就有数据可消费；若统一传 0，则 rank≠0 的进程会先去等远端 shard 0，白白浪费本地立即可用的数据。

### 4.2 AllGatherRunner 传输半：旋转缓冲、epoch 与 CE 推送

本模块回答实践任务的前半句——「A 仍在被 copy stream 填充时就开始计算」是如何发生的。

#### 4.2.1 概念说明

`AllGatherRunner` 是「传输半」。它知道一切关于缓冲区、flags、stream 和 CE 调度的事，却对它喂的 GEMM 一无所知。它对外只暴露一个上下文管理器：

```python
with runner.gather(a_shard) as (a_full, ag_args):
    d = gemm(a_full, b, ..., ag_args=ag_args)  # 任意 quack GEMM
```

关键设计点：

1. **常驻对称缓冲区**：远端 rank 的数据通过 NVLink 读过来后，**绝不会缓存进本地 L2**（peer 读永不进本地 L2），所以必须在本地 HBM 物化一份。runner 分配一块所有 rank 共享的对称内存缓冲区，每个 peer 的 shard 直接写进接收方的这块缓冲。
2. **2 个旋转缓冲（双缓冲）**：消费者（GEMM）在下一次调用前不再需要 gathered A（训练在反向时重新 gather），所以深度大于 2 没有意义。用 `parity`（奇偶，i%2）选择缓冲：调用 i 用 `bufs[i%2]`，调用 i+1 用另一个，调用 i+2 回到第一个——此时调用 i 的 GEMM 必须已经结束。
3. **CE 推送（ce_push）传输**：唯一的内建传输。每个 rank（owner）用 CE 把自己的 shard **反向环序**发给每个 peer（先发给 rank-1，再 rank-2，…），然后远程写那个 peer 的 flag。反向环序让每个接收方在自己的「环旋转消费顺序」下最先拿到下一个需要的 shard。
4. **三条 stream**：ambient/compute stream（跑 epoch 自增、本地 staging、GEMM）、push_stream（跑发送突发）、barrier_stream（只跑复用屏障）。

#### 4.2.2 核心流程

单次调用的时间线（模块 docstring 里有完整图示）：

```
ambient/compute 流           push 流              barrier 流
─────────────────            ──────              ──────────
wait ev_reuse[p]             (在 GEMM 体之后才启用)
  (producer_acquire: 缓冲 B 全空)
epoch 自增; 快照 epoch[p]
本地 shard 拷进 B[me]
写本地 flag
record ev_shard_staged ────► wait ev_shard_staged
                             wait ev_reuse[p]
【GEMM 体, 内核门控自旋】      反向环, 每个 peer:
  flags[s] >= epoch[p] 时       CE 拷 B[me] ~~► peer 的 B[me]
  放行该 shard                  4B epoch[p] ~~► peer 的 flag[me]
record ev_gemm_end ───────────────────────────► wait ev_gemm_end
                                                 barrier(channel=p)
                                                 record ev_reuse[p]
```

「copy stream 填充 A 时就开始计算」就发生在中间这一段：GEMM 体在 ambient 流上**立即启动**（本地 shard 已 staging 完，本地 flag 已置），而远端 shard 的 CE 发送在 push_stream 上**并行进行**。内核里，load warp 算到本地 shard 的 tile 时（flag 已就绪）直接放行；算到远端 shard 的 tile 时，自旋等待——但此时 push_stream 正在以 CE 满速把那个 shard 推过来。计算与通信就这样重叠了。

**epoch 的设计**：epoch 是一个常驻显存的全局计数器（`epoch[2]`），每次调用 `g += 1` 再快照到 `epoch[parity]`。flag 写的是「本次调用 epoch 值的 4 字节拷贝」。这样设计让整个调用对 CUDA Graph 捕获安全——epoch 值在执行时从显存读，永不烘焙进主机，对任意重放次数都单调递增。

#### 4.2.3 源码精读

缓冲、flags、epoch 的分配在 [AllGatherRunner.__init__](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L340-L423)：

```python
# 恰好 2 个旋转缓冲，永久：消费者从不需要下次调用之后的 gathered A
self.bufs = symm_mem.empty((2, self.m_total, k), dtype=dtype, device=self.device)
self.handle = symm_mem.rendezvous(self.bufs, self.group.group_name)
...
# 到达 flags, shard 内 chunk 优先：flag[shard*chunks + c]
self.flags = symm_mem.empty(
    self.world_size * arrival_chunks, dtype=torch.int32, device=self.device
)
...
# 常驻显存 epoch: 行 0/1 = 每奇偶快照, 行 2 = 全局计数器
self.epoch = torch.zeros(3, 1, dtype=torch.int32, device=self.device)
```

epoch 自增与快照在 [_bump_epoch](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L595-L612)：

```python
def _bump_epoch(self, parity, stream):
    torch.add(self.epoch[2], 1, out=self.epoch[2])          # 全局行 g += 1（捕获安全的内核）
    _check(runtime.cudaMemcpyAsync(                          # 4B 快照 g -> epoch[parity]
        self.epoch[parity].data_ptr(), self.epoch[2].data_ptr(), 4, ...))
```

flag 写入（producer_commit）在 [_write_flag](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L568-L593)——用 CE 做 4 字节拷贝，**而非** torch 算子：

```python
def _write_flag(self, flags_base_ptr, parity, chunk, stream):
    # 为什么用 CE 不用 torch op：对 PEER 提交时这是承重的——
    # torch op 是 SM 内核，而 peer 的持久化 GEMM 在自旋等这个 flag 时
    # 占满了所有 SM（SM 写者会饿死在它后面）；CE 不需要 SM。
    _check(runtime.cudaMemcpyAsync(
        flags_base_ptr + 4 * (self.rank * self.arrival_chunks + chunk),
        self.epoch[parity].data_ptr(), 4, ... cudaMemcpyDeviceToDevice, stream))
```

主上下文管理器 [gather](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L641-L815) 的三段结构：进入时 `_stage_local`（staging 本地 shard + 写本地 flag），`yield` 出 GEMM 体执行的位置，退出时入队 CE 发送 + 复用屏障。退出时入队反向环发送（[L744-L764](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L744-L764)）：

```python
for step in range(1, self.world_size):
    dst_rank = (self.rank - step) % self.world_size   # 反向环序
    dst_base = self.handle.buffer_ptrs[dst_rank] + parity * self.buffer_bytes + my_off
    for c in range(self.arrival_chunks):
        _check(runtime.cudaMemcpyAsync(dst_base + c*chunk_bytes, src_base + c*chunk_bytes,
                                       chunk_bytes, ..., self.push_stream.cuda_stream))
        # 紧跟该 chunk 数据 memcpy 之后，远程写 peer 的 flag——释放对方门控
        self._write_flag(self.flags_handle.buffer_ptrs[dst_rank], parity, c, self.push_stream)
```

注意「数据 memcpy → flag 写」在同一 push_stream 上按序排列，所以 **stream 顺序天然保证 flag-implies-data（标志置位即意味数据已到）**，无需任何就绪握手。

#### 4.2.4 代码实践

**实践目标**：理解双缓冲复用安全与「数据先于 flag」的 stream 顺序保证。

**操作步骤**：

1. 读 [gather() 的退出段 L766-L775](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L766-L775)：复用屏障 `handle.barrier(channel=parity)` 聚合所有 rank 的「本缓冲的 GEMM 都结束了」，之后才 `record ev_reuse[parity]`。
2. 回到模块 docstring 的「Reuse safety」段（[L180-L184](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L180-L184)）：2 个旋转缓冲 + 一个离关键路径的跨 rank 屏障，门控调用 i+2 的 staging；epoch 单调，flags 从不复位。

**需要观察的现象**：`ev_reuse[parity]` 是「EMPTY（空）」语义——它由调用 i 的屏障在调用 i+2 之前被等待，证明「调用 i 的所有 GEMM 已结束，缓冲可被所有人重新 staging」。

**预期结果**：你能解释「为什么需要 2 个缓冲而非 1 个」——1 个缓冲会让调用 i+1 的发送（写 peer 的缓冲）必须等调用 i 的 GEMM 结束，丢失一整轮重叠；2 个缓冲让调用 i+1 的传输可以比最慢 rank 的 gemm_i 整整提前一轮。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_write_flag` 用 `cudaMemcpyAsync`（CE）而不是 `tensor.fill_` 或 `torch.add`？

**答案**：因为对 peer 的 flag 提交是「承重的」——peer 的持久化 GEMM 正在自旋等这个 flag，占满了所有 SM。SM 内核（`fill_`/`add`）会饿死在持久化 grid 后面甚至死锁；而 CE 是 DMA 引擎，不占任何 SM，能正常释放门控。此外统一用一种机制就只需一个可见性论证。

**练习 2**：epoch 为什么用一个全局递增计数器（MPI-RMA 风格），而不是 mbarrier 那样的 0/1 相位翻转？

**答案**：因为重叠是跨调用的——调用 i+1 的 flag 写入（值 = epoch_{i+1}）会在调用 i 的消费者仍在自旋（读 epoch_i）时合法到达。单调 GEQ 能吸收这种「深一层的重叠」；而 0/1 相位翻转会死锁（i+1 的 FULL 信号与 i 的同相位，无法区分）。

### 4.3 shard-major 旋转调度：到达顺序消费

#### 4.3.1 概念说明

传输半用反向环把 shard 推过来，接收方按什么顺序消费这些 shard？答案就是「shard-major 旋转解码」：调度器把线性的 work index 反线性化成 `(shard, shard 内 cluster)`，并且把 shard 维度按 `first_shard` 旋转，使「调度意义上的 shard 0」就是本 rank 的本地 shard。

直觉：每个 rank 像在一个环上，自己站在自己的位置（本地 shard），然后顺时针依次取下一个 rank 的 shard。这样：

- 本地 shard 立即可用 → GEMM 一启动就能算。
- 第 j 个远端 shard 来自 peer `(rank + j) % num_shards`，正好在前面 j 个 shard 被计算时陆续到达。
- 这与传输半的「反向环发送」对偶：owner 把 shard 先发给 rank-1，于是每个接收方在自己的旋转消费顺序下最先拿到下一个需要的 shard。

L2 swizzle（光栅化 + group + 蛇形）**只在每个 shard 的子问题内部**运行，不跨 shard。

#### 4.3.2 核心流程

调度器在 `create()` 里为 AllGather 构造一个 `AgParams`，把 swizzle 的几何量建立在「单个 shard 的子问题」上（[L284-L304](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L284-L304)）：

```
ag_nclusters_m_per_shard = 总 M 方向 cluster 数 // num_shards
problem_shape_ncluster_mn_swz = (ag_nclusters_m_per_shard, n_clusters_n)  # swizzle 用子形状
clusters_per_shard_fdd = FastDivmod(ag_nclusters_m_per_shard * n_clusters_n)
```

解码时（设备内核里），把线性 cluster id 拆成 `(ag_shard, cluster_id_in_shard)`，再环旋转：

```
ag_shard, cluster_id_in_problem = divmod(cluster_id_in_problem, clusters_per_shard_fdd)
ag_shard = ag_shard + first_shard
if ag_shard >= num_shards:
    ag_shard = ag_shard - num_shards          # 环绕
# 然后在 shard 子问题上跑普通 swizzle
cid_m, cid_n = swizzle(cluster_id_in_shard)
cid_m = cid_m + ag_shard * nclusters_m_per_shard   # 映射回全局 M 坐标
```

用数学表达旋转（模运算）：

\[
\text{physical\_shard} = (\text{schedule\_shard} + \text{first\_shard}) \bmod \text{num\_shards}
\]

调度意义上的 shard 0 映射到物理 shard `first_shard`（即本 rank），shard 1 映射到 `(first_shard+1) % num_shards`，依此类推。

#### 4.3.3 源码精读

解码逻辑在 [quack/tile_scheduler.py:L604-L614](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L604-L614)：

```python
ag_shard = Int32(0)
if const_expr(params.ag is not None):
    ag_shard, cluster_id_in_problem = divmod(
        cluster_id_in_problem, params.ag.clusters_per_shard_fdd
    )
    ag_shard = ag_shard + params.ag.first_shard
    if ag_shard >= params.ag.num_shards:
        ag_shard = ag_shard - params.ag.num_shards
cid_m, cid_n = self._swizzle_cta(cluster_id_in_problem, loc=loc, ip=ip)
if const_expr(params.ag is not None):
    cid_m = cid_m + ag_shard * params.ag.nclusters_m_per_shard
```

蛇形反射也要在 shard 内部反射（[L511-L514](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L511-L514)）——因为 swizzle 跑在 shard 的子问题上，蛇形应反射到 shard 的 M 范围而非全局 M 范围：

```python
ncluster_slow = (
    ... params.problem_shape_ncluster_mnl[0]
    if const_expr(params.ag is None)
    else params.ag.nclusters_m_per_shard   # AllGather: 蛇形在 shard 内反射
)
```

> **代价说明**：源码注释（[L590-L603](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L590-L603)）诚实记录了 shard-major 解码的代价——每个 shard 都要把全部 N 重扫一遍，导致 B 被每个 shard 重读一次（TP4 16384×4096 约 −3.6pp L2 命中率）。这是「按到达顺序消费」的固有代价，作者尝试了多种修复（跨 shard 蛇形续接、调 swizzle、B 的 cache hint、TMA 预取）均被实测否定，故保留。

#### 4.3.4 代码实践

**实践目标**：用一个具体例子手算 shard-major 旋转解码。

**操作步骤**：假设 `world_size=4`（num_shards=4），每个 shard 有 2 个 M 方向 cluster（`nclusters_m_per_shard=2`），N 方向 4 个 cluster。`first_shard=1`（即 rank 1）。

1. 总 cluster 数 = 4 shard × (2×4) = 32，但 swizzle 子问题 = 2×4 = 8。
2. 取 `cluster_id_in_problem = 9`：
   - `divmod(9, clusters_per_shard_fdd=8)` → `ag_shard=1, id_in_shard=1`。
   - `ag_shard = 1 + first_shard(1) = 2`（< 4，不环绕）→ 物理 shard 2。
   - `id_in_shard=1` 在 (2,4) 子问题里 swizzle → 比如沿 M 光栅化得 `cid_m=0, cid_n=1`。
   - 全局 `cid_m = 0 + 2(shard) * 2(per_shard) = 4`，`cid_n=1`。
3. 解读：这个 work index 落在物理 shard 2、该 shard 内的第 0 行第 1 列 cluster。

**需要观察的现象**：`cluster_id_in_problem` 从 0 增长时，先填满 schedule-shard 0（= 物理 shard `first_shard`，本地），再 schedule-shard 1（物理 `(first_shard+1)%4`），符合环序。

**预期结果**：你能手算出 rank 1 上 `cluster_id_in_problem = 0..7` 全部落在物理 shard 1（本地），`8..15` 落在物理 shard 2，`16..23` 落在物理 shard 3，`24..31` 落在物理 shard 0。

#### 4.3.5 小练习与答案

**练习 1**：为什么 L2 swizzle 只在 shard 子问题内部跑，而不是全局 M×N？

**答案**：因为 tile 是按 shard 顺序消费的——一个 shard 内的所有 N-tile 必须连续算完（趁这个 shard 的 A 还驻留在 HBM），才能让 N 方向的 B 复用集中在一段时间窗内。如果把 swizzle 跑在全局形状，会跨 shard 交错，破坏「一个 shard 内扫完所有 N」的到达顺序假设，且不改善实测的 DRAM 读放大。

**练习 2**：shard-major 解码的「−3.6pp L2 命中率」代价，作者为什么没有去掉？

**答案**：因为这个代价是「按到达顺序消费」的固有结果（每个 shard 重扫全部 N，导致 B 重读），而到达顺序消费正是重叠通信与计算的必要条件。多种修复尝试（跨 shard 蛇形、调 swizzle、B 的 evict_last hint、TMA 预取）均被 NCU 实测否定——长记分板 stall 时间线平坦，缺失已均匀分布且被流水线掩盖，预取无料可平滑。故保留并诚实记录。

### 4.4 load-warp 到达门：flags[...]>=epoch 如何避免读到未就绪的 tile

本模块回答实践任务的后半句——门控如何避免读到未就绪的 tile。

#### 4.4.1 概念说明

`ag_wait_m_tile` 是 AllGather+GEMM 流水线的 **consumer_wait**（消费者等待）：在 AB-load warp 发出一个 tile 的第一次 TMA 之前，先自旋，直到该 tile 所属 shard（及 chunk）的到达 flag 满足条件。

回顾 u3-l5 的 full/empty 词汇：这里 flags 是**细粒度、点对点的 FULL**（延迟关键——门控当前的消费），而 `ev_reuse` 是**粗粒度、集体的 EMPTY**（仅吞吐——门控两轮后的重填）。这种不对称是有意设计：FULL 必须精细到每个 shard/chunk，因为内核要知道「这块数据现在能不能读」。

门控的数学核心是**模运算 GEQ（modular greater-or-equal）**，借用 TransformerEngine 的 CHECK_IDS 技巧：满足条件当且仅当 `(val - epoch)` 在环绕 int32 算术下符号位为 0（即非负）。这样 flags 可以比 epoch 领先到 2³¹ 之远而不需要任何回绕重同步路径。

#### 4.4.2 核心流程

门控函数 `ag_wait_m_tile` 的逻辑：

```
输入: pid_m (tile 的 M 坐标), cluster_shape_m, last_gate (上次满足的 gate 索引, 初值 -1)
1. cid_m = pid_m // cluster_shape_m
2. shard = cid_m // nclusters_m_per_shard
3. chunk = (cid_m - shard*nclusters_m_per_shard) // nclusters_m_per_chunk
4. gate = shard * num_chunks + chunk
5. if gate == last_gate: 直接返回 (1 项满足缓存命中, 跳过 flag 读取)
6. epoch = load(ag.epoch, relaxed, scope=gpu)        # 一次 L2 热读
7. val   = load(ag.flags[gate], relaxed, scope=sys)  # 读 flag
8. while (val - epoch) < 0:                          # 模 GEQ 不满足则自旋
       val = load(ag.flags[gate], relaxed, scope=sys)
9. return gate                                        # 调用方回传作 last_gate
```

**为什么这能避免读到未就绪的 tile？** 三层保障：

1. **数据先于 flag（stream 顺序）**：传输半在每个 peer 的 push_stream 上先做数据 `cudaMemcpyAsync`，再写 flag（同一 stream 的后续操作）。CE 按 FIFO 服务队列，故 flag 被置位时数据 memcpy 已入队完成。
2. **flag 落 L2，TMA 读 L2 一致点**：flag 写是 CE 写，落在本 rank 的 L2 一致点（coherence point）。门控用 `relaxed` 读观察到 flag 后，TMA 随后读 A 的数据时，CE 写早已在 L2 可见——L1 陈旧性到不了 TMA 读。
3. **单调 epoch 区分本次与历史**：本次调用用 `epoch_parity`（= 全局 g 的快照），传输半只写「本次 epoch 值」到 flag。若某 shard 还没到（flag 仍是上一轮的旧值 `< epoch`），门控自旋不放行；直到本次 CE 把新 epoch 值写进 flag，`val - epoch` 变非负才放行。

满足条件用模运算表达：

\[
\text{satisfied} \iff \big((\text{val} - \text{epoch}) \bmod 2^{32}\big) < 2^{31}
\]

即在环绕 int32 下 `val >= epoch`。注意条件写成循环体 `while (val - epoch) < 0`，`< 0` 等价于「符号位置 1」，正是上式取反。

**1 项满足缓存（satisfied-gate cache）**：flags 在一次 launch 内单调，所以一旦某 gate 通过就永远通过；而调度在 `cid_m` 组内最快扫 N，连续 tile 绝大多数映射到同一个 `(shard, chunk)`。记住 `last_gate`，命中时跳过 `sys` 作用域的 flag 读取，省掉大多数 tile 的门控开销。

#### 4.4.3 源码精读

门控函数 [ag_wait_m_tile](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L123-L177)：

```python
@cute.jit
def ag_wait_m_tile(params, pid_m, cluster_shape_m, last_gate):
    cid_m = pid_m // cluster_shape_m
    shard = cid_m // params.ag.nclusters_m_per_shard
    chunk = (cid_m - shard * params.ag.nclusters_m_per_shard) // params.ag.nclusters_m_per_chunk
    gate = shard * params.ag.num_chunks + chunk
    if gate != last_gate:                       # 1 项满足缓存
        epoch = cute.arch.load(params.ag.epoch.iterator.llvm_ptr, Int32,
                               sem="relaxed", scope="gpu")
        ptr = params.ag.flags.iterator + gate
        val = cute.arch.load(ptr.llvm_ptr, Int32, sem="relaxed", scope="sys")
        while (val - epoch) < 0:                # 模 GEQ: 不满足则自旋
            val = cute.arch.load(ptr.llvm_ptr, Int32, sem="relaxed", scope="sys")
    return gate
```

`relaxed` 而非 `acquire` 的理由见 docstring（[L150-L159](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L150-L159)）：门控的数据由 TMA 读（在 L2 一致点取，CE 写已可见），L1 陈旧到不了 TMA；若 `acquire-sys` 会降级成 `LDG.STRONG.SYS + CCTL.IVALL`——每个 tile 都全 L1 失效，即便 flag 早置位也照做，纯属浪费。NCCL 的 CE 集合 flag 等待也用同样的 relaxed/volatile 模式。

设备内核的接入点（SM90，SM100/SM120 同构）在 [quack/gemm_sm90.py:L1060-L1083](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1060-L1083)，在持久化调度循环里、load warp 发出第一次 TMA 之前：

```python
ag_last_gate = Int32(-1)  # 1 项满足缓存
...
while work_tile.is_valid_tile:
    tile_coord_mnkl = work_tile.tile_idx
    # 只有 load warp 门控 —— MMA/epilogue warp 是 AB pipeline 的下游
    if const_expr(getattr(tile_sched_params, "ag", None) is not None):
        iket.range_push("ag_wait")
        ag_last_gate = ag_wait_m_tile(
            tile_sched_params, tile_coord_mnkl[0], self.cluster_shape_mnk[0], ag_last_gate
        )
        iket.range_pop()
    iket.range_push("tma_load")
    ...  # 此后才发 A 的 TMA
```

注意 `getattr(tile_sched_params, "ag", None)`：varlen 调度器的 Params 类根本没有 `ag` 字段，用 `getattr` 安全取默认 `None`，`const_expr` 在编译期折叠掉整个分支——故同一份内核源码既服务普通 GEMM 也服务 AllGather+GEMM，只是 cubin 不同。

#### 4.4.4 代码实践

**实践目标**：用一个数值例子走通门控判定，并理解「延迟到达」时门控如何保持正确。

**操作步骤**：

1. 设本次调用 `epoch = 5`（全局 g 自增后的快照）。本地 shard 的 flag 在 staging 时立即被写成 5。
2. 假设远端 shard 2 还没到——它的 flag 还是上一轮调用 i-2 写的旧值 3。
3. load warp 算到物理 shard 2 的某 tile：`gate = 2*num_chunks + chunk`，读 `val = flags[gate] = 3`。
4. 判定：`(val - epoch) = (3 - 5) = -2 < 0` → 自旋，不放行。
5. 与此同时 push_stream 把 shard 2 推过来，CE 写 `flags[gate] = 5`。
6. 下次读 `val = 5`：`(5 - 5) = 0 ≥ 0` → 放行，load warp 才发 TMA 读 A。

**需要观察的现象**：在 step 4 自旋期间，GEMM 的其他 warp（MMA/epilogue）并非闲置——它们在消费 AB pipeline 中**更早 shard**已就绪的 tile。这就是重叠的本质：等 shard 2 时，shard 0/1 的 tile 在被算。

**预期结果**：你能解释「为什么门控只放在 load warp，而不放 MMA warp」——因为 load warp 是 AB pipeline 的生产者（唯一发 TMA 读 A 的人），门控在生产源头拦截即可；MMA/epilogue warp 消费 pipeline 下游，只要 load warp 不往 pipeline 喂未就绪数据，它们自然安全。

#### 4.4.5 小练习与答案

**练习 1**：如果用普通的 `val >= epoch` 比较而不是模运算 GEQ，在 int32 回绕时会出什么问题？

**答案**：当 epoch 接近 int32 上限并回绕到小正数，而 flag 还停留在回绕前的大值时，普通 `>=` 会误判「已满足」（大值 ≥ 小值），门控放行读到上一轮的旧数据。模运算 GEQ 把 `(val - epoch)` 当环绕量看符号位，能正确处理「flag 比 epoch 领先到 2³¹」的情况，无需任何回绕重同步路径。

**练习 2**：1 项满足缓存（`last_gate`）为什么安全？

**答案**：因为 flags 在一次 launch 内单调——一旦某个 `(shard, chunk)` 的 flag 达到 epoch，就不会再回退（本调用内只增不改）。所以「曾经通过」的 gate 永远保持通过，缓存命中直接跳过读取是安全的。它只在通信受限的小角落有意义（计算受限时门控本来就在 producer_acquire 的松弛时间里顺带完成）。

## 5. 综合实践

把四个模块串起来，完成一个「源码阅读 + 多 rank 验证」的综合任务。

**任务**：跟踪一次 AllGather+GEMM 调用的完整生命周期，画出「数据流 + 控制流」的对应关系，并（如有 ≥2 张 GPU）跑通官方多 rank 测试。

**步骤 1（源码阅读型，必做）**：对照本讲四个模块，在纸上画一张时间线，标注：

- ambient 流上的：`_wait_reuse` → `_bump_epoch` → `_stage_local`（写本地 flag）→ **GEMM 启动** → `record ev_gemm_end`。
- push 流上的：`wait ev_shard_staged` → 反向环 `[CE 数据 memcpy, CE 写 peer flag]`。
- 内核内：load warp 算本地 shard（flag 已就绪，直接放行）→ 算远端 shard（`ag_wait_m_tile` 自旋直到 `flags[gate] >= epoch`）。
- 标出「GEMM 启动」与「远端 CE 发送」的时间重叠区间——这就是通信与计算重叠的来源。

**步骤 2（几何校验，必做）**：读 [validate_ag_geometry](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L452-L468)。解释为什么 shard/chunk 边界必须落在 `tile_M * cluster_M * num_chunks` 的整数倍上——否则一个 tile 会横跨两个 shard/chunk，对它用单个 flag 门控就不正确。用一个反例（如 shard_rows 不能被 `tile_M*cluster_M` 整除）说明会触发哪个断言。

**步骤 3（多 rank 运行，可选，待本地验证）**：若有 ≥2 张同型号 NVIDIA GPU（SM90+），可运行官方测试（见 [tests/test_distributed_gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_distributed_gemm.py)）：

```bash
# pytest 入口会拉起 torchrun 子进程，每 GPU 一个 rank
pytest tests/test_distributed_gemm.py -x
# 或直接用 torchrun（4 rank 示例）
torchrun --nproc_per_node=4 tests/test_distributed_gemm.py
```

测试里特别值得读的是 `delay_comm=True` 分支（[test_distributed_gemm.py:L78-L84](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_distributed_gemm.py#L78-L84)）：它在 `ag.push_stream` 上 `torch.cuda._sleep` 故意把传输推迟，使数据到达（及其 flag 写）provably 迟到，从而**强制门控进入自旋路径**。若门控有任何竞态，这个用例就会读到未就绪 tile 而数值出错——这正是回归测试编码的失败模式（数值错误而非仅形状）。

**预期结果**：你能用一句话说清「为什么 GEMM 不必等 AllGather 完成」——因为内核的 load warp 在每个 tile 的第一次 TMA 前用 `flags[shard*num_chunks+c] >= epoch` 门控，本地 shard 立即可用先算，远端 shard 按 shard-major 旋转顺序在 CE 推送下陆续到达，门控在每个 shard 到达时放行，于是计算与通信重叠。

## 6. 本讲小结

- **两半解耦**：AllGather+GEMM 分传输半（`AllGatherRunner`，拥有缓冲/flags/epoch/CE 调度）与内核半（持久化 GEMM + 调度器 + 到达门），通过极小的 `AllGatherArguments` 契约连接，使任意 quack GEMM 变体加一个 `ag_args` 关键字参数即可参与重叠。
- **`AllGatherArguments`**：`flags[shard*num_chunks+c] >= *epoch` 门控一个 tile 的第一次 TMA；`num_shards/first_shard` 驱动 shard-major 旋转解码；`num_chunks` 是子 shard 到达粒度。每个 rank 传 `first_shard=rank`，本地 shard 立即可用。
- **传输半的 CE 推送**：owner 用 CE 按反向环序把 shard 发给每个 peer，紧跟一次 4 字节 flag 写（同 stream 顺序保证 flag-implies-data）；CE 不占 SM，不会被自旋门控的持久化 GEMM 饿死。2 个旋转缓冲 + 离关键路径的跨 rank 屏障提供复用安全。
- **shard-major 旋转调度**：调度器把 work index 拆成 `(shard, shard 内 cluster)`，shard 按 `first_shard` 环旋转，L2 swizzle 只在 shard 子问题内跑；代价是每个 shard 重扫全部 N（B 被重读），作者实测多种修复均无效而保留。
- **load-warp 到达门**：`ag_wait_m_tile` 在 load warp 发第一次 TMA 前自旋，用模运算 GEQ（`(val-epoch)` 符号位判定）避免读到未就绪 tile；`relaxed` 读足够（数据由 TMA 在 L2 一致点读）；1 项满足缓存省掉大多数 tile 的 flag 读。
- **epoch 单调不回绕**：用全局递增的 device-resident 计数器而非 0/1 相位，吸收跨调用的深一层重叠，且对 CUDA Graph 捕获安全（epoch 值执行时从显存读，不烘焙进主机）。

## 7. 下一步学习建议

- **BlockScaledAllGatherRunner**：本讲只讲了密集 A。`quack/distributed/all_gather_gemm.py` 的 `BlockScaledAllGatherRunner`（[L818 起](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L818)）在同一套 flag 下额外传输 scale-factor 通道，建议结合 u7-l1（Blockscaled 操作数）阅读，理解「一组 flag 发布两个 payload（qdata + SFA）」如何由同 stream 顺序保证。
- **CUDA Graph 捕获的三处特例**：模块 docstring 的「CUDA graphs」段（[L197-L238](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/distributed/all_gather_gemm.py#L197-L238)）详述了跨调用流水线给图捕获带来的三个难题（捕获前记录的 event 不能 wait、悬空分支、主机值烘焙）及其解法，是进阶分布式图捕获的绝佳材料。
- **反向章节（未实现）**：docstring 末尾的「Training contract / Future」段指出 wgrad（`dW = dD^T @ A_full`）把 gathered 维变成归约维，门控需移到 k-loop 起点——这是未来扩展方向，可作为研究型阅读。
- **实测基准**：`benchmarks/benchmark_all_gather_gemm.py` 与 `benchmarks/benchmark_blockscaled_all_gather_gemm.py` 包含 TP2/4/8 的端到端测量协议，结合模块 docstring 的「Overhead model」段可理解 ~3.5%/7%/11-17% 的开销分解。
