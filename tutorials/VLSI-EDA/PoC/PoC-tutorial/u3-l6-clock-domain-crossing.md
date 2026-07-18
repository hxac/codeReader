# 时钟域穿越：misc/sync

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「为什么跨时钟域不能直接连一根线」，以及亚稳态（metastability）是怎么产生的。
- 区分 PoC 在 `src/misc/sync/` 下提供的五类同步器：`sync_Bits`（flag）、`sync_Reset`（复位）、`sync_Pulse`（脉冲）、`sync_Strobe`（选通）、`sync_Vector`（多位向量），并为给定信号选对同步器。
- 读懂两级 D-FF 同步器的移位寄存器结构，理解 `SYNC_DEPTH` 如何折中「可靠性 vs 延迟」。
- 看懂每个同步器里 `_async` 与 `_meta` 命名的信号，以及配套约束文件 `ucf/MetaStability.ucf` 为什么要对它们做 timing ignore。

本讲承接 u3-l2（厂商选择与可移植机制）。那里讲的「双层选择」框架在这里会被反复用到：`.files` 在编译期选厂商子实体，`generate` 在展开期分发。

---

## 2. 前置知识

在进入源码前，先用最朴素的语言建立两个直觉。

**直觉一：触发器是一个「采样器」。** 一个 D 触发器在每个时钟上升沿采样输入，把它「冻结」成输出。它要求输入在采样窗口（建立时间 `t_su` + 保持时间 `t_h`）内稳定。如果输入正好在窗口里翻转，触发器的输出就会在一段时间内停在 0 和 1 之间——这就是**亚稳态**。亚稳态最终会自行「决断」到 0 或 1，但决断所需时间是个随机变量。

**直觉二：采样窗口由「目的时钟」决定。** 信号从时钟域 A 跨到时钟域 B，B 的时钟是否采样到稳定值，只取决于「在 B 的边沿附近，信号是否稳定」。如果 A、B 是两个毫无相位关系的独立时钟，A 的数据随时可能恰好在 B 的采样窗口里翻转，单级触发器就有一定概率采到亚稳态。

**解决办法：串一串触发器。** 把第一级触发器的亚稳态输出，再送给第二级、第三级……每多一级，就多给一个时钟周期让亚稳态「自己决断」。决断时间越长，到后级时已是稳定值的概率越接近 1。这就是**多级同步器（synchronizer chain）**。

可靠性常用平均故障间隔时间（MTBF）衡量，其经典近似为：

\[
\mathrm{MTBF} \;=\; \frac{\exp\!\left(T_{\mathrm{res}} / \tau\right)}{f_{\mathrm{clk}} \cdot f_{\mathrm{data}} \cdot C}
\]

其中 \(T_{\mathrm{res}}\) 是留给亚稳态决断的时间（约为一个时钟周期减去建立时间），\(\tau\) 与 \(C\) 是器件工艺常数，\(f_{\mathrm{clk}}\)、\(f_{\mathrm{data}}\) 分别是目的时钟频率与数据翻转频率。注意分子是指数：每多一级同步器，\(T_{\mathrm{res}}\) 约多一个时钟周期，MTBF 会**指数级**上升。这就是「级数多一点，可靠性高很多」的数学根源，也是 `SYNC_DEPTH` 这个 generic 存在的理由。

还要记住一个工程铁律：**单 bit 的 flag 可以直接用同步器；多 bit 的总线绝不能逐 bit 同步**——因为各 bit 经过各自的同步链后会错位，得到的是「拼接出来的、从未存在过的值」。多位数据必须用握手（如 `sync_Vector`）或异步 FIFO（见 u3-l4）来传。本讲末尾的 `sync_Strobe` / `sync_Vector` 正是用来处理这一类「不能逐 bit 同步」的信号。

> 名词速查：CDC = Clock Domain Crossing（时钟域穿越）；FF = Flip-Flop（触发器）；TIG = Timing IGnore（时序忽略约束）；亚稳态 = metastability。

---

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/misc/sync/` 命名空间下，外加一份约束文件：

| 文件 | 作用 |
| --- | --- |
| `src/misc/sync/sync.pkg.vhdl` | 命名空间根包：定义 `T_MISC_SYNC_DEPTH` 子类型，集中声明 `sync_Bits`、`sync_Reset` 及其厂商子实体的 component。 |
| `src/misc/sync/sync_Bits.vhdl` | flag 同步器（通用包装实体），逐 bit 独立的多级 D-FF 同步，是其它同步器的「积木」。 |
| `src/misc/sync/sync_Reset.vhdl` | 复位专用同步器：异步置位、同步释放。 |
| `src/misc/sync/sync_Pulse.vhdl` | 脉冲同步器：把源域的极短脉冲拉伸成电平再同步（1+2 D-FF）。 |
| `src/misc/sync/sync_Strobe.vhdl` | 选通同步器：用 T-FF + 双向握手在两个时钟域间传单周期脉冲。 |
| `src/misc/sync/sync_Vector.vhdl` | 多位向量同步器：握手 + 数据捕获，安全传递一整条总线。 |
| `src/misc/sync/sync_Bits.files` | pyIPCMI 编译清单：演示厂商子实体的「编译期选择」。 |
| `ucf/MetaStability.ucf` | 亚稳态约束样板：对所有 `*_async` 信号 TIG，对所有 `*_meta*` 触发器分组后 TIG。 |

阅读建议：先读 `sync.pkg.vhdl` 看对外接口与 `SYNC_DEPTH` 取值范围；再读 `sync_Bits.vhdl` 吃透「多级移位寄存器」这一核心结构；最后 `sync_Reset` / `sync_Pulse` / `sync_Strobe` / `sync_Vector` 都是在 `sync_Bits` 基础上的变体。

---

## 4. 核心概念与源码讲解

### 4.1 同步器分类与选型

#### 4.1.1 概念说明

跨时钟域传的信号，按「它随时间怎么变化」可以分成几类，每类需要不同的同步策略。PoC 据此提供了五个核，名字直接对应信号类型：

| 信号类型 | 特征 | 对应核 | 核心机制 |
| --- | --- | --- | --- |
| flag（电平标志） | 长时间稳定，变化很慢 | `sync_Bits` | 多级 D-FF 直接采样 |
| reset（复位） | 必须立刻生效，但释放要干净 | `sync_Reset` | 异步置位 + 同步释放 |
| pulse（脉冲） | 极短，可能短于目的时钟周期 | `sync_Pulse` | 源域先把脉冲「锁存成电平」再同步 |
| strobe（选通） | 单周期高有效脉冲，两边都要反馈 | `sync_Strobe` | T-FF 翻转 + 双向握手 |
| vector（多位总线） | 多 bit 必须整体到达 | `sync_Vector` | 握手 + 数据捕获 |

**为什么不能只用一个 `sync_Bits` 通吃？** 因为「采样」解决不了所有问题：

- 一个目的周期都撑不到的窄脉冲，会被目的时钟「漏采」——必须先在源域把它变成电平（`sync_Pulse`）。
- 复位信号如果「同步置位」，那器件在上电后、复位到达前会乱跑——所以复位要「异步立刻拉高、同步慢慢释放」（`sync_Reset`）。
- 多 bit 总线逐位同步会错位——必须用握手保证「整体更新」（`sync_Vector`）。

#### 4.1.2 核心流程

给定一个待跨域信号，选型决策树如下：

```text
待跨域信号
  │
  ├─ 是复位？ ───────────► sync_Reset
  │
  ├─ 单 bit，且变化慢（flag）？ ──► sync_Bits
  │
  ├─ 单 bit，但很窄（pulse/strobe）？
  │     ├─ 只需源→目的单向、可容忍「忙」反馈 ─► sync_Strobe
  │     └─ 否则 ──────────────────────► sync_Pulse
  │
  └─ 多 bit 总线？ ───────► sync_Vector（或异步 FIFO）
```

#### 4.1.3 源码精读

五类同步器的对外端口都能在根包里看到（注意 `sync.pkg.vhdl` 只声明了 `sync_Bits` 与 `sync_Reset` 两类 component，其余三个核在内部用 `entity PoC.sync_Bits` 直接例化，故不需要 component 声明）。

`T_MISC_SYNC_DEPTH` 把同步深度限定在 2 到 16 之间，2 是最低保障：

[src/misc/sync/sync.pkg.vhdl:37-51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync.pkg.vhdl#L37-L51) — 根包定义 `T_MISC_SYNC_DEPTH`（2..16）与 `sync_Bits` 组件声明：`BITS`（同步多少位）、`INIT`（初值）、`SYNC_DEPTH`（同步链级数）三个 generic，以及 `Clock`/`Input`/`Output` 三端口。

[src/misc/sync/sync.pkg.vhdl:79-88](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync.pkg.vhdl#L79-L88) — `sync_Reset` 组件声明：没有 `BITS`（复位就是 1 bit），只有 `SYNC_DEPTH`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：用本节的决策树，为 5 个真实信号选对同步器。
2. **步骤**：打开 `sync.pkg.vhdl` 与四个同步器文件头部注释里的 `Entity:` / `.. ATTENTION::` 行，对照下表填写。
3. **需要观察的现象**：每个核注释里的 ATTENTION 都明确写了「只用于哪类信号」——这是 PoC 作者给使用者的硬约束。
4. **预期结果**（参考答案）：

   | 信号 | 选择 |
   | --- | --- |
   | 按键消抖后的「长按」电平 | `sync_Bits` |
   | 外部按钮产生的异步复位 | `sync_Reset` |
   | 源域一个时钟周期宽的「启动转换」脉冲 | `sync_Strobe`（或 `sync_Pulse`） |
   | 从慢域读到快域的 8 位 ADC 结果 | `sync_Vector` |
   | 某中断「发生过」的单 bit 标志 | `sync_Bits` |

5. **待本地验证**：选型只是设计阶段判断，是否真的安全取决于你的两个时钟频率比与器件工艺，最终要靠时序报告里的同步器 MTBF 来确认。

#### 4.1.5 小练习与答案

**练习 1**：能不能用 `sync_Bits` 来同步一个「只有源域半个周期宽」的脉冲？为什么？

> **答**：不能可靠地用。该脉冲可能整个落在目的时钟的两个采样沿之间，被完全漏采。`sync_Bits` 只适合「变化足够慢、保证至少被采到一次」的 flag；窄脉冲要用 `sync_Pulse` 或 `sync_Strobe`。

**练习 2**：能不能用 `sync_Bits`（`BITS=>8`）来同步一条 8 位地址总线？

> **答**：不能。8 个 bit 各自走独立的同步链，链路延迟可能不同，目的域会拼出「高位是上一拍、低位是这一拍」的错位值。多 bit 必须用 `sync_Vector` 的握手捕获或异步 FIFO，保证整体一致更新。

---

### 4.2 flag 同步器 sync_Bits 与 SYNC_DEPTH

#### 4.2.1 概念说明

`sync_Bits` 是最基础、也最重要的同步器：把一个或多个**彼此独立**的慢变 flag，逐位送入一条多级 D-FF 移位寄存器。它是 `sync_Strobe`、`sync_Vector` 内部直接例化的「积木」（见 4.4、4.5）。`SYNC_DEPTH` 控制这条移位寄存器有几级，直接对应上一节 MTBF 公式里的 \(T_{\mathrm{res}}\)：级数越多，亚稳态决断时间越长，MTBF 指数级上升，但跨域延迟也线性增加。

#### 4.2.2 核心流程

每一位的处理完全相同且互相独立，用 `for i in 0 to BITS-1 generate` 展开。每位的信号链是：

```text
Input(i) ──► Data_async ──► [Data_meta] ──► [Data_sync(1)] ──► ... ──► [Data_sync(SYNC_DEPTH-1)] ──► Output(i)
            (组合缓冲)      (第1级FF,      (第2级FF)                  (第SYNC_DEPTH级FF)
                              易亚稳态)
```

- `Data_async`：纯组合缓冲，给待同步信号一个「名字」与驱动点，本身不解决亚稳态。
- `Data_meta`：第 1 级采样 FF，**最可能进入亚稳态**的一级——因为它的输入直接来自异步域。
- `Data_sync(1..SYNC_DEPTH-1)`：后续各级，给亚稳态留出决断时间。`Output` 取最高位 `Data_sync(SYNC_DEPTH-1)`。

总级数 = `Data_meta`(1) + `Data_sync`(SYNC_DEPTH-1) = **SYNC_DEPTH** 级。

#### 4.2.3 源码精读

先看实体声明，三个 generic 的含义：

[src/misc/sync/sync_Bits.vhdl:68-79](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L68-L79) — `sync_Bits` 实体：`BITS`（位数，默认 1）、`INIT`（各级 FF 的复位初值）、`SYNC_DEPTH`（默认取 `T_MISC_SYNC_DEPTH'low` = 2）。端口中 `Clock` 是**目的域**时钟，`Input` 标注 `@async`，`Output` 标注 `@Clock`。

`INIT_I` 把用户传入的 `INIT` 先 `descend`（转成 downto 方向）、再 `resize` 到 `BITS` 位宽，得到每位的初值。厂商分支用 `DEV_INFO.Vendor` 做展开期选择（u3-l2 讲过的双层选择）：

[src/misc/sync/sync_Bits.vhdl:82-94](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L82-L94) — 定义 `INIT_I` 与 `DEV_INFO`，并进入 `genGeneric`（非 Altera、非 Xilinx 时展开）。注意 `Data_meta` 与 `Data_sync` 的初值都初始化为 `INIT_I(i)`，并标注 `:= ...`。

`Data_sync` 的下标范围 `SYNC_DEPTH-1 downto 1`，正好是 `SYNC_DEPTH-1` 个元素，加上 `Data_meta` 共 SYNC_DEPTH 级。移位与输出逻辑：

[src/misc/sync/sync_Bits.vhdl:104-114](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L104-L114) — 核心移位：`Data_meta <= Data_async`；`Data_sync <= Data_sync(high-1 downto 1) & Data_meta`（整体左移，最高位被推出为输出，`Data_meta` 进入最低位）；`Output(i) <= Data_sync(high)`。

这里有两个关键的 Xilinx 综合属性（见 4.6 节约束）：

[src/misc/sync/sync_Bits.vhdl:96-101](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L96-L101) — `ASYNC_REG of Data_meta is "TRUE"`（告诉综合器这是异步域进来的第一级，别乱优化）；`SHREG_EXTRACT of Data_meta/Data_sync is "NO"`（禁止把移位寄存器折叠进 SRL 查找表，必须保持独立 FF，否则同步链就毁了）。

厂商分支把活儿交给专用子实体（u3-l2 的模式）：

[src/misc/sync/sync_Bits.vhdl:119-146](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L119-L146) — `genAltera` 例化 `sync_Bits_Altera`、`genXilinx` 例化 `sync_Bits_Xilinx`，generic/port 一一映射。三个 `generate` 分支互斥（由 `VENDOR` 守卫），保证任一器件只展开一条实现。

> 编译期那一半的选择在 `sync_Bits.files` 里：
> [src/misc/sync/sync_Bits.files:11-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.files#L11-L24) — 先编 `sync.pkg.vhdl`；若 `DeviceVendor="Altera"` 则引入 `lib/Altera.files` 并编 `sync_Bits_Altera.vhdl`，若是 Xilinx 同理引入 Xilinx 原语库与 `sync_Bits_Xilinx.vhdl`；最后才编作为 Top-Level 包装的 `sync_Bits.vhdl`。pyIPCMI 据此只把「用得到的那一份」厂商实现喂给工具链。

#### 4.2.4 代码实践（参数实验型）

1. **目标**：直观感受 `SYNC_DEPTH` 如何决定同步链长度。
2. **步骤**：在源码里把 `SYNC_DEPTH` 当成变量，手算两种取值下每位产生的 FF 数量。
3. **需要观察的现象**：`Data_sync` 的下标范围随 `SYNC_DEPTH` 变化。
4. **预期结果**：
   - `SYNC_DEPTH = 2`（默认）：`Data_sync` 范围 `1 downto 1` = 1 个元素；加 `Data_meta` 共 **2** 级 FF。
   - `SYNC_DEPTH = 4`：`Data_sync` 范围 `3 downto 1` = 3 个元素；加 `Data_meta` 共 **4** 级 FF，跨域延迟 4 个目的时钟周期。
5. **待本地验证**：用 GHDL/NVC 或 Vivado 综合后查看寄存器报告，确认每个同步位真的生成了 `SYNC_DEPTH` 个 FF（且没被合并进 SRL）。

#### 4.2.5 小练习与答案

**练习 1**：某设计目的时钟 250 MHz，数据翻转频繁，`SYNC_DEPTH=2` 时 MTBF 只有几分钟。把 `SYNC_DEPTH` 改成 4，MTBF 大致如何变化？

> **答**：按本节 MTBF 公式，多 2 级 ≈ 多 2 个周期的决断时间 \(T_{\mathrm{res}}\)，分子是 \(\exp(T_{\mathrm{res}}/\tau)\)，每级使 MTBF 乘以约 \(\exp(T_{\mathrm{clk}}/\tau)\)（一个很大的数）。从 2 级到 4 级通常让 MTBF 提升若干个数量级（从「分钟」到「数百年」量级是常见的）。这就是为什么关键路径上多挂一两级 FF 收益巨大。

**练习 2**：`Output` 为什么取 `Data_sync(Data_sync'high)` 而不是 `Data_meta`？

> **答**：`Data_meta` 是直接采样异步输入的第一级，最可能处于亚稳态；必须经过后续若干级让它决断到稳定值后，才能作为可信输出。`high`（最高位）是离亚稳态源头最远、决断时间最长的一级。

---

### 4.3 复位同步器 sync_Reset

#### 4.3.1 概念说明

复位是一条特殊的跨域信号。对它有两个看似矛盾的要求：

- **要立刻生效**：一旦外部复位拉起，整个器件最好「马上」进入复位态，不能等目的时钟。
- **要干净地释放**：复位释放的瞬间，绝不能让某些触发器看到「释放」、另一些没看到，否则状态机一上电就乱。

`sync_Reset` 用「**异步置位、同步释放**」（async assert, sync deassert）同时满足两者：复位有效时，组合逻辑立刻把链路全部拉高；复位撤掉时，用一个同步链让「释放」沿时钟整齐传播。

#### 4.3.2 核心流程

```text
              ┌─────────────────────────────────────┐
Input(async) ─┤  = '1' ?                            ├─► Output (复位输出)
              │   异步：Data_meta<=1, Data_sync<=全1 │     （异步拉高、同步释放）
              │  否则在每个 Clock 沿：              │
              │   Data_meta<=0; Data_sync 左移      │
              └─────────────────────────────────────┘
                  process(Clock, Data_async)
```

- 复位**有效**（`Data_async='1'`）：异步地把所有 FF 置 1，`Output` 立刻为 1 ——「异步置位」。
- 复位**撤掉**：每个 `Clock` 沿，0 值像 `sync_Bits` 那样逐级移入，若干周期后 `Output` 才同步变 0 ——「同步释放」。

#### 4.3.3 源码精读

实体只有 `SYNC_DEPTH` 一个 generic（复位就是单 bit）：

[src/misc/sync/sync_Reset.vhdl:60-69](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Reset.vhdl#L60-L69) — `sync_Reset` 实体：`Input` 是异步复位输入，`Output` 是同步到 `Clock` 域的复位输出。

注意一个细节：`sync_Reset` 的厂商守卫用的是裸常量 `VENDOR`，而 `sync_Bits` 用的是 `DEV_INFO.Vendor`。两者都来自 config 包（`VENDOR` 是 config 暴露的命名器件厂商常量，`DEV_INFO.Vendor` 是 `T_DEVICE_INFO` 记录里的同名字段），语义一致，只是访问写法不同：

[src/misc/sync/sync_Reset.vhdl:74-80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Reset.vhdl#L74-L80) — `genGeneric` 守卫 `if (VENDOR /= VENDOR_ALTERA) and (VENDOR /= VENDOR_XILINX)`；信号初值都设成 `'1'`（上电即处于复位态），`ASYNC_REG` 同时标在 `Data_meta` 和 `Data_sync` 上。

关键的「异步置位、同步释放」进程——注意敏感列表里同时有 `Clock` 和 `Data_async`：

[src/misc/sync/sync_Reset.vhdl:93-102](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Reset.vhdl#L93-L102) — `if (Data_async = '1')` 分支异步地把 `Data_meta`、`Data_sync` 全置 1（复位立刻生效）；`elsif rising_edge(Clock)` 分支把 0 移入链路（复位同步释放）。`Output <= Data_sync(high)`。

> 提示：与 `sync_Bits` 相比，这里 `Data_sync` 的范围是 `SYNC_DEPTH-1 downto 0`（含 0），所以默认 `SYNC_DEPTH=2` 时复位释放链有 `Data_meta + Data_sync(1..0)` 共 3 级——复位同步器通常比普通 flag 同步器多一级，给释放沿更稳的同步。

#### 4.3.4 代码实践（源码追踪型）

1. **目标**：在源码里定位「异步置位」和「同步释放」这两段逻辑，确认它们落在同一个进程。
2. **步骤**：打开 `sync_Reset.vhdl` 第 93–102 行，回答：进程的敏感列表是什么？哪个分支负责立刻生效？哪个分支负责干净释放？
3. **需要观察的现象**：`if (Data_async = '1')` 不依赖 `Clock`，所以 `Output` 在复位有效时与时钟无关地变高。
4. **预期结果**：敏感列表 = `(Clock, Data_async)`；`Data_async='1'` 分支 = 异步置位；`rising_edge(Clock)` 分支 = 同步释放。
5. **待本地验证**：写一个最小测试台，给 `Input` 一个短脉冲复位，观察 `Output` 是「立刻拉高、但撤掉后过几拍才变低」。

#### 4.3.5 小练习与答案

**练习 1**：为什么复位释放必须「同步」，而置位可以「异步」？

> **答**：复位释放的瞬间，如果不同步，不同触发器会在不同时钟沿看到「释放」，导致状态机上电后进入不可预测状态。而复位置位是「把所有 FF 强制清成已知值」，越快越好，与时钟无关反而更安全，所以可以异步。

**练习 2**：`sync_Reset` 输出的复位，文档头建议怎么走线到下游各 FF？

> **答**：见文件头注释（`sync_Reset.vhdl` 第 16–19 行）——`Output` 应经**全局缓冲**（global buffer，如 BUFG）扇出到目的 FF 的复位端，保证在一个时钟周期内到达所有目标，避免复位释放再次出现偏斜。

---

### 4.4 脉冲与选通同步器：sync_Pulse 与 sync_Strobe

#### 4.4.1 概念说明

当源域信号是一个**短脉冲**（可能比目的时钟周期还窄），直接用 `sync_Bits` 会被漏采。两个核各自解决一种窄信号场景：

- **`sync_Pulse`**：源域来了一个极短脉冲，先在源域用一个 FF 把它「锁存成电平」，再用 2 级 D-FF 同步过去；目的域采到后，反馈把电平清掉。结构是「1（源域锁存）+ 2（同步）」共 1+2 D-FF，文档称 "1+2 D-FF synchronizer"。
- **`sync_Strobe`**：处理「单周期高有效」的选通脉冲，且**两个方向都需要反馈**。它用 T-FF（翻转触发器）把脉冲翻成电平变化，过 `sync_Bits` 同步到对岸，再用 XOR 还原成脉冲；同时算出一个 `Busy` 信号回灌源域，阻止「上一个还没传完就来下一个」。这是一个闭环握手。

#### 4.4.2 核心流程

**sync_Pulse**（源域锁存 → 同步 → 反馈清除）：

```text
源域:  Input(i) 上升沿 ──► Data_async 置 1（锁存为电平）
目的域: Data_async ──► [meta] ──► [sync] ──► Output
反馈:  当 (Input 已回 0) 且 (Output 已为 1) ──► Data_async 清 0
```

**sync_Strobe**（T-FF 翻转 + 双向握手 + XOR 还原）：

```text
Clock1 域: Input 上升沿 ──► T1 翻转（strobe→电平翻转）  Busy = T1 ⊕ 回采
            T1 ──► sync_Bits(到 Clock2) ──► syncClk2_Out
Clock2 域: D2 缓存 syncClk2_Out；Changed_Clk2 = syncClk2_Out ⊕ D2 ──► Output（还原出脉冲）
            syncClk2_Out ──► sync_Bits(回 Clock1) ──► syncClk1_Out ──► 参与 Busy 计算
```

每来一个 strobe，`T1` 翻转一次；对岸检测到电平变化（XOR）就还原出一个脉冲；`Busy` 在「翻转尚未被对岸确认」期间为 1，回灌源域阻止新输入。

#### 4.4.3 源码精读

`sync_Pulse` 的源域锁存进程——把窄脉冲变电平，并在传完后清除：

[src/misc/sync/sync_Pulse.vhdl:101-108](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Pulse.vhdl#L101-L108) — `process(Input(i), Data_sync(high))`：`Input` 上升沿时 `Data_async <= '1'`（锁存脉冲为电平）；当 `(not Input and Data_sync(high))='1'`（输入已撤、且已同步过去）时 `Data_async <= '0'`（清除，等待下一次）。

随后是标准的两级同步（与 `sync_Bits` 同构）：

[src/misc/sync/sync_Pulse.vhdl:110-116](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Pulse.vhdl#L110-L116) — 目的域 `rising_edge(Clock)` 把 `Data_async` 移入 `Data_meta`、`Data_sync`，输出取最高位。

> ⚠️ **源码注意点（待本地验证）**：在本 HEAD，`sync_Pulse` 的 generic 分支里 `Data_meta`/`Data_sync` 的初值写成了 `INIT_I(i)`（第 90、91 行），但该实体的 generic 列表里**并没有** `INIT`，架构里也**未定义**常量 `INIT_I`（对比 `sync_Bits` 第 83 行有 `constant INIT_I ...`）。这意味着在「通用厂商」分支下展开时，这一句会因 `INIT_I` 未声明而无法编译。Altera/Xilinx 分支不受影响（它们例化的是厂商子实体）。阅读时把它当作「设计意图 = 像 sync_Bits 那样给各级 FF 一个初值」来理解即可；若你要在 Generic 器件上仿真/综合该核，需要先补上 `INIT` generic 与 `INIT_I` 常量定义，或本地确认该问题是否已在下游修复。

`sync_Strobe` 的 T-FF 翻转（注意 `GATED_INPUT_BY_BUSY` 控制是否用 `Busy` 回灌屏蔽新输入）：

[src/misc/sync/sync_Strobe.vhdl:95-108](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Strobe.vhdl#L95-L108) — `D0` 延迟 `Input` 用于上升沿检测；`Changed_Clk1 = not D0 and Input(i)` 检出新 strobe；`T1 <= (Changed_Clk1 and not Busy_i) xor T1`（带 busy 屏蔽的 T-FF）或 `T1 <= Changed_Clk1 xor T1`（不屏蔽）。

还原与 busy 计算（纯组合）：

[src/misc/sync/sync_Strobe.vhdl:110-123](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Strobe.vhdl#L110-L123) — `D2` 在 `Clock2` 沿缓存对岸回采；`Changed_Clk2 = syncClk2_Out xor D2` 还原出目的域脉冲给 `Output`；`Busy_i = T1 xor syncClk1_Out` 告诉源域「上一笔尚未完成」。

最后，`sync_Strobe` 直接例化两个 `sync_Bits`，一个把 `T1` 送到 Clock2，一个把回采送回 Clock1：

[src/misc/sync/sync_Strobe.vhdl:126-146](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Strobe.vhdl#L126-L146) — `syncClk2 : entity PoC.sync_Bits` 与 `syncClk1 : entity PoC.sync_Bits`，这就是「直接实体例化」（无需 component 声明），也是 `sync.pkg.vhdl` 里没有 `sync_Strobe` 组件声明的原因。

#### 4.4.4 代码实践（源码追踪型）

1. **目标**：跟踪 `sync_Strobe` 一个 strobe 从 Clock1 到 Clock2 的完整旅程。
2. **步骤**：沿 4.4.2 的流程图，在源码里把每一步对应的信号与行号标出来。
3. **需要观察的现象**：`Busy` 在「T1 已翻转但尚未被对岸确认」时为 1。
4. **预期结果**：`Input`↑ → `Changed_Clk1`→`T1` 翻转 → `sync_Bits`(→Clock2) → `syncClk2_Out` 变 → `Changed_Clk2` = XOR 还原 → `Output` 出一个 Clock2 周期脉冲；同时 `syncClk2_Out` 经 `sync_Bits`(→Clock1) 回采，`T1 ⊕ syncClk1_Out` 变 0 表示完成，`Busy` 撤销。
5. **待本地验证**：写测试台给两个不同频率的时钟，连续发两个 strobe，观察 `Busy` 期间第二个 strobe 是否被吞掉（`GATED_INPUT_BY_BUSY=TRUE` 时）。

#### 4.4.5 小练习与答案

**练习 1**：`sync_Strobe` 的 `Busy` 信号对设计者意味着什么使用约束？

> **答**：`Busy=1` 表示「上一个 strobe 还在对岸同步链里传，尚未被确认」。设计者应在源域用 `Busy` 来**反压**（gate）新输入；若 `GATED_INPUT_BY_BUSY=TRUE`，核内部已自动屏蔽，否则需在设计里自己判断。忽略 `Busy` 连发 strobe 会丢脉冲。

**练习 2**：`sync_Pulse` 与 `sync_Strobe` 都处理窄脉冲，何时该选哪个？

> **答**：`sync_Pulse` 是单向的（源→目的），结构更轻（1+2 FF），适合「目的域只需被唤醒一次、不需要回告」的场景；`sync_Strobe` 是带 `Busy` 反馈的闭环握手，适合「源域必须知道脉冲是否已被安全接收、不能丢」的高可靠场景，但延迟更大、资源更多。

---

### 4.5 多位向量同步器 sync_Vector

#### 4.5.1 概念说明

多位总线不能逐 bit 同步（见 4.1.5）。`sync_Vector` 的思路是：**只同步一个「数据变了」的握手信号**（1 bit，可以用 `sync_Bits`），等目的域确认「变了」之后，再**整体捕获**这条总线。这样总线各 bit 不需要各自走同步链，而是被当成「准静态」数据，在握手完成时整批快照。

#### 4.5.2 核心流程

```text
Clock1 域:
  比较新旧 Input ──► Changed_Clk1（只看 MASTER_BITS 高位是否变化）
  if not Busy: D0 <= Input（快照）, T1 翻转（通知对岸“变了”）

握手:
  T1 ──► sync_Bits(→Clock2) ──► syncClk2_Out
  syncClk2_Out ──► sync_Bits(→Clock1) ──► syncClk1_Out ──► Busy = T1 ⊕ syncClk1_Out

Clock2 域:
  Changed_Clk2 = syncClk2_Out ⊕ D2（还原出“变了”脉冲）
  if Changed_Clk2: D4 <= D0（整体捕获总线快照）  ──► Output = D4
```

要点：数据走的是 `D0`（源域寄存）→ `D4`（目的域捕获）这条「非同步」通路，靠握手保证捕获时刻稳定；真正跨亚稳态边界的只有 `T1` 这 1 bit。

#### 4.5.3 源码精读

实体把总线拆成 `MASTER_BITS`（参与变化比较与传输的主部位）和 `SLAVE_BITS`（附加位）：

[src/misc/sync/sync_Vector.vhdl:50-65](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Vector.vhdl#L50-L65) — `MASTER_BITS`（默认 8）+ `SLAVE_BITS`（默认 0）合成总位宽；端口有 `Clock1`/`Clock2`/`Input`/`Output`，外加 `Busy`（回告源域）与 `Changed`（告知目的域「有新值」）。

源域进程——`Busy` 闸控下快照输入并翻转握手位：

[src/misc/sync/sync_Vector.vhdl:97-105](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Vector.vhdl#L97-L105) — `if (Busy_i='0')` 才 `D0 <= Input`（捕获新值）且 `T1 <= T1 xor Changed_Clk1`（翻转通知）。

目的域进程——握手完成时**整体**捕获总线：

[src/misc/sync/sync_Vector.vhdl:108-118](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Vector.vhdl#L108-L118) — `if (Changed_Clk2='1') then D4 <= D0;` 把源域快照整批搬过来，`Output <= D4`。

变化检测只比较 `MASTER_BITS`：

[src/misc/sync/sync_Vector.vhdl:124-126](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Vector.vhdl#L124-L126) — `Changed_Clk1 <= '0' when D0(MASTER_BITS-1 downto 0) = Input(...) else '1'`；`Changed_Clk2 = syncClk2_Out xor D2`；`Busy_i = T1 xor syncClk1_Out`。

同样直接例化两个 `sync_Bits`（每位 1 bit 同步）：

[src/misc/sync/sync_Vector.vhdl:133-153](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Vector.vhdl#L133-L153) — `syncClk2`/`syncClk1` 两条握手各用一个 `BITS=>1` 的 `sync_Bits`。

#### 4.5.4 代码实践（源码追踪型）

1. **目标**：确认「真正跨亚稳态的只有 1 bit，而总线是整体快照」。
2. **步骤**：在源码里分别标出（a）走 `sync_Bits` 同步链的信号、（b）直接从源域寄存器搬到目的域寄存器的数据通路。
3. **需要观察的现象**：`D4 <= D0` 的更新条件是 `Changed_Clk2='1'`，即「握手确认到达」那一刻才捕获。
4. **预期结果**：（a）只有 `T1`/`syncClk1_In` 各 1 bit 过 `sync_Bits`；（b）`Input → D0(Clock1) → D4(Clock2) → Output` 是跨域数据通路，靠 `Busy`/`Changed` 握手保证 `D4` 捕获时 `D0` 已稳定。
5. **待本地验证**：测试台里在 `Busy=1` 期间连续改 `Input`，观察 `Output` 是否只更新成「`Busy` 撤销前最后一次握手确认的那个值」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `Changed_Clk1` 只比较 `MASTER_BITS`，而不是整条 `MASTER_BITS+SLAVE_BITS`？

> **答**：这是设计取舍——`MASTER_BITS` 通常是「真正会触发下游动作」的关键位（如地址、命令），只要它们变就触发一次握手传输；`SLAVE_BITS` 是随路数据，跟着主部位一起被 `D0`/`D4` 快照传过去。这样能减少不必要的握手。如果你的 `SLAVE_BITS` 也必须独立触发传输，就需要重新设计触发条件。

**练习 2**：相比「每位各挂一个 `sync_Bits`」，`sync_Vector` 的代价是什么、好处是什么？

> **答**：好处是数据各 bit 不需各自走同步链，避免了错位，且数据通路延迟小（寄存器到寄存器一拍）；代价是**吞吐受限**——一次握手只能传一个「快照」，新值在 `Busy` 期间被反压（连续变化会被合并）。所以它适合「偶发更新的配置/状态总线」，不适合连续高速数据流（那该用 u3-l4 的异步 FIFO）。

---

### 4.6 时序约束：_meta 与 _async 信号

#### 4.6.1 概念说明

同步器是「故意」把异步信号接进来的，第一级 FF 注定可能亚稳态。但综合器/时序分析器默认会按「所有路径都必须满足建立/保持时间」来检查，于是它会报错：从异步源到 `Data_meta` 的路径「违例」。这些违例是**预期内、无法消除**的——我们就是靠多级 FF 来容忍它，而不是靠时序收敛。

所以必须告诉工具：**这些路径不要查时序**。这就是「亚稳态约束」。PoC 用一套**命名约定** + 一份**通配约束**来自动化这件事：

- 凡是异步输入信号，命名带 `_async` 后缀（如 `Data_async`）。
- 凡是可能亚稳态的第一级 FF，命名带 `_meta`（如 `Data_meta`）。
- 约束文件用通配符 `*_async`、`*_meta*` 一次性给它们打上 TIG（Timing Ignore）。

`sync_Bits`、`sync_Reset` 文件头注释的 `Constraints:` 段（见 `sync_Bits.vhdl` 第 19–24 行）正是反复强调：「Please add constraints for meta stability to all '_meta' signals and timing ignore constraints to all '_async' signals.」

#### 4.6.2 核心流程

`ucf/MetaStability.ucf` 用三行 UCF（Xilinx ISE 约束语法）覆盖所有同步器：

```text
NET "*_async"                TIG;                              -- 1) 异步输入网线：整条路径不查时序
INST "*_meta*"               TNM = "METASTABILITY_FFS";        -- 2) 亚稳态 FF 归为一组
TIMESPEC "TS_MetaStability" = FROM FFS TO "METASTABILITY_FFS" TIG;  -- 3) 任何 FF 到该组的路径都不查时序
```

- 第 1 行：所有名字匹配 `*_async` 的网线，其路径直接 TIG（timing ignore）。
- 第 2 行：所有名字匹配 `*_meta*` 的触发器实例归入命名组 `METASTABILITY_FFS`。
- 第 3 行：定义时间规格——从任何 FF 出发、到达 `METASTABILITY_FFS` 组的路径，一律 TIG。

三条合起来：把「异步输入→第一级亚稳态 FF」这条本就不可能收敛的路径，从时序分析里彻底摘出去，让工具把精力放在真正可收敛的路径上。

#### 4.6.3 源码精读

约束样板文件本体：

[ucf/MetaStability.ucf:4-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf#L4-L6) — 三行 UCF：`NET "*_async" TIG;`、`INST "*_meta*" TNM = "METASTABILITY_FFS";`、`TIMESPEC "TS_MetaStability" = FROM FFS TO "METASTABILITY_FFS" TIG;`。

命名约定在源码里的体现——`sync_Bits` 的 `Data_async` / `Data_meta` 正好命中通配符：

[src/misc/sync/sync_Bits.vhdl:92-93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L92-L93) — 信号名 `Data_async`（命中 `NET "*_async"`）与 `Data_meta`（命中 `INST "*_meta*"`）。只要不擅自改这两个名字，通配约束就自动生效。

除了 UCF 通配，`sync_Bits` 还在源码里直接写了 Xilinx 综合属性，作为「源码内嵌约束」兜底：

[src/misc/sync/sync_Bits.vhdl:96-101](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L96-L101) — `ASYNC_REG of Data_meta is "TRUE"`（标注异步寄存器，指导布局与 ASYNC_REG 链）；`SHREG_EXTRACT ... is "NO"`（禁止把同步链吸进 SRL 查找表）。

> 这两层约束的关系：UCF 是给时序分析器看的（TIG，告诉它「别查」）；`ASYNC_REG`/`SHREG_EXTRACT` 是给综合/布局器看的（告诉它「这些 FF 必须相邻摆放、别合并」）。二者配合才能让同步链真正可靠。

#### 4.6.4 代码实践（动手写约束型）

1. **目标**：为一个异步复位信号选对同步核，并写出配套的 `_async` / `_meta` 约束。
2. **步骤**：
   1. 选核：异步复位 → `sync_Reset`（异步置位、同步释放，见 4.3）。
   2. 例化（示例代码，非项目原有）：
      ```vhdl
      rst_sync : entity PoC.sync_Reset
        generic map ( SYNC_DEPTH => 3 )
        port map ( Clock => clk_core, Input => ext_reset_n_async, Output => rst_core_sync );
      ```
   3. 写约束：把 `ucf/MetaStability.ucf` 的三行加入工程，或针对该例化实例单独写：
      ```text
      NET  "ext_reset_n_async" TIG;
      INST "rst_sync/Data_meta" TNM = "METASTABILITY_FFS";
      TIMESPEC "TS_MetaStability" = FROM FFS TO "METASTABILITY_FFS" TIG;
      ```
3. **需要观察的现象**：加入约束后，时序报告中「到 `Data_meta` 的路径」应被标记为 ignored/TIG，不再算违例。
4. **预期结果**：复位路径不再报 setup 违例；`ASYNC_REG` 让 `Data_meta` 与后续 FF 被相邻布局。
5. **待本地验证**：具体约束语法依工具而异——ISE 用上面的 UCF，Vivado 改用 XDC（`set_false_path` / `set_property ASYNC_REG TRUE`），Quartus 用 SDC（`set_false_path` + `ALTERA_ATTRIBUTE` 注入）。本仓库 `ucf/` 以 UCF 为主，跨工具细节需对照各厂商文档。

#### 4.6.5 小练习与答案

**练习 1**：如果把 `sync_Bits` 内部信号 `Data_meta` 改名成 `Data_stage1`，会发生什么？

> **答**：`ucf/MetaStability.ucf` 里的 `INST "*_meta*"` 与 `NET "*_async"` 就不再命中它，亚稳态约束失效，时序分析会重新报「到该 FF 的异步路径违例」。这正是 PoC 强制 `_meta`/`_async` 命名约定的原因——约束靠名字生效，名字是接口的一部分，不能随便改。

**练习 2**：TIG（Timing Ignore）是「让路径不满足时序」吗？

> **答**：不是。TIG 是「让时序分析器**不检查**这条路径」，而不是「让这条路径变快/变慢」。对同步器第一级，这条路径本来就不可能、也不需要满足建立/保持时间——我们靠多级 FF 来容忍亚稳态，而不是靠时序收敛。TIG 只是诚实地把这一点告诉工具，避免它浪费精力去优化一条注定「违例」的路径。

---

## 5. 综合实践

**任务：给一个小双时钟域设计配上完整的同步与约束方案。**

场景：你有一个慢控制域 `clk_ctrl`（10 MHz，按钮、配置寄存器）和一个快数据域 `clk_data`（100 MHz，数据处理）。需要跨域传递 4 类信号：

1. 一个外部硬件复位按钮产生的异步复位 `ext_rst_n`（异步、需接入 `clk_data` 域）。
2. 一个 `clk_ctrl` 域产生的「长按标志」`locked_flag`（慢变单 bit）。
3. 一个 `clk_ctrl` 域发出的「启动一次采集」单周期脉冲 `start_pulse`。
4. 一条 12 位的配置向量 `cfg_value`，由 `clk_ctrl` 偶发更新，供 `clk_data` 使用。

请完成：

1. **选型**：为 4 路信号各选一个 `sync_*` 核，说明理由（对照 4.1 决策树）。
2. **实例化**：写出 4 个同步器的 VHDL 例化代码（generic 自定，`SYNC_DEPTH` 给出你的选择与理由）。
3. **约束**：把 `ucf/MetaStability.ucf` 纳入工程，并指出每一路同步器的哪些内部信号会被 `*_async` / `*_meta*` 通配符自动覆盖；如有信号名不命中，补充单点约束。
4. **风险自查**：指出本方案中哪一路的「吞吐」受握手限制（提示：第 4 路），并说明若 `cfg_value` 变成「每周期都变」的高速流，该换成 u3-l4 的哪种核。

> 参考方向：(1) 用 `sync_Reset`；(2) 用 `sync_Bits`（`BITS=>1`）；(3) 用 `sync_Strobe`（需 `Busy` 反压）或 `sync_Pulse`；(4) 用 `sync_Vector`（`MASTER_BITS=>12`），偶发更新合适，高速流应换异步 FIFO（`fifo_ic_got`）。约束方面，四路的 `Data_async`/`Data_meta`（及 `sync_Strobe`/`sync_Vector` 内部 `sync_Bits` 的同名信号）均自动命中通配符；外部端口 `ext_rst_n` 若不含 `_async` 后缀，需补一条 `NET "ext_rst_n" TIG;`。

---

## 6. 本讲小结

- 跨时钟域的根本敌人是**亚稳态**：第一级采样 FF 可能停在中间电平，靠**多级 D-FF** 串联给它决断时间，MTBF 随级数**指数级**改善——这是 `SYNC_DEPTH` 存在的数学理由。
- PoC 按信号形态提供五类同步器：`sync_Bits`（flag）、`sync_Reset`（复位：异步置位/同步释放）、`sync_Pulse`（窄脉冲：源域锁存为电平）、`sync_Strobe`（单周期选通：T-FF + 双向握手 + `Busy` 反压）、`sync_Vector`（多位总线：握手 + 整体捕获）。
- `sync_Bits` 是核心积木：`for-generate` 逐位展开一条 `Data_async → Data_meta → Data_sync(...) → Output` 的移位链，总级数 = `SYNC_DEPTH`（默认 2，范围 2..16）；`sync_Strobe`/`sync_Vector` 内部直接 `entity PoC.sync_Bits` 例化它。
- 厂商选择沿用 u3-l2 的**双层框架**：`.files`（如 `sync_Bits.files`）在编译期按 `DeviceVendor` 选 Altera/Xilinx 子实体并引入原语库，`generate` 在展开期分发到 `genGeneric`/`genAltera`/`genXilinx`。
- **多位信号绝不能逐 bit 同步**，必须靠握手（`sync_Vector`）或异步 FIFO 保证整体一致；这是 CDC 工程的第一铁律。
- 同步器靠**命名约定 + 通配约束**免受时序分析打扰：`ucf/MetaStability.ucf` 对 `*_async` 网线与 `*_meta*` FF 做 TIG；源码内还用 `ASYNC_REG`/`SHREG_EXTRACT` 指导综合器别把同步链优化掉。改名即破约束。

---

## 7. 下一步学习建议

- **向「数据流」走**：本讲的 `sync_Vector` 是低吞吐的偶发总线同步；连续高速跨域数据流应学 u3-l4 的 **`fifo_ic_got`**（跨钟 FIFO，格雷码指针 + 双 FF 同步），它正是 `sync_Bits` 思想在多 bit 上的工业化延伸。
- **向「厂商实现」走**：想看厂商专用同步器如何用底层原语实现，可读 `src/misc/sync/sync_Bits_Xilinx.vhdl`、`sync_Bits_Altera.vhdl`（需先把 `MY_DEVICE` 设成对应厂商，使其被 `.files` 选中），对比通用 `generate` 版本与原语版本的差异。
- **向「测试」走**：`tb/misc/sync/` 下有各同步器的测试台（命名遵循 `<entity>_tb.vhdl`），结合 u4-l1/u4-l2 学到的仿真辅助包与测试台骨架，可以亲手跑一次跨钟同步波形，观察 `Busy`、`Changed` 与亚稳态决断过程。
- **补齐命名空间全貌**：`src/misc/sync/` 下还有 `sync_Command.files`，提示存在 `sync_Command` 核（本讲未展开）；可作为延伸阅读，理解「命令」类跨域信号的同步策略。
