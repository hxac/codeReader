# 死锁/活锁避免与底层同步

## 1. 本讲目标

上一讲（u9-l1）我们确立了 ready/valid 握手接口的「接口定义、握手过程、AXI 不根本」三件套。本讲继续往下挖一层，回答四个问题：

1. 为什么 ready/valid 接口内部**禁止组合路径**（组合环），违反了会怎样？
2. 死锁（deadlock）和活锁（livelock）分别是怎么产生的？怎样用**信号约束**避免它们？
3. 为什么模块内部状态**只能在握手完成的那一拍改变**？怎样用一行 `handshake_complete` 把这条规则落地？
4. ready/valid 之下还藏着什么更根本的东西？——会合同步（rendez-vous，OK_IN/OK_OUT）。

学完后，你应该能：
- 用信号约束正确写出不会死锁、不会活锁的 ready/valid 接口；
- 写出 `handshake_complete` 的组合表达式，并用它门控任何影响接口的内部状态；
- 解释 Skid Buffer 里 `insert`/`remove` 为什么就是两次 `handshake_complete`；
- 理解 ready/valid 只是「会合同步 + 单向数据流」的一种实现，并能看懂 OK_IN/OK_OUT 同步模型。

## 2. 前置知识

本讲假设你已经掌握（来自 u9-l1）：

- **source / destination 接口**：source 输出 `valid`+`data`、接收 `ready`；destination 输出 `ready`、接收 `valid`+`data`；连接恒由 source 到 destination，所有信号同步于时钟上升沿。
- **握手完成**：同一拍 `valid` 与 `ready` 同时为高即完成一次传输。

此外需要一点数字电路基础（来自 u2/u3）：

- **组合逻辑与时序逻辑**：组合输出随输入即时变化；时序输出只在时钟边沿更新，由寄存器持有。
- **组合环（combinational loop）**：若干组合门首尾相接形成无寄存器打断的反馈环，会导致输出无法稳定，是电路大忌。
- **阻塞赋值 `=` 与非阻塞赋值 `<=`**：组合块用 `=`，时钟块用 `<=`。

## 3. 本讲源码地图

本讲只精读两个文件，它们一抽象一具体、互为印证：

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| `handshake.html` | ready/valid 握手**规则正文** | 给出禁止组合环、避免死锁/活锁、内部状态门控、会合同步的理论规则 |
| `Pipeline_Skid_Buffer.v` | 规则的**范本实现** | 用 `insert`/`remove` 落地 `handshake_complete`，用 `state_next` 隔离两个接口，证明规则可工程化 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**组合环与死锁/活锁**、**handshake_complete 门控内部状态**、**会合同步**。

### 4.1 组合环与死锁/活锁

#### 4.1.1 概念说明

ready/valid 接口把模块连成链。连接一旦发生，就出现一个潜在危险：**组合环**。

回顾接口信号方向：`valid`/`data` 由 source 指向 destination，`ready` 由 destination 指回 source。如果在**同一个接口内部**，允许 `ready` 组合地驱动 `valid`（source 接口），或允许 `valid` 组合地驱动 `ready`（destination 接口），那么两个接口一对接，就会形成一个完全在组合逻辑里打转的环——信号绕一圈又一圈，永远到不了寄存器，电路无法收敛。

这里要先区分两个容易混的概念：

- **死锁（deadlock）**：双方都在「等对方先动」，于是谁也不动，握手永远不发生。
- **活锁（livelock）**：双方都在动，但动作总错开，`valid` 和 `ready` 此起彼伏却永不同时为高，握手同样永远不完成。

#### 4.1.2 核心流程

避免组合环与死锁/活锁，靠三条对信号行为的约束：

**约束 A：接口内部不得有组合路径（防组合环）**

| 接口类型 | 禁止的组合路径 |
|----------|----------------|
| source | `ready → valid` |
| destination | `valid → ready` |

违反任何一条，对接后即成环；哪怕只有一边成环、另一边没成环，残留的那条组合路径仍会在两个接口间来回绕一圈，拉长关键路径、拖慢时钟频率、恶化布局布线。

**约束 B：source 不得等 ready 才拉 valid（防死锁）**

- source：**不许**等到 destination 先拉高 `ready` 才拉高 `valid`。
- destination：**可以**等 source 先拉高 `valid` 才拉高 `ready`。

这是一个不对称约束：destination 允许「观望」，source 不允许。两者都观望就谁也不肯先动——死锁。

**约束 C：valid 一旦拉高就必须保持，直到握手完成（防活锁）**

若 source 拉一下 `valid` 又放下，destination 也拉一下 `ready` 又放下，二者总错峰，就活锁。所以 `valid` 必须是「拉起后保持」(raise and hold steady)，直到某拍 `valid & ready` 同高、握手完成，才允许改变。

把三条约束合起来，安全握手的伪代码如下：

```
source 侧:
    有数据 -> 拉高 valid 并保持, 不得依据 ready 决定是否拉 valid
    (valid & ready) 同高那一拍 -> 一次传输完成, 此后可放下 valid 或换下一笔

destination 侧:
    能收 -> 拉高 ready (可等 valid 再拉, 也可先拉)
    (valid & ready) 同高那一拍 -> 收下数据

两侧: valid 不得在本接口内被 ready 组合驱动, ready 不得在本接口内被 valid 组合驱动
```

#### 4.1.3 源码精读

**禁止组合环（规则正文）。** `handshake.html` 明确要求 source 与 destination 两种接口内部都不得有从输入信号到输出信号的组合路径，否则对接即成环：

[handshake.html:61-63](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L61-L63) —— 规定 source 接口不得有 `ready→valid`、destination 接口不得有 `valid→ready` 的组合路径，否则对接形成组合环。

正文接着补一句：哪怕只有一种接口成环、侥幸没产生组合环，残留的组合路径仍会在两接口间往返，拖慢频率、恶化 P&R：

[handshake.html:65-72](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L65-L72) —— 说明「省一拍延迟却拖累整个设计」不划算，正确做法是用流水线寄存器既避免组合环又提升频率。

正文还有一条务实附注（ADDENDUM）：理论归理论，实践中允许接口内 `valid` 与 `ready` 间有组合路径有时是合理折中——不是每条连接都值得加缓冲。但一旦放任组合路径，就必须自己小心组合环与死锁/活锁：

[handshake.html:74-81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L74-L81) —— ADDENDUM：允许组合路径可以，但必须自行承担防组合环与防死锁/活锁的责任。

**范本实现：Skid Buffer 如何斩断组合路径。** `Pipeline_Skid_Buffer.v` 的核心思路正是「用一拍寄存器把输入侧与输出侧解耦」，从而彻底消除两侧之间的组合路径。作者把这条设计基线标注为「关键的一小段代码」：

[Pipeline_Skid_Buffer.v:283-286](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L283-L286) —— 这一小段代码暗含 skid buffer 的根本假设：一个接口的当前状态**不得**依赖另一个接口的当前状态，否则就是两接口间的组合路径。

具体地，输入侧的 `input_ready` 是用 `state_next`（寄存过的下一拍状态）算出来的，**不是**用输出侧当前的 `output_valid`/`output_ready` 直接组合出来：

[Pipeline_Skid_Buffer.v:291-303](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L291-L303) —— `input_ready` 取自 `(state_next != FULL)`（或 Circular Buffer Mode 下恒为 1），经 `Register` 打一拍输出，与输出侧握手信号无组合依赖。

这就是约束 A 的工程落地：两接口都被寄存器隔开，没有组合往返。

**避免死锁/活锁（规则正文）。** `handshake.html` 把约束 B、C 写得很清楚：

[handshake.html:151-157](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L151-L157) —— 防死锁：source 不得等 ready 才拉 valid；destination 可以等 valid 才拉 ready。这种「等」会把握手拖到第二拍，但恰好可用于仲裁器选择性地完成某一路握手。

[handshake.html:159-163](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L159-L163) —— 防活锁：source 拉高 valid 后必须保持，直到握手完成，否则 valid/ready 各自闪一下却永不同拍同高。

#### 4.1.4 代码实践

**实践目标**：用一个反例直观理解「source 等 ready 才拉 valid」为何是死锁温床，并把它改成符合约束 B 的写法。

**操作步骤**：

1. 阅读下面两段「示例代码」（非项目原有代码，仅用于对照）：

```verilog
// 示例代码 —— 危险写法（违反约束 B，易死锁）
always @(posedge clock) begin
    // 错误：valid 依赖 ready。若 destination 也等 valid 才拉 ready，
    //       双方互相等待 -> 死锁。
    output_valid <= (input_ready == 1'b1) ? data_available : output_valid;
end
```

```verilog
// 示例代码 —— 安全写法（符合约束 B + C）
always @(posedge clock) begin
    // 正确：valid 只由「是否有数据」决定，与 ready 无关；拉高后保持到握手完成。
    if (data_available && !output_valid)
        output_valid <= 1'b1;
    else if (output_valid && output_ready)  // 握手完成才放下
        output_valid <= 1'b0;
end
```

2. 在脑中（或仿真里）跟踪「destination 也写成 `ready <= (valid == 1'b1)`」时两段代码各自的握手时序。

**需要观察的现象**：
- 危险写法下，若双方都互等，`output_valid` 与对端 `ready` 永远停在 0，无任何传输。
- 安全写法下，`output_valid` 一旦有数据就拉起并保持，不依赖 `ready`，对端何时拉 `ready` 都能完成。

**预期结果**：安全写法能完成传输；危险写法在「双方都观望」时挂死。若要在仿真器中复现，需自行搭建最小 testbench，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：约束 A 说 source 接口不得有 `ready→valid` 的组合路径。如果一个模块为了省一拍延迟，故意在 source 接口里写了 `assign output_valid = output_ready & has_data;`，单独看它会形成组合环吗？接到一个正常的 destination 后会怎样？

**答案**：单独看不会（环要两段对接才闭合成圈）。但接到正常 destination 后，`ready`（destination 给 source）组合地驱动了 source 的 `valid`，而 destination 自己也可能 `valid→ready` 组合驱动，于是两接口间出现一条往返组合路径，拉长关键路径、拖慢频率；若双方都组合驱动，则成组合环、电路无法收敛。

**练习 2**：约束 B 允许 destination「等 valid 才拉 ready」，但禁止 source「等 ready 才拉 valid」。这个不对称为什么不会导致死锁？

**答案**：因为 source 的 `valid` 是**无条件**（只要有数据）拉起的，它不依赖任何对方信号。destination 无论何时决定拉 `ready`，只要 `valid` 已经高，握手就能完成。双方不会陷入「你先我后」的循环——source 总是「先动」的那一个。

---

### 4.2 handshake_complete 门控内部状态

#### 4.2.1 概念说明

上一节约束了**接口信号本身**的行为。本节约束**模块内部状态**——凡会影响 source 或 destination 接口行为的内部状态（计数器、状态机、缓冲指针……），都必须且只能在**握手完成的那一拍**改变。

为什么？因为握手完成的那一拍（`valid & ready` 同高）是数据真正被收发的唯一瞬间。如果内部状态在别的拍改变，就可能让接口给出与实际数据传输不一致的信号，导致丢数据。

`handshake.html` 举了一个非常具体的反例：你在数一个包里还剩多少个字（packet word counter）。如果你在计数器到 0 时立刻切换状态（以为包发完了），而恰好那一拍 destination 的 `ready` 拉低了（包的最后一个字还没真正发出去），那么等 destination 重新拉高 `ready` 时，source 已经跳到下一个包、把上一包的最后一个字丢了——并连带污染后续所有包。

规则因此非常简单：**先定义一行 `handshake_complete`，再用它门控所有影响接口的状态转移**。

#### 4.2.2 核心流程

落地分三步：

1. **定义握手完成信号**（组合逻辑，一行）：

```
handshake_complete = (ready == 1'b1) && (valid == 1'b1)
```

2. **把它当作使能（enable）**：凡是影响接口的内部寄存器更新，都加一道「当且仅当 `handshake_complete` 时才更新」的门控。

3. **状态在完成拍改变**：这样内部状态永远与「数据真正被传输的那一拍」对齐，不会抢跑、不会漏拍。

用伪代码表达一个「包字计数器」：

```
always @(*)
    handshake_complete = (ready == 1'b1) && (valid == 1'b1);

always @(posedge clock)
    if (handshake_complete)              // 仅在真正传输的那一拍
        if (word_count == LAST_WORD)
            word_count <= 0;             // 这笔包最后一个字确实发出去了, 才归零
        else
            word_count <= word_count + 1;
```

> 关键：归零判断被包在 `handshake_complete` 门控里，所以「计数器到 0」与「最后一个字真正发出去」被绑定在同一拍，destination 临时拉低 `ready` 不会让计数器提前跳走。

#### 4.2.3 源码精读

**规则正文：一行 handshake_complete。** `handshake.html` 把这条规则直接写成了模板代码：

[handshake.html:179-182](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L179-L182) —— 任何影响 source/destination 接口的内部状态，只能在 `valid & ready` 同高的「完成拍」改变，否则丢数据。

[handshake.html:195-197](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L195-L197) —— 模板：`handshake_complete = (ready == 1'b1) && (valid == 1'b1);`，用它门控所有影响接口的状态转移。

正文紧接着用前述「包计数器丢最后一字」的例子说明为何必须如此：

[handshake.html:184-190](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L184-L190) —— 反例：在计数器到 0 时立刻切状态，若 destination 的 `ready` 恰好同拍拉低，就丢掉上一包最后一字并污染后续所有包。

**范本实现：Skid Buffer 把 handshake_complete 用了两次。** Skid Buffer 有两个 ready/valid 接口（输入侧、输出侧），所以它定义了**两个**握手完成信号，分别命名为 `insert`（输入侧完成 = 收到一笔）和 `remove`（输出侧完成 = 送出一笔）。二者就是 `handshake_complete` 的两份实例：

[Pipeline_Skid_Buffer.v:325-331](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L325-L331) —— `insert = (input_valid && input_ready)` 是输入侧的 handshake_complete；`remove = (output_valid && output_ready)` 是输出侧的 handshake_complete。

然后，**所有**影响数据通路和接口的内部变换（`load`/`flow`/`fill`/`flush`/`unload`/`dump`/`pass`）都由 `insert`/`remove` 组合门控——也就是说，Skid Buffer 的状态机和写使能严格遵守「只在握手完成拍改变」的规则：

[Pipeline_Skid_Buffer.v:350-358](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L350-L358) —— 七种数据通路变换（load/flow/fill/flush/unload/dump/pass）每一个条件里都含 `insert` 或 `remove`，没有任何一种变换能在「没有握手完成」时发生。

[Pipeline_Skid_Buffer.v:391-395](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L391-L395) —— 数据通路写使能（`data_out_wren`/`data_buffer_wren`/`use_buffered_data`）全部由这些门控过的变换派生，因此寄存器内容同样只在握手完成拍改写。

这就是 `handshake_complete` 规则在真实模块里的样子：抽象的一行规则，落地成 `insert`/`remove` 两个使能，再由它们统一门控整个状态机与数据通路。

#### 4.2.4 代码实践

**实践目标**：自己写一遍 `handshake_complete`，并用它门控一个包字计数器——正是 `handshake.html` 反例要求的安全写法。

**操作步骤**：

1. 新建一个最小模块骨架（示例代码，非项目原有文件）：

```verilog
// 示例代码 —— 包字计数器（安全版）
`default_nettype none
module Packet_Word_Counter
#(
    parameter WORD_COUNT_WIDTH = 0,   // 实例化时设定
    parameter LAST_WORD        = 0    // 每包字数 - 1
)
(
    input  wire                        clock,
    input  wire                        clear,
    input  wire                        valid,   // source 给本模块
    input  wire                        ready,   // destination 回本模块
    output wire [WORD_COUNT_WIDTH-1:0] word_count
);
    // 第 1 步: 定义握手完成
    reg handshake_complete = 1'b0;
    always @(*) begin
        handshake_complete = (ready == 1'b1) && (valid == 1'b1);
    end

    // 第 2 步: 用 handshake_complete 门控计数器更新
    reg [WORD_COUNT_WIDTH-1:0] count = {WORD_COUNT_WIDTH{1'b0}};
    always @(posedge clock) begin
        if (clear)
            count <= {WORD_COUNT_WIDTH{1'b0}};
        else if (handshake_complete) begin           // 仅在真正传输的那一拍
            if (count == LAST_WORD[WORD_COUNT_WIDTH-1:0])
                count <= {WORD_COUNT_WIDTH{1'b0}};   // 最后一字确实发出去了, 才归零
            else
                count <= count + 1'b1;
        end
    end

    assign word_count = count;
endmodule
`default_nettype wire
```

2. 对照 `handshake.html` 的模板核对：你的 `handshake_complete` 表达式是否与 [handshake.html:195-197](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L195-L197) 一致？计数器更新是否被 `if (handshake_complete)` 完整包住？

3. 跟踪一条时序：设 `LAST_WORD = 3`，让 `valid` 持续为高，但在 `count == 3` 那一拍故意把 `ready` 拉低一拍，下一拍再拉高。

**需要观察的现象**：`count` 在 `ready` 拉低的那一拍**不归零**（因为没有握手完成）；直到 `ready` 重新拉高、`handshake_complete` 为真的那一拍才归零，第 4 个字（`count==3` 的数据）真正被送出。

**预期结果**：归零永远发生在数据真正被传输的那一拍，绝不抢跑——这正是规则要保证的。仿真验证需自行搭 testbench，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：Skid Buffer 里为什么需要**两个** `handshake_complete`（`insert` 和 `remove`）而不是一个？

**答案**：因为 Skid Buffer 有两个独立的 ready/valid 接口——输入侧和输出侧，它们各自独立握手、各自独立完成。`insert` 表示「输入侧这一笔收到」，`remove` 表示「输出侧这一笔送出」。两者可以同拍发生（`flow`/`pass`）、也可以只发生一个（`load`/`fill`/`flush`/`unload`）、也可以都不发生。只用一个使能无法表达「同拍既收又发」。

**练习 2**：如果把包计数器的归零逻辑写成 `if (count == LAST_WORD) count <= 0;`（**没有**包在 `handshake_complete` 里），destination 在 `count==LAST_WORD` 那拍临时拉低 `ready` 会发生什么？

**答案**：计数器抢跑归零、切到下一个包，但 `LAST_WORD` 那个字其实还没被 destination 收下（`ready` 为 0）。等 destination 重新拉高 `ready`，source 已经送的是新包的字了——上一包最后一字丢失，且后续包全部错位污染。这正是 `handshake.html` 反例描述的故障。

---

### 4.3 会合同步（rendez-vous）

#### 4.3.1 概念说明

前两节都在讲 ready/valid 怎么用对。本节退后一步问：**ready/valid 到底是什么？**

本书的观点是：ready/valid 并不根本，它只是更底层的**同步机制**（synchronization，又名 **rendez-vous**，会合）的一种实现。我们把同步机制抽象成最纯粹的形态：

- 每个模块有一个输入 `OK_IN` 和一个输出 `OK_OUT`。
- 模块之间把 `OK_OUT` 接到对方的 `OK_IN`。
- 当一个模块需要「和对方对齐」时，就拉高并保持自己的 `OK_OUT`，然后等。
- 当双方在某拍同时看到 \((OK\_IN = 1) \land (OK\_OUT = 1)\) 时，就认为「会合成功」，双方的控制逻辑可以同时改变状态。

注意：会合成功时**没有数据流动**，也**没有任何一方控制另一方**——这只是一次纯粹的时间对齐。master/slave 这类术语之所以该被抛弃，正是因为它假定了一种并不根本的方向性约束。

ready/valid 是在这个同步机制之上**加上单向数据流**得到的：source 用 `valid` 当它的「我 OK 了」，destination 用 `ready` 当它的「我 OK 了」，会合成功的那一拍顺带传一笔数据。

#### 4.3.2 核心流程

会合同步的执行过程：

```
模块 A: 需要同步 -> 拉高并保持 OK_OUT_A
模块 B: 需要同步 -> 拉高并保持 OK_OUT_B
                            |
                            v
   某拍: (OK_IN_A && OK_OUT_A) 且 (OK_IN_B && OK_OUT_B) 同时为真
                            |
                            v
            双方控制逻辑同拍改变状态 -> 会合完成
```

把 ready/valid 视为「会合 + 单向数据」的特例：

| 会合模型 | ready/valid 对应 |
|----------|------------------|
| 模块的 `OK_OUT`（我准备好了） | source 的 `valid` / destination 的 `ready` |
| 模块的 `OK_IN`（对方准备好了） | source 的 `ready`（回信号）/ destination 的 `valid` |
| \((OK\_IN \land OK\_OUT)\) 同拍为真 | `handshake_complete` |
| 会合后双方各自改状态 | 内部状态只在 `handshake_complete` 拍改变 |
| 会合信号要防死锁/活锁 | `valid` 必须 raise-and-hold 等约束 |

一个重要推论：会合信号 `OK_OUT`（也就是 `valid`/`ready`）**必须遵守与 ready/valid 完全相同的死锁/活锁避免规则**——即 4.1 节的约束 B、C。把模型抽象一层，规则不变。

#### 4.3.3 源码精读

**会合是 ready/valid 的本质。** `handshake.html` 在「Underlying Synchronization」一节明确把 ready/valid 降格为同步机制的实现：

[handshake.html:210-219](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L210-L219) —— ready/valid 只是更根本的「同步/会合」机制的一种实现；这也是该抛弃 master/slave 术语的深一层理由：它假定了并不根本的设计约束。

**OK_IN / OK_OUT 模型。** 正文用两个并发模块演示会合：每个模块各有一个 `OK_IN` 输入和 `OK_OUT` 输出，互相对接；需要同步时拉高并保持 `OK_OUT`，直到双方同拍看到两者皆高：

[handshake.html:225-233](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L225-L233) —— 会合判据 `(OK_IN == 1'b1) && (OK_OUT == 1'b1) == 1'b1` 同拍成立即同步成功，此时无数据传输、也无主从控制关系。

**会合是 ready/valid 的祖先。** 正文点破二者的关系，并强调 OK 信号同样要遵守死锁/活锁规则：

[handshake.html:240-244](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L240-L244) —— 在会合上加单向数据流就是 ready/valid；也可做双向同时数据传输，但两个 OK 信号必须遵循前面同样的死锁/活锁避免规则。

**与本书其他同步构件的联系。** 正文还提了一句：把两个模块输出接到 `Pipeline_Synchronizer_Lazy`，则连同下游模块一起实现「三方同步 + 数据传输」——这是会合思想在本书的另一种落地（该模块见后续 u11-l2）：

[handshake.html:235-238](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L235-L238) —— 经 Pipeline Synchronizer 实现三方会合与数据传输。

最后，正文指出这套会合机制与异步逻辑里的 2 相/4 相握手神似，只是有了时钟这个绝对时间参考后被简化了：

[handshake.html:246-249](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L246-L249) —— 会合与异步逻辑 2/4 相握手的相似性。

#### 4.3.4 代码实践

**实践目标**：把会合判据 `(OK_IN && OK_OUT)` 与 ready/valid 的 `handshake_complete` 逐项对齐，亲手验证「ready/valid = 会合 + 单向数据」。

**操作步骤**：

1. 在纸上画两个并发模块 A、B，各画一个 `OK_OUT`（输出）和一个 `OK_IN`（输入），用两根线交叉互连（A.OK_OUT → B.OK_IN，B.OK_OUT → A.OK_IN）。
2. 写出每个模块的会合判据（示例代码）：

```verilog
// 示例代码 —— 模块 A 的会合判据
always @(*) begin
    rendezvous_A = (OK_IN_A == 1'b1) && (OK_OUT_A == 1'b1);
end
```

3. 在旁边列出对应关系表（见 4.3.2），逐行填：如果 A 是 source、B 是 destination，那么 A 的 `OK_OUT_A` 改叫什么？会合判据改叫什么？
4. 核对：会合判据的表达式结构 `(OK_IN && OK_OUT)` 与 [handshake.html:196](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L195-L197) 的 `(ready && valid)` 是否同构？

**需要观察的现象**：填完表后你会发现，`handshake_complete` 与 `rendezvous` 是同一个布尔结构的两种命名；区别只在于 ready/valid 模型把会合结果绑到了一笔单向数据上。

**预期结果**：能口述「valid 是 source 的 OK_OUT、ready 是 destination 的 OK_OUT、handshake_complete 就是 rendezvous 成立」这条映射。这是一个阅读/理解型实践，无需仿真。

#### 4.3.5 小练习与答案

**练习 1**：会合成功时「没有数据传输、也没有主从关系」。那 ready/valid 相比纯会合多了什么？

**答案**：多了一笔**单向数据流**——source 在会合成功的那一拍同时驱动 `data`，destination 在同一拍采样 `data`。数据是叠加在会合之上的载荷，会合本身只是时间对齐。

**练习 2**：为什么把模型抽象到 OK_IN/OK_OUT 会合后，4.1 节的死锁/活锁规则依然适用？

**答案**：因为死锁/活锁约束针对的是「互等信号」的行为模式（谁先动、拉起后保不保持），与会合模型还是 ready/valid 模型无关——OK_OUT 与 valid/ready 扮演的是同一种「我准备好了」的角色。所以 [handshake.html:243-244](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L240-L244) 明确要求两个 OK 信号遵循同样的防死锁/防活锁规则。

---

## 5. 综合实践

把本讲三块内容串起来，给 Skid Buffer 的输入侧加一个「累计已收笔数」计数器，要求它**严格遵守**本讲三条规则。

**任务**：

1. 打开 `Pipeline_Skid_Buffer.v`，定位 [Pipeline_Skid_Buffer.v:325-331](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L325-L331) 中的 `insert` 信号——它就是输入侧的 `handshake_complete`。
2. 设计一个新模块 `Skid_Receive_Counter`，端口含 `clock`、`clear`、`input_valid`、`input_ready`、计数值输出。
3. 内部按 4.2 节做法定义 `handshake_complete = (input_valid && input_ready)`，并**仅**用它门控计数器自增。
4. 检查你的设计是否满足：
   - **约束 A**：`input_ready` 是 Skid Buffer 给出的、寄存过的信号，你的计数器没有在 `valid` 与 `ready` 间引入组合路径。
   - **约束 C**：你的计数器自增只在 `handshake_complete` 拍发生，符合「内部状态只在完成拍改变」。
   - **会合视角**：用 4.3 的映射解释 `handshake_complete` 为何就是一次会合。
5. 跟踪一条时序验证：让 Skid Buffer 处于 FULL 状态（`input_ready` 拉低）时持续给 `input_valid`，确认计数器**不**自增；直到 Skid Buffer 让出一格、`input_ready` 重新拉高那一拍才自增。

**预期结果**：计数器严格反映「真正被 Skid Buffer 收下的笔数」，与 Skid Buffer 的 `insert`/`load`/`fill` 语义一致。综合与时序验证需自行在 CAD 工具中完成，**待本地验证**。

## 6. 本讲小结

- **接口内禁组合路径**：source 不得有 `ready→valid`、destination 不得有 `valid→ready` 的组合路径，否则对接成环；Skid Buffer 用寄存器隔离两接口来满足此约束。
- **防死锁靠不对称**：source 不得等 ready 才拉 valid，destination 可以等 valid 才拉 ready——双方都观望才会死锁。
- **防活锁靠保持**：valid 拉高后必须 raise-and-hold，直到握手完成，否则 valid/ready 闪错峰永远完不成握手。
- **内部状态只在完成拍改**：先定义 `handshake_complete = (ready && valid)`，再用它门控所有影响接口的状态转移；Skid Buffer 的 `insert`/`remove` 就是两次 `handshake_complete`，门控了全部状态机与数据通路变换。
- **会合才是本质**：ready/valid 只是「OK_IN/OK_OUT 会合 + 单向数据流」的实现；OK 信号须遵守同样的死锁/活锁规则。
- **AXI 并不根本**：正因如此，本书停在 ready/valid 层，用 Skid Buffer/Fork/Join/Arbiter 自建接口，而非直接用 AXI。

## 7. 下一步学习建议

本讲把 ready/valid 的「规则与本质」讲完，下一讲 u10-l1《Skid Buffer 与 COTTC FSM 方法》会把 `Pipeline_Skid_Buffer.v` 完整拆开——重点是用 EMPTY/BUSY/FULL 三态状态图、load/flow/fill/flush/unload 数据通路变换，以及 COTTC（约束/操作/变换/转移/控制）状态机设计法，系统讲解如何「设计」出一个满足本讲所有规则的模块。建议：

- 重读 `Pipeline_Skid_Buffer.v` 的 [L182-L371](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L182-L371) 控制路径，带着本讲的 `insert`/`remove` 视角去看状态转移。
- 关注 `fsm.html`（FSM 设计方法正文），为 COTTC 方法做预习。
- 后续 u11-l2 会用 `Synchronous_Muller_C_Element` 与 `Pipeline_Synchronizer_Lazy` 把本讲的「会合」落地成具体的多路同步构件，可与本讲 4.3 节对照阅读。
