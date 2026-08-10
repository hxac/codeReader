# 输出饱和与定点截位：顶层最后一级流水线

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 FIR_x2 顶层最后一段 `always` 块（[07_FIR_x2/FIR_x2.v:L168-L172](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L168-L172)）为什么要把 48 位累加结果压回 32 位输出。
- 复述饱和判断的位级条件 `ADD_DATA[MULT_WIDTH-2] == ADD_DATA[MULT_WIDTH-3]` 的含义，并能据此预测输出是「直接截取」还是「钳位到最大/最小值」。
- 解释截位（truncation）与舍入（rounding）的区别，并指出本设计实际采用的是**截位**而非四舍五入。
- 理解 `BCKx2O_REG / LRCKx2O_REG / DATAO_REG` 这组最终流水线寄存器如何把数据与过采样时钟再次同步打拍后送出。

本讲是 u5 运算通路的收尾，承接 [u5-l2 ADD 累加积分器](u5-l2-add-accumulator.md) 输出的 48 位卷积和 `ADD_DATA`，把它变成对外可见的 32 位 `DATA_O`。

## 2. 前置知识

在进入本讲前，你需要先建立以下直觉（它们在前置讲义中已讲过，这里只做一句话回顾）：

- **累加器很宽，输出很窄**：上一讲 `ADD` 模块用 48 位（`MULT_WIDTH = DATA_WIDTH + COEF_WIDTH = 32 + 16`）寄存器 `ADDO_REG` 存放一个过采样样点的完整卷积和（256 次乘加），而最终对外输出的 PCM 数据只有 32 位（`DATA_WIDTH`/`DATAO_WIDTH`）。48 → 32 必然要丢掉信息，丢得不对就会产生可听的「咔哒」爆音。
- **二补码与符号扩展**：有符号定点数用最高位（MSB）表示符号，正数的最高若干位全是 0，负数的最高若干位全是 1，这叫符号扩展。判断一个宽位宽数「能否被无损截短」就是看它的高位是否构成合法的符号扩展。
- **饱和（saturation） vs 回绕（wraparound）**：当数值超出目标位宽能表示的范围时，「回绕」是直接丢弃高位（结果会从极大值跳到极小值，灾难性失真）；「饱和」则是把超界的值钳制到目标位宽能表示的最大或最小值（平滑削顶，听感上可接受）。本讲讲的就是饱和。
- **时钟随数据打拍**：从 u4-l1 起反复出现的理念——过采样时钟 `LRCKx2/BCKx2` 不是外部 PLL 给的，而是和数据一起逐级寄存延迟，保证两者在输出端同拍抵达。最后一级流水线同样遵守这一契约。

## 3. 本讲源码地图

本讲只涉及顶层模块一个文件，但它牵涉的内部信号横跨整条数据通路：

| 文件 | 作用 |
|---|---|
| [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) | 顶层。本讲聚焦其 [L74-L79](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L74-L79) 的位宽参数、[L88-L89](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L88-L89) 的 `ADD_DATA` 线、[L94-L98](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L94-L98) 的输出寄存器，以及核心的 [L167-L177](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L167-L177) 流水线块与输出赋值。 |
| [07_FIR_x2/Questa/report.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt) | Questa 覆盖率报告。[L11-L17](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt#L11-L17) 显示 FIR_x2 的分支/条件/语句覆盖全部 100%，是验证「饱和分支确实被触发」的实证。 |
| [06_ADD/ADD.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v) | 上一讲的累加器。仅需确认其 [L92-L93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L92-L93) 输出的 `ADD_O` 即本讲的 `ADD_DATA`，位宽 48、有符号。 |

## 4. 核心概念与源码讲解

### 4.1 饱和溢出判断

#### 4.1.1 概念说明

`ADD_DATA` 是 48 位有符号数，`DATAO_REG` 是 32 位有符号数。一个自然的想法是「直接砍掉高 16 位」，但只要累加结果的真实数值超出 32 位能表示的范围 \([-2^{31},\ 2^{31}-1]\)，砍高位就会让极大值瞬间变成极小值（回绕），在音频里就是刺耳的爆音。

**饱和（saturation）** 的作用就是：当数值超界时，不回绕，而是把它「按住」在边界上——正溢出钳到 `0x7FFF_FFFF`（\(2^{31}-1\)），负溢出钳到 `0x8000_0000`（\(-2^{31}\)）。这等效于一个软限幅器（soft limiter），听感上是平滑的削顶，远好于回绕。

那么「是否超界」怎么在硬件里用一两根线判断？答案是**保护位（guard bit）法**：在真正要保留的输出符号位之上，额外留 1 位作为「溢出探头」，比较这 1 位与输出符号位是否相等即可。本设计里：

- `ADD_DATA` 的 48 位按下表分层：

| 位段 | 位号 | 角色 |
|---|---|---|
| 符号位 | bit 47 | 48 位数的真实符号（MSB） |
| 保护位 | bit 46 = `MULT_WIDTH-2` | 溢出探头，与输出符号位比较 |
| 输出符号位 | bit 45 = `MULT_WIDTH-3` | 截取后 32 位输出的 MSB |
| 输出数据 | bit 44 … bit 14 | 截取后 32 位输出的低 31 位 |
| 丢弃 | bit 13 … bit 0 | 截位丢掉的低位（见 4.2） |

#### 4.1.2 核心流程

饱和判断的伪代码：

```
if (ADD_DATA[46] == ADD_DATA[45]):
    # 高位构成合法符号扩展 → 数值落在 32 位范围内 → 不饱和
    DATAO = ADD_DATA[45:14]                 # 直接截取（见 4.2）
else:
    # 保护位与输出符号位不一致 → 超界 → 饱和
    if ADD_DATA[46]==0 and ADD_DATA[45]==1: # 正溢出
        DATAO = 0x7FFF_FFFF                  # 钳到最大正数
    else:                                    # ADD_DATA[46]==1, [45]==0 负溢出
        DATAO = 0x8000_0000                  # 钳到最小负数
```

为什么只比 `bit46` 和 `bit45`，而不比 `bit47`？这是一个**带前提的设计**：它假设真实累加结果的幅度总小于 \(2^{46}\)，使得 `bit47` 永远等于 `bit46`（即高位始终是合法符号扩展）。在这个前提下，`bit46` 就是「真实符号」，`bit46==bit45` 等价于「bit47/46/45 三位全同、数值落在 \([-2^{45},\ 2^{45}-1]\) 范围内」，比较两位即可。

这个前提不是空中楼阁：它由系数生成器（见 [u6-l1 系数生成](u6-l1-fir-coefficient-generation.md)）的「抽头和溢出检查（MAX_TOTAL）」从源头保证——系数设计阶段就确保最大可能的累加值不会冲破 \(2^{46}\)。换句话说，饱和逻辑和系数设计是配套的。

#### 4.1.3 源码精读

核心判断就在这一行的条件部分：

[07_FIR_x2/FIR_x2.v:L171](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L171)

```verilog
DATAO_REG <= (ADD_DATA[MULT_WIDTH-2] == ADD_DATA[MULT_WIDTH-3]) ? <不饱和分支> : <饱和分支>;
```

代入 `MULT_WIDTH=48`：条件就是 `ADD_DATA[46] == ADD_DATA[45]`，即「保护位是否等于输出符号位」。这一句同时承担了「检测溢出」和「选择两条数据通路」两件事，是整段的灵魂。

而 `ADD_DATA` 这根线的来源，在顶层实例化 `ADD` 模块时连到其输出 `ADD_O`：

[07_FIR_x2/FIR_x2.v:L88-L89](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L88-L89) 声明 `wire signed [MULT_WIDTH-1:0] ADD_DATA;`，[L162](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L162) `.ADD_O(ADD_DATA)`。对应 [06_ADD/ADD.v:L92-L93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L92-L93) `assign ADD_O = ADDO_REG;`——也就是上一讲那个 48 位累加寄存器。

**实证**：饱和分支不是「写了但跑不到」的死代码。覆盖率报告 [07_FIR_x2/Questa/report.txt:L11-L17](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt#L11-L17) 显示 FIR_x2 的 Branches 为 2/2 = 100%、Conditions 1/1、Statements 7/7，说明在默认的 1 kHz 正弦测试激励下，第 171 行三元表达式的两个分支都被执行到——饱和路径确实被触发了。

#### 4.1.4 代码实践

**目标**：确认饱和分支在默认仿真中会被命中。

1. 打开 [07_FIR_x2/Questa/report.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt)，定位到 `Design Unit: work.FIR_x2` 一节。
2. 读出 Branches / Conditions / Statements 三行的 Hits 与 Coverage。
3. 按 [u1-l3 仿真流程](u1-l3-simulation-flow.md) 在本地 Questa 跑一次顶层仿真（默认读入 `PCM_1kHz_44100fs_32bit.txt`）。
4. 重新生成覆盖率报告，观察 FIR_x2 的 Branches 是否仍为 2/2。

**需要观察的现象**：报告中 FIR_x2 的 Branches 应为 `2 / 2 / 0 / 100.00%`，即两个分支（不饱和、饱和）都被命中过、Misses 为 0。

**预期结果**：饱和分支被命中，说明 1 kHz 满幅附近的正弦经滤波后，偶发的过冲会触发钳位。若你把测试信号换成极小幅度（例如把 `PCM_*.txt` 的样点统一缩小 100 倍），饱和分支可能不再被命中，Branches 的 Hits 会下降——这反向印证了饱和判断与信号幅度的关系。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把饱和判断改成只比较 `bit47` 和 `bit45`（跳过保护位 `bit46`），会有什么问题？

**参考答案**：`bit47` 与 `bit45` 之间隔了 `bit46`。当数值落在 \([2^{45},\ 2^{46})\) 这种「正溢出但未到 `bit47`」的带区时，`bit47=0`、`bit45=1`，二者确实不等、能检出；但这要求每次都跨两位比较，且会让「正好 `bit46` 与 `bit45` 不同、但 `bit47` 与 `bit45` 恰好相同」的边界情况判断混乱。更关键的是，本设计的截取窗口是以 `bit45` 为输出 MSB、`bit46` 为紧邻保护位来安排的（见 4.2），保护位必须紧贴输出符号位才有几何意义，所以比较 `bit46==bit45` 是与截取方案自洽的唯一选择。

**练习 2**：饱和逻辑依赖「`bit47` 恒等于 `bit46`」这一前提。如果某次系数设计失误导致累加结果冲到了 \(+2^{46}\)（`bit46=1, bit45=0, bit47=0`），输出会变成什么？正确值又该是什么？

**参考答案**：此时 `bit46(=1) != bit45(=0)`，走饱和分支，输出 `{bit46=1, 31{bit45=0}} = 0x8000_0000`（最小负数 \(-2^{31}\)）。但 \(+2^{46}\) 是个**正**数，正确做法应钳到最大正数 `0x7FFF_FFFF`。可见一旦前提被破坏，正溢出会被误判成负满量程——这正是为什么 [u6-l1](u6-l1-fir-coefficient-generation.md) 要做 MAX_TOTAL 抽头和检查，从源头杜绝这种情况。

---

### 4.2 定点截位舍入

#### 4.2.1 概念说明

把 48 位压到 32 位，除了「超界怎么办」（4.1 的饱和），还有「不超界时怎么取 32 位」（本节的截位）。先澄清两个常被混淆的术语：

- **截位（truncation）**：直接丢掉低位，等价于向 \(-\infty\) 方向取整（对二补码而言是 floor）。实现最简单，零额外硬件。
- **舍入（rounding）**：在截位前先加上半个 LSB（即加 \(2^{\text{丢位数}-1}\)），实现「四舍五入」到最近整数，误差更小、对称性更好，但要多一个加法器。

本设计的标题虽写作「定点舍入」，但**源码里实际只有截位、没有加偏置**（见 4.2.3）。这一点务必记牢：它是一种「向负无穷取整」的定点压缩，最大量化误差为 1 个输出 LSB。

为什么丢的恰好是低 14 位？因为系数是按定点分数量化的（系数生成细节见 [u6-l1](u6-l1-fir-coefficient-generation.md)）。把累加和右移 14 位（\(\div 2^{14}\)）正好抵消系数带来的 14 位定标，让输出回到与输入相同的「整数 PCM」量纲：

\[
\text{DATA\_O} \approx \left\lfloor \frac{\text{ADD\_DATA}}{2^{14}} \right\rfloor,\quad \text{当 } \text{ADD\_DATA} \in [-2^{45},\ 2^{45}-1]
\]

#### 4.2.2 核心流程

两条数据通路，由 4.1 的条件二选一：

```
# 通路 A：不饱和（bit46 == bit45）
DATAO = ADD_DATA[45 : 14]        # 取 bit45 作符号位、bit14 作 LSB，共 32 位

# 通路 B：饱和（bit46 != bit45）
DATAO = { ADD_DATA[46], {31{ADD_DATA[45]}} }   # 用真实符号位 bit46 钳位
```

通路 A 的位段 `[45:14]` 正好 32 位（\(45-14+1=32\)），`bit45` 是输出 MSB/符号位，`bit14` 是输出 LSB，丢掉的是 `[13:0]` 共 14 位低位与 `[47:46]` 共 2 位高位（其中 `bit46` 已被条件用掉、`bit47` 是符号扩展）。

通路 B 的构造很巧妙：它**用 `bit46` 当钳位后的符号位**，后面跟 31 个 `bit45`。

| `bit46` | `bit45` | 钳位结果 | 含义 |
|---|---|---|---|
| 0 | 1 | `{0, 31'h7FFFFFFF}` = `0x7FFF_FFFF` | 正溢出 → 最大正数 \(2^{31}-1\) |
| 1 | 0 | `{1, 31'h0000000}` = `0x8000_0000` | 负溢出 → 最小负数 \(-2^{31}\) |

注意 `{31{ADD_DATA[45]}}` 是「按真实符号填满」：正溢出时填 1（凑成全 1 的最大正数），负溢出时填 0（凑成只有符号位的负满量程）。这与「符号扩展」的方向一致。

#### 4.2.3 源码精读

整条表达式集中在一行，把它拆成三段看：

[07_FIR_x2/FIR_x2.v:L171](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L171)

```verilog
DATAO_REG <= (ADD_DATA[MULT_WIDTH-2] == ADD_DATA[MULT_WIDTH-3])               // ① 条件
           ? ADD_DATA[MULT_WIDTH-3 : MULT_WIDTH-3-(DATAO_WIDTH-1)]            // ② 不饱和：截取 [45:14]
           : {ADD_DATA[MULT_WIDTH-2], {(DATAO_WIDTH-1){ADD_DATA[MULT_WIDTH-3]}}}; // ③ 饱和：钳位
```

- ② 不饱和分支：代入 `MULT_WIDTH=48, DATAO_WIDTH=32` 得 `ADD_DATA[45:14]`，纯位选择，**没有任何加法**——这就是「截位而非舍入」的铁证。若要改成舍入，需要在这之前先 `ADD_DATA + (1<<13)`，本设计没有这么做。
- ③ 饱和分支：`{ADD_DATA[46], {31{ADD_DATA[45]}}}`，用重复复制操作符 `{31{...}}` 生成 31 个相同的 `bit45`，再拼上 `bit46` 作符号位，恰好 32 位。

位宽参数派生自 [07_FIR_x2/FIR_x2.v:L74-L79](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L74-L79) 的 `localparam`：`MULT_WIDTH = DATA_WIDTH + COEF_WIDTH = 48`，而 `DATAO_WIDTH` 是 [L58](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L58) 的独立参数（默认 32）。整段截取/钳位都只用这两个 localparam/parameter 表达，没有硬编码 48 或 32，因此改位宽时这一行无需手改——但注意「丢 14 位」这个定标关系是和系数生成绑定的，不能随意改 `DATAO_WIDTH`（详见 [u6-l1](u6-l1-fir-coefficient-generation.md)）。

#### 4.2.4 代码实践

**目标**：人为构造两个会触发饱和的 `ADD_DATA` 值，手工套用第 171 行，预测 `DATAO_REG`。

1. 先写出 48 位二补码（用十六进制表示，共 12 个 hex 位）。
2. **正溢出样例**：取 `ADD_DATA = 0x0000_2000_0000_0000`（即 \(+2^{45}\)）。
   - 读位：`bit47=0, bit46=0, bit45=1`。
   - 条件：`bit46(0) == bit45(1)`？否 → 走饱和分支 ③。
   - 结果：`{bit46=0, 31{bit45=1}} = 0x7FFF_FFFF`（最大正数）。
3. **负溢出样例**：取 `ADD_DATA = 0xFFFF_C000_0000_0000`（即 \(-2^{46}\)，注意它仍在前提范围 \((-2^{46}, 2^{46})\) 边界、`bit47==bit46==1`）。
   - 读位：`bit47=1, bit46=1, bit45=0`。
   - 条件：`bit46(1) == bit45(0)`？否 → 走饱和分支 ③。
   - 结果：`{bit46=1, 31{bit45=0}} = 0x8000_0000`（最小负数）。
4. **不饱和样例（对照）**：取 `ADD_DATA = 0x0000_1FFF_FFFF_FFFF`（即 \(2^{45}-1\)，恰在不饱和上界）。
   - 读位：`bit47=0, bit46=0, bit45=0`。
   - 条件：`bit46(0) == bit45(0)`？是 → 走截取分支 ②。
   - 结果：`ADD_DATA[45:14]` = `0x7FFF_FFFF`（符号位 0 + 31 个 1）。可见「不饱和的上界」与「饱和的正钳位值」数值相同，过渡是连续的。

**需要观察的现象**：三个样例的输出分别是 `0x7FFF_FFFF`、`0x8000_0000`、`0x7FFF_FFFF`，且正方向不饱和上界与饱和钳位值相等（无跳变），负方向同理。

**预期结果**：如上。若想在仿真中真正观测到钳位，可在 testbench 里用 `force u_FIR_x2.ADD_DATA = 48'h0000_2000_0000_0000;` 强行注入（注意 `ADD_DATA` 是模块内部 wire，需用层次路径），然后在下一个 `posedge MCLK_I` 查看 `DATA_O`。具体波形**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把本设计的「截位」改成「四舍五入」，最少要改哪里？

**参考答案**：在第 171 行之前，先把 `ADD_DATA` 加上半个输出 LSB 再截位，即把分支 ② 的源从 `ADD_DATA` 换成 `ADD_DATA + (1 << 13)`（因为丢 14 位，半个 LSB 是 \(2^{13}\)）。注意加法可能改变 `bit46/bit45` 从而影响饱和判断，因此严谨做法是**先做饱和判断（用原始 `ADD_DATA`），再在选定截取窗口后加偏置**，或者把饱和上界相应抬升半个 LSB。本设计为追求对称与硬件极简选择了纯截位。

**练习 2**：截位的最大量化误差是多少？舍入又是多少？

**参考答案**：截位丢掉低 14 位，误差范围 \([0,\ 2^{14}-1]\) 个 `ADD_DATA` 原始单位，即不到 1 个输出 LSB（\(2^{14}\) 原始单位 = 1 输出 LSB），且始终偏向负方向。舍入的误差范围是 \([-2^{13},\ +2^{13}]\)，幅度减半且关于零对称，音质更优，代价是一个加法器。

---

### 4.3 输出流水线寄存

#### 4.3.1 概念说明

饱和/截位只是「算出 32 位结果」，这个结果还要和过采样时钟 `LRCKx2/BCKx2` 一起被寄存一拍才能送出芯片。这一拍就是顶层最后的 `always` 块，由三个寄存器 `BCKx2O_REG / LRCKx2O_REG / DATAO_REG` 构成。

为什么数据已经算好了还要再打一拍？两个原因：

1. **时序**：第 171 行的三元表达式含位选择与拼接，是组合逻辑；直接对外输出会让 `DATA_O` 的翻转依赖 `ADD_DATA` 这一长组合链（乘法→累加→比较→选择），不利于满足建立时间。寄存一拍把它切断。
2. **对齐契约**：从 u4-l1 起就强调「时钟随数据打拍」。`LRCKx2` 一路从 `FIR_COEF` → `MULT` → `ADD` 已经被打了几拍，数据也同步被打了几拍。最后这一级把 `DATAO_REG`、`LRCKx2O_REG`、`BCKx2O_REG` 在**同一个 `posedge MCLK_I`** 里一起更新，确保三者到达输出管脚时仍是逐样点对齐的——下游 DAC 才能正确锁存。

#### 4.3.2 核心流程

```
always @(posedge MCLK_I):
    BCKx2O_REG  <= BCKx2O_wire      # 来自 ADD 的 BCKx2_O
    LRCKx2O_REG <= LRCKx2O_wire     # 来自 ADD 的 LRCKx2_O（= 过采样样点节拍）
    DATAO_REG   <= <饱和/截位结果>   # 来自 ADD_DATA 的 32 位结果

# 输出选择
BCKx2_O  = (WADDR_WIDTH >= 7) ? BCKx2O_REG : MCLK_I   # 地址够宽才用派生时钟
LRCKx2_O = LRCKx2O_REG                                # 恒用寄存版
DATA_O   = DATAO_REG                                  # 恒用寄存版
```

注意 `BCKx2_O` 的阈值选择 `(WADDR_WIDTH >= 7)`：它与 `ADD`、`MULT` 里的同一道阈值（[06_ADD/ADD.v:L95](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L95)）一致。当滤波器地址宽度太小（抽头很少，`WADDR_WIDTH<7`）时，派生的 `BCKx2` 不可靠，于是兜底直接输出 `MCLK_I`。默认 `WADDR_WIDTH=8`，走派生寄存版 `BCKx2O_REG`。

#### 4.3.3 源码精读

最终流水线块与输出赋值：

[07_FIR_x2/FIR_x2.v:L167-L177](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L167-L177)

```verilog
/* Pipeline */
always @ (posedge MCLK_I) begin
    BCKx2O_REG  <= BCKx2O_wire;
    LRCKx2O_REG <= LRCKx2O_wire;
    DATAO_REG   <= (ADD_DATA[...] == ...) ? ... : ...;   // 即 4.2 的第 171 行
end

/* Output Assign */
assign BCKx2_O  = (WADDR_WIDTH >= 7) ? BCKx2O_REG : MCLK_I;
assign LRCKx2_O = LRCKx2O_REG;
assign DATA_O   = DATAO_REG;
```

三个寄存器的声明与初值在 [07_FIR_x2/FIR_x2.v:L94-L98](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L94-L98)：`BCKx2O_REG`、`LRCKx2O_REG` 初值 0，`DATAO_REG` 初值全 0（上电静音）。它们对应的「未寄存」输入 `BCKx2O_wire/LRCKx2O_wire` 来自 [L163-L164](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L163-L164) 实例化 `ADD` 时的 `.LRCKx2_O(LRCKx2O_wire)`、`.BCKx2_O(BCKx2O_wire)` 连线。

这也是全模块**唯一的 `always` 块**（顶层其余子模块的逻辑都封装在各自文件里）。它既是数据通路的终点站，也是「时钟随数据打拍」链条的最后一环。

#### 4.3.4 代码实践

**目标**：数清一个输入样点从 `DATA_I` 到 `DATA_O` 一共经过多少级 `MCLK` 寄存，验证数据与 `LRCKx2_O` 同拍到达。

1. 打开顶层 [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v)，沿 `RDATA`(DATA_BUFFER) → `COEF`(FIR_COEF) → `MULT_DATA`(MULT) → `ADD_DATA`(ADD) → `DATAO_REG`(本讲) 追踪。
2. 对照各子模块累计读延迟：`DATA_BUFFER` 的 SDPRAM 取 `OUTPUT_REG="TRUE"` 为 2 拍（见 [u3-l3](u3-l3-sdpram-primitive.md)）；`FIR_COEF` 的 SPROM 同样 2 拍（见 [u4-l3](u4-l3-sprom-primitive.md)）；`MULT` 输入寄存 1 拍 + 输出寄存 1 拍 = 2 拍（见 [u5-l1](u5-l1-mult-pipeline.md)）；`ADD` 内 `MULT_REG` 1 拍 + 累加/输出节拍；本讲再 1 拍。
3. 同步追踪 `LRCKx2`：它在 `FIR_COEF` 打 2 拍、`MULT` 打 2 拍、`ADD` 经 `LRCKx2_REG` 1 拍、本讲 `LRCKx2O_REG` 1 拍。

**需要观察的现象**：`DATAO_REG` 与 `LRCKx2O_REG` 在**同一个 `posedge MCLK_I`** 被赋值（同一 always 块的三条非阻塞赋值），因此二者在输出端必然同拍变化。

**预期结果**：数据通路与节拍通路的寄存级数相互匹配，最终 `DATA_O` 的每一个有效样点都对齐 `LRCKx2_O` 的同一次翻转。这正是「对齐契约」在最后一级的兑现。具体拍数清点**待本地验证**（建议在波形里用光标测量 `DATA_I` 有效到 `DATA_O` 有效之间的 `MCLK` 周期数）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DATAO_REG`、`LRCKx2O_REG`、`BCKx2O_REG` 必须放在**同一个** `always` 块里？

**参考答案**：因为它们必须**在同一时钟沿同步更新**才能保持对齐。若拆到不同 `always` 块甚至不同 always 的敏感列表有差异，三者之间就会出现 1 拍甚至更多的相对偏移，破坏「数据与节拍同拍抵达」的契约，下游 DAC 会错锁样点。非阻塞赋值 `<=` 配合同一敏感列表，是保证一组信号齐步走的标准写法。

**练习 2**：`BCKx2_O` 在 `WADDR_WIDTH < 7` 时兜底到 `MCLK_I`，这意味着什么？为什么 `LRCKx2_O` 没有同样的兜底？

**参考答案**：`BCKx2` 是位时钟的 2 倍频，地址位宽太小（滤波器抽头极少）时，`SPROM_CONT` 派生 `BCKx2` 所需的位索引（`CADDR_REG[W-7]`）会落到负位、无法生成，于是兜底用 `MCLK_I` 保证至少有个时钟跑。而 `LRCKx2` 由地址最高位派生（见 [u4-l2](u4-l2-sprom-cont-polyphase-clock.md)），只要 `RADDR_WIDTH>=1` 就一定能生成，不需要兜底。所以二者阈值逻辑不同。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「**饱和行为观测与边界推演**」小任务：

1. **跑通仿真并看覆盖率**：按 [u1-l3](u1-l3-simulation-flow.md) 在 Questa 跑顶层仿真，确认 [report.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt) 中 FIR_x2 的 Branches = 2/2。这说明饱和分支被默认 1 kHz 正弦触发过。

2. **在波形里抓一次饱和**：在 `run.do` 里把 `DATA_O`、`ADD_DATA`（用层次路径 `u_FIR_x2.ADD_DATA`）、`LRCKx2_O` 加入波形。运行后找到 `DATA_O` 出现 `0x7FFF_FFFF` 或 `0x8000_0000` 的时刻，回看同一时刻的 `ADD_DATA`：
   - 若 `DATA_O=0x7FFF_FFFF`，应能观察到 `ADD_DATA` 的 `bit46=0, bit45=1`（正溢出）。
   - 若 `DATA_O=0x8000_0000`，应能观察到 `bit46=1, bit45=0`（负溢出）。

3. **手算对照**：对你在第 2 步抓到的那次饱和，把 `ADD_DATA` 的 48 位十六进制抄下来，套用 [L171](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L171) 的三段（条件 / 截取 / 钳位）手算一遍，验证与波形中的 `DATA_O` 一致。

4. **改信号看覆盖变化**：把测试输入文件换成一个小幅度信号（或把 `PCM_1kHz_44100fs_32bit.txt` 的样点统一除以一个常数另存），重新跑仿真并生成覆盖率报告，观察 FIR_x2 的 Branches Hits 是否从 2 降到 1（饱和分支不再被命中）。这一步直观印证「饱和只在信号足够大时发生」。

若本地暂无 Questa 环境，第 1～4 步的精确数值**待本地验证**，但你可以先完成第 3 步的纸面推演——它只依赖第 171 行的位级逻辑，不依赖任何工具。

## 6. 本讲小结

- FIR_x2 顶层**唯一**的 `always` 块（[L168-L172](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L168-L172)）把 48 位累加和 `ADD_DATA` 压回 32 位输出 `DATA_O`，核心是**饱和 + 截位**。
- 饱和判断只用一根保护位：`ADD_DATA[46] == ADD_DATA[45]` 相等则不饱和、不等则钳位；这依赖「`bit47` 恒等于 `bit46`」的前提，该前提由 [u6-l1](u6-l1-fir-coefficient-generation.md) 的系数抽头和检查（MAX_TOTAL）保证。
- 截位是**纯位选择 `ADD_DATA[45:14]`**，丢低 14 位、`bit45` 作输出符号位、`bit46` 作保护位——**没有加偏置，是截位而非四舍五入**，最大误差不到 1 个输出 LSB。
- 饱和分支不是死代码：[report.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt) 显示 FIR_x2 Branches 2/2 = 100%，默认 1 kHz 正弦就会触发钳位。
- `BCKx2O_REG/LRCKx2O_REG/DATAO_REG` 在同一 `posedge MCLK_I` 同步更新，是「时钟随数据打拍」对齐契约的最后一环；`BCKx2_O` 沿用 `WADDR_WIDTH>=7` 阈值，地址太窄时兜底到 `MCLK_I`。
- 正溢出钳到 `0x7FFF_FFFF`、负溢出钳到 `0x8000_0000`，与不饱和上界数值连续、无跳变。

## 7. 下一步学习建议

本讲结束了 u5 运算通路（乘法→累加→饱和输出），整条信号链的 RTL 已全部走完。接下来建议：

- 进入 **[u6-l1 FIR 系数生成](u6-l1-fir-coefficient-generation.md)**：弄清系数是如何量化的、为什么丢的恰好是 14 位、MAX_TOTAL 抽头和检查如何保证本讲的「`bit47`==`bit46`」前提。本讲的饱和逻辑离开它就无从谈起。
- 阅读 **[u6-l3 PSL 断言与覆盖率](u6-l3-psl-assertions-coverage.md)**：本讲多次引用 `report.txt`，那里会系统讲解覆盖率报告各栏（Branches/Conditions/Statements/Assertions）的含义，以及如何为饱和分支补充断言。
- 若关心移植，看 **[u6-l4 FPGA 移植与开发板示例](u6-l4-fpga-porting-examples.md)**：饱和/截位这段纯组合逻辑跨厂商无忧，但 `DATAO_WIDTH` 与系数定标的耦合在换平台时需要一并核对。
