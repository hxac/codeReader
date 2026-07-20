# 记录状态机：Idle→PreTrig→WaitTrig→PostTrig→Done

## 1. 本讲目标

本讲聚焦 `data_rec` 核心记录器的「指挥中枢」——五状态有限状态机（FSM）。读完本讲你应该能够：

- 说出五个状态各自的职责，并画出完整的状态迁移图。
- 精确指出每个迁移条件对应的源码行（`p_comb` 中 `case r.State_2` 的各分支）。
- 解释 `Done` 之后为什么软件「读一次状态寄存器」就能让记录器自动回到 `Idle`（Ack 自动确认机制）。
- 掌握内部枚举状态 `State_t` 与对外 4 位 `State` 端口、寄存器地图状态码三者之间的映射关系。

本讲只讲状态机本身，不展开流水线细节（见 u3-l3）、地址/采样计数器（见 u3-l4）与触发源裁决（见 u4）。

## 2. 前置知识

### 2.1 什么是有限状态机（FSM）

硬件里的状态机用一组**离散状态**和**迁移条件**描述控制流程：每个时钟周期，电路处于某一个状态，根据当前状态和输入信号决定下一周期进入哪个状态。本记录器是一个典型的 **Moore 型** 状态机——状态本身（而非输入）决定了对外输出的主要行为（是否写存储、是否拉高 `Done` 等）。

VHDL 中通常用一个**枚举类型**列出所有状态名，综合工具会自动为每个状态分配一个二进制编码。本记录器的状态机写在 `p_comb`（组合进程）里，状态本身寄存在信号 `r.State_2` 上，由 `p_seq`（时序进程）在每个上升沿更新。

### 2.2 为什么要分五个状态

一次完整录制可以自然切成五段：

| 阶段 | 在做什么 | 是否往存储写样本 |
|------|----------|------------------|
| Idle | 空闲，等待软件下达 Arm 命令 | 否 |
| PreTrig | 抓**前触发**样本（触发发生前的波形） | 是 |
| WaitTrig | 前触发已满，等待触发事件到来 | 是（继续滚动） |
| PostTrig | 触发已发生，抓**后触发**样本 | 是 |
| Done | 录制完成，等软件读走数据并确认 | 否 |

这种「先蓄一段前触发、再等触发、再补一段后触发」的设计，正是示波器抓波形的经典做法（见 u1-l1 对项目定位的说明）。

### 2.3 两进程法与 `_2` 后缀

本讲的状态机写在**两进程法**的组合进程 `p_comb` 中。你会看到大量带数字后缀的信号，比如 `State_2`、`Trigger_2`、`In_Vld(1)`。后缀 `_2` 表示这是**流水线第 2 级（Stage 2）**寄存的版本——状态机的所有判定都发生在 Stage 2，因此参与判定的输入也要对齐到 Stage 2（例如 `r.In_Vld(1)` 是输入有效信号延迟一拍后的 Stage 2 副本）。完整流水线拆解见 u3-l3，本讲你只需记住：**状态机看到的所有信号都是 Stage 2 对齐的**。

## 3. 本讲源码地图

本讲主要涉及 3 个源码文件：

| 文件 | 在本讲的作用 |
|------|--------------|
| [`hdl/data_rec.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 状态机的全部逻辑：状态类型定义、`case r.State_2` 迁移、`State` 端口输出、复位初值 |
| [`hdl/data_rec_register_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 状态码常量 `Reg_Stat_State*_c`（对外暴露给软件的数字编码） |
| [`hdl/data_rec_vivado_wrp.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | 封装层：Ack 自动确认逻辑、`Ack` 端口的由来 |
| [`testbench/top_tb/top_tb_case0_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd) | 验证全状态序列的回归测试（实践依据） |

## 4. 核心概念与源码讲解

### 4.1 State_t 类型与状态枚举

#### 4.1.1 概念说明

记录器需要一个变量来「记住自己现在处于五段录制流程的哪一段」。VHDL 最自然的做法是定义一个枚举类型 `State_t`，把五个状态名列出来。综合时，工具会按**枚举位置值（position）**给每个名字分配编码：第一个列出的状态位置为 0，第二个为 1，以此类推。这一点很关键——它直接决定了对外 4 位 `State` 端口的数值。

#### 4.1.2 核心流程

状态类型的定义只有一行，但它是整个状态机的「目录」：

```
Idle_s    位置 0   空闲
PreTrig_s 位置 1   前触发积累
WaitTrig_s位置 2   等待触发
PostTrig_s位置 3   后触发积累
Done_s    位置 4   录制完成
```

这个位置序号会被显式映射到寄存器包里定义的状态码常量上，从而让软件读到的 `State` 数值与源码里的状态名一一对应。复位时状态机被初始化为 `Idle_s`，保证上电后记录器处于安全的空闲态。

#### 4.1.3 源码精读

枚举类型定义在架构的声明区：

- 枚举类型 [`hdl/data_rec.vhd:92`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L92)：`type State_t is (Idle_s, PreTrig_s, WaitTrig_s, PostTrig_s, Done_s);` —— 这一行就是五个状态的「目录」，顺序即编码。
- 状态寄存器 [`hdl/data_rec.vhd:104`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L104)：`State_2 : State_t;` 是 `data_rec_r` 记录的一个字段，承载当前状态（Stage 2 对齐）。
- 复位初值 [`hdl/data_rec.vhd:373`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L373)：`r.State_2 <= Idle_s;` 在 `p_seq` 的复位分支里，确保复位后从空闲态起步。

对外暴露的状态码常量定义在寄存器包里，数值与枚举位置完全一致：

- 状态码常量 [`hdl/data_rec_register_pkg.vhd:23-27`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L23-L27)：`StateIdle_c=0`、`StatePreTrig_c=1`、`StateWaitTrig_c=2`、`StatePostTrig_c=3`、`StateDone_c=4`。

软件通过 AXI 读 `Reg_Stat_Addr_c`（地址 `0x0000`，见 u2-l2）就能拿到这 4 位状态码，从而知道记录器当前处于哪一段。

#### 4.1.4 代码实践

**实践目标**：确认枚举位置值、寄存器包常量、对外 `State` 端口三者数值一致。

**操作步骤（源码阅读型）**：

1. 打开 [`hdl/data_rec.vhd:92`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L92)，数一下 `Done_s` 是第几个列出的状态（从 0 起算）。
2. 打开 [`hdl/data_rec_register_pkg.vhd:23-27`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L23-L27)，核对 `Reg_Stat_StateDone_c` 的值。
3. 找到 4.2.3 节引用的 `State` 端口输出映射（`case r.State_2` → `to_unsigned(Reg_Stat_StateDone_c, ...)`），确认 `Done_s` 分支输出的数值。

**需要观察的现象**：三处数值应当都是 4。

**预期结果**：`Done_s` 是第 5 个（位置 4），`Reg_Stat_StateDone_c = 4`，对外 `State` 端口在 Done 态输出 `0x4`。如果三处对不上，软件读到的状态码就会与实际状态错位——这是维护时容易踩的坑。

#### 4.1.5 小练习与答案

**练习 1**：如果把枚举里 `Done_s` 和 `PostTrig_s` 的列出顺序对调（即写成 `(..., Done_s, PostTrig_s)`），但不改寄存器包常量，会出现什么问题？

> **答案**：对调后枚举位置变了——`Done_s` 变成位置 3、`PostTrig_s` 变成位置 4。但寄存器包里 `Reg_Stat_StateDone_c` 仍是 4、`Reg_Stat_StatePostTrig_c` 仍是 3。于是软件读到状态码 4 时以为「完成」，实际硬件却在「后触发」阶段，状态判断完全错乱。结论：枚举顺序、寄存器包常量、`State` 输出映射三处必须同步维护。

**练习 2**：复位后状态机处于哪个状态？为什么这个选择是安全的？

> **答案**：`Idle_s`。因为 Idle 态不写存储（`MemWr_3` 被强制为 0）、不拉 `Done` 中断、也不会在没有软件命令的情况下贸然开始录制，给软件一个确定的、无副作用的起点。

### 4.2 p_comb 中的 case r.State_2 状态迁移

#### 4.2.1 概念说明

状态类型只定义了「有哪些状态」，真正的控制逻辑写在 `p_comb` 里的 `case r.State_2` 分支中。这个 `case` 在每个组合求值周期都根据「当前状态 + 当前输入」计算出**下一状态**（写入变量 `v.State_2`），再由 `p_seq` 在时钟上升沿把 `v` 提交到寄存器 `r`。

除了状态本身，这个 `case` 还顺带产生两个**单拍脉冲**输出：

- `Trigger_2`：在触发生效那一拍拉高一周期（标记「触发刚刚发生」）。
- `Done(2)`：在进入 Done 那一拍拉高，随后被流水搬运到 `Done(3)` 对外输出。

这两个脉冲在 `case` **之前**被先置 0，仅在对应迁移分支里置 1，保证它们是干净的单周期脉冲。

#### 4.2.2 核心流程

完整迁移图如下（箭头上是精确的迁移条件）：

```
                      Arm='1'
        ┌──────────────────────────────────┐
        ▼                                  │
     ┌───────┐  Arm='1'   ┌─────────┐      │
  ──▶│ Idle  │───────────▶│ PreTrig │      │
     │  (0)  │            │   (1)   │      │
     └───────┘            └────┬────┘      │
        ▲                     │            │
        │                     │ PreTrigSpls=0  (跳过前触发)
        │                     │ 或 (AdrCnt_2=PreTrigSpls-1 且 In_Vld(1)='1')
        │                     ▼            │
        │                 ┌─────────┐      │
        │                 │WaitTrig │      │
        │                 │   (2)   │      │
        │                 └────┬────┘      │
        │        Ack='1'       │ TrigNow_2='1'  (同时 Trigger_2:='1')
        │     ┌────────────────┘            │
        │     │                             │
        │     │                             ▼
        │     │                          ┌─────────┐
        │     │                          │PostTrig │
        │     │                          │   (3)   │
        │     │                          └────┬────┘
        │     │                               │ SplCnt_2 >= TotalSpls
        │     │                               │ (同时 Done(2):='1')
        │     │                               ▼
        │     │                            ┌─────────┐
        │     └────────────────────────────│  Done   │
        │                                  │   (4)   │
        │                                  └────┬────┘
        │                                       │ Arm='1' (立即重 Arm)
        └──────────────────────Ack='1'──────────┤  → 直达 PreTrig（重新录制）
                                                ▼
                                       （Arm 分支回到 PreTrig）
```

要点归纳：

1. **Idle → PreTrig**：软件写 Arm 位。
2. **PreTrig → WaitTrig**：前触发样本数已满；若 `PreTrigSpls=0` 则直接跳过（不需要前触发）。
3. **WaitTrig → PostTrig**：触发条件 `TrigNow_2` 为真（三类触发源经裁决后的结果，详见 u4-l1）。
4. **PostTrig → Done**：总样本计数 `SplCnt_2` 达到 `TotalSpls`。
5. **Done 的两条出路**：`Arm='1'` 直接回到 PreTrig（不等读数据就立即开下一轮）；`Ack='1'` 回到 Idle（确认读数据完毕）。注意 Done 态里 `Arm` 优先于 `Ack`（`if Arm ... elsif Ack`）。

#### 4.2.3 源码精读

迁移逻辑全部集中在 `p_comb` 的一段，先是两个脉冲的默认清零，再是 `case`：

- 默认清零 [`hdl/data_rec.vhd:244-245`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L244-L245)：`v.Trigger_2 := '0'; v.Done(2) := '0';` —— 先把两个脉冲清零，下面只有命中迁移的分支才会重新置 1。

```vhdl
case r.State_2 is
    when Idle_s =>
        if (Arm = '1') then
            v.State_2 := PreTrig_s;
        end if;
    when PreTrig_s =>
        if unsigned(PreTrigSpls) = 0 then
            v.State_2 := WaitTrig_s;                 -- 无需前触发，跳过
        elsif (r.AdrCnt_2 = unsigned(PreTrigSpls)-1) and (r.In_Vld(1) = '1') then
            v.State_2 := WaitTrig_s;                 -- 前触发样本写满
        end if;
    when WaitTrig_s =>
        if TrigNow_2 = '1' then
            v.State_2  := PostTrig_s;
            v.Trigger_2 := '1';                      -- 标记触发脉冲
        end if;
    when PostTrig_s =>
        if r.SplCnt_2 >= unsigned(TotalSpls) then
            v.State_2  := Done_s;
            v.Done(2)  := '1';                       -- 标记完成脉冲
        end if;
    when Done_s =>
        if Arm = '1' then
            v.State_2 := PreTrig_s;                  -- 立即重 Arm，开下一轮
        elsif Ack = '1' then
            v.State_2 := Idle_s;                     -- 确认读出，回空闲
        end if;
    when others => null;
end case;
```

完整源码见 [`hdl/data_rec.vhd:246-276`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L246-L276)。

注意几个关键细节：

- `PreTrig` 的迁移条件用了 `r.In_Vld(1)='1'`，意为「只有在真正采到一个有效样本的周期里才检查地址是否到顶」。`In_Vld` 为 0 的周期不计样本、也不推进状态。这是 u3-l1 讲过的「In_Vld 同时门控数据写入与触发判定」在状态机层面的体现。
- `Done(2)` 与 `Trigger_2` 是「事件脉冲」而非「状态电平」。它们随后被流水搬运到第 3 级：`Done` 经 [`hdl/data_rec.vhd:158`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L158) 的 `v.Done(3) := r.Done(2)` 搬运，最终由 [`hdl/data_rec.vhd:354`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L354) `Done <= r.Done(3)` 对外输出；`Trigger_2` 则直接由 [`hdl/data_rec.vhd:393`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L393) `Trig_Out <= r.Trigger_2` 转发到 `Trig_Out` 端口（v2.4 新增，见 u1-l1）。

对外 4 位 `State` 端口的输出也在 `p_comb` 末尾，用另一个 `case` 把枚举映射为状态码：

```vhdl
case r.State_2 is
    when Idle_s     => State <= std_logic_vector(to_unsigned(Reg_Stat_StateIdle_c,    State'length));
    when PreTrig_s  => State <= std_logic_vector(to_unsigned(Reg_Stat_StatePreTrig_c, State'length));
    when WaitTrig_s => State <= std_logic_vector(to_unsigned(Reg_Stat_StateWaitTrig_c,State'length));
    when PostTrig_s => State <= std_logic_vector(to_unsigned(Reg_Stat_StatePostTrig_c,State'length));
    when Done_s     => State <= std_logic_vector(to_unsigned(Reg_Stat_StateDone_c,    State'length));
    when others     => State <= (others => '0');
end case;
```

见 [`hdl/data_rec.vhd:340-347`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L340-L347)。这里显式用 `Reg_Stat_State*_c` 常量（而非依赖枚举位置）把内部状态翻译成软件可见的数字编码，可读性更好，也避免了「改枚举顺序就静默出错」的风险。

#### 4.2.4 代码实践

**实践目标**：用回归测试 `case0` 验证五个状态的完整流转顺序，并把每个断言对应到状态机源码行。

**操作步骤（源码阅读型，有 Modelsim 环境者可实跑）**：

1. 打开 [`testbench/top_tb/top_tb_case0_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd)。
2. 按下表把每个 `axi_single_expect(Reg_Stat_Addr_c, ...)` 断言对应到状态机源码行：

   | 测试行 | 期望状态码 | 对应状态 | 触发该迁移的源码行 |
   |--------|-----------|----------|--------------------|
   | L62 | `StateIdle_c` | Idle（复位后） | 复位 `r.State_2 <= Idle_s`（L373） |
   | L84 | `StatePreTrig_c` | PreTrig | Idle→PreTrig 分支（L247-L250），Arm 由 L82 写入 |
   | L91 | `StateWaitTrig_c` | WaitTrig | PreTrig→WaitTrig 分支（L251-L258），前触发由 L89 灌满 |
   | L115 | `StatePostTrig_c` | PostTrig | WaitTrig→PostTrig 分支（L259-L263），外部触发由 L113 触发 |
   | L124 | `StateDone_c` | Done | PostTrig→Done 分支（L264-L268），后触发由 L120 灌满 |
   | L133 | `StateIdle_c` | Idle（自动回退） | Done→Idle 分支（L272-L273），Ack 由 L124 的读状态自动产生（见 4.3） |

3. （可选）实跑仿真：按 u1-l3 的方法在 `sim/` 目录执行回归，在 `Transcript` 中确认 case0 全部断言通过、无 `###ERROR###`。

**需要观察的现象**：测试在 Done 态（L124）读了一次状态寄存器，**之后没有再写任何 Ack**，但 L133 却回到了 Idle——这正是下一节要讲的「自动确认」。

**预期结果**：六个断言依次命中 Idle→PreTrig→WaitTrig→PostTrig→Done→Idle，与迁移图一致；case0 整体通过。

> 待本地验证：若你手头没有 Modelsim/Questa，可仅完成步骤 1–2 的源码阅读；步骤 3 标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：在 `Done_s` 分支里，`Arm` 和 `Ack` 用的是 `if ... elsif ...`（互斥），如果改成两个独立的 `if`（即 `if Arm then ... end if; if Ack then ... end if;`），会有什么后果？

> **答案**：`v.State_2` 会被连续赋值两次。如果同一周期 `Arm='1'` 且 `Ack='1'` 同时出现，最后一个 `if`（Ack）会覆盖前者，状态去 Idle 而不是 PreTrig，行为与设计意图相反。用 `elsif` 明确了「Arm 优先」，保证在 Done 态下 Arm 永远胜出，语义清晰且无歧义。

**练习 2**：`PreTrigSpls=0` 时直接 `v.State_2 := WaitTrig_s`。结合 u3-l1 的端口位宽，为什么这里要用 `unsigned(PreTrigSpls) = 0` 判定而不是省略这一步？

> **答案**：`PreTrigSpls` 是可配置的前触发样本数，软件可能把它设为 0（表示「我不需要前触发，触发一来就开始数后触发」）。如果不单独处理 0，下面 `unsigned(PreTrigSpls)-1` 会因无符号下溢变成一个巨大的值，地址计数器永远到不了它，状态机会卡在 PreTrig 永远进不了 WaitTrig。所以必须显式处理 0 这个特例。

### 4.3 Ack 自动确认逻辑

#### 4.3.1 概念说明

`Done` 之后记录器需要一次「确认」（Ack）才能回到 Idle，目的是让软件有机会先把存储里的数据读完，再让记录器进入空闲、等待下一轮。但如果你去看封装层，会发现软件**从来不需要显式写一个「Ack」寄存器位**——只要软件在 Done 态下**读一次状态寄存器**，Ack 就自动产生了。

这是一个很优雅的设计：软件本来就要轮询状态寄存器来「发现录制完成了」，而「读状态 + 发现已完成」这个动作本身就顺手完成了确认。一读两得，软件协议更简单。

之所以把这部分放在封装层（而不是 `data_rec` 核心），是因为「读寄存器」这件事只存在于 AXI 时钟域，而 `data_rec` 核心运行在数据时钟域、对 AXI 毫无感知（见 u2-l1 的双时钟域划分）。

#### 4.3.2 核心流程

自动确认的产生链路：

```
[AXI 域] 软件读 Reg_Stat_Addr_c (0x0000)
        │ reg_rd(状态字)=1  且  reg_stat_state=Done(4)
        ▼
   AckDone='1'  （封装层组合逻辑，单拍）
        │ 经 psi_common_pulse_cc 跨时钟域
        ▼
[数据域] port_cfg_ack='1'  →  data_rec 的 Ack 端口
        │ 命中 Done_s 分支的 elsif Ack
        ▼
   r.State_2: Done_s → Idle_s
```

两个判定条件缺一不可：

1. **被读的是状态寄存器**（`reg_rd(ToWordAddr(Reg_Stat_Addr_c)) = '1'`）——读别的寄存器不算。
2. **当前确实处于 Done 态**（`unsigned(reg_stat_state) = Reg_Stat_StateDone_c`）——非 Done 态读状态寄存器不会触发 Ack。

注意 `reg_stat_state` 已经是从数据域经 `status_cc` 跨到 AXI 域的状态值，所以这里比较的是「AXI 域看到的状态」。`AckDone` 是一个 AXI 域的单拍脉冲，再经 `pulse_cc` 跨回数据域送到 `data_rec` 的 `Ack` 端口。跨时钟域的细节见 u5-l2，本节只关注它「读状态即确认」的语义。

#### 4.3.3 源码精读

Ack 自动确认的核心是一行组合逻辑，位于封装层寄存器解码区：

- 状态回读 [`hdl/data_rec_vivado_wrp.vhd:312`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L312)：`reg_rdata(ToWordAddr(Reg_Stat_Addr_c))(3 downto 0) <= reg_stat_state;` —— 把状态码放到状态寄存器低 4 位供软件读。
- 自动确认 [`hdl/data_rec_vivado_wrp.vhd:313`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L313)：

```vhdl
AckDone <= '1' when (reg_rd(ToWordAddr(Reg_Stat_Addr_c)) = '1')
           and (unsigned(reg_stat_state) = Reg_Stat_StateDone_c) else '0';
```

这一行就是「读状态寄存器 + 当前是 Done」两个条件相与，产生 AXI 域单拍脉冲 `AckDone`。

随后 `AckDone` 被打包进脉冲跨时钟域信号组，送到数据域：

- 打包 [`hdl/data_rec_vivado_wrp.vhd:414-416`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L414-L416)：`CcPFromAxIn(CcPFromAxi_Ack_c) <= AckDone;` —— 与 `Arm`、`TrigCntClr` 一起组成三条「AXI→数据」脉冲。
- 跨域实例 [`hdl/data_rec_vivado_wrp.vhd:418-431`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L418-L431)：`i_cc_pulse_fromAxi`（`psi_common_pulse_cc`）把这三个脉冲同步到数据时钟域。
- 送回核心 [`hdl/data_rec_vivado_wrp.vhd:434`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L434)：`port_cfg_ack <= CcPFromAxOut(CcPFromAxi_Ack_c);`
- 端口映射 [`hdl/data_rec_vivado_wrp.vhd:488`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L488)：`Ack => port_cfg_ack,` —— `data_rec` 实例的 `Ack` 端口接的就是这个跨域后的脉冲，直接命中 4.2 节 `Done_s` 分支的 `elsif Ack = '1'`，完成 Done→Idle 迁移。

测试平台 case0 也明确依赖这一机制：L124 读状态发现 Done，L131 的注释 `-- automatic return to idle after reading done` 直接点明了「读完成状态会自动回 Idle」，随后 L133 断言回到 Idle（见 [`testbench/top_tb/top_tb_case0_pkg.vhd:131-133`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L131-L133)）。

#### 4.3.4 代码实践

**实践目标**：验证「只有读状态寄存器、且处于 Done 态」才会触发 Ack；并解释 `Done` 中断与自动 Ack 的关系。

**操作步骤（源码阅读型）**：

1. 在 [`hdl/data_rec_vivado_wrp.vhd:313`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L313) 找到 `AckDone` 的判定，确认两个条件。
2. 回顾 case0：L66 断言复位后 `Done_Irq='0'`；L127 断言录制完成后 `Done_Irq` 曾跳高（`Done_Irq'last_event < now - lastDoneCheck`）；L143 断言回到 Idle 后 `Done_Irq` 又是 0。
3. 追踪 `Done_Irq` 的来源：`Done` 端口（`r.Done(3)`）经 `pulse_cc` 跨到 AXI 域得到 `Done_Irq`（[`hdl/data_rec_vivado_wrp.vhd:438-455`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L438-L455)）。

**需要观察的现象**：

- `Done_Irq`（中断）和 `Ack`（自动确认）是**两条独立的路径**：中断告知软件「完成了」，软件读状态寄存器查询时顺手产生 Ack。两者都源自 `Done` 事件，但用途不同。
- 如果软件只收到中断却迟迟不读状态寄存器，记录器会**一直停在 Done 态**，不会自动回 Idle——因为 Ack 依赖「读状态」这个动作。

**预期结果**：软件的标准流程是「收到 Done_Irq → 读状态寄存器确认是 Done（同时自动 Ack 回 Idle）→ 读存储数据」。`Done_Irq` 是通知，读状态是确认，两步缺一不可。

#### 4.3.5 小练习与答案

**练习 1**：如果软件在 `PostTrig` 态（还没到 Done）就读了状态寄存器，会发生什么？

> **答案**：不会触发 Ack。因为 `AckDone` 的第二个条件 `unsigned(reg_stat_state) = Reg_Stat_StateDone_c` 不满足（此时状态码是 3 而非 4），`AckDone` 保持 0。软件在任意状态读状态寄存器都是安全的，只有 Done 态下的读取才会触发确认。这正是把「状态判定」写进 AckDone 条件的目的。

**练习 2**：为什么 `AckDone` 必须走 `pulse_cc`（脉冲跨域），而不能像 `PreTrigSpls`、`TrigEna` 那样走 `status_cc`（状态跨域）？

> **答案**：`AckDone` 是一个**单拍事件**（读状态寄存器只在一个 AXI 周期内拉高 `reg_rd`），其语义是「这一次确认」。如果走 `status_cc`（按多比特状态采样），单拍脉冲很可能被采漏，或者被展宽成不确定的电平，导致 Ack 丢失或重复。`pulse_cc` 专门用于把单拍脉冲可靠地从一个时钟域搬到另一个时钟域，并保证对侧也输出一个干净的脉冲，正是 Ack 这种「事件型」信号需要的。配置类信号（如 `PreTrigSpls`）是持续保持的电平值，用 `status_cc` 采样即可，两者不能混用（详见 u5-l2）。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全状态序列追踪」任务。

**场景**：软件要对一个通道做一次普通外部触发录制，参数为 `PreTrigSpls=30`、`TotalSpls=100`、`MemoryDepth_g=128`，外部触发已使能。

**任务**：

1. **画迁移图**：参照 4.2.2 的格式，自己画一张五状态迁移图，在每个箭头上写出**精确的源码级条件**（引用 `hdl/data_rec.vhd` 的行号），而不是泛泛地写「前触发满」。
2. **标注脉冲**：在图上用星号标出哪两个迁移会产生单拍脉冲（`Trigger_2`、`Done(2)`），并说明它们分别对外走到哪个端口（`Trig_Out`、`Done`→`Done_Irq`）。
3. **追踪自动确认**：写出软件从「收到 `Done_Irq`」到「记录器回到 Idle」之间发生的全部事件序列（按顺序列出：读哪个寄存器、哪个信号拉高、跨了几次时钟域、命中 `case r.State_2` 的哪个分支）。
4. **回答关键问题**：在 Done 态下，如果软件不读数据而是直接写 Arm，状态机会怎样？如果软件既不读状态也不写 Arm，状态机又会怎样？

**参考要点（用于自检）**：

- Idle→PreTrig：Arm（L248）；PreTrig→WaitTrig：`AdrCnt_2=29 且 In_Vld(1)='1'`（L256）；WaitTrig→PostTrig：`TrigNow_2='1'`（L260，同时 `Trigger_2:='1'` L262）；PostTrig→Done：`SplCnt_2>=100`（L265，同时 `Done(2):='1'` L267）；Done→Idle：`Ack='1'`（L272）。
- 脉冲：WaitTrig→PostTrig 产生 `Trigger_2`→`Trig_Out`；PostTrig→Done 产生 `Done(2)`→`r.Done(3)`→`Done` 端口→`pulse_cc`→`Done_Irq`。
- 自动确认序列：软件收到 `Done_Irq` → 读 `Reg_Stat_Addr_c`（0x0000）→ `reg_rd` 拉高 + `reg_stat_state=4` → `AckDone='1'`（L313）→ `pulse_cc` 跨到数据域 → `port_cfg_ack='1'` → 命中 `Done_s` 的 `elsif Ack`（L272）→ `r.State_2` 变 `Idle_s`。
- Done 下直接写 Arm：命中 `Done_s` 的 `if Arm`（L270），直达 PreTrig 开下一轮，**跳过回 Idle**。既不读状态也不写 Arm：停在 Done 不动，`DoneTime` 计数持续累加（见 u4-l5）。

## 6. 本讲小结

- 记录器用枚举类型 `State_t` 定义五个状态（`Idle_s/PreTrig_s/WaitTrig_s/PostTrig_s/Done_s`），枚举位置 0–4 与寄存器包常量 `Reg_Stat_State*_c`、对外 `State` 端口数值一一对应，三处必须同步维护。
- 状态迁移全部写在 `p_comb` 的 `case r.State_2`（`hdl/data_rec.vhd:246-276`）：Arm 启动、前触发满进入等待、`TrigNow_2` 触发后触发、`SplCnt_2>=TotalSpls` 完成、Done 下 Arm 重新录制或 Ack 回 Idle。
- `Trigger_2` 和 `Done(2)` 是在 `case` 之前清零、命中迁移时置 1 的单拍脉冲，分别对外走到 `Trig_Out` 和 `Done`（再跨域成 `Done_Irq`）。
- `PreTrigSpls=0` 是被显式处理的特例，避免无符号下溢导致状态机卡死。
- Ack 由封装层自动产生：软件在 Done 态读状态寄存器即触发 `AckDone`（`hdl/data_rec_vivado_wrp.vhd:313`），经 `pulse_cc` 跨域送回核心命中 Done→Idle。
- `Done_Irq`（通知）与 `Ack`（确认）是两条独立路径：中断告诉软件「完成了」，读状态寄存器这个动作本身完成确认。

## 7. 下一步学习建议

本讲把状态机的「骨架」讲清楚了，但还没回答两个问题：这些带 `_2` 后缀的信号到底是怎么一拍一拍搬过来的？地址计数器和采样计数器又是如何精确配合状态机决定「前触发满」「后触发满」的？

- **u3-l3 两进程法与 Stage0-3 流水线**：深入 `p_comb`/`p_seq` 模板与 `data_rec_r` 记录，把本讲里所有 `_2`/`_3` 后缀信号的来龙去脉讲透。
- **u3-l4 地址计数器、采样计数器与触发长度**：讲解 `AdrCnt_2`、`SplCnt_2` 如何为本讲的 PreTrig→WaitTrig、PostTrig→Done 迁移提供判定依据。
- **u4-l1 触发源总览与 TrigEna 掩码**：本讲把 `TrigNow_2` 当作一个现成条件，u4-l1 会拆开它由三类触发源（外部/软件/自触发）经 `TrigEna` 掩码合成的全过程。
