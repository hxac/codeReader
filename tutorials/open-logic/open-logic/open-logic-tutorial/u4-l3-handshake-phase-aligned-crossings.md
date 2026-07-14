# 握手与相位对齐跨时钟域

## 1. 本讲目标

本讲继续 Open Logic 跨时钟域（CDC）系列。学完后你应该能够：

- 说清楚 `olo_base_cc_handshake` 如何用标准 AXI-S 的 Valid/Ready 在**两个完全异步的时钟域**之间搬运数据，以及它为什么「安全但不高吞吐」。
- 理解「相位对齐 + 整数倍频率」这一特殊前提为什么能让跨域变得**更便宜**（无需 RAM、可达满吞吐）。
- 读懂 `olo_base_cc_n2xn`（慢到快）与 `olo_base_cc_xn2n`（快到慢）两个实体的实现，并能判断自己的时钟是否符合使用前提。
- 在仿真里跑通一个快慢时钟比 1:N 的数据流，亲眼确认它不需要异步 FIFO 也能正确跨域。

## 2. 前置知识

本讲假设你已经掌握下面几讲的内容（它们建立了本讲直接复用的概念，这里只做最小回顾）：

- **u4-l1 跨时钟域原理与约束、复位穿越**：CDC 必须「电路 + 约束」配套；同步器降低亚稳态概率；跨域逻辑要接 `Xxx_RstOut` 而不是 `RstIn`。
- **u4-l2 简单跨时钟域：pulse/simple/status**：「翻转（toggle）协议」——源域用脉冲翻转一个稳定电平，目的域用「本拍 XOR 上一拍」还原出恰好一个脉冲。`cc_simple` 跨「带 Valid 的采样值」，`cc_pulse` 跨「事件脉冲」，二者都无 RAM、无反压。
- **u2-l2 流水线阶段与 AXI-S 握手**：AXI-S 的 Valid/Ready 握手、反压（下游不收则上游停）的含义。

补充几个本讲会反复用到的直觉：

| 概念 | 一句话解释 |
| :--- | :--- |
| 异步时钟 | 两个时钟来自不同源、没有固定相位关系（如 100 MHz 与 55.32 MHz）。 |
| 相位对齐 | 两个时钟来自同一个 PLL，慢时钟的上升沿恰好与某些快时钟上升沿重合。 |
| 整数倍频率 | 一个时钟周期是另一个的整数倍（如 50 MHz 与 100 MHz，比为 1:2）。 |
| 满吞吐（100% Perf.） | 每个时钟周期都能传一个数据，不被迫插入空闲拍。 |
| 反压（Ready） | 下游有权用 Ready=0 让上游暂停，数据不丢。 |

一句话锚定本讲的两类场景：

> **异步时钟 + 要反压 → `cc_handshake`（慢，无 RAM）。**  
> **同源、相位对齐、整数倍 → `cc_n2xn` / `cc_xn2n`（便宜，可满吞吐，无 RAM）。**

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/base/vhdl/olo_base_cc_handshake.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd) | 异步时钟域之间的 Valid/Ready 握手跨越；内部复用 `cc_simple`（前向）+ `cc_pulse`（反向应答）。 |
| [src/base/vhdl/olo_base_cc_n2xn.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_n2xn.vhd) | 慢时钟 → 快时钟（整数倍、相位对齐）的跨越，用翻转协议。 |
| [src/base/vhdl/olo_base_cc_xn2n.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_xn2n.vhd) | 快时钟 → 慢时钟（整数倍、相位对齐）的跨越，用 2 深度缓冲 + 计数器。 |
| [doc/base/clock_crossing_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md) | 全库 CDC 通则：约束方法、复位穿越、选型表。 |
| [test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd) | xn2n 的 VUnit 测试台，含「相位对齐时钟」的产生方式，是本讲综合实践的蓝本。 |

> 说明：本讲引用的源码行号均基于当前 HEAD `ecca8af`。`cc_n2xn` / `cc_xn2n` 的官方文档（`olo_base_cc_n2xn.md`、`olo_base_cc_xn2n.md`）在「Architecture」一节明确写道「The architecture of the entity is simple, no detailed description is required」，因此实现细节须以源码为准——这正是本讲要精读的部分。

---

## 4. 核心概念与源码讲解

### 4.1 cc_handshake：异步时钟域之间的 Valid/Ready 握手跨越

#### 4.1.1 概念说明

当两个时钟**完全异步**（没有相位关系），你又需要：

1. 传输**任意数据**（多 bit）；
2. 支持**反压**（下游可以 Ready=0 让上游等）；
3. **不希望/不能**占用 RAM 资源；

那么 `olo_base_cc_handshake` 就是 Open Logic 给出的答案。

它的设计哲学在文件头注释里写得很直白：「**not meant to achieve high-performance but to be simple and safe**」（不为高性能，而为简单安全）。它的吞吐是受限的：传送一个字需要

\[
T_{\text{transfer}} = (2 + \text{SyncStages\_g})\cdot T_{\text{in}} + (2 + \text{SyncStages\_g})\cdot T_{\text{out}}
\]

也就是说，一次传输要完成「请求过域 + 应答回域」一个完整往返，期间上游必须停手等待。因此它**达不到满吞吐**。

> 何时选它、何时不选？官方建议：只要能用分布式 RAM（LUT 当小 RAM），优先用 `olo_base_fifo_async`（u3-l1），它对小深度更省资源、吞吐更高。只有当分布式 RAM 不可用（工艺不支持，或 LUT 资源紧张）时，才退而用 `cc_handshake`。

#### 4.1.2 核心流程

`cc_handshake` 的本质是一个**跨两个异步域的「请求—应答」（request / acknowledge）往返**，用两个现成的积木搭出来：

- **前向（In 域 → Out 域）**：用 `cc_simple` 把「数据 + 一次请求（Transaction 脉冲）」送到目的域。
- **反向（Out 域 → In 域）**：用 `cc_pulse` 把「应答脉冲（Ack）」送回源域。

一次完整传输的伪代码如下：

```
# In 域（源）
当 In_Valid=1 且未持有数据(InLatched=0)：
    锁存数据 -> InLatched <= 1
    In_Ready <= 0          # 停止接收，等应答
    产生 InTransaction 脉冲  # 作为请求交给 cc_simple

# cc_simple 把请求 + 数据搬到 Out 域
# Out 域（目的）
收到 OutTransaction=1：
    驱动 Out_Data / Out_Valid
    当 Out_Ready=1：
        产生 OutAck 脉冲    # 作为应答交给 cc_pulse
    （若 Out_Ready=0，用 OutLatched 暂存，做到不丢）

# cc_pulse 把应答脉冲搬回 In 域
# In 域收到 InAck：
    InLatched <= 0
    In_Ready <= 1          # 解锁，准备收下一个字
```

关键点：**源域只有收到反向应答后才会解锁**，这就天然保证了「每来一个请求，目的域恰好处理一次，不丢不重」。代价是每次都要等一个跨域往返。

#### 4.1.3 源码精读

实体声明有三个泛型：数据宽度 `Width_g`、复位期间 `In_Ready` 电平 `ReadyRstState_g`、同步器级数 `SyncStages_g`（2~4）；端口在两个时钟域各有一组 `RstIn`（复位请求）/ `RstOut`（复位有效输出）：

[src/base/vhdl/olo_base_cc_handshake.vhd:30-50](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L30-L50) — 实体声明：泛型与双时钟域端口（每个域都有 `RstIn`/`RstOut`，符合 u4-l1 的复位穿越约定）。

源域进程 `p_in` 负责锁存与解锁——注意它把复位写在进程**末尾**作为覆盖（Open Logic 全库约定）：

[src/base/vhdl/olo_base_cc_handshake.vhd:74-94](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L74-L94) — `p_in` 进程：收到应答 `InAck` 则清锁存；当 `In_Valid` 且 `In_ReadyI` 同时有效则置锁存；末尾复位覆盖。两次对 `InLatched` 的赋值中，后写的覆盖先写的，正是 u1-l5 讲过的「复位放进程末尾」写法。

`In_Ready` 的组合逻辑：未锁存或刚收到应答时为高；`ReadyRstState_g='0'` 时复位期间强制拉低：

[src/base/vhdl/olo_base_cc_handshake.vhd:96-99](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L96-L99) — `In_Ready` 组合产生；`InTransaction` 即前向请求脉冲（`In_Valid and In_ReadyI`）。

前向通路直接复用 `cc_simple`，把请求脉冲和数据一起搬到目的域：

[src/base/vhdl/olo_base_cc_handshake.vhd:102-118](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L102-L118) — `i_scc`：实例化 `olo_base_cc_simple`，`In_Valid => InTransaction`、`Out_Valid => OutTransaction`，数据直通。

反向通路用 `cc_pulse` 把目的域的应答送回源域：

[src/base/vhdl/olo_base_cc_handshake.vhd:124-136](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L124-L136) — `i_bcc`：实例化 `olo_base_cc_pulse`，注意它的「In」接的是 `Out_Clk`（目的域），「Out」接的是 `In_Clk`（源域），方向与前向相反，把 `OutAck` 回传为 `InAck`。

目的域进程 `p_out` 用 `OutLatched` 暂存数据，使得下游 `Out_Ready=0` 时数据不丢：

[src/base/vhdl/olo_base_cc_handshake.vhd:139-159](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L139-L159) — `p_out` 进程与输出逻辑：`OutTransaction` 来了但下游不收就锁存到 `OutLatched`；`Out_Valid = OutTransaction or OutLatched`；`OutAck = Out_Valid and Out_Ready` 回送给反向通路。

> 这套「前向 cc_simple + 反向 cc_pulse」的组合，正是 u4-l2 讲过的两个积木的再组合。`cc_handshake` 自己不发明新协议，只把它们拼成一个支持反压的闭环。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：跟踪一个数据字穿过 `cc_handshake` 的「请求—应答」闭环，验证它「每请求恰处理一次、需跨域往返」。

**操作步骤**：

1. 打开 [test/base/olo_base_cc_handshake/olo_base_cc_handshake_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_handshake_tb.vhd) 的 `Basic` 用例（约 L154-L161）：它先推一个 `5`，再推一个 `10`，中间 `wait for 20*SlowerClock_Period_c`。
2. 对照实体源码，在心里把 `5` 这一个字走一遍：`push` → `InTransaction` → `cc_simple`（经同步器）→ `OutTransaction` → `OutAck` → `cc_pulse`（经同步器）→ `InAck` → 解锁。
3. 数一下这一趟穿过了几个同步器链（前向一条 + 反向一条），据此估算单字延迟。

**需要观察的现象**：第二个字 `10` 必须等第一个字整趟往返完成后才能进入（`In_Ready` 在往返期间为 0）。

**预期结果**：两个数据值都按序出现在输出侧；单字延迟与上面公式一致（默认 `SyncStages_g=2`）。精确的仿真波形与延迟拍数**待本地验证**（见 4.4 的运行步骤，把目标改成 `cc_handshake` 即可）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cc_handshake` 无法达到满吞吐？
**答案**：每传一个字都要等「请求过域 + 应答回域」一个完整往返，期间 `In_Ready` 为 0，上游被迫插入大量空闲拍。

**练习 2**：若你的设计既可以用分布式 RAM、又必须满吞吐地跨异步时钟，应该选 `cc_handshake` 还是 `olo_base_fifo_async`？为什么？
**答案**：选 `olo_base_fifo_async`。它支持反压、能缓冲、可达满吞吐；`cc_handshake` 仅在「不能用 RAM」时才是退路。

---

### 4.2 相位对齐前提：为什么整数倍时钟能更便宜地跨域

#### 4.2.1 概念说明

`cc_n2xn` 与 `cc_xn2n` 是 Open Logic 跨时钟域家族里的「特惠款」。它们能用上，前提是两个时钟**同时满足**：

1. **来自同一个 PLL**（同步派生）；
2. **相位对齐**：慢时钟的上升沿，恰好和某些快时钟的上升沿重合；
3. **整数倍频率**：一个周期是另一个的整数倍；
4. **频率不相等**：官方文档明确写「does not work if the two clocks have the same frequency」。

为什么这个前提这么值钱？因为一旦边沿对齐，**快时钟域里的寄存器在对齐边沿之后已经稳定**，慢时钟在那同一拍去读它就是安全的——多 bit 数据可以**直接采样**，不需要异步 FIFO、不需要格雷码、不需要长长的同步器往返。

代价是适用面窄：它**不支持异步时钟**（选型表里 `Async. Clocks` 列为空），但换来 `100% Perf.`（满吞吐）和 `No RAM`。这是典型的「用适用范围换资源与吞吐」的取舍。

对照 [doc/base/clock_crossing_principles.md:60-69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L60-L69) 的选型表，`cc_n2xn`/`cc_xn2n` 与 `cc_handshake` 的差异一目了然：

| 实体 | 支持异步时钟 | 100% 满吞吐 | 无 RAM |
| :--- | :---: | :---: | :---: |
| cc_handshake | ✅ | ❌ | ✅ |
| fifo_async | ✅ | ✅ | ❌（需 RAM） |
| cc_n2xn / cc_xn2n | ❌（须相位对齐） | ✅ | ✅ |

#### 4.2.2 核心流程

相位对齐带来的「特惠」可以这样形式化。设快时钟周期为 \(T\)，慢时钟周期为 \(N\cdot T\)（\(N\ge 2\) 整数）。由于边沿对齐，每个慢时钟上升沿同时也是某个快时钟上升沿。于是：

- 慢域写入一个寄存器后，在下一个对齐的快时钟边沿，该值已稳定，快域可直接读。
- 反之，快域写入后，慢域在下一次自己的上升沿（也是对齐边沿）读到的也是稳定值。

因此这两个实体都**直接跨域读取对方的计数器/翻转位/数据寄存器**，只用模运算或异或来判断「有没有新数据」。这种直接读跨域信号的做法**只有在相位对齐时才安全**——这是理解后续两个实体的总钥匙。

#### 4.2.3 源码精读

「相位对齐」「整数倍」「频率不可相同」三条硬性前提，写在两个实体的文档里：

[doc/base/olo_base_cc_n2xn.md:21-23](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_cc_n2xn.md#L21-L23) — 明确「two clocks must be phase aligned」且「does not work if the two clocks have the same frequency」。

`cc_n2xn`/`cc_xn2n` 内部**没有**实例化任何位同步器（对比 `cc_handshake` 用的 `cc_simple`/`cc_pulse` 都含同步器链），它们只实例化了一个**复位跨越** `olo_base_cc_reset`（因为复位仍需穿越，见 u4-l1）。这也从侧面印证：数据通路靠的是相位对齐，而非同步器。

#### 4.2.4 代码实践（阅读型）

**实践目标**：在测试台里确认「相位对齐 + 整数倍」是如何被构造出来的。

**操作步骤**：

1. 打开 [test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd) 的时钟进程（L169-L180）。
2. 阅读这段进程：快时钟 `In_Clk` 在「慢时钟的半个周期」内翻转 `ClockRatio_g` 次，然后慢时钟 `Out_Clk` 才翻转一次。

[test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd:169-180](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd#L169-L180) — 相位对齐时钟的产生：快慢时钟共享同一个 `0.5*InClk_Period_c` 时间基准，慢时钟边沿始终与某个快时钟边沿重合，正是 PLL 相关时钟在真实硬件上的样子。

**需要观察的现象**：两个时钟的上升沿在仿真波形上周期性地重合；周期比为 `ClockRatio_g:1`。

**预期结果**：周期比精确等于 `ClockRatio_g`，且重合点稳定。

#### 4.2.5 小练习与答案

**练习 1**：能否用 `cc_n2xn` 把数据从 100 MHz 跨到 55.32 MHz？
**答案**：不能。55.32 MHz 与 100 MHz 既不是整数倍关系，也不相位对齐。这种情况必须用支持异步时钟的 `cc_handshake` 或 `fifo_async`。

**练习 2**：为什么官方强调「两个时钟频率相同时 `cc_n2xn`/`cc_xn2n` 不工作」？
**答案**：这两个实体的计数器/翻转逻辑是为「严格整数倍、边沿对齐」设计的，频率相同时不存在「边沿子集」关系可利用，逻辑前提不成立。频率相同时应直接用寄存器打一拍（同频同相）或改用 `cc_simple`/`cc_handshake`。

---

### 4.3 cc_n2xn：慢时钟到快时钟

#### 4.3.1 概念说明

`cc_n2xn` 名字里的 `n2xn` 表示「从 \(n\) 到 \(x\cdot n\)」：**输入时钟慢、输出时钟快**（输出频率是输入的整数倍）。典型场景：50 MHz 域 → 100 MHz 域，二者由同一 PLL 产生。

由于输出（快）域总是能在输入（慢）域的下一个数据到来之前把当前数据取走，这里只需要**一个数据寄存器 + 一个翻转位**，就能安全跨域——这比 `cc_handshake` 的往返闭环便宜得多，而且能满吞吐。

#### 4.3.2 核心流程

它采用**翻转（toggle）协议**（u4-l2 讲过），但省掉了同步器往返：

```
In_Ready = (InToggle == OutToggle) 且 非复位   # 翻转位相等 = 无待处理 = 可收

# 输入侧（慢域）
当 In_Valid=1 且 InToggle==OutToggle：
    InDataReg <= In_Data          # 锁存数据
    InToggle  <= not InToggle     # 翻转，标记「有新数据」

# 输出侧（快域）
当 InToggle != OutToggle（有新数据）且 缓冲可收：
    OutDataReg <= InDataReg       # 取走数据
    OutToggle  <= InToggle        # 追平翻转位，标记「已处理」
    OutDataVld <= 1
```

「翻转位相等/不等」本身就是一个 1 深度的握手：相等说明没有待处理的数据（可以收新数据），不等说明有一笔待取。因为输出比输入快，输出总能在一个输入周期内追平，所以不需要第二级缓冲。

#### 4.3.3 源码精读

`In_Ready` 的组合逻辑——翻转位相等即可接收：

[src/base/vhdl/olo_base_cc_n2xn.vhd:66](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_n2xn.vhd#L66) — `In_Ready`：当 `InToggle = OutToggle`（无待处理）且非复位时为高。

输入侧进程，锁存数据并翻转标记：

[src/base/vhdl/olo_base_cc_n2xn.vhd:68-80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_n2xn.vhd#L68-L80) — `p_input`：条件成立时把 `In_Data` 存入 `InDataReg` 并翻转 `InToggle`；末尾复位把 `InToggle` 清 0。

输出侧进程，检测新数据并取走：

[src/base/vhdl/olo_base_cc_n2xn.vhd:82-104](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_n2xn.vhd#L82-L104) — `p_output`：当 `InToggle /= OutToggle`（有新数据）且缓冲可收时，把 `InDataReg` 拷到 `OutDataReg`、置有效、并用 `OutToggle <= InToggle` 追平翻转位。

注意整个实体只实例化了复位跨越：

[src/base/vhdl/olo_base_cc_n2xn.vhd:110-118](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_n2xn.vhd#L110-L118) — `i_rst_cc`：`olo_base_cc_reset`，为两侧提供 `RstOut`。**数据通路没有任何位同步器**——这就是相位对齐前提换来的便宜。

#### 4.3.4 代码实践（阅读型）

**实践目标**：理解「输入慢、输出快」时为何只需 1 深度缓冲。

**操作步骤**：

1. 在 `p_output`（L82-L104）中确认：输出侧一旦追平翻转位，`In_Ready`（由 `InToggle==OutToggle` 决定）就会重新拉高。
2. 推演：50 MHz 输入每 20 ns 来一个数据；100 MHz 输出每 10 ns 检查一次。输出必然在下一个输入数据到来之前完成追平。

**预期结果**：背靠背输入时，每个输入数据都能被接收（`In_Ready` 在输入周期内恢复），不会因来不及处理而丢失。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cc_n2xn` 只用一个 `InDataReg`（没有影子寄存器）就够了？
**答案**：因为输出域更快，总能在输入域送来下一个数据之前把当前数据取走并追平翻转位，`In_Ready` 随即恢复。不存在「上游硬塞、下游来不及」的情况，故无需第二级缓冲。

**练习 2**：`In_Ready` 在一次「接收 → 输出取走」的过程中，电平如何变化？
**答案**：接收时翻转位变不等 → `In_Ready` 拉低；输出域（快）取走并追平翻转位 → `In_Ready` 重新拉高。整个过程发生在一个输入周期之内。

---

### 4.4 cc_xn2n：快时钟到慢时钟

#### 4.4.1 概念说明

`cc_xn2n` 名字表示「从 \(x\cdot n\) 到 \(n\)」：**输入时钟快、输出时钟慢**（输出频率是输入的整数分之一）。典型场景：100 MHz 域 → 50 MHz 域（比为 2:1），二者同源。

这里出现新难点：**在两个慢时钟边沿之间，快域可能塞进多个数据**。如果只留 1 深度缓冲就会丢数据。`cc_xn2n` 的解法是用**一对 2 深度寄存器 + 两个模 4 计数器**充当「填充度水位」。

#### 4.4.2 核心流程

输入侧（快域）维护计数器 `InCnt`，输出侧（慢域）维护 `OutCnt`，二者之差就是「待处理的水位」（因相位对齐，跨域直读安全）：

```
In_Ready = (InCnt - OutCnt != 2) 且 非复位   # 水位没到 2 就还能收

# 输入侧（快域）
当 In_Valid=1 且 水位 != 2：
    InCnt         <= InCnt + 1
    InDataReg     <= In_Data      # 当前
    InDataRegLast <= InDataReg    # 上一个（构成 2 深度缓冲）

# 输出侧（慢域）
当 InCnt != OutCnt（有数据）且 可收：
    若 水位(InCnt-OutCnt) == 1：Out_Data <= InDataReg      # 取最新的
    否则（水位>=2）           ：Out_Data <= InDataRegLast  # 追赶：取稍早的
    OutCnt <= OutCnt + 1
    置有效
```

水位到 2 就停收（`In_Ready=0`），等输出域取走再放行，从而做到不丢。两个模 4（2 bit）计数器之差天然表示 0~3 的水位，代码里用 `InCnt - OutCnt /= 2` 判满。

#### 4.4.3 源码精读

`In_Ready` 由水位判定：

[src/base/vhdl/olo_base_cc_xn2n.vhd:66](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_xn2n.vhd#L66) — `In_Ready`：`InCnt - OutCnt /= 2`（水位未满）且非复位时为高。

输入侧进程，维护计数器与 2 深度缓冲：

[src/base/vhdl/olo_base_cc_xn2n.vhd:68-81](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_xn2n.vhd#L68-L81) — `p_input`：每收一个数据，`InCnt` 加 1，并把旧 `InDataReg` 推进 `InDataRegLast`，形成两级缓冲。

输出侧进程，按水位决定取哪一级、并追赶计数器：

[src/base/vhdl/olo_base_cc_xn2n.vhd:83-105](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_xn2n.vhd#L83-L105) — `p_output`：当 `InCnt /= OutCnt` 且可收时，水位为 1 取 `InDataReg`，否则取 `InDataRegLast`，并 `OutCnt <= OutCnt + 1`。

同样只实例化复位跨越，数据通路无位同步器：

[src/base/vhdl/olo_base_cc_xn2n.vhd:110-118](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_xn2n.vhd#L110-L118) — `i_rst_cc`：`olo_base_cc_reset` 提供两侧 `RstOut`。

> 顺带一提：`cc_n2xn`/`cc_xn2n` 的复位**没有**像 `cc_handshake` 那样双向联动（选型表里它们的 `Reset Crossing` 列也为空），它们各自处理复位即可——这也是相位对齐简化设计的体现。

#### 4.4.4 代码实践（运行型，本讲主实践）

**实践目标**：在「快时钟 : 慢时钟 = 4 : 1」、相位对齐的两个时钟之间，用 `cc_xn2n` 传输一串数据，验证**无需异步 FIFO 即可正确跨域**，并说清前提条件。

**操作步骤**：

1. **确认前提**：阅读 [test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd:44-47](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd#L44-L47)：`InClk=100 MHz`（快），`OutClk_Period = ClockRatio_g × InClk_Period`（慢）。再读时钟进程 [L169-L180](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd#L169-L180)，确认快慢时钟共享 `0.5*InClk_Period_c` 基准，边沿对齐、比为 `ClockRatio_g:1`。
2. **加入 1:4 配置**：默认 `ClockRatio_g=3`，且 `sim/test_configs/olo_base.py` 只注册了 `[2, 3, 19]`（见 [olo_base.py:46-50](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L46-L50)）。为得到 **4:1**，在该循环里增加一行（仅改讲义目录下的说明，**不要改源码仓库文件**；正式实验时可临时改本地副本）：
   ```python
   for R in [2, 3, 4, 19]:   # 加入 4，对应 4:1
       named_config(tb, {'ClockRatio_g': R})
   ```
3. **运行仿真**（仿真器选择与命令格式见 u1-l4）：在 `sim` 目录运行 xn2n 的 `FullThrottle` 用例。VUnit 用位置参数做测试名过滤，命令大致为（精确命令与路径**待本地验证**）：
   ```bash
   cd sim
   python run.py --ghdl olo_base_cc_xn2n.FullThrottle
   ```
4. **观察波形/日志**：`FullThrottle` 用例（[olo_base_cc_xn2n_tb.vhd:139-142](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd#L139-L142)）连续推 100 个递增值，再用 VUnit slave 逐个检查。重点看：快域每 4 拍可塞入若干数据、慢域每拍取走一个、`In_Ready` 在水位到 2 时短暂拉低。

**需要观察的现象**：快域连发、慢域慢取，但 100 个值全部按序、无丢失地出现在输出侧；中途 `In_Ready` 会周期性拉低（水位满），但很快恢复。

**预期结果**：所有 100 个 `check_axi_stream` 断言通过，测试报告 `pass`。这证明在 4:1 相位对齐前提下，`cc_xn2n` 仅靠两级寄存器 + 计数器即可正确跨域，**确实没有用到异步 FIFO**。

**前提条件小结**（务必满足，否则结果不成立）：

- 两个时钟来自**同一 PLL**、**相位对齐**；
- 频率为**整数倍**（本实践为 4:1）；
- 两时钟**频率不同**。

#### 4.4.5 小练习与答案

**练习 1**：`cc_xn2n` 为什么需要 `InDataReg` **和** `InDataRegLast` 两级，而 `cc_n2xn` 只需要一级？
**答案**：`cc_xn2n` 输入快、输出慢，两个慢时钟边沿之间可能积压 2 个数据，必须用两级缓冲暂存，并用水位计数器在「满」时停收；`cc_n2xn` 输入慢、输出快，输出总能即时取走，一级足够。

**练习 2**：把 `InCnt - OutCnt /= 2` 改成 `/= 3`（假设计数器够宽）会发生什么？
**答案**：缓冲深度会变成 3（需配套第三级寄存器才能不丢）。直接改而不加寄存器会导致数据丢失——这正说明「水位阈值」与「缓冲物理深度」必须严格匹配。

---

## 5. 综合实践

**任务**：为一条「传感器采样（快域）→ 下游处理（慢域）」的数据通路选型并验证跨时钟域方案。

设定：传感器跑 120 MHz，下游跑 40 MHz，二者由同一片 PLL 的两路相关输出产生（相位对齐，3:1）。

请完成：

1. **选型**：在 `cc_handshake`、`olo_base_fifo_async`、`cc_xn2n` 三者中选出最合适的一个，并说明理由（提示：是否相位对齐？是否需要 RAM？是否需要满吞吐？）。
2. **方向判断**：本场景应选 `n2xn` 还是 `xn2n`？说明你的判断依据（输入快还是慢）。
3. **验证设计**：参照 [olo_base_cc_xn2n_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_xn2n/olo_base_cc_xn2n_tb.vhd)，把 `ClockRatio_g` 设为 3、`InClk_Frequency_c` 设为 120.0e6，运行 `FullThrottle`，确认数据无损跨域。

**参考答案**：

1. **选 `cc_xn2n`**。理由：两时钟相位对齐、3:1 整数倍，满足 `cc_xn2n` 的全部前提；它能满吞吐、不占 RAM。`cc_handshake` 吞吐太低且无必要；`fifo_async` 虽也能用但白白消耗 RAM。
2. **选 `xn2n`**（快到慢）。传感器 120 MHz 是快域（输入），下游 40 MHz 是慢域（输出），输入频率是输出的 3 倍，符合 `xn2n` 的定义。
3. 仿真应能通过；此场景正是 `cc_xn2n` 的标准用法。精确波形**待本地验证**。

## 6. 本讲小结

- `cc_handshake` 用「前向 `cc_simple` + 反向 `cc_pulse`」拼出跨**异步**时钟域的 Valid/Ready 握手闭环，安全但低吞吐、无 RAM，是「不能用 RAM」时的退路。
- `cc_n2xn`/`cc_xn2n` 仅适用于**同源、相位对齐、整数倍且频率不同**的时钟对；换来的是无 RAM、可满吞吐、数据通路无位同步器。
- 相位对齐让「跨域直读对方寄存器」变得安全，这是后两个实体便宜的根本原因。
- `cc_n2xn`（慢→快）用**翻转协议 + 1 级寄存器**；`cc_xn2n`（快→慢）用**模 4 计数器水位 + 2 级寄存器**，水位到 2 即停收以防丢。
- 三者都遵循 u4-l1 的复位与约束约定：选型表的 `Async. Clocks` / `100% Perf.` / `No RAM` / `Reset Crossing` 列是决策核心。
- 选型口诀：要异步 + 反压 + 不能用 RAM → `cc_handshake`；要异步 + 高吞吐 → `fifo_async`；相位对齐整数倍 → `cc_n2xn`/`cc_xn2n`。

## 7. 下一步学习建议

- **横向比较**：回到 u3-l1（异步 FIFO）与 u4-l2（cc_simple/cc_status），把本讲的 `cc_handshake`/`cc_n2xn`/`cc_xn2n` 一并放进选型表，亲手为若干常见场景（异步等频、异步不等频、同源整数倍）选型并说明理由。
- **深入约束**：阅读 [doc/base/clock_crossing_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md) 的「Constraining」一节，为 `cc_handshake` 的两条异步路径写出 `set_max_delay -datapath_only` 约束；思考 `cc_n2xn`/`cc_xn2n` 因相位对齐，约束为何不同。
- **继续路线**：本讲是 u4 跨时钟域单元的最后一篇。下一单元（u5）进入时序、仲裁、CRC 等更上层的 base 构件，其中 `olo_base_flowctrl_handler` 等会反复用到本讲与 u2-l2 讲过的 AXI-S 反压概念，建议届时回头对照。
