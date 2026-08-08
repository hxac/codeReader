# 全局参数 N/L/K 与寄存器类概念

## 1. 本讲目标

本讲是理解整个 chisel-npu 代码库的「钥匙课」。读完本讲你应该能够：

1. 准确说出 **N(bits)**、**L**、**K** 三个全局符号各自的含义、默认值，以及它们各自流到硬件的哪个部件。
2. 理解 `NCoreBackend` 在实例化时为什么强制 **`K == mmalu.n`**（K 等于 MMALU 脉动阵列的边长）。
3. 掌握 **VX / VE / VR** 三类寄存器的数量、每寄存器位宽，以及它们如何共享同一块物理存储（别名关系）。
4. 理解 **`L` 必须被 4 整除** 的根本原因。
5. 看懂一条指令是如何通过 `funct7[1:0]` 这 2 个比特选择操作哪一类寄存器的。

这三个参数和三类寄存器贯穿 ISA、VALU、寄存器堆、后端连线**所有代码**。项目文档直接把它们称为「整个代码库最主要的错误来源」（AGENTS.md 原话：「Confusing them is the primary source of errors in this codebase」）。所以本讲值得慢一点、读细一点。

## 2. 前置知识

在开始之前，你需要知道几个本讲会用到的基本概念（无需精通）：

- **寄存器堆（Register File）**：CPU/NPU 里用来临时存放操作数的一小块快速存储。本讲的 VX/VE/VR 就是这块存储的三种「看法」。
- **SIMD 通道（lane）**：一条向量指令一次能并行处理多少个数据元素，这个「多少」就是通道数。本讲的 **K** 就是通道数。
- **脉动阵列（Systolic Array）**：MMALU 用来做矩阵乘的 n×n 硬件网格。这里你只需要知道它有一个「边长 n」，后面会用到。
- **ChiselEnum**：Chisel 里定义一组带名字的常量的方式，本讲里 `VecWidth` 就是一个 ChiselEnum，用来列举「VX/VE/VR」。
- **字节（byte）= 8 bits**。本讲的 **N(bits)=8**，恰好一个 VX 通道占 1 字节，这让「物理存储字节数」的计算非常直观。

如果你已经读过本手册的 **u1-l1（项目总览）**，对 MMALU / VALU / 多宽度寄存器堆三个名字有印象即可，本讲不会假设你了解它们的内部实现。

## 3. 本讲源码地图

本讲涉及的关键文件如下，先建立一个整体印象：

| 文件 | 作用 | 本讲关注点 |
|:---|:---|:---|
| `docs/index.md` | 项目文档首页，含**记号表（Notation）** | N/L/K 的权威定义表、寄存器类别名表 |
| `AGENTS.md` | 给 Agent 的代码库须知 | N/L/K 的「authoritative」表、MMALU `n` 与 `K` 的关系说明 |
| `src/main/scala/isa/instrFormat.scala` | 32 位指令格式定义 | `VecWidth` 枚举、`funct7` 位段常量、`Funct7Attrs` 编码 |
| `src/main/scala/sram/multiWidthRegister.scala` | 多宽度寄存器堆实现 | VX/VE/VR 别名的字节级实现 |
| `src/main/scala/backend/SimpleBackend.scala` | `NCoreBackend` 顶层连线 | 参数列表、`K==n` 的实例化绑定 |
| `src/main/scala/alu/mma/mma.scala` | MMALU 顶层模块 | `MMALU` 的 `n` / `nbits` 参数 |

一句话概括它们的关系：`docs/index.md` 与 `AGENTS.md` 用表格**规定**了 N/L/K 是什么；`instrFormat.scala` 把「选择哪类寄存器」编码进指令的 `funct7`；`multiWidthRegister.scala` 用同一块存储**实现**了三类寄存器的别名；`SimpleBackend.scala` 在顶层把 `K` 传给 MMALU 当作 `n`，完成约束 `K==n`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** 三个全局参数 N(bits) / L / K（对应「docs/index.md 记号表」）
- **4.2** 寄存器类别名表 VX / VE / VR（对应「寄存器类别名表」+ 物理实现）
- **4.3** `funct7` 中的 width 编码与 `VecWidth` 枚举（对应「VecWidth 枚举」）

### 4.1 三个全局参数 N(bits) / L / K

#### 4.1.1 概念说明

整个 chisel-npu 代码库到处出现三个符号。它们的含义如下：

| 符号 | 含义 | 测试默认值 | Top 默认值（K=64） |
|:---:|:---|:---:|:---:|
| **`N`**（口语读作 **N(bits)**） | **基础通道位宽**，单位是比特。等于 MMALU 的 `nbits`。文档里**永远**拼写为 `N(bits)` 以免和别的 N 混淆。 | 8 | 8 |
| **`L`** | **VX 寄存器的数量**。**必须被 4 整除**（原因见 4.2）。 | 32 | 32 |
| **`K`** | **每个寄存器的 SIMD 通道数**。在后端边界上**等于** MMALU 脉动阵列的边长 `n`。 | 8 | 64 |

直觉上可以这样记：

- **N(bits)** 决定「**一个数据元素有多宽**」——默认 8 比特，即 INT8。这正是 NPU 最常用的低精度量化格式。
- **L** 决定「**有几条寄存器**」——默认 32 条 VX 寄存器，类似 RISC-V 的 32 个通用寄存器。
- **K** 决定「**一条向量指令同时处理几个元素**」——测试用 8，上板（FPGA top）用 64，通道越多算力越强。

#### 4.1.2 核心流程：参数如何流到硬件

这三个参数不是孤立的，它们在硬件实例化时被绑定到具体模块：

1. **N(bits)** → 同时流向两处：
   - 作为 **MMALU 的 `nbits`**（矩阵乘每个乘数的位宽）；
   - 作为 **VALU / 寄存器堆的「VX 通道位宽」**。
2. **L** → 流向 **`MultiWidthRegisterBlock` 的 VX 行数**（物理存储有 L 行）。
3. **K** → 流向 **VALU / 寄存器堆的通道数**，**同时**被原样传给 MMALU 当作它的阵列边长 `n`。

最关键的一条约束是：在 `NCoreBackend` 这个边界上，**`K` 必须等于 MMALU 的 `n`**。为什么？因为 MMALU 每一拍产出 K 个结果，这些结果要写回 VR 寄存器的 K 个通道；如果 `K ≠ n`，MMALU 的输出宽度就和寄存器堆的通道数对不上，连线就会错位。所以代码里直接把 `K` 当作 `n` 传进去来保证一致。

注意一个容易踩的坑：**MMALU 的 `n` 是「脉动阵列边长」，本质上是「阵列规模」参数；而 `K` 是「SIMD 通道数」。** 二者物理含义不同，只是在后端被人为绑成相等。AGENTS.md 特别强调「不要把它们混淆」（Do not conflate them）。

#### 4.1.3 源码精读

先看文档里的**权威定义表**（记号表）：

[docs/index.md:17-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md#L17-L22) 用一张表钉死了 N/L/K 的含义与默认值，并明确 `K` 等于 MMALU 的阵列边长 `n`。

[AGENTS.md:34-38](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L34-L38) 给出同一张表的「authoritative」版本，额外说明 `N(bits)` 等于 MMALU 的 `nbits`、`L` 必须被 4 整除。这是整个代码库的「北辰」。

参数定义也写进了源码注释。[src/main/scala/isa/instrFormat.scala:5-9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L5-L9) 在指令格式文件开头复述了这三个参数，并明确 `K` 在后端边界等于 MMALU 的 `n`。

接着看参数如何被绑定到硬件。[src/main/scala/backend/SimpleBackend.scala:52-59](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L52-L59) 定义了 `NCoreBackend(K=8, N=8, L=32)`，并用 `require` 守卫了 `L % 4 == 0`：

```scala
class NCoreBackend(
    val K: Int = 8,
    val N: Int = 8,
    val L: Int = 32,
) extends Module {
  require(L % 4 == 0, s"NCoreBackend: L=$L must be divisible by 4")
  require(K > 0 && N > 0)
```

`K==n` 的绑定在 MMALU 实例化处。[src/main/scala/backend/SimpleBackend.scala:156-158](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L156-L158) 把 `K` 作为第二个实参（也就是 `n`）传给 `MMALU`：

```scala
// MMALU (systolic array; n = K lanes, nbits = N)
val mmalu = Module(new MMALU(new MMPE(N), K, N, N4))  // (pe_gen, n=K, nbits=N, accum_nbits=N4=4N)
```

对照 [src/main/scala/alu/mma/mma.scala:23](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L23) 的 `MMALU` 形参列表 `class MMALU[...](pe_gen, val n: Int = 8, val nbits: Int = 8, val accum_nbits: Int = 32)`，可以清楚看到：实参 `K` → 形参 `n`（阵列边长），实参 `N` → 形参 `nbits`（通道位宽），实参 `N4 = 4*N` → 形参 `accum_nbits`（累加器位宽）。这就是「`K==n`」「`N==nbits`」在代码里的真实落点。

> 前置讲义 u1-l1 已提到「在 backend 边界 K==mmalu.n」——本讲用源码把这个约束精确地定位到了两行代码。

#### 4.1.4 代码实践：在三个文件里定位同一个参数

**实践目标**：亲手验证「同一个 N/L/K 定义在文档、注释、代码里是一致的」。

**操作步骤**：

1. 打开 `docs/index.md`，找到第 17–22 行的记号表，记下「Test default」一列。
2. 打开 `AGENTS.md` 第 34–38 行，对比「Default (test)」列是否一致。
3. 打开 `src/main/scala/isa/instrFormat.scala` 第 5–9 行的注释，核对注释里写的默认值。
4. 打开 `src/main/scala/backend/SimpleBackend.scala` 第 52–56 行，看 `NCoreBackend` 构造参数的默认值。

**需要观察的现象**：四处对 N/L/K 默认值的描述应当完全一致——`N=8`、`L=32`、测试态 `K=8`。

**预期结果**：你会确认 N=8、L=32、K=8（测试态）在四处都相同；同时注意到「Top 默认 K=64」这一栏只在文档表里出现（因为 top 入口目前只 elaborate MMALU，见 u1-l3）。这一致性正是项目刻意维护的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `K` 从 8 改成 16，但忘了改 MMALU 的 `n`，会发生什么？
**答案**：在 `NCoreBackend` 里不会出错，因为它直接把 `K` 当 `n` 传给 `MMALU`（见 SimpleBackend.scala:158），所以 `n` 会自动跟着变成 16。这正是「`K==n`」约束用代码强制的意义——你无法让二者不一致。

**练习 2**：`N(bits)` 为什么在文档里永远拼写为 `N(bits)` 而不是 `N`？
**答案**：因为代码库里还有别的 `N`（比如 `N2=2*N`、`N4=4*N`），单写 `N` 容易让人误以为是「数量」。`N(bits)` 强调它是「位宽」，等于 MMALU 的 `nbits`。

---

### 4.2 寄存器类别名 VX / VE / VR

#### 4.2.1 概念说明

VX / VE / VR **不是三块独立的存储**，而是**同一块物理存储的三种「视图」**。它们的差别只在「把多少个相邻字节打包成一个通道」：

| 类别 | 数量 | 每通道位宽 | 别名关系 |
|:---|:---:|:---:|:---|
| `VX[0..L-1]` | 32 | **N bits**（原生） | native（原生视图） |
| `VE[0..L/2-1]` | 16 | **2N bits** | `VE[i] = VX[2i] ∥ VX[2i+1]` |
| `VR[0..L/4-1]` | 8 | **4N bits** | `VR[i] = VX[4i..4i+3]` |

直觉解释：

- **VX** 是「原生」视图：每条 VX 寄存器是 K 个 N-bit 通道。N=8 时就是一个 INT8 向量。
- **VE** 把**相邻两条** VX 寄存器拼起来，每通道变成 2N 位（INT16）。因为每条 VE 占 2 条 VX，所以 VE 只有 L/2 条。
- **VR** 把**相邻四条** VX 寄存器拼起来，每通道变成 4N 位（INT32/FP32）。因为每条 VR 占 4 条 VX，所以 VR 只有 L/4 条。

这就是 **`L` 必须被 4 整除** 的根本原因：VR 每次要占 4 条 VX 行，如果 L 不是 4 的倍数，最后那组 VR 就会「凑不齐 4 行」，地址映射会越界、对不齐。源码用 `require(L % 4 == 0, ...)` 强制检查。

#### 4.2.2 核心流程：字节级别名是怎么实现的

物理存储只有一份，结构是 **L 行 × K 通道 × N 位**（每行对应一条 VX 寄存器）。三类视图只是**读 / 写时如何重新打包**这些字节：

- **读 VX[i]**：直接取第 i 行，得到 K 个 N 位通道。
- **读 VE[i]**：取第 `2i` 行（放低 N 位）和第 `2i+1` 行（放高 N 位），拼成 K 个 2N 位通道。
- **读 VR[i]**：取第 `4i, 4i+1, 4i+2, 4i+3` 四行，分别放到由低到高的 4 段，拼成 K 个 4N 位通道。
- **写 VR[i]**：把每个 4N 位通道拆成 4 段，**原子地**同时更新 4 行 VX——这正是文档说的「Writing VR[i] atomically updates the four underlying VX rows」（写 VR[i] 原子地更新底层 4 行 VX）。
- **冲突仲裁**：当 VX/VE/VR/ext 写端口同时写同一行时，按优先级 **VR > VE > VX > ext** 决定谁赢（last-writer-wins 由软件保证）。

总物理存储字节数（无论从哪个视图看都一样，因为它们共享同一份存储）：

\[
\text{Total bytes} = L \times K \times \frac{N}{8}
\]

测试态（L=32, K=8, N=8）就是 `32 × 8 × 1 = 256` 字节；Top（L=32, K=64, N=8）就是 `32 × 64 × 1 = 2 KiB`。

#### 4.2.3 源码精读

先看文档里的**寄存器类别名表**：

[docs/index.md:24-30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md#L24-L30) 给出 VX/VE/VR 的数量、通道位宽与别名公式，并点明三者共享同一块物理字节（`L × K × N/8`）。

[AGENTS.md:40-47](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L40-L47) 给出同样的别名表，并在第 48 行解释 MMALU `n` 与 `K` 的关系（呼应 4.1）。

再看实现。寄存器堆的文件头注释把别名规则讲得很清楚：

[src/main/scala/sram/multiWidthRegister.scala:5-31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L5-L31) 说明物理存储是 L 行 K 通道 N 位，VE[i] 由 VX[2i]∥VX[2i+1] 组成，VR[i] 由 VX[4i..4i+3] 组成，并写明地址位宽：VX 地址 5 位、VE 地址 4 位、VR 地址 3 位（log2 关系）。

[src/main/scala/sram/multiWidthRegister.scala:49-57](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L49-L57) 是那条强制约束与派生常量：

```scala
require(L % 4 == 0, s"MultiWidthRegisterBlock: L=$L must be divisible by 4")
...
val VE_SIZE = L / 2
val VR_SIZE = L / 4
```

物理存储本体只有一行：

[src/main/scala/sram/multiWidthRegister.scala:91](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L91) —— `val mem = RegInit(VecInit(Seq.fill(L)(VecInit(Seq.fill(K)(0.U(N.W))))))`，即 **L 行、每行 K 个 N 位**。VX/VE/VR 全部建立在这一份 `mem` 之上。

读 VE 如何「拼两行」：

[src/main/scala/sram/multiWidthRegister.scala:100-109](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L100-L109) —— 用 `baseRow = ve_r_addr ## 0.U(1.W)`（即地址乘 2）定位起始行，取 `mem(baseRow)` 作低 N 位、`mem(baseRow+1)` 作高 N 位，`Cat(hi, lo)` 拼出 2N 位通道。这正是别名公式 `VE[i] = VX[2i] ∥ VX[2i+1]` 的硬件实现。

读 VR 如何「拼四行」：

[src/main/scala/sram/multiWidthRegister.scala:111-121](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L111-L121) —— 用 `baseRow = vr_r_addr ## 0.U(2.W)`（即地址乘 4）定位起始行，取连续 4 行 `b0..b3`，`Cat(b3, b2, b1, b0)` 拼出 4N 位通道（VX[4i] 在最低位、VX[4i+3] 在最高位）。

写 VR 如何「拆四行」：

[src/main/scala/sram/multiWidthRegister.scala:171-183](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L171-L183) —— 写 VR 时按 `sub = 0..3` 把每个 4N 位通道切成 4 段，同时置 `wr_en` 于 4 行，从而**原子地**更新底层 4 条 VX。

> 一个本讲只需「知道」、细节留待 u3-l2 的点：[multiWidthRegister.scala:82-86](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L82-L86) 里的 `ext_r_addr`（外部 VX 宽度读端口）即使不用也**必须被驱动**，否则 firtool 会报「uninitialized sink」错误。这是后端连线时的一个 gotcha。

#### 4.2.4 代码实践：手推一次 VR[0] 的写后读别名

**实践目标**：通过纯源码阅读，验证「写 VR[0] 会改变 VX[0..3] 的读出」。

**操作步骤**：

1. 假设向 `VR[0]` 写入一个 4N 位通道值（每通道 32 位，低到高 4 段分别是 `A0, A1, A2, A3`）。
2. 对照 [multiWidthRegister.scala:171-183](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L171-L183)，写出 `sub=0..3` 时分别写到哪些行：`row = base + sub = 0+sub`，即第 0、1、2、3 行。
3. 对照 [multiWidthRegister.scala:111-121](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L111-L121)，预测随后读 `VX[0]`、`VX[1]`、`VX[2]`、`VX[3]` 会分别得到哪一段。

**需要观察的现象**：写 VR[0] 后，`VX[0] = A0`、`VX[1] = A1`、`VX[2] = A2`、`VX[3] = A3`（最低段落到最低行）。

**预期结果**：你确认了「写一条 VR 等于同时写 4 条 VX」——这就是别名的本质。（完整仿真验证见 u3-l2 的 `MultiWidthRegisterSpec`，本讲只做纸面推导。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 VE 有 L/2 条，而 VR 只有 L/4 条？
**答案**：因为 VE 每条占 2 行 VX，VR 每条占 4 行 VX。总行数都是 L，所以 VE = L/2，VR = L/4。

**练习 2**：用测试态参数算出 VR 寄存器一条的总位宽。
**答案**：一条 VR = K 个通道 × 4N 位 = 8 × 32 = 256 位 = 32 字节。

**练习 3**：如果 `L=30`（不被 4 整除），会发生什么？
**答案**：`MultiWidthRegisterBlock` 与 `NCoreBackend` 的 `require(L % 4 == 0, ...)` 会在 elaborate 阶段直接抛异常，硬件根本生成不出来。这就是把约束写进 `require` 的意义。

---

### 4.3 `funct7` 中的 width 编码与 `VecWidth` 枚举

#### 4.3.1 概念说明

每条向量指令需要告诉硬件：「我这次操作的是 VX、VE 还是 VR？」这个选择编码在 32 位指令字的 **`funct7[1:0]`** 这 2 个比特里，由 `VecWidth` 枚举定义取值：

| `funct7[1:0]` | `VecWidth` 枚举值 | 含义 | 通道位宽 |
|:---:|:---:|:---|:---:|
| `00` | `VX` | 操作 VX 类 | N bits |
| `01` | `VE` | 操作 VE 类 | 2N bits |
| `10` | `VR` | 操作 VR 类 | 4N bits |
| `11` | `VW_RSV` | 保留（译码器判为非法） | — |

也就是说，**同一条加法指令**，配上不同的 `funct7[1:0]`，就会分别做 INT8 / INT16 / INT32 的逐通道加法，并写回对应类别的寄存器。这是「一条指令、三种宽度」的关键。

> 一个跨讲义伏笔：在译码后送到 VALU 的控制 Bundle 里，这个字段被**改名**为 `regCls`（原叫 `width`，为避免和 Chisel 的 `chisel3.Width` 冲突）。本讲只需知道「`funct7[1:0]` 选寄存器类」，`regCls` 的细节留待 u3-l1。

#### 4.3.2 核心流程：2 比特如何选择寄存器类

整条链路是：

1. 汇编器把 `width`（一个 `VecWidth` 枚举值）放进 `funct7[1:0]`（见 `Funct7Attrs.encode`）。
2. 译码器从 32 位指令字里取出 `funct7[25..31]`，再取其低 2 位 `funct7[1:0]` 得到宽度码。
3. 后端依据这个 2 位码（即 `regCls`）决定从哪类读端口取操作数、结果写回哪类写端口。

`funct7` 一共 7 位，除了 width 还有别的属性（round/sat/dtype）。完整的 `funct7` 子字段布局如下（本讲重点看 `[1:0]`）：

| 子字段 | 在 funct7 中的位 | 含义 |
|:---|:---:|:---|
| **width** | **[1:0]** | **VX=0 / VE=1 / VR=2 / 3=保留** |
| round | [3:2] | 舍入模式（RNE/RTZ/floor/ceil） |
| sat | [4] | 是否饱和（0=回绕 / 1=饱和） |
| dtype | [6:5] | 数据类型（INT/FP/BF） |

#### 4.3.3 源码精读

`VecWidth` 枚举本身的定义非常短：

[src/main/scala/isa/instrFormat.scala:72-77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L72-L77) 定义了四个值：

```scala
object VecWidth extends ChiselEnum {
  val VX     = Value(0.U(2.W))   // N(bits)-wide lanes
  val VE     = Value(1.U(2.W))   // 2N-wide lanes
  val VR     = Value(2.U(2.W))   // 4N-wide lanes
  val VW_RSV = Value(3.U(2.W))   // reserved
}
```

`funct7` 子字段位段的常量在 `InstrBits` 对象里：

[src/main/scala/isa/instrFormat.scala:57-60](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L57-L60) 给出 `F7_WIDTH_LO = 0`、`F7_WIDTH_HI = 1`，即 width 占 funct7 的第 0、1 位。

文件头注释里的属性布局表与之一致：

[src/main/scala/isa/instrFormat.scala:17-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L17-L22) 写明 `[1:0] width : 00=VX 01=VE 10=VR 11=reserved`。

`Funct7Attrs.encode` 给出了 width 在编码时的真实位位置：

[src/main/scala/isa/instrFormat.scala:117-125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L117-L125) —— `(width & 3) | ((round & 3) << 2) | ... | ((dtype & 3) << 5)`。其中 `width & 3` 不移位，直接落在最低 2 位 `[1:0]`，round 落在 `[3:2]`，dtype 落在 `[6:5]`。这印证了上表。

#### 4.3.4 代码实践：手算一条指令的 width 比特

**实践目标**：确认「VE 类、不饱和、INT、RNE 舍入」对应 `funct7` 的哪一个 7 位值。

**操作步骤**：

1. 查 `VecWidth`：VE = 1，所以 `width = 0b01`。
2. 查 round：RNE = 0，所以 `round = 0b00`，放在 `[3:2]`。
3. 查 sat：不饱和 = 0，放在 `[4]`。
4. 查 dtype：INT = 0，所以 `dtype = 0b00`，放在 `[6:5]`。
5. 按 `Funct7Attrs.encode` 的公式拼：`width | (round<<2) | (sat<<4) | (dtype<<5)` = `0b01 | 0 | 0 | 0 = 0b0000001 = 0x01`。

**需要观察的现象**：最终 `funct7 = 0x01`，其低 2 位 `01` 正是 VE。

**预期结果**：你验证了「width 永远住在 funct7 的最低 2 位」。把 dtype 改成 FP（=1）会得到 `0x01 | (0b01<<5) = 0x21`——低 2 位仍是 `01`，width 不变。

#### 4.3.5 小练习与答案

**练习 1**：一条 `vadd` 指令，`funct7[1:0] = 0b10`，它操作哪类寄存器？每通道几位？
**答案**：`0b10 = 2 = VR`，操作 VR 类，每通道 4N = 32 位（INT32）。

**练习 2**：为什么 `VW_RSV (3)` 要单独留出来？
**答案**：3 位宽的 2 比特最大能表示 4 个值，但只用到 VX/VE/VR 三个。把第 4 个值标为「保留」让译码器能识别非法指令——若指令里出现 `width=3`，译码器会置 `illegal`（见 u2-l5）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这道「纸上推演 + 源码定位」的综合任务（这是本讲的主实践任务，**待本地验证**部分需在有 Docker 环境时自行核对）。

**任务背景**：假设硬件配置为 **N=8、L=32、K=8**（即测试态默认值）。

**第 1 步——计算三类寄存器的规模**。填出下表（参考 4.2 的公式与源码）：

| 类别 | 寄存器数量 | 每寄存器位宽 | 占用字节 |
|:---|:---:|:---:|:---:|
| VX | ? | K×N = ? | ? |
| VE | ? | K×2N = ? | ? |
| VR | ? | K×4N = ? | ? |
| **总物理存储** | — | — | L×K×N/8 = ? |

**参考答案**：

| 类别 | 数量 | 每寄存器位宽 | 字节 |
|:---|:---:|:---:|:---:|
| VX | L = **32** | 8×8 = **64 位** = 8 B | 32×8 = **256 B** |
| VE | L/2 = **16** | 8×16 = **128 位** = 16 B | 16×16 = **256 B** |
| VR | L/4 = **8** | 8×32 = **256 位** = 32 B | 8×32 = **256 B** |
| **总物理存储** | — | — | 32×8×1 = **256 B** |

注意三类视图各自「乘开」都是 256 字节——这**不是巧合**，正是因为它们别名到同一块 256 字节物理存储。

**第 2 步——定位 width 编码位段**。在 `src/main/scala/isa/instrFormat.scala` 中找到编码「width 选择」的 `funct7` 位段，并写出对应的常量名。

**参考答案**：width 占 `funct7[1:0]`，对应常量是 `InstrBits.F7_WIDTH_LO = 0` 与 `F7_WIDTH_HI = 1`（见 instrFormat.scala:57-58）；取值由 `VecWidth` 枚举给出（VX=0/VE=1/VR=2，见 instrFormat.scala:72-77）。

**第 3 步——写出别名下标公式**。把 VE[i]、VR[i] 表示成若干条 VX 的拼接（注意高低位顺序，依据 multiWidthRegister.scala 的 `Cat` 顺序）。

**参考答案**（逐通道，`∥` 表拼接，左为高位）：

\[
\text{VE}[i] \equiv \text{VX}[2i+1] \;\|\; \text{VX}[2i]
\]

\[
\text{VR}[i] \equiv \text{VX}[4i+3] \;\|\; \text{VX}[4i+2] \;\|\; \text{VX}[4i+1] \;\|\; \text{VX}[4i]
\]

即 VE[i] 把 VX[2i] 放在低 N 位、VX[2i+1] 放在高 N 位；VR[i] 把 VX[4i] 放在最低 N 位、VX[4i+3] 放在最高 N 位。这与源码 `Cat(hi, lo)`（VE 读，第 107 行）和 `Cat(b3, b2, b1, b0)`（VR 读，第 119 行）完全对应。

**第 4 步（可选，待本地验证）**：在容器内运行 `make container` 进入镜像，启动 `sbt console`，`import isa.NpuAssembler._`，用 `vadd(rd=0, rs1=1, rs2=2, width=VE, sat=false)` 打印指令字的 32 位十六进制值，检查其 `funct7`（高 7 位）低 2 位是否为 `01`（VE）。由于本讲聚焦参数与寄存器类概念，此步的完整指令构造留待 u2-l4。

## 6. 本讲小结

- **N(bits)、L、K** 是贯穿全库的三个全局参数：N=基础通道位宽（默认 8）、L=VX 寄存器数（默认 32，须被 4 整除）、K=SIMD 通道数（测试 8 / top 64）。
- 在 `NCoreBackend` 边界上 **`K == mmalu.n`**、**`N == mmalu.nbits`**，由 `new MMALU(new MMPE(N), K, N, N4)` 这一行强制（SimpleBackend.scala:158）。
- **VX / VE / VR** 是同一块物理存储（`L×K×N/8` 字节）的三种视图：VE 把相邻 2 条 VX 拼成 2N 位，VR 把相邻 4 条 VX 拼成 4N 位。
- **`L` 必须被 4 整除**，因为每条 VR 要占 4 行 VX；代码用 `require(L % 4 == 0, ...)` 守卫。
- 一条指令通过 **`funct7[1:0]`** 这 2 个比特选择操作哪类寄存器，取值由 `VecWidth` 枚举定义（VX=0/VE=1/VR=2/保留=3）。
- 测试态总物理存储 = 32×8×1 = **256 字节**；三类视图各自乘开也都是 256 字节，印证「别名」关系。

## 7. 下一步学习建议

本讲建立了 N/L/K 与三类寄存器的全局心智模型，接下来的学习路径：

- **想看别名机制的完整仿真验证** → 阅读 **u3-l2（多宽度寄存器堆）**，它会带你跑 `MultiWidthRegisterSpec`，亲手 poke/peek 验证「写 VR、读 VX」的别名。
- **想了解指令格式如何整体编码** → 阅读 **u2-l1（32 位指令编码格式 R/I/S）**，本讲的 `funct7[1:0]` 只是 `funct7` 的一部分。
- **想看 `width` 译码后如何变成 `regCls` 控制信号** → 阅读 **u3-l1（微操作与控制 Bundle）**，了解 `NCoreVALUBundle` 里那个被改名的字段。
- **直接看源码**：`src/main/scala/isa/instrFormat.scala`（枚举与位段）、`src/main/scala/sram/multiWidthRegister.scala`（别名实现）、`src/main/scala/backend/SimpleBackend.scala`（参数绑定）是巩固本讲的最佳三份文件。
