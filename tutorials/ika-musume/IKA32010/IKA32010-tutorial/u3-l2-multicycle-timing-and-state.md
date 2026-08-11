# 多周期指令时序与状态机

## 1. 本讲目标

在上一篇（u3-l1）里，我们把那个近 1219 行的组合 `always @(*)` 块当作「微码存储器」来看，建立了「默认值 + `casez` 覆盖」的整体印象，但当时刻意回避了一个问题：**为什么有些指令要占用不止一个机器周期？处理器又是怎么把一条指令拆到多个周期里执行的？**

本讲就来回答它。读完本讲，你应当能够：

1. 说清 `ex_inst_cycle` 这个 2 位计数器如何为多周期指令「分相位」，以及 `ex_inst_cycle_rst` 为什么默认是 `YES`。
2. 看懂复位释放延迟链 `rs_n_z` / `rs_n_zz` 如何对 `i_RS_n` 做两级同步与上升沿检测。
3. 解释 `ex_state` 状态机在「复位态」与「正常态」之间的转移条件，以及复位期间微码做了什么。
4. 拿到一条具体的多周期指令（如 `TBLR`、`IN`），能逐相位列出 `busctrl_req`、`if_pc_modesel`、栈操作、`ram_wr` 的取值，并解释数据如何流动。

本讲是专家层的第二篇，承接 u3-l1 的微码骨架，向下打通「一条指令跨多个机器周期」的执行细节，为后续 u3-l3（中断机制）、u3-l6（分支与子程序）、u3-l7（I/O 与表类指令）提供时序基础。

## 2. 前置知识

本讲假设你已经读过以下两篇：

- **u1-l4 时钟分频与周期计数器**：知道 `cyclecntr` 在 `0→1→2→3` 循环，4 个 `i_EMUCLK` 构成一个 DSP 机器周期；`cyc_ncen`（`cyclecntr==3`）是主工作拍，几乎所有寄存器都在这一拍更新。
- **u3-l1 微码架构总览**：知道顶层那个大 `always @(*)` 块的求值顺序是「默认值 → 复位态判断 → 中断预检查 → `casez` 译码」，以及 `if_opcodereg` 是当前正在译码的指令寄存器。

在此之上，本讲引入几个新术语：

| 术语 | 含义 |
|------|------|
| **机器周期（machine cycle）** | 4 个 `i_EMUCLK`，即 `cyclecntr` 走完一圈；是 IKA32010 的基本时间单位 |
| **指令周期（instruction cycle）** | 一条指令占用的机器周期数；多数指令为 1，分支/调用/返回/压栈弹栈为 2，表读写为 3 |
| **相位（phase）** | 多周期指令内部的第几个机器周期，由 `ex_inst_cycle` 编号（0、1、2） |
| **单周期指令** | 不依赖 `ex_inst_cycle`、一条机器周期内完成的指令（如 `NOP`、`ADD`） |
| **多周期指令** | 内部按 `ex_inst_cycle` 分相位、占用多个机器周期的指令（如 `IN`、`TBLR`） |
| **原子性（atomic）** | 多周期指令在执行期间「拒绝中断」，等价于不可分割的一次操作 |

一个直觉性的比喻：单周期指令像「一口吞」的快餐，`ex_inst_cycle` 永远停在 0；多周期指令像「分三道菜上的正餐」，每上一道菜 `ex_inst_cycle` 走一格，走完复位回 0，迎接下一位客人（下一条指令）。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| `src/IKA32010.sv` | 顶层模块。本讲关注的代码全在这里：时钟与 `cyc_ncen`（L48–L61）、指令周期计数器（L511–L515）、复位延迟链（L518–L525）、状态机（L528–L533）、微码 `always @(*)` 块的默认值区（L537–L598）、复位态分支（L600–L609）、中断预检查（L611–L617）、各多周期指令的 `casez` 分支（`POP`/`PUSH` L690–L732、`B` L1194–L1211、`IN`/`OUT` L1604–L1661、`TBLR`/`TBLW` L1663–L1743） |
| `src/IKA32010_mnemonics.sv` | 常量字典。本讲引用其中的 `PC_*`（PC 模式）、`BUSCTRL_*`/`OPCODE_READ` 等（总线事务类型）、`YES`/`NO` |

不需要读 testbench，但本讲的综合实践会建议你在仿真器里观察波形，相关环境搭建已在 u1-l5 讲过。

## 4. 核心概念与源码讲解

### 4.1 多周期执行机制：ex_inst_cycle 与 ex_inst_cycle_rst

> 本模块对应最小模块：**ex_inst_cycle / ex_inst_cycle_rst**。

#### 4.1.1 概念说明

先回答一个关键问题：**IKA32010 的大多数指令真的是单周期吗？**

是的。看 `casez` 译码表就会发现，绝大多数指令（`NOP`、`ADD`、`LAC`、`MPY`……）的分支体里**根本不出现 `ex_inst_cycle` 这个名字**——它们假定自己永远在第 0 相位执行，一条机器周期就完事。这与原始 TMS32010 的设计一致：它是早期定点 DSP，强调单周期乘加，算术逻辑指令基本都在一个机器周期内完成。

但有几类指令天然无法一个周期做完，因为它们要在**同一条指令内发起多次外部总线事务**，或需要「先存后取」的配合：

- **分支 / 调用 / 返回**（`B`、`BANZ`、`CALL`、`RET`……）：要先装入新 PC，再去新地址取指，至少 2 周期。
- **压栈 / 弹栈**（`PUSH`、`POP`）：要把数据搬进/搬出 4 级硬件栈，2 周期。
- **I/O**（`IN`、`OUT`）：要先发起外设端口读/写事务，再把数据落盘到 RAM 或从 RAM 取出，2 周期。
- **表读写**（`TBLR`、`TBLW`）：要在程序空间与数据 RAM 之间搬运一个字，且要「借 PC 当地址指针、用栈保存返回地址」，3 周期。

IKA32010 的做法是：给每条指令配一个**指令周期计数器 `ex_inst_cycle`**，多周期指令用它区分「现在是第几相位」，每个相位执行不同的一组微码动作。这就是本模块的核心。

#### 4.1.2 核心流程

`ex_inst_cycle` 是一个 2 位寄存器（取值 0/1/2/3），它的工作流程可以概括成下面这个伪代码：

```
每个 cyc_ncen（机器周期最后一拍）上升沿：
    if (ex_inst_cycle_rst == YES)
        ex_inst_cycle <= 0          # 复位：下一条指令从第 0 相位开始
    else
        ex_inst_cycle <= ex_inst_cycle + 1   # 继续：进入下一相位
```

关键在于 `ex_inst_cycle_rst` 这个**由微码驱动的控制信号**：

- 它的**默认值是 `YES`**（在微码默认值区设定）。
- 单周期指令不去碰它 → 它保持 `YES` → 计数器每个机器周期都复位回 0 → 永远只看到相位 0。
- 多周期指令在自己的前几个相位里把它**显式改写成 `NO`** → 计数器继续递增 → 进入相位 1、相位 2……
- 多周期指令的**最后一个相位不去碰它**（让它回到默认 `YES`）→ 计数器在最后一拍复位 → 这条指令结束，下一条从相位 0 开始。

这是一套非常优雅的「自终止」机制：多周期指令只需在前几个相位写 `ex_inst_cycle_rst = NO`，最后一个相位置之不理，计数器就会自动归零，无需手写「结束」逻辑。

与此配套的还有两个要点，本模块先点出、后文详述：

1. **保持指令寄存器**：多周期指令在每个相位都把 `if_opcodereg_force_iack` 显式置 `NO`，目的是不让指令寄存器被刷新，使同一个 `casez` 分支能在连续多个机器周期里反复命中。详见 4.1.3。
2. **拒绝中断**：同一句 `if_opcodereg_force_iack = NO` 还附带「原子性」效果——中断无法在多周期指令中间插入。详见 4.2。

#### 4.1.3 源码精读

**计数器本体**只有两行，是本讲最短的精华：

[文件 src/IKA32010.sv:511-515](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L511-L515) —— `ex_inst_cycle` 是 2 位寄存器，仅在 `cyc_ncen`（`cyclecntr==3`）上升沿更新；`ex_inst_cycle_rst` 为真则归零，否则加一。

```systemverilog
reg     [1:0]   ex_inst_cycle;
reg             ex_inst_cycle_rst;
always @(posedge i_EMUCLK) if(cyc_ncen) begin
    ex_inst_cycle <= (ex_inst_cycle_rst) ? 2'd0 : ex_inst_cycle + 2'd1;
end
```

注意它是 `posedge i_EMUCLK` + `if(cyc_ncen)`，而不是直接 `posedge cyc_ncen`——这是全项目一致的写法（见 u1-l4），把所有跨周期的状态更新统一挂在 `i_EMUCLK` 上、用 `cyc_ncen` 当门控。所以「一个机器周期」对应 `ex_inst_cycle` 的一次更新。

**默认值**在微码块顶部设定，与 u3-l1 介绍过的其它默认值并列：

[文件 src/IKA32010.sv:545-549](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L545-L549) —— 默认「复位计数器」+「冲刷指令寄存器」，这两条默认值是单周期指令能「零赋值」工作的前提。

```systemverilog
//reset instruction cycle?
ex_inst_cycle_rst = YES;

//force next opcode nop?
if_opcodereg_force_iack = YES; //flush!
```

这里出现了一个本讲反复打交道的信号 `if_opcodereg_force_iack`，名字容易误导，需要先澄清它的真实语义。看它生效的地方：

[文件 src/IKA32010.sv:183-188](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L183-L188) —— 在 `cyclecntr==3` 拍，若 `if_opcodereg_force_iack` 为真，指令寄存器被强制写成内部 IACK 码 `0xF000`；否则只有在「指令读」事务时才用 `i_DIN` 覆盖，其余情况保持不变。

```systemverilog
if(cyclecntr == 2'd3) begin
    if(if_opcodereg_force_iack) if_opcodereg <= 16'hF000;
    else begin
        if(busctrl_mode[2:0] == 3'd1) if_opcodereg <= i_DIN;
    end
end
```

由此可以推出 `if_opcodereg_force_iack` 的三态语义：

| `if_opcodereg_force_iack` | 总线事务 | 下一拍的 `if_opcodereg` |
|---|---|---|
| `YES` | 任意 | 被强制写成 `0xF000`（内部 IACK） |
| `NO` | `OPCODE_READ`（指令读） | 取进下一条指令 `i_DIN` |
| `NO` | 其它（表读/表写/IN/OUT/STOP） | **保持原值不变** |

第三行是多周期指令的关键：当一个相位既把 `if_opcodereg_force_iack` 置 `NO`、又把 `busctrl_req` 设成非指令读事务时，指令寄存器就**原封不动地保留**当前多周期指令的操作码。于是下一个机器周期 `casez` 仍命中同一条指令的同一个分支，只是 `ex_inst_cycle` 已经加一，进入下一个相位。

> 关于默认值 `YES`：它看似会让指令寄存器每个周期都被冲成 IACK。但实际上，在「正常态」入口处，它会被一句覆盖语句重新赋值——见 4.2.3。默认 `YES` 主要对「复位态」与「非法指令兜底」起兜底冲刷作用，是一种安全网。

**单周期指令 vs 多周期指令的对比**，最直观的例子是 `NOP` 与 `POP`：

[文件 src/IKA32010.sv:638-643](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L638-L643) —— `NOP` 的分支体只有一个反汇编打印，完全不碰 `ex_inst_cycle`，依赖默认值在 1 周期内完成。

```systemverilog
//NOP
16'b0111_1111_1000_0000: begin
    `ifdef IKA32010_DISASSEMBLY
        disasm_type0("NOP", if_pc);
    `endif
end
```

而 `POP` 则把动作拆到两个相位：

[文件 src/IKA32010.sv:690-711](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L690-L711) —— `POP` 在相位 0 弹栈到累加器并把 `ex_inst_cycle_rst` 改成 `NO`；相位 1 什么都不做，让默认值把计数器复位回 0。

```systemverilog
//POP - POP stack to accumulator
16'b0111_1111_1001_1101: begin
    if(ex_inst_cycle == 2'd0) begin
        ...                       //弹栈、写累加器
        ex_inst_cycle_rst = NO;   //别复位，进入相位 1
        if_opcodereg_force_iack = NO;  //保持指令寄存器
        stk_push = NO; stk_pop = YES; ...
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = OPCODE_READ; ...  //相位 1：恢复取指，计数器自动复位
    end
    ...
end
```

注意 `POP` 的相位 1 里**没有再写 `ex_inst_cycle_rst`**——它回到默认 `YES`，于是这一拍结束后计数器归零，`POP` 收尾。这正是「自终止」机制的体现。

#### 4.1.4 代码实践

**实践目标**：亲手验证「单周期指令永远停在相位 0，多周期指令会让 `ex_inst_cycle` 走起来」。

**操作步骤（源码阅读型，无需运行）**：

1. 在 `src/IKA32010.sv` 里用搜索定位 `ex_inst_cycle_rst = NO`，统计它出现在多少个分支里。预期：每出现一次，就意味着某条多周期指令多了一个「非终结相位」。
2. 对每条这样的指令，数它有几个 `if(ex_inst_cycle == ...)` 子分支，加 1 即是该指令的总周期数。
3. 把结果填进下表（前两行已示范）：

| 指令 | `ex_inst_cycle_rst = NO` 出现次数 | 总周期数 |
|------|----|----|
| `POP` | 1（相位 0） | 2 |
| `TBLR` | 2（相位 0、1） | 3 |
| `PUSH` | ？ | ？ |
| `B` | ？ | ？ |
| `CALL` | ？ | ？ |
| `IN` | ？ | ？ |
| `OUT` | ？ | ？ |
| `TBLW` | ？ | ？ |

**需要观察的现象**：每条多周期指令的「`NO` 出现次数」应等于「总周期数 − 1」，因为最后一个相位靠默认 `YES` 收尾。

**预期结果**：`PUSH`/`B`/`CALL`/`RET`/`IN`/`OUT` 均为 2 周期（各 1 次 `NO`），`TBLR`/`TBLW` 为 3 周期（各 2 次 `NO`）。这与你能在 TMS32010 用户手册里查到的指令周期表一致，也是 IKA32010「半周期精确」的一个佐证。

#### 4.1.5 小练习与答案

**练习 1**：假如某条多周期指令忘了在相位 0 写 `ex_inst_cycle_rst = NO`，会发生什么？

**参考答案**：计数器会在相位 0 结束时就被默认 `YES` 复位回 0，于是下一拍 `ex_inst_cycle` 仍是 0，永远进不了相位 1——相当于这条指令被「截断」成单周期，后续相位的动作（如 `TBLR` 的写 RAM）永远不会执行，行为错误。

**练习 2**：`ex_inst_cycle` 是 2 位宽，理论最多支持 4 个相位（0/1/2/3）。本项目最长用到几个相位？为什么 2 位够用？

**参考答案**：最长用到 3 个相位（`TBLR`/`TBLW` 用到相位 0、1、2）。2 位可表示 0–3 共 4 个值，足以覆盖；且因为最后一个相位靠默认 `YES` 自动复位，计数器不会真正走到 3 之后，不存在溢出风险。

**练习 3**：为什么单周期指令（如 `ADD`）的分支体里完全看不到 `ex_inst_cycle`？

**参考答案**：因为 `ex_inst_cycle` 在默认 `YES` 的作用下永远停在 0，单周期指令只需假定自己在相位 0 执行即可，无需也不应当对相位做任何判断；写 `if(ex_inst_cycle==0)` 是冗余。

---

### 4.2 复位延迟链 rs_n_z/rs_n_zz 与处理器状态机 ex_state

> 本模块对应最小模块：**rs_n_z / rs_n_zz 复位延迟** 与 **ex_state 状态机**。

#### 4.2.1 概念说明

`ex_inst_cycle` 解决了「一条指令内部怎么分相位」，但还有一个更前置的问题：**处理器从复位到开始执行第一条指令，中间发生了什么？**

`i_RS_n` 是异步低有效复位信号（见 u1-l3）。直觉上，复位一释放，处理器就该立刻开跑。但真实硬件里有两个考量：

1. **同步化**：`i_RS_n` 来自外部，可能相对 `i_EMUCLK` 有亚稳态风险。经典做法是用一两级触发器把它「同步」到本地时钟域。
2. **边沿检测**：处理器只想在「复位释放的那一瞬间」做一次状态切换（从「复位态」进入「正常态」），之后就不再关心 `i_RS_n`（直到下一次复位）。这需要对同步后的信号做上升沿检测。

IKA32010 用 `rs_n_z` / `rs_n_zz` 两个触发器完成「同步 + 边沿检测」，再用一个 1 位状态寄存器 `ex_state` 记录处理器处于复位态（0）还是正常态（1）。这套机制和 4.1 的中断同步链（`int_n_z`/`zz`/`zzz`）是同一种设计模式，本讲先讲复位版，u3-l3 会讲中断版。

#### 4.2.2 核心流程

复位释放的时序可以用下面的状态图描述：

```
   i_RS_n = 0 (复位中)
        |
        |  rs_n_z = 0, rs_n_zz = 0, ex_state = 0  (复位态)
        |
   i_RS_n 拉高 (释放)
        |
        |  第 1 个 cyc_ncen: rs_n_z <= 1, rs_n_zz <= 0   (同步中)
        |  第 2 个 cyc_ncen: rs_n_z <= 1, rs_n_zz <= 1
        |                     且 (~rs_n_zz & rs_n_z) = (~0 & 1) = 1  -> 上升沿命中
        v
   ex_state <= 1  (正常态)
```

注意边沿检测表达式 `~rs_n_zz & rs_n_z`：它为真当且仅当「当前值 `rs_n_z` 已是 1，而上一拍 `rs_n_zz` 还是 0」——正是 `rs_n_z` 的上升沿。一旦 `ex_state` 翻成 1，`if(ex_state == 1'b0)` 这个条件就不再成立，状态机「锁死」在正常态，不会因为 `rs_n_z` 后续抖动而反复跳变。

把上述过程换算成机器周期数：从 `i_RS_n` 释放到 `ex_state` 翻 1，需要 2 个 `cyc_ncen` 边沿，即 2 个机器周期。若用 `T_{mach}` 表示一个机器周期，则复位释放延迟约为：

\[
T_{\text{release}} \approx 2 \cdot T_{\text{mach}} = 2 \cdot 4 \cdot T_{\text{EMUCLK}} = 8 \cdot T_{\text{EMUCLK}}
\]

（这是一个工程估算，实际还取决于 `i_RS_n` 释放时刻相对 `cyc_ncen` 的相位，待本地波形验证。）

`ex_state` 一旦进入正常态，它就**不再参与常规指令的译码**——你看 `casez` 译码表里没有任何地方读 `ex_state`。`ex_state` 只在微码块最外层做一次二选一：复位态走一段「停总线 + PC 复位」的固定动作，正常态才进入中断预检查与 `casez`。

#### 4.2.3 源码精读

**复位延迟链**：

[文件 src/IKA32010.sv:518-525](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L518-L525) —— `rs_n_z` 在复位时强制 0，否则在 `cyc_ncen` 拍跟随 `i_RS_n`；`rs_n_zz` 比 `rs_n_z` 慢一拍，构成两级移位同步链。

```systemverilog
reg             rs_n_z, rs_n_zz;
always @(posedge i_EMUCLK) begin
    if(!i_RS_n) rs_n_z <= 1'b0; //reset state
    else begin if(cyc_ncen) begin
        rs_n_z <= i_RS_n;
        rs_n_zz <= rs_n_z;
    end end
end
```

**状态机**：

[文件 src/IKA32010.sv:528-533](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L528-L533) —— `ex_state` 复位为 0；当且仅当处于复位态且检测到 `rs_n_z` 上升沿时，转移到正常态 1。一旦为 1 便不再回退（除非整体复位）。

```systemverilog
always @(posedge i_EMUCLK) begin
    if(!i_RS_n) ex_state <= 1'b0; //reset state
    else begin if(cyc_ncen) begin
        if(ex_state == 1'b0) if((~rs_n_zz & rs_n_z) == 1'b1) ex_state <= 1'b1;
    end end
end
```

`ex_state` 的声明与注释见 [文件 src/IKA32010.sv:503-507](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L503-L507)。

**微码块对 `ex_state` 的使用**——这是本模块最值得读的一段，它把「复位态」与「正常态」分成了两条互斥的执行路径：

[文件 src/IKA32010.sv:600-617](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L600-L617) —— 复位态：停总线、PC 复位、打印 RESET；正常态：先做中断预检查（覆盖 `if_opcodereg_force_iack` 等默认值），再进入 `casez` 译码。

```systemverilog
if(ex_state == 1'b0) begin
    busctrl_req = BUSCTRL_STOP; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    if_pc_modesel = PC_RESET;
    `ifdef IKA32010_DISASSEMBLY
    disasm = {"IKA32010_", `IKA32010_DEVICE_ID, ": RESET\n"};
    $display(disasm);
    `endif
end

else begin
    //interrupt check
    if_opcodereg_force_iack = (int_rq) ? YES : NO;
    if_pc_modesel           = (int_rq) ? PC_LOAD_INTERRUPT : PC_INCREASE;
    stk_push                = (int_rq) ? YES : NO;
    stk_data_sel            = (int_rq) ? STACK_DATA_PC : STACK_DATA_ACC;

    casez(if_opcodereg)
        ...
```

这段代码揭示了三件事：

1. **复位态是一个「安全岛」**：总线被 `BUSCTRL_STOP` 停掉（不会乱驱动外部总线），PC 走 `PC_RESET`（保持在 0），所有会改写状态的动作都不发生。`ex_inst_cycle_rst` 维持默认 `YES`，计数器停在 0。
2. **正常态入口的三元组覆盖了 4.1.3 里那个「默认 `YES`」**：`if_opcodereg_force_iack = (int_rq) ? YES : NO`。也就是说，在正常态、无中断时，`if_opcodereg_force_iack` 实际是 `NO`，指令寄存器可以正常取指或保持——这才让 4.1 讲的「多周期指令靠 `force_iack=NO` 保持操作码」成立。
3. **中断预检查**：`int_rq`（中断请求）会在 `casez` 之前先把 PC 模式改成 `PC_LOAD_INTERRUPT`（跳到向量 `0x002`）、压栈保存返回地址、并把指令寄存器冲成 IACK。这部分是 u3-l3 的主题，本讲只需知道：多周期指令之所以在每个相位都**再次显式写** `if_opcodereg_force_iack = NO`（带注释 `//deny interrupt request`），就是为了把这里被 `int_rq` 抬起来的 `YES` 强行压回去，从而保证多周期指令**执行期间不被中断打断**（原子性）。

#### 4.2.4 代码实践

**实践目标**：在 testbench 里观察从 `i_RS_n` 释放到第一条指令执行之间的「静默期」，验证复位延迟的存在。

**操作步骤**：

1. 打开 `src/IKA32010_tb.v`，看 [文件 src/IKA32010_tb.v:15-24](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L15-L24) 的复位激励：`#30 RS_n <= 1'b0; #100 RS_n <= 1'b1;`。即先拉低 100 单位、再拉高。
2. 用你熟悉的仿真器（Icarus / Verilator / ModelSim 等）跑 `IKA32010_tb`，在波形里把 `main.ex_state`、`main.rs_n_z`、`main.rs_n_zz`、`main.cyclecntr`、`main.if_pc` 加到波形窗。
3. 把时间游标定位到 `RS_n` 上升沿之后的几个 `EMUCLK` 周期，观察 `ex_state` 从 0 翻到 1 的时刻。

**需要观察的现象**：

- `RS_n` 拉低期间，`ex_state` 一直是 0，`if_pc` 是 0（或 `x`），总线选通 `MEN_n/DEN_n/WE_n` 全高（空闲）。
- `RS_n` 释放后，`ex_state` 并不是立刻翻 1，而是等了约 2 个机器周期（注意 testbench 里 `i_CLKIN_PCEN` 是 1/4 占空比窄脉冲，一个机器周期 = 16 个 `EMUCLK`，所以这段静默期在波形上会显得比较长）。
- `ex_state` 翻 1 之后，`if_pc` 才开始按指令推进，`MEN_n` 才开始出现周期性低脉冲（取指）。

**预期结果**：`ex_state` 的翻转发生在 `~rs_n_zz & rs_n_z` 为真的那个 `cyc_ncen` 拍，比 `RS_n` 上升沿晚约 2 个 `cyc_ncen`。

> 若无仿真环境，本实践退化为「源码阅读型」：手工推演 L518–L533 在「`RS_n` 释放后第 1、第 2 个 `cyc_ncen`」时 `rs_n_z`/`rs_n_zz`/`ex_state` 的取值，结论相同。

#### 4.2.5 小练习与答案

**练习 1**：把边沿检测表达式 `~rs_n_zz & rs_n_z` 改成 `rs_n_z`（去掉边沿检测），会有什么后果？

**参考答案**：那样 `ex_state` 只要 `rs_n_z` 为 1 就会翻 1——功能上看似也能进入正常态。但若 `i_RS_n` 在释放后存在短暂抖动（再次拉低又拉高），`rs_n_z` 会跟着翻转，而 `ex_state` 一旦为 1 就不再回退（因为 `if(ex_state==1'b0)` 不成立），所以对 `ex_state` 本身影响不大。真正的损失是：你失去了一个干净的「单次触发」语义，且无法复用同一套电路去检测后续事件（中断链正是复用了这套边沿检测思想）。工程上保留边沿检测是更稳健、更可复用的写法。

**练习 2**：为什么 `ex_state` 只有 1 位（0/1），而不是一个更大的状态机？

**参考答案**：因为 IKA32010 的取指-译码-执行并没有拆成显式的多状态 FSM（不像经典 MIPS 的 IF/ID/EX/MEM/WB）。它用「组合微码 + 单周期/多周期指令计数器」来组织执行，处理器宏观上只有「复位」与「正常运行」两种状态。多周期指令内部的「相位」由 `ex_inst_cycle` 承担，而非 `ex_state`。所以 1 位 `ex_state` 足够。

**练习 3**：复位期间 `if_pc_modesel = PC_RESET`。结合 u2-l2，`PC_RESET` 会让 `if_pc` 变成什么？

**参考答案**：`PC_RESET` 是 PC 模式常量之一（见 [文件 src/IKA32010_mnemonics.sv:7](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L7)）。在 PC 更新逻辑里它会把 `if_pc` 清零，使复位后从程序空间 `0x000` 开始取指（具体实现见 u2-l2）。

---

### 4.3 多周期指令相位分解：IN（2 周期）与 TBLR（3 周期）

> 本模块是 4.1、4.2 的综合应用，对应学习目标里「弄清多周期指令在 cycle 0/1/2 各自做什么」。

#### 4.3.1 概念说明

有了 `ex_inst_cycle` 分相位、有了 `ex_state` 保证复位安全，我们就能逐相位拆解一条真实的多周期指令。本模块选两个最有代表性的：

- **`IN`（2 周期）**：从外设端口读一个字，写到数据 RAM。它展示「相位 0 发起外设读、相位 1 落盘」的最小两拍结构。
- **`TBLR`（3 周期）**：从程序空间读一个字（地址在累加器里），写到数据 RAM。它展示「借 PC 当地址指针、用栈保存返回地址」的三拍结构，是全项目最复杂的多周期指令之一。

理解这两个，`OUT`、`TBLW`、`PUSH`/`POP`、`CALL`/`RET`、各分支指令都是同类套路，可举一反三。

#### 4.3.2 核心流程

**`IN` 的两相位流程**：

```
相位 0：发起外设读
  - busctrl_req = COMMAND_IN, 地址 = PA（指令字 [10:8]）
  - if_pc_modesel = PC_HOLD       # PC 暂停，不取下一条
  - ex_inst_cycle_rst = NO        # 进入相位 1
  - force_iack = NO               # 保持指令寄存器 + 拒绝中断
  => 总线控制器在 4 个子相位里读外设，cyclecntr==3 时把 i_DIN 锁进 busctrl_inlatch

相位 1：落盘到 RAM
  - busctrl_req = OPCODE_READ     # 恢复取指
  - register_wrbus_source_sel = WRBUS_SOURCE_INLATCH  # 把刚读进的数据送上写总线
  - ram_wr = YES                  # 写入 RAM（地址由直接/间接寻址决定）
  - （间接寻址副作用：AR 自增自减、ARP 改写）
  => 计数器默认复位，IN 结束
```

**`TBLR` 的三相位流程**（难点在于「借用 PC 与栈」）：

TBLR 的语义是「把程序空间 `MEM[ACC]` 的一个字搬到数据 RAM」。但程序空间的地址只能通过 PC 输出（见 u2-l3，`o_AOUT` 在 `BUSCTRL_ADDR_PC` 模式下输出 `if_pc`）。所以 TBLR 必须：先把 PC 临时改成累加器值去读程序空间，读完再把 PC 改回来——而「改回来」需要用栈保存原 PC。

```
相位 0：准备——保存返回地址，把 PC 指向表源
  - stk_push = YES, stk_data_sel = STACK_DATA_PC   # 把当前 PC（=返回地址）压栈
  - register_wrbus_source_sel = WRBUS_SOURCE_SHB    # 累加器经移位器 B 上写总线
  - if_pc_modesel = PC_LOAD_WRBUS                   # PC ← ACC（表源地址）
  - busctrl_req = BUSCTRL_STOP                      # 本相位不做总线事务
  - ex_inst_cycle_rst = NO; force_iack = NO

相位 1：发起表读——读程序空间，同时恢复 PC
  - busctrl_req = DATA_READ, 地址 = if_pc（=表源地址）
  - register_wrbus_source_sel = WRBUS_SOURCE_STACK  # 栈顶（返回地址）上写总线
  - if_pc_modesel = PC_LOAD_WRBUS                   # PC ← 栈顶（恢复返回地址）
  - stk_pop = YES                                   # 弹栈
  - ex_inst_cycle_rst = NO; force_iack = NO
  => 总线控制器在 cyclecntr==3 把表数据锁进 busctrl_inlatch

相位 2：落盘到 RAM
  - busctrl_req = OPCODE_READ                       # 恢复取指（PC 已是返回地址）
  - register_wrbus_source_sel = WRBUS_SOURCE_INLATCH # 表数据上写总线
  - ram_wr = YES                                    # 写入数据 RAM
  - （间接寻址副作用）
  => 计数器默认复位，TBLR 结束
```

这套「相位 0 压栈改 PC、相位 1 读表 + 弹栈恢复 PC、相位 2 落盘」的三拍编排，是 TBLR/TBLW 共用的骨架，差别只在相位 1 的事务类型（`DATA_READ` vs `DATA_WRITE`）与相位 2 的数据流向。

#### 4.3.3 源码精读

**`IN` 指令**：

[文件 src/IKA32010.sv:1604-1632](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1604-L1632) —— `IN` 的两相位译码。相位 0 发起外设读并保持 PC；相位 1 把锁存的数据写进 RAM。

```systemverilog
//IN - Input data from port
16'b0100_0???_????_????: begin
    if(ex_inst_cycle == 2'd0) begin
        busctrl_req = COMMAND_IN; busctrl_addr_muxsel = BUSCTRL_ADDR_PERIPHERAL;
        if_pc_modesel = PC_HOLD;
        ex_inst_cycle_rst = NO;
        if_opcodereg_force_iack = NO;
        stk_push = NO; stk_data_sel = STACK_DATA_ACC;
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        register_wrbus_source_sel = WRBUS_SOURCE_INLATCH;
        ram_wr = YES;
        if(if_opcodereg[7]) begin           //间接寻址副作用
            reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
            if(!if_opcodereg[3]) begin
                if(if_opcodereg[0]) reg_arp_set = YES;
                else                reg_arp_rst = YES;
            end
        end
    end
    ...
end
```

对照总线控制器（u2-l3）可知：相位 0 的 `COMMAND_IN` 会在 `cyclecntr==3` 把外设数据 `i_DIN` 锁进 `busctrl_inlatch`（[文件 src/IKA32010.sv:233-240](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L233-L240)）；相位 1 再用 `WRBUS_SOURCE_INLATCH` 把它送上写总线、`ram_wr = YES` 写进 RAM。两相位缺一不可——读外设和写 RAM 都各占一个完整的机器周期。

**`TBLR` 指令**：

[文件 src/IKA32010.sv:1663-1702](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1663-L1702) —— `TBLR` 的三相位译码，完美对应 4.3.2 的流程图。

```systemverilog
//TBLR - Table read
16'b0110_0111_????_????: begin
    if(ex_inst_cycle == 2'd0) begin
        busctrl_req = BUSCTRL_STOP; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        register_wrbus_source_sel = WRBUS_SOURCE_SHB;
        if_pc_modesel = PC_LOAD_WRBUS;     //PC ← ACC
        ex_inst_cycle_rst = NO;
        if_opcodereg_force_iack = NO;
        stk_push = YES; stk_data_sel = STACK_DATA_PC;   //压返回地址
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = DATA_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;  //读程序空间
        register_wrbus_source_sel = WRBUS_SOURCE_STACK;
        if_pc_modesel = PC_LOAD_WRBUS;     //PC ← 栈顶（恢复）
        ex_inst_cycle_rst = NO;
        if_opcodereg_force_iack = NO;
        stk_push = NO; stk_pop = YES; stk_data_sel = STACK_DATA_ACC;     //弹栈
    end
    else if(ex_inst_cycle == 2'd2) begin
        busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        register_wrbus_source_sel = WRBUS_SOURCE_INLATCH;  //表数据
        ram_wr = YES;                                       //写 RAM
        if(if_opcodereg[7]) begin ... end                  //间接寻址副作用
    end
    ...
end
```

有几个细节值得圈出：

- **相位 0 用 `BUSCTRL_STOP`**：本相位不发任何总线事务，只做「压栈 + 改 PC」两件内部事。这是为了让相位 1 拿到稳定的、指向表源的 PC 后再去读程序空间。
- **`stk_push` 与 `stk_pop` 严格对称**：相位 0 压栈（`STACK_DATA_PC`），相位 1 弹栈。结合 u2-l6（4 级栈是移位寄存器式），这意味着 TBLR 执行完后栈内容完全恢复，不留痕迹——它只是「借」了一格栈当临时存储。
- **相位 2 不写 `ex_inst_cycle_rst`**：回到默认 `YES`，计数器归零，TBLR 自然收尾。这是 4.1 「自终止」机制在三周期指令上的体现。
- **每个相位都写 `force_iack = NO`**：注释 `//deny interrupt request`。这是把 4.2.3 里被 `int_rq` 抬起的 `YES` 压回去，确保 TBLR 三拍之内不被中断打断——否则中途改 PC、动栈的状态会被中断响应破坏。

**对比 `TBLW`**：[文件 src/IKA32010.sv:1704-1743](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1704-L1743) 与 TBLR 几乎逐行对称，唯一差别在相位 1：TBLW 是 `DATA_WRITE`（把 RAM 数据写到程序空间），且相位 0 用 `DATA_READ` 读出 RAM 数据准备写出。读者可对照阅读，体会「同一套三拍骨架，换事务类型即换方向」。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：以 `TBLR` 为例，亲手整理出三相位里关键控制信号的取值表，把本讲三个模块的知识串起来。

**操作步骤**：

1. 重新读一遍 [文件 src/IKA32010.sv:1663-1702](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1663-L1702) 的 TBLR 分支。
2. 对相位 0、1、2，逐个判断下列信号是「显式赋值」还是「沿用默认值」；若是沿用默认值，回忆 L540–L598 与 L613–L616 的默认/覆盖规则后填入。
3. 把结果填进下表（下表已给出第 1 行作为格式示范，其余自行补全）：

| 信号 | 相位 0 | 相位 1 | 相位 2 |
|------|--------|--------|--------|
| `busctrl_req` | `BUSCTRL_STOP` | ？ | ？ |
| `busctrl_addr_muxsel` | `BUSCTRL_ADDR_PC` | ？ | ？ |
| `register_wrbus_source_sel` | `WRBUS_SOURCE_SHB` | ？ | ？ |
| `if_pc_modesel` | `PC_LOAD_WRBUS` | ？ | ？（默认，无中断时） |
| `stk_push` | `YES` | ？ | ？ |
| `stk_pop` | `NO`（默认） | ？ | ？ |
| `stk_data_sel` | `STACK_DATA_PC` | ？ | ？（默认） |
| `ram_wr` | `NO`（默认） | ？ | ？ |
| `ex_inst_cycle_rst` | `NO` | ？ | ？（默认） |
| `if_opcodereg_force_iack` | `NO` | ？ | ？（默认，无中断时） |

**需要观察的现象**：相位 2 那一列应当几乎全是「默认值」——这正是多周期指令「最后一拍靠默认值收尾」的直观体现。

**预期结果（参考答案）**：

| 信号 | 相位 0 | 相位 1 | 相位 2 |
|------|--------|--------|--------|
| `busctrl_req` | `BUSCTRL_STOP` | `DATA_READ` | `OPCODE_READ` |
| `busctrl_addr_muxsel` | `BUSCTRL_ADDR_PC` | `BUSCTRL_ADDR_PC` | `BUSCTRL_ADDR_PC` |
| `register_wrbus_source_sel` | `WRBUS_SOURCE_SHB` | `WRBUS_SOURCE_STACK` | `WRBUS_SOURCE_INLATCH` |
| `if_pc_modesel` | `PC_LOAD_WRBUS` | `PC_LOAD_WRBUS` | `PC_INCREASE`（默认） |
| `stk_push` | `YES` | `NO` | `NO`（默认） |
| `stk_pop` | `NO`（默认） | `YES` | `NO`（默认） |
| `stk_data_sel` | `STACK_DATA_PC` | `STACK_DATA_ACC` | `STACK_DATA_PC`（默认） |
| `ram_wr` | `NO`（默认） | `NO`（默认） | `YES` |
| `ex_inst_cycle_rst` | `NO` | `NO` | `YES`（默认） |
| `if_opcodereg_force_iack` | `NO` | `NO` | `NO`（默认，无中断时） |

> 说明：相位 2 的 `if_pc_modesel`、`stk_data_sel`、`ex_inst_cycle_rst`、`force_iack` 都来自 4.2.3 的「正常态入口覆盖」与微码默认值区，TBLR 相位 2 没有再改写它们。「无中断时」指 `int_rq == 0`，使 L613–L616 的三元组退化为默认值。

**进阶（待本地验证）**：在 testbench 里手工构造一段极简程序：先用 `LACK`/`ADD` 把累加器设成某个程序空间地址，再执行 `TBLR`，在波形里观察 `main.ex_inst_cycle` 是否如上表在 0→1→2→0 走一遍，并核对目标 RAM 单元是否被写入正确数据。由于本项目 testbench 默认加载的是外部 ROM 镜像（[文件 src/IKA32010_tb.v:78-84](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L78-L84) 用 `$readmemh` 读 `dsp_hi.txt`/`dsp_lo.txt`），自行构造程序需要你提供对应的 ROM 镜像文件——这部分留作开放练习。

#### 4.3.5 小练习与答案

**练习 1**：`TBLR` 相位 0 为什么用 `BUSCTRL_STOP` 而不是直接 `DATA_READ`？合并相位 0 和相位 1 行不行？

**参考答案**：相位 0 需要先把 PC 改成累加器值（表源地址），但 PC 的更新发生在 `cyc_ncen` 拍（相位 0 结束时）。若相位 0 同时发起 `DATA_READ`，总线控制器读到的还是**旧的** PC（TBLR 下一条指令的地址），读到错误数据。所以必须先用一个 `BUSCTRL_STOP` 的「空拍」让 PC 稳定到表源地址，再在相位 1 发起读。这是「PC 更新与总线事务错开一拍」的典型时序约束，无法合并。

**练习 2**：`TBLR` 在三个相位里都把 `if_opcodereg_force_iack = NO`，注释写 `deny interrupt request`。假如相位 1 忘了写这一句，会发生什么？

**参考答案**：相位 1 的 `force_iack` 会回到 4.2.3 的正常态入口值 `(int_rq) ? YES : NO`。若此刻恰好 `int_rq` 为真（有中断请求），`force_iack` 变 `YES`，指令寄存器在相位 1 结束时被冲成 IACK，于是相位 2 不再命中 TBLR 分支——表数据不会被写进 RAM，TBLR 被中途截断。更糟的是，PC 已被改成返回地址、栈已弹过，状态半残。所以多周期指令必须每相位都重申 `force_iack = NO` 以保证原子性。

**练习 3**：`IN` 是 2 周期，`TBLR` 是 3 周期。从「需要几次外部总线事务」与「是否需要借用 PC/栈」两个角度，解释为什么 TBLR 比 IN 多一个周期。

**参考答案**：`IN` 只需一次外部总线事务（读外设），所以相位 0 读、相位 1 写 RAM，2 周期够用；它不动 PC（`PC_HOLD`），也不动栈。`TBLR` 需要在程序空间读一个字，而程序空间地址只能经 PC 输出，于是必须「改 PC 去读、读完恢复 PC」——改 PC 前要压栈保存返回地址，读完后要弹栈恢复 PC。这就多出了「相位 0 准备（压栈 + 改 PC，空拍）」这一拍，总共 3 周期。换言之，多出的那一拍是为「借用 PC 当地址指针」付出的代价。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「多周期指令时序侦探」任务：

**任务背景**：假设你要向同事解释「为什么 `TBLR` 占 3 个机器周期、而 `ADD` 只占 1 个」，并用源码证据说服他。

**要求**：

1. **从计数器角度**：引用 [文件 src/IKA32010.sv:511-515](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L511-L515)，说明 `ex_inst_cycle_rst` 的默认值如何让 `ADD` 永远停在相位 0，而 `TBLR` 如何在 [文件 src/IKA32010.sv:1663-1702](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1663-L1702) 里两次把 `ex_inst_cycle_rst` 改成 `NO`。
2. **从指令寄存器保持角度**：引用 [文件 src/IKA32010.sv:183-188](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L183-L188)，说明 `TBLR` 每相位写 `force_iack = NO` 是如何让同一个操作码在三拍内反复命中同一个 `casez` 分支的。
3. **从复位安全角度**：引用 [文件 src/IKA32010.sv:528-533](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L528-L533) 与 [文件 src/IKA32010.sv:600-609](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L600-L609)，说明处理器复位时为何不会误执行多周期指令的中间相位（因为 `ex_state==0` 直接走 `BUSCTRL_STOP` + `PC_RESET`，根本不进 `casez`）。
4. **产出**：一张三栏（相位 0/1/2）的 TBLR 控制信号表（可直接复用 4.3.4 的答案表），配一段 200 字以内的中文解说。

完成这个任务后，你应当能不查资料地说出：**IKA32010 的多周期执行 = 「保持操作码 + 计数器分相位 + 每相位一组微码 + 默认值自终止 + 复位态与中断原子性保护」**。

## 6. 本讲小结

- `ex_inst_cycle` 是 2 位指令周期计数器，在 `cyc_ncen` 拍更新；`ex_inst_cycle_rst` 默认 `YES` 使单周期指令永远停在相位 0，多周期指令在前几个相位把它改 `NO` 以推进，最后一相位靠默认 `YES` 自终止。
- 多周期指令在每个相位都写 `if_opcodereg_force_iack = NO`，使指令寄存器在非取指事务期间保持不变，从而让同一个 `casez` 分支连续命中多个机器周期。
- `rs_n_z`/`rs_n_zz` 构成两级同步 + 上升沿检测链，`ex_state` 借 `~rs_n_zz & rs_n_z` 从复位态（0）一次性转移到正常态（1），复位释放延迟约 2 个机器周期。
- `ex_state` 在微码块最外层做二选一：复位态停总线 + PC 复位，正常态才进入中断预检查与 `casez`；正常态入口的 `(int_rq) ? ...` 三元组覆盖了 `force_iack` 等默认值。
- 多周期指令「拒绝中断」（每相位重申 `force_iack = NO`）保证了执行原子性；`TBLR` 额外借用 PC 当程序空间地址指针、用栈保存返回地址，因此比 `IN` 多一个「准备拍」，共 3 周期。
- 本讲涉及的指令周期数（2 或 3）与 TMS32010 用户手册一致，是 IKA32010「半周期精确」的具体体现。

## 7. 下一步学习建议

本讲把「多周期 + 状态机」的骨架搭好了，接下来可以按以下顺序深入：

1. **u3-l3 中断机制**：本讲多次提到 `int_rq`、`if_opcodereg_force_iack` 与「拒绝中断」，下一篇会把 `int_n_z`/`zz`/`zzz` 三级同步链、`int_latched`、内部 IACK 操作码、`reg_intm`（`DINT`/`EINT`）完整讲清。它与本讲的复位延迟链是同一套设计模式，对照阅读会非常顺畅。
2. **u3-l6 分支与子程序类指令译码**：本讲用 `POP`/`IN`/`TBLR` 做了多周期示范，分支类（`B`/`BANZ`/`CALL`/`RET` 等）也是 2 周期结构，可在那一篇里看到「条件判断 + 两相位」的组合。
3. **u3-l7 乘法器与 I/O/数据存储类指令译码**：会把本讲的 `IN`/`OUT`/`TBLR`/`TBLW` 放回指令系统全图，连同乘法器类指令一起讲。
4. **回看 u2-l3 总线控制器**：本讲多次引用「相位 0 的 `COMMAND_IN` 在 `cyclecntr==3` 锁存 `busctrl_inlatch`」这类结论，其逐相位电平时序的根源就在 u2-l3 的 `case(busctrl_mode[2:0])` 大表里，值得带着本讲的 cycle 0/1/2 视角再读一遍那张表。

建议阅读源码的顺序：先重读 [src/IKA32010.sv:503-533](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L503-L533)（状态与计数器），再带着「分相位」的眼光扫一遍 [src/IKA32010.sv:690-732](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L690-L732)（`POP`/`PUSH`）与 [src/IKA32010.sv:1663-1743](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1663-L1743)（`TBLR`/`TBLW`），把本讲的时序表逐一在源码里「对号入座」。
