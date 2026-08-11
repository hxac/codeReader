# MixColumns 与 GF(2^8) 乘法运算

> 所属单元：Unit 2 AES 加密核心数据通路
> 依赖讲义：u2-l1（Verilog 基础与 AES 顶层架构）
> 本讲是 AES 数据通路的「列级扩散」专题，承接 u2-l2 的字节级变换，向下深入到伽罗瓦域算术。

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 AES 的 **MixColumns（列混淆）** 在数学上到底做了什么：把状态矩阵的每一列，当作 GF(2⁸) 上的多项式，与一个固定多项式相乘。
- 理解 **GF(2⁸) 伽罗瓦域** 的基本运算：加法 = 异或（XOR），乘法 = 多项式乘法后对一个不可约多项式取模。
- 掌握 **xtime（×2）** 运算的位运算实现，并看懂为什么「左移一位 + 条件异或 0x1B」就等价于「乘 2 取模」。
- 读懂本仓库的四个源文件：`aes_mix_columns.v`、`aes_mix_columns_mul.v`、`x_times.v`、`x_time_square.v`，并能指出它们各自的设计思路。
- 像上一讲一样，**带着批判眼光读源码**：本仓库是草稿级 RTL，MixColumns 通路同样存在未完成之处（最典型的是 `x_times.v` 把模约简注释掉了），本讲会带你一一识别。

## 2. 前置知识

### 2.1 为什么要 MixColumns

上一讲的 SubBytes（S-Box）给 AES 带来了**非线性**（混淆，confusion）；ShiftRows（行移位）把不同列的字节打散到不同列。但仅有这些还不够：如果一列里的字节在变换后仍然只影响自己这一列，那么密文的每一位只会依赖少数几个明文/密钥位，密码会被差分/线性攻击击破。

MixColumns 的作用是 **扩散（diffusion）**：让一列里的**每一个输出字节都同时依赖这一列的全部 4 个输入字节**。这样明文里 1 个比特的变化，经过若干轮后会「雪崩」式地影响整个状态。MixColumns 与 ShiftRows 配合，保证 AES 在 2 轮后，每个状态位都依赖 16 个明文位（即所谓的「雪崩准则」）。

### 2.2 伽罗瓦域 GF(2⁸) 是什么

普通算术里，字节 `0x05` 就是一个整数 5。但在 AES 里，一个字节被解释成一个**系数在 GF(2)（即 {0,1}，加法为 XOR）上的 7 次多项式**：

\[ b_7b_6\cdots b_1b_0 \;\longleftrightarrow\; b_7 x^7 + b_6 x^6 + \cdots + b_1 x + b_0 \]

例如 `0x57` = `0101_0111` 对应多项式 \( x^6 + x^4 + x^2 + x + 1 \)。

- **加法**：多项式对应系数相加，在 GF(2) 里就是 XOR。所以 GF(2⁸) 的加法 = 字节 XOR。
- **乘法**：先按多项式乘法展开，结果最高可能到 \( x^{14} \) 次；再对一个 **8 次不可约多项式** 取模，把结果压回 8 位。

AES 选定的不可约多项式是：

\[ m(x) = x^8 + x^4 + x^3 + x + 1 \]

写成十六进制是 `0x11B`。注意它有 9 位（含 \( x^8 \)），其低 8 位 `0x1B` = `0001_1011` 就是本仓库宏 [`POLYNOMIAL_IRR`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v#L24) 的值。

> **术语解释：不可约多项式（irreducible polynomial）**。在多项式环里，「不可约」类比整数里的「质数」——不能被分解成两个次数更低的多项式之积。选定一个 8 次不可约多项式取模后，所有 8 位字节在这套加/乘运算下构成一个有 256 个元素的域（field），即 GF(2⁸)。AES 选 \( m(x) \) 并非随意，它是 GF(2⁸) 上众多不可约多项式之一，标准里固定不变。

### 2.3 关键工具：xtime（乘以 2）

MixColumns 矩阵里出现的常数乘子是 2 和 3。3·a 又可以拆成 `(2·a) ⊕ a`。所以**只要能高效实现「乘以 2」，MixColumns 所需的全部常数乘法就都能搞定**。这个「乘以 2」运算记作 **xtime**：

\[ \text{xtime}(b) = b \cdot x \bmod m(x) \]

直觉上，「乘以 x」就是把多项式的每一项次数 +1，也就是把字节**左移一位**。但如果原字节的最高位 \( b_7 = 1 \)，左移后会出现 \( x^8 \) 项，超出 8 位，必须用 \( x^8 \bmod m(x) = x^4 + x^3 + x + 1 \)（即 `0x1B`）替换掉它。于是：

\[ \text{xtime}(b) = \begin{cases} (b \ll 1) \oplus \texttt{0x1B}, & b_7 = 1 \\ b \ll 1, & b_7 = 0 \end{cases} \]

用一条位运算写出来（`b7 = b >> 7` 取最高位）：

\[ \text{xtime}(b) = \bigl((b \ll 1) \;\oplus\; (b_7 \cdot \texttt{0x1B})\bigr) \mathbin{\&} \texttt{0xFF} \]

这就是后面 `x_times.v` 想要实现的东西。

## 3. 本讲源码地图

本讲涉及的关键文件都在 AES 核心目录下：

| 文件 | 作用 | 是否被顶层使用 |
| --- | --- | --- |
| [hdl/src/aes_mix_columns.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_mix_columns.v) | 模块 `MixColumns`：对**一列 4 字节**做列混淆，同时给出加密 (`a0..a3`) 与解密 (`c0..c3`) 两套输出 | **是**，被 `aes_top` 例化 4 次（每列一次） |
| [hdl/utils/aes_mix_columns_mul.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_mix_columns_mul.v) | 模块 `MixColumnMul`：另一种（更直白的矩阵乘法式）写法 | **否**，全仓库未被任何模块例化，属备选草稿 |
| [hdl/utils/x_times.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/x_times.v) | 模块 `x_times`：实现 GF(2⁸) 的「乘以 2」（xtime） | 被 `MixColumns` 例化 4 次 |
| [hdl/utils/x_time_square.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/x_time_square.v) | 模块 `x_time_square`：用纯组合逻辑（按位表达式）实现某个 GF(2⁸) 常数乘 | 被 `MixColumns` 在解密通路例化 2 次 |

补充参考（非本讲最小模块，但有助于对照「正确答案」）：

| 文件 | 作用 |
| --- | --- |
| [hdl/utils/aes_types.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v) | 定义 `POLYNOMIAL_IRR = 0x1B` 等全局宏 |
| [hdl/VE_sv/ve_AES_Core.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv) | 验证环境里的**黄金参考模型**，含正确的 `MixColumn` 函数与 `mul` 函数，可作为「标准答案」对照 |

> 注意：`aes_top.v` 例化的是 `MixColumns`（来自 `aes_mix_columns.v`），**不是** `MixColumnMul`。这一点很重要——意味着我们要精读的「生效」实现是 `aes_mix_columns.v`，而 `aes_mix_columns_mul.v` 只是一个未被启用的备选草稿。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：
1. **MixColumns 的数学原理**（域、矩阵、为什么这么乘）；
2. **xtime（×2）的硬件实现**（`x_times.v` 与 `x_time_square.v`）；
3. **两种列混淆实现对照**（生效的 `aes_mix_columns.v` 与备选的 `aes_mix_columns_mul.v`）。

---

### 4.1 MixColumns 的数学原理（最小模块：MixColumns）

#### 4.1.1 概念说明

MixColumns 把状态的**一列**（4 个字节 \( s_0, s_1, s_2, s_3 \)，从上到下）看成一个 GF(2⁸) 上的 3 次多项式：

\[ c(x) = s_3 x^3 + s_2 x^2 + s_1 x + s_0 \]

然后乘以一个**固定的**多项式 \( a(x) \)：

\[ a(x) = \{03\} x^3 + \{01\} x^2 + \{01\} x + \{02\} \]

并且这个乘法要再对一个特殊的多项式 \( x^4 + 1 \) 取模（因为列只有 4 字节，次数要压回 3 次）。由于 \( x \equiv 1 \pmod{x^4+1} \)，等价地，这个「列乘法」可以写成一组**循环移位的 GF(2⁸) 乘加**，最终化简为一个 4×4 的矩阵乘法：

\[ \begin{bmatrix} r_0 \\ r_1 \\ r_2 \\ r_3 \end{bmatrix} = \begin{bmatrix} 02 & 03 & 01 & 01 \\ 01 & 02 & 03 & 01 \\ 01 & 01 & 02 & 03 \\ 03 & 01 & 01 & 02 \end{bmatrix} \begin{bmatrix} s_0 \\ s_1 \\ s_2 \\ s_3 \end{bmatrix} \]

其中每一行的「乘」是 GF(2⁸) 乘法，「加」是 XOR。展开第 0 行：

\[ r_0 = 02\cdot s_0 \;\oplus\; 03\cdot s_1 \;\oplus\; 01\cdot s_2 \;\oplus\; 01\cdot s_3 \]

注意矩阵的**循环结构**：每一行只是上一行右移一位。这正是「乘以 \( a(x) \) 再模 \( x^4+1 \)」的体现。这个矩阵是可逆的（解密用的 InvMixColumns 用其逆矩阵，常数变为 `{0e,0b,0d,09}`）。

仓库的验证环境 `ve_AES_Core.sv` 里给出了与标准完全一致的参考实现，可以直接对照「正确写法」：

```systemverilog
res[i][j] = mul(2, a[i][j])
          ^ mul(3, a[(i+1) % 4][j])
          ^ a[(i + 2) % 4][j]
          ^ a[(i + 3) % 4][j];
```

参见 [ve_AES_Core.sv:111-137](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv#L111-L137)，这里调用的 `mul` 是用对数/反对数表实现的**正确的** GF(2⁸) 乘法，见 [ve_AES_Core.sv:48-60](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv#L48-L60)。

#### 4.1.2 核心流程

对一列 4 字节做 MixColumns 的步骤：

1. 取列 \( (s_0, s_1, s_2, s_3) \)。
2. 计算 4 个「乘 2」：\( t_k = \text{xtime}(s_k) \)（即 \( 02\cdot s_k \)）。
3. 计算 4 个「乘 3」：\( 03\cdot s_k = t_k \oplus s_k \)。
4. 按矩阵把乘积 XOR 起来得到 \( r_0..r_3 \)。

伪代码：

```
for i in 0..3:
    r[i] = xtime(s[i])               # 2 * s[i]
         ^ (xtime(s[i]) ^ s[i])      # 3 * s[(i+1)%4]   —— 注意行循环
         ^ s[(i+2)%4]
         ^ s[(i+3)%4]
```

> 工程上还有一种**更省硬件**的写法：不直接算 4 个独立的 xtime，而是先算相邻字节的「差」\( s_i \oplus s_j \)，对「差」做一次 xtime，再用 XOR 还原。这种写法能把 4 个 xtime 共享、减少面积，正是本仓库 `aes_mix_columns.v` 采用的思路（见 4.3）。

#### 4.1.3 源码精读

MixColumns 在顶层是如何挂上的，见 [aes_top.v:130-141](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L130-L141)（第 0 列的例化）：

```verilog
MixColumns mix_inst0(MC_input[`u8_MSB(0):`u8_LSB(0)],   // b0
                     MC_input[`u8_MSB(1):`u8_LSB(1)],   // b1
                     MC_input[`u8_MSB(2):`u8_LSB(2)],   // b2
                     MC_input[`u8_MSB(3):`u8_LSB(3)],   // b3
                     MC_output_e[`u8_MSB(0):`u8_LSB(0)], ...  // a0..a3 加密输出
                     MC_output_d[`u8_MSB(0):`u8_LSB(0)], ...); // c0..c3 解密输出
```

要点：

- 128 位状态被宏 `u8_MSB/u8_LSB` 切成 16 字节（见 u2-l1）。每 4 字节一组送进一个 `MixColumns`，所以 `aes_top` 例化了 **4 个** `mix_inst0..3`（[aes_top.v:130-182](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L130-L182)），分别处理第 0、1、2、3 列。
- 一个 `MixColumns` 模块**同时**给出加密输出 `MC_output_e`（接 `a0..a3`）和解密输出 `MC_output_d`（接 `c0..c3`）。顶层在 [aes_top.v:107-108](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L107-L108) 把它们分别送进 `reg_mix_col_lvl`（加密通路）和 `reg_inv_mix_col_lvl`（解密通路），再用 `encrypt` 选择，实现加解密共用数据通路（这是 u2-l1 讲过的反馈式结构）。

#### 4.1.4 代码实践

**实践目标**：手算一列 MixColumns，建立对「矩阵乘法 + GF 乘法」的直觉。

**操作步骤**：

1. 取一列 \( (s_0,s_1,s_2,s_3) = (\texttt{0xdb}, \texttt{0x13}, \texttt{0x53}, \texttt{0x45}) \)（这是 FIPS-197 附录 B 里的经典示例列）。
2. 对每个字节手算 xtime（注意 `0xdb` 最高位是 1，需要约简！）：
   - `xtime(0xdb)`：`0xdb<<1 = 0x1B6`，最高位为 1 故异或 `0x1B` → `0x1B6 ^ 0x1B = 0x1AD`，取低 8 位 = `0xAD`。
   - `xtime(0x13) = 0x26`（最高位 0，不约简）。
   - `xtime(0x53) = 0xA6`。
   - `xtime(0x45) = 0x8A`。
3. 按 \( r_0 = 02\cdot s_0 \oplus 03\cdot s_1 \oplus s_2 \oplus s_3 \) 等四行 XOR。

**预期结果**（这一列的标准答案，可对任意标准 AES 实现复现）：

\[ (s_0,s_1,s_2,s_3) = (\texttt{db},\texttt{13},\texttt{53},\texttt{45}) \;\longrightarrow\; (r_0,r_1,r_2,r_3) = (\texttt{8e},\texttt{4d},\texttt{a1},\texttt{bc}) \]

详细推导见本讲「5. 综合实践」。如果待本地验证，可用 Python `pycryptodome` 或在线 AES 计算器核对。

#### 4.1.5 小练习与答案

**练习 1**：MixColumns 矩阵里为什么只出现 1、2、3 这三个常数？为什么没有 4、5？

> **答案**：因为 AES 把 MixColumns 定义为「列多项式乘以固定多项式 \( a(x)=03x^3+01x^2+01x+02 \)」，该多项式的 4 个系数就是 {02, 03, 01, 01}。展开成矩阵后每行只是系数循环移位，所以整张矩阵只用到 1、2、3。更高的常数（如 0x09、0x0e）出现在 **InvMixColumns**（解密）的逆矩阵里。

**练习 2**：MixColumns 是线性的还是非线性的？去掉它会怎样？

> **答案**：完全线性（GF(2⁸) 上的矩阵乘法 + XOR）。它与线性的 ShiftRows、非线性的 SubBytes 互补：SubBytes 提供非线性（混淆），MixColumns 提供扩散。若去掉 MixColumns，每个状态位的影响力无法在一轮内扩散到整列，AES 将无法满足雪崩准则，安全性急剧下降。

---

### 4.2 xtime（×2）的硬件实现（最小模块：x_times）

#### 4.2.1 概念说明

上一节我们看到，MixColumns 的所有常数乘法都能归结为 xtime（乘 2）。xtime 在硬件里非常便宜：一次左移 + 一次条件 XOR。本仓库提供了两个相关模块：

- `x_times.v`：号称实现「乘以 2」，**但本仓库这版其实不完整**——它只做了左移，把模约简（异或 0x1B）注释掉了。
- `x_time_square.v`：用一串按位赋值实现某个 GF(2⁸) 常数乘，被解密通路使用。

读这两个文件的核心目的，除了学 xtime 的位运算技巧，更是**练习「对照标准、识别草稿」**的源码阅读能力。

#### 4.2.2 核心流程

正确的 xtime 流程（一位判别 + 条件异或）：

```
输入 b[7:0]
tmp = b << 1            // 左移一位，tmp 是 9 位，最高位 tmp[8] = b[7]
if (b[7] == 1)
    out = tmp[7:0] ^ 0x1B   // 有 x^8 项，用 0x1B 替换
else
    out = tmp[7:0]          // 没溢出，直接取
```

用一条位运算（无分支，适合硬件）：

\[ \text{out} = \bigl((b \ll 1) \oplus (\{8\{b_7\}\} \mathbin{\&} \texttt{0x1B})\bigr)[7:0] \]

其中 ` {8{b7}} ` 是把 b7 复制成 8 位（Verilog 写法）。这一句的逻辑是：只有当 b7=1 时，`{8{b7}} & 0x1B` 才等于 0x1B，否则为 0——正好实现了上面的 if/else。

#### 4.2.3 源码精读

先看 `x_times.v`，文件头注释明确写了它的用途是「GF(2⁸) 里乘以 02，用于 MixColumn」：

[x_times.v:20-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/x_times.v#L20-L35)

```verilog
module x_times(data_in, data_out);
    input  [7:0] data_in;
    output [7:0] data_out;

    wire [7:0] mul_02;   // 乘以 02，注释说「implemented as a shift with one position」
    //wire [7:0] reduction;  // 模约简：x^8+x^4+x^3+x+1 = 0001_1011

    assign mul_02 = (data_in << 1);     // ← 只左移
    //assign reduction = mul_02 ^ `POLYNOMIAL_IRR;   // ← 约简被注释掉了！

    assign data_out = mul_02;           // ← 输出就是左移结果
endmodule
```

**这段代码做了什么**：把输入字节左移一位，丢弃最高位（因为 `data_out` 只有 8 位），**完全没有条件异或 0x1B**。也就是说：

\[ \text{本仓库 } \texttt{x\_times}(b) = (b \ll 1) \mathbin{\&} \texttt{0xFF} \neq \text{xtime}(b) \]

**它和正确 xtime 的差距**：当输入最高位 `b7 = 0` 时两者一致；当 `b7 = 1` 时（即字节 ≥ 0x80），正确 xtime 还要再异或 0x1B，而本仓库没有。例如：

| 输入 b | 正确 xtime(b) | 本仓库 x_times(b) | 是否一致 |
| --- | --- | --- | --- |
| `0x13`（b7=0） | `0x26` | `0x26` | ✅ 一致 |
| `0x53`（b7=0） | `0xA6` | `0xA6` | ✅ 一致 |
| `0xdb`（b7=1） | `0xAD` | `0xB6` | ❌ 不一致（少了 `^0x1B`） |

> **草稿警示（承接 u2-l1/u2-l2）**：作者显然**知道**要做约简——注释里写了「modular reduction with … = 0001_1011」，还留了一行被注释掉的 `reduction = mul_02 ^ POLYNOMIAL_IRR`，但最终没接到 `data_out` 上。这是一处典型的「TODO 未完成」。如果你要把这个核心用于真实加解密，**必须自己补上约简**（见 4.2.4 实践）。这也再次印证：本仓库是供阅读/学习的草稿，不是经过验证的产品级核心。

再看 `x_time_square.v`，它用一串**按位赋值**实现一个 GF(2⁸) 上的线性映射（某个常数乘）：

[x_time_square.v:21-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/x_time_square.v#L21-L35)

```verilog
module x_time_square(data_in, data_out);
    input  [7:0] data_in;
    output [7:0] data_out;

    assign data_out[7] = data_in[5];
    assign data_out[6] = data_in[4];
    assign data_out[5] = data_in[7] ^ data_in[3];
    assign data_out[4] = data_in[2] ^ data_in[6] ^ data_in[7];
    assign data_out[3] = data_in[6] ^ data_in[1];
    assign data_out[2] = data_in[7] ^ data_in[0];
    assign data_out[1] = data_in[6] ^ data_in[7];
    assign data_out[0] = data_in[0];
endmodule
```

**这段代码做了什么**：它把输出每一位都写成输入若干位的 XOR——这是 GF(2⁸) 上「乘以一个固定常数」的标准组合电路展开（因为 GF(2⁸) 乘法对每个输出位都是输入位的线性函数，可直接用 XOR 门实现，无需乘法器）。文件头注释写「Multiplication with 02」其实是**笔误/复制的模板**——从真值表反推（令输入 = 1，即 `data_in[0]=1`，得到 `data_out = 0x05`），它实现的是乘以 `0x05` 这一类的常数乘，而非乘 2。它被 `MixColumns` 的解密通路（`c0..c3`）使用。

> 这种「按位展开常数乘」的写法值得学习：在 FPGA 上，GF(2⁸) 常数乘不需要 DSP、不需要查表，几级 LUT/XOR 就能完成，非常省资源。判断它到底乘了哪个常数，最简单的办法就是代入 `b = 1` 看输出（如上得到 `0x05`）。

#### 4.2.4 代码实践

**实践目标**：亲手把 `x_times.v` 改成「正确的 xtime」，并在仿真里验证 `0xdb → 0xAD`。

**操作步骤**（仅作为阅读练习提出修改方案，**不要改动仓库源码**，可在自己的副本里试验）：

1. 把 `assign mul_02 = (data_in << 1);` 之后的注释行替换为真正的条件约简。一种等价写法：
   ```verilog
   wire msb = data_in[7];
   assign data_out = (data_in << 1) ^ (msb ? 8'h1B : 8'h00);
   ```
2. 写一个最小 testbench，对 `8'd0` 到 `8'd255` 全扫描，把 DUT 输出与你用 C/Python 写的正确 xtime 对照。

**需要观察的现象**：

- 修改前：`x_times(8'hdb)` = `8'hb6`（错误）。
- 修改后：`x_times(8'hdb)` = `8'had`（正确）。
- `b7=0` 的输入（如 `0x13`）修改前后都是 `0x26`，不受影响——验证了「约简只在最高位为 1 时触发」。

**预期结果**：全扫描 256 个输入，DUT 与参考函数逐位一致，则 xtime 修正完成。

> 若手头没有仿真器，本实践可降级为「源码阅读型」：在纸上对 `0xdb` 走一遍修正后的逻辑，确认得到 `0xAD` 即可（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：用一条无分支位运算写出正确 xtime（不使用 `? :`）。

> **答案**：`assign data_out = (data_in << 1) ^ ({8{data_in[7]}} & 8'h1B);`。当 `data_in[7]=0` 时 `{8{0}}&0x1B=0`，等价于不约简；为 1 时 `& 0x1B = 0x1B`，触发约简。

**练习 2**：`x_times.v` 里那行被注释的 `reduction = mul_02 ^ POLYNOMIAL_IRR` 即便取消注释、并改成 `data_out = reduction`，仍然不是正确的 xtime。为什么？

> **答案**：因为它**无条件**异或 0x1B。正确的 xtime 是「仅当最高位为 1 时才异或 0x1B」。无条件异或会对所有输入都约简，导致 `b7=0` 的输入（本不需要约简）被错误地多异或了一次 0x1B。必须加上 `data_in[7]` 的判别。

**练习 3**：`x_time_square.v` 代入输入 `8'h01` 得到输出多少？由此推测它实现的是乘以哪个常数？

> **答案**：`data_in=0x01` 时只有 `data_in[0]=1`，按位算得 `data_out[2]=1`、`data_out[0]=1`，其余为 0 → `data_out = 0b00000101 = 0x05`。因为「乘以常数 c」会把 1 映射到 c 本身，故该模块实现的是 GF(2⁸) 上乘以 `0x05`（属于解密 InvMixColumns 所需常数族的一员）。

---

### 4.3 两种列混淆实现对照（最小模块：aes_mix_columns_mul）

#### 4.3.1 概念说明

本仓库给了**两种** MixColumns 写法，风格迥异：

1. `aes_mix_columns.v`（模块 `MixColumns`）——**面积优化写法**。它不直接算 4 个独立 xtime，而是利用「相邻字节之差」共享 xtime，并额外用 `x_time_square` 一次性算出解密通路所需的常数乘。一个模块同时输出加密 (`a0..a3`) 和解密 (`c0..c3`) 两组结果。**这是顶层实际使用的版本。**
2. `aes_mix_columns_mul.v`（模块 `MixColumnMul`）——**直白矩阵写法**。它声明每个输入字节要乘的常数（加密 {2,3,1,1}，解密 {0e,0b,0d,09}），用 `dec` 信号在两套常数间切换。**但它的「乘法」实现是错的**（详见 4.3.3），且**从未被任何模块例化**，属备选草稿。

对照这两种写法，能让你理解同一个数学操作在硬件上可以有多种实现风格，以及「代码意图」与「代码实际行为」可能脱节。

#### 4.3.2 核心流程

`MixColumns`（生效版）对一列 \( b_0,b_1,b_2,b_3 \) 的处理：

1. 算 4 个「相邻差」：\( x_0 = b_3 \oplus b_0,\; x_1 = b_1 \oplus b_0,\; x_2 = b_2 \oplus b_1,\; x_3 = b_3 \oplus b_2 \)。
2. 对每个差做一次 xtime：\( x_k' = \text{xtime}(x_k) \)。
3. 用 XOR 把 xtime 结果与原字节组合，得到加密输出 \( a_0..a_3 \)。
4. 解密输出 \( c_0..c_3 \) 在 \( a \) 的基础上，再用 `x_time_square` 做一次常数乘并 XOR 回去。

> 这种「先 XOR 再 xtime」的等价性来自 GF(2⁸) 的分配律：\( 02\cdot(u\oplus v) = 02\cdot u \oplus 02\cdot v \)。标准矩阵乘 \( r_0 = 02s_0 \oplus 03s_1 \oplus s_2 \oplus s_3 \) 可改写成关于「差」的形式，从而共享 xtime 实例，节省门数。

#### 4.3.3 源码精读

先看生效的 `aes_mix_columns.v`（注意它 `include` 的不是本模块独有头文件，端口直接罗列）：

[aes_mix_columns.v:17-49](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_mix_columns.v#L17-L49)

```verilog
module MixColumns( b0, b1, b2, b3, a0, a1, a2, a3, c0, c1, c2, c3);
    input    [7:0] b0, b1, b2, b3;        // 一列的 4 个输入字节
    output   [7:0] a0, a1, a2, a3;        // 加密输出
    output   [7:0] c0, c1, c2, c3;        // 解密输出

    wire [7:0] x0, x1, x2, x3;            // 相邻差
    wire [7:0] x0_out, x1_out, x2_out, x3_out;  // 差的 xtime
    wire [7:0] z0, z1;                    // x_time_square 输出

    assign x0 = b3 ^ b0;   assign x1 = b1 ^ b0;
    assign x2 = b2 ^ b1;   assign x3 = b3 ^ b2;

    x_times xtime_inst0 (x0, x0_out);     // 4 个 xtime（注意：依赖 x_times.v）
    x_times xtime_inst1 (x1, x1_out);
    x_times xtime_inst2 (x2, x2_out);
    x_times xtime_inst3 (x3, x3_out);

    assign a0 = (x0_out ^ b1) ^ x3;       // 加密输出由 xtime + 原字节组合
    assign a1 = (x1_out ^ b0) ^ x3;
    assign a2 = (x2_out ^ b3) ^ x1;
    assign a3 = (x3_out ^ b2) ^ x1;

    x_time_square xtime_sq0(a3^a1, z0);   // 解密通路再用常数乘
    x_time_square xtime_sq1(a2^a0, z1);

    assign c0 = z1 ^ a0;                  // 解密输出
    assign c1 = z0 ^ a1;
    assign c2 = z1 ^ a2;
    assign c3 = z0 ^ a3;
endmodule
```

**这段代码做了什么**：

- 端口 `b0..b3` 是一列输入，`a0..a3` 是加密 MixColumns 输出，`c0..c3` 是解密（InvMixColumns）输出——单模块双输出。
- 先算 4 个相邻差，各过一个 `x_times`（即 4.2 讨论的「缺约简」xtime），再用 XOR 组合成 `a0..a3`。
- 解密输出 `c0..c3` 在加密输出的基础上，用 `x_time_square` 再做一步常数乘并 XOR 回来。

> **草稿警示**：因为 `a0..a3` 依赖 `x_times`，而 `x_times` 缺约简（4.2），所以只要某列里出现 ≥ 0x80 的字节，这条加密通路的 `a0..a3` 就会偏离标准 MixColumns。换句话说，**当前提交的 AES 核心在加密 MixColumns 上是不正确的**，需要先修好 `x_times.v`（4.2.4）才能用于真实加解密。这一点和 u2-l2 指出的「`aes_shift_rows` 未接出 `data_out`」等隐患同源——都是草稿未完成项。把核心跑通的练习留给后续讲义（如 u3-l5 仿真验证）。

再看备选的 `aes_mix_columns_mul.v`：

[aes_mix_columns_mul.v:21-44](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_mix_columns_mul.v#L21-L44)

```verilog
module MixColumnMul (a_in1, a_in2, a_in3, a_in4, dec, data_out);
    input [7:0] a_in1, a_in2, a_in3, a_in4;
    input dec;            // dec = 1 表示解密
    output [7:0] data_out;

    wire [7:0] mul_en_1 = a_in1 ^ 8'h02;   // ← 意图是「乘以 02」，写成了 XOR
    wire [7:0] mul_en_2 = a_in2 ^ 8'h03;
    wire [7:0] mul_en_3 = a_in3 ^ 8'h01;
    wire [7:0] mul_en_4 = a_in4 ^ 8'h01;

    wire [7:0] mul_dec_1 = a_in1 ^ 8'h0C;  // 解密常数族（注意也写成了 XOR）
    wire [7:0] mul_dec_2 = a_in2 ^ 8'h08;
    wire [7:0] mul_dec_3 = a_in3 ^ 8'h0C;
    wire [7:0] mul_dec_4 = a_in4 ^ 8'h08;

    assign data_out = (mul_en_1 ^ (mul_dec_1 & dec)) ^
                      (mul_en_2 ^ (mul_dec_2 & dec)) ^
                      (mul_en_3 ^ (mul_dec_3 & dec)) ^
                      (mul_en_4 ^ (mul_dec_4 & dec));
endmodule
```

**这段代码做了什么**：它把「GF(2⁸) 乘以常数 c」误写成了「字节与常数 c 异或」——`a_in1 ^ 8'h02` 只是翻转 `a_in1` 的 bit1，绝非 GF 乘法。`& dec` 利用 Verilog 对 1 位与 8 位按位与的扩展，在加密/解密常数族间切换。最终 `data_out` 把四项 XOR 在一起。

> **草稿警示**：这是一个**功能不正确**的 MixColumns 草稿——它把乘法写成了异或。好在它**从未被例化**（顶层用的是 `MixColumns`），所以不影响核心行为。它的价值在于：(1) 展示了「用 `dec` 信号切换加密/解密常数族」的接口设计意图；(2) 作为反面教材，提醒读者 GF(2⁸) 乘法 ≠ 普通按位异或。正确的常数乘应调用 4.2 的 xtime（如 `02·a = xtime(a)`，`03·a = xtime(a)^a`），或用对数表/查表。

#### 4.3.4 代码实践

**实践目标**：用 Python 实现正确的 GF(2⁸) 乘法，对照 `MixColumnMul` 的「错误写法」，直观感受两者的差别。

**操作步骤**：

1. 写一个 `gf_mul(a, b)`（算法：俄式乘法 / shift-and-XOR，见 5. 综合实践的完整代码）。
2. 用它实现正确的单列 MixColumns（输入 4 字节，输出 4 字节）。
3. 对同一列输入，也手算 `MixColumnMul` 的 `data_out`（注意它用的是 XOR，不是乘法）。
4. 比较两者。

**需要观察的现象**：

- 正确的 `gf_mul` + 矩阵：对 \( (\texttt{db},\texttt{13},\texttt{53},\texttt{45}) \) 输出 \( (\texttt{8e},\texttt{4d},\texttt{a1},\texttt{bc}) \)。
- `MixColumnMul` 的写法（XOR）：`data_out = (a_in1^0x02) ^ (a_in2^0x03) ^ (a_in3^0x01) ^ (a_in4^0x01)`（当 `dec=0`），化简后 = `a_in1^a_in2^a_in3^a_in4 ^ 0x01`，完全丢失了「乘法」语义，结果与正确 MixColumns 毫无关系。

**预期结果**：两种实现的输出截然不同，证明 `MixColumnMul` 的「XOR 当乘法」是错误的；而正确的 `gf_mul` 与仓库验证环境 `ve_AES_Core.sv` 的 `mul` 函数一致。

#### 4.3.5 小练习与答案

**练习 1**：`MixColumns`（生效版）为什么要算「相邻差」再做 xtime，而不是对每个字节各做一次 xtime？

> **答案**：为了**节省面积**。利用 GF(2⁸) 分配律 \( 02\cdot(u\oplus v) = 02\cdot u \oplus 02\cdot v \)，标准矩阵里的乘积项可以重组为「差」的形式，从而让多个乘 2 共享 xtime 实例，减少组合逻辑门数。代价是可读性下降，需要数学等价变换才能看懂。

**练习 2**：`MixColumnMul` 里 `mul_en_1 = a_in1 ^ 8'h02` 这行，如果作者本意是「乘以 2」，应该怎么写？

> **答案**：应写成 `wire [7:0] mul_en_1 = xtime(a_in1);`，其中 `xtime(b) = (b<<1) ^ (b[7] ? 8'h1B : 8'h00)`。即调用 4.2 的 GF(2⁸) 乘 2，而不是直接异或常数。

**练习 3**：在顶层 `aes_top.v` 里搜索 `MixColumn`，确认实际例化的是哪个模块？为什么这点很重要？

> **答案**：搜索结果是 `MixColumns mix_inst0..3`（来自 `aes_mix_columns.v`），**没有** `MixColumnMul`。这很重要：它决定了我们精读、修复的重点是 `aes_mix_columns.v`（及其依赖的 `x_times.v`），而 `aes_mix_columns_mul.v` 即便有 bug 也不会进入实际数据通路。读源码时先搞清「哪个模块真的被用到」，能避免在死代码上浪费精力。

---

## 5. 综合实践：用 Python 复现 MixColumns 并核对标准答案

本实践把本讲的数学（GF(2⁸)、xtime、矩阵乘）与源码（`x_times.v` 的缺失约简、`MixColumnMul` 的错误乘法）串起来。

### 5.1 实践目标

- 用 Python 实现正确的 GF(2⁸) 乘法与 MixColumns；
- 对经典列 \( (\texttt{0xdb},\texttt{0x13},\texttt{0x53},\texttt{0x45}) \) 复现标准答案 \( (\texttt{0x8e},\texttt{0x4d},\texttt{0xa1},\texttt{0xbc}) \)；
- 复现仓库 `x_times.v`（无约简）的行为，直观看到它与正确 xtime 的差距。

### 5.2 参考代码（示例代码，非项目原有）

```python
# 示例代码：用于教学对照，非 FPGA_Library 仓库内容
IRR = 0x1B  # m(x) 低 8 位，对应 aes_types.v 的 POLYNOMIAL_IRR

def xtime(b):
    """正确的 GF(2^8) 乘以 2：左移一位，最高位为 1 则异或 0x1B。"""
    b8 = (b >> 7) & 1           # 最高位
    return ((b << 1) ^ (b8 * IRR)) & 0xFF

def xtime_repo(b):
    """模拟仓库 x_times.v 的行为：只左移、不做约简。"""
    return (b << 1) & 0xFF      # 8 位，最高位被丢弃

def gf_mul(a, b):
    """GF(2^8) 乘法（俄式乘法 / shift-and-XOR）。"""
    res = 0
    while b:
        if b & 1:
            res ^= a
        b >>= 1
        a = xtime(a)            # 每轮 a 乘以 2
    return res

def mix_columns_column(col):
    """对一列 4 字节做 MixColumns，返回 4 字节。"""
    s0, s1, s2, s3 = col
    r0 = gf_mul(2, s0) ^ gf_mul(3, s1) ^ s2 ^ s3
    r1 = s0 ^ gf_mul(2, s1) ^ gf_mul(3, s2) ^ s3
    r2 = s0 ^ s1 ^ gf_mul(2, s2) ^ gf_mul(3, s3)
    r3 = gf_mul(3, s0) ^ s1 ^ s2 ^ gf_mul(2, s3)
    return [r0, r1, r2, r3]

col = [0xdb, 0x13, 0x53, 0x45]
print("MixColumns:", [hex(x) for x in mix_columns_column(col)])
# 预期: ['0x8e', '0x4d', '0xa1', '0xbc']

# 对照仓库 x_times.v（无约简）与正确 xtime 的差距
for b in [0x13, 0x53, 0xdb]:
    print(f"xtime({hex(b)}): correct={hex(xtime(b))}, repo={hex(xtime_repo(b))}")
# 预期: 0x13/0x53 两者一致; 0xdb correct=0xad, repo=0xb6（差一个 0x1B）
```

### 5.3 操作步骤

1. 把上面的代码存为 `gf_mix.py`，运行 `python3 gf_mix.py`。
2. 核对 `MixColumns` 输出是否为 `['0x8e', '0x4d', '0xa1', '0xbc']`。
3. 核对 `xtime(0xdb)` 的 `correct=0xad` 而 `repo=0xb6`——这正是 4.2 指出的「`x_times.v` 缺约简」的可复现证据。
4. （进阶）把 `col` 换成全零列 `[0,0,0,0]`，确认输出也是全零（线性变换把零映射到零）；再换成两列互为逆（先用 MixColumns 再用 InvMixColumns），确认能还原。

### 5.4 预期结果与结论

- 正确实现给出标准答案 \( (\texttt{8e},\texttt{4d},\texttt{a1},\texttt{bc}) \)，与 FIPS-197 附录 B、仓库验证环境 `ve_AES_Core.sv` 的 `MixColumn` 一致。
- 仓库 `x_times.v` 对 `0xdb` 给出 `0xb6`（应为 `0xad`），证明约简缺失。
- 结论：仓库当前提交的 AES 核心 MixColumns 通路**不完整**，需补全 `x_times.v` 的约简后才能用于真实加解密；这正是后续 u3-l5（仿真验证）要解决的事。

> 若本机无 Python，可改用 C 实现（`xtime` 用 `(b<<1) ^ ((b>>7)*0x1B) & 0xFF`），逻辑完全相同。无法运行时，以上数值结果标注为「待本地验证」。

## 6. 本讲小结

- **MixColumns 是 AES 的扩散层**：把每一列当成 GF(2⁸) 上的多项式，乘以固定多项式 \( a(x)=03x^3+01x^2+01x+02 \)，等价于一个 4×4 循环矩阵乘法，常数只有 1、2、3。
- **GF(2⁸) 运算**：加法 = XOR；乘法 = 多项式乘法后对不可约多项式 \( m(x)=x^8+x^4+x^3+x+1 \)（低 8 位 `0x1B`，即 `POLYNOMIAL_IRR`）取模。
- **xtime（×2）是核心积木**：左移一位，仅当最高位为 1 时再异或 `0x1B`；MixColumns 所需的 2、3 乘法都由它派生。
- **生效实现 `aes_mix_columns.v`** 用「相邻差 + 共享 xtime」节省面积，单模块同时输出加密 `a0..a3` 与解密 `c0..c3`；被 `aes_top` 例化 4 次。
- **草稿警示（贯穿全讲）**：`x_times.v` 把模约简注释掉了（对 ≥ 0x80 字节结果错误）；`aes_mix_columns_mul.v` 把 GF 乘法误写成 XOR 且从未被例化。两者都印证本仓库是供阅读的草稿，修复与验证留给后续讲义。
- **读源码的方法论**：先查「哪个模块真被例化」（`MixColumns` 而非 `MixColumnMul`），再精读生效路径；遇到可疑实现，用仓库自带的验证环境 `ve_AES_Core.sv` 作「标准答案」对照。

## 7. 下一步学习建议

- **向密钥侧延伸**：MixColumns 用到的 GF(2⁸) 算术同样贯穿密钥扩展（Key Schedule 里的 RotWord/SubWord/Rcon）。建议进入 **u2-l4 密钥扩展 Key Schedule**，看轮密钥如何递推生成。
- **向 S-Box 深处延伸**：xtime 只是 GF(2⁸) 乘 2。若想看 GF(2⁸) 上的**求逆**如何在硬件上高效实现（用复合域 GF((2⁴)²)），请进入 **u2-l5 S-Box 的复合域 GF(2⁴) 高效实现**——那里会用到本讲的域运算直觉。
- **验证闭环**：本讲反复指出 MixColumns 通路不完整。想看如何用仿真把核心跑通、用黄金参考模型比对，请进入 **u3-l5 仿真验证与 SystemVerilog 验证环境**，那里的 `ve_AES_class_MixColumn` 正是用来检查 `MixColumns` DUT 的。
