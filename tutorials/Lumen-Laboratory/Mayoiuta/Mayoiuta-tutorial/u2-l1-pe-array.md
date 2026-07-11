# 处理单元与脉动阵列 PE Array

> 本讲属于「核心计算通路」单元（u2），承接 [u1-l3 顶层 SoC 架构](./u1-l3-soc-top.md)。
> 在上一讲里，我们把 `NPU_SOC` 看作一张「装配图」，知道它例化了很多个 `npu_core`，但核内部到底怎么算，我们刻意留下了「待确认」。从本讲开始，我们真正进入核里的计算电路。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚什么是**脉动阵列（systolic array）**，以及它为什么比「一个一个算」更适合做神经网络的乘加。
- 读懂 [`pe_array.v`](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L1-L86) 里的两个模块：阵列 `PE_Array` 与单个处理单元 `Processing_Element`。
- 解释数据如何从 **north/west 流向 south/east**，以及边界 PE（`i==0`、`j==0`）为什么要用三目运算符挑选输入。
- 解释三条 `opcode`（`0x1` 加载权重、`0x2` 乘加、`0x3` 直通）各自做什么，以及 `accumulator`、`weight_reg` 这两个寄存器的作用。
- 自己给 `Processing_Element` 增加一条新的 `opcode`，并说清楚它对阵列数据流的影响。

## 2. 前置知识

在进入源码之前，先用最直白的方式建立两个直觉。

### 2.1 为什么 NPU 离不开「乘加（MAC）」

神经网络里最频繁的运算是**乘加**：把一个输入激活值 \(a\) 乘以一个权重 \(w\),再累加到一个结果上。

\[
\text{accumulator} \leftarrow \text{accumulator} + a \times w
\]

一条这样的指令叫一个 **MAC（Multiply-Accumulate）**。一层卷积或全连接层，本质上就是**几千万到几亿次 MAC**。CPU 一次只能算寥寥几个，GPU 靠成百上千个核心并行算，而 NPU 的做法是**直接在硅片上铺一张由专用乘加电路组成的网格**，让数据像流水一样从网格的一侧流进去、从另一侧流出来，沿途每个网格点都在不停地做 MAC。这张网格就是**脉动阵列**。

### 2.2 什么是「脉动」

「脉动（systolic）」是个比喻：像心脏一下一下地泵血。在电路上，它指**每个时钟节拍，所有处理单元同时把数据往前推一格**。数据不是「算完一个再去取下一个」，而是像波纹一样从阵列的左上角向右下角扩散，每个 PE（Processing Element，处理单元）在每个节拍都「吃进一个数、算一次 MAC、把数据吐给下一个 PE」。

这种结构的好处是：**只需要很少的对外读写，就能让大量 PE 同时干活**——因为数据一旦进入阵列，就在 PE 之间就地传递，不必每拍都跑回主存去取。这正是 NPU 省电又快的根源。

> 术语速查：**PE**（Processing Element，处理单元）= 阵列里的一个格子；**MAC** = 乘加；**opcode** = 操作码，告诉 PE 这一拍该干哪种活；**权重驻留** = 把权重一次性装进 PE，之后反复使用，不再每拍重新取。

## 3. 本讲源码地图

本讲只精读一个文件，但它含两个模块：

| 文件 | 模块 | 行号 | 作用 |
|---|---|---|---|
| `hardware/rtl/core/pe_array.v` | `PE_Array` | L1–L42 | **阵列**：用 `generate-for` 把一堆 `Processing_Element` 铺成 \(N \times N\) 的网格，并连好数据线。 |
| `hardware/rtl/core/pe_array.v` | `Processing_Element` | L44–L86 | **单个处理单元**：真正做「装权重 / 乘加 / 直通」的电路。 |

整颗芯片里，`PE_Array` 会被上一层的计算核（`npu_core`，源码当前仓库未提供，**待确认**）例化，作为核里干 MAC 苦力的部件。本讲我们只聚焦这两个模块本身。

> 说明：仓库目前**没有**为 `pe_array.v` 提供测试平台（testbench）或仿真脚本。所以本讲凡是涉及「跑起来看波形」的环节，都会标注 **待本地验证**，并给出你可以自己写的最小 testbench 骨架。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：先看「网格」`PE_Array`（4.1），再看「格子」`Processing_Element`（4.2）。建议先读 4.1 建立全局，再用 4.2 理解细节。

---

### 4.1 脉动阵列 PE_Array：把乘加铺成一张网格

#### 4.1.1 概念说明

`PE_Array` 要解决的问题是：**怎样把大量相同的 PE 组织起来，让数据自动在它们之间流动，而不用外面的控制逻辑去一根线一根线地指挥。**

它的思路非常几何化：

- 把 PE 排成 \(N \times N\) 的方阵（这里 \(N\) 就是参数 `ARRAY_SIZE`，默认 8）。
- 给阵列**两个方向的输入**：
  - **north_in**：从阵列**顶部**灌进来的一列数（每个 PE 一份）。
  - **west_in**：从阵列**左侧**灌进来的一行数（每个 PE 一份）。
- 让数据**自然地往南（下）和往东（右）流**：
  - 自上而下：每个 PE 把自己「吃进」的北边数据，从南边吐给正下方的 PE。
  - 自左而右：每个 PE 把自己「吃进」的西边数据，从东边吐给右边的 PE。
- 阵列的**输出**自然汇聚在两条边上：
  - **south_out**：最底行各 PE 的南向输出。
  - **east_out**：最右列各 PE 的东向输出。

为什么要这样设计？因为卷积里，**同一份激活要和一整列不同的权重相乘，同一份权重要和一整行不同的激活相乘**。让激活纵向流动、权重横向流动，两股数据流在每个 PE 的格子里「相遇」一次，就完成一次 MAC——这正是经典**权重驻留型脉动阵列**（weight-stationary systolic array）的拓扑。

#### 4.1.2 核心流程

`PE_Array` 本身**不计算**，它只做两件事：**铺格子**和**连线**。它的工作流程可以这样描述：

1. **参数化**：用 `DATA_WIDTH`（数据位宽，默认 16）和 `ARRAY_SIZE`（边长，默认 8）两个参数决定阵列规模。
2. **铺格子**：用 `generate-for` 双重循环，例化 `ARRAY_SIZE × ARRAY_SIZE` 个 `Processing_Element`，记第 \(i\) 行第 \(j\) 列的那个为 \(\text{PE}(i,j)\)。
3. **连「纵向」数据线**：
   - 第 0 行（\(i=0\)）的 PE，北边输入直接取**外部** `north_in[j]`。
   - 其余行（\(i>0\)）的 PE，北边输入取**正上方** \(\text{PE}(i-1,j)\) 的 `south_out`。
4. **连「横向」数据线**：
   - 第 0 列（\(j=0\)）的 PE，西边输入直接取**外部** `west_in[i]`。
   - 其余列（\(j>0\)）的 PE，西边输入取**左边** \(\text{PE}(i,j-1)\) 的 `east_out`。
5. **汇聚输出**：把最底行的 `south_out` 接到顶层 `south_out`；把最右列的 `east_out` 接到顶层 `east_out`（**此处源码写法存疑，见 4.1.3**）。
6. **广播控制**：`opcode`、`start`、`clk`、`rst_n` 同一时钟域内广播给所有 PE；所有 PE 的 `done` 也汇到同一根线（**多驱动，见 4.1.3**）。

用伪代码画出一张 \(4 \times 4\) 阵列的数据流向（箭头表示数据流方向）：

```
        north_in[0]  north_in[1]  north_in[2]  north_in[3]
            ↓            ↓            ↓            ↓
west_in[0]→ PE(0,0) →e→ PE(0,1) →e→ PE(0,2) →e→ PE(0,3)
            ↓s           ↓s           ↓s           ↓s
west_in[1]→ PE(1,0) →e→ PE(1,1) →e→ PE(1,2) →e→ PE(1,3)
            ↓s           ↓s           ↓s           ↓s
west_in[2]→ PE(2,0) →e→ PE(2,1) →e→ PE(2,2) →e→ PE(2,3)
            ↓s           ↓s           ↓s           ↓s
west_in[3]→ PE(3,0) →e→ PE(3,1) →e→ PE(3,2) →e→ PE(3,3)
            ↓s           ↓s           ↓s           ↓s
         south_out[0] south_out[1] south_out[2] south_out[3]
```

（`→e→` 表示东向流，`↓s` 表示南向流。）

#### 4.1.3 源码精读

**① 模块端口与参数** —— [hardware/rtl/core/pe_array.v:1-14](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L1-L14)

这段声明了 `PE_Array` 的「对外契约」：两个参数（`DATA_WIDTH=16`、`ARRAY_SIZE=8`），以及时钟/复位、两组输入数组（`north_in`、`west_in`）、两组输出数组（`south_out`、`east_out`）、4 位 `opcode`、`start`、`done`。注意 `north_in`/`west_in`/`south_out`/`east_out` 都是**非打包数组（unpacked array）端口**，每个含 `ARRAY_SIZE` 个 `DATA_WIDTH` 位宽的元素——也就是「阵列每行/每列一个数」。

**② 内部连线网** —— [hardware/rtl/core/pe_array.v:16-17](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L16-L17)

这里声明了两张二维线网：`pe_data_h`（横向，承载 east/west 数据）和 `pe_data_v`（纵向，承载 north/south 数据）。它们就是「PE 之间传递数据的水管」。`[ARRAY_SIZE][ARRAY_SIZE]` 表示这是一个 \(N \times N\) 的二维数组，对应每个 PE 的输出。

**③ generate-for 铺格子 + 边界选择** —— [hardware/rtl/core/pe_array.v:19-37](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L19-L37)

这是全模块最关键的一段。双重 `for` 循环里，每个 \((i,j)\) 都例化一个 `Processing_Element`。最值得读的是这两行三目运算符（L27–L28）：

```verilog
.north_in(i == 0 ? north_in[j]   : pe_data_v[i-1][j]),
.west_in (j == 0 ? west_in[i]    : pe_data_h[i][j-1]),
```

中文翻译：**「如果我贴着上边界（\(i==0\)），北边数据就从外部端口取；否则从正上方那个 PE（\(\text{PE}(i-1,j)\)）的南向输出取。如果我贴着左边界（\(j==0\)），西边数据就从外部端口取；否则从左边那个 PE（\(\text{PE}(i,j-1)\)）的东向输出取。」**

这三目运算符正是 4.1.2 里说的「边界 PE 用外部输入，内部 PE 用相邻 PE 的输出」的实现。如果省掉它、直接写 `pe_data_v[i-1][j]`，那么 \(i=0\) 时会出现 `pe_data_v[-1][j]` 这种**非法下标**——所以这个判断不是多余的，是必须的。

**④ 输出汇聚（含一处待确认）** —— [hardware/rtl/core/pe_array.v:39-40](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L39-L40)

```verilog
assign south_out = pe_data_v[ARRAY_SIZE-1];
assign east_out  = pe_data_h[ARRAY_SIZE][ARRAY_SIZE-1];
```

- 第 39 行：`pe_data_v[ARRAY_SIZE-1]` 取的是纵向线网的**最后一行**，也就是最底行各 PE 的南向输出，赋给 `south_out`。语义正确 ✅。
- 第 40 行：**这里有一个明显的问题** ⚠️。`pe_data_h` 在第 16 行声明为 `pe_data_h [ARRAY_SIZE][ARRAY_SIZE]`，两个维度的合法下标都是 `0 .. ARRAY_SIZE-1`（即 `0..7`）。但第 40 行却写了 `pe_data_h[ARRAY_SIZE][...]`，也就是 `pe_data_h[8][...]`——**下标越界**。按对称性，东向输出本应收集**最右列**各 PE（即 `pe_data_h[i][ARRAY_SIZE-1]`，对所有 \(i\)）的 east_out，而当前写法既越界、也无法表达「逐行收集最右列」的语义。**此行的正确写法与意图，标注为「待确认」。**

> 这是本讲第一个「待确认」点。在源码阅读型项目里，遇到这类下标越界或语义不清的写法，正确做法是如实记录、不替原作者脑补，等后续维护者修正或给出测试平台后再下结论。

#### 4.1.4 代码实践（源码阅读型）

> **实践目标**：亲手验证 4.1.3 里的边界选择逻辑，并定位第 40 行的越界问题。

**操作步骤**：

1. 把 `ARRAY_SIZE` 在脑海里临时改成 2，画出 \(2 \times 2\) 阵列，标出每个 PE 的 \((i,j)\)。
2. 对每个 PE，依据 L27–L28 的三目运算符，填出它的 `north_in` 和 `west_in` 分别来自哪里（外部端口 or 哪个相邻 PE）。
3. 对照 L39–L40，写出你心目中 `south_out`、`east_out` 「应该」收集的是哪些 PE 的输出。
4. 把你的预期和第 40 行的实际写法对比，指出不一致之处。

**需要观察的现象 / 预期结果**：

- 4 个 PE 中，\(\text{PE}(0,0)\)、\(\text{PE}(0,1)\) 的 `north_in` 取自外部 `north_in[0]`、`north_in[1]`；\(\text{PE}(1,0)\)、\(\text{PE}(1,1)\) 的 `north_in` 取自 \(\text{PE}(0,0)\)、\(\text{PE}(0,1)\) 的 `south_out`。
- `east_out` 按对称性应取最右列（\(\text{PE}(i,1)\)）的输出，但第 40 行写成 `pe_data_h[2][1]`，下标 `2` 已越界（合法范围为 `0..1`）。
- 结论：第 40 行存在下标越界，**待确认**其本意。

**运行结果**：本实践为源码阅读型，无需运行；若你想用仿真器验证，需自行编写 testbench（仓库未提供），结果 **待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`ARRAY_SIZE` 默认为 8 时，`PE_Array` 里一共例化了多少个 `Processing_Element`？

> **答**：`ARRAY_SIZE × ARRAY_SIZE = 8 × 8 = 64` 个。

**练习 2**：为什么第 27 行不能直接写成 `.north_in(pe_data_v[i-1][j])`？

> **答**：当 `i==0`（第一行）时，`i-1` 等于 -1，是非法下标。三目运算符 `i==0 ? north_in[j] : pe_data_v[i-1][j]` 保证了第一行从外部端口取数，其余行从正上方 PE 取数。

**练习 3**：`south_out` 和 `east_out` 在「几何上」分别对应阵列的哪两条边？

> **答**：`south_out` 对应**底边**（最底行各 PE 的南向输出）；`east_out` 对应**右边**（最右列各 PE 的东向输出）。注意源码第 40 行对 `east_out` 的实现存在越界，**待确认**。

---

### 4.2 单个处理单元 Processing_Element：权重、累加与三条 opcode

#### 4.2.1 概念说明

`Processing_Element`（PE）是阵列里真正干活的「格子」。它要做的事其实很简单：**记住一个权重，然后反复把流经自己的激活值乘以这个权重，累加到一个寄存器里。**

为此，每个 PE 内部有两个关键寄存器：

- **`weight_reg`**（权重寄存器）：存放「属于这个 PE」的权重。权重一旦装进来，就**驻留**在这里，之后每拍反复用，不必再去外面取。
- **`accumulator`**（累加器）：存放「到目前为止这个 PE 算出的乘加之和」。每来一个激活，就往上加一次 \(a \times w\)。

PE 用一个 4 位的 `opcode` 来决定「这一拍干哪种活」，目前定义了三种：

| opcode | 名称 | 含义 | 是否更新 `south_out`/`east_out` |
|---|---|---|---|
| `0x1` | 加载权重 | 把西边来的数装进 `weight_reg` | 否（数据不往下/往右传） |
| `0x2` | 乘加（MAC） | `accumulator += north_in * weight_reg` | 否（同上） |
| `0x3` | 直通（passthrough） | 北→南、西→东，原样转发 | 是（数据继续流动） |
| 其它 / `0x0` | 默认 | 什么都不做，`done` 拉低 | 否 |

注意一个重要细节：**只有在 `0x3` 直通模式下，数据才会往南、往东继续流**。在 `0x1` 加载权重和 `0x2` 乘加模式下，PE 不会更新 `south_out`/`east_out`——也就是说，这两个模式下阵列的「数据泵」是**暂停**的。这会影响整个阵列的运行节奏（见 4.2.4 的实践讨论）。

**一个乘累加的数值例子**：假设某 PE 的 `weight_reg = 3`，连续三拍 `north_in` 依次收到 `2`、`4`、`5`，`accumulator` 初值为 0。则：

\[
\begin{aligned}
\text{第 1 拍后：} \quad & \text{accumulator} = 0 + 2 \times 3 = 6 \\
\text{第 2 拍后：} \quad & \text{accumulator} = 6 + 4 \times 3 = 18 \\
\text{第 3 拍后：} \quad & \text{accumulator} = 18 + 5 \times 3 = 33
\end{aligned}
\]

最终 `accumulator = 33`，正是激活序列 \((2,4,5)\) 与权重 \(3\) 的「加权求和」\((2+4+5)\times 3 = 33\)。把每个 PE 的最终累加值收集起来，就是这一片乘加运算的结果。

#### 4.2.2 核心流程

`Processing_Element` 是一个**时钟同步、低有效复位**的状态机。每个时钟上升沿（或复位下降沿）它做一次决策：

1. 若 `rst_n` 为低（复位）：把 `accumulator`、`weight_reg`、`done` 全部清零。
2. 否则，按 `opcode` 分派：
   - `0x1`：`weight_reg ← west_in`；`done ← 1`。（装权重）
   - `0x2`：`accumulator ← accumulator + north_in * weight_reg`；`done ← 1`。（乘加）
   - `0x3`：`south_out ← north_in`；`east_out ← west_in`；`done ← 1`。（直通转发）
   - 其它：`done ← 0`。（空闲）
3. 每个 `opcode` 处理完后，`done` 拉高一个周期，向上层报告「这一拍我做完了」。

流程图：

```
          ┌─────────────── rst_n == 0 ? ───────────────┐
          │                                              │
        是│                                            否│
          ▼                                              ▼
   清零 accumulator/                            case(opcode)
   weight_reg/done                              ┌──0x1── weight_reg←west_in; done←1
                                                ├──0x2── accumulator←accumulator+north_in*weight_reg; done←1
                                                ├──0x3── south_out←north_in; east_out←west_in; done←1
                                                └──default── done←0
```

#### 4.2.3 源码精读

**① 端口与寄存器声明** —— [hardware/rtl/core/pe_array.v:44-59](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L44-L59)

这段定义了 PE 的端口：北入 `north_in`、西入 `west_in`、南出 `south_out`、东出 `east_out`、4 位 `opcode`、`start`、`done`，以及内部的 `accumulator`、`weight_reg` 两个寄存器（L58–L59）。

> **待确认（第二个）**：端口列表里的 `start`（L54）在整个模块里**从未被使用**——`always` 块既不读它，也不依据它启动任何操作。也就是说，目前 PE 并没有「等 start 信号才开始」的门控逻辑。这是设计上待确认的一点：`start` 要么是预留未实现，要么应参与 `done` 或状态控制。

**② 时钟同步 + 复位** —— [hardware/rtl/core/pe_array.v:61-66](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L61-L66)

`always @(posedge clk or negedge rst_n)` 是标准的「时钟上升沿触发、复位低有效」写法。复位时三个状态量清零，保证上电后 PE 处于干净状态。

**③ 三条 opcode** —— [hardware/rtl/core/pe_array.v:67-82](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L67-L82)

这是 PE 的「大脑」，逐条对应 4.2.1 的表格：

```verilog
case(opcode)
    4'h1: begin weight_reg <= west_in;                      done <= 1; end  // 加载权重
    4'h2: begin accumulator <= accumulator + (north_in*weight_reg); done<=1; end  // 乘加
    4'h3: begin south_out <= north_in; east_out <= west_in; done <= 1; end  // 直通
    default: done <= 0;
endcase
```

关于第 73 行的乘加，有一个**设计上的取舍**值得指出：

```verilog
accumulator <= accumulator + (north_in * weight_reg);
```

`north_in` 和 `weight_reg` 都是 `DATA_WIDTH`（默认 16）位。两个 16 位数相乘，完整结果应是 **32 位**，但 `accumulator` 只有 16 位，于是**高位被截断**。这在硬件上很常见（为了省面积、省功耗），但也意味着：**累加过程中如果中间结果超过 16 位，就会溢出**。真实 NPU 通常会配合「定点缩放（requantization）」或更宽的累加器来处理这件事——本模块没做，**这是一个简化实现**，使用时需注意位宽规划。

**④ 关于 `done` 的多驱动（第三个待确认）**

回到 [L33](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L33)：`PE_Array` 在 `generate-for` 里把**每一个** PE 的 `done` 都接到顶层同一根 `done` 线上。也就是说，`ARRAY_SIZE×ARRAY_SIZE`（默认 64）个 PE 的 `done` 输出**全部驱动同一根线**。

- 在**仿真**里，由于所有 PE 收到同一个 `opcode`、按相同逻辑计算 `done`，它们会驱动出相同的值，表面上「看起来没问题」。
- 但在**真实综合**里，多个 `reg` 输出驱动同一根线属于**多驱动（multi-driver）**，通常会被综合工具报错或导致总线竞争。

因此「所有 PE 共享一根 `done`」这种接法，**待确认**其是否为有意为之（例如是否本意应是「线与」或「开漏」），还是应当在上一层用 `done-and`/`done-or` 树来归并。

#### 4.2.4 代码实践（修改型，主实践）

> **实践目标**：给 `Processing_Element` 增加一条新的 `opcode`（加偏置 bias），加深对 `case` 分派与「数据流是否暂停」的理解。
>
> ⚠️ 本实践**不修改仓库源码**。下面的代码是**示例代码**，请在你自己的临时副本或草稿文件中尝试。

**操作步骤**：

1. 在 [L67–L82](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L67-L82) 的 `case` 里，新增一条 `0x4` 分支。`opcode` 是 4 位，可用范围 `0x0..0xF`，`0x1/0x2/0x3` 已占用、`0x0` 落入 `default`，所以 `0x4` 是空闲的。

示例代码（加偏置，把 `west_in` 当作偏置值加到累加器）：

```verilog
// 示例代码：新增 opcode 0x4 —— 加偏置 bias
4'h4: begin
    accumulator <= accumulator + west_in;   // west_in 这一拍当作 bias
    done        <= 1;
end
```

2. 思考这条新分支**对阵列数据流的影响**：和 `0x1`、`0x2` 一样，这条分支**没有**给 `south_out`、`east_out` 赋值，所以执行加偏置的这一拍，数据**不会**往南、往东推进——阵列的「泵」暂停一拍。

3. （进阶）如果你希望加偏置的同时**数据照常流动**，可以补上转发：

```verilog
// 示例代码：加偏置且同时转发数据
4'h4: begin
    accumulator <= accumulator + west_in;
    south_out   <= north_in;   // 北 → 南
    east_out    <= west_in;    // 西 → 东
    done        <= 1;
end
```

**需要观察的现象 / 预期结果**：

- 加入 `0x4` 后，向 PE 发 `opcode=4'h4`，应看到 `accumulator` 每拍增加 `west_in` 的值，`done` 拉高。
- 若采用第 2 步写法（不转发），执行 `0x4` 期间下游 PE 收不到新数据；若采用第 3 步写法（转发），下游 PE 会继续收到流动的数据。
- 复位后 `accumulator` 应为 0；连续多次 `0x4` 后 `accumulator` 应等于各次 `west_in` 之和（注意 16 位截断）。

**运行结果**：仓库未提供 testbench，以上行为 **待本地验证**。若要验证，可参考第 5 节综合实践里给出的最小 testbench 骨架。

#### 4.2.5 小练习与答案

**练习 1**：某 PE 当前 `accumulator=6`、`weight_reg=3`、`north_in=4`，本拍 `opcode=0x2`。下一拍 `accumulator` 是多少？

> **答**：\(6 + 4 \times 3 = 18\)。

**练习 2**：`opcode=0x1`（加载权重）这一拍，PE 会不会把数据往南、往东传？为什么？

> **答**：不会。`0x1` 分支只更新 `weight_reg` 和 `done`，并没有给 `south_out`、`east_out` 赋值，所以这一拍数据不流动。

**练习 3**：两个 16 位数相乘，结果最多需要多少位？本模块的 `accumulator` 是多少位？这意味着什么？

> **答**：两个 16 位数相乘最多需要 32 位；而 `accumulator` 只有 16 位（`DATA_WIDTH`）。这意味着乘累加的中间结果若超过 16 位就会被**截断**，存在溢出风险，是一个简化实现。

---

## 5. 综合实践：推演一次「加载权重 → 乘加 → 直通」的小阵列运行

> 这个任务把 4.1 和 4.2 串起来：你将**手工推演**一个 \(2 \times 2\) 阵列的三阶段运行，并写一个最小 testbench 骨架去验证你的推演。

**任务背景**：把 `ARRAY_SIZE` 设为 2，`DATA_WIDTH` 保持 16。考察最左上角的 \(\text{PE}(0,0)\)。

**第 1 步——加载权重（opcode=0x1）**：从 `west_in[0]` 送入权重 \(w=3\)。由于 \(\text{PE}(0,0)\) 的 `j==0`，它的 `west_in` 直接取外部 `west_in[0]`，所以一拍后 `weight_reg = 3`。

**第 2 步——乘累加（opcode=0x2，连续 3 拍）**：从 `north_in[0]` 依次送入激活 \(2, 4, 5\)。\(\text{PE}(0,0)\) 的 `i==0`，其 `north_in` 直接取外部 `north_in[0]`。三拍后：

\[
\text{accumulator} = 0 + 2\times3 + 4\times3 + 5\times3 = 33
\]

**第 3 步——直通（opcode=0x3）**：这一拍 \(\text{PE}(0,0)\) 把 `north_in` 转给 `south_out`、`west_in` 转给 `east_out`，即数据继续流向 \(\text{PE}(1,0)\) 和 \(\text{PE}(0,1)\)。

**第 4 步——写出最小 testbench 骨架（示例代码）**：

```verilog
// 示例代码：pe_array 的最小 testbench 骨架（ARRAY_SIZE=2）
`timescale 1ns/1ps
module tb_pe_array;
    reg clk = 0, rst_n = 0, start = 0;
    reg [3:0] opcode;
    wire done;
    reg  [15:0] north_in [0:1];
    wire [15:0] south_out [0:1];
    reg  [15:0] west_in  [0:1];
    wire [15:0] east_out [0:1];

    PE_Array #(.DATA_WIDTH(16), .ARRAY_SIZE(2)) dut (
        .clk(clk), .rst_n(rst_n), .opcode(opcode), .start(start), .done(done),
        .north_in(north_in), .west_in(west_in),
        .south_out(south_out), .east_out(east_out)
    );

    always #5 clk = ~clk;          // 100MHz 时钟

    initial begin
        opcode = 4'h0;
        #12 rst_n = 1;             // 释放复位
        // 阶段1：加载权重
        west_in[0]=16'd3; opcode=4'h1; @(posedge clk);
        // 阶段2：乘累加 2,4,5
        opcode=4'h2; north_in[0]=16'd2; @(posedge clk);
        north_in[0]=16'd4;        @(posedge clk);
        north_in[0]=16'd5;        @(posedge clk);
        // 观察 dut 内 PE(0,0) 的 accumulator，预期 33
        $finish;
    end
endmodule
```

**预期结果**：仿真结束时，\(\text{PE}(0,0)\) 的 `accumulator` 应为 33。由于第 40 行 `east_out` 越界问题（4.1.3），最右列的东向输出可能不正确，请重点观察 `south_out` 与各 PE 的 `accumulator`。

**运行结果**：仓库未提供仿真脚本与工具链，上述数值 **待本地验证**。建议你用 Icarus Verilog / Verilator 等开源仿真器自行跑一遍，并对照波形确认。

## 6. 本讲小结

- **脉动阵列**把大量相同 PE 铺成 \(N \times N\) 网格，让激活纵向流、权重横向流，每个 PE 在格子里就地做乘加——这是 NPU 高能效的核心结构。
- [`PE_Array`](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L1-L42) 只做「铺格子 + 连线」：用 `generate-for` 例化 PE，用三目运算符 `i==0 ? ... : pe_data_v[i-1][j]` 区分边界 PE 与内部 PE 的输入来源。
- [`Processing_Element`](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L44-L86) 靠 4 位 `opcode` 分派三种动作：`0x1` 装权重到 `weight_reg`、`0x2` 把 `north_in*weight_reg` 累加进 `accumulator`、`0x3` 把数据北→南、西→东直通转发。
- 只有 `0x3` 直通模式会推进数据；`0x1`/`0x2` 期间阵列的「数据泵」暂停——这决定了上层调度阵列的节奏。
- 本讲如实标注了 **三处待确认**：第 40 行 `east_out` **下标越界**；`start` 端口**未被使用**；所有 PE 的 `done` **多驱动同一根线**。另指出 `0x2` 的乘积被**截断到 16 位**，是简化实现。

## 7. 下一步学习建议

- **横向延伸（同单元）**：脉动阵列擅长「矩阵乘」，而卷积可以改写成矩阵乘。下一讲 [u2-l2 卷积加速引擎 Conv Engine](./u2-l2-conv-engine.md) 会讲 `conv_engine.v` 如何用滑动窗口把 3×3 卷积喂给乘累加电路，与本讲的 MAC 紧密相关。
- **精度扩展（同单元）**：本讲 PE 的乘法是定点的、且结果被截断。想知道如何在一个 PE 里同时支持 INT8/FP16/BFLOAT16/INT16，请读 [u2-l3 自适应多精度计算单元 Adaptive_PE](./u2-l3-adaptive-pe.md)。
- **回到系统**：想看 `PE_Array` 在整颗 SoC 里处于什么位置、数据从哪进来、结果往哪写回，可重读 [u1-l3 顶层 SoC 架构](./u1-l3-soc-top.md)，并在学完 u2-l2 后跳到 [u4-l3 全系统数据通路与集成](./u4-l3-system-integration.md)。
- **动手建议**：本讲标注的三处「待确认」非常适合作为练手题——尝试在不破坏现有语义的前提下，给出你认为正确的 `east_out` 写法、为 `start` 设计一个门控用途、或把 `done` 改成归并树，并用综合工具检查是否消除了多驱动告警（结果待本地验证）。
