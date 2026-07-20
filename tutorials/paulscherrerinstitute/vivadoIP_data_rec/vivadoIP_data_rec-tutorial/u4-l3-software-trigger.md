# 软件触发：sticky pending 行为

## 1. 本讲目标

本讲聚焦三类触发源中的**软件触发（Software Trigger）**。读完本讲你应该能够：

- 说清软件触发从「软件写 AXI 寄存器」到「核心内部产生一次录制」的完整通路；
- 理解 `SwTrigPending_2` 的 **sticky（粘滞）pending** 语义：写 1 后请求一直保持，直到进入 `PreTrig` 状态才被清除；
- 解释为什么「先写 `SwTrig=1`、再 `Arm`」也能立即触发，以及为什么这种粘滞行为是 **free-running（自循环）模式** 得以实现的关键；
- 判断在一次录制完成后是否需要把 `SwTrig` 写回 0，并说明原因。

本讲承接 u4-l1 建立的「三类触发源 + TrigEna 掩码 + TrigNow_2 合成」总框架，只把镜头推到软件触发这一条链路上。

## 2. 前置知识

在进入本讲前，你需要已经掌握以下概念（来自前置讲义）：

- **记录状态机**（u3-l2）：`Idle → PreTrig → WaitTrig → PostTrig → Done`。触发只在 `WaitTrig` 状态经 `TrigNow_2=1` 兑现，迁入 `PostTrig`。
- **两进程法与流水级命名**（u3-l3）：组合进程 `p_comb` 用 `v := r` 模板计算下一拍 `r_next`；信号名后的数字后缀（如 `SwTrigPending_2`、`Trigger_2`）表示它所在的**流水级**，`_2` 即 Stage2 对齐。
- **触发源总框架**（u4-l1）：三类源（外部 / 软件 / 自触发）经各自 `TrigEna` 位相与后 OR 成 `TrigNow_2`，再与 `r.In_Vld(1)` 相与门控。其中 `TrigEna` 的三个位索引为 `Reg_TrigEna_ExtIdx_c=0`、`Reg_TrigEna_SwIdx_c=1`、`Reg_TrigEna_SelfIdx_c=2`。

一个关键术语要先点明：**pending（待处理请求）**。外部触发与软件触发都不是「边沿一到就立刻触发」，而是先把「有一个触发请求」这件事**锁存**成一个标志位（pending），等记录器走到 `WaitTrig` 状态、且 `In_Vld` 有效时才消费它。本讲要讲的 sticky，就是软件触发这个 pending 标志位的特殊清除规则。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `hdl/data_rec.vhd` | 核心记录器。定义 `SwTrig` 输入端口、`SwTrigPending_2` 锁存标志，以及 sticky 置位/清除的全部逻辑。 |
| `hdl/data_rec_register_pkg.vhd` | 寄存器地图。定义软件触发寄存器地址 `Reg_SwTrig_Addr_c`、位索引 `Reg_SwTrig_TrigIdx_c`，以及触发源掩码位 `Reg_TrigEna_SwIdx_c`。 |
| `hdl/data_rec_vivado_wrp.vhd` | Vivado 封装层。把 AXI 写入解码成 `reg_swtrig` 电平，经 `status_cc` 跨时钟域送到核心的 `SwTrig` 端口。 |
| `testbench/top_tb/top_tb_case3_pkg.vhd` | 软件触发测试用例 `case3`。覆盖单次触发、采样中触发、未 Arm 时不触发、先写触发再 Arm、free-running 重 Arm 等。 |
| `testbench/top_tb/top_tb_pkg.vhd` | 测试平台公共过程 `InputSamples` / `CheckData`，是理解 case3 激励与校验的基础。 |
| `epics/TemplateInput/CONTROL.tpl` | EPICS 控制模板。`SWTRIG` 记录默认值为 1，注释明确指出这是 free-running 模式的前提。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **SwTrig 端口与软件触发寄存器**——软件触发从哪里来，如何在地址地图上找到它。
2. **SwTrigPending_2 的 sticky 置位与清除**——核心内部如何锁存、何时清除。
3. **free-running 模式与 EPICS SWTRIG 默认值**——把 sticky 行为放到真实使用场景中理解。

---

### 4.1 SwTrig 端口与软件触发寄存器

#### 4.1.1 概念说明

软件触发，就是**由软件（CPU/PS 端）主动写一个寄存器位来命令记录器「现在触发一次」**。它与外部触发（硬件脉冲）、自触发（数据落入选定范围）并列，是三类触发源之一。

软件触发的物理体现是核心 `data_rec` 上的一个单比特输入端口 `SwTrig`。这个端口**不是一拍脉冲**，而是一个**电平（level）**：当软件往寄存器写了 1，这个端口在核心侧就持续为 1，直到软件再写 0。这一点和 `Arm`（写一次产生一拍脉冲）不同，是后面理解 sticky 行为的前提。

在寄存器地图上，软件触发对应一个 32 位寄存器 `Reg_SwTrig_Addr_c`，只有 bit0 有效（`Reg_SwTrig_TrigIdx_c`）。而要让它真正参与触发，还必须在触发源使能寄存器 `Reg_TrigEna_Addr_c` 中把软件触发对应的位（bit1，`Reg_TrigEna_SwIdx_c`）置 1。

#### 4.1.2 核心流程

软件触发的「下达」通路如下：

```text
软件写 AXI:  Reg_SwTrig_Addr_c ← 1（bit0）
        │  (reg_wdata 在 AXI 域保持这个值)
        ▼
封装层解码:  reg_swtrig <= reg_wdata(bit0)        —— 电平，不是脉冲
        │  (status_cc 跨 AXI 域 → 数据域)
        ▼
核心端口:    SwTrig = '1'                          —— 持续为 1，直到软件写 0
        ▼
进入 4.2:    把请求锁存进 SwTrigPending_2
```

软件触发对应的 `TrigEna` 写入值为：

\[
\text{TrigEna}_{\text{sw}} = 2^{\text{Reg\_TrigEna\_SwIdx\_c}} = 2^{1} = 2
\]

即只使能软件触发时，往 `Reg_TrigEna_Addr_c` 写 `0x2`（这一点 case3 第 59 行就是这么做的）。

#### 4.1.3 源码精读

**核心端口定义**——`SwTrig` 是一个单比特输入，注释写得很直白「Force trigger from software」：

[hdl/data_rec.vhd:L61-L61](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L61-L61) —— `SwTrig : in std_logic;`，软件强制触发输入。

**寄存器地图中的软件触发寄存器**——地址 `0x001C`，只有 bit0 是触发位：

[hdl/data_rec_register_pkg.vhd:L43-L44](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L43-L44) —— `Reg_SwTrig_Addr_c = 16#001C#`，`Reg_SwTrig_TrigIdx_c = 0`。

**触发源使能位索引**——软件触发对应 `TrigEna` 的 bit1：

[hdl/data_rec_register_pkg.vhd:L50-L53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L50-L53) —— 三个位索引 Ext=0、Sw=1、Self=2。

**封装层把 AXI 写入解码成 `reg_swtrig` 电平**——注意这里**只取 `reg_wdata`**，没有 `and reg_wr`：

[hdl/data_rec_vivado_wrp.vhd:L325-L325](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L325-L325) —— `reg_swtrig <= reg_wdata(...)(Reg_SwTrig_TrigIdx_c);`

对比紧邻的 `Arm` 解码就能看出差别：

[hdl/data_rec_vivado_wrp.vhd:L316-L316](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L316-L316) —— `reg_cfg_arm <= reg_wr(...) and reg_wdata(...)(Reg_Cfg_ArmIdx_c);`

`Arm` 用 `reg_wr and reg_wdata`，`reg_wr` 只在 AXI 写当拍为 1，所以 `reg_cfg_arm` 是**单拍脉冲**；而 `reg_swtrig` 只用 `reg_wdata`，`reg_wdata` 会**保持**最近一次写入的值，所以 `reg_swtrig` 是**电平**。这是软件触发能「持续请求」的根源。

**跨时钟域**——`reg_swtrig` 这个电平经 `status_cc`（不是 `pulse_cc`！）从 AXI 域送到数据域：

[hdl/data_rec_vivado_wrp.vhd:L382-L382](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L382-L382) —— 把 `reg_swtrig` 打入 `CcSFromAxIn` 的 `SwTrig_c` 比特。

[hdl/data_rec_vivado_wrp.vhd:L409-L409](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L409-L409) —— 从 `CcSFromAxOut` 取出送给 `port_swtrig`。

[hdl/data_rec_vivado_wrp.vhd:L496-L496](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L496-L496) —— `SwTrig => port_swtrig`，连到核心实例。

> 小结：软件触发走的是「**电平寄存器 + status_cc**」通路，到达核心 `SwTrig` 端口时是一个可持续的电平。这一点决定了它和外部触发（边沿、脉冲）在性质上完全不同。

#### 4.1.4 代码实践

**实践目标**：在地址地图上定位软件触发寄存器，并理解 `reg_swtrig` 为何是电平而非脉冲。

**操作步骤**：

1. 打开 `hdl/data_rec_register_pkg.vhd`，确认 `Reg_SwTrig_Addr_c = 16#001C#`（字节地址 0x1C，即字地址 7）。
2. 打开 `hdl/data_rec_vivado_wrp.vhd`，对比第 316 行（`reg_cfg_arm`）与第 325 行（`reg_swtrig`）的解码表达式。
3. 思考：若把第 325 行改成 `reg_swtrig <= reg_wr(...) and reg_wdata(...)(Reg_SwTrig_TrigIdx_c);`，行为会变成什么？

**需要观察的现象 / 预期结果**：

- 当前实现下，软件写一次 `0x1C ← 1`，`reg_swtrig` 会**一直保持 1**，直到软件写 `0x1C ← 0`。
- 若改成 `reg_wr and reg_wdata`，`reg_swtrig` 只在写当拍为 1（一拍脉冲）。结合 4.2 的 sticky 逻辑思考：改成脉冲后，**先写触发再 Arm** 的用例还能触发吗？（提示：脉冲只活一拍，跨时钟域后未必能被 `if SwTrig='1'` 捕获到。）

> 本实践为源码阅读型实践，无需运行仿真；如需验证，可在改码后跑 case3 观察是否仍通过（**注意：本实践仅为思考练习，请勿真正修改源码**）。

#### 4.1.5 小练习与答案

**练习 1**：要让记录器**只**接受软件触发，应往 `Reg_TrigEna_Addr_c` 写什么值？要同时接受外部和软件触发呢？

**答案**：只接受软件触发写 \(2^1 = 2\)（`0x2`）；同时接受外部（bit0）和软件（bit1）写 \(2^0 + 2^1 = 3\)（`0x3`）。

**练习 2**：`SwTrig` 端口是电平还是脉冲？依据是封装层的哪一行？

**答案**：电平。依据是 [data_rec_vivado_wrp.vhd:L325](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L325-L325) 只用 `reg_wdata`（保持型），没有 `and reg_wr`（写脉冲）。

---

### 4.2 SwTrigPending_2 的 sticky 置位与清除

#### 4.2.1 概念说明

软件触发到达核心 `SwTrig` 端口后，**并不直接**进入 `TrigNow_2`，而是先被锁存成一个标志位 `SwTrigPending_2`。这个标志位有一个特别的清除规则，称为 **sticky（粘滞）pending**：

- **置位**：只要 `SwTrig = '1'`，`SwTrigPending_2` 就被置 1；
- **清除**：只有进入 `PreTrig_s` 状态（且此刻 `SwTrig = '0'`）才被清 0；复位和在 `MinRecPeriod` 抑制期内也会清 0。

「粘滞」的含义是：**一旦置 1，即使软件立刻把 `SwTrig` 写回 0，这个 pending 也不会马上消失**——它会一直等到下一次录制开始（进入 `PreTrig`）才被抹掉。换句话说，软件触发被设计成一个「**下达一次命令，等待消费一次**」的单次请求，而不是一个「电平持续多久就触发多少次」的条件。

这与外部触发 `ExtTrigPending_2` 在结构上很像（都是锁存 pending，都在 `PreTrig` 清除），但触发条件不同：外部是**上升沿**置位，软件是**电平**置位。

#### 4.2.2 核心流程

`SwTrigPending_2` 的完整生命周期：

```text
                  ┌─────────────────────────────────────────────┐
   SwTrig='1' ──▶ │  SwTrigPending_2 := 1   (任何状态都置位)    │
                  └─────────────────────────────────────────────┘
                                        │
                                        ▼
                   参与 TrigNow_2 合成（在 WaitTrig 被消费）
                                        │
              ┌─────────────────────────┼─────────────────────────┐
                清除条件（任一发生即清 0）:
                (a) 进入 PreTrig_s 且 SwTrig='0'    —— 新一轮录制抹掉旧请求
                (b) MinRecPeriod 抑制期内           —— 来得太早的请求被丢弃
                (c) 复位 Rst='1'                    —— 同步复位
```

注意 sticky 的精妙之处在清除条件 (a) 的那个「**且 `SwTrig='0'`**」：进入 `PreTrig` 时，如果 `SwTrig` 仍然是 1，则**置位分支优先**，pending 不会被清除反而继续保持 1。这正是 free-running 模式的支点（详见 4.3）。

#### 4.2.3 源码精读

**sticky pending 的核心逻辑**——一段 `if ... elsif ...`，置位优先于 PreTrig 清除：

[hdl/data_rec.vhd:L217-L222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L217-L222) —— 软件触发 pending 的置位与清除。读法：

```vhdl
-- SW Trigger
if SwTrig = '1' then            -- ① 电平置位：SwTrig=1 → pending=1（优先级最高）
    v.SwTrigPending_2 := '1';
elsif r.State_2 = PreTrig_s then-- ② 进入 PreTrig 且 SwTrig=0 → 清除（新一轮抹旧请求）
    v.SwTrigPending_2 := '0';
end if;                         -- ③ 其它情况：v := r 默认保持 → 这就是"粘滞"
```

关键点：`v := r` 是 `p_comb` 开头的默认赋值（见 [data_rec.vhd:L153](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L153-L153)），所以当 `if/elsif` 都不命中时，`SwTrigPending_2` **保持原值不变**——这正是 sticky 的实现机制。

**参与 TrigNow_2 合成**——pending 与 `TrigEna(bit1)` 相与后 OR 进总触发，再受 `In_Vld(1)` 门控：

[hdl/data_rec.vhd:L225-L228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L225-L228) —— 触发掩码合成。软件触发对应的一项是 `(r.SwTrigPending_2 and TrigEna(Reg_TrigEna_SwIdx_c))`。注意它读的是 `r.SwTrigPending_2`（已寄存的值），而上一段写的是 `v.SwTrigPending_2`（下一拍的新值），两者错开一拍——所以**当拍置位的 pending，要到下一拍才能参与触发**。

**MinRecPeriod 抑制期清除**——两次录制间隔太短时，早到的请求被丢弃（不延后）：

[hdl/data_rec.vhd:L231-L241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L231-L241) —— 最小录制间隔计数器，其中 [L238](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L238-L238) 一并把 `v.SwTrigPending_2 := '0'` 清掉。

**复位清除**——同步复位把 pending 清 0：

[hdl/data_rec.vhd:L379-L379](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L379-L379) —— `r.SwTrigPending_2 <= '0';`（在 `p_seq` 的复位分支）。

#### 4.2.4 代码实践

**实践目标**：用 case3 的「单次软件触发」片段，验证 sticky pending「写 1 后即使马上写 0，触发依然兑现」的行为。

**操作步骤**：

1. 打开 `testbench/top_tb/top_tb_case3_pkg.vhd`，阅读第 56–75 行的第一个子用例。
2. 关注第 67–69 行的激励序列：

   ```vhdl
   axi_single_write(Reg_SwTrig_Addr_c, 1*2**Reg_SwTrig_TrigIdx_c, ...);  -- 写 SwTrig=1
   wait for 200 ns;
   axi_single_write(Reg_SwTrig_Addr_c, 0, ...);                           -- 马上写 SwTrig=0
   wait for 200 ns;
   InputSamples(100, ToDut, Clk);                                         -- 继续灌样本
   ```

3. 跑仿真（在 `sim/` 目录按 README 执行回归，参考 u1-l3），观察 case3 是否通过。

**需要观察的现象**：

- 软件在 `WaitTrig` 状态写了 `SwTrig=1` 又立刻写 `0`，但记录器**仍然在随后走到 `Done`**（第 74 行 `axi_single_expect(... Reg_Stat_StateDone_c ...)` 期望 Done）。
- `CheckData(10, 95, ...)` 校验录制了 10 个样本、首个样本序号为 95（与触发时刻吻合）。

**预期结果**：case3 该子用例通过。原因是写 `SwTrig=1` 那一拍已经把 `SwTrigPending_2` 置 1，之后写 `0` 只让 `SwTrig` 电平拉低，但 `if SwTrig='1'` 不再命中、`elsif PreTrig` 也不命中（状态已是 `WaitTrig`），所以 pending **保持 1**，直到在 `WaitTrig` 被 `TrigNow_2` 消费。

> 若无条件运行仿真，可标注「待本地验证」，仅做源码追踪。

#### 4.2.5 小练习与答案

**练习 1**：设 `SwTrigPending_2` 当前为 1，记录器在 `WaitTrig` 状态、`In_Vld` 有效、`TrigEna(bit1)=1`。下一拍 `SwTrigPending_2` 会变成什么？

**答案**：触发当拍 `TrigNow_2=1` 使状态迁入 `PostTrig`；但 `SwTrigPending_2` 的清除只发生在 `PreTrig_s`，`PostTrig` 不清除它。若此刻 `SwTrig=0`，则 pending 保持 1（不会被消费清零，要等下一次 `PreTrig`）。这说明 pending **不是「消费即清」**，而是「新一轮录制才清」。

**练习 2**：为什么 `TrigNow_2` 表达式里软件触发一项读 `r.SwTrigPending_2`，而置位逻辑写 `v.SwTrigPending_2`？

**答案**：`v` 是下一拍的新值，`r` 是当前已寄存的值。组合进程里「置位」计算的是下一拍的状态，而「参与触发」必须用当前确定的状态，两者错开一拍，保证置位的请求下一拍才生效，避免组合环路与同一拍内竞争。

---

### 4.3 free-running 模式与 EPICS SWTRIG 默认值

#### 4.3.1 概念说明

理解了 sticky pending，就能回答本讲最实用的一个问题：**free-running（自循环）模式是怎么实现的？**

所谓 free-running，就是记录器「**一录完就自动再录，循环不停**」，不依赖外部信号或数据特征，纯粹靠软件循环驱动。在本 IP 里，它的实现极为简洁：

- 让 `SwTrig` **始终保持 1**（软件只写一次 `0x1C ← 1`，之后不再写 0）；
- 每次录制结束后，软件（或 EPICS 状态机）**重新 Arm**。

由于 sticky 规则中「置位优先于 PreTrig 清除」，只要 `SwTrig=1`，那么每一次进入 `PreTrig` 时 pending 都会立刻被重新置 1 → 走到 `WaitTrig` 立刻触发 → 录完进 `Done` → 重新 Arm 又进 `PreTrig` → 又立刻触发 …… 如此循环。这就是 free-running。

正因为此，EPICS 控制模板里 `SWTRIG` 这个记录的**默认值被设成 1**，并且模板里有一行注释明确警告：「Should always be one, otherwise freerunnning mode will not work」（应始终保持为 1，否则 free-running 模式无法工作）。

#### 4.3.2 核心流程

free-running 一次循环的状态流转（`SwTrig` 恒为 1）：

```text
Arm ─▶ PreTrig ─▶ WaitTrig ─▶ PostTrig ─▶ Done ─▶ (软件读状态→Ack 或直接重新 Arm) ─▶ PreTrig ─▶ ...
        │                                                          │
        └ 进入 PreTrig 时:                                        └ Done 后重新 Arm
          SwTrig=1 → pending 仍=1 (置位优先)                       回到 PreTrig，pending 仍=1
          → 量化前触发样本后到 WaitTrig                            → 立刻又触发 → 循环
          → pending=1 → TrigNow=1 → 立刻触发
```

对照 EPICS 触发源选择 `TRIGSRC`，「Free-Running」这一档的值正是 2（\(=2^{\text{SwIdx}}\)），即把软件触发位选上——这从另一个角度印证了 free-running = 软件触发源 + `SwTrig` 保持 1。

#### 4.3.3 源码精读

**EPICS `SWTRIG` 记录默认值为 1**——模板里白纸黑字：

[epics/TemplateInput/CONTROL.tpl:L188-L198](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L188-L198) —— `record(bo, "...SWTRIG")`，第 189 行注释「Should always be one, otherwise freerunnning mode will not work」，第 194 行 `field(VAL, "1")`，第 195 行 `field(PINI, "YES")`（初始化即处理，上电就把 1 写进寄存器）。

**EPICS 触发源选择中 Free-Running = 值 2**：

[epics/TemplateInput/CONTROL.tpl:L119-L131](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L119-L131) —— `TRIGSRC` 的 `TWVL=2` 对应 `TWST="Free-Running"`，值 2 = \(2^{\text{Reg\_TrigEna\_SwIdx\_c}}\) = 软件触发位。

**EPICS 读数据后自动重新 Arm**——`POSTREAD` 记录在读完数据后链到 `ARM.PROC`：

[epics/TemplateInput/CONTROL.tpl:L326-L329](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L326-L329) —— `POSTREAD` 的 `DO1=1`、`LNK1 → ARM.PROC`，即每次读完数据自动重新 Arm。配合 `SWTRIG=1`（sticky pending），构成 free-running 闭环。

**case3 中 free-running 的直接演示**——录完后**不**清 `SwTrig`，直接重新 Arm，立即又触发：

[testbench/top_tb/top_tb_case3_pkg.vhd:L150-L168](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L150-L168) —— 关键序列（节选）：

```vhdl
-- 第 153 行: 先写 SwTrig=1（之后一直没写 0）
axi_single_write(Reg_SwTrig_Addr_c, 1*2**Reg_SwTrig_TrigIdx_c, ...);
-- 第 154 行: 然后 Arm
axi_single_write(Reg_Cfg_Addr_c, 1*2**Reg_Cfg_ArmIdx_c, ...);
...
-- 第 158 行: 期望 Done（"Done Status 6"）—— 先置触发再 Arm 也能立即触发
axi_single_expect(Reg_Stat_Addr_c, Reg_Stat_StateDone_c, ...);
...
-- 第 162 行: 录完后 SwTrig 仍为 1，直接重新 Arm
axi_single_write(Reg_Cfg_Addr_c, 1*2**Reg_Cfg_ArmIdx_c, ...);
...
-- 第 166 行: 再次期望 Done（"Done Status 7"）—— 没有再写 SwTrig，却再次触发 = free-running
axi_single_expect(Reg_Stat_Addr_c, Reg_Stat_StateDone_c, ...);
-- 第 168 行: 用例结尾才把 SwTrig 写回 0（清理现场，方便后续用例）
axi_single_write(Reg_SwTrig_Addr_c, 0, ...);
```

注意第 162 行重新 Arm 前后**都没有**再写 `SwTrig`，但第 166 行依然期望 `Done`——这正是 sticky pending 让 free-running 成为可能的直接证据。

#### 4.3.4 代码实践

**实践目标**：用 case3 的 free-running 片段回答综合实践任务的两个问题——sticky 为何让 free-running 可行？触发后需要把 `SwTrig` 写回 0 吗？

**操作步骤**：

1. 阅读 [top_tb_case3_pkg.vhd:L150-L168](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L150-L168)，对照 [data_rec.vhd:L217-L222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L217-L222) 的 sticky 逻辑。
2. 用一张时序表推演「`SwTrig` 恒为 1、反复 Arm」时，每次进入 `PreTrig` 那一拍 `SwTrigPending_2` 的取值。
3. 回答：触发后是否需要把 `SwTrig` 写回 0？

**需要观察的现象 / 预期结论**：

- **sticky 为何让 free-running 可行**：因为清除条件 (a) 是「进入 `PreTrig` **且** `SwTrig=0`」。当 `SwTrig` 恒为 1 时，`if SwTrig='1'` 分支永远优先命中，pending 在每次 `PreTrig` 都被重新置 1，于是每次 Arm 后必然立即触发，形成循环。如果 pending 改成「消费即清」或「`SwTrig` 下降沿清」，free-running 就不成立。
- **触发后需要把 `SwTrig` 写回 0 吗？——视模式而定**：
  - **单次模式（一次 Arm 只录一次）**：**需要**写回 0。否则下一次 Arm 会因为 pending 仍为 1 而立即触发。case3 中所有单次子用例（第 67–69、80–82、94–96 行等）都在触发后把 `SwTrig` 写回 0，就是这个原因。
  - **free-running 模式**：**不需要**写回 0，反而**必须保持 1**。这正是 EPICS `SWTRIG` 默认值为 1、且注释「应始终为 1」的原因。
- 所以「是否写回 0」没有唯一答案，取决于你想要单次还是循环。case3 最后第 168 行写回 0，只是为了清理现场、避免影响后续用例，并非触发所必需。

> 待本地验证：可在仿真中把 case3 第 168 行注释掉，观察后续用例是否会因 `SwTrig` 残留为 1 而误触发（提示：后续用例若重新 Arm，确实会立即触发）。

#### 4.3.5 小练习与答案

**练习 1**：假如把 [data_rec.vhd:L217-L222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L217-L222) 的 `if/elsif` 顺序对调成「先判 `PreTrig` 清除、再判 `SwTrig` 置位」，free-running 还能工作吗？

**答案**：不能。对调后，进入 `PreTrig` 那一拍会**先**把 pending 清 0，哪怕 `SwTrig=1`。于是重新 Arm 后 pending 为 0，不会立即触发，free-running 断链。这正说明现有「置位优先」的顺序是 free-running 成立的必要条件。

**练习 2**：EPICS 模板里 `SWTRIG` 记录 `field(VAL, "1")` 与 `field(PINI, "YES")` 合在一起的效果是什么？

**答案**：`VAL=1` 设默认值为 1，`PINI=YES`（Process On Initialization）让该记录在 EPICS 启动时立即处理一次，把值 1 写入硬件寄存器 `0x1C`。两者合起来保证上电后 `SwTrig` 立即为 1，free-running 模式开箱可用。

**练习 3**：case3 第 102–117 行测试「未 Arm 时写 `SwTrig` 无效」。结合状态机解释为什么无效。

**答案**：未 Arm 时状态为 `Idle_s`。写 `SwTrig=1` 会把 `SwTrigPending_2` 置 1，但状态机在 `Idle` 下只看 `Arm`（[data_rec.vhd:L247-L250](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L247-L250)），不会进入 `WaitTrig`，pending 永远不被消费。一旦之后 Arm，进入 `PreTrig` 时若 `SwTrig` 已被写回 0，pending 被清除；若仍为 1 则会触发。所以「未 Arm 时写触发」本身不会产生录制。

---

## 5. 综合实践

**任务**：画出软件触发从「软件写寄存器」到「核心兑现一次录制」的**完整时序链**，并标注每一段落在哪个时钟域、对应哪一行源码；然后用一段话回答综合实践题。

建议按以下步骤完成：

1. **画链路图**（含时钟域标注）：
   - AXI 域：软件写 `Reg_SwTrig_Addr_c ← 1` → `reg_wdata` 保持 → 解码出 `reg_swtrig` 电平（[data_rec_vivado_wrp.vhd:L325](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L325-L325)）。
   - 跨域：`reg_swtrig` 经 `status_cc`（AXI→Data）到 `port_swtrig`（[L382](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L382-L382)、[L409](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L409-L409)）。
   - 数据域：`SwTrig` 端口 → sticky 置位 `SwTrigPending_2`（[data_rec.vhd:L217-L222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L217-L222)）→ 与 `TrigEna(bit1)`、`In_Vld(1)` 合成 `TrigNow_2`（[L225-L228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L225-L228)）→ `WaitTrig` 状态消费 → 迁入 `PostTrig`（[L259-L263](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L259-L263)）。
2. **回答综合实践题**（直接引用 case3 证据）：
   - sticky pending 为何让 free-running 可行？—— 引用 [top_tb_case3_pkg.vhd:L162-L166](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L162-L166)（重 Arm 未再写 `SwTrig` 仍触发）与 [data_rec.vhd:L218-L219](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L218-L219)（置位优先）。
   - 触发后需要把 `SwTrig` 写回 0 吗？—— 单次模式需要（引用 case3 第 69 行），free-running 不需要且必须保持 1（引用 [CONTROL.tpl:L189](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L189-L189) 注释）。

> 完成后，你应当能用一两句话向别人解释清楚：为什么本 IP 的软件触发寄存器「上电默认 1」是一个刻意的设计，而不是一个 bug。

## 6. 本讲小结

- 软件触发由 `SwTrig` 单比特端口引入，对应寄存器 `Reg_SwTrig_Addr_c (0x1C)` 的 bit0，并由 `TrigEna` 的 bit1（`Reg_TrigEna_SwIdx_c`）使能。
- 封装层把 AXI 写入解码成 `reg_swtrig` **电平**（仅用 `reg_wdata`，无 `reg_wr`），经 `status_cc` 跨到数据域——这是软件触发「可持续请求」的根源。
- 核心 `SwTrigPending_2` 是 **sticky pending**：`SwTrig=1` 即置位（优先级最高），仅在进入 `PreTrig` 且 `SwTrig=0` 时清除，复位与 `MinRecPeriod` 抑制期也会清。
- 「置位优先于 PreTrig 清除」的 `if/elsif` 顺序是 free-running 成立的必要条件：`SwTrig` 恒为 1 时，每次重新 Arm 都会立即触发。
- 触发后是否写回 `SwTrig=0` 取决于模式：单次模式必须写 0 以免误触发，free-running 必须保持 1。
- EPICS 模板 `SWTRIG` 记录默认 `VAL=1`、`PINI=YES`，注释「应始终为 1，否则 free-running 不工作」，印证了上述设计意图。

## 7. 下一步学习建议

- **继续触发单元**：下一篇 u4-l4 讲**自触发**（self-trigger），它和软件触发共享同一套 `TrigEna` 合成框架，但 pending 处理截然不同（非锁存的瞬时变量、按通道范围检测、进入/退出边沿），对照阅读能加深对「为什么只有外部和软件用锁存 pending」的理解。
- **横向对比**：回头对比 u4-l2 外部触发的 `ExtTrigPending_2`（上升沿置位、同样在 `PreTrig` 清除），三种触发源的异同就齐了。
- **延伸到封装与集成**：若想搞清 `status_cc` 为何能安全传递 `SwTrig` 电平、而 `Arm` 必须走 `pulse_cc`，请阅读 u5-l2（跨时钟域策略）；若想了解 EPICS 如何把 `SWTRIG`、`TRIGSRC` 经 regDev 映射到这些寄存器地址，请阅读 u6-l3（EPICS 集成）。
