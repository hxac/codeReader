# 选通与节拍生成

## 1. 本讲目标

学完本讲后，你应该能够：

- 说明「选通（strobe）」在数字电路里的含义，以及为什么 FPGA 系统到处需要它。
- 区分 psi_common 提供的四种节拍生成方式：按**频率**生成的 `strobe_generator`、按**周期数**且运行时可配置的 `strobe_generator_cfg`、固定时间基准（us/ms/sec）的 `tickgenerator`、以及对已有选通做**整数分频**的 `strobe_divider`。
- 读懂这四个组件的源码，理解它们各自如何用计数器实现节拍。
- 针对一个具体场景（如「100 MHz 时钟下产生 1 kHz，再分频 10 倍」）选出正确组件并写出实例化代码。

本讲是 u6 单元「时序与节拍生成」的第一篇，承接 u2-l1 的 `math_pkg`（这里会用到 `from_uslv`，以及频率比的概念），并为后续 u6-l2（时钟测量）、u8（TDM 转换）、u9（SPI/I2C）等大量「靠选通驱动」的组件打基础。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 什么是选通（strobe）

在 FPGA 里，几乎所有功能模块都跑在同一个连续时钟 `clk` 上。但很多操作并不需要**每个**时钟周期都做一次——例如「每 1 毫秒采一次样」「每收到一个 1 kHz 脉冲就搬运一帧数据」。这时我们需要一个**单周期宽**的脉冲信号来「点名」：脉冲拉高的那一拍，下游模块才真正动作。这个单周期脉冲就叫**选通（strobe）**，本库里输出端口通常命名为 `vld_o` 或 `xxx_o`。

可以把它想成教室里的「响铃」：铃响一次（一拍），全班同学同时开始/结束一项动作；铃不响时就保持空闲。

### 2.2 频率、周期与计数比

选通的本质是一个「分频计数器」。设时钟频率为 \(f_{clk}\)、想要的选通频率为 \(f_{strobe}\)，那么两次选通之间相隔的时钟周期数（计数比）为：

\[
ratio = \left\lceil \frac{f_{clk}}{f_{strobe}} \right\rceil
\]

向上取整是因为计数器只能数整数个周期；这会带来**小于一个时钟周期**的频率误差，后续测试平台正是据此校验（误差 \(<\) 一个时钟周期即合格）。

> 注意：`math_pkg` 提供了一个现成的 [`ratio`](hdl/psi_common_math_pkg.vhd) 函数（见 u2-l1），但 `strobe_generator` 并没有调用它，而是直接在源码里写 `integer(ceil(freq_clock_g / freq_strobe_g))`。本讲会点出这一区别。

### 2.3 边沿检测（edge detection）

很多节拍相关组件都要回答一个问题：「这个输入是不是**刚从 0 变成 1**？」做法是把输入打一拍寄存（记为 `xxxLast`），然后比较：

```
上升沿 = (输入 = '1') and (上一拍的输入 = '0')
```

本讲的四个组件里有三个用到了这一手法（`syncLast`、`str_dff_s`），请记住这个套路。

## 3. 本讲源码地图

本讲涉及的真实源码文件如下：

| 文件 | 作用 | 有无测试平台 |
|:--|:--|:--|
| [hdl/psi_common_strobe_generator.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd) | 按**频率**生成单周期选通（周期在综合时固定） | 有 |
| [hdl/psi_common_strobe_generator_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator_cfg.vhd) | 按**周期数**生成选通，周期由端口 `count_i` 运行时配置 | **无（HEAD 新增，待补 TB）** |
| [hdl/psi_common_tickgenerator.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd) | 生成 1 us / 1 ms / 1 sec 三种固定时间基准节拍 | 有 |
| [hdl/psi_common_strobe_divider.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_divider.vhd) | 对**已有选通**做整数分频（每 N 个选通放行一个） | 有 |
| [hdl/psi_common_math_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd) | 提供 `from_uslv` 等转换函数（被 `strobe_generator_cfg` 调用） | 有（包级 TB） |

四者关系一句话概括：

- `strobe_generator` / `strobe_generator_cfg` 是**源头**——它们凭空产生一个选通；
- `strobe_divider` 是**后处理**——它消费一个已有选通、把它变慢；
- `tickgenerator` 是**专用**——只产生 us/ms/sec 三种「墙上时间」节拍，接口与前三者不同。

---

## 4. 核心概念与源码讲解

### 4.1 strobe_generator：频率法（编译期固定周期）

#### 4.1.1 概念说明

这是库中最经典的选通发生器：你告诉它「时钟多快」「想要多快的选通」，它就在综合时算出一个固定的计数比，运行时按这个比不断输出单周期脉冲。适合**频率一旦确定就不再变化**的场景（绝大多数 FPGA 设计都是如此）。

它的实体非常精简，只有三个 generic、四个端口（见 [hdl/psi_common_strobe_generator.vhd:18-26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L18-L26)）：

- `freq_clock_g`（real，单位 Hz）：时钟频率。
- `freq_strobe_g`（real，单位 Hz）：期望的选通频率。
- `rst_pol_g`（默认 `'1'`）：复位极性。
- `sync_i`（可选，默认 `'0'`）：同步输入，可用于「重新对齐」选通相位。

#### 4.1.2 核心流程

整个组件就一个时钟进程，逻辑可概括为：

```text
复位 → count=0, vld_o=0
每个上升沿：
  若 (count 数到 ratio_c-1) 或 (检测到 sync_i 上升沿):
      vld_o <= '1'      # 这一拍输出选通
      count <= 0        # 重新开始计数
  否则:
      vld_o <= '0'
      count <= count+1
```

其中计数上限 `ratio_c` 是一个**编译期常量**：

\[
ratio\_c = \left\lceil \frac{freq\_clock\_g}{freq\_strobe\_g} \right\rceil
\]

`sync_i` 的作用是「重新对齐相位」：当 `sync_i` 出现上升沿时，立刻强制产生一个选通并把计数清零，之后又回到正常的按频率自激振荡。文档里用一张 1/4 时钟频率的波形图说明了这一点（见 [doc/files/psi_common_strobe_generator.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_strobe_generator.md)）。

#### 4.1.3 源码精读

计数比常量与计数信号声明（[hdl/psi_common_strobe_generator.vhd:30-32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L30-L32)）：`ratio_c` 用 `ceil` 向上取整，`count` 的范围直接绑定到 `ratio_c`，综合器据此推断计数器位宽。

核心判断在这一行（[hdl/psi_common_strobe_generator.vhd:44-50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L44-L50)）：

```vhdl
if (count = ratio_c - 1) or ((sync_i = '1') and (syncLast = '0')) then
  vld_o <= '1';
  count  <= 0;
else
  vld_o <= '0';
  count  <= count + 1;
end if;
syncLast <= sync_i;   -- 打一拍，用于下次边沿检测
```

注意这是 `or` 的两个**相互独立**的条件：计数到顶产生选通（正常自激），与 `sync_i` 上升沿产生选通（外部对齐），互不干扰。`syncLast <= sync_i` 就是 2.3 节所说的边沿检测寄存器。

#### 4.1.4 代码实践

**实践目标**：在 100 MHz 时钟下生成 1 kHz 选通，观察 `ratio_c` 取值并验证频率。

**操作步骤**（源码阅读型 + 仿真）：

1. 阅读测试平台 [testbench/psi_common_strobe_generator_tb/psi_common_strobe_generator_tb.vhd:51-61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_strobe_generator_tb/psi_common_strobe_generator_tb.vhd#L51-L61)，看 DUT 如何被实例化。注意 TB 把 generic 也参数化了：`freq_clock_g`、`freq_strobe_g`（整数）在 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) 里以 `-gfreq_clock_g=... -gfreq_strobe_g=...` 传入（见 4.1.5）。
2. 自行推算：`ratio_c = ceil(100.0e6 / 1000.0) = 100000`，即每 100000 个时钟周期产生一个选通。
3. 看 TB 的自检逻辑 [testbench/psi_common_strobe_generator_tb/psi_common_strobe_generator_tb.vhd:117-130](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_strobe_generator_tb/psi_common_strobe_generator_tb.vhd#L117-L130)：它连续测量 100 个选通周期，断言每个周期与理论值 `periodExp = 1s/freq_strobe_g` 的偏差小于一个时钟周期。

**需要观察的现象**：仿真无 `###ERROR###` 打印，说明选通周期稳定在理论值 ±1 个时钟周期内。

**预期结果**：100 MHz/1 kHz 时，相邻两次 `OutVld_obs='1'` 之间相隔约 100000 个时钟周期（10 µs）。

> 待本地验证：具体仿真命令需按 u1-l3 搭好 PsiSim/psi_tb 工作副本结构后，用 `sim/run.tcl` 跑 `psi_common_strobe_generator_tb` 这一条用例。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ratio_c` 用 `ceil`（向上取整）而不是 `round` 或 `floor`？
**答案**：`ceil` 保证计数比 ≥ 真实比值，即选通**不会比期望更快**（不会超出下游处理能力）。若用 `floor`，计数比可能偏小，选通频率偏高，可能压垮下游。

**练习 2**：库里有现成的 `math_pkg.ratio(a,b)` 函数（见 [hdl/psi_common_math_pkg.vhd:494-506](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L494-L506)），为什么 `strobe_generator` 没用它？
**答案**：`ratio()` 会自动判断两值大小并取大/小（且两值相等时会打 warning、返回 1）；而这里要求**严格** `freq_clock_g/freq_strobe_g`（时钟必大于选通频率），作者直接写 `ceil(freq_clock_g/freq_strobe_g)` 语义更直白，也避免了 `ratio()` 在两频率相等时打 warning 的副作用。

---

### 4.2 strobe_generator_cfg：周期法（运行时可配置）

#### 4.2.1 概念说明

`strobe_generator_cfg` 是 **HEAD（98c2fcc）新增**的组件（提交信息：「ADD: add new strobe generator with clock cycle count, no tb will come later」）。它与 `strobe_generator` 解决同一个问题——产生单周期选通——但**配置方式完全不同**：

- `strobe_generator` 用**频率**（Hz）描述，且在 generic 里写死，综合后不可改；
- `strobe_generator_cfg` 用**周期数**（多少个时钟周期发一次）描述，且通过端口 `count_i` 在**运行时**动态配置。

换言之，前者是「我要 1 kHz」，后者是「我要每 1234 个时钟周期一次」。后者适合需要在线调采样率、节拍间隔可变的场合。

> 注意：本组件目前**没有测试平台**（提交信息明确说「no tb will come later」），也未登记到 `sim/config.tcl`。使用时需自行验证。

#### 4.2.2 核心流程

```text
复位 或 (count_i 发生变化) → count=0, vld_o=0
每个上升沿：
  若 (count 数到 count_i-1) 或 (sync_i='1')，且 (syncLast='0'):
      vld_o <= '1'
      count <= 0
  否则:
      vld_o <= '0'
      count <= count+1
```

两个关键点：

1. **周期来自端口**：目标周期是 `from_uslv(count_i)`（把 `std_logic_vector` 转成整数），因此可在运行时由寄存器/AXI 总线改写。
2. **配置变更即复位计数**：内部用一个打一拍的 `count_dff_s` 跟踪 `count_i`，一旦发现 `count_i` 变了，就把计数器清零重新开始，避免新旧周期混杂。

#### 4.2.3 源码精读

实体声明（[hdl/psi_common_strobe_generator_cfg.vhd:20-28](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator_cfg.vhd#L20-L28)）：`nb_g` 是 `count_i` 的位宽（1~32），`count_i` 即「周期数」。

内部信号（[hdl/psi_common_strobe_generator_cfg.vhd:32-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator_cfg.vhd#L32-L34)）：`count` 是运行计数器，`count_dff_s` 是 `count_i` 的延迟一拍副本，用于检测配置变化。

进程里两段关键逻辑：

「配置变化即复位」分支（[hdl/psi_common_strobe_generator_cfg.vhd:41-42](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator_cfg.vhd#L41-L42)）：

```vhdl
count_dff_s <= count_i;
if rst_i = rst_pol_g or count_dff_s /= count_i then
  count <= 0; ...
```

正常计数与选通产生（[hdl/psi_common_strobe_generator_cfg.vhd:47-53](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator_cfg.vhd#L47-L53)）：

```vhdl
if (count = from_uslv(count_i)-1 or (sync_i = '1')) and (syncLast = '0') then
  vld_o <= '1'; count <= 0;
else
  vld_o <= '0'; count <= count + 1;
end if;
```

这里用到了 u2-l1 讲过的 `from_uslv`（[hdl/psi_common_math_pkg.vhd:386-389](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L386-L389)），它等价于 `to_integer(unsigned(input))`。

> **与 4.1 的细微差别**：`strobe_generator` 的条件是 `(count到顶) or (sync上升沿)`——两个条件独立；而本组件是 `((count到顶) or (sync='1')) and (syncLast='0')`——整个表达式都被 `syncLast='0'` 门控。也就是说，若把 `sync_i` 长期拉高，本组件会被 `syncLast` 锁住而不再发周期选通；而 `strobe_generator` 不会。使用 `sync_i` 时请留意这一差异。

#### 4.2.4 代码实践

**实践目标**：实例化 `strobe_generator_cfg`，让它在 `count_i=100` 与 `count_i=50` 之间切换，观察选通频率的变化。

**操作步骤**（示例代码——非项目原有代码）：

```vhdl
-- 示例代码：周期可配置的选通发生器
signal cfg_period : std_logic_vector(7 downto 0) := to_uslv(100, 8); -- 借用 math_pkg.to_uslv

i_strb_cfg : entity work.psi_common_strobe_generator_cfg
  generic map(rst_pol_g => '1', nb_g => 8)
  port map(clk_i => clk, rst_i => rst,
           count_i => cfg_period, sync_i => '0', vld_o => strb);
```

**需要观察的现象**：`cfg_period=100` 时，`strb` 每 100 个时钟周期拉高一拍；把 `cfg_period` 改成 50 后，选通间隔立即变成 50 个周期。

**预期结果**：选通频率随 `count_i` 改变而实时变化；配置变更那一拍计数器被清零。

> 待本地验证：本组件无测试平台，需自行搭建最小 TB 或在已有工程里观测波形确认。

#### 4.2.5 小练习与答案

**练习 1**：如果 `count_i` 写成全 0（即 `from_uslv(count_i)=0`），会发生什么？
**答案**：判断条件变成 `count = 0-1 = -1`，而 `count` 从 0 起递增、永不为 -1（且 `sync_i` 默认为 0），于是 `vld_o` 永远不拉高。即 `count_i=0` 是非法配置，与 `strobe_divider` 显式处理 `ratio=0` 不同，本组件不会「直通」，而是静默不出选通。使用时务必保证 `count_i ≥ 1`。

**练习 2**：为什么需要 `count_dff_s` 这个延迟寄存器？
**答案**：用来检测 `count_i` 是否刚刚发生变化（`count_dff_s /= count_i`）。一旦检测到变化就清零计数器，使新周期从 0 开始干净计数，避免「按旧周期数到一半、又按新周期判断」导致的错乱。

---

### 4.3 tickgenerator：固定时间基准（us/ms/sec）

#### 4.3.1 概念说明

很多系统需要「墙上时间」式的节拍：每 1 微秒、1 毫秒、1 秒各发一个脉冲，用来驱动看门狗、统计窗口、日志刷新等。`tickgenerator` 就是为这类需求设计的专用组件，它一次输出**三个**节拍（`tick1us_o` / `tick1ms_o` / `tick1sec_o`），并且脉冲宽度可配。

与前两个组件最大的不同：

- 它**不用频率/周期 generic 来描述选通**，而是只给一个 `clk_in_mhz_g`（时钟频率，单位 MHz），内部自动推导出 us/ms/sec 三级分频。
- 它**没有复位端口**——所有计数器都带初值（`: = 1`），依赖 FPGA 上电初始化。

#### 4.3.2 核心流程

这是一条**级联进位计数器**链（carry chain）：

\[
\underbrace{clk}_{f_{clk}\text{ MHz}} \xrightarrow{\div clk\_in\_mhz\_g} \underbrace{1\,\text{us}} \xrightarrow{\div 1000} \underbrace{1\,\text{ms}} \xrightarrow{\div 1000} \underbrace{1\,\text{sec}}
\]

逻辑可概括为：

```text
count_clk 数到 clk_in_mhz_g    → carry_1us=1, 产生 1us tick, count_clk 回 1
  (仅在 carry_1us=1 时) count_1us 数到 1000 → carry_1ms=1, 产生 1ms tick, count_1us 回 1
    (仅在 carry_1us 且 carry_1ms 时) count_1ms 数到 1000/speedup → carry_1sec=1, 产生 1sec tick
```

其中 `c_THRESHOLD = 1000` 是写死的常量（us→ms、ms→sec 都是 1000 进制），唯一随时钟变化的是第一级 `clk_in_mhz_g`。

**仿真加速开关** `sim_sec_speedup_factor_g`：它**只作用于秒级**（缩短 `count_1ms` 的上限，见 [hdl/psi_common_tickgenerator.vhd:78](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L78)），让仿真不必真的等 1 秒。文档与源码注释都强调：**实现（上板）时必须设为 1**。

#### 4.3.3 源码精读

实体与 generic（[hdl/psi_common_tickgenerator.vhd:17-25](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L17-L25)）：注意没有 `rst_i`，`tick_width_g` 控制脉冲宽度。

第一级（产生 us 进位），[hdl/psi_common_tickgenerator.vhd:55-61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L55-L61)：

```vhdl
if (count_clk < clk_in_mhz_g) then
  count_clk <= count_clk + 1; carry_1us := '0';
else
  carry_1us := '1'; count_clk <= 1;   -- 数到 clk_in_mhz_g 即 1 us
end if;
```

第二级（产生 ms 进位），[hdl/psi_common_tickgenerator.vhd:64-73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L64-L73)：仅当 `carry_1us='1'` 时才递增 `count_1us`，数到 `c_THRESHOLD`(1000) 即 1 ms。

第三级（产生 sec 进位），[hdl/psi_common_tickgenerator.vhd:76-90](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L76-L90)：仅在 `carry_1us` 与 `carry_1ms` 同时为 1 时递增 `count_1ms`，上限是 `c_THRESHOLD / sim_sec_speedup_factor_g`。

**脉冲宽度控制**用了一个移位寄存器（[hdl/psi_common_tickgenerator.vhd:51-53](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L51-L53) 与 [hdl/psi_common_tickgenerator.vhd:96-98](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L96-L98)）：进位发生时把整个向量置全 1，之后每拍从低位补 0 移出；输出取最高有效位之一，于是脉冲宽度 = `tick_width_g` 个时钟周期。

#### 4.3.4 代码实践

**实践目标**：读懂测试平台如何校验三个节拍的精度。

**操作步骤**：

1. 阅读 [testbench/psi_common_tickgenerator_tb/psi_common_tickgenerator_tb.vhd:85-107](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tickgenerator_tb/psi_common_tickgenerator_tb.vhd#L85-L107)。
2. 看 TB 如何测量：`wait until rising_edge(tick1us)` 两次，记录时间差，断言它恰为 `1 us`。
3. 注意秒级断言用的是 `(1 sec / SIM_SPEEDUP_FACTOR)`（TB 里 `SIM_SPEEDUP_FACTOR=20`，[testbench/psi_common_tickgenerator_tb/psi_common_tickgenerator_tb.vhd:41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tickgenerator_tb/psi_common_tickgenerator_tb.vhd#L41)），即仿真里秒节拍每 50 ms 来一次。

**需要观察的现象**：三条 `SUCCESS:` 打印出现，无 `###ERROR###`。

**预期结果**：在 8 MHz 时钟下（TB 默认 `clk_in_mhz_g=8`），1 us 节拍每 8 个时钟周期来一次。

#### 4.3.5 小练习与答案

**练习 1**：`sim_sec_speedup_factor_g` 为什么只影响秒级、不影响 us/ms？
**答案**：因为 us 和 ms 的仿真时间很短（微秒/毫秒级），可接受；而真正等 1 秒会让仿真极慢。作者只在第三级 `count_1ms` 的上限里除以该因子（[第 78 行](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L78)），把秒节拍提前，从而加速仿真；上板时因子为 1，秒节拍恢复为真实 1 秒。

**练习 2**：为什么这个组件没有复位端口？
**答案**：所有计数器与移位寄存器都带初值（`:= 1`、`:= (others => '0')`，见 [hdl/psi_common_tickgenerator.vhd:34-40](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tickgenerator.vhd#L34-L40)），依赖 FPGA 上电时的寄存器初始化机制。对纯 FPGA 目标这是常见做法，但若用于 ASIC 或需要同步复位的场合则需自行加复位。

---

### 4.4 strobe_divider：选通分频（消费已有选通）

#### 4.4.1 概念说明

前三个组件都是「凭空产生」选通。`strobe_divider` 不同——它**接收一个已有的选通**，每收到 N 个就放行 1 个到输出，相当于对选通做整数分频。典型用法：先用 `strobe_generator` 产生一个较快的基准选通，再用 `strobe_divider` 派生出若干更慢的选通，避免在设计中堆多个大计数器。

它的分频比 `ratio_i` 是**运行时可配置**的端口（不是 generic），并且对输入做了**上升沿检测**——所以输入既可以是单周期选通，也可以是多周期的普通脉冲，它都能正确识别「一次事件」。

#### 4.4.2 核心流程

```text
复位 → counter=0
每个上升沿：
  str_dff_s <= vld_i           -- 打一拍，做边沿检测
  vld_o <= '0'                 -- 默认不输出
  若 (检测到 vld_i 上升沿):
      若 (counter = ratio_i-1) 或 (ratio_i = 0):   -- 数满 或 非法值
          counter <= 0
          vld_o <= '1'         -- 放行这一个
      否则:
          counter <= counter+1
```

两个要点：

1. **输出比输入晚一拍**：因为做了边沿检测（`str_dff_s`）且输出寄存，文档明确指出「output has a delay of one clock cycle compared to the input」。
2. **`ratio_i = 0` 是非法值**，组件做了保护：直接把每个输入都放行（不分频），见 [hdl/psi_common_strobe_divider.vhd:46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_divider.vhd#L46)。

#### 4.4.3 源码精读

实体（[hdl/psi_common_strobe_divider.vhd:18-26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_divider.vhd#L18-L26)）：`width_g` 是 `ratio_i` 的位宽（默认 4，即最大分频比 15），`rst_pol_g` **默认 `'0'`（低有效）**——注意这与 `strobe_generator`（默认 `'1'`）相反，混用时务必核对。

核心逻辑（[hdl/psi_common_strobe_divider.vhd:43-52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_divider.vhd#L43-L52)）：

```vhdl
str_dff_s <= vld_i;
vld_o     <= '0';
if str_dff_s = '0' and vld_i = '1' then          -- 上升沿检测
  if (counter_s = unsigned(ratio_i) - 1) or (unsigned(ratio_i) = 0) then
    counter_s <= 0;  vld_o <= '1';                -- 数满(或非法)则放行
  else
    counter_s <= counter_s + 1;
  end if;
end if;
```

测试平台验证了两种输入形态（[testbench/psi_common_strobe_divider_tb/psi_common_strobe_divider_tb.vhd:117-147](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_strobe_divider_tb/psi_common_strobe_divider_tb.vhd#L117-L147)）：单周期选通与 50% 占空比脉冲，并断言输出计数 `= 输入计数 / ratio`（非法 `ratio=0` 时 TB 用 `choose` 改写成 1，见 [第 42 行](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_strobe_divider_tb/psi_common_strobe_divider_tb.vhd#L42)）。

#### 4.4.4 代码实践

**实践目标**：把 1 kHz 选通分频 10 倍，得到 100 Hz 选通。

**操作步骤**（示例代码——非项目原有代码，承接 4.1 产生的 `strb_1k`）：

```vhdl
-- 示例代码：1kHz ÷ 10 = 100Hz
signal strb_1k  : std_logic;
signal strb_100 : std_logic;

i_div : entity work.psi_common_strobe_divider
  generic map(width_g => 4, rst_pol_g => '1')     -- 注意改回高有效复位，与上面统一
  port map(clk_i => clk, rst_i => rst,
           vld_i => strb_1k,
           ratio_i => to_uslv(10, 4),             -- 10 = "1010"
           vld_o => strb_100);
```

**需要观察的现象**：`strb_100` 每出现一次，`strb_1k` 恰好出现了 10 次；且 `strb_100` 比「第 10 个 `strb_1k`」晚一个时钟周期。

**预期结果**：输入 1 kHz、分频比 10 → 输出 100 Hz，相位滞后一拍。

> 待本地验证：可参照 `psi_common_strobe_divider_tb` 的结构自建最小 TB，用 `IntCompare` 校验输出计数。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `strobe_divider` 要对输入做上升沿检测，而 `strobe_divider` 的输入明明叫 `vld_i`（暗示是单周期选通）？
**答案**：为了通用性。若输入确实是单周期选通，上升沿检测等价于直接用 `vld_i`；但若上游给出的是多周期电平（如某个「忙」标志），上升沿检测能确保只在它**首次拉高**那一拍计一次事件，不会重复计数。文档也明示了这一点。

**练习 2**：分频比上限是多少？想分频 1000 倍怎么办？
**答案**：上限由 `width_g` 决定，默认 `width_g=4` 即最大 15。要分频 1000 倍，需把 `width_g` 设为至少 10（\(2^{10}=1024 \ge 1000\)），并保证 `ratio_i` 端口位宽匹配。

---

## 5. 综合实践

**任务**：在 100 MHz 时钟下，用本讲学到的组件搭建一个「多速率节拍分发」小系统：

1. 用 `strobe_generator` 产生一个 **1 kHz** 基准选通（`freq_clock_g=100.0e6`、`freq_strobe_g=1000.0`，理论 `ratio_c=100000`）。
2. 用 `strobe_divider` 把这个 1 kHz 选通分频 **10 倍**，得到 **100 Hz** 选通。
3. 再加一个 `tickgenerator`（`clk_in_mhz_g=100`）输出 1 ms 节拍，观察 100 Hz 选通（周期 10 ms）与 1 ms 节拍之间的对齐关系。

**要求**：

- 画出（或用文字描述）三个输出的时序关系：1 kHz、100 Hz、1 ms。
- 注意三个组件的**复位极性默认值不同**（`strobe_generator` 与 `strobe_divider` 默认不同），在顶层统一成同一种极性。
- 说明：如果要求 1 kHz 这个频率**运行时可调**，应该把 `strobe_generator` 换成哪个组件？（答：换成 `strobe_generator_cfg`，并用 `count_i` 控制周期。）

**预期结论**：100 MHz 下，1 kHz 选通每 100000 拍一次；经 10 分频后 100 Hz 每 1000000 拍一次（=10 ms）；`tickgenerator` 的 1 ms 节拍（100000 拍一次）恰是 100 Hz 周期的 1/10。三者共享同一时钟即可对齐。

> 待本地验证：本综合实践为设计型任务，需自行实例化并仿真确认各选通周期。

## 6. 本讲小结

- **选通（strobe）**是单周期宽的「点名」脉冲，FPGA 系统靠它驱动各种周期性动作；其本质是分频计数器，计数比 \(ratio=\lceil f_{clk}/f_{strobe}\rceil\)。
- **`strobe_generator`** 用**频率**（Hz）描述、周期在 generic 里综合时固定，适合频率不变的场合；`sync_i` 上升沿可重新对齐相位。
- **`strobe_generator_cfg`**（HEAD 新增、暂无 TB）用**周期数**描述、由端口 `count_i` **运行时**配置，配置变化即清零计数器；调用 `math_pkg.from_uslv` 把位向量转成整数。
- **`tickgenerator`** 是专用时间基准组件，靠**级联进位计数器**一次输出 1 us/1 ms/1 sec 三种节拍，`c_THRESHOLD=1000` 写死、无复位端口、`sim_sec_speedup_factor_g` 仅加速秒级仿真。
- **`strobe_divider`** 消费**已有选通**做整数分频，对输入做上升沿检测（兼容多周期脉冲），`ratio_i` 运行时可配、`ratio=0` 视为非法直通，输出滞后输入一拍。
- **选型口诀**：源头用 generator（频率固定 → `strobe_generator`；周期可调 → `strobe_generator_cfg`；要 us/ms/sec → `tickgenerator`）；变慢已有选通用 `strobe_divider`。

## 7. 下一步学习建议

- **u6-l2 时钟测量 `clk_meas`**：它正是用一个已知参考时钟去**测量**另一个时钟/选通的频率——本质上是对本讲「计数比」思想的反向应用，建议紧接着学。
- **u8-l2 并-TDM 转换 `par_tdm`/`tdm_par`**：TDM 转换几乎总是需要一个 `strobe_generator` 提供通道切换节拍，是本讲组件的直接下游。
- **u9 SPI/I2C 主机**：SPI/I2C 的 SCLK 分频、字节节拍都依赖选通发生器，学完本讲再去看总线接口会更顺。
- 建议在本地按 u1-l3 搭好 PsiSim/psi_tb 工作副本后，跑一遍 `psi_common_strobe_generator_tb`、`psi_common_strobe_divider_tb`、`psi_common_tickgenerator_tb` 这三条回归用例，亲手验证本讲的频率/分频结论。
