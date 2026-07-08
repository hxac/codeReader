# Register 家族

## 1. 本讲目标

本讲把「寄存器」从单个模块抬升为一个**家族**来看。学完后你应该能够：

- 说清 `Register`、`Register_Toggle`、`Register_areset` 三个变体的**接口差异**（各自有哪些控制信号）。
- 理解 `Register_Toggle` 是如何**在 `Register` 之上再搭一层**拼出来的（构建块库的典型复用）。
- 解释 `Register_areset` 为什么必须用**嵌套 `if`**，而 `Register` 用**并列 `if`**。
- 在面对一个具体设计时，能判断**该选家族里的哪一个**。

> 说明：关于「复位哲学」「last-assignment-wins 惯用法」「为什么复位必须用 `if` 而不能用三元」的深入推导，已经在上一讲 [u3-l2 复位哲学与 Register 模块](./u3-l2-resets-and-register-module.md) 里讲透。本讲**不再重复**那些推导，而是承接它，聚焦在「三个变体作为一个家族如何选用与组合」。

## 2. 前置知识

阅读本讲前，你最好已经掌握（来自更早的讲义）：

- **Verilog-2001 可综合子集与 `default_nettype none`**（u2-l1）：每个文件开头关闭隐式线网，端口里的 `wire`/`reg` 含义。
- **参数化与位宽**（u2-l2）：`parameter` 默认值为 `0` 的「吵闹失败」护栏、`{WORD_WIDTH{1'b0}}` 复制构造定宽常量。
- **赋值风格**（u3-l1）：组合块用阻塞 `=`、时钟块用非阻塞 `<=`、三元优于 `if/else`。
- **复位哲学**（u3-l2）：上电复位 / 同步 `clear` / 异步 `areset` 三种来历，以及「最后赋值胜出」惯用法。

两个本讲会反复用到的小结论，先放在这里：

1. **非阻塞赋值 `<=` 的右侧读的是旧值**。这正是复位「必须用 `if`、不能用三元」的根因——若写成 `data_out <= (clear==1'b1) ? RESET_VALUE : data_out;`，当 `clear` 为 0 时这句会排入「写回旧值」，盖掉前面那句 `data_out <= data_in;`。
2. **把寄存器封装成模块，是为了在最底层分离「数据」与「控制」**。`clear`、`clock_enable`、`areset` 这些都属于控制信号，把它们收进一个模块，周围逻辑就更干净。

## 3. 本讲源码地图

本讲涉及三个文件，恰好是「基础块 → 派生块 → 特殊变体」的递进关系：

| 文件 | 角色 | 一句话作用 |
| --- | --- | --- |
| [Register.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v) | 家族基座 | 最普通的同步寄存器：`clock_enable` + 同步 `clear`，无异步复位。 |
| [Register_Toggle.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Toggle.v) | 派生块 | 在 `Register` 之上加一个 2:1 选择器和取反，实现「按 `toggle` 翻转」。 |
| [Register_areset.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v) | 特殊变体 | 带**异步复位** `areset` 的寄存器，用于「控制逻辑可能卡死、必须强行复位」的少数场合。 |

阅读时请把这三个文件并排打开，注意对比它们的**端口列表**和**`always` 块写法**。

## 4. 核心概念与源码讲解

### 4.1 Register

#### 4.1.1 概念说明

`Register` 是整个家族的**基座**：一个最朴素的同步寄存器。它解决的问题是——**把「存一个字」这件最小的事，连同它需要的控制信号，封装成一个有名字的模块**。

为什么连这么简单的东西都要做成模块？源码注释说得很直白：

> 这样做能在最底层分离数据与控制（包括各种复位，复位属于控制）。这种分离让我们简化控制逻辑、减少一些布线资源。

它的接口只有 5 个信号：

- `clock`：时钟。
- `clock_enable`：时钟使能，决定本拍是否载入新数据。
- `clear`：同步清零（在时钟边沿生效），把寄存器拉回 `RESET_VALUE`。
- `data_in`：输入数据。
- `data_out`：输出数据（`reg`，因为来自本模块自己的 `always` 块）。

注意它**刻意没有异步复位**。原因在 u3-l2 讲过：异步复位即便常接 0，也会**抑制寄存器重定时（register retiming）**，而重定时是提升时序的关键优化。所以默认变体不要它。

#### 4.1.2 核心流程

每个时钟上升沿，`Register` 按如下顺序处理（「最后赋值胜出」）：

```text
posedge clock:
    if (clock_enable == 1)  data_out <= data_in      // ① 载入新值
    if (clear == 1)         data_out <= RESET_VALUE  // ② 清零，可能覆盖 ①
```

两条 `if` 是**并列**的，不是 `else if`。因为是非阻塞赋值，它们都在时间步末尾统一生效，**后一句的赋值会胜出**。于是得到自然的优先级：`clear` 优先于 `clock_enable`。

优先级真值表（`clock_enable` 与 `clear` 的组合）：

| `clock_enable` | `clear` | 本拍后 `data_out` |
| :---: | :---: | :--- |
| 0 | 0 | 保持不变 |
| 1 | 0 | 载入 `data_in` |
| 0 | 1 | `RESET_VALUE` |
| 1 | 1 | `RESET_VALUE`（`clear` 胜出） |

上电初值由 `initial` 块在仿真/综合阶段设定为 `RESET_VALUE`，运行时也可通过对 `clear` 拉一拍回到这个值——这就是「上电复位免费、且运行时可回」的体现。

#### 4.1.3 源码精读

模块头部遵循本书约定：`default_nettype none` 开头，参数默认值都是 `0`（必须实例化设参数才能综合），`data_out` 是 `output reg`：

[Register.v:41-52](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L41-L52) —— 模块声明与端口：注意 `WORD_WIDTH` 默认 `0`，`RESET_VALUE` 默认 `0`，`data_out` 为 `reg`。

[Register.v:54-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L54-L56) —— 用 `initial` 给 `output reg` 赋上电初值。因为 `reg` 端口不能在声明处初始化（那是 SystemVerilog 才允许），所以紧跟一个 `initial` 块。

[Register.v:65-73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L65-L73) —— 这是整个模块的心脏：单时钟沿、两条并列 `if`，靠「最后赋值胜出」让 `clear` 自然优先。**注意这里不能用三元运算符**（注释 58-63 行解释了原因，详见 u3-l2）。

#### 4.1.4 代码实践

**实践目标**：通过阅读接口，预测 `clock_enable` 与 `clear` 同时为 1 时的行为，验证「`clear` 胜出」。

**操作步骤（源码阅读型 + 实例化）**：

1. 想象你写了下面这段实例化（**示例代码**，非项目原文件）：

   ```verilog
   Register #( .WORD_WIDTH(8), .RESET_VALUE(8'h00) ) my_reg (
       .clock        (clk),
       .clock_enable (1'b1),   // 常常使能
       .clear        (clear_sig),
       .data_in      (8'hAA),
       .data_out     (q)
   );
   ```

2. 设定一个场景：`clear_sig` 在某拍为 `1`，同时 `data_in` 是 `8'hAA`。

**需要观察的现象**：

- 在 `clear_sig == 1` 的那一拍**之后**，`q` 应为 `8'h00`（`RESET_VALUE`），而不是 `8'hAA`。
- `clear_sig` 撤销后的下一拍，`q` 才变为 `8'hAA`。

**预期结果**：`clear` 优先于 `clock_enable`，与 4.1.2 的真值表一致。

> 待本地验证：如果你有 Icarus Verilog，可写一个最小 testbench 用 `Simulation_Clock`（见 u18-l2）驱动 `clear_sig`，在波形上确认上述两个时刻的 `q` 值。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Register.v:65-73` 的两条并列 `if` 改成 `if/else if`（`clear` 在前，`clock_enable` 在 `else`），功能会变吗？

**答案**：功能不变。`clear` 仍优先，`clock_enable` 仅在 `clear==0` 时生效。但本书偏好并列写法，因为它更清晰地表达「各控制信号独立、靠赋值顺序决定优先级」，便于增删控制信号时不破坏结构。

**练习 2**：为什么 `Register` 的参数默认值是 `0`，而不是某个「合理」的位宽（比如 32）？

**答案**：这是本书的统一护栏（见 u1-l2）：默认 `0` 会让 `[WORD_WIDTH-1:0]` 退化为非法的 `[-1:0]`，在精化（elaboration）阶段吵闹地失败，逼你实例化时显式设 `WORD_WIDTH`，杜绝静默用错位宽。下一节的 `Register_areset` 偏离了这条约定，恰好是个反例。

---

### 4.2 Register_Toggle

#### 4.2.1 概念说明

`Register_Toggle` 是家族里**最值得学的一块**，因为它示范了「构建块库」的核心玩法：**不重写一个新寄存器，而是在 `Register` 之上再搭一层**。

它的功能：当 `toggle` 为 1 时，本拍输出**取反**（`~data_out`）；当 `toggle` 为 0 时，行为退化成普通 `Register`（载入 `data_in`）。源码注释把它描绘成一个**两态小 FSM**：

- `clear`：回到起始态；
- `clock_enable`：控制何时允许跳转；
- `data_in`：强行置成某个数据相关的状态；
- `toggle`：在**不知道当前是哪个态**的情况下，切到另一个态。

这个「不需要知道当前态就能翻转」的能力，把「信号**出现**」型事件转换成「信号**变化**」型事件——这正是**2 相握手（2-phase handshake）**和**脉冲跨时钟域**的基础。本书里 `CDC_Pulse_Synchronizer_2phase`、`CDC_Word_Synchronizer`、`CDC_FIFO_Buffer`、`Pipeline_FIFO_Buffer` 等多个模块都实例化了它。

#### 4.2.2 核心流程

`Register_Toggle` 的内部结构是「**组合一层选择逻辑 + 复用一个 `Register`**」：

```text
           ┌─────────────────────────────────────┐
data_in ──┐ │                                     │
          ├─►│  new_value = toggle ? ~data_out :  │──► Register ──► data_out
toggle  ──┘ │                  data_in            │     (复用)
           └─────────────────────────────────────┘
                 ▲                                  │
                 └──────────── data_out 反馈 ───────┘
```

关键组合表达式只有一行：

\[ \text{new\_value} = \begin{cases} \sim \text{data\_out} & \text{若 } toggle=1 \\ \text{data\_in} & \text{若 } toggle=0 \end{cases} \]

随后把 `new_value` 当作普通数据喂给内部那个 `Register`。于是：

- `clock_enable=0` 时：`Register` 不载入，输出保持（不翻转）。
- `clock_enable=1, toggle=1` 时：载入 `~data_out`，输出翻转。
- `clock_enable=1, toggle=0` 时：载入 `data_in`，退化成普通寄存器。
- `clear=1` 时：回到 `RESET_VALUE`（来自内部 `Register`）。

注意 `data_out` 在这里是 `output **wire**`（不是 `reg`），因为它来自内部 `Register` 实例的输出，而非本模块自己的 `always` 块——这是 u2-l1「`output wire` 表示来自子模块实例」规则的活样本。

#### 4.2.3 源码精读

[Register_Toggle.v:47-59](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Toggle.v#L47-L59) —— 比基座多了一个 `toggle` 输入；`data_out` 是 `wire`（来自内部实例）。

[Register_Toggle.v:61](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Toggle.v#L61) —— 用 `{WORD_WIDTH{1'b0}}` 复制构造给内部 `reg new_value` 一个定宽初值（u2-l2 的惯用法）。

[Register_Toggle.v:63-75](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Toggle.v#L63-L75) —— **直接实例化 `Register`**！把 `new_value` 接到 `.data_in`，把 `clock/clock_enable/clear` 透传。这就是「派生块复用基座」的全部秘密：寄存器本身的时序、复位、使能逻辑一行都不重写。

[Register_Toggle.v:77-79](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Toggle.v#L77-L79) —— 组合块（阻塞赋值，符合 u3-l1 规则）算出 `new_value`。这里**可以**用三元，因为它是组合逻辑赋值，不涉及非阻塞的「读旧值」陷阱。

**真实使用范例**：在 [CDC_Pulse_Synchronizer_2phase.v:138-151](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L138-L151) 中，`Register_Toggle` 被实例化为 `start_handshake`：把 `toggle` 接输入脉冲、`data_in` 接自己的输出 `sending_toggle`（反馈环）。每来一个脉冲，输出电平就翻转一次——把「脉冲出现」事件转成了「电平跳变」事件，供对端时钟域用边沿检测识别。

#### 4.2.4 代码实践

**实践目标**：用 `Register_Toggle` 实现一个「按 `enable` 翻转的标志位」——即 `enable` 为 1 的每一拍，输出都翻转一次，相当于一个 1 位计数器/二分频。

**操作步骤**：

1. 新建一个顶层（**示例代码**），实例化 `Register_Toggle`，把 `enable` 接到两个地方：

   ```verilog
   `default_nettype none
   module Toggle_Flag
   #(
       parameter RESET_VALUE = 1'b0
   )(
       input  wire clock,
       input  wire enable,    // 为 1 的每拍都翻转
       input  wire clear,
       output wire flag       // 翻转标志
   );

       Register_Toggle
       #(
           .WORD_WIDTH     (1),
           .RESET_VALUE    (RESET_VALUE)
       )
       toggle_flag
       (
           .clock          (clock),
           .clock_enable   (enable),   // 用 enable 当使能
           .clear          (clear),
           .toggle         (1'b1),     // 始终允许翻转
           .data_in        (1'b0),     // toggle=1 时 data_in 无效，给任意值即可
           .data_out       (flag)
       );

   endmodule
   ```

   关键点：`toggle` 恒为 `1`（每拍都想翻转），用 `clock_enable` 来控制「哪几拍翻转」。于是 `enable` 为 1 的每拍，`flag` 都会取反——这正是「按 enable 翻转的标志位」。

**需要观察的现象**：

- `enable` 持续为 1 时，`flag` 是 `clock` 的二分频方波。
- `enable` 为 0 时，`flag` 冻结在当前值。
- `clear` 拉一拍后，`flag` 回到 `RESET_VALUE`。

**预期结果**：`flag` 的翻转频率 = `enable` 有效期间的时钟频率 / 2。

> 待本地验证：可用 `Simulation_Clock`（u18-l2）+ `enable` 脉冲驱动，在波形上数翻转次数，确认二分频关系。

#### 4.2.5 小练习与答案

**练习 1**：`Register_Toggle` 的 `data_out` 为什么是 `wire` 而 `Register` 的是 `reg`？

**答案**：因为 `Register_Toggle` 的输出来自它内部实例化的 `Register`（一个子模块实例），按 u2-l1 的约定 `output wire` 表示「来自子模块实例」；而 `Register` 的输出来自自身 `always` 块的 `reg`，所以是 `output reg`。

**练习 2**：如果想让输出**每 4 拍**翻转一次（四分频），只改这一个模块够吗？

**答案**：不够。`Register_Toggle` 只能在 `clock_enable` 为 1 的当拍翻转。要四分频，需要在它前面再加一个「每 2 拍输出一个使能脉冲」的逻辑（例如另一个 `Register_Toggle` 做二分频，再用 `Pulse_Generator` 提取沿），或者直接级联两个二分频。这正是源码注释里「链式多个 toggle 拼成无加法器的计数器」的思路。

---

### 4.3 Register_areset

#### 4.3.1 概念说明

`Register_areset` 是家族里**带异步复位**的特殊变体。它的存在是为了应对一种少数但真实的情况：**控制逻辑可能卡死，必须用一个不依赖时钟的信号把寄存器强行拽回初值**。

本书对它的态度是「能不用就不用」（注释里黑体强调），理由有二：

1. 异步复位在仿真中会引发怪象（行为仿真看似漏采数据、带时序的后仿里出现「不可能的」变化）。
2. **即便常接 0，异步复位的存在本身就会抑制寄存器重定时**，拖累时序。

所以默认请用 `Register`；只有 ASIC 流程或极个别关键寄存器才用 `Register_areset`。更有意思的事实是：**全书没有任何模块实例化 `Register_areset`**——它只出现在别处的注释里作为指引。连 `Reset_Synchronizer` 都明确说「无法实例化它」，因为同步器必须把 `ASYNC_REG` 等属性直接写到裸 `reg` 上（属性随声明走，见 u4-2）。这本身就印证了「尽量别用」。

> 一个值得注意的观察：`Register_areset` 的 `WORD_WIDTH` 默认值是 **32**（见下文源码），而不是本书统一的 `0`。这意味着它**可以直接综合**出一个 32 位寄存器而不会吵闹失败——这与 u1-l2 建立的「默认 0 护栏」约定不一致，可能是历史遗留。实例化时仍应显式写清 `WORD_WIDTH`，别依赖这个默认值。

#### 4.3.2 核心流程

异步复位的关键在于**敏感性列表里有两个事件**：

```text
always @(posedge clock, posedge areset)   // 时钟沿 OR 复位沿，都可能触发
```

正因为有两个触发源，我们就**无法再用「并列 `if` + 最后赋值胜出」**来区分「这条 `if` 该响应哪个事件」——因为 `always` 块一旦被任一沿触发就整体执行，并列 `if` 看不出是谁触发的。因此必须用**嵌套 `if`** 显式表达优先级：

```text
posedge clock 或 posedge areset:
    if (areset == 1)          // ① 异步复位，最高优先
        data_out <= RESET_VALUE
    else
        if (clock_enable == 1) data_out <= data_in   // ② 同步载入
        if (clear == 1)        data_out <= RESET_VALUE  // ③ 同步清零（最后赋值胜出）
```

注意：只有最外层那条 `if (areset)` 是「表达优先级」的嵌套；内层 `clock_enable` 与 `clear` 之间仍然回到了「并列 `if` + 最后赋值胜出」。这是本书对 u3-l2 惯用法的精确应用：**优先级用嵌套 `if`，平级控制用并列 `if`**。

#### 4.3.3 源码精读

[Register_areset.v:53-65](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L53-L65) —— 比基座多一个 `areset` 输入；注意第 55 行 `WORD_WIDTH = 32`（与其他两个变体的 `0` 不同，见 4.3.1 的观察）。

[Register_areset.v:67-69](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L67-L69) —— 同样用 `initial` 设上电初值。

[Register_areset.v:71-87](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L71-L87) —— 这段注释是全书少见的「解释为什么不能用惯用法」的地方：当复位异步地出现在敏感性列表里时，多个 `if` 没法判断各自该响应哪个事件，因此必须用嵌套 `if` 显式表达结构优先级。注释还指出，这**很可能是你唯一需要异步信号进敏感性列表、或需要显式结构优先级的地方**。

[Register_areset.v:89-102](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L89-L102) —— 对比 `Register.v:65-73`：敏感性列表是 `posedge clock, posedge areset`；最外层 `if (areset)` 决定「是复位沿还是时钟沿」，`else` 分支内才是 `clock_enable` 与 `clear` 的并列 `if`（沿用最后赋值胜出）。

#### 4.3.4 代码实践

**实践目标**：对比 `Register` 与 `Register_areset` 的**敏感性列表与 `if` 结构**，理解为什么后者必须嵌套。

**操作步骤（源码阅读型对比）**：

1. 并排打开 [Register.v:65-73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L65-L73) 和 [Register_areset.v:89-102](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_areset.v#L89-L102)。
2. 在心里把 `Register_areset` 的敏感性列表**改回** `always @(posedge clock)`，但**保留**嵌套 `if` 结构——问自己：功能还对吗？
3. 反过来，把 `Register_areset` 的嵌套 `if` **改成** 4.1 里那种并列 `if`（三条并列：`areset`、`clock_enable`、`clear`），敏感性列表保持 `posedge clock, posedge areset`——问自己：综合出来的还是「异步复位优先」的触发器吗？

**需要观察的现象 / 思考结论**：

- 第 2 步：敏感性列表只剩时钟沿后，`areset` 不再能异步触发，整个块只在时钟沿执行——`areset` 退化成了同步信号，异步复位能力丢失。说明**异步能力来自敏感性列表里的 `posedge areset`**。
- 第 3 步：并列 `if` 无法表达「`areset` 一旦为 1 就立刻、优先地复位」。综合器看到敏感性列表里两个事件却无法从结构上判断每条 `if` 归谁，可能推断出非预期的锁存或错误的优先级。这正是源码注释坚持用嵌套 `if` 的原因。

**预期结果**：能用自己的话讲清——「异步复位必须同时满足两点：① `posedge areset` 进敏感性列表（提供触发源）；② 嵌套 `if (areset)` 在最外层（表达优先级）。二者缺一不可。」

> 待本地验证：若用 Vivado/Quartus，可分别综合「正确版」与「并列 if 版」，对比综合报告里复位端口是否接到触发器的异步复位端（`PRE`/`CLR` 或 `R`）。

#### 4.3.5 小练习与答案

**练习 1**：`Register_areset` 里 `clock_enable` 和 `clear` 为什么不用嵌套 `if` 区分，而用并列 `if`？

**答案**：因为它们都是**同步**信号——只在时钟沿（`else` 分支内）才被处理，触发源相同（都是 `posedge clock`），不需要用结构区分「谁触发的」。它们之间是平级的控制信号，用「并列 `if` + 最后赋值胜出」即可让 `clear` 自然优先，与基座 `Register` 完全一致。嵌套 `if` 只用于区分异步 `areset` 与时钟沿这两个**不同的触发源**。

**练习 2**：为什么本书宁愿写 `Register_areset` 这个单独模块，也不在 `Register` 上加一个可选的 `areset` 端口？

**答案**：因为异步复位端口**即便常接 0 也会抑制寄存器重定时**（见 4.3.1）。如果把 `areset` 做进 `Register`，那么每一个用 `Register` 的地方都会背上这个代价。把它拆成独立模块，让默认的、绝大多数场合使用的 `Register` 保持「无异步复位、可重定时」的干净属性，只有真正需要的人才付出代价。这是「数据/控制分离 + 默认安全」哲学的体现。

---

## 5. 综合实践

把三个变体串起来，做一次「**家族选型 + 拼装**」的小设计。

**任务背景**：你要做一个简单的事件计数指示器——每收到一个 `event` 脉冲，就翻转一次对外输出的 `led` 电平（用「电平变化」而非「脉冲」表示事件次数，方便慢速人眼或异域逻辑识别）；同时整个模块要能被一个外部按钮 `force_reset`（与 `clock` 异步）强行清零。

**要求**：

1. 判断 `led` 翻转这一功能该用家族里的哪个变体？（提示：需要「不知道当前态也能翻转」。）
2. 判断 `force_reset` 这个**异步**按钮接到哪里、需要怎样的同步处理？（提示：见 u13-l2 的复位同步；异步按钮不能直接进同步路径。）
3. 写出模块骨架（**示例代码**），用「基座 + 派生块」的方式拼装。

**参考思路**：

- `led` 翻转用 `Register_Toggle`：`toggle` 接 `event`，`clock_enable` 接 `1'b1`，`data_in` 接自身输出形成反馈（或直接用 4.2.4 的写法）。
- `force_reset` 是异步按钮，应先用 `Reset_Synchronizer`（其内部本质是 `Register_areset` 的思路）同步到 `clock` 域，再把同步后的复位接到 `Register_Toggle` 的 `clear`——**不要**直接用 `Register_areset` 承接按钮，因为复位信号本身必须先同步（u3-l2、u13-l2）。
- 这样既用到了 `Register_Toggle`（派生块），又理解了为什么默认不直接用 `Register_areset`，而是先做复位同步。

**验收**：能说清「`Register_Toggle` 负责『翻转』语义、`Register_areset` 负责『异步强行复位』语义、二者不可混用，且异步信号进同步域前必须先同步」——这就把本讲三个最小模块与上一讲的复位哲学真正串起来了。

> 待本地验证：完整 testbench 需要一个异步 `force_reset` 按钮模型和一个 `event` 脉冲源，可在掌握 u18-l2 的 cocotb 写法后补全。

## 6. 本讲小结

- `Register` 是家族基座：`clock_enable` + 同步 `clear`，**无异步复位**，用并列 `if` + 最后赋值胜出，刻意保持可重定时。
- `Register_Toggle` 示范了构建块库的核心玩法——**在 `Register` 之上加一层组合逻辑**（`toggle ? ~data_out : data_in`），不重写时序；`data_out` 因来自子实例而是 `wire`。
- `Register_Toggle` 把「信号出现」事件转成「信号变化」事件，是 2 相握手与脉冲跨域的基础，本书多个 CDC 模块都实例化了它。
- `Register_areset` 是带异步复位的特殊变体；因敏感性列表含两个事件（`posedge clock, posedge areset`），**必须用嵌套 `if`** 表达优先级，而内层同步信号仍用并列 `if`。
- 异步复位即便常接 0 也抑制重定时，故本书「能不用就不用」——事实上全书无任何模块实例化 `Register_areset`，复位应先经同步器再使用。
- 选型口诀：默认用 `Register`；需要翻转语义用 `Register_Toggle`；只有在控制逻辑可能卡死、必须异步强复位的极少数 ASIC/关键寄存器场合才用 `Register_areset`。

## 7. 下一步学习建议

本讲把「单个寄存器」讲完，自然的下一步是把寄存器**串成流水线**：

- **下一讲 [u6-l2 寄存器流水线](./u6-l2-register-pipeline.md)**：学习 `Register_Pipeline` / `Register_Pipeline_Simple` / `Register_Pipeline_Variable`，看如何用一串 `Register` 实现固定/可变深度的延迟。你会再次看到「基座复用」的模式。
- **未来 [u13-l2 复位同步与标志同步](./u13-l2-reset-and-flag-sync.md)**：回头看 `Reset_Synchronizer`，理解为什么它「无法实例化 `Register_areset`」、以及异步复位同步化的完整做法。
- **延伸阅读源码**：打开 [CDC_Pulse_Synchronizer_2phase.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v)，跟踪 `Register_Toggle` 如何与反馈环一起实现 2 相握手——这是本讲 `Register_Toggle` 的真实舞台。
