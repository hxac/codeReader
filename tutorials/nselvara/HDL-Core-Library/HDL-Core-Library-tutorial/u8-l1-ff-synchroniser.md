# 单比特同步器 ff_synchroniser

## 1. 本讲目标

学完本讲，你应该能够：

- 用自己的话说清「跨时钟域（CDC）」「亚稳态」「MTBF」这三个概念，以及它们之间的关系。
- 读懂 `ff_synchroniser` 的 entity 接口与 4 个 generic 的含义。
- 区分两套实现：Xilinx 的 `xpm_cdc_single` 黑盒，与 Intel 的「显式同步链 + 综合属性」。
- 解释 `SYNC_SHIFT_FF`（同步链长度）如何在「延迟」与「MTBF」之间权衡，以及 `SRC_INPUT_REG` 的取舍。
- 用测试台观察「快时钟域 → 慢时钟域」的跨域延迟，并能动手比较 `SYNC_SHIFT_FF = 2` 与 `4` 的节拍差异。

## 2. 前置知识

本讲承接前面已建立的概念，不再重复细节：

- **时钟域、触发器、建立/保持时间**：来自第 4、5 单元的时序模块（消抖器、复位、时钟门控）。
- **entity / architecture / generic**：来自 [u2-l1 同一实体多架构模式](u2-l1-multi-architecture-pattern.md)。
- **综合属性 `preserve` / `dont_touch` / `altera_attribute`**：来自 [u2-l3 综合属性与时钟门控](u2-l3-synthesis-attributes-clock-gating.md)。
- **厂商库 `xpm` / `altera_mf` / `unisim`**：来自 [u2-l2 厂商仿真库](u2-l2-vendor-simulation-libraries.md)。
- **VUnit 测试台骨架与 `generate_advanced_clock`**：来自 [u1-l3](u1-l3-environment-and-simulation.md)、[u3-l3](u3-l3-tb-utils-clock-generation.md)、[u11-l1](u11-l1-vunit-testbench-structure.md)。

下面先用最通俗的方式补一个本讲专属、必须先理解的概念：**亚稳态**。

一个 D 触发器在时钟上升沿采样 D 端。若 D 在「建立时间 \(t_{su}\)」之前就已稳定，输出 Q 会在一个干净的延迟后翻到新值。但若 D 恰好在建立/保持时间窗口内发生变化（违反 setup/hold），触发器就会进入**亚稳态**：Q 既不是干净的 0 也不是干净的 1，而是停留在半电压值，经过一段**不可预测**的时间后才随机「塌缩」成 0 或 1。

亚稳态本身不会烧坏触发器。真正的危害是：这个塌缩过程可能拖得很久，以至于下游逻辑在本拍就读到了一个还在抖动的中间电平，再被不同扇出的门解读成不同的 0/1，于是整个系统的状态错乱。

跨时钟域时，源域信号相对目的域时钟是**异步**的，采样时刻撞进 setup/hold 窗口的概率永远不为零——亚稳态不可避免，只能降低它「造成危害」的概率。`ff_synchroniser` 就是干这件事的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ip/ff_synchroniser/ff_synchroniser.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd) | 设计源码：1 个 entity + 2 套 architecture（Xilinx / Intel）。 |
| [ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd) | VUnit 测试台：100 MHz 源域 → 25 MHz 目的域，验证同步链的稳定跟随。 |
| [ip/ff_synchroniser/tb/tb_ff_synchroniser.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.do) | ModelSim/QuestaSim 波形脚本，按「接口 / 内部 / 测试台」分组展示信号。 |

> 一个先要记住的「以源码为准」事实：本模块**只有两套 architecture**（`xilinx_behavioural_ff_synchroniser` 与 `intel_behavioural_ff_synchroniser`），**没有** `own_behavioural_*`。测试台文件头的注释提到 `own_behavioural_ff_synchroniser`（见后文 4.2 节），但那是一段过时注释，实际例化的是 Intel 架构。

## 4. 核心概念与源码讲解

### 4.1 ff_synchroniser：模块全貌与 CDC 问题

#### 4.1.1 概念说明

`ff_synchroniser` 解决的问题是：**把一个单比特信号，从一个时钟域安全地搬到另一个时钟域**。

「安全」分两层，必须区分清楚，这是本讲最容易混淆的点：

1. **亚稳态危害**（本模块要解决的）：目的域第一级触发器可能亚稳态。用一串触发器（同步链）逐级在目的时钟域重新采样，给亚稳态留出更多「塌缩恢复」时间，把危害概率压到极低。
2. **事件覆盖**（本模块**不**解决）：一个比目的时钟周期还窄的脉冲，可能根本没被任何一次采样沿采到，从而整体丢失。这属于「脉冲跨越」问题，需要握手机制或基于电平翻转（toggle）的 CDC，**不是**靠加长同步链能解决的。

一句话：**同步链压低亚稳态危害，但救不回被漏采的窄脉冲。** 第 5 节的综合实践会让你亲手验证这一点。

衡量「亚稳态危害有多小」的指标是 **MTBF**（Mean Time Between Failures，平均故障间隔）。同步链越长，MTBF 越长，代价是延迟也越大。4.3 节给出定量关系。

#### 4.1.2 核心流程

从外部看，模块只有两个时钟、一个输入、一个输出。信号在内部的流动是：

```
source_clk 域                         destination_clk 域
┌──────────┐   src_reg     ┌──────────────────────────────┐
│source_   │──(可选寄存)──▶│ meta_stable_reg              │
│domain    │   跨域边界     │   │ (第1级，最易亚稳态)       │
└──────────┘                │   ▼                          │
                            │ sync_chain(0)→(1)→…→(high)   │
                            │                       │      │
                            │                       ▼      │
                            │              destination_    │
                            │                  domain      │
                            └──────────────────────────────┘
```

执行过程：

1. （可选）在源时钟域用 `src_reg` 把输入寄存一刀，保证跨边界的是寄存器干净输出，而非带毛刺的组合逻辑。
2. 在目的时钟域，第一级 `meta_stable_reg` 采样跨域信号——这一级最可能亚稳态。
3. `sync_chain` 是一条移位寄存器链，每拍把信号往后挪一级，给亚稳态留恢复时间。
4. 输出取链的最末一级，此时亚稳态已极大概率塌缩成稳定值。

#### 4.1.3 源码精读

entity 只声明一次，端口契约固定，4 个 generic 控制行为：[ip/ff_synchroniser/ff_synchroniser.vhd:20-33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L20-L33) —— 这是「同一 entity 多架构」模式（[u2-l1](u2-l1-multi-architecture-pattern.md)）的端口契约部分。

四个 generic 的含义：[ip/ff_synchroniser/ff_synchroniser.vhd:22-25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L22-L25)

```vhdl
SYNC_SHIFT_FF: positive range 2 to 10 := 4; -- 同步链长度（触发器个数），范围 2~10，默认 4
INIT_SYNC_FF: boolean := false;  -- 仿真初始化值开关（仅影响 xpm 仿真模型）
SIM_ASSERT_MSG: boolean := false;-- 仿真断言信息开关
SRC_INPUT_REG: boolean := true;  -- 是否在源域先寄存一刀
```

端口只有 4 个：源时钟、目的时钟、源域输入、目的域输出：[ip/ff_synchroniser/ff_synchroniser.vhd:28-31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L28-L31)。

注意 `SYNC_SHIFT_FF` 的下界是 2——单级触发器不算合格的同步器，至少要两级。

#### 4.1.4 代码实践（阅读型）

1. **目标**：建立模块的整体心智模型。
2. **步骤**：打开 [ff_synchroniser.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd)，在纸上画出 4.1.2 的框图，把 4 个 generic 的默认值标到对应位置。
3. **观察**：entity 之后、第一个 architecture 之前，出现了一行 `library xpm; use xpm.vcomponents.all;`（[第 35-36 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L35-L36)）——厂商库声明被**局部化**到具体 architecture 之前，这正是 [u2-l1](u2-l1-multi-architecture-pattern.md) 讲过的「依赖局部化」风格。
4. **预期结果**：你能不看源码，复述出「源域输入 →（可选 src_reg）→ meta_stable_reg → sync_chain → 输出」这条数据通路。

#### 4.1.5 小练习与答案

**Q1**：为什么 entity 里 `SYNC_SHIFT_FF` 的范围下界写成 2，而不是 1？

**答**：只有一级触发器的「同步器」无法给亚稳态任何额外恢复时间——它本身就是那个会亚稳态的触发器，下游立刻读到抖动电平。至少要两级（第一级亚稳态、第二级等它塌缩后再采）才有意义，故下界为 2。

**Q2**：模块名里 `ff` 是什么的缩写？

**答**：Flip-Flop（触发器）。`ff_synchroniser` = 「用一串触发器做成的同步器」。

---

### 4.2 两套实现：xpm_cdc_single 黑盒 vs 显式同步链

#### 4.2.1 概念说明

同一个 entity 配了两套 architecture（[u2-l1](u2-l1-multi-architecture-pattern.md)），它们的「外部行为」一致（都是把单比特同步到目的域），但「内部实现」截然不同：

| | Xilinx 实现 | Intel 实现 |
| --- | --- | --- |
| 架构名 | `xilinx_behavioural_ff_synchroniser` | `intel_behavioural_ff_synchroniser` |
| 做法 | 例化黑盒原语 `xpm_cdc_single` | 手写一条显式移位寄存器同步链 |
| 厂商库 | `library xpm`（紧贴该架构前） | 仅用 `altera_attribute`（字符串属性，仿真可忽略） |
| 综合属性 | 无（由 xpm 宏内部处理） | `preserve` + `altera_attribute`（SDC 假路径 + 同步器识别） |

关键区别在于「谁负责保证同步器的可靠性」：

- **Xilinx 侧**把全部责任交给 `xpm_cdc_single` 这个厂商宏——它内部已做好正确的约束、布局、链长，你只管配 generic。
- **Intel 侧**用纯 RTL 写出同步链，可靠性靠你手动挂综合属性来保证（防优化 + 假路径 + 同步器识别）。

> 一个以源码为准的提醒：测试台文件头注释 [tb_ff_synchroniser.vhd:4-5](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd#L4-L5) 说「只测 `own_behavioural_ff_synchroniser`」，但该架构**并不存在**；实际例化的是 Intel 架构 [tb_ff_synchroniser.vhd:205](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd#L205)。注释已过时，以源码为准。

#### 4.2.2 核心流程

**Xilinx 流程**：直接把 entity 的 generic 与 port 一一映射给 `xpm_cdc_single`，结束。整个架构体只有这一个元件例化语句。

**Intel 流程**：分两段进程——

1. 源域进程：每个 `source_clk` 上升沿把 `source_domain` 打进 `src_reg`。
2. 目的域进程：每个 `destination_clk` 上升沿，把 `src_reg` 打进 `meta_stable_reg`，并把整条 `sync_chain` 右移一位、最低位补入 `meta_stable_reg`。
3. 输出 = `sync_chain` 的最高位（链的最末一级）。

#### 4.2.3 源码精读

**Xilinx 架构**整体：[ip/ff_synchroniser/ff_synchroniser.vhd:43-58](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L43-L58)。核心是 generic map 把 4 个 generic 翻译成 xpm 的参数，其中用 `boolean'pos(...)` 把 VHDL 的布尔转成 xpm 要的 0/1 整数：[ff_synchroniser.vhd:45-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L45-L51)

```vhdl
xpm_cdc_single_sync_inst: xpm_cdc_single
    generic map (
        DEST_SYNC_FF   => SYNC_SHIFT_FF,                 -- 同步链长度
        INIT_SYNC_FF   => boolean'pos(INIT_SYNC_FF),     -- true→1, false→0
        SIM_ASSERT_CHK => boolean'pos(SIM_ASSERT_MSG),
        SRC_INPUT_REG  => boolean'pos(SRC_INPUT_REG)     -- 是否在源域寄存
    )
```

端口连线极简，`src_clk`/`src_in`/`dest_clk`/`dest_out` 一一对应：[ff_synchroniser.vhd:52-57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L52-L57)。

**Intel 架构**整体：[ff_synchroniser.vhd:66-100](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L66-L100)。先看信号声明：[ff_synchroniser.vhd:67-69](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L67-L69)

```vhdl
signal src_reg: std_ulogic;                                        -- 源域寄存器
signal meta_stable_reg: std_ulogic;                                -- 同步链第 1 级（最易亚稳态）
signal sync_chain: std_ulogic_vector(SYNC_SHIFT_FF - 2 downto 0);  -- 其余各级；-2 含义见 4.3
```

再看综合属性（承接 [u2-l3](u2-l3-synthesis-attributes-clock-gating.md)）：[ff_synchroniser.vhd:71-79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L71-L79)。三件事：

- `altera_attribute` + `SYNCHRONIZER_IDENTIFICATION "FORCED IF ASYNCHRONOUS"` 挂在 `src_reg` 上，让 Quartus 在判定该路径为异步跨域时把它识别为同步器级，做紧凑布局与 MTBF 优化。
- 一条 `set_false_path -to ... meta_stable_reg` 的 SDC 假路径（[第 74 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L74)），把终点落在亚稳态第一级的路径从时序分析里剔除——因为跨域到达时间本来就不可靠分析。
- `preserve`（boolean）挂在 `src_reg` 与整条 `sync_chain` 上，阻止综合工具把它们优化掉或重定时（retiming）；注意首级 `meta_stable_reg` **没有** `preserve`，它只靠上面那条假路径保护——这与 [u2-l3](u2-l3-synthesis-attributes-clock-gating.md) 的结论一致。

源域进程（无条件存在，见 4.3 的提醒）：[ff_synchroniser.vhd:83-88](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L83-L88)

```vhdl
comb_reg_source_domain_proc: process (source_clk)
begin
    if rising_edge(source_clk) then
        src_reg <= source_domain;   -- 源域打一拍
    end if;
end process;
```

目的域移位链：[ff_synchroniser.vhd:91-97](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L91-L97)

```vhdl
sync_chain_in_dst_dom_proc: process (destination_clk)
begin
    if rising_edge(destination_clk) then
        meta_stable_reg <= src_reg;                                        -- 第 1 级
        sync_chain <= sync_chain(high-1 downto low) & meta_stable_reg;     -- 整体右移，最低位补第 1 级
    end if;
end process;
```

输出取链尾：[ff_synchroniser.vhd:99](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L99)。

#### 4.2.4 代码实践（对比型）

1. **目标**：看清两套实现的 generic/属性对应关系。
2. **步骤**：对照源码填下面这张表（粗体项需你补全）。

   | 关切点 | Xilinx 实现 | Intel 实现 |
   | --- | --- | --- |
   | 同步链长度由谁决定 | `DEST_SYNC_FF => SYNC_SHIFT_FF` | **（自填：哪段代码？）** |
   | 源域寄存由谁决定 | `SRC_INPUT_REG => boolean'pos(...)` | **（自填：是否受 generic 控制？）** |
   | 防优化靠什么 | 由 xpm 宏内部处理 | **（自填：哪几个属性？）** |

3. **观察**：Xilinx 架构里一个 `preserve` 都没有；Intel 架构里却有三个属性声明。
4. **预期结果**：你能解释「Xilinx 把可靠性责任外包给 xpm 宏，Intel 把可靠性责任写进 RTL + 属性」这一根本差异。

#### 4.2.5 小练习与答案

**Q1**：Xilinx 架构里 `boolean'pos(INIT_SYNC_FF)` 的作用是什么？为什么不直接写 `INIT_SYNC_FF => INIT_SYNC_FF`？

**答**：`xpm_cdc_single` 的 `INIT_SYNC_FF` 参数是整数类型（0/1），而 entity 的 `INIT_SYNC_FF` 是 VHDL `boolean`。`boolean'pos(true)=1`、`boolean'pos(false)=0`，用它完成布尔→整数的类型转换。

**Q2**：Intel 架构的 `meta_stable_reg` 没有 `preserve` 属性，综合时会不会被优化掉？

**答**：不会丢失，因为它在功能上被 `sync_chain` 的移位逻辑驱动、又驱动输出，是同步链的必要环节；`preserve` 缺失只是不强制保留其位置，它转而靠那条 `set_false_path -to meta_stable_reg` 的 SDC 假路径来屏蔽跨域假违例（见 [u2-l3](u2-l3-synthesis-attributes-clock-gating.md)）。

---

### 4.3 SYNC_SHIFT_FF / SRC_INPUT_REG：MTBF 与延迟的权衡

#### 4.3.1 概念说明

**SYNC_SHIFT_FF（同步链长度）——延迟与 MTBF 的权衡。**

同步链每多一级，就给亚稳态多一个时钟周期的「塌缩恢复」时间。由于恢复是一个指数衰减过程，MTBF 对链长极度敏感。经典同步器 MTBF 公式为：

\[
\text{MTBF} \;=\; \frac{e^{\,T_{res}/\tau}}{T_{0}\cdot f_{clk}\cdot f_{data}}
\]

其中 \(T_{res}\) 是亚稳态信号在被下一级采样前可用的恢复时间，\(\tau\) 与 \(T_{0}\) 是触发器工艺常数，\(f_{clk}\) 是目的时钟频率，\(f_{data}\) 是数据翻转频率。

每多一级同步链，\(T_{res}\) 大约多一个目的时钟周期，于是 MTBF 乘以约 \(e^{T_{clk}/\tau}\)——这是一个通常极大的因子。所以把 `SYNC_SHIFT_FF` 从 2 改到 4，可能把「每秒故障数次」的同步器变成「数百万年才故障一次」。**代价**是输出延迟也增加了同样的级数：链长 \(N\) ≈ 在目的域延迟 \(N\) 拍。

这正是 entity 注释「configurable to set the length of the synchronisation chain to achieve the desired MTBF」（[第 5 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L5)）的含义。

**SRC_INPUT_REG（是否在源域先寄存一刀）。**

- `true`：先在源域把输入寄存一刀，保证跨边界的是寄存器干净输出，而非穿过多级组合逻辑、带毛刺的信号。需要源时钟存在，多 1 拍源域延迟。
- `false`：不在源域寄存，信号直入目的域同步链；源时钟随之不再必要，适合源信号已在上游寄存好、或根本没有源时钟的异步输入（如机械按键）。

> 以源码为准的一个重要事实：`SRC_INPUT_REG` **只对 Xilinx 架构生效**——它被映射进 `xpm_cdc_single` 的 `SRC_INPUT_REG` 参数（[第 50 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L50)）。而 Intel 架构里的源域进程 `comb_reg_source_domain_proc`（[第 83-88 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L83-L88)）**无条件存在**，整个架构体里从未引用 `SRC_INPUT_REG`。即选 Intel 架构时，无论该 generic 取何值，输入都会被源域寄存一刀。移植时若依赖 `SRC_INPUT_REG=false` 的「无源时钟」语义，须注意这一点。

#### 4.3.2 核心流程

`SYNC_SHIFT_FF` 如何同时决定「链长」「延迟」「存储位宽」三件事：

1. **链长**：目的域共 `SYNC_SHIFT_FF` 个触发器 = `meta_stable_reg`(1 个) + `sync_chain`(\(N-1\) 个)。
2. **延迟**：输入稳定后，输出约在 `SYNC_SHIFT_FF` 拍目的时钟后才稳定跟随（测试台里用 `SYNC_SHIFT_FF + 1` 拍的等待来保证对齐，见下文）。
3. **存储位宽**：`sync_chain` 声明为 `std_ulogic_vector(SYNC_SHIFT_FF - 2 downto 0)`，宽度 = \((N-2)-0+1 = N-1\)。注释 `-2: Include meta_stable_reg as first element`（[第 69 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L69)）点明了这一点：第 1 级单独用标量 `meta_stable_reg`，所以向量里只放其余 \(N-1\) 级，向量上界写成 `N-2`。

举例校验：

| `SYNC_SHIFT_FF` | `meta_stable_reg` | `sync_chain` 宽度 | 目的域总链长 | 检查 |
| --- | --- | --- | --- | --- |
| 2 | 1 | 1（`0 downto 0`） | 2 | \(1+1=2\) ✓ |
| 4（默认/测试台值） | 1 | 3（`2 downto 0`） | 4 | \(1+3=4\) ✓ |

#### 4.3.3 源码精读

generic 声明：[ff_synchroniser.vhd:22-25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L22-L25)。Xilinx 侧把 `SYNC_SHIFT_FF` 映射给 `DEST_SYNC_FF`、`SRC_INPUT_REG` 映射给同名 xpm 参数：[ff_synchroniser.vhd:47-50](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L47-L50)。Intel 侧用 `SYNC_SHIFT_FF` 推导向量宽度：[ff_synchroniser.vhd:69](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L69)，并把链尾接至输出：[ff_synchroniser.vhd:99](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L99)。

测试台里把 `SYNC_SHIFT_FF` 设为常量 4：[tb_ff_synchroniser.vhd:55-58](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd#L55-L58)，并在每次改变 `source_domain` 后等待 `SYNC_SHIFT_FF + 1` 个目的时钟周期再去比对输出（例如 [第 129、134 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd#L129-L134)）——这个 `+1` 正是给满链长延迟留足对齐余量。

#### 4.3.4 代码实践（仿真型·主实践）

> 本实践对应讲义规格里的代码实践任务。

测试台已经把源域配成 100 MHz、目的域配成 25 MHz（快→慢），正好契合「快时钟域窄脉冲跨到慢时钟域」的场景：[tb_ff_synchroniser.vhd:43-44](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd#L43-L44)。

1. **实践目标**：比较 `SYNC_SHIFT_FF = 2` 与 `4` 的输出节拍差异，并亲手观察「窄脉冲可能被漏采」。
2. **操作步骤**：
   - 先按原样（`SYNC_SHIFT_FF = 4`）跑一次测试台（用 [u1-l3](u1-l3-environment-and-simulation.md) 的 `test_runner.py`），确认全绿。
   - 把测试台里的常量改为 `constant SYNC_SHIFT_FF: positive := 2;`（[第 55 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd#L55)），再跑一次，仍应全绿（因为等待拍数随 `SYNC_SHIFT_FF` 自动收缩）。
   - （可选，示例代码）在 `test_own_ff_synchroniser` 过程末尾追加一段单源时钟周期窄脉冲激励，观察它是否被目的域采到：

     ```vhdl
     -- 示例代码：送一个仅持续 1 个 source_clk 周期（10 ns）的窄脉冲
     source_domain <= '0';
     wait_destination_clk_cycles(SYNC_SHIFT_FF + 1);
     check_destination_domain_own(expected => '0');
     source_domain <= '1';
     wait_source_clk_cycles(1);   -- 10 ns，远小于目的时钟周期 40 ns
     source_domain <= '0';
     wait_destination_clk_cycles(SYNC_SHIFT_FF + 2);
     -- 观察这一拍 destination_domain_own 是否一定为 '1'
     ```
3. **需要观察的现象**：
   - **节拍差异**：`source_domain` 稳定为 `'1'` 后，`destination_domain_own` 在 `SYNC_SHIFT_FF=2` 时约 2 个目的时钟周期后才变 `'1'`；`=4` 时约 4 个目的时钟周期后才变 `'1'`。链更长 = 延迟更大。
   - **窄脉冲**：上面那段 10 ns 窄脉冲**未必**在 `destination_domain_own` 上出现——因为目的域每 40 ns 才采样一次，10 ns 的脉冲很可能落在两次采样沿之间被整段漏掉。这跟 `SYNC_SHIFT_FF` 无关，是「事件覆盖」问题。
4. **预期结果**：你会得到两张波形，分别显示 2 拍与 4 拍的跨域延迟；并确认窄脉冲可能丢失。**待本地验证**：窄脉冲是否被采到取决于它与目的采样沿的相对相位，仿真中可能时有时无——这正是本实践要揭示的不可靠性。
5. **解释对 MTBF 的影响**：链从 2 加到 4，多出的 2 级让 \(T_{res}\) 增大约两个目的时钟周期，MTBF 被乘以约 \(e^{2T_{clk}/\tau}\)（指数放大）。延迟换可靠性，这就是同步链设计的核心权衡。

#### 4.3.5 小练习与答案

**Q1**：给定 `SYNC_SHIFT_FF = 4`，Intel 架构里 `sync_chain` 的宽度是多少？写出推导。

**答**：宽度为 3。`sync_chain` 声明为 `std_ulogic_vector(SYNC_SHIFT_FF - 2 downto 0)` = `(2 downto 0)`，元素个数 \(= 2-0+1 = 3\)。它代表第 1 级 `meta_stable_reg` 之后的 3 级，连同 `meta_stable_reg` 共 4 级。

**Q2**：某按钮信号没有源时钟，想用 `ff_synchroniser` 同步到系统时钟域。`SRC_INPUT_REG` 应设成什么？选 Xilinx 还是 Intel 架构更省心？

**答**：语义上应设 `SRC_INPUT_REG => false`（无源时钟，不能在源域寄存）。但注意「以源码为准」的陷阱：只有 **Xilinx 架构**尊重这个 generic；**Intel 架构**无条件包含源域进程、仍需要 `source_clk`。因此这种「无源时钟」场景选 Xilinx 架构更贴合 generic 语义，选 Intel 架构则必须额外提供一个（可空闲的）`source_clk`。

**Q3**：为什么测试台每次改 `source_domain` 后要等 `SYNC_SHIFT_FF + 1` 拍而不是恰好 `SYNC_SHIFT_FF` 拍？

**答**：链长 \(N\) 决定信号要经过 \(N\) 级触发器，再加上采样对齐、仿真 delta 的余量，多等 1 拍可确保输出已稳定落到最末一级，避免在「正好翻沿」的临界拍上比对产生偶发误判。

## 5. 综合实践

把本讲三块内容串起来：**用快→慢跨域场景，量化观察「链长对延迟与 MTBF 的影响」与「同步链救不回窄脉冲」两件事。**

任务步骤：

1. **准备**：按 [u1-l3](u1-l3-environment-and-simulation.md) 建好 venv、拉取 `vhdl_utils` 子模块，确保 `test_runner.py` 能发现 `tb_ff_synchroniser`。注意该测试台例化的是 Intel 架构（[第 205 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd#L205)），其 `altera_attribute` 是纯字符串、仿真器会忽略，故无需厂商库即可编译运行。
2. **量化延迟**：分别用 `SYNC_SHIFT_FF = 2` 和 `4` 跑测试台，在波形（配套 `.do` 脚本 [tb_ff_synchroniser.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.do) 已把 `sync_chain` 加进 Internal 分组）上量出 `source_domain` 稳定到 `destination_domain_own` 稳定的目的时钟周期数，填表：

   | `SYNC_SHIFT_FF` | 实测延迟（目的周期） | 与理论 \(N\) 是否吻合 |
   | --- | --- | --- |
   | 2 | （填） | |
   | 4 | （填） | |

3. **观察漏采**：加入 4.3.4 的窄脉冲示例激励，多次运行（或微调脉冲相位），记录 `destination_domain_own` 是否出现对应脉冲。
4. **画图与解释**：画一张时序图，标注两个 `SYNC_SHIFT_FF` 取值的输出对齐节拍；用一句话说明「链长 +2 如何换来 MTBF 指数级提升」，以及「为什么窄脉冲丢失不是同步链的锅」。

> 波形脚本提示：`.do` 第 10 行引用的 `Own_Arch_DuT/src_sig` 在当前源码里已改名为 `src_reg`（[第 67 行](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L67)）。若在 ModelSim 里该信号显示为空，把 `.do` 里的 `src_sig` 改成 `src_reg` 即可——这是另一处「以源码为准」的过时引用。

## 6. 本讲小结

- `ff_synchroniser` 解决的是**单比特跨时钟域的亚稳态危害**，靠一串触发器（同步链）给亚稳态留恢复时间；它**不**解决窄脉冲被漏采的「事件覆盖」问题。
- 模块采用「同一 entity 多架构」模式，只有两套实现：Xilinx 用黑盒 `xpm_cdc_single`，Intel 用显式移位同步链 + `preserve` / `altera_attribute`（SDC 假路径 + 同步器识别）。
- `SYNC_SHIFT_FF`（范围 2~10）同时决定链长、目的域延迟、内部向量位宽；链越长延迟越大，但 MTBF 指数级提升（\(\text{MTBF}\propto e^{T_{res}/\tau}\)）。
- `SRC_INPUT_REG` 只在 Xilinx 架构里真正生效；Intel 架构无条件包含源域寄存进程，移植「无源时钟」场景时须留意。
- 测试台用 100 MHz → 25 MHz 的快→慢跨域配置，等待 `SYNC_SHIFT_FF + 1` 拍来对齐输出；文件头注释与 `.do` 脚本均有过时引用，一律以源码为准。

## 7. 下一步学习建议

- 下一讲 [u8-l2 多比特同步器 ff_synchroniser_vector](u8-l2-ff-synchroniser-vector.md) 解决「多比特信号为什么不能逐比特简单同步」——它会把 valid 位与数据拼接后用 `xpm_cdc_array_single` 一次同步，是异步 FIFO 指针跨域的基石。
- 随后进入 [第 9 单元 FIFO 设计](u9-l1-sync-fifo-behavioral.md)，你会在 `fifo_async` 里看到本讲的同步器被用来跨域传递格雷码指针，把本讲的单比特同步能力组合成完整的异步 FIFO。
- 想深入 MTBF 推导，可阅读 Xilinx/Intel 各自的「Metastability in FPGAs」应用笔记，对照本讲的公式理解工艺常数 \(\tau\)、\(T_{0}\)。
