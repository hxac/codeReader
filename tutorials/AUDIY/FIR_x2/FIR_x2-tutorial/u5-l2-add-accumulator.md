# ADD：累加积分器与 LRCKx2 复位

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `ADD` 模块如何用一个 `ADD_REG` 寄存器把乘法器送来的乘积逐拍累加成「卷积和」。
- 解释 `LRCKx2` 下降沿检测（`~LRCKx2_I & LRCKx2_REG`）如何触发「输出累加和 → 复位重新开始」这一关键动作。
- 说明 `NRST_I` 复位时累加器与输出寄存器分别取什么值，以及 `BCKx2_O` 在地址位宽不足时为何兜底到 `MCLK_I`。
- 能用 `ADD_TB` 在 Questa 中跑通仿真，并在波形里找到「一个 `LRCKx2` 周期内逐步累加、下降沿一次性输出」的证据。

本讲承接 [u5-l1（MULT 有符号乘法器）](./u5-l1-mult-pipeline.md)：上一讲的乘法器输出 48 位乘积 `MULT_DATA` 与对齐节拍 `LRCKx2_MULT`，正是本讲 `ADD` 模块的两个核心输入。

## 2. 前置知识

### 2.1 FIR 滤波就是「乘加的累加」

一个 N 抽头 FIR 滤波器的本质是离散卷积：

\[ y[n] = \sum_{k=0}^{N-1} h[k] \cdot x[n-k] \]

即：把输入样点 \(x\) 与系数 \(h\) 两两相乘，再把所有乘积加起来，得到一个输出样点 \(y[n]\)。前面的模块已经帮我们算出了每一项的乘积（`MULT` 模块），现在只差「把乘积累加成最终结果」这一步——这正是 `ADD` 模块要做的。

### 2.2 多相分解下的「每周期 256 次乘加」

FIR_x2 是 2 倍过采样滤波器，512 个系数经多相奇偶分解后，每个过采样样点只需累加 256 个乘积（参见 [u4-l2](./u4-l2-sprom-cont-polyphase-clock.md)）。而 `LRCKx2`（2 倍采样节拍）的周期恰为 256 个 `MCLK`（参见 [u2-l2](./u2-l2-audio-clock-model.md)）。这两个「256」天然对上：**一个 `LRCKx2` 周期内累加 256 个乘积，周期结束就输出一个过采样样点**。

### 2.3 下降沿检测：把「长时间电平」压成「一次性脉冲」

`LRCKx2` 在半个周期内持续为高（或为低），我们不能在每个 `MCLK` 都复位累加器。需要一个电路识别「刚才还是高、现在变低了」这一瞬间——即下降沿。做法是记住上一拍的值（`LRCKx2_REG`），再用「当前为低 且 上一拍为高」来判断，这一思路在 [u3-l2](./u3-l2-dpram-ring-buffer-controller.md) 的 `LRCK` 上升沿检测中已经见过。

### 2.4 非阻塞赋值与时序对齐契约

回顾 [u5-l1](./u5-l1-mult-pipeline.md) 建立的「对齐契约」：数据每过一级寄存器延迟 1 拍，配套的节拍信号 `LRCKx2` 也要相应打拍，保证数据与节拍在输出端同拍抵达。本讲会看到 `MULT_I` 与 `LRCKx2_I` 在 `ADD` 内部各自再延迟 1 拍（`MULT_REG` 与 `LRCKx2_REG`），二者仍然同步。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [06_ADD/ADD.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v) | 被测设计（DUT）：累加积分器，逐拍累加乘积，下降沿输出并复位。 |
| [06_ADD/ADD_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD_TB.v) | 测试激励：把 `DATA_BUFFER`、`FIR_COEF`、`MULT`、`ADD` 四级串起来，喂入 PCM 样点。 |
| [06_ADD/Questa/ADD.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/Questa/ADD.bat) | 仿真批处理：`vlib/vlog/vsim` 编译并启动仿真。 |
| [06_ADD/Questa/run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/Questa/run.do) | 波形脚本：`add wave`、`run -all`、覆盖率报告。 |

> 注意：`ADD_TB.v` 不是单独测 `ADD`，而是把 `DATA_BUFFER → FIR_COEF → MULT → ADD` 四级全部实例化，给 `ADD` 喂入真实通路产生的乘积。这样能观察到与实际滤波器一致的行为。

## 4. 核心概念与源码讲解

### 4.1 累加递推时序

#### 4.1.1 概念说明

`ADD` 模块的核心是一根「累加寄存器」`ADD_REG`。每个 `MCLK` 上升沿，它把当前乘积加到自己身上：

\[ \text{ADD\_REG}[t+1] = \text{ADD\_REG}[t] + \text{MULT\_REG}[t] \]

这里 `MULT_REG` 是输入寄存器，把外部送来的 `MULT_I`（来自 `MULT` 模块的 48 位乘积）打一拍。引入 `MULT_REG` 有两个目的：

1. **改善时序**：乘法器输出到累加器之间加一级寄存器，拆分组合路径，便于跑高频。
2. **保持对齐**：`MULT_I` 延迟 1 拍变 `MULT_REG`，与之配套的 `LRCKx2_I` 也延迟 1 拍变 `LRCKx2_REG`，二者同步，下降沿检测才能对准正确的乘积。

这样经过 256 个 `MCLK`，`ADD_REG` 里就累加了一个过采样样点所需的全部 256 个乘积。

#### 4.1.2 核心流程

```text
每个 MCLK 上升沿（普通周期）：
    MULT_REG  ← MULT_I            // 输入打一拍
    LRCKx2_REG ← LRCKx2_I         // 节拍打一拍（同时用于下降沿检测与延迟输出）
    ADD_REG   ← ADD_REG + MULT_REG // 累加递推
```

> 这是「非下降沿」周期的行为；下降沿周期的特殊处理见 4.2。

#### 4.1.3 源码精读

输入寄存器与累加递推在同一 `always` 块里，[06_ADD/ADD.v:78-80](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L78-L80) 先打拍：

```verilog
MULT_REG   <= MULT_I;            // 乘积输入寄存
LRCKx2_REG <= LRCKx2_I;          // 节拍输入寄存（兼作下降沿检测与延迟输出）
BCKx2_REG  <= (RAM_ADDR_WIDTH >= 7) ? BCKx2_I : 1'b0;
```

[06_ADD/ADD.v:86-89](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L86-L89) 是普通周期的累加递推：

```verilog
end else begin
    /* Normal Operation */
    ADD_REG <= (NRST_I == 1'b1) ? (ADD_REG + MULT_REG) : {MULT_WIDTH{1'b0}};
end
```

注意几个要点：

- `ADD_REG` 与 `MULT_REG` 都声明为 `signed`（[06_ADD/ADD.v:70-72](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L70-L72)），相加时按二补码符号扩展，正负乘积都能正确累加。
- 位宽都是 `MULT_WIDTH`（48 位），累加过程中不会溢出（系数和做了溢出检查，详见 [u6-l1](./u6-l1-fir-coefficient-generation.md)）。
- 由于是非阻塞赋值（`<=`），`ADD_REG + MULT_REG` 用的是**本拍之前**的旧值，符合时序逻辑语义。

#### 4.1.4 代码实践

**实践目标**：看清 `MULT_REG` 与 `ADD_REG` 的逐拍递推关系。

**操作步骤**：

1. 打开 Questa，进入 `06_ADD/Questa/`，运行 `ADD.bat` 启动仿真。
2. `run.do` 默认只加了端口级信号。在波形窗口手动追加内部寄存器：
   ```tcl
   add wave -position insertpoint sim:/ADD_TB/u_ADD/MULT_I
   add wave -position insertpoint sim:/ADD_TB/u_ADD/MULT_REG
   add wave -position insertpoint sim:/ADD_TB/u_ADD/ADD_REG
   add wave -position insertpoint sim:/ADD_TB/u_ADD/LRCKx2_I
   ```
3. 重启并放大波形（`restart; run 5000`），找一段 `LRCKx2_I` 稳定为高电平的区间。

**需要观察的现象**：

- `MULT_REG` 比 `MULT_I` 滞后正好 1 个 `MCLK`（输入寄存器效果）。
- 每来一个 `MCLK` 上升沿，`ADD_REG` 的值增加「上一个 `MULT_REG`」，呈阶梯式递增（或递减，视系数与数据符号而定）。

**预期结果**：在一个 `LRCKx2` 周期内，`ADD_REG` 从某个起始值开始，逐拍累加 256 个 `MULT_REG`，呈阶梯状变化。若本地未装 Questa，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么用 `MULT_REG`（打一拍后的值）参与累加，而不是直接用 `MULT_I`？

> **参考答案**：直接用 `MULT_I` 会让「乘法器组合输出 → 加法」连成一条长组合路径，限制最高工作时钟；打一拍后路径被寄存器切开。同时 `MULT_I` 延迟 1 拍，与 `LRCKx2_I` 延迟 1 拍（`LRCKx2_REG`）保持同步，下降沿检测才能对准对应的乘积。

**练习 2**：`ADD_REG` 与 `MULT_REG` 为什么都声明为 `signed`？

> **参考答案**：FIR 卷积中乘积有正有负（系数可正可负），Verilog 中只有两边都 `signed` 才会按二补码做符号扩展的加法；否则会被当作无符号数相加，导致负数处理错误。

---

### 4.2 LRCKx2 下降沿复位

#### 4.2.1 概念说明

累加不能无限进行下去。每累加满 256 个乘积（一个 `LRCKx2` 周期），就要：

1. **输出**：把累加好的卷积和送到输出寄存器 `ADDO_REG`，供下游饱和/舍入使用。
2. **复位并重启**：把 `ADD_REG` 清零，为下一个过采样样点重新开始累加。

本设计的巧妙之处在于——它**不在复位周期里浪费一拍**。复位的那一拍，`ADD_REG` 不是被清零，而是直接装载当前乘积 `MULT_REG`，让这个乘积成为下一个样点累加的「第一项」。这样 256 个乘积一个不少。

触发这两件事的时机是 `LRCKx2` 的下降沿：因为 `LRCKx2` 一个完整周期（高电平 + 低电平）正好对应累加一个过采样样点的全部时间，下降沿标志着「这一个样点累加完毕」。

#### 4.2.2 核心流程

下降沿检测条件（注意 Verilog 运算符优先级）：

\[ \texttt{\textasciitilde LRCKx2\_I} \;\&\; (\texttt{LRCKx2\_REG} == 1) \]

`==` 的优先级高于 `&`，所以等价于「`LRCKx2_I` 当前为低 **且** 上一拍（`LRCKx2_REG`）为高」——即下降沿那一拍。

下降沿那拍的动作：

```text
if (下降沿)：
    ADDO_REG ← ADD_REG          // 输出本周期累加好的卷积和
    ADD_REG  ← MULT_REG          // 不清零，而是用当前乘积作为下一周期的第一项
```

完整的「周期 → 周期」时序如下（设相邻两个下降沿分别在第 T0 拍和第 T0+P 拍，P=256）：

| 拍号 | LRCKx2_I | LRCKx2_REG(旧) | 动作 | ADD_REG(下一拍值) |
|------|----------|----------------|------|-------------------|
| T0   | 0（下降沿）| 1              | 输出 + 重启 | MULT_REG[T0] |
| T0+1 | 0        | 0              | 累加 | MULT_REG[T0] + MULT_REG[T0+1] |
| ...  | ...      | ...            | 累加 | 逐项累加 |
| T0+P-1 | 0      | 0              | 累加 | Σ MULT_REG[T0 .. T0+P-1]（共 P 项）|
| T0+P | 0（下降沿）| 1              | 输出 + 重启 | MULT_REG[T0+P] |

可见在第 T0+P 拍，`ADDO_REG` 捕获的正是恰好 P=256 个乘积之和——一个完整的过采样样点。

#### 4.2.3 源码精读

下降沿检测与复位在 [06_ADD/ADD.v:82-85](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L82-L85)：

```verilog
if (~LRCKx2_I & LRCKx2_REG == 1'b1) begin
    /* Negedge of LRCKx2: Reset Adder. */
    ADD_REG <= (NRST_I == 1'b1) ? MULT_REG : {MULT_WIDTH{1'b0}};
    ADDO_REG <= (NRST_I == 1'b1) ? ADD_REG : {MULT_WIDTH{1'b0}};
end
```

解读：

- **下降沿条件** `~LRCKx2_I & LRCKx2_REG == 1'b1`：因 `==` 优先级高于 `&`，实际是 `(~LRCKx2_I) & (LRCKx2_REG == 1'b1)`，即「当前低、上拍高」。`LRCKx2_REG == 1'b1` 写法上等价于直接用 `LRCKx2_REG`，这里显式比较是为了可读性。
- **`ADD_REG <= MULT_REG`**：复位不是清零，而是用当前乘积重新「播种」。`NRST_I` 无效时才真正清零（详见 4.3）。
- **`ADDO_REG <= ADD_REG`**：把本周期累加完毕的和（旧 `ADD_REG`）锁存到输出。由于 `ADDO_REG` 只在下降沿更新，输出 `ADD_O` 每个过采样样点只变一次，样点之间保持稳定。

输出赋值在 [06_ADD/ADD.v:93-94](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L93-L94)：

```verilog
assign ADD_O    = ADDO_REG;   // 卷积和输出（每样点更新一次）
assign LRCKx2_O = LRCKx2_REG; // 节拍延迟 1 拍输出，与 ADD_O 对齐
```

注意 `LRCKx2_REG` 一物两用：既是下降沿检测所需的「上一拍值」，又直接作为延迟 1 拍的输出节拍 `LRCKx2_O`，确保 `ADD_O` 与 `LRCKx2_O` 同拍变化，下游（[FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) 顶层最后一级流水线）能正确采样。

#### 4.2.4 代码实践

**实践目标**：在波形里抓到「下降沿瞬间 `ADD_O` 跳变、`ADD_REG` 回到种子值」。

**操作步骤**：

1. 在前述波形基础上再追加输出寄存器：
   ```tcl
   add wave -position insertpoint sim:/ADD_TB/u_ADD/ADDO_REG
   add wave -position insertpoint sim:/ADD_TB/u_ADD/ADD_O
   ```
2. 把 `LRCKx2_I`、`LRCKx2_REG`、`ADD_REG`、`ADDO_REG` 放在相邻行。
3. 在 `LRCKx2_I` 由 1 变 0 的位置放光标。

**需要观察的现象**：

- 下降沿那一拍：`ADDO_REG`（即 `ADD_O`）跳变为 `ADD_REG` 此前的累加和；同一拍 `ADD_REG` 变为当前的 `MULT_REG`（种子值），下一拍起重新阶梯递增。
- `LRCKx2_O`（= `LRCKx2_REG`）比 `LRCKx2_I` 滞后 1 个 `MCLK`。

**预期结果**：每个 `LRCKx2` 下降沿对应一次「`ADD_O` 输出新样点 + `ADD_REG` 复位重启」，两者发生在同一 `MCLK` 上升沿。若本地未装 Questa，则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把复位那一拍的 `ADD_REG <= MULT_REG` 改成 `ADD_REG <= 0`（清零），会对滤波结果产生什么影响？

> **参考答案**：每个过采样样点会少累加 1 个乘积（少算下降沿那拍的 `MULT_REG`），导致卷积和偏小、滤波器增益与特性偏离设计值。当前设计用「装载种子」代替「清零」，正是为了不丢失这一拍，保证每个样点恰好累加满 256 项。

**练习 2**：`ADDO_REG` 在非下降沿周期是否更新？这意味着输出 `ADD_O` 的更新频率是多少？

> **参考答案**：不更新。`ADDO_REG` 只在 `if`（下降沿）分支里被赋值，普通 `else` 分支不碰它。因此 `ADD_O` 每个 `LRCKx2` 周期（= 每个过采样样点）只更新一次，样点之间保持上一次的值稳定输出——这正是「采样保持」的预期行为。

---

### 4.3 复位值选择

#### 4.3.1 概念说明

`ADD` 模块有两个层面的「复位」概念，要区分清楚：

1. **系统复位 `NRST_I`（低有效）**：外部输入。当 `NRST_I = 0` 时，累加器与输出寄存器都被强制写 0，保证上电或异常时输出干净。当 `NRST_I = 1` 时，才是正常的累加/输出行为。
2. **周期性下降沿复位**：这是 4.2 讲的「每个样点结束时的内部重启」，与 `NRST_I` 无关。

本模块用三元运算符 `(... NRST_I ...)` 把这两种情况统一编码：`NRST_I` 为真时走正常值，为假时走全 0。这样只需一套逻辑。

此外，输出 `BCKx2_O` 还有一个与地址位宽相关的「兜底」选择：当 `RAM_ADDR_WIDTH < 7` 时（地址太窄，标准音频时钟比例不成立），`BCKx2` 无法由地址派生，模块就把 `BCKx2_O` 直接接到 `MCLK_I` 作为占位，避免悬空。这一阈值逻辑与 `MULT`、`FIR_COEF` 中的处理一致。

#### 4.3.2 核心流程

```text
if (下降沿)：
    ADDO_REG ← (NRST_I==1) ? ADD_REG   : 0
    ADD_REG  ← (NRST_I==1) ? MULT_REG  : 0
else（普通周期）：
    ADD_REG  ← (NRST_I==1) ? (ADD_REG + MULT_REG) : 0

BCKx2_O ← (RAM_ADDR_WIDTH >= 7) ? BCKx2_REG : MCLK_I
```

复位真值表：

| NRST_I | 所处周期 | ADD_REG(下一拍) | ADDO_REG(下一拍) | 含义 |
|--------|----------|-----------------|------------------|------|
| 1 | 下降沿 | MULT_REG（种子）| ADD_REG（输出本样点和）| 正常输出 + 重启 |
| 1 | 普通 | ADD_REG + MULT_REG | 不变 | 正常累加 |
| 0 | 下降沿 | 0 | 0 | 系统复位，全清零 |
| 0 | 普通 | 0 | 不变 | 系统复位，保持清零 |

#### 4.3.3 源码精读

复位值选择散布在 [06_ADD/ADD.v:82-89](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L82-L89) 的每个赋值里：

```verilog
if (~LRCKx2_I & LRCKx2_REG == 1'b1) begin
    ADD_REG  <= (NRST_I == 1'b1) ? MULT_REG : {MULT_WIDTH{1'b0}};
    ADDO_REG <= (NRST_I == 1'b1) ? ADD_REG  : {MULT_WIDTH{1'b0}};
end else begin
    ADD_REG  <= (NRST_I == 1'b1) ? (ADD_REG + MULT_REG) : {MULT_WIDTH{1'b0}};
end
```

`{MULT_WIDTH{1'b0}}` 是位宽参数化的全 0 写法，保证无论 `MULT_WIDTH` 取何值都能正确清零。

`BCKx2` 的兜底在 [06_ADD/ADD.v:80](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L80) 与 [06_ADD/ADD.v:95](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L95) 两处：

```verilog
BCKx2_REG <= (RAM_ADDR_WIDTH >= 7) ? BCKx2_I : 1'b0;          // 第 80 行
...
assign BCKx2_O = (RAM_ADDR_WIDTH >= 7) ? BCKx2_REG : MCLK_I;  // 第 95 行
```

默认 `RAM_ADDR_WIDTH = 8`（≥ 7），走正常路径：`BCKx2_O = BCKx2_I` 延迟 1 拍。若地址位宽不足 7，则兜底输出 `MCLK_I`。注意兜底分支里 `BCKx2_REG` 被写成常量 `1'b0`（无意义占位），真正的输出直接取 `MCLK_I`。

> 这里的 `RAM_ADDR_WIDTH` 对应顶层的 `WADDR_WIDTH`（数据 RAM 地址位宽），在 [FIR_x2.v:153-156](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L153-L156) 实例化 `ADD` 时传入。

#### 4.3.4 代码实践

**实践目标**：观察 `NRST_I` 拉低时累加器被强制清零的行为。

**操作步骤**：

1. 阅读 [06_ADD/ADD_TB.v:156-161](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD_TB.v#L156-L161)，里面有一段被注释掉的复位产生器：
   ```verilog
   /*
   always begin
       #4989 NRST_I <= 1'b0;
       #5    NRST_I <= 1'b1;
   end
   */
   ```
2. 取消这段注释（仅在你的本地仿真副本里改 TB，**不要改仓库源码**），重新跑 `ADD.bat`。
3. 在波形里同时观察 `NRST_I`、`ADD_REG`、`ADDO_REG`。

**需要观察的现象**：

- `NRST_I` 拉低期间，`ADD_REG` 立即变 0 并保持；无论是否遇到下降沿，`ADDO_REG` 也会在下降沿被写 0。
- `NRST_I` 恢复高电平后，`ADD_REG` 从下一拍起重新开始累加。

**预期结果**：复位低有效，强制累加通路归零；释放后从 `MULT_REG` 种子重新累加。若不修改 TB（保持注释），则 `NRST_I` 全程为 1，只观察正常累加路径——此时「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`NRST_I = 0` 期间遇到一次 `LRCKx2` 下降沿，`ADDO_REG` 会变成什么？这意味着什么？

> **参考答案**：变成 0。因为下降沿分支里 `ADDO_REG <= (NRST_I==1) ? ADD_REG : 0`，复位时取 0。这意味着复位期间输出 `ADD_O` 会被强制拉到 0，下游不会读到半截累加的脏数据。

**练习 2**：为什么 `BCKx2_O` 在 `RAM_ADDR_WIDTH < 7` 时要兜底到 `MCLK_I`，而不是直接给 0？

> **参考答案**：`BCKx2_O` 是要送给下游（如外部 DAC 或顶层输出）的位时钟信号，若给常量 0 会丢失时钟节拍，导致下游无法采样数据。当地址位宽不足以派生正确的 `BCKx2` 时，回退到稳定的 `MCLK_I` 至少保证有一个可用时钟，是一种「保底可用」的工程处理。不过在 FIR_x2 标准配置（`RAM_ADDR_WIDTH = 8`）下永远不会走到这个兜底分支。

---

## 5. 综合实践

本实践把三个最小模块串起来，用 `ADD_TB` 完整验证累加积分器的「逐拍累加 → 下降沿输出与复位」全过程。

**实践目标**：在 Questa 波形中，用一个完整 `LRCKx2` 周期把 `MULT_I → MULT_REG → ADD_REG（阶梯累加）→ ADDO_REG（下降沿一次性输出）` 的数据流走通，并验证「每周期累加 256 个乘积」。

**操作步骤**：

1. 进入 `06_ADD/Questa/`，运行 `ADD.bat`（脚本会编译 `ADD.v` 及其依赖的 `DPRAM_CONT`、`SDPRAM`、`DATA_BUFFER`、`SPROM_CONT`、`SPROM`、`FIR_COEF`、`MULT`、`ADD_TB.v`，见 [ADD.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/Questa/ADD.bat)）。
2. 在 `run.do` 自动添加的端口信号基础上，手动补齐内部寄存器，建议顺序：
   ```tcl
   add wave -divider {Accumulator}
   add wave sim:/ADD_TB/u_ADD/MCLK_I
   add wave sim:/ADD_TB/u_ADD/LRCKx2_I
   add wave sim:/ADD_TB/u_ADD/LRCKx2_REG
   add wave sim:/ADD_TB/u_ADD/MULT_I
   add wave sim:/ADD_TB/u_ADD/MULT_REG
   add wave -radix signed sim:/ADD_TB/u_ADD/ADD_REG
   add wave -radix signed sim:/ADD_TB/u_ADD/ADDO_REG
   add wave sim:/ADD_TB/u_ADD/ADD_O
   add wave sim:/ADD_TB/u_ADD/LRCKx2_O
   ```
   （`ADD_REG`/`ADDO_REG` 用 `-radix signed` 以有符号十进制显示，便于观察阶梯累加。）
3. `restart; run 20000` 后，放大到两相邻 `LRCKx2_I` 下降沿之间的区间。

**需要观察与验证的现象**：

| 检查项 | 预期 |
|--------|------|
| `MULT_REG` 与 `MULT_I` | `MULT_REG` 滞后 `MULT_I` 恰好 1 个 `MCLK` |
| `LRCKx2_REG` 与 `LRCKx2_I` | `LRCKx2_REG`（= `LRCKx2_O`）滞后 `LRCKx2_I` 恰好 1 个 `MCLK` |
| `ADD_REG` 在普通周期 | 每拍增加一个 `MULT_REG`，呈阶梯状 |
| 下降沿那一拍 | `ADDO_REG` 跳变为旧 `ADD_REG`；`ADD_REG` 跳变为当前 `MULT_REG`（种子） |
| 周期内 `MCLK` 拍数 | 相邻两个下降沿之间约 256 个 `MCLK`（对应 256 个乘积）|
| `ADD_O` 更新频率 | 每个 `LRCKx2` 周期只变一次（采样保持）|

**预期结果**：你能看到 `ADD_REG` 像楼梯一样逐拍累加，到达下降沿时把整段楼梯的「顶」送到 `ADDO_REG` 输出，随后楼梯从新的种子值重新开始爬升——这就是一个过采样样点卷积和的完整生成过程。若本地无 Questa 环境，标注「待本地验证」，可改为源码阅读型实践：对照 [ADD.v:77-90](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD.v#L77-L90) 手动推演上表每一行。

> 提示：若想看到「负数累加」（楼梯向下），可把测试输入换成 [Impulse_44100Hz_32bit.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/Questa/Impulse_44100Hz_32bit.txt)（在 [ADD_TB.v:139](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/06_ADD/ADD_TB.v#L139) 切换 `$fopen` 的文件名），冲激响应会让 `ADD_REG` 先正后负地变化。

## 6. 本讲小结

- `ADD` 用一根 `signed` 累加寄存器 `ADD_REG`，每拍执行 `ADD_REG <= ADD_REG + MULT_REG`，把 `MULT` 送来的 48 位乘积累加成卷积和；输入寄存器 `MULT_REG` 既改善时序又保持与节拍对齐。
- `LRCKx2` 下降沿由 `~LRCKx2_I & LRCKx2_REG == 1'b1` 检测（注意 `==` 优先级高于 `&`），那一拍把累加好的和锁存到 `ADDO_REG` 输出，并把 `ADD_REG` 重新装载为当前 `MULT_REG`（不清零，不丢拍），开始下一个样点。
- 一个 `LRCKx2` 周期 = 256 个 `MCLK`，恰好累加 256 个乘积，对应一个过采样样点的完整卷积和；`ADD_O` 每周期只更新一次，呈采样保持。
- `LRCKx2_REG` 一物两用：既是下降沿检测的「上一拍值」，又是延迟 1 拍的输出节拍 `LRCKx2_O`，保证 `ADD_O` 与 `LRCKx2_O` 同拍变化、与下游对齐。
- `NRST_I` 低有效时通过三元运算符把 `ADD_REG`/`ADDO_REG` 强制写 0；`BCKx2_O` 在 `RAM_ADDR_WIDTH < 7` 时兜底到 `MCLK_I`，标准配置（=8）下走延迟 1 拍的正常路径。
- `ADD` 的输出 `ADD_DATA` 与 `LRCKx2O_wire` 进入顶层最后一级流水线（[FIR_x2.v:168-172](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L168-L172)）做饱和与定点舍入。

## 7. 下一步学习建议

- 继续学习 [u5-l3（输出饱和与定点舍入）](./u5-l3-output-saturation-rounding.md)：看 `ADD_DATA` 的 48 位累加和如何被饱和判断与截位舍入成 32 位输出，这是 `ADD` 的直接下游。
- 若对「256 个乘积」如何保证不溢出感兴趣，可跳读 [u6-l1（FIR 系数生成）](./u6-l1-fir-coefficient-generation.md)，了解 `fir_gen.py` 中对奇偶抽头和的 `MAX_TOTAL` 溢出检查。
- 想验证时序不变式的读者，可结合 [u6-l3（PSL 断言与覆盖率）](./u6-l3-psl-assertions-coverage.md)，尝试为 `ADD` 的「下降沿那拍 `ADDO_REG` 必更新、`ADD_REG` 必等于 `MULT_REG`」设计一条 PSL 断言草案。
