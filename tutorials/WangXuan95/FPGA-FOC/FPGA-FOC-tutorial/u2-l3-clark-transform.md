# Clark 变换 clark_tr.v

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 Clark 变换（三相 abc → 两相 αβ）在 FOC 电流环里所处的位置，以及为什么要做这一步。
- 看懂 [`clark_tr.v`](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v) 的三级流水线结构，并能解释 `i_en → en_s1 → en_s2 → o_en` 这条使能脉冲握手链如何让数据与节拍同步下传。
- 解释 \(I_\alpha = 2I_a - I_b - I_c\) 与 \(I_\beta = \sqrt{3}(I_b - I_c)\) 这两个公式为什么被整体放大了 2 倍，以及这个统一增益为什么对 FOC 无害。
- 读懂用 `{N{sign}, val[15:k]}` 这种「符号扩展右移」来近似 1/2、1/8、1/16 … 并逐级累加去逼近 \(\sqrt{3}\) 的定点技巧，并能亲手算出近似的相对误差。

本讲只覆盖一个最小模块：**clark_tr**。它是 FOC 数据流里把「三相电流」压成「两相电流」的第一个算法模块，承接上一讲 [`u2-l2`](u2-l2-angle-and-current-recon.md) 重构出的 `ia/ib/ic`，输出 `ialpha/ibeta` 给下一讲 [`u2-l4`](u2-l4-park-and-sincos.md) 的 Park 变换使用。

## 2. 前置知识

在进入源码前，先用最直白的方式建立两个直觉。

### 2.1 为什么要从三相变两相？

无刷电机/永磁同步电机的定子上有三相绕组（A、B、C），三相电流 \(I_a, I_b, I_c\) 共同决定了一个「合成的电流矢量」。问题是：三相绕组在空间上互差 120°，直接拿三个数去算角度、做控制，公式里到处是 120°、\(\sqrt{3}\)，非常别扭。

Clark 变换的本质，就是把这三个互差 120° 的测量值，重新投影到**两个互相垂直**的坐标轴 α、β 上。投影完之后：

- α、β 两轴正交（互差 90°），后面再做旋转（Park 变换）、再做 PI、再调电压，都只需要处理「两个互相垂直的分量」，数学上大大简化。
- 由于三相对称时满足基尔霍夫电流定律 \(I_a + I_b + I_c = 0\)，三个数里其实只有两个是独立的，所以「三变二」没有信息损失。

一句话：**Clark 变换 = 把 120° 三相坐标系，旋转/投影成 90° 正交的两相坐标系，方便后续控制。**

### 2.2 为什么要在 FPGA 里用「移位加法」近似 √3？

Clark 变换的公式里有 \(\sqrt{3}\) 这个无理系数（≈1.732）。在软件里直接写 `1.732 * x` 是理所当然的事，但在 FPGA 里：

- 乘一个无理小数，需要定点小数乘法器，面积大、时序紧。
- 除法 `/` 或浮点运算综合出来的电路非常昂贵。

而 \(\sqrt{3}\) 可以被分解成一串「2 的整数次幂的倒数」之和（因为 1/2、1/4、1/8 … 都能用**算术右移**一行实现，几乎不花资源）：

\[
\sqrt{3} \approx 1 + \tfrac{1}{2} + \tfrac{1}{8} + \tfrac{1}{16} + \tfrac{1}{32} + \tfrac{1}{128} + \tfrac{1}{256} + \tfrac{1}{1024} + \tfrac{1}{2048}
\]

后面会验证，这 9 项之和与真实 \(\sqrt{3}\) 的相对误差只有约 **0.007%**。这种「用移位加法逼近无理系数」是 FPGA 定点信号处理的看家本领，本讲会带你逐行拆解它在代码里长什么样。

> 名词速查：
> - **流水线（pipeline）**：把一段运算拆成几级，每级用一个时钟沿把中间结果存进寄存器，下一级再算。换来的是更高主频，代价是多几个时钟周期的延迟。
> - **握手脉冲（`i_en`/`o_en`）**：一个仅持续一个时钟周期的高电平脉冲，表示「这一拍数据有效」。模块之间靠它对齐节拍，而不是靠计数器。
> - **有符号定点（`signed [15:0]`）**：16 位二进制补码，最高位是符号位，表示范围 \(-32768 \sim +32767\)。本项目里电流类信号统一用这个位宽。

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，外加一个仿真文件用于实践：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [RTL/foc/clark_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v) | Clark 变换模块本体 | 全部内容，是本讲主角 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | FOC 算法顶层 | 第 119–129 行例化 `clark_tr` 的上下文，看清它的输入从哪来、输出到哪去 |
| [SIM/tb_clark_park_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v) | Clark+Park 仿真测试平台 | 第 68–78 行用 `sincos` 合成三相正弦作为 `clark_tr` 的激励，用于波形验证 |

`clark_tr` 在系统中的位置（承接 [u2-l2](u2-l2-angle-and-current-recon.md) 的电流重构）：

```
ADC 三相值 ──电流重构──> ia, ib, ic ──[clark_tr]──> ialpha, ibeta ──[park_tr]──> id, iq
                          (foc_top 内)              ← 本讲             (下一讲)
```

## 4. 核心概念与源码讲解

### 4.1 从三相到两相：Clark 变换的数学与动机

#### 4.1.1 概念说明

Clark 变换解决的问题：把空间互差 120° 的三相电流 \(I_a, I_b, I_c\)，投影到与 A 相绕组对齐的 α 轴、以及与之垂直的 β 轴上，得到两个正交分量 \(I_\alpha, I_\beta\)。

按「投影」写出来的原始公式（未归一化形式）是：

\[
I_\alpha = I_a - \tfrac{1}{2}I_b - \tfrac{1}{2}I_c
\]

\[
I_\beta = \tfrac{\sqrt{3}}{2}I_b - \tfrac{\sqrt{3}}{2}I_c = \tfrac{\sqrt{3}}{2}(I_b - I_c)
\]

这两个公式里都带着 1/2，在整数硬件里直接算会触发**截断误差**（比如 `5/2` 在整数除法里变成 2，丢掉了 0.5）。作者的解决办法非常朴素：**把两个公式同时乘以 2，消掉所有的 1/2**。于是得到代码里实际使用的形式：

\[
I_\alpha = 2I_a - I_b - I_c
\]

\[
I_\beta = \sqrt{3}(I_b - I_c)
\]

也就是说，代码算出的 \(I_\alpha, I_\beta\) 都比「投影形式」整体放大了 **2 倍**。这个统一的 ×2 增益为什么无害？因为：

1. FOC 是**线性系统**，\(I_\alpha, I_\beta\) 同时放大一个常数倍，等价于把电流的单位重新标定了一下。
2. 后面的 PI 控制器会自动把这个未知增益吸收进 Kp/Ki 里——这正是 [u2-l1](u2-l1-foc-top-overview.md) 反复强调的「系数不必精确，误差交给 PID」的工程思想。

> 注：若再用基尔霍夫电流定律 \(I_a + I_b + I_c = 0\) 代入，\(I_\alpha = 2I_a - I_b - I_c = 2I_a - (-I_a) = 3I_a\)。也就是说 α 分量在三相对称时恰好是 A 相电流的 3 倍，这一点在后面看仿真波形时有用。

#### 4.1.2 核心流程

Clark 变换在 FOC 数据流里的数据流图：

```
          ┌─────────────┐
ia ──────▶│             │
ib ──────▶│  clark_tr   │──▶ ialpha (= 2·ia − ib − ic)
ic ──────▶│             │──▶ ibeta  (≈ √3·(ib − ic))
          └─────────────┘
       (伴随 i_en 脉冲)        (3 拍后伴随 o_en 脉冲)
```

模块在收到 `i_en` 脉冲后的第 3 个时钟沿，把算好的 `o_ialpha/o_ibeta` 连同一个 `o_en` 脉冲一起送出。

#### 4.1.3 源码精读

先看模块的端口定义，确认它「吃进什么、吐出什么」：

[RTL/foc/clark_tr.v:9-16](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L9-L16) —— 端口：输入三相有符号电流 `i_ia/i_ib/i_ic`（注释标明范围 −8191~8191）和使能脉冲 `i_en`，输出两相有符号电流 `o_ialpha/o_ibeta` 和使能脉冲 `o_en`。

再看它在 `foc_top` 里被怎么接线，重点是输入直接来自上一讲的电流重构结果 `ia/ib/ic`，使能来自 `en_iabc`：

[RTL/foc/foc_top.v:119-129](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L119-L129) —— 例化 `clark_tr`：`.i_en(en_iabc)` 把「三相电流有效」脉冲接进来，`.o_en(en_ialphabeta)` 把「αβ 有效」脉冲送出去给 Park 变换。注意它的复位接的是 `init_done` 而不是顶层 `rstn`——这是 FOC 在初始化阶段把所有算法模块按住、标定完初始角度后再统一放手的统一做法（详见 [u2-l2](u2-l2-angle-and-current-recon.md) 的 Φ 标定部分）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认 Clark 变换在整个 FOC 通路里的「上游」和「下游」。
2. **操作步骤**：
   - 打开 [foc_top.v:99-109](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L99-L109)，找到电流重构的 `always` 块，看清 `ia/ib/ic` 是怎么由 `adc_a/adc_b/adc_c` 算出来的。
   - 再打开 [foc_top.v:140-150](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L140-L150)，看清 `park_tr` 如何消费 `clark_tr` 的 `ialpha/ibeta`。
3. **需要观察的现象**：`clark_tr` 的 `i_en` 来自电流重构的 `en_iabc`，它的 `o_en`（即 `en_ialphabeta`）又成了 `park_tr` 的 `i_en`。
4. **预期结果**：你能画出一条 `en_adc → en_iabc → en_ialphabeta → en_idq` 的脉冲接力链，`clark_tr` 正好处在中间一棒。

#### 4.1.5 小练习与答案

**练习 1**：若三相电流满足 \(I_a + I_b + I_c = 0\)，把 \(I_\alpha = 2I_a - I_b - I_c\) 化简成只含 \(I_a\) 的形式。

**参考答案**：由 KCL 得 \(I_b + I_c = -I_a\)，代入得 \(I_\alpha = 2I_a - (-I_a) = 3I_a\)。所以 α 分量恰好是 A 相电流的 3 倍（在代码这个「放大 2 倍」的版本里）。

**练习 2**：代码为什么不直接用归一化（幅值不变）的 Clark 公式 \(I_\alpha = \tfrac{2}{3}(I_a - \tfrac{1}{2}I_b - \tfrac{1}{2}I_c)\)？

**参考答案**：归一化公式带 2/3 这个分数系数，在整数硬件里会产生截断误差，而且额外的除法/乘法代价不小。作者干脆把公式整体放大、消掉所有分数，换来的统一增益交给 PI 控制器吸收，既省硬件又不影响控制效果。

---

### 4.2 三级流水线与 i_en/o_en 脉冲握手

#### 4.2.1 概念说明

`clark_tr` 不是「一个时钟周期算完」，而是把运算拆成了**三级流水线**，每级用一个 `posedge clk` 把中间结果锁存进寄存器。这样做有两个好处：

- **时序宽松**：每一级只做几个加减法和移位，关键路径很短，主频可以拉得很高。
- **节拍对齐**：每一级都配一个使能寄存器（`en_s1`、`en_s2`、`o_en`），让「数据有效」的脉冲像接力棒一样逐级下传，保证输出端在正确的那一拍才认取数据。

这与全库统一的 `i_en/o_en` 单周期高电平脉冲握手约定一致（见 [u2-l1](u2-l1-foc-top-overview.md)）：脉冲到了哪一级，那一级寄存的数据就是「这一批」要算的。

#### 4.2.2 核心流程

三级流水线的节拍（设 `i_en` 在第 0 拍为高）：

```
拍0: i_en=1, ia/ib/ic 有效 ─┐
拍1: en_s1=1, 锁存 ax2_s1/bmc_s1/bpc_s1  (stage1: 算 2·Ia、Ib−Ic、Ib+Ic)
拍2: en_s2=1, 锁存 ialpha_s2 和三个 beta 部分和    (stage2: 算 Iα、Iβ 的移位项)
拍3: o_en =1, 输出 o_ialpha/o_ibeta               (output stage: 求和输出)
```

所以 `clark_tr` 的输入到输出延迟是 **3 个时钟周期**。这正是 [u2-l1](u2-l1-foc-top-overview.md) 里「Clark 贡献 3 拍」、整条 `en_adc → en_idq` 共 6 拍的由来（电流重构 1 + Clark 3 + Park 2）。

#### 4.2.3 源码精读

先看流水线寄存器的声明，三级各有一组：

[RTL/foc/clark_tr.v:18-24](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L18-L24) —— 声明 stage1 的 `en_s1, ax2_s1, bmc_s1, bpc_s1`，stage2 的 `en_s2, ialpha_s2, i_beta1_s2, i_beta2_s2, i_beta3_s2`。注意每个数据寄存器旁边都配了一个 `en_s*`，这就是「使能脉冲接力棒」。

stage1 的核心只有三句加减法 + 把 `i_en` 打一拍：

[RTL/foc/clark_tr.v:27-35](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L27-L35) —— `en_s1 <= i_en`（使能下传），`ax2_s1 <= i_ia << 1`（即 \(2I_a\)），`bmc_s1 <= i_ib - i_ic`（即 \(I_b - I_c\)，β 通路的「原料」），`bpc_s1 <= i_ib + i_ic`（即 \(I_b + I_c\)，α 通路要用）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手数清从 `i_en` 到 `o_en` 经过了几个时钟沿。
2. **操作步骤**：在 [clark_tr.v:27-65](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L27-L65) 里追踪 `en_s1`、`en_s2`、`o_en` 三个寄存器的赋值来源。
3. **需要观察的现象**：`en_s1 <= i_en`、`en_s2 <= en_s1`、`o_en <= en_s2`，三者首尾相接。
4. **预期结果**：一个 `i_en` 脉冲会在 3 个时钟周期后变成 `o_en` 脉冲输出，对应三级流水线的 3 拍延迟。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `clark_tr` 改成组合逻辑（不要流水线，`assign o_ialpha = 2*i_ia - i_ib - i_ic;`），会带来什么问题？

**参考答案**：功能上结果一样，但组合路径会变长（尤其是 β 通路的多次移位相加），关键路径时序变差，整条 FOC 链的主频会被它拖低。流水线拆级正是为了把这条长路径切断。

**练习 2**：为什么每级都要有一个 `en_s*` 寄存器跟着数据走，而不是只用一个全局 `i_en`？

**参考答案**：因为数据本身被流水线延迟了 3 拍，指示「数据有效」的脉冲也必须同样延迟 3 拍，下游才能知道「现在出现在 o_ialpha/o_ibeta 上的值是有效的」。如果直接把 `i_en` 接给下游，节拍就对不上了。

---

### 4.3 Iα 计算通路：放大 2 倍以避免整数截断

#### 4.3.1 概念说明

α 通路实现的就是 4.1 节推导的 \(I_\alpha = 2I_a - I_b - I_c\)。它比 β 通路简单得多——因为这里的系数（2、−1、−1）全是整数，不需要任何无理数近似，只需要移位（`<<1` 实现 ×2）和加减法。

回顾它的推导动机：原始投影公式 \(I_\alpha = I_a - \tfrac{1}{2}I_b - \tfrac{1}{2}I_c\) 里有 1/2，整体乘 2 消掉分数，得到 \(2I_\alpha = 2I_a - I_b - I_c\)。代码算的就是这个「放大 2 倍」后的值。

#### 4.3.2 核心流程

α 通路横跨两个流水线级：

```
stage1:  ax2_s1 = ia << 1        (= 2·Ia)
         bpc_s1 = ib + ic        (= Ib + Ic)
                 ↓
stage2:  ialpha_s2 = ax2_s1 − bpc_s1   (= 2·Ia − Ib − Ic = Iα)
                 ↓
output:  o_ialpha = ialpha_s2
```

注意 `ax2_s1`（2 倍 A 相）和 `bpc_s1`（B+C 相之和）是在 stage1 就并行算好的中间量，stage2 只做一次减法。这种「提前在上一级把公共子表达式算好」是手工流水线设计的常见技巧。

#### 4.3.3 源码精读

stage1 里 α 通路用到的两个中间量：

[RTL/foc/clark_tr.v:32-34](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L32-L34) —— `ax2_s1 <= i_ia << 1` 算 \(2I_a\)（左移 1 位等于乘 2）；`bpc_s1 <= i_ib + i_ic` 算 \(I_b + I_c\)。

stage2 里 α 通路的最终减法（就一句）：

[RTL/foc/clark_tr.v:43](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L43) —— `ialpha_s2 <= ax2_s1 - bpc_s1`，即 \(I_\alpha = 2I_a - (I_b + I_c) = 2I_a - I_b - I_c\)。

输出级把 `ialpha_s2` 直接转交给 `o_ialpha`：

[RTL/foc/clark_tr.v:60-64](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L60-L64) —— `if(en_s2) begin o_ialpha <= ialpha_s2; ... end`。注意这里用 `if(en_s2)` 包住，意味着只有「数据有效」那一拍才更新输出，避免把无效的中间垃圾透传出去。

#### 4.3.4 代码实践（源码阅读型 + 计算验证）

1. **实践目标**：验证 α 通路不会溢出 16 位有符号范围。
2. **操作步骤**：
   - 查端口注释 [clark_tr.v:13](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L13)，输入 `i_ia/i_ib/i_ic` 的范围是 −8191~8191。
   - 估算 \(I_\alpha = 2I_a - I_b - I_c\) 的极端值：当 \(I_a = 8191, I_b = I_c = -8191\) 时，\(I_\alpha = 2\times8191 -(-8191) -(-8191) = 32764\)。
3. **需要观察的现象**：32764 < 32767（16 位有符号最大值）。
4. **预期结果**：在最极端输入下 α 通路恰好不溢出。这正是作者把输入范围限定在 ±8191（而非满量程 ±32767）的原因——给「放大 2 倍 + 三相叠加」留出了不溢出的裕量。

#### 4.3.5 小练习与答案

**练习 1**：为什么 α 通路的 `i_ia << 1` 用左移，而不是写 `i_ia * 2`？

**参考答案**：两者综合结果通常一致，但左移 `<<` 在硬件上明确表达「这就是一根连线挪一位、不花乘法器」的意图，可读性和可综合性都更好；`*2` 则可能让某些综合工具真的去调一个乘法器（虽然聪明的工具会优化掉）。

**练习 2**：stage1 为什么要单独算一个 `bpc_s1 = ib + ic`，而不是在 stage2 直接写 `ax2_s1 - i_ib - i_ic`？

**参考答案**：因为 `i_ib/i_ic` 是 stage1 的输入，到了 stage2 已经「过期」了（流水线里每级只能看到上一级锁存的值）。stage2 想用 \(I_b + I_c\)，就必须在 stage1 先把它算好锁进 `bpc_s1`。这正是流水线设计的核心约束：跨级的数据必须显式打拍。

---

### 4.4 Iβ 计算通路：用移位加法近似 √3 的定点技巧

> ⚠️ 这一节是本讲的核心，也是本讲义规定的主实践任务所在。请重点阅读。

#### 4.4.1 概念说明

β 通路要实现 \(I_\beta = \sqrt{3}(I_b - I_c)\)。难点在 \(\sqrt{3}\) 这个无理系数（≈1.7320508）。如前所述，作者没有用乘法器，而是把 \(\sqrt{3}\) 拆成了一串「2 的整数次幂的倒数」之和，每一项都用**算术右移**实现。

先看一个关键写法：`$signed({{N{bmc_s1[15]}}, bmc_s1[15:k]}})`。这是一段「符号扩展的算术右移」模板，含义是：

- `bmc_s1[15]` 是 16 位有符号数 `bmc_s1` 的符号位（最高位）。
- `{N{bmc_s1[15]}}` 把符号位复制 N 份。
- `bmc_s1[15:k]` 取 `bmc_s1` 的最高 (16−k) 位。
- 拼起来仍是 16 位，等于把 `bmc_s1` **算术右移 k 位**，即 \(\lfloor bmc\_s1 / 2^k \rfloor\)（对负数也是符号扩展，不会变成无符号逻辑右移）。

验证一下位数：`{k{sign}}`（k 位）+ `bmc_s1[15:k]`（16−k 位）= 16 位，正好。所以这个模板可以稳定地产出一个 16 位有符号的「bmc_s1 除以 \(2^k\)」。

> 为什么不直接写 `bmc_s1 >>> k`（Verilog-2001 的算术右移）？其实也可以，而且更简洁。作者用显式拼接的写法，是为了让符号扩展和位宽在所有综合工具下都毫无歧义，是一种强调可移植性的风格。第 4.4.5 节有一道练习正是让你比较这两种写法。

#### 4.4.2 核心流程

β 通路的「原料」是 stage1 算好的 `bmc_s1 = ib - ic`。stage2 把它复制成 9 个移位项分别右移 0、1、3、4、5、7、8、10、11 位，再分到三组部分和 `i_beta1_s2 / i_beta2_s2 / i_beta3_s2` 里：

```
stage1:  bmc_s1 = ib − ic
                ↓
stage2:  i_beta1_s2 = bmc_s1·(1 + 1/2 + 1/8)            ← 用 >>0, >>1, >>3
         i_beta2_s2 = bmc_s1·(1/16 + 1/32 + 1/128)       ← 用 >>4, >>5, >>7
         i_beta3_s2 = bmc_s1·(1/256 + 1/1024 + 1/2048)   ← 用 >>8, >>10, >>11
                ↓
output:  o_ibeta = i_beta1_s2 + i_beta2_s2 + i_beta3_s2
```

三个部分和加起来，等于 `bmc_s1` 乘以这 9 个系数之和。下一小节就来算这个和到底有多接近 \(\sqrt{3}\)。

#### 4.4.3 源码精读

stage1 提供 β 通路的原料：

[RTL/foc/clark_tr.v:33](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L33) —— `bmc_s1 <= i_ib - i_ic`，即 \(I_b - I_c\)，它是 β 通路唯一的输入源。

stage2 的三组部分和（本讲最关键的代码）：

[RTL/foc/clark_tr.v:44-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L44-L52) —— 这里把 `bmc_s1` 用前面讲的「符号扩展右移」模板拆成 9 个移位项，分成三组累加。第 44–46 行是 `i_beta1_s2`（含原值 `bmc_s1` 本身，即 >>0），第 47–49 行是 `i_beta2_s2`，第 50–52 行是 `i_beta3_s2`。

输出级把三组部分和相加得到最终的 `o_ibeta`：

[RTL/foc/clark_tr.v:63](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L63) —— `o_ibeta <= i_beta1_s2 + i_beta2_s2 + i_beta3_s2`。

#### 4.4.4 代码实践（本讲主实践任务：手算 √3 近似与误差）

这是本讲义规定的实践任务。**这是一个纯计算/源码阅读型实践，不需要运行仿真**，但要拿出一张表和一个误差数字。

1. **实践目标**：把 `i_beta1_s2 / i_beta2_s2 / i_beta3_s2` 里的每一项换算成相对 `bmc_s1` 的乘数，验证三段之和近似 \(\sqrt{3}\)，并算出近似误差。

2. **操作步骤**：

   对照 [clark_tr.v:44-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L44-L52)，按下表逐项把「符号扩展右移模板」翻译成乘数。模板 `$signed({{N{bmc_s1[15]}}, bmc_s1[15:k]}})` = 算术右移 k 位 = 乘以 \(1/2^k\)（其中 `bmc_s1` 本身 = 右移 0 位 = 乘 1）：

   | 所属 | 代码片段 | 右移位数 k | 乘数（相对 bmc_s1） |
   |---|---|---|---|
   | i_beta1_s2 | `bmc_s1`（原值） | 0 | \(1\) |
   | i_beta1_s2 | `{{1{...}}, bmc_s1[15:1]}` | 1 | \(1/2\) |
   | i_beta1_s2 | `{{3{...}}, bmc_s1[15:3]}` | 3 | \(1/8\) |
   | i_beta2_s2 | `{{4{...}}, bmc_s1[15:4]}` | 4 | \(1/16\) |
   | i_beta2_s2 | `{{5{...}}, bmc_s1[15:5]}` | 5 | \(1/32\) |
   | i_beta2_s2 | `{{7{...}}, bmc_s1[15:7]}` | 7 | \(1/128\) |
   | i_beta3_s2 | `{{8{...}}, bmc_s1[15:8]}` | 8 | \(1/256\) |
   | i_beta3_s2 | `{{10{...}}, bmc_s1[15:10]}` | 10 | \(1/1024\) |
   | i_beta3_s2 | `{{11{...}}, bmc_s1[15:11]}` | 11 | \(1/2048\) |

3. **把三段分别求和**：

   \[
   \text{i\_beta1\_s2} = bmc\_s1 \cdot (1 + \tfrac{1}{2} + \tfrac{1}{8}) = bmc\_s1 \cdot 1.625
   \]

   \[
   \text{i\_beta2\_s2} = bmc\_s1 \cdot (\tfrac{1}{16} + \tfrac{1}{32} + \tfrac{1}{128}) = bmc\_s1 \cdot 0.1015625
   \]

   \[
   \text{i\_beta3\_s2} = bmc\_s1 \cdot (\tfrac{1}{256} + \tfrac{1}{1024} + \tfrac{1}{2048}) = bmc\_s1 \cdot 0.00537109375
   \]

4. **需要观察的现象（总和 vs √3）**：把三段加起来：

   \[
   1.625 + 0.1015625 + 0.00537109375 = 1.73193359375
   \]

   而真实值：

   \[
   \sqrt{3} \approx 1.7320508075688772
   \]

   - 绝对误差：\(1.73205080757 - 1.73193359375 \approx 0.00011721\)
   - 相对误差：\(\dfrac{0.00011721}{1.73205080757} \approx 0.0000677 \approx 0.0068\%\)

5. **预期结果**：这 9 个移位项之和 \(1.73193359375\) 把 \(\sqrt{3}\) 逼近到了**相对误差仅约 0.007%**。对于 FOC 这种靠 PI 闭环纠错的线性系统，这个误差完全可以被 PID 的积分项吃掉，根本看不出影响。这就是作者敢于用移位加法替代乘法器的底气。

6. **补充观察**：分三段（`i_beta1/2/3_s2`）而不是一段写完，是为了让每段的位宽增长可控、便于综合工具并行处理三个独立的加法树，时序更友好。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `i_beta3_s2` 那三行（最小的 1/256、1/1024、1/2048 三项）整个删掉，相对误差会变差多少？值得吗？

**参考答案**：删掉后系数和变成 \(1.73193359375 - 0.00537109375 = 1.7265625\)，绝对误差 ≈ 0.00549，相对误差 ≈ 0.317%，比原来（0.007%）差了约 47 倍。虽然对 FOC 闭环仍可能勉强工作，但误差明显变大，所以加上这三行成本极低（就是三个移位加法）的「精修项」是划算的。

**练习 2**：把 `$signed({{3{bmc_s1[15]}}, bmc_s1[15:3]}})` 改写成等价的 Verilog-2001 算术右移写法。

**参考答案**：因为 `bmc_s1` 已声明为 `reg signed [15:0]`，可直接写 `bmc_s1 >>> 3`（`>>>` 对有符号操作数做算术右移）。两者语义等价，显式拼接写法只是更强调符号扩展、可移植性更好。

**练习 3**：为什么 β 通路的 9 个系数里没有 \(1/4\)（>>2）、\(1/6\) 之类的项，而是挑了 0、1、3、4、5、7、8、10、11 这一组？

**参考答案**：因为这些「2 的整数次幂倒数」之和能以最少项数、最高精度逼近 \(\sqrt{3}\)（类似二进制展开逼近目标值）。0、1、3、4、5、7、8、10、11 这组移位量是作者搜索出来的一组高精度解：跳过 2、6、9 位能在不显著损失精度的前提下减少加法项数。这是一种典型的「定点系数逼近」设计选择。

## 5. 综合实践：跑仿真，眼见为实地验证 Clark 变换

把本讲学的「数学公式 + 移位近似」放到仿真里验证一遍。这个实践承接 [u1-l4](u1-l4-iverilog-simulation.md) 教过的 iverilog + gtkwave 流程。

### 实践目标

用 [`SIM/tb_clark_park_tr.v`](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v) 跑出波形，亲眼看到：

1. 输入 `ia/ib/ic` 是三路互差 120° 的正弦波；
2. Clark 变换后 `ialpha` 与 `ibeta` 是两路**相位差 90°（正交）**、**幅值相等**的正弦波；
3. 验证 4.4 节算出的 √3 近似确实让 `ibeta` 幅值落在预期范围内。

### 操作步骤

1. **理解激励是怎么来的**。testbench 借用了 `sincos` 模块（本来是给 Park 变换算 sin/cos 用的，这里临时当正弦信号发生器）合成三相正弦：

   [SIM/tb_clark_park_tr.v:37-65](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L37-L65) —— 三个 `sincos` 实例分别给 `ia/ib/ic`，相位互差 120°（注释里用 `(2/3)π`、`(1/3)π`、`0` 配出），振幅 ±16384。

   [SIM/tb_clark_park_tr.v:68-78](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L68-L78) —— 注意输入 `clark_tr` 前先除以 2（`ia / 16'sd2`），把振幅压到 ±8192，正好落在端口注释要求的 ±8191 范围内（也对应 4.3 节留的不溢出裕量）。

2. **编译并运行**（参考 [u1-l4](u1-l4-iverilog-simulation.md)）。若在 Linux 下，可手动执行 [tb_clark_park_tr_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr_run_iverilog.bat) 里的等价命令：

   ```bash
   cd SIM
   iverilog -g2001 -o sim.out tb_clark_park_tr.v ../RTL/foc/sincos.v ../RTL/foc/clark_tr.v ../RTL/foc/park_tr.v
   vvp -n sim.out
   ```

   运行后会生成 `dump.vcd`。

3. **用 gtkwave 打开 `dump.vcd`**，把 `ia, ib, ic, ialpha, ibeta` 都设为 **Signed Decimal → Analog → Step**（正弦/负半周才能正确显示，方法见 [u1-l4](u1-l4-iverilog-simulation.md)）。

### 需要观察的现象与预期结果

- **三相输入**：`ia/ib/ic` 是三路等幅（±8192）、相位互差 120° 的正弦波。
- **αβ 正交**：`ialpha` 与 `ibeta` 是两路等幅正弦波，且 `ibeta` 相位比 `ialpha` **滞后 90°（π/2）**——这就是 Clark 变换「三相互差 120° → 两相互差 90°」的直观体现。
- **幅值核对**：因为代码把公式放大了 2 倍，且三相对称时 \(I_\alpha = 3I_a\)（见 4.1.5 练习 1），所以 `ialpha` 的振幅应为 \(3 \times 8192 = 24576\)。`ibeta` 振幅理论上也是 24576（因为 \(I_\beta = \sqrt{3}(I_b-I_c)\)，而 \(I_b-I_c\) 的振幅是 \(\sqrt{3}\times8192\)，相乘后仍是 \(3\times8192\)）；由于 √3 被近似，实测 `ibeta` 振幅会比 24576 略低约 0.007%（4.4 节算出的相对误差），这种微差在波形上几乎看不出来，正好印证「近似精度足够」。
- **如果波形对不上**：先检查是否把信号设成了 signed（否则负半周会变成巨大的正值），再检查是否漏编译了 `sincos.v`（会报模块未定义）。

> 若无法本地运行 iverilog/gtkwave，可标注「待本地验证」，转而用 4.4 节的手算结果作为理论依据：Clark 变换的数学正确性已由公式推导保证，√3 近似的误差也已量化到 0.007%。

## 6. 本讲小结

- Clark 变换把三相电流 \(I_a/I_b/I_c\) 投影成两个正交分量 \(I_\alpha/I_\beta\)，是 FOC 电流环里「三相 → 两相」的第一步，承接电流重构、喂给 Park 变换。
- 代码用的公式 \(I_\alpha = 2I_a - I_b - I_c\)、\(I_\beta = \sqrt{3}(I_b - I_c)\) 是把投影公式整体放大 2 倍得到的，目的是消掉 1/2 避免整数截断；这个统一增益由 PI 控制器吸收，对 FOC 无害。
- `clark_tr` 是**三级流水线**（stage1 → stage2 → output），配 `en_s1/en_s2/o_en` 三个使能寄存器让数据脉冲逐级下传，输入到输出延迟 3 个时钟周期。
- α 通路只用左移（`<<1`）和加减法，因为系数全是整数；β 通路则用 `$signed({{N{sign}}, val[15:k]}})` 这种「符号扩展算术右移」模板，把 \(\sqrt{3}\) 拆成 9 个「2 的整数次幂倒数」之和来逼近。
- 手算验证：9 项之和 \(1.73193359375\) 把 \(\sqrt{3}\)（≈1.7320508）逼近到相对误差仅 **≈0.007%**，精度足够交给 PID 吃掉。
- 输入范围被刻意限定在 ±8191，是为了给「放大 2 倍 + 三相叠加」留出 16 位有符号不溢出的裕量（极端值 32764 < 32767）。

## 7. 下一步学习建议

- **下一讲 [u2-l4 Park 变换与 sincos 计算器](u2-l4-park-and-sincos.md)**：本讲得到的 `ialpha/ibeta` 还在**定子**直角坐标系里。Park 变换会把它旋转到跟随转子转动的 **dq 坐标系**，让正弦电流「坍缩」成近似直流，这样 PI 才能对它做无静差控制。届时你会看到 `park_tr` 如何调用 `sincos` 模块算出 \(\sin\psi/\cos\psi\)，并理解 4.4 节这类定点技巧为何贯穿全库。
- **延伸阅读**：
  - 想巩固「符号扩展右移」和饱和截断的定点套路，可先扫一眼 [RTL/foc/pi_controller.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v) 里的 `protect_add/protect_mul`（会在 [u2-l5](u2-l5-pi-controller.md) 精读）。
  - 想看 Clark 变换下游怎么消费 `ialpha/ibeta`，直接读 [foc_top.v:140-150](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L140-L150) 的 `park_tr` 例化。
  - 第 [u4-l1 定点数运算与饱和保护](u4-l1-fixed-point-and-saturation.md) 会系统汇总全库的定点标度约定，把本讲的 √3 近似放到全局视角里看。
