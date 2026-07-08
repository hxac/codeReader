# 复位同步与标志同步

## 1. 本讲目标

上一讲（u13-l1）建立了 CDC（时钟域穿越）的两块基石：**亚稳态**的成因，以及用它构建的 **`CDC_Bit_Synchronizer` 位同步器**。本讲把位同步器用起来，解决两个最常见、也最容易写错的 CDC 场景。

学完后你应当能够：

1. 说清「**异步置位、同步释放**」的复位同步原理，并能用 `Reset_Synchronizer` 为一个时钟域生成干净的同步复位。
2. 用 `CDC_Flag_Bit` 实现「在一个时钟域置位、在另一个时钟域清除」的跨域标志，并解释它为什么用「两个翻转寄存器的相对差」来表达标志值。
3. 说清「**同步器输入必须直接来自寄存器、中间不能有组合逻辑**」这条铁律的根因（毛刺），以及 `ASYNC_REG` / `PRESERVE` / `IOB` 等约束为什么必须写在源码里、写在寄存器声明上方。

## 2. 前置知识

本讲默认你已经掌握 u13-l1 的内容，下面三句话做最小回顾：

- **亚稳态**：异步采样可能撞上信号跳变，触发器输出停在「非 0 非 1」的中间态；它**不可消除，只能压低概率、阻止传播**。
- **位同步器**：一串至少两级、紧挨摆放、由接收时钟驱动、中间无组合逻辑的寄存器；第一级允许亚稳，后续级给它消解时间，使传播概率指数级下降。
- **两条铁律**：每次跨越只能同步一个位；同步器延迟在 1–3 周期之间漂移。

本讲还会用到两个前面单元的概念：

- **复位的三种来历**（u3-l2）：上电复位（bitstream 写初值，免费）、同步复位 `clear`（时钟沿生效）、异步复位 `areset`（直接接触发器复位端）。本书立场是「能不用异步就不用」。
- **源码内约束**（u4-l2）：`ASYNC_REG`/`PRESERVE`/`IOB` 这类 `(* ... *)` 属性绑定到具体寄存器实例，必须写在源码里，随实例化自动生效。

一个新术语：**recovery/removal（恢复/撤销时间）**。它之于异步复位的「释放」，就像 setup/hold 之于普通数据——如果复位释放得太靠近时钟沿，不同触发器可能在不同周期看到「复位已撤」，导致重启时各路逻辑错位。本讲要解决的就是这件事。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `CDC_Bit_Synchronizer.v` | 位同步器，本讲所有跨域的「搬运工」，也是 u13-l1 的主角，这里作为被复用的基石。 |
| `Reset_Synchronizer.v` | 复位同步器：把一个异步复位变成「异步置位、同步释放」，内部手工展开了一位同步器 + 异步复位寄存器。 |
| `CDC_Flag_Bit.v` | 跨域标志位：在一个时钟域置位、在另一个时钟域清除，内部用两个翻转寄存器 + 两个位同步器构成环。 |

三者关系：`CDC_Flag_Bit` 直接实例化 `CDC_Bit_Synchronizer`；`Reset_Synchronizer` 不实例化它（因为要给寄存器加属性、还要带复位），但**复刻了它的同步链结构**。

## 4. 核心概念与源码讲解

### 4.1 复位同步器

#### 4.1.1 概念说明

复位信号本身也是一个「跨时钟域」的信号：产生复位的地方（外部引脚、上电电路、或另一个时钟域）和被复位的逻辑所用的时钟，通常没有固定相位关系。于是复位和 u13-l1 讲的数据一样，也有亚稳态风险——但风险点很特别：

- **复位「置位」时，不怕亚稳态。** 复位的本意就是把所有寄存器强制到一个已知值，谁先到谁后到无所谓，反正最后都是那个已知值。
- **复位「释放」时，非常怕亚稳态。** 如果释放瞬间正好卡在时钟沿附近（违反 recovery/removal 时间），有的触发器这一拍就退出复位、有的下一拍才退出，重启后逻辑就错位了——而且这种错位仿真里几乎看不到，只在真实芯片上偶发。

所以工业界标准做法叫 **「异步置位、同步释放」（async assert, sync deassert）**：置位时立刻、异步地把所有相关寄存器按下去；释放时，让释放动作经过一条同步链，保证整个时钟域在**同一个时钟沿**整齐地退出复位。这正是 `Reset_Synchronizer` 做的事，见其开头的说明：

[Reset_Synchronizer.v:3-7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L3-L7) —— 把异步复位过滤成「立即异步置位、只能随接收时钟同步释放（延迟 2 或 3 拍）」，避免释放太靠近时钟沿引发亚稳态。

#### 4.1.2 核心流程

`Reset_Synchronizer` 的行为可以拆成两段：

1. **异步置位（assert）**：`reset_in` 一旦进入有效电平，立即（不等时钟）把同步链上的**所有**寄存器打成复位有效值，于是 `reset_out` 立刻有效。因为全体寄存器同时被按住，不存在谁先谁后的问题。
2. **同步释放（deassert）**：`reset_in` 撤销后，撤销信号要顺着一条深度为 `DEPTH` 的寄存器链，一拍一拍地移位，直到最后一拍到达 `reset_out`。这样整个时钟域在同一个时钟沿整齐退出复位。

用伪代码表示这条链的释放过程（`DEPTH=2` 为例）：

```
reset_in 撤销后:
  cycle 0: sync_reg[0] <= inactive  // 第一级可能亚稳，需 1 拍消解
  cycle 1: sync_reg[1] <= sync_reg[0]
  cycle 2: reset_out  =  sync_reg[1]  -> 释放
```

由此得到释放延迟的公式。设同步链深度为 \(D = 2 + \text{EXTRA\_DEPTH}\)，则：

\[ \text{释放延迟} \in \{D,\; D+1\} \text{ 个接收时钟周期} \]

多出来的那一拍，正是留给第一级寄存器消解亚稳态的——这与 u13-l1 讲的「位同步器延迟在 1–3 周期漂移」是同一回事。当 \(D=2\) 时，释放延迟落在 2–3 拍，恰好覆盖那个区间。

#### 4.1.3 源码精读

先看端口与参数。注意 `RESET_ACTIVE_STATE` **默认值是 2，既不是 0 也不是 1**——这是 u1-l2「参数默认值必须为 0/空/非法，让忘设参数的人吵闹失败」哲学的延伸：默认值非法，逼你必须显式声明复位是高有效还是低有效：

[Reset_Synchronizer.v:65-77](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L65-L77) —— 模块端口；`RESET_ACTIVE_STATE` 默认 2，必须由实例化设成 0（低有效）或 1（高有效）。

模块开头先用 `initial` 把 `reset_out` 初始化为「无效态」，避免上电时误触发复位（注意 `RESET_ACTIVE_STATE[0]` 取参数的最低位，再取反得到无效电平）：

[Reset_Synchronizer.v:79-81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L79-L81) —— 上电把 `reset_out` 设为无效态。

接着是核心：一条深度可调的同步链，以及挂在它上方的一组布局/优化约束（约束的含义留到 4.3 讲，这里先注意它们**写在 `reg` 声明正上方**）：

[Reset_Synchronizer.v:89-118](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L89-L118) —— `DEPTH = 2 + EXTRA_DEPTH`；同步链寄存器声明上方挂着 Vivado/Quartus 的同步器约束；`initial` 把整条链初始化为无效态。

真正实现「异步置位、同步释放」的是下面这段 `generate`。它按 `RESET_ACTIVE_STATE` 分成两份几乎相同的 `always` 块，唯一区别是复位信号的触发沿（低有效用 `negedge reset_in`，高有效用 `posedge reset_in`）。作者自己注释说这种代码重复「很丑，但别无他法」——因为敏感列表里的沿必须随有效电平改变：

[Reset_Synchronizer.v:144-178](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L144-L178) —— `if (reset_in 有效)` 分支把整条链**同时**打入有效值（异步置位）；`else` 分支移入无效值（同步释放）。两份代码仅复位沿不同。

读低有效那份（`RESET_ACTIVE_STATE === 0`）就能看懂机制：

```verilog
always @(posedge receiving_clock, negedge reset_in) begin
    if (reset_in == RESET_ACTIVE_STATE [0]) begin      // reset_in==0，即被置位
        for(i = 0; i < DEPTH; i = i+1)
            sync_reg [i] <= RESET_ACTIVE_STATE [0];    // 整条链全部按到 0 -> reset_out 立即有效
    end
    else begin                                          // reset_in==1，已释放
        sync_reg [0] <= ~RESET_ACTIVE_STATE [0];       // 移入无效值 1
        for(i = 1; i < DEPTH; i = i+1)
            sync_reg [i] <= sync_reg [i-1];            // 逐级下传 -> reset_out 同步释放
    end
end
```

要点：复位信号出现在**敏感列表**里（`negedge reset_in`），所以置位是**异步**的——不等时钟；而释放走的是 `else` 里的移位，受 `posedge receiving_clock` 驱动，所以是**同步**的。`reset_out` 直接取链的最后一拍：

[Reset_Synchronizer.v:188-190](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L188-L190) —— `reset_out` 取同步链末端，因此「全体置位时立即有效，逐级释放时同步撤销」。

最后看一个精巧的「安全栅栏」：如果 `RESET_ACTIVE_STATE` 被设成 0/1 以外的值，模块会去实例化一个不存在的模块，强制综合/仿真当场报错。注意它用的是恒等比较 `===` 而非 `==`——这样含 `X` 的参数不会隐式匹配 0 而蒙混过关：

[Reset_Synchronizer.v:132-186](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L132-L186) —— 非法参数走 `else` 分支，实例化 `NonExistentModuleForErrorChecking` 报错；用 `===` 防止 `X` 值隐式匹配。

> 为什么不直接实例化 `CDC_Bit_Synchronizer`？因为它没有复位输入，而复位同步器自己必须能从复位里出来（鸡生蛋问题），所以同步链寄存器需要自己的异步复位路径，且必须把约束直接贴在 `reg` 上，只能手工展开。见 [Reset_Synchronizer.v:18-24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L18-L24)。

#### 4.1.4 代码实践

**实践目标**：为一个时钟域生成同步复位，并在仿真里观察「立即置位、延迟释放」。

**操作步骤**：

1. 新建一个仿真工程，把 `Reset_Synchronizer.v` 加进去。
2. 写一个极小的顶层（示例代码），用 `Reset_Synchronizer` 把外部异步复位转成同步复位，再驱动一个普通寄存器：

```verilog
// 示例代码：为目标时钟域生成同步复位
`default_nettype none
module sync_reset_example
#(
    parameter WORD_WIDTH = 8
)(
    input  wire                    ext_clock,       // 目标时钟域
    input  wire                    async_reset_in,  // 异步复位源（高有效）
    input  wire [WORD_WIDTH-1:0]   data_in,
    output reg  [WORD_WIDTH-1:0]   data_out
);
    wire synced_reset;

    Reset_Synchronizer
    #(
        .EXTRA_DEPTH        (0),
        .RESET_ACTIVE_STATE (1)                      // 1 = 高有效
    )
    reset_sync
    (
        .receiving_clock (ext_clock),
        .reset_in        (async_reset_in),           // 必须直接来自寄存器，中间不得有组合逻辑
        .reset_out       (synced_reset)
    );

    always @(posedge ext_clock) begin
        if (synced_reset == 1'b1)
            data_out <= {WORD_WIDTH{1'b0}};
        else
            data_out <= data_in;
    end
endmodule
```

3. 写一个测试台：先拉高 `async_reset_in`，让 `ext_clock` 自由跑；然后在**两个 `ext_clock` 上升沿之间**的随机时刻撤销 `async_reset_in`（刻意制造「释放靠近时钟沿」）。

**需要观察的现象**：

- `async_reset_in` 一拉高，`synced_reset`（和 `data_out`）几乎立即变为复位态，与 `ext_clock` 无关——这是**异步置位**。
- `async_reset_in` 撤销后，`synced_reset` 并不立刻撤销，而是等 2–3 个 `ext_clock` 周期后才整齐变低——这是**同步释放**。
- 多次随机时刻释放，释放延迟在 2 或 3 拍之间跳动。

**预期结果**：复位释放永远落在某个 `ext_clock` 整数倍时刻，且 `data_out` 的复位撤销与下游逻辑严格同步。仿真数值「待本地验证」（取决于你的释放时刻与仿真器分辨率）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `RESET_ACTIVE_STATE` 留成默认值 2 就综合，会发生什么？
**答案**：`generate` 走到 `else` 分支，实例化不存在的 `NonExistentModuleForErrorChecking`，综合/仿真当场报错。这是有意的「吵闹失败」，防止复位静默失效。

**练习 2**：为什么作者要写两份几乎一样的 `always` 块，而不是用一份 + 三元运算符统一处理？
**答案**：复位的有效电平决定了它在敏感列表里用 `posedge` 还是 `negedge`，这只能通过写两份代码体现。而且复位是全书少数**必须用 `if`、不能用三元**的地方（见 u3-l2），三元在非阻塞赋值下会排入「写回旧值」而盖掉前一句。

**练习 3**：`EXTRA_DEPTH` 什么时候需要大于 0？
**答案**：当器件工作在接近最高频率、或接近温度/电压极限时，两级同步的 MTBF（平均故障间隔）可能不够，需要再加几级给亚稳态更多消解时间。具体看器件数据手册。

---

### 4.2 标志同步（CDC_Flag_Bit）

#### 4.2.1 概念说明

很多 CDC 场景需要这样一个标志：「在 A 时钟域把它**置位**，在 B 时钟域把它**清除**」。比如 A 域算完一件事，拉一个标志通知 B 域；B 域处理完后清掉它，表示「收到了」。

这听起来简单，写起来却满是陷阱：

- 直接用一个跨域的 SR 锁存？置位和清除可能同时发生、或落在彼此的 setup/hold 窗口内，产生竞争。
- 只用一位同步器？置位和清除分别由两个域发起，没法用「一位」同时表达两个方向的动作。

`CDC_Flag_Bit` 的解法非常优雅——**不直接存「0/1」，而是存「两个翻转寄存器的相对差」**：把一个翻转寄存器拆成两半，一半在置位域、一半在清除域。两者**不同**就代表标志为 1，**相同**就代表标志为 0。每个域只翻转自己那一半，互不干涉。

它是从 Weinstein 的 Flancter 思想派生的，但把所有跨域信号都过了同步器，因而更简单、也免去了「置位与清除不能同时」的约束——代价是要求两个时钟域都在跑（见 [CDC_Flag_Bit.v:7-18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L7-L18)）。

#### 4.2.2 核心流程

模块里有 4 个核心元件，构成一个环：

```
        clock_set 域                         clock_reset 域
   ┌───────────────────┐                ┌───────────────────┐
   │  setting_bit      │  set_toggle    │                   │
   │  (Register)       │──────────────▶ │  set_to_reset     │
   │  data_in = ~reset │                │  (CDC_Bit_Sync)   │
   │       _toggle_sync│                │  -> set_toggle_sync│
   └───────────────────┘                └───────────────────┘
            ▲                                     │
   reset_tg │                                     ▼
   gle_sync │                ┌───────────────────┐
            │                │  resetting_bit    │
   ┌───────────────────┐     │  (Register)       │
   │  reset_to_set     │◀────│  data_in = set_   │
   │  (CDC_Bit_Sync)   │reset│       toggle_sync │
   │  -> reset_tg_sync │ _tg │                   │
   └───────────────────┘  le └───────────────────┘
```

两个输出用「不等比较」得到标志值：

```verilog
bit_out_set   = (set_toggle        != reset_toggle_synced);  // 置位域看到的标志
bit_out_reset = (set_toggle_synced != reset_toggle);         // 清除域看到的标志
```

一次「置位」的流程：

1. 在 `clock_set` 域脉冲 `bit_set` 一拍 → `setting_bit` 载入 `~reset_toggle_synced`（取同步过来的清除翻转的**反**）→ 本域翻转立即改变。
2. 此时 `set_toggle` 与 `reset_toggle_synced` 必然不同 → `bit_out_set` **立即**变 1（标志在本域可见）。
3. `set_toggle` 经 `set_to_reset` 同步器，延迟 2–3 拍到达清除域 → `bit_out_reset` 也变 1（标志传到对岸）。

一次「清除」是对称的：在 `clock_reset` 域脉冲 `bit_reset` → `resetting_bit` 载入 `set_toggle_synced`（与对岸**相同**）→ 两者相同 → 标志清 0，先在本域可见，再传到置位域。

关键点：每个域只动自己的翻转寄存器，比较的却是**同步过来**的对岸翻转，所以永远不会出现 setup/hold 竞争。标志的「0/1」不是某个寄存器的值，而是**两个寄存器值的相对关系**。

#### 4.2.3 源码精读

端口分成对称的两组，每组一个时钟、一个清除、一个脉冲输入、一个标志输出：

[CDC_Flag_Bit.v:40-54](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L40-L54) —— `clock_set`/`clear_set`/`bit_set`/`bit_out_set` 与 `clock_reset`/`clear_reset`/`bit_reset`/`bit_out_reset` 完全对称。

置位域的翻转寄存器 `setting_bit`，复用 u6-l1 的 `Register`：`clock_enable` 接 `bit_set` 脉冲（脉冲来才翻转），`data_in` 取对岸同步翻转的「反」。注意它的输入来自 `Register`/同步器输出，**不是组合逻辑**——这正满足 4.3 要讲的「同步器输入必须来自寄存器」：

[CDC_Flag_Bit.v:72-84](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L72-L84) —— `setting_bit`：脉冲 `bit_set` 时载入 `~reset_toggle_synced`，使本域翻转与对岸「不同」→ 置位。

随后用 `CDC_Bit_Synchronizer` 把 `set_toggle` 同步到清除域：

[CDC_Flag_Bit.v:90-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L90-L99) —— `set_to_reset` 同步器，把置位域翻转搬到清除域。

清除域的 `resetting_bit` 镜像地存在，载入对岸同步翻转的「同」值；再经 `reset_to_set` 同步器搬回置位域，闭合整环：

[CDC_Flag_Bit.v:108-133](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L108-L133) —— `resetting_bit` 与 `reset_to_set` 同步器，构成环的另一半。

最后两个输出就是两个「不等比较」：

[CDC_Flag_Bit.v:139-142](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L139-L142) —— 两域各自比较「本域翻转」与「对岸同步翻转」，不同即为 1。

> 关于全局清除：`clear_set` 与 `clear_reset` 必须**同时**拉高至少 \(4 + \text{EXTRA\_CDC\_STAGES}\) 个周期，否则某个输出可能意外置位而非清除。这是因为清除要穿过同步链传播，需要足够拍数让整环一致。见 [CDC_Flag_Bit.v:30-36](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L30-L36)。

#### 4.2.4 代码实践

**实践目标**：在仿真里观察「一域置位、对岸延迟可见；对岸清除、本域延迟清零」。

**操作步骤**：

1. 把 `CDC_Flag_Bit.v`、`CDC_Bit_Synchronizer.v`、`Register.v` 都加入工程（注意依赖链）。
2. 实例化（示例代码）：

```verilog
// 示例代码：跨域标志——A 域置位、B 域清除
CDC_Flag_Bit
#(
    .EXTRA_CDC_STAGES (0)
)
u_flag
(
    .clock_set     (clk_a),
    .clear_set     (1'b0),
    .bit_set       (set_pulse_a),     // 在 A 域拉高一拍
    .bit_out_set   (flag_in_a),       // A 域看到的标志

    .clock_reset   (clk_b),
    .clear_reset   (1'b0),
    .bit_reset     (reset_pulse_b),   // 在 B 域拉高一拍
    .bit_out_reset (flag_in_b)        // B 域看到的标志
);
```

3. 提供两个不同周期/相位的时钟 `clk_a`、`clk_b`；先在 `clk_a` 域给 `set_pulse_a` 一个单拍脉冲，等若干拍后再在 `clk_b` 域给 `reset_pulse_b` 一个单拍脉冲。

**需要观察的现象**：

- `set_pulse_a` 一出现，`flag_in_a` **当拍**就变 1（本域立即可见）。
- 之后约 2–3 个 `clk_b` 周期，`flag_in_b` 才变 1（标志穿过同步链到达对岸）。
- `reset_pulse_b` 一出现，`flag_in_b` **当拍**变 0；再过 2–3 个 `clk_a` 周期，`flag_in_a` 变 0。
- 重复置位/清除，两侧输出最终总是一致（都 0 或都 1），只是有 CDC 延迟差。

**预期结果**：标志永远先在被操作的那个域可见，再以 1–3 拍的不确定延迟传到对岸，但绝不会出现「一侧 1 一侧 0 永久卡死」。具体延迟拍数「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bit_out_set` 用「不等比较」而不是直接输出某个寄存器？
**答案**：标志值是两个翻转寄存器的**相对差**，不是任何单个寄存器的绝对值。两翻转不同即标志为 1，相同即 0；这样每个域只翻自己的寄存器，比较靠同步过来的对岸值，天然避开竞争。

**练习 2**：`setting_bit` 的 `data_in` 为什么是 `~reset_toggle_synced`（取反），而 `resetting_bit` 的 `data_in` 是 `set_toggle_synced`（不取反）？
**答案**：置位要让两翻转「不同」→ 取对岸的反；清除要让两翻转「相同」→ 取对岸的同。一次置位、一次清除合起来让某个翻转被翻了偶数次，相对关系回到初值，标志归零。

**练习 3**：为什么 `CDC_Flag_Bit` 要求两个时钟域都一直在跑？
**答案**：它靠同步器搬运翻转值，若某个域的时钟停了，同步链就不传播，另一域的标志可能卡住。需要「某域时钟可能停」的场合应改用带异步复位的方案（见模块开头说明引用的 `Register_areset`）。

---

### 4.3 毛刺与布局约束

#### 4.3.1 概念说明

这是本讲最容易被忽视、却最致命的一块。它回答两个问题：

**问题一：为什么同步器的输入必须直接来自寄存器，中间不能有任何组合逻辑？**

组合逻辑有一个特性：当多路输入同时变化、且各条路径延迟不同时，输出会在稳定到最终值之前，出现短暂的错误跳变——这就是**毛刺（glitch）**。在普通同步设计里，毛刺无所谓：下游寄存器只在时钟沿采样，而毛刺早在下一个时钟沿到来之前就消失了，被自然滤掉。

但同步器不一样：它的接收时钟和发送域**没有任何相位关系**，是个「不相关」的时钟。这个异步时钟完全可能恰好在毛刺发生的瞬间采样，于是：

- 一个本该保持稳定的 1，可能因毛刺被采成一次假的 0→1→0，在接收域变成一个**凭空出现的错误脉冲**；
- 或者一个本该被检测到的跳变，因毛刺提前回落而被**漏采**。

这种错误在仿真里几乎永远复现不出来（仿真里组合逻辑是零延迟的），只在真实芯片上偶发——是最阴险的一类 bug。

**问题二：那些 `(* ... *)` 约束到底是什么，为什么必须写在源码里？**

u13-l1 已经提过 `ASYNC_REG`/`PRESERVE`/`IOB`，这里把它们讲全。同步器的有效工作依赖寄存器的**物理布局**——同步链各级必须紧挨在一起，第一级要能合法地「吃下」亚稳态并给后续级消解时间。如果综合器把这些寄存器优化掉、塞进 DSP/BRAM 的输入寄存器、或摆到远离逻辑阵列的 I/O 寄存器位置，MTBF（平均故障间隔）会急剧崩塌。所以要在源码里用属性明确告诉工具「这些寄存器是同步器，动不得、要挨着摆」。

#### 4.3.2 核心流程

把约束按厂商和用途归类：

| 约束 | 厂商 | 作用 |
|------|------|------|
| `ASYNC_REG = "TRUE"` | Vivado | 标记这些寄存器组成同步器：紧挨布局、纳入 MTBF 报告、允许第一级作亚稳态接收 |
| `IOB = "false"` | Vivado | 禁止摆进 I/O 寄存器位置（离逻辑阵列太远） |
| `PRESERVE` | Quartus | 禁止优化/合并这些寄存器（如塞进 DSP/BRAM 输入寄存器） |
| `useioff = 0` | Quartus | 禁止使用 I/O 寄存器 |
| `altera_attribute ... SYNCHRONIZER_IDENTIFICATION "FORCED IF ASYNCHRONOUS"` | Quartus | 强制识别为同步器，紧挨布局 |

「写在源码里」的工作机制：`(* ... *)` 属性前置在 `reg` 声明之前，就修饰**该声明**；它随实例化自动生效，不依赖外部约束文件。这是 u4-l2「源码内约束」原则的具体落地——因为同步器对布局极度敏感，外部 XDC/QSF 漏写一行就会静默降低 MTBF，属于「设计吵闹、失效静默」的高危项，必须把约束绑在寄存器本身上。

对于从 FPGA 外部引脚进来的信号，还有一条额外规则：信号要先经过一个**专用 I/O 寄存器**（同步于 I/O 时钟），再喂给内部时钟驱动的同步器。这个 I/O 寄存器既承担「合法的引脚寄存」，又顺手滤掉了外部输入的毛刺。

#### 4.3.3 源码精读

先看 `CDC_Bit_Synchronizer` 里关于毛刺的警告——这就是「输入必须直接来自寄存器」铁律的原始出处：

[CDC_Bit_Synchronizer.v:62-75](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Bit_Synchronizer.v#L62-L75) —— 必须直接从寄存器喂同步器；否则汇聚的组合逻辑会毛刺，而异步的接收时钟可能恰好采到毛刺，把它变成接收域里一个真实但错误的脉冲。

以及「不能当 I/O 寄存器用」的说明——外部输入要先过专用 I/O 寄存器，它顺便滤掉输入毛刺：

[CDC_Bit_Synchronizer.v:77-85](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Bit_Synchronizer.v#L77-L85) —— 同步器级不能放 I/O 寄存器；外部信号应先进一个同步于 I/O 时钟的专用 I/O 寄存器，再接内部时钟驱动的同步器。

约束本身在两个模块里几乎一字不差，都写在 `reg sync_reg` 声明正上方。`CDC_Bit_Synchronizer` 这一份：

[CDC_Bit_Synchronizer.v:115-124](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Bit_Synchronizer.v#L115-L124) —— Vivado（`IOB`+`ASYNC_REG`）与 Quartus（`useioff`+`PRESERVE`+`altera_attribute`）两组约束前置在 `reg sync_reg [DEPTH-1:0]` 声明之前。

`Reset_Synchronizer` 里同样的约束块（额外说明了对两家工具各自的理由）：

[Reset_Synchronizer.v:91-110](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L91-L110) —— 注释解释：Vivado 要紧挨布局并进 MTBF 报告；Quartus 要防优化并标为同步器；两家都禁止摆进 I/O 寄存器。

`Reset_Synchronizer` 还把「输入必须来自寄存器」单独写进了使用须知——因为毛刺会**触发虚假复位**，后果比普通数据毛刺更严重：

[Reset_Synchronizer.v:27-33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Reset_Synchronizer.v#L27-L33) —— `reset_in` 必须直接来自寄存器，组合逻辑毛刺会触发虚假复位；同步器寄存器也不能放进 I/O 寄存器。

最后，`CDC_Flag_Bit` 是「正确喂法」的活教材：它的两个同步器输入分别来自 `Register` 的 `data_out`（`set_toggle`、`reset_toggle`），都是寄存器输出，没有组合逻辑夹在中间，天然满足铁律。回看 [CDC_Flag_Bit.v:90-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L90-L99) 与 [CDC_Flag_Bit.v:124-133](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Flag_Bit.v#L124-L133)，`bit_in` 接的都是 `set_toggle`/`reset_toggle` 这类寄存器输出。

#### 4.3.4 代码实践

这是一个「**源码阅读型 + 反面教材**」实践，因为没有可直接跑的脚本，且这类 bug 本就无法用仿真暴露。

**实践目标**：识别「同步器输入夹了组合逻辑」这种隐性 bug，并理解为什么仿真抓不到。

**操作步骤**：

1. 阅读下面这段「**反面示例代码**」（故意写错），找出问题：

```verilog
// 反面示例代码：同步器输入夹了组合逻辑——危险！
wire gated_valid = (valid_a & ready_a) | (some_other_condition);  // 汇聚的组合逻辑

CDC_Bit_Synchronizer sync_bad (
    .receiving_clock (clk_b),
    .bit_in          (gated_valid),   // ❌ 输入来自组合逻辑，会毛刺
    .bit_out         (valid_b)
);
```

2. 把它改成正确写法：先用一个 `Register` 把 `gated_valid` 打一拍，再用这个寄存器输出喂同步器（正面示例代码）：

```verilog
// 正面示例代码：先寄存，再同步
wire gated_valid = (valid_a & ready_a) | (some_other_condition);

reg  valid_a_regd;
always @(posedge clk_a) valid_a_regd <= gated_valid;   // 先打一拍，滤掉毛刺

CDC_Bit_Synchronizer sync_good (
    .receiving_clock (clk_b),
    .bit_in          (valid_a_regd),  // ✅ 输入直接来自寄存器
    .bit_out         (valid_b)
);
```

3.（可选）用 `verilator --lint_only` 或你所用工具的 lint，确认两种写法都能通过 lint——从而体会「lint 和仿真都过，不代表 CDC 安全」。

**需要观察的现象 / 思考**：

- 反面写法里，`gated_valid` 是两路信号的或，两路延迟不同就会在输出产生毛刺。
- 仿真里毛刺不出现（零延迟），所以两种写法仿真波形看起来都「正常」——这正是危险之处。
- 想象 `clk_b` 的上升沿恰好落在毛刺窗口，`valid_b` 就会多出一个假脉冲。

**预期结果**：你应当得出结论——**同步器的 `bit_in`/`reset_in` 必须在源码层面就保证来自寄存器输出**，这是代码评审要查的项，不是仿真能保证的项。具体仿真「待本地验证」，但结论与是否运行无关。

#### 4.3.5 小练习与答案

**练习 1**：为什么普通同步设计不怕组合逻辑毛刺，而同步器怕？
**答案**：普通设计的下游寄存器只在自己的时钟沿采样，毛刺在下一个沿到来前早已消失，被自然滤掉。同步器的接收时钟与发送域无相位关系，可能恰好在毛刺窗口采样，把毛刺变成真实的错误脉冲，或漏采真实跳变。

**练习 2**：`ASYNC_REG` 和 `IOB="false"` 各解决什么问题？
**答案**：`ASYNC_REG` 告诉 Vivado 这些寄存器是同步器，要紧挨布局、纳入 MTBF 报告、允许第一级接收亚稳态；`IOB="false"` 禁止把它们摆进 I/O 寄存器位置，因为那里离逻辑阵列太远，会破坏同步链各级紧挨的要求。

**练习 3**：为什么这些约束写在源码里，而不是统一放外部约束文件？
**答案**：因为 `(* ... *)` 前置于 `reg` 声明就绑定该实例、随实例化自动生效；同步器对布局极敏感，外部约束文件漏写一行就会静默降低 MTBF。写进源码是「吵闹设计、杜绝静默失效」。

## 5. 综合实践

把本讲三块串起来，搭一个最小的「**跨域握手复位**」小系统：

- 有两个时钟域 `clk_a`（快）和 `clk_b`（慢），以及一个外部异步复位 `ext_reset`（高有效）。
- 用 `Reset_Synchronizer` 为 `clk_b` 域生成同步复位 `rst_b`。
- 在 `clk_a` 域用一个受 `rst_a`（可由另一个 `Reset_Synchronizer` 生成，或直接用 `ext_reset`）控制的计数器，每数到某个值产生一个单拍 `done_a` 脉冲。
- 用 `CDC_Flag_Bit` 把 `done_a` 作为「事件标志」传到 `clk_b` 域：`bit_set` 接 `done_a`，`bit_out_reset` 在 `clk_b` 域可见；`clk_b` 域处理完后用 `bit_reset` 清掉。

要求你在设计里自查：

1. 两个 `Reset_Synchronizer` 的 `reset_in` 是否都直接来自寄存器/引脚寄存器？（对照 4.3）
2. `CDC_Flag_Bit` 的两个同步器输入是否都来自 `Register` 输出？（它内部已保证，但你要能指出是哪两个信号）
3. `clk_b` 域受 `rst_b` 控制的逻辑，是否都在同一个 `clk_b` 沿整齐退出复位？（对照 4.1）

这个练习把「同步复位生成 → 跨域事件标志 → 喂同步器的纪律」三件事拧成一根绳，是后续 u14（字同步、脉冲同步、CDC FIFO）的标准前置模式。

## 6. 本讲小结

- **复位同步器**实现「异步置位、同步释放」：置位时整条同步链同时按下去（异步、立即），释放时撤销信号逐级移位（同步、整齐），把 recovery/removal 风险隔离在第一级；释放延迟为 \(D\) 或 \(D+1\) 拍。
- `Reset_Synchronizer` 因要给寄存器挂约束、还要自带复位路径，无法直接实例化 `CDC_Bit_Synchronizer`，只能手工展开同步链；`RESET_ACTIVE_STATE` 默认 2 是有意的「吵闹失败」。
- **`CDC_Flag_Bit`** 用「两个翻转寄存器的相对差」表达跨域标志：置位让两翻转「不同」、清除让两翻转「相同」，每域只翻自己的寄存器、比较同步过来的对岸值，天然避开 setup/hold 竞争。
- **毛刺铁律**：同步器输入必须直接来自寄存器、中间不得有组合逻辑——异步接收时钟可能恰好采到组合毛刺，制造仿真不可见的假脉冲或漏采。
- **源码内约束**（`ASYNC_REG`/`IOB`/`PRESERVE`/`useioff`/`altera_attribute`）绑在 `reg` 声明上，保证同步链紧挨布局、不被优化、不进 I/O 寄存器，守住 MTBF。
- 三个模块共享同一哲学：CDC 的正确性靠**结构纪律**（直接来自寄存器、约束随声明走、每次只同步一位），而非靠仿真保证。

## 7. 下一步学习建议

本讲把「一位信号如何干净地跨域」讲到了复位与标志两种实用形态。接下来 u14 会向两个方向扩展：

- **多位数据跨域**：`CDC_Word_Synchronizer` 用「只同步一个 valid、多位数据直接采样」的策略，把本讲的单位规则推广到总线——它正是 `CDC_Flag_Bit` 思想的近亲。
- **脉冲跨域与 FIFO**：`CDC_Pulse_Synchronizer`（2 相/4 相 toggling）处理「脉冲」而非电平；`Weinstein_Flancter` 与 `CDC_FIFO_Buffer` 则把本讲的翻转/同步思想用到跨域事件计数与 Gray 码指针 FIFO 上。

建议在进入 u14 前，先回头确认你能默写出 `CDC_Bit_Synchronizer` 的两级结构、`ASYNC_REG` 的作用，以及「输入必须来自寄存器」这条铁律——它们会在整个 CDC 单元里反复出现。
