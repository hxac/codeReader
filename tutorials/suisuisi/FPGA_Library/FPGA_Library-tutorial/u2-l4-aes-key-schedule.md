# 密钥扩展 Key Schedule

## 1. 本讲目标

AES-128 加密时，初始轮和 10 个轮变换里各要做一次 AddRoundKey（把状态与一个 128 位「轮密钥」逐位异或）。10 轮加密一共需要 **11 个轮密钥**，可如果让用户直接提供 11 个 128 位密钥，密钥管理会变得不现实。Key Schedule（密钥扩展）就是用一个 128 位种子密钥，**确定性、可逆地**推导出全部 11 个轮密钥的算法。

学完本讲，你应当能够：

- 说清 AES-128 密钥扩展里 4 个 32 位字 \(W_0{\sim}W_3\) 的异或递推关系，并能手算前两轮的轮密钥；
- 看懂本仓库 `function_g` 如何用「RotWord + SubWord + Rcon」实现非线性变换 \(g\)；
- 理解轮常数 Rcon 的数学含义，以及 `round_constants` 模块为何用一坨组合逻辑（而不是查表）来产生它；
- 识别本仓库 RTL 与 FIPS-197 标准的一处偏差（密钥扩展是否应当随加/解密切换 S-Box），并知道「以仿真为准」。

本讲只讲密钥扩展本身；这些轮密钥如何被 AddRoundKey 消费、加解密如何共用同一条数据通路，已经在 [u2-l1](u2-l1-aes-top-architecture.md) 讲过，后续 [u2-l5](u2-l5-aes-sbox-composite-field.md) 会深入 S-Box 的复合域实现。

## 2. 前置知识

在进入源码前，先用通俗语言把三个概念讲清楚。

**（1）为什么要「扩展」密钥？** AES 是迭代型密码：同一组变换（SubBytes / ShiftRows / MixColumns / AddRoundKey）反复执行多轮。每一轮都混入不同的轮密钥，才能让密文的每一位都对密钥的每一位高度敏感——这叫「扩散」与「混淆」。直接重复使用同一个 128 位密钥会严重削弱安全性，所以需要一套算法把它「拉长」成 11 个互不相同的轮密钥。

**（2）什么是「字（word）」？** AES 把 128 位密钥与状态都看成 **4 个 32 位字** 的序列。对 AES-128，种子密钥就是 \(W_0, W_1, W_2, W_3\)；扩展算法继续算出 \(W_4, W_5, \dots, W_{43}\)，共 44 个字。每相邻 4 个字组成一个 128 位轮密钥：第 0 个轮密钥是 \(W_0{\sim}W_3\)，第 1 个是 \(W_4{\sim}W_7\)，依此类推。

**（3）异或（XOR）与 GF(2⁸) 回顾。** 本讲的运算只有两种：逐位异或（在 GF(2) 上等价于加法），以及 S-Box 字节替换（GF(2⁸) 上的求逆 + 仿射变换，详见 [u2-l3](u2-l3-aes-mixcolumns-gf.md) 与 [u2-l5](u2-l5-aes-sbox-composite-field.md)）。异或有一个关键性质：\(a \oplus a = 0\)，且异或可逆——这正是密钥扩展能用一连串异或「自洽地」推导出新字的数学基础。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/` 下）：

| 文件 | 作用 |
|------|------|
| [src/aes_key_schedule.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_key_schedule.v) | 顶层密钥扩展模块 `key_schedule`：把当前轮密钥的 4 个字做异或递推，算出下一轮密钥。 |
| [utils/aes_function_g.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_function_g.v) | 非线性函数 \(g\)：对最后一个字做「旋转 + S-Box 替换 + 轮常数异或」。 |
| [utils/aes_round_constants.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_round_constants.v) | 轮常数 Rcon 的组合逻辑实现，输入轮序号、输出 8 位常数。 |

辅助理解（非本讲精读对象，但会被引用）：

- [src/aes_top.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v) 例化了 `key_schedule`，并用 `round_counter` 驱动它逐轮推导。
- [utils/aes_types.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v) 集中定义了 `` `u32 ``、`` `u8 ``、`` `u4 ``、`KEY_SIZE` 等宏。
- [src/aes_s_box.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v) 提供 `function_g` 例化的 `bSbox` 模块。
- [VE_sv/ve_AES_Core.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv) 是 SystemVerilog 写的黄金参考模型，其中的 `KeyExpansion` 函数是判断 RTL 正确性的标尺。

---

## 4. 核心概念与源码讲解

### 4.1 key_schedule：轮密钥的异或递推

#### 4.1.1 概念说明

`key_schedule` 解决的问题是：**给定第 \(r\) 轮的 4 个字，算出第 \(r+1\) 轮的 4 个字。** 它是一个纯组合模块——没有时钟、没有寄存器，输入一变，输出立刻（经过组合逻辑延迟后）更新。真正「记住」当前轮密钥、按轮推进的是顶层 `aes_top` 里的寄存器，本模块只负责「算下一步」。

AES-128 的字递推规则（\(i\) 从 4 开始，每 4 个字为一组）：

\[
W_i =
\begin{cases}
W_{i-4} \oplus g(W_{i-1}), & i \bmod 4 = 0 \\
W_{i-4} \oplus W_{i-1}, & i \bmod 4 \neq 0
\end{cases}
\]

也就是说，每个新字都等于「4 个字之前的那个字」异或「前一个字」；唯独每组的第 1 个字（\(i\) 是 4 的倍数）要把「前一个字」换成非线性函数 \(g\) 的输出，从而注入非线性与轮间差异。

#### 4.1.2 核心流程

把上面的通式套到一组 4 个字上（设当前轮密钥为 \(W_0,W_1,W_2,W_3\)，下一轮为带撇的 \(W'_0{\sim}W'_3\)）：

```
g_out = g(W_3)                    # 只对最后一个字做非线性变换
W'_0 = W_0 ⊕ g_out                # 第 1 个字：注入 g
W'_1 = W_1 ⊕ W'_0                 # 链式异或：每个新字依赖前一个“新字”
W'_2 = W_2 ⊕ W'_1
W'_3 = W_3 ⊕ W'_2
```

注意是「链式」的：\(W'_1\) 依赖 \(W'_0\) 而不是 \(W_0\)，\(W'_2\) 依赖 \(W'_1\)…… 这样 4 个新字会快速「雪崩」地吸收 \(g\) 注入的非线性。这正是下面源码里 `W1_new = W1 ^ W0_new`（而不是 `W1 ^ W0`）的原因。

#### 4.1.3 源码精读

先看端口与内部信号（[aes_key_schedule.v:L32-L49](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_key_schedule.v#L32-L49)）：

```verilog
module key_schedule (key_in, encrypt, round_index,  key_out);
    input  [`KEY_SIZE-1: 0] key_in;     // 当前轮密钥（128 位）
    input  encrypt;                     // 加密/解密选择（见 4.2 的讨论）
    input  `u4 round_index;             // 轮序号，用于选 Rcon
    output [`KEY_SIZE-1: 0] key_out;    // 下一轮密钥（128 位）

    wire `u32 W0, W1, W2, W3;           // 当前轮的 4 个字
    wire `u32 W0_new, W1_new, W2_new, W3_new;  // 下一轮的 4 个字
    wire `u32 g_out;                    // g(W3) 的结果

    function_g g_inst(W3, encrypt, round_index, g_out);   // 例化 g

    assign W0  = key_in [31 : 0];       // 注意：W0 是最低位字
    assign W1  = key_in [63 :32];
    assign W2  = key_in [95 :64];
    assign W3  = key_in [127:96];       // W3 是最高位字
```

这里 `` `u32 ``、`` `u4 ``、`` `KEY_SIZE `` 都是宏，定义在 [aes_types.v:L26-L36](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v#L26-L36)（`` `u32 ``=`[31:0]`，`KEY_SIZE`=128）。

> ⚠️ **字序约定坑**：本仓库里 `W0 = key_in[31:0]`（最低位字），`W3 = key_in[127:96]`（最高位字）。所以当你把种子密钥按十六进制「从左到右」写成 4 个字时，**最左边的字对应 RTL 的 `W3`**。读代码和写 testbench 时务必记住这一映射，否则手算结果会和仿真对不上。

接着是核心的链式异或（[aes_key_schedule.v:L51-L59](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_key_schedule.v#L51-L59)）：

```verilog
assign W0_new = W0 ^ g_out;        // 注入非线性 g
assign W1_new = W1 ^ W0_new;       // 链式：异或的是“新字”
assign W2_new = W2 ^ W1_new;
assign W3_new = W3 ^ W2_new;

assign key_out [31 : 0] = W0_new;
assign key_out [63 :32] = W1_new;
assign key_out [95 :64] = W2_new;
assign key_out [127:96] = W3_new;
```

这 4 行 `assign` 就是 4.1.2 里那段伪代码的一一对应。整个模块没有任何时序逻辑——它是一个「组合函数」：`key_out = f(key_in, round_index, encrypt)`。

那么谁负责「逐轮推进」呢？是顶层 `aes_top`（[aes_top.v:L111-L114](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L111-L114)）：

```verilog
if (round_counter < `NO_OF_ROUNDS) begin
    round_counter <= round_counter + 1'b1;
    round_key     <= round_key_out;   // 把 key_schedule 的输出锁存为下一轮密钥
end
```

而例化语句是 [aes_top.v:L128](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L128)：

```verilog
key_schedule key_schedule_inst(round_key, encrypt, round_counter, round_key_out);
```

可以看到 `round_counter`（4 位，复位为 0，每拍 +1 直到 10）被当作 `round_index` 喂给密钥扩展，进而决定本轮用哪个 Rcon。

#### 4.1.4 代码实践

**实践目标**：用「读代码 + 手算」验证 `key_schedule` 的递推结构确实与标准一致。

**操作步骤**：

1. 打开 [aes_key_schedule.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_key_schedule.v)，确认 `W0_new` 用到 `g_out`，而 `W1_new/W2_new/W3_new` 分别异或「前一个新字」。
2. 取 AES-128 官方测试向量种子密钥 `2b7e1516 28aed2a6 abf71588 09cf4f3c`（FIPS-197 附录 A.1），把它当作 \(W_0,W_1,W_2,W_3\)。
3. 假设 `g_out` 已经算好（下一节会算），验证下面 4 个新字（见 4.2.4 的完整手算）等于官方第 1 轮密钥 `a0fafe17 88542cb1 23a33939 2a6c7605`。

**需要观察的现象**：链式异或让 `g_out` 的影响从 `W0_new` 一路传播到 `W3_new`——只要 `g_out` 一个字变了，4 个新字全部改变。

**预期结果**：4.2.4 节给出逐步推导，最终与官方第 1 轮密钥逐字节吻合。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `W1_new = W1 ^ W0_new` 误写成 `W1_new = W1 ^ W0`（异或旧字而不是新字），扩展出的密钥还正确吗？

> **答案**：不正确。标准 AES 要求链式异或新字（即 \(W'_i = W_i \oplus W'_{i-1}\)）。改成异或旧字后，\(g\) 的非线性只能影响 \(W'_0\) 一个字，其余 3 个字退化为「旧字间的简单异或」，与标准结果不一致，解密时会得到错误明文。

**练习 2**：`key_schedule` 模块本身有没有寄存器？它每轮产出一个新密钥，是靠什么「记住」当前密钥的？

> **答案**：模块内全是 `wire` 和 `assign`，没有寄存器，是纯组合逻辑。当前轮密钥由顶层 `aes_top` 的寄存器 `round_key` 保持，每拍把 `key_schedule` 的组合输出 `round_key_out` 锁存回去，从而实现「逐轮推进」。

---

### 4.2 function_g：RotWord + SubWord + Rcon 的组合

#### 4.2.1 概念说明

\(g\) 是密钥扩展里**唯一的非线性来源**。它对一个 32 位字做三件事：

1. **RotWord（字旋转）**：把 4 个字节循环左移一位，\([b_0,b_1,b_2,b_3] \to [b_1,b_2,b_3,b_0]\)。
2. **SubWord（字替换）**：对旋转后的每个字节过一次正向 S-Box。
3. **加 Rcon（轮常数）**：只在最高位字节上异或一个轮常数。

写成公式：

\[
g(W) = \mathrm{SubWord}\bigl(\mathrm{RotWord}(W)\bigr) \oplus \mathrm{Rcon}
\]

其中 Rcon 是一个「只有最高位字节非零」的字，形如 \((\text{rc},\,00,\,00,\,00)\)。为什么要旋转 + 替换？因为单纯异或是线性的，攻击者可以用线性代数工具倒推密钥；旋转打乱字节位置、S-Box 注入非线性，两者一起让密钥扩展具备「抗代数攻击」的强度。

#### 4.2.2 核心流程

设输入字 \(W = [b_0,b_1,b_2,b_3]\)（\(b_0\) 是最高位字节）：

```
t        = RotWord(W)            # [b1, b2, b3, b0]
s        = SubWord(t)            # [S[b1], S[b2], S[b3], S[b0]]
g_out    = s ⊕ (rc, 00, 00, 00)  # 只有第一个字节被 rc 改写
```

注意：RotWord 和 SubWord 的顺序在数学上可交换（先替换再旋转，每个字节一一对应），但本仓库的实现是**先把替换后的字节摆到旋转后的位置**——等价于「先旋转再替换」，结果一致。

#### 4.2.3 源码精读

先看字节切片与 S-Box 例化（[aes_function_g.v:L18-L40](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_function_g.v#L18-L40)）：

```verilog
module function_g(data_in, encrypt, round_no, data_out);
    input  `u32 data_in;
    input          encrypt;
    input  `u4     round_no;
    output `u32    data_out;

    assign v0 = data_in[31:24];   // 最高位字节
    assign v1 = data_in[23:16];
    assign v2 = data_in[15: 8];
    assign v3 = data_in[ 7: 0];   // 最低位字节

    bSbox s0(A.(v0), .encrypt(encrypt), .Q(v0_out));  // 4 个字节各过一个 S-Box
    bSbox s1(A.(v1), .encrypt(encrypt), .Q(v1_out));
    bSbox s2(A.(v2), .encrypt(encrypt), .Q(v2_out));
    bSbox s3(A.(v3), .encrypt(encrypt), .Q(v3_out));

    round_constants rc_inst (round_no, rc_i);   // 查 Rcon
```

每个 `bSbox` 把一个字节映射成它的（正/逆）S-Box 输出，由 `encrypt` 选择方向（[aes_s_box.v:L21-L24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L21-L24)：`encrypt=1` 走正向 S-Box，`=0` 走逆 S-Box）。

> ⚠️ **两处草稿级隐患（与前几讲一致，需以仿真为准）**：
> - 端口连接写成 `A.(v0)`，标准 Verilog 命名端口连接应是 `.A(v0)`。严格的仿真/综合工具会报语法错；本仓库能跑通说明用了宽松工具链或手工修补。阅读时按 `.A(v0)` 理解即可。
> - 第 25 行声明了 `v0_in,v1_in,v2_in,v_3in` 等 `wire` 却从未使用，是无害的死代码，但也说明这是未清理的草稿。

接着是「旋转 + 加 Rcon」的输出组装（[aes_function_g.v:L44-L47](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_function_g.v#L44-L47)）：

```verilog
assign data_out[31:24] = v1_out ^ rc_i;   # 旋转：v1 摆到最高位；并异或 Rcon
assign data_out[23:16] = v2_out;          # v2 摆到次高位
assign data_out[15: 8] = v3_out;          # v3
assign data_out[ 7: 0] = v0_out;          # v0 循环回到最低位
```

输出字节序是 \([v_1, v_2, v_3, v_0]\)（MSB→LSB），正是「左移一位」的 RotWord；每个字节都已经是 S-Box 输出，等价于 SubWord；`rc_i` 只异或进最高位字节，对应 Rcon 的 \((\text{rc},00,00,00)\) 形态。三步一气呵成。

> 🔎 **重要偏差：密钥扩展是否该随加解密切换 S-Box？**
> 按 FIPS-197 标准，**密钥扩展永远只用正向 S-Box**，加密和解密推导出的 11 个轮密钥是完全相同的（只是解密时按相反顺序使用它们）。本仓库的黄金参考模型也这么做——[ve_AES_Core.sv:L189](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv#L189) 里 `KeyExpansion` 用的是 `computed_val.S`（正向表），与加解密方向无关。
> 但本 RTL 把 `encrypt` 一路传进了 `function_g` 的 `bSbox`，于是**解密时密钥扩展会改用逆 S-Box**，产生的轮密钥与正向不同——这与标准及黄金模型不一致。这是否会导致解密错误，取决于 `aes_top` 统一数据通路里其它部分是否做了配套补偿；**结论待本地仿真验证**（见第 5 节综合实践）。读代码时请把这一点记在心里。

#### 4.2.4 代码实践

**实践目标**：用 FIPS-197 官方密钥手算前两轮（\(W_4{\sim}W_{11}\)），并逐字节验证 `function_g` 的「旋转 + 替换 + 加 Rcon」逻辑。

**操作步骤**：

种子密钥：\(W_{0\sim3} = \texttt{2b7e1516}\;\texttt{28aed2a6}\;\texttt{abf71588}\;\texttt{09cf4f3c}\)。

**第 1 轮**（\(W_4{\sim}W_7\)，用 Rcon[1] = 0x01）：

1. \(g(W_3)\)：\(W_3=\texttt{09cf4f3c}\)，字节 \([09,\text{cf},4\text{f},3\text{c}]\)。
   - RotWord → \([\text{cf},4\text{f},3\text{c},09]\)
   - SubWord → \([\text{S[cf]},\text{S[4f]},\text{S[3c]},\text{S[09]}] = [8\text{a},84,\text{eb},01]\)
   - ⊕ Rcon[1] \((01,00,00,00)\) → \([8\text{b},84,\text{eb},01]\)，即 \(g(W_3)=\texttt{8b84eb01}\)
2. 链式异或：
   - \(W_4 = W_0 \oplus g(W_3) = \texttt{2b7e1516} \oplus \texttt{8b84eb01} = \texttt{a0fafe17}\)
   - \(W_5 = W_1 \oplus W_4 = \texttt{28aed2a6} \oplus \texttt{a0fafe17} = \texttt{88542cb1}\)
   - \(W_6 = W_2 \oplus W_5 = \texttt{abf71588} \oplus \texttt{88542cb1} = \texttt{23a33939}\)
   - \(W_7 = W_3 \oplus W_6 = \texttt{09cf4f3c} \oplus \texttt{23a33939} = \texttt{2a6c7605}\)

   得第 1 轮密钥 `a0fafe17 88542cb1 23a33939 2a6c7605`，与 FIPS-197 官方值逐字节吻合 ✅。

**第 2 轮**（\(W_8{\sim}W_{11}\)，用 Rcon[2] = 0x02，待本地验证）：

1. \(g(W_7)\)：\(W_7=\texttt{2a6c7605}\)，字节 \([2\text{a},6\text{c},76,05]\)。
   - RotWord → \([6\text{c},76,05,2\text{a}]\)
   - SubWord → \([\text{S[6c]},\text{S[76]},\text{S[05]},\text{S[2a]}] = [50,38,6\text{b},\text{e5}]\)
   - ⊕ Rcon[2] \((02,00,00,00)\) → \([52,38,6\text{b},\text{e5}]\)，即 \(g(W_7)=\texttt{52386be5}\)
2. 链式异或（按本仓库 RTL 逻辑推得）：
   - \(W_8 = \texttt{a0fafe17} \oplus \texttt{52386be5} = \texttt{f2c295f2}\)
   - \(W_9 = \texttt{88542cb1} \oplus \texttt{f2c295f2} = \texttt{7a96b943}\)
   - \(W_{10} = \texttt{23a33939} \oplus \texttt{7a96b943} = \texttt{5935807a}\)
   - \(W_{11} = \texttt{2a6c7605} \oplus \texttt{5935807a} = \texttt{7359f67f}\)

   得第 2 轮密钥 `f2c295f2 7a96b943 5935807a 7359f67f`。

**需要观察的现象**：每组的第 1 个字（\(W_4, W_8\)）经过了完整的「旋转+S-Box+Rcon」，而同组其余 3 个字只是链式异或——非线性「注入点」每 4 个字才出现一次。

**预期结果 / 待本地验证**：第 1 轮已与官方值吻合；第 2 轮为按 RTL 逻辑推得的结果，请用参考实现（如 Python `pycryptodome` 的内部扩展、或 NIST CAVP 向量）核对，尤其要确认你采用的字节序与本仓库 `W3=key_in[127:96]` 的约定一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Rcon 只异或到最高位字节，而不是 4 个字节都异或？

> **答案**：Rcon 的作用是「给每一轮注入一个轮间互不相同的常数」，从而让各轮密钥彼此区分。把常数只放在一个字节上已经足以达到这个目的；同时 Rcon 本身是 GF(2⁸) 上不断「乘 2」的序列（见 4.3），单字节表达最简洁。标准如此定义，实现就照此忠实还原。

**练习 2**：把 `function_g` 里 `data_out[7:0] = v0_out` 改成 `data_out[7:0] = v3_out`，会破坏什么？

> **答案**：会破坏 RotWord。原代码输出序是 \([v_1,v_2,v_3,v_0]\)（左移一位），改后变成 \([v_1,v_2,v_3,v_3]\)——既不是合法旋转、又丢了 \(v_0\) 的信息。后续所有轮密钥都会算错，加解密失败。

---

### 4.3 aes_round_constants：用组合逻辑实现 Rcon 查找表

#### 4.3.1 概念说明

轮常数 Rcon 是一个有 10 个取值的序列（AES-128 只需前 10 个）。它本质上是 GF(2⁸) 上不断「乘以 \(x\)」（即 xtime，乘 2）的迭代结果：

\[
\text{rc}_j = x^{j-1} \bmod m(x), \quad m(x)=x^8+x^4+x^3+x+1
\]

写成字节（前 10 个）：

| \(j\) | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|------|---|---|---|---|---|---|---|---|---|----|
| rc\(_j\) | 01 | 02 | 04 | 08 | 10 | 20 | 40 | 80 | 1B | 36 |

注意第 9 个值：\(\text{rc}_9 = \text{xtime}(0\text{x}80)\)。\(0\text{x}80\) 是 \(x^7\)，左移一位得到 \(x^8\)，超过一个字节，要用 \(m(x)\) 取模：\(x^8 \bmod m(x) = x^4+x^3+x+1 = \texttt{0x1B}\)（关于这个不可约多项式，详见 [u2-l3](u2-l3-aes-mixcolumns-gf.md)）。第 10 个 \(\text{rc}_{10}=\text{xtime}(0\text{x}1B)=0\text{x}36\)（最高位为 0，无需约简）。

#### 4.3.2 核心流程

最直观的实现是把这张 10 项的小表放进一个 ROM 里查。但本仓库的 `round_constants` 选择了另一条路：**把这张真值表直接展开成一堆与/或/非门**——也就是说，它把「输入 4 位轮序号 → 输出 8 位 Rcon」看成一个 4 输入 8 输出的组合逻辑函数，对每个输出位手写（或综合工具自动生成的）最小项表达式。

输入是 `round_counter`（0~9，对应 Rcon 第 1~10 项）。注意这个映射有一个**偏移 1**：`round_index=0` 产出 Rcon 的第 1 项 `0x01`，`round_index=9` 产出第 10 项 `0x36`。

#### 4.3.3 源码精读

整个模块只有 16 行（[aes_round_constants.v:L1-L16](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_round_constants.v#L1-L16)）：

```verilog
module round_constants(i,Rcon);
    input  [3:0] i;
    wire   [7:0] b;
    output [7:0] Rcon;

    assign b[0] = (~i[2])&(~i[1])&(~i[0]);          // ...8 个最小项表达式...
    /* b[1]..b[7] 各自是一段与/或/非组合逻辑 */
    assign Rcon = b;
endmodule
```

每个 `b[k]` 都是把「4 位输入 `i`」映射到「该输出位」的真值表表达式。比如 `b[0]` 在 `i=0`（即 `i[2:0]=000`）时为 1，对应 `0x01` 的最低位；当 `i` 取其它值时，`b[0]` 由对应的最小项置位（例如 `i=8` 时整个字节为 `0x1B`）。

> 这是一段「能跑但不宜读」的代码：它不是为人类可读性写的，而是真值表的门级展开。读它的正确方式不是逐行理解每个最小项，而是**用仿真把 10 个输入逐一跑一遍**，确认输出表正是 `01,02,04,08,10,20,40,80,1B,36`。

这种「查表展开成组合逻辑」的写法在 FPGA 上有个好处：不需要占用一块 Block RAM，只用少量 LUT 就能实现；代价是可读性差。本仓库在 S-Box 上走了相反的路（用复合域运算而非查表，见 [u2-l5](u2-l5-aes-sbox-composite-field.md)），风格上并不统一——这再次印证它是学习/草稿级 RTL。

#### 4.3.4 代码实践

**实践目标**：验证 `round_constants` 的 10 个输出确实是标准 Rcon 序列。

**操作步骤**（源码阅读型，无需开发板）：

1. 打开 [aes_round_constants.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_round_constants.v)。
2. 写一个 4 行的 testbench，对 `i` 从 0 扫到 9，`$display` 出 `Rcon`：

   ```verilog
   // 示例代码：最小激励（仅供说明，非仓库自带文件）
   reg  [3:0] i; wire [7:0] r;
   round_constants dut(.i(i), .Rcon(r));
   initial begin
     for (i = 0; i <= 9; i = i + 1) #10 $display("i=%0d Rcon=%02h", i, r);
     $finish;
   end
   ```

3. 把打印结果与 4.3.1 的标准表逐项比对。

**需要观察的现象**：输出序列应为 `01 02 04 08 10 20 40 80 1b 36`。

**预期结果**：逐项吻合；特别留意 `i=8 → 1b`（发生了 GF 模约简）和 `i=9 → 36`。若用 Icarus Verilog，编译时需把 `aes_types.v` 中的宏包含进来；若工具对 `function_g` 的 `A.(v0)` 语法报错，可单独只测本模块（它没有这个语法问题）。**完整上板/仿真流程待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `round_constants` 的输入是 4 位？10 轮只需要 10 个值，3 位（0~7）不够、4 位（0~15）是不是浪费？

> **答案**：4 位是因为它在顶层直接接 `round_counter`（[aes_top.v:L65](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v#L65) 声明为 `reg [3:0]`），而 `round_counter` 要能表示到 10（二进制 `1010`），3 位最大只能到 7，不够。多出来的编码（10~15）在本设计中不会发生（计数到 10 即复位），属于无害冗余。

**练习 2**：把这张 Rcon 表改用一块同步 ROM（`case` 语句 + 寄存器输出）实现，会对 `key_schedule` 的时序产生什么影响？

> **答案**：原实现是纯组合的，`Rcon` 在 `i` 变化后「很快」就稳定。改成同步 ROM 后，`Rcon` 要晚一拍才有效；由于 `key_schedule` 也是组合逻辑，`round_key_out` 会相应推迟一拍，顶层 `aes_top` 的逐轮节拍需要配套调整（例如多花一拍或提前一格取 Rcon），否则轮密钥会错位。这正体现了「组合 vs 时序」的权衡。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「密钥扩展端到端验证」。

**任务**：以 `2b7e1516 28aed2a6 abf71588 09cf4f3c` 为种子密钥，做下面三件事：

1. **手算**：按 4.2.4 的方法，手算出第 1、第 2 轮密钥（\(W_4{\sim}W_{11}\)），并把每个 `g` 的中间结果（RotWord 后、SubWord 后、加 Rcon 后）都列出来。
2. **追源码**：在 [aes_key_schedule.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_key_schedule.v) 与 [aes_function_g.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_function_g.v) 里标出：哪几行实现「RotWord」、哪几行实现「SubWord」、哪一行实现「加 Rcon」、哪几行实现「链式异或」。用一句话总结 `encrypt` 信号是如何一路传到 S-Box 的。
3. **验偏差**：参考 [ve_AES_Core.sv:L166-L225](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/VE_sv/ve_AES_Core.sv#L166-L225) 的黄金 `KeyExpansion`（注意它恒用正向 S-Box），写一段简短说明：**若把 RTL 的 `encrypt` 在密钥扩展里强制接成 `1'b1`（始终用正向 S-Box），会不会更符合 FIPS-197？这样的改动可能让哪一条路径（加密 / 解密）的行为发生变化？** 并指出最终结论需要通过哪种手段确认（提示：仿真比对黄金模型）。

**交付物**：一张「字 → 推导过程 → 最终值」的表（覆盖 \(W_4{\sim}W_{11}\)），加上一段对 4.2.3 中「encrypt 传入密钥扩展」偏差的分析。

> 说明：本仓库随仓库发布的工程脚本含有 Windows 硬编码路径等问题（见 [u1-l3](u1-l3-vivado-project-template.md)），直接 `source create_project.tcl` 未必能一次跑通。若无法在本地建工程，至少完成第 1、2 步的「手算 + 读码」；第 3 步可用 Icarus Verilog / Verilator 单独仿真这三个模块（注意补齐宏定义并规避 `A.(v0)` 语法问题），结论标注「待本地验证」即可。

## 6. 本讲小结

- AES-128 用一个 128 位种子密钥，通过密钥扩展确定性地产出 11 个轮密钥（44 个 32 位字 \(W_0{\sim}W_{43}\)）。
- 递推核心：每组的第 1 个字 \(W'_0 = W_0 \oplus g(W_3)\)，其余字链式异或 \(W'_i = W_i \oplus W'_{i-1}\)；本仓库 [aes_key_schedule.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_key_schedule.v) 用 4 行 `assign` 忠实实现了这一结构。
- \(g\) = RotWord + SubWord + Rcon，是密钥扩展唯一的非线性来源；本仓库 [aes_function_g.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_function_g.v) 用「字节摆位 + 4 个 `bSbox` + 单字节异或」一步完成。
- Rcon 是 GF(2⁸) 上反复 xtime 的序列（`01,02,…,80,1B,36`）；[aes_round_constants.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_round_constants.v) 把这张表展开成纯组合逻辑，无 RAM 占用。
- `key_schedule` 是纯组合模块，逐轮推进靠顶层 `aes_top` 的 `round_key` 寄存器与 `round_counter` 协作。
- ⚠️ 草稿级隐患：`function_g` 存在 `A.(v0)` 非标准端口写法与死代码；更关键的是它把 `encrypt` 传入密钥扩展，使解密时改用逆 S-Box，偏离 FIPS-197「密钥扩展恒用正向 S-Box」的规则，需以仿真比对黄金模型 `ve_AES_Core.sv` 为准。

## 7. 下一步学习建议

- 想搞清 `bSbox` 内部如何用复合域 GF((2⁴)²) 一次算出 S-Box/逆 S-Box，请继续 [u2-l5 S-Box 的复合域高效实现](u2-l5-aes-sbox-composite-field.md)，它会展开 `gf_inv_8` 等子模块。
- 想看这 11 个轮密钥如何被 AddRoundKey 消费、加解密如何共用同一条反馈数据通路，回顾 [u2-l1 AES 顶层架构](u2-l1-aes-top-architecture.md) 中 `round_counter` 与 `selection` 多路选择的部分。
- 想从「验证」角度确认本讲提到的偏差，进入 [u3-l5 仿真验证与 SystemVerilog 验证环境](u3-l5-simulation-verification.md)，学习 `VE_sv` 目录里 `test_program` 如何驱动 `ve_AES_env` 与黄金 `AesCore` 模型。
- 建议同步阅读 FIPS-197 §5.2（Key Expansion）官方伪代码，把它与本文的手算过程并列对照，巩固对字递推与 Rcon 的直觉。
