# 时序相关实体：delay / strobe / rate_limit / latency_comp

## 1. 本讲目标

在数据通路里，「时间」本身常常是需要被操控的对象：把一拍数据推迟若干拍、按固定节拍产生事件、限制流的吞吐、或者让一条旁路数据和一条经过处理的数据重新对齐。Open Logic 在 base 区域提供了一组专门解决「时序操控」的实体。

学完本讲，你应当能够：

- 区分**固定延迟**（`olo_base_delay`）与**运行时可配延迟**（`olo_base_delay_cfg`），并理解它们对 RAM/SRL 资源的选取策略；
- 用 `olo_base_strobe_gen` 产生固定频率的单周期脉冲，用 `olo_base_strobe_div` 对脉冲做整数分频，并理解整数模式与小数模式的差别；
- 用 `olo_base_rate_limit` 把 AXI-S 流的平均速率限制到 `MaxSamples_g/Period_g`，并区分 SMOOTH 与 BLOCK 两种节流风格；
- 用 `olo_base_latency_comp` 让一条旁路数据通路与一个处理单元的延迟重新对齐，并理解 DYNAMIC 与 FIXED_CYCLES 两种模式。

> 本讲承接 [u2-l2 流水线阶段与 AXI-S 握手]：所有实体都遵循两进程法、AXI-S 握手与同步高有效复位约定，这里不再重复这些规则，而是把它们当作既定前提。

## 2. 前置知识

本讲用到以下几个已经在前面讲义中建立的概念，先做一句话回顾：

- **数据拍（beat / sample）**：AXI-S 流中一次成功握手（`Valid='1'` 且 `Ready='1'` 的同一时钟沿）所传递的一个数据。本讲里「延迟 N 拍」严格指 N 个**数据拍**，不是 N 个时钟周期——只有当 `In_Valid` 恒为 `'1'` 时，二者才相等。
- **AXI-S 反压（back-pressure）**：下游用 `Ready` 控制接收节奏；当 `Ready` 拉低时数据被「卡住」。
- **两进程法（two-process method）**：组合进程 `p_comb` 只算下一拍状态 `r_next`，时序进程 `p_seq` 只打拍并复位，状态收纳进 `record`。在 `olo_base_strobe_div` 与 `olo_base_rate_limit` 里你会再次看到这套写法。
- **块 RAM 与 SRL**：FPGA 上实现「深度延迟链」的两种资源。块 RAM（BRAM）容量大但读口慢；SRL（移位寄存器查找表）用 LUT 实现小延迟、速度快。本讲的延迟实体正是围绕「用 RAM/SRL 替代大量触发器」来设计的。
- **读时写行为 RBW/WBR**：同地址同周期读写时返回旧值（RBW，读前写）还是新值（WBR，写前读），由 `RamBehavior_g` 控制（详见 [u2-l3 RAM 实现]）。

一个贯穿全讲的设计原则：**RAM/SRL 没有复位**。一旦用 RAM 存延迟，复位后存储内容会残留，所以每个用 RAM 的实体都要额外加一段「让残留内容表现为 0」的逻辑。这一点会在 4.1 重点展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `src/base/vhdl/olo_base_delay.vhd` | **固定延迟**。延迟值由泛型 `Delay_g` 在编译期决定，按深度自动选 SRL 或 BRAM。 |
| `src/base/vhdl/olo_base_delay_cfg.vhd` | **运行时可配延迟**。延迟值由端口 `Delay` 在运行期动态设置，用 BRAM 实现。 |
| `src/base/vhdl/olo_base_strobe_gen.vhd` | **脉冲发生器**。按 `FreqStrobeHz_g` 产生单周期脉冲，支持整数/小数模式与相位同步。 |
| `src/base/vhdl/olo_base_strobe_div.vhd` | **脉冲分频器**。每 N 个输入脉冲放行一个，N 可运行期配置。 |
| `src/base/vhdl/olo_base_rate_limit.vhd` | **速率限制器**。把 AXI-S 流限制为每 `Period_g` 拍最多 `MaxSamples_g` 个样本。 |
| `src/base/vhdl/olo_base_latency_comp.vhd` | **延迟补偿器**。延迟旁路数据以匹配并行处理单元的延迟，含 overrun/underrun 检测。 |

配套文档位于 `doc/base/olo_base_<entity>.md`，测试台位于 `test/base/olo_base_<entity>/olo_base_<entity>_tb.vhd`。

---

## 4. 核心概念与源码讲解

### 4.1 固定延迟：olo_base_delay

#### 4.1.1 概念说明

`olo_base_delay` 把输入数据推迟 `Delay_g` 个**数据拍**后输出。它解决的核心问题是：用 RAM/SRL 资源高效实现深度延迟链，而不是用成百上千个触发器堆出来。

延迟的单位是「数据拍」而非「时钟周期」：数据只有在 `In_Valid='1'` 的拍才会沿延迟链前进。因此：

- 想要「时钟周期」意义上的固定延迟 → 把 `In_Valid` 接 `'1'`（或留空走默认值）；
- 想要 AXI-S 反压流意义上的延迟 → 把 `In_Valid` 接到 `Valid and Ready`（一次成功握手才前进一拍），文档里专门画了这个用法。

#### 4.1.2 核心流程

延迟链由「存储体 + 末级输出寄存器」两段拼成，存储体只承担 `Delay_g-1` 拍，末级寄存器再补 1 拍：

```
In_Data ──► [ 存储体: Delay_g-1 拍 ] ──MemOut──► [ 输出寄存器: 1 拍 ] ──► Out_Data
```

存储体的实现按 `Resource_g` 与深度自动二选一：

- 深度小（`Delay_g < BramThreshold_g`，默认阈值 128）→ 用 **SRL**（LUT 移位寄存器）；
- 深度大 → 用 **BRAM**（环形缓冲，读写指针循环）。

三种退化情形直接绕过存储体：

| `Delay_g` | 实现 |
| :--- | :--- |
| 0 | `Out_Data <= In_Data`（纯组合直通，0 拍） |
| 1 | `MemOut <= In_Data`，只剩末级输出寄存器（1 拍） |
| ≥ 2 | SRL 或 BRAM + 输出寄存器 |

一个关键难点：**RAM/SRL 不可复位**，复位后存储内容残留为历史数据。`RstState_g=true`（默认）通过在输出端用一个计数器 `RstStateCnt` 强制把复位后前 `Delay_g` 拍输出成 0，从而「掩盖」残留内容——这是该实体最重要的工程技巧。

#### 4.1.3 源码精读

泛型与端口声明，注意延迟单位由 `Delay_g` 决定、`In_Valid` 默认 `'1'`：

[src/base/vhdl/olo_base_delay.vhd:L34-L53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay.vhd#L34-L53) —— `Delay_g` 是延迟拍数，`Resource_g`（AUTO/SRL/BRAM）与 `BramThreshold_g` 控制资源选择，`RstState_g` 控制复位后是否输出 0。

存储体只承担 `Delay_g-1` 拍（`MemTaps_c`），剩余 1 拍由末级输出寄存器补足：

[src/base/vhdl/olo_base_delay.vhd:L60-L63](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay.vhd#L60-L63) —— `MemTaps_c := max(Delay_g - 1, 0)`，这一行决定了「存储体深度 = 总延迟 − 1」。

SRL 分支：小延迟用一个数组当移位寄存器，并加综合属性确保它被推断成 LUT-SRL 而非散触发器：

[src/base/vhdl/olo_base_delay.vhd:L78-L103](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay.vhd#L78-L103) —— `g_srl` 在 `Delay_g>1` 且（指定 SRL，或 AUTO 且 `Delay_g < BramThreshold_g`）时生成。`p_srl` 进程在 `In_Valid='1'` 时整体右移一级；`shreg_extract`/`srl_style`/`ramstyle` 属性分别面向通用、AMD、Altera 工具，保证推断稳定。

BRAM 分支：大延迟改用环形 RAM，两个循环计数器当读写指针，复用 [u2-l3] 讲过的 `olo_base_ram_sdp`：

[src/base/vhdl/olo_base_delay.vhd:L106-L156](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay.vhd#L106-L156) —— `g_bram` 在指定 BRAM 或 AUTO 且 `Delay_g >= BramThreshold_g` 时生成。`p_bram` 维护读写地址循环；复位时把写指针拨到 `MemTaps_c-1`、读指针归零，使第一拍读到的就是新写入的数据。实例化的 `olo_base_ram_sdp` 深度恰为 `MemTaps_c`。

最关键的「复位后输出 0」逻辑在末级输出寄存器里：

[src/base/vhdl/olo_base_delay.vhd:L168-L193](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay.vhd#L168-L193) —— `g_nonzero` 在 `Delay_g>0` 时生成输出寄存器。`RstStateCnt` 从 0 数到 `Delay_g-1`：在数满之前（即复位后前 `Delay_g` 个有效拍），若 `RstState_g=true` 则强制输出 0，正好把 RAM 里残留的历史数据全部「盖过去」；数满后正常输出 `MemOut`。这就是文档所说的「复位后第一个时钟周期即可正常工作」。

#### 4.1.4 代码实践

**目标**：直观感受 `Delay_g` 是「数据拍」而非「时钟周期」。

1. 实例化一个 `Delay_g=4, Width_g=8` 的 `olo_base_delay`，`In_Valid` 先接 `'1'`；
2. 让 `In_Data` 每拍递增 1，用仿真波形数 `In_Data=D` 出现在 `Out_Data` 之间相隔几拍；
3. 把 `In_Valid` 改成周期性拉低（例如每 3 拍拉低 1 拍），保持 `In_Data` 不变，重新数 `Out_Data` 的间隔。

**观察与预期**：

- 第 2 步应看到 `Out_Data` 比 `In_Data` 晚 **4 个有效数据拍**（若 `In_Valid` 恒为 `'1'`，也就等于 4 个时钟周期）；
- 第 3 步应看到 `Out_Data` 仍晚 4 个**有效拍**，但折算成时钟周期会变长——这证明延迟链只在 `In_Valid='1'` 时前进。
- 复位后前 4 个有效拍 `Out_Data` 应为 0（`RstState_g` 默认 true），第 5 个有效拍起才出现真实数据。

> 仓库自带测试台 `test/base/olo_base_delay/olo_base_delay_tb.vhd` 已覆盖多种 `Delay_g`/`Resource_g`/`RstState_g` 组合，可直接运行对照。运行方式见 4.3.4 或 [u1-l4]。结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Delay_g=1` 时，实体还会用 RAM 或 SRL 吗？为什么？
**答案**：不会。`Delay_g=1` 时 `MemTaps_c=0`，存储体为空，`g_single` 直接 `MemOut <= In_Data`（[L159-L161](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay.vhd#L159-L161)），只剩末级输出寄存器提供那 1 拍延迟。

**练习 2**：为什么末级输出寄存器是「总是用触发器」而不是也放进 RAM？
**答案**：RAM 读口通常较慢，把它放在时序路径末端会拖累 `Out_Data` 后续逻辑的建立时间；用一颗 fabric 触发器收尾能显著改善时序（见文件头描述 [L9-L11](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay.vhd#L9-L11)）。

---

### 4.2 运行时可配延迟：olo_base_delay_cfg

#### 4.2.1 概念说明

`olo_base_delay_cfg` 与 4.1 解决同一类问题（把数据延迟若干拍），但延迟值不再写死在泛型里，而是由**运行期端口** `Delay` 动态决定，上限由 `MaxDelay_g` 约束。典型用途：通信链路里的时延校准、可编程相位调整、测试时扫不同延迟。

设计取舍：

- 它**只用 BRAM**（不像 `olo_base_delay` 那样在 SRL/BRAM 间选择），因为运行期要随机访问任意延迟量，环形 RAM 比固定移位链更合适；
- 改变 `Delay` 后，输出会在不到 5 个数据拍内反映新延迟，这期间 `Out_Data` 内容**未定义**（见文档图示）；
- 可选支持 `Delay=0` 直通（`SupportZero_g`），但会引入一条输入到输出的组合路径，对时序不利，默认关闭。

#### 4.2.2 核心流程

```
                 ┌── Delay=1 ──► In_Data ──────────────────┐
In_Data ──► [BRAM: MaxDelay 深度] ──MemOut ──┐             ├──(mux)──► OutNonzero ──► Out_Data
                  ↑ 读地址 = 写地址 - Delay   │             │
                  └── 2 级 SRL ── Delay=2,3 ─┘── Delay≥4 ──┘
```

写地址每个有效拍 +1（循环）；读地址滞后写地址 `Delay` 个位置，于是读出的就是 `Delay` 拍前写入的数据。小延迟（2、3）走一条 2 级移位寄存器，不必经过 RAM 读延迟；`Delay=1` 直接取 `In_Data`；`Delay≥4` 取 RAM 输出。

读地址计算里有一个容易踩坑的细节：当 `MaxDelay_g` 是 2 的幂时，`Delay` 端口会比地址多 1 个比特，因此代码做了一个显式的截断与 `+3` 偏移（见 4.2.3）。

#### 4.2.3 源码精读

泛型与端口，注意 `Delay` 端口位宽是 `log2ceil(MaxDelay_g+1)`：

[src/base/vhdl/olo_base_delay_cfg.vhd:L34-L52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay_cfg.vhd#L34-L52) —— `MaxDelay_g` 是延迟上限；`SupportZero_g` 控制 `Delay=0` 直通；`Delay` 是运行期延迟值输入。

读地址计算与「2 的幂多 1 位」的处理：

[src/base/vhdl/olo_base_delay_cfg.vhd:L75-L96](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay_cfg.vhd#L75-L96) —— `p_bram` 每有效拍让 `WrAddr` 递增；`RdAddr_v := WrAddr - Delay + 3`，其中 `+3` 与 `g_ram` 仅在 `MaxDelay_g>3` 时生成（[L70](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay_cfg.vhd#L70)）共同处理 RAM 读延迟与地址对齐；`RdAddr_v(RdAddr'range)` 做截断，正是注释里说的「`Delay` 比 address 多 1 位」的 2 的幂情形。

输出选择 mux，按当前 `Delay` 取不同来源：

[src/base/vhdl/olo_base_delay_cfg.vhd:L119-L152](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay_cfg.vhd#L119-L152) —— `p_srl` 维护 2 级移位寄存器 `SrlSig`；`p_outreg` 用 `case` 选择：`Delay=1`→`In_Data`，`2`→`SrlSig(0)`，`3`→`SrlSig(1)`，其余→RAM 输出 `MemOut`。

`SupportZero_g` 的组合直通路径：

[src/base/vhdl/olo_base_delay_cfg.vhd:L154-L160](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay_cfg.vhd#L154-L160) —— 当 `SupportZero_g=true` 且 `Delay=0` 时，`Out_Data <= In_Data` 是纯组合，绕过所有寄存器，这正是文档警告「对时序不利」的原因。

#### 4.2.4 代码实践

**目标**：观察运行期切换 `Delay` 时输出的「未定义窗口」。

1. 实例化 `Width_g=8, MaxDelay_g=20, SupportZero_g=false` 的 `olo_base_delay_cfg`，`In_Valid` 接 `'1'`；
2. 初始 `Delay=3`，让 `In_Data` 每拍递增，等输出稳定；
3. 在某一拍把 `Delay` 改成 `10`，继续观察 `Out_Data`；
4. 数从切换到输出重新稳定，中间有几拍是「错位」的数据。

**观察与预期**：

- 切换前 `Out_Data` 应是 `In_Data` 的 3 拍前镜像；
- 切换后约 5 拍内 `Out_Data` 会出现与输入对不上的值（文档承诺「小于 5 拍」内收敛），之后稳定为 10 拍延迟。
- 这段窗口正是文档图中标注的「undefined」区，**使用时必须由上层逻辑屏蔽或忽略**这几拍。

> 仓库测试台 `test/base/olo_base_delay_cfg/olo_base_delay_cfg_tb.vhd` 通过 `RandomStall_g`、`SupportZero_g`、`MaxDelay_g`、`RamBehavior_g` 等组合覆盖这些场景（配置见 `sim/test_configs/olo_base.py`）。结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `olo_base_delay_cfg` 不像 `olo_base_delay` 那样提供 SRL 选项？
**答案**：运行期需要按任意 `Delay` 随机读取，环形 RAM 天然支持「读地址 = 写地址 − Delay」的随机访问；而 SRL 是固定深度的移位链，改延迟要重建链路，不适合运行期动态配置。

**练习 2**：`MaxDelay_g=4` 时，`g_ram` 会生成吗？延迟 4 拍的数据从哪里来？
**答案**：`g_ram` 条件是 `MaxDelay_g > 3`（[L70](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_delay_cfg.vhd#L70)），`MaxDelay_g=4` 满足，会生成 RAM；延迟 4 的样本走 `case` 的 `others` 分支取 `MemOut`。若 `MaxDelay_g<=3`，则不生成 RAM，`MemOut` 恒为 0。

---

### 4.3 脉冲生成与分频：olo_base_strobe_gen / olo_base_strobe_div

#### 4.3.1 概念说明

很多模块需要一个「节拍」——周期性的单周期脉冲，用来触发采样、刷新状态或驱动低频逻辑。Open Logic 用两个实体配合产生任意节拍：

- **`olo_base_strobe_gen`**：从时钟频率 `FreqClkHz_g` 与目标频率 `FreqStrobeHz_g` 出发，产生周期为 \( T_{strobe} = 1/FreqStrobeHz\_g \) 的单周期脉冲。它**不接输入数据**，只输出 `Out_Valid` 脉冲。
- **`olo_base_strobe_div`**：把已有的脉冲流按整数比 N 分频，每 N 个输入脉冲放行 1 个。常接在 `strobe_gen` 之后做级联分频。

`strobe_gen` 有两种精度模式：

- **整数模式（aequidistant，默认）**：相邻脉冲间隔的时钟周期数恒定，简单且抖动为零，但实际频率可能偏离目标多达半个时钟周期；
- **小数模式（`FractionalMode_g=true`）**：相邻间隔在两个相邻整数间抖动 1 拍，长期平均频率误差 <1%，适合目标频率较高、对长期频率精度敏感的场景。

#### 4.3.2 核心流程

**strobe_gen** 用一个计数器数「还要多少个时钟周期才发下一个脉冲」：

- 整数模式：计数值上限
  \[ PeriodCounts_c = round\!\left(\frac{FreqClkHz\_g}{FreqStrobeHz\_g}\right) \]
  每拍 `Count` 加 1，达到 `WrapBorder_c = PeriodCounts_c - 1` 时发脉冲并归零。
- 小数模式：把上述量统一放大 100 倍，每拍 `Count` 加 100，达到上限时发脉冲并把计数器减去 `WrapBorder_c`（**不是**归零），从而保留小数余数：
  \[ PeriodCounts_c = round\!\left(\frac{FreqClkHz\_g}{FreqStrobeHz\_g} \times 100\right),\quad Increment_c = 100 \]

另有两个增强：

- **`In_Sync` 相位同步**：检测到 `In_Sync` 上升沿时，立刻把 `Count` 清零并发出一个脉冲，使节拍相位对齐到外部事件。
- **`Out_Ready` 握手**：若接了 `Out_Ready`，`Out_Valid` 会保持高直到被 `Out_Ready` 收走；但 `Out_Valid` 的**上升沿频率**仍精确等于 `FreqStrobeHz_g`。

**strobe_div** 用两进程法维护 `{Count, OutValid}`：每来一个 `In_Valid` 脉冲，`Count` 加 1；当 `Count >= In_Ratio` 时归零并在下一拍（`Latency_g=1`）或当拍（`Latency_g=0`）发出 `Out_Valid`。由于 `In_Ratio` 是「期望分频比 − 1」，故每 `In_Ratio+1` 个输入脉冲放行一个。

#### 4.3.3 源码精读

**strobe_gen** 的常数定义——整数/小数两种周期与增量：

[src/base/vhdl/olo_base_strobe_gen.vhd:L54-L62](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_gen.vhd#L54-L62) —— `PeriodCountsInteger_c` 与 `PeriodCountsFractional_c` 由 `choose()` 按模式二选一；`Increment_c` 在小数模式下为 100，整数模式为 1；`WrapBorder_c = PeriodCounts_c - Increment_c`。

主进程 `p_strobe`，三个分支：同步、回卷发脉冲、累加：

[src/base/vhdl/olo_base_strobe_gen.vhd:L80-L112](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_gen.vhd#L80-L112) —— `In_Sync` 上升沿（[L84-L86](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_gen.vhd#L84-L86)）清零并发脉冲；`Count >= WrapBorder_c`（[L88-L94](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_gen.vhd#L88-L94)）回卷——注意小数模式用 `Count - WrapBorder_c` 保留余数，整数模式直接归 0；其余情况累加并按 `Out_Ready` 清 `Out_Valid`。复位覆盖在进程末尾（[L106-L110](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_gen.vhd#L106-L110)）。

入口处两条断言约束了可用范围（小数模式比例 < 10⁶、最大比例 ≈ 2.15×10⁹）：

[src/base/vhdl/olo_base_strobe_gen.vhd:L71-L78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_gen.vhd#L71-L78)。

**strobe_div** 的两进程法，`p_comb` 做比率计数与延迟选择：

[src/base/vhdl/olo_base_strobe_div.vhd:L61-L95](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_div.vhd#L61-L95) —— `In_Valid='1'` 时若 `Count >= In_Ratio`（或 `MaxRatio_g=1`）则归零并置 `OutValid`，否则 `Count+1`（[L68-L76](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_div.vhd#L68-L76)）；`Latency_g=0` 取组合值、`Latency_g=1` 取寄存值（[L78-L91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_div.vhd#L78-L91)）；握手完成（`OutValid='1'` 且 `Out_Ready='1'`）时清 `OutValid`。

`In_Ratio` 端口的默认值就是 `MaxRatio_g-1`，所以不接端口时即为「按 `MaxRatio_g` 分频」的编译期配置：

[src/base/vhdl/olo_base_strobe_div.vhd:L31-L44](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_div.vhd#L31-L44)。

#### 4.3.4 代码实践（本讲主实践）

**目标**：用 `strobe_gen` 产生 1 ms 周期脉冲，驱动 `strobe_div` 做 1/10 分频得到 10 ms 节拍，并用 `delay_cfg` 把一路 AXI-S 数据延迟若干拍，最后在波形上对齐三者节拍。

设时钟为 100 MHz。

1. **生成节拍**：实例化 `olo_base_strobe_gen`，`FreqClkHz_g => 100.0e6, FreqStrobeHz_g => 1000.0`（周期 1 ms），`In_Sync`/`Out_Ready` 不接；
2. **分频**：把 `strobe_gen` 的 `Out_Valid` 接到 `olo_base_strobe_div` 的 `In_Valid`，`MaxRatio_g => 10`、`In_Ratio` 不接（即按 10 分频），于是 `strobe_div.Out_Valid` 每 10 ms 一个脉冲；
3. **延迟数据**：另起一路 `In_Data`（每 `strobe_gen` 脉冲更新一次），实例化 `olo_base_delay_cfg`（`Width_g=8, MaxDelay_g=20`），运行期 `Delay` 设为 5；
4. 在仿真里跑约 30 ms，把 `strobe_gen.Out_Valid`、`strobe_div.Out_Valid`、`In_Data`、`delay_cfg.Out_Data` 拉到同一波形窗。

**运行方式**（仓库已提供测试台，可先复用其运行入口；详见 [u1-l4]）：

```bash
# 在 sim/ 目录下，单独运行 strobe_gen 的测试台作为环境验证
cd sim
python3 run.py --ghdl -v "*strobe_gen*"
python3 run.py --ghdl -v "*strobe_div*"
```

> 上述 `run.py` 用法与 `--ghdl/--nvc` 仿真器切换在 [u1-l4] 已讲过。本实践需要你**自己写一个顶层测试台**把三个实体串起来——仓库没有现成的「三者串联」测试台。

**观察与预期**：

- `strobe_gen.Out_Valid` 相邻脉冲间隔 1 ms（100 MHz 下为 100 000 个时钟周期）；
- `strobe_div.Out_Valid` 相邻脉冲间隔 10 ms，且每次都落在某个 `strobe_gen` 脉冲上（分频不引入新节拍，只挑选）；
- `delay_cfg.Out_Data` 比 `In_Data` 晚 5 个 `strobe_gen` 脉冲（因为 `In_Valid` 用了节拍，延迟单位是数据拍）。若你把 `In_Valid` 接成恒 `'1'`，则延迟退化为 5 个时钟周期——这正是 4.1 强调的「拍 vs 周期」差别。

> 结果待本地验证。若手写测试台，记得在文件头加 `-- vunit: run_all_in_same_sim` 并按 VUnit 约定声明 `runner_cfg` 泛型（参照 `olo_base_strobe_gen_tb.vhd` [L25-L32](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_strobe_gen/olo_base_strobe_gen_tb.vhd#L25-L32)）。

#### 4.3.5 小练习与答案

**练习 1**：100 MHz 时钟、`FreqStrobeHz_g=1.0e6`，整数模式下相邻脉冲间隔多少周期？真实频率是多少？
**答案**：`PeriodCounts_c = round(100e6/1e6) = 100`，间隔 100 个时钟周期，真实频率精确为 1 MHz（此处无误差）。误差出现在比例不能整除时，例如 100 MHz 产生 13.2 MHz 时 `round(100/13.2)=round(7.575)=8`，真实频率变为 12.5 MHz，偏离较多——这时应改用小数模式。

**练习 2**：`strobe_div` 的 `In_Ratio` 为什么要写「期望比 − 1」？
**答案**：代码在 `Count >= In_Ratio` 时归零并发脉冲（[L70](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_div.vhd#L70)）。`Count` 从 0 数到 `In_Ratio` 共经历 `In_Ratio+1` 个输入脉冲，所以「每 3 个放行 1 个」要写 `In_Ratio=2`。

**练习 3**：`MaxRatio_g=1` 且 `In_Ratio` 不接时，`strobe_div` 行为是什么？
**答案**：`MaxRatio_g=1` 时 `p_comb` 直接走 `or MaxRatio_g = 1` 分支（[L70](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_div.vhd#L70)），每个输入脉冲都置 `OutValid`，但 `OutValid` 会保持到 `Out_Ready` 收走——即把单周期脉冲「展宽」成持续到应答的脉冲，这正是文档说的「ready 转换」用法。

---

### 4.4 速率限制：olo_base_rate_limit

#### 4.4.1 概念说明

`olo_base_rate_limit` 把一条 AXI-S 流的平均速率限制为「每 `Period_g` 个时钟周期最多 `MaxSamples_g` 个样本」。典型场景：下游器件处理能力有限且**不支持反压**（不会拉低 `Ready`），例如某些 DAC、传感器接口；此时若不主动节流，上游突发会把下游冲垮。

它提供两种节流风格：

- **SMOOTH（默认）**：把样本在时间上**均匀铺开**，输出近似恒定速率，禁止突发；
- **BLOCK**：允许短突发（只要一个 `Period_g` 周期内的样本数不超 `MaxSamples_g`），只限制周期内的平均。突发更友好但瞬时速率可能超限。

二者平均速率相同，均为 `MaxSamples_g / Period_g` 样本/拍，区别只在时间分布。

参数还能运行期配置：`RuntimeCfg_g=true` 时，`Period_g`/`MaxSamples_g` 退化为「上限」，真实值由 `Cfg_Period`/`Cfg_MaxSamples` 端口给出（端口值 = 真实值 − 1），可在线调整。

#### 4.4.2 核心流程

实体先可选地用 `olo_base_pl_stage` 寄存输入侧 `Ready`（`RegisterReady_g`，改善时序），然后按 `Period_g` 分两条 generate 路径：

- `Period_g = 1` → 无限制，直通（每拍都能传一个样本，谈不上限速）；
- `Period_g > 1` → 进入节流核，计算一个 `AllowSample` 门控信号，再用它「与」到握手信号上：
  \[ Out\_Valid = In\_Valid \wedge AllowSample,\qquad In\_Ready = Out\_Ready \wedge AllowSample \]
  即只有 `AllowSample='1'` 的拍才允许一次握手通过。

`AllowSample` 的计算分两模式（均在两进程法 `p_comb` 里）：

- **SMOOTH** 用一个「信用计数器」`SmoothCounter`：
  - 一次输出传输 → `SmoothCounter -= SmoothLimit`（消耗信用，`SmoothLimit = Period − MaxSamples`）；
  - 否则若 `SmoothCounter < SmoothLimit` → `SmoothCounter += MaxSamples`（积累信用）；
  - `AllowSample = (SmoothCounter >= SmoothLimit)`。
- **BLOCK** 用「周期计数 + 周期内样本计数」：
  - `PeriodCounter` 在 `0..Period-1` 循环，每个周期末归零并把 `SamplesCounter` 也清零；
  - 每次实际输出传输 `SamplesCounter+1`；
  - `AllowSample = (SamplesCounter < MaxSamples)`。

> 直觉：SMOOTH 的信用机制让样本等间隔溢出；BLOCK 则是「先到先得，发完本周期配额就停」。

#### 4.4.3 源码精读

泛型与端口，注意配置端口位宽与默认值：

[src/base/vhdl/olo_base_rate_limit.vhd:L39-L64](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L39-L64) —— `Mode_g`（SMOOTH/BLOCK）、`Period_g`、`MaxSamples_g`、`RuntimeCfg_g`；`Cfg_Period`/`Cfg_MaxSamples` 默认值为泛型 − 1，体现「端口值 = 真实值 − 1」约定。

入口断言保证 `MaxSamples_g <= Period_g` 且 `Mode_g` 合法：

[src/base/vhdl/olo_base_rate_limit.vhd:L81-L89](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L81-L89)。

`Period_g=1` 时的直通捷径：

[src/base/vhdl/olo_base_rate_limit.vhd:L122-L127](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L122-L127) —— 无需节流，三信号直接对接。

SMOOTH 模式的信用计数逻辑：

[src/base/vhdl/olo_base_rate_limit.vhd:L189-L203](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L189-L203) —— 输出传输时减 `SmoothLimit`，否则在低于门限时加 `MaxSamples`；`AllowSample` 由 `SmoothCounter >= SmoothLimit` 决定。

BLOCK 模式的周期/样本双计数器：

[src/base/vhdl/olo_base_rate_limit.vhd:L206-L227](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L206-L227) —— `SamplesCounter` 只在实际输出时增；`PeriodCounter` 到顶后两者同时清零；`AllowSample` 由 `SamplesCounter < MaxSamples` 决定。

门控握手与输出赋值——`AllowSample` 同时作用于 `Out_Valid` 和 `In_Ready`，保证被拦下的样本仍留在上游：

[src/base/vhdl/olo_base_rate_limit.vhd:L249-L256](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L249-L256)。

运行期配置的寄存与换算（注意 `+1` 把「值 − 1」还原为真实值）：

[src/base/vhdl/olo_base_rate_limit.vhd:L164-L183](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L164-L183) —— `RuntimeCfg_g=true` 时把 `Cfg_*` 端口寄存并计算 `SmoothLimit`，`MaxSamples_v = Cfg_MaxSamples + 1`；`to01` 仅为规避仿真问题，功能无影响（见注释）。

#### 4.4.4 代码实践

**目标**：对比 SMOOTH 与 BLOCK 在相同配额下的时间分布。

1. 实例化 `olo_base_rate_limit`，`Width_g=8, Period_g=4, MaxSamples_g=2`，上游持续给 `In_Valid='1'`、`Out_Ready='1'`；
2. 先设 `Mode_g="SMOOTH"`，仿真约 20 拍，记录每个 `Out_Valid='1'` 的拍号；
3. 改为 `Mode_g="BLOCK"`，同样记录；
4. 统计两种模式下「连续 4 拍窗口内」通过的样本数，以及样本之间的间隔。

**观察与预期**：

- 两种模式 4 拍内都恰好通过 2 个样本（平均速率 0.5 样本/拍，符合 `2/4`）；
- SMOOTH 下样本近似等间隔（约每 2 拍一个），无连续突发；
- BLOCK 下样本倾向于聚集在周期开头（可出现连续 2 个），周期尾部静默。
- 这与文档给出的示意图（`Period_g=4, MaxSamples_g=2`）一致。结果待本地验证。

> 运行仓库测试台：`python3 run.py --ghdl -v "*rate_limit*"`（配置见 `sim/test_configs/olo_base.py` [L352-L354](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L352-L354)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SMOOTH 和 BLOCK 的平均速率相同？
**答案**：二者都把节流核约束在「每 `Period_g` 拍最多 `MaxSamples_g` 个」上。SMOOTH 用信用计数器把样本铺平，BLOCK 用周期配额先到先得；无论哪种，长期通过的样本数都被同一个上限钳制，故平均速率都是 `MaxSamples_g/Period_g`。

**练习 2**：`Cfg_MaxSamples` 端口写 1 代表每周期几个样本？
**答案**：2 个。端口值是「真实值 − 1」，代码里 `MaxSamples_v := to_integer(unsigned(Cfg_MaxSamples)) + 1`（[L172](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_rate_limit.vhd#L172)）。

---

### 4.5 延迟补偿：olo_base_latency_comp

#### 4.5.1 概念说明

`olo_base_latency_comp` 解决一个常见对齐问题：一份数据被分成两路，一路进入某个处理单元（如滤波器，有若干拍延迟），另一路**旁路**直走。若希望两路在输出端重新同拍到达，就必须给旁路路补上同样多的延迟——这正是「延迟补偿」。

它的设计有一个重要特点：**非侵入式（non-intrusive）**。它不修改任何握手信号：`In_Ready` 和 `Out_Valid` 都是**输入**端口（来自处理单元的握手），它只负责把 `In_Data` 延迟后从 `Out_Data` 送出，并持续检查对齐是否成功。一旦对齐失败（样本被覆盖或被空读），就在 `Err_Overrun`/`Err_Underrun` 上报错（粘住，直到复位）。

两种工作模式：

- **DYNAMIC（默认）**：用一个内部 FIFO 动态匹配延迟，**不需要预先知道处理单元的精确延迟**，甚至允许非恒定延迟；代价是 LUT 开销略大；
- **FIXED_CYCLES**：延迟固定为 `Latency_g` 个时钟周期，用固定延迟线实现，资源更省，但要求处理单元延迟已知且恒定，且支持每拍一个样本。

> 前提：处理单元**不改变采样率**（每个输入样本对应一个输出样本）。

#### 4.5.2 核心流程

定义两个「节拍」信号：
\[ In\_Beat = In\_Valid \wedge In\_Ready\quad(\text{一个样本被写入}),\qquad Out\_Beat = Out\_Valid \wedge Out\_Ready\quad(\text{一个样本被读出}) \]

理想情况下，每个 `In_Beat` 之后恰好 `Latency_g` 拍出现一个 `Out_Beat`。实体据此检查：

- **Overrun**：新样本要写入，但上一拍样本还没被读出 → 数据丢失；
- **Underrun**：要读出样本，但延迟线里没有样本 → 空读。

**DYNAMIC 模式**用一个深度 `Latency_g+2` 的同步 FIFO：`In_Beat` 当写使能、`Out_Beat` 当读使能，FIFO 自动吸收可变延迟。Overrun = 写时 FIFO 满；Underrun = 读时 FIFO 空。

**FIXED_CYCLES 模式**用 `olo_base_delay`（`Delay_g = Latency_g-1`）做固定延迟线，并把「样本有效位」连同数据一起移位（数据宽度 +1）。这样延迟线输出端的有效位就标记了「这一拍是否真有样本」，据此检测 over/underrun；再用一个 `Data_Latched` 状态保持上一样本，容忍有限的握手抖动。

#### 4.5.3 源码精读

泛型与端口——注意 `In_Ready` 与 `Out_Valid` 都是 `in`（非侵入）：

[src/base/vhdl/olo_base_latency_comp.vhd:L31-L58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L31-L58) —— `Mode_g`（DYNAMIC/FIXED_CYCLES）、`Latency_g`（DYNAMIC 下为最大延迟，FIXED_CYCLES 下为精确周期数）、`AssertsDisable_g`/`AssertsName_g` 控制错误报告；`Err_Overrun`/`Err_Underrun` 为粘性错误输出。

节拍信号定义：

[src/base/vhdl/olo_base_latency_comp.vhd:L79-L81](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L79-L81) —— `In_Beat`/`Out_Beat` 是写/读节拍。

DYNAMIC 模式：同步 FIFO + 错误检测：

[src/base/vhdl/olo_base_latency_comp.vhd:L84-L146](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L84-L146) —— FIFO 深度 `Latency_g+2`（[L93](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L93)），`In_Beat` 当写使能、`Out_Beat` 当读使能；`p_errors` 检测：写时节拍且 FIFO 满（`In_Rdy='0'`）且未同时读出 → Overrun（[L117-L125](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L117-L125)）；读时节拍但 FIFO 空（`Out_Vld='0'`）→ Underrun（[L128-L136](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L128-L136)）。

FIXED_CYCLES 模式：固定延迟线 + 有效位移位 + 握手容忍：

[src/base/vhdl/olo_base_latency_comp.vhd:L149-L228](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L149-L228) —— 把 `In_Beat` 拼到数据最高位（`InData(Width_g) <= In_Beat`，[L158-L159](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L158-L159)），整体送入 `olo_base_delay`（`Delay_g => Latency_g-1`，宽度 +1，[L162-L177](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L162-L177)）；`p_handshake` 用移位出的有效位 `Delay_Beat` 与 `Data_Latched` 状态判断 over/underrun，并在 `Delay_Beat='1'` 时锁存新数据、`Out_Beat='1'` 时释放（[L185-L226](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_latency_comp.vhd#L185-L226)）。

#### 4.5.4 代码实践

**目标**：观察 DYNAMIC 模式如何吸收可变延迟，以及 underrun 如何被检出。

1. 实例化 `olo_base_latency_comp`，`Width_g=8, Mode_g="DYNAMIC", Latency_g=4`；
2. 自己构造一对激励：`In_Beat`（即 `In_Valid and In_Ready`）按不规则节拍产生，`Out_Beat` 在 `In_Beat` 后 3~5 拍（模拟一个 3~5 拍可变延迟的处理单元）拉高；
3. 先让 `Out_Beat` 始终能跟上，仿真约 30 拍，检查 `Err_Overrun`/`Err_Underrun` 始终为 0；
4. 再故意制造一次「读早了」：在某个尚无对应 `In_Beat` 的拍拉高 `Out_Beat`，观察 `Err_Underrun`。

**观察与预期**：

- 第 3 步：尽管处理单元延迟在 3~5 拍间变化，FIFO 能吸收，`Out_Data` 始终对应正确的历史样本，无错误；
- 第 4 步：`Err_Underrun` 在出错拍拉高并**保持**（粘性），直到复位才清零；
- `Out_Data` 与 `In_Data` 的内容关系应满足「先进先出」顺序。结果待本地验证。

> 仓库测试台：`python3 run.py --ghdl -v "*latency_comp*"`（配置见 `sim/test_configs/olo_base.py` [L368-L376](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L368-L376)，含一个低层测试台 `olo_base_latency_comp_lolevel_tb`）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 DYNAMIC 模式的 FIFO 深度是 `Latency_g+2` 而不是 `Latency_g`？
**答案**：`Latency_g` 在 DYNAMIC 模式下是「最大延迟」，FIFO 需要在最坏情况下暂存最多 `Latency_g` 个样本；额外 +2 留出裕量应对读写节拍的瞬时不对齐与 FIFO 满空标志的判别延迟，避免误报 overrun。文档在「Architecture」一节明确说明深度为 `Latency_g + 2`。

**练习 2**：FIXED_CYCLES 模式为什么要把 `In_Beat` 拼进数据位一起延迟？
**答案**：固定延迟线本身不知道某一拍是否真有样本（`In_Beat` 可能为 0）。把有效位随数据一起移位，延迟线输出端的有效位 `Delay_Beat` 就准确标记了「`Latency_g` 拍前那一刻是否有样本」，从而能判断 overrun（新样本到、旧样本还没被读）与 underrun（要读、但有效位为 0）。

---

## 5. 综合实践：一条「节流 + 对齐」的数据通路

把本讲四个模块串成一个真实小系统：

**场景**：一个传感器每拍产出样本，但下游处理单元每 4 拍只能消费 2 个样本（无反压），且处理单元本身有 3 拍延迟。设计要求：上游样本不被丢弃，旁路的「时间戳」要与处理后的样本对齐。

**设计步骤**：

1. 用 `olo_base_rate_limit`（`Mode_g="SMOOTH", Period_g=4, MaxSamples_g=2, Width_g=<数据宽>`）把传感器流节流到下游能承受的速率；
2. 节流后的主流送入下游处理单元（这里用一个 3 拍延迟的 `olo_base_delay` 模拟即可）；
3. 同一份节流后的样本作为「旁路时间戳」，送入 `olo_base_latency_comp`（`Mode_g="FIXED_CYCLES", Latency_g=3`）的 `In_Data`；把处理单元输出的 `Valid/Ready` 接到 `latency_comp` 的 `Out_Valid/Out_Ready`、把输入侧的 `Valid/Ready` 接到 `In_Valid/In_Ready`；
4. 另起一路慢节拍：用 `olo_base_strobe_gen`（例如 `FreqStrobeHz_g` 设为时钟的 1/1000）+ `olo_base_strobe_div`（`MaxRatio_g=10`）产生一个周期性「快照」脉冲，用来在每个快照点检查 `Err_Overrun`/`Err_Underrun` 是否为 0。

**验证清单**：

- 速率限制器输出端，连续 4 拍窗口内样本数 ≤ 2；
- `latency_comp.Out_Data` 与处理单元输出在**同一拍**有效（对齐成功）；
- 长时间运行 `Err_Overrun`/`Err_Underrun` 保持 0；若你故意让处理单元延迟超过 `Latency_g=3`，应观察到 `Err_Underrun` 被点亮。

> 这是典型的「先限速、再对齐」组合：`rate_limit` 防止下游过载，`latency_comp` 保证旁路数据与处理结果同步。整个系统仍可在 `sim/run.py` 框架内仿真——把你写的顶层 TB 放进 `test/base/`，库名用 `olo_tb`，并加 `-- vunit: run_all_in_same_sim` 注释即可被 VUnit 发现。结果待本地验证。

## 6. 本讲小结

- **延迟的单位是「数据拍」不是「时钟周期」**：`olo_base_delay` 与 `olo_base_delay_cfg` 只在 `In_Valid='1'` 时前进；想要周期级延迟就把 `In_Valid` 接 `'1'`，想要 AXI-S 拍级延迟就接 `Valid and Ready`。
- **固定 vs 可配**：`olo_base_delay` 在 SRL/BRAM 间按深度自动选资源、延迟写死在泛型；`olo_base_delay_cfg` 只用 BRAM、延迟运行期可变，切换后有 <5 拍的未定义窗口。
- **RAM 不可复位**是贯穿延迟类实体的难题：`olo_base_delay` 用 `RstStateCnt` 在输出端把复位后前 `Delay_g` 拍强制清零，掩盖 RAM 残留。
- **脉冲两件套**：`olo_base_strobe_gen` 按频率产生脉冲（整数模式等距、小数模式高精度），`olo_base_strobe_div` 做整数分频（`In_Ratio` = 期望比 − 1）。
- **速率限制**：`olo_base_rate_limit` 用一个 `AllowSample` 门控信号同时作用于 `Out_Valid` 与 `In_Ready`，SMOOTH 用信用计数器均铺、BLOCK 用周期配额先到先得，平均速率都是 `MaxSamples_g/Period_g`。
- **延迟补偿**：`olo_base_latency_comp` 非侵入式地对齐旁路与处理通路，DYNAMIC 用 FIFO 吸收可变延迟、FIXED_CYCLES 用固定延迟线，并以粘性的 `Err_Overrun`/`Err_Underrun` 报告失配。

## 7. 下一步学习建议

- **深入 RAM 底层**：本讲的 `olo_base_delay`/`delay_cfg` 都依赖 `olo_base_ram_sdp`，建议接着读 [u2-l3 RAM 实现]，彻底弄清 RBW/WBR 与块 RAM 推断。
- **FIFO 与节流的关系**：`olo_base_rate_limit` 的「拦下样本仍留在上游」与 `olo_base_fifo_sync` 的反压是同一思想的两面；可对比阅读 [u2-l4 同步 FIFO]。
- **下一讲 [u5-l2 仲裁器]**：从「时序控制」过渡到「多请求者竞争」，学习 `arb_prio`/`arb_rr`/`arb_wrr` 三种仲裁策略。
- **扩展阅读**：若你的节拍需要跨时钟域，结合 [u4-l1/u4-l2 跨时钟域]，把 `strobe_gen` 产生的脉冲用 `olo_base_cc_pulse` 安全地送到另一个时钟域。
