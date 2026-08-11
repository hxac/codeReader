# 内部写总线 reg_wrbus 与数据源选择

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **`reg_wrbus` 是什么**：它是 IKA32010 内部一条贯穿全局的 16 位「写总线」，几乎所有需要从 A 处搬运到 B 处的数据都要先踏上这条总线。
- 看懂 [`register_wrbus_source_sel`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L132-L144) 这个组合 MUX 如何在 **7 个数据源之间二选一**，把某一个源的值送上 `reg_wrbus`。
- 记住 [`WRBUS_SOURCE_*`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L11-L18) 这 7 个常量的含义，以及它们各自对应哪个硬件模块的输出。
- 弄清「微码驱动 MUX」的工作方式：微码（那个大 `casez(if_opcodereg)` 块）只需在必要时改写 `register_wrbus_source_sel`，其余指令沿用默认值即可。

本讲是进阶层（u2）的第一讲，也是 u1-l4（时钟与节拍）的直接续篇。u1-l4 告诉我们芯片在**哪个节拍**更新寄存器；本讲回答**更新的数据从哪条内部通路流过来**。从本讲开始，我们将逐个拆解 IKA32010 的数据通路模块，而 `reg_wrbus` 是这些模块之间共同的「主干道」——先把它看明白，后面读 RAM、ALU、栈、乘法器都会顺畅很多。

## 2. 前置知识

### 2.1 什么是「总线」

「总线（bus）」就是一组**被多个部件共享**的连线。你可以把它想象成工厂里的主传送带：原料仓库、成品仓库、打包车间都可以把东西放上传送带，也可以从传送带上取东西。关键约束是——**同一时刻传送带上只能有一件货物**，否则就会撞车。

在数字电路里，这个「不能撞车」的约束由**多路选择器（MUX，Multiplexer）**来保证：MUX 像传送带入口的调度员，每一拍只放行一个来源，其它来源全部断开。本讲的 `reg_wrbus` 就是这条传送带，`register_wrbus_source_sel` 就是那位调度员。

### 2.2 组合 MUX 与 `always @(*)`

「组合逻辑」是指输出随输入**立刻变化、不等时钟**的电路。在 Verilog 里用 `always @(*)` 块或 `assign` 语句描述。本讲的写总线 MUX 就是一个纯组合电路：

```verilog
always @(*) begin
    case(register_wrbus_source_sel)
        ...   // 哪个 case 命中，reg_wrbus 就立刻等于哪个源
    endcase
end
```

只要 `register_wrbus_source_sel` 这个 3 位选择信号的值变了，`reg_wrbus` 就在**同一个瞬间**跟着变，不需要等时钟沿。这一点很重要：`reg_wrbus` 本身虽然名字里有 `reg`，但它在这里是被组合块驱动的，行为更像一根「随时跟随选择器变化的线」。

> 小贴士：Verilog 里 `reg` 关键字并不意味着「时钟寄存器」，它只是「可以在 `always` 块里被赋值」的声明。一个 `reg` 到底是寄存器还是组合线，取决于它所在的 `always` 块是 `@(posedge clk)`（时序）还是 `@(*)`（组合）。`reg_wrbus` 属于后者。

### 2.3 水平微码与「默认值 + 覆盖」

IKA32010 的指令译码采用一种叫**水平微码（horizontal microcode）**的风格：在一个大的 `always @(*)` 块里，**先把所有控制信号赋一个安全的默认值**，再用 `casez(if_opcodereg)` 按指令逐条**覆盖**需要改动的少数信号。

对本讲而言，这意味着：`register_wrbus_source_sel` 有一个**全局默认值**（默认选 RAM），绝大多数指令如果不显式改写它，就一律从 RAM 读数据。只有那些确实需要从别处取数据的指令（比如 LACK 取立即数、PUSH 取累加器、POP 取栈），才会在自己的 `casez` 分支里把 `register_wrbus_source_sel` 改成别的源。这个设计让每条指令的微码都很短——只要管自己关心的那几个信号即可。完整的微码框架留到 u3-l1 展开，本讲只借用它「驱动 MUX」的这一面。

## 3. 本讲源码地图

本讲涉及两个源文件，引用其中若干段。

| 文件 | 本讲关注的位置 | 作用 |
|------|--------------|------|
| `src/IKA32010.sv` | [第 81 行：`reg_wrbus` 声明](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L81) | 声明这条 16 位写总线 |
| `src/IKA32010.sv` | [第 124–129 行：7 个数据源的线/寄存器声明](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L124-L129) | 列出喂给 MUX 的全部来源 |
| `src/IKA32010.sv` | [第 132–144 行：写总线 MUX](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L132-L144) | `case(register_wrbus_source_sel)` 选源的核心逻辑 |
| `src/IKA32010.sv` | [第 590 行：默认值](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L590) | 微码块顶部给 MUX 的默认选择 `WRBUS_SOURCE_RAM` |
| `src/IKA32010_mnemonics.sv` | [第 11–18 行：`WRBUS_SOURCE_*` 常量](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L11-L18) | 7 个源的编号定义 |
| `src/IKA32010.sv` | [第 786–802 行：ADD 指令](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L786-L802) 与 [第 879–888 行：LACK 指令](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L879-L888) | 综合实践中两条对照指令 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `reg_wrbus` 写总线**：解释这条总线是什么、谁会消费它。
- **4.2 `register_wrbus_source_sel` MUX**：解释选源机制本身，以及微码如何驱动它。
- **4.3 `WRBUS_SOURCE` 常量与 7 个数据源**：逐个讲清 7 个源的硬件出处。

### 4.1 写总线 reg_wrbus

#### 4.1.1 概念说明

IKA32010 内部有很多数据需要在不同部件之间搬运：把 RAM 里的数搬到 ALU 去算、把累加器的值搬到栈去保存、把指令字里编码的立即数搬到辅助寄存器……如果每两个部件之间都单独拉一根线，连线数量会爆炸。常见的解法是**开辟一条共享总线**，让所有部件都挂在上面，分时复用。

IKA32010 选择了这样一条 16 位宽的共享总线，命名为 `reg_wrbus`（wr = write，因为它主要承担「把数据写入某个目的地」的角色）。它的地位很像 CPU 里的「内部数据总线」：这一拍谁要往别处送数据，谁就把自己的输出送上 `reg_wrbus`；同一拍谁要接收数据，谁就从 `reg_wrbus` 上取。

需要强调两点：

1. **位宽 16 位**：因为 TMS32010 是 16 位定点 DSP，指令字、数据字、累加器低字都是 16 位宽。少数目的地只需要其中一部分位（比如 PC 只要低 12 位），它们会在取用时自己截取。
2. **同一拍只有一个源**：这是总线的铁律。为此必须有 MUX 在入口处做调度，这正是 4.2 要讲的。

#### 4.1.2 核心流程

`reg_wrbus` 的「生产—消费」关系可以用下面这张流向图概括：

```
        7 个数据源                      1 个 MUX                  多个消费者
 ┌───────────────────┐            ┌──────────────┐          ┌──────────────────┐
 │ SHB(移位器B/ACC)  │──┐         │              │   ┌──────▶│ PC（取低12位）    │
 │ RAM(数据存储)     │──┤         │              │   │       │ AR（辅助寄存器）  │
 │ AR(辅助寄存器)    │──┤         │  reg_wrbus   │   ├──────▶│ T 寄存器          │
 │ STACK(堆栈)       │──┼──MUX──▶│   (16 bit)   │───┼──────▶│ RAM（写回）        │
 │ IMM(指令立即数)   │──┤   sel    │              │   ├──────▶│ 移位器A 输入      │
 │ FLAG(状态标志)    │──┤         │              │   ├──────▶│ 堆栈（压ACC低12位）│
 │ INLATCH(外部输入) │──┘         └──────────────┘   └──────▶│ o_DOUT（OUT输出） │
                                                              └──────────────────┘
                       由 register_wrbus_source_sel
                       在 0..6 中选一个
```

要点：

- **生产端**：MUX 在 7 个源里挑一个，把它的值赋给 `reg_wrbus`。挑谁由 `register_wrbus_source_sel` 决定。
- **消费端**：`reg_wrbus` 同时连到很多地方（PC、AR、T、RAM、移位器 A、栈、`o_DOUT`），但每一拍**真正把 `reg_wrbus` 写进自己**的消费者，取决于各自的「写使能」是否打开。比如 `reg_t_ld` 为高时 T 寄存器才采信 `reg_wrbus`，`ram_wr` 为高时 RAM 才把 `reg_wrbus` 写入存储单元。
- 因此「总线不撞车」分两层保证：生产端靠 MUX 只放行一个源；消费端靠各自的写使能只在自己该写时才写。

#### 4.1.3 源码精读

`reg_wrbus` 的声明只有一行，[src/IKA32010.sv:81](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L81)：

```verilog
reg     [15:0]  reg_wrbus;
```

它真正被赋值的地方是 4.2 要讲的那个组合 MUX。这里先看**谁在消费它**，这样你能建立「这条总线到底通向哪里」的全局印象。用 Grep 搜 `reg_wrbus` 的读取点，整理成下表（均位于 `src/IKA32010.sv`）：

| 消费者 | 代码位置 | 取用方式 | 由哪条指令触发 |
|--------|---------|---------|--------------|
| 程序计数器 PC | [第 110 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L110) `if_pc <= reg_wrbus[11:0]` | 取低 12 位 | `PC_LOAD_WRBUS` 模式（B/CALA/CALL/RET） |
| 外部输出 `o_DOUT` | [第 248 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L248) `o_DOUT <= reg_wrbus` | 全 16 位 | OUT 指令（表写事务） |
| 辅助寄存器 AR | [第 318 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L318) `reg_ar[...] <= reg_wrbus` | 全 16 位 | LAR / LARK |
| T 寄存器 | [第 390 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L390) `if(reg_t_ld) reg_t <= reg_wrbus` | 全 16 位 | LT / LTD |
| 移位器 A 输入 | [第 429 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L429) `sha_output = ... reg_wrbus ...` | 符号扩展后移位 | ADD/LAC/AND 等算逻指令 |
| RAM 写口 | [第 493 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L493) `.i_DIN(reg_wrbus)` | 全 16 位 | SACH/SACL/SAR/SSR |
| 堆栈写口 | [第 415 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L415) `stk_data_sel ? if_pc : reg_wrbus[11:0]` | 取低 12 位 | PUSH（压累加器低字） |
| LST 位判读 | [第 665–681 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L665-L681) `if(reg_wrbus[15]) ...` | 逐位判断 | LST（从内存装状态寄存器） |

> 注意 LST 这一行很特别：它不是把 `reg_wrbus`「整体搬走」，而是逐位读 `reg_wrbus[15]`/`[14]`/`[8]`/`[0]` 来决定把哪些状态位置 1 或清 0。这也算一种「消费」——把总线上的内容解释成一束标志位。

可以看到，`reg_wrbus` 几乎是所有写动作的必经之路。理解了这条总线，就抓住了 IKA32010 数据通路的「中轴线」。

#### 4.1.4 代码实践

**实践目标**：亲手确认「`reg_wrbus` 是几乎所有写动作的中转站」，建立全局印象。

**操作步骤（源码阅读型）**：

1. 在 `src/IKA32010.sv` 中搜索字符串 `reg_wrbus`（注意排除它被**赋值**的那一行，即 MUX 内部）。
2. 把每个**读取**点按 4.1.3 的表格格式记录：所在行号、取用方式（整体 / 取低 12 位 / 逐位）、由哪条指令的哪个控制信号触发。
3. 重点关注「取用方式」一列：为什么 PC 和栈只取 `reg_wrbus[11:0]`？（提示：PC 和返回地址都是 12 位，见 u1-l3 端口 `o_AOUT[11:0]`。）

**需要观察的现象**：你会发现 `reg_wrbus` 被读取的次数明显多于被赋值的次数（赋值只有 MUX 里那一处）。这正是「总线」的特征：一个写入点（MUX）、多个读取点（各消费者）。

**预期结果**：得到一张类似 4.1.3 的「消费者清单」，并能解释每个消费者为何只取它需要的那几位。

> 待本地验证：如果你有支持交叉引用的编辑器，可右键 `reg_wrbus` 选「查找所有引用」，自动核对清单是否完整。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PC 取的是 `reg_wrbus[11:0]` 而不是整个 `reg_wrbus`？多出的高 4 位去哪了？

**参考答案**：因为 TMS32010 的程序地址空间只有 12 位（`o_AOUT` 是 12 位，PC `if_pc` 也是 `reg [11:0]`，见 [第 98 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L98)）。`reg_wrbus` 是 16 位，高 4 位对 PC 无意义，所以直接截断丢弃。

**练习 2**：`reg_wrbus` 这个名字里有 `reg`，它在电路上真的是一个触发器（寄存器）吗？

**参考答案**：在本设计里不是。虽然声明为 `reg [15:0]`，但它唯一被赋值的地方在 `always @(*)` 组合块里（4.2 的 MUX），所以它综合出来是一根**组合驱动的连线**（带 MUX），而不是时钟沿触发的寄存器。`reg` 在这里只是 Verilog 的语法声明，不决定电路性质。

---

### 4.2 选源 MUX：register_wrbus_source_sel

#### 4.2.1 概念说明

4.1 说了，总线同一拍只能有一个源。那「这一拍选谁」由谁来定？答案是一个 3 位的选择信号 `register_wrbus_source_sel`，以及一个由它驱动的组合 MUX。

为什么是 3 位？因为有 7 个源（外加一个 default），3 位能表示 0–7 共 8 个值，正好够用（`3'd7` 落到 default 分支，输出全 0）。

这个选择信号**不是凭空产生的**，而是由指令译码（微码）决定的。也就是说：**每一条正在执行的指令，会根据自己的需要，告诉 MUX「这一拍从哪个源取数据」**。比如 LACK 指令需要取指令字里的立即数，它就在自己的微码分支里把 `register_wrbus_source_sel` 设成 `WRBUS_SOURCE_IMM`；ADD 指令需要从 RAM 取操作数，但它**什么都不用做**——因为默认值就是 `WRBUS_SOURCE_RAM`。

这就是 2.3 提到的「默认值 + 覆盖」的好处：大多数算逻指令的操作数都来自 RAM，所以默认值设成 RAM，能省掉大量重复的赋值语句。

#### 4.2.2 核心流程

整个选源机制的数据流如下：

```
指令译码(微码 casez 块)
        │
        │  根据当前指令 if_opcodereg，
        │  决定是否改写 register_wrbus_source_sel
        ▼
register_wrbus_source_sel  (3 bit, 取值 0..6)
        │
        │  纯组合 always @(*) case
        ▼
   ┌────────────────────────────────┐
   │  case(sel)                     │
   │    SHB    : reg_wrbus = ...    │
   │    RAM    : reg_wrbus = ...    │  ← 默认值也是 RAM
   │    ...                          │
   │  endcase                       │
   └────────────────────────────────┘
        │
        ▼
     reg_wrbus (立刻跟随 sel 变化，无时钟延迟)
```

关键点：`reg_wrbus` 是**组合跟随**的。当 `if_opcodereg`（指令寄存器）在一个机器周期里稳定下来后，微码组合块立刻算出 `register_wrbus_source_sel` 的值，MUX 又立刻把对应源送上 `reg_wrbus`——这一切都发生在同一个周期内、不占额外的时钟拍。真正的「写回」动作（PC、AR、RAM 等真正采信 `reg_wrbus`）才发生在 `cyc_ncen` 那个节拍上（回顾 u1-l4）。

#### 4.2.3 源码精读

MUX 本体在 [src/IKA32010.sv:132-144](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L132-L144)：

```verilog
reg     [2:0]   register_wrbus_source_sel;
always @(*) begin
    case(register_wrbus_source_sel)
        WRBUS_SOURCE_SHB     : reg_wrbus = shb_output;
        WRBUS_SOURCE_RAM     : reg_wrbus = ram_output;
        WRBUS_SOURCE_AR      : reg_wrbus = ar_data_output;
        WRBUS_SOURCE_STACK   : reg_wrbus = {4'h0, stk_output};
        WRBUS_SOURCE_IMM     : reg_wrbus = {8'h00, if_opcodereg[7:0]};
        WRBUS_SOURCE_FLAG    : reg_wrbus = flag_output;
        WRBUS_SOURCE_INLATCH : reg_wrbus = busctrl_inlatch;
        default              : reg_wrbus = 16'h0000;
    endcase
end
```

逐条解读：

- `register_wrbus_source_sel` 声明为 `reg [2:0]`，3 位宽。
- 整个 `always @(*)` 块是纯组合逻辑——`sel` 一变，`reg_wrbus` 立刻重算。
- 每个 `case` 分支把 `reg_wrbus` 指向一个源。注意两个分支做了**位宽对齐**：
  - `WRBUS_SOURCE_STACK`：栈输出 `stk_output` 只有 12 位（返回地址），所以高位补 0 拼成 16 位 `{4'h0, stk_output}`。
  - `WRBUS_SOURCE_IMM`：立即数取指令字的低 8 位 `if_opcodereg[7:0]`，高位补 0 拼成 `{8'h00, if_opcodereg[7:0]}`。
- `default` 分支输出全 0，对应 `sel` 取到未定义值（如 `3'd7`）时的安全兜底。

默认值在哪？在微码块的顶部，[src/IKA32010.sv:590](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L590)：

```verilog
//read source
register_wrbus_source_sel = WRBUS_SOURCE_RAM;
```

这行在 `casez(if_opcodereg)` **之前**执行，所以它对**所有指令**生效；只有当某条指令在自己的分支里显式改写它，才会被覆盖。用 Grep 搜 `register_wrbus_source_sel =`，可以看到所有「改写点」，整理如下：

| 指令 | 改写成 | 行号 | 含义 |
|------|--------|------|------|
| POP | `WRBUS_SOURCE_STACK` | [695](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L695) | 出栈到累加器，数据来自栈 |
| PUSH | `WRBUS_SOURCE_SHB` | [718](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L718) | 压累加器入栈，数据来自移位器 B（=ACC） |
| SSR（官方 SST） | `WRBUS_SOURCE_FLAG` | [754](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L754) | 存状态寄存器到 RAM |
| LACK | `WRBUS_SOURCE_IMM` | [883](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L883) | 立即数装累加器 |
| LARK | `WRBUS_SOURCE_IMM` | [1111](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1111) | 立即数装辅助寄存器 |
| SAR | `WRBUS_SOURCE_AR` | [1170](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1170) | 存辅助寄存器到 RAM |
| B / CALA | `WRBUS_SOURCE_SHB` | [1406](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1406) / [1667](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1667) | 跳到累加器指的地址（ACC→PC） |
| CALL / RET | `WRBUS_SOURCE_STACK` | [1447](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1447) / [1677](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1677) | 从栈取返回地址装 PC |
| TBLR / IN | `WRBUS_SOURCE_INLATCH` | [1617](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1617) / [1687](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1687) | 把外部读入的数据装累加器 |

这张表读起来很有规律：**指令要从哪里取数据，就把 `sel` 指向哪里**。而表中**没有出现**的指令（ADD、LAC、AND、SACL…）都默默沿用默认的 `WRBUS_SOURCE_RAM`，因为它们的操作数本来就来自数据 RAM。

#### 4.2.4 代码实践

**实践目标**：验证「默认值 + 覆盖」的说法——确认大多数指令确实不改写 `register_wrbus_source_sel`，从而默认从 RAM 取数。

**操作步骤（源码阅读型）**：

1. 在 `src/IKA32010.sv` 搜索 `register_wrbus_source_sel =`（注意带等号，只找赋值点，不找 case 里的比较）。
2. 把每个命中行连同它所在的指令注释（如 `//LACK`、`//SAR`）记下来，得到 4.2.3 那张表。
3. 统计：改写点一共多少处？IKA32010 实现的指令总数（粗略数 `casez` 分支里的注释块）远多于此。这说明「默认值 RAM」替大量算逻指令省去了显式赋值。

**需要观察的现象**：改写点只有十几处，而指令有几十条——多数指令根本不碰 `register_wrbus_source_sel`，完全依赖第 590 行的默认值。

**预期结果**：得到一张「指令 → 改写的源」对照表，并能据此判断任意一条指令的 `reg_wrbus` 来自哪个源：先查它在不在这张表里，不在就默认 RAM。

> 待本地验证：可挑一条表中没有的指令（如 AND，[第 891 行附近](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L891)），确认它的分支里确实没有 `register_wrbus_source_sel =` 这一句。

#### 4.2.5 小练习与答案

**练习 1**：`register_wrbus_source_sel` 是 3 位，能表示 8 个值，但只定义了 7 个源（0–6）。当它等于 `3'd7` 时会发生什么？

**参考答案**：会落到 MUX 的 `default` 分支，`reg_wrbus` 被赋成 `16'h0000`（全零）。这是组合 `case` 没有全覆盖时的安全兜底，避免综合出锁存器（latch）。在正常指令流里 `sel` 不会取到 7，因为微码只会写入 0–6 或沿用默认值 1（RAM）。

**练习 2**：为什么默认值偏偏选 `WRBUS_SOURCE_RAM`，而不是 `WRBUS_SOURCE_SHB` 或别的？

**参考答案**：因为 IKA32010（以及 TMS32010）最常见的指令形态是「对数据存储器里的操作数做运算」，ADD/SUB/LAC/AND/OR/XOR/SACL 等一大批算逻指令的操作数都来自 RAM。把默认值设成 RAM，可以让这数量最多的一类指令「什么都不用写」就拿到正确的源，最大化地压缩微码体积。相比之下，需要 ACC（SHB）、栈、立即数、I/O 的指令是少数，让它们各自显式改写更划算。

---

### 4.3 WRBUS_SOURCE 常量与 7 个数据源

#### 4.3.1 概念说明

4.2 的 MUX 里出现了 7 个名字：`shb_output`、`ram_output`、`ar_data_output`、`stk_output`、`if_opcodereg[7:0]`、`flag_output`、`busctrl_inlatch`。这 7 个就是 `reg_wrbus` 的全部数据源。本小节逐个交代它们的**硬件出处**——每个源到底是哪个模块、哪根线产生的。

为避免在代码里写「魔数」（0、1、2…），作者把这 7 个源的编号定义成了有意义的常量，集中放在 [src/IKA32010_mnemonics.sv:11-18](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L11-L18)：

```verilog
//Write bus sources
localparam  WRBUS_SOURCE_SHB     = 3'd0;
localparam  WRBUS_SOURCE_RAM     = 3'd1;
localparam  WRBUS_SOURCE_AR      = 3'd2;
localparam  WRBUS_SOURCE_STACK   = 3'd3;
localparam  WRBUS_SOURCE_IMM     = 3'd4;
localparam  WRBUS_SOURCE_FLAG    = 3'd5;
localparam  WRBUS_SOURCE_INLATCH = 3'd6;
```

这遵循了 u1-l2 提到的约定：**看到陌生大写名字就回 mnemonics 查**。这里每个常量的取值（0–6）必须和 MUX 的 `case` 分支一一对应，否则选源就会错乱——你可以对照 4.2.3 的 MUX 源码核对，顺序完全一致。

#### 4.3.2 核心流程

7 个源的「出身」各不相同，下表先给全景，4.3.3 再逐个上源码：

| 常量 | 值 | 喂给 MUX 的变量 | 产生该变量的硬件 | 位宽处理 |
|------|:-:|----------------|-----------------|---------|
| `WRBUS_SOURCE_SHB` | 0 | `shb_output` | 移位器 B（对累加器 ACC 输出做移位） | 16 位 |
| `WRBUS_SOURCE_RAM` | 1 | `ram_output` | 数据 RAM 子模块 `u_ram` 的读口 | 16 位 |
| `WRBUS_SOURCE_AR` | 2 | `ar_data_output` | 辅助寄存器 `reg_ar[0/1]` | 16 位 |
| `WRBUS_SOURCE_STACK` | 3 | `stk_output` | 硬件堆栈 `u_stack` 的读口 | 12 位，MUX 里补成 16 位 |
| `WRBUS_SOURCE_IMM` | 4 | `if_opcodereg[7:0]` | 指令寄存器里的立即数字段 | 8 位，MUX 里补成 16 位 |
| `WRBUS_SOURCE_FLAG` | 5 | `flag_output` | 由各状态位拼接而成 | 16 位 |
| `WRBUS_SOURCE_INLATCH` | 6 | `busctrl_inlatch` | 外部总线输入锁存器 | 16 位 |

一个共同点：**所有源最终都被整理成 16 位**送入 MUX（栈和立即数在 MUX 分支里做了零扩展）。这样 MUX 的输出 `reg_wrbus` 始终是规整的 16 位，下游消费者用起来不用再操心来源差异。

#### 4.3.3 源码精读

7 个源的变量声明集中在 [src/IKA32010.sv:124-129](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L124-L129)：

```verilog
//sources
reg     [15:0]  shb_output;
wire    [15:0]  ar_data_output;
wire    [11:0]  stk_output;
wire    [15:0]  flag_output;
wire    [15:0]  ram_output;
reg     [15:0]  busctrl_inlatch;
```

（`if_opcodereg` 是指令寄存器，早在 [第 89 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L89) 声明，所以这里没有重复列出。）逐个看它们的产生方式：

**① SHB（移位器 B，= 累加器经移位）**——[第 463 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L463)：

```verilog
assign  shb_output = shb_mux ? shb_intermediate[31:16] : shb_intermediate[15:0];
```

`shb_intermediate` 是累加器 `alu_acc_output` 经移位后的 32 位结果（见 [第 464–471 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L464-L471)），`shb_mux` 决定取高 16 位还是低 16 位。所以选 SHB 就是「把累加器的（可能移位后的）内容送上总线」——PUSH、SACH、B、CALA 都靠它。

**② RAM（数据存储读出）**——由子模块 `u_ram` 的 `o_DOUT` 提供，[第 491–494 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L491-L494)：

```verilog
IKA32010_ram u_ram (
    ..., .i_ADDR(ram_addr), .i_DIN(reg_wrbus), .o_DOUT(ram_output)
);
```

注意这个实例化里 `reg_wrbus` 既是 RAM 的**写口** `i_DIN`，`ram_output` 又是 RAM 的**读口** `o_DOUT` 并回流成 `reg_wrbus` 的一个源——RAM 与写总线是**双向**关系。详细寻址机制留到 u2-l5。

**③ AR（辅助寄存器）**——[第 298 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L298)：

```verilog
assign  ar_data_output = reg_ar[if_opcodereg[8]]; //used to save AR data
```

按指令字的第 8 位从 `reg_ar[0]`、`reg_ar[1]` 两个辅助寄存器里挑一个输出，供 SAR 指令把辅助寄存器内容存进 RAM。辅助寄存器本身的细节留到 u2-l4。

**④ STACK（堆栈读出）**——由子模块 `u_stack` 的 `o_DOUT` 提供，12 位返回地址，[第 415 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L415)：

```verilog
.i_DIN(stk_data_sel ? if_pc : reg_wrbus[11:0]), .o_DOUT(stk_output)
```

POP 和 RET 选这个源，把栈顶的返回地址送上总线（再装进 ACC 或 PC）。栈的细节留到 u2-l6。

**⑤ IMM（指令内立即数）**——直接取指令寄存器低 8 位，MUX 里零扩展：`{8'h00, if_opcodereg[7:0]}`。LACK、LARK 选它。这是唯一一个**不来自任何寄存器/存储器、而是直接来自指令字编码**的源。

**⑥ FLAG（状态标志拼接）**——[第 479 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479)：

```verilog
assign  flag_output = {alu_flag_ovfl, reg_ovm, reg_intm, 4'b1111, reg_arp, 6'b111111, 1'b1, reg_dp};
```

把溢出 V、溢出模式 OVM、中断使能 INTM、辅助寄存器指针 ARP、数据页 DP 等状态位，按 TMS32010 状态寄存器 ST0 的位序拼成 16 位。SSR（官方 SST）选它，把状态字存进 RAM。注意位序与 LST（4.1.3 第 8 行）读取的位序严格对应——存什么位，LST 就读什么位。

**⑦ INLATCH（外部总线输入锁存）**——`busctrl_inlatch` 是一个寄存器，在表读（TBLR）和输入（IN）事务的相位 3 把外部 `i_DIN` 锁存进来，[第 217 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L217) 与 [第 239 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L239)：

```verilog
2'd3: begin ... busctrl_inlatch <= i_DIN; end   // 相位3锁存外部数据
```

TBLR、IN 选它，把从程序区或外设读进来的数据送上总线，再装进累加器。这是 `reg_wrbus` 与**外部世界**沟通的窗口。

> 小结：7 个源覆盖了「内部运算结果（SHB）、存储（RAM/STACK）、寻址寄存器（AR）、指令自带（IMM）、状态（FLAG）、外部输入（INLATCH）」全部典型数据来源。`reg_wrbus` 之所以能当「中轴线」，正是因为它能从这 7 处任意一处取数。

#### 4.3.4 代码实践

**实践目标**：把「常量 → 变量 → 产生该变量的硬件/行号」三者一一对应起来，巩固对 7 个源出身的记忆。

**操作步骤（源码阅读型）**：

1. 打开 [src/IKA32010_mnemonics.sv:11-18](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L11-L18)，抄下 7 个常量及其取值。
2. 打开 [src/IKA32010.sv:132-144](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L132-L144) 的 MUX，记下每个常量对应的变量名。
3. 对每个变量，去源码里找它的 `assign` 或实例化 `.o_DOUT(...)`（参考 4.3.3 给出的行号），确认它的硬件出处。
4. 把结果填成一张「常量 | 值 | 变量 | 产生行号 | 含义」五列表（即 4.3.2 的扩充版）。

**需要观察的现象**：你会确认常量取值顺序与 MUX `case` 分支顺序完全一致，且 7 个源恰好来自 7 个不同的硬件位置，没有重复。

**预期结果**：得到一张完整的「数据源履历表」，今后看到任意一条指令选了某个 `WRBUS_SOURCE_*`，就能立刻说出数据具体从哪个模块流出来。

> 待本地验证：可尝试把 `WRBUS_SOURCE_RAM` 和 `WRBUS_SOURCE_AR` 的取值在 mnemonics 里人为对调，重新审视 MUX——你会发现 `reg_wrbus` 会取到错误的源，从而体会「常量值必须与 case 分支一一对应」这条隐性约束。

#### 4.3.5 小练习与答案

**练习 1**：指令 `LACK 0x3C`（把累加器装成 0x3C）执行时，`reg_wrbus` 上的 16 位值具体是什么？

**参考答案**：LACK 选 `WRBUS_SOURCE_IMM`，MUX 里 `reg_wrbus = {8'h00, if_opcodereg[7:0]}`。立即数 0x3C 编码在指令字低 8 位，所以 `reg_wrbus = {8'h00, 8'h3C} = 16'h003C`。随后 ALU 把它（经移位器 A、按字节选择）加到被清零的端口 A 上，累加器得到 0x3C。

**练习 2**：为什么 `flag_output` 里要把 `reg_ovm`、`reg_intm`、`reg_arp`、`reg_dp` 这些状态位放在固定的比特位置上拼？

**参考答案**：因为 TMS32010 的状态寄存器 ST0 有固定的位定义（哪一位是 OVM、哪一位是 INTM……都是芯片手册规定好的）。`flag_output` 必须按这个固定位序拼接，存进 RAM 后才能被 LST 指令按同样位序正确读回（对照 4.1.3 的 LST：它读的正是 `[15]`=[V]、`[14]`=[OVM]、`[8]`=[ARP]、`[0]`=[DP]）。位序一旦错乱，存进去的状态就再也恢复不回来了。

---

## 5. 综合实践

把本讲三个模块串起来：**追踪一条 ADD 指令和一条 LACK 指令执行时，`reg_wrbus` 分别来自哪个数据源，并画出各自的数据流向图。**

### 5.1 实践目标

用两条对照指令验证本讲的核心结论——

- **ADD**：操作数来自数据 RAM，靠「默认值」选源，**指令微码里不写 `register_wrbus_source_sel`**。
- **LACK**：操作数来自指令字里的立即数，靠**显式改写** `register_wrbus_source_sel = WRBUS_SOURCE_IMM`。

两者最终都把数据经 `reg_wrbus` → 移位器 A → ALU 送进累加器，区别只在「`reg_wrbus` 的上游是谁」。

### 5.2 操作步骤

1. **读 ADD 的微码**，[src/IKA32010.sv:786-802](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L786-L802)：

   ```verilog
   //ADD - Add to accumulator with shift
   16'b0000_????_????_????: begin
       alu_modesel = ALU_ADD;
       alu_acc_ld = YES;
       sha_amt = {1'b0, if_opcodereg[11:8]};
       ...
   end
   ```

   确认：分支里**没有** `register_wrbus_source_sel = ...`。因此它沿用 [第 590 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L590) 的默认值 `WRBUS_SOURCE_RAM`，即 `reg_wrbus = ram_output`。

2. **读 LACK 的微码**，[src/IKA32010.sv:879-888](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L879-L888)：

   ```verilog
   //LACK - Load accumulator immediate
   16'b0111_1110_????_????: begin
       alu_modesel = ALU_ADD; alu_paz = YES; alu_pbdata = ALU_PBDATA_BYTE;
       alu_acc_ld = YES;
       register_wrbus_source_sel = WRBUS_SOURCE_IMM;   // ← 显式改写
       ...
   end
   ```

   确认：分支里**有** `register_wrbus_source_sel = WRBUS_SOURCE_IMM`，于是 `reg_wrbus = {8'h00, if_opcodereg[7:0]}`。

3. **分别画数据流向图**。

   **ADD 的数据流**（操作数来自 RAM）：

   ```
   数据 RAM ──o_DOUT──▶ ram_output ──MUX(sel=RAM,默认)──▶ reg_wrbus
                                                                │
                                                                ▼
                         移位器A(sha_amt=指令[11:8]) ── sha_output
                                                                │
                                                                ▼
                          ALU(portB, 模式=ADD) ──加到──▶ 累加器 ACC
   ```

   **LACK 的数据流**（操作数来自指令立即数）：

   ```
   指令寄存器 if_opcodereg[7:0] ──MUX(sel=IMM,显式)──▶ reg_wrbus(=00..II)
                                                                │
                                                                ▼
                         移位A + ALU(portA置零=alu_paz, 字节选=BYTE)
                                                                │
                                                                ▼
                                            ACC = 0 + II = 立即数
   ```

4. **在两张图上标注三个关键差异**：
   - ADD 的 MUX 选择来自**默认值**（第 590 行），LACK 的来自**显式赋值**（第 883 行）。
   - ADD 的上游是 `ram_output`（存储器），LACK 的上游是 `if_opcodereg[7:0]`（指令字）。
   - 两者下游都走「`reg_wrbus` → 移位器 A → ALU → ACC」这条共同通路。

### 5.3 需要观察的现象

- 同一条「`reg_wrbus` → 移位器 A → ALU」的下游通路，上游换一个源，就实现了完全不同的指令语义（「内存数加到 ACC」 vs 「立即数装 ACC」）。这正是共享总线 + 可选源 MUX 的价值：**通路复用，源可切**。
- ADD 微码之所以能写得这么短（只管 `alu_modesel`/`sha_amt`），正是因为默认值替它处理了选源。

### 5.4 预期结果

得到上面两张数据流向图，并能口头复述：**「ADD 走默认 RAM 源，LACK 显式选 IMM 源，两者汇入同一条 `reg_wrbus` 后再经移位器 A 进 ALU。」**

> 待本地验证：若打开 [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) 编译时定义 `IKA32010_DISASSEMBLY` 宏（参见 u1-l5、u3-l8），可在仿真波形里同时观察 `register_wrbus_source_sel`、`reg_wrbus`、`ram_output`、`if_opcodereg` 四者：执行 ADD 时 `reg_wrbus` 应与 `ram_output` 一致，执行 LACK 时应等于 `{8'h00, if_opcodereg[7:0]}`。本讲不假设你已经跑过仿真，上述为预期对照。

## 6. 本讲小结

- [`reg_wrbus`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L81) 是一条贯穿 IKA32010 全局的 16 位共享写总线，几乎所有跨部件的数据搬运都要踏上它；它有一个写入点（MUX）、多个读取点（PC/AR/T/RAM/移位器A/栈/`o_DOUT`）。
- [`register_wrbus_source_sel`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L132-L144) 是一个纯组合 MUX，由 3 位选择信号在 7 个数据源里挑一个送上 `reg_wrbus`，输出立刻跟随选择变化、不占时钟拍。
- 微码采用「[默认值 RAM](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L590) + `casez` 覆盖」的风格：大多数算逻指令的操作数来自 RAM，故沿用默认值；只有 LACK/LARK/SAR/POP/PUSH/SSR/B/CALL/RET/TBLR/IN 等少数指令才显式改写选源。
- 7 个源 [`WRBUS_SOURCE_*`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L11-L18) 分别来自：移位器 B（ACC）、RAM、辅助寄存器 AR、堆栈、指令立即数、状态标志拼接、外部输入锁存——覆盖了全部典型数据出处。
- 栈与立即数在 MUX 分支里做了零扩展（`{4'h0, stk_output}`、`{8'h00, if_opcodereg[7:0]}`），保证 `reg_wrbus` 始终是规整 16 位。
- ADD 与 LACK 的对照说明：共享总线 + 可选源 MUX 让「下游通路复用、上游源可切」，一条 `reg_wrbus` 就能支撑几十条语义各异的指令。

## 7. 下一步学习建议

现在你已经掌握了 IKA32010 的「内部数据中轴线」。接下来可以沿着两个方向深入：

- **u2-l2（程序计数器 PC 与取指）**：看 `reg_wrbus` 的一个重要消费者——PC 如何在 `PC_LOAD_WRBUS` 模式下从总线取地址（[第 110 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L110)），理解 B/CALL/RET 如何借这条总线完成跳转。
- **u2-l5（数据 RAM 与寻址方式）**：深入 `u_ram` 子模块，弄清 `ram_output`（本讲默认源）和 `reg_wrbus → RAM 写口`这对双向关系背后的直接/间接寻址与 DMOV 机制。
- **u3-l1（微码架构总览）**：把本讲借用的「默认值 + casez 覆盖」放到完整的微码框架里看，理解 `register_wrbus_source_sel` 只是微码驱动的一大堆控制信号之一。
