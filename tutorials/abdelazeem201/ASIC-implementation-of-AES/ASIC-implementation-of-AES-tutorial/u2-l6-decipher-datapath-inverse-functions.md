# 解密数据通路逆变换函数

> 本讲对应大纲 `u2-l6`，依赖 [u2-l4 加密数据通路四个变换函数](u2-l4-encipher-datapath-functions.md)。
> 我们只看 `aes_decipher_block.v` 中的**纯组合逆变换函数**，状态机细节留给 [u2-l7 解密轮控制状态机](u2-l7-decipher-round-fsm.md)。

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 AES 解密为什么是加密的「逆过程」，以及四个逆变换 `InvSubBytes / InvShiftRows / InvMixColumns / AddRoundKey` 各自做什么。
- 看懂 `inv_shiftrows` 的字节重排规则，并验证它确实是 `shiftrows` 的逆。
- 看懂 `inv_mixcolumns` 固定矩阵 `[0e, 0b, 0d, 09]`，以及 `inv_mixw` 如何用 `gm09 / gm11 / gm13 / gm14` 四个乘法器实现它。
- 理解 `gm09 / gm11 / gm13 / gm14` 不是凭空写的常量乘法，而是由基础乘法 `gm2 / gm4 / gm8` 通过异或**复合**得到的，并能推导 `gm14(op) = op × 0x0e`。
- 理解 `addroundkey` 在加解密中完全相同（一次异或）。

## 2. 前置知识

### 2.1 回顾：AES 加密一轮做了什么

在 u2-l4 中我们讲过，AES 加密的每一轮（除初始轮和最终轮外）由四个变换串联：

\[
\text{SubBytes} \rightarrow \text{ShiftRows} \rightarrow \text{MixColumns} \rightarrow \text{AddRoundKey}
\]

其中：

- `SubBytes`：逐字节查 S-box（非线性替换）。
- `ShiftRows`：对状态矩阵的每一行做循环**左移**（第 r 行左移 r 字节）。
- `MixColumns`：在有限域 GF(2⁸) 上对每一列做矩阵乘法，矩阵是 `[2,3,1,1]`。
- `AddRoundKey`：与轮密钥异或。

### 2.2 GF(2⁸) 上的乘法与 xtime

AES 的所有字节运算都在有限域 GF(2⁸) 上，使用不可约多项式：

\[
m(x) = x^8 + x^4 + x^3 + x + 1 \quad (\text{即 } \texttt{0x11b})
\]

「乘以 2」（也叫 `xtime`）是最基础的操作：把字节左移 1 位，若原最高位为 1，则溢出，需要再异或 `0x1b`（即 `0x11b` 的低 8 位）来取模。u2-l4 已经讲过 `gm2`、`gm3`：

\[
\text{gm2}(op) = (op \ll 1) \oplus (\texttt{0x1b} \cdot op[7])
\]
\[
\text{gm3}(op) = \text{gm2}(op) \oplus op \quad (\text{即} \times 2 + \times 1 = \times 3)
\]

本讲要把这套乘法**扩展到高次**（×4、×8、×9、×0xb、×0xd、×0xe），用来构造逆列混淆。

### 2.3 解密 = 把每一步倒过来

直觉上，解密就是把加密的每一步求逆，并**反向执行**：

| 加密一轮 | 解密对应一轮 |
|---|---|
| SubBytes | InvSubBytes |
| ShiftRows（左移） | InvShiftRows（右移） |
| MixColumns（矩阵 `[2,3,1,1]`） | InvMixColumns（矩阵 `[0e,0b,0d,09]`） |
| AddRoundKey | AddRoundKey（异或的逆还是异或） |

注意两点：

1. `AddRoundKey` 的逆运算就是它自己（`a ⊕ k ⊕ k = a`），所以加解密共用同一个函数。
2. `InvMixColumns` 的矩阵不是 `[2,3,1,1]` 的简单取反，而是另一组固定系数 `[0e, 0b, 0d, 09]`，这正是本讲的重头戏。

> 小提示：本工程实际采用的是「等价逆密码（equivalent inverse cipher）」的执行顺序——`InvShiftRows → InvSubBytes → AddRoundKey → InvMixColumns`，把 `InvShiftRows` 提到上一拍的尾部。这会在第 4.4 节的 `round_logic` 里体现，**时钟级**的细节留给 u2-l7。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它同时定义了「乘法器」和「三个逆变换」两层抽象：

| 源码片段 | 行号 | 作用 |
|---|---|---|
| `gm2 / gm3 / gm4 / gm8` | [L51-L73](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L51-L73) | 基础乘法器（×2/×3/×4/×8），是高次乘法的积木 |
| `gm09 / gm11 / gm13 / gm14` | [L75-L97](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L75-L97) | 逆列混淆专用乘法器（×9/×0xb/×0xd/×0xe） |
| `inv_mixw` | [L99-L115](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L99-L115) | 对一个 32 位字（一列）做逆列混淆 |
| `inv_mixcolumns` | [L117-L133](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L117-L133) | 对 128 位（4 列）逐字调用 `inv_mixw` |
| `inv_shiftrows` | [L135-L151](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L135-L151) | 逆行移位（行右移） |
| `addroundkey` | [L153-L157](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L153-L157) | 轮密钥加（异或） |
| `inv_sbox_inst` 例化 | [L205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205) | 解密私挂的逆 S-box（实现 InvSubBytes） |
| `round_logic` 组合块 | [L270-L358](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L270-L358) | 把上述函数按 `update_type` 串成各轮运算 |

## 4. 核心概念与源码讲解

### 4.1 InvShiftRows：逆行移位

#### 4.1.1 概念说明

`ShiftRows` 把状态矩阵的第 r 行**循环左移** r 字节；`InvShiftRows` 自然就是**循环右移** r 字节，把字节「移回原位」。它和 `SubBytes / MixColumns` 不同，是**纯字节置换**，不改变任何字节的值，只改变它们的位置，因此完全不需要任何算术，只要重新连线。

#### 4.1.2 核心流程

先把 128 位状态切成 4 个 32 位「字」`w0..w3`，每个字代表状态矩阵的**一列**，字内字节序为：`[31:24]=row0, [23:16]=row1, [15:08]=row2, [07:00]=row3`。

`InvShiftRows` 对第 r 行做右移 r，等价于：输出第 c 列第 r 行的字节 ← 输入第 `(c - r) mod 4` 列第 r 行的字节。

以 row1（每个字的 `[23:16]`）为例（右移 1）：

| 输出列 c | 取自输入列 (c−1) mod 4 |
|---|---|
| 0 (w0) | 3 (w3) |
| 1 (w1) | 0 (w0) |
| 2 (w2) | 1 (w1) |
| 3 (w3) | 2 (w2) |

其余行同理（row0 不动，row2 右移 2，row3 右移 3）。

#### 4.1.3 源码精读

[rtl/aes_decipher_block.v:L135-L151](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L135-L151) 实现了 `inv_shiftrows`：

```verilog
ws0 = {w0[31 : 24], w3[23 : 16], w2[15 : 08], w1[07 : 00]};
ws1 = {w1[31 : 24], w0[23 : 16], w3[15 : 08], w2[07 : 00]};
ws2 = {w2[31 : 24], w1[23 : 16], w0[15 : 08], w3[07 : 00]};
ws3 = {w3[31 : 24], w2[23 : 16], w1[15 : 08], w0[07 : 00]};
```

读法：看 `ws0`（第 0 列输出）——`[31:24]` 取 `w0`（row0 不动），`[23:16]` 取 `w3`（row1 来自第 3 列，即右移 1），`[15:08]` 取 `w2`（row2 来自第 2 列，右移 2），`[07:00]` 取 `w1`（row3 来自第 1 列，右移 3）。完全对应上表。

对比加密的 `shiftrows`（[rtl/aes_encipher_block.v:L112-L115](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L112-L115)），`ws0` 的 row1 取的是 `w1`（左移 1）。两者方向相反，正好互逆。

#### 4.1.4 代码实践

**实践目标**：验证 `inv_shiftrows` 与 `shiftrows` 互为逆。

**操作步骤**（源码阅读型）：

1. 打开 [rtl/aes_encipher_block.v:L112-L115](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L112-L115) 的 `shiftrows`。
2. 构造一个只有 row1 第 0 列为非零的字节，例如令 `w0[23:16] = 0xAA`，其余字节为 0。
3. 先做 `shiftrows`：row1 左移 1，`0xAA` 从 col0 移到 col3 → 出现在 `w3[23:16]`。
4. 再对结果做 `inv_shiftrows`：row1 右移 1，`0xAA` 从 col3 移回 col0 → 回到 `w0[23:16]`。

**预期结果**：`inv_shiftrows(shiftrows(x)) == x`，字节回到原位。

#### 4.1.5 小练习与答案

**练习 1**：`inv_shiftrows` 中 `ws0[07:00]`（row3, col0）取自哪个字？为什么？

> **答案**：取自 `w1[07:00]`（col1 的 row3）。因为 row3 要右移 3 位，等价于左移 1 位，col0 ← col1。

**练习 2**：`ShiftRows` 和 `InvShiftRows` 改变字节的值吗？

> **答案**：不改变，只改变字节在 128 位状态中的位置（纯置换），所以实现上只是重新连线，没有任何运算。

---

### 4.2 InvMixColumns 与 gm09/11/13/14：逆列混淆

这是本讲最核心、也最难的模块。我们分两层讲：先看「逆列混淆矩阵 + `inv_mixw`」，再看它依赖的「高次乘法器 `gm09/11/13/14`」。

#### 4.2.1 概念说明

`MixColumns` 把状态的每一列（4 字节）看作 GF(2⁸) 上的多项式，左乘一个固定矩阵：

\[
\begin{bmatrix} 2 & 3 & 1 & 1 \\ 1 & 2 & 3 & 1 \\ 1 & 1 & 2 & 3 \\ 3 & 1 & 1 & 2 \end{bmatrix}
\]

`InvMixColumns` 则左乘该矩阵的逆：

\[
\begin{bmatrix} \texttt{0e} & \texttt{0b} & \texttt{0d} & \texttt{0d?} \\ \end{bmatrix}
\quad\Longrightarrow\quad
\begin{bmatrix} \texttt{0e} & \texttt{0b} & \texttt{0d} & \texttt{09} \\ \texttt{09} & \texttt{0e} & \texttt{0b} & \texttt{0d} \\ \texttt{0d} & \texttt{09} & \texttt{0e} & \texttt{0b} \\ \texttt{0b} & \texttt{0d} & \texttt{09} & \texttt{0e} \end{bmatrix}
\]

也就是说，逆列混淆的每一行是 `[0e, 0b, 0d, 09]` 的循环移位。这就要求我们能在硬件上算「乘以 0x0e / 0x0b / 0x0d / 0x09」。注意 `0e, 0b, 0d, 09` 都比加密用的 `2, 3` 大得多，直接建大乘法表会浪费面积，所以工程用**复合乘法器**来实现它们。

#### 4.2.2 核心流程：乘法器如何复合

关键思想：GF(2⁸) 乘法对加法（异或 ⊕）满足**分配律**：

\[
a \cdot op \oplus b \cdot op = (a \oplus b) \cdot op
\]

所以任何「乘以一个常量」都可以拆成若干次「乘以 2」的复合再异或。`gm2` 是原子操作，反复套用就得到 ×4、×8：

\[
\text{gm4}(op) = \text{gm2}(\text{gm2}(op)) \;\Rightarrow\; \times 4
\]
\[
\text{gm8}(op) = \text{gm2}(\text{gm4}(op)) \;\Rightarrow\; \times 8
\]

再把 ×8/×4/×2/×1 异或组合，就得到逆矩阵需要的全部系数：

\[
\text{gm09}(op) = \text{gm8}(op) \oplus op \;\Rightarrow\; \times (8+1) = \times \texttt{0x09}
\]
\[
\text{gm11}(op) = \text{gm8}(op) \oplus \text{gm2}(op) \oplus op \;\Rightarrow\; \times (8+2+1) = \times \texttt{0x0b}
\]
\[
\text{gm13}(op) = \text{gm8}(op) \oplus \text{gm4}(op) \oplus op \;\Rightarrow\; \times (8+4+1) = \times \texttt{0x0d}
\]
\[
\text{gm14}(op) = \text{gm8}(op) \oplus \text{gm4}(op) \oplus \text{gm2}(op) \;\Rightarrow\; \times (8+4+2) = \times \texttt{0x0e}
\]

每个系数的二进制位直接告诉你它由哪些 2 的幂相加：`0x09=8+1`、`0x0b=8+2+1`、`0x0d=8+4+1`、`0x0e=8+4+2`。这就是 `gm09/11/13/14` 函数体里异或项的来历。

#### 4.2.3 源码精读

**(a) 基础乘法器 `gm2/gm3/gm4/gm8`** —— [rtl/aes_decipher_block.v:L51-L73](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L51-L73)：

```verilog
function [7:0] gm2(input [7:0] op);
  gm2 = {op[6:0], 1'b0} ^ (8'h1b & {8{op[7]}});
endfunction

function [7:0] gm4(input [7:0] op);
  gm4 = gm2(gm2(op));   // ×4
endfunction

function [7:0] gm8(input [7:0] op);
  gm8 = gm2(gm4(op));   // ×8
endfunction
```

`gm2` 与加密侧 [rtl/aes_encipher_block.v:L55-L59](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L55-L59) 完全一致——乘以 2 的定义只有一个。`gm4`、`gm8` 则是解密侧新增的「更高阶的 2 的幂」。

**(b) 逆矩阵专用乘法器 `gm09/11/13/14`** —— [rtl/aes_decipher_block.v:L75-L97](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L75-L97)：

```verilog
function [7:0] gm09(input [7:0] op); gm09 = gm8(op) ^ op;            endfunction
function [7:0] gm11(input [7:0] op); gm11 = gm8(op) ^ gm2(op) ^ op; endfunction
function [7:0] gm13(input [7:0] op); gm13 = gm8(op) ^ gm4(op) ^ op; endfunction
function [7:0] gm14(input [7:0] op); gm14 = gm8(op) ^ gm4(op) ^ gm2(op); endfunction
```

注意 `gm14` **不带 `^ op`**，因为 `0x0e = 8+4+2`，末位（×1）是 0。

**(c) `inv_mixw`：一列的逆列混淆** —— [rtl/aes_decipher_block.v:L99-L115](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L99-L115)：

```verilog
b0 = w[31:24]; b1 = w[23:16]; b2 = w[15:08]; b3 = w[07:00];

mb0 = gm14(b0) ^ gm11(b1) ^ gm13(b2) ^ gm09(b3);  // [0e, 0b, 0d, 09]
mb1 = gm09(b0) ^ gm14(b1) ^ gm11(b2) ^ gm13(b3);  // 循环右移
mb2 = gm13(b0) ^ gm09(b1) ^ gm14(b2) ^ gm11(b3);
mb3 = gm11(b0) ^ gm13(b1) ^ gm09(b2) ^ gm14(b3);
```

四个输出字节 `mb0..mb3` 的系数恰好是 `[0e, 0b, 0d, 09]` 的四次循环右移，正好对应逆矩阵的四行。对比加密侧 `mixw`（[rtl/aes_encipher_block.v:L76-L79](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L76-L79)）用的 `[2,3,1,1]`，结构完全对称，只是系数更「重」。

**(d) `inv_mixcolumns`：4 列并联** —— [rtl/aes_decipher_block.v:L117-L133](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L117-L133) 把 128 位切成 4 个字，各调一次 `inv_mixw`，再拼回 128 位。和加密侧 `mixcolumns` 结构一致。

#### 4.2.4 代码实践

**实践目标**：用源码里的乘法定义，手算一个 `gm14` 验证它确实是 ×0x0e。

**操作步骤**：

1. 取 `op = 0x57`（一个常用测试字节）。
2. 逐步算：`gm2(0x57)`、`gm4(0x57)`、`gm8(0x57)`。
3. 再算 `gm14(0x57) = gm8 ⊕ gm4 ⊕ gm2`。
4. 与 `0x57 × 0x0e` 的「二进制展开法」结果对比。

**手算过程**：

- `gm2(0x57)`：`0x57 = 0101_0111`，最高位 0，直接左移 → `0xAE`。
- `gm4(0x57) = gm2(0xAE)`：`0xAE = 1010_1110`，最高位 1，左移得 `0x5C`，再 ⊕ `0x1b` → `0x47`。
- `gm8(0x57) = gm2(0x47)`：`0x47 = 0100_0111`，最高位 0，左移 → `0x8E`。
- `gm14(0x57) = 0x8E ⊕ 0x47 ⊕ 0xAE`：先 `0x8E ⊕ 0x47 = 0xC9`，再 `0xC9 ⊕ 0xAE = 0x67`。

**预期结果**：`gm14(0x57) = 0x67`，且 `0x57 × 0x0e`（= ×8 ⊕ ×4 ⊕ ×2 = `0x8E ⊕ 0x47 ⊕ 0xAE`）同样等于 `0x67`。两者一致，说明 `gm14` 就是 ×0x0e 的正确实现。（若你手算结果不同，多半是某一步 `gm2` 忘了在最高位为 1 时 ⊕ `0x1b`。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gm14` 的函数体里**没有** `^ op`，而 `gm09/11/13` 都有？

> **答案**：`0x0e = 0b1110`，最低位（×1）是 0，不需要 `op` 项；而 `0x09=0b1001`、`0x0b=0b1011`、`0x0d=0b1101` 最低位都是 1，需要 `^ op`。

**练习 2**：能否把 `gm13(op)` 改写成 `gm8(op) ^ gm4(op) ^ op` 以外的形式？

> **答案**：可以，例如 `gm13(op) = gm8(op) ^ gm5(op)`，其中 `gm5(op) = gm4(op) ^ op`（×5）。只要展开后的 2 的幂异或组合等于 `0x0d = 8+4+1` 即可；源码选择的是最直观的 `8+4+1` 形式。

**练习 3**：`gm2(gm2(op))` 在数学上为什么等于 ×4，而不是「先把 op 左移两位再说」？

> **答案**：因为每调一次 `gm2` 都做了一次「左移 + 必要时 ⊕ 0x1b 取模」。GF(2⁸) 中 `(a \bmod m)\cdot b \bmod m = a\cdot b \bmod m`，所以分两次取模与一次取模结果相同，`gm2(gm2(op)) = op \times x^2 = op \times 4`。

---

### 4.3 AddRoundKey：轮密钥加

#### 4.3.1 概念说明

`AddRoundKey` 把 128 位状态与 128 位轮密钥逐位异或。异或是自逆运算（`a ⊕ k ⊕ k = a`），所以**加密和解密共用同一个函数**，没有任何差别。它依赖 u2-l3 讲过的「`round_key` 由 `key_mem` 按轮号异步给出」。

#### 4.3.2 核心流程

\[
\text{addroundkey}(data, rkey) = data \oplus rkey
\]

就这一行。在解密中它出现在三个地方：初始轮（与最后一把轮密钥异或）、每个主轮（与对应轮密钥异或）、最终轮（与第 0 把轮密钥异或）。

#### 4.3.3 源码精读

[rtl/aes_decipher_block.v:L153-L157](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L153-L157)：

```verilog
function [127:0] addroundkey(input [127:0] data, input [127:0] rkey);
  addroundkey = data ^ rkey;
endfunction
```

与加密侧 [rtl/aes_encipher_block.v:L121-L125](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L121-L125) 逐字符相同。`round_key` 端口由 [rtl/aes_decipher_block.v:L18](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L18) 的输入提供，源头是 `aes_core` 里 `key_mem` 按本模块输出的 `round` 号组合选出的那把轮密钥（见 u2-l1 / u2-l3）。

#### 4.3.4 代码实践

**实践目标**：确认 `addroundkey` 的自逆性。

**操作步骤**：在 [rtl/aes_decipher_block.v:L288-L357](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L288-L357) 的 `round_logic` 里数一下 `addroundkey(...)` 出现在哪几个 `update_type` 分支。

**预期结果**：它在 `INIT_UPDATE`、`MAIN_UPDATE`、`FINAL_UPDATE` 三个分支都出现，只有 `SBOX_UPDATE`（做 InvSubBytes）那一拍没有它。说明除 InvSubBytes 阶段外，每一轮都会与轮密钥异或一次。

#### 4.3.5 小练习与答案

**练习 1**：为什么解密能直接复用加密的 `addroundkey`，而不需要写一个「逆 addroundkey」？

> **答案**：因为异或是自逆的，`a ⊕ k ⊕ k = a`。加密时 `state ⊕ key`，解密时对密文再做 `⊕ key` 即可还原，函数完全相同。

**练习 2**：`addroundkey` 是组合逻辑还是时序逻辑？

> **答案**：组合逻辑（`function`，纯 `^` 运算）。真正把结果存下来的是外层 `reg_update` 时序块里的 `block_w*_reg` 寄存器。

---

### 4.4 逆变换如何在 round_logic 中串联（含 InvSubBytes）

本节不是新的「函数」，而是把 4.1–4.3 的三个函数 + InvSubBytes 放回它们的真实使用场景，看 `round_logic` 组合块如何调用它们。这能帮你理解「为什么需要逆变换」以及「InvSubBytes 为什么没有写成 function」。

#### 4.4.1 概念说明

`round_logic`（[rtl/aes_decipher_block.v:L270-L358](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L270-L358)）是一个 `always @*` 组合块，按 `update_type` 选择本轮要把哪种运算结果写回 4 个字寄存器。它对应加密侧的 `round_logic`（u2-l4），但调用的是逆函数。

`InvSubBytes` 没有写成 function，而是通过一个**私挂的逆 S-box 实例**实现：[rtl/aes_decipher_block.v:L205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205)

```verilog
aes_inv_sbox inv_sbox_inst(.sword(tmp_sboxw), .new_sword(new_sboxw));
```

这个 `inv_sbox_inst` 是解密专用、不与加密共享的（加密走 `aes_core` 里唯一的正向 `sbox_inst`，见 u2-l2）。`aes_inv_sbox` 内部是 4 路并行查表（[rtl/aes_inv_sbox.v:L24-L27](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v#L24-L27)），但它只处理**一个 32 位字**，所以 128 位的 InvSubBytes 要分 4 拍逐字完成（由 `sword_ctr` 控制，细节在 u2-l7）。

#### 4.4.2 核心流程

四个 `update_type` 分支各自的运算链条：

| `update_type` | 运算链条 | 对应轮 |
|---|---|---|
| `INIT_UPDATE` | `inv_shiftrows(addroundkey(block, rkey))` | 初始轮（先异或最后一把轮密钥，再逆行移位） |
| `SBOX_UPDATE` | `inv_sbox_inst` 逐字替换（4 拍） | InvSubBytes |
| `MAIN_UPDATE` | `inv_shiftrows(inv_mixcolumns(addroundkey(old, rkey)))` | 主轮（异或 → 逆列混淆 → 逆行移位） |
| `FINAL_UPDATE` | `addroundkey(old, rkey)` | 最终轮（只异或第 0 把轮密钥） |

注意 `MAIN_UPDATE` 的顺序是 **AddRoundKey → InvMixColumns → InvShiftRows**，而 `InvSubBytes` 在它前一拍（`SBOX_UPDATE`）已经做完。把 `InvShiftRows` 放在一轮的尾部、`InvSubBytes` 放在下一轮的头部，正是「等价逆密码」的写法——它与 FIPS 197 的标准逆密码数学等价，只是把行移位挪了位置，方便和加密模块用同一种 `round_logic` 框架实现。

#### 4.4.3 源码精读

`INIT_UPDATE` —— [rtl/aes_decipher_block.v:L290-L300](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L290-L300)：

```verilog
old_block    = block;                         // 初始轮读输入端口，不是寄存器
addkey_block = addroundkey(old_block, round_key);
inv_shiftrows_block = inv_shiftrows(addkey_block);
block_new    = inv_shiftrows_block;
```

`MAIN_UPDATE` —— [rtl/aes_decipher_block.v:L333-L343](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L333-L343)：

```verilog
addkey_block         = addroundkey(old_block, round_key);
inv_mixcolumns_block = inv_mixcolumns(addkey_block);
inv_shiftrows_block  = inv_shiftrows(inv_mixcolumns_block);
block_new            = inv_shiftrows_block;
```

`FINAL_UPDATE` —— [rtl/aes_decipher_block.v:L345-L352](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L345-L352) 只有一句 `block_new = addroundkey(old_block, round_key)`。

`SBOX_UPDATE` —— [rtl/aes_decipher_block.v:L302-L331](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L302-L331)：用 `sword_ctr_reg` 选出当前要替换的字 `tmp_sboxw`，送进 `inv_sbox_inst`，把返回的 `new_sboxw` 写回**仅那一个**字（其余三个字的写使能保持 0）。

#### 4.4.4 代码实践

**实践目标**：在源码里确认「InvSubBytes 不是 function，而是模块例化」。

**操作步骤**：

1. 在 `aes_decipher_block.v` 中搜索 `function`，确认只有 `gm2..gm14 / inv_mixw / inv_mixcolumns / inv_shiftrows / addroundkey`，没有 `inv_subbytes`。
2. 看 [L205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205) 的 `inv_sbox_inst` 例化，以及 [L302-L331](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L302-L331) 里 `tmp_sboxw → new_sboxw` 的连线。

**预期结果**：`round_logic` 把 4 个逆变换函数 + 1 个逆 S-box 实例组合起来，分别落在 `INIT/SBOX/MAIN/FINAL` 四个分支里。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `InvSubBytes` 用模块例化，而 `InvShiftRows/InvMixColumns/AddRoundKey` 用 `function`？

> **答案**：因为 `InvSubBytes` 要查一张 256 字节的常量表（逆 S-box），工程选择把它做成独立的 ROM 模块 `aes_inv_sbox` 复用；而另外三个变换是纯算术/连线，没有大表，用 `function` 描述最简洁。

**练习 2**：`MAIN_UPDATE` 里三个逆函数的顺序能随便换吗？

> **答案**：不能。必须严格按 `AddRoundKey → InvMixColumns → InvShiftRows` 的顺序，因为这是「等价逆密码」对运算顺序的要求；打乱顺序会导致结果错误（这些变换之间不可交换）。

---

## 5. 综合实践：推导 `gm14(op) = op × 0x0e`

本讲的核心练习是把第 4.2 节的乘法器复合关系完整推导一遍，确认 `gm14` 确实实现了 GF(2⁸) 上的「乘以 0x0e」。

### 实践目标

证明 [rtl/aes_decipher_block.v:L93-L97](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L93-L97) 的

```verilog
gm14(op) = gm8(op) ^ gm4(op) ^ gm2(op)
```

等价于 `op × 0x0e`。

### 操作步骤（推导）

1. **基础原子**：`gm2(op) = op × 2`（xtime，见 u2-l4 / [L51-L55](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L51-L55)）。
2. **复合到 ×4**：`gm4(op) = gm2(gm2(op)) = op × 4`（[L63-L67](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L63-L67)）。
3. **复合到 ×8**：`gm8(op) = gm2(gm4(op)) = op × 8`（[L69-L73](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L69-L73)）。
4. **代入 gm14**：

\[
\text{gm14}(op) = \text{gm8}(op) \oplus \text{gm4}(op) \oplus \text{gm2}(op) = op \times 8 \oplus op \times 4 \oplus op \times 2
\]

5. **用分配律合并**（GF(2⁸) 中乘法对加法 ⊕ 分配）：

\[
op \times 8 \oplus op \times 4 \oplus op \times 2 = op \times (8 \oplus 4 \oplus 2)
\]

6. **算括号里的常数**：

\[
8 \oplus 4 \oplus 2 = 1000_2 \oplus 0100_2 \oplus 0010_2 = 1110_2 = \texttt{0x0e} = 14
\]

7. **结论**：

\[
\text{gm14}(op) = op \times \texttt{0x0e} \quad \checkmark
\]

这与 `inv_mixw` 中 `mb0` 对 `b0` 的系数 `gm14` = `0e`（[L108](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L108)）完全吻合。

### 数值验证（可选，待本地验证）

取 `op = 0x57`：按 4.2.4 节手算得 `gm14(0x57) = 0x67`；用「`0x57 × 0x0e = 0x57×8 ⊕ 0x57×4 ⊕ 0x57×2 = 0x8E ⊕ 0x47 ⊕ 0xAE = 0x67`」结果一致。你可以在仿真里给 `aes_decipher_block` 喂一组 NIST 解密向量（见 u3-l2）做端到端验证。

### 需要观察的现象

- 推导的关键是第 5 步的**分配律**：正是因为 GF(2⁸) 乘法对异或分配，才能把 `op×8 ⊕ op×4 ⊕ op×2` 合并成 `op×(8⊕4⊕2)`。
- 复合乘法器的面积代价：实现 `gm14` 只需要复用 `gm2`（一份 ×2 逻辑），通过函数嵌套调用，不必为每个系数单独建乘法器——这正是 ASIC 设计中「用时间/复用换面积」的典型手法（详见 u3-l4）。

## 6. 本讲小结

- 解密是加密的逆过程：`InvSubBytes / InvShiftRows / InvMixColumns / AddRoundKey` 四个逆变换，其中三个是 `function`，`InvSubBytes` 通过私挂的 `inv_sbox_inst` 实现。
- `inv_shiftrows`（[L135-L151](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L135-L151)）是纯字节置换，把每行**右移** r 字节，方向与加密的左移相反。
- `inv_mixcolumns` 用固定矩阵 `[0e, 0b, 0d, 09]`（[L99-L115](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L99-L115)），系数比加密的 `[2,3,1,1]` 重得多。
- `gm09/11/13/14` 不是新写的乘法器，而是由 `gm2 → gm4 → gm8` 复合再异或得到，依据是 GF(2⁸) 乘法对加法的分配律。
- `addroundkey`（[L153-L157](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L153-L157)）加解密完全相同，就是一次 128 位异或。
- `round_logic`（[L270-L358](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L270-L358)）把这些函数按 `INIT/SBOX/MAIN/FINAL` 四种 `update_type` 串起来，采用「等价逆密码」的运算顺序。

## 7. 下一步学习建议

- 本讲只讲了**组合函数层**，还没讲这些函数如何按时钟节拍流动。下一讲 [u2-l7 解密轮控制状态机](u2-l7-decipher-round-fsm.md) 会打开 `decipher_ctrl` FSM，讲 `round_ctr` 为什么是**递减**计数（与加密的递增相反），以及一次完整解密到底花多少拍。
- 想看这些逆变换的端到端效果，可跳到 [u3-l2 仿真验证与 NIST 测试向量](u3-l2-verification-and-nist-vectors.md)，用 NIST 的解密已知应答验证 `inv_mixcolumns` 是否正确。
- 想理解「为什么用复合乘法器而不是大乘法表」，可预习 [u3-l4 面向 ASIC 的设计取舍](u3-l4-asic-design-tradeoffs.md)。
