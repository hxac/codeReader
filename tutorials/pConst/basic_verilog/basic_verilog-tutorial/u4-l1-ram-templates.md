# RAM/ROM 模板：单口与真双口

> 本讲属第 4 单元「存储器与 FIFO」的第一篇。前置讲义是 [u2-l4 位宽计算：clogb2 与 $clog2](u2-l4-clogb2-width.md)：本讲会用到一个核心结论——寻址 `DEPTH` 个表项所需的地址位宽等于 `$clog2(DEPTH)`，在仓库老代码里写作 `clogb2(DEPTH-1)`。

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `true_single_port_write_first_ram` 与 `true_dual_port_write_first_2_clock_ram` 这两个模板的端口、参数与行为；
- 说清什么叫 **write-first（写优先）** 读写同址语义，以及它与「读延迟一拍」的关系；
- 理解 `(* ramstyle = ... *)` / `RAM_STYLE` 属性如何引导综合工具把一段数组推断成 **block RAM（块内存）**，而不是一堆触发器；
- 学会用 `INIT_FILE` 参数 + `$readmemh` 在上电时初始化存储内容，并把单口 RAM 当 **ROM** 用；
- 自己动手例化一个 16×8 的 ROM，装入一张正弦表，并在 testbench 里扫描地址、打印读出值。

## 2. 前置知识

在硬件设计里，你需要「存一堆数据」时，有两种典型资源：

- **触发器（flip-flop / register）**：快、随机访问，但一个 bit 就占一个触发器，存几百个数据就把芯片塞满了。
- **块内存（block RAM，BRAM）**：FPGA 里专门切的独立存储小块（Xilinx 叫 BRAM，Altera 叫 M9K/M10K/M20K 等），容量大、省逻辑资源，但有一个硬性规矩——**它的读是「同步」的**：你今天给出地址，要等下一个时钟沿之后，数据才出现在输出口。

正因为读是同步的，我们写 RAM 模板时不能像写软件数组那样「给地址立刻拿数据」，而要按时钟节拍来安排。本讲这两个模板，本质上就是用一段标准的 SystemVerilog 行为级代码，**让综合工具认出「这是一块 BRAM」**，从而映射到芯片里的专用存储资源，而不是被拆成一堆触发器。

两个关键术语先交代清楚：

| 术语 | 含义 |
|---|---|
| 单口（single-port）RAM | 只有一组时钟/地址/数据线，**同一时刻要么读、要么写**。 |
| 真双口（true dual-port）RAM | 有两组完全独立的端口（A 与 B），各自带时钟/地址/数据，**可同时一读一写，甚至两边都能写**。 |
| write-first（写优先） | 同一拍对**同一地址**又读又写时，读出的是「刚写入的新值」，而不是旧值。 |

还有一个你应该已经熟悉的概念（来自 u1-l2 / u2-l4）：仓库里的模块几乎都遵循「头注释 / INFO / 例化模板 / module 实现」的**四段式**结构，并且大量使用 `#(parameter ...)` **参数化端口**——位宽、深度都由参数决定，例化时改参数就能改电路、不动源码。本讲这两个 RAM 模板正是这一风格的典型样本。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [true_single_port_write_first_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv) | 单口 RAM/ROM 模板，本讲的主角。 |
| [true_dual_port_write_first_2_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_dual_port_write_first_2_clock_ram.sv) | 真双口 RAM 模板，A/B 两端口可各自带独立时钟，本讲对照讲解。 |
| [clogb2.svh](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh) | 计算「地址/计数位宽」的老式函数，被两个模板 `\`include` 进来用于推导地址位宽。 |

补充引用（真实存在，用于讲清「用法」与「初始化文件格式」）：

| 文件 | 作用 |
|---|---|
| [fifo_single_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv) | 仓库里的单时钟 FIFO，**内部直接例化**真双口 RAM，是「真实用法」的最佳范例（下一讲 u4-l2 的主角）。 |
| [scripts/mem_writer_examples/16x8bit_linear.mem](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer_examples/16x8bit_linear.mem) | 一份现成的 16×8 初始化文件，演示 `$readmemh` 能直接吃掉的「纯十六进制、每行一个」格式。 |
| [scripts/mem_writer.sh](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer.sh) / [scripts/mem_writer_adv.py](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer_adv.py) | 仓库自带的 `.mem` 文件生成脚本（线性/随机/正弦/余弦）。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：4.1 单口模板的整体结构 → 4.2 write-first 语义 → 4.3 ramstyle 属性 → 4.4 `$readmemh` 初始化与 ROM 化 → 4.5 真双口模板与真实用法。

### 4.1 单口 RAM 模板：true_single_port_write_first_ram

#### 4.1.1 概念说明

「单口 RAM」是最简单的存储：一组 `clk`、一组 `addr`、一个 `we`（写使能）、一路 `din`（写数据）、一路 `dout`（读数据）。同一拍里，你要么在写（把 `din` 存进 `addr`），要么在读（把 `addr` 里的内容送到 `dout`），不能既读又写。

`true_single_port_write_first_ram.sv` 就是仓库给出的单口模板。它的 INFO 一句话点明了定位——「单口 RAM/ROM 模块，并在 Quartus 上验证可自动推断为块内存」：

[true_single_port_write_first_ram.sv:7-9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L7-L9) — INFO 说明这是单口 RAM/ROM，目标是让工具自动推断成块内存。

#### 4.1.2 核心流程

单口 RAM 一拍之内只走下面两条路径之一：

```text
每个 clk 上升沿，且 ena==1 时：
  ├─ 若 wea==1（写）：  data_mem[addra] <= din；同时 dout <= din   ← 写优先
  └─ 若 wea==0（读）：  dout <= data_mem[addra]                    ← 读延迟 1 拍
```

两个要点先记住，后面 4.2 会展开：

1. **读是同步的**：你这一拍给地址，**下一拍** `dout` 才有效。
2. **写的时候顺便把 `dout` 也更新成新值**：这就是模块名里 `write_first` 的由来。

#### 4.1.3 源码精读

先看参数与端口声明：

[true_single_port_write_first_ram.sv:32-46](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L32-L46) — 参数 `RAM_WIDTH`（字宽，默认 16）、`RAM_DEPTH`（深度，默认 8）、`RAM_STYLE`（实现风格，默认 `"block"`）、`INIT_FILE`（初始化文件，默认空）；端口为 `clka/addra/ena/wea/dina/douta`。

地址位宽是这一行的关键，它承接 u2-l4：

[true_single_port_write_first_ram.sv:41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L41) — `input [clogb2(RAM_DEPTH-1)-1:0] addra`。把 u2-l4 的等式 `clogb2(n) == $clog2(n+1)` 代入：`clogb2(RAM_DEPTH-1) == $clog2(RAM_DEPTH)`，正好是「寻址 DEPTH 个表项所需的地址位数」。例如 `RAM_DEPTH=16` → `clogb2(15)=4` → `addra[3:0]`，可寻址 0..15。

存储体本身只有一行声明（属性部分留到 4.3 讲）：

[true_single_port_write_first_ram.sv:59](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L59) — `(* ramstyle = RAM_STYLE *) logic [RAM_WIDTH-1:0] data_mem [RAM_DEPTH-1:0];`，这就是 RAM 的存储数组，深 `RAM_DEPTH`、每字 `RAM_WIDTH` 位。

例化模板（用法速查，复制即用）：

[true_single_port_write_first_ram.sv:13-29](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L13-L29) — 例化模板。注意模板里 `wea(1'b1)` 表示例化成「只写」口；当 ROM 用时把它改成 `wea(1'b0)` 即可。

> 小提示：仓库每个模块都把「例化模板」用块注释包起来放在 module 上方（u1-l2 讲过的四段式）。阅读顺序是**先看模板学会用，再看 module 懂原理**。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认地址位宽公式，建立对参数化深度的直觉。

1. 打开 [true_single_port_write_first_ram.sv:41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L41)。
2. 按 u2-l4 的 `clogb2` 表，手算以下三种深度的 `addra` 位宽：

   | `RAM_DEPTH` | `clogb2(RAM_DEPTH-1)` | `addra` 位宽 | 可寻址范围 |
   |---|---|---|---|
   | 8  | ? | ? | 0..? |
   | 16 | ? | ? | 0..? |
   | 32 | ? | ? | 0..? |

3. **预期结果**：分别为 3 位（0..7）、4 位（0..15）、5 位（0..31）。若你的答案与此一致，说明你已经把 u2-l4 的位宽公式用到了真实存储器上。

#### 4.1.5 小练习与答案

- **Q1**：把 `RAM_DEPTH` 从 8 改成 10（非 2 的幂），`addra` 位宽是几？还能正常寻址吗？
  - **答**：`clogb2(9)=4`，`addra` 仍是 4 位（0..15），地址 0..9 有效，10..15 会访问到未使用的存储单元。模板并不强制深度为 2 的幂（但配套的 FIFO 会让深度保持 2 的幂，见 u4-l2）。
- **Q2**：模块没有 `nrst` 复位端口，`data_mem` 上电后是什么值？
  - **答**：取决于 `INIT_FILE`——给了文件就按文件初始化（见 4.4）；不给就用 `initial` 循环清零。注意块内存**不支持**复位后重新初始化，所以它根本没有复位脚。

---

### 4.2 write-first：读写同址的「写优先」语义

#### 4.2.1 概念说明

双口/单口 RAM 在「同一地址同一拍又读又写」时，读到的值是新值还是旧值？业界定义了三种模式：

| 模式 | 同址同拍读到的值 |
|---|---|
| **write-first（写优先）** | 新值（刚写入的） |
| read-first（读优先） | 旧值 |
| no-change（不变） | 读数据保持不变 |

本模块选的是 **write-first**：写的同时，输出口立刻反映新数据。它的好处是行为直观、像软件数组；代价是写路径多了一条「同时把输出口也置成新值」的连线。

#### 4.2.2 核心流程

把 4.1.2 的流程再聚焦到「写优先」这一点上：

\[ \text{dout}(t) = \begin{cases} \text{din}(t) & \text{若 } ena \land wea \text{（写，输出新值）} \\ \text{data\_mem}[\text{addr}](t-1) & \text{若 } ena \land \overline{wea} \text{（读，输出上一拍存的内容）} \end{cases} \]

读路径的「\(t-1\)」就是**同步读延迟一拍**。

#### 4.2.3 源码精读

整段时序逻辑只有 10 行，却同时定义了「写优先」与「读延迟一拍」：

[true_single_port_write_first_ram.sv:79-88](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L79-L88) — `always @(posedge clka)`：写时 `data_mem[addra]<=din` 且 `ram_data_a<=din`（输出即新值，**write-first**）；读时 `ram_data_a<=data_mem[addra]`（**读延迟一拍**）。

注意它用的是 `always @(posedge clka)`（Verilog-2001 风格）而非 `always_ff`（本模块是较早期写法），但语义一致：全部非阻塞赋值 `<=`，描述的是同步时序电路。

输出没有任何额外流水线寄存器：

[true_single_port_write_first_ram.sv:90-91](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L90-L91) — `// no output register` + `assign douta = ram_data_a;`。意思是「读口只有 RAM 自带的那一拍寄存器，没有再串一级输出寄存」。所以读延迟恰好是 **1 个时钟**，没有 2 拍版本那种更高的 Fmax 换延迟的取舍。

#### 4.2.4 代码实践（波形观察型）

**目标**：在波形里看清「读延迟一拍」与「写优先」。

1. 写一个最小 testbench 例化 `RAM_WIDTH=8, RAM_DEPTH=16` 的单口模板（参考综合实践里的 tb 骨架）。
2. 序列 A（纯读）：`ena=1, wea=0`，依次给 `addra = 0,1,2,...`，每拍换一个地址。
3. 序列 B（写优先）：先往地址 5 写入 `8'hAB`（`wea=1, dina=8'hAB`），紧接着下一拍对**同一地址 5** 发起读（`wea=0`）。
4. **需要观察的现象**：
   - 序列 A：`douta` 比你给的 `addra` **晚一拍**才对上号（比如你给地址 3，要等下个沿 `douta` 才显示地址 3 的内容）。
   - 序列 B：写入 `8'hAB` 的那一拍，`douta` 在该拍结束时就已变成 `8'hAB`——这就是 write-first。
5. **预期结果**：序列 A 体现 1 拍读延迟；序列 B 体现「同址读写输出新值」。**待本地验证**（用 iverilog/ModelSim，方法见 u1-l3）。

#### 4.2.5 小练习与答案

- **Q1**：如果想让同址读写读到**旧值**（read-first），代码该怎么改？
  - **答**：把写分支里的 `ram_data_a <= dina;` 删掉，写时只更新 `data_mem[addra]`，不碰 `ram_data_a`；读到的就是该拍开始时数组里的旧内容。
- **Q2**：为什么说「读延迟恰好 1 拍」而不是 0 拍？
  - **答**：因为读路径 `ram_data_a <= data_mem[addra]` 是非阻塞赋值，要等下一个时钟沿才更新 `ram_data_a`，而 `douta` 直接取自 `ram_data_a`，所以从给地址到 `douta` 有效相隔一个时钟。这正是块内存的同步特性。

---

### 4.3 ramstyle / RAM_STYLE 属性：引导 block RAM 推断

#### 4.3.1 概念说明

你用 `logic [W-1:0] data_mem [D-1:0]` 声明了一段数组，综合工具会把它实现成什么？可能是块内存（BRAM），也可能是分布 RAM（LUTRAM），最糟是一堆触发器。工具会按自己的启发式猜。**`ramstyle` 属性就是你来「点名」**：告诉工具「请把这段数组做成块内存」，避免小块被误推断成触发器、白白浪费逻辑资源。

#### 4.3.2 核心流程

属性挂在数组声明前，值由参数 `RAM_STYLE` 传入：

```text
(* ramstyle = RAM_STYLE *)  logic [W-1:0] data_mem [D-1:0];
                            └─ 这段数组按 RAM_STYLE 指定的资源类型实现
```

不同厂商的取值不一样，模板顶部的注释列出了两家的常用值：

[true_single_port_write_first_ram.sv:48-54](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L48-L54) — Xilinx `ram_style` 取值 `auto|block|distributed|register|ultra`；Altera `ramstyle` 取值 `logic|M9K|MLAB` 等；注释特别注明「`ram_style` 在 Vivado 里与 `ramstyle` 等价」（即 Vivado 接受无下划线写法）。

所以模板统一写 `(* ramstyle = RAM_STYLE *)`（无下划线），**一套代码同时被 Quartus 与 Vivado 接受**，默认值 `"block"` 引导两边都推断成块内存。

#### 4.3.3 源码精读

[true_single_port_write_first_ram.sv:59](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L59) — 属性 `(* ramstyle = RAM_STYLE *)` 紧贴数组声明，是综合工具识别「这段数组如何实现」的指令。

注意上面还注释掉了一种 **Quartus 专用**的初始化写法：

[true_single_port_write_first_ram.sv:55-57](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L55-L57) — `(* ram_init_file = INIT_FILE *)` 可让 Quartus 直接读 `.mif` 文件初始化。它被注释掉，是因为作者选择了**跨工具通用**的 `$readmemh` 方案（见 4.4），而不只用 Quartus 私有属性。

#### 4.3.4 代码实践（对比型，需综合工具）

**目标**：感受 `RAM_STYLE` 对资源的影响。

1. 用 Vivado 或 Quartus 综合一个 `RAM_DEPTH=64, RAM_WIDTH=16` 的单口模板。
2. 分别用 `RAM_STYLE="block"` 与 `RAM_STYLE="register"` 综合两次。
3. **需要观察的现象**：看综合报告里的资源占用——`"block"` 应当占用块内存（BRAM / M10K），`"register"` 则会把数组铺成 64×16=1024 个触发器。
4. **预期结果**：`"block"` 省逻辑资源、用专用存储块；`"register"` 占用大量触发器。**待本地验证**（本实践依赖具体器件与工具版本）。

> 提示：仓库的跨 IDE 基准（u7-l3）就是用同一份 RTL 在不同工具下比 Fmax/资源，思路与本实践一致。

#### 4.3.5 小练习与答案

- **Q1**：如果完全不写 `(* ramstyle ... *)` 属性，这段数组还能变成块内存吗？
  - **答**：通常仍能，工具会自动推断，但小块（比如深度很浅）可能被推断成 LUTRAM 或触发器。属性的作用是**强制**指定，消除不确定性。
- **Q2**：为什么模板用无下划线的 `ramstyle` 而不是 Xilinx 文档里的 `ram_style`？
  - **答**：注释明说「`ram_style` 在 Vivado 里与 `ramstyle` 等价」，而无下划线写法也能被 Quartus 认，于是用一个名字兼容两家工具，保持模板「跨厂商」。

---

### 4.4 `$readmemh` 与 INIT_FILE：初始化与 ROM 化

#### 4.4.1 概念说明

很多场合你需要 RAM **上电就有内容**：查表（LUT）、正弦表、系数表、指令 ROM……仓库用「`INIT_FILE` 参数 + `$readmemh`」一步到位：

- `$readmemh("文件名", 数组, 起始地址, 结束地址)` 在仿真开始（`initial`）时把**十六进制**文本文件里的数依次灌进数组。
- 关键在于：现代 Vivado / Quartus 都会把这段 `initial $readmemh` 翻译成**块内存的上电初值**，所以它不仅仿真有效，**下板也有数**。这正是仓库选择它、而不用 Quartus 私有 `ram_init_file` 的原因。

把「写入使能 `wea` 永远接 0」+「`INIT_FILE` 装入内容」，单口 RAM 就成了 **ROM**——只读不写、内容上电固定。

#### 4.4.2 核心流程

初始化是一个 `generate` 二选一：

```text
若 INIT_FILE != "" ：  initial $readmemh(INIT_FILE, data_mem, 0, RAM_DEPTH-1);   ← 从文件装入
否则              ：  initial for(i=0..RAM_DEPTH-1) data_mem[i] = 0;            ← 全零
```

`.mem` 文件就是「纯十六进制、每行一个数」的文本。仓库现成的 [scripts/mem_writer_examples/16x8bit_linear.mem](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer_examples/16x8bit_linear.mem) 内容是 `0,1,2,...,F`，正好是 16×8 的初值表。

#### 4.4.3 源码精读

[true_single_port_write_first_ram.sv:64-77](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L64-L77) — `generate` 块按 `INIT_FILE` 是否为空二选一：给了文件就 `$readmemh(INIT_FILE, data_mem, 0, RAM_DEPTH-1)`，否则用 `initial` 循环把每个字清零。四个参数分别是「文件名、目标数组、起始下标、结束下标」。

> 对照参考：[`dual_port_single_port_ram_templates/Verilog/single_port_rom.v:23-26`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/dual_port_single_port_ram_templates/Verilog/single_port_rom.v#L23-L26) 是仓库另一处用 `$readmemb`（二进制版本）初始化 ROM 的范例，注释里明确「没有这个文件设计无法编译」，可见这套做法是仓库的标准实践。

仓库还提供脚本帮你批量生成 `.mem`：

- [scripts/mem_writer.sh:13-32](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer.sh#L13-L32) — bash 双重循环，按「深度 × 位宽」生成线性表（`printf "%0${ws}X\n"`）与随机表。
- [scripts/mem_writer_adv.py:26-35](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer_adv.py#L26-L35) — Python 计算 `sin(rad)*65535` 并写成十六进制，是正弦/余弦表生成器。

#### 4.4.4 代码实践（动手型：把单口 RAM 当 ROM 用）

**目标**：例化一个 16×8 的 ROM，装入一张正弦表，扫描地址并打印读出值。这是本讲的核心实践。

**步骤 1：准备 16×8 正弦表 `.mem` 文件**

仿照 `16x8bit_linear.mem` 的「纯十六进制、每行一个」格式，写一份 16 项、每项 8 位的正弦表。下面是本讲按 \(\text{val}_i = \mathrm{round}\!\left(127.5 \times (1+\sin(2\pi i/16))\right)\) **手算得到的示例数据**（记为 `sin16.mem`，不是仓库原有文件）：

```text
80
B0
DA
F5
FF
F5
DA
B0
80
4F
25
0A
00
0A
25
4F
```

> 提示：你也可以仿照 `mem_writer_adv.py` 自己写两行 Python 生成它；不同四舍五入约定下个别值可能差 1，不影响实践结论。

**步骤 2：写 testbench（示例代码，需按你的环境调整路径）**

```systemverilog
// 示例代码：sin16_rom_tb.sv —— 仅作示意，需自行放入工程并使 sin16.mem 可被读到
`timescale 1ns/1ps
module sin16_rom_tb;
  logic clk = 0;
  always #5 clk = ~clk;            // 100 MHz

  logic [3:0] addra;
  logic [7:0] douta;

  true_single_port_write_first_ram #(
    .RAM_WIDTH( 8 ),
    .RAM_DEPTH( 16 ),
    .RAM_STYLE( "block" ),
    .INIT_FILE( "sin16.mem" )      // 上电装入正弦表
  ) dut (
    .clka ( clk ),
    .addra( addra ),
    .ena  ( 1'b1 ),                // 始终使能
    .wea  ( 1'b0 ),                // ★ 关键：只读，ROM 模式
    .dina ( '0 ),
    .douta( douta )
  );

  initial begin
    addra = 0;
    for (int i = 0; i < 16; i++) begin
      addra = i[3:0];
      @(posedge clk);              // 同步读：本沿采样 addra
      #1;                          // 等 NBA 更新完，douta 才稳定
      $display("addr=%0d  douta=%02h", i, douta);
    end
    $finish;
  end
endmodule
```

**步骤 3：编译运行**

按 u1-l3 的方法，用 iverilog（须 `-g2012`）或 ModelSim 编译。注意：

- 两个模板都在模块末尾 `\`include "clogb2.svh"`，编译时要把 `clogb2.svh` 所在目录加入 include 搜索路径（`+incdir+` / `-I`）。
- `sin16.mem` 要放在仿真工作目录下，否则 `$readmemh` 找不到文件。

**需要观察的现象**：终端逐行打印 `addr=0..15` 与对应 `douta`。

**预期结果**：打印值与 `sin16.mem` 的 16 行**一一对应**（地址 0→`80`，地址 4→`FF`，地址 12→`00`，……）。若一致，说明 ROM 初始化与同步读两条机制都跑通了。**待本地验证**（路径与工具不同，命令需自行调整，参考 u1-l3）。

#### 4.4.5 小练习与答案

- **Q1**：把 `INIT_FILE` 留空（默认 `""`），再读地址 0，`douta` 会是什么？
  - **答**：`generate` 走「全零」分支，`data_mem` 全 0，所以读到 `8'h00`（所有地址都是 0）。
- **Q2**：`.mem` 文件里写成 `0,1,2,...,F`（用 `$readmemh`）和写成二进制 `0000,0001,...`（用 `$readmemb`）效果一样吗？
  - **答**：数值上等价，区别只在进制与函数：`$readmemh` 按十六进制读、`$readmemb` 按二进制读。本模板用的是 `$readmemh`，所以文件必须是十六进制。
- **Q3**：为什么模块没有「复位即可重新装入初值」的机制？
  - **答**：块内存的初值是上电时烧进去的，**触发器复位改不了块内存内容**（这点 FIFO 模块的 INFO 也专门警告过）。所以模板干脆不设复位脚，初值只在 `initial`/上电那一刻生效。

---

### 4.5 真双口 RAM：true_dual_port_write_first_2_clock_ram

#### 4.5.1 概念说明

「真双口（true dual-port）」有两组**完全独立**的端口 A 与 B：各自 `clk/addr/en/we/din/dout`。两个端口可同时一读一写、甚至两边同时写不同地址。它最常见的用途是做**跨时钟域的数据缓冲**：A 口接写时钟域、B 口接读时钟域，两个时钟频率/相位可以不同——这正是模块名里 `2_clock` 的含义。

#### 4.5.2 核心流程

A、B 两口的行为与单口完全同构，只是各有一套：

```text
posedge clka 且 ena：  wea? 写 A 侧 / 读 A 侧   （write-first）
posedge clkb 且 enb：  web? 写 B 侧 / 读 B 侧   （write-first）
两个端口共享同一个 data_mem 数组。
```

#### 4.5.3 源码精读

端口声明就是单口「复制成两份」：

[true_dual_port_write_first_2_clock_ram.sv:39-60](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_dual_port_write_first_2_clock_ram.sv#L39-L60) — 参数与单口一致；端口分两组：A 口 `clka/addra/ena/wea/dina/douta`、B 口 `clkb/addrb/enb/web/dinb/doutb`，地址位宽同样是 `clogb2(RAM_DEPTH-1)`。

两个 `always` 块分别驱动 A、B 口，写法与单口逐字一致：

[true_dual_port_write_first_2_clock_ram.sv:94-114](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_dual_port_write_first_2_clock_ram.sv#L94-L114) — A 口 `always @(posedge clka)`、B 口 `always @(posedge clkb)`，各自走「写优先/读延迟一拍」的同款逻辑；两端口共享同一 `data_mem`。

**真实用法**——仓库的 FIFO 直接例化了它：

[fifo_single_clock_ram.sv:102-121](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L102-L121) — 把真双口 RAM 当 FIFO 的存储体：A 口当**写口**（`clka=clk, wea=1'b1, addra=w_ptr, dina=w_data`），B 口当**读口**（`clkb=clk, web=1'b0, addrb=r_ptr, doutb=r_data`）。这里两个时钟都接同一个 `clk`（单时钟 FIFO），但模板本身支持两端口不同时钟。

#### 4.5.4 代码实践（源码阅读 + 微型 tb）

**目标**：用「一写一读」验证双口模板，体会「A 写 B 读」。

1. 阅读 [fifo_single_clock_ram.sv:102-121](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L102-L121)，记下 A/B 两口的接法。
2. 写一个最小 tb：例化 `RAM_WIDTH=8, RAM_DEPTH=16` 的双口模板；A 口 `wea=1, ena=1`，在地址 3 写 `8'h7E`；B 口 `web=0, enb=1`，几个周期后读地址 3。
3. **需要观察的现象**：A 口写入后，B 口能在后续读到 `8'h7E`。
4. **预期结果**：两个端口共享同一存储，A 写入的数据可被 B 读出（受读延迟一拍约束）。**待本地验证**。

#### 4.5.5 小练习与答案

- **Q1**：把单时钟 FIFO 里的双口 RAM 两端口都接 `clk`，那它和「真双口」的跨时钟域能力是什么关系？
  - **答**：模板**支持**两端口不同时钟（跨域），但 FIFO 把两个时钟都接到同一个 `clk`，于是退化成单时钟使用。需要跨域时，把 `clka/clkb` 接不同时钟即可（注意跨域还要处理满/空标志的同步问题，留到 u4-l2/u3）。
- **Q2**：A、B 两口同时写**同一地址**会怎样？
  - **答**：这是「写冲突」，结果未定义（取决于具体块内存硬件）。设计上应避免两端口对同一地址同时写；模板本身不做冲突仲裁。

---

## 5. 综合实践

把 4.1–4.4 串起来，搭一个「**扫描地址、串口……不，先打印正弦表**」的最小 ROM 读表器。本任务对应规格里指定的实践：

> 例化 `true_single_port_write_first_ram` 做一个 16×8 的 ROM，用 `.mem` 文件初始化为正弦表，在 testbench 中扫描地址打印读出值。

**任务分解**：

1. 用 `mem_writer_adv.py` 的思路（或直接手写）生成 `sin16.mem`（16 项 × 8 位，纯十六进制、每行一个，内容见 4.4.4）。
2. 例化 `true_single_port_write_first_ram`，参数 `RAM_WIDTH=8 / RAM_DEPTH=16 / RAM_STYLE="block" / INIT_FILE="sin16.mem"`；端口 `wea=1'b0, ena=1'b1`（ROM 模式）。
3. testbench 里用一个 0→15 的计数器当 `addra`，逐拍扫描；由于读延迟一拍，在 `@(posedge clk)` 后采样 `douta` 并 `$display`。
4. **进阶**（选做）：把 `douta` 接到 4.4 之外的模块——比如下一讲 u4-l2 的 FIFO 写口，或更后面 u6-l3 的 `pwm_modulator`，把这张正弦表变成一个「数控振荡器」的雏形。
5. **验收标准**：打印出的 16 个值与 `sin16.mem` 逐行一致，且在波形上能看到 `addra` 自增、`douta` 滞后一拍跟随的同步读关系。

> 编译要点（来自 u1-l3）：iverilog 加 `-g2012` 并用 `+incdir+` 指向 `clogb2.svh` 所在目录；`sin16.mem` 放进仿真工作目录。命令需按你的实际环境调整，**待本地验证**。

## 6. 本讲小结

- 单口模板 `true_single_port_write_first_ram` 用一段标准时序代码 + `(* ramstyle *)` 属性，让综合工具把数组推断成块内存，端口为 `clk/addr/en/we/din/dout`。
- **write-first**：同址又读又写时输出新值；读路径因非阻塞赋值天然**延迟一拍**（同步读），且没有额外输出寄存器。
- 地址位宽写作 `clogb2(RAM_DEPTH-1)`，由 u2-l4 的等式它等于 `$clog2(RAM_DEPTH)`——寻址 `DEPTH` 项所需的地址位数。
- `(* ramstyle = RAM_STYLE *)` 跨 Quartus/Vivado 通用，默认 `"block"` 强制走块内存；属性值两家取值不同（Xilinx `block/distributed/...`、Altera `M9K/MLAB/...`）。
- 初始化用 `generate` 二选一：`INIT_FILE` 非空则 `$readmemh` 装入（仿真与下电都有效），否则全零；`wea` 恒接 0 即 ROM 化。
- 真双口模板 `true_dual_port_write_first_2_clock_ram` 把单口逻辑复制成 A/B 两套，支持两端口不同时钟；仓库 FIFO 直接把它当「一写一读」的存储体例化。

## 7. 下一步学习建议

- 下一讲 **[u4-l2 单时钟 FIFO：fifo_single_clock_ram](u4-l2-single-clock-fifo.md)** 会深入讲解本讲末尾看到的那个 FIFO：它如何用真双口 RAM + 环形指针 + `cnt` 计数实现满/空判断与同时读写仲裁。你现在已经掌握了它的「存储体」部分，下一步只需看懂「控制逻辑」。
- 若想从「初始化」延伸，可阅读 [scripts/mem_writer.sh](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer.sh) 与 [scripts/mem_writer_adv.py](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/mem_writer_adv.py)，自己生成任意深度/位宽、任意函数的 `.mem` 表。
- 进阶可对比 [`dual_port_single_port_ram_templates/`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/dual_port_single_port_ram_templates) 目录下的厂商原版模板（read-first / no-change 等其它读写模式），理解为什么作者最终选了 write-first。
