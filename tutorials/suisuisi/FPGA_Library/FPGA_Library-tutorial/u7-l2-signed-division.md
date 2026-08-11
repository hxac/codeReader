# 有符号定点除法器

## 1. 本讲目标

本讲深入 projf 数学库中最复杂的一个模块 `div.sv`——一个**有符号定点除法器**。学完后你应该能够：

- 说清「恢复余数法（restoring division）」在硬件上是如何用「移位 + 比较 + 减法 + 上商」一步步算出商的；
- 读懂 `div.sv` 的五状态机 `IDLE / INIT / CALC / ROUND / SIGN`，并解释每个状态干什么；
- 看懂 `divu_int.sv`（无符号整数）、`divu.sv`（无符号定点）与 `div.sv`（有符号定点）三者如何构成一条由易到难的实现阶梯；
- 解释 ROUND 状态里的**高斯舍入（round half to even）**与 SIGN 状态里的**符号还原**是怎么得到最终商的；
- 理解除零（`dbz`）与溢出（`ovf`）两条异常路径的检测时机。

本讲是 u7-l1（定点数与 Q 格式）的直接下游：我们不再讨论「一个数怎么表示」，而是讨论「两个定点数怎么相除、结果怎么落回 Q 格式」。

## 2. 前置知识

本讲默认你已经掌握 u7-l1 的内容，重点复习这几条：

- **Q 格式**：一个 `WIDTH` 位、`FBITS` 位小数的定点数，整数位 `IBITS = WIDTH − FBITS`（含符号位），精度为 \(2^{-\text{FBITS}}\)。例如 `WIDTH=8, FBITS=4` 时，`+1.5` 编码为 \(1.5 \times 16 = 24 = \mathtt{0001\_1000}\)。
- **二进制补码与 `signed`**：同一串比特在 `signed`/`unsigned` 下数值不同；`signed` 影响符号扩展与比较语义。
- **SMALLEST**：\(n\) 位有符号数里最小的那个值 \(-2^{n-1}\)（如 8 位的 \(\mathtt{1000\_0000}=-128\)），它没有对应的正数，取绝对值会溢出——这是后面「溢出检测」要特别处理的对象。
- **高斯舍入（向偶舍入）**：恰好在 0.5 ULP（最低有效位）处时，向最近的偶数舍入，以消除纯「四舍五入」带来的统计偏差。u7-l1 已在乘法场景里提过它，本讲把它用到除法的最后一位。
- **SystemVerilog 子集**：`logic`、`always_comb`/`always_ff`、`enum`、`$clog2`——projf 库用这一小套语法让 Verilog 更安全（见 u5-l1）。

还需要一个朴素直觉：**为什么 FPGA 上除法这么麻烦？** 加法、乘法在 FPGA 里基本上「一个时钟周期」就能做完（乘法有专用 DSP 单元），但除法既没有专用硬指令、又本质上是**迭代**过程——你得一位一位地把商「试」出来。所以库里的 `div` 是一个多周期状态机，而不是一条组合逻辑赋值。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `ThreePart/projf-explore/lib/maths/` 下：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [div.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv) | 有符号定点除法，带高斯舍入 | 主角，核心状态机 |
| [divu.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu.sv) | 无符号定点除法，向零截断 | 阶梯的第二级 |
| [divu_int.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu_int.sv) | 无符号整数除法，带余数 | 阶梯的第一级，最简单 |
| [test/div.py](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py) | `div` 的 cocotb 测试 | 实践依据（含舍入/边界用例） |
| [test/div.mk](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.mk) | cocotb 编译脚本 | 说明测试用 `WIDTH=9,FBITS=4` |

三个除法模块共享**同一段迭代核心**（`always_comb`），区别只在外围控制：要不要处理符号、要不要舍入、要不要输出余数。所以我们会从最朴素的 `divu_int` 切入，先把迭代核心吃透，再逐步加上「定点」「有符号」「舍入」这三层复杂度。

## 4. 核心概念与源码讲解

### 4.1 divu：无符号除法的迭代核心

#### 4.1.1 概念说明

人类手算十进制除法时，是「估商→相乘→相减→落下一位」。二进制除法更简单：商的每一位只能是 0 或 1，所以不需要「估」，只需要「比」——把部分余数和除数比一下，够减就上 1 并减，不够减就上 0。这就是**恢复余数法（restoring division）**：

```
部分余数 R 初始化为 0
重复 n 次（n = 被除数位数）:
    把 {R, 被除数} 整体左移 1 位   # 被除数的下一位被"挪进"部分余数
    if R >= 除数:
        R = R - 除数
        商的这一位 = 1
    else:
        商的这一位 = 0
最终：商在低位寄存器里，余数在 R 里
```

关键技巧：projf 把「部分余数（acc）」和「被除数/商（quo）」拼成**一个长的移位寄存器** `{acc, quo}`。每左移一次，被除数的高位就被「挤」进 acc，而 quo 的最低位腾出来装新算出的商位。一个寄存器同时承担「消费被除数」和「积累商」两件事，省硬件。

> 为什么除法是迭代的？因为商有几位就要循环几次，每次只产出 1 个比特。这是除法和加/乘最大的区别，也是它必须做成多周期状态机的根本原因。

#### 4.1.2 核心流程

三个除法模块的迭代核心几乎逐字相同，区别只在「循环多少次」「要不要舍入状态」「要不要输出余数」：

| 模块 | 迭代次数 ITER | 输入 | 输出 | 舍入 |
|------|---------------|------|------|------|
| `divu_int` | `WIDTH` | 无符号整数 | 商 + 余数 | 无（整数精确） |
| `divu` | `WIDTH + FBITS` | 无符号定点 | 商 | 向零截断 |
| `div` | `(WIDTH−1) + FBITS` | 有符号定点 | 商 | 高斯舍入 |

注意 `div` 的 ITER 用的是 `WIDTHU + FBITS`，其中 `WIDTHU = WIDTH − 1`，因为有符号数去掉符号位后绝对值少一位。多出来的 `FBITS` 次迭代，正是为了在整数商之外再算出 `FBITS` 位**小数**——这正是「定点除法」相对「整数除法」多做的活。

每次迭代的判定逻辑（伪代码）：

```
if (acc >= 除数) {        # 够减
    acc = acc - 除数;      # 减
    {acc, quo} 左移, quo 最低位 = 1   # 上商 1
} else {                  # 不够减
    {acc, quo} 左移, quo 最低位 = 0   # 上商 0
}
```

#### 4.1.3 源码精读

迭代核心写在一段纯组合逻辑里（三个文件几乎一样，这里看最简单的 `divu_int.sv`）：

[divu_int.sv:28-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu_int.sv#L28-L35) —— 比较、减法、上商一气呵成：

```verilog
always_comb begin
    if (acc >= {1'b0, b1}) begin
        acc_next = acc - b1;
        {acc_next, quo_next} = {acc_next[WIDTH-1:0], quo, 1'b1};  // 上商 1
    end else begin
        {acc_next, quo_next} = {acc, quo} << 1;                    // 上商 0
    end
end
```

几点要读懂：

- `acc` 比 `quo`/`b1` **宽 1 位**（`logic [WIDTH:0] acc`，见 [divu_int.sv:24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu_int.sv#L24)）。这多出来的 1 位是「借位/保护位」，让 `acc − b1` 在不够减时也不会算错，并容纳移位时的进位。
- `{1'b0, b1}` 是把除数前面补一个 0，凑成和 `acc` 一样的位宽再做比较。
- else 分支的 `{acc, quo} << 1` 把整个长寄存器左移一位——这就是「把被除数的下一位挪进部分余数」。
- if 分支先减 (`acc_next = acc − b1`)，再拼接 `{acc_next[WIDTH-1:0], quo, 1'b1}`：低 WIDTH 位是减法结果、接上 quo、再补一个 `1` 作为本位商。

控制逻辑（何时启动、何时结束）在 `always_ff` 里。[divu_int.sv:38-64](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu_int.sv#L38-L64)：`start` 一来就把被除数装进长寄存器（`{acc, quo} <= {{WIDTH{1'b0}}, a, 1'b0}`），然后每个时钟跑一次迭代，直到 `i == WIDTH-1`。结束时商在 `quo_next`，余数要把最后一次多余的移位撤回：

[divu_int.sv:54-59](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu_int.sv#L54-L59) —— 输出商与余数：

```verilog
if (i == WIDTH-1) begin  // we're done
    ...
    val <= quo_next;
    rem <= acc_next[WIDTH:1];  // undo final shift   撤回最后一次移位才是真正的余数
end
```

> `rem <= acc_next[WIDTH:1]` 为什么要「撤回移位」？因为结束的那一拍，迭代核心又左移了一次，acc 里的内容被整体左移了 1 位，真正的余数是它右移 1 位后的值。

升级到 `divu.sv`（无符号定点）只多了两件事：一是迭代次数变成 `ITER = WIDTH + FBITS`（[divu.sv:31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu.sv#L31)），多算 `FBITS` 位小数；二是多了一个**溢出检测**，在 `i == WIDTH-1` 时检查商的高位是否非零（[divu.sv:67](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu.sv#L67)），原因是多算的小数位会把原本「溢出」的高位顶出寄存器。它**不做舍入**，直接截断（round towards zero）。

#### 4.1.4 代码实践

**实践目标**：用最简单的 `divu_int` 验证你对「恢复余数法」的理解。

**操作步骤**（纯源码阅读型，无需上板）：

1. 打开 [divu_int.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu_int.sv)，默认参数 `WIDTH=5`。
2. 设被除数 `a = 13`（`01101`）、除数 `b = 5`（`00101`），手算预期：商 `2`、余数 `3`。
3. 模拟 `INIT`：`{acc, quo} <= {5'b0, a, 1'b0}`，即 `acc=0, quo=01101_0... `（注意 `quo` 是 5 位，装的是 `a` 的低位移位后的初值）。
4. 逐拍套用 4.1.1 的伪代码，记录每拍的 `acc`、`quo`，共 `WIDTH=5` 拍。
5. 最后一拍读 `val = quo_next`（商）、`rem = acc_next[WIDTH:1]`（余数）。

**需要观察的现象**：每次「够减」时 `quo` 最低位变成 1、`acc` 变小；每次「不够减」时只是整体左移。

**预期结果**：商 `val = 2`、余数 `rem = 3`。如果你的某一步对不上，多半是忘了 `acc` 比 `quo` 宽 1 位，或忘了结束时余数要撤回一次移位。

#### 4.1.5 小练习与答案

**练习 1**：`divu_int.sv` 里为什么 `acc` 要比 `quo` 多 1 位？

**参考答案**：多出的 1 位是保护/借位位。一来比较 `acc >= b1` 时需要统一位宽（前面补 0）；二来做 `acc − b1` 后整体左移时，可能产生向高位的进位，没有这 1 位会丢失信息。

**练习 2**：`divu.sv`（无符号定点）相对 `divu_int.sv` 多迭代了 `FBITS` 次。这多出来的迭代在算什么？为什么 `divu` 不需要像 `divu_int` 那样输出余数？

**参考答案**：多出来的 `FBITS` 次迭代是在算商的**小数部分**（把整数除法延伸到定点小数）。`divu_int` 是整数除法，余数有数学意义所以输出；`divu` 是定点除法，小数部分已经被迭代算进商里了，剩下的余数低于表示精度、直接丢弃（截断），所以不输出。

---

### 4.2 div 的完整状态机：INIT/CALC/ROUND/SIGN

#### 4.2.1 概念说明

`divu` 只能处理无符号数。要做**有符号**除法，最干净的思路是：**先取绝对值，做无符号除法，最后把符号装回去**。这把问题分解成四步：

1. 取 `|a|`、`|b|`，并记下「两数符号是否不同」（决定结果正负）；
2. 用无符号迭代算 `|a| / |b|`；
3. 对结果做高斯舍入；
4. 按符号差异，把商还原成带符号的补码。

每一步对应状态机的一个状态，加上等待启动的 `IDLE`，共五个状态。这就是 `div.sv` 用 `enum` 显式写出的状态机（[div.sv:54](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L54)）。相比 `divu.sv` 用 `start/busy` 标志位的隐式控制，`div` 用 `enum` 把流程讲得更清楚——也正因为多了「舍入」和「符号」两个独立阶段，必须显式状态机。

> 注意一个关键设计：**取绝对值用 `WIDTHU = WIDTH−1` 位**（[div.sv:25](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L25)）。因为去掉符号位后，绝对值的数值部分只有 `WIDTH−1` 位。这也决定了 `SMALLEST`（\(-2^{WIDTH-1}\)）无法取绝对值——它的绝对值需要 `WIDTH` 位，放不下，所以必须当作溢出特判（见 4.3）。

#### 4.2.2 核心流程

`div.sv` 的状态机流转：

```
              start(且 b==0) ──────────────► IDLE (dbz=1)
              start(且 a或b==SMALLEST) ─────► IDLE (ovf=1)
IDLE ──start──► INIT ──► CALC(迭代 ITER 次) ──► ROUND ──► SIGN ──► IDLE
   ▲                                              │        │
   └──────────────────────────────────────────────┘        │
                          (done=1, valid=1) ◄──────────────┘
```

各状态职责：

- **IDLE**：等待 `start`。启动时做除零与 SMALLEST 溢出预检查，并寄存 `au=|a|`、`bu=|b|`、`sig_diff = a_sig ^ b_sig`。
- **INIT**：把 `{acc, quo}` 初始化为 `{{WIDTHU{1'b0}}, au, 1'b0}`，`i=0`，进入 CALC。
- **CALC**：跑 `ITER = WIDTHU + FBITS` 次迭代核心（与 `divu` 同一段组合逻辑）；在中途 `i == WIDTHU-1` 时检查溢出；跑完最后一拍进入 ROUND。
- **ROUND**：用「再迭代一次」得到的额外 1 位 + 余数，做高斯舍入。
- **SIGN**：按 `sig_diff` 把无符号商还原成补码，置 `done=1, valid=1`，回 IDLE。

完成一次除法的总时延约为 \(1(\text{IDLE锁存}) + 1(\text{INIT}) + \text{ITER}(\text{CALC}) + 1(\text{ROUND}) + 1(\text{SIGN})\) 个时钟周期。对默认 `WIDTH=8, FBITS=4`（`ITER=11`），约 15 拍后 `done` 拉高。

#### 4.2.3 源码精读

状态声明用 `enum`，一眼看清流程（[div.sv:54](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L54)）：

```verilog
enum {IDLE, INIT, CALC, ROUND, SIGN} state;
```

迭代次数与计数器位宽由参数自动推导（[div.sv:29-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L29-L30)）：

```verilog
localparam ITER = WIDTHU + FBITS;  // 迭代次数 = 无符号位宽 + 小数位
logic [$clog2(ITER):0] i;          // 多 1 位给 ROUND 那次额外迭代用
```

符号提取是纯组合逻辑（[div.sv:38-41](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L38-L41)）：取最高位作为符号，`a[WIDTH-1+:1]` 是「从第 WIDTH−1 位起取 1 位」的位选择写法。

INIT 状态完成初始化（[div.sv:58-63](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L58-L63)）：

```verilog
INIT: begin
    state <= CALC;
    ovf <= 0;
    i <= 0;
    {acc, quo} <= {{WIDTHU{1'b0}}, au, 1'b0};  // 被除数 au 装入长寄存器，最低位留 0 给首个商位
end
```

CALC 状态是主力（[div.sv:64-76](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L64-L76)）。它一边跑迭代，一边在 `i == WIDTHU-1` 时检查溢出，在 `i == ITER-1` 时切到 ROUND：

```verilog
CALC: begin
    if (i == WIDTHU-1 && quo_next[WIDTHU-1:WIDTHU-FBITSW] != 0) begin  // 溢出
        state <= IDLE; busy <= 0; done <= 1; ovf <= 1;
    end else begin
        if (i == ITER-1) state <= ROUND;  // 算完最后一拍进舍入
        i <= i + 1;
        acc <= acc_next;  // 锁存本次迭代结果
        quo <= quo_next;
    end
end
```

> 溢出判据 `quo_next[WIDTHU-1:WIDTHU-FBITSW] != 0`（默认参数下即 `quo_next[6:3] != 0`）的直觉是：**这些是会被后续移位「挤出」quo 寄存器的高位商位**。若它们非零，说明真正的商需要比 `WIDTHU` 位更多才能装下，结果落不进可用位宽 → 溢出。4.2.4 的跟踪例子里会在 `i=6` 这一拍触发这个检查。

迭代核心本身（`always_comb`）与 `divu` 完全同构，只是位宽换成了 `WIDTHU`，见 [div.sv:44-51](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L44-L51)，这里不再重复贴。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：完整跟踪 `div.sv` 计算 \((+1.5)/(-0.5)\) 的全过程，列出每拍的 `state`、`i`、`acc`、`quo`，验证最终商与符号。

**操作步骤**：

1. 采用**模块默认参数** `WIDTH=8, FBITS=4`（这样能直接对照源码的参数声明）。注意：本仓库的 cocotb 测试实际用 `WIDTH=9` 编译（见 [div.mk:14](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.mk#L14)），本练习为简化用默认值 8。
2. 编码输入（Q4 定点）：`a = +1.5 = 24 = 0001_1000`，`b = -0.5 = -8 = 1111_1000`。预期数学结果 \(-3.0\)，Q4 编码应为 \(-48 = 1101\_0000 = \mathtt{0xD0}\)。
3. 先算 IDLE 里寄存的量：`a_sig=0, b_sig=1`，`sig_diff = 1`（异号→结果为负）；`au = |a| = 24`，`bu = |b| = 8`。
4. 仿照 4.1 的迭代规则，逐拍推进。判定里 `acc` 与 `bu=8` 比较：`acc >= 8` 则减法上商 1，否则只移位上商 0。

**需要观察的现象**：前几拍 `acc` 逐拍翻倍积累（因为一直不够减），直到第 6 拍 `acc` 达到 12 才首次够减；`i=6` 那拍会经过溢出检查但平安通过；最后 `quo` 收敛到 48。

**预期结果**（每拍进入该状态时的寄存值）：

| 进入状态 | i | acc | quo | 本拍动作 | 说明 |
|----------|----|-----|-----|----------|------|
| INIT | 0 | — | — | `{acc,quo}<=0,48` | 装入 au=24（左移 1 位） |
| CALC | 0 | 0 | 48 | acc<8 → 移位 | 下一拍 acc=0, quo=96 |
| CALC | 1 | 0 | 96 | 移位 | acc=1, quo=64 |
| CALC | 2 | 1 | 64 | 移位 | acc=3, quo=0 |
| CALC | 3 | 3 | 0 | 移位 | acc=6, quo=0 |
| CALC | 4 | 6 | 0 | 移位 | acc=12, quo=0 |
| CALC | 5 | 12 | 0 | **12≥8 → 减+上商1** | acc=8, quo=1 |
| CALC | 6 | 8 | 1 | 溢出检查通过；8≥8→减+上商1 | acc=0, quo=3 |
| CALC | 7 | 0 | 3 | 移位 | acc=0, quo=6 |
| CALC | 8 | 0 | 6 | 移位 | acc=0, quo=12 |
| CALC | 9 | 0 | 12 | 移位 | acc=0, quo=24 |
| CALC | 10 | 0 | 24 | `i==ITER-1` → 转 ROUND；移位 | acc=0, quo=48 |
| ROUND | — | 0 | 48 | 额外位=0，不舍入 | quo 保持 48 |
| SIGN | — | — | 48 | `sig_diff=1` → `val<= {1'b1,-48}` | **done=1, valid=1** |

最终 `val = {1'b1, -48}`：48 的 7 位补码取负为 `1010000`，前面补符号位 1 得 `1101_0000 = 0xD0 = -48`，按 Q4 解码即 \(-3.0\)。与数学预期 \((+1.5)/(-0.5) = -3.0\) 完全一致，符号为负也正确。

**待本地验证**：上表是按源码逻辑手工推导的。建议你用 cocotb（见 4.3.4）把 `WIDTH/FBITS` 设成 8、输入这两个数，对照波形确认 `acc/quo/state` 的逐拍值与上表一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `div` 的 `ITER` 用 `WIDTHU + FBITS`，而 `divu` 用 `WIDTH + FBITS`？

**参考答案**：`div` 先把输入取绝对值，绝对值的数值部分只有 `WIDTH−1 = WIDTHU` 位，所以基于绝对值做迭代只需 `WIDTHU` 次就能消费完被除数，再加 `FBITS` 次算小数。`divu` 不取绝对值，被除数是完整的 `WIDTH` 位，所以是 `WIDTH + FBITS`。

**练习 2**：在上表的跟踪里，`i=6` 那拍 `quo_next` 算出来是 3（`0000011`），溢出检查 `quo_next[6:3]` 等于多少？为什么没有触发溢出？

**参考答案**：`quo_next[6:3] = 0000 = 0`，所以 `!= 0` 不成立，不溢出。这些高位商位全 0 意味着真正的商能装进 7 位 `quo` 寄存器、不会被后续移位挤丢。若它们非零，就说明商太大、落不进 `WIDTHU` 位，才会溢出。

---

### 4.3 舍入、符号修正与异常检测

#### 4.3.1 概念说明

**为什么定点除法要舍入？** 整数除法 `13/5` 的商 `2` 是精确的（剩下的是余数）。但定点除法要把小数也算进商：`13/5 = 2.6`，而 Q4 只能表示到 \(1/16\)，`2.6` 落不到整点上。我们必须决定把它表示成 `2.5625`（=41/16）还是 `2.625`（=42/16）。这个「就近选一个」的决定就是**舍入**。

`div.sv` 用的是**高斯舍入（round half to even，也叫银行家舍入）**，规则：

- 被丢弃的部分 **< 0.5 ULP**：向下（截断）；
- 被丢弃的部分 **> 0.5 ULP**：向上（商 +1）；
- 被丢弃的部分 **恰好 = 0.5 ULP**：向**偶数**靠（当前 LSB 为 1/奇数时 +1 变偶，为 0/偶数时不变）。

相比「四舍五入」，高斯舍入在大量运算里没有正向偏差，是 DSP 与统计计算里的标准选择。

**符号修正**则简单：无符号商 `quo` 算好后，若 `sig_diff=1`（两数异号），结果取负；否则原样。注意取负是在 `WIDTHU` 位上做、再前面补一位符号位，拼回 `WIDTH` 位的补码 `val`。

#### 4.3.2 核心流程

ROUND 状态的判断流程：

```
计算"再迭代一次"的 quo_next（多拿 1 位精度）
if quo_next[0] == 1:                        # 被丢弃的最高位是 1（≥ 0.5 ULP）
    if (quo[0]==1 或 余数≠0):  quo = quo+1   # >0.5，或 =0.5 且当前奇数 → 上舍入
    else:                      不变          # =0.5 且当前偶数 → 保持（向偶）
else:                                        # 被丢弃 < 0.5 ULP
    不变                                     # 截断
```

注意 `quo_next[0]` 是「再迭代一次」得到的**额外那一位**——它是比 `quo` 最低位还低一位的商位，正好充当舍入判定的「0.5 判定线」。`quo[0]` 是当前商的 LSB（决定奇偶），`acc_next`（余数）是否非零决定「是否恰好在半分点」。

SIGN 状态的符号还原：

```
if quo != 0:
    val = sig_diff ? {1'b1, -quo} : {1'b0, quo}
```

异常检测（在 IDLE 启动时，[div.sv:94-105](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L94-L105)）分两种，都**立即返回、不进 CALC**：

- **除零** `dbz`：`b == 0`。
- **溢出** `ovf`：`a == SMALLEST` 或 `b == SMALLEST`（绝对值放不下）。

另有**第二类溢出**在 CALC 中途触发（4.2.3 已讲）：商太大装不下。

#### 4.3.3 源码精读

ROUND 状态（[div.sv:77-83](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L77-L83)）——高斯舍入的全部代码：

```verilog
ROUND: begin  // Gaussian rounding
    state <= SIGN;
    if (quo_next[0] == 1'b1) begin  // next digit is 1, so consider rounding
        // round up if quotient is odd or remainder is non-zero
        if (quo[0] == 1'b1 || acc_next[WIDTHU:1] != 0) quo <= quo + 1;
    end
end
```

逐项对应 4.3.2 的规则：`quo_next[0]` 是判定线；`quo[0]==1`（当前奇数）或 `acc_next[WIDTHU:1]!=0`（余数非零，注意取的是高位、跳过最低位以对齐移位）任一成立就 +1。`quo_next[0]==0` 时什么都不做（截断）。三种情况全覆盖，恰好半分时由 `quo[0]` 的奇偶定去向——这就是「向偶」。

SIGN 状态（[div.sv:84-90](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L84-L90)）——符号还原：

```verilog
SIGN: begin  // adjust quotient sign if non-zero and input signs differ
    state <= IDLE;
    if (quo != 0) val <= (sig_diff) ? {1'b1, -quo} : {1'b0, quo};
    busy <= 0; done <= 1; valid <= 1;
end
```

`{1'b1, -quo}`：在 `WIDTHU` 位上对 `quo` 取负（补码），前面补 1 个符号位 1，得到负数的 `WIDTH` 位补码；`{1'b0, quo}` 则是正数（符号位 0）。

IDLE 里的异常预检查（[div.sv:94-105](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L94-L105)）：

```verilog
if (b == 0) begin                      // divide by zero
    ... dbz <= 1; ...
end else if (a == SMALLEST || b == SMALLEST) begin  // overflow
    ... ovf <= 1; ...
end else begin
    au <= (a_sig) ? -a[WIDTHU-1:0] : a[WIDTHU-1:0];  // |a|
    bu <= (b_sig) ? -b[WIDTHU-1:0] : b[WIDTHU-1:0];  // |b|
    sig_diff <= (a_sig ^ b_sig);
    ...
end
```

> 一个值得留意的细节：SIGN 里只有 `quo != 0` 时才给 `val` 赋值。若 `a == 0`（商本应为 0），`val` 不会被清零，而是保持上一次的值。在「每次计算前先复位」的使用方式下（cocotb 测试就是这么做的）这没问题，因为复位把 `val` 清成了 0；但若不复用地连续计算 `0/b`，`val` 会残留旧结果。这是阅读源码时需要知道的一条边界行为——库作者假设使用者会通过 `valid`/`done` 或复位来管理结果有效性。

#### 4.3.4 代码实践

**实践目标**：用本仓库自带的 cocotb 测试实际跑一遍 `div.sv`，观察舍入、符号与边界（除零/溢出）的真实行为。

**操作步骤**：

1. 安装依赖：Icarus Verilog（`iverilog`）、Python 的 `cocotb` 与 `spfpm`（测试用它做黄金参考模型，见 [div.py:9](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L9)）。大致命令：`pip install cocotb spfpm`，并用包管理器装 `iverilog`。
2. 进入测试目录：
   ```bash
   cd ThreePart/projf-explore/lib/maths/test
   ```
3. 运行 `div` 的全部测试（[Makefile:5-6](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/Makefile#L5-L6) 会调用 `div.mk`）：
   ```bash
   make div
   ```
   注意 `div.mk` 用 `-Pdiv.WIDTH=9 -Pdiv.FBITS=4` 覆盖了默认参数（[div.mk:14](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.mk#L14)），所以仿真用的是 9 位字长。
4. 挑几个有代表性的用例对照源码理解：
   - **符号**：`sign_2` 测 `-3/2`（[div.py:103-105](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L103-L105)），验证 SIGN 的取负；
   - **舍入**：`round_1` 测 `5.0625/2`（[div.py:119-122](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L119-L122)），`5.0625/2 = 2.53125`，Q4 半分点应向偶舍入；
   - **除零**：`dbz_1` 测 `2/0`（[div.py:239-241](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L239-L241)），断言 `dbz==1, valid==0`；
   - **溢出（结果太大）**：`ovf_1` 测 `8/0.25`（[div.py:276-277](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L276-L277)），结果 32 超出范围；
   - **溢出（SMALLEST 输入）**：`ovf_4` 测 `1/-16`（[div.py:342-344](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L342-L344)），在 `WIDTH=9` 下 `-16 = -256 = SMALLEST`，命中 IDLE 里的 `b==SMALLEST` 预检查。

**需要观察的现象**：测试用 `spfpm` 的 `FXfamily` 做黄金参考，把 DUT 的 `val` 与模型值比对（[div.py:42-62](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L42-L62)）。`done` 只应高一拍（[div.py:64-66](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L64-L66)）。

**预期结果**：所有不带 `expect_fail` 的测试通过；`nonbin_4`（`3.6/0.6`）和 `nonbin_5`（`0.4/0.1`）被标 `expect_fail=True`（[div.py:226,232](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L226)）——这两个用例的真值恰好在两个可表示点正中间且无法精确表示，DUT 与模型可能选到不同侧，属于已知的「舍入方向分歧」，不是 bug。

**待本地验证**：本环境未安装 `iverilog`/`cocotb`，以上为按源码与 Makefile 推导的预期，实际输出请你在本地运行 `make div` 后确认。

#### 4.3.5 小练习与答案

**练习 1**：用 `WIDTH=9, FBITS=4`（cocotb 测试的配置）解释为什么 `ovf_4` 测 `1/-16` 会溢出，而 `1/-15` 不会。

**参考答案**：`WIDTH=9` 时 `SMALLEST = -256`，Q4 解码即 \(-16.0\)。所以 `-16` 正好是 `SMALLEST`，命中 IDLE 里 `b == SMALLEST` 的预检查，立即判溢出（绝对值 \(|-256| = 256\) 放不进 `WIDTHU=8` 位）。而 `-15 = -240` 不是 `SMALLEST`，且 `1/-15 ≈ -0.0667` 结果在范围内，不溢出。

**练习 2**：ROUND 里若把判断改成「只要 `quo_next[0]==1` 就 `quo+1`」（即普通四舍五入），相对高斯舍入会有什么不同？哪个用例能看出差别？

**参考答案**：区别在「恰好半分点」：普通四舍五入总是向上（+1），高斯舍入则在当前 LSB 为偶数时保持不变、奇数时才 +1，长期无统计偏差。看 `round_1`（`5.0625/2 = 2.53125`，Q4 半分点）：高斯舍入向偶得 `2.5`，普通四舍五入得 `2.5625`。`div.py` 的模型用的也是高斯舍入，所以改了 DUT 后这个用例会对不上模型而失败。

**练习 3**：为什么除零和 SMALLEST 溢出要在 IDLE 里**提前**检测，而不是等 CALC 跑完再说？

**参考答案**：除零时除数是 0，迭代里的 `acc − b1` 与比较 `acc >= 0` 会陷入无意义循环、产不出有效商；SMALLEST 取绝对值会溢出（`|SMALLEST|` 超过 `WIDTHU` 位），`au/bu` 一开始就是错的。两者都在「还没开始算」时就能判定，提前检测既避免浪费十几个时钟周期跑出垃圾结果，也避免迭代里出现无法收敛的状态。

---

## 5. 综合实践

把本讲三个层面的知识串起来，做一个**对比阅读 + 行为预测**的小项目：

1. **并列阅读三个模块的迭代核心**：打开 [divu_int.sv:28-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu_int.sv#L28-L35)、[divu.sv:35-42](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/divu.sv#L35-L42)、[div.sv:44-51](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L44-L51)，确认这三段 `always_comb` 结构完全相同，只是位宽参数（`WIDTH` vs `WIDTHU`）不同。这能让你确信：**三个除法器共享同一个「恢复余数」心脏，差别全在外围控制逻辑。**

2. **画一张特性对照表**，栏目含：输入是否有符号、迭代次数、是否有状态机 enum、是否舍入（及舍入方式）、是否输出余数、异常检测种类。填完后你应该能用一句话向别人解释「从 `divu_int` 到 `div` 每一步加了什么」。

3. **预测并验证一个舍入用例**：选 `round_3`（`15.9375/2`，[div.py:129-132](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L129-L132)）。先用纸笔按 ROUND 状态的规则推出 DUT 应得的 Q4 结果，再在本地 `make div` 看测试日志里的 `dut val` 与 `model val`（[div.py:51-54](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/div.py#L51-L54)）是否与你的手算一致。

4. **（进阶）改参数观察时延**：把 `div.mk` 里的 `FBITS` 从 4 改成 6（保持 `WIDTH=9`），预测完成一次除法需要多少拍（提示：`ITER` 变了），再跑测试用 `done` 脉冲位置验证。注意这会缩小整数表示范围，要思考是否需要同时调 `WIDTH`。

> 这个综合练习把「读源码—画对照—手算预测—仿真验证」四步走完一遍，正是阅读任何硬件 IP 的标准流程。

## 6. 本讲小结

- 三个除法器共享**同一段恢复余数法迭代核心**（`always_comb` 里的移位-比较-减法-上商），差别全在外围：`divu_int`（整数+余数）→ `divu`（定点+截断）→ `div`（有符号+高斯舍入）。
- `div.sv` 用 `enum {IDLE,INIT,CALC,ROUND,SIGN}` 显式状态机把「取绝对值→迭代→舍入→还符号」四步串起来，思路是**把有符号除法拆成无符号除法 + 符号处理**。
- 迭代次数 `ITER = WIDTHU + FBITS`：前 `WIDTHU` 次消费被除数、定整数商；后 `FBITS` 次算小数部分。这是「定点除法」比「整数除法」多干的活。
- **溢出有两路**：IDLE 里预检 `a/b == SMALLEST`（绝对值放不下）；CALC 中途检商的高位是否会被挤出 `quo` 寄存器。除零 `b==0` 在 IDLE 即时返回 `dbz`。
- **ROUND 做高斯舍入（向偶）**：用「再迭代一次」的额外位 `quo_next[0]` 作 0.5 判定线，结合当前 LSB 奇偶与余数是否为零，决定是否 `quo+1`。
- **SIGN 还原符号**：`sig_diff` 为真时输出 `{1'b1, -quo}`，否则 `{1'b0, quo}`；`quo==0` 时不赋值（依赖外部复位清零，是一条值得留意的边界行为）。
- 本仓库自带 cocotb 测试（`test/div.py`），用 `spfpm` 做黄金参考模型，覆盖符号、舍入、除零、溢出与最小/最大边界，是验证理解的最佳工具。

## 7. 下一步学习建议

- **横向对比乘法**：本库的 [mul.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv) 同样是「有符号定点 + 高斯舍入」，但乘法单周期、靠 DSP，舍入逻辑却与 `div` 的 ROUND 思想相通。对比阅读能加深你对「定点结果如何落回 Q 格式」的理解。
- **开方**：[sqrt.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv) 与 [sqrt_int.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv) 也是逐位迭代算法，与除法的恢复余数法是近亲（都是「试位 + 比较」），结构上的对照会很有启发。
- **把除法用起来**：参考 projf 的 [Mandelbrot demo](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot)（对应大纲 u7-l5），看复数迭代里如何排布乘法/除法/开方的多周期握手（`start/busy/done/valid`），把本讲的「done 只高一拍、需配合 valid」用到真实数据通路上。
- **读官方讲解**：projf 作者的博文 [Division in Verilog](https://projectf.io/posts/division-in-verilog/)（README [第50行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/README.md#L50) 有链接）逐图讲解了这套算法的来历，是本讲最好的延伸读物。
