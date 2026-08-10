# Verilog 基础与 AES 顶层架构

## 1. 本讲目标

学完本讲后，你应当能够：

- 读懂一段真实 Verilog：模块（`module`）、端口（`input/output`）、`wire` 与 `reg` 的区别、`assign` 与 `always` 的用法、`` `define `` 宏与 `` `include ``/`` `ifndef `` 的作用。
- 说出 AES-128 的算法结构：10 轮、初始与每轮的 AddRoundKey、以及 SubBytes → ShiftRows → MixColumns 的顺序，并知道最后一轮没有 MixColumns。
- 把 `aes_top.v` 这张"接线图"看懂：四个寄存级、轮计数器、加密/解密多路选择，以及为什么状态矩阵要拆成 4 个并行的 `MixColumns` 实例。

本讲是 Unit 2（AES 加密核心数据通路）的入口。它只负责"顶层架构与语言基础"，具体每一个变换（SubBytes、MixColumns、Key Schedule）的内部实现会在 u2-l2 ~ u2-l5 逐一展开。

## 2. 前置知识

### 2.1 一句话认识 Verilog

Verilog 是一种**硬件描述语言**：你写的不是"一行行依次执行的指令"，而是"电路里有哪些元件、它们怎么连线"。综合工具（如 Vivado）把这份描述翻译成 FPGA 上的真实门电路。两个最关键的概念：

- **并行**：所有 `assign` 和 `always` 块描述的电路同时工作，没有先后顺序。
- **时钟**：时序逻辑（`always @(posedge clk)`）只在时钟上升沿更新，这是硬件"心跳"。

### 2.2 wire 与 reg 的直觉

| 关键字 | 直觉 | 用在哪 |
|--------|------|--------|
| `wire` | 一根导线，组合逻辑的连线 | `assign` 连续赋值、模块例化之间的连接 |
| `reg` | 一个能"记住"值的存储元件 | 在 `always` 块内被赋值的变量 |

> 注意：`reg` 不一定真的综合成寄存器。如果它只在组合 `always` 块里被赋值，综合出来仍是导线/查找表。关键字只是语法约束，不是最终硬件。

### 2.3 AES 是什么

AES（Advanced Encryption Standard）是一种**对称分组密码**：用同一把密钥加密和解密。AES-128 的分组长度和密钥长度都是 128 位（16 字节）。它把 16 字节明文经过 10 轮反复"混淆与扩散"，输出 16 字节密文。本讲只关心它的整体骨架，细节留给后续讲义。

## 3. 本讲源码地图

本讲聚焦 AES 核心的"顶层"与"全局配置"，涉及的文件都在 `HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/` 下：

| 文件 | 作用 |
|------|------|
| [src/aes_top.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v) | **顶层模块**，例化并串联所有 AES 变换，本讲主线。 |
| [utils/aes_types.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v) | **全局宏定义**：数据/密钥宽度、轮数、不可约多项式、字节定位宏。 |
| [src/aes_include.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_include.v) | **聚合头文件**，用一系列 `` `include `` 把复合域 S-Box 子模块集中引入。 |

`aes_top.v` 还例化了下面这些"兄弟模块"，本讲只看它们的**端口与连线含义**，内部实现留给后续讲义：

- `src/aes_sbox128.v` —— 16 字节并行 SubBytes（u2-l2）。
- `src/aes_shift_rows.v` —— ShiftRows 行移位（u2-l2）。
- `src/aes_mix_columns.v` —— MixColumns 列混淆（u2-l3）。
- `src/aes_key_schedule.v` —— 密钥扩展（u2-l4）。
- `src/aes_s_box.v` —— 单字节 S-Box，复合域实现（u2-l5）。

> 区分（承接 u1-l2）：`hdl/src/` 是**算法 RTL**，`ip_repo/.../hdl/` 是外层的 **AXI 包装**。本讲只读算法 RTL，AXI 封装在 Unit 3 讲。

## 4. 核心概念与源码讲解

### 4.1 模块一：aes_include / aes_types —— 宏定义与工程配置

#### 4.1.1 概念说明

一个稍大的设计会有很多"全局常量"：数据位宽、密钥位宽、轮数、复位电平、伽罗瓦域的不可约多项式……。如果把它们硬编码在每个文件里，要改 AES-256 就得逐文件手改。Verilog 的解决方式是：

- `` `define ``：定义**文本宏**。它在编译期被原样替换，更像 C 的 `#define` 而不是变量。
- `` `include ``：把另一个文件的内容原地插入，相当于"复制粘贴"进来。
- `` `ifndef ... `define ... `endif ``：**条件守卫**，确保一个宏只被定义一次，避免重复定义报错。

`aes_types.v` 集中放所有宏，`aes_include.v` 再用 `` `include `` 把它和一堆 S-Box 子模块串起来。这就是本模块要解决的问题：**一处定义、处处可用、避免重复**。

#### 4.1.2 核心流程

宏的工作流程是纯文本展开：

1. 编译器读到 `` `include "utils/aes_types.v" ``，把 `aes_types.v` 的全文插入到 `aes_include.v` 的这一行。
2. 此后所有 `` `define `` 出现过的名字（如 `KEY_SIZE`）在该编译会话中全局可见。
3. 任何文件里写 `` `KEY_SIZE ``，都会被替换成 `128`。
4. 如果某文件再用 `` `ifndef KEY_SIZE `` 守卫重新定义，因为宏已存在，守卫块被跳过——规范定义优先。

`aes_include.v` 的 include 链如下（[aes_include.v:21-39](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_include.v#L21-L39)）：

```verilog
`include "utils/aes_types.v"      // 全局宏
`include "gf_s_box/gf_inv_2.v"    // 复合域求逆子模块（u2-l5）
`include "gf_s_box/gf_mul_4.v"
`include "gf_s_box/gf_inv_8.v"
`include "utils/mux2_1.v"
`include "aes_s_box.v"
```

> 这里的相对路径 `utils/...`、`gf_s_box/...` 是相对于 `hdl/` 目录解析的，具体搜索路径由 Vivado 工程的 include 目录设置决定。

**一个值得注意的"缺口"**：`aes_include.v` 主要服务复合域 S-Box 子模块（`gf_s_box/*`），它**并没有** include `aes_top.v`、`aes_shift_rows.v`、`aes_key_schedule.v`、`aes_mix_columns.v`、`aes_sbox128.v`。也就是说，它不是"整个 AES 设计的总头文件"，真正的完整文件清单来自 Vivado 工程本身（`create_project.tcl` / `component.xml`，见 u1-l3、u3-l1）。读这个仓库时要分清"include 头"与"工程文件列表"两件事。

#### 4.1.3 源码精读

`aes_types.v` 定义的全部宏（[aes_types.v:21-40](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v#L21-L40)）：

```verilog
`define ENCRIPT 1'b1          // 加密模式选择值
`define DECRIPT 1'b0          // 解密模式选择值
`define POLYNOMIAL_IRR 8'b0001_1011   // GF(2^8) 不可约多项式
`define KEY_SIZE      128
`define DATA_SIZE     128
`define R_ACTIV     1'b1      // 复位有效电平：高
`define R_INACTIV   1'b0
`define NO_OF_ROUNDS  10      // AES-128 轮数
`define u32   [31:0]          // "类型"宏：32 位
`define u8    [7:0]
`define u4    [3:0]
`define u8_MSB(x)  8*(x+1) - 1   // 第 x 字节的最高位下标
`define u8_LSB(x)  8*x           // 第 x 字节的最低位下标
```

逐类理解：

- **模式与电平**：`ENCRIPT`/`DECRIPT`（注意作者拼写为 ENCRIPT/DECRIPT，源码如此）是 `encrypt` 端口的选择值；`R_ACTIV` 说明本设计是**高电平复位**。
- **关键参数**：`KEY_SIZE`、`DATA_SIZE` 都是 128，`NO_OF_ROUNDS` 是 10——这三行就是"AES-128"这个名字的全部含义。
- **不可约多项式**：`8'b0001_1011` = 0x1B，对应 \(x^8+x^4+x^3+x+1\)（系数为 1 的位是第 0、1、3、4 位）。它是 AES 在伽罗瓦域 GF(\(2^8\)) 上做乘法/求逆时所用的"模"，具体用法在 u2-l3 与 u2-l5。
- **"类型"宏**：`` `u8 `` 会展开成 `[7:0]`，于是 `` wire `u8 my_byte; `` 等价于 `wire [7:0] my_byte;`。这是用宏模拟"类型别名"。
- **字节定位宏**（本讲最常用）：把 128 位状态看作 16 字节，第 \(i\) 字节占据位 \([8i+7 : 8i]\)。即

\[
\text{u8\_MSB}(i) = 8(i+1)-1 = 8i+7,\qquad \text{u8\_LSB}(i) = 8i.
\]

例如 `u8_MSB(0):u8_LSB(0)` = `[7:0]` 是第 0 字节，`u8_MSB(15):u8_LSB(15)` = `[127:120]` 是第 15 字节。`aes_top.v` 正是用这两个宏把 128 位总线切成 4 列喂给 4 个 `MixColumns`。

> **批判阅读（承接 u1-l3）**：`aes_top.v` 顶部又用 `` `ifndef `` 自定义了一份同样的宏（[aes_top.v:20-42](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L20-L42)），其中 `u8_LSB` 写成了 `` `define u8_LSB(x) 8*x1 ``（多了一个 `1`，疑为笔误；规范值 `8*x` 在 `aes_types.v`）。由于有 `` `ifndef `` 守卫，只要 `aes_types.v`（经 `aes_include.v`）先被编译，规范定义就会生效，`aes_top.v` 里的这行被跳过。但若把 `aes_top.v` 单独编译、不带 `aes_types.v`，这处笔误就会生效。**结论：以 `aes_types.v` 为准，并在仿真中验证**（见 u3-l5）。这是读本仓库应有的警惕。

#### 4.1.4 代码实践：追踪宏展开

**实践目标**：亲手做一次"编译器视角"的宏展开，确认字节定位宏确实把 128 位切成 16 个连续字节。

**操作步骤**：

1. 打开 `aes_types.v`，找到 `u8_MSB` / `u8_LSB` 两个宏。
2. 用纸笔（或计算器）对 \(i = 0, 1, 7, 8, 15\) 计算 `u8_MSB(i)` 与 `u8_LSB(i)`。
3. 把它们写成位区间 `[MSB:LSB]`，检查相邻字节的区间是否首尾相接、合起来正好覆盖 `[127:0]`。

**需要观察的现象**：

- 第 0 字节 = `[7:0]`，第 1 字节 = `[15:8]`，……，第 15 字节 = `[127:120]`。
- 16 个区间无重叠、无空隙，正好拼成 128 位。

**预期结果**：

| \(i\) | u8_MSB(i) | u8_LSB(i) | 区间 |
|------|-----------|-----------|------|
| 0 | 7 | 0 | `[7:0]` |
| 1 | 15 | 8 | `[15:8]` |
| 7 | 63 | 56 | `[63:56]` |
| 8 | 71 | 64 | `[71:64]` |
| 15 | 127 | 120 | `[127:120]` |

**进阶（源码阅读型）**：对照 `aes_include.v` 的 include 列表与本讲"源码地图"里 `aes_top.v` 实际例化的模块，列出"`aes_include.v` 没有覆盖、但 `aes_top.v` 用到"的模块有哪些（答案见下文练习）。

#### 4.1.5 小练习与答案

**练习 1**：为什么所有 `` `define `` 都要包在 `` `ifndef ... `endif `` 里？

> **答案**：防止同一个宏在多文件/多次 include 时被重复定义导致编译警告或冲突；并让最先被编译的（规范的）定义生效，后来的本地副本被跳过。

**练习 2**：`POLYNOMIAL_IRR = 8'b0001_1011` 对应的多项式是什么？为什么只写 8 位？

> **答案**：对应 \(x^8+x^4+x^3+x+1\)（即 0x11B，1_0001_1011）。8 位只写出 \(x^7\dots x^0\) 的系数，最高位 \(x^8\) 隐含为 1。它是 AES 在 GF(\(2^8\)) 上做模运算的不可约多项式（u2-l3 详述）。

**练习 3**：`aes_include.v` 没有覆盖、但 `aes_top.v` 实际例化的模块有哪些？

> **答案**：`aes_sbox128`、`aes_shift_rows`、`key_schedule`、`MixColumns`（以及 `aes_top` 自身）。说明 `aes_include.v` 不是全设计的总头文件。

---

### 4.2 模块二：aes_top —— 顶层模块与数据通路

#### 4.2.1 概念说明：AES-128 的轮函数

AES-128 的加密流程在算法层面是这样的：

```
明文(128b) ──AddRoundKey(轮密钥0)──┐
                                   ↓
   ┌──────── 第 1~9 轮（每轮相同）─────────┐
   │  SubBytes → ShiftRows → MixColumns → AddRoundKey  │
   └──────────────────────────────────────────────────┘
                                   ↓
   ┌──────── 第 10 轮（最后一轮）──────────┐
   │  SubBytes → ShiftRows → AddRoundKey   ← 没有 MixColumns
   └──────────────────────────────────────────────────┘
                                   ↓
                                密文(128b)
```

四个变换的直觉：

- **AddRoundKey**：状态与轮密钥做按位异或（XOR），把密钥的"影响"注入数据。
- **SubBytes**：对每个字节查 S-Box 做非线性替换，提供"混淆"。
- **ShiftRows**：把状态按行循环移位，打乱字节位置。
- **MixColumns**：在 GF(\(2^8\)) 上对每一列做矩阵乘法，提供"扩散"。

**状态矩阵**：16 字节被排成 \(4\times4\) 矩阵，按**列**填充。MixColumns 一次处理**一列（4 字节）**，所以一共 4 列 → 需要 4 个并行的 `MixColumns` 实例。这正是 `aes_top.v` 里出现 4 个 `mix_inst` 的根本原因。

解密是加密的逆过程（InvSubBytes / InvShiftRows / InvMixColumns），且复用**同一套**密钥扩展（u2-l4）。

#### 4.2.2 核心流程：反馈式数据通路

`aes_top.v` 没有把 10 轮展开成 10 份硬件，而是用一个**反馈回路**：数据每经过一拍在"SubBytes → ShiftRows → MixColumns → AddRoundKey"的组合云里走一轮，靠轮计数器 `round_counter` 数到 10。

数据流向（按信号命名）：

```
        ┌────────────── selection 多路选择 ──────────────┐
        ↓                                                │
   SB_input ─SubBytes(aes_sbox128)─→ SB_output           │
                                          ↓ (寄存)         │
                                   reg_sub_byte_lvl       │
                                          ↓                │
              ─ShiftRows(aes_shift_rows)─→ SH_out ────────┤
                                          ↓                │
                                     MC_input ◄────────────┤ AddRoundKey(解密路径)
                                          ↓                │
              ─MixColumns×4────────────→ MC_output_e/d     │
                                          ↓ (寄存/反馈)     │
                          reg_data_in_lvl / reg_mix_col_lvl /
                          reg_inv_mix_col_lvl ─────────────┘
        同时：text_out = SH_out ^ round_key   （最后一轮 AddRoundKey）
        同时：key_schedule 每拍把 round_key → round_key_out
```

控制信号要点（全部来自源码，[aes_top.v:80-98](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L80-L98)）：

- `round_counter`：0 → 10 循环，标记当前是第几轮。
- `r_cnt_zero = (round_counter==0 || round_counter==1)`：标记最前两拍（因为流水线里同时有相邻轮的数据，所以需要把第 0、1 轮一起特殊处理）。
- `selection[1:0]`：由 `encrypt` 与 `r_cnt_zero` 组合得到，决定下一拍 SubBytes 的输入来自哪一级寄存器。
- `MC_input`：加密时直接取 `SH_out`；解密时取 `SH_out ^ round_key`（"等价逆密码"结构，让加解密共用同一条数据通路）。

> 关于每拍是否都更新这四个寄存级、数据如何逐轮严格反馈，源码的时序细节较微妙（`always` 块在 `reset` 有效时给四个寄存级播种初值，运行分支只推进 `round_counter` 与 `round_key`），**精确的逐拍行为需要仿真确认**，本讲先建立结构地图，验证留到 u3-l5。

#### 4.2.3 源码精读

**(1) 模块与端口** —— Verilog 的"函数签名"（[aes_top.v:44-53](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L44-L53)）：

```verilog
module aes_top (clk, reset, text_in, key, encrypt, text_out);
    input clk;                                   // 时钟
    input reset;                                 // 复位/启动（高有效）
    input [`DATA_SIZE -1:0] text_in;             // 明文/密文，128 位
    input [`KEY_SIZE -1:0]  key;                 // 密钥，128 位
    input encrypt;                               // 1=加密，0=解密
    output [`DATA_SIZE -1:0] text_out;           // 结果，128 位
```

- `module 名字 (端口列表); ... endmodule` 是 Verilog 模块的基本骨架。
- `` `DATA_SIZE `` 宏展开成 `128`，于是 `input [127:0] text_in;`。这是宏做"参数化"的典型用法。
- `clk`/`reset`/`encrypt` 是 1 位控制信号（没写位宽默认 1 位）。

**(2) 内部 wire 与 reg**（[aes_top.v:55-71](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L55-L71)）：

```verilog
reg  [`DATA_SIZE -1:0] reg_data_in_lvl;      // 输入级（初始 AddRoundKey 后）
reg  [`DATA_SIZE -1:0] reg_sub_byte_lvl;     // SubBytes 后的寄存级
reg  [`DATA_SIZE -1:0] reg_mix_col_lvl;      // (加密) MixColumns 后
reg  [`DATA_SIZE -1:0] reg_inv_mix_col_lvl;  // (解密) InvMixColumns 后
reg  [3:0]             round_counter;        // 轮计数，4 位够表示 0~10
reg  [`KEY_SIZE-1:0]   round_key;            // 当前轮密钥
wire [`DATA_SIZE -1:0] SB_output;            // 组合连线，不需要记忆 → wire
```

- 四个 `reg _lvl` 是流水线的"寄存级"，名字里的 `lvl` = level（级）。
- `SB_output`、`SH_out`、`MC_output_e/d` 是组合逻辑的输出连线，所以用 `wire`。
- `round_counter` 是 `[3:0]`（0~15），覆盖 0~10 足够。

**(3) 组合输出与多路选择**（[aes_top.v:80-98](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L80-L98)）：

```verilog
assign text_out = SH_out ^ round_key;        // 最终 AddRoundKey（最后一轮）
assign r_cnt_zero = (round_counter == 0 || round_counter == 1) ? 1 : 0;
assign MC_input = (encrypt) ? SH_out : SH_out ^ round_key;  // 解密多一次 XOR
assign selection[0] = encrypt & (~r_cnt_zero);
assign selection[1] = r_cnt_zero;

always @(selection, reg_data_in_lvl, reg_mix_col_lvl, reg_inv_mix_col_lvl) begin
    case(selection)
        2'b00 : SB_input <= reg_inv_mix_col_lvl;   // 解密路径
        2'b01 : SB_input <= reg_mix_col_lvl;       // 第 0/1 轮
        2'b10 : SB_input <= reg_data_in_lvl;       // 加密常规轮
    endcase
end
```

- `assign` 写**连续赋值**：右边一变，左边立刻更新（组合逻辑）。`^` 是按位异或，`&` 是按位与，`~` 是取反。
- `encrypt ? a : b` 是三元运算符，和 C 一样。
- `always @(敏感列表)` 是**组合 always**：敏感列表里任一信号变化就执行，用来描述多路选择器（`case`）。这里用 `<=`（非阻塞）写组合逻辑并不规范，但功能上等价于一个选择器。

**(4) 时序逻辑：复位/启动与轮推进**（[aes_top.v:100-122](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L100-L122)）：

```verilog
always @(posedge clk) begin                   // 只在时钟上升沿动作
    if (reset == `R_ACTIV) begin              // 复位 = 启动新分组
        round_counter <= 0;
        round_key     <= key;
        reg_data_in_lvl    <= key ^ text_in;  // 初始 AddRoundKey
        reg_sub_byte_lvl   <= SB_output;
        reg_mix_col_lvl    <= MC_output_e ^ round_key;
        reg_inv_mix_col_lvl<= MC_output_d;
    end else begin
        if (round_counter < `NO_OF_ROUNDS) begin
            round_counter <= round_counter + 1'b1;   // 数到 10
            round_key     <= round_key_out;          // 密钥扩展前进一拍
        end else begin
            round_counter <= 0;                      // 做完 10 轮，回到起点
            round_key     <= key;
        end
    end
end
```

- `always @(posedge clk)` 是**时序 always**，描述寄存器；`<=` 是**非阻塞赋值**，所有右侧先用旧值求值、在块结束时同时更新，这是写时序逻辑的标准做法。
- 这里 `reset` 更像"**启动/加载新分组**"信号：拉高时把 `key ^ text_in`（初始 AddRoundKey 结果）放进 `reg_data_in_lvl`，并把轮计数清零、轮密钥装初值。
- 运行分支只推进 `round_counter` 与 `round_key`（经 `key_schedule`），数满 10 轮后回卷。

**(5) 模块例化：把变换接成电路**（[aes_top.v:124-141](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L124-L141)）：

```verilog
aes_s_box128  sbox_inst        (SB_input, encrypt, SB_output);   // SubBytes
aes_shift_rows shift_row_inst  (reg_sub_byte_lvl, encrypt, SH_out); // ShiftRows
key_schedule  key_schedule_inst(round_key, encrypt, round_counter, round_key_out);
MixColumns    mix_inst0 (                       // 4 个里的第 0 列
        MC_input[`u8_MSB(0):`u8_LSB(0)], MC_input[`u8_MSB(1):`u8_LSB(1)],
        MC_input[`u8_MSB(2):`u8_LSB(2)], MC_input[`u8_MSB(3):`u8_LSB(3)],
        MC_output_e[`u8_MSB(0):`u8_LSB(0)], /* ...加密输出 a0..a3 */
        MC_output_d[`u8_MSB(0):`u8_LSB(0)] /* ...解密输出 c0..c3 */);
```

- 例化语法：`模块名 实例名 (端口连接);`。这里用的是**按位置**连接（顺序必须与被调模块端口声明一致）。
- `MixColumns` 的端口顺序是 `b0,b1,b2,b3, a0,a1,a2,a3, c0,c1,c2,c3`（[aes_mix_columns.v:17-21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_mix_columns.v#L17-L21)）：前 4 个是输入列，中间 4 个是**加密**输出，最后 4 个是**解密**输出。
- `mix_inst0` 处理第 0 列（字节 0~3），`mix_inst1` 处理第 1 列（字节 4~7），依此类推，`mix_inst3` 处理第 3 列（字节 12~15）。**4 个实例 = 状态矩阵的 4 列，并行做 MixColumns**。
- `SB_input[...]` 用 `` `u8_MSB ``/`` `u8_LSB `` 切片，正好体现 4.1 里讲的字节定位宏——学以致用。

#### 4.2.4 代码实践：画出 aes_top.v 的数据通路框图

**实践目标**：把 `aes_top.v` 的"接线关系"画成一张框图，固化对本讲的理解。这是本讲的主实践。

**操作步骤**：

1. 在纸或绘图工具上，画出以下"方块"（每个对应一段源码）：
   - 输入端口：`text_in`、`key`、`encrypt`、`clk`、`reset`；输出端口 `text_out`。
   - 四个寄存级：`reg_data_in_lvl`、`reg_sub_byte_lvl`、`reg_mix_col_lvl`、`reg_inv_mix_col_lvl`。
   - 四个变换实例：`aes_sbox128`（SubBytes）、`aes_shift_rows`（ShiftRows）、4×`MixColumns`、`key_schedule`。
   - 控制信号：`round_counter`、`r_cnt_zero`、`selection[1:0]`。
2. 按 4.2.2 的流向，用箭头连接：
   - `selection` →（多路选择）→ `SB_input` → `aes_sbox128` → `SB_output` → `reg_sub_byte_lvl`。
   - `reg_sub_byte_lvl` → `aes_shift_rows` → `SH_out`。
   - `SH_out`（与解密路径的 `^round_key`）→ `MC_input` → 4×`MixColumns` → `MC_output_e/d` → 对应寄存级 → 反馈回 `selection`。
   - `SH_out ^ round_key` → `text_out`。
   - `round_key` ↔ `key_schedule` ↔ `round_key_out`，`round_counter` 驱动 `key_schedule` 与 `selection`。
3. 在 `MC_input` 喂给 4 个 `MixColumns` 的地方，用虚线把 128 位标注成 4 列（字节 0~3 / 4~7 / 8~11 / 12~15），分别指向 `mix_inst0..3`。

**需要观察的现象**：

- 整个数据通路是一个**闭环反馈**，而不是 10 份串联的硬件。
- 4 个 `MixColumns` 是**并行**的，分别对应状态矩阵的 4 列。
- 加密与解密**共用同一条通路**，差别只在 `selection` 与 `MC_input` 的多路选择。

**预期结果**：得到一张包含"输入 → SubBytes → (寄存) → ShiftRows → MixColumns×4 → (寄存/反馈) → text_out"的闭环图，并清楚标出 `round_counter` 与 `selection` 的控制位置。

**说明**：精确的逐拍波形（每个寄存级在哪一拍更新）需要仿真确认，**待本地验证**（见 u3-l5 的 VE_sv 环境）。本实践只要求结构正确。

#### 4.2.5 小练习与答案

**练习 1**：AES-128 一共几轮？最后一轮和中间轮相比少了哪个变换？

> **答案**：10 轮。最后一轮**没有 MixColumns**，即 SubBytes → ShiftRows → AddRoundKey。`aes_top.v` 里 `text_out = SH_out ^ round_key` 正好对应"最后一轮 ShiftRows 之后直接做 AddRoundKey"，没有再经过 MixColumns。

**练习 2**：为什么 `aes_top.v` 里要例化 4 个 `MixColumns`，而不是 1 个？

> **答案**：AES 状态是 \(4\times4\) 字节矩阵，MixColumns 按列处理，共 4 列。为了在一个时钟内并行算完所有列，用 4 个实例各处理一列（字节 0~3、4~7、8~11、12~15）。

**练习 3**：`text_out = SH_out ^ round_key` 实现的是哪个 AES 变换？为什么用异或？

> **答案**：AddRoundKey。AddRoundKey 的定义就是把状态与轮密钥按位异或；异或的自反性（`a^k^k = a`）也让同一套硬件既能加密又能解密。

**练习 4**：`always @(posedge clk)` 块里用 `<=`（非阻塞）而不是 `=`（阻塞），有什么好处？

> **答案**：非阻塞赋值让块内所有右侧先用旧值求值、在时钟沿结束时统一更新，避免赋值顺序依赖，是描述时序逻辑（寄存器）的标准写法，能正确综合并减少仿真/综合不一致。

## 5. 综合实践：跟踪一次"加密"的信号流

把本讲的两条主线（宏 + 数据通路）串起来：

**任务**：假设 `encrypt = 1`、`reset` 来一个高电平脉冲启动一个新分组。请按下列步骤追踪信号流，并标注每一步对应哪个源码位置。

1. **加载**：`reset` 有效时，`key ^ text_in` 写入 `reg_data_in_lvl`（[aes_top.v:105](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L105)）。说明这一步实现了 AES 的哪一个部分。
2. **选路**：当 `round_counter` 推进到常规轮时，`selection` 会选 `reg_data_in_lvl` 作为 `SB_input`（[aes_top.v:89-98](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L89-L98)）。
3. **三连变换**：依次说出 `aes_sbox128` → `reg_sub_byte_lvl` → `aes_shift_rows` → `MC_input` → 4×`MixColumns` 分别对应 AES 的哪几个变换（[aes_top.v:124-141](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L124-L141)）。
4. **字节切片**：指出 `mix_inst2` 处理的是哪 4 个字节、对应位区间是多少（用 `u8_MSB`/`u8_LSB` 写出）。
5. **收尾**：说明为什么最终输出取 `SH_out ^ round_key` 而不是某个 MixColumns 输出。

**交付物**：一张标注完整的信号流图 + 一段文字解释。**精确波形待本地验证**，本任务只考查对结构的理解。

## 6. 本讲小结

- `aes_types.v` 用 `` `define `` + `` `ifndef `` 守卫集中管理全局宏：`KEY_SIZE`/`DATA_SIZE=128`、`NO_OF_ROUNDS=10`、不可约多项式 `0x1B`，以及字节定位宏 `u8_MSB`/`u8_LSB`。
- `aes_include.v` 是复合域 S-Box 子模块的聚合头文件，**不是**全设计的总文件清单——`aes_top.v` 实际依赖的模块清单来自 Vivado 工程。
- AES-128 = 初始 AddRoundKey + 9 轮"SubBytes→ShiftRows→MixColumns→AddRoundKey" + 最后一轮（无 MixColumns）。
- `aes_top.v` 用**反馈式数据通路**实现这 10 轮：四个寄存级 + `selection` 多路选择 + `round_counter` 控制，加解密共用同一条通路。
- 状态矩阵 4 列 → 4 个并行 `MixColumns` 实例，用 `u8_MSB`/`u8_LSB` 宏按列切片。
- 读懂了 Verilog 的 `module`/端口/`wire`/`reg`/`assign`/`always`/例化/宏——这些语法会贯穿后续所有讲义。

## 7. 下一步学习建议

- 想深入每个变换的实现，按顺序读：**u2-l2**（SubBytes 与 ShiftRows）→ **u2-l3**（MixColumns 与 GF(\(2^8\)) 乘法，用到本讲的 `POLYNOMIAL_IRR`）→ **u2-l4**（Key Schedule）。
- 想知道 S-Box 为什么不查表而用复合域电路，读 **u2-l5**（GF(\(2^4\)) 高效实现）。
- 想验证本讲遗留的"逐拍波形"问题，跳到 **u3-l5**（仿真与 VE_sv 验证环境），用 `tb_s_box.v` 起步。
- 建议同时打开 `aes_top.v` 和 `aes_types.v` 两个窗口对照阅读，每看到一个宏就回去查它的定义。
