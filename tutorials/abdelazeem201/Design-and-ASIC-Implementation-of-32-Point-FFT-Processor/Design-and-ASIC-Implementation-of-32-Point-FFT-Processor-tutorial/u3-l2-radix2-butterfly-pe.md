# radix2 蝶形单元与 SDC 处理元

## 1. 本讲目标

本讲深入 `RTL/radix2.v`，把它从一个"黑盒子"读成一张"可计算的电路图"。学完后你应该能够：

- 说清 `radix2` 模块那 4 个状态（`2'b00/01/10/11`）分别做什么、谁产生 `state` 信号、`outvalid` 在哪些状态拉高。
- 看懂 **first half**（前半拍）里"加法分支送 `op`、减法分支送 `delay`"的分工。
- 推导 **second half**（后半拍）里把复数乘法从 **4 乘 2 加** 优化为 **3 乘 5 加** 的代数过程，并与 `radix2.v` 的 `inter / mul_r / mul_i` 三行代码逐行对齐。
- 解释末尾 `mul_r[31:8]` 截位为什么等价于"除以 256"，以及它如何与 u2-l2 的旋转因子定点放大相互抵消。

本讲是整个 FFT 流水线里"最数学"的一篇，但所有结论都落在不到 90 行的真实 Verilog 上。

## 2. 前置知识

在进入源码前，先统一四个名词（如果你已经熟悉，可以跳过）：

1. **蝶形运算（butterfly）**：radix-2 FFT 的最小运算单元。输入两个复数 \(x[m]\) 与 \(x[m+N/2]\)，输出两路：一路是和 \(x[m]+x[m+N/2]\)（产生偶数频率），另一路是差再乘旋转因子 \((x[m]-x[m+N/2])\cdot W\)（产生奇数频率）。因为画出来像蝴蝶的翅膀，所以叫蝶形。详见 u2-l1。

2. **复数乘法**：两个复数 \((a+jb)\)、\((c+jd)\) 相乘，结果是
   \[
   (a+jb)(c+jd) = (ac-bd) + j(ad+bc)
   \]
   直接实现需要 4 次实数乘法（\(ac,bd,ad,bc\)）和 2 次实数加法（\(ac-bd\)、\(ad+bc\)），简称 **4 乘 2 加**。本讲的主角就是如何把它压成 3 次乘法。

3. **状态机（FSM）**：用一个 `state` 寄存器告诉电路"现在该干哪一步"。本模块用 2 位 `state` 把同一个处理元（PE）在不同时钟拍分时复用，先做加减、再做乘法。

4. **有符号定点数**：`signed [23:0]` 表示 24 位二进制补码，最高位是符号位。u3-l1 已经讲过：输入 12 位经符号扩展 + 左移 8 位变成 24 位、数值放大 256 倍；u2-l2 讲过旋转因子也统一乘了 256。两边都"放大 256 倍"是为了在小数运算里保留精度，本讲末尾的截位会把这层放大再除回去。

> 承接关系：本讲假设你已读过 **u3-l1（顶层 FFT 模块）**，知道 5 级流水线里每级都长成"radix2 + shift + ROM"三件套；并读过 **u2-l2（旋转因子定点）**，知道旋转因子被乘 256 后存进 ROM。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的地方 |
|------|------|----------------|
| `RTL/radix2.v` | **主角**：蝶形单元 / SDC 处理元（PE） | 全文精读 |
| `RTL/FFT.v` | 顶层，例化 5 个 radix2 | 看 `radix_no1` 的接线，理解 `din_a/din_b/op/delay` 各自连到哪 |
| `RTL/ROM_2.v` | 产生 `state` 信号 + 提供旋转因子 | 看 `state` 如何在 `00→01→10` 之间跳转 |

一句话定位：**`radix2.v` 是运算核心，`ROM_2.v` 是它的"指挥棒"（给 state 和旋转因子），`FFT.v` 是把它们接成回路的"导线"。**

## 4. 核心概念与源码讲解

### 4.1 三态机与 outvalid 控制

#### 4.1.1 概念说明

`radix2` 模块本身不计数、不存状态，它的"该做哪一步"完全由外部送进来的 2 位 `state` 信号决定。这是 SDC（Single-path Delay Commutator，单路延迟换向器）架构的一个关键设计：**用一个外部计数器（在 ROM 模块里）统一调度，PE 只管"看 state 干活"**。

四个状态码的语义：

| `state` | 名字 | 含义 | `outvalid` |
|---------|------|------|------------|
| `2'b00` | waiting | 等待：流水线还没攒够数据，不计算、不输出 | 0 |
| `2'b01` | first half | 前半拍：做加法（和）与减法（差），和送 `op`，差送 `delay` | 1 |
| `2'b10` | second half | 后半拍：把上一拍的"差"乘旋转因子，送 `op` | 1 |
| `2'b11` | disable | 闲置/兜底：不输出 | 0 |

> 注意：实际运行时 ROM 只会产生 `00/01/10` 三种值，`2'b11` 是防御性的 default 分支，正常流程走不到。

#### 4.1.2 核心流程

ROM 模块里的计数器按"先等几个周期 → 前半拍若干拍 → 后半拍"的节奏切换 `state`。以最后一级 `ROM_2` 为例（计数阈值最小，最好读）：

```text
count < 2            → state = 00 (waiting)
count ≥ 2 且 s_count < 2 → state = 01 (first half)，s_count 每拍 +1
count ≥ 2 且 s_count ≥ 2 → state = 10 (second half)
```

也就是：**等 2 拍 → 前半拍 2 拍 → 之后一直后半拍**。`outvalid` 跟着 `state` 走：只要进入了 `01` 或 `10`，本拍的计算结果就是有效的，`outvalid=1`。

#### 4.1.3 源码精读

先看端口与内部寄存器声明：

[RTL/radix2.v:14-27](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L14-L27) —— 模块端口。注意 `state` 是 **输入**（`input wire [1:0] state`），说明"做什么"由外部决定；`din_a_*` 是反馈数据、`din_b_*` 是新输入、`w_r/w_i` 是旋转因子；`op_*` 是计算结果、`delay_*` 是送进延时线（shift）的旁路、`outvalid` 标志本拍输出是否有效。

[RTL/radix2.v:29-30](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L29-L30) —— 内部寄存器。`inter/mul_r/mul_i` 是 42 位宽（注释 `//was 27` 说明作者从 27 位扩到 42 位以防乘法溢出）；`a,b,c,d` 是 24 位中间变量。整块逻辑写在 `always@(*)` 里，是**纯组合逻辑**（没有时钟），所以 `state` 一变、输出立刻跟着变。

接着看 4 个状态分支的整体骨架：

[RTL/radix2.v:37-81](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L37-L81) —— `case(state)` 大开关。`2'b00` 和 `2'b11` 都只把 `outvalid` 设成 0（见 [L38-43](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L38-L43) 与 [L73-76](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L73-L76)）；真正干活的是 `2'b01`（first half）和 `2'b10`（second half），分别在 4.2 和 4.3 详讲。

再看 `state` 是怎么被外部"指挥"出来的，读 `ROM_2.v`：

[RTL/ROM_2.v:48-56](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v#L48-L56) —— `state` 的生成。`count` 是主计数器，`s_count` 是"前半拍"内部小计数器。三段 `if/else if` 正好对应 waiting / first half / second half。其它级（`ROM_16/8/4`）逻辑相同，只是阈值 `2` 换成 `16/8/4`，留到 u3-l4 详讲。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立"`state` 由 ROM 决定、PE 只执行"的认知。
2. **步骤**：打开 `RTL/radix2.v`，在 [L37](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L37) 的 `case(state)` 处标注每个分支的 `outvalid` 值；再打开 `RTL/ROM_2.v` [L48-56](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v#L48-L56)，用箭头画出 `count/s_count → state` 的映射。
3. **观察现象**：你会发现 `radix2` 模块里**没有任何计数器**，所有时序都来自 `state` 这个输入。
4. **预期结果**：得到一张 `count∈{0,1}→00`、`count≥2 且 s_count∈{0,1}→01`、`count≥2 且 s_count≥2→10` 的小表。

#### 4.1.5 小练习与答案

- **Q1**：为什么 `radix2` 把 `state` 设计成输入，而不是自己在内部维护一个状态机？
  - **答**：因为同一级里的 `state`、旋转因子地址、shift 延时深度必须**严格同步**。把计数器统一放在 ROM 模块，可以让"给 state"和"给旋转因子"用同一个计数器，避免多套计数器错拍；PE 保持无状态、纯组合，也利于时序收敛。

- **Q2**：`2'b11` 这个分支在正常仿真中会被触发吗？
  - **答**：不会。ROM 模块只会输出 `00/01/10`，`2'b11` 是 `case` 的防御性 default，保证组合逻辑不出现锁存器（latch）。

---

### 4.2 first half 加减分支

#### 4.2.1 概念说明

回忆 u2-l1 的 radix-2 DIF 蝶形：一对输入要算"和"与"差"两路。在 SDC 架构里，这两路**不是同时输出**，而是被拆到不同时间拍：

- **和**（加法分支）：当拍就算完，直接送到下一级（`op`）。
- **差**（减法分支）：先塞进延时线（`delay` → shift 寄存器），等过若干拍回来，再到 second half 里乘旋转因子。

这正是 "Delay Commutator（延迟换向）"得名的原因——差的那一路被"延迟"了，并在合适的时机"换向"回到处理元。

#### 4.2.2 核心流程

first half（`state==2'b01`）里，`din_a` 是**从 shift 反馈回来的旧数据**，`din_b` 是**当拍新输入**。计算如下：

```text
和（实部）a = din_a_r + din_b_r      → op_r   （送往下一级）
和（虚部）b = din_a_i + din_b_i      → op_i
差（实部）c = din_a_r - din_b_r      → delay_r（送往 shift 延时线）
差（虚部）d = din_a_i - din_b_i      → delay_i
outvalid = 1
```

注意命名小坑：源码里局部变量 `a,b,c,d` **只在本分支内部有效**，和端口注释里的 `//a //b //c //d` 不是一回事（端口注释把 `din_b` 标成 a/b、`w` 标成 c/d，是给 second half 用的）。读 first half 时请以本讲的"和/差"解释为准。

#### 4.2.3 源码精读

[RTL/radix2.v:44-57](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L44-L57) —— first half 主体。`a/b` 是和（送 `op`），`c/d` 是差（送 `delay`），`outvalid=1`。

要理解 `delay` 最终去了哪、`din_a` 又是从哪来的，必须看顶层接线：

[RTL/FFT.v:95-108](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L108) —— `radix_no1` 的例化。关键四行：
- `.din_a_r(shift_16_dout_r)` —— `din_a` 接的是 **shift_16 的输出**（反馈回来的旧数据）；
- `.din_b_r(din_r_wire)` —— `din_b` 接的是**新鲜输入**；
- `.op_r(radix_no1_op_r)` —— 和的结果送往下一级 `radix_no2`；
- `.delay_r(radix_no1_delay_r)` —— 差的结果送往 `shift_16` 的输入（见 [FFT.v:110-117](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L110-L117)）。

于是形成回路：`radix2.delay → shift → radix2.din_a`。first half 里算出的"差"被推进 shift，过 N 拍后从 `din_a` 回来，正好赶上 second half 去乘旋转因子。

还有一个容易被忽略的细节——`delay` 的默认值：

[RTL/radix2.v:35-36](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L35-L36) —— 在 `always` 一开头就把 `delay_r = din_b_r; delay_i = din_b_i;`。意思是：**除了 first half 会把 `delay` 改写成"差"，其它状态下 `delay` 都等于"原样透传的新输入"**。这就是"换向器（commutator）"——`delay` 这个口子在不同拍把"差"或"原始输入"轮换着送进延时线，从而维持流水线里始终有数据在流动。

#### 4.2.4 代码实践（跟踪型）

1. **目标**：搞清 first half 的"和送 op、差送 delay"分工。
2. **步骤**：假设 `din_a_r=10, din_b_r=4`（为方便手算用小整数；真实硬件是 24 位补码），手算 first half 的四个输出。
3. **观察**：`op_r` 应为 14（和），`delay_r` 应为 6（差）。
4. **预期结果**：和（14）流向下一级，差（6）进入 shift 等待 second half 取用。
5. **待本地验证**：若想看真实波形，可在仿真器里给 `radix_no1` 的 `op_r/delay_r` 加波形，观察 first half 拍上两者是否等于"和/差"。

#### 4.2.5 小练习与答案

- **Q1**：为什么"和"可以直接送下一级，而"差"要先绕一圈 shift？
  - **答**：和那一路是 radix-2 DIF 的偶数频率输出，本拍即可确定，无需再乘旋转因子；差那一路是奇数频率输出，必须乘 \(W\) 才完整，而乘法要等到 second half 用专门的乘法器做，所以先存进延时线排队。

- **Q2**：`delay` 在 second half（`2'b10`）里等于什么？
  - **答**：等于 `din_b`（透传），见 [L62-63](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L62-L63)，与默认值一致。second half 的"主要任务"是乘旋转因子，此时 `delay` 只负责把新输入继续往后传，维持回路不断流。

---

### 4.3 second half 复数乘法（3 乘 5 加）

#### 4.3.1 概念说明

second half（`state==2'b10`）的核心任务是把"差"那一路乘上旋转因子。这里藏着本设计最大的工程巧思——**用 3 次乘法 + 5 次加法，替代朴素的 4 次乘法 + 2 次加法**。这是高斯/卡拉苏巴（Karatsuba）风格的复数乘法技巧，在 ASIC 里能省下一个实数乘法器，而乘法器的面积/功耗远大于加法器，因此非常划算。

> 说明：本节只讲 second half 这一段乘法的"3 乘 5 加"是怎么来的；上升到整个蝶形 PE 的"加法器减半"分析，留到 u7-l1。

#### 4.3.2 核心流程

目标是计算两个复数的乘积 \((a+jb)(c+jd)\)。朴素做法是 4 乘 2 加：

\[
(a+jb)(c+jd) = \underbrace{(ac-bd)}_{\text{实部}} + j\underbrace{(ad+bc)}_{\text{虚部}}
\quad\text{（4 次乘：}ac,bd,ad,bc\text{；2 次加）}
\]

优化思路：引入一个**公共子表达式** \(\text{inter}=b(c-d)\)，把 4 个乘积中的两两共享出来。推导如下：

\[
\begin{aligned}
\text{inter} &= b(c-d) \\[2pt]
\text{实部} &= c(a-b) + \text{inter} = ca - cb + bc - bd = ac - bd \quad\checkmark \\[2pt]
\text{虚部} &= d(a+b) + \text{inter} = da + db + bc - bd = ad + bc \quad\checkmark
\end{aligned}
\]

最终只需要 **3 次乘法**：\(b(c-d)\)、\(c(a-b)\)、\(d(a+b)\)；以及 **5 次加/减法**：\((c-d)\)、\((a-b)\)、\((a+b)\)、实部的"+inter"、虚部的"+inter"。代价是多了 3 个加法器，换来少 1 个乘法器。

> 对应到代码里的变量：`a=din_a_r`、`b=din_a_i`（反馈回来的"差"那一路的实/虚部），`c=w_r`、`d=w_i`（旋转因子的实/虚部）。

#### 4.3.3 源码精读

[RTL/radix2.v:58-72](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L58-L72) —— second half 主体。先把 `a/b` 指向反馈数据 `din_a`（即 first half 存进 shift、如今转回来的"差"），再执行 3 乘 5 加：

[RTL/radix2.v:65-67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L65-L67) —— 三行就是上面公式的直译：
- `inter = b * (w_r - w_i);`        —— \(b(c-d)\)
- `mul_r = w_r * (a - b) + inter;` —— \(c(a-b)+\text{inter}=\) 实部
- `mul_i = w_i * (a + b) + inter;` —— \(d(a+b)+\text{inter}=\) 虚部

注意三个乘子里的减法/加法 \((w_r-w_i)\)、\((a-b)\)、\((a+b)\) 由综合工具合并成加法器，`inter` 被实部、虚部**复用**了一次，这正是省下一个乘法器的关键。

[RTL/radix2.v:29](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L29) —— `inter/mul_r/mul_i` 声明为 42 位有符号（注释 `//was 27`）。因为旋转因子虽存成 24 位、实际有效值只有 ±256 量级（约 9~10 位），与 24 位数据相乘后结果在 35 位左右，42 位足够容纳且留有余量，避免溢出。

> 第 5 级 `radix_no5` 的旋转因子被常数化为 `w_r=256, w_i=0`（即 \(c=1,d=0\)），见 [FFT.v:236-237](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L236-L237)。代入 3 乘 5 加公式后只剩纯加减，所以第 5 级不需要 ROM——这解释了 u1-l2 里"ROM 只有 4 个"的现象。

#### 4.3.4 代码实践（纸笔计算型）——本讲主实践

> 这是本讲的核心实践，要求**手算验证两种乘法数值一致**。

1. **目标**：证明 `radix2.v` 的 3 乘 5 加与标准 4 乘 2 加结果完全相同。
2. **给定数据**：取 \(a=3,\ b=2\)（数据 \(3+j2\)），旋转因子取 \(c=0.7071,\ d=-0.707\)（对应 FFT.py 里的 `w[4]/w_i[4]`，即 \(W_{32}^{4}\)）。
3. **步骤 A（标准 4 乘 2 加）**：
   - 实部 \(= ac - bd = 3\times0.7071 - 2\times(-0.707) = 2.1213 + 1.414 = 3.5353\)
   - 虚部 \(= ad + bc = 3\times(-0.707) + 2\times0.7071 = -2.121 + 1.4142 = -0.7068\)
4. **步骤 B（SDC 3 乘 5 加，照搬 radix2.v 公式）**：
   - \(\text{inter} = b(c-d) = 2\times(0.7071-(-0.707)) = 2\times1.4141 = 2.8282\)
   - 实部 \(= c(a-b)+\text{inter} = 0.7071\times(3-2)+2.8282 = 0.7071+2.8282 = 3.5353\)
   - 虚部 \(= d(a+b)+\text{inter} = (-0.707)\times(3+2)+2.8282 = -3.535+2.8282 = -0.7068\)
5. **观察现象**：A、B 两路的实部、虚部**逐位相等**。
6. **预期结果**：得到 \(3.5353 - j0.7068\)，两种方法完全一致，说明 3 乘 5 加是 4 乘 2 加的等价改写，没有引入数值误差（在定点量化之前）。

#### 4.3.5 小练习与答案

- **Q1**：把数据换成 \(a=2,\ b=1\)、旋转因子 \(c=0.5,\ d=-0.5\)，分别用两种方法算，验证是否都得 \(1.5 - j0.5\)。
  - **答**：
    - 标准：实部 \(=2\times0.5-1\times(-0.5)=1.5\)，虚部 \(=2\times(-0.5)+1\times0.5=-0.5\)。
    - SDC：\(\text{inter}=1\times(0.5-(-0.5))=1\)；实部 \(=0.5\times(2-1)+1=1.5\)；虚部 \(=(-0.5)\times(2+1)+1=-1.5+1=-0.5\)。两者一致 ✓。

- **Q2**：为什么"省一个乘法器"在 ASIC 里值得，即使多用了 3 个加法器？
  - **答**：一个 24×24 有符号乘法器的面积和功耗大约是一个 24 位加法器的几十倍。用 3 个廉价加法器换掉 1 个昂贵乘法器，整体面积与功耗显著下降；这也正是 SDC PE 的核心卖点（详见 u7-l1/u7-l2）。

---

### 4.4 结果截位 [31:8]

#### 4.4.1 概念说明

second half 算出的 `mul_r/mul_i` 是 42 位的乘积，但下一级 radix2 的输入只要 24 位，而且 u3-l1 讲过：旋转因子在 u2-l2 里被统一乘了 256（\(2^8\)）。所以这里必须做两件事：**(a) 把 42 位缩回 24 位；(b) 把那层 ×256 的放大除回去**。`mul_r[31:8]` 这一刀同时完成了这两件事。

#### 4.4.2 核心流程

\[
\texttt{op\_r} = \texttt{mul\_r[31:8]} \;\Longleftrightarrow\; \left\lfloor \frac{\texttt{mul\_r}}{256} \right\rfloor \;\text{（取 24 位）}
\]

- 取 `[31:8]` = 从第 8 位到第 31 位，共 \(31-8+1=24\) 位。
- 等价于把 42 位数**右移 8 位**再保留低 24 位，也就是除以 256 取整。
- 因为旋转因子自带 ×256，乘积自带 ×256，右移 8 位正好抵消，使输出回到"数据原本的 24 位尺度"，与下一级输入对齐。
- 低 8 位（`[7:0]`）被丢弃，是定点量化的舍入误差来源（这也是 testbench 用 SNR 而非逐位比对的根本原因，见 u5-l1）。

#### 4.4.3 源码精读

[RTL/radix2.v:69-70](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L69-L70) —— `op_r = (mul_r[31:8]); op_i = (mul_i[31:8]);`。`op_r/op_i` 声明为 `signed [23:0]`（见 [L22-23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L22-L23)），正好接住这 24 位。结合 4.3 的手算：若把 4.3 实践里的旋转因子按 ×256 量化成整数（\(c\approx181,d\approx-181\)，因为 \(0.7071\times256\approx181\)），数据也放大 256 倍后相乘，最后 `[31:8]` 截位，结果会回到与浮点手算一致的整数尺度。

#### 4.4.4 代码实践（参数观察型）

1. **目标**：直观体会"截位 = 除以 256"。
2. **步骤**：假设某拍 `mul_r = 42'd 384`（十进制 384）。手算 `mul_r[31:8]` 的值。
3. **观察**：\(384 / 256 = 1.5\)，取整后整数部分为 1（Verilog 截位是向零截断）。
4. **预期结果**：`op_r = 1`，低 8 位（即 0.5 的小数部分）被丢弃。
5. **待本地验证**：可在仿真中用 `$display("mul_r=%d op_r=%d", mul_r, op_r);` 打印 second half 的中间值与截位后值，观察二者的 256 倍关系。

#### 4.4.5 小练习与答案

- **Q1**：为什么是右移 8 位，而不是 7 位或 9 位？
  - **答**：因为 u2-l2 里旋转因子的定点量化系数 \(S=256=2^8\)。乘积里多出来的放大倍数就是 \(2^8\)，必须右移 8 位才能精确抵消，移少了会放大、移多了会缩小尺度。

- **Q2**：截掉的低 8 位会带来什么后果？为什么 testbench 用 SNR≥40dB 判定而不是要求逐位相等？
  - **答**：低 8 位是定点舍入误差，每级截位都会累积小幅噪声，所以硬件输出不可能与浮点黄金值逐位相等；用 SNR 衡量"信号能量远大于噪声能量"（≥40dB 即噪声约万分之一）才是合理的功能判定标准（见 u5-l1）。

---

## 5. 综合实践

**任务：跟踪一个完整蝶形在 `radix2` PE 里的两拍旅程，把"状态机 + 加减分支 + 3 乘 5 加 + 截位"四件事串成一条线。**

设第一级某次蝶形的反馈数据与新输入（实部，虚部按相同方式处理）为：

- 反馈（`din_a`，来自 shift_16）：\(a_{\text{old}} = 4\)（实部），\(b_{\text{old}} = 0\)（虚部）
- 新输入（`din_b`）：实部 \(= 12\)，虚部 \(= 0\)
- 旋转因子（`w_r/w_i`，来自 ROM_16）：\(c = 0.7071,\ d = -0.707\)

请按下面四步填表（建议在草稿纸上完成）：

| 拍 | `state` | 本拍计算 | `op_r` 去向 | `delay_r` 去向 |
|----|---------|----------|-------------|-----------------|
| 第 1 拍 | `2'b01`（first half） | 和 \(=4+12=16\)；差 \(=4-12=-8\) | 和 \(16\to\) 下一级 | 差 \(-8\to\) shift_16 |
| 第 2 拍（若干拍后差回来了） | `2'b10`（second half） | 见下 | 乘积截位 \(\to\) 下一级 | 透传新输入 \(\to\) shift_16 |

在第 2 拍里，回来的"差"是 \(-8\)（实部），虚部差是 0。请用 4.3 的 3 乘 5 加公式（注意这里 \(b=0\)）算出 `mul_r`，再说明经 `[31:8]` 截位、并考虑 ×256 定点后的最终 `op_r` 含义。

**参考答案要点**：
- 因为 \(b=0\)，\(\text{inter}=0\cdot(c-d)=0\)；实部 \(=c(a-b)+0=0.7071\times(-8-0)=-5.6568\)；虚部 \(=d(a+b)+0=-0.707\times(-8)=5.656\)。
- 两种方法（4 乘 2 加：\((-8)\times0.7071-0=-5.6568\)）结果一致 ✓。
- 在真实硬件里，\(-8\) 会以放大 256 倍的 24 位补码参与运算，旋转因子也放大 256 倍，乘积再 `[31:8]` 截位除以 256，最终 `op_r` 回到与 \(-5.6568\) 对应的整数定点值。
- 这就完成了一次完整的 SDC 蝶形：**first half 出"和"，second half 把"差"乘旋转因子，两者先后流向下一级**。

> 进阶：把虚部也填上、再画出 `radix_no1_outvalid` 在这两拍的波形，你就完成了从"读一个 PE"到"读懂一级流水线"的过渡——这正是下一讲 u4-l1 的入口。

## 6. 本讲小结

- `radix2` 是**纯组合**的蝶形处理元，靠外部 `state` 信号分时复用；`2'b00/01/10/11` 分别对应 waiting / first half / second half / disable，`state` 由 ROM 模块的计数器产生。
- **first half**（`2'b01`）算"和"与"差"：和（`din_a+din_b`）送 `op` 直达下一级，差（`din_a-din_b`）送 `delay` 进 shift 延时线，形成 `delay→shift→din_a` 反馈回路。
- **second half**（`2'b10`）把反馈回来的"差"乘旋转因子，用 **3 乘 5 加**（`inter=b(c-d)`、`mul_r=c(a-b)+inter`、`mul_i=d(a+b)+inter`）等价替代 4 乘 2 加，省下一个乘法器。
- `inter/mul_r/mul_i` 用 42 位宽容纳乘积；末尾 `op=mul[31:8]` 截位等价于除以 256，抵消旋转因子的定点放大，把结果对齐回 24 位数据通路。
- 第 5 级旋转因子常数化为 \(1+j0\)（`w_r=256,w_i=0`），3 乘 5 加退化为纯加减，故第 5 级无需 ROM。

## 7. 下一步学习建议

- **u3-l3（移位寄存器与延时单元）**：本讲多次提到"差送进 shift、过 N 拍回来"，下一讲就专门讲 `shift_16/8/4/2/1` 如何用超宽寄存器实现这条延时线，把 `delay→shift→din_a` 回路补全。
- **u3-l4（ROM 与状态控制）**：本讲只读了 `ROM_2` 的 `state` 生成，下一讲系统对比 `ROM_16/8/4/2` 的阈值差异与旋转因子查表。
- **u4-l1（五级流水线数据流串讲）**：把本讲的"单个 PE 两拍"扩展到"五级 PE 串联"，画出完整数据流框图。
- **u7-l1（SDC PE 加法器优化分析）**：若你想把本讲的"3 乘 5 加"上升到"整个蝶形加法器减半"的 PE 级论证，那是专家层的延伸阅读。
