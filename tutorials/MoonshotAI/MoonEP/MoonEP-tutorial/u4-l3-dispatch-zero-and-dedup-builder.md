# u4-l3 dispatch 的零填充 warp 与去重构建 warp

## 1. 本讲目标

上一讲（u4-l2）我们读完了 `DispatchKernel` 的数据通路：warp 0 生产、warp 1 消费，按行 TMA 把 token 直写到远端 NVL shard。但一个完整的 dispatch 还要回答两个数据通路覆盖不到的问题：

1. **接收分段的 padding 行谁来写？** 规划器把每 rank 的接收缓冲切成 E+B 个段，段长向上对齐到 `token_padding`。分组 GEMM（如 DeepGEMM）按 `cu_seqlens` 读整段，包括没人派发 token 的 padding 行——这些行里是上一轮迭代的脏数据，必须清零。
2. **重复槽的元数据谁来建？** 发送端用负数 dst 省掉了重复 payload 的传输（u3-l5），但接收端必须知道「哪个主行要扇出到哪些重复槽」，这个去重结构（`dup_groups`/`dup_loffs`/`dup_counts`）要在接收端被物化出来。

这两个任务分别由 dispatch 内核的 **warp 2（零填充 warp）** 和 **warp 3..6（去重构建 warp）** 完成。本讲学完后你应该能：

- 说清 `zero_fill_ranges` 与零填充 warp 的逐行对应关系，以及它与数据通路并发执行却不产生竞争的原因。
- 掌握三个 builder scratch——`primary_packed`、`kmask`、`kidx_to_loff`——的位编码含义与各自的写入方式（原子 / 非原子）。
- 理解 dedup 结构「组间顺序不稳定、组内顺序稳定」的成因（warp 级 `atomicAdd` 预留），以及为什么测试必须比较集合语义。
- 能对一个小规模用例手工推导 builder 的全部中间状态。

## 2. 前置知识

本讲默认你已读过前几讲的关键结论，快速回顾：

- **接收分段与 padding**（u2-l1 / u3-l3）：每 rank 的接收缓冲逻辑上是 E+B 个段（E 个本名专家段 + B 个预取槽段），非空段长向上取整到 `token_padding` 的倍数，总槽位 `NvS = S·K + (token_padding−1)·2·E/R`。规划器为每段输出 `(pad_start_loff, n_pad_rows)`，即 `zero_fill_ranges`，形状 `[E+B, 2]`。
- **负数 dst 编码**（u3-l5）：同一 token 的多个 top-k 条目落到同一目的 rank 时，仅 k 序最小的条目保持非负 dst 并拷贝 payload，其余写 `-raw_dst - 1`，只散射权重不拷 payload。接收端需要把这些重复槽「补」出来。
- **src_info 溯源**（u3-l4）：planning 的 passB 阶段把每个槽位的来源写成 `src_rank * NvS + offv`（`offv = token*K + kidx`），远程写入目的 rank 的 meta_buf `SRC_INFO` 区；`-1` 是空槽哨兵。这是 builder 的唯一输入。
- **cp.async.bulk S2G 与 bulk_group**（u4-l1）：S2G（shared→global）拷贝用 `cp.async.bulk ... .bulk_group` 编组，发射后要 `commit_group`，用 `wait_group(N)` 等待直到在飞组数 ≤ N。
- **自复位屏障**（u3-l6 / u4-l1）：`grid_sync`/`cross_rank_barrier`/`cross_warp_sync` 都用「计数器 + 哨兵位翻转（TAG）」实现免清零复用；`builder_bar` 是其中一种，初始化为 0 后不再清理。

还需要三个本讲新用到的 GPU 位运算原语（都封装在 [moonep/_common.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py) 中）：

| 助手 | 等价 PTX / 含义 |
|---|---|
| `atom_min_relaxed_gpu_s32`（[L109](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L109)） | `atom.min.relaxed.gpu.global.s32`，多线程并发取最小值 |
| `atom_or_relaxed_gpu_b32`（[L142](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L142)） | `atom.or.relaxed.gpu.global.b32`，多线程并发按位或 |
| `popc_b32` / `ctz_b32`（[L159](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L159) / [L176](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L176)） | `popc`（置位数）/ `ctz`（最低置位的位置，即 trailing zeros） |

`relaxed` 内存序意味着只保证原子性、不单独携带同步语义——builder 各 pass 之间的可见性由显式屏障（Phase 1/2）补齐。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [moonep/dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py) | 本讲主战场：warp 2 零填充（L450-L497）、warp 3.. 去重构建（L499-L681）、宿主侧边界检查与 scratch 接线 |
| [moonep/constants.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py) | packed 编码的位宽单一事实来源：`KIDX_BITS`、`DEDUP_BUILDER_WARPS` |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | 上游：`zero_fill_ranges` 的生成（Phase C）、`src_info` 的清零与发布、`warp_inclusive_scan` 助手、`MoonEPCommPlan` 的 dedup 字段契约 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | builder scratch 的分配（`primary_packed` 等）与 meta_buf `SRC_INFO` 区布局 |
| [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) | dedup 结构的 PyTorch 参考实现（确定性顺序），实践对拍的依据 |
| [moonep/dispatch_epilogue.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py) | 下游：消费 dedup 三件套做重复展开（下一讲 u4-l4 的主角，本讲只看契约） |

## 4. 核心概念与源码讲解

先建立全景。dispatch 内核按 warp 特化分工，`DispatchKernel` 的类文档与常量定义了布局（[moonep/dispatch.py:L50-L81](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L50-L81)）：

```python
# 3 fixed warps (producer + consumer + zero) + DEDUP_BUILDER_WARPS
# dedup builder warps on fresh-planning paths.
num_threads = 96 + 32 * DEDUP_BUILDER_WARPS
PRODUCER_WARP = 0
CONSUMER_WARP = 1
ZERO_WARP = 2
DEDUP_BUILDER_WARP = 3
```

| warp | 角色 | 运行路径 |
|---|---|---|
| 0 | G2S 生产者（u4-l2 已讲） | 所有路径 |
| 1 | S2G 消费者 + 权重散射（u4-l2 已讲） | 所有路径 |
| 2 | 零填充：清 padding 行 + 配对权重槽 | **所有路径（含 plan 复用）** |
| 3..6 | 去重构建：物化 dedup 三件套 | **仅 fresh planning（`build_dedup_map=True`）** |

两条路径的线程数在构造期分岔（[moonep/dispatch.py:L113-L115](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L113-L115)）：fresh 路径 96 + 32×4 = 224 线程（7 warp），复用路径 96 线程（3 warp），builder 分支被 `const_expr` 编译期整体剔除，省掉 4 个 warp 的启动开销。这个差异的语义依据写在 `launch_dispatch` 的文档里（[moonep/dispatch.py:L861-L864](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L861-L864)）：`build_dedup_map` 仅在紧随 fresh planning 之后为真，复用与反向路径传 False，避免从**过期的 src_info** 重建出错误的 dedup 结构——已保存的 `dup_groups`/`dup_loffs`/`dup_counts` 随 plan 原样复用。

注意内核里的分支结构：warp 0/1 是一组 `if/elif`，warp 2 与 warp 3.. 是**另一组** `if/elif`（[L360](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L360)、[L467](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L467)、[L502](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L502)）。也就是说同一 CTA 内 7 个 warp 同时在飞：warp 0/1 走数据通路的同时，warp 2 在清零、warp 3..6 在建表——三股工作完全并发，靠「写集合互不相交」避免竞争，最后在出口的 `cross_rank_barrier` 汇合（[L683-L690](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L683-L690)，u4-l2 已精读）。

### 4.1 零填充 warp（zero warp）

#### 4.1.1 概念说明

回忆 u3-l3：规划器为每个接收段计算对齐后的段长，`cu_seqlens` 记录每段的** padded 结束偏移**，分组 GEMM 就按 `cu_seqlens` 切段读取。于是接收缓冲的每一行属于且仅属于三类之一：

1. **真实 token 行**：warp 1（消费者）按非负 dst 经 TMA 写入；
2. **padding 行**：段尾对齐补出来的空行，`token_count < padded_count` 时有 `(pad_start, n_pad) = (base + token_count, padded_count - token_count)`；
3. **空段的整段**：`token_count = 0` 的段不占空间，不存在行。

问题就在第 2 类：hidden_buf 是一块反复复用的驻留缓冲，padding 行没人写，里面就是**上一轮迭代的旧 token 数据**。分组 GEMM 无法区分真实与 padding 行（它只看段边界），旧数据会被当成本轮 token 参与矩阵乘——这是一个静默的正确性 bug。零填充 warp 的职责就是：在每次 dispatch 的同时，把本 rank 分段内所有 padding 行清成 bf16 零；当 `with_weights=True` 时，把 meta_buf 里与之配对的路由权重槽也清成 0。

它每次 dispatch 都要跑（包括 plan 复用路径），因为每次迭代的分段布局都随路由变化，脏数据风险是逐轮存在的。

#### 4.1.2 核心流程

```
warp 2（每个 CTA 都有一个；lane 0 发射拷贝，全 warp 参与流控计数）

for e in bidx, bidx + num_sms, ... (grid-stride 遍历 E+B 个段):
    (pad_start, n_pad) = zero_fill_ranges[e]        # 一次连续 int2 读
    if n_pad > 0:
        for j in 0 .. n_pad-1:
            loff = pad_start + j
            local_row = rank * NvS_padded + loff    # 只写本 rank 自己的 shard
            lane 0:
                cp.async.bulk S2G: zero_smem[H] -> hidden_buf[local_row]  # H*2 字节
                commit_group
                if with_weights:
                    meta[rank, weights_off + loff] = 0        # int32 0 == fp32 0.0
            if issued >= stages - 1:
                wait_group(stages - 1)              # 在飞 bulk 组数 <= stages-1
            issued += 1
wait_group(0)                                       # 收尾排空
```

三个设计要点：

- **写谁**：只写本 rank 自己的分段（`rank * NvS_padded + loff`），因为 `zero_fill_ranges` 是 Phase D 从 rank0 组播里拉取的**本 rank 视角**切片（u3-l6）。每个 rank 的零填充 warp 清自己 shard 的 padding，互不越界。
- **零从哪来**：不是现场生成，而是一块启动时一次性清零的共享内存 `zero_smem`，每行 S2G 都从它拷出——16 字节对齐、内容恒零，是 bulk copy 的理想源。
- **流控**：S2G bulk copy 是异步编组的，无界发射会把 SM 的 bulk_group 资源耗尽。这里复用了流水线深度 `stages` 作为在飞上限（与 warp 1 的节流策略一致，u4-l2 的 `li >= stages-1` 判断同款），`wait_group(stages-1)` 表示「等到最多剩 stages-1 个未完成组」。

#### 4.1.3 源码精读

**（a）零源头的准备：`zero_smem` 的一次性初始化。** 内核开头、任何分支之前，全部 `num_threads` 个线程协作把 `zero_smem` 清零（[moonep/dispatch.py:L339-L349](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L339-L349)）：

```python
# ----- zero_smem one-shot init (reused by every padding s2g from
# warp 2). All NUM_THREADS cooperatively zero H bf16 elements, then
# a single fence publishes the cta-scoped smem writes to the
# cp.async.bulk async proxy for the rest of the kernel.
H_PER_THR_ZERO = cutlass.const_expr((H + num_threads - 1) // num_threads)
for j in cutlass.range_constexpr(H_PER_THR_ZERO):
    idx = j * num_threads + tidx
    if idx < Int32(H):
        zero_smem[idx] = BFloat16(0)
cute.arch.fence_view_async_shared()
cute.arch.barrier()
```

`fence_view_async_shared()` 是关键一步：cp.async.bulk 走的是**异步代理（async proxy）**，普通 smem 写必须经这道 fence 才对异步拷贝可见；随后的 `barrier()`（CTA 级 `__syncthreads`）保证所有线程都写完并看到 fence 之后，warp 2 才可能开始发射。smem 里的这块零区域在 L322-L326 分配（[moonep/dispatch.py:L322-L326](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L322-L326)），128 字节对齐满足 bulk copy 对齐要求。

**（b）`zero_fill_ranges` 的读取布局。** 宿主侧把它线性化成长度 `2*(E+B)` 的一维张量，使 warp 2 对每个段的 `(pad_start, n_pad)` 两连读落在连续地址上（[moonep/dispatch.py:L207-L213](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L207-L213)）：

```python
# zero_fill_ranges: int32 [E+B, 2] — col 0 pad_start_loff, col 1 n_pad_rows.
# Linearized as length 2*(E+B) so warp 2's per-group read pulls both
# values via a single contiguous int2 load.
zero_fill_ranges_tensor = cute.make_tensor(
    zero_fill_ranges_ptr,
    cute.make_layout((2 * cutlass.const_expr(self.zero_groups),)),
)
```

**（c）零填充主循环。** [moonep/dispatch.py:L450-L497](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L450-L497)（L450-L466 是注释块，代码从 L467 起）：

```python
if warp_idx == self.ZERO_WARP:
    issued = Int32(0)
    e = Int32(bidx)
    while e < Int32(zero_groups):
        pad_start = zero_fill_ranges_tensor[e * Int32(2) + Int32(0)]
        n_pad = zero_fill_ranges_tensor[e * Int32(2) + Int32(1)]
        if n_pad > Int32(0):
            for j in cutlass.range(n_pad, unroll=1):
                loff = pad_start + Int32(j)
                local_row = Int32(rank) * Int32(NvS_padded) + loff
                if cute.arch.lane_idx() == 0:
                    g_row_int = (gmem_dst.iterator
                        + cutlass.Int64(local_row) * cutlass.Int64(H)).toint()
                    z_smem_int = zero_smem.iterator.toint()
                    cp_async_bulk_s2g(z_smem_int.ir_value(),
                                      g_row_int.ir_value(),
                                      Int32(H_BYTES).ir_value())
                    cute.arch.cp_async_bulk_commit_group()
                    if cutlass.const_expr(self.with_weights):
                        meta_tensor[Int32(rank) * meta_stride + weights_off + loff] = Int32(0)
                if issued >= Int32(stages - 1):
                    cute.arch.cp_async_bulk_wait_group(stages - 1)
                issued += Int32(1)
        e += Int32(self.num_sms)
    cute.arch.cp_async_bulk_wait_group(0)
```

逐点解读：

- **grid-stride 分段**：`e` 从 `bidx`（CTA 编号）出发、步长 `num_sms`，E+B 个段在所有 CTA 间均匀摊开。每个 CTA 的 warp 2 独立处理自己名下段的全部 padding 行。
- **只写本 rank 行**：`local_row = rank * NvS_padded + loff`。注意 `NvS_padded`（VMM 对齐后的物理行距）而非 `NvS`——shard 内寻址必须按物理布局（u2-l2）。
- **行号用 Int64**：`Int64(local_row) * Int64(H)`，与数据通路同一防溢出策略（`R*NvS_padded*H` 生产上可超 2^31）。
- **lane 0 单线程发射**：与 warp 1 同理，bulk copy 由单 lane 发射，32 个 lane 重复发射会重复拷贝、重复 commit。权重槽零写是普通 st，也放在 lane 0 里与拷贝顺序配对。`int32 0` 直接当 `fp32 0.0` 写——位模式全零两者相同，省一次类型转换（注释见 [L456-L459](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L456-L459)）。
- **流控阈值**：`issued >= stages - 1` 才 `wait_group(stages - 1)`，即前 `stages-1` 次发射无等待，之后每次发射前先排空到上限以内；循环外 `wait_group(0)` 排空尾部队列，保证出口屏障前所有零写落盘。

**（d）为什么与数据通路并发是安全的？** 注释 [moonep/dispatch.py:L460-L465](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L460-L465) 给出论证：**每一行至多一个写者**——真实 token 行只由 dispatch 消费者写（可能来自远端 rank 的 NVL 写），padding 行只由本 rank 的 zero warp 写，两个集合按定义不相交，因此无需任何锁。可见性由出口 `cross_rank_barrier` 的 grid_sync + system-scope release/acquire 原子统一发布。

**（e）上游：`zero_fill_ranges` 是怎么算出来的。** 规划器 Phase C 中，每个目的 rank 的每个段先算对齐段长（[moonep/planning.py:L905-L913](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L905-L913)）：

```python
padded_count = 0
if token_count > 0:
    if cutlass.const_expr(tp > 1):
        padded_count = cute.round_up(token_count, tp)
    else:
        padded_count = token_count
```

空段（`token_count == 0`）`padded_count` 也为 0，不占任何槽位。随后块级前缀和给出每段基址 `base`，写入时顺手记下 padding 区间（[moonep/planning.py:L934-L952](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L934-L952)）：

```python
base = s_scan_warp_prefix[warp_id] + inclusive - total_padded
for i in cutlass.range_constexpr(IPT_EB):
    group_idx = tid * IPT_EB + i
    if group_idx < E + B:
        padded_end = base + padded_values[i]
        token_count = count_values[i]
        ...
        pad_start = 0
        pad_count = 0
        if token_count > 0:
            pad_extra = padded_values[i] - token_count
            if pad_extra > 0:
                pad_start = base + token_count
                pad_count = pad_extra
        zero_fill_start[dest_rank, group_idx] = pad_start
        zero_fill_count[dest_rank, group_idx] = pad_count
```

即公式：

\[ \text{pad\_start} = \text{base}_g + \text{tokens}_g, \qquad \text{pad\_count} = \lceil \text{tokens}_g / \text{tp} \rceil \cdot \text{tp} - \text{tokens}_g \]

空段与恰好对齐的段都输出 `(0, 0)`，内核里 `n_pad > 0` 的判断直接跳过。

#### 4.1.4 代码实践

**实践目标**：不看运行结果，纯手工复现「段布局 → zero_fill_ranges → warp 2 发射序列」这条链，验证你对公式的理解。

**操作步骤**（写一个纯 Python 脚本 `zero_warp_sim.py`，无需 GPU；以下为示例代码）：

1. 设 `token_padding = 8`，构造 5 个连续段的 `token_count = [5, 8, 0, 12, 1]`（覆盖「有 padding / 恰好对齐 / 空段 / 多倍对齐 / 单 token」五种情形）。
2. 按 planning.py L905-L952 的公式逐段计算 `padded_count`、`base`、`pad_start`、`pad_count`，拼出 `zero_fill_ranges`。
3. 模拟 warp 2：设 `num_sms = 2`、`stages = 4`、`rank = 0`、`NvS_padded = 64`，按 grid-stride 规则（CTA0 处理段 0/2/4，CTA1 处理段 1/3）输出每个 CTA 的发射序列 `(行号, 字节数)` 与权重零写槽位，并标出每次 `wait_group(stages-1)` 触发的时机（`issued` 计数）。

```python
# zero_warp_sim.py（示例代码：手工复现 planning 段布局 + warp 2 发射序列）
def build_ranges(counts, tp):
    ranges, base = [], 0
    for c in counts:
        padded = ((c + tp - 1) // tp) * tp if c > 0 else 0
        extra = padded - c if c > 0 else 0
        ranges.append((base + c, extra) if extra > 0 else (0, 0))
        base += padded
    return ranges, base

def warp2_trace(ranges, num_sms, stages, NvS_padded, rank=0, H_BYTES=14336):
    per_cta = {b: [] for b in range(num_sms)}
    for b in range(num_sms):
        issued = 0
        e = b
        while e < len(ranges):
            pad_start, n_pad = ranges[e]
            for j in range(n_pad):
                loff = pad_start + j
                row = rank * NvS_padded + loff
                per_cta[b].append(("s2g", row, H_BYTES, f"w[{loff}]=0"))
                if issued >= stages - 1:
                    per_cta[b].append(("wait_group", stages - 1))
                issued += 1
            e += num_sms
        per_cta[b].append(("wait_group", 0))
    return per_cta

ranges, total = build_ranges([5, 8, 0, 12, 1], tp=8)
print("zero_fill_ranges:", ranges, "total slots:", total)
for cta, trace in warp2_trace(ranges, num_sms=2, stages=4, NvS_padded=64).items():
    print(f"CTA{cta}:"); [print("  ", t) for t in trace]
```

**需要观察的现象**：

- `zero_fill_ranges` 应为 `[(5,3), (0,0), (0,0), (28,4), (33,7)]`，总槽位 40（= 8+8+0+16+8）；段 1 恰好对齐、段 2 为空，都输出 `(0,0)`；段 3 基址累计到 16（空段不占槽位），`pad_start = 16+12 = 28`；段 4 基址 32，`pad_start = 32+1 = 33`。
- CTA0 发射段 0 的 3 行 + 段 4 的 7 行；CTA1 只发射段 3 的 4 行——零填充负载本身就不均衡，这也是为什么要全 CTA 摊开而非单 CTA 处理。
- `stages=4` 时每个 CTA 的前 3 次发射无 `wait_group`，从第 4 次起每次发射前先等待。

**预期结果**：脚本输出与上述手工推导一致。若想进一步在真实内核上验证，需多 GPU + NVLink 环境，运行 `torchrun --nproc_per_node=<R> -m pytest tests/test_dispatch.py` 并观察断言（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么零填充 warp 在 plan 复用路径（`build_dedup_map=False`）也必须运行，而去重构建 warp 可以跳过？

**答案**：padding 行的内容是 hidden_buf 中的驻留旧数据，每一轮 dispatch 的分段布局都随路由变化，脏数据风险逐轮存在，与 plan 是否新鲜无关；而去重结构（`dup_groups` 等）是 plan 的一部分——plan 不变则重复槽的拓扑不变，已物化的结构可直接复用，且复用路径上 `src_info` 可能已被后续迭代覆盖（过期），重建反而会算错。参见 [moonep/dispatch.py:L861-L864](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L861-L864)。

**练习 2**：零填充 warp 与 warp 1 消费者可能同时写 hidden_buf，为什么不需要原子操作或互斥？

**答案**：两者的写集合按定义不相交——消费者只写非负 dst 指向的真实 token 行，zero warp 只写 `zero_fill_ranges` 标出的 padding 行，每行至多一个写者；跨 rank 的 NVL 写同样只落在真实 token 行。正确性论证见 [moonep/dispatch.py:L460-L465](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L460-L465)，可见性由出口 `cross_rank_barrier` 统一发布。

**练习 3**：`with_weights=True` 时权重槽写的是 `Int32(0)`，为什么可以直接当 fp32 零用？

**答案**：meta_buf 是 int32 视图，路由权重按 4 字节整块搬运、从不做算术（u4-l2 的 int32 位拷贝设计）；IEEE 754 的 `+0.0` 位模式是全零，与 int32 的 0 完全一致，所以清零无需类型转换。见 [moonep/dispatch.py:L456-L459](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L456-L459) 与 [L489-L492](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L489-L492)。

### 4.2 去重构建 warp（dedup builder warps）

#### 4.2.1 概念说明

发送端的负数 dst 编码（u3-l5）省掉的是**重复 payload 的 NVLink 传输**，但接收端的 NvS shard 上，重复槽最终仍必须有数据——分组 GEMM 看到的 `cu_seqlens` 分段里每个槽位都是一份数据。补齐分两步：

1. **本讲（构建）**：fresh dispatch 期间，builder warps 从 planning 发布的 `src_info` 里**发现**重复组——哪些主行有重复槽、重复槽在哪——把结果物化成 plan 拥有的三件套 `dup_groups` / `dup_loffs` / `dup_counts`。
2. **下一讲（展开）**：`dispatch_epilogue` 内核读这三件套，把主行 payload 原地扇出到所有重复槽。

builder 的输入只有一个：本 rank meta_buf `SRC_INFO` 区的 `NvS` 个 int32（布局见 [moonep/api.py:L248-L251](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L248-L251)，偏移 `SRC_INFO_OFF = BARRIER_OFF + BARRIER_SLOTS` 见 [moonep/api.py:L328-L333](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L328-L333)）。回顾发布侧（u3-l4）：planning 先把本 rank slice 清成 `-1`（[moonep/planning.py:L973-L979](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L973-L979)），各源 rank 把 `src_val = src_rank * NvS + offv` 远程写进目的 rank 的槽位（[moonep/planning.py:L1068-L1073](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1068-L1073)），一道 `cross_rank_barrier` 保证所有发布可见后才允许 dispatch 读（[moonep/planning.py:L1074-L1077](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1074-L1077)）。

解码规则（与 dst 的 rank-stride 编码互为镜像）：

\[
\text{src\_rank} = \lfloor \text{info} / NvS \rfloor,\quad
\text{offv} = \text{info} \bmod NvS,\quad
\text{token} = \lfloor \text{offv} / K \rfloor,\quad
k_{idx} = \text{offv} \bmod K
\]

「重复组」的定义由此清晰：**同一 `(src_rank, token)` 的多个 top-k 条目落到了同一个目的 rank（本 rank）**。判重维度是目的 rank，不是专家（u3-l5 已强调）。

为高效发现重复，builder 使用三个 scratch（在 Buffer 构造时分配一次，[moonep/api.py:L385-L392](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L385-L392)）：

| scratch | 形状 | 编码含义 | 写入方式 |
|---|---|---|---|
| `primary_packed` | `[R*S]` int32 | 按 `key = src_rank*S + token` 索引；值 `packed = (kidx << NvS_BITS) \| loff`，多候选并发取最小（即 kidx 最小的条目当选主槽） | `atom_min`（多写者） |
| `kmask` | `[R*S]` uint32 | 同 key 索引；bit `kidx` 置 1 表示该 token 的第 `kidx` 个 top-k 条目落到了本 rank | `atom_or`（多写者） |
| `kidx_to_loff` | `[R*S*K]` int32 | 按 `key*K + kidx` 索引；直接映射「哪个 k 落到了哪个槽位」 | 普通写（单写者） |

位宽常量的单一事实来源在 [moonep/constants.py:L7-L10](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L7-L10)：`KIDX_BITS = 7`，于是内核里（[moonep/dispatch.py:L306-L308](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L306-L308)）：

```python
NvS_BITS = cutlass.const_expr(32 - 1 - KIDX_BITS)   # = 24
NvS_MASK = cutlass.const_expr((1 << NvS_BITS) - 1)
INT32_MAX = cutlass.const_expr(0x7FFFFFFF)
```

即 packed 布局：`[31]` 保留恒 0（保证任何合法 packed < `INT32_MAX` 初值，`atom_min` 的初值永远不会赢）、`[30:24]` 是 kidx（7 位，K ≤ 127）、`[23:0]` 是 loff（NvS ≤ 2^24−1）。**kidx 放高位是有意的**：`atom_min` 取最小 packed ⇔ 先比 kidx 再比 loff ⇔ **kidx 最小的条目当选主槽**。这与发送端 canonical dst 的规则（k 序扫描、每个目的 rank 第一次出现保持非负、拷 payload，u3-l5）严格一致——consumer 写 payload 的行恰好就是 builder 选出的主行，epilogue 从主行扇出才有数据可扇。

为什么需要两个原子、一个不需要？`primary_packed` 与 `kmask` 以 `key` 为地址，而**重复恰恰意味着同一 key 有多条 src_info**，会被不同 lane 并发写，必须原子合并；`kidx_to_loff` 的地址是 `(key, kidx)` 二元组，而一个 top-k 条目在 planning 中只被分配唯一一个 `(dest_rank, loff)`，因此同一地址在本 rank slice 内至多出现一次写者，普通写即可（这正是练习 1 的答案，先卖个关子）。

#### 4.2.2 核心流程

builder 的执行分五个阶段，两两之间夹着「CTA 内命名屏障 + 跨 CTA `cross_warp_sync`」双层屏障：

```
每个 CTA 有 DEDUP_BUILDER_WARPS(=4) 个 builder warp
全局 builder 编号 gb = bidx * 4 + (warp_idx - 3)，共 num_sms*4 个
NvS 被均分为 num_sms*4 个 chunk，每个 builder 拥有一个

Phase 0  初始化
    各 builder 分块写 primary_packed[key] = INT32_MAX, kmask[key] = 0
    gb==0 的 lane<2 写 dup_counts[0..1] = 0
    屏障（发布初始化）

Pass 1  逐槽位选举（key 粒度的原子合并）
    for loff in 我的 chunk (lane 以 32 步进):
        info = src_info[rank, loff]
        if info >= 0:
            解码 (src_rank, token, kidx); key = src_rank*S + token
            atom_min(primary_packed[key], (kidx << 24) | loff)
            atom_or(kmask[key], 1 << kidx)
            kidx_to_loff[key*K + kidx] = loff
    屏障（发布选举结果）

Pass 2a  统计（只读选举结果，无集合操作）
    for loff in 我的 chunk:
        if info >= 0 且 loff == primary_loff(key) 且 popc(kmask[key]) - 1 > 0:
            lane 组计数 +1，重复计数 += popc(kmask[key]) - 1
    warp 内 inclusive scan -> lane 排他偏移
    lane 0 两个原子加：dup_counts[0] += 组总数, dup_counts[1] += 重复总数
        返回值即本 warp 在紧凑前缀中的基址（预留区间）

Pass 2b  发射（写进预留区间）
    for loff in 我的 chunk:
        if 是主槽 且 dup_count > 0:
            dup_groups[my_grp] = (loff, my_dup, dup_count)
            dup_mask = kmask & ~(1 << primary_kidx)
            while dup_mask:                       # ctz 从低到高枚举 kidx
                dup_loffs[my_dup + pos] = kidx_to_loff[key*K + ctz(dup_mask)]
                清最低位
```

几个流程级的设计决策，源码注释都给出了实测理由：

- **为什么每 CTA 4 个 builder warp**：chunk 扫描是延迟受限的（每步一条依赖加载链 `info → election`），单 warp 时每个 SM 只有一条链在跑，把 chunk 拆给多个 warp 就能摊薄墙钟时间；实测超过几个 warp 后收益趋平，4 是甜点（[moonep/constants.py:L12-L17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L12-L17)）。目标是让建表时间**藏进** NVLink 传输的阴影里。
- **为什么屏障分两层**：CTA 内 4 个 builder warp 先在命名屏障 8 上会合，然后每 CTA 只派第一个 builder warp 参加 `num_sms` 方的 `cross_warp_sync`。若 4 个 warp 全部参加，全局屏障规模是 `num_sms*4`，实测大屏障的同步开销会吃掉多 warp 带来的加速（[moonep/dispatch.py:L517-L525](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L517-L525)）。屏障用硬件命名屏障 8，因为 id 0..7 中低号已被 CUTLASS pipeline 与 `__syncthreads` 占用。
- **为什么每次全量重初始化**：调用方通常把一个 Buffer 复用很多轮迭代，且（`async_finish=True` 时）无法保证上一轮 dispatch 已彻底结束，尾清理方案（只清用过的前缀）被实测为收益中性且更脆弱，于是每轮 fresh dispatch 都全量写初值（[moonep/dispatch.py:L527-L533](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L527-L533)）。
- **为什么 Pass 2 拆成 2a/2b 两遍**：必须先知道全部组数才能给每条输出定位，两遍是「计数-重放」的标准结构；2a 里刻意不放任何 warp 集合操作，让编译器能把依赖加载跨迭代流水化（[moonep/dispatch.py:L586-L592](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L586-L592)）。
- **为什么每 warp 只做 2 次 atomicAdd**：输出定位通过「warp 聚合 + 一次原子预留区间」完成，而不是每条记录一次 atomicAdd——逐记录的原子方案会串行化，在早期实现中成为主导延迟（[moonep/dispatch.py:L612-L617](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L612-L617)）。

**顺序不稳定**是本讲的重点结论：`dup_groups`/`dup_loffs` 只保证**紧凑前缀有效**，前缀内**组与组之间**的顺序由各 warp 的 `atomicAdd` 到达顺序决定，跨运行、跨迭代都不保证稳定；但**组内** `dup_loffs` 的顺序是确定的——`ctz` 从低位到高位枚举 kidx，天然按 kidx 升序。契约原文见 `MoonEPCommPlan` 的字段注释（[moonep/planning.py:L44-L50](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L44-L50)）：`dup_counts = [n_groups, n_dup_loffs]`，仅紧凑前缀有效，顺序由 builder 的 atomicAdd 决定、不保证稳定。因此测试不能逐元素比对，必须比较组集合——这正是 `tests/planning_reference.py` 里 `ReferenceDedupPlan` 文档强调的（[tests/planning_reference.py:L10-L22](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L10-L22)，集合比较器是 `kernel_test_utils.dedup_plan_semantic_errors`）。

#### 4.2.3 源码精读

**（a）builder 的编号与 chunk 划分。** [moonep/dispatch.py:L499-L525](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L499-L525)：

```python
elif warp_idx >= self.DEDUP_BUILDER_WARP:
    if cutlass.const_expr(self.build_dedup_map):
        lane = cute.arch.lane_idx()
        n_builders = cutlass.const_expr(self.num_sms * DEDUP_BUILDER_WARPS)
        gb = (bidx * Int32(DEDUP_BUILDER_WARPS)
              + (warp_idx - self.DEDUP_BUILDER_WARP))
        is_leader = Int32(0)
        if gb == 0:
            is_leader = Int32(1)
        ...
        cta_bar = pipeline.NamedBarrier(8, 32 * DEDUP_BUILDER_WARPS)
```

`const_expr(self.build_dedup_map)` 让整个分支在复用路径上**编译期消失**。`gb` 是全局 builder 编号；`n_builders = num_sms * 4` 个 builder 平分 `NvS` 与 `R*S` 两段地址空间。

**（b）Phase 0 初始化与双层屏障。** [moonep/dispatch.py:L527-L550](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L527-L550)：

```python
seg_pk = cute.ceil_div(R * S, n_builders)
sbeg_pk = seg_pk * gb
send_pk = cutlass.min(sbeg_pk + seg_pk, R * S)
for base_pk in cutlass.range(sbeg_pk + lane, send_pk, Int32(32)):
    primary_packed_tensor[base_pk] = INT32_MAX
    kmask_tensor[base_pk] = Uint32(0)
if gb == 0:
    if lane < 2:
        dup_counts_tensor[lane] = Int32(0)

# Phase 1: init published.
cta_bar.arrive_and_wait()
if warp_idx == self.DEDUP_BUILDER_WARP:
    cross_warp_sync(builder_bar_tensor.iterator, num_sms, is_leader)
cta_bar.arrive_and_wait()
```

初始化按 `R*S` 也分 chunk；`dup_counts` 两元素由全局 0 号 builder 顺带清零。屏障模式是「CTA 内会合 → 每 CTA 第一个 builder warp（`warp_idx == DEDUP_BUILDER_WARP`）代表全 CTA 参加全局 `cross_warp_sync` → 回来再会合一次」——第二次 `arrive_and_wait` 保证 warp 3 带回的全局可见性传递给 CTA 内其余 3 个 builder warp。`cross_warp_sync` 的实现（[moonep/_common.py:L350](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L350)）是参与 warp 的 lane 0 做 release/acquire 自旋，`builder_bar` 是 TAG 自复位计数器（所以 [moonep/api.py:L385-L388](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L385-L388) 只在构造时 `torch.zeros` 一次）。

**（c）Pass 1：选举。** [moonep/dispatch.py:L552-L576](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L552-L576)：

```python
# Pass 1: elect one primary local slot per (source rank, token)
# and record the kidx -> loff mapping for duplicate expansion.
seg_src = cute.ceil_div(NvS, n_builders)
sbeg_src = gb * seg_src
send_src = cutlass.min(sbeg_src + seg_src, NvS)
for loff in cutlass.range(sbeg_src + lane, send_src, Int32(32)):
    info = meta_tensor[rank * meta_stride + SRC_INFO_OFF + loff]
    if info >= 0:
        src_rank = info // NvS
        offv = info - src_rank * NvS
        token = offv // K
        kidx = offv - token * K
        key = src_rank * S + token
        packed = (kidx << NvS_BITS) | loff
        primary_packed_ptr = (primary_packed_tensor.iterator + key).toint()
        atom_min_relaxed_gpu_s32(primary_packed_ptr.ir_value(), packed)
        kmask_ptr = (kmask_tensor.iterator + key).toint()
        atom_or_relaxed_gpu_b32(kmask_ptr.ir_value(), Uint32(1) << kidx)
        kidx_to_loff_tensor[key * K + kidx] = loff
```

chunk 按 `NvS` 划分，`lane` 以 32 为步进交错取槽位。注意解码用的是减法（`info - src_rank * NvS`）而非取模——值域受宿主边界检查约束（见 (f)），等价且省一次除法。三个写入目标恰好对应三个 scratch：前两个原子（同 key 多写者），最后一个普通写（`(key, kidx)` 唯一写者）。这里读的 `src_info` 之所以已是完整、可见的，靠的就是 planning 出口那道 `cross_rank_barrier`（[moonep/planning.py:L1074-L1077](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1074-L1077)）。

**（d）Pass 2a：统计与 warp 聚合预留。** [moonep/dispatch.py:L593-L641](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L593-L641)：

```python
lane_grp_n = Int32(0)
lane_dup_n = Int32(0)
for loff in cutlass.range(sbeg_src + lane, send_src, Int32(32)):
    info = meta_tensor[rank * meta_stride + SRC_INFO_OFF + loff]
    if info >= 0:
        ...
        packed = primary_packed_tensor[key]
        mask = Uint32(kmask_tensor[key])
        primary_loff = packed & NvS_MASK
        dup_count = popc_b32(mask) - 1
        if loff == primary_loff:
            if dup_count > Int32(0):
                lane_grp_n += Int32(1)
                lane_dup_n += dup_count

incl_grp = warp_inclusive_scan(lane_grp_n, lane)
incl_dup = warp_inclusive_scan(lane_dup_n, lane)
grp_total = cute.arch.shuffle_sync(incl_grp, Int32(31))
dup_total = cute.arch.shuffle_sync(incl_dup, Int32(31))
lane_grp_off = incl_grp - lane_grp_n
lane_dup_off = incl_dup - lane_dup_n

grp_base_w = Int32(0)
dup_base_w = Int32(0)
if lane == 0:
    grp_count_ptr = (dup_counts_tensor.iterator + 0).toint()
    grp_base_w = atom_add_relaxed_gpu_s32(grp_count_ptr.ir_value(), grp_total)
    dup_count_ptr = (dup_counts_tensor.iterator + 1).toint()
    dup_base_w = atom_add_relaxed_gpu_s32(dup_count_ptr.ir_value(), dup_total)
grp_base_w = cute.arch.shuffle_sync(grp_base_w, Int32(0))
dup_base_w = cute.arch.shuffle_sync(dup_base_w, Int32(0))
```

判主条件 `loff == primary_loff`：每个重复组中只有主槽的拥有者发射该组，其余槽静默跳过——一个组恰好一个发射者。`dup_count = popc(kmask) - 1`：kmask 的置位数是「该 token 落到本 rank 的 top-k 条目总数」，减去主槽自己就是重复数。`warp_inclusive_scan` 是从 planning 模块导入的 5 步 shuffle 前缀和（[moonep/planning.py:L162-L169](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L162-L169)），给出 lane 内偏移；lane 0 的两个 `atom_add` 返回**加之前的旧值**，即本 warp 输出在紧凑前缀中的起始下标，再经 shuffle 广播给全 warp。

**（e）Pass 2b：发射。** [moonep/dispatch.py:L643-L681](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L643-L681)：

```python
my_grp = grp_base_w + lane_grp_off
my_dup = dup_base_w + lane_dup_off
for loff in cutlass.range(sbeg_src + lane, send_src, Int32(32)):
    info = meta_tensor[rank * meta_stride + SRC_INFO_OFF + loff]
    if info >= 0:
        ...
        packed = primary_packed_tensor[key]
        mask = Uint32(kmask_tensor[key])
        primary_kidx = packed >> NvS_BITS
        primary_loff = packed & NvS_MASK
        dup_count = popc_b32(mask) - 1
        if loff == primary_loff:
            if dup_count > Int32(0):
                dup_group_key = my_grp * 3
                dup_groups_tensor[dup_group_key] = loff
                dup_groups_tensor[dup_group_key + 1] = my_dup
                dup_groups_tensor[dup_group_key + 2] = dup_count
                key_row = key * K
                dup_mask = mask & (~(Uint32(1) << primary_kidx))
                pos = Int32(0)
                while dup_mask != Uint32(0):
                    cur_dup_kidx = ctz_b32(dup_mask)
                    cur_dup_loff = kidx_to_loff_tensor[key_row + cur_dup_kidx]
                    dup_loffs_tensor[my_dup + pos] = cur_dup_loff
                    pos += Int32(1)
                    dup_mask = dup_mask & (dup_mask - Uint32(1))
                my_grp += Int32(1)
                my_dup += dup_count
```

`dup_groups` 每组三元组 `(primary_loff, dup_start, dup_n)`——主槽行号、该组重复槽在 `dup_loffs` 里的起始下标、重复个数。发射重复槽时从 kmask 里抠掉主槽的位（`mask & ~(1 << primary_kidx)`），`ctz` 循环从低位到高位枚举剩余 kidx，经 `kidx_to_loff` 反查出槽位；`dup_mask & (dup_mask - 1)` 是清最低置位的标准位技巧。分类逻辑必须与 2a 逐字一致（注释 L591-L592 特意提醒 "keep the classification in sync with Pass 2b below"），否则预留计数与实际发射数不匹配会写越界。组内顺序按 kidx 升序，与参考实现 `group_loffs[1:]` 的 k 序完全一致；组间顺序则由两个 `atomicAdd` 的到达顺序决定——不稳定的根源就在这里。

**（f）宿主侧护栏：编码上限与 scratch 校验。** fresh 路径上 `launch_dispatch` 先跑两组断言。位编码上限（[moonep/dispatch.py:L761-L784](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L761-L784)）：

```python
NvS_BITS = 32 - 1 - KIDX_BITS
assert N <= NvS, ...            # src_info 的 offv 域 [0, N) 必须装得下
assert R * NvS <= int32_max, ...  # src_info 线性编码上限
assert K <= (1 << KIDX_BITS) - 1,   # packed 的 kidx 域（<=127）
assert NvS <= (1 << NvS_BITS) - 1,  # packed 的 loff 域（<=2^24-1）
assert K <= 32, ...             # kmask 是单个 b32
```

scratch 形状/dtype/设备校验（[moonep/dispatch.py:L817-L836](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L817-L836)）核对 `primary_packed`/`kmask`/`kidx_to_loff`/`builder_bar` 四块。复用路径上这些指针全部换成 `plan.dst` 当哑占位——内核不解引用，只为满足「指针参数非空」（[moonep/dispatch.py:L941-L946](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L941-L946)）。

**（g）下游契约。** 产出的三件套被 `DispatchEpilogueKernel` 这样消费（[moonep/dispatch_epilogue.py:L53-L56](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L53-L56)）：`dup_groups` 列 `(primary_loff, dup_start, dup_n)` 头，`dup_loffs` 是紧凑的重复槽数组，`dup_counts[0]` 是有效组数——**在设备端读取**，不做宿主同步。这正是「紧凑前缀 + 设备端计数」设计的动机：dedup 结构永远是 GPU 到 GPU 的数据，全程不出卡。

#### 4.2.4 代码实践

**实践目标**：构造一个 4 token、含两个重复组的小用例，**先手工推导** `primary_packed`、`kmask`、`kidx_to_loff` 与三件套的期望内容，再写模拟脚本按内核逻辑（Pass 1 → 2a → 2b）复算，与 `tests/planning_reference.py` 的参考语义对照。

**设定**（示例数据；真实 `NvS` 由规划公式决定，本实践站在接收端视角，只需 src_info 自洽）：`R=2, S=4, K=3, NvS=8`，本 rank 为 rank 1，其 `SRC_INFO` 区 8 个槽位为：

| loff | src_info 值 | 解码 (src_rank, token, kidx) | 说明 |
|---|---|---|---|
| 0 | `0*8 + 0` = 0 | (0, 0, 0) | token0 的 k0 → 组 A 主槽候选 |
| 1 | `0*8 + 2` = 2 | (0, 0, 2) | token0 的 k2 → 组 A 重复槽 |
| 2 | `0*8 + 4` = 4 | (0, 1, 1) | token1 的 k1，无重复 |
| 3 | `1*8 + 6` = 14 | (1, 2, 0) | token2 的 k0 → 组 B 主槽候选 |
| 4 | `1*8 + 7` = 15 | (1, 2, 1) | token2 的 k1 → 组 B 重复槽 |
| 5..7 | -1 | — | 空槽（可能是 padding 行） |

**手工推导**（`NvS_BITS=24`）：

- key(0,0)=0：两个候选 packed = `(0<<24)|0 = 0` 与 `(2<<24)|1`，`atom_min` 取 0 → 主槽 loff 0；`kmask = 0b101 = 5`；`kidx_to_loff[0*3+0]=0`、`[0*3+2]=1`；`dup_count = popc(5)-1 = 1` → **组 A**。
- key(0,1)=1：`packed = (1<<24)|2`，`kmask = 0b010`，`dup_count = 0` → 无组。
- key(1,2)=1*4+2=6：两个候选 packed = `(0<<24)|3 = 3` 与 `(1<<24)|4`，取 3 → 主槽 loff 3；`kmask = 0b011 = 3`；`kidx_to_loff[6*3+0]=3`、`[6*3+1]=4`；`dup_count = 1` → **组 B**。
- 三件套（组间顺序不定）：`dup_counts = [2, 2]`；`dup_groups = {(0, s0, 1), (3, s1, 1)}` 且 `{s0, s1} = {0, 1}`；`dup_loffs` 为 `[1, 4]` 的某种组间排列（组内单元素）。

**操作步骤**：把上表存成列表，写 `simulate_builder.py`（以下为示例代码，无需 GPU）：

```python
# simulate_builder.py（示例代码：按 dispatch builder 的 pass 结构模拟）
KIDX_BITS = 7
NVS_BITS = 32 - 1 - KIDX_BITS          # 24
NVS_MASK = (1 << NVS_BITS) - 1

def simulate_builder(src_info, R, S, K, NvS):
    INT32_MAX = 2**31 - 1
    primary_packed = [INT32_MAX] * (R * S)     # Phase 0
    kmask = [0] * (R * S)
    kidx_to_loff = [None] * (R * S * K)

    def decode(info):                          # Pass 1/2 共用的解码
        src_rank, offv = divmod(info, NvS)
        token, kidx = divmod(offv, K)
        return src_rank, token, kidx

    for loff, info in enumerate(src_info):     # Pass 1：原子操作的顺序无关等价
        if info < 0:
            continue
        src_rank, token, kidx = decode(info)
        key = src_rank * S + token
        packed = (kidx << NVS_BITS) | loff
        primary_packed[key] = min(primary_packed[key], packed)  # == atom_min
        kmask[key] |= 1 << kidx                                 # == atom_or
        kidx_to_loff[key * K + kidx] = loff                     # 唯一写者

    dup_groups, dup_loffs = [], []           # Pass 2a/2b（单线程顺序版）
    for loff, info in enumerate(src_info):
        if info < 0:
            continue
        src_rank, token, _ = decode(info)
        key = src_rank * S + token
        mask = kmask[key]
        dup_count = bin(mask).count("1") - 1                    # == popc - 1
        if dup_count > 0 and loff == (primary_packed[key] & NVS_MASK):
            primary_kidx = primary_packed[key] >> NVS_BITS
            dup_groups.append((loff, len(dup_loffs), dup_count))
            dup_mask = mask & ~(1 << primary_kidx)
            while dup_mask:
                k = (dup_mask & -dup_mask).bit_length() - 1      # == ctz
                dup_loffs.append(kidx_to_loff[key * K + k])
                dup_mask &= dup_mask - 1
    return primary_packed, kmask, kidx_to_loff, dup_groups, dup_loffs

src_info = [0, 2, 4, 14, 15, -1, -1, -1]
pp, km, k2l, groups, loffs = simulate_builder(src_info, R=2, S=4, K=3, NvS=8)
print("primary_packed[0]/[1]/[6] =", pp[0], pp[1], pp[6])
print("kmask          =", [bin(m) for m in (km[0], km[1], km[6])])
print("kidx_to_loff   =", {i: v for i, v in enumerate(k2l) if v is not None})
print("dup_groups     =", groups)
print("dup_loffs      =", loffs)
```

**需要观察的现象**：

- `primary_packed[0] = 0`（kidx=0 当选）、`primary_packed[6] = 3`（loff 3、kidx 0 当选）——印证「kidx 在高位 ⇒ atom_min 选 k 序最小」，与发送端「k 序第一个保持非负 dst」一致。
- `kidx_to_loff` 恰好 5 个非空条目，对应 5 条非哨兵 src_info，无冲突地址。
- `dup_groups` 是 `{(0, 0, 1), (3, 1, 1)}`（本例顺序恰好按 loff，但**换一组 chunk 划分或 atomicAdd 到达顺序就可能翻转**——模拟脚本只能给出一种合法顺序）；`dup_counts` 语义为 `[2, 2]`。

**预期结果**：脚本输出与手工推导一致。若要与真实内核对拍，需多 GPU + NVLink 环境：`torchrun --nproc_per_node=<R> -m pytest tests/test_dispatch.py`，其中参考期望正是 `tests/planning_reference.py` Part 3（[L239-L295](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L239-L295)）按 `groups.setdefault(dest, []).append(...)` 的 k 序产生的确定性结构，比较用集合语义（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`primary_packed` 和 `kmask` 的写入都需要原子操作，为什么 `kidx_to_loff[key*K + kidx] = loff` 用普通写就安全？

**答案**：`kidx_to_loff` 的地址由 `(key, kidx)` 二元组唯一确定，而一个 top-k 条目（即一个确定的 `(src_rank, token, kidx)`）在 planning 中只被分配唯一一个 `(dest_rank, loff)`，因此它只出现在唯一一个目的 rank 的 src_info slice 里、对应槽位只被读到一次——同一地址在本 rank 内至多一个写者。`primary_packed`/`kmask` 则以 `key` 为地址，而「重复」恰恰意味着同一 key 有多条 src_info 指向本 rank 的不同槽位，会被并发合并，必须原子。见 [moonep/dispatch.py:L565-L576](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L565-L576)。

**练习 2**：`dup_groups` 组间顺序不稳定，为什么 `dup_loffs` 的组内顺序却是稳定的？测试该如何应对？

**答案**：组内发射用 `ctz` 从低位到高位枚举 `dup_mask` 的置位（即 kidx 升序，[L672-L679](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L672-L679)），单个 lane 内顺序确定；组间顺序由各 warp 对 `dup_counts` 的 `atomicAdd` 到达顺序决定（[L644-L647](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L644-L647) 注释明说 "NOT stable run-to-run"）。下游（epilogue/prologue）按 `(dup_start, dup_n)` 索引读取，对组序不敏感，所以只需测试侧用集合语义比较（`kernel_test_utils.dedup_plan_semantic_errors`，参考实现文档 [tests/planning_reference.py:L10-L22](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L10-L22)）。

**练习 3**：把 `NvS_BITS` 从 24 改小（比如 20），什么配置会先坏？宿主侧哪条断言会拦住它？

**答案**：`NvS > 2^20 - 1` 的配置先坏——packed 的 loff 域装不下槽位号，与 kidx 域重叠，`atom_min` 会选出错误的「主槽」。宿主侧 `assert NvS <= (1 << NvS_BITS) - 1`（[moonep/dispatch.py:L780-L783](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L780-L783)）在 `launch_dispatch` 入口即拦截，内核不会带着坏编码启动。这正是 constants.py 作为「位宽单一事实来源」的意义：内核与宿主检查引用同一常量。

## 5. 综合实践

把两个模块串成一张「**一次 fresh dispatch 后，本 rank NvS shard 的完整状态图**」。写一个纯 Python 脚本 `shard_map.py`（示例代码，无需 GPU）：

1. **输入**：`E+B` 个段的 `token_count` 列表（含空段与恰好对齐段）、`token_padding`、`R/S/K`，以及一份手工 src_info（可沿用 4.2.4 的表）。
2. **段布局**：按 4.1.3(e) 的公式计算每段 `(base, padded, pad_start, pad_count)` 与 `cu_seqlens`，汇总总槽位并与 `NvS = S*K + (tp-1)*2*(E/R)` 的上界公式对照（应不超过）。
3. **shard 分类**：把 `[0, NvS)` 每一行标成 `real`（有 src_info）/ `dup`（重复组槽位）/ `pad`（zero_fill_ranges 覆盖）/ `unused`，输出一张类似下面的文本图：

```
loff: 0 1 2 3 4 5 6 7
tag : R D R R D P P _    # R=real D=dup P=pad _=unused
```

4. **核对闭环**：验证 (a) 每个重复组的 real 行（主槽）与 dup 行一一配对、主槽的 kidx 最小；(b) `pad` 行集合与 `zero_fill_ranges` 展开后的行集合完全一致；(c) `real ∪ dup ∪ pad = [0, cu_seqlens[-1])` 恰好覆盖所有有效分段——即「消费者写的行 + epilogue 补的行 + 零填充写的行」拼出分组 GEMM 要读的完整分段。

第 (c) 条是本讲两个 warp 存在意义的最终闭环：dispatch 结束时，shard 上没有任何一行是「没人负责」的。若想在真实环境验证完整闭环，运行 `torchrun --nproc_per_node=<R> -m pytest tests/test_dispatch.py tests/test_e2e.py`（**待本地验证**）。

## 6. 本讲小结

- dispatch 内核 7 个 warp 三线并发：warp 0/1 数据通路（u4-l2）、warp 2 零填充（所有路径）、warp 3..6 去重构建（仅 fresh planning，`const_expr` 编译期剔除复用路径）。
- 零填充 warp 以 grid-stride 摊开 E+B 个段，从一次性清零的 `zero_smem` 出发，用受 `stages` 节流的 cp.async.bulk S2G 把 padding 行清零，并配对清零权重槽（int32 0 == fp32 0.0）；它与数据通路无竞争的根据是「每行至多一个写者」。
- 去重构建以 `src_info` 为唯一输入，用三个 scratch 完成发现：`primary_packed`（`kidx<<24 | loff`，`atom_min` 选 kidx 最小者当主槽，与发送端 canonical dst 规则严格一致）、`kmask`（b32 位图，`atom_or` 合并）、`kidx_to_loff`（`(key,kidx)` 唯一写者的直接映射）。
- 输出三件套 `dup_groups`/`dup_loffs`/`dup_counts` 只有紧凑前缀有效：组间顺序由 warp 级 `atomicAdd` 预留的到达顺序决定、不稳定；组内顺序按 kidx 升序、稳定——测试必须比较集合语义。
- builder 的性能设计环环有实测依据：每 CTA 4 warp 摊薄延迟受限的扫描、双层屏障控制全局参与者数为 num_sms、每 warp 2 次原子预留取代逐记录原子、Pass 2a 无集合操作以便编译器流水化依赖加载。
- 位宽契约（`KIDX_BITS=7`、`NvS≤2^24-1`、`K≤32` 等）以 constants.py 为单一事实来源，宿主 `_check_dedup_builder_bounds` 在启动前拦截所有越界配置。

## 7. 下一步学习建议

本讲产出的 dedup 三件套马上就有消费者：下一讲 **u4-l4（dispatch epilogue：本地重复展开）** 精读 `DispatchEpilogueKernel` 如何读 `dup_counts[0]`（设备端、免宿主同步）并以 round-robin 组批次把主行扇出到所有重复槽，建议先重读本讲 4.2.4 的用例再进入。随后 **u4-l5（combine prologue）** 会看到同一套三件套在反向的镜像消费（重复组 fp32 累加回主行），届时可以回头对照本讲的「主槽/重复槽」拓扑加深理解。如果你想巩固 warp 级原语，可以带着本讲的用例去读 [moonep/_common.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py) 中 `atom_min`/`atom_or`/`popc`/`ctz` 的 PTX 封装，并在 [tests/test_dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py) 中观察集合语义断言的实际写法。
