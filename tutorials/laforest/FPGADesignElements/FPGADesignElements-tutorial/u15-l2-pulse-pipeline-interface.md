# 脉冲与流水线互转

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清两种接口风格的差别——**ready/valid 弹性流水线接口**（每拍都能握手、靠反压调速）与**脉冲接口**（事件来一下就处理一下）——以及为什么**迭代型模块**（initiation interval 大于 1）天然更适合脉冲接口。
- 用 `Pipeline_to_Pulse` 把一个 ready/valid **输入**握手翻译成单周期脉冲喂给「脉冲输入型」模块，并讲清它那段看似绕口的 `ready_in` 启动逻辑（两个 `Pulse_Latch` 各管一件事）为什么要这么写。
- 用 `Pulse_to_Pipeline` 把一个「脉冲输出型」模块的结果翻译成 ready/valid **输出**握手，并解释为什么中间必须插一个输出缓冲（Skid/Half/FIFO）来切断 `ready_out → module_ready` 的反向组合路径。
- 把两个转换器接在一个迭代内核两侧，组成「ready/valid 进 → 脉冲 → 迭代计算 → 脉冲 → ready/valid 出」的完整往返结构，并说明它与握手纪律（u9-l2 的「接口内不得有组合环」）以及 CDC（脉冲天然可跨域）的关系。

## 2. 前置知识

本讲是「脉冲逻辑与接口转换」单元的第二篇，把 **u15-l1** 的脉冲零件和 **u10-l1** 的握手缓冲接到了一起。你需要三块基础：

- **u15-l1（脉冲生成、锁存与分频）**：这是本讲的正式前置。你已经知道 `Pulse_Latch` 把一拍脉冲「冻」成持续电平、靠 `Register` 的「最后赋值胜出」让 `clear` 优先——本讲两个转换器内部都会反复用它来「记住一个握手还没完成」。你也知道脉冲是「只高一拍就回落」的事件型信号。
- **u10-l1（Skid Buffer 与 COTTC FSM）**：学习路径上的关键前置。你已经知道 ready/valid 握手接口的规矩——同一拍 `valid` 与 `ready` 同高即完成一次传输；**接口内部不得有组合环路**（source 不得 `ready→valid`、destination 不得 `valid→ready`）。你也知道 `Pipeline_Skid_Buffer`/`Pipeline_Half_Buffer`/`Pipeline_FIFO_Buffer` 这些弹性缓冲的作用：用内部的寄存器**切断输入到输出的组合路径**，允许两侧各自独立握手。本讲的 `Pulse_to_Pipeline` 正是靠插一个这样的缓冲来切断反向组合路径。
- **u9-l1 / u9-l2（握手接口与死锁/活锁避免）**（概念性前置）：知道 `handshake_complete = (ready && valid)`，知道「valid 拉高后必须保持到握手完成」。

一个直觉式的复习：在本书的模块库里，模块之间通信有两种「方言」。

- **ready/valid 弹性接口**：双方随时可以谈，`valid` 表示「我有数据」、`ready` 表示「我能收」，同拍都高就成交。它能反压、能变速，是流水线的通用语（u9-l1）。
- **脉冲接口**：没有 `ready`，事件以「一拍脉冲」的形式表达——「我给你一个新输入」「我算完了」。它更轻，但天然假设**双方按某种节奏默契配合**。

麻烦在于：很多有趣的计算模块（累加器、除法器、滤波器）内部有**反馈回路**或**迭代循环**，算一个结果要好几拍，**没法每拍都吃一个新输入**——本书称之为 **initiation interval（启动间隔）大于 1**。这种模块如果硬套 ready/valid，控制逻辑会很别扭。本书的做法是：让这类模块的输入/输出**本就用脉冲接口**（来一个脉冲吃一个输入、算完发一个脉冲），再用本讲两个转换器在外面把它「包装」成 ready/valid，好和流水线世界对接。

> 这两个转换器在 **u14-l1（字同步与脉冲同步）** 里其实有「远亲」：CDC 脉冲同步器也是把事件变成 toggle/脉冲过时钟域。区别是——本讲两个模块**只在一个时钟域内**工作，它们不是同步器；但它们暴露的脉冲接口，恰好是日后插 `CDC_Pulse_Synchronizer` 跨域的天然接口（见 4.1.4）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Pipeline_to_Pulse.v` | 把 ready/valid **输入**握手翻译成「单周期脉冲 + 数据」喂给脉冲输入型模块，并据模块的 `module_ready` 防止喂得太快。 |
| `Pulse_to_Pipeline.v` | 把脉冲输出型模块的「结果 + 一拍 done 脉冲」翻译成 ready/valid **输出**握手，结果被下游读走后回送 `module_ready`。可选 Skid/Half/FIFO 输出缓冲。 |
| `Averager_Powers_of_Two.v` | 真实范例：输入侧用 `Pipeline_to_Pulse`、输出侧用 `Pulse_to_Pipeline`，中间夹一个迭代累加器，凑成完整往返。 |
| 辅助原语（前序讲义已学，本讲直接复用） | `Pulse_Latch`（u15-l1）、`Pipeline_Skid_Buffer`/`Pipeline_Half_Buffer`/`Pipeline_FIFO_Buffer`（u10-l1、u12-l1）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先在 **4.1** 讲清「接口转换」这件事的来由与必须共同遵守的纪律（这是两个转换器共享的前提），再在 **4.2、4.3** 分别钻进 `Pipeline_to_Pulse` 与 `Pulse_to_Pipeline` 的实现。

### 4.1 接口转换：两种方言之间为什么需要翻译

#### 4.1.1 概念说明

设想你有一个累加器：它要连续吃 N 个样本、累加、最后除以 N 给出平均值。它内部有一条「累加值」的反馈回路，**每收一个样本要算一拍**，不可能像普通流水线那样每拍吞吐一个新输入。用本书的话说，它的 **initiation interval 大于 1**。

对这种模块，最自然的设计是：

- **输入侧**：给它一个 `valid` **脉冲**表示「这是一个新样本」，数据线同时给出样本值。模块自己决定何时能收下一个。
- **输出侧**：算完后它发出一个 `done` **脉冲**表示「结果好了」，数据线同时给出结果。

这种「脉冲接口」对迭代模块很顺手。但你周围的系统讲的是 ready/valid。于是需要两个翻译器：

- `Pipeline_to_Pulse`：在**输入侧**，把外部的 ready/valid 握手翻成脉冲喂进去。
- `Pulse_to_Pipeline`：在**输出侧**，把模块算完的脉冲翻成 ready/valid 给下游。

两个转换器把迭代内核「夹」在中间，对外呈现一个规规矩矩的 ready/valid 模块——这正是 `Averager_Powers_of_Two` 的结构（4.1.3 会看到）。

#### 4.1.2 核心流程

完整的往返结构长这样：

```text
   ready/valid                  脉冲接口                  ready/valid
   输入握手                                                输出握手
 ┌───────────┐            ┌──────────────┐            ┌────────────┐
 │ valid_in  │            │module_data_in│            │ valid_out  │
 │ ready_in ◀┤  Pipeline  │  _valid(脉冲)│   迭代     │  ready_out │
 │ data_in   │ ──to_Pulse▶│  module_data │  内核      │  data_out  │
 └───────────┘            └──────┬───────┘            └────────────┘
          ▲                      │                          │
          │                      ▼                          ▼
          │  module_ready   ┌─────────┐    module_data_out(脉冲done)
          └─────────────────│  迭代   │◀────────────────────────
             (回送「可收下一 │  内核   │
              个输入」)      └─────────┘
                             │module_data_out_valid(脉冲)│
                             └────────────► Pulse_to_Pipeline
```

关键在于那条**回送链路**：输出侧一旦把结果交给下游（`Pulse_to_Pipeline` 完成输出握手），就把 `module_ready` 拉高一拍，告诉内核「上一个结果我收走了、你可以接下一个输入了」；输入侧的 `Pipeline_to_Pulse` 看到 `module_ready` 才放行下一个输入握手。这条往返链路把「输入完成」和「输出完成」耦合成一次完整的「进—算—出」事务。

#### 4.1.3 源码精读

这套往返结构在 `Averager_Powers_of_Two` 里是真用着的。开篇注释把动机讲得很直白：

> Accepts 2^POWER_OF_TWO_EXPONENT input samples, then makes the average available until it is read out. The output is buffered, so a new average may be started before the previous average is read out.

[Averager_Powers_of_Two.v:1-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L1-L13)——一个「吃多个样本、累加、出平均值」的迭代模块，输出还带缓冲（允许上一笔没读走就开始算下一笔）。

输入侧，注释「先把输入握手转成给累加器的脉冲接口」：

> First, convert the input handshake into a pulse interface to the Accumulator_Binary.

[Averager_Powers_of_Two.v:77-104](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L77-L104)——实例 `bring_em_in` 就是 `Pipeline_to_Pulse`：左边的 `input_valid`/`input_ready`/`input_sample` 是 ready/valid，右边出来的 `input_sample_passed`/`sample_valid` 是给累加器的脉冲，而 `input_sample_next`（接 `module_ready`）由后面的控制逻辑决定何时为真。

输出侧，注释「把脉冲控制的输出转成输出流水线握手接口」：

> Finally, convert the pulse-controlled output to the output pipeline handshake interface.

[Averager_Powers_of_Two.v:256-286](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L256-L286)——实例 `bring_em_out` 就是 `Pulse_to_Pipeline`，`OUTPUT_BUFFER_TYPE` 选了 `"SKID"`。右边的 `truncated_average`/`truncated_average_valid` 是内核的脉冲输出，左边出来的 `output_valid`/`output_ready`/`output_average` 是 ready/valid，`average_read_out`（接 `module_ready`）回送给控制逻辑。

控制逻辑把两端接上：

```verilog
always @(*) begin
    input_sample_next       = ((sample_done == 1'b1) && (samples_remaining != COUNTER_ZERO)) || (clear_done == 1'b1);
    truncated_average_valid =  (sample_done == 1'b1) && (samples_remaining == COUNTER_ZERO);
end
```

[Averager_Powers_of_Two.v:338-341](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L338-L341)——`input_sample_next` 是回送给 `Pipeline_to_Pulse` 的 `module_ready`（累加完一个且还没攒够 → 可收下一个样本）；`truncated_average_valid` 是送给 `Pulse_to_Pipeline` 的 done 脉冲（攒够了 → 出平均值）。两行组合逻辑把「进—算—出」的节奏定死。

#### 4.1.4 代码实践

**实践目标**：在真实源码里把这套往返结构「点」出来，建立全局印象，再进入下一节的细节。

**操作步骤**：

1. 打开 [Averager_Powers_of_Two.v:84-104](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L84-L104)，找到 `bring_em_in`（`Pipeline_to_Pulse`），列出它的「ready/valid 侧」和「脉冲侧」各有哪些端口。
2. 打开 [Averager_Powers_of_Two.v:262-286](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L262-L286)，找到 `bring_em_out`（`Pulse_to_Pipeline`），同样列出两侧端口，并注意 `OUTPUT_BUFFER_TYPE` 选了什么。
3. 把 [Averager_Powers_of_Two.v:338-341](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L338-L341) 的两行赋值在 4.1.2 的框图里标出来：哪个信号是「输入侧的 `module_ready`」，哪个是「输出侧的 done 脉冲」。

**需要观察的现象 / 预期结果**：

- 你能指认出「ready/valid 进 → 脉冲 → 迭代累加器 → 脉冲 → ready/valid 出」这条完整数据通路，并说出 `module_ready` 是把两侧耦合起来的那一根回送线。
- 你会注意到两个转换器**都没有碰时钟域**——它们只是同一时钟域内的接口翻译。脉冲要跨域，得另外插 `CDC_Pulse_Synchronizer`（见 4.4 的练习）。

> 待本地验证：本步是源码阅读型实践，无需运行。若你之后用仿真跑 `Averager_Powers_of_Two`，可在波形里确认 `sample_valid`（脉冲）与 `input_sample_next`（回送 ready）的交替节奏。

#### 4.1.5 小练习与答案

**练习 1**：为什么本书不直接让累加器用 ready/valid 接口，而要先把它做成脉冲接口、再用两个转换器包装？

**参考答案**：累加器内部有反馈回路、算一个结果要 N 拍，**initiation interval 大于 1**，不能每拍吃一个新输入。如果硬用 ready/valid，模块内部就得自己维护「我现在能不能收下一个」的状态机，控制逻辑和数据通路纠缠在一起。本书的做法是「数据/控制分离」（u4-l1、u10-l1）：让内核只认简单的脉冲事件，把「何时能收下一个」的握手逻辑外包给 `Pipeline_to_Pulse`，把「结果怎么交出去」外包给 `Pulse_to_Pipeline`。内核因此保持纯粹，转换器则可复用于所有迭代模块。

**练习 2**：两个转换器的头注释都强调「连接的模块必须至少有一级流水线寄存器、不得有组合直通路径」。请用 4.1.2 的回送链路解释：如果内核是零延迟的组合逻辑，会出什么问题？

**参考答案**：回送链路是 `module_ready →（输入侧）ready_in → input_handshake_done → module_data_in_valid →（内核）→ module_data_out_valid →（输出侧）valid_out_internal → output_handshake_done → module_ready`。若内核是组合直通，这条链路就闭合成了一个**纯组合环路**（u9-l2 讲过的接口内禁忌），会产生振荡/无法收敛。要求内核至少有一级寄存器，就是用那一拍延迟**打断**这条环。

---

### 4.2 Pipeline_to_Pulse：把 ready/valid 输入翻译成脉冲输入

#### 4.2.1 概念说明

`Pipeline_to_Pulse` 解决输入侧的翻译：外面来一个 ready/valid 握手，它把数据**原样**交给内核，并发出一个**单周期脉冲** `module_data_in_valid` 告诉内核「有新数据了」。同时，它必须保证**不会比内核能消化的更快地喂入**——只有当内核通过 `module_ready` 表示「我能收下一个」时，才放行下一次输入握手。

头注释点出它的职责与那条「不喂太快」的约束：

> This Pipeline to Pulse module converts a pipeline input with a ready/valid handshake into a pulse input interface and prevents updating the input faster than the connected module can handle, based on a separate signal which indicates that new data can be accepted, usually from a similar output handshake interface.

[Pipeline_to_Pulse.v:20-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L20-L25)——「据一个单独的 ready 信号防止更新太快」，这个信号就是从输出侧回送来的 `module_ready`。

#### 4.2.2 核心流程

模块要回答三个问题，答案都用组合逻辑给出：

1. **数据怎么传？** 直通：`module_data_in = data_in`。
2. **脉冲何时发？** 当输入握手完成的那一拍：`module_data_in_valid = (valid_in && ready_in)`。因为 `ready_in` 之后会被「掐断」，这个本会持续的电平自然就被**截成了一个单周期脉冲**。
3. **`ready_in` 何时为真？** 这是最绕的部分，分三种情形：
   - **初始（还没做过任何握手）**：必须为 1，否则第一个握手永远完不成（内核天生没有输入 ready 信号）。
   - **握手刚完成**：立刻为 0，等内核算完。
   - **内核报 `module_ready`**：为 1，允许下一个握手——并且 `module_ready` **直接旁路**到 `ready_in`，省掉一拍延迟。

核心机关是用**两个 `Pulse_Latch`** 把「是否做过首次握手」和「内核当前是否就绪」这两件**互相矛盾**的事分开存（见 4.2.3）。

#### 4.2.3 源码精读

参数与端口——注意 `module_data_in`/`module_data_in_valid`/`ready_in` 都是 `output reg`（来自本模块组合逻辑），`WORD_WIDTH` 默认 0（u2-l2 的安全栅栏）：

[Pipeline_to_Pulse.v:32-59](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L32-L59)——`initial` 把 `ready_in` 置 1（与下方逻辑自洽，仅为仿真）、把脉冲输出清零。

**第一段：握手完成判定 + 数据/脉冲直通。**

```verilog
reg input_handshake_done = 1'b0;

always @(*) begin
    input_handshake_done = (valid_in == 1'b1) && (ready_in == 1'b1);
end

always @(*) begin
    module_data_in       = data_in;
    module_data_in_valid = (input_handshake_done == 1'b1);
end
```

[Pipeline_to_Pulse.v:63-76](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L63-L76)——`module_data_in_valid` 直接等于 `input_handshake_done`。注释 [Pipeline_to_Pulse.v:69-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L69-L71) 点明：`input_handshake_done` 会被后面的逻辑「打断」成一拍脉冲。

**第二段（最难）：`ready_in` 的启动逻辑。** 先看作者为什么这么写，注释说得很透彻：

> We need to have `ready_in` be 1 both initially and after a `clear`, else we can't complete the initial input handshake ... and nothing would ever start. This initial state contradicts the use of "clear" to bring `ready_in_latched` back to zero once the input handshake is done. So we instead keep that initial state in a separate pulse latch.

[Pipeline_to_Pulse.v:78-86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L78-L86)——矛盾在于：`ready_in` 初始得是 1（才能启动），但 `clear` 又要把 `ready_in_latched` 拉回 0。这两个要求打架，于是把「初始就绪」单独存进另一个锁存器。

第一个 `Pulse_Latch` 存「**有没有做过首次握手**」：

```verilog
Pulse_Latch #(.RESET_VALUE(1'b0)) generate_initial_ready_in (
    .clock     (clock),
    .clear     (clear),
    .pulse_in  (input_handshake_done),
    .level_out (initial_ready_in)
);
```

[Pipeline_to_Pulse.v:87-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L87-L99)——它上电为 0，**首次** `input_handshake_done` 一来就置 1 并一直保持（直到 `clear`）。所以 `initial_ready_in == 1'b0` **仅**在「从未握过手」时成立——这正是「强制允许第一次」的那个开关。

第二个 `Pulse_Latch` 存「**内核当前是否就绪**」：

```verilog
reg clear_ready_in_latched = 1'b0;
always @(*) begin
    clear_ready_in_latched = (input_handshake_done == 1'b1) || (clear == 1'b1);
end

Pulse_Latch #(.RESET_VALUE(1'b0)) generate_ready_in_latched (
    .clock     (clock),
    .clear     (clear_ready_in_latched),
    .pulse_in  (module_ready),
    .level_out (ready_in_latched)
);
```

[Pipeline_to_Pulse.v:105-123](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L105-L123)——内核报 `module_ready` 时它被置 1（记住「可以收下一个了」）；一旦输入握手完成（或 `clear`），它就被清 0。于是 `ready_in_latched` 在「内核就绪」与「下一次握手完成」之间保持为 1。

最后，三者拼成 `ready_in`：

```verilog
always @(*) begin
    ready_in = (initial_ready_in == 1'b0) || (ready_in_latched == 1'b1) || (module_ready == 1'b1);
end
```

[Pipeline_to_Pulse.v:129-131](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L129-L131)——三个条件任一为真则就绪：① 还没握过手（首次强制就绪）；② 内核已就绪且本笔还没握完（锁存就绪）；③ 内核**这一拍**报了 `module_ready`（直接旁路，省一拍延迟）。注释 [Pipeline_to_Pulse.v:125-127](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L125-L127) 解释了第三项「旁路」的意义：去掉一拍延迟，同时用锁存项兜底「没能马上握完」的情形。

把 [Pipeline_to_Pulse.v:32-51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L32-L51) 的端口表对照 [Pulse_Latch.v:10-19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Latch.v#L10-L19) 看，能确认两个 `Pulse_Latch` 都只是 `Register` 的固定接法（u15-l1），模块自身零触发器，全靠复用。

#### 4.2.4 代码实践

**实践目标**：手算一次「首次握手 → 等内核 → 内核就绪 → 第二次握手」的逐拍 `ready_in`，验证那段三选一逻辑。

**操作步骤（含示例代码）**：

1. 给 `Pipeline_to_Pulse` 配一个极简的「假内核」：`module_ready` 在收到输入后过 2 拍才拉高 1 拍。把 ready/valid 侧的 `valid_in` 常拉高（始终有数据要送）。

```verilog
// 示例代码：仅供阅读理解，非项目原有文件
reg valid_in    = 1'b1;            // 上游始终有数据
wire ready_in;                     // 本模块输出
reg  [7:0] data_in = 8'hAB;
wire [7:0] module_data_in;
wire       module_data_in_valid;
reg        module_ready = 1'b0;    // 假内核：自己控制何时 ready

Pipeline_to_Pulse #(.WORD_WIDTH(8)) cvt
(
    .clock(clock), .clear(1'b0),
    .valid_in(valid_in), .ready_in(ready_in), .data_in(data_in),
    .module_data_in(module_data_in),
    .module_data_in_valid(module_data_in_valid),
    .module_ready(module_ready)
);
```

2. 逐拍推演 `initial_ready_in`、`ready_in_latched`、`ready_in`（参考 4.2.3 的公式）。注意 `Pulse_Latch` 的输出比其 `pulse_in` **晚一拍**（它是 `Register`）。
3. 在第 1 拍握手完成后，把 `module_ready` 故意保持 0 几拍，观察 `ready_in` 是否一直为 0（反压住上游）；再拉高 `module_ready` 一拍，观察 `ready_in` 是否同拍变 1。

**需要观察的现象 / 预期结果**：

- 上电后 `ready_in` 立刻为 1（`initial_ready_in==0`），首次握手得以完成，`module_data_in_valid` 发出**一拍**脉冲。
- 握手完成那拍之后 `ready_in` 掉到 0（`initial_ready_in` 变 1、`ready_in_latched` 被清），上游被反压，`module_data_in_valid` 不再发。
- `module_ready` 拉高那一拍，`ready_in` **同拍**为 1（旁路生效），下一次握手可完成。

> 待本地验证：在仿真里应看到 `module_data_in_valid` 是严格的单周期脉冲，且两次脉冲的间距由你给 `module_ready` 的节奏决定——这正是「不喂太快」。

#### 4.2.5 小练习与答案

**练习 1**：`module_data_in_valid` 没有任何「拉高一拍再回落」的显式逻辑，为什么它最终是个单周期脉冲而不是持续电平？

**参考答案**：因为 `module_data_in_valid = input_handshake_done = valid_in && ready_in`，而 `ready_in` 在握手完成的那一拍之后会被（通过清 `ready_in_latched`）拉到 0。所以 `input_handshake_done` 只能在「`ready_in` 还为 1 的那一拍」成立，下一拍 `ready_in` 一掉，脉冲自然结束。注释 [Pipeline_to_Pulse.v:69-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L69-L71) 说的「会被后面的逻辑打断成一拍脉冲」就是这意思。

**练习 2**：如果删掉第三个条件 `(module_ready == 1'b1)`，只保留前两项，模块还能工作吗？会有什么代价？

**参考答案**：仍能工作，但**慢一拍**。没有旁路时，`module_ready` 必须先被第二个 `Pulse_Latch` 锁存成 `ready_in_latched`（晚一拍），`ready_in` 才会变 1，于是从「内核就绪」到「真正放行下一次握手」多出一拍空档。第三项把 `module_ready` 直接旁路，就是为了去掉这一拍延迟、保住吞吐，同时用第二项锁存兜底「内核就绪了但上游这拍没能完成握手」的情形。

**练习 3**：为什么作者要分两个 `Pulse_Latch`，而不是把 `ready_in_latched` 的初值直接设成 1？

**参考答案**：因为 `clear` 的语义是「把 `ready_in_latched` 清回 0」（[Pipeline_to_Pulse.v:105-109](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L105-L109)）。若让它初值为 1，则每次 `clear` 之后都无法回到「强制就绪以启动」的状态，第一次握手就再也完不成了。「初始就绪」和「clear 后归零」是两个方向相反的要求，塞进一个寄存器会自相矛盾，所以拆成两个：`initial_ready_in` 只记「有没有握过手」、`ready_in_latched` 只记「内核现在就绪与否」，各管各的，互不打架。

---

### 4.3 Pulse_to_Pipeline：把脉冲输出翻译成 ready/valid 输出

#### 4.3.1 概念说明

`Pulse_to_Pipeline` 解决输出侧的翻译：内核算完后给出结果 `module_data_out` 和一个 **done 脉冲** `module_data_out_valid`，本模块把它包装成 ready/valid 输出（`valid_out`/`ready_out`/`data_out`）。一旦下游把结果读走（完成输出握手），就回送 `module_ready` 一拍，告诉内核「上一个结果我收走了、你可以接下一个输入」。

头注释点出它的角色和那条「切断反向组合路径」的必要性：

> Wraps a module with an output pulse interface inside a ready/valid output handshake interface. *The connected module must have at least one pipeline stage from input to output. No combinational paths allowed else the input and output handshake logic will form a loop.*

[Pulse_to_Pipeline.v:4-7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L4-L7)——内核至少要有一级寄存器，否则输入/输出握手逻辑会成环（呼应 4.1.5 练习 2）。

为什么需要输出缓冲？因为 `module_ready = output_handshake_done`，而 `output_handshake_done` 又依赖于下游的 `ready_out`。如果不插一个寄存器切断，下游的 `ready_out` 就会**组合地**决定 `module_ready` 何时拉高，与内核可能的组合反馈耦合成环。本书的解法是在输出握手处插一个弹性缓冲（Skid/Half/FIFO），用它的内部寄存器切断这条反向组合路径，同时还能吸收速率失配。

#### 4.3.2 核心流程

模块分四步：

1. **锁存 done 脉冲**：用 `Pulse_Latch` 把 `module_data_out_valid` 冻成电平 `valid_out_latched`，直到输出握手完成才清。
2. **直通 + 锁存合一**：`valid_out_internal = valid_out_latched || module_data_out_valid`。直通那一项省掉一拍延迟（done 来的当拍就有效），锁存那一项兜底「下游没立刻 ready」的情形。
3. **输出握手完成判定**：`output_handshake_done = valid_out_internal && ready_out_internal`，其中 `ready_out_internal` 是输出缓冲的输入侧 ready。
4. **回送 `module_ready`**：`module_ready = output_handshake_done`——结果被缓冲收下的那一拍，就告诉内核「可收下一个」。

输出缓冲的类型由参数 `OUTPUT_BUFFER_TYPE` 在精化期三选一：`"HALF"` / `"SKID"` / `"FIFO"`，分别实例化 `Pipeline_Half_Buffer` / `Pipeline_Skid_Buffer` / `Pipeline_FIFO_Buffer`。

#### 4.3.3 源码精读

参数与端口——`OUTPUT_BUFFER_TYPE` 选缓冲类型，`module_ready` 是 `output reg`：

[Pulse_to_Pipeline.v:54-81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L54-L81)。

**第一段：输出握手完成 + 回送 ready。**

```verilog
reg  valid_out_internal     = 1'b0;
wire ready_out_internal;
reg  output_handshake_done  = 1'b0;

always @(*) begin
    output_handshake_done = (valid_out_internal == 1'b1) && (ready_out_internal == 1'b1);
    module_ready          = (output_handshake_done == 1'b1);
end
```

[Pulse_to_Pipeline.v:89-96](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L89-L96)——`ready_out_internal` 来自缓冲的输入侧 ready。缓冲收下数据（两边都高）的那一拍，`module_ready` 拉高，回送给内核。

**第二段：锁存 done 脉冲 + 直通。**

```verilog
Pulse_Latch #(.RESET_VALUE(1'b0)) generate_valid_out_latched (
    .clock     (clock),
    .clear     (output_handshake_done),
    .pulse_in  (module_data_out_valid),
    .level_out (valid_out_latched)
);

always @(*) begin
    valid_out_internal = (valid_out_latched == 1'b1) || (module_data_out_valid == 1'b1);
end
```

[Pulse_to_Pipeline.v:101-121](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L101-L121)——done 脉冲来时，`valid_out_internal` **当拍**就为 1（直通项），省一拍延迟；若下游没立刻 ready，`Pulse_Latch` 把它冻成电平保持，直到 `output_handshake_done` 清掉它。注释 [Pulse_to_Pipeline.v:115-117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L115-L117) 说清了这两项的分工。

**第三段：用 `generate` 选输出缓冲。** 这是切断反向组合路径的关键：

```verilog
generate
    if (OUTPUT_BUFFER_TYPE == "HALF") begin : gen_half
        Pipeline_Half_Buffer #(...) output_buffer (...);
    end
    else if (OUTPUT_BUFFER_TYPE == "SKID") begin : gen_skid
        Pipeline_Skid_Buffer  #(...) output_buffer (...);
    end
    else if (OUTPUT_BUFFER_TYPE == "FIFO") begin : gen_fifo
        Pipeline_FIFO_Buffer  #(...) output_buffer (...);
    end
endgenerate
```

[Pulse_to_Pipeline.v:131-196](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L131-L196)——三种缓冲的端口接法完全一致（`input_valid=valid_out_internal`、`input_ready=ready_out_internal`、`input_data=module_data_out`、输出去下游），区别只在内部行为。注释 [Pulse_to_Pipeline.v:123-129](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L123-L129) 解释：Half Buffer 会堵住 `module_ready` 直到被读走、只缓冲最早一笔；Skid Buffer 允许重叠（边收下一笔边等读走上一笔）、最多持 2 笔；FIFO 可持 `FIFO_BUFFER_DEPTH` 笔。

**怎么选缓冲？** 注释 [Pulse_to_Pipeline.v:31-50](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L31-L50) 给了选型指南，整理成表：

| 缓冲类型 | 何时选 | 对内核计算的重叠影响 |
| --- | --- | --- |
| **Skid**（默认推荐） | 一般情况 | 允许内核在上一笔结果等待读走时**立刻开算下一笔** |
| **Half** | 不允许内核在上一笔被读走前开算下一笔 | 内核必须等输出缓冲空了才能再算 |
| **FIFO** | 下游读取是**突发式**的（一阵快一阵慢） | 内核可连续算多笔攒在 FIFO 里 |
| `OUTPUT_BUFFER_CIRCULAR` 置非 0 | 永远只要「最新」结果 | 中间未被读走的结果会被丢弃 |

> 一个 Kahn 过程网络（Kahn Process Network）意义上的约束：注释 [Pulse_to_Pipeline.v:21-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L21-L25) 指出——内核的结果**只能被读出一次**，即使它在下次更新前一直保持。这是为了让内核能把自己的输出寄存器**复用**进计算（而不必另开缓冲），缓冲这件事由本模块的 `output_buffer` 承担。

#### 4.3.4 代码实践

**实践目标**：实例化一个 `Pulse_to_Pipeline`（选 Skid），喂一个「done 脉冲 + 结果」，观察它如何变成 ready/valid 输出，并验证下游反压时脉冲会被正确「冻住」。

**操作步骤（含示例代码）**：

1. 实例化 `Pulse_to_Pipeline`，`OUTPUT_BUFFER_TYPE` 选 `"SKID"`。模拟内核每隔几拍发一个 done 脉冲；下游 `ready_out` 先保持 0（反压），几拍后再放开。

```verilog
// 示例代码：仅供阅读理解，非项目原有文件
reg  [7:0] module_data_out       = 8'h00;
reg        module_data_out_valid = 1'b0;   // 内核的 done 脉冲
wire       valid_out;
reg        ready_out = 1'b0;               // 下游 ready，先反压
wire [7:0] data_out;
wire       module_ready;

Pulse_to_Pipeline
#(
    .WORD_WIDTH             (8),
    .OUTPUT_BUFFER_TYPE     ("SKID"),
    .OUTPUT_BUFFER_CIRCULAR (0)
)
cvt_out
(
    .clock(clock), .clear(1'b0),
    .valid_out(valid_out), .ready_out(ready_out), .data_out(data_out),
    .module_data_out(module_data_out),
    .module_data_out_valid(module_data_out_valid),
    .module_ready(module_ready)
);

// 测试意图：
// (a) 第 N 拍给 module_data_out_valid 一拍脉冲 -> 当拍 valid_out 应为 1
// (b) ready_out 保持 0 -> valid_out 保持 1（脉冲被冻住），data_out 保持该结果
// (c) 数拍后 ready_out=1 -> 完成输出握手，module_ready 同拍为 1（回送内核）
```

2. 对照 [Pulse_to_Pipeline.v:101-121](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L101-L121)，解释为什么 done 脉冲来那拍 `valid_out` 就为 1（直通），以及为什么下游不 ready 时 `valid_out` 仍保持 1（`Pulse_Latch` 冻结）。
3. 把 `OUTPUT_BUFFER_TYPE` 改成 `"HALF"`，重复观察：预期 `module_ready` 会**更晚**才拉高（Half Buffer 要等被读走才放行），从而「卡住」内核不让它开下一笔。

**需要观察的现象 / 预期结果**：

- done 脉冲与 `valid_out` 的首次拉高**同拍**（直通生效，无延迟）。
- 下游反压期间 `valid_out` 保持 1、`data_out` 保持结果不变——单周期脉冲被安全冻成了电平。
- 下游 ready 那拍完成握手，`module_ready` 同拍为 1。
- 换成 Half Buffer 后，重叠能力消失：内核必须等输出被读走才能继续。

> 待本地验证：在仿真里应确认 Skid 模式下「下游反压」不会丢结果（脉冲被 `Pulse_Latch` 冻住），且 Half 与 Skid 在 `module_ready` 时机上的差异。

#### 4.3.5 小练习与答案

**练习 1**：`valid_out_internal = valid_out_latched || module_data_out_valid` 里为什么要有 `module_data_out_valid` 这个直通项？去掉它行不行？

**参考答案**：直通项是为了**省一拍延迟**——`Pulse_Latch` 是 `Register`，其输出比 `pulse_in` 晚一拍；若只靠锁存项，`valid_out` 要等 done 脉冲的**下一拍**才有效。加上直通项后，done 来的**当拍** `valid_out` 就有效，下游若恰好 ready，输出握手当拍就能完成、当拍就能回送 `module_ready`，吞吐最佳。锁存项则兜底「下游没立刻 ready」——把脉冲冻住等下游。注释 [Pulse_to_Pipeline.v:115-117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L115-L117) 即此意。

**练习 2**：为什么 `module_ready` 必须由「输出缓冲收下数据」触发（`output_handshake_done`），而不是由「下游读走数据」触发？

**参考答案**：因为一旦结果进了输出缓冲，**内核的输出寄存器就被腾出来**可以复用于下一笔计算了（见注释 [Pulse_to_Pipeline.v:21-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L21-L25)）。所以「可收下一个输入」的正确时机是「结果已被缓冲收下」，而不是「结果已被最终下游读走」——后者是缓冲自己的事，与内核无关。这也正是 Skid 缓冲能带来重叠的原因：内核不必等下游读走，只要缓冲收下就能开下一笔。

**练习 3**：注释说结果「只能被读出一次」。如果下游反压很久、其间内核又发了第二个 done 脉冲（Skid 模式下），会怎样？

**参考答案**：Skid 缓冲最多持 2 笔（注释 [Pulse_to_Pipeline.v:126-127](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L126-L127)）。第一笔进缓冲后 `module_ready` 回送、内核可开第二笔；第二笔 done 来时若缓冲还没被读走，缓冲将**堵住** `module_ready`（`ready_out_internal` 为 0），内核被反压，直到下游读走一笔。这正是注释 [Pulse_to_Pipeline.v:8-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L8-L13) 说的「两笔待读时会停顿」。结果始终只读出一次，不会重复。

---

### 4.4 与 CDC 及握手的关系

本讲两个转换器**只在一个时钟域内**工作——它们不是同步器，内部没有 `CDC_Bit_Synchronizer` 那种亚稳态防护。但它们暴露的**脉冲接口**，恰好是连接 CDC 世界的天然钩子，理解这一点能帮你把本书的 CDC 单元（u13、u14）和本单元串起来。

**与握手纪律的关系。** 「`module_ready` + done 脉冲」本质上是一种**会合式握手**（u9-l2、u11-l2 讲过的 rendez-vous）：输入侧完成握手 → 内核计算 → 输出侧完成握手 → 回送 `module_ready` → 允许下一次输入。这整套往返，等价于一次「进—算—出」的异步握手事务。两个转换器之所以反复强调「内核不得有组合直通」（[Pulse_to_Pipeline.v:4-7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L4-L7)、[Pipeline_to_Pulse.v:9-11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L9-L11)），正是 u9-l2 那条「接口内不得有组合环」铁律在本场景的具体化身——内核那一拍寄存器延迟，是打断回送环路、避免组合振荡的唯一手段。

**与 CDC 的关系。** 脉冲是**最适合跨时钟域**的事件载体：一个单周期脉冲可以通过 `CDC_Pulse_Synchronizer_2phase`/`_4phase`（u14-l1）安全地传到另一个时钟域。所以，如果你想把一个迭代内核**拆到另一个时钟域**运行，最干净的做法是：

```text
域A: ready/valid → Pipeline_to_Pulse → 脉冲 →[CDC_Pulse_Synchronizer]→ 脉冲 → 内核(域B)
域B: 内核 → 脉冲 →[CDC_Pulse_Synchronizer]→ 脉冲 → Pulse_to_Pipeline → ready/valid (域A)
```

即用 `CDC_Pulse_Synchronizer` 替换本讲框图里「内核到转换器」的脉冲连线，ready/valid 包装仍留在各自的域内。这也解释了为什么本书要把脉冲接口做得这么「纯」——它越纯，越容易插同步器跨域。注意：`module_ready` 这类**电平/握手**信号若也要跨域，须走 `CDC_Word_Synchronizer` 或回到脉冲形式过 `CDC_Pulse_Synchronizer`，绝不能裸传多位（u13-l1 的「每次只能同步一位」铁律）。

> 复用对照：u14-l1 里 `CDC_Pulse_Synchronizer` 的 2 相 toggle + 边沿还原，正是 u15-l1 的 `Pulse_Generator`（还原边沿）+ 一个 toggle 寄存器；而本讲的 `Pulse_to_Pipeline`/`Pipeline_to_Pulse` 又是用 u15-l1 的 `Pulse_Latch` 拼出来的。这些零件层层复用，是本书「构建块库」的典型面貌。

## 5. 综合实践

把本讲两个转换器接在一个迭代内核两侧，复刻 `Averager_Powers_of_Two` 的往返结构，并回答一连串关于握手时序与缓冲选型的问题。

**场景**：你要设计一个「吃 N 个样本、求和、输出总和」的模块 `Summer_N`，对外暴露 ready/valid；内核是一个 `Accumulator_Binary`，每收一个样本累加一拍，攒够 N 个发一次 done。

**任务**：

1. **画框图、标信号**：仿照 4.1.2，画出 `Summer_N` 的内部结构（`Pipeline_to_Pulse` → 累加器 → `Pulse_to_Pipeline`），标出每根 ready/valid 信号、脉冲信号、以及 `module_ready` 回送线。指出哪两个实例对应 [Averager_Powers_of_Two.v:84-104](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L84-L104) 与 [Averager_Powers_of_Two.v:262-286](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L262-L286)。
2. **缓冲选型论证**：`Averager_Powers_of_Two` 的输出侧选了 `"SKID"`（[Averager_Powers_of_Two.v:262-269](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L262-L269)）。请据 [Pulse_to_Pipeline.v:31-50](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L31-L50) 解释：为什么这里选 Skid 而不是 Half？什么下游读取模式下你会改选 FIFO？
3. **启动逻辑核验**：`Summer_N` 上电后第一次输入握手为什么一定能完成？引用 [Pipeline_to_Pulse.v:78-86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L78-L86) 与 [Pipeline_to_Pulse.v:129-131](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L129-L131) 说明。
4. **组合环排查**：若有人图省事把累加器换成「零延迟纯组合加法」（不要任何寄存器），这个 `Summer_N` 会在哪里形成组合环？引用 4.1.5 练习 2 与 [Pulse_to_Pipeline.v:4-7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L4-L7) 解释为什么不行。
5. **跨域改造**：若把累加器挪到另一个更快的时钟域，你会把本讲哪个模块插到脉冲通路上？为什么不能直接把 `module_data_out_valid` 这根多位/电平信号裸拉过去？（联系 u13-l1、u14-l1）

**参考要点**：

1. 左 `bring_em_in`（`Pipeline_to_Pulse`）把 `input_valid`/`input_ready`/`input_sample` 翻成 `sample_valid` 脉冲；右 `bring_em_out`（`Pulse_to_Pipeline`）把 done 脉冲翻成 `output_valid`/`output_ready`/`output_average`；`module_ready` 由 [Averager_Powers_of_Two.v:338-341](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Averager_Powers_of_Two.v#L338-L341) 的 `input_sample_next`/`truncated_average_valid` 控制回送。
2. 选 Skid 是为了让累加器在「上一笔平均值还没被读走」时就能开始攒下一笔（重叠计算与等待，见 [Pulse_to_Pipeline.v:39-41](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L39-L41)）；若下游读取是突发式（一阵读一阵不读），改选 FIFO 可攒多笔（[Pulse_to_Pipeline.v:45-47](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_to_Pipeline.v#L45-L47)）。
3. 上电时 `initial_ready_in` 为 0，使 [Pipeline_to_Pulse.v:129-131](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L129-L131) 第一项成立，`ready_in=1`，故首次握手必能完成（注释 [Pipeline_to_Pulse.v:78-82](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_to_Pulse.v#L78-L82)）。
4. 环路为 `module_ready → ready_in → input_handshake_done → module_data_in_valid →（组合加法）→ module_data_out_valid → valid_out_internal → output_handshake_done → module_ready`，全组合则振荡；故内核必须至少一级寄存器。
5. 在脉冲通路上插 `CDC_Pulse_Synchronizer_2phase`（默认，吞吐更高）。不能裸传，因为多位/电平跨域会撞上 u13-l1 的「每次只能同步一位」铁律——电平信号得先转成 toggle 脉冲再同步。

## 6. 本讲小结

- **两种接口方言**：ready/valid 弹性接口能反压变速，是流水线通用语；脉冲接口更轻、天然适合 **initiation interval > 1** 的迭代模块（累加、除法等带反馈回路的计算）。
- **`Pipeline_to_Pulse`** 把 ready/valid **输入**翻成单周期脉冲：数据直通、`module_data_in_valid = valid_in && ready_in`（被后续逻辑掐成一拍脉冲）；最绕的是 `ready_in` 用**两个 `Pulse_Latch`** 分别存「是否握过手」和「内核是否就绪」，以化解「初始就绪」与「clear 归零」的矛盾，并靠直通 `module_ready` 旁路省一拍延迟。
- **`Pulse_to_Pipeline`** 把内核的 **done 脉冲**翻成 ready/valid **输出**：`Pulse_Latch` 冻脉冲、直通项省延迟；`module_ready = output_handshake_done`（结果被缓冲收下即回送）。**必须插输出缓冲**（Skid/Half/FIFO 三选一）切断 `ready_out → module_ready` 的反向组合路径，缓冲类型决定能否重叠计算与读取。
- 两个转换器都硬性要求**内核至少有一级寄存器、不得组合直通**——内核那一拍延迟是打断 `module_ready` 回送环、避免组合振荡的唯一手段，是 u9-l2「接口内不得有组合环」铁律在本场景的具体化身。
- 它们**只在单时钟域内**工作、不是同步器；但暴露的脉冲接口正是插 `CDC_Pulse_Synchronizer` 跨域的天然钩子，从而把本单元与 u13/u14 的 CDC 单元串成一体。
- 两者都由 `Pulse_Latch`（u15-l1）和现成的弹性缓冲（u10-l1、u12-l1）拼出，模块自身几乎零时序逻辑，是本书「构建块库」哲学的又一范例。

## 7. 下一步学习建议

- 想看更复杂的「由构建块组装的迭代引擎」，进入 **u17-l1（Pipeline_Iterator 与握手乘法器）**：那里的迭代计算引擎正是用本讲两个转换器加握手数据通路组装出来的，本讲是其零件基础。
- 想深入「输出缓冲三选一」的内部机理，回看 **u10-l1（Skid Buffer）**、**u10-l2（Half Buffer）**、**u12-l1（FIFO/Credit/Stall Smoother）**——理解它们的 `ready`/`valid` 如何切断组合路径、吞吐与面积如何取舍。
- 想把脉冲真正跨时钟域，回看 **u14-l1（字同步与脉冲同步）** 与 **u14-l2（Flancter 与 CDC FIFO）**：`CDC_Pulse_Synchronizer` 的 2/4 相握手、toggle + 边沿还原，正是接在本讲脉冲接口上的同步手段。
- 若要亲手验证本讲模块，参考 **u18-l2（仿真、测试台与综合验证）**：用 `Simulation_Clock` 与 `Synthesis_Harness` 搭测试台，把 4.2.4 / 4.3.4 的示例序列在仿真里跑出波形。
