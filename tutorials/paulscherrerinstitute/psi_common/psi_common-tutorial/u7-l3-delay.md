# 可配置延迟 delay / delay_cfg

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「延迟线（delay line）」在数字电路里解决什么问题，以及为什么不应该用一长串触发器（FF）来实现大延迟。
- 读懂 `psi_common_delay`：它能用 SRL、BRAM 或 AUTO 三种方式实现**固定**延迟，并把总延迟精确地拆成「存储抽头 + 输出寄存器」。
- 解释 `resource_g`、`bram_threshold_g`、`ram_behavior_g` 等 generic 如何决定延迟线最终落在哪种 FPGA 资源上。
- 读懂 `psi_common_delay_cfg`：它把延迟值从 generic 搬到运行时寄存器 `del_i`，并理解它为什么对小延迟（≤3）走 SRL、对大延迟走 BRAM，以及 `hold_g` 在「加大延迟」时如何避免输出毛刺。
- 理解两个延迟组件都复用 `psi_common_sdp_ram` 作为底层存储，从而承接 u3-l1 学过的简单双口 RAM。

## 2. 前置知识

本讲默认你已经掌握：

- **VHDL generic（类属参数）与 `if generate`**：延迟线大量用 generic 在编译期切换实现，运行时配置则用普通端口。
- **AXI-S 的 VLD 语义**（u1-l4）：本讲的两个组件都用 `vld_i` 作为「数据有效/选通」信号，但没有 `rdy` 反压——它们是**源同步**的，数据只在 `vld_i='1'` 时才流动。
- **简单双口 RAM `psi_common_sdp_ram`**（u3-l1）：包括 `depth_g/width_g`、同步读的 1 拍读延迟、`ram_behavior_g`（RBW/WBR）、`shared variable` 存储建模与 `ram_style` 综合属性。本讲的 BRAM 分支就是直接例化它。
- **`log2ceil` 推导位宽**（u2-l1）：地址位宽、`del_i` 端口宽度都由它自动推导。

两个关键直觉先建立起来：

1. **延迟 = 把数据「排队」若干拍后再输出。** 本质上需要一个先进先出的「移位队列」。队列越深，要存的历史样本越多。
2. **FPGA 上「存历史」有三种资源**：触发器 FF（快、但贵，1 个 FF 存 1 bit）、SRL（Shift Register LUT，用查找表当移位寄存器，密度高、适合中等深度）、BRAM（块存储，容量大、但有固定读延迟、不可复位内容）。延迟线的全部设计艺术，就是**根据延迟深度选对资源**。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [hdl/psi_common_delay.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd) | **固定延迟**组件。延迟深度由 generic `delay_g` 在综合时钉死，可在 SRL/BRAM/AUTO 三种实现间切换。 |
| [hdl/psi_common_delay_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay_cfg.vhd) | **运行时可配置延迟**组件。延迟深度由端口 `del_i` 在运行时设置，同时包含 SRL（小延迟）与 BRAM（大延迟）两条路径。 |
| [hdl/psi_common_sdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd) | 两个延迟组件的 BRAM 分支共同复用的**简单双口 RAM**（u3-l1 已学）。 |
| [testbench/psi_common_delay_tb/psi_common_delay_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_delay_tb/psi_common_delay_tb.vhd) | 固定延迟的自校验测试平台，验证三种 resource 与不同 delay 的功能等价性。 |
| [testbench/psi_common_delay_cfg_tb/psi_common_delay_cfg_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_delay_cfg_tb/psi_common_delay_cfg_tb.vhd) | 可配置延迟的自校验测试平台，重点验证运行时改延迟（含 hold 模式）。 |

## 4. 核心概念与源码讲解

### 4.1 delay 固定延迟：结构与「存储抽头 + 输出寄存器」

#### 4.1.1 概念说明

「延迟线」的作用是把输入数据**原样**推迟若干拍后输出，常用于：对齐两条路径的延迟、补偿其它模块的流水线延迟、构建数据重排等。

最朴素的实现是「一串 FF」：延迟 N 拍就用 N 个寄存器首尾相连。问题在于 FF 是 FPGA 上最贵的存储资源之一——延迟 1000 拍、位宽 16 bit 就要 16000 个 FF，极其浪费。

`psi_common_delay` 的核心思想是：**用 FPGA 的存储资源（SRL 或 BRAM）来当移位队列**，只把最后一级留在 FF 上以改善时序（RAM 输出通常较慢）。同时它解决了一个 RAM 延迟线特有的麻烦：**RAM 内容不可复位**，复位后旧数据仍残留在存储单元里。该组件不靠「复位后重写 RAM」（那需要时间），而是在**输出端**用逻辑把残留数据替换成 0，从而复位后第一拍即可正常工作。

#### 4.1.2 核心流程

延迟线被拆成两部分（这是理解全组件最重要的一步）：

\[ \text{总延迟} = \underbrace{(\text{delay\_g} - 1)}_{\text{存储抽头 MemTaps\_c}} + \underbrace{1}_{\text{输出寄存器}} = \text{delay\_g} \text{ 拍} \]

即常量 `MemTaps_c = delay_g - 1` 决定了「存储资源里要放几个抽头」，而最后 1 拍恒由输出寄存器 `p_outreg` 提供。于是：

- `delay_g = 1`：不需要任何存储，输出寄存器本身就提供 1 拍延迟。
- `delay_g = 10`：存储里放 9 个抽头 + 1 个输出寄存器。

伪代码（数据流）：

```
dat_i ──► [ 存储抽头 × MemTaps_c ] ──► MemOut ──► [ 输出寄存器 ] ──► dat_o
                                         │
                         （存储不可复位，故在输出寄存器处把复位后的残留替换为 0）
```

所有动作都由 `vld_i` 选通：只有 `vld_i='1'` 的拍，数据才在队列里前进一格。因此**延迟是以「有效样本」计的，不是以绝对时钟周期计**——当 `vld_i` 每拍都高时，样本延迟 = 时钟延迟；当 `vld_i` 断续时，队列只在有效拍移位。

#### 4.1.3 源码精读

先看端口与 generic（注意 `delay_g`、`resource_g`、`bram_threshold_g`、`rst_state_g`）：

[hdl/psi_common_delay.vhd:20-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L20-L34) — 实体声明，固定延迟的全部 generic 与端口。

关键的存储抽头常量：

[hdl/psi_common_delay.vhd:38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L38) — `MemTaps_c := delay_g - 1`，把总延迟拆成「存储抽头 + 输出寄存器」的依据。

三个 `if generate` 分支的「单级直通」分支（`delay_g=1` 时存储为空）：

[hdl/psi_common_delay.vhd:119-121](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L119-L121) — `g_single`：`delay_g=1` 时 `MemOut <= dat_i`，延迟全部由输出寄存器提供。

最后是所有分支共用的输出寄存器，它同时承担「输出打拍改善时序」和「复位后把 RAM 残留替换为 0」两件事：

[hdl/psi_common_delay.vhd:124-142](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L124-L142) — `p_outreg`：复位后用 `RstStateCnt` 计数到 `delay_g-1`，期间强制输出 0；计数满后再放行 `MemOut`。`rst_state_g=false` 则跳过清零、直接输出存储现有内容。

> 关键点：`rst_state_g`（默认 `True`）控制复位行为。`True` = 复位后输出 `delay_g` 拍的 0（掩盖 RAM 残留）；`False` = 复位后立即输出存储里原有的（可能是陈旧的）数据。这个 generic 之所以放在输出寄存器里实现，正是文档强调的「无需花时间重写 RAM，复位后第一拍即可用」。

#### 4.1.4 代码实践

**目标**：通过阅读测试平台，验证「无论 `resource_g` 取哪种，固定延迟的功能行为完全一致」。

**步骤**：

1. 打开 [testbench/psi_common_delay_tb/psi_common_delay_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_delay_tb/psi_common_delay_tb.vhd)。
2. 定位「Vld high constantly」段（约 L110-L125），阅读断言：
   - `if i < delay_g + 1 then assert unsigned(dat_o) = 0`（队头还没排满，输出为 0）
   - `else assert unsigned(dat_o) = i - 1 - delay_g`（队头排出，正好延迟 `delay_g` 个有效样本）
3. 注意该断言里**没有任何对 `resource_g` 的判断**——同一个断言对 SRL/BRAM/AUTO 都成立。

**需要观察的现象**：无论 `resource_g` 是 `BRAM`、`SRL` 还是 `AUTO`，输出序列都满足 `dat_o = i - delay_g`（连续 `vld` 时）。

**预期结果**：三种实现功能完全等价，差异只在底层 FPGA 资源占用（需综合才能看到，见 4.5 综合实践）。

> 该回归组合已在 `sim/config.tcl` 的 L292-L299 注册（`-gresource_g=BRAM/SRL/AUTO` 以及 `-gresource_g=BRAM -gdelay_g=3 -gram_behavior_g=RBW/WBR`），可直接跑。

#### 4.1.5 小练习与答案

**练习 1**：若 `delay_g=1`、`resource_g="BRAM"`，组件会综合出 BRAM 吗？
**答案**：不会。`g_bram` 与 `g_srl` 分支都要求 `delay_g > 1`；`delay_g=1` 走 `g_single`，`MemOut <= dat_i`，只有 1 个输出寄存器，不消耗任何 RAM/SRL。

**练习 2**：复位后 `rst_state_g=True` 时，输出在前几拍是什么？为什么这样做？
**答案**：复位后前 `delay_g` 个有效拍输出全 0，用 `RstStateCnt` 计数控制。因为底层 RAM/SRL 内容不可复位，残留的是旧数据；在输出端清零可以避免把这些陈旧数据当成有效输出，且不需要额外时间重写存储，复位后第一拍即可工作。

---

### 4.2 resource 选择：SRL / BRAM / AUTO 的取舍

#### 4.2.1 概念说明

`resource_g` 是 `psi_common_delay` 最有特色的 generic，它让同一个 RTL 在三种底层资源间切换：

- **`"SRL"`**：用查找表充当移位寄存器（Xilinx 称 SRL，Intel 称 MLAB/LUTRAM）。密度比 FF 高得多，适合**中小深度**延迟。本组件用一个数组信号加综合属性来「提示」综合器把它映射成 SRL。
- **`"BRAM"`**：用块 RAM 当循环缓冲。容量大，但有固定同步读延迟、内容不可复位，适合**大深度**延迟。组件要求 `delay_g >= 3` 才能用 BRAM。
- **`"AUTO"`**（默认）：由 `bram_threshold_g`（默认 128）做分界——延迟抽头数低于阈值走 SRL，达到或超过阈值走 BRAM，让工具按深度自动选最划算的资源。

#### 4.2.2 核心流程

分支判定的逻辑（三者互斥，由 `if generate` 在编译期选定）：

\[ \text{SRL 分支启用} \iff (\text{delay\_g} > 1) \land \big[(\text{resource\_g}=\text{SRL}) \lor (\text{resource\_g}=\text{AUTO} \land \text{delay\_g} < \text{bram\_threshold\_g})\big] \]

\[ \text{BRAM 分支启用} \iff (\text{delay\_g} > 1) \land \big[(\text{resource\_g}=\text{BRAM}) \lor (\text{resource\_g}=\text{AUTO} \land \text{delay\_g} \ge \text{bram\_threshold\_g})\big] \]

三道断言守住参数合法性：

1. `resource_g` 必须是 `AUTO/SRL/BRAM` 三者之一；
2. `resource_g="BRAM"` 时必须 `delay_g >= 3`（小延迟用 BRAM 地址逻辑不划算且地址位宽会出问题）；
3. `bram_threshold_g > 3`（保证 AUTO 模式下 SRL 有合法的取值区间）。

#### 4.2.3 源码精读

三道参数校验断言：

[hdl/psi_common_delay.vhd:46-48](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L46-L48) — 校验 `resource_g` 合法性、BRAM 最小延迟、`bram_threshold_g` 下限。

SRL 分支（含综合属性提示）：

[hdl/psi_common_delay.vhd:51-67](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L51-L67) — `g_srl`：声明 `MemTaps_c` 个元素的移位寄存器数组，`vld_i='1'` 时整体右移；通过 `shreg_extract="true"`、`srl_style="srl"` 两个属性引导综合器用 SRL 资源实现。

> 注意 L54-L55 的两个属性：`shreg_extract` 允许综合器把移位寄存器「抽取」进 LUT；`srl_style` 直接指定用 `srl`（而非 `register`/`block`）。这是 vendor 相关属性（Xilinx），但写在 RTL 里对不识别它的工具无害。

BRAM 分支（与 AUTO 阈值判定的另一半）：

[hdl/psi_common_delay.vhd:70-116](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L70-L116) — `g_bram`：`resource_g="BRAM"` 或（`AUTO` 且 `delay_g >= bram_threshold_g`）时启用，用循环地址 + `sdp_ram` 实现（详见 4.3）。

#### 4.2.4 代码实践

**目标**：在不改源码的前提下，预测不同 `(delay_g, resource_g, bram_threshold_g)` 组合下走哪个分支。

**步骤**：对下表每一行，依据 4.2.2 的判定式，写出会启用哪个 `generate` 分支（`g_srl` / `g_bram` / `g_single`）。

| `delay_g` | `resource_g` | `bram_threshold_g` | 预测分支 |
|:---:|:---:|:---:|:---|
| 1 | AUTO | 128 | ? |
| 64 | SRL | 128 | ? |
| 64 | AUTO | 128 | ? |
| 200 | AUTO | 128 | ? |
| 3 | BRAM | 128 | ? |
| 2 | BRAM | 128 | ? |

**需要观察的现象**：对照源码 L51 与 L70 的条件，逐行核对。

**预期结果**：依次为 `g_single`、`g_srl`、`g_srl`（64 < 128）、`g_bram`（200 ≥ 128）、`g_bram`、**断言报错**（`delay_g=2 < 3`，BRAM 不允许，L47 触发 `###ERROR###`）。

> 第 6 行是「待本地验证」的反面教材：它会在 elaborate 阶段直接报错，组件不会生成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bram_threshold_g` 必须大于 3？
**答案**：BRAM 分支要求 `delay_g >= 3`（见 L47 断言）。若 `bram_threshold_g <= 3`，AUTO 模式下 `delay_g` 在 `[2, bram_threshold_g)` 区间会无解——例如 `bram_threshold_g=3` 时，`delay_g=2` 既不满足 SRL（需要 `< 3`）也不满足 BRAM（需要 `>=3` 且 `>=3`），三个分支都不命中。故强制 `bram_threshold_g > 3` 留出合法 SRL 区间。

**练习 2**：同样 `delay_g=100`，`SRL` 与 `BRAM` 实现的资源形态有何不同？
**答案**：`SRL` 会综合成约 `width_g × 99` bit 的 LUT 移位寄存器（占用查找表资源，可能拆成多级 SRL 级联）；`BRAM` 会综合成 1 个深度 99、宽 `width_g` 的块 RAM（占用 1 个 BRAM 原语，但有同步读延迟）。深度越大，BRAM 越划算；深度很小则 SRL 更省。

---

### 4.3 RAM 复用：以 sdp_ram 实现循环缓冲

#### 4.3.1 概念说明

`psi_common_delay` 的 BRAM 分支没有自己重新写一段 RAM 模型，而是**直接例化** u3-l1 学过的 `psi_common_sdp_ram`。这是 PSI 库「底层存储只写一次、上层组件复用」思想的典型体现（`sync_fifo`/`async_fifo`/`delay` 都复用 `sdp_ram`）。

把 RAM 当延迟线的关键技巧是**循环缓冲（circular buffer）**：维护一对读/写指针 `WrAddr`/`RdAddr`，写指针每来一个有效样本就前进一格并在到达深度上限时回绕，读指针始终滞后写指针固定的步数。这样 RAM 里始终保存着「最近若干个样本」，读指针读出的就是「若干拍之前写入的数据」。

#### 4.3.2 核心流程

设 `MemTaps_c = delay_g - 1`，BRAM 深度即为 `MemTaps_c`，地址位宽为 `log2ceil(MemTaps_c)`。指针在复位时被设成「读滞后写 `MemTaps_c - 1` 步」：

\[ \text{WrAddr}_{\text{rst}} = \text{MemTaps\_c}-1,\qquad \text{RdAddr}_{\text{rst}} = 0 \]

之后每个 `vld_i='1'` 拍，两指针同步前进并在 `MemTaps_c-1` 处回绕，保持相对距离不变。总延迟由三段拼成：

\[ \text{BRAM 总延迟} = \underbrace{(\text{MemTaps\_c}-1)}_{\text{读指针滞后步数}} + \underbrace{1}_{\text{sdp\_ram 同步读延迟}} + \underbrace{1}_{\text{输出寄存器}} = \text{MemTaps\_c}+1 = \text{delay\_g} \]

这正是 4.1.2 公式在 BRAM 路径上的具体兑现。

伪代码（指针维护）：

```
on reset:
    WrAddr <= MemTaps_c - 1
    RdAddr <= 0
on vld_i='1':
    WrAddr <= (WrAddr == MemTaps_c-1) ? 0 : WrAddr+1
    RdAddr <= (RdAddr == MemTaps_c-1) ? 0 : RdAddr+1
RAM: 在 WrAddr 写 dat_i，在 RdAddr 同步读 → MemOut
```

#### 4.3.3 源码精读

地址维护进程（复位初值与回绕）：

[hdl/psi_common_delay.vhd:74-95](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L74-L95) — `p_bram`：复位时 `WrAddr <= MemTaps_c-1`、`RdAddr <= 0`；`vld_i='1'` 时两指针同步自增并在 `MemTaps_c-1` 处回绕。

例化 `sdp_ram`（注意深度、同步模式、`ram_behavior_g` 透传）：

[hdl/psi_common_delay.vhd:98-115](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L98-L115) — `i_bram`：`depth_g => MemTaps_c`、`is_async_g => false`（单时钟）、`ram_style_g => "auto"`、`ram_behavior_g` 由上层透传；`wr_i`/`rd_i` 都接 `vld_i`，`rd_clk_i` 接 `ground_c`（同步模式下读时钟被忽略）。

对照底层 RAM 的实现（u3-l1）：

[hdl/psi_common_sdp_ram.vhd:37-40](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L37-L40) — `shared variable mem` + `ram_style` 属性，正是延迟线 BRAM 分支赖以获得块 RAM 资源的底层机制。

[hdl/psi_common_sdp_ram.vhd:44-63](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L44-L63) — 同步读进程，由 `ram_behavior_g` 决定同地址读写时是 RBW（读得旧值）还是 WBR（读得新值）。延迟线透传该 generic，以便匹配不同 FPGA 原生 RAM 的语义（见 u3-l1）。

> 设计要点：延迟线把 `ram_behavior_g` 一路透传到 `sdp_ram`，是为了让循环缓冲在不同工艺下都能正确综合——某些 FPGA 的 BRAM 原生是 RBW，另一些（如部分 LUT-RAM）是 WBR，选对行为才能避免综合器插入额外逻辑或报时序违例。

#### 4.3.4 代码实践

**目标**：跟踪 `delay_g=3`、`resource_g="BRAM"` 时一个样本从输入到输出的完整路径。

**步骤**：

1. 算出 `MemTaps_c = delay_g - 1 = 2`，故 BRAM 深度为 2，地址位宽 `log2ceil(2) = 1`。
2. 复位后：`WrAddr = MemTaps_c-1 = 1`，`RdAddr = 0`。
3. 假设连续 `vld_i='1'`，输入序列 `d0, d1, d2, d3 ...`，逐拍写下 `WrAddr/RdAddr` 的变化与 RAM 里写入的内容。
4. 算出 `d0` 在哪一拍出现在 `dat_o`（应正好是第 `delay_g=3` 个有效样本之后）。

**需要观察的现象**：读指针始终滞后写指针 1 步（`MemTaps_c-1=1`），再加上 `sdp_ram` 的 1 拍同步读延迟和 1 拍输出寄存器，合计 3 拍。

**预期结果**：`d0` 在输入后的第 3 个有效拍出现在 `dat_o`，与测试平台「Vld high constantly」段的断言 `dat_o = i - 1 - delay_g` 一致（该组合已由 `sim/config.tcl` L297-L298 的 `-gresource_g=BRAM -gdelay_g=3` 回归覆盖）。

> 若你手工推导时序拿不准，可直接跑 `sim/config.tcl` 中 `-gresource_g=BRAM -gdelay_g=3 -gram_behavior_g=RBW` 这一回归项，用波形核对（待本地验证具体波形）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rd_clk_i => ground_c`（接常量 '0'）也能正常工作？
**答案**：因为 `is_async_g => false`（同步模式），`sdp_ram` 在 `g_sync` 分支里只用 `wr_clk_i` 一个时钟同时完成读写（见 sdp_ram L44-L63），`rd_clk_i` 仅在异步分支才使用，故同步模式下接 `ground_c` 即可。

**练习 2**：把 `ram_behavior_g` 从 `RBW` 改成 `WBR`，延迟线的总延迟会变吗？
**答案**：不会。`RBW`/`WBR` 只影响「同一拍对同一地址既读又写」时读到新值还是旧值；延迟线的读写指针始终指向不同地址（读滞后写），不会同时命中同一格，故行为对延迟长度无影响。它存在的意义是匹配不同 FPGA 原生 RAM 的语义以利于综合。

---

### 4.4 delay_cfg：运行时可配置延迟

#### 4.4.1 概念说明

`psi_common_delay` 的延迟深度在综合时钉死。当你需要**运行时动态改变延迟**（例如软件可调的对齐、自适应补偿）时，就要用 `psi_common_delay_cfg`：它把延迟值从 generic 搬到了端口 `del_i`，由寄存器在运行时写入。

可配置带来了三个新问题，组件分别给出了解法：

1. **BRAM 有 3 拍固有流水线延迟**（写地址建立 + 同步读 + 输出寄存器）。当 `del_i <= 3` 时，BRAM 的循环地址公式会失效（读地址会跑到写地址前面），所以小延迟必须改走一条 SRL 支路。
2. **运行时减小延迟**需要等 3 拍流水线排空才生效（见源码头注释）。
3. **运行时增大延迟**会让读指针「跳」到更靠后的位置，输出端出现短暂毛刺；`hold_g` 用一个 RS 锁存器在增大延迟期间冻结读地址、平滑过渡。

#### 4.4.2 核心流程

端口 `del_i` 的位宽由 `max_delay_g` 自动推导：

\[ \text{del\_i 位宽} = \text{log2ceil}(\text{max\_delay\_g}) \]

故 `max_delay_g=256` 时 `del_i` 为 8 bit，可表示 0..255（实际可设最大延迟为 `max_delay_g-1`）。BRAM 深度向上取整到 2 的幂：

\[ \text{BRAM 深度} = 2^{\,\text{log2ceil}(\text{max\_delay\_g})} \]

输出选择由 `del_i` 当前值动态决定（一个多路选择）：

\[ \text{mem\_out\_s} = \begin{cases} \text{dat\_i} & \text{del\_i} = 1 \\ \text{srl\_s}(0) & \text{del\_i} = 2 \\ \text{srl\_s}(1) & \text{del\_i} = 3 \\ \text{mem\_out2\_s (BRAM)} & \text{del\_i} > 3 \end{cases} \]

BRAM 路径的读地址（注意 `+3` 补偿 BRAM 固有 3 拍延迟）：

\[ \text{rd\_addr\_s} = \text{wr\_addr\_s} - \text{del\_i} + 3 \]

即 BRAM 自身贡献固定 3 拍，读指针再多滞后 `del_i - 3` 步，合计 `del_i` 拍。这正是 `del_i <= 3` 不能走 BRAM 的根本原因（会出现非负的无效偏移）。

伪代码（hold 模式，增大延迟时）：

```
del_dff_s <= del_i            -- del_i 打一拍，用于检测变化
if (del_dff_s < del_i):       -- 检测到延迟增大
    rs_s <= '1'               -- 进入 hold
    diff_s <= del_i - del_dff_s
    latch_count_s <= 0
elif (latch_count_s == diff_s - 2):
    rs_s <= '0'               -- hold 结束

if (rs_s == '1' and hold_g):
    rd_addr_s <= rd_addr_s    -- 冻结读地址，平滑过渡
else:
    rd_addr_s <= wr_addr_s - del_i + 3
```

#### 4.4.3 源码精读

实体与 `del_i` 端口（位宽自动推导）：

[hdl/psi_common_delay_cfg.vhd:24-37](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay_cfg.vhd#L24-L37) — 实体声明，注意 L34 的 `del_i : in std_logic_vector(log2ceil(max_delay_g) - 1 downto 0)`。

地址控制进程（含 `+3` 偏移与 hold 逻辑）：

[hdl/psi_common_delay_cfg.vhd:53-94](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay_cfg.vhd#L53-L94) — `p_bram`：维护 `wr_addr_s`，按 `del_i` 计算 `rd_addr_s`（L75 的 `wr_addr_s - del_i + 3`），并用 RS 锁存 `rs_s` 与计数器 `latch_count_s` 在增大延迟时冻结读地址。

BRAM 例化（深度向上取整到 2 的幂）：

[hdl/psi_common_delay_cfg.vhd:97-112](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay_cfg.vhd#L97-L112) — `i_bram`：`depth_g => 2**log2ceil(max_delay_g)`（L99），同样复用 `sdp_ram`。

小延迟 SRL 支路与输出多路选择：

[hdl/psi_common_delay_cfg.vhd:115-130](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay_cfg.vhd#L115-L130) — `p_srl` 维护 3 级 SRL；`mem_out_s` 由 `del_i` 当前值在 `dat_i / srl_s(0) / srl_s(1) / mem_out2_s` 间四选一。

> 关键差异（对比 `delay`）：`delay_cfg` 没有 `resource_g`——它**同时**实例化了 SRL 和 BRAM 两条路径，由 `del_i` 的运行时值经多路器选择输出。这是「可配置」的代价：资源占用比固定延迟高（两条路径都要存在），但换来了运行时灵活性。另外它没有 `rst_state_g`/输出清零机制，复位仅复位地址与寄存器，RAM 残留数据靠正常数据流冲刷。

#### 4.4.4 代码实践

**目标**：通过测试平台理解「运行时改变延迟」的两种方向（增大 / 减小）的行为差异。

**步骤**：

1. 打开 [testbench/psi_common_delay_cfg_tb/psi_common_delay_cfg_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_delay_cfg_tb/psi_common_delay_cfg_tb.vhd)。
2. 定位「change delay 33 -> 13」段（约 L147-L164）：在数据流中（`i=75` 时）把 `del_sti` 从 33 改成 13（**减小**延迟）。注意 L157-L162 的断言：变化后的前 3 拍仍按旧延迟校验（`i - 1 - prev_delay_v`），第 4 拍起才按新延迟校验。
3. 再看「Vld toggling」段（约 L180-L191）：`hold_g=True` 下改变延迟时，输出在过渡期保持 `hold_v`（旧值）。

**需要观察的现象**：
- **减小延迟**：约 3 拍后新延迟生效（对应源码头注释「delay decrease takes 3 clock cycles」）。
- **增大延迟**（`hold_g=True`）：输出先「保持」若干拍（由 `diff_s` 决定），再切到新延迟，避免毛刺。

**预期结果**：与上述断言一致；该 TB 在 `sim/config.tcl` L225-L229 以 `max_delay_g=50` 和 `max_delay_g=100` 两组参数注册回归。

> `hold_g` 在该 TB 里被硬编码为 `True`（见 TB L91 注释 `-- DO NOT EDIT in this TESTBENCH`），故 `hold_g=false` 的行为「待本地验证」——你可以在自己的例化里设 `hold_g=>false` 观察读地址立即跟随、输出出现短暂过渡值的现象。

#### 4.4.5 小练习与答案

**练习 1**：`max_delay_g=100` 时，`del_i` 几位宽？BRAM 实际深度是多少？
**答案**：`log2ceil(100) = 7`（因 \(2^6=64 < 100 \le 128 = 2^7\)），故 `del_i` 为 7 bit（可表示 0..127）；BRAM 深度 `2**log2ceil(100) = 2**7 = 128`（比 100 多分配了 28 格，这是向上取整到 2 的幂的代价）。

**练习 2**：为什么 BRAM 路径的读地址公式是 `wr_addr_s - del_i + 3`，而不是 `wr_addr_s - del_i`？
**答案**：因为 BRAM 路径自身有 3 拍固有流水线延迟（写地址建立 1 拍 + `sdp_ram` 同步读 1 拍 + 输出寄存器 1 拍）。若读地址直接滞后 `del_i` 步，总延迟会变成 `del_i + 3`；用 `del_i - 3` 的滞后量正好让固有 3 拍 + 存储偏移 `del_i - 3` = `del_i` 拍。也正因如此 `del_i <= 3` 时该公式会给出非负/非法偏移，必须改走 SRL。

**练习 3**：把 `hold_g` 设为 `false` 会怎样？
**答案**：增大延迟时不再冻结读地址，`rd_addr_s` 立即跳到新位置，输出端可能出现短暂的非预期值（毛刺）；好处是延迟变化即时反映，过渡更快。适合对短暂过渡不敏感、要求延迟立即生效的场景。

---

## 5. 综合实践

**任务**：比较 `psi_common_delay` 在 `BRAM` 与 `SRL` 两种 `resource_g` 下、不同 `delay_g` 的资源占用思路，并验证二者功能等价。

**背景**：`sim/config.tcl` 已为 `psi_common_delay_tb` 注册了多组回归（L292-L299）：`resource_g=BRAM/SRL/AUTO`，以及 `delay_g=3` 配 `RBW/WBR` 的边界组合。这些回归**只验证功能等价**（同一断言对所有 resource 成立），看不到资源差异——资源差异必须经综合/实现才能看到。

**操作步骤**：

1. **功能验证（仿真）**：
   - 按 u1-l3 的方式跑 `psi_common_delay_tb` 的三组回归（`BRAM`/`SRL`/`AUTO`），确认全部无 `###ERROR###`。这证明三种实现行为一致。

2. **资源对比（综合，待本地验证具体数字）**：在自己工程的测试顶层里，例化三个 `psi_common_delay`，仅 `resource_g` 不同，其余参数相同：

   ```vhdl
   -- 示例代码：仅用于资源对比的例化片段，非库内原有代码
   g_small : entity work.psi_common_delay
       generic map ( width_g => 16, delay_g => 16,  resource_g => "SRL",  bram_threshold_g => 128 )
       port map ( clk_i => clk, rst_i => rst, dat_i => d, vld_i => v, dat_o => q_srl, vld_o => v_srl );

   g_bram  : entity work.psi_common_delay
       generic map ( width_g => 16, delay_g => 16,  resource_g => "BRAM", bram_threshold_g => 128 )
       port map ( clk_i => clk, rst_i => rst, dat_i => d, vld_i => v, dat_o => q_bram, vld_o => v_bram );

   g_auto  : entity work.psi_common_delay
       generic map ( width_g => 16, delay_g => 512, resource_g => "AUTO", bram_threshold_g => 128 )
       port map ( clk_i => clk, rst_i => rst, dat_i => d, vld_i => v, dat_o => q_auto, vld_o => v_auto );
   ```

   - 对 `delay_g=16`：综合后比较 `g_small`（应为 SRL/LUTRAM）与 `g_bram`（应为 1 个 BRAM 原语）的资源报告。预期 SRL 占用查找表资源、BRAM 占用 1 个块 RAM。
   - 对 `delay_g=512, AUTO`：确认它自动落在 BRAM（因 `512 >= bram_threshold_g=128`）。

3. **资源拐点观察**：固定 `resource_g="AUTO"`，扫描 `delay_g` 从 16 到 256，找到综合报告里从「SRL 为主」切换到「BRAM 为主」的拐点，验证它是否出现在 `bram_threshold_g`（128）附近。

**需要观察的现象**：功能上三者输出序列完全一致；资源上 SRL 用 LUT、BRAM 用块 RAM，AUTO 按 `bram_threshold_g` 自动切换。

**预期结果**：小延迟 SRL 更省、大延迟 BRAM 更省；AUTO 在阈值处自动选优。具体 LUT/BRAM 个数「待本地验证」（依赖目标 FPGA 型号与综合工具）。

> 提示：`srl_style` 与 `ram_style` 是 vendor 相关属性（主要面向 Xilinx），在 Intel/其它工具上行为可能不同；跨厂商对比资源时需留意（待本地验证）。

## 6. 本讲小结

- **延迟线 = 存储抽头 + 输出寄存器**：`psi_common_delay` 把总延迟 `delay_g` 拆成 `MemTaps_c = delay_g - 1` 个存储抽头 + 1 个输出寄存器，存储用 SRL/BRAM、末级用 FF 改善时序。
- **`resource_g` 三选一**：`SRL`（中小深度，LUT 移位寄存器）、`BRAM`（大深度，块 RAM，要求 `delay_g >= 3`）、`AUTO`（按 `bram_threshold_g` 默认 128 自动切换）。
- **RAM 复用**：BRAM 分支直接例化 `psi_common_sdp_ram`，用读/写指针循环缓冲实现延迟，并把 `ram_behavior_g` 透传以匹配不同工艺的原生 RAM 语义。
- **RAM 不可复位的对策**：`rst_state_g=True` 时在输出寄存器处用计数器把复位后前 `delay_g` 拍强制清零，掩盖 RAM 残留，复位后第一拍即可用。
- **`delay_cfg` 运行时可配置**：延迟值搬到端口 `del_i`（位宽 `log2ceil(max_delay_g)`），同时保留 SRL（`del_i<=3`）与 BRAM（`del_i>3`，读地址 `wr_addr - del_i + 3` 补偿 3 拍固有延迟）两条路径；`hold_g` 在增大延迟时冻结读地址防毛刺，减小延迟则需 3 拍生效。
- **共性**：两个组件都是 `vld_i` 选通的**源同步**延迟（无反压），延迟按有效样本计；都复用 `sdp_ram` 与 `log2ceil`/`from_uslv` 等 math_pkg 工具函数。

## 7. 下一步学习建议

- **乒乓缓冲 `psi_common_ping_pong`（u7-l4）**：同样以 `tdp_ram` 为底层存储，把延迟线的「单进单出」扩展为「写满一块、读另一块」的双缓冲，是连续数据流缓冲的下一步。
- **AXI 流水线 `psi_common_axi_multi_pl_stage`（u9-l4）**：如果你需要的是「带 AXI-S 握手与反压」的多级延迟（而非本讲的源同步延迟），可对比阅读它，体会「打拍」与「延迟线」在接口语义上的差别。
- **进阶实践**：尝试把本讲综合实践里的 `delay` 替换成 `delay_cfg`，在运行时通过 `del_i` 改变延迟，观察 `hold_g` 对输出毛刺的影响，体会「固定」与「可配置」两种风格的工程取舍。
