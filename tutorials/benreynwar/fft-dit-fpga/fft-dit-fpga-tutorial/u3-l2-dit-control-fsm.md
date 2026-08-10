# dit 控制状态机：INIT / IDLE / CALC / SEND

## 1. 本讲目标

本讲专注于 `dit.v` 中那台**指挥整颗 FFT 引擎**的有限状态机（FSM）。学完后你应当能够：

- 读懂 FSM 的四个状态 `INIT` / `IDLE` / `CALC` / `SEND` 各自的职责与转移条件；
- 理解 FSM 如何通过 `tf_addr_nd`（向旋转因子模块要数据）与 `x_nd`（向蝶形模块喂数据）这两个一拍脉冲来驱动整条计算流水线；
- 说清“何时推进到下一级 stage”“何时切换工作缓存”“何时认定整个 FFT 结束”；
- 把 `case` 块里的位运算条件（如 `&(out1_addr)`、`updated0 & updated1`、`S==1`）翻译成“这一拍发生了什么”。

本讲**不**展开蝶形地址的位运算推导（那是 u3-l3 的主题），也**不**重复缓冲与 A/B 翻转的细节（那是 u3-l1 的主题），而是在前两者给出的“数据通路”之上，专门讲清楚“谁来按节拍驱动这条通路”。

## 2. 前置知识

在进入 FSM 之前，请先在脑中确立这几样东西（它们都来自前置讲义）：

- **蝶形（butterfly）是计算原子**：一次蝶形吃进两个复数 `XA`、`XB` 和一个旋转因子 `W`，吐出 `YA = XA + W·XB`、`YB = XA − W·XB`。蝶形内部是一条 4 级流水线，并且要求 `x_nd` 不能连续两拍为 1（见 u2-l3）。
- **旋转因子模块是查表 ROM**：给一个地址 `tf_addr` 并拉高一拍 `tf_addr_nd`，它下一拍就在 `tf_out` 上给出对应的旋转因子（见 u2-l1）。
- **DIT FFT 分 NLOG2 级**，每级有 `N/2` 个蝶形。`dit` 用 `bufferX` / `bufferY` 两块工作缓存做乒乓，每级读完一块、写满另一块；用 `updatedX` / `updatedY` 两张位图标记“哪些槽位已经写好、可以读”。
- **dit 内部有多个互不相连的 always 块**：本讲的 FSM 只是其中之一，它和“入口写进程”“出口读进程”“蝶形回写进程”并行运转，彼此靠共享的 `reg` / `wire` 通信。

如果上面任何一点你还不熟，建议先回看 u2-l3 与 u3-l1 再继续。

> 一句话定位：FSM 是“乐队指挥”。真正干乘法的是蝶形，真正存数据的是各块缓冲，但“这一拍该喂哪个蝶形、下一拍该要哪个旋转因子、哪一刻该换级”——全是这台 FSM 在拍板。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `dit.v` | FFT 顶层模块，含缓冲、FSM、蝶形回写、模块例化 | 第 183–188 行的状态定义、第 308–443 行的 FSM `always` 块、第 502–508 行的 `updated` 清零 |

FSM 不是孤立的一段代码，它驱动的寄存器散布在 `dit.v` 各处，本讲会随时跳过去引用。相关但**不**在本讲展开的：`butterfly.v`（蝶形流水线）、`generate_twiddlefactors.py`（旋转因子生成）。

## 4. 核心概念与源码讲解

### 4.1 状态机的四个状态与它驱动的寄存器

#### 4.1.1 概念说明

一台 FFT 引擎要按节拍完成三件事：① 把一帧输入攒齐；② 按 NLOG2 级、每级 N/2 个蝶形，逐个把数据喂进蝶形；③ 收尾，宣布结果就绪。`dit` 把这三件事拆成四个状态：

- **`INIT`**：每一帧（或每一轮完整 FFT）开始前的“复位台”。把地址归零、把“当前是第几级”的计数器拨到第一级、向旋转因子模块预请求第一个旋转因子。
- **`IDLE`**：等待输入缓冲被一帧数据写满。一旦写满，立刻把**第一个**蝶形的输入连同 `x_nd=1` 推进蝶形，进入计算循环。
- **`CALC`**：计算循环里的“决策拍”。它不喂蝶形，而是决定：本级的下一个蝶形地址推进一格，还是本级已经做完、该切换到下一级。同时预取下一个旋转因子。
- **`SEND`**：计算循环里的“投喂拍”。在确认要读的两个槽位都已写好（`updated0 & updated1`）后，把这一拍蝶形的输入推进去（`x_nd=1`），然后回到 `CALC` 继续；若是整个 FFT 的最后一个蝶形，则回到 `INIT` 收尾。

FSM 状态用一个 2 位寄存器 `fsm_state` 编码，四个状态用 `localparam` 命名（见 [dit.v:L183-L188](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L183-L188)，这里中文说明：用常量名代替裸数字 0/1/2/3，让 `case` 块可读）。

#### 4.1.2 核心流程

FSM 只负责“动嘴”，真正干活的是它驱动的一组寄存器。下面这张表把 FSM 名义上“拥有”的关键寄存器列清楚（注意：其中部分寄存器在别的 always 块里也会被读，但**写**主要由 FSM 负责）：

| 寄存器 | 含义 | 由谁置位 |
|--------|------|---------|
| `out0_addr` | 当前蝶形在本级的“偶输出”地址，遍历 0..N/2-1 | FSM |
| `S` | 当前级的“序列数”，每级右移 1 位：`N/2 → N/4 → … → 1` | FSM |
| `series_bits` | 从 `out0_addr` 中提取“序列内偏移 j”的掩码，随 `S` 同步右移 | FSM |
| `readbuf_switch` | 工作缓存乒乓方向（读 X 还是读 Y），每级翻转 | FSM |
| `tf_addr_nd` | 向旋转因子模块请求一个新旋转因子的一拍脉冲 | FSM |
| `x_nd` | 向蝶形模块宣告“本拍有新输入”的一拍脉冲 | FSM |
| `finished` | “本拍送出的是整个 FFT 的最后一个蝶形”标记 | FSM |
| `bufferin_read_switch` / `bufferin_full0_B` / `bufferin_full1_B` | 输入双缓存的读侧 A/B 翻转（A 由入口写进程翻，B 由 FSM 翻） | FSM |

整体节拍可以概括为一句话：**`INIT` 开局 → `IDLE` 等满 → `CALC`↔`SEND` 交替跑完所有蝶形 → 最后一个蝶形后回 `INIT`**。

#### 4.1.3 源码精读

状态定义与 `fsm_state` 寄存器：

[dit.v:L183-L188](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L183-L188) —— 中文说明：`fsm_state` 是 2 位 reg，四个 `localparam` 给状态编号，便于在 `case` 中用名字引用。

`out0_addr` / `S` / `series_bits` 这三个“地址发生器”寄存器的声明：

[dit.v:L236-L239](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L236-L239) —— 中文说明：这三个寄存器随 `stage` 演化，FSM 在 `INIT` 给初值、在 `CALC` 推进，是后续地址 wire（`in0_addr`/`in1_addr`/`out1_addr`/`tf_addr`）的源头。

`first_stage` 与 `last_stage` 两个判定 wire，FSM 在多处用到：

[dit.v:L273-L277](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L273-L277) —— 中文说明：`first_stage` 为真当且仅当 `S==N/2`（第一级），`last_stage` 为真当且仅当 `S==1`（最后一级）。这两个判定决定了“该不该释放输入缓冲”和“该不该收尾”。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立“FSM 名下到底管着哪些寄存器”的清晰清单。
2. **操作步骤**：打开 `dit.v`，从第 183 行起到 FSM `always` 块结束（第 443 行），把所有出现在“`<=` 左值”里的寄存器名抄一遍。
3. **需要观察的现象**：你会发现左值集合大致等于 §4.1.2 那张表；但有一个例外——`updatedX` / `updatedY` 的清零**不在** FSM 块里（被注释掉了，见 [dit.v:L397-L402](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L397-L402)）。
4. **预期结果**：`updatedX` / `updatedY` 的清零被搬到了第 502–508 行的“蝶形回写进程”里（见 [dit.v:L502-L508](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L502-L508)，中文说明：为了让 `updatedX/Y` 只被一个进程驱动，避免 Verilog 的“多驱动冲突”，FSM 只负责“告诉对方该清了”，真正动手清的是回写进程）。
5. 这一步无需运行仿真，纯静态阅读即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fsm_state` 用 2 位就够？  
**参考答案**：因为只有 4 个状态（0/1/2/3），2 位恰好编码 \(2^2 = 4\) 种取值，再多一位就是浪费。

**练习 2**：`S` 的取值序列是什么？为什么级数正好是 `NLOG2`？  
**参考答案**：`S` 从 `N/2` 开始，每级右移一位，序列为 \(N/2, N/4, \dots, 2, 1\)。从 \(N/2\) 一路除以 2 到 1，需要 \(\log_2(N) = \text{NLOG2}\) 步，所以正好对应 `NLOG2` 级。

---

### 4.2 四状态转移逻辑精读

#### 4.2.1 概念说明

把 §4.1 的状态职责“落”到代码上，就是 FSM `always` 块里那个大 `case (fsm_state)`。这一节我们逐状态读，把每一条转移的**触发条件**和**副作用**列出来。读 FSM 的关键技巧是：**只看“本拍满足什么条件 → 下一拍进入哪个状态 → 顺手改了哪些寄存器”**，不要被地址位运算分心（那是 u3-l3）。

#### 4.2.2 核心流程

把 `case` 块压缩成一张状态转移表（这是本讲的核心交付物）：

| 当前状态 | 触发条件 | 下一状态 | 本拍关键动作 |
|----------|----------|----------|-------------|
| `INIT` | 无条件（每帧开局） | `IDLE` | `out0_addr←0`；`S←N/2`；`series_bits` 初始化；`tf_addr_nd←1`（预取第 1 个旋转因子）；`finished←0` |
| `IDLE` | `bufferin_read_full == 1` | `CALC` | `x_nd←1`（把**第一个**蝶形的输入推进蝶形） |
| `IDLE` | `bufferin_read_full == 0` | `IDLE` | 等待输入缓冲被写满 |
| `CALC` | `&(out1_addr) == 1`（本级最后一个蝶形刚做完） | `SEND` | **推进下一级**：`series_bits>>1`、`S>>1`、`out0_addr←0`、`readbuf_switch` 翻转；若 `first_stage` 则释放输入缓冲 |
| `CALC` | `&(out1_addr) == 0` | `SEND` | **本级下一个蝶形**：`out0_addr ← out0_addr + 1` |
| `SEND` | `updated0 & updated1` 且 `&(out1_addr) & (S==1)` | `INIT` | `x_nd←1`；`finished←1`（整个 FFT 的最后一个蝶形已送出） |
| `SEND` | `updated0 & updated1` 且非最后蝶形 | `CALC` | `x_nd←1`（送出本拍蝶形输入） |
| `SEND` | `updated0 & updated1 == 0` | `SEND` | 原地等待，直到要读的两个槽位都写好 |

注意 `CALC` 无论走哪条分支，**下一状态都是 `SEND`**，并且都会 `tf_addr_nd←1`、`x_nd←0`。也就是说 `CALC` 永远是“决策 + 预取旋转因子”，紧接着的 `SEND` 才是“投喂蝶形”。`CALC↔SEND` 构成计算主循环，每个蝶形占一对 `CALC+SEND`。

#### 4.2.3 源码精读

整个 FSM `always` 块（含复位）：

[dit.v:L308-L443](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L308-L443) —— 中文说明：复位时回到 `INIT` 并清零所有握手脉冲；否则按 `case (fsm_state)` 分派。

**`INIT` 分支**（开局台）：

[dit.v:L326-L343](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L326-L343) —— 中文说明：把地址与级计数器复位到第一级，并拉高一拍 `tf_addr_nd` 预先索取第一个旋转因子，然后进入 `IDLE` 等数据。注意 `series_bits <= {NLOG2{1'b1}} >> 1` 即“低 NLOG2-1 位全 1”，正是第一级用来提取偏移 `j` 的掩码。

**`IDLE` 分支**（等满 + 发第一个蝶形）：

[dit.v:L344-L360](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L344-L360) —— 中文说明：`bufferin_read_full` 是输入双缓存“读侧那块已满”的指示（见 u3-l1）。一旦为真，立刻 `x_nd←1` 把第一个蝶形推进去，状态跳到 `CALC`。注释里那句“During the first step in this state the twiddle factor module will update”点出了关键时序：`INIT` 里请求的旋转因子，恰好在 `IDLE` 这一拍由旋转因子模块更新到位。

**`CALC` 分支**（决策拍）：

[dit.v:L361-L411](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L361-L411) —— 中文说明：恒定 `tf_addr_nd←1`、`x_nd←0`、`fsm_state←SEND`。然后用 `if (&(out1_addr))` 二选一——本级结束就推进下一级（`S>>1`、`series_bits>>1`、`out0_addr←0`、`readbuf_switch` 翻转，且 `first_stage` 时翻输入缓冲的 B 标志、切 `bufferin_read_switch`），否则只把 `out0_addr` 加 1。

**`SEND` 分支**（投喂拍 + 收尾判定）：

[dit.v:L412-L436](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L412-L436) —— 中文说明：先 `tf_addr_nd←0`；只有当 `updated0 & updated1`（要读的两个槽位都已写好）才 `x_nd←1` 并离开本状态。若同时满足 `&(out1_addr) & (S==1)`，说明这是整个 FFT 最后一个蝶形，于是回 `INIT` 并置 `finished←1`；否则回 `CALC` 继续。`updated0 & updated1` 不满足时，原地停留（注释“Waiting for data to be written.”）。

> 关于“本级结束”判定的小数学：`out1_addr = {1'b1, out0_addr[NLOG2-2:0]}`（[dit.v:L253](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L253)），它的最高位恒为 1，因此缩位与 `&(out1_addr)` 为真，等价于 \(out0\_addr\) 的低 \( \text{NLOG2}-1 \) 位全 1，即 \( out0\_addr = N/2 - 1 \)——恰好是本级最后一个蝶形。所以 `&(out1_addr)` 就是“本级末”的统一判据，与 `S` 无关。

#### 4.2.4 代码实践（源码阅读型——本讲主任务）

1. **实践目标**：把 §4.2.2 的状态转移表**亲手**从源码核出来，并画成状态转移图。
2. **操作步骤**：
   - 准备纸笔或任意画图工具。
   - 画 4 个节点：`INIT` / `IDLE` / `CALC` / `SEND`。
   - 对照 [dit.v:L326-L436](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L326-L436)，给每条边标上触发条件，重点标注这三处：
     - `IDLE → CALC`：`bufferin_read_full`
     - `CALC` 内的“本级末”分支：`&(out1_addr)`
     - `SEND → INIT`：`updated0 & updated1` & `&(out1_addr)` & `(S==1)`
   - 在 `SEND` 节点上画一个自环，标注 `~(updated0 & updated1)`（原地等待）。
3. **需要观察的现象**：`CALC → SEND` 有两条边（本级末 / 非本级末），但都指向 `SEND`；`SEND` 有三条出边（回 `INIT`、回 `CALC`、自环）。
4. **预期结果**：得到一张“`INIT → IDLE → (CALC ↔ SEND)* → INIT`”的图，其中 `CALC↔SEND` 是密集来回的主循环，`INIT` 是每帧的复位入口。把这张图与 §4.2.2 的表格互相印证，确保每条边都能在源码里找到对应行。
5. 无法确定画图细节时，标注“待本地验证”，但表格内容来自源码、可直接核对。

#### 4.2.5 小练习与答案

**练习 1**：`CALC` 状态无论走哪条分支，下一状态都是 `SEND`，这是巧合还是设计？  
**参考答案**：是设计。`CALC` 的职责是“决策 + 预取旋转因子”，它不直接喂蝶形（`x_nd←0`）；喂蝶形的动作统一放在 `SEND` 里。所以 `CALC` 之后必须进 `SEND`，保证“先取好旋转因子、再喂数据”的固定节拍。

**练习 2**：`SEND` 状态的自环（原地等待）什么时候会真正发生？第一级会发生吗？  
**参考答案**：当 `updated0 & updated1` 为 0 时发生，即要读的两个工作缓存槽位还没被蝶形回写进程写好。**第一级不会发生**——因为在 `first_stage` 时 `updated0`/`updated1` 被强制为 1（见 [dit.v:L287-L288](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L287-L288)），数据直接来自已满的输入缓冲。自环只可能出现在第二级及以后，等前一级的蝶形结果陆续写满工作缓存。

---

### 4.3 tf_addr_nd 与 x_nd：如何驱动蝶形与旋转因子

#### 4.3.1 概念说明

`tf_addr_nd` 和 `x_nd` 是 FSM 伸向外的“两只手”：

- `tf_addr_nd`：拍一下旋转因子模块的肩膀——“按当前 `tf_addr` 给我换个新旋转因子”。它是**一拍脉冲**，且 `tf_addr` 是 wire（[dit.v:L261](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L261)），随 `out0_addr` 实时变化。
- `x_nd`：拍一下蝶形模块的肩膀——“本拍 `xa`/`xb`/`w` 上是有效新输入，开算”。它也是**一拍脉冲**。

回忆 u2-l3：蝶形要求 `x_nd` 不能连续两拍为 1（否则四级流水线的乘法器复用会撞车）。FSM 的时序恰好满足这个约束——下文会看到 `x_nd` 天然隔拍出现。

#### 4.3.2 核心流程

把两个脉冲在四个状态里的取值列出来：

| 状态 | `tf_addr_nd`（本拍结束后） | `x_nd`（本拍结束后） |
|------|---------------------------|----------------------|
| `INIT` | `1`（预取第 1 个旋转因子） | `0` |
| `IDLE` | `0` | `1`（发第 1 个蝶形，当输入已满时） |
| `CALC` | `1`（预取下一个旋转因子） | `0` |
| `SEND` | `0` | `1`（发当前蝶形，当数据就绪时） |

观察规律：**`tf_addr_nd` 与 `x_nd` 永远不同时为 1**，二者严格交替。这带来两个直接好处：

1. 旋转因子模块和蝶形模块各自独占自己的“被唤醒拍”，互不抢资源。
2. 旋转因子总是**提前一拍**被请求，等下一拍 `x_nd` 喂数据时，`tf_out` 早已稳定——蝶形拿到的是“刚出炉且已就绪”的旋转因子。

时序骨架（以首拍为例，`Cn` 表示第 n 个时钟）：

```
C0 INIT   : tf_addr_nd<=1            （请求旋转因子 #0）
C1 IDLE   : tf_addr_nd<=0, x_nd<=1   （旋转因子 #0 到位；同时把第 1 个蝶形喂进去）
C2 CALC   : x_nd 仍=1（蝶形开算 #0）, tf_addr_nd<=1, x_nd<=0（请求旋转因子 #1）
C3 SEND   : x_nd<=1（喂蝶形 #1）
C4 CALC   : 蝶形开算 #1 ...
```

可见每个蝶形消耗“一个 `CALC` + 一个 `SEND`”，共两拍；旋转因子的请求始终比对应蝶形的喂数据早一拍。

#### 4.3.3 源码精读

两个脉冲的声明：

[dit.v:L291-L293](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L291-L293) —— 中文说明：`tf_addr_nd`、`x_nd` 都是 reg，由 FSM 唯一驱动，分别接到旋转因子模块与蝶形模块。

`INIT` 里对 `tf_addr_nd` 的预置（已经在前节引用的 [dit.v:L326-L343](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L326-L343) 中：`tf_addr_nd <= 1'b1; x_nd <= 1'b0;`）——确保进 `IDLE` 前旋转因子已被请求。

`IDLE` 里对 `x_nd` 的首次置位（[dit.v:L354-L359](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L354-L359)）：`tf_addr_nd <= 1'b0;` 之后 `if (bufferin_read_full) ... x_nd <= 1'b1;`——本拍撤销旋转因子请求、置数据有效。

`CALC` 里恒定 `tf_addr_nd <= 1'b1; x_nd <= 1'b0;`（[dit.v:L367-L369](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L367-L369)）。

`SEND` 里对 `x_nd` 的条件置位（[dit.v:L418-L420](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L418-L420)）：`if (updated0 & updated1) x_nd <= 1'b1;`——只有数据就绪才喂数据。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证“`tf_addr_nd` 比 `x_nd` 永远早一拍、且二者不重叠”。
2. **操作步骤**：仿照 §4.3.2 的时序骨架，从 `INIT` 开始手推前 6 拍（`C0`–`C5`），逐拍写下 `fsm_state`、`out0_addr`、`tf_addr_nd`、`x_nd` 的取值（假设输入缓冲始终已满、`updated` 始终就绪）。
3. **需要观察的现象**：`x_nd` 的 1 出现在 `C1`、`C3`、`C5`……（隔拍），`tf_addr_nd` 的 1 出现在 `C0`、`C2`、`C4`……，两序列错开。
4. **预期结果**：得到一张两拍一蝶形的节拍表，能直观看到“请求旋转因子 → 下一拍喂数据”的固定相位关系。这正是蝶形 `x_nd` 不能连续为 1 这一约束被天然满足的原因。
5. 若你对某拍取值不确定，标“待本地验证”，并用 iverilog/MyHDL 协同仿真（见 u4-l1）实测确认。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `CALC` 里的 `tf_addr_nd <= 1'b1` 改成 `1'b0`，会发生什么？  
**参考答案**：旋转因子模块不再被请求新值，`tf_out` 会停留在上一个值。于是从第二个蝶形起，蝶形用的旋转因子是错的（始终是第 0 个），FFT 结果完全错误。这说明 `CALC` 里的这一拍请求是“每个蝶形都要换旋转因子”的必要条件。

**练习 2**：为什么 `x_nd` 在 `IDLE` 和 `SEND` 里置 1，而在 `CALC` 里恒为 0？  
**参考答案**：因为喂数据这件事只发生在“要进入/继续计算循环”的时刻——首拍在 `IDLE`，其后每个蝶形在 `SEND`。`CALC` 是决策拍，不该喂数据，否则 `x_nd` 会连续两拍为 1，违反蝶形流水线的乘法器复用约束。

---

### 4.4 stage 推进、缓存切换与 finished 收尾

#### 4.4.1 概念说明

`CALC↔SEND` 主循环里，绝大多数节拍只是把 `out0_addr` 加 1（在本级内前进）。但每隔 `N/2` 个蝶形会出现一次“本级末”（`&(out1_addr)` 为真），此时 `CALC` 要做三件大事：

1. **推进 stage**：`S` 右移一位、`series_bits` 右移一位、`out0_addr` 归零——为下一级的 N/2 个蝶形重置地址发生器。
2. **切换工作缓存**：`readbuf_switch` 翻转，下一级改成“读另一块、写这一块”的乒乓方向。
3. **第一级特殊处理**：若刚结束的是第一级（`first_stage`），输入缓冲的使命已完成，翻它的 B 满 flag、切 `bufferin_read_switch`，把输入双缓存“释放”给入口进程去装下一帧——这正是 u3-l1 讲的“输入与计算并行”的实现开关。

而当本拍是**整个 FFT 的最后一个蝶形**时（`&(out1_addr) & (S==1)`，即最后一级的本级末），`SEND` 不回 `CALC`，而是回 `INIT` 并置 `finished←1`。`finished` 不会直接通知外界，而是搭着蝶形的旁路通道（`m_in`）穿过 4 级流水线，从 `finished_z` 出来，再被延迟两拍后用来翻转 `bufferout_full_A`——宣布“输出缓冲已满，一帧 FFT 结果可供读出”。

#### 4.4.2 核心流程

stage 推进的判定树（位于 `CALC` 内）：

```
if (&(out1_addr)):           # 本级最后一个蝶形刚做完
    if (first_stage):        # 刚做完的是第一级
        释放输入缓冲（翻 B 满 flag，切 bufferin_read_switch）
    series_bits >> 1          # 下一级的偏移掩码
    S >> 1                    # 下一级的序列数
    out0_addr <- 0            # 下一级从 0 号蝶形开始
    readbuf_switch ~          # 工作缓存乒乓换向
else:
    out0_addr <- out0_addr+1  # 本级继续前进
```

收尾的判定（位于 `SEND` 内）：

```
if (updated0 & updated1):
    x_nd <- 1
    if (&(out1_addr) & (S==1)):   # 最后一级的本级末
        fsm_state <- INIT
        finished  <- 1
    else:
        fsm_state <- CALC
```

`finished` 的传播链（跨进程）：

```
FSM 置 finished=1
  -> 拼入 m_in，进入蝶形 4 级流水
  -> 从蝶形输出端 finished_z 出来
  -> 回写进程里延迟两拍 finished_z_old[0] -> [1]
  -> finished_z_old[1]=1 时翻 bufferout_full_A  （输出缓冲“满”）
```

#### 4.4.3 源码精读

stage 推进与第一级释放输入缓冲（`CALC` 内的“本级末”分支）：

[dit.v:L370-L403](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L370-L403) —— 中文说明：`&(out1_addr)` 命中时，先判断 `first_stage` 决定是否释放输入双缓存（翻 `bufferin_full1_B`/`bufferin_full0_B`、切 `bufferin_read_switch`），再把 `series_bits`、`S` 各右移一位、`out0_addr` 归零、`readbuf_switch` 翻转。被注释掉的 `updatedX/Y` 清零已搬到回写进程。

收尾判定（`SEND` 内）：

[dit.v:L423-L428](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L423-L428) —— 中文说明：`&(out1_addr) & (S==1)` 同时成立，意味着最后一级的最后一个蝶形——回 `INIT`、`finished←1`。

`finished` 寄存器声明与含义：

[dit.v:L263](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L263) —— 中文说明：注释“Set to 1 when x_nd is set to 1 from the last BF calculation of the FFT.”即只在最后一个蝶形喂数据的那一拍置 1。

`finished` 被拼进蝶形的旁路元数据 `m_in`（连同路由信息一起穿过流水线）：

[dit.v:L562](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L562) —— 中文说明：`m_in = {readbuf_switch_old, out0_addr, out1_addr, finished, last_stage}`，`finished` 只是搭便车的 1 位标记，与地址等路由信息一同延迟 3 拍到达输出侧。

输出侧对 `finished_z` 的延迟与“宣布输出满”：

[dit.v:L482-L520](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L482-L520) —— 中文说明：`finished_z` 经 `finished_z_old[0]`、`finished_z_old[1]` 两级延迟，当 `finished_z_old[1]` 为 1 时翻转 `bufferout_full_A`，标记输出缓冲已被一帧完整结果写满。这段延迟是为了对齐“最后一个蝶形结果真正落进 `bufferout`”的时刻。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：把“stage 推进 → 工作缓存换向 → 收尾”这条链全程跟一遍。
2. **操作步骤**：取 `N=8`（即 `NLOG2=3`，`S` 序列为 4→2→1，共 3 级，每级 4 个蝶形）。
   - 在纸上列出每一级 `S`、`series_bits`（二进制）、`readbuf_switch` 的取值。
   - 标出三次“本级末”发生的节拍，以及三次里哪一次会触发 `first_stage` 释放输入缓冲（答：第一次）。
   - 标出整个 FFT 唯一一次 `&(out1_addr) & (S==1)` 命中的节拍（答：`S=1` 那级的第 4 个蝶形在 `SEND` 中）。
3. **需要观察的现象**：`readbuf_switch` 每级翻转一次，所以 3 级里它依次为 0→1→0（或反之），工作缓存的读/写角色来回交换；第一级读 `bufferin`，最后一级写 `bufferout`（由 `last_stage` 标记经 `m_in` 传到输出侧决定，见 [dit.v:L276-L277](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L276-L277) 与 [dit.v:L524-L527](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L524-L527)）。
4. **预期结果**：得到一张 `N=8` 的 3 级、12 个蝶形的“级—S—缓存方向—是否释放输入—是否收尾”全程表，能清楚指认 `finished=1` 发生在最后一个蝶形。
5. 若你对某级 `series_bits` 取值不确定，标“待本地验证”，但级数与蝶形数可由 \( \text{NLOG2} \) 与 \( N/2 \) 直接算出。

#### 4.4.5 小练习与答案

**练习 1**：为什么释放输入缓冲（翻 B 满 flag）只在 `first_stage` 为真时做，而不是每级都做？  
**参考答案**：因为输入双缓存只在**第一级**被读取——第一级之后数据已经搬进工作缓存 `bufferX`/`bufferY`，输入缓冲与计算再无关系。所以在第一级结束时释放它，恰好让入口进程能开始装下一帧，实现“边算当前帧、边收下一帧”的并行；后续级重复释放反而会破坏还未被读走的输入数据。

**练习 2**：`finished=1` 是在 `SEND` 里置的，但 `bufferout_full_A` 的翻转却在另一个 always 块里。为什么要绕这么远？  
**参考答案**：两个原因。其一，`finished` 要穿过蝶形的 4 级流水线，等最后一个蝶形的**结果**真正写进 `bufferout` 时，`finished` 才应生效——所以要先经 `finished_z` → `finished_z_old[0]` → `finished_z_old[1]` 对齐延迟。其二，`bufferout_full_A` 由“蝶形回写进程”驱动，与 FSM 是不同进程；把翻转放在回写进程里，保证一个 reg 只被一个进程写，避免 Verilog 多驱动冲突（与 u3-l1 的 A/B 拆分同理）。

---

## 5. 综合实践

把本讲四节串起来，完成一次“FSM 全程推演 + 状态转移图绘制”的综合任务。

**任务**：选定 `N=8`（`NLOG2=3`），在不运行仿真的前提下，逐拍推演一次完整 FFT 中 FSM 的行为，产出三件成果。

1. **状态转移图**：依据 [dit.v:L325-L441](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L325-L441) 的 `case` 块画出 4 节点转移图，每条边标注触发条件（`bufferin_read_full`、`&(out1_addr)`、`updated0 & updated1`、`&(out1_addr) & (S==1)`、`~(updated0 & updated1)` 自环）。
2. **节拍表**：从 `INIT` 起算，逐拍记录 `fsm_state` / `out0_addr` / `S` / `tf_addr_nd` / `x_nd`，直到出现一次 `finished←1` 并回到 `INIT`。预期共约 \( \underbrace{1}_{INIT} + \underbrace{1}_{IDLE} + \underbrace{2 \times (3 \times 4)}_{CALC+SEND, 3 级 × 每级 4 蝶形} = 26 \) 拍左右（精确拍数取决于 `SEND` 是否发生自环等待，可标“待本地验证”）。
3. **级—缓存对照表**：列出 3 级各自读哪块、写哪块（`bufferin` / `bufferX` / `bufferY` / `bufferout`），并标出“释放输入缓冲”发生在第一级末、“写 `bufferout`”发生在最后一级（由 `last_stage` 标记经旁路通道传达）。

**验收标准**：三件成果能互相印证——节拍表里出现的每一次状态跳转都能在转移图里找到对应的边；节拍表里 `tf_addr_nd` 与 `x_nd` 的 1 严格交替；级—缓存表里第一级读 `bufferin`、最后一级写 `bufferout` 与源码 [dit.v:L281-L282](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L281-L282) 一致。完成后建议用 u4-l1 的 MyHDL 协同仿真跑一次 `N=8`，对照 DEBUG 日志（`FSM_ST_*` 与 `-------NEXT STAGE---------`，见 [dit.v:L373](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L373)）核对你的推演。

## 6. 本讲小结

- `dit` 的控制核心是一台 4 状态 FSM：`INIT` 开局、`IDLE` 等输入满、`CALC↔SEND` 交替跑完所有蝶形、最后一个蝶形后回 `INIT` 收尾。
- `CALC` 是“决策 + 预取旋转因子”拍（恒置 `tf_addr_nd=1`、`x_nd=0`），`SEND` 是“投喂蝶形”拍（条件置 `x_nd=1`）；二者构成每个蝶形占两拍的主循环。
- `tf_addr_nd` 与 `x_nd` 严格交替、永不重叠，旋转因子始终比喂数据早一拍到位——这既满足蝶形流水线对 `x_nd` 不能连续为 1 的约束，也保证旋转因子就绪后才被消费。
- `&(out1_addr)` 是“本级最后一个蝶形”的统一判据（等价于 \( out0\_addr = N/2-1 \)）；命中时 `CALC` 推进下一级（`S>>1`、`series_bits>>1`、`out0_addr←0`、`readbuf_switch` 翻转），并在第一级末释放输入双缓存。
- `updated0 & updated1` 是 `SEND` 的数据就绪握手；第一级因 `updated` 被强制为 1 永不等待，第二级起才可能在此自环，等前一级蝶形结果写满工作缓存。
- `finished` 是“最后一个蝶形”标记，搭蝶形旁路通道穿过 4 级流水、再延迟两拍，最终翻转 `bufferout_full_A` 宣布一帧结果就绪——跨进程传递，避免多驱动冲突。

## 7. 下一步学习建议

- **接着学 u3-l3（地址计算）**：本讲刻意回避了 `out0_addr` 如何映射到 `in0_addr`/`in1_addr`/`out1_addr`/`tf_addr` 的位运算。学完 u3-l3，你就能把 FSM 里每一次 `out0_addr` 推进“翻译”成具体的蝶形读/写物理地址。
- **回头看 u3-l1（缓冲与 A/B）**：本讲的“释放输入缓冲”“工作缓存换向”“`updated` 清零”都依赖 u3-l1 的双缓冲机制，对照阅读能加深对“FSM 为何如此拍板”的理解。
- **向前到 u4-l1（协同仿真）**：想实时看到 `FSM_ST_INIT/IDLE/CALC/SEND` 与 `-------NEXT STAGE---------` 的 DEBUG 输出，需要打开 `DEBUGMODE` 并用 MyHDL + iverilog 协同仿真，这正是 u4-l1 的内容。
