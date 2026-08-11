# SubBytes 与 ShiftRows：字节替换与行移位

## 1. 本讲目标

本讲是 AES 数据通路的「字节级变换」专题。学完后你应当能够：

- 说清 AES 一轮中的 **SubBytes**（字节替换）和 **ShiftRows**（行移位）分别在做什么、为什么需要它们。
- 看懂本项目里 `bSbox`（单字节 S-Box）、`aes_s_box128`（16 字节并行 S-Box）和 `aes_shift_rows`（行移位）三个模块的 Verilog 实现。
- 理解加密 / 解密如何**共用同一条数据通路**：通过一个 `encrypt` 选择信号，让 S-Box 在「正向 S-Box」与「逆向 S-Box」之间切换，让 ShiftRows 在「左移」与「右移」之间切换。
- 能够拿一段 128 位状态，**手算**出 SubBytes 和 ShiftRows 的输出，并与标准 AES 表对照验证。
- 延续 [u1-l3](u1-l3-vivado-project-template.md) 与 [u2-l1](u2-l1-aes-top-architecture.md) 已经建立的批判性阅读习惯：本仓库的 RTL 属于「草稿级」工程，含笔误与未完成模块，读源码时要边读边在仿真里验证。

## 2. 前置知识

在进入源码前，先用三段话把直觉建立起来。

**(a) AES 的状态（State）是一张 4×4 字节矩阵。**
一次 AES-128 处理 128 位数据，即 16 字节。这 16 字节不是排成一条线，而是**按列填入**一张 4×4 矩阵。这一点非常重要：SubBytes 对每个字节单独操作（与位置无关），而 ShiftRows 对**每一行**做循环移位（与位置强相关）。如果搞错字节排布，ShiftRows 就会全错。

**(b) 为什么要做「非线性替换」？**
如果 AES 只有线性运算（异或、移位、矩阵乘），那么整轮加密都可以写成 \( y = Mx \oplus k \) 的形式，攻击者用少量明密文对就能解出密钥。SubBytes 通过一张**非线性**的查找表（S-Box）打破这种线性结构，是 AES 安全性的核心来源。

**(c) 为什么要做「行移位」？**
SubBytes 是「逐字节」的——第 *i* 个字节换成什么，只取决于第 *i* 个字节本身，字节之间没有混合。ShiftRows 把不同列的字节重新打散到新的位置上，让后续的 MixColumns（[u2-l3](u2-l3-aes-mixcolumns-gf.md)）能够把整列混在一起，从而实现「扩散（diffusion）」。

> 一个关键约定：本仓库的全局宏都在 [aes_types.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v#L21-L40) 中定义，包括 `ENCRIPT=1'b1`、`DECRIPT=1'b0`、`DATA_SIZE=128`，以及把 128 位切成 16 字节的字节定位宏 `u8_MSB(x)=8*(x+1)-1`、`u8_LSB(x)=8*x`。本讲所有字节编号都基于这套宏：**字节 *x* 占据比特 \([8x{+}7 : 8x]\)**。

## 3. 本讲源码地图

| 文件 | 模块 | 作用 |
|------|------|------|
| `hdl/src/aes_s_box.v` | `bSbox` | **单字节** S-Box，用 Canright 复合域算法同时支持正向 / 逆向替换（本讲的算法内核）。 |
| `hdl/src/aes_sbox128.v` | `aes_s_box128` | 例化 16 个 `bSbox`，对整张 4×4 状态做**并行** SubBytes。 |
| `hdl/src/aes_shift_rows.v` | `aes_shift_rows` | 用纯组合连线（字节重排）实现 ShiftRows / InvShiftRows。 |
| `hdl/src/aes_sub_byte.v` | `aes_sub_byte` | 一个**未完成**的替代方案（只写了仿射变换的一部分）。用来对照学习，但 `aes_top` 并不使用它。 |
| `hdl/utils/select_not_8.v` | `select_not_8` | 8 位宽的 2 选 1 多路器，是 `bSbox` 复用加 / 解密通路的关键零件。 |
| `hdl/tb/tb_s_box.v` | `tb_sbox` | S-Box 的仿真激励（加解密往返测试）。 |
| `hdl/src/aes_top.v` | `aes_top` | 顶层，例化上面这些模块（[u2-l1](u2-l1-aes-top-architecture.md) 已讲）。 |

> 说明：本仓库按「算法 RTL」与「AXI 包装」分目录（见 [u1-l2](u1-l2-directory-map.md)）。本讲的所有文件都在 `hdl/src`、`hdl/utils`、`hdl/tb` 下，属于**算法 RTL** 这一侧。

## 4. 核心概念与源码讲解

### 4.1 SubBytes：逐字节的非线性替换

#### 4.1.1 概念说明

SubBytes 把状态矩阵里的**每一个字节** \( a \)（8 位）经过一张固定的查找表 S-Box，替换成另一个字节 \( b = S[a] \)。AES 的 S-Box 不是随便选的表，它在有限域 \( GF(2^8) \) 上有精确的数学定义：

\[
S[a] = \text{Affine}\bigl(\text{Inv}(a)\bigr)
\]

即「先在 \( GF(2^8) \) 里求乘法逆元，再做一次仿射变换」。其中 0 的逆元规定为 0。这两步合起来，得到一张高度非线性、且没有任何「不动点 / 线性结构」的 256 字节表。

解密用的 InvSubBytes 用的是同一张表的**逆表** \( S^{-1} \)，即 \( a = S^{-1}[b] \)。本项目用一个聪明的设计——**同一个电路靠 `encrypt` 信号在正、逆之间切换**，而不需要两张表、两套电路。

#### 4.1.2 核心流程

`bSbox` 模块对**单个字节**做替换，流程是 Canright 复合域算法（数学细节留给 [u2-l5](u2-l5-aes-sbox-composite-field.md)，这里只看骨架）：

```text
输入字节 A (8 bit)
   │
   ├─ 换基：GF(2^8) → 复合域，同时根据 encrypt 选两套系数 B / Y
   │        （select_not_8 在 B、Y 间二选一，得到 Z）
   │
   ├─ gf_inv_8：在复合域里求逆，得到 C
   │
   ├─ 换基回去：复合域 → GF(2^8)，再根据 encrypt 选两套系数 D / X
   │        （select_not_8 在 D、X 间二选一，得到 Q）
   │
   └─ 输出字节 Q = S[A]（encrypt=1）或 S⁻¹[A]（encrypt=0）
```

关键点：**求逆运算 `gf_inv_8` 是加解密共用的**；正逆 S-Box 的区别，全靠两次 `select_not_8` 选择不同的「换基 + 仿射」系数来实现。这就是「加解密共用通路」的由来。

#### 4.1.3 源码精读

`bSbox` 的模块声明与那个决定正逆的 `encrypt` 引脚：

[aes_s_box.v:21-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L21-L24) —— 模块名为 `bSbox`，`encrypt` 为 1 时输出正向 S-Box，为 0 时输出逆向 S-Box。

求逆前后的两处核心例化，浓缩了上面流程图的全部控制：

[aes_s_box.v:55-56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L55-L56) —— `select_not_8 sel_in(...)` 在加密基 `B` 与解密基 `Y` 之间选一个送给求逆器；`gf_inv_8 inv(...)` 做复合域求逆。

[aes_s_box.v:84](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L84) —— `select_not_8 sel_out(...)` 在加密还原 `D` 与解密还原 `X` 之间选一个，得到最终输出 `Q`。

中间那一大段 `assign R1=...; assign B[7]=...; assign Y[7]=...`（[aes_s_box.v:30-54](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L30-L54)）和 `assign T1=...; assign D[7]=...; assign X[7]=...`（[aes_s_box.v:58-83](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L58-L83)），就是把换基矩阵和仿射矩阵**手工展开成异或树**（因为矩阵元素只有 0/1，乘法就是与、加法就是异或）。`~^` 是同或（XNOR）。这些展开是 Canright 论文给的固定系数，照抄即可，本讲不必逐位推导，[u2-l5](u2-l5-aes-sbox-composite-field.md) 会讲它们怎么来的。

帮助 `bSbox` 实现「加解密二选一」的小零件 `select_not_8`，本质就是 8 个 2 选 1 多路器：

[select_not_8.v:21-33](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/select_not_8.v#L21-L33) —— 对每一位用 `mux2_1` 在 `A`、`B` 之间按选择信号 `s` 取一个，组成 8 位输出 `Q`。

> **关于 `aes_sub_byte.v`（请批判阅读）**：仓库里还有一个 [aes_sub_byte.v:16-40](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sub_byte.v#L16-L40)。它看起来想用「直接做仿射变换」的方式实现 S-Box：[第 31 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sub_byte.v#L31) 出现的 `8'h63` 正是 AES 仿射常数 \( c \)，[第 33-40 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sub_byte.v#L33-L40) 是若干位的异或组合（仿射矩阵的一部分）。**但这个模块是未完成的草稿**：它既没有做 \( GF(2^8) \) 求逆（`inv_in` 声明了却没接），也**从未给 `s_out` 赋值**，输出悬空。顶层 [aes_top.v:124](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L124) 例化的是 `aes_s_box128`（内部用 `bSbox`），**并不使用 `aes_sub_byte`**。我们读它是为了认出「仿射变换的零件长什么样」，而不是把它当成可用实现。这正是 [u2-l1](u2-l1-aes-top-architecture.md) 提醒过的：仓库里存在重复 / 半成品文件，以 `aes_top` 实际例化者为准。

#### 4.1.4 代码实践

**实践目标**：用仓库自带的 testbench 验证 `bSbox` 的「加解密往返」性质——先加密再解密，应当还原成原字节。

**操作步骤**：

1. 打开 [tb_s_box.v:24-47](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/tb/tb_s_box.v#L24-L47)。它例化了两个 `bSbox`：第一个用 `ENCRIPT` 把 `data_in` 加密成 `data_out_e`，第二个用 `DECRIPT` 把 `data_out_e` 解密成 `data_out_d`，输入依次为 `0x01`、`0x10`、`0x02`。
2. 用任意 Verilog 仿真器（Icarus Verilog / Verilator / Vivado xsim）编译 `bSbox` 及其依赖（`select_not_8`、`mux2_1`、`gf_inv_8` 等 gf_s_box 目录下的子模块），再加 `tb_s_box.v` 跑仿真，观察 `data_out_e` 与 `data_out_d` 波形。
3. 对照标准 AES S-Box 表核对。

**预期结果**（按标准 S-Box，`S[0x01]=0x7c`、`S[0x10]=0xca`、`S[0x02]=0x77`）：

| `data_in` | `data_out_e`（正向 S-Box） | `data_out_d`（再逆向，应还原） |
|-----------|----------------------------|--------------------------------|
| 0x01      | 0x7c                       | 0x01                           |
| 0x10      | 0xca                       | 0x10                           |
| 0x02      | 0x77                       | 0x02                           |

即 `data_out_d` 应当在三个时刻分别等于 `0x01`、`0x10`、`0x02`。若不等，说明 `bSbox` 的加解密选择或求逆链路有问题。

> **两处「待本地验证」的隐患**（继续 [u2-l1](u2-l1-aes-top-architecture.md) 的批判性阅读）：
> - testbench 里两个例化都起名 `sbox_e`（[tb_s_box.v:31-32](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/tb/tb_s_box.v#L31-L32)），这是非法的例化名重复，严格工具会报错，需把第二个改成如 `sbox_d`。
> - testbench 没有写 `$display` 断言，只能靠看波形判断，不会自动报 pass/fail。
>
> 因此本实践结论标注为「**待本地验证**」：请自行修正例化名后再跑，以仿真实测为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bSbox` 只需要**一个**求逆模块 `gf_inv_8`，就能同时支持加密和解密？

**参考答案**：因为 AES 的正向 S-Box 与逆向 S-Box 共享同一个 \( GF(2^8) \) 求逆运算，差别只在求逆「之前 / 之后」的仿射与换基系数。电路用两个 `select_not_8` 按 `encrypt` 选不同的系数（B/Y 与 D/X），复用中间那一个 `gf_inv_8`，所以一份求逆电路即可。

**练习 2**：标准 S-Box 里 `S[0x00]` 等于多少？为什么是这个值？

**参考答案**：`S[0x00] = 0x63`。因为规定 0 在 \( GF(2^8) \) 中的逆元仍是 0，再对 0 做仿射变换 \( b \mapsto Mb \oplus c \)，其中常数 \( c = \mathtt{0x63} \)，所以结果就是 `0x63`。这也解释了 `aes_sub_byte.v` 里那个魔数 `8'h63` 的来历。

---

### 4.2 aes_s_box128：16 字节并行 S-Box

#### 4.2.1 概念说明

SubBytes 要对状态的**全部 16 个字节**同时替换。由于每个字节的替换互相独立，最自然的硬件实现就是**并行摆放 16 个单字节 S-Box**，一个时钟周期内完成整张状态的替换。`aes_s_box128` 就是干这件事的「包装层」：它本身不做算法，只负责把 128 位输入切成 16 字节，分别喂给 16 个 `bSbox`，再把 16 个 8 位输出拼回 128 位。

#### 4.2.2 核心流程

```text
128 位输入 A
  ├── 字节0  ──► bSbox s0 ──► 字节0  ┐
  ├── 字节1  ──► bSbox s1 ──► 字节1  │
  ├── ...                             ├─► 拼成 128 位输出 Q
  └── 字节15 ──► bSbox s15──► 字节15 ┘
          （所有 bSbox 共享同一个 encrypt 信号）
```

字节切片用全局宏 `u8_MSB(x):u8_LSB(x)` 完成，即字节 *x* 占据比特 \([8x{+}7 : 8x]\)。

#### 4.2.3 源码精读

模块声明：

[aes_sbox128.v:23-27](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sbox128.v#L23-L27) —— 声明 128 位输入 `A`、模式 `encrypt`、以及 128 位结果 `Q`。

16 个并行例化（节选首尾）：

[aes_sbox128.v:28-31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sbox128.v#L28-L31) —— `s0`~`s3` 处理字节 0~3（即状态第 0 列）。

[aes_sbox128.v:43-46](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sbox128.v#L43-L46) —— `s12`~`s15` 处理字节 12~15（即状态第 3 列）。

顶层里的实际接线：

[aes_top.v:124](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L124) —— `aes_s_box128 sbox_inst(SB_input, encrypt, SB_output);`，把待替换状态 `SB_input` 整体送入，得到 `SB_output`。

> **两个必须知道的「坑」**（再次体现草稿级代码特征）：
> 1. **端口方向笔误**：[aes_sbox128.v:26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sbox128.v#L26) 把 `Q` 声明成了 `input`，但 `Q` 明明是 16 个 `bSbox` 驱动的输出。这是一个会让严格工具报错的笔误，正确应为 `output`。**待本地验证**：你的仿真器 / 综合工具是否容忍它。
> 2. **宏定义笔误的传染**：[aes_sbox128.v:14-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sbox128.v#L14-L24) 自带一份带 `` `ifndef `` 守卫的 `u8_MSB/u8_LSB`，但 `u8_LSB(x)` 被写成了 `8*x1`（多了一个 `1`，错误）。它**靠 [aes_types.v:38-40](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v#L38-L40) 先把正确宏定义好、再由 `` `ifndef `` 跳过这份错误副本**才得以幸免。这正是 [u2-l1](u2-l1-aes-top-architecture.md) 警告过的「重复宏有笔误，以 aes_types.v 为准」——只要工程文件列表里漏了 `aes_types.v`，整套字节切片就会全部错位。

#### 4.2.4 代码实践

**实践目标**：在阅读层面验证「并行 16 例化」的结构，并在仿真层面确认它能一次性替换 16 字节。

**操作步骤**：

1. 数一数 [aes_sbox128.v:28-46](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sbox128.v#L28-L46) 里 `bSbox` 的数量，应为 16 个，且每个的输入 / 输出位段都不重叠、刚好覆盖 128 位。
2. 写一个最小 testbench：给 `A` 赋 16 个已知字节（例如 `00 11 22 ... ff`），`encrypt=1`，读取 `Q`。**注意**：因为 `Q` 端口方向有笔误，可能需要先把 `input` 改成 `output` 才能正确观测（修改请在你自己的工作副本上进行，不要改动仓库源码）。

**预期结果**：每个输出字节都等于对应输入字节查标准 S-Box 的结果（见 4.3 的综合实践表）。

**待本地验证**：因前述端口笔误，能否直接仿真取决于工具，请以本地实测为准。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `encrypt` 从 1 改成 0，`aes_s_box128` 的行为如何变化？

**参考答案**：16 个 `bSbox` 全部切到逆向 S-Box，整模块从 SubBytes 变成 InvSubBytes。这正是解密轮里要用的变换，所以加解密可以复用同一个 `aes_s_box128` 实例。

**练习 2**：`aes_s_box128` 是组合逻辑还是时序逻辑？它会占用多少份「单字节 S-Box」电路？

**参考答案**：纯组合逻辑（只有 `bSbox` 例化和连线，没有时钟 / 寄存器）。它物理上展开了 16 份单字节 S-Box 电路，面积是单字节 S-Box 的 16 倍，换来的是「一个周期替换完整状态」的吞吐。

---

### 4.3 ShiftRows：行循环移位

#### 4.3.1 概念说明

ShiftRows 对状态矩阵的**每一行**做**向左**的循环移位，第 *r* 行（从 0 数）左移 *r* 个字节：

| 行 | 左移量 | 移位前（列序） | 移位后 |
|----|--------|----------------|--------|
| 0  | 0      | `b0 b4 b8 b12` | `b0 b4 b8 b12` |
| 1  | 1      | `b1 b5 b9 b13` | `b5 b9 b13 b1` |
| 2  | 2      | `b2 b6 b10 b14`| `b10 b14 b2 b6` |
| 3  | 3      | `b3 b7 b11 b15`| `b15 b3 b7 b11` |

解密用的 InvShiftRows 则**向右**循环移位 *r* 个字节。注意：因为状态是**按列**排布的，所谓「行移位」在 128 位向量里表现为「跨列的字节搬动」，这正是它在硬件里需要一张重排表的原因。

#### 4.3.2 核心流程

ShiftRows 不做任何运算，只做**字节位置的重排**，因此硬件上就是一组连线（crossbar）：

```text
data_in 的 16 个字节 ──► 按「行左移 r」规则重新编号 ──► Mix_input0..15
        （encrypt=1：左移；encrypt=0：右移，由三元运算符 ?: 选择）
```

本模块把输出字节命名为 `Mix_input0..15`，意思是「这些字节将直接喂给 MixColumns」。注意一个设计细节：**这些 `Mix_input*` 是模块内部 wire，模块声明了 `data_out` 却没有把它连出去**（见 4.3.3 的坑）。

输出字节编号与输入字节的对应关系（加密）：

| 输出字节 | 来自输入字节（encrypt） | 来自输入字节（decrypt） |
|----------|------------------------|------------------------|
| Mix_input0  | 0  | 0  |
| Mix_input1  | 5  | 13 |
| Mix_input2  | 10 | 10 |
| Mix_input3  | 15 | 7  |
| Mix_input4  | 4  | 4  |
| Mix_input5  | 9  | 1  |
| Mix_input6  | 14 | 14 |
| Mix_input7  | 3  | 11 |
| Mix_input8  | 8  | 8  |
| Mix_input9  | 13 | 5  |
| Mix_input10 | 2  | 2  |
| Mix_input11 | 7  | 15 |
| Mix_input12 | 12 | 12 |
| Mix_input13 | 1  | 9  |
| Mix_input14 | 6  | 6  |
| Mix_input15 | 11 | 3  |

> 你可以先遮住「来自输入字节」两列，自己按行移位规则推一遍，再与上表对照——这正是本讲综合实践要做的事。

#### 4.3.3 源码精读

模块声明：

[aes_shift_rows.v:30-34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_shift_rows.v#L30-L34) —— 128 位 `data_in`、模式 `encrypt`、128 位 `data_out`。

第 0 行不移位（最简单的一句）：

[aes_shift_rows.v:56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_shift_rows.v#L56) —— `Mix_input0 = data_in` 的字节 0，对应第 0 行不移位。

第 1 行（左移 1 / 右移 1 的选择）：

[aes_shift_rows.v:57-59](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_shift_rows.v#L57-L59) —— 用 `(encrypt == ENCRIPT) ? 加密源 : 解密源` 的三元运算符，逐字节在左移、右移两套来源间二选一。例如 `Mix_input1`：加密取字节 5（行 1 左移 1 的结果），解密取字节 13（行 1 右移 1 的结果）。

完整的 16 条重排连线（含全部三行的加解密选择）：

[aes_shift_rows.v:56-74](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_shift_rows.v#L56-L74) —— 注意每条 `assign` 都是纯组合，没有任何寄存器；加密分支正是 4.3.2 那张表。

顶层里的实际接线：

[aes_top.v:126](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L126) —— `aes_shift_rows shift_row_inst(reg_sub_byte_lvl, encrypt, SH_out);`，输入是 SubBytes 那一级的寄存器 `reg_sub_byte_lvl`，输出 `SH_out` 随后被 MixColumns 与 AddRoundKey 使用（[aes_top.v:80](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L80)、[aes_top.v:86](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L86)）。

> **关键坑（待本地验证）**：通读全文你会发现，模块**只计算了内部 wire `Mix_input0..15`，却没有任何一条语句把 `data_out` 接上这些 wire**（如 `assign data_out = {Mix_input15, ..., Mix_input0};` 是缺失的）。这意味着按字面源码，`data_out`（即顶层 `SH_out`）处于悬空 / 未驱动状态，整条数据通路会失效。重排**逻辑**本身（4.3.2 的表）是正确且完整的，缺的只是最后一步「把内部 wire 拼接到输出端口」。**请在本机仿真 `aes_top` 时重点确认这一点**：若 `SH_out` 全 X / 全 Z，多半就是这个输出未赋值导致的，需要你（在自己的副本里）补上拼接语句再验证。这是本仓库「草稿级 RTL」最典型的例子，也再次印证了「读源码 + 跑仿真」缺一不可。

#### 4.3.4 代码实践

**实践目标**：用一张最小的状态，手算 ShiftRows 的加密输出，并与本模块的连线表逐字节核对。

**操作步骤**：

1. 取一个 16 字节状态，按列填入矩阵（字节编号 0~15）：

```
列→     0      1      2      3
行↓
0     byte0  byte4  byte8  byte12
1     byte1  byte5  byte9  byte13
2     byte2  byte6  byte10 byte14
3     byte3  byte7  byte11 byte15
```

2. 对每一行做左移 *r*，写出新的 4×4 矩阵，再按列读回，得到输出字节 0~15。
3. 把你的结果与 [aes_shift_rows.v:56-74](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_shift_rows.v#L56-L74) 加密分支里的字节来源逐一对照。

**预期结果**：你的手算结果应与 4.3.2 的「来自输入字节（encrypt）」列完全一致——这同时验证了源码重排逻辑的正确性。

**待本地验证**：因 4.3.3 指出的 `data_out` 未赋值问题，端到端仿真需先补上输出拼接语句；补完后，对任意输入，模块输出都应符合上表。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 2 行的加密（左移 2）和解密（右移 2）取的字节来源**完全一样**？

**参考答案**：一行只有 4 个字节。左移 2 等价于右移 2（因为 \( 4-2=2 \)），所以行 2 的正逆 ShiftRows 结果相同。对应代码里 `Mix_input2`、`Mix_input6`、`Mix_input10`、`Mix_input14` 这几行的 `?:` 两边取的是同一个字节。

**练习 2**：ShiftRows 用「跨列搬字节」来实现「行移位」。如果状态当初是**按行**而不是按列填入 128 位向量的，重排表会变成什么样？

**参考答案**：若按行填，同一行的 4 个字节在向量里就是连续的，行移位就退化为「向量内 8 位片段的组内循环移位」，重排会简单很多；但代价是 MixColumns（按列运算）会变复杂。AES 统一采用按列排布，是为了让 MixColumns 这种「 diffusion 主力」实现更直观，于是把复杂性留给了 ShiftRows 的跨列重排。

---

## 5. 综合实践

把 SubBytes 与 ShiftRows 串起来，做一次完整的「手算 + 对源码」练习。这也是本讲规格里要求的综合任务。

**输入状态**（16 字节，128 位，`encrypt = 1`）：

```
字节序:  00 11 22 33 44 55 66 77 88 99 aa bb cc dd ee ff
```

按列填入矩阵后为：

```
列→     0      1      2      3
行↓
0     0x00   0x44   0x88   0xcc
1     0x11   0x55   0x99   0xdd
2     0x22   0x66   0xaa   0xee
3     0x33   0x77   0xbb   0xff
```

**第 1 步：SubBytes**（查标准 S-Box，逐字节替换）。由于本例特意选了「行号 = 列号」的对角字节，查表很省事：

| 字节 | 0x00 | 0x11 | 0x22 | 0x33 | 0x44 | 0x55 | 0x66 | 0x77 |
|------|------|------|------|------|------|------|------|------|
| S-Box| 0x63 | 0x82 | 0x93 | 0xc3 | 0x1b | 0xfc | 0x33 | 0xf5 |

| 字节 | 0x88 | 0x99 | 0xaa | 0xbb | 0xcc | 0xdd | 0xee | 0xff |
|------|------|------|------|------|------|------|------|------|
| S-Box| 0xc4 | 0xee | 0xac | 0xea | 0x4b | 0xc1 | 0x28 | 0x16 |

替换后矩阵（仍按列）：

```
列→     0      1      2      3
行↓
0     0x63   0x1b   0xc4   0x4b
1     0x82   0xfc   0xee   0xc1
2     0x93   0x33   0xac   0x28
3     0xc3   0xf5   0xea   0x16
```

对照源码：这一步等价于 `aes_s_box128` 对每个字节各跑一个 `bSbox`（[aes_sbox128.v:28-46](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_sbox128.v#L28-L46)）。

**第 2 步：ShiftRows**（每行左移 *r*）。

- 行 0 不变：`0x63 0x1b 0xc4 0x4b`
- 行 1 左移 1：`0xfc 0xee 0xc1 0x82`
- 行 2 左移 2：`0xac 0x28 0x93 0x33`
- 行 3 左移 3：`0x16 0xc3 0xf5 0xea`

再按列读回，得到输出字节序（0~15）：

```
0x63 0xfc 0xac 0x16 0x1b 0xee 0x28 0xc3 0xc4 0xc1 0x93 0xf5 0x4b 0x82 0x33 0xea
```

**第 3 步：对照源码连线表**验证。把上一步每个输出位置回推到「它来自哪个输入字节」，应当与 [aes_shift_rows.v:56-74](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_shift_rows.v#L56-L74) 的加密分支一一吻合，例如：

- 输出字节 1 = 0xfc，来自输入字节 5 = 0xfc ✓（对应 `Mix_input1` 加密分支取字节 5）
- 输出字节 3 = 0x16，来自输入字节 15 = 0x16 ✓（`Mix_input3` 取字节 15）
- 输出字节 13 = 0x82，来自输入字节 1 = 0x82 ✓（`Mix_input13` 取字节 1）

**进阶（可选）**：把 `encrypt` 改成 0，用 InvSubBytes（逆向 S-Box）+ InvShiftRows（右移）再算一遍。若你把上一步的密文当作输入，应当能还原回原始的 `00 11 ... ff`——这验证了 SubBytes 与 ShiftRows 在加解密上的可逆性。

> 说明：本综合实践是「手算 + 对源码」型的，所有中间值均可与公开的 AES 标准（FIPS-197）对照。要把它变成「上板 / 仿真」型实践，可基于 `tb_s_box.v` 扩写一个针对 `aes_s_box128` 与 `aes_shift_rows` 的 testbench；但务必先处理 4.2.3、4.3.3 指出的端口笔误与输出未赋值问题（在你的工作副本上），结论以本地仿真为准。

## 6. 本讲小结

- **SubBytes** 用一张非线性 S-Box 对状态每个字节独立替换，是 AES 安全性的来源；本项目的 `bSbox` 用 Canright 复合域算法实现，并靠 `encrypt` 信号在正向 / 逆向 S-Box 间切换，**加解密共用一套求逆电路**。
- **`aes_s_box128`** 把 16 个 `bSbox` 并行摆放，一个周期完成整张状态的 SubBytes；它是纯组合、面积换吞吐的包装层。
- **ShiftRows** 不做运算，只做按行的字节循环移位（加密左移、解密右移），在 128 位向量里表现为跨列的字节重排；本项目用一组带 `?:` 的组合连线实现，加解密差异体现在三元运算符的两条分支里。
- 状态的**按列排布**是理解 ShiftRows 的钥匙：字节 *x* 处于 \((row = x \bmod 4,\ col = \lfloor x/4 \rfloor)\)。
- 本仓库为「草稿级 RTL」：`aes_sub_byte.v` 是未完成、未被顶层使用的替代方案；`aes_sbox128.v` 有 `Q` 端口方向笔误与宏定义笔误；`aes_shift_rows.v` 计算了重排 wire 却**未把 `data_out` 接出**。这些都要靠仿真核实——延续 [u1-l3](u1-l3-vivado-project-template.md)、[u2-l1](u2-l1-aes-top-architecture.md) 确立的「批判性阅读」方法。
- 宏 `u8_MSB/u8_LSB` 在多个文件里被重复定义且带笔误，正确版本唯一存在于 `aes_types.v`，工程文件列表绝不能漏掉它。

## 7. 下一步学习建议

- **横向**：本讲只讲了「字节级」的两个变换。下一讲 [u2-l3 AES MixColumns 与 GF(2^8) 乘法](u2-l3-aes-mixcolumns-gf.md) 将讲「列级」的 MixColumns——它与 ShiftRows 配合完成扩散，并首次正式展开 \( GF(2^8) \) 的乘法运算（`x_times`、`aes_mix_columns_mul`），是本讲 `bSbox` 内部那段「异或树」的数学底座。
- **纵向**：若你想彻底弄懂 `bSbox` 里那些 `R1..R9`、`T1..T10` 异或树是怎么来的，请直接进入 [u2-l5 S-Box 的复合域 GF(2^4) 高效实现](u2-l5-aes-sbox-composite-field.md)，那里会拆解 `gf_inv_8 → gf_inv_4 → gf_mul_4 → gf_scl_4` 的递归求逆结构。
- **配套阅读**：密钥如何在每轮提供 round key，见 [u2-l4 密钥扩展 Key Schedule](u2-l4-aes-key-schedule.md)；这些变换最终如何被 AXI 接口包装成可被处理器驱动的 IP，见 Unit 3（[u3-l1](u3-l1-vivado-ip-structure.md) 起）。
- **建议动手**：在进入下一讲前，先把本讲的「手算 SubBytes + ShiftRows」练到不查表也能推，再写一个最小 testbench 验证 `bSbox` 的加解密往返——这会让你对「为什么 AES 这样设计」有肌肉记忆。
