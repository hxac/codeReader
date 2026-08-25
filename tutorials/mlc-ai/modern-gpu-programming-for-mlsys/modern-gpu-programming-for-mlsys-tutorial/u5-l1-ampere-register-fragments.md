# u5-l1 Ampere：寄存器 fragment 与 ldmatrix

## 1. 本讲目标

本讲是「Tensor Core 布局三代演进」单元的第一讲，回溯 Ampere 世代的数据路径。学完本讲你应该能够：

1. 说出 `mma.sync` 的输入输出来源：A、B、C/D 全部以寄存器 fragment 的形式分布在 warp 的 32 个线程中。
2. 手推 `mma.sync.aligned.m16n8k16` 下 A、B、C/D 三种 fragment 中任意 lane 持有的元素坐标，并用 `S[(8,4,2):(4@laneid,1@laneid,1@reg)]` 记号复述。
3. 解释 `ldmatrix` 如何「一个 warp 协同、少数 lane 出地址、全体 lane 收数据」地把 SMEM 中的一个 8×8 tile 拼装成 fragment。
4. 解释 Ampere 时代 SMEM swizzle 的由来：行写要连续、列读要散开，二者不可兼得，于是内核手写 XOR 地址计算。

## 2. 前置知识

本讲直接建立在 u4-l2（命名轴）之上，并复用 u4-l4（swizzle）的结论。需要的概念如下：

- **warp 与 lane**：一个 warp 是 32 个锁步执行的线程，每个线程用 lane ID（0–31）标识。warp 是 `mma.sync` 的执行单位。
- **命名轴布局**：u4-l2 把布局函数的返回值从单个线性地址推广为坐标字典，记法 `S[(shape):(strides)]`，每个 stride 标注归属的物理轴，如 `4@laneid` 表示这一维对 lane ID 的贡献是 4；`@reg` 表示对 lane 本地寄存器槽位的贡献。同一轴被多个维度引用时贡献相加。
- **寄存器 fragment（register fragment）**：一个矩阵 tile 分散在 warp 各线程寄存器里的那份「切片」。单个线程的 fragment 拼起来才是完整 tile。
- **SMEM bank 与 bank conflict**：SMEM 按 4 字节粒度分成 32 个 bank，\( \text{bank} = (\text{addr}//4) \bmod 32 \)。同一 wavefront 内落在同 bank 不同地址的访问被串行化。
- **XOR swizzle**：u4-l4 讲过的地址重排，用「列 sector 异或行号」让同一 tile 的行读与列读都避开 bank conflict；它是与 `S[...]` 布局组合的独立地址变换，不是仿射映射。

不熟悉 Tensor Core 本身的读者，只需记住一件事：它是 SM 里专门做矩阵乘累加 \( D = AB + C \) 的硬件单元，本讲关心的是「它要求数据摆在哪里」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [chapter_layout_generations/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md) | 本章正文「Tensor Core 数据布局的演进」，Ampere 部分位于 L30–L178 |
| [zh/chapter_layout_generations/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_layout_generations/index.md) | 上述章节的中文镜像（u1-l2 讲过的 zh 前缀约定） |
| [img/scripts/gen_mma_m16n8k16_fragment.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_m16n8k16_fragment.py) | 生成 m16n8k16 C/D fragment 教学图的脚本，脚本内直接编码了 lane 映射公式 |
| [img/scripts/gen_ldstmatrix.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_ldstmatrix.py) | 生成 ldmatrix/stmatrix 数据搬移示意图的脚本，同样编码了映射与地址供给规则 |
| [img/scripts/README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md) | 全部图表脚本的运行说明（依赖 matplotlib、numpy） |

## 4. 核心概念与源码讲解

### 4.1 Ampere mma.sync：一切都发生在寄存器里

#### 4.1.1 概念说明

三代 Tensor Core 指令在数学上做同一件事：\( D = AB + C \)。这个公式完全没说矩阵放在哪、以什么顺序被读。正文在开篇就把三代的差别压缩成三句话：Ampere 的 `mma.sync` 从 warp 内各线程的寄存器读 A、B、C，D 也留在寄存器；Hopper 的 `wgmma` 可以经描述符直接读 SMEM；Blackwell 的 `tcgen05.mma` 把累加器搬进 TMEM。见 [chapter_layout_generations/index.md:L4-L10](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L4-L10)。

Ampere 是三代中「寄存器参与最深」的一代：**输入和输出都在寄存器里**。这意味着内核必须自己把数据按硬件规定的位置摆进每个线程的寄存器——放错一个元素，指令就会把它当成另一个矩阵元素，结果直接错。

#### 4.1.2 核心流程

Ampere 高性能内核的标准数据路径是三行：

```text
SMEM --ldmatrix--> registers
registers --mma.sync--> registers
registers --ordinary store--> SMEM or GMEM
```

执行流程：

1. 内核先把 A、B tile 暂存到 SMEM（通常由合并访存的普通 load 完成）。
2. 用 `ldmatrix` 把 SMEM 中的元素装进各线程「正确的」寄存器，形成 fragment。
3. 整个 warp 协同执行一条 `mma.sync`，硬件按固定规则消费这些寄存器。
4. D 仍是寄存器 fragment，epilogue 用普通 store 写回 SMEM 或 GMEM。

正文特别强调：`mma.sync` 执行的那一刻，**只有寄存器内容重要**——上面是常见高性能路线，但普通 load 加寄存器操作也能拼出同样的 fragment。这一点在排查问题时很关键：寄存器里的值对了，MMA 就对，与数据是怎么来的无关。

#### 4.1.3 源码精读

正文 Ampere 一节的开头，见 [chapter_layout_generations/index.md:L30-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L30-L44)：这段说明了 `mma.sync.aligned.m16n8k*` 指令族从寄存器取 A/B/C、写回 D 到寄存器；一条 `mma.sync` 由整个 warp 集体执行，PTX 规定了 tile 如何分散到 32 个线程的寄存器，每个线程持有的部分就是它的 register fragment；随后给出上面三行数据路径。

约束的来源，见 [chapter_layout_generations/index.md:L24-L28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L24-L28)：一个实用的 Tensor Core 布局必须**同时**满足三个条件——GMEM 访问要合并、SMEM 访问要避开 bank conflict、每个矩阵元素要落在 MMA 指令要求的位置。前两个在性能单元已经讲过，本章专讲第三个。

为什么「位置」这件事值得单独一章，见 [chapter_layout_generations/index.md:L18-L22](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L18-L22)：Tensor Core 指令按固定的硬件规则解释寄存器、SMEM 地址和 TMEM 坐标，放错位置指令不会报错，而是把元素当成别的元素算出错误结果。

章末的三代对比表浓缩了本单元全景，见 [chapter_layout_generations/index.md:L301-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L301-L309)：Ampere 行写着「A/B 主来源 = 寄存器，累加器 = 寄存器，SMEM 布局由内核显式计算地址与 swizzle」——这最后一列正是本讲 4.3 的伏笔，也是与 Hopper（描述符）、Blackwell（TMA 描述符 + TMEM 布局）的分水岭。

#### 4.1.4 代码实践

**实践目标**：把三代数据路径抄成一张自己的对照表，确认 Ampere 的独特性，为后两讲定位。

**操作步骤**：

1. 打开 [chapter_layout_generations/index.md:L299-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L299-L309) 的对比表，再打开 [chapter_layout_generations/index.md:L287-L297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L287-L297) 的「三代寄存器 fragment 角色变化」一段。
2. 自己画一张 3 列表（Ampere / Hopper / Blackwell），行包括：A/B 主来源、累加器位置、寄存器 fragment 在计算阶段是否持有累加器、SMEM 布局的描述方式。
3. 在表下用一句话回答：哪一代的「输入布局」也必须由内核按线程拼装？

**需要观察的现象**：表格里 Ampere 是唯一「输入也在寄存器」的世代；Hopper 只剩累加器在寄存器；Blackwell 连累加器都在计算阶段离开了寄存器，寄存器 fragment 退居 TMEM 与 epilogue 之间的边界。

**预期结果**：三代演进的主线是「寄存器负担逐代减轻、SMEM 布局的描述从手写地址升级为描述符」。这与正文 L287–L297 的结论一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `mma.sync` 执行时「唯一重要的是寄存器内容」？

**答案**：因为指令只消费寄存器。`ldmatrix` 只是构造 fragment 的常见高性能手段，用普通 load 和寄存器操作拼出同样的值也能让 MMA 正确执行（见正文 L44）。

**练习 2**：一个实用的 Tensor Core 布局要同时满足哪三个约束？

**答案**：GMEM 访问合并、SMEM 访问避开 bank conflict、每个矩阵元素位于 MMA 指令要求的位置（见正文 L26–L28）。

**练习 3**：把一个元素放错寄存器位置，硬件会怎样？

**答案**：不会报错。指令按固定规则解释寄存器内容，会把放错的元素当成另一个矩阵元素参与计算，产出错误结果（见正文 L20）。

### 4.2 fragment 映射：m16n8k16 的每线程坐标

#### 4.2.1 概念说明

「fragment 映射」回答的问题是：给定 lane ID \( l \)，这个线程的寄存器里到底装着矩阵的哪些元素？PTX 对每种 MMA 指令形状都规定了唯一答案。本模块以 `mma.sync.aligned.m16n8k16`（fp16/bf16 输入、fp32 累加）为例，A 为 row-major、B 为 column-major，warp 计算：

\[ D_{16\times 8} = A_{16\times 16} B_{16\times 8} + C_{16\times 8} \]

对照来源见 [chapter_layout_generations/index.md:L46-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L46-L54)。

#### 4.2.2 核心流程

C/D 映射用「两步读法」：

1. 32 个 lane 每 4 个一组，共 8 组；第 \( g \) 组负责 rows \( g \) 和 \( g+8 \)。
2. 组内第 \( t \) 个 lane 负责这两行的 columns \( 2t \) 和 \( 2t+1 \)。

对 lane ID \( l \)：

\[ g = l \mathbin{//} 4, \qquad t = l \bmod 4 \]

它持有 4 个 fp32 累加值：

\[ (g,\,2t),\quad (g,\,2t{+}1),\quad (g{+}8,\,2t),\quad (g{+}8,\,2t{+}1) \]

A fragment 复用同样的 \( g \)、\( t \)，坐标换成 \( (m,k) \)，每 lane 8 个 fp16、两个打包进一个 32-bit 寄存器，共 4 个寄存器：

| 寄存器 | 持有的 \( (m,k) \) 坐标 |
|---|---|
| reg 0 | \( (g,\ 2t+\{0,1\}) \) |
| reg 1 | \( (g+8,\ 2t+\{0,1\}) \) |
| reg 2 | \( (g,\ 2t+\{8,9\}) \) |
| reg 3 | \( (g+8,\ 2t+\{8,9\}) \) |

B fragment 坐标为 \( (k,n) \)，每 lane 4 个 fp16、两个打包一个寄存器，共 2 个寄存器：reg 0 持有 \( (2t+\{0,1\},\ g) \)，reg 1 持有 \( (2t+\{8,9\},\ g) \)。对 B 来说 **\( g \) 决定 \( n \) 坐标，\( t \) 与寄存器编号共同决定 \( k \)**——与 A/C 恰好分工相反。

守恒校验（双射性，u4-l2 的检验工具）：A 共 \( 32 \times 8 = 256 = 16 \times 16 \)；B 共 \( 32 \times 4 = 128 = 16 \times 8 \)；C/D 共 \( 32 \times 4 = 128 = 16 \times 8 \)。元素不多不少，每个逻辑元素恰好一个物理位置。

#### 4.2.3 源码精读

- C/D 两步读法与公式，见 [chapter_layout_generations/index.md:L60-L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L60-L90)：这段给出 \( g=l//4 \)、\( t=l \bmod 4 \)、四个累加值坐标，并以 lane 5 为例算出 \( g=1, t=1 \)，持有 \( (1,2),(1,3),(9,2),(9,3) \)。
- A fragment 的四寄存器表，见 [chapter_layout_generations/index.md:L92-L103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L92-L103)。
- B fragment 的两寄存器表与「\( g \) 定 \( n \)、\( t \) 与寄存器号定 \( k \)」，见 [chapter_layout_generations/index.md:L105-L114](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L105-L114)。
- 用 u4-l2 记号表达 C/D 的前 8 行，见 [chapter_layout_generations/index.md:L116-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L116-L143)：

  ```text
  S[(8, 4, 2) : (4@laneid, 1@laneid, 1@reg)]
  ```

  三个坐标是 `(row, column_pair, element_in_pair)`，逻辑坐标 `(row, col)` 变换为 `(row, col//2, col%2)`，于是

  ```text
  lane_id = row * 4 + col // 2
  slot    = col % 2
  ```

  正文随后回到 lane 5 的两个元素 \( (1,2),(1,3) \)：原子坐标 \( (1,1,0),(1,1,1) \)，都映射到 lane \( 1\times4+1=5 \)，最后一个坐标选 slot 0 或 1。完整的 C/D fragment 沿 M 有两个这样的 8×8 原子；原子只是描述手段，硬件仍执行完整的一条指令。
- 教学图脚本里硬编码的正是这条映射：见 [img/scripts/gen_mma_m16n8k16_fragment.py:L120-L141](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_m16n8k16_fragment.py#L120-L141)，其中 `lane = 4 * g + c // 2` 是 `lane_id = row*4 + col//2` 的直接翻译；lane 5 高亮框的四个坐标列在 [img/scripts/gen_mma_m16n8k16_fragment.py:L166-L172](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_m16n8k16_fragment.py#L166-L172)；图注文案（含 `g = lane // 4`、`t = lane mod 4` 公式）在 [img/scripts/gen_mma_m16n8k16_fragment.py:L33-L53](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_mma_m16n8k16_fragment.py#L33-L53)。

#### 4.2.4 代码实践

**实践目标**：用程序枚举 32 个 lane 的 A/B/C fragment 坐标，自动验证守恒与双射性——这就是本讲规格要求的手推任务的机器自查版。

**操作步骤**：

1. 新建一个独立脚本（**示例代码**，非项目原有文件），实现三个函数：

   ```python
   def cd_fragment(lane):
       g, t = lane // 4, lane % 4
       return [(g, 2*t), (g, 2*t+1), (g+8, 2*t), (g+8, 2*t+1)]

   def a_fragment(lane):
       g, t = lane // 4, lane % 4
       regs = []
       for m, kbase in ((g, 0), (g+8, 0), (g, 8), (g+8, 8)):
           regs.append([(m, kbase + 2*t), (m, kbase + 2*t + 1)])
       return regs  # 4 个寄存器 x 各 2 个 fp16

   def b_fragment(lane):
       g, t = lane // 4, lane % 4
       return [[(2*t, g), (2*t+1, g)], [(2*t+8, g), (2*t+9, g)]]  # 2 个寄存器
   ```

2. 打印 lane 5 与 lane 18 的三张 fragment 表，先手推再对答案。
3. 做三项断言：所有 lane 的 C/D 坐标合并后恰好等于 `{(m,n) | 0<=m<16, 0<=n<8}`（128 个、无重复）；A 合并后等于 16×16 全集（256 个）；B 等于 16×8 全集（128 个）。
4. 用 `S[(8,4,2):(4@laneid,1@laneid,1@reg)]` 的公式 `lane_id = row*4 + col//2, slot = col%2` 写一个反查函数，抽查 C/D 的 8 个元素与第 1 步结果一致。

**需要观察的现象**：lane 5 的 C/D 是 \( (1,2),(1,3),(9,2),(9,3) \)；A 是 reg0=\((1,2),(1,3)\)、reg1=\((9,2),(9,3)\)、reg2=\((1,10),(1,11)\)、reg3=\((9,10),(9,11)\)；B 是 reg0=\((2,1),(3,1)\)、reg1=\((10,1),(11,1)\)。

**预期结果**：三项断言全部通过；lane 18（\( g=4,t=2 \)）的 C/D 为 \( (4,4),(4,5),(12,4),(12,5) \)。

#### 4.2.5 小练习与答案

**练习 1**：A fragment 的一个 32-bit 寄存器里装几个 fp16？为什么？

**答案**：2 个。两个 16-bit 元素打包进一个 32-bit 寄存器，所以 8 个元素只需 4 个寄存器（见正文 L92）。

**练习 2**：用 `S[(8,4,2):(4@laneid,1@laneid,1@reg)]` 求 C/D 元素 \( (5,6) \) 的 lane 与槽位。

**答案**：`(row, col//2, col%2) = (5, 3, 0)`，`lane_id = 5*4+3 = 23`，slot 0。

**练习 3**：B fragment 里「决定 \( n \) 坐标」的是 \( g \) 还是 \( t \)？A fragment 里呢？

**答案**：B 里 \( g \) 决定 \( n \)，\( t \) 加寄存器编号决定 \( k \)（见正文 L114）；A 里正相反，\( g \)（及 +8）决定 \( m \)，\( t \) 与寄存器编号决定 \( k \)。

### 4.3 ldmatrix 与 SMEM swizzle：装填与写回的两难

#### 4.3.1 概念说明

知道了 fragment 的目标形态，下一个问题是：怎么把 SMEM 里的 tile「倒进」那 32 组寄存器？逐元素 `ld.shared` 需要内核自己实现跨 lane 分发。Ampere 提供专用指令 `ldmatrix`（`.m8n8.b16` 形式），一条指令完成「按行取、按 fragment 分发」。

与它对偶的问题是写回：MMA 之后 D 在寄存器里，epilogue 用普通 store 写 SMEM/GMEM。普通 store 偏好「沿行连续」，而后续的 `ldmatrix` 或其他消费常常「跨行取列」——同一份 SMEM 布局要同时伺候两种模式，这正是 u4-l4 swizzle 登场的现场。

#### 4.3.2 核心流程

`ldmatrix.sync.aligned.m8n8.x1/.x2/.x4.shared.b16` 由整个 warp 一起执行，`.x1/.x2/.x4` 分别装载 1、2、4 个 8×8 矩阵。规则：

1. **地址供给**：对矩阵 \( m \) 的第 \( r \) 行，由 lane \( m \times 8 + r \) 提供该行的起始地址。于是 `.x1` 用 lanes 0–7 的地址，`.x2` 用 0–15，`.x4` 用全部 32 个 lane。
2. **数据分发**：与供给是两个独立角色。以 `.x1` 为例，64 个元素分给全部 32 个 lane，lane \( l \) 收到

   \[ \text{row} = l \mathbin{//} 4, \qquad \text{cols} = 2\,(l \bmod 4),\ 2\,(l \bmod 4)+1 \]

   即第 0 行的 8 个元素去 lanes 0–3（lane 0 得 columns 0–1，lane 1 得 2–3……），每 lane 收到的两个 fp16 打包进一个 32-bit 寄存器。
3. `.trans` 修饰符让每个 8×8 矩阵按列主序装载；Hopper（sm_90）起还有反向的 `stmatrix` 把 fragment 写回 SMEM。

swizzle 的动机（以 (8,64) fp16 tile 为例）：每行 64×2B = 128B。固定列、遍历 8 行时地址为 \( r \times 128 + c \times 2 \)，则

\[ \text{bank} = \left(\frac{r \times 128 + 2c}{4}\right) \bmod 32 = \left(32r + \lfloor c/2 \rfloor\right) \bmod 32 = \lfloor c/2 \rfloor \bmod 32 \]

与 \( r \) 无关——8 行全落同一个 bank，产生 8-way 冲突。XOR swizzle（u4-l4：mapped_col = col ⊕ row，作用在 16B sector 粒度）在保持行内连续的同时把列读散到不同 bank：swizzle 后同一列 8 行所在 sector 变为 \( s_0 \oplus r \)，随 \( r \) 取遍 8 个不同值，bank 各不相同，冲突消失。

#### 4.3.3 源码精读

- ldmatrix 三种形式与「矩阵 \( m \) 行 \( r \) 的地址来自 lane \( m \times 8+r \)」，见 [chapter_layout_generations/index.md:L147-L166](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L147-L166)。这段还给出分发公式 `row = l // 4`、`cols = 2*(l%4), 2*(l%4)+1`，并强调「提供地址」与「接收数据」是分离的角色：左边 T0–T7 出地址，右边 32 个 lane 都持有数据；`.trans` 与 `stmatrix`（Hopper 引入）也在此说明。
- 示意图脚本的「真值注释」直接写明了这两条规则，见 [img/scripts/gen_ldstmatrix.py:L1-L5](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_ldstmatrix.py#L1-L5)：docstring 标注「lane l holds row l/4, cols 2*(l%4) and +1; row r address comes from lane r」。
- 脚本画 SMEM 侧时按行上色、左侧标注 `T{r}`，见 [img/scripts/gen_ldstmatrix.py:L44-L51](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_ldstmatrix.py#L44-L51)，图下注「row r address ← lane T{r}」。
- 脚本画寄存器侧时用 `lane = 4 * r + j` 反推每个 column-pair 的归属，见 [img/scripts/gen_ldstmatrix.py:L53-L64](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_ldstmatrix.py#L53-L64)，图注写明「lane l → row l/4, cols 2·(l%4), +1 (1 b32 = 2 fp16)」。
- 脚本脚标概括了 `.x1/.x2/.x4` 的地址来源与 `.trans` 含义，见 [img/scripts/gen_ldstmatrix.py:L74-L76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_ldstmatrix.py#L74-L76)。
- 写回与 swizzle 的正文，见 [chapter_layout_generations/index.md:L170-L178](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L170-L178)：MMA 后 epilogue 用普通逐线程 store 写回；输入路径则出现「store 偏好行连续、ldmatrix 跨行读」的矛盾；(8,64) fp16 tile 每行恰 128B，固定列的 8 个元素因行跨 128B 而全部映射到同一 bank，形成 8-way 冲突；Ampere 内核通常**手写 XOR 地址计算**来重排物理摆放，既保住高效的连续行访问，又把跨行读散到各 bank。

#### 4.3.4 代码实践

**实践目标**：（1）运行项目脚本重生成 ldmatrix 示意图；（2）手推「谁出地址、谁收数据」对照表；（3）写一段文字说明 ldmatrix 如何消费 swizzle 过的 SMEM——这是本讲规格指定的实践任务。

**操作步骤**：

1. 按 [img/scripts/README.md:L3-L23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L3-L23) 的说明运行脚本（依赖 `matplotlib`、`numpy`）：

   ```bash
   cd img/scripts
   python gen_ldstmatrix.py        # -> ../ldstmatrix.svg
   ```

2. 打开生成的 `img/ldstmatrix.svg`（或直接读脚本源码），对照左右两侧：左侧行 \( r \) 标着 `T{r}`（出地址的 lane），右侧每个 column-pair 标着 `L{lane}`（收数据的 lane）。
3. 手推一张 8 行对照表：行 \( r \) / 出地址的 lane / 收数据的 lanes（4 个）。先用 `row = l//4` 反推（收行 \( r \) 的是 lanes \( 4r \)–\( 4r{+}3 \)），再到图上核对。
4. 写一段 5–8 句的文字，说明 `ldmatrix` 如何把 SMEM 中 swizzle 过的行拼装成 fragment（要点见下面「预期结果」）。
5. 选做：把脚本中 `ROWC` 调色板换掉或把 `figsize` 改大，重新运行，确认输出随之变化——确认这张图是「代码生成」而非手绘。

**需要观察的现象**：出地址与收数据是两套 lane 序：行 0 的地址来自 lane 0，数据去 lanes 0–3；行 5 的地址来自 lane 5，数据却去 lanes 20–23。图中央一条向右的 `ldmatrix (SMEM → reg)` 箭头和一条向左的 `stmatrix (reg → SMEM)` 箭头。

**预期结果**：对照表 8 行全部与图一致；你的文字说明应覆盖以下要点——`ldmatrix.x1` 一次装载一个 8×8 b16 tile，8 条「16 字节连续行」由 lanes 0–7 的地址指定；swizzle 不改变行内 8 个 fp16 的连续性，只是把整条 16B 行块搬到 XOR 重排后的 sector，因此**由出地址的 lane 在计算地址时先把 XOR 算进去**，`ldmatrix` 本身并不知道 swizzle 的存在；数据分发规则（`row = l//4`、列对 `2*(l%4), +1`、两元素打包一个 b32 寄存器）完全不因 swizzle 而改变。这正是 Ampere 与后两代的本质区别：swizzle 活在内核手写的地址计算里，而 Hopper 把它写进矩阵描述符、Blackwell 把它写进 TMA 的 tensor map。（若你所在环境无法运行 matplotlib，本步骤标注「待本地验证」，改为直接阅读脚本源码完成对照表。）

#### 4.3.5 小练习与答案

**练习 1**：`ldmatrix.x2` 的行地址来自哪些 lane？

**答案**：lanes 0–15。矩阵 \( m \in \{0,1\} \)、行 \( r \)，地址来自 lane \( m \times 8 + r \)（见正文 L155）。

**练习 2**：(8,64) fp16 tile 的固定列读为什么是 8-way 冲突？

**答案**：行跨 128B，\( 128/4 = 32 \) 恰为 bank 总数，故 \( 32r \bmod 32 = 0 \)，8 行同 bank（见正文 L174 与本讲 4.3.2 的推导）。

**练习 3**：行 5 的地址由谁提供？行 5 的数据由谁接收？

**答案**：地址由 lane 5 提供（`T5`）；数据由 lanes 20–23 接收（`row = l//4 = 5` ⇒ \( l \in [20,23] \)，四条 lane 各拿一对相邻列）。

**练习 4**：`ldmatrix` 知道 SMEM 里做了 XOR swizzle 吗？

**答案**：不知道。swizzle 只体现在内核为每行提供的（已异或的）地址上；指令只负责「按地址取 16B 连续行、按固定规则分发给 32 个 lane」。这一职责在 Hopper 移入矩阵描述符、在 Blackwell 移入 TMA 描述符（见正文 L303–L305 对比表）。

## 5. 综合实践

**任务**：写一个纯 Python 的「Ampere fragment 装填-写回」模拟器，把本讲三个模块串起来（无需 GPU；以下为**示例代码**思路，自己实现）。

1. **装填**：模拟 `ldmatrix.x4` 装载 m16n8k16 的 A tile。A 是 16×16，恰好 4 个 8×8 块；让 lane \( m \times 8 + r \) 提供第 \( m \) 块第 \( r \) 行的地址（地址指向你模拟的 SMEM 数组中某个 8×8 子块），再用 `row = l//4`、`cols = 2*(l%4), +1` 的分发规则填出 32 个 lane 的 fragment；与 4.2 的 `a_fragment(lane)` 逐一比对，必须完全一致。
2. **swizzle**：把模拟 SMEM 改成 (8,64) fp16 的 128B-swizzle 摆放（sector 级 `mapped_sector = sector ⊕ row`），分别统计「固定列读 8 行」在 plain 行主序与 swizzle 后各自命中的 bank 序列，验证前者 8 行同 bank、后者 8 个不同 bank。
3. **写回**：模拟 epilogue——把 C/D fragment（4.2 的 `cd_fragment`）按 plain 行主序写回一个 (16,8) fp32 SMEM tile，统计一次 warp 级写回的 bank 冲突情况；再讨论：如果这个 tile 未来要被另一条 `ldmatrix` 按列读，你会对它应用哪种 swizzle？
4. **结论文档**：用半页写出三个数字（A/B/C 每 lane 元素数：8/4/4）+ 两张表（fragment 坐标表、地址/数据 lane 对照表）+ 一段 swizzle 说明。

**验收标准**：第 1 步两个 fragment 完全相等；第 2 步 plain 版冲突重数为 8、swizzle 版为 1；第 3 步能给出带理由的 swizzle 选择。

## 6. 本讲小结

- Ampere `mma.sync` 的 A、B、C/D 全部以寄存器 fragment 形式分布在 warp 的 32 个线程中，是三代里「寄存器负担」最重的一代。
- m16n8k16 的映射由 \( g=l//4 \)、\( t=l \bmod 4 \) 决定：C/D 每 lane 4 个 fp32（\( (g,2t),(g,2t{+}1),(g{+}8,2t),(g{+}8,2t{+}1) \)），A 每 lane 8 个 fp16 分 4 个寄存器，B 每 lane 4 个 fp16 分 2 个寄存器且 \( g \) 定 \( n \)。
- 这套分布可用 `S[(8,4,2):(4@laneid,1@laneid,1@reg)]` 表达：`lane_id = row*4 + col//2`、`slot = col%2`；元素数守恒是检验布局的基本工具。
- `ldmatrix` 由整个 warp 协同执行：lanes 0–7/0–15/0–31 出行地址（`.x1/.x2/.x4`），全体 32 个 lane 按同一公式收数据；出地址与收数据是分离角色。
- swizzle 的由来：行写要连续、跨行读要散开，(8,64) fp16 tile 行跨 128B 导致 8-way bank 冲突；Ampere 的解法是内核手写 XOR 地址计算，`ldmatrix` 指令本身对 swizzle 无感知。

## 7. 下一步学习建议

下一讲（u5-l2）进入 Hopper：`wgmma.mma_async` 让四个 warp 协作发起一条 MMA，A/B 经 64 位**矩阵描述符**直接从 SMEM 读取——重点对比「描述符记录步长与 swizzle 模式」如何取代本讲的 ldmatrix + 手写地址。建议带着问题先读 [chapter_layout_generations/index.md:L180-L221](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L180-L221)，并按 README 运行 `img/scripts/gen_smem_descriptor.py` 生成描述符示意图。学完 u5-l2 再进入 u5-l3（Blackwell TMEM 累加器与 scale factor），即可完整看懂「寄存器负担逐代减轻」这条主线。
