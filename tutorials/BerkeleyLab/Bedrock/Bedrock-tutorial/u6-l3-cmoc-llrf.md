# cmoc：低电平 RF 控制器（LLRF）

## 1. 本讲目标

本讲是「形式化验证与 RF 系统级设计」单元的第三讲。前面 u6-l2 用 `rtsim` 仿真了一个孤立的 RF 腔体（被控对象），本讲则把**控制器**装上去——讲解 `cmoc`（Cryomodule Controller）子系统如何把 Bedrock 的 DSP 下变频/反馈链路、腔体仿真器与压电/慢通道桥接拼成一个完整的**低电平 RF 控制（Low-Level RF，LLRF）闭环**。

学完本讲你应该能够：

- 说清 LLRF 控制器要解决的本质问题：把腔体电场幅度与相位稳定锁在设定值。
- 理解 `cryomodule.v` 这个顶层如何用 **三个时钟域**（`lb_clk` / `clk1x` / `clk2x`）同时承载「上位机总线」「控制器」「腔体仿真器」，并用 `data_xdomain` 安全搬移 localbus 写侧。
- 顺着 `rf_controller.v` 走完一条完整的 DSP 数据通路：DDS 本振 → 数字下变频 → 反馈核 `fdbk_core` → 输出滤波 → 上变频 DAC，并理解 `7/33` 有理速率。
- 看懂 `slow_bridge.v`（8 位慢通道串行回读）与 `piezo_control.v`（压电控制占位）的设计意图与现状。

本讲的代码实践任务，要求你在 `cryomodule.v` 里定位把 localbus 写侧搬到 `clk1x` 域的 `data_xdomain` 实例，并解释 `.size(32+17)` 这一参数搬移了哪些信号——这是 u4-l1（CDC 基础）知识在本工程中的真实落地。

## 2. 前置知识

本讲为 advanced 难度，假设你已经读过：

- **u3-l2 / u3-l3**：DDS 本振（`rot_dds`、`ph_acc`）、混频、下变频/上变频、IQ 基带、定点截位/饱和。
- **u3-l4**：CIC 抽取、滤波器与速率变换（理解 `7/33` 这种有理分频为何重要）。
- **u4-l1**：时钟域跨越（CDC）基础——`data_xdomain` / `flag_xdomain` / `reg_tech_cdc` 如何把多位数据安全搬到另一时钟域。
- **u4-l4**：Packet Badger（`badger`）如何用 UDP/localbus 把 FPGA 内部寄存器暴露给网络。
- **u6-l2**：`rtsim` 腔体仿真——`cav_mode` / `cav_elec` / `cav_mech` 模拟谐振腔的电气与机械模式。
- **u2-l2 / u2-l3**：localbus 协议与 `newad.py` 寄存器映射自动生成（本讲大量出现 `(* external *)` magic 注释与 `AUTOMATIC_*` 宏）。

几个本讲反复出现的术语，先用一句话对齐：

- **LLRF（Low-Level RF）**：粒子加速器里负责把超导腔（cryomodule cavity）内电磁场幅度/相位稳定到设定点的数字控制系统，区别于管「全局时序与束流轨道」的高电平控制。
- **腔（cavity）/ cryomodule**：一段超导谐振腔，外加其真空/冷却组件；多个腔可串成一台 cryomodule。
- **本振（LO）**：用于把射频信号搬到基带（或反过来）的参考正余弦。
- **IF（中频）**：ADC 直接采到的、未下变频的射频信号频率。
- **压电（piezo）**：用压电陶瓷微调腔体机械形变，从而补偿慢漂移（如 Lorentz 力失谐）。
- **LCLS-2**：SLAC 的直线加速器相干光源二期，cmoc 的标称时钟与 7/33 分频就是为它设计的。

## 3. 本讲源码地图

本讲涉及的关键源码集中在 `cmoc/` 子目录，并牵涉 `dsp/`、`rtsim/`、`badger/`、`localbus/` 中的若干被实例化模块。

| 文件 | 角色 |
| --- | --- |
| [cmoc/cryomodule.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v) | 顶层：把 LLRF 控制器 + 腔体仿真器装进同一模块，跨三个时钟域，地址解码全部自动生成。 |
| [cmoc/llrf_shell.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/llrf_shell.v) | 控制器时钟域（`clk1x`）的「外壳」：在 `rf_controller` 外面再包一层时序发生器、慢回读与 ADC min/max。 |
| [cmoc/rf_controller.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v) | LLRF 控制器主体：DDS→下变频→反馈核→输出滤波→上变频的完整 DSP 通路。 |
| [cmoc/piezo_control.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/piezo_control.v) | 压电控制子模块（当前为占位实现，留有扩展接口）。 |
| [cmoc/slow_bridge.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/slow_bridge.v) | 把 8 位慢速移位寄存器流从 `clk1x` 域桥接到 localbus（`lb_clk`）域。 |
| [cmoc/cryomodule_badger.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule_badger.v) | 把 `cryomodule` 挂到 Packet Badger 的 UDP 端口，演示「网络可达的 RF 控制器」。 |
| [cmoc/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/Makefile) | 构建/仿真入口：`all` / `checks` / `cryomodule_check` / `cryomodule_badger_tb` 等。 |
| [dsp/data_xdomain.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v) | 本讲 CDC 机制的主角：带门控的多位数据跨域拷贝器。 |

> 提示：`cryomodule.v` 还会实例化 `rtsim` 里的 `station` / `beam` / `cav_mech`（腔体仿真器）以及 `dsp` 里的 `circle_buf`、`reg_delay`、`dpram` 等。这些在 u6-l2 与 u3 系列讲义里已讲过，本讲只把它们当作「被 cmoc 装配的积木」引用。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 cryomodule 顶层与三时钟域**、**4.2 rf_controller 反馈核心**、**4.3 piezo_control 压电控制**、**4.4 slow_bridge 慢通道**。第 5 节再给一个把控制器挂到以太网（`cryomodule_badger`）的综合实践。

### 4.1 cryomodule：LLRF 控制器 + 腔体仿真器的一体化顶层

#### 4.1.1 概念说明

`cryomodule` 这个名字容易误导：它**不是**真实腔体，而是一个把「LLRF 控制器」与「腔体仿真器（来自 rtsim）」塞进同一模块的**演示/测试顶层**。文件开头一行注释点明了它的双重身份：

> Combination of LLRF controller and cavity emulation. Portable Verilog, interfaces to a host via an abstract local bus.（[cryomodule.v:14-15](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L14-L15)）

为什么要这么做？因为 LLRF 控制器是否「压得住」腔体，**必须在闭环下**才能验证——单独仿真控制器只看得到「我发出了 drive」，看不到「腔体有没有被稳住」。把控制器和被控腔体放在一起全速仿真，就能用一套 testbench 同时检验「DSP 正确性」「CDC 正确性」「控制环路是否收敛」。这恰好是 u6-l2 `rtsim` 的天然延伸：rtsim 造好了腔体，cmoc 把控制器插上去。

#### 4.1.2 核心流程：三个时钟域的职责划分

cmoc 顶层管理三个时钟域，注释里写得很清楚（[cryomodule.v:17-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L17-L21)）：

| 时钟 | 典型频率 | 归属 | 谁在跑 |
| --- | --- | --- | --- |
| `lb_clk` | 125 MHz | 上位机总线 | localbus 读写、配置 ROM、`circle_buf` 读侧 |
| `clk1x` | 94 MHz（LCLS-2 ADC） | 控制器 | `llrf_shell` → `rf_controller`（DSP 反馈链路） |
| `clk2x` | 2× `clk1x` | 腔体仿真器 | `station` / `beam` / `cav_mech`（rtsim 腔体模型） |

之所以要三个域，是因为这三件事速率不同、来源不同：

- 总线速率由以太网决定，与 ADC 无关。
- 控制器必须跑在 **ADC 采样率** 上（每个 ADC 样本都要处理）。
- 腔体仿真器为了精确模拟比控制器带宽更高的腔体模式，跑在 **2× ADC 速率** 上（`cryomodule_test_setup.py:15` 明确说「Divide by two for the cavity simulator clock time step」）。

由此引出本模块的核心难题：**上位机（`lb_clk`）写下来的控制参数，怎么安全送到 `clk1x` 控制器域和 `clk2x` 仿真器域？** 答案就是两个 `data_xdomain` 实例（详见 4.1.3）。

顶层地址空间也被切成三大块（[cryomodule.v:26-54](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L26-L54)）：

- 写地址 `0x0000–0x3fff` / `0x4000–0x7fff`：分别给 1 号、2 号 LLRF 控制器（`llrf_shell`）。
- 写地址 `0x8000–0xffff`：给仿真器（`vmod1`，即 rtsim 顶层）。
- 读地址 `0x10000–0x17fff`：配置/参数 ROM、慢回读（`slow_bridge`）、循环波形缓冲（`circle_buf`）。

整套地址解码**不是手写**的，而是由 `newad.py` 扫描各模块端口上的 `(* external *)` 与 `(* lb_automatic *)` 属性自动生成——这正是 u2-l3 讲过的方法学在大型工程里的实战。`cryomodule.v` 顶部一连串 `AUTOMATIC_decode/map/beam/cavity/llrf/cav_mech/tgen` 宏（[cryomodule.v:3-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L3-L12)）就是 newad 生成、由 `cryomodule_auto.vh` 注入的连线。

#### 4.1.3 源码精读：用 data_xdomain 把 localbus 写侧搬到 clk1x / clk2x

这是本讲的**核心源码点**，也是代码实践任务的目标。先看顶层端口（[cryomodule.v:57-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L57-L68)）：

```verilog
module cryomodule(
    input clk1x,
    input clk2x,
    input lb_clk,
    input [31:0] lb_data,
    input [16:0] lb_addr,
    input lb_write,  // single-cycle causes a write
    input lb_read,
    output [31:0] lb_out
);
```

注意 `lb_write` 的注释：**单周期脉冲触发一次写**。这正是 u4-l1 讲过、`data_xdomain` 设计所依赖的「门控」前提。然后是两个跨域实例（[cryomodule.v:110-119](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L110-L119)）：

```verilog
`ifndef SIMPLE_DEMO
// Transfer local bus to clk2x domain
data_xdomain #(.size(32+17)) lb_to_2x(
  .clk_in(lb_clk), .gate_in(lb_write), .data_in({lb_addr,lb_data}),
  .clk_out(clk2x), .gate_out(clk2x_write), .data_out({clk2x_addr,clk2x_data}));
// Transfer local bus to clk1x domain
data_xdomain #(.size(32+17)) lb_to_1x(
  .clk_in(lb_clk), .gate_in(lb_write), .data_in({lb_addr,lb_data}),
  .clk_out(clk1x), .gate_out(clk1x_write), .data_out({clk1x_addr,clk1x_data}));
`endif // SIMPLE_DEMO
```

逐行解读：

- `.size(32+17)`：一次跨域拷贝 **49 位** 数据——正好是 `{lb_addr[16:0], lb_data[31:0]}` 拼接后的总宽度。
- `.data_in({lb_addr,lb_data})` / `.data_out({clk1x_addr,clk1x_data})`：把**地址和数据打包成一条总线**一起过域，再在目的端拆开。这样地址与数据天然同拍到达、不会错配。
- `.gate_in(lb_write)`：用写脉冲当「数据有效」标志，跨域后还原成 `clk1x_write`。

`data_xdomain` 内部如何安全搬这 49 位？看 [dsp/data_xdomain.v:14-46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v#L14-L46)：

```verilog
reg [size-1:0] data_latch=0;
always @(posedge clk_in) if (gate_in) data_latch <= data_in;   // ① 锁存

wire gate_x;
flag_xdomain foo(                                               // ② 单比特门控跨域
    .clk1(clk_in),  .flagin_clk1(gate_in),
    .clk2(clk_out), .flagout_clk2(gate_x));

reg [size-1:0] data_out_r=0;
always @(posedge clk_out) begin
   if (gate_x) data_out_r <= data_pipe;                         // ③ 目的域采数
   ...
end
```

三步法（来自 u4-l1）：

1. **`clk_in` 域锁存**：`gate_in`（即 `lb_write`）拉高的那一拍，把 49 位数据锁进 `data_latch`，此后数据**稳定不动**。
2. **单比特门控跨域**：`flag_xdomain` 用「翻转 + 两级同步 + 异或检测」把单比特脉冲安全送到 `clk_out` 域（[flag_xdomain.v:9-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v#L9-L21)），这是唯一真正「跨异步域」的信号。
3. **`clk_out` 域采数**：检测到 `gate_x` 后，把（已经稳定很久的）`data_latch` 采进 `data_out_r`。

为什么这样安全？因为**多位数据被门控冻结**，跨域的只有 1 比特；只要 `clk_out` 比 `gate_in` 的速率快一倍以上（`data_xdomain.v:1` 的约束），就不会漏采，也不会采到「数据正在变化」的中间态。

**为什么写侧简单、读侧难？** 写是「推」：上位机主动发起、稀疏、单拍，`gate_in` 天然满足「不连续拉高」的约束（`flag_xdomain` 仿真里还会警告 `gate_in=1'b1` 被滥用，见 [flag_xdomain.v:23-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v#L23-L35)）。读是「拉」：要求数据在固定周期内返回，牵涉往返握手——这正是 u4-l2（`mem_gateway` 固定延迟读）和 u4-l3（`jit_rad` 跨域即时读回）要解决的难题，cmoc 在写侧回避了它。

#### 4.1.4 代码实践：定位并解读 lb_to_1x 实例

**实践目标**：亲手把 u4-l1 的 `data_xdomain` 理论对应到本工程的真实实例，确认「搬了什么、搬了多少位、为什么安全」。

**操作步骤**：

1. 运行构建与自检（详见第 5 节，至少先确认环境能跑）：
   ```bash
   make -C cmoc all checks
   ```
2. 打开 [cmoc/cryomodule.v:110-119](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L110-L119)，找到 `lb_to_1x` 与 `lb_to_2x` 两个 `data_xdomain` 实例。
3. 对照 [dsp/data_xdomain.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/data_xdomain.v)，画出：`lb_write → data_latch 锁存 → flag_xdomain 翻转同步 → clk1x 域采到 clk1x_write` 的时序链。

**需要观察的现象 / 预期结果**：

- `.size(32+17)` = **49**，对应 `{lb_addr[16:0](17 位), lb_data[31:0](32 位)}`，即**把地址和数据一起过域**。
- 两个实例的 `data_in` 完全相同（都用 `{lb_addr,lb_data}`），区别只在 `clk_out`：一个去 `clk1x`（控制器），一个去 `clk2x`（仿真器）。这意味着同一次 localbus 写，**控制器和仿真器各自在自己的时钟域收到一份**，由各自的地址解码决定是否认领。
- `clk1x_write` 在 `clk1x` 域里是「偶尔出现的单拍脉冲」，频率远低于 `clk1x`，满足 `data_xdomain` 的「clk_out 至少 2× gate_in 速率」约束。

如果无法本地运行 iverilog，**待本地验证**：可只做源码阅读部分（步骤 2、3），不依赖仿真。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `.size(32+17)` 改成 `.size(32)`，会丢失什么功能？
> **答**：地址 `lb_addr` 不会被跨域，目的域只拿到 `lb_data` 却不知道往哪个地址写，控制器无法区分「写 phase_step」还是「写 modulo」等不同寄存器。

**练习 2**：为什么 `cryomodule` 需要**两份** `data_xdomain`（`lb_to_1x` 和 `lb_to_2x`），而不是把数据先搬到 `clk1x` 再从 `clk1x` 搬到 `clk2x`？
> **答**：因为 `clk1x` 与 `clk2x` 虽然是 2× 关系，但相位不保证对齐（仿真里还专门有「clock phasing hack」处理 `iq` 的 2× 关系，见 4.2.4）。从 `lb_clk` 各自直接搬一份，避免级联 CDC 累积延迟与亚稳态风险，也让仿真器与控制器互不阻塞。

**练习 3**：`SIMPLE_DEMO` 宏（[cryomodule.v:110](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L110) 与 [:344-353](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L344-L353)）打开后会发生什么？
> **答**：跳过两个 `data_xdomain`、跳过仿真器与控制器实例化，用一组 `assign` 给出常量驱动（`drive=0`、`iq` 来自分频等），目的是得到一个「5 分钟就能综合完的精简 bitfile」，用于流程联调而非功能验证。注释 `// Used to get a 5-minute bitfile build`（[:56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L56)）点明了这一点。

---

### 4.2 rf_controller：LLRF 反馈控制核心

#### 4.2.1 概念说明

`rf_controller.v` 才是真正「做 RF 控制」的模块。它的使命用一句话概括：**测出腔体电场的复数幅度，与设定值比较，算出驱动修正量，喂回给腔体。** 这是一个经典的反馈环（feedback loop），离散化后可写成：

\[
u[n] = K_p\,(r[n] - y[n]) \;+\; \text{(历史项/IIR/前馈)}
\]

其中 \(y[n]\) 是测量到的腔体场（复数 IQ），\(r[n]\) 是设定值，\(u[n]\) 是输出驱动。`rf_controller` 把这条公式拆成一条 DSP 流水线，每个环节都对应一个 `dsp/` 子模块。

注释里 Larry Doolittle 留了一句诚实的免责声明（[rf_controller.v:3-5](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L3-L5)）：`// XXX Still under construction`，提示读者部分功能（尤其 piezo）尚未完工。

#### 4.2.2 核心流程：一条 DSP 数据通路

顺着数据流向，`rf_controller` 的内部链路如下（端口见 [rf_controller.v:14-71](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L14-L71)）：

```
ADC(a_field) ── fwashout ── fdownconvert ──┐
                                            ├─ fdbk_in ─ fdbk_core ─ fdbk_out_xy ─ lp_notch ─ drive ─ second_if_out ─ DAC
fiber(iq_recv) ─────────────────────────────┘                                              ↑
DDS(rot_dds) ── cosa,sina (LO) ──────────────────────────────────────────────────────────┘
                                                                                          (LO 也喂 fdownconvert)
另: cim_12x(多通道混频) ─ ccfilt ─ fchan_subset ─ mon_result (波形监测)
    cim_12x ─ piezo_control ─ piezo_ctl
```

把它拆成几个关键阶段：

1. **本振 DDS**：`rot_dds` 按 `phase_step` + `modulo` 产生 cos/sin 本振（[rf_controller.v:82-96](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L82-L96)），这是 u3-l2 讲过的有理频率合成，用于 LCLS-2 的 `7/33` IF。
2. **下变频**：`fwashout`（洗去直流/慢漂）+ `fdownconvert`（IF→基带 IQ），见 [rf_controller.v:181-193](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L181-L193)。
3. **反馈核**：`fdbk_core` 拿到复数误差 `fdbk_in`，算出复数驱动 `fdbk_out_xy`（[rf_controller.v:195-211](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L195-L211)）。
4. **输出滤波**：`lp_notch`（低通+陷波）整形驱动（[rf_controller.v:213-221](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L213-L221)）。
5. **上变频**：`second_if_out` 把基带驱动搬回 IF、产出 DAC 对（[rf_controller.v:223-230](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L223-L230)）。
6. **并行监测/压电支路**：`cim_12x` 多通道混频 → `ccfilt`/`fchan_subset` 出波形监测 `mon_result`；同一混频结果还喂 `piezo_control`。

#### 4.2.3 源码精读：7/33 有理速率与 DDS

LCLS-2 的中频频率是 `clk × 7/33`——不是整数分频，而是有理分频。`rf_controller` 用一个 33 拍的状态机来产生 CIC 时序（[rf_controller.v:128-137](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L128-L137)）：

```verilog
parameter cic_base_period = 33;  // nominal LCLS-II IF = clk * 7/33
...
always @(posedge clk) begin
   cic_state <= cic_state==(cic_base_period-1) ? 0 : cic_state+1;
   cic_sample <= cic_state==0;
   if (cic_sample) wave_cnt <= wave_cnt==1 ? wave_samp_per : wave_cnt-1;
end
```

每 33 个 `clk` 周期产生一个 `cic_sample` 脉冲，对应该有理频率的一个有效样本。这与 u3-l2 `ph_acc` 用「粗计数器 + 细残差 + 可编程模数」合成任意有理频率是同一思想，只是这里用在 CIC 降采样节拍上。

DDS 的幅度常数也精心算过（[rf_controller.v:86-90](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L86-L90)）：

```verilog
// floor(2^17*(32/33)^2/1.646760258-3) = 74840
parameter [17:0] lo_amp = 74694;
```

这里 `1.646760258` 正是 u3-l1 讲过的 **CORDIC 固有增益**（`rot_dds` 内部用 CORDIC 旋转模式产生 cos/sin）。`lo_amp` 故意比理论值小一点，留出余量避免满量程溢出——这是定点 DSP 的典型权衡。

#### 4.2.4 源码精读：反馈核与上变频

反馈核的输入源选择体现了「本地 ADC」与「光纤远端 IQ」的切换（[rf_controller.v:196-211](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L196-L211)）：

```verilog
// Select input source: local ADC or remote over fiber
wire signed [17:0] fdbk_in = use_fiber_iq[0] ? {iq_recv,1'b0} : {field_xy,2'b0};
...
(* lb_automatic *)
fdbk_core fdbk_core  // auto
    (.clk(clk),
    .sync(sync), .iq(iq), .in_xy(fdbk_in), .out_xy(fdbk_out_xy), ...);
```

`use_fiber_iq[0]` 决定误差信号来自本地下变频（`field_xy`）还是光纤链路（`iq_recv`）。`fdbk_core`、`lp_notch`、`piezo_control` 都标了 `(* lb_automatic *)`，意味着它们的内部寄存器由 newad 自动展开进 regmap，上位机可按名字访问。

输出端，`drive` 取自 `lp_notch` 输出的高位（[rf_controller.v:221](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L221)）：`assign drive = drive_w[19:2];`——典型的「右移截位」换取满量程，正是 u3-l2 讲过的定点截位。

还有一个值得注意的「clock phasing hack」（[cryomodule.v:309-342](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L309-L342)）：控制器跑在 `clk1x`，腔体仿真器跑在 `clk2x`，`iq` 标志与 `drive` 要从 `clk1x` 域安全交给 `clk2x` 域。代码用 `clk1x_div2` 的两级同步 + 异或检测来对齐相位，注释里坦承这「Passes testbench, but still ugly and possibly fragile」（[:314-315](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L314-L315)）。这是 `clk1x`/`clk2x` 同源 2× 关系下的一种实用技巧，但**不是**通用 CDC 方案——通用多位跨域仍走 `data_xdomain`。

#### 4.2.5 代码实践：跑 rf_controller 测试台

**实践目标**：验证 `rf_controller` 能独立仿真（不依赖整个 cryomodule），并理解测试台如何注入激励。

**操作步骤**：

1. 编译并仿真：
   ```bash
   make -C cmoc rf_controller_tb
   ```
   注意 [Makefile:21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/Makefile#L21) 把 `rf_controller_check` 列入 `NO_CHECK`，因为该 testbench 自述「Non-checking testbench. Will always PASS」（[rf_controller_tb.v:44-49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller_tb.v#L44-L49)），它只跑波形、不判数值。
2. 阅读 [rf_controller_tb.v:93-107](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller_tb.v#L93-L107)：测试台通过直接赋值 `dut_phase_step = 222425*4096+868`、`dut_modulo = 4` 配置 7/33 本振，并在 `#6000` 后把 `dut_use_fiber_iq` 从 1 切到 0，模拟「先用光纤 IQ、再切本地 ADC」。
3. 想看波形：`make -C cmoc rf_controller.vcd` 后用 gtkwave 打开 `rf_controller.gtkw` 预置信号组。

**需要观察的现象**：`mon_result` 在 `mon_strobe` 拍上更新、`mon_boundary` 标记一帧边界；切换 `use_fiber_iq` 后，反馈输入源改变但环路仍稳定运行。

**预期结果**：仿真打印 `PASS`（非校验型，仅表示跑完无 `$finish(1)`）。若本机无 iverilog，**待本地验证**。

#### 4.2.6 小练习与答案

**练习 1**：`fdbk_in` 的两个来源 `{iq_recv,1'b0}` 与 `{field_xy,2'b0}` 为什么都在低位补 0？
> **答**：左移 1 或 2 位等于乘 2 或 4，是把下变频/光纤送来的定点数**对齐到反馈核期望的满量程刻度**，同时不引入舍入误差（纯移位）。这是定点 DSP 里常见的「刻度对齐」手法。

**练习 2**：`drive = drive_w[19:2]` 丢掉了低 2 位，为什么不直接用 `drive_w`？
> **答**：`drive_w` 是 20 位，而下游 DAC 通路期望 18 位。取高 18 位是定点截位（保留符号位），用低位精度换满量程动态范围，对应 u3-l2 讲的「截位不改位宽、调刻度」思想。

**练习 3**：为什么 `lo_amp` 注释里出现 `(32/33)^2` 这个因子？
> **答**：因为下游的 `fdownconvert` / 上变频链路内部有 `(33/32)` 相关的刻度变换（见 [rf_controller.v:107-113](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L107-L113) 的 `cosal = cosa + cosa>>>4`，即乘 17/16≈1.0625）。预先在 LO 幅度上除掉这个增益，保证整条链路净增益接近 1，避免级联放大导致饱和。

---

### 4.3 piezo_control：压电控制（占位实现）

#### 4.3.1 概念说明

超导腔在加电时会因 Lorentz 力发生微小形变，导致谐振频率漂移（Lorentz detuning）。**压电（piezo）执行器**贴在腔壁上，施加反向机械力补偿这种漂移。LLRF 系统里通常有一条比 RF 反馈慢得多的「压电环」，专门追这种秒级/毫秒级的慢漂移。

在 Bedrock 里，`piezo_control` 是这条慢环的**预定接口**，但目前是占位实现。注释直说（[piezo_control.v:28-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/piezo_control.v#L28-L31)）：

> Non-zero placeholder. Eventually intend to use reg_mac2 or similar; that code takes 2K address space...

这说明作者保留了**寄存器映射与数据通路接口**（输入来自 `cim_12x` 的多通道混频结果 `sr_in`，输出 `piezo_ctl`），但真正的控制算法（计划用 `reg_mac2` 这类乘加阵列实现 LQG/状态反馈）尚未填入。

#### 4.3.2 核心流程

即便占位，接口已经为「未来的控制器」预留好：

```
sr_in[35:0] (cim_12x 多通道混频) ──►  (未来的 reg_mac2 / lqg_loop1)
                                          │
host 寄存器: piezo_dc, sf_consts, trace_en ─┘
                                          ▼
                                     piezo_ctl[17:0] → 压电 DAC
                                     piezo_stb / sat_count / trace_out (监测)
```

#### 4.3.3 源码精读

整个模块当前就是把上位机写的 `piezo_dc` 直接送出，再补几个零（[piezo_control.v:33-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/piezo_control.v#L33-L39)）：

```verilog
assign piezo_stb = 1;
assign piezo_ctl = {piezo_dc, 2'b0};   // 左移 2 位 = ×4 刻度
assign sat_count = 0;
assign trace_out = 0;
...
```

几个细节值得品味：

- `piezo_ctl = {piezo_dc, 2'b0}`：又是定点左移刻度对齐（与 4.2.6 练习 1 同理），把 16 位寄存器值放到 18 位 DAC 刻度上。
- 端口上挂着一串 `(* external *)`（[piezo_control.v:14-23](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/piezo_control.v#L14-L23)）：`piezo_dc`、`sf_consts`、`sf_consts_addr`、`trace_en`、`trace_en_addr`。这些都被 newad 自动分配地址、写进 `regmap_cryomodule.json`，上位机能直接按名字读写——**接口先于实现**，这是大型 FPGA 工程常见的演进方式。
- 在 `rf_controller` 里实例化时（[rf_controller.v:169-178](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L169-L178)），`piezo_stb` 被复用为 `trace_boundary`、`sat_count` 也接出，说明这些监测口即使算法未实现也已在监测链路里就位。

#### 4.3.4 代码实践：阅读占位接口并规划替换

**实践目标**：理解「占位模块 + 已就位 regmap」的工程模式，为将来填入真实算法做准备。

**操作步骤**：

1. 打开 [cmoc/piezo_control.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/piezo_control.v)，列出它对外承诺的全部信号（输入/输出/external 寄存器）。
2. 生成 regmap 后查看 piezo 相关寄存器：
   ```bash
   make -C cmoc cryomodule_auto    # 触发 newad 生成 _autogen/regmap_cryomodule.json
   grep -i piezo cmoc/_autogen/regmap_cryomodule.json
   ```
3. 对照 [cmoc/rules.mk:13-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rules.mk#L13-L17) 看 `piezo_control_auto` 如何被 `rf_controller_auto`、`cryomodule_auto` 依赖。

**需要观察的现象**：即便 `piezo_control` 内部全是 `assign = 0`，它的 `external` 端口仍在 regmap JSON 里占到了地址——上位机软件已经能写 `piezo_dc`，将来算法填入后**无需改地址表**即可生效。

**预期结果**：能看到 `piezo_dc`、`sf_consts`、`trace_en` 等条目及其地址。若未跑 newad，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：既然 `piezo_control` 现在只是把 `piezo_dc` 送出去，为什么不直接在 `rf_controller` 里写 `assign piezo_ctl = {piezo_dc,2'b0}`，而要单独建个模块？
> **答**：为了**接口稳定**。把这个占位独立成模块、并预定好 `sr_in`（混频输入）和一系列 external 寄存器，将来用 `reg_mac2`/LQG 替换时，只需改模块内部、不动 `rf_controller` 的实例化与 regmap——这是「先定接口、后填实现」的解耦设计。

**练习 2**：`piezo_stb = 1`（恒高）在下游意味着什么？
> **答**：`piezo_stb` 被接到 `rf_controller` 的 `trace_boundary`（[rf_controller.v:174](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/rf_controller.v#L174)）。占位阶段恒高表示「每拍都是边界」，等真实算法实现后再给出有意义的选通节拍。

---

### 4.4 slow_bridge：慢通道串行回读

#### 4.4.1 概念说明

RF 控制器有「快」和「慢」两类回读需求：

- **快**：波形采集（每个样本都要存），走 `circle_buf` 大容量循环缓冲（[cryomodule.v:216-224](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L216-L224)）。
- **慢**：ADC 的逐帧 min/max、时间戳、tag 标签等低频状态量，不需要每拍存，但要周期性地、低引脚数地送回 localbus。

`slow_bridge` 就是慢通道的**跨域搬运工**：把控制器域（`slow_clk = clk1x`）里一条 **8 位宽**的串行移位寄存器流，桥接到 localbus 域（`lb_clk`）供上位机读。注释说得很直白（[slow_bridge.v:10-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/slow_bridge.v#L10-L13)）：「Keep data path narrow for routing reasons, not logic element count」——选 8 位宽是为了布线资源，不是逻辑资源。

#### 4.4.2 核心流程

`slow_bridge` 用一块小双口 RAM（`dpram`）做「快照 → 逐字节读出」的中转：

```
clk1x 域:  slow_snap 脉冲 ──► 把控制器里的慢移位寄存器(ADC min/max, timestamp, tag...)
                             一拍 8 字节地写进 dpram (write_addr 自增)
                                          │ (双口 RAM 天然跨域)
lb_clk 域:  lb_read ──► 按 lb_addr[8:0] 直接读 dpram 同一地址 ──► lb_out[7:0]
```

`invalid` 信号在「正在搬移」期间拉高，提示上位机此时数据可能不完整。

#### 4.4.3 源码精读

核心是那块 512×8 的双口 RAM（[slow_bridge.v:18-33](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/slow_bridge.v#L18-L33)）：

```verilog
reg running=0, shifting=0;
reg [8:0] write_addr=0;
always @(posedge slow_clk) begin
    if (slow_snap | &write_addr) running <= slow_snap;   // 快照触发一次搬运
    if (running) write_addr <= write_addr+1;              // 顺序写满 512 字节
    shifting <= running & |write_addr[8:4];
end

dpram #(.aw(9), .dw(8)) ram(.clka(slow_clk), .clkb(lb_clk),
    .addra(write_addr), .dina(slow_out), .wena(running),
    .addrb(lb_addr[8:0]), .doutb(ram_out));
assign lb_out = ram_out;
assign slow_op = slow_snap | shifting;
assign invalid = running;
```

要点：

- **写侧在 `slow_clk`（= clk1x）**：`slow_snap` 一来，`running` 置位，`write_addr` 从 0 自增，把控制器送来的 8 位 `slow_out` 顺序灌进 RAM。
- **读侧在 `lb_clk`**：上位机用 `lb_addr[8:0]` 直接寻址同一块 RAM，**无需任何握手**——因为双口 RAM 的两个端口各自同步，地址稳定一拍就能读到。
- `running` 期间 `invalid=1`：上位机应避开此时读，保证拿到完整的一帧。

这条慢链路里**真正送的是什么数据**？看 `llrf_shell.v` 的组装（[llrf_shell.v:108-115](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/llrf_shell.v#L108-L115)）：ADC 三路（field/forward/reflect）的 min/max、当前 tag、上一帧 tag_old，以及 `timestamp` 模块送来的时间戳——都是诊断腔体健康必不可少的低频量。`cryomodule.v` 还把自己那份 `circle_count`/`circle_stat` 拼进同一移位寄存器（[cryomodule.v:242-250](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L242-L250)）。

#### 4.4.4 代码实践：跟踪一次慢回读的数据来源

**实践目标**：把「localbus 读 `0x2000` 区」与「控制器里某段 min/max 计算」连成一条完整因果链。

**操作步骤**：

1. 在 [cmoc/cryomodule.v:233-238](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L233-L238) 找到 `slow_bridge` 实例，确认 `slow_clk=clk1x`、`lb_clk` 来自顶层。
2. 跳到 [cmoc/llrf_shell.v:63-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/llrf_shell.v#L63-L70)，看 `minmax` 模块如何对 `a_field` 求 min/max，并在 `mm_snap=slow_snap` 时复位。
3. 顺着 [llrf_shell.v:108-115](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/llrf_shell.v#L108-L115) 看这些 min/max 如何被打包进 `slow_sr_data`，最终经 `slow_out[7:0]` 送给 `slow_bridge`。

**需要观察的现象**：一次 `slow_snap` 触发后，`minmax` 复位开始统计新一帧，同时旧一帧的统计值正被 `slow_bridge` 逐字节搬到 lb_clk 域——读和统计**并行不冲突**。

**预期结果**：你能画出 `a_field → minmax → slow_sr_data → slow_bridge.dpram → lb_out` 的完整通路。这是纯源码阅读型实践，不依赖仿真。

#### 4.4.5 小练习与答案

**练习 1**：`slow_bridge` 为什么用双口 RAM，而不是像 localbus 写侧那样用 `data_xdomain`？
> **答**：因为这是**读侧**——上位机要按任意地址、任意时刻读，且要读到「一整帧」多个字节。`data_xdomain` 只适合「稀疏单拍事件」跨域；双口 RAM 的两个端口各自同步、可独立寻址，天然适合「一边顺序写、另一边随机读」的场景，无需握手。

**练习 2**：`write_addr` 是 9 位（512 深度），但 `slow_sr_data` 在 `llrf_shell` 里只有 `7*16=112` 位（14 字节）。多出来的空间作何用？
> **答**：`cryomodule.v` 自己也往这条移位寄存器拼接数据（`circle_count`/`circle_stat` 等，见 [:242](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L242)），加上 `timestamp` 每帧持续追加，512 字节是为「多腔、多状态量」预留的余量。具体填多少由 `slow_larger.list` 决定（见 [cryomodule.v:42-45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L42-L45) 注释）。

---

## 5. 综合实践：把控制器挂到以太网（cryomodule_badger）

本讲四个最小模块讲完后，做一个把它们与 u4-l4（Packet Badger）串起来的综合任务：理解 `cryomodule_badger` 如何让一个 RF 控制器变成**网络可达**的设备。

**任务**：阅读 [cmoc/cryomodule_badger.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule_badger.v)，画出「PC 发一个 UDP 包 → 改写某个 localbus 寄存器 → 影响控制器行为」的完整数据通路。

**步骤**：

1. 看 `rtefi_blob badger` 实例（[cryomodule_badger.v:35-67](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule_badger.v#L35-L67)）：它解析以太网帧，把 UDP 端口 3（`p3_*`）的 localbus 请求还原成 `rtefi_lb_addr/data/control_strobe`。
2. 看写/读选通逻辑（[cryomodule_badger.v:70-71](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule_badger.v#L70-L71)）：
   ```verilog
   wire lb_write = rtefi_lb_control_strobe & ~rtefi_lb_control_rd & ~rtefi_lb_addr[17];
   wire lb_read  = rtefi_lb_control_strobe &  rtefi_lb_control_rd & ~rtefi_lb_addr[17];
   ```
   `~rtefi_lb_addr[17]` 保证只把低 16 位地址区（控制器区，对应 4.1.2 的地址表）交给 `cryomodule`。
3. 看 `cryomodule` 实例（[cryomodule_badger.v:72-86](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule_badger.v#L72-L86)）：`lb_clk = gmii_tx_clk`（以太网发时钟），正是 4.1 里的 `lb_clk` 域。
4. 串起整条链：
   ```
   PC UDP 包 → GMII(rx) → rtefi_blob 解码 → rtefi_lb_addr/data
        → cryomodule.lb_addr/lb_data/lb_write (lb_clk 域)
        → data_xdomain(lb_to_1x) → clk1x 域地址解码 → rf_controller 的 phase_step 等寄存器
        → DDS 频率改变 → 控制器行为改变
   ```
5. 运行 badger 测试台（需要 VPI，可能较重）：
   ```bash
   make -C cmoc cryomodule_badger_tb
   ```
   注意 [Makefile:21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/Makefile#L21) 也把 `cryomodule_badger_check` 列入 `NO_CHECK`，它依赖 `tap-vpi.vpi`（虚拟以太网 tap），主要验证「包能进去、数据能回来」。

**预期结果**：你能解释为什么这次写侧跨域依然只用 `data_xdomain`（写是推、稀疏），而读回腔体波形则走 `circle_buf` 的双口 RAM + 固定延迟（u4-l2/u4-l3 思路）。若 `tap-vpi` 编译失败或无 iverilog，**待本地验证**，可只完成步骤 1–4 的源码阅读。

## 6. 本讲小结

- `cmoc` 的顶层 `cryomodule.v` 是**控制器 + 腔体仿真器**的一体化演示，管理 `lb_clk` / `clk1x` / `clk2x` 三个时钟域，地址解码与连线几乎全部由 newad（`AUTOMATIC_*` 宏）自动生成。
- localbus 写侧用两个 `data_xdomain #(.size(32+17))` 实例（`lb_to_1x` / `lb_to_2x`）把 `{lb_addr, lb_data}` 共 49 位安全搬到控制器域与仿真器域——这是 u4-l1 CDC 理论在工程里的落地：多位数据靠门控冻结、只让单比特 `flag_xdomain` 真正跨异步域。
- `rf_controller.v` 是真正的 LLRF 反馈核心：DDS（`rot_dds`，7/33 有理速率）→ 下变频（`fdownconvert`）→ 反馈核（`fdbk_core`）→ 输出滤波（`lp_notch`）→ 上变频（`second_if_out`），定点刻度处处精心对齐（`lo_amp` 抵消 CORDIC 增益、截位换满量程）。
- `piezo_control.v` 是「接口先于实现」的范例：当前只把 `piezo_dc` 直送，但 external 寄存器与数据通路已就位，将来填入 `reg_mac2`/LQG 不影响 regmap。
- `slow_bridge.v` 用 512×8 双口 RAM 把 `clk1x` 域的 8 位串行慢状态（ADC min/max、时间戳、tag）桥接到 localbus 域，体现「读侧跨域用双口 RAM 而非 `data_xdomain`」的取舍。
- LLRF 控制的本质是一个复数反馈环 \(u[n]=K(r[n]-y[n])\)，Bedrock 把它拆成可复用的 `dsp/` 积木，再用 cmoc 把积木、腔体（rtsim）与网络（badger）装配成可仿真、可上板的完整系统。

## 7. 下一步学习建议

- **u6-l4 digaree**：digaree 是另一个更大型的 DSP 应用工程，同样用 Python 代码生成（`cgen_*.py`、`pfloat.py`）。学完 cmoc 后看 digaree，能对比「同一套 newad + dsp 积木」如何支撑不同规模的工程。
- **回到 u4-l2 / u4-l3**：如果你对 5. 综合实践中「为什么读侧不能直接用 data_xdomain」还意犹未尽，去读 `mem_gateway`（固定延迟读）与 `jit_rad`（跨域即时读回），它们给出了 cmoc 在读侧回避掉的那些难题的标准答案。
- **u7 系列（SoC 与平台工程）**：cmoc 是「子系统级」综合，u7-l4（`projects/` 工程集成）会把它这类模块连同板级支持、外设驱动一起装进一块真实板卡（如 Marble）的 `marble_top.v`，完成从子系统到完整产品的最后一步。
- **建议继续精读的源码**：`cmoc/llrf_shell.v`（控制器外壳、慢回读组装）、`dsp/fdownconvert.v`（与 u3-l3 联动）、`rtsim/station.v`（腔体仿真器如何被 cmoc 实例化），以及运行 `make -C cmoc cryomodule_check` 后阅读 `verify_cryomodule.py`，看它如何用 numpy 验证闭环仿真下腔体场幅度收敛到期望值（约 1934，误差 ±5）。
