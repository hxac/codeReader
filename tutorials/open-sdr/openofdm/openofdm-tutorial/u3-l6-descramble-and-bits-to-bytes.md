# 解扰与串并转换

## 1. 本讲目标

本讲是 OFDM 解码流水线的最后一公里。经过 u3-l5 的 Viterbi 卷积解码后，我们拿到的是一条**被扰码过的串行比特流**，还不能直接当字节用。本讲要解决两个问题：

1. **解扰（descramble）**：把发射机加过的扰码去掉，还原出原始数据比特。
2. **串并转换（bits_to_bytes）**：把还原后的串行比特流，每 8 个组装成 1 个字节，送给上层（MAC）。

学完本讲你应当能够：

- 说清 802.11 扰码器为什么存在、它的生成多项式 \(S(x)=x^7+x^4+1\) 如何用 7 级 LFSR 实现。
- 解释 OpenOFDM 用「前 7 个接收比特直接当状态」这种巧妙做法的数学依据。
- 读懂 `descramble.v` 的初始化 + 运行两阶段逻辑，以及 `bits_to_bytes.v` 的移位装配过程。
- 说清 `ofdm_decoder.v` 里 `skip_bit=9` 跳过的到底是什么——它对应 802.11 DATA 字段 SERVICE 头里 7 个初始化位之外的 9 个保留位。

---

## 2. 前置知识

### 2.1 为什么要扰码

数字通信信道里，如果数据出现长串连续的 `0` 或 `1`，会带来两个麻烦：

- 接收端的定时恢复（时钟同步）依赖信号电平翻转，长串不变电平会让接收机「找不准节拍」。
- OFDM 这种多子载波体制下，某些 bit 模式会让信号的峰均比（PAPR）变高，放大器容易失真。

所以发射机在卷积编码之后、调制之前，会先做一次**扰码（scrambling）**：用一个伪随机序列与数据逐比特异或，把数据「打散」成长串 0/1 概率各半的近似随机序列。到了接收端，Viterbi 解码还原出的比特还是「被扰码过的」，必须再做一次**解扰（descrambling）**，异或同一组伪随机序列，才能还原原始数据。

关键性质：异或两次同一个量等于没异或——`a ⊕ b ⊕ b = a`。所以**扰码和解扰用的是同一套逻辑**（见 [decode.rst 的 Descrambling 一节](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L197-L204)），只是发射端叫 scrambler、接收端叫 descrambler。

### 2.2 LFSR：线性反馈移位寄存器

扰码序列由一个 **LFSR（Linear Feedback Shift Register）** 产生。它是一串移位寄存器，每个时钟把内容移一位，空出来的那位用「某些位的异或」填上，这个异或叫**反馈（feedback）**。802.11 用的反馈多项式是：

\[ S(x) = x^7 + x^4 + 1 \]

意思是：7 级寄存器，反馈取第 7 级和第 4 级的异或。下面会看到代码里 `state[6] ^ state[3]` 正对应这一项（下标从 0 数，所以第 7 级是 `state[6]`、第 4 级是 `state[3]`）。

### 2.3 802.11 DATA 字段的结构

这是理解 `skip_bit=9` 的关键。一个 802.11 OFDM 数据符号流（SIGNAL 之后的 DATA 部分）在卷积解码之后、解扰之后，比特布局是：

| 比特区间 | 长度 | 含义 |
|----------|------|------|
| bit 0 – bit 6 | 7 bit | SERVICE 字段的「扰码初始化位」，发射端固定填 0 |
| bit 7 – bit 15 | 9 bit | SERVICE 字段的保留位（reserved） |
| bit 16 起 | 变长 | MPDU（MAC 帧真正要传的有效载荷） |
| 末尾 | 6 bit + pad | tail（卷积码归零尾）与填充 |

也就是说 SERVICE 字段共 16 bit，其中**前 7 bit 在发射端被强制写成 0**。这 7 个 0 经过扰码后，等于把发射机 LFSR 的初态「白送」给了接收机——这正是下文「直接用前 7 个接收比特当状态」技巧的物理依据。

> 说明：上表是 802.11 标准对 DATA 字段 SERVICE 头的通用约定，用于解释本讲代码里的 `skip_bit=9`。OpenOFDM 仓库本身没有把这 16 bit 的位图画成文档，本表帮助你看懂代码意图。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [verilog/descramble.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v) | 解扰器本体：7 级 LFSR，前 7 bit 自动初始化，之后逐比特异或还原 |
| [verilog/bits_to_bytes.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/bits_to_bytes.v) | 串并转换：移位缓冲每 8 bit 装配成 1 字节并发出 strobe |
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | 子流水线顶层：例化上面两个模块，并实现 `skip_bit` 跳过 SERVICE 头 |
| [docs/source/decode.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst) | 官方对扰码/解扰原理的数学推导（本讲核心理论的权威出处） |
| [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v) | 测试台：把 `descramble_out` 与 `byte_out` 落盘，供实践观察 |

本讲不涉及新算法，只是把 u3-l5 Viterbi 输出后的收尾两步讲透。两个模块都很短（都不到 50 行），但背后的 LFSR 约定和 SERVICE 字段跳过逻辑值得细抠。

---

## 4. 核心概念与源码讲解

### 4.1 descramble 解扰器

#### 4.1.1 概念说明

`descramble` 模块要做的事：输入一个**被扰码的比特** `in_bit`，输出**还原后的原始比特** `out_bit`。根据「扰码 = 数据 ⊕ 伪随机序列」，解扰就是再异或一次同一个序列：

\[ B_n = B^s_n \oplus X^1_n \]

其中 \(B^s_n\) 是第 \(n\) 个接收到的（扰码后）比特，\(X^1_n\) 是 LFSR 当前产生的伪随机位，\(B_n\) 是还原出的原始数据位。

这里有一个**状态同步**难题：接收机必须让自己的 LFSR 跟发射机的 LFSR 跑在同一组初态上，否则异或出来的全是错的。802.11 的做法（也是 OpenOFDM 采用的）是利用 SERVICE 字段前 7 个固定为 0 的比特。官方 [decode.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L228-L290) 给出了两种理解方式：

- **笨办法（计算法）**：先缓存前 7 个接收比特，因为发射端这 7 个原始位是 0，所以接收到的 7 个比特 \(B^s_0\ldots B^s_6\) 直接就是 LFSR 的 7 次反馈输出，据此反解出初态 \(X_0\)，再从头解扰。要缓存、要算、要走两遍，硬件不划算。
- **巧办法（直装法，OpenOFDM 采用）**：注意到前 7 个扰码位正好等于「扰完这 7 位之后」的 LFSR 状态 \(X_7\)，即 \(X_7^7=B^s_0, X_7^6=B^s_1, \ldots, X_7^1=B^s_6\)。于是**把接收到的头 7 个比特直接依次塞进状态寄存器**，塞满那一刻状态就对了，立刻可以解扰第 8 个比特 \(B^s_7\)。无需反解、无需重跑。

下面会看到，`descramble.v` 就是这个「巧办法」的极简实现。

#### 4.1.2 核心流程

模块运行分两阶段，用一个 `inited` 标志区分：

```
复位后 inited=0：
  每来一个 input_strobe（in_bit 是扰码位 B^s_k）：
      把 in_bit 写入 state[6 - bit_count]
      bit_count: 0→1→2→...→6
      当 bit_count 到 6 时：inited=1（状态装填完毕）
      （此阶段不产生 out_bit / output_strobe）

inited=1 之后：
  每来一个 input_strobe：
      feedback = state[6] ^ state[3]            // 伪随机位
      out_bit  = feedback ^ in_bit              // 还原原始位
      output_strobe = 1
      state <= {state[5:0], feedback}           // LFSR 左移一位
```

数学上，记 LFSR 状态为 \(X^1\ldots X^7\)（\(X^7\) 在 `state[6]`，\(X^1\) 在 `state[0]`），则：

\[ X^1 = X^7 \oplus X^4 \]
\[ B_n = B^s_n \oplus X^1 \]

每次处理后状态更新为：

\[ X_{n+1}^{i} \leftarrow X_n^{i-1}\quad (i=2,\ldots,7),\qquad X_{n+1}^{1} \leftarrow X_n^{7}\oplus X_n^{4} \]

这正是 \(S(x)=x^7+x^4+1\) 的标准 Fibonacci 型 LFSR。

#### 4.1.3 源码精读

先看端口与状态声明。模块极其精简，只有一个 7 位状态、一个 5 位计数器和一个初始化完成标志：

[verilog/descramble.v:L1-L19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L1-L19) —— 模块端口与 `feedback` 组合线。

其中 [descramble.v:L19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L19) 的 `wire feedback = state[6] ^ state[3];` 就是生成多项式 \(x^7+x^4+1\) 的硬件描述（第 7、4 级异或）。

初始化阶段（前 7 bit 直装）：

[verilog/descramble.v:L29-L36](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L29-L36) —— 把每个进来的扰码位写入 `state[6-bit_count]`，从 `state[6]` 写到 `state[0]`，写满 7 个就置 `inited=1`。

注意写入顺序：`bit_count=0` 时写 `state[6]`（第 1 个接收位 \(B^s_0\) 落在 \(X^7\)），`bit_count=6` 时写 `state[0]`（第 7 个接收位 \(B^s_6\) 落在 \(X^1\)）。这正好对应 [decode.rst 推导出的](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L264-L273) \(X_7^7=B^s_0,\ldots,X_7^1=B^s_6\)。

运行阶段（解扰）：

[verilog/descramble.v:L37-L41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L37-L41) —— 三件事同时做：`out_bit = feedback ^ in_bit`（还原）、`output_strobe=1`、`state` 左移一位并把 `feedback` 补进最低位。

复位与空闲：

[verilog/descramble.v:L22-L27](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L22-L27) 与 [descramble.v:L42-L44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L42-L44) —— 复位清零所有寄存器；没有 `input_strobe` 时拉低 `output_strobe`，延续全项目「数据 + strobe」握手风格。

> 关键观察：初始化阶段（前 7 bit）**不产生 `output_strobe`**。这意味着这 7 个 SERVICE 初始化位被模块「静默吃掉」，下游根本看不到它们。这一点在 4.3 节解释 `skip_bit` 时会用到。

#### 4.1.4 代码实践

**目标**：用纸笔验证 `descramble.v` 实现的确实是 802.11 多项式 \(S(x)=x^7+x^4+1\)，并跑一遍小例子确认「直装法」成立。

**操作步骤（源码阅读型，不依赖综合）**：

1. 打开 [descramble.v:L19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L19)，确认 `feedback = state[6] ^ state[3]`。把 `state[6]` 记作 \(X^7\)、`state[3]` 记作 \(X^4\)，则反馈函数 \(f=X^7\oplus X^4\)，对应多项式 \(x^7+x^4+1\)（常数 1 表示这是一个「有自反馈」的标准 LFSR 描述）。
2. 手算一个小序列：假设发射机 LFSR 初态 \(X_0 = 1010100\)（即 `state=1010100`，\(X^7\ldots X^1\)），发射端要发的原始数据前几位是 `0000...`（模拟 SERVICE 头）。
   - 第 1 位：feedback = \(X^7\oplus X^4 = 1\oplus0 = 1\)；扰码位 \(B^s_0 = 0\oplus1 = 1\)；状态左移成 `0101001`。
   - 继续算下去，你会得到 7 个扰码位 \(B^s_0\ldots B^s_6\)。
3. 现在切换到接收端视角：把这 7 个扰码位**依次**喂给 `descramble.v` 的初始化逻辑（写进 `state[6]..state[0]`），检查写完后 `state` 是否等于「发射端扰完 7 位后的状态」（即上一步左移 7 次后的状态）。两者应当完全一致——这就是「直装法」成立的可视化证明。
4. 喂第 8 个扰码位 \(B^s_7\)，手算 `out_bit = feedback ^ in_bit`，应当等于发射端原始的第 8 位数据。

**需要观察的现象**：第 3 步里「接收端装填完的状态」与「发射端扰完 7 位的状态」逐位相同。

**预期结果**：两者相同，证明 OpenOFDM 这种「直接把前 7 个接收比特当状态」的写法在数学上等价于标准解扰。若你手算出现不一致，通常是把 `state[6]` 当成了 \(X^1\)（注意本实现里高位对应高阶 \(X^7\)）。

> 本实践为纸笔推导，无需运行命令；若要机验，可参考 4.3.4 的仿真实践。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `feedback` 改成 `state[6] ^ state[1]`，对应的多项式是什么？还能解 802.11 的扰码吗？

**答案**：对应 \(x^7+x^2+1\)，反馈抽头从第 4 级挪到了第 2 级。这不是 802.11 标准多项式，解出来的全是错的——发射端用的是 \(x^7+x^4+1\)，多项式必须逐字相同才能正确解扰。

**练习 2**：`descramble.v` 里 `state` 为什么是左移（`{state[5:0], feedback}`）而不是右移？如果改成右移，初始化阶段的写入顺序要怎么改？

**答案**：左移是因为本实现约定 `state[6]` 是最高阶 \(X^7\)、`state[0]` 是最低阶 \(X^1\)，新反馈位补进 \(X^1\)（即 `state[0]`），所以 `{state[5:0], feedback}`。若改成右移并把新位补进最高位，则需把状态阶数映射反过来，初始化时也要按相反顺序写入——本质上等价，但与 [decode.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L264-L273) 的 \(X_7^7=B^s_0\) 约定不一致，容易出错。

---

### 4.2 bits_to_bytes 串并转换

#### 4.2.1 概念说明

解扰后得到的是一条**串行比特流**（每个时钟最多来 1 个有效比特）。但 MAC 层、FCS 校验、主机协议栈都是以**字节（8 bit）**为单位工作的。`bits_to_bytes` 就是这个「串→并」的收口模块：它把每 8 个串行比特攒成 1 个 8 位字节，攒满那一刻发出一个 `output_strobe`，把字节交给上层。

它的特点：

- **与符号边界无关**：它不关心当前是哪个 OFDM 符号，只机械地「数够 8 个就吐 1 个字节」。字节边界完全由「从第 1 个有效比特开始数」决定。
- **流式无反压**：延续全项目「数据 + strobe」风格，输入来一个攒一个，攒满就吐，没有握手回压。

#### 4.2.2 核心流程

模块用一根 8 位移位寄存器 `bit_buf` 和一个 3 位地址计数器 `addr` 实现：

```
复位：addr=0, bit_buf=0
每来一个 input_strobe（bit_in 是 1 个比特）：
    bit_buf 整体右移 1 位（bit_buf[6:0] <= bit_buf[7:1]）
    新 bit_in 放进 bit_buf[7]
    addr++
    如果 addr 已经是 7（说明这是第 8 个比特）：
        byte_out = {bit_in, bit_buf[7:1]}   // 刚到的 + 已攒的 7 位
        output_strobe = 1
        addr 回到 0
    否则：
        output_strobe = 0
```

设一个字节内依次到达的 8 个比特为 \(b_0, b_1, \ldots, b_7\)（\(b_0\) 先到），则攒满时：

\[ \text{byte\_out} = \{b_7,\ b_6,\ b_5,\ b_4,\ b_3,\ b_2,\ b_1,\ b_0\} \]

即**最先到达的 \(b_0\) 落在最低位（LSB），最后到达的 \(b_7\) 落在最高位（MSB）**。这与 802.11「字节内先传低位」的 bit 顺序一致——整条解码链的位序约定就是为了在这个收口处自然拼出正确字节。

#### 4.2.3 源码精读

端口与缓冲声明：

[verilog/bits_to_bytes.v:L1-L15](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/bits_to_bytes.v#L1-L15) —— 8 位 `bit_buf` 移位缓冲 + 3 位 `addr` 计数器。

核心装配逻辑：

[verilog/bits_to_bytes.v:L23-L32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/bits_to_bytes.v#L23-L32) —— 每拍右移、新位入顶；`addr==7`（用旧值判断，即第 8 个比特到达当拍）时拼出 `byte_out` 并拉高 `output_strobe`。

注意两个细节：

1. **判断用的是旧 `addr`**：`if (addr == 7)` 在 `addr <= addr + 1` 之前求值（非阻塞赋值），所以当 `addr` 当前是 7 时，这一拍正是第 8 个比特，立刻装配并输出，下一拍 `addr` 回到 0。
2. **装配表达式 `{bit_in, bit_buf[7:1]}`**：此时 `bit_buf[7:1]` 还是**更新前**的值（非阻塞，右移尚未生效），它装的是前 7 个比特 \(b_0\ldots b_6\)（\(b_0\) 已被逐步右移到 `bit_buf[1]`，\(b_6\) 在 `bit_buf[7]`），再加上当前 `bit_in`(\(b_7\)) 放最高位，正好拼成 \(\{b_7,\ldots,b_0\}\)。

复位与空闲：

[verilog/bits_to_bytes.v:L18-L35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/bits_to_bytes.v#L18-L35) —— 复位清零；无 `input_strobe` 时 `output_strobe=0`。

#### 4.2.4 代码实践

**目标**：跟踪一个完整字节的装配过程，确认位序。

**操作步骤（源码阅读型）**：

1. 打开 [bits_to_bytes.v:L24-L28](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/bits_to_bytes.v#L24-L28)。
2. 假设依次输入 8 个比特 `1,1,0,1,0,0,1,0`（\(b_0\ldots b_7\)），用一张表逐拍记录 `bit_buf` 与 `addr` 的变化（初值 `bit_buf=00000000, addr=0`）。
3. 第 8 拍（`addr==7`）时，手算 `byte_out = {bit_in, bit_buf[7:1]}`。

**需要观察的现象**：第 8 拍 `byte_out` 的 8 位值；最先输入的 \(b_0=1\) 是否落在 bit0（LSB）。

**预期结果**：`byte_out = {b7,b6,b5,b4,b3,b2,b1,b0} = {0,1,0,0,1,0,1,1} = 8'b01001011 = 0x4B`。\(b_0=1\) 落在 LSB，验证位序正确。

**待本地验证**：可在 `verilog/` 目录写一个最小测试台，按拍 `input_strobe` 这 8 个比特，`$display` 观察 `byte_out`，应得到 `0x4B`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `addr` 只需要 3 位宽？

**答案**：`addr` 数到 7 就回零（每 8 个比特一个周期），范围是 0–7，3 位（\(2^3=8\)）刚好够。自然回绕也满足，无需额外清零逻辑（`addr <= addr + 1` 在 3 位下从 7 加 1 自动回 0）。

**练习 2**：如果某次传输的有效字节数不是 8 的整数倍（末尾有 pad 比特），`bits_to_bytes` 会怎样？

**答案**：模块本身不感知「包结束」，只会继续把 pad 比特也攒成字节输出。是否丢弃尾部多余字节由上层（`dot11.v` 用 `byte_count >= pkt_len` 判断）负责，`bits_to_bytes` 只管机械装配。这正是 [u4-l5](u4-l5-fcs-and-crc32.md) 里 FCS/长度校验要做的收尾。

---

### 4.3 service 比特跳过逻辑（ofdm_decoder 中的衔接）

#### 4.3.1 概念说明

`descramble` 和 `bits_to_bytes` 各自都很简单，但它们之间有个**关键的衔接问题**：解扰后的比特流里，开头并不是 MPDU 数据，而是 16 bit 的 SERVICE 头。

回顾 2.3 节的 DATA 字段结构：

- 前 7 bit：SERVICE 的扰码初始化位。这 7 bit 在 `descramble` 初始化阶段被「静默吃掉」（不产生 `output_strobe`），所以**根本不会传到下游**。
- 接下来 9 bit（bit 7 – bit 15）：SERVICE 的保留位。这部分会被 `descramble` 正常解扰并输出（`output_strobe=1`），但它们**不是 MPDU**，不能进入字节装配。

所以 `ofdm_decoder` 必须在这 9 个保留位到达 `bits_to_bytes` 之前把它们拦掉，否则 MPDU 的第一个字节就会错位 9 个比特。这个拦截就是 `skip_bit=9` 的职责。

> 一句话：`skip_bit=9` 跳过的是 **SERVICE 字段 16 bit 里、刨去 descramble 已吞掉的 7 个初始化位之后、剩下的 9 个保留位**。\(7 + 9 = 16\)，正好一个完整 SERVICE 字段；之后才是真正的 MPDU 字节。

注意这只对 **DATA 符号**（`do_descramble=1`）生效。SIGNAL / HT-SIG 字段不走扰码（`do_descramble=0`），Viterbi 输出直接送 `bits_to_bytes`，没有 SERVICE 头、也不需要跳过。

#### 4.3.2 核心流程

`ofdm_decoder.v` 里用一段时序逻辑选路：

```
每拍：
  if (~do_descramble):                      // SIGNAL / HT-SIG
      bit_in     = conv_decoder_out         // Viterbi 直出
      bit_in_stb = conv_decoder_out_stb     // 不跳过
  else:                                     // DATA
      bit_in = descramble_out               // 解扰后
      if (descramble_out_strobe):
          if (skip_bit > 0):                // 还在 SERVICE 保留位区间
              skip_bit--
              bit_in_stb = 0                // 丢弃，不送给 bits_to_bytes
          else:
              bit_in_stb = 1                // MPDU 比特，放行
      else:
          bit_in_stb = 0
```

`skip_bit` 在复位时初始化为 9（[ofdm_decoder.v:L127](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L127)），每放行一个 DATA 扰码位就递减，减到 0 之后才开始给 `bits_to_bytes` 发 strobe。这样 `bits_to_bytes` 收到的第 1 个有效比特就是 MPDU 的第 1 位，字节边界天然对齐。

#### 4.3.3 源码精读

先看两个子模块如何被例化、信号如何流动：

[verilog/ofdm_decoder.v:L93-L116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L93-L116) —— `descramble` 把 Viterbi 输出 `conv_decoder_out` 解扰成 `descramble_out`；`bits_to_bytes` 接收的却不是 `descramble_out` 本身，而是经跳过逻辑处理后的 `bit_in / bit_in_stb`。这正说明「跳过」发生在这两个模块之间。

`skip_bit` 的声明与初值：

[verilog/ofdm_decoder.v:L47](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L47) 与 [ofdm_decoder.v:L126-L128](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L126-L128) —— `reg [3:0] skip_bit;` 复位时 `skip_bit <= 9;`，注释明写「skip the first 9bits of descramble out (service bits)」。

跳过与选路主体：

[verilog/ofdm_decoder.v:L155-L172](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L155-L172) —— 这是本节核心。`~do_descramble` 分支把 Viterbi 输出直送；`do_descramble` 分支用 `skip_bit` 把前 9 个解扰位拦掉。

还有一个保护条件：

[ofdm_decoder.v:L155](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L155) 的 `if (deinter_out_count > 0)` —— 只有当解交织真正产出过比特（`deinter_out_count` 由 [ofdm_decoder.v:L133-L134](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L133-L134) 累计）之后，才允许往 `bits_to_bytes` 送数，避免在流水线预热阶段装配出垃圾字节。

#### 4.3.4 代码实践（仿真可验）

**目标**：用仓库自带的样本和测试台，直接观察 `skip_bit` 的效果——确认 `byte_out.txt` 的第一字节对应 MPDU 第一字节，而非 SERVICE。

**操作步骤**：

1. 进入 `verilog/` 目录，按 u1-l2 跑通仿真：
   ```bash
   cd verilog
   make simulate
   ```
   默认样本是 `testing_inputs/conducted/dot11a_24mbps_*.txt`（见 [dot11_tb.v:L83](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L83)）。
2. 测试台已经把关键信号落盘（[dot11_tb.v:L129](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L129) 与 [dot11_tb.v:L133](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L133)）：
   - `sim_out/descramble_out.txt`：`S_DECODE_DATA` 期间每个 `descramble_out_strobe` 写一行（[dot11_tb.v:L220-L223](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L220-L223)）。
   - `sim_out/byte_out.txt`：`S_DECODE_DATA` 期间每个 `byte_out_strobe` 写一个 2 位十六进制（[dot11_tb.v:L225-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L225-L228)）。
3. 数 `descramble_out.txt` 的总行数记为 \(N_d\)，数 `byte_out.txt` 的总行数记为 \(N_b\)。

**需要观察的现象**：

- \(N_d\) 应当比 \(8 \times N_b\) 多出**恰好 9**（被 `skip_bit` 拦掉的 9 个 SERVICE 保留位）。即 \(N_d \approx 8 N_b + 9\)。
- `byte_out.txt` 的第一行就是 MPDU 的第一个字节（对 802.11 数据帧通常是 MAC 帧头的第一字节，如 QoS Data 的 Frame Control 第一字节）。

**预期结果**：\(N_d - 8 N_b = 9\)，直观证明 `skip_bit=9` 跳过的正是 SERVICE 的 9 个保留位。

**待本地验证**：上述关系依赖样本是否为完整 DATA 帧、以及 Viterbi flush 是否引入尾比特；若差值不是 9，请结合 `signal_out.txt` 里的 `legacy_len`（见 [dot11_tb.v:L199-L203](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L199-L203)）核算包长度，并阅读 u4-l2 关于 `(legacy_len+3)<<4` 的长度计算。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `skip_bit` 初值从 9 改成 0，`byte_out` 会怎样错位？

**答案**：SERVICE 的 9 个保留位会被当成 MPDU 的前 9 个比特进入字节装配，导致**所有后续字节整体错位 9 个比特**（约 1 个字节多），FCS 几乎必然失败。这正好说明 `skip_bit` 是对齐字节边界的命脉。

**练习 2**：为什么 `skip_bit` 拦截只在 `do_descramble=1` 时生效？SIGNAL 字段不需要跳过吗？

**答案**：SIGNAL / HT-SIG 字段在发射端**没有扰码**（它们要被接收机直接读出 rate/length），也就没有 SERVICE 头。`ofdm_decoder` 对这些字段走 `~do_descramble` 分支，Viterbi 输出直接送 `bits_to_bytes`，前 3 个字节（24 bit）就是 SIGNAL 内容，无需任何跳过。`do_descramble` 同时控制「是否解扰」和「是否跳过 SERVICE」，二者绑定出现。

**练习 3**：`skip_bit` 用 4 位寄存器存放 9，有没有浪费？

**答案**：9 需要至少 4 位（\(2^3=8\) 不够），所以 4 位是下限，没有浪费。递减到 0 后保持 0（`skip_bit > 0` 不再成立），逻辑天然终止。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，绘制一张「Viterbi 输出 → descramble → skip 9 → bits_to_bytes → byte_out」的完整数据通路图，并用默认样本验证字节边界。

**步骤**：

1. 画通路图，标注每一段的位宽与 strobe 信号：
   - `conv_decoder_out`（1 bit + `conv_decoder_out_stb`）→ `descramble`
   - `descramble_out`（1 bit + `descramble_out_strobe`）→ `ofdm_decoder` 跳过逻辑（`skip_bit` 9→0）
   - 放行后的 `bit_in`（1 bit + `bit_in_stb`）→ `bits_to_bytes`
   - `byte_out`（8 bit + `byte_out_strobe`）→ 上层
2. 在图上用红笔标出「前 7 bit 静默初始化」（descramble 内部，无 strobe）和「接下来 9 bit 被 skip_bit 拦截」两段，写明它们合起来正好是 16 bit SERVICE 头。
3. 跑一次 `make simulate`，打开 `sim_out/byte_out.txt`，对照样本文件名里的速率（24 Mbps）和 MAC 地址，确认第一个字节是合理的 MAC 帧头字节（Frame Control）。
4. 用 4.3.4 的方法核对 \(N_d - 8 N_b = 9\)。

**验收标准**：通路图能解释「为什么 byte_out 的第一字节就是 MPDU 第一字节」，且仿真数据与该解释一致。完成后，你就把 OFDM 解码流水线从射频样本一路追到了字节输出——这之后只剩 u4 单元的控制平面（状态机、SIGNAL/HT-SIG 解析、FCS）把整包「管起来」。

---

## 6. 本讲小结

- **扰码和解扰用同一套逻辑**：都是「数据 ⊕ LFSR 输出」，因为异或两次同一序列等于没异或。802.11 多项式是 \(S(x)=x^7+x^4+1\)，对应 [descramble.v:L19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L19) 的 `state[6] ^ state[3]`。
- **OpenOFDM 用「直装法」同步 LFSR**：把接收到的头 7 个扰码位直接当状态装填（[descramble.v:L29-L36](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L29-L36)），依据是 SERVICE 头前 7 bit 在发射端固定为 0，使得这 7 个扰码位恰好等于扰完后的状态 \(X_7\)。
- **初始化阶段静默**：前 7 bit 不产生 `output_strobe`，被 descramble 内部吃掉，下游看不到。
- **bits_to_bytes 是纯机械装配**：8 位移位缓冲 + 3 位计数器，每 8 个比特拼一个字节，最先到的位落在 LSB（[bits_to_bytes.v:L23-L32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/bits_to_bytes.v#L23-L32)）。
- **`skip_bit=9` 对齐 MPDU 字节边界**：它跳过 SERVICE 字段 16 bit 中除 7 个初始化位之外剩下的 9 个保留位（[ofdm_decoder.v:L126-L128](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L126-L128)），只对 DATA（`do_descramble=1`）生效。
- **至此数据平面走完**：从 `sample_in` 到 `byte_out`，整条 OFDM 解码流水线的「比特还原」部分全部闭环。

---

## 7. 下一步学习建议

- **进入控制平面（u4 单元）**：本讲只讲了「数据怎么变成字节」，但谁来告诉 `ofdm_decoder` 现在是 SIGNAL 还是 DATA？谁来算 `num_bits_to_decode`、谁来拉 `do_descramble`？答案在 [u4-l1 dot11 顶层状态机](u4-l1-dot11-statemachine.md)。建议接着读 `dot11.v` 的 `S_DECODE_SIGNAL` / `S_DECODE_DATA` 状态，看它们如何驱动本讲的 `do_descramble` 与 `num_bits_to_decode`。
- **补全长度与校验闭环**：`byte_out` 出来后还要过 FCS（CRC-32）才能确认整包正确，见 [u4-l5 FCS 校验与 CRC32](u4-l5-fcs-and-crc32.md)。那里会解释 `byte_count >= pkt_len` 如何收尾，以及本讲提到的「尾部 pad 字节」如何被丢弃。
- **源码延伸阅读**：如果想看扰码器的「正面」（发射端），仓库没有发射机，但 [docs/source/decode.rst 的 Descrambling 一节](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L197-L290) 给出了完整的数学推导，是理解本讲「直装法」的最佳补充材料。另外 [rand_gen.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen.v) 是同款 LFSR 思路的另一个实例，可在 u6-l6 对照阅读。
