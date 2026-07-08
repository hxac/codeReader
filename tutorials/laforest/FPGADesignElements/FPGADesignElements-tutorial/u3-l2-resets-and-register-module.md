# 复位哲学与 Register 模块

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 FPGA 上**三种「复位」**的来历与分工：上电复位（power-on reset）、同步复位（也叫 clear）、异步复位（areset），以及为什么本书「能用同步就别用异步」。
- 写出 **「最后赋值胜出（last assignment wins）」** 复位惯用法，并说清它和上一讲见过的「链式三元最后赋值胜出」有什么不同。
- 讲明白一个反直觉的点：**复位是全书里少数几个「必须用 `if`、不能用三元 `?:`」的地方**，而且根因藏在非阻塞赋值 `<=` 的求值时机里。
- 打开 `Register.v` / `Register_areset.v`，看懂作者如何把上面所有取舍**封装进一个可复用的构建块**，以及为什么连一个寄存器都要做成模块。

这三件事是上一讲 [u3-l1 赋值风格、三元运算符与逻辑设计](./u3-l1-assignments-and-ternary.md) 留下的伏笔的收束：那篇说「三元优于 `if/else`，除了个别复位代码」，本讲就来拆这颗「例外」的炸弹。

## 2. 前置知识

本讲承接 [u3-l1](./u3-l1-assignments-and-ternary.md) 和 [u2-l2 参数化与位宽处理](./u2-l2-parameterization-and-widths.md)，并回扣 [u2-l1](./u2-l1-verilog2001-and-default-nettype.md)。请确认你已经知道：

- **阻塞 `=` vs 非阻塞 `<=`**：组合块 `always @(*)` 只用 `=`（立即写入，下一行可见）；时钟块 `always @(posedge clock)` 只用 `<=`（先采样旧值，时间步末统一写入）。（u3-l1）
- **「最后赋值胜出」**：在同一个块里多次给同一个变量赋值，**最后一条生效**。上一讲我们用它把 7 条状态转移写成 7 行链式三元（[u3-l1 4.2.3](./u3-l1-assignments-and-ternary.md)）。本讲它再次登场，但这次是在**时钟块**里、用来写复位。
- **参数默认值为 `0`**：本书所有 `parameter` 默认 `0`，忘设参数会让 `[WORD_WIDTH-1:0]` 退化成非法的 `[-1:0]`，在精化（elaboration）阶段吵闹失败。（u2-l2）
- **`reg` 输出端口不能在声明处初始化**，必须用紧跟的 `initial` 块赋初值。（u2-l1）

本讲要回答的问题是：**寄存器除了「存数据」，还要能「回到一个已知状态」——这件事到底怎么做、做几次、用什么信号触发？**

先建立三个直觉：

- **FPGA 的初始状态是「免费」的**：配置比特流（bitstream）里就写着每个寄存器上电时的值，由片上专用电路一次性加载，不占你的逻辑资源。
- **「复位」其实是「控制」，不是「数据」**：把一个寄存器拉回某个值，是一种控制行为，应当和数据通路分开。
- **异步 ≠ 更好**：异步复位听起来「更可靠」，但在 FPGA 上它会制造仿真怪象、拖慢设计（抑制寄存器重定时），是本书极力避免的东西。

> 名词小贴士：
> - **上电复位（power-on reset）**：FPGA 加载配置时，由片上硬件把所有寄存器置成 bitstream 里写好的初值。对设计者而言「自动发生」。
> - **同步复位 / clear**：在时钟边沿生效的复位，本质是「控制写进寄存器的数据」。
> - **异步复位 / areset**：一旦拉高立即生效、不等时钟边沿的复位，直接接到触发器的硬件复位端。
> - **寄存器重定时（register retiming）**：综合/布局布线工具把寄存器在组合逻辑前后移动以平衡时序的优化。异步复位会**抑制**它。
> - **复位树（reset tree）**：把一个复位信号扇出到成百上千个寄存器的互连网络，越大越费布线资源、越难满足时序。

## 3. 本讲源码地图

本讲盯三个文件：

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `verilog.html` | 全书 Verilog 编码规范正文 | Resets 一节：上电/同步/异步取舍、last-assignment-wins 惯用法、异步复位 + clock_enable 为何要嵌套 if |
| `Register.v` | 一个**同步**复位寄存器模块（本书默认的寄存器） | 把全部复位取舍封装进一个构建块 |
| `Register_areset.v` | 上述寄存器的**异步**复位变体（特殊场合才用） | 用嵌套 `if` 表达异步复位的优先级，对比 `Register.v` |

`Register` 是全书最底层、被实例化次数最多的模块之一——上一讲里 `Pipeline_Skid_Buffer` 的五个寄存器全是它的实例。理解了它，你就理解了本书「连一个寄存器都做成模块」的设计哲学。

## 4. 核心概念与源码讲解

### 4.1 上电复位、同步复位与异步复位

#### 4.1.1 概念说明

「复位」听起来是一件事，其实在 FPGA 上有**三种来历不同、用法不同**的东西，混在一起最容易踩坑。先把它们分清楚：

**（1）上电复位——免费的初始状态**

FPGA 不是上电后「什么都没有」。配置比特流里**写着每个寄存器、大部分片上存储器的初值**，加载配置时由专用硬件电路一次性灌进去。这意味着：

- 你的设计一上电就处于一个**已知状态**，不需要任何代码去「跑一遍复位」。
- 这个初值是「免费」的：不占逻辑、不占布线。
- 它还能在**运行时被回到**——只要你的设计允许。

既然如此，很多寄存器**根本不需要复位信号**。设好初值（声明处赋值，或 `reg` 输出端口在 `initial` 块里赋值）就够了。规范要求「所有寄存器都必须用不含 X/Z 的值初始化」，正是这个意思：

[verilog.html:L131-L135](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L131-L135) —— 所有寄存器都必须用不含 X/Z 的值初始化（声明处或 `initial` 块），并提示「某些复位 + 初始化组合在某些 FPGA 系列上会冲突」。

**（2）同步复位（clear）——运行中按需清零**

如果你需要在**正常运行中**把寄存器拉回初值（比如状态机要回到 IDLE、计数器要归零），用一条**同步**的 `clear` 信号。它的工作方式不是「触发硬件复位端」，而是**改变写进寄存器的数据**：当 `clear==1`，时钟边沿到来时把 `RESET_VALUE` 写进去。

- 它在时钟边沿生效，行为可预期，仿真和硬件一致。
- 它会引入一点额外逻辑（一个多路选择），但这部分逻辑会被综合器**折叠进**原本就要喂给寄存器的前级逻辑里——很多时候本来就需要，不亏。
- 它还能让你在运行时回到上电初值，而无需复杂设计。

**（3）异步复位（areset）——能不用就不用**

异步复位直接接到触发器的硬件复位端，**一旦拉高立即生效，不等时钟边沿**。本书对它的态度非常明确：**能不用就不用**。原因有三：

- **仿真怪象**：因为立即生效，寄存器可能「在一个时钟周期内就变了」，行为仿真里看着像「没采到数据」，带时序信息的后仿真里更是「不可能地变化」，极难调试。
- **抑制寄存器重定时**：哪怕异步复位被常接地（接 `0`），它**存在**这件事本身就会让综合器不敢做寄存器重定时——而这是提速、减负的关键优化。
- **复位网络变大**：大量寄存器共用一个异步复位，复位树庞大，布线和时序都受拖累。

规范的态度一句话：「尽量用上电复位，**限制**需要显式异步复位的东西，并且**复位信号本身要与时钟同步**」：

[verilog.html:L717-L739](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L717-L739) —— 用好隐式上电复位、限制异步复位、复位信号要经同步器（如 CDC Bit Synchronizer）同步到时钟；异步触发的硬件复位会引发仿真怪象，运行中清零应改用同步 `clear`。

真正的异步复位只在两种场合是合理的例外：**时钟还没有的时候**（如 PLL 复位），或者**同步控制逻辑本身卡死**、需要硬掰回来的时候。

#### 4.1.2 核心流程

三种「复位」的触发方式、代价、适用场合可以这样对比：

| | 上电复位 | 同步 clear | 异步 areset |
| --- | --- | --- | --- |
| **谁来触发** | 片上硬件加载 bitstream | 你写的 `clear` 信号，在时钟边沿 | 你写的 `areset` 信号，立即 |
| **何时生效** | 仅上电（一次） | 任意时钟边沿 | 拉高瞬间，不等时钟 |
| **是否占资源** | 免费 | 一点被折叠的逻辑 | 复位树 + 抑制重定时 |
| **仿真是否好懂** | 不涉及 | 是 | 否（怪象多） |
| **本书建议** | 默认靠它设初值 | 运行中清零用这个 | 尽量别用 |

一个设计的典型复位策略是：

```text
1. 所有寄存器在 bitstream 里设好初值（上电复位，免费）。
2. 运行中需要清零的，加一条同步 clear。
3. 只有时钟未就绪、或控制逻辑可能卡死的关键寄存器，才上异步 areset，
   且 areset 信号本身要先经同步器同步到本时钟域。
```

#### 4.1.3 源码精读

`Register.v` 的正文用三段注释把上面三种取舍讲得清清楚楚。先看「上电复位」段——注意那句「免费、且运行时可回到」：

[Register.v:L10-L15](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L10-L15) —— 上电复位：寄存器初值在 bitstream 里、由专用硬件加载，免费且运行时可回到，因此免去了相应的控制与数据逻辑。

紧接着「异步复位」段——注意作者**故意不在 `Register.v` 里实现异步复位**，并解释了原因（抑制重定时）：

[Register.v:L17-L24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L17-L24) —— 异步复位在此不实现，因其存在（即便接地）会抑制寄存器重定时；若确需异步复位（ASIC 或关键寄存器），改用 `Register_areset`。

再看「同步复位（clear）」段——讲清了「会多一点点逻辑，但会被折叠进前级逻辑、本来就需要」：

[Register.v:L26-L33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L26-L33) —— 运行中清零用同步 `clear`；它可能引入额外逻辑，但该逻辑会被折叠进喂给寄存器的其它逻辑，且让你无需复杂设计就能回到上电初值。

规范正文还进一步说明：bitstream 里包含所有寄存器和大部分片上存储器的初值，所以**多数逻辑根本不需要复位信号**就能正确启动——设好初值即可：

[verilog.html:L741-L751](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L741-L751) —— bitstream 含所有寄存器与多数片上存储器的初值；声明处或 `initial` 块设初值，存储器可用 `$readmemh()`。

为什么强调「复位 + 初始化的组合在某些芯片上不成立」？规范引用了一段关于**不同 FPGA 系列复位能力差异**的说明：触发器粗分四类（不可初始化、只能常 0、全可配、init 须匹配 set/reset），所以「声明初值 + 异步复位」的组合在某些芯片上**硬件无法满足**；而同步复位总能用输入逻辑模拟出来，因此「全局同步复位永远可行」：

[verilog.html:L753-L781](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L753-L781) —— 不同 FPGA 系列触发器复位能力分四类；异步复位到寄存器很罕见且非好事（抑制重定时），所以寄存器初始化仍可行，除非确需显式异步复位。

而 `Register_areset.v` 的「异步复位」注释段，则把异步复位的危害和「即便要用也必须同步化」讲得更细，并明确给出「尽可能避免」的告诫：

[Register_areset.v:L17-L36](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L17-L36) —— 异步复位立即生效会引发仿真怪象；必须用时，复位源要与时钟同步以免在下级寄存器亚稳态窗口附近翻转；能不用就不用，改用无异步复位的 `Register`。

#### 4.1.4 代码实践

**实践目标**：用阅读源码的方式，把三种复位在 `Register.v` 里的「有/无」关系理清。

**操作步骤**：

1. 打开 `Register.v`，分别定位三段注释：上电复位（L10–L15）、异步复位（L17–L24）、同步 clear（L26–L33）。
2. 回答：`Register.v` **实际实现了**哪几种复位？（提示：看端口表里有没有 `areset`。）
3. 打开 `Register_areset.v` 的端口表（L58–L65），对比它比 `Register.v` 多了哪个输入。
4. 解释：为什么作者说 `Register.v` 里「异步复位即便接地也不实现」？

**需要观察的现象**：`Register.v` 的端口只有 `clock / clock_enable / clear / data_in / data_out`，**没有 `areset`**；`Register_areset.v` 多了一个 `areset` 输入。

**预期结果**：`Register.v` 只依赖上电复位 + 同步 `clear`，**不提供**异步复位；`Register_areset.v` 才是那个带异步复位的特殊变体。作者不在默认 `Register` 里放异步复位，是因为「异步复位端的存在」本身（哪怕接地）就会抑制寄存器重定时，拖慢设计。

> **待本地验证**：若你手头有综合工具，可分别综合 `Register.v`（设好 `WORD_WIDTH`）和 `Register_areset.v`，对比综合报告里「能否进行寄存器重定时 / 复位网络的扇出」差异——这正是作者取舍的现实依据。

#### 4.1.5 小练习与答案

**练习 1**：一个状态机寄存器，上电时要进入 `IDLE`，运行中遇到错误要能立刻回到 `IDLE`。按本书规范，应该用哪种复位？为什么不用异步复位？

**参考答案**：上电进入 `IDLE` 靠**上电复位**——在 `initial` 块（或声明处）把寄存器初值设为 `IDLE` 编码即可，免费。运行中回到 `IDLE` 用**同步 `clear`**（或在下一状态逻辑里把次态设为 `IDLE`）。不用异步复位，是因为它会引发仿真怪象、抑制寄存器重定时、放大复位树，除非控制逻辑可能卡死才考虑。

**练习 2**：规范说「复位信号本身要与时钟同步」。如果你有一个外部按键产生的异步复位，直接接到 `Register_areset` 的 `areset` 上，会有什么隐患？

**参考答案**：`areset` 异步撤销（释放）的时刻可能恰好落在下级寄存器的亚稳态窗口附近，导致部分寄存器看到复位释放、部分没看到，或直接进入亚稳态。正确做法是让外部复位先经过一个同步器（如 `Reset_Synchronizer` / `CDC_Bit_Synchronizer`）同步到本时钟域，再喂给 `areset`（见 [verilog.html:L857-L860](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L857-L860)）。

---

### 4.2 「最后赋值胜出」复位惯用法

#### 4.2.1 概念说明

上一讲（[u3-l1 4.2.3](./u3-l1-assignments-and-ternary.md)）我们在**组合块**里见过「最后赋值胜出」：把多条转移条件写成链式三元，每条命中就覆盖一次，最后命中的胜出。现在我们把同一个思想搬到**时钟块**里写复位——但有一个关键差别，正是这个差别让「三元」在这里失灵。

先看**惯常的 `if/else` 复位写法**有什么毛病：

```verilog
// 示例代码：惯常的 if/else 复位写法（有问题）
always @(posedge clock) begin
    if (reset == 1'b1) begin
        foo <= FOO_RESET;
        bar <= BAR_RESET; // 但 bar 其实不需要复位！
    end
    else begin
        foo <= foo_next;
        bar <= bar_next;
    end
end
```

问题在于：用 `if/else` 时，**每个寄存器都必须在两个分支里都被赋值**，否则综合会推断出锁存器。于是哪怕 `bar` 根本不需要复位，你也被迫在 `reset` 分支里写一句 `bar <= BAR_RESET`——结果**所有寄存器都被卷进复位树**，复位网络最大化，布线资源浪费、时序更难满足。

规范给出的解决之道有两条，其中第二条就是本节主角——**「最后赋值胜出」惯用法**：

[verilog.html:L784-L824](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L784-L824) —— 先批评 `if/else` 复位把所有寄存器卷进复位树；再给出两种替代写法：三元赋值复位、和「最后赋值胜出」复位。

「最后赋值胜出」写法是这样的：**先正常赋值，再用 `if (reset)` 覆盖成复位值**。需要复位的寄存器才加那句 `if`，不需要的就只有正常赋值——于是复位树只覆盖真正需要复位的寄存器，最小化复位网络。

```verilog
// 示例代码：「最后赋值胜出」复位（bar 不复位，自然不进复位树）
always @(posedge clock) begin
    foo <= foo_next;
    bar <= bar_next;       // 不需要复位，正常赋值即可

    if (reset == 1'b1) begin
        foo <= FOO_RESET;  // 只有 foo 被复位
    end
end
```

这里能用的原理，正是非阻塞赋值 `<=` 的「最后赋值胜出」语义：同一个块里对 `foo` 赋值两次，**后一句胜出**。`reset` 拉高时，第二句覆盖第一句，`foo` 得到 `FOO_RESET`；`reset` 为低时，第二句根本不执行，`foo` 保持第一句的 `foo_next`。

#### 4.2.2 核心流程

那么能不能用上一讲大力推崇的**三元运算符**来写这句覆盖？ temptation 很强：

```verilog
// 示例代码：用三元实现 clear（看起来很自然，但在这里是错的！）
always @(posedge clock) begin
    if (clock_enable == 1'b1) data_out <= data_in;             // (A)
    data_out <= (clear == 1'b1) ? RESET_VALUE : data_out;      // (B) 三元
end
```

**这是错的**，而且错得很隐蔽。根因在非阻塞赋值 `<=` 的求值时机：**右边的 `data_out` 读到的是旧值**（本时间步开始时的值），而不是 (A) 刚刚排进队列的 `data_in`。

逐拍追踪就一目了然。设当前 `data_out = OLD`，某拍 `clock_enable=1`、`clear=0`、`data_in=NEW`：

| 语句 | `reset==1`? | 排入队列的写入 | `<=` 右侧读到的 `data_out` |
| --- | --- | --- | --- |
| (A) `if(clock_enable) data_out <= data_in` | — | `data_out <= NEW` | — |
| (B) `data_out <= (clear)? RESET : data_out` | `clear=0` | `data_out <= OLD` | **OLD（旧值！）** |

「最后赋值胜出」→ (B) 胜出 → `data_out` 变成 `OLD`，而不是 `NEW`。**`data_in` 被悄悄吞掉了，寄存器永远采不进新数据。** 只要 `clear` 不拉高，(B) 每拍都排一句 `data_out <= data_out(旧)`，把 (A) 顶掉。

根因一句话：**三元永远会排入一句赋值**（要么 `RESET_VALUE`，要么 `data_out`）；当条件不成立时，它排入的是「写回旧值」，于是在「最后赋值胜出」的语义下，把前面那句有用的赋值盖掉了。

而 `if` 语句不同：**条件不成立时，它什么都不排**，前面那句 `data_out <= data_in` 得以保留。所以这里**必须用 `if`，不能用三元**。

```text
三元的毛病：不命中也写一句（写回旧值）→ 盖掉前一句赋值
if 的好处：  不命中就不写      → 前一句赋值得以保留
```

这正是规范里点名的那条「非阻塞赋值 + 三元」的微妙之处（源自 Claire Wolf 的讨论）：

[verilog.html:L826-L837](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L826-L837) —— 「最后赋值胜出」惯用法归功于 Olof Kindgren；并引用 Claire Wolf 的讨论，点明非阻塞赋值与三元运算符在此处的一个微妙陷阱。

> **和上一讲的联系**：上一讲说「三元优于 `if/else`」有四条理由（防锁存器、可赋值常量、可链式、传播 X）。本讲揭示的是那四条理由的**边界**——在「时钟块 + 非阻塞赋值 + 最后赋值胜出」的复位语境下，三元的「永远排入一句赋值」反而成了缺陷。复位是全书里少数几个 `if` 不可替代的地方之一。

#### 4.2.3 源码精读

`Register.v` 的实现段直接用注释点明了这颗「地雷」：

[Register.v:L58-L63](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L58-L63) —— 这里用「最后赋值胜出」惯用法实现复位；并指出此处**不能用三元**，否则 `clear` 不成立时那句 `data_out <= (clear)? RESET_VALUE : data_out` 会用当前 `data_out`（旧值）盖掉前面的赋值。

实际实现只有短短一段 `always`，结构正是「先 `clock_enable` 赋值，再 `if (clear)` 覆盖」：

[Register.v:L65-L73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L65-L73) —— `Register.v` 的时钟块：两个并列的 `if`，先按 `clock_enable` 写 `data_in`，再按 `clear` 覆盖成 `RESET_VALUE`。

```verilog
always @(posedge clock) begin
    if (clock_enable == 1'b1) begin
        data_out <= data_in;
    end

    if (clear == 1'b1) begin
        data_out <= RESET_VALUE;
    end
end
```

逐句读：

- 第一个 `if`：`clock_enable` 拉高时，排入 `data_out <= data_in`。
- 第二个 `if`：`clear` 拉高时，排入 `data_out <= RESET_VALUE`，靠「最后赋值胜出」盖掉前一句。
- 两个信号都为 `0` 时：两句都不排，`data_out` 保持不变（这正是「保持」语义）。
- 两个信号都为 `1` 时：第二句胜出，`clear` 优先级高于 `clock_enable`。

注意这两个 `if` 是**并列**的（不是 `if/else`），这正是「最后赋值胜出」的写法特征——它不强迫你在两个分支里都赋值，于是复位树只覆盖真正会被复位的情形。

#### 4.2.4 代码实践

**实践目标**（本讲指定实践任务）：用「最后赋值胜出」惯用法，写一个带 `clock_enable` 与 `clear` 的时钟 `always` 块；并说明为什么 `clear` 不能用三元运算符实现。本质上，你是在重新推导 `Register.v` 的核心。

**操作步骤**：

1. 先按「最后赋值胜出」写出正确版本（这就是 `Register.v` 的写法）：

   ```verilog
   // 示例代码：正确——用并列 if 实现最后赋值胜出
   always @(posedge clock) begin
       if (clock_enable == 1'b1) begin
           data_out <= data_in;        // (A)
       end

       if (clear == 1'b1) begin
           data_out <= RESET_VALUE;    // (B) 只在 clear 时才排入
       end
   end
   ```

2. 再写出「看起来更简洁、实则是错」的三元版本，并准备用它做对照：

   ```verilog
   // 示例代码：错误——用三元实现 clear
   always @(posedge clock) begin
       if (clock_enable == 1'b1) begin
           data_out <= data_in;                            // (A)
       end
       data_out <= (clear == 1'b1) ? RESET_VALUE : data_out; // (B') 三元
   end
   ```

3. 用第 4.2.2 节的表格，对三元版本做一次「`clear=0`、`clock_enable=1`、`data_in=NEW`、当前 `data_out=OLD`」的逐拍追踪，得出 `data_out` 最终变成什么。

**需要观察的现象**：

- 正确版本里，当 `clear=0`、`clock_enable=1` 时，(B) 不执行，`data_out` 在下个边沿变成 `NEW`——寄存器正常采数。
- 三元版本里，同样条件下，(B') 仍然执行，排入 `data_out <= OLD`（旧值），盖掉 (A)，`data_out` 停在 `OLD`——寄存器**采不进新数**。
- 只有当 `clear=1` 时，两个版本才恰好一致（都得 `RESET_VALUE`）。

**预期结果**：

- 正确（并列 `if`）：`clear` 与 `clock_enable` 的全部四种组合下行为都正确（保持 / 采数 / 清零 / 清零优先）。
- 错误（三元）：只要 `clear=0`，(B') 就会用旧值盖掉前面的赋值，导致 `clock_enable` 失效。

**为什么 `clear` 不能用三元？** 一句话总结：非阻塞赋值 `<=` 的右侧读旧值，三元在条件不成立时会排入一句「写回旧值」的赋值，在「最后赋值胜出」语义下盖掉了前面真正有用的赋值；而 `if` 在条件不成立时**什么都不排**，前面的赋值得以保留。

> **待本地验证**：用仿真器（Icarus Verilog / Verilator）给上面两段各写一个最小测试桩，施加「`clear=0`、`clock_enable=1`、每拍换 `data_in`」的激励，观察正确版本里 `data_out` 跟随 `data_in` 变化、而三元版本里 `data_out` 卡死不变——亲眼看到这颗地雷引爆。

#### 4.2.5 小练习与答案

**练习 1**：惯常的 `if/else` 复位写法（`reset` 分支和 `else` 分支各赋值所有寄存器）有什么缺点？

**参考答案**：它强迫**每个寄存器都在两个分支里被赋值**（否则生锁存器），于是哪怕不需要复位的寄存器也被迫在 `reset` 分支写一句复位值，导致**所有寄存器都进入复位树**，复位网络最大化，浪费布线、拖累时序。

**练习 2**：把 `Register.v` 时钟块里的第二个 `if (clear)` 改写成三元 `data_out <= (clear == 1'b1) ? RESET_VALUE : data_in;`（注意这里三元写的是 `data_in` 而不是 `data_out`），行为还正确吗？和本节讲的「错误三元」有何不同？

**参考答案**：这种改写「碰巧」在多数情况下行为正确——因为它的 `else` 分支写的是 `data_in` 而非旧 `data_out`，所以 `clear=0` 时排入 `data_out <= data_in`，与 (A) 一致，不会吞数据。但它**语义不同**：它不再受 `clock_enable` 约束（`clock_enable=0`、`clear=0` 时它仍会排入 `data_out <= data_in`，把「保持」语义破坏掉），而且和「最后赋值胜出」的清晰意图（先正常、再覆盖）相去甚远，可读性差、易错。本节强调的「错误三元」特指 `else` 分支写**旧 `data_out`** 的那种，它才会在 `clear=0` 时吞掉前一句赋值。

---

### 4.3 Register 模块：把复位封装进构建块

#### 4.3.1 概念说明

读到这里你可能会问：综合器本来就会从 `always @(posedge clock)` 推断出寄存器，何必再写一个 `Register` 模块把它包起来？作者的回答是：**为了在最底层就把「数据」和「控制」分开**。

复位属于**控制**（它决定寄存器何时、以何值被改写），而不是数据。如果把复位逻辑散落在每个用到寄存器的地方，控制逻辑就会和数据通路纠缠不清。把「一个带 `clock_enable`、`clear`、初值的寄存器」封装成一个模块后：

- **控制逻辑被收口**：所有关于「要不要写、要不要清、初值是什么」的取舍，都藏在 `Register` 内部，使用者只管 `data_in → data_out`。
- **数据通路更干净**：上层模块的 `always @(*)` 只算数据，不再掺入复位细节。
- **复用与一致性**：全书成百上千个寄存器都走同一套复位规则，不会有人手抖写错。

作者的这段自述（`Register.v` 开篇）把意图说得很直白：

[Register.v:L4-L8](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L4-L8) —— 把寄存器做成模块而非让 HDL 推断，是为了在最底层分离数据与控制（含各种复位），从而简化控制逻辑、减少部分布线资源。

#### 4.3.2 核心流程

`Register` 模块的接口和数据流非常简单：

```text
输入                         输出
clock        ─┐
clock_enable ─┼─► [写门控] ──► [clear 覆盖] ──► data_out (reg)
clear        ─┤            (最后赋值胜出)
data_in      ─┘
RESET_VALUE  (参数，上电初值 + clear 值)
```

- **写门控**：`clock_enable==1` 才排入 `data_out <= data_in`。
- **clear 覆盖**：`clear==1` 再排入 `data_out <= RESET_VALUE`，靠「最后赋值胜出」盖过前者。
- **上电初值**：由 `initial` 块设成 `RESET_VALUE`，写入 bitstream，上电时由硬件加载。

而 `Register_areset`（异步复位变体）多了一条「**最高优先级的异步复位**」通路，它的控制流多了一层：

```text
always @(posedge clock, posedge areset):
  if (areset==1)         data_out <= RESET_VALUE     // 异步复位，最高优先
  else
      if (clock_enable)  data_out <= data_in         // 同步写
      if (clear)         data_out <= RESET_VALUE     // 同步清，最后赋值胜出
```

注意 `Register_areset` 的敏感列表是 `posedge clock, posedge areset`——**两个事件**。这带来一个根本性的麻烦，下一小节细说。

#### 4.3.3 源码精读

先看 `Register.v` 的「前置事项」：开头 `` `default_nettype none ``（[u2-l1](./u2-l1-verilog2001-and-default-nettype.md)），模块头里 `WORD_WIDTH` 默认为 `0`（[u2-l2](./u2-l2-parameterization-and-widths.md) 的「吵闹失败」约定），`RESET_VALUE` 也默认 `0`：

[Register.v:L39-L52](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L39-L52) —— `Register.v` 的模块头：`default_nettype none`、`WORD_WIDTH=0`/`RESET_VALUE=0` 的参数默认值、端口方向与 `wire`/`reg` 类型。

因为 `data_out` 是 `output reg`（[u2-l1](./u2-l1-verilog2001-and-default-nettype.md) 讲过：`reg` 输出端口不能在声明处初始化），所以紧跟一个 `initial` 块设上电初值：

[Register.v:L54-L56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L54-L56) —— `data_out` 的上电初值由 `initial` 块设为 `RESET_VALUE`，写入 bitstream。

至此 `Register.v` 的全貌齐了：参数化位宽 + 上电初值 + 同步 `clock_enable`/`clear`，全部封装在一个模块里。**没有 `areset`**——这正是作者「默认不带异步复位」取舍的体现。

再看 `Register_areset.v`。它的模块头比 `Register` 多一个 `areset` 输入；有意思的是它的 `WORD_WIDTH` 默认值是 `32` 而不是 `0`（与全书的「默认 0」约定不一致，可能是历史遗留，实例化时仍应显式设参）：

[Register_areset.v:L53-L65](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L53-L65) —— `Register_areset` 的模块头：比 `Register` 多一个 `areset` 输入；注意 `WORD_WIDTH` 默认为 32（与 `Register.v` 的默认 0 不同）。

最关键的部分来了：**为什么 `Register_areset` 不能像 `Register` 那样用「两个并列 `if`」实现最后赋值胜出？** 因为它的敏感列表里**同时有 `posedge clock` 和 `posedge areset` 两个事件**，如果还写成两个并列 `if`，仿真器/综合器**无从判断每个 `if` 该响应敏感列表里的哪一个事件**——于是无法正确推断出「异步复位」的硬件。作者用一大段注释解释了这个困境：

[Register_areset.v:L71-L87](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L71-L87) —— 解释为什么这里不能用「最后赋值胜出」的并列 `if`：敏感列表里有两个事件时，无法确定每个 `if` 响应哪一个；正确做法是用嵌套 `if` 显式表达异步复位的优先级。并指出这是全书极少数需要异步信号进敏感列表、或需要显式结构优先级的地方。

于是 `Register_areset` 改用**嵌套 `if`** 来结构性地表达优先级：最外层先判 `areset`，命中就复位；否则进 `else`，里面再用「最后赋值胜出」的并列 `if` 处理 `clock_enable` 和 `clear`：

[Register_areset.v:L89-L102](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L89-L102) —— `Register_areset` 的时钟块：`always @(posedge clock, posedge areset)`，外层 `if(areset)...else...` 表达异步复位最高优先级，内层用并列 `if` 处理 `clock_enable`/`clear`。

```verilog
always @(posedge clock, posedge areset) begin
    if (areset == 1'b1) begin
        data_out <= RESET_VALUE;          // 异步复位：最高优先
    end
    else begin
        if (clock_enable == 1'b1) begin
            data_out <= data_in;          // 同步写
        end

        if (clear == 1'b1) begin
            data_out <= RESET_VALUE;      // 同步清：最后赋值胜出
        end
    end
end
```

规范正文里有一段专门讲「异步复位 + clock_enable」为何必须这样写，并强调这「极可能是你唯一需要把异步信号写进敏感列表、或需要显式表达结构优先级的地方」，以及「即便触发器复位硬件是异步的，喂给它的复位信号也应当是同步的」：

[verilog.html:L839-L860](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L839-L860) —— 异步复位硬件与 clock_enable 推断的冲突：「最后赋值胜出」在此失效，必须用嵌套 `if` 结构性地表达复位优先级；且复位信号本身应同步，否则可能在下级寄存器亚稳态窗口附近翻转。

> **小结这条规则**：同步复位（`Register`）用**并列 `if` + 最后赋值胜出**；异步复位（`Register_areset`）因为敏感列表有两个事件，被迫用**嵌套 `if`** 显式表达优先级。前者是常态，后者是例外。

#### 4.3.4 代码实践

**实践目标**：对比 `Register.v` 与 `Register_areset.v` 两个时钟块，弄清「何时该选哪个」，并理解异步复位触发时的行为差异。

**操作步骤**：

1. 并排打开 [Register.v:L65-L73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L65-L73) 和 [Register_areset.v:L89-L102](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L89-L102)。
2. 列出两者的三点不同：（a）敏感列表；（b）`if` 是并列还是嵌套；（c）有没有 `areset` 输入。
3. 设想一个场景：`clock_enable=1`、`data_in=NEW`、当前 `data_out=OLD`，**在两个相邻时钟边沿之间**突然拉高 `areset`。分别推断两个模块里 `data_out` 何时变成 `RESET_VALUE`。
4. 决策：一个普通数据通路寄存器该选哪个？一个 PLL 复位相关的寄存器呢？

**需要观察的现象**：

- `Register`：`areset` 拉高**不会**立即生效（它根本没这个端口）；只有 `clear` 在**下个时钟边沿**才把 `data_out` 清成 `RESET_VALUE`。
- `Register_areset`：`areset` 拉高**立即**（不等时钟边沿）把 `data_out` 变成 `RESET_VALUE`，因为它在敏感列表里。

**预期结果**：

- 普通数据通路寄存器 → 选 `Register`（无异步复位，不抑制重定时，行为可预期）。
- PLL 复位、或控制逻辑可能卡死需要硬掰的关键寄存器 → 选 `Register_areset`，且 `areset` 信号要先经同步器同步到本时钟域。

> **待本地验证**：用仿真器对 `Register_areset` 施加「时钟运行中拉高 `areset`」的激励，观察 `data_out` 是否在 `areset` 拉高的当拍（而非下个时钟边沿）就跳变成 `RESET_VALUE`——这正是异步复位「立即生效」的直观体现，也是它容易引发仿真怪象的根源。

#### 4.3.5 小练习与答案

**练习 1**：为什么作者说「连一个寄存器都做成模块」是值得的？给出两条理由。

**参考答案**：（1）在最底层把**数据与控制分离**——复位属于控制，封装进 `Register` 后，上层的数据通路 `always @(*)` 只算数据，不再掺入复位细节，控制逻辑被收口、简化，布线资源也更省。（2）**复用与一致性**——全书成百上千个寄存器共用同一套经过推敲的复位规则（上电初值 + 同步 clear + 避免异步），杜绝各自手写出错。

**练习 2**：`Register_areset` 为什么不能像 `Register` 那样用「两个并列 `if` + 最后赋值胜出」来实现 `areset`？

**参考答案**：`Register_areset` 的敏感列表是 `posedge clock, posedge areset`，有**两个事件**。若仍用两个并列 `if`，工具**无法判断每个 `if` 该响应敏感列表里的哪一个事件**，因而无法正确推断出「异步复位」硬件。必须改用嵌套 `if`，把 `areset` 放在最外层，**结构性地**表达它对 `clock_enable`/`clear` 的优先级。

---

## 5. 综合实践

把本讲三块内容（三种复位 / 最后赋值胜出 / Register 封装）串起来做一个选型与设计任务。

**任务背景**：你要设计一个状态标志寄存器 `flag`，需求如下：

1. 上电时 `flag` 必须为 `0`（已知初值）。
2. 正常运行中，外部逻辑每拍可能给它一个新值 `flag_next`，由 `flag_enable` 控制是否更新。
3. 出现致命错误时，要把 `flag` 立即清回 `0`。
4. 错误信号 `fatal_error` 来自**另一个时钟域**（异步于本模块时钟）。

**请完成**：

**(a) 选型**：这个 `flag` 该用 `Register` 还是 `Register_areset`？给出理由。

**(b) 复位信号处理**：如果要用到异步复位，`fatal_error` 能不能直接接到 `areset` 上？应该怎么处理？（提示：回看 4.1.5 练习 2 和 [verilog.html:L857-L860](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L857-L860)。）

**(c) 实例化与等价手写**：写出用所选模块实例化 `flag` 的代码（设好 `WORD_WIDTH`、`RESET_VALUE`），再**手写**一个与之等价的、用「最后赋值胜出」惯用法的时钟 `always` 块，并确认你用的是**并列 `if` 而非三元**（说明为什么）。

**参考要点**：

- (a) 因为 `fatal_error` 跨时钟域且要求「立即」清零，倾向用 `Register_areset`；但若你愿意把 `fatal_error` 先同步到本时钟域、并接受「下个时钟边沿才清零」，则用普通 `Register` + 同步 `clear` 更好（不抑制重定时、无仿真怪象）。**本书默认推荐后者**：除非控制逻辑可能卡死，否则用 `Register`。
- (b) 不能直接接。`fatal_error` 异步于本时钟域，直接接 `areset` 会在撤销时刻引发亚稳态。应先用同步器（如 `CDC_Bit_Synchronizer` / `Reset_Synchronizer`）把 `fatal_error` 同步到本时钟域，再喂给 `areset`（或 `clear`）。
- (c) 示例（**示例代码**，非项目原有）：

  ```verilog
  // 用 Register 实例化（推荐：把 fatal_error 同步后接到 clear）
  Register
  #(
      .WORD_WIDTH  (1),
      .RESET_VALUE (1'b0)
  )
  flag_reg
  (
      .clock        (clock),
      .clock_enable (flag_enable),
      .clear        (fatal_error_synced), // 经同步器同步后
      .data_in      (flag_next),
      .data_out     (flag)
  );

  // 等价手写：最后赋值胜出，用并列 if（不能用三元！）
  always @(posedge clock) begin
      if (flag_enable == 1'b1) begin
          flag <= flag_next;            // (A)
      end

      if (fatal_error_synced == 1'b1) begin
          flag <= 1'b0;                 // (B) clear 覆盖
      end
  end
  ```

  **为什么不能用三元**：若把 (B) 写成 `flag <= (fatal_error_synced == 1'b1) ? 1'b0 : flag;`，则 `fatal_error_synced=0` 时它会排入 `flag <= flag(旧值)`，在「最后赋值胜出」下盖掉 (A) 的 `flag_next`，导致 `flag_enable` 失效、寄存器采不进新值（见 4.2.2 的追踪）。

**完成后自检**：

- 是否说清了三种复位各自的来历与适用场合？
- 「最后赋值胜出」写法是否用了**并列 `if`**（不是 `if/else`、不是三元）？
- 是否解释了为什么 `Register_areset` 必须用嵌套 `if`？
- 跨时钟域的复位信号是否经过了同步？

> **待本地验证**：把 (c) 的两种写法（实例化 vs 手写）放进同一个测试桩，施加相同的 `flag_enable`/`flag_next`/`fatal_error_synced` 激励，比对 `flag` 波形是否完全一致。

## 6. 本讲小结

- **三种「复位」要分清**：**上电复位**由 bitstream 免费、一次性给出初值（设好 `initial`/声明初值即可）；**同步 clear** 在时钟边沿按需清零，逻辑会被折叠进前级；**异步 areset** 立即生效，会引发仿真怪象、抑制寄存器重定时、放大复位树——本书「能不用就不用」，仅用于时钟未就绪或控制逻辑卡死的场合。
- **复位信号要同步**：即便用异步复位，喂给 `areset` 的信号也应先经同步器同步到本时钟域，否则在亚稳态窗口附近翻转会出问题。
- **「最后赋值胜出」复位惯用法**：先正常赋值、再用 `if (reset)` 覆盖复位值，只把真正需要复位的寄存器卷进复位树，避免 `if/else` 写法强迫全员复位。
- **复位是少数必须用 `if` 不能用三元的地方**：非阻塞赋值 `<=` 右侧读旧值，三元在条件不成立时会排入「写回旧值」，在最后赋值胜出语义下盖掉前一句有用的赋值；`if` 在条件不成立时什么都不排，前一句得以保留。承接 [u3-l1](./u3-l1-assignments-and-ternary.md)「三元优于 if/else」的边界。
- **`Register` 把这些取舍封装进构建块**：在最底层分离数据与控制，全书寄存器共用一套复位规则；默认 `Register` 不带异步复位，`Register_areset` 才是异步复位变体。
- **同步用并列 `if`，异步用嵌套 `if`**：`Register` 的敏感列表只有 `posedge clock`，用并列 `if` + 最后赋值胜出；`Register_areset` 敏感列表有 `posedge clock, posedge areset` 两个事件，必须用嵌套 `if` 显式表达异步复位优先级——这是全书极少数需要异步信号进敏感列表的地方。

## 7. 下一步学习建议

- 想看 `Register` 家族的其他变体（如 `Register_Toggle`），以及它们如何用同一套封装思想支持不同的控制信号，读 [u6-l1 Register 家族](./u6-l1-register-family.md)。
- 本讲反复提到的「复位信号要先经同步器同步到本时钟域」，其背后的同步器在 [u13-l2 复位同步与标志同步](./u13-l2-reset-and-flag-sync.md)（`Reset_Synchronizer` / `CDC_Bit_Synchronizer`）里详细讲解，建议接着读。
- 想看 `Register` 在真实复杂模块里如何被批量实例化（上一讲提到的 `Pipeline_Skid_Buffer` 五个寄存器全是它），可翻 [u10-l1 Skid Buffer 与 COTTC FSM 方法](./u10-l1-skid-buffer-fsm.md)，把 `Pipeline_Skid_Buffer.v` 完整读一遍。
- 若你想知道规范里提到的「复位排序（reset sequencing）」是怎么用计数器延迟上电复位副本的，可读 `verilog.html` 的 Reset Sequencing 一节（[L903-L910](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L903-L910)）。
