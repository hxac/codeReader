# Half Buffer 与延迟握手

## 1. 本讲目标

本讲是「Skid Buffer 与 FSM 实现方法」单元的第二篇，承接 u10-l1 的 Skid Buffer 与 u9-l1 的 ready/valid 握手接口。学完本讲，你应当能够：

- 说清什么是**延迟握手（delayed handshake）**，以及它为何在等时（isochronous）流水线里把吞吐砍半。
- 读懂 `Pipeline_Half_Buffer` 的全部源码：一个数据寄存器 + 一个满/空位如何用 `Register` 搭出来，组合控制块如何在没有输入到输出组合路径的前提下驱动两侧握手。
- 把 Half Buffer 当作**迭代计算的控制器**：配合 `Pulse_Generator`，用「输出 valid 的上升沿启动计算、计算完了再脉冲 ready」来自然产生「可以处理下一项」的信号。
- 在 Half Buffer 与 Skid Buffer 之间做出正确选型：只差一个寄存器，吞吐却差一倍，各自适合什么场景。

## 2. 前置知识

本讲默认你已经掌握下列概念（均在前置讲义中建立）：

- **ready/valid 握手接口**（u9-l1）：source 驱动 `valid`/`data` 指向 destination，destination 驱动 `ready` 指回 source；同拍 `valid && ready` 即握手完成。接口内不得有组合路径（source 禁 `ready→valid`，destination 禁 `valid→ready`），否则对接即成环。
- **handshake_complete 门控**（u9-l2）：凡影响接口的内部状态，只能在与握手完成同拍改变。
- **Skid Buffer 与数据/控制分离**（u10-l1）：为了消掉握手接口上的组合路径，给数据通路加一个「滑行刹车」用的缓冲寄存器；COTTC FSM 把 EMPTY/BUSY/FULL 三态与 load/flow/fill/flush/unload 等变换系统化。
- **Register 家族与「最后赋值胜出」**（u3-l2、u6-l1）：`Register` 端口为 `clock`/`clock_enable`/`clear`/`data_in`/`data_out`；时钟块内 `clear` 的优先级高于 `clock_enable`。

补充一个本讲要用到、但前面没展开的术语：**等时流水线（isochronous / latency-equal pipeline）**，指各级处理时间相等（典型为每级 1 拍）的理想流水线。它是衡量「该不该重叠计算」的基准。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Pipeline_Half_Buffer.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v) | 本讲主角：单数据寄存器 + 单满/空位的 ready/valid 缓冲，可作延迟握手 / 迭代控制器。 |
| [handshake.html](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html) | 握手规则正文，其中「Delayed Handshakes」一节是 Half Buffer 的理论依据。 |
| [Pulse_Generator.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v) | 把电平的上升沿转成一拍脉冲，Half Buffer 迭代控制方案的启动开关。 |
| [Pipeline_Skid_Buffer.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v) | 对照组：两个数据寄存器，支持满吞吐，与本讲做选型对比。 |
| [Register.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v) | Half Buffer 内部复用的基础寄存器，承担数据存储与满/空位两职。 |

## 4. 核心概念与源码讲解

### 4.1 延迟握手：把「完成」推迟到算完为止

#### 4.1.1 概念说明

正常的 ready/valid 握手追求**单拍完成**：source 一拉 `valid`，destination 只要能收就立刻拉 `ready`，同拍成交，下一拍 source 就能送下一项。这要求 source 与 destination 的处理**重叠**进行——destination 还在算当前项时，source 已经在准备下一项。

**延迟握手**反其道而行：destination 在 source 拉高 `valid` 的那一拍**先把数据收下**，但**不立即回 `ready`**，而是等自己把这项数据处理完，再拉一拍 `ready` 来「正式完成」握手，从而推动 source 送出下一项。换句话说，握手是「收下即开始、算完才确认」。

这本不是违规——它在 ready/valid 规则下完全合法——但它**破坏了重叠**：source 必须等 destination 算完才能进下一项，于是失去了流水线「边算边送」的好处。

#### 4.1.2 核心流程

设 source 与 destination 各需 \(T\) 拍处理（等时）。

- **理想重叠流水线**（正常握手）：第 0 项成交后，第 1 项立刻可送；吞吐为每拍一项：

\[
\text{throughput}_{\text{overlap}} = \frac{1}{T}
\]

- **延迟握手**（不重叠）：第 \(k\) 项必须「source 送 \(T\) + destination 算 \(T\)」串行完成，source 才能送第 \(k+1\) 项：

\[
\text{throughput}_{\text{delayed}} = \frac{1}{2T} = \frac{1}{2}\,\text{throughput}_{\text{overlap}}
\]

即在等时场景下，延迟握手把吞吐**减半**。这正是 handshake.html 里那句「this incorrect handshake will halve the throughput instead of doubling it」的含义。

延迟握手的时间轴（等时，\(T=1\) 拍）：

```text
cycle:   0      1      2      3      4
source:  v0     等R    v1     等R    v2        (v=assert valid; 等R=等 ready)
dest:    收0    算0/R  收1    算1/R  收2        (R=算完才脉冲 ready)
成交:           ^0           ^1           ^2   <- 每两项间隔 2 拍
```

每两项成交间隔 2 拍，吞吐减半。

#### 4.1.3 源码精读

延迟握手的理论定义在 handshake.html 的「Delayed Handshakes」小节：[handshake.html:103-126](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L103-L126)。

其中关键的两段——先点出吞吐代价，再给出迭代场景下的补救用法：

> 节选自 [handshake.html:110-116](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L110-L116)：延迟握手合法，但会干扰常规数据流流水线；当 source 与 destination 处理时间相等时，它会**把吞吐减半而不是加倍**。

> 节选自 [handshake.html:117-126](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L117-L126)：当不需要（也不可能）满吞吐流水线时（例如**迭代计算**），延迟握手反而是一种很有用的**控制机制**——它向 source 报告「可以处理下一项了」，而 Half Buffer 正是这种机制的实现。

#### 4.1.4 代码实践

**实践目标**：亲手验证「等时 → 减半」这条结论。

1. 假设 source 每拍都能给出一项（`valid` 常高），destination 每项要算 1 拍，且采用延迟握手（收下后下一拍才脉冲 `ready`）。
2. 在纸上画出 6 个时钟周期的 `valid` / `ready` / 成交标记。
3. 数一数这段时间内成交了几项，除以周期数，得到吞吐。

**需要观察的现象**：成交每隔一拍才出现一次。

**预期结果**：6 拍约成交 3 项，吞吐 ≈ 1/2 项/拍，与公式 \(\frac{1}{2T}\) 吻合。（本实践为纸面推导，待本地用仿真波形最终验证。）

#### 4.1.5 小练习与答案

**练习 1**：若 destination 处理时间为 3 拍、source 处理时间为 1 拍，延迟握手下吞吐是多少？正常重叠握手又是多少？

**答案**：延迟握手 = \(1/(3+1) = 1/4\) 项/拍；正常重叠 = \(1/\max(3,1) = 1/3\) 项/拍。延迟握手永远比重叠慢，且 source/dest 时间越接近，相对损失越大。

**练习 2**：延迟握手有没有违反 u9-1 的「source 不得等 ready 才拉 valid」这条防死锁规则？

**答案**：没有。destination 只是**推迟**拉 `ready`，并非等 source 的某个新信号；source 的 `valid` 仍然保持高（防活锁），最终一定会与 destination 的 `ready` 同拍相遇而成交。

---

### 4.2 Half Buffer：一个寄存器 + 一个满/空位

#### 4.2.1 概念说明

`Pipeline_Half_Buffer` 是一个**单拍深**的 ready/valid 缓冲：它把输入侧握手与输出侧握手**解耦**（二者之间无组合路径），但与 Skid Buffer 不同，它**不允许并发读写**——缓冲里的数据必须先被读走，才能再写入，因此最大带宽减半（CBM 例外）。

它的物理实现极简，只有两份存储：

- 一个 `WORD_WIDTH` 位的数据寄存器（`half_buffer`）；
- 一个 1 位的满/空标志（`empty_full`，输出 `buffer_full`）。

注意一个细节差异：Half Buffer 的 `input_ready`、`output_valid` 是 **`output reg`（组合输出）**，而 Skid Buffer 的同名信号是 **`output wire`（寄存输出）**。原因下文会讲。

#### 4.2.2 核心流程

Half Buffer 只有两个状态，由 `buffer_full` 一位编码：

```text
                 insert (输入侧握手完成 = set_to_full)
    ┌───────┐ ───────────────────────────────────────> ┌──────┐
    │ EMPTY │                                         │ FULL │
    │  full │ <────────────────────────────────────── │ full │
    └───────┘            remove (输出侧握手完成)       └──────┘
    input_ready=1                                  input_ready=0
    output_valid=0                                 output_valid=1
```

- **EMPTY**：`buffer_full=0`。`input_ready=1`（可收），`output_valid=0`（无可给）。输入侧一成交，本拍 `set_to_full=1`，下拍进入 FULL。
- **FULL**：`buffer_full=1`。`input_ready=0`（不可再收），`output_valid=1`（有数据给）。输出侧一成交，本拍 `set_to_empty=1`，下拍回到 EMPTY。

注意：FULL 状态下 `input_ready=0`，所以**输入与输出握手不可能同拍成交**（这是与 Skid Buffer「flow」变换的根本区别）。这强制输入、输出交替出现 → 吞吐减半。

控制信号的组合定义（来自源码）：

```text
input_ready      = (!buffer_full) || CIRCULAR_BUFFER
output_valid     =  buffer_full
set_to_full      =  input_valid && input_ready              // 输入握手完成 = insert
set_to_empty     = (output_valid && output_ready && !set_to_full) || clear
half_buffer_load =  set_to_full                              // 成交即载入新数据
```

为什么 `set_to_empty` 要加 `&& !set_to_full`？因为只有一个数据寄存器：若（在 CBM 下）同拍既 insert 又 remove，应当让新写入的数据留下、缓冲保持 FULL，所以此时**不能**把满/空位清掉。普通模式下 `set_to_full` 与「输出握手完成」不可能同拍为真（理由见上），这一项是给 CBM 的 `pass` 变换兜底。

满/空位 `empty_full` 被当成一个 **set/clear 触发器**来用：`clock_enable=set_to_full`（置 1）、`clear=set_to_empty`（清 0）、`data_in=1'b1`。借助 Register「`clear` 优先于 `clock_enable`」的「最后赋值胜出」语义（u3-l2/u6-l1），二者同时有效时清零胜出。

#### 4.2.3 源码精读

**端口与参数** ——[Pipeline_Half_Buffer.v:50-66](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L50-L66)：注意 `input_ready`、`output_valid` 声明为 `output reg`（组合），而 `output_data` 是 `output wire`（来自下面的 Register 实例）。`WORD_WIDTH`、`CIRCULAR_BUFFER` 默认都是 0（u1-l2 的安全栅栏：必须实例化设参数）。

**数据存储** ——[Pipeline_Half_Buffer.v:70-86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L70-L86)：实例化一个 `Register` 作 `half_buffer`，`clock_enable` 接 `half_buffer_load`（即 `set_to_full`），`data_in` 接 `input_data`，`data_out` 接 `output_data`。只有输入侧握手完成的那一拍才载入。

**满/空位** ——[Pipeline_Half_Buffer.v:88-106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L88-L106)：1 位 `Register` 作 `empty_full`，`clock_enable=set_to_full`、`clear=set_to_empty`、`data_in=1'b1`，输出即 `buffer_full`。复位值 `1'b0`（上电为 EMPTY）。

**组合控制块** ——[Pipeline_Half_Buffer.v:118-125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L118-L125)：用阻塞赋值（u3-l1 的组合块铁律）一次算出 `input_ready`、`output_valid`、`set_to_full`、`set_to_empty`、`half_buffer_load`。整段只依赖 `buffer_full`（已寄存）与各侧本地握手信号，**不**把输入侧信号组合地连到输出侧，故满足 handshake.html 的无组合环路规则（[handshake.html:59-81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L59-L81)）。

源码头注释也对两种模式做了直接说明：[Pipeline_Half_Buffer.v:2-9](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L2-L9)（解耦但不可并发读写，吞吐减半）与 [Pipeline_Half_Buffer.v:28-46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L28-L46)（CBM 改为缓存「最新」值、允许同拍收发）。

#### 4.2.4 代码实践

**实践目标**：用 `buffer_full` 这一位预测整模块行为，验证「输入/输出强制交替」。

1. 阅读 [Pipeline_Half_Buffer.v:118-125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L118-L125)。
2. 填下面这张表（设 `CIRCULAR_BUFFER=0`、`clear=0`）：

| `buffer_full` | `input_valid` | `output_ready` | `input_ready` | `output_valid` | 下一拍 `buffer_full` |
| --- | --- | --- | --- | --- | --- |
| 0 | 1 | x | ? | ? | ? |
| 1 | x | 1 | ? | ? | ? |
| 1 | 1 | 1 | ? | ? | ?（注意：这里能否成交？）|

3. 解释第 3 行里 `input_ready` 为什么是 0，从而说明「FULL 时无法 insert」。

**预期结果**：第 3 行 `input_ready=0`，故 `set_to_full=0`，输入侧不会成交；输出侧成交后 `set_to_empty=1`，下拍回到 EMPTY。这就是「必须先读出才能再写入」的机制根源。（待本地用仿真波形确认每拍翻转。）

#### 4.2.5 小练习与答案

**练习 1**：Half Buffer 的 `input_ready`、`output_valid` 为何可以做成组合输出而不会引入毛刺/组合环？

**答案**：它们的唯一输入是已寄存的 `buffer_full`（外加 elaboration 期常量 `CIRCULAR_BUFFER`），所以它们只会在时钟边沿改变 `buffer_full` 之后才翻转，本身不依赖任何输入侧的 `valid`/组合信号，自然不会形成输入到输出的组合路径。

**练习 2**：把 `set_to_empty` 里的 `&& !set_to_full` 去掉，在普通（非 CBM）模式下行为会变吗？

**答案**：不会。普通模式下 `set_to_full` 与输出握手完成不可能同拍为真（FULL 时 `input_ready=0`），故该项恒为 0，去掉无影响。它只在 CBM 的同拍收发（pass）时起兜底作用——防止新数据被错误清掉。

---

### 4.3 Half Buffer 作为迭代控制机制

#### 4.3.1 概念说明

延迟握手在等时流水线里是「坏事」，因为白白丢掉了重叠。但有一类计算**本来就无法重叠**：**迭代计算**——一个结果要反复迭代很多拍才能算完（如恢复余数除法、多次乘加、解方程的定点迭代），在结果算完之前既不能、也不该接收下一项。对这类计算，「重叠」从来就不存在，延迟握手的吞吐代价为零。

于是延迟握手从「缺陷」变成「特性」：它天然给出一个**控制信号**——「我现在算完了，可以收下一项了」。Half Buffer 就是把这种控制机制做成标准件的模块。

#### 4.3.2 核心流程

把一个迭代计算模块接到 Half Buffer 的**输出侧**（作 destination），Half Buffer 的输入侧接上游 source。整套「启动—计算—完成—收下一项」的时序如下（参见 handshake.html 的描述）：

1. **收下**：source 拉高 `input_valid`，Half Buffer 此时 EMPTY 故 `input_ready=1`，输入侧同拍成交，数据落入 `half_buffer` 寄存器，下拍 `buffer_full=1`。
2. **启动**：Half Buffer 的 `output_valid` 随 `buffer_full` 一起拉高，数据出现在 `output_data`。用 `Pulse_Generator` 检测 `output_valid` 的**上升沿**，产生一拍启动脉冲，开启迭代计算。
3. **计算**：迭代模块在这一拍拿到数据开始算，期间把自己的 `output_ready`（即 Half Buffer 输出侧的 destination ready）**保持低**——这正是「延迟握手」：收下了但不确认。
4. **完成**：迭代算完后，模块拉一拍 `output_ready`，输出侧握手成交 → `set_to_empty=1` → 下拍 `buffer_full=0`。
5. **放行下一项**：`buffer_full=0` 使 `input_ready` 重新拉高，source 被允许送下一项——这一信号就是「可以处理下一项了」。

关键点：**Half Buffer 的 `input_ready` 本身就是迭代节拍控制器**。上游不必知道迭代要几拍，只要盯着 `input_ready` 即可。`output_valid` 的上升沿则是「开始第 k 次迭代」的发令枪。

#### 4.3.3 源码精读

handshake.html 把这套用法写得非常清楚：[handshake.html:117-126](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L117-L126)——Half Buffer 接收一项后，在自身 source 接口上把该项与一个 valid 同时呈现；用 Pulse Generator 检测 valid 上升沿来启动内部逻辑，控制逻辑只需在算完后脉冲 ready 即可完成握手，「简化了控制并保持了并发」。

Half Buffer 源码头注释给出了同样的工程动机：[Pipeline_Half_Buffer.v:11-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L11-L25)——对「输入要等输出读出才能收新数据、且算一个结果要好几拍」的长耗时模块，Half Buffer 让模块能立刻把结果倒进缓冲、随即接收新输入，把下一次计算与「等最终目的地读走缓冲」的时间**重叠**起来；同时它也是一种控制机制，配合 Pulse Generator 用输出 valid 的上升沿启动内部逻辑。

`Pulse_Generator` 的实现就一行边沿检测：[Pulse_Generator.v:52-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v#L52-L56)——把输入延迟一拍后比较，`pulse_posedge_out = level_in && !level_in_delayed`，正好把 `output_valid` 的 0→1 跳变变成一拍脉冲。

#### 4.3.4 代码实践

**实践目标**：在纸上把「Half Buffer + Pulse_Generator + 迭代模块」串成一条控制链，标出每一步的因果关系。

1. 画三方框图：
   - 上游 source → Half Buffer（输入侧 source 接口）；
   - Half Buffer（输出侧 source 接口）→ 迭代模块（destination）；
   - `output_valid` 另接一个 `Pulse_Generator`，其 `pulse_posedge_out` 接迭代模块的 `start`。
2. 画出 `buffer_full`、`output_valid`、`pulse_posedge_out`、迭代模块的 `done`（即它回送的 `output_ready` 脉冲）四个信号，标一次完整迭代（假设算 3 拍）的时间轴。
3. 指出：`input_ready` 在哪一拍重新升高？它对应迭代流程的哪一步？

**需要观察的现象**：`pulse_posedge_out` 只在每次 `output_valid` 上升沿出现一拍；在迭代模块 `done` 脉冲出现的下一拍，`buffer_full` 才掉到 0，`input_ready` 才升高。

**预期结果**：`input_ready` 的上升沿恰好「可以处理下一项」，与第 5 步一致。这条链就是「延迟握手 = 迭代控制器」的实物化。（本实践为纸面时序推导，待本地仿真验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `output_valid` 的**上升沿**（而不是电平）来启动计算？

**答案**：`output_valid` 在整个 FULL 期间都为高（可能持续多拍，直到被读走）。用电平启动会在每拍重复触发；用上升沿经 Pulse_Generator 转成一拍脉冲，保证每个数据项只启动一次计算。

**练习 2**：如果迭代模块算完后忘记脉冲 `output_ready`，会发生什么？

**答案**：输出侧握手永不完成，`buffer_full` 永远为 1，`input_ready` 永远为 0，整个流水线卡死在上游——这是延迟握手必须配防死锁/超时控制的现实原因。

---

### 4.4 Half Buffer 与 Skid Buffer：一个寄存器之差

#### 4.4.1 概念说明

Half Buffer 与 Skid Buffer 解决的是**同一个矛盾**——给 ready/valid 握手加流水线寄存器会逼出输入到输出的组合路径。二者的差别只在于「用几个寄存器消这个矛盾」：

- **Half Buffer**：1 个数据寄存器。代价是**不可并发读写**，吞吐减半。
- **Skid Buffer**：2 个数据寄存器（多一个「skid/缓冲」寄存器）。换来的是**可并发读写**（BUSY 态下 insert+remove 同拍成交，即 flow 变换），满吞吐。

#### 4.4.2 核心流程

以「连续送 N 项、下游每项都立刻收」为负载，比较二者（非 CBM，启动后稳态）：

| 维度 | Half Buffer | Skid Buffer |
| --- | --- | --- |
| 数据寄存器数 | 1 | 2 |
| 状态数 / 编码 | 2 态（`buffer_full` 一位） | 3 态（EMPTY/BUSY/FULL，2 位） |
| `input_ready`/`output_valid` | 组合（`output reg`） | 寄存（`output wire`，来自 Register 实例） |
| 稳态吞吐 | **1/2 项/拍**（输入输出强制交替） | **1 项/拍**（BUSY 态可 flow） |
| 同拍 insert+remove | 普通：不可；CBM：可以 | 普通：BUSY 态可以（flow）；FULL 态 CBM 可以（pass） |
| 适用场景 | 迭代计算、延迟握手控制、极小面积 | 普通流水线打拍、时序收敛、满吞吐背靠背传输 |

Half Buffer 的稳态时间轴（吞吐 1/2）：

```text
cycle:  0     1     2     3     4
状态:   EMPTY FULL  EMPTY FULL  EMPTY
成交:   in0   out0  in1   out1  in2     <- 每拍只能成交一侧
```

Skid Buffer 的稳态时间轴（吞吐 1，BUSY 态 flow）：

```text
cycle:  0     1     2     3     4
状态:   EMPTY BUSY  BUSY  BUSY  BUSY
成交:   in0   in1   in2   in3   in4     <- in 与 out 同拍 flow
              /out0 /out1 /out2 /out3
```

选型口诀：**要满吞吐、要打拍修时序 → Skid Buffer；要迭代控制、面积最小、或刻意限速 → Half Buffer。**

#### 4.4.3 源码精读

**Skid Buffer 的两个数据寄存器** ——[Pipeline_Skid_Buffer.v:143-180](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L143-L180)：`data_buffer_reg`（skid 寄存器）与 `data_out_reg`（输出寄存器），中间一个 2:1 选择器。正是这个 `data_buffer_reg` 让输入在输出被读时「skid to a stop」而不丢数据。

**Skid Buffer 的 flow 变换** ——[Pipeline_Skid_Buffer.v:352](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L352)：`flow = (state==BUSY) && insert && remove`，BUSY 态下输入输出同拍成交、状态维持 BUSY——这就是满吞吐的来源，Half Buffer 没有等价变换。

**Skid Buffer 把握手输出寄存起来** ——[Pipeline_Skid_Buffer.v:291-319](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L291-L319)：`input_ready`、`output_valid` 各自从一个 `Register` 实例引出（故为 `output wire`），用 `state_next` 计算，「nice registered outputs」。对比 Half Buffer 的组合输出 [Pipeline_Half_Buffer.v:118-125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L118-L125)，可见状态机越复杂、越倾向寄存输出。

**面积对比** ——Skid Buffer 头注释给出 64 位连接的资源账：[Pipeline_Skid_Buffer.v:399-402](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L399-L402)（128 个缓冲寄存器 + 4~9 个 FSM/接口寄存器）；Half Buffer 同位宽只需 64 个数据寄存器 + 1 个满/空位 + 少量控制，面积约为前者一半。这与上表「寄存器数 1 vs 2」一致。

#### 4.4.4 代码实践

**实践目标**：用一个寄存器之差解释一倍吞吐之差。

1. 对照阅读 [Pipeline_Half_Buffer.v:70-86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Half_Buffer.v#L70-L86)（1 个数据寄存器）与 [Pipeline_Skid_Buffer.v:143-180](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L143-L180)（2 个数据寄存器 + 选择器）。
2. 回答：Half Buffer 缺的正是 `data_buffer_reg`。请说明——当输出侧正在被读（`output_ready=1`）而输入侧同时有新数据（`input_valid=1`）到来时，Skid Buffer 靠这个多余寄存器做了什么，而 Half Buffer 为什么做不到？
3. 给下面三个场景选型（Half / Skid）并说明理由：(a) 把一个长组合逻辑切成两段以满足时序；(b) 一个恢复余数除法器，每项要迭代 16 拍；(c) 一个 1 进 1 出的限速器，故意把上游带宽砍半。

**预期结果**：(a) Skid（要满吞吐打拍）；(b) Half（迭代控制，重叠无意义，且 `input_ready` 即「下一项」信号）；(c) Half（刻意减半）。(待本地综合/仿真确认资源与吞吐数值。)

#### 4.4.5 小练习与答案

**练习 1**：Half Buffer 和 Skid Buffer 都有 `CIRCULAR_BUFFER` 参数。两者 CBM 的语义有何相同与不同？

**答案**：相同——都把输入侧改为「总可收」，缓存「最新」值而非「最早」值，允许同拍 insert+remove。不同——Half Buffer 的 CBM 是「单入口循环缓冲」（只有 1 项，新值覆盖未读旧值），Skid Buffer 的 CBM 是「双入口循环缓冲」（2 项，未读的较早值会被缓冲寄存器里的次新值替换）。

**练习 2**：既然 Skid Buffer 吞吐更高、功能更全，为什么还要保留 Half Buffer？

**答案**：面积更小（数据寄存器减半）、控制更简单（2 态/1 位 vs 3 态/2 位、组合 vs 寄存输出），并且在迭代计算场景下 Skid Buffer 多出来的吞吐**用不上**——延迟握手本就不重叠。Half Buffer 是「刚好够用」的最小实现，契合本书「构建块库」按需选件的理念。

---

## 5. 综合实践

**任务**：为一个假想的 4 拍迭代乘法器设计一个 ready/valid 接口外壳，并解释 Half Buffer 在其中的角色。

要求：

1. 画出整体框图：上游 source → `Pipeline_Half_Buffer` → （输出侧同时接）①迭代乘法器的 `data_in`、②`Pulse_Generator` 的 `level_in`；迭代乘法器的 `done` 接回 Half Buffer 的 `output_ready`；Half Buffer 的 `input_ready` 引出作「busy/可收下一项」状态。
2. **画出 Half Buffer 内部的 valid/ready 路径**：标清 `input_valid`/`input_ready`/`input_data`、`output_valid`/`output_ready`/`output_data` 的方向，并明确标出 `input_ready` 只依赖 `buffer_full`、`output_valid` 只依赖 `buffer_full`，二者之间**无组合路径**（唯一耦合是寄存的 `buffer_full` 与 `half_buffer`）。
3. 给出一次完整迭代（4 拍计算）的时间轴：含 `input_valid`、`input_ready`、`buffer_full`、`output_valid`、`pulse_posedge_out`、`done`(`output_ready`) 六个信号。
4. 计算：若上游连续送数据、每项算 4 拍，稳态吞吐是多少？与「等时理想流水线」相比损失了多少？这个损失是否可避免？

**参考要点**：

- 内部路径图核心是「两侧握手信号都只由 `buffer_full` 组合产生，无 input→output 组合连线」——这正是它能插入 ready/valid 链路而不引入组合环的原因。
- 稳态吞吐 = 1 项 / (1 拍载入 + 4 拍计算) ≈ 1/5 项/拍；相对等时理想流水线的 1 项/拍有损失，但**不可避免**——迭代计算本质串行，无法重叠，Half Buffer 只是把这个事实如实暴露成控制信号。
- 若误用 Skid Buffer 替代，多出的 skid 寄存器不会提升吞吐（下游算 4 拍才是瓶颈），却白白多占面积——这正是选 Half Buffer 的理由。

（本任务为设计型纸面练习；有条件时可用 `Simulation_Clock` 与 `Synthesis_Harness`（见 u18-l2）搭测试台，用 cocotb 验证时间轴。）

## 6. 本讲小结

- **延迟握手**让 destination「先收下、算完才确认」，合法但破坏计算重叠，在等时流水线里把吞吐从 \(1/T\) 砍到 \(1/(2T)\)。
- 当计算**本质串行**（迭代计算）时，重叠本就不存在，延迟握手零代价，反而变成「可以处理下一项」的天然控制信号。
- `Pipeline_Half_Buffer` 用**一个数据寄存器 + 一个满/空位**实现这种控制：`input_ready = !buffer_full`、`output_valid = buffer_full`，二者都只依赖已寄存的 `buffer_full`，故无组合环。
- FULL 状态下 `input_ready=0`，**强制输入输出交替** → 吞吐减半（CBM 例外）。
- 配合 `Pulse_Generator` 检测 `output_valid` 上升沿即可启动迭代，算完后脉冲 `output_ready` 完成握手、放行下一项——`input_ready` 即迭代节拍器。
- 与 **Skid Buffer** 相比只差一个 skid 寄存器：Half Buffer 1 寄存器/2 态/组合输出/半吞吐，适合迭代与限速；Skid Buffer 2 寄存器/3 态/寄存输出/满吞吐，适合打拍修时序。

## 7. 下一步学习建议

- 阅读 `Pipeline_Half_Buffer` 的 CBM 分支与 `Pipeline_Skid_Buffer` 的 CBM `pass`/`dump` 变换，对照理解「缓存最新 vs 缓存最早」的单/双入口循环缓冲。
- 进入 u11（仲裁与同步原语），看 `Synchronous_Muller_C_Element` 与 `Pipeline_Synchronizer_Lazy` 如何在更底层实现 handshake.html 所说的 OK_IN/OK_OUT「会合（rendez-vous）」——延迟握手正是会合的一种时序实例。
- 若对「把多个构建块拼成大引擎」感兴趣，可跳读 u17-l1 的 `Pipeline_Iterator`：它正是 Half Buffer 这套迭代控制思想的规模化应用。
