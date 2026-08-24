# cta_group 与 block-scaled MMA

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分 `cta_group::1` 与 `cta_group::2` 两种模式：前者只用当前 CTA 的 SMEM/TMEM，后者使用一个 CTA 对（cluster 内 `%cluster_ctarank` 仅差最低位的一偶一奇两个 CTA）的资源，且两种模式使用不同的 TMEM 累加器映射。
2. 写出四种常见配置下累加器元素 `C[m,n]` 到 `(CTA, TLane, TCol)` 的映射规则：`cta_group::1` M=128（恒等映射）、`cta_group::1` M=64（Layout F，四组 16 行、Lane 步距 32、对齐 0/16）、`cta_group::2` M=256（M 连续对半切）、`cta_group::2` M=128 dense A（Layout B，N 对折进 Lane 轴）。
3. 描述 block-scaled MMA 中 SFA/SFB 跨两 CTA 的放置规则：SFA 沿 M 分片（各 CTA 只持自己那半行的 scale factor），SFB 覆盖完整 N 并组播到 CTA 对双方；每 CTA 内部再经 `.warpx4` 复制到四个 32-lane 分区。
4. 说出 `tcgen05.cp`、`tcgen05.mma`、`tcgen05.ld` 等异步指令之间交接数据的三个条件，并会用"三问法"检查一条 `tcgen05` 指令。

## 2. 前置知识

本讲是「Blackwell Tensor Core 与 TMEM」单元第二讲，直接承接 u7-l1：

- **u7-l1（tcgen05.mma 的执行方式与 TMEM 累加器）**：你已经知道 `tcgen05.mma` 是单线程语义的 tile 级指令，A/B 经 `a-desc`/`b-desc` 从 SMEM 读取、累加器在 TMEM，`idesc` 打包 M/N/K，`enable-input-d` 即 accum 标志，完成信号经 `tcgen05.commit` 挂到 mbarrier。本讲回答上一讲留下的悬而未决的问题：**`cta_group` 这个限定符到底控制什么，以及累加器具体摆进 TMEM 的哪些坐标**。

还需要几个更早的概念，简单回顾：

| 概念 | 一句话回顾 |
| --- | --- |
| TMEM 二维地址空间（u2-l2、u4-l2） | 每 CTA 128 Lane × 最多 512 Col、每格 32 bit；坐标用命名轴 \( \text{TLane}, \text{TCol} \) 描述，Lane 是数据侧坐标而非线程 laneid |
| cluster 与 DSMEM（u2-l2） | cluster 把多个 CTA（各驻留一个 SM）编成一组，成员可经 DSMEM 互访对方的 SMEM；`cta_group::2` 的 CTA 对就住在同一个 cluster 里 |
| 操作 scope（u2-l1） | 谁发起、谁执行、谁受益可以分离；本讲的 `cta_group` 正是把"受益/占用资源的范围"从单 CTA 扩到 CTA 对 |
| replication（u4-l3） | `R[shape:strides]` 让同一逻辑元素出现在多个物理位置；scale factor 的 `.warpx4` 就是 `R[4:32@TLane]`，把 32-lane 基础 tile 复制到四个 32-lane 分区 |
| `tcgen05.cp` 与 `scale_vec`（u5-l3） | TMA 止步 SMEM，scale factor 需经 `tcgen05.cp.32x128b.warpx4` 补上"最后一公里"进 TMEM；`scale_vec` 1X/2X/4X 决定 32-bit 字内字节打包 |

**术语提示**：

- **CTA 对（CTA pair）**：同一 cluster 中 `%cluster_ctarank` 只差最低位的两个 CTA，一个偶 rank（even CTA）、一个奇 rank（odd CTA）。
- **Layout A/B/C/D/F**：PTX 文档给累加器 TMEM 映射编的号。本讲涉及 Layout F（`cta_group::1` M=64）、Layout A（`cta_group::2` M=256）、Layout B（`cta_group::2` M=128 dense A）；structured-sparse A 的 `cta_group::2` M=128 用 Layout C（本讲不展开）。
- **SFA/SFB/SFK**：block-scaled 格式（MXFP8、NVFP4）的 scale factor。`SFA(M, SFK)` 给 A 的每行一组，`SFB(N, SFK)` 给 B 的每列一组；SFK = K / block size（NVFP4 每 16 个元素一块，MXFP8 每 32 个一块）。
- **组播（multicast）与分片（shard）**：同一份数据送到多处叫复制/组播；一份数据按轴切开、各处只持一片叫分片。SFB 走前者，SFA 走后者。

## 3. 本讲源码地图

本仓库是教材仓库，"源码"是章节正文、交互演示与书图脚本：

| 文件 | 作用 |
| --- | --- |
| [chapter_tensor_cores/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md) | 本讲精读对象。第 110-207 行讲 `cta_group` 与四种 M 配置的 TMEM 映射，第 209-243 行讲 block-scaled 放置，第 245-251 行讲指令间交接 |
| [img/scripts/gen_mma_layouts.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py) | 生成五种配置示意图（四张映射图 + 一张 block-scaled 图）的 matplotlib 脚本，其注释标明了各配置对应的 Layout 编号，是本讲代码实践的对象 |
| [img/mma_cg1_m64.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg1_m64.svg)、[img/mma_cg2_m256.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg2_m256.svg)、[img/mma_cg2_m128.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg2_m128.svg)、[img/mma_block_scaled.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_block_scaled.svg) | 上述脚本的输出，正文嵌入的四张图，本讲实践的对照答案 |
| [_extra/demo/tcgen05_intro.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tcgen05_intro.html) | 交互演示（u7-l1 已用过的默认 M=128 视图，可换 N、转置 A/B 观察指令参数变化） |
| [chapter_layout_generations/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md) | 补充材料：`tcgen05.cp` 数据路径与 `scale_vec` 选字节的细节（u5-l3 已精读，本讲只引用结论） |

## 4. 核心概念与源码讲解

### 4.1 模块一：cta_group 两种模式——单 CTA 与 CTA 对

#### 4.1.1 概念说明

`cta_group` 是 `tcgen05.mma` 指令名里的一个限定符，它回答的问题是：**这一次 MMA 占用哪一级的资源？**

- `cta_group::1`：只使用**当前 CTA** 的 SMEM 与 TMEM。MMA 产生的累加器全部落在自己 CTA 的 TMEM 里。
- `cta_group::2`：使用**一个 CTA 对**的资源。CTA 对是同一 cluster 中 `%cluster_ctarank` 只差最低位的两个 CTA（一偶一奇）；MMA 可以访问两个 CTA 的 TMEM，累加器分布在两块 TMEM 上。

关键认识有三点：

1. **`cta_group` 不改变单线程发起语义**。无论 `::1` 还是 `::2`，仍然只需要一个线程发出指令；它只决定操作占用单 CTA 还是 CTA 对的资源。
2. **发起线程可以在任一侧，但对端必须活着**。CTA 对中只需一个线程发起 `tcgen05.mma`，它可属于 even 或 odd 任一 CTA，但另一个 CTA 必须保持活跃。本书后续内核的惯例是在 even CTA 中选出一个线程发起，并用 `tcgen05.commit` 把完成信号挂到 mbarrier。
3. **两种模式的 TMEM 累加器映射不同**——这正是下一模块的主题。

另外，指令描述符 `idesc` 提供 N。对本书主用的 `f16`/`bf16` 路径，`cta_group::1` 支持 N 从 8 到 256、步长 8；`cta_group::2` 支持 N 从 16 到 256、步长 16。下文四种配置中的符号 N 取这些合法值中的任意一个。

#### 4.1.2 核心流程

一次带 `cta_group` 限定的 MMA，从"选资源范围"到"落 TMEM"的决策链：

```text
选择 cta_group（::1 或 ::2）
        │
        ├─ ::1 → 资源范围 = 当前 CTA
        │         累加器写入本 CTA TMEM
        │         N ∈ {8, 16, ..., 256}（步长 8，f16/bf16 路径）
        │
        └─ ::2 → 资源范围 = CTA 对（cluster 内 rank 差最低位）
                  even CTA + odd CTA，对端须保持活跃
                  累加器分布在两块 TMEM
                  N ∈ {16, 32, ..., 256}（步长 16，f16/bf16 路径）
        │
        ▼
由 (cta_group, M, A 是否 dense, 是否 .ws) 四个选择共同确定
累加器布局：把每个逻辑坐标 (m, n) 映射到某 CTA 的 (TLane, TCol)
        │
        ▼
even CTA 中 elect 出的一个线程发出 tcgen05.mma
→ tcgen05.commit 挂 mbarrier → 硬件完成后经 barrier 通知
```

注意最后一层：**累加器布局不是由 `cta_group` 单独决定的**，而是由 `cta_group`、M 的大小、A 是 dense 还是 structured sparse、是普通 `tcgen05.mma` 还是 weight-stationary 的 `tcgen05.mma.ws` 这四个选择共同决定。本讲覆盖 dense A、非 `.ws` 的四种组合。

#### 4.1.3 源码精读

指令形式与字段表（u7-l1 已逐字段讲过，这里只看 `cta_group` 一行）：

> [chapter_tensor_cores/index.md:L54-L58](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L54-L58) 给出指令形式 `tcgen05.mma.cta_group.kind [d-tmem], a-desc, b-desc, idesc, ...`；字段表中 [L64](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L64) 写明 `cta_group` 的作用是"选择当前 CTA 或一个 CTA 对作为 MMA 使用的资源"。

`cta_group` 一节的定义性陈述：

> [chapter_tensor_cores/index.md:L110-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L110-L116) 说明：`::1` 只更新当前 CTA 的 TMEM；`::2` 访问 CTA 对双方的 TMEM，CTA 对由 cluster 中 `%cluster_ctarank` 仅差最低位的两个 CTA 构成（一偶一奇）；CTA 对中只需一个线程发起指令，该线程可属于任一 CTA，但对端必须保持活跃；本书内核惯例是在 even CTA 中选一个线程发起并用 `tcgen05.commit` 关联 mbarrier。

布局由四个选择共同决定，以及 N 的取值范围：

> [chapter_tensor_cores/index.md:L118-L120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L118-L120) 写道：累加器布局取决于 `cta_group`、M 的大小、A 是 dense 还是 structured sparse、是普通 `tcgen05.mma` 还是 `.ws` 四个选择；`idesc` 提供 N，`f16`/`bf16` 路径下 `cta_group::1` 支持 N 从 8 到 256 步长 8，`cta_group::2` 支持 N 从 16 到 256 步长 16。

#### 4.1.4 代码实践

**实践目标**：把"CTA 对"从文字变成可计算的规则，并验证你对发起规则的理解。

**操作步骤**：

1. 阅读上面的源码精读段落，确认三条规则：对内 rank 仅差最低位；发起线程可在任一侧；对端必须活跃。
2. 写一个小脚本（示例代码，无需 GPU）：

```python
# peer.py —— 示例代码：由 cluster 内 rank 求所在 CTA 对
def cta_pair(rank: int):
    """返回 (本 CTA 是 even 还是 odd, 对端 rank)。对内 rank 仅差最低位。"""
    role = "even" if rank % 2 == 0 else "odd"
    return role, rank ^ 1        # 翻转最低位即对端

for rank in range(4):            # 假设 cluster 大小为 4：两个 CTA 对
    print(f"rank {rank}: {cta_pair(rank)}")
```

3. 再回答一个阅读检查题（不改代码）：若内核让 even CTA 的一个线程发起 `cta_group::2` MMA，而 odd CTA 在此之前已经退出，会发生什么？

**需要观察的现象**：脚本的 4 行输出应把 rank 0/1 配成一对、rank 2/3 配成一对，各自一偶一奇。

**预期结果**：

```text
rank 0: ('even', 1)
rank 1: ('odd', 0)
rank 2: ('even', 3)
rank 3: ('odd', 2)
```

（待本地验证——本讲作者未在写作环境中运行此脚本，但它只依赖整数运算。）

**阅读检查的答案**：对端必须保持活跃（正文原话 "the peer CTA must remain active"）；odd CTA 提前退出违反了 `cta_group::2` 的前提，MMA 无法在 CTA 对的资源上正确完成。这也是后续 GEMM Step 8 中两个 CTA 的收尾要互相配合的原因。

#### 4.1.5 小练习与答案

**练习 1**：用一句话说清 `cta_group` 改变与不改变的东西。

**答案**：它改变 MMA 占用的资源范围（当前 CTA 的 SMEM/TMEM，还是 CTA 对双方的）以及累加器的 TMEM 映射；它不改变"单个线程发起、硬件执行整个 tile"的指令语义。

**练习 2**：`f16` 路径下 `cta_group::2` 为什么 N 至少是 16、步长也是 16？正文给了原因吗？

**答案**：正文只陈述了取值范围（[L120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L120)），未给原因。一个合理推断是：`cta_group::2` 的累加器布局（Layout B，见模块二）把 N 沿中点对折、分别映射到 Lane 轴的下半与上半，因此 N/2 也必须保持 8 的倍数，于是 N 只能取 16 的倍数——此为依据布局规则的推断，待与 PTX ISA 文档核对。

**练习 3**：一个 cluster 里有 4 个 CTA（rank 0-3），能发起几条互不冲突的 `cta_group::2` MMA？

**答案**：两对——(rank 0, rank 1) 与 (rank 2, rank 3) 各自是一条 `cta_group::2` MMA 的资源范围；配对由最低位决定，不能跨对组合（如 rank 1 与 rank 2 不能成对，它们的 rank 不止差最低位）。

### 4.2 模块二：四种 M 配置的 TMEM 累加器映射

#### 4.2.1 概念说明

上一模块说"两种模式的 TMEM 映射不同"，本模块把这个"映射"讲透：**给定逻辑坐标 `C[m,n]`，它落在哪个 CTA 的哪根 TLane、哪列 TCol？**

为什么要关心这件事？因为 TMEM 布局是"写端"与"读端"的合同：`tcgen05.mma` 按布局写，epilogue 的 `tcgen05.ld` 必须用**兼容的 TMEM 地址与 load 形状**才能把逻辑 C tile 还原出来（[L207](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L207)）。布局弄错，字节都在但元素认错——与 u6-l1 的"tensor map、SMEM 布局与 MMA 指令必须描述同一物理排布"是同一条一致性纪律在 TMEM 侧的翻版。

四种配置（dense A、非 `.ws`）一句话概括：

| 配置 | 布局 | 一句话规则 | Lane 占用 |
| --- | --- | --- | --- |
| `cta_group::1`，M=128 | 恒等映射 | 行 \( m \) 直接就是 TLane \( m \)，N 沿列展开 | 一个 CTA 的 128 根 lane 全占 |
| `cta_group::1`，M=64 | Layout F | 64 行分四组各 16 行，每组放进一个 32-lane 区的半边；对齐参数 a=0/16 选互补位置 | 每 32-lane 区只用一半，共 64 根 |
| `cta_group::2`，M=256 | Layout A | M 连续对半切：even 存行 0-127、odd 存行 128-255，各占满自己 lane 0-127 | 两个 CTA 各 128 根全占 |
| `cta_group::2`，M=128（dense A） | Layout B | M 对半切后再把 N 对折进 Lane 轴：N 低半落 lane 0-63、高半落 lane 64-127 | 两个 CTA 各 128 根全占（被 N 的两半瓜分） |

为什么会有这些花样？根子在于 **TMEM 每格 32 bit、每 CTA 固定 128 根 Lane**：

- M=128 恰好等于 lane 数，行 lane 一一对应，最省心；
- M=64 只有 64 行，硬件数据通路仍按四个 32-lane 区（对应 `warp-rank % 4 = 0,1,2,3`）组织，于是把 64 行拆成四组"撒"进四个区，每区用一半——剩下的半边正好塞第二块独立的 M=64 tile；
- M=256 超过单 CTA lane 数，只能靠 CTA 对凑出 256 行；
- M=128 却用 `cta_group::2` 时，行数不超过单 CTA 容量，为什么还要两个 CTA？为的是让两 CTA 各持一半 A、共用整块 B（模块三与 GEMM Step 8 的动机）；此时每 CTA 只算 64 行，空出的另一半 Lane 轴正好被 N 的另一半利用，TMEM 面积不浪费。

#### 4.2.2 核心流程

四个配置的映射规则（N 为合法范围内的任意值）：

**配置 1：`cta_group::1`，M=128（恒等映射）**

```text
CTA   = 当前 CTA
TLane = m
TCol  = n（N 沿列展开）
占用  = 128 Lane × N Col
```

**配置 2：`cta_group::1`，M=64，Layout F**

设 \( a \) 为 lane 对齐量（0 或 16）：

\[
\text{group} = \lfloor m / 16 \rfloor,\qquad
\text{row\_in\_group} = m \bmod 16,
\]

\[
\text{TLane} = 32\cdot\text{group} + a + \text{row\_in\_group},\qquad
\text{TCol} = n .
\]

对齐 0 时四组行分别落 lane 0-15、32-47、64-79、96-111；lane 16-31、48-63、80-95、112-127 不属于本 tile。对齐 16 时恰好落进这些空出的位置。两块 tile 的 Lane 不重叠，因此**可以共用同一批 TMEM 列**。

**配置 3：`cta_group::2`，M=256（Layout A）**

```text
CTA   = even（m < 128） / odd（m >= 128）
TLane = m mod 128（各自 CTA 内 lane 0-127 全占）
TCol  = n
```

物理上是两块分属不同 CTA 的 TMEM；逻辑上拼成一个 \( 256 \times N \) 的累加器 tile。A/B 在两个 CTA 的 SMEM 里如何分布取决于内核设计，不属于累加器布局的一部分（脚本注释给出了常见方案：每 CTA 持一半 B 列，跨对汇集出完整 B，各用自己那半 A 乘完整 B 得到自己的 128 行 C）。

**配置 4：`cta_group::2`，M=128，dense A（Layout B）**

先按 M 对半：even 存 C 行 0-63，odd 存行 64-127。CTA 内再把 64 行分成两个 32 行组、把 N 分成低半（\( n < N/2 \)）与高半，2×2 地映射到该 CTA 的四个 32-lane 区：

| N 范围 | CTA 内本地 M 行 | TMEM lanes |
| --- | --- | --- |
| `0 ... N/2-1` | `0-31` | `0-31` |
| `0 ... N/2-1` | `32-63` | `32-63` |
| `N/2 ... N-1` | `0-31` | `64-95` |
| `N/2 ... N-1` | `32-63` | `96-127` |

设 \( m_{\text{local}} = m \bmod 64 \)：

\[
\text{CTA} =
\begin{cases}
\text{even}, & m < 64\\
\text{odd}, & m \ge 64
\end{cases}
\qquad
\text{TLane} =
\begin{cases}
m_{\text{local}}, & n < N/2\\
64 + m_{\text{local}}, & n \ge N/2
\end{cases}
\]

章节给出的实例：N=16 时 `C[10,3]` 在 even CTA 的 TLane 10；`C[10,11]` 仍在 even CTA，但因 n=11 落在 N 高半，映射到 TLane 74。

**双射性自检**（u4-l2 引入的检验工具）：每个配置下，\( M \times N \) 个逻辑元素应恰好占满同样多个互不相同的 \( (\text{CTA}, \text{TLane}, \text{TCol}) \) 格子。例如配置 3：\( 2 \text{ 个 CTA} \times 128 \text{ lane} \times N \text{ col} = 256N \) ✓；配置 4：\( 2 \times 128 \times (N/2) = 128N \) ✓。

#### 4.2.3 源码精读

**配置 1（恒等映射）**：

> [chapter_tensor_cores/index.md:L122-L128](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L122-L128) 写道：这是最直接的情形——一个 CTA 算 128 行输出 tile，TMEM 恰有 128 根 Lane，累加器行 \( m \) 直接映射到 TMEM Lane \( m \)，N 沿列展开，结果占 128 Lane × N Col；A、B 读自本 CTA SMEM，完整累加器 tile 留在本 CTA TMEM。

**配置 2（Layout F）**：

> [chapter_tensor_cores/index.md:L130-L142](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L130-L142) 定义 Layout F：把 128 根 TMEM lane 分成四个 32-lane 区（对应硬件数据通路的 `warp-rank % 4 = 0,1,2,3`），把 64 个 M 行分成四组各 16 行、每组放进一个区；每组只有 16 行，所以每个 32-lane 区只用到一半。设 lane 对齐量 \( a \) 为 0 或 16，给出公式 `group = m//16`、`TLane = group*32 + a + m%16`。
>
> [chapter_tensor_cores/index.md:L144-L166](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L144-L166) 列出对齐 0 的行→lane 表（行 0-15→lane 0-15、行 16-31→lane 32-47、行 32-47→lane 64-79、行 48-63→lane 96-111，其余 lane 不属于本 tile），并说明第二块独立 M=64 tile 可取对齐 16，恰好占满互补位置；两块 tile 的 Lane 不重叠，因此能共用同一批 TMEM 列。

**配置 3（Layout A）**：

> [chapter_tensor_cores/index.md:L168-L176](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L168-L176) 写道：M=256 超出单 CTA 的 128 根 Lane，累加器分布到 CTA 对的两块 TMEM 分配上——even CTA 存逻辑行 0-127、odd CTA 存行 128-255，各自用本 CTA lane 0-127，N 沿各自 TMEM 列展开；物理上是两块分属不同 CTA 的 128 行 TMEM 区域，逻辑上构成一个 \( 256\times N \) 累加器 tile；A、B 在两 CTA SMEM 间的分布取决于内核，不属于累加器布局。

**配置 4（Layout B）**：

> [chapter_tensor_cores/index.md:L178-L189](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L178-L189) 说明 dense A 的 `cta_group::2, M=128` 用 PTX 的 Layout B：M 先对半（even 存行 0-63、odd 存行 64-127），CTA 内 64 行再分两个 32 行组、N 分低高两半，2×2 映射到该 CTA 的四个 32-lane 区，并用表格列出 (N 范围, 本地 M 行) → TMEM lanes 的对应。
>
> [chapter_tensor_cores/index.md:L191-L203](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L191-L203) 给出公式：`CTA = even if m<64 else odd`；`TLane = m_local if n<N/2 else 64+m_local`（\( m_{\text{local}}=m\bmod 64 \)）；举出 N=16 的实例 `C[10,3]`→even CTA TLane 10、`C[10,11]`→even CTA TLane 74；最后强调 structured-sparse A 时改用 Layout C，上述映射不适用。

**写读合同**：

> [chapter_tensor_cores/index.md:L207](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L207) 收束一句：这四种布局规定了 `tcgen05.mma` 把每个累加器元素写到哪个 CTA、哪个 TMEM 位置；随后的 `tcgen05.ld` 必须使用兼容的 TMEM 地址与 load 形状才能重建原来的逻辑 C tile。

**图脚本侧的对应**：

> [img/scripts/gen_mma_layouts.py:L1-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L1-L9) 的模块注释写明脚本的地基是 nymph-rust 解释器 `tcgen05.rs` 中 `mma_blocks()` 的累加器 Layouts D/F/A/B，并概括了操作数路径：`rows_per_cta = m or m/2`、`n_seg = n or n/2`、B 跨 CTA 对汇集、SFA 按 M 分片、SFB 每 CTA 持全 N。
>
> [img/scripts/gen_mma_layouts.py:L152-L161](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L152-L161)（注释标明 Layout A）与 [L163-L172](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L163-L172)（Layout B）分别画出两种 `cta_group::2` 配置；M=256 图的中间注释写明"B 的两半（各 N/2）跨对拼成完整 B(N,K)；每个 CTA 用自己的 A 半边乘完整 B 得到自己的 128 行 C"，M=128 图则标注"N 低半→lane 0-63、N 高半→lane 64-127"。
>
> [img/scripts/gen_mma_layouts.py:L102-L120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L102-L120) 画 Layout F：循环里偶数条带画四个 16 行组（"rows 0-15" 等），奇数条带画成灰色斜纹——正文中"交替的 Lane 带未被使用/留给对齐 16 的第二块 tile"的图形化表达（[L110-L119](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L110-L119)）。

顺带一提：脚本 docstring 列出 Layouts D/F/A/B 四个编号，正文明确点名 F（cg1 M=64）与 B（cg2 M=128 dense）、脚本注释把 cg2 M=256 标为 A，剩下的 D 按排除法对应 cg1 M=128——这一字母对应关系以脚本注释与 PTX 文档为准。

#### 4.2.4 代码实践

**实践目标**：把四种映射实现成可执行的 Python 函数，用章节给出的实例与双射性检验自证正确，并打印 lane 条带草图（本讲规格中的画图任务前半部分）。

**操作步骤**：

1. 新建 `mma_layouts.py`（示例代码，无需 GPU），实现四个映射函数：

```python
# mma_layouts.py —— 示例代码：四种 (cta_group, M) 配置的 TMEM 累加器映射
# 公式依据 chapter_tensor_cores/index.md "How cta_group Sets the Operation Scope" 一节

def cg1_m128(m, n):
    """cta_group::1, M=128：行 m 恒等映射到 TLane m，N 沿 TCol 展开。"""
    return ("cta0", m, n)

def cg1_m64(m, n, align=0):
    """cta_group::1, M=64（Layout F，非 .ws）：四组各 16 行，Lane 步距 32。
    align 取 0 或 16，选互补的两组 lane 位置。"""
    return ("cta0", (m // 16) * 32 + align + m % 16, n)

def cg2_m256(m, n):
    """cta_group::2, M=256（Layout A）：M 连续对半切到 CTA 对，各占满 lane 0-127。"""
    return ("even" if m < 128 else "odd", m % 128, n)

def cg2_m128_dense(m, n, N):
    """cta_group::2, M=128（dense A，Layout B）：N 对折进 Lane 轴。
    注意：正文显式给出的只有 CTA 与 TLane 公式；tcol 取 n 在其 N 半边
    内的位置（n % (N//2)）是按图示的直接推论，待本地验证。"""
    cta = "even" if m < 64 else "odd"
    m_local = m % 64
    tlane = m_local if n < N // 2 else 64 + m_local
    return (cta, tlane, n % (N // 2))
```

2. 追加断言，对照正文实例与双射性：

```python
# 1) 章节例证：Layout B、N=16（正文 L201）
assert cg2_m128_dense(10, 3, 16)  == ("even", 10, 3)
assert cg2_m128_dense(10, 11, 16) == ("even", 74, 3)

# 2) Layout F 的行->lane 表（对齐 0，正文 L146-L153）
assert [cg1_m64(m, 0)[1] for m in range(16)] == list(range(16))
assert cg1_m64(16, 0)[1] == 32
assert cg1_m64(32, 0)[1] == 64
assert cg1_m64(48, 0)[1] == 96
assert cg1_m64(0, 0, align=16)[1] == 16      # 互补对齐的第二块 tile

# 3) 双射性：M*N 个元素恰好占 M*N 个互不相同的 (CTA, TLane, TCol) 格
def bijective(map_fn, M, N):
    cells = {map_fn(m, n) for m in range(M) for n in range(N)}
    return len(cells) == M * N

assert bijective(lambda m, n: cg1_m128(m, n), 128, 16)
assert bijective(lambda m, n: cg1_m64(m, n), 64, 16)
assert bijective(cg2_m256, 256, 16)
assert bijective(lambda m, n: cg2_m128_dense(m, n, 16), 128, 16)
print("all assertions passed")
```

3. 再打印 Layout F 的 lane 条带草图：

```python
def sketch_cg1_m64():
    """# = 对齐 0 的当前 tile；= = 对齐 16 的互补位置；空白 = 两者都不用。"""
    owner = [" "] * 128
    for m in range(64):
        owner[cg1_m64(m, 0)[1]] = "#"
        owner[cg1_m64(m, 0, align=16)[1]] = "="
    for base in range(0, 128, 32):
        print(f"lane {base:3d}-{base + 31:3d}: " + "".join(owner[base:base + 32]))

sketch_cg1_m64()
```

**需要观察的现象**：断言全部通过；条带图中每个 32-lane 区都呈现"前 16 格 `#`、后 16 格 `=`"，即两块 M=64 tile 恰好互补铺满 128 根 lane。

**预期结果**：

```text
all assertions passed
lane   0- 31: ################================
lane  32- 63: ################================
lane  64- 95: ################================
lane  96-127: ################================
```

（待本地验证——写作环境未执行 Python，以上输出为按公式推演的结果。）

4.（可选）对照 [img/mma_cg1_m64.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg1_m64.svg) 与 [img/mma_cg2_m128.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg2_m128.svg) 检查你的理解：橙色条带对应 `#`，斜纹空带对应 `=`。

#### 4.2.5 小练习与答案

**练习 1**：Layout F 中，要让第三组（行 32-47）落进 lane 80-95，对齐参数 \( a \) 应取多少？

**答案**：\( a=16 \)。第三组 group=2，\( \text{TLane} = 2\times32 + a + (m \bmod 16) \)，行 32（组内第 0 行）要落在 80，需 \( 64 + a = 80 \)。

**练习 2**：`cta_group::2`、M=128（dense A）、N=16 时，`C[70,5]` 与 `C[70,13]` 各在哪里？

**答案**：两处都在 odd CTA（\( m=70\ge 64 \)），\( m_{\text{local}}=6 \)。`C[70,5]`：\( n=5<8 \) 落 N 低半，TLane 6；`C[70,13]`：\( n=13\ge 8 \) 落 N 高半，TLane \( 64+6=70 \)。

**练习 3**：配置 2（cg1 M=64）只用了一半 lane，为什么双射性检验仍然通过？两块对齐互补的 tile 为什么能共用同一批 TMEM 列？

**答案**：双射性按"单个 tile"计：一块 M=64 tile 有 \( 64\times N \) 个元素，恰好占 64 根互不相同的 lane × N 列，一一对应。两块 tile 的 lane 集合（对齐 0 与对齐 16）在 128 根 lane 轴上完全不重叠，同一列号下落在不同 lane，因此不会互相覆盖——"列共享、lane 分家"。

### 4.3 模块三：block-scaled MMA 的 scale factor 放置与指令交接

#### 4.3.1 概念说明

MXFP8、NVFP4 等 block-scaled 格式把每个数据块的公共指数抽成 scale factor：A 侧是 `SFA(M, SFK)`（A 每行、每个 K-scale 块一个），B 侧是 `SFB(N, SFK)`（B 每列一个）。block-scaled `tcgen05.mma` 一边继续从 SMEM 读 A/B，一边**从 TMEM 读 SFA/SFB**。于是数据路径分成两条：

```text
A, B:     GMEM -> SMEM -> tcgen05.mma
SFA, SFB: GMEM -> SMEM -> tcgen05.cp -> TMEM -> tcgen05.mma
```

scale factor 比 A/B 多一步 `tcgen05.cp`，因为 TMA 止步 SMEM（u5-l3 讲过的"最后一公里"）。

本模块聚焦的问题是：**在 CTA 对里，SFA/SFB 各放在哪？** 答案由乘法本身决定——输出坐标 \( (m,n) \) 处，A 用 `SFA[m,sfk]`、B 用 `SFB[n,sfk]`，所以 **scale factor 的放置跟随 M 与 N 的切分方式**：

- **SFA 沿 M 分片**（shard）：`cta_group::2` M=256 时 even CTA 只算行 0-127，就只需要 `SFA[0:128,:]`；odd CTA 持 `SFA[128:256,:]`。谁算哪段行，谁就备哪段行的 scale factor。
- **SFB 覆盖完整 N 并组播**（replicate）：常见实现里每个 CTA 只把 B 的半边 N 列搬进自己的 SMEM，但**协作 MMA 消费的是完整 B tile**，所以完整 N 的 `SFB[0:N,:]` 必须对两个 CTA 都可用——先把 SFB 组播进 CTA 对的两份 SMEM，再用 `tcgen05.cp.cta_group::2` 从各 CTA 的本地 SMEM 拷进本地 TMEM。
- **CTA 内还有第二层复制**：无论 SFA 还是 SFB，`tcgen05.cp.32x128b.warpx4` 都把打包好的 32-lane 基础布局组播到本 CTA 的全部四个 32-lane TMEM 分区（TLane 0-31、32-63、64-95、96-127）——这正是 u4-l3 用 `R[4:32@TLane]` 描述过的跨 warp 广播。

最后，把 `tcgen05.cp`、`tcgen05.mma`、`tcgen05.ld` 这些异步指令串成一条链需要**三个条件**：操作必须指向正确的 CTA 或 CTA 对；生产者的输出布局必须与消费者期望的布局一致；消费者访问数据前必须先走对应的完成与排序机制（u7-l1 的 commit/mbarrier/fence 协议）。任一条件不满足，硬件可能解读错误的 TMEM 坐标，或读到还在更新中的数据。

#### 4.3.2 核心流程

`cta_group::2` M=256 block-scaled MMA 一次完整的数据准备与执行：

```text
# —— 数据准备：三条搬运线 ——
线 1（A/B 主链路）：
  GMEM --TMA--> 各 CTA SMEM
    even CTA: A 行 0-127   +  B 的低半 N 列
    odd  CTA: A 行 128-255 +  B 的高半 N 列
    （B 的两半跨 CTA 对汇集出完整 B(N,K)——MMA 从两侧 SMEM 读）

线 2（SFB，组播）：
  GMEM --TMA/组播--> 两份 SMEM（内容相同：SFB[0:N, :]）
    even SMEM: SFB[0:N, :]      odd SMEM: SFB[0:N, :]

线 3（SFA，分片）：
  GMEM --> even SMEM: SFA[0:128, :]     odd SMEM: SFA[128:256, :]

# —— SMEM -> TMEM：tcgen05.cp.cta_group::2 ——
每 CTA 把本地 SMEM 里的 SFA/SFB 拷进本地 TMEM；
.32x128b.warpx4 顺带把 32-lane 基础布局组播到四个 32-lane 分区
    even TMEM: SFA(行 0-127)×4 分区   + SFB(全 N)×4 分区
    odd  TMEM: SFA(行 128-255)×4 分区 + SFB(全 N)×4 分区

# —— 计算 ——
even CTA 选出一个线程发起 block-scaled tcgen05.mma（cta_group::2）
  A、B：从两侧 SMEM 描述符读
  SFA/SFB：从两侧 TMEM 读
  累加器：按 Layout A 写入（even 行 0-127、odd 行 128-255）
→ tcgen05.commit 挂 mbarrier → 消费者 wait + fence 后 tcgen05.ld 读回
```

三条放置规则一图以蔽之：**SFA 按 M 切、SFB 全 N 双份、CTA 内四分区复制**。

#### 4.3.3 源码精读

**数据路径分叉**：

> [chapter_tensor_cores/index.md:L209-L218](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L209-L218) 说明：MXFP8 与 NVFP4 是两种具体的 block-scaled 格式；`SFA(M,SFK)` 给 A 的每行供应 scale factor，`SFB(N,SFK)` 给 B 的每列供应；block-scaled `tcgen05.mma` 从 TMEM 读这些因子、继续从 SMEM 读 A/B，并画出两条路径（A/B 走 GMEM→SMEM→mma，SFA/SFB 走 GMEM→SMEM→`tcgen05.cp`→TMEM→mma）。

**放置规则跟随 M/N 切分**：

> [chapter_tensor_cores/index.md:L220-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L220-L229) 写道：输出坐标 \( (m,n) \) 处 A 用 `SFA[m,sfk]`、B 用 `SFB[n,sfk]`，因此 CTA 对中的 scale factor 放置跟随 M 与 N 的划分；以图中 M=256 MMA 为例，even CTA 算行 0-127、odd CTA 算行 128-255，每边只需自己那些 A 行的 SFA——`even: SFA[0:128,:]`、`odd: SFA[128:256,:]`。

**SFB 组播与 `tcgen05.cp.cta_group::2`**：

> [chapter_tensor_cores/index.md:L231-L237](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L231-L237) 说明：图中常用实现是每个 CTA 只把 B 的半边 N 列搬进本地 SMEM，而协作 MMA 消费完整 B tile，因此完整 N 的 `SFB[0:N,:]` 必须对两个 CTA 都可用；常见做法是先把 SFB 组播进 CTA 对的两份 SMEM，再用 `tcgen05.cp.cta_group::2` 把数据从各 CTA 的本地 SMEM 拷进本地 TMEM。

**CTA 内第二层复制**：

> [chapter_tensor_cores/index.md:L239-L243](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L239-L243) 写道：每 CTA 内还有第二层复制——对 SFA 与 SFB，`tcgen05.cp.32x128b.warpx4` 都把打包好的 32-lane 基础布局拷进全部四个 32-lane TMEM 分区（TLane 0-31、32-63、64-95、96-127）；所以图中 SFA 沿 M 分片、每个 CTA 各有一份完整 SFB，且两个 scale factor 布局在各 CTA 内部都再复制到四个 32-lane 分区。

**指令间交接的三个条件与三问法**：

> [chapter_tensor_cores/index.md:L245-L251](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L245-L251) 总结：串联 `tcgen05.cp`、`tcgen05.mma`、`tcgen05.ld` 这类异步指令需要三个条件——操作指向正确的 CTA 或 CTA 对、生产者输出布局与消费者期望布局一致、消费者在访问数据前使用对应的完成与排序机制；任一条件失败，硬件可能解读错误的 TMEM 坐标或读到仍在更新的数据。理解 `tcgen05` 的钥匙是三问：这条指令用哪个 CTA 的资源、它把数据映射到哪里、下一阶段何时可以安全消费结果。

**图脚本侧的对应**：

> [img/scripts/gen_mma_layouts.py:L174-L194](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L174-L194) 画 block-scaled 配置：每 CTA 一行，SMEM 侧是 fp8/fp4 打包的 A（各自那半行）与 B（半边列），TMEM 侧并排放 SFA `(M,SFK)`、SFB `(N,SFK)`（标注 full N、multicast）与 C 累加器，图内文字标明 "SFA split by M (per CTA) · SFB full-N to both"（[L189-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L189-L190)），中间注释还给出 SFK = K / block size（NVFP4 为 16、MXFP8 为 32，[L191-L193](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_layouts.py#L191-L193)）。

**交叉引用（u5-l3 已精读，不重复）**：`tcgen05.cp` 如何写出 `R[...]` 复制布局、`scale_vec` 1X/2X/4X 的字内打包与 `SFA_ID`/`SFB_ID` 选字节，见 [chapter_layout_generations/index.md:L251-L285](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L251-L285)。

#### 4.3.4 代码实践

**实践目标**：为 block-scaled `cta_group::2` M=256 场景生成 SFA/SFB 放置表（本讲规格中画图任务的后半部分），并与脚本生成的书图互相印证。

**操作步骤**：

1. 在 `mma_layouts.py` 中追加放置函数与打印（示例代码）：

```python
def sf_placement_cg2_m256(M=256, N=64):
    """block-scaled、cta_group::2、M=256 的 SFA/SFB 放置表。
    规则：SFA 沿 M 分片（谁算哪段行谁备哪段）；SFB 全 N 组播到双方。"""
    half = M // 2
    rows = {"even": (0, half), "odd": (half, M)}
    print(f"block-scaled cg2 M={M}, N={N}")
    for cta, (lo, hi) in rows.items():
        sfa = f"SFA[{lo}:{hi}, :]        # 分片：只持本 CTA 计算的 {hi-lo} 行"
        sfb = f"SFB[0:{N}, :]            # 组播：完整 N，两个 CTA 各一份"
        repl = "tcgen05.cp.32x128b.warpx4 -> 复制到 TLane 0-31/32-63/64-95/96-127"
        print(f"{cta:>4} CTA TMEM:\n        {sfa}\n        {sfb}\n        {repl}")

sf_placement_cg2_m256()
```

2. 执行后与 [img/mma_block_scaled.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_block_scaled.svg) 对照：图上每个 CTA 的 TMEM 框里 SFA 子块标注的行区间、SFB 子块标注的 "full N (multicast)" 应与你的打印一致。
3.（可选，需 matplotlib）运行书图脚本重新生成五张 SVG：

```bash
cd 仓库根目录
python img/scripts/gen_mma_layouts.py
git status img/   # 脚本会覆盖 img/ 下五个 .svg；确认无误后可 git checkout -- img/ 还原
```

**需要观察的现象**：步骤 1 打印出两 CTA 各持 SFA 半边（0-127 与 128-255）、SFB 双份全 N、且都带四分区复制标注；步骤 3 脚本输出 `wrote 5 figures: ...` 并刷新五个 SVG（内容应与仓库中版本一致）。

**预期结果**（步骤 1 的输出，待本地验证）：

```text
block-scaled cg2 M=256, N=64
even CTA TMEM:
        SFA[0:128, :]        # 分片：只持本 CTA 计算的 128 行
        SFB[0:64, :]         # 组播：完整 N，两个 CTA 各一份
        tcgen05.cp.32x128b.warpx4 -> 复制到 TLane 0-31/32-63/64-95/96-127
 odd CTA TMEM:
        SFA[128:256, :]      # 分片：只持本 CTA 计算的 128 行
        SFB[0:64, :]         # 组播：完整 N，两个 CTA 各一份
        tcgen05.cp.32x128b.warpx4 -> 复制到 TLane 0-31/32-63/64-95/96-127
```

**注意**：步骤 3 会覆写仓库 `img/` 下的五个提交资产；建议在一次性克隆里做，或做完用 `git checkout -- img/` 还原，不要把重新生成的图提交进仓库。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SFB 必须完整 N 双份，而 SFA 只需各持半边？

**答案**：由乘法决定——输出 \( (m,n) \) 处 A 行用到 `SFA[m,:]`、B 列用到 `SFB[n,:]`。每个 CTA 只计算自己那半 M 行，所以只需那段行的 SFA（分片即可）；但协作 MMA 消费的 B tile 覆盖完整 N（每 CTA 的 SMEM 只搬了半边 B 列，跨对拼成整块），两个 CTA 算的行都要乘到全部 N 列，因此完整 N 的 SFB 对双方都必须可用（组播双份）。

**练习 2**：把放置规则迁移到 `cta_group::2` M=128（dense A，Layout B）的 block-scaled 场景，SFA/SFB 应怎么放？

**答案**：规则是"放置跟随 M 与 N 的划分"。Layout B 下 even CTA 算行 0-63、odd CTA 算行 64-127，故 SFA 分片为 even 持 `SFA[0:64,:]`、odd 持 `SFA[64:128,:]`；N 仍由协作 MMA 完整消费，SFB 依旧全 N 组播到双方；CTA 内同样经 `.warpx4` 复制到四个 32-lane 分区。（正文以 M=256 为例陈述规则 [L222](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L222)，此处是同一规则在 Layout B 切分方式下的应用，属推论，待本地验证。）

**练习 3**：用"三问法"检查 block-scaled 链条里的 `tcgen05.cp` 这一步。

**答案**：三问（[L251](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L251)）逐条套用——①用哪个 CTA 资源：`tcgen05.cp.cta_group::2` 作用于 CTA 对，从各 CTA 本地 SMEM 拷进本地 TMEM；②映射到哪里：`.32x128b.warpx4` 把打包的 32-lane 基础布局写进 TMEM 并复制到四个 32-lane 分区，这个输出布局必须正是 block-scaled `tcgen05.mma` 期望读的 SFA/SFB 布局；③何时可消费：MMA 发起前必须确认 cp 已完成（经对应的完成/排序机制），否则会读到尚未就位的 scale factor。

## 5. 综合实践

**任务**：把本讲三个模块合成一份"四配置 + scale factor 放置"的推演报告，并尽可能与书图互相印证。

1. **整理一份完整脚本**：把 4.2.4 的四个映射函数与断言、4.3.4 的 `sf_placement_cg2_m256` 合并，再补两个草图函数，一次性输出全部推演结果：
   - `sketch_cg1_m64()`（4.2.4 已给）；
   - `sketch_cg2_m128(N=16)`：按 4.2.2 的表格，对 even/odd 各打印两行——"lanes 0-63 ← 本 CTA 64 行 × N 低半（n < 8）"与"lanes 64-127 ← 本 CTA 64 行 × N 高半（n ≥ 8）"；
   - 对 `cg1_m128` 与 `cg2_m256` 各打印一行 lane 占用摘要（如 `cta_group::1 M=128: lane m = row m，全 128 根占用`）。
2. **与书图对照**：打开 [img/mma_cg1_m128.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg1_m128.svg)、[mma_cg1_m64.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg1_m64.svg)、[mma_cg2_m256.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg2_m256.svg)、[mma_cg2_m128.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg2_m128.svg)、[mma_block_scaled.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_block_scaled.svg)，逐张核对你的草图/放置表与图中橙色（TMEM 累加器）、斜纹空带、SFA/SFB 子块标注是否一致；不一致时回到正文公式找原因。
3. **（可选，需 Blackwell GPU 与 tvm.tirx 环境）**：本仓库正文中的内核尚无 block-scaled/`cta_group::2` 的 TIRx 完整示例（相关用法在后续 GEMM Step 8 与 tirx-kernels 参考仓库展开）。若暂无 GPU，就用第 1、2 步的源码推演作为本讲实践成果，并明确记录"待本地验证"。
4. **回答一个串联问题**（写进报告结尾）：若把 `cta_group::1` M=128 内核原样改成 `cta_group::2` M=256，epilogue 的 `tcgen05.ld` 需要相应改什么？——提示：累加器从"一块 TMEM、恒等映射"变成"两块 TMEM、M 连续对半"，每个 CTA 的读回 warp 只需按映射读取本 CTA那份 128 行（lane 窗口规则见下一讲），且读回形状必须与 Layout A 兼容（[L207](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L207)）。

**预期成果**：一份能跑通的 `mma_layouts.py`（或明确的"待本地验证"清单）、五张图与推演输出的对照记录、以及对第 4 问的文字回答。

## 6. 本讲小结

- `cta_group` 选择 MMA 的资源范围：`::1` 只用当前 CTA 的 SMEM/TMEM，`::2` 用 cluster 内 `%cluster_ctarank` 仅差最低位的 CTA 对；它不改变单线程发起语义，但对端 CTA 必须保持活跃。
- 累加器 TMEM 布局由 `cta_group`、M、A 是否 dense、是否 `.ws` 四个选择共同决定；本讲覆盖四种 dense 非 `.ws` 配置：cg1 M=128 恒等映射、cg1 M=64 Layout F（四组 16 行、Lane 步距 32、对齐 0/16 互补）、cg2 M=256 Layout A（M 连续对半切）、cg2 M=128 Layout B（M 对半 + N 对折进 Lane 轴）。
- M=64 的 Layout F 只用每区一半 lane，剩下的互补位置正好放第二块独立 tile，两块 tile 可共用同一批 TMEM 列；四个 32-lane 区对应硬件数据通路的 `warp-rank % 4`，与 epilogue 各 warp 读自己 32-lane 窗口的方式呼应。
- block-scaled MMA 的 SFA/SFB 放置跟随 M/N 切分：SFA 沿 M 分片（各 CTA 只持自己算的那段行），SFB 完整 N 组播到 CTA 对双方（经 SMEM 组播 + `tcgen05.cp.cta_group::2` 进各自 TMEM）；每 CTA 内再经 `.warpx4` 复制到四个 32-lane 分区。
- 串联 `tcgen05.cp`/`mma`/`ld` 的三个条件：指向正确的 CTA 或 CTA 对、生产者输出布局等于消费者期望布局、消费者先走完成与排序机制；口诀是三问——用谁的资源、映射到哪、何时可消费。

## 7. 下一步学习建议

- **下一讲 u7-l3（TMEM 分配生命周期与 lane 访问窗口）**：本讲一直在说"累加器写进 TMEM 某坐标"，但 TMEM 的列是怎么分配出来的？`tcgen05.alloc`/`dealloc`/`relinquish_alloc_permit` 的调用顺序与大小限制、每个 warp 能访问哪个 32-lane 窗口，是本讲 Layout F"四个 32-lane 区"的直接后续。
- **u7-l4（tcgen05.ld/st）**：本讲反复强调"`tcgen05.ld` 必须用兼容的地址与 load 形状"，shape/repeat 因子与 `wait::ld`/`wait::st` 就在那里展开。
- **u8-l1/l2（mbarrier 与 phase）**：本讲的"完成与排序机制"一词的具体协议。
- **向前看 u13-l2（GEMM Step 8）**：`cta_group::2` 在真实 TIRx 内核中的完整用法——A/B 切分、DSMEM 互访、跨 CTA 屏障，会把本讲的 Layout A 与模块三的放置规则全部用上。
- **PTX ISA 文档的 `tcgen05.mma` 一节**：Layout A/B/C/D/F 的官方定义与 structured-sparse（Layout C）、`.ws` 形式的映射，是本讲四种配置之外的两个分支。
