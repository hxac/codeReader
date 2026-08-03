# lfsr：通用并行 LFSR/CRC 引擎

## 1. 本讲目标

本讲深入讲解 `rtl/lfsr.v`——verilog-ethernet 全库**最底层的校验类构建块**。学完后你应当能够：

- 说清 `lfsr` 模块的 7 个参数（`LFSR_WIDTH`、`LFSR_POLY`、`LFSR_CONFIG`、`LFSR_FEED_FORWARD`、`REVERSE`、`DATA_WIDTH`、`STYLE`）各自的含义。
- 理解「并行展开」：为什么一个 `DATA_WIDTH=8` 的实例能在一个时钟周期内处理一整个字节，而不必移位 8 次。
- 区分 **Galois** 与 **Fibonacci** 两种 LFSR 拓扑，知道以太网 FCS（CRC-32）用哪种、64b66b 扰码器用哪种。
- 能够亲手实例化一个以太网 CRC-32 计算器，并把结果和标准值对比。

本讲是第二单元（LFSR/CRC 与 FCS）的基础，下一讲 `u2-l2` 的 `axis_eth_fcs` 系列正是建立在本模块之上。

## 2. 前置知识

阅读本讲前，你需要大致了解：

- **Verilog 基础**：`module`、`parameter`、`wire`/`reg`、`assign`、`generate`、`function`，以及组合逻辑（无时钟）的概念。
- **AXI-Stream 接口**（见 `u1-l3`）：本讲不直接用 AXI-Stream，但 `lfsr` 的产物（FCS、扰码）最终都会挂到 AXI-Stream 数据通路上。
- **异或（XOR）运算**：LFSR/CRC 全程只做「按位异或」这一种运算，它是 GF(2)（只有 0、1 两个元素的域）上的加法。
- **多项式与位掩码**：一个生成多项式 \( P(x) \) 用一串二进制位表示，某一位为 1 就代表该次项存在，例如 \( x^5+x^2+1 \) 写成 `5'b100101`。

一个最关键的直觉：**LFSR（线性反馈移位寄存器）是一种「下一状态 = 当前状态若干位的异或」的状态机**。因为「异或」是线性的，所以无论移位多少次，下一状态都能写成「当前状态与输入数据的某种异或组合」——这正是本模块「并行展开」成立的原因。请带着这个直觉往下读。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rtl/lfsr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v) | 本讲主角。全参数化、纯组合逻辑的 LFSR/CRC 引擎。 |
| [rtl/axis_eth_fcs.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v) | 以太网 FCS（CRC-32）发生器，是 `lfsr` 最典型的真实用例（下一讲详解）。 |
| [rtl/eth_phy_10g_tx_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v) | 10G PHY 发送侧，用 `lfsr` 实现 64b66b 扰码器与 PRBS-31 伪随机序列发生器。 |
| [rtl/arp_cache.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v) | ARP 缓存，用 `lfsr` 做哈希函数（Fibonacci 配置）。 |

本讲只精读 `rtl/lfsr.v`，其余三个文件作为「它被怎么用」的佐证。

## 4. 核心概念与源码讲解

### 4.1 LFSR 参数化模型

#### 4.1.1 概念说明

LFSR（Linear Feedback Shift Register，线性反馈移位寄存器）是一类特殊的移位寄存器：它的串入位不是外部给定的，而是「寄存器当前若干位的异或」。给定一个**生成多项式**指明「取哪些位做异或」，LFSR 就能循环产生一个很长的位序列。

它有两类典型用途：

- **伪随机序列（PRBS）/ 扰码**：用 LFSR 产生看似随机的比特流，10G 以太网的 64b66b 编码就用它做扰码，让线路上的 0/1 分布均匀。
- **循环冗余校验（CRC）**：把数据「移入」一个 LFSR，最终寄存器里剩下的值就是校验码。以太网帧尾的 4 字节 FCS 就是 CRC-32。

`lfsr` 模块把上面所有变体统一抽象成**一个纯组合逻辑的「下一状态」函数**：给它当前状态 `state_in` 和一段输入数据 `data_in`，它直接算出移位若干位后的新状态 `state_out` 和被移出的数据 `data_out`。它本身不存任何状态（没有寄存器），状态由调用方在外面保存并回送——这是理解它的第一要义。

#### 4.1.2 核心流程

模块对外只做一件事，可以用一行伪代码描述：

```
(state_out, data_out) = LFSR_NEXT(state_in, data_in)   // 纯组合逻辑
```

调用方负责把 `state_out` 存进寄存器、下一拍再当作 `state_in` 喂回来，从而实现「逐字节累加 CRC」或「逐拍产生伪随机流」。

参数与端口的对应关系：

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `LFSR_WIDTH` | 状态寄存器宽度 | 31 |
| `LFSR_POLY` | 生成多项式（不含最高次项） | `31'h10000001` |
| `LFSR_CONFIG` | 拓扑：`"GALOIS"` 或 `"FIBONACCI"` | `"FIBONACCI"` |
| `LFSR_FEED_FORWARD` | 前馈（用于解扰/PRBS 校验） | 0 |
| `REVERSE` | 输入输出按位反转 | 0 |
| `DATA_WIDTH` | 每次移入的数据位数 | 8 |
| `STYLE` | 实现风格 `AUTO`/`LOOP`/`REDUCTION` | `"AUTO"` |

端口只有 4 个：`data_in`、`state_in`（输入）与 `data_out`、`state_out`（输出）。

#### 4.1.3 源码精读

模块声明与全部参数、端口集中在开头，请先记住这些名字，后面会反复用到：

[rtl/lfsr.v:34-56](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L34-L56) —— 模块名 `lfsr`，7 个 `parameter`，4 个端口（`data_in`/`state_in` 进，`data_out`/`state_out` 出）。

模块开头有一大段注释，其中有一张**常用 LFSR/CRC 设置速查表**，几乎是本库的「作弊条」，配置任何校验模块前都应先查它：

[rtl/lfsr.v:183-200](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L183-L200) —— 列出 CRC16/CRC32/PRBS6…31/64b66b 等的配置、长度、多项式、初值。例如 `CRC32` 行写着 `Galois, bit-reverse, 32'h04c11db7, 32'hffffffff, 以太网 FCS；末尾取反`。

关于多项式书写有个易错点：表中多项式**不含最高次项**。比如 CRC-32 的完整多项式是

\[ x^{32}+x^{26}+x^{23}+x^{22}+x^{16}+x^{12}+x^{11}+x^{10}+x^{8}+x^{7}+x^{5}+x^{4}+x^{2}+x+1 \]

但参数里写的是 `32'h04c11db7`——最高位 \( x^{32} \) 被省略，由 `LFSR_WIDTH=32` 自动补回。注释里明确说明了这一点：

[rtl/lfsr.v:92-102](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L92-L102) —— 「最大项 \( x^{32} \) 被抑制，由 `LFSR_WIDTH` 自动生成」。

一个真实用例佐证：以太网 FCS 发生器 `axis_eth_fcs` 正是按这张表配置 `lfsr` 的：

[rtl/axis_eth_fcs.v:89-103](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L89-L103) —— `LFSR_WIDTH(32)`、`LFSR_POLY(32'h4c11db7)`、`LFSR_CONFIG("GALOIS")`、`REVERSE(1)`，与速查表完全一致。

#### 4.1.4 代码实践

**目标**：查阅速查表，自己配置一个 CRC-32 实例的参数，并与库内 `axis_eth_fcs` 的真实配置核对。

**步骤**：

1. 打开 [rtl/lfsr.v:183-200](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L183-L200) 的速查表，读出 `CRC32` 一行的配置、多项式与初值。
2. 打开 [rtl/axis_eth_fcs.v:89-103](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L89-L103)，逐项比对 `LFSR_WIDTH`/`LFSR_POLY`/`LFSR_CONFIG`/`REVERSE` 是否与表一致。
3. 再打开 10G PHY 的扰码器 [rtl/eth_phy_10g_tx_if.v:135-149](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L135-L149)，读出 64b66b 扰码器的配置（应为 `FIBONACCI`、`58'h8000000001`、`REVERSE(1)`），同样对照速查表的 `64b66b` 行。

**观察**：你会发现库里所有校验/扰码模块的参数都能在那张速查表里找到对应行——这张表是配置 `lfsr` 的权威依据。

**预期结果**：`axis_eth_fcs` 与表中 `CRC32` 行一致；`eth_phy_10g_tx_if` 的扰码器与表中 `64b66b` 行一致。

#### 4.1.5 小练习与答案

**练习 1**：要把 `lfsr` 用作以太网 FCS（CRC-32），`LFSR_CONFIG` 和 `REVERSE` 应分别取什么值？

> **答案**：`LFSR_CONFIG = "GALOIS"`，`REVERSE = 1`（bit-reverse）。见速查表 `CRC32` 行与 `axis_eth_fcs.v:89-103` 的实例。

**练习 2**：`LFSR_POLY` 参数里要不要写出最高次项（如 CRC-32 的 \( x^{32} \)）？

> **答案**：不要。模块会根据 `LFSR_WIDTH` 自动补出最高次项，因此 CRC-32 写 `32'h4c11db7` 即可（见 `lfsr.v:92-102` 的说明）。

### 4.2 并行展开下一状态

#### 4.2.1 概念说明

最朴素的 LFSR 一次只移 1 位。若要在 8 位数据通路上做 CRC，每来一个字节就得移位 8 次——在 125 MHz 的千兆 MAC 里，这意味着 8 倍的时钟频率，极不现实。

`lfsr` 模块的核心巧思叫**并行展开（unrolling）**：既然单步「下一状态」是当前状态的**线性**函数（只有异或），那么「连续移位 `DATA_WIDTH` 次」也必然是当前状态的某个线性函数。于是我们可以**一次性算出移位 8 位后的新状态**，一个时钟周期处理一个字节，无需提高时钟。

具体说，把单步移位看作 GF(2) 上的矩阵乘法：

\[ s_{\text{next}} = A \cdot s \oplus b \cdot d \]

其中 \( s \) 是状态向量，\( d \) 是输入位，\( A \) 是一个二进制矩阵，\( b \) 是二进制向量，乘法和加法都在 GF(2)（即按位与、按位异或）下进行。连续移位 \( W_d \)（=`DATA_WIDTH`）次后：

\[
\begin{bmatrix} s_{\text{out}} \\ d_{\text{out}} \end{bmatrix}
= M \cdot
\begin{bmatrix} s_{\text{in}} \\ d_{\text{in}} \end{bmatrix}
\pmod{2}
\]

\( M \) 是一个 \((\text{LFSR\_WIDTH}+\text{DATA\_WIDTH})\) 阶的二元矩阵。**`lfsr` 模块的全部工作，就是预先算好这个矩阵 \( M \)，再用它一次性算出输出。**

#### 4.2.2 核心流程

模块分两步完成「并行展开」：

1. **静态求掩码**（elaboration 时执行一次）：用一个 `function` 模拟移位寄存器连移 `DATA_WIDTH` 次，记录「每个输出位由哪些输入位异或得到」，结果存成一张位掩码表。
2. **运行时求值**（每个输出位一条 `assign`）：取出对应掩码，把 `(data_in, state_in)` 与掩码按位与，再整体异或，得到该输出位。

伪代码：

```
function lfsr_mask(index):           // 返回第 index 个输出位的掩码
    把每个状态位、数据位初始化为「只影响自己」的单位掩码
    for 每个输入数据位 (共 DATA_WIDTH 次):
        模拟一次 LFSR 移位，更新所有掩码之间的依赖
    返回 第 index 个输出位 对应的掩码向量

// 运行时（纯组合逻辑）：
state_out[n] = XOR_REDUCE( {data_in, state_in} & lfsr_mask(n) )
data_out[n]  = XOR_REDUCE( {data_in, state_in} & lfsr_mask(n + LFSR_WIDTH) )
```

注意第 2 步里的 `XOR_REDUCE( X & mask )`：掩码某位为 1 就把对应输入位「选中」，再把选中的位全部异或——这正是矩阵乘法在 GF(2) 上的逐位实现。

#### 4.2.3 源码精读

**第一步：求掩码的 `function`。** 它在 elaboration 期被调用，先给每个状态位/数据位赋一个「单位掩码」（只影响自己），为线性展开做准备：

[rtl/lfsr.v:219-230](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L219-L230) —— 初始化位掩码：`lfsr_mask_state[i][i] = 1`，相当于给矩阵放上单位阵的列向量。

随后循环 `DATA_WIDTH` 次，每轮模拟一次移位并更新掩码之间的依赖。注意它循环的次数由 `data_mask` 控制，从最高位逐位右移直到为 0，恰好遍历 `DATA_WIDTH` 个输入位：

[rtl/lfsr.v:235-268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L235-L268) —— Fibonacci 分支：每轮先算出本轮的反馈值，再执行一次移位，共循环 `DATA_WIDTH` 轮，等价于一次移位 `DATA_WIDTH` 位。

函数末尾按请求的 `index` 返回对应输出位的掩码，并用 `{data_val, state_val}` 把「数据部分」和「状态部分」拼成一个宽掩码：

[rtl/lfsr.v:334-342](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L334-L342) —— 返回值是一个 `LFSR_WIDTH+DATA_WIDTH` 位的掩码，高半段对应 `data_in`，低半段对应 `state_in`。

**第二步：运行时求值。** 这里有一个巧妙的「双实现」机制。模块根据 `STYLE` 选择两种**功能等价**但仿真/综合特性不同的写法。先看 `REDUCTION` 风格——直接用 Verilog 的归约异或运算符 `^`：

[rtl/lfsr.v:362-377](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L362-L377) —— `assign state_out[n] = ^({data_in, state_in} & mask);`，一行就完成了「选位 + 异或」。这在 iverilog 里仿真很快。

再看 `LOOP` 风格——用双重 `for` 循环逐位异或：

[rtl/lfsr.v:396-408](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L396-L408) —— 在 `always @*` 里用循环逐位异或，仿真较慢但综合结果更稳定（对 ISE 友好）。

`AUTO` 模式会在仿真时自动选 `REDUCTION`、综合时自动选 `LOOP`，靠 `synthesis translate_off/on` 注释区分：

[rtl/lfsr.v:346-356](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L346-L356) —— 仿真时定义 `SIMULATION` 宏，`AUTO` 解析为 `REDUCTION`；综合器忽略该宏，`AUTO` 解析为 `LOOP`。

#### 4.2.4 代码实践

**目标**：亲手实例化一个 `DATA_WIDTH=8` 的 CRC-32 引擎，喂入固定字节序列，仿真得到 CRC 值并与标准值对比。

这是一个**源码阅读 + 最小实例化**型实践。下面是示例 testbench（非项目原有代码，已标注）：

```verilog
// ===== 示例代码：tb_lfsr_crc32.v（本讲新增，非仓库原有文件）=====
`timescale 1ns / 1ps
`default_nettype none

module tb_lfsr_crc32;
    reg  [7:0]  data_in;
    reg  [31:0] state_in;
    wire [7:0]  data_out;
    wire [31:0] state_out;

    // 以太网 CRC-32：Galois, bit-reverse, poly 0x04c11db7
    lfsr #(
        .LFSR_WIDTH(32),
        .LFSR_POLY(32'h4c11db7),
        .LFSR_CONFIG("GALOIS"),
        .LFSR_FEED_FORWARD(0),
        .REVERSE(1),
        .DATA_WIDTH(8),
        .STYLE("AUTO")
    ) crc_inst (
        .data_in(data_in),
        .state_in(state_in),
        .data_out(data_out),
        .state_out(state_out)
    );

    // 经典校验串 "123456789"（ASCII 0x31..0x39）
    reg [7:0] message [0:8];
    integer i;

    initial begin
        message[0]=8'h31; message[1]=8'h32; message[2]=8'h33;
        message[3]=8'h34; message[4]=8'h35; message[5]=8'h36;
        message[6]=8'h37; message[7]=8'h38; message[8]=8'h39;

        state_in = 32'hFFFFFFFF;          // CRC-32 标准初值
        for (i = 0; i < 9; i = i + 1) begin
            data_in  = message[i];
            #10;                          // 等组合逻辑稳定
            state_in = state_out;         // 链式回送：逐字节累加 CRC
        end

        $display("raw  state_out = %08x", state_out);
        $display("CRC-32 final  = %08x", ~state_out);  // 末尾取反
        $display("expected      = cbf43926");
        $finish;
    end
endmodule
```

**操作步骤**（假定已按 `u1-l4` 配好 iverilog）：

1. 把上面的 testbench 存为 `tb_lfsr_crc32.v`（放在仓库**之外**的临时目录，勿写入 `rtl/`）。
2. 编译并运行：
   ```bash
   iverilog -g2012 -o sim tb_lfsr_crc32.v rtl/lfsr.v
   vvp sim
   ```

**需要观察的现象**：

- `lfsr` 是纯组合逻辑，没有时钟；`#10` 延时只是让输出稳定后再采样。
- 每个字节处理后，`state_in = state_out` 把新状态回送——这就是 `axis_eth_fcs.v` 里 `crc_state <= crc_next` 的手动版（见 [rtl/axis_eth_fcs.v:114-115](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L114-L115)）。
- 末尾的 `~state_out` 对应 CRC-32 标准的「输出取反」。

**预期结果**：`CRC-32 final` 应打印 `cbf43926`，与 `"123456789"` 的标准 CRC-32（IEEE 802.3 / zlib）一致。该值是国际通用的 CRC-32 标准校验向量。

**若结果不符**：请检查初值是否为 `0xFFFFFFFF`、是否漏掉末尾取反、`LFSR_CONFIG` 是否误设成 `FIBONACCI`。逐拍中间值的具体数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DATA_WIDTH=8` 的实例能在一个周期内处理一整个字节？

> **答案**：因为它把「连续移位 8 次」展开成了一个组合逻辑函数——由于每一步都是 GF(2) 上的线性映射，8 步的复合也是线性映射，可以一次性算出。见 `lfsr.v:235-268` 的循环展开。

**练习 2**：`state_out` 的每一位是 `state_in`/`data_in` 的什么函数？由什么决定？

> **答案**：是 GF(2) 上的线性函数，即「输入位某个子集的异或」。这个子集由 `lfsr_mask(n)` 返回的掩码决定（见 `lfsr.v:371-372`）。

**练习 3**：`REDUCTION` 与 `LOOP` 两种风格功能上是否等价？为什么需要两套？

> **答案**：功能完全等价。`REDUCTION` 在 iverilog 仿真更快、在 Quartus 综合略好，但在 ISE 综合较差；`LOOP` 在两类综合器都稳定但仿真慢。`AUTO` 会在仿真时选 `REDUCTION`、综合时选 `LOOP`（见 `lfsr.v:346-356`）。

### 4.3 Galois 与 Fibonacci 配置

#### 4.3.1 概念说明

LFSR 有两种等价的内部接线方式：

- **Fibonacci（多对外反馈）**：从寄存器的若干个抽头异或得到**一个**反馈位，再串行移入寄存器顶端。常用于 PRBS 伪随机序列、扰码器/解扰器。
- **Galois（多对内反馈）**：反馈位在移位过程中被异或到寄存器内部的**多个**抽头位置。常用于 CRC 生成与校验。

两者在数学上能产生相同的序列（多项式相同时），但电路形态不同。模块注释里画了 ASCII 示意图，Fibonacci 是「抽头异或 → 单点串入」，Galois 是「单点反馈 → 多点异或注入」。

为什么 CRC 一般用 Galois？因为 Galois 拓扑下，每个抽头是一个独立的异或门，非常适合「多位并行展开」——这正是 `axis_eth_fcs` 选 `GALOIS` 的原因。而扰码器/PRBS 传统上用 Fibonacci。

此外还有两个相关开关：

- `LFSR_FEED_FORWARD`：把反馈改成前馈，用于**自同步解扰**和 PRBS 校验（接收侧）。
- `REVERSE`：按位反转输入与输出。CRC-32 标准按字节低位在前（reflected），`REVERSE=1` 让模块自动处理这种位序，调用方仍按自然字节序喂数据。

#### 4.3.2 核心流程

两种拓扑在「求掩码」函数里的区别，仅在于**反馈（异或注入）相对移位的先后**：

```
Fibonacci 单步：
    feedback = XOR( 抽头位 ) ^ data_in     // 先算反馈
    寄存器整体移位，顶位移入 feedback        // 再移位

Galois 单步：
    feedback = 最高位 ^ data_in             // 先取反馈源
    寄存器整体移位                          // 先移位
    在每个抽头位置，把 feedback 异或进去      // 再注入反馈
```

由于求掩码函数循环 `DATA_WIDTH` 次、每次都执行上述单步，最终得到的并行展开矩阵会因拓扑不同而不同——但调用方完全感知不到，只看到 `state_out`/`data_out` 四个端口。

#### 4.3.3 源码精读

**Fibonacci 分支**——反馈在移位**之前**计算（先算 `state_val`，再把它用于移位）：

[rtl/lfsr.v:242-268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L242-L268) —— 先遍历多项式抽头异或出 `state_val`（L243-L248），再执行移位并把 `state_val` 移入顶端（L251-L267）。

**Galois 分支**——反馈在移位**之后**注入（先移位，再把 `state_val` 异或到各抽头）：

[rtl/lfsr.v:271-304](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L271-L304) —— 先移位（L279-L295），再遍历多项式抽头，把 `state_val` 异或进 `lfsr_mask_state[j]`（L298-L303）。

两者对照，能清楚看到「先反馈后移位」与「先移位后反馈」的差异。

**`REVERSE` 处理**——若开启，则对返回的掩码做按位反转，从而让输出按 LSB-first 语义：

[rtl/lfsr.v:311-332](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L311-L332) —— `REVERSE=1` 时把掩码的位序反转，等效于「输入输出按位翻转」。

**真实用例对照**：

- CRC-32（Galois）：[rtl/axis_eth_fcs.v:89-103](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L89-L103)，`LFSR_CONFIG("GALOIS")`、`REVERSE(1)`。
- 64b66b 扰码器（Fibonacci）：[rtl/eth_phy_10g_tx_if.v:135-149](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L135-L149)，`LFSR_CONFIG("FIBONACCI")`、`REVERSE(1)`。
- PRBS-31 发生器（Fibonacci，无数据输入）：[rtl/eth_phy_10g_tx_if.v:151-164](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L151-L164)，注意它把 `data_in` 全部置 0，只用 `state_out` 驱动状态、从 `data_out` 取伪随机序列。

#### 4.3.4 代码实践

**目标**：对比两种拓扑的掩码计算顺序，并用 PRBS-31 配置观察伪随机输出。

**步骤**：

1. 打开 [rtl/lfsr.v:242-268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L242-L268) 与 [rtl/lfsr.v:271-304](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L271-L304)，在纸上标注「反馈计算」与「移位」的先后顺序，确认 Fibonacci 是「先反馈后移位」、Galois 是「先移位后反馈」。
2. 把 4.2.4 节的示例 testbench 复制一份，改成 PRBS-31 发生器：`LFSR_WIDTH(31)`、`LFSR_POLY(31'h10000001)`、`LFSR_CONFIG("FIBONACCI")`、`REVERSE(1)`、`DATA_WIDTH(8)`，并把 `data_in` 全置 0；给一个非零初值（如 `31'h00000001`），连续回送 `state_out` 若干拍。
3. 仿真并打印每拍的 `data_out`。

**观察**：`data_out` 会呈现看似随机的 8 位序列；只要初值非零，序列就不会恒为 0。PRBS 序列的具体值「待本地验证」，但其周期性与「最大长度」性质可对照 ITU 标准查阅。

**预期结果**：能解释 Fibonacci 与 Galois 在掩码函数里的代码顺序差异；PRBS-31 实例能产生非零的伪随机字节流。

#### 4.3.5 小练习与答案

**练习 1**：10G 以太网的 64b66b 扰码器应使用 Galois 还是 Fibonacci？

> **答案**：Fibonacci。见 [rtl/eth_phy_10g_tx_if.v:135-149](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L135-L149) 的 `scrambler_inst`，`LFSR_CONFIG("FIBONACCI")`。

**练习 2**：在求掩码函数里，Fibonacci 分支「先算反馈再移位」，Galois 分支「先移位再注入反馈」——这句判断对吗？请指出对应行号。

> **答案**：对。Fibonacci 反馈在 [lfsr.v:243-248](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L243-L248)（移位在 L251 之后）；Galois 反馈注入在 [lfsr.v:298-303](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v#L298-L303)（移位在 L279 之前）。

**练习 3**：`REVERSE=1` 解决了什么问题？为什么 `axis_eth_fcs` 要打开它？

> **答案**：CRC-32（以太网 FCS）标准按字节低位在前（reflected）处理。`REVERSE=1` 让模块内部按位反转输入输出，调用方就能按自然字节序喂数据，而不必自己手动反转每一位。`axis_eth_fcs` 因此设 `REVERSE(1)`。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个**端到端的 CRC-32 校验器**：

1. **参数化模型**：按速查表配置 `lfsr`（CRC-32：Galois、`0x4c11db7`、`REVERSE=1`、`DATA_WIDTH=8`）。
2. **并行展开**：理解它能一个周期处理一个字节（掩码在 elaboration 期算好）。
3. **拓扑选择**：确认 CRC 用 Galois。
4. 在 4.2.4 的示例 testbench 基础上，扩展为**任意长度报文**：把 `message` 数组改大、用一个 `for` 循环逐字节回送 `state_out → state_in`，初值 `0xFFFFFFFF`，末尾取反。
5. **交叉验证**：用 Python 计算同一段报文的 `zlib.crc32`，与仿真打印的 `~state_out` 比较：
   ```python
   import zlib
   print(f"{zlib.crc32(b'123456789'):08x}")   # 期望 cbf43926
   ```

**验收标准**：对你自选的 3 段不同长度报文（如空串之外的短、中、长各一段），仿真输出与 `zlib.crc32` 完全一致。若一致，说明你已彻底掌握 `lfsr` 的参数、并行展开与拓扑三要素，并具备了阅读 `axis_eth_fcs` 等上层模块的能力。

## 6. 本讲小结

- `lfsr` 是一个**纯组合逻辑**的「下一状态」函数，状态由调用方在外部寄存并回送；它本身不存任何状态。
- 7 个参数中，`LFSR_WIDTH`/`LFSR_POLY`/`LFSR_CONFIG`/`REVERSE` 决定算法本身，`DATA_WIDTH` 决定一次移多少位，`STYLE`/`LFSR_FEED_FORWARD` 决定实现细节。
- 核心机制是**并行展开**：把「连续移位 `DATA_WIDTH` 次」预编译成一张 GF(2) 线性映射掩码表，运行时只做「选位异或」，从而一个周期处理一个字节。
- **Galois** 多用于 CRC（如以太网 FCS），**Fibonacci** 多用于 PRBS/扰码（如 64b66b）；二者在掩码函数里仅「反馈相对移位的先后」不同。
- 多项式**不含最高次项**，由 `LFSR_WIDTH` 自动补回；`REVERSE=1` 让 reflected 算法（CRC-32）能按自然字节序喂数据。
- 仿真 CRC-32：初值 `0xFFFFFFFF`，逐字节回送 `state_out → state_in`，末尾取反，即可得到标准 FCS（`"123456789"` → `0xcbf43926`）。

## 7. 下一步学习建议

- 下一讲 **`u2-l2 以太网 FCS 计算、校验与插入`** 会把本模块包成 [rtl/axis_eth_fcs.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v)、`axis_eth_fcs_check.v`、`axis_eth_fcs_insert.v`，挂在 AXI-Stream 通路上——届时你会看到本讲的初值/取反/链式回送是如何被封装成「给一帧自动算出 4 字节 FCS」的。
- 想提前看 LFSR 在 PHY 层的用途，可读 [rtl/eth_phy_10g_tx_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v) 的扰码器与 PRBS-31 实例（专家层 `u10` 单元会详讲）。
- 想了解 LFSR 用作哈希的场景，可读 [rtl/arp_cache.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v)（在 `u6` ARP 单元详讲）。
