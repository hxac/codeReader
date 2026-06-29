# Quartus FPGA 综合与资源利用

## 1. 本讲目标

前几讲我们一直在「仿真」世界里验证这个脉动阵列——用 Verilator 把 SystemVerilog 翻译成 C++ 跑测试台。仿真能告诉我们「逻辑对不对」，但回答不了「这块逻辑真正烧进芯片要花多少硬件」。本讲把视角从**仿真**切到**综合（synthesis）与实现（implementation）**，目标有三个：

1. 读懂 Quartus 工程文件 `.qpf` / `.qsf`，知道 FPGA 工具是靠哪些「赋值」把一堆 `.sv` 源码锁定到一颗具体芯片上的。
2. 学会看 Cyclone V 芯片的**资源利用报告**（逻辑单元、寄存器、引脚、DSP 块），并理解为什么本设计的 **DSP 块数量恰好等于 PE 数量**。
3. 理解把同一份 RTL 拿去开源工具链（Yosys）或严格 SV2005 编译器时会踩到什么坑（`for (genvar ...)` 语法、packed 数据类型）。

学完后，你应该能独立打开 `FPGA/` 工程文件，指出综合目标芯片、顶层模块和源文件清单，并能用规模参数 \(N\) 预测出阵列会吃掉多少 DSP 与引脚。

## 2. 前置知识

在进入正文前，先建立几个 FPGA 综合相关的基础直觉。如果你已经熟悉，可以跳到第 3 节。

- **RTL 与网表（netlist）**：我们写的 SystemVerilog 是「寄存器传输级」描述，描述的是数据在寄存器之间如何流动、如何组合。综合工具（Quartus、Yosys 等）的任务，就是把这份 RTL 翻译成由具体芯片基本单元（查找表 LUT、寄存器、DSP 块、布线）组成的「网表」。仿真关心「逻辑对不对」，综合关心「这些逻辑能不能装进这颗芯片、要花多少资源」。

- **ALM（Adaptive Logic Module）**：Intel/Altera FPGA 的基本逻辑单元，内部含查找表（ALUT）和寄存器。Quartus 报告里的「Total logic elements」指的就是 ALM 数量。可以粗略类比为 Xilinx 那边的 LUT/CLB。

- **DSP 块**：FPGA 里专门为乘加运算硬化的硅模块。Cyclone V 的一个 DSP 块内含可变精度乘法器（最多 18×18 位）和累加器。一个 8×8 的乘法会被工具「吸」进 DSP 块，而不是用一片片 ALM 拼出来——这样更快、更省逻辑资源。

- **`.qpf` 与 `.qsf`**：Quartus 的两个工程文本文件。`.qpf`（Quartus Project File）是工程的身份名片（版本、修订名）；`.qsf`（Quartus Settings File）是真正的「配置清单」，用一行行 `set_global_assignment` 告诉工具：目标器件家族、具体型号、顶层是哪个模块、要编译哪些源文件、引脚怎么绑、时序约束是多少。二者都是纯文本，可以用编辑器直接看，也能被版本管理。

- **SV2005**：SystemVerilog 有多个标准版本（IEEE 1800-2005、1800-2009、1800-2012、1800-2017）。版本越新，语法越方便（比如可以在 `for` 里直接声明 `genvar`）。不同工具支持到哪个版本不一样，这就埋下了后面要讲的兼容性坑。

## 3. 本讲源码地图

本讲涉及的文件分两类：FPGA 工程文件（决定「怎么综合」）和 RTL/文档（决定「被综合的是什么」）。

| 文件 | 角色 |
|---|---|
| `FPGA/2D-Systolic-Array-Multiplier.qpf` | 工程身份文件，记录 Quartus 版本与工程修订名。 |
| `FPGA/2D-Systolic-Array-Multiplier.qsf` | 工程配置清单：目标芯片家族/型号、顶层模块、源文件、输出目录、仿真工具等。本讲重点。 |
| `rtl/pe.sv` | 处理单元。其中的 `mult = i_a*i_b` 是每个 PE 占用一个 DSP 块的根源。 |
| `rtl/systolicArray.sv` | 阵列互联，含 `for (genvar ...)` 风格的 generate——SV2005 兼容性 caveat 的现场之一。 |
| `rtl/topSystolicArray.sv` | 顶层，同样含 `for (genvar ...)` 变换循环。 |
| `README.md` | `Further Work` 一节给出资源利用截图与 SV2005/Yosys 的文字说明。 |

## 4. 核心概念与源码讲解

本讲按「工程文件 → 资源映射 → 工具兼容性」三个最小模块展开。

### 4.1 Quartus 工程文件：把源码锁到一颗芯片上

#### 4.1.1 概念说明

FPGA 综合不是「给我一堆 `.sv` 我自己想办法」。工具必须先回答三个问题：

1. **烧到哪颗芯片？**（器件家族 + 具体型号）
2. **顶层模块是谁？**（综合从哪个模块开始向外展开）
3. **要编译哪些源文件？**（工程包含哪些 `.v` / `.sv`）

这三个答案，连同引脚约束、时序约束、输出目录等，全部写在 `.qsf` 里，一行一个 `set_global_assignment`（全局赋值）。而 `.qpf` 只是记录工程版本与修订名，让 Quartus 能识别「这是一个工程、名字叫什么」。

把它们理解成「工程的两张配置卡」：`.qpf` 是封面，`.qsf` 是正文。

#### 4.1.2 核心流程

Quartus 综合一个工程的标准流程：

1. 读取 `.qpf`，确定工程修订名与 Quartus 版本。
2. 读取 `.qsf`，确定器件（`FAMILY` + `DEVICE`）、顶层（`TOP_LEVEL_ENTITY`）、源文件清单（若干 `SYSTEMVERILOG_FILE`）。
3. 从顶层模块开始精化（elaboration）整个设计层次，展开参数化与 generate。
4. 综合（Analysis & Synthesis）成工艺映射网表。
5. 适配（Fitter / Place & Route）到 `DEVICE` 指定的芯片。
6. 产出资源利用报告、时序报告，写到 `PROJECT_OUTPUT_DIRECTORY`（本工程是 `output_files`）。

#### 4.1.3 源码精读

先看 `.qsf` 里最关键的几行——把设计锁到 Cyclone V 的 5CSEMA5F31C6，并指定顶层为 `topSystolicArray`：

[FPGA/2D-Systolic-Array-Multiplier.qsf:40-42](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/FPGA/2D-Systolic-Array-Multiplier.qsf#L40-L42)

```tcl
set_global_assignment -name FAMILY "Cyclone V"
set_global_assignment -name DEVICE 5CSEMA5F31C6
set_global_assignment -name TOP_LEVEL_ENTITY topSystolicArray
```

- `FAMILY "Cyclone V"` 选定器件家族，限制后续可选型号范围。
- `DEVICE 5CSEMA5F31C6` 是具体型号——这正是 DE10-Nano / DE1-SoC 开发板上那颗 Cyclone V SoC 芯片，逻辑资源约 32,070 个 ALM、87 个 DSP 块。这串型号会在第 4.2 节的「芯片总量」里再次出现。
- `TOP_LEVEL_ENTITY topSystolicArray` 指定综合入口就是我们在 u1-l3 读过的顶层模块。

接着是源文件清单与输出/仿真配置：

[FPGA/2D-Systolic-Array-Multiplier.qsf:46-53](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/FPGA/2D-Systolic-Array-Multiplier.qsf#L46-L53)

```tcl
set_global_assignment -name SYSTEMVERILOG_FILE ../rtl/topSystolicArray.sv
set_global_assignment -name SYSTEMVERILOG_FILE ../rtl/systolicArray.sv
set_global_assignment -name SYSTEMVERILOG_FILE ../rtl/pe.sv
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files
...
set_global_assignment -name EDA_SIMULATION_TOOL "Questa Intel FPGA (SystemVerilog)"
```

注意三个要点：

1. **路径用 `../rtl/`**：`.qsf` 位于 `FPGA/` 子目录，而 RTL 在仓库根的 `rtl/` 下，所以是相对 `FPGA/` 往上一层再进 `rtl/`。这意味着工程文件可以随仓库一起移动，只要相对结构不变。
2. **三个源文件与 u2 的模块层次一一对应**：顶层 `topSystolicArray` → 实例化 `systolicArray` → 内部 generate 出 `pe`。Quartus 会从顶层自动找到下游模块，但显式列出源文件能避免「漏文件」。
3. **`EDA_SIMULATION_TOOL` 指向 Questa**：Quartus 工程默认的门级仿真器是 Questa Intel FPGA（注意：本仓库实际功能验证用的是 Verilator，见 u3-l1，这里的 Questa 选项只是 Quartus 工程自带的仿真配置，与 Verilator 流程并行存在）。

再看 `.qpf`，它非常短，只记身份信息：

[FPGA/2D-Systolic-Array-Multiplier.qpf:26-31](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/FPGA/2D-Systolic-Array-Multiplier.qpf#L26-L31)

```tcl
QUARTUS_VERSION = "23.1"
DATE = "09:50:38  January 27, 2024"
# Revisions
PROJECT_REVISION = "2D-Systolic-Array-Multiplier"
```

`QUARTUS_VERSION "23.1"` 与 `.qsf` 头部注释里的 `Version 23.1std.0 Build 991`（Lite 版）一致，表明工程是用 Quartus Prime 23.1 标准版精简版创建的。`PROJECT_REVISION` 是修订名，Quartus 允许同一工程有多套修订（不同 `.qsf`），本工程只有一套。

#### 4.1.4 代码实践

1. **实践目标**：不打开 GUI，纯靠读文本文件说出「这工程综合到什么芯片、顶层是谁、含哪些源文件、报告输出到哪」。
2. **操作步骤**：用编辑器打开 `FPGA/2D-Systolic-Array-Multiplier.qsf`，定位到上面四组赋值；再打开 `.qpf` 确认 Quartus 版本与修订名。
3. **需要观察的现象**：你会看到 `.qsf` 中所有「真正影响综合目标」的关键信息都集中在文件中段（`FAMILY`/`DEVICE`/`TOP_LEVEL_ENTITY`/`SYSTEMVERILOG_FILE`），而文件头部大段是 Intel 的版权注释、尾部是一些 `EDA_GENERATE_FUNCTIONAL_NETLIST`、`PARTITION_*` 之类的默认值。
4. **预期结果**：你能口述出「Cyclone V 5CSEMA5F31C6、顶层 topSystolicArray、三个 `../rtl/*.sv` 源文件、报告写进 output_files」。
5. **待本地验证**：若你装了 Quartus，可在命令行 `cd FPGA && quartus_sh --flow compile 2D-Systolic-Array-Multiplier`（或 GUI 打开 `.qpf` 后点 Compile）触发综合，确认 `output_files/` 下生成报告。

#### 4.1.5 小练习与答案

**练习 1**：如果要把这个设计改烧到另一颗芯片（例如 Cyclone IV 的 EP4CE22F17C6），至少要改 `.qsf` 里的哪两行？

**参考答案**：`FAMILY`（从 `"Cyclone V"` 改为 `"Cyclone IV"`）和 `DEVICE`（从 `5CSEMA5F31C6` 改为 `EP4CE22F17C6`）。器件家族与具体型号必须配套，且新芯片的 DSP 块数量/结构不同，会影响第 4.2 节的资源占用。

**练习 2**：`.qsf` 里 `SYSTEMVERILOG_FILE` 的路径是 `../rtl/topSystolicArray.sv`，这个相对路径的基准目录是哪里？如果把 `FPGA/` 目录整体移动到仓库外，路径会失效吗？

**参考答案**：基准是 `.qsf` 所在的 `FPGA/` 目录。只要 `FPGA/` 与 `rtl/` 仍保持「兄弟目录」关系（即 `FPGA/` 的上一层里有 `rtl/`），`../rtl/...` 就有效；把 `FPGA/` 单独移走而把 `rtl/` 留下，路径就会失效。

---

### 4.2 Cyclone V 综合与资源利用：DSP 与 PE 的一一对应

#### 4.2.1 概念说明

综合完成后，Quartus 会给一份**资源利用报告（Flow Summary / Fitter Report）**，告诉你设计用了多少 ALM、寄存器、引脚、存储位、DSP 块。这是评估「设计能不能装进芯片、规模还能不能再放大」的核心依据。

本设计的资源利用有一个非常漂亮、可预测的特性：**DSP 块的数量恰好等于 PE 的数量**。原因藏在 PE 的乘法里——每个 PE 做一次 8×8 乘法，综合工具会把这一个乘法映射到一颗 DSP 块。这是脉动阵列「大量重复 MAC 单元」结构在 FPGA 上的自然落地。

README 用一句话点明了这个关系，并附了两张资源截图（2×2 与 4×4 阵列）。

#### 4.2.2 核心流程

为什么 DSP 数会等于 PE 数？推理链如下：

1. 阵列由 \(N\times N\) 个 PE 构成，PE 总数 \(=N^{2}\)。
2. 每个 PE 内部唯一的乘法是 `mult = i_a*i_b`（两个 8 位无符号数相乘）。
3. 一个 8×8 乘法正好能装进 Cyclone V DSP 块的可变精度乘法器（最大 18×18）。
4. 综合器发现 \(N^{2}\) 个互相独立的乘法，于是各分配一颗 DSP 块。

因此可建立可预测的关系式：

\[
\text{DSP 块数} = N^{2}
\]

同理，引脚数也能从端口位宽推出来。顶层端口为 `i_a`、`i_b`（各 \(N\times N\times 8\) 位）、`o_c`（\(N\times N\times 32\) 位），加上 4 个控制/时钟信号（`i_clk`、`i_arst`、`i_validInput`、`o_validResult`）：

\[
\text{引脚数} = 2\cdot N^{2}\cdot 8 + N^{2}\cdot 32 + 4 = 48N^{2} + 4
\]

把 \(N=2\)、\(N=4\) 代入，可与截图对拍：

- \(N=2\)：DSP \(=4\)，引脚 \(=48\cdot4+4=196\)。
- \(N=4\)：DSP \(=16\)，引脚 \(=48\cdot16+4=772\)。

#### 4.2.3 源码精读

先看 README 对资源与 DSP 关系的说明（这是本模块结论的直接出处）：

[README.md:142-155](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L142-L155)

> The RTL was compiled using Quartus Prime to target a Cyclone V FPGA and the resource utilization for a 2x2 and 4x4 systolic array is shown below.
> …
> The number of DSP units utilised correspond to the number of PEs instantiated in each systolic array respectively.
> Note: Quartus only supports SystemVerilog 2005 so the syntax of the for loops needs to be amended to compile the design.

这一段同时给出了两个本讲核心结论：DSP 数 = PE 数，以及 SV2005 的 for 循环 caveat（留到 4.3 讲）。两张截图位于 `images/2x2Resource.png` 与 `images/4x4Resource.png`。

再看 DSP 映射的根源——PE 里的乘法：

[rtl/pe.sv:22-25](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L22-L25)

```systemverilog
logic [31:0] mult;

always_comb
  mult = i_a*i_b;
```

`i_a`、`i_b` 都是 `[7:0]`，`i_a*i_b` 是 8×8 无符号乘法，结果扩展进 32 位 `mult`。正是这一行让综合器在每个 PE 里识别出一个可硬化乘法器，进而占用一颗 DSP。注意 PE 里**没有第二个乘法**——加法（`mac_q + mult`）用 ALM 实现即可，不必额外占用 DSP。

把两张截图的关键读数整理成下表（4×4 的精确寄存器数请以 `images/4x4Resource.png` 截图为准）：

| 资源 | 2×2 阵列（\(N=2\)，4 PE） | 4×4 阵列（\(N=4\)，16 PE） | 芯片总量 5CSEMA5F31C6 |
|---|---|---|---|
| 逻辑单元（ALMs） | 123 | 532（约 2%） | 32,070 |
| 寄存器 | 245 | 显著上升（见截图） | 32,070 |
| I/O 引脚 | 196 | 772 | — |
| **DSP 块** | **4** | **16（约 18%）** | **87** |

观察要点：

- **DSP 列完美符合 \(N^{2}\)**：4 PE → 4 DSP，16 PE → 16 DSP。4×4 用掉 16/87 ≈ 18% 的 DSP，说明这颗芯片理论上有余量做到更大的阵列（理论上 \(N=\lfloor\sqrt{87}\rfloor=9\) 时 DSP 占满，但引脚与逻辑会成为更早的瓶颈）。
- **逻辑单元（ALM）随规模超线性增长**：从 \(N=2\) 的 123 涨到 \(N=4\) 的 532。因为顶层 `topSystolicArray` 里的 `row_q`/`col_q` 移位寄存器宽度是 \(N\times(2N-1)\times 8\)，随 \(N\) 平方级膨胀，这部分吃掉不少 ALM/寄存器。
- **引脚是最大瓶颈**：4×4 已经要 772 个 I/O，而 5CSEMA5F31C6 的可用用户 I/O 远少于这个量级。这说明本设计直接按顶层扁平端口烧进芯片并不现实——真实部署必然要把 `i_a`/`i_b`/`o_c` 改成时分复用的窄接口（这也是 README「Further Work」里提到对接 SIMD 处理器的动机之一）。

#### 4.2.4 代码实践

1. **实践目标**：用规模参数 \(N\) 预测 DSP 与引脚占用，再与截图对拍，验证「DSP=PE」关系。
2. **操作步骤**：
   - 打开 `images/2x2Resource.png`、`images/4x4Resource.png`，记录各自的 Total DSP Blocks 与 Total pins。
   - 用公式 \(\text{DSP}=N^{2}\)、\(\text{pins}=48N^{2}+4\) 分别算 \(N=2\)、\(N=4\) 的预测值。
   - 把预测值与截图对拍。
3. **需要观察的现象**：DSP 列 4→4、16→16 严格等于 PE 数；引脚 196、772 与公式吻合。
4. **预期结果**：预测与截图一致，从而确认「每个 PE 的 8×8 乘法独占一颗 DSP」。
5. **待本地验证**：若本地有 Quartus，可把 RTL 参数 `N` 与相关改动设好后综合一次，自己读 `output_files/` 下的报告，确认你预测的 DSP 数与工具报告一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PE 里的加法 `mac_q + mult` 没有额外占用 DSP 块？

**参考答案**：Cyclone V 的 DSP 块虽然含累加器，但综合器是否把加法也吸收进 DSP 取决于面积/时序权衡。这里乘法 `mult` 已经是明确的硬化候选；而 `mac_q + mult` 是 32 位加法，用普通 ALM 实现代价很低，工具倾向于把乘法放进 DSP、加法留在 ALM。无论加法落在哪里，乘法只有一个，所以「DSP 数 = PE 数」的关系不变。

**练习 2**：仅从 DSP 资源看，5CSEMA5F31C6（87 个 DSP）理论上最大能放多大的方阵阵列？实际为什么到不了那么大？

**参考答案**：\( \lfloor\sqrt{87}\rfloor = 9\)，纯按 DSP 计可放 9×9（81 个 DSP）。但实际到不了：其一，引脚 \(48N^{2}+4\) 在 \(N=9\) 时已逾 3000，远超芯片用户 I/O；其二，顶层移位寄存器与控制逻辑的 ALM 占用随 \(N\) 平方增长。所以 DSP 并非唯一瓶颈，引脚与逻辑会更早封顶。

---

### 4.3 SV2005 for 循环语法 caveat 与 Yosys 开源流程限制

#### 4.3.1 概念说明

同一份 RTL，在不同工具下「能不能编过」并不一样。本设计踩到两类工具兼容性问题：

1. **Quartus 的 SV2005 限制**：README 明确写「Quartus only supports SystemVerilog 2005 so the syntax of the for loops needs to be amended」。本设计大量使用 `for (genvar i = 0; i < N; i++)` 这种**内联 genvar 声明**写法，这是 SystemVerilog 2009（IEEE 1800-2009）才引入的语法，SV2005 不支持，必须改写才能在 Quartus 下综合。

2. **Yosys 不支持 packed 数据类型**：本设计（尤其 `systolicArray.sv`、`topSystolicArray.sv`）大量使用 packed 多维数组（如 `logic [N-1:0][N:0][7:0]`）。Yosys 是主流开源综合工具（OpenLane ASIC 流程、FPGA 的 nextpnr 流程都靠它），但它对 packed 数据类型的支持有限，导致开源流程跑不动这份 RTL。

这两点共同说明：**「仿真过了」不等于「到处都能综合」**。Verilator 是为仿真优化的，宽松；而真实综合器（尤其开源工具）对 SV 子集的支持更窄。设计要走向物理实现，编码风格必须向「最窄公共子集」靠拢。

#### 4.3.2 核心流程

**SV2005 的改写思路**：SV2009 的内联写法

```systemverilog
for (genvar i = 0; i < N; i++) begin: PerRow
  for (genvar j = 0; j < N; j++) begin: PerCol
    ...
  end
end
```

要改写成 SV2005 兼容形式，两处变动：

1. **把 `genvar` 声明提到外面**：SV2005 不允许在 `for` 头里声明 genvar，需单独 `genvar i, j;`。
2. **显式 `generate ... endgenerate` 包夹**：SV2005/Verilog-2001 要求 generate 区域用关键字界定（SV2009 起可省略）。

改写后大致是：

```systemverilog
genvar i, j;
generate
  for (i = 0; i < N; i = i + 1) begin: PerRow
    for (j = 0; j < N; j = j + 1) begin: PerCol
      ...
    end
  end
endgenerate
```

**Yosys 的 packed 限制**：Yosys 前端对 SV 的 packed 多维数组支持不全（尤其是在端口、模块间互联上用 `logic [A][B][C]` 并整体移位/下标）。开源流程下需要把多维 packed 数组**拆平（flatten）**成一位宽的大向量（如 `logic [N*N*8-1:0]`）再手工做下标运算。这正是 README 里说的「I plan on forking the project and unpacking all the data types」。

> 说明：以上 SV2005 改写与「拆 packed」均为依据 SV 标准与 README 描述给出的源码阅读型推断，属于示例说明，并非仓库中已存在的另一份代码。

#### 4.3.3 源码精读

先看 README 对两条工具限制的原文：

[README.md:130-140](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L130-L140)

> The RTL makes use of packed data types for ease of coding … However, packed data types are not supported by a widely used open source tool - Yosys. …
> In the future, I plan on forking the project and unpacking all the data types.

这段确认了 packed 数据类型是为了「编码方便、可读性」而用，代价是牺牲了 Yosys 开源流程兼容性。

接着看 SV2009 风格 generate 的现场——`systolicArray.sv` 用内联 `genvar` 声明、无 `generate` 关键字：

[rtl/systolicArray.sv:42-74](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L42-L74)

```systemverilog
for (genvar i = 0; i < N; i++) begin: PerDummyRowColInterconnect
  ...
end: PerDummyRowColInterconnect

for (genvar i = 0; i < N; i++) begin: PerRow
  for (genvar j = 0; j < N; j++) begin: PerCol
    pe u_pe ( ... );
  end: PerCol
end: PerRow
```

`for (genvar i = ...)` 直接在 `for` 头里声明并初始化 genvar，且整个块没有外层 `generate`/`endgenerate`——这正是 SV2009 起允许、SV2005 不允许的写法。`topSystolicArray.sv` 的变换循环用的是同一套风格：

[rtl/topSystolicArray.sv:111-133](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L111-L133)

```systemverilog
for (genvar i = 0; i < N; i++) begin: perRowCol
  ...
  for (genvar j = 0; j < N; j++) begin: perRowElement
    ...
  end: perRowElement
end: perRowCol
```

所以 README 说的「the syntax of the for loops needs to be amended」，指的就是散布在 `systolicArray.sv`（三处）和 `topSystolicArray.sv`（三处 `for (genvar ...)`）里的这些循环——综合进 Quartus 前都要按 4.3.2 的方式改写。

再看 packed 多维数组的现场（Yosys 痛点所在）。互联网与端口上的 packed 写法在 `systolicArray.sv`：

[rtl/systolicArray.sv:26-39](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L26-L39)

```systemverilog
input  var logic [N-1:0][(2*N)-2:0][7:0] i_row
...
logic [N-1:0][N:0][7:0] rowInterConnect;
logic [N:0][N-1:0][7:0] colInterConnect;
```

这种 `[N-1:0][(2*N)-2:0][7:0]` 三维 packed 数组，配合 u2-l3 讲过的整体左移/右移、按下标 `[i][j]` 访问，在 Verilator 与 Quartus 下都没问题，但正是 Yosys 难以消化的结构。

#### 4.3.4 代码实践

1. **实践目标**：把 `systolicArray.sv` 的一处 `for (genvar ...)` 改写成 SV2005 兼容形式，并理解 packed 数组为何难倒 Yosys。
2. **操作步骤**：
   - 找到 `systolicArray.sv` 第 56-74 行的 `PerRow`/`PerCol` 双层循环。
   - 在循环**外**新增一行 `genvar i, j;`，把两个 `for` 头改成 `for (i = 0; i < N; i = i + 1)`，并用 `generate`/`endgenerate` 包住整段。
   - （仅思考，不必真改）设想把 `rowInterConnect [N-1:0][N:0][7:0]` 拆成一位宽向量 `logic [N*N*8-1 : 0]`，问：原来 `rowInterConnect[i][j]` 这样的二维下标访问，要如何换算成一位宽向量的位段？
3. **需要观察的现象**：改写后逻辑完全等价（只是语法更老派）；packed 拆平后，每个原本 `[i][j][7:0]` 的 8 位元素对应一位宽向量里的 `[ ((i*(N+1)+j)*8) +: 8 ]` 位段。
4. **预期结果**：你能说清楚「SV2005 改写只动语法不动行为」，而「拆 packed 既动数据结构又要重写下标」——后者工作量大得多，这正是作者把 Yosys 兼容列为「future fork」的原因。
5. **待本地验证**：若本地有 Quartus，可用改写后的版本综合一次确认通过；若本地有 Yosys（`yosys -p "read_verilog -sv ..."`），可尝试读入原 RTL，观察它在 packed 类型上报什么错。

> 注意：本实践涉及修改源码语法，请在**副本**上操作，不要改动仓库原始 RTL。

#### 4.3.5 小练习与答案

**练习 1**：`for (genvar i = 0; i < N; i++)` 这一行里，哪一部分是 SV2009 才支持、SV2005 必须改掉的？

**参考答案**：是 `genvar` 的**内联声明**——即在 `for` 头里直接写 `genvar i`。SV2005 要求 genvar 单独声明（`genvar i;`），不能塞进 `for` 头。同时 SV2005 还要求用 `generate ... endgenerate` 显式界定 generate 区域。

**练习 2**：为什么作者宁可让 RTL 暂不支持 Yosys，也要保留 packed 多维数组写法？

**参考答案**：packed 多维数组让矩阵在代码里保持「二维形状」（`i_a[i][j]`、`rowInterConnect[i][j]`），可读性高、与数学定义同构，便于在 Verilator/Quartus 这类支持完整的工具下开发与维护。代价是放弃 Yosys 开源流程。作者判断「可读性 > 开源流程兼容」，并把拆 packed 列为后续 fork 工作，是一次明确的工程取舍。

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「从工程文件到资源预测」的小任务：

1. **读工程**：打开 `FPGA/2D-Systolic-Array-Multiplier.qsf`，写出目标芯片家族、型号、顶层模块、源文件清单。
2. **算资源**：假设把阵列规模从默认的 \(N=4\) 放大到 \(N=8\)，用本讲的公式预测 DSP 块数（\(=N^{2}=64\)）与引脚数（\(=48N^{2}+4=3076\)），并判断 5CSEMA5F31C6（87 个 DSP）的 DSP 是否够用、引脚是否够用。
3. **判兼容**：指出放大 \(N\) 后，`systolicArray.sv` 里哪些 `for (genvar ...)` 循环需要为 Quartus SV2005 改写，以及 packed 端口位宽变化对 Yosys 兼容性的影响。
4. **下结论**：写一段话回答「这个设计在 Cyclone V 上能否继续放大、瓶颈在哪、要走向开源流程需做什么改造」。

参考结论要点：\(N=8\) 时 DSP 64 < 87 够用，但引脚 3076 远超芯片用户 I/O，引脚是首要瓶颈；for 循环仍需按 4.3.2 改写；packed 端口宽度随 \(N\) 变宽，Yosys 兼容仍需拆平。最终建议把扁平宽端口改成窄接口时分复用（对接 SIMD 处理器），并把 packed 拆平以兼容开源流程。

## 6. 本讲小结

- Quartus 工程由 `.qpf`（身份）与 `.qsf`（配置清单）组成；`.qsf` 里 `FAMILY`/`DEVICE`/`TOP_LEVEL_ENTITY`/`SYSTEMVERILOG_FILE` 四类赋值决定综合目标与源码范围。
- 本设计综合到 Cyclone V 5CSEMA5F31C6（约 32,070 ALM、87 DSP）；2×2 用 4 个 DSP、4×4 用 16 个 DSP，引脚分别为 196、772。
- **DSP 块数恰好等于 PE 数 \(N^{2}\)**，因为每个 PE 的 8×8 乘法独占一颗 DSP；引脚数满足 \(48N^{2}+4\)，可由端口位宽推导并与截图对拍。
- RTL 用了 SV2009 风格的 `for (genvar ...)` 内联声明，综合进 Quartus（SV2005 子集）前需改写为单独 `genvar` 声明 + `generate/endgenerate`。
- RTL 大量使用 packed 多维数组提升可读性，但 Yosys 开源流程对 packed 支持有限，需要后续 fork 把数据类型「拆平」。
- 引脚是放阵列规模的首要瓶颈，真实部署需把宽端口改成时分复用窄接口（呼应 README 的 SIMD 处理器设想）。

## 7. 下一步学习建议

- **向「接口窄化」深入**：本讲指出引脚是瓶颈，下一站可读 README「Interface module to a custom SIMD processor」设想，并尝试设计一个把 `i_a`/`i_b`/`o_c` 串行化的 wrapper，体会从「仿真宽端口」到「FPGA 窄引脚」的工程改造。
- **向「开源流程」深入**：参照 u3-l4 的改造实践，尝试把 packed 数组拆平，让 RTL 能被 Yosys 读入，理解 ASIC（OpenLane）与 FPGA（nextpnr）开源流程的差异。
- **复看参数化与编码风格**：结合 u3-l2，把本讲的 SV2005 兼容性问题放进「参数化设计 + packed 数组 + 编码约定」的整体框架里，思考「可读性 vs 可综合性」的取舍。
- **复看 RTL 模块**：若对 DSP 映射与 generate 互联仍有疑问，回看 u2-l1（PE 乘加）与 u2-l2（阵列 generate 互联），它们是本讲资源结论的源头。
