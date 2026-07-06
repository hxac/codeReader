# ROM 与状态控制模块

## 1. 本讲目标

本讲专门拆解 `ROM_16.v`、`ROM_8.v`、`ROM_4.v`、`ROM_2.v` 这四个「ROM 状态控制」模块。学完后你应该能够：

- 说清楚每个 ROM 模块承担的**两重职责**：既存旋转因子、又产生 `state` 状态控制信号。
- 看懂 `count` / `s_count` 计数器如何通过分段比较，生成 `2'b00 / 2'b01 / 2'b10` 三态信号去驱动 `radix2` 蝶形单元。
- 解释「等待 / 前半 / 后半」三段的计数阈值为何是 \(16/8/4/2\)，以及它与上一级移位延时深度（u3-l3）和 radix-2 DIF 分组（u2-l1）的对应关系。
- 读懂 `FFT.v` 顶层里 `in_valid → rom16 → rom8 → rom4 → rom2` 的 valid 菊花链，理解各级状态机如何与数据流自动对齐。
- 识别出 `ROM_16` 与 `ROM_8/4/2` 在编码风格上的真实差异（valid 自保持锁存 vs. 死代码），并具备「读源码、不轻信注释」的鉴别力。

> 本讲是 u3（核心 RTL 拆解）的最后一讲，承接 u3-l2（蝶形单元）和 u3-l3（移位延时），并为 u4-l1（五级流水线串讲）铺好「控制时序」这一块。

## 2. 前置知识

阅读本讲前，请先在脑中准备好下面几块拼图（均来自前置讲义）：

1. **radix2 蝶形的三态机（u3-l2）。** `radix2` 模块本身是纯组合逻辑、无状态，它靠外部送来的 2 位 `state` 信号分时复用：`2'b00` 等待、`2'b01` first half（算和/差）、`2'b10` second half（差回流后乘旋转因子）、`2'b11` 禁止。本讲要回答的问题就是：**这个 `state` 信号从哪儿来？** 答案正是 ROM 模块。

2. **旋转因子与定点表示（u2-l2）。** 旋转因子 \(W_N^k=\cos(2\pi k/N)-j\sin(2\pi k/N)\) 被定点量化为 \(\times 256\) 的 24 位二进制补码。本讲会看到这些定点值是如何以 `case` 查表的形式硬编码进 ROM 的。

3. **移位延时深度逐级减半（u3-l3）。** `shift_16/8/4/2/1` 的延时深度是 \(16/8/4/2/1\)。本讲会发现，ROM 的「等待段」阈值恰好也是 \(16/8/4/2\)——这不是巧合，而是 SDC（单路延迟换向器）反馈回路对控制时序的硬性要求。

4. **radix-2 DIF 的五级分解（u2-l1）。** 32 点 FFT 分 5 级，每级需要的旋转因子个数减半：第 1 级 16 个、第 2 级 8 个、第 3 级 4 个、第 4 级 2 个、第 5 级 1 个（恒为 1，故不需要 ROM）。本讲会用源码证实这条规律。

> 两个 Verilog 小术语先点一下：**时序 always 块**（`always@(posedge clk ...)`）描述寄存器，按节拍更新；**组合 always 块**（`always @(*)`）描述下一拍取值（常以 `next_xxx` 命名）和纯组合输出。ROM 模块就是用「时序块存计数器 + 组合块算 state/查表」这一对 always 搭起来的。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|------------|
| [RTL/ROM_16.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v) | 第 1 级 ROM（存 16 个 \(W_{32}^k\) + 产生 state） | 单计数器风格 + valid 自保持锁存 |
| [RTL/ROM_8.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_8.v) | 第 2 级 ROM（存 8 个 \(W_{16}^k\) + 产生 state） | 双计数器（count + s_count）风格 |
| [RTL/ROM_4.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_4.v) | 第 3 级 ROM（存 4 个 \(W_8^k\)） | 与 ROM_8 同构，阈值更小 |
| [RTL/ROM_2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v) | 第 4 级 ROM（存 2 个 \(W_4^k\)） | 最小一级，用于画时序图 |
| [RTL/radix2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v) | state 信号的**消费者** | `case(state)` 如何分派三段行为 |
| [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) | 顶层例化与连线 | valid 菊花链接线；第 5 级为何无 ROM |

> 说明：第 5 级（`radix_no5`）旋转因子被常数化为 `256+j0`，所以**没有对应的 ROM_1**，其 `state` 由 `FFT.v` 内部的 `no5_state` 逻辑生成。这是本讲在「菊花链」一节会顺手点出的边界情况，详细时序留到 u4-l3。

## 4. 核心概念与源码讲解

### 4.1 计数器与 state 生成

#### 4.1.1 概念说明

`radix2` 蝶形单元自己没有时钟、没有状态，它每一次该做「等待 / 算和差 / 乘旋转因子」中的哪一件事，完全由一个 2 位输入 `state` 决定。那么 `state` 谁来产生？答案就是和蝶形并排的 ROM 模块。

ROM 模块内部有一个（或两个）自由运行的计数器，每来一拍时钟、且输入有效时加 1。**`state` 就是这个计数器当前值的分段函数**：计数器还小的时候输出 `2'b00`（等待），计数器进入中段输出 `2'b01`（first half），进入后段输出 `2'b10`（second half）。换句话说，ROM 用一个计数器把时间轴切成三段，从而把蝶形单元的状态机「按时序排好」。

这样设计的好处是：**控制时序被本地化在 ROM 里**，蝶形单元保持极简的纯组合逻辑（u3-l2），两者各司其职、解耦清晰。

#### 4.1.2 核心流程

ROM 产生 `state` 的通用流程（伪代码）：

```
每拍 (posedge clk):
    若 in_valid 或 内部 valid 拉高:  count ← count + 1
    否则:                            count 保持

组合输出 state:
    若 count < N:              state = 2'b00   // 等待段
    若 count ≥ N 且 处于前半:   state = 2'b01   // first half
    若 count ≥ N 且 处于后半:   state = 2'b10   // second half
    (ROM_16 还有 count ≥ 3N:    state = 2'b11)  // 禁止/帧结束
```

这里 \(N\) 是该级的「阈值」，取值为 \(16/8/4/2\)。需要特别注意：**ROM_16 只用 `count` 一个计数器**做分段；而 **ROM_8/4/2 用 `count` 判等待段、再用第二个计数器 `s_count` 判前半/后半**。这是两种不同的编码风格，是本讲的一个重要鉴别点。

#### 4.1.3 源码精读

先看 ROM_16 的状态生成（单计数器风格）：

[RTL/ROM_16.v:L41-L51](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L41-L51) —— 用 `count` 的三个区间直接生成 `state`：`[0,16)`→等待、`[16,32)`→first half、`[32,48)`→second half、`[48,+∞)`→禁止。区间长度恰好都是 16。

再看 ROM_8 的状态生成（双计数器风格）：

[RTL/ROM_8.v:L48-L57](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_8.v#L48-L57) —— 这里 `count` 只用来判等待段（`count<8`），一旦 `count≥8` 就改用 `s_count` 判前半（`s_count<8`）和后半（`s_count≥8`），并在前半/后半里都执行 `next_s_count = s_count + 1`。

最小一级 ROM_2 完全同构，只是阈值换成 2：

[RTL/ROM_2.v:L48-L56](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v#L48-L56) —— `count<2` 等待、`s_count<2` first half、`s_count≥2` second half。

最后看消费端——`radix2` 如何用这个 `state`：

[RTL/radix2.v:L37-L81](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L37-L81) —— `case(state)` 把 `2'b00/01/10/11` 分别映射到「等待 / first half / second half / disable」四段行为。本讲关注的「state 从哪来」在这里收口：ROM 给出的 2 位 `state` 直接成为蝶形单元的指令。

#### 4.1.4 代码实践

**目标**：亲手验证「`state` 是计数器的分段函数」。

**步骤**：
1. 打开 `RTL/ROM_16.v` 第 45–51 行，把四个 `if/else if` 分支抄成一张表：`count` 范围 → `state` 值。
2. 打开 `RTL/ROM_8.v` 第 48–57 行，做同样的事，但注意这里前半/后半的判据变量是 `s_count` 而非 `count`。
3. 对比两张表，回答：ROM_16 的「禁止态」(`2'b11`) 在 ROM_8 里有对应分支吗？

**预期现象**：ROM_16 有 `else state = 2'd3`（即 `2'b11`，对应帧结束）；ROM_8/4/2 **没有**这个分支——它们的 `state` 只会在 `00/01/10` 之间走，不存在显式的禁止态。

**结果**：这一差异直接对应了「ROM_16 会自我终止一帧，后三级不会」的事实（详见 4.4 节）。无需仿真即可从源码读出。

#### 4.1.5 小练习与答案

**练习 1**：如果把 ROM_16 第 45 行的 `count<6'd16` 改成 `count<6'd8`，第 1 级的等待段会变成几拍？对蝶形运算有什么影响？

**答案**：等待段从 16 拍缩短为 8 拍。但第 1 级的 `shift_16` 延时仍是 16 拍（u3-l3），于是「差」还没从移位线回流，蝶形就被迫提前进入 first half，会导致配对错位、FFT 结果错误。这正说明 **ROM 的等待阈值必须与该级移位延时深度一致**。

**练习 2**：ROM_8 的 `state` 会不会出现 `2'b11`？

**答案**：不会。ROM_8 的组合逻辑只有 `count<8→00`、`s_count<8→01`、`s_count≥8→10` 三条分支，没有赋值 `2'd3` 的路径，`state` 恒在 `{00,01,10}` 中。

---

### 4.2 旋转因子查表

#### 4.2.1 概念说明

ROM 模块的另一半职责，是**在 second half 阶段把当前需要的旋转因子送给蝶形**。回顾 u3-l2：second half 里 `radix2` 要计算 `inter=b*(w_r-w_i)`、`mul_r=w_r*(a-b)+inter`、`mul_i=w_i*(a+b)+inter`，这里的 `w_r/w_i` 就是 ROM 送来的 24 位定点旋转因子。

实现方式非常直白：把该级所有旋转因子的定点值（u2-l2 已讲过 \(\times 256\) 量化与补码）硬编码进一个 `case` 语句，用计数器当前值作为索引去查。这其实就是一张「用组合逻辑实现的只读查找表」——所以模块名叫 ROM。

#### 4.2.2 核心流程

查表的关键是「**用哪个变量当索引**」和「**索引在哪个区间有效**」：

```
ROM_16:  case(count)    索引区间 count = 32..47   → 16 个旋转因子
ROM_8 :  case(s_count)  索引区间 s_count = 8..15  → 8 个旋转因子
ROM_4 :  case(s_count)  索引区间 s_count = 4..7   → 4 个旋转因子
ROM_2 :  case(s_count)  索引区间 s_count = 2..3   → 2 个旋转因子
```

注意两个规律：
- 索引区间**只在 second half 段命中**有效条目（因为 second half 才需要旋转因子），其余拍命中 `default`（输出 `256+j0`，即旋转因子 1）。
- 每级旋转因子个数 \(16/8/4/2\) **逐级减半**，正对应 radix-2 DIF 每级所需 \(W_{32}/W_{16}/W_8/W_4\) 的个数（u2-l1）。

#### 4.2.3 源码精读

ROM_16 的查表（用 `count` 当索引，共 16 项）：

[RTL/ROM_16.v:L52-L57](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L52-L57) —— `count=32` 时输出 \(w_r=256, w_i=0\)，即 \(W_{32}^{0}=1\) 的定点值（\(\cos 0 - j\sin 0\) 再 \(\times 256\)）。

[RTL/ROM_16.v:L93-L97](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L93-L97) —— `count=40` 时输出 \(w_r=0, w_i=-256\)。验证：\(k=8,N=32\Rightarrow 2\pi k/N=\pi/2\)，\(\cos(\pi/2)=0,\sin(\pi/2)=1\)，故 \(W=\cos-j\sin=0-j=-j\)，定点即 \(0-j\cdot 256\)。✓

ROM_8 的查表（改用 `s_count` 当索引，共 8 项）：

[RTL/ROM_8.v:L58-L95](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_8.v#L58-L95) —— `s_count=8..15` 共 8 项，对应第 2 级需要的 8 个 \(W_{16}^k\)。注意：与 ROM_16 不同，这里**只赋值 `w_r/w_i`，不再赋值 `next_valid`**。

ROM_2 的查表（最小，仅 2 项）：

[RTL/ROM_2.v:L57-L70](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v#L57-L70) —— `s_count=2` 给 \(256+j0\)（\(W_4^0=1\)），`s_count=3` 给 \(0-j256\)（\(W_4^1=-j\)）。恰好 2 个旋转因子，对应第 4 级（4 点蝶形）只需 \(W_4^0,W_4^1\)。

#### 4.2.4 代码实践

**目标**：验证 ROM 中硬编码的定点旋转因子与数学定义一致。

**步骤**：
1. 对 ROM_16，取 `count=33`（[L58-L62](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L58-L62)）：读出 \(w_r=\text{0xFB}=251\)、\(w_i=\text{0xFFCE}=-50\)（按 24 位补码解释，注意它写成 3 字节分段）。
2. 手算 \(W_{32}^{1}=\cos(2\pi/32)-j\sin(2\pi/32)=\cos(\pi/16)-j\sin(\pi/16)\approx 0.9808-j\,0.1951\)，再 \(\times 256\) 得 \(\approx 251-j\,50\)。
3. 与第 1 步读出的值对比。

**预期结果**：`251` 与 `0.9808×256≈251.1` 一致；`-50` 与 `-0.1951×256≈-49.9` 一致。说明 ROM_16 的 `count=33` 确实存的是 \(W_{32}^{1}\)。

> 说明：ROM 里 24 位值按 `8'b _ 8'b _ 8'b` 三段书写，最高段是符号位的扩展。把它当 24 位二进制补码整体解释即可还原十进制。该对比只需计算器，**待本地验证**的部分是你在仿真器里实际打印 `w_r/w_i` 的波形。

#### 4.2.5 小练习与答案

**练习 1**：ROM_8 的 `case(s_count)` 一共列了几个有效条目？为什么是这个数？

**答案**：8 个（`s_count=8..15`）。因为第 2 级是 16 点子变换的蝶形，radix-2 DIF 每级需要「该级点数 / 2」个旋转因子，\(16/2=8\)。

**练习 2**：为什么所有 ROM 的 `default` 分支都输出 `w_r=256, w_i=0`？

**答案**：`default` 命中的拍次（等待段、first half、以及尚未到 second half 的拍）蝶形并不真正使用旋转因子（first half 只算加减、second half 才乘旋转因子）。给一个安全的「旋转因子 = 1」作为默认值，可避免组合逻辑产生不定态 `x`，是防御性写法。

---

### 4.3 等待 / 前半 / 后半阈值

#### 4.3.1 概念说明

把 4.1、4.2 两条线索合起来，ROM 模块其实把时间轴切成了**三段**：

- **等待段（waiting, state=00）**：蝶形还没开始真正干活，输入数据正在填满移位延时线。
- **前半段（first half, state=01）**：蝶形算「和」与「差」。和直接送 `op` 进入下一级；差送 `delay` 进入 shift 延时线，N 拍后回流。
- **后半段（second half, state=10）**：回流的差作为 `din_a`，乘上 ROM 给出的旋转因子，得到该级最终输出。

这三段的长度不是随便定的——它们由该级的「组大小」决定，而组大小在 radix-2 DIF 里逐级减半（u2-l1）。所以四级的阈值也逐级减半：\(16 \to 8 \to 4 \to 2\)。

#### 4.3.2 核心流程

把四个 ROM 的三段阈值整理成一张表（本讲的核心结论表）：

| 模块（级） | 等待段判据 | 前半段判据 | 后半段判据 | 每段拍数 | 旋转因子个数 | 查表索引变量 |
|------------|------------|------------|------------|----------|--------------|--------------|
| ROM_16（级 1） | `count<16` | `count∈[16,32)` | `count∈[32,48)` | 16 / 16 / 16 | 16 | `count`（32~47） |
| ROM_8 （级 2） | `count<8`  | `s_count∈[0,8)`  | `s_count∈[8,16)` | 8 / 8 / 8 | 8 | `s_count`（8~15） |
| ROM_4 （级 3） | `count<4`  | `s_count∈[0,4)`  | `s_count∈[4,8)` | 4 / 4 / 4 | 4 | `s_count`（4~7） |
| ROM_2 （级 4） | `count<2`  | `s_count∈[0,2)`  | `s_count∈[2,4)` | 2 / 2 / 2 | 2 | `s_count`（2~3） |

观察三条规律：
1. **每段拍数 = N**：等待、前半、后半各持续 \(N\) 拍，\(N=16/8/4/2\)。
2. **等待段阈值 N = 该级 shift 延时深度**（u3-l3 的 `shift_16/8/4/2`）：这是 SDC 反馈回路的硬约束——必须等移位线填满，蝶形才能正确配对。
3. **旋转因子个数 = N**：每级存 \(N\) 个旋转因子，恰好等于该级蝶形对的数目，也等于 radix-2 DIF 该级需要的 \(W\) 个数。

> 这三条规律把本讲（ROM）、u3-l3（shift）和 u2-l1（算法）三者拧成了一股绳：**N 这个数字同时是「等待拍数 / 移位深度 / 旋转因子个数 / 蝶形对距离」**，是 SDC 架构的核心参数。

#### 4.3.3 源码精读

ROM_16 三段阈值的源码（已链接于 4.1.3）：`count` 的 `[0,16)/[16,32)/[32,48)` 三段，每段 16 拍。

ROM_8 三段阈值的源码（已链接于 4.1.3）：`count<8` 决定等待段，`s_count` 的 `[0,8)/[8,16)` 决定前半/后半。注意 ROM_8 的 `s_count` 在等待段**不递增**（保持原值），只在 `count≥8` 后才开始每拍 +1：

[RTL/ROM_8.v:L37-L46](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_8.v#L37-L46) —— `next_s_count = s_count`（保持）是默认值；只有进入 4.1.3 链接的前半/后半分支时才被改写为 `s_count + 1`。这种「默认保持、命中才递增」的写法是这段代码的精髓。

ROM_2 同构，阈值最小（N=2），是把整张表压缩到极致的版本：

[RTL/ROM_2.v:L36-L46](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v#L36-L46) —— 第 37 行还多了一句 `state = 2'd0;` 作为最外层默认值，确保任何未命中的情况都落到「等待」而非 `x`。

#### 4.3.4 代码实践

**目标**：把四个 ROM 的阈值填进 4.3.2 的表，并验证「每段拍数 = N」。

**步骤**：
1. 对 `ROM_16/8/4/2` 分别读出「等待段上界」（即 `count<N` 里的 N）。
2. 对 `ROM_8/4/2` 读出「前半/后半分界」（即 `s_count<N` 里的 N）。
3. 数一数每个 `case` 里有效旋转因子条目数。
4. 把三列数字横向对比。

**预期结果**：每一行的「等待拍数 / 前半拍数 / 后半拍数 / 旋转因子个数」四个数全部相等，都等于 N。这正是 4.3.2 表格的由来。

**待本地验证**：在仿真器里给 `ROM_2` 喂一个持续的 `in_valid`，观察 `state` 信号是否按 `00(2拍) → 01(2拍) → 10(2拍)` 的节拍跳转，并对照 4.3.2 的表。

#### 4.3.5 小练习与答案

**练习 1**：为什么每段的拍数恰好等于该级旋转因子的个数？

**答案**：因为 second half 每拍消耗一个旋转因子，而该级一共只有 N 个不同的旋转因子；又因为前半/后半是「成对」的（每一个「差」都要回流后乘一次旋转因子），所以前半也持续 N 拍。等待段则要等于移位延时深度 N。三个 N 因此相等。

**练习 2**：第 5 级的「N」应该是多少？为什么它不需要 ROM？

**答案**：第 5 级 N=1（2 点蝶形）。它只需要 1 个旋转因子 \(W_2^0=1\)，定点即 `256+j0`，是个常数，所以 `FFT.v` 在 [radix_no5 例化](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L230-L243) 处直接把 `.w_r(24'd256), .w_i(24'd0)` 写死，省掉了 ROM_1。

---

### 4.4 valid 菊花链传递

#### 4.4.1 概念说明

到目前为止我们说的都是「单级 ROM 内部怎么计数」。但五级流水线有四个 ROM，它们的计数必须**彼此对齐**——第 2 级的 ROM 不能在第 1 级还没产出数据时就开始计时。

项目采用的方案非常优雅：**让上一级蝶形的 `outvalid` 去当下一级 ROM 的 `in_valid`**。于是 valid 信号像接力棒一样沿流水线向后传：`in_valid → 第1级 → 第2级 → 第3级 → 第4级 → 第5级`。每一级「开始计数」的时刻，恰好是它「开始收到有效数据」的时刻，控制时序因此与数据流自动同步。这种接法叫 **valid 菊花链（daisy chain）**。

这里还藏着一个容易被忽略的细节：ROM_16 是唯一「真正用 valid 自保持」的一级；ROM_8/4/2 虽然也声明了 `valid/next_valid`，却根本没驱动它们——这是一段值得识别的历史遗留/死代码。

#### 4.4.2 核心流程

菊花链的接线（在 `FFT.v` 顶层）：

```
in_valid_reg ──┬──> ROM_16.in_valid   (第1级)
               └──> radix_no1 ... 
radix_no1.outvalid ──> ROM_8.in_valid  (第2级)  ── 也喂 shift_8
radix_no2.outvalid ──> ROM_4.in_valid  (第3级)  ── 也喂 shift_4
radix_no3.outvalid ──> ROM_2.in_valid  (第4级)  ── 也喂 shift_2
radix_no4.outvalid ──> (第5级 no5_state 逻辑 + shift_1)
```

每级 ROM 的内部计数规则（伪代码）：

```
每拍:
    若 in_valid 或 内部valid:   count ← count+1
ROM_16 额外: 内部 valid 由 next_valid 自保持, 在 count=47 时 next_valid=0 收尾
ROM_8/4/2 : 内部 valid 从不被赋值, 实际只靠 in_valid 计数
```

#### 4.4.3 源码精读

先看顶层如何把 valid 接成菊花链。第 1 级 ROM 用输入寄存后的 valid：

[RTL/FFT.v:L119-L126](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L119-L126) —— `ROM_16` 的 `.in_valid(in_valid_reg)`，并由它产生 `rom16_state` 去驱动 `radix_no1`。

第 2 级 ROM 用第 1 级蝶形的 outvalid：

[RTL/FFT.v:L152-L159](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L152-L159) —— `ROM_8` 的 `.in_valid(radix_no1_outvalid)`。第 3、4 级同理（[ROM_4: L186-L193](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L186-L193)、[ROM_2: L220-L227](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L220-L227)），分别接 `radix_no2_outvalid`、`radix_no3_outvalid`。

再看 ROM_16 真正使用的 valid 自保持锁存：

[RTL/ROM_16.v:L24-L39](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L24-L39) —— 时序块里 `valid <= in_valid`（建立）与 `valid <= next_valid`（保持）。`next_valid` 在 `case(count)` 里被赋值：count=32..46 给 `1`，**count=47 给 `0`**（[L128-L132](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L128-L132)）。这使第 1 级在一帧（48 拍）后自动停止计数、并把 `state` 切到 `2'b11`（禁止）。

与之对照，ROM_8 的时序块**根本没有 `valid` 赋值**：

[RTL/ROM_8.v:L26-L35](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_8.v#L26-L35) —— 只更新 `count` 和 `s_count`。于是 `valid`（复位后为 0/未定义）恒不变，`if(in_valid || valid)` 实际等价于 `if(in_valid)`；而 `next_valid` 在组合块里**从未被赋值**，是死代码。ROM_4/ROM_2 与 ROM_8 完全一样。

> 为什么第 1 级需要自保持、后三级不需要？因为第 1 级的 `in_valid`（`in_valid_reg`）只在喂 32 个输入样本期间为高，覆盖不了一帧所需的 48 拍，必须靠 valid 锁存「续命」；而后三级的 `in_valid` 来自上一级 `outvalid`，其有效区间本身就足够长，无需再续。这是两种风格的真正成因（推断，待本地验证）。

第 5 级是菊花链的终点，没有 ROM：

[RTL/FFT.v:L290-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L290-L298) —— `no5_state` 由 `r4_valid`（即 `radix_no4_outvalid`）和 `s5_count` 共同生成，相当于把「ROM 的计数器+查表」逻辑内联进了顶层（因为旋转因子已是常数，查表退化成固定值）。

#### 4.4.4 代码实践

**目标**：把四级 ROM 的 `in_valid` 来源画成一张接力表，并识别死代码。

**步骤**：
1. 在 `FFT.v` 中检索 `ROM_16(`、`ROM_8(`、`ROM_4(`、`ROM_2(` 四处例化，记下各自的 `.in_valid(...)` 来源。
2. 在 `ROM_8.v` / `ROM_4.v` / `ROM_2.v` 中检索 `next_valid`，确认它是否被任何赋值语句驱动。
3. 在 `ROM_16.v` 中检索 `next_valid`，确认它在 `case(count)` 里被驱动，且 `count=47` 时为 0。

**预期现象**：
- 四级 `in_valid` 来源依次为：`in_valid_reg → radix_no1_outvalid → radix_no2_outvalid → radix_no3_outvalid`。
- ROM_8/4/2 里 `next_valid` **没有任何赋值**（死代码）；ROM_16 里 `next_valid` 在每个 `case` 分支都被赋值。

**结果**：接力表证实了 valid 菊花链；检索结果证实了「只有第 1 级真正使用 valid 锁存」这一鉴别结论。整个过程纯静态阅读，无需仿真。

> 进阶观察（**待本地验证**）：由于 ROM_8/4/2 从不复位或赋值 `valid`，仿真上电时它可能是 `x`，使 `if(in_valid || valid)` 在 `in_valid=0` 的拍产生 `x`。设计中靠「`in_valid` 先于计数到达」规避了这一点，但严格意义上的初值处理是一个可改进点——可作为综合后的 lint/仿真 X 传播检查项。

#### 4.4.5 小练习与答案

**练习 1**：如果第 2 级 ROM 的 `in_valid` 不接 `radix_no1_outvalid`，而是也接 `in_valid_reg`，会发生什么？

**答案**：第 2 级 ROM 会在第 1 级**还没产出任何有效数据**时就开始计数并切到 first half，导致 `radix_no2` 的 `din_a`（来自 `shift_8` 反馈）尚未就绪，蝶形算出错误结果。菊花链的意义正是让每级「计数起点」对齐「数据起点」。

**练习 2**：ROM_16 的 `next_valid` 在 `count=47` 处设为 0，起什么作用？

**答案**：让内部 `valid` 在一帧（48 拍）结束时掉回 0，从而停止 `count` 递增，并把 `state` 推进到 `2'b11`（禁止）。这是第 1 级「一帧自我收尾」的机制；后三级没有等价机制，依赖外部 valid 的自然结束。

---

## 5. 综合实践：ROM_2 状态跳转时序图

把本讲四个最小模块串起来，完成规格里要求的核心任务：**对比三级阈值 + 画 ROM_2 的状态跳转时序图**。

### 5.1 三级阈值对比表

通读 `ROM_16.v`、`ROM_8.v`、`ROM_2.v`（再加 `ROM_4.v` 凑齐），把每个 ROM 的下列字段填出来（答案已在本讲 4.3.2 给出，这里作为你独立核对的模板）：

| 模块 | 等待上界 (count<) | 前半/后半分界 | 状态变量风格 | 查表变量 | 旋转因子数 | 是否用 valid 自保持 |
|------|-------------------|---------------|--------------|----------|------------|---------------------|
| ROM_16 | 16 | count: 16 / 32 | 单 count | count | 16 | 是（count=47 收尾） |
| ROM_8  | 8  | s_count: 8 | count+s_count | s_count | 8 | 否（死代码） |
| ROM_4  | 4  | s_count: 4 | count+s_count | s_count | 4 | 否（死代码） |
| ROM_2  | 2  | s_count: 2 | count+s_count | s_count | 2 | 否（死代码） |

**结论**：等待周期数 = 前半周期数 = 后半周期数 = 旋转因子数 = N（16/8/4/2），逐级减半。

### 5.2 ROM_2 单帧状态跳转时序图

设 `in_valid` 自第 1 拍起持续为高（即 `radix_no3_outvalid` 持续有效）。依据 [ROM_2.v:L36-L70](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v#L36-L70) 的组合逻辑，逐拍推导（`count`/`s_count` 为寄存器当前值，`state`/`w` 为其组合输出）：

```
拍 | count | s_count | state | 含义     | case(s_count) 输出 (w_r, w_i)
---|-------|---------|-------|----------|---------------------------------
 0 |   0   |    0    |  00   | 等待     | default → (256, 0)        = W4^0
 1 |   1   |    0    |  00   | 等待     | default → (256, 0)
 2 |   2   |    0    |  01   | 前半     | default → (256, 0)   [s_count 即将→1]
 3 |   3   |    1    |  01   | 前半     | default → (256, 0)   [s_count 即将→2]
 4 |   4   |    2    |  10   | 后半     | s_count=2 → (256, 0) = W4^0
 5 |   5   |    3    |  10   | 后半     | s_count=3 → (0, -256)= W4^1
```

读法要点：
- **拍 0–1**：`count<2`，处于等待段，`s_count` 不递增（保持 0）；蝶形在 `state=00` 下不产出有效结果。
- **拍 2–3**：`count≥2` 且 `s_count<2`，进入 first half（`state=01`）；`s_count` 开始每拍 +1（0→1→2）。
- **拍 4–5**：`s_count≥2`，进入 second half（`state=10`）；`case(s_count)` 命中有效条目，分别给出 \(W_4^0\) 与 \(W_4^1\) 两个旋转因子，供 `radix_no4` 做复数乘法。

把这张表画成波形（每拍一个状态值），就是要求的状态跳转时序图：`state = 00,00,01,01,10,10`，恰好在「等待 2 拍 → 前半 2 拍 → 后半 2 拍」之间切换，与 4.3.2 表中 N=2 完全吻合。

> **关于连续多帧**：`ROM_8/4/2` 的 `count`/`s_count` 在帧间不复位（无 ROM_16 那样的 `next_valid=0` 收尾），设计假定的是连续数据流。`s_count` 是 2 位（ROM_2），会在第 6 拍后回绕，从而周期性地重复「前半/后半」。精确的多帧/回绕时序**待本地验证**——建议在仿真器里跑 `ROM_2` 并观察 `state` 与 `s_count` 超过 6 拍后的行为来确认。

### 5.3 自检清单

完成本实践后，你应该能不查源码答出：
- [ ] 四级 ROM 的等待阈值各是多少？为什么等于该级 shift 深度？
- [ ] ROM_16 与 ROM_8 在「状态生成」上的编码风格差别是什么？
- [ ] 哪一级 ROM 真正使用了 `valid` 自保持？为什么只有它需要？
- [ ] valid 菊花链的四级 `in_valid` 分别来自哪里？
- [ ] ROM_2 在 second half 输出几个旋转因子？分别是什么值？

## 6. 本讲小结

- ROM 模块身兼二职：**用计数器分段产生 `state`**（驱动 `radix2` 三态机），并**用 `case` 查表输出旋转因子**（供 second half 复数乘法）。
- 四级 ROM 的阈值逐级减半：等待/前半/后半各 \(N=16/8/4/2\) 拍，且 \(N\) 同时等于移位延时深度、旋转因子个数、蝶形对距离——这是 SDC 架构的核心参数。
- **ROM_16 是单计数器风格**（`count` 直接分段，含 `2'b11` 禁止态），**ROM_8/4/2 是双计数器风格**（`count` 判等待、`s_count` 判前半/后半并查表）。
- **valid 菊花链**：`in_valid_reg → rom16 → radix_no1.outvalid → rom8 → radix_no2.outvalid → rom4 → radix_no3.outvalid → rom2`，使各级状态机自动对齐数据流。
- **ROM_16 是唯一真正使用 valid 自保持锁存的一级**（`next_valid` 在 `count=47` 收尾）；ROM_8/4/2 声明了 `valid/next_valid` 却从未驱动，属可识别的死代码。
- 第 5 级无 ROM：旋转因子退化为常数 `256+j0`，`state` 由顶层 `no5_state` 逻辑生成。

## 7. 下一步学习建议

- **u4-l1 五级流水线数据流串讲**：把本讲的 ROM/shift/radix2 三件套串成完整数据通路，看清 `state` 与 valid 如何在级间协同，理解一个样本从 `din` 到第 5 级 `out` 的全程。
- **u4-l3 控制时序与握手信号**：精读 `FFT.v` 里生成 `no5_state` 的两个 always 块——那是「没有 ROM 的第 5 级」如何用 `s5_count`+`r4_valid` 复刻本讲所述的计数器+state 机制。
- **u7-l1 SDC PE 加法器优化**：本讲只关心「second half 何时给出旋转因子」；下一阶段可深入「second half 拿到旋转因子后如何用 3 乘 5 加完成复数乘法」。
- **延伸阅读**：用 `Grep` 在 `RTL/` 下搜索 `next_valid`、`s_count`，对照本讲的「死代码」结论，培养在真实工程代码中辨别「声明了但没用」的信号的习惯。
