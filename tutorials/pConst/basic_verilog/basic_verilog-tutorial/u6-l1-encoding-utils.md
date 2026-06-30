# 编码转换与位反转工具箱

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚仓库里 `bin2gray` / `gray2bin` 两个模块各自到底做哪种方向的转换，并能手写它们的位运算公式（**注意：这两个文件的 INFO 注释写反了，本讲会以代码实际行为为准**）。
- 掌握「二进制 ↔ 独热（one-hot / positional）」双向转换的写法，理解 `bin2pos` 为什么是 2 的幂次位宽爆炸、`pos2bin` 如何从低位向高位扫描并报告 `err_no_hot` / `err_multi_hot`。
- 看懂 `leave_one_hot`「只留下最低位的热位」的实现，并把它和 `pos2bin` 的「取最低热位索引」联系起来。
- 理解 `reverse_vector` / `reverse_bytes` / `reverse_dimensions` 三个「纯重排线序」模块为什么综合后**不占任何 FPGA 资源**，以及它们在大端↔小端、总线字节序、二维数组转置里的用途。
- 写一个自校验 testbench，验证「二进制→格雷→二进制」往返无损，并验证 `leave_one_hot` 只保留最低置位。

本讲是 u6 单元的第一篇，所有模块都是**纯组合**（`always_comb`），没有时钟、没有复位，是理解后续仲裁器（u6-l2）、加法树（u6-l3）等更复杂组合逻辑的基石。

## 2. 前置知识

本讲依赖 u2-l4（`$clog2` / 位宽）建立的两个直觉，这里再回顾一次：

**直觉一：表示位置用「二进制」，表示「有没有选中」用「独热」。**
一个 4 路选择，你可以用 2 bit 二进制数 `00/01/10/11` 来说「选第几个」，也可以用 4 根线 `0001/0010/0100/1000` 来说「哪一根被选中了」。前者省线（log₂N 根），后者费线（N 根）但一眼就能看出谁有效、且天然适合做多请求仲裁。这两种表示之间来回转换，就是本讲的 `bin2pos` / `pos2bin`。

**直觉二：异或 `^` 是「不进位的加法」，可以用来「抵消」和「还原」。**
`a ^ a == 0`，`a ^ 0 == a`。把一个数和它右移一位的结果异或，得到格雷码；把格雷码从高位向低位「逐位异或回去」又能还原出原数。格雷码相邻两数只有 1 bit 不同，这让它在跨时钟域计数器（u3-l2 的 `cdc_strobe`）、旋转编码器、FIFO 指针里极其有用。

**术语速查**：

| 术语 | 含义 |
| --- | --- |
| 格雷码（Gray code） | 相邻两个数只有 1 bit 不同的二进制编码 |
| 独热码 / 位置码（one-hot / positional） | N bit 向量里恰好只有 1 bit 为 1，表示「选中第 i 个」 |
| 组合逻辑（combinational） | 输出只取决于当前输入，没有时钟、没有记忆 |
| 大端 / 小端（big/little-endian） | 多字节数据里「高位字节在前」还是「低位字节在前」的排列约定 |

> 阅读提示：本讲会反复强调「以代码实际行为为准」。仓库里 `bin2gray.sv` 与 `gray2bin.sv` 的 INFO 文字注释互相写反了（见 4.1.3），这正是 u1-l1 提到过的「文档会滞后于代码」的一个典型例子。**判断一个模块做什么，看它的端口名和公式，不要只看 INFO。**

## 3. 本讲源码地图

本讲涉及 8 个文件，全部位于仓库根目录，全部是纯组合模块：

| 文件 | 作用 |
| --- | --- |
| [bin2gray.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/bin2gray.sv) | **二进制→格雷码**。一行公式 `gray = bin ^ (bin>>1)`。（注意 INFO 文字写反成「Gray→binary」） |
| [gray2bin.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/gray2bin.sv) | **格雷码→二进制**。用 `for` 循环对 `gray` 的各次右移做异或前缀还原。（注意 INFO 文字写反成「binary→Gray」） |
| [bin2pos.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/bin2pos.sv) | 二进制→独热。`pos[bin]=1`，输出位宽 \(2^{\text{BIN\_WIDTH}}\)，会「爆炸」。 |
| [pos2bin.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin.sv) | 独热→二进制。从低位向高位扫描，取**最低**热位索引，并报告无热位/多热位错误。 |
| [leave_one_hot.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/leave_one_hot.sv) | 只保留输入向量里**最低的一个热位**，其余清零。是 `pos2bin` 的「兄弟」。 |
| [reverse_vector.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector.sv) | 按位反序：`out[i]=in[WIDTH-1-i]`。综合后零资源。 |
| [reverse_bytes.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_bytes.sv) | 按字节反序（大端↔小端），用 packed array 处理。综合后零资源。 |
| [reverse_dimensions.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_dimensions.sv) | 反转二维数组的两个维度（转置），用双层 generate。综合后零资源。 |

辅助文件（testbench 与函数版替代品，供实践时参考）：

| 文件 | 作用 |
| --- | --- |
| [bin2pos_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/bin2pos_tb.sv) / [pos2bin_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin_tb.sv) / [leave_one_hot_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/leave_one_hot_tb.sv) / [reverse_vector_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector_tb.sv) | 各模块自带的 testbench，演示了「随机数驱动 + 组合直查」的写法。 |
| [gray_functions_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/gray_functions_tb.sv) | 演示格雷转换的**函数版**替代写法 `gray_functions#(16)::bin2gray(...)`，与模块版功能等价。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：4.1 讲格雷码互转（`bin2gray` / `gray2bin`），4.2 讲独热互转与「留最低热位」（`bin2pos` / `pos2bin` / `leave_one_hot`），4.3 讲三个位/字节/维度反转工具（`reverse_vector` / `reverse_bytes` / `reverse_dimensions`）。

### 4.1 格雷码转换：bin2gray / gray2bin

#### 4.1.1 概念说明

普通二进制计数的致命弱点是：相邻两个数可能同时翻转很多位。例如 \(0111_2 \rightarrow 1000_2\) 一次翻转了全部 4 bit。如果这 4 根线被另一个时钟域采样，由于布线延迟不同，采样瞬间可能看到 `0000`、`0100`、`1111` 等任何中间非法值。

**格雷码（Gray code / 反射二进制码）** 通过重新排序，保证**任意两个相邻整数只有 1 bit 不同**：

| 十进制 | 二进制 | 格雷码 | 翻转位数(bin) | 翻转位数(gray) |
| ---: | ---: | ---: | :---: | :---: |
| 0 | 000 | 000 | — | — |
| 1 | 001 | 001 | 1 | 1 |
| 2 | 010 | 011 | 2 | 1 |
| 3 | 011 | 010 | 1 | 1 |
| 4 | 100 | 110 | 3 | 1 |
| 5 | 101 | 111 | 1 | 1 |
| 6 | 110 | 101 | 2 | 1 |
| 7 | 111 | 100 | 1 | 1 |

正是这个「相邻只翻 1 bit」的性质，让 u3-l2 的 `cdc_strobe` 敢用 2 位格雷计数器把脉冲安全地搬到另一个时钟域——最坏情况只是「晚一拍采样」，绝不会出现非法中间态。

本模块提供两个互逆操作：`bin2gray`（二进制→格雷）、`gray2bin`（格雷→二进制）。

> ⚠️ **重要**：`bin2gray.sv` 的 INFO 注释写着「Gray code to binary converter」，`gray2bin.sv` 的 INFO 注释写着「Binary to gray code converter」——**两条注释互相写反了**。判断方向请看**模块名、端口名和公式**，不要看这两条 INFO。下面以代码实际行为为准来讲解。

#### 4.1.2 核心流程

**二进制 → 格雷码**，经典公式只有一行：

\[
\text{gray} \;=\; \text{bin} \;\oplus\; (\text{bin} \gg 1)
\]

直觉：最高位（MSB）原样保留，其余每一位 `gray[i] = bin[i] ^ bin[i+1]`，即「本位与上一位的异或」。右移一位再异或，正好实现了这件事。

**格雷码 → 二进制**，是上面操作的逆运算，公式是「从 MSB 向 LSB 的前缀异或」：

\[
\text{bin}[i] \;=\; \bigoplus_{k=i}^{\text{WIDTH}-1} \text{gray}[k]
\]

直觉：`bin[MSB] = gray[MSB]`，`bin[i] = gray[i] ^ bin[i+1]`。展开后等价于把 `gray` 的各次右移版本全部异或在一起：

\[
\text{bin} \;=\; \text{gray} \;\oplus\; (\text{gray} \gg 1) \;\oplus\; (\text{gray} \gg 2) \;\oplus\; \cdots \;\oplus\; (\text{gray} \gg (\text{WIDTH}-1))
\]

仓库的 `gray2bin.sv` 正是用一个 `for` 循环累加这串异或。

**互逆性验证**（手算 3 位，`bin = 011`）：

- `gray = 011 ^ 001 = 010`（即十进制 3 的格雷码是 2，与上表第 4 行一致）。
- 再 `gray2bin`：`010 ^ 001(>>1) ^ 000(>>2) = 010 ^ 001 = 011`，还原成功。

伪代码：

```
bin2gray(bin):
    return bin ^ (bin >> 1)

gray2bin(gray):
    bin = 0
    for i in 0..WIDTH-1:
        bin ^= gray >> i      # 累加各次右移的异或
    return bin
```

#### 4.1.3 源码精读

**bin2gray.sv**——一行核心公式：

[bin2gray.sv:L30-L32](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/bin2gray.sv#L30-L32) —— `always_comb` 块里只有一行：`gray_out = bin_in ^ (bin_in >> 1)`，正是 4.1.2 的二进制→格雷公式。模块名 `bin2gray`、端口 `bin_in`→`gray_out`、公式三者一致，**确认它是二进制→格雷**。

```verilog
always_comb begin
  gray_out[WIDTH-1:0] = bin_in[WIDTH-1:0] ^ ( bin_in[WIDTH-1:0] >> 1 );
end
```

[bin2gray.sv:L8-L9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/bin2gray.sv#L8-L9) —— INFO 文字注释，写着 "Gray code to binary converter"。**这条注释与代码相反**，是已知的文档错误，不要被它误导。

**gray2bin.sv**——循环累加异或：

[gray2bin.sv:L30-L36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/gray2bin.sv#L30-L36) —— 先把 `bin_out` 清零，再用 `for (integer i=0; i<WIDTH; i++)` 循环，每次 `bin_out ^= gray_in >> i`，等价于把 `gray_in` 的 `>>0`、`>>1`、…、`>>(WIDTH-1)` 全部异或起来，正是 4.1.2 的格雷→二进制前缀异或式。模块名 `gray2bin`、端口 `gray_in`→`bin_out`、公式一致，**确认它是格雷→二进制**。

```verilog
always_comb begin
  bin_out[WIDTH-1:0] = '0;
  for( integer i=0; i<WIDTH; i++ ) begin
     bin_out[WIDTH-1:0] ^= gray_in[WIDTH-1:0] >> i;
  end
end
```

[gray2bin.sv:L8-L9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/gray2bin.sv#L8-L9) —— INFO 文字注释，写着 "Binary to gray code converter"。**同样与代码相反**，是另一个写反的文档错误。

> 旁支：仓库还有一个**函数版**实现，见 [gray_functions_tb.sv:L165-L172](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/gray_functions_tb.sv#L165-L172)，通过 `\`include "gray_functions.vh"` 后用 `gray_functions#(16)::bin2gray(...)` 调用，与这里的模块版功能等价，适合不想多例化一个模块、直接在 `always_comb` 里写表达式的场景。

#### 4.1.4 代码实践（手算 + 往返验证）

**实践目标**：先用纸笔验证 `bin2gray` 公式，再用最小 testbench 验证「二进制→格雷→二进制」往返无损。

**操作步骤**：

1. 对下表每个 `bin`，按 `gray = bin ^ (bin>>1)` 手算格雷码，再与「格雷码标准序列」对照。

| bin(十进制) | bin(4 位二进制) | 手算 gray | 标准序列 gray |
| ---: | ---: | ---: | ---: |
| 3 | 0011 | ? | 0010 |
| 5 | 0101 | ? | 0111 |
| 10 | 1010 | ? | 1111 |

2. 在仓库根目录新建下面这个最小 testbench（**示例代码**，非项目原有文件），例化 `WIDTH=4` 的 `bin2gray` 和 `gray2bin` 串联，遍历全部 16 个值做往返自检：

```verilog
// 示例代码：gray_roundtrip_tb.sv
`timescale 1ns / 1ps

module gray_roundtrip_tb;

  logic [3:0] bin_in, gray_mid, bin_back;

  bin2gray #(.WIDTH(4)) bg (.bin_in(bin_in),   .gray_out(gray_mid));
  gray2bin #(.WIDTH(4)) gb (.gray_in(gray_mid),.bin_out(bin_back));

  integer i, errors = 0;
  initial begin
    for (i = 0; i < 16; i = i + 1) begin
      bin_in = i[3:0];
      #1;                                   // 等组合逻辑稳定
      if (bin_back !== bin_in) begin
        $display("FAIL at %0d: gray=%b back=%b", i, gray_mid, bin_back);
        errors = errors + 1;
      end
    end
    if (errors == 0) $display("gray round-trip OK for all 16 values");
    else             $display("FAILED with %0d errors", errors);
    $finish;
  end

endmodule
```

3. 用 iverilog（须 `-g2012`，因为这些 `.sv` 用了 `logic`/`always_comb`）编译运行：

```bash
iverilog -g2012 -o gray.vvp gray_roundtrip_tb.sv bin2gray.sv gray2bin.sv
vvp gray.vvp
```

**预期结果**：

- 手算列：`3→0010`、`5→0111`、`10→1111`，与标准序列一致。
- 仿真打印 `gray round-trip OK for all 16 values`，`errors` 为 0。

**需要观察的现象**：留意 `bin=7(0111)` 这一行——它的格雷码是 `0100`，与 `bin` 只差 1 bit；而如果把 `bin` 直接当二进制看，`7→8` 会翻转 4 bit。这正是格雷码的价值。把 `WIDTH` 改成 8、循环上限改成 256，可验证更大位宽（本讲综合实践会用到）。

> 待本地验证：`#1` 延时是为让 `always_comb` 在输入变化后重新求值；不同仿真器对零时刻组合更新的调度略有差异，若读到 X，可把 `#1` 加大到 `#10`。

#### 4.1.5 小练习与答案

**练习 1**：`bin2gray.sv` 的 INFO 注释说它是 "Gray code to binary converter"，这句话对吗？怎么仅凭代码判断它的真实方向？

> **答案**：不对，注释写反了。判断方法：看模块名 `bin2gray`、端口 `bin_in`（输入）→`gray_out`（输出）、以及公式 `gray = bin ^ (bin>>1)`——三者都表明它是**二进制→格雷**。INFO 文字是已知的文档错误。

**练习 2**：为什么 `gray2bin` 要用 `for` 循环，而 `bin2gray` 只有一行？

> **答案**：二进制→格雷每位独立（`gray[i]=bin[i]^bin[i+1]`），一次 `bin ^ (bin>>1)` 就能并行算出所有位；格雷→二进制是「前缀异或」（`bin[i]` 依赖所有 `gray[k], k≥i]`），需要把各次右移累加，所以用循环展开成一长串异或门。

**练习 3**：4 位格雷码，`bin=12(1100)` 的格雷码是多少？

> **答案**：`1100 ^ 0110 = 1010`。验证：从标准序列看，十进制 12 的格雷码确实是 `1010`。

---

### 4.2 独热转换：bin2pos / pos2bin / leave_one_hot

#### 4.2.1 概念说明

「二进制」与「独热（one-hot，仓库叫 positional）」是描述「选中哪一个」的两种表示，先建立对照：

| 含义 | 二进制（2 bit） | 独热（4 bit） |
| --- | --- | --- |
| 选第 0 个 | 00 | 0001 |
| 选第 1 个 | 01 | 0010 |
| 选第 2 个 | 10 | 0100 |
| 选第 3 个 | 11 | 1000 |

二进制省线（\( \lceil \log_2 N \rceil \) 根），独热播费线（\(N\) 根）但好处是「谁有效一目了然」，且天然适合「多个请求同时有效，要挑一个」的仲裁场景（u6-l2 的 `priority_enc` / `round_robin_enc` 都建立在独热之上）。

仓库提供两个互逆转换器，外加一个「留最低热位」的工具：

- **`bin2pos`**：二进制→独热。`2'd0 → 4'b0001`，`8'd5 → 256'b…00100000`（第 5 位为 1）。
- **`pos2bin`**：独热→二进制（`bin2pos` 的逆），并额外报告两种异常：**无热位**（`err_no_hot`）、**多热位**（`err_multi_hot`）。
- **`leave_one_hot`**：输入一个任意位向量，只**保留最低的那一个热位**，其余清零。例如 `1101_0010 → 0000_0010`。

> 「多热位」是独热编码里的非法状态。`pos2bin` 在多热位时仍然给出一个结果（最低热位的索引），同时用 `err_multi_hot` 告警；`leave_one_hot` 则干脆把多热位「修正」成只剩最低位的合法独热。两者都遵循「以最低位为准」的约定。

#### 4.2.2 核心流程

**bin2pos（二进制→独热）**：一行核心动作——把输出先清零，再把第 `bin` 个比特置 1：

```
pos = 0
pos[bin] = 1
```

这等价于一个 \(2^{\text{BIN\_WIDTH}}\) 选 1 的译码器（decoder）。注意位宽「爆炸」：`BIN_WIDTH=8` 时 `POS_WIDTH=256`。

**pos2bin（独热→二进制）**：从低位（bit 0）向高位扫描，**记录第一个为 1 的位的索引**；若之后又遇到 1，置 `err_multi_hot`；若全 0，置 `err_no_hot`：

```
found = 0
bin = 0
for i in 0..POS_WIDTH-1:
  if (not found) and pos[i]:   # 第一个热位
      bin = i
  if found and pos[i]:         # 又遇到一个热位 → 多热位
      err_multi_hot = 1
  if pos[i]:
      found = 1
err_no_hot = (pos == 0)
```

**leave_one_hot（留最低热位）**：对每一位 `i`，输出为 1 当且仅当「`in[i]` 为 1 **且** 比 `i` 更低的所有位都为 0」：

```
out[0] = in[0]
out[i] = in[i] and (lower bits in[i-1:0] all zero)   # i>=1
```

「更低位全零」用归约或 `|in[i-1:0] == 0` 判断。这样只有**最低的那个热位**满足条件，更高位的热位都会因为「下面已经有一个 1」而被清掉。

#### 4.2.3 源码精读

**bin2pos.sv**——译码器：

[bin2pos.sv:L34-L37](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/bin2pos.sv#L34-L37) —— `always_comb` 里先把 `pos` 清零，再 `pos[bin] = 1'b1`。`bin` 是变量索引，综合器会展开成一个 \(2^{\text{BIN\_WIDTH}}\) 选 1 译码器。

```verilog
always_comb begin
  pos = 0;
  pos[bin] = 1'b1;
end
```

[bin2pos.sv:L25-L31](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/bin2pos.sv#L25-L31) —— 参数 `POS_WIDTH = 2**BIN_WIDTH`，正是「位宽爆炸」的来源：`BIN_WIDTH=8` 时输出 256 bit。INFO 里的 `8'd5 becomes 256'b100000` 描述的就是把 bit 5 单独置 1（那个字面量只画了 6 位，省略了高位的 0）。

**pos2bin.sv**——扫描取最低热位 + 错误报告：

[pos2bin.sv:L42](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin.sv#L42) —— `err_no_hot = (pos == 0)`，用全等比较判断「一个热位都没有」。

[pos2bin.sv:L47-L66](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin.sv#L47-L66) —— 用一个 `for (i=0; i<POS_WIDTH; i++)` 从低位扫到高位：`~found_hot && pos[i]` 时记录 `bin = i`（即**最低**热位的索引）；`found_hot && pos[i]` 时置 `err_multi_hot`；任何 `pos[i]` 为 1 都把 `found_hot` 置 1。注意 [pos2bin.sv:L37-L38](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin.sv#L37-L38) 的注释明确：多热位时「only least-sensitive active bit affects the output」——即输出只反映**最低位**的热位，更高的热位只触发告警、不改变 `bin`。

```verilog
for (i=0; i<POS_WIDTH; i++) begin
  if ( ~found_hot && pos[i] ) begin
    bin[(BIN_WIDTH-1):0] = i[(BIN_WIDTH-1):0];   // 记下最低热位索引
  end
  if ( found_hot && pos[i] ) begin
    err_multi_hot=1'b1;                          // 又遇到一个 → 多热位
  end
  if ( pos[i] ) begin
    found_hot = 1'b1;
  end
end
```

[pos2bin_tb.sv:L100-L122](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin_tb.sv#L100-L122) —— 自带 testbench 演示了如何**故意注入错误**来观察告警：用一个组合块，在 `&RandomNumber1[7:4]` 时把 `pos` 与随机数或起来（制造多热位）、在 `&RandomNumber1[11:8]` 时把 `pos` 清零（制造无热位），再把结果送进 `pos2bin` 看 `err_no_hot` / `err_multi_hot` 是否如期置位。这是「故意构造非法输入验证错误通路」的好范例。

**leave_one_hot.sv**——留最低热位：

[leave_one_hot.sv:L34-L45](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/leave_one_hot.sv#L34-L45) —— 用 `generate for (i=1; i<WIDTH; i++)` 为每个高位生成一个 `always_comb`：`out[i] = in[i] && ~( |in[(i-1):0] )`。含义正是「`in[i]` 为 1 **且** 低于 i 的位没有 1」。bit 0 单独用 [leave_one_hot.sv:L45](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/leave_one_hot.sv#L45) 的 `assign out[0] = in[0];` 处理（它没有更低的位可比）。

```verilog
generate
  for( i=1; i<WIDTH; i++ ) begin : gen_for
    always_comb begin
        out[i] <= in[i] && ~( |in[(i-1):0] );
    end
  end
endgenerate
assign out[0] = in[0];
```

[leave_one_hot.sv:L6-L12](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/leave_one_hot.sv#L6-L12) —— INFO 给了两个例子：`16'b1101_0000 → 0001_0000`（最低热位是 bit 4，保留它）、`16'b1101_0010 → 0000_0010`（最低热位是 bit 1，保留它）。⚠️ 注意例子里写成 `8'b…` 是字面量位宽笔误，实际应是 `16'b`，但**结果数值是对的**（保留的就是最低热位）。又是「以代码为准」的一例。

> 代码风格提示：[leave_one_hot.sv:L39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/leave_one_hot.sv#L39) 在 `always_comb` 里用了非阻塞赋值 `<=`，而 u1-l2 讲过组合逻辑惯例是用阻塞 `=`（对比 [reverse_vector.sv:L34](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector.sv#L34) 用的就是 `=`）。这在纯组合、无反馈的场合能综合出同样的电路，属作者的写法不一致，读代码时心里有数即可。

#### 4.2.4 代码实践（构造多热位，观察 pos2bin 与 leave_one_hot）

**实践目标**：亲手制造「合法独热」「多热位」「无热位」三种输入，对照观察 `pos2bin` 的输出与错误标志、`leave_one_hot` 的「留最低位」行为。

**操作步骤**：

1. 在仓库根目录新建下面这个 testbench（**示例代码**）。它例化 `BIN_WIDTH=4`（即 `POS_WIDTH=16`）的 `bin2pos`→`pos2bin` 闭环，外加一个 `WIDTH=8` 的 `leave_one_hot`：

```verilog
// 示例代码：onehot_tb.sv
`timescale 1ns / 1ps

module onehot_tb;

  // bin2pos -> pos2bin 闭环（4 bit <-> 16 bit one-hot）
  logic [3:0]  bin;
  logic [15:0] pos;
  logic [3:0]  bin_back;
  logic        e_no, e_multi;

  bin2pos #(.BIN_WIDTH(4)) bp (.bin(bin), .pos(pos));
  pos2bin #(.BIN_WIDTH(4)) pb (
    .pos(pos), .bin(bin_back), .err_no_hot(e_no), .err_multi_hot(e_multi)
  );

  // leave_one_hot（8 bit）
  logic [7:0] v_in, v_out;
  leave_one_hot #(.WIDTH(8)) loh (.in(v_in), .out(v_out));

  initial begin
    // (a) 合法独热往返：0..15 都应还原，无错误
    $display("== bin2pos -> pos2bin round-trip ==");
    for (int i = 0; i < 16; i = i + 1) begin
      bin = i[3:0]; #1;
      $display("  bin=%0d pos=%b back=%0d err_no=%b err_multi=%b",
               i, pos, bin_back, e_no, e_multi);
    end

    // (b) leave_one_hot：多热位只留最低
    $display("== leave_one_hot ==");
    v_in = 8'b1101_0010; #1;   // 热位: bit1,bit4,bit6,bit7 -> 最低 bit1
    $display("  in=%b  out=%b (expect 0000_0010)", v_in, v_out);

    v_in = 8'b0000_0000; #1;   // 无热位
    $display("  in=%b  out=%b (expect 0000_0000)", v_in, v_out);

    v_in = 8'b0000_0100; #1;   // 单一热位，原样保留
    $display("  in=%b  out=%b (expect 0000_0100)", v_in, v_out);
    $finish;
  end

endmodule
```

2. 编译运行（iverilog，须把用到的三个模块一起编进去）：

```bash
iverilog -g2012 -o onehot.vvp onehot_tb.sv bin2pos.sv pos2bin.sv leave_one_hot.sv
vvp onehot.vvp
```

**预期结果**：

- (a) 16 行里 `back` 始终等于 `i`，`err_no` / `err_multi` 恒为 0。
- (b) 三行 `out` 分别为 `0000_0010`、`0000_0000`、`0000_0100`。

**需要观察的现象**：在 (a) 里，`pos` 每行都恰好只有 1 bit 为 1，且那个 1 的位置正好是 `i`——这就是「译码」。如果你想看 `err_multi_hot` 真的点亮，可以参考 [pos2bin_tb.sv:L100-L109](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin_tb.sv#L100-L109) 的做法，把另一个随机值或进 `pos` 再送进 `pos2bin`。

> 待本地验证：`for (int i ...)` 是 SystemVerilog 写法，需 `-g2012`；若用老式 Verilog，把 `int i` 改成先声明 `integer i;` 再 `for (i=0; ...)`。

#### 4.2.5 小练习与答案

**练习 1**：`bin2pos` 的 `BIN_WIDTH=6` 时，输出 `pos` 有多少 bit？为什么说它「位宽爆炸」？

> **答案**：`POS_WIDTH = 2^6 = 64` bit。因为独热编码每个可能值占独立的一根线，N bit 二进制对应 \(2^N\) 根独热线，指数增长，所以只适合 BIN_WIDTH 较小的场合（仓库 testbench 里常用 4）。

**练习 2**：`pos2bin` 在输入 `16'b1010_0000`（bit 7 和 bit 5 同时为 1）时，`bin`、`err_no_hot`、`err_multi_hot` 分别是多少？

> **答案**：从低位扫，第一个热位是 bit 5，所以 `bin = 5`；之后又遇到 bit 7，`err_multi_hot = 1`；输入非全零，`err_no_hot = 0`。即「输出取最低热位 5，同时报告多热位错误」。

**练习 3**：`leave_one_hot` 和 `pos2bin` 对「多热位」的处理有什么联系？

> **答案**：两者都「以最低热位为准」。`pos2bin` 把最低热位的索引作为输出、并点亮 `err_multi_hot` 告警；`leave_one_hot` 则把多热位「清洗」成只剩最低热位的合法独热。可以把 `leave_one_hot` 看作「先把输入修正成合法独热」，再接 `pos2bin` 就不会触发 `err_multi_hot`。

---

### 4.3 位 / 字节 / 维度反转：reverse_vector / reverse_bytes / reverse_dimensions

#### 4.3.1 概念说明

这三兄弟解决的是「**物理线序**重排」问题：信号本身不变，只是把多位总线里的位、字节、或二维维度的顺序整个倒过来。

为什么要单独做模块？因为在 SystemVerilog 里，`out[i] = in[WIDTH-1-i]` 这种「下标反向」不能简单地用一条赋值写完整个总线（直接 `out = {<<{in}}` 流运算虽能做，但可读性、可综合性因工具而异）。把它们封装成模块，既可读、又可复用，还能在波形和网表里看到一个清晰的「反转」节点。

**关键结论：这三个模块综合后不占任何 FPGA 资源。** 因为它们只是改变连线关系（哪根线接哪根线），综合器会把它们优化成纯线网（wire），零逻辑门、零触发器。INFO 注释里反复强调的 "instance does NOT occupy any FPGA resources!" 就是这个意思。

三者各自的应用：

- **`reverse_vector`**：按位反序。把 `in[7]` 接到 `out[0]`、`in[6]` 接到 `out[1]`…… 常用于把「MSB first」和「LSB first」的数据流互转（u5-l2 的 `spi_master` 就用它实现 MSB/LSB first 可配置）。
- **`reverse_bytes`**：按字节反序。把整字节为单位倒过来，经典用途是 **大端↔小端** 转换（网络字节序 ↔ 主机字节序）。
- **`reverse_dimensions`**：反转二维 packed 数组的两个维度（类似矩阵转置），用于多维数据（如图像行/列）的维度重排。

#### 4.3.2 核心流程

三者都是「按下标公式重连」，没有任何运算：

```
reverse_vector:        out[i]        = in[WIDTH-1-i]              # 按位倒序
reverse_bytes:         out_byte[i]   = in_byte[BYTES-1-i]         # 按字节倒序
reverse_dimensions:    out[j][i]     = in[i][j]                   # 二维转置
```

它们都用 `generate`（或 `for`）在**编译期**把每个下标连接展开成一堆连续赋值，运行期没有「循环」开销。

> 「位反序」是这三个里最容易和「按位取反」搞混的。`reverse_vector` **不改变 0/1 的值**，只改变它们的位置；而按位取反 `~in` 会把 0 变 1、1 变 0。两者完全不同。

#### 4.3.3 源码精读

**reverse_vector.sv**——按位反序：

[reverse_vector.sv:L32-L36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector.sv#L32-L36) —— 在一个 `always_comb` 里用 `for (i=0; i<WIDTH; i++)` 给每一位赋值 `out[i] = in[(WIDTH-1)-i]`，正是「下标倒序」。

```verilog
always_comb begin
  for (i = 0; i < WIDTH ; i++) begin : gen_reverse
    out[i] = in[(WIDTH-1)-i];
  end
end
```

[reverse_vector.sv:L6-L9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector.sv#L6-L9) —— INFO 说明 `in[7]→out[0]`、`in[6]→out[1]`，并强调 "instance does NOT occupy any FPGA resources!"。[reverse_vector.sv:L15](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector.sv#L15) 还提醒 `WIDTH must be >=2`。仓库 `spi_master.sv` 正是靠它在入口/出口各做一次位反序，实现 MSB/LSB first 可配置。

**reverse_bytes.sv**——按字节反序（大端↔小端）：

[reverse_bytes.sv:L33-L47](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_bytes.sv#L33-L47) —— 先用 packed array `logic [BYTES-1:0][7:0] byte_data` 把输入 `in` 重新「视角化」成字节阵列（`assign byte_data = in;`，纯线网 reinterpretation），再用 `generate for` 把第 `i` 个字节接到第 `BYTES-1-i` 个位置：`rev_byte_data[i] = byte_data[(BYTES-1)-i]`，最后 `assign out = rev_byte_data;`。

```verilog
genvar i;
generate
  for (i = 0; i < BYTES ; i++) begin : gen_reverse
    always_comb begin
      rev_byte_data[i] = byte_data[(BYTES-1)-i];
    end
  end
endgenerate
assign out = rev_byte_data;
```

[reverse_bytes.sv:L6-L10](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_bytes.sv#L6-L10) —— INFO 给例：`in[15] byte becomes out[7]`（2 字节情形，高字节与低字节互换），并点明用途 "convert big-endian data to little-endian"。

> 技巧：用 packed array `[BYTES-1:0][7:0]` 把一根扁平总线「切片」成字节，是 SystemVerilog 处理字节序的惯用法。它不消耗资源，只是给同一组 bit 换了个分组视角。

**reverse_dimensions.sv**——二维转置：

[reverse_dimensions.sv:L34-L46](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_dimensions.sv#L34-L46) —— 输入是二维 packed 数组 `[D1_WIDTH-1:0][D2_WIDTH-1:0]`，输出维度对调成 `[D2_WIDTH-1:0][D1_WIDTH-1:0]`，用双层 `generate` 做 `out[j][i] = in[i][j]`，本质就是矩阵转置。

```verilog
generate
  for (i = 0; i < D1_WIDTH ; i++) begin : gen_i
  for (j = 0; j < D2_WIDTH ; j++) begin : gen_j
    always_comb begin
      out[j][i] = in[i][j];
    end
  end
  end
endgenerate
```

[reverse_dimensions.sv:L6-L9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_dimensions.sv#L6-L9) —— INFO 例：`in[7][1] → out[1][7]`、`in[6][10] → out[10][6]`，即两个维度的尺寸互换。

#### 4.3.4 代码实践（验证 reverse_vector 的可逆性）

**实践目标**：用 testbench 验证「连续两次 `reverse_vector` 等于恒等（还原）」，并直观看到位序翻转。

**操作步骤**：

1. 在仓库根目录新建下面这个 testbench（**示例代码**）。例化两个 `WIDTH=8` 的 `reverse_vector` 串联，输入随机图案，观察第一次反序、第二次还原：

```verilog
// 示例代码：reverse_tb.sv
`timescale 1ns / 1ps

module reverse_tb;

  logic [7:0] in, mid, out;

  reverse_vector #(.WIDTH(8)) r1 (.in(in),  .out(mid));
  reverse_vector #(.WIDTH(8)) r2 (.in(mid), .out(out));

  initial begin
    in = 8'b1001_0110; #1;
    $display("in   =%b", in);
    $display("mid  =%b  (一次反序: 1001_0110 -> 0110_1001)", mid);
    $display("out  =%b  (两次反序应还原为 in)", out);
    if (out === in) $display("PASS: 两次反序还原成功");
    else            $display("FAIL: 还原失败");
    $finish;
  end

endmodule
```

2. 编译运行：

```bash
iverilog -g2012 -o rev.vvp reverse_tb.sv reverse_vector.sv
vvp rev.vvp
```

**预期结果**：

```
in   =10010110
mid  =01101001
out  =10010110
PASS: 两次反序还原成功
```

**需要观察的现象**：`mid` 是 `in` 的镜像（bit7↔bit0、bit6↔bit1…），`out` 又翻回来等于 `in`。可以在综合后查看该实例的资源报告，确认 `reverse_vector` 实例的 LUT/FF 使用为 0（INFO 所 claims 的 "no FPGA resources"）。

> 待本地验证：「零资源」需在真实综合器（Quartus/Vivado）的 resource report 里确认；纯仿真无法看到资源占用。`WIDTH` 奇偶都可工作（仓库 `reverse_vector_tb.sv` 分别例化了 `WIDTH=15` 和 `WIDTH=14` 验证奇偶位宽，见 [reverse_vector_tb.sv:L85-L101](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector_tb.sv#L85-L101)）。

#### 4.3.5 小练习与答案

**练习 1**：`reverse_vector` 和按位取反 `~in` 有什么区别？

> **答案**：`reverse_vector` 只改变位的位置（线序），不改变 0/1 的值；`~in` 只改变值（0↔1），不改变位置。例如 `1001` 经 `reverse_vector` 得 `1001`（回文），经 `~` 得 `0110`。

**练习 2**：为什么这三个模块综合后不占 FPGA 资源？

> **答案**：它们只描述「哪根线接哪根线」的连接关系（连续赋值/纯组合直连），没有任何逻辑运算（与/或/非）也没有触发器。综合器把它优化成纯线网（连线），所以 LUT 和 FF 占用都是 0。

**练习 3**：要把一个 32 bit 大端数据转成小端，用哪个模块？参数怎么设？

> **答案**：用 `reverse_bytes`，设 `BYTES=4`（32 bit = 4 字节）。它会把字节 0↔3、1↔2 对调，正好完成大端↔小端转换。

---

## 5. 综合实践

把本讲三个模块串起来：写一个**自校验** testbench，一次性验证（a）8 位二进制经 `bin2gray → gray2bin` 往返还原；（b）`leave_one_hot` 对多热位输入只保留最低置位。这正是本讲规格要求的实践任务。

**实践目标**：用一个 testbench 同时覆盖「格雷往返无损」和「留最低热位」两条结论，对全部 256 个 8 位值做穷举自检，并对几个典型多热位向量做点测。

**操作步骤**：

1. 在仓库根目录新建 `encoding_utils_tb.sv`（**示例代码**）：

```verilog
// 示例代码：encoding_utils_tb.sv
`timescale 1ns / 1ps

module encoding_utils_tb;

  // ===== (a) gray 往返：8 位 =====
  logic [7:0] b_in, g_mid, b_back;
  bin2gray #(.WIDTH(8)) bg (.bin_in(b_in),   .gray_out(g_mid));
  gray2bin #(.WIDTH(8)) gb (.gray_in(g_mid), .bin_out(b_back));

  // ===== (b) leave_one_hot：8 位 =====
  logic [7:0] oh_in, oh_out;
  leave_one_hot #(.WIDTH(8)) loh (.in(oh_in), .out(oh_out));

  integer i;
  integer errors = 0;

  // 计算最低置位的期望值：mask 掉所有高于最低置位的位
  // 即 oh_in & (-(oh_in)) 的低 8 位（隔离最低置位）
  function [7:0] lowest_hot(input [7:0] v);
    integer k;
    reg found;
    begin
      lowest_hot = 8'h00;
      found = 1'b0;
      for (k = 0; k < 8; k = k + 1) begin
        if (!found && v[k]) begin
          lowest_hot[k] = 1'b1;
          found = 1'b1;
        end
      end
    end
  endfunction

  initial begin
    // ---- (a) 8 位 gray 往返穷举自检 ----
    $display("== (a) bin2gray -> gray2bin round-trip, 8 bit, all 256 values ==");
    for (i = 0; i < 256; i = i + 1) begin
      b_in = i[7:0];
      #1;
      if (b_back !== b_in) begin
        $display("  FAIL at %0d: gray=%b back=%b", i, g_mid, b_back);
        errors = errors + 1;
      end
    end
    if (errors == 0) $display("  round-trip OK for all 256 values");

    // ---- (b) leave_one_hot 点测 ----
    $display("== (b) leave_one_hot, 8 bit ==");
    for (i = 0; i < 256; i = i + 1) begin
      oh_in = i[7:0];
      #1;
      if (oh_out !== lowest_hot(oh_in)) begin
        $display("  FAIL at in=%b: out=%b expect=%b",
                 oh_in, oh_out, lowest_hot(oh_in));
        errors = errors + 1;
      end
    end

    // 几个典型值明文打印
    oh_in = 8'b1101_0010; #1;
    $display("  in=1101_0010 out=%b (expect 0000_0010)", oh_out);
    oh_in = 8'b0000_0000; #1;
    $display("  in=0000_0000 out=%b (expect 0000_0000)", oh_out);
    oh_in = 8'b1000_0000; #1;
    $display("  in=1000_0000 out=%b (expect 1000_0000)", oh_out);

    if (errors == 0) $display("ALL CHECKS PASSED");
    else             $display("FAILED with %0d errors", errors);
    $finish;
  end

endmodule
```

2. 编译运行（把三个被测模块一起编进去）：

```bash
iverilog -g2012 -o enc.vvp encoding_utils_tb.sv bin2gray.sv gray2bin.sv leave_one_hot.sv
vvp enc.vvp
```

**需要观察的现象**：

- (a) 部分：256 个值全部往返还原，没有任何 `FAIL`，证明 `bin2gray` 与 `gray2bin` 互为逆函数。
- (b) 部分：256 个值里，`leave_one_hot` 的输出与黄金模型 `lowest_hot()` 完全一致；三个明文例子分别是 `0000_0010`（多热位→最低位 bit1）、`0000_0000`（无热位）、`1000_0000`（单一最高热位原样保留）。
- 最后一行应为 `ALL CHECKS PASSED`。

**预期结果**：终端打印 `round-trip OK for all 256 values`、三个明文例子数值正确、最终 `ALL CHECKS PASSED`。

> 待本地验证：`function` 内用 `for` + `reg found` 是可综合写法，在 iverilog `-g2012` 下应可通过；若编译报 `lowest_hot` 作用域问题，可把期望值改用纯表达式 `oh_in & (~(oh_in) + 1)`（位运算隔离最低置位，即 `oh_in & -oh_in`）来计算，效果相同。

## 6. 本讲小结

- **`bin2gray`（二进制→格雷）**：一行 `gray = bin ^ (bin>>1)`；**`gray2bin`（格雷→二进制）**：循环累加 `bin ^= gray>>i`。两者互为逆函数。⚠️ 两文件的 INFO 文字注释互相写反了，判断方向要看模块名、端口名和公式。
- 格雷码「相邻两数只翻 1 bit」的性质，是 u3-l2 `cdc_strobe` 跨时钟域安全搬移脉冲的基石。
- **`bin2pos`（二进制→独热）** 是 \(2^N\) 选 1 译码器，`pos[bin]=1`，位宽随 `BIN_WIDTH` 指数爆炸；**`pos2bin`（独热→二进制）** 从低位扫描取最低热位索引，并报告 `err_no_hot` / `err_multi_hot`。
- **`leave_one_hot`** 只保留最低热位：`out[i] = in[i] && ~(|in[i-1:0])`，与 `pos2bin` 同样「以最低位为准」，可看作把多热位清洗成合法独热。
- **`reverse_vector` / `reverse_bytes` / `reverse_dimensions`** 三个模块只重排线序、不做任何运算，综合后**零 FPGA 资源**；分别用于位序翻转（MSB/LSB）、字节序翻转（大端↔小端）、二维转置。
- 贯穿全讲的阅读纪律：**遇到 INFO 注释与代码冲突，以代码端口名和公式为准**（本讲的 `bin2gray`/`gray2bin` 注释互换、`leave_one_hot` 例子的 `8'b` 笔误都是例子）。

## 7. 下一步学习建议

- 顺着「独热」主线进入 **u6-l2（优先级与轮询仲裁）**：`priority_enc` 直接复用 `leave_one_hot` + `pos2bin` 的思想把多请求独热向量编码成一个授权，`round_robin_enc` 在此基础上轮转优先级。本讲的 `pos2bin` / `leave_one_hot` 是读懂仲裁器的直接前置。
- 想看格雷码的「实战」用法，回顾 **u3-l2（cdc_strobe）**：那里用 2 位格雷计数器把单拍脉冲安全搬到另一个时钟域，是 `bin2gray` 思想的典型落地。
- `reverse_vector` 的真实应用见 **u5-l2（spi_master）**：它在移位寄存器入口/出口各做一次位反序，实现 SPI 的 MSB first / LSB first 可配置，是「零资源重排」服务于协议字序的好例子。
- 继续本单元后续：**u6-l3（加法树、滤波、PWM/PDM）** 和 **u6-l4（脉冲与事件发生）** 会用到本讲建立的全组合逻辑阅读基础。
