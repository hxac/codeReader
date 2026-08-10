# 时钟使能与门控 clock_enable

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `clock_enable` 用**两层 `if generate` 嵌套**在编译期裁剪出**三种**时钟处理实现（BUFGCE / 通用门控 / 直通）。
- 解释 Xilinx `BUFGCE` 为何能提供**无毛刺**的全局时钟门控，而手写的 `clk_out <= clk_in when en else '0'` 为何有毛刺风险。
- 说明 Intel/Altera 为何**不推荐直接门控时钟**，而要改用 PLL 使能引脚或寄存器使能引脚。
- 理解 `clock_enable` 展示了本库「同一实体多架构」模式之外的**另一种**等价的厂商适配手段——单一架构 + 编译期 `if generate` 选择。

## 2. 前置知识

### 2.1 什么是时钟门控（clock gating）

FPGA 里每个时序元件（触发器、RAM）都需要一个时钟。在很多场景下，我们希望**在不需要时让某部分电路的时钟停下来**，以省功耗或控制通信节拍（例如 SPI 只在传输期间才输出 `spi_clk`）。这种「按需接通/断开时钟」的技术就叫**时钟门控（clock gating）」。

直觉上最简单的写法是：

```vhdl
clk_out <= clk_in when clk_enable = '1' else '0';
```

`clk_enable` 为 1 时 `clk_out` 跟随 `clk_in`，为 0 时 `clk_out` 恒为 0。问题是：`clk_enable` 与 `clk_in` 通常**没有固定相位关系**，如果 `clk_enable` 恰好在 `clk_in` 的边沿附近翻转，输出就可能产生一个**不完整的窄脉冲（runt pulse / 毛刺）」，被下游触发器当成一次假时钟沿，造成功能错误。

### 2.2 三种规避毛刺的思路

| 思路 | 做法 | 典型厂商 |
| --- | --- | --- |
| 全局缓冲器门控 | 用专用原语在全局时钟网络上安全地启停时钟，内部保证只在完整周期边界切换 | Xilinx `BUFGCE` |
| 直接组合门控 | 用一个 LUT/多路选择器在 `clk_in` 与 `'0'` 间选择（有毛刺风险） | 通用 / 不推荐用于 Intel |
| 寄存器使能 | 时钟永远不停，改给每个寄存器加 `clock enable` 引脚，不使能时寄存器保持原值 | Intel/Altera 推荐 |

### 2.3 与前序讲义的衔接

- 本讲是「时钟生成与门控」单元的第一讲。PLL（倍频/分频产生新时钟）在下一讲 [u5-l2](u5-l2-pll-clock-generation.md) 讲，本讲只讲「如何门控已有时钟」。
- [u2-l1（同一实体多架构模式）](u2-l1-multi-architecture-pattern.md) 讲过：本库通过 `xilinx_behavioural_*` / `intel_behavioural_*` / `own_behavioural_*` **多套 architecture** 来适配厂商。`clock_enable` 用的是**另一条等价路径**——只有**一套 `behavioural` 架构**，靠架构**内部**的 `if generate` 在编译期选择实现。这两种手段殊途同归：换厂商时都不用改端口连线，只改一个 generic。
- [u2-l2（厂商仿真库）](u2-l2-vendor-simulation-libraries.md) 讲过 `unisim` 是 Xilinx 的硬件原语行为模型库。本讲的 `BUFGCE` 正是 `unisim` 里的原语。

## 3. 本讲源码地图

| 文件 | 作用 | 是否有独立测试台 |
| --- | --- | --- |
| [ip/clock_enable/clock_enable.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd) | 本讲主角：时钟使能/门控模块，单一 `behavioural` 架构，含三种 `if generate` 分支 | 否（靠 `spi_tx` 间接覆盖） |
| [ip/communication/spi/spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd) | `clock_enable` 唯一的真实例化点：在 SPI 主模式下用它门控 `spi_clk_out` | 是（`tb_spi_tx`） |
| [ip/communication/spi/tb/tb_spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd) | 间接观察 `clock_enable` 行为的入口：例化 `spi_tx` 时给定门控 generic 的取值 | — |

> 提示：`clock_enable` 没有专属的 `tb_clock_enable.vhd`（参见 [u1-l2](u1-l2-directory-structure.md) 中关于「无独立测试台、靠使用方间接覆盖」的说明）。因此本讲的「运行型」实践会借助 `tb_spi_tx` 来观察行为。

---

## 4. 核心概念与源码讲解

### 4.1 clock_enable 模块：两个 generic 如何裁剪出三种实现

#### 4.1.1 概念说明

`clock_enable` 的实体声明只有一个端口三元组：`clk_in`（输入时钟）、`clk_enable`（使能信号）、`clk_out`（输出时钟）。它**没有复位、没有数据通路**，唯一的职责就是决定 `clk_out` 如何从 `clk_in` 派生出来。

决定派生方式的是两个 **boolean generic**：

- `ENABLE_INTERNAL_CLOCK_GATING`（默认 `true`）：是否启用「内部门控」。为 `false` 时进入直通模式。
- `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL`（默认 `false`）：在内部门控启用时，是否使用 Xilinx 专用的 `BUFGCE` 原语。

> 命名小提示：`..._AND_NOT_INTERNAL` 这种冗长后缀是作者刻意为之，读作「用 Xilinx 门控、**而不用** internal（通用）门控」。把布尔量的「真」含义直接写进名字，能减少误配。

这两个 generic 的组合并不等于 4 种实现——因为当 `ENABLE_INTERNAL_CLOCK_GATING = false` 时，外层分支直接走直通，`USE_XILINX_CLK_GATE_AND_NOT_INTERNAL` 的取值被**完全忽略**。所以实际只有 **3 种**有效结果。

#### 4.1.2 核心流程

源码用「外层 `if generate` + 内层 `if/else generate`」的两层嵌套实现三分支选择：

```
if ENABLE_INTERNAL_CLOCK_GATING generate          -- 外层：是否门控
    if USE_XILINX_CLK_GATE_AND_NOT_INTERNAL generate   -- 内层：用哪种门控
        分支 A: 例化 BUFGCE          (Xilinx 全局, 无毛刺)
    else generate
        分支 B: clk_out <= clk_in when en else '0'  (通用组合门控, 有毛刺风险)
    end generate;
else generate
    分支 C: clk_out <= clk_in        (直通, 不门控)
end generate;
```

对应的真值表如下（共 4 种 generic 组合，但只有 3 种结果）：

| `ENABLE_INTERNAL_CLOCK_GATING` | `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL` | 结果分支 | 适用场景 |
| :---: | :---: | --- | --- |
| `true`  | `true`  | A：BUFGCE | Xilinx，时钟走全局网络 |
| `true`  | `false` | B：组合门控 | 通用 / 局部时钟网络（不推荐 Intel） |
| `false` | `true`  | C：直通 | （`USE_XILINX...` 被忽略）Intel 推荐走法 |
| `false` | `false` | C：直通 | Intel 推荐走法 |

`if generate` 是 **elaboration 期（编译期）」求值的：综合工具根据两个 generic 的常量值，只把命中分支的电路留下、其余分支**整段丢弃**。最终落进芯片的永远是**唯一一种**实现，没有冗余逻辑，也不会在运行时切换。

> 与「多 architecture」的对比：[u2-l1](u2-l1-multi-architecture-pattern.md) 的做法是「一个 entity 多套 architecture，例化时用 `entity work.xxx(arch_name)` 选一套」。`clock_enable` 的做法是「一套 architecture，内部用 `if generate` 选一段」。两者都是**编译期二选一/三选一**，区别只在写法风格：多 architecture 把厂商库声明局部化到各自架构前；而 `clock_enable` 只有一套架构，于是 `library unisim` 直接写在文件顶部。

#### 4.1.3 源码精读

实体声明——两个 generic 加三端口，极简（[clock_enable.vhd:25-40](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L25-L40)，这段代码声明了端口契约与两个布尔 generic）：

```vhdl
entity clock_enable is
    generic (
        ENABLE_INTERNAL_CLOCK_GATING: boolean := true;
        USE_XILINX_CLK_GATE_AND_NOT_INTERNAL: boolean := false
    );
    port (
        clk_in: in std_ulogic;
        clk_enable: in std_ulogic;
        clk_out: out std_ulogic
    );
end entity;
```

整个架构体只有一段 `if generate` 结构（[clock_enable.vhd:42-62](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L42-L62)，这里用两层嵌套 `if generate` 把三种实现写在一起）：

```vhdl
architecture behavioural of clock_enable is
begin
    clk_gating: if ENABLE_INTERNAL_CLOCK_GATING generate
        xilinx_clk_gate: if USE_XILINX_CLK_GATE_AND_NOT_INTERNAL generate
            BUFGCE_inst: BUFGCE
                port map ( O => clk_out, CE => clk_enable, I => clk_in );
        else generate
            clk_out <= clk_in when clk_enable = '1' else '0';
        end generate;
    else generate
        clk_out <= clk_in; -- Clock passes through, rely on enable pins at registers instead
    end generate;
end architecture;
```

读法要点：

- 外层标号 `clk_gating`（[clock_enable.vhd:45](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L45)）对应 `ENABLE_INTERNAL_CLOCK_GATING`。
- 内层标号 `xilinx_clk_gate`（[clock_enable.vhd:47](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L47)）对应 `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL`，内层的 `else generate`（[clock_enable.vhd:55-57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L55-L57)）是通用组合门控。
- 外层的 `else generate`（[clock_enable.vhd:59-61](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L59-L61)）是直通分支，注释明确写出设计意图：时钟直通，把「不工作」的控制在寄存器使能引脚上完成。

文件顶部的厂商库声明（[clock_enable.vhd:22-23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L22-L23)，这行为 `BUFGCE` 提供可见性）：

```vhdl
library unisim;
use unisim.vcomponents.all;
```

注意：即便你选了直通模式（`ENABLE_INTERNAL_CLOCK_GATING = false`），`BUFGCE` 从不被例化，这行 `library unisim` 也依然在文件顶部——它无害，但需要仿真器/综合器能找到 `unisim` 库（参见 [u2-l2](u2-l2-vendor-simulation-libraries.md) 关于厂商仿真库供给的讨论，以及 README 中 `use_xilinx_libs` 解决 `glbl.GSR` 报错的部分）。

文件头的注释块（[clock_enable.vhd:5-14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L5-L14)）已经把两家厂商的门控策略差异写得很清楚，是本讲最重要的「设计理由」原始依据，建议逐条对照阅读。

#### 4.1.4 代码实践

**实践目标**：亲手验证「两个 generic → 三种结果」的真值表，并观察 `tb_spi_tx` 里实际选中的是哪一支。

**操作步骤**：

1. 打开 [ip/communication/spi/tb/tb_spi_tx.vhd:341-344](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L341-L344)，找到 `spi_tx` 的例化处的 generic 映射：
   - `ENABLE_INTERNAL_CLOCK_GATING => true`
   - `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL => false`
2. 对照本讲 4.1.2 的真值表，确认这对应**分支 B（通用组合门控）**——也就是 `tb_spi_tx` 默认并未走 Xilinx `BUFGCE`。
3. 按 [u1-l3](u1-l3-environment-and-simulation.md) 的步骤运行一次 `test_runner.py`，让 `tb_spi_tx` 通过。

**需要观察的现象**：仿真通过说明分支 B 在行为级上是自洽的（行为级仿真看不出毛刺，因为仿真器按事件精确推进，不会自发产生组合冒险）。

**预期结果**：`tb_spi_tx` 全部用例 pass。毛刺风险是**真实硅片 + 综合后布线延迟**才暴露的问题，行为级仿真无法复现——这一点本身就是一个重要认知。

> 若本地无法运行仿真（缺少 VUnit / NVC 环境），可标注「待本地验证」并改为纯阅读型实践：在源码里把三个分支各自对应的 generic 组合写在注释里，完成 4.2.4 的电路图练习即可。

#### 4.1.5 小练习与答案

**练习 1**：如果用户把 `ENABLE_INTERNAL_CLOCK_GATING` 设成 `false`、却同时把 `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL` 设成 `true`，综合后芯片里会有 `BUFGCE` 吗？

**参考答案**：不会。外层 `clk_gating` 的 `if generate` 条件为假，整个含 `BUFGCE` 的内层块在 elaboration 期被丢弃，直接走外层 `else generate` 的直通分支。这正是真值表第 3 行的情况——`USE_XILINX...` 的取值被忽略。

**练习 2**：为什么作者把 generic 取值的「真/假」语义直接编码进名字（`..._AND_NOT_INTERNAL`），而不是只叫 `USE_XILINX_CLK_GATE`？

**参考答案**：名字里带 `AND_NOT_INTERNAL` 明确表达了「选了 Xilinx 门控就**不要**用通用 internal 门控」的互斥意图，降低误配概率；读代码的人一眼就知道这两个 generic 在内层是二选一关系。

---

### 4.2 BUFGCE：无毛刺的全局时钟门控原语

#### 4.2.1 概念说明

`BUFGCE` 是 Xilinx `unisim` 库里的原语，全称可理解为「带时钟使能的全局缓冲器（Clock Buffer with Enable）」。它的作用有两个：

1. **全局时钟驱动**：把时钟送上 FPGA 的全局时钟网络（低偏移、高扇出），就像 [u5-l2](u5-l2-pll-clock-generation.md) 里 `PLL` 输出后面常跟一个 `BUFG` 一样。
2. **安全门控**：原语**内部**保证只在时钟的完整周期边界（下降沿）才真正启停输出，因此不会产生半截脉冲。

直觉上，你可以把它想成一个「懂礼貌」的开关：普通的 `when en else '0'` 开关可能在时钟跳到一半时被你掰下去，砍出一个半高脉冲；而 `BUFGCE` 会等当前这个完整周期走完，才在下一个干净的时刻断开。

#### 4.2.2 核心流程

`BUFGCE` 的端口极其简单：

| 端口 | 方向 | 含义 |
| --- | --- | --- |
| `I`  | in  | 输入时钟 |
| `CE` | in  | 时钟使能（高有效） |
| `O`  | out | 输出时钟 |

行为可概括为：

```
当 CE=1：O 跟随 I（输出完整时钟）
当 CE=0：O 停在固定电平（通常是低），且切断动作发生在 I 的下降沿之后
```

关键在于「切断/接通发生在周期边界」。可用一个简化时序示意（`↓` 表示下降沿，切断只在此处生效）：

```
I   : ‾‾‾‾‾‾\_/‾‾‾‾‾‾\_/‾‾‾‾‾‾\_/‾‾‾‾‾‾
CE  : ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\_______________
O   : ‾‾‾‾‾‾\_/‾‾‾‾‾‾\________________   <- CE 下降后,O 在最近的 ↓ 之后干净停住
```

对照分支 B 的组合门控，CE 的下降沿与 `I` 没有任何对齐保证，因此 `O` 可能在 `I` 的高电平中途被切断，留下一个窄尖峰。

#### 4.2.3 源码精读

`BUFGCE` 的例化只出现在分支 A（[clock_enable.vhd:47-53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L47-L53)，这里把模块端口直接映射到原语端口）：

```vhdl
xilinx_clk_gate: if USE_XILINX_CLK_GATE_AND_NOT_INTERNAL generate
    BUFGCE_inst: BUFGCE
        port map (
            O => clk_out,
            CE => clk_enable,
            I => clk_in
        );
```

三处映射一目了然：模块的 `clk_in` → 原语 `I`，`clk_enable` → 原语 `CE`，原语 `O` → 模块 `clk_out`。模块本身不写任何「无毛刺」逻辑——这件事完全交给 `BUFGCE` 原语的硅片实现，这正是「点菜/上菜」分工（参见 [u2-l2](u2-l2-vendor-simulation-libraries.md)）：RTL 只例化原语，无毛刺行为由 `unisim` 仿真模型 / Vivado 综合知识提供。

#### 4.2.4 代码实践

**实践目标**：把三种实现分支各自画成一张「等效电路图」，建立从 RTL 文字到电路结构的直觉。

**操作步骤**：

1. 取一张纸或打开任意画图工具，分别画出下面三种 generic 组合的等效电路。
2. 对照本讲源码确认每个图的输入/输出连线。

**需要画的三张图（文字描述版）**：

- **分支 A（`ENABLE=true, USE_XILINX=true`）**：`clk_in` → `[BUFGCE: I, CE=clk_enable]` → `O` → `clk_out`。一个带使能的全局缓冲器盒子。
- **分支 B（`ENABLE=true, USE_XILINX=false`）**：一个 2 选 1 多路选择器，选择端接 `clk_enable`：`clk_enable=1` 选 `clk_in`，`clk_enable=0` 选 `'0'`，输出接 `clk_out`。（这个 mux 通常被综合进一个 LUT，正是毛刺来源。）
- **分支 C（`ENABLE=false`）**：一根直通线 `clk_in` → `clk_out`，`clk_enable` 引脚悬空不接。

**预期结果**：三张图里只有分支 A 含专用时钟原语；分支 B 是纯组合逻辑；分支 C 是连线。这正是「同一端口契约、三种实现」的可视化。

#### 4.2.5 小练习与答案

**练习 1**：分支 B 的 `clk_out <= clk_in when clk_enable = '1' else '0'`，在综合后通常被映射成什么器件？为什么它有毛刺风险？

**参考答案**：通常映射成一个 LUT 实现的 2 选 1 多路选择器（`clk_enable` 作选择端，`clk_in` 与 `'0'` 作数据端）。因为 `clk_enable` 与 `clk_in` 无固定相位关系，且 LUT 的选择端到输出存在布线延迟，`clk_enable` 翻转瞬间可能在输出端产生窄尖峰。

**练习 2**：假如你在一颗 Xilinx FPGA 上，`clk_in` 已经走过全局网络、你只想「关掉它一段时间」，应该选哪个分支？为什么？

**参考答案**：选分支 A（`BUFGCE`）。它既能保持全局网络驱动，又能在周期边界安全切断，无毛刺。分支 B 有毛刺风险；分支 C 根本不门控。

---

### 4.3 真实用法：在 spi_tx 中门控 SPI 时钟

#### 4.3.1 概念说明

`clock_enable` 在本库中只有**一个**真实例化点：SPI 发送器 `spi_tx` 的主模式（controller）下，用它来生成 `spi_clk_out`。SPI 是主从协议，主机的 `spi_clk` 只在**传输进行期间**才应该翻转；传输结束后 `spi_clk` 应停止，从机据此知道一次传输已结束。

`spi_tx` 把 `clock_enable` 的两个 generic **原样透传**给顶层使用方，让用户根据目标 FPGA（Xilinx / Intel）自行决定门控策略——这是本库「把厂商决策留给顶层」的一贯风格。

#### 4.3.2 核心流程

在 `spi_tx` 内部，SPI 时钟的产生流程是：

```
1. CONTROLLER_AND_NOT_PERIPHERAL = true 时才例化 clock_enable（从机模式不产生时钟）
2. clk_in  <= spi_clk_in        （SPI 系统时钟,持续运行）
3. clk_enable <= not spi_chip_select_n   （片选有效(低)期间才放时钟）
4. clk_out => spi_clk_out       （真正送到总线上的 SPI 时钟）
```

关键点：`clk_enable` 接的是 `not spi_chip_select_n`。SPI 片选是**低有效**，所以「片选拉低 = 正在通信 = 放出时钟」；「片选拉高 = 通信结束 = 时钟停止」。这样 `clock_enable` 就把「SPI 只在传输期间翻转时钟」这个语义实现了。

#### 4.3.3 源码精读

`clock_enable` 的例化在 `spi_tx` 架构体的末尾（[spi_tx.vhd:159-180](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L159-L180)，整段被 `if CONTROLLER_AND_NOT_PERIPHERAL generate` 包裹，主机模式才生成时钟）：

```vhdl
spi_clk_driver: if CONTROLLER_AND_NOT_PERIPHERAL generate
    spi_clk_enable_inst: entity work.clock_enable
        generic map (
            ENABLE_INTERNAL_CLOCK_GATING => ENABLE_INTERNAL_CLOCK_GATING,
            USE_XILINX_CLK_GATE_AND_NOT_INTERNAL => USE_XILINX_CLK_GATE_AND_NOT_INTERNAL
        )
        port map (
            clk_in => spi_clk_in,
            clk_enable => not spi_chip_select_n,
            clk_out => spi_clk_out
        );
else generate
    spi_clk_out <= '-';   -- 从机模式: SPI 时钟由外部主机提供, 本模块输出无所谓
end generate;
```

要点：

- 两个门控 generic 从 `spi_tx` 实体（[spi_tx.vhd 的 generic 区](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L38-L48)）原样透传，使用方只需在顶层改一次。
- `clk_enable => not spi_chip_select_n`（[spi_tx.vhd:175](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L175)）是把「传输进行中」语义转成「时钟放行」的连接。
- 从机模式（`else generate`）里 `spi_clk_out <= '-'`，意为「不关心/高阻态」——SPI 从机本就不产生时钟，这条线由外部主机驱动。

#### 4.3.4 代码实践

**实践目标**：写一段**示例 RTL**，用 `clock_enable` 给一个最小 SPI 主机产生受控的 SPI 时钟，并理解「占空比受控」的真实含义。

> 先澄清一个易混淆点：`clock_enable` 是一个**纯门控（开/关）」模块，它不会去调制单个时钟脉冲的占空比（高电平时间 / 周期）。它控制的是「**在哪段时间窗口内允许 `clk_in` 通过**」。如果你需要改变单个脉冲的占空比，那要用 [u5-l2 的 PLL](u5-l2-pll-clock-generation.md)。下面这段示例展示的是「传输期间放行、传输结束后停住」的受控时钟。

**操作步骤（示例代码，非项目原有）**：

```vhdl
-- 示例代码：用 clock_enable 产生受传输窗口控制的 SPI 时钟
-- （仅为说明用法，不是项目里的真实文件）

spi_clk_gate: entity work.clock_enable
    generic map (
        ENABLE_INTERNAL_CLOCK_GATING      => true,   -- Xilinx 全局网络用 BUFGCE 时改成 true/true
        USE_XILINX_CLK_GATE_AND_NOT_INTERNAL => false
    )
    port map (
        clk_in     => system_clk,        -- 持续运行的系统时钟
        clk_enable => transfer_active,   -- 传输进行中为 '1'，否则为 '0'
        clk_out    => spi_sclk           -- 只在 transfer_active 期间翻转的 SPI 时钟
    );
```

**需要观察的现象**（结合 `tb_spi_tx`）：

1. 一次传输开始（片选拉低）→ `spi_clk_out` 开始翻转；传输结束（片选拉高）→ `spi_clk_out` 停止。
2. 默认 `tb_spi_tx` 用的是分支 B（`ENABLE=true, USE_XILINX=false`），所以 `spi_clk_out` 是「`system_clk` 与 `'0'` 二选一」的波形。

**预期结果**：在 `tb_spi_tx` 的波形里，`spi_clk_out` 的翻转区间与 `spi_chip_select_n = '0'` 的区间严格对齐。这就是 `clock_enable` 在 SPI 中的全部职责——**用门控把「时钟是否翻转」与「是否在传输」绑定**。

> 关于「占空比」：若你的目标是让 SPI 时钟本身具有非 50% 占空比，正确做法是调 PLL 的输出占空比参数（见下一讲），而非用 `clock_enable`。`clock_enable` 控制的是时钟的**有无**，不是单脉冲的**宽窄**。

**若无法运行仿真**：标注「待本地验证」，改为阅读型实践——在 `spi_tx.vhd` 里确认 `clk_enable => not spi_chip_select_n` 这行，并解释为什么从机模式的 `else generate` 把 `spi_clk_out` 赋成 `'-'`。

#### 4.3.5 小练习与答案

**练习 1**：在 `spi_tx` 中，为什么 `clk_enable` 接的是 `not spi_chip_select_n` 而不是 `spi_chip_select_n`？

**参考答案**：SPI 片选 `spi_chip_select_n` 是低有效——「拉低」表示选中/正在通信。而 `clock_enable` 的 `CE` 是高有效（`CE=1` 放行时钟）。所以需要取反：片选拉低（通信中）→ `not spi_chip_select_n = 1` → 放行 SPI 时钟。

**练习 2**：如果你把这块设计移植到 Intel FPGA，按文件头注释的建议，两个 generic 应该怎么设？`spi_clk_out` 还会翻转吗？

**参考答案**：设 `ENABLE_INTERNAL_CLOCK_GATING => false`（`USE_XILINX...` 随意，通常设 `false`）。此时 `clock_enable` 走直通分支，`clk_out` 恒等于 `clk_in`，**`spi_clk_out` 会一直翻转、不会在传输结束后停住**。因此 Intel 方案下不能靠这个模块停时钟，而要改用寄存器使能——SPI 移位寄存器加一个 `transfer_active` 使能引脚，时钟常转、寄存器在非传输期保持原值。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个贯通任务：

**任务**：你为一个跨厂商产品维护 SPI 主机，需要给三家目标板配置 `clock_enable`。

1. **板 A（Xilinx，SPI 时钟走全局网络）**：写出 `clock_enable` 的 generic 配置，画出等效电路，并说明为什么这是三家里的最优解。
2. **板 B（Xilinx，但 SPI 时钟只在本地的少量寄存器用，不走全局网络）**：写出 generic 配置，并解释为何此时作者允许 `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL => false`。
3. **板 C（Intel）**：写出 generic 配置，说明 `spi_clk_out` 在该配置下的行为，并指出你还需要在 SPI 移位寄存器上额外做什么才能达到「非传输期不翻转有效数据」的效果。

**交付物**：

- 三张等效电路图（可手画）。
- 三组 generic 取值。
- 一段话：总结「`if generate` 编译期裁剪」与 [u2-l1](u2-l1-multi-architecture-pattern.md)「多 architecture」两种厂商适配手段的异同。

**检查清单**：

- [ ] 板 A 用了 `BUFGCE`，且能解释无毛刺。
- [ ] 板 B 用了组合门控，且能说出它与板 A 的差异是「不走全局网络所以不必非用 `BUFGCE`」。
- [ ] 板 C 走直通，且能说出 Intel 下真正「停」的是寄存器使能而非时钟。
- [ ] 能说出两种适配手段的共同点（都靠 generic、都在编译期二选一/三选一、都不改端口连线）与差异（多 architecture 把库声明局部化；`clock_enable` 单架构 + 顶部统一声明 `unisim`）。

## 6. 本讲小结

- `clock_enable` 用**两层 `if generate` 嵌套**（外层 `ENABLE_INTERNAL_CLOCK_GATING`、内层 `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL`）在编译期裁剪出**三种**实现：`BUFGCE`、组合门控、直通。
- 4 种 generic 组合只产生 3 种结果：当 `ENABLE_INTERNAL_CLOCK_GATING = false` 时 `USE_XILINX...` 被忽略，恒走直通。
- `BUFGCE` 是 Xilinx 全局缓冲器，**只在完整周期边界启停**，无毛刺；手写 `clk_out <= clk_in when en else '0'` 是组合多路选择器，有毛刺风险。
- Intel/Altera 不推荐直接门控时钟，应改用 PLL 使能引脚或**寄存器使能引脚**（时钟常转、寄存器按需保持）。
- `clock_enable` 是本库「多 architecture」模式之外的**另一种**等价厂商适配手段：单一 `behavioural` 架构 + 内部 `if generate` 选择。
- 真实例化点只有一处：`spi_tx` 主模式下，用 `not spi_chip_select_n` 作 `clk_enable`，实现「只在传输期间放行 SPI 时钟」。

## 7. 下一步学习建议

- **下一讲 [u5-l2 PLL 时钟生成](u5-l2-pll-clock-generation.md)**：当你需要的不是「开关时钟」而是「产生不同频率/相位的新时钟」时，就要用 PLL。届时你会看到 `PLLE2_BASE`（Xilinx）与 `altclklock`（Intel）两套厂商实现，以及为什么 PLL 是全库**唯一没有 `own_behavioural_*` 行为级实现**的 IP（因而被 CI 排除）。
- **横向对比**：回头重读 [u2-l1（多架构模式）](u2-l1-multi-architecture-pattern.md) 与本讲的 `if generate`，体会两种厂商适配手段的取舍。
- **延伸阅读**：在 `spi_tx.vhd` 里继续往下看 `case generate` 如何针对四种 SPI 模式（CPOL/CPHA）对齐串行数据与片选时序，那是 [u10-l2（SPI 发送）](u10-l2-spi-tx.md) 的主题。
