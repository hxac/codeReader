# data_rec 实体：generics 与端口语义

## 1. 本讲目标

本讲从「地址地图」（u2-l2）进一步下沉到记录器的核心 RTL 文件 `hdl/data_rec.vhd`，专门拆解它的**实体声明（entity）**：四个 generic 如何决定整块 IP 的规模、各个端口为什么是这个位宽、寄存器侧端口与 u2-l2 的地址地图如何一一对应。

学完后你应当能够：

- 说清 `NumOfInputs_g`、`InputWidth_g`、`MemoryDepth_g`、`TrigInputs_g` 四个 generic 的取值范围与各自控制的维度（通道数 / 位宽 / 采样深度 / 外部触发路数）。
- 给定一组 generic，**手算**出 `Mem_Data`、`Mem_Adr`、`PreTrigSpls`、`TotalSpls` 等端口的位宽，并解释位宽公式为什么是这样。
- 解释 `Mem_Adr`、`Mem_Data`、`Mem_Wr`、`FirstSplAddr` 四个存储侧端口的用途，以及为什么 `TotalSpls` 比 `PreTrigSpls` 多 1 个比特。
- 把 `PreTrigSpls`、`TotalSpls`、`SelfTrigLo/Hi`、`TrigEna` 等端口回连到 u2-l2 给出的寄存器地址。

本讲只读 entity，**不展开**状态机和流水线的内部实现（那是 u3-l2、u3-l3 的任务），只把「这张脸长什么样、每个接口通向哪」讲透。

## 2. 前置知识

在开始之前，你需要具备以下概念（不熟悉的先看 u1、u2 对应讲义）：

- **VHDL entity / architecture / generic / port**：entity 是一个模块对外的「接口卡片」，generic 是编译期参数（类似 C++ 的模板参数），port 是运行期的信号引脚。本讲几乎全部围绕 entity 展开。
- **位宽与地址空间的关系**：要寻址 \(N\) 个存储单元，地址线至少需要 \(\lceil \log_2 N \rceil\) 位。例如寻址 256 个样本需要 8 位地址。
- **`log2ceil` 与 `log2`（来自 `psi_common_math_pkg`）**：本工程用 `log2ceil(n)` 表示向上取整 \(\lceil \log_2 n \rceil\)（要多少位才能装下），用 `log2(n)` 表示向下取整 \(\lfloor \log_2 n \rfloor\)。两者相等当且仅当 \(n\) 是 2 的幂——这正是判断「非二次幂深度」的依据（见 4.1.3）。
- **u2-l2 的寄存器地图**：记录器对外暴露 13 个寄存器（`0x0000`–`0x0030`）和一块存储区（`Mem_Addr_c = 0x0080`）。本讲会把 entity 的寄存器侧端口逐个挂回这些地址。
- **IPIC 信号组**：封装层 `data_rec_vivado_wrp` 用 `reg_rd/reg_wr/reg_wdata/reg_rdata` 与 `mem_*` 这组信号与 AXI 桥（`psi_common_axi_slave_ipif`）对接。entity 的寄存器侧端口在封装层里正是从 `reg_wdata` 抽出来的（u5-l1 详讲）。

## 3. 本讲源码地图

本讲只涉及三个文件，重点是第一个：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `hdl/data_rec.vhd` | 核心记录器 RTL（entity + 两进程架构） | entity 的 generic 与全部 port，第 23–76 行 |
| `hdl/data_rec_register_pkg.vhd` | 寄存器地址与字段常量包 | 把 entity 的寄存器侧端口挂回地址 |
| `hdl/data_rec_vivado_wrp.vhd` | Vivado 封装层（AXI/时钟域/存储） | 第 460–509 行如何实例化 `data_rec`，generic/port 映射 |

记忆口诀（承接 u1-l1）：**README 是入口，PDF 是权威，源码是事实**。本讲读的就是「事实」。

## 4. 核心概念与源码讲解

本讲把 entity 拆成四个最小模块：**generic 参数**、**数据/触发端口**、**寄存器侧端口**、**存储侧端口**。前三者决定「IP 长多大、接什么信号」，最后一个决定「录到的波形怎么交出去」。

### 4.1 data_rec 实体总览与四个 generic

#### 4.1.1 概念说明

`data_rec` 是一块**纯数据域**的记录器 RTL：它只认一个时钟 `Clk` 和一个复位 `Rst`，完全不知道 AXI 的存在。所有「可配置」的维度都通过 generic 在综合时固定下来。理解 generic 是理解整个 entity 的前提——因为**几乎所有端口的位宽都是 generic 的函数**。

四个 generic 分别控制四个互相独立的维度：

| generic | 类型与范围 | 默认值 | 控制的维度 |
| --- | --- | --- | --- |
| `NumOfInputs_g` | `positive range 1 to 8` | 4 | **数据通道数**（同时记录几路并行数据） |
| `InputWidth_g` | `positive` | 8 | **每路数据的位宽**（每样本几个比特） |
| `MemoryDepth_g` | `positive` | 128 | **每通道采样深度**（环形缓冲能存多少个样本） |
| `TrigInputs_g` | `natural range 0 to 8` | 1 | **外部触发的路数**（可 OR 的外部触发数） |

注意一个关键设计：**数据通道数与外部触发路数互相独立**（u1-l1 已强调，u2-l1 也提过）。你可以有 4 路数据却只接 1 路外部触发，也可以 1 路数据接 8 路外部触发。这正是把它们拆成两个 generic 的原因。

#### 4.1.2 核心流程

generic 不参与运行期数据流，它在综合阶段被固化，进而决定：

```text
NumOfInputs_g  ─┬─► In_Data0..7 中实际使用的路数
                 ├─► Mem_Data 总宽 = NumOfInputs_g × InputWidth_g
                 └─► SelfTrigChEna 的位宽

InputWidth_g   ─┬─► 每路 In_DataX 的位宽
                 ├─► Mem_Data 总宽（与上同）
                 └─► SelfTrigLo/Hi 的位宽

MemoryDepth_g  ─┬─► Mem_Adr / PreTrigSpls / FirstSplAddr 的位宽 = ⌈log2(MemoryDepth_g)⌉
                 ├─► TotalSpls 的位宽 = ⌈log2(MemoryDepth_g)⌉ + 1
                 └─► 是否走「非二次幂」地址分支（NonPwr2MemDepth_c）

TrigInputs_g   ─┬─► Trig_In 的位宽
                 └─► EnableExtTrig 的位宽
```

#### 4.1.3 源码精读

实体声明整体位于 [hdl/data_rec.vhd:23-76](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L23-L76)，其中 generic 部分是这四行：

[hdl/data_rec.vhd:24-31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L24-L31) — 声明四个 generic 与默认值。注意 `NumOfInputs_g` 与 `TrigInputs_g` 有显式范围（`1 to 8` / `0 to 8`），而 `InputWidth_g` 与 `MemoryDepth_g` 只标了 `positive`，范围约束放在了封装层。

```vhdl
NumOfInputs_g : positive range 1 to 8 := 4;
InputWidth_g  : positive               := 8;
MemoryDepth_g : positive               := 128;
TrigInputs_g  : natural range 0 to 8   := 1
```

封装层 `data_rec_vivado_wrp` 给同样的四个 generic 加了更细的约束，并**新增了第五个** `TrigForwarding_g`（v2.4，控制 `Trig_Out` 是否引出，u1-l4、u4-l5 已介绍）：

[hdl/data_rec_vivado_wrp.vhd:24-36](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L24-L36) — 封装层的 generic 块。注意 `InputWidth_g` 这里被限定为 `range 1 to 32`，并且多了 `TrigForwarding_g : boolean := false`。

实例化时只把前四个 generic 往下传（`TrigForwarding_g` 只在封装层用，不进核心）：

[hdl/data_rec_vivado_wrp.vhd:460-466](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L460-L466) — `i_data_rec` 实例的 generic map，仅映射四个 generic。

「非二次幂深度」的判定常量也由 generic 派生，是后续 u3-l5 的入口，这里先认识它：

[hdl/data_rec.vhd:84](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L84) — `NonPwr2MemDepth_c`：当向下取整与向上取整不等时为 `true`。例如 `MemoryDepth_g = 30` 时 \(\lfloor\log_2 30\rfloor = 4\)、\(\lceil\log_2 30\rceil = 5\)，两者不等 → 非二次幂。

```vhdl
constant NonPwr2MemDepth_c : boolean := (log2(MemoryDepth_g) /= log2ceil(MemoryDepth_g));
```

#### 4.1.4 代码实践

**实践目标**：对比核心 entity 与封装层的 generic 块，理解「同一 generic 在多处镜像」的工程现实（u1-l4 已强调过这一点）。

**操作步骤**：

1. 打开 `hdl/data_rec.vhd` 第 24–31 行，记下 4 个 generic 的范围与默认值。
2. 打开 `hdl/data_rec_vivado_wrp.vhd` 第 24–36 行，记下封装层的 5 个 generic。
3. 打开 `scripts/package.tcl`（或生成的 `component.xml`），找到这 4 个 generic 在 IP 打包侧的默认值与范围。

**需要观察的现象**：三处对 `InputWidth_g` 的范围约束不同——entity 里是裸 `positive`、封装层是 `1 to 32`、打包侧通常给出 GUI 可选范围。

**预期结果**：能列出一个三列表格（generic | entity 范围 | 封装层范围 | 打包侧范围），并解释为什么 entity 故意不写死范围（保持核心 RTL 的可复用性，把工程约束下放到封装层）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TrigInputs_g` 的范围是 `0 to 8` 而不是 `1 to 8`？

> **答案**：因为可能存在「完全不用外部触发」的配置（例如只用软件触发或自触发的 free-running / self-trigger 模式，见 u4-l1）。允许 `TrigInputs_g = 0` 让 `Trig_In` 成为空数组（`downto` 后为负即零宽），综合时把外部触发相关逻辑全部优化掉。

**练习 2**：`NonPwr2MemDepth_c` 在 `MemoryDepth_g = 128` 和 `MemoryDepth_g = 30` 时分别取什么值？

> **答案**：128 是 \(2^7\)，\(\lfloor\log_2 128\rfloor = \lceil\log_2 128\rceil = 7\)，故为 `false`（二次幂）；30 不是 2 的幂，\(4 \ne 5\)，故为 `true`（非二次幂，走更复杂的地址分支）。

### 4.2 数据端口与触发端口

#### 4.2.1 概念说明

数据与触发是记录器的两路「输入」：

- **数据端口**：8 路并行数据 `In_Data0..7` 加一个公共有效信号 `In_Vld`。注意 entity **永远声明满 8 路**，实际使用前 `NumOfInputs_g` 路——未用的几路在封装层通过 `component.xml` 的端口使能条件隐藏掉（u1-l4）。
- **触发端口**：外部触发总线 `Trig_In`（位宽 = `TrigInputs_g`）和转发端口 `Trig_Out`（v2.4 新增，把内部裁决后的触发信号转发出去）。

`In_Vld` 是一个容易被忽略但极其关键的信号：**记录器只在 `In_Vld = '1'` 的时钟周期采到一个样本**。它既门控数据写入，也门控触发判定（u4-l1 的 `TrigNow and r.In_Vld(1)`）。

#### 4.2.2 核心流程

数据进入后的去向（本讲只看接线，内部流水线留给 u3-l3）：

```text
In_Data0..7 ──► In_Data(0..7) 数组（architecture 内）
                 └─ 循环 0..NumOfInputs_g-1 把前 N 路打进流水线 Data_0
In_Vld      ──► 流水线 In_Vld(0..2)，各级用它门控写入与计数
Trig_In     ──► 流水线 Trig_In(0..2)，做上升沿检测（u4-l2）
Trig_Out    ◄── r.Trigger_2（内部实际使用的触发，见第 393 行）
```

#### 4.2.3 源码精读

数据与触发端口声明：

[hdl/data_rec.vhd:37-46](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L37-L46) — 8 路数据 + `In_Vld`。每路位宽都是 `InputWidth_g-1 downto 0`。

```vhdl
In_Data0 : in std_logic_vector(InputWidth_g-1 downto 0);
-- ... In_Data1 .. In_Data6 同形 ...
In_Data7 : in std_logic_vector(InputWidth_g-1 downto 0);
In_Vld   : in std_logic;
```

[hdl/data_rec.vhd:47-49](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L47-L49) — 触发端口。`Trig_In` 位宽 = `TrigInputs_g`，`Trig_Out` 是 v2.4 新增的可选转发端口。

```vhdl
Trig_In  : in  std_logic_vector(TrigInputs_g-1 downto 0);
Trig_Out : out std_logic;   -- Optional output for trigger sharing
```

architecture 里把 8 路硬连线收进数组（注意这是**无条件**的全 8 路）：

[hdl/data_rec.vhd:130-137](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L130-L137) — `In_Data(0..7) <= In_Data0..7`。后续所有循环只取 `0 to NumOfInputs_g-1`，多余路虽然接了线却不参与运算。

`Trig_Out` 的赋值在 architecture 末尾，直通内部触发寄存器：

[hdl/data_rec.vhd:393](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L393) — `Trig_Out <= r.Trigger_2;`。它转发的是**经过触发源裁决与最小间隔抑制之后**真正使用的触发（即 `Trigger_2`，在 u4-l1、u4-l5 详讲），所以能安全地用于级联别的记录器或同步外部逻辑。

#### 4.2.4 代码实践

**实践目标**：搞清「entity 声明满 8 路、只用前 N 路」这一设计带来的封装层后果。

**操作步骤**：

1. 在 `hdl/data_rec.vhd` 中用搜索定位所有 `for i in 0 to NumOfInputs_g-1 loop`，数一下有几处。
2. 打开 `component.xml`，找 `In_Data0..In_Data7` 各自的 `portName` 与 `spirit:portValue`（或 `add_port_enablement_condition`）条件。

**需要观察的现象**：`In_DataN` 的可见条件形如 `NumOfInputs_g > N`（u1-l4 已给出）。

**预期结果**：能口头解释——「entity 层为简化代码写死 8 路，参数化靠循环上界；封装层再按 generic 把多余端口隐藏，使 IP 用户在 Block Design 里只看到实际通道数」。这是一个典型的「RTL 简洁性 vs. IP 易用性」的折中。

#### 4.2.5 小练习与答案

**练习 1**：如果 `NumOfInputs_g = 3`，`In_Data3..In_Data7` 这 5 个端口在 entity 层是否还存在？信号会去哪？

> **答案**：在 entity 层**仍然存在并被接到 `In_Data(3..7)` 数组**，但所有运算循环 `0 to NumOfInputs_g-1 = 0 to 2` 都不触及它们。综合器会把这段死代码与未用输入优化掉；在 IP 层面则由 `component.xml` 的端口使能条件隐藏，用户看不到。

**练习 2**：`Trig_Out` 转发的是 `Trig_In` 吗？为什么不是？

> **答案**：不是。`Trig_Out <= r.Trigger_2`，转发的是**经过三类触发源（外部/软件/自触发）OR 合成、又经过 `TrigEna` 掩码与 `MinRecPeriod` 抑制之后**真正触发本次录制的信号（u4-l1、u4-l5）。直接转发 `Trig_In` 没有意义，因为它可能被掩码屏蔽或被最小录制间隔丢弃。

### 4.3 寄存器侧端口（配置、状态与计数）

#### 4.3.1 概念说明

寄存器侧端口是软件控制记录器的「面板」：软件通过 AXI 写某些端口来配置和触发，读某些端口来观察状态。这些端口在 u2-l2 的地址地图里已经登记，本节是把它们**从地址挂回 entity 引脚**。

按方向和用途，寄存器侧端口可分四组：

| 组 | 端口 | 方向 | 对应寄存器地址 |
| --- | --- | --- | --- |
| 状态 | `State` | out | `Reg_Stat_Addr_c` = `0x0000` |
| 录制控制 | `Arm`、`Ack`、`Done` | in/out | `Arm`→`Reg_Cfg_Addr_c` bit0；`Done`→产生 `Done_Irq` |
| 长度配置 | `PreTrigSpls`、`TotalSpls` | in | `0x0008`、`0x000C` |
| 自触发配置 | `SelfTrigLo`、`SelfTrigHi`、`SelfTrigChEna`、`SelfTrigOnExit`、`SelfTrigOnEnter` | in | `0x0010`、`0x0014`、`0x0018` |
| 触发源选择 | `SwTrig`、`TrigEna`、`EnableExtTrig` | in | `0x001C`、`0x0028`、`0x0030` |
| 计数与间隔 | `TrigCntClr`、`TrigCnt`、`DoneTime`、`MinRecPeriod` | in/out | `0x0020`、`0x0024`、`0x002C` + `Cfg` bit16 |

其中**长度配置**端口是本节重点：`PreTrigSpls` 是前触发采样数，`TotalSpls` 是总采样数（前+后），它们的位宽差异直接反映了「环形缓冲」与「录制窗口」的关系。

#### 4.3.2 核心流程

寄存器侧端口与录制窗口（本讲只看「含义」，状态机内部用法见 u3-l2、u3-l4）：

```text
Arm         ──► 启动一次录制（Idle → PreTrig）
PreTrigSpls ──► 环形缓冲先积累多少个前触发样本
[三类触发源经 TrigEna 掩码 → Trigger_2]
TotalSpls   ──► 触发后再补记到总共多少个样本（PostTrig 结束判据）
Done        ◄── 录制完成（PostTrig → Done），上升沿经封装层产生 Done_Irq
State       ◄── 当前处于 5 个状态中的哪一个（4 位编码）
Ack         ──► 软件确认已读走数据（Done → Idle）
```

#### 4.3.3 源码精读

寄存器侧端口整体声明：

[hdl/data_rec.vhd:50-68](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L50-L68) — 全部寄存器侧端口。注意位宽几乎都是 generic 的函数。

重点看四个长度/范围端口及其位宽差异：

```vhdl
PreTrigSpls : in std_logic_vector(log2ceil(MemoryDepth_g)-1 downto 0);  -- 前触发样本数
TotalSpls   : in std_logic_vector(log2ceil(MemoryDepth_g)   downto 0);  -- 总样本数（多 1 位！）
SelfTrigLo  : in std_logic_vector(InputWidth_g-1 downto 0);             -- 自触发下界
SelfTrigHi  : in std_logic_vector(InputWidth_g-1 downto 0);             -- 自触发上界
```

**为什么 `TotalSpls` 比 `PreTrigSpls` 多 1 个比特？** 这是本节最值得想清楚的一点：

- `PreTrigSpls` 最大只能是 `MemoryDepth_g - 1`（前触发样本不能超过缓冲容量），所以 \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) 位就够。
- `TotalSpls` 是「前触发 + 后触发」的总和，它**可以超过 `MemoryDepth_g`**（post-trigger 阶段会继续往环形缓冲里写并回绕）。状态机在 PostTrig 里用 `r.SplCnt_2 >= unsigned(TotalSpls)` 判定结束，比较对象 `SplCnt_2` 本身就是 \(\lceil\log_2(\text{MemoryDepth\_g})\rceil + 1\) 位（见 record 定义第 108 行）。为让软件能把 `TotalSpls` 设到比缓冲深度还大（u6-l2 的 case4「非法配置」测试就用到了这一点），它必须多 1 位。

`State` 是 4 位输出，编码来自 `data_rec_register_pkg`：

[hdl/data_rec.vhd:51](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L51) — `State : out std_logic_vector(3 downto 0)`，4 位足以装下 5 个状态码（0..4）。

[hdl/data_rec_register_pkg.vhd:22-27](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L22-L27) — `Reg_Stat_StateIdle_c=0` … `Reg_Stat_StateDone_c=4`。`p_comb` 末尾的 `case r.State_2 is`（[hdl/data_rec.vhd:340-347](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L340-L347)）就是把内部状态枚举翻成这组数值送到 `State` 引脚。

触发源使能与计数端口：

[hdl/data_rec.vhd:66-68](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L66-L68) — `TrigEna`（3 位：Ext/Sw/Self）、`EnableExtTrig`（位宽 = `TrigInputs_g`）、`MinRecPeriod`（32 位）。三者的位索引常量见寄存器包：

[hdl/data_rec_register_pkg.vhd:50-57](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L50-L57) — `Reg_TrigEna_ExtIdx_c=0`、`Reg_TrigEna_SwIdx_c=1`、`Reg_TrigEna_SelfIdx_c=2`，以及 `Reg_MinRecPeriod_Addr_c=0x002C`、`Reg_EnableExtTrig_Addr_c=0x0030`。

#### 4.3.4 代码实践

**实践目标**：把 entity 的寄存器侧端口与 u2-l2 的地址地图一一对应，建立「引脚 ↔ 地址」的双向索引。

**操作步骤**：

1. 打开 `hdl/data_rec_register_pkg.vhd` 第 22–60 行，抄下所有 `Reg_*_Addr_c`。
2. 对照 `hdl/data_rec.vhd` 第 50–68 行的端口，把每个端口挂到对应地址。
3. 对 `SelfTrigChEna`/`OnExit`/`OnEnter` 这三个端口，注意它们**共用一个寄存器** `Reg_SelftrigCfg_Addr_c = 0x0018`，按 `Reg_SelftrigCfg_ChEnaSft_c=0`、`ExitSft_c=8`、`EnterSft_c=16` 拼接（[hdl/data_rec_register_pkg.vhd:38-41](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L38-L41)）。

**需要观察的现象**：一个 32 位寄存器可以承载多个窄端口（字段拼接），如 `Cfg` 寄存器同时含 `Arm`(bit0) 与 `TrgCntClr`(bit16)。

**预期结果**：得到一张三列表格（端口 | 方向 | 寄存器地址 + 字段位）。这正是后续 u5-l1（AXI 解码）和 u6-l3（EPICS 模板）的工作底稿。

#### 4.3.5 小练习与答案

**练习 1**：`State` 为什么是 4 位而不是 3 位？3 位也能装 0..4。

> **答案**：3 位确实能装 0..4（最大 7），用 4 位主要是对齐到「半字节」便于软件阅读和 IP-XACT 描述，也留出未来扩展状态码的余量。这是工程上的舒适冗余，不是数学必需。

**练习 2**：软件想「只允许软件触发和自触发、禁用外部触发」，应向 `TrigEna` 写入什么值？

> **答案**：`TrigEna` 的 bit0=Ext、bit1=Sw、bit2=Self（[hdl/data_rec_register_pkg.vhd:51-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L51-L53)）。要 Sw + Self、禁 Ext，即 bit1 和 bit2 置 1、bit0 清 0 → 二进制 `110` = `0x6`。（u4-l1 会从 `TrigNow_2` 合成公式再深入一遍。）

### 4.4 存储侧端口（Mem_*）

#### 4.4.1 概念说明

存储侧端口是记录器把波形交给外部的「出货口」。核心记录器自己**不内置 RAM**——它只输出写地址、写数据和写使能，真正的存储器（每通道一块双端口 RAM）实例化在封装层（u5-l3）。这种「地址/数据/使能」三件套是经典的「外部存储器接口」风格。

四个端口：

| 端口 | 方向 | 位宽 | 用途 |
| --- | --- | --- | --- |
| `Mem_Wr` | out | 1 | 写使能：高电平表示本拍把一个样本写入环形缓冲 |
| `Mem_Adr` | out | \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) | 写地址：环形缓冲当前写入位置（0..MemoryDepth_g-1） |
| `Mem_Data` | out | `NumOfInputs_g × InputWidth_g` | 写数据：**所有通道拼接成一个宽字**，一个地址存所有通道同一时刻的样本 |
| `FirstSplAddr` | out | \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) | 本段录制里「最早样本」的地址，供读出时把环形缓冲对齐成线性序列 |

最关键的设计是 **`Mem_Data` 把所有通道拼成一个宽字**：一个写地址对应一个时间点，该时间点所有通道的样本**同时**落盘。这意味着存储器是「按时间交织」而非「按通道分库」——分库是封装层的事（u5-l3 把这个宽字 demux 到每通道独立 RAM）。

`FirstSplAddr` 解决的是环形缓冲的「起点对齐」问题：因为写入是环形的，触发发生时地址计数器并不在 0，所以读出时必须知道「第一个样本存在哪个地址」，才能把环展开成线性数组。它的计算分二次幂与非二次幂两种情况（u3-l5 详讲）。

#### 4.4.2 核心流程

每来一个有效样本（`In_Vld=1` 且非 Idle/Done）时的存储侧动作：

```text
Mem_Adr  ◄── AdrCnt_3（环形计数，到 MemoryDepth_g-1 回绕）
Mem_Data ◄── Data_3（所有通道拼接，通道 i 占 [i*W, (i+1)*W-1]）
Mem_Wr   ◄── In_Vld(2) 且 非 Idle/Done
触发发生时（Trigger_2='1'）：
FirstSpl_3 ◄── AdrCnt_2 - PreTrigSpls  （二次幂，简单减法）
             或带借位回绕的版本        （非二次幂，u3-l5）
FirstSplAddr ◄── std_logic_vector(FirstSpl_3)
```

#### 4.4.3 源码精读

存储侧端口声明：

[hdl/data_rec.vhd:69-73](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L69-L73) — 四个存储侧端口。`Mem_Data` 总宽 = `NumOfInputs_g*InputWidth_g`，`Mem_Adr` 与 `FirstSplAddr` 同宽。

```vhdl
Mem_Wr       : out std_logic;
Mem_Adr      : out std_logic_vector(log2ceil(MemoryDepth_g)-1 downto 0);
Mem_Data     : out std_logic_vector(NumOfInputs_g*InputWidth_g-1 downto 0);
FirstSplAddr : out std_logic_vector(log2ceil(MemoryDepth_g)-1 downto 0)
```

`Mem_Data` 的通道拼接发生在 `p_comb` 末尾的输出赋值段：

[hdl/data_rec.vhd:348-353](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L348-L353) — 通道 i 占据 `Mem_Data` 的 `((i+1)*InputWidth_g-1 downto i*InputWidth_g)` 段，即**通道 0 在最低位**。`Mem_Adr`、`Mem_Wr`、`FirstSplAddr` 在同一处输出。

```vhdl
for i in 0 to NumOfInputs_g-1 loop
    Mem_Data((i+1)*InputWidth_g-1 downto i*InputWidth_g) <= r.Data_3(i);
end loop;
Mem_Adr      <= std_logic_vector(r.AdrCnt_3);
Mem_Wr       <= r.MemWr_3;
FirstSplAddr <= std_logic_vector(r.FirstSpl_3);
```

封装层把这几个端口接到真正的双端口 RAM：

[hdl/data_rec_vivado_wrp.vhd:505-508](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L505-L508) — `Mem_Wr/Mem_Adr/Mem_Data` 接到内部信号 `RecMemWr/RecMemAdr/RecMemData`，`FirstSplAddr` 接到封装层的同名信号。封装层随后会把 `RecMemData` 这个宽字按通道切片，分别写到每通道的 TDP RAM（u5-l3）。

`FirstSplAddr` 在封装层用于构造 AXI 读地址（把环形地址转成线性）：

[hdl/data_rec_vivado_wrp.vhd:513](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L513) — 二次幂情况下 `AxiMemAdr = mem_addr(...) + FirstSplAddr`，即「读地址 = 软件给的线性偏移 + 起点地址」。

#### 4.4.4 代码实践

**实践目标**：验证 `Mem_Data` 的通道拼接顺序，并理解它如何被封装层 demux。

**操作步骤**：

1. 在 `hdl/data_rec.vhd` 第 348–350 行确认通道 i 的位段公式。
2. 打开 `hdl/data_rec_vivado_wrp.vhd`，搜索 `RecMemData`，找到它被切成每通道片段并送入各 TDP RAM 写端口的位置（`g_mem` generate 块，u5-l3 详讲）。

**需要观察的现象**：`RecMemData` 的低位段（通道 0）进 RAM 0，依次类推；写地址 `RecMemAdr` 与写使能 `RecMemWr` 被所有通道**共用**（因为同一时刻、同一地址写所有通道）。

**预期结果**：能解释「为什么所有通道的 RAM 共用 `RecMemAdr` 和 `RecMemWr`」——因为 `data_rec` 保证同一拍、同一地址、所有通道一起落盘，存储结构是「按时间对齐」的。

#### 4.4.5 小练习与答案

**练习 1**：`NumOfInputs_g=4`、`InputWidth_g=16` 时，`Mem_Data` 多宽？通道 2 占哪几位？

> **答案**：总宽 = 4×16 = 64 位（63 downto 0）。通道 2（`i=2`）占 `((2+1)*16-1 downto 2*16)` = `47 downto 32`。

**练习 2**：为什么 `Mem_Adr` 和 `FirstSplAddr` 位宽相同？

> **答案**：两者都表示环形缓冲里的一个地址（0..MemoryDepth_g-1），都需要 \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) 位。`Mem_Adr` 是「当前写到哪」，`FirstSplAddr` 是「本段最早样本在哪」，是同一个地址空间里的两个指针。

## 5. 综合实践

**任务**：给定 `NumOfInputs_g = 6`、`InputWidth_g = 12`、`MemoryDepth_g = 256`，计算 `Mem_Data`、`Mem_Adr`、`PreTrigSpls`、`TotalSpls` 四个端口的位宽，并逐个解释原因。

**解题步骤（参考作答）**：

1. 先算二次幂判定：\(256 = 2^8\)，故 \(\lceil\log_2 256\rceil = 8\)，`NonPwr2MemDepth_c = false`（二次幂路径）。
2. `Mem_Data`：位宽 = `NumOfInputs_g × InputWidth_g` = \(6 \times 12 = 72\) 位，声明为 `71 downto 0`。这 72 位里通道 0 占 `11 downto 0`、通道 1 占 `23 downto 16`……通道 5 占 `71 downto 60`。
3. `Mem_Adr`：位宽 = \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) = 8 位，声明为 `7 downto 0`，可寻址 0..255，正好覆盖 256 个样本。
4. `PreTrigSpls`：位宽 = \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) = 8 位，声明为 `7 downto 0`。前触发样本最多 255（不能满 256，否则没有后触发空间）。
5. `TotalSpls`：位宽 = \(\lceil\log_2(\text{MemoryDepth\_g})\rceil + 1\) = 9 位，声明为 `8 downto 0`，可表示 0..511。多出的 1 位允许总样本数超过缓冲深度（供 post-trigger 回绕写入与 case4「非法配置」测试使用）。

**延伸思考（可选）**：把上述值代入 `data_rec_register_pkg` 的 `MemAddr` 函数，手算通道 2、样本 5 的字节地址：

\[
\text{MemAddr}(2,5,256) = 0x0080 + (2 \cdot 2^{\lceil\log_2 256\rceil} + 5)\cdot 4 = 0x0080 + (2\cdot 256 + 5)\cdot 4 = 0x0080 + 517\cdot 4 = 0x0080 + 0x814 = 0x0894
\]

通道间距 \(2^{\lceil\log_2 256\rceil} = 256\) 个样本 = 1024 字节，因此通道 2 的起点是 `0x0080 + 2*1024 = 0x0880`，样本 5 再加 `5*4 = 20 = 0x14` 字节，得 `0x0894`。这与 u2-l2 给出的 `MemAddr` 公式一致，可作为自检。

> 说明：以上位宽与地址计算均为依据源码声明与 `MemAddr` 公式的**纸面推导**，未在仿真器中运行；如要在本地复现，可在 Vivado Tcl 控制台用 `report_property` 或在仿真里打印 `Mem_Data'length`、`Mem_Adr'length` 验证（**待本地验证**）。

## 6. 本讲小结

- `data_rec` 的 entity 由四个 generic 决定规模：`NumOfInputs_g`（1..8 通道）、`InputWidth_g`（位宽）、`MemoryDepth_g`（深度）、`TrigInputs_g`（0..8 外部触发）。前两者是「宽」维度，后两者分别控制存储与触发，**通道数与外部触发数互相独立**。
- 几乎所有端口位宽都是 generic 的函数：`Mem_Data` = `NumOfInputs_g × InputWidth_g`；地址类端口 = \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) 位；`TotalSpls` 比地址类多 1 位以允许总样本数超过缓冲深度。
- 数据端口 entity 写死 8 路、循环只用前 `NumOfInputs_g` 路；`In_Vld` 同时门控数据写入与触发判定；`Trig_Out` 转发的是内部裁决后的 `Trigger_2` 而非原始 `Trig_In`。
- 寄存器侧端口可逐个挂回 u2-l2 的地址地图（`State`→`0x0000`、`PreTrigSpls`→`0x0008`、`TrigEna`→`0x0028` 等），`SelfTrigChEna/OnExit/OnEnter` 三者共用 `0x0018` 一个寄存器按位移拼接。
- 存储侧是「外部存储器接口」风格：核心不内置 RAM，只输出 `Mem_Wr/Mem_Adr/Mem_Data`；`Mem_Data` 把所有通道拼成宽字、按时间对齐落盘；`FirstSplAddr` 给出环形缓冲的起点供读出对齐。
- 「非二次幂深度」由 `NonPwr2MemDepth_c` 判定（`log2 ≠ log2ceil`），它切换地址处理与 `FirstSpl` 计算的两套分支，是 u3-l5 的主题。

## 7. 下一步学习建议

理解了 entity 这张「接口卡片」之后，自然要进入卡片背后怎么工作：

- **u3-l2（记录状态机）**：看 `State_t` 枚举与 `p_comb` 里 `case r.State_2` 如何在 Idle→PreTrig→WaitTrig→PostTrig→Done 之间迁移，以及 `Arm`/`Ack`/`Done` 三个端口在迁移中扮演的角色。
- **u3-l3（两进程法与流水线）**：看 `data_rec_r` record 与 `p_comb`/`p_seq` 模板，理解本讲多次出现的带后缀信号（`Data_2`、`AdrCnt_3`、`Trigger_2`）为什么这样命名。
- **u3-l4（地址与采样计数器）**：看 `AdrCnt_2/3` 的环形回绕与 `SplCnt_2` 如何用 `PreTrigSpls`/`TotalSpls` 决定录制窗口——本讲的位宽推导在那里变成真实的计数行为。

建议在进入 u3-l2 前，先回头在 `hdl/data_rec.vhd` 里把本讲列出的端口在源码中逐个定位一遍，建立「端口名 ↔ 源码行」的肌肉记忆，后续读状态机时会顺畅很多。
