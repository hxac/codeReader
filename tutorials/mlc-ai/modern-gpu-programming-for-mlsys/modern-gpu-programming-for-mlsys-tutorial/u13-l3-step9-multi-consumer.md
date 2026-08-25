# Step 9：多消费者 warp specialization

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Step 9 在 Step 8 骨架上改了什么：加入第二个 MMA 消费者 warp 与第二个回写 warpgroup，cluster 输出 tile 沿 M 扩大到 512×256。
2. 推导「cluster tile → 消费者 → CTA → 行块」四级行坐标公式，并确定每个消费者的 A 行区间、D 行区间与 TMEM 列区间。
3. 重新核算多消费者下的四类屏障（`tma2mma`、`mma2tma`、`mma2ld`、`ld2mma`）的槽数、期望到达数与字节账本。
4. 解释为什么共享的是 B tile 而不是 A tile（章末练习 3），并定量算出共享带来的算术强度增益。
5. 独立完成一个行范围变体设计（每消费者每 CTA 64 行），重新核算屏障到达数、TMEM 列分配与 SMEM 占用。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先用两分钟把要用到的结论复习一遍：

- **Step 7 的三角色四屏障模型**（u13-l1）：TMA 生产者 warp、MMA 消费者 warp、回写 warpgroup 三种并发角色，靠 `tma2mma`（数据就绪）/`mma2tma`（缓冲归还）/`mma2ld`（结果就绪）/`ld2mma`（TMEM 归还）四道屏障交接；`PipelineState` 把 stage 与 phase 捆绑推进。回写组内部用 **named barrier**（`bar.sync ID, count`）同步，每个 CTA 有编号 0–15 共 16 个槽位。
- **Step 8 的双 CTA cluster**（u13-l2）：`cta_group=2` 的协作 MMA 由 CTA 0 单线程发起、硬件读两侧 SMEM 并写两侧 TMEM；`cbx = T.cta_id_in_cluster` 选本 CTA 装载的 A/B 切片；完成通知用 `cta_mask=3`（二进制 `11`）广播到两个 CTA；`remote_view(0)` 让两侧共同更新 CTA 0 上的同一道屏障账本，因此 `expect_tx` 字节数要乘 `CTA_GROUP` 合账登记。
- **TMEM 容量与 cg2 映射**（u7-l2、u7-l3）：TMEM 按 128 lane × 最多 512 列分配，书中内核一律申请 512 列再按列切片。`cta_group::2`、M=256 时累加器用 Layout A（每 CTA 存 128 行、N 沿列展开）；`cta_group::2`、M=128（dense A）时用 Layout B（每 CTA 存 64 行，N 的上下半折进 Lane 轴上下半）。
- **mbarrier 记账规则**（u8-l1）：一个相位完成的充要条件是「线程到达数」与「在途字节数」同时归零；登记过小会提前放行、静默读旧数据，过大则挂死；`wait` 只观察、不修改状态，所以多个消费者可以同时等同一道 `tma2mma[stage]`。
- **算术强度直觉**（u3-l1）：每 stage 装载的字节支撑多少 FLOP，决定了带宽侧的利用效率。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | 本讲主战场：Step 9 正文（行划分表、角色表、三处协同修改）与完整内核 `hgemm_v9`，以及九步端到端性能表和章末练习 |
| [chapter_tensor_cores/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md) | `cta_group::2` 在 M=256 与 M=128 两种配置下的 TMEM 累加器映射，是本讲变体设计的硬件依据 |

## 4. 核心概念与源码讲解

### 4.1 第二个 MMA 消费者

#### 4.1.1 概念说明

Step 8 结束时，一个 cluster（两个 CTA）里只有一条「消费链」：一个 MMA 发起 warp 发一条协作 MMA，一个回写 warpgroup 读 TMEM。生产者（TMA）每 stage 装载的数据只喂给这一条链。

Step 9 的想法很朴素：**同一个 stage 好的 B tile，其实可以被两条消费链同时消费**。于是内核加入第二个 MMA 消费者 warp 和第二个回写 warpgroup，两条链读不同的 A 块、共享同一批 B 切片，各算各的输出行范围。cluster 输出 tile 因此从 Step 8 的 256×256 沿 M 扩大到 **512×256**。

这不是又一次「换引擎」式的优化——dispatch 仍然是 `tcgen05` + `cta_group=2`，变化全部发生在 **scope**（谁来执行）与 **layout**（数据摆在哪）两个要素上：

- scope：CTA 0 里现在有**两个**消费者 warp（warp 0 与 warp 1），用 `warp_id` 选择；warpgroup 从 2 个变成 3 个（`WG_NUMBER=3`）。
- layout：`Asmem` 增加一个长度为 `NUM_CONSUMER` 的消费者轴；TMEM 512 列切成两个累加器区间；两个消费者复用同一批 staged B tile。

#### 4.1.2 核心流程

角色表（依据书中 Step 9 的 Warp Roles 一节）：

| Warpgroup | Warp | 角色 |
|-----------|------|------|
| WG 2 | warp 0 | MMA 发起 warp 0：CTA 0 选出线程发起消费者 0 的 MMA，读 `Asmem[..., 0]`，写 TMEM `[0:256]` |
| WG 2 | warp 1 | MMA 发起 warp 1：CTA 0 选出线程发起消费者 1 的 MMA，读 `Asmem[..., 1]`，写 TMEM `[256:512]` |
| WG 2 | warp 3 | TMA 生产者：每个 CTA 装载本地两个 A 块 + 一个 B 块 |
| WG 0 | 全部 warp | 每个 CTA 回写消费者 0 的本地输出行，读 TMEM `[0:256]` |
| WG 1 | 全部 warp | 每个 CTA 回写消费者 1 的本地输出行，读 TMEM `[256:512]` |

一个 K-tile 流过流水线时的交接过程（两侧 CTA 对称执行生产与回写，MMA 只在 CTA 0 发起）：

```text
WG2 warp3（生产者，两个 CTA 各一份）:
    wait mma2tma[stage]        ← 等「两个消费者都读完」该 stage（2 次到达）
    TMA 装载 Asmem[stage,0]、Asmem[stage,1]、Bsmem[stage]
    若 cbx==0: arrive.expect_tx 98304 字节 → CTA0 的 tma2mma[stage]

WG2 warp0（消费者0，仅 CTA0）      WG2 warp1（消费者1，仅 CTA0）:
    wait ld2mma[0]（TMEM 区间空闲）   wait ld2mma[1]
    for k: wait tma2mma[stage]        for k: wait tma2mma[stage]
           gemm_async → TMEM[0:256]          gemm_async → TMEM[256:512]
           commit → mma2tma[stage]           commit → mma2tma[stage]
    mma2ld[0].arrive（cta_mask=3）    mma2ld[1].arrive（cta_mask=3）

WG0（两个 CTA）: 读 TMEM[0:256]     WG1（两个 CTA）: 读 TMEM[256:512]
    wait mma2ld[0]                      wait mma2ld[1]
    4×64 列读回 + TMA store             4×64 列读回 + TMA store
    两侧共 256 线程 arrive ld2mma[0]@CTA0   两侧共 256 线程 arrive ld2mma[1]@CTA0
```

关键变化：`mma2tma` 的放行条件从「一个消费者读完」变成「**两个**消费者都读完」，`mma2ld`/`ld2mma` 则从「按 tile 复用」变成「**按消费者编号开槽**复用」。

#### 4.1.3 源码精读

**角色守卫：一个 warpgroup 装下两条消费链。** 内核常量区把 `WG_NUMBER` 提到 3，并新增 `NUM_CONSUMER=2` 与 `EPI_N=64`：

[chapter_gemm_advanced/index.md#L680-L688](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L680-L688) 定义了 `CTA_GROUP=2`、`NUM_CONSUMER=2`、`MMA_N = BLK_N * CTA_GROUP = 256`、`PIPE_DEPTH=4`、`EPI_N=64`、`WG_NUMBER=3`——消费者个数、回写分块宽度与 warpgroup 总数都由这几个常量决定。

消费者分支用 `warp_id < NUM_CONSUMER` 一网打尽两个 warp：

[chapter_gemm_advanced/index.md#L789-L797](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L789-L797) 让 warp 0 和 warp 1 走同一段消费者代码，靠 `warp_id` 区分身份：各自的 `PipelineState` 独立推进，起始 `mma_ps` phase=0（等数据）、`ld_ps` phase=1（TMEM 起始可用）；注意 `if cbx == 0` 守卫——**只有 CTA 0 发起协作 MMA**，CTA 1 的 warp 0/1 什么都不做。warp 2 两个分支条件都不满足，同样落空。

发起 MMA 时，`warp_id` 同时选择了 A 块与 TMEM 目标区间：

[chapter_gemm_advanced/index.md#L800-L810](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L800-L810) 中累加器写 `tmem[:, warp_id * MMA_N : warp_id * MMA_N + MMA_N]`（消费者 0 落在列 `[0:256]`、消费者 1 落在 `[256:512]`），操作数 A 取 `Asmem[mma_ps.stage, warp_id, :, :]`，B 取共享的 `Bsmem[mma_ps.stage, :, :]`；每条 MMA 后 `mma2tma.arrive(..., cta_mask=3)` 向两侧各记一次「我读完了」，K 循环结束后 `mma2ld.arrive(warp_id, ..., cta_mask=3)` 通知属于本消费者的回写组。

回写侧按 `wg_id` 选消费者，且 named barrier 的 ID 随之分开：

[chapter_gemm_advanced/index.md#L816-L837](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L816-L837) 中 `elif wg_id < NUM_CONSUMER` 让 WG0 服务消费者 0、WG1 服务消费者 1：`mma2ld.wait(wg_id, ...)` 只等**本**消费者的就绪信号；随后把 256 列拆成 4 个 `EPI_N=64` 的块逐块搬出——每线程一次只持有 64 个 fp32/fp16 值，`Dsmem` 也只需要 `(NUM_CONSUMER, BLK_M, EPI_N)` 的窄条，而不是整块 128×256。两道 `T.cuda.warpgroup_sync(wg_id + 10)` 使用 ID 10 与 11，书中在 Step 7 一节明确解释了原因：

[chapter_gemm_advanced/index.md#L103-L105](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L103-L105) 说明每个 CTA 有 16 个 named barrier 槽位（0–15），Step 9 有两个回写 warpgroup，因此调用 `warpgroup_sync(wg_id + 10)` 分配 ID 10 和 11，**避免两个组的到达被计到同一个账上**。

缓冲与屏障的分配也随之扩维：

[chapter_gemm_advanced/index.md#L710-L720](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L710-L720) 中 `mma2ld = TCGen05Bar(pool, NUM_CONSUMER)` 与 `ld2mma = MBarrier(pool, NUM_CONSUMER)` 的深度都变成 2（每个消费者一个槽），`Asmem` 形状变为 `(PIPE_DEPTH, NUM_CONSUMER, BLK_M, BLK_K)`（每个 stage 容纳两个 A 块），而 `Bsmem` 形状不变——**B 没有消费者轴，一份 B 两个消费者共用**。

#### 4.1.4 代码实践

**实践目标**：确认自己真的分清了「谁发起、谁执行、谁等待」，并理解一个 warp 在角色表外时的行为。

**操作步骤**：

1. 打开 [chapter_gemm_advanced/index.md#L754-L860](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L754-L860) 的 `hgemm_v9` 内核，手工填写下面这张表（每行写出守卫条件的表达式）：

   | 线程（wg_id, warp_id, cbx） | 执行的角色代码 | 发起/参与的 tile 操作 |
   |---|---|---|
   | (2, 3, 0) 与 (2, 3, 1) | ？ | ？ |
   | (2, 0, 0) | ？ | ？ |
   | (2, 0, 1) | ？ | ？ |
   | (2, 2, 0) | ？ | ？ |
   | (0, 任意, 0/1) | ？ | ？ |
   | (1, 任意, 0/1) | ？ | ？ |

2. 只跟踪 **CTA 0 的 warp 1（消费者 1）处理第一个输出 tile 的第一个 K 迭代**，按执行顺序写下它遇到的每一次 `wait` 与 `arrive`，标明屏障名、槽号和屏障在哪个 CTA 上。

**需要观察的现象**：消费者 1 的第一次 `ld2mma.wait(1, phase=1)` 为什么立即通过（提示：回写侧 `wb_ps` 起始 phase=0，`ld2mma` 初始相位为 0；消费者侧 `ld_ps` 起始 phase=1，`try_wait(1)` 在屏障处于相位 0 时立即返回——这正是 u8-l2 的「资源起始可用的一端给 1」规则）；而第一次 `tma2mma.wait(stage=0, phase=0)` 会阻塞，直到生产者登记的 98304 字节全部送达。

**预期结果**：角色表中 `(2, 0, 1)`、`(2, 1, 1)`（CTA 1 的两个消费者 warp）与 `(2, 2, *)`（warp 2）三行的「角色代码」都是「无——不满足任何分支条件，直接落到清理段」。这是源码推演实践，无需 GPU；若在 Blackwell 环境中编译运行（方法见 u9-l2），预期 `assert_close` 通过（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：WG 2 的 warp 2 在 Step 9 里做什么？

**答案**：什么都不做。角色分配只覆盖 warp 0/1（`warp_id < NUM_CONSUMER` 的消费者）与 warp 3（生产者），warp 2 两个条件都不满足，不进入任何角色分支，只在结尾参与 `cluster_sync` 与清理。

**练习 2**：两个回写 warpgroup 为什么必须用 `warpgroup_sync(wg_id + 10)`（ID 10 与 11），而不能像 Step 7 那样共用 ID 10？

**答案**：named barrier 按 ID 累计到达次数。两个回写组各自 128 线程、推进节奏不同步；若共用 ID 10，`bar.sync 10, 128` 的账本会把两组的到达混在一起——先到齐的组可能被另一组的线程「顶替计数」而提前通过，或在另一组尚未到达时挂住。分配 10、11 两个 ID 让两组的同步互不干扰。

**练习 3**：`mma2ld` 和 `ld2mma` 的槽数为什么必须从 1 变成 `NUM_CONSUMER`，而 `tma2mma`、`mma2tma` 仍按流水线 stage 开槽？

**答案**：`tma2mma`/`mma2tma` 保护的对象是 **SMEM stage**——一个 stage 的数据对两个消费者同时就绪、也被两个消费者先后读完，所以按 stage 开槽、到达数翻倍即可。`mma2ld`/`ld2mma` 保护的对象是 **每个消费者自己的 TMEM 列区间**——两个区间（`[0:256]` 与 `[256:512]`）生命周期独立，快的消费者不该被慢的拖住，所以必须按消费者编号各开一个槽。

---

### 4.2 行范围划分

#### 4.2.1 概念说明

两个消费者必须算**不同的输出行**，否则就是重复劳动。Step 9 把 cluster 输出 tile 沿 M 扩大到 512 行，行坐标因此变成四级嵌套：

\[
\text{行起点} = (m_{idx} \cdot NUM\_CONSUMER \cdot CTA\_GROUP + c \cdot CTA\_GROUP + cbx) \cdot BLK\_M
\]

即 **cluster tile（512 行）→ 消费者（256 行）→ CTA（128 行）→ 行块**：`NUM_CONSUMER * CTA_GROUP = 4` 个 128 行块在一个 cluster tile 内首尾相接，消费者编号 `c` 与簇内编号 `cbx` 各贡献一级偏移。列方向没有消费者轴：两个消费者覆盖**同样的** 256 个输出列（`n_base : n_base+256`），这正是它们能共享 B 的前提。

与之配套，TMEM 的 512 列也被切成两个累加器区间：消费者 0 用 `[0:256]`，消费者 1 用 `[256:512]`。每条协作 MMA 是 `cta_group::2`、M=256 的形态——按 [chapter_tensor_cores/index.md#L168-L176](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L168-L176) 的 Layout A，偶数 CTA 存逻辑行 0–127、奇数 CTA 存 128–255，各自用满本 CTA 的 128 lane，N 沿列展开占 256 列。

#### 4.2.2 核心流程

书中给出的完整划分表（`m_base = m_idx * 512`，`n_base = n_idx * 256`）：

| 消费者 | CTA 0 的 A/D 行 | CTA 1 的 A/D 行 | CTA 对供应的 B 行 | 本 CTA TMEM 列区间 | 回写组 |
|--------|----------------|----------------|------------------|-------------------|--------|
| 0 | `m_base : m_base+128` | `m_base+128 : m_base+256` | `n_base : n_base+256` | `[0:256]` | WG 0 |
| 1 | `m_base+256 : m_base+384` | `m_base+384 : m_base+512` | `n_base : n_base+256` | `[256:512]` | WG 1 |

配套的地址计算规则：

- **加载起点**：`m_st = (m_idx * NUM_CONSUMER * CTA_GROUP + cbx) * BLK_M` 指向消费者 0 的 A 行；消费者 1 再偏移 `CTA_GROUP * BLK_M = 256` 行。
- **调度器粒度**：cluster tile 是 512×256，所以 `num_m_tiles = M // 512`、`num_n_tiles = N // 256`。
- **回写起点**：`m_st_epi = (m_idx * 4 + wg_id * 2 + cbx) * BLK_M`（行方向按消费者再按 CTA 细分）；`n_st_epi = n_idx * MMA_N + i * EPI_N`（列方向只按 64 列 chunk 推进，与 `cbx`、`wg_id` 均无关——每个消费者都写满同样的 256 列）。

#### 4.2.3 源码精读

**调度器按 512×256 的 cluster tile 切矩阵**：

[chapter_gemm_advanced/index.md#L741-L749](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L741-L749) 中 `num_m_tiles=M // 256 // NUM_CONSUMER`（即 M//512）、`num_n_tiles=N // 256`，调度器以 cluster 为单位派发 tile；`m_st = (m_idx * NUM_CONSUMER * CTA_GROUP + cbx) * BLK_M` 与 `n_st = (n_idx * CTA_GROUP + cbx) * BLK_N` 给出本 CTA 为消费者 0 装载的 A/B 行起点。这段代码就是四级行坐标公式的直接翻译。

**消费者 1 的 A 行起点 = 消费者 0 起点 + 256**：

[chapter_gemm_advanced/index.md#L762-L776](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L762-L776) 的 `tma_load` 里，`m_st_c1 = m_st + CTA_GROUP * BLK_M` 算出消费者 1 的行起点，随后一条 `@T.inline` 函数连发三份 TMA 拷贝：`Asmem[stage, 0]`（消费者 0 的 A 块）、`Asmem[stage, 1]`（消费者 1 的 A 块）、`Bsmem[stage]`（共享 B 块），全部把完成字节报到 CTA 0 的 `tma2mma[stage]`。

**TMEM 列区间由 `warp_id` 平移**：

[chapter_gemm_advanced/index.md#L802-L806](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L802-L806) 中 `tmem[:, warp_id * MMA_N : warp_id * MMA_N + MMA_N]` 让消费者 0 写列 0–255、消费者 1 写列 256–511，两个区间合起来恰好占满 512 列 TMEM——这是「申请 512 列再按列切片」策略（u7-l3）的又一次落地。

**回写行区间由 `wg_id` 与 `cbx` 共同决定**：

[chapter_gemm_advanced/index.md#L838-L845](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L838-L845) 中 `m_st_epi = (m_idx * NUM_CONSUMER * CTA_GROUP + wg_id * CTA_GROUP + cbx) * BLK_M`，而 `n_st_epi = n_idx * MMA_N + i * EPI_N` 只依赖 chunk 编号 `i`——行方向有消费者与 CTA 两级偏移，列方向没有。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 枚举 Step 9 的行范围划分，验证「四级坐标恰好无缝铺满输出矩阵、两个消费者各写各的行、共享同样的列」。

**操作步骤**：把下面的示例代码存成 `step9_rows.py` 并运行（纯 Python，无需 GPU 与 tvm）。

```python
# 示例代码：枚举 Step 9 的行范围划分（对应 4.2.2 的公式）
CTA_GROUP, NUM_CONSUMER, BLK_M, BLK_N = 2, 2, 128, 128
MMA_N = BLK_N * CTA_GROUP            # 256
M, N = 4096, 4096

def row_range(m_idx, consumer, cbx):
    start = (m_idx * NUM_CONSUMER * CTA_GROUP + consumer * CTA_GROUP + cbx) * BLK_M
    return start, start + BLK_M

num_m_tiles = M // (NUM_CONSUMER * CTA_GROUP * BLK_M)   # M // 512
num_n_tiles = N // (CTA_GROUP * BLK_N)                  # N // 256
print("cluster tiles:", num_m_tiles * num_n_tiles)

m_idx = 3
m_base = m_idx * NUM_CONSUMER * CTA_GROUP * BLK_M
blocks = sorted(row_range(m_idx, c, x) for c in range(NUM_CONSUMER) for x in range(CTA_GROUP))
print(blocks)
assert blocks[0][0] == m_base and blocks[-1][1] == m_base + 512
assert all(a[1] == b[0] for a, b in zip(blocks, blocks[1:]))   # 无缝且不重叠
print("OK")
```

**需要观察的现象**：打印出的 4 个行块是否恰好覆盖 `[m_base, m_base+512)` 且互不重叠；两个消费者的 B 行区间是否都是同一组 `n_base : n_base+256`。

**预期结果**：`cluster tiles: 128`；`m_idx=3` 时四个行块为 `[1536,1664)、[1664,1792)、[1792,1920)、[1920,2048)`（顺序对应 `(c=0,cbx=0)、(c=0,cbx=1)、(c=1,cbx=0)、(c=1,cbx=1)`），最后打印 `OK`。以上为手工核算的确定性算术结果，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：消费者 1 在 CTA 1 中装载的 A 行区间是什么（用 `m_base` 表示）？

**答案**：`m_base+384 : m_base+512`。代入公式 \((m_{idx}\cdot 4 + 1\cdot 2 + 1)\cdot 128\)，即消费者 1 的 256 行中的后 128 行。

**练习 2**：回写公式 `m_st_epi` 里有 `wg_id * CTA_GROUP` 这一项，`n_st_epi` 里却没有对应的消费者项，为什么？

**答案**：行方向上两个消费者负责不相交的 256 行区间，必须用 `wg_id * CTA_GROUP * BLK_M` 平移；列方向上两个消费者覆盖**完全相同**的 256 列，回写只需按 `i * EPI_N` 在 64 列 chunk 间推进，与消费者编号无关。

**练习 3**：`M = N = K = 4096` 时调度器会切出多少个 cluster tile？每个 cluster（共 `148 // 2 = 74` 个）平均处理几个？

**答案**：`num_m_tiles = 4096 // 512 = 8`，`num_n_tiles = 4096 // 256 = 16`，共 128 个 cluster tile；74 个 cluster 平均每个约 1.73 个 tile。

---

### 4.3 B tile 共享

#### 4.3.1 概念说明

本讲标题里的「多消费者」之所以有收益，根源在 **B tile 共享**：一个 stage 里只装载**一份** B 切片，却同时喂给两条消费链的协作 MMA。用书中原话说：一组 staged B 切片参与两次协作 MMA，B 的装载成本相对计算量大约减半。

为什么共享的是 B 而不是 A（章末练习 3，[chapter_gemm_advanced/index.md#L905-L909](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L905-L909)）？核心逻辑只有三句：

1. **两个消费者必须算不同的行 → 必须用不同的 A 块**。若共享 A 又共享 B，两条 MMA 算出完全相同的结果，纯属重复计算，一份带宽都省不下来。
2. **两个消费者的输出覆盖同样的 256 列 → 用同一组 B 切片即可**。\(A_0 \times B\) 与 \(A_1 \times B\) 是两个不同的输出块，共享 B 不产生任何冗余。
3. **收益落在带宽侧**：B 字节只搬一次、参与两次 MMA。定量地，设一个 A 块与一个 B 块字节数均为 \(X\)、每条 MMA 的计算量为 \(C\)：不共享时两份计算要搬 \(4X\) 字节，共享 B 后只搬 \(3X\)，算术强度变为

\[
\frac{2C}{3X} \Big/ \frac{C}{2X} = \frac{4}{3}
\]

按每 CTA 每 stage 口径：Step 8 每 CTA 装 32 KB、算 \(2 \times 128 \times 256 \times 64 = 4194304\) FLOP，AI = 128 FLOP/B；Step 9 每 CTA 装 48 KB、算 \(2 \times 256 \times 256 \times 64 = 8388608\) FLOP，AI ≈ 170.7 FLOP/B，恰为 4/3 倍。

顺带一提（设计层面的呼应，属推理而非书中原文）：对称的替代方案是共享 A、各装不同 B（沿 N 拆分），字节账本同为 \(3X\)；本书选择沿 M 拆分，与 Step 6 起 `l2_group_size=8` 的调度习惯一致——那条遍历顺序本来就让「共享同一 B tile 的任务」相邻，M 拆分使 L2 级与 SMEM 级的 B 复用方向一致。真正错误的选项不是「共享 A」，而是「两个消费者共享全部操作数」。

#### 4.3.2 核心流程

共享 B 之后，所有「账本」都要重算。四道屏障在 Step 9 下的完整核算表：

| 屏障 | 类型 | 槽数 | 每相位期望到达 | 谁到达 | 谁等待 | 保护什么 |
|------|------|------|----------------|--------|--------|----------|
| `tma2mma` | TMABar | `PIPE_DEPTH=4` | 1 次线程到达 + 98304 B 在途字节 | `cbx==0` 的生产者单线程（`expect_tx`）；两侧 TMA 引擎逐笔 `complete-tx` | CTA 0 的两个消费者 warp | SMEM stage 数据就绪 |
| `mma2tma` | TCGen05Bar | 4 | `NUM_CONSUMER = 2` | 两个消费者各自的 `tcgen05.commit`（`cta_mask=3`，两侧同名屏障各收 2 次） | 两个 CTA 的生产者 | SMEM stage 可覆写 |
| `mma2ld` | TCGen05Bar | `NUM_CONSUMER=2` | 每 slot 1 次 | 消费者 \(c\) 的 K 循环收尾 commit（`cta_mask=3`） | 两侧的 WG \(c\) 回写组 | TMEM 列区间 \([256c, 256c+256)\) 可读 |
| `ld2mma` | MBarrier | 2 | 每 slot \(128 \times CTA\_GROUP = 256\) | 两侧 WG \(c\) 的全体 128 线程（经 `remote_view(0)` 汇到 CTA 0） | CTA 0 的消费者 warp \(c\) | TMEM 列区间可覆写 |

字节账本：每 CTA 每 stage 装载 \(NUM\_CONSUMER \cdot BLK\_M \cdot BLK\_K + BLK\_N \cdot BLK\_K = 24576\) 个 fp16 元素（48 KB），两个 CTA 合账：

\[
expect\_tx = CTA\_GROUP \times (NUM\_CONSUMER \cdot BLK\_M \cdot BLK\_K + BLK\_N \cdot BLK\_K) \times 2 = 98304 \text{ 字节}
\]

SMEM 账本（每 CTA）：`Asmem` = 4×2×128×64×2 B = 128 KB，`Bsmem` = 4×128×64×2 B = 64 KB（每 stage 合计 48 KB × 4），`Dsmem` = 2×128×64×2 B = 32 KB，总计约 **224 KB**——恰好在 B200 每 SM 228 KB 上限之内（另有 `move_base_to(1024)` 预留的约 1 KB 元数据）。对照 Step 7 的公式（每 stage 32 KB），Step 9 在 `PIPE_DEPTH=4` 下就达到了 Step 7 `PIPE_DEPTH=6` 才会到达的 224 KB。

性能结果：[chapter_gemm_advanced/index.md#L885-L890](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L885-L890) 给出 Step 8 → Step 9 从 0.104 ms 降到 0.094 ms（约 10%），最终与 cuBLAS 参考持平。增益不如 Step 8 那样翻倍，因为此时内核已不再单纯卡在 B 的装载带宽上——AI 提升 4/3 只在带宽受限时才能全额兑现。

#### 4.3.3 源码精读

**装载侧：2 份 A + 1 份 B**：

[chapter_gemm_advanced/index.md#L778-L787](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L778-L787) 中生产者先 `mma2tma.wait(tma_ps.stage, ...)`（该屏障需集齐 **2** 次到达才放行，见下一条），再调用 `tma_load` 连发三份 TMA 拷贝；仅当 `cbx == 0` 时由 CTA 0 的生产者线程执行 `tma2mma_cta0.arrive(tma_ps.stage, CTA_GROUP * (NUM_CONSUMER * BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE)`——把两个 CTA 合计 98304 字节一次性登记到 CTA 0 的账本上。这正是 Step 8 练习 2「为什么乘 `CTA_GROUP`」在双消费者下的推广：乘的还是 CTA 数，只是每 CTA 的字节从 2 块变成 3 块。

**到达数翻倍的初始化**：

[chapter_gemm_advanced/index.md#L722-L727](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L722-L727) 中 `mma2tma.init(NUM_CONSUMER)` 把每个 stage 的期望到达数设为 2——一个 stage 的 A/B 要被两条消费链各读一遍，生产者必须等两个消费者都 commit 后才能覆写；`mma2ld.init(1)`（每 slot 1 次）与 `ld2mma.init(128 * CTA_GROUP)`（每 slot 256 次）沿袭 Step 8 的口径，只是开了两个槽。书中正文对此有一句精准概括：

[chapter_gemm_advanced/index.md#L655-L657](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L655-L657) 说明 `tma2mma`/`mma2tma` 仍按 stage 索引（两个 MMA warp 等同一道 `tma2mma[stage]`），而 `mma2ld`/`ld2mma` 改按消费者索引——slot 0 保护消费者 0 的 TMEM 区间 `[0:256]`、slot 1 保护 `[256:512]`；MMA 侧用 `warp_id` 选槽，回写侧用 `wg_id` 选同一个槽。

**B 只有一份的操作数证据**：

[chapter_gemm_advanced/index.md#L718-L720](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L718-L720) 中 `Asmem` 带 `NUM_CONSUMER` 轴而 `Bsmem` 形状仍为 `(PIPE_DEPTH, BLK_N, BLK_K)`——共享不是靠运行时判断实现的，而是直接体现在缓冲区形状上。

**为什么共享 B 是对的**（书中原话）：

[chapter_gemm_advanced/index.md#L631](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L631) 一句讲清：消费者需要不同的 A 块是因为它们算不同的输出行，用同一组 B 切片是因为两组结果覆盖同样的输出列；于是一组 staged B 切片能参与两次协作 MMA，B 的装载成本相对计算量约减半。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的账本表变成可复算的参数化脚本，检验对「到达数、字节数、SMEM 占用」三本账的掌握。

**操作步骤**：运行下面的示例代码（纯 Python，无需 GPU），并按注释逐项与核算表对照。

```python
# 示例代码：Step 9 的字节账本与 SMEM 占用核算
CTA_GROUP, NUM_CONSUMER = 2, 2
BLK_M, BLK_N, BLK_K = 128, 128, 64
PIPE_DEPTH, EPI_N, F16 = 4, 64, 2

per_cta_stage = (NUM_CONSUMER * BLK_M * BLK_K + BLK_N * BLK_K) * F16
expect_tx = CTA_GROUP * per_cta_stage
print("per-CTA per-stage:", per_cta_stage, "bytes")   # 预期 49152 (48 KB)
print("expect_tx:", expect_tx, "bytes")               # 预期 98304 (96 KB)

asmem = PIPE_DEPTH * NUM_CONSUMER * BLK_M * BLK_K * F16
bsmem = PIPE_DEPTH * BLK_N * BLK_K * F16
dsmem = NUM_CONSUMER * BLK_M * EPI_N * F16
print(f"Asmem {asmem//1024} KB + Bsmem {bsmem//1024} KB + Dsmem {dsmem//1024} KB"
      f" = {(asmem+bsmem+dsmem)//1024} KB (B200 上限 228 KB)")

flop = 2 * (NUM_CONSUMER * CTA_GROUP * BLK_M * 128 * 2) * BLK_K   # 每 CTA 每 stage
print("AI =", flop / per_cta_stage, "FLOP/byte")       # 预期约 170.7
```

**需要观察的现象**：三本账（到达数不在脚本里，抄 4.3.2 的表即可）、`expect_tx`、SMEM 总量、算术强度各自落在哪个数量级；把 `NUM_CONSUMER` 改回 1（即退化为 Step 8 的装载口径）再跑一遍，观察 AI 是否回落到 128。

**预期结果**：`49152 / 98304 / 128+64+32 = 224 KB / AI ≈ 170.67`；`NUM_CONSUMER=1` 时 `per_cta_stage = 32768`、AI = 128.0。以上为确定性算术，待本地验证。若想在 Blackwell GPU 上实测，可按 u9-l2 的回路编译 `hgemm_v9` 并对照 fp32 参考断言，再按 [chapter_gemm_advanced/index.md#L862-L864](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L862-L864) 的协议（锁频、1000 次迭代）计时（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果忘了把 `mma2tma.init` 的参数从 1 改成 `NUM_CONSUMER`，会发生什么？

**答案**：第一个消费者的 `tcgen05.commit` 就让该 stage 的相位完成，生产者随即覆写这个 stage；第二个消费者的 MMA 可能看到被覆盖的 A/B 数据——内核不报错，静默产出错误结果。这正是 u8-l1「登记过小 → 提前放行」的到达数版本。

**练习 2**：`expect_tx` 若只登记单个 CTA 的字节数（不乘 `CTA_GROUP`），症状是什么？

**答案**：字节账提前清零，`tma2mma` 的相位提前完成，消费者在对面 CTA 的数据尚未送达时就发起协作 MMA——同样是静默错误结果；反之多登则会在 `try_wait` 上挂死。

**练习 3**：两个消费者共享 B 之后，`tma2mma[stage]` 会被两个 warp 各 `wait` 一次，为什么不会出现「一个消费者把屏障状态消费掉、另一个等不到」的问题？

**答案**：mbarrier 的 `wait` 只观察相位、不修改计数（u8-l1），因此任意多个等待者可以同时等同一道屏障；真正受「几个消费者」影响的是**到达侧**的账目——`mma2tma` 要集齐 2 次到达才放行。

## 5. 综合实践

**任务一：回答章末练习 3——为什么共享 B 而不是 A。**

参考答案要点（对照 [chapter_gemm_advanced/index.md#L631](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L631) 自查）：

1. 消费者之间的差异必须体现在输出上。Step 9 让两个消费者算不同的 256 行，因此 A 必须不同；若连 B 也相同，两条 MMA 结果完全一样，是纯冗余。
2. 两个消费者的输出覆盖同样的 256 列，所以 B 可以相同——\(A_0 B\) 与 \(A_1 B\) 是两个不同输出块，共享 B 零冗余。
3. 收益是带宽：一份 B 参与两次 MMA，AI 提升 4/3（128 → 约 170.7 FLOP/B），实测 0.104 → 0.094 ms（约 10%）。

**任务二：行范围变体设计——把每个 CTA 内两个消费者的行块从 128+128 改为 64+64（即 `BLK_M: 128 → 64`），其余参数（`BLK_N=128`、`BLK_K=64`、`CTA_GROUP=2`、`NUM_CONSUMER=2`、`PIPE_DEPTH=4`、`EPI_N=64`）不变，重新核算三本账。**

操作步骤与预期结果（源码推演，无需 GPU）：

1. **cluster tile 与行坐标**：M 维变为 \(2 \times 2 \times 64 = 256\) 行，cluster tile 为 256×256；`num_m_tiles = M // 256`（M=4096 时为 16，共 16×16=256 个 cluster tile，是原设计的两倍）。行起点公式不变，只是 `BLK_M` 换成 64。
2. **屏障到达数**：全都不变——`mma2tma.init(NUM_CONSUMER)=2`、`mma2ld.init(1)`、`ld2mma.init(128 × CTA_GROUP)=256`。到达数取决于**消费者个数与线程数**，与每个消费者算多少行无关。
3. **expect_tx**：\(2 \times (2 \times 64 \times 64 + 128 \times 64) \times 2 = 65536\) 字节（每 CTA 每 stage 32 KB）。
4. **TMEM 列分配**：每条协作 MMA 变为 `cta_group::2`、M=128（dense A）的形态，累加器改用 [chapter_tensor_cores/index.md#L178-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L178-L205) 的 **Layout B**：每 CTA 只存 64 行，N 的上下半折进 Lane 轴上下半，因此每个消费者的累加器约占 \(N/2 = 128\) 列；两个消费者合计约 256 列，512 列 TMEM 只用一半（精确的 `tcgen05.ld` 寻址与装载形状需按该节映射重推，待确认）。
5. **SMEM 占用**：每 stage \((2 \times 64 \times 64 + 128 \times 64) \times 2 = 32\) KB，4 个 stage 共 128 KB；`Dsmem` = 2×64×64×2 B = 16 KB；总计约 **144 KB**，比原设计省 80 KB——`PIPE_DEPTH` 可以提高到 6（6×32+16 = 208 KB ≤ 228 KB）。
6. **代价与结论**：算术强度掉回 128 FLOP/B——每 CTA 每 stage 装 32 KB 只算 \(2 \times 128 \times 256 \times 64 = 4194304\) FLOP，B 块占每 stage 字节的一半而非三分之一，共享 B 的 4/3 增益被 M 减半抵消；同时 Layout B 使 Lane↔行不再 1:1 对应，回写的 `tid_in_wg` 视图与 `tcgen05.ld` 形状都要重写。**省了 SMEM 却丢了复用，方向与 Step 9 的目的相悖**——这正解释了书中为何选择「每消费者 256 行」的大 M 配置。

把以上结果整理成一张与 4.3.2 同格式的对比表（原设计 vs 变体），并列出变体的三条风险（AI 下降、epilogue 映射复杂化、tile 数翻倍带来的调度开销）。

## 6. 本讲小结

- Step 9 在 Step 8 的骨架上加入第二个 MMA 消费者 warp 与第二个回写 warpgroup（`WG_NUMBER=3`），cluster 输出 tile 沿 M 扩大到 512×256；dispatch 不变，变化集中在 scope 与 layout。
- 行坐标是四级嵌套：cluster tile（512 行）→ 消费者（256 行）→ CTA（128 行）→ 行块；列方向无消费者轴，两个消费者覆盖同样的 256 列。
- 两个消费者读不同的 A 块、写 TMEM 的 `[0:256]` 与 `[256:512]`，共享同一份 `Bsmem`——共享直接体现在缓冲区形状上（`Asmem` 带消费者轴，`Bsmem` 不带）。
- 账本重算：`mma2tma` 每 stage 期望 2 次到达（两个消费者都读完才放行）；`mma2ld`/`ld2mma` 按消费者开槽，每槽 1 次硬件到达 / 256 次线程到达；`expect_tx` 为 98304 字节；SMEM 约 224 KB，逼近 B200 的 228 KB 上限。
- 共享 B 的收益是带宽侧的：AI 提升 4/3（128 → 约 170.7 FLOP/B），实测 0.104 → 0.094 ms（约 10%），九步优化的终点与 cuBLAS 持平。
- 两个回写组的 named barrier 必须分 ID（10 与 11）；回写按 `EPI_N=64` 分块，压低每线程寄存器与 `Dsmem` 峰值。

## 7. 下一步学习建议

- 下一讲 u13-l4（端到端性能解读）会把 Step 1–9 的完整性能表放在一起归因分析，并用书的基准协议规划复测——本讲只看了 8→9 这一段。
- Step 9 的多消费者结构正是 Flash Attention 4 的前身：u14-l2 会看到更多角色（两组 softmax、PV/校正组）如何按本讲的「按消费者开槽 + 共享 staged 操作数」模式组织屏障协议，建议带着 4.3.2 的核算表去读。
- 若想亲手验证本讲的账本，可回到 u9-l2 的编译验证回路跑 `hgemm_v9`，并按 u15-l4 的基准规范（先正确性、锁频、CUDA events 多迭代）复测 Step 8 与 Step 9 的 10% 差距。
- 对变体设计感兴趣的话，可继续阅读 [chapter_tensor_cores/index.md#L178-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L178-L205) 的 Layout B 映射，尝试写出 BLK_M=64 变体的 epilogue 读回视图。
