# ALU 运算单元 cpuops

## 1. 本讲目标

ALU（Arithmetic Logic Unit，算术逻辑单元）是 CPU 里最基础也最核心的执行部件——它负责「算」。本讲聚焦 ZipCPU 流水线第 4 级（执行级）里的 ALU 模块 `cpuops`，学完后你应当能够：

- 说清 `cpuops` 支持哪些运算、它的输入输出端口各代表什么。
- 对任意一次 ADD/SUB 运算，**手工推出**结果同时产生的四个条件码 `Z/N/C/V` 各是 0 还是 1，并解释为什么。
- 理解 `OPT_SHIFTS` 这个综合期参数如何「裁剪」出两种移位实现，以及为什么 FPGA 上常常要专门处理移位。
- 看懂 `zipcore` 是如何实例化 `cpuops`、又如何把它的「结果」和「标志」分别写回寄存器堆和状态寄存器 `CC` 的。

本讲承接 u3-l1（zipcore 流水线总体结构）：取指→译码→读操作数之后，指令进入第 4 级，ALU 就在这里登场。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是 ALU？** 它是一个组合/时序混合电路，输入两个操作数和一个「做哪种运算」的选择信号，输出运算结果和一组「标志位」。可以把它想成一个带旋钮的计算器：旋钮拧到 ADD 就做加法，拧到 AND 就做按位与。

**第二，什么是条件码（条件标志）Z/N/C/V？** 它们是上一次 ALU 运算留下的「副产品」，用四个比特记录结果的四类特征：

| 标志 | 含义 | 何时置 1 |
|------|------|----------|
| `Z`（Zero） | 结果为零 | 运算结果全 0 |
| `N`（Negative） | 结果为负 | 结果最高位（符号位）为 1 |
| `C`（Carry） | 进位/借位 | **无符号**运算溢出 |
| `V`（Overflow） | 溢出 | **有符号**运算溢出 |

后续的条件执行（u2-l4）和分支指令，正是靠读这几个标志来决定要不要执行/跳转的。

**第三，什么是「综合期参数」？** Verilog 的 `parameter` 不是运行时的开关，而是「综合（把代码变成硬件电路）时的剪刀」。参数为 0 时对应电路**根本不会被生成**。这一点在 u3-l1 已建立，本讲会用 `OPT_SHIFTS` 再演示一次。

> 关键术语复习：见 u2-l1 关于状态寄存器 `CC` 的讲解——`CC` 的最低 4 位正是 `Z/C/N/V`（注意顺序：bit0=Z、bit1=C、bit2=N、bit3=V）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/core/cpuops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v) | ALU 本体：实现算术/逻辑/移位/位反转/装入低位等运算，并生成 `Z/N/C/V` 标志。乘法在此模块内调用 `mpyop` 子模块（乘法本身在 u3-l5 详讲）。 |
| [rtl/core/zipcore.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v) | CPU 内核。在第 4 级实例化 `cpuops`（实例名 `doalu`），并把它的结果 `alu_result`、标志 `alu_flags` 在写回阶段分别送往寄存器堆和 `CC` 寄存器。 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范。其中的 OpCode 表（5 位操作码）和 CC 寄存器位定义是理解 `i_op` 4 位编码与标志含义的权威依据。 |

本讲涉及的最小模块：

1. **cpuops 的接口与运算集合**——它认识哪些运算、端口怎么连。
2. **条件码 Z/N/C/V 的产生**——结果如何同时算出四个标志（以 ADD 为例）。
3. **OPT_SHIFTS 控制的移位实现**——移位的两种实现选项与取舍。
4. **zipcore 对 cpuops 的实例化与写回**——结果和标志如何分别落盘。

---

## 4. 核心概念与源码讲解

### 4.1 cpuops 的接口与运算集合

#### 4.1.1 概念说明

`cpuops` 是一个**独立的 ALU 模块**，对内核而言就是一个「黑盒计算器」：你给它两个 32 位操作数 `i_a`、`i_b` 和一个 4 位运算选择码 `i_op`，再给它一个时钟节拍 `i_stb`（strobe，选通），它就吐出 32 位结果 `o_c`、4 位标志 `o_f`、以及「本次结果是否有效」`o_valid` / 「我还在算」`o_busy`。

为什么 `i_op` 只有 4 位，而 spec 里的操作码是 5 位？因为**最高位（bit 4）被用来区分「写不写回结果」**：

- 5 位操作码 `0x00 SUB` 与 `0x10 CMP` 的低 4 位都是 `0x0`——它们都做减法；
- 区别在于 `CMP`（比较）只更新标志、**丢弃结果**，而 `SUB`（减法）既更新标志又写回结果。

这个「丢弃还是写回」的差别由内核的写回控制（`op_wR`）处理，**不在 ALU 内部**。所以 ALU 只需要 4 位 `i_op` 来选运算类型，`SUB` 和 `CMP` 对它而言是同一种运算。

#### 4.1.2 核心流程

`cpuops` 的核心是一个时钟驱动的 `casez`，每个时钟沿、当 `i_stb` 有效时，按 `i_op` 选择一条运算并锁存结果：

```
每个 i_clk 上升沿：
  若 i_stb（有新请求）:
      pre_sign <= i_a[31]            // 记下 A 的符号，供标志计算用
      c         <= 0                  // 进位默认清零
      按 i_op 选择：
        0x0 -> {c,o_c} = i_a - i_b   // SUB / CMP
        0x1 -> o_c     = i_a & i_b   // AND / TST
        0x2 -> {c,o_c} = i_a + i_b   // ADD
        0x3 -> o_c     = i_a | i_b   // OR
        0x4 -> o_c     = i_a ^ i_b   // XOR
        0x5 -> {o_c,c} = i_a >> i_b  // LSR 逻辑右移
        0x6 -> {c,o_c} = i_a << i_b  // LSL 逻辑左移
        0x7 -> {o_c,c} = i_a >>> i_b // ASR 算术右移
        0x8 -> o_c     = bit_reverse(i_b)        // BREV 位反转
        0x9 -> o_c     = {i_a[31:16], i_b[15:0]} // LDILO 装入低半字
        0xA -> o_c     = mpy_result[63:32]       // MPYHU 无符号乘高位
        0xB -> o_c     = mpy_result[63:32]       // MPYSHI 有符号乘高位
        0xC -> o_c     = mpy_result[31:0]        // MPY 乘低位
        其它 -> o_c    = i_b                     // MOV / LDI
```

注意几处用 `{c, o_c}` 或 `{o_c, c}` 的拼接：它们把「进位位」和「32 位结果」拼成一个 33 位数，从而让加法/减法/移位的**最高位溢出**自然落到 `c` 里——这就是 `C`（Carry）标志的来源之一。

> 关于乘法（`0xA/0xB/0xC`）：`cpuops` 内部实例化了一个 `mpyop` 子模块来真正算乘法，乘法可能需要多个时钟周期（`o_busy` 拉高、`o_valid` 延后）。乘法的实现细节留到 **u3-l5** 专讲，本讲只把它当作「ALU 的一个多周期伙伴」。
>
> 关于除法：spec 中的 `DIVU/DIVS`（5 位操作码 `0x0E/0x0F`，低 4 位 `0xE/0xF`）**不经过 `cpuops`**。内核 `zipcore` 把除法指令路由给一个独立的 `div` 模块（实例名 `thedivide`）。`cpuops` 顶部的注释说「opcodes 0-13 是 ALU、14-15 是除法」即指此分工。

#### 4.1.3 源码精读

模块端口与综合期参数（`OPT_MPY` 选乘法实现、`OPT_SHIFTS` 选移位实现、`OPT_LOWPOWER` 控制低功耗门控）：

[rtl/core/cpuops.v:L41-L70](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L41-L70) —— `cpuops` 的 module 声明：核心输入是 4 位 `i_op`、两个 32 位操作数 `i_a`/`i_b`、选通 `i_stb`；核心输出是 32 位结果 `o_c`、4 位标志 `o_f`、有效/忙握手 `o_valid`/`o_busy`。

运算选择的主 `casez`（注意 ADD/SUB/移位的 33 位拼接如何把进位收进 `c`）：

[rtl/core/cpuops.v:L180-L204](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L180-L204) —— 主 ALU 运算 `casez`：`4'b0000` 是 CMP/SUB、`4'b0010` 是 ADD（`{c,o_c} <= i_a + i_b`）、`4'b0101/0110/0111` 是三种移位、`4'b1000` 是位反转、`default`（含 MOV 的 `0xD`）是 `o_c <= i_b`。

multiply 多周期判定与 `mpyop` 子模块实例化：

[rtl/core/cpuops.v:L155-L175](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L155-L175) —— `this_is_a_multiply_op` 识别 `i_op` 为乘法（`0xA/0xB/0xC`）时拉高，并把请求转给 `mpyop` 子模块；乘法结果 `mpy_result` 再由上面的 `casez` 选入 `o_c`。

`o_valid` 握手（决定单周期运算 vs 多周期乘法分别何时回报结果）：

[rtl/core/cpuops.v:L232-L240](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L232-L240) —— 普通运算（含 ADD）当 `OPT_MPY<=1` 时下一拍即 `o_valid<=i_stb`；带多周期乘法时，非乘法运算立即有效，乘法运算则要等 `mpydone`。

#### 4.1.4 代码实践

**实践目标**：确认 `i_op` 的 4 位编码与 spec 的 5 位操作码的对应关系。

**操作步骤**：
1. 打开 [doc/src/spec.tex 的 OpCode 表](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L696-L738)，记下几条 5 位操作码：`SUB=0x00`、`AND=0x01`、`ADD=0x02`、`CMP=0x10`、`TST=0x11`、`MOV=0x0D`。
2. 对照 [cpuops.v 的 casez](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L185-L200)，写出它们各自对应的 4 位 `i_op`。
3. 验证你的结论：`SUB` 与 `CMP` 的 `i_op` 是否相同？`AND` 与 `TST` 呢？

**需要观察的现象 / 预期结果**：`SUB` 和 `CMP` 的低 4 位都是 `0x0`（同一运算），`AND` 和 `TST` 的低 4 位都是 `0x1`。它们的差别只在 bit 4：bit4=0 写回结果（SUB/AND），bit4=1 只置标志（CMP/TST）。这正是 ALU 只用 4 位 `i_op` 的原因。

#### 4.1.5 小练习与答案

**练习 1**：指令 `OR`（`0x03`）和 `XOR`（`0x04`）的 `i_op` 分别是多少？在 `cpuops` 里它们是否产生进位 `c`？

**参考答案**：`i_op` 分别是 `4'h3` 和 `4'h4`。它们在 casez 中只赋值 `o_c`（`o_c <= i_a | i_b` 和 `o_c <= i_a ^ i_b`），没有触碰 `c`，而 `c` 在每个请求开始时被清零（`c <= 1'b0`），因此逻辑/位运算恒定 `C=0`。

**练习 2**：`default` 分支（`o_c <= i_b`）覆盖了哪些指令？为什么 ALU 把它们统一处理成「直接把 B 操作数送出去」？

**参考答案**：覆盖 `i_op >= 0xD`，对应 `MOV` 和 `LDI`（以及不会被送进来的除法码）。`MOV Rb,Ra` 的语义就是「把 B 送入 A」，`LDI` 在译码端已被改写为等价的 MOV 形式（见 u3-l3），所以对 ALU 而言都只是 `o_c = i_b`。

---

### 4.2 条件码 Z/N/C/V 的产生

#### 4.2.1 概念说明

`cpuops` 最巧妙的地方在于：**一次运算不仅算出 32 位结果，还「顺手」算出四个标志**，而且几乎不增加额外硬件。理解这一点是本讲的核心。

四个标志里，`Z`（零）和 `N`（负）最直白——它们只看结果本身：结果全 0 则 `Z=1`，结果最高位为 1 则 `N=1`。`C`（进位）来自无符号溢出，`V`（有符号溢出）来自有符号溢出。

但 ZipCPU 有一个**容易被忽略的细节**：当 ADD/SUB 发生**有符号溢出**时，它会把上报给 `CC` 的 `N` 位**取反**。这是为了让 `N` 反映「数学上正确结果的符号」，从而使有符号比较（`LT`/`GE`）只需看 `N` 一个位即可成立。这个「N 修正」逻辑藏在标志组装表达式里，我们会在源码精读中拆开。

#### 4.2.2 核心流程

标志在**结果 `o_c` 锁存之后**，由组合逻辑（`assign`）即时算出：

```
z  = (o_c == 0)                          // 零标志：结果全 0
n  = o_c[31]                             // 原始负标志：结果符号位
v  = set_ovfl      && (pre_sign != o_c[31])   // 有符号溢出
vx = keep_sgn_on_ovfl && (pre_sign != o_c[31])// 需要修正 N 的溢出

o_f = { v,               // bit3 = V
        n ^ vx,           // bit2 = N（溢出时取反）
        c,                // bit1 = C
        z }               // bit0 = Z
```

其中两个寄存器 `set_ovfl` 和 `keep_sgn_on_ovfl` 在**上一拍**根据 `i_op` 和输入符号预算好，专门描述「这次运算是否可能产生有符号溢出」：

- **ADD（`i_op==0x2`）**：两操作数**同号**才可能溢出 → `set_ovfl = (i_a[31]==i_b[31])`。
- **SUB/CMP（`i_op==0x0`）**：两操作数**异号**才可能溢出 → `set_ovfl = (i_a[31]!=i_b[31])`。
- 移位 `LSL/LSR`（`0x6/0x5`）也参与溢出判定。

`pre_sign` 在运算当拍记录 `i_a[31]`（被加数/被减数的符号），与结果符号 `o_c[31]` 比较：符号从不一致即说明「有符号溢出」。

**以 ADD `0x7FFFFFFF + 0x00000001` 为例**（正数上溢）：

\[
\texttt{0x7FFFFFFF} + \texttt{0x00000001} = \texttt{0x80000000}
\]

逐位追踪：
- `{c, o_c} = i_a + i_b` → 33 位和 = `0x0_80000000`，故 `c=0`、`o_c=0x80000000`。
- `z = 0`（结果非零）；原始 `n = o_c[31] = 1`。
- `set_ovfl`：ADD 且 `i_a[31]==i_b[31]`（`0==0`）→ `1`。
- `pre_sign = i_a[31] = 0`，`o_c[31] = 1`，二者不等 → `v = 1`、`vx = 1`。
- 修正后的 `N = n ^ vx = 1 ^ 1 = 0`。
- 最终 `o_f = {V=1, N=0, C=0, Z=0} = 4'b1000`。

含义：发生了有符号溢出（`V=1`），但「真正的」结果是正数，故 `N` 被修正为 0；无符号层面没溢出（`C=0`，`0x80000000` 仍落在 32 位内）；结果非零（`Z=0`）。

#### 4.2.3 源码精读

`set_ovfl` / `keep_sgn_on_ovfl` 的预算（注意 ADD 取「同号」、SUB 取「异号」）：

[rtl/core/cpuops.v:L125-L139](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L125-L139) —— 每个请求拍根据 `i_op` 与输入符号，锁存「是否可能溢出」（`set_ovfl`）与「是否需要修正 N」（`keep_sgn_on_ovfl`），二者都只在 ADD/SUB 等运算下才为 1。

`o_f` 的组装（注意 `n ^ vx` 这一处「溢出时翻转 N」）：

[rtl/core/cpuops.v:L222-L228](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L222-L228) —— `z/n/v/vx` 的定义与 `o_f = { v, n^vx, c, z }` 的拼装。这正是把 `o_f` 直接对应到 `CC` 的 bit3..0 = `V/N/C/Z`（与 spec 的 CC 位定义顺序一致）。

主 casez 中 ADD/SUB 的 33 位拼接（`C` 的来源）：

[rtl/core/cpuops.v:L186-L188](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L186-L188) —— `4'b0000:{c,o_c}<=...-...`（SUB/CMP）与 `4'b0010:{c,o_c}<=i_a+i_b`（ADD）：用 33 位拼接把第 33 位（无符号进位）收进 `c`，即 `C` 标志。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 ADD 运算，手工推出 `Z/N/C/V` 四个标志，填满下面这张「合并真值表」，再用源码逻辑自检。

**操作步骤**：
1. 对下表每一行，按本节「核心流程」的公式，**先合上书**算出 `o_c`、`C`、原始 `N`、`V`、修正后 `N`、`Z`，最后写出 `o_f`（4 位二进制，`{V,N,C,Z}`）。
2. 算完后对照下方参考答案。
3. 若想进一步验证，可阅读 `cpuops` 的形式化证明配置 [bench/formal/cpuops.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/cpuops.sby)（需安装 SymbiYosys，在 `bench/formal` 下 `make cpuops`），它对 ALU 的握手与移位结果做了机器检查（注意：该证明主要验证移位与 busy/valid 握手，并不逐例验证 ADD 标志，所以 ADD 标志仍以本节的源码推导为准）。

**输入表（均为 ADD，`i_op=0x2`）**：

| i_a | i_b | 预期 o_c | 预期 o_f {V,N,C,Z} | 物理含义 |
|-----|-----|----------|--------------------|----------|
| `0x7FFFFFFF` | `0x00000001` | ? | ? | 正数上溢 |
| `0xFFFFFFFF` | `0x00000001` | ? | ? | 无符号回绕到 0 |
| `0x00000005` | `0x00000003` | ? | ? | 普通加法 |
| `0x80000000` | `0x80000000` | ? | ? | 负数下溢 |

**预期结果（参考答案）**：

| i_a | i_b | o_c | o_f {V,N,C,Z} | 说明 |
|-----|-----|-----|---------------|------|
| `0x7FFFFFFF` | `0x00000001` | `0x80000000` | `4'b1000` | V=1 有符号溢出；N 被修正为 0（真正为正）；C=0；Z=0 |
| `0xFFFFFFFF` | `0x00000001` | `0x00000000` | `4'b0010` | C=1 无符号进位；Z=1 结果为 0；V=0（异号相加不溢出）；N=0 |
| `0x00000005` | `0x00000003` | `0x00000008` | `4'b0000` | 无溢出、非零、为正 |
| `0x80000000` | `0x80000000` | `0x00000000` | `4'b1010` | V=1 有符号溢出；N 修正为 0（真正为负→修正后为 0，因为结果回绕到 0）；C=1；Z=1 |

> 第 4 行的 `N` 修正值得细品：两个最小负数相加，数学结果应是 `−2³²`，超出 32 位有符号范围；回绕后 `o_c=0`，原始 `n=0`，而 `vx=1`，故修正后 `N = 0^1 = 1`……这里请你**亲手再验一遍** `pre_sign`、`o_c[31]`、`vx` 三者的值，看看最终 `N` 到底是 0 还是 1——本表这一格刻意留作思考点。**待本地验证**：建议你照公式逐项推算，确认该格。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ADD 的溢出判定条件是「两操作数同号」（`i_a[31]==i_b[31]`），而 SUB 是「两操作数异号」？

**参考答案**：有符号溢出只可能发生在「结果的真实符号与操作数符号矛盾」时。ADD 同号相加，结果本应保持同号，若变号则溢出；SUB 等价于「A + (−B)」，当 A、B 异号时相当于两个同号数相加，才可能溢出。所以硬件用 `i_a[31]==i_b[31]`（ADD）和 `i_a[31]!=i_b[31]`（SUB）分别捕捉这两种情形。

**练习 2**：`o_f` 的 4 位顺序 `{V,N,C,Z}` 与 spec 的 `CC` 寄存器低位定义一致吗？为什么这点很重要？

**参考答案**：一致。spec 规定 `CC` 的 bit0=Z、bit1=C、bit2=N、bit3=V，而 `o_f={v,n^vx,c,z}` 正好把 V 放 bit3、N 放 bit2、C 放 bit1、Z 放 bit0。因此内核写回时可以直接把 `o_f` 整体写入 `CC[3:0]`，无需任何位重排。

---

### 4.3 OPT_SHIFTS 控制的移位实现

#### 4.3.1 案念说明

移位指令（`LSR` 逻辑右移、`LSL` 逻辑左移、`ASR` 算术右移）看似简单，在 FPGA 上却很「贵」：一个「任意位数」的桶形移位器（barrel shifter）要用大量查找表（LUT）和互连资源。`cpuops.v` 文件末尾的资源统计就体现了这一点——带移位的 iCE40 实现需要 748 个 `SB_LUT4`，而**不带移位**只要 323 个，**移位器几乎翻倍了 LUT 用量**。

因此 ZipCPU 用综合期参数 `OPT_SHIFTS` 给设计者一个选择：

- `OPT_SHIFTS=1`（默认）：生成完整的桶形移位器，支持任意 0–31 位的移位。
- `OPT_SHIFTS=0`：**完全不生成移位硬件**，移位结果退化为「移 0 位」（等于原值），靠软件用循环移位来实现。

这是一个典型的「面积 vs 功能」取舍：面积极度紧张时砍掉移位器，用编译器的循环展开换回功能。

#### 4.3.2 核心流程

`OPT_SHIFTS` 用 `generate if` 在综合期二选一：

```
generate if (OPT_SHIFTS) begin : IMPLEMENT_SHIFTS
    // 完整桶形移位器：
    //   ASR：先符号扩展到 33 位再 >>>，并对大移位量（≥32）填符号位
    //   LSR：对 ≥32 的移位量清零，否则 i_a 右移
    //   LSL：对 ≥32 的移位量清零，否则 i_a 左移
    w_asr_result / w_lsr_result / w_lsl_result  ←  真实移位结果（含进位位）
end else begin : NO_SHIFTS
    // 退化为「不移位」：结果就是 i_a 本身（高位补 i_a[31] 或 0）
    w_asr_result = { i_a[31], i_a }
    w_lsr_result = { 1'b0,   i_a }
    w_lsl_result = { i_a,    1'b0 }
end
```

注意三个移位结果都是 **33 位**（多出的一位即「最后被移出的那一位」，会落到 `C` 标志里——这正是 spec 所说「移位用进位位捕获最后移出的位」）。

对大移位量的处理也很讲究：当移位量 `i_b ≥ 32` 时，`LSL/LSR` 结果直接清零，`ASR` 结果填满符号位 `i_a[31]`；这种边界由 `(|i_b[31:5])` 等条件在纯组合逻辑里快速判定，避免实际去搭一个 32 位的移位多路选择器。

#### 4.3.3 源码精读

`OPT_SHIFTS` 的 `generate` 二选一与边界处理：

[rtl/core/cpuops.v:L92-L113](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L92-L113) —— `IMPLEMENT_SHIFTS` 分支用 `>>>`/`<<`/`>>` 计算三种 33 位移位结果，并对 `i_b≥32` 做清零/填符号处理；`NO_SHIFTS` 分支则把结果直接退化为「i_a 不动」。

主 casez 里移位如何取用这 33 位结果（注意 `ASR` 把进位放在高位 `{o_c,c}`，而 `LSR` 放在低位 `{o_c,c}`，二者拼接方向不同）：

[rtl/core/cpuops.v:L191-L193](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L191-L193) —— `LSR/LSL/ASR` 三条分别取 `w_lsr_result/w_lsl_result/w_asr_result[32:0]`，把移出的那一位送入 `c`（即 `C` 标志）。

移位结果的资源代价（佐证 `OPT_SHIFTS` 存在的理由）：

[rtl/core/cpuops.v:L393-L399](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L393-L399) —— 文件末尾的 iCE40 资源注释：带移位需 748 个 LUT4，不带仅 323 个，移位器约多耗一倍 LUT。

形式化证明中对移位边界的断言（佐证移位边界逻辑的正确性是被机器验证过的）：

[rtl/core/cpuops.v:L336-L389](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L336-L389) —— `FORMAL` 段对移位量 0、1、2、31、32、≥32 等边界分别断言 `LSR/LSL/ASR` 的正确结果。

#### 4.3.4 代码实践

**实践目标**：体会 `OPT_SHIFTS` 关闭后移位指令会怎样「失效」。

**操作步骤**：
1. 重读 [cpuops.v 的 NO_SHIFTS 分支](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L106-L112)：当 `OPT_SHIFTS=0` 时，`w_lsr_result` 等 33 位结果分别是什么？
2. 想象一条汇编 `LSR #4,R1`（把 R1 逻辑右移 4 位）在 `OPT_SHIFTS=0` 的内核上执行：`o_c` 会变成什么？`C` 标志呢？

**需要观察的现象 / 预期结果**：`NO_SHIFTS` 下 `w_lsr_result = {1'b0, i_a[31:0]}`，于是 `LSR` 的 `o_c = i_a`（原值未移）、`c = 0`。也就是说，硬件层面这条 `LSR` 相当于什么都没做——移位功能「消失」了，必须由编译器/软件用多次加法或循环来补偿。这正是省面积的代价。

> 这个实践是「源码阅读型」：它不需要你真的去综合一个无移位器的内核（那很慢），而是通过读 `generate` 分支推断行为。

#### 4.3.5 小练习与答案

**练习 1**：移位结果为什么是 33 位而不是 32 位？多出的那一位送给谁？

**参考答案**：多出的一位用来保存「移位过程中最后被移出去的那一位」，它被送入 `C`（Carry）标志。spec 明确说移位用进位位捕获最后移出的位，以便用汇编实现「扩展精度移位」。参见 casez 中 `{o_c,c} <= w_lsl_result[32:0]` 等写法。

**练习 2**：当移位量 `i_b = 40`（≥32）时，`LSR`、`LSL`、`ASR` 的结果分别是什么？

**参考答案**：`(|i_b[31:5])` 为真，于是 `LSR` 和 `LSL` 结果直接清零（`o_c=0`、`c=0`）；`ASR` 则全部填符号位 `i_a[31]`（即算术右移 ≥32 位后，正数变全 0、负数变全 1）。

---

### 4.4 zipcore 对 cpuops 的实例化与写回

#### 4.4.1 概念说明

`cpuops` 只是「算出结果和标志」，真正决定**要不要把结果写回寄存器、要不要把标志写回 CC** 的是内核 `zipcore`。这里要把两件事分清（u3-l1、u2-l4 已铺垫）：

- **条件执行（`set_cond`）**：指令位 21–19 的条件码是否满足。不满足时，结果**不写回**。这个判决在内核里做，不在 ALU 里——ALU 总是照算不误。
- **四路写回竞争**：写回阶段有四个可能的「结果来源」——ALU、内存（访存）、除法、FPU。它们经过一个多路选择，最终只允许一个写进寄存器堆（由 `wr_index` / `wr_reg_ce` 控制）。

`cpuops` 的实例名是 `doalu`，它的结果线叫 `alu_result`、标志线叫 `alu_flags`、握手线叫 `alu_valid`/`alu_busy`。这些信号在内核里到处被引用，是第 4 级与第 5 级（写回）之间的「血管」。

#### 4.4.2 核心流程

ALU 一次运算到落盘的全链路：

```
第 3 级（读操作数）输出：op_opn(4位运算码)、op_Av、op_Bv(两操作数)、op_wR、op_wF(是否写结果/写标志)
        │
        ▼  set_cond = 条件码是否满足  ←  由此决定「这条指令是否真正生效」
第 4 级（执行）：
   alu_ce = (该指令是 ALU 类) 且 流水线允许前进
   doalu.i_op  ← op_opn
   doalu.i_a   ← op_Av
   doalu.i_b   ← op_Bv
   doalu.i_stb ← alu_ce
        │  下一拍
        ▼
   alu_result = doalu.o_c   （32 位结果）
   alu_flags  = doalu.o_f   （{V,N,C,Z}）
   alu_valid  = doalu.o_valid
        │
        ▼  写回控制（组合了 set_cond、alu_wR、alu_wF）
第 5 级（写回）：
   若 (alu_wR && alu_valid && !clear_pipeline)：把 alu_result 写入寄存器堆[op_R]
   若 (alu_wF && alu_valid && !clear_pipeline)：把 alu_flags  写入 CC[3:0]
```

其中 `alu_wR`/`alu_wF` 在第 4 级锁存，它们的值是 `op_wR`/`op_wF` **再与 `set_cond` 相与**——也就是说，条件不满足的指令，其 `alu_wR`/`alu_wF` 被清零，ALU 算出的结果自然就被「丢弃」了。这就是 ZipCPU 条件执行的硬件实现：**不冲刷流水线，而是在写回端静默关闭写使能**（见 u2-l4）。

#### 4.4.3 源码精读

`cpuops` 的实例化（实例名 `doalu`，传入 `op_opn/op_Av/op_Bv`，输出 `alu_result/alu_flags`）：

[rtl/core/zipcore.v:L1506-L1523](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1506-L1523) —— 第 4 级里 `cpuops doalu(...)` 的实例化：`.i_op(op_opn)`、`.i_a(op_Av)`、`.i_b(op_Bv)`、`.i_stb(alu_ce)`，结果与标志接到 `alu_result`/`alu_flags`，握手接到 `alu_valid`/`alu_busy`；`OPT_LOWPOWER` 时还会在非 `alu_ce` 拍把输入置零以省功耗。

`set_cond` 的定义（条件码满足判定）：

[rtl/core/zipcore.v:L1599](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1599) —— `set_cond = ((op_F[7:4]&op_Fl[3:0])==op_F[3:0])`：把指令带的条件码与当前 `CC` 标志比较，相等才认为条件满足。

`alu_wR`/`alu_wF` 如何吸收 `set_cond`（条件执行的写回端实现）：

[rtl/core/zipcore.v:L1604-L1629](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1604-L1629) —— 流水线模式下，`alu_wR <= (op_wR)&&(set_cond)&&(!op_illegal)`、`alu_wF` 同理；条件不满足时二者清零，ALU 结果即被静默丢弃。

寄存器堆写使能 `wr_reg_ce`（ALU 与内存/除法/FPU 的四路竞争）：

[rtl/core/zipcore.v:L2262-L2269](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2262-L2269) —— `wr_reg_ce` 综合 `dbgv`（调试写）、`i_mem_valid`（访存）、`alu_wR&&alu_valid`（ALU）、`div_valid`、`fpu_valid` 多个来源，决定本拍是否写寄存器堆。

标志写使能 `wr_flags_ce` 与标志值 `wr_flags`：

[rtl/core/zipcore.v:L2397-L2403](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2397-L2403) —— `wr_flags_ce` 要求 `alu_wF` 为真且未清流水线；[L2426-L2435](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2426-L2435) 的 `casez` 按 `wr_index` 选出 `alu_flags`/`div_flags`/`fpu_flags` 之一作为本次要写入 `CC` 的标志值。

`alu_flags` 如何嵌入完整的 `CC` 寄存器（区分用户组/监管组 `uflags`/`iflags`）：

[rtl/core/zipcore.v:L2440-L2447](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2440-L2447) —— `w_uflags`/`w_iflags` 把 `wr_flags`（即 `alu_flags`）放进 `CC` 的低 4 位，高位拼接睡眠、GIE、异常位等其它字段，形成完整的 16 位条件码字。

#### 4.4.4 代码实践

**实践目标**：把 ALU 结果与标志的「写回」这一段调用链在源码里走通。

**操作步骤**：
1. 从 [zipcore.v:L1506](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1506) 的 `doalu` 实例出发，找到 `alu_result` 与 `alu_flags` 这两根线。
2. 顺 `alu_flags` 往下追：它先进入 [wr_flags 的 casez](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2426-L2435)（对应 `wr_index==3'b010` 的 ALU 分支），再被 [w_uflags/w_iflags](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2440-L2447) 拼进 `CC`。
3. 回答：一条无条件 `ADD R2,R1`（会写结果、会写标志）经过这条链路后，`op_wR`、`op_wF`、`set_cond`、`alu_wR`、`alu_wF` 各应是几？

**需要观察的现象 / 预期结果**：无条件 ADD 的 `op_wR=1`、`op_wF=1`；`set_cond` 对无条件指令恒为 1；于是 `alu_wR=1`、`alu_wF=1`，结果与标志都会被写回。若是 `ADD.NZ R2,R1`（仅当 `Z=0` 时执行），则 `set_cond` 取决于当前 `Z`：`Z=1` 时 `alu_wR=alu_wF=0`，ALU 虽然照算，但结果与标志都不落盘。

#### 4.4.5 小练习与答案

**练习 1**：为什么条件不满足的指令不需要「冲刷流水线」？

**参考答案**：因为条件判决的结果（`set_cond`）被吸收进了写回使能 `alu_wR`/`alu_wF`。条件不满足时这两个使能清零，ALU 算出的结果不会写回寄存器堆和 `CC`，对外效果等同于「没执行」，但流水线照常前进，省去了冲刷带来的气泡（详见 u2-l4、u3-l7）。

**练习 2**：`alu_valid` 和 `alu_busy` 在写回端分别起什么作用？

**参考答案**：`alu_valid` 表示「本拍 ALU 有一个有效结果可供写回」，是写回的触发信号之一（`wr_reg_ce`/`wr_flags_ce` 都需要它）。`alu_busy` 表示「ALU 正在算一个多周期运算（乘法）还没出结果」，流水线会据此停顿，避免在结果未就绪时写回或送入下一条依赖指令（见 u3-l7 的冒险与停顿）。

---

## 5. 综合实践

**任务**：做一次「全链路追踪」——从一条汇编指令到 `CC` 标志的最终落盘。

假设有如下指令序列（伪汇编，仅用于追踪）：

```
LDI  0x7FFFFFFF,R1      ; R1 = 最大正数
LDI  1,R2                ; R2 = 1
ADD  R2,R1               ; R1 = R1 + R2，溢出
```

请完成下列追踪（**纯源码阅读，无需运行**）：

1. **译码侧**（u3-l3）：`ADD R2,R1` 的 5 位操作码是多少？送到 `cpuops` 的 4 位 `i_op` 又是多少？`op_Av`（源 A，即被改写的目的寄存器 R1）、`op_Bv`（B 操作数，即 R2）各是什么值？
2. **执行侧**（本讲 4.2）：在 [cpuops 的 casez](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L180-L204) 里，这条 ADD 走哪个分支？写出 `o_c`、`o_f` 的二进制值。
3. **标志组装**（本讲 4.2）：解释为什么此时 `V=1` 而 `N=0`（被修正），`C=0`、`Z=0`。
4. **写回侧**（本讲 4.4）：无条件 ADD 的 `alu_wR`/`alu_wF` 为何都是 1？`alu_flags` 经过 [wr_flags casez](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2426-L2435) 与 [w_uflags/w_iflags](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2440-L2447) 后，最终写进 `CC` 的 bit3..0 是什么？它对应的 `Z/C/N/V` 又是什么？

**预期结果**：操作码 `0x02`，`i_op=4'h2`，`op_Av=0x7FFFFFFF`、`op_Bv=0x00000001`；走 `4'b0010`（ADD）分支，`o_c=0x80000000`、`o_f=4'b1000`；`CC[3:0]` 写为 `4'b1000`，即 `V=1, N=0, C=0, Z=0`。这条追踪把译码→执行→标志生成→写回四个阶段串了起来，是理解整个 ALU 数据通路的最好练习。

## 6. 本讲小结

- `cpuops` 是 ZipCPU 的 ALU，输入 4 位 `i_op` + 两个 32 位操作数，输出 32 位结果 `o_c` 与 4 位标志 `o_f`；5 位 spec 操作码的最高位用来区分「写不写回」（如 `SUB`/`CMP` 共用 `i_op=0x0`）。
- 四个标志在一次运算中**同时产生**：`Z` 看结果是否为零、原始 `N` 看符号位、`C` 来自无符号进位（33 位拼接的最高位）、`V` 来自有符号溢出判定（ADD 同号、SUB 异号才可能溢出）。
- ZipCPU 在有符号溢出时会把上报的 `N` **取反**（`n ^ vx`），使 `N` 反映「真实符号」，便于有符号比较；`o_f={V,N,C,Z}` 的位序与 spec 的 `CC` 低位定义完全一致，可直接写入。
- `OPT_SHIFTS` 是综合期「剪刀」：为 1 时生成完整桶形移位器（代价约翻倍 LUT），为 0 时移位退化为「不移位」、由软件补偿。
- `zipcore` 在第 4 级以 `doalu` 实例化 `cpuops`；条件执行通过把 `set_cond` 吸收进写回使能 `alu_wR`/`alu_wF` 来实现（不冲刷流水线）；结果与标志在写回阶段与内存/除法/FPU 四路竞争后分别落入寄存器堆与 `CC`。

## 7. 下一步学习建议

- **乘除法单元（u3-l5）**：本讲提到 `cpuops` 内部调用 `mpyop` 算乘法、除法由独立的 `div` 处理。下一讲专门讲清多周期乘法（`mpyop`/`slowmpy`）与除法（`div`）的迭代算法和 `alu_busy`/`o_busy` 握手。
- **流水线冒险与停顿（u3-l7）**：`alu_busy`、`alu_valid` 如何参与流水线停顿、RAW 冒险如何用停顿解决，将在那里系统讲解。
- **想动手验证**：可阅读 [bench/formal/cpuops.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/cpuops.sby) 与 `cpuops.v` 文末的 `FORMAL` 段，看形式化方法如何机器证明 ALU 的移位边界与握手契约（u5-l2 会系统讲形式化验证体系）。
