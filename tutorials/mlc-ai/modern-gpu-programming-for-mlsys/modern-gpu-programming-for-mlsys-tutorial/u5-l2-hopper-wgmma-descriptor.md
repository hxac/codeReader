# u5-l2 Hopper：wgmma 与共享内存描述符

## 1. 本讲目标

本讲是「Tensor Core 布局三代演进」单元的第二讲，进入 Hopper 世代。学完本讲你应该能够：

1. 说明 `wgmma.mma_async` 的协作方式：一条 MMA 由一个 warpgroup（4 个 warp、128 线程）共同执行，SS 与 RS 两种形式分别对应 A 来自 SMEM 还是寄存器，而 B 永远来自 SMEM。
2. 解析矩阵描述符（matrix descriptor）的各字段含义：这是一个装在寄存器里的 64 位值，记录起始地址、leading/stride 两个字节偏移、base offset 与 swizzle 模式；三个地址字段一律以 16 字节为单位编码。
3. 对比「寄存器累加器」与「SMEM 直接读」的差异：Hopper 把输入布局的描述从内核手写地址升级为描述符，但 C/D 累加器仍分布在每线程寄存器中，内核因此同时维护两套布局表示。
4. 写一个 Python 函数，从（SMEM 基址、leading byte offset、stride byte offset、swizzle 模式）算出各字段编码值并拼出描述符——这是本讲规格指定的代码实践。

## 2. 前置知识

本讲直接承接 u5-l1（Ampere fragment 与 ldmatrix），并复用 u4-l4（swizzle）的结论。需要的概念如下：

- **warpgroup**：4 个连续 warp，共 128 线程（u2-l1）。Hopper 把它引入为发起 warpgroup 级 MMA 的单位；128 线程恰好与 TMEM 的 128 lane 对齐（u2-l2），这条对应关系要到 Blackwell 才真正兑现，本讲先记住「4 warp = 128 线程」。
- **寄存器 fragment**（u5-l1）：矩阵 tile 分散在各线程寄存器里的切片。Ampere 的 A/B/C/D 全是 fragment；`ldmatrix` 负责「warp 协同、少数 lane 出地址、全体 lane 收数据」地把 SMEM 数据装进 fragment。
- **XOR swizzle 与 atom**（u4-l4）：SWEM 按 4 字节粒度分 32 个 bank；swizzle 用「sector 异或行号」重排地址，让行读与列读都无冲突。`SWIZZLE_128B` 的 atom 是 8 行 × 128 B 的最小地址重复单元，共 1024 B；`SWIZZLE_64B`/`SWIZZLE_32B` 的 atom 分别是 8 × 64 B 与 8 × 32 B。
- **「放错位置 = 算错结果」**（u5-l1 引过的章首论断）：Tensor Core 指令按固定硬件规则解释寄存器、SMEM 地址与 TMEM 坐标，位置错了硬件不会报错，只会把元素当成另一个元素。

不熟悉 Hopper 架构的读者只需记住一句话：Hopper（sm_90）在 Ampere 之上做了两件事——把 MMA 的发起单位从 warp 放大到 warpgroup，并允许 Tensor Core 经描述符直接消费 SMEM。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [chapter_layout_generations/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md) | 本章正文，Hopper 一节位于 L180–L221，三代对比表位于 L299–L309 |
| [zh/chapter_layout_generations/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_layout_generations/index.md) | 上述章节的中文镜像（u1-l2 的 zh 前缀约定） |
| [img/scripts/gen_smem_descriptor.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py) | 生成 K-major 128B swizzle 下描述符示意图的脚本，docstring 与图注写明了 atom、ldo、sdo、start_address 的编码规则 |
| [img/scripts/README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md) | 图表脚本运行说明（依赖 matplotlib、numpy） |
| [chapter_data_layout/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md) | u4-l4 的 swizzle 章节，本讲引用其 sector/atom 定义（L513–L521、L531–L532） |
| [chapter_background/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md) | 执行层级章节，warpgroup 定义与「Hopper 引入 wgmma」的表述位于 L48–L51、L138 |
| [chapter_tensor_cores/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md) | Tensor Cores 章节，L50 以「单线程语义」对照 `wgmma` 的 warpgroup 协作发起 |
| [chapter_tma/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md) | TMA 章节，「描述符必须与 SMEM 实际摆法一致」的通用表述位于 L48 |
| [chapter_tirx_layout_api/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md) | TIRx Layout API 章节，L470 说明内核不应手推这些参数、应让 dtype 与描述符模式代劳 |

## 4. 核心概念与源码讲解

### 4.1 wgmma 四 warp 协作：从 warp 到 warpgroup

#### 4.1.1 概念说明

Hopper 的主 Tensor Core 接口是 `wgmma.mma_async`。它相对 Ampere 有两个变化，正文把它们分得很清楚：

1. **协作范围变大**：MMA 从一个 warp 放宽到一个 warpgroup——4 个连续 warp、128 线程一起执行一条 `wgmma.mma_async`。
2. **输入路径变化（更重要）**：B 一律经**矩阵描述符**从 SMEM 直接读取；A 可以同样来自 SMEM，也可以来自寄存器。两种组合习称 SS 与 RS：

```text
SS: A from SMEM, B from SMEM -> wgmma -> register accumulator
RS: A from registers, B from SMEM -> wgmma -> register accumulator
```

对 SMEM 操作数，内核不再需要 `ldmatrix` 去拼 A/B 的寄存器 fragment——WGMMA 自己读数据，但它得知道矩阵从哪开始、数据组之间隔多远、SMEM 里用的是哪种 swizzle。携带这些信息的载体就是矩阵描述符。

为什么说这个变化「更重要」？回想 u5-l1 的结论：Ampere 的 swizzle 活在内核手写的地址计算里，`ldmatrix` 对它一无所知。Hopper 把「布局的描述」从内核代码搬进了一个数据结构，Tensor Core 按描述符自己算地址——这是「布局即元数据」的第一步，Blackwell 的 TMA tensor map 沿同一条思路走得更远。

另外注意指令名里的 `mma_async`：它是异步的，发起与完成是分开的两件事。执行层级章节在讲三代 MMA 演进时正是以「Hopper 引入异步 warpgroup MMA」概括这一代，见 [chapter_background/index.md:L138](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L138)。

#### 4.1.2 核心流程

一条 SS 形式的 `wgmma.mma_async` 从准备到完成：

1. 内核把 A、B tile 写入 SMEM，物理摆放（含 swizzle 模式）由内核决定。
2. 内核为 A、B 各构造一个 64 位矩阵描述符（字段见 4.2），装入寄存器。
3. warpgroup 的 128 个线程协同发起一条 `wgmma`，指令参数里带描述符（或 A 的寄存器 fragment，RS 形式）与 C/D 的寄存器 fragment 位置。
4. Tensor Core 按描述符直接从 SMEM 读 A/B，做 \( D = AB + C \)。
5. D 写回 warpgroup 各线程的寄存器 fragment，由 epilogue 消费。

角色分工与 Ampere 的对照（三代对比表中的 Hopper 行）：

| 维度 | Ampere `mma.sync` | Hopper `wgmma.mma_async` |
|---|---|---|
| 发起/执行单位 | 1 个 warp（32 线程） | 1 个 warpgroup（4 warp，128 线程） |
| A 的来源 | 寄存器 fragment | SMEM（经描述符）或寄存器 |
| B 的来源 | 寄存器 fragment | 一律 SMEM（经描述符） |
| C/D 位置 | 寄存器 fragment | 寄存器 fragment（不变） |
| SMEM 布局怎么表达 | 内核显式计算地址与 swizzle | 描述符记录步长与 swizzle 模式 |

#### 4.1.3 源码精读

- Hopper 一节的总起，见 [chapter_layout_generations/index.md:L180-L193](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L180-L193)：这段写明「Hopper 把 MMA 从一个 warp 放宽到一个 warpgroup；4 个连续 warp、128 线程共同执行 `wgmma.mma_async`」，随后指出更重要的变化是输入路径——B 经描述符来自 SMEM，A 可来自 SMEM 或寄存器，即 SS/RS 两种形式（代码块在 [L188-L191](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L188-L191)）；最后一句点题：对 SMEM 操作数不再用 `ldmatrix` 拼 fragment，但指令需要知道矩阵起点、数据组间距与 swizzle 模式，「矩阵描述符携带这些信息」。
- warpgroup 的定义与历史定位，见 [chapter_background/index.md:L48-L51](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L48-L51)：**warpgroup = 4 个连续 warp = 128 线程；warpgroup 这一概念正是 Hopper 为发起 warpgroup 级 MMA（`wgmma`）而引入的**，Blackwell 上它的 4 个 warp 还能各自覆盖一个 32-lane TMEM 窗口（u7-l3 会用到）。
- 章首 overview 的三句浓缩，见 [chapter_layout_generations/index.md:L4-L10](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L4-L10)：Hopper 句为「`wgmma.mma_async` 可经矩阵描述符直接从 SMEM 读输入，但累加器仍分布在每线程寄存器中」——前半句是 4.2 的主题，后半句是 4.3 的主题。
- 章末对比表的 Hopper 行，见 [chapter_layout_generations/index.md:L299-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L299-L309)：A 可来自寄存器或 SMEM、B 来自 SMEM、累加器在寄存器、「SMEM 布局由矩阵描述符记录步长与 swizzle 模式」。
- 一个有用的前瞻对照：Tensor Cores 章在讲 `tcgen05.mma` 执行方式时特意拿三代发起语义作比——「与 Ampere 的 `mma.sync` 和 Hopper 的 `wgmma.mma_async` 不同，`tcgen05.mma` 是单线程语义：一个被选出的线程发起指令，硬件启动整个 tile 级 MMA，其余线程不必各自提交同一指令的副本」，见 [chapter_tensor_cores/index.md:L50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L50)。反过来说，`wgmma` 的「warpgroup 协作」意味着 128 个线程要一起把这条指令提交上去。

#### 4.1.4 代码实践

**实践目标**：把「谁发起、输入从哪来、结果到哪去」整理成一张跨代对照表，并核对仓库中所有提到 wgmma 的位置，确认自己对协作方式的理解与全书表述一致。

**操作步骤**：

1. 通读 [chapter_layout_generations/index.md:L180-L193](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L180-L193)，抄下 SS/RS 两行数据路径。
2. 在仓库根目录执行下面的只读搜索，逐个打开命中处，记录每处对 wgmma 的说法（哪一章、强调什么）：

   ```bash
   grep -rn "wgmma" --include="*.md" . | grep -v tutorial | grep -v "/zh/"
   ```

3. 做一个算术核对：warpgroup = 4 warp × 32 lane = 128 线程；再对照 u2-l2 讲过的 TMEM「128 lane × 最多 512 列」，写一句说明为什么 128 这个数会在 Hopper 出现、在 Blackwell 兑现。
4. 把 4.1.2 的对照表补上第三列 Blackwell（只填你能从 [L299-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L299-L309) 对比表确认的内容，不确定的留白到 u5-l3 再补）。

**需要观察的现象**：`wgmma` 一词的命中只应有三处——本章（chapter_layout_generations，L8/L184/L189–L190/L293/L304）、chapter_background（L49 与 L138）、chapter_tensor_cores（L50，拿它与 `tcgen05.mma` 的单线程语义对照）。另一些章节（chapter_tma、chapter_data_layout 等）谈的是「描述符必须与摆法一致」这一原则，但不含 `wgmma` 字样——用 `grep -rn "descriptor"` 才能搜到它们。三处 wgmma 表述互相一致：warpgroup 级、异步、B 恒为 SMEM。

**预期结果**：你的对照表中 Hopper 列与 4.1.2 的表完全一致；第 3 步能写出「128 线程的 warpgroup 与 TMEM 的 128 lane 同宽，Blackwell 让 4 个 warp 各读 32-lane 窗口」这类表述（依据 [chapter_background/index.md:L48-L51](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L48-L51) 与 u2-l2）。若某些 grep 结果在你环境不可复现，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：SS 与 RS 各是什么的缩写？哪一侧在两种形式里都不变？

**答案**：指 A/B 两个操作数的来源——SS 是 A、B 都来自 SMEM（shared），RS 是 A 来自寄存器（register）、B 来自 SMEM。两种形式里 B 恒为 SMEM，可变的只有 A（见正文 [L186-L191](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L186-L191)、[L221](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L221)）。

**练习 2**：为什么说「输入路径变化」比「warp 放大到 warpgroup」更重要？

**答案**：因为它改变了内核的职责边界：Ampere 内核必须用 `ldmatrix` 把 SMEM 数据拼成 fragment、自己手写 swizzle 地址；Hopper 内核只需构造描述符，Tensor Core 直接消费 SMEM。布局的描述从代码升级为数据结构（见正文 [L186-L193](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L186-L193)）。

**练习 3**：`wgmma` 下内核还需要为 B 准备 `ldmatrix` 吗？

**答案**：不需要。B 恒走 SMEM 描述符路径，WGMMA 直接读取；需要 fragment 的是 RS 形式里的 A 和 C/D 累加器（见正文 [L193](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L193)、[L221](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L221)）。

### 4.2 矩阵描述符：装在寄存器里的 64 位「寻址配方」

#### 4.2.1 概念说明

矩阵描述符是一个 **64 位、装在寄存器里的值**。可以把它理解成一张写给 Tensor Core 的寻址配方：矩阵本体留在 SMEM，描述符告诉 `wgmma` 去哪找这个 tile、如何在其中前进。正文给出五个字段：

| 字段 | 含义 |
|---|---|
| `matrix start address` | 矩阵在 SMEM 中的起点 |
| `leading dimension byte offset`（`ldo`） | 沿 leading 维走到下一个数据组所用的字节偏移 |
| `stride dimension byte offset`（`sdo`） | 沿 stride 维走到下一个数据组所用的字节偏移 |
| `matrix base offset` | 矩阵起点在重复 swizzle 图案内部的位置 |
| `swizzle mode` | 无 swizzle，或 32B / 64B / 128B swizzling |

三条使用规则：

1. **编码单位是 16 字节**：三个地址字段（start、`ldo`、`sdo`）都以 16 B 为单位编码——等价于把字节地址右移 4 位再填入。示意图脚本在 start_address 的标注里直接写了 `addr ≫ 4`。
2. **major mode 与 swizzle mode 共同决定哪个矩阵方向是 leading、哪个是 stride**。
3. **swizzle mode 决定 atom 形状与 atom 内部的 XOR 置换**：atom 是 u4-l4 讲过的最小地址重复单元，`SWIZZLE_128B` 的 atom 是 8 行 × 128 B（1 KB 连续块）。

对最常用的 **swizzled K-major 布局**，正文给了两条特化规则：`ldo` 使用**固定编码 1**（K 方向遵循固定的 atom 内布局，无须额外步长）；`sdo` 给出**从一个 8 行组到下一个 8 行组的字节偏移**（即沿 M 方向跳过一个 atom）。`matrix base offset` 在起点与图案边界对齐时取 0。

#### 4.2.2 核心流程

WGMMA 用描述符定位一个 tile 的过程：

1. 从 `matrix start address` 出发（解码时左移 4 位还原字节地址）。
2. 沿 K（leading 维）按固定的 atom 内布局前进——K-major swizzled 下这就是 `ldo = 1` 的含义。
3. 沿 M（stride 维）用 `sdo` 跳到下一个 8 行组，即下一个 atom：每个 atom 是 1 KB 连续块，内部 8 行 × 128 B 背靠背排列。
4. 进入目标 atom 后，由 swizzle mode 决定 atom 形状与其中每个 16 B sector 的 XOR 置换，从而确定字节位置。

写成伪代码（针对 K-major 128B swizzle、fp16、每行恰 64 个元素=128 B 的情形）：

```text
atom_idx   = m // 8                 # 沿 M 的第几个 8 行组
row_in_atom= m % 8
byte_addr  = start + atom_idx * sdo_bytes      # sdo 编码值 * 16
             + row_in_atom * 128               # atom 内 8 行背靠背
             + ((2*k) // 16 XOR row_in_atom) * 16   # XOR swizzle 重排 sector
             + (2*k) % 16                      # sector 内偏移
```

一个直接推论（正文用整段强调）：**描述符必须与 SMEM 里的字节一致**。如果 TMA 用 128B swizzle 写入 tile，WGMMA 的描述符就必须按同样的 128B swizzle 去解释它。TMA 与 WGMMA 用的是两条不同指令各自的描述符，但二者必须描述同一份物理摆法。TMA 章节把同一条原则表述为「swizzle 改变物理排列而不改变逻辑内容；TMA 描述符、SMEM tile 布局与后续 MMA 指令必须描述同一物理排列，否则字节到了 SMEM，Tensor Core 却把它们当成错误的矩阵元素」。

做个数量级演算帮助建立直觉（算术演算，非书中数据）：A tile 取 (M=64, K=64) fp16、K-major `SWIZZLE_128B`。每行 64×2 B = 128 B，恰好一个 atom 行宽；M=64 ⇒ 沿 M 堆 8 个 atom。于是：

- atom 尺寸 8 × 128 B = 1024 B ⇒ `sdo` = 1024 B，编码值 \( 1024 / 16 = 64 \)；
- K 方向不超过一个 atom 行宽 ⇒ `ldo` 固定编码 1；
- tile 起点若与 atom 边界对齐 ⇒ `matrix base offset` = 0。

#### 4.2.3 源码精读

- 描述符的总定义与字段表，见 [chapter_layout_generations/index.md:L195-L207](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L195-L207)：开头一句「矩阵描述符是装在寄存器里的 64 位值，把它想成 WGMMA 的寻址配方」；字段表（L199–L205）列出上表五个字段；L207 写明「WGMMA 从 start address 出发，用 `ldo` 与 `sdo` 到达后续数据组；**三个地址字段都以 16 字节为单位编码**；major mode 与 swizzle mode 决定 leading/stride 各对应哪个矩阵方向」。
- K-major 特化规则与示意图讲解，见 [chapter_layout_generations/index.md:L209-L215](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L209-L215)：「swizzled K-major 布局下 `ldo` 用固定编码 1，`sdo` 给出从一个 8 行组到下一个的字节偏移；swizzle mode 决定 atom 形状与 atom 内的 XOR 置换；base offset 在起点对齐图案边界时为 0」。随后以 A 操作数、K-major 128B swizzle 为例：K 水平、M 垂直、每个色块是一个「8 行 × 128 B 行」的 atom，左上黑点即 start_address。最后一段是关键的一致性论述：「**描述符必须与 SMEM 中的字节一致**——TMA 用 128B swizzle 写入，WGMMA 描述符就必须按 128B swizzle 解释；两条指令的描述符各自独立，但描述的是同一物理摆法」。
- 示意图脚本的 docstring 是一份浓缩说明书，见 [img/scripts/gen_smem_descriptor.py:L1-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L1-L9)：参数即 `ptx_*_encode_matrix_descriptor` 风格的（start_address、ldo、sdo、swizzle/layout_type）；「操作数是 swizzle atom 的二维网格；swizzle 格式与 major mode 决定 atom 形状与 XOR 图案；**每个 atom 是一个连续的 8 × 128 B（1 KB）块**；K-major swizzled 布局下 ldo 用固定编码 1、sdo 在 8 行组之间前进」。
- 脚本正文中的图面标注逐条对应正文规则：atom 标签 `atom 8 × 128 B` 见 [L43-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L43-L44)；第一个 atom 内部的连续性标注（8 行背靠背、byte 0 → +896 B、「contiguous 1 KB」）见 [L48-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L48-L56)；**start_address 的编码 `addr ≫ 4`** 见 [L64-L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L64-L65)；`ldo = 1 (fixed for K-major swizzle)` 见 [L67-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L67-L69)；`sdo`（next 8 rows, M 方向）的双向箭头见 [L71-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L71-L75)；「swizzle 格式决定 atom 形状（此处 8×128 B，否则 64/32/16 B）与其中 XOR 图案」见 [L77-L79](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L77-L79)；两条脚注（atom 连续性、sdo 与 XOR 分工）见 [L81-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L81-L84)。
- atom 与 sector 的原始定义在 u4-l4 章节：16 B sector、`SWIZZLE_128B` 每行 8 个 sector 共 128 B、atom 总尺寸 8 × 128 B = 1024 B，见 [chapter_data_layout/index.md:L513-L521](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L513-L521)；`SWIZZLE_64B`/`SWIZZLE_32B` 的 atom 分别为 8 × 64 B、8 × 32 B，见 [L531-L532](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L531-L532)。
- 「描述符必须与实际摆法一致」的 TMA 侧表述，见 [chapter_tma/index.md:L48](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L48)。
- 面向未来的告慰：TIRx Layout API 章节明确说「内核通常**不应**手推这些参数，数据类型与描述符模式会代为选择配置；真正的要求是 TIRx layout、TMA 描述符与 MMA 三者对 SMEM 摆法达成一致」，见 [chapter_tirx_layout_api/index.md:L470](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L470)。也就是说：理解本讲的字段是为了看懂机制，写内核时这层会被 u10 的 Layout API 接管。

#### 4.2.4 代码实践

**实践目标**：本讲规格指定的任务——解析 wgmma 矩阵描述符的位域布局，写一个 Python 函数从（SMEM 基址、leading byte offset、stride byte offset、swizzle 模式）生成各字段值并拼出描述符。

先划清事实边界，避免编造：本书正文只规定了（a）五个字段及含义（[L199-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L199-L205)）、（b）三个地址字段以 16 字节为单位编码（[L207](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L207)）、（c）start_address 的编码是 `addr ≫ 4`（脚本 [L64-L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L64-L65)）。**字段到比特位的精确分配本书未给出**；下面函数里的位号按 PTX ISA 文档中 wgmma/tcgen05 共享内存描述符的通用约定书写，属**示例代码**，精确位号「待本地验证」（可与 PTX ISA 文档或 CUTLASS 的 SM90 GMMA 描述符构造代码核对）。字段值的计算逻辑本身（除位号外）完全来自本书。

**操作步骤**：

1. 先运行项目脚本重生成示意图（运行方式见 [img/scripts/README.md:L15](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L15)，依赖见 [L23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L23)）：

   ```bash
   cd img/scripts
   python gen_smem_descriptor.py        # -> ../wgmma_descriptor_kmajor.svg
   ```

   打开生成的 SVG（或直接读脚本），核对五处标注：`atom 8 × 128 B`、`contiguous 1 KB`、`start_address (addr ≫ 4)`、`ldo = 1`、`sdo (next 8 rows, M direction)`。

2. 新建一个独立脚本（**示例代码**，非项目原有文件），实现描述符构造：

   ```python
   SWIZZLE_ENC = {"none": 0, "128B": 1, "64B": 2, "32B": 3}  # PTX ISA 通用约定，待本地验证

   def descriptor_fields(start_addr, ldo_bytes, sdo_bytes, swizzle):
       """从字节量算各字段编码值。三个地址字段一律按 16 B 编码（正文 L207、脚本 L65）。"""
       return {
           "start_address": (start_addr >> 4) & 0x3FFF,   # addr >> 4
           "ldo":           (ldo_bytes >> 4) & 0x3FFF,
           "sdo":           (sdo_bytes >> 4) & 0x3FFF,
           "base_offset":   0,        # 起点与 swizzle 图案边界对齐时为 0（正文 L209）
           "swizzle":       SWIZZLE_ENC[swizzle],
       }

   def pack_descriptor(f):
       """把字段拼成 64 位描述符。位号按 PTX ISA 通用约定书写，待本地验证。"""
       return (f["start_address"]        << 0
             | f["ldo"]                  << 16
             | f["sdo"]                  << 32
             | (f["base_offset"] & 0x7)  << 46
             | (f["swizzle"]    & 0x7)   << 49)

   def make_kmajor_128b(start_addr, m_rows):
       """K-major swizzled：ldo 固定编码 1；sdo = 8 行组间距 = 一个 atom 的字节数。"""
       atom_bytes = 8 * 128                      # SWIZZLE_128B atom = 8 x 128 B（u4-l4）
       assert m_rows % 8 == 0
       return pack_descriptor(descriptor_fields(
           start_addr, ldo_bytes=16, sdo_bytes=atom_bytes * 1, swizzle="128B"))
   ```

   注意 `make_kmajor_128b` 里 `ldo_bytes=16` 是为了让 `>>4` 后得到编码值 1——正文说 K-major swizzled 的 `ldo` 是「固定编码 1」，这是编码后的值，不是字节数。

3. 用 4.2.2 的算例做单元测试：A tile (M=64, K=64) fp16、K-major `SWIZZLE_128B`、SMEM 基址取 `0x2000`。断言 `descriptor_fields(0x2000, 16, 1024, "128B")` 等于 `{"start_address": 0x200, "ldo": 1, "sdo": 64, "base_offset": 0, "swizzle": 1}`，并打印 `hex(pack_descriptor(...))`。
4. 做两个扰动实验并记录：(a) 基址改为 `0x2001`（偏 1 字节）再算 start_address 字段；(b) 基址改为 `0x2010`（偏 16 字节）再算。比较两次的编码值与拼出的 64 位值。
5. 把 `m_rows` 依次取 8/16/64，用 `sdo_bytes = 1024`（atom 尺寸不随 M 变）重新算 `sdo` 编码值，确认它恒为 64——`sdo` 描述的是相邻 8 行组之间的距离，与 tile 有多少个组无关。

**需要观察的现象**：步骤 4 中，偏 1 字节的基址算出的 start_address 编码值与 `0x2000` 完全相同（`0x2001 >> 4 == 0x2000 >> 4`），描述符 64 位值也不变；偏 16 字节则编码值加 1。这正是「以 16 字节为单位编码」的直接后果：**描述符看不见 16 字节以内的起点偏移**，SMEM 摆放必须与这个粒度对齐，否则硬件会去错误的 16 B 块取数。

**预期结果**：步骤 3 的断言通过；`pack_descriptor` 输出一个 64 位整数（十六进制打印出来低 14 位是 `0x200`，bit 16–29 是 1，bit 32–45 是 64，bit 49–51 是 swizzle 编码）；步骤 5 中 `sdo` 编码值恒为 64。若你的环境无法运行 matplotlib（步骤 1），标注「待本地验证」，改为直接阅读脚本源码完成标注核对。

#### 4.2.5 小练习与答案

**练习 1**：描述符里哪三个字段以 16 字节为单位编码？为什么 `start_address` 用 `addr ≫ 4`？

**答案**：`matrix start address`、`ldo`、`sdo`。`≫ 4` 即除以 16，把字节地址换算成 16 B 单位后再填入字段（正文 [L207](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L207)；脚本 [L64-L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L64-L65)）。

**练习 2**：K-major swizzled 布局下 `ldo` 为何是固定编码 1？`sdo` 的物理含义是什么？

**答案**：K 方向遵循固定的 atom 内布局，不需要额外步长，故 `ldo` 恒为 1；`sdo` 是沿 M 从一个 8 行组跳到下一个 8 行组（即跨一个 atom）的字节偏移，`SWIZZLE_128B` 下就是 1024 B、编码值 64（正文 [L209-L213](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L209-L213)；脚本 [L67-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_smem_descriptor.py#L67-L75)）。

**练习 3**：TMA 与 WGMMA 各有自己的描述符，二者是什么关系？

**答案**：彼此独立、但必须描述同一份 SMEM 物理摆法。TMA 用 128B swizzle 写入，WGMMA 描述符就必须按 128B swizzle 解释；任一侧不一致，字节虽然到了 SMEM，Tensor Core 会把元素认错（正文 [L215](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L215)；TMA 章同义表述见 [chapter_tma/index.md:L48](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L48)）。

**练习 4**：把 SMEM 基址从 atom 边界（1024 的倍数）挪到「atom 内偏 128 B」处，描述符哪个字段需要变？

**答案**：`matrix base offset`——它记录矩阵起点在重复 swizzle 图案内部的位置，起点对齐图案边界时为 0，不对齐时就要填入图案内偏移（正文 [L204](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L204)、[L209](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L209)）。注意不能只改 start_address 来「凑」，因为 XOR 图案是按图案边界定义的。

### 4.3 寄存器累加器：输入升级了，输出还在原地

#### 4.3.1 概念说明

Hopper 只升级了输入路径，**C/D 累加器仍分布在 warpgroup 各线程的寄存器里**，epilogue 直接消费这些寄存器 fragment。每线程持多少个累加值由指令形状与累加类型决定（正文原话）。

由此得到本讲最重要的一条结构性结论：**一个 Hopper 内核同时维护两套布局表示**。SMEM 里的 A/B 用矩阵描述符描述；寄存器里的 RS 形式 A 与 C/D 累加器用每线程 fragment 描述。B 恒走 SMEM 路径，A 两条路可选。

「寄存器累加器」的代价可以用寄存器压力来感受（算术演算，非书中数据）：warpgroup 128 线程，若一条指令的累加器是 64×64 的 fp32 tile，共 4096 个值，平均每线程 32 个寄存器被占住——而这还只是一条指令的 D；K 循环里若要同时养多个输出 tile 或 prefetch 数据，寄存器立刻成为绑定资源（呼应 u3-l3 的 occupancy 分析）。Blackwell 把累加器搬进 TMEM，正是冲着这份压力来的（u5-l3）。

顺带一个 Hopper 时代的补充：`stmatrix`（把寄存器 fragment 写回 SMEM 的指令）从 sm_90 起才可用——寄存器累加器时代，epilogue 的「fragment → SMEM」路径也因此获得了专用指令（u5-l1 已提过）。

#### 4.3.2 核心流程

Hopper 内核的完整数据路径与「两套表示」的分工：

```text
准备阶段：  构造 A、B 的 SMEM 描述符（若 A 走 SMEM）
计算阶段：  A/B in SMEM --wgmma--> C/D in 每线程寄存器 fragment
回写阶段：  寄存器 fragment --epilogue（普通 store / stmatrix）--> SMEM 或 GMEM
```

1. **描述符表示**覆盖 SMEM 中的 A/B：起点、`ldo`、`sdo`、base offset、swizzle 模式（4.2）。
2. **fragment 表示**覆盖寄存器中的 C/D（以及 RS 形式的 A）：每线程持有哪些元素，由指令形状与累加类型决定，映射方式与 u5-l1 的 m16n8k16 fragment 同族。
3. 两套表示在一条 `wgmma` 里汇合：指令按描述符读 SMEM、按 fragment 规则写寄存器。
4. epilogue 消费寄存器 fragment，写回 SMEM/GMEM。

三代对比下「寄存器 fragment 的角色」在收缩：Ampere 与 Hopper 用它在计算期间持有累加器；Blackwell 的累加器留在 TMEM，寄存器 fragment 主要退守在 TMEM 与 epilogue 的边界上。

#### 4.3.3 源码精读

- 「累加器仍在寄存器」一节，见 [chapter_layout_generations/index.md:L217-L221](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L217-L221)：WGMMA 直接读 SMEM 输入，但 C/D 仍分布在 warpgroup 各线程的寄存器中，epilogue 消费这些 fragment；「指令形状与累加类型决定每线程持多少累加值」；**「因此 Hopper 内核同时使用两套布局表示——矩阵描述符描述 SMEM 中的 A 与 B，每线程寄存器 fragment 描述寄存器来源的 A 与 C/D 累加器；B 恒走 SMEM 路径，A 两条路皆可」**。
- 三代寄存器 fragment 角色的总结，见 [chapter_layout_generations/index.md:L287-L297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L287-L297)：Ampere 用 `ldmatrix` 构造被 `mma.sync` 消费的 fragment；Hopper 的 `wgmma` 把累加器写进 fragment 供 epilogue 用；Blackwell 的累加器在计算阶段留在 TMEM，epilogue 开始前由 `tcgen05.ld` 装进 fragment。「fragment 的角色因此逐代变化：Ampere/Hopper 用它在计算期间持累加器，Blackwell 主要把它用在 TMEM 与 epilogue 的边界上」。
- 章末三代对比表的第三、四列，见 [chapter_layout_generations/index.md:L299-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L299-L309)：累加器位置三代依次为 寄存器 → 寄存器 → TMEM；SMEM 布局的表达依次为 内核显式计算 → 描述符 → 描述符（SMEM 输入）+ TMEM 布局（累加器与 scale factor）。收尾的读法建议也值得抄下：「跟踪数据流要一步一步来：一条指令写下的布局必须是下一条指令读的布局」。
- `stmatrix` 自 sm_90 可用的说明，见 [chapter_layout_generations/index.md:L164](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L164)。

#### 4.3.4 代码实践

**实践目标**：把「寄存器累加器 vs SMEM 直接读」的对比做成一张可复算的表，并用一个数量级演算把寄存器压力算出来，为 u5-l3 的 TMEM 动机做铺垫。

**操作步骤**：

1. 通读 [chapter_layout_generations/index.md:L287-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L287-L309)，把对比表抄成自己的三列表：A/B 主来源 / 累加器位置 / SMEM 布局的表达方式。
2. 写一小段**示例代码**（非项目原有），做寄存器压力演算：

   ```python
   def regs_per_thread(m, n, threads=128):
       """warpgroup 128 线程平摊一个 m x n fp32 累加器所需的每线程寄存器数（算术演算）。"""
       return m * n / threads

   for shape in [(64, 64), (64, 128), (128, 128)]:
       print(shape, regs_per_thread(*shape))
   ```

3. 对照 u3-l3 讲过的资源压力清单（寄存器、SMEM、TMEM 列、warp/CTA 槽位），写三句话回答：Hopper 内核的绑定资源最容易是哪一个？Blackwell 把累加器移入 TMEM 后，这份压力转移到了哪里（提示：TMEM 的 512 列上限，u2-l2）？为什么说「输入描述符化」并没有减轻寄存器压力？
4. 回到 [chapter_layout_generations/index.md:L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L309)，把「一条指令写下的布局必须是下一条指令读的布局」这句读法守则抄进你的笔记，并在旁边列出 Hopper 内核里的三对「写者→读者」：TMA 写 SMEM → wgmma 读 SMEM；wgmma 写寄存器 → epilogue 读寄存器；epilogue 写 SMEM/GMEM → 下一个消费者。

**需要观察的现象**：步骤 2 的输出是 32 / 64 / 128——累加器 tile 从 64×128 长到 128×128 时，仅累加器一项每线程就要 128 个寄存器，接近常见的每线程 255 个寄存器上限的一半。

**预期结果**：三列表中 Hopper 列为「A 可来自寄存器或 SMEM；B 来自 SMEM｜累加器在寄存器｜描述符记录步长与 swizzle 模式」；步骤 3 能写出「Hopper 的绑定资源常是寄存器；Blackwell 把这份压力转移到 TMEM 列数；输入描述符化省的是地址计算与 ldmatrix 装填，不是累加器寄存器」。步骤 2 属算术演算（非书中数据），用于建立直觉。

#### 4.3.5 小练习与答案

**练习 1**：Hopper 内核「同时维护两套布局表示」，分别是什么、各覆盖谁？

**答案**：矩阵描述符覆盖 SMEM 中的 A 与 B；每线程寄存器 fragment 覆盖 RS 形式的 A 与 C/D 累加器。B 恒走 SMEM 路径，A 两条路皆可（正文 [L219-L221](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L219-L221)）。

**练习 2**：为什么说「输入描述符化」没有减轻寄存器压力？

**答案**：描述符取代的是 `ldmatrix` 装填与内核手写的 SMEM 地址计算（A/B 的输入路径）；C/D 累加器仍在每线程寄存器里，占寄存器的恰恰是累加器，它一点没少（正文 [L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L8)、[L217-L219](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L217-L219)）。

**练习 3**：三代中「寄存器 fragment 在计算阶段持有累加器」的是哪几代？

**答案**：Ampere 与 Hopper。Blackwell 的累加器在计算阶段留在 TMEM，fragment 主要用于 TMEM 与 epilogue 之间的边界搬运（正文 [L287-L297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L287-L297)）。

## 5. 综合实践

**任务**：写一个纯 Python 的「描述符 ↔ swizzled SMEM」一致性检查器，把本讲三个模块与 u4-l4 的 swizzle 串成一条链（无需 GPU；以下为**示例代码**思路，自己实现）。

1. **模拟 SMEM**：构造一个 (M=64, K=64) 的 fp16 逻辑 tile，物理摆成 K-major `SWIZZLE_128B`：8 个 atom 沿 M 堆叠，每个 atom 内 8 行 × 128 B 连续，行内 16 B sector 按 `物理 sector = 逻辑 sector ⊕ 行号(atom 内)` 做 XOR 置换（u4-l4 / u5-l1 的同款规则）。
2. **编码描述符**：用 4.2.4 的 `descriptor_fields` + `pack_descriptor` 生成该 tile 的描述符：`start_address = 基址>>4`、`ldo = 1`、`sdo = 1024B>>4 = 64`、`base_offset = 0`、swizzle = 128B。
3. **实现「wgmma 视角」的寻址**：写函数 `addr_via_descriptor(m, k)`，按 4.2.2 的伪公式（start + (m//8)·sdo + (m%8)·128 + XOR 后的 sector + 扇区内偏移）计算逻辑元素 (m,k) 的物理字节地址。
4. **验证三项**：
   - 双射性：枚举全部 64×64 个逻辑元素，`(m,k) → 字节地址` 无碰撞、恰好铺满 8 KB；
   - 与第 1 步的模拟摆法逐元素一致（描述符读到的 = swizzle 写下的，即正文 L215 的一致性要求）；
   - bank 无冲突：固定 k、遍历同一 atom 内 8 行，8 次 sector 访问落 8 个不同 bank 组（复用 u5-l1 综合实践第 2 步的统计方法）。
5. **扰动测试**：把描述符的 swizzle 字段换成「无 swizzle」、其余不变，重跑第 4 步，记录读错元素的百分比；再只把 `sdo` 改成 512，观察错位模式。用两句话总结「描述符必须与字节一致」失败时的症状。
6. **结论文档**：半页纸 = 一个描述符字段表 + 一张 atom 布局草图 + 三项验证结果 + 扰动症状总结。

**验收标准**：第 4 步三项全部通过；第 5 步两种扰动都能读到「错误的元素」（位置系统性错开，而非随机），并能说出错位沿哪个方向。

## 6. 本讲小结

- `wgmma.mma_async` 由一个 warpgroup（4 warp、128 线程）协同执行；warpgroup 这一执行层级正是 Hopper 为发起 warpgroup 级 MMA 而引入的。
- 输入路径升级为描述符：B 一律经矩阵描述符从 SMEM 直读，A 可走 SMEM（SS）或寄存器（RS），内核不再为 B 拼 `ldmatrix` fragment。
- 矩阵描述符是装在寄存器里的 64 位值，五字段为 start address、`ldo`、`sdo`、base offset、swizzle mode；三个地址字段一律以 16 字节为单位编码（`addr ≫ 4`）。
- K-major swizzled 布局下 `ldo` 固定编码 1，`sdo` 是相邻 8 行组（atom）间距；`SWIZZLE_128B` 的 atom 是 8 × 128 B = 1 KB 连续块，swizzle mode 同时决定 atom 形状与其中 XOR 图案。
- 描述符必须与 SMEM 的实际字节一致：TMA 与 WGMMA 的描述符彼此独立，但必须描述同一物理摆法，否则字节到了、元素认错。
- C/D 累加器仍分布在每线程寄存器中：Hopper 内核同时维护「描述符（SMEM 中的 A/B）+ fragment（寄存器中的 A 与 C/D）」两套布局表示；输入描述符化并未减轻累加器的寄存器压力——这是 Blackwell 引入 TMEM 累加器的直接动机。

## 7. 下一步学习建议

下一讲（u5-l3）进入 Blackwell：`tcgen05.mma` 保留描述符化的 SMEM 输入路径，但把累加器搬进 TMEM，block-scaled MMA 的 SFA/SFB 也放入 TMEM 并经 `tcgen05.cp` 搬入。建议带着两个问题读 [chapter_layout_generations/index.md:L223-L297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L223-L297)：累加器进 TMEM 后，本讲 4.3 的两套布局表示变成了几套？scale factor 的 `R[4 : 32@TLane]` 复制（u4-l3）与 `scale_vec` 字内重复为何是两件不同的事？可配合运行 `img/scripts/gen_sf_scale_vec.py` 与 `gen_mma_layouts.py`（见 [img/scripts/README.md:L12-L14](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L12-L14)）。本讲的「描述符必须与字节一致」原则还将在 u6（TMA tensor map 的 swizzle 模式字段）与 u10（TIRx Layout API 如何代劳这些参数）反复出现，值得现在就把它写进笔记。
