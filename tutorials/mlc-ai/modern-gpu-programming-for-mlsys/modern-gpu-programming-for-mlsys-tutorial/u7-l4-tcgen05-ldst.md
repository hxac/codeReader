# u7-l4 tcgen05.ld/st：数据搬运、打包与异步等待

## 1. 本讲目标

本讲是「Blackwell Tensor Core 与 TMEM」单元的收官。u7-l3 解决了「哪个 warp 能碰哪些 TMEM Lane」与「TMEM 怎么分配释放」，本讲回答剩下的三个实际问题：

1. **一次搬多少**：`tcgen05.ld` / `tcgen05.st` 的 `.shape` 与 `.num` 如何决定搬运总量与每线程寄存器用量，你能据此算出任一指令形式下每个线程要准备几个寄存器。
2. **16-bit 数据怎么进出 32-bit 列**：`.pack::16b` / `.unpack::16b` 的打包/解包规则，以及 FA4 中「同一行 TMEM 同时用 fp32 与 fp16 两种视图寻址」的真实做法，你能推导出 fp16 元素落在哪个物理列。
3. **wait 放在哪**：`tcgen05.wait::ld` 与 `tcgen05.wait::st` 的语义边界——前者保护「寄存器还没就绪」，后者保护「TMEM 写入还没完成」，你能给一段含 ld 与 st 的内核标注它们必须出现的位置及原因。

## 2. 前置知识

- **warp 集体指令（warp-collective）**：一条指令由 warp 内全部 32 个线程共同执行，硬件利用各线程的 lane ID 分配工作。u7-l3 已见过 `tcgen05.alloc` 是 warp 集体指令；本讲的 `tcgen05.ld/st` 同样如此。
- **TMEM 物理结构**：每个 CTA 的 TMEM 是 128 Lane × 最多 512 Column 的二维空间，每个 `(Lane, Column)` 单元 32 bit；分配沿 Column 维进行。TMEM 的 Lane 是**数据侧地址坐标**，不是线程的 lane ID。
- **Lane 访问窗口**：warpgroup 内编号为 \( w \) 的 warp 只能访问 \( \text{TLane} \in [32w,\, 32w+31] \)，列向不受限。读一个跨满 128 Lane 的累加器需要 4 个 warp 各做一次 warp 级访问。
- **异步与「发起/完成分离」**：u7-l1 已见过 `tcgen05.mma` 异步执行、靠 `tcgen05.commit` 挂到 mbarrier 通知完成。本讲的 ld/st 也是异步的，但完成机制更轻量：直接用 `tcgen05.wait::ld` / `::st` 由**当前线程**等待，不需要 mbarrier。
- **寄存器 fragment**：u5-l1 讲过 warp 级 MMA 的数据散布在各线程寄存器中的「fragment」概念；本讲 m8n8 示例中的 lane→数据映射是同一思想的复现。
- **TIRx 中的写法对照**：TIRx 源码里 `T.ptx.tcgen05.wait.ld()` 用点号，对应 PTX 里的 `tcgen05.wait::ld`（双冒号）。tile 操作 `Tx.wg.copy_async(reg, tmem)` 会 lower 成 `tcgen05.ld`，`Tx.wg.copy_async(tmem, reg)` 会 lower 成 TMEM store。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `chapter_tmem/index.md` | 本讲主教材：TMEM 章正文，覆盖 ld/st 集体语义、shape/num、16-bit 打包与异步等待 |
| `img/scripts/gen_tcgen05_ldst.py` | 生成 `img/tcgen05_ldst.svg` 的脚本，画出 m8n8 映射下 TMEM ↔ 寄存器 fragment 的双向搬运 |
| `img/scripts/gen_tmem_layout.py` | 生成 `img/tmem_layout_v3.png` 的脚本，画出 FA4 中 S/P/O 在 TMEM 列上的布局，含 fp16 视图与物理列的换算 |
| `chapter_gemm_basics/index.md` | GEMM Step 1 内核的真实 epilogue：`Tx.wg.copy_async` 读回累加器 + `T.ptx.tcgen05.wait.ld()` |
| `chapter_flash_attention/index.md` | FA4 内核的真实用法：softmax 分块读 S、写 P 后 `wait.st` 再 arrive、O 校正后的 `wait.st`，以及 `tmem_as_f16` 双视图 |

## 4. 核心概念与源码讲解

### 4.1 ld/st 是 warp 集体指令

#### 4.1.1 概念说明

`tcgen05.ld` 把数据从 TMEM 搬进寄存器，`tcgen05.st` 反向搬回 TMEM。它们是 TMEM 与寄存器之间唯一的常规通道——MMA 的累加器进 TMEM 后，epilogue 必须靠 `tcgen05.ld` 读回；softmax 这类「夹在两个 MMA 之间的寄存器运算」则既要 `ld` 读入、又要 `st` 写出。

「warp 集体」意味着：warp 内**每个线程都执行同一条指令、提供同一个 TMEM 地址操作数 `[taddr]`**。你不能像发起 TMA 或 `tcgen05.mma` 那样只让一个线程去发——硬件靠的是全体 32 个线程的 lane ID 来分发数据。这与 u2-l1 建立的「操作 scope」结论一致：`tcgen05.ld/st` 的 scope 是 warp。

#### 4.1.2 核心流程

一次 `tcgen05.ld` 的执行过程：

1. warp 内 32 个线程锁步执行同一条 `tcgen05.ld`，各自给出相同的 `[taddr]`（TMEM 地址，含 Lane 与 Column 信息）。
2. 硬件按 `.shape` / `.num`（见 4.2）确定本次访问覆盖的 TMEM 单元集合。
3. 硬件用每个线程自己的 lane ID，把这部分单元分发到该线程的目的寄存器（或反向：把寄存器写回对应单元）。
4. 指令异步返回，寄存器/内存的真正就绪要靠 `tcgen05.wait::ld` / `::st`（见 4.4）。

配套约束（承接 u7-l3）：warp 只能访问自己 warpgroup 内的 32-Lane 窗口，所以 `[taddr]` 的 Lane 坐标必须落在窗口内；列坐标则可及全部列。

#### 4.1.3 源码精读

TMEM 章正文给出集体语义的定义：

> [chapter_tmem/index.md:L99-L105](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L99-L105)
>
> 这一段说明：`tcgen05.ld` 把 TMEM 数据装进寄存器、`tcgen05.st` 反向搬回；二者都是 warp 集体指令——warp 内每个线程执行同一条指令、提供同一个 `[taddr]`，硬件用各线程的 lane ID 把访问分发到它的寄存器，或把寄存器放回对应的 TMEM 单元。配图 `tcgen05_ldst.svg` 用一个 m8n8 风格的寄存器 fragment 展示两个方向。

该配图正是由本讲要读的第一个脚本生成的。脚本左侧画 TMEM tile（8 个 TLane × 8 个 TCol），右侧画 warp 的 32 个线程各持的寄存器：

- [img/scripts/gen_tcgen05_ldst.py:L39-L47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tcgen05_ldst.py#L39-L47)：按行（TLane）着色绘制 8×8 的 TMEM tile，标注「row m → TLane m (TCol → N)」——这个局部映射里逻辑行直接落在 TLane 上。
- [img/scripts/gen_tcgen05_ldst.py:L49-L60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tcgen05_ldst.py#L49-L60)：绘制右侧 fragment，映射规则写在代码注释与图注里——`lane l → row l/4, cols 2·(l%4),+1`，即 lane 5 持有第 1 行的第 2、3 列两个元素，且一个 b32 寄存器装 2 个元素。
- [img/scripts/gen_tcgen05_ldst.py:L63-L68](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tcgen05_ldst.py#L63-L68)：两条箭头标出方向——`tcgen05.ld` 从 TMEM 指向寄存器，`tcgen05.st` 从寄存器指向 TMEM。

注意章节原文的提醒（[chapter_tmem/index.md:L103-L105](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L103-L105)）：m8n8 只是 `tcgen05.ld/st` 支持的**一种局部映射**，实际数据搬移由 `.shape`、`.num` 与可选的 `.pack::16b` / `.unpack::16b` 决定。

#### 4.1.4 代码实践

**实践目标**：把「warp 集体 + lane 分发」落到具体的 lane→数据映射上。

**操作步骤**：

1. 打开 [img/scripts/gen_tcgen05_ldst.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tcgen05_ldst.py)，读懂 L49-L60 的双重循环：外层 `r` 是行、内层 `j` 是行内第 j 对元素，`lane = 4 * r + j`。
2. 若本地装有 matplotlib，可在仓库根目录运行 `python img/scripts/gen_tcgen05_ldst.py`，它会打印 `wrote tcgen05_ldst.svg` 并在 `img/` 下重新生成该图（脚本自带 `matplotlib.use("Agg")`，无需显示环境）。运行结果：`img/tcgen05_ldst.svg` 更新。**待本地验证**（取决于 matplotlib 是否可用）。
3. 对照图中右侧 fragment，手工核对下表（见 4.1.5 练习 1 的答案区）。

**需要观察的现象**：图中每个线程框内只有一个 b32 寄存器、装 2 个 16-bit 元素；32 个线程 × 2 元素 = 64 元素，恰好等于左侧 8×8 tile 的元素总数——**元素数守恒**。

**预期结果**：你能不看图回答「lane 13 持有哪个 TMEM 单元」这类问题。

#### 4.1.5 小练习与答案

**练习 1**：在 m8n8 映射中，lane 5 和 lane 13 各持有哪些逻辑元素（用 (行, 列) 表示）？

**答案**：映射规则为 lane \( l \) → 行 \( \lfloor l/4 \rfloor \)、列 \( 2(l \bmod 4) \) 与 \( 2(l \bmod 4)+1 \)。lane 5：\( \lfloor 5/4 \rfloor = 1 \)，\( 5 \bmod 4 = 1 \)，故持有 (1,2) 与 (1,3)。lane 13：行 3、列 \( 2 \times 1 = 2 \) 与 3，故持有 (3,2) 与 (3,3)。

**练习 2**：为什么不能用 `if lane_id == 0` 把 `tcgen05.ld` 变成单线程操作？

**答案**：`tcgen05.ld/st` 是 warp 集体指令，硬件需要 warp 内全部 32 个线程的 lane ID 才能把 TMEM 单元分发到各线程的寄存器（或反向收集）。只让一个线程执行，其余 31 份目的寄存器无人提供，指令语义不成立。对比 u6-l1 的 TMA 与 u7-l1 的 `tcgen05.mma`：那两类操作的数据搬运由引擎/张量核心完成，单线程发起即可——「谁执行」由操作的 scope 决定，不能混用。

**练习 3**：一个 warp 想读跨满 128 个 TLane 的累加器，为什么需要 4 次 warp 级访问？

**答案**：u7-l3 的 Lane 窗口规则——warpgroup 内编号 \( w \) 的 warp 只能访问 \( [32w, 32w+31] \)。单个 warp 的窗口只有 32 个 Lane，128 行得分给 4 个 warp 各读自己那 32 行，共 4 次访问；这也正是「warpgroup 读 TMEM」这一说法的具体含义。

### 4.2 shape 与 repeat：一次搬多少、每线程几个寄存器

#### 4.2.1 概念说明

`.shape` 与 `.num` 共同决定一次 ld/st 的搬运量：

- `.shape`（如 `16x128b`）指定**多少个 TMEM lane 参与**、**每个 lane 取多少位基础数据量**；
- `.num`（如 `.x4`）把这个基础量**重复若干次**。

对内核作者来说，最关键的推论是**每线程寄存器用量**：搬运总量除以 32 个线程。寄存器是每线程私有、总量最小的资源（u2-l2），ld 的目的寄存器数直接决定这段代码的寄存器压力，进而影响 u3-l3 讲过的 occupancy。

还要分清一件事：这里的 shape 是**硬件搬运形状**（TMEM 与寄存器之间搬多少数据），与 MMA 的 `M × N × K` 逻辑形状是两回事——后者描述矩阵乘的逻辑维度，前者只管数据量；寄存器里的数据如何映射回逻辑矩阵元素，由 fragment 布局（u5-l1）解释。

#### 4.2.2 核心流程

记 `.shape` 为 \( L \) 个 lane、每 lane 每 repeat \( B \) 位，repeat 因子为 \( N \)，则：

\[ \text{搬运总量（32-bit 单元数）} = L \times N \times \frac{B}{32} \]

\[ \text{每线程寄存器数} = \frac{L \times N \times (B/32)}{32} \]

对 `16x128b`（\( L = 16 \)，\( B = 128 \)，\( B/32 = 4 \) 个 cell）：

\[ R_{\text{thread}} = \frac{16 \times N \times 4}{32} = 2N \]

即 `.x1`→2、`.x2`→4、`.x4`→8、`.x8`→16 个 32-bit 寄存器每线程。

#### 4.2.3 源码精读

章节正文以 `tcgen05.ld.sync.aligned.16x128b.x4.b32 {r0..r7}, [taddr]` 为例展开（[chapter_tmem/index.md:L107-L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L107-L135)）：

- 每个 TMEM lane 含 4 个 128-bit 组（`.x4` 的四次重复），每组 4 个 32-bit cell；
- 左侧总量 `16 lanes × 4 groups/lane × 4 cells/group = 256` 个 32-bit cell；
- 分给 32 个线程：`256 cells / 32 threads = 8` 个 32-bit 寄存器/线程，正好对上指令写的 `{r0..r7}` 八个目的寄存器；
- 把 `.x4` 改成 `.x1`，则搬 `16 × 1 × 4 = 64` 个 cell、每线程 2 个寄存器。

正文随后给出对照表：

> [chapter_tmem/index.md:L137-L142](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L137-L142)
>
> | 指令形式 | 每 lane 数据量 | 每线程寄存器 |
> | --- | ---: | ---: |
> | `.16x128b.x1` | 128 bits | 2 |
> | `.16x128b.x2` | 256 bits | 4 |
> | `.16x128b.x4` | 512 bits | 8 |
> | `.16x128b.x8` | 1024 bits | 16 |

并在 [chapter_tmem/index.md:L144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L144) 强调：这个数据搬运 shape 不同于 MMA 的 `M × N × K` 指令 shape——前者描述 TMEM 与寄存器之间的硬件搬运，后者给出矩阵乘的逻辑维度。

再看真实内核里的用量。GEMM Step 1 的 epilogue 中，每个线程要装下一整行 fp32 累加结果：

> [chapter_gemm_basics/index.md:L134-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L134-L143)
>
> `Dreg = T.alloc_local((BLK_N,), acc_type)` 为每线程分配 `BLK_N` 个 fp32 寄存器（`BLK_N = 128`，即每线程 128 个 32-bit 寄存器）；`Dreg.view(128, BLK_N, layout=TileLayout(S[(128, BLK_N):(1@tid_in_wg, 1)]))` 把 warpgroup 的 128 个线程与 128 个输出行一一对齐；`Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])` 即 lower 成 `tcgen05.ld`。

FA4 则反其道而行：softmax 读 S 时**故意分块**，把一整行 128 个 fp32 拆成四次 32 列的加载（[chapter_flash_attention/index.md:L369-L384](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L369-L384)），原文点明动机：「TMEM 加载被分块，而不是 softmax 计算被分块……保持每次 tile 操作的寄存器元组较小」。这正是 4.2 公式的工程应用：控制单次 ld 的目的寄存器数量。

#### 4.2.4 代码实践

**实践目标**：对任意 `.shape`/`.num` 组合，快速心算每线程寄存器用量。

**操作步骤**：

1. 用公式 \( R_{\text{thread}} = L \times N \times (B/32) / 32 \) 推导 `16x256b.x2` 与 `32x32b.x8` 两种形式（章节表格未列）的每 lane 数据量与每线程寄存器数。这两条是基于 [chapter_tmem/index.md:L109](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L109)「.shape 指定 lane 数与每 lane 基础数据量、.num 重复之」规则的推导，属**推导练习**而非书中原表。
2. 对照 GEMM Step 1：warpgroup 4 个 warp 各自窗口读 32 行 × `BLK_N=128` 列 fp32，算出每线程寄存器数，与 `T.alloc_local((BLK_N,), acc_type)` 对齐。
3. 回顾 FA4 的 `SOFTMAX_LD_CHUNK=32` 分块（[chapter_flash_attention/index.md:L372-L382](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L372-L382)），解释它把「一次 tile 操作的寄存器元组」控制在多大。

**需要观察的现象**：寄存器用量随 `.num` 线性增长；shape 里 lane 数翻倍、每 lane 位宽减半时总量不变。

**预期结果**（步骤 1，推导值）：`16x256b.x2`：每 lane \( 2 \times 256 \) bit = 512 bit，总量 \( 16 \times 2 \times 8 = 256 \) cell，每线程 8 个寄存器；`32x32b.x8`：每 lane \( 8 \times 32 \) bit = 256 bit，总量 \( 32 \times 8 \times 1 = 256 \) cell，每线程同样 8 个寄存器——两条不同路径殊途同归。步骤 2：\( 32 \times 128 \times 1 / 32 = 128 \) 个寄存器每线程，即 `Dreg` 一行。

#### 4.2.5 小练习与答案

**练习 1**：把 `.16x128b.x4` 改成 `.16x128b.x8`，目的寄存器列表要写几个？为什么？

**答案**：16 个（`{r0..r15}`）。总量翻倍为 \( 16 \times 8 \times 4 = 512 \) cell，除以 32 线程 = 16 个 32-bit 寄存器，与书中表格 `.16x128b.x8 → 16` 一致。

**练习 2**：`.shape` 与 MMA 的 `M × N × K` 有什么区别？

**答案**：`.shape` 描述 TMEM 与寄存器之间的**硬件搬运形状**——多少 lane 参与、每 lane 取多少位；MMA 的 `M × N × K` 描述矩阵乘的**逻辑维度**。寄存器里的数据如何对应逻辑矩阵元素，由 fragment 布局决定（u5-l1），不由 `.shape` 直接给出（[chapter_tmem/index.md:L144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L144)）。

**练习 3**：FA4 为什么不让 softmax 一次读满整行 128 个 fp32？

**答案**：一次读满会让单次 tile 操作的目的寄存器元组达到 128 个/线程，寄存器压力集中在一条指令上；分成四次 32 列加载后每次元组更小，FA4 给 WG0/WG1 的 200 寄存器预算需要同时容纳 `s_chunk_buf` 与 softmax 中间量（[chapter_flash_attention/index.md:L369-L384](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L369-L384)）。注意分块的是**加载**，softmax 本身仍处理完整行。

### 4.3 16-bit 打包：pack::16b、unpack::16b 与 fp16 视图

#### 4.3.1 概念说明

TMEM 的每个 cell 是 32 bit，`tcgen05.ld/st` 的寄存器操作数也是 32 bit，但内核常常处理 16-bit 数据（如 FA4 的 P 是 fp16）。为此：

- `tcgen05.ld` 上的 `.pack::16b`：把**相邻两个 TMEM 列**的两个 16-bit 数据片段打包进**一个 32-bit 寄存器**；
- `tcgen05.st` 上的 `.unpack::16b`：把**一个 32-bit 寄存器**拆成两个 16-bit 片段，写入**相邻两个 TMEM 列**。

关键认识：打包/解包**只改变数据在 TMEM 与寄存器之间搬动时的组织方式，不改变分配单位**——TMEM 仍沿 Column 维分配，每个分配到的列仍包含全部 128 个 Lane（[chapter_tmem/index.md:L150](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L150)）。

#### 4.3.2 核心流程

硬件视角下，一个 32-bit 物理列 \( p \) 的两半就是两个 16-bit 槽位：

```text
physical column p (32 bits)
┌────────────────┬────────────────┐
│ fp16 slot 2p   │ fp16 slot 2p+1 │
└────────────────┴────────────────┘
```

由此得到 fp16 视图索引与物理列的换算（设 fp16 视图列索引为 \( j \)）：

\[ \text{物理列} = \lfloor j / 2 \rfloor, \qquad \text{槽位} = j \bmod 2 \]

反过来，写入时 `.unpack::16b` 把寄存器的两个 16-bit 片段分别送往物理列 \( 2k \) 与 \( 2k+1 \) 对应的槽位——即「128 个 fp16 值填满 64 个物理列」。

#### 4.3.3 源码精读

章节定义：

> [chapter_tmem/index.md:L146-L150](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L146-L150)
>
> 这段规定：TMEM 的 cell 与寄存器操作数都是 32 bit；`tcgen05.ld` 的 `.pack::16b` 把相邻 TMEM 列的两个 16-bit 片段合进一个 32-bit 寄存器，`tcgen05.st` 的 `.unpack::16b` 把一个 32-bit 寄存器拆成两个 16-bit 片段写回相邻列；且打包/解包不改变分配单位。

最完整的真实现场在 FA4：同一块 TMEM 分配同时挂 fp32 与 fp16 两种视图。

> [chapter_flash_attention/index.md:L548-L571](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L548-L571)
>
> `tmem = tmem_pool.alloc((128, N_COLS_TMEM), "float32")` 之后 `tmem_as_f16 = tmem_pool.alloc((128, N_COLS_TMEM * 2), "float16")`——两块 buffer 每行位数相同（512×32 = 1024×16 = 16384 bit），所以 `tmem_as_f16` 是**同一 TMEM 行的另一种索引方案，不是第二次分配**。原文随后画出上面的 32-bit cell 双槽位示意图，并总结：`tmem[:, p]` 把整个 cell 当一个 fp32 寻址，`tmem_as_f16[:, 2p]` 与 `[:, 2p+1]` 寻址其中的两个 fp16 值。

随后 FA4 用 `rearrange` 造出分 stage 的视图（[chapter_flash_attention/index.md:L573-L583](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L573-L583)），并对 P0 给出换算链（[chapter_flash_attention/index.md:L585-L593](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L585-L593)）：

```text
P_region[0, 1, :, n]
    -> tmem_as_f16[:, 128 + n]       # col_start = 128
    -> physical column 64 + n // 2
```

即 P0 的第 0、1 个 fp16 落在物理列 64 的两个槽位，第 2、3 个落在物理列 65……128 个 fp16 填满物理列 `[64, 128)`。stage 1 同理：`384 + n → 192 + n//2`，P1 落在 `[192, 256)`。

这些区间最终被画成书图并由脚本固化：

> [img/scripts/gen_tmem_layout.py:L78-L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_layout.py#L78-L90)
>
> 脚本以 `tmem_s_base = 0, tmem_p_base = 64, tmem_o_base = 256` 为源常量绘制 S0/S1/P0/P1/O0/O1 各区间，并在 L83-L87 的注释里写明：P 经 fp16 视图寻址（源码用 `tmem_as_f16[:, tmem_col_p * 2 + ...]`），因此每个 128 列的 fp16 tile 只占 64 个物理 fp32 列——P0 标注为「phys 64-127 / f16 view 128-255」，P1 为「phys 192-255 / f16 view 384-511」。

时间维度上，P 与 S 的重叠是**复用**而非共存（[chapter_flash_attention/index.md:L616](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L616)）：QKᵀ MMA 先把完整 S0 写进物理列 `[0,128)`；softmax 把 S0 全部读进寄存器后，才把 128 个 fp16 的 P0 两个一列打包写进 `[64,128)`——覆盖掉不再需要的后 64 个 fp32 分数。这条纪律由哪些机制保障，正是 4.4 的主题。

#### 4.3.4 代码实践

**实践目标**：独立完成「fp16 视图列 → 物理列 + 槽位」的双向换算。

**操作步骤**：

1. 写一个五行 Python 脚本（**示例代码**，非项目原有）：
   ```python
   def phys(j):        # fp16 视图列 j -> (物理列, 槽位)
       return j // 2, j % 2

   # P_region[1, 1, :, n] -> tmem_as_f16[:, 384 + n] -> physical column 192 + n // 2
   for n in [0, 1, 2, 6, 127]:
       j = 384 + n
       print(n, phys(j), 192 + n // 2)
   ```
2. 核对输出：每个 \( n \) 的 `(j//2, j%2)` 中，`j//2` 必须等于 `192 + n//2`。
3. （可选，需 matplotlib）运行 `python img/scripts/gen_tmem_layout.py`，检查重新生成的 `img/tmem_layout_v3.png` 中 P0/P1 的区间标注与你的换算一致。**待本地验证**。

**需要观察的现象**：相邻两个 \( n \)（如 0 与 1）落在同一物理列的不同槽位；\( n \) 每隔 2 物理列才 +1。

**预期结果**：`n=0→(192,0)`、`n=1→(192,1)`、`n=2→(193,0)`、`n=6→(195,0)`、`n=127→(255,1)`；P1 整体覆盖物理列 `[192,256)`。

#### 4.3.5 小练习与答案

**练习 1**：`.pack::16b` 会改变 TMEM 的分配单位吗？

**答案**：不会。原文（[chapter_tmem/index.md:L150](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L150)）明确：打包/解包只改变数据在 TMEM 与寄存器之间搬动时的组织方式；TMEM 仍沿 Column 维分配，每个分配的列仍含全部 128 Lane。FA4 的 `tmem_as_f16` 也只是同一行的另一种索引方案，不是新分配。

**练习 2**：128 个 fp16 的 P0 为什么恰好占 64 个物理列、且起点选在 64？

**答案**：每两个 fp16 打包进一个 32-bit cell，128 个 fp16 → 64 列。起点 64 是 `tmem_p_base = 64`：P_region 选取的是 score stage 高半段的 fp16 视图（`tmem_as_f16[:, 128 + n]`，128 的 fp16 视图对应物理列 64），从而让 P0 时间上复用 S0 的后半段物理列 `[64,128)`（[chapter_flash_attention/index.md:L585-L616](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L585-L616)）。

**练习 3**：`tmem[:, 100]` 与 `tmem_as_f16[:, 200]`、`tmem_as_f16[:, 201]` 是什么关系？

**答案**：三者指向同一个 32-bit cell。`tmem[:, 100]` 把它作为一个 fp32 整体寻址；`tmem_as_f16[:, 200]` 与 `[:, 201]` 分别寻址它的低、高两个 fp16 槽位（\( 200 = 2 \times 100 \)，\( 201 = 2 \times 100 + 1 \)，依据 [chapter_flash_attention/index.md:L562-L571](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L562-L571)）。

### 4.4 wait::ld / wait::st：异步等待的正确位置

#### 4.4.1 概念说明

`tcgen05.ld` 与 `tcgen05.st` 都是**异步**的：指令发射后立即返回，数据尚未真正到达。两条等待纪律：

- **`tcgen05.wait::ld`**：在使用 load 的目的寄存器**之前**执行——保护的是「寄存器还没被填上」；
- **`tcgen05.wait::st`**：在依赖 store 已真正写入 TMEM 之前（例如要覆盖、或要通知别的 warp 来读）执行——保护的是「TMEM 写入尚未完成」。

两条 wait 各自覆盖**当前线程此前发出的全部**同类操作（[chapter_tmem/index.md:L154](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L154)），所以不必逐条等待；一批 ld 之后放一条 `wait::ld` 即可。

与 mbarrier 的分工值得注意：`wait::ld/st` 是**线程自己等自己**的轻量机制；若数据要交给**另一个线程或 warp** 消费，光等自己的异步操作完成还不够，还需线程同步（barrier arrive/wait）与适当的 `tcgen05.fence` 建立跨线程顺序（[chapter_tmem/index.md:L156](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L156)）。u8 单元将系统展开 mbarrier。

#### 4.4.2 核心流程

```text
# load 侧：wait::ld 挡在「用寄存器」之前
tcgen05.ld ...          # 异步：寄存器尚未就绪
tcgen05.wait::ld        # 等本线程此前全部 ld 完成
use registers           # 转换、计算、写 GMEM……

# store 侧：wait::st 挡在「依赖写入完成」之前
tcgen05.st ...          # 异步：TMEM 尚未真正更新
tcgen05.wait::st        # 等本线程此前全部 st 完成
barrier.arrive()        # 通知消费者（跨线程，还需 fence/同步配合）
```

放错位置的两种典型故障：把 `wait::ld` 放到「使用寄存器」之后 → 读到未定义值；把 `wait::st` 省略、直接 arrive → 消费者（如 PV MMA）读到旧的 TMEM 内容。

#### 4.4.3 源码精读

章节规定（[chapter_tmem/index.md:L152-L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L152-L158)）：ld 之后、使用目的寄存器之前执行 `tcgen05.wait::ld`；st 之后、依赖写入完成之前用 `tcgen05.wait::st`；每个 wait 分别覆盖当前线程此前发出的全部同类操作；跨线程消费还需线程同步与 `tcgen05.fence`。

**真实内核一：GEMM Step 1 的 wait::ld。**

> [chapter_gemm_basics/index.md:L134-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L134-L143)
>
> ```python
> Dreg_wg = Dreg.view(128, BLK_N, layout=TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)]))
> Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])   # lower 成 tcgen05.ld
> T.ptx.tcgen05.wait.ld()                            # ← 挡在使用寄存器之前
> Tx.cast(Dreg_f16[:], Dreg[:])                      # 使用 Dreg：fp32 -> fp16
> Tx.copy(D[m_thr, n_st:n_st+BLK_N], Dreg_f16[:])    # 写 GMEM
> ```
>
> [chapter_gemm_basics/index.md:L155](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L155) 的原文点明因果：「`Tx.wg.copy_async(Dreg_wg, tmem)` 经该视图读累加器并 lower 成 Blackwell 的 TMEM load 指令 `tcgen05.ld`。load 是异步的，所以在任何线程使用 `Dreg` 之前必须先完成 `T.ptx.tcgen05.wait.ld()`」。若把 wait 挪到 `Tx.cast` 之后，cast 读到的就是未填充的寄存器。

**真实内核二：FA4 的 wait::st——写完 P 才能放行 PV MMA。**

> [chapter_flash_attention/index.md:L443-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L443-L459)
>
> ```python
> for i in T.unroll(P_SPLIT_Q):
>     Tx.wg.copy_async(
>         P_region[wg_id, 1, :, i * BLK_N // 4 : (i + 1) * BLK_N // 4],
>         p_chunk[:, i * BLK_N // 4 : (i + 1) * BLK_N // 4],
>     )                       # 寄存器 -> fp16 TMEM 视图，lower 成 TMEM store
> T.ptx.tcgen05.wait.st()     # ← 等本线程此前全部 st 完成
> p_o_rescale.arrive(wg_id)   # 然后才 arrive，放行等这批 P 的消费者
> ```
>
> 这里 `wait.st` 与 `arrive` 的先后就是纪律本身：arrive 之后等待方（校正/回写路径）即可读取 P，若省略 wait，PV MMA 可能读到尚未写入（或旧）的 TMEM 内容。原文在 [chapter_flash_attention/index.md:L620](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L620) 把它写进整体协议：「写 P 时，`tcgen05.wait::st` 先等异步 TMEM store 完成，此后 softmax 线程才在 `p_o_rescale` 或 `p_ready_2` 上 arrive；PV MMA 等到匹配的 barrier 后才读」。

**真实内核三：FA4 的 O 校正循环同样是 ld→改→st→wait。**

> [chapter_flash_attention/index.md:L725-L731](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L725-L731)
>
> `Tx.wg.mul(o_row, o_row, acc_scale)` 之后把 `o_row` 经 `Tx.wg.copy_async` 写回 `O_region`，紧跟 `T.ptx.tcgen05.wait.st()`——缩放后的 O 必须真正落回 TMEM，后续 PV MMA 的累加才有正确初值。

另外两点收尾：其一，SMEM→TMEM 的 `tcgen05.cp` 用另一套 shape 与另一套完成机制，其 scale-factor 用途已在 u5-l3 / u7-l2 讲过（[chapter_tmem/index.md:L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L158)）；其二，章节结尾给出读 TMEM 内核的四步检查清单——分配/释放了多少列、当前 warp 的 Lane 窗口、每条 ld/st 的 `.shape`/`.num` 产生多少寄存器、对应异步操作是否已完成（[chapter_tmem/index.md:L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L160)）——本讲四个模块恰好逐一对应这四问。

#### 4.4.4 代码实践

**实践目标**：给一段同时含 ld 与 st 的伪内核标注两条 wait 的必经位置。

**操作步骤**：

1. 阅读下面的伪内核（**示例代码**，依 FA4 softmax 缩影改写，变量名沿用正文）：
   ```python
   # 前置：s_ready 屏障已证明 S 写入 TMEM 完成
   for chunk_idx in T.unroll(BLK_N // SOFTMAX_LD_CHUNK):
       Tx.wg.copy_async(s_chunk[:, ...], S_region[wg_id, :, ...])   # (A) tcgen05.ld
   # (位置 1)
   tile_max = row_max_merge(s_chunk_buf)        # (B) 使用 s_chunk_buf
   p_chunk = exp2(s_chunk_buf - row_max_safe)   # (C) 使用 s_chunk_buf
   for i in T.unroll(P_SPLIT_Q):
       Tx.wg.copy_async(P_region[wg_id, 1, :, ...], p_chunk[:, ...])  # (D) TMEM store
   # (位置 2)
   p_ready.arrive(wg_id)                        # (E) 通知 PV MMA 读 P
   ```
2. 回答：位置 1、位置 2 各应放什么指令？为什么？
3. 追问：把位置 2 的指令去掉，直接 `arrive`，PV MMA 会出什么问题？把位置 1 的指令挪到 (C) 之后呢？

**需要观察的现象**：两个位置保护的资源不同——位置 1 保护本线程的寄存器，位置 2 保护 TMEM 的写入可见性（跨 warp 消费）。

**预期结果**：位置 1 填 `T.ptx.tcgen05.wait.ld()`——(B)(C) 要读 `s_chunk_buf`，必须等本线程此前全部 ld 完成；位置 2 填 `T.ptx.tcgen05.wait.st()`——arrive 之后消费者即可读 P，必须先等 st 真正写入 TMEM。去掉位置 2：PV MMA 可能读到旧值/未写入值（错误结果）；位置 1 挪到 (C) 后：`exp2` 读到未填充寄存器。与真实内核 [chapter_flash_attention/index.md:L443-L448](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L443-L448) 的写法一致。

#### 4.4.5 小练习与答案

**练习 1**：连发 4 条 `tcgen05.ld` 之后要用数据，需要 4 条 `wait::ld` 吗？

**答案**：不需要。`tcgen05.wait::ld` 覆盖当前线程**此前发出的全部** ld（[chapter_tmem/index.md:L154](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L154)），FA4 的四次 32 列加载之后就只放一条 wait。st 同理。

**练习 2**：`wait::st` 之后马上 `barrier.arrive()`，消费者的顺序就绝对安全了吗？

**答案**：wait 只解决「我写完了」；arrive/wait 解决「消费者知道我写完了」。原文（[chapter_tmem/index.md:L156](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L156)）还要求「适当的 `tcgen05.fence`」来建立跨线程顺序——这与 u7-l1 中消费 MMA 结果前先 `tcgen05.fence::after_thread_sync` 是同一家族的要求。FA4 的完整协议还依赖发起线程把 PV MMA 与下一个 QKᵀ MMA 排成固定的 `tcgen05` 序列（[chapter_flash_attention/index.md:L620](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L620)）。

**练习 3**：GEMM Step 1 的 epilogue 里为什么看不到 `wait::st`？

**答案**：因为它的 epilogue 只做了 TMEM→寄存器→GMEM 单向流动：`tcgen05.ld` 之后写 GMEM 走的是 `Tx.copy`（普通存储），写完 GMEM 的完成问题由后续 `T.cuda.cta_sync()` 等机制处理（见 [chapter_gemm_basics/index.md:L262-L268](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L262-L268)）。`wait::st` 只在「寄存器写回 TMEM」时才需要——那是 FA4 这类「中间结果回 TMEM 再喂下一个 MMA」的内核才有的形态（P 写回、O 校正写回）。

## 5. 综合实践

**任务：为一段「FA4 softmax 缩影」内核补全打包换算与同步标注。**

下面是一段整合本讲全部内容的伪内核（**示例代码**，依据 [chapter_flash_attention/index.md:L369-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L369-L459) 与 [L548-L616](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L548-L616) 改写）：

```python
# TMEM 分配：fp32 主视图 + fp16 别名视图（同一块物理存储）
tmem        = tmem_pool.alloc((128, 512), "float32")
tmem_as_f16 = tmem_pool.alloc((128, 1024), "float16")
S_region = tmem.rearrange("m (s n) -> s m n", n=128)
P_region = tmem_as_f16.rearrange("m (s two n) -> s two m n", two=2, n=128)

s_ready.wait(phase)                          # QKᵀ MMA 已把 S0 写进物理列 [0,128)
for c in T.unroll(4):                        # 分 4 次、每次 32 列读整行
    Tx.wg.copy_async(s_chunk[:, c*32:(c+1)*32],
                     S_region[0, :, c*32:(c+1)*32])       # → tcgen05.ld
# 【甲】← 此处缺一条指令
softmax_and_exp2(s_chunk_buf)                # 寄存器内完成行级 softmax
for i in T.unroll(P_SPLIT_Q):
    Tx.wg.copy_async(P_region[0, 1, :, i*32:(i+1)*32],
                     p_chunk[:, i*32:(i+1)*32])           # → TMEM store（unpack 16-bit）
# 【乙】← 此处缺一条指令
p_ready.arrive(wg_id)                        # 放行 PV MMA 读 P
```

请完成：

1. **补指令**：写出【甲】【乙】两处应填的 TIRx 语句，并各用一句话说明保护的资源。
2. **打包推导**：写出 `P_region[0, 1, :, n]` 到物理列的一般公式；据此给出 P0 覆盖的物理列区间，并解释为什么该区间与 S0 的后半段重叠是安全的。
3. **寄存器核算**：每次 32 列的 `tcgen05.ld` 分块为每线程带来多少个 32-bit 目的寄存器？若改成一次读满 128 列呢？
4. **图验证**（可选，需 matplotlib）：运行 `python img/scripts/gen_tmem_layout.py` 与 `python img/scripts/gen_tcgen05_ldst.py`，对照生成的 `img/tmem_layout_v3.png` 与 `img/tcgen05_ldst.svg` 检查第 2 问与 lane 映射。**待本地验证**。

**参考答案**：

1. 【甲】`T.ptx.tcgen05.wait.ld()`——保护本线程的目的寄存器：softmax 要读 `s_chunk_buf`，必须等此前全部 ld 完成。【乙】`T.ptx.tcgen05.wait.st()`——保护 TMEM 写入可见性：arrive 后 PV MMA 即可读 P，必须先等 st 真正写进 TMEM。
2. 公式：\( j = 128 + n \)（fp16 视图列），物理列 \( = \lfloor j/2 \rfloor = 64 + \lfloor n/2 \rfloor \)，槽位 \( = j \bmod 2 \)。P0 覆盖 `[64, 128)`。安全的原因是**时间上不共存**：s_ready 保证 S0 已完整写入；softmax 先把 S0 全部读进寄存器（甲处 wait 之后），P0 才覆盖后半段——此时那 64 个 fp32 分数已不再需要；PV MMA 等 p_ready 之后才读 P。
3. 每次 32 列：warpgroup 内每线程持一行的一段，32 个 fp32 → 32 个 32-bit 寄存器/线程；一次读满 128 列则需 128 个/线程（这正是 FA4 选择 `SOFTMAX_LD_CHUNK=32` 分块以缩小单次寄存器元组的原因）。
4. 图中 P0 标注为 phys 64-127 / f16 view 128-255，P1 为 phys 192-255 / f16 view 384-511，与第 2 问公式一致；`tcgen05_ldst.svg` 右侧 fragment 的 lane→数据映射为 `lane l → row l/4, cols 2·(l%4),+1`。

## 6. 本讲小结

- `tcgen05.ld/st` 是 **warp 集体指令**：32 个线程执行同一条指令、给同一个 `[taddr]`，硬件按 lane ID 分发数据；配合 u7-l3 的 Lane 窗口规则，读 128-Lane 累加器需 4 个 warp 各访问一次。
- 搬运量由 `.shape`（lane 数 × 每 lane 基础位宽）与 `.num`（重复次数）决定，每线程寄存器数 \( = L \times N \times (B/32) / 32 \)；`.16x128b.xN` 即每线程 \( 2N \) 个 32-bit 寄存器。这是硬件搬运形状，别与 MMA 的 `M×N×K` 混淆。
- TMEM cell 恒为 32 bit：`tcgen05.ld` 的 `.pack::16b` 把相邻两列的 16-bit 拼进一个寄存器，`.unpack::16b` 反向拆分；FA4 用 `tmem` / `tmem_as_f16` 双视图寻址同一块物理存储，换算公式为物理列 \( \lfloor j/2 \rfloor \)、槽位 \( j \bmod 2 \)，128 个 fp16 恰占 64 列。
- 异步纪律：**用寄存器之前 `tcgen05.wait::ld`，依赖写入完成之前 `tcgen05.wait::st`**；每个 wait 覆盖当前线程此前全部同类操作。GEMM Step 1 只需前者（单向读回），FA4 两者都需要（P 写回、O 校正）。
- 跨线程消费光有 wait 不够：还需 barrier arrive/wait 与适当的 `tcgen05.fence` 建立顺序——这正引出下一单元的 mbarrier。

## 7. 下一步学习建议

- **下一讲 u8-l1（mbarrier）**：本讲反复出现的 `s_ready.wait` / `p_ready.arrive` 就是 mbarrier 的使用现场——arrive/wait 分离与字节追踪机制将解释「wait 之前谁必须到达、到达几次」。
- **回读真实内核**：带着本讲的四步检查清单（[chapter_tmem/index.md:L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L160)）重读 [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) 的 Step 1–3 epilogue 与 [chapter_flash_attention/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md) 的 softmax/correction 段落，逐一核对每条 `wait` 的位置。
- **单元九衔接**：u9 将进入 TIRx 编程模型，届时你会看到本讲的 `T.ptx.tcgen05.wait.ld()`（底层 PTX 辅助调用）与 `Tx.wg.copy_async`（tile 操作）两个层级的分工如何构成 scope/layout/dispatch 三要素中的 dispatch 一面。
