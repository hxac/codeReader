# Step 8：双 CTA cluster 协作 MMA

## 1. 本讲目标

Step 7（见 u13-l1）已经把 TMA 加载、MMA、回写拆给不同的 warp 角色，但所有协作都发生在**一个 CTA 内部**。本讲精读 GEMM 优化路线的第八步 `hgemm_v8`：把两个 CTA 组成一个 cluster，用一条 `cta_group=2` 的协作 MMA 一次产出 \(256\times256\) 的输出 tile。

学完本讲，你应该能够：

1. **画出 A、B 在两个 CTA 间的切分图**，说清哪个操作数就近读取、哪个操作数要经 DSMEM 跨 CTA 读取对端 SMEM。
2. **推导两 CTA 场景的 tile 地址计算**：给定 `m_idx`、`n_idx`、`cbx`，算出每个 CTA 加载的 A/B 切片起点与回写的输出区域。
3. **解释 `arrive.expect_tx` 的字节数为何乘 `CTA_GROUP`**（章末练习 2），并据此预测登记错误时的故障症状。
4. 区分 TIRx 中两种跨 CTA 屏障机制：`remote_view(0)` 的「单点账本」与 `cta_mask=3` 的「双副本广播」。

一句话定位：Step 8 = Step 7 的角色与流水线骨架不动，把**协作 scope 从 CTA 扩到 CTA 对**，换来 staged 操作数的复用率翻倍（书中实测 0.23 ms → 0.104 ms，约 2.2×）。

## 2. 前置知识

本讲是 advanced 阶段的深水区，直接建立在四讲旧知识之上。先用通俗语言把需要的地基过一遍。

### 2.1 cluster 与 DSMEM（回顾 u2-l2）

GPU 的执行层级里，**cluster** 是跨 SM 的一组 CTA（本讲固定为 2 个）。同一 cluster 内的 CTA 可以通过 **DSMEM（分布式共享内存）** 互相访问对方的 SMEM——数据还躺在各自的 SM 里，只是地址可以被对端引用。这解决了「两个 SM 想共用一份片上数据又不想搬两遍」的问题。

### 2.2 cta_group::2 与双 CTA 的 TMEM 映射（回顾 u7-l2）

`tcgen05.mma` 有一个 `cta_group` 限定符：

- `cta_group::1`：只用当前 CTA 自己的 SMEM/TMEM。
- `cta_group::2`：使用 cluster 内 rank 仅差最低位的一对 CTA（一偶一奇），硬件从两个 CTA 的 SMEM 读操作数，把累加结果写进**两个 CTA 各自的 TMEM**。

对 \(M=256\) 的输出 tile（Layout A），256 行沿 M **连续对半切**：CTA 0 的 TMEM 攒前 128 行，CTA 1 的 TMEM 攒后 128 行，各占 \(128\text{ lane}\times 256\text{ col}\)。发起语义不变：仍是**单个线程提交一条指令**，硬件替你完成跨 CTA 的数据搬运。

### 2.3 Step 7 的四道屏障与 cta_mask=0（回顾 u13-l1）

Step 7 用 `tma2mma` / `mma2tma` / `mma2ld` / `ld2mma` 四道屏障连接 TMA producer、MMA consumer、回写 warpgroup 三个角色。当时所有完成信号只需更新**本 CTA** 的屏障，因此 `arrive` 调用一律用 `cta_mask=0`。书中在同一节末尾埋了伏笔：

> In Step 7, each completion signal only needs to update a barrier in the current CTA, so these calls use `cta_mask=0`. Step 8 forms a two-CTA cluster and uses `cta_mask=3` (binary `11`) to update the corresponding barriers in both CTAs.

见 [chapter_gemm_advanced/index.md:71](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L71)。

### 2.4 mbarrier 的字节账本纪律（回顾 u8-l1、u12-l1）

`TMABar` 底层是 mbarrier：`arrive.expect_tx(n)` 登记「本相位预期送达 n 字节」，TMA 引擎每完成一段传输就以 complete-tx 扣减；**pending 到达数与字节计数同时归零，相位才算完成**。铁律：

- 登记过小 → 账本提前清零 → `wait` 提前通过 → **静默读旧数据**；
- 登记过大 → 账本永远清不零 → **内核挂死**在 `try_wait`。

这条纪律是理解本讲练习 2（字节数为何乘 `CTA_GROUP`）的钥匙。

### 2.5 Step 7 → Step 8 三要素对照

| 要素 | Step 7 | Step 8 | 变化点 |
|------|--------|--------|--------|
| scope | 三角色协作于**单 CTA** 内 | 协作范围扩到 **cluster 中两个 CTA** | MMA 发起仍单线程，但数据/受益跨 CTA 对 |
| layout | A、B 在本 CTA SMEM，累加器在本 CTA TMEM | A、B 切片分布在**两 CTA 的 SMEM**，累加器跨**两 CTA 的 TMEM** | 每 CTA 载入量不变，cluster 合计翻倍 |
| dispatch | `cta_group=1`、`cta_mask=0` | `Tx.gemm_async` 用 `cta_group=2`；完成通知用 `cta_mask=3` | 协作 MMA + 双 CTA 通知 |

## 3. 本讲源码地图

本讲的全部正文依据集中在 GEMM 高级章节的 Step 8 一节：

| 文件 | 位置 | 作用 |
|------|------|------|
| [chapter_gemm_advanced/index.md:325-333](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L325-L333) | Step 8 开篇 | 执行结构总览（scope/layout/dispatch 三要素变化） |
| [chapter_gemm_advanced/index.md:335-356](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L335-L356) | A/B 切分 | 256×256 tile 在两 CTA 间的划分表与复用账 |
| [chapter_gemm_advanced/index.md:358-378](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L358-L378) | 地址计算 | `cbx` 选切片、`n_st_epi` 选回写块 |
| [chapter_gemm_advanced/index.md:380-419](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L380-L419) | 数据交接 | 三处跨 CTA 交接与字节/到达数核算 |
| [chapter_gemm_advanced/index.md:426-595](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L426-L595) | `hgemm_v8` | 本讲精读的完整内核源码 |
| [chapter_gemm_advanced/index.md:862-902](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L862-L902) | 端到端性能 | 九步性能表与 Step 7→8 的归因 |
| [chapter_gemm_advanced/index.md:905-909](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L905-L909) | 章末练习 | 练习 2 是本讲综合实践的原题 |
| `_extra/demo/cta_cluster.html` | 交互演示 | 可点击的 2-CTA 协作 MMA 切分图（本地构建后被拷入站点，路径 `demo/cta_cluster.html`） |

中文读者可对照镜像文件 `zh/chapter_gemm_advanced/index.md`（中英同构，见 u1-l2）。

## 4. 核心概念与源码讲解

### 4.1 cta_group::2 协作 MMA：从「一个 CTA 内分工」到「一对 CTA 合算」

#### 4.1.1 概念说明

Step 7 结束时，单个 CTA 内已经有三角色并发的流水线，但它算的输出 tile 只有 \(128\times128\)：每 CTA 每 stage 从 GMEM 搬 1 个 A 切片（\(128\times64\)）和 1 个 B 切片（\(128\times64\)），每个切片只参与 1 次「自己的乘法」。

Step 8 的想法是**把买卖做大**：两个 CTA 把各自搬来的切片**互相借用**——每个 A 切片都和**两个** B 切片相乘，一次协作 MMA 产出 \(256\times256\) 的 tile。搬运量（cluster 合计）翻倍，输出元素变 4 倍，于是**每字节搬运支撑的 FLOP 翻倍**。这正是 u3-l2 讲过的「提高数据复用以抬高算术强度」在 cluster 层面的兑现。

关键认知：`cta_group=2` 改变的是**执行时的数据路径范围**（硬件从两 CTA 的 SMEM 读、向两 CTA 的 TMEM 写），而不是发起方式——指令仍然只由 **CTA 0 的一个选中线程**提交一次。这延续了 u2-l1 建立的「发起与执行分离」观念。

#### 4.1.2 核心流程

Step 8 一个 K 块上的协作计算可以概括为：

```text
每个 CTA（cbx = 0, 1）各自执行：
  1. TMA producer（本 CTA warp 3）：
       载 A[m_st : m_st+128, k : k+64]   → 本 CTA Asmem[stage]
       载 B[n_st : n_st+128, k : k+64]   → 本 CTA Bsmem[stage]
       完成字节都记到 CTA 0 的 tma2mma[stage]（经 remote_view）

仅 CTA 0（cbx == 0）的 MMA consumer（warp 0，elect_sync 选 1 线程）：
  2. wait tma2mma[stage]           # 等两 CTA 的 4 个切片全部到位
  3. Tx.gemm_async(..., cta_group=2)
       硬件读两 CTA 的 Asmem/Bsmem（B 需跨 CTA 取对端切片）
       累加结果写进两 CTA 各自的 TMEM（每家 128 行 × 256 列）
  4. mma2tma.arrive(..., cta_mask=3)   # 通知两 CTA：stage 可复用
  5. K 循环结束后 mma2ld.arrive(..., cta_mask=3)  # 通知两 CTA：TMEM 就绪

两 CTA 各自的回写 warpgroup：
  6. 各读本地 TMEM 的 128 行 × 256 列，分两块写回 GMEM
  7. 各 128 线程到达 CTA 0 的 ld2mma（合计 256 次）
```

#### 4.1.3 源码精读

**执行结构总览。** 章节开头的 blockquote 用三要素概括了 Step 8 的全部变化：scope 扩到两 CTA、layout 跨两 CTA 的 SMEM/TMEM、dispatch 用 `cta_group=2` 与 `cta_mask=3`：

> - Scope: the cooperating scope now spans two CTAs in a cluster, not one.
> - Layout: A and B slices reside in the SMEM of both CTAs, while the accumulator spans their two TMEM spaces.
> - Dispatch: `Tx.gemm_async` uses `cta_group=2` to issue a two-CTA cooperative MMA, and `cta_mask=3` sends completion notifications to both CTAs.

见 [chapter_gemm_advanced/index.md:330-333](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L330-L333)。这段是全讲的「目录」，后面三小节分别展开这三行。

**关键常量。** `hgemm_v8` 相对 `hgemm_v7` 新增了 `CTA_GROUP = 2` 与 `MMA_M, MMA_N = 256, 256`，并把 `PIPE_DEPTH` 从 2 提到 4；`BLK_M/BLK_N/BLK_K` 保持 128/128/64 不变——**每个 CTA 的搬运粒度没变，变大的是协作产出的 tile**：

```python
CTA_GROUP = 2
BLK_M, BLK_N, BLK_K = 128, 128, 64
MMA_M, MMA_N = 256, 256
K_TILES = K // BLK_K
PIPE_DEPTH = 4
```

见 [chapter_gemm_advanced/index.md:433-439](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L433-L439)。

**MMA 只在 CTA 0 发起一次。** MMA consumer 整条路径被 `if cbx == 0:` 包住，内部再用 `warp_id == 0` 与 `elect_sync()` 过滤到唯一线程；`Tx.gemm_async` 携带 `cta_group=CTA_GROUP`，累加器写到 `tmem[:, :MMA_N]`（本地 TMEM 的前 256 列）：

```python
if cbx == 0:
    if T.filter(lane_id, T.ptx.elect_sync()):
        ...
        for k in range(K_TILES):
            tma2mma.wait(mma_ps.stage, mma_ps.phase)
            Tx.gemm_async(
                tmem[:, :MMA_N],
                Asmem[mma_ps.stage, :, :],
                Bsmem[mma_ps.stage, :, :],
                accum=(k != 0), dispatch="tcgen05", cta_group=CTA_GROUP)
            mma2tma.arrive(mma_ps.stage, cta_group=CTA_GROUP, cta_mask=3)
            mma_ps.advance()
        mma2ld.arrive(0, cta_group=CTA_GROUP, cta_mask=3)
```

见 [chapter_gemm_advanced/index.md:536-552](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L536-L552)。注意 `accum=(k != 0)` 的 K 循环累加语义与 Step 2/7 完全一致（u11-l3、u13-l1）——变的是协作范围，不是数学。

**收益的量化。** 端到端性能表把这一步的增益记为：0.23 ms → 0.104 ms，约 2.2×，归因于「协作 MMA 提高了 staged 操作数的复用」：

> **Step 7 → Step 8**: the two-CTA cooperative MMA increases reuse of the staged operands, reducing runtime from 0.23 ms to 0.104 ms, another gain of about 2.2×.

见 [chapter_gemm_advanced/index.md:889](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L889) 与完整性能表 [chapter_gemm_advanced/index.md:866-877](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L866-L877)。

#### 4.1.4 代码实践

**实践目标**：建立「哪些调用点感知 cluster」的全局清单——Step 8 的所有改动最终都体现为若干个参数从 1/0 变成 `CTA_GROUP`/`3`。

**操作步骤**：

1. 打开 [chapter_gemm_advanced/index.md:426-595](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L426-L595)（`hgemm_v8` 全文），逐行检索三个关键词：`cta_group`、`cta_mask`、`remote_view`。
2. 把每个命中点按生命周期分类填入下表（示例格式）：

| 生命周期阶段 | 调用点 | 参数 |
|---|---|---|
| TMEM 分配 | `tcgen05.alloc` | `cta_group=CTA_GROUP` |
| 数据搬运 | `Tx.copy_async`（A、B 各一） | `cta_group=CTA_GROUP`，`mbar` 指向 CTA 0 屏障 |
| 计算 | `Tx.gemm_async` | `cta_group=CTA_GROUP` |
| 完成通知 | `mma2tma.arrive` / `mma2ld.arrive` | `cta_mask=3` |
| 回写交接 | `ld2mma_cta0.arrive(0)` | 经 `remote_view(0)` |
| 回收 | `cluster_sync` + `relinquish` + `dealloc` | `cta_group=CTA_GROUP` |

3. 对照 Step 7 内核（[chapter_gemm_advanced/index.md:124-303](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L124-L303)）把对应行的参数值抄在旁边，形成 v7→v8 差异列。

**需要观察的现象**：`cta_group=CTA_GROUP` 会出现在「资源分配、TMA 搬运、MMA、资源回收」四类调用上；`cta_mask=3` 只出现在两处 `arrive` 上；`remote_view` 只出现在两道屏障的取视图处。

**预期结果**：在 `hgemm_v8` 代码段内，`cta_group=CTA_GROUP` 共 8 处（alloc 1、copy_async 2、gemm_async 1、mma2tma.arrive 1、mma2ld.arrive 1、relinquish 1、dealloc 1），`cta_mask=3` 共 2 处，`remote_view(0)` 共 2 处。若你的统计与之一致，说明你没有漏掉任何 cluster 感知点。有 Blackwell GPU 时，可进一步按 u9-l2 回路把内核抄入 `.py` 文件编译，并在生成的 CUDA 源里检索 `cta_group` 相关 PTX 限定符；无 GPU 时本实践为纯源码阅读，结论同样成立。

#### 4.1.5 小练习与答案

**练习 1**：`Tx.gemm_async` 在 `hgemm_v8` 中实际由几个线程、几个 CTA 执行？

**答案**：1 个线程、1 个 CTA。三重过滤 `if cbx == 0`（只在 CTA 0）、`elif warp_id == 0`（WG1 的 warp 0）、`T.filter(lane_id, T.ptx.elect_sync())`（warp 内选 1 线程）逐级收敛；CTA 1 根本不进入 MMA 发起分支。硬件执行时才跨两个 CTA 读 SMEM / 写 TMEM。

**练习 2**：`PIPE_DEPTH` 从 Step 7 的 2 提到 4，每 CTA 的 SMEM 开销是多少？会超过 B200 的 228 KB 上限吗？

**答案**：每 stage 一对 A/B 切片共 \((128\times64+128\times64)\times2\) 字节 = 32 KB，回写缓冲 `Dsmem`（\(128\times128\) fp16）另占 32 KB，合计 \(4\times32+32=160\) KB < 228 KB，可以启动，但余量已不多（此口径沿用 u13-l1 的成本公式，未计屏障等元数据）。

**练习 3**：CTA 1 的回写 warpgroup 调用 `mma2ld.wait(...)`，它等的通知是谁发出的？

**答案**：CTA 0 的 MMA consumer 单线程在 K 循环结束后执行 `mma2ld.arrive(0, cta_group=CTA_GROUP, cta_mask=3)`；`cta_mask=3`（二进制 `11`）使 CTA 1 中**对应的屏障副本**也被更新，所以 CTA 1 等待的是本地副本上的这次远端到达。

### 4.2 A/B 切分、DSMEM 读取路径与 tile 地址计算

#### 4.2.1 概念说明

协作 MMA 要能跑，前提是「谁搬什么」想清楚。Step 8 的切分逻辑由两条规则决定：

- **A 决定行所有权**：CTA 0 载 A 的前 128 行，CTA 1 载后 128 行。由于 \(M=256\) 的 Layout A 是沿 M 连续对半切（u7-l2），每个 CTA 的 A 切片恰好只喂自己 TMEM 里的那 128 行输出——**A 是就近读取的**。
- **B 决定列覆盖****：** 本书约定 \(D=AB^T\)、B 按 \(N\times K\) 存储（u11-l1），所以 CTA 0/1 载入的 stored-B 行块经转置后分别成为输出的前/后 128 列。而**每个 CTA 必须为自己的 128 行算出全部 256 列**，256 列来自两个 B 切片——于是协作 MMA 执行时，Tensor Core 必须**跨 CTA 读取对端的 `Bsmem`**（经 DSMEM），这正是书中插图中央的「cross-CTA reads」。

一句话：**A 各读各的，B 两边都读对端**。这就是本讲学习目标里那张切分图的核心。

#### 4.2.2 核心流程

用 ASCII 画出一个 \(256\times256\) cluster tile（`m_base = m_idx*256`，`n_base = n_idx*256`）：

```text
                    输出 D（256 列）
              n_base .. +128 .. +256
            ┌────────────┬────────────┐
 CTA 0 拥有  │  D[0:128,   0:256]     │   ← CTA 0 的 TMEM（128×256）
 的 128 行   │                        │
 m_base      ├────────────┴────────────┤
 CTA 1 拥有  │  D[128:256, 0:256]     │   ← CTA 1 的 TMEM（128×256）
 的 128 行   │                        │
            └─────────────────────────┘

 操作数侧（每个 K 块）：
   CTA 0 SMEM:  Asmem0 = A[m_base   : m_base+128, k块]   ──┐
                Bsmem0 = B[n_base   : n_base+128, k块]   ──┼─┐
   CTA 1 SMEM:  Asmem1 = A[m_base+128: m_base+256, k块]   ─┘ │
                Bsmem1 = B[n_base+128: n_base+256, k块]   ───┘

   协作 MMA（cta_group=2）读取：
     Asmem0 → 只供 CTA 0 的 128 行        （就近）
     Asmem1 → 只供 CTA 1 的 128 行        （就近）
     Bsmem0 → 两个 CTA 都要读             （本端直读 + 对端经 DSMEM）
     Bsmem1 → 两个 CTA 都要读             （本端直读 + 对端经 DSMEM）
```

复用账（cluster 视角，一个 \(BLK_K=64\) 的 K 块）：

- 搬运字节：两 CTA 各 \((128+128)\times64\times2 = 32\) KB，合计 64 KB；
- 计算量：\(2\times256\times256\times64 = 8.4\) MFLOP；
- 算术强度 \(= 8.4\text{M}/64\text{K} = 128\) FLOP/byte，恰为 Step 7 每 CTA 口径（64 FLOP/byte）的 **2 倍**——与书中「每个 staged 操作数参与约 2 倍计算」的叙述一致（见 [chapter_gemm_advanced/index.md:356](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L356)）。

tile 地址计算只有三个公式（\( \text{cbx}\in\{0,1\} \) 为 CTA 在 cluster 内的位置）：

\[
m\_st = m\_base + \text{cbx}\times BLK\_M,\qquad
n\_st = n\_base + \text{cbx}\times BLK\_N
\]

\[
n\_st\_epi = n\_base + no\times BLK\_N,\qquad no\in\{0,1\}
\]

要点：`m_st` 既是 A 切片起点、也是该 CTA 写的第一个输出行；`n_st` 只选该 CTA **贡献**的 stored-B 行块；`n_st_epi` **不含 cbx**——因为回写覆盖的是该 CTA 的全部 256 列，由块编号 `no` 选择，与「谁搬的 B」无关。

#### 4.2.3 源码精读

**切分叙述与划分表。** 正文先讲 A：两 CTA 各载 128 行、各自拥有对应的输出行带；再讲 B：stored-B 行块经 \(B^T\) 成为前/后 128 输出列，因此协作 MMA 要按图中中央的 cross-CTA reads 访问对端 `Bsmem`，「每个 A 切片与两个 B 切片相乘」：

> Each CTA must compute all 256 columns for its own 128 output rows. The cooperative MMA therefore follows the cross-CTA reads in the center of the figure to access the other CTA's `Bsmem` as well. Each A slice is multiplied by both B slices.

见 [chapter_gemm_advanced/index.md:337-339](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L337-L339)。随后的表格给出精确划分（[chapter_gemm_advanced/index.md:349-354](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L349-L354)）：

| CTA | A slice loaded | Stored-B slice loaded | D region written |
|-----|----------------|-----------------------|------------------|
| CTA 0 | `A[m_base:m_base+128, :]` | `B[n_base:n_base+128, :]` | `D[m_base:m_base+128, n_base:n_base+256]` |
| CTA 1 | `A[m_base+128:m_base+256, :]` | `B[n_base+128:n_base+256, :]` | `D[m_base+128:m_base+256, n_base:n_base+256]` |

这一节还嵌入了交互演示（[chapter_gemm_advanced/index.md:341-347](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L341-L347)）：点击 `Asmem`、`Bsmem` 或中央 cross-CTA reads，即可高亮该部分如何参与协作 MMA——建议本地构建后打开 `_build/html/demo/cta_cluster.html` 对照本讲的 ASCII 图核对。

**`cbx` 选切片。** 正文用三个表达式概括地址计算：`cbx` 由 `T.cta_id_in_cluster([CTA_GROUP, 1])` 声明（cluster 形状 \(2\times1\)），CTA 0 从 `m_base`/`n_base` 起步，CTA 1 各推进 128 行：

```python
cbx, cby = T.cta_id_in_cluster([CTA_GROUP, 1])
m_st = m_base + cbx * BLK_M
n_st = n_base + cbx * BLK_N
```

见 [chapter_gemm_advanced/index.md:364-368](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L364-L368)，语义解释见 [chapter_gemm_advanced/index.md:370](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L370)。

**回写块不含 `cbx`。** 两个 B 切片都参与 MMA，所以每个 CTA 拿到自己 128 行的全部 256 列，回写分两块进行，块起点由 `no` 选择、没有 `cbx`——「`cbx` 选的是该 CTA 载入的 B 切片，不是它写的输出列」：

```python
n_st_epi = n_base + no * BLK_N
```

见 [chapter_gemm_advanced/index.md:372-378](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L372-L378)。

**内核中的落地。** 调度器按 \(256\times256\) 的 cluster tile 工作：`num_m_tiles=M//256`、`num_n_tiles=N//256`、cluster 数 `SM_COUNT//CTA_GROUP`，并用 `bx // CTA_GROUP` 把 CTA 序号折算成 cluster 序号；`m_st`/`n_st` 与正文公式等价（`m_idx * CTA_GROUP + cbx` 再乘 `BLK_M` 即 `m_base + cbx*128`）：

```python
tile_scheduler = ClusterPersistentScheduler2D(
    "ts", num_m_tiles=M // 256, num_n_tiles=N // 256,
    l2_group_size=8, num_clusters=SM_COUNT // CTA_GROUP)
tile_scheduler.init(bx // CTA_GROUP)
m_idx = T.meta_var(tile_scheduler.m_idx)
n_idx = T.meta_var(tile_scheduler.n_idx)
m_st = T.meta_var((m_idx * CTA_GROUP + cbx) * BLK_M)
n_st = T.meta_var((n_idx * CTA_GROUP + cbx) * BLK_N)
```

见 [chapter_gemm_advanced/index.md:490-497](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L490-L497)。回写侧 `for no in T.unroll(2)` 每次处理 128 列，`n_st_epi = n_idx * 256 + no * 128` 选块起点，随后发起 TMA store（见 [chapter_gemm_advanced/index.md:567-583](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L567-L583)）。分两块回写的动机书中也点明了：`Dsmem` 只申请 \((128,128)\)，每次只需保留 128 个 fp32，「每个线程不必同时持有 256 个 fp32」（[chapter_gemm_advanced/index.md:419](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L419)）。

#### 4.2.4 代码实践

**实践目标**：用一个可复算的小脚本验证三组地址公式，把「切分图」落到具体数字上。

**操作步骤**：

1. 把下面这段**示例代码**保存为 `tile_addr_check.py`（这是本讲义自编的验算脚本，不是仓库原有代码）：

```python
BLK_M = BLK_N = 128
CTA_GROUP = 2

def cluster_tile_addr(m_idx, n_idx, cbx, no):
    m_base = m_idx * 256          # 256 = CTA_GROUP * BLK_M
    n_base = n_idx * 256
    m_st = m_base + cbx * BLK_M   # A 切片起点 = 本 CTA 首个输出行
    n_st = n_base + cbx * BLK_N   # 本 CTA 贡献的 stored-B 行块起点
    n_st_epi = n_base + no * BLK_N  # 回写块起点（不含 cbx）
    return m_st, n_st, n_st_epi

# 例：M=N=4096 时共 16x16 个 cluster tile；取 m_idx=5, n_idx=3
for cbx in (0, 1):
    for no in (0, 1):
        print(f"cbx={cbx} no={no} ->", cluster_tile_addr(5, 3, cbx, no))
```

2. 运行 `python tile_addr_check.py`，把输出与 `hgemm_v8` 中 [chapter_gemm_advanced/index.md:496-497](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L496-L497) 与 [chapter_gemm_advanced/index.md:579](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L579) 的表达式逐项对照。

**需要观察的现象**：`m_st` 随 `cbx` 变化而 `n_st_epi` 不随 `cbx` 变化；同一 CTA 的两次回写（`no=0,1`）列区间恰好拼成 256 列。

**预期结果**：`cbx=0` 得 `m_st=1280, n_st=768`，回写块 `n_st_epi=768/896`；`cbx=1` 得 `m_st=1408, n_st=896`，回写块仍为 `768/896`。即 CTA 1 载 `B[896:1024]` 但写 `D[1408:1536, 768:1024]` 的全部 256 列。此脚本是纯 Python 验算，不依赖 GPU，结果确定；有 Blackwell GPU 时可再给 `hgemm_v8` 的 `tma_load` 临时加打印（或用 u15-l5 的 nsys 时间线）核对加载区域，属可选加深。

#### 4.2.5 小练习与答案

**练习 1**：CTA 1 载入的 A 切片参与哪些输出行的计算？它的结果落在哪个 TMEM？

**答案**：只参与 `D[m_base+128 : m_base+256, n_base : n_base+256]` 这 128 行（Layout A 沿 M 连续对半切），累加在 CTA 1 自己的 TMEM 里；它从不为 CTA 0 的行做贡献。

**练习 2**：`n_st_epi = n_base + no * BLK_N` 里为什么没有 `cbx`？

**答案**：`cbx` 出现在加载侧——它选择该 CTA 为协作 MMA **贡献**哪个 B 切片；而回写覆盖的是该 CTA 拥有的 128 行的**全部 256 列**（两个 B 切片的乘积都要写出去），列块由 `no` 选择。加载贡献与写出覆盖是两件事（[chapter_gemm_advanced/index.md:378](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L378)）。

**练习 3**：从 cluster 视角算一个 K 块（`BLK_K=64`，fp16）的算术强度，并与 Step 7 单 CTA 口径比较。

**答案**：搬运 \(2\times(128\times64+128\times64)\times2=65536\) 字节，计算 \(2\times256\times256\times64=8388608\) FLOP，算术强度 \(=8388608/65536=128\) FLOP/byte；Step 7 每 CTA 为 \(2\times128\times128\times64=2097152\) FLOP 对 32768 字节，即 64 FLOP/byte。翻倍来源于「每个 A 切片乘两个 B 切片」。

### 4.3 跨 CTA 屏障交接与字节账本

#### 4.3.1 概念说明

两个 CTA 有**各自的 SMEM 和各自的屏障对象**，但一条协作 MMA 的正确性条件横跨两家：它必须等**两边**的 A、B 切片都到位才能发起，完成后又必须让**两边**都知道「stage 可以复用了」「TMEM 有结果了」。于是 Step 8 的同步协议要解决一个新问题：**屏障放在哪家、谁来到达、谁被通知**。

TIRx 里出现两种互补的跨 CTA 机制，务必分清：

1. **`remote_view(0)`——单点账本**：屏障只有 CTA 0 的一份，CTA 1 通过远端视图直接操作它（把 TMA 完成字节记过去、把回写到达记过去）。适合「多家生产、一家消费」的交接。`tma2mma` 与 `ld2mma` 用这种。
2. **`cta_mask=3`——双副本广播**：两 CTA 各有一份同名屏障副本，`arrive` 一次性更新两份。适合「一家生产、两家消费」的交接。`mma2tma` 与 `mma2ld` 用这种（`3` = 二进制 `11`，两位各对应一个 CTA）。

为什么 `tma2mma` 选单点账本？因为它的消费者（MMA consumer）只在 CTA 0，等的就是一个「两边数据都齐」的总条件。为什么 `mma2tma` 选广播？因为它的消费者是**两个 CTA 各自的** TMA producer，各自 `wait` 本地副本即可，不必跨 SM 去看 CTA 0 的脸色。

而本讲标题里的核心问题——`arrive.expect_tx` 字节数为何乘 `CTA_GROUP`——正是「单点账本」的直接推论：**一道账本管两家的账**。

#### 4.3.2 核心流程

Step 8 的三处交接汇总成一张表（也是综合实践要交的作业）：

| 交接 | 屏障（类型） | 屏障在哪 | 谁到达 | 到达量 | 谁等待 | 跨 CTA 机制 |
|------|--------------|----------|--------|--------|--------|-------------|
| TMA → MMA | `tma2mma[stage]`（TMABar） | 仅 CTA 0 一份 | 两 CTA 的 TMA 引擎 complete-tx；CTA 0 生产者线程 `arrive.expect_tx(65536)` | 字节 \(= CTA\_GROUP\times(BLK\_M\,BLK\_K+BLK\_N\,BLK\_K)\times F16\_SIZE \) | CTA 0 的 MMA consumer | `remote_view(0)`：CTA 1 的 `mbar` 参数指向 CTA 0 |
| MMA → TMA | `mma2tma[stage]`（TCGen05Bar） | 两 CTA 各一份副本 | CTA 0 单线程经 `tcgen05.commit` | 每 stage 1 次完成 | 两 CTA 各自的 TMA producer | `cta_mask=3` 广播更新两份 |
| MMA → 回写 | `mma2ld[0]`（TCGen05Bar） | 两 CTA 各一份副本 | CTA 0 单线程（K 循环结束后） | 1 次 | 两 CTA 各自的回写 warpgroup | `cta_mask=3` |
| 回写 → 下一 tile | `ld2mma[0]`（MBarrier） | 仅 CTA 0 一份 | 两 CTA 各 128 个回写线程 | \(128\times CTA\_GROUP=256\) 次到达 | CTA 0 的 MMA consumer | `remote_view(0)` + `init(128*CTA_GROUP)` |

外加 TMEM 的生命周期：`tcgen05.alloc` / `relinquish_alloc_permit` / `dealloc` 都带 `cta_group=CTA_GROUP`（CTA 对两侧各出一 warp 执行同一分配，见 u7-l3），释放前用 `cluster_sync()` 确认两边都不再用 TMEM。

#### 4.3.3 源码精读

**交接一：TMA → MMA，一道账本管两家。** 正文明确「本实现用 CTA 0 的 `tma2mma` 追踪**两个 CTA** 发起的 TMA load，双方经同一个远端视图引用它」，并给出字节数公式：

```python
tma2mma_cta0 = tma2mma.remote_view(0)
```

> The TMA loads from both CTAs report completion to this barrier. The selected producer thread in CTA 0 registers the combined byte count for both CTAs.

```python
CTA_GROUP * (BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE
```

见 [chapter_gemm_advanced/index.md:384-396](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L384-L396)。内核中的落地有两处：CTA 1 一侧的 `tma_load` 把 `mbar` 参数写成 `tma2mma_cta0.ptr_to([tma_ps.stage])`（[chapter_gemm_advanced/index.md:511-519](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L511-L519)，`Tx.copy_async` 同时带 `cta_group=CTA_GROUP`）；登记字节的 `arrive` 只由 `cbx == 0` 一侧执行：

```python
if cbx == 0:
    tma2mma_cta0.arrive(tma_ps.stage,
        CTA_GROUP * (BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE)
```

见 [chapter_gemm_advanced/index.md:526-528](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L526-L528)。**章末练习 2 的完整答案**（原题见 [chapter_gemm_advanced/index.md:908](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L908)）：

> **为何每个 CTA 各搬各的数据，`expect_tx` 却要乘 `CTA_GROUP`？**
>
> 1. `expect_tx` 登记的不是「我搬了多少」，而是「**这道屏障本相位内所有会向它 complete-tx 的传输**共多少字节」（u8-l1 的账本纪律）。
> 2. Step 8 把两个 CTA 的 4 条 TMA load（每家 1 条 A + 1 条 B）的完成字节**全部记到同一道屏障**——CTA 0 的 `tma2mma[stage]`，CTA 1 经 `remote_view(0)` 把 `mbar` 指过去。
> 3. 必须合账的原因在消费侧：这些数据的消费者是**同一条**协作 MMA，它只在 CTA 0 发起一次，正确性前提是「两家的 4 个切片同时到位」。完成条件是两家的总字节，账本就得按总字节登记。
> 4. 所以 CTA 0 的生产者线程登记 \(2\times(128\times64+128\times64)\times2=65536\) 字节：单 CTA 32768 字节 × `CTA_GROUP`。「各搬各的」描述的是**执行方**，「乘 CTA_GROUP」描述的是**完成条件的口径**，两者并不矛盾。
> 5. 反证：若只登记 32768，两家的 complete-tx 合计仍会扣减 65536，账本在任何一半（哪一半先到不确定）到齐时就会误判清零、相位提前完成——MMA 在对端 B 切片尚未落进 SMEM 时发起，**静默产出错误结果**；反之登记过大则永远等不齐，**挂死**在 `tma2mma.wait`。

**交接二：MMA → 两端。** 协作 MMA 只发一次；`if cbx == 0` 守卫把发起权留在 CTA 0，硬件读两 CTA 的 SMEM、更新两 CTA 的 TMEM；每条异步 MMA 之后由同一线程经 `mma2tma` 登记完成，K 循环全部发出后再经 `mma2ld` 登记累加器就绪，`cta_mask=3` 让两个 CTA 都收到通知：

```python
if cbx == 0:
    Tx.gemm_async(..., cta_group=2)

for k in range(K_TILES):
    Tx.gemm_async(..., cta_group=2)
    mma2tma.arrive(mma_ps.stage, cta_group=2, cta_mask=3)

mma2ld.arrive(0, cta_group=2, cta_mask=3)
```

见 [chapter_gemm_advanced/index.md:398-415](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L398-L415)。正文对 `cta_mask=3` 的解释是：MMA 一旦完成，`mma2tma` 允许**两侧**的 TMA producer 复用其消费过的 SMEM stage；K 循环结束后，`mma2ld` 告诉**每个 CTA** 的回写 warpgroup TMEM 累加器已就绪。Step 7 一节也预告了这一变化（`cta_mask=0` → `cta_mask=3`，[chapter_gemm_advanced/index.md:71](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L71)），并指出 Step 8/9 的回写到达改用 `remote_view(0)` 让两 CTA 都更新 CTA 0 的屏障（[chapter_gemm_advanced/index.md:113](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L113)）。

**交接三：回写 → 下一 tile，到达数翻倍。** 回写 warpgroup 用完 TMEM 后，**每个 CTA 的 128 个线程**都到达 CTA 0 的 `ld2mma`，期望到达数因此是 \(128\times CTA\_GROUP=256\)；只有全部到齐，下一个输出 tile 才能复用该 TMEM 区域：

> After the writeback warpgroups have finished using TMEM, 128 threads in each CTA arrive on CTA 0's `ld2mma` barrier. Its expected arrival count is therefore `128 * CTA_GROUP`, or 256.

见 [chapter_gemm_advanced/index.md:417](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L417)。内核侧对应三行：初始化 `ld2mma.init(128 * CTA_GROUP)`（[chapter_gemm_advanced/index.md:474](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L474)）、远端视图 `ld2mma_cta0 = ld2mma.remote_view(0)`（[chapter_gemm_advanced/index.md:500-501](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L500-L501)）、回写循环末尾两 CTA 各自的 `ld2mma_cta0.arrive(0)`（[chapter_gemm_advanced/index.md:586](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L586)）——注意这里**没有** `if cbx == 0` 守卫，两个 CTA 的 128 线程都要执行。

**TMEM 生命周期收口。** TMEM 的分配与释放同样带 `cta_group`，释放前先 `cluster_sync()` 确保 two CTA 都结束访问（[chapter_gemm_advanced/index.md:419](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L419)；内核对应 [chapter_gemm_advanced/index.md:590-593](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L590-L593)）。

#### 4.3.4 代码实践

**实践目标**：用「故意改错一个参数 → 推演症状」的方式，验证你真的理解每处跨 CTA 数字的守护对象。这是 u15-l7 调试方法论的前置训练。

**操作步骤**（源码推演；有 Blackwell GPU 时可实际编译复现，症状判断标注「待本地验证」）：

1. **故障 A——少乘 `CTA_GROUP`**：把 [chapter_gemm_advanced/index.md:526-528](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L526-L528) 的 `arrive` 字节数改为 `(BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE`（32768）。
2. **故障 B——通知没广播**：把 [chapter_gemm_advanced/index.md:549](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L549) 的 `mma2tma.arrive(..., cta_mask=3)` 改回 `cta_mask=0`。
3. **故障 C——到达数没翻倍**：把 [chapter_gemm_advanced/index.md:474](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L474) 的 `ld2mma.init(128 * CTA_GROUP)` 改为 `ld2mma.init(128)`。
4. 对每个故障，先**写下**你预测的症状（死锁 / 错误结果 / 正常），再对照下面的参考判断。

**需要观察的现象 / 预期结果**（推演结论）：

- **故障 A → 静默错误结果**：账本在两家中任意一家的 32768 字节到齐时即误判相位完成，`tma2mma.wait` 提前通过，MMA 读到未填满的对端切片；内核正常结束但数值不对（不是死锁）。
- **故障 B → 死锁**：CTA 1 的 `mma2tma` 副本永远收不到到达，其 TMA producer 在后续 K 块的 `mma2tma.wait` 上无限等待。
- **故障 C → 静默错误结果（时序相关）**：256 次到达只登记了 128 的期望，相位在每边各到一半时就翻转，CTA 0 的 MMA consumer 提前认为 TMEM 空闲、在回写未读完时发起新 tile 的 MMA，覆盖旧累加器。
- 三个故障的病灶分别在「字节账本的口径」「通知的广播范围」「到达数的口径」，恰好对应本模块的三处交接。GPU 上的实际复现结果：待本地验证（本仓库为文档仓库，内核需按 u9-l2 回路自行落盘编译）。

#### 4.3.5 小练习与答案

**练习 1**：`tma2mma` 与 `mma2tma` 都跨 CTA，机制有何不同？

**答案**：`tma2mma` 是**单点账本**——屏障只有 CTA 0 的一份，CTA 1 通过 `remote_view(0)` 把 TMA 完成字节记到它上面，消费者也只在 CTA 0；`mma2tma` 是**双副本广播**——两 CTA 各有一份副本，`arrive(..., cta_mask=3)` 一次更新两份，两边的 TMA producer 各自 `wait` 本地副本。选择取决于生产者/消费者各在几家。

**练习 2**：为什么 `tma2mma_cta0.arrive(...)` 外面要套 `if cbx == 0:`，而 `ld2mma_cta0.arrive(0)` 不能套？

**答案**：`expect_tx` 只需登记一次（登记两次会把字节数翻倍成 131072，账本永远清不了零 → 挂死），所以只让 CTA 0 的生产者线程做；`ld2mma` 要收的是**两 CTA 各 128 个回写线程**的到达（合计 256 次），CTA 1 的线程必须照常执行 `arrive`，否则到达数永远差 128，MMA consumer 挂死在 `ld2mma.wait`。

**练习 3**：`Tx.copy_async` 上的 `cta_group=CTA_GROUP` 参数（[chapter_gemm_advanced/index.md:514](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L514)）与 `Tx.gemm_async` 上的同名参数，守护的是同一件事吗？

**答案**：不是。`gemm_async` 上的 `cta_group=2` 决定 MMA 的协作 scope（硬件跨两 CTA 读 SMEM、写 TMEM）；`copy_async` 上的 `cta_group` 属于 TMA 搬运的分组限定，与 mbarrier 完成通知的分组方式相关，书中正文未展开其精确语义（待确认）。二者都标着「cluster 感知」，但作用对象不同——这正是 4.1.4 实践里按生命周期分类的意义。

## 5. 综合实践

**任务**：完整解答章末练习 2（[chapter_gemm_advanced/index.md:908](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L908)），并交付两份「以后读 Step 9 / FA4 都能复用」的图表。

**第一份交付：A、B 切分与 DSMEM 路径图。** 按 4.2.2 的模板画出一个 \(256\times256\) cluster tile（可手绘、可用 matplotlib，也可直接玩交互演示 `demo/cta_cluster.html`），图上必须标出：

1. 两个 CTA 各自的 `Asmem`/`Bsmem` 切片及其 GMEM 源区间（用 4.2.4 脚本的数字：`m_idx=5, n_idx=3` 时 CTA 0 载 `A[1280:1408]`、`B[768:896]`，CTA 1 载 `A[1408:1536]`、`B[896:1024]`）；
2. **A 就近读取**（各 CTA 的 A 切片只喂本 CTA TMEM 的 128 行），**B 跨 CTA 读取**（两 CTA 的输出都需要两个 B 切片，Tensor Core 经 DSMEM 取对端 `Bsmem`）；
3. 每个 CTA 的 TMEM 累加器是 \(128\text{ lane}\times256\text{ col}\)，回写分两块、每块 128 列。

**第二份交付：屏障交接表。** 抄录 4.3.2 的四行表格并自己核对每一格：每道屏障放在哪家、谁到达、到达量（字节数 65536 / 到达数 256）、谁等待、用 `remote_view` 还是 `cta_mask=3`。**「哪道屏障跨 CTA 生效」的答案**：四道全部跨 CTA——`tma2mma` 与 `ld2mma` 是「CTA 0 单份 + 远端操作」，`mma2tma` 与 `mma2ld` 是「两 CTA 双副本 + `cta_mask=3` 广播」。

**文字题**：用三到五句话回答「为何每个 CTA 各搬各的数据，`arrive.expect_tx` 却乘 `CTA_GROUP`」。参考答案骨架见 4.3.3 的五点（账本口径 ≠ 搬运执行方；一道账本管两家；消费者是同一条协作 MMA；登记 65536；少登则提前通过、多登则挂死）。

**可选加深（有 Blackwell GPU 时）**：把 `hgemm_v8` 抄入 `.py` 文件，按 u9-l2 回路 `tvm.compile(tir_pipeline="tirx")` 编译并用 PyTorch 参考断言验证 PASS；然后依次复现 4.3.4 的三个故障，记录实际症状与推演是否一致；最后按 u15-l4 的基准协议计时，对照书中 0.104 ms（B200、`M=N=K=4096`）。
无 GPU 时本综合实践的图表与推演部分完全可做，GPU 部分标注「待本地验证」。

## 6. 本讲小结

- **Step 8 只改协作范围**：warp 角色与流水线骨架沿用 Step 7，把 scope 从单 CTA 扩到 2-CTA cluster，一条 `cta_group=2` 的协作 MMA 产出 \(256\times256\) tile，实测 0.23 ms → 0.104 ms（约 2.2×）。
- **切分规则**：A 沿 M 对半决定行所有权、就近读取；B 的两个切片都被两个 CTA 的输出需要，协作 MMA 执行时经 DSMEM 跨 CTA 读对端 `Bsmem`——「每个 A 切片乘两个 B 切片」使 staged 操作数的复用翻倍（tile 级算术强度 64 → 128 FLOP/byte）。
- **地址三公式**：`m_st = m_base + cbx*128`（加载与行所有权）、`n_st = n_base + cbx*128`（本 CTA 贡献的 B 切片）、`n_st_epi = n_base + no*128`（回写块，不含 `cbx`）。
- **两种跨 CTA 屏障机制**：`remote_view(0)` 单点账本（`tma2mma`、`ld2mma` 在 CTA 0，多家生产一家消费）与 `cta_mask=3` 双副本广播（`mma2tma`、`mma2ld`，一家生产两家消费）。
- **练习 2 的答案**：`expect_tx` 登记的是屏障本相位全部关联传输的总字节；两 CTA 的 4 条 TMA load 都向 CTA 0 的同一道屏障 complete-tx，消费它们的是同一条协作 MMA，故必须登记 \(CTA\_GROUP\times(128\times64+128\times64)\times2=65536\) 字节——执行方各搬各的，完成口径要合账。
- **登记/到达数错误的三种症状**：字节少登 → 提前通过、静默错果；字节多登或漏广播 `cta_mask` → 挂死；到达数少登 → 相位提前翻转、覆盖未读完的 TMEM。

## 7. 下一步学习建议

下一讲 **u13-l3（Step 9：多消费者 warp specialization）** 在本讲的 cluster 之上再加第二个 MMA consumer：两个 consumer 读不同的 A 行块、**共享同一批 B 切片**，cluster 输出扩到 \(512\times256\)。你会看到本讲的三个数字如何再次变形——`expect_tx` 的字节数变成 \(CTA\_GROUP\times(NUM\_CONSUMER\times BLK\_M\times BLK\_K + BLK\_N\times BLK\_K)\times F16\_SIZE\)、`mma2tma` 每 stage 的期望到达数变成 `NUM_CONSUMER`、`mma2ld`/`ld2mma` 从按 stage 索引改成按 consumer 索引（源码见 [chapter_gemm_advanced/index.md:598-660](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L598-L660)）。建议先完成本讲综合实践的两份图表再进入下一讲；若想加深 cluster 硬件基础，可回看 `chapter_tensor_cores` 中 cta_group::2 的 TMEM 映射一节（u7-l2）与 `_extra/demo/cta_cluster.html` 交互演示。
