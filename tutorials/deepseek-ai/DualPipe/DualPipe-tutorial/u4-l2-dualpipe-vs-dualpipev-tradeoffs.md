# 双向 vs V 型：气泡、显存与设备取舍

## 1. 本讲目标

学完本讲后，你应当能够：

- 读懂 README 中「Pipeline Bubbles and Memory Usage Comparison」这张对比表的四列含义，并指出 DualPipe 与 DualPipeV 在其中**几乎完全相同、唯独设备数不同**。
- 解释为什么在「相同 PP 阶段数」这一前提下，DualPipe 需要 \( PP \) 个设备，而 DualPipeV 只需要 \( PP/2 \) 个设备。
- 从源码层面验证这一差异：两端喂数据 vs 一端喂数据 + V 型转折、偶数设备约束、以及调度循环公式里「半量 vs 全量」的对应关系。
- 在气泡大小、激活显存、所需设备数三个维度上做工程取舍，并给出选型建议。

本讲是专家层「DualPipeV 变体与实战」单元的第二篇，承接 [u4-l1 DualPipeV 的 V 型调度](u4-l1-dualpipev-v-schedule.md)，把视角从「DualPipeV 内部怎么调度」抬升到「DualPipe 与 DualPipeV 放在一起该怎么选」。

## 2. 前置知识

本讲默认你已经掌握前置讲义建立的概念，这里只做最小回顾：

- **PP（pipeline stages）**：模型沿深度切成的阶段数，是衡量「模型有多深」的量。本讲里 PP 始终指阶段数（stage 数），不是设备数。
- **气泡（bubble）**：流水线在「灌水」与「排水」阶段的设备空闲时间。前置讲义已给出三种方法的气泡公式。
- **微批次（micro-batch / chunk）**：把一个 batch 切成多片依次灌入流水线，以提高利用率。
- **每设备持两个阶段**：DualPipe 与 DualPipeV 都让每个进程持有一对镜像模块（stage \( r \) 与 stage \( PP{-}1{-}r \)），代价是每设备参数量 \( 2\times \)。
- **F&B 重叠 / 零气泡（zero bubble）**：前反向重叠把一次前向与一次反向塞进同一格；零气泡把可延后的权重梯度 W 抠出填进气泡。两者在本讲的两种调度里完全相同。

若以上任一概念陌生，请先回到 [u1-l1](u1-l1-project-overview.md)、[u2-l1](u2-l1-bidirectional-pipeline-concepts.md)、[u2-l4](u2-l4-weightgradstore-zero-bubble.md) 与 [u4-l1](u4-l1-dualpipev-v-schedule.md) 补课。本讲不再重复它们的内部机制，只聚焦「对比与取舍」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `README.md` | 提供**权威对比表**（气泡 / 每设备参数 / 每设备激活 / 设备数）与符号定义。 |
| `dualpipe/dualpipe.py` | DualPipe 引擎：两端喂数据、偶数设备约束、调度循环用「半量」量（`num_half_ranks` / `half_rank` / `half_num_chunks`）。 |
| `dualpipe/dualpipev.py` | DualPipeV 引擎：一端喂数据、V 型转折（detach 衔接）、调度循环用「全量」量。 |
| `examples/example_dualpipe.py` | DualPipe 示例：模型深度 = 设备数；启动步长 −2、需偶数 GPU；两端各喂一半数据。 |
| `examples/example_dualpipev.py` | DualPipeV 示例：模型深度 = 2× 设备数；启动步长 −1、任意 GPU 数；只从首端喂数据。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 对比表精读**（建立「四列里只有设备数不同」的认知）与 **4.2 设备数与激活显存差异**（从源码挖出这个差异的根源）。

### 4.1 README 对比表精读：四列里藏着唯一的区别

#### 4.1.1 概念说明

README 有一张表，把 DualPipe、DualPipeV 与两种经典流水线算法（1F1B、ZB1P）放在**同一前提**下对比——「based on the same number of PP stages」，即**模型深度（阶段数 PP）相同**。

这张表有四列，回答四个独立问题：

1. **Bubble（气泡大小）**：流水线空转了多少。
2. **Parameter Per Device（每设备参数量）**：相对 1F1B 的 \( 1\times \)，每设备要装几倍参数。
3. **Activation Per Device（每设备激活显存）**：以「一个微批次在一个 stage 上的激活」为单位，每设备峰值要存几个。
4. **#Devices（所需设备数）**：跑这套调度至少要几张卡。

读懂这张表的关键，是先看 DualPipe 与 DualPipeV 这**两行**——你会发现前三列**逐字相同**，只有第四列 #Devices 不同。这就是本讲要解释的全部现象：**两种调度的算法代价几乎完全一致，差异只在于谁更省设备**。

#### 4.1.2 核心流程

先把四列的公式摆出来（PP 为阶段数，且为偶数）：

| 方法 | Bubble | 参数/设备 | 激活/设备 | 设备数 |
|------|--------|-----------|-----------|--------|
| 1F1B | \((PP{-}1)(F{+}B)\) | \(1\times\) | \(PP\) | \(PP\) |
| ZB1P | \((PP{-}1)(F{+}B{-}2W)\) | \(1\times\) | \(PP\) | \(PP\) |
| **DualPipe** | \((PP/2{-}1)(F\&B{+}B{-}3W)\) | \(2\times\) | \(PP{+}1\) | \(PP\) |
| **DualPipeV** | \((PP/2{-}1)(F\&B{+}B{-}3W)\) | \(2\times\) | \(PP{+}1\) | \(PP/2\) |

符号约定（README 原文）：

- \(F\)：一个前向 chunk 的执行时间。
- \(B\)：一个完整反向 chunk 的执行时间。
- \(W\)：一个「权重的反向（backward for weights）」chunk 的执行时间。
- \(F\&B\)：**互相重叠**的一次前向与一次反向的总执行时间，约等于 \(\max(F, B)\)，通常 \(B \geq F\)，故 \(F\&B \approx B\)。

把 DualPipe 与 DualPipeV 两行逐列对照：

- **气泡**：完全相同，都是 \((PP/2{-}1)(F\&B{+}B{-}3W)\)。系数 \(PP/2{-}1\) 来自双向灌水把有效灌满长度从 \(PP\) 压到 \(PP/2\)；\(-3W\) 来自零气泡把权重梯度抠出补位。
- **每设备参数**：都是 \(2\times\)，因为每个进程持有一对镜像 stage。
- **每设备激活**：都是 \(PP{+}1\)。
- **设备数**：DualPipe 要 \(PP\)，DualPipeV 只要 \(PP/2\)。← **唯一区别**。

直观地算个数值（仅作示意，设 \(F=1, B=2, W=1, F\&B\approx B=2\)，\(PP=8\)）：

\[
\begin{aligned}
\text{1F1B 气泡} &= 7\times(1+2)=21 \\
\text{ZB1P 气泡} &= 7\times(1+2-2)=7 \\
\text{DualPipe / DualPipeV 气泡} &= 3\times(2+2-3)=3
\end{aligned}
\]

可见 DualPipe/DualPipeV 的气泡（3）远小于 1F1B（21），也小于 ZB1P（7），而**两者彼此相等**。气泡不是二者的分水岭。

#### 4.1.3 源码精读

对比表与符号定义全部来自 README 的这一段：

- [README.md:24-36](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L24-L36) —— 这是「Pipeline Bubbles and Memory Usage Comparison」整节，含表头、四行方法、以及 \(PP/F/B/W/F\&B\) 的符号说明。注意 DualPipe 与 DualPipeV 两行的前三列逐字相同，只有 `#Devices` 列分别是 `PP` 与 `PP/2`。

DualPipeV 的定位说明也值得一读，它明确说 DualPipeV 是从 DualPipe「切半（cut-in-half）」得到的：

- [README.md:14-22](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L14-L22) —— DualPipeV 的介绍与调度图说明，明确它是 DualPipe 的「concise V-shape schedule」变体。

#### 4.1.4 代码实践

**实践目标**：亲手验证「DualPipe 与 DualPipeV 气泡公式相同、且都优于 1F1B/ZB1P」。

**操作步骤**：下面这段纯算术脚本不需要 GPU 或 PyTorch，任何 Python 环境都能跑（**示例代码**）：

```python
# 示例代码：对比四种流水线算法的气泡大小（纯算术，无需 GPU）
def bubble(method, PP, F=1, B=2, W=1, FB=2):
    if method == "1F1B":
        return (PP - 1) * (F + B)
    if method == "ZB1P":
        return (PP - 1) * (F + B - 2 * W)
    if method in ("DualPipe", "DualPipeV"):
        return (PP // 2 - 1) * (FB + B - 3 * W)
    raise ValueError(method)

PP = 8
for m in ("1F1B", "ZB1P", "DualPipe", "DualPipeV"):
    print(f"{m:12s} bubble = {bubble(m, PP)}")
```

**需要观察的现象**：DualPipe 与 DualPipeV 输出相同的数（都是 3），且都小于 1F1B、ZB1P。

**预期结果**：

```
1F1B         bubble = 21
ZB1P         bubble = 7
DualPipe     bubble = 3
DualPipeV    bubble = 3
```

> 注意：绝对数值依赖你对 \(F/B/W\) 的假设，但「DualPipe == DualPipeV < ZB1P < 1F1B」这个**相对结论与假设无关**，因为两者的公式系数完全一致。

#### 4.1.5 小练习与答案

**练习 1**：把 PP 从 8 改成 16，DualPipe 与 DualPipeV 的气泡分别是多少？它们还相等吗？

**答案**：\((16/2{-}1)(F\&B{+}B{-}3W) = 3(F\&B{+}B{-}3W)\)。用上面的数值假设即 \(3\times(2+2-3)=3\)。两者始终相等，因为公式完全相同。

**练习 2**：表里 DualPipe/DualPipeV 的「参数/设备」为什么是 \(2\times\) 而 1F1B 是 \(1\times\)？

**答案**：DualPipe/DualPipeV 让每个进程持有一对镜像 stage（stage \(r\) 与 stage \(PP{-}1{-}r\)），所以每设备装了两份参数；1F1B 每设备只装一个 stage，故 \(1\times\)。

**练习 3**：如果有人告诉你「DualPipe 气泡比 DualPipeV 小」，你能从表里判断对错吗？

**答案**：错。两行 Bubble 列逐字相同。气泡不是二者的区别维度。

### 4.2 设备数与激活显存差异：从源码挖出根因

#### 4.2.1 概念说明

既然气泡、参数、激活三列都相同，本模块就专注回答：**为什么 DualPipe 要 \(PP\) 个设备，而 DualPipeV 只要 \(PP/2\) 个？** 这要从它们「怎么喂数据」说起。

- **DualPipe（双向，两端喂）**：数据从流水线的**首末两端相向喂入**，形成两条独立的数据流——一条从首 rank 前向流到末 rank 算 loss，另一条从末 rank 反向流回首 rank 算 loss。两条流是**不同的微批次**，在中部交叉。因为两条流都横跨整个 \(PP\) 跨度、方向相反，所以**每一个设备都作为独立节点被两条流各自用一次**，无法折叠 → 需要 \(PP\) 个设备。

- **DualPipeV（V 型，一端喂 + 折返）**：数据**只从首 rank 喂入**，前向流到末 rank 后**就地折返**（把前向输出 detach 后当作反向输入），反向再流回首 rank 算 loss，呈 V 字形。因为前向与反向是**同一批数据**在折返点衔接，折返点（V 型顶点 = 末 rank）上的两个相邻 stage 共驻一台设备、无需通信 → 只需 \(PP/2\) 个设备。

一句话：**DualPipe 是两条独立流的交叉，DualPipeV 是一条流的折叠**。折叠省下了一半设备。

至于「激活显存」：两种调度每设备都持两个 stage，在流水线最繁忙时刻都要缓存约 \(PP{+}1\) 个微批次的激活，所以这一列也是相同的 \(PP{+}1\)。设备数差异**不**来自显存模型，而纯粹来自拓扑（两端喂 vs 一端折叠）。

#### 4.2.2 核心流程

先把 DualPipeV「为什么 \(PP/2\) 个设备就够了」的算账过程写清楚。设设备数 \(D = PP/2\)，设备 \(r\) 持有 stage \(r\)（前向用）与 stage \(PP{-}1{-}r\)（反向用）：

- **前向**流过设备 \(0 \to 1 \to \dots \to D{-}1\)，设备 \(r\) 计算 stage \(r\) → 覆盖 stage \(\{0, 1, \dots, D{-}1\} = \{0,\dots,PP/2{-}1\}\)。
- **折返点**在末设备 \(r=D{-}1\)：前向止于 stage \(D{-}1=PP/2{-}1\)，反向起于该设备的 stage \(PP{-}1{-}(D{-}1) = PP/2\)。两个相邻 stage \(PP/2{-}1\) 与 \(PP/2\) **共驻一台设备**，detach 衔接、零通信。
- **反向**流回设备 \(D{-}1 \to \dots \to 0\)，设备 \(r\) 计算 stage \(PP{-}1{-}r\) → 覆盖 stage \(\{PP/2, \dots, PP{-}1\}\)。
- 合起来 stage \(\{0,\dots,PP/2{-}1\} \cup \{PP/2,\dots,PP{-}1\} =\) 全部 \(PP\) 个 stage。✅

所以 \(D = PP/2\) 个设备就覆盖了深度 \(PP\) 的模型。DualPipe 没有这个折返点（两条独立流各自横跨 \(PP\) 跨度），只能用 \(PP\) 个设备。

这个差异在源码里体现为四组对照：

1. **模型深度构造**：DualPipe 示例按设备数建 \(PP\) 个 stage；DualPipeV 示例建 \(2\times\) 设备数个 stage。
2. **喂数据方式**：DualPipe 首末两端各喂一半；DualPipeV 只首端喂全部。
3. **设备数约束**：DualPipe 断言设备数（num_ranks）为偶数；DualPipeV 无此约束。
4. **调度循环公式**：DualPipe 用「半量」量（`half_rank`、`num_half_ranks`、`half_num_chunks`）；DualPipeV 用「全量」量（`rank`、`num_ranks`、`num_chunks`）。

#### 4.2.3 源码精读

**(1) 模型深度与设备数的 1:1 vs 1:2 关系**

DualPipe 示例：模型深度等于设备数（`pp_size` 既是设备数也是 stage 数）：

- [examples/example_dualpipe.py:128](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L128) —— `full_modules = nn.Sequential(*[PipelineStage(hidden_size) for _ in range(pp_size)])`，stage 数 = 设备数 = PP。

DualPipeV 示例：模型深度是设备数的两倍：

- [examples/example_dualpipev.py:127](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L127) —— `full_modules = nn.Sequential(*[PipelineStage(hidden_size) for _ in range(pp_size * 2)])`，stage 数 = 2× 设备数 = PP。**同样的设备数，DualPipeV 能训两倍深的模型**。

**(2) 启动时的设备数步长**

DualPipe 要求偶数 GPU，启动步长 −2：

- [examples/example_dualpipe.py:199-202](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L199-L202) —— `num_gpus = torch.cuda.device_count() // 2 * 2`（向下取偶），`for ngpus in range(num_gpus, 0, -2)`（步长 −2）。

DualPipeV 任意 GPU 数，启动步长 −1：

- [examples/example_dualpipev.py:180-183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L180-L183) —— `num_gpus = torch.cuda.device_count()`，`for ngpus in range(num_gpus, 0, -1)`（步长 −1）。

**(3) 引擎里的偶数约束**

DualPipe `step()` 显式断言设备数为偶数（双向对称所需）：

- [dualpipe/dualpipe.py:332-333](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L332-L333) —— `assert num_ranks % 2 == 0` 以及 `num_chunks % 2 == 0`。双向调度必须对折，故设备数与微批次数都得是偶数。

DualPipeV `step()` 没有偶数约束：

- [dualpipe/dualpipev.py:318](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L318) —— 只断言 `num_chunks >= num_ranks * 2`，对 `num_ranks` 无奇偶要求。

**(4) 两端喂 vs 一端喂**

DualPipe 从首末两端各喂一半输入、各自配对端的标签：

- [examples/example_dualpipe.py:145-153](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L145-L153) —— first rank 取 `full_x.chunk(2)[0]`、last rank 取 `full_x.chunk(2)[1]`，中间 rank 拿 `None`。两端都有输入。
- 引擎侧 [dualpipe/dualpipe.py:345-353](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L345-L353) —— 把输入 scatter 成 `half_num_chunks`（半量）后，分别塞进 first/last rank 的两个 phase 槽。

DualPipeV 只从首端喂全部输入：

- [examples/example_dualpipev.py:143-146](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L143-L146) —— 仅 first rank 保留 `x`，其余 rank 置 `None`。
- 引擎侧 [dualpipe/dualpipev.py:325-328](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L325-L328) —— 首端 scatter 成**全部** `num_chunks`（全量）。

**(5) V 型转折：detach 衔接（DualPipeV 独有）**

这是 DualPipeV 省 half 设备的核心机制——在末 rank 把前向产出就地折成反向输入，无需通信：

- [dualpipe/dualpipev.py:79-80](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L79-L80) —— `_forward_compute_chunk` 中，末 rank 的 phase 0 前向产出经 `output.detach().requires_grad_()` 折成 phase 1（反向）输入。这就是 V 型顶点的「上行→下行」衔接。
- [dualpipe/dualpipev.py:171-172](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L171-L172) —— 重叠版本 `_forward_backward_compute_chunk` 里同样的 detach 衔接。
- 反向侧的「梯度桥」[dualpipe/dualpipev.py:115-116](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L115-L116) 与 [dualpipe/dualpipev.py:182-183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L182-L183) —— 把反向算出的输入梯度桥回 phase 0 的输出梯度，使整张自动微分图等价于一张连通图。

对比 DualPipe 的 loss 计算，它在**两端**都可能算 loss（两条独立流各自在对端算 loss）：

- [dualpipe/dualpipe.py:75](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L75) —— `is_last_stage = (self.is_first_rank and phase == 1) or (self.is_last_rank and phase == 0)`，首末两端都是某条流的终点。

DualPipeV 只在**首端**算 loss（单向折返，只有一个终点）：

- [dualpipe/dualpipev.py:70](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L70) —— `is_last_stage = (self.is_first_rank and phase == 1)`，只有首端。

**(6) 调度循环：半量 vs 全量**

DualPipe 把所有循环量对折：

- [dualpipe/dualpipe.py:334-336](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L334-L336) —— 定义 `num_half_ranks`、`half_rank = min(rank, num_ranks-1-rank)`、`half_num_chunks`。`half_rank` 用 `min` 把前后半段 rank 对称到同一位置量，是双向调度能用一套循环写所有 rank 的关键。
- [dualpipe/dualpipe.py:358-359](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L358-L359) —— `step_1 = (num_half_ranks - half_rank - 1) * 2`。
- [dualpipe/dualpipe.py:381-382](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L381-L382) —— 主步 `step_4 = half_num_chunks - num_ranks + half_rank + 1`。

DualPipeV 用全量，因为只有一条流、无对折：

- [dualpipe/dualpipev.py:331](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L331) —— `step_1 = (num_ranks - rank - 1) * 2`（用 `num_ranks` 与 `rank`，非半量）。
- [dualpipe/dualpipev.py:353](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L353) —— 主步 `step_4 = num_chunks - num_ranks * 2 + rank + 1`。

两套公式形状一致，差别只在「半量 / 全量」与对应的特殊点守卫（DualPipe 是 `is_middle_rank`，DualPipeV 是 `is_last_rank`）：

- DualPipe 主步特判 [dualpipe/dualpipe.py:384-385](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L384-L385)：`if i == 0: if self.is_middle_rank: ...`（中部对折点）。
- DualPipeV 主步特判 [dualpipe/dualpipev.py:355-356](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L355-L356)：`if i == 0: if self.is_last_rank: ...`（V 型顶点）。

> 把「半量/全量」、特殊点（middle vs last）、喂数据方式（两端 vs 一端）三件事叠起来看，就能从源码层面确认：**DualPipe 的拓扑是对称双向（需 \(PP\) 设备），DualPipeV 的拓扑是单向折返（需 \(PP/2\) 设备）**。

#### 4.2.4 代码实践

**实践目标**：用一段脚本把「同模型深度下两种调度的资源需求」打印成表，直观看到 DualPipeV 省一半设备。

**操作步骤**：运行下面这段纯算术脚本（**示例代码**，无需 GPU/PyTorch）：

```python
# 示例代码：同模型深度（PP 阶段）下，DualPipe vs DualPipeV 的资源需求
def compare(pp_stages):
    return {
        "PP_stages(模型深度)": pp_stages,
        "DualPipe_设备数": pp_stages,            # 表里 #Devices = PP
        "DualPipeV_设备数": pp_stages // 2,       # 表里 #Devices = PP/2
        "每设备参数倍数": "2x (两者相同)",
        "每设备激活(微批次×stage)": pp_stages + 1,  # 两者相同 = PP+1
        "气泡系数(PP/2-1)": pp_stages // 2 - 1,    # 两者相同
    }

for pp in (4, 8, 16):
    print(compare(pp))
```

**需要观察的现象**：无论 PP 取多少，`DualPipe_设备数` 恒为 `DualPipeV_设备数` 的两倍；而参数倍数、激活、气泡系数两行完全相同。

**预期结果**（PP=8）：

```
DualPipe 需 8 设备，DualPipeV 需 4 设备；两者都 2x 参数、9 (=PP+1) 激活、气泡系数 3。
```

**进阶（源码阅读型实践）**：打开两个示例的 `__main__` 块，对照 [example_dualpipe.py:199-202](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L199-L202) 与 [example_dualpipev.py:180-183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L180-L183)，确认：在同一台机器（相同 `device_count()`）上，DualPipe 会用偶数张卡跑 `pp_size` 深度的模型，而 DualPipeV 用同样的卡数能跑 `2×pp_size` 深度的模型。这印证了「同设备数下 DualPipeV 训更深模型 / 同模型深度下 DualPipeV 用更少设备」。

#### 4.2.5 小练习与答案

**练习 1**：假设模型深度 PP=8。DualPipe 与 DualPipeV 各需多少设备？每设备参数与激活分别约为多少？

**答案**：DualPipe 需 8 个设备，DualPipeV 需 4 个。两者每设备参数都是 \(2\times\)，每设备激活都是 \(PP{+}1 = 9\)（个微批次×stage 单位）。

**练习 2**：DualPipe `step()` 为什么要断言 `num_ranks % 2 == 0`，而 DualPipeV 不需要？

**答案**：DualPipe 是双向对称调度，必须能对折（前后半段镜像），故设备数必须偶数；DualPipeV 是单向折返，没有对称对折需求，任意设备数都行。

**练习 3**：DualPipeV 的 detach 衔接发生在哪个 rank、起什么作用？

**答案**：发生在末 rank（`is_last_rank`）。它把前向（phase 0）的产出 `detach().requires_grad_()` 后当作反向（phase 1）的输入，使 V 型顶点的前向终点与反向起点共驻一台设备、无需 P2P 通信——这正是 DualPipeV 能省下一半设备的机制核心。

## 5. 综合实践

**任务**：撰写一段对比说明（可作为团队技术备忘），回答下面问题——

> 在 **8 个 PP 阶段、相同微批次**的前提下，DualPipe 与 DualPipeV 分别需要多少设备？每设备激活显存与参数约为多少倍？给出你的选型建议。

**写作要点（参考答案提纲）**：

1. **设备数**：DualPipe 需 8 台，DualPipeV 需 4 台（依据 README 表与示例 [example_dualpipe.py:128](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L128) / [example_dualpipev.py:127](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L127)）。
2. **激活显存**：两者相同，每设备约 \(PP{+}1 = 9\) 个微批次×stage 的激活（依据 README 表 Activation 列）。
3. **参数**：两者相同，每设备 \(2\times\)（因为每进程持两个镜像 stage）。
4. **气泡**：两者相同，\((PP/2{-}1)(F\&B{+}B{-}3W)\)。
5. **选型建议**：
   - 若**模型深度固定**、想省设备 → 选 **DualPipeV**（4 台即可，省一半）。
   - 若**设备数固定**、想训更深模型 → 选 **DualPipeV**（同样 8 台能训 16 阶段模型）。
   - 若确实需要**两端独立喂数据**（例如两条数据流天然从两端进入、或与既有双向拓扑对接），且设备数能匹配深度 1:1、且为偶数 → 选 **DualPipe**。
   - 注意 DualPipe 强制偶数设备数（[dualpipe.py:332](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L332)），DualPipeV 无此限制。

> 结论一句话：**在算法代价（气泡/参数/激活）完全相同的前提下，DualPipeV 在设备效率上严格优于 DualPipe；DualPipe 的价值在于「双向独立喂数据」这一拓扑特性，而非更省资源。**

## 6. 本讲小结

- README 对比表里，DualPipe 与 DualPipeV 的**气泡、每设备参数（\(2\times\)）、每设备激活（\(PP{+}1\)）三列完全相同**，唯一区别是 #Devices。
- DualPipe 需 \(PP\) 个设备，DualPipeV 只需 \(PP/2\) 个设备。
- 根因是拓扑：DualPipe 是**两条独立流的交叉**（两端喂数据，[example_dualpipe.py:145-153](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L145-L153)），每设备被两条流各用一次；DualPipeV 是**一条流的折叠**（一端喂数据 + 末 rank detach 折返，[dualpipev.py:79-80](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L79-L80)），折返点两 stage 共驻一台设备。
- DualPipe 强制偶数设备数（[dualpipe.py:332](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L332)），DualPipeV 任意设备数（[dualpipev.py:318](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L318)）。
- 调度循环公式形状一致，差别在「半量（`half_rank` 等）vs 全量（`rank` 等）」与特殊点守卫（middle vs last rank）。
- 选型：设备效率优先选 DualPipeV；需要双向独立喂数据才选 DualPipe。

## 7. 下一步学习建议

- **实战收尾**：进入 [u4-l3 实战：自定义流水线模块与正确性验证](u4-l3-custom-pipeline-module-lab.md)，亲手实现一个可被 DualPipe/DualPipeV 调度的 `PipelineStage`，并用 `ref_step` + `cal_diff < 1e-13` 做数值校验，把本讲的「选型」落到可运行代码上。
- **回顾调度细节**：若想再确认 DualPipe 主步 `is_middle_rank` 特判与 DualPipeV `is_last_rank` 特判为何都标注「don't overlap to further reduce bubble」，可回看 [u3-l5 八步调度](u3-l5-dualpipe-eight-step-schedule.md) 与 [u4-l1 DualPipeV V 型调度](u4-l1-dualpipev-v-schedule.md) 的主步讲解。
- **延伸阅读**：README 引用的 [DeepSeek-V3 技术报告](https://arxiv.org/pdf/2412.19437) 与 Sea AI Lab 的 [Cut-in-half 博文](https://hackmd.io/@ufotalent/r1lVXsa9Jg)，后者正是 DualPipeV 的来源，能帮助理解「切半」为何不改变气泡与显存、只改变设备数。
