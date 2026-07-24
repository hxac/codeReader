# JESD204B 子系统

## 1. 本讲目标

JESD204B 是连接高速数据转换器（ADC/DAC）与 FPGA 的事实标准串行链路协议。本讲聚焦 SURF 在 `protocols/jesd204b/` 中的实现，学完后你应当能够：

- 用 **lane（通道）模型** 描述一条 JESD 链路：每条 lane 由收发器（GT）搬运的「数据字 + K 字符 + 错误标志」组成，并对应 `JesdGtRxLaneType` 记录与一条 `JesdRxLane` 接收通道。
- 画出一次链路建立的状态流转：**代码组同步（K28.5 对齐）→ ILAS 初始通道对齐序列 → 数据态**，并说清 `JesdSyncFsmRx` 的七个状态各自负责什么。
- 理解 **LMFC（本地多帧时钟）** 如何为「跨 lane 对齐」提供统一节拍，以及 **SYSREF 监测** 如何与确定性延迟挂钩。
- 能对照真实源码，把一个寄存器状态字的每一位映射回硬件事件。

本讲承接 u5-l4（8B/10B 线路码、K 字符定界）与 u1-l5（双进程 RTL 风格），是单元七高速链路协议的一环。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。如果你已熟悉 JESD204B，可跳到第 3 节。

**为什么需要 JESD204B？** 现代 ADC 一片芯片就有 8 个通道、每通道上 Gbps，若用传统并行 LVDS 引脚，PCB 走线等长与时序闭合几乎不可能。JESD204B 把并行采样数据「串行化」到少数几对高速收发器（lane）上，用 8B/10B 编码（见 u5-l4）在线路上自同步。代价是：串行链路上每条 lane 的数据到达 FPGA 的时刻存在抖动（lane-to-lane skew），必须重新对齐才能还原出「同一时刻」的采样。

**对齐靠什么？** 靠两个工具：

1. **K28.5（comma）代码组**：发送端周期性插入的 8B/10B 控制字符，接收端靠它找到「字边界」——即一个 32 位 GT 字里数据从哪个字节开始。
2. **LMFC + ILAS**：LMFC 是一个每 `K×F/GT_WORD_SIZE` 拍脉冲一次的本地节拍；ILAS（Initial Lane Alignment Sequence）是发送端在 LMFC 边界插入的、用 R/A 控制字符定界的对齐序列。各 lane 把 ILAS 期间的字先存进各自的弹性缓冲（elastic buffer），等到同一个 LMFC 边界再一齐读出，从而消除 lane 间的到达时差。

**确定性延迟靠什么？** 靠 **SYSREF**。SYSREF 是一个由系统主时钟派生、分发给发送端和所有接收端的慢速脉冲。各端都用 SYSREF 的上升沿去「校准」自己的 LMFC 计数器（让 LMFC 的第 0 拍对齐 SYSREF），于是从采样到出现在 FPGA 输出端的延迟就成了可预测的固定值——这就是 subclass 1 的确定性延迟。若不要求确定性延迟（subclass 0），则不必卡 SYSREF，链路也能通，但延迟会随每次上电变化。

**关键参数**（本讲会反复出现，全在 `Jesd204bPkg.vhd` 与实体泛型里）：

| 参数 | 含义 | 典型值 |
|------|------|--------|
| `GT_WORD_SIZE_C` | 一个 GT 字含多少字节 | 4（即 32 位） |
| `F_G` | 一帧含多少字节 | 2 |
| `K_G` | 一个多帧（multiframe）含多少帧 | 32 |
| `L_G` | 链路有多少 lane | 1–32 |
| `K×F` | 一个多帧的字节数 | 64 |

LMFC 周期 = `K×F / GT_WORD_SIZE` = `32×2/4` = 16 个 devClk 周期。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `protocols/jesd204b/rtl/` 下，仿真台在 `protocols/jesd204b/sim/`）：

| 文件 | 作用 |
|------|------|
| `Jesd204bPkg.vhd` | 协议包：GT 字宽常量、8B/10B K 字符常量、`JesdGtRxLane/TxLaneType` 记录、对齐/字节序函数、加扰器过程 |
| `Jesd204bRx.vhd` | 多 lane 接收顶层：SYSREF 调理、LMFC 生成、例化 `L_G` 条 `JesdRxLane`、汇总 `nSync` |
| `JesdRxLane.vhd` | 单 lane 接收：弹性缓冲 FIFO、同步 FSM、对齐与字符替换、状态字拼装 |
| `JesdSyncFsmRx.vhd` | **同步状态机**：IDLE→SYSREF→SYNC→HOLD→ALIGN→ILA→DATA 七态，是本讲核心 |
| `JesdLmfcGen.vhd` | LMFC 脉冲发生器，按 SYSREF 上升沿对齐 |
| `JesdIlasGen.vhd` | 发送侧 ILAS 字生成：在 LMFC 边界插入 A/R 控制字符 |
| `JesdAlignFrRepCh.vhd` | 字节对齐 + 控制字符替换 + 解扰 |
| `JesdSysrefMon.vhd` | SYSREF 周期最小/最大值监测，经异步 FIFO 上报 |
| `sim/Jesd204bTb.vhd` | VHDL 仿真台：TX↔RX 自环，含人为字节错位激励 |

构建清单 `ruckus.tcl` 把 `rtl/` 进综合、把 `sim/` 标 `sim_only`（见 u1-l2、u1-l3）。

## 4. 核心概念与源码讲解

### 4.1 Lane 模型：一条收发通道的数据结构

#### 4.1.1 概念说明

JESD204B 是**点对点、多 lane** 的串行链路：一条「链路」由 `L_G` 条物理 lane 并行组成，每条 lane 由一对 GTH/GTY 收发器搬运一个 32 位（`GT_WORD_SIZE_C=4` 字节）的 GT 字。

SURF 把「一条 lane 从 GT 收到的一切」打包成一个记录 `JesdGtRxLaneType`：不只是 32 位数据，还包括每字节是否为 K 控制字符、每字节的 8B/10B 解码错误、以及该 lane 收发器的复位完成与 CDR 稳定标志。用记录（而非扁平端口）的好处与 AXI 总线一致（见 u3-l1、u4-l1）：端口干净、可整体传入过程、可声明数组一次性描述所有 lane。

接收侧的层次是 **`Jesd204bRx`（多 lane 顶层）→ 例化 `L_G` 条 `JesdRxLane`（单 lane）→ 每条内含一个 `JesdSyncFsmRx`（状态机）**。本模块只讲「数据结构 + 顶层例化」，状态机留到 4.2。

#### 4.1.2 核心流程

单条 lane 的数据流（接收方向）如下：

```
GT 收发器
   │  data(32b) + dataK(4b) + dispErr(4b) + decErr(4b) + rstDone + cdrStable
   ▼
JesdGtRxLaneType 记录  ── 打包成一条 lane 的输入
   ▼
JesdRxLane
   ├─ 弹性缓冲 FIFO（按 LMFC 对齐多 lane）
   ├─ JesdSyncFsmRx（代码组同步 / ILAS / 数据态）
   └─ JesdAlignFrRepCh（字对齐 + K 字符替换 + 解扰）
   ▼
sampleData_o(32b) + dataValid_o   ← 还原出的并行采样
```

多 lane 汇总：每条 lane 给出自己的 `nSync`（同步请求/已完成）信号，`Jesd204bRx` 用 `uAnd` 把所有 lane 的 `nSync` 「与」起来——只有**所有**已使能的 lane 都同步成功，整条链路才算 `nSync`。

#### 4.1.3 源码精读

**GT 字宽与 8B/10B K 字符常量**。`GT_WORD_SIZE_C=4` 决定了后续一切位宽；四个 K 字符各有专门用途（见 [Jesd204bPkg.vhd:28-38](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bPkg.vhd#L28-L38)）：

```vhdl
constant GT_WORD_SIZE_C : positive := 4;
constant K_CHAR_C : slv(7 downto 0) := x"BC";  -- K.28.5  代码组同步/comma
constant R_CHAR_C : slv(7 downto 0) := x"1C";  -- K.28.0  ILAS 起始
constant A_CHAR_C : slv(7 downto 0) := x"7C";  -- K.28.3  ILAS 多帧边界
constant F_CHAR_C : slv(7 downto 0) := x"FC";  -- K.28.7  帧对齐字符
```

**单 lane 输入记录**。把一条 lane 从 GT 收到的全部信号打包（见 [Jesd204bPkg.vhd:59-74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bPkg.vhd#L59-L74)）：

```vhdl
type JesdGtRxLaneType is record
   data      : slv((GT_WORD_SIZE_C*8)-1 downto 0);  -- PHY 接收数据（32 位）
   dataK     : slv(GT_WORD_SIZE_C-1 downto 0);      -- 每字节是否为 K 字符
   dispErr   : slv(GT_WORD_SIZE_C-1 downto 0);      -- 每字节 disparity 错误
   decErr    : slv(GT_WORD_SIZE_C-1 downto 0);      -- 每字节 not-in-table 错误
   rstDone   : sl;                                   -- 收发器复位完成
   cdrStable : sl;                                   -- CDR 时钟恢复稳定
end record;
```

注意 `dataK/dispErr/decErr` 都是**每字节一位**——因为 8B/10B 解码器对 GT 字里的每个字节独立工作，任一字节出错都能定位。

**记录数组描述整条链路**。在 [Jesd204bPkg.vhd:85-86](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bPkg.vhd#L85-L86)，一行就声明了「任意多条 lane」的集合类型：

```vhdl
type JesdGtRxLaneTypeArray is array (natural range <>) of JesdGtRxLaneType;
```

**多 lane 顶层例化**。`Jesd204bRx` 用一个 generate 循环例化 `L_G` 条单 lane 接收器，共享同一个 LMFC 与 SYSREF 上升沿（见 [Jesd204bRx.vhd:313-338](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bRx.vhd#L313-L338)）：

```vhdl
generateRxLanes : for i in L_G-1 downto 0 generate
   JesdRx_INST : entity surf.JesdRxLane
      port map (
         devClk_i     => devClk_i,
         sysRef_i     => s_sysrefRe,   -- 所有 lane 共享 SYSREF 上升沿
         r_jesdGtRx   => s_jesdGtRxArr(i),
         lmfc_i       => s_lmfc,       -- 所有 lane 共享同一个 LMFC
         nSyncAny_i   => s_nSyncAny,
         nSync_o      => s_nSyncVec(i),
         ...);
end generate;
```

**所有 lane 同步的「与」汇总**。在 [Jesd204bRx.vhd:357-362](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bRx.vhd#L357-L362)：未使能的 lane 其 `nSync` 视为「不参与」（置高放行），使能的 lane 才真正参与「与」：

```vhdl
syncVectEn : for i in L_G-1 downto 0 generate
   s_nSyncVecEn(i) <= s_nSyncVec(i) or not s_enableRx(i);  -- 未使能则放行
end generate;
s_nSyncAny <= '0' when allBits(s_enableRx, '0')
              else uAnd(s_nSyncVecEn);  -- 所有使能 lane 都 nSync 才为 1
```

**顶层泛型与合法性断言**。`Jesd204bRx` 把 JESD 关键参数都做成泛型（见 [Jesd204bRx.vhd:40-57](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bRx.vhd#L40-L57)），并在 elaboration 期用 assert 拦截非法组合（见 [Jesd204bRx.vhd:168-169](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bRx.vhd#L168-L169)）：

```vhdl
assert (((K_G * F_G) mod GT_WORD_SIZE_C) = 0) report "K_G setting is incorrect" severity failure;
assert (F_G = 1 or F_G = 2 or (F_G = 4 and GT_WORD_SIZE_C = 4)) ...
```

即多帧字节数 `K×F` 必须能被 GT 字宽整除（否则 LMFC 周期不是整数拍）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把 lane 数据结构到顶层例化串起来，理解「一条 lane」在代码里到底是什么。

**操作步骤**：

1. 打开 `Jesd204bPkg.vhd` 第 59–86 行，确认一条 lane 的记录字段数，以及数组类型是几维。
2. 打开 `Jesd204bRx.vhd`，搜索 `generateRxLanes`，确认 `L_G` 条 lane 共享了哪几个信号（答案应含 `s_sysrefRe` 与 `s_lmfc`）。
3. 在 `Jesd204bRx.vhd` 顶层端口（第 87 行附近）确认输入 `r_jesdGtRxArr` 的类型正是 `jesdGtRxLaneTypeArray(L_G-1 downto 0)`。

**需要观察的现象**：所有 lane 的 `lmfc_i`、`sysRef_i` 来自**同一个**信号源，而 `r_jesdGtRx` 是**按 lane 独立**的数组元素。这正是「跨 lane 对齐靠共享节拍，单 lane 数据各自独立」的体现。

**预期结果**：你能用一句话回答「为什么 lane 间能对齐」——因为它们共享同一个 LMFC 边界作为读出基准。

#### 4.1.5 小练习与答案

**练习 1**：`JesdGtRxLaneType` 里为什么 `dispErr` 是 4 位而不是 1 位？
**答案**：8B/10B 解码器对 GT 字里的每个字节独立解码，4 位 `dispErr`（`GT_WORD_SIZE_C-1 downto 0`）给每字节一个 disparity 错误标志，便于定位是哪个字节出错（见 [Jesd204bPkg.vhd:62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bPkg.vhd#L62)）。

**练习 2**：若 `L_G=4` 但只有 lane 0、1 被使能，`s_nSyncAny` 何时为 1？
**答案**：未使能的 lane 2、3 经 `s_nSyncVecEn(i) <= ... or not s_enableRx(i)` 被强制放行为 1；只要 lane 0、1 各自的 `nSync` 都为 1，`uAnd(s_nSyncVecEn)` 即为 1（见 [Jesd204bRx.vhd:357-362](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bRx.vhd#L357-L362)）。

---

### 4.2 ILAS 与 LMFC：跨 lane 对齐的节拍与序列

#### 4.2.1 概念说明

单条 lane 拿到 32 位数据后，还要解决两个问题：

1. **字边界**：数据从 32 位字的哪个字节开始？靠检测 K28.5（comma）代码组定位。
2. **lane 间对齐**：不同 lane 的数据到达时刻不同，怎么对齐到「同一采样时刻」？靠 LMFC + ILAS + 弹性缓冲。

**LMFC（Local Multi-Frame Clock）** 不是一根外部时钟线，而是接收端在 devClk 域**自己产生**的一个周期性单拍脉冲，周期为 `K×F/GT_WORD_SIZE` 个 devClk。它的「第 0 拍」被 SYSREF 上升沿校准——这就是 LMFC 与 SYSREF 的耦合点。

**ILAS（Initial Lane Alignment Sequence）** 是发送端在链路同步后发出的一段特殊序列：在连续若干个 LMFC 边界上插入 R（K.28.0，序列起始）和 A（K.28.3，多帧边界）控制字符。接收端各 lane 把这段序列期间收到的字先**写进各自的弹性缓冲 FIFO 但暂不读出**，直到收到完整的 ILAS、且到达一个 LMFC 边界时，再**同时**开始读出——于是各 lane 的数据被对齐到了同一个 LMFC 节拍上。

整个接收同步过程被 `JesdSyncFsmRx` 编排成七个状态的状态机。

#### 4.2.2 核心流程

`JesdSyncFsmRx` 的状态流转（subclass 1，确定性延迟）：

```
IDLE_S ── SYSREF∧K稳定∧使能∧GT就绪 ──▶ SYSREF_S
SYSREF_S ── K28.5 检测到 ∧ LMFC 边界 ──▶ SYNC_S    （拉高 nSync 请求对齐）
SYNC_S ── 首个非 K 字（数据开始） ──▶ HOLD_S          （此时关掉缓冲读，开始囤字）
HOLD_S ── 下一个 LMFC 边界 ──▶ ALIGN_S                （对齐字边界）
ALIGN_S ──（无条件）──▶ ILA_S                         （锁定对齐位置，重置 ILAS 计数）
ILA_S ── 计满 NUM_ILAS_MF_G 个 LMFC ──▶ DATA_S        （ILAS 结束，数据有效）
DATA_S ── 任一 lane 失同步/链路错/失能 ──▶ IDLE_S      （回到起点重新对齐）
```

代码组同步的「稳定」判定：K28.5 必须在**连续 4 个**时钟周期都被检测到（当前拍 + 3 拍延迟寄存），才认为 comma 稳定，避免毛刺误触发。

LMFC 脉冲数学：周期计数器从 0 数到 `PERIOD_C = K×F/GT_WORD_SIZE − 1` 后归零并发脉冲。以默认 `K=32, F=2, GT_WORD_SIZE=4`：

\[
\text{LMFC 周期} = \frac{K \cdot F}{\text{GT\_WORD\_SIZE}} = \frac{32 \times 2}{4} = 16 \text{ 个 devClk}
\]

#### 4.2.3 源码精读

**LMFC 发生器**。`JesdLmfcGen` 用双进程骨架（见 u1-l5）维护一个周期计数器，并在 SYSREF 上升沿把计数器清零、把 LMFC 对齐到 SYSREF（见 [JesdLmfcGen.vhd:51](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdLmfcGen.vhd#L51) 与 [JesdLmfcGen.vhd:80-97](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdLmfcGen.vhd#L80-L97)）：

```vhdl
constant PERIOD_C : positive := ((K_G * F_G)/GT_WORD_SIZE_C)-1;  -- = 15
...
v.sysrefRe := sysref_i and not r.sysrefD1;             -- SYSREF 上升沿
if (r.sysrefRe = '1' and nSync_i = '0') then           -- 仅在未同步时对齐
   v.cnt  := (others => '0');
   v.lmfc := '1';                                       -- 第 0 拍脉冲
elsif (r.cnt = PERIOD_C) then
   v.cnt  := (others => '0');
   v.lmfc := '1';                                       -- 周期性脉冲
else
   v.cnt  := r.cnt + 1;
   v.lmfc := '0';
end if;
```

注意 `nSync_i = '0'` 的守卫：只在链路尚未同步时才允许 SYSREF 重新对齐 LMFC，避免数据态被 SYSREF 打断。

**状态机的稳定 comma 判定**。连续 4 拍检测 K28.5（见 [JesdSyncFsmRx.vhd:154-156](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSyncFsmRx.vhd#L154-L156)）：

```vhdl
s_kDetected <= detKcharFunc(dataRx_i, chariskRx_i, GT_WORD_SIZE_C);
-- 连续三个延迟寄存器也都为 1，才认为 comma 稳定
s_kStable   <= s_kDetected and r.kDetectRegD1 and r.kDetectRegD2 and r.kDetectRegD3;
```

`detKcharFunc` 要求 GT 字的**所有 4 个字节**都是 K28.5 且 `charisk` 全 1，才返回 1（见 [Jesd204bPkg.vhd:163-173](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bPkg.vhd#L163-L173)）。

**IDLE→SYSREF 的转移条件分 subclass**。subclass 1 多卡一个 SYSREF（见 [JesdSyncFsmRx.vhd:189-197](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSyncFsmRx.vhd#L189-L197)）：

```vhdl
if subClass_i = '1' then
   if sysRef_i = '1' and enable_i = '1' and nSyncAnyD1_i = '0'
      and gtReady_i = '1' and s_kStable = '1' then
      v.state := SYSREF_S;
   end if;
else  -- subclass 0：不需要 SYSREF
   if enable_i = '1' and gtReady_i = '1' and s_kStable = '1' then
      v.state := SYSREF_S;
   end if;
end if;
```

**HOLD 状态关闭缓冲读**。这是跨 lane 对齐的关键：进入 HOLD 后 `readBuff_o := '0'`，弹性缓冲只写不读，把 ILAS 期间的字囤起来；等到 LMFC 边界再进 ALIGN 恢复读出（见 [JesdSyncFsmRx.vhd:237-253](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSyncFsmRx.vhd#L237-L253)）。

**ILAS 计数到 DATA**。`ILA_S` 数够 `NUM_ILAS_MF_G`（默认 4）个 LMFC 后进入数据态（见 [JesdSyncFsmRx.vhd:284-295](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSyncFsmRx.vhd#L284-L295)）：

```vhdl
if (lmfc_i = '1') then
   v.cnt := r.cnt + 1;                    -- 数 LMFC 边界
end if;
if r.cnt = NUM_ILAS_MF_G then
   v.state := DATA_S;                      -- ILAS 结束，数据有效
   v.dataValid := '1';
elsif enable_i = '0' or s_kStable = '1' then
   v.state := IDLE_S;                      -- 中途又看到 comma 说明失步，重来
end if;
```

**单 lane 的弹性缓冲 FIFO**。`JesdRxLane` 例化一个 `FifoSync`，写使能由 FSM 的 `readBuff` 反相控制（见 [JesdRxLane.vhd:165-196](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdRxLane.vhd#L165-L196)）：

```vhdl
s_bufRst <= devRst_i or not s_nSync or not enable_i;
s_bufWe  <= not s_bufRst and not s_bufFull;        -- 使能且未满就写
s_bufRe  <= r.bufWeD1 and s_readBuff;              -- 由 FSM 的 readBuff 控制何时读
```

**发送侧的 ILAS 字生成**。`JesdIlasGen` 在 LMFC 边界（延迟 1、2 拍对齐）插入 A 字符到字高位、R 字符到字低位（见 [JesdIlasGen.vhd:78-89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdIlasGen.vhd#L78-L89)）：

```vhdl
if enable_i = '1' and ilas_i = '1' then
   if r.lmfcD1 = '1' then                      -- LMFC 后第 1 拍：发 A 字符（高位）
      vIlasData(high downto high-7) := A_CHAR_C;
      vIlasK(high) := '1';
   end if;
   if r.lmfcD2 = '1' then                      -- LMFC 后第 2 拍：发 R 字符（低位）
      vIlasData(7 downto 0) := R_CHAR_C;
      vIlasK(0) := '1';
   end if;
end if;
```

#### 4.2.4 代码实践（仿真台阅读 + 可选 GHDL 运行）

**目标**：对照 `sim/Jesd204bTb.vhd` 理解一次完整对齐过程，并定位 LMFC 在其中的作用。

**操作步骤**：

1. 打开 `sim/Jesd204bTb.vhd`，看第 31 行 `CLK_PERIOD_C : time := 1 us` 与第 133 行 `sysRef <= cnt(6)`——推导出 SYSREF 周期是多少个 devClk（提示：`cnt(6)` 翻转周期 = 128 拍，SYSREF 一个完整周期 = 128 拍）。
2. 看第 42 行 `BYTE_SHIFT_C : natural := 3` 与第 176–177 行——这是仿真台**故意**把 GT 数据字节错位喂给 RX，用来验证 `JesdAlignFrRepCh` 的字对齐能力。说明它对应 `JesdAlignFrRepCh` 里 `detectPosFuncSwap` 返回的哪种 position。
3. 看第 253–267 行的配置序列：RX 写 `x"00000004" <= x"0000000B"`（即 `SysrefDelay=8`）、`x"00000010" <= x"00000023"`（scrEnable、subClass=1、replaceEnable）。把这些值与本讲讲的 subclass 1 + 加扰 + 字符替换对应起来。
4. 跟踪 `nSync` 信号：它由 `Jesd204bRx` 输出（第 242 行），反馈给 `Jesd204bTx` 的 `nSync_i`（第 155 行），形成收发自同步环。说明 DATA_S 状态下若某 lane 出错会怎样回到 IDLE。

**可选运行（待本地验证）**：该仿真台是纯 VHDL（用 `axiLiteBusSimWrite` 过程驱动），可用 GHDL 编译运行，命令类似（具体语法依本机 GHDL/ruckus 版本，**待本地验证**）：

```bash
make MODULES=$PWD analysis    # 先做全仓库语法分析（见 u1-l2）
# 再用 GHDL 加载 surf 库后 elaborate Jesd204bTb 并运行，观察 nSync/dataValid 波形
```

**需要观察的现象**：复位释放后，`nSync` 会先为 0，经过若干 SYSREF 周期与 ILAS 后跳 1；同时 `dataValid` 跳 1，`rxData` 开始与 `nextRxData` 逐拍比较无错（`rxDataErrorDet` 保持 0）。

**预期结果**：你能指出「LMFC 边界」是 SYNC→HOLD→ALIGN 跳转和 ILAS 计数的统一节拍，没有 LMFC 就无法让多 lane 在同一拍恢复读出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `JesdLmfcGen` 里 SYSREF 对齐 LMFC 的条件要加 `nSync_i = '0'`？
**答案**：只在链路未同步时才允许 SYSREF 重新校准 LMFC 计数器；一旦进入数据态，LMFC 必须自由运行，否则会因 SYSREF 抖动而错乱数据节拍（见 [JesdLmfcGen.vhd:88](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdLmfcGen.vhd#L88)）。

**练习 2**：`ILA_S` 状态里 `s_kStable = '1'` 为什么会触发回到 `IDLE_S`？
**答案**：ILAS 期间正常收到的应是 R/A 控制字符与数据，若此时又检测到稳定的 K28.5 comma，说明链路重新进入了代码组同步阶段（对齐已乱），必须从头来过（见 [JesdSyncFsmRx.vhd:293-294](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSyncFsmRx.vhd#L293-L294)）。

**练习 3**：弹性缓冲 FIFO 的读使能 `s_bufRe` 何时被拉低？意义何在？
**答案**：HOLD 状态下 `readBuff_o='0'`，故 `s_bufRe` 为 0，缓冲只写不读，把各 lane 的 ILAS 字囤住，直到 ALIGN/Data 态统一读出，从而吸收 lane 间 skew（见 [JesdSyncFsmRx.vhd:240](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSyncFsmRx.vhd#L240) 与 [JesdRxLane.vhd:167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdRxLane.vhd#L167)）。

---

### 4.3 SYSREF 监测：确定性延迟的健康体检

#### 4.3.1 概念说明

确定性延迟要求 SYSREF 与 devClk 之间满足严格的周期关系：SYSREF 的上升沿必须**恰好**落在 devClk 的某个固定相位上，且 SYSREF 周期必须是 LMFC 周期的整数倍。若 SYSREF 周期不稳定（抖动、分频错误），LMFC 的对齐基准就会漂移，确定性延迟失效。

`JesdSysrefMon` 不参与数据通路，它是一个**旁路体检器**：在 devClk 域连续测量两次 SYSREF 上升沿之间的 devClk 周期数，统计其历史**最大值与最小值**，再经异步 FIFO 跨域送到 AXI-Lite，供软件读取。若 `max ≠ min`，就说明 SYSREF 周期不稳定，确定性延迟不可信。

这与 u4-l4 的 `AxiStreamMon` 思路一致——都是「旁路统计、不影响数据、跨域上报」。

#### 4.3.2 核心流程

`JesdSysrefMon` 内部用一个 16 位自由计数器 `cnt`，每拍 +1；每检测到一次 SYSREF 边沿（`sysrefEdgeDet_i` 上升沿脉冲）就把当前 `cnt` 作为「本次周期」采样，并复位计数器：

- 复位后的**第一次**边沿：只做「武装」（`armed := "01"`），不记录——因为此时 `cnt` 是从随机时刻开始数的，无意义。
- **第二次**边沿：得到第一个有效周期，同时初始化 `min` 和 `max`。
- **之后每次**边沿：把本次周期与历史 `min/max` 比较、更新。

统计值经 `SynchronizerFifo`（32 位，低 16 位 min、高 16 位 max）从 devClk 跨到 axilClk 域输出。

#### 4.3.3 源码精读

**接口与两个时钟域**。输入 `sysrefEdgeDet_i` 在 devClk 域，输出 min/max 在 axilClk 域（见 [JesdSysrefMon.vhd:24-36](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSysrefMon.vhd#L24-L36)）：

```vhdl
port (
   devClk          : in  sl;
   sysrefEdgeDet_i : in  sl;       -- devClk 域：SYSREF 边沿脉冲
   axilClk         : in  sl;
   statClr         : in  sl;       -- axilClk 域：清零
   sysRefPeriodmin : out slv(15 downto 0);  -- axilClk 域输出
   sysRefPeriodmax : out slv(15 downto 0));
```

**armed 两态防首拍污染**。复位后第一次边沿只武装、不采样，第二次才正式建立 min/max（见 [JesdSysrefMon.vhd:85-100](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSysrefMon.vhd#L85-L100)）：

```vhdl
if (r.armed = "00") then
   v.armed := "01";                       -- 第一次：仅武装
elsif (r.armed = "01") then
   v.armed := "11";                       -- 第二次：建立基准
   v.sysRefPeriodmax := r.cnt;
   v.sysRefPeriodmin := r.cnt;
else                                      -- 正常态：滚动更新极值
   if (r.cnt > r.sysRefPeriodmax) then v.sysRefPeriodmax := r.cnt; end if;
   if (r.cnt < r.sysRefPeriodmin) then v.sysRefPeriodmin := r.cnt; end if;
end if;
```

**清零的跨域**。`statClr` 来自 axilClk 域，用 `SynchronizerOneShot` 压成 devClk 域单拍脉冲（见 u2-l1）再复位统计（见 [JesdSysrefMon.vhd:60-66](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSysrefMon.vhd#L60-L66) 与 [JesdSysrefMon.vhd:119-121](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSysrefMon.vhd#L119-L121)）。

**结果跨域上报**。把 32 位（min+max 拼接）一次性写入 `SynchronizerFifo`，保证 min 与 max 是**同一拍**采到的成对值，不会错位（见 [JesdSysrefMon.vhd:135-145](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSysrefMon.vhd#L135-L145)）：

```vhdl
U_sync : entity surf.SynchronizerFifo
   generic map (DATA_WIDTH_G => 32)
   port map (
      wr_clk            => devClk,
      din(15 downto 0)  => r.sysRefPeriodmin,
      din(31 downto 16) => r.sysRefPeriodmax,
      rd_clk            => axilClk,
      dout(15 downto 0)  => sysRefPeriodmin,
      dout(31 downto 16) => sysRefPeriodmax);
```

**SYSREF 上升沿在顶层如何产生**。`Jesd204bRx` 把同步后的 SYSREF 延迟（`SlvDelay`，可配 1–256 拍，由 AXI 寄存器 `sysrefDlyRx` 控制）送进 `JesdLmfcGen`，后者输出的 `sysrefRe_o`（SYSREF 上升沿）就是喂给各 `JesdRxLane` 的 `sysRef_i`（见 [Jesd204bRx.vhd:281-306](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/Jesd204bRx.vhd#L281-L306)）。`SlvDelay` 让软件能微调 SYSREF 相对 LMFC 的相位，是确定性延迟调优的旋钮。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解 SYSREF 监测如何反映确定性延迟的健康度。

**操作步骤**：

1. 打开 `JesdSysrefMon.vhd`，确认它有**几个**进程、分别跑在哪个时钟域（devClk 的 comb/seq + axilClk 的输出端）。
2. 假设 devClk = 250 MHz（4 ns），SYSREF 标称周期 = 128 个 devClk。问：软件读回的 `sysRefPeriodmin/max` 期望值是多少？若读到 `min=127, max=129`，说明什么？
3. 在 `Jesd204bRx.vhd` 找到 `SlvDelay`（第 281 行）与 `JesdSysrefMon` 的调用关系，确认 SYSREF 上升沿是经过延迟后才用于 LMFC 对齐的。

**需要观察的现象**：`JesdSysrefMon` 完全不在数据通路上——即使去掉它，链路照常工作；它只读取 SYSREF 边沿做统计。

**预期结果**：你能解释「`max ≠ min` ⇒ SYSREF 周期抖动 ⇒ LMFC 对齐基准不稳 ⇒ 确定性延迟不可信」这条因果链。期望读数为 128（按仿真台 `cnt(6)` 推算）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 min/max 要拼成 32 位一次性跨域，而不是分两次送？
**答案**：min 与 max 是同一历史窗口内成对采样的极值；若分两次跨域，可能在两次读取之间又更新了一次，导致 min 与 max 来自不同时刻，失去「同窗口极值」语义。拼成 32 位经一个 `SynchronizerFifo` 原子跨域可保证成对一致（见 [JesdSysrefMon.vhd:135-145](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSysrefMon.vhd#L135-L145)，并对照 u2-l2 异步 FIFO 原子跨域）。

**练习 2**：`armed` 为什么用两位（`"00"/"01"/"11"`）而不是一个布尔？
**答案**：需要区分「复位后从未见过边沿」「见过一次边沿」「见过两次及以上」三种状态；两位恰好编码这三个阶段，使首拍（计数器从随机值开始）不污染统计（见 [JesdSysrefMon.vhd:86-101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/jesd204b/rtl/JesdSysrefMon.vhd#L86-L101)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「带教式」链路建立追踪。这是一道**源码阅读 + 推理**的综合题，不要求运行硬件。

**任务**：给定配置 `subClass=1`（确定性延迟）、`F_G=2, K_G=32, GT_WORD_SIZE_C=4, L_G=1`、加扰使能、字符替换使能，按时间顺序回答下列问题，每问都给出对应源码的文件与行号依据。

1. **复位释放后**：`JesdLmfcGen` 的计数器周期是多少？SYSREF 上升沿何时会把 LMFC 对齐？（依据 `JesdLmfcGen.vhd:51,88`）
2. **代码组同步**：RX 需要连续看到几拍 K28.5 才认为 comma 稳定？此时 `JesdSyncFsmRx` 从哪个状态跳到哪个状态？（依据 `JesdSyncFsmRx.vhd:154-156,189-197`）
3. **ILAS 期间**：发送端在 LMFC 边界后第 1、2 拍分别插入哪个 K 字符？接收端的弹性缓冲 FIFO 此时的读使能是什么状态？（依据 `JesdIlasGen.vhd:80-88` 与 `JesdSyncFsmRx.vhd:237-253`、`JesdRxLane.vhd:167`）
4. **进入数据态**：`ILA_S` 要数够几个 LMFC 才进 `DATA_S`？进入后哪一根信号变高表示数据有效？（依据 `JesdSyncFsmRx.vhd:284-295`）
5. **LMFC 的作用总结**：用一句话说明 LMFC 在第 2、3、4 步中扮演了什么统一角色。
6. **健康体检**：链路运行中，软件读 `JesdSysrefMon` 的 min/max 各为多少才算 SYSREF 稳定？若不一致，对确定性延迟有何影响？（依据 `JesdSysrefMon.vhd:85-114`）

**参考答案要点**：
1. LMFC 周期 16 拍；SYSREF 上升沿在 `nSync_i='0'`（未同步）时清零计数器并对齐。
2. 连续 4 拍（当前 + 3 拍延迟）；`IDLE_S → SYSREF_S`。
3. 第 1 拍插 A（K.28.3）到字高位，第 2 拍插 R（K.28.0）到字低位；HOLD 态 `readBuff='0'`，FIFO 读使能 `s_bufRe=0`，只写不读。
4. 数够 `NUM_ILAS_MF_G=4` 个 LMFC；`dataValid_o` 变高。
5. LMFC 是代码组同步→ILAS→数据态各阶段跳转与 ILAS 计数、弹性缓冲统一读出的**公共节拍**。
6. min=max（仿真台下应为 128）才算稳定；不一致则 LMFC 对齐基准漂移，确定性延迟失效。

## 6. 本讲小结

- **Lane 模型**：一条 lane = 一个 `JesdGtRxLaneType` 记录（32 位 data + 每字节的 dataK/dispErr/decErr + rstDone/cdrStable）；`Jesd204bRx` 例化 `L_G` 条 `JesdRxLane`，所有 lane 共享 LMFC 与 SYSREF 上升沿，`nSync` 取所有使能 lane 的「与」。
- **代码组同步**：靠 K28.5（comma）连续 4 拍稳定检测定位字边界；`detKcharFunc` 要求整字 4 字节都是 K28.5。
- **ILAS + LMFC + 弹性缓冲**：LMFC 是周期为 `K×F/GT_WORD_SIZE`（默认 16 拍）的自产脉冲；ILAS 期间各 lane 把字写入弹性 FIFO 但 HOLD 态暂停读出，待 LMFC 边界统一读出，消除 lane 间 skew。
- **七态状态机**：`IDLE→SYSREF→SYNC→HOLD→ALIGN→ILA→DATA`，subclass 1 在 IDLE 多卡一个 SYSREF；任一 lane 在 DATA 态出错即回 IDLE 重来。
- **SYSREF 监测**：`JesdSysrefMon` 是旁路体检器，测 SYSREF 周期的 min/max 并经 `SynchronizerFifo` 原子跨域上报；`max≠min` 即确定性延迟不可信。
- **延迟旋钮**：`SlvDelay`（1–256 拍，AXI 寄存器 `sysrefDlyRx` 可配）让软件微调 SYSREF 相位，是确定性延迟调优手段。

## 7. 下一步学习建议

- **发送侧**：本讲侧重 RX，建议接着读 `Jesd204bTx.vhd`、`JesdTxLane.vhd`、`JesdSyncFsmTx.vhd`，对照理解发送端如何产生 K28.5、ILAS（R/A 字符）与加扰数据。
- **字节对齐细节**：`JesdAlignFrRepCh.vhd` 配合 `Jesd204bPkg.vhd` 的 `detectPosFuncSwap/JesdDataAlign/JesdCharAlign`，是字边界 barrel-shifter 的完整实现，值得作为 4.x 的深入练习单独精读。
- **寄存器层**：`JesdRxReg.vhd`/`JesdTxReg.vhd` 把状态字与控制（使能、subClass、sysrefDly、加扰、替换、错误屏蔽）挂到 AXI-Lite，可对照 u3-l2 的 helper 四步骨架阅读。
- **协议族横向**：本讲是单元七的一环，后续可对比 u7-l1（PGP 的 VC/BTF 定界）、u7-l2（RSSI 的可靠重传），体会「不同高速协议如何各自解决定界与对齐」。
- **PyRogue 镜像**：状态寄存器布局最终要镜像到 `python/surf`，学完 u9-l4 后可回来对照 JESD 的状态字逐位建模。
