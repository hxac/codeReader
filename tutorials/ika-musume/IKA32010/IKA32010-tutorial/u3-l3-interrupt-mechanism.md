# 中断机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚外部中断引脚 `i_INT_n` 的一个下降沿，如何经过 `int_n_z / int_n_zz / int_n_zzz` 三级同步链被识别，并被锁存进 `int_latched`。
- 解释 `int_rq = int_latched & ~reg_intm` 这个式子里每一项的含义，以及 `reg_intm`（DINT/EINT 控制）如何充当中断总开关。
- 复述一次完整中断响应的全部动作：跳转到向量地址 `0x002`、把返回地址压入硬件栈、注入内部 IACK 操作码完成应答与清锁存。
- 把中断响应放进 [u3-l2](u3-l2-multicycle-timing-and-state.md) 讲过的「机器周期 / 相位 / 多周期原子性」框架里，理解为什么多周期指令执行期间中断会被自动推迟。

本讲是专家层第三讲，承接 [u3-l1（微码架构）](u3-l1-microcode-architecture.md) 与 [u3-l2（多周期时序与状态机）](u3-l2-multicycle-timing-and-state.md)，需要你已经熟悉「微码默认值 + casez 覆盖」「`cyc_ncen` 主工作拍」「`if_opcodereg_force_iack` 把指令寄存器强制刷成 `0xF000`」这些机制。

## 2. 前置知识

### 2.1 中断：让 CPU「暂停当前工作去救火」

中断（interrupt）是 CPU 的一种输入机制：外部设备把一根专用信号线拉到有效电平，CPU 在执行完当前指令后，**自动**暂停主程序、跳转到一个固定的「中断服务程序（ISR）」入口去处理紧急事件，处理完再跳回主程序被打断的位置继续执行。

要实现这套机制，硬件必须解决四个问题：

1. **识别**：外部信号可能毛刺多、与 CPU 时钟异步，怎么稳定地认出「一次中断请求」？
2. **记忆**：CPU 此刻可能正忙、或者中断被软件关掉了，怎么把「已发生但尚未处理」的请求记住？
3. **许可**：软件有时不希望被打断，怎么提供一个总开关？
4. **响应**：被允许后，怎么「保存现场 → 跳到 ISR → 处理完返回」？

TMS32010（也就是 IKA32010 复刻的对象）的设计回答如下：用 **下降沿触发**（`i_INT_n` 从高变低代表一次请求），用 **锁存位** 记忆，用 **INTM 状态位** 做总开关，用 **固定向量地址 `0x002`** 作 ISR 入口、**4 级硬件栈** 保存返回地址。

### 2.2 下降沿检测：为什么要「同步链 + 延迟一拍比较」

外部信号 `i_INT_n` 与核心时钟 `i_EMUCLK` 是异步的——它可能在任意时刻翻转。如果直接用它去触发逻辑，会撞上 **亚稳态（metastability）**：触发器在采样窗口附近可能输出一个既不是 0 也不是 1 的电压，并需较长时间才能稳定，导致后续逻辑误判。

标准对策是「同步链」：让信号先经过一两级触发器，使其与本地时钟对齐、把亚稳态概率压到极低。再额外留一拍「延迟副本」，把「当前值」与「上一拍值」做比较，就能识别出上升沿或下降沿。

下降沿的布尔表达为「当前值为 0、上一拍值为 1」：

\[
\text{falling} = \neg\,\text{cur} \;\wedge\; \text{prev}
\]

本讲的 `~int_n_zz & int_n_zzz` 正是这个式子。

### 2.3 回顾两个关键节拍

来自 [u1-l4](u1-l4-clock-and-cycle-counter.md)：

- `cyc_ncen`：`cyclecntr==3` 且 `i_CLKIN_PCEN` 为高，是核心的 **主工作拍**。PC、栈、累加器、指令寄存器等几乎所有状态都在这一拍更新。
- `cyc_pcen`：`cyclecntr==1` 且 `i_CLKIN_PCEN` 为高，是辅助拍，在本讲里用于中断同步链的第二级推进。

来自 [u3-l2](u3-l2-multicycle-timing-and-state.md)：

- `if_opcodereg_force_iack`：当它为 `YES` 时，指令寄存器在 `cyclecntr==3` 的边沿被强制刷成内部 IACK 码 `0xF000`，而不是锁存总线取来的真指令。多周期指令在中间相位把它改写成 `NO`，从而保证「指令不被中途打断」的原子性。

本讲的「响应」全靠这两个机制。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 唯一的硬件源文件。中断相关的逻辑分布其中：同步链、锁存、`reg_intm`、PC 跳转、微码里的中断预检查与 IACK 分支。 |
| [src/IKA32010_mnemonics.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv) | 常量字典。本讲用到 `PC_LOAD_INTERRUPT`、`STACK_DATA_PC/ACC`、`YES/NO`。 |
| [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) | 唯一的 testbench。它在仿真中途把 `INT_n` 拉低再抬起来，是观察中断的现成激励。 |

中断逻辑在顶层文件里横跨多个区块，阅读时建议按下面的顺序跳转：端口声明 → PC 跳转分支 → `reg_intm` → 同步链/锁存/请求 → 指令寄存器强制 → 微码预检查与 IACK 分支 → DINT/EINT 指令。

## 4. 核心概念与源码讲解

### 4.1 三级同步链 `int_n_z / int_n_zz / int_n_zzz`

#### 4.1.1 概念说明

外部中断引脚 `i_INT_n`（低有效）进来后，不能直接用。IKA32010 用三个触发器串成一条同步链：

- `int_n_z`：第一级，在主工作拍 `cyc_ncen` 直接采样原始 `i_INT_n`，起 **去亚稳态** 作用。
- `int_n_zz`：第二级，在辅助拍 `cyc_pcen`（即下一个机器周期的相位 1）承接 `int_n_z`，得到与本地时钟对齐的「当前同步值」。
- `int_n_zzz`：第三级，在 `cyc_ncen` 再延迟一拍复制 `int_n_zz`，作为「上一拍同步值」。

随后用第二级与第三级做下降沿比较。这一设计把「同步」与「边沿检测」合在一处，且所有比较都落在 `cyc_ncen` 这一拍，便于与核心的取指/写回节拍对齐。

#### 4.1.2 核心流程

三级同步与判沿都写在一个 `always @(posedge i_EMUCLK)` 块里：

1. 复位时（`i_RS_n==0`）三级全部置 `1`（空闲态），避免复位瞬间被误判成中断。
2. 正常运行：
   - `cyc_ncen`（相位 3）：`int_n_z <= i_INT_n`，同时 `int_n_zzz <= int_n_zz`。
   - `cyc_pcen`（相位 1，下一周期）：`int_n_zz <= int_n_z`。
3. 在每个 `cyc_ncen` 边沿用 **边沿前** 的值判断：`~int_n_zz & int_n_zzz` 为真，即代表检测到一个下降沿，随后置位锁存（见 4.2）。

由于两级都跨相位，从 `i_INT_n` 首次被采低到锁存位置位，约有 **1 个机器周期** 量级的固定延迟。

#### 4.1.3 源码精读

外部中断输入端口声明（低有效输入）：

- [src/IKA32010.sv:25-30](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L25-L30) —— 其中 `input wire i_INT_n` 是外部中断请求线。

三级同步链与判沿逻辑：

```verilog
reg int_n_z, int_n_zz, int_n_zzz, int_latched;
wire int_rq = int_latched & ~reg_intm;
always @(posedge i_EMUCLK) begin
    if(!i_RS_n) begin
        int_n_z <= 1'b1; int_n_zz <= 1'b1; int_n_zzz <= 1'b1;
    end else begin
        if(cyc_ncen) int_n_z   <= i_INT_n;   // 相位3：采原始输入
        if(cyc_pcen) int_n_zz  <= int_n_z;    // 相位1：推进一级
        if(cyc_ncen) int_n_zzz <= int_n_zz;   // 相位3：延迟副本
    end
    ...
```

完整段落见 [src/IKA32010.sv:352-365](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L352-L365)。

`cyc_ncen` / `cyc_pcen` 的定义，说明同步链的三个更新点其实都受 `i_CLKIN_PCEN` 控制：

- [src/IKA32010.sv:55-61](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L55-L61) —— `o_CLKOUT_NCEN = (cyclecntr==3) & i_CLKIN_PCEN`，`o_CLKOUT_PCEN = (cyclecntr==1) & i_CLKIN_PCEN`。

时序举例（设 `i_INT_n` 在某机器周期 0 的相位 3 之前已拉低，并保持低）：

| 观察时刻（相位 3 边沿用边沿前的值） | `int_n_z` | `int_n_zz` | `int_n_zzz` | `~zz & zzz` |
|---|---|---|---|---|
| 复位稳态 | 1 | 1 | 1 | 0 |
| 周期 0 相位 3 之后 | 0 | 1 | 1 | 0 |
| 周期 1 相位 3（判沿） | 0 | 0 | 1 | **1 → 锁存！** |
| 周期 2 相位 3 之后 | 0 | 0 | 0 | 0 |

可见锁存发生在「输入首次被采低」之后约一个机器周期。后续因为 `int_n_zz` 与 `int_n_zzz` 都已变 0，`~zz & zzz` 恒为 0，**只要 `i_INT_n` 一直保持低就不会重复触发**——想再来一次中断，外部设备必须先把它抬回高再拉低。

#### 4.1.4 代码实践

**实践目标**：用肉眼跟踪一条下降沿穿过同步链的全过程，确认「延迟约一个周期」与「保持低不重触发」。

**操作步骤（源码阅读型）**：

1. 打开 [src/IKA32010.sv:355-365](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L355-L365)。
2. 假设 `i_INT_n` 在某相位 3 前由 1 变 0，按上表逐拍填写 `int_n_z/zz/zzz` 的取值。
3. 再假设 `i_INT_n` 这次只低了一个相位就被抬回高（短脉冲）：重画表格，观察是否还能在 `int_n_zz` 上稳定看到 0。

**需要观察的现象**：`int_n_z` 几乎即时跟随输入；`int_n_zz` 滞后它约半个机器周期（一个相位 1）；`int_n_zzz` 又滞后一级。`~int_n_zz & int_n_zzz` 只在两者错位的那一拍为真。

**预期结果**：一个足够宽（至少跨过一个相位 3 到下一个相位 1）的低电平会被识别；过窄的脉冲可能被采不到，这是边沿触发中断的固有特性。**完整仿真验证留待第 5 节综合实践。**

#### 4.1.5 小练习与答案

**练习 1**：把 `int_n_zzz` 这一级删掉、直接用 `~int_n_zz & int_n_z` 判沿，逻辑上似乎也能检测下降沿。源码为什么不用这种写法？

> 答案：`int_n_z` 直接采异步的 `i_INT_n`，可能处于亚稳态或正在翻途中；拿它做比较不可靠。`int_n_zz` 和 `int_n_zzz` 都已经经过同步，且两者都在 `cyc_ncen` 这一拍比较，结果稳定。第三级的本质是「同步后的延迟副本」，不是多余的。

**练习 2**：为什么三级同步的复位值是 `1` 而不是 `0`？

> 答案：`i_INT_n` 空闲态是高（1）。复位时把同步链置 1，相当于让它们等于「没有中断」的稳态，避免复位释放瞬间因初值为 0 而被误判出一个下降沿。

---

### 4.2 锁存位 `int_latched` 与请求线 `int_rq`

#### 4.2.1 概念说明

同步链只负责「认出一次下降沿」，但它不会把这件事记住——下一拍边沿信号就过去了。所以还需要一个 **锁存位 `int_latched`**：一旦检测到下降沿就置 1，并一直保持，直到 CPU 主动应答（`int_ack`）才清 0。

但「锁存了」不等于「立刻去处理」。CPU 是否真的响应，还要看软件有没有把中断关掉。这就是中断请求线：

```verilog
wire int_rq = int_latched & ~reg_intm;
```

- `int_latched`：有未被应答的中断事件。
- `reg_intm`：中断屏蔽位（1=禁用，0=启用），由 DINT/EINT 指令控制（见 4.3）。

只有「有事件」**且** 「未屏蔽」时，`int_rq` 才为真，下游微码才会真正进入响应流程。

#### 4.2.2 核心流程

1. 下降沿被检测到（`~int_n_zz & int_n_zzz`）→ `int_latched <= 1`。
2. `int_latched` 保持 1，期间 `int_rq` 是否有效完全由 `reg_intm` 决定：
   - 软件执行 `EINT`（`reg_intm=0`）→ `int_rq` 立刻跟随 `int_latched` 变有效。
   - 软件执行 `DINT`（`reg_intm=1`）→ 即使 `int_latched=1`，`int_rq` 也被压为 0，中断被挂起但不响应。
3. 中断被响应后，内部 IACK 指令（见 4.4）把 `int_ack` 拉高 → `int_latched <= 0`，一次请求结清。

注意：`int_latched` 只会被 **下降沿** 置位、只会被 **应答** 清零，与 `i_INT_n` 的电平高低不再直接相关。这是典型的「边沿触发 + 应答清锁存」模型。

#### 4.2.3 源码精读

锁存位与请求线的完整代码：

```verilog
reg int_ack;
reg int_n_z, int_n_zz, int_n_zzz, int_latched;
wire int_rq = int_latched & ~reg_intm;            // 请求 = 有事件 且 未屏蔽
always @(posedge i_EMUCLK) begin
    ...
    if(!i_RS_n) int_latched <= 1'b0;
    else begin if(cyc_ncen) begin
        if(int_ack) int_latched <= 1'b0;          // 应答清锁存
        else begin
            if(~int_n_zz & int_n_zzz) int_latched <= 1'b1;  // 下降沿置位
        end
    end end
end
```

见 [src/IKA32010.sv:352-374](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L352-L374)。要点：

- `int_rq` 是 **组合 wire**（[L354](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L354)），无延迟地反映「锁存 ∧ 未屏蔽」。
- `int_latched` 的置位条件（[L371](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L371)）与清零条件（[L369](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L369)）互斥地写在一个 `if/else` 里，避免冲突。
- `int_ack` 默认值是 `1'b0`（[L593](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L593)），只有 IACK 指令分支里才会改写成 `YES`。

#### 4.2.4 代码实践

**实践目标**：在源码里把「事件层（`int_latched`）」与「许可层（`int_rq`）」清晰地分离开来观察。

**操作步骤（源码阅读型）**：

1. 在 [src/IKA32010.sv:352-374](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L352-L374) 找到 `int_latched` 的所有驱动点，确认它 **从不直接看 `reg_intm`**。
2. 再到 [L354](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L354) 看 `int_rq` 是在哪里把 `reg_intm` 引入的。

**预期结果**：你会得到一张分层视图——同步链 → `int_latched`（记事件）→ `int_rq`（看许可）→ 微码（看 `int_rq` 决定是否响应）。这种分层让「关中断」只是临时挂起请求，而不是丢失事件。

#### 4.2.5 小练习与答案

**练习 1**：假设 `int_latched=1` 时软件执行 `DINT`（`reg_intm` 变 1），随后又执行 `EINT`（`reg_intm` 变 0）。这期间 `int_rq` 如何变化？中断会丢失吗？

> 答案：`DINT` 后 `int_rq=0`，中断被挂起但不响应；`EINT` 后 `int_rq` 立刻随 `int_latched` 回到 1，于是被响应。只要 `int_ack` 没发生，`int_latched` 就保持 1，事件不会丢失。

**练习 2**：`int_ack` 与 `~int_n_zz & int_n_zzz` 写在同一个 `if/else` 里。如果某拍二者同时为真，会发生什么？

> 答案：`if(int_ack)` 优先，`int_latched <= 0`。即「应答」优先于「新边沿」。不过实际上应答发生在 IACK 那拍，而新下降沿若刚到，往往要到下一拍才被比较，二者很少真正撞在同一拍；源码这样写是为了在万一冲突时让应答确定性地胜出。

---

### 4.3 中断使能 `reg_intm` 与 DINT / EINT

#### 4.3.1 概念说明

`reg_intm`（interrupt mask）是 1 位状态寄存器，扮演中断总开关：

- `reg_intm == 0`：中断启用（enable），`int_rq` 可以为真。
- `reg_intm == 1`：中断禁用（disable），`int_rq` 被强制为 0。

它通过两条「置位/复位」风格的微码信号改写：

- `reg_intm_dis = YES` → `reg_intm <= 1`（DINT 指令）。
- `reg_intm_en = YES` → `reg_intm <= 0`（EINT 指令）。

复位后 `reg_intm = 1`（默认关中断），因此上电后主程序必须显式执行一次 `EINT` 才会开始接受中断。

`reg_intm` 还是 **状态寄存器的一位**，可被 LST 加载、被 SST/SSR 读出（见 [u3-l4](u3-l4-control-and-aux-instructions.md)），便于在 ISR 里成对地 `DINT`/`EINT` 保护临界区。

> 说明：在 IKA32010 的实现里，**进入中断响应（IACK）时并不会自动把 `reg_intm` 置 1**——4.4 节的 IACK 分支里没有 `reg_intm_dis`。也就是说，若不在 ISR 开头手动 `DINT`，理论上允许嵌套中断。这一点是否与原始 TMS32010 的「应答时自动关中断」一致，**待对照 `docs/TMS32010_Users_Guide_1985.pdf` 确认**；至少源码本身是「软件负责管理 INTM」。

#### 4.3.2 核心流程

`reg_intm` 的更新逻辑用一张 2 输入真值表实现（`{en, dis}` 为选择码）：

| `{reg_intm_en, reg_intm_dis}` | 动作 | `reg_intm` 次态 |
|---|---|---|
| 00 | 不变 | 保持 |
| 01 | 禁用 | 1 |
| 10 | 启用 | 0 |
| 11 | 冲突，保持 | 保持 |

只在 `cyc_ncen` 这一拍更新，复位强制为 1。

#### 4.3.3 源码精读

`reg_intm` 寄存器与更新真值表：

```verilog
//interrupt enable bit
reg reg_intm; //0 = interrupt enabled, 1 = interrupt disabled
reg reg_intm_en, reg_intm_dis;
always @(posedge i_EMUCLK) begin
    if(!i_RS_n) reg_intm <= 1'b1;          // 复位：关中断
    else begin if(cyc_ncen) begin
        case({reg_intm_en, reg_intm_dis})
            2'b00: reg_intm <= reg_intm;
            2'b01: reg_intm <= 1'b1;        // DINT
            2'b10: reg_intm <= 1'b0;        // EINT
            2'b11: reg_intm <= reg_intm;
        endcase
    end end
end
```

见 [src/IKA32010.sv:273-286](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L273-L286)。

`reg_intm` 在状态寄存器 `flag_output` 中的位置（bit 13）：

- [src/IKA32010.sv:479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479) —— `flag_output = {alu_flag_ovfl, reg_ovm, reg_intm, 4'b1111, reg_arp, ...}`，故 LST/SSR 可整体读写它。

DINT / EINT 指令的微码分支：

```verilog
//DINT
16'b0111_1111_1000_0001: begin
    reg_intm_dis = YES;            // 关中断
    ...
end
//EINT
16'b0111_1111_1000_0010: begin
    reg_intm_en = YES;             // 开中断
    ...
end
```

见 [src/IKA32010.sv:645-661](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L645-L661)。两条指令除了改这一个信号外，其余都沿用默认值（取指、PC 自增），是典型的「单周期、只拨一个开关」的控制类指令。

#### 4.3.4 代码实践

**实践目标**：确认 `reg_intm` 的初值、改写途径，以及它对 `int_rq` 的门控关系。

**操作步骤（源码阅读型）**：

1. 在 [L277](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L277) 确认复位值是 1。
2. 全文搜索 `reg_intm_en` 与 `reg_intm_dis`，确认它们 **只在 DINT/EINT 分支被置 YES**，其余都靠默认 NO。
3. 回到 [L354](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L354)，把 `reg_intm` 代入 0/1，看 `int_rq` 如何被门控。

**预期结果**：你会看到 `reg_intm` 完全由 DINT/EINT 两个指令字驱动，与中断硬件其他部分没有任何反馈——它就是一个纯粹的、软件可见的开关位。

#### 4.3.5 小练习与答案

**练习 1**：主程序复位后立刻在地址 0 写一段循环，却没有 `EINT`，外部反复拉低 `i_INT_n`。会发生什么？

> 答案：`reg_intm` 一直为 1，`int_rq` 恒为 0，CPU 永远不响应。但 `int_latched` 仍会在第一个下降沿被置 1 并一直保持——若之后执行 `EINT`，这个挂起的中断会立刻被响应。

**练习 2**：ISR 里想防止自己被打断，应该插入哪条指令？返回前又该插哪条？

> 答案：入口处 `DINT`（`reg_intm_dis=YES`），出口处、`RET` 之前 `EINT`（`reg_intm_en=YES`）。或者用 LST/SSR 整体保存/恢复状态寄存器，把 INTM 位一起管理起来。

---

### 4.4 内部 IACK 指令与中断响应全流程

#### 4.4.1 概念说明

到上一节为止，`int_rq` 已经能正确地变成 1。剩下的核心问题是：微码怎么把它转化成「跳转到 `0x002` + 压栈 + 应答」这一连串动作？

IKA32010 的设计非常巧妙：它 **不专设一套独立的中断控制线**，而是复用已有的指令译码通路，做法分两步：

1. **预检查（在 casez 之前）**：每个机器周期、译码之前，先看 `int_rq`。若有效，就把本该译码的真指令「拦截」——具体说，是把这些信号改写成中断响应所需的样子：PC 改为 `PC_LOAD_INTERRUPT`、栈改为 `push` 当前 PC、指令寄存器改为强制刷成 IACK。
2. **IACK 是一条「凭空捏造」的内部指令**：当指令寄存器被刷成 `0xF000` 后，casez 会命中第一个分支 `16'b1111_0000_0000_0000`，这条「指令」负责完成应答（拉 `int_ack`）并恢复正常的取指。

这样做的好处是：中断响应被自然地塞进了「一条指令」的时间槽里，无需额外状态，也无需改动主数据通路。

#### 4.4.2 核心流程

把 [u3-l2](u3-l2-multicycle-timing-and-state.md) 的「指令寄存器每拍锁存、PC 比指令提前一拍」记在心里，一次完整中断响应的时序如下（记被 `int_rq` 命中的那一拍为周期 T，此时 `if_opcodereg` = 主程序指令@A，`if_pc` = A+1）：

1. **周期 T（命中）**：组合微码的预检查发现 `int_rq=1`，于是
   - `if_pc_modesel = PC_LOAD_INTERRUPT`
   - `stk_push = YES`，`stk_data_sel = STACK_DATA_PC`
   - `if_opcodereg_force_iack = YES`

   这些值在该拍的相位 3 边沿生效：
   - 指令@A 的写回正常提交（它不会被打断半截）。
   - 栈顶 `stack[0]` 收到 `if_pc` = **A+1**（返回地址）。
   - `if_pc <= 0x002`。
   - `if_opcodereg <= 0xF000`（IACK），**而不是** 这一拍取来的指令@A+1——也就是说，指令@A+1 被推迟到 RET 之后再执行。

2. **周期 T+1（执行 IACK）**：`if_opcodereg = 0xF000`，casez 命中 IACK 分支，它 **覆盖** 了预检查设的那些值：
   - `busctrl_req = OPCODE_READ`（从 `0x002` 取 ISR 第一条指令）。
   - `if_pc_modesel = PC_INCREASE`（`if_pc` 将从 `0x002` 变 `0x003`）。
   - `if_opcodereg_force_iack = NO`（解除强制，下一拍恢复正常取指）。
   - `int_ack = (int_rq) ? YES : NO`（应答）。

   该拍相位 3 边沿：
   - `int_latched <= 0`（请求结清）。
   - `if_opcodereg` 锁存 ISR 第一条指令。
   - `if_pc <= 0x003`。

3. **周期 T+2 起**：CPU 正常执行 ISR，直到 `RET`。`RET` 经 `WRBUS_SOURCE_STACK` + `PC_LOAD_WRBUS` 把栈顶 A+1 弹回 PC，主程序在 A+1 处无缝续上。

> 与多周期指令的关系（承接 [u3-l2](u3-l2-multicycle-timing-and-state.md)）：多周期指令（如 TBLR、IN）在中间相位会把 `if_opcodereg_force_iack` 强制写 `NO`，从而 **覆盖** 预检查里 `(int_rq)?YES:NO` 的 YES。因此即便 `int_rq=1`，中断也会被推迟到该多周期指令的最后一拍才被接受——这就是「指令不被中途打断」的原子性来源。

#### 4.4.3 源码精读

**(a) PC 跳转分支** —— 中断向量地址的来源：

```verilog
case(if_pc_modesel)
    PC_HOLD           : if_pc <= if_pc;
    PC_INCREASE       : if_pc <= if_pc_next;
    PC_LOAD_IMMEDIATE : if_pc <= i_DIN[11:0];
    PC_LOAD_INTERRUPT : if_pc <= 12'h002;   // 中断向量
    PC_LOAD_WRBUS     : if_pc <= reg_wrbus[11:0];
    PC_RESET          : if_pc <= 12'h000;
    ...
endcase
```

见 [src/IKA32010.sv:102-114](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L102-L114)，关键行 [L109](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L109)。`PC_LOAD_INTERRUPT = 3'd3` 定义在 [IKA32010_mnemonics.sv:5](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L5)。

**(b) 指令寄存器的强制刷写** —— 这是把「真指令」换成「IACK」的机关：

```verilog
if(cyclecntr == 2'd3) begin
    if(if_opcodereg_force_iack) if_opcodereg <= 16'hF000;   // 强制 IACK
    else begin
        if(busctrl_mode[2:0] == 3'd1) if_opcodereg <= i_DIN; // 否则锁存取来的指令
    end
end
```

见 [src/IKA32010.sv:182-187](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L182-L187)，关键行 [L184](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L184)。注意它同时被 `i_CLKIN_PCEN` 与 `cyclecntr==3` 双重门控（[L182](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L182)），与 PC、栈的更新对齐到同一个相位 3 边沿。

**(c) 微码里的预检查** —— 这是「每个周期先看一眼中断」的地方，位于正常态入口、casez 之前：

```verilog
else begin
    //interrupt check
    if_opcodereg_force_iack = (int_rq) ? YES : NO;
    if_pc_modesel           = (int_rq) ? PC_LOAD_INTERRUPT : PC_INCREASE;
    stk_push                = (int_rq) ? YES : NO;
    stk_data_sel            = (int_rq) ? STACK_DATA_PC : STACK_DATA_ACC;

    casez(if_opcodereg)
        ...
```

见 [src/IKA32010.sv:611-618](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L611-L618)。`int_rq` 有效时，这四行把本周期重定向为「跳 `0x002` + 压返回地址 + 下拍注入 IACK」。注意复位态分支（`ex_state==0`）走的是另一条路（停总线、PC 复位，见 [L600-609](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L600-L609)），不经过这段预检查。

**(d) IACK 内部指令分支** —— casez 的第一个分支，负责应答并恢复正常取指：

```verilog
//internal special instruction IACK
16'b1111_0000_0000_0000: begin
    busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    if_pc_modesel = PC_INCREASE;

    //acknowledge interrupt
    if_opcodereg_force_iack = NO;
    int_ack = (int_rq) ? YES : NO;
    stk_push = NO; stk_data_sel = STACK_DATA_ACC;
    `ifdef IKA32010_DISASSEMBLY
        if(int_rq) $display("IKA32010_", `IKA32010_DEVICE_ID, ": IRQ RECEIVED\n");
    `endif
end
```

见 [src/IKA32010.sv:624-636](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L624-L636)。几个细节：

- 它出现在 casez 最前面，会 **覆盖** 预检查里关于 `if_pc_modesel/stk_push/force_iack` 的设置——所以 IACK 那拍不再跳转、不再压栈、解除强制。
- `int_ack` 用 `(int_rq) ? YES : NO` 做条件：只有在确有请求时才真应答。复位延迟期间指令寄存器也可能被刷成 `0xF000`（因默认 `force_iack=YES`），那时 `int_rq=0`，于是 `int_ack=NO`，不会误清锁存、也不会误打印 `IRQ RECEIVED`。
- `DEVICE_ID` 宏便于在多片 DSP 系统里区分是谁收到的中断（见 [u3-l9](u3-l9-fpga-synthesis-and-integration.md)）。

**(e) 默认值** —— `int_ack` 默认 `1'b0`、`if_opcodereg_force_iack` 默认 `YES`：

- [src/IKA32010.sv:549](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L549) —— `if_opcodereg_force_iack = YES; //flush!`（复位态下保持「冲刷」）。
- [src/IKA32010.sv:593](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L593) —— `int_ack = 1'b0;`

#### 4.4.4 代码实践

**实践目标**：用一句话讲清 IACK 这条「假指令」做了哪四件事。

**操作步骤（源码阅读型）**：

1. 打开 [src/IKA32010.sv:624-636](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L624-L636)。
2. 逐行列出 IACK 分支设置的信号，并标注它是 **「恢复常态」**（如 `OPCODE_READ`、`PC_INCREASE`、`force_iack=NO`、`stk_push=NO`）还是 **「做应答」**（`int_ack=YES`）。
3. 解释：为什么 IACK 必须排在 casez 的第一个分支？

**预期结果**：IACK = ①恢复正常取指 ②PC 改回自增 ③解除强制刷写 ④发出应答清锁存。排在第一个是为了让被 `0xF000` 占据的指令寄存器立刻被识别，而不会先被某个真指令的位模式（带通配符的）分支误匹配。

#### 4.4.5 小练习与答案

**练习 1**：周期 T 里，指令@A 的微码（比如 ADD）和中断预检查都给 `if_pc_modesel` 赋了值，最后以哪个为准？

> 答案：预检查在 casez **之前** 赋值，ADD 分支在 casez **之内**。由于微码用阻塞赋值 `=` 顺序执行，后执行的 ADD 分支会覆盖预检查的值。但 ADD 这类单周期指令并不改 `if_pc_modesel`，所以它最终仍保持预检查设的 `PC_LOAD_INTERRUPT`。结论：单周期指令执行其算术效果，同时中断跳转生效——指令@A 完成、PC 跳 `0x002`、栈压 A+1。

**练习 2**：如果 ISR 里忘记写 `RET`，会发生什么？

> 答案：CPU 会从 `0x002` 一路顺序执行下去，永远不把 A+1 弹回 PC，主程序被打断后无法返回——这是软件 bug，硬件不负责挽救。栈顶那个 A+1 会一直留在那儿直到被下一次 push 覆盖。

**练习 3**：为什么 IACK 分支里 `int_ack` 要写成 `(int_rq) ? YES : NO` 而不是直接 `YES`？

> 答案：指令寄存器变为 `0xF000` 的途径不只有「真中断」一条——复位延迟期默认 `force_iack=YES` 也会让它变 `0xF000`。用 `int_rq` 做条件，确保只在确有挂起请求时才应答清锁存，复位期的「假 IACK」不会误清 `int_latched`、也不会误触发 `IRQ RECEIVED` 打印。

## 5. 综合实践

**任务**：追踪一次完整中断过程，画出从 `i_INT_n` 拉低到 `int_latched` 清零的端到端时序，并标注「跳 `0x002`」「压栈」「IACK 应答」三件事各发生在哪一拍。

现成的 testbench 已经在仿真中途发了一次中断（[src/IKA32010_tb.v:22-23](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L22-L23)）：

```verilog
#2420 INT_n <= 1'b0;
#63   INT_n <= 1'b1;
```

它把 `INT_n` 拉低 63 个时间单位后又抬起来，正是一次「下降沿 + 上升沿」。注意 testbench 还用 `divider` 把 `i_CLKIN_PCEN` 做成了 1/4 占空比的窄脉冲（见 [u1-l5](u1-l5-simulation-and-testbench.md)），所以一个机器周期在仿真里被拉长到 16 个 `EMUCLK`。

**推荐做法（两者任选其一）**：

**A. 仿真验证型（需要可用的 ROM 文件）**

1. 用 `+define+IKA32010_DISASSEMBLY` 启用反汇编，编译运行 testbench。
2. 在 `always @(posedge i_EMUCLK)` 里临时加一句探针（**示例代码**，仅用于观察，不要提交）：

   ```verilog
   // 示例代码：仿真观察用
   always @(posedge i_EMUCLK) if(cyc_ncen)
       $display("t=%0t pc=%h op=%h latched=%b rq=%b intm=%b",
                $time, if_pc, if_opcodereg, int_latched, int_rq, reg_intm);
   ```

3. 在 `INT_n` 拉低前，确认主程序里已经执行过 `EINT`（否则 `reg_intm=1`、`int_rq` 一直为 0，看不到响应）。testbench 加载的是街机 ROM，是否含 `EINT` **待本地验证**；若没有，可自备一段最小 ROM（在地址 0 放 `EINT`、地址 2 放 ISR、其间填 `NOP`）。
4. 观察打印：应能看到 `latched` 在某拍从 0 跳 1；之后某拍 `op` 变成 `f000`（IACK）且打印 `IRQ RECEIVED`；`pc` 在那一拍前后出现 `002`；`latched` 在 IACK 拍后回 0。

**B. 纯源码阅读型（无需运行仿真）**

1. 锁定四个代码点：同步链/锁存（[L352-374](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L352-L374)）、PC 跳转（[L109](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L109)）、指令寄存器强制（[L184](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L184)）、预检查与 IACK（[L611-636](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L611-L636)）。
2. 在纸上按 4.4.2 的「周期 T → T+1 → T+2」推进，画一张表，列出每个相位 3 边沿后 `int_n_zz / int_n_zzz / int_latched / int_rq / if_pc / if_opcodereg / stack[0]` 的取值。
3. 在表上标出三件事的发生拍：① `if_pc` 首次变为 `002`（跳向量）；② `stack[0]` 首次变为返回地址（压栈）；③ `int_latched` 从 1 回 0（IACK 应答）。

**预期结果**：三件事分别落在「命中拍 T」「命中拍 T（与跳转同拍）」「IACK 拍 T+1」。整体上从 `i_INT_n` 下降到锁存清零，约耗费 2 个机器周期（同步链 1 拍 + 响应 1 拍）。**精确到拍数的波形待本地仿真验证。**

## 6. 本讲小结

- IKA32010 的中断是 **边沿触发**：`i_INT_n` 的下降沿被 `int_n_z → int_n_zz → int_n_zzz` 三级同步链稳定识别，式子 `~int_n_zz & int_n_zzz` 在主工作拍 `cyc_ncen` 上判沿。
- 事件被记入锁存位 `int_latched`：下降沿置位、应答 `int_ack` 清零，与输入电平不再相关；保持低电平不会重复触发。
- 请求线 `int_rq = int_latched & ~reg_intm` 把「事件」与「许可」分层；`reg_intm` 是由 DINT/EINT 软件控制的总开关，复位默认关中断（=1）。
- 响应过程复用指令译码通路：微码在 casez 之前做 `int_rq` 预检查，命中时把 PC 重定向到向量 `0x002`、把返回地址压栈、把指令寄存器强制刷成内部 IACK 码 `0xF000`。
- IACK 是 casez 第一个分支，负责「恢复正常取指 + PC 自增 + 解除强制 + 拉高 `int_ack` 清锁存」四件事；`int_ack` 用 `int_rq` 做条件，避免复位延迟期的假 IACK 误清锁存。
- 多周期指令在中间相位把 `if_opcodereg_force_iack` 强制写 `NO`，使中断被推迟到指令完成之后——这就是 [u3-l2](u3-l2-multicycle-timing-and-state.md) 所说「指令不被中途打断」的原子性。

## 7. 下一步学习建议

- 继续 [u3-l4（控制类与辅助寄存器类指令译码）](u3-l4-control-and-aux-instructions.md)：那里会精读 LST/SSR 对状态寄存器（含 `reg_intm` 这一位）的整体读写，本讲的 INTM 正是其中一位。
- 阅读 [u3-l6（分支与子程序类指令）](u3-l6-branch-and-subroutine-instructions.md)：RET 怎样经 `WRBUS_SOURCE_STACK` + `PC_LOAD_WRBUS` 把本讲压入的返回地址弹回 PC，二者合起来才是完整的「中断返回」。
- 对照 `docs/TMS32010_Users_Guide_1985.pdf` 的中断章节，核实「IACK 是否应自动置 INTM=1」「向量地址」等与本讲实现是否一致，把本讲标注的 **待确认** 项补全。
- 进阶到 [u3-l9（FPGA 综合与系统集成）](u3-l9-fpga-synthesis-and-integration.md)：在多片 DSP 系统里，用 `DEVICE_ID` 区分 `IRQ RECEIVED` 来自哪一颗，是本讲 IACK 分支里那个宏的实际用途。
