# u14-l6 causal 掩码、GQA 与 tile 调度

## 1. 本讲目标

前五讲（u14-l1 到 u14-l5）把 FA4 的主干讲完了：在线 softmax 状态、TMEM 上的 S/P/O 布局、warp 角色与屏障协议、两个 MMA 与夹在中间的 softmax、条件 rescaling 与 writeback。但那条流水线一直假设两件事：每个 query 能看到所有 key（non-causal），一个 Q tile 的 128 行都来自同一个 head。本讲撤掉这两个假设，补齐 FA4 的收尾机制，回答三个问题：

1. **causal 掩码怎么落地**：右下对齐（bottom-right aligned）的因果掩码在数学上是什么？内核为什么在"整块跳过"和"块内列掩码"两个层级上同时处理它？
2. **GQA 怎么打包**：多个 query head 共享一个 K/V head 时，128 行的 Q tile 如何同时装下"序列位置 × query head"两个维度？为什么 QKᵀ MMA 对此毫无感知？
3. **tile 怎么调度、结果怎么验证**：causal 与 non-causal 为什么需要两个不同的调度器？跑通 FA4 的编译与数值验证回路需要满足哪些硬性约束？

学完后你应能：对任意给定 `SEQ_LEN_Q`、`SEQ_LEN_KV`、`BLK_N` 画出 K/V 块的"全有效 / 部分有效 / 跳过"分类表；把 Q tile 里的任意 packed 行映射回 `(序列偏移, query head)`；并独立完成（或推演）FA4 的编译与 torch 参考对照。

## 2. 前置知识

本讲直接建立在以下已建立的认知上（只引用结论，不重复推导）：

- **在线 softmax 与掩码的相互作用（u14-l1 / u14-l3）**：每行维护 `row_max` / `row_sum` / `O` 三元组；\( p_{ij} = \mathrm{exp2}((s_{ij}-r_i)\cdot\text{scale\_log2}) \)。若某列在指数化前被置为 \(-\infty\)，它既不抬高行最大值，\( p_{ij} \) 也恒为 0——这是"列掩码"能无声融入在线 softmax 的原因。
- **FA4 角色与流水线（u14-l2 / u14-l5）**：WG0/WG1 跑两个 Q stage 的 softmax，WG3 的三个 warp 分别发 TMA load、两类 MMA、TMA store，WG2 做校正与 non-causal epilogue；causal 路径的 epilogue 移到 WG0/WG1（本讲 4.1 会展开）。
- **K_SPLIT 约定（u14-l4）**：`K_SPLIT = (4 if is_causal else 6) * MMA_K`，即 PV MMA 把 128 列的 P 拆成 causal 的 64+64 或 non-causal 的 96+32 两段——这是 causal 模式改变流水线细节的例证之一。
- **持久内核与 tile scheduler（u12-l3）**：GEMM Step 6 用静态 `ClusterPersistentScheduler2D` + `l2_group_size=8` 让固定数量的 CTA 循环认领 tile 并改善 L2 局部性；FA4 的调度器是同一思想在 attention 上的变体。
- **负载不均的静态/动态解法（u8-l3）**：CLC 用硬件动态认领解决 persistent 调度的尾部空闲；本讲会看到 FA4 causal 模式选择了另一条静态路——LPT（Longest Processing Time first，最长处理时间优先）重排。
- **编译与验证回路（u1-l3 / u9-l2）**：`tvm.compile(IRModule, target="cuda", tir_pipeline="tirx")` 产出可直接收 PyTorch 张量的 `ex.mod`；数值对照用 `torch.testing.assert_close`。

仍要提醒（u14-l4 起反复出现的事实）：本仓库只有教材正文，内核源码在 [tirx-kernels 的 `flash_attention4.py`](https://github.com/mlc-ai/tirx-kernels/blob/5be39749e7dfd2c4bdae9b4d396f8ec35af07126/tirx_kernels/attention/flash_attention4.py)，本章代码节选自它（见 [chapter_flash_attention/index.md:270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L270)）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲使用方式 |
|---|---|---|
| `chapter_flash_attention/index.md` | FA4 章正文（节选自 tirx-kernels） | 精读 *Causal Masking*（L790–800）、*GQA Support*（L802–835）、*Tile Scheduling*（L837–857）、*Compile and Verify*（L859–906）四个小节，即本讲全部三个最小模块的原文；另引用约定表中的 `K_SPLIT`（L276）、GQA_RATIO=1 时的屏障配对（L306）、causal epilogue 归属（L266、L770）与章末练习 7/8（L916–917） |

本讲是 FA4 单元里唯一不需要新 TMEM 布局、新屏障类型的一讲——三个模块分别改的是**循环边界**（causal）、**地址解释**（GQA）和**任务顺序**（调度），这正是它们能放在收尾位置的原因。

## 4. 核心概念与源码讲解

### 4.1 causal 掩码：右下对齐与块级三分法

#### 4.1.1 概念说明

causal（因果）attention 规定每个 query 只能使用**不晚于它自己位置**的 key——这是自回归语言模型的生成约束。当 Q 与 K 等长时，score 矩阵的有效区域就是主对角线及以下；当两者不等长（例如 KV 带前缀、比 Q 长）时，本书实现采用**右下对齐**的掩码：把有效区域锚定在矩阵右下角。正文给出精确规则——query 位置 `i` 至多访问 key 位置 \( i + \text{SEQ\_LEN\_KV} - \text{SEQ\_LEN\_Q} \)，再截断到 \( \text{SEQ\_LEN\_KV}-1 \)（[chapter_flash_attention/index.md:792](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L792)）：

\[ j_{\max}(i) = \min\bigl(i + (S_{KV} - S_Q),\; S_{KV}-1\bigr) \]

直觉：最后一个 query 必须能看到最后一个 key（右下角那个格子永远有效），于是所有 query 的可见上限整体平移 \( S_{KV}-S_Q \)。等长时偏移为 0，退化为标准下三角。

内核在**两个层级**同时消化这条规则（同一句原文）：完全无效的块直接跳过；跨越边界的块照常计算、由 softmax 在指数化前把无效列置 \(-\infty\)。为什么分两级？因为块是搬运与计算的单位——整块无效时，跳过它同时省下 TMA 流量、MMA 指令和 softmax 工作；只有边界块才需要付出"算完再掩掉"的代价。而 \(-\infty\) 列在在线 softmax 里是"零副作用"的：不进 `row_max`、\( p_{ij}=0 \)、对 `row_sum` 与 `O` 无贡献（[chapter_flash_attention/index.md:796](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L796)）。

#### 4.1.2 核心流程

对当前 Q 任务（其行对应的 query 位置集合为 \( I \)）：

```text
offset      = SEQ_LEN_KV - SEQ_LEN_Q
j_max       = min(max_query_pos_in_task + offset, SEQ_LEN_KV - 1)
n_block_max = j_max // BLK_N + 1        # 排他上界：要访问的 K/V 块数

对块 b = 0 .. n_block_max-1:
    if (b+1)*BLK_N - 1 <= j_max:  全有效   → 正常计算，无需掩码
    else:                         部分有效 → 照常 QKᵀ MMA；
                                          softmax 对每行由 query 位置与块偏移推出列上限，
                                          超过上限的列在寄存器中置 -inf
对块 b = n_block_max .. 总块数-1:  跳过     → 根本不加载、不计算
```

分类判据整理成判别式（块 \( b \) 覆盖 key \( [\,b\cdot B_N,\ (b+1)\cdot B_N) \,) \)：

\[ \text{块 } b \text{ 对 query } i \begin{cases} \text{全有效} & (b{+}1)B_N - 1 \le j_{\max}(i) \\ \text{部分有效} & b\,B_N \le j_{\max}(i) < (b{+}1)B_N - 1 \\ \text{跳过} & j_{\max}(i) < b\,B_N \end{cases} \]

注意任务成本随 query 位置**单调不减**：\( \text{n\_block\_max}(i) = \lfloor j_{\max}(i)/B_N \rfloor + 1 \)。靠前的 Q 任务可能只看 1 个块，靠后的要看全部——这个不均衡正是 4.3 节 causal 调度器存在的理由。

还有一个容易忽略的细节：掩码在 softmax 里的实现不是逐元素比较坐标，而是 `mask_r2p(...)` 把列上限换算成一组位掩码，每张掩码管至多 32 个元素，用 bit test 生成谓词，lower 到高效的寄存器-谓词路径（[chapter_flash_attention/index.md:798](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L798)）。每行 128 列恰是 4 张 32-bit 掩码的量。

#### 4.1.3 源码精读

**（1）块级裁剪：`get_n_block_max`。** 正文："`get_n_block_max(...)` 返回当前 Q 任务所需 K/V 块的排他上界，循环只访问 `0` 到 `n_block_max - 1`，从不加载不含任何有效 score 的更高编号块"——[chapter_flash_attention/index.md:794](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L794)。这是"跳过"层级的实现载体：K/V 循环的 trip count 本身被 causal 规则压缩。

**（2）块内掩码：寄存器里的 \(-\infty\)。** 正文："跨边界的块仍跑 QKᵀ MMA，但 softmax 在指数化前掩掉无效列。对每行，它由 query 位置和块偏移推出列上限，保留上限及以下的列，之后的列在寄存器中置 \(-\infty\)。这些列不影响行最大值，其 \( p_{ij} \) 值变为零"——[chapter_flash_attention/index.md:796](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L796)。掩码住在 softmax 已经把 S 行读进来的寄存器里，不新增任何访存。

**（3）位掩码加速。** `mask_r2p` 的设计与"每行 128 列"的配合见 [chapter_flash_attention/index.md:798](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L798)；同时它点明"完全位于边界内的块不需要任何掩码"——全有效块零开销。

**（4）causal 不只是掩码。** 正文最后一段总结 causal 模式带来的四处调度与交接变化："它修剪 K/V trip count、把掩码插进驻留寄存器的 softmax、把 PV 拆分改为 64+64、并把最终 epilogue 移入 WG0/WG1，消除了向 WG2 的最后一次 `row_sum` 交接"——[chapter_flash_attention/index.md:800](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L800)。其中 PV 拆分 64+64 与约定表一致（`K_SPLIT = 4 * MMA_K`，[chapter_flash_attention/index.md:276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L276)）；epilogue 移交 WG0/WG1 的原因也能从 u14-l5 的角度理解：causal 下每个任务的 K/V 数不同，由跑该 stage softmax 的战组顺手收尾，省掉一次跨战组的 `row_sum` mailbox 往返（对照 [chapter_flash_attention/index.md:770](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L770)）。

#### 4.1.4 代码实践：手推 + 脚本核对块分类表（章末练习 7）

1. **实践目标**：对 `SEQ_LEN_Q=6`、`SEQ_LEN_KV=8`、`BLK_N=4` 的小算例，给出每个 query 位置的 K/V 块分类表，并用脚本核对。
2. **操作步骤**：
   - 手推：偏移 \( = 8-6 = 2 \)；对每个 \( i \in [0,6) \) 算 \( j_{\max}(i)=\min(i+2,7) \)；块 0 覆盖 key 0–3、块 1 覆盖 key 4–7；按 4.1.2 的判别式分类。
   - 写一个脚本（**示例代码**，非项目原有）核对：

     ```python
     def classify(seq_q, seq_kv, blk_n):
         off = seq_kv - seq_q
         n_blocks = (seq_kv + blk_n - 1) // blk_n
         table = []
         for i in range(seq_q):
             jmax = min(i + off, seq_kv - 1)
             row = []
             for b in range(n_blocks):
                 if b * blk_n > jmax:
                     row.append("skip")
                 elif (b + 1) * blk_n - 1 <= jmax:
                     row.append("full")
                 else:
                     row.append("partial")
             table.append((i, jmax, row))
         return table

     for i, jmax, row in classify(6, 8, 4):
         print(f"query {i}: j_max={jmax}, blocks={row}")
     ```

3. **需要观察的现象**：打印出的每行 `blocks` 序列，以及每行非 `skip` 的块数（即 `n_block_max`）。
4. **预期结果**（手推答案，脚本应逐行复现）：

   | query \( i \) | \( j_{\max} \) | 块 0（key 0–3） | 块 1（key 4–7） | 访问块数 |
   |---|---|---|---|---|
   | 0 | 2 | **部分有效**（key 3 无效） | 跳过 | 1 |
   | 1 | 3 | 全有效 | 跳过 | 1 |
   | 2 | 4 | 全有效 | **部分有效**（仅 key 4 有效） | 2 |
   | 3 | 5 | 全有效 | 部分有效 | 2 |
   | 4 | 6 | 全有效 | 部分有效 | 2 |
   | 5 | 7 | 全有效 | 全有效 | 2 |

   练习 7 的两问随之有答案：query 0 可见的最大 key 索引是 **2**，query 5 是 **7**；任务成本随 query 位置递增（1 → 2 个块），因此 causal 任务天然不均衡，调度器应让重的（靠后的）Q 块先上——这正是 4.3 节 LPT 调度器的动机。
5. 本实践为纯 CPU 推演，无需 GPU；若想更进一步，可把 `blk_n` 改成 128、`seq` 改成 1024，观察访问块数从 1 到 8 的阶梯（见第 5 节综合实践）。

#### 4.1.5 小练习与答案

1. **练习**：`SEQ_LEN_Q = SEQ_LEN_KV = 10`、`BLK_N=4`，query 7 需要访问哪些块？各属哪类？
   **答案**：偏移 0，\( j_{\max}=7 \)。块 0（key 0–3）全有效、块 1（key 4–7）全有效、块 2（key 8–11）跳过；`n_block_max = 7//4+1 = 2`。
2. **练习**：整块跳过与块内 \(-\infty\) 掩码在节省的开销上有何不同？为什么边界块不能也用"跳过"解决？
   **答案**：跳过同时省 TMA 流量、MMA 指令与 softmax 工作；掩码只省"无效列对结果的贡献"，搬运与计算照付。边界块里同块还有有效列（例如 4.1.4 表中 query 2 的块 1），粒度是整块搬运/计算，无法只算半块，所以必须算完再掩。
3. **练习**：为什么掩码必须在指数化**之前**置 \(-\infty\)，而不是把算出的 \( p_{ij} \) 清零就行？
   **答案**：先置 \(-\infty\) 才能保证无效列不参与 `row_max`（否则一个虚假的大 score 会抬高参考，虽不改最终归一化结果，但破坏"参考取自已见有效 score"的约定与数值稳定性）；清零 \( p \) 只挡住 `row_sum` 与 `O`，挡不住 `row_max`。事实上两者都得到正确结果（列贡献为零），但"不进 row_max + p=0"是正文明确写出的双重保证（[chapter_flash_attention/index.md:796](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L796)）。

### 4.2 GQA：把多个 query head 打包进一个 Q tile

#### 4.2.1 概念说明

GQA（Grouped Query Attention，分组查询注意力）让若干 query head 共享一个 K/V head，以削减 K/V 的存储与访存流量。设 query head 数为 `num_qo_heads`、K/V head 数为 `num_kv_heads`，则每个 K/V head 服务

\[ \text{GQA\_RATIO} = \text{num\_qo\_heads} \,//\, \text{num\_kv\_heads} \]

个 query head（[chapter_flash_attention/index.md:804](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L804)）。极端情形：`GQA_RATIO=1` 即普通 MHA（每 head 一套 K/V），`num_kv_heads=1` 即 MQA。

FA4 处理 GQA 的方式不是为每个 query head 单独跑流水线，而是**重新解释 Q tile 的 128 行**：这 128 行不再对应 128 个序列位置，而是"若干序列位置 × 组内若干 query head"。关键在于两个量（原文代码，[chapter_flash_attention/index.md:806-809](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L806-L809)）：

```python
GQA_RATIO = num_qo_heads // num_kv_heads
SEQ_Q_PER_TILE = BLK_M // GQA_RATIO
```

即一个 128 行 tile 装 `SEQ_Q_PER_TILE` 个序列位置 × `GQA_RATIO` 个 query head。例如 `GQA_RATIO=4` 时，128 行 = 32 个序列位置 × 4 个 query head（原文举例，[chapter_flash_attention/index.md:811](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L811)）。

为什么这样做划算？K 与 V **不为每个 query head 复制**：打包进 128 行的所有 `GQA_RATIO` 个 query head 复用同一个 `kv_head_idx` 的那一块 K/V tile（[chapter_flash_attention/index.md:833](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L833)）。一次 K/V 搬运服务 4 倍（以 GQA_RATIO=4 为例）的 query 计算——这与 GEMM Step 9 共享 staged B 是同一个"每片数据多算几次"的思想（u13-l3）。

#### 4.2.2 核心流程

对 tile 内第 `row` 行（0 ≤ row < 128），坐标拆包公式为（原文，[chapter_flash_attention/index.md:813-817](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L813-L817)）：

```text
seq_offset    = row // GQA_RATIO          # 行对应的序列内位置偏移
q_head_offset = row % GQA_RATIO           # 组内第几个 query head
q_head        = kv_head_idx * GQA_RATIO + q_head_offset
```

整条数据路的分工：

```text
加载 Q（4D 视图，TMA 一次搬完，无需重排 pass）:
    Q_smem_4d[i_q, 0:SEQ_Q_PER_TILE, 0:GQA_RATIO, :]  ←  Q[batch, 序列传, head 组, dim]
计算（完全无感知）:
    QKᵀ MMA / softmax / PV MMA 仍把 Q_smem 当作普通的 128 × HEAD_DIM 操作数
回写 O（对称的 4D 视图）:
    O[batch, seq, qo_head, dim] 按 (seq_offset, q_head) 拆开存回
```

注意 row 的**行主序嵌套**：`seq_offset` 是外层、`q_head_offset` 是内层——相邻两行通常属于同一序列位置的不同 head。下游两处必须懂这套编码：调度器的 query-tile 步长用 `SEQ_Q_PER_TILE` 换算序列推进量；causal 掩码的行位置用 `GQA_RATIO` 从 packed 行还原 query 的真实序列位置（[chapter_flash_attention/index.md:835](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L835)）。

#### 4.2.3 源码精读

**（1）Q 加载的 4D 视图。** 原文代码（[chapter_flash_attention/index.md:821-831](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L821-L831)）：

```python
Q_smem_4d = Q_smem.view(SMEM_PIPE_DEPTH_Q, SEQ_Q_PER_TILE, GQA_RATIO, HEAD_DIM)
Tx.copy_async(
    Q_smem_4d[i_q, :, :, :],
    Q[batch_idx,
      m_start + i_q * SEQ_Q_PER_TILE : m_start + (i_q + 1) * SEQ_Q_PER_TILE,
      kv_head_idx * GQA_RATIO : (kv_head_idx + 1) * GQA_RATIO,
      :],
    **tma_copy_q,
)
```

读点有三处：

- 目的端 `Q_smem.view(...)` 把同一块 SMEM 重解释成 `(stage, sequence, query head within the group, dim)` 四维——回顾 u4-l1：视图只改元数据，数据不动。
- 源端切片 `[m_start + i_q*SEQ_Q_PER_TILE : ..., kv_head_idx*GQA_RATIO : ...]` 说明 `m_start` 以**序列位置**为单位、`i_q` 是 Q stage 编号、head 维只取当前组 `GQA_RATIO` 个。
- 正文强调："视图告诉 TMA copy 如何解释源与目的坐标，**不需要单独的重排 pass**"（[chapter_flash_attention/index.md:819](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L819)）——布局翻译被吸收进 TMA 的地址计算，正是 u6-l1"tensor map 描述一切、指令只给坐标"的复利。

**（2）输出侧镜像。** "输出侧与输入对称，用匹配的 4D 视图把 packed 行在 epilogue 后存回 `O[batch, seq, qo_head, dim]`"——[chapter_flash_attention/index.md:833](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L833)。

**（3）计算路径零改动。** 原文结论："GQA 不改变 QKᵀ MMA、softmax 或 PV MMA 的 tile 形状：计算路径仍看到一个普通的 `128 × HEAD_DIM` Q 操作数"（[chapter_flash_attention/index.md:835](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L835)）。用三要素语言总结：GQA 只动了 **layout 的解释**（load/store 两端的 4D 视图），scope 与 dispatch 不变。

**（4）GQA_RATIO 甚至影响同步粒度。** 屏障总表里 statistics named barrier 的参与者一栏写着："一个 softmax 战组与 WG2 配对（`GQA_RATIO=1` 时改为配对的 warp）"——[chapter_flash_attention/index.md:306](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L306)。退化到 MHA 时 mailbox 的交接按更细的 warp 粒度配对，说明 `GQA_RATIO` 这个参数渗透得比"只是个加载技巧"更深。

#### 4.2.4 代码实践：packed 行 → (序列偏移, query head) 映射（章末练习 8）

1. **实践目标**：对 `num_qo_heads=32`、`num_kv_heads=8`、`BLK_M=128`、`kv_head_idx=3`，计算 `GQA_RATIO` 与 `SEQ_Q_PER_TILE`，并映射 packed 行 0、5、127。
2. **操作步骤**：先手推，再用脚本核对（**示例代码**）：

   ```python
   def unpack(row, gqa_ratio, kv_head_idx):
       seq_offset = row // gqa_ratio
       q_head_offset = row % gqa_ratio
       q_head = kv_head_idx * gqa_ratio + q_head_offset
       return seq_offset, q_head_offset, q_head

   gqa_ratio = 32 // 8          # = 4
   seq_q_per_tile = 128 // 4    # = 32
   for row in (0, 5, 127):
       print(row, unpack(row, gqa_ratio, kv_head_idx=3))
   ```
3. **需要观察的现象**：三行的 `(seq_offset, q_head_offset, q_head)`，以及 `q_head` 是否始终落在 `[12, 16)` 区间。
4. **预期结果**：

   | packed row | seq_offset | q_head_offset | q_head |
   |---|---|---|---|
   | 0 | 0 | 0 | 12 |
   | 5 | 1 | 1 | 13 |
   | 127 | 31 | 3 | 15 |

   `GQA_RATIO=4`、`SEQ_Q_PER_TILE=32`；`kv_head_idx=3` 的组是 head 12–15。练习 8 的最后一问——为什么 128 行能共享同一块 K/V tile：每行的 `q_head` 都落在该组内，而组内所有 query head 按定义共享 `kv_head_idx=3` 的 K/V，因此一次 stage 好的 K/V tile 对全部 128 行有效，计算路径又只把它当作 128×HEAD_DIM 的普通操作数。
5. 纯 CPU 推演即可完成；如想看真实代码，去 tirx-kernels 的 `flash_attention4.py` 中搜索 `GQA_RATIO` 与 `SEQ_Q_PER_TILE` 的定义与使用点（待本地验证：具体行号随仓库版本变化）。

#### 4.2.5 小练习与答案

1. **练习**：`num_qo_heads=16`、`num_kv_heads=16`、`BLK_M=128` 时 `GQA_RATIO` 与 `SEQ_Q_PER_TILE` 各是多少？此时 Q tile 的语义是什么？
   **答案**：`GQA_RATIO=1`、`SEQ_Q_PER_TILE=128`，退化为 MHA——128 行就是 128 个连续序列位置，无 head 打包。另注意此时 statistics named barrier 按 warp 粒度配对（4.2.3 第 4 点）。
2. **练习**：`kv_head_idx=3`、`GQA_RATIO=4` 时，packed 行 100 对应什么坐标？
   **答案**：`seq_offset = 100//4 = 25`，`q_head_offset = 100%4 = 0`，`q_head = 12`。
3. **练习**：如果 `GQA_RATIO` 不能整除 `BLK_M=128`（例如 `GQA_RATIO=5`），会发生什么？这对应 4.3 节的哪条编译约束？
   **答案**：128 行无法均匀切成"序列位置 × head"，行坐标拆包会在 tile 边界处错位。对应验证小节的硬性约束"`GQA_RATIO` 必须整除 `BLK_M=128`"（[chapter_flash_attention/index.md:866](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L866)）。

### 4.3 tile 调度与编译验证

#### 4.3.1 概念说明

前两节解决了"一个任务怎么做"；本节解决"任务从哪来、做完给谁"。调度器把每个 CTA 映射到一个 `(batch, kv_head, m_block)` attention 任务，其中一个 `m_block` 包含前面引入的两个 Q stage，即每个任务同时推进两个 query tile（[chapter_flash_attention/index.md:839](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L839)）。任务总数 = `batch × num_kv_heads × m_block 数`。

non-causal 与 causal 的关键差异在**任务成本**：non-causal 每个任务遍历同样数量的 K/V 块（所有 query 看全部 key）；causal 的任务成本随 `m_block` 递增（4.1 的结论）。因此两种模式用了两个调度器（[chapter_flash_attention/index.md:839-842](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L839-L842)）：

- **`FlashAttentionLinearScheduler`（non-causal）**：任务等价，启动固定数量的 persistent CTA；每个 CTA 完成一个任务后把线性任务索引加上 `num_ctas`，继续处理下一个分配。这就是 u12-l3 GEMM Step 6 的静态 round-robin persistent 模式。
- **`FlashAttentionLPTScheduler`（causal）**：LPT 即"最长处理时间优先"——调度理论里的经典启发式：长任务先上，短任务填缝，最小化收尾时的尾部空闲。它做两件事：其一，**反转 `m_block` 顺序**，让更靠后、更重的块先被调度，削弱 launch 末端的不均衡；其二，把展平的 `batch × kv_head` 索引按 `L2_SWIZZLE` 分组，在推进到下一个 `m_block` 之前先访问当前组内的 batch/KV-head 任务，让一组有界的 K/V 工作集在调度器推进 `m_block` 的过程中保持在 L2 里。当前实现对每个 causal 任务启动一个 CTA。

这是 u8-l3 留下的问题在 FA4 里的回答：CLC 用硬件动态认领解决负载不均，FA4 causal 选择的是**静态重排 + L2 分组**这条零运行时开销的路——任务成本可从 `m_block` 位置**先验地**算出时，静态 LPT 就够用。

#### 4.3.2 核心流程

两个调度器暴露**同一个循环接口**（原文代码，[chapter_flash_attention/index.md:848-855](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L848-L855)）：

```python
while scheduler.valid():
    m_block_idx = scheduler.m_block_idx
    batch_idx = scheduler.batch_idx
    kv_head_idx = scheduler.head_idx
    # process one Q block against its K/V block range
    scheduler.next_tile()
```

循环体内部对两种模式完全一致：TMA load、QKᵀ MMA、softmax、PV MMA、correction、TMA store（[chapter_flash_attention/index.md:857](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L857)）。差异全部藏在 `next_tile()`：

```text
non-causal: next_tile() 把 persistent CTA 推进到下一个任务（索引 += num_ctas），循环继续
causal:     一个 CTA 只拥有当前任务，next_tile() 后 valid() 变 False，退出循环
```

调度器还负责把 `m_block_idx` 换算成 4.2 节 Q 加载里的 `m_start`（以序列位置为单位，步长牵涉 `SEQ_Q_PER_TILE` 与每任务两个 stage），并给 causal 模式提供 4.1 节的 `get_n_block_max` 所需的任务位置信息。

最后是调参警示（原文）：调度常量按本书的 B200 配置调优，**不是**普适的 Blackwell 参数——`max_ctas=148` 封顶 non-causal persistent worker 数（148 即 B200 的 SM 数）；`L2_SIZE=50 MiB` 是计算 `L2_SWIZZLE` 时假设的**可用** cache 预算，不是 GPU 报告的完整 L2 容量。换一块 SM 数或 cache 配置不同的 Blackwell，应重新调这些值或从目标配置推导（[chapter_flash_attention/index.md:844](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L844)）。

#### 4.3.3 源码精读

**（1）两种策略的原文对照。** 见 [chapter_flash_attention/index.md:841-842](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L841-L842)：Linear 的"每个任务访问相同数量的 K/V blocks"对应成本均匀的前提；LPT 的"靠前的 Q block 可能只访问 1 个 K/V block，靠后的可能全访问"正是 4.1.2 推出的单调性。

**（2）编译与验证的完整回路。** FA4 与 GEMM 示例的构造方式不同：它用工厂函数 `get_flash_attention4_kernel` 生成内核；安装 README 所述的配套仓库（tirx-kernels）后 `import flash_attention4`、编译、与 PyTorch 参考对照（[chapter_flash_attention/index.md:861](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L861)）。当前 `flash_attention4.py` 面向固定 tile 形状特化，不是通用 attention 接口，输入必须满足四条约束（[chapter_flash_attention/index.md:863-868](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L863-L868)）：

| 约束 | 原因 |
|---|---|
| `NUM_QO_HEADS` 被 `NUM_KV_HEADS` 整除 | `GQA_RATIO` 必须是整数（4.2 的地板除） |
| `GQA_RATIO` 整除 `BLK_M=128` | 128 个 packed 行须均匀映射回序列位置 |
| `HEAD_DIM` 必须为 128 | TMEM 区域、PV MMA 与 epilogue 都按该宽度组织 |
| non-causal 路径 `SEQ_LEN_KV` 被 `BLK_N=128` 整除 | 代码向上取整 K/V 块数，但不给 non-causal 的末尾残块加尾掩码；内置 causal/non-causal 测试配置都取 128 的倍数 |

注意第四条只约束 non-causal：causal 路径有 4.1 的列掩码机制，天然能处理残块；non-causal 没有。

**（3）验证脚本本体。** [chapter_flash_attention/index.md:872-900](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L872-L900)，四个关键读点：

```python
B, S, Hq, Hkv, D = 1, 1024, 32, 8, 128   # GQA: 32 query heads share 8 KV heads
assert Hq % Hkv == 0
assert 128 % (Hq // Hkv) == 0
assert D == 128
assert S % 128 == 0
...
kernel = get_flash_attention4_kernel(B, S, S, Hq, Hkv, D, is_causal=False)
...
ex = tvm.compile(tvm.IRModule({"main": kernel}), target=target, tir_pipeline="tirx")
ex.mod(Q, K, V, O)
...
ref = F.scaled_dot_product_attention(qt, kt, vt, enable_gqa=True).transpose(1, 2).half()
torch.testing.assert_close(O, ref, rtol=1e-2, atol=1e-2)
```

- 四条 `assert` 把上表约束前置成显式检查；
- torch 参考用 `F.scaled_dot_product_attention(..., enable_gqa=True)`——参考实现同样按 GQA 语义让 32 个 query head 共享 8 个 KV head，对照才公平；先 `.float()` 升 fp32、最后 `.half()`，与 u9-l2 的验证纪律同构；
- 容差 `rtol=1e-2, atol=1e-2` 与源码自带测试一致。**预期输出**：`... -> PASS`（[chapter_flash_attention/index.md:902](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L902)）。

**（4）误差从哪来、错了查哪里。** 内核以 fp32 做在线 softmax 累加，但仍有若干有限精度效应使其偏离 fp32 参考：输入与操作数的 fp16 存储与舍入、硬件 `exp2` 与三次多项式近似的有限精度、分块累加的求和顺序不同、`O` 的最终 fp16 cast（[chapter_flash_attention/index.md:902](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L902)）。**偏大的误差通常指向 softmax 交接**：漏了 `s_ready`、`p_o_rescale` 或 `p_ready_2` 的 wait，或某个 `row_max`/`row_sum` 更新没到达校正路径（[chapter_flash_attention/index.md:904](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L904)）——即错果先怀疑 u14-l2/u14-l4 的屏障协议，而不是本讲的调度与掩码。

#### 4.3.4 代码实践：跑通（或推演）FA4 的编译验证

1. **实践目标**：有 Blackwell GPU 时完整跑通 FA4 的编译与 torch 参考断言；无 GPU 时完成"约束核查 + 流程推演"版本。
2. **操作步骤**：
   - **有 GPU**：按 u1-l3 安装 `apache-tvm==0.26.0`、`cuda-bindings` 与 tirx-kernels（revision 与 TVM 版本成对钉死），把 [chapter_flash_attention/index.md:872-900](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L872-L900) 的脚本存成 `run_fa4.py`（**内核必须写在文件里**，TIRx 靠 Python 源码检视解析，不能塞进 `python -c`），执行 `python run_fa4.py`。跑通 non-causal 后把 `is_causal=False` 改为 `True` 再跑一次。
   - **无 GPU**：对下面每组配置判断四条约束是否全过，并说明第一条失败项（**示例练习**，答案见第 3 步）：
     - (a) `Hq=32, Hkv=8, D=128, S=1024, causal`
     - (b) `Hq=30, Hkv=8, D=128, S=1024`
     - (c) `Hq=32, Hkv=16, D=128, S=1024`
     - (d) `Hq=32, Hkv=8, D=64, S=1024`
     - (e) `Hq=32, Hkv=8, D=128, S=1000, non-causal`
     - (f) `Hq=32, Hkv=8, D=128, S=1000, causal`
3. **需要观察的现象 / 预期结果**：
   - 有 GPU：打印 `FA4: B=1 S=1024 Hq=32 Hkv=8 D=128, non-causal -> PASS`；causal 版同样 PASS。**待本地验证**（本讲义未在 GPU 上运行）。
   - 推演答案：(a) 全过；(b) 失败——`Hq % Hkv = 30 % 8 ≠ 0`，`GQA_RATIO` 非整数；(c) 全过（`GQA_RATIO=2` 整除 128）；(d) 失败——`HEAD_DIM` 必须为 128；(e) 失败——non-causal 要求 `S % 128 == 0`；(f) 过——causal 的列掩码能处理任意尾块（`SEQ_LEN_KV` 不需要被 `BLK_N` 整除，这是 4.1 掩码机制的直接收益，约束表只限制 non-causal）。
4. **加分项**：故意把 `rtol` 收紧到 `1e-4` 观察断言失败（有限精度效应的量级），或在 tirx-kernels 源码中找 `FlashAttentionLPTScheduler` 的 `next_tile()` 实现，核对 4.3.2 的行为描述（待本地验证）。

#### 4.3.5 小练习与答案

1. **练习**：为什么 causal 模式要反转 `m_block` 顺序，而 non-causal 不用？
   **答案**：causal 任务成本随 `m_block` 单调递增（4.1.2），若按自然顺序调度，最重的任务落在 launch 末尾形成长尾；反转后重任务先上、轻任务填缝（LPT）。non-causal 每个任务成本相同，顺序不影响均衡，用简单的线性 stride（`num_ctas`）即可。
2. **练习**：`L2_SWIZZLE` 分组解决什么问题？为什么说它是"统计性改善"？
   **答案**：同一 `m_block` 下的不同 `(batch, kv_head)` 任务共享相近的 K/V 访问模式；先在组内遍历 batch/KV-head 再推进 `m_block`，能让一组有界的 K/V 工作集持续命中 L2（[chapter_flash_attention/index.md:842](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L842)）。它与 u12-l3 的 `l2_group_size` 同源：改善的是缓存的统计行为而非正确性，也不保证命中。
3. **练习**：`max_ctas=148` 与 `L2_SIZE=50 MiB` 为什么不能直接搬到其他 Blackwell GPU？
   **答案**：148 对应本书 B200 的 SM 数（non-causal persistent worker 数不应超过它）；50 MiB 是推导 `L2_SWIZZLE` 时假设的可用 L2 预算，不是标称容量。SM 数或 cache 配置不同时应重调或从目标配置推导（[chapter_flash_attention/index.md:844](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L844)）。

## 5. 综合实践：为一个 causal + GQA 配置做完整的调度推演

把本讲三个模块串成一张图。配置取 `B=2, S_Q=S_KV=1024, Hq=32, Hkv=8, D=128, causal`（验证脚本的因果变体），完成以下推演（纯 CPU，**示例代码**）：

1. **任务清单**：`GQA_RATIO=4`、`SEQ_Q_PER_TILE=32`，一个 `m_block` 含 2 个 Q stage 共 64 个序列位置（由"每任务两个 128 行 Q tile、每 tile 32 个序列位置"推出），故每个 `(batch, kv_head)` 有 \( 1024/64=16 \) 个 `m_block`，任务总数 \( 2\times 8\times 16=256 \)。
2. **任务成本**：自注意力的掩码偏移为 0，`m_block` \( b \) 内最靠后 query 的 \( j_{\max}=64b+63 \)，该任务的 K/V 块数为 \( \lfloor(64b+63)/128\rfloor+1=\lceil(b{+}1)/2\rceil \)：依次为 1,1,2,2,…,8,8，每个 `(batch, kv_head)` 合计 72 块。
3. **调度模拟**：写一个小脚本枚举 256 个任务，分别按"自然顺序"与"反转 `m_block` 后再按 `L2_SWIZZLE` 分组遍历"两种顺序排到 148 个工人上（简单贪心：每次把下一个任务分给最早空闲的工人），画出（或打印）两种顺序下工人完成时间的分布，观察自然顺序的尾部空闲如何被反转消除。
   - **需要观察的现象**：自然顺序下最晚完工的工人比平均多承担多少块；反转后 makespan 是否下降。
   - **预期结果**：自然顺序把 8 块的重任务堆在末尾，产生明显长尾；反转后长尾缩短。具体数字**待本地验证**（取决于模拟的分配细节）。
4. **（可选，需 Blackwell GPU）**：按 4.3.4 跑通 causal 验证脚本后，把 `S` 改为 2048 重跑，对照第 2 步推演的任务成本阶梯是否与运行时间的变化方向一致。

这个实践里，第 1 步是 GQA 打包（4.2），第 2 步是 causal 块分类（4.1），第 3 步是调度顺序（4.3）——FA4 收尾三件套在一次推演里各就各位。

## 6. 本讲小结

- **causal 掩码是右下对齐的**：query \( i \) 至多看到 key \( \min(i+(S_{KV}-S_Q),\, S_{KV}-1) \)；内核分两级处理——整块无效的靠 `get_n_block_max` 压缩 K/V 循环 trip count 直接跳过，跨界块照常计算、由 softmax 在指数化前把无效列置 \(-\infty\)（不进 `row_max`、\( p=0 \)），`mask_r2p` 用 32 元素一位掩码加速谓词生成。
- **causal 改的不只是掩码**：还把 PV 拆分改为 64+64、把最终 epilogue 移入 WG0/WG1（省掉最后一次向 WG2 的 `row_sum` 交接）。
- **GQA 是行语义的重新解释**：128 行 = `SEQ_Q_PER_TILE` 个序列位置 × `GQA_RATIO` 个 query head，拆包公式 `seq_offset=row//GQA_RATIO`、`q_head=kv_head_idx*GQA_RATIO+row%GQA_RATIO`；Q 加载与 O 回写用 4D 视图翻译坐标，K/V 不复制、整 tile 共享，QKᵀ/softmax/PV MMA 完全无感知。
- **调度按任务成本分家**：non-causal 任务等价，用 persistent 的 `FlashAttentionLinearScheduler`（线性 stride）；causal 成本随 `m_block` 递增，用 `FlashAttentionLPTScheduler`（反转 `m_block` 的 LPT + `L2_SWIZZLE` 分组保 L2），二者共享同一循环接口，差异全在 `next_tile()`。
- **调度常量是 B200 专属**：`max_ctas=148`、`L2_SIZE=50 MiB`（可用预算非标称容量），换 GPU 须重调。
- **验证有硬性约束与既定容差**：`GQA_RATIO` 为整数且整除 128、`HEAD_DIM=128`、non-causal 要求 `S_KV` 被 128 整除（causal 靠掩码豁免）；`rtol=atol=1e-2` 与源码测试一致，偏大误差优先怀疑 softmax 交接屏障而非本讲机制。

## 7. 下一步学习建议

FA4 单元到此完结，全书正文也接近收尾。建议三条路：

1. **单元十五（附录工具链）**：u15-l4（可复现基准测试）教你给 FA4/GEMM 计时——本讲的调度推演只有配上测量才闭环；u15-l7（调试 warp-specialized 内核）的 worksheet 方法正好用于 4.3.3 第 4 点的"偏大误差查交接"流程。
2. **单元十六（扩展实践）**：u16-l2 的 capstone 建议从 FA4 出发做变体——本讲的三个模块（掩码分类、GQA 打包、调度顺序）是天然的低风险改点：例如改 `BLK_N` 观察块分类表变化、为非 128 倍数的 non-causal 序列补尾掩码、或把 LPT 调度换成 CLC 动态认领（对照 u8-l3）。
3. **源码延伸**：去 tirx-kernels 仓库读 `flash_attention4.py` 中 `get_n_block_max`、`mask_r2p`、两个 scheduler 类与 `get_flash_attention4_kernel` 的真实实现，核对正文的节选与简化；这也是向本书贡献改进（如尾块掩码）的入口。
