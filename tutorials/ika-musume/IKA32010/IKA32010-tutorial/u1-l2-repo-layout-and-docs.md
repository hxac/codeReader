# 目录结构、源码与文档导航

## 1. 本讲目标

本讲不写任何 RTL 逻辑，只解决一个问题：**拿到这个仓库后，文件摆在哪、各自管什么、遇到不懂的行为该翻哪份资料。** 学完本讲，你应当能够：

- 说出 `src/` 下四个文件分别负责什么，并画出它们之间「谁包含谁、谁调用谁」的关系图。
- 知道 `docs/` 下三份官方资料各自的定位，能判断「我现在的问题该先翻哪一份」。
- 走通一条「官方文档 → 源码」的对照阅读路径：看到一个指令名或一串操作码，能在手册、指令表、源码三处之间快速互相定位。
- 独立完成一次「指令清点」：把源码实际实现的指令分组列出来，再与官方资料核对，标注出命名差异或待确认项。

## 2. 前置知识

本讲承接 [u1-l1 项目总览]，那里我们建立了坐标系：IKA32010 是用 SystemVerilog 写的 TI TMS32010 DSP 软核，端口名即文档（`i_`/`o_` 表方向、`_n` 表低有效），并由顶层微码统一调度。本讲不再重复这些结论，而是把镜头推进到「文件级」。

阅读本讲前，建议先了解几个 SystemVerilog/Verilog 的最小概念（不熟也没关系，下面会随用随解释）：

- **module（模块）**：一段可复用的硬件描述，有端口（输入/输出）和内部逻辑。本项目的核心 `module IKA32010` 就是那颗 DSP 的「外壳」。
- **`` `include "xxx.sv" ``**：编译期的**纯文本包含**。它会把指定文件的内容原封不动地「贴」到当前位置，就像 C 语言的 `#include`。这是理解「多文件如何变成一个设计」的关键。
- **localparam / parameter**：常量。用名字代替魔法数字，让代码可读。例如用 `ALU_ADD` 代替 `3'd4`。
- **casez**：带「不关心位 `?`/`z`」的多分支选择语句，本项目的指令译码就是靠它做的。
- **testbench（测试平台）**：一个只为仿真存在、不会被综合成真实电路的顶层模块，用来给被测模块喂时钟、复位和激励。

## 3. 本讲源码地图

整个仓库的目录很精简，根目录只有 `LICENSE`、`README.md` 和两个子目录：

| 路径 | 类型 | 作用 |
| --- | --- | --- |
| `LICENSE` | 文本 | BSD 2-Clause 许可证。 |
| `README.md` | 文本 | 项目说明、模块实例化示例、编译选项、FPGA 资源占用。 |
| `src/IKA32010.sv` | SystemVerilog 源码 | **主文件/编译入口**。含顶层模块 `IKA32010`（含微码）和 4 个子模块（ALU/RAM/Stack/Multiplier），共约 2000 行。 |
| `src/IKA32010_mnemonics.sv` | SystemVerilog 源码 | 常量定义：PC 控制、写总线源、总线事务、ALU 模式等所有 `localparam`。 |
| `src/IKA32010_disasm.sv` | SystemVerilog 源码 | 反汇编打印函数 `disasm_type0`~`disasm_type6`，仿真时把执行的指令打印到控制台。 |
| `src/IKA32010_tb.v` | Verilog 源码 | testbench，产生时钟/复位/中断激励并加载程序 ROM。 |
| `docs/TMS32010_Users_Guide_1985.pdf` | PDF（约 13 MB） | **权威参考**：TI 1985 年官方《TMS32010 User's Guide》。 |
| `docs/TMS320C1X.PDF` | PDF（约 1.1 MB） | TMS320C1x 系列（含 32010 内核）补充手册。 |
| `docs/opcode table.xlsx` | 电子表格（约 45 KB） | 操作码-指令对照表，适合在 Excel/LibreOffice 中打开核对。 |

> 提示：根目录没有 `Makefile`、没有 CI 配置、也没有 `package.json` 之类的构建脚本。这是一个**纯 HDL IP 核**仓库——把它加进你自己的 FPGA 工程里综合即可，没有「一键编译」的概念。这一点和普通软件项目很不一样。

## 4. 核心概念与源码讲解

本讲的两个最小模块是 **`src` 目录** 和 **`docs` 目录**，分别对应「代码怎么组织」和「资料怎么对照」。

### 4.1 src 目录：四个源文件的分工与包含关系

#### 4.1.1 概念说明

一个常见误区是以为「四个源文件 = 四个独立设计」。其实不是。`src/` 里有**一个**真正的编译入口 `IKA32010.sv`，另外两个 `.sv` 文件是被它用 `` `include `` 「贴」进来的片段，而那 4 个硬件子模块（ALU/RAM/Stack/Multiplier）则**物理上就写在 `IKA32010.sv` 这一个文件里**（文件末尾）。

理解这一点后，整个 `src/` 的结构就清楚了：

- **`IKA32010.sv`** 是「主干 + 四肢」。它既包含顶层 `module IKA32010`，也包含 `IKA32010_alu`、`IKA32010_ram`、`IKA32010_stack`、`IKA32010_multiplier` 四个子模块。
- **`IKA32010_mnemonics.sv`** 是「常量字典」。顶层微码里出现的 `PC_INCREASE`、`ALU_ADD`、`WRBUS_SOURCE_RAM` 等名字，全部在这里定义。
- **`IKA32010_disasm.sv`** 是「调试工具」。它只在定义了 `IKA32010_DISASSEMBLY` 宏时才被包含，提供 7 个把指令格式化成字符串并 `$display` 打印的函数。
- **`IKA32010_tb.v`** 是独立的测试平台（注意扩展名是 `.v` 而非 `.sv`），实例化 `IKA32010` 并喂激励，**不被任何文件 include**。

#### 4.1.2 核心流程：文件如何拼成一个设计

把「编译一个 IP 核」想象成「把若干张纸按顺序粘成一卷」。流程是：

```
   仿真器/综合器读入 IKA32010.sv 作为入口
            │
            ▼
   ┌──────────────────────── IKA32010.sv ────────────────────────┐
   │                                                              │
   │  module IKA32010          ← 顶层模块 + 微码 (L1–L1757)       │
   │      │                                                       │
   │      │  `define IKA32010_DISASSEMBLY                         │
   │      │  `include "IKA32010_disasm.sv"     (L37, 条件包含) ──┐ │
   │      │  `include "IKA32010_mnemonics.sv"  (L41, 必定包含) ─┐│ │
   │      │                                                     ││ │
   │  module IKA32010_alu        (L1760–L1906)                  ││ │
   │  module IKA32010_ram        (L1909–L1940)                  ││ │
   │  module IKA32010_stack      (L1943–L1982)                  ││ │
   │  module IKA32010_multiplier (L1985–L2018)                  ││ │
   └────────────────────────────────────────────────────────────┘│ │
                                                                  │ │
   IKA32010_mnemonics.sv ◄──────────────────────── 常量被「贴」到 L41
   IKA32010_disasm.sv    ◄─────────────────────── 函数被「贴」到 L37

   IKA32010_tb.v   独立文件，自己实例化 IKA32010（不参与上面的包含链）
```

要点：

1. **入口只有一个**：你把 `IKA32010.sv` 交给工具，工具沿着 `` `include `` 自动把另外两个 `.sv` 拉进来。你不需要单独「编译」`IKA32010_mnemonics.sv`。
2. **子模块在同一文件**：4 个子模块不需要你去 `src/` 下找别的文件，它们就在 `IKA32010.sv` 的末尾，由顶层通过「模块名实例化」直接调用。
3. **testbench 旁路**：`IKA32010_tb.v` 是给仿真器单独跑的，和上面的包含链无关。

#### 4.1.3 源码精读

**(1) 入口处的两个 `include` —— 整个仓库最关键的几行**

在顶层模块端口声明之后，紧接着就是调试宏定义和两个包含指令：

[src/IKA32010.sv:L33-L42](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L33-L42) —— 这里定义了三个调试宏，并用 `` `include `` 把反汇编文件和常量文件「贴」进来。

```verilog
`define IKA32010_DISASSEMBLY
`define IKA32010_DISASSEMBLY_SHOWID
`define IKA32010_DEVICE_ID "ikakawa"
`ifdef IKA32010_DISASSEMBLY
`include "IKA32010_disasm.sv"
`endif

//include mnemonic list
`include "IKA32010_mnemonics.sv"
```

读这段可以得出几件事：

- `IKA32010_disasm.sv` 被 `` `ifdef IKA32010_DISASSEMBLY `` 包住，是**可选**的——关掉这个宏，反汇编就完全不参与编译，省资源。这也呼应了 README 里说的「This requires the `IKA32010_disasm.sv` file」。
- `IKA32010_mnemonics.sv` 是**无条件包含**的，说明顶层微码「每时每刻都依赖」这些常量。
- 三个宏（`DISASSEMBLY` / `DISASSEMBLY_SHOWID` / `DEVICE_ID`）正是 README「Compilation options」一节介绍的开关，`DEVICE_ID` 在多片 DSP 系统里用来区分设备。

**(2) 常量字典 `IKA32010_mnemonics.sv`**

整个文件只有 63 行，全部是 `localparam`，按用途分组、每组带注释。它就是顶层微码的「词汇表」：

[src/IKA32010_mnemonics.sv:L1-L18](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L1-L18) —— PC 控制模式与写总线数据源的常量定义。

```verilog
//Program counter control
localparam  PC_HOLD             = 3'd0;
localparam  PC_INCREASE         = 3'd1;
localparam  PC_LOAD_IMMEDIATE   = 3'd2;
...
//Write bus sources
localparam  WRBUS_SOURCE_SHB     = 3'd0;
localparam  WRBUS_SOURCE_RAM     = 3'd1;
...
```

以后只要在微码里看到陌生的全大写名字（例如 `BUSCTRL_STOP`、`ALU_SUBC`、`STACK_DATA_PC`），第一反应就是回这个文件查它的数值和分组含义，不要去猜。

**(3) 调试工具 `IKA32010_disasm.sv`**

这个文件定义了 7 个函数 `disasm_type0`~`disasm_type6`，每个负责格式化一组指令格式（因为不同指令的操作数编码不同，需要不同的打印方式）。每个函数的套路一样：拼出 `PC=0x??? | 助记符 操作数` 的字符串再 `$display` 打印。以最简单的 type0（无操作数指令，如 NOP）为例：

[src/IKA32010_disasm.sv:L7-L21](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L7-L21) —— 把指令格式化成 `PC=0x... | <助记符>` 并打印。

```verilog
function void disasm_type0;
    input   string  mnemonic;
    input   [11:0]  pc;
    ...
    $sformat(num_data, " PC=0x%3h |", {pc-1}[11:0]);
    disasm = {disasm, num_data};
    disasm = {disasm, " ", mnemonic};
    ...
    if(pc_z != pc) $display(disasm);
    pc_z = pc;
endfunction
```

两个细节值得记住（后面看仿真日志会用到）：打印的 PC 是 `pc-1`，因为取指后 PC 已前进；变量 `pc_z` 用来去重，同一个 PC 只打印一次（多周期指令会多次进入同一分支）。

**(4) 4 个子模块都在主文件末尾**

不需要到别处找，它们就在 `IKA32010.sv` 的末尾，依次排列：

| 子模块 | 行范围 | 一句话作用 |
| --- | --- | --- |
| [src/IKA32010.sv:L1760-L1778](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1760-L1778) `IKA32010_alu` | L1760–L1906 | 七种运算（AND/OR/XOR/ABS/ADD/SUB/SUBC）+ 32 位累加器 + Z/N/V 标志。 |
| [src/IKA32010.sv:L1909-L1917](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1909-L1917) `IKA32010_ram` | L1909–L1940 | 256×16 双口 RAM，含 DMOV 数据搬移逻辑。 |
| [src/IKA32010.sv:L1943-L1953](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1943-L1953) `IKA32010_stack` | L1943–L1982 | 4 级、12 位硬件堆栈，push/pop/hold。 |
| [src/IKA32010.sv:L1985-L1994](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1985-L1994) `IKA32010_multiplier` | L1985–L2018 | 16×16 有符号乘法（T/P 寄存器），会被综合成 FPGA 的 DSP 块。 |

它们的具体实现会在进阶层（u2）逐个精读，这里你只需要知道「位置在哪、端口长什么样」即可。

**(5) 顶层微码：把一切串起来的地方**

`IKA32010.sv` 第 537 行起有一个巨大的 `always @(*)` 组合块，它先给所有控制信号赋「默认值」，再用 `casez(if_opcodereg)` 按当前指令逐条覆盖——这就是本核的「微码」。指令分组用注释横幅标了出来：

[src/IKA32010.sv:L537-L543](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L537-L543) —— 微码块入口：先给总线请求和 PC 模式赋默认值。

[src/IKA32010.sv:L618-L643](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L618-L643) —— `casez(if_opcodereg)` 的开头：先是内部特殊指令 IACK，再是第一条真正的控制类指令 NOP。

你不必现在读懂它，只要建立印象：**源码自己用注释把指令分成了 CONTROL / ACCUMULATOR / AUXILLARY REGISTER / BRANCH 等若干组**（横幅见 L621、L772、L1089、L1190），这正是 4.2 节做「指令清点」时的天然入口。

#### 4.1.4 代码实践：追踪 `include` 包含链

**实践目标**：亲手验证「四个文件谁包含谁」的关系，而不是只看上面的图。

**操作步骤**：

1. 打开 `src/IKA32010.sv`，跳到第 33–42 行，确认有且仅有两条 `` `include ``（一条条件、一条无条件）。
2. 打开 `src/IKA32010_mnemonics.sv`，数一下它一共有多少个 `localparam`/`parameter`，并记下它们分成几组（看注释）。
3. 打开 `src/IKA32010_disasm.sv`，数一下有几个 `function`，它们的名字是不是 `disasm_type0` 到 `disasm_type6`。
4. 在 `IKA32010.sv` 里跳到第 1760、1909、1943、1985 行，确认这 4 个 `module` 都在同一个文件里。
5. 打开 `src/IKA32010_tb.v`，确认它**没有**被任何文件 `` `include ``，而是自己用 `IKA32010 main (...)` 实例化了被测模块（第 30 行附近）。

**需要观察的现象**：

- 步骤 1 应看到 `IKA32010_disasm.sv` 被 `` `ifdef IKA32010_DISASSEMBLY ... `endif `` 包住，而 `IKA32010_mnemonics.sv` 没有。
- 步骤 4 应看到 4 个子模块都在主文件内，验证「子模块不是独立文件」。
- 步骤 5：testbench 里 `i_CLKIN_PCEN` 连的是 `~cen_n`（由分频器产生），这正是 u1-l1 讲过的「PCEN 相位脉冲」的具体来源。

**预期结果**：你能向别人画出 4.1.2 节那张包含关系图，并解释为什么综合时只需把 `IKA32010.sv` 作为顶层文件加进工程。

> 一个真实但需注意的细节（**待本地验证**）：`IKA32010_tb.v` 第 63、82–83 行用 `$readmemh` 加载的 ROM 路径是 `D:/PROCESSOR/IKA32010/IKA32010/rom/*.txt`（作者本机的 Windows 路径），本仓库并不附带这些 ROM 文件。所以**直接跑这个 testbench 会报找不到文件**。这一现象会在 [u1-l5 仿真与 testbench] 里详细处理，这里只需知道「tb 引用了仓库外的外部文件」。

#### 4.1.5 小练习与答案

**练习 1**：如果不想要反汇编日志、想省一点 FPGA 资源，应该改哪里？为什么删掉 `IKA32010_disasm.sv` 文件本身不够？

> **答案**：注释掉 `IKA32010.sv` 第 33 行的 `` `define IKA32010_DISASSEMBLY ``（或改成在工程级不定义该宏）。只删文件不够，因为第 36–38 行的 `` `ifdef `` 会保护它——宏开着时 `` `include `` 还在，删了文件反而会让包含找不到目标而报错；宏关掉后整个反汇编块才真正不参与编译。

**练习 2**：微码里出现 `alu_modesel = ALU_SUBC;`，`ALU_SUBC` 的值是多少？在哪个文件定义的？

> **答案**：`ALU_SUBC = 3'd6`，定义在 `IKA32010_mnemonics.sv` 第 47 行（"ALU mode" 注释组内）。

**练习 3**：为什么 `IKA32010_tb.v` 的扩展名是 `.v` 而不是 `.sv`？

> **答案**：它用的是传统 Verilog-2001 风格（`reg`/`always`/`$readmemh`），不依赖 SystemVerilog 特性，写成 `.v` 表明它可以在纯 Verilog 仿真器上跑。而被测核 `IKA32010.sv` 用了 `string`、`signed'()` 等 SystemVerilog 特性，必须是 `.sv`。

### 4.2 docs 目录：官方手册与指令表作为权威参考

#### 4.2.1 概念说明

IKA32010 是对一颗**真实存在过的芯片**（TI 1983/1985 年的 TMS32010）的复刻。这意味着：**芯片官方手册是语义的最终裁判**。当源码行为和你的直觉冲突时，错的几乎总是直觉，要去翻手册；而 `docs/` 目录就是为了让你不必去网上找资料，把三份权威文档直接放在手边：

- **`TMS32010_Users_Guide_1985.pdf`**：主角。TI 官方《TMS32010 User's Guide》，定义了架构、寄存器、每条指令的语义和时序、电气特性。源码注释里多处直接引用它的页码（见 4.2.3）。
- **`opcode table.xlsx`**：操作码-指令对照表。把「16 位操作码的二进制位段」和「指令助记符」整理成表格，最适合用来核对源码 `casez` 里那一串串 `16'b...` 到底对应哪条指令。
- **`TMS320C1X.PDF`**：TMS320C1x 系列（C14/C15/…，共享 32010 内核）补充手册，提供系列级背景，遇到手册主本讲不清的细节时可作旁证。

一句话定位：**讲不清语义翻 PDF，对不上操作码翻 xlsx，想了解家族背景翻 C1X。**

#### 4.2.2 核心流程：从「一个疑问」到「定位答案」

把「对照阅读」做成一条可重复的流水线。假设你想搞清楚「`MAR` 指令到底改了什么」：

```
   ① 在源码 casez 里看到某条指令
            │
            ▼
   ② 打开 docs/opcode table.xlsx，用操作码位段确认指令名
            │
            ▼
   ③ 打开 docs/TMS32010_Users_Guide_1985.pdf，按指令名查语义/时序
            │
            ▼
   ④ 回到源码，把手册描述的控制信号与微码实现逐行对照
            │  （源码注释经常直接写出 PDF 页码，可跳过 ②③ 直接 ①→④）
            ▼
   ⑤ 在 testbench/仿真日志里用 disasm 输出验证理解
```

关键技巧：**源码里经常埋着指向手册的「路标」**，让你不必每次都从操作码反查。下节会给出一个真实例子。

#### 4.2.3 源码精读

**(1) 源码注释直接引用手册页码 —— 最省力的对照入口**

辅助寄存器自增/自减的实现处，注释直接写出了手册章节和 PDF 页码：

[src/IKA32010.sv:L322-L325](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L322-L325) —— 注释「see page 2-9 of the user manual(pdf p32)」把源码和官方手册精确关联起来。

```verilog
2'b01: reg_ar[reg_arp][8:0] <= reg_ar[reg_arp][8:0] - 9'd1; //see page 2-9 of the user manual(pdf p32)
2'b10: reg_ar[reg_arp][8:0] <= reg_ar[reg_arp][8:0] + 9'd1; //see page 2-9 of the user manual(pdf p32)
```

这就告诉你：AR 的增减只动低 9 位，依据是手册「page 2-9」（PDF 第 32 页）。**遇到带这类注释的行，优先翻到那一页对照读，效率最高。**

**(2) 源码自带的指令分组横幅 —— 清点的天然目录**

微码块里用大写注释把指令分了组，这几行就是「指令清点」的目录页：

| 行号 | 横幅注释 |
| --- | --- |
| [src/IKA32010.sv:L621](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L621) | `//  CONTROL INSTRUCTIONS` |
| [src/IKA32010.sv:L772](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L772) | `//  ACCUMULATOR INSTRUCTIONS` |
| [src/IKA32010.sv:L1089](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1089) | `//  AUXILLARY REGISTER AND DATA POINTER INSTRUCTIONS` |
| [src/IKA32010.sv:L1190](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1190) | `//  BRANCH INSTRUCTIONS` |

> 顺带提一句：横幅里 `AUXILLARY` 是源码里的拼写（标准拼写是 Auxiliary）。这类笔误在阅读时要心里有数，不影响功能。横幅之后还跟着乘法器类、I/O 与数据搬移类指令（它们没有单独的横幅，但紧随分支类之后）。

**(3) 一个真实的「命名不一致」案例：SSR vs SST**

这是「为什么要对照官方资料」最好的例子。源码里这条指令叫 `SSR`：

[src/IKA32010.sv:L752-L766](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L752-L766) —— 源码把它命名为 `SSR`，注释为「Store status register」，操作码 `16'b0111_1100_????_????`。

```verilog
//SSR - Store status register
16'b0111_1100_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_FLAG;
    ram_wr = YES;
    ...
    disasm_type2("SSR", if_opcodereg, if_pc, 0, 0);
```

而在 TI 官方手册里，同一操作码的指令标准助记符是 **`SST`**（Store Status register Temporary）。也就是说：

- 功能上：源码 `SSR` = 官方 `SST`（把状态寄存器存到数据 RAM）。
- 名字上：源码用了自己的缩写，和官方不一致。

这种差异只有「对照阅读」才会发现。它不是 bug，但你必须知道，否则拿着官方汇编程序/资料来对源码时会找不到 `SST`。类似的还有 `MAR`，源码拆成了 `MAR(LARP)` 与 `MAR(NOP)` 两种编码（见 L1119、L1160），对应官方 `MAR` 的不同寻址形式。

#### 4.2.4 代码实践：源码指令清点 + 与官方资料核对

这是本讲的主实践，**只读不改**。

**实践目标**：列出源码实际实现了哪些指令、分属哪组，再与 `docs/` 核对，标出命名差异或待确认项。

**操作步骤**：

1. **源码侧清点（已为你做完，请自行复核）**。我在源码里搜索了所有 `disasm_typeX("助记符", …)` 调用，得到下表（括号内是该指令在 `IKA32010.sv` 里出现的大致行号，供你定位）：

   | 分组（源码横幅） | 实现的指令 |
   | --- | --- |
   | Control（L621） | `NOP` `DINT` `EINT` `LST` `POP` `PUSH` `ROVM` `SOVM` `SSR`（另有内部特殊指令 `IACK`，非用户指令，L625） |
   | Accumulator（L772） | `ABS` `ADD` `ADDH` `ADDS` `AND` `LAC` `LACK` `OR` `SACH` `SACL` `SUB` `SUBC` `SUBH` `SUBS` `XOR` `ZAC` `ZALH` `ZALS` |
   | Aux Reg & Data Pointer（L1089） | `LAR` `LARK` `LDP` `LDPK` `SAR` `MAR`（含 `MAR(LARP)`、`MAR(NOP)` 两种编码） |
   | Branch（L1190） | `B` `BANZ` `BGEZ` `BGZ` `BIOZ` `BLEZ` `BLZ` `BNZ` `BV` `BZ` `CALA` `CALL` `RET` |
   | （横幅之后）乘法器类 | `APAC` `LT` `LTA` `LTD` `MPY` `MPYK` `PAC` `SPAC` |
   | （横幅之后）I/O 与数据搬移 | `DMOV` `IN` `OUT` `TBLR` `TBLW` |

2. **打开 `docs/opcode table.xlsx`**（用 Excel/LibreOffice Calc）。把上表里的指令逐个在该表里找到，记下它的**操作码位段**（即 `casez` 里 `16'b....` 那串），核对源码分支里的二进制是否与表一致。

3. **打开 `docs/TMS32010_Users_Guide_1985.pdf`**，翻到「Instruction Set Summary / 指令集总表」一节（通常在手册前部）。把官方指令清单和上表对齐，重点记录：
   - 官方有、源码表里**没有**的指令 → 标「待确认：是否未实现」。
   - 名字对不上但功能相同的（已知：`SSR`↔官方 `SST`）→ 标「命名差异」。

**需要观察的现象 / 待本地验证**：

- 我无法在本环境里打开二进制的 `.xlsx` 和 13 MB 的 PDF，因此「官方清单里是否还有源码未实现的指令」这一项**待本地验证**。请你亲自打开两份文档完成核对。
- 一个**已知**的命名差异：源码 `SSR` 对应官方 `SST`（见 4.2.3）。
- 一个**已知**的实现差异：`MAR` 在源码里被拆成 `MAR(LARP)`/`MAR(NOP)` 两种编码（L1119、L1160），对应官方 `MAR` 的不同寻址形式，请在 xlsx 里确认这两种编码都合法。

**预期结果**：得到一张三列表格——`源码助记符 | 官方助记符 | 操作码位段`，其中绝大多数行两列名字相同；少数行（如 `SSR`/`SST`）名字不同但功能一致；如有官方存在而源码缺失的指令，单独列出并标「待确认」。

> 说明：本实践是「源码阅读 + 文档对照」型实践，不运行仿真。真正运行指令、看波形/反汇编日志的实践放在 [u1-l5]。

#### 4.2.5 小练习与答案

**练习 1**：你想知道「`SUBC` 条件减法是怎么实现除法的」，应该按什么顺序翻资料？

> **答案**：先在 `IKA32010.sv` 搜 `SUBC` 看微码（L980 附近）和 ALU 子模块里 `subc_divided/prev_subc` 的处理（L1793–L1872），建立直觉；再翻 `TMS32010_Users_Guide_1985.pdf` 的 SUBC 条目读官方给出的除法步骤与示例；最后回源码逐行对应。顺序是「源码直觉 → 手册权威 → 源码对照」。

**练习 2**：`opcode table.xlsx` 和 PDF 各自更适合回答什么问题？

> **答案**：xlsx 适合回答「这串 `16'b...` 操作码是哪条指令」「这条指令的操作数位段怎么切」这类**编码/查表**问题；PDF 适合回答「这条指令语义是什么、影响哪些标志、占几个周期」这类**语义/时序**问题。

**练习 3**：在源码里看到注释 `//see page 2-9 of the user manual(pdf p32)`，`page 2-9` 和 `pdf p32` 为什么是两个不同的页号？

> **答案**：`page 2-9` 是手册自己的「章节-页」编号（第 2 章第 9 页），`pdf p32` 是这份 PDF 文件在阅读器里的物理页码。两者通常不一致，注释同时给出是为了你用任一种导航方式都能快速跳到同一处。

## 5. 综合实践：给仓库画一张「导航地图」

把本讲两节合起来，完成一份属于你自己的「仓库导航地图」（一个 Markdown 或纸笔笔记即可）：

1. **文件关系图**：画出 `src/` 四个文件的包含/实例化关系（参考 4.1.2 的图），并在每个文件旁用一句话写它的职责。
2. **文档定位表**：列一张「问题类型 → 该翻哪份 docs」的表（语义翻 PDF、操作码翻 xlsx、家族背景翻 C1X）。
3. **指令清点表**：把 4.2.4 的清点结果整理进去，并至少标注一处命名差异（`SSR`/`SST`）和一处待确认项。
4. **一条对照链**：自选一条指令（建议 `LTD` 或 `TBLR`），完整走一遍 4.2.2 的五步流水线：源码分支 → xlsx 操作码 → PDF 语义 → 源码逐行对照 → 想象在 disasm 日志里看到的样子。

完成后，你就拥有了快速「定位任何行为」的能力——这是后续所有源码精读讲义的基础。

## 6. 本讲小结

- `src/` 只有一个编译入口 `IKA32010.sv`；它用 `` `include `` 拉进 `IKA32010_mnemonics.sv`（常量，必定包含）和 `IKA32010_disasm.sv`（反汇编，条件包含）。
- 4 个硬件子模块（ALU/RAM/Stack/Multiplier）**物理上写在 `IKA32010.sv` 末尾**，不是独立文件；`IKA32010_tb.v` 是旁路的独立 testbench。
- `IKA32010_mnemonics.sv` 是常量字典——看到陌生的大写名字就回这里查；`IKA32010_disasm.sv` 提供 7 个 `disasm_typeX` 函数把执行的指令打印成 `PC=0x... | 助记符 操作数`。
- `docs/` 三份资料分工明确：PDF 是语义权威、xlsx 是操作码查表、C1X 是家族背景；源码注释经常直接给出 PDF 页码（如 L322–L325），是最高效的对照入口。
- 源码用注释横幅（L621/L772/L1089/L1190）把指令分成 Control/Accumulator/Aux Reg/Branch 等组，是「指令清点」的天然目录。
- 对照阅读能发现命名差异：源码 `SSR` 对应官方 `SST`；`MAR` 被拆成 `MAR(LARP)`/`MAR(NOP)` 两种编码。

## 7. 下一步学习建议

- 想看「这颗核对外暴露哪些引脚、每个引脚什么极性」→ 下一讲 [u1-l3 顶层模块端口与引脚定义]。
- 想搞懂「4 个 EMUCLK 怎么变成 1 个 DSP 周期、PCEN/NCEN 是什么」→ [u1-l4 时钟分频与周期计数器]。
- 想动手跑仿真、看反汇编日志如何打印 PC 与指令 → [u1-l5 仿真与 testbench 入门]（届时会处理 tb 引用仓库外 ROM 路径的问题）。
- 之后再进入进阶层 u2，按「数据通路」逐个精读 4 个子模块。
