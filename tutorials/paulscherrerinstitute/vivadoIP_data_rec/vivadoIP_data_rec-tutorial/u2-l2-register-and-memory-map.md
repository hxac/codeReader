# 寄存器与存储地址地图

## 1. 本讲目标

上一讲（u2-l1）我们看清了 IP 对外的「门面」：`data_rec_vivado_wrp` 的算法侧端口和 AXI4 Slave 五通道。但软件究竟往**哪个地址**写一个字才能让记录器 Arm？往**哪个地址**读才能拿到第 3 通道第 100 个样本？这些问题必须由一张明确的「地址地图」来回答。

本讲的目标是：

1. 能逐个说出 `data_rec_register_pkg` 中定义的全部寄存器**字节地址**与**关键字段位**。
2. 理解 `TrigEna` 寄存器中三种触发源（外部/软件/自触发）分别占据哪一位，以及 `Cfg` 寄存器中 `Arm` 与 `TrgCntClr` 的位定义。
3. 掌握两个工具函数 `ToWordAddr` 和 `MemAddr` 的含义，能手算任意「通道 + 样本」对应的字节地址。

学完本讲，你就拥有了一份软件驱动开发与测试平台编写都离不开的「地址速查表」。

## 2. 前置知识

在进入地址地图前，先用三段话建立必要直觉。

**字节地址 vs 字地址。** AXI4 数据总线固定 32 位，即每次读写搬运 **4 个字节**（一个 word）。因此寄存器/存储地址都按 4 字节对齐：`0x0000`、`0x0004`、`0x0008`、`0x000C`、…… 我们把 `0x0000` 这样的地址叫**字节地址**（byte address），把它除以 4 得到的 `0`、`1`、`2`、`3` 叫**字地址**（word address）。AXI 事务线上出现的是字节地址；而内部用一个数组 `reg_rdata(0 to 31)` 索引寄存器时用的是字地址——这就是 `ToWordAddr` 存在的理由。

**IPIC 信号组。** 封装层把 AXI 解码后给用户逻辑留下一组简化的「本地总线」信号（IP Interconnect, IPIC）：

- `reg_rd(i)` / `reg_wr(i)`：第 `i` 个字（字地址为 `i`）正在被**读** / **写**，高有效、单拍脉冲。
- `reg_wdata(i)`：第 `i` 个字写入的 32 位数据。
- `reg_rdata(i)`：第 `i` 个字回读的 32 位数据（用户逻辑驱动）。
- `mem_addr` / `mem_wr` / `mem_wdata` / `mem_rdata`：存储区（录制样本）的地址与数据。

本讲主要关心「地址 `i` 是谁」「数据中的哪几位有意义」；AXI 如何翻译成这些 IPIC 信号是 u5-l1 的主题，这里只需把它们当作已就绪的输入。

**位（bit）、字段（field）、掩码。** 一个 32 位寄存器常被切成若干字段。例如某寄存器 bit0 是 `Arm`，bit16 是 `TrgCntClr`。软件「按位写」通常用 `值 * 2**位号` 的写法：写 `1*2**0 = 1` 触发 Arm，写 `1*2**16 = 0x10000` 触发 TrgCntClr。这种「左移」写法正是字段位常量（`_Idx_c` / `_Sft_c`）的用途。

## 3. 本讲源码地图

本讲只涉及两个文件，它们构成了「地址真相」的全部来源：

| 文件 | 作用 |
|------|------|
| [`hdl/data_rec_register_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | **地址真相之源**。一个 VHDL package，定义全部寄存器地址常量、字段位常量，以及 `ToWordAddr` / `MemAddr` 两个函数。软件、测试平台、封装层都 `use` 它，保证三处地址永远一致。 |
| [`hdl/data_rec_vivado_wrp.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | 封装层。用这些常量把 IPIC 信号解码成记录器需要的端口（`Arm`、`PreTrigSpls`、`TrigEna` ……），并实例化每通道存储 RAM。本讲引用它来证明「地址常量如何被消费」。 |

测试平台 [`testbench/top_tb/top_tb_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd) 和 [`testbench/top_tb/top_tb_case0_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd) 则是这些地址的「活样本」，会在代码实践里反复出现。

> 口诀：**寄存器包是唯一真相源（single source of truth），封装层是消费者，测试平台是证人。** 改地址只改 package，三处自动同步。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

- **4.1 寄存器地址常量**（`Reg_*_Addr_c`）——整张地址地图的骨架。
- **4.2 字段位常量**（`Reg_*_Idx_c` / `*_Sft_c`）——每个寄存器内部字段的位定义。
- **4.3 `ToWordAddr` / `MemAddr` 函数**——字节↔字换算与存储寻址公式。

### 4.1 寄存器地址常量（`Reg_*_Addr_c`）

#### 4.1.1 概念说明

记录器对外暴露的「控制旋钮」和「状态指示灯」被收拢成一组 32 位寄存器，每个寄存器占用 4 字节、地址 4 字节对齐。`data_rec_register_pkg` 用一批 `Reg_<名字>_Addr_c` 整型常量把这些地址固化下来，值用 VHDL 十六进制字面量 `16#XXXX#` 表示。

这样做的好处是：地址只在**一处**定义，软件、VHDL、测试平台都引用同一个名字（如 `Reg_Cfg_Addr_c`），谁也不会写错地址。这是嵌入式寄存器地图的标准工程实践。

#### 4.1.2 核心流程

地址从 `0x0000` 开始递增，按功能分组排列：

```
0x0000  Reg_Stat          状态（只读）
0x0004  Reg_Cfg           配置/控制（只写脉冲：Arm、TrgCntClr）
0x0008  Reg_Pretrig       预触发采样数（读写）
0x000C  Reg_Totspl        总采样数（读写）
0x0010  Reg_SelftrigLo    自触发下限（读写）
0x0014  Reg_SelftrigHi    自触发上限（读写）
0x0018  Reg_SelftrigCfg   自触发通道/方向配置（读写）
0x001C  Reg_SwTrig        软件触发（只写）
0x0020  Reg_TrigCnt       触发计数（只读）
0x0024  Reg_DoneTime      Done 持续时钟数（只读）
0x0028  Reg_TrigEna       触发源使能掩码（读写）
0x002C  Reg_MinRecPeriod  最小录制间隔（读写）
0x0030  Reg_EnableExtTrig 外部触发逐路使能（读写，复位=全 1）
0x0034..0x007C            保留（未使用）
0x0080  Mem_Addr_c        存储区起始（每通道一段）
```

注意两点：

1. **`0x0034`–`0x007C` 是保留空隙**：寄存器只用到 `0x0030`，存储区却从 `0x0080` 起，中间留空。这是因为封装层声明了 `USER_SLV_NUM_REG = 32` 个寄存器字（覆盖 `0x0000`–`0x007C`），存储区是另一段独立空间，从 `0x0080` 开始。
2. **`Mem_Addr_c` 不是寄存器，而是存储区起点**：它和上面的寄存器共享同一块 16 KiB AXI 空间，但语义不同——读写它会命中每通道的样本 RAM，而不是某个配置字。

#### 4.1.3 源码精读

地址常量全部定义在 package 头部，连续排列、一目了然：

寄存器地址常量（节选状态、配置、触发使能三组）：[hdl/data_rec_register_pkg.vhd:22-57](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L22-L57)

```vhdl
constant Reg_Stat_Addr_c        : integer := 16#0000#;
...
constant Reg_Cfg_Addr_c         : integer := 16#0004#;
...
constant Reg_TrigEna_Addr_c     : integer := 16#0028#;
...
constant Reg_MinRecPeriod_Addr_c: integer := 16#002C#;
constant Reg_EnableExtTrig_Addr_c: integer := 16#0030#;
```

存储区起点单独定义在最末尾，与寄存器拉开距离：[hdl/data_rec_register_pkg.vhd:60](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L60)

```vhdl
constant Mem_Addr_c : integer := 16#0080#;
```

封装层用 `USER_SLV_NUM_REG = 32` 声明寄存器字数，这正是 `0x0080/4 = 32`，印证了「寄存器占前 32 个字、存储区从第 32 个字（`0x80`）开始」的布局：[hdl/data_rec_vivado_wrp.vhd:116](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L116)

复位默认值 `RegRstVal_c` 也用地址常量来定位「外部触发使能」字，把它默认置全 1（上电即允许所有外部触发）：[hdl/data_rec_vivado_wrp.vhd:222-223](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L222-L223)

```vhdl
constant RegRstVal_c : t_aslv32(0 to USER_SLV_NUM_REG-1) :=
    (Reg_EnableExtTrig_Addr_c/4 => (others => '1'),  -- 上电默认使能所有外部触发
     others => (others => '0'));
```

这里 `Reg_EnableExtTrig_Addr_c/4` 就是手动版的 `ToWordAddr`，得到字地址 `0x30/4 = 12`。

#### 4.1.4 代码实践

**实践目标：** 用一张表把全部地址常量整理出来，并与封装层的解码逐一对照，确认「每个地址都被消费」。

**操作步骤：**

1. 打开 [`hdl/data_rec_register_pkg.vhd:22-60`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L22-L60)，按下表填写「字节地址 / 字地址 / 读或写」三列。
2. 打开封装层的解码段 [`hdl/data_rec_vivado_wrp.vhd:311-342`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L311-L342)，给每个寄存器补上「被谁解码」一列。
3. 找出哪些寄存器**没有**出现在 `reg_rdata` 回读列表里——它们就是只写寄存器。

**参考答案表：**

| 地址常量 | 字节地址 | 字地址 | 读写 | 用途 |
|----------|----------|--------|------|------|
| `Reg_Stat_Addr_c` | 0x0000 | 0 | RO | 当前状态机状态（bit0-3） |
| `Reg_Cfg_Addr_c` | 0x0004 | 1 | WO | `Arm`(bit0)、`TrgCntClr`(bit16)，写脉冲 |
| `Reg_Pretrig_Addr_c` | 0x0008 | 2 | RW | 预触发采样数 |
| `Reg_Totspl_Addr_c` | 0x000C | 3 | RW | 总采样数 |
| `Reg_SelftrigLo_Addr_c` | 0x0010 | 4 | RW | 自触发下限 |
| `Reg_SelftrigHi_Addr_c` | 0x0014 | 5 | RW | 自触发上限 |
| `Reg_SelftrigCfg_Addr_c` | 0x0018 | 6 | RW | 通道使能/OnExit/OnEnter |
| `Reg_SwTrig_Addr_c` | 0x001C | 7 | WO | 软件触发（sticky） |
| `Reg_TrigCnt_Addr_c` | 0x0020 | 8 | RO | 触发次数累计 |
| `Reg_DoneTime_Addr_c` | 0x0024 | 9 | RO | Done 持续时钟数 |
| `Reg_TrigEna_Addr_c` | 0x0028 | 10 | RW | 触发源掩码（Ext/Sw/Self） |
| `Reg_MinRecPeriod_Addr_c` | 0x002C | 11 | RW | 最小录制间隔 |
| `Reg_EnableExtTrig_Addr_c` | 0x0030 | 12 | RW | 外部触发逐路使能（复位=全 1） |
| `Mem_Addr_c` | 0x0080 | 32 | RW | 存储区起点（样本 RAM） |

**需要观察的现象：** `Reg_Cfg_Addr_c`（0x04）和 `Reg_SwTrig_Addr_c`（0x1C）在 [`hdl/data_rec_vivado_wrp.vhd:331-342`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L331-L342) 的回读列表里**不出现**——这正好证明它们是只写寄存器。

**预期结果：** 13 个寄存器 + 1 个存储区起点，字地址 13–31 为保留空隙。

#### 4.1.5 小练习与答案

**练习 1：** 软件想读「触发计数」，应发起一次 AXI 读事务到哪个字节地址？
**答案：** `Reg_TrigCnt_Addr_c = 0x0020`。

**练习 2：** 为什么 `RegRstVal_c` 里用 `Reg_EnableExtTrig_Addr_c/4` 而不直接写 `12`？
**答案：** 用地址常量除以 4 表达「字地址 12」，与 package 保持单一真相源；若日后地址重排，只需改 package，封装层自动正确。

---

### 4.2 字段位常量（`Reg_*_Idx_c` / `*_Sft_c`）

#### 4.2.1 概念说明

一个 32 位寄存器往往不止一个字段。比如 `Reg_TrigEna`（0x28）用最低 3 位分别表示「是否允许外部触发 / 软件触发 / 自触发」；`Reg_Cfg`（0x04）把 bit0 给 `Arm`、bit16 给 `TrgCntClr`。package 用两类常量记录这些位：

- `Reg_<Reg>_<Field>_Idx_c`：**Index**，某个 1 位字段的位号（用于单 bit 索引）。
- `Reg_<Reg>_<Field>_Sft_c`：**Shift**，某个字段的起始位号（用于「把一个值左移到这里」）。

两者本质上都是「位号」，区别只在命名风格与语义侧重：`_Idx_c` 多用于单 bit 标志（如 `ArmIdx=0`），`_Sft_c` 多用于可能多 bit 的字段（如自触发配置组 `ChEnaSft=0`、`ExitSft=8`、`EnterSft=16`）。测试平台里统一用 `值 * 2**位号` 的写法来置位，所以二者用法相同。

#### 4.2.2 核心流程

本讲最关键的三组字段位定义：

**(a) `Cfg` 寄存器（0x04）—— 控制位**

| 字段 | 常量 | 位号 | 含义 |
|------|------|------|------|
| Arm | `Reg_Cfg_ArmIdx_c` | 0 | 写 1 → 启动一次录制（单拍脉冲） |
| TrgCntClr | `Reg_Cfg_TrgCntClr_Idx_c` | 16 | 写 1 → 清零触发计数器 |

**(b) `TrigEna` 寄存器（0x28）—— 触发源掩码**

| 字段 | 常量 | 位号 | 含义 |
|------|------|------|------|
| 外部触发使能 | `Reg_TrigEna_ExtIdx_c` | 0 | 允许外部 `Trig_In` 触发 |
| 软件触发使能 | `Reg_TrigEna_SwIdx_c` | 1 | 允许软件写 `SwTrig` 触发 |
| 自触发使能 | `Reg_TrigEna_SelfIdx_c` | 2 | 允许数据落入范围触发 |

所以写入值 = `Ext·2⁰ + Sw·2¹ + Self·2²`。例如「只允许软件触发」写 `0b010 = 2`，「外部 + 自触发」写 `0b101 = 5`。

**(c) `SelftrigCfg` 寄存器（0x18）—— 自触发配置**

| 字段 | 常量 | 位号 | 含义 |
|------|------|------|------|
| 通道使能 | `Reg_SelftrigCfg_ChEnaSft_c` | 0 起（`NumOfInputs_g` 位） | 哪些通道参与自触发判定 |
| OnExit | `Reg_SelftrigCfg_ExitSft_c` | 8 | 数据**离开**范围时触发 |
| OnEnter | `Reg_SelftrigCfg_EnterSft_c` | 16 | 数据**进入**范围时触发 |

此外还有 `Reg_Stat_State*_c`（状态码 0–4，见 4.2.3）和 `Reg_SwTrig_TrigIdx_c`（软件触发位 = 0）。

#### 4.2.3 源码精读

`Cfg` 与状态码字段定义：[hdl/data_rec_register_pkg.vhd:22-31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L22-L31)

```vhdl
constant Reg_Stat_StateIdle_c     : integer := 0;
constant Reg_Stat_StatePreTrig_c  : integer := 1;
constant Reg_Stat_StateWaitTrig_c : integer := 2;
constant Reg_Stat_StatePostTrig_c : integer := 3;
constant Reg_Stat_StateDone_c     : integer := 4;
...
constant Reg_Cfg_Addr_c           : integer := 16#0004#;
constant Reg_Cfg_ArmIdx_c         : integer := 0;
constant Reg_Cfg_TrgCntClr_Idx_c  : integer := 16;
```

`TrigEna` 三触发源位定义（本讲最重要的一组）：[hdl/data_rec_register_pkg.vhd:50-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L50-L53)

```vhdl
constant Reg_TrigEna_Addr_c   : integer := 16#0028#;
constant Reg_TrigEna_ExtIdx_c : integer := 0;
constant Reg_TrigEna_SwIdx_c  : integer := 1;
constant Reg_TrigEna_SelfIdx_c: integer := 2;
```

`SelftrigCfg` 用 `_Sft_c` 风格定义三字段：[hdl/data_rec_register_pkg.vhd:38-41](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L38-L41)

```vhdl
constant Reg_SelftrigCfg_ChEnaSft_c : integer := 0;
constant Reg_SelftrigCfg_ExitSft_c  : integer := 8;
constant Reg_SelftrigCfg_EnterSft_c : integer := 16;
```

封装层如何消费这些位常量？以 `Cfg` 为例，把「写脉冲」与「数据位」相与，得到单拍控制脉冲：[hdl/data_rec_vivado_wrp.vhd:316-317](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L316-L317)

```vhdl
reg_cfg_arm       <= reg_wr(ToWordAddr(Reg_Cfg_Addr_c)) and reg_wdata(ToWordAddr(Reg_Cfg_Addr_c))(Reg_Cfg_ArmIdx_c);
reg_cfg_trigcntclr<= reg_wr(ToWordAddr(Reg_Cfg_Addr_c)) and reg_wdata(ToWordAddr(Reg_Cfg_Addr_c))(Reg_Cfg_TrgCntClr_Idx_c);
```

`SelftrigCfg` 则用区间直接切出多 bit 字段（通道使能 = 低 `NumOfInputs_g` 位）：[hdl/data_rec_vivado_wrp.vhd:322-324](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L322-L324)

```vhdl
reg_selftrigchena  <= reg_wdata(ToWordAddr(Reg_SelftrigCfg_Addr_c))(NumOfInputs_g-1 downto 0);
reg_selftrigonexit <= reg_wdata(ToWordAddr(Reg_SelftrigCfg_Addr_c))(Reg_SelftrigCfg_ExitSft_c);
reg_selftrigonenter<= reg_wdata(ToWordAddr(Reg_SelftrigCfg_Addr_c))(Reg_SelftrigCfg_EnterSft_c);
```

测试平台用 `值 * 2**位号` 置位，可读性极强（来自 case0）：[testbench/top_tb/top_tb_case0_pkg.vhd:82](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L82) 写 Arm；[testbench/top_tb/top_tb_case0_pkg.vhd:108](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L108) 写外部触发使能：

```vhdl
axi_single_write(Reg_Cfg_Addr_c,    1*2**Reg_Cfg_ArmIdx_c, ...);     -- 写 0x0001 启动
axi_single_write(Reg_TrigEna_Addr_c,1*2**Reg_TrigEna_ExtIdx_c, ...); -- 写 0x0001 允许外部触发
```

#### 4.2.4 代码实践

**实践目标：** 用字段位常量推算「软件应写入的数值」，并在测试平台里找到对应证据。

**操作步骤：**

1. 推算下列三种触发配置下，应写入 `Reg_TrigEna_Addr_c`（0x28）的十进制与十六进制值：
   - 只允许软件触发
   - 只允许自触发
   - 同时允许外部 + 自触发
2. 在 [`testbench/top_tb/top_tb_case3_pkg.vhd:59`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L59)（软件触发用例）和 [`testbench/top_tb/top_tb_case2_pkg.vhd:61`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L61)（自触发用例）里核对。
3. 思考：为什么 `Arm` 用「写脉冲」（`reg_wr and bit`）而不是普通电平寄存？

**需要观察的现象：** case3 写的是 `1*2**Reg_TrigEna_SwIdx_c = 2 = 0x2`；case2 写的是 `1*2**Reg_TrigEna_SelfIdx_c = 4 = 0x4`。

**预期结果：** 只软件 = `0b010 = 2`；只自触发 = `0b100 = 4`；外部 + 自触发 = `0b101 = 5`。`Arm` 用脉冲是因为每次写入应只触发**一次**录制，避免一次写持续多个周期导致重复 Arm（脉冲只生效一拍）。

**说明：** 本实践为源码阅读型，无需运行仿真即可完成推算与核对。

#### 4.2.5 小练习与答案

**练习 1：** 要让记录器「同时响应外部触发和软件触发」，应向 `Reg_TrigEna_Addr_c` 写什么值？
**答案：** `Ext·2⁰ + Sw·2¹ = 1 + 2 = 3 = 0x3`。

**练习 2：** `Reg_Cfg_TrgCntClr_Idx_c = 16`，软件清零触发计数应向 `Reg_Cfg_Addr_c` 写什么值？
**答案：** `1*2**16 = 0x10000`（见 [case0 第 153 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L153)）。

**练习 3：** 为什么 `SelftrigCfg` 用 `_Sft_c` 而 `TrigEna` 用 `_Idx_c`？
**答案：** 主要是命名风格：`SelftrigCfg` 里 `ChEna` 是多 bit 字段（起始位 = 移位量），故用 Shift 语义；`TrigEna` 三位都是独立 1 bit 标志，故用 Index。两者本质都是位号，测试平台用法相同。

---

### 4.3 `ToWordAddr` / `MemAddr` 函数

#### 4.3.1 概念说明

地址地图里还有两个「计算工具」：

- **`ToWordAddr(ByteAddr)`**：把字节地址换算成字地址（`/4`）。封装层用字地址索引 `reg_rd/reg_wr/reg_wdata/reg_rdata` 这四个长度为 32 的数组。
- **`MemAddr(channel, sample, memdepth)`**：给定通道号、样本号、存储深度，返回该样本的**字节地址**。这是软件/测试平台读取录制波形的入口公式。

理解 `MemAddr` 的关键是「通道间距」概念：每个通道独占一段连续样本空间，段大小是「向上取整到二次幂」的深度。

#### 4.3.2 核心流程

**字节↔字换算：**

\[ \text{WordAddr} = \text{ByteAddr} \,/\, 4 \]

**通道间距：** 为了让 AXI 地址的高位直接当「通道选择」、低位直接当「样本索引」，每个通道分到的样本数必须是对齐的二次幂：

\[ \text{ChannelSpacing} = 2^{\lceil \log_2(\text{memdepth}) \rceil} \]

- 当 `memdepth` 本身是二次幂（如 128）：`ChannelSpacing = memdepth`。
- 当 `memdepth` 是非二次幂（如 30）：`ChannelSpacing = 2^⌈log2(30)⌉ = 32`，比 30 大，留出少量空隙换取地址译码简单。

**样本字节地址：** 通道内每个样本占 4 字节（一个 32 位字）：

\[ \text{MemAddr} = \text{Mem\_Addr\_c} + (\text{channel}\cdot\text{ChannelSpacing} + \text{sample}) \times 4 \]

整体布局示意（以 `memdepth=128`、`NumOfInputs_g=4` 为例）：

```
0x0080 ┌────────────────────┐  Mem_Addr_c
       │ Ch0: 样本0..127     │  ← ChannelSpacing=128 样本 = 0x200 字节
0x0280 ├────────────────────┤
       │ Ch1: 样本0..127     │
0x0480 ├────────────────────┤
       │ Ch2: 样本0..127     │
0x0680 ├────────────────────┤
       │ Ch3: 样本0..127     │
0x0880 └────────────────────┘
```

软件读 Ch2 的第 5 个样本 → 命中 `MemAddr(2,5,128)`；AXI 高位自动选中 Ch2 的 RAM，低位选中样本 5（具体的 RAM 读出与 `FirstSplAddr` 环形对齐由 u5-l3 详讲）。

#### 4.3.3 源码精读

两个函数的声明在 package 头部：[hdl/data_rec_register_pkg.vhd:62-66](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L62-L66)

```vhdl
function ToWordAddr(ByteAddr : in integer) return integer;

function MemAddr(channel  : in integer;
                 sample   : in integer;
                 memdepth : in integer) return integer;
```

函数体同样简洁——`ToWordAddr` 就是除以 4：[hdl/data_rec_register_pkg.vhd:75-78](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L75-L78)

```vhdl
function ToWordAddr(ByteAddr : in integer) return integer is
begin
    return ByteAddr/4;
end function;
```

`MemAddr` 把上面两个公式合成一行：[hdl/data_rec_register_pkg.vhd:80-86](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L80-L86)

```vhdl
function MemAddr(channel, sample, memdepth : in integer) return integer is
    constant ChannelSpacing_c : integer := 2**log2ceil(memdepth);
begin
    return Mem_Addr_c + (channel*ChannelSpacing_c + sample)*4;
end function;
```

`log2ceil` 来自依赖库 `psi_common_math_pkg`（见 package 顶部的 `use`，[第 15 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L15)），它返回「≥输入的最小二次幂的指数」。

**封装层如何用 `ToWordAddr` 索引寄存器：** 几乎每条解码都先 `ToWordAddr(Reg_X_Addr_c)` 得到字地址，再用它索引 IPIC 数组。以状态回读为例：[hdl/data_rec_vivado_wrp.vhd:312-313](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L312-L313)

```vhdl
reg_rdata(ToWordAddr(Reg_Stat_Addr_c))(3 downto 0) <= reg_stat_state;
AckDone <= '1' when (reg_rd(ToWordAddr(Reg_Stat_Addr_c))='1') and (unsigned(reg_stat_state)=Reg_Stat_StateDone_c) else '0';
```

第二行还揭示了一个巧妙机制：**软件读状态寄存器且当前处于 Done 时，自动产生 `AckDone` 脉冲**，把状态机从 Done 拉回 Idle。也就是说，「读状态」本身兼任「确认 Done」。

**测试平台如何用 `MemAddr` 校验波形：** `CheckData` 过程对每个通道、每个样本算出期望值，再用 `MemAddr` 算出读取地址，经 AXI 比对：[testbench/top_tb/top_tb_pkg.vhd:142-147](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L142-L147)

```vhdl
for ch in 0 to NumOfInputs_c-1 loop
    for spl in 0 to samples-1 loop
        ExpVal_v := ch*2**(InputWidth_c-3) + spl + startValue;
        axi_single_expect(MemAddr(ch, spl, MemoryDepth_v), ExpVal_v, ms, sm, aclk, ...);
    end loop;
end loop;
```

这正是 `MemAddr` 的典型用法：**给地址地图里的存储区算出一个可读地址**。

#### 4.3.4 代码实践

**实践目标：** 手算 `MemAddr(ch=2, sample=5, memdepth=128)` 的字节地址，并验证它能被封装层的地址译码正确切成「通道 2 / 样本 5」。

**操作步骤：**

1. 算通道间距：`ChannelSpacing = 2**log2ceil(128) = 2**7 = 128`。
2. 代入公式：
   \[ \text{MemAddr} = 0x0080 + (2\times128 + 5)\times4 = 0x80 + 261\times4 = 0x80 + 1044 \]
3. 换算：`128 + 1044 = 1172 = 0x494`；字地址 = `1172/4 = 295 = 0x127`。
4. 反向校验封装层译码（`memdepth=128` 时 `log2ceil=7`）：
   - 去掉存储基址后的相对字节地址 = `0x494 - 0x80 = 0x414 = 1044`。
   - 样本索引位 = 相对地址的 bit[8:2] = `0x414` 的 bit8..2。

**需要观察的现象（待本地验证二进制切分）：** 把 `1044` 写成二进制 `100_0001_0100`，取 bit8..bit2 得 `000_0101 = 5`（样本 5 ✓）；取 bit10..bit9（通道选择 `AxiMemSel`）得 `10 = 2`（通道 2 ✓）。

**预期结果：** `MemAddr(2, 5, 128) = 0x494`（十进制 1172），对应通道 2、样本 5。封装层的 `AxiMemAdr <= mem_addr(log2ceil(MemoryDepth_g)+1 downto 2)` 与 `AxiMemSel <= mem_addr(log2ceil(MemoryDepth_g)+4 downto log2ceil(MemoryDepth_g)+2)` 正是用同一切分逻辑（见 [hdl/data_rec_vivado_wrp.vhd:513](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L513) 与 [hdl/data_rec_vivado_wrp.vhd:531](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L531)）。

**说明：** 二进制切分部分建议在本地用计算器或脚本核验；公式与十进制结果已确定。

#### 4.3.5 小练习与答案

**练习 1：** `MemAddr(0, 0, 128)` 等于多少？它的含义是什么？
**答案：** `0x80 + (0·128 + 0)·4 = 0x80`。含义：第 0 通道第 0 个样本，即存储区最首端。

**练习 2：** 若 `memdepth=30`（非二次幂），通道间距是多少？Ch1 样本 0 的字节地址是多少？
**答案：** `ChannelSpacing = 2**log2ceil(30) = 2**5 = 32`；`MemAddr(1,0,30) = 0x80 + (1·32 + 0)·4 = 0x80 + 128 = 0x100`。注意通道间距 32 > 30，每通道尾部有 2 个样本空隙——这正是非二次幂深度需要特殊处理（u3-l5 / u5-l3）的根源之一。

**练习 3：** 为什么 `ToWordAddr` 在封装层里到处出现，而 `MemAddr` 却只在测试平台里出现？
**答案：** 封装层的寄存器用「字地址索引数组」，所以每次都要把字节地址常量转成字地址；存储区则由 AXI slave 的 `mem_addr` 直接给出地址、用位切分译码，不需要 `MemAddr`。`MemAddr` 是给「想访问某通道某样本的软件/测试平台」用的便捷公式。

---

## 5. 综合实践

把三个最小模块串起来，完成一份「软件驱动初始化清单」的设计。

**场景：** 你在为这个 IP 写一段 Linux 用户态驱动初始化代码（伪代码即可）。需求是：上电后配置一次「外部触发、4 通道、每通道记录 100 个样本、其中前 30 个是预触发」，然后启动录制，最后在 Done 后读回 Ch2 的第 5 个样本。

**任务：**

1. 用本讲的地址常量与字段位常量，写出需要依次写入的（地址, 值）序列。提示：
   - 预触发数 → `Reg_Pretrig_Addr_c`，值 `30`。
   - 总采样数 → `Reg_Totspl_Addr_c`，值 `100`。
   - 触发源 → `Reg_TrigEna_Addr_c`，值用 `Reg_TrigEna_ExtIdx_c` 推算。
   - 启动 → `Reg_Cfg_Addr_c`，值用 `Reg_Cfg_ArmIdx_c` 推算。
2. 轮询 `Reg_Stat_Addr_c` 的低 4 位，等到它等于 `Reg_Stat_StateDone_c`（=4）。
3. 读 `Reg_Stat_Addr_c` 这一动作本身会自动 `Ack` 回 Idle——指出源码中哪一行保证了这一点。
4. 用 `MemAddr` 公式手算 Ch2 样本 5（`memdepth` 取 `MemoryDepth_g=128`）的读取地址。

**参考要点：**

- 写入序列：`(0x08, 30)`、`(0x0C, 100)`、`(0x28, 1*2**0 = 1)`（仅外部触发）、`(0x04, 1*2**0 = 1)`（Arm）。
- 轮询直到 `*(0x00) & 0xF == 4`。
- 自动 Ack 由 [hdl/data_rec_vivado_wrp.vhd:313](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L313) 的 `AckDone` 逻辑保证。
- Ch2 样本 5 读取地址 = `MemAddr(2,5,128) = 0x494`。

> 注意：本任务只产出「地址 + 值」清单与手算结果，不要求实现真实驱动；`MemoryDepth_g` 的实际值取决于 IP 例化参数，本例取 128 仅为演算。

## 6. 本讲小结

- `data_rec_register_pkg` 是地址地图的**唯一真相源**：13 个寄存器（`0x0000`–`0x0030`）+ 1 个存储区起点（`Mem_Addr_c = 0x0080`），全部用 `Reg_*_Addr_c` 常量定义。
- 字段位常量分 `_Idx_c`（单 bit 索引）与 `_Sft_c`（字段移位）两种风格，本质都是位号；`Cfg` 的 `Arm`/`TrgCntClr` 在 bit0/bit16，`TrigEna` 的 Ext/Sw/Self 在 bit0/1/2。
- `ToWordAddr(ByteAddr) = ByteAddr/4` 把字节地址换算成字地址，用于索引封装层的 IPIC 寄存器数组。
- `MemAddr(ch,spl,depth) = 0x80 + (ch·2^⌈log2(depth)⌉ + spl)·4` 给出任意通道/样本的字节地址；通道间距向上取整到二次幂，换取地址高位直接当通道选择。
- 复位默认值 `RegRstVal_c` 让外部触发使能上电为全 1；读状态寄存器在 Done 态会自动产生 `Ack` 脉冲。
- `Reg_Cfg` 与 `Reg_SwTrig` 没有回读，是只写寄存器；`Reg_Stat`/`Reg_TrigCnt`/`Reg_DoneTime` 是只读寄存器。

## 7. 下一步学习建议

有了地址地图，下一步可以分两个方向深入：

- **向下看「地址如何被消费」**：u3 系列讲 `data_rec` 核心如何使用 `PreTrigSpls`/`TotalSpls`/`TrigEna` 等端口（即本讲这些寄存器最终驱动的逻辑），从 u3-l1（实体与 generics）开始。
- **向深看「地址如何被译码」**：u5-l1 讲封装层如何用 `psi_common_axi_slave_ipif` 把 AXI 事务翻译成本讲这些 `reg_rd/reg_wr/mem_addr` 信号；u5-l3 讲每通道双端口 RAM 如何读出，以及 `FirstSplAddr` 如何把环形缓冲对齐成线性数据（呼应 4.3 里提到的「非二次幂空隙」）。
- **横向看「地址如何被软件用」**：u6-l3 讲 EPICS 模板生成器如何把通道/触发数展开成 db 记录，本质就是按本讲地址地图批量生成访问点。

建议先把本讲的地址表打印或抄录一份放在手边，后续阅读任何源码或写任何驱动时都能随时查阅。
