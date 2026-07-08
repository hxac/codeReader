# 计数、检测与投票

## 1. 本讲目标

本讲进入「位操作与高级布尔运算」单元。前面你已经会用 `Annuller`、`Bit_Reducer`、`Word_Reducer` 把位/字折叠成一位或一个字（见 u5-l1），也见过 `Bitmask_Isolate_Rightmost_1_Bit` 用 `x & (-x)` 孤立最低位 1 的技巧（见 u11-l1 优先编码器）。本讲把这些零件组合成五个「面向整数语义」的实用模块。

学完后你应该能够：

1. 说清 **population count（popcount / 汉明重量）** 的电路结构——查表 + 加法链，以及为何故意把输出位宽留宽、再让 CAD 收回。
2. 用「孤立最低位 1 → 取对数」实现 **ntz（尾零计数）**，并理解为何 **nlz（首零计数）** 只是「位反转 + ntz」、零额外逻辑。
3. 用「XOR + popcount」实现 **Hamming_Distance（汉明距离）**，用「popcount + 比较阈值」实现 **Bit_Voting（位投票）** 的五种结果。

## 2. 前置知识

- **位宽与复制构造**：用 `{N{1'b0}}` 构造定宽常量，用 `base +: width` 做变址部分位选（见 u2-l2）。
- **组合块与阻塞赋值**：本讲五个模块都是纯组合逻辑，`always @(*)` 内一律阻塞 `=`（见 u3-l1）。
- **字归约 `Word_Reducer`**：把多个字按某布尔运算（AND/OR/XOR/...）逐位折叠成一个字，是「逐位同时比较多个字」的通用工具（见 u5-l1）。
- **孤立最低位 1**：`x & (-x)` 把一个字里最低位的 1 单独留下，其余清零（见 u11-l1）。
- **`clog2(N)`**：返回编址 N 个项目所需的二进制位数 ⌈log₂N⌉（见 u8-l2）。
- **lean on CAD**：本书反复出现的哲学——故意把常量、位宽写成「够宽但恒定」的形式，让综合器在精化/优化阶段替你折叠掉冗余逻辑，源码因此更简单、更不易写错。

> 关键直觉：popcount 是本讲的「母模块」。ntz/nlz 借助它处理边界；Hamming_Distance 和 Bit_Voting 直接实例化它。掌握 popcount，其余四个都是它的组合应用。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
|------|------|----------|
| `Population_Count.v` | 数出输入字中 1 的个数（popcount） | **母模块**，其余四个都复用它或它的零件 |
| `Number_of_Trailing_Zeros.v` | 数最低位 1 之前的连续 0 个数（ntz） | 「孤立最低位 1 + 取对数」范本 |
| `Number_of_Leading_Zeros.v` | 数最高位 1 之前的连续 0 个数（nlz） | 「位反转 + ntz」，零逻辑 |
| `Hamming_Distance.v` | 数两个字之间不同的位数 | 「XOR + popcount」 |
| `Bit_Voting.v` | 把一个字看成选票，输出全 1/全 0/多数/少数/平票 | 「popcount + 阈值比较」 |

辅助构件（前序讲义已讲，本讲会引用其关键行）：

| 文件 | 作用 |
|------|------|
| `Bitmask_Isolate_Rightmost_1_Bit.v` | `x & (-x)`，孤立最低位 1 |
| `Logarithm_of_Powers_of_Two.v` | 把独热位转换成它的位索引（对数） |
| `Word_Reverser.v` | 精化期重排字序/位序，零逻辑 |
| `Word_Reducer.v` | 多字逐位布尔归约（这里用来做 XOR） |

## 4. 核心概念与源码讲解

### 4.1 人口计数 (Population Count)

#### 4.1.1 概念说明

**Population count**（也叫 **Hamming weight**、popcount）回答一个最朴素的问题：

> 一个 N 位字里，到底有几个 1？

它把一个「位掩码」映射成一个「整数」，于是所有整数运算（比较、加减、范围判断）都能用来描述位集合的性质。用途极广：奇偶校验、纠错码、网络掩码匹配、神经网络的二值化激活统计、哈希（如 SimHash 的符号比较）、优先级仲裁里的请求计数……都可以归结为 popcount。

N 位输入最多有 N 个 1，要表示 0..N 这个范围，需要

\[
\text{POPCOUNT\_WIDTH} = \lceil \log_2(N+1) \rceil
\]

位。例如 32 位字的 popcount 用 6 位即可表示 0..32。这是本讲所有「位数计算」的出发点。

#### 4.1.2 核心流程

直接用一个 N 输入加法器把 N 个位加起来当然可以，但那样写出来的代码会随 N 变化、难以参数化。本书采用一个「查表 + 加法链」的算法，对任意 N 都用同一份代码：

```
1. 把 N 位输入切成 N/2 个「位对」（每对 2 位）。
2. 用一张 4 项的查表，把每个位对 (00,01,10,11) 映射成它的 1 的个数 (0,1,1,2)。
3. 把每个 2 位的「对计数」零扩展到 POPCOUNT_WIDTH。
4. 把所有对计数一个接一个累加（一条加法链），链尾就是总数。
5. 若 N 为奇数，把孤立的最高位单独加进累加器。
```

这条加法链看似是「链式」而非「树式」，但因为大部分被加的位是恒定 0，CAD 工具会在优化阶段把它重组成一棵带进位链的 LUT/加法树，最终的关键路径长度仍只有 \(\lceil\log_2(N+1)\rceil\) 级——和手写树式加法器一样好，代码却简单得多。这正是 **lean on CAD** 的典型范例。

> 精化期「剥出首迭代」（peeled-out first iteration）：累加链里第一个对计数没有「前一个」可加，所以第 0 项单独写，循环从 `i=1` 开始。这样循环体内引用 `i-1` 时永远不会出现负下标。这个模式在 `Population_Count`、`Logarithm_of_Powers_of_Two`、`Word_Reducer` 里反复出现。

#### 4.1.3 源码精读

**模块端口与输出位宽**。`POPCOUNT_WIDTH` 不让用户设，直接等于 `WORD_WIDTH`（故意留宽），见 [Population_Count.v:50-60](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L50-L60)。注释（[Population_Count.v:23-38](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L23-L38)）说清了理由：让用户记住输出该是 ⌈log₂(N+1)⌉ 而非 ⌈log₂N⌉ 很容易出错，索性输出位宽等于输入位宽、让综合器自动推断出真正的窄位宽并把多余逻辑清掉。`count_out` 是 `output reg`，并用 `initial` 初始化为 0（承接 u2-l1 的 reg 输出初始化约定）。

**4 项查表**。一个 2 位值描述「位对本身」，一个 2 位值描述「该位对里 1 的个数」，两者位数恰好相同，于是可以直接用位对的值当索引、查表替换：

```verilog
(* ramstyle = "logic" *)        // Quartus
(* ram_style = "distributed" *) // Vivado
reg [1:0] popcount2bits [0:3];
initial begin
    popcount2bits[0] = 2'd0;
    popcount2bits[1] = 2'd1;
    popcount2bits[2] = 2'd1;
    popcount2bits[3] = 2'd2;
end
```

见 [Population_Count.v:76-86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L76-L86)。两个 `ramstyle`/`ram_style` 属性是关键：这张小表必须被综合成 **LUT 逻辑**，不能被 CAD 随机推断成 Block RAM（注释提到作者见过这种随机翻车）。

**对计数与填充位宽**。算出有多少个位对、以及把 2 位对计数扩展到 `POPCOUNT_WIDTH` 需要补多少个 0，见 [Population_Count.v:96-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L96-L99)。`PAD_WIDTH` 在不需要填充时取一个「否则不可能」的最大值，留作后面的特殊分支处理。

**剥出首迭代**。第 0 个位对单独翻译并填充，作为累加链的起点，见 [Population_Count.v:128-138](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L128-L138)：

```verilog
paircount[0 +: 2]             = popcount2bits[word_in[0 +: 2]];
popcount[0 +: POPCOUNT_WIDTH] = {PAD, paircount[0 +: 2]};
```

注意 `verilator lint_off UNOPTFLAT`（[Population_Count.v:112-114](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L112-L114)）：`popcount` 数组前一项喂给后一项，linter 看上去像「组合环」，其实是按 `i` 严格递增的链，故告知 linter 忽略。

**加法链**。从 `i=1` 起，每个位对查表后与上一拍累加值相加，见 [Population_Count.v:144-155](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L144-L155)：

```verilog
for(i=1; i < PAIR_COUNT; i=i+1) begin : per_paircount
    paircount[2*i +: 2] = popcount2bits[word_in[2*i +: 2]];
    popcount[POPCOUNT_WIDTH*i +: POPCOUNT_WIDTH] =
        {PAD,paircount[2*i +: 2]} + popcount[POPCOUNT_WIDTH*(i-1) +: POPCOUNT_WIDTH];
end
```

**奇位宽收尾 + 取链尾**。若 `WORD_WIDTH` 为奇数，最高位不在任何位对里，单独加进最后一个累加值；最终输出就是链尾那个累加值，见 [Population_Count.v:160-166](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Population_Count.v#L160-L166)。

#### 4.1.4 代码实践

**实践目标**：实例化 `Population_Count`，验证它对一个 8 位字正确数出 1 的个数，并观察「输出位宽等于输入位宽」这一设计。

**操作步骤**（示例代码，非项目原有文件）。把下面的测试台存为 `tb_popcount.v`：

```verilog
`default_nettype none
module tb_popcount;
    localparam W = 8;
    reg  [W-1:0] word_in;
    wire [W-1:0] count_out;   // 注意：输出位宽与输入相同（=8），而非最小的 4 位

    Population_Count #(.WORD_WIDTH(W)) dut
      (.word_in(word_in), .count_out(count_out));

    initial begin
        word_in = 8'b1011_0011; #10;  // 5 个 1
        $display("popcount(10110011) = %0d (expect 5)", count_out);

        word_in = 8'b1111_1111; #10;  // 8 个 1
        $display("popcount(11111111) = %0d (expect 8)", count_out);

        word_in = 8'b0000_0000; #10;  // 0 个 1
        $display("popcount(00000000) = %0d (expect 0)", count_out);
        $finish;
    end
endmodule
```

用 Icarus Verilog 编译运行（仓库是扁平目录，依赖文件都在同目录，故用 `*.v` 一并送入，`-s` 指定顶层）：

```bash
iverilog -g2012 -s tb_popcount -o sim.vvp tb_popcount.v *.v
vvp sim.vvp
```

**需要观察的现象**：

- 三行输出分别是 `5`、`8`、`0`。
- `count_out` 是 8 位宽（与输入相同），但高位恒为 0——综合后这部分会被 CAD 删除，实际只需 4 位即可表示 0..8。

**预期结果**：输出与注释里的 expect 一致。若你无法运行仿真器，可标注「待本地验证」，并手动对 `8'b1011_0011` 数 1 的个数确认等于 5。

#### 4.1.5 小练习与答案

**练习 1**：32 位输入的 popcount，最少需要几位输出？加法链里有几个加法器？

> **答案**：最少 ⌈log₂(32+1)⌉ = 6 位（能表示 0..32）。32 位切成 16 个位对，剥出首迭代后加法链有 16−1 = 15 个加法器（注释原文也是 15 个 6 位加法器）。

**练习 2**：为什么作者坚持把 `POPCOUNT_WIDTH` 写成 `WORD_WIDTH` 而不是让用户传入窄位宽？

> **答案**：用户容易把 ⌈log₂(N+1)⌉ 错算成 ⌈log₂N⌉（少一位会丢掉「全部是 1」那个值）。留宽后，代码与精化原理图都更简单，多余的高位是恒 0，会被综合器自动优化掉，既不浪费逻辑，又消除了人为算错位宽的隐患。

---

### 4.2 尾零与首零计数 (ntz / nlz)

#### 4.2.1 概念说明

**Number of Trailing Zeros (ntz)**：从一个字的最低位（LSB）开始数，到第一个 1 为止，中间有几个连续的 0。全零输入则返回字宽本身。示例（取自源码注释 [Number_of_Trailing_Zeros.v:7-14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Number_of_Trailing_Zeros.v#L7-L14)）：

| 输入(5位) | ntz |
|-----------|-----|
| 11111 | 0 |
| 00010 | 1 |
| 01100 | 2 |
| 11000 | 3 |
| 10000 | 4 |
| 00000 | 5（= 字宽，全零特例）|

**Number of Leading Zeros (nlz)**：从最高位（MSB）开始数，到第一个 1 为止，中间有几个连续的 0。它在硬件上「免费」——把输入位反转后跑一遍 ntz 即可。

ntz/nlz 在软件里极其常见（`__builtin_ctz`/`__builtin_clz`）：计算对齐、找第一个空闲资源、浮点数正规化、除以 2 的幂、哈希分散等。硬件实现的关键洞察是：

> 一个孤立的 1 是 2 的幂，它的「位置」就是它的以 2 为底的对数；而这个对数值恰好等于「它后面有几个 0」。

于是 **ntz = 孤立最低位 1 + 取对数**。

#### 4.2.2 核心流程

**ntz 流程**：

```
1. lsb_1 = word_in & (-word_in)          // 孤立最低位 1（独热）
2. index = log2(lsb_1)                    // 独热位 → 它的位索引
3. 若 word_in 全零：ntz = WORD_WIDTH      // 对数未定义的特例
   否则        ：ntz = index
```

**nlz 流程**：

```
1. reversed = bit_reverse(word_in)        // MSB 与 LSB 互换
2. nlz = ntz(reversed)                    // 复用 ntz
```

位反转把「最左感兴趣的位」变成「最右感兴趣的位」，从而复用 ntz 那套「自右向左」的并行位操作（孤立最低位 1）。`Word_Reverser` 在精化期只是重新布线，零逻辑、零延迟代价。

#### 4.2.3 源码精读

**ntz 第一步：孤立最低位 1**。直接实例化 `Bitmask_Isolate_Rightmost_1_Bit`，见 [Number_of_Trailing_Zeros.v:35-45](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Number_of_Trailing_Zeros.v#L35-L45)。那个模块的核心就一行，见 [Bitmask_Isolate_Rightmost_1_Bit.v:28-30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Isolate_Rightmost_1_Bit.v#L28-L30)：`word_out = word_in & (-word_in);`。

**ntz 第二步：取对数得索引**。实例化 `Logarithm_of_Powers_of_Two`，把独热位转成它的位索引，见 [Number_of_Trailing_Zeros.v:53-62](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Number_of_Trailing_Zeros.v#L53-L62)。该模块对每个输入位预算出它的对数（位索引），用该位是否为 1 做门控，再 OR 归约——逻辑量随位宽线性增长，见 [Logarithm_of_Powers_of_Two.v:110-117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Logarithm_of_Powers_of_Two.v#L110-L117)。当输入全零，对数未定义，模块拉高 `logarithm_undefined`。

**ntz 第三步：全零特例**。全零时对数无意义，但 ntz 的正确答案是 `WORD_WIDTH`（一个对数永远取不到的值），用一个三元处理，见 [Number_of_Trailing_Zeros.v:64-70](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Number_of_Trailing_Zeros.v#L64-L70)：

```verilog
always @(*) begin
    word_out = (logarithm_undefined == 1'b1) ? WORD_WIDTH : trailing_zero_count_raw;
end
```

**nlz：位反转 + 复用 ntz**。先实例化 `Word_Reverser`（参数 `WORD_WIDTH=1, WORD_COUNT=WORD_WIDTH`，即逐位反转），再喂给一个 `Number_of_Trailing_Zeros`，见 [Number_of_Leading_Zeros.v:35-46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Number_of_Leading_Zeros.v#L35-L46) 与 [Number_of_Leading_Zeros.v:48-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Number_of_Leading_Zeros.v#L48-L56)。注意 `word_out` 在 nlz 里是 `output wire`（来自子实例），而 ntz 里是 `output reg`——这正对应 u2-l1 讲过的规则：输出来自子模块实例用 `wire`，来自本模块局部逻辑用 `reg`。

#### 4.2.4 代码实践

**实践目标**：跟踪 ntz 在 `word_in = 8'b0001_0100`（十进制 20）上的执行链，亲手算出结果 2。

**操作步骤**（源码阅读型实践，无需仿真）：

1. `word_in & (-word_in)`：`-word_in` 是两位补码取反加一。`0001_0100` → 取反 `1110_1011` → 加一 `1110_1100`。与原值相与：`0001_0100 & 1110_1100 = 0000_0100`。于是 `lsb_1 = 0000_0100`（孤立出 bit 2）。
2. `log2(0000_0100)`：唯一的 1 在第 2 位，对数值 = 2，即 `trailing_zero_count_raw = 2`。
3. 全零判断：`word_in` 非零，`logarithm_undefined = 0`。
4. 最终 `word_out = 2`。

**需要观察的现象**：ntz 结果 2，恰等于「最低位 1（bit 2）之后有几个 0」。

**预期结果**：手算 `0001_0100` 的 ntz = 2。再用同样方法验证注释里的样例：`01100` → 孤立 `00100` → log2 = 2 ✓；`00000` → 全零 → 返回 5 ✓。

> 进阶：用 nlz 验证 `01100`（5 位）。位反转变为 `00110`，ntz(00110)：孤立最低位 1 = `00010`，log2 = 1，故 nlz = 1（注释样例 `01100 --> 00001` ✓）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ntz 在全零输入时要返回 `WORD_WIDTH`，而不是 0？

> **答案**：0 是一个合法的 ntz 值（输入最低位就是 1 时 ntz=0，如 `11111`）。若全零也返回 0，就无法区分「最低位是 1」和「根本没有 1」。返回 `WORD_WIDTH`（对数永远取不到的值）让全零成为可识别的特例。这也是 `Logarithm_of_Powers_of_Two` 要单独拉一根 `logarithm_undefined` 线的原因——log₂0 未定义，不能用 0 占位。

**练习 2**：nlz 模块里没有任何 `always` 块、没有任何寄存器，它是怎么「算」出结果的？

> **答案**：nlz = `Word_Reverser`（精化期纯布线，零逻辑）+ `Number_of_Trailing_Zeros`（组合逻辑）。前者只是把线交叉连接，后者提供全部计算。nlz 本身只做实例化连线，所以 `word_out` 是 `output wire`。这是「模块即连线、模块即设计意图」的体现。

---

### 4.3 汉明距离与位投票 (Hamming Distance / Bit Voting)

#### 4.3.1 概念说明

**Hamming Distance（汉明距离）**：两个字之间「不同的位」的个数。把两个字逐位 XOR，不同的位变 1、相同的位变 0；再 popcount 这个 XOR 结果，就得到差异位数。它是编码理论、错误检测、DNA 序列比较、近似匹配的基础度量。

**Bit Voting（位投票）**：把一个字的每一位看作一张「选票」（1 = 赞成、0 = 反对），用 popcount 数出赞成票数，再与若干阈值比较，一次性给出五种互斥的投票结论：

- **unanimity_ones**：全票赞成（所有位都是 1）。
- **unanimity_zeros**：全票反对（所有位都是 0）。
- **majority**：多数赞成（赞成数过半；平票时为 0，全赞成时为 1）。
- **minority**：少数赞成（赞成数不足半数；平票时为 0，全反对时为 1）。
- **tie**：平票（仅当总票数为偶数时才可能出现）。

注释（[Bit_Voting.v:13-17](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Voting.v#L13-L17)）特别说明：每个输出单独有效，**不必组合多个输出来判断情形**。正因如此， unanimity 才拆成 ones/zeros 两路——否则你得看 majority 和 minority 才能反推是哪种全票。

#### 4.3.2 核心流程

**Hamming Distance**：

```
1. different_bits = word_A XOR word_B     // 逐位异或（Word_Reducer）
2. distance = popcount(different_bits)    // 数 1 的个数
```

**Bit Voting**（设总票数 = INPUT_COUNT = N）：

\[
\text{TIE} = \lfloor N/2 \rfloor,\quad \text{MAJORITY} = \lfloor N/2 \rfloor + 1,\quad \text{MINORITY} = N - \text{MAJORITY}
\]

```
popcount = 数赞成票
unanimity_zeros = (popcount == 0)
unanimity_ones  = (popcount == N)
majority        = (popcount >= MAJORITY)
minority        = (popcount <= MINORITY)
tie             = (popcount == TIE) AND (N 是偶数)
```

阈值在精化期由 `localparam` 算好（[Bit_Voting.v:59-64](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Voting.v#L59-L64)）。`tie` 多乘一个 `COUNT_IS_EVEN` 常量：当 N 为奇数时，这个「与」把整路 tie 逻辑折叠成恒 0，省掉对应的比较器——又一个 lean on CAD 的例子。

> 验证阈值（N=2k 偶数）：TIE=k, MAJORITY=k+1, MINORITY=2k−(k+1)=k−1。于是 majority = (popcount ≥ k+1) 即「严格过半」；minority = (popcount ≤ k−1) 即「严格不足半」；tie = (popcount == k)。三者互斥且与两个 unanimity 互补，五个输出恰好覆盖所有情况。

#### 4.3.3 源码精读

**Hamming_Distance：XOR + popcount**。用 `Word_Reducer`（`OPERATION="XOR"`, `WORD_COUNT=2`）把拼好的 `{word_A, word_B}` 逐位异或，再用 `Population_Count` 数 1，见 [Hamming_Distance.v:20-32](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Hamming_Distance.v#L20-L32) 与 [Hamming_Distance.v:34-42](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Hamming_Distance.v#L34-L42)。整个模块没有任何 `always` 块，纯靠实例化两个已测试的子模块拼出来——`distance` 因来自子实例而是 `output wire`。注释指出输出位宽留作 `WORD_WIDTH`，综合后只用最低 ⌊log₂N⌋+1 位。

**Bit_Voting：popcount + 比较**。先实例化 `Population_Count` 数赞成票，见 [Bit_Voting.v:71-81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Voting.v#L71-L81)。阈值比较在一个组合 `always` 块里完成，见 [Bit_Voting.v:88-94](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Voting.v#L88-L94)：

```verilog
always @(*) begin
    unanimity_zeros = (popcount == UNANIMITY_ZEROS);
    unanimity_ones  = (popcount == UNANIMITY_ONES);
    majority        = (popcount >= MAJORITY);
    minority        = (popcount <= MINORITY);
    tie             = (popcount == TIE) && (COUNT_IS_EVEN == 1'b1);
end
```

注意阈值比较全部写成**等式/不等式比较**（`(popcount == TIE)`），而非按位运算——这承接 u3-l1 的布尔写法规范，且 `COUNT_IS_EVEN` 是精化期常量，奇数 N 时 `tie` 整路被常量传播消除。

#### 4.3.4 代码实践

**实践目标**：用 `Hamming_Distance` 比较两个 8 位字，亲手对照「手算 XOR 后数 1」与模块输出。

**操作步骤**。扩展 4.1.4 的测试台，加入 `Hamming_Distance` 实例（示例代码）：

```verilog
`default_nettype none
module tb_hamming;
    localparam W = 8;
    reg  [W-1:0] a, b;
    wire [W-1:0] dist;

    Hamming_Distance #(.WORD_WIDTH(W)) dut
      (.word_A(a), .word_B(b), .distance(dist));

    initial begin
        a = 8'b1010_1100; b = 8'b1010_1010; #10;  // XOR=0000_0110 -> 2 个 1
        $display("hamming(10101100,10101010) = %0d (expect 2)", dist);

        a = 8'b1010_1010; b = 8'b0101_0101; #10;  // XOR=1111_1111 -> 8 个 1
        $display("hamming(10101010,01010101) = %0d (expect 8)", dist);

        a = 8'b1010_1010; b = 8'b1010_1010; #10;  // XOR=0000_0000 -> 0 个 1
        $display("hamming(10101010,10101010) = %0d (expect 0)", dist);
        $finish;
    end
endmodule
```

编译运行：

```bash
iverilog -g2012 -s tb_hamming -o sim.vvp tb_hamming.v *.v
vvp sim.vvp
```

**需要观察的现象**：三行输出依次为 `2`、`8`、`0`。手算 `1010_1100 XOR 1010_1010 = 0000_0110`，数其中 1 的个数 = 2，与模块输出一致。

**预期结果**：输出与 expect 一致。无法运行时标注「待本地验证」，并用手算 XOR + 数 1 的方式核验。

**延伸**：把 `Hamming_Distance` 换成 `Bit_Voting`（参数 `INPUT_COUNT=8`），输入 `8'b1111_0000`（4 票赞成，N=8 偶数），应看到 `tie=1`、`majority=0`、`minority=0`、两个 unanimity 均为 0；输入 `8'b1111_1111` 应看到 `unanimity_ones=1`、其余为 0。

#### 4.3.5 小练习与答案

**练习 1**：`Hamming_Distance` 里为什么用 `Word_Reducer` 做 XOR，而不是直接写 `assign different_bits = word_A ^ word_B;`？

> **答案**：两者综合结果等价（`Word_Reducer` 在 `WORD_COUNT=2` 时就是逐位异或）。用 `Word_Reducer` 体现本书「把布尔运算做成已测试模块、主体只做连线」的构建块库哲学（见 u5-l1）：运算语义集中在 `Bit_Reducer`/`Word_Reducer` 一处，主体代码不改、可读性更好。当然，直接写 `^` 也完全正确，这里更多是风格选择。

**练习 2**：N=7（奇数个投票位）时，`tie` 输出会是什么？为什么？

> **答案**：恒为 0。N 为奇数时 `COUNT_IS_EVEN = 0`，`(popcount == TIE) && (COUNT_IS_EVEN == 1'b1)` 整路被常量传播折叠成常量 0，对应的比较器逻辑被综合器删除。奇数个位永远不可能平票，所以这个输出在奇数 N 下本就无意义，模块用精化期常量把它「编没了」。

---

## 5. 综合实践

**任务**：用本讲三个母模块组装一个「接收字完整性检查器」，把 popcount、hamming、ntz 串成一条诊断流水线。

**需求**：给定一个 8 位的接收字 `rx_word` 和一个 8 位的期望字 `expected`，组合输出：

1. `parity_odd`：`rx_word` 中 1 的个数为奇数（用 `Population_Count` 取最低位即可）。
2. `error_count`：`rx_word` 与 `expected` 的汉明距离（用 `Hamming_Distance`）。
3. `first_error_index`：第一个出错位的位置——对两者的 XOR 掩码取 ntz（用 `Number_of_Trailing_Zeros`）。当 `error_count == 0` 时该值应为字宽 8（无错误）。

**设计要点（示例结构，非项目原有代码）**：

```verilog
// 1. 奇偶校验：popcount 的最低位即奇偶标志
Population_Count #(.WORD_WIDTH(8)) count_ones
  (.word_in(rx_word), .count_out(rx_popcount));
assign parity_odd = rx_popcount[0];

// 2. 错误位数
Hamming_Distance #(.WORD_WIDTH(8)) count_errors
  (.word_A(rx_word), .word_B(expected), .distance(error_count));

// 3. 首个错误位：先 XOR 出差异掩码，再 ntz
assign error_mask = rx_word ^ expected;
Number_of_Trailing_Zeros #(.WORD_WIDTH(8)) find_first
  (.word_in(error_mask), .word_out(first_error_index));
```

**验证用例**（请自行手算或仿真核对）：

| rx_word | expected | error_mask | parity_odd | error_count | first_error_index |
|---------|----------|------------|------------|-------------|-------------------|
| 1010_1100 | 1010_1010 | 0000_0110 | 0(共4个1,偶) | 2 | 1 |
| 1111_1111 | 1111_1111 | 0000_0000 | 0(共8个1,偶) | 0 | 8(全零特例) |
| 0000_0001 | 0000_0000 | 0000_0001 | 1(共1个1,奇) | 1 | 0 |

> 这个练习把本讲三块内容连起来：popcount 做奇偶（4.1）、hamming 做差异计数（4.3）、ntz 做故障定位（4.2）。注意第 3 行 `first_error_index = 0`，因为唯一的出错位就在 bit 0——这正是 ntz「孤立最低位 1 再取对数」的直接结果。第一行的 `error_mask = 0000_0110`，最低位 1 在 bit 1，故 ntz = 1。

**待本地验证**：若你有 Icarus Verilog，按 4.1.4 的编译方式把上述结构连同测试用例写成测试台运行；若无，至少完成表格里的手算。

## 6. 本讲小结

- **popcount 是母模块**：用「位对查表（00/01/10/11 → 0/1/1/2）+ 加法链 + 奇位宽收尾」实现，输出位宽故意留成输入位宽，靠 CAD 收回冗余；小查表必须用 `ramstyle="logic"` 防止被推断成 BRAM。
- **ntz = 孤立最低位 1 + 取对数**：`x & (-x)` 给出独热位，`Logarithm_of_Powers_of_Two` 给出它的索引即尾零数；全零输入是对数未定义的特例，返回字宽本身。
- **nlz 几乎免费**：位反转（`Word_Reverser`，精化期纯布线）+ 复用 ntz，零额外逻辑。
- **Hamming_Distance = XOR + popcount**：逐位异或找出不同位，再数 1。
- **Bit_Voting = popcount + 阈值比较**：精化期算好全票/多数/少数/平票阈值，一次性给出五个互斥结论；奇数票时 `tie` 整路被常量消除。
- **共性哲学**：五个模块都用「整数语义看位集合」的思路，把布尔/掩码运算封装成已测试的构建块，主体只做实例化与连线，并把「够宽但恒定」的部分交给 CAD 折叠。

## 7. 下一步学习建议

- 下一讲 **u16-l2 Bitmask 位操作库** 将深入 `Bitmask_Isolate_Rightmost_1_Bit`（本讲 ntz 的基石）、`Bitmask_Next_with_Constant_Popcount_ntz`、`Bitmask_Thermometer_from_Count` 等「位操作惯用法的电路实现」，可直接看作本讲 ntz 那条 `x & (-x)` 技巧的扩展。
- 回顾 **u11-l1 优先编码与仲裁器**：那里的 `Priority_Encoder` 同样建立在「孤立最低位 1」之上，与本讲 ntz 共享同一块基石，对照阅读能加深理解。
- 若对 popcount 的进位链/树形结构感兴趣，可阅读 `Adder_Subtractor_Binary.v`（u8-l1）了解 FPGA 专用进位链如何被推断。
- 动手方向：试着用 `Population_Count` + `Arithmetic_Predicates_Binary`（u8-l1）实现一个「当 1 的个数超过阈值时报警」的比较器，体会 popcount 把掩码映射成整数后用算术描述性质的力量。
