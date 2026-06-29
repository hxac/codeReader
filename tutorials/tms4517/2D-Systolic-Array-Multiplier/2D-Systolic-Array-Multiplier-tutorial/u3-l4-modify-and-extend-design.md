# 进阶实践：修改与扩展设计

## 1. 本讲目标

前面十二讲，我们把这个二维脉动阵列「读」透了：从 PE 的乘加、到阵列互联、到顶层时序、到 Verilator 测试台、再到 Quartus 综合的资源利用。本讲是整个学习手册的**收口篇**——不再「读」设计，而是「**改**」设计。目标有三个：

1. **安全改规模**：能够精确列出「把阵列规模 \(N\) 从 4 改成任意合法值」需要同步修改的全部位置，并说清楚哪些量会自动派生、哪些是**绝不能动**的陷阱。
2. **改运算语义**：理解把 PE 里「无符号 8 位乘加」改造成「**有符号** 8 位乘加」需要改动哪些地方（端口符号性、位宽、符号扩展），以及测试台 C++ 侧对应的符号扩展陷阱。
3. **看懂扩展方向**：了解 README「Further Work」里「对接 SIMD 处理器」的设想，能画出把当前扁平宽端口包成窄接口的 wrapper 草图，体会从「仿真模型」走向「可部署 IP」的工程跨度。

学完后，你应当具备在这个参数化框架上动手二次开发的信心：改规模不漏改、改符号不踩坑、扩接口有思路。

## 2. 前置知识

本讲会把前序知识「用」起来而非重复讲。进入正文前，确认你熟悉下面几个概念：

- **有符号数与无符号数（signed / unsigned）**：`logic [7:0]` 在 SystemVerilog 里默认是**无符号**的，可表示 \(0\sim255\)。`logic signed [7:0]` 才是有符号，用**二进制补码（two's complement）**表示 \(-128\sim+127\)。同一个 8 位比特串 `8'b1111_1111`，无符号读作 255，有符号读作 -1——**比特没变，解释方式变了**。

- **二进制补码的一个关键性质**：只要位宽足够、不溢出，**补码加法的比特结果与无符号加法完全相同**。也就是说 `a + b` 这串比特，无论 `a`、`b` 声明成 signed 还是 unsigned，算出来的位模式是一样的（前提是不超出位宽）。这个性质后面解释「为什么累加器不用改位宽」时会用到。但**乘法不一样**：有符号乘和无符号乘的比特结果不同，必须显式声明。

- **符号扩展（sign extension）**：把一个窄的有符号数展宽时，要用它的**符号位**（最高位）填充高位。例如 `8'sb1111_1111`（-1）展宽到 16 位是 `16'b1111_1111_1111_1111`（仍是 -1），而不是零填充。零填充会把它错变成 +255。

- **INT8 与神经网络**：真实神经网络量化（如 Google TPU）普遍用 **INT8 = 有符号 8 位**（\(-128\sim+127\)）表示权重和激活。本项目为了简单用了无符号 8 位，所以「改成有符号」不只是练习，而是让设计更贴近真实 AI 加速器的数据语义。

- **SIMD 处理器**：单指令多数据（Single Instruction Multiple Data）处理器，一条指令同时处理多个数据元素。README 设想的「自定义 SIMD 处理器」是指一个能从存储器取矩阵、驱动阵列、再把结果写回存储器的小控制器。

如果对 PE 的乘加通路（`mult`/`mac_q`/`mac_d`）或顶层的参数化派生（`MULT_CYCLES`/`PAD`）已经模糊，建议先回看 [u2-l1](u2-l1-processing-element-mac.md) 与 [u3-l2](u3-l2-parameterization-and-sv-style.md)。

## 3. 本讲源码地图

本讲涉及的文件分为「要改的 RTL/TB」和「提供改造依据的文档」两类。

| 文件 | 角色 |
|---|---|
| `rtl/topSystolicArray.sv` | 顶层。第 4 行 `parameter ... N = 4` 是改规模的**唯一 RTL 改动点**；其中的 `localparam` 派生链演示了「自动适配」。 |
| `rtl/pe.sv` | 处理单元。第 12-13 行 `i_a`/`i_b` 与第 22-36 行 MAC 通路是有符号改造的**核心现场**。 |
| `rtl/systolicArray.sv` | 阵列互联。PE 端口符号性变化后，互联网的声明需要同步理解（比特不变，但端口匹配要一致）。 |
| `tb/tb_topSystolicArray.cpp` | C++ 测试台。第 16 行宏 `N` 是改规模的**唯一 TB 改动点**；第 92-99、131-140、156-181 行是有符号改造时 C++ 侧的符号扩展陷阱现场。 |
| `README.md` | 第 23-27 行给出改 `N` 的官方说明；第 56-60 行解释 8 位/32 位的取舍；第 157-163 行是 SIMD 扩展设想的出处。 |

## 4. 核心概念与源码讲解

本讲按「改规模 → 改符号 → 扩接口」三个最小模块展开，难度与改动量依次递增。

### 4.1 安全修改阵列规模 N 的完整改动点

#### 4.1.1 概念说明

本项目最大的工程亮点是**高度参数化**：整个阵列的规模由单一参数 \(N\) 驱动。这意味着「把 4×4 改成 8×8」理论上不该是一场地毯式搜索，而应该是**两处定点修改 + 一套自动派生**。

但「参数化」不等于「随便改」。改 \(N\) 时有两类位置必须区分清楚：

1. **必须手动改的真源头**：RTL 参数与 TB 宏各一处，共两行。
2. **绝不能误改的「假依赖」**：TB 里有些数字看起来与 \(N\) 有关，其实是**字节/字比值**，由元素位宽决定，与 \(N\) 无关——改了反而会出错。

此外还有一类**自动派生量**（`MULT_CYCLES`、`PAD`、各 packed 数组宽度），它们由 `localparam` 和参数化端口声明自动算出，不需要也不能手动改。把它们理清，就是本模块的全部内容。

#### 4.1.2 核心流程

把规模从 \(N=4\) 改成 \(N=K\) 的完整流程：

1. **改 RTL**：打开 `rtl/topSystolicArray.sv`，把第 4 行 `parameter int unsigned N = 4` 的 `4` 改成 `K`。
2. **改 TB**：打开 `tb/tb_topSystolicArray.cpp`，把第 16 行 `#define N 4` 的 `4` 改成 `K`。
3. **重新仿真**：`cd tb && make all`。

仅此而已。下面这些**都不用手动改**，它们会自动跟随 \(N\)：

- 阵列规模：`systolicArray` 经 `#(.N(N))` 接收 \(N\)，内部 `generate` 自动铺出 \(K\times K\) 个 PE。
- 乘法周期：`MULT_CYCLES = 3*N-2`（\(N=K\) 时为 \(3K-2\)）。
- 移位寄存器宽度：`row_q`/`col_q` 声明为 `[N-1:0][(2*N)-2:0][7:0]`，自动变宽。
- 补零宽度：`PAD = 8*(N-1)`、`APPEND_ZERO` 自动变长。
- TB 字数组个数：`numArrays = (N²+3)/4`，自动算出承载 \(K\times K\) 个 8 位元素所需的 32 位字数。
- TB 触发节拍：`assertValidInput = 3*N+3`，自动拉开新一轮 `validInput` 的间距，始终比 `MULT_CYCLES` 多留 5 拍余量。

而下面这个**绝对不能改**（最常见的误改）：

- TB 第 134 行 `for (int j = 0; j < 4; j++)` 里的 `4`。它是「一个 32 位字装 4 个字节」的**字节比值**，由元素位宽 8 决定，与 \(N\) 无关。[u3-l2](u3-l2-parameterization-and-sv-style.md) 已专门警告过这一点。

#### 4.1.3 源码精读

先看 README 对改规模流程的官方说明（本模块结论的直接出处）：

[README.md:23-27](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L23-L27)

> By default the RTL and TB are configured to a matrix size of 4x4.
>
> To modify the default matrix size: `cd rtl`, open `topSystolicArray.sv` and modify the paramater `N`. And, `cd tb`, open `tb_topSystolicArray.sv` and modify the macro `N`.

> 小提示：README 这里把测试台文件名写成了 `tb_topSystolicArray.sv`，但实际文件是 `tb/tb_topSystolicArray.cpp`（C++ 测试台，见 [u3-l1](u3-l1-verilator-cpp-testbench.md)）。这是文档里的一处小笔误，改的时候认准 `.cpp` 文件即可。

这两处「真源头」在源码里分别是：

RTL 参数（改这一行的 `4`）：

[rtl/topSystolicArray.sv:4](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L4)

```systemverilog
#(parameter int unsigned N = 4)                             /* Modify this */
```

注释 `/* Modify this */` 是作者留下的明确路标。这个 `N` 随后通过实例化传给阵列：

[rtl/topSystolicArray.sv:161-163](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L161-L163)

```systemverilog
systolicArray
#(.N (N))
u_systolicArray
```

TB 宏（改这一行的 `4`）：

[tb/tb_topSystolicArray.cpp:16](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L16)

```cpp
#define N 4 // Square matrix dimension.                       /* Modify this */
```

同样有 `/* Modify this */` 路标。注意 RTL 参数与 TB 宏是**两套独立的名字空间**——一个在 SystemVerilog 里、一个在 C++ 预处理器里，必须手动保持一致，工具不会帮你校对（这是本设计唯一的「人工接缝」，[u3-l2](u3-l2-parameterization-and-sv-style.md) 已点明）。

再验证「自动派生」确实生效。`MULT_CYCLES` 是 `localparam`，完全由 \(N\) 算出：

[rtl/topSystolicArray.sv:36-38](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L36-L38)

```systemverilog
localparam int unsigned MULT_CYCLES = 3*N-2;
localparam int unsigned MULT_CYCLES_W = $clog2(MULT_CYCLES+1);
```

\(N=4\) 时 `MULT_CYCLES=10`；你把 \(N\) 改成 8，它自动变成 22，计数器位宽 `MULT_CYCLES_W` 也自动用 `$clog2` 重算。补零宽度同理：

[rtl/topSystolicArray.sv:93-94](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L93-L94)

```systemverilog
localparam int unsigned PAD = 8*(N-1);
localparam bit [PAD-1:0] APPEND_ZERO = PAD'(0);
```

最后看 TB 里那个「假依赖」陷阱——`4` 是字节比值，不是 \(N\)：

[tb/tb_topSystolicArray.cpp:131-140](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L131-L140)

```cpp
int index = 0;
for (int i = 0; i < numArrays; i++) {
  for (int j = 0; j < 4; j++) {
    dut->i_a[i] |= (static_cast<uint32_t>(singleArrayA[index] << (8 * j)));
    dut->i_b[i] |= (static_cast<uint32_t>(singleArrayB[index] << (8 * j)));
    index++;
  }
}
```

这里的 `4` 来自注释（第 127-129 行）所说「Verilator 把输入端口表示成 32 位数组」，一个 32 位字 = 4 个 8 位字节。把 \(N\) 从 4 改成 8，**外层 `numArrays` 会自动变**（它由 \(N\) 算出），**内层 `4` 不变**。若误把 `4` 也改成 \(N\)，打包逻辑立刻错乱。

#### 4.1.4 代码实践

1. **实践目标**：把阵列规模从 \(N=4\) 改成 \(N=6\)，确认只需两行改动即可跑通，并观察接口位宽与 `MULT_CYCLES` 的自动变化。
2. **操作步骤**：
   - 改 `rtl/topSystolicArray.sv` 第 4 行：`N = 4` → `N = 6`。
   - 改 `tb/tb_topSystolicArray.cpp` 第 16 行：`#define N 4` → `#define N 6`。
   - 执行 `cd tb && make all`。
3. **需要观察的现象**：仿真仍然打印 Matrix A / B / Expected result Matrix，且不报 `ERROR: output matrix received is incorrect.`；终端里 `numArrays` 自动变成 \((36+3)/4 = 9\) 个 32 位字（可在第 103 行加一条 `std::cout` 打印确认）。
4. **预期结果**：6×6 矩阵乘法通过校验，证明两行改动 + 自动派生链工作正常。
5. **待本地验证**：本环境未安装 Verilator，且本讲禁止修改源码，故未实际运行；请在本地副本上验证。可顺便对照 [u3-l3](u3-l3-quartus-fpga-synthesis.md) 的公式预测 \(N=6\) 时的 DSP 数（\(=36\)）与引脚数（\(=48\cdot36+4=1732\)）。

> 注意：本实践涉及修改源码，请在**副本**上操作，不要直接改仓库原始 RTL/TB。

#### 4.1.5 小练习与答案

**练习 1**：把 \(N\) 从 4 改成 6 后，`MULT_CYCLES`、`MULT_CYCLES_W`、`PAD` 各变成多少？

**参考答案**：`MULT_CYCLES = 3*6-2 = 16`；`MULT_CYCLES_W = $clog2(16+1) = $clog2(17) = 5`（因为 \(2^4=16<17\le 2^5=32\)）；`PAD = 8*(6-1) = 40`。三者全部由 `localparam` 自动算出，无需手改。

**练习 2**：有人为了「让测试台也支持 6×6」，把 `tb_topSystolicArray.cpp` 第 134 行的 `j < 4` 改成了 `j < 6`。这会导致什么后果？

**参考答案**：会破坏输入打包。内层循环的 `4` 是「一个 32 位字装 4 个字节」的字节比值，与 \(N\) 无关。改成 `j < 6` 后，每个 32 位字会被塞进 6 个元素（第 5、6 个元素的移位量 `8*4=32`、`8*5=40` 会超出 32 位字被截断/溢出），元素错位、`index` 越界，矩阵乘法结果必然错误、仿真报错退出。

---

### 4.2 把无符号 MAC 改造为有符号乘法

#### 4.2.1 概念说明

当前 PE 做的是**无符号** 8 位乘加：`i_a`、`i_b` 都是 `logic [7:0]`（无符号），乘出来的 `mult` 也是无符号的。这让输入只能表示 \(0\sim255\) 的非负数。

但真实 AI 加速器（包括 README 第 56-58 行引用的 Google TPU）普遍用 **INT8 有符号** 数据。要让这个阵列真正「像 TPU」，就得把 MAC 改成有符号。这个改造本身很小，但它**精准地暴露了三个层次的细节**，是非常好的综合练习：

1. **乘法的符号性**：必须让两个操作数都声明为 signed，乘法才会按补码进行。
2. **符号扩展**：窄的有符号乘积要正确展宽到 32 位累加器。
3. **C++ 侧的符号陷阱**：测试台在把负数打包进 32 位字时，会遭遇 C++ 整型提升带来的符号扩展，必须显式屏蔽。

#### 4.2.2 核心流程

改造分 RTL 侧和 TB 侧两部分。

**RTL 侧（`pe.sv`）**：核心思想是「让乘法的两个操作数都有符号，并把累加通路统一声明为 signed」。

- 端口 `i_a`、`i_b`：`logic [7:0]` → `logic signed [7:0]`。
- 乘积 `mult`、累加 `mac_d`/`mac_q`、输出 `o_y`：`logic [31:0]` → `logic signed [31:0]`。
- （为一致性）透传寄存器 `a_q`/`b_q` 与输出 `o_a`/`o_b`：可一并声明为 `logic signed [7:0]`。

改完后，`mult = i_a*i_b` 在两个 signed 操作数下按补码相乘，结果自动**符号扩展**到 32 位。位宽验证：signed \([-128,127]\times[-128,127]\) 的单次乘积落在 \([-16256, +16384]\)，累加 \(N\) 次（\(N=4\) 时最大 \(4\times16384=65536\)）远不到 32 位有符号上限 \(2^{31}-1\)，所以**累加器位宽 32 位无需改变**。

**位宽不变的两个理论依据**：

1. **补码加法 == 无符号加法（位级）**：只要不溢出，`mac_q + mult` 的比特结果在 signed/unsigned 声明下完全一致。所以累加这一步即使保持 unsigned 声明，位模式也是对的。
2. **但乘法必须改**：有符号乘与无符号乘的位级结果不同，这是改造的**唯一硬性必然**——必须让乘法两端都 signed。

> 因此最小可行改动是「端口 `i_a`/`i_b` 改 signed + `mult` 改 signed（保证符号扩展到 32 位）」。把 `mac`/`o_y` 一并改 signed 是为了**意图清晰、避免日后比较/打印时被误读为无符号**，属于工程健壮性而非正确性必需。

**TB 侧（`tb_topSystolicArray.cpp`）**：要让测试台产生负数并正确比对其结果。

- 数据类型：`matrixA`/`matrixB` 由 `uint8_t` 改 `int8_t`；`matrixC` 由 `uint32_t` 改 `int32_t`。
- 随机生成：`rand() % maxValue`（\(0\sim255\)）后用 `static_cast<int8_t>(...)` 重解释，自然覆盖 \(-128\sim+127\)。
- **打包陷阱（关键）**：原代码 `singleArrayA[index] << (8*j)` 在 `int8_t` 下会**符号扩展**（如 -1 先提升为 `int` 的 `0xFFFFFFFF`，再左移污染整个字），必须先 `static_cast<uint8_t>(...)` 剥成裸比特再移位。
- 比对：`o_c` 在 Verilator 侧是 `uint32_t`，承载的恰是有符号结果的补码位模式；与 `int32_t` 期望比较时，可显式 `static_cast<int32_t>(dut->o_c[...])` 让意图清晰。

#### 4.2.3 源码精读

先看 README 对 8 位数据语义的说明——它没写「无符号」，但提到 TPU 与神经网络，正是改成有符号的动机：

[README.md:56-60](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L56-L60)

> The elements of the input matrices are 8 bit integers. This was chosen out of simplicity and provides an appropriate level of accuracy for neural network calculations, as described in Google's TPU blog post. Furthermore, the elements of the output matrix are set to 32 bits. …

「8 bit integers」未限定符号，实现上是无符号；而 TPU 的 INT8 是有符号。改成 signed，就是让数据语义对齐 TPU。

再看改造的核心现场——PE 的端口与 MAC 通路：

[rtl/pe.sv:6-18](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L6-L18)

```systemverilog
module pe
  ( input  var logic        i_clk
  , input  var logic        i_arst
  , input  var logic        i_doProcess
  , input  var logic [7:0]  i_a        // ← 改 signed
  , input  var logic [7:0]  i_b        // ← 改 signed
  , output var logic [7:0]  o_a        // ← 可改 signed（比特不变）
  , output var logic [7:0]  o_b        // ← 可改 signed（比特不变）
  , output var logic [31:0] o_y        // ← 改 signed
  );
```

把 `i_a`/`i_b`/`o_y` 改成 `signed` 后，下游乘法即按补码进行。接着是 MAC 通路本身：

[rtl/pe.sv:22-39](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L22-L39)

```systemverilog
logic [31:0] mult;            // ← 改 logic signed [31:0]

always_comb
  mult = i_a*i_b;             // 两端 signed → 补码乘，符号扩展到 32 位

logic [31:0] mac_d, mac_q;    // ← 改 logic signed [31:0]

always_ff @(posedge i_clk, posedge i_arst)
  if (i_arst)
    mac_q <= '0;
  else
    mac_q <= mac_d;

always_comb
  mac_d = (i_doProcess) ? mac_q + mult : '0;

always_comb
  o_y = mac_q;
```

要点逐条对应：

- `mult = i_a*i_b`：当 `i_a`、`i_b` 均 signed 时，SystemVerilog 按「上下文宽度取 max(操作数宽, 左值宽) = 32」先对两操作数**符号扩展**到 32 位再相乘，取低 32 位赋给 signed `mult`。这正是我们想要的、正确的 32 位补码乘积。
- `mac_q + mult`：两 signed 32 位相加，补码加法位级正确，累加值在 \([-16256N, +16384N]\) 范围内（\(N\le16\) 时远不溢出）。
- `'0` 清零：对所有位填 0，对 signed 而言就是 `+0`，语义无误。
- 位宽 32 位**保持不变**：单次乘积只需有符号 16 位，累加 \(N\) 次也远不到 32 位上限。

互联网与阵列层面：`o_a`/`o_b` 在 PE 间透传的是 8 位比特串。声明成 signed 与否**不影响比特内容**（signed 只改变算术解释，不改变寄存器存的内容），所以 `systolicArray.sv` 里的 `rowInterConnect`/`colInterConnect` 比特照常流动。为保持端口符号性一致、避免综合器告警，建议把 `systolicArray.sv` 的 `i_row`/`i_col`/`o_c` 与 `topSystolicArray.sv` 的 `i_a`/`i_b`/`o_c` 也一并声明 signed——这是一致性整理，不是正确性必需。

[rtl/systolicArray.sv:26-30](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L26-L30)

```systemverilog
, input  var logic [N-1:0][(2*N)-2:0][7:0] i_row   // ← 可整体加 signed
, input  var logic [N-1:0][(2*N)-2:0][7:0] i_col   // ← 可整体加 signed
, output var logic [N-1:0][N-1:0][31:0]    o_c     // ← 可整体加 signed
```

最后看 TB 侧的符号扩展陷阱。先看当前的无符号随机生成与类型声明：

[tb/tb_topSystolicArray.cpp:27-29](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L27-L29)

```cpp
uint8_t matrixA[N][N];    // ← 改 int8_t
uint8_t matrixB[N][N];    // ← 改 int8_t
uint32_t matrixC[N][N];   // ← 改 int32_t
```

[tb/tb_topSystolicArray.cpp:92-99](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L92-L99)

```cpp
void initializeInputMatrices() {
  for (int i = 0; i < N; i++) {
    for (int j = 0; j < N; j++) {
      matrixA[i][j] = rand() % maxValue;   // ← 改 static_cast<int8_t>(rand() % maxValue)
      matrixB[i][j] = rand() % maxValue;
    }
  }
}
```

`static_cast<int8_t>(rand() % 256)` 会把 \(128\sim255\) 重解释成 \(-128\sim-1\)，覆盖完整 INT8 范围。

**最关键的陷阱**在打包处。当前（无符号）写法：

[tb/tb_topSystolicArray.cpp:133-139](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L133-L139)

```cpp
for (int i = 0; i < numArrays; i++) {
  for (int j = 0; j < 4; j++) {
    dut->i_a[i] |= (static_cast<uint32_t>(singleArrayA[index] << (8 * j)));
    dut->i_b[i] |= (static_cast<uint32_t>(singleArrayB[index] << (8 * j)));
    index++;
  }
}
```

当 `singleArrayA` 改成 `std::vector<int8_t>` 后，`singleArrayA[index] << (8*j)` 会先把 `int8_t`（如 -1，比特 `0xFF`）**整型提升**为 `int` 并**符号扩展**成 `0xFFFFFFFF`，再左移 8 位得到 `0xFFFFFF00`——这会把高 24 位全置 1，污染同一字里的其它元素。正确写法是**先剥成无符号裸比特再移位**（示例代码）：

```cpp
// 示例代码：先转 uint8_t 截取低 8 位裸比特，避免整型提升带来的符号扩展
dut->i_a[i] |= (static_cast<uint32_t>(static_cast<uint8_t>(singleArrayA[index])) << (8 * j));
dut->i_b[i] |= (static_cast<uint32_t>(static_cast<uint8_t>(singleArrayB[index])) << (8 * j));
```

这样 `-1` 先变成 `uint8_t` 的 `0xFF`（值 255），再提升为 `int` 的 `255`，左移后只占用预期的 8 位。

期望结果计算（`calculateResultMatrix`）天然正确：`matrixA`/`matrixB` 变成 `int8_t` 后，`matrixA[i][k] * matrixB[k][j]` 在 C++ 里提升为 `int` 做**有符号乘**，累加进 `int32_t matrixC`，与 RTL 的有符号 MAC 语义一致：

[tb/tb_topSystolicArray.cpp:144-154](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L144-L154)

```cpp
void calculateResultMatrix() {
  for (int i = 0; i < N; i++) {
    for (int j = 0; j < N; j++) {
      matrixC[i][j] = 0;
      for (int k = 0; k < N; ++k) {
        matrixC[i][j] += matrixA[i][k] * matrixB[k][j];   // int8_t*int8_t → int，有符号
      }
    }
  }
}
```

最后是比对。`dut->o_c[...]` 在 Verilator 里是 `uint32_t`，承载的恰是有符号结果的补码位模式。原比较 `dut->o_c[(N*i)+j] != matrixC[i][j]` 在把 `matrixC` 改成 `int32_t` 后，由于 C++ 会把 `int32_t` 转成 `uint32_t` 再比，两边位模式一致时仍判等——**巧合上仍正确**。但为意图清晰，建议显式转型：

[tb/tb_topSystolicArray.cpp:164-169](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L164-L169)

```cpp
for (int i = 0; i < N; i++) {
  for (int j = 0; j < N; j++) {
    if (dut->o_c[(N * i) + j] != matrixC[i][j]) {   // ← 建议改 static_cast<int32_t>(dut->o_c[...]) != matrixC[i][j]
      incorrect = true;
    }
  }
}
```

#### 4.2.4 代码实践

1. **实践目标**：把 PE 改造成有符号 8 位乘加，并让测试台生成含负数的输入，跑通后验证有符号矩阵乘法结果正确。这是本讲的主实践。
2. **操作步骤**（在副本上进行）：
   - **RTL**：`rtl/pe.sv` 把 `i_a`、`i_b`（第 12-13 行）、`mult`（第 22 行）、`mac_d`/`mac_q`（第 27 行）、`o_y`（第 17 行）以及 `a_q`/`b_q`（第 45 行）、`o_a`/`o_b`（第 15-16 行）统一加 `signed`。建议同步给 `systolicArray.sv` 与 `topSystolicArray.sv` 的相关端口加 `signed` 以保持一致。
   - **TB**：`tb/tb_topSystolicArray.cpp` 把 `matrixA`/`matrixB` 改 `int8_t`、`matrixC` 改 `int32_t`；`initializeInputMatrices` 用 `static_cast<int8_t>(rand() % maxValue)`；打包处按上面「示例代码」先 `static_cast<uint8_t>` 再移位；比对处建议显式 `static_cast<int32_t>`。
   - 运行 `cd tb && make all`。
3. **需要观察的现象**：终端打印的 Matrix A / B 里出现 `ff`、`fe`、`80` 这类补码值（即负数）；Expected result Matrix 里出现 32 位补码负数（如 `ffffff80` = -128）；仿真不报 `ERROR: output matrix received is incorrect.`。
4. **预期结果**：含负数的有符号矩阵乘法逐元素通过校验，证明 RTL 与 TB 的符号语义一致。
5. **待本地验证**：本环境未安装 Verilator，且本讲禁止修改源码，故未实际运行。一个可手算验证的小例子：\(A=\begin{bmatrix}-1 & 0\\0 & 0\end{bmatrix}\)、\(B=\begin{bmatrix}2 & 0\\0 & 0\end{bmatrix}\)（需把 \(N\) 设为 2），期望 \(C=A\times B=\begin{bmatrix}-2 & 0\\0 & 0\end{bmatrix}\)，即 `o_c[0]` 应为 `0xFFFFFFFE`。

> 注意：本实践涉及修改源码，请在**副本**上操作，不要改动仓库原始 RTL/TB。

#### 4.2.5 小练习与答案

**练习 1**：如果只把 `i_a` 改成 `signed`，而 `i_b` 仍是 `unsigned`，`mult = i_a*i_b` 会怎样？

**参考答案**：会变成**无符号乘**。SystemVerilog 规定：只要有一个操作数是无符号，整个乘法就按无符号处理。于是 `i_a` 的负数会被当成大正数参与相乘，结果错误。这正是为什么必须「**两个**操作数都声明 signed」——这是改造的硬性必然。

**练习 2**：为什么把 MAC 改成有符号后，累加器位宽 32 位**不需要**加宽？

**参考答案**：单次有符号 8×8 乘积最大 \(|{-128}\times{-128}|=16384\)，约需有符号 16 位；累加 \(N\) 次（文档范围 \(N\le16\)）最大约 \(16\times16384\approx2.6\times10^5\)，远小于 32 位有符号上限 \(2^{31}-1\approx2.1\times10^9\)。再加上补码加法位级与无符号加法等价，所以 32 位足够，无需加宽。

**练习 3**：TB 打包处为什么必须先 `static_cast<uint8_t>` 再左移，而不能直接 `int8_t << 8`？

**参考答案**：C++ 中 `int8_t` 在参与 `<<` 前会先**整型提升**为 `int`，并**符号扩展**。负数 `-1`（`0xFF`）会变成 `int` 的 `0xFFFFFFFF`，左移 8 位得 `0xFFFFFF00`，污染同一个 32 位字里的其它元素位置。先 `static_cast<uint8_t>` 把它截成裸比特 `0xFF`（值 255），提升后是正数 `255`，左移只占用预期的 8 位，从而正确打包。

---

### 4.3 对接 SIMD 处理器接口的进一步工作

#### 4.3.1 概念说明

前两个模块都是在「现有扁平接口」内部动手。本模块往外看一步：**这个宽得离谱的端口，怎么连到真实系统上？**

回顾 [u3-l3](u3-l3-quartus-fpga-synthesis.md) 的资源结论：顶层端口位宽是 \(48N^{2}+4\)，\(N=4\) 就要 772 个引脚，远超任何 FPGA 的用户 I/O。也就是说，`i_a`/`i_b`/`o_c` 这套「一次喂进整张矩阵」的接口，在仿真里很爽，**在芯片上根本接不出去**。

README 的「Further Work」给出了作者设想的方向：做一个**自定义 SIMD 处理器**当「前端」，从存储器里把矩阵一行行/一字字地取出来，**串行灌进**阵列，等 `o_validResult` 一亮，再把结果**读回**存储器。这本质上是用一个「窄接口 + 控制器」去包裹「宽接口 + 计算核」，把引脚瓶颈转化成「时间换引脚」。

#### 4.3.2 核心流程

SIMD 处理器与脉动阵列协作的时序骨架（设计草图，非仓库已有代码）：

1. **取指/译码**：处理器执行一条自定义「矩阵乘」指令，给出矩阵 A、B 在存储器里的基地址。
2. **加载 A、B（串行化）**：处理器通过窄存储器接口逐字读出 A、B 的元素，写入 wrapper 内部的**输入寄存器组**（寄存器组宽度对齐 `i_a`/`i_b`，把串行数据**并行展开**成阵列要的宽端口）。
3. **启动计算**：寄存器组就绪后，wrapper 拉高 `i_validInput` 一拍。
4. **等待完成**：处理器等待 \(3N-2\) 拍（或轮询/中断等 `o_validResult` 脉冲）。
5. **读回 C（解串行）**：`o_validResult` 亮那拍，wrapper 把宽 `o_c` 锁进**输出寄存器组**，处理器再逐字读回存储器。

关键设计点：

- **握手契合**：当前顶层是**单向触发**握手——`i_validInput` 启动、`o_validResult` 单拍脉冲报告完成，**没有 ready、不能反压**（见 [u1-l3](u1-l3-top-interface-and-dataflow.md)）。所以 wrapper 只要保证「加载完成才拉 validInput、validResult 那拍锁存 o_c」即可，无需处理反压。
- **窄接口选型**：wrapper 对外可以是简单的存储器映射（load/store 寄存器），或一条 AXI-Lite 从口；对内仍是宽端口。这样芯片对外引脚从 \(48N^{2}+4\) 降到几十根（地址 + 数据 + 控制）。
- **状态机**：wrapper 内部一个小 FSM（IDLE → LOAD_A → LOAD_B → START → WAIT → CAPTURE → READBACK → IDLE）就够。

#### 4.3.3 源码精读

先看 README 对 SIMD 设想的原文（本模块的唯一文字依据）：

[README.md:157-163](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L157-L163)

> **Interface module to a custom SIMD processor**
>
> It would be interesting to create a SIMD processor with custom instructions that can fetch the input matrices from memory, and drive the input ports of the top level module. The processor would then wait for a few clock cycles for the result to be calculated. It would then read the output ports of the top level module and write the result back to memory.

这段话给出了三步：取矩阵→驱动端口→等几拍→读回写内存。它对应的就是上面流程里的 2-5 步。

再看「为什么需要 wrapper」的根源——顶层那组宽端口（这是 SIMD 模块要包裹的对象）：

[rtl/topSystolicArray.sv:5-16](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L5-L16)

```systemverilog
( input  var logic                      i_clk
, input  var logic                      i_arst
, input  var logic [N-1:0][N-1:0][7:0]  i_a     // 宽端口：N*N*8 位
, input  var logic [N-1:0][N-1:0][7:0]  i_b     // 宽端口：N*N*8 位
, input  var logic                      i_validInput
, output var logic [N-1:0][N-1:0][31:0] o_c     // 宽端口：N*N*32 位
, output var logic                      o_validResult
);
```

两组 \(N\times N\times8\) 的输入端口加上一组 \(N\times N\times32\) 的输出端口，合起来正是 \(48N^{2}+4\) 引脚的来源。SIMD wrapper 要做的，就是在这些宽端口与一个窄存储器接口之间做「并/串转换 + 握手控制」。

最后看握手时序的依据——`o_validResult` 是单拍脉冲（见 [u2-l4](u2-l4-control-counter-clock-gating.md)），wrapper 必须在**那一拍**锁存 `o_c`：

[rtl/topSystolicArray.sv:54-67](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L54-L67)

```systemverilog
//o_validResult is asserted to signal the end of the matrix multiplication
// process.
logic validResult_q;

always_ff @(posedge i_clk, posedge i_arst)
  if (i_arst)
    validResult_q <= '0;
  else if (counter_q == MULT_CYCLES_W'(MULT_CYCLES))
    validResult_q <= '1;
  else
    validResult_q <= '0;

always_comb
  o_validResult = validResult_q;
```

`validResult_q` 仅在 `counter_q == MULT_CYCLES` 那一拍为 1，下一拍因计数器自增而回 0——所以 wrapper 的输出寄存器组要用 `o_validResult` 当使能，**仅这一拍**把 `o_c` 锁存下来，否则下一拍结果就没了。

> 说明：本模块描述的 SIMD wrapper 是依据 README 设想给出的**设计草图**，仓库中尚不存在对应源码（属「Further Work」）。下面给出一段示意性 RTL 骨架，标注为示例代码。

```systemverilog
// 示例代码：SIMD wrapper 骨架（仓库中尚不存在，仅为设计示意）
module simd_wrapper #(parameter int unsigned N = 4) (
    input  var logic i_clk, i_arst,
    // 对外的窄接口（示意）：存储器映射的加载/读回
    input  var logic i_loadA, i_loadB, i_start,
    // ... 窄数据/地址端口省略 ...
    // 对内的宽端口：直接连到 topSystolicArray
    output var logic [N-1:0][N-1:0][7:0]  o_a, o_b,
    output var logic                      o_validInput,
    input  var logic [N-1:0][N-1:0][31:0] i_c,
    input  var logic                      i_validResult
);
  // 内部寄存器组把窄接口串行数据展开成宽端口
  logic [N-1:0][N-1:0][7:0] a_reg, b_reg;
  logic [N-1:0][N-1:0][31:0] c_capture;

  always_ff @(posedge i_clk, posedge i_arst)
    if (i_arst) begin
      o_validInput <= 1'b0;
    end else if (i_start) begin
      o_a <= a_reg; o_b <= b_reg;   // 并行展开
      o_validInput <= 1'b1;          // 单拍启动
    end else begin
      o_validInput <= 1'b0;
    end

  // 仅在 validResult 那一拍锁存结果
  always_ff @(posedge i_clk)
    if (i_validResult) c_capture <= i_c;
endmodule
```

这段骨架只演示「窄→宽展开 + 单拍启动 + 单拍锁存」三件事，省略了真正的存储器接口与 FSM，目的是让你直观看到 wrapper 如何把宽端口「包」起来。

#### 4.3.4 代码实践

1. **实践目标**：不写完整 RTL，而是用「画图 + 推算」把 SIMD wrapper 的时序与引脚收益理清。
2. **操作步骤**：
   - 画出 wrapper 的 FSM 状态图（IDLE → LOAD_A → LOAD_B → START → WAIT → CAPTURE → READBACK → IDLE），标出每个状态的转移条件（`i_loadA` 完成、`i_start`、`o_validResult` 等）。
   - 对 \(N=4\)，算出「串行加载一张 A 矩阵」需要多少拍（\(N\times N=16\) 次写），与「计算本身」\(3N-2=10\) 拍比较，判断哪一段是瓶颈。
   - 算出 wrapper 对外引脚数（假设窄接口用 8 位数据 + 4 位地址 + 4 根控制），与原宽端口 772 引脚对比。
3. **需要观察的现象**：加载阶段（16 拍）比计算阶段（10 拍）还长——说明在「窄接口」下，**数据搬运**成了新瓶颈，这正是真实 AI 加速器要靠 DMA / 宽存储器接口缓解的问题。
4. **预期结果**：你能口述「wrapper 把 772 引脚压到 ~20 根，代价是加载/读回要花 \(N^{2}\) 拍，需要并行加载或 DMA 来摊平」。
5. **待本地验证**：本实践为设计推演型，无需运行；若你已学过简单 FSM，可尝试把上面骨架补成一个最小的可仿真 wrapper（仍是副本上操作）。

#### 4.3.5 小练习与答案

**练习 1**：当前顶层握手「没有 ready 信号」这一特性，对 SIMD wrapper 的设计是帮忙还是添乱？

**参考答案**：是**帮忙（简化）**。因为没有反压，wrapper 只要「加载完就拉 `i_validInput`、`o_validResult` 亮那拍锁存 `o_c`」即可，不必处理「阵列没准备好要重传」的复杂情况。代价是 wrapper 必须自己保证两次乘法之间至少隔 \(3N-2\) 拍，不能在上一次结果还没出来时又启动新的一次。

**练习 2**：为什么 SIMD wrapper 必须在 `o_validResult` 那一拍锁存 `o_c`，不能晚一拍再读？

**参考答案**：因为 `o_validResult` 是**单拍脉冲**（`validResult_q` 仅在 `counter_q == MULT_CYCLES` 那拍为 1），而 `o_c` 的有效性也是与这个脉冲对齐的窗口。下一拍计数器自增、`doProcess` 即将关闭、MAC 将被清零，`o_c` 不再保持有效结果。所以 wrapper 必须用 `o_validResult` 当使能、当拍锁存，错过这一拍就读不到正确结果了。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「在参数化框架上做一次有意义的二次开发」的小任务：

1. **改规模**：把阵列规模从 \(N=4\) 改成 \(N=3\)（两行改动：RTL 第 4 行、TB 第 16 行），确认 `MULT_CYCLES` 自动变成 7、`numArrays` 自动变成 \((9+3)/4=3\)。
2. **改符号**：在 \(N=3\) 的基础上，按 4.2 节把 PE 改成有符号 8 位乘加，TB 改成生成含负数的 `int8_t` 输入（注意打包处的 `static_cast<uint8_t>` 陷阱）。
3. **造用例**：手算一个固定的 \(3\times3\) 含负数用例，例如
   \[
   A=\begin{bmatrix}-1&2&0\\0&-3&1\\2&0&1\end{bmatrix},\quad
   B=\begin{bmatrix}1&0&-1\\2&-2&0\\0&1&3\end{bmatrix}
   \]
   先手算期望 \(C=A\times B\)，再把这套数硬编码进 `initializeInputMatrices`（替代随机生成），运行 `cd tb && make all`。
4. **验证**：确认仿真打印的 Received matrix（即 `o_c`）与手算期望逐元素一致（注意负数显示为补码）。
5. **下结论**：写一段话说明「这次改造里，哪些是必改（端口 signed、TB 打包 cast）、哪些是自动派生（MULT_CYCLES、numArrays）、哪些是绝不能动（打包内层 `4`）」。

参考结论要点：RTL 必改 `i_a`/`i_b`/`mult` 为 signed（保证乘法为补码且符号扩展到 32 位），TB 必改类型为 `int8_t`/`int32_t` 且打包处先转 `uint8_t`；`MULT_CYCLES`、`PAD`、`numArrays`、`assertValidInput` 全部自动派生；打包内层 `4` 是字节比值绝不能改。手算期望 \(C\) 时按有符号乘加逐元素核对，例如 \(C_{00}=(-1)\cdot1+2\cdot2+0\cdot0=3\)，\(C_{02}=(-1)\cdot(-1)+2\cdot0+0\cdot3=1\)，以此类推。

## 6. 本讲小结

- **改规模只需两行**：RTL `topSystolicArray.sv` 第 4 行 `parameter N` 与 TB `tb_topSystolicArray.cpp` 第 16 行宏 `N`；`MULT_CYCLES`、`PAD`、各 packed 端口宽度、`numArrays`、`assertValidInput` 全部由 `localparam`/参数化声明**自动派生**。
- **TB 内层 `4` 是字节比值**（一个 32 位字装 4 字节），由元素位宽决定、与 \(N\) 无关，是最常见的误改陷阱，绝不能动。
- **RTL 参数与 TB 宏是两个人工接缝**，分属 SV 与 C++ 名字空间，必须手动同步，工具不校对。
- **有符号改造的最小硬性改动**是让乘法两端都声明 `signed`（`i_a`/`i_b`/`mult`），保证补码相乘并符号扩展到 32 位；累加器位宽 32 位无需加宽（补码加法位级等价于无符号加法，且不溢出）。
- **C++ 侧的最大陷阱**是 `int8_t << n` 的整型提升符号扩展，必须先 `static_cast<uint8_t>` 剥成裸比特再左移打包。
- **SIMD wrapper 是把宽端口「包」成窄接口**的方向（呼应 README Further Work），用「并/串转换 + 单拍启动 + 单拍锁存」化解 \(48N^{2}+4\) 引脚瓶颈；代价是加载/读回成为新瓶颈，需 DMA/并行加载摊平。

## 7. 下一步学习建议

本讲是学习手册的收口，接下来不再有后续讲义，建议从三个方向继续深入：

- **朝「真实 AI 加速器」深入**：对照本讲 4.2 的有符号改造，去读 Google TPU 的脉动阵列实现（README 第 41 行给了论文与博客链接），重点看真实 TPU 如何处理 INT8 量化、如何做流水线化的数据加载与双缓冲，体会「教学版」与「工业版」的差距。
- **朝「可部署 IP」深入**：把 4.3 的 SIMD wrapper 草图落地为一个可仿真的 wrapper（窄存储器映射接口 + FSM），并复看 [u3-l3](u3-l3-quartus-fpga-synthesis.md) 的引脚瓶颈结论，亲手验证「窄接口」能把引脚从 772 降到几十根；进一步可研究 AXI 总线与 DMA，理解真实 SoC 如何搬运矩阵。
- **朝「开源流程」深入**：参照 [u3-l3](u3-l3-quartus-fpga-synthesis.md) 的 Yosys 限制，尝试把本设计（或你改造后的有符号版本）的 packed 多维数组拆平，让它能被 Yosys 读入，走通 OpenLane（ASIC）或 nextpnr（FPGA）开源流程，理解 RTL→GDSII/Bitstream 的完整链路。
- **回头夯实基础**：若在改造中遇到符号性、位宽、握手时序的疑问，回看 [u2-l1](u2-l1-processing-element-mac.md)（PE 乘加）、[u2-l3](u2-l3-row-column-matrix-transform.md)（矩阵变换）、[u2-l4](u2-l4-control-counter-clock-gating.md)（控制时序）与 [u3-l2](u3-l2-parameterization-and-sv-style.md)（参数化与编码风格），它们是所有二次开发的根基。
