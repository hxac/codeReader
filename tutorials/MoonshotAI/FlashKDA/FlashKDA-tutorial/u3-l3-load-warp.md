# LOAD warp：多张量 TMA 预取与事务字节预算

## 1. 本讲目标

上一讲（u3-l2）我们看完了 Kernel 2 的整体架构：192 线程被切成 4 个 MMA warp、1 个 LOAD warp、1 个 STORE warp，靠「装载流 + 存储流」两条流水线协作。本讲钻进其中一条腿——**LOAD warp（装载 warp）**，逐行精读它的 producer 循环。读完本讲，你应该能够：

1. 说出 LOAD warp 每处理一个 tile 所做的 5 个固定动作：`producer_acquire` → 取 barrier → 发 8 份 TMA 拷贝 → 推进流水线状态 → `producer_tail` 收尾。
2. 列出一个 stage 里 8 份 TMA 拷贝的完整清单（源、目的地、dtype、字节数），并解释 beta 为什么用 1D TMA 且要按 `& ~7` 向下对齐。
3. 手工计算 `kTmaTransactionBytes` 并验证它等于 17984 字节，理解「多张量共享一个 transaction barrier」的字节记账式完成语义。
4. 说明 `elect_one_sync` 与 K1 的 `threadIdx.x == 0` 两种「单线程发起 TMA」写法的差异。

## 2. 前置知识

- **TMA（Tensor Memory Accelerator，SM90 引入）**：一块独立的拷贝引擎。CPU/GPU 线程只发出一条指令（`cp.async.bulk.tensor`），描述符里预先编码了 gmem 形状、box 大小与 swizzle 模式；数据搬运由硬件异步完成，不占用寄存器，也不经过 L1 的通用访存路径。
- **mbarrier（内存屏障）与「事务字节」**：SM90 的 mbarrier 除了常见的「到达计数」外，还有一个**事务字节计数器**。`arrive_and_expect_tx(N)` 表示一次线程到达并声明「再等 N 字节的异步事务」；每条带完成语义的 TMA 拷贝结束时，会把实际搬运的字节数原子累加进去。两个计数都满足时，barrier 相位翻转。这是本讲的核心机制。
- **CUTLASS `PipelineTmaAsync`**：把「每 stage 一对 empty/full barrier」封装成生产者-消费者流水线：生产者 `producer_acquire`（等 stage 空闲 + 登记事务字节）→ TMA 拷贝挂到 full barrier → 消费者 `consumer_wait`（等 full 翻转）→ `consumer_release`（到达 empty barrier，归还 stage）。它的实现位于 CUTLASS submodule 的 `include/cutlass/pipeline/sm90_pipeline.hpp`（submodule 未检出时，按本讲的 mbarrier 语义理解即可，不依赖具体实现行号）。
- **承接前讲**：u3-l2 的 warp 角色划分与 `SharedStorageK2` 布局；u2-l5 的 TMA 描述符家族；u2-l8 的 workspace 位一致契约（K1 写下的比特 = K2 读到的比特）。

## 3. 本讲源码地图

| 文件 | 本讲关注范围 | 作用 |
| --- | --- | --- |
| `csrc/smxx/fwd_kernel2.cuh` | L189-L237、L320-L421、L434-L447、L735-L741 | K2 主体：warp 角色、pipeline 构造、LOAD warp 的 producer 循环（本讲主角）、消费侧对照 |
| `csrc/smxx/utils.cuh` | L63-L143 | `WorkspaceSizes` 字节常量、`WarpRole` 枚举、`make_load_pipeline` / `make_store_pipeline` 工厂 |
| `csrc/smxx/fwd_kernel1.cuh` | L158-L161、L200-L225、L345、L515-L527 | 对照组：K1 的单发 TMA 模式（`threadIdx.x == 0`）、同款 beta 对齐、workspace 写侧寻址 |
| `csrc/smxx/fwd_launch.cu` | L29-L31、L49-L59、L105-L114 | `kInputStages/kOutputStages` 常量、beta 的一维 gmem 布局、K2 的 8 个 LOAD 描述符 |

## 4. 核心概念与源码讲解

### 4.1 producer 循环与 stage 管理

#### 4.1.1 概念说明

在 K2 里，递推是**序列内串行**的：第 `t` 个 tile 的计算依赖第 `t-1` 个 tile 结束时的状态 `s_acc`。如果 MMA warp 每算完一个 tile 才开始搬下一个 tile 的数据，global memory 的几百纳秒延迟会完全暴露在关键路径上。

LOAD warp 的存在就是为了把「搬数据」从「算数据」里剥离出来：它提前把未来 tile 需要的全部输入搬进 smem 的多级缓冲（stage），MMA warp 一算完就能立刻在下一个 stage 上开工。装载流由 `PipelineTmaAsync<InputStages>` 管理，`InputStages = 3` 意味着 smem 里同时最多有 3 个 tile 的输入在「已填充 / 正在消费」状态。

这条流水线上只有一个生产者角色——LOAD warp，而且真正干活的只有其中**一条 lane**（由 `elect_one_sync` 选出）；消费者是 128 条 MMA 线程（4 个 warp）。

#### 4.1.2 核心流程

LOAD warp 的主体是一个 `t_tiles` 次循环，每个 tile 固定 5 步：

```text
load_write = 生产者起始状态（视为所有 stage 均空闲）
for t in 0 .. t_tiles-1:
    ① producer_acquire(load_write)
         等 empty barrier：该 stage 上一轮的 128 个消费者都已 release；
         随后 leader lane 对 full barrier 执行 arrive_and_expect_tx(17984)
    ② tma_barrier = producer_get_barrier(load_write)
         取得该 stage 的 transaction barrier（full barrier）
    ③ stage = load_write.index()      # 在 0..2 之间循环
       ws_idx = head_idx * total_tiles + tile_base + t   # workspace 寻址
    ④ 发 8 条 cute::copy(tma_xxx.with(*tma_barrier), …)  # 全部挂同一个 barrier
    ⑤ ++load_write                    # index+1，绕一圈后 phase 翻转
producer_tail(load_write)             # 排空流水线，让消费侧末尾的 wait 不悬挂
```

用时间轴看 `InputStages = 3` 时的重叠关系：

```text
tick   LOAD warp                     MMA warps
----   -------------------------     -------------------------
 0     acquire(s0) → 8×TMA           (等 s0)
 1     acquire(s1) → 8×TMA           wait(s0) → 计算 t0 …
 2     acquire(s2) → 8×TMA           计算 t0 …
 3     acquire(s0)：必须先等 MMA      计算 t0 完成 → release(s0)
       在 tick 0 那轮的 release(s0)
```

`PipelineState` 由 `index` 与 `phase` 两个量组成：`index = t % Stages` 选择用哪个 stage；每绕一圈 `phase` 翻转一次，用于区分「这一轮的翻转事件」和「上一轮的翻转事件」，防止流水线把陈旧的 barrier 完成误读为新的。生产者起始状态（`make_producer_start_state`）自带 `Stages` 个「空闲额度」，等价于把第一轮的 empty barrier 视为已经到达，所以前 3 次 acquire 不会等待。

#### 4.1.3 源码精读

**① warp 角色判定**：`kComputeThreads = 128`，warp 0-3 是 MMA；`warp_id == 4` 是 LOAD_QKG，`warp_id == 5` 是 STORE。这决定了后面所有 `if (warp_role == …)` 分支谁会执行。

[fwd_kernel2.cuh:L189-L197](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L189-L197)
```cpp
int warp_id = threadIdx.x / kWarpSize;
WarpRole warp_role = WarpRole::NonParticipant;
if (warp_id < kComputeThreads / kWarpSize) {
    warp_role = WarpRole::MMA;
} else if (warp_id < kComputeThreads / kWarpSize + 1) {
    warp_role = WarpRole::LOAD_QKG;
} else if (warp_id < kComputeThreads / kWarpSize + 2) {
    warp_role = WarpRole::STORE;
}
```

**② 构造装载流**：`make_load_pipeline` 的实参依次是 `kTmaTransactionBytes`（本讲 4.3 的主角）、角色、`num_producers = 1`、`num_consumers = kComputeThreads = 128`。

[fwd_kernel2.cuh:L199-L213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L199-L213)

工厂函数里做角色映射，并且在 LOAD warp 内部用 `elect_one_sync` 选出 leader——只有这条 lane 会在 `producer_acquire` 内部执行 `arrive_and_expect_tx`（登记事务字节）。`Shape<_1,_1>` 表示 cluster 尺寸 1×1，即不做 TMA 多播。

[utils.cuh:L86-L116](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L86-L116)
```cpp
if (warp_role == WarpRole::LOAD_QKG) {
    role = Pipeline::ThreadCategory::Producer;
    is_leader = cute::elect_one_sync();
} else if (warp_role == WarpRole::MMA) {
    role = Pipeline::ThreadCategory::Consumer;
}
params.transaction_bytes = transaction_bytes;
```

**③ 循环头**：`producer_acquire` → 取 barrier → 读 stage 号 → 算 `ws_idx`。注意 `ws_idx = head_idx * total_tiles + tile_base + t` 正是 u2-l8 讲过的 workspace 契约读侧公式（K1 写侧用全局 tile 号，K2 读侧用 `tile_base + t`，二者指向同一格）。

[fwd_kernel2.cuh:L346-L351](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L346-L351)
```cpp
for (int t = 0; t < t_tiles; ++t) {
    load_pipeline.producer_acquire(load_write);
    using LoadBarrierType = typename LoadPipeline::ProducerBarrierType;
    LoadBarrierType* tma_barrier = load_pipeline.producer_get_barrier(load_write);
    int stage = load_write.index();
    int ws_idx = head_idx * total_tiles + tile_base + t;
```

**④ 循环收尾**：每轮末尾 `++load_write` 推进状态；整个循环结束后 `producer_tail` 把流水线排空——它对生产者已推进过的每个 stage 再做一次「只等待、不登记新事务」的 acquire（等待消费者 release），保证消费者在序列末尾的 `consumer_wait` 不会永远等不到数据。

[fwd_kernel2.cuh:L419-L421](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L419-L421)
```cpp
    ++load_write;
}
load_pipeline.producer_tail(load_write);
```

**⑤ 消费侧对照**（理解 stage 的生命周期闭环）：MMA warp 在计算前 `consumer_wait`，读的是同一个 stage 号；计算完成后先 `fence_view_async_shared`（让本 warp 的 generic-proxy 写对 async proxy 可见，供 STORE warp 的 TMA 读取），再 `consumer_release` 归还 stage——这正是下一轮 `producer_acquire` 所等待的 empty barrier 到达。

- 消费等待：[fwd_kernel2.cuh:L434-L439](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L434-L439)（`load_pipeline.consumer_wait(load_read); int load_stage = load_read.index();`）
- 释放：[fwd_kernel2.cuh:L735-L741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L735-L741)（`fence_view_async_shared(); store_pipeline.producer_commit(out_write); load_pipeline.consumer_release(load_read); ++load_read;`）

**⑥ `elect_one_sync` vs `threadIdx.x == 0`**：K2 在循环外算一次 `bool lane_predicate = cute::elect_one_sync();`（[fwd_kernel2.cuh:L237](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L237)），整个 LOAD warp 体被 `if (warp_role == WarpRole::LOAD_QKG && lane_predicate)` 包住（[fwd_kernel2.cuh:L324](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L324)）——即只有被选中的那条 lane 发起全部 8 条拷贝。`elect.sync` 是 SM90 的硬件指令，在 warp 的活跃 lane 中选出恰好一条。对照 K1（无 warp 专用化），单发 TMA 由 CTA 的线程 0 负责，源码注释还特意点出了这个区别：

[fwd_kernel1.cuh:L200-L206](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L200-L206)
```cpp
// --- TMA load inputs (single-shot, no pipeline)
// Only thread 0 issues TMA loads (not elect_one_sync which is per-warp)
if (threadIdx.x == 0) {
    ...
    shared_storage.tma_load_barrier.arrive_and_expect_tx(kTmaTransactionBytes);
```

顺带注意 `make_load_pipeline` 里的 `is_leader` 与 kernel 里的 `lane_predicate` 是两次独立的 `elect_one_sync`，它们都在 load warp 全活跃时调用，选出的是同一条 lane（实践中为最低编号活跃 lane）——所以「登记事务字节的线程」与「发拷贝的线程」是同一条，但语义上二者并不需要是同一线程：barrier 是 warp 共享的资源。

#### 4.1.4 代码实践

**实践目标**：用一个纯 Python 的离散事件模型，验证「stage 数 S 能掩盖多大的消费者延迟 L」，从直觉上理解 `kInputStages = 3` 的意义。

**操作步骤**：保存以下脚本为 `pipeline_sim.py`（示例代码，放在仓库任意临时位置，不要提交）：

```python
def simulate(S, L, T):
    """S=stage 数，L=消费者每 tile 耗时，T=tile 总数。
    生产者每 tick 发起一个 tile 的装载；消费者按序消费，每个 tile 耗时 L tick，
    消费完毕才 release 对应 stage。返回生产者总等待 tick 数。"""
    free_at = [0] * S      # 各 stage 被释放的时刻（0 = 起始即空闲，对应 producer start state）
    con_free = 0           # 消费者空闲时刻
    prev_fill = -1
    stalls = 0
    for t in range(T):
        s = t % S
        earliest = prev_fill + 1 if t > 0 else 0
        if free_at[s] > earliest:          # producer_acquire 需要等 stage 归还
            stalls += free_at[s] - earliest
            earliest = free_at[s]
        prev_fill = earliest               # 填入 stage s（TMA 发出即离开）
        start = max(earliest, con_free)    # consumer_wait + 按序消费
        con_free = start + L
        free_at[s] = con_free              # consumer_release
    return stalls

for L in (1, 2, 4):
    for S in (1, 2, 3):
        print(f"S={S}, L={L}, T=8 -> stalls={simulate(S, L, 8)}")
```

**需要观察的现象**：L=1（消费者与生产者同速）时任何 S 都不等待；L=2 时等待次数随 S 增大而减少。

**预期结果**（模型是确定性的，可直接推演）：L=1 时三个 S 全为 0；L=2、T=8 时 `S=1 → 7`、`S=2 → 5`、`S=3 → 3`。也就是说每加深一级缓冲，大约多掩盖一个 L 的延迟；S=1 时流水线退化为完全串行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `InputStages` 改成 1，LOAD warp 何时首次阻塞？
**答案**：`t = 0` 时生产者起始状态允许立即填 s0；`t = 1` 起，每次 `producer_acquire` 都必须先等 MMA 把上一个 tile 消费完并 `consumer_release`。装载与计算完全串行化，TMA 延迟全部暴露。

**练习 2**：为什么 `producer_acquire` 必须每个 tile 调一次，而不能整个循环只调一次？
**答案**：它一次做两件事——等 stage 空闲（empty barrier）以及让 leader 对该 stage 的 full barrier 执行 `arrive_and_expect_tx(17984)`。第二轮若不重新登记，事务字节计数不会复位：要么 barrier 永不翻转（消费者挂死），要么沿用旧计数导致语义错乱。每填一个 stage 都要重新走一遍完整的登记。

**练习 3**：`producer_tail` 不调用会发生什么？
**答案**：消费者按序对每个 tile 执行 `consumer_wait`。若生产者在循环尾没有对已推进的 stage 做「等待消费完成」的收尾，某些流水线实现下末端 stage 的状态无法收束，消费者的最后一次 wait 可能悬挂。`producer_tail` 是生产者对「我不再生产了」的正式声明。

### 4.2 一个 stage 的 8 次 TMA 拷贝清单

#### 4.2.1 概念说明

K2 每 tile 需要的全部输入恰好是 8 份张量：

- **2 份原始输入**：`v`（当前 tile 的值向量块）和 `beta`（写入强度 logits）——直接来自用户传入的 gmem；
- **6 份 workspace 中间量**：`k_decayed`、`q_decayed`、`k_restored`、`g_total`、`INV`、`Mqk`——来自 K1 写出的 workspace（u2-l8 的契约）。

为什么不能合并成更少的拷贝？workspace 在 gmem 里是**分离数组**（separated arrays：同一种量按 `[H × total_tiles]` 连续排布，六段各自独立），且六段的 dtype、形状、smem 布局互不相同，每种需要独立的 TMA 描述符；v 与 beta 又是另外两个 gmem 张量。所以 8 份是当前数据布局下的自然下限。这份清单也正好是 `InputStorage` 的 8 个成员（[fwd_kernel2.cuh:L84-L93](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L84-L93)）。

#### 4.2.2 核心流程

一个 stage 的 8 份拷贝（CHUNK=16、D=128）：

| # | 源（gmem） | 目的地（smem） | 元素形状 | dtype | 字节 |
| --- | --- | --- | --- | --- | --- |
| 1 | v，布局 (H, T_total, D) | `input[stage].v` | 16×128 | bf16 | 4096 |
| 2 | beta，一维 (H·T_total) | `input[stage].beta` | 32（对齐窗口） | bf16 | 64 |
| 3 | ws k_decayed，(n_ht, 16, 128) | `input[stage].k_decayed` | 16×128 | bf16 | 4096 |
| 4 | ws q_decayed，(n_ht, 16, 128) | `input[stage].q_decayed` | 16×128 | bf16 | 4096 |
| 5 | ws k_restored，(n_ht, 16, 128) | `input[stage].k_restored` | 16×128 | bf16 | 4096 |
| 6 | ws g_total，(n_ht, 128) | `input[stage].g_total` | 128 | fp32 | 512 |
| 7 | ws INV，(n_ht, 16, 16) | `input[stage].INV` | 16×16 | bf16 | 512 |
| 8 | ws Mqk，(n_ht, 16, 16) | `input[stage].Mqk` | 16×16 | bf16 | 512 |
| | | | | **合计** | **17984** |

其中 `n_ht = H × total_tiles`。注意第 3-8 项合计 13824 字节，恰等于 `WorkspaceSizes::kPerTile`（K1 每 tile 写出的 workspace 字节，[utils.cuh:L63-L77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L63-L77)）；一个 stage 再加上 v（4096）与 beta 窗口（64）就是 17984。

**beta 的一维对齐技巧**是本模块的重点。host 侧把 beta 从 `[T, H]` 转置成每 head 连续的 `[H, T]`（u2-l2），kernel 里再把它当作长度 `H * T_total` 的一维数组寻址。TMA 要求 gmem 起始地址 16 字节对齐；bf16 每元素 2 字节，8 个元素一组正好 16 字节，于是元素下标按 `& ~7` 向下对齐。但真实起点可能比对齐起点最多靠后 7 个元素，本 tile 实际需要 16 个元素（CHUNK=16），最坏要覆盖到 `对齐起点 + 7 + 16 = 23` 处，所以 smem 里加载一个 32 元素（64 字节）的窗口必然罩住所需的 16 个。写成公式：设

\[ \text{beta\_linear} = 8q + r, \quad r \in \{0,1,\dots,7\} \]

则加载区间为 \([8q,\ 8q+32)\)，消费区间为 \([8q+r,\ 8q+r+16)\)，由 \(r + 16 \le 23 < 32\) 知消费区间恒被覆盖。消费端用 `beta_smem_offset = beta_linear & 7`（即 \(r\)）找回真实起点。

#### 4.2.3 源码精读

**① gmem 逻辑张量与 CTA 切片**：循环外一次性构造 8 个 gmem 逻辑张量（`get_tma_tensor` 用 TMA 描述符里的形状/步长还原坐标→地址的映射）和各自的 CTA 切片。

[fwd_kernel2.cuh:L324-L336](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L324-L336)
```cpp
if (warp_role == WarpRole::LOAD_QKG && lane_predicate) {
    Tensor g_v = tma_load_v.get_tma_tensor(make_shape(H, T_total, D));
    Tensor g_beta = tma_load_beta.get_tma_tensor(make_shape(H * T_total));
    // Workspace gmem tensors
    auto g_ws_kd = tma_load_ws_kd.get_tma_tensor(make_shape(H * total_tiles, CHUNK, D));
    ...
    LoadPipelineState load_write = cutlass::make_producer_start_state<LoadPipeline>();
```

**② 拷贝 1：v**。源坐标 `(head_idx, bos + t*CHUNK, 0)` 定位到当前 tile 的 16×128 块，拷贝时用 `.with(*tma_barrier)` 把这条 TMA 挂到本 stage 的事务 barrier 上——这就是「8 条拷贝共享一个 barrier」的写法。

[fwd_kernel2.cuh:L353-L359](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L353-L359)
```cpp
// TMA load v
auto v_off = g_v.layout()(head_idx, int(bos) + t * CHUNK, 0);
Tensor g_v_tile = make_tensor(g_v.data() + v_off,
    make_layout(make_shape(Int<1>{}, Int<CHUNK>{}, Int<D>{}), stride(g_v.layout())));
Tensor s_v_tile = make_tensor(make_smem_ptr(shared_storage.input[stage].v.begin()), TMAVOLayout{});
cute::copy(tma_load_v.with(*tma_barrier),
    cta_tma_load_v.partition_S(g_v_tile), cta_tma_load_v.partition_D(s_v_tile));
```

**③ 拷贝 2：beta（1D、对齐）**。注意三行连续的小算法：线性下标 → `& ~7` 对齐 → 构造 32 元素的源/目的张量。smem 目的布局 `TMABetaSmemLayout` 就是 32 元素连续（无 swizzle、无哑模式，见 [fwd_kernel2.cuh:L41](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L41)），因为 beta 随后只被 MMA warp 按标量逐元素读取（[fwd_kernel2.cuh:L586-L587](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L586-L587)），不需要 LDSM/swizzle。

[fwd_kernel2.cuh:L361-L368](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L361-L368)
```cpp
// TMA load beta (1D)
int beta_linear = head_idx * T_total + (int(bos) + t * CHUNK);
int beta_aligned = beta_linear & ~7;
auto beta_off = g_beta.layout()(beta_aligned);
Tensor g_beta_tile = make_tensor(g_beta.data() + beta_off, BetaSmemLayout{});
Tensor s_beta_tile = make_tensor(make_smem_ptr(shared_storage.input[stage].beta.begin()), TMABetaSmemLayout{});
cute::copy(tma_load_beta.with(*tma_barrier), ...);
```

**④ 拷贝 3-8：六份 workspace**。六段代码结构完全相同：以 `ws_idx` 为首坐标，从分离数组中取一个 tile，写入带 swizzle 的 smem 布局。这里只列 k_decayed 一段（其余五行见下方链接）：

- k_decayed：[fwd_kernel2.cuh:L370-L377](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L370-L377)
- q_decayed：[fwd_kernel2.cuh:L378-L385](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L378-L385)
- k_restored：[fwd_kernel2.cuh:L386-L393](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L386-L393)
- g_total（fp32、128 元素）：[fwd_kernel2.cuh:L394-L401](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L394-L401)
- INV（16×16）：[fwd_kernel2.cuh:L402-L409](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L402-L409)
- Mqk（16×16）：[fwd_kernel2.cuh:L410-L417](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L410-L417)

```cpp
auto off = g_ws_kd.layout()(ws_idx, 0, 0);
Tensor g_tile = make_tensor(g_ws_kd.data() + off,
    make_layout(make_shape(Int<1>{}, Int<CHUNK>{}, Int<D>{}), stride(g_ws_kd.layout())));
Tensor s_tile = make_tensor(make_smem_ptr(shared_storage.input[stage].k_decayed.begin()), TMAVOLayout{});
cute::copy(tma_load_ws_kd.with(*tma_barrier), cta_ws_kd.partition_S(g_tile), cta_ws_kd.partition_D(s_tile));
```

**⑤ 描述符与 gmem 布局（launch 侧）**：8 个 LOAD 描述符在 `fwd_launch.cu` 里成对出现——K1 的六个 STORE 与 K2 的六个 LOAD 同型同源（位一致契约）；beta 的一维布局 `[H*T]` 在这里定义。

- beta 一维 gmem 布局与张量：[fwd_launch.cu:L51-L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L51-L59)（`auto beta_gmem_layout = make_layout(make_shape(H * T_total));`）
- K2 的 8 个 LOAD 描述符：[fwd_launch.cu:L105-L114](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L105-L114)

**⑥ 消费端取回 beta**：MMA warp 用 `beta_smem_offset = (...) & 7` 从 32 元素窗口里跳到本 tile 的真实 16 个元素——生产端的 `& ~7` 与消费端的 `& 7` 是同一手法互补的两半。

- K2 消费端：[fwd_kernel2.cuh:L447](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L447)（`int beta_smem_offset = (head_idx * T_total + int(bos) + t * CHUNK) & 7;`）
- K1 生产端同款对齐：[fwd_kernel1.cuh:L222-L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L222-L225)；K1 消费端同款取余：[fwd_kernel1.cuh:L345](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L345)

一个自然的疑问：窗口末端会不会越界？最后一个 head 的最后一个 tile，`beta_aligned + 32` 最多超出张量末端约 9 个元素。TMA 硬件按描述符维度做边界检查、越界部分补零，因此不会出错，且这些补零元素从不会被消费（消费区间只到 `r + 15`）。

#### 4.2.4 代码实践

**实践目标**：验证 beta 的 32 元素对齐窗口在任意 `beta_linear` 下都能覆盖所需的 16 个元素。

**操作步骤**：运行以下脚本（示例代码）：

```python
import random

H, T_total = 96, 8192
ok_cover = ok_align = True
worst = 0
for _ in range(100_000):
    bl = random.randrange(0, H * T_total - 16)   # 任意合法 tile 起点
    r = bl & 7
    aligned = bl & ~7
    ok_align &= (aligned % 8 == 0) and (aligned * 2 % 16 == 0)  # 16B 对齐
    ok_cover &= (aligned <= bl) and (bl + 16 <= aligned + 32)   # 窗口覆盖
    worst = max(worst, bl + 16 - aligned)        # 实际触及的最大跨度
print("对齐 OK:", ok_align, "覆盖 OK:", ok_cover, "最大触及跨度:", worst)
```

**需要观察的现象**：两个布尔量恒为 True；最大触及跨度为 23（当 `r = 7` 时，`7 + 16 = 23`）。

**预期结果**：`对齐 OK: True 覆盖 OK: True 最大触及跨度: 23`。23 ≤ 32，说明 32 元素窗口是最坏情形下的安全上界。

#### 4.2.5 小练习与答案

**练习 1**：`beta_linear = 9973` 时，aligned、窗口区间、`beta_smem_offset` 各是多少？
**答案**：`9973 = 8×1246 + 5`，故 `aligned = 9973 & ~7 = 9968`；窗口为元素区间 \([9968, 10000)\)（64 字节）；`beta_smem_offset = 9973 & 7 = 5`；本 tile 消费 smem 下标 5..20（共 16 个），落在窗口内。

**练习 2**：为什么窗口取 32 个元素而不是恰好 16 或 24？
**答案**：对齐后真实起点最多偏移 7 个元素，16 个所需元素最坏触及跨度 23；任何 ≥ 23 且满足 16 字节对齐的窗口都正确。32（64 字节）是向上取整到 2 的幂的实现选择，同时就是 `BetaSmemLayout` 的 cosize（`Layout<Shape<Int<32>>>`）。取 16 会丢元素（16 < 23），取 24 虽然覆盖但不是常规的 box 粒度。

**练习 3**：如果把 8 份拷贝里的 INV 拷贝删掉，运行时会发生什么？
**答案**：事务字节预算仍是 17984，但实际只搬运 17472 字节——mbarrier 的事务计数永远达不到登记值，full barrier 不翻转，MMA warp 的 `consumer_wait` 挂死（表现为 kernel 不返回）。这正是下一节要讲的「预算必须精确」。

### 4.3 事务字节预算 kTmaTransactionBytes

#### 4.3.1 概念说明

把 8 条 TMA 拷贝挂到同一个 barrier 上，靠的是 mbarrier 的**字节记账**完成语义：

1. leader lane 在 `producer_acquire` 内执行 `arrive_and_expect_tx(kTmaTransactionBytes)`：到达计数 +1，并声明「等待 N 字节事务」；
2. 每条 `.with(barrier)` 的 TMA 拷贝完成时，硬件把本次实际搬运字节数累加进该 barrier 的事务计数（`complete_tx::bytes`）；
3. 到达计数满足 **且** 事务字节累计达到 N 时，barrier 相位翻转，`consumer_wait` 返回。

由此得到一条**硬约束：`kTmaTransactionBytes` 必须精确等于一个 stage 内 8 条拷贝的字节总和**。

- **少算**（预算 < 实际）：barrier 提前翻转，MMA 在部分数据还没落地时就开始读——静默的错误结果，比崩溃危险得多；
- **多算**（预算 > 实际）：计数永远差一截，barrier 不翻转——消费者挂死。

「8 份共享一个 barrier」的收益：一次 `consumer_wait` 覆盖全部 8 份数据、省掉 7 套 barrier 的初始化与等待、与「8 份数据同一 tile 一起消费」的生命周期天然对齐。前提正是这份编译期预算把清单固化了下来。

#### 4.3.2 核心流程

预算公式的每一项对应 4.2 清单的一行或数行：

\[ \underbrace{2048 \times 2}_{v,\ 4096} + \underbrace{32 \times 2}_{beta,\ 64} + \underbrace{3 \times 2048 \times 2}_{kd/qd/kr,\ 12288} + \underbrace{128 \times 4}_{g\_total,\ 512} + \underbrace{2 \times 256 \times 2}_{INV/Mqk,\ 1024} = 17984 \text{ 字节} \]

其中各 cosize 的来历：

- `cosize(MMALayout) = 16 × 128 = 2048`：K_INTER atom 铺满 (16,128) 无 padding；可用 `WorkspaceSizes::kKDecayed = CHUNK*D*2 = 4096` 字节反推（[utils.cuh:L70](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L70)）；`VOLayout` 是 `MMALayout` 的别名（[fwd_kernel2.cuh:L21](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L21)），cosize 同为 2048；
- `cosize(BetaSmemLayout) = 32`（[fwd_kernel2.cuh:L23](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L23)）；
- `cosize(GTotalLayout) = D = 128`（[fwd_kernel2.cuh:L34](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L34)）；
- `cosize(LMLayout) = 16 × 16 = 256`（由 `kINV = CHUNK*CHUNK*2 = 512` 字节反推，[utils.cuh:L74](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L74)）。

三条交叉验证恒等式：

- 一个 stage 的 TMA 事务字节 = `InputStorage` 各成员字节数之和（TMA 恰好把整个 stage 填满）；
- \( 17984 = \underbrace{13824}_{\text{K1 每 tile 写出（kPerTile）}} + \underbrace{4096}_{v} + \underbrace{64}_{beta\ 窗口} \)；
- 对照 K1 的单发预算：\( 3 \times 2048 \times 2 + 64 + 512 = 12864 \) 字节（q + k + g_bf16 三份 bf16、beta、fp32 dt_bias）。

#### 4.3.3 源码精读

**① K2 的预算定义**：注意公式被 `#ifndef TMA_DISABLE_ALL` 包住，宏关闭时只剩保底的 `0u`——这让变量在消融编译下仍有定义。注释逐项列出了 8 份张量。

[fwd_kernel2.cuh:L172-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L172-L181)
```cpp
// Transaction bytes: v + beta + k_decayed + q_decayed + k_restored + g_total + INV + Mqk
constexpr uint32_t kTmaTransactionBytes =
#ifndef TMA_DISABLE_ALL
    uint32_t(cute::cosize_v<VOLayout>) * uint32_t(sizeof(BF16)) +
    uint32_t(32) * uint32_t(sizeof(BF16)) +                    // beta (bf16, sigmoid fused)
    uint32_t(cute::cosize_v<MMALayout>) * uint32_t(sizeof(BF16)) * 3 +  // kd, qd, kr
    uint32_t(cute::cosize_v<GTotalLayout>) * uint32_t(sizeof(float)) +  // g_total
    uint32_t(cute::cosize_v<LMLayout>) * uint32_t(sizeof(BF16)) * 2 +   // INV, Mqk
#endif
    0u;
```

**② 预算的使用点**：作为 `transaction_bytes` 传入 pipeline 构造，最终落在每次 `producer_acquire` 的 `arrive_and_expect_tx` 上。

[fwd_kernel2.cuh:L202-L206](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L202-L206)
```cpp
LoadPipeline load_pipeline = make_load_pipeline<InputStages>(
    shared_storage.load_pipeline,
    kTmaTransactionBytes,
    warp_role, 1, kComputeThreads
);
```

**③ K1 的同款预算（对照）**：K1 是单发模式（无流水线），同一个 barrier 一次性登记五份拷贝（q、k、beta、g_bf16、dt_bias）共 12864 字节——聚合字节记账的另一个用例。

[fwd_kernel1.cuh:L158-L161](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L158-L161)
```cpp
constexpr uint32_t kTmaTransactionBytes =
    uint32_t(cute::cosize_v<QKLayout>) * uint32_t(3 * sizeof(BF16)) +  // q + k + g_bf16
    uint32_t(32) * uint32_t(sizeof(BF16)) +  // beta (bf16, sigmoid fused)
    uint32_t(D) * uint32_t(sizeof(float));  // dt_bias
```

**④ 初始状态载入（第三个用例）**：K2 开头把 `initial_state` 载入 `state_acc` 也走同一机制，用的是独立的 `state_acc_tma_barrier`，预算 `kStateTransactionBytes = cosize(StateSmemLayout) × sizeof(BF16) = 16384 × 2 = 32768` 字节（fp32 状态则为 65536）。

[fwd_kernel2.cuh:L243-L249](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L243-L249)
```cpp
constexpr uint32_t kStateTransactionBytes = cute::cosize_v<StateSmemLayout> * sizeof(BF16);
shared_storage.state_acc_tma_barrier.init(1);
cutlass::arch::fence_barrier_init();
shared_storage.state_acc_tma_barrier.arrive_and_expect_tx(kStateTransactionBytes);
```

**⑤ smem 侧对账**：`InputStorage` 的 8 个成员的 `cosize × sizeof` 之和恰好等于 17984——「预算 = 一个 stage 的物理容量」并非巧合，而是 TMA 把整个 stage 一次填满的直接体现。

[fwd_kernel2.cuh:L84-L93](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L84-L93)
```cpp
struct InputStorage {
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<VOLayout>> v;
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<BetaSmemLayout>> beta;
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> k_decayed;
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> q_decayed;
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> k_restored;
    alignas(128) cute::ArrayEngine<float, cute::cosize_v<GTotalLayout>> g_total;
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<LMLayout>> INV;
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<LMLayout>> Mqk;
};
```

#### 4.3.4 代码实践

**实践目标**：手工计算并复核 `kTmaTransactionBytes`。

**操作步骤**：

1. 先在纸上算：`cosize(VOLayout)*2 + 32*2 + 3*cosize(MMALayout)*2 + 128*4 + 2*cosize(LMLayout)*2`（CHUNK=16、D=128）。
2. 再用脚本复核（示例代码）：

```python
COSIZE = {
    "VOLayout": 16 * 128,     # MMALayout 别名，16×128
    "BetaSmem": 32,           # Layout<Shape<Int<32>>>
    "MMALayout": 16 * 128,
    "GTotalLayout": 128,      # D
    "LMLayout": 16 * 16,
}
total = (COSIZE["VOLayout"] * 2 + 32 * 2 + 3 * COSIZE["MMALayout"] * 2
         + 128 * 4 + 2 * COSIZE["LMLayout"] * 2)
kPerTile = 3 * (16 * 128 * 2) + 128 * 4 + 2 * (16 * 16 * 2)   # WorkspaceSizes 常量
print(total, total == kPerTile + 16 * 128 * 2 + 64, kPerTile)
```

**需要观察的现象**：脚本应证明恒等式 `total == kPerTile + 4096 + 64` 成立。

**预期结果**：`17984 True 13824`。若你手算的结果不是 17984，回头检查是否把 beta 当成了 16 个元素（正确是 32）或把 g_total 当成了 bf16（正确是 fp32）。

#### 4.3.5 小练习与答案

**练习 1**：有人把 beta 的 TMA 窗口从 32 改成 16 个元素，但忘了改预算公式。预算值与实际字节数各是多少？会发生什么？
**答案**：窗口少搬 \( (32-16) \times 2 = 32 \) 字节，实际搬运 \( 17984 - 32 = 17952 \) 字节，而预算仍是 17984。预算 > 实际，mbarrier 的事务计数永远差 32 字节达不到登记值，full barrier 不翻转，MMA warp 的 `consumer_wait` 挂死（kernel 不返回）。反过来若预算 < 实际，barrier 会提前翻转，MMA 读到未完成的数据——静默出错。这道题的要点：预算与实际必须逐字节一致，且窗口本身还必须满足覆盖性（16 元素窗口在最坏偏移 \( r = 7 \) 时罩不住 16 个所需元素，即使预算写对也是错）。

**练习 2**：K1 的 `kTmaTransactionBytes` 是多少？逐项列出。
**答案**：\( 2048 \times 6 + 32 \times 2 + 128 \times 4 = 12288 + 64 + 512 = 12864 \) 字节，即 q + k + g_bf16 三份 16×128 bf16（12288）+ beta 32 元素 bf16（64）+ dt_bias 128 个 fp32（512）。

**练习 3**：为什么 `TMA_DISABLE_ALL` 下公式要保留一个 `0u`？
**答案**：`constexpr` 变量必须有一个定义。宏关闭时所有求和项被预处理掉，`0u` 兜底保证 `kTmaTransactionBytes` 仍是一个合法的常量表达式（虽然此时 `make_load_pipeline` 也被编译掉、该常量无人使用），从而同一份源码在两种编译配置下都能通过编译。见 [fwd_kernel2.cuh:L3-L5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L3-L5) 的注释。

## 5. 综合实践

本讲的综合实践分两部分：先手算预算（已由 4.3.4 覆盖，这里做记录），再做一次 `TMA_DISABLE_ALL` 消融实验，亲眼看「没有 LOAD/STORE warp 的 K2」是什么下场。

### 5.1 手算事务字节预算（记录）

在 `FlashKDA-tutorial/notes/`（或你的学习笔记）里记下这组数：

| 量 | 值（字节） | 来源 |
| --- | --- | --- |
| v + beta + 6 份 workspace（K2 每 stage） | 17984 | fwd_kernel2.cuh L173-L181 |
| K1 单发（q+k+g_bf16+beta+dt_bias） | 12864 | fwd_kernel1.cuh L158-L161 |
| workspace 每 tile（kPerTile） | 13824 | utils.cuh L63-L77 |
| 初始状态载入（bf16 / fp32） | 32768 / 65536 | fwd_kernel2.cuh L245、L274 |

### 5.2 TMA_DISABLE_ALL 消融：正确性与性能

**实践目标**：验证 LOAD/STORE warp 与两条流水线在 K2 中的必要性——关掉它们之后 kernel 是否仍然正确？性能数字还能信吗？

**背景**：`csrc/smxx/fwd_kernel2.cuh` 顶部预留了消融开关（[fwd_kernel2.cuh:L3-L5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L3-L5)，默认注释掉）。定义该宏后：流水线构造（L199-L213）、初始状态载入（L240-L318）、**整个 LOAD warp 体（L320-L423）**、消费侧的 wait/release（L435-L443、L735-L741）以及 STORE warp（L745-L838）全部被编译掉；MMA warp 退化为读固定的 `input[0]` / `output[0]`（L440-L442 的 `constexpr int load_stage = 0`）。

**操作步骤**：

1. **基线**：确认已按 u1-l3 完成安装，`bash tests/test.sh` 全绿，并用 `python benchmarks/bench_fwd.py --mode fixed --H 32` 记一组基线耗时（H 默认 96，跑小形状更快；参数说明见 u3-l10）。
2. **改宏重编译**：把 `csrc/smxx/fwd_kernel2.cuh` 第 5 行的 `// #define TMA_DISABLE_ALL` 取消注释（或在 `setup.py` 的 nvcc 参数里加 `-DTMA_DISABLE_ALL`），然后重编译：
   ```bash
   pip install --no-build-isolation -v . 2>&1 | tee build_ablation.log
   ```
3. **正确性**：再跑一次 `python tests/test_fwd.py`（需要 fla 与 matplotlib，见 test.sh），记录 exact-match（`torch.equal`）断言是否通过。
4. **性能**：再跑一次与第 1 步完全相同参数的 benchmark，记录 mean/min/max。
5. **恢复现场**：`git checkout -- csrc/smxx/fwd_kernel2.cuh`（若改的是 setup.py 一并还原），重装一次回到基线。

**需要观察的现象与预期结果**：预期测试**失败**——宏定义后没有任何代码去填充 `input[0]`（8 份拷贝全被编译掉），也没有任何代码把 `output[0]` 写回 gmem，MMA 读的是未初始化 smem、`out` 保持调用前的内容；benchmark 耗时大概率「变快」，因为装载与存储工作被整体移除，但这个数字对错误的输出毫无意义。两条结论合起来正是本讲的立意：LOAD warp 不是可有可无的加速器，而是数据路径本身；脱离正确性的性能对比没有价值。（以上为按源码路径推出的预期，标记为**待本地验证**——请把实际观察填进下表。）

**记录表模板**：

| 配置 | test_fwd.py 结果 | bench fixed mean (ms) | 结论 |
| --- | --- | --- | --- |
| 基线 | 全部通过 | | |
| TMA_DISABLE_ALL | | | |

## 6. 本讲小结

- LOAD warp 是装载流唯一的生产者：每 tile 走 `producer_acquire`（等 stage 空闲 + leader `arrive_and_expect_tx`）→ 取本 stage 的事务 barrier → 发 8 条 TMA 拷贝 → `++load_write`，循环结束用 `producer_tail` 排空流水线；只有 `elect_one_sync` 选出的一条 lane 真正发起拷贝（对照 K1 的 `threadIdx.x == 0`）。
- 一个 stage 的 8 份拷贝 = v（4096B）+ beta 1D 对齐窗口（64B）+ workspace 六份（12288B + 512B + 1024B）；beta 用 `& ~7` 对齐保证 TMA 的 16 字节全局对齐、用 32 元素窗口覆盖最坏偏移 7 + 16 = 23，消费端以 `& 7` 取回真实起点。
- `kTmaTransactionBytes = 17984` 必须逐字节等于 8 条拷贝之和：mbarrier 靠「到达计数 + 事务字节计数」聚合判定完成，少算会提前翻转造成静默错误，多算会挂死。
- 预算、`InputStorage` 容量与 `WorkspaceSizes::kPerTile` 三者可以对账：17984 = 13824（K1 每 tile 写出）+ 4096（v）+ 64（beta）。
- `TMA_DISABLE_ALL` 把装载/存储路径整体编译掉，MMA 读未初始化 smem——它是研究流水线结构贡献的消融开关，不是可用的回退模式。

## 7. 下一步学习建议

- **u3-l4（MMA 相位 1-5）**：顺着本讲的数据流往下读——LOAD warp 搬进来的 v/beta/INV/Mqk 如何被 4 个 MMA warp 消费成输出，`consumer_wait` 之后的 Phase 1-5 各用哪些 stage 缓冲。
- **u3-l7（STORE warp 与尾块处理）**：看与本讲对称的另一半——存储流（`PipelineAsync`，软件到达计数而非字节记账）、整块 TMA store 与 varlen 尾块的逐元素回退。
- **CUTLASS pipeline 源码**：`cutlass/include/cutlass/pipeline/sm90_pipeline.hpp`（需检出 submodule），对照本讲的 `producer_acquire / consumer_wait / consumer_release / producer_tail` 语义读 `PipelineTmaAsync` 的 barrier 实现，验证「empty/full barrier 对 + 事务字节计数」的描述。
- **u3-l12（二次开发实践）**：本讲综合实践用到的 `TMA_DISABLE_ALL` 只是消融开关家族的一员，该讲会系统盘点 `BLOCK_LEVEL_K1/K2`、`kInputStages` 等旋钮及其实验方法。
