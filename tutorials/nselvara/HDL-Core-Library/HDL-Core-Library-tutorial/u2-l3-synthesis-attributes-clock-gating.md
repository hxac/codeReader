# 综合属性、防优化与时钟门控策略

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `preserve`、`dont_touch`、`altera_attribute`、`SYNCHRONIZER_IDENTIFICATION` 这几类综合属性分别属于哪家厂商、各阻止综合工具做哪种「优化」。
- 解释为什么同步链寄存器、复位信号一旦被综合工具优化掉或合并，就会带来亚稳态恶化、复位不可靠等隐蔽 bug。
- 区分 Xilinx `BUFGCE` 提供的「无毛刺全局时钟门控」与 Intel「不推荐直接门控时钟、应改用寄存器使能」两种哲学的根本差异。
- 看懂 `clock_enable.vhd` 里三层嵌套 `if generate` 如何在**编译期（elaboration）**裁剪出唯一一套实现，并理解这与本库主流的「多 architecture」模式是两种等价的厂商适配手段。

## 2. 前置知识

在进入源码前，先建立三个直觉。它们是本讲所有讨论的基础。

### 2.1 综合工具会「自作主张」地优化

RTL 写的是「逻辑意图」，而综合工具（Xilinx 的 Vivado、Intel 的 Quartus）的任务是把它映射成器件上真实的查找表（LUT）和触发器（FF）。为了省面积、提速度，工具会做很多**重写**：

- 发现一个寄存器的输出从来没人用，就把它**删掉**（dead-code elimination）。
- 发现两个寄存器逻辑等价，就把它们**合并**成一个（register merging）。
- 把寄存器在流水线里前后**搬动**（retiming）来平衡时序。

对普通逻辑这很好；但对**同步链**和**复位树**，这种「优化」是灾难：同步链被缩短 → 亚稳态概率飙升；复位被合并/删除 → 上电状态不确定。综合属性就是设计者贴在信号上的「请勿动手」标签。

### 2.2 亚稳态与同步链

当信号从一个时钟域跨到另一个时钟域（CDC，Clock Domain Crossing），目的域的触发器很可能在输入变化的一瞬间采样，输出会长时间停留在 `0` 和 `1` 之间的非法电平上，这种现象叫**亚稳态（metastability）**。解决方法是串一串触发器（同步链），给电平足够的时间自行稳定。链越长，到链尾仍然非法的概率越低，平均故障间隔（MTBF）越长。所以同步链的**长度**和**每一级都被如实实现**，是设计意图的核心，绝不能被工具删级。

### 2.3 时钟门控的两种思路

让模块在空闲时停下来，有两种思路：

- **门控时钟（clock gating）**：直接把时钟信号本身关掉，模块没有翻转就没有动态功耗。但用普通与门关时钟会产生**毛刺（glitch）**，把寄存器误触发。
- **寄存器使能（register enable / clock enable）**：时钟照常翻转，但每个寄存器有一个 `CE` 引脚，`CE=0` 时该拍不锁存新值。Intel 推荐这种；Xilinx 则提供了专用的全局缓冲器 `BUFGCE`，能在全局时钟网络上做到**无毛刺**的门控。

本讲的 `clock_enable` 正是把这两种取舍封装成一个可配置模块。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `ip/clock_enable/clock_enable.vhd` | 时钟使能/门控模块，用嵌套 `if generate` 在 BUFGCE、通用门控、直通三种模式间选择 | BUFGCE 原语；嵌套 generate 编译期选择 |
| `ip/ff_synchroniser/ff_synchroniser.vhd` | 单比特跨时钟域同步器，有 Xilinx / Intel 两套 architecture | `preserve` 防删级；`altera_attribute` + `SYNCHRONIZER_IDENTIFICATION` |
| `ip/reset_on_startup/reset_on_startup.vhd` | 上电复位控制器，复位信号一拍延迟直通 | 同一信号上**同时**挂 `dont_touch`(Xilinx) 与 `preserve`(Intel) |

> 说明：`clock_enable` 与本库大多数 IP 的写法不同——它没有 `xilinx_behaviourral_*` / `intel_behaviourral_*` / `own_behaviourral_*` 三套并列的 architecture，而是用**一个** `architecture behavioural` + **generic 控制的嵌套 generate** 来切模式。这是上一讲（u2-l1）「同一实体多架构模式」之外的第二种厂商适配手段，本讲 4.3 会专门对比。

## 4. 核心概念与源码讲解

### 4.1 综合属性与防优化：让工具「别动」关键寄存器

#### 4.1.1 概念说明

本库用到的综合属性可分为两类：

**1. 通用「防删/防合并」属性**

| 属性 | 类型 | 归属工具 | 作用 |
|------|------|---------|------|
| `preserve` | `boolean` | Intel Quartus（Vivado 也部分支持） | 保留该信号/实例，禁止删除、合并、复制等优化 |
| `dont_touch` | `string`（值为 `"true"`） | Xilinx Vivado | 禁止对该信号/实例做优化、布局、重定时（retiming） |

注意两者的 **VHDL 类型不同**：`preserve` 是 `boolean`，`dont_touch` 是 `string`。这是工具各自的历史约定，不能混用。

**2. Intel 专属「透传赋值」属性 `altera_attribute`**

`altera_attribute` 是一个类型为 `string` 的通用属性，它的值是一段**原样的 Quartus 赋值语句**，会被原封不动地传给 Quartus。本库用它做了两件 Intel 同步器必须的事：

- `SYNCHRONIZER_IDENTIFICATION "FORCED IF ASYNCHRONOUS"`：告诉 Quartus 这个寄存器是同步器，让它做专门的布局（把同步链各级放在一起）和约束。
- `SDC_STATEMENT "... set_false_path -to ..."`：内嵌一段 SDC（Synopsys Design Constraints）时序约束，把到该寄存器的路径声明为**假路径（false path）**——因为它是异步跨域路径，时序分析本来就无意义，分析反而会报一堆假违例。

#### 4.1.2 核心流程

防优化属性的「工作流程」其实是**综合流程中的一个旁路**：

```text
RTL 源码（带属性）
   │
   ▼
elaboration（确立）── 属性随信号一并记录
   │
   ▼
综合优化阶段 ──┬─ 普通信号：照常删/并/搬
              └─ 带 preserve/dont_touch 的信号：跳过优化，原样保留
   │
   ▼
门级网表（同步链长度、复位树形态如实保留）
```

关键点：属性只在**综合阶段**起作用，仿真阶段一般被忽略——所以你在仿真里看不到它们的效果，这也是为什么本讲实践里多处需要标注「待本地综合验证」。

#### 4.1.3 源码精读

**(a) 同一信号挂双重属性——`reset_on_startup.vhd`**

这是全库最干净的「双厂商兼容」写法。复位延迟信号 `rst_delayed` 上**同时**挂了 Xilinx 的 `dont_touch` 和 Intel 的 `preserve`：

```vhdl
-- NOTE: These attributes are used to prevent synthesis tools from optimising the reset signal away
-- Xilinx attribute for preventing optimisation
attribute dont_touch: string;
attribute dont_touch of rst_delayed: signal is "true";

-- Intel/Altera attribute for preventing optimisation
attribute preserve: boolean;
attribute preserve of rst_delayed: signal is true;
```

永久链接：[ip/reset_on_startup/reset_on_startup.vhd:L28-L35](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L28-L35)

这段代码在说：`rst_delayed` 是把外部 `rst_in` 延迟一拍的本地复位，它驱动着一票下游寄存器的复位端。如果工具发现它能被「吸收」进下游或被常量传播掉，复位拓扑就乱了。所以两个属性一起挂——哪边工具来综合都能识别。这正是 u2-l1 讲过的「跨厂商」思想在**属性层**的体现。

**(b) 同步链的 `preserve` 与 `altera_attribute`——`ff_synchroniser.vhd` 的 Intel architecture**

Intel 实现里声明了三个信号，其中 `src_reg`（源域先寄存一刀）和 `sync_chain`（目的域同步链）都挂了 `preserve`：

```vhdl
signal src_reg: std_ulogic;
signal meta_stable_reg: std_ulogic;
signal sync_chain: std_ulogic_vector(SYNC_SHIFT_FF - 2 downto 0); -- -2: Include meta_stable_reg as first element

attribute altera_attribute: string;
attribute altera_attribute of src_reg: signal is "-name SYNCHRONIZER_IDENTIFICATION ""FORCED IF ASYNCHRONOUS""";
-- Apply a SDC constraint to meta stable flip flop
attribute altera_attribute of intel_behavioural_ff_synchroniser: architecture is "-name SDC_STATEMENT ""set_false_path -to [get_registers {*|sync_chain_in_dst_dom_proc:*|:meta_stable_reg}] """;

-- set 'preserve' attribute to src_reg and sync_chain -> the synthesis tool doesn't optimise them away
attribute preserve: boolean;
attribute preserve of src_reg: signal is true;
attribute preserve of sync_chain: signal is true;
```

永久链接：[ip/ff_synchroniser/ff_synchroniser.vhd:L66-L79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L66-L79)

请逐行读懂这段：

1. `src_reg` 的 `altera_attribute` 把它标记为同步器（`SYNCHRONIZER_IDENTIFICATION`），Quartus 会把后续各级尽量紧凑地放在一起，降低级间布线延迟——这正是源码开头 `@note` 提到的 "The FFs should be placed together"。
2. 架构级的 `altera_attribute` 内嵌了一条 SDC 假路径，**指向 `meta_stable_reg`**——同步链的第一级，也就是最可能发生亚稳态的那一级。把它的输入路径设为 false path，时序分析就不会再为这条本来就不可分析的异步路径报违例。
3. `preserve` 挂在 `src_reg` 和 `sync_chain` 上，注释明确写着「让综合工具不要把它们优化掉」。

> **关于 `meta_stable_reg` 的一个事实核对**：源码里 `meta_stable_reg`（[L68](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L68)）**本身并没有**单独的 `preserve` 属性——`preserve` 只挂在了 `src_reg` 和 `sync_chain` 上。`meta_stable_reg` 的保护是靠架构级 SDC `set_false_path -to ... meta_stable_reg`（假路径约束 + 同步器识别）来实现的，而非 `preserve`。本讲后面 4.1.4 的实践会基于**真实存在的** `src_reg`/`sync_chain` 上的 `preserve` 来设计，而不是去删一个并不存在的属性。

**(c) 同步链的实际数据流**

把这两段代码连起来看数据流：

```vhdl
sync_chain_in_dst_dom_proc: process (destination_clk)
begin
    if rising_edge(destination_clk) then
        meta_stable_reg <= src_reg;  -- First element of the sync chain
        sync_chain <= sync_chain(sync_chain'high - 1 downto sync_chain'low) & meta_stable_reg;
    end if;
end process;

destination_domain <= sync_chain(sync_chain'high);
```

永久链接：[ip/ff_synchroniser/ff_synchroniser.vhd:L91-L99](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L91-L99)

理解注释里 `-2` 的含义：同步链总长度是 `SYNC_SHIFT_FF`，但 `sync_chain` 这个向量的长度是 `SYNC_SHIFT_FF - 2`，因为链的「第一级」是单独的 `meta_stable_reg`，链的「最末级」才是真正稳定的输出 `sync_chain(high)`。若 `preserve` 不顶住，工具可能把这条精心设计的链折叠成 1 级，MTBF 直接崩塌。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `preserve` 阻止 Quartus 删级，并理解同步链被缩短的后果。

**操作步骤**：

1. 打开 [ip/ff_synchroniser/ff_synchroniser.vhd:L77-L79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L77-L79)，把这两行**注释掉**：

   ```vhdl
   attribute preserve of src_reg: signal is true;
   attribute preserve of sync_chain: signal is true;
   ```

2. 用 Intel Quartus 综合 `intel_behavioural_ff_synchroniser`（设 `SYNC_SHIFT_FF => 4`），打开综合后的网表 / RTL Viewer。
3. 对照另一份**未改动**的综合结果，数一数目的域同步链的触发器个数。

**需要观察的现象**：

- 改动前：目的域应有 4 级（`meta_stable_reg` + `sync_chain` 的 3 位）。
- 改动后：Quartus 可能因为「这些寄存器逻辑等价/可吸收」而把它们合并或删减，链变短。

**预期结果**：去掉 `preserve` 后，同步链长度不再受设计者控制，**MTBF 显著下降**。由于这是综合阶段的行为，仿真看不出来——**待本地综合验证**（需要 Quartus 工具链）。如果你没有工具，可改为「源码阅读型实践」：在 `reset_on_startup.vhd` 的 [L30-L35](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L30-L35) 上解释：为什么 `rst_delayed` 必须**同时**挂 `dont_touch` 和 `preserve`，缺一个会怎样（缺 `dont_touch` → Vivado 可能优化它；缺 `preserve` → Quartus 可能优化它）。

#### 4.1.5 小练习与答案

**练习 1**：`preserve` 和 `dont_touch` 的 VHDL 类型分别是什么？为什么必须用不同类型？

**参考答案**：`preserve` 是 `boolean`（值 `true`），`dont_touch` 是 `string`（值 `"true"`）。类型不同是因为它们由两家不同的工具厂商各自约定——Quartus 识别 `boolean` 型 `preserve`，Vivado 识别 `string` 型 `dont_touch`。把两者都声明出来，同一份代码在两家工具下都能被正确识别。

**练习 2**：在 `ff_synchroniser` 的 Intel architecture 里，`meta_stable_reg` 没有 `preserve`，它靠什么被「保护」？

**参考答案**：靠架构级 `altera_attribute` 内嵌的 SDC 约束 `set_false_path -to ... meta_stable_reg`（[L74](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L74)），配合 `src_reg` 上的 `SYNCHRONIZER_IDENTIFICATION` 让 Quartus 把它当同步器对待。它的「保护」走的是**时序约束/同步器识别**路线，而不是 `preserve` 的「禁止删减」路线。

**练习 3**：为什么时序分析器要对到 `meta_stable_reg` 的路径声明 `set_false_path`？

**参考答案**：这是一条异步跨时钟域路径，数据相对目的时钟的到达时间是无规律的，本来就不满足、也不该用同步建立/保持时间去衡量。不声明 false path，时序分析会报大量假违例，淹没真正的问题。

---

### 4.2 BUFGCE：Xilinx 的无毛刺全局时钟门控

#### 4.2.1 概念说明

`BUFGCE` 是 Xilinx 的「带时钟使能的全局缓冲器」（Global Buffer with Clock Enable）。它有两个关键特性：

- 它坐在**全局时钟网络**上，驱动能力强、偏斜（skew）小，专门用来把时钟分发到整片 FPGA。
- 它的使能引脚 `CE` 经过专门设计，**在时钟边沿之间切换使能**，保证输出要么是完整的时钟脉冲、要么是稳定的常量电平——**不会产生毛刺**。

对比之下，最朴素的手写门控是：

```vhdl
clk_out <= clk_in when clk_enable = '1' else '0';
```

这是一个普通的**多路选择器/与门**。如果 `clk_enable` 恰好在 `clk_in` 为高电平期间变化，输出会出现一个被截短的窄脉冲（runt pulse / glitch），下游触发器可能在窄脉冲的边沿上被误触发，造成功能错误。`BUFGCE` 就是为了消除这个隐患而存在的硬核原语。

#### 4.2.2 核心流程

`BUFGCE` 的接口极简：

```text
        ┌──────────┐
  I ───▶│  BUFGCE  │───▶ O   (门控后的时钟输出)
        │          │
  CE ──▶│  (使能)   │
        └──────────┘
```

- `I`：输入时钟。
- `CE`：使能，`1` 时时钟透传，`0` 时输出冻结（停在某个固定电平）。
- `O`：输出，驱动全局网络。

使能的切换由原语内部同步处理，确保只在「安全」时刻生效，故无毛刺。

#### 4.2.3 源码精读

`clock_enable.vhd` 里 Xilinx 模式直接例化 `BUFGCE`：

```vhdl
xilinx_clk_gate: if USE_XILINX_CLK_GATE_AND_NOT_INTERNAL generate
    BUFGCE_inst: BUFGCE
        port map (
            O => clk_out,
            CE => clk_enable,
            I => clk_in
        );
```

永久链接：[ip/clock_enable/clock_enable.vhd:L47-L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L47-L53)

注意文件顶部的库声明：

```vhdl
library unisim;
use unisim.vcomponents.all;
```

永久链接：[ip/clock_enable/clock_enable.vhd:L22-L23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L22-L23)

`BUFGCE` 来自 Xilinx 的 `unisim` 库（底层硬件原语），所以必须 `library unisim` 并 `use unisim.vcomponents.all`。这与 u2-l2 讲过的厂商库体系一致：RTL 例化原语，行为由厂商库在仿真时提供、由器件知识在综合时映射。

还要读文件开头那段详尽注释，它直接讲明了厂商取舍：

```vhdl
--! @note:      Clock gating differences between vendors:
--!             - Xilinx: Use BUFGCE (set USE_XILINX_CLK_GATE_AND_NOT_INTERNAL to true) for
--!               glitch-free clock gating on global clock networks.
--!             - Intel/Altera: Direct clock gating (clk_out <= clk_in when enable else '0')
--!               is NOT recommended for Intel devices. Instead:
--!               1. Set ENABLE_INTERNAL_CLOCK_GATING to false when using Intel devices.
--!               2. Use PLL enable pins at the clock source instead.
--!               3. Use register enable pins rather than gating the clock.
```

永久链接：[ip/clock_enable/clock_enable.vhd:L5-L14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L5-L14)

这段注释是本模块的「设计说明书」：Xilinx 有 BUFGCE，可以安全门控；Intel 没有等价物，门控会引入风险，所以推荐**寄存器使能**而不是门控时钟。

#### 4.2.4 代码实践

**实践目标**：通过阅读与对比，理解「手写门控产生毛刺、BUFGCE 不产生毛刺」的差别。

**操作步骤**：

1. 读 [ip/clock_enable/clock_enable.vhd:L55-L57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L55-L57) 的通用门控分支：`clk_out <= clk_in when clk_enable = '1' else '0';`
2. 构想一个激励：`clk_in` 跑 100 MHz，在 `clk_in` 高电平正中间拉低 `clk_enable`。
3. 在脑海里（或画图）跟踪 `clk_out` 的波形。

**需要观察的现象**：手写门控会在 `clk_enable` 下跳的瞬间，把当时那个高电平脉冲截断成一个窄脉冲。

**预期结果**：窄脉冲的边沿可能被下游触发器当成有效时钟沿，导致误翻转——这正是注释里「NOT recommended」的原因。`BUFGCE` 因为内部对 `CE` 做了安全处理，不会出现这种截断。**待本地仿真验证**（可在 ModelSim 里对通用门控分支跑一个最小 testbench 复现毛刺）。

#### 4.2.5 小练习与答案

**练习 1**：`BUFGCE` 的三个端口 `I`、`CE`、`O` 分别是什么？

**参考答案**：`I` 是输入时钟，`CE` 是使能（高有效，为 0 时冻结输出），`O` 是门控后的全局时钟输出。

**练习 2**：为什么 `clk_out <= clk_in when clk_enable = '1' else '0';` 在 Intel 上不推荐，却能出现在 Xilinx 的「else」分支里作为兜底？

**参考答案**：它是「没有 BUFGCE 时的最简兜底」，逻辑上能门控，但本质是普通选择器，存在毛刺风险，所以源码注释明确把它标注为「not recommended for Intel」。它存在的意义是给「既非 Xilinx、又确实需要门控」的场景留一个能工作的退路，而非推荐用法。

---

### 4.3 嵌套 if generate：编译期选择门控策略

#### 4.3.1 概念说明

`if generate` 是 VHDL-2008 的并发语句，在**elaboration（确立）阶段**根据常量/generic 的真假来决定哪些代码被纳入设计。它的判断发生在综合之前，效果是「编译期裁剪」——只有命中分支的电路才存在。

`clock_enable` 用**两层嵌套** `if generate` 把三种门控策略压进**一个** `architecture behavioural`：

| `ENABLE_INTERNAL_CLOCK_GATING` | `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL` | 生成的实现 |
|---|---|---|
| `true` | `true` | Xilinx `BUFGCE`（无毛刺全局门控） |
| `true` | `false` | 通用门控 `clk_out <= clk_in when clk_enable='1' else '0'`（不推荐 Intel） |
| `false` | （忽略） | 直通 `clk_out <= clk_in`（推荐 Intel，靠寄存器使能停模块） |

这是一种与 u2-l1「多 architecture」**不同但等价**的厂商适配手段：

- **多 architecture**：把每个厂商实现写成独立的 architecture，调用方用 `entity work.xxx(arch_name)` 选。
- **嵌套 generate**：把所有实现写进一个 architecture，用 generic 在编译期选分支。

两者都能做到「换厂商只改一处配置」，区别在于组织粒度：多 architecture 是「文件内分块」，generate 是「architecture 内分叉」。本库大多用前者，`clock_enable` 选了后者，因为它要切的是**几个原语级别的小片段**，用 generate 更紧凑。

#### 4.3.2 核心流程

确立阶段的裁剪过程：

```text
读取 generic: ENABLE_INTERNAL_CLOCK_GATING, USE_XILINX_CLK_GATE_AND_NOT_INTERNAL
        │
        ▼
   评估外层 clk_gating generate
        │
   ┌────┴────────────────────┐
   true                      false
   │                         │
   ▼                         ▼
评估内层 xilinx_clk_gate   选中「直通」
generate                  clk_out <= clk_in
   │
┌──┴──┐
true  false
│     │
▼     ▼
BUFGCE  通用门控
```

最终只有一条路径上的代码会被实例化。其余分支对综合工具而言**不存在**，不占资源、不报错。

#### 4.3.3 源码精读

完整的三层结构：

```vhdl
architecture behavioural of clock_enable is
begin
    clk_gating: if ENABLE_INTERNAL_CLOCK_GATING generate
        xilinx_clk_gate: if USE_XILINX_CLK_GATE_AND_NOT_INTERNAL generate
            BUFGCE_inst: BUFGCE port map ( O => clk_out, CE => clk_enable, I => clk_in );
        else generate
            clk_out <= clk_in when clk_enable = '1' else '0';
        end generate;
    else generate
        clk_out <= clk_in; -- Clock passes through, rely on enable pins at registers instead
    end generate;
end architecture;
```

永久链接：[ip/clock_enable/clock_enable.vhd:L42-L62](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L42-L62)

请逐层看：

- **外层** [L45](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L45) `clk_gating`：是否启用内部门控。
- **内层** [L47](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L47) `xilinx_clk_gate`：内部门控启用时，用 Xilinx BUFGCE 还是通用门控。
- **外层 else** [L60](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L60)：完全直通，注释点名这是「推荐给 Intel/Altera」的模式——时钟照常跑，停模块改用下游寄存器的使能引脚。

两个 generic 的默认值也值得注意：

```vhdl
ENABLE_INTERNAL_CLOCK_GATING: boolean := true;
USE_XILINX_CLK_GATE_AND_NOT_INTERNAL: boolean := false
```

永久链接：[ip/clock_enable/clock_enable.vhd:L29-L33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L29-L33)

默认 `ENABLE_INTERNAL_CLOCK_GATING=true` 且 `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL=false` → 命中**通用门控**分支。这是一个中立默认值：既不假定 Xilinx（不强制 BUFGCE），也启用了某种门控。要安全门控，Xilinx 用户需显式把第二个 generic 设 `true`；Intel 用户需把第一个设 `false`。

#### 4.3.4 代码实践

**实践目标**：把本模块学的「门控取舍」固化成可读的配置注释，本讲综合任务里也会用到。

**操作步骤**：

1. 打开 [ip/clock_enable/clock_enable.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd)。
2. 在调用方（或一个假设的顶层）为两种目标器件各写一段「配置注释 + 例化」，明确 generic 取值。

**参考写法（示例代码，非项目原有）**：

```vhdl
-- ===== 示例代码：两种厂商的 clock_enable 配置 =====

-- 1) Xilinx：用全局网络上的 BUFGCE 做无毛刺门控
--    BUFGCE 坐在全局时钟网络上，CE 切换安全，无 runt pulse。
xilinx_inst: entity work.clock_enable
    generic map (
        ENABLE_INTERNAL_CLOCK_GATING      => true,
        USE_XILINX_CLK_GATE_AND_NOT_INTERNAL => true   -- 走 BUFGCE 分支
    )
    port map ( clk_in => pll_clk, clk_enable => ce, clk_out => gated_clk );

-- 2) Intel：不要门控时钟！改成「直通 + 寄存器使能」
--    直通让时钟常转，由下游寄存器的 CE 引脚决定哪一拍不锁存，
--    既省功耗又不引入毛刺。
intel_inst: entity work.clock_enable
    generic map (
        ENABLE_INTERNAL_CLOCK_GATING => false    -- 直通分支，clk_out <= clk_in
    )
    port map ( clk_in => pll_clk, clk_enable => ce, clk_out => always_clk );
-- 注意：Intel 方案下，真正「停模块」靠下游寄存器的 clock-enable 引脚实现。
```

**需要观察的现象**：两段配置只有 generic 不同，端口连线完全一致——这正是「接口稳定、实现可换」的好处。

**预期结果**：Xilinx 配置走 BUFGCE 分支（[L48-L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L48-L53)）；Intel 配置走直通分支（[L60](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L60)）。是否实际生效需综合后看网表——**待本地综合验证**。

#### 4.3.5 小练习与答案

**练习 1**：若 `ENABLE_INTERNAL_CLOCK_GATING=false`，`USE_XILINX_CLK_GATE_AND_NOT_INTERNAL` 还有意义吗？为什么？

**参考答案**：没有意义。外层 `clk_gating` generate 命中 else 分支（直通），内层 `xilinx_clk_gate` generate 根本不会被评估，所以第二个 generic 此时被忽略。这是嵌套 generate 的「短路」特性——外层为假时内层整棵子树不存在。

**练习 2**：`clock_enable` 用「嵌套 generate」而 `ff_synchroniser` 用「多 architecture」来处理厂商差异，各自的优缺点是什么？

**参考答案**：嵌套 generate 把所有实现集中在一个 architecture，适合差异是「几行原语」的小片段，代码紧凑、切换只需改 generic；缺点是厂商库声明（如 `library unisim`）必须放在文件顶部，即便某分支不用也得声明，依赖不够局部化。多 architecture 把每套实现隔离在独立 architecture，厂商库声明可贴在对应 architecture 之前（如 `ff_synchroniser` 的 `library xpm`），依赖局部化更干净；缺点是端口契约要在 entity 里写一次、每套实现都要重复维护。两者都实现了「换厂商只改一处」。

**练习 3**：为什么通用门控分支 `clk_out <= clk_in when clk_enable = '1' else '0';` 被放在 `else generate` 而不是独立成第三种「推荐」模式？

**参考答案**：因为它有毛刺风险，是「退而求其次」的兜底，不是推荐用法。源码注释明确标注「not recommended for Intel」。把它放在内层 else，语义是「内部门控启用、但又不是 Xilinx 时」才用它，避免它被当成默认安全选项。

---

## 5. 综合实践

把本讲三个模块串起来的综合任务：**为一个跨厂商项目设计「安全的复位 + 同步 + 门控时钟」配置，并用属性和 generate 守住设计意图。**

**任务背景**：假设你要在同一份 RTL 里同时支持 Xilinx 和 Intel 两个目标，且 SPI 模块需要：① 上电时复位可靠；② 一根来自快时钟域的 `start` 脉冲要安全跨到 SPI 慢时钟域；③ SPI 空闲时停时钟省功耗。

**操作步骤**：

1. **复位**：例化 [reset_on_startup](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd)。解释它为什么不需要你额外操心厂商——因为 `rst_delayed` 上**同时**挂了 `dont_touch`（[L30-L31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L30-L31)）和 `preserve`（[L34-L35](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L34-L35)），两家工具都会保留它。

2. **跨域脉冲**：用 `entity work.ff_synchroniser(xilinx_behavioural_ff_synchroniser)`（Xilinx）或 `entity work.ff_synchroniser(intel_behavioural_ff_synchroniser)`（Intel）例化，把 `start` 跨到 SPI 域。说明 Intel architecture 里 `preserve`（[L77-L79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L77-L79)）和 `altera_attribute` 的 SDC false_path（[L74](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L74)）分别守住了「链长度」和「时序不误报」两件事。

3. **门控 SPI 时钟**：用 4.3.4 的两段配置注释，分别为 Xilinx（BUFGCE）和 Intel（直通 + 寄存器使能）给出 `clock_enable` 的 generic 取值。

4. **画一张总图**：标出三个模块、各自用到的属性/原语，以及它们在两家厂商下的形态差异。

**预期结果**：你能用一张表说清——同一个 `entity`，Xilinx 走 BUFGCE + xpm_cdc_single + dont_touch；Intel 走直通 + 显式同步链(preserve+SDC) + preserve。端口连线不变，只换 architecture 名或 generic。**待本地综合/仿真验证**（Xilinx 用 Vivado，Intel 用 Quartus）。

## 6. 本讲小结

- **`preserve`(boolean, Intel) 与 `dont_touch`(string, Xilinx)** 都是「请勿优化」标签，类型不同是两家工具的历史约定；`reset_on_startup` 把两者挂在同一信号 `rst_delayed` 上，实现一份代码两家通吃。
- **`altera_attribute`** 是 Intel 的「赋值透传」通道，本库用它做两件事：`SYNCHRONIZER_IDENTIFICATION` 标记同步器、`SDC_STATEMENT` 内嵌 `set_false_path` 给亚稳态首级开绿灯。
- 同步链的设计意图是「长度如实保留」：`ff_synchroniser` 用 `preserve` 顶住 `src_reg`/`sync_chain`，用 SDC false_path 保护 `meta_stable_reg`（注意：源码里 `meta_stable_reg` 本身没有 `preserve`）。
- **`BUFGCE`** 是 Xilinx 全局网络上的无毛刺时钟门控，CE 由原语内部安全处理；手写 `clk <= clk_in when en else '0'` 是有毛刺风险的兜底。
- **Intel 不推荐直接门控时钟**，应改用 PLL 使能或寄存器使能；`clock_enable` 用「直通」分支（`ENABLE_INTERNAL_CLOCK_GATING=false`）承载这一哲学。
- **嵌套 `if generate`** 在 elaboration 期裁剪出唯一实现，是「多 architecture」之外另一种厂商适配手段；`clock_enable` 用两层嵌套切出 BUFGCE / 通用门控 / 直通三种模式。

## 7. 下一步学习建议

- 本讲只讲了**单比特**同步器。多比特信号如何安全跨域、以及它如何成为异步 FIFO 指针同步的基石，请继续学 **u8 单元（时钟域跨域同步器）**，特别是 `ff_synchroniser_vector`。
- 想看 `clock_enable` 真正在通信链路里怎么产生 SPI 时钟，可直接跳到 **u10-l2（SPI 发送 spi_tx）**，那里会用到本讲的门控模块。
- 想深入复位设计，可先读 **u4-l2（上电复位 reset_on_startup）**，它会把本讲的 `dont_touch`/`preserve` 放进完整的复位计数器流程里讲透。
- 如果对综合工具的优化行为（合并、retiming、复制）还不够熟，建议在 Vivado/Quartus 的综合报告里对照「有没有挂属性」两种结果，亲手观察差异。
