# 计数、移位与算术

> 单元 u3 · 讲义 u3-l3
> 依赖：u2-l2（时序原语：触发器家族）
> 适用对象：已掌握 stdlib 触发器、`generate if(SYN=="TRUE")` 双实现骨架的读者

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂并参数化 `oh_counter`，理解它如何用「补码加法 + 进位」实现增/减计数与回绕；
- 区分三类算术原语：进位传播加法器（`oh_add`）、乘法器（`oh_mult`）、桶形移位器（`oh_shift`）；
- 理解**进位保存加法（Carry-Save Adder，CSA）**为什么能加速多操作数求和，并用 `oh_csa32` / `oh_csa42` 搭出压缩树；
- 知道 `oh_parity` 与 `oh_lfsr` 的用途，尤其是 LFSR 作为测试激励源的原理；
- 用 `oh_csa32 + oh_add` 拼出「三个 32 位数求和」的完整数据通路并仿真验证。

## 2. 前置知识

本讲是**数据通路（datapath）**的入门。数据通路是 CPU/DSP/DMA 里专门做数值运算的部分，和「控制通路」相对。在继续前，请确认你理解以下概念（若不熟，先回看 u2-l2）：

- **组合逻辑与时序逻辑**：本讲的 `oh_add` / `oh_mult` / `oh_shift` / `oh_csa32` / `oh_parity` 都是纯组合逻辑（输出只取决于当前输入）；`oh_counter` 与 `oh_lfsr` 是时序逻辑（带 `clk`，内部有寄存器）。
- **非阻塞赋值 `<=`**：时序逻辑里的寄存器更新都用 `<=`。
- **参数化与 soft/hard 双实现**：stdlib 原语用 `#(parameter N=...)` 调位宽，用 `SYN` / `TYPE` 两个字符串参数配合 `generate if(SYN=="TRUE")` 在可综合 RTL（soft）与 ASIC 硬核（hard）之间切换。本讲的 hard 分支大多引用了仓库里并未定义/已脱节的 `asic_*` 单元，属于占位实现，**学习请一律以 `SYN=="TRUE"` 的 soft 分支为准**（这点与 u2-l2、u3-l1 的结论一致）。

两个本讲会用到的数学约定：

- 二进制加法的进位传播：两个 N 位数相加，结果最多 N+1 位，最高位的进位叫 `cout`。
- 二进制乘法：两个 N 位数相乘，结果最多 2N 位。
- 模 2 运算：异或 `^` 就是「不进位的加法」，是校验、LFSR、CSA 的数学基础。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `stdlib/rtl/` 下，全部是可综合 RTL 原语：

| 文件 | 类型 | 作用 |
|------|------|------|
| `oh_counter.v` | 时序 | 可参数化的增/减计数器，支持装载、回绕 |
| `oh_add.v` | 组合 | 二进制进位传播加法器（CPA） |
| `oh_mult.v` | 组合 | 二进制乘法器（带可选符号扩展） |
| `oh_shift.v` | 组合 | 桶形移位器（左/右、算术/逻辑） |
| `oh_csa32.v` | 组合 | 3:2 进位保存加法器（一位全加器） |
| `oh_csa42.v` | 组合 | 4:2 压缩器（两个 csa32 拼成，带级联进位） |
| `oh_parity.v` | 组合 | 奇偶校验（异或归约） |
| `oh_lfsr.v` | 时序 | 伽罗瓦线性反馈移位寄存器（伪随机） |

辅助文件：

| 文件 | 作用 |
|------|------|
| `oh_csa62.v` | 6:2 压缩器，演示如何用 csa32/csa42 搭更宽的压缩树 |
| `stdlib/testbench/tb_oh_lfsr.v` | oh_lfsr 的标准 testbench 包装 |

> 一个重要事实：本讲的算术原语**没有被 stdlib 内其他模块实例化**（在 `stdlib/rtl`、`elink`、`gpio`、`edma` 里都搜不到对它们的引用），它们是供上层设计调用的「积木」。只有 `oh_lfsr` 有配套 testbench。因此本讲的实践以「自己写小程序调用原语 + iverilog 仿真」为主。

## 4. 核心概念与源码讲解

### 4.1 可参数化计数器 oh_counter

#### 4.1.1 概念说明

计数器是数字电路里出现频率最高的时序部件之一：分频、地址发生、状态机节拍、超时检测都要用它。`oh_counter` 想用一个模块覆盖常见需求：

- 位宽可参数化（`N`）；
- 既能加也能减（`dec`）；
- 可以从指定值开始（`load` / `load_data`）；
- 减到 0 或加到全 1 时给出 `wraparound` 信号，并可选自动回绕（`autowrap`）。

它的核心思想很巧妙：**把「加 1 / 减 1」统一成一个加法**。减 1 等价于「加上 -1 的补码」，于是增/减计数可以共用同一个加法器，只是加数不同。

#### 4.1.2 核心流程

1. 根据 `dec` 把 1 位输入 `in` 变换成 N 位加数 `inb` 与进位 `cin`：
   - 加计数：`inb = in`，`cin = 0`；
   - 减计数：用补码，即 `inb = ~in`（再补一串高位 1），`cin = 1`。
2. 用一个加法器算 `count_in = count + inb (+ cin)`。
3. 每个 `clk` 上升沿：若 `load` 则装入 `load_data`；否则若 `en` 且未越界，则把 `count_in` 写入 `count`。
4. `wraparound` 在「减到 0」或「加到全 1」时拉高；若 `autowrap=0` 则到了边界后停摆（不再自增/自减）。

#### 4.1.3 源码精读

端口定义清晰列出了一组控制位与时钟/输出：

[stdlib/rtl/oh_counter.v:L8-L25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L8-L25) —— `oh_counter` 的端口：`clk/in/en/dec/autowrap/load/load_data` 为输入，`count` 与 `wraparound` 为输出。注意 `count` 是 `output reg`，因为它在 `always` 块里被赋值。

增/减的「补码预处理」：

[stdlib/rtl/oh_counter.v:L30-L32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L30-L32) —— 把 1 位 `in` 扩展为 N 位加数 `inb`：加计数时高位补 0，减计数时高位补 1（构成补码），最低位取 `~in`；同时 `cin` 在减计数时置 1，用来把补码凑成真正的「-1」。

时序更新主体：

[stdlib/rtl/oh_counter.v:L35-L39](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L35-L39) —— 时钟上升沿：`load` 优先级最高；否则在 `en` 有效且「未到边界或允许自动回绕」时更新。`wraparound & ~autowrap` 正是「到了边界但不让回绕」的停摆条件。

边界检测用归约运算符 `|`（或归约）与 `&`（与归约）：

[stdlib/rtl/oh_counter.v:L41-L42](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L41-L42) —— `~(|count)` 判全 0（减到 0），`(&count)` 判全 1（加到最大），两者都在 `en` 时输出 `wraparound`。

加法器的 soft/hard 双实现：

[stdlib/rtl/oh_counter.v:L45-L62](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L45-L62) —— soft 分支用一句 `count + inb` 完成加法；hard 分支例化 `asic_add`（仓库未定义，为占位）。

> ⚠️ **读源码须注意（以源码为准）**：soft 分支的 `count_in = count + inb` **没有加上** `cin`。按 4.1.2 的设计意图，减计数需要 `inb + cin` 才是正确的「-1」。因此 soft 分支在 `dec=1, in=1` 时的步长与加计数不一致，建议**用仿真确认**你所用参数下的实际行为。此外 hard 分支里出现的 `cout_in` / `in_mux` 是未声明的标识符（应为 `count_in` / `inb`），属历史遗留，无法直接综合。结论：把 `oh_counter` 当作「加计数 + 边界检测」的原语来学习，减计数路径先用仿真验证再使用。

#### 4.1.4 代码实践

**目标**：把 `oh_counter` 配置成一个「模 4 加计数器」，观察 `wraparound` 的行为。

**操作步骤**（源码阅读型 + 仿真型）：

1. 阅读上面三段源码，回答：要让计数器在 0→1→2→3→0 循环，`N`、`dec`、`autowrap` 应分别取什么？
2. 写一个最小 testbench（**示例代码**，不是项目原有文件）：

```verilog
// 示例代码：tb_counter.v —— 验证 oh_counter 模 4 加计数
module tb;
  reg clk=0, en, dec, autowrap, load;
  reg [1:0] load_data;
  wire [1:0] count;
  wire wraparound;

  oh_counter #(.N(2), .SYN("TRUE")) dut (
    .clk(clk), .in(1'b1), .en(en), .dec(dec),
    .autowrap(autowrap), .load(load), .load_data(load_data),
    .count(count), .wraparound(wraparound));

  always #5 clk = ~clk;
  initial begin
    en=1; dec=0; autowrap=1; load=0; load_data=0;
    #50 $finish;
  end
  initial $monitor("t=%0t count=%0d wrap=%0d", $time, count, wraparound);
endmodule
```

3. 编译运行（需让 iverilog 找到 `oh_counter.v`）：
   ```bash
   iverilog -g2005 -y stdlib/rtl -o sim tb_counter.v
   vvp sim
   ```

**预期现象**：`count` 在 0→1→2→3→0 之间循环；每次从 3 回到 0 的那一拍 `wraparound=1`。

**待本地验证**：若你把 `autowrap` 改成 0，`count` 应在 3 处停住，`wraparound` 持续为 1。请实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：`wraparound` 的两个归约条件里，为什么减计数用 `~(|count)`、加计数用 `(&count)`？

> **答案**：`(|count)` 为真表示 `count` 至少有一位是 1，取反后为真即「全 0」；`(&count)` 为真表示所有位都是 1，即「最大值」。这正是减到 0、加到满的两个边界。

**练习 2**：如果想让计数器从 5 开始递增，应置哪些信号？

> **答案**：先把 `load=1`、`load_data=5` 给一拍，让 `count` 装入 5；之后 `load=0`、`en=1`、`dec=0` 即可从 5 递增。

---

### 4.2 算术原语：加法、乘法、移位

#### 4.2.1 概念说明

这三个原语覆盖了数据通路里最基础的三类运算：

- **进位传播加法器（Carry-Propagate Adder，CPA）`oh_add`**：就是我们纸笔做加法的方式——从最低位起，逐位相加并把进位向高位传。结果是确定的二进制数。它的延迟随位宽线性增长（行波进位）或对数增长（超前进位），是「最终把数算出来」的加法器。
- **乘法器 `oh_mult`**：把两个 N 位数相乘得 2N 位积。soft 实现直接用 Verilog 的 `*`，让综合工具自行推断；可配置是否按有符号数处理。
- **桶形移位器 `oh_shift`**：能在一个组合级里把数据左移或右移任意位（由 `shamt` 指定），右移可选算术（高位补符号位）或逻辑（高位补 0）。名字来自「像桶一样一级一级转」的多级选择结构。

#### 4.2.2 核心流程

**加法** `oh_add`：

\[ \text{sum} = a + b + c_{in},\qquad c_{out} = \text{最高位进位} \]

结果 `{cout, sum}` 共 N+1 位。`k` 是「进位扼杀（carry kill）」信号，用于 hard 实现里强制某些位不产生进位（soft 分支未用）。

**乘法** `oh_mult`：

\[ \text{product} = a \times b \quad(\text{宽度 } 2N) \]

若 `asigned=1` 且 `a` 的最高位为 1，则先把 `a` 符号扩展一位再相乘，`bsigned` 同理，从而正确处理负数。

**移位** `oh_shift`：设位宽 `N`，移位量位宽 `S = \lceil \log_2 N \rceil`（即 `$clog2(N)`）。

- 左移：`out = in << shamt`（低位补 0）；
- 逻辑右移：`out = in >> shamt`（高位补 0）；
- 算术右移：先把 `in` 符号扩展到 2N 位，再右移，等效于高位补符号位。

#### 4.2.3 源码精读

`oh_add` 的 soft 分支极其简洁：

[stdlib/rtl/oh_add.v:L24-L29](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_add.v#L24-L29) —— `{cout, sum} = a + b + cin`：把进位输出和和数拼成 N+1 位一次性算出。

> ⚠️ **以源码为准**：soft 分支里 `carry`（完整进位向量）被写死成 `'b0`，旁边有 `//TODO: FIX` 注释；`k`（carry kill）在 soft 分支未参与运算。所以**实际可用的只有 `sum` 与 `cout`**，`carry` 输出请勿依赖。hard 分支才提供逐位 `carry` 与 `k` 控制（`asic_add` 仓库未定义）。

`oh_mult` 的符号扩展：

[stdlib/rtl/oh_mult.v:L25-L31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mult.v#L25-L31) —— `a_sext = asigned & a[N-1]`：仅当「声明为有符号」且「最高位确为 1」时才补一个符号位 1；否则补 0。两个操作数各自符号扩展后用 `$signed(...) * $signed(...)` 相乘。

> ⚠️ **以源码为准**：soft 分支**只驱动了 `product`**，`sum` 与 `carry` 两个输出端口未驱动（它们是给 hard 实现暴露部分积用的）。使用时只取 `product` 即可。

`oh_shift` 的算术右移技巧：

[stdlib/rtl/oh_shift.v:L28-L37](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_shift.v#L28-L37) —— `shift_in = arithmetic & in[N-1]` 决定高位补 0 还是补符号位；把 `in` 符号扩展成 2N 位的 `in_sext` 再右移 `shamt`，取低 N 位即得算术右移结果。左移则直接 `in << shamt`。最后用 `right` 在两者间二选一。

> ⚠️ **以源码为准**：hard 分支例化 `asic_shift` 时传了 `.TYPE(TYPE)`，但 `oh_shift` 的参数表只有 `N/S/SYN`，并没有 `TYPE`（见 [stdlib/rtl/oh_shift.v:L8-L12](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_shift.v#L8-L12)）。hard 分支无法直接编译，学习以 soft 分支为准。

#### 4.2.4 代码实践

**目标**：观察 `oh_mult` 有符号/无符号的区别。

**操作步骤**（源码阅读型，**待本地验证**）：

1. 设 `N=4`，`a=4'b1111`、`b=4'b0001`。
2. 先令 `asigned=0, bsigned=0`：`a` 被当成 15，`product = 15 * 1 = 15 = 32'h0000_000F`。
3. 再令 `asigned=1, bsigned=1`：`a` 被当成 -1，`product = -1 * 1 = -1 = 32'hFFFF_FFFF`。
4. 写一个最小 testbench 例化 `oh_mult`，分别打印两种配置下 `product` 的值。

**预期结果**：同一组输入，仅改变 `asigned/bsigned`，`product` 在 `0x0F` 与 `0xFFFFFFFF`（符号扩展到 8 位）之间切换。

#### 4.2.5 小练习与答案

**练习 1**：`oh_add` 的 `cout` 和 `sum` 拼起来为什么是 N+1 位？

> **答案**：两个 N 位数相加最多产生 N+1 位结果，最高位即进位输出 `cout`，低 N 位是 `sum`，所以 `{cout, sum}` 共 N+1 位。

**练习 2**：`oh_shift` 的 `shamt` 位宽为什么是 `$clog2(N)`？

> **答案**：要对 N 位数据移位，移位量取值范围是 0～N-1，共 N 种，表示它正好需要 `\lceil\log_2 N\rceil` 位，即 `$clog2(N)`。

---

### 4.3 进位保存加法与多操作数压缩：oh_csa32 / oh_csa42

#### 4.3.1 概念说明

这是本讲最有思想深度的一节。问题来自：**要把 K 个数加起来，怎么算最快？**

朴素做法：用 CPA（`oh_add`）依次累加，K 个数需要 K-1 次串行加法，每次都要等进位从最低位传到最高位，延迟很长。

观察：一位全加器（Full Adder）其实是一个 **3:2 压缩器**——输入同列的 3 个比特 `(in0, in1, in2)`，输出一个本列的「和位」`s` 和一个进到下一列的「进位」`c`：

\[
s = in_0 \oplus in_1 \oplus in_2,\qquad
c = in_0 in_1 + in_1 in_2 + in_2 in_0
\]

关键在于：**这个 c 不马上往高位传播**，而是存进一个「进位向量」。于是三个数相加的结果可以用两个向量 `(S, C)` 表示，其数值为：

\[
\text{value} = S + (C \ll 1)
\]

这种「不进位、只保存」的表示叫 **进位保存表示（carry-save form）**。把三个数压成 `(S,C)` 只花一级全加器的时间（每一位独立运算，没有横向进位），与位宽无关。要继续加第四个数，再把第四个数与 `(S,C)` 一起喂给一个 csa32 即可，仍是 O(1) 列延迟。

只有到最后**真的需要一个普通二进制数**时，才用一次 CPA（`oh_add`）把 `(S, C)` 合并——这一步叫 **vector merging add**。这就是 **Wallace/Daddara 树**的核心：用 CSA 树把多操作数压缩到 2 个，再接一个最终加法器。乘法器、FIR 滤波器、点积硬件都用这个套路。

`oh_csa42`（4:2 压缩器）把 4 个输入压成 `(S, C)`，内部用两个 csa32 实现，并多了 `cin/cout` 用于相邻列之间的级联进位（便于流水线）。

#### 4.3.2 核心流程

**oh_csa32（3:2）**：逐位独立做一位全加，输出 `s[N-1:0]` 与 `c[N-1:0]`，满足 `in0 + in1 + in2 = s + 2*c`（把 c 左移一位相加）。

**oh_csa42（4:2）**：

1. 第一级 csa32：`fa0(in0,in1,in2) → (sum_int, carry_int)`；
2. 第二级 csa32：`fa1(in3, sum_int, carry_int) → (s, c)`；
3. `cin`/`cout` 把第一级最高位的进位交给相邻列，使多个 csa42 能横向串成压缩树。

最终结果仍满足 `in0+in1+in2+in3 + (cin 的贡献) = s + 2*c + (cout 的贡献)`。

#### 4.3.3 源码精读

`oh_csa32` 是本讲最干净的模块——soft 分支就是一位全加器的逐位广播：

[stdlib/rtl/oh_csa32.v:L20-L27](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_csa32.v#L20-L27) —— `s` 是三输入异或（模 2 和），`c` 是三对的「与」再「或」（多数表决），正是标准一位全加器。N 位就是把这一位逻辑同时作用在每一列上，**没有任何横向进位**——这正是它快的原因。

`oh_csa42` 用两个 csa32 拼出 4:2 压缩：

[stdlib/rtl/oh_csa42.v:L23-L51](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_csa42.v#L23-L51) —— `carry_int[N:0]` 是 N+1 位的进位链：`carry_int[0]=cin`，`carry_int[N]=cout`。第一级 `fa0` 产生中间和 `sum_int` 与进位 `carry_int[N:1]`；第二级 `fa1` 把 `in3`、`sum_int` 与 `carry_int[N-1:0]` 再压一次，得到最终的 `s` 与 `c`。

把 csa32/csa42 进一步组合，可以搭出更宽的压缩器，仓库里的 `oh_csa62` 就是个现成例子：

[stdlib/rtl/oh_csa62.v:L32-L60](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_csa62.v#L32-L60) —— 6:2 压缩器 = 两个 `oh_csa32`（各压三个输入）+ 一个 `oh_csa42`（把两个中间结果和进位再压一次）。这就是压缩树的搭建方式。

> ⚠️ **以源码为准**：`oh_csa42` 的 hard 分支（[L52-L75](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_csa42.v#L52-L75)）缺少 `genvar` 声明、引用了未声明的 `carry`，无法直接编译；soft 分支完全可用，学习以 soft 分支为准。

#### 4.3.4 代码实践（本讲主实践）

**目标**：用 `oh_csa32` 做三个 32 位数求和的第一级压缩，再用 `oh_add` 做最终的进位传播加法，得到完整结果并自校验。

**原理**：`A + B + C` 最多需要 34 位（因为 `3×(2^{32}-1) < 2^{34}`）。
- 第一级 `oh_csa32`（N=32）：输入 `A/B/C`，输出和向量 `s[31:0]` 与进位向量 `cr[31:0]`，此时 `A+B+C = s + (cr<<1)`，但还分布在两个向量里。
- 第二级 `oh_add`（N=33）：把 `s` 零扩展到 33 位，把 `cr` 左移一位成 33 位，二者相加，`{cout, sum}` 即 34 位最终结果。

**操作步骤**：

1. 新建 `sum3.v`（**示例代码**）：

```verilog
// 示例代码：sum3.v —— 三个 32 位无符号数求和（csa32 压缩 + add 合并）
module sum3 #(parameter N = 32) (
    input  [N-1:0]  a, b, c,
    output [N+1:0]  sum        // 结果最多 N+2 = 34 位
);
    wire [N-1:0] s;             // CSA 和向量
    wire [N-1:0] cr;            // CSA 进位向量

    // 第一级：3:2 压缩，每一位独立，无横向进位
    oh_csa32 #(.N(N), .SYN("TRUE")) u_csa (
        .in0(a), .in1(b), .in2(c),
        .s(s),   .c(cr));

    // 第二级：进位传播加法（CPA），把 s 与 (cr<<1) 合并
    // a = {0, s}   （33 位）  b = {cr, 0}  （33 位 = cr 左移 1）
    oh_add #(.N(N+1), .SYN("TRUE")) u_add (
        .a   ({1'b0, s}),
        .b   ({cr, 1'b0}),
        .k   ({(N+1){1'b0}}),
        .cin (1'b0),
        .sum (sum[N:0]),         // 低 33 位
        .cout(sum[N+1]),         // 第 34 位
        .carry());               // soft 分支恒为 0，悬空即可
endmodule
```

2. 再写一个自校验 testbench（**示例代码**）：

```verilog
// 示例代码：tb_sum3.v —— 用 $random 产生激励并和软件参考值比对
module tb;
    reg  [31:0] a, b, c;
    wire [33:0] sum;
    integer i, errors;

    sum3 dut(.a(a), .b(b), .c(c), .sum(sum));

    initial begin
        errors = 0;
        for (i = 0; i < 100; i = i + 1) begin
            a = {$random}; b = {$random}; c = {$random};
            #1;
            if (sum !== (a + b + c)) begin
                $display("FAIL: a=%0d b=%0d c=%0d dut=%0d ref=%0d",
                         a, b, c, sum, a+b+c);
                errors = errors + 1;
            end
        end
        $display("DONE, errors=%0d", errors);
        $finish;
    end
endmodule
```

3. 编译运行（`-y stdlib/rtl` 让 iverilog 按文件名找到 `oh_csa32.v`、`oh_add.v`）：
   ```bash
   iverilog -g2005 -y stdlib/rtl -o sim tb_sum3.v sum3.v
   vvp sim
   ```

**预期结果**：输出 `DONE, errors=0`。

**待本地验证**：如果你把 `oh_add` 的位宽从 `N+1` 改回 `N`（即不给 `cr<<1` 留出最高进位位），观察 `errors` 是否变大、在哪些输入下出错——这能直观体会「进位向量需要左移一位、且需要多一位宽」的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 csa32「每位独立、与位宽无关」？

> **答案**：csa32 的每一位 `s[i]` 和 `c[i]` 只依赖 `in0[i]/in1[i]/in2[i]`，没有任何一位向相邻位的进位传播。因此无论 N 是 8 还是 128，单级 csa32 的列延迟都是固定的一级全加器延迟。

**练习 2**：把三个 32 位数压成 `(s, cr)` 后，为什么最终结果有 34 位而不是 33 位？

> **答案**：`3×(2^{32}-1) = 2^{33} + 2^{32} - 3`，介于 `2^{33}` 与 `2^{34}` 之间，所以需要 34 位才能无溢出表示。

**练习 3**：如果要一次压 4 个数，用 `oh_csa42` 会比「先 csa32 压三个、再 add 加第四个」好在哪里？

> **答案**：`oh_csa42` 把四个数压成 `(s,c)` 仍只需两级全加器延迟，且保持进位保存形式，可以继续接下一级 CSA；而「csa32 + add」中的 `add` 是进位传播加法，延迟大且破坏了进位保存形式，不利于进一步压缩。

---

### 4.4 奇偶校验与伪随机：oh_parity / oh_lfsr

#### 4.4.1 概念说明

这两个小原语经常用在**测试与可靠性**场景：

- **奇偶校验 `oh_parity`**：对一段数据做异或归约，得到 1 位校验位。偶校验下，数据和校验位一起含偶数个 1；用于检测单比特错误。
- **线性反馈移位寄存器 `oh_lfsr`**：用很少的硬件就能产生周期很长、统计特性接近随机的比特序列，俗称「伪随机」。LFSR 是硬件测试（产生激励）、加扰、BIST（内建自测试）里最常用的廉价随机源。

`oh_lfsr` 采用 **Galois 结构**：状态每拍右移一位，最低位作为反馈位；若反馈位为 1，则把一个由 `taps` 指定的多项式模式异或回状态。选择「最大长度多项式」时，周期可达：

\[
T = 2^N - 1
\]

即遍历除全 0 外的所有 N 位状态。种子（seed）必须非零。

#### 4.4.2 核心流程

**oh_parity**：`out = ^in`（异或归约）。

**oh_lfsr（Galois）**，每拍 `en` 有效时：

\[
\text{out}_{\text{next}} = (\{N\{out[0]\}\}\ \&\ \text{taps}) \oplus (\text{out} \gg 1)
\]

即：把 `out` 右移一位；如果移出去的最低位 `out[0]` 是 1，就用 `taps` 异或回高位。复位时装入 `seed`。

#### 4.4.3 源码精读

`oh_lfsr` 的状态更新只有两行，但浓缩了 Galois LFSR 的全部数学：

[stdlib/rtl/oh_lfsr.v:L73-L81](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_lfsr.v#L73-L81) —— 复位时装 `seed`；`en` 时执行 `({(N){out[0]}} & taps) ^ (out >> 1)`。`{(N){out[0]}}` 把反馈位广播成 N 位掩码，与 `taps` 相与后只保留反馈位为 1 时该拍要翻转的位，再与右移后的状态异或。

文件头给出了一张最大长度多项式表（N 从 4 到 64），直接可用作 `taps`：

[stdlib/rtl/oh_lfsr.v:L22-L57](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_lfsr.v#L22-L57) —— 例如 N=32 时 `taps = 32'h80000057`，N=8 时 `taps = 8'h8E`（引用自 CMU Koopman 的 LFSR 多项式表）。

仓库已经为 `oh_lfsr` 准备了标准 testbench 包装，把 LFSR 的 `out` 接到 `dut_status`：

[stdlib/testbench/tb_oh_lfsr.v:L59-L67](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/tb_oh_lfsr.v#L59-L67) —— 例化 `oh_lfsr`，`taps` 接 `ctrl`、`seed` 接 `seed` 输入、`en` 恒为 1，`out` 作为状态输出。

`oh_parity` 表面上只有一行：

[stdlib/rtl/oh_parity.v:L8-L17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_parity.v#L8-L17) —— 模块体里写的是 `assign parity = ^in;`。

> ⚠️ **以源码为准（重要）**：`oh_parity` 的**输出端口名是 `out`**，但模块体内赋值给的是一个**未声明的隐式线网 `parity`**，真正的输出端口 `out` **没有任何驱动**（综合/仿真时会得到 `z` 或 `x`）。这是仓库里一处明显的遗留问题。若你要实际使用奇偶校验，需要自行把 `assign parity = ^in;` 改成 `assign out = ^in;`（本讲不修改源码，请你在自己的封装模块里例化并修正，或当作「读源码找 bug」的练习）。

#### 4.4.4 代码实践

**目标**：用 `tb_oh_lfsr.v` 的接法，手动跑一个 8 位 LFSR，观察它是否在 `2^8 - 1 = 255` 拍后回到种子。

**操作步骤**（源码阅读型 + 仿真型，**待本地验证**）：

1. 仿照 `tb_oh_lfsr.v` 写一个独立的小 testbench（**示例代码**）：

```verilog
// 示例代码：tb_lfsr.v —— 8 位 Galois LFSR，taps=0x8E，观察周期
module tb;
    reg clk=0, nreset=1, en=1;
    reg  [7:0] taps = 8'h8E;
    reg  [7:0] seed = 8'h01;
    wire [7:0] out;
    integer    i;

    oh_lfsr #(.N(8), .TYPE("GALOIS")) dut (
        .clk(clk), .nreset(nreset), .en(en),
        .taps(taps), .seed(seed), .out(out));

    always #5 clk = ~clk;
    initial begin
        nreset=0; #2 nreset=1;          // 复位装入 seed
        for (i=0; i<256; i=i+1) begin
            @(posedge clk);
            $display("step=%0d out=%02h", i+1, out);
        end
        $display("seed=%02h out_after_255_steps=%02h", seed, out);
        $finish;
    end
endmodule
```

2. 运行：`iverilog -g2005 -y stdlib/rtl -o sim tb_lfsr.v && vvp sim | tail -3`。

**预期现象**：第 255 步的 `out` 应等于 `seed`（0x01），证明周期恰为 `2^8-1`。

**待本地验证**：若把 `taps` 改成非最大长度多项式（如随意取 `8'h11`），周期会变短，提前回到种子。

#### 4.4.5 小练习与答案

**练习 1**：LFSR 的种子为什么不能为 0？

> **答案**：全 0 状态下，`out[0]=0`，反馈项 `({N{out[0]}} & taps)=0`，下一拍仍是 `out>>1 = 0`，即「全 0 是不动点」。所以种子为 0 会把 LFSR 永远锁死在全 0，周期退化。

**练习 2**：`oh_parity` 当前源码的 bug 是什么？怎么修？

> **答案**：模块体内 `assign parity = ^in;` 赋值给了未声明的隐式线网 `parity`，而端口 `out` 未被驱动。修正方法：把语句改成 `assign out = ^in[N-1:0];`。

---

## 5. 综合实践

把本讲的计数器、算术、LFSR 串成一个**「伪随机序列累加器」**：

**任务**：设计一个模块 `rand_accum`，每拍用 `oh_lfsr`（N=8，taps=`8'h8E`）产生一个新随机字节，用 `oh_counter`（N=16）统计已经产生的字节数，并用 CSA 思路把「当前随机字节 + 上一次的累加和 + 0」逐步累加（即用一个 `oh_add` 维护 16 位累加和）。要求：

1. `oh_lfsr` 的 `en` 接 `oh_counter` 的 `wraparound` 反面（持续运行即可）；
2. 每个 `clk` 把 LFSR 的 8 位输出零扩展到 16 位，与上次的 16 位累加和相加（用 `oh_add`），结果回写进一个 16 位寄存器；
3. 当 `oh_counter` 计满 256 次后停机，拉高 `done`；
4. 用 testbench 比对硬件累加和与软件参考值。

**要点提示**：
- LFSR 输出虽是伪随机，但确定性可复现，所以硬件和软件参考用同一 `seed/taps` 就能逐拍比对。
- 这就是 DMA（u8-l3）、数据通路里「地址步进 + 数据搬运 + 计数」的雏形——`oh_counter` 管节拍，`oh_add` 管累加，`oh_lfsr` 管激励。

> 这是设计型任务，**待本地验证**：请自行写出 RTL 与 testbench 并用 `iverilog -g2005 -y stdlib/rtl` 编译运行。

## 6. 本讲小结

- `oh_counter` 用「补码加法」统一增/减计数，靠归约运算 `|`/`&` 检测全 0/全 1 边界并给出 `wraparound`；学习以 soft 分支为准，减计数路径先仿真确认。
- `oh_add`（CPA）/`oh_mult` /`oh_shift` 是最基础的加、乘、移位原语；soft 分支分别有 `carry` 恒 0、`sum/carry` 未驱动、hard 分支 `TYPE` 未声明等遗留，使用时只依赖各自真正驱动的输出（`sum/cout`、`product`、`out`）。
- **进位保存加法**是本讲核心：一位全加器即 3:2 压缩器，`oh_csa32` 把三数压成 `(S,C)` 且无横向进位；多操作数求和用 CSA 树压缩到 2 个向量，最后用一次 `oh_add` 合并。
- `oh_csa42`/`oh_csa62` 演示了如何用 csa32 搭出更宽的压缩器，是 Wallace 树的积木。
- `oh_lfsr`（Galois）是廉价伪随机源，最大长度多项式下周期 `2^N-1`；`oh_parity` 是异或归约校验，但当前源码存在输出端口未驱动的 bug，使用前需修正。

## 7. 下一步学习建议

- **进入 u3-l4（仲裁器与脉冲控制）**：学习 `oh_arbiter` / `oh_pulse` / `oh_stretcher` / `oh_debouncer`，补齐控制类原语，为 emesh/elink 的多主端仲裁打基础。
- **回顾 u3-l2（FIFO）**：FIFO 的读写指针就是用计数器思想实现的，本讲的 `oh_counter` 与 u3-l2 的指针逻辑互相对照会更有收获。
- **前瞻 u8-l3（edma）**：DMA 的地址步进（stride、1D/2D）本质上是计数器 + 加法器的组合，本讲的 `oh_counter` + `oh_add` 是理解 edma 数据通路的钥匙。
- **延伸阅读**：想深入了解 CSA 树与快速乘法，可搜索「Wallace tree」「Daddara multiplier」「carry-save adder」；LFSR 部分可对照 `oh_lfsr.v` 头部引用的 CMU Koopman 多项式表（https://users.ece.cmu.edu/~koopman/lfsr/index.html ）。
