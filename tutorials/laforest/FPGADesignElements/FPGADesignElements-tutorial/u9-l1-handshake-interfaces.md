# 握手接口与握手过程

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 ready/valid 握手接口的三个信号（`valid`、`ready`、`data`）各自的方向与含义，并区分 **source（源）** 接口与 **destination（目的）** 接口。
- 用一句话给出「握手完成」的判定条件，并能在一个具体时序图里指出握手发生在哪个时钟周期。
- 解释三条防止握手失败的铁律（valid 必须保持、source 不能等 ready、接口内不得有组合环路），并说明它们各自防的是「死锁」还是「活锁」。
- 说清作者「AXI 并不根本」的观点：ready/valid 才是底层机制，AXI 只是在它之上加了一堆假设；再往下，更根本的东西是「会合同步（rendez-vous）」。

本讲是「Ready/Valid 握手原理」单元的第一篇，全部概念来自全书的握手规范正文 `handshake.html`。下一讲 `u9-l2` 会把这些规则落到真实 Verilog 模块 `Pipeline_Skid_Buffer.v` 上。

## 2. 前置知识

本讲几乎不依赖具体硬件描述语言细节，但有几个概念需要先对齐：

- **时钟上升沿（rising edge of the clock）**：本书所有握手信号都「同步到时钟上升沿」——也就是说，我们只在每个时钟上升沿去观察/采样这些信号，信号也只在上升沿之后才可能改变。一个「时钟周期（cycle）」就是相邻两个上升沿之间的一段时间。
- **组合逻辑 vs 时序逻辑**：组合逻辑的输出在输入变化后「立刻」变化（没有时钟参与）；时序逻辑的输出只在时钟沿上更新。本讲会反复用到「组合通路（combinational path）」这个词，意思是「信号不经寄存器、一气呵成从输入传到输出」。
- **握手（handshake）**：你可以把它想成两个人传递物品——给的人要先把东西举起来表示「我这里有」，接的人要示意「我现在能接」，两人同时满足，这次传递才算完成。本讲就是把这套日常动作翻译成三个电信号。
- **模块与接口**：在 `u4-l1` 里我们讲过，一个子系统应按职责拆成「处理 / 控制 / 接口」三类模块。本讲专门讲「接口」这一类——也就是模块之间怎么连接、怎么传递数据与控制。ready/valid 握手就是本书选定的接口通用语。

> 提示：本讲引用的全部是概念章 `handshake.html`（由 Verilog 注释体系之外、手写的 HTML 正文），它本身不含可综合模块，但其中给出的 `handshake_complete` 表达式是货真价实的 Verilog 代码片段，后续每个握手模块都会用到它。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它承载了全书所有弹性流水线模块的共同约定：

| 文件 | 作用 |
|------|------|
| `handshake.html` | 全书「Ready/Valid 握手与会同步规则」的概念正文。定义了 source/destination 接口、握手过程、死锁/活锁规避、内部状态更新规则，并讨论了握手之下的「会合同步」本质与「AXI 并不根本」的立场。 |

正文中出现的 `Pipeline_Skid_Buffer`、`Pipeline_Half_Buffer`、`Pulse_Generator`、`Pipeline_Synchronizer_Lazy` 等都是仓库里真实存在的模块，本讲只把它们作为「应用举例」点名，具体源码留到 `u9-l2` 及更后续的讲义精读。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**接口定义**、**握手过程**、**AXI 不根本**。

### 4.1 接口定义

#### 4.1.1 概念说明

要连接两个模块，最朴素的想法是「拉一根线把数据送过去」。但发送方怎么知道对方这会儿能不能收？接收方又怎么知道线上的数据是不是有效的、是不是该看？如果两边各干各的，数据就会丢、会重复、会错位。

ready/valid 握手用一个极简办法解决这个问题：**给数据再配两个一位的控制信号**。

- `valid`（有效）：由发送方驱动，为 1 表示「我此刻线上有有效数据」。
- `ready`（就绪）：由接收方驱动，为 1 表示「我此刻能再收一个数据」。
- `data`（数据）：宽度任意，由发送方驱动，承载真正的有效载荷。

本书把这套三信号封装成两种「互补」的接口视角：

- **source（源）接口**：拥有并输出 `valid` 和 `data`，接收 `ready`。它是数据的出方。
- **destination（目的）接口**：输出 `ready`，接收 `valid` 和 `data`。它是数据的入方。

一个连接**永远从 source 指向 destination**，别的接法都不成立。这就像水龙头（source）只能往水桶（destination）里灌，不能反过来。

#### 4.1.2 核心流程

接口定义的关键不是「有三个信号」，而是「三个信号各自由谁驱动、朝哪个方向走、以及一个硬性约束」。

信号的驱动与方向可以列成下表：

| 信号 | 位宽 | 驱动方 | 方向 | 含义 |
|------|------|--------|------|------|
| `valid` | 1 | source | source → destination | 「我有有效数据」 |
| `data` | 任意 | source | source → destination | 有效载荷本身 |
| `ready` | 1 | destination | destination → source | 「我能再收一个」 |

三个信号**全部同步到时钟上升沿**。

此外有一条关系到能否工作的硬约束——**接口内部不得有组合环路**：

- 在 source 接口里，不得存在从输入 `ready` 到输出 `valid` 的组合通路。
- 在 destination 接口里，不得存在从输入 `valid` 到输出 `ready` 的组合通路。

为什么？因为一旦你把一个 source 接到一个 destination，若两边都各有这样一条组合通路，就会拼出一个**组合环**：

```
valid ──(dest 内部组合)──► ready ──(source 内部组合)──► valid ...
```

组合环在数字电路里是非法的（无法稳定求值）。即便只有一边有这种通路、侥幸没成环，剩余的组合路径也会在两个接口之间来回穿一趟，拖长关键路径、压低时钟频率、增加布局布线难度——作者明确表态：**为了省这一拍延迟而牺牲全设计的性能，不值**。

> 注意：正文在「Loops」一节末尾给了一条**实践修正（ADDENDUM）**：理论上述组合通路都该避免，但实际工程里，允许 valid 与 ready 之间存在组合通路有时是划算的（到处加缓冲会让设计臃肿）。一旦你选择走组合通路，就必须格外小心组合环与下面 4.2 要讲的死锁/活锁问题。这是一个「可违反但要自负其责」的约定。

#### 4.1.3 源码精读

接口的三信号定义在正文的「Interfaces」一节，措辞很干脆：

> [handshake.html:L43-L49](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L43-L49) —— 定义两种互补接口（source/destination），各有 `valid`/`ready`/`data` 三个信号，`ready`/`valid` 为一位、`data` 任意宽，且**全部同步到时钟上升沿**。

两种接口各自的驱动职责用两条列表说清：

> [handshake.html:L51-L54](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L51-L54) —— source 输出 `valid` 与 `data`、接收 `ready`；destination 输出 `ready`、接收 `valid` 与 `data`。

以及那条连接方向铁律：

> [handshake.html:L56-L57](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L56-L57) —— 连接永远从 source 到 destination，别无他法。

关于组合环路约束：

> [handshake.html:L61-L63](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L61-L63) —— source 接口内不得有 `ready → valid` 的组合通路，destination 接口内不得有 `valid → ready` 的组合通路，否则相连即成组合环。

#### 4.1.4 代码实践

**实践目标**：把 source/destination 接口的信号方向画成一张图，亲手确认 `valid`/`data` 与 `ready` 走向相反。

**操作步骤**：

1. 在纸上（或文本编辑器里）画出两个方框，左边标 `SOURCE`，右边标 `DESTINATION`。
2. 从 SOURCE 到 DESTINATION 画两条向右的箭头，分别标 `valid` 和 `data`。
3. 从 DESTINATION 到 SOURCE 画一条向左的箭头，标 `ready`。
4. 在三个箭头旁各注明驱动方（`valid`/`data` 由 source 驱动，`ready` 由 destination 驱动）。

参考画法（ASCII 示意图，本讲义自制）：

```
            ┌─────── valid ───────┐
            │                     ▼
      ┌───────────┐          ┌──────────────┐
      │  SOURCE   │── data ─►│ DESTINATION  │
      │  (源接口) │          │  (目的接口)  │
      └───────────┘◄─ ready ─└──────────────┘
                       ▲
            ┌──────────┘
```

**需要观察的现象**：`valid` 与 `data` 同向（都向右），`ready` 反向（向左）。这三根线没有第四种走向。

**预期结果**：你能从图上一眼看出「source 只管往外送 valid 和 data，并听 ready 的回话」。若你画出了 `ready` 也朝右、或 `valid` 朝左，那就是画错了——连接必须从 source 到 destination。

#### 4.1.5 小练习与答案

**练习 1**：如果 source 接口内部存在一条 `ready → valid` 的组合通路，把它接到一个「干净」的 destination（内部无 `valid → ready` 通路）上，会形成组合环吗？会有什么后果？

**参考答案**：不会形成组合环（环需要两边都贡献一段），但会形成一条从 source 出发、经 destination 又回到 source 的**往返组合路径**，拖长关键路径，可能压低设计能跑到的最高时钟频率，并让布局布线更困难。这正是正文说「不值」的代价。

**练习 2**：为什么 `data` 的宽度可以是「任意」的，而 `valid`/`ready` 必须是 1 位？

**参考答案**：`valid`/`ready` 只回答「有没有 / 能不能」这种是非题，1 位足够；`data` 承载实际有效载荷，宽度由具体应用（8 位、32 位、一整个包都可）决定，所以接口定义把它留作「任意宽度」。

### 4.2 握手过程

#### 4.2.1 概念说明

有了接口，下一步是「握手的动作」到底怎么发生。核心问题只有一个：**这一次数据传递，到底算不算数？在哪个周期算数？**

答案极其简洁：当 `valid` 与 `ready` **在同一个时钟周期的上升沿同时为 1** 时，这一次握手就算完成，destination 在**同一周期**接收数据。这就是全书反复使用的「握手完成」判定。

由此衍生出三个绕不开的子问题，作者都给了明确规矩：

1. **谁先动手？** source 有数据就该尽快拉高并稳住 `valid`；destination 能收就该尽快拉高 `ready`。最理想的情况是 destination 在 source 拉 `valid` 之前就把 `ready` 拉好，这样握手一拍就完成。
2. **能不能中途反悔？** destination 可以任意时刻拉高/拉低 `ready`；但 source 一旦拉高 `valid`，就**必须保持到握手完成**，中途不得撤回。
3. **能不能互相等？** source **不得**等 destination 拉了 `ready` 才去拉 `valid`；destination **可以**等 source 拉了 `valid` 才去拉 `ready`。这个不对称是为了防死锁。

这三条规矩分别防的是「最坏情况」：第 2 条防**活锁（livelock）**——两边都在动，却始终碰不上面；第 3 条防**死锁（deadlock）**——两边都死等对方先动，永远等下去。

#### 4.2.2 核心流程

握手完成的判定可以写成一个一位的布尔量：

\[ \text{handshake\_complete} \;=\; (\text{ready} \,==\, 1'b1) \;\land\; (\text{valid} \,==\, 1'b1) \]

只有当它在一个时钟周期里为真，那个周期才算「握手发生」，destination 才接收数据，模块内部相关状态才允许变化。

一次完整握手在时间上可能有两种形态：

**形态 A：单周期握手（最优）**

destination 提前把 `ready` 拉好，source 一拉 `valid` 当拍就同时为 1，握手当拍完成。

| 时钟周期 | `valid` | `ready` | `data` | `handshake_complete` | 说明 |
|----------|---------|---------|--------|----------------------|------|
| 1 | 1 | 1 | D0 | 1 | 当拍完成，destination 收下 D0 |
| 2 | 0 | 1 | — | 0 | source 无更多数据，撤 `valid` |

**形态 B：两周期握手**

source 先拉 `valid`（destination 还没准备好，`ready=0`），下一拍 destination 才拉 `ready`，握手在第二拍完成。注意 source 在第一拍必须**保持** `valid=1` 与 `data=D0` 不变。

| 时钟周期 | `valid` | `ready` | `data` | `handshake_complete` | 说明 |
|----------|---------|---------|--------|----------------------|------|
| 1 | 1 | 0 | D0 | 0 | source 已就绪，destination 未就绪，未完成 |
| 2 | 1 | 1 | D0 | 1 | 第二拍完成，destination 收下 D0 |
| 3 | 0 | 1 | — | 0 | 撤 `valid` |

形态 B 比 A 多花一拍，但它是合法的；只要 source 在第 1 拍稳住 `valid`，就不会丢数据。

防死锁与活锁的两条铁律可以对照记忆：

- **防死锁（不对称等待）**：source 不得等 `ready` 才拉 `valid`；destination 可以等 `valid` 才拉 `ready`。理由：若两边都「等对方先动」，就僵住了。允许 destination 等 valid 有实际用途——比如仲裁器要从多个 source 里挑一个完成握手。
- **防活锁（valid 必须保持）**：source 拉 `valid` 后必须保持到 `handshake_complete` 为真。理由：若 source 只是「瞬时」闪一下 `valid`、destination 也只是「瞬时」闪一下 `ready`，两者可能永远对不上，于是两边都在动、握手却永远完不成。

还有一个关于**内部状态**的强约束：模块里凡会影响 source/destination 接口的内部状态，**只能在握手完成的那一拍改变**，否则会丢数据。正文举的例子是「给包内字计数」：如果你在计数到 0 的当拍就立刻改状态进入下一个包，偏偏同一拍 destination 的 `ready` 掉了（这次没握成手），那最后一个字就丢了，还连带搞坏后面的包。正解是用 `handshake_complete` 把所有这类状态更新门控（gate）住。

#### 4.2.3 源码精读

握手过程的定义集中在「Handshake Procedure」一节：

> [handshake.html:L85-L89](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L85-L89) —— source 有数据时拉高**并稳住** `valid`，destination 能收时才拉 `ready`；**两者同高即握手完成**，destination 在同周期接收数据。

关于「提前就绪、尽快拉 valid」以缩短到单周期握手的建议：

> [handshake.html:L91-L95](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L91-L95) —— destination 尽量在 source 拉 valid 前就拉好 ready，source 尽量有数据就拉 valid，二者都能把握手缩短到一拍。

死锁与活锁的两条铁律（这是本模块最容易考、也最容易写错的地方）：

> [handshake.html:L151-L157](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L151-L157) —— 防死锁：source 不得等 ready 才拉 valid（destination 可以等 valid 才拉 ready），这种等待会把握手拖到两拍、在第二拍完成，但用于「多 source 选一」的仲裁场景。

> [handshake.html:L159-L163](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L159-L163) —— 防活锁：source 一旦拉高 valid，**必须保持到握手完成**，否则可能出现「两边都在闪、却永远碰不上」的活锁。

最后是那段货真价实的 Verilog 片段——它给出了 `handshake_complete` 的标准写法，并要求用它门控所有影响接口的内部状态跳变：

> [handshake.html:L194-L198](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L194-L198) —— 用一个组合块定义 `handshake_complete = (ready == 1'b1) && (valid == 1'b1);`。

> [handshake.html:L179-L182](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L179-L182) —— 任何影响接口的内部状态，只能在 `handshake_complete` 为真的那一拍改变，否则会丢数据。

#### 4.2.4 代码实践

**实践目标**：给定一组 `valid`/`ready` 的时间序列，亲手判定握手发生在哪一拍，并验证「source 必须保持 valid」这条规则。

**操作步骤**：

1. 抄下下面这张「待分析」时序表（本讲义自制的练习数据）：

   | 时钟周期 | `valid` | `ready` | `data` |
   |----------|---------|---------|--------|
   | 1 | 1 | 0 | D0 |
   | 2 | 1 | 0 | D0 |
   | 3 | 1 | 1 | D0 |
   | 4 | 1 | 1 | D1 |
   | 5 | 0 | 1 | —  |

2. 对每一拍套用 \( \text{handshake\_complete} = (\text{ready}\,\&\,\text{valid}) \)，标出哪些拍握手完成。
3. 回答：D0 在第几拍被接收？D1 在第几拍被接收？共完成了几次握手？
4. 检查：source 在第 1、2 拍 `ready=0` 时是否保持了 `valid=1` 与 `data=D0` 不变？若 source 在第 2 拍就撤了 `valid`，会发生什么？

**需要观察的现象**：第 1、2 拍虽有 `valid` 但 `ready=0`，握手不成；第 3 拍两者同高，D0 被收；第 4 拍两者同高，D1 被收；第 5 拍 `valid=0`，无握手。

**预期结果**：D0 在第 3 拍被接收，D1 在第 4 拍被接收，共完成 2 次握手。若 source 在第 2 拍就撤了 `valid`，则 D0 在前两拍从未被接收过、随后 source 又转向 D1，D0 就**永久丢失**了——这正是「valid 必须保持到握手完成」要防的后果。

> 待本地验证：若你想在仿真里确认，可以把这张表喂给一个只含 `handshake_complete` 组合块的最小 testbench，在每拍打印 `(ready & valid)` 的值，对照上面的手算结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么防死锁的规则是「不对称」的——source 不能等 ready，destination 却可以等 valid？如果允许 source 也等 ready 会怎样？

**参考答案**：若 source 也被允许「等 destination 先拉 ready 才拉 valid」，就可能两边互相死等：source 等 ready，destination 等 valid，谁也不先动，握手永远不发生，这就是死锁。允许 destination 等 valid 是安全的，因为 source 总能先拉 valid 而不必等任何人；这种「单边可等」也恰好服务于仲裁器「从多个 source 里挑一个」的需要。

**练习 2**：一个模块在 `handshake_complete` 为假时改了内部计数器，违反了「内部状态只能在握手完成拍改变」的规则，最坏后果是什么？

**参考答案**：会丢数据。正文举的例子：若你在「包内字计数到 0」的当拍就改状态进入下一个包，而恰好这一拍 destination 的 `ready` 是 0（没握成手），那最后一个字没被收下就被跳过了，当前包和后续包全部错位损坏。把状态更新用 `handshake_complete` 门控住，就能保证「没真正握成手就不前进」。

### 4.3 AXI 不根本

#### 4.3.1 概念说明

很多初学者第一次接触 ready/valid 是通过 **AXI** 总线（ARM 的 AMBA AXI4 规范）。于是容易产生一个错觉：AXI 是「正统」，ready/valid 只是 AXI 的一个细节。

本书的立场恰好相反，标题就叫 **「AXI is not Fundamental」**（AXI 并不根本）。理由有三层：

1. **AXI 的每个通道本身就是一个 ready/valid 接口**。AXI4-Stream 里的 `TLAST` 也只是「附在握手上的一个元数据位」。也就是说，是 ready/valid 撑起了 AXI，而不是反过来。
2. **AXI 带来太多不必要的假设与复杂度**。对绝大多数设计而言，直接套用 AXI4 / AXI4-Stream 会把一堆用不上的规矩也背进来。
3. **更划算的做法是从原语自己拼**。用 `Skid Buffer`、`Arbiter`（合并器）、`Fork`（分叉）、`Join`（汇合）等小块，按需搭出自己的握手接口，更小、更灵活、更易理解。

再往下深挖一步：ready/valid 握手本身也不是最底层——它只是**「同步」（synchronization，又叫 rendez-vous「会合」）**这一更基本机制的一种特定实现。作者用一对叫 `OK_IN`/`OK_OUT` 的信号来演示这种会合：两个并发模块各自有 `OK_IN` 输入与 `OK_OUT` 输出，把一方的输出接到另一方的输入；当某模块需要同步，就拉高并保持 `OK_OUT`，直到两方在**同一个时钟周期**同时看到

\[ (\text{OK\_IN} \,==\, 1'b1) \;\land\; (\text{OK\_OUT} \,==\, 1'b1) \]

它们就「会合」了——没有任何数据流动，也没有谁控制谁。**如果你在这种会合之上再让数据单向流动，就重新发明了 ready/valid 接口。**

这套机制和异步逻辑里的 **2 相 / 4 相握手**很像，只是因为我们有一个时钟作为绝对时间基准，所以被简化了。

> 这一段「会合」观点是下一讲 `u9-l2` 讨论 OK_IN/OK_OUT 会合同步、以及更后面 `Synchronous_Muller_C_Element`（Muller C 元素）的理论铺垫。

#### 4.3.2 核心流程

把「AXI 不根本」展开成一个认识层次：

```
        最底层：会合同步 (rendez-vous)   —— OK_IN / OK_OUT 双方同时就绪
                  │  (+ 单向数据流)
                  ▼
        中间层：ready/valid 握手接口     —— valid/ready/data 三信号
                  │  (+ 一堆通道、ID、last、协议假设)
                  ▼
        上层：AXI4 / AXI4-Stream         —— 工业标准总线
```

从上往下看，每一层都只是「下一层 + 一些附加约定」。所以本书的选择是：**直接停在最简洁的 ready/valid 这一层**，按需把会合与数据流组合起来，需要缓冲就加 `Skid Buffer`、需要分流就加 `Fork`、需要合流就加 `Merge`/`Arbiter`，而不是从笨重的 AXI 顶部往下削。

这条认识还有一个副产品：本书拒绝使用「master/slave」（主/从）这套术语，改用「source/destination」。因为 master/slave 隐含了「一方控制另一方」的假设，而会合机制里**没有任何一方在控制另一方**——它们只是平等地「约定同时前进」。这种术语上的洁癖，本身就是「AXI 不根本」立场的延伸。

#### 4.3.3 源码精读

「AXI is not Fundamental」一节开门见山：

> [handshake.html:L24-L30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L24-L30) —— 作者坦言这些规则最初脱胎自 AMBA AXI4 规范第 A3 章，但用「source/destination」替换了不准确的「master/slave」术语。

紧接着给出「ready/valid 才是 AXI 的底层」的论证，以及「自己拼更划算」的结论：

> [handshake.html:L32-L41](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L32-L41) —— AXI4 每个通道都是 ready/valid 接口，AXI4-Stream 的 TLAST 只是握手控制的元数据；AXI 带来过多假设，从 `Skid Buffer`/`Arbiter`/`Fork`/`Join` 等原语自建更简单、更小、更灵活。

更底层的「会合同步」论证与 OK_IN/OK_OUT 示意：

> [handshake.html:L210-L219](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L210-L219) —— ready/valid 只是「同步（会合）」的一种实现；这也是要抛弃 master/slave 术语的更深层理由。

> [handshake.html:L228-L233](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L228-L233) —— 当两方在同一拍同时看到 `(OK_IN==1) && (OK_OUT==1)`，就完成了同步——没有数据流动，也没有谁控制谁。

> [handshake.html:L240-L244](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L240-L244) —— 在会合之上加单向数据流，就重新发明了 ready/valid 接口。

与异步逻辑的对照：

> [handshake.html:L246-L249](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L246-L249) —— 这套会合机制与异步逻辑的 2 相 / 4 相握手相似，只是有时钟作绝对时间基准而被简化。

#### 4.3.4 代码实践

**实践目标**：用一个真实模块，体会「从原语自建握手接口」比「直接上 AXI」轻多少。

**操作步骤**：

1. 打开仓库里的 [Pipeline_Skid_Buffer.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v)（这是正文 L38 链接到的模块），只看它的**端口列表**（module 头部的输入输出声明），不要细读实现。
2. 数一数：它的 source/destination 接口是不是正好就是 `valid`/`ready`/`data` 三信号（各一套），加上一个共用的 `clock`（与可选 `clear`）？
3. 对比设想：如果要用 AXI4-Stream 表达同样的「单向数据流 + 握手」，至少还要多引入 `TVALID`/`TREADY`/`TDATA` 之外的哪些信号（如 `TID`/`TDEST`/`TSTRB`/`TKEEP`/`TLAST`/`TUSER` …）？

**需要观察的现象**：`Pipeline_Skid_Buffer` 的端口极简——本质就是「一对 valid/ready/data + 时钟」，没有 AXI 那一长串附加通道与元数据信号。

**预期结果**：你会直观感受到正文「从原语自建更简单、更小」的判断——一个能解决「流水线握手矛盾」的完整模块，端口清单短得能一眼看完；而等价的 AXI4-Stream 接口会强塞进一堆当前设计根本用不上的信号。这正是作者提倡停在 ready/valid 这一层、按需加原语的理由。

> 待本地验证：端口清单可在源码里直接读到，无需仿真；若你想进一步确认它的握手行为，留到 `u9-l2` 我们再精读这个模块的实现。

#### 4.3.5 小练习与答案

**练习 1**：用一句话解释「AXI 不根本」这个标题。

**参考答案**：AXI 的每个通道本身就是一个 ready/valid 接口，ready/valid 才是撑起 AXI 的底层机制；而 ready/valid 又只是「会合同步 + 单向数据流」的一种实现。所以 AXI 处在这条因果链的「上层」而非「根本」。

**练习 2**：作者为什么用 source/destination 替换 master/slave？这和「会合」观点有什么关系？

**参考答案**：master/slave 隐含「一方控制另一方」的假设，但会合机制里两方是平等的——它们只是约定「同时就绪、同时前进」，没有谁在控制谁。source/destination 只描述数据流动方向（出方/入方），不带控制含义，因此与底层会合语义一致，这正是「AXI 不根本」立场在术语上的体现。

**练习 3**：会合（OK_IN/OK_OUT）与 ready/valid 握手的差别是什么？

**参考答案**：会合只约定「双方同时就绪、同时前进」，**不涉及数据流动，也没有主控方**；ready/valid 握手是在会合之上**加上单向的数据流**（valid/data 从 source 到 destination）。换言之，ready/valid = 会合 + 定向数据传输。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯穿任务。

**任务**：设计并分析一次完整的 source → destination 数据传递。

1. **画连接图**：画出 source 与 destination 两个方框，标出 `valid`、`data`（source→destination）与 `ready`（destination→source）三根线的方向，并在每根线旁注明驱动方。确认接口内无组合环路。（对应 4.1）
2. **编一个两周期握手的时序表**：自拟 5 个时钟周期的 `valid`/`ready`/`data` 取值，要求其中恰好发生 2 次握手，且 source 在等待期间始终保持了 `valid` 与 `data` 稳定。（对应 4.2）
3. **标注握手发生的周期**：在你画的表里用 \( \text{handshake\_complete} = (\text{ready} \,\&\, \text{valid}) \) 算出每一拍是否握手，圈出发生握手的周期，并说明每个 `data` 值是在哪一拍被 destination 接收的。（对应 4.2）
4. **检验规则**：检查你的时序表是否满足三条铁律——valid 保持到完成（防活锁）、source 没有等 ready 才拉 valid（防死锁）、内部状态只在 `handshake_complete` 拍改变。若你故意造一个违反「valid 保持」的版本，指出数据会在哪一拍丢失。（对应 4.2）
5. **一句话定位**：用本讲的层次观点（会合 → ready/valid → AXI），说明你画的这套接口停在中间层、为什么没必要升到 AXI。（对应 4.3）

**验收标准**：

- 连接图中 `ready` 与 `valid`/`data` 方向相反，无组合环。
- 时序表里两次握手周期被正确圈出，且 source 等待期间 `valid`/`data` 保持不变。
- 能说清「若 source 中途撤 valid，哪个数据会丢」。
- 能说出「停在 ready/valid 层即可，无需 AXI 的额外假设」。

> 待本地验证（可选进阶）：把你的时序表写成一个最小 testbench，驱动一个只含 `handshake_complete` 组合块的空壳模块，每拍打印 `(ready & valid)`，对照手算结果。本讲不要求跑仿真，重点是画图与判定正确。

## 6. 本讲小结

- ready/valid 握手用三个信号连接模块：`valid`、`data` 由 source 驱动并指向 destination，`ready` 由 destination 驱动并指回 source；三者全部同步到时钟上升沿。连接永远从 source 到 destination。
- 接口内部不得有组合环路：source 内不能有 `ready→valid` 组合通路，destination 内不能有 `valid→ready` 组合通路，否则相连即成环（或至少拖长关键路径）。
- 握手完成的判定只有一句：**同一拍 `valid` 与 `ready` 同高**，destination 在该拍接收数据；标准写法是 `handshake_complete = (ready == 1'b1) && (valid == 1'b1)`。
- 防失败的三条铁律：source 拉高 valid 后必须保持到完成（防活锁）；source 不得等 ready 才拉 valid、destination 可以等 valid 才拉 ready（防死锁）；影响接口的内部状态只能在 `handshake_complete` 拍改变（防丢数据）。
- 「AXI 不根本」：AXI 的每个通道本身就是 ready/valid 接口，ready/valid 又只是「会合同步（OK_IN/OK_OUT）+ 单向数据流」的实现；因此本书停在 ready/valid 层、用 `Skid Buffer`/`Fork`/`Join`/`Arbiter` 等原语按需自建接口，比直接套 AXI 更小更灵活，并以 source/destination 取代 master/slave 术语。
- 单周期握手是最优（destination 提前 ready）；两周期握手合法（source 先 valid、保持到下一拍 ready），多花一拍但不丢数据。

## 7. 下一步学习建议

- **下一讲 `u9-l2`（死锁/活锁避免与底层同步）**：把本讲的规则真正落到 Verilog 上——精读 `Pipeline_Skid_Buffer.v`，看它如何实现一个符合全部握手规则的弹性缓冲，并用 `handshake_complete` 门控内部包计数等状态；同时深入 OK_IN/OK_OUT 会合同步的工程含义。
- **更后续**：`u10`（Skid Buffer 与 COTTC FSM 方法）会以 `Pipeline_Skid_Buffer` 为范本讲状态机设计法；`u11-l2`（Muller C 元素与流水线同步器）会把本讲的「会合」落地为 `Synchronous_Muller_C_Element` 与 `Pipeline_Synchronizer_Lazy`。
- **建议先做的源码预习**：在进入 `u9-l2` 前，先扫一眼 `Pipeline_Skid_Buffer.v` 的端口列表（不必读实现），验证它确实就是「一对 valid/ready/data + 时钟」——这会让下一讲的精读顺畅很多。
