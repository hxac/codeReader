# combine prologue：本地重复累加

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 prologue 与 dispatch epilogue 的**镜像关系**：epilogue 在前向把主行「扇出」到重复槽（1 → 多），prologue 在 combine 之前把每个重复组「归约」回主行（多 → 1），两者消费同一套去重三件套。
2. 理解为什么归约必须发生在**目的 rank 本地、combine 之前**：combine 内核对负数 dst 条目完全跳过 payload 拉取，主行必须预先等于全组的 fp32 和。
3. 读懂 `CombinePrologueKernel` 的 warp 布局：warp 0 是 G2S 生产者（每 stage 搬 `B` 行），warp 1–4 是 128 线的 fp32 累加消费者。
4. 掌握**生产者预扫描 + `expect_tx` 精确声明**机制：生产者先用 lane 并行 + 蝴蝶归约算出本 CTA 的总行数，使每个 stage——包括最后一个半满 stage——都只声明真实行数，省掉填充拷贝与宿主同步。
5. 理解**stage 可跨组边界**时生产者与消费者如何靠同一个 `cnt` 行计数器对齐 stage 边界，而累加器 `acc_reg` 却按组边界清零/回写——两套节拍解耦。
6. 独立写出 prologue 的 PyTorch 参考实现，并构造跨 stage 组边界的用例验证计数器对齐逻辑。

## 2. 前置知识

本讲是 u4 单元（通信内核）的第五篇，建立在 u4-l1（PTX/流水线基础）、u4-l2（dispatch 数据通路）、u4-l4（dispatch epilogue）之上。

### 2.1 去重在前向与反向留下的「欠账」

u3-l5 讲过去重编码：同一 token 的多个 top-k 条目落到**同一个目的 rank** 时，只有最小 k 的条目真正经 NVLink 拷贝 payload（主行，primary row），其余条目的 dst 写成 `-raw_dst - 1`。u4-l4 讲过 dispatch epilogue 如何在前向把主行扇出到重复槽，让 shard 成为分组 GEMM 可直接消费的 `[NvS, H]` 布局。

现在考虑反方向。combine 要把每个 token 的 K 份派发结果**加权求和**送回源 rank。对去重过的条目，combine 内核对负数 dst 解码后会得到主行槽位——也就是说，它期望**主行已经等于「主行 + 全部重复槽」的贡献之和**。但专家 FFN 是在 epilogue 扇出**之后**才跑的：主行和重复槽各自被同一份权重变换过，值相同但没有相加。谁来做这次求和？答案就是 combine prologue：在 combine 之前、于目的 rank 本地，把每个重复组以 fp32 精度累加回主行。

模块文档写得很直白：

> The dispatch epilogue fans a primary out to its duplicates; this kernel is the reverse reduction for the combine direction.

### 2.2 与 epilogue 逐点对照

| 维度 | dispatch epilogue（u4-l4） | combine prologue（本讲） |
| --- | --- | --- |
| 语义 | 主行 → 重复槽（扇出，1 → 多） | 重复组 → 主行（归约，多 → 1） |
| 时机 | dispatch 之后、分组 GEMM 之前 | combine 输入 staging 之后、combine 之前 |
| 流水线单元 | 每 stage 装 `B` 个**组的主行** | 每 stage 装 `B` **行**（主行 + 重复槽混排） |
| 消费者 | 1 个 S2G warp（bulk_group 编组） | 4 个 fp32 ACC warp（寄存器累加） |
| `consumer_group` | 1 | 4 |
| 组归属 | 组**批次** round-robin 到 CTA | 单**组** round-robin 到 CTA（组不可拆分） |
| `_B_CANDIDATES` / `_MIN_STAGES` | (32,16,8,4,2,1) / 2 | (16,8,4,2,1) / 4 |
| 跨 rank 屏障 | 无（dispatch 出口屏障已发布） | 无（combine 入口屏障统一发布） |
| PDL 角色 | `pdl_wait_predecessor`（等 dispatch） | `pdl_trigger_dependents`（放行 combine） |

### 2.3 为什么要 fp32 累加

shard 是 bf16。若直接在 bf16 上做 K 份求和，每次加法都舍入到 bf16（约 8 位尾数），误差会随重复槽数累积。prologue 把每行读进 smem 后转 fp32，在**寄存器**里累加，组尾才一次性转回 bf16 写回主行：

\[ \text{shard}[p_g] \leftarrow \mathrm{bf16}\!\left(\mathrm{fp32}(\text{shard}[p_g]) + \sum_{d \in D_g} \mathrm{fp32}(\text{shard}[d])\right) \]

其中 \( p_g \) 是组 \( g \) 的主行槽位、\( D_g \) 是它的重复槽集合。注意重复槽**不被清零也不被改写**——combine 的负 dst 条目永远不会读它们，下一轮 dispatch 的零填充 warp 会按 `zero_fill_ranges` 处理 padding 行（重复槽不是 padding 行，但它们的内容在下一次 dispatch 中会被整体覆盖或不再被引用）。

### 2.4 回顾：`PipelineTmaAsync` 的双条件

u4-l1/u4-l4 讲过 G2S 拷贝走 mbarrier 事务计数：生产者在 stage 开张时 `mbarrier_arrive_and_expect_tx` 一次，既完成自己的 arrive 又声明本 stage 将到达的字节数；每笔 `cp.async.bulk` 完成时硬件自动向 mbarrier「记 N 字节」。**full 屏障翻相 = 生产者 arrive + 全部声明字节到达**；消费者 `consumer_wait` 等 full，`consumer_release` 向 **empty** 屏障 arrive。本讲的新点是 `consumer_group=4`：empty 屏障要收齐 4 个 ACC warp 各自 lane 0 的 arrive 才翻相——这会引出 4.1.3 中 `sync_warp` 的必要性。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [moonep/combine_prologue.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py) | **本讲主角**。`CombinePrologueKernel` 设备内核 + `launch_combine_prologue` 宿主封装，全文件约 600 行 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | 调用编排：`_run_combine_on_current_stream` 里 staging → prologue → combine 的先后关系 |
| [moonep/combine.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py) | 下游契约：负 dst 条目跳过 payload、入口 `cross_rank_barrier` 的位置 |
| [moonep/dispatch_epilogue.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py) | 镜像参照：扇出方向的设计与几何常量 |
| [moonep/_common.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py) | 本讲用到的共享助手：`cp_async_bulk_g2s`、`pdl_trigger_dependents` |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | 去重三件套的契约定义与分配 |
| [tests/test_combine.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py) | 真实调用范本，含官方的 `duplicate_topk_cross_stage`（跨 stage 组边界）回归用例 |

## 4. 核心概念与源码讲解

本讲的最小模块只有一个：`CombinePrologueKernel`。按「概念 → 流程 → 源码 → 实践」拆开。

### 4.1 CombinePrologueKernel

#### 4.1.1 概念说明

combine 方向的输入是「各源 rank 的 token 在本 rank 专家上计算后的输出」，已经被 staging 进本 rank 的 NVL shard（`zero_copy=False` 时由 `ctx['hidden_buf_local'].copy_(hidden_nvsh)` 拷入；`zero_copy=True` 时 FFN 输出直接别名 shard）。由于去重，一个 token 落到本 rank 的 K 份拷贝中只有主行是真数据，重复槽是 FFN 对扇出副本变换后的**冗余副本**。combine 内核从远端拉行时只认非负 dst（负数解码后指向主行），所以主行必须预先吸收全组贡献——这就是 prologue 的全部职责：一个**纯本地、原地**的按组 fp32 归约。

它解决三个工程问题：

1. **零宿主同步**：有效组数 `n_groups = dup_counts[0]` 在设备端读取，宿主不知道也不需要知道组数，启动几何（grid = `num_sms_dedup`）与数据无关。
2. **精确的流水线握手**：TMA 流水线每个 stage 装 `B` 行，最后一个 stage 往往不满 `B` 行。若 `expect_tx` 恒声明 `B` 行的字节数，full 屏障永远等不齐。传统做法是补 padding 拷贝凑满——浪费带宽；本内核让生产者**预扫描**自己的组头算出精确总行数，每个 stage 声明 `min(B, rows_left)` 行。
3. **原地安全性**：主行既是输入（被读进 smem）又是输出（被累加结果覆写）。内核必须证明读先于写，且没有任何其他组会碰这一行。

#### 4.1.2 核心流程

一次 `combine` 调用在宿主侧的编排（对应 `api.py` 的 `_run_combine_on_current_stream`）：

```text
(可选) launch_inter_rank_sync            # 预同步发令枪
zero_copy=False: hidden_buf_local.copy_(hidden_nvsh)   # staging 进 shard
launch_combine_prologue(ctx, plan)        # ★ 本讲：本地重复组 fp32 归约
launch_combine(...)                       # 下一讲：入口跨 rank 屏障 + K 求和回传
```

设备内核内部（grid = num_sms，block = 160 线 = 5 warp，cooperative 启动）：

```text
n_groups = dup_counts[0]                          # 设备端读取，无宿主同步
组 g 归属 CTA (g % num_sms)，各 CTA 独立处理自己的组序列

warp 0（G2S 生产者）:
  预扫描: rows_left = Σ_{本CTA的组} (dup_n + 1)    # lane 并行 + 蝴蝶归约
  把组序列展开成 flat 行流: 每组「主行在前、重复槽在后」
  for 每个 stage:
    等 empty 屏障
    lane 0: expect_tx = min(B, rows_left) * H_BYTES   # 精确声明
    lane 0: 对 stage 内 B 行逐行发 cp.async.bulk G2S（源=shard 行，目=smem 槽）
    每搬一行 rows_left -= 1, cnt += 1; cnt==B 时推进 stage

warp 1..4（fp32 ACC 消费者，共 128 线）:
  走同一个 flat 行流，用同一个 cnt 计数器               # stage 边界与生产者镜像
  for 每个组:
    acc_reg.fill(0.0)                                 # 累加器按「组」清零
    for 组内每行:
      cnt==0 时 consumer_wait 等 full 屏障
      从 smem 读该行，fp32 累加进 acc_reg
      cnt==B 时 fence + sync_warp + consumer_release   # 按「stage」释放
    组尾: acc_reg 转 bf16 写回主行（gmem 原地覆写）
```

关键的数学关系只有两个。设本 CTA 拥有组集合 \( G \)：

\[ \text{rows\_left} = \sum_{g \in G} (\text{dup\_n}_g + 1), \qquad \text{tx\_rows} = \min(B,\ \text{rows\_left}) \]

注意 `cnt` 与 `acc_reg` 是**两套节拍**：`cnt` 按 stage 推进（每 `B` 行一循环），驱动 mbarrier 握手；`acc_reg` 按组推进（组首清零、组尾回写），驱动数值语义。一个 stage 可以装下多个组、也可以劈开一个组——两者互不干扰，这正是「stage 可跨组边界」的对齐方式：**生产者与消费者遍历完全相同的 flat 行流、维护完全相同的 `cnt`，所以 stage 切分天然一致；组边界只由消费者自己的循环结构决定。**

#### 4.1.3 源码精读

**(a) 类契约与几何常量**

[combine_prologue.py:41-80](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L41-L80) 是类文档，浓缩了全部设计。挑三个要点：组 `i` round-robin 归属 block `i % num_sms`；`dup_counts[0]` 设备端读取；生产者预扫描使每个 stage 宣布精确 `expect_tx`。原地安全性的论证也在其中（后文单独展开）。

[combine_prologue.py:82-96](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L82-L96) 定义几何常量：160 线（1 生产者 warp + 128 ACC 线），`_B_CANDIDATES = (16, 8, 4, 2, 1)`、`_MIN_STAGES = 4`。与 epilogue 的 `(32,...)/2` 不同，这里的 B 是**行数**而非组数，且注释明确「优先取仍能保住 ≥4 级流水的最大批」——归约方向既要摊销握手又要更深流水来掩盖 TMA 延迟与累耗时。

**(b) 几何选择与 smem 预算**

[combine_prologue.py:98-123](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L98-L123) 的 `__init__` 依 smem 预算选 `(B, stages_acc)`，H 过大到连 B=1 都放不下两级流水时直接抛 `RuntimeError`；随后断言 `H % 128 == 0`，并从 `(8,4,2,1)` 里选能整除 `H/128` 的最大向量化宽度 `VEC`（VEC≥2 走向量化 LDS/STG，VEC==1 走标量路径——原因见 (f)）。[combine_prologue.py:125-150](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L125-L150) 的 `_smem_bytes` 统计 bf16 行缓冲（`stages × B × H × 2` 字节）加每 stage 两个 i64 mbarrier，128/16 字节对齐后加 256 字节 CUTLASS 头部余量；`_pick_geometry` 自大到小试 B、取首个能达到 `_MIN_STAGES` 的组合。

顺带一提：构造参数里有 `R`，但注释（[combine_prologue.py:88-90](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L88-L90)）说明几何已不再依赖 R——它只进编译缓存键。

**(c) 宿主入口与流水线对象**

[combine_prologue.py:154-193](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L154-L193) 是 `@cute.jit` 宿主入口：把裸指针包装成 `[NvS, H]` 布局的张量视图，再以 `grid=(num_sms,), block=(160,), cooperative=True` 启动内核（cooperative 是惯例保守选择，本内核实际没有跨 CTA 栅障）。

设备内核开头（[combine_prologue.py:219-243](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L219-L243)）分配 smem 并创建流水线：

```python
acc_load_mbar = smem.allocate_array(Int64, num_elems=2 * stages_acc)
acc_smem = smem.allocate_tensor(
    BFloat16,
    cute.make_ordered_layout((B, H, stages_acc), order=(1, 0, 2)),
    byte_alignment=128,
)
acc_load_pipe = pipeline.PipelineTmaAsync.create(
    barrier_storage=acc_load_mbar,
    num_stages=stages_acc,
    producer_group=pipeline.CooperativeGroup(pipeline.Agent.Thread, 1),
    consumer_group=pipeline.CooperativeGroup(pipeline.Agent.Thread, 4),   # ← 4 个 ACC warp
    tx_count=0,
)
n_groups = dup_counts_tensor[0]        # 设备端读取，无宿主同步
```

这段代码做了什么：按 `(B 行, H 元素, stages 级)` 的顺序布局 bf16 行缓冲；创建 TMA 异步流水线——生产者是 1 个线程（warp 0 lane 0），消费者组是 4 个线程（4 个 ACC warp 各自的 lane 0），意味着 **empty 屏障要收齐 4 个 arrive** 才翻相。最后一行从 `dup_counts[0]` 读出有效组数，整个内核从此不再需要任何宿主提供的计数。

**(d) warp 0：预扫描、expect_tx 与 G2S 主循环**

预扫描在 [combine_prologue.py:254-268](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L254-L268)：

```python
rows_left = Int32(0)
gj = Int32(bidx + lane * num_sms)
while gj < n_groups:
    rows_left += dup_groups_tensor[gj * 3 + 2] + Int32(1)   # dup_n + 1
    gj += Int32(num_sms * 32)
for off in (16, 8, 4, 2, 1):
    rows_left += cute.arch.shuffle_sync_bfly(rows_left, Int32(off))
```

这段代码做了什么：lane \( l \) 负责组 \( \text{bidx} + (32j + l)\cdot\text{num\_sms} \)，各自累加 `dup_n + 1`；五步蝴蝶 shuffle 把 32 个 lane 的部分和归约成 warp 一致的总行数。组头每条 12 字节且马上会被主循环重读，L1 缓存命中，预扫描开销约 `n_groups / (num_sms * 32)` 次迭代。

组头预取与**守卫加载**在 [combine_prologue.py:270-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L270-L288)：lane 0/1/2 各读组头一个字段，shuffle 广播后，lane \( j \)（\( 1 \le j \le \text{dup\_n} \)）预取重复槽行号 `dup_loffs[dup_start + j - 1]`——于是 **lane j 持有行 j**，后续内层循环一次 shuffle 即得任意行号。注意 `if gi < n_groups` 的守卫：超过紧凑前缀的组头是垃圾数据，其 `dup_start` 会作为 `dup_loffs` 的下标产生非法读；守卫让越界情形读出全零头。

主循环 [combine_prologue.py:290-356](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L290-L356) 的核心是内层 `k` 循环（每组 `dup_count + 1` 行）：

```python
for k in cutlass.range(dup_count + 1, unroll=1):
    row_loff = cute.arch.shuffle_sync(row_pref, k)     # lane j 持有行 j → k ≤ 31

    if cnt == Int32(0):                                 # stage 开张
        acc_load_pipe.sync_object_empty.wait(...)       # 等 empty
        if lane == 0:
            tx_rows = Int32(B)
            if rows_left < tx_rows:
                tx_rows = rows_left                      # min(B, rows_left)
            cute.arch.mbarrier_arrive_and_expect_tx(
                mbar, tx_rows * H_BYTES)                 # 精确声明字节数

    if lane == 0:                                       # cp.async.bulk 单线程指令
        g_row_int = (hidden.iterator + Int64(row_loff) * Int64(H)).toint()
        s_row_int = (acc_smem.iterator
                     + acc_load_state.index * B * H + cnt * H).toint()
        cp_async_bulk_g2s(...)

    rows_left -= Int32(1)
    cnt += Int32(1)
    if cnt == Int32(B):                                 # stage 装满 → 推进
        cnt = Int32(0)
        acc_load_state.advance()
```

这段代码做了什么：stage 的第一行（`cnt==0`）处等空、由 lane 0 声明 `min(B, rows_left) * H_BYTES` 字节——`rows_left` 此刻**仍包含当前行**，所以最后一个半满 stage 声明的正是它的真实行数，full 屏障必然凑齐、无需填充拷贝；随后 lane 0 逐行发起 G2S（全局地址用 Int64 乘加防 int32 溢出）；每搬一行 `rows_left` 与 `cnt` 同步递减，`cnt` 到 `B` 归零并推进 stage 索引。循环尾 [combine_prologue.py:350-356](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L350-L356) 用**预先读好的下一组头**（同样带 `gi_next < n_groups` 守卫）刷新 lane 持有的行号，隐藏组头加载延迟。

**(e) warp 1–4：fp32 累加与按组回写**

消费者走**同一个 flat 行流**，见 [combine_prologue.py:361-482](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L361-L482)。每线程拥有 `H / 128` 个（VEC≥2 时 `H / (128·VEC)` 个向量）fp32 寄存器。每组三步：

1. **清零**（[combine_prologue.py:401-403](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L401-L403)）：`acc_reg.fill(0.0)`——线程私有寄存器，无需跨 warp 同步。
2. **逐行累加**（[combine_prologue.py:405-438](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L405-L438)）：`cnt==0` 时 `consumer_wait` 等 full（注意等的是 **stage** 而非组）；然后从 `acc_smem[..., cnt, stage]` 读当前行累加。VEC≥2 时手工构造 smem/gmem 指针按 `VEC` 元素向量读写（[combine_prologue.py:422-438](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L422-L438)），`s_vec.load().to(Float32)` 保证 fp32 精度。
3. **stage 释放**（[combine_prologue.py:440-454](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L440-L454)）：

```python
cnt += Int32(1)
if cnt == Int32(B):
    cnt = Int32(0)
    cute.arch.fence_view_async_shared()
    cute.arch.sync_warp()
    acc_load_pipe.consumer_release(acc_use_state)
    acc_use_state.advance()
```

这段代码做了什么：`cnt` 到 `B`（stage 读完）时，先 fence 再 `sync_warp`，然后**只由 lane 0** 向 empty 屏障 arrive。注释（[combine_prologue.py:442-451](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L442-L451)）解释了为什么：`consumer_group=4` 只统计每个 ACC warp lane 0 的信号，`sync_warp` 保证 32 个 lane 的 smem 读全部完成后 lane 0 才 arrive，否则生产者可能在别的 lane 还没读完时就覆写该 stage；跨 warp 的完成则由「empty 屏障需收齐 4 个 arrive」保证。

组尾回写在 [combine_prologue.py:456-478](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L456-L478)：`acc_reg` 转 bf16、按 `primary_loff` 原地写回 gmem 的主行。VEC==1 时只能用标量 `hidden[primary_loff, idx] = BFloat16(acc_reg[c])`——[combine_prologue.py:68-69](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L68-L69) 与 [combine_prologue.py:368-371](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L368-L371) 说明了原因：CuTe DSL 的窄精度向量 store 要求至少 32 位对齐，单个 bf16（16 位）不满足，故 VEC==2 起才能走向量化 store 路径。

**(f) 原地安全性：读先于写的证明**

[combine_prologue.py:71-75](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L71-L75) 给出论证，展开成三条：

1. 每行（主行或重复槽）只属于**一个**组、在组内只出现一次——不存在跨组的读写冲突；
2. 消费者对主行的写发生在「容纳该组最后一行的 stage 的 full 屏障翻相之后」；流水线按序推进，该组更早的行（包括主行自己）所在的 stage 必已翻相，即主行的 G2S 拷贝已完成、数据安全进入过 smem；
3. 重复槽只读不写。

因此生产者后续对 smem 的覆写、对其他行 的 G2S 都不会与本写竞争。

**(g) PDL 衔接与宿主封装**

内核结尾 [combine_prologue.py:484-485](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L484-L485) 在 `pdl_trigger=True` 时调 `pdl_trigger_dependents`（[_common.py:393-400](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L393-L400)：sync_threads + 全局栅栏 + lane 0 触发 `griddepcontrol.launch_dependents`）。注意方向：prologue 自己**不** wait 前驱（它的前驱是 staging 的普通 `copy_` 内核，非 PDL 感知），只在结尾放行后继——combine 以 `use_pdl` 启动并在入口 `pdl_wait_predecessor`（[combine.py:234-235](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L234-L235)），于是 prologue 的收尾与 combine 的启动空隙被隐藏。

宿主封装 `launch_combine_prologue`（[combine_prologue.py:545-600](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L545-L600)）做四件事：

- 前置校验（[combine_prologue.py:566-579](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L566-L579)）：`H % 128 == 0`（同时覆盖 16 字节 bulk 拷贝对齐）、shard 是连续 bf16 CUDA 张量且形状 `(NvS, H)`、**`plan.K <= 32`**——因为「lane j 持有行 j」的预取依赖 shuffle 宽度 32，`dup_count ≤ K-1 ≤ 31`；
- `_check_prologue_plan`（[combine_prologue.py:497-513](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L497-L513)）：`dup_groups (NvS,3)`、`dup_loffs (NvS,)`、`dup_counts (2,)` 均为连续 int32 且在本设备；
- 经 `_get_compiled`（[combine_prologue.py:516-542](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L516-L542)，`lru_cache` 以 `(H,R,NvS,num_sms,device,pdl)` 为键）取编译产物；
- 包四个指针（shard + 三件套）后在当前流上启动。

**(h) 编排位置与下游契约**

[api.py:663-696](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L663-L696) 的 `_run_combine_on_current_stream` 固定顺序：可选 `inter_rank_sync` → staging 拷贝 → `launch_combine_prologue` → `launch_combine`。下游契约写在 [combine.py:9-13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L9-L13) 与 [combine.py:53-60](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L53-L60)：combine 只拉 `dst >= 0` 的行，负条目的贡献「已在目的 rank 被 prologue 预归约进主行」；跨 rank 发布屏障在 **combine 入口**（[combine.py:237-242](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L237-L242)）。这正是 prologue 自己不需要跨 rank 屏障的原因：它只碰本 rank 的 shard，它写的主行连同 staging 拷贝一起，由 combine 的入口屏障对全体 rank 统一发布。去重三件套的来源与形状契约见 [planning.py:44-50](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L44-L50)（分配在 [planning.py:1229-1233](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1229-L1233)）；plan 复用路径直接沿用保存的三件套快照，不重建。

#### 4.1.4 代码实践

**实践目标**：用纯 PyTorch（CPU 即可，无需 GPU 与 `moonep._C`）写出 prologue 的语义参考实现，并构造一个「组横跨 stage 边界 + 末尾半满 stage」的用例，重放生产者/消费者的 `cnt` 计数器，验证两套节拍（stage 握手 vs 组累加）解耦后的正确性。

**操作步骤**：保存为 `prologue_ref.py` 并运行 `python prologue_ref.py`：

```python
# prologue_ref.py — combine prologue 的参考实现 + 跨 stage 边界模拟（示例代码，CPU 可运行）
import torch

torch.manual_seed(0)

def prologue_reference(shard, dup_groups, dup_loffs, dup_counts):
    """语义参考：主行 ← bf16( fp32(主行) + Σ fp32(重复槽) )；重复槽不改动。"""
    out = shard.clone()
    for g in range(int(dup_counts[0])):
        primary, dup_start, dup_n = (int(v) for v in dup_groups[g])
        acc = out[primary].float()                       # fp32 累加，对齐内核
        for j in range(dup_n):
            acc = acc + out[int(dup_loffs[dup_start + j])].float()
        out[primary] = acc.to(torch.bfloat16)            # 组尾一次性回写
    return out

def build_case():
    # num_sms=2、bidx=0 的 CTA 拥有组 0/2/4（组 i → CTA i % 2），B=4：
    #   组0: 主行+5 重复槽 = 6 行  → 横跨 stage0/stage1 边界
    #   组2: 主行+1 重复槽 = 2 行
    #   组4: 仅主行       = 1 行  → 末尾半满 stage（expect_tx = 1 行）
    dup_groups = torch.tensor([(0, 0, 5), (6, 5, 1), (8, 6, 0)], dtype=torch.int32)
    dup_loffs  = torch.tensor([1, 2, 3, 4, 5, 7], dtype=torch.int32)
    dup_counts = torch.tensor([3, 6], dtype=torch.int32)
    shard = torch.randn(16, 8).to(torch.bfloat16)
    return shard, dup_groups, dup_loffs, dup_counts

def simulate_pipeline(dup_groups, dup_counts, num_sms=2, bidx=0, B=4):
    """重放 warp0/warp1-4 共同的 flat 行流与 cnt 计数器，打印每个 stage 的
    expect_tx 行数与覆盖的 (组序号, 组内行号)。"""
    n_groups = int(dup_counts[0])
    rows = []                                  # flat 流：每组主行在前、重复槽在后
    for s, g in enumerate(range(bidx, n_groups, num_sms)):
        rows += [(s, k) for k in range(int(dup_groups[g][2]) + 1)]
    rows_left, cnt, stage = len(rows), 0, 0
    print(f"CTA{bidx} 的组: {list(range(bidx, n_groups, num_sms))}，flat 流共 {rows_left} 行")
    stage_rows = [[]]
    for r in rows:
        if cnt == 0:                           # stage 开张：宣布精确 expect_tx
            print(f"  stage{stage}: expect_tx = min({B}, {rows_left}) = {min(B, rows_left)} 行")
        stage_rows[stage].append(r)
        rows_left -= 1; cnt += 1
        if cnt == B:                           # 生产/消费计数器同步推进
            cnt = 0; stage += 1; stage_rows.append([])
    return stage_rows

shard, dg, dl, dc = build_case()
out = prologue_reference(shard, dg, dl, dc)

# 自检 1：组 0 的结果与手工逐行 fp32 求和一致（该组横跨 stage 边界）
p, ds, dn = (int(v) for v in dg[0])
manual = shard[p].float()
for j in range(dn):
    manual = manual + shard[int(dl[ds + j])].float()
assert torch.equal(out[p], manual.to(torch.bfloat16))
# 自检 2：重复槽与未涉及行不被改写
assert torch.equal(out[7], shard[7]) and torch.equal(out[9:], shard[9:])
print("prologue_reference 语义自检通过")

print("各 stage 覆盖:", simulate_pipeline(dg, dc))
```

**需要观察的现象**：

1. `simulate_pipeline` 输出 `expect_tx = min(4, 9) = 4`、`min(4, 5) = 4`、`min(4, 1) = 1`——最后那个半满 stage 只声明 1 行，正是内核免填充拷贝的关键；
2. `各 stage 覆盖` 打印出 `[[(0,0..3)], [(0,4),(0,5),(1,0),(1,1)], [(2,0)]]`——组 0 被劈在 stage0 与 stage1，同时 stage1 又装了组 2 的全部，「stage 跨组、组跨 stage」双向发生；
3. 尽管如此，自检 1 通过——因为 `cnt`（stage 节拍）只管握手，`acc_reg`（组节拍）独立地在组首清零、组尾回写，组 0 在两个 stage 里的两段贡献落在同一个累加器上。

**预期结果**：三条打印全部出现、两个 assert 均不抛异常。本实践为纯 CPU 脚本，可在任何装有 PyTorch 的环境运行（本次讲义编写未实际执行，输出为依据代码逻辑的推导，**待本地验证**）。

**延伸（多卡环境）**：`tests/test_combine.py` 里的 `duplicate_topk_cross_stage` 用例（[tests/test_combine.py:45-54](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L45-L54)，S=64、K=3、epn=4）就是官方为「跨 stage 组边界」准备的 GPU 级回归；其调用范本 `_combine_full`（[tests/test_combine.py:116-139](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L116-L139)）展示了 staging → `launch_combine_prologue` → `launch_combine` 的标准顺序，可用 `torchrun --nproc_per_node=2 -m pytest tests/test_combine.py` 运行（需 NVLink 互联的双卡，**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `tx_rows` 固定为 `B`（不与 `rows_left` 取 min），会发生什么？

**答案**：最后一个半满 stage 的 `expect_tx` 声明了 `B * H_BYTES` 字节，但实际只会到达 `rows_left * H_BYTES < B * H_BYTES`——full 屏障的事务计数永远凑不齐，`consumer_wait` 死锁。传统解法是补 padding 拷贝凑满 B 行（浪费 NVLink 之外的无效搬运），本内核用生产者预扫描从根源上免除。

**练习 2**：为什么 prologue 不需要 `cross_rank_barrier`，而 combine 需要？屏障放在 combine 入口而不是 prologue 出口，有什么额外好处？

**答案**：prologue 只读写本 rank 的 `hidden_buf_local`，没有任何跨 rank 访问；它写的主行必须在 combine 读远端行之前对全体 rank 可见，这份发布职责由 combine 入口的 `cross_rank_barrier`（[combine.py:237-242](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L237-L242)）统一承担。放在 combine 入口还能把 `zero_copy=False` 的 staging 拷贝与 prologue 的累加写**一起**发布——若放在 prologue 出口，staging 拷贝就得自带一道屏障。

**练习 3**：组头预取处为什么要写 `if gi < n_groups:` 的守卫？去掉守卫最坏会发生什么？

**答案**：本 CTA 的组序列末端（或空 CTA 的首个）`gi` 会越过 `dup_counts[0]` 界定的紧凑前缀，读到的「组头」是未初始化的垃圾 int32；它的 `dup_start` 字段随即被当作 `dup_loffs` 的下标做依赖加载（[combine_prologue.py:286-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L286-L288)），垃圾下标意味着越界读。守卫让越界情形读出全零头（`primary=0, dup_start=0, dup_n=0`），`row_pref` 退化为安全值；主循环的 `while gi < n_groups` 保证这些行根本不会被消费。

## 5. 综合实践

把本讲与 u4-l4 串起来，做一个「combine 方向迷你模拟器」，验证 epilogue→FFN→prologue→combine 的完整语义闭环（纯 CPU，示例代码思路）：

1. **构造**：随机生成 `dst [S,K]`（含同 rank 重复，重复项写 `-raw_dst-1`）、`shard [NvS,H]`（bf16 随机数，视作「FFN 输出已 staging」）。可参考 [tests/test_combine.py:174-208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L174-L208) 的 `_combine_global_reference` 里构造 `global_buf` 与负 dst 解码（`raw = -dst-1 if dst<0 else dst`）的写法。
2. **prologue**：按 4.1.4 的 `prologue_reference` 把重复组归约回主行。
3. **combine 模拟**：对每个 token 的 K 个条目，解码 `raw_dst`（负数解码后指向主行），从归约后的 shard 取行做 fp32 加权求和，得到 `[S,H]` 输出。
4. **对拍**：与「不去重的朴素参考」（K 份全部直接取原始重复行求和，即 FFN 前先做 u4-l4 的扇出）对比，两者应当逐元素相等（同为 bf16 舍入前 fp32 求和的口径下；若朴素参考改成 bf16 逐步累加，则允许小误差——这正是 fp32 累加的意义，可顺手量化）。
5. **观察**：把某个重复组的重复槽数增大（如 K=8 全重复），对比两条路径的 bf16 误差分布，体会「寄存器 fp32 累加 + 组尾一次舍入」相对「bf16 链式累加」的精度收益。

若有多卡环境，可进一步把第 2–3 步替换为真实的 `launch_combine_prologue` + `launch_combine`（对照 `_combine_full` 的调用顺序），与 CPU 参考对拍（**待本地验证**）。

## 6. 本讲小结

- `CombinePrologueKernel` 是 dispatch epilogue 的**镜像**：epilogue 把主行扇出到重复槽（前向、供分组 GEMM），prologue 把每个重复组以 fp32 精度归约回主行（combine 前、供 K 求和）；两者共享同一套去重三件套契约。
- 它是**纯本地**内核：无跨 rank 通信、无自身跨 rank 屏障；主行写回的可见性由 combine 入口的 `cross_rank_barrier` 统一发布，staging 拷贝也因此搭了便车。
- **零宿主同步**：`n_groups = dup_counts[0]` 在设备端读取，组 round-robin 到固定 grid（`num_sms_dedup`），启动几何与数据量无关。
- **精确 `expect_tx`**：生产者预扫描（lane 并行 + 蝴蝶归约）得本 CTA 总行数，每个 stage 声明 `min(B, rows_left)` 行的字节数，最后半满 stage 无需填充拷贝即可让 full 屏障必然翻相。
- **两套节拍解耦**：生产者与消费者遍历同一 flat 行流、维护同一 `cnt`，stage 边界镜像对齐（可跨组边界）；累加器 `acc_reg` 却按组清零/回写，组也可横跨 stage 边界，互不干扰。
- 关键约束与安全性：`plan.K <= 32`（lane 持行 + shuffle 宽度）、`H % 128 == 0`（ACC 线数兼 bulk 拷贝对齐）、VEC==1 因 CuTe 窄精度向量 store 的 32 位对齐限制走标量路径、原地写主行的安全性由「组独占行 + 组尾写在含组末行的 stage 翻相之后」保证。

## 7. 下一步学习建议

下一讲（u4-l6）进入 `moonep/combine.py` 的 `CombineKernel`：三段式流水线（G2S 加载 → 4 个 fp32 ACC warp → S2G 写回）如何只拉取非负 dst 的行、按 token-major 归并出 `[S,H]` 输出并收集路由权重，以及入口 `cross_rank_barrier` 如何与本讲的 prologue 输出衔接。建议先带着两个问题读源码：负 dst 条目为什么权重照常解码？`NamedBarrier` 子块同步与本讲 `sync_warp` 各自解决哪一层的竞态？之后可再读 [tests/test_combine.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py) 的 identity round-trip 断言（输出 = 输入 × K），体会 dispatch→epilogue→prologue→combine 全链路的语义闭环。
