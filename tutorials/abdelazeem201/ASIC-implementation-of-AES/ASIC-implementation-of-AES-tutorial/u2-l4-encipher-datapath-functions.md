# 加密数据通路四个变换函数

## 1. 本讲目标

本讲聚焦 AES 加密一轮里的四个核心变换函数：**SubBytes、ShiftRows、MixColumns、AddRoundKey**，全部在 `rtl/aes_encipher_block.v` 中实现。

学完后你应当能够：

- 说出 AES 一轮的四个变换各做了什么，以及初始轮 / 中间轮 / 最终轮在函数组合上的差别。
- 读懂 `shiftrows`、`mixcolumns`/`mixw`、`addroundkey` 三个纯组合 `function`，并理解本工程把 **SubBytes 外挂到共享 S-box** 的设计。
- 看懂 `gm2`/`gm3` 如何用「左移 + 条件异或」实现 GF(2⁸) 上的乘法，并能手算一个 32 位字的 `mixw` 结果。
- 理解 `round_logic` 这一个 `always @*` 块如何把四个变换拼装成「初始轮 / 中间轮 / 最终轮」三种数据通路。

> 前置提醒：本讲只讲「**一轮里的纯组合函数**」，不涉及轮计数、状态机时序（那是 u2-l5「加密轮控制状态机」的内容）。本讲依赖 u2-l2（S-box 查表与 GF(2⁸) 基本概念）。

## 2. 前置知识

### 2.1 AES 的一轮长什么样

AES 把 128 位明文看成一张 4×4 的字节矩阵（称为 **state**）。加密时对 state 反复做「轮变换」，AES-128 做 10 轮、AES-256 做 14 轮。每一轮（除首尾两轮有微调）依次执行：

| 步骤 | 作用（直觉） |
|------|----------|
| **SubBytes** | 每个字节查 S-box 表做非线性替换，是 AES 唯一的非线性来源（混淆）。 |
| **ShiftRows** | 把每一行循环左移不同位数，打散列内关系（扩散）。 |
| **MixColumns** | 在每一列内部做 GF(2⁸) 矩阵乘法，把字节进一步混合（扩散）。 |
| **AddRoundKey** | 把当前 state 与本轮的「轮密钥」逐位异或，引入密钥。 |

标准 AES 的轮结构是：

- 第 0 轮（初始轮）：只做 **AddRoundKey**。
- 第 1 ~ N−1 轮（中间轮）：**SubBytes → ShiftRows → MixColumns → AddRoundKey**。
- 第 N 轮（最终轮）：**SubBytes → ShiftRows → AddRoundKey**（**省去 MixColumns**）。

本工程的 `aes_encipher_block` 正是按这个结构来的，只是把 SubBytes 单独拆出来用共享 S-box 处理（见 4.1）。

### 2.2 GF(2⁸) 与「乘 2 = 左移 + 可能异或 0x1b」

MixColumns 里的乘法不是普通乘法，而是在有限域 **GF(2⁸)**（也称伽罗瓦域）上的乘法。理解下面这一点就够用了：

- 字节就是 GF(2⁸) 里的一个元素，看作一个最高次为 7 的多项式，每一位是系数。例如 `0x57 = 0101 0111` 代表 \(x^6 + x^4 + x^2 + x + 1\)。
- 域里的加法就是**按位异或（XOR）**。
- 乘法要用 AES 规定的**不可约多项式** \(x^8 + x^4 + x^3 + x + 1\)（写成十六进制是 `0x11b`）做取模归约。

最关键的运算是 **×2**（也叫 `xtime`）：

- 把字节左移一位（等价于多项式升一次幂）。
- 如果原字节最高位（bit 7）是 1，左移后会「溢出」到 bit 8，这时就要异或 `0x1b` 把它折回来。`0x1b` 就是 `0x11b` 去掉最高位后的低 8 位。
- 用一句话记：**「左移一位；若最高位是 1，再异或 0x1b」**。

有了 ×2，就能组合出其他乘法：**×3 = ×2 + ×1**（即 `gm3 = gm2 ^ op`）。MixColumns 只用到 ×1、×2、×3 三种乘法，所以只要会 ×2 就够了。

## 3. 本讲源码地图

本讲全部围绕一个文件：

| 文件 | 本讲关注的内容 |
|------|--------------|
| [`rtl/aes_encipher_block.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v) | AES 加密轮的纯组合变换函数 `gm2`/`gm3`/`mixw`/`mixcolumns`/`shiftrows`/`addroundkey`，以及把它们拼装成三种轮通路的 `round_logic` 块。SubBytes 经由端口外接到共享 S-box。 |

模块端口里与本讲直接相关的两组信号：

- `sboxw`（输出，32 位）/ `new_sboxw`（输入，32 位）：SubBytes 的「请求字 / 应答字」接口，详见 4.1。
- `block`（输入，128 位明文）/ `new_block`（输出，128 位密文）/ `round_key`（输入，128 位轮密钥）：加密数据的主通路。

> 说明：解密（`aes_decipher_block.v`）用的是「逆」版本的同一批函数，原理对称，本讲不展开，留给 u2-l6。

## 4. 核心概念与源码讲解

本讲按「四个变换」拆成四个最小模块。其中 **SubBytes**（4.1）是外接到共享 S-box 的，**ShiftRows / MixColumns / AddRoundKey**（4.2~4.4）是本文件内的纯组合 `function`。最后在 4.5 看 `round_logic` 如何把它们拼起来。

### 4.1 SubBytes（经共享 S-box）

#### 4.1.1 概念说明

SubBytes 把 state 里的每个字节 \(b\) 替换成 S-box 表里的第 \(b\) 项，即 \(b \mapsto \text{S}[b]\)。这是 AES 唯一的非线性步骤，负责「混淆」。

本工程没有在 `aes_encipher_block` 内部例化 S-box，而是**把唯一的正向 S-box 硬件放到 `aes_core` 里共享**（见 u2-l1、u2-l2）：加密通路和密钥扩展分时复用同一个 `aes_sbox` 实例。因此本模块只通过两个端口「借用」外面的 S-box：

- `sboxw`（输出）：告诉外面的 S-box「我现在要替换这个 32 位字」。
- `new_sboxw`（输入）：外面 S-box 把替换好的字送回来。

128 位 state 被拆成 4 个 32 位字（`block_w0_reg` ~ `block_w3_reg`），**一次只替换一个字，4 拍完成 SubBytes**（这也是 u2-l3 提到的「逐字 4 拍」、用时间换面积的取舍）。

#### 4.1.2 核心流程

1. 进入 `SBOX_UPDATE` 阶段后，本模块根据 `sword_ctr_reg`（0~3）选中一个字，把它放到 `sboxw` 上。
2. 外部共享 S-box 在同一周期内算出 `new_sboxw`（查表是组合逻辑，零延迟返回）。
3. 本模块把 `new_sboxw` 写回对应那个字寄存器（`block_wN_we` 置 1）。
4. `sword_ctr_reg` 自增，下一拍换下一个字，直到 4 个字都替换完。

注意第 3 步的小细节：写回时 4 个字位置都填同一个 `new_sboxw`，但**只有被选中那一个的写使能 `_we` 拉高**，所以只有目标字被更新，其余三个保持不变。

#### 4.1.3 源码精读

SubBytes 的「请求 / 应答」端口定义：

[aes_encipher_block.v:21-22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L21-L22) — 定义 `sboxw`（要替换的字，输出给外部 S-box）和 `new_sboxw`（替换后的字，由外部 S-box 送回）。

`round_logic` 块里 `SBOX_UPDATE` 分支负责逐字驱动 SubBytes：

[aes_encipher_block.v:261-290](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L261-L290) — `block_new` 四个字位都填 `new_sboxw`，再用 `case (sword_ctr_reg)` 只把当前选中字送上 `muxed_sboxw`（再 `assign` 到 `sboxw`），并只对那一个字置写使能。

#### 4.1.4 代码实践

**实践目标**：确认 SubBytes 在本模块里是「外接」的，并理解逐字 4 拍的写回机制。

**操作步骤**：

1. 打开 `rtl/aes_encipher_block.v`，搜索 `sboxw`，确认它只出现在端口声明和 `assign sboxw = muxed_sboxw;`（第 173 行）以及 `SBOX_UPDATE` 分支里，**本文件内没有任何 S-box 查表常量**——查表确实在外部。
2. 阅读 261~290 行：注意 `block_new = {new_sboxw, new_sboxw, new_sboxw, new_sboxw};` 这一行，结合下面 `case (sword_ctr_reg)` 只置一个 `block_wN_we`，验证「4 个位置同值、只写一个」的设计。
3. 想象 `sword_ctr_reg` 从 0 走到 3：4 拍内 `block_w0_reg → block_w3_reg` 依次被替换。

**预期结果**：你应当能解释「为什么 `block_new` 要把 `new_sboxw` 重复 4 次」——因为 `block_new` 是 128 位、按字切片写入，而任意一拍只有 1 个 32 位字在更新，其余 3 个位置因写使能为 0 而被忽略，所以重复填同一个值最省事。**待本地验证**：可结合 u2-l5 的仿真，在 `CTRL_SBOX` 状态用波形观察 `sword_ctr_reg` 与 4 个 `block_w*_reg` 的变化。

#### 4.1.5 小练习与答案

- **练习 1**：为什么不在本模块内例化自己的 `aes_sbox`，而要外接到 `aes_core` 共享？
  - **答案**：加密通路和密钥扩展都需要正向 S-box，但它们不会同时工作。共享一份硬件能省下 S-box 的面积 / 功耗，这正是本工程「用时间换面积」的核心取舍（见 u2-l2、u3-l4）。
- **练习 2**：`sword_ctr_reg` 是几位？为什么正好够用？
  - **答案**：2 位（`reg [1:0]`，见第 131 行）。要数 4 个字，2 位刚好表示 0~3。

---

### 4.2 ShiftRows（字节重排）

#### 4.2.1 概念说明

ShiftRows 对 state 的**每一行**做循环左移：第 0 行不移，第 1 行左移 1 字节，第 2 行左移 2，第 3 行左移 3。它纯粹是「换位置」，没有任何计算，目的是让一列里的字节来自不同的原始列，配合 MixColumns 实现**扩散**。

本工程把 128 位 state 看作 4 个 32 位字 \(w_0, w_1, w_2, w_3\)（\(w_0\) 是最高 32 位），每个字的 4 个字节正好是「一列」：字的最高字节（bit 31~24）是第 0 行，最低字节（bit 7~0）是第 3 行。所以「按列成字」的排列下，ShiftRows 是**跨字、按行重排**。

#### 4.2.2 核心流程

输出列 \(ws_c\) 的第 \(r\) 行 = 输入列 \((c+r) \bmod 4\) 的第 \(r\) 行。用公式写：

\[
\text{new}[r][c] = \text{old}[r][(c+r) \bmod 4]
\]

由此推出每个输出字的字节来源：

| 输出字 | byte0（行0） | byte1（行1） | byte2（行2） | byte3（行3） |
|--------|--------|--------|--------|--------|
| \(ws_0\) | \(w_0\) 行0 | \(w_1\) 行1 | \(w_2\) 行2 | \(w_3\) 行3 |
| \(ws_1\) | \(w_1\) 行0 | \(w_2\) 行1 | \(w_3\) 行2 | \(w_0\) 行3 |
| \(ws_2\) | \(w_2\) 行0 | \(w_3\) 行1 | \(w_0\) 行2 | \(w_1\) 行3 |
| \(ws_3\) | \(w_3\) 行0 | \(w_0\) 行1 | \(w_1\) 行2 | \(w_2\) 行3 |

#### 4.2.3 源码精读

[aes_encipher_block.v:103-119](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L103-L119) — `shiftrows` 函数。先把 128 位拆成 4 个字（行 107~110），再按上表把字节重新拼接成 4 个新字（行 112~115），最后拼回 128 位（行 117）。每一行的位选取恰好对应「行号 = 字节在字里的位置」「列号偏移 = 行号」。

> 对照行 112：`ws0 = {w0[31:24], w1[23:16], w2[15:08], w3[07:00]}`，正是「第 0 字来自 \(w_0\) 的行0、第 1 字来自 \(w_1\) 的行1……」，与公式完全一致。

#### 4.2.4 代码实践

**实践目标**：用一个具体输入手算 ShiftRows，确认字节来源表。

**操作步骤**：

1. 取一个 128 位 state，用十六进制写成 4×4 矩阵（列为字、字节高位在上）。例如令 4 个字为：
   - \(w_0 =\) `11 12 13 14`，\(w_1 =\) `21 22 23 24`，\(w_2 =\) `31 32 33 34`，\(w_3 =\) `41 42 43 44`（左边是行0/最高字节）。
2. 按字节来源表手算输出 4 个字。
3. 对照 `shiftrows` 函数第 112~115 行验证。

**预期结果**：输出应为
- \(ws_0 =\) `11 22 33 44`
- \(ws_1 =\) `21 32 43 14`
- \(ws_2 =\) `31 42 13 24`
- \(ws_3 =\) `41 12 23 34`

你能看到「行 r 整体左移 r 字节」的效果（例如行0 全是 `11 22 33 44` 不变；行1 的 `12 22 32 42` 左移 1 后变成 `22 32 42 12`，分布在 4 个输出字中）。**这是手算题，可直接验证。**

#### 4.2.5 小练习与答案

- **练习 1**：ShiftRows 改变了字节内容吗？
  - **答案**：没有，只改变位置。它是纯置换（permutation）。
- **练习 2**：为什么 ShiftRows 后，每个输出字里的 4 个字节都来自**不同**的输入字？
  - **答案**：因为每行左移的位数（0/1/2/3）各不相同，输出列的 4 行恰好取自 4 个不同的输入列，这正是扩散所需要的效果——下一步 MixColumns 才能把这些「跨列」字节混在一起。

---

### 4.3 MixColumns（gm2 / gm3 / mixw）

#### 4.3.1 概念说明

MixColumns 在**每一列内部**做一次 GF(2⁸) 矩阵乘法，把 4 个字节彻底混合，是 AES 最强的扩散步骤。每列视为一个 4 维列向量，左乘固定矩阵 \(M\)（系数只有 1、2、3）：

\[
\begin{bmatrix} d_0 \\ d_1 \\ d_2 \\ d_3 \end{bmatrix}
=
\begin{bmatrix}
2 & 3 & 1 & 1 \\
1 & 2 & 3 & 1 \\
1 & 1 & 2 & 3 \\
3 & 1 & 1 & 2
\end{bmatrix}
\begin{bmatrix} b_0 \\ b_1 \\ b_2 \\ b_3 \end{bmatrix}
\]

其中加法是 XOR，乘法是 GF(2⁸) 乘法。由于系数只有 2、3、1，只要实现「×2」「×3」即可，于是有了 `gm2`、`gm3` 两个基础函数；`mixw` 对一个 32 位字（即一列）算出 4 个输出字节；`mixcolumns` 对 4 个字各调一次 `mixw`。

#### 4.3.2 核心流程

- `gm2(op)`：×2，即 `xtime`。左移一位，若最高位为 1 再异或 `0x1b`。
- `gm3(op)`：×3，即 \(\text{gm2}(op) \oplus op\)。
- `mixw(w)`：把字 \(w\) 拆成 \(b_0\)（最高字节）~ \(b_3\)（最低字节），按矩阵 \(M\) 算出 4 个输出字节：

\[
\begin{aligned}
mb_0 &= 2 b_0 \oplus 3 b_1 \oplus b_2 \oplus b_3 \\
mb_1 &= b_0 \oplus 2 b_1 \oplus 3 b_2 \oplus b_3 \\
mb_2 &= b_0 \oplus b_1 \oplus 2 b_2 \oplus 3 b_3 \\
mb_3 &= 3 b_0 \oplus b_1 \oplus b_2 \oplus 2 b_3
\end{aligned}
\]

注意：本工程把 32 位字的**最高字节 \(b_0\) 当作「行0」**，与 `shiftrows` 的约定一致，所以矩阵系数按上式「从上到下循环右移」排列。

#### 4.3.3 源码精读

`gm2` 实现 GF(2⁸) 上的 ×2：

[aes_encipher_block.v:55-59](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L55-L59) — `{op[6:0], 1'b0}` 是左移一位；`8'h1b & {8{op[7]}}` 表示「仅当原最高位 op[7] 为 1 时，才异或 0x1b」（否则 `{8{1'b0}}` 为全 0，异或无效）。

`gm3` 实现 ×3：

[aes_encipher_block.v:61-65](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L61-L65) — `gm3 = gm2(op) ^ op`，即 \(2 \cdot op \oplus op = 3 \cdot op\)。

`mixw` 对一个字（一列）算矩阵乘法：

[aes_encipher_block.v:67-83](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L67-L83) — 行 71~74 拆字节；行 76~79 正是上面 4 个公式；行 81 拼回 32 位。逐行比对，系数矩阵与标准 AES 完全一致。

`mixcolumns` 对 4 个字各调一次 `mixw`：

[aes_encipher_block.v:85-101](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L85-L101) — 行 89~92 把 128 位拆成 4 个字，行 94~97 各调 `mixw`，行 99 拼回 128 位。注意 `mixw` 是**纯组合函数**，4 个字之间相互独立、天然并行。

#### 4.3.4 代码实践

**实践目标**：对一个示例 32 位字 \(w\)，手算 `mixw(w)` 的 4 个输出字节，对照源码确认 `gm2`/`gm3` 的异或组合正确。

**操作步骤**：取经典教材用例列 \(w = \text{0xdb135345}\)，即
- \(b_0 = \text{0xdb}\)，\(b_1 = \text{0x13}\)，\(b_2 = \text{0x53}\)，\(b_3 = \text{0x45}\)。

**第 1 步：算各字节需要的 gm2 / gm3。**

- `gm2(0xdb)`：`0xdb = 1101 1011`，最高位为 1 → 左移 `1011 0110 = 0xb6`，再异或 `0x1b` → `0xb6 ^ 0x1b = 0xad`。
- `gm2(0x13)`：`0x13 = 0001 0011`，最高位为 0 → 左移 `0010 0110 = 0x26`，不异或 → `0x26`。
- `gm2(0x53)`：最高位为 0 → 左移 `1010 0110 = 0xa6`，不异或 → `0xa6`。
- `gm2(0x45)`：最高位为 0 → 左移 `1000 1010 = 0x8a`，不异或 → `0x8a`。
- `gm3(x) = gm2(x) ^ x`：`gm3(0xdb)=0x76`，`gm3(0x13)=0x35`，`gm3(0x53)=0xf5`，`gm3(0x45)=0xcf`。

**第 2 步：套 `mixw` 公式。**

- \(mb_0 = \text{gm2}(b_0) \oplus \text{gm3}(b_1) \oplus b_2 \oplus b_3 = \text{0xad} \oplus \text{0x35} \oplus \text{0x53} \oplus \text{0x45} = \text{0x8e}\)
- \(mb_1 = b_0 \oplus \text{gm2}(b_1) \oplus \text{gm3}(b_2) \oplus b_3 = \text{0xdb} \oplus \text{0x26} \oplus \text{0xf5} \oplus \text{0x45} = \text{0x4d}\)
- \(mb_2 = b_0 \oplus b_1 \oplus \text{gm2}(b_2) \oplus \text{gm3}(b_3) = \text{0xdb} \oplus \text{0x13} \oplus \text{0xa6} \oplus \text{0xcf} = \text{0xa1}\)
- \(mb_3 = \text{gm3}(b_0) \oplus b_1 \oplus b_2 \oplus \text{gm2}(b_3) = \text{0x76} \oplus \text{0x13} \oplus \text{0x53} \oplus \text{0x8a} = \text{0xbc}\)

**预期结果**：`mixw(0xdb135345) = {0x8e, 0x4d, 0xa1, 0xbc} = 0x8e4da1bc`。这正是 AES 教材里 MixColumns 的标准答案（输入列 `db 13 53 45` → 输出列 `8e 4d a1 bc`），与源码第 76~79 行的异或组合完全吻合。**手算即可验证，无需运行仿真。**

#### 4.3.5 小练习与答案

- **练习 1**：手算 `gm2(0x57)` 和 `gm3(0x57)`。
  - **答案**：`0x57 = 0101 0111`，最高位为 0 → `gm2(0x57) = 1010 1110 = 0xae`；`gm3(0x57) = 0xae ^ 0x57 = 0xf9`。
- **练习 2**：为什么 MixColumns 的矩阵系数只用 1、2、3，而不用更大的数？
  - **答案**：1、2、3 都是 GF(2⁸) 上**可逆且实现极简**的乘法（×1 直接用，×2 是 xtime，×3 = ×2 ⊕ ×1），同时矩阵 \(M\) 在 GF(2⁸) 上可逆，保证了解密时能用逆矩阵还原。系数小也让硬件面积小、速度快。
- **练习 3**：`gm2` 里为什么是异或 `0x1b` 而不是 `0x11b`？
  - **答案**：因为字节只有 8 位，左移后产生的第 9 位（bit 8，即 `0x11b` 的最高位）通过「左移 + 异或低 8 位」隐式实现：左移一位本身就贡献了那个 \(x^8\)，异或 `0x1b`（= `0x11b` 去掉最高位）正好完成对不可约多项式 \(x^8+x^4+x^3+x+1\) 的取模归约。

---

### 4.4 AddRoundKey

#### 4.4.1 概念说明

AddRoundKey 把当前 state 与本轮的 128 位「轮密钥」逐位异或，是把密钥注入 state 的唯一步骤。异或在 GF(2⁸) 里就是加法，所以 AddRoundKey 既是「加」密钥，也是可逆的（再异或同一把密钥就还原）。它极其简单——一行异或——但每轮都做，配合每轮不同的轮密钥（由 u2-l3 的密钥扩展提供），保证密钥深刻参与每一轮的混淆扩散。

#### 4.4.2 核心流程

\[
\text{result} = \text{data} \oplus \text{rkey}
\]

128 位整体异或，没有任何字节顺序问题。本工程把同一份 `round_key`（由 `aes_core` 按轮号从 `key_mem` 取出）喂给加密和解密两条通路。

#### 4.4.3 源码精读

[aes_encipher_block.v:121-125](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L121-L125) — `addroundkey` 函数，整个函数体就是一句 `data ^ rkey`。

AddRoundKey 在三种轮里都被用到，只是「异或的对象」不同。看 `round_logic` 里的三处调用：

[aes_encipher_block.v:247-249](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L247-L249) —
- 行 247：`addkey_init_block = addroundkey(block, round_key)` —— 初始轮：**直接对输入明文 `block`** 异或轮密钥（此时还没经过 SubBytes/ShiftRows/MixColumns）。
- 行 248：`addkey_main_block = addroundkey(mixcolumns_block, round_key)` —— 中间轮：对 **SubBytes→ShiftRows→MixColumns** 之后的结果异或轮密钥。
- 行 249：`addkey_final_block = addroundkey(shiftrows_block, round_key)` —— 最终轮：对 **SubBytes→ShiftRows** 之后（**省去 MixColumns**）的结果异或轮密钥。

#### 4.4.4 代码实践

**实践目标**：手算一次 AddRoundKey，验证它就是逐位异或。

**操作步骤**：令 `data = 0x0a0b0c0d`，`rkey = 0x01020304`，逐字节异或。

**预期结果**：`0x0a^0x01=0x0b`，`0x0b^0x02=0x09`，`0x0c^0x03=0x0f`，`0x0d^0x04=0x09`，故 `addroundkey(0x0a0b0c0d, 0x01020304) = 0x0b090f09`。**手算即可验证。**

#### 4.4.5 小练习与答案

- **练习 1**：用同一把密钥对 AddRoundKey 的结果再做一次 AddRoundKey，会得到什么？为什么？
  - **答案**：得到原始 `data`。因为 \((a \oplus k) \oplus k = a\)，XOR 的自逆性使 AddRoundKey 可逆，这正是解密能逐轮「撤销」加密的基础。
- **练习 2**：初始轮的 AddRoundKey 异或的对象是 `block`（端口明文），而中间轮异或的是 `mixcolumns_block`。这说明初始轮与中间轮的数据通路有何不同？
  - **答案**：初始轮**没有** SubBytes/ShiftRows/MixColumns，直接对原始明文加轮密钥；中间轮则是先做完前三步变换再加轮密钥。

---

### 4.5 round_logic：把四个变换拼成三种轮

> 本节是把 4.1~4.4 串起来的「总装车间」，属于本讲的综合视角，时序细节留给 u2-l5。

#### 4.5.1 概念说明

`aes_encipher_block` 用一个组合 `always @*` 块 `round_logic` 决定「本拍要写回什么样的 `block_new`」。它先**一次性算好**三套候选结果（初始轮、中间轮、最终轮各一套），再根据 `update_type`（由 FSM 在 u2-l5 给出）选出哪一套写入。

关键观察：四个变换的「拼装关系」在这一段代码里一目了然。

#### 4.5.2 核心流程

伪代码（精简自 `round_logic`）：

```
old_block          = 当前 4 个字寄存器拼起来          # SubBytes 已在前面 SBOX 阶段写进这些寄存器
shiftrows_block    = shiftrows(old_block)            # ShiftRows
mixcolumns_block   = mixcolumns(shiftrows_block)     # ShiftRows → MixColumns
addkey_init_block  = addroundkey(block, round_key)   # 初始轮：明文 + 密钥
addkey_main_block  = addroundkey(mixcolumns_block, round_key)   # 中间轮：S→SR→MC + 密钥
addkey_final_block = addroundkey(shiftrows_block, round_key)    # 最终轮：S→SR + 密钥（无 MC）

case (update_type):
  INIT_UPDATE  → 写回 addkey_init_block
  SBOX_UPDATE  → 逐字写回 new_sboxw（SubBytes，见 4.1）
  MAIN_UPDATE  → 写回 addkey_main_block
  FINAL_UPDATE → 写回 addkey_final_block
```

注意一个精妙的衔接点：`shiftrows` / `mixcolumns` 的输入是 `old_block`（= 4 个 `block_w*_reg`），而这些寄存器在 `SBOX_UPDATE` 阶段已经被 SubBytes 的结果覆盖过。所以当 FSM 走到 `MAIN_UPDATE` 时，`old_block` 已经是「SubBytes 之后」的状态，于是自然实现了 **SubBytes → ShiftRows → MixColumns → AddRoundKey** 的顺序，无需显式传递中间变量。

#### 4.5.3 源码精读

三套候选结果的计算：

[aes_encipher_block.v:244-249](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L244-L249) — `shiftrows_block` 与 `mixcolumns_block` 串成 `ShiftRows → MixColumns`；三个 `addroundkey` 分别给初始轮、中间轮、最终轮。

按 `update_type` 选结果写回：

[aes_encipher_block.v:251-313](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L251-L313) — `INIT_UPDATE`（行 252~259）写初始轮结果；`SBOX_UPDATE`（行 261~290）逐字做 SubBytes；`MAIN_UPDATE`（行 292~299）写中间轮结果（含 MixColumns）；`FINAL_UPDATE`（行 301~308）写最终轮结果（**无 MixColumns**）。

> 这一节体现了 u1-l3 讲过的「组合块开头先给所有输出写默认值」的约定：行 237~242 先把 `block_new`、各 `_we` 清零，再在 `case` 里按需置位，避免生成锁存器。

#### 4.5.4 代码实践

**实践目标**：把「三种轮 = 不同的函数组合」这条结论落到具体代码行。

**操作步骤**：

1. 在 `round_logic` 里找到 `mixcolumns_block` 的定义（行 246），确认它只被 `addkey_main_block`（行 248）使用、而最终轮 `addkey_final_block`（行 249）用的是 `shiftrows_block`（**不含** `mixcolumns_block`）。
2. 对照 4.5.2 的伪代码，验证：初始轮只含 AddRoundKey、中间轮含 ShiftRows+MixColumns+AddRoundKey、最终轮含 ShiftRows+AddRoundKey（缺 MixColumns）。
3. 思考：SubBytes 在这张图里出现在哪里？

**预期结果**：你能说清「SubBytes 由 `SBOX_UPDATE` 单独完成、其结果存回寄存器后被 `old_block` 读出，再喂给 ShiftRows/MixColumns」。**这是源码阅读型实践，无需运行。**

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `round_logic` 要同时算好 `addkey_init_block`、`addkey_main_block`、`addkey_final_block` 三套，而不是只算当前需要的那一套？
  - **答案**：这是纯组合逻辑块，硬件上三套电路本就并行存在、同时算好，由 `update_type` 选哪一套写回寄存器。分别写三段 `case` 里现算也可以，但作者选择在块开头统一算好，代码更清晰、也便于综合工具优化共享子表达式（如 `shiftrows_block` 被主轮和最终轮共用）。
- **练习 2**：若把最终轮误写成 `addroundkey(mixcolumns_block, round_key)`（即最终轮也做 MixColumns），会怎样？
  - **答案**：加密结果将错误，与 AES 标准不符，NIST 测试向量（u3-l2）会失败、`error_ctr` 不为 0。最终轮省去 MixColumns 是 AES 规范的硬性要求。

## 5. 综合实践

**任务**：在纸上把「一个中间轮」的完整数据通路走一遍，把四个变换串起来。

给定（任意取值，仅为演示）一个 128 位中间态和本轮 128 位轮密钥：

- 中间态（按字，左为行0）：\(w_0=\text{0xdb135345}\)，\(w_1=\text{0x02030405}\)，\(w_2=\text{0x06070809}\)，\(w_3=\text{0x0a0b0c0d}\)。
- 本轮轮密钥：\(\text{0x00000000...0000}\)（全 0，方便手算，AddRoundKey 不改变内容）。

**要求**：

1. 说明本轮会依次经过 SubBytes → ShiftRows → MixColumns → AddRoundKey。
2. 只对 \(w_0\) 这一列，套用本讲 4.3.4 已经验证过的结论 `mixw(0xdb135345)=0x8e4da1bc`，指出：**如果** SubBytes 已把 \(w_0\) 变成 `0xdb135345`（仅为演算假设），那么经过 ShiftRows（取 \(ws_0\) 的行0 字节来自 \(w_0\) 行0）与 MixColumns 后，\(w_0\) 对应列的 MixColumns 输出会是 `0x8e4da1bc`。
3. 写出 `round_logic` 里对应的代码行：SubBytes 在 `SBOX_UPDATE`（行 261~290）、ShiftRows+MixColumns 在行 245~246、中间轮 AddRoundKey 在行 248 与 292~299。
4. 反思：为什么全 0 轮密钥下，AddRoundKey 这一步「看起来没作用」，但真实密钥下它会彻底改变每一轮的结果？

**预期结果**：你能画出一条 `old_block →(SubBytes 已写入)→ shiftrows → mixcolumns → addroundkey → block_new` 的数据流，并标注每一步对应的函数与行号。这道题把本讲四个变换与 `round_logic` 串成了一条完整的一轮数据通路，为 u2-l5（轮控制 FSM）打好基础。

## 6. 本讲小结

- AES 一轮 = **SubBytes → ShiftRows → MixColumns → AddRoundKey**；初始轮只有 AddRoundKey，最终轮省去 MixColumns。
- **SubBytes** 在本模块是「外接」的：通过 `sboxw`/`new_sboxw` 端口借用 `aes_core` 里的共享 S-box，逐字 4 拍完成（用时间换面积）。
- **ShiftRows** 是纯字节置换，按行循环左移 0/1/2/3 字节，靠跨字的位拼接实现。
- **MixColumns** 是 GF(2⁸) 矩阵乘法；`gm2`（左移+条件异或 0x1b）实现 ×2，`gm3=gm2⊕op` 实现 ×3，`mixw` 对一列套用固定矩阵，`mixcolumns` 对 4 列各调一次。
- **AddRoundKey** 就是一句 `data ^ rkey`，初始轮对明文做、中间轮对 ShiftRows+MixColumns 结果做、最终轮对 ShiftRows 结果做。
- `round_logic` 这一个组合块一次性算好三种轮的候选结果，由 `update_type` 选写哪一套；SubBytes 的结果通过寄存器「隐式」流入 ShiftRows/MixColumns。

## 7. 下一步学习建议

- **下一步学 u2-l5「加密轮控制状态机」**：本讲的四个变换是「算什么」，u2-l5 讲「**什么时候算、算几轮**」——即 `encipher_ctrl`（IDLE/INIT/SBOX/MAIN/FINAL）状态机、`sword_ctr`（逐字 SubBytes 计数）、`round_ctr`（轮计数）如何驱动 `update_type`，把本讲的函数在时间轴上排开。
- 若想对比加密与解密的「逆」函数（InvSubBytes / InvShiftRows / InvMixColumns，以及 gm09/11/13/14 如何由 gm2/gm4/gm8 复合而成），可跳到 **u2-l6**。
- 想看这些函数被真实 NIST 向量检验，可读 **u3-l2** 的 testbench 自检逻辑，并用 `mixw(0xdb135345)=0x8e4da1bc` 这个结论去校对 MixColumns 的正确性。
