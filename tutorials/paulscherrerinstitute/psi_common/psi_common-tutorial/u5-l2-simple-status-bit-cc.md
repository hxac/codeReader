# 数据/状态/位跨越：simple_cc / status_cc / bit_cc

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `psi_common_simple_cc` 如何**复用** `pulse_cc`，把"脉冲跨越"升级为"单值数据跨越"——数据原地锁存、只让 `valid` 这一个 bit 跨域，并理解它"数据率 ≤ 目标时钟的 1/4"的硬性限制。
- 说清楚 `psi_common_status_cc` 如何在 `simple_cc` 之上自动产生握手节拍（请求-应答回环），从而在无需使用者提供 `valid`、也无需知道时钟频率的情况下搬运慢变状态/配置值。
- 说清楚 `psi_common_bit_cc` 为何是库中最简单的跨越——一个带综合属性的双级同步器，以及它"各 bit 不保证同拍到达"的根本限制。
- 面对一个真实场景，在**脉冲 / 单值数据 / 慢变状态 / 独立单 bit** 四种跨越形态中做出正确选型。
- 理解这三个组件如何把"复位输出"（`a_rst_o` / `b_rst_o`）对外传递，以及 `bit_cc` 为何**不**提供复位跨越。

## 2. 前置知识

本讲是 CDC（Clock Domain Crossing，时钟域跨越）单元的第二篇，承接 [u5-l1 脉冲跨越 pulse_cc 与复位同步](u5-l1-pulse-cc.md)。请先建立以下两块认知。

**多 bit 总线跨域的"撕裂"危险。** 跨时钟域之所以难，不在于单 bit——单 bit 最坏只是采到"旧值"或"新值"，串两级触发器（同步器）就能解决。真正危险的是多 bit 总线：当多个 bit 在同一个源时钟沿同时翻转，由于各 bit 布线延迟不同，目的时钟可能在"某些 bit 已翻、某些 bit 还没翻"的中间态上采样，得到一个既不是旧值也不是新值的**撕裂值**。例如 4 位总线从 `0111` 变到 `1000` 时，目的域可能瞬间采到 `1111`。本讲的三个组件，本质上都在回答同一个问题：**怎样让多 bit 数据跨域时不被撕裂。**

**pulse_cc 的"翻转-同步-异或"手法（复习）。** 在 [u5-l1](u5-l1-pulse-cc.md) 讲过：`pulse_cc` 把 A 域一个单周期脉冲先转成"长期稳定的电平翻转（toggle）"，经 B 域多级同步器采样，再用相邻两级同步值的"异或"把翻转还原成 B 域的单周期脉冲；它同时用 4 级同步链做"异步复位、同步释放"，并通过 `ASYNC_REG` / `shreg_extract` / `syn_srlstyle` 属性把同步器锁定为真实触发器。本讲 `simple_cc` 直接例化 `pulse_cc` 来搬运 `valid` 信号，所以这一手法是本讲的底层。

本讲要讲的三个组件，与 `pulse_cc` 的关系一览：

| 组件 | 跨越对象 | 复用关系 | 有复位跨越？ |
|:-----|:---------|:---------|:-------------|
| `simple_cc` | 单个数据值（带 `Vld`） | 直接例化 `pulse_cc` | 有 |
| `status_cc` | 慢变状态/配置值 | 例化 `simple_cc` | 有 |
| `bit_cc` | 多个相互独立的单 bit | 自带双级同步器（**不**用 `pulse_cc`） | **无** |

> 小提示：库里还有 `sync_cc_n2xn` / `sync_cc_xn2n`，处理的是**整数比同步时钟**下的连续 AXI-S 数据流，本讲不涉及（见 [u5-l3](u5-l3-sync-ratio-cc.md)）。本讲三个组件针对的都是**完全异步时钟**之间的"事件型"跨越。

## 3. 本讲源码地图

本讲涉及四个源码文件，全部位于 `hdl/` 目录下：

| 文件 | 作用 | 是否本讲精读 |
|:-----|:-----|:-------------|
| [hdl/psi_common_pulse_cc.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd) | 脉冲 + 复位跨越的基石，被 `simple_cc` 直接例化 | 回顾引用 |
| [hdl/psi_common_simple_cc.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd) | 单值数据跨越（带 `Vld` 握手） | 精读 |
| [hdl/psi_common_status_cc.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd) | 慢变状态/配置值跨越，自动生成节拍 | 精读 |
| [hdl/psi_common_bit_cc.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd) | 独立单 bit 跨越，双级同步器 | 精读 |

对应测试平台：`testbench/psi_common_simple_cc_tb/` 与 `testbench/psi_common_status_cc_tb/`，两者都已在 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) 注册（分别在第 237、245 行）。注意 `bit_cc` **没有专属测试平台**——它的行为就是两个寄存器，没什么可自检的。

## 4. 核心概念与源码讲解

### 4.1 simple_cc：把脉冲跨越升级成单值数据跨越

#### 4.1.1 概念说明

`pulse_cc` 只能跨越"有没有发生过一个事件"（脉冲），跨越不了事件**携带的数据**。但现实里我们经常要跨时钟域传一个**采样值**，比如"ADC 在 A 域采到了一个数，要送到 B 域处理"。这种传递有两个特点：

1. 数据本身是多 bit 总线，**不能**直接送进同步器（多 bit 同时翻转会采到撕裂中间值）。
2. 数据是**偶发**的，用一个 `Vld`（valid）脉冲标记"这一拍数据有效"，符合 AXI-S 握手约定，但**不需要反压**（`Rdy`）。

`simple_cc` 的核心思想非常巧妙：**数据不动，只跨 `Vld` 这一个脉冲。** 数据在源域用一个寄存器锁住，等 `Vld` 慢慢同步到目标域时，源域那个寄存器里的数据早就稳定了，目标域再安全地把它采走。

这个想法能成立的前提是：**下一个 `Vld` 必须等上一个被处理完之后才能来**。所以文档里写死了一条限制——

> 数据率必须显著低于目标时钟频率，具体是 **4 倍以下**（4x lower）。

#### 4.1.2 核心流程

`simple_cc` 内部只做三件事：

```text
┌──────────── 域 A (a_clk_i) ────────────┐   ┌──────── 域 B (b_clk_i) ────────┐
│  a_vld_i ──┐                           │   │   VldBI    ┌────────────────┐  │
│  a_dat_i ──┤  DataA_p:                 │   │           │  DataB_p:       │  │
│            ▼  a_vld_i=1 时             │   │           │ VldBI=1 时      │  │
│   ┌─────────────────┐   a_vld_i(0)     │   │           │  b_dat_o<=锁存 │  │
│   │ DataLatchA<=dat │──────────► i_pulse_cc ───►│      │  b_vld_o<=VldBI│  │
│   └─────────────────┘                  │   │           └────────────────┘  │
└────────────────────────────────────────┘   └────────────────────────────────┘
```

1. **A 域锁存数据**：只要 `a_vld_i='1'`，就把 `a_dat_i` 写进寄存器 `DataLatchA`（不要求握手，来了就存）。
2. **`pulse_cc` 跨越 `Vld`**：把 `a_vld_i` 当作单 bit 脉冲喂给 `pulse_cc`，在 B 域还原成 `VldBI`。
3. **B 域采数据**：`VldBI='1'` 那一拍，B 域把 `DataLatchA`（此时早已稳定）读进 `b_dat_o`，同时 `b_vld_o` 跟随 `VldBI`。

数据总线**没有**经过同步器——它只是被源域锁住后，在目标域"恰好有效"的那一拍被采样。这是整个设计的精髓：因为 B 域采样时刻比数据更新时刻晚了数拍（同步链延迟），此时 `DataLatchA` 早已稳定，所以不会撕裂。

#### 4.1.3 源码精读

实体声明，注意它用 `width_g` 让数据位宽完全 generic 化（[hdl/psi_common_simple_cc.vhd:L19-L33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L19-L33)）：

```vhdl
entity psi_common_simple_cc is
  generic(width_g      : positive := 16;        -- 数据位宽
          a_rst_pol_g  : std_logic:='1';        -- A 域复位极性
          b_rst_pol_g  : std_logic:='1');       -- B 域复位极性
  port(   a_clk_i      : in  std_logic;
          a_rst_i      : in  std_logic;
          a_rst_o      : out std_logic;         -- 合并后的 A 域复位输出
          a_dat_i      : in  std_logic_vector(width_g - 1 downto 0);
          a_vld_i      : in  std_logic;         -- AXI-S valid
          b_clk_i      : in  std_logic;
          b_rst_i      : in  std_logic;
          b_rst_o      : out std_logic;         -- 合并后的 B 域复位输出
          b_dat_o      : out std_logic_vector(width_g - 1 downto 0);
          b_vld_o      : out std_logic);        -- AXI-S valid
```

关键点是它**直接例化 `pulse_cc`**，把 `a_vld_i` 接到 `pulse_cc` 的 `a_dat_i(0)`，从 `b_dat_o(0)` 取回还原后的脉冲 `VldBI`（[hdl/psi_common_simple_cc.vhd:L46-L63](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L46-L63)）：

```vhdl
i_pulse_cc : entity work.psi_common_pulse_cc
  generic map( num_pulses_g => 1, ... )
  port map(
    a_clk_i    => a_clk_i,
    a_rst_i    => a_rst_i,
    a_rst_o    => RstAI,          -- pulse_cc 顺带把复位跨越也做了
    a_dat_i(0) => a_vld_i,        -- ★ 把 valid 当成单脉冲喂进去
    b_clk_i    => b_clk_i,
    b_rst_i    => b_rst_i,
    b_rst_o    => RstBI,
    b_dat_o(0) => VldBI );        -- ★ 还原后的脉冲
a_rst_o <= RstAI;                 -- 复位输出对外透传
b_rst_o <= RstBI;
```

A 域锁存数据的过程极其简单——`a_vld_i` 一拉高就存（[hdl/psi_common_simple_cc.vhd:L66-L77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L66-L77)）：

```vhdl
DataA_p : process(a_clk_i) is
begin
  if rising_edge(a_clk_i) then
    if RstAI = a_rst_pol_g then
      DataLatchA <= (others => '0');
    else
      if a_vld_i = '1' then
        DataLatchA <= a_dat_i;    -- 不看任何 ready，来了就存
      end if;
    end if;
  end if;
end process;
```

B 域在收到脉冲的那一拍把稳定的数据采走（[hdl/psi_common_simple_cc.vhd:L80-L93](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L80-L93)）：

```vhdl
DataB_p : process(b_clk_i) is
begin
  if rising_edge(b_clk_i) then
    if RstBI = b_rst_pol_g then
      b_dat_o <= (others => '0');
      b_vld_o <= '0';
    else
      b_vld_o <= VldBI;           -- valid 直接跟随还原脉冲
      if VldBI = '1' then
        b_dat_o <= DataLatchA;    -- 此时源域数据已稳定，安全采样
      end if;
    end if;
  end if;
end process;
```

可以看到：`b_dat_o` 只在 `VldBI='1'` 那一拍更新，**valid 撤销后 `b_dat_o` 保持上一次的值**（这一行为会被测试平台专门验证，见 4.1.4）。

> **读源码 vs 读文档**：组件文档 `doc/files/psi_common_simple_cc.md` 把 generic 写成 `data_width_g`，但**真实源码用的是 `width_g`**。源码是权威，文档表格里的名字偶尔会过时，实例化时一律以 `.vhd` 为准。

#### 4.1.4 代码实践

**实践目标**：通过阅读自校验测试平台，确认 `simple_cc` 在 `valid` 撤销后"保持上次数据"的行为，并预测仿真结果。

**操作步骤**：

1. 打开 [testbench/psi_common_simple_cc_tb/psi_common_simple_cc_tb.vhd:L18-L21](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_simple_cc_tb/psi_common_simple_cc_tb.vhd#L18-L21)，注意它的两个 generic：`clock_ratio_n_g=3`、`clock_ratio_d_g=2`，配合第 35 行的 `ClockAFrequency_c := 100.0e6`，即默认 A=100 MHz、B=150 MHz（异步），数据宽度 `DataWidth_c=8`。
2. 阅读 [testbench/psi_common_simple_cc_tb/psi_common_simple_cc_tb.vhd:L163-L174](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_simple_cc_tb/psi_common_simple_cc_tb.vhd#L163-L174) 的数据测试段：
   - 第 164-168 行：A 域把 `a_dat_i=X"AB"`、`a_vld_i='1'` 摆一拍，然后撤销 `a_vld_i`。
   - 第 169-170 行：等 B 域 `b_vld_o='1'`，断言 `b_dat_o = X"AB"`。
   - 第 171-174 行：再等 10 个 B 时钟周期（此时 `valid` 早已撤销），断言 `b_dat_o` **仍然是** `X"AB"`。
3. 该 TB 已在 [sim/config.tcl:L237](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L237) 注册为 `create_tb_run "psi_common_simple_cc_tb"`，会以多组不同 `clock_ratio` 各跑一遍。

**需要观察的现象**：`valid` 是单周期脉冲；脉冲过后，`b_dat_o` 不归零、不清空，而是**锁存住最近一次传输的值**，直到下一次 `valid` 才更新。

**预期结果**：两条 `assert` 都通过（不打印 `###ERROR###`）。若手头有仿真器，可按 [u1-l3](u1-l3-dependencies-and-simulation.md) 的方式跑 `sim/run.tcl`；否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `simple_cc` 把数据总线 `DataLatchA` 直接从 A 域连到 B 域，却不违反 CDC 原则？

> **答**：因为 B 域只在 `VldBI='1'` 那一拍采样 `DataLatchA`，而 `VldBI` 是 `Vld` 脉冲经 `pulse_cc` 同步链后才出现的，此时 `DataLatchA` 早已稳定多拍，不存在撕裂窗口。CDC 的禁忌是"直接用同步器采多变信号总线"，而这里采的是一个**已知稳定**的寄存器。

**练习 2**：如果把目的时钟从 50 MHz 换成 200 MHz，`simple_cc` 允许的最大数据速率会如何变化？

> **答**：最大数据速率正比于目的时钟频率（约束是 ≤ f_b/4）。f_b 从 50 MHz 升到 200 MHz，允许的数据速率也提升 4 倍（从约 12.5 MS/s 提升到 50 MS/s）。

**练习 3**：`simple_cc` 没有 `Rdy`（反压）信号。如果你需要反压，应该用库里的哪个组件？

> **答**：`simple_cc` 面向偶发单值，不支持反压，调用者必须自行限速（≤ f_b/4）。需要反压的连续数据流应使用 `async_fifo`（[u4-l2](u4-l2-async-fifo.md)）或整数比同步时钟下的 `sync_cc_n2xn` / `sync_cc_xn2n`（[u5-l3](u5-l3-sync-ratio-cc.md)）。

### 4.2 status_cc：自驱动节拍的慢变状态跨越

#### 4.2.1 概念说明

`simple_cc` 要求**使用者**自己产生 `a_vld_i`，并且要保证速率够低。但有一类信号根本谈不上"采样率"——比如一个 FIFO 的填充水位、一个配置寄存器的值、一个状态标志。它们变化很慢，我们只关心"B 域能不能拿到 A 域当前最新的值"，不关心具体哪一拍拿到，偶尔漏掉中间值也无所谓。

`status_cc` 就是为此而生。它的接口比 `simple_cc` 还简单——**只有数据，没有 `Vld`**：

| | `simple_cc` | `status_cc` |
|:--|:--|:--|
| 输入 | `a_dat_i` + `a_vld_i` | 仅 `a_dat_i` |
| 输出 | `b_dat_o` + `b_vld_o` | 仅 `b_dat_o` |
| 采样时机 | 由使用者用 `Vld` 指定 | 由组件**自己**内部生成 |
| 适用 | 偶发的数据采样 | 慢变状态/配置 |

文档给出的限制更松也更严——**变化率 ≤ 较慢时钟的 1/10**；但即使变化更快也不会出错，只是会"跳过"中间值（B 域总是看到"上一次传的值或下一次将传的值"之一）。

#### 4.2.2 核心流程

`status_cc` 的核心难题是：**既然使用者不提供 `Vld`，组件怎么知道何时该把数据传一次？** 答案是构造一个**自驱动的请求-应答（request/acknowledge）环路**：

```text
A 域                                  B 域
─────                                ─────
  │  ① 复位后，A 自动产生第一个 VldA      │
  │ ────────────(经 simple_cc)────────► │  ② B 收到 VldB，采到数据
  │                                      │  ③ B 翻转 RecToggle（应答）
  │  ④ A 同步到 RecToggle 的变化         │
  │ ◄──────────(3 级同步)──────────────  │
  │  ⑤ A 检测到变化 → 产生下一个 VldA     │
  │ ────────────(经 simple_cc)────────► │  ...循环往复
```

这套机制完全不依赖两个时钟的频率：A 只有在确认"B 已经收到上一个值"之后，才会发下一个。于是节拍被自动调节到 CDC 延迟能承受的最大速率，**无需使用者知道任何频率信息**。

检测"应答变化"用了一个小技巧：把同步后的 `RecToggle` 存成两级 `RecToggleSync`，当**最高位和次高位不同**时，说明刚刚捕获到一次翻转边沿，于是触发下一次发送——本质上就是 `pulse_cc` 里"同步 + 异或还原脉冲"思想在反向（B→A）通路上的复用。

#### 4.2.3 源码精读

`status_cc` 内部例化了 `simple_cc`（[hdl/psi_common_status_cc.vhd:L105-L122](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L105-L122)），自己只负责**生成 `VldA`** 和**检测接收**：

```vhdl
i_scc : entity work.psi_common_simple_cc
  generic map( width_g => width_g, ... )
  port map(
    a_clk_i => a_clk_i,  a_rst_i => a_rst_i,  a_rst_o => RstIntA,
    a_dat_i => a_dat_i,  a_vld_i => VldA,        -- ★ status_cc 自己生成的节拍
    b_clk_i => b_clk_i,  b_rst_i => b_rst_i,  b_rst_o => RstIntB,
    b_dat_o => b_dat_o,  b_vld_o => VldB );      -- ★ 接收指示
```

**A 域节拍生成**（[hdl/psi_common_status_cc.vhd:L62-L88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L62-L88)）做两件事。第一，复位释放后产生**唯一一个**首发脉冲（用 `Started` 标志位保证只发一次）：

```vhdl
-- 把 B 域复位状态同步回 A，等 B 复位释放后再开始
RstIntBSync <= RstIntBSync(RstIntBSync'left - 1 downto 0) & RstIntB;
...
-- 产生首个 vld 脉冲
if (Started = '0') and (RstIntBSync(RstIntBSync'left) = '0') then
  VldA    <= '1';
  Started <= '1';
end if;
```

第二，把 B 域的"应答翻转"`RecToggle` 同步回 A，**检测到变化就发下一个**：

```vhdl
RecToggleSync <= RecToggleSync(RecToggleSync'left - 1 downto 0) & RecToggle;
...
-- 检测应答翻转：相邻两级同步寄存器不同，说明 B 刚应答过
if RecToggleSync(RecToggleSync'left) /= RecToggleSync(RecToggleSync'left - 1) then
  VldA <= '1';
end if;
```

**B 域接收检测**（[hdl/psi_common_status_cc.vhd:L91-L102](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L91-L102)）每收到一个 `VldB` 就翻转一次应答位：

```vhdl
p_recdet : process(b_clk_i) is
begin
  if rising_edge(b_clk_i) then
    if RstIntB = b_rst_pol_g then
      RecToggle <= '0';
    else
      if VldB = '1' then
        RecToggle <= not RecToggle;   -- ★ 每收到一个值就翻转一次
      end if;
    end if;
  end if;
end process;
```

> **读懂这段代码的关键**：整个 `status_cc` 没有直接例化 `pulse_cc`，而是借用了同样的"翻转-同步-边沿检测"套路，只不过这里它跑在反向（B→A）通路上，用来传"应答"。复位和综合属性同样齐全（[hdl/psi_common_status_cc.vhd:L47-L58](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L47-L58)）：`RstIntBSync`、`RecToggle`、`RecToggleSync` 三个跨域信号都贴了 `ASYNC_REG` / `shreg_extract` / `syn_srlstyle`，确保综合器把它们实现成真实触发器。

#### 4.2.4 代码实践

**实践目标**：确认 `status_cc` 的"使用者全程不提供 `valid`"特性，并验证 B 域最终能追上 A 域的慢变值。

**操作步骤**：

1. 打开 [testbench/psi_common_status_cc_tb/psi_common_status_cc_tb.vhd:L154-L168](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_status_cc_tb/psi_common_status_cc_tb.vhd#L154-L168) 的数据测试段。
2. 第 163-165 行：复位释放后，把 `a_dat_i <= X"AB"`，**只设置数据、不碰任何 valid**，等 `12 * SlowerClockPeriod_c`，断言 `b_dat_o = X"AB"`。
3. 第 166-168 行：改为 `a_dat_i <= X"CD"`，再等 12 个慢周期，断言 `b_dat_o = X"CD"`。
4. 该 TB 已在 [sim/config.tcl:L245](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L245) 注册为 `create_tb_run "psi_common_status_cc_tb"`。

**需要观察的现象**：使用者**全程没有提供任何 valid 信号**，只改 `a_dat_i`；组件自己完成了所有节拍。`b_dat_o` 最终等于最近设置的值。TB 用 `SlowerClockPeriod_c`（两端较慢者）作等待单位，且故意等较长时间（12 个慢周期），正对应"数据变化必须比慢时钟慢得多（≤1/10）"的约束。

**预期结果**：两条断言通过（不打印 `###ERROR###`）。若手头有仿真器可跑回归；否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`status_cc` 为什么不需要 `Vld` 输入？它的 `VldA` 从哪里来？

> **答**：因为这是慢变状态信号，使用者无法（也不必）指定采样点。`VldA` 由组件内部 `p_vldgen` 进程自动生成——首发脉冲由"B 域复位释放"触发，后续脉冲由"B 域应答翻转被同步回 A"触发，形成一个自驱动的请求-应答环。

**练习 2**：为什么 `status_cc` 要求变化率 ≤ 较慢时钟的 1/10，而 `simple_cc` 是 1/4？

> **答**：`status_cc` 一次传输要走完一个完整的请求-应答往返（A→B 发数据 + B→A 回应答），延迟是 `simple_cc` 单程的大约两倍以上，所以同样安全裕度下允许的变化率更低。但这只是"保证不跳值"的速率；信号变化更快时组件仍能正确工作，只是会跳过中间值。

**练习 3**：`RecToggleSync(left) /= RecToggleSync(left-1)` 这句判断在检测什么？

> **答**：检测 `RecToggle`（B 域应答位）的跳变。`RecToggleSync` 是把 B 域 `RecToggle` 同步到 A 域的 3 级移位寄存器，比较最高两级就是在做边沿检测——二者不同说明 B 刚翻转过（刚收到一个值），于是 A 可以发下一个值了。

### 4.3 bit_cc：独立单 bit 的双级同步器

#### 4.3.1 概念说明

前面两个组件都为了"跨多 bit 数据"而构造了锁存 + 握手机制。但有一类信号天生就是**单 bit 且相互独立**的——比如几个独立的中断标志、几个互不相关的控制开关。对这种信号，最简单也最标准的做法就是：**每个 bit 各自串一个双级同步器**。

`bit_cc` 就是这个最简组件。它甚至**不例化 `pulse_cc`**——因为单 bit 信号本身就没有"多位同翻撕裂"的风险，两级触发器足矣。它也**不带复位**（连复位端口都没有），因为这类静态控制位通常不需要确定的复位值，复位由各自所属的逻辑处理。

它有一个必须牢记的限制（来自[官方文档](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_bit_cc.md)）：

> 各 bit **不保证**在同一拍到达目标域，因此**只能**用于相互独立的单 bit 信号。

为什么？因为每个 bit 的同步器独立工作，亚稳态收敛时间是随机的，某个 bit 可能这一拍稳定、另一个下一拍才稳定（bit skew）。如果你把一个多 bit 总线（比如 `0101`）喂给 `bit_cc`，目标域可能采到 `0000` 或 `1111` 这种撕裂中间值——这和"用同步器直接采多位总线"是同一个错误。

#### 4.3.2 核心流程

`bit_cc` 的全部行为可以用一行话讲完：每个 bit 经过两个串接的触发器，输出第二级的值。

```text
dat_i(bit) ──► [Reg0] ──► [Reg1] ──► dat_o(bit)
                 ▲          ▲
                 └─ ASYNC_REG/shreg_extract/syn_srlstyle 属性，锁定为真实 FF
```

数据流是单向的（A 域 → B 域），只有一个目的时钟 `clk_i`（即 B 域时钟），A 域甚至不暴露时钟端口——源端信号由 A 域自己的逻辑驱动即可。

#### 4.3.3 源码精读

实体极其精简，只有一个 generic 和三个端口（[hdl/psi_common_bit_cc.vhd:L19-L24](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L19-L24)）：

```vhdl
entity psi_common_bit_cc is
  generic(width_g : positive := 1);
  port(   dat_i : in  std_logic_vector(width_g - 1 downto 0);  -- 源域（A）
          clk_i : in  std_logic;                               -- 目的域（B）时钟
          dat_o : out std_logic_vector(width_g - 1 downto 0)); -- 目的域（B）
```

实现就是一个进程，两级寄存器（[hdl/psi_common_bit_cc.vhd:L45-L52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L45-L52)）：

```vhdl
p_dff : process(clk_i) is
begin
  if rising_edge(clk_i) then
    Reg0 <= dat_i;
    Reg1 <= Reg0;
  end if;
end process;
dat_o <= Reg1;
```

注意这个进程**没有 `reset` 分支**——这是 `bit_cc` 与本讲其他两个组件的重大区别。信号声明里给了初值 `(others => '0')`（[hdl/psi_common_bit_cc.vhd:L28-L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L28-L29)），上电后两个寄存器为 0，之后就是纯粹的同步采样。

真正"有技术含量"的是那一组综合属性（[hdl/psi_common_bit_cc.vhd:L31-L41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L31-L41)）：

```vhdl
attribute syn_srlstyle of Reg0 : signal is "registers";   -- 别用 SRL
attribute shreg_extract of Reg0 : signal is "no";          -- 别吸收进移位查找表
attribute ASYNC_REG of Reg0 : signal is "TRUE";            -- 标记为异步路径第一级
-- Reg1 同样三件套
```

这三件套与 `pulse_cc` 里用的完全一样（见 [u5-l1](u5-l1-pulse-cc.md)）。`ASYNC_REG=TRUE` 告诉 Xilinx Vivado：这个寄存器的输入来自异步时钟域，请把 `Reg0` 和 `Reg1` 紧挨着放进同一个 slice、并自动施加 `set_max_delay` 约束；`shreg_extract=no` / `syn_srlstyle=registers` 阻止综合器把两个触发器合并进 SRL/LUT 移位单元。**有了这些属性，`bit_cc` 不需要任何手写时序约束**——官方文档明确写"无需特殊约束，只要输出时钟的周期约束即可"。

> **读源码 vs 读文档**：文档 `doc/files/psi_common_bit_cc.md` 把 generic 写成 `num_bits_g`，源码同样是 `width_g`——又一个"文档名过期、源码为准"的例子。

#### 4.3.4 代码实践

**实践目标**：通过思想实验（源码阅读型实践）理解"为什么 `bit_cc` 不能用于多 bit 总线"。

**操作步骤**：

1. **正确用法**：在纸上设计——A 域有 4 个独立的中断标志 `irq0..irq3`，要用 `bit_cc` 的 `width_g=4` 一次跨越。说明每个中断是独立的双级同步器，互不影响。
2. **错误用法**：假设你误把一个 A 域的 4 位计数器 `cnt(3:0)` 接到 `bit_cc` 的 `dat_i`。对照源码第 45-52 行：4 个 bit 各自独立走 `Reg0`/`Reg1`，互不等待。画出当 `cnt` 从 `0111` 变到 `1000`（4 个 bit 同时翻）时，B 域可能采到的中间值。
3. 阅读官方文档 [doc/files/psi_common_bit_cc.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_bit_cc.md) 关于"不保证同拍到达"的告警。

**需要观察的现象**：

- 正确用法中，4 个中断哪个先到 B 域无关紧要，因为它们逻辑独立。
- 错误用法中，`cnt` 从 `0111→1000` 时，B 域可能采到 `0000`、`1111`、`0110` 等任意中间组合——这就是用 `bit_cc` 跨相关多 bit 总线的经典陷阱。

**预期结果**：理解"独立单 bit"是 `bit_cc` 的硬性前提。本实践为推理型，无需运行仿真；本组件无测试平台。待本地验证（无仿真器时通过代码推理即可）。

#### 4.3.5 小练习与答案

**练习 1**：`bit_cc` 为什么不给复位端口？上电时输出是什么？

> **答**：靠寄存器声明初值 `(others => '0')` 初始化（FPGA 触发器上电初值或仿真初值），所以省掉了复位端口与复位同步逻辑；上电后输出先为 0，随后跟随源端稳定值。若你的应用要求确定性的同步复位，应在源端保证 `dat_i` 复位为已知值。

**练习 2**：`bit_cc` 的进程只有 `Reg0 <= dat_i; Reg1 <= Reg0;` 两行，似乎谁都会写。它的价值在哪里？

> **答**：价值在那一组综合属性。如果没有 `ASYNC_REG`/`shreg_extract`/`syn_srlstyle`，综合器可能把两个触发器优化进 SRL 或拉开布局，导致同步器的 MTBF（平均无故障时间）急剧下降。`bit_cc` 把"标准双级同步器 + 正确属性 + 跨厂商约束"打包成一个可复用、免手写约束的单元。

**练习 3**：5 个独立的中断标志要从 200 MHz 域跨到 100 MHz 域，用 `bit_cc(width_g=>5)` 合适吗？

> **答**：合适。各中断相互独立，bit 间错位不影响语义；每个中断只是"有/无"事件，两级同步器足够。这正是 `bit_cc` 的典型用法。若误用 `bit_cc` 跨一个 8 位相关状态字则会撕裂，那应改用 `status_cc`。

### 4.4 三者选型与复位输出传递

#### 4.4.1 概念说明

学完三个组件，最实用的能力是**会选**。把上一讲的 `pulse_cc` 也纳入对比，CDC 单元里"层级递进"的四个组件各管一种信号形态：

| 组件 | 信号形态 | 是否带数据 | 谁管节拍 | 反压(rdy) | 典型场景 |
|:--|:--|:--|:--|:--|:--|
| `pulse_cc` | 单 bit 事件 | 否 | 用户(vld) | 否 | 触发、中断脉冲 |
| `bit_cc` | 多个独立单 bit | 是（各 bit 独立） | 源端持续驱动 | 否 | 独立标志、控制位 |
| `simple_cc` | 多 bit 样本 + valid | 是 | 用户(vld) | 否 | 偶发采样数据 |
| `status_cc` | 多 bit 慢变值 | 是 | 组件自动 | 否 | 状态/配置寄存器 |
| `async_fifo` | 连续数据流 | 是 | 握手 | 是 | 高速连续流（见 [u4-l2](u4-l2-async-fifo.md)） |

选型的核心是回答三个问题：①信号是单 bit 还是多 bit？单 bit 且独立 → `bit_cc`；多 bit → `simple_cc` 或 `status_cc`。②谁来决定采样时机？使用者能给出明确 `Vld` → `simple_cc`；信号慢变、给不出 `Vld` → `status_cc`。③纯事件无数据 → `pulse_cc`；连续高速流 → `async_fifo`。

#### 4.4.2 核心流程：复位输出是怎么"透传"出来的

`simple_cc` 和 `status_cc` 都有一对 `a_rst_o` / `b_rst_o` 输出。它们的语义是：**任一域的复位被声明，两个域都会进入复位，且各自向自己的时钟同步释放。** 这让使用者可以把这两个 `rst_o` 直接当成该域的"干净复位"来用。

这条复位链的源头其实都在 `pulse_cc` 里（`simple_cc` 直接拿 `pulse_cc` 的 `a_rst_o`/`b_rst_o`；`status_cc` 拿 `simple_cc` 的，间接还是 `pulse_cc` 的）。`pulse_cc` 用 4 级同步链做"异步复位、同步释放"，并用 `a_rst_pol_g` / `b_rst_pol_g` 两个 generic 支持高/低有效复位的四种组合（详见 [u5-l1](u5-l1-pulse-cc.md)）。于是上层组件几乎不用写复位跨越代码，只要把 `pulse_cc` 的复位输出"接出来"即可——等于白送一个跨域复位同步器。`bit_cc` 则**完全没有复位端口**，这是它和另外两者的重大区别。

#### 4.4.3 源码精读

三件套的复用关系一目了然，全部可在源码里指认：

- `simple_cc` 例化 `pulse_cc` 传 valid：[hdl/psi_common_simple_cc.vhd:L46-L61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L46-L61)。
- `status_cc` 例化 `simple_cc` 传数据：[hdl/psi_common_status_cc.vhd:L105-L122](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L105-L122)。
- `bit_cc` 不依赖任何其他组件，自含两级同步器：[hdl/psi_common_bit_cc.vhd:L45-L52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L45-L52)。

`simple_cc` 的复位透传只有两行赋值（[hdl/psi_common_simple_cc.vhd:L62-L63](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L62-L63)）：

```vhdl
a_rst_o <= RstAI;   -- RstAI 来自内部 pulse_cc 的 a_rst_o
b_rst_o <= RstBI;   -- RstBI 来自内部 pulse_cc 的 b_rst_o
```

这两个信号随后被本组件自己的数据进程当作复位条件用，例如（[hdl/psi_common_simple_cc.vhd:L69](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L69)）`if RstAI = a_rst_pol_g then DataLatchA <= (others => '0');`。`status_cc` 的透传完全同理（[hdl/psi_common_status_cc.vhd:L123-L124](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L123-L124)）。

对比之下，`bit_cc` **完全没有复位端口**（[hdl/psi_common_bit_cc.vhd:L19-L24](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L19-L24)），进程里也没有 `reset` 分支。这是选型时容易踩的坑：如果你的下游逻辑依赖一个跨域过来的"干净复位"，**不能**用 `bit_cc`，得用 `simple_cc`（哪怕你只跨 1 bit 数据）或者直接用 `pulse_cc`。

> 因此形成一条清晰的依赖链：`pulse_cc` → `simple_cc` → `status_cc`，每往上一层就多解决一个问题（数据怎么带、节拍谁来管）；`bit_cc` 另起一路。理解这条链就能推断：改 `pulse_cc` 的同步器深度会影响 `simple_cc` 和 `status_cc` 的延迟，但不会影响 `bit_cc`。

#### 4.4.4 代码实践

**实践目标**：给定一组真实需求，为每个需求选出正确的组件并说明复位接法。

**操作步骤**：对下面四个需求，分别写出组件名 + generic + 复位端口处理方式。

| # | 需求描述 | 你的选择 |
|:-|:---------|:---------|
| a | 把 A 域 16 位"当前温度采样"偶发送到 B 域，A 域有 `Vld` | ？ |
| b | 把 A 域一个 FIFO 的 12 位填充水位送到 B 域监控 | ？ |
| c | 把 A 域 3 个独立的中断引脚送到 B 域 | ？ |
| d | 把 A 域的"系统复位"信号本身同步到 B 域，作为 B 域的干净复位 | ？ |

**需要观察的现象 / 预期结果**：

- a → `simple_cc`（`width_g=16`），使用者提供 `a_vld_i`，可用 `b_rst_o` 复位 B 域接收逻辑。
- b → `status_cc`（`width_g=12`），无需 `Vld`，节拍自驱动。
- c → `bit_cc`（`width_g=3`），不需要复位跨越。
- d → 这是跨"复位"本身，最贴切的是 `pulse_cc`（它内置复位同步链）；`bit_cc` 不合适（它不带复位跨越，单 bit 复位跨越应走专门的复位同步器）。

需求 a 是 `simple_cc` 与 `async_fifo` 的分水岭：当数据接近连续、可能超过 f_b/4 时，必须改用带反压的 `async_fifo`（[u4-l2](u4-l2-async-fifo.md)）。

#### 4.4.5 小练习与答案

**练习 1**：你需要跨时钟域传一个 8 位配置寄存器的值，B 域只是周期性地读它显示。用哪个组件最省心？

> **答**：`status_cc`。配置寄存器是典型的慢变状态，使用者无需提供 `Vld`，组件自节拍搬运，B 域永远看到某个一致的有效值。`bit_cc` 不行（8 位相关总线会撕裂），`simple_cc` 需要使用者自己造 `Vld`，更费事。

**练习 2**：三个组件中，哪个**不**直接或间接依赖 `pulse_cc`？这带来什么选型后果？

> **答**：`bit_cc`。它用自带的两个寄存器做双级同步，不例化 `pulse_cc`，也不复用它的复位跨越。后果：`bit_cc` 没有复位输出端口——若下游需要一个跨域过来的"干净复位"，必须改用 `simple_cc`/`status_cc`/`pulse_cc`。

**练习 3**：同样是"把一个 8 bit 值从 A 跨到 B"，什么情况下选 `simple_cc`、什么情况下选 `status_cc`？

> **答**：若该值是"事件触发的单样本"且你愿意自己拉 `valid`，选 `simple_cc`；若该值是"持续存在、慢变、没有明确采样点"的状态量且你不想管 `valid`，选 `status_cc`。若速率超过 f_b/4 或需要反压，两者都不对，选 `async_fifo`。

## 5. 综合实践：为一个双时钟域系统选型并接线

**任务背景**：假设你正在设计一个数据采集子系统，包含两个时钟域——`clk_adc`（100 MHz，ADC 采样域）和 `clk_ctrl`（50 MHz，控制/寄存器域），二者完全异步。你需要跨越这两个域传递以下 4 个信号：

1. ADC 每完成一次转换，会在 `clk_adc` 域给出一个 16 位采样值 `sample` 和一个一拍脉冲 `sample_vld`。
2. 一个 24 位的"增益配置"寄存器，由 `clk_ctrl` 域的寄存器映射写入，但 DAC（在 `clk_adc` 域）需要读取它。
3. 一个 `clk_adc` 域的"ADC 忙"标志，要送到 `clk_ctrl` 域的状态寄存器。
4. 一个"采样次数计数器溢出"的告警脉冲，要从 `clk_adc` 域送到 `clk_ctrl` 域去触发中断。

**请完成**：

1. 为每个信号选择本讲合适的 CC 组件（或说明应改用其他组件），给出 `width_g` 和方向。
2. 指出信号 1 在什么速率条件下会从"`simple_cc` 够用"切换到"必须用 `async_fifo`"，给出临界数据速率。
3. 写出每个组件实例化时的复位接法：哪些信号需要把 `rst_o` 接到下游逻辑、哪些不必。
4. 为这些 A→B 跨域路径写一条 Vivado 约束（参考 `doc/files/psi_common_simple_cc.md` 给出的范例）。

**参考思路**：

- 信号 1（偶发数据 + `Vld`，但若采样率高就是连续流）：若采样率 ≤ f_b/4 = 12.5 MS/s，可用 `simple_cc`（`width_g=16`）；若采样率接近 100 MHz，则必须改用 `async_fifo`（见 [u4-l2](u4-l2-async-fifo.md)）。
- 信号 2（慢变 24 位配置）：`status_cc`，`width_g=24`，方向 `clk_ctrl → clk_adc`。
- 信号 3（慢变 1 位状态）：`status_cc`（`width_g=1`）比 `bit_cc` 更合适——若下游依赖干净复位；若完全不关心复位，`bit_cc` 也能用。体会二者取舍。
- 信号 4（事件脉冲，无数据）：这正是 `pulse_cc`（[u5-l1](u5-l1-pulse-cc.md)）的本职工作，不必套 `simple_cc`。
- 约束范例：`set_max_delay --datapath_only --from [get_clocks clk_adc] -to [get_clocks clk_ctrl] 20.0`（20 ns = `clk_ctrl` 一个周期，50 MHz）。含义是源域到目的域的跨域路径延迟不得超过目的时钟一个周期。

**验证方式**：把你的选型写成一张表，对照 4.4.1 的决策三问逐条核验。若手头有仿真环境，可跑 `psi_common_status_cc_tb`（[sim/config.tcl:L245](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L245)），观察 B 域 `b_dat_o` 是否在若干慢时钟周期后追上 `a_dat_i`，作为信号 2 这类"慢变值"行为的佐证。若无，标注「待本地验证」并保留选型推理。

## 6. 本讲小结

- `simple_cc` 用"valid 走 `pulse_cc`、数据原地锁存"的方式，让多 bit 样本安全跨域——数据不跨域、只跨 valid 这一个 bit；它只用 valid、不带 ready，调用者须保证数据速率 ≤ f_b/4。
- `status_cc` 在 `simple_cc` 之上加了一个 A↔B 请求-应答回环（发 valid → B 翻转应答 → A 检测应答再发），从而**自动**生成传输节拍，适合慢变状态/配置值；允许跳过中间值，约束是变化率 ≤ f_slow/10。
- `bit_cc` 是给"相互独立的单 bit"每人配一组双级同步器，最便宜；但因各 bit 互不对齐，**严禁用于多 bit 相关总线**，否则产生撕裂值；它不带复位、不复用 `pulse_cc`。
- 三件套形成依赖链 `pulse_cc → simple_cc → status_cc`，`bit_cc` 独立成路；选型看"信号形态 + 谁管节拍 + 是否容忍跳值 + 是否需要反压"。
- 复位跨域在 `simple_cc`/`status_cc` 中都顺带由底层 `pulse_cc` 完成，并把 `a_rst_o`/`b_rst_o` 透传给使用者；`bit_cc` 无复位端口，靠寄存器初值——需要跨域干净复位时不能用 `bit_cc`。
- 文档表格里的 generic 名（`data_width_g`/`num_bits_g`）偶尔与源码（`width_g`）不一致，**实例化时一律以 `.vhd` 源码为准**。

## 7. 下一步学习建议

- 如果你的场景是**整数倍频的两个同步时钟**（比如 100 MHz 与 25 MHz，同源），本讲的异步跨越就过度了——请接着读 [u5-l3 同步整数比跨越 sync_cc_n2xn / sync_cc_xn2n](u5-l3-sync-ratio-cc.md)，看库如何用 `ratio` 和 AXI-S 握手高效处理这种情形。
- 如果你需要跨越的是**连续高速数据流**且需要反压，本讲三个组件都不合适，请读 [u4-l2 异步 FIFO：async_fifo 与格雷码指针](u4-l2-async-fifo.md)。
- 想了解这些组件如何被"按位宽批量生成"，可预习 [u11-l2 Python 代码生成器](u11-l2-code-generators.md)，其中 `generators/psi_common_simple_cc_X.py` 正是为 `simple_cc` 生成特定位宽实例的脚本。
- 想动手验证本讲组件，可阅读它们的测试平台（`testbench/psi_common_simple_cc_tb/`、`testbench/psi_common_status_cc_tb/`），其自校验 TB 结构将在 [u11-l1 编写自校验测试平台](u11-l1-self-checking-testbench.md) 系统讲解。
