# 二进制计数器与可复用函数

## 1. 本讲目标

本讲是「整数算术」单元的第二篇，把上一讲的加减法器「用起来」——拼装出一个最常见的时序构件：**计数器**。学完后你应该能够：

- 说清为什么 `Counter_Binary` **不是一段手写的 `count <= count + 1`**，而是「一个加法器 + 几个寄存器」的组合，并能讲出这种拆分带来的好处。
- 读懂 `Counter_Binary` 的三套操作（`run` 计数 / `load` 载入 / `clear` 清零）及其**优先级**，会用 `INCREMENT` 实现「每拍计 N 个」「每 N 拍计一次」这类需求。
- 会用 `` `include "clog2_function.vh" `` 引入可复用函数，并用 `clog2(N)` 算出「给 N 个项目编址需要几位」。

本讲承接 [u8-l1 加减法、进位与算术谓词](./u8-l1-adder-predicates.md)（你已经认识 `Adder_Subtractor_Binary`）和 [u6-l1 Register 家族](./u6-l1-register-family.md)（你已经认识 `Register` 的 `clock_enable` / `clear` / 「最后赋值胜出」）。本讲就是把这二者**拼**到一起，并顺手讲清「函数怎么共享」这件工程小事。

## 2. 前置知识

阅读本讲前，你最好已经掌握（来自更早的讲义）：

- **参数化与位宽**（[u2-l2](./u2-l2-parameterization-and-widths.md)）：`parameter` 默认值为 `0` 的「吵闹失败」护栏、`{WORD_WIDTH{1'b0}}` 复制构造定宽常量、`localparam` 持有内部常量。
- **赋值风格与三元**（[u3-l1](./u3-l1-assignments-and-ternary.md)）：组合块 `always @(*)` 用阻塞 `=`、链式三元优于嵌套 `if/else`、「算 `*_next` + 时钟块存」范式。
- **构建块库**（[u4-l1](./u4-l1-modularization-and-building-blocks.md)、[u5-l1](./u5-l1-constant-annuller-reducer.md)）：用经过测试的小模块自底向上拼装，`Counter_Binary = Adder_Subtractor_Binary + Register` 正是这种思想的范本。

两个本讲会反复用到的小结论，先放在这里：

1. **计数器本质上是一个「反馈的加法」**。把当前值 `count` 接回加法器的 `A` 输入，把步长 `INCREMENT` 接到 `B` 输入，每个时钟把和存回 `count`——这就是计数。计数方向（加 / 减）不过是 `add_sub` 的选择。
2. **Verilog-2001 里函数只能定义在模块体内部**。所以可复用的纯计算函数（如 `clog2`）必须放进一个 `.vh` 文件，再用 `` `include `` 在模块体开头引入；为了能被多处包含，它还得是「幂等」的。

## 3. 本讲源码地图

本讲围绕两个主文件展开，并复用两个上一讲已讲过的模块：

| 文件 | 角色 | 一句话作用 |
| --- | --- | --- |
| [Counter_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v) | 主模块 | 二进制加 / 减计数器：用加法器算下一拍值，用寄存器存。 |
| [clog2_function.vh](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/clog2_function.vh) | 函数库 | \(\lceil \log_2 N \rceil\)：返回「给 N 个项目编址所需的位数」。 |
| [Adder_Subtractor_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v) | 复用块（u8-l1） | 计数器内部用它算 `count + INCREMENT`。 |
| [Register.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v) | 复用块（u6-l1） | 计数器内部用它存当前值与各标志位。 |

阅读时请把 `Counter_Binary.v` 与上面两个复用块并排打开，重点观察它**不写任何算术、也不写任何触发器**，只做「连线」。

## 4. 核心概念与源码讲解

### 4.1 「计数器 = 加法器 + 寄存器」的组合思想

#### 4.1.1 概念说明

很多人写计数器，习惯直接在 `always @(posedge clock)` 里写一句 `count <= count + 1;`。这当然能工作，但它把三件事揉在了一起：

- **算下一拍值**（`count + 1`，组合算术）；
- **决定何时更新**（时钟、使能、清零，控制）；
- **把值存起来**（触发器，存储）。

本书的做法是把这三件事**拆开**：「算」交给 `Adder_Subtractor_Binary`，「存」交给 `Register`，计数器模块自己只负责「连线」与「控制选择」。这正是 [u4-l1](./u4-l1-modularization-and-building-blocks.md) 讲的「数据 / 控制 / 接口分离」在最小尺度上的体现。

这么拆的好处，源码注释说得很直白：**换计数方案时只换加法器，不动其它逻辑**。比如想把二进制计数换成 BCD 计数、LFSR 计数，只要替换那个算「下一拍值」的子模块；同时也把「让 CAD 工具正确推断快速进位链」的位宽技巧藏进加法器，不污染计数器主逻辑。

#### 4.1.2 核心流程

计数器的数据通路可以画成一条「反馈环」：

```
        INCREMENT ──┐
                    ▼
 count ──►[ Adder_Subtractor ]──► incremented_count ──► (选择) ──► next_count
   ▲              │                                                      │
   │              └─► carry_out / carries / overflow (标志)              │
   │                                                                     ▼
   └─────────────────────────── [ Register (clock_enable/clear) ] ◄───────┘
                       (下一个时钟边沿把 next_count 存回 count)
```

要点：

- 加法器**永远在算** `count + INCREMENT`（或减），它是纯组合的，输出叫 `incremented_count`。
- 一个组合多路选择器在 `incremented_count` 与外部 `load_count` 之间选一个，得到 `next_count`。
- `Register` 在时钟边沿把 `next_count` 存成新的 `count`；`clock_enable` 控制「这一拍存不存」，`clear` 控制「这一拍强制回到初值」。
- 于是 `count` 被反馈回加法器 `A` 输入，形成闭环——这就是「计数」。

#### 4.1.3 源码精读

计数器一上来就把「算下一拍值」这件事整个交给加法器（[Counter_Binary.v:55-74](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L55-L74)）：把当前 `count` 接到 `A`、步长 `INCREMENT` 接到 `B`、`up_down` 接到 `add_sub`（0 加 / 1 减）。注意它实例化得是 `Adder_Subtractor_Binary`，复用了 u8-l1 讲过的全部进位 / 溢出计算。

随后是经典的「组合块算 `*_next` + 时钟块存」范式（[Counter_Binary.v:84-90](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L84-L90)）：用阻塞赋值算出 `next_count` 和一组 `load_*` / `clear_*` 使能。注意这里**没有**任何触发器——存储全部留给下面的 `Register`。

最后是存储环节，主计数用一个 `Register`（[Counter_Binary.v:94-106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L94-L106)）：`RESET_VALUE` 直接绑成 `INITIAL_COUNT`，所以上电与 `clear` 都回到同一个初值。「何时存」靠 `clock_enable = load_counter`、「何时清」靠 `clear = clear_counter` 表达，触发器本身的「最后赋值胜出」语义（见 [Register.v:65-73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L65-L73)）天然让 `clear` 优先于 `clock_enable`。

#### 4.1.4 代码实践

**实践目标**：亲手确认「计数器里没有任何算术、也没有任何触发器，只有连线」。

**操作步骤**：

1. 打开 [Counter_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v)。
2. 全文搜索 `+`、`-`、`<=`、`posedge`。
3. 数一数模块里共有几个 `Register` 实例、几个 `Adder_Subtractor_Binary` 实例。

**需要观察的现象**：

- 你**不会**在模块体内找到任何 `count <= count + 1` 之类的算术赋值，也不会找到任何 `always @(posedge clock)`——算术在加法器里、触发器在 `Register` 里。
- 你会找到 **1 个** `Adder_Subtractor_Binary`（算下一拍值）和 **4 个** `Register`（分别存 `count`、`carries`、`carry_out`、`overflow`）。

**预期结果**：计数器 = 1 个加法器 + 4 个寄存器 + 一段组合选择逻辑，自身零算术零触发器。这也解释了为什么「换计数方案只换加法器」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Adder_Subtractor_Binary` 换成一个算 BCD 加法的子模块（接口相同），计数器的控制逻辑要改吗？
**答案**：不用改。控制逻辑（`run` / `load` / `clear` 的选择与各 `Register` 的连线）与「下一拍值怎么算」是分离的，换加法器只换数据通路。

**练习 2**：为什么 `next_count` 要用组合块（`always @(*)` + 阻塞 `=`）算，而不是直接在时钟块里写？
**答案**：因为存储已经交给了 `Register`。组合块算出 `next_count` 后，由 `Register` 的 `clock_enable` 决定这一拍是否真的写入，符合「算 `*_next` + 时钟块存」范式，也避免了时序块里混用阻塞 / 非阻塞的陷阱。

---

### 4.2 Counter_Binary 模块：增量、初值与级联

#### 4.2.1 概念说明

理解了「加法器 + 寄存器」的骨架，本节把这个骨架「填满」成可用的计数器。`Counter_Binary` 用三个参数刻画它如何计数：

- `WORD_WIDTH`：计数值位宽（默认 `0`，**必须**实例化时设定，否则端口退化为非法的 `[-1:0]`——这是全书的「吵闹失败」护栏，见 [u1-l2](./u1-l2-repo-layout-and-conventions.md)）。
- `INCREMENT`：每拍加 / 减的步长（默认 `0`）。这是本模块最有用的参数——它让计数器可以「每拍计 N 个」，而不是只能 `+1`。
- `INITIAL_COUNT`：上电与 `clear` 回到的初值（默认 `0`）。

并暴露三套控制信号，优先级为 **`clear` > `load` > `run`**：`clear` 清回初值、`load` 载入一个给定值（即便 `run` 为 0）、`run` 才是正常计数。源码注释把 `load` 的优先级描述得很清楚：「Load overrides counting」。

#### 4.2.2 核心流程

记 `incremented_count = count ± INCREMENT`（由 `up_down` 选加减）。组合块用链式三元 / 逻辑表达式得到下面一组信号：

```
next_count    = load ? load_count      : incremented_count   // load 选载入值，否则选计数结果
load_counter  = run  || load                                  // 计数或载入都要写 count
clear_counter = clear                                      // 清零写 count
load_flags    = run                                         // 只有计数时才更新进位/溢出标志
clear_flags   = load || clear                                // 载入或清零都把标志清 0
```

于是在一个时钟边沿：

| 当前生效信号 | `count` 下一拍 | 标志（`carry_out`/`carries`/`overflow`）下一拍 |
| --- | --- | --- |
| `clear=1` | `INITIAL_COUNT` | 清 0 |
| `clear=0, load=1` | `load_count` | 清 0 |
| `clear=0, load=0, run=1` | `count ± INCREMENT`（可能回绕） | 更新为本次加法的标志 |
| 全为 0 | 保持不变 | 保持不变 |

关于回绕：源码注释说明，当计数越过 `0` 或 `(2^WORD_WIDTH)-1` 时会回绕，并在该周期置起溢出标志。无符号场景看 `carry_out`、有符号场景看 `overflow`（二者都来自加法器，见 [u8-l1](./u8-l1-adder-predicates.md) 关于 `overflow = carries[MSB] != carry_out` 的推导）。标志位为什么在 `load` / `clear` 时被清 0、只在 `run` 时更新？因为载入或清零的值并非「算出来的」，不携带任何进位 / 溢出含义。

**级联（chaining）**：当需要超大位宽计数器、或在不同进制下计数（每一位各用一个计数器）时，把前一个计数器的 `carry_out` 与下一个计数器的 `run` 相「与」即可；`carry_in` 与逐位 `carries` 则为更一般的级联保留。

#### 4.2.3 源码精读

模块头与参数（[Counter_Binary.v:23-45](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L23-L45)）：注意 `INCREMENT` 和 `INITIAL_COUNT` 的位宽都用 `[WORD_WIDTH-1:0]`，与计数值同宽，这样步长与初值都自动跟随 `WORD_WIDTH`。

`load` 覆盖计数的核心就在这一句三元（[Counter_Binary.v:85](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L85)）：`load` 为 1 时 `next_count` 取 `load_count`，否则取 `incremented_count`。而 `load_counter = run || load`（[Counter_Binary.v:86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L86)）保证即便 `run=0`、只要 `load=1` 也会写入。

标志位的「只在 `run` 时更新、在 `load`/`clear` 时清零」由 `load_flags = run` 与 `clear_flags = load || clear` 表达（[Counter_Binary.v:88-89](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L88-L89)），再交给后面三个 1 位 / 多位 `Register`（[Counter_Binary.v:108-148](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L108-L148)）存储。

真实使用范例可参看 `Pulse_Divider`：它把 `INCREMENT` 绑成 `WORD_ONE`（步长 1）、`INITIAL_COUNT` 绑成 `INITIAL_DIVISOR`（[Pulse_Divider.v:85-90](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L85-L90)），就是一个标准的「每拍 +1、从初值计起」的分频计数器。

#### 4.2.4 代码实践

**实践目标**：实例化 `Counter_Binary`，用 `INCREMENT` 实现「每拍计 4 个字节」的字节计数器（模拟一条每周期传 4 字节的总线）。

**操作步骤**（以下为**示例代码**，非项目原有文件）：

```verilog
// 示例代码：统计已传输字节数，总线每拍传 4 字节
localparam WORD_WIDTH   = 16;
localparam [15:0] FOUR  = 16'd4;

wire [15:0] byte_count;
wire        carry_out;
wire [15:0] carries;
wire        overflow;

Counter_Binary
#(
    .WORD_WIDTH     (WORD_WIDTH),
    .INCREMENT      (FOUR),         // 每拍 +4，而非 +1
    .INITIAL_COUNT  (16'd0)
)
byte_counter
(
    .clock          (clock),
    .clear          (1'b0),
    .up_down        (1'b0),          // 只增不减
    .run            (bus_valid),     // 总线有数据的那一拍才计数
    .load           (1'b0),
    .load_count     (16'd0),
    .carry_in       (1'b0),
    .carry_out      (carry_out),
    .carries        (carries),
    .overflow       (overflow),
    .count          (byte_count)
);
```

**需要观察的现象**：每个 `bus_valid` 为 1 的时钟边沿后，`byte_count` 增加 4（0→4→8→…），而不是增加 1；`run=0` 的拍 `byte_count` 保持不变。

**预期结果**：经过 *k* 个有效拍后 `byte_count == 4*k`（回绕前）。`INCREMENT` 让「每拍计 N 个」这件事在模块层面就解决了，外面无需再乘。

> 仿真波形需用 iverilog / Verilator / Vivado 等工具本地跑出，本讲不假设已运行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：若同时拉高 `run` 和 `load`，`count` 会变成什么？
**答案**：变成 `load_count`。因为组合块里 `next_count = load ? load_count : incremented_count`，`load` 优先；`load_counter = run || load` 也为真，所以会写入 `load_count`。即 **`load` 覆盖计数**。

**练习 2**：若同时拉高 `clear` 和 `load`，`count` 又会变成什么？
**答案**：变成 `INITIAL_COUNT`。虽然 `next_count` 此刻是 `load_count`，但 `clear_counter=1` 使主 `Register` 的 `clear` 生效，而 `Register` 内「最后赋值胜出」让 `clear` 优先于 `clock_enable`。优先级整体为 `clear` > `load` > `run`。

**练习 3**：要做「每 3 拍计一次」的计数器，怎么接？
**答案**：把 `run` 接成一个每 3 拍拉高 1 拍的脉冲（可用 `Counter_Binary` 自己配合比较器生成），`INCREMENT` 仍设为 1。计数器只在 `run=1` 的拍更新，等价于每 3 拍 +1。

---

### 4.3 clog2 与可复用函数库

#### 4.3.1 概念说明

计数器常常需要一个「配套」的位宽计算：给你 N 个项目（比如 N 个寄存器、深度为 N 的 FIFO），**编址它们需要几位二进制**？答案就是 \(\lceil \log_2 N \rceil\)，记作 `clog2(N)`。例如 `clog2(16)=4`（0..15 用 4 位）、`clog2(17)=5`。

Verilog-2005 起才有内建的 `$clog2()`，而本书坚持用 **Verilog-2001 可综合子集**（见 [u2-l1](./u2-l1-verilog2001-and-default-nettype.md)），所以要自带一个 `clog2` 函数，放在 `clog2_function.vh` 里。

但 Verilog-2001 有个限制：**函数只能定义在模块体内部**（没有 SystemVerilog 的 `package`）。于是本书的约定是：把可复用的纯计算函数放进单独的 `.vh` 文件，需要它的模块在**模块体开头**用 `` `include `` 引入。这个约定写进了全书编码规范（[verilog.html:72-85](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L72-L85)）。

#### 4.3.2 核心流程

`clog2` 的实现很巧。对正整数 *N*，编址 *N* 个项目（下标 0 到 *N-1*）所需的位数为

\[
\text{clog2}(N) = \lceil \log_2 N \rceil = \lfloor \log_2 (N-1) \rfloor + 1 \quad (N \ge 2)
\]

函数里先把入参减 1（`temp = value - 1`），然后数「右移多少次才变成 0」——这正是 \(\lfloor \log_2 (N-1) \rfloor + 1\)。减 1 是为了处理 *N* 恰为 2 的幂的情形：`clog2(16)` 要得到 4 而不是 5。

> **位数陷阱**：`clog2(N)` 返回的是「编址 *N* 个项目（0 到 *N-1*）」的位数。如果你要表示的计数值本身可能取到 *N*（即 0 到 *N*，共 *N+1* 个状态，比如「FIFO 里最多存 *N* 项」的占用计数），就需要 **`clog2(N)+1`** 位。本书 `Pipeline_Stall_Smoother` 正是这样用的。

因为 `.vh` 会被多个模块各自 `` `include `` 一次，函数定义可能被重复引入，所以文件用 `` `ifndef / `define / `endif `` 包成**幂等**（idempotent）——第一次包含时定义、之后忽略，全局只定义一次。

#### 4.3.3 源码精读

幂等保护与函数定义（[clog2_function.vh:33-45](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/clog2_function.vh#L33-L45)）：`` `ifndef CLOG2_FUNCTION `` 守卫保证多次包含安全。注意注释里特意说明用一个 `temp` 变量做计算，是因为「直接给函数输入端口赋值会触发 Vivado 告警」——又一个把工具怪癖藏进小函数的理由。

核心算法就是那个右移循环（[clog2_function.vh:40-43](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/clog2_function.vh#L40-L43)）：从 `temp = value-1` 开始，每右移一位、计数器加一，直到 `temp` 归零。

真实使用范例见 `Pipeline_Stall_Smoother`：它在模块体开头引入函数（[Pipeline_Stall_Smoother.v:78](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L78)），随即用一个 `localparam` 把函数结果固化成位宽（[Pipeline_Stall_Smoother.v:89](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L89)），再把这个位宽喂给 `Counter_Binary`（[Pipeline_Stall_Smoother.v:100-103](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L100-L103)）。这正是「`clog2` 算位宽 → `Counter_Binary` 用这个位宽」的标准链路。

本书的可复用函数库都遵循同一套 `.vh` + 幂等守卫的组织方式，目录下能找到（`abs` / `gcd` / `lcm` / `min` / `max` / `clog2` / `adjust_to_multiple` / `word_count` / `word_pad` 等）。

#### 4.3.4 代码实践

**实践目标**：用 `clog2` 为「4096 个 32 位字的存储器」计算地址位宽。

**操作步骤**（以下为**示例代码**）：

```verilog
module my_mem #(parameter NUM_WORDS = 4096) (...);
    `include "clog2_function.vh"            // 模块体开头引入

    localparam ADDR_WIDTH = clog2(NUM_WORDS); // 给 4096 个字编址：=12
    localparam WORD_WIDTH = 32;

    reg  [WORD_WIDTH-1:0] mem [0:NUM_WORDS-1];
    wire [ADDR_WIDTH-1:0] address;
    ...
endmodule
```

**需要观察的现象 / 预期结果**（手算即可验证）：

- `clog2(4096)`：`temp=4095`，右移 12 次归零 → **12** 位。（\(2^{12}=4096\)，下标 0..4095 恰好用 12 位。）
- `clog2(5)`：`temp=4`，右移 3 次归零（4→2→1→0）→ **3** 位。（5 个项目用 3 位编址 0..4。）
- `clog2(8)`：`temp=7`，右移 3 次归零 → **3** 位。（注意 8 也是 3，不是 4。）
- 若占用计数可能取到 4096（0..4096），则需 `clog2(4096)+1 = 13` 位。

> 把上述 `clog2(...)` 表达式放进 `initial $display(...)` 用 iverilog 跑一下即可逐个验证，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`clog2(1)` 和 `clog2(2)` 各得几？为什么 `clog2(1)` 在工程上要小心？
**答案**：按算法 `clog2(1)`：`temp=0`，循环不执行 → 返回 **0**；`clog2(2)`：`temp=1`，右移 1 次归零 → **1**。`clog2(1)=0` 会得到 0 位宽（非法的 `[-1:0]`），所以当项目数可能为 1 时要特别处理，避免撞上零位宽问题（参见 [u2-l2](./u2-l2-parameterization-and-widths.md)）。

**练习 2**：为什么 `clog2_function.vh` 必须是幂等的？
**答案**：因为它通过 `` `include `` 被多个模块各自包含一次，若无 `` `ifndef `` 守卫，函数 `clog2` 会被重复定义，导致编译错误。幂等守卫保证全局只定义一次。

**练习 3**：为什么函数放在 `.vh` 而不是 `package` 里？
**答案**：因为本书限定 Verilog-2001，而 `package` 是 SystemVerilog 才有的；Verilog-2001 的函数只能定义在模块体内部，故用 `.vh` + `` `include `` 在模块体开头引入。

---

## 5. 综合实践

把本讲三件事（计数器 = 加法器 + 寄存器、`INCREMENT` 步长、`clog2` 算位宽）串起来，完成下面这个小设计：

**任务**：设计一个「按 4 字节 / 拍传输」的总线字节计数器，并自动算出计数器位宽。

要求：

1. 用 `clog2` 由「最大字节数」算出 `WORD_WIDTH`。
2. 实例化一个 `Counter_Binary`，`INCREMENT = 4`，`up_down = 0`，`run` 接 `bus_valid`，从 0 计到最大字节数。
3. 再用 `clog2` 由「最大字节数 / 4」算出**字地址**位宽，说明它与字节计数器位宽的差别。

参考做法（**示例代码**）：

```verilog
module bus_byte_counter
#(
    parameter MAX_BYTES = 1024          // 例如：最多统计 1024 字节
)
(
    input  wire clock,
    input  wire bus_valid,
    output wire [clog2(MAX_BYTES)-1:0] byte_count   // 注：实际工程中宜先用 localparam 固化
);
    `include "clog2_function.vh"

    // 这里为可读性展开；真实代码应先把 clog2 结果存进 localparam 再用于端口声明
    localparam COUNT_WIDTH    = clog2(MAX_BYTES);     // 字节计数值位宽
    localparam WORD_ADDR_WIDTH = clog2(MAX_BYTES/4);  // 字(4字节)地址位宽
    localparam STEP            = 4;

    wire [COUNT_WIDTH-1:0] count;
    wire carry_out, overflow;
    wire [COUNT_WIDTH-1:0] carries;

    Counter_Binary
    #(
        .WORD_WIDTH     (COUNT_WIDTH),
        .INCREMENT      (STEP),          // 每拍 +4 字节
        .INITIAL_COUNT  ({COUNT_WIDTH{1'b0}})
    )
    the_counter
    (
        .clock      (clock),
        .clear      (1'b0),
        .up_down    (1'b0),
        .run        (bus_valid),
        .load       (1'b0),
        .load_count ({COUNT_WIDTH{1'b0}}),
        .carry_in   (1'b0),
        .carry_out  (carry_out),
        .carries    (carries),
        .overflow   (overflow),
        .count      (count)
    );

    assign byte_count = count;
endmodule
```

完成后请自检：

- 解释为什么 `Counter_Binary` 里看不到任何 `+` 或触发器（答案见 4.1）。
- 说出 `MAX_BYTES = 1024` 时 `COUNT_WIDTH` 与 `WORD_ADDR_WIDTH` 各是几。答案：`COUNT_WIDTH = clog2(1024) = 10`（\(2^{10}=1024\)，下标 0..1023）；而 1024 字节 = 256 个 4 字节字，`WORD_ADDR_WIDTH = clog2(256) = 8`。二者不同，因为字节地址比字地址多出 2 位（\(\log_2 4 = 2\)）。
- 想清楚：如果计数可能取到 `MAX_BYTES` 本身（0 到 1024，共 1025 个状态），位宽要多 1 位吗？（提示：见 4.3 的位数陷阱——此时需 `clog2(1024)+1 = 11` 位。）

> 端口声明里直接写 `clog2(...)` 在某些工具下可能受限，工程实践中更稳妥的做法是**先用 `localparam` 固化 `clog2` 结果、再用该 `localparam` 声明端口宽度**，如示例中段所示。完整可综合版本请本地用 Vivado / iverilog 验证，**待本地验证**。

## 6. 本讲小结

- `Counter_Binary` **不是**一句 `count <= count + 1`，而是「1 个 `Adder_Subtractor_Binary` 算下一拍值 + 4 个 `Register` 存储」的组合，自身零算术、零触发器，只做连线和控制选择。
- 这种「算 / 存分离」让我们可以**换加法器来换计数方案**（BCD、LFSR……），而不动控制逻辑；也把进位链推断的位宽技巧藏进加法器。
- 三套操作优先级为 **`clear` > `load` > `run`**；`load` 覆盖计数、`clear` 覆盖载入，靠 `Register` 的「最后赋值胜出」实现。
- `INCREMENT` 参数让计数器天然支持「每拍计 N 个」（如每拍 4 字节），标志位只在 `run` 时更新、在 `load`/`clear` 时清零。
- 级联多个计数器时，把前级的 `carry_out` 与后级的 `run` 相「与」；`carry_in` 与逐位 `carries` 为更一般级联保留。
- `clog2(N) = ⌈log₂ N⌉` 由 `clog2_function.vh` 提供；因 Verilog-2001 函数只能定义在模块体内，故以 `.vh` + 幂等 `` `ifndef `` 守卫 + 模块体开头 `` `include `` 的方式共享，全书 `abs`/`gcd`/`min`/`max` 等函数同此组织。

## 7. 下一步学习建议

- **往后看一个真实组合范例**：读 [Pipeline_Stall_Smoother.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v)，看它如何「`clog2` 算位宽 → 实例化两个 `Counter_Binary`」实现占用计数与触发计数——这是把本讲两件套用在一起的范本。
- **进入握手与弹性流水线**：计数器是后续 [u9 Ready/Valid 握手原理](./u9-l1-handshake-interfaces.md)、[u10 Skid Buffer](./u10-l1-skid-buffer-fsm.md) 等弹性流水线构件的基础设施（如 `Pipeline_FIFO_Buffer` / `Pipeline_Credit_Buffer` 内部都靠计数器管深度）。
- **想做分频**：直接读 [Pulse_Divider.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v)，看 `Counter_Binary` 配合比较器如何实现脉冲分频，这是 u15「脉冲逻辑」的预演。
