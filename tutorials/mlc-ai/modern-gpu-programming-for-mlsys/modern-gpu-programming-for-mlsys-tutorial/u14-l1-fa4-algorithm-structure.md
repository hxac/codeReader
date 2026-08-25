# FA4 算法结构与 tile 数据流

## 1. 本讲目标

本讲是 Flash Attention 4（FA4）系列的第一讲，只回答三个问题：

1. **在线 softmax 的分块递推公式是什么？** 为什么分块处理 K/V 后仍然能得到与标准 attention 完全一致的结果？FA4 的「条件 rescaling」延迟了什么、又凭什么不改变结果？
2. **FA4 的 tile 数据流长什么样？** `QKᵀ MMA → softmax → PV MMA` 这条链上，每一步读什么、写什么、数据躺在哪个存储空间（GMEM/SMEM/TMEM/寄存器）？
3. **FA2 → FA3 → FA4 改了什么、没改什么？** 三代 Flash Attention 的差别是算法差别还是硬件映射差别？

学完本讲，你应该能独立写出 FA4 主循环的伪代码（带存储空间标注），并能说出其中哪些步骤在 GEMM 内核中根本不存在。本讲**不**深入 warp 角色与屏障协议（u14-l2）、softmax 数值细节（u14-l3）、TMEM 布局复用（u14-l4）等主题，只在必要处预告。

## 2. 前置知识

本讲默认你已读过前置讲义，以下概念会直接使用：

- **Attention 是什么**：给定查询矩阵 `Q`、键矩阵 `K`、值矩阵 `V`（每个注意力头的形状都是「序列长度 × 头维度 \(d\)」），计算 \(O=\operatorname{softmax}(QK^{\top}/\sqrt{d})V\)。`QKᵀ` 得到查询与键之间的注意力分数，逐行 softmax 变成权重，再加权求和 `V` 得到输出。
- **softmax 的数值稳定技巧**：指数函数爆炸得很快，直接对原始分数取 `exp` 会溢出。标准做法是先减去该行最大值再取指数——分子分母同乘一个常数，归一化结果不变。
- **GEMM 主线（单元十一~十三）**：TMA 搬 tile、`tcgen05.mma` 做矩阵乘、TMEM 存累加器、warp specialization 拆角色、mbarrier 交接。FA4 用的全部是这些机制，只是把它们接成了一条**不同的计算链**。
- **Roofline 结论（u3-l1）**：attention 的算术强度落在拐点的哪一侧，几乎完全由「中间张量（score 矩阵）是否落盘」决定。本讲会从算法侧解释这句话。
- **三次 Tensor Core 布局演进（单元五）**：Ampere 的寄存器 fragment → Hopper 的 wgmma SMEM 描述符 → Blackwell 的 TMEM 累加器。FA2/FA3/FA4 的分野正是踩着这三代硬件走的。

如果你对「分块累加」已经很熟（GEMM Step 2 的 K 循环），那么在线 softmax 对你来说只有一个新知识点：**换参考值时旧状态要乘一个换算系数**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [chapter_flash_attention/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md) | FA4 章正文。本讲主要精读前 200 行：`Algorithm Structure`（算法推导与伪代码）、`Tile Primitive Data Flow`（tile 数据路径）；后续小节属于 u14-l2 ~ u14-l6 |
| [img/scripts/gen_flash_attention_pipeline.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py) | 用 matplotlib 生成 FA4 流水线时间线图（`img/flash_attention_pipeline_v2.png`）。脚本里逐块写死了 TMA 加载顺序、MMA 发起顺序、softmax/校正事件，是从内核源码提炼出的「一图流」，是本讲读图实践的素材 |
| `zh/chapter_flash_attention/index.md` | 上述正文的中文镜像，路径为英文路径加 `zh/` 前缀 |

另有两个外部引用（正文给出，非本仓库文件）：章节代码节选自 [tirx-kernels 仓库的 `flash_attention4.py`](https://github.com/mlc-ai/tirx-kernels/blob/5be39749e7dfd2c4bdae9b4d396f8ec35af07126/tirx_kernels/attention/flash_attention4.py)，算法阈值等设定出自 [FA4 论文](https://arxiv.org/abs/2603.05451)。

## 4. 核心概念与源码讲解

### 4.1 在线 softmax：分块递推与条件 rescaling

#### 4.1.1 概念说明

自注意力有一个隐蔽的内存炸弹：对一个序列长度为 \(L\) 的头，score 矩阵 \(S=QK^{\top}\) 是 \(L\times L\) 的，fp32 下要占 \(4L^2\) 字节。\(L=4096\) 时一个头就是 64 MiB，根本放不进片上存储；如果把它写到 GMEM、再读回来做 softmax 和第二个矩阵乘，这部分中间流量随序列长度**平方增长**。这正是 u3-l1 里「naive attention 与 flash attention 算术强度天差地别」的根源。

FlashAttention 的解法：**一次只处理一个查询块，把 K/V 按块流进来，每行的 softmax 状态用三个标量/向量携带**。score 块一旦被消费就丢弃，完整 的 \(S\) 矩阵从头到尾不出现在 GMEM。代价是：softmax 不能「一眼看全整行」了，最大值和分母都必须**增量维护**——这就是在线（online）softmax。

FA4 在此之上加了一个工程优化：**条件 rescaling**。基本在线 softmax 每遇到更大的行最大值就立刻换参考值、立刻重缩放旧状态；FA4 先看新旧参考值的差距，差距不大就**继续用旧参考值**，把重缩放推迟掉。正文把这三个逐行状态记为 `row_max`（指数参考 \(r_i\)）、`row_sum`（\(\ell_i\)）、`O`（\(o_i\)）。

#### 4.1.2 核心流程

先写出「一次性」的逐行 softmax。固定查询行 \(i\)，记：

\[ s_{ij}=q_i\cdot k_j,\qquad m_i^{\max}=\max_j s_{ij} \]

以精确最大值为指数参考，未归一化的权重、分母、加权和、最终输出为：

\[ p_{ij}=\exp\!\left(\frac{s_{ij}-m_i^{\max}}{\sqrt d}\right),\qquad
\ell_i=\sum_j p_{ij},\qquad
o_i=\sum_j p_{ij}v_j,\qquad
O_i=\frac{o_i}{\ell_i} \]

分块后，每行只保留三元组 \((r_i,\ \ell_i,\ o_i)\)，其中 \(r_i\) 是当前使用的指数参考（不一定等于 \(m_i^{\max}\)）。处理新块时：

1. 算出本块行最大 \(m_{\mathrm{block}}\)，候选参考 \(r_c=\max(r_{\mathrm{old}},\ m_{\mathrm{block}})\)。
2. 实现里用 base-2 指数，令 \(\alpha=\log_2(e)/\sqrt d\)（代码变量 `scale_log2`），则 \(\exp\!\big((s-m)/\sqrt d\big)=2^{(s-m)\alpha}\)。
3. 定义带符号差距 \(\delta=(r_{\mathrm{old}}-r_c)\,\alpha\le 0\)（候选不小于旧值，故 \(\delta\) 非正；\(-\delta\) 是候选超出旧值的量，以 2 的指数计）。
4. **阈值判定**（论文取 \(\tau=\log_2 256=8\)）：
   - \(\delta\ge -8\)：保留旧参考，`acc_scale = 1`，旧状态无需换算；
   - \(\delta < -8\)：采用候选参考，`acc_scale = exp2(delta)`。

换参考为什么需要一个系数？因为参考值变了，此前所有以旧参考为基准累积的指数都要补上同一个因子：

\[ \underbrace{e^{(s-r_c)/\sqrt d}}_{\text{新基准}}
=\underbrace{e^{(s-r_{\mathrm{old}})/\sqrt d}}_{\text{已累积}}\cdot
\underbrace{e^{(r_{\mathrm{old}}-r_c)/\sqrt d}}_{a_{\mathrm{scale}}},\qquad
a_{\mathrm{scale}}=2^{\delta} \]

于是每块的递推为：

\[ \ell_i \leftarrow \ell_i\cdot a_{\mathrm{scale}}+\textstyle\sum_{j\in\text{block}}p_{ij},\qquad
o_i \leftarrow o_i\cdot a_{\mathrm{scale}}+\sum_{j\in\text{block}}p_{ij}v_j,\qquad
r_i\leftarrow r_{\text{new}} \]

阈值 8 的含义：容忍旧状态与新的「真实最大」之间最多差 \(2^8=256\) 倍——中间量的指数增长被有界地放宽，换来的是校正路径少跑很多趟。首块特殊：没有旧状态，直接采用 `candidate_max` 且 `acc_scale = 1`。

一个边界情形值得单独记：若某行到目前为止一个有效分数都没遇到（causal 掩码下会出现），\(r_{\mathrm{new}}\) 是 \(-\infty\)，直接算 \(S-(-\infty)\) 会得到 \(-\infty-(-\infty)\) 这种未定义形式。实现里用 `row_max_safe = 0 if new_ref == -inf else new_ref` 兜底：被掩码的分数 \(s=-\infty\) 使指数值为 \(2^{-\infty}=0\)，\(P\) 全零，旧状态不被清掉。

最后一点：把自然指数改写成 base-2 只是代数变形，本身并不解除指数路径的吞吐瓶颈。FA4 进一步把指数求值**拆到两条执行路径**——一部分元素走硬件 `exp2`，另一部分走 FP32 FMA 评估的三次多项式（实现里的 `ex2_emulation_2`），让两类运算单元并行干活。这改变的是「指数怎么算」，不是上面的递推公式。

#### 4.1.3 源码精读

正文的算法推导集中在 `Algorithm Structure` 一节：

- [chapter_flash_attention/index.md:24-56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L24-L56)：说明 \(L\times L\) score 矩阵占 \(4L^2\) 字节、无法驻留片上，然后从 \(q_i,k_j,v_j\) 出发逐步定义 \(s_{ij}\)、\(m_i^{\max}\)、\(p_{ij}\)、\(\ell_i\)、\(o_i\)、\(O_i\)，并引出分块后逐行保留的三个状态（`row_max`/`row_sum`/`O`）。
- [chapter_flash_attention/index.md:58-76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L58-L76)：定义 `scale_log2`（\(\alpha\)）、候选参考 \(r_c\)、差距 `delta`（\(\delta\)），以及阈值 \(\tau=8\) 的取舍说明——256 倍封顶，在「少做 rescale」与「限制指数增长」之间取平衡。
- [chapter_flash_attention/index.md:78-102](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L78-L102)：推导换算系数 \(a_{\mathrm{scale}}=2^\delta\)，并把三个逐行状态映射到代码变量 `row_max`/`row_sum`/`O`；随后列出三种情形（首块 / `delta >= -8` / `delta < -8`）。注意正文特别强调：`row_max` 名字里带 max，但在阈值允许时可以暂时落后于精确最大值 \(m_i^{\max}\)。
- [chapter_flash_attention/index.md:104-155](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L104-L155)：完整的核心伪代码（单个查询块的主循环）。这段伪代码**刻意忽略**了 warp 角色与流水线重叠——真实内核做的是同样的数学，只是把这些步骤摊到不同角色上交错执行。其中 `all(acc_scale == 1)` 是「本 warp 拥有的 32 行全部无需重缩放」的简写，真实内核按 WG2 里每个 warp 各自的 32 行分别判断。
- [chapter_flash_attention/index.md:157-169](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L157-L169)：`-inf` 边界情形的 `row_max_safe` 处理；指数双路径（硬件 `exp2` + FMA 多项式）的说明；以及 S、P、O 三类 tile 的驻留位置（S 在 TMEM 由 QKᵀ MMA 写、P 由 softmax 从 TMEM 读出算好再写回、O 是 PV MMA 在 TMEM 里的累加器）。
- [chapter_flash_attention/index.md:270-282](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L270-L282)：读码约定表，给出 `rescale_threshold`（当前 8.0）、`scale_log2`、`acc_scale` 等反复出现的变量名的权威定义，`acc_scale` 被明确标注为「softmax 本地用来更新旧 `row_sum`，同时传给 WG2 去重缩放 TMEM 里的旧 `O`」。

#### 4.1.4 代码实践

在线 softmax 的递推公式完全可以在 CPU 上用 NumPy 验证，不需要 GPU。下面的脚本（**示例代码**，非项目原有文件）把伪代码逐行翻译成向量化实现，并对照一次性 softmax：

```python
# fa4_online_softmax.py —— 示例代码：分块在线 softmax 与一次性 softmax 对照
import numpy as np

def attention_reference(Q, K, V):
    """一次性 softmax 的参考实现。"""
    S = Q @ K.T / np.sqrt(Q.shape[-1])
    P = np.exp(S - S.max(axis=-1, keepdims=True))
    return (P @ V) / P.sum(axis=-1, keepdims=True)

def attention_online(Q, K, V, blk_n=128, rescale_threshold=8.0, stats=None):
    """按伪代码实现的分块在线 softmax；stats 收集 rescale 触发情况。"""
    L, d = Q.shape
    scale_log2 = np.log2(np.e) / np.sqrt(d)          # alpha
    row_max = np.full(L, -np.inf)                    # r_i
    row_sum = np.zeros(L)                            # l_i
    O = np.zeros((L, V.shape[-1]))                   # o_i
    first_block = True
    for k0 in range(0, K.shape[0], blk_n):
        S = Q @ K[k0:k0+blk_n].T                     # ① QK^T MMA -> S
        candidate_max = np.maximum(row_max, S.max(axis=-1))
        if first_block:
            new_ref = candidate_max; acc_scale = np.ones(L)
        else:
            delta = (row_max - candidate_max) * scale_log2   # delta <= 0
            keep = delta >= -rescale_threshold
            new_ref = np.where(keep, row_max, candidate_max)
            acc_scale = np.where(keep, 1.0, np.exp2(delta))
        if stats is not None:
            stats["rescaled_rows"] += int((acc_scale != 1.0).sum())
            stats["p_max"] = max(stats["p_max"], 0.0)
        row_max_safe = np.where(np.isfinite(new_ref), new_ref, 0.0)
        P = np.exp2((S - row_max_safe[:, None]) * scale_log2)  # ② softmax -> P
        if stats is not None:
            stats["p_max"] = max(stats["p_max"], float(P.max()))
        row_sum = row_sum * acc_scale + P.sum(axis=-1)
        O = O * acc_scale[:, None] + P @ V[k0:k0+blk_n]        # ③ PV MMA -> O
        row_max, first_block = new_ref, False
    return O / row_sum[:, None]                      # ④ epilogue 归一化

rng = np.random.default_rng(0)
L, d = 512, 128
Q, K, V = rng.standard_normal((L, d)), rng.standard_normal((L, d)), rng.standard_normal((L, d))

ref = attention_reference(Q, K, V)
for thr in (0.0, 8.0, 1e9):                          # 阈值 0 = 每次都换参考值
    st = {"rescaled_rows": 0, "p_max": 0.0}
    out = attention_online(Q, K, V, rescale_threshold=thr, stats=st)
    print(f"threshold={thr:>8}: max|diff|={np.abs(out-ref).max():.3e}, "
          f"rescaled_rows={st['rescaled_rows']}, p_max={st['p_max']:.3f}")

# 构造幅度逐块递增的 K，逼出 rescale：第 b 块乘 (1 + 3b)
K_amp = K.copy().reshape(4, L // 4, d)
for b in range(4):
    K_amp[b] *= (1 + 3 * b)
K_amp = K_amp.reshape(L, d)
for thr in (8.0, 1e9):
    st = {"rescaled_rows": 0, "p_max": 0.0}
    out = attention_online(Q, K_amp, V, rescale_threshold=thr, stats=st)
    print(f"amplified, threshold={thr:>8}: rescaled_rows={st['rescaled_rows']}, "
          f"p_max={st['p_max']:.3e}")
```

1. **实践目标**：验证「分块 + 在线 softmax + 条件 rescaling」在任何阈值下都还原标准 attention，并观察阈值对 rescale 次数与中间量幅度的影响。
2. **操作步骤**：保存为 `fa4_online_softmax.py`，`pip install numpy` 后运行 `python fa4_online_softmax.py`。
3. **需要观察的现象**：三行常规数据输出中 `max|diff|` 都应在 \(10^{-13}\) 量级（fp64）；`rescaled_rows` 为 0 或极小（随机数据的分数块间最大值差距很小，\(\delta\) 远不到 -8）；`p_max` 随阈值增大而增大。放大数据的两组里，threshold=8 会触发若干行重缩放，threshold=1e9（永不换参考）则一次不触发、`p_max` 显著变大。
4. **预期结果**：所有配置的最终 `O` 都与参考一致——**条件 rescaling 是性能优化，不改变数学结果**；阈值越大，校正越少、中间量越肥。具体触发行数随随机种子浮动，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把阈值从 8 改成 0（即每遇到更大的块最大值就立刻换参考），最终输出会变吗？中间量的范围会变吗？

> **答案**：最终输出不变（在浮点舍入误差内）——两种策略只是选择不同的指数参考，归一化时分子分母同乘的常数相互抵消。中间量会变：阈值 0 时每块 \(p_{ij}\le 1\)、`row_sum` 单调且温和；阈值大时 \(p_{ij}\) 可达 \(2^{|\delta|}\)（阈值 8 即 256 倍），`row_sum`/`O` 的幅度随之增大，极端情况下会溢出（fp32 远早于 fp64）。

**练习 2**：为什么 \(\delta\) 恒有 \(\delta\le 0\)？如果允许 \(\delta>0\) 会发生什么？

> **答案**：因为 \(r_c=\max(r_{\mathrm{old}},m_{\mathrm{block}})\ge r_{\mathrm{old}}\)，候选参考永远不会更小。若参考值变小（\(\delta>0\)），\(a_{\mathrm{scale}}=2^\delta>1\) 会放大旧状态——这在数学上成立，但会让已累积的 `row_sum`/`O` 无界膨胀，毫无收益；参考值只升不降正是数值稳定的关键，阈值只是放宽了「升」的及时性。

**练习 3**：伪代码里 `row_max_safe = 0 if new_ref == -inf else new_ref` 这一行防的是什么？去掉它（直接用 `new_ref`）在非 causal 场景下会出问题吗？

> **答案**：防的是因果掩码下「某行至今没有有效分数」时 \(-\infty-(-\infty)\) 产生 NaN。用它兜底后，被掩码的 \(s=-\infty\) 代入 \(2^{(s-0)\alpha}=2^{-\infty}=0\)，即本块对这些行的 \(P\)、`row_sum`、`O` 贡献全为零，已累积的旧状态原样保留。非 causal 场景下每行必有有效分数，`new_ref` 不会是 \(-\infty\)，去掉这行不影响正确性，但 causal 路径（u14-l6）会算出 NaN。

### 4.2 tile 数据流：TMEM 是两个 MMA 之间的传送带

#### 4.2.1 概念说明

算法落实到内核，每个 K/V 块会产生或更新三类 tile：score 块 `S`、权重块 `P`、输出累加块 `O`。**它们驻留在哪，决定了整条数据路径**。FA4 的选择是让 TMEM 充当两个 MMA 之间的传送带：QKᵀ MMA 把 `S` 写进 TMEM，softmax 把 `S` 从 TMEM 读进寄存器、算出 `P` 再写回 TMEM，PV MMA 从 TMEM 读 `P`、从 SMEM 读 `V`，把结果累加进 TMEM 里的 `O`。

这样做的前提是 u7 系列讲过的 Blackwell 能力：`tcgen05.mma` 的操作数可以一边来自 SMEM 描述符、一边来自 TMEM（PV MMA 的 `P` 正是 TMEM 操作数），而 `tcgen05.ld/st` 是 warp 集体的 TMEM↔寄存器通道。GEMM 内核里 TMEM 只装一个「长寿命累加器」；FA4 里 TMEM 同时装着中间产物 `S`/`P` 和累加器 `O`，三者还要复用同一块物理列——那是 u14-l4 的主题。

#### 4.2.2 核心流程

一个 K/V 块的完整数据路径（沿正文逐行展开）：

```text
Q, K:  GMEM --TMA load--> SMEM --QKᵀ MMA--> S in TMEM
S:     TMEM --tcgen05.ld--> registers --softmax--> P in registers
P:     registers --TMEM store--> P in TMEM
V:     GMEM --TMA load--> V in SMEM
P, V:  P in TMEM + V in SMEM --PV MMA--> O in TMEM
按需:   O in TMEM --tcgen05.ld--> registers --rescale/TMEM store--> O in TMEM
收尾:   O in TMEM --tcgen05.ld--> registers --normalize/cast--> O in SMEM --TMA store--> O in GMEM
```

把它对齐到 TIRx tile 操作与硬件指令：

| 阶段 | 搬什么/算什么 | TIRx 原语 | 硬件路径 |
|------|--------------|-----------|----------|
| 加载 Q/K/V | GMEM tile → SMEM tile | `Tx.copy_async(..., dispatch="tma_auto")` | TMA load |
| QKᵀ MMA | SMEM 的 Q、K → TMEM 的 S | `Tx.warp.gemm_async(..., dispatch="tcgen05")` | `tcgen05.mma` |
| softmax 读 | TMEM 的 S → warpgroup 寄存器 tile | `Tx.wg.copy_async(reg, tmem)` | `tcgen05.ld` |
| softmax 写 | 寄存器的 P → fp16 TMEM 视图 | `Tx.wg.copy_async(tmem_as_f16, reg)` | TMEM store + `tcgen05.wait.st()` |
| PV MMA | TMEM 的 P + SMEM 的 V → TMEM 的 O | `Tx.warp.gemm_async(..., dispatch="tcgen05")` | 带 TMEM 操作数的 `tcgen05.mma` |
| 校正 | TMEM 的 O → 寄存器 → TMEM 的 O | TMEM 读回、寄存器乘法、TMEM store | `tcgen05.ld` / TMEM store |
| epilogue | 最终 O → 寄存器 → SMEM → GMEM | TMEM 读回、`Tx.copy`、TMA store | `tcgen05.ld` + TMA store |

与 GEMM 对照，FA4 在两个 MMA **中间**插进了 softmax：`S` 必须从 TMEM 读进寄存器、`P` 必须再写回 TMEM；参考值变化时还要为 `O` 多跑一趟 TMEM→寄存器→TMEM 的重缩放。这些额外的存储往返正是后续讲义里布局与屏障要保护的对象。

时间线上（见 `img/flash_attention_pipeline_v2.png`），内核同时保持**两个 Q stage** 在飞：每个 stage 是一个可复用槽，含 SMEM 里的 Q 缓冲、TMEM 里对应的 `S`/`P`/`O` 区域和保护它们的屏障。WG0 跑 stage 0 的 softmax、WG1 跑 stage 1 的 softmax、WG3 发 TMA 与 MMA、WG2 做校正与 epilogue。MMA warp 的发起顺序是「先 QKᵀ 引导，之后当前 V 的 PV MMA 与下一个 K 的 QKᵀ MMA 交错」，softmax 写完 `P` 的一部分列后 PV MMA 就能启动（`P` 按 `K_SPLIT` 分成两组列先后放行）。图里还有一个值得注意的细节：K/V 块是**从最后一个块倒序流式处理**的（先 `K[n-1]` 后 `K[n-2]`……以 `V[0]` 收尾）——由于分块求和可交换、在线 softmax 与块处理顺序无关，倒序不影响数学结果，是内核调度上的选择。

#### 4.2.3 源码精读

- [chapter_flash_attention/index.md:171-200](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L171-L200)：`Tile Primitive Data Flow` 一节全文——上面那张数据路径文字图与「阶段 → TIRx 原语 → 硬件路径」对照表都出自这里，末尾一句话点明与 GEMM 的本质差别（softmax 插在两个 MMA 之间，`S` 要读出、`P` 要写回，参考值变化还要为 `O` 加一趟往返）。
- [chapter_flash_attention/index.md:163-169](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L163-L169)：S、P、O 三类 tile 的定义与驻留位置；`O` 在参考值变化时「读出→寄存器重缩放→写回」的额外路径也在此说明。
- [chapter_flash_attention/index.md:204-206](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L204-L206)：两个 Q stage 在飞的定义（SMEM 的 Q 缓冲 + TMEM 的 S/P/O 区域 + 屏障构成一个复用槽），以及四个 warpgroup 的一句分工预告（细节归 u14-l2）。
- [img/scripts/gen_flash_attention_pipeline.py:94-101](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py#L94-L101)：时间线图的六行泳道——WG3 warp1（TMA load）、WG3 warp0（MMA issue）、WG0（softmax Q stage 0）、WG1（softmax Q stage 1）、WG2（correction/epilogue）、WG3 warp2（TMA store）。
- [img/scripts/gen_flash_attention_pipeline.py:106-118](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py#L106-L118)：TMA 加载顺序的数据来源——注释写明「来自源码：Q0, K_last, Q1, V_last, 然后 K/V 流」，后续块依次是 `K[n-2]`、`V[n-2]`、`K[n-3]`……即倒序流式。
- [img/scripts/gen_flash_attention_pipeline.py:120-134](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py#L120-L134)：MMA 发起顺序——先两条 QKᵀ 引导（Q0、Q1 各配 `K[n-1]`），之后 PV（当前 V）与 QKᵀ（下一个 K）交错，收尾是 `P0 @ V[0]`、`P1 @ V[0]`。
- [img/scripts/gen_flash_attention_pipeline.py:136-148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py#L136-L148)：softmax 与校正事件——WG0/WG1 各自的「softmax Sx / 写入 Px」、WG2 在时间线**开头**的「pre-release O0/O1」（首块 PV MMA 直接初始化 `O`，无需等待校正，对应约定表里的 `should_accumulate` 标志）、中段的「按需重缩放 O0/O1」、以及收尾的「normalize O0/O1」和 TMA store。

#### 4.2.4 代码实践（本讲主线实践）

1. **实践目标**：不看正文伪代码，凭本节的数据路径自己写出 FA4 主循环伪代码，标注每步的存储空间与「GEMM 中不存在」的步骤，再用正文与时间线图核对。

2. **操作步骤**：
   - 先只看 [chapter_flash_attention/index.md:175-184](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L175-L184) 的数据路径文字图，在纸上写出主循环：KV 块迭代 → 两个 MMA 与 softmax 的先后顺序 → 每步读/写的存储空间。
   - 打开 `img/flash_attention_pipeline_v2.png`（或 [img/scripts/gen_flash_attention_pipeline.py:120-134](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py#L120-L134) 的代码）核对：MMA warp 是「QKᵀ 与 PV 交错」而不是「先算完所有 QKᵀ」；K/V 是倒序流。
   - 最后与 [chapter_flash_attention/index.md:104-153](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L104-L153) 的伪代码对照，检查数学步骤有无遗漏。

3. **需要观察的现象**：你写的循环里应该出现「TMEM→寄存器→TMEM」这种 GEMM 里没有的往返；PV MMA 的两个操作数应分别来自 TMEM（P）和 SMEM（V）；`O` 的初始化只发生在第一个 KV 块。

4. **预期结果**：参考答案（**示例伪代码**，非 causal 路径，`⭐` 标注 GEMM 中不存在的步骤）：

```text
# 初始化（每 CTA 一次）
SMEM:   Q_smem[2], K_smem[3], V_smem[3]        # 两个 Q stage + 三级 K/V 流水
SMEM:   sScale 邮箱（acc_scale / row_sum）      # ⭐ softmax 与校正组之间传统计量
TMEM:   S_region[2], P_region[2], O_region[2]  # 每个 Q stage 一套
寄存器: row_max, row_sum（每线程一行）⭐；s_chunk_buf（每线程 128 个 fp32 分数）⭐

for n in n_blocks-1 .. 0:                      # K/V 块倒序流式处理
    ① TMA load:  GMEM 的 K[n], V[n] → SMEM 空闲 stage   （WG3 warp1 发起）
    ② QKᵀ MMA:   SMEM 的 Q + SMEM 的 K[n] → TMEM 的 S   （WG3 warp0 发起）
    ③ softmax ⭐: TMEM 的 S --tcgen05.ld--> 寄存器
                  选参考值/acc_scale，P = exp2((S-ref)*scale_log2)
                  寄存器的 P --TMEM store--> TMEM（按 K_SPLIT 分两组放行）
                  row_sum = row_sum*acc_scale + rowsum(P)
    ④ PV MMA:     TMEM 的 P + SMEM 的 V[n] → TMEM 的 O  （首块初始化，其后累加）
                  若 acc_scale ≠ 1：O 需先换算到新参考值
    ⑤ 校正 ⭐:    （按需）WG2 把 TMEM 的旧 O 读进寄存器，乘 acc_scale 写回 TMEM
epilogue ⭐:      O / row_sum → fp16 → SMEM 暂存 → TMA store 写回 GMEM
```

GEMM 中不存在的步骤：③ 整个 softmax（含指数计算与 `S`/`P` 的 TMEM 往返）、⑤ 旧 `O` 的条件重缩放、epilogue 里除以 `row_sum` 的归一化；若走 causal 路径，还有「`S` 的被掩码位置填 \(-\infty\)」（见伪代码 `if causal:` 分支）。GEMM 的 epilogue 只做 dtype 转换与搬运，不做逐行除法。

### 4.2.5 小练习与答案

**练习 1**：PV MMA 的两个操作数各在哪个存储空间？为什么它可以这样组织，而 GEMM Step 1~9 里两个操作数都在 SMEM？

> **答案**：`P` 在 TMEM、`V` 在 SMEM。`tcgen05.mma` 允许操作数经 SMEM 描述符或 TMEM 地址给出（u7-l1），FA4 正是把 softmax 的产物 `P` 直接留在 TMEM 供 PV MMA 消费，省掉「`P` 写回 SMEM」这一跳。GEMM 的两个操作数都由 TMA 搬进 SMEM，没有中间计算环节插在两个 MMA 之间，自然都放 SMEM。

**练习 2**：FA4 每个 KV 块至少发生几次 TMEM↔寄存器往返？分别为了什么？

> **答案**：至少三次读加两次写：读 `S`（softmax 输入）、写 `P`（softmax 输出，fp16 视图）、读 `O`（仅当需要重缩放，即条件 rescaling 要省掉的那次）、epilogue 阶段读最终 `O`。此外 softmax 内部 `tcgen05.ld` 分四个 32 列块、TMEM store 分批进行，但都属于同一逻辑往返。对比 GEMM：TMEM 只在 epilogue 被读一次，没有中途往返——这就是「softmax 插在两个 MMA 之间」的直接代价，也是条件 rescaling 想优化掉的靶子。

**练习 3**：时间线图开头 WG2 的「pre-release O0/O1」是什么意思？为什么它能出现在流水线最前面？

> **答案**：它是 WG2 提前把两个 `O` 槽的使用权归还给流水线（对应校正与 softmax 之间关于 `O` 可写性的交接）。第一个 KV 块的 PV MMA 以 `should_accumulate=False` 初始化 `O`（见约定表 [chapter_flash_attention/index.md:277](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L277)），旧 `O` 里没有需要保护或换算的状态，所以 WG2 一开始就能放行；从第二个块起才需要「按需重缩放」。

### 4.3 FA 版本演进：同一算法的三次硬件映射

#### 4.3.1 概念说明

三代 Flash Attention 的**算法内核没有变**：分块处理 K/V + 在线 softmax + 不落盘 score 矩阵。变的是这套算法到 GPU 的**映射**——哪个线程块/ warp 干哪件事、数据走哪条硬件路径、靠什么重叠。正文用一句话概括这条演进线：FA2 改进线程块与 warp 间的工作划分；FA3 在 Hopper 上用 TMA、WGMMA 和 warp specialization 把数据搬运、两个 MMA 和 softmax 交错起来；FA4 面向 Blackwell，围绕 `tcgen05` 和 TMEM 重组流水线。

这条演进线与单元五的 Tensor Core 三代演进严丝合缝：FA2 时代的矩阵乘落在 Ampere 的 `mma.sync` + 寄存器 fragment 上，FA3 用上 Hopper 的 wgmma（SMEM 描述符直读），FA4 则吃到了 Blackwell 的 `tcgen05` + TMEM 累加器。**硬件给出什么原语，算法就换一种方式铺到硬件上**——这是本手册反复出现的主题（scope/layout/dispatch 三要素）在「算法族」层面的重现。

#### 4.3.2 核心流程

| 维度 | FA2 | FA3 | FA4 |
|------|-----|-----|-----|
| 目标硬件 | Ampere | Hopper | Blackwell（sm_100a） |
| 矩阵乘路径 | `mma.sync`，操作数与累加器都在寄存器 fragment | `wgmma`，B 经 SMEM 描述符直读，累加器仍在寄存器 | `tcgen05`，累加器进 TMEM，`P` 可作 TMEM 操作数 |
| 数据搬运 | 常规访存指令 | TMA | TMA（load 与 store 都走引擎） |
| 组织方式 | 改进 thread block / warp 间的作业划分 | warp specialization，把数据搬运、两个 MMA、softmax 交错 | 多角色 + 两个 Q stage 在飞，softmax/校正/epilogue 各有专职 warpgroup |
| softmax 的新矛盾 | — | 与 MMA 争执行资源 | `S`/`P` 的 TMEM 往返、`O` 的重缩放成为新瓶颈 → 条件 rescaling + 指数双路径 |

FA4 相对前代新增的两个「算法层」改动，都针对 softmax 这一步：

1. **条件 rescaling**：参考值差距在阈值内就不换，直接省掉一趟 `O` 的 TMEM→寄存器→TMEM 往返（4.1 的推导）。
2. **指数双路径**：硬件 `exp2` 与 FMA 多项式近似分担指数求值，避免单一执行单元限流。

#### 4.3.3 源码精读

- [chapter_flash_attention/index.md:4-10](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L4-L10)：章首 Overview 的三条要点——分块 + 在线 softmax 避免完整 score 矩阵写 GMEM；FA4 为 Blackwell 重组流水线（分角色执行 QKᵀ MMA、softmax、PV MMA 与输出校正，TMEM 在其间承载 `S`/`P`/`O`）；条件 rescaling 减少 `O` 的 TMEM 往返、硬件 `exp2` 与 FMA 多项式分担指数工作。
- [chapter_flash_attention/index.md:12-22](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L12-L22)：attention 公式与动机（直接实现会物化完整 score 矩阵）、三代版本差异的那句总结，以及 FA4 计算链的提法——「CUDA core 把 `S` 变成未归一化权重 tile `P`，PV MMA 用 `P` 和 `V` 更新输出累加器 `O`」。
- [chapter_flash_attention/index.md:159-161](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L159-L161)：指数双路径的动机——base-2 改写只是代数变形，若所有元素都挤硬件 `exp2`，该单元仍是限流点；论文中部分元素走硬件 `exp2`、部分走 FP32 FMA 三次多项式（实现为 `ex2_emulation_2`），两类单元并发。

#### 4.3.4 代码实践

1. **实践目标**：从时间线图里找出 FA4「多角色 + 交错」映射的三个证据，并尝试本地重绘这张图。

2. **操作步骤**：
   - 打开仓库中已生成的 [img/flash_attention_pipeline_v2.png](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/flash_attention_pipeline_v2.png)，对照泳道找证据：(a) MMA warp 是否交错发起 QKᵀ 与 PV；(b) 两个 Q stage 的 softmax 是否在不同 warpgroup 并行；(c) K/V 是否倒序加载。
   - （可选）重绘：脚本会覆盖 `img/` 下的入库图片，**不要直接在仓库里跑**。先复制到临时目录再运行：

     ```bash
     mkdir -p /tmp/fa4fig/scripts
     cp img/scripts/gen_flash_attention_pipeline.py /tmp/fa4fig/scripts/
     cd /tmp/fa4fig/scripts
     python gen_flash_attention_pipeline.py          # 输出 ../flash_attention_pipeline_v2.png
     ```

     依赖仅为 `matplotlib`（见 [img/scripts/README.md:23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L23)）；默认 `--lang en` 输出 PNG，不需要 CJK 字体。输出路径写死为 `../`，见 [img/scripts/gen_flash_attention_pipeline.py:167-168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py#L167-L168)，这正是建议在临时目录运行的原因。

3. **需要观察的现象**：重绘出的图与入库图一致；改脚本里 `mma_blocks` 列表（如删掉交错的 QKᵀ 块）能直观看到流水线「断流」。
4. **预期结果**：三个证据都能在图中定位——(a) `PV MMA P0 @ V[n-1]` 夹在 `QKᵀ Q0 @ K[n-2]` 与 `QKᵀ Q1 @ K[n-2]` 之间；(b) 「softmax S0」在 WG0 泳道、「softmax S1」在 WG1 泳道且时间上错开一格；(c) 加载顺序为 Q0, K[n-1], Q1, V[n-1], K[n-2], V[n-2], ……。运行结果**待本地验证**（无 matplotlib 时直接看入库图片即可）。

#### 4.3.5 小练习与答案

**练习 1**：FA2/FA3/FA4 都没有改变哪个算法成分？它们争相改变的是什么？

> **答案**：都没改变「分块处理 K/V + 在线 softmax 维护逐行状态 + 不物化完整 score 矩阵」这一算法骨架。争相改变的是映射：作业划分（FA2）、TMA/WGMMA/warp specialization 带来的交错（FA3）、围绕 `tcgen05`+TMEM 的多角色流水线与条件 rescaling（FA4）。

**练习 2**：为什么说「FA4 的两个算法层改动都是被 softmax 逼出来的」？

> **答案**：FA4 把累加器与中间产物都放进 TMEM 后，softmax 成为两个 MMA 之间唯一必须经过寄存器的环节：`S` 要读出、`P` 要写回，参考值变化时 `O` 还要额外往返一趟。条件 rescaling 直接砍掉「按块重缩放 `O`」的多数往返；指数双路径则解除 softmax 内部 `exp2` 单元的吞吐限制。两者的靶子都是这条被拉长的 softmax 路径。

**练习 3**：如果下一代硬件允许 warp 直接对 TMEM 中的数据做逐元素超越函数（等价于「softmax 不出 TMEM」），本讲数据路径里的哪些往返会消失？

> **答案**：`S` 的 TMEM→寄存器 读回与 `P` 的寄存器→TMEM 写回都消失，softmax 在 TMEM 内完成；`O` 的校正若也能原地做，则 ⑤ 的往返同样消失——数据路径坍缩回 GEMM 形态（load→MMA→epilogue）。这是一个假想练习，用于检验你是否真的理解「FA4 的额外数据移动全部来自 softmax 插在两个 MMA 之间」。

## 5. 综合实践

**任务：把 4.1 的 NumPy 验证脚本扩展成一个带 causal 掩码与条件 rescaling 的「算法层 FA4 模拟器」，再为它写出 tile 级伪代码。**

1. 在 4.1.4 脚本的 `attention_online` 里加入 causal 掩码：处理第 \(n\) 块时，把 `S` 中「key 位置 > query 位置」的元素置为 \(-\infty\)（对应伪代码的 `if causal: S[masked positions] = -inf`）。注意这会激活 `row_max_safe` 的 \(-\infty\) 兜底分支——验证完全被掩码的行输出为 0 而不是 NaN。
2. 让脚本统计并打印：每个 KV 块触发重缩放的行数、`P` 的最大值、以及**首块之后才出现有效分数的行数**（右下对齐 causal 下每行第一个有效块不同）。
3. 对照 4.2.4 的参考伪代码，把你的 Python 实现里每一步标注上它在真实内核中的执行角色与存储空间（如「② QKᵀ MMA：WG3 warp0 发起，SMEM+SMEM→TMEM」）。
4. 写一段 200 字左右的说明：如果把 `rescale_threshold` 设为 0（严格在线 softmax）或 \(\infty\)（从不重缩放），你的模拟器在数值上各发生什么？这与正文中「阈值 8 平衡了重缩放次数与指数增长上限」的说法如何对应？

**验收标准**：causal 版本与 `torch.nn.functional.scaled_dot_product_attention`（或手写的带掩码参考实现，先 `float()` 升 fp32）在 `rtol=1e-5` 内一致；倒序处理 KV 块（`range(n_blocks-1, -1, -1)`）时结果不变——这验证了「块处理顺序与在线 softmax 无关」，也解释了时间线图里倒序加载为什么是合法的。

## 6. 本讲小结

- 完整 score 矩阵 \(L\times L\) 是 attention 的内存炸弹；FlashAttention 用「分块处理 K/V + 逐行在线 softmax」把它消灭，每行只携带 \((r_i,\ell_i,o_i)\) 三个状态。
- 换指数参考需要给旧状态乘 \(a_{\mathrm{scale}}=2^\delta\)；FA4 用阈值 8（\(\delta\ge -8\) 就不换）推迟重缩放，减少 `O` 的 TMEM 往返——这是性能优化，不改变数学结果。
- FA4 的 tile 数据流：QKᵀ MMA 把 `S` 写入 TMEM，softmax 把 `S` 读进寄存器算出 `P` 再写回 TMEM，PV MMA 以 TMEM 的 `P` + SMEM 的 `V` 累加进 TMEM 的 `O`，epilogue 归一化后经 SMEM 由 TMA 写回。
- TMEM 成为两个 MMA 之间的传送带；「softmax 插在中间」带来的 TMEM↔寄存器往返是 FA4 相对 GEMM 的全部额外数据移动，也是条件 rescaling 与指数双路径两个优化的靶子。
- FA2/FA3/FA4 算法不变、映射三级跳（作业划分 → TMA/WGMMA/交错 → `tcgen05`+TMEM 多角色流水线），与 Ampere/Hopper/Blackwell 的 Tensor Core 演进一一对应。
- 内核保持两个 Q stage 在飞，MMA warp 交错发起 QKᵀ 与 PV，K/V 块倒序流式处理（块序与在线 softmax 的数学无关）。

## 7. 下一步学习建议

下一讲 **u14-l2「FA4 的 warp 角色与 barrier 协议」**把本讲数据路径里的每个阶段落实到线程：WG0~WG3 的具体分工、`setmaxnreg` 的寄存器再分配、以及 `s_ready`/`p_o_rescale`/`p_ready_2`/`o_ready` 等屏障各自的等待者、到达数与完成条件（正文的 `Warp Roles and Scope` 与 `Barrier Roles and Completion Conditions` 两节）。建议先自己读一遍 [chapter_flash_attention/index.md:296-311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L296-L311) 的屏障表，试着用 u8-l1/u13-l1 的「屏障类型由完成者决定」原则解释为什么 `p_o_rescale` 需要 256 次到达而 `s_ready` 只需要 1 次。之后再按顺序进入 u14-l3（softmax 数值）、u14-l4（TMEM 布局复用）、u14-l5（rescaling 与 writeback）、u14-l6（causal/GQA/调度）。
