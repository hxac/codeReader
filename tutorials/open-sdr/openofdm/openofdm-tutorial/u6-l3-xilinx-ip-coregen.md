# Xilinx IP core 与 coregen 依赖

## 1. 本讲目标

OpenOFDM 的解码流水线里，最重、最复杂的几个运算单元——FFT、卷积解码（Viterbi）、复数乘法器、除法器，以及一堆查找表（相位表、旋转表、解交织表）——**都不是手写 RTL**，而是直接调用 Xilinx 提供的 IP 核（黑盒）。它们集中放在 `verilog/coregen/` 目录下，由 Xilinx 的 **CORE Generator**（简称 coregen）工具生成。

本讲的读者在 u1-l3 已经知道仓库源码分三类（手写 RTL / Xilinx IP 与库 / USRP 平台代码），在 u3-l5 已经知道 Viterbi 是一个加上了 strobe 翻译的 IP 封装，在 u5-l4 已经知道查找表由 Python 脚本离线生成 `.coe/.mif`。本讲要回答的核心问题是：

> 这些 IP 核到底是什么东西？一个 IP 在磁盘上为什么会有一堆后缀各异的文件（`.v` / `.vho` / `.veo` / `.ngc` / `.xco` / `.mif`）？我们没有 Xilinx 综合工具链，为什么还能用 iverilog 把整个解码器仿真起来？FFT 到底是哪个 IP 干的？

学完本讲你应该能够：

- 读懂 `dot11_modules.list` 的四段结构，并准确说出每一行属于「手写 RTL / USRP 平台模块 / coregen IP / Xilinx 库」哪一类。
- 区分一个 coregen IP 的 `.xco`、`.ngc`、`.v`、`.veo`、`.vho`、`.mif/.coe` 各自的作用，知道哪些给综合用、哪些给仿真用。
- 识别 coregen 输出的**两种 `.v` 风格**：netgen 扁平网表（计算型 IP）与 CORE Generator 行为封装（查找表 ROM），并理解它们最终都依赖 Xilinx 仿真库。
- 在解码链路中定位 `xfft_v7_1`、`viterbi_v7_0`、`div_gen_v3_0`、`complex_multiplier`、三个查找表各自的实例化位置与角色，并指出 FFT 由哪个 IP 承担。

## 2. 前置知识

在进入源码前，先用通俗语言把几个 Xilinx 工具链的术语讲清楚。

- **IP 核（IP core）**：一段可复用、参数化的硬件功能块，由芯片厂商（这里是 Xilinx）提供。你可以把它理解成「现成的集成电路，但用 Verilog 描述」。OpenOFDM 不自己实现 FFT，而是调用 Xilinx 的 FFT IP。
- **CORE Generator（coregen）**：Xilinx 的图形化工具。你在 GUI 里勾选参数（位宽、点数、流水级数、初始化文件……），它就为你「下料」生成一整套文件。`.xco` 就是这次下料的「配方单」。
- **综合（synthesis）**：把 Verilog 描述翻译成具体 FPGA 芯片上的底层单元（查找表 LUT、触发器 FF、进位链、DSP 块、块 RAM）的过程。Xilinx 的综合工具叫 XST，属于 ISE 工具链。**综合后才得到能上板的网表 `.ngc`**。
- **netgen**：Xilinx 的网表转换工具。它能把综合得到的 `.ngc`（二进制网表）反推成一份「由底层仿真原语组成的 Verilog 文件」，专门供仿真用。本讲你会看到它生成的 `.v` 文件顶部明确写着「This file cannot be synthesized and should only be used with simulation tools」。
- **UNISIM / XilinxCoreLib**：Xilinx 的两套**仿真库**。UNISIM 是芯片底层原语（LUT、FF、进位链 MUXCY/XORCY、DSP48A、块 RAM 等）的行为模型；XilinxCoreLib 是较高层 IP（如块内存生成器 `BLK_MEM_GEN_V4_2`）的行为模型。仿真时，netgen 网表里的 `LUT2`、`FDRE`、`DSP48A` 这些名字必须从 UNISIM 库里找到定义才能跑起来。
- **黑盒（black box）**：综合时告诉工具「这个模块我已经有现成网表 `.ngc` 了，你不要管它内部，直接当成一个不透明的盒子接进去」。coregen 的 `.v` 文件里带有 `synthesis attribute box_type ... is "black_box"` 就是这个意思。

一句话总结：**`.ngc` 是给 Xilinx 综合/上板用的「成品」，`.v` 是给仿真器用的「替身」，两者描述同一个 IP。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/dot11_modules.list](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list) | iverilog 的命令文件（`-c`），列出全部要编译的源文件、Xilinx 库搜索路径 |
| [verilog/coregen/viterbi_v7_0.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v) | Viterbi 卷积解码 IP 的 netgen 仿真模型 |
| [verilog/coregen/xfft_v7_1.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/xfft_v7_1.v) | FFT IP 的 netgen 仿真模型（解码链路里唯一的 FFT） |
| [verilog/coregen/div_gen_v3_0.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/div_gen_v3_0.v) | 除法器 IP 的 netgen 仿真模型 |
| [verilog/coregen/complex_multiplier.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/complex_multiplier.v) | 复数乘法器 IP 的 netgen 仿真模型（基于 DSP48A 硬核） |
| [verilog/coregen/atan_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v) | atan 查找表的 CORE Generator 行为封装（单口 ROM） |
| [verilog/coregen/rot_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v) | 旋转因子查找表的行为封装（双口 ROM） |
| [verilog/coregen/deinter_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v) | 解交织查找表的行为封装（单口 ROM，2048×22） |
| [verilog/coregen/atan_lut.xco](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.xco) | atan_lut 的 coregen 配方单（参数、器件、存储类型） |
| [verilog/coregen/atan_lut.veo](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.veo) | Verilog 实例化模板（可粘贴进自己 RTL 的片段） |
| [verilog/Makefile](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile) | 用 `iverilog -c dot11_modules.list` 编译 |
| [docs/source/verilog.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/verilog.rst) | 文档：解释为何大量使用量化与查找表（Verilog Hacks） |

## 4. 核心概念与源码讲解

### 4.1 iverilog 命令文件：dot11_modules.list 的四段结构

#### 4.1.1 概念说明

`dot11_modules.list` 不是 Verilog 源码，而是给 iverilog 的**命令文件**（command file）。iverilog 用 `-c` 接受它，[Makefile](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile) 里 `COMPILER_FLAGS = -v -c $(MODULE_LIST) ...` 就是这么用的。命令文件里每一行要么是一个**源文件路径**，要么是一个以 `-` 开头的**选项**（典型的是 `-y` 库搜索路径）。它的作用是把「要编译哪些文件、遇到不认识的模块名去哪里找」一次性讲清楚，免去在命令行上列一长串文件名。

理解这份清单，是理解「OpenOFDM 怎么把 Xilinx IP 拼进自研 RTL」的入口。

#### 4.1.2 核心流程

这份文件天然分成四段，从上到下依次是：

1. **Xilinx 库搜索路径**（`-y`）：告诉 iverilog「遇到不认识的模块名，去这两个目录里找同名 `.v`」。
2. **手写 RTL**：作者自写的解码逻辑源文件。
3. **USRP 平台模块**：`usrp2/` 下的胶水模块。
4. **coregen IP 的 `.v` 仿真模型** + 一个显式列出的 Xilinx 原语文件。

注意：测试台 `dot11_tb.v` **不在这份清单里**——它由 Makefile 在命令行末尾单独追加（`$(TESTBENCH)`），这与 u1-l3 的结论一致。

#### 4.1.3 源码精读

**第一段：Xilinx 库搜索路径**

[dot11_modules.list:1-2](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L1-L2) 这两行是 `-y` 选项，指向 Xilinx ISE 安装目录下的 `unisims/` 与 `XilinxCoreLib/` 两个仿真库目录：

```
-y ./Xilinx/12.2/ISE_DS/ISE/verilog/src/unisims/
-y ./Xilinx/12.2/ISE_DS/ISE/verilog/src/XilinxCoreLib/
```

> ⚠️ **重要事实**：仓库里**并没有** `Xilinx/` 这个目录——这两个路径在你本机必须真实存在（随 Xilinx ISE WebPACK 安装而来）。这是「无 Xilinx 工具链也能仿真」这句话里最容易被忽略的一层细节，本讲 4.2 节会专门讲它。

**第二段：手写 RTL**

[dot11_modules.list:4-32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L4-L32) 从顶层 `dot11.v` 开始，列出了全部自写的解码模块（`sync_short.v`、`sync_long.v`、`ofdm_decoder.v`、`equalizer.v`、`demodulate.v`、`deinterleave.v` 等），以及辅助原语（`delayT.v`、`crc32.v`、`rate_to_idx.v`）。这些就是 u1-l3 所说的「逐行精读对象」。

**第三段：USRP 平台模块**

[dot11_modules.list:33-34](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L33-L34) 是 `usrp2/setting_reg.v` 与 `usrp2/ram_2port.v`，即 u4-l4 讲过的配置寄存器原语与双口 RAM 胶水模块。

**第四段：coregen IP + 一个 Xilinx 原语**

[dot11_modules.list:36-42](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L36-L42) 把 7 个 coregen IP 的 `.v` 仿真模型显式列进来：

```
./coregen/xfft_v7_1.v          // FFT
./coregen/complex_multiplier.v // 复数乘法器
./coregen/viterbi_v7_0.v       // Viterbi 卷积解码
./coregen/div_gen_v3_0.v       // 除法器
./coregen/deinter_lut.v        // 解交织查找表 ROM
./coregen/atan_lut.v           // atan 查找表 ROM
./coregen/rot_lut.v            // 旋转因子查找表 ROM
```

[dot11_modules.list:44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L44) 还显式列了一个 `./Xilinx/.../unisims/MULT18X18S.v`，它是 UNISIM 库里的 18×18 乘法器原语（被 DSP48A 的仿真模型间接需要）。单独列出来是因为 `-y` 路径在本机可能不存在，作者把这一个关键原语点名拉进来，提高仿真成功率。

#### 4.1.4 代码实践

**实践目标**：亲手把 `dot11_modules.list` 的每一行归类，建立「编译到底吃进哪些文件」的直觉。

**操作步骤**：

1. 打开 `verilog/dot11_modules.list`，为每一行加上行内注释，标注它属于下面哪一类：
   - `LIB`：`-y` 库搜索路径（L1-2）
   - `RTL`：手写 Verilog（L4-32）
   - `PLAT`：USRP 平台模块（L33-34）
   - `IP`：coregen IP 仿真模型（L36-42）
   - `PRIM`：显式列出的 Xilinx 原语（L44）
2. 统计每类的行数，填一张表。

**需要观察的现象 / 预期结果**：四段划分清晰可数；测试台 `dot11_tb.v` 不在其中。

| 类别 | 行 | 数量 |
|------|-----|------|
| LIB | 1-2 | 2 |
| RTL | 4-32 | 27 |
| PLAT | 33-34 | 2 |
| IP | 36-42 | 7 |
| PRIM | 44 | 1 |

> （上述归类基于当前 HEAD 的清单文件；实际运行 `make compile` 是否成功取决于本机 Xilinx 仿真库路径，**待本地验证**。）

#### 4.1.5 小练习与答案

**Q1**：为什么 `dot11_modules.list` 里有些文件带 `./` 前缀（如 `./coregen/xfft_v7_1.v`），有些却不带（如 `dot11.v`）？

**答案**：带 `./` 的是相对当前目录（`verilog/`）的显式路径，确保 iverilog 精确命中；不带前缀的裸文件名，iverilog 会按当前工作目录解析。两者本质都是「源文件」，只是写法不同；而 `-y` 开头的行不是源文件，是「找不到模块时去搜的目录」。

**Q2**：如果删除 [dot11_modules.list:44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L44) 这一行，最可能出什么问题？

**答案**：`MULT18X18S` 原语的仿真模型若既不在 `-y` 路径里、又没被显式列出，iverilog 会把它当成未定义模块，导致 `complex_multiplier`（DSP48A 内部用到的乘法原语）仿真行为不正确或报 unknown module。这一行是「双保险」。

---

### 4.2 coregen IP 封装：一个 IP 的文件族与两种 .v 风格

#### 4.2.1 概念说明

打开 `verilog/coregen/` 目录，你会看到**每一个 IP 都对应一整套后缀各异的文件**。初学者最容易懵：到底哪个才是「真」的？答案是：**它们是同一个 IP 的不同「视图」**，分别服务于综合、仿真、原理图、不同语言（Verilog/VHDL）。

更要紧的是：这 7 个 IP 的 `.v` 文件其实有**两种截然不同的内部风格**——一种是由 netgen 把综合网表「摊平」成底层原语的扁平网表，另一种是 CORE Generator 直接生成的、调用高层行为模型的薄封装。分清这两种风格，才能理解「为什么仿真需要 Xilinx 仿真库」。

#### 4.2.2 核心流程

**先看一个 IP 的文件族**（以 `atan_lut` 为例）：

| 后缀 | 含义 | 用途 |
|------|------|------|
| `.xco` | coregen 配方单（文本，GUI 参数） | 用 coregen 重新生成 IP 的输入 |
| `.ngc` | 综合后网表（二进制） | Xilinx 综合/上板用 |
| `.v` | Verilog **仿真模型** | iverilog 仿真用（本讲主角） |
| `.veo` | Verilog **实例化模板** | 拷贝粘贴到自己 RTL 里 |
| `.vho` | VHDL 实例化模板 | VHDL 设计用 |
| `.vhd` | VHDL 仿真模型 | VHDL 仿真用 |
| `.coe` / `.mif` | 存储初始化数据 | LUT 类 IP 的表内容（u5-l4 生成） |
| `.ncf` | 约束文件 | 时序/布局约束（本仓库为空） |
| `.asy` / `.sym` | 原理图符号 | 原理图编辑器用 |
| `.xise` / `.gise` | ISE 工程状态 | ISE GUI 用 |
| `_xmdf.tcl` / `_flist.txt` | 元数据 / 文件清单 | 工具链内部用 |

**两种 `.v` 风格的判别流程**：

```
打开 coregen/*.v
  ├─ 顶部是 netgen 头注释（Command: ... -sim ... .ngc .v）？
  │     且 Purpose 写 "verification model ... cannot be synthesized"？
  │     └─ 是 → 风格A：netgen 扁平网表（计算型 IP）
  │           内部是大量 LUT/FF/进位链/DSP48A 原语，体积数 MB
  │
  └─ 否，顶部是 Xilinx 版权 + "reference the XilinxCoreLib" 提示？
        且 body 被包在 // synthesis translate_off ... translate_on 之间？
        └─ 是 → 风格B：CORE Generator 行为封装（查找表 ROM）
              内部只例化一个 BLK_MEM_GEN_V4_2，体积数 KB
```

#### 4.2.3 源码精读

**风格 A：netgen 扁平网表（计算型 IP）**

[viterbi_v7_0.v:14-27](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v#L14-L27) 的头部把来历交代得很清楚：它由 `netgen` 工具用 `-sim` 模式从一个 `.ngc` 综合网表反推而来，并明确声明「This verilog netlist is a verification model ... This file cannot be synthesized and should only be used with supported simulation tools.」——也就是**只供仿真、不能再拿去综合**。

[viterbi_v7_0.v:36-46](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v#L36-L46) 是它的对外端口（也是整个 IP 唯一需要让上层看懂的接口）：

```verilog
module viterbi_v7_0 (
  sclr, ce, rdy, clk, data_out, erase, data_in0, data_in1
)/* synthesis syn_black_box syn_noprune=1 */;
  input sclr;        // 同步清零
  input ce;          // 时钟使能
  output rdy;        // 输出就绪
  input clk;
  output data_out;            // 解码出的 1 比特
  input [1 : 0] erase;        // 去穿孔空位标志
  input [2 : 0] data_in0;     // 路径0 的 3bit 软判决
  input [2 : 0] data_in1;     // 路径1 的 3bit 软判决
```

这正是 u3-l5 讲过的「3bit 软判决 + erase」接口。端口之后，文件主体是几千个 UNISIM 原语的连线（名字长得像 `\blk00000003/sig00000084`）。统计 viterbi 网表里的原语，可以看到它完全用 FPGA 纯逻辑资源搭出来：

- `LUT2/LUT3/LUT4`（共约 4300 个）：查找表，实现布尔函数；
- `MUXCY/XORCY`（共约 2200 个）：进位链，专做快速加法/比较；
- `FDRE`（48 个）：带复位使能的 D 触发器；
- `MUXF5/MUXF6/MUXF7`（共 86 个）：把多个 LUT 串成更宽的函数。

同属风格 A 的还有 [xfft_v7_1.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/xfft_v7_1.v)、[div_gen_v3_0.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/div_gen_v3_0.v)、[complex_multiplier.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/complex_multiplier.v)。其中 `complex_multiplier` 比较特别：它例化的是 **`DSP48A`** 硬核（Spartan-3A-DSP 专用的乘加单元）而不是一堆 LUT——这也正是 OpenOFDM 必须上「Spartan 3A-DSP」、而不是普通 Spartan 3 的原因（u6-4 讲的 USRP N210 平台）。

**风格 B：CORE Generator 行为封装（查找表 ROM）**

[atan_lut.v:40-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v#L40-L48) 的端口极其简单（一个时钟、一个 8 位地址、一个 9 位数据输出）：

```verilog
module atan_lut(clka, addra, douta);
input clka;
input [7 : 0] addra;     // 256 项
output [8 : 0] douta;    // 每项 9 位
```

它内部只做一件事：[atan_lut.v:50-130](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v#L50-L130) 在 `// synthesis translate_off` 与 `// synthesis translate_on` 之间，例化一个块内存生成器 `BLK_MEM_GEN_V4_2`，并用 `.C_INIT_FILE_NAME("atan_lut.mif")` 把表内容灌进去：

```verilog
// synthesis translate_off
      BLK_MEM_GEN_V4_2 #(
          .C_READ_DEPTH_A(256),
          .C_READ_WIDTH_A(9),
          .C_MEM_TYPE(3),                 // 3 = 单口 ROM
          .C_INIT_FILE_NAME("atan_lut.mif"),
          ...
      ) inst ( .CLKA(clka), .ADDRA(addra), .DOUTA(douta), ... );
// synthesis translate_on

// synthesis attribute box_type of atan_lut is "black_box"
```

这里的两个关键设计意图：

- `translate_off/translate_on`：让**综合器忽略**中间这段（综合时这个模块是黑盒，由 `.ngc` 提供真实实现），而**仿真器执行**这段（用 `BLK_MEM_GEN_V4_2` 这个高层行为模型当替身）。
- `box_type = "black_box"`：再次以综合属性声明它是黑盒，告诉 XST「别试图综合这个模块体」。

`rot_lut` 与 `deinter_lut` 是同样的薄封装，只是存储形态不同：[rot_lut.v:40-54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v#L40-L54) 的端口声明带两套 `(clka,addra,douta)` 与 `(clkb,addrb,doutb)`，本身就是双口的证据；其内部 `BLK_MEM_GEN_V4_2` 参数进一步配成 `C_MEM_TYPE=4`（双口 ROM）、`C_COMMON_CLK=1`，深度 512、宽度 32，这正是 u6-l2 讲过的「一块真双口 BRAM 同时服务 `sync_long` 与 `equalizer`」的物质基础；`deinter_lut` 是单口 ROM（`C_MEM_TYPE=3`、深度 2048、宽度 22，与 u3-l4/u5-l4 的「2048×22 位 ROM」一致）。

**配方单 `.xco` 怎么读**

[atan_lut.xco:21-30](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.xco#L21-L30) 锁定了目标器件 `xc3sd3400a` / `spartan3adsp` / `fg676` / `-5`（与 u6-4 的 USRP N210 Spartan 3A-DSP 对得上）；[atan_lut.xco:35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.xco#L35) 选了 `Block_Memory_Generator`；[atan_lut.xco:54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.xco#L54) 设 `memory_type=Single_Port_ROM`。`.xco` 就是把 GUI 上点的每一个参数以 `CSET ...=...` 的形式落盘，从而可复现。

**实例化模板 `.veo`**

[atan_lut.veo:33-39](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.veo#L33-L39) 给了一段可直接粘贴的 Verilog：

```verilog
//----------- Begin Cut here for INSTANTIATION Template ---// INST_TAG
atan_lut YourInstanceName (
    .clka(clka),
    .addra(addra), // Bus [7 : 0]
    .douta(douta)); // Bus [8 : 0]
```

而项目里真正的实例化在 [phase.v:85-89](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L85-L89)，把 `clock/atan_addr/atan_data` 接进去——和模板几乎一字不差。

#### 4.2.4 代码实践

**实践目标**：用「读头部 + 数原语」的方法，亲手把 7 个 coregen IP 分成两种风格，并理解它们各自依赖哪个 Xilinx 仿真库。

**操作步骤**：

1. 打开 `verilog/coregen/viterbi_v7_0.v`、`xfft_v7_1.v`、`div_gen_v3_0.v`、`complex_multiplier.v`，只看前 30 行头部，确认它们都带 netgen 标志（`-sim -ofmt verilog`、`verification model ... cannot be synthesized`）。
2. 打开 `atan_lut.v`、`rot_lut.v`、`deinter_lut.v`，确认它们头部是版权声明 + 「reference the XilinxCoreLib」，且 body 被包在 `translate_off/translate_on` 中。
3. （可选）对 `viterbi_v7_0.v` 与 `complex_multiplier.v` 分别执行（**示例命令，待本地验证**）：
   ```
   grep -cE 'LUT[2346]|MUXCY|XORCY|FDRE|FDCE' verilog/coregen/viterbi_v7_0.v
   grep -cE 'DSP48A' verilog/coregen/complex_multiplier.v
   ```

**需要观察的现象 / 预期结果**：

| IP | 风格 | 仿真依赖的库 | 内部主要原语 |
|----|------|--------------|--------------|
| viterbi_v7_0 | A（netgen） | unisims | LUT/进位链/FF/MUXF |
| xfft_v7_1 | A（netgen） | unisims | LUT/FF/RAMB… |
| div_gen_v3_0 | A（netgen） | unisims | LUT/进位链/FF |
| complex_multiplier | A（netgen） | unisims | **DSP48A**（×3） |
| atan_lut | B（封装） | XilinxCoreLib | BLK_MEM_GEN_V4_2 |
| rot_lut | B（封装） | XilinxCoreLib | BLK_MEM_GEN_V4_2 |
| deinter_lut | B（封装） | XilinxCoreLib | BLK_MEM_GEN_V4_2 |

**关键结论**：风格 A 依赖 `unisims`，风格 B 依赖 `XilinxCoreLib`——这两个库都由 [dot11_modules.list:1-2](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L1-L2) 的 `-y` 指向。所以「用 .v 仿真模型跑 iverilog」在事实上仍需要这两套 Xilinx 仿真库存在；不需要的只是「跑 Xilinx 综合/实现」这一步。

#### 4.2.5 小练习与答案

**Q1**：`atan_lut.v` 里 `// synthesis translate_off` 包住的代码，在 iverilog 仿真时会执行吗？在 XST 综合时会执行吗？

**答案**：iverilog 仿真时会执行——`translate_off/translate_on` 只是给综合器的指令，仿真器照常解析这段 Verilog，从而得到由 `BLK_MEM_GEN_V4_2` 行为模型提供的 ROM 功能。XST 综合时会**忽略**这段，模块体被当成黑盒，真实实现来自配套的 `.ngc`。

**Q2**：既然 `viterbi_v7_0.v` 是「不能综合」的仿真模型，那真正能上板的 Viterbi 实现从哪里来？

**答案**：来自同名的 `viterbi_v7_0.ngc`（综合网表）。`.v` 给仿真用，`.ngc` 给综合/上板用，二者描述同一个 IP。coregen 的规矩就是「一个 IP，两副面孔」。

**Q3**：为什么 `complex_multiplier` 用 `DSP48A` 而不像 viterbi 那样用一堆 LUT？

**答案**：复数乘法是高密度定点乘加运算，用 FPGA 专用的 DSP 硬核（DSP48A）在面积、速度、功耗上都远优于用 LUT 拼。这也解释了为何 [dot11_modules.list:44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L44) 要单独拉一个 `MULT18X18S.v` 进来。

---

### 4.3 五个 IP 在解码链路中的角色，以及 verilog.rst 怎么讲

#### 4.3.1 概念说明

分清了文件族与两种 `.v` 风格后，最后一步是把它们「安」回解码链路。OpenOFDM 的 8 步流水线（u1-l5）里，有 4 个计算步骤由风格 A 的 IP 承担，另外 3 个查找表（相位、旋转、解交织）由风格 B 的 IP 承担。[docs/source/verilog.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/verilog.rst) 这篇文档则从「设计哲学」角度解释了为什么查找表 IP 会出现在这里——因为 FPGA 算力有限，作者大量用「量化 + 查表」近似来省资源。

#### 4.3.2 核心流程

把 IP 安回链路的映射关系：

```
sample_in
  → power_trigger / sync_short / sync_long(内含 xfft_v7_1 做 FFT)
                                                ↑ FFT 在这一步
  → equalizer(内含 div_gen_v3_0 做复数除法、complex_multiplier 做复乘)
                                                ↑ 除法/复乘在这一步
  → ofdm_decoder(内含 viterbi_v7_0 做卷积解码)
                              ↑ Viterbi 在这一步
  → byte_out

旁路查找表：atan_lut(phase)、rot_lut(rotate，双口共享)、deinter_lut(deinterleave)
```

`verilog.rst` 的叙事主线是：FPGA 算力受限 → 用两类「hack」近似 → 幅值估计（`complex_to_mag`，非 IP）与相位/旋转估计（用 atan_lut / rot_lut 这两个 IP）。

#### 4.3.3 源码精读

**FFT：xfft_v7_1（解码链路里唯一的 FFT）**

[sync_long.v:185-199](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L185-L199) 例化 `xfft_v7_1 dft_inst`，关键连接：

```verilog
xfft_v7_1 dft_inst (
    .clk(clock),
    .fwd_inv(1),           // 1 = 正向 FFT（不是 IFFT）
    .start(fft_start_delayed),
    .fwd_inv_we(1),
    .xn_re(fft_in_re), .xn_im(fft_in_im),   // 16bit 时域输入
    .xk_re(fft_out_re), .xk_im(fft_out_im), // 23bit 频域输出
    .rfd(fft_ready), .done(fft_done),
    .busy(fft_busy), .dv(fft_valid)
);
```

`fwd_inv=1` 表明这是正向 FFT（接收端做的是把时域样本变频域子载波）。这与 u1-l5 说的「FFT 藏在 sync_long 内部、并非独立模块」完全对应——**FFT 实际由 `xfft_v7_1` 承担**。它的端口定义见 [xfft_v7_1.v:36-53](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/xfft_v7_1.v#L36-L53)（`xn_re/xn_im` 输入 16 位，`xk_re/xk_im` 输出 23 位）。

**复数乘法：complex_multiplier**

[complex_mult.v:29-37](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L29-L37) 把 `complex_multiplier` 包成带 strobe 的握手模块；u3-l2 已讲过它「输入寄存 → IP → 输出寄存 + delayT 对齐 strobe」的三段式。它在全项目多处被复用（`sync_short`、`equalizer`，以及 `stage_mult` 里一次例化 4 个）。

**除法：div_gen_v3_0**

[divider.v:1-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v#L1-L29) 把 `div_gen_v3_0` 封装成固定 36 拍延时的除法器，被 `phase.v`（求 atan 前的 Q/I 除法）和 `equalizer.v`（复数除法均衡）使用。文件顶部注释 `* DELAY: 36 cycles` 与 [divider.v:24](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v#L24) 的 `delayT #(.DELAY(36))` 一致，正是 u2-l3 里 `DELAY(36)` 的来源。

**Viterbi 卷积解码：viterbi_v7_0**

[ofdm_decoder.v:81-90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L81-L90) 直接内联例化 `viterbi_v7_0`（u3-l5 讲过的「IP 内联」选择）；此外项目还保留了独立的封装 [viterbi.v:20-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v#L20-L29)，它的注释「A wrapper of Xilinx Viterbi IP core / Added strobe signal」与 `ce = reset | (enable & input_strobe)`、`rdy(output_strobe)` 正是 u3-l5 所说的「把无 strobe 的 IP 翻译成项目统一握手风格」的范本。

**三个查找表 IP 的实例化**

- [phase.v:85-89](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L85-L89)：`atan_lut` 把比值 `tan(θ)` 映射成定点相位；
- [dot11.v:105-113](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L105-L113)：`rot_lut` 在顶层用双口分别接 `sync_long` 与 `equalizer`（u6-2 的双口共享）；
- [deinterleave.v:69-73](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L69-L73)：`deinter_lut` 给出解交织的地址/位选指令（u3-l4）。

**verilog.rst 的设计叙事**

[verilog.rst:1-7](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/verilog.rst#L1-L7) 开宗明义：因为 FPGA 算力有限，实现里大量采用「量化（quantization）+ 查表（look up table）」两类近似。[verilog.rst:105-111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/verilog.rst#L105-L111) 进一步说明：atan 表用 `int(tan(θ)*256)` 作 key、把 `[0,π/4]` 量化成 256 段，表内容存进 `verilog/atan_lut.coe`，再由 coregen 生成 [coregen/atan_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v)。这就把 u5-l4 的 Python 生成脚本（产出 `.coe/.mif`）与本讲的核心 gen IP（把 `.coe` 烧进块 RAM）串成了一条完整链路：

```
gen_atan_lut.py → atan_lut.coe/.mif → coregen → atan_lut.v(BLK_MEM_GEN_V4_2) → phase.v 例化
```

#### 4.3.4 代码实践

**实践目标**：把每个 IP 精确定位到「被谁例化、在解码哪一步、做什么」，并指出 FFT 由谁承担。

**操作步骤**：

1. 用本讲提供的命令在每个 RTL 文件里搜 IP 名（**示例命令，待本地验证**）：
   ```
   grep -rn 'xfft_v7_1\|viterbi_v7_0\|div_gen_v3_0\|complex_multiplier\|atan_lut\|rot_lut\|deinter_lut' verilog/*.v
   ```
2. 为每个 IP 填写下表的「实例化位置 / 角色」两列。

**需要观察的现象 / 预期结果**：

| IP | 实例化位置 | 角色 |
|----|-----------|------|
| `xfft_v7_1` | sync_long.v:185（`dft_inst`） | **FFT**：时域→频域 |
| `complex_multiplier` | complex_mult.v:29；stage_mult.v 多处 | 复数乘法（自相关、均衡等） |
| `div_gen_v3_0` | divider.v:17 | 实数除法（atan 的 Q/I、均衡） |
| `viterbi_v7_0` | ofdm_decoder.v:81 | 卷积解码（Viterbi） |
| `atan_lut` | phase.v:85 | tan(θ)→θ 相位查表 |
| `rot_lut` | dot11.v:105 | sin/cos 旋转因子（双口共享） |
| `deinter_lut` | deinterleave.v:69 | 解交织地址/位选指令 |

**回答「FFT 由哪个 IP 承担」**：由 `xfft_v7_1` 承担，在 `sync_long.v` 中以 `dft_inst` 实例化，`fwd_inv=1` 表示正向变换。

#### 4.3.5 小练习与答案

**Q1**：`xfft_v7_1` 的输入 `xn_re/xn_im` 是 16 位、输出 `xk_re/xk_im` 是 23 位，位宽为什么变宽了？

**答案**：FFT 是多点求和，中间会累加多个乘积，位宽自然增长；IP 内部用更宽的数据通路防止溢出，对外输出保留更高精度（23 位）给下游 `equalizer` 处理。

**Q2**：`verilog.rst` 说作者用「查表」代替了本该用 CORDIC 的相位/旋转运算。结合本讲，这个「表」在硬件里是哪个 IP？

**答案**：相位表是 `atan_lut`、旋转表是 `rot_lut`，二者都是风格 B 的 coregen 块内存 ROM（`BLK_MEM_GEN_V4_2`），内容由 `gen_atan_lut.py`/`gen_rot_lut.py` 离线生成（u5-l4）。

**Q3**：如果要把 FFT 从 64 点改成 128 点，本讲涉及的哪些东西会受影响？

**答案**：至少要改 `xfft_v7_1` 的 coregen 配置（`.xco` 的点数参数）并重新生成 `.ngc` 与仿真 `.v`；iverilog 仿真依赖的 `xfft_v7_1.v` 也要随之替换。这正是「IP 是黑盒、内部不可手改」的代价。

---

## 5. 综合实践

**任务**：为 OpenOFDM 产出一份《coregen IP 依赖与仿真可行性报告》，把本讲三个最小模块串成一份可交付的工程文档。

**要求包含三部分**：

1. **带分类注释的 `dot11_modules.list`**：在清单每一行后注明 `LIB/RTL/PLAT/IP/PRIM`（参考 4.1.4 的表）。
2. **IP 依赖总表**：合并 4.2.4 与 4.3.4 两张表，给出每个 IP 的「风格（A netgen / B 封装）→ 仿真依赖库（unisims / XilinxCoreLib）→ 实例化位置 → 解码链路角色」。并在表下用一句话回答「FFT 由哪个 IP 承担、在哪例化」。
3. **仿真可行性说明**：基于本讲证据，写一段 150 字以内的说明——澄清「无 Xilinx 综合工具链也能仿真」的精确含义：仿真靠 `.v` 模型，**不需要**运行 XST/实现流程；但 `.v` 模型（风格 A 的 UNISIM 原语、风格 B 的 `BLK_MEM_GEN_V4_2`）仍需 [dot11_modules.list:1-2](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L1-L2) 指向的 `unisims` 与 `XilinxCoreLib` 仿真库存在（它们随免费 ISE WebPACK 提供；本仓库未自带 `Xilinx/` 目录）。

**自检**：

- 表中 7 个 IP 全部归类，无遗漏。
- 「FFT = xfft_v7_1，sync_long.v:185 dft_inst，fwd_inv=1」这一结论能在源码中找到出处。
- 说明里不要把「不需要综合」与「不需要任何 Xilinx 库」混为一谈。

> 如果你本机装了 iverilog 与 Xilinx ISE，可额外尝试：把 `dot11_modules.list` 的两行 `-y` 路径改成你本机 ISE 的真实路径，执行 `make compile`，记录是否报 unknown module。仿真能否跑通取决于你本机环境，**待本地验证**。

## 6. 本讲小结

- `dot11_modules.list` 是 iverilog 的命令文件（`-c`），分四段：Xilinx 库搜索路径（L1-2）、手写 RTL（L4-32）、USRP 平台模块（L33-34）、coregen IP 与一个原语（L36-44）；测试台 `dot11_tb.v` 不在其中，由 Makefile 单独追加。
- 一个 coregen IP 是一整套文件：`.xco`（配方）、`.ngc`（综合网表/上板）、`.v`（仿真模型）、`.veo/.vho`（实例化模板）、`.coe/.mif`（表数据）等；`.ngc` 给综合，`.v` 给仿真，二者描述同一 IP。
- coregen 的 `.v` 有两种风格：**风格 A**（viterbi/xfft/div_gen/complex_multiplier）是 netgen 扁平网表，由 UNISIM 原语（LUT/FF/进位链/DSP48A）组成，文件头声明「cannot be synthesized」；**风格 B**（atan_lut/rot_lut/deinter_lut）是薄封装，body 在 `translate_off` 内例化 `BLK_MEM_GEN_V4_2`、并以 `black_box` 属性交给 `.ngc` 综合。
- 两种风格分别依赖 `unisims` 与 `XilinxCoreLib` 仿真库，由清单前两行 `-y` 指向；故「用 iverilog 仿真」不需要跑 Xilinx 综合，但仍需要这两套（免费）仿真库存在。
- `complex_multiplier` 用 `DSP48A` 硬核，这正是 OpenOFDM 必须落在 Spartan 3A-DSP（USRP N210）上的原因；`viterbi_v7_0` 是用 LUT/进位链/FF 纯逻辑搭出的软核。
- 解码链路里的 FFT 由 `xfft_v7_1` 承担，在 `sync_long.v:185` 以 `dft_inst` 例化、`fwd_inv=1`；其余 IP 各司其职：复乘、除法、Viterbi、三个查找表，由 `verilog.rst` 的「量化 + 查表」设计哲学统一串起。

## 7. 下一步学习建议

- 顺读 **u6-4 USRP N210 集成**：把本讲的「IP 黑盒 + Spartan 3A-DSP」放回上板语境，理解 `dot11` 模块如何作为接收链一段接入 `custom_dsp_rx.v`。
- 回看 **u5-4 查找表生成脚本**与本讲 4.3.3 末尾的链路，确认 `gen_*_lut.py → .coe → coregen → .v` 这条「软件造表、硬件查表」闭环。
- 若对 IP 内部感兴趣，可挑一个风格 A 的网表（推荐从最小的 `complex_multiplier.v` 的 3 个 `DSP48A` 实例入手），对照 Xilinx UG190（Spartan-3 DSP48A 用户指南）阅读其仿真模型，理解定点乘加在硬核里的真实实现——这是把「黑盒」变「灰盒」的进阶练习。
