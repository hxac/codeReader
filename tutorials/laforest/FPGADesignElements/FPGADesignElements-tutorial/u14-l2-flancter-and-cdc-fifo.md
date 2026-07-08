# Flancter 与 CDC FIFO

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **Weinstein Flancter** 是什么：它如何让你在「A 时钟域置位、B 时钟域清位」，而**不**用异步复位、也**不**依赖对方的时钟，并理解它为何适合「统计极快且异步的传感器脉冲」。
- 画出 **CDC FIFO Buffer** 的数据通路（双时钟双口 RAM + 两个地址计数器）与控制通路（满/空判定 + ready/valid 接口），并说清楚「满」和「空」地址相同时如何用一个**绕回位（wrap-around bit）**区分。
- 解释「多位指针跨时钟域」的根本困难：逐位同步会撕裂数值；讲明白**经典 Cummings 方案为何用 Gray 码指针**，并诚实指出——**本书这两个 FIFO 并不用 Gray 码**，而是改用「二进制指针 + 绕回位 + `CDC_Word_Synchronizer` 握手式整字同步」达到同样的安全效果。
- 读懂 **CDC FIFO Repacker** 如何用「最小公倍数深度 + head/tail 双计数器」把一种位宽的 ready/valid 流**无缝重新打包**成另一种位宽，并跨时钟域输出。

## 2. 前置知识

本讲是 CDC（Clock Domain Crossing，时钟域穿越）单元的第三篇，承接前两讲，并用到更早的握手与寄存器知识：

- **u13-l1（亚稳态与 CDC 基本理论）**：异步采样会撞出**亚稳态**，`CDC_Bit_Synchronizer` 用一串紧挨摆放的寄存器把传播概率压到指数级低；铁律是「**每次跨越只能同步一个位**」——并行同步多位无法保证同延迟，会撕裂数据。本讲的多位指针跨域正是这条铁律的最大考验。
- **u13-l2（复位同步与标志同步）**：`CDC_Flag_Bit` 用「两个翻转寄存器的**相对差**」表达标志值，天然避开 setup/hold 竞争。Flancter 是这套思想的「不对称加强版」——置位与清位发生在**不同**时钟域。
- **u14-l1（字同步与脉冲同步）**：`CDC_Word_Synchronizer` 在发送域**锁存**整字、整个握手往返期间字保持稳定，只把一个 toggle 形式的 `valid` 位过 CDC，从而把多位的「1.5× 频率代价」摊到整字上。本讲的 FIFO 直接复用它做指针跨域——这是本书**不采用 Gray 码**的关键原因。
- **u6-l1（Register 家族）** / **u8-l2（Counter_Binary）**：FIFO 的地址用 `Counter_Binary`（加法器 + 寄存器），绕回位用 `Register_Toggle`。
- **u9-1（ready/valid 握手）**：FIFO 两端都是 ready/valid 接口，握手完成 `(valid && ready)` 即一次插入/移除。

一个直觉式复习：同步器只认「电平」，且一个同步器通道一次只认一个位。那么问题来了——FIFO 要在两个时钟域之间共享「读写到了第几个地址」这个**多位**信息，还不能让数值在跨域时被撕裂，怎么办？经典答案是 Gray 码，本书的答案是「整字握手同步」。本讲就把这两条路放在一起讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Weinstein_Flancter.v` | Rob Weinstein 的 Flancter：在 set 域置位、reset 域清位，`bit_out` 异步于两域、须经位同步器后使用。 |
| `CDC_FIFO_Buffer.v` | 跨时钟域 FIFO 缓冲：双时钟双口 RAM + 二进制地址 + 绕回位 + 两个 `CDC_Word_Synchronizer` 做指针跨域；支持任意深度（非仅 2 的幂）与循环缓冲模式。源自 Cummings SNUG2002 论文。 |
| `CDC_FIFO_Repacker.v` | 跨时钟域 FIFO「重打包器」：输入/输出位宽可不同且不必互为整倍数；用最小公倍数做缓冲深度、head/tail 双计数器无缝重排位宽。指针跨域方案与 Buffer 完全一致。 |
| `CDC_Word_Synchronizer.v`（u14-l1 已学，本讲复用） | 两个 FIFO 的指针跨域都靠它：锁存整字、只同步一个 toggle，保证多位指针整体相干。 |
| 辅助原语（前序讲义已学） | `Register`、`Register_Toggle`、`Counter_Binary`、`RAM_Simple_Dual_Port_Dual_Clock`、`CDC_Bit_Synchronizer`；`.vh` 函数 `clog2`/`lcm`/`max`。 |

> **一个必须先讲清的诚实提醒**：本讲规格与练习题里提到「Gray 码指针」，这是**经典异步 FIFO（Cummings 论文）的标准做法**，也是这两个 FIFO 文件头部注释里引用的出处。但**本书的这两个 FIFO 实际并没有用 Gray 码**——它们保留二进制指针，加一个绕回位，再用 `CDC_Word_Synchronizer` 把整字相干地送过 CDC。所以本讲会先讲 Gray 码「为什么能解决问题」（理论），再讲本书「用什么替代了它」（源码），二者对照着学。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**Flancter**、**CDC FIFO Buffer**、**多位指针跨域（Gray 码 vs 本书方案）与 Repacker**。前两个是具体电路，第三个是贯穿两个 FIFO 的「多位指针怎么安全跨域」这个核心难题。

### 4.1 Weinstein Flancter：跨域事件标志

#### 4.1.1 概念说明

很多场景需要在 A 时钟域「记下一个事件发生了」（置位一个标志），而在 B 时钟域「确认并清掉它」。最朴素的办法是用一个带异步复位的触发器，但本书一贯**避免异步复位**（u3-l2 讲过：异步复位抑制寄存器重定时、放大复位树、引发仿真怪象）。

**Flancter**（Rob Weinstein 发明）给出另一种思路：用**两个普通寄存器**的**相对关系**来表达标志值，置位和清位各自只在自己那一个时钟域里发生，互不需要对方的时钟。

- **置位**（set 域）：让两个寄存器**不同** → 标志为 1。
- **清位**（reset 域）：让两个寄存器**相同** → 标志为 0。

这和 u13-l2 的 `CDC_Flag_Bit`（两个翻转寄存器的相对差）是同一族思想，区别在于 Flancter 的 set/reset 分别落在**不同**时钟域，因而能服务于「事件极快、读取方慢且异步」的场合。`reading.html` 的参考条目指出它特别适合「[counting very fast and asynchronous sensor pulses](./Flancter_fastevent_counter.pdf)」（统计极快且异步的传感器脉冲）。

> 单个 Flancter 本身只是「**至少发生了一次事件**」的 1 位标志。要**计数**（而不是只标记有无），通常把多个 Flancter 接成计数器的逐位（每个 Flancter 当一位，事件像进位一样逐级触发 set），各位输出**各自**过一位同步器后被慢速域一起读出。由于高位 Flancter 翻转频率逐级减半，跨域读出的多位置至多有一位在变化（这正是 Gray 码的相干性），慢速域因此能采到一个一致的计数值。**确切电路见 Flancter 应用笔记，本讲不展开。**

#### 4.1.2 核心流程

Flancter 内部只有两个 `Register` 和一个异或：

1. `register_set`（`clock_set` 域）：`clock_enable = bit_set`，数据输入恒为「`register_reset` 输出的取反」。
2. `register_reset`（`clock_reset` 域）：`clock_enable = bit_reset`，数据输入恒为「`register_set` 输出的原值」。
3. `bit_out = (register_set_data != register_reset_data)`：两者**不同**即置位。

时序追踪（设初值皆 0）：

| 操作（域） | register_set | register_reset | bit_out |
| --- | --- | --- | --- |
| 初值 | 0 | 0 | 0（相同） |
| 脉冲 `bit_set`（set 域） | 载入 ~0 = **1** | 0 | **1**（不同） |
| 脉冲 `bit_reset`（reset 域） | 1 | 载入 1 = **1** | **0**（相同） |

关键约束（源码「Operating Conditions」）：set 与 reset **绝不能重叠**，也不能落在对方的 setup/hold 窗口里。为保证这一点——**一旦置位就不能再次置位，直到被清位；一旦清位就不能再次清位，直到被置位**。这也是为什么单个 Flancter 是「事件发生过」的标志而非多事件计数器。

#### 4.1.3 源码精读

模块端口：set 域有 `clock_set/clear_set/bit_set`，reset 域有 `clock_reset/clear_reset/bit_reset`，输出 `bit_out`。

[Weinstein_Flancter.v:35-46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Weinstein_Flancter.v#L35-L46) — 端口声明。注意没有 `areset`，复位靠两域各自的 `clear_set`/`clear_reset`。

置位寄存器：数据输入是「reset 寄存器输出的取反」，使能是 `bit_set`：

[Weinstein_Flancter.v:59-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Weinstein_Flancter.v#L59-L71) — `register_set`：`data_in = ~register_reset_data`。置位就是让两者「不同」。

清位寄存器：数据输入是「set 寄存器输出的原值」，使能是 `bit_reset`：

[Weinstein_Flancter.v:76-88](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Weinstein_Flancter.v#L76-L88) — `register_reset`：`data_in = register_set_data`。清位就是让两者「相同」。

输出：两者不同即置位，是纯组合异或：

[Weinstein_Flancter.v:95-97](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Weinstein_Flancter.v#L95-L97) — `bit_out = register_set_data != register_reset_data`。

> **两个隐性 CDC 跨域**：`register_set` 的 `data_in` 读的是 reset 域的 `register_reset_data`，`register_reset` 的 `data_in` 读的是 set 域的 `register_set_data`——这是两条**异步组合跨域**，进入的是寄存器**数据端**（不是时钟端）。源码「Operating Conditions」与 `reading.html` 都强调：必须给 CAD 工具显式约束这两条线的 min/max 延迟，并用控制逻辑保证 set/reset 互斥。此外 `bit_out` 是两个不同域寄存器的组合异或，**异步于两域**，使用前必须过 [`CDC_Bit_Synchronizer`](./CDC_Bit_Synchronizer.html)。

#### 4.1.4 代码实践

**实践目标**：亲手验证「置位让两寄存器不同、清位让两寄存器相同」的相对差语义，并确认 `bit_out` 必须经同步器。

**操作步骤（源码阅读 + 思维实验）**：

1. 打开 [Weinstein_Flancter.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Weinstein_Flancter.v)，对照 4.1.2 的时序表，在纸上追踪「连续两次 `bit_set`、中间不 `bit_reset`」会发生什么。
2. 回答：第二次 `bit_set` 时，`register_set` 的 `data_in = ~register_reset_data` 是多少？`register_set` 会因此再次翻转吗？
3. 解释这为什么正好对应「Operating Conditions」里「一旦置位就不能再次置位直到清位」的约束。

**需要观察的现象**：第二次 `bit_set` 时 `register_reset_data` 仍为 0，故 `data_in` 仍为 1，`register_set` 维持 1，`bit_out` 不变。

**预期结果**：你会确认单个 Flancter 是「至少一次事件」的标志，而非累加计数器——这正是它要级联成计数器才能「统计快事件」的原因。

> 本地若有 Verilog 仿真器（如 iverilog/verilator），可写一个最小 testbench：两个不同周期时钟分别驱动 `bit_set`/`bit_reset`，观察 `bit_out`。若不具备，标注「待本地验证」即可，思维实验已足够达成目标。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Flancter 能在 set 域置位而「不需要看到 reset 域的时钟」？
**答**：置位只在自己的 `clock_set` 边沿把 `register_set` 更新为「`~register_reset_data`」；它读的是 reset 寄存器的**当前输出值**（一条数据跨域），并不需要 reset 域的时钟参与，也不复位任何触发器。

**练习 2**：源码为何强调 `bit_set` 与 `bit_reset` 绝不能重叠？
**答**：两者若重叠或落在对方 setup/hold 窗口内，等于在异步条件下同时改写两个有相对关系的寄存器，可能撞出亚稳态或得到一个既非「相同」也非「不同」的中间态，破坏标志语义。

**练习 3**：源码注释说「多数情况下用 `CDC_Flag_Bit` 更简单」。请说出 Flancter 相对 `CDC_Flag_Bit` 的独有能力。
**答**：Flancter 的置位与清位分处**不同**时钟域，且能在事件极快、读取域慢且异步时也不丢事件；`CDC_Flag_Bit` 的置位/清位通常在同一控制下、面向较温和的速率。

---

### 4.2 CDC FIFO Buffer：异步 FIFO 的数据通路与控制

#### 4.2.1 概念说明

一个跨时钟域 FIFO 要同时解决三件事：

1. **存储**：在两个异步时钟域之间缓存数据，平滑两侧速率失配，并切断输入到输出的组合路径（从而可流水化、改善时序与并发）。
2. **握手**：两端各自是 ready/valid 接口，互不知道对方的时钟频率与相位。
3. **空/满判定**：写指针在写域、读指针在读域，必须跨域比较才能知道「还有没有数据可读 / 还有没有空位可写」。

`CDC_FIFO_Buffer` 直接脱胎于 Clifford Cummings 的经典论文 *Simulation and Synthesis Techniques for Asynchronous FIFO Design* (SNUG 2002)（见文件头注释），但实现细节有本书自己的取舍（见 4.3）。它支持**任意正整数深度（非仅 2 的幂）**、可作**循环缓冲**、两时钟准同步时最小输入到输出延迟 7 拍。

#### 4.2.2 核心流程

整体是「数据通路 + 控制通路」分离（呼应 u10-l1 的设计方法）：

```
            input_clock 域                       output_clock 域
 input_data ──►[写口]──► 双时钟双口 RAM ──►[读口]──► output_data
                 ▲                                  ▲
          write_address                       read_address
        (Counter_Binary)                    (Counter_Binary)
                 │                                  │
        write_wrap_bit                       read_wrap_bit
       (Register_Toggle)                   (Register_Toggle)
                 │                                  │
                 └──── CDC_Word_Synchronizer ───────┘   (两个方向各一个)
```

1. **写**：输入握手完成 → 把 `input_data` 写入 RAM 的 `write_addr`，写地址 +1，到 `DEPTH-1` 回绕。
2. **读**：输出握手完成（或输出空闲而缓冲有数据）→ 从 `read_addr` 读出，读地址 +1，回绕。
3. **绕回位**：每当地址回绕到 0，翻转一个位。因为读写地址**永不相超**（one never passes the other），比较「地址 + 绕回位」即可区分空与满。
4. **跨域比较**：写地址 + 写绕回位 经 `CDC_Word_Synchronizer` 送到读域；读地址 + 读绕回位 经另一个送到写域。
5. **满/空**：
   - **空**：读地址 == 同步过来的写地址，**且**两个绕回位**相同**（读追上了写）。
   - **满**：同步过来的读地址 == 写地址，**且**两个绕回位**不同**（写绕了一圈追上读）。

> 这里的关键机关：**「空」和「满」的地址值是一模一样的**（读写指针重合），单看地址无法区分。多出来的那一位「绕回位」就是用来打破这个对称的——它记录「谁绕的圈数多」。

#### 4.2.3 源码精读

**存储体**是双时钟双口 RAM（写口在 `input_clock`、读口在 `output_clock`）：

[CDC_FIFO_Buffer.v:142-163](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Buffer.v#L142-L163) — `RAM_Simple_Dual_Port_Dual_Clock` 实例 `buffer`：写口接 `input_clock`/`input_data`，读口接 `output_clock`/`output_data`。注释指出两域时钟异步，故**不可能也无需写转发**（同址并发读写只可能发生在循环缓冲满时，且总是返回已存值）。

**地址计数器**：写地址与读地址各用一个 `Counter_Binary`，初值 `ADDR_ZERO`，每次 `+ADDR_ONE`，到 `DEPTH-1`（`ADDR_LAST`）回绕（`load` 优先于 `run`）：

[CDC_FIFO_Buffer.v:177-198](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Buffer.v#L177-L198) — `write_address` 计数器（运行于 `input_clock`）。读地址计数器结构相同，运行于 `output_clock`（203-224 行）。注意 `DEPTH` 是任意正整数，地址宽度 `ADDR_WIDTH = clog2(DEPTH)`。

**绕回位**：每当地址回绕到 0，翻转一个位。用 `Register_Toggle` 实现：

[CDC_FIFO_Buffer.v:244-278](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Buffer.v#L244-L278) — `write_wrap_around_bit`（写域）与 `read_wrap_around_bit`（读域）。源码注释写明：写地址若绕一圈从背后追上读地址 → 满（绕回位**不同**）；读地址若追上写地址 → 空（绕回位**相同**）。

**指针跨域**：两个 `CDC_Word_Synchronizer`，字宽 = `ADDR_WIDTH + 1`（即 `{绕回位, 地址}` 拼成一字）。`sending_valid` 恒接 1、`receiving_ready` 接回 `receiving_valid`，形成「完成一次就立刻开始下一次」的连续采样：

[CDC_FIFO_Buffer.v:309-333](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Buffer.v#L309-L333) — `write_to_read`：把 `{buffer_write_addr_wrap_around, buffer_write_addr}` 送到读域。另一个方向 `read_to_write` 结构相同（339-363 行）。注释点出：同步过来的地址会**滞后**几拍，但地址永不相超，故只造成「容量略需加深」的代价，不会出错。

**满/空判定**（纯组合）：

[CDC_FIFO_Buffer.v:379-382](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Buffer.v#L379-L382) — `stored_items_zero`（空：地址相等且绕回位相同）、`stored_items_max`（满：地址相等且绕回位不同）。

**输入接口**：不满（或循环缓冲模式）就 `input_ready`；握手完成即写入并推进写地址：

[CDC_FIFO_Buffer.v:397-405](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Buffer.v#L397-L405) — `input_ready = !stored_items_max || CIRCULAR_BUFFER`；`insert = valid && ready`；到 `ADDR_LAST` 则 `load` 回绕并翻转绕回位。

#### 4.2.4 代码实践

**实践目标**：亲手从源码确认「空/满地址相同、靠绕回位区分」，并理解指针跨域为何允许滞后。

**操作步骤（源码阅读型实践）**：

1. 打开 [CDC_FIFO_Buffer.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Buffer.v)，定位 4.2.3 引用的 5 处代码点。
2. 假设 `DEPTH=4`，画一张表：连续 4 次写入（不满）期间，`write_addr`、`write_wrap_bit`、`read_addr`、`read_wrap_bit` 的变化。确认第 4 次写入后地址回到 0、绕回位翻为 1，此刻读侧仍全 0 → 满足「满」条件（地址都 = 0，但绕回位 1≠0）。
3. 接着读出 1 次：`read_addr` 变 1。再读到地址绕回需要 4 次——确认读写地址**永不相超**。

**需要观察的现象**：地址相等时，绕回位相同=空、不同=满；同步指针滞后只影响「是否多留一两格」，不影响正确性。

**预期结果**：你能向别人讲清「为什么需要那一位绕回位」，以及「为什么指针可以滞后于真实值」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 RAM 不需要写转发逻辑？
**答**：两端口时钟异步，无法做同址写转发；而正常模式下不会同址并发读写，循环缓冲模式下同址并发（满时）总是返回已存值，故无需转发。

**练习 2**：两个接口为什么必须**一起**复位？
**答**：异步复位只复位一侧会破坏读写地址的相对关系，导致重复读写或丢数据。正确做法是选一侧为主复位，用 `Reset_Synchronizer` 同步到另一侧，且两侧都退出复位后才开始工作。

**练习 3**：循环缓冲模式（`CIRCULAR_BUFFER != 0`）下 `input_ready` 为什么可以恒与满状态无关？
**答**：循环缓冲总是允许写入，满时覆盖最旧数据；由于 `input_ready` 不再依赖空/满状态，输入输出握手可在同拍完成，不再被迫交替。

---

### 4.3 多位指针跨域：Gray 码 vs 本书的「绕回位 + 整字同步」& Repacker

#### 4.3.1 概念说明：经典方案为什么用 Gray 码

FIFO 的核心难题：**读写指针是多位二进制数，必须跨异步时钟域被对方比较**。u13-1 的铁律说「每次跨越只能同步一个位」。如果直接把多位地址**逐位**各接一个位同步器，由于各同步器的延迟不同（1–3 拍漂移），采样那一拍可能抓到一个**根本没出现过的中间值**。

> 例子：地址从 `011`（3）变 `100`（4），三位同时翻转。若高位同步得快、低位同步得慢，对方可能瞬时看到 `111`（7）——这是真实序列里**从不存在**的值，会导致错误的空/满判断。

**经典 Cummings 方案的解药是 Gray 码指针**：把二进制指针转成 Gray 码再逐位同步。Gray 码相邻两值**只有一位不同**，所以任意一拍采样，要么是旧值、要么是新值，**绝不会出现第三种**——即便有同步延迟不一致，也至多采到「上一拍」或「这一拍」，地址单调推进、永不相超，安全性得到保证。空/满判定则用「Gray 指针多保留一位 MSB」来区分两个重合状态（和本书的「绕回位」目的相同）。本书也确实提供了这套转换原语 [`Binary_to_Gray_Reflected.v`](./Binary_to_Gray_Reflected.html) / [`Gray_to_Binary_Reflected.v`](./Gray_to_Binary_Reflected.html)。

#### 4.3.2 概念说明：本书为什么不用 Gray 码

**但本书的这两个 FIFO 没有走 Gray 码这条路。** 它们的做法是：

1. 指针保持**普通二进制**，不转 Gray。
2. 额外拼一位**绕回位**（4.2 已述），凑成 `{绕回位, 二进制地址}` 一字。
3. 把这一整字交给 `CDC_Word_Synchronizer`（u14-l1）：它在发送域**锁存整字**，整个 2 相握手往返期间整字**保持稳定**，只把一个 toggle 形式的 `valid` 位过 CDC；接收域确认后才允许下一字。于是多位地址**作为一个整体**相干地到达对方——不会有任何「撕裂的中间值」。

换句话说：**Gray 码靠「每次只变一位」让逐位同步安全；本书靠「整字锁存 + 单 toggle 握手」让整字同步安全。** 两条路殊途同归，都满足「采到的永远是某个真实存在过的指针值」。本书的代价是：整字握手往返有延迟（准同步时单次 5–8 拍），指针同步会**滞后**几拍；但只要读写地址永不相超，滞后只意味着「FIFO 要稍微深一点才能达到峰值容量」，注释明确指出这点。

#### 4.3.3 源码精读：Repacker 复用同一套指针方案，并重组位宽

`CDC_FIFO_Repacker` 解决一个更难的问题：输入是一种位宽（如 8 位）的 ready/valid 流，输出要**无缝重打包**成另一种位宽（如 12 位），且跨时钟域。它的指针跨域与 `CDC_FIFO_Buffer` **完全相同**（二进制 + 绕回位 + `CDC_Word_Synchronizer`），独特之处在数据通路的**位宽重组**。

**缓冲深度用最小公倍数**：要同时装下整数个输入字和整数个输出字，最小深度是两者位宽的最小公倍数。用 LCM 就退化成一个「输入输出位宽不同」的普通 FIFO，省掉了复杂的预排程：

[CDC_FIFO_Repacker.v:92-127](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L92-L127) — `BUFFER_DEPTH_MIN = lcm(WORD_WIDTH_OUTPUT, WORD_WIDTH_INPUT)`。但 LCM 深度不足以保证满吞吐：`CDC_Word_Synchronizer` 最坏 8 拍延迟，需缓冲能容纳「两倍以上（17）」个最宽字以避免停顿，故引入 `BUFFER_DEPTH_MULTIPLIER = (17 / ITEM_COUNT_MIN) + 1`（`+1` 是整数除法的安全裕量），最终 `BUFFER_DEPTH = LCM × 倍数`。

设输入位宽 \(w_i\)、输出位宽 \(w_o\)，则：

\[
\text{BUFFER\_DEPTH\_MIN} = \mathrm{lcm}(w_o, w_i), \qquad
\text{ITEM\_COUNT\_MIN} = \frac{\mathrm{lcm}(w_o, w_i)}{\max(w_o, w_i)}
\]

\[
\text{BUFFER\_DEPTH} = \mathrm{lcm}(w_o, w_i) \times \left( \left\lfloor \frac{17}{\text{ITEM\_COUNT\_MIN}} \right\rfloor + 1 \right)
\]

**存储体用寄存器而非 BRAM**：因为要按「任意位宽子集」无间隙地读写，BRAM 只支持固定有限字长，无法胜任：

[CDC_FIFO_Repacker.v:163](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L163) — `reg [BUFFER_DEPTH-1:0] buffer`，按位构成。

**head/tail 双计数器**：每侧用**两个**计数器（头 head、尾 tail），尾从 0 起、头从 `位宽-1` 起，每次 `+位宽` 并在 `ADDR_LAST` 回绕到起点。因深度是 LCM，计数器总能整字索引、无残料：

[CDC_FIFO_Repacker.v:193-239](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L193-L239) — 写侧 `write_address_tail`（初值 `ADDR_ZERO`）与 `write_address_head`（初值 `WORD_WIDTH_INPUT-1`），增量均为 `WORD_WIDTH_INPUT`。读侧对称（248-294 行，增量 `WORD_WIDTH_OUTPUT`）。用两个计数器（而非计数器+加法器）是为了**并发**算出下一头尾。

**指针跨域**：与 Buffer 一模一样，两个 `CDC_Word_Synchronizer`，字宽 `1 + ADDR_WIDTH`，传 `{绕回位, tail 地址}`：

[CDC_FIFO_Repacker.v:382-406](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L382-L406) — `write_to_read` 把 `{buffer_write_addr_wrap_around, buffer_write_addr_tail}` 送读域；`read_to_write` 反向（412-436 行）。

**满/空（此处称 cannot_read/cannot_write）**：用 head/tail 的范围比较判断「是否有足够空间写一整字 / 是否有足够数据读一整字」：

[CDC_FIFO_Repacker.v:453-456](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L453-L456) — `cannot_read`（读头 > 同步写尾 且 绕回位相同）、`cannot_write`（同步读尾 < 写头 且 绕回位不同）。

**无间隙位宽读写**：用变址部分位选 `buffer[addr +: WIDTH]` 精确写入/读出所需位宽。源码注释说明这里「罕见地在 generate 外用了 `if`」，因为要表达「对寄存器的**一部分**做时钟使能」而非数据 mux：

[CDC_FIFO_Repacker.v:479-483](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L479-L483) — 插入时 `buffer[buffer_write_addr_tail +: WORD_WIDTH_INPUT] <= input_data`；读出侧 `output_data <= buffer[buffer_read_addr_tail +: WORD_WIDTH_OUTPUT]`（514-518 行）。

#### 4.3.4 代码实践

**实践目标**：把「Gray 码方案」与「本书绕回位 + 整字同步方案」对照清楚，并亲手算一次 Repacker 的缓冲深度。

**操作步骤**：

1. **对照两种多位指针跨域方案**（填表）：

   | 维度 | 经典 Cummings（Gray 码） | 本书（绕回位 + `CDC_Word_Synchronizer`） |
   | --- | --- | --- |
   | 指针编码 | 二进制 → Gray 码 | 保持二进制 |
   | 跨域方式 | 每位各一个位同步器 | 整字锁存，只同步一个 toggle |
   | 保证相干的原理 | 相邻值只变一位 | 整字在握手往返期稳定 |
   | 空/满区分 | 多一位 Gray MSB | 一位绕回位 |
   | 延迟代价 | 位同步 1–3 拍 | 整字握手 5–8 拍（指针滞后更多） |

2. **回答练习题里的「为什么经典方案用 Gray 码、本书却不用」**——用上表的两行「保证相干的原理」作答。
3. **算一次 Repacker 深度**：设 `WORD_WIDTH_INPUT = 8`、`WORD_WIDTH_OUTPUT = 12`。求 LCM、`ITEM_COUNT_MIN`、`BUFFER_DEPTH`。

**需要观察的现象**：LCM(12,8)=24；`max=12`，故 `ITEM_COUNT_MIN = 24/12 = 2`；`BUFFER_DEPTH_MULTIPLIER = (17/2)+1 = 8+1 = 9`；`BUFFER_DEPTH = 24 × 9 = 216`。

**预期结果**：你算出缓冲深度 216 位，并理解「+1 裕量」与「17 来自 2×最坏 8 拍 CDC 延迟」的含义。若你的整数除法约定与我不同（截断 vs 四舍五入），结果可能略有差异——源码注释正是因此才加 `+1` 裕量，请标注「待本地验证确切值」。

#### 4.3.5 小练习与答案

**练习 1**：练习题——CDC FIFO 为什么（在经典方案里）用 Gray 码做读写指针跨域？本书的这两个 FIFO 实际用了什么替代它？
**答**：经典方案逐位同步多位指针会因各同步器延迟不同而采到「撕裂的中间值」；Gray 码相邻值只变一位，保证采到的永远是某个真实存在过的值。本书的 `CDC_FIFO_Buffer`/`CDC_FIFO_Repacker` 没有用 Gray 码，而是保留二进制指针 + 一位绕回位，把 `{绕回位, 地址}` 整字交给 `CDC_Word_Synchronizer`，靠「整字锁存 + 单 toggle 握手」保证相干——等价的安全、不同的实现。

**练习 2**：Repacker 为什么用 LCM 作缓冲深度，而不是用 2 的幂？
**答**：LCM 是「同时装下整数个输入字和整数个输出字」的最小深度，使缓冲退化为一个位宽不同的普通 FIFO，省掉了复杂的预排程和宽 mux；代价是存储可能略大，换来设计的极大简化（只需计数）。

**练习 3**：Repacker 的存储体为什么用寄存器而不用 BRAM？
**答**：要按任意位宽子集无间隙读写（输入/输出位宽可不等且不必互为整倍数），BRAM 只支持固定字长无法胜任；故用寄存器堆 + 变址部分位选 `buffer[addr +: WIDTH]` 精确切片。

---

## 5. 综合实践

**任务**：为一个「快速异步传感器 → 慢速处理域」的数据通路，设计一个跨域方案，并把本讲三块知识串起来。

设定：传感器在「传感器时钟域」产出位宽 10 位、速率很快的数据脉冲；处理域时钟慢得多，且位宽需求是 16 位。要求不丢脉冲、并把数据跨域重打包成 16 位。

**请完成**：

1. **事件不丢**：说明为什么不能直接逐位同步传感器脉冲，并指出若只关心「事件有无」可用 Flancter（或更简单的 `CDC_Flag_Bit`）做标志——讲清 Flancter「set 域置位、reset 域清位」的相对差原理（参考 [Weinstein_Flancter.v:59-97](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Weinstein_Flancter.v#L59-L97)）。
2. **数据不丢且重打包**：说明应选用 `CDC_FIFO_Repacker`（`WORD_WIDTH_INPUT=10`、`WORD_WIDTH_OUTPUT=16`），算出 LCM(16,10)=80、`max=16`、`ITEM_COUNT_MIN=5`、`BUFFER_DEPTH_MULTIPLIER=(17/5)+1=3+1=4`、`BUFFER_DEPTH=80×4=320`（标注「待本地验证」）。
3. **指针安全**：解释这个 Repacker 内部为何不需要 Gray 码——它的指针用 `{绕回位, 二进制地址}` 经 `CDC_Word_Synchronizer` 整字相干跨域（参考 [CDC_FIFO_Repacker.v:382-406](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L382-L406)）；并与经典 Cummings Gray 码方案对比，说出两者「保证采样到真实指针值」的不同原理。
4. **复位纪律**：写出两侧必须一起复位、且都用 `Reset_Synchronizer` 把主复位同步到对方域的要求（参考 [CDC_FIFO_Repacker.v:46-59](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_FIFO_Repacker.v#L46-L59)）。

> 这个综合实践不需要你写可综合代码，而是检验你能否在真实约束下**选对构件、讲清原理、算对参数**。把答案写成一段简短设计说明即可。

## 6. 本讲小结

- **Flancter** 用两个寄存器的**相对差**表达标志：set 域置位让两者「不同」、reset 域清位让两者「相同」，无需异步复位、无需对方时钟；`bit_out` 异步于两域，使用前必须过位同步器。单个 Flancter 是「事件有无」标志，级联方可「计数快事件」。
- **CDC FIFO Buffer** = 双时钟双口 RAM + 两个 `Counter_Binary` 地址 + 两个 `Register_Toggle` 绕回位 + 两个 `CDC_Word_Synchronizer` 做指针跨域；支持任意深度与循环缓冲，源自 Cummings 论文。
- **空/满判定**的核心机关：空和满的地址值相同，靠**绕回位**区分（相同=空、不同=满）；读写地址**永不相超**，故指针同步滞后只影响峰值容量、不影响正确性。
- **多位指针跨域**有两条等价安全路径：经典 **Gray 码**（每位一个同步器，相邻值只变一位）vs 本书 **「二进制 + 绕回位 + 整字握手同步」**（整字锁存、只同步一个 toggle）。**本书这两个 FIFO 走的是后者，不用 Gray 码**——这是规格里「Gray 码」提法的实际出处与诚实订正。
- **CDC FIFO Repacker** 用 **LCM 缓冲深度** + **head/tail 双计数器** + **变址部分位选**，把一种位宽的 ready/valid 流无缝重打包成另一种位宽并跨域；存储用寄存器（非 BRAM）以支持任意位宽子集。
- 三个模块共享同一哲学：**CDC 正确性靠结构纪律（互斥、整字相干、一起复位）保证，而非靠仿真**。

## 7. 下一步学习建议

- **下一讲 u15-l1（脉冲生成、锁存与分频）**：从 CDC 转向脉冲处理原语——`Pulse_Latch`/`Pulse_Generator`/`Pulse_Divider`，其中 `Pulse_Generator` 正是 `CDC_Word_Synchronizer` 把 toggle 还原成脉冲所用、也是 Flancter 计数器读侧会用到的边沿检测件。
- **回顾 u14-l1**：本讲的 `CDC_Word_Synchronizer` 是 u14-l1 的主角，强烈建议对照重读其「整字锁存 + 单 toggle」机制，巩固「本书为何不用 Gray 码」的结论。
- **延伸阅读（项目内）**：若你想看本书**真正**的 Gray 码实现，读 [`Binary_to_Gray_Reflected.v`](./Binary_to_Gray_Reflected.html) 与 [`Gray_to_Binary_Reflected.v`](./Gray_to_Binary_Reflected.html)，以及 `tests/Counter_Gray_Tb.py`/`.sv` 的 Gray 计数器测试台；再对照 Cummings 论文（`CDC_FIFO_Buffer.v` 头部链接）体会两种指针跨域方案的取舍。
- **延伸阅读（项目外）**：`reading.html` 的 Flancter 条目与 `Flancter_fastevent_counter.pdf`（统计快事件的完整电路）、`Flancter_App_Note.pdf`（原始应用笔记）。
