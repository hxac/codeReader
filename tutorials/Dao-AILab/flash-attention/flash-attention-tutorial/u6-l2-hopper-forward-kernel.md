# Hopper 前向 Kernel 与 TMA

## 1. 本讲目标

上一讲（u6-l1）我们走通了 FA4 的 Ampere 前向基线 `FlashAttentionForwardSm80`：Q 常驻、K/V 用 `cp.async` 流水、用 warp 级的 `MmaF16BF16Op` 算两段 GEMM、在线 softmax 累加。本讲在这个基线之上，讲解 Hopper（SM90）专用的 `FlashAttentionForwardSm90` 相对 Ampere 做了哪些升级。

学完后你应当能够：

1. 说清 **warp-group MMA（WGMMA）** 与 Ampere 的 warp 级 MMA 在「谁来算、算多大块、同步方式」上的本质差异。
2. 理解 **TMA（Tensor Memory Accelerator）异步批量拷贝** 如何用单个线程发起整块搬运、并用 **mbarrier 按字节数** 通知完成，取代 Ampere 的 `cp.async` + 计数器。
3. 看懂 SM90 kernel 的 **producer/consumer 线程划分、swizzled 共享内存布局、`fence_view_async_shared` + `warpgroup.wait_group` 的同步模型**，以及 `intra_wg_overlap` 如何跨 warp-group 重叠两段 GEMM。
4. 能用 `FLASH_ATTENTION_ARCH` 强制切换 SM80 / SM90 路径并解释为何数学结果一致。

## 2. 前置知识

本讲默认你已掌握 u6-l1 的内容。回顾几个关键术语：

- **三级存储层次**：全局内存（gmem / HBM）→ 共享内存（smem / SRAM）→ 寄存器（rmem）。前向 kernel 的核心就是把 Q/K/V 在这三层之间搬运并在寄存器/共享内存里做矩阵乘。
- **cp.async（Ampere）**：每个线程发起一次 128-bit 的 gmem→smem 异步拷贝，用 `commit_group` / `wait_group` 这套**计数器**跟踪「还有几组拷贝没完成」。详见 u5-l2、u6-l1。
- **在线 softmax**：用 `row_max`（m）和 `row_sum`（ℓ）两个寄存器张量逐块消化分数，靠重缩放因子维护归一化状态。详见 u4-l1。
- **PipelineStateSimple（u5-l1）**：用一个 Int32 同时编码循环缓冲的槽号 `index` 与圈数 `phase`，靠 phase 奇偶区分「满/空」。
- **命名屏障（u5-l3）**：硬件每个 CTA 有 15 把编号屏障，FA4 用 `enum.IntEnum` 给它们起语义化名字。

补充两个本讲用到、上一讲没展开的硬件概念：

- **WGMMA（Warp-Group Matrix Multiply Accumulate）**：Hopper 新增的矩阵乘指令。一条 WGMMA 指令由**一整个 warp-group（4 个 warp，128 个线程）协同**完成，可以直接从 **smem** 取操作数（A 或 B），结果异步写回寄存器累加器。这和 Ampere 的 warp 级 MMA（一条指令 1 个 warp、32 线程、操作数必须先 `ldmatrix` 搬到寄存器）完全不同。
- **TMA（Tensor Memory Accelerator）**：Hopper 新增的异步拷贝引擎。**单个线程**发起一条 `cp.async.bulk` 指令，硬件就会把一整块张量（最多几千字节）从 gmem 搬到 smem，搬运的「形状、步长、对齐」信息在编译期固化进一个 **TMA descriptor**。完成与否由 **mbarrier** 的 `complete_tx::bytes` 机制按**字节数**判定。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [flash_attn/cute/flash_fwd_sm90.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py) | `FlashAttentionForwardSm90`：Hopper 前向 kernel 的全部实现（WGMMA、TMA、producer/consumer、intra_wg_overlap）。 |
| [flash_attn/cute/flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) | `FlashAttentionForwardBase`（被 Sm90 继承的公共基类，提供 `epilogue` 等）与 `FlashAttentionForwardSm80`（Ampere 基线，作对照）。 |
| [flash_attn/cute/pipeline.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pipeline.py) | FA4 对 cutlass `PipelineTmaAsync` 的薄封装，重写了 `producer_acquire` 以支持按字节数设置 mbarrier。 |
| [flash_attn/cute/named_barrier.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/named_barrier.py) | `NamedBarrierFwd` 枚举，含 SM90 专用的 `WarpSchedulerWG1/2/3`。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | `_get_device_arch`（架构分发，可被 `FLASH_ATTENTION_ARCH` 覆盖）、`_tile_size_fwd_sm90`（tile 配置）、SM80/SM90 kernel 的实例化。 |

> 说明：FA4 的 Hopper 辅助函数（WGMMA 的 gemm 封装、smem 布局生成等）来自外部包 `quack`（`from quack import sm90_utils, copy_utils, layout_utils`）和 cutlass 自带的 `cutlass.utils.hopper_helpers`，不在本仓库内。本仓库没有独立的 `hopper_helpers.py`，讲解时只引用本仓库真实存在的那一层。

## 4. 核心概念与源码讲解

先给一张贯穿全讲的「Sm80 vs Sm90」对照表，后面三个最小模块分别展开其中几行：

| 维度 | Sm80（Ampere 基线） | Sm90（Hopper） |
|---|---|---|
| MMA 指令 | warp 级 `MmaF16BF16Op (16,8,16)`，1 warp = 32 线程 | warp-group 级 **WGMMA**，1 warp-group = 128 线程，一次算 64 行 |
| 操作数来源 | 必须 `ldmatrix` 把 smem→rmem 后再算 | A/B 可直接从 **smem** 取，PV 的 P 还可放寄存器（`mma_pv_is_rs`） |
| gmem→smem 搬运 | `cp.async`，每线程 128-bit，`commit/wait_group` 计数 | **TMA** `cp.async.bulk`，单线程发起整块，**mbarrier 按字节**通知 |
| 流水线状态 | 手写 `smem_pipe_read/write` Int32 | `PipelineTmaAsync` + mbarrier 数组 |
| 线程职责 | 单 kernel，所有 warp 既是搬运又是计算 | **producer / consumer 分离**（前 1 个 warp-group 搬运，后 N 个 warp-group 计算） |
| 流水级数 | `num_stages=1` | `num_stages=2`（更深） |
| 线程数 | 128（4 warps） | 384 = 128 ×（num_wg_mma + 1） |
| smem 布局 | 普通 row-major | **swizzled**（配合 WGMMA 的 bank 冲突规避） |
| 同步原语 | `barrier()` + `cp_async_wait_group` | `warpgroup.wait_group(0/1)` + `fence_view_async_shared()` + 命名屏障 |
| 高级特性 | 无 | `intra_wg_overlap`：跨 warp-group 重叠 QK 与 PV 两段 GEMM |

下面三个模块分别讲：① warp-group MMA，② TMA 与 mbarrier，③ smem 布局与同步（含 producer/consumer 划分与 intra_wg_overlap）。

---

### 4.1 warp-group MMA（WGMMA）

#### 4.1.1 概念说明

Ampere 的 `MmaF16BF16Op` 是**warp 级**指令：一个 warp（32 线程）一条指令算一个 `16×8×16` 的小块，且 A、B 两个操作数都必须先用 `ldmatrix` 从 smem 加载到寄存器（rmem），累加也在寄存器里。要把 `tile_m=128` 行算完，需要 `tile_m/16` 个 warp 重复发射多次指令，搬运与计算的并行度都受限于 warp 内的 32 线程。

Hopper 的 **WGMMA** 是**warp-group 级**指令：一个 warp-group（4 个 warp，128 线程）协同发射一条指令，一次计算一个 `64×N×16` 的块（N 可达 256），并且：

- 操作数 A、B **可以直接从共享内存读取**，无需先 `ldmatrix` 到寄存器——省掉了 Ampere 里一整层 smem→rmem 的搬运。
- 计算是**异步**的：发射后线程可以继续干别的活，过会儿再用 `warpgroup.wait_group(k)` 等待「还有 k 组 WGMMA 在飞」全部完成。
- 累加器更大：一个 warp-group 持有更大的累加器，减少重缩放次数。

这带来的收益是：搬运（TMA）和计算（WGMMA）能更彻底地重叠，且每条指令搬的算的都更多，从而把注意力从「被带宽卡住」推向「算力受限」。

#### 4.1.2 核心流程

前向主循环里单个 n block 仍是两段 GEMM 夹一次在线 softmax：

```
S = Q @ K.T          # 第一段：WGMMA，A=Q(smem), B=K(smem) → acc_S(rmem)
[online softmax]      # 更新 row_max/row_sum，产出 row_scale，rescale 旧 O
P = S 转 fp16         # acc_S(fp32) → P(寄存器或 smem)
O += P @ V            # 第二段：WGMMA，A=P(rmem 或 smem), B=V(smem) → acc_O(rmem)
```

与 Ampere 的差别在于「操作数从哪来」和「谁来发射/等待」：

- QK 那段：A=Q、B=K 都在 smem，WGMMA 直接读 smem。
- PV 那段：A=P。若 `mma_pv_is_rs=True`（RS = register/shared），P 留在寄存器，V 在 smem；否则 P 先写回 smem 再让 WGMMA 读。

#### 4.1.3 源码精读

SM90 的 MMA 通过 `_get_tiled_mma` 构造，用的是 cutlass 的 `make_trivial_tiled_mma` + `warpgroup.OperandMajorMode`：

[flash_attn/cute/flash_fwd_sm90.py:96-118](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L96-L118) —— 构造 QK 与 PV 两段 WGMMA。关键参数：

- `atom_layout_mnk=(self.tile_m // 64, 1, 1)`：每个 WGMMA atom 负责 64 行，所以沿 M 方向排 `tile_m//64` 个 warp-group。
- `tiler_mn=(64, self.tile_n)`：单个 warp-group 覆盖 64×tile_n 的块。
- `a_source=warpgroup.OperandSource.RMEM if self.mma_pv_is_rs else SMEM`：PV 段的 P 来自寄存器（RS 模式）还是共享内存。
- 累加类型 `Float32`。

对比 Ampere 的同函数，可见指令从 warp 级换成了 warp-group 级、操作数来源从「必须寄存器」变成「可共享内存」：

[flash_attn/cute/flash_fwd.py:588-599](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L588-L599) —— Sm80 用 `warp.MmaF16BF16Op(self.dtype, Float32, (16,8,16))`，沿 M 排 `num_threads//32` 个 warp，操作数靠 `ldmatrix` 进寄存器。

WGMMA 的「异步」体现在 `__call__` 里对线程数与寄存器的分配上。SM90 把线程显式分成 producer / consumer 两组，consumer 拿更多寄存器给 WGMMA 累加器：

[flash_attn/cute/flash_fwd_sm90.py:206-217](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L206-L217) —— 由 tiled_mma 推出 `num_wg_mma`（计算用的 warp-group 数，默认 2），`num_threads = 128 × (num_wg_mma + 1)`（多出来的 1 个 warp-group 当 producer），并按 `num_wg_mma` 查表给 MMA / producer 分配寄存器上限（如 num_wg_mma=2 时 MMA 240、producer 24）。

实际的两段 WGMMA 在 consumer 端的 `mma` 里由 `partition_fragment_ABC` 切分操作数、由 `gemm_zero_init` / `gemm_w_idx` 发射：

[flash_attn/cute/flash_fwd_sm90.py:973-982](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L973-L982) —— QK 段切出 `tSrQ, tSrK`（`gemm_zero_init` 初始化 acc_S），PV 段切出 `acc_O, tOrP, tOrVt`（`gemm_w_idx` 累加到 acc_O）。注意这里的「slice」是对 warp-group 取的（`wg_mma_qk = tiled_mma_qk.get_slice(warp_group_thread_layout(warp_group_idx))`），而不是对单个线程——这正是 warp-group MMA 的特征。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，确认「SM90 一次 WGMMA 算 64 行」这一事实，并理解 `num_wg_mma` 如何决定线程数。

**操作步骤**：

1. 打开 [flash_attn/cute/flash_fwd_sm90.py:96-118](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L96-L118)，记下 `atom_layout_mnk=(tile_m//64, 1, 1)` 与 `tiler_mn=(64, tile_n)`。
2. 假设默认配置 `tile_m=128`（来自 `_tile_size_fwd_sm90` 的 hdim128 档），手算：`tile_m//64 = 2`，即 `num_wg_mma = 2`，`num_threads = 128×(2+1) = 384`。
3. 对照 [flash_attn/cute/interface.py:856](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L856)（`num_stages=2`）与 [interface.py:319](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L319)（`num_threads: int = 384`）核对。
4. 再读 Sm80 的 [flash_fwd.py:588-599](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L588-L599)：Ampere 默认 `num_threads=128`（4 个 warp），每个 warp 一条 `16×8×16` MMA。

**需要观察的现象 / 预期结果**：SM90 用 384 个线程、2 个计算 warp-group，每条 WGMMA 由 128 线程协同算 64 行；Sm80 用 128 线程、4 个 warp，每条 MMA 由 32 线程算 16 行。把这两组数字填进一张表，就直观看到了「指令粒度」的跃升。本实践为纯阅读型，无需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么 WGMMA 能省掉 Ampere 里 `ldmatrix` 这一层 smem→rmem 搬运？
**答案**：因为 WGMMA 允许操作数 A/B 直接从共享内存读取（`OperandSource.SMEM`），硬件自己处理 smem 寻址与 bank 冲突，不再需要先把数据搬到寄存器再喂给 MMA 单元。

**练习 2**：PV 段的 `mma_pv_is_rs=True` 中，P 放在寄存器、V 放在共享内存。这样做相对「P、V 都在 smem」有什么好处？
**答案**：P 是上一步 QK GEMM 的结果（已在累加器/寄存器里），把它转成 fp16 后直接留在寄存器作为 PV 段的 A 操作数，省掉一次「寄存器→smem→再被 WGMMA 读」的往返，也省掉一次 smem 容量分配。代价是寄存器压力更大，所以小 hdim（如 ≤128）才开 RS 模式（见 `_tile_size_fwd_sm90`）。

---

### 4.2 TMA 异步拷贝与 mbarrier

#### 4.2.1 概念说明

Ampere 的 `cp.async` 是「每线程搬一小段」：128 个线程各自搬 128-bit，拼出一整块。完成跟踪靠**计数器**——`commit_group` 把若干次拷贝打包成一组，`wait_group(k)` 等到「在飞的不超过 k 组」。它的粒度是「指令组数」，搬了多少字节本身不直接参与同步。

Hopper 的 **TMA** 是「单线程搬一整块」：任意一个线程（通常是 producer warp 的 0 号线程）发射一条 `cp.async.bulk`，硬件按一个**预编译好的 TMA descriptor**（编码了张量形状、步长、swizzle、对齐）把整块张量从 gmem 搬到 smem。完成跟踪改用 **mbarrier** 的 `complete_tx::bytes` 机制——mbarrier 被设置为「期望收到 B 字节」，TMA 每搬一字节就给 mbarrier 记账，攒满 B 字节 mbarrier 翻转相位，consumer 就知道「这块搬完了」。于是同步粒度精确到了**字节数**，天然契合「整块搬运」。

TMA 的好处：

1. **解放线程**：一块 128×128×fp16（32KB）的 K tile，Ampere 要 128 个线程各发 16 条 `cp.async`；TMA 只要 1 个线程发 1 条指令，其余 127 个线程可以同时去算上一块。
2. **硬件处理寻址**：边界、步长、对齐都在 descriptor 里固化，不用线程手算下标、不用谓词，越界自动填零。
3. **更深流水**：配合 mbarrier 可以轻松做 `num_stages=2` 甚至更多的循环缓冲，把 HBM 延迟藏得更深。

#### 4.2.2 核心流程

SM90 前向用三条独立的 TMA 流水线，对应 Q、K、V 三个张量：

```
对每个 m_block（一个 Q tile）:
  producer:
    acquire Q 的 mbarrier 槽 0 (phase)
    发射 TMA: gmem Q tile → smem sQ（单缓冲，1 stage）
    commit（让 mbarrier 期望收到 Q 的字节数）
    for n_block from n_block_max-1 down to n_block_min:
      acquire K 的 mbarrier 槽 index
      发射 TMA: gmem K tile → smem sK[index]
      acquire V 的 mbarrier 槽 index
      发射 TMA: gmem V tile → smem sV[index]
      commit K、V（按各自字节数）
      advance（index/phase 推进）
  consumer:
    等 Q 槽 0 的 mbarrier（字节到齐）→ WGMMA QK
    等 K/V 各槽的 mbarrier → WGMMA PV
    release 已消费的槽（让 producer 能再写）
```

关键点：Q 是**单缓冲**（整个 m_block 只搬一次、反复读），所以 Q 的 mbarrier 只有 `1×2` 把（full/empty 各一）；K/V 是**多级缓冲**（`num_stages=2`），所以各有 `num_stages×2` 把 mbarrier。

#### 4.2.3 源码精读

TMA descriptor 在 `__call__` 里构造。三条 TMA 分别用 `CopyBulkTensorTileG2SOp`（gmem→smem）和 `CopyBulkTensorTileS2GOp`（smem→gmem，给 O 用）：

[flash_attn/cute/flash_fwd_sm90.py:260-301](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L260-L301) —— 用 `cpasync.make_tiled_tma_atom` 为 Q、K、V 各建一个 TMA atom + tma_tensor。注意 K、V 的第 5 个参数 `1` 表示暂不做 multicast。

mbarrier 的存储空间在共享内存里预留，Q 给 `1×2`、K 和 V 各给 `num_stages×2` 把：

[flash_attn/cute/flash_fwd_sm90.py:131-144](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L131-L144) —— `mbar_ptr_Q_struct = MemRange[Int64, 1*2]`，`mbar_ptr_K/V_struct = MemRange[Int64, num_stages*2]`。注释 `1 stage * 2 for Q pipeline (full + empty)` 解释了为何乘 2（每把 mbarrier 还要配一个 empty 伙伴来反压 producer）。

`kernel` 里把这三套流水线实例化为 `PipelineTmaAsync`（Q 单级，K/V 多级），并用 `pipeline_init_arrive/wait` 处理 cluster 握手：

[flash_attn/cute/flash_fwd_sm90.py:458-516](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L458-L516) —— `PipelineTmaAsync.create(...)` 时把 `tx_count=self.tma_copy_bytes["Q"/"K"/"V"]` 传进去，这就是 mbarrier 期望收到的字节数。若 `use_tma_KV=False`（分页 KV 且 page_size≠tile_n 的退化情况），则回退到 `PipelineCpAsync`，回到 Ampere 那套计数器流水。

`PipelineTmaAsync` 的 `producer_acquire` 被 FA4 重写过，关键在于用 `arrive_and_expect_tx` 把字节数告诉 mbarrier：

[pipeline.py:300-330](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pipeline.py#L300-L330) —— `producer_acquire` 先 `wait` 在 empty mbarrier（等 consumer 释放槽位），再 `arrive_and_expect_tx(index, tx_count)` 把 `tx_count` 字节的期望登记到 full mbarrier。之后 TMA 搬运完，硬件按 `complete_tx::bytes` 自动让 full mbarrier 翻转。

producer 端实际发射 TMA 的代码在 `load`：

[flash_attn/cute/flash_fwd_sm90.py:686-690](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L686-L690) —— Q 的 TMA：`copy_utils.tma_get_copy_fn(tma_atom_Q, ..., gQ, sQ, single_stage=True)` 得到一个闭包 `load_Q`，调用它即发射 `cp.async.bulk`。

[flash_attn/cute/flash_fwd_sm90.py:916-934](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L916-L934) —— `load_KV`：TMA 路径直接调 `tma_load_fn(src_idx=...)` 发射搬运，再 `pipeline_kv.producer_commit(producer_state)` 推进状态；非 TMA 路径则退回 `paged_kv_manager.load_KV` + `cp_async_commit_group`。

对比 Ampere 的搬运，差别一目了然——Sm80 没有 TMA，全靠手写 `cp.async` + `commit_group`/`wait_group` 计数器：

[flash_attn/cute/flash_fwd.py:960-989](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L960-L989) —— Sm80 的 prologue：`load_Q` 后 `cp_async_commit_group()`，再循环 `load_K`/`load_V` 各配一个 `commit_group`，用 `smem_pipe_write` 手写下标轮转。注意这里**每个线程都在搬**，且没有 mbarrier。

#### 4.2.4 代码实践

**实践目标**：确认 SM90 的三条 TMA 流水线的「级数 × 2」配置，并理解字节数 `tx_count` 如何参与同步。

**操作步骤**：

1. 读 [flash_fwd_sm90.py:131-144](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L131-L144)，写出 Q / K / V 各预留了多少把 mbarrier（答案：Q=2，K=2×num_stages，V=2×num_stages；num_stages=2 时 K、V 各 4 把）。
2. 读 [flash_fwd_sm90.py:264-271](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L264-L271)，看 `self.tma_copy_bytes` 是怎么按 `size_in_bytes` 算出来的。
3. 读 [pipeline.py:323-327](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pipeline.py#L323-L327)，确认 `extra_tx_count==0` 时用 `arrive(producer_mask)`，否则用 `arrive_and_expect_tx(index, tx_count)`。

**需要观察的现象 / 预期结果**：理解「mbarrier 的 full/empty 配对」与「字节数记账」如何取代 Ampere 的「commit/wait_group 计数」。本实践为纯阅读型，无需 GPU。

**进阶（可选，需 Hopper GPU）**：设置 `CUTE_DSL_KEEP_PTX=1` 运行一次 SM90 前向，在导出的 PTX 里搜索 `cp.async.bulk` 与 `mbarrier.arrive.expect_tx`，确认这两类指令确实出现在编译产物中。具体现象**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Q 的流水线只有 1 级，而 K/V 是多级？
**答案**：一个 m_block 对应一个 Q tile，它被反复用于和所有 n_block 的 K 做 GEMM，所以只需搬一次、单缓冲即可；K/V 则有多个 n_block 要轮转搬入，用多级循环缓冲让「搬下一块」与「算当前块」重叠，隐藏 HBM 延迟。

**练习 2**：TMA 的完成同步是「按字节数」的，这比 Ampere 的「按指令组数」有什么好处？
**答案**：TMA 一次搬运整块、字节数在编译期已知，mbarrier 直接期望该字节数，硬件每搬一字节就记账，无需线程再发射额外的 arrive 指令；既省指令又天然支持「一块 = 一次同步」的粗粒度握手，还能让 multicast / cluster 场景下的字节汇总变得简单。

---

### 4.3 Sm90 共享内存布局与同步

#### 4.3.1 概念说明

有了 WGMMA 和 TMA，还差两块拼图才能拼出 SM90 kernel：**共享内存布局** 与 **线程同步模型**。

**Swizzled 共享内存布局**：WGMMA 要直接从 smem 读操作数，但若 Q/K/V 在 smem 里按朴素 row-major 排列，相邻线程读相邻地址会引发 bank 冲突。Hopper 的解法是 **swizzle**（XOR 乱序）——用 `warpgroup.make_smem_layout_atom` 生成一个带 swizzle 的布局，让 WGMMA 的访问模式恰好不撞 bank。FA4 把这个布局生成交给 `quack.sm90_utils.make_smem_layout`，并把 K/V 的多级缓冲维度（`num_stages`）编进布局。

**Producer / Consumer 线程划分**：Ampere 是「所有 warp 既搬又算」，靠 `wait_group` 在搬运和计算之间切换。Hopper 把线程**显式二分**：

- 前 1 个 warp-group（warp_idx 0~3，128 线程）当 **producer**：只负责发射 TMA（其中 warp 0 真正发指令，其余空转或做 cp.async 退化路径）。寄存器配额压到很小（`setmaxregister_decrease`）。
- 后 `num_wg_mma` 个 warp-group（默认 warp_idx 4~11，256 线程）当 **consumer**：只负责 WGMMA 计算，寄存器配额拉大（`setmaxregister_increase`）给累加器。

这样搬运和计算真正并行，且互不抢寄存器。

**同步模型**：WGMMA 是异步的，操作数又在 smem，于是需要三类同步：

1. **`warpgroup.wait_group(k)`**：等「还有不超过 k 组 WGMMA 在飞」。通常 PV 段用 `wait_group(0)`（全等完），QK 段在 overlap 模式下用 `wait_group(1)`（允许 1 组在飞，好让搬运重叠）。
2. **`fence_view_async_shared()`**：当某个 warp 把数据（比如 P）写进 smem、要让 WGMMA 读时，必须先发这条 fence，保证「异步 store 已对全 warp-group 可见」。
3. **命名屏障 `WarpSchedulerWG1/2/3`**：在 `intra_wg_overlap` 模式下，多个计算 warp-group 轮流接力处理同一个 n block 的「QK 半步」和「PV 半步」，靠这几把命名屏障约先后。

**intra_wg_overlap（warp-group 内重叠）**：默认开启。它把一个 n block 的处理拆成「先做 QK + softmax（first_half_block）」和「再做 PV（last_half_block）」两半，让 warp-group A 在做 block i 的 PV 时，warp-group B 已经在做 block i+1 的 QK——跨 warp-group 重叠两段 GEMM，进一步提升吞吐。

#### 4.3.2 核心流程

SM90 的 `kernel` 入口在做了 TMA descriptor 预取、流水线实例化、cluster 握手之后，按 `warp_idx` 二分：

```
prefetch_descriptor(Q/K/V/O 的 TMA atom)     # 仅 warp 0
分配 smem、构造 sQ/sK/sV/sVt/sO
pipeline_init_arrive(cluster)                 # cluster 握手上半
构造 BlockInfo / SeqlenInfo / AttentionMask / TileScheduler
pipeline_init_wait(cluster)                   # cluster 握手下半

if warp_idx < 4:           # producer（前 1 个 warp-group）
    setmaxregister_decrease(num_producer_regs)
    self.load(...)         # 发射 Q/K/V 的 TMA，acquire/commit mbarrier
else:                      # consumer（后 num_wg_mma 个 warp-group）
    setmaxregister_increase(num_mma_regs)
    self.mma(...)          # WGMMA 主循环：等 mbarrier → QK → softmax → PV
```

consumer 主循环里（`mma` 方法），对每个 n block：

```
acquire K 槽 (wait mbarrier)         # 等这块 K 搬完
WGMMA: S = Q @ K.T  (wait_group -1)  # 异步发射，不立即等
acquire V 槽
WGMMA: O += P @ V  (wait_group)      # 等上一段完成
release K/V 槽                       # 通知 producer 可写下一块
[softmax: row_max/row_sum/rescale]
fence_view_async_shared()            # 让 P 的 smem store 可见（非 RS 模式）
```

`intra_wg_overlap` 模式下这段被拆进 `first_half_block_overlap`（QK + softmax + 写 P）与 `last_half_block_overlap`（PV），中间用 `WarpSchedulerWG` 命名屏障接力。

#### 4.3.3 源码精读

**Swizzled smem 布局**：SM90 重写 `_get_smem_layout_atom` 用 `warpgroup.make_smem_layout_atom`：

[flash_attn/cute/flash_fwd_sm90.py:72-94](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L72-L94) —— Q/K 共用一个 atom（行序、tile_hdim），V/O 共用一个（tile_hdimv）；若 PV 不走 RS（`not mma_pv_is_rs`），额外给 P 建一个 `(tile_m, tile_n)` 的 smem 布局。对比 Sm80 用的是 `sm80_utils.get_smem_layout_atom`（朴素布局，无 swizzle）。

**Producer / Consumer 二分**：这是 SM90 与 Ampere 在结构上最大的不同——

[flash_attn/cute/flash_fwd_sm90.py:580-636](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L580-L636) —— `if warp_idx < 4: # Producer` 走 `self.load` 并 `setmaxregister_decrease`；`else: # Consumer` 走 `self.mma` 并 `setmaxregister_increase`，且 consumer 把 `tidx` 减去 128（跳过 producer 那个 warp-group）来定位自己在 warp-group 内的线程号。Ampere 的 [flash_fwd.py:745](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L745) 起的 `kernel` 没有这种二分，所有线程一起又搬又算。

**异步 WGMMA 的等待与 fence**：在 `mma_one_n_block`（不开 overlap 的主循环体）里：

[flash_attn/cute/flash_fwd_sm90.py:1368-1407](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L1368-L1407) —— `pipeline_k.consumer_wait`（等 K 的 mbarrier）→ `mma_qk_fn(..., wg_wait=-1)`（发射 QK，允许 1 组在飞）→ `warpgroup.wait_group(0)`（确保 QK 完成）→ `pipeline_k.consumer_release`（释放 K 槽）→ softmax → `cute.arch.fence_view_async_shared()` + `sync_warp()`（让 P 的 smem store 对 WGMMA 可见，仅非 RS 模式）→ `mma_pv_fn(..., wg_wait=0)`（PV 累加）。这套 `wait_group` + `fence` 是 Ampere 完全没有的。

对比 Ampere 的同功能函数，它用 `cp_async_wait_group` + `barrier()` 同步搬运，用同步的 `gemm`/`gemm_rs`：

[flash_attn/cute/flash_fwd.py:1108-1146](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1108-L1146) —— Sm80 的 `compute_one_n_block`：`cp_async_wait_group(num_stages*2-2)` + `barrier()` 等数据，再调同步的 `sm80_utils.gemm`。

**intra_wg_overlap 与 WarpScheduler 命名屏障**：开 overlap 时用 `mma_one_n_block_intrawg_overlap`，它把 QK 和 PV 拆给不同 warp-group 接力：

[flash_attn/cute/flash_fwd_sm90.py:1410-1477](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L1410-L1477) —— 注意 `warpgroup.wait_group(1)`（L1442，允许 QK 还在飞时就开始下一块 PV）与 `warpgroup.wait_group(0)`（L1453），以及 `self.warp_scheduler_barrier_sync()` / `arrive()`（L1432、L1441）负责让多个计算 warp-group 轮流接力。

命名屏障的定义在这里：

[flash_attn/cute/named_barrier.py:6-12](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/named_barrier.py#L6-L12) —— `NamedBarrierFwd` 里 `WarpSchedulerWG1/2/3` 专为 SM90 的 intra_wg_overlap 准备（支持最多 3 个计算 warp-group 接力）。`warp_scheduler_barrier_sync/arrive` 的实现见 [flash_fwd_sm90.py:1524-1545](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L1524-L1545)，用 `barrier_id + canonical_warp_group_idx` 让每个 warp-group 拿到自己的屏障。

**O 的 TMA 回写**：SM90 的 `use_tma_O = use_tma_Q`（[flash_fwd_sm90.py:225-228](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L225-L228)），所以输出 O 也走 TMA（`CopyBulkTensorTileS2GOp`），由基类的 `epilogue` 处理：

[flash_attn/cute/flash_fwd.py:398-417](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L398-L417) —— `use_tma_O` 分支：`fence_view_async_shared()` + `barrier_arrive(Epilogue)` 让 smem 的 O 对 TMA 可见，warp 4 用 `tma_get_copy_fn` 发射 `cp.async.bulk` 把 sO 写回 gmem，再 `cp_async_bulk_wait_group(0)`。Ampere（`use_tma_O=False`）走 else 分支用 universal copy。

#### 4.3.4 代码实践

**实践目标**：在源码里把 SM90 的「同步三件套」逐一标出来，理解它们各自防的是哪类冒险。

**操作步骤**：

1. 在 [flash_fwd_sm90.py:1368-1407](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L1368-L1407) 中找出：
   - 等 K 数据到齐的那行（`pipeline_k.consumer_wait`）；
   - 等 WGMMA 完成的那行（`warpgroup.wait_group(0)`）；
   - 让 P 的 smem 写对 WGMMA 可见的那行（`fence_view_async_shared`）。
2. 在 [flash_fwd_sm90.py:580-636](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L580-L636) 确认 producer/consumer 各自调了 `setmaxregister_decrease` / `setmaxregister_increase`，并思考为何要分寄存器配额。
3. 把这三类同步填进下表（已在下方「预期结果」给出）。

**需要观察的现象 / 预期结果**：

| 同步原语 | 防的冒险 |
|---|---|
| `pipeline_k.consumer_wait`（mbarrier） | consumer 在 K/V 搬完前就开算（RAW：写后读） |
| `warpgroup.wait_group(0)` | 在 WGMMA 结果还没落回累加器前就读 acc_S/acc_O |
| `fence_view_async_shared()` | warp 把 P 写进 smem 后，WGMMA（异步）读到旧值 |
| `WarpSchedulerWG1/2/3` | overlap 模式下多个 warp-group 抢着处理同一个半步 |

本实践为纯阅读型，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么 consumer 要 `setmaxregister_increase`（拿更多寄存器），而 producer 要 `decrease`？
**答案**：consumer 跑 WGMMA，需要大累加器（acc_S、acc_O）和缓存的 Q/P 片段，寄存器越多越能展开；producer 只发 TMA（主要工作是等 mbarrier、发一条指令），几乎不需要寄存器，压低它的配额能把省下来的寄存器预算让给 consumer，整体 occupancy 更高。

**练习 2**：`intra_wg_overlap` 模式下，为什么 QK 段用 `wait_group(1)` 而 PV 段（在 `last_half_block_overlap` 里）用 `wait_group(0)`？
**答案**：QK 段允许「还有 1 组在飞」，是为了让搬运下一块 K/V 与当前 QK 计算重叠；而 PV 段是「收尾」，必须等当前所有 WGMMA 落回 acc_O 才能 finalize（归一化、写 LSE），所以 `wait_group(0)` 全部等完。

---

## 5. 综合实践：强制切换 SM80 / SM90 路径并对比

本任务把三个最小模块串起来：用同一个输入分别走 Ampere 与 Hopper 两条前向路径，验证它们**数学等价**（误差仅来自 fp16 舍入），并对照源码列出 Sm90 多用到的硬件特性。

### 背景：架构如何分发

FA4 在 Python 层用 `_get_device_arch()` 探测整数 arch，可用环境变量 `FLASH_ATTENTION_ARCH` 覆盖（注意它被 `@lru_cache` 缓存，必须在首次调用前设置）：

[flash_attn/cute/interface.py:76-92](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L76-L92) —— 读 `FLASH_ATTENTION_ARCH`，否则用 `torch.cuda.get_device_capability()`。

分发逻辑按 `arch // 10` 选 kernel 类：`8`→Sm80、`9`→Sm90：

[flash_attn/cute/interface.py:823-866](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L823-L866) —— Sm80 用 `num_stages=1`、`num_threads=128`；Sm90 用 `num_stages=2`、`num_threads=384`、并多带 `intra_wg_overlap` / `mma_pv_is_rs` 两个旋钮（来自 `_tile_size_fwd_sm90`，见 [interface.py:123-155](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L123-L155)）。

### 实践脚本

> 运行前提：需要一张 **Hopper（H100/H200）** GPU。因为 SM90 kernel 有 `assert self.arch.is_family_of(Arch.sm_90a)`（[flash_fwd_sm90.py:70](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L70)），在非 Hopper GPU 上无法真正执行 SM90 路径；而在 Hopper 上可以通过 `FLASH_ATTENTION_ARCH=sm_80` 强制走 Ampere 路径（PTX 向前兼容）。若你只有 Ampere GPU，本脚本的 `sm_90` 分支会失败，请把 `sm_90` 分支标记为「待本地验证」并只跑 `sm_80`。
>
> 此外，`FLASH_ATTENTION_ARCH` 只决定 kernel 选择；要让 PTX 编译目标也匹配，通常还需配合 `CUTE_DSL_ARCH`。下脚本采用「进程级隔离」：每个 arch 用独立子进程、在 import 前设好环境变量。

```python
# compare_sm80_sm90.py  —— 示例代码（非项目自带脚本）
import os
# 必须在 import torch / flash_attn 之前设置，因为 _get_device_arch 被 lru_cache
os.environ["FLASH_ATTENTION_ARCH"] = os.environ.get("TARGET_ARCH", "sm_90")
os.environ.setdefault("CUTE_DSL_ARCH", os.environ["FLASH_ATTENTION_ARCH"])

import torch
from flash_attn.cute import flash_attn_func
from flash_attn.cute.interface import _get_device_arch

torch.manual_seed(0)
b, s, h, d = 2, 512, 8, 128
q = torch.randn(b, s, h, d, dtype=torch.float16, device="cuda") * 0.1
k = torch.randn(b, s, h, d, dtype=torch.float16, device="cuda") * 0.1
v = torch.randn(b, s, h, d, dtype=torch.float16, device="cuda") * 0.1

print("selected arch =", _get_device_arch())   # 确认分发到了哪条路径
out, lse = flash_attn_func(q, k, v, causal=True)   # 首次调用会 JIT 编译，慢
print("out.shape", out.shape, "lse.shape", lse.shape, "lse.dtype", lse.dtype)

# 把 out, lse 存盘，换一个 arch 再跑一遍后读回来对比
torch.save({"out": out.cpu(), "lse": lse.cpu()}, f"result_{os.environ['FLASH_ATTENTION_ARCH']}.pt")
```

运行方式（两个独立子进程，避免 lru_cache 串味）：

```bash
TARGET_ARCH=sm_80 python compare_sm80_sm90.py
TARGET_ARCH=sm_90 python compare_sm80_sm90.py
python -c "
import torch
a = torch.load('result_sm_80.pt'); b = torch.load('result_sm_90.pt')
print('max |O_sm80 - O_sm90| =', (a['out']-b['out']).abs().max().item())
print('max |LSE_sm80 - LSE_sm90| =', (a['lse']-b['lse']).abs().max().item())
"
```

### 需要观察的现象 / 预期结果

1. 两次 `selected arch` 分别打印 `80` 与 `90`（确认分发成功）。
2. `out.shape == (2, 512, 8, 128)`、`lse.shape == (2, 8, 512)`、`lse.dtype == torch.float32`（LSE 形状是 `(batch, num_heads, seqlen_q)`，详见 u2-l1）。
3. `max |O_sm80 - O_sm90|` 与 `max |LSE_sm80 - LSE_sm90|` 应为**很小的数**（量级 ~1e-2 或更小，随 fp16 舍入浮动）——两条路径数学等价，差异仅来自浮点累加顺序不同。**具体数值待本地验证。**
4. 首次调用耗时远大于第二次（JIT 编译 vs 命中缓存，详见 u11-l1）。

### 列出 Sm90 多用到的 3 项硬件特性

对照源码，SM90 路径相对 Sm80 多用到的关键硬件特性（任选 3 项即可）：

1. **WGMMA（warp-group MMA）**：`_get_tiled_mma` 用 `warpgroup.OperandMajorMode` + `atom_layout_mnk=(tile_m//64,1,1)`（[flash_fwd_sm90.py:96-118](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L96-L118)），操作数直接来自 smem/rmem，由 `warpgroup.wait_group` 异步等待。
2. **TMA（cp.async.bulk）**：用 `CopyBulkTensorTileG2SOp` / `CopyBulkTensorTileS2GOp` 构造 TMA atom（[flash_fwd_sm90.py:260-301](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L260-L301)），单线程发起整块搬运，完成由 mbarrier 的 `complete_tx::bytes` 判定（[pipeline.py:323-327](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pipeline.py#L323-L327)）。
3. **mbarrier + producer/consumer 二分 + setmaxregister**：线程按 `warp_idx < 4` 二分（[flash_fwd_sm90.py:580-636](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L580-L636)），配合 `setmaxregister_decrease/increase` 与 swizzled smem 布局（[flash_fwd_sm90.py:72-94](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm90.py#L72-L94)）。
4. **intra_wg_overlap**：用 `WarpSchedulerWG1/2/3` 命名屏障（[named_barrier.py:6-12](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/named_barrier.py#L6-L12)）跨 warp-group 重叠 QK 与 PV。

> 若无法运行：把脚本当作阅读脚手架，重点完成「列出 3 项硬件特性」并附上对应源码链接即可。

## 6. 本讲小结

- **WGMMA 取代 warp 级 MMA**：一条指令由 128 线程协同算 64 行，操作数直接来自 smem/rmem，计算异步化，由 `warpgroup.wait_group` 等待。
- **TMA 取代 cp.async**：单线程按预编译 descriptor 发射整块 gmem↔smem 搬运，完成由 mbarrier 的 `complete_tx::bytes` 按字节数判定；Q 单级、K/V 多级三条独立流水线。
- **线程二分**：前 1 个 warp-group 当 producer（低寄存器、发 TMA），后 `num_wg_mma` 个 warp-group 当 consumer（高寄存器、跑 WGMMA），搬运与计算真正并行。
- **同步三件套**：mbarrier（等数据）、`warpgroup.wait_group`（等 WGMMA）、`fence_view_async_shared`（让 smem store 对异步 WGMMA 可见）。
- **Swizzled smem 布局 + intra_wg_overlap**：swizzle 规避 bank 冲突；`WarpSchedulerWG1/2/3` 命名屏障让多个计算 warp-group 接力重叠两段 GEMM。
- **架构分发不影响数学**：SM80 与 SM90 是同一公式的不同实现，`FLASH_ATTENTION_ARCH` 只切实现路径，输出差异仅来自浮点舍入。

## 7. 下一步学习建议

- 下一讲（u7）进入前向高级特性：`pack_gqa`（u7-l1）、SplitKV 与 Combine kernel（u7-l2）、Paged KV（u7-l3）。其中 SplitKV 会复用本讲提到的 LSE（log-sum-exp）做跨 split 合并，建议带着「LSE 是 online softmax 的天然产物」这一认知去读。
- 若你对 WGMMA/TMA 的硬件细节感兴趣，可先读 u5-l2（copy_utils 与 TMA）与 u5-l3（命名屏障与 mbarrier），再回头看本讲的同步代码会更顺。
- 专家层 u8 会把同样的「TMA + 异步 MMA + persistent + 命名屏障」模式推到 Blackwell（SM100），届时会引入 UMMA（tcgen05）、片上 tmem 累加与 2CTA cluster，本讲是它的直接前置。
- 想验证编译产物，可参考 u11-l5（GPU Kernel 调试与 PTX/SASS），用 `CUTE_DSL_KEEP_PTX=1` 导出 PTX 检查 `cp.async.bulk`、`wg.mma`、`mbarrier` 等指令。
