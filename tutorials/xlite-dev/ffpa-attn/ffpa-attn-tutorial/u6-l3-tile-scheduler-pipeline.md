# Tile scheduler 与 producer/consumer pipeline

> 适用后端：CuTeDSL（SM90 专用 kernel 为主，SM80 通用路径为辅）。
> 前置讲义：[u6-l1 CuTeDSL 后端总览与 SM80/SM90 分发](u6-l1-cutedsl-overview-sm80-sm90.md)、[u6-l2 CuTeDSL 布局转换、校验与 varlen 接入](u6-l2-cutedsl-layout-varlen.md)。

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「tile scheduler」要解决的问题：把一个线性的 `block_idx`（CTA / thread block 编号）映射成一组实际要算的注意力 tile 坐标 `(m_block, head_idx, batch_idx, split_idx)`，并解释为什么不同的场景（dense 非因果 / dense 因果 / varlen）需要不同的映射策略。
- 区分三种**已被使用**的调度器 `SingleTileScheduler` / `SingleTileLPTScheduler` / `SingleTileVarlenScheduler`，以及一种**预留但暂无调用点**的 `SingleTileLPTBwdScheduler`；理解 L2 swizzle 与 LPT（Longest Processing Time）各自的目的。
- 解释 producer/consumer 流水线如何用 `PipelineTmaAsync`（TMA 加载）与 `PipelineAsync`（SMEM 中转）+ mbarrier 把 `g2s`（global→SMEM）和 MMA 计算重叠起来，从而隐藏全局显存延迟。
- 读懂 `NamedBarrier`（命名屏障）如何用 SM90 硬件 0–15 号 barrier ID 给跨 warp group 的握手起「 mnemonic 名字」，以及为什么它和 Pipeline 既是替代关系又是互补关系。

本讲只讲「调度与同步」这一层，不展开具体 MMA / softmax 计算——那是 u6-l4 的工作。

## 2. 前置知识

在进入源码前，先用三段大白话把背景建立起来。这些概念是 Hopper（SM90）GPU 编程的常识，FFPA 直接复用了 CUTLASS CuTe DSL 的抽象。

### 2.1 CTA、warp、warp group

- **CTA（Cooperative Thread Array）**：就是一个 thread block，一次 `launch` 里被派发到某个 SM 上的「一组线程」。本讲里把它叫 **block**，它的编号 `block_idx`（即 `cuda.blockIdx`）就是调度器要解码的输入。
- **warp**：32 个线程为一组，是 SM 上真正同步 / 调度的最小单位。
- **warp group**：Hopper 上 4 个 warp（128 线程）组成一个 warp group，一条 `wgmma`（Warp Group MMA）指令就能让这一组线程完成一次大块矩阵乘。FFPA 的 SM90 kernel 把一个 CTA 切成多个 warp group，让它们各司其职。

### 2.2 为什么要「重叠 g2s 与 MMA」

注意力 kernel 的主循环是：**加载一个 KV 块** → **算 QKᵀ** → **softmax** → **算 PV** → 加载下一个 KV 块……。其中：

- 「加载」是把数据从全局显存（gmem，HBM）搬到共享内存（SMEM，片上 SRAM），叫 **g2s**，带宽受限、延迟高。
- 「算」是在寄存器（RMEM）里做 WGMMA，速度极快。

如果串行执行（先全加载完再算），算力单元会长时间空转等数据。解决办法是**软件流水线（pipeline）**：当 MMA 正在算第 `k` 块时，提前把第 `k+1`、`k+2` 块加载进 SMEM 的另一块缓冲区，让「加载」和「计算」在时间上重叠。Hopper 提供了两条硬件特性来实现这种重叠：

- **TMA（Tensor Memory Accelerator）**：一个硬件单元，由单个线程发起，就能异步搬运一整块多维张量，搬运完成会通过 mbarrier 通知。
- **mbarrier（memory barrier）**：SM90 的硬件同步原语，本质是一个计数屏障，生产者「投递（arrive）+ 期望字节数（expect_tx）」，消费者「等待（wait）」，字节数到齐 + 到达次数达标后放行。

### 2.3 tile scheduler 在图里的位置

每个 CTA 启动后第一件事：**我是谁？我要算哪块？**——这正是 tile scheduler 回答的问题。回答完之后，CTA 才进入上面那个「加载↔计算」主循环。所以本讲的两条主线其实是顺序的：

```
block_idx ──[tile scheduler]──► (m_block, head, batch, split)
                                          │
                                          ▼
                              ┌─── pipeline 主循环 ───┐
                              │ producer: g2s (TMA)   │  ◄── 重叠
                              │ consumer: WGMMA       │
                              └───────────────────────┘
```

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [src/ffpa_attn/cute/utils/tile_scheduler.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py) | tile→工作 tile 的映射 | `WorkTileInfo`、`TileSchedulerArguments`、三种 active scheduler + 一种 reserved scheduler |
| [src/ffpa_attn/cute/utils/pipeline.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py) | 对 CUTLASS `cutlass.pipeline` 的薄封装 | `PipelineTmaAsync`、`PipelineAsync`、`_call_with_elect_one` |
| [src/ffpa_attn/cute/utils/named_barrier.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py) | 命名屏障枚举 | `NamedBarrierFwd`、`NamedBarrierBwd` |
| [src/ffpa_attn/cute/\_fwd_d512_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py) | SM90 d512 前向 kernel（3 个 warp group） | scheduler 选型、pipeline 创建、warp group 分发——把前三个文件「串起来」的真实调用点 |
| [src/ffpa_attn/cute/\_fwd_generic_sm80.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm80.py) | SM80 通用前向 kernel（单 CTA） | 单 warp group、不 warp-specialize 的简单路径，作对照 |

> 提示：`tile_scheduler.py` / `pipeline.py` / `named_barrier.py` 三个文件头部都写明「This file is copied from …/flash-attention/…」，是 Tri Dao 的 flash-attention CuTe 实现的 **SM90-only 精简版**。文件顶部的注释列出了被裁剪掉的功能（CLC 硬件调度、cluster、split-KV、持久化 kernel 等），这些是 SM100+ 才用得上的，FFPA 在 SM90 上不需要，所以读源码时遇到「Removed / Reserved」字样不必纠结。

## 4. 核心概念与源码讲解

### 4.1 Tile Scheduler：从 block_idx 到工作 tile

#### 4.1.1 概念说明

一个注意力计算任务可以被切成很多个独立的「工作 tile」，每个 tile = **一个 Q 行块 `m_block`** × **一个注意力头 `head_idx`** × **一个 batch `batch_idx`**，再加上一个用于 split-KV 的 `split_idx`（SM90 训练里恒为 0）。GPU 启动 kernel 时会开出一批 CTA，每个 CTA 拿到一个全局唯一的 `block_idx`。**Tile scheduler 的职责就是写一个函数 `block_idx → (m_block, head_idx, batch_idx, split_idx)`**，让每个 CTA 知道自己该算哪一块。

为什么这事值得专门抽象成一个类，而不是直接写 `m_block = block_idx % num_block_m`？

1. **不同场景要不同的映射**：dense（定长）可以直接用三维 grid 平铺；但因果掩码下每个 tile 的计算量不等（越靠下的 Q 行要看的 KV 越多），需要做负载均衡；varlen（变长）下每个 batch 的 Q 长度不同，连「总共有多少个 tile」都无法在 host 端精确算出来。
2. **统一接口**：CUTLASS 的 kernel 模板指望 scheduler 暴露一组固定方法（`get_current_work` / `advance_to_next_work` / `initial_work_tile_info` / `producer_tail` 等），kernel 主循环只调这些方法、不关心具体映射规则，于是换 scheduler = 换映射策略，kernel 主体不动。

FFPA 把所有工作 tile 的坐标统一打包成一个 **4 轴元组** `(block, head, batch, split)`，封装在 `WorkTileInfo` 里：

[src/ffpa_attn/cute/utils/tile_scheduler.py:L71-L81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L71-L81) —— 改写 `WorkTileInfo`，使其携带 `(block, head, batch, split)` 四轴。

注意第 4 轴 `split` 是为 split-KV 预留的，但 SM90 训练路径**恒为 0**（文件头注释明确写了 `num_splits: Int32  # always 1 for SM90 training`），所以实际只用到前 3 轴。

#### 4.1.2 核心流程

所有 scheduler 共享同一套调用契约，kernel 主循环按下面的骨架使用它们：

```text
scheduler = TileScheduler.create(params)          # host 侧用 get_grid_shape 决定开多少 CTA
work = scheduler.initial_work_tile_info()          # CTA 启动后第一次取工作
while work.is_valid_tile:
    m_block, head_idx, batch_idx, split = work.tile_idx
    # ……producer / consumer 主循环，对这块 tile 做 g2s + MMA……
    scheduler.advance_to_next_work()                # 推进到下一块
    work = scheduler.get_current_work()
scheduler.producer_tail()                           # 收尾：flush 流水线里没消费完的 stage
```

三种 active scheduler 的区别，全部集中在「`get_current_work` 怎么把 `block_idx` 解码成 `(m_block, head_idx, batch_idx)`」这一步：

| scheduler | grid 形状 | tile_idx 来源 | 映射策略 | 典型场景 |
|---|---|---|---|---|
| `SingleTileScheduler` | `(num_block, num_head, num_batch)` | `block_idx` 三轴直接读 | **恒等映射**，无 swizzle | dense 非因果前向 / 非确定性反向 / 反向 preprocess |
| `SingleTileLPTScheduler` | `(total_blocks, 1, 1)` | `block_idx[0]` | **L2 swizzle + LPT 块序反转** | dense 因果 / local 前向 |
| `SingleTileVarlenScheduler` | `(total_blocks_max·num_head, 1, 1)` | `block_idx[0]` | **warp 级 prefix-sum 定位 batch**，可选 LPT/head swizzle | varlen 前向 / 反向 |
| `SingleTileLPTBwdScheduler` | `(total_blocks, 1, 1)` | `block_idx[0]` | L2 swizzle + SPT（反向块序） | **预留**，暂无调用点 |

#### 4.1.3 源码精读

**(a) 共享参数 `TileSchedulerArguments`**——所有 scheduler 的 host 侧入口都接收这同一个数据结构，再各自 `to_underlying_arguments` 转成自己的 `Params`：

[src/ffpa_attn/cute/utils/tile_scheduler.py:L89-L107](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L89-L107) —— 共享参数：`num_block/num_head/num_batch`、`headdim/headdim_v`、`total_q`、`tile_shape_mn`、varlen 用的 `mCuSeqlensQ`，以及两个 constexpr 开关 `lpt`、`head_swizzle`。

这里有几个会被后面反复用到的字段：`lpt`（是否做 LPT 块序反转）、`head_swizzle`（varlen 反向里是否做 head-major 的 L2 调度，**当前无调用点置 True**）、`element_size`（每个元素的字节数，算 L2 占用要用）。

**(b) `SingleTileScheduler`——恒等映射，最简单。** 它的 grid 就是天然的三维，`block_idx` 三轴分别对应 `m_block / head_idx / batch_idx`：

[src/ffpa_attn/cute/utils/tile_scheduler.py:L159-L173](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L159-L173) —— `get_grid_shape` 直接返回 `(num_block, num_head, num_batch)`；`get_current_work` 把 `block_idx` 三轴原样塞进 `WorkTileInfo`，`split` 补 0。

`advance_to_next_work` 把 `_is_first_block` 置 `False` 后再调一次 `get_current_work`——注意 SM90 这些都是 **single-tile** scheduler，一个 CTA 只算一块，所以「下一块」就是把 `is_valid_tile` 翻成 `False`，让主循环退出（见 [L181-L183](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L181-L183)）。它不持有持久化状态。

**(c) `SingleTileLPTScheduler`——L2 swizzle + LPT。** 这是最值得读的一种。先看 host 侧如何根据 L2 容量算出「swizzle 宽度」：

[src/ffpa_attn/cute/utils/tile_scheduler.py:L227-L262](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L227-L262) —— 把单头的 KV 体积 `size_one_head = seqlen_k·(headdim+headdim_v)·element_size` 与一个保守的 L2 预算 `size_l2 = 50MB` 比较，`swizzle = 2^floor(log2(size_l2 / size_one_head))`，即「L2 里塞得下几个头的 KV」，并向下取整到 2 的幂。

直觉解释：我们希望**连续若干个 CTA 共用同一批 K/V**，从而命中 L2。`swizzle` 就是「能同时驻留 L2 的头数」。把工作按 `swizzle` 宽度切成一节一节的「section」，节内连续调度，就能让 K/V 在 L2 里被复用。

device 侧的解码把线性 `tile_idx` 拆成「在第几节」+「节内偏移」，再拆成 `(block, head, batch)`：

[src/ffpa_attn/cute/utils/tile_scheduler.py:L303-L324](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L303-L324) —— L2-swizzled 坐标映射：两次 `divmod` 把 `tile_idx` 还原成 `(block, head, batch)`，最后一句 `block = num_block - 1 - block` 就是 LPT。

把这段 divmod 链画成图（设 `swizzle = S`、`num_block = M`、节大小 `l2_major = S·M`）：

```text
tile_idx
   │  divmod(l2_major = S·M)
   ▼
bidhb ──► 第几节(section)
l2_mod ──► 节内偏移
   │  divmod(l2_minor = S)        ◄── 末节用 remainder 而非 S
   ▼
block         = l2_mod // S        (Q 行块)
bidhb_residual= l2_mod %  S        (节内第几个 head-batch)
   │  bidhb_actual = bidhb·S + bidhb_residual
   │  divmod(num_head)
   ▼
batch_idx, head_idx
   │  if lpt: block ← M - 1 - block
   ▼
WorkTileInfo(block, head_idx, batch_idx, split)
```

两个细节值得点出：

- **末节（residual）特殊处理**：`num_head·num_batch` 未必是 `swizzle` 的整数倍，最后一节装不满 `swizzle` 个头，所以单独算了 `num_hb_remainder` 和 `l2_minor_residual_divmod`，避免用错误的除数（见 [L310-L316](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L310-L316) 与 [L248-L260](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L248-L260)）。
- **LPT 块序反转**：`block = num_block - 1 - block`（[L318-L319](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L318-L319)）。**LPT = Longest Processing Time first**。在因果掩码下，`m_block` 越大（Q 行越靠下）的 tile 要累加的 KV 列越多、耗时越长；让最重的 tile 先被派发到空闲 SM，能让所有 SM 大致同时收尾、避免长尾拖慢。`lpt` 这个 constexpr 来自 host 侧 `args.lpt = self.is_causal or self.is_local`（见后文 [_fwd_d512_sm90.py:L509](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L509)）——非因果时各 tile 工作量相等，无需 LPT，直接走 `SingleTileScheduler`。

> 关于网格形状：`SingleTileLPTScheduler.get_grid_shape` 返回 `(total_blocks, 1, 1)`（[L294-L301](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L294-L301)），即把所有 tile 拍扁到第一维、后两维为 1，因为映射逻辑已经把「头/batch」编码进 `tile_idx` 了。

**(d) `SingleTileVarlenScheduler`——变长场景的 warp 级 prefix-sum 映射。** varlen 的难点：每个 batch 的序列长度不同，**host 端无法精确知道总 tile 数**，于是 grid 取一个保守上界，多开的 CTA 靠 `is_valid_tile=False` 提前退出。

[src/ffpa_attn/cute/utils/tile_scheduler.py:L592-L603](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L592-L603) —— grid 上界 `total_blocks_max = ceil_div(total_q + num_batch·(tile_m-1), tile_m)`，再乘以 `num_head`。

核心是 `_varlen_coord_map`：用 warp 内 32 个 lane 各自算一个 batch 的 Q 块数，做 **warp 级 prefix sum**，再通过 `vote_ballot_sync` / `shuffle_sync` 定位当前 `tile_idx` 落在哪个 batch：

[src/ffpa_attn/cute/utils/tile_scheduler.py:L629-L715](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L629-L715) —— device 侧坐标映射：lane→batch 的块数 → `warp_prefix_sum` 累加 → `while` 跳过整组 → `vote_ballot_sync` 在组内二分定位 batch → 解出 `(block, head, batch)`。

辅助函数 `_get_num_m_blocks` 让一个 lane 读出「第 `lane` 个 batch 的 Q 块数」（[L605-L627](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L605-L627)），序列长度来自 `mSeqUsedQ` 或 `mCuSeqlensQ` 的差分（`shuffle_sync_down` 取下一个累加偏移）。`warp_prefix_sum` 本体在 utils 包里（[src/ffpa_attn/cute/utils/\_\_init\_\_.py:L506-L520](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/__init__.py#L506-L520)），用 `log2(32)=5` 轮 butterfly shuffle 累加实现。

varlen 也支持 `lpt` / `head_swizzle`（[L677-L707](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L677-L707)）：开启后会在 `(block, head)` 平面里再做一次「每节 `nheads_in_l2` 个头」的 swizzle，并且 `block = num_m_blocks - 1 - block` 做 LPT 反转，用于确定性（deterministic）反向。但默认 `lpt=False, head_swizzle=False`（[L521-L522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L521-L522)）。

**(e) `SingleTileLPTBwdScheduler`——预留。** 文件里第四个 scheduler 是给「确定性反向」准备的，但类注释明确写了当前没有任何调用点：

[src/ffpa_attn/cute/utils/tile_scheduler.py:L360-L368](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L360-L368) —— 「Reserved: SM90 deterministic backward scheduler (SPT + L2 swizzle). Currently no call site imports this」。

它的反向块序反转用的是 `spt`（Shortest Processing Time，[L463-L465](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L463-L465)），与前向 LPT 方向相反，但和前向一样都做 L2 swizzle。读它有助于理解「同样的 swizzle 骨架可以配不同的块序策略」。

#### 4.1.4 代码实践

**目标**：用纯 Python 复现 `SingleTileLPTScheduler` 的 swizzle 计算，观察 `swizzle` 随 head_dim 增大如何变化——这一步不需要 GPU，只是算术，能直接 `python` 跑。

**操作步骤**：

1. 把下面的脚本存为 `swizzle_demo.py`（**示例代码，非项目原有文件**）：

   ```python
   # 示例代码：复现 tile_scheduler.py 里 SingleTileLPTScheduler.Params.create 的 swizzle 计算
   import math

   def lpt_swizzle(seqlen_k, headdim, headdim_v, element_size=2, size_l2=50*1024*1024):
       size_one_head = seqlen_k * (headdim + headdim_v) * element_size
       if size_l2 < size_one_head:
           swizzle = 1
       else:
           ratio = size_l2 // size_one_head
           swizzle = 1 << (31 - count_leading_zeros(ratio))  # 2^floor(log2(ratio))
       return size_one_head, swizzle

   def count_leading_zeros(n):  # 复刻 utils.clz 的语义（32 位）
       return 31 - (n.bit_length() - 1)
   ```

2. 对几组典型形状算一下：

   ```python
   for D in (256, 512, 1024):
       sz, sw = lpt_swizzle(seqlen_k=8192, headdim=D, headdim_v=D, element_size=2)
       print(f"D={D:4d}  size_one_head={sz/1024/1024:6.2f}MB  swizzle={sw}")
   ```

**需要观察的现象**：随着 `D` 增大，`size_one_head`（单头 KV 字节数）线性增长，`swizzle`（L2 能塞下的头数）按 2 的幂阶梯式下降（例如 256→…→8→4→2→1）。

**预期结果**（待本地验证，具体取决于 `size_l2=50MB` 的取定）：`D=256` 时 swizzle 较大（比如 8 或 16），`D=1024` 时可能降到 1（单头 KV 已接近或超过 50MB 预算）。这正好解释了为什么 FFPA 在大 head_dim 下格外在意 L2 复用——head_dim 越大，能共享 L2 的头越少。

#### 4.1.5 小练习与答案

**练习 1**：`SingleTileScheduler` 的 `advance_to_next_work` 调用后 `is_valid_tile` 会变成什么？为什么 SM90 还要保留这个方法？

**答案**：变成 `False`（[L181-L183](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L181-L183)），因为这是 single-tile scheduler，一个 CTA 只算一块。保留它是为了对齐 CUTLASS 的 scheduler 协议——kernel 主循环写成 `while is_valid_tile: … advance()` 的通用形式，换上持久化 scheduler（一个 CTA 算多块）时主循环不用改。FFPA 在 SM90 上把持久化路径裁掉了，所以这里「推进」=「结束」。

**练习 2**：因果掩码下，为什么 LPT 要把 `block` 反转成 `num_block - 1 - block`，而不是直接按原顺序调度？

**答案**：因果掩码下 `m_block` 越大（Q 行越靠下），要累加的 KV 列块越多，tile 越重。若按原顺序让轻 tile 先跑，重 tile 会集中在末尾、拖出长尾；LPT 把最重的 tile 优先派发到空闲 SM，使所有 SM 的完工时间更接近、整体吞吐更高（[L318-L319](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L318-L319)）。

**练习 3**：`SingleTileVarlenScheduler.get_grid_shape` 返回的 `total_blocks_max` 为什么是个「上界」，多开的 CTA 怎么处理？

**答案**：因为变长 batch 里每个序列的 Q 块数依赖运行时数据（`mCuSeqlensQ` / `mSeqUsedQ`），host 端算不出精确总数，只能用 `ceil_div(total_q + num_batch·(tile_m-1), tile_m)` 估上界（[L600-L602](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L600-L602)）。多开的 CTA 在 `_varlen_coord_map` 里会得到 `batch_idx >= num_batch`，于是 `is_valid_tile=False`（[L657-L659](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/tile_scheduler.py#L657-L659)），kernel 主循环直接跳过、空跑退出。

---

### 4.2 Pipeline：producer/consumer 流水线与 mbarrier

#### 4.2.1 概念说明

`pipeline.py` 解决的是 **「把 g2s 和 MMA 重叠起来」** 这件事。核心抽象是 **producer / consumer 流水线**：

- **producer**：负责往 SMEM 的**多级环形缓冲区（multi-stage circular buffer）**里填数据。在 TMA 路径里，producer 是发起 TMA 搬运的 warp。
- **consumer**：负责从 SMEM 里取数据做计算（这里是 WGMMA）。
- **mbarrier**：每一级缓冲区配一个 mbarrier，充当 producer 和 consumer 之间的「信箱」：producer 填完一级就 `arrive + expect_tx(字节数)`，consumer 算 `wait` 等字节数到齐；consumer 用完一级就 `release`，producer 下轮 `acquire` 时会等它被释放。

多级缓冲（`num_stages`，常取 2）的意义：当 consumer 在算第 `k` 级时，producer 已经在填第 `k+1` 级，于是「加载」的延迟被「计算」掩盖。这就是 Hopper 上 flash-attention 能打满带宽的关键。

FFPA 没有从零写这套机制，而是继承 CUTLASS 的 `cutlass.pipeline.PipelineAsync` / `PipelineTmaAsync` / `PipelineCpAsync`，只做**薄薄一层重写**，加上 FFPA 自己需要的两个能力：

1. `PipelineTmaAsync.producer_acquire` 多了一个 `extra_tx_count` 参数。
2. `PipelineAsync` 的 `producer_commit` / `consumer_release` 支持「只让一个选举线程去发 barrier arrive」（`elect_one`）。

`pipeline.py` 里还有几个类（`PipelineStateSimple`、`_PipelineIndexPhaseMixin`、`NamedBarrier`、`PipelineCpAsync`）是**预留/未使用**的，文件注释里写得很清楚（见 [L36-L38](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L36-L38)、[L123-L126](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L123-L126)、[L171-L174](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L171-L174)、[L276-L278](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L276-L278)），读源码时认得它们是「为将来 / 为非 TMA 路径留的接口」即可，不必深究。

#### 4.2.2 核心流程

一个 TMA 流水线的典型一轮（以加载 K 为例）：

```text
producer (warp-0, elect_one):                consumer (MMA warp group):
  producer_acquire(state)                      consumer_wait(state)        # 等数据到齐
    └─ wait sync_object_empty (等这级被释放)        └─ wait sync_object_full (等 producer 投递+字节数到齐)
  TMA load K  → SMEM[state.index]             WGMMA on SMEM[state.index]
  producer_commit(state)                      consumer_release(state)     # 归还这级给 producer
    └─ arrive sync_object_full + expect_tx     state.advance()
  state.advance()
```

`state` 是一个 `PipelineState`，记录「当前在第几级（`index`）、相位（`phase`）」。相位是一个翻转位，因为 mbarrier 是「奇偶交替」的——同一级被复用时，靠相位区分是「这一轮」还是「上一轮」的到达，避免新旧信号混淆。

FFPA 的两层重写分别作用于图里的两处：

- **`PipelineTmaAsync`** 改的是 `producer_acquire` 那一步（TMA 专用，能处理「期望字节数」）。
- **`PipelineAsync`** 改的是 `producer_commit` / `consumer_release` 那一步（让 elect_one 线程发 arrive，省通信）。

#### 4.2.3 源码精读

**(a) `PipelineTmaAsync`——多带一个 `extra_tx_count`。** TMA 搬运的字节数平时是固定的（`tx_count`，创建时给定），但有时同一次 `commit` 里会**额外**再发一些非 TMA 的 store（比如把寄存器里的中间结果写回 SMEM），这些字节数要并进 mbarrier 的「期望字节数」里，否则 consumer 会一直等不齐。FFPA 重写 `producer_acquire` 就是为了接收这个 `extra_tx_count`：

[src/ffpa_attn/cute/utils/pipeline.py:L315-L350](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L315-L350) —— `PipelineTmaAsync` 只重写 `producer_acquire`：`extra_tx_count==0` 时走 `arrive`，否则用 `arrive_and_expect_tx(tx_count + extra_tx_count)` 把额外字节数合并进去。

逐段读这个方法（[L319-L347](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L319-L347)）：

1. 先 `if_generate` 条件等待 `sync_object_empty`——即「等 consumer 把这级缓冲释放掉」，这是流水线背压（backpressure）的来源：consumer 没消费完，producer 就不能覆盖。
2. 再按 `extra_tx_count` 决定 `arrive`（默认字节数）还是 `arrive_and_expect_tx`（加额外字节数）。

> 注意 `producer_acquire` 在 TMA 语义里**既等空又投递满**：它先 `wait(empty)` 等 consumer 释放，再 `arrive(full)` 告诉 consumer「我马上要填了，请等这些字节」。真正的 TMA 搬运发生在 acquire 与 commit 之间，由 kernel 显式调用 `load_*`（带 `tma_bar_ptr`）完成。

**(b) `PipelineAsync`——elect_one 提交/释放。** 这是个**通用**（非 TMA）流水线，常用于 SMEM→SMEM 的中转（比如一个 warp group 把算好的 `sP` 交给另一个 warp group）。它的 producer/consumer 都是普通的 warp，不是 TMA 硬件单元。FFPA 给它加了「只让一个选举线程发 barrier arrive」的开关：

[src/ffpa_attn/cute/utils/pipeline.py:L207-L243](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L207-L243) —— `PipelineAsync` 在 `create` 时接收 4 个开关：`elect_one_commit` / `syncwarp_before_commit` / `elect_one_release` / `syncwarp_before_release`。

`elect_one` 的意义：mbarrier 的 `arrive` 如果让 warp 里 32 个线程都发，会重复计数；通常只要「每 warp 选出一个代表（elect_one）」发一次即可。FFPA 用 `_call_with_elect_one` 统一处理：

[src/ffpa_attn/cute/utils/pipeline.py:L104-L114](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L104-L114) —— `_call_with_elect_one`：开关打开时先 `sync_warp` 再进 `elect_one()` 区块只让代表线程调用父类方法，否则全体调用。

`producer_commit` / `consumer_release` 各自把父类方法包了一层（[L245-L267](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L245-L267)）。`syncwarp_before_*=False` 用于「线程已经收敛过」的场景（比如紧跟在 `wgmma.wait_group` 之后，warp 内天然同步），此时不必再冗余 `sync_warp`——这在 d512 kernel 里被实际用到（见后文）。

**(c) 「re-class」工厂 `_override_create`。** 这两个 Pipeline 子类都用了同一个技巧：CUTLASS 父类是 `@dataclass(frozen=True)`（不可变），没法在 `__init__` 里塞新字段。FFPA 的做法是**先用父类 `create` 造好对象，再用 `object.__setattr__` 把 `__class__` 换成子类**，顺便注入新字段：

[src/ffpa_attn/cute/utils/pipeline.py:L18-L28](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L18-L28) —— `_override_create` 返回一个静态工厂：构造父类实例后改写 `__class__`。`PipelineTmaAsync` 在文件末尾就是用它挂上自己的 `create`（[L350](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L350)）。

#### 4.2.4 代码实践

**目标**：在真实 kernel 里追踪一次「producer 发 TMA → consumer 等 → MMA」的完整握手，确认 Pipeline 如何把 g2s 和 MMA 重叠。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 [_fwd_d512_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py) 的 `producer` 方法，定位 K 的加载（[L932-L940](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L932-L940)）。你会看到三步：`pipeline_k.producer_acquire(...)` → `load_K(...)`（真发 TMA）→ `pipeline_k.producer_commit(...)`。
2. 对照 `producer` 的 docstring（[L843-L853](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L843-L853)），它明确说：consumer 侧的 `pipeline_q/k/v` 自带 empty-mbarrier 信用循环，所以 `producer_acquire` 每次 load 都会**自然地等 WG1/WG2 释放上一槽**，把旧的 `QueryEmpty` 语义吸收进了 `PipelineTmaAsync`。
3. 再看 consumer 侧（`mma_wg1` 里的 `pipeline_k.consumer_wait(...)`，[L1156-L1164](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L1156-L1164)），确认它在 acquire 数据后才做 WGMMA。
4. 注意 producer 是**逐 n_block 循环发 K/V**（[L919-L960](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L919-L960)），而 consumer 也在逐 n_block 算；因为 `num_stages_k>=2`，两者错位一两个 stage 并行推进——这就是「g2s 与 MMA 重叠」的实物证据。

**需要观察的现象**：producer 和 consumer 用的是**同一个 `pipeline_k` 对象**的两面（producer 调 `producer_*`、consumer 调 `consumer_*`），它们靠 mbarrier 自动对齐 stage，源码里没有任何显式的「等加载完」标志位——同步全藏在 Pipeline 里。

**预期结果**：能画出下面这种时序图（stage 数=2 为例）：

```text
producer: | load K0 | load K1 | load K2 | load K3 | ...
consumer:          | MMA K0  | MMA K1  | MMA K2  | ...
                     ↑ wait 挡住直到 K0 的 mbarrier 放行
```

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PipelineTmaAsync` 要把 `extra_tx_count` 合并进 mbarrier 的「期望字节数」，而不是另开一个 barrier？

**答案**：mbarrier 的 `expect_tx` 把「到达次数」和「字节数」绑在同一个屏障上：consumer 的 `wait` 只有在「生产者到达次数足够 **且** 累计字节数达到期望值」时才放行（[L339-L347](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L339-L347)）。如果另开 barrier，TMA 字节和非 TMA 字节会分成两路信号，consumer 要 `wait` 两个屏障才敢读，既慢又容易写错。合并成一路是最稳妥的做法。

**练习 2**：`PipelineAsync` 的 `syncwarp_before_commit=False` 什么时候用？看 d512 kernel 里 `pipeline_P` 的创建（[_fwd_d512_sm90.py:L662-L673](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L662-L673)）说明原因。

**答案**：当调用点**已经做过 warp 内同步**时用 `False`，避免冗余的第二次 `sync_warp`。d512 里 `pipeline_P`（跨 WG 的 sP 握手）的 producer 是 WG1，它在 `commit` 前已经发了 `fence_view_async_shared + sync_warp`（见注释 [L667-L672](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L667-L672)），所以把 PipelineAsync 自带的 `syncwarp` 关掉。

**练习 3**：`pipeline.py` 里哪些类当前**没有**被 SM90 kernel 使用？怎么知道的？

**答案**：`PipelineStateSimple`、`make_pipeline_state`、`_PipelineIndexPhaseMixin`（及其 `_w_index` 方法）、`NamedBarrier`（indexed 包装）、`PipelineCpAsync` 都未被使用——文件里每个这类定义上方都有「Reserved」「no SM90 call site uses these methods today」之类的注释（[L36-L38](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L36-L38)、[L86-L98](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L86-L98)、[L123-L126](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L123-L126)、[L171-L174](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L171-L174)、[L276-L278](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L276-L278)）。SM90 路径只实际使用 `PipelineAsync`（带 elect_one）与 `PipelineTmaAsync`。

---

### 4.3 NamedBarrier：跨 warp group 的命名屏障

#### 4.3.1 概念说明

Hopper 的 `bar.sync` / `bar.arrive` PTX 指令只接受 **0–15 号** 屏障 ID（见 [named_barrier.py:L21](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L21) 的注释），编号 0 还被 `__syncthreads()` 占用。于是一个 CTA 里能用的命名屏障只有 15 个，非常稀缺。

当一个 kernel 里有**多个 warp group 协作**（比如 d512 的 producer / WG1 / WG2 三个组），它们之间需要各种「我算完了，你可以接着算」的握手信号：epilogue 同步、sP 发布/消费、scale 就绪……如果直接在代码里写裸数字 `bar.arrive 7`，很快就没人记得 7 代表什么。`NamedBarrier` 的做法是**用 `IntEnum` 给每个屏障 ID 起一个 mnemonic 名字**，于是源码里写 `int(NamedBarrierFwd.PEmpty)` 而不是裸 `14`，可读性大幅提升。

`named_barrier.py` 本身**只定义两个枚举**（`NamedBarrierFwd` / `NamedBarrierBwd`），不含任何运行时逻辑——它就是一张「名字↔ID」对照表。真正发起 barrier 的是 kernel 主体（用 `cute.arch.barrier_arrive(...)` 等）或 Pipeline 内部（mbarrier）。FFPA 在 `pipeline.py` 里**另有一个**同名但带运行时逻辑的 `NamedBarrier` 类（[pipeline.py:L175-L202](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L175-L202)），但那是预留的 indexed 包装，**当前没有调用点**，注意不要和 `named_barrier.py` 里的纯枚举混淆。

#### 4.3.2 核心流程

命名屏障在 d512 kernel 里的使用模式有两种：

1. **直接用枚举值作 barrier_id**：在 `cute.arch.barrier_arrive(barrier_id=int(NamedBarrierFwd.PFull), ...)` 这种调用里，把枚举值当成 ID 传进去（见 [_fwd_d512_sm90.py:L1291](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L1291)、[L1307](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L1307)）。
2. **被 Pipeline 取代**：很多原本用 `PFull/PEmpty/ScaleReady` 的跨 WG 握手，在 d512 里被改写成了 `pipeline_P` / `pipeline_Scale`（`PipelineAsync` + mbarrier），因为 Pipeline 自带多级缓冲、比裸 named barrier 表达力更强（见 [_fwd_d512_sm90.py:L653-L693](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L653-L693) 的注释「replaces NamedBarrierFwd.ScaleReady in mainloop」）。

所以现在的角色分工是：**Pipeline 管「有数据流转、需要多级缓冲」的握手；NamedBarrier 管「一次性事件型」的同步**（比如 epilogue 收尾、跨 WG 单缓冲 sP 握手）。

#### 4.3.3 源码精读

**(a) `NamedBarrierFwd`——前向命名屏障。**

[src/ffpa_attn/cute/utils/named_barrier.py:L7-L17](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L7-L17) —— 9 个前向屏障名，从 1 开始（0 留给 `sync_threads()`）。

逐个含义：`Epilogue`（epilogue 收尾同步）、`WarpSchedulerWG1/2/3`（warp group 内的 warp 调度）、`PFull/PEmpty`（sP 的发布/消费，跨 WG1↔WG2）、`VZero`（sV 清零）、`QueryEmpty`（Q 缓冲释放）、`ScaleReady`（scale 就绪）。注意 `PFull/PEmpty/ScaleReady` 在 d512 里大多已被 Pipeline 取代，但枚举仍保留以便其它 SM90 kernel 或单缓冲路径使用。

**(b) `NamedBarrierBwd`——反向命名屏障，更紧张。**

[src/ffpa_attn/cute/utils/named_barrier.py:L20-L41](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L20-L41) —— 15 个反向屏障名，**几乎用满了 0–15 的全部预算**。

反向比前向更需要屏障，因为反向要同时算 `dQ / dK / dV` 三个梯度，跨 WG 的握手更复杂：`dSFull/dSEmpty`（sdS 的发布/消费）、`PFull/PEmpty`（单缓冲 sP 握手）、`dSLocal`（WG2 内部 STSM→WGMMA 的 fence）。注释里特意标了几条「reserved/unused」（[L31-L34](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L31-L34)），说明 ID 预算被精心分配过、留了空位给未来。`dSLocal` 是第 15 号——已经是上限，再多就要溢出到 UB 区间了。

> 一个易踩的坑：`enum.auto()` 从 1 开始递增，所以 `NamedBarrierBwd` 里每一项后面的行内注释（`# 5`、`# 6`…`# 15`）就是它真实的 barrier ID。改这个枚举的顺序会**静默地**改写所有 PTX 屏障号，必须同步检查是否有 ID >15。

#### 4.3.4 代码实践

**目标**：确认「NamedBarrier 只是一张名字表」+「它和 Pipeline 既替代又互补」这两点。

**操作步骤**（源码阅读型实践）：

1. 在 d512 kernel 里搜索 `NamedBarrierFwd.` 的所有使用点（例如 `int(NamedBarrierFwd.PEmpty)` 在 [_fwd_d512_sm90.py:L1291](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L1291) 与 [L1544](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L1544)），确认它们都是作为 `barrier_id=` 传入，没有任何运行时方法调用。
2. 对照 [_fwd_d512_sm90.py:L653-L693](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L653-L693)：注释明说 `pipeline_Scale` **替代** `NamedBarrierFwd.ScaleReady`、`pipeline_P` **替代** `PFull/PEmpty` 的跨 WG 握手。

**需要观察的现象**：枚举本身只是 `IntEnum`（纯静态），但 kernel 通过 `int(...)` 取出它的数值当 PTX barrier ID；被 Pipeline 替代的那些名字在 d512 里**不再被引用**，但仍留在枚举里。

**预期结果**：能口述「NamedBarrier = 屏障 ID 的可读别名；Pipeline = 带多级缓冲的高级同步。两者底层都落到 SM90 的 mbarrier / named barrier 硬件，但抽象层次不同」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `NamedBarrierFwd` 从 1 开始，而不是 0？

**答案**：PTX 的 0 号 named barrier 保留给 `__syncthreads()`（即 `bar.sync 0`），自定义屏障只能从 1 开始（[L8-L9](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L8-L9) 的注释）。0–15 是 SM90 `bar.sync`/`bar.arrive` 的合法区间，超出是 UB（[L21](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L21)）。

**练习 2**：如果反向 kernel 再多需要一个跨 WG 握手，能直接在 `NamedBarrierBwd` 末尾加一项吗？为什么？

**答案**：不能随便加。`NamedBarrierBwd` 已经用到 15 号（`dSLocal`，[L40-L41](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L40-L41)），再加就会变成 16，超出 SM90 的 0–15 合法区间、成为 UB。要新增握手，得先「复用」那些标注 `reserved/unused` 的空位（[L31-L34](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/named_barrier.py#L31-L34)），或者改用 mbarrier（Pipeline）来承载——这正是 FFPA 把越来越多握手迁到 Pipeline 的原因之一。

**练习 3**：`pipeline.py` 里那个带运行时逻辑的 `NamedBarrier` 类（[pipeline.py:L175-L202](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L175-L202)）和 `named_barrier.py` 里的枚举是什么关系？

**答案**：它们是**两个不同的东西，只是同名**。`named_barrier.py` 的是纯 `IntEnum`（名字→ID），当前被 kernel 直接用作 barrier_id；`pipeline.py` 的是 `NamedBarrierOg` 的子类、带 `arrive_w_index` 等运行时方法（indexed barrier，给连续一段 barrier ID 用），**当前没有调用点**（[pipeline.py:L171-L174](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pipeline.py#L171-L174) 注明「Current SM90 kernels use the IntEnum-based NamedBarrierFwd/Bwd instead」）。读源码时不要把两者混为一谈。

---

## 5. 综合实践

把本讲三条线（scheduler 选型、pipeline 创建、warp group 分发）在 **d512 SM90 kernel** 里串成一张完整的「CTA 启动→干活」流程图。这是本讲最重要的综合练习。

**任务**：阅读 [_fwd_d512_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py) 的 `kernel` 方法（[L567-L742](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L567-L742) 是 pipeline 创建、warp group 分发；前文 [L486-L512](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L486-L512) 是 scheduler 选型），按下面 5 步填出一张时序/分工图。

1. **scheduler 选型**（[L486-L492](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L486-L492)）：varlen 走 `SingleTileVarlenScheduler`；dense 非因果/local 走 `SingleTileScheduler`；dense 因果走 `SingleTileLPTScheduler`。画出这个三分支决策树。
2. **grid 决定**（[L511-L512](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L511-L512)）：`get_grid_shape` 决定开多少 CTA。
3. **pipeline 创建**（[L628-L693](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L628-L693)）：列出 5 个 pipeline 对象（`pipeline_q/k/v` 用 `PipelineTmaAsync`；`pipeline_P/Scale` 用 `PipelineAsync`）及其 producer/consumer warp group。
4. **warp group 分发**（[L744-L773](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L744-L773)）：按 `warp_group_idx` 把 CTA 的线程分成 producer（WG0）、`mma_wg1`（WG1）、`mma_wg2`（WG2）三路；producer 用 `setmaxregister_decrease` 压低寄存器，MMA WG 用 `setmaxregister_increase` 抬高寄存器。
5. **producer 内部**（[L824-L971](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L824-L971)）：画出「`initial_work_tile_info` → 循环 `producer_acquire/load/commit` → `advance_to_next_work` → `producer_tail`」的骨架，确认它和 4.1.2 里画的 scheduler 调用契约完全一致。

**产出要求**（待本地验证）：一张包含「3 个 WG × 时间轴」的图，标注：

- WG0（producer）何时 `acquire`/`commit` Q/K/V；
- WG1（QK+softmax+PV-front）何时 `consumer_wait` K、做 WGMMA、`producer_acquire` pipeline_P 把 sP 发给 WG2；
- WG2（PV-back）何时 `consumer_wait` pipeline_P、做 WGMMA、`consumer_release`；
- scheduler 在哪几个点被调用（`initial_work_tile_info` / `advance_to_next_work`）。

**加分项**：对照 SM80 通用 kernel [_fwd_generic_sm80.py:L398-L449](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm80.py#L398-L449)，指出它**没有** warp specialization（单 CTA、无 producer WG）、scheduler 只在 `SingleTileScheduler` / `SingleTileVarlenScheduler` 间二选一（[L343-L345](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm80.py#L343-L345)）、也不用 LPT——因为 SM80 路径把因果掩码直接在主循环里 `if not is_causal` 处理，没有为 L2 swizzle 单独开一条调度。

## 6. 本讲小结

- **Tile scheduler 回答「我是谁、算哪块」**：把线性 `block_idx` 解码成 4 轴工作 tile `(m_block, head_idx, batch_idx, split)`；FFPA 用三种 active 实现 + 一种 reserved 实现，共享 `WorkTileInfo` 与 CUTLASS scheduler 协议。
- **三种映射对应三种场景**：`SingleTileScheduler`（恒等映射、dense 非因果）、`SingleTileLPTScheduler`（L2 swizzle + LPT 块序反转、dense 因果）、`SingleTileVarlenScheduler`（warp 级 prefix-sum 定位 batch、varlen）；`SingleTileLPTBwdScheduler` 预留给确定性反向。
- **L2 swizzle 的目的是让连续 CTA 共用 K/V**，swizzle 宽度由 `size_l2(50MB) / 单头KV体积` 向下取 2 的幂决定；**LPT 的目的是负载均衡**，让因果掩码下最重的 tile 先跑。
- **Pipeline 把 g2s 与 MMA 重叠**：`PipelineTmaAsync`（TMA 加载，带 `extra_tx_count`）和 `PipelineAsync`（SMEM 中转，带 elect_one）通过 mbarrier 的 empty/full 双向信用实现多级缓冲背压。
- **NamedBarrier 是屏障 ID 的可读别名**，受 SM90「0–15 号」硬限制；它和 Pipeline 是互补关系——一次性事件用裸 named barrier，有数据流转的握手用 Pipeline。
- **这套抽象来自 flash-attention**：三个文件都标注 copied from Dao-AILab/flash-attention，FFPA 做了 SM90-only 精简（去掉 CLC / cluster / split-KV / 持久化）并保留了若干「Reserved」接口。

## 7. 下一步学习建议

- **下一讲 [u6-l4 CuTeDSL SM90 专用 kernel：d384 / d512 与 generic](u6-l4-cutedsl-sm90-specialized-kernels.md)** 会进入这些 scheduler / pipeline 的**使用者**——具体的 `FFPAAttnFwdSm90SplitD` kernel 类，看 mainloop 如何把 online softmax、Split-D 与本讲的流水线编织在一起。
- 想加深对 mbarrier / TMA 硬件语义的理解，可读 CUTLASS 官方的 [CuTe DSL pipeline 教程](https://github.com/NVIDIA/cutlass/tree/main/examples) 与 PTX 手册的 `mbarrier` / `cp.async.bulk.tensor` 章节——本讲的 `arrive_and_expect_tx`、`phase` 等概念都来自那里。
- 想理解「为什么 flash-attention 要这么 swizzle」，可对比读原版 [Dao-AILab/flash-attention 的 tile_scheduler.py](https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/cute/tile_scheduler.py)（FFPA 版是它的精简子集），看被裁掉的 CLC / 持久化 / cluster 路径在 SM100+ 上如何演化。
- 若打算做二次开发（新增 head_dim 或新场景），回到 [u9-l4 扩展指南](u9-l4-extension-guide.md) 时会再次用到本讲的 scheduler 选型决策树——选错 scheduler（比如 varlen 漏用 prefix-sum）会导致错误的 tile 映射。
