# 上电复位 reset_on_startup

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「上电复位」要解决什么问题，以及为什么 FPGA/ASIC 上电后需要一段确定长度的强制复位窗口。
- 读懂 `reset_on_startup` 这一个完整的小模块：启动计数器、一拍延迟直通、双极性输出合并。
- 解释 `startup_counter` 如何用 `subtype'high` 自动适配 `RESET_TIME_IN_CLK_CYCLES` 这个 generic。
- 说明 `rst_delayed` 为什么是一拍延迟的「直通」寄存器，以及它和启动复位如何通过一个组合逻辑多路选择合并成 `rst_out`。
- 看懂 `dont_touch`（Xilinx）与 `preserve`（Intel）两个综合属性为何同时挂在同一个信号上，以及它们如何在不使用「多 architecture」的前提下实现跨厂商兼容。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

**第一，什么是复位，为什么有「极性」。** 数字电路里，寄存器（flip-flop）上电时的值是不确定的。为了让整个系统从一个已知状态开始工作，我们用一个 `reset` 信号把所有寄存器强行置成初值。这个复位信号「有效」时的电平，不同项目约定不同：有的约定低电平有效（`0` 表示「正在复位」，记作 active-low），有的约定高电平有效（`1` 表示「正在复位」，记作 active-high）。这就是「极性」（polarity）。本模块用一个 generic `RESET_POLARITY` 把极性变成可配置项，同一份代码两种极性都能用。

**第二，为什么需要「上电复位」这一段额外窗口。** 即便外部电源已经稳定、晶振已经开始抖动，FPGA 内部的全局复位网络、PLL 的 `locked` 信号、各模块的初始值，都需要若干个时钟周期才能「就位」。如果在第 0 拍就让系统自由运行，可能会有一批寄存器在复位网络还没铺开时就已被时钟采样，导致状态机跑飞。解决办法很简单：上电后先强制保持复位状态若干拍（一个可配置的窗口），等一切都稳定了再释放。这正是 `reset_on_startup` 的本职工作。

**第三，为什么复位信号要「防优化」。** 综合工具（Vivado / Quartus）会主动删除、合并它认为「冗余」的寄存器。本模块里有一个「直通」寄存器 `rst_delayed <= rst_in`，它只是把输入复位延迟一拍——综合工具很容易判定它冗余、把它吸收掉，从而破坏设计者想要的复位树拓扑。所以源码用综合属性（`dont_touch` / `preserve`）明确告诉工具「这个寄存器别动」。这一点在 [u2-l3 综合属性、防优化与时钟门控策略](u2-l3-synthesis-attributes-clock-gating.md) 里已建立概念，本讲把它落到 `reset_on_startup` 的具体源码上。

> 与 u2 的衔接：u2 讲的是「同一 entity 多 architecture」的跨厂商模式（Xilinx / Intel / 自研各一套 architecture）。本模块**不走那条路**——它只有一套 `behavioural` 架构，跨厂商兼容完全靠「同一个信号上同时挂两个厂商属性」实现。这是本库里的另一种、更轻量的跨厂商手法，值得对照理解。

## 3. 本讲源码地图

本讲只涉及一个 IP，两个文件（加一个波形脚本）：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| `ip/reset_on_startup/reset_on_startup.vhd` | 设计源码（可综合） | 上电复位控制器的全部逻辑：启动计数器 + 一拍延迟直通 + 双极性合并 + 防优化属性。 |
| `ip/reset_on_startup/tb/tb_reset_on_startup.vhd` | 测试台（仅仿真） | 用 VUnit 写的验证：同时例化 active-low 与 active-high 两个 DUT，验证启动窗口长度、释放后的跟随行为和一拍延迟。 |
| `ip/reset_on_startup/tb/tb_reset_on_startup.do` | 波形脚本（ModelSim/QuestaSim Tcl） | 把信号按「接口 / 内部 / 测试台」分组，方便在波形窗口里观察 `startup_counter`、`rst_delayed`、`reset_on_startup` 的逐拍变化。 |

按 [u1-l2](u1-l2-directory-structure.md) 讲过的三件套约定，设计源码在 IP 目录根，测试台与波形脚本在 `tb/` 子目录且加 `tb_` 前缀，`test_runner.py` 凭 `tb_*.vhd` 自动发现它。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：模块整体、启动计数器与极性、一拍延迟直通与防优化属性。

### 4.1 reset_on_startup：上电复位控制器整体

#### 4.1.1 概念说明

`reset_on_startup` 是一个「上电复位控制器」。它做两件事：

1. **上电窗口**：上电后的一段可配置时长内，无论外部 `rst_in` 是什么，都强制输出有效复位。
2. **复位直通**：上电窗口结束之后，把外部 `rst_in`（延迟一拍）转发到 `rst_out`，让外部复位按钮或看门狗复位能继续生效。

换句话说，`rst_out` 同时受两个来源驱动：「内部启动复位」和「外部输入复位（延迟一拍）」。两者只要有一个有效，输出就有效。这是一个典型的「复位源聚合」。

#### 4.1.2 核心流程

把模块看成三块：

```text
          ┌──────────────────────┐
rst_in ──▶│  一拍延迟直通         │──▶ rst_delayed ──┐
          │  rst_delayed<=rst_in │                  │
          └──────────────────────┘                  │
                                                    ▼
          ┌──────────────────────┐           ┌──────────┐
clk  ────▶│  启动计数器           │──▶ reset_ │  OR 合并  │──▶ rst_out
          │  计满 RESET_TIME_... │   on_     │ (按极性)  │
          │  后释放               │   startup └──────────┘
          └──────────────────────┘
```

- 启动计数器每个时钟沿 `+1`，计到 `RESET_TIME_IN_CLK_CYCLES` 后把内部 `reset_on_startup` 信号翻转为「无效」。
- `rst_delayed` 是 `rst_in` 的寄存器版本（延迟一拍）。
- `rst_out` 是组合逻辑：当 `rst_delayed` 或 `reset_on_startup` 任一为有效极性时，输出有效；否则输出无效。

#### 4.1.3 源码精读

先看实体与 generic，这是整个模块的「契约」：

[reset_on_startup.vhd:L12-L22](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L12-L22) — 声明了两个 generic（复位窗口长度 `RESET_TIME_IN_CLK_CYCLES`，默认 2；复位极性 `RESET_POLARITY`，默认 `'0'` 即低有效）和三个端口（`clk` / `rst_in` / `rst_out`）。

注意 `RESET_TIME_IN_CLK_CYCLES` 类型是 `positive`，意味着最小值是 1——至少复位 1 拍，不允许写 0。`RESET_POLARITY` 的注释把两种取值的含义写得很清楚：`'0'` 是 active low，`'1'` 是 active high。

再看输出的合并逻辑，这一行是整个模块行为的「总开关」：

[reset_on_startup.vhd:L39-L40](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L39-L40) — `rst_out` 是一条**并发条件信号赋值**（组合逻辑，不是寄存器）。当 `rst_delayed` 或 `reset_on_startup` 等于 `RESET_POLARITY`（即「有效」）时，输出 `RESET_POLARITY`；否则输出 `not RESET_POLARITY`（即「无效」）。

这里有一个关键细节：**`rst_out` 本身没有寄存器**，它纯粹是两个内部信号的组合函数。所谓「一拍延迟」并不来自 `rst_out`，而来自 `rst_delayed` 这个寄存器。理解这一点对后面读测试台的断言至关重要。

整个时序逻辑只有一个进程 `reset_controller`，它同时管 `rst_delayed` 和启动计数器，下一节细看。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：在脑子里把模块的「三块结构」和源码行号对上号。

**操作步骤**：

1. 打开 `reset_on_startup.vhd`，用三个颜色的笔/标记标注：
   - 「启动计数器」相关行（变量声明、自增、释放赋值）。
   - 「一拍延迟直通」相关行（`rst_delayed <= rst_in`）。
   - 「输出合并」相关行（`rst_out <= ... when ...`）。
2. 打开波形脚本 [tb_reset_on_startup.do:L3-L14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.do#L3-L14)，观察它是如何用 `add wave -divider` 把信号分成 `Interface`（`clk` / `rst_in` / `rst_out`）、`Internal`（`reset_on_startup` / `rst_delayed` / `startup_counter`）、`tb` 三组的——这与「三块结构」完全对应。

**需要观察的现象**：波形脚本里 `startup_counter` 被标注了 `-radix unsigned`，说明它被当成无符号整数显示；你能直接看到它从 0 数到上限的过程。

**预期结果**：三块结构与源码行号一一对应，无遗漏、无多余。

#### 4.1.5 小练习与答案

**练习 1**：`rst_out` 是寄存器输出还是组合输出？这意味着它的延迟特性由什么决定？

> **答案**：`rst_out` 是组合输出（并发赋值，不在时钟进程里）。它本身不引入时钟延迟；模块对 `rst_in` 的一拍响应延迟完全来自 `rst_delayed` 这个寄存器。

**练习 2**：`RESET_TIME_IN_CLK_CYCLES` 为什么用 `positive` 而不是 `natural`？

> **答案**：`positive` 是 `natural` 的子类型，范围从 1 开始，排除了 0。这从类型层面保证「复位窗口至少 1 拍」，避免用户误写 0 导致启动计数器逻辑退化为永远不复位。

---

### 4.2 启动计数器与复位极性（startup_counter / RESET_POLARITY）

#### 4.2.1 概念说明

这一块解决「上电窗口有多长」和「有效电平是高还是低」两件事，分别由 `RESET_TIME_IN_CLK_CYCLES` 和 `RESET_POLARITY` 两个 generic 控制。

核心设计巧思在于：**极性不是用 `if-else` 分支写死的，而是被抽象成一个统一的「等于 `RESET_POLARITY` 即为有效」的判定**。信号初值、释放赋值、输出合并，全都用 `RESET_POLARITY` / `not RESET_POLARITY` 表达，于是同一份代码天然支持两种极性，没有任何分支冗余。

而「窗口长度」则由一个 saturating 计数器实现：它从 0 数到一个上限就停住，到达上限的那一拍把内部启动复位信号释放。

#### 4.2.2 核心流程

启动计数器是一个 saturating（饱和）计数器：

```text
每个 clk 上升沿:
    若 startup_counter < 上限:
        startup_counter := startup_counter + 1
    若 startup_counter == 上限:        # 已经数到了
        reset_on_startup <= 无效极性   # 释放启动复位（下一拍生效）
```

设 \(N = \text{RESET\_TIME\_IN\_CLK\_CYCLES}\)，计数器从 0 数到 \(N\) 需要经历 \(N\) 个上升沿。在这 \(N\) 拍内 `reset_on_startup` 保持有效，`rst_out` 因此保持有效；第 \(N\) 个上升沿把 `reset_on_startup` 翻转为无效（信号赋值下一拍生效），此后 `rst_out` 是否有效就只看 `rst_delayed`（即外部 `rst_in`）了。

时钟周期 \(T_{\text{clk}}\) 已知时，复位窗口的物理时长为：

\[
T_{\text{reset}} = N \cdot T_{\text{clk}} = \frac{N}{f_{\text{clk}}}
\]

#### 4.2.3 源码精读

先看两个内部信号的初值——它们都用 `RESET_POLARITY` 初始化为「有效」，这正是「上电即复位」的来源：

[reset_on_startup.vhd:L25-L26](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L25-L26) — `reset_on_startup` 与 `rst_delayed` 都初值为 `RESET_POLARITY`。注意 VHDL 里信号初值在 elaboration 期生效，FPGA 上对应上电初值（在支持初值的器件上），所以仿真第 0 拍和上电第 0 拍 `rst_out` 都是有效。

再看启动计数器的变量声明，这里用了一个很地道的 VHDL 写法：

[reset_on_startup.vhd:L43-L44](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L43-L44) — `startup_counter` 是一个 `variable`，子类型是 `natural range 0 to RESET_TIME_IN_CLK_CYCLES`。把 generic 直接写进子类型区间，意味着这个变量的合法范围随 generic 自动伸缩。

接下来是计数与释放逻辑，注意它如何用 `subtype'high` 避免硬编码上限：

[reset_on_startup.vhd:L51-L57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L51-L57) — 计数器在小于 `startup_counter'subtype'high`（即 `RESET_TIME_IN_CLK_CYCLES`）时自增，达到上限后停止；同时在上限处把 `reset_on_startup` 赋为 `not RESET_POLARITY`（释放）。

`startup_counter'subtype'high` 这个写法的妙处：它返回变量所属子类型的上界，而这个上界正是 generic `RESET_TIME_IN_CLK_CYCLES`。于是「比较阈值」与「generic」被绑定在同一个声明里，将来改 generic 不需要再去改比较表达式，避免了「改了 generic 忘了改阈值」一类的不一致 bug。

释放赋值用的是 `not RESET_POLARITY`（无效极性），与初值的 `RESET_POLARITY`（有效极性）成对出现——极性的抽象贯穿始终。

#### 4.2.4 代码实践

**实践目标**：把 `RESET_TIME`（测试台里推导窗口长度的源头）改大，预测并验证启动窗口的拍数变化。

> 说明：本实践在**你本地的副本**上做实验，便于学习；仓库的提交不要改。另外，测试台其实已经用一个 `generate` 循环同时例化了 active-low 和 active-high 两个 DUT（见下方 4.3.3），所以你即便不改极性，也能直接观察 active-high 行为。

**操作步骤**：

1. 打开 `tb_reset_on_startup.vhd`，找到这三个常量：
   - [tb_reset_on_startup.vhd:L39-L41](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.vhd#L39-L41) — 时钟 100 MHz、复位时长 100 ns。
   - [tb_reset_on_startup.vhd:L43](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.vhd#L43) — 由 `RESET_TIME` 推导出 `RESET_TIME_IN_CLOCK_CYCLES`。
2. 手算当前值：时钟 100 MHz ⇒ 周期 10 ns；`RESET_TIME = 100 ns` ⇒ 窗口 \(N = 100/10 = 10\) 拍。
3. **预期表**（active-high DUT，`rst_in` 保持无效 `'0'`）：

   | 上电后第 k 拍（上升沿计数） | 预期 `rst_out`（index 1） | 理由 |
   | --- | --- | --- |
   | 0 ~ 9（共 10 拍） | `'1'`（有效） | `reset_on_startup` 仍有效 |
   | 第 10 拍之后 | `'0'`（无效） | 计数器到上限，`reset_on_startup` 释放 |

4. 在本地副本里把 `RESET_TIME : time := 100 ns` 改成 `200 ns`，重算 \(N = 200/10 = 20\)，把上表的有效拍数从 10 改成 20。
5. 运行仿真（参照 [u1-l3](u1-l3-environment-and-simulation.md) 用 `test_runner.py`）。注意：测试台的校验循环也用同一个 `RESET_TIME_IN_CLOCK_CYCLES` 常量驱动（[tb_reset_on_startup.vhd:L119-L123](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.vhd#L119-L123)），所以测试会自动适配新窗口长度。

**需要观察的现象**：波形里 `startup_counter` 从 0 数到新上限（10 或 20）的整个过程；数到上限的那一拍之后 `reset_on_startup` 翻转，`rst_out` 随之失效。

**预期结果**：实测有效窗口拍数与手算一致；若不一致，优先检查你是否同时改了 `RESET_TIME` 而忘了时钟频率。

> 关于测试台里那行奇怪的表达式 `RESET_TIME / (1.0 / SYS_CLK_FREQUENCY) / 1 sec`：它本质是 \(\text{RESET\_TIME} \times f_{\text{clk}}\)，即「复位时长里包含多少个时钟周期」。中间 `time / real` 得到的是时间量，末尾的 `/ 1 sec` 把它归一化成无量纲整数，最后 `natural(...)` 取整。结果就是窗口拍数。

**待本地验证**：本讲不假定你已经跑过命令；若暂时没有仿真器，可只完成「手算 + 预期表」，把实测列留空待补。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `RESET_TIME_IN_CLK_CYCLES` 设为 `positive` 的最小值 1，启动窗口是几拍？计数器会经历什么？

> **答案**：窗口为 1 拍。第 1 个上升沿计数器从 0 自增到 1（即上限），同一拍触发释放赋值，`reset_on_startup` 在下一拍（第 2 个上升沿后）变为无效。所以 `rst_out` 仅在第 0 拍~第 1 个上升沿之间保证有效。

**练习 2**：源码里「有效」和「无效」分别用什么表达式表示？为什么不需要 `if RESET_POLARITY = '0' then ... else ...` 这种分支？

> **答案**：「有效」= `RESET_POLARITY`，「无效」= `not RESET_POLARITY`。因为所有判定（初值、释放、输出合并）都写成「等于 `RESET_POLARITY` 即有效」的统一形式，极性被抽象成一个值，不需要按极性分叉逻辑。

---

### 4.3 rst_delayed 一拍延迟直通与防优化双属性

#### 4.3.1 概念说明

启动窗口结束之后，模块要能继续响应外部复位 `rst_in`。最直接的做法是 `rst_out <= rst_in`，但源码没有这么做，而是中间多加了一个寄存器 `rst_delayed <= rst_in`，让 `rst_out` 看 `rst_delayed` 而不是直接看 `rst_in`。效果是：**外部复位生效/失效都比输入晚一拍**。

为什么非要晚这一拍？源码注释给出理由：`rst_delayed` 充当复位树的「扇出缓冲」——它把一个高扇出的全局复位 `rst_in` 收敛到一个本地寄存器，再由这个寄存器去驱动本地紧密互联的消费者，避免综合器生成一个扇出过大的网络。

但这带来一个副作用：综合工具很容易认为 `rst_delayed <= rst_in` 是「冗余的换名寄存器」而把它优化掉（直接把消费者接到 `rst_in`），从而破坏上述扇出意图。于是源码用两个综合属性把它「钉住」。这两个属性一个是 Xilinx 的、一个是 Intel 的，同时挂在同一个信号上——这就是本模块的跨厂商手法。

> 与 [u2-l3](u2-l3-synthesis-attributes-clock-gating.md) 的衔接：u2-l3 已经介绍过 `preserve`（boolean，Intel/Quartus）和 `dont_touch`（string，Xilinx/Vivado）两类「请勿优化」标签的来历。本讲把它们具体落到 `rst_delayed` 这个信号上，并解释为何两者并存。

#### 4.3.2 核心流程

一拍延迟直通 + 输出合并的时序：

```text
外部 rst_in 在第 k 拍变为有效:
    第 k 拍上升沿: rst_delayed <= 有效   # 但此时 rst_delayed 仍是旧值
    ⇒ 第 k 拍 rst_out 仍为旧状态（延迟！）
    第 k+1 拍: rst_delayed 已是有效 ⇒ rst_out 变有效

外部 rst_in 在第 k 拍变为无效:
    第 k 拍 rst_out 仍有效（延迟！）
    第 k+1 拍 rst_out 变无效
```

注意：这「一拍延迟」对**外部复位**才显现；**上电启动复位**不经过 `rst_delayed`，它经 `reset_on_startup` 直达输出合并逻辑，所以上电窗口的释放时序由计数器决定，与这一拍延迟无关。

防优化双属性的「跨厂商」效果：

| 属性 | 类型 | 取值 | 工具 | 作用 |
| --- | --- | --- | --- | --- |
| `dont_touch` | `string` | `"true"` | Xilinx / Vivado | 阻止优化、合并、重定时 |
| `preserve` | `boolean` | `true` | Intel / Quartus | 阻止优化、合并 |

两个属性写在同一份源码里：Vivado 只认 `dont_touch`（忽略 `preserve`），Quartus 只认 `preserve`（忽略 `dont_touch`），于是**同一份 RTL 在两家工具下都能保住这个寄存器**，无需维护两套 architecture。这就是本模块区别于 u2「多 architecture」模式的轻量跨厂商手法。

#### 4.3.3 源码精读

先看直通赋值与注释解释的扇出意图：

[reset_on_startup.vhd:L47-L49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L47-L49) — `rst_delayed <= rst_in` 在时钟进程里，是一个标准的寄存器赋值；注释说明它的存在是为了「把扇出控制在合理水平」，因为本地复位的消费者本就紧密互联。

再看两个防优化属性——这是本模块跨厂商兼容的核心：

[reset_on_startup.vhd:L28-L35](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L28-L35) — 先声明 `dont_touch: string` 并把 `rst_delayed` 标为 `"true"`（Xilinx），再声明 `preserve: boolean` 并把 `rst_delayed` 标为 `true`（Intel）。两者都只挂在 `rst_delayed` 上。

值得注意：`reset_on_startup` 信号**没有**挂任何属性。原因是它是「有真实逻辑」的信号（被条件赋值驱动），综合工具不太会把它当成冗余换名删掉；而 `rst_delayed` 是纯粹的直通寄存器，最容易被优化，所以只保护它一个。

最后，输出合并逻辑把两个复位源按极性 OR 起来：

[reset_on_startup.vhd:L37-L40](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/reset_on_startup.vhd#L37-L40) — 注释点明「当 `rst_delayed` 或 `reset_on_startup` 任一为有效极性时，输出有效」。这就是「内部启动复位」与「外部延迟复位」两个来源的聚合点。

测试台如何验证这一拍延迟？看 `checker` 进程里的断言序列：

[tb_reset_on_startup.vhd:L138-L144](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.vhd#L138-L144) — 在把 `rst_in` 拉成有效**之后立即**检查 `rst_out`，预期它「还没有」变有效（1.4a/1.4b），印证一拍延迟；随后 `wait_clk_cycles(1)` 再检查才预期有效（1.5a/1.5b）。

[tb_reset_on_startup.vhd:L153-L160](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.vhd#L153-L160) — 释放 `rst_in` 时同理：立即检查仍有效（1.6a/1.6b，延迟），一拍后才无效（1.7a/1.7b）。

而测试台「同时验证两种极性」靠的是这个 generate 循环：

[tb_reset_on_startup.vhd:L188-L200](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.vhd#L188-L200) — 用 `RESET_POLARITIES := ('0', '1')` 数组生成两个 DUT 实例，index 0 是 active-low、index 1 是 active-high，共用同一套时钟与激励。这就是为什么前面 4.2.4 说「不改极性也能观察 active-high」。

#### 4.3.4 代码实践

**实践目标**：验证「外部复位的一拍延迟」与「双极性同时正确」，并产出预期与实测对比表。

**操作步骤**：

1. 在 `tb_reset_on_startup.vhd` 的 `checker` 进程里通读 `test_startup_reset` 过程（[L108-L168](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/reset_on_startup/tb/tb_reset_on_startup.vhd#L108-L168)），它分四段：启动窗口有效 → 释放后无效 → 拉低 `rst_in` 后延迟一拍才有效 → 释放 `rst_in` 后延迟一拍才无效。
2. 填写下面这张「预期 vs 实测」对比表（以 active-low DUT，index 0 为例，`RESET_TIME_IN_CLOCK_CYCLES = 10`）：

   | 测试点 | 激励动作 | 预期 `rst_out`(0) | 断言标签 | 实测 |
   | --- | --- | --- | --- | --- |
   | 启动窗口内 | 上电，`rst_in`='1'（无效） | `'0'`（有效） | 1.1a | 待补 |
   | 窗口结束后 | 同上，进入第 11 拍 | `'1'`（无效） | 1.2a | 待补 |
   | 拉低 `rst_in` 当拍 | `rst_in`='0'（有效） | `'1'`（仍无效，延迟） | 1.4a | 待补 |
   | 拉低 `rst_in` 次拍 | — | `'0'`（有效） | 1.5a | 待补 |
   | 释放 `rst_in` 当拍 | `rst_in`='1'（无效） | `'0'`（仍有效，延迟） | 1.6a | 待补 |
   | 释放 `rst_in` 次拍 | — | `'1'`（无效） | 1.7a | 待补 |

3. 对照 active-high DUT（index 1）把每个 `'0'`/`'1'` 取反，预期同样通过（断言标签 1.1b ~ 1.7b）。
4. （可选）给 `reset_controller` 进程加一行 `report "counter=" & to_string(startup_counter);`（仅在仿真可见，综合时会被 `report` 语句自然忽略），重新跑仿真，逐拍对照计数器值与 `rst_out` 翻转。

**需要观察的现象**：`rst_in` 动作当拍 `rst_out` 不动、下一拍才跟随——这是一拍延迟的直接证据；两个极性的 DUT 波形互为反相，但节拍完全一致。

**预期结果**：六行预期全部与实测吻合；若 1.4a/1.6a 这类「延迟」断言失败，多半是有人把 `rst_out` 改成了直接看 `rst_in`（绕过了 `rst_delayed`）。

**待本地验证**：若无仿真器，可只完成预期列，把「实测」列留空，并把步骤 4 的 `report` 当作日后调试时的预设手段。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dont_touch` 是 `string` 类型、`preserve` 是 `boolean` 类型？能否统一成一个？

> **答案**：因为两家工具的历史约定不同——Vivado 的 `dont_touch` 用字符串 `"true"/"false"`，Quartus 的 `preserve` 用布尔 `true/false`。VHDL 里一个属性名只能有一种类型，所以必须分成两个属性声明，无法合并成一个。

**练习 2**：如果删掉 `rst_delayed` 上的两个属性，综合器最可能做出什么「错误」优化？对设计有什么实际影响？

> **答案**：综合器很可能判定 `rst_delayed <= rst_in` 是冗余换名，把 `rst_delayed` 删掉、让消费者直接接 `rst_in`。功能上仿真仍正确，但物理上失去了一个本地扇出缓冲，可能导致复位树扇出过大、时序（尤其是复位释放时的 skew）变差——这正是注释里「holds the fan-out at a reasonable level」想避免的。

**练习 3**：`reset_on_startup` 信号为什么不需要挂防优化属性？

> **答案**：它由条件赋值驱动（计满才释放），带有真实逻辑，综合工具不会把它当成「冗余换名」删除；而 `rst_delayed` 是纯直通、最易被优化，所以才单独保护它。

---

## 5. 综合实践

把本讲的三块知识串起来，完成一次「只读源码 + 完整预测」的综合练习（无需改源码）。

**任务**：以测试台现有的双 DUT 配置（active-low index 0、active-high index 1、`RESET_TIME_IN_CLOCK_CYCLES = 10`）为对象，手画一张覆盖「上电 → 启动窗口 → 释放 → 外部复位生效 → 外部复位释放」全过程的时序图，要求：

1. 画出 `clk`（100 MHz）、两个 DUT 的 `rst_in`、两个 DUT 的 `rst_out`、以及任一 DUT 内部的 `startup_counter` 与 `reset_on_startup`。
2. 在图上标出三个关键事件的发生拍：
   - 启动窗口结束、`reset_on_startup` 释放的拍（计数器到 10）。
   - 外部 `rst_in` 拉有效后，`rst_out` 跟随的那一拍（体现 `rst_delayed` 一拍延迟）。
   - 外部 `rst_in` 释放后，`rst_out` 跟随的那一拍。
3. 把图上的每个转折点与 `tb_reset_on_startup.vhd` 里的断言标签（1.1a/b ~ 1.7a/b）一一对应。

**验收标准**：你能用一句话解释「为什么上电窗口的释放不经过一拍延迟、而外部复位的生效/释放却经过一拍延迟」——前者由 `reset_on_startup`（直达合并逻辑）控制，后者由 `rst_delayed`（寄存器）控制。

> 进阶（可选）：若本地有仿真器，运行 `test_runner.py` 打开 GUI（参照 [u1-l3](u1-l3-environment-and-simulation.md) 把 `gui` 设为 True），用 `tb_reset_on_startup.do` 加载波形分组，把你手画的时序图与实测波形逐拍核对。

## 6. 本讲小结

- `reset_on_startup` 是一个上电复位控制器：上电后强制保持复位一个可配置窗口，窗口结束后把外部 `rst_in`（延迟一拍）直通到输出。
- 启动计数器用 `variable ... natural range 0 to RESET_TIME_IN_CLK_CYCLES` 声明，并用 `subtype'high` 做比较阈值，使窗口长度随 generic 自动伸缩、无硬编码。
- 极性被抽象成 `RESET_POLARITY` 一个值：所有「有效」判定写成 `= RESET_POLARITY`，「无效」写成 `not RESET_POLARITY`，无需 if-else 分叉即可同时支持 active-low / active-high。
- `rst_out` 是组合输出，模块对 `rst_in` 的一拍延迟完全来自 `rst_delayed` 这个直通寄存器；该寄存器还充当本地复位树的扇出缓冲。
- 跨厂商兼容不走「多 architecture」，而是在 `rst_delayed` 上同时挂 `dont_touch`（Xilinx）和 `preserve`（Intel）两个属性——同一份 RTL 两家工具都能保住这个寄存器。
- 测试台用一个 `generate` 循环同时例化两种极性的 DUT，并通过「立即检查仍为旧值、一拍后才跟随」的断言精确验证了一拍延迟。

## 7. 下一步学习建议

- **横向对比跨厂商手法**：回到 [u2-l1](u2-l1-multi-architecture-pattern.md) 与 [u2-l3](u2-l3-synthesis-attributes-clock-gating.md)，把「多 architecture」（fifo / ff_synchroniser）与「单 architecture + 双属性」（reset_on_startup）两种手法对照一遍，思考各自适合什么场景。
- **进入时钟生成**：本讲的启动计数器是一个最简单的 saturating 计数器；下一单元 [u5-l1 clock_enable](u5-l1-clock-enable-gating.md) 会用更复杂的嵌套 `if generate` 处理时钟门控，那里的 BUFGCE 同样是 Xilinx 全局网络原语，可与本讲的属性防优化思路连起来读。
- **继续读源码**：若你想看「复位信号如何被下游消费」，可在 `ip/` 下搜索哪些模块的端口里有 `rst` / `rst_n`，观察它们如何把本模块的 `rst_out` 接到自己的复位端口上，从而理解复位树在整座 IP 库里的传播。
