# u14-l4 PV MMA 与 TMEM 布局复用

## 1. 本讲目标

上一讲（u14-l3）结束时，softmax 已经把 fp32 分数 `S` 读进寄存器、算出 fp16 权重 `P` 并写回 TMEM。本讲从这一点继续，读完 FA4 计算链的最后一段矩阵乘，并回答三个问题：

1. **PV MMA 怎么算**：`P` 在 TMEM、`V` 在 SMEM、`O` 在 TMEM，这条"跨存储空间"的乘加如何发起、分几段、各等什么。
2. **S/P/O 怎么挤进 512 列 TMEM**：为什么没有给 `P` 单独预留区域，fp16 视图列与 32-bit 物理列如何换算，六个区域 `S0/S1/P0/P1/O0/O1` 各占哪些物理列。
3. **复用为什么不出错**：`P` 直接覆写 `S` 的后半段，哪些等待与屏障保证"先读完再覆写、先写完再读、先消费再重写"。

学完后你应能独立完成书中章末练习 3：推导六个区域的物理列区间、指出重叠、说出防护屏障。

## 2. 前置知识

本讲是 FA4 单元第四讲，直接建立在以下已建立的认知上（不再重复推导）：

- **FA4 计算链（u14-l1 / u14-l3）**：`QKᵀ MMA → softmax → PV MMA`，三个中间量 `S`（分数）、`P`（未归一化权重）、`O`（输出累加器）都驻留 TMEM；softmax 由 WG0/WG1 执行，MMA 由 WG3 warp 0 发起，校正由 WG2 执行（u14-l2）。
- **TMEM 物理结构（u2-l2 / u7-l3）**：每个 CTA 可用的 Tensor Memory 是 128 行 × 最多 512 列的二维空间，每个"格子"固定 32 bit；按列动态分配。
- **tcgen05.ld/st 与 16-bit 打包（u7-l4）**：`.pack::16b / .unpack::16b` 把相邻两个 16-bit 片段装进/拆出一个 32-bit 寄存器；写 TMEM 后必须 `tcgen05.wait::st`，读 TMEM 后用数据前必须 `wait::ld`。
- **屏障类型由完成者决定（u8-l1 / u14-l2）**：`TMABar` 靠字节计数、`TCGen05Bar` 靠 `tcgen05.commit` 挂接的硬件完成通知、普通 `MBarrier` 靠线程到达计数。
- **术语速查**：`q_stage`/`i_q` 是当前 Q 流水线 stage（0 或 1）；`MMA_N=128` 是分数 tile 与 TMEM 区域的基础宽度；`MMA_K=16` 是 PV MMA 每个内层 K 步消耗的归约位置数；`K_SPLIT` 是 PV MMA 第一段消费的列数；`should_accumulate` 决定当前 PV MMA 是初始化还是累加 `O`；`phase_tmem` 是与当前 `P`/`O` 迭代相关的屏障相位奇偶。这些约定集中列在章节的读码约定表中：[chapter_flash_attention/index.md:274-283](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L274-L283)。

一个本讲反复用到的换算先写在这里：**fp16 视图列 \( c \) 对应 32-bit 物理列 \( \lfloor c/2 \rfloor \)、槽位 \( c \bmod 2 \)**；反过来，物理列 \( p \) 的两个 fp16 槽是 \( 2p \) 与 \( 2p+1 \)。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲使用方式 |
|---|---|---|
| `chapter_flash_attention/index.md` | FA4 章正文（节选自 tirx-kernels 的 `flash_attention4.py`） | 精读其中 *PV MMA*（约 L478–532）与 *TMEM Layout and Reuse*（约 L534–622）两节，屏障总表在 L296–309 |
| `img/scripts/gen_tmem_layout.py` | 用 matplotlib 生成书中 TMEM 布局图 `img/tmem_layout_v3.png` 的脚本 | 作为区间推导的"机器核对"依据，也是本讲实践的改编素材 |
| `img/scripts/README.md` | 全部图表脚本的运行说明 | 实践中重生成布局图时引用 |

书中明确说明：章节代码摘录自 [tirx-kernels 仓库的 `flash_attention4.py`](https://github.com/mlc-ai/tirx-kernels/blob/5be39749e7dfd2c4bdae9b4d396f8ec35af07126/tirx_kernels/attention/flash_attention4.py)，本仓库只含教材正文与图表脚本，不含内核源码本身（见 [chapter_flash_attention/index.md:270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L270)）。

## 4. 核心概念与源码讲解

### 4.1 PV MMA：P 在 TMEM、V 在 SMEM、O 在 TMEM

#### 4.1.1 概念说明

PV MMA 完成在线 softmax 递推中的 \( o_i \leftarrow o_i \cdot a_{\text{scale}} + P_{\text{block}}V_{\text{block}} \) 这一步的矩阵部分：

\[ O \leftarrow O + P\,V \]

三个操作数各居一个位置：

- \( P \)：128×128 的 fp16 未归一化权重，**在 TMEM**（softmax 刚写回的那份）；
- \( V \)：当前 K/V 块的 128×d（d=HEAD_DIM=128）fp16 值矩阵，**在 SMEM**（TMA 装载）；
- \( O \)：128×d 的 fp32 累加器，**在 TMEM**。

这与全书 GEMM 的 \( D = AB^{\top} \) 约定同构：\( P \) 扮演 A，传入的 V 切片就是转置意义上的 B 操作数（代码里 `transB=True` 声明这一关系），乘出的 128×d tile 累加进 \( O \)。

**为什么 P 必须先写回 TMEM**（u14-l3 的遗留问题）：PV MMA 的 `P` 操作数需要 MMA 可读的 TMEM 布局，它无法消费散落在 softmax 各线程私有寄存器里的值——`P_region` 这个 fp16 视图就是把"每线程一行"的 softmax 结果变成下一个 MMA 期望的矩阵操作数，正文在 softmax 一节末尾专门回答了这一点：[chapter_flash_attention/index.md:474-476](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L474-L476)。

第一个 K/V 块没有旧 \( O \)，用 `should_accumulate=false` 直接初始化；后续块 `should_accumulate=true` 累加。发射前 WG2 必须完成（或确认不需要）对旧 \( O \) 的 rescale——这就是 `p_o_rescale` 屏障串联的第二重条件。

#### 4.1.2 核心流程

PV MMA 的归约维是当前块的 128 个 key/value 位置。内核不把它当一段发完，而是在 `K_SPLIT` 处切成两段：

```text
K_SPLIT = (4 if causal else 6) * MMA_K        # causal: 64，non-causal: 96

等待: kv_load.full (V 完整入 SMEM)
等待: p_o_rescale  (P[:, :K_SPLIT] 已写回 TMEM 且 O 槽可初始化/累加)
发射: O += P[:, :K_SPLIT]  @ V[:K_SPLIT, :]    accum = should_accumulate

等待: p_ready_2    (P[:, K_SPLIT:128] 已写回 TMEM)
发射: O += P[:, K_SPLIT:128] @ V[K_SPLIT:, :]  accum = True（恒为真）

最后一个 K/V 块完成后: o_ready 把最终 O 交给 epilogue
```

分段动机：softmax 写 `P` 是按四个 32 列 chunk 逐段进行的。如果 128 个归约位置作为一个整体交接，PV MMA 必须等全部四个 chunk 落盘才能开工；分段后，第一段 MMA 在 softmax 还在做剩余 TMEM store 时就已开跑，压缩了 Tensor Core 等待 `P` 写回的时间。causal 路径分成 64+64，non-causal 分成 96+32，正文把这称作"按工况调优的切分点"（regime-tuned）：[chapter_flash_attention/index.md:525-532](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L525-L532)。

#### 4.1.3 源码精读

PV MMA 的完整发射代码在章节 *PV MMA* 小节：[chapter_flash_attention/index.md:489-515](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L489-L515)。关键片段（摘自正文）：

```python
K_SPLIT = T.meta_var((4 if is_causal else 6) * MMA_K)

# First segment: P[:, :K_SPLIT] and the matching rows of V.
Tx.warp.gemm_async(
    O_region[SMEM_PIPE_DEPTH_Q + i_q, :, :],
    P_region[i_q, 1, :, 0:K_SPLIT],
    V_smem[kv_stage, 0:K_SPLIT, 0:HEAD_DIM],
    transB=True,
    accum=should_accumulate,
    dispatch="tcgen05",
    cta_group=CTA_GROUP,
)

p_ready_2.wait(i_q, phase_tmem)
Tx.warp.gemm_async(
    O_region[SMEM_PIPE_DEPTH_Q + i_q, :, :],
    P_region[i_q, 1, :, K_SPLIT:BLK_N],
    V_smem[kv_stage, K_SPLIT:BLK_N, 0:HEAD_DIM],
    transB=True,
    accum=True,
    dispatch="tcgen05",
    cta_group=CTA_GROUP,
)
```

逐行读法：

- 目的地都是 `O_region[SMEM_PIPE_DEPTH_Q + i_q, :, :]`。`O_region` 是 `S_region` 的别名（下一模块展开），`SMEM_PIPE_DEPTH_Q=2`，所以两个 Q stage 的累加器分别落在第 2、3 块，即物理列 `[256,384)` 与 `[384,512)`。
- `P_region[i_q, 1, :, 0:K_SPLIT]` 与 `P_region[i_q, 1, :, K_SPLIT:BLK_N]`：两段各取 `P` 的一段列，`1` 这个下标是 fp16 视图的"高半区"选择器（4.3 节解释）。
- 第一段的 `accum=should_accumulate`、第二段的 `accum=True`：即使第一个 K/V 块，第一段也已经写入了部分和，第二段必须累加。
- 两段之间只夹一句 `p_ready_2.wait(i_q, phase_tmem)`——第二段**不再**等 `kv_load.full`，因为该屏障已经证明完整 V tile 就绪，一次证明对两段都有效。

正文的四要素框（scope/layout/dispatch/handoff）总结如下：[chapter_flash_attention/index.md:517-521](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L517-L521)

> - Scope: WG3 warp 0 执行 warp 级 tile 操作。
> - Layout: `P` in TMEM + V in SMEM → `O` in TMEM。
> - Dispatch: `tcgen05`，带 TMEM 操作数。
> - Handoff: 第一段等 `kv_load.full` 与 `p_o_rescale`；第二段另加 `p_ready_2`；最后一个 K/V 块后 `o_ready` 交给 epilogue。

等待语义的原文说明（谁证明什么）见 [chapter_flash_attention/index.md:523](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L523)；两段式与 softmax 写回的重叠关系在 *What Each MMA Waits For* 一节有图解版：[chapter_flash_attention/index.md:628-640](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L628-L640)。

#### 4.1.4 代码实践

**实践目标**：通过回答三个"为什么"，确认你理解了两段式设计，而不是只记住了代码形状。

**操作步骤**（源码阅读型，无需 GPU）：

1. 通读 [chapter_flash_attention/index.md:478-532](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L478-L532)，对照上面的逐行读法。
2. 书面回答：
   - a) 第二段为什么永远 `accum=True`？
   - b) 第二段为什么不再等 `kv_load.full`？
   - c) 如果去掉 `p_ready_2`、把两段合并成一条消费全部 128 列的 `Tx.warp.gemm_async`，性能上失去了什么？正确性上需要改哪一处等待？

**需要观察的现象**：无运行现象；检验标准是你的答案能否在正文中找到原句支撑。

**预期结果**（对照正文核对）：

- a) 即使第一个 K/V 块，第一段 MMA 也已经把部分和写进 `O`，第二段必须在此基础上累加（正文 L523："The second segment always accumulates"）。
- b) `kv_load.full` 证明的是**完整** V tile 在 SMEM，一次成立即对两段都有效（正文 L638）。
- c) 失去的是重叠：合并后 PV MMA 必须等 `P` 全部四个 chunk 写回 TMEM 才能开始（正文 L532）；正确性上则应改成等一个覆盖全部 128 列的单一就绪条件（相当于把 `p_o_rescale` 与 `p_ready_2` 合并成一次交接）。

#### 4.1.5 小练习与答案

**练习 1**：`MMA_K=16`。分别计算 causal 与 non-causal 路径的 `K_SPLIT`，并写出两段各消费多少个 `MMA_K` 步。

**答案**：`K_SPLIT = (4 if is_causal else 6) * MMA_K`，故 causal 为 64（4 步 + 4 步，64+64），non-causal 为 96（6 步 + 2 步，96+32）。

**练习 2**：PV MMA 的三个操作数分别在哪里？哪一个是"从 TMEM 读"的？

**答案**：`P` 在 TMEM（fp16 视图 `P_region[i_q,1,:,:]`）、`V` 在 SMEM（`V_smem[kv_stage]`）、`O` 在 TMEM（`O_region[2+i_q,:,:]`）。`P` 是 TMEM 操作数；`O` 是 TMEM 累加器目的地。`dispatch="tcgen05"` 的一条指令同时覆盖"SMEM 读 B、TMEM 读 A、TMEM 写 D"三条通路。

**练习 3**：`phase_tmem` 在 `p_ready_2.wait(i_q, phase_tmem)` 里起什么作用？

**答案**：它是该 Q stage 的 TMEM 相关屏障在第 `i_q` 轮迭代所期望的相位奇偶。两道屏障按 K/V 块逐轮复用，硬件每完成一相自动翻转，等待方必须用相位区分"这一轮"与"上一轮"的完成（u8-l2 的相位复用规则在 FA4 里的实例）。

### 4.2 fp16 列打包：tmem 与 tmem_as_f16 双视图

#### 4.2.1 概念说明

TMEM 的物理格子固定 32 bit，但同一行物理存储可以按不同元素宽度"看"：按 fp32 看是 512 列，按 fp16 看是 1024 列。FA4 对同一份 512 物理列的分配建立**两个 buffer**：

- `tmem`：形状 (128, 512) 的 fp32 视图，用于寻址 `S` 与 `O`；
- `tmem_as_f16`：形状 (128, 1024) 的 fp16 视图，用于寻址 `P`。

这不是两次分配，而是同一批位的两套索引方案。`S`/`O` 是 fp32（MMA 累加精度），`P` 是 fp16（PV MMA 的输入精度、且体积减半），双视图让两类数据能在同一物理 TMEM 上各用各的坐标语言。

#### 4.2.2 核心流程

每行比特数守恒：

\[ 512 \times 32 = 1024 \times 16 = 16384 \text{ bit} \]

物理列 \( p \)（一个 32-bit 单元）与 fp16 视图列的双射：

\[ \text{fp16 视图列 } c \;\Longleftrightarrow\; \text{物理列 } \lfloor c/2 \rfloor,\ \text{槽位 } c \bmod 2 \]

这正是 u7-l4 讲过的 `.pack::16b / .unpack::16b` 在地址侧的对应物：相邻两个 16-bit 值共享一个 32-bit 物理列。于是 128 个 fp16 恰好填满 64 个物理列——这个"减半"是下一模块布局复用得以成立的前提。

#### 4.2.3 源码精读

创建双视图的源码（正文摘录自内核）：[chapter_flash_attention/index.md:544-553](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L544-L553)

```python
tmem_pool = T.TMEMPool(
    pool, total_cols=N_COLS_TMEM, cta_group=CTA_GROUP, tmem_addr=tmem_addr,
    alloc_warp=12, dealloc_warp=0,
)
tmem = tmem_pool.alloc((128, N_COLS_TMEM), "float32")
tmem_pool.move_base_to(0)
tmem_as_f16 = tmem_pool.alloc((128, N_COLS_TMEM * 2), "float16")
tmem_pool.commit()
```

关键一行是 `tmem_pool.move_base_to(0)`：它把分配游标拨回物理列 0，使 `tmem_as_f16` 与 `tmem` 从**同一物理位置**开始（[chapter_flash_attention/index.md:542](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L542)）。比特守恒与单元图在正文中给出：[chapter_flash_attention/index.md:557-571](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L557-L571)

```text
physical column p (32 bits)
┌────────────────┬────────────────┐
│ fp16 slot 2p   │ fp16 slot 2p+1 │
└────────────────┴────────────────┘
```

即 `tmem[:, p]` 把整个单元当一个 fp32 值寻址，`tmem_as_f16[:, 2p]` 与 `tmem_as_f16[:, 2p+1]` 寻址它的两个 fp16 值。在阶段表中，softmax 写回一行明确走 fp16 视图并跟一句 `tcgen05.wait::st()`：[chapter_flash_attention/index.md:190-198](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L190-L198)。

#### 4.2.4 代码实践

**实践目标**：把"fp16 视图列 → 物理列/槽位"变成肌肉记忆，并用脚本自查。

**操作步骤**：

1. 手算 fp16 视图列 \( c = 0,1,2,\dots,7 \) 对应的 (物理列, 槽位)。
2. 写一个 5 行 Python 函数复算（示例代码，非项目原有代码）：

```python
def f16_view_to_phys(c):
    return c // 2, c % 2          # (物理列, 槽位)

for c in range(8):
    print(c, f16_view_to_phys(c))
```

3. 再验证反向：物理列 \( p=5 \) 的两个 fp16 槽是哪两列？

**需要观察的现象**：打印表中物理列每两个连续的 \( c \) 共享一个值，槽位在 0/1 间交替。

**预期结果**：\( c=0\to(0,0),\ c=1\to(0,1),\ c=2\to(1,0),\dots,\ c=7\to(3,1) \)；反向 \( p=5 \) 对应 \( c=10 \) 与 \( c=11 \)。纯算术推导，无需 GPU，可本地运行核对。

#### 4.2.5 小练习与答案

**练习 1**：`tmem_as_f16` 的形状是 (128, 1024)。为什么它的第二维是 `tmem` 的两倍而不是四倍？

**答案**：fp16 是 16 bit，恰为 32 bit 物理单元的一半，故每物理列容纳 2 个 fp16，列数翻倍（512×2=1024）。若是 fp8 才会翻四倍。

**练习 2**：一个 128 行 × 128 列的 fp16 tile 占多少物理列？

**答案**：128 个 fp16 打包成 64 个物理列（每列 2 个），再乘 128 行方向不变（TMEM 每行独立编列），即 64 个物理列。这就是 4.3 节 `P` 只覆盖 `S` 后半 64 列的原因。

**练习 3**：`tmem[:, 100]` 与 `tmem_as_f16[:, 200]`、`tmem_as_f16[:, 201]` 之间是什么关系？

**答案**：三者指向同一物理单元（物理列 100 的 32 bit）。`tmem[:,100]` 把它整体当一个 fp32；后两者分别寻址它的低/高 16-bit 槽。

### 4.3 TMEM 布局复用：S/P/O 的物理列区间推导

#### 4.3.1 概念说明

先算预算。FA4 为每个 CTA 分配 128 行 × 512 物理列的 TMEM。两个 Q stage 各需要：

- 一个 128 列的 fp32 分数 tile `S`；
- 一个 128 列的 fp32 输出累加器 `O`。

\[ 2 \times (128_{\,S} + 128_{\,O}) = 512 \text{ 列} \]

恰好填满全部分配——**没有任何余量给 `P` 留独立区域**。解决方案不是扩大 TMEM（硬件上限就是 512 列），而是**时序复用（temporal reuse）**：`P` 写进 `S` 的后半段物理列。正文明确强调"There is no separate region reserved for `P`. The overlap is temporal reuse; `S` and `P` do not coexist in those bits"：[chapter_flash_attention/index.md:616](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L616)。预算推导见 [chapter_flash_attention/index.md:534-540](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L534-L540)。

#### 4.3.2 核心流程

两个视图各自 `rearrange` 成带 stage 索引的区域：

```python
S_region = T.meta_var(tmem.rearrange("m (s n) -> s m n", n=MMA_N))
O_region = S_region
P_region = T.meta_var(
    tmem_as_f16.rearrange("m (s two n) -> s two m n", two=2, n=MMA_N)
)
```

（摘自 [chapter_flash_attention/index.md:575-581](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L575-L581)，`MMA_N=128`。）

推导链条分三步：

**第一步（fp32 视图）**：(128, 512) 按 `"m (s n) -> s m n"`、`n=128` 切成 4 个 128 列块：

| 块索引 | 物理列 | 用途 |
|---|---|---|
| 0 | [0, 128) | `S_region[0]` = S0 |
| 1 | [128, 256) | `S_region[1]` = S1 |
| 2 | [256, 384) | `O_region[2+0]` = O0 |
| 3 | [384, 512) | `O_region[2+1]` = O1 |

`O_region` 直接别名 `S_region`，用块 `SMEM_PIPE_DEPTH_Q + i_q`（即 2、3）选累加器（[chapter_flash_attention/index.md:583](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L583)）。

**第二步（fp16 视图）**：(128, 1024) 按 `"m (s two n) -> s two m n"`、`two=2, n=128` 切：每个 `s` 块覆盖 256 个 fp16 列（= 128 物理列），再对半拆成低/高两个 128 元素半区（各 = 64 物理列）。内核用 `P_region[q_stage, 1, :, :]`（`two=1` 高半区）作为 `P` 的落点。

**第三步（套换算公式）**：以 `P0` 为例，设 \( n \) 为 tile 内逻辑列（正文原推导在 [chapter_flash_attention/index.md:585-593](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L585-L593)）：

```text
P_region[0, 1, :, n]
    -> tmem_as_f16[:, 128 + n]       # fp16 视图列起点 128
    -> 物理列 64 + n // 2            # 套 ⌊c/2⌋
```

即 `P0[:, 0]` 与 `P0[:, 1]` 共享物理列 64 的两个 16-bit 槽，`P0[:, 2]`/`P0[:, 3]` 占物理列 65……128 个 fp16 填满 `[64, 128)`。stage 1 的 fp16 起点是 \( 128 + 1\times 256 = 384 \)（推导在 [chapter_flash_attention/index.md:595-602](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L595-L602)），故 `P1` 落在 `[192, 256)`。

汇总成书中的区域表（[chapter_flash_attention/index.md:605-614](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L605-L614)，配图 `img/tmem_layout_v3.png`）：

| 区域 | 每行存的数据 | 物理列 | 备注 |
|---|---|---|---|
| S0 | 128 个 fp32 分数 | [0, 128) | |
| P0 | 128 个 fp16 权重 | [64, 128) | **复用 S0 后半** |
| S1 | 128 个 fp32 分数 | [128, 256) | |
| P1 | 128 个 fp16 权重 | [192, 256) | **复用 S1 后半** |
| O0 | 128 个 fp32 累加值 | [256, 384) | 独占 |
| O1 | 128 个 fp32 累加值 | [384, 512) | 独占 |

时间线（以 stage 0 为例）：QKᵀ MMA 先把完整 S0 写进 `[0,128)` → softmax 把整行 S0 装入寄存器 → softmax 把 128 个 fp16 P0 两两打包写进 `[64,128)`，**覆写掉后 64 个已消费的 fp32 分数**。第一半 `[0,64)` 的陈旧分数不再被读，只等下一轮 QKᵀ MMA 整区覆写。

#### 4.3.3 源码精读

图表脚本 `gen_tmem_layout.py` 把上表画成了书中的布局图，是"机器核对"版的事实来源。槽位坐标集中在 `main()` 里：[img/scripts/gen_tmem_layout.py:78-90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_layout.py#L78-L90)

```python
add_slot(ax, 0, 128, 1.35, "S0", ...)
add_slot(ax, 128, 256, 1.35, "S1", ...)
add_slot(ax, 64, 128, 0.63, "P0", ..., tr("phys 64-127\nf16 view 128-255", ...))
add_slot(ax, 192, 256, 0.63, "P1", ..., tr("phys 192-255\nf16 view 384-511", ...))
add_slot(ax, 256, 384, -0.08, "O0", ...)
add_slot(ax, 384, 512, -0.08, "O1", ...)
```

脚本内注释直接复述了换算规则：[img/scripts/gen_tmem_layout.py:83-86](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_layout.py#L83-L86) ——"P 通过 fp16 视图寻址……每个 128 列 fp16 tile 占 64 个物理 fp32 TMEM 列"；图注也点明 P0 别名 S0 的 64–127 列、P1 别名 S1 的 192–255 列：[img/scripts/gen_tmem_layout.py:92-103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_layout.py#L92-L103)。图形输出写出到 `../tmem_layout_v3.png`（或中文版 SVG）：[img/scripts/gen_tmem_layout.py:106-107](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_layout.py#L106-L107)。

另外注意：`S_region`/`O_region`/`P_region` 一旦定义，计算代码就全部用结构化索引（如 `S_region[q_stage, :, :]`）选区域，不再手算裸 TMEM 列号——正文以此收束本节：[chapter_flash_attention/index.md:622](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L622)。

#### 4.3.4 代码实践（本讲主实践：章末练习 3）

对应书中练习 3（[chapter_flash_attention/index.md:912](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L912)）：*利用 fp16 列到 32-bit 物理列的映射推导 S0/S1/P0/P1/O0/O1 的物理列区间，标出哪些区域重叠、哪道屏障防止过早读写。*

**实践目标**：不看书中表格，独立复现六个区间，并让脚本替你找重叠。

**操作步骤**：

1. 先在纸上按 4.3.2 的三步推导六个区间。
2. 用下面的核对脚本复算（示例代码，非项目原有代码；纯 Python，无需 GPU/tvm）：

```python
MMA_N = 128            # 章节: MMA_N = BLK_N = 128
N_COLS_TMEM = 512      # 章节: 每分配 512 物理列
Q_DEPTH = 2            # 章节: SMEM_PIPE_DEPTH_Q = 2 (Q pipeline depth 2)

# fp32 视图: 4 个 128 列块
S = {s: (s * MMA_N, (s + 1) * MMA_N) for s in range(Q_DEPTH)}
O = {s: ((Q_DEPTH + s) * MMA_N, (Q_DEPTH + s + 1) * MMA_N) for s in range(Q_DEPTH)}

# fp16 视图: stage s 高半区 -> fp16 列 [s*256+128, s*256+256) -> 物理列
def phys_of_f16(lo, hi):                       # 左闭右开
    return (lo // 2, (hi + 1) // 2)

P = {s: phys_of_f16(s * 2 * MMA_N + MMA_N, (s + 1) * 2 * MMA_N) for s in range(Q_DEPTH)}

def overlap(a, b):
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo < hi else None

for name, d in (("S", S), ("P", P), ("O", O)):
    for s, rng in d.items():
        print(f"{name}{s}: 物理列 {rng}")
for s in range(Q_DEPTH):
    print(f"P{s} 与 S{s} 重叠: {overlap(P[s], S[s])}")
```

3. 对照书中区域表（[chapter_flash_attention/index.md:607-614](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L607-L614)）与布局图脚本坐标（[img/scripts/gen_tmem_layout.py:80-90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_layout.py#L80-L90)）核对。
4. 对每个重叠，写出防护屏障（对照 4.4 节自查）。

**需要观察的现象**：脚本输出的区间与手推一致；唯一的重叠是 `P0⊂S0`、`P1⊂S1`（各为 S 的后 64 列），O0/O1 与任何区域都不重叠。

**预期结果**：S0=[0,128)、P0=[64,128)、S1=[128,256)、P1=[192,256)、O0=[256,384)、O1=[384,512)。此为确定性算术推导，本地运行即得；未在本讲环境中实际执行，输出格式以你本地运行为准。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `P_region` 的选择从 `two=1` 改成 `two=0`（低半区），`P0` 会落在哪个区间？布局还成立吗？

**答案**：`P_region[0,0,:,n] -> tmem_as_f16[:, n] -> 物理列 n//2`，即 `[0,64)`，覆盖 S0 的**前**半。布局依然成立（softmax 先整行读走 S，两个半区的分数在被覆写时都已是死数据），但书中实现与图表均按高半区（`two=1`）描述，改动需同步所有引用 `P_region[...,1,:,:]` 的读写两端。

**练习 2**：为什么 `O` 不参与这种复用、要独占 256 列？

**答案**：`O` 是长寿命累加器，跨全部 K/V 块存活（首块初始化、后续累加、必要时被 rescale、最后才被 epilogue 读走），与按块生灭的 `S`/`P` 生命周期不同步，没有安全的覆写窗口；且预算上 2×(S+O) 已占满 512 列，`O` 的 256 列正是靠 `P` 复用 `S` 才腾出来的。

**练习 3**：`O_region` 与 `S_region` 是什么关系？为什么可以这么写？

**答案**：`O_region = S_region` 是同一 fp32 视图的别名——二者都只是对同一 (128,512) buffer 的 `rearrange` 结果的不同命名。因为 `S` 用块 0/1、`O` 用块 2/3，索引不冲突，别名不会引入额外存储。

### 4.4 屏障防护：防止过早读写的完成信号

#### 4.4.1 概念说明

时序复用把"同一批位"的读写次序变成正确性关键。正文列出三条必须成立的顺序（[chapter_flash_attention/index.md:618](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L618)）：

1. softmax 必须**先把完整 `S` 读入寄存器**，`P` 才能覆写 `S` 的后半；
2. PV MMA 必须等到**对应的 `P` chunk 已写入**才能读；
3. 下一次 QKᵀ MMA 必须等到**当前 `P` 被消费完**才能再覆写该区域。

源码级的程序顺序本身不足以建立这些条件——异步引擎（Tensor Core、TMA）的进度对线程不可见（u8-l1 的核心前提），必须由完成信号补齐。这也呼应正文的总结："Most barriers that FA4 adds beyond GEMM surround softmax"——FA4 相比 GEMM 多出的屏障大多围绕 softmax，因为它把寄存器计算、`P` 的 TMEM 重写和 `O` 的可选 rescale 插在了两个 MMA 之间，每个边界都需要显式的就绪/归还信号（[chapter_flash_attention/index.md:665](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L665)）。

#### 4.4.2 核心流程

三条顺序要求对应的机制（正文原述见 [chapter_flash_attention/index.md:620](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L620)）：

| 顺序要求 | 机制 | 谁等谁 |
|---|---|---|
| ① S 写完才能读 | QKᵀ MMA 的 `tcgen05.commit` 把完成挂到 `s_ready`（TCGen05Bar） | softmax 等 `s_ready` 后才开始 `tcgen05.ld` 读 S |
| ①' S 读完整才能被 P 覆写 | softmax 的加载循环（4×32 列 `tcgen05.ld`）先于 P 存储循环；分数仅在 TMEM→寄存器加载完成后被使用 | softmax 自己的程序顺序 + 加载完成等待 |
| ② P 写完才能被 MMA 读 | softmax 写 P 后 `tcgen05.wait::st`，再 arrive `p_o_rescale` / `p_ready_2`；MMA 端 wait 同名屏障 | PV MMA 等 softmax 的到达 |
| ②' O 就绪才能初始化/累加 | WG2 完成 rescale（或确认不需要）后向 `p_o_rescale` 贡献另一半到达 | PV MMA 等 WG2 的到达 |
| ③ P 消费完才能被下轮 S 覆写 | WG3 warp 0 以**同一发射线程**的固定 `tcgen05` 序列先后发出 PV MMA 与下一个 QKᵀ MMA，lowering 必须保留其间所需的 `tcgen05` 依赖 | 隐式：同线程异步序列的依赖 |
| 最终 O 才能被 epilogue 读 | 最后一段 PV MMA 的完成经 `o_ready`（TCGen05Bar） | WG2 等 `o_ready` |

其中 `p_o_rescale` 是最容易被低估的一道：它是 `MBarrier`，期望 **256** 次到达——softmax 战组 128 线程存完前 `K_SPLIT` 列贡献 128 次，WG2 的 128 线程在把 `O` 准备好（首块为预先放行，后续块为 rescale 完成或确认无需）后贡献另外 128 次；一次 wait 同时证明"P 的前段就绪"与"O 槽可用"两件事（[chapter_flash_attention/index.md:640](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L640)）。`p_ready_2` 则只数 softmax 战组的 128 次、只放行第二段 MMA。

书中屏障总表把本讲涉及的四个条目的完成条件列得很清楚（[chapter_flash_attention/index.md:296-309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L296-L309)）：

| 屏障 | 类型/到达数 | 完成后什么变得安全 |
|---|---|---|
| `s_ready` | TCGen05Bar，1 次硬件通知 | softmax 可读 S 的 TMEM tile |
| `p_o_rescale` | MBarrier，256 次到达 | 第一段 PV MMA 可读 `P[:, 0:K_SPLIT]` 并初始化/累加 O |
| `p_ready_2` | MBarrier，128 次到达 | 第二段 PV MMA 可读 `P[:, K_SPLIT:128]` |
| `o_ready` | TCGen05Bar，1 次硬件通知 | epilogue 可读最终 O 累加器 |

#### 4.4.3 源码精读

**softmax 写 P 与两次交接**（摘自正文，[chapter_flash_attention/index.md:439-459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L439-L459)）：

```python
P_SPLIT_Q = T.meta_var(2 if is_causal else 3)
for i in T.unroll(P_SPLIT_Q):
    Tx.wg.copy_async(
        P_region[wg_id, 1, :, i * BLK_N // 4 : (i + 1) * BLK_N // 4],
        p_chunk[:, i * BLK_N // 4 : (i + 1) * BLK_N // 4],
    )
T.ptx.tcgen05.wait.st()
p_o_rescale.arrive(wg_id)

for i in T.unroll(4 - P_SPLIT_Q):
    Tx.wg.copy_async(
        P_region[wg_id, 1, :,
                 (P_SPLIT_Q + i) * BLK_N // 4 : (P_SPLIT_Q + i + 1) * BLK_N // 4],
        p_chunk[:,
                (P_SPLIT_Q + i) * BLK_N // 4 : (P_SPLIT_Q + i + 1) * BLK_N // 4],
    )
T.ptx.tcgen05.wait.st()
p_ready_2.arrive(wg_id)
```

读法：softmax 先写 `P_SPLIT_Q` 个 32 列 chunk（causal 2 个、non-causal 3 个，恰好凑足 `K_SPLIT` 列），`tcgen05.wait::st` 确认这些异步 TMEM store 落地后才 arrive `p_o_rescale`——保证 MMA 醒来时读到的 `P[:, :K_SPLIT]` 不是陈旧位。剩余 chunk 走同样节奏后 arrive `p_ready_2`。

**WG2 侧对 O 的放行**（摘自正文校正一节的控制流代码块 [chapter_flash_attention/index.md:736-748](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L736-L748)）：

```python
# The correction loop returns the other Q stage in its alternating protocol.
p_o_rescale.arrive(i_q)
softmax_corr.empty.arrive(1 - i_q)
```

即：即使某 warp 的 32 行全部无需 rescale、跳过了 TMEM→寄存器→TMEM 数据路径，它**仍然**要执行这些到达——跳过数据路径不跳过同步协议（正文 L750）。这两行出现在 [chapter_flash_attention/index.md:746-747](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L746-L747)。

**发射侧的固定序列**：MMA warp 把 PV MMA 与后续 QKᵀ MMA 作为同一线程的固定 `tcgen05` 序列发出（时间线上表现为 `score Q0*K[n-1] → score Q1*K[n-1] → value P0*V[n-1] → score Q0*K[n-2] → …` 的交错，见 [chapter_flash_attention/index.md:693-704](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L693-L704)），lowering 保留其间依赖，这就是顺序要求 ③ 的机制。

#### 4.4.4 代码实践

**实践目标**：把"复用/交接关系 → 防护机制"整理成一张可复查的防护矩阵，训练"看到重叠就问谁防护"的 reflex。

**操作步骤**（源码阅读型，无需 GPU）：

1. 逐行阅读屏障总表 [chapter_flash_attention/index.md:296-311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L296-L311) 与 *What Each MMA Waits For* 一节 [chapter_flash_attention/index.md:628-640](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L628-L640)。
2. 填写下面的矩阵（每行一条复用或交接）：

| 存储位的复用/交接 | 写者 | 读者 | 防护机制（屏障/等待） |
|---|---|---|---|
| S 区（QKᵀ 写 → softmax 读） | ？ | ？ | ？ |
| S 后半（softmax 写 P 覆写） | ？ | ？ | ？ |
| P 区（softmax 写 → PV MMA 读） | ？ | ？ | ？ |
| S 区再覆写（P 消费后 → 下一轮 QKᵀ） | ？ | ？ | ？ |
| O 区（PV MMA 累加 ↔ WG2 rescale） | ？ | ？ | ？ |
| O 区最终读（PV MMA → epilogue） | ？ | ？ | ？ |

**需要观察的现象**：每一行都能在正文中找到明确的"谁等待、谁到达"句子；没有任何一行只靠程序顺序防护（第 ③ 行是显式声明的同线程 `tcgen05` 依赖，属例外且由编译器保证）。

**预期结果**（对照核对）：S 行 = `s_ready`；S 后半 = softmax 先完成 4×32 列加载再写 P（加载完成等待 + 程序顺序）；P 行 = `wait::st` + `p_o_rescale`（前 K_SPLIT 列）/`p_ready_2`（其余列）；S 再覆写 = 同发射线程的固定 `tcgen05` 序列；O 累加/rescale = `p_o_rescale` 的 WG2 半 128 次到达；O 最终读 = `o_ready`。

#### 4.4.5 小练习与答案

**练习 1**：删掉 softmax 存 P 后的 `tcgen05.wait.st()`（两处都删），屏障到达照旧发生。会发生什么？

**答案**：`p_o_rescale` / `p_ready_2` 的 arrive 不再保证 TMEM store 已落地——MMA 可能在 `P` 的位还没写进去时就被放行去读，读到陈旧数据，产生**静默的数值错误**（大概率能通过编译、结果错）。这正是 u7-l4 "使用目的寄存器/依赖 TMEM 写入完成前须 wait" 纪律的实例。

**练习 2**：`p_o_rescale` 为什么是 256 次到达而不是 128 次？首块有什么特殊处理？

**答案**：它一次性合并两个独立条件：softmax 存完 `P[:, :K_SPLIT]`（128 次）与 WG2 把 `O` 准备好（128 次）。首块没有旧 `O` 可 rescale，WG2 预先贡献自己那 128 次放行，让第一段 PV MMA 直接以 `accum=false` 初始化 `O`（正文 L640、时间线的 `pre-release O0/O1` 事件）。

**练习 3**：`o_ready` 与 `p_o_rescale` 都涉及 `O`，二者分工是什么？

**答案**：`p_o_rescale` 管的是**稳态循环内**的 O 槽就绪（初始化或 rescale 完成后才允许下一段 PV MMA 累加）；`o_ready` 管的是**最终** PV MMA 段完成之后、epilogue 读走最终 `O` 之前的那道门。前者由线程到达计数完成，后者由 `tcgen05.commit` 挂接的硬件通知完成。

## 5. 综合实践

**任务**：制作一份「FA4 TMEM 布局与屏障防护推演报告」，把本讲四个模块串起来。无需 Blackwell GPU，全部可本地完成。

1. **区间与重叠计算**：运行 4.3.4 的核对脚本，把六个区域的物理列区间与重叠关系整理成表；把书中区域表（[chapter_flash_attention/index.md:607-614](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L607-L614)）与图脚本坐标（[img/scripts/gen_tmem_layout.py:80-90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_layout.py#L80-L90)）作为两份独立参照交叉核对，三处必须一致。
2. **重生成并改编布局图**：按 [img/scripts/README.md:19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L19) 的说明，在 `img/scripts` 目录运行 `python gen_tmem_layout.py`（依赖 matplotlib、numpy，输出 `../tmem_layout_v3.png`，会覆盖仓库图片——建议先 `cp` 到临时目录再改编运行，注意脚本输出是相对路径 `../tmem_layout_v3.png`）。改编目标：给 `add_slot` 增加一个半透明覆盖矩形，把 P0/P1 与 S0/S1 的重叠区（[64,128) 与 [192,256)）显式描出来，直观呈现"时序复用"。
3. **防护矩阵**：完成 4.4.4 的六行矩阵，并在每行标注对应的正文出处（行号区间）。
4. **推演问答**：最后回答两个检验性问题——(a) 若把 `K_SPLIT` 从 96 改成 64（non-causal 路径），`p_o_rescale` 与 `p_ready_2` 各自放行的列段如何变化？(b) `P` 改用低半区（`two=0`）后，报告中哪些数字需要同步修改？

**验收标准**：三处区间来源一致、重叠只出现在 S/P 之间、矩阵每行有正文出处、两个问答能自圆其说（(a) 第一段放行列段变为 `[0,64)`、第二段 `[64,128)`，即 softmax 首批只需写 2 个 chunk；(b) P0→[0,64)、P1→[128,192)，重叠改落在 S 的前半）。

## 6. 本讲小结

- **PV MMA** 是 \( O \leftarrow O + PV \)：`P`（fp16，TMEM）× `V`（fp16，SMEM）累加进 `O`（fp32，TMEM），由 WG3 warp 0 以 `dispatch="tcgen05"` 发起；归约维按 `K_SPLIT`（causal 64 / non-causal 96）分两段，第一段等 `kv_load.full` + `p_o_rescale`，第二段只加等 `p_ready_2` 且恒 `accum=True`。
- **fp16 列打包**：同一 512 物理列的 TMEM 通过 `move_base_to(0)` 建立 fp32/fp16 双视图，换算 \( c \leftrightarrow (\lfloor c/2\rfloor,\ c\bmod 2) \)；128 个 fp16 恰占 64 个物理列。
- **TMEM 布局复用**：预算 \( 2\times(128_S+128_O)=512 \) 列排满，`P` 不设独立区域，而是覆写 `S` 的后半——S0=[0,128)、P0=[64,128)、S1=[128,256)、P1=[192,256)、O0=[256,384)、O1=[384,512)；重叠是时序复用，`S` 与 `P` 从不同时占用同一些位。
- **屏障防护**：三条顺序要求由 `s_ready`（S 可读）、"先整行加载后写 P"（S 读完整）、`wait::st` + `p_o_rescale`/`p_ready_2`（P 写完才读）、同线程固定 `tcgen05` 序列（P 消费完才覆写）共同保证；`O` 由 `p_o_rescale`（256 次到达，含 WG2 放行）与 `o_ready`（epilogue 读取）防护。
- FA4 相比 GEMM 多出的屏障大多围绕 softmax——因为 softmax 把寄存器计算、`P` 的 TMEM 重写与 `O` 的可选 rescale 插进了两个 MMA 之间，每个边界都需要显式信号。

## 7. 下一步学习建议

- **下一讲 u14-l5（条件 rescaling 与 writeback）**：本讲多次出现的 `p_o_rescale` 的 WG2 半、`o_ready` 之后的路径，正是下一讲的主角——`delta < -8` 时 `O` 的 TMEM→寄存器→TMEM 校正循环、`any_sync` 两级过滤、以及最终 `O/row_sum` 经 SMEM 由 TMA store 写回 GMEM 的完整链路。
- **回看 u8-l2（相位复用）**：本讲 `p_ready_2.wait(i_q, phase_tmem)` 里相位的作用在下一讲的跨块迭代中会更密集，建议复习 full/empty 双屏障与 stage 复用环。
- **延伸阅读**：正文章节 *Pipeline Timeline*（[chapter_flash_attention/index.md:667-710](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L667-L710)）把本讲的屏障画进了时间线，能看到 Q/KV/TMEM 三条流速不同的流水线如何各自独立推进；有余力可对照 tirx-kernels 的 `flash_attention4.py` 原文验证 `S_region`/`P_region`/`O_region` 的真实定义。
