# 跨时钟域原理与约束、复位穿越

## 1. 本讲目标

本讲是 Open Logic 跨时钟域（Clock Domain Crossing，简称 CDC / CC）系列的第一讲，目标是让你在接触任何一个具体的 `olo_base_cc_*` 实体之前，先建立**全局共性**的认知。学完后你应当能够：

- 说清楚为什么跨时钟域需要专门的同步电路与时序约束，并掌握手动 `set_max_delay` 约束的写法；
- 区分 AMD（Vivado）的自动约束（scoped constraints）与其他厂商必须手动约束的差异；
- 理解为什么大多数跨时钟域实体都要「顺便」做一次**复位穿越**，以及 `Xxx_RstIn` / `Xxx_RstOut` 该怎么接；
- 读懂 `olo_base_cc_reset` 与作为同步器基础的 `olo_base_cc_bits` 的 RTL 实现；
- 会用官方的**选择表（Selection Table）**，针对「多 bit 状态低更新率」「单周期脉冲」等不同需求挑出正确的实体。

本讲不展开讲任何一个具体数据通路实体（`cc_pulse` / `cc_simple` / `cc_status` / `cc_handshake` 等的内部细节），那是后续讲义的主题；本讲只讲它们**共同遵守的原理**。

## 2. 前置知识

本讲依赖你在 [u1-l5](u1-l5-conventions-and-anatomy.md) 学到的两进程法、同步高有效复位约定，以及 [u2-l1](u2-l1-base-packages.md) 对 `olo_base_pkg_attribute`（跨厂商综合属性包）的认识。在此之上，补充几个本讲会用到的通俗概念：

- **时钟域（clock domain）**：由同一个时钟驱动的所有触发器构成一个时钟域。同一个域内的信号都「踩着同一个节拍」变化。
- **异步时钟（asynchronous clocks）**：两个时钟没有固定相位关系（例如由独立晶振产生，或 PLL 输出但未声明相互定时）。信号从一个异步时钟域进入另一个时，目的触发器的建立/保持时间可能被违反。
- **亚稳态（metastability）**：当建立/保持时间被违反时，目的触发器的输出会在 0 与 1 之间「悬停」一段时间才随机塌缩到某个电平。这个不确定电平若被后续逻辑当成有效值，就会导致功能错误。
- **同步器（synchronizer）**：用一串级联触发器（Open Logic 默认 2 级，可配 2~4 级）把亚稳态「等过去」——给电压足够长的时间塌缩，再送给后级逻辑使用。级数越多，亚稳态穿透概率越低（代价是延迟更高）。
- **时序约束（timing constraint）**：告诉综合/布局布线工具某条路径「注定是跨时钟域」的，不要按常规时序去分析它，而要按 CDC 规则（如 `set_max_delay ... -datapath_only`）处理。**同步器能降低亚稳态概率，但若不约束，工具仍会报时序违例；约束和电路二者缺一不可。**
- **scoped constraints（作用域约束）**：AMD Vivado 的一种机制——把约束文件绑定到某个具体模块实例（`read_xdc -ref <entity>`），工具会自动把约束「贴」到该模块的每个实例上。这就是 Open Logic 在 Vivado 下能「自动约束」的基础。

> 关键直觉：**电路（同步器）解决「物理上能不能稳定」，约束解决「工具认不认这条路是 CDC」**。两者一起，才是一次正确的跨时钟域。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| [doc/base/clock_crossing_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md) | 全部 `olo_base_cc_*` 共同遵守的「原则文档」：约束方式、复位处理、选择表。本讲的主干。 |
| [doc/base/olo_base_cc_reset.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_cc_reset.md) | `olo_base_cc_reset` 的说明文档，含复位穿越的波形级解释。 |
| [src/base/vhdl/olo_base_cc_reset.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd) | 复位穿越实体的 RTL 实现：双向同步、应答反馈、全套综合属性。 |
| [src/base/vhdl/olo_base_cc_bits.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd) | 多 bit 同步器，是几乎所有跨时钟域实体的物理基础。 |
| [src/base/tcl/olo_base_constraints_amd.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/tcl/olo_base_constraints_amd.tcl) | AMD（Vivado）自动约束的聚合脚本，逐个加载每个实体的 scoped 约束。 |
| [src/base/tcl/olo_base_cc_bits.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/tcl/olo_base_cc_bits.tcl) | `olo_base_cc_bits` 的 scoped 约束样板，演示 `set_max_delay` + `set_bus_skew` 的标准写法。 |

> 说明：原则文档里让你 `source .../src/base/tcl/constraints_amd.tcl`，但仓库中实际的聚合脚本名为 `olo_base_constraints_amd.tcl`（`**/constraints_amd.tcl` 在当前 HEAD 并不存在）。以仓库实际文件为准。

## 4. 核心概念与源码讲解

### 4.1 跨时钟域与时序约束：手动约束与 AMD 自动约束

#### 4.1.1 概念说明

跨时钟域的信号在目的域里天然不满足常规时序（建立/保持时间）。如果不在约束里明确声明「这条路是 CDC」，布局布线工具会：

- 把它当普通路径去优化，可能为了让它「时序收敛」而插入缓冲、改变走线，反而破坏同步器结构；
- 或者在时序报告里持续报这条路径为违例（false violation），淹没真正的时序问题。

所以 CDC 路径必须被**专门约束**。Open Logic 的官方原则文档规定：所有跨时钟域都需要下面这一对约束——一条管「源域 → 目的域」，一条管反方向（因为复位穿越等需要双向）。

Open Logic 同时提供两条路：

- **手动约束**：适用于任何厂商工具，你自己写 `set_max_delay`。
- **自动约束**：仅 AMD（Vivado）支持，靠 scoped constraints 文件自动识别 Open Logic 的所有跨时钟域并正确约束。

#### 4.1.2 核心流程

手动约束的核心是这一对命令，取**较快时钟的周期**作为 `max_delay`：

```
set_max_delay -from [get_clocks <src-clock>] -to [get_clocks <dst-clock>] -datapath_only <period-of-faster-clock>
set_max_delay -from [get_clocks <dst-clock>] -to [get_clocks <src-clock>] -datapath_only <period-of-faster-clock>
```

要点：

- `-datapath_only` 表示只约束数据路径、忽略时钟偏斜的启动/锁存检查——这是 CDC 路径的标准做法，因为两个异步时钟之间本来就没有可信的偏斜关系。
- 用「较快时钟的周期」作为上限，既给同步器足够时间塌缩亚稳态，又不至于把约束放得过松。
- **两条都要写**，因为像复位穿越这类机制需要在两个方向上都传递信号。

AMD 自动约束的流程则是：

1. 用 `import_sources.tcl` 把 Open Logic 导入 Vivado 工程时，约束会被自动应用（参见 HowTo）；
2. 若要手动启用，建一个空的 TCL 约束文件、**仅用于 implementation**，里面写一行 `source .../olo_base_constraints_amd.tcl`；
3. 该聚合脚本为每个 CDC 实体调用 `read_xdc -ref <entity> ... -unmanaged`，把各自的 scoped 约束贴到所有实例上，并把它们标记为 `PROCESSING_ORDER LATE`、`used_in_synthesis false`（约束只在实现阶段生效）。

> 版本注意：原则文档明确写着，自动约束目前**只对 AMD 工具有效**；其它厂商（Quartus/Efinity/Gowin/Libero 等）必须手动约束。且 2024.2 之前的 Vivado 从 Verilog 例化时也需要手动约束。

#### 4.1.3 源码精读

原则文档把手写约束的「官方模板」直接给出，并标注了工具与版本限制：

- [doc/base/clock_crossing_principles.md:L9-L16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L9-L16) —— 手动约束的一对 `set_max_delay`，这是所有非 AMD 工具必须照抄的模板。
- [doc/base/clock_crossing_principles.md:L18-L22](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L18-L22) —— 明确「自动约束仅 AMD；其它工具须手动；Vivado 2024.2 前从 Verilog 例化也须手动」。
- [doc/base/clock_crossing_principles.md:L24-L36](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L24-L36) —— AMD 自动约束的三步启用方法。

AMD 聚合脚本展示了「自动」到底自动在哪——它只是替你把每个实体的 scoped 约束文件逐个加载进来：

```tcl
# 摘自 olo_base_constraints_amd.tcl
read_xdc -quiet -ref olo_base_cc_reset $fileLoc/olo_base_cc_reset.tcl -unmanaged
read_xdc -quiet -ref olo_base_cc_bits   $fileLoc/olo_base_cc_bits.tcl   -unmanaged
read_xdc -quiet -ref olo_base_cc_simple $fileLoc/olo_base_cc_simple.tcl -unmanaged
...
set_property used_in_synthesis false [get_files $fileLoc/olo_base_cc_reset.tcl]
...
set_property PROCESSING_ORDER LATE   [get_files $fileLoc/olo_base_cc_reset.tcl]
```

- [src/base/tcl/olo_base_constraints_amd.tcl:L8-L24](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/tcl/olo_base_constraints_amd.tcl#L8-L24) —— `-ref <entity>` 把约束绑定到该实体的所有实例；`-unmanaged` 让 Vivado 不去校验路径对象是否存在；`LATE` 顺序确保在用户约束之后处理。

单个 scoped 约束文件长什么样？以 `olo_base_cc_bits.tcl` 为例，它展示了 CDC 约束的标准写法——**用单元（cell）而非端口定位时钟**，因为 scoped 约束里端口名不可靠：

```tcl
# 摘自 olo_base_cc_bits.tcl
set launch_clk [get_clocks -of_objects [get_cell RegIn*]]   ;# 源域时钟
set latch_clk  [get_clocks -of_objects [get_cell Reg0*]]    ;# 目的域时钟
set period [get_property -min PERIOD [concat $launch_clk $latch_clk]]
set_max_delay -from $launch_clk -to [get_cell Reg0*] -datapath_only $period
set_bus_skew  -from [get_cell RegIn*] -to [get_cell Reg0*]  $period
```

- [src/base/tcl/olo_base_cc_bits.tcl:L10-L16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/tcl/olo_base_cc_bits.tcl#L10-L16) —— `set_max_delay` 限制单条路径延迟；`set_bus_skew` 限制**同一总线各位之间的偏斜**，这对多 bit 数据安全穿越至关重要（防止各位到达目的域的时间差过大导致采样到「半新半旧」的值）。

注意：scoped 约束文件只给 `cc_reset`、`cc_bits`、`cc_simple` 三个「底层」实体提供了。`cc_pulse`、`cc_status`、`cc_handshake` 等是**基于这三个底层实体搭建**的，因此它们的 CDC 路径会被底层实体的 scoped 约束自动覆盖（详见 4.4）。这正是 Vivado 自动约束能「一次覆盖全库」的原因。

#### 4.1.4 代码实践

**实践目标**：手写一段非 AMD 工具下的通用 CDC 约束，理解每个开关的含义。

**操作步骤**：

1. 假设工程里有两个时钟：`clk_core`（100 MHz，周期 10 ns）与 `clk_spi`（50 MHz，周期 20 ns）。较快周期为 10 ns。
2. 写一个 XDC/TCL 片段（示例代码，非项目原文件）：

   ```tcl
   # 示例代码：手写 CDC 约束（clk_core <-> clk_spi）
   set_max_delay -from [get_clocks clk_core] -to [get_clocks clk_spi] -datapath_only 10.0
   set_max_delay -from [get_clocks clk_spi]  -to [get_clocks clk_core] -datapath_only 10.0
   ```

3. 对照原则文档 [doc/base/clock_crossing_principles.md:L9-L16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L9-L16) 逐字核对，确认两条方向都写了、`-datapath_only` 存在、周期取的是较快时钟。

**需要观察的现象**：在 Vivado 里把这段约束替换为你工程里真实的两个时钟名，打开实现后的时序报告，确认原先报红的 CDC 路径被归入 `set_max_delay` 约束、不再出现建立时间违例。

**预期结果**：CDC 路径在 `report_timing_summary` 里表现为由 `set_max_delay` 覆盖，而非由默认时钟关系推导。若你用的不是 AMD 工具，这就是你必须为每个跨时钟域手写的「标配」。

> 待本地验证：上述 Vivado 报告现象需在你本机工程中确认；本讲无法替你运行 GUI 工具。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `set_max_delay` 要带 `-datapath_only`？
**答案**：两个异步时钟之间没有可信的时钟偏斜/启动-锁存关系，`-datapath_only` 告诉工具只校验数据路径延迟、忽略源/目的时钟的偏斜与抖动检查，这正是 CDC 路径想要的语义。

**练习 2**：你的工程用了 Quartus，能不能依赖 Open Logic 的 scoped constraints 自动搞定约束？
**答案**：不能。自动约束仅对 AMD（Vivado）有效；Quartus 等其它厂商工具必须按 [doc/base/clock_crossing_principles.md:L9-L16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L9-L16) 的模板手动写两条 `set_max_delay`。

### 4.2 复位穿越：为什么需要、怎么连接

#### 4.2.1 概念说明

绝大多数跨时钟域实体的两侧各有自己的逻辑，这些逻辑各自属于不同的时钟域。**如果两侧不同时复位，就会在复位释放前后的「边角时刻」出现一侧已开始工作、另一侧仍被钳制的不一致**，导致握手信号、指针、计数器错位。原则文档的原话是：「通常时钟跨越的两侧必须同时复位，以避免复位周围的边角条件产生非预期行为。」

为此，Open Logic 把「复位穿越」做成了标准件 `olo_base_cc_reset`：当任一时钟域收到复位请求，它在**两个时钟域都拉高复位**，并保证「两侧同时处于复位至少一个时钟周期，再一起释放」。

关键约定（用户视角，黑盒）：

- 每个跨时钟域实体在两侧都提供 **`Xxx_RstIn`（复位请求输入）** 与 **`Xxx_RstOut`（复位有效输出）**。
- 你把外部的复位请求接到 `RstIn`；
- **周围需要跟着复位一起复位的逻辑，一律接 `RstOut`，而不是接 `RstIn`。**

#### 4.2.2 核心流程

一次复位穿越的流程（以 A 域收到 `A_RstIn` 为例）：

1. A 域检测到 `A_RstIn=1`，立即置位本域的复位锁存 → `A_RstOut` 拉高，A 域逻辑进入复位；
2. 该复位被异步地同步进 B 域（异步置位、同步释放的 FF 链）→ `B_RstOut` 拉高，B 域逻辑也进入复位；
3. B 域的复位被「应答」回 A 域，确认对侧确实收到了；
4. A 域收到应答后，才允许释放本域锁存 → `A_RstOut` 释放；随后 `B_RstOut` 也释放。

这条「请求 → 双侧置位 → 应答 → 释放」的链路，保证了**至少有一个完整时钟周期，两侧同时处于复位**。

#### 4.2.3 源码精读

原则文档把「为什么要 RstIn/RstOut、怎么接」讲得很直接：

- [doc/base/clock_crossing_principles.md:L38-L53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L38-L53) —— 复位处理总述：哪些逻辑该接 `Xxx_RstOut`。
- [doc/base/olo_base_cc_reset.md:L17-L25](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_cc_reset.md#L17-L25) —— 双向同步、保证两侧同时复位至少一拍；外部请求接 `RstIn`，逻辑用 `RstOut` 复位。
- [doc/base/olo_base_cc_reset.md:L55-L68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_cc_reset.md#L55-L68) —— 波形级解释：`RstALatch` → `A_RstOut` → `RstRqstA2B` → `B_RstOut` → `RstAckB2A` → 释放，最终保证两侧同拍复位。

接口层面，实体在两侧各给出 `Clk/RstIn/RstOut` 三件套：

```vhdl
-- 摘自 olo_base_cc_reset.vhd
port (
    A_Clk    : in  std_logic;
    A_RstIn  : in  std_logic := '0';   -- A 域复位请求
    A_RstOut : out std_logic;          -- A 域复位有效（用它复位 A 域逻辑）
    B_Clk    : in  std_logic;
    B_RstIn  : in  std_logic := '0';
    B_RstOut : out std_logic
);
```

- [src/base/vhdl/olo_base_cc_reset.vhd:L34-L46](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd#L34-L46) —— 实体端口；`RstIn` 默认 `'0'`（不强制接），`RstOut` 无默认值（必须用）。

#### 4.2.4 代码实践

**实践目标**：把一个跨时钟域实体（如 `olo_base_cc_pulse`）的复位端口正确接好，并说明为什么周围逻辑要接 `RstOut` 而非 `RstIn`。

**操作步骤**：

1. 打开 [src/base/vhdl/olo_base_cc_pulse.vhd:L38-L47](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L38-L47)，确认它在 `In_*` 与 `Out_*` 两侧各暴露了 `RstIn` 与 `RstOut`。
2. 在它内部找到对 `olo_base_cc_reset` 的例化：[src/base/vhdl/olo_base_cc_pulse.vhd:L69-L80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L69-L80)，看清 `In_RstIn/Out_RstIn` 如何进入 cc_reset、`RstOut` 如何被引出给外部。
3. 画一张接线图：你的「源域复位」→ `In_RstIn`，源域周边逻辑的复位端 ← `In_RstOut`；目的域同理。

**需要观察的现象**：在波形上（参照 [doc/base/olo_base_cc_reset.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_cc_reset.md) 的波形描述）确认：仅拉 `In_RstIn` 时，`Out_RstOut` 也会被拉高，且二者存在一段重叠期。

**预期结果**：源域周边逻辑和目的域周边逻辑都用各自的 `RstOut` 复位时，两侧在复位窗口内行为一致，不会出现「一侧已工作、一侧仍复位」的错位。

> 待本地验证：波形需在仿真中确认；本讲不代你跑仿真。

#### 4.2.5 小练习与答案

**练习 1**：某同学把跨时钟域实体两侧「需要跟着复位的逻辑」接到了 `RstIn` 上，会有什么隐患？
**答案**：`RstIn` 只是「复位请求」，不保证对侧域此刻也处于复位。这样周围逻辑可能在对侧还没复位完毕时就提前开始工作，造成握手/指针错位。正确做法是接 `RstOut`，它保证两侧同步复位。

**练习 2**：如果两侧都不需要外部复位（`RstIn` 都悬空），`RstOut` 还会有复位吗？
**答案**：会。`RstIn` 默认 `'0'`，但实体上电初值（见 4.3）会让两侧 `RstOut` 先各自拉高、完成一次上电复位穿越后再释放，从而保证上电时两侧同步进入已知状态。

### 4.3 cc_reset 与 cc_bits 的 RTL 实现

#### 4.3.1 概念说明

`olo_base_cc_bits` 是 Open Logic 里**最基础的同步器**：把若干个**互相独立**的单 bit 信号从源域同步到目的域，每一位都用一串级联触发器实现。它只适合「各位之间无相关性」的信号（例如若干独立的使能位），**不适合**直接同步一个多 bit 的数据总线（那会采样到「半新半旧」的值——多 bit 数据安全穿越要靠 `cc_simple`/`cc_status`/`cc_handshake` 等，见 4.4）。

`olo_base_cc_reset` 则是一个「双向复位同步器」：它内置异步置位/同步释放的复位 FF 链，并用 `olo_base_cc_bits` 在两个方向上回传「复位应答」，构成 4.2 描述的握手闭环。两者合起来，是几乎所有跨时钟域实体的物理底座。

#### 4.3.2 核心流程

**`olo_base_cc_bits`（单/多 bit 同步器）**：

1. `RegIn`：在源时钟域先把输入打一拍（隔离、规整）；
2. `Reg0` → `RegN(0..)`：在目的时钟域串成 `SyncStages_g` 级同步链（默认 2 级）；
3. `Out_Data` 取同步链最后一级；
4. 复位只作用于同步链本身，把它清成已知值。

**`olo_base_cc_reset`（双向复位穿越，以 A 域为例）**：

1. `RstALatch`：一旦 `A_RstIn=1` 就置位，只有收到对侧应答 `RstAckB2A` 才清除；
2. `RstRqstB2A`：B→A 方向的「复位请求」FF 链，被 `RstBLatch` 异步置位、在 `A_Clk` 下同步释放；
3. `A_RstOut <= RstALatch or RstRqstB2A(left)`：A 域复位 = 本域请求 **或** 对域传来的请求；
4. 两个 `olo_base_cc_bits` 实例分别把 B→A、A→B 的请求线回采为应答，闭环。

两侧逻辑完全对称（A/B 互换即可）。

#### 4.3.3 源码精读

先看同步器 `olo_base_cc_bits` 的同步链定义与进程：

```vhdl
-- 摘自 olo_base_cc_bits.vhd
type SyncStages_t is array(0 to SyncStages_g - 2) of std_logic_vector(Width_g - 1 downto 0);
signal RegIn : std_logic_vector(Width_g - 1 downto 0) := (others => '0');
signal Reg0  : std_logic_vector(Width_g - 1 downto 0) := (others => '0');
signal RegN  : SyncStages_t                           := (others => (others => '0'));
```

- [src/base/vhdl/olo_base_cc_bits.vhd:L58-L64](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L58-L64) —— `Reg0` 是目的域第一级，`RegN` 数组承载剩余 `SyncStages_g-1` 级；总级数 = `1 + (SyncStages_g-1)` = `SyncStages_g`。

输入侧先在源域打一拍：

```vhdl
-- 摘自 olo_base_cc_bits.vhd
p_inff : process (In_Clk) is
begin
    if rising_edge(In_Clk) then
        RegIn <= In_Data;
        if In_Rst = '1' then RegIn <= (others => '0'); end if;
    end if;
end process;
```

- [src/base/vhdl/olo_base_cc_bits.vhd:L109-L117](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L109-L117) —— 源域寄存，把异步输入规整为同步于 `In_Clk` 的信号，再做跨域。

目的域同步链：

```vhdl
-- 摘自 olo_base_cc_bits.vhd
p_outff : process (Out_Clk) is
begin
    if rising_edge(Out_Clk) then
        Reg0    <= RegIn;
        RegN(0) <= Reg0;
        for i in 1 to RegN'high loop
            RegN(i) <= RegN(i - 1);
        end loop;
        if Out_Rst = '1' then
            Reg0 <= (others => '0'); RegN <= (others => (others => '0'));
        end if;
    end if;
end process;
Out_Data <= RegN(RegN'high);
```

- [src/base/vhdl/olo_base_cc_bits.vhd:L120-L142](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L120-L142) —— 标准多级同步链；复位按 u1-l5 的「进程末尾覆盖」约定，只复位同步寄存器。

为了 AMD 自动约束能找到源时钟，文件还专门留了一个带 `dont_touch`/`keep` 的内部信号 `In_Clk_Sig`：

- [src/base/vhdl/olo_base_cc_bits.vhd:L97-L106](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L97-L106) —— 注释明说「required for automatic constraining in vivado」；scoped 约束 `olo_base_cc_bits.tcl` 正是用 `get_clocks -of_objects [get_cell RegIn*]` 反查这个时钟。

`olo_base_cc_reset` 的核心是「异步置位、同步释放」的复位 FF 链（这是所有 CDC 复位同步器的标准写法）：

```vhdl
-- 摘自 olo_base_cc_reset.vhd（A 域：同步 B 传来的复位请求）
p_a_rst_sync : process (RstBLatch, A_Clk) is
begin
    if RstBLatch = '1' then                       -- 异步置位
        RstRqstB2A <= (others => '1');
    elsif rising_edge(A_Clk) then                 -- 同步释放：逐拍推 0
        RstRqstB2A <= RstRqstB2A(RstRqstB2A'left - 1 downto 0) & '0';
    end if;
end process;
```

- [src/base/vhdl/olo_base_cc_reset.vhd:L104-L111](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd#L104-L111) —— 敏感表里同时有 `RstBLatch`（异步）与 `A_Clk`，实现「对侧一来复位立即在本域拉高、本域时钟下逐级释放」。

本域锁存与应答释放：

```vhdl
-- 摘自 olo_base_cc_reset.vhd（A 域锁存）
p_a_rst : process (A_Clk) is
begin
    if rising_edge(A_Clk) then
        if A_RstIn = '1' then
            RstALatch <= '1';                     -- 记下复位请求
        elsif RstAckB2A = '1' then                -- 等到对侧确认收到才释放
            RstALatch <= '0';
        end if;
    end if;
end process;
A_RstOut <= RstALatch or RstRqstB2A(RstRqstB2A'left);  -- 本域请求 或 对域请求
```

- [src/base/vhdl/olo_base_cc_reset.vhd:L113-L126](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd#L113-L126) —— 这就是 4.2 那条「请求 → 等应答 → 释放」闭环的代码体现。B 域是镜像，见 [src/base/vhdl/olo_base_cc_reset.vhd:L129-L151](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd#L129-L151)。

应答回传用两个 `olo_base_cc_bits`：

- [src/base/vhdl/olo_base_cc_reset.vhd:L154-L184](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd#L154-L184) —— `i_ackb2a` 把 B 域的请求线同步回 A 域作为 `RstAckB2A`，`i_acka2b` 反之；`Width_g=1`，因为只回传单根应答。

最后，注意两个实体都堆了一大摞综合属性——这是「Trustable Code」的体现：强制同步链被实现成独立触发器、不被合并、不被抽成移位寄存器（SRL）、被工具识别为异步寄存器。

- [src/base/vhdl/olo_base_cc_reset.vhd:L63-L99](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd#L63-L99) —— `shreg_extract=suppress`、`dont_merge`、`preserve`、`async_reg` 等属性；这些常量都来自 [u2-l1](u2-l1-base-packages.md) 讲过的 `olo_base_pkg_attribute`。
- [src/base/vhdl/olo_base_cc_bits.vhd:L66-L95](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L66-L95) —— 同一组属性作用于 `RegIn/Reg0/RegN`。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，确认「异步置位、同步释放」与「应答闭环」是如何在 RTL 里落地的。

**操作步骤**：

1. 打开 [src/base/vhdl/olo_base_cc_reset.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd)，在 `p_a_rst_sync`（L104）里指出实现「异步置位」的是进程敏感表里的 `RstBLatch`，实现「同步释放」的是 `elsif rising_edge(A_Clk)` 分支里的移位写法。
2. 在 [src/base/vhdl/olo_base_cc_reset.vhd:L154-L184](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd#L154-L184) 跟踪应答闭环：`RstRqstA2B(left)` → `i_ackb2a.In_Data` → `RstAckB2A` → `p_a_rst` 里清 `RstALatch`。
3. 在 [src/base/vhdl/olo_base_cc_bits.vhd:L66-L95](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L66-L95) 数一下同步链上每级触发器都挂了哪些属性，解释 `async_reg` 的作用。

**需要观察的现象**：在仿真里（把 `SyncStages_g=2`），人为在 `A_RstIn` 上加一个单拍脉冲，观察 `A_RstOut` 与 `B_RstOut` 都会拉高，并且在释放前存在「两侧同为 1」的至少一个 `A_Clk` 周期。

**预期结果**：`A_RstOut` 在 `A_RstIn` 拉高后几乎立即拉高；`B_RstOut` 经同步链后拉高；随后 `A_RstOut` 释放，`B_RstOut` 再释放——与 [doc/base/olo_base_cc_reset.md:L55-L68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_cc_reset.md#L55-L68) 的波形描述一致。

> 待本地验证：上述波形需在本机仿真中确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `olo_base_cc_bits` 不适合直接同步一条 16 bit 的数据总线？
**答案**：它的每一位是**独立**同步的，各位经过同步链后到达目的域的时间可能错开，目的域可能采样到「部分位是新值、部分位是旧值」的拼接值。多 bit 数据需用带握手/反馈的 `cc_simple`/`cc_status`/`cc_handshake`（见 4.4）保证一次采样拿到完整一致的值。

**练习 2**：`olo_base_cc_reset` 里为什么要把 `shreg_extract` 置为抑制？
**答案**：防止综合工具把同步链的若干级触发器「折叠」进一个移位寄存器原语（SRL/LUTRAM）。移位寄存器对亚稳态的恢复特性不同于独立触发器，会破坏同步器的可靠性，所以必须保持为独立 FF。

### 4.4 选择表：如何挑对实体

#### 4.4.1 概念说明

Open Logic 提供了一整套 `olo_base_cc_*` 实体加一个 `olo_base_fifo_async`，覆盖了几乎所有跨时钟域场景。但它们的代价各异：有的不耗 RAM、有的支持满吞吐、有的带反压、有的自带复位穿越。选错实体，要么资源浪费（用异步 FIFO 传一个偶发脉冲），要么功能不达标（用 `cc_bits` 传多 bit 数据）。

原则文档提供了一张**选择表**，把每个实体在 8 个维度上的能力列成矩阵，让你按需求查表挑选。本节教你怎么读这张表。

#### 4.4.2 核心流程

选型流程：

1. 明确你的数据特性：是**事件脉冲**还是**数据采样**？是否**多 bit**？是否需要**反压**？是否**满吞吐**？能否**耗 RAM**？两个时钟是否**真正异步**？
2. 在选择表里按列筛选，挑出满足所有「硬要求」的实体；
3. 在候选里再按「软偏好」（资源、性能）二选一。

选择表中的关键列含义（详见 [doc/base/clock_crossing_principles.md:L71-L88](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L71-L88)）：

- **Async. Clocks**：允许真正异步（相位不锁）的时钟；
- **Data**：允许传数据（而不只是事件）；
- **Multi Bits**：允许安全传多 bit；
- **Valid (Sampled)**：自带有效信号，能传采样点而不重复/丢失；
- **Ready**：自带反压；
- **Reset Crossing**：内置复位穿越；
- **100% Perf.**：不强制插入空闲周期；
- **No RAM**：不耗 RAM 资源。

#### 4.4.3 源码精读

选择表全文（直接来自原则文档）：

- [doc/base/clock_crossing_principles.md:L60-L69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L60-L69)

| 实体 | 异步时钟 | 数据 | 多 bit | 有效(采样) | 反压 | 复位穿越 | 100%性能 | 无RAM |
| :--- | :--: | :--: | :--: | :--: | :--: | :--: | :--: | :--: |
| olo_base_cc_reset | ✓ | | | | | ✓ | | ✓ |
| olo_base_cc_bits | ✓ | ✓ | | | | | ✓ | ✓ |
| olo_base_cc_pulse | ✓ | | | ✓ | | ✓ | ✓ | ✓ |
| olo_base_cc_simple | ✓ | ✓ | ✓ | ✓ | | ✓ | | ✓ |
| olo_base_cc_status | ✓ | ✓ | ✓ | | | ✓ | | ✓ |
| olo_base_cc_handshake | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| olo_base_fifo_async | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |
| olo_base_cc_n2xn | | ✓ | ✓ | ✓ | ✓ | | ✓ | ✓ |
| olo_base_cc_xn2n | | ✓ | ✓ | ✓ | ✓ | | ✓ | ✓ |

读法示例（本讲关注的两种典型需求）：

- **多 bit、低更新率的状态/配置**：要「数据 + 多 bit + 复位穿越 + 无 RAM」，但**不需要**采样有效（值最终会传过去，不要求精确采样点）。对应 **`olo_base_cc_status`**——它的文档也正写明「main use case is to pass status information or configuration register values」。
- **单周期脉冲**：要「有效(采样) + 复位穿越 + 100% 性能 + 无 RAM」，但**不传数据、不多 bit**。对应 **`olo_base_cc_pulse`**——它保证每个脉冲都被传过去，但脉冲频率必须远低于较慢时钟。

> 注意一个易混点：`cc_status` 的「Valid」列是空的，意思是它**不提供**精确采样点（值会异步地「某时刻」出现在对侧）；而 `cc_simple` 提供有效信号、能传带时序的采样值，代价是不能 100% 性能（需空拍）。这两个的区别正是「状态/配置」与「数据流」的分界。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：依据选择表，为两种需求各选定一个实体，并写出对应的 `set_max_delay` 约束 TCL。

**操作步骤**：

1. **需求 A：跨两个异步时钟域传递一个 16 位配置寄存器，更新很稀疏。**
   - 查表：需要「数据 + 多 bit + 无 RAM + 复位穿越」，不需精确采样 → 选 **`olo_base_cc_status`**。
   - 验证：打开 [src/base/vhdl/olo_base_cc_status.vhd:L10-L15](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_status.vhd#L10-L15)，确认其定位正是「传递状态信息或配置寄存器值」。
   - 写约束（示例代码，非项目原文件；设源时钟 `clk_a`、目的时钟 `clk_b`，较快周期 `Tfast`）：

     ```tcl
     # 示例代码：cc_status 的手动 CDC 约束（双向）
     set_max_delay -from [get_clocks clk_a] -to [get_clocks clk_b] -datapath_only $Tfast
     set_max_delay -from [get_clocks clk_b] -to [get_clocks clk_a] -datapath_only $Tfast
     ```

2. **需求 B：跨两个异步时钟域传递一个单周期事件脉冲，脉冲很稀疏。**
   - 查表：需要「有效(采样) + 100% 性能 + 无 RAM + 复位穿越」，不传数据、不多 bit → 选 **`olo_base_cc_pulse`**。
   - 验证：打开 [src/base/vhdl/olo_base_cc_pulse.vhd:L9-L15](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L9-L15)，确认「脉冲频率必须远低于较慢时钟」「保证全部脉冲被传递」。
   - 写约束（示例代码）：

     ```tcl
     # 示例代码：cc_pulse 的手动 CDC 约束（双向）
     set_max_delay -from [get_clocks clk_a] -to [get_clocks clk_b] -datapath_only $Tfast
     set_max_delay -from [get_clocks clk_b] -to [get_clocks clk_a] -datapath_only $Tfast
     ```

3. 对照模板 [doc/base/clock_crossing_principles.md:L9-L16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L9-L16) 自检：两个方向都写了、带了 `-datapath_only`、周期取较快时钟。

**需要观察的现象**：若你在 Vivado 中用 `cc_status`/`cc_pulse`，会发现它们本身**没有**独立的 scoped `.tcl` 文件——它们的 CDC 路径来自底层 `cc_simple`/`cc_bits`/`cc_reset` 的 scoped 约束自动覆盖（见 4.1.3）。若用非 AMD 工具，则上面这段手动约束就是你必须加的。

**预期结果**：`cc_status` 传配置、`cc_pulse` 传脉冲，时序报告中对应的跨域路径都被 `set_max_delay` 正确覆盖，无建立时间违例。

> 待本地验证：实际工具行为需在本机确认；本讲不代你运行工具。

#### 4.4.5 小练习与答案

**练习 1**：需求是「连续的高速数据流，必须支持反压，可以耗 RAM」，应选哪个实体？
**答案**：`olo_base_fifo_async`。它是表中唯一同时满足「反压 + 100% 性能 + 多 bit + 有效」且**不勾选 No RAM** 的实体——本质是一个异步 FIFO（详见 u3-l1）。

**练习 2**：同样是传数据，`cc_simple` 和 `cc_status` 的核心区别是什么？
**答案**：`cc_simple` 带「有效(采样)」、能传带时序的采样值，但会插入空拍、不能 100% 性能；`cc_status` 不带精确采样点、只保证「值最终会传到对侧」，适合不关心精确时刻的配置/状态。前者是数据流，后者是配置面。

## 5. 综合实践

把本讲四条主线串起来，设计一个最小的「双域外设接口」CDC 子系统：

- **场景**：`clk_core`（核心域）要把一个 16 位的采样率配置写到 `clk_adc`（ADC 采样域），同时要把 ADC 侧偶发的「数据就绪」单周期脉冲报回核心域。两个时钟相互异步。

- **任务**：

  1. **选型**：依据选择表 [doc/base/clock_crossing_principles.md:L60-L69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L60-L69) 选出两个实体——配置寄存器（多 bit、低更新率）用 `olo_base_cc_status`；就绪脉冲（单周期、稀疏）用 `olo_base_cc_pulse`。
  2. **接复位**：两个实体都自带复位穿越。把 `clk_core` 的系统复位接到它们的 `In_RstIn`/`Out_RstIn`，核心域与 ADC 域的周边逻辑分别用各自的 `RstOut` 复位。参照 [doc/base/clock_crossing_principles.md:L38-L53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L38-L53) 说明为什么不能直接用 `RstIn` 复位周边逻辑。
  3. **写约束**：写一组 `clk_core` ↔ `clk_adc` 的 `set_max_delay`（双向、`-datapath_only`、较快周期）。说明在 Vivado 下这步可以由 scoped constraints 自动完成，在其它工具下必须手写。
  4. **复用底层理解**：说明 `cc_status` 与 `cc_pulse` 内部都最终落到 `cc_reset`（复位穿越）与 `cc_bits`（同步链），并指出它们在 [src/base/vhdl/olo_base_cc_status.vhd:L91-L125](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_status.vhd#L91-L125) 与 [src/base/vhdl/olo_base_cc_pulse.vhd:L69-L109](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L69-L109) 的例化位置。

- **验收**：能画出该子系统的方框图（两域、两个 CC 实体、复位与约束标注），并解释每一处选择与连线的依据。仿真层面验证：改一次配置后 ADC 域能看到新值；ADC 拉一次就绪脉冲后核心域收到恰好一个脉冲。

> 待本地验证：完整仿真与综合需在本机工程中确认。

## 6. 本讲小结

- 跨时钟域需要**两件事配套**：同步器电路（降低亚稳态概率）+ 时序约束（告诉工具这是 CDC）；缺一不可。
- 手动约束的模板是一对 `set_max_delay ... -datapath_only <较快周期>`，双向都要写；这是所有非 AMD 工具的标配。
- AMD（Vivado）靠 scoped constraints 自动覆盖全库 CDC；聚合脚本 `olo_base_constraints_amd.tcl` 为每个底层实体加载各自的 `.tcl`。注意仓库实际文件名与原则文档的旧写法略有出入。
- 复位穿越是 CDC 的「隐形刚需」：周围逻辑必须接 `Xxx_RstOut` 而非 `RstIn`，保证两侧同步复位。
- `olo_base_cc_reset` 用「异步置位、同步释放」FF 链 + `olo_base_cc_bits` 回采应答，构成双向复位闭环；两者都靠一摞综合属性保持同步链为独立触发器。
- 选实体靠**选择表**：按「数据/脉冲、多 bit、有效、反压、满吞吐、无 RAM」等维度查表；配置面用 `cc_status`，脉冲用 `cc_pulse`，连续数据流用 `cc_handshake` 或 `fifo_async`。

## 7. 下一步学习建议

本讲建立的是全部 `olo_base_cc_*` 的共性。建议按依赖顺序继续：

1. 先学 **u4-l2 简单跨时钟域（pulse/simple/status）**：本讲只把 `cc_pulse`/`cc_status` 当黑盒用了选型，u4-l2 会进入它们内部，看清 `cc_pulse` 的「边沿翻转发（toggle synchronizer）」、`cc_simple` 的数据锁存、`cc_status` 的握手反馈各自如何保证不丢不重。
2. 再学 **u4-l3 握手与相位对齐跨时钟域**：讲 `cc_handshake` 如何把标准 Valid/Ready 握手跨异步域，以及 `cc_n2xn`/`cc_xn2n` 在**相位对齐**的整数倍时钟间如何省掉异步 FIFO。
3. 若你对异步 FIFO 的指针同步（格雷码）感兴趣，可配合 **u3-l1 异步 FIFO**，它把本讲的 `cc_bits` 同步器用在了读写指针跨越上。
4. 阅读建议：把 [doc/base/clock_crossing_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md) 的选择表打印贴在桌前，遇到任何跨时钟域需求先查表，再读对应实体的源码。
