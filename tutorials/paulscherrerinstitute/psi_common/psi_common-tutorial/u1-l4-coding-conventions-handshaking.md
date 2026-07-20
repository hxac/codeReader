# 编码规范、AXI-S 握手与 TDM 约定

## 1. 本讲目标

本讲是入门单元的最后一讲。在认识项目、看懂目录、跑通仿真之后，我们要建立「读任意一个 psi_common 组件源码时都需要用到的通用约定」。读完本讲，你应当能够：

- 说出库的命名规范（`snake_case`、`_i/_o/_io` 后缀、架构命名等），并能在源码里一眼识别端口方向。
- 说清楚 AXI4-Stream（简称 AXI-S）握手中 VLD/RDY 的规则，包括「谁必须等谁」「数据在哪一拍真正传输」。
- 理解 TDM（时分复用）数据流为什么在等速率时**不需要通道指示信号**，以及隐式通道循环 0-1-2-0-1-2-… 的含义。
- 打开 `psi_common_pl_stage.vhd` 时，能把这些规范逐一对应到真实代码上，并解释 `use_rdy_g` 如何切换两种握手实现。

本讲只读文档与一个代表组件，不展开 FIFO、CDC 等具体实现——那些是后续单元的内容。

## 2. 前置知识

本讲假设你已经读过 u1-l1、u1-l2、u1-l3，知道 psi_common 是一个可综合 VHDL 库，了解仓库目录与仿真方式。这里补充几个 VHDL 基础术语，方便完全没有 VHDL 背景的读者：

- **VHDL**：一种硬件描述语言，用来描述数字电路的结构与行为，最终会被「综合」成 FPGA/ASIC 上的真实逻辑门。
- **entity（实体）**：描述一个模块的「外壳」，即它对外暴露的输入输出端口，类似于软件里的接口/函数签名。
- **architecture（架构）**：描述 entity 内部「怎么实现」，即电路逻辑。一个 entity 可以有多种架构。
- **port（端口）**：entity 上的引脚，分 `in`（输入）、`out`（输出）、`inout`（双向）。
- **generic（类属参数）**：编译期可配置的常量，例如数据位宽、是否启用某功能。类似于软件里模板参数或宏开关。
- **std_logic / std_logic_vector**：VHDL 里最常用的信号类型，分别表示 1 比特和一组比特（向量），向量取值范围用 `downto` 表示，例如 `(7 downto 0)` 是 8 比特。
- **握手（handshaking）**：发送方和接收方用一对控制信号协商「这一拍数据是否有效、是否被接收」，避免数据丢失。
- **反压（backpressure）**：接收方来不及处理时，通过握手信号告诉发送方「先停一下」，这种机制叫反压。

有了这些概念，下面进入正文。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [doc/README.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md) | 库的总索引，开头给出了「提交代码到本库的快速语法规则」，即编码规范的权威出处。 |
| [doc/old/ch1_introduction/ch1_introduction.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md) | 详细介绍文档，其中 1.5 节讲 AXI-S 握手、1.6 节讲 TDM 约定，是这两块约定的官方说明。 |
| [hdl/psi_common_pl_stage.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd) | 一个带 AXI-S 握手的流水线级组件，是本讲用来「把规范落到代码上」的代表示例。 |
| [hdl/psi_common_par_tdm.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd) | 并行转 TDM 的组件，用来直观展示 TDM 隐式通道循环在代码中长什么样（补充阅读）。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：命名规范、AXI-S 握手语义、TDM 约定、规范在 pl_stage 中的体现。前三块讲「规则」，第四块用一个真实组件把规则全部串起来。

### 4.1 命名规范

#### 4.1.1 概念说明

psi_common 是一个由多人长期维护、组件被跨项目复用的库。为了让任何人打开任意文件都能快速读懂，库规定了一整套**命名与格式约定**。这些约定本身不改变电路功能，但它们是「库的普通话」：遵守了，代码就自带可读性；不遵守，维护者不会接受合并。

约定的权威出处是 [doc/README.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md) 开头的 **Quick syntax rules to push into the library** 一节（[doc/README.md:L8-L16](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L8-L16)）。

#### 4.1.2 核心流程

把这一节翻译成要点清单：

- **snake_case**：所有标识符用小写加下划线，例如 `width_g`、`dat_i`，不用驼峰 `widthG`。
- **去掉 Tab，改用空格**：缩进统一用空格，避免不同编辑器下 Tab 宽度不一致导致错位。
- **端口方向后缀**：输入加 `_i`、输出加 `_o`、双向加 `_io`。这样只看名字就知道信号往哪个方向流。
- **完整结尾**：`entity`/`architecture`/`package`/`procedure`/`function` 等块都要显式写 `end entity;`、`end architecture;`，而不是简写成 `end;`。
- **接口前缀**：属于同一个外部接口的信号，用一个共同前缀聚合，例如 `adc_clk_i`、`adc_data_i`、`adc_vld_i` 都属于 ADC 接口。
- **架构命名**：架构名只用三个固定词之一：`behav`（行为级）、`struc`（结构级，即例化其他组件拼装）、`rtl`（寄存器传输级，可综合的常规写法）。
- **结构级连线前缀**：在 `struc` 架构里，组件 A 连到组件 B 的内部信号建议用 `compa2compb_` 前缀，例如 `fifo2filter_*`。

记住一条主线：**名字要同时表达「方向」和「归属」**——后缀管方向，前缀管归属。

#### 4.1.3 源码精读

以 pl_stage 的 entity 声明为例，可以一眼看出规范落地：

[psi_common_pl_stage.vhd:L22-L34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L22-L34) —— entity 声明，generic 与 port 都遵循规范：

- generic 全是 snake_case，且带语义后缀：`width_g`（`_g` 表示 generic）、`use_rdy_g`、`rst_pol_g`。
- 每个 port 都带方向后缀：`clk_i`、`rst_i`、`vld_i`、`rdy_o`、`dat_i`、`vld_o`、`rdy_i`、`dat_o`，输入一律 `_i`、输出一律 `_o`。
- 行尾用 `end entity;` 显式收尾（[L34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L34)）。
- 架构名是 `rtl`（[L37](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L37)：`architecture rtl of psi_common_pl_stage is`），结尾 `end architecture;`（[L127](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L127)）。

注意一个细节：pl_stage 的握手信号本身没有加接口前缀（因为它只有一个数据流接口），但像 par_tdm 这种多信号组件会把 AXI-S 的 `last` 信号也纳入同一套命名：`last_i`、`last_o`（见 [psi_common_par_tdm.vhd:L31-L35](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L31-L35)）。等真正遇到 `adc_*`、`spi_*` 这类多接口组件时，前缀就会发挥作用。

#### 4.1.4 代码实践

> **实践目标**：用肉眼在源码里「机械地」验证命名规范，建立对后缀规则的肌肉记忆。

操作步骤：

1. 打开 [hdl/psi_common_pl_stage.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L22-L34) 的 entity 部分（L22–L34）。
2. 列出全部 8 个 port，分成两组：以 `_i` 结尾的和以 `_o` 结尾的。
3. 对照 port 声明里的 `in`/`out` 关键字，确认后缀与方向一一对应。

需要观察的现象：

- 所有 `in` 端口都以 `_i` 结尾：`clk_i`、`rst_i`、`vld_i`、`dat_i`、`rdy_i`。
- 所有 `out` 端口都以 `_o` 结尾：`rdy_o`、`vld_o`、`dat_o`。
- 没有任何端口用驼峰或无后缀命名。

预期结果：5 个 `_i`、3 个 `_o`，后缀与 `in`/`out` 完全一致。（这是纯源码阅读，无需运行即可确认。）

#### 4.1.5 小练习与答案

**练习 1**：如果某个端口是 `dat_o`，它是输入还是输出？依据是什么？

> **答案**：是输出。依据是后缀 `_o` 表示 output（输出），同时它在源码里也声明为 `out std_logic_vector`。

**练习 2**：架构名可以随便取成 `my_impl` 吗？

> **答案**：不行。库规范要求架构名只能是 `behav`、`struc`、`rtl` 三者之一。pl_stage 用的是 `rtl`，表示可综合的常规寄存器传输级实现。

---

### 4.2 AXI-S 握手语义

#### 4.2.1 概念说明

当一个组件要把数据「一拍一拍」传给下一个组件时，双方需要一套规则来确认「这一拍的数据对方到底收没收到」。psi_common 全库统一采用 **AXI4-Stream 握手协议（AXI-S）**。它只有一对核心控制信号：

- **VLD（TVALID）**：发送方（源端）拉高，表示「我这一拍的数据是有效的，请你收」。
- **RDY（TREADY）**：接收方（宿端）拉高，表示「我这一拍准备好了，可以收」。

数据本身（TDATA）通常伴随这对信号一起传输。在 psi_common 里，VLD/RDY 这套信号常常被简写为 `vld`/`rdy`（见命名模块）。文档里的权威说明是 [ch1_introduction.md 1.5 节 Handshaking Signals](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L109-L133)。

注意文档开头就声明：并非所有实体都实现 AXI-S 的全部可选特性（例如反压可以被省略），但**只要实现了，就遵循 AXI-S 标准**（[ch1_introduction.md:L110-L114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L110-L114)）。

#### 4.2.2 核心流程

AXI-S 的核心就一句话：**在某个时钟上升沿，只有 VLD 与 RDY 同时为 1，这一拍的数据才算被成功传输。** 用逻辑表达：

\[
\text{本拍发生传输} \iff (\mathrm{VLD} = 1) \land (\mathrm{RDY} = 1)
\]

围绕这句话，AXI-S 标准规定了几条约束（见 [ch1_introduction.md:L115-L121](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L115-L121)）：

1. 数据传输发生在 VLD 与 RDY（若存在）同时为高的那一拍，二者谁先拉高无所谓。
2. **源端不允许**等到 RDY 拉高之后才去拉 VLD。即源端决定「何时给数据」是自主的，不能依赖对方的 RDY。
3. 一旦 VLD 被拉高，在握手完成之前**必须保持拉高**，不能中途撤销。
4. 宿端**允许**等 VLD 拉高之后再去拉 RDY。
5. 宿端拉高 RDY 后，**允许**在 VLD 还没来之前先把 RDY 撤掉。

把这几条串起来理解：**数据传输的「时机」由 VLD 与 RDY 的「与」决定，但 VLD 是源端承诺（给了就不能撤），RDY 是宿端许可（可以灵活进出）。** 这就是为什么 RDY 常被异步转发，从而在 pl_stage 里需要专门处理（见 4.4 节）。

用伪代码描述一个源端发送一拍数据的过程：

```
# 源端（master）视角
准备好数据 dat，拉高 vld
loop:
    if rdy == 1:        # 握手成功
        可以准备下一拍
        break
    else:               # 反压：保持 vld 与 dat 不变，继续等
        保持 vld=1, dat 不变
```

#### 4.2.3 源码精读

在 pl_stage 里，AXI-S 三件套体现为一组端口：

[psi_common_pl_stage.vhd:L26-L33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L26-L34) —— 输入侧握手 `vld_i`/`rdy_o`/`dat_i`，输出侧握手 `vld_o`/`rdy_i`/`dat_o`。注意输入侧的 RDY 是 `rdy_o`（向**上游**输出「我准备好了」），输出侧的 RDY 是 `rdy_i`（接收**下游**反馈的「它准备好了」），这正是 AXI-S 双向握手的典型接法。

文档里的命名同义词表（[ch1_introduction.md:L127-L133](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L127-L133)）也说明：psi_common **不严格遵循** AXI-S 标准命名，常用同义词是：

| AXI-S 标准 | psi_common 常见写法 |
|-----------|-------------------|
| TDATA | `Data`、`InData`、`OutData`、`Sig`，或业务相关名字 |
| TVALID | `Vld`、`InVld`、`OutVld`、`Valid`、`str` |
| TREADY | `Rdy`、`InRdy`、`OutRdy` |

pl_stage 用的就是 `dat`/`vld`/`rdy` 这套简写。文档还特别指出：psi_common 有时用一个 TDATA 拆成的多个数据信号共享同一对握手信号（而不是拼成一个大向量），这是为了可读性（[ch1_introduction.md:L133](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L127-L133)）。

一个常被忽略的 AXI-S 细节：pl_stage 的输入侧 `rdy_o` 是**寄存器输出**（见 4.4 节），而 `rdy_i`（下游反馈）默认值为 `'1'`（[L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L32)），表示「如果不接反压，就当下游永远准备就绪」。

#### 4.2.4 代码实践

> **实践目标**：通过阅读测试平台的断言，反推 AXI-S 握手在反压时的预期波形。

操作步骤：

1. 打开测试平台 [testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd)。
2. 找到 L134 附近的断言：`assert rdy_o = '1' or not handle_rdy_g report "###ERROR###: rdy_o went low unexpectedly" ...`。
3. 结合 L70 的例化 `use_rdy_g => handle_rdy_g`，理解 TB 用的 generic `handle_rdy_g` 直接映射到组件的 `use_rdy_g`（[TB:L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L29) 与 [TB:L70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L70)）。

需要观察的现象：

- 当 `handle_rdy_g = false`（即组件 `use_rdy_g = false`）时，断言条件 `not handle_rdy_g` 恒真，所以「`rdy_o` 被拉低」永远不会触发报错——因为这种模式下根本没有反压。
- 当 `handle_rdy_g = true` 时，TB 在某些场景里**期望** `rdy_o` 保持为 1，一旦被意外拉低就报 `###ERROR###`。

预期结果：你能用一句话解释「为什么 TB 要用 `handle_rdy_g` 真假各跑一遍」——因为它要分别覆盖「启用反压」与「不启用反压」两条 AXI-S 代码分支。若想真正跑一遍，可按 u1-l3 的方式 `source ./run.tcl`（**待本地验证**：取决于你的 PsiSim/psi_tb 环境是否就绪）。

#### 4.2.5 小练习与答案

**练习 1**：某拍 `vld=1` 但 `rdy=0`，这一拍的数据算传输成功了吗？源端下一步该怎么做？

> **答案**：不算。传输成功要求 VLD 与 RDY 同时为 1。此时是反压，源端必须**保持 `vld=1` 且数据不变**，继续等待，直到某一拍 `rdy` 也变 1 才算完成。

**练习 2**：为什么文档说「源端不允许等 RDY 拉高后才拉 VLD」？

> **答案**：如果源端等 RDY、宿端又等 VLD（标准允许宿端这么等），双方就会死锁——谁也不肯先动。所以规则强制源端自主拉 VLD，把「启动握手」的主动权固定给源端。

---

### 4.3 TDM 约定

#### 4.3.1 概念说明

**TDM（Time-Division Multiplexing，时分复用）** 是指：多路信号共用同一条数据线，按时间轮流传输——第 0 拍传通道 0、第 1 拍传通道 1、…… 周而复始。在 FPGA 里，TDM 是用一条高速数据流「假装」多条慢速通道的常用手段（后续 u8 单元会专门讲 TDM 转换组件）。

psi_common 对 TDM 定了一条**很关键的设计约定**（见 [ch1_introduction.md 1.6 节](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L135-L145)）：

> 当多路信号以**相同采样率** TDM 传输时，**不实现额外的通道指示信号**，通道按固定顺序隐式循环（例如 3 通道：0-1-2-0-1-2-…）。

只有当各通道采样率**不同**时，才额外加一个「通道编号」指示信号。

#### 4.3.2 核心流程

约定的推理（文档 [L143-L145](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L143-L145) 给出了理由）是：

- 等速率 TDM 是最常见用法。如果不加通道指示，那么**所有组合逻辑组件**（加法器、函数逼近、二进制除法等）都不需要「知道自己在处理 TDM」，也不必维护通道计数器。
- 这意味着：同一个组合逻辑组件可以**原封不动**地用在 TDM 流上，因为它处理的是「逐拍到来的数据」，至于这一拍属于哪个通道，由系统上下文隐式约定，组件本身不关心。

换句话说，约定是为了**让 TDM 对组合逻辑透明**，最大化组件复用性。

隐式循环的时序示意（3 通道、等速率）：

```
拍号:    0    1    2    3    4    5    6  ...
通道:    ch0  ch1  ch2  ch0  ch1  ch2  ch0 ...
数据:    a0   b0   c0   a1   b1   c1   a2  ...
```

接收方只要按固定节拍对齐，就知道第 0/3/6… 拍是 ch0，无需额外信号告知。

#### 4.3.3 源码精读

TDM 约定的代码体现，可以看并行转 TDM 的组件 par_tdm：它把 `ch_nb_g` 路并行数据轮流送上单条输出线，**完全靠移位顺序**决定通道，没有任何通道编号端口。

[psi_common_par_tdm.vhd:L22-L36](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L22-L36) —— entity 端口：输入 `dat_i` 是把所有通道拼成的大向量（`ch_nb_g * ch_width_g` 位），输出 `dat_o` 只有一路（`ch_width_g` 位）。注意端口里**没有**任何 `channel_o` 之类的通道指示信号，印证了「等速率 TDM 不加通道指示」。

实现层面，它用一个移位寄存器 `ShiftReg`，每拍把数据整体右移一个通道宽度，于是低位窗口依次露出通道 0、1、2…：

[psi_common_par_tdm.vhd:L72-L76](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L72-L76) —— `shift_right(r.ShiftReg, ch_width_g)` 每拍右移一个通道宽度，输出 `dat_o <= r.ShiftReg(ch_width_g - 1 downto 0)`（[L79](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L79)）始终取最低窗口。通道顺序完全由「先放进去谁、移位方向如何」隐式决定，外部无需也无法从某个端口读出「当前是第几通道」。

#### 4.3.4 代码实践

> **实践目标**：通过阅读 par_tdm 的 generic 与端口，确认「等速率 TDM 不带通道指示」这条约定在代码里成立。

操作步骤：

1. 打开 [hdl/psi_common_par_tdm.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L22-L36) 的 entity（L22–L36）。
2. 在端口列表里找：有没有任何一个输出信号是用来表示「当前输出的是第几号通道」的？
3. 对照 `ch_nb_g`（通道数）与 `ch_width_g`（每通道位宽），计算 `dat_i` 的总位宽。

需要观察的现象：

- 端口里只有 `dat_i`/`vld_i`/`rdy_o`/`last_i` 输入和 `dat_o`/`vld_o`/`rdy_i`/`last_o` 输出，**没有通道编号输出**。
- `last_i`/`last_o` 是 AXI-S 的 TLAST（标记一包数据的最后一拍），不是通道编号。

预期结果：`dat_i` 总位宽 = `ch_nb_g * ch_width_g`（默认 8×16=128 位），`dat_o` = `ch_width_g`（默认 16 位），通道顺序由移位隐式循环，无通道指示信号——这正是 TDM 约定的直接体现。

#### 4.3.5 小练习与答案

**练习 1**：假设 4 通道等速率 TDM，写出前 8 拍的通道顺序。

> **答案**：ch0、ch1、ch2、ch3、ch0、ch1、ch2、ch3（即 0-1-2-3-0-1-2-3，周期为 4）。

**练习 2**：什么情况下 psi_common 才会为 TDM 流额外加一个「通道编号」信号？

> **答案**：当各通道采样率**不相同**时。因为此时固定循环顺序无法表达「某些通道出现得更频繁」，必须显式带上当前通道编号，接收方才能正确解复用。

---

### 4.4 规范在 pl_stage 中的体现（含 `use_rdy_g`）

#### 4.4.1 概念说明

前面三块讲了规则，这一块把规则全部落到一个组件上。`psi_common_pl_stage` 是一个**带 AXI-S 握手的流水线级**：它把输入数据寄存一拍后再输出，目的是「打断长组合逻辑路径」——尤其是因为 RDY 经常被异步转发，容易形成跨多级的长连线，必须在中间插寄存器切断（见文件头注释 [psi_common_pl_stage.vhd:L11-L14](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L11-L14)）。

它的关键 generic 是 `use_rdy_g`：

- `use_rdy_g = true`：完整实现 AXI-S 双向握手（处理下游反压）。
- `use_rdy_g = false`：**省略反压**——不处理 RDY，假设下游永远准备就绪。这正是文档说的「反压可被省略」的情形。

这个 generic 直接对应 AXI-S 是「可选特性」的体现。

#### 4.4.2 核心流程

pl_stage 采用 PSI 库贯穿全库的**二进程 record 设计法（two-process method）**，理解它对后续读 FIFO、CDC 等组件至关重要：

1. 用一个 **record 类型**把所有内部寄存器打包成一个对象（pl_stage 里叫 `tp_r`，包含主寄存器 `DataMain`/`DataMainVld`、影子寄存器 `DataShad`/`DataShadVld`、输出 `rdy_o`）。
2. 声明两个信号：当前值 `r` 和下一拍值 `r_next`（都是该 record 类型）。
3. **组合进程** `p_comb`：读入 `r` 与外部输入，计算出 `r_next`（纯组合逻辑，描述「下一拍应该变成什么」）。
4. **时序进程** `p_seq`：在时钟上升沿把 `r_next` 打入 `r`（纯寄存器，只负责存储与复位）。

这种写法的好处是：时序进程极简（只有 `r <= r_next` 和复位），所有「业务逻辑」集中在组合进程里，可读性好、易维护。pl_stage 在 [L39-L46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L39-L46) 定义 record，`p_comb` 在 [L53-L92](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L53-L92)，`p_seq` 在 [L98-L108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L98-L108)。

`use_rdy_g` 用 VHDL 的 `generate` 语句切出两套实现（**同一份文件里两个互斥分支**）：

```
g_rdy  : if  use_rdy_g generate   -- 完整 AXI-S（带反压）
            ... p_comb + p_seq（主/影寄存器 + rdy_o 逻辑）
         end generate;

g_nrdy : if not use_rdy_g generate -- 简化版（无反压）
            rdy_o <= '0';          -- 永不反压
            ... 单进程寄存 dat/vld
         end generate;
```

#### 4.4.3 源码精读

**分支一：启用反压（`g_rdy`，[L51-L109](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L51-L109)）**

这是完整 AXI-S 实现。核心难点是：`rdy_o` 是**寄存器输出**（为了切断组合路径），所以当下游某一拍突然拉低 `rdy_i` 时，本组件的 `rdy_o` 要到**下一拍**才能反映出来。在这个一拍窗口里，上游可能还会送来一个有效数据——这个「多出来」的数据无处可去，就由**影子寄存器 `DataShad`** 暂存。

判断逻辑 `IsStuck_v`（[L61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L61)）：

\[
\text{IsStuck} = (\text{DataMainVld}=1) \land (\mathrm{rdy\_i}=0) \land (\mathrm{vld\_i}=1 \lor \text{DataShadVld}=1)
\]

即「主寄存器有有效数据 且 下游不准备 且（有新数据到来 或 影子寄存器已占用）」。一旦卡住（stuck）：

- 新到来的数据写入**影子寄存器** `DataShad`（[L73-L75](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L73-L75)），而不是主寄存器。
- `rdy_o` 被拉低（[L84-L85](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L84-L85)），告诉上游停止再送。
- 等下游恢复 `rdy_i=1`，主寄存器数据被取走后（[L64-L68](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L64-L68)），影子寄存器的内容搬进主寄存器（`v.DataMain := r.DataShad`），腾出影子空间。

输出赋值在 [L94-L96](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L94-L96)：`rdy_o`/`vld_o`/`dat_o` 全部来自寄存器 `r`，因此都是「已寄存」的，这就是注释里说的「all signals are registered in both directions (including RDY)」。

**分支二：不启用反压（`g_nrdy`，[L113-L125](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L113-L125)）**

极简：`rdy_o` 直接接 `'0'`（[L114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L114)），表示「我从不向上游反压」；同时输入侧的 `rdy_i` 用默认值 `'1'`（[L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L32)），即假定下游永远准备就绪。于是只需一个进程把 `dat_i`/`vld_i` 寄存一拍（[L115-L124](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L115-L124)），没有主/影寄存器那一套复杂逻辑。这正是 AXI-S「反压可选」的直接体现：能用简单方式就不用复杂方式。

#### 4.4.4 代码实践

> **实践目标**：跟踪一次「下游反压」时数据在主/影寄存器间的流动，亲手把 4.2–4.4 三块知识串起来。这是本讲的核心实践。

操作步骤：

1. 打开 [hdl/psi_common_pl_stage.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd)，定位到 `g_rdy` 分支（L51–L109）。
2. 假设 `use_rdy_g = true`，按下表逐拍手工推演（初始 `DataMainVld=0, DataShadVld=0, rdy_o=1`，下游 `rdy_i` 第 2 拍起拉低）：

   | 拍 | vld_i | dat_i | rdy_i（下游） | IsStuck？ | DataMain / Vld | DataShad / Vld | rdy_o |
   |----|-------|-------|--------------|----------|----------------|----------------|-------|
   | 0  | 1     | A     | 1            | 否       | ← A / 1         | 空 / 0         | 1     |
   | 1  | 1     | B     | 1            | 否       | ← B / 1         | 空 / 0         | 1     |
   | 2  | 1     | C     | **0**        | 是       | B / 1           | ← C / 1        | **0** |
   | 3  | 1     | D     | 0            | 是       | B / 1           | D / 1（注）    | 0     |

   注：第 3 拍因 `rdy_o=0`，按 AXI-S 规则上游不应再送有效数据；这里仅为说明影子寄存器已满时的边界。

3. 对照 [L60-L88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L60-L88)，逐条确认你的推演与代码一致。

需要观察的现象：

- 第 2 拍下游拉低 `rdy_i`，但 `rdy_o` 直到**第 2 拍的组合结果**才在**第 3 拍**的 `rdy_o` 上体现——这就是「`rdy_o` 是寄存器、反压延迟一拍」的关键现象。
- 第 2 拍到来的 C 无法进入主寄存器（B 还卡在里面），于是被影子寄存器 `DataShad` 接住，**数据没有丢失**。
- 影子寄存器的存在，正是为了弥补「`rdy_o` 晚一拍」这一 AXI-S 合规性漏洞。

预期结果：你能画出主/影寄存器在反压前后的状态转移，并解释「为什么 `rdy_o` 必须寄存，以及寄存后为什么需要影子寄存器兜底」。若环境就绪，可用 `run.tcl` 跑 `handle_rdy_g=true` 的 TB 对照波形（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：把 `use_rdy_g` 设为 `false` 后，组件还会处理下游的 `rdy_i` 吗？为什么？

> **答案**：不会。`g_nrdy` 分支里 `rdy_i` 取默认值 `'1'`，相当于假定下游永远准备就绪；组件只是把 `dat_i`/`vld_i` 寄存一拍输出，`rdy_o` 恒为 `'0'`。这是 AXI-S「反压可选」的简化用法，适用于已知下游不会反压的场景。

**练习 2**：`DataShad`（影子寄存器）解决了什么问题？如果没有它会怎样？

> **答案**：它解决了「`rdy_o` 寄存导致反压延迟一拍」期间上游多送来的那一拍数据的暂存问题。如果没有它，这一拍数据会丢失，从而违反 AXI-S「握手完成前数据必须被正确接收」的语义。

**练习 3**：pl_stage 的 `p_seq` 进程里，复位分支把哪几个寄存器清零、把 `rdy_o` 复位成什么值？（提示：看 [L98-L108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L98-L108)）

> **答案**：把 `DataMainVld` 和 `DataShadVld` 清成 `'0'`（无有效数据），并把 `rdy_o` 复位成 `'1'`（复位后立即准备好接收上游数据）。

---

## 5. 综合实践

把本讲三块知识（命名规范、AXI-S 握手、TDM 约定）和二进程设计法串成一个综合任务：

**任务：给 `psi_common_pl_stage` 写一份「规范体检报告」。**

1. **命名体检**：列出它的全部 generic 与 port，逐个标注「方向后缀是否正确」「是否 snake_case」「是否带 `_g`/`_i`/`_o` 等语义后缀」，并指出架构名属于 `behav`/`struc`/`rtl` 中的哪一种。引用 [pl_stage entity L22-L34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L22-L34) 与 [architecture L37/L127](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L37)。
2. **握手体检**：在端口里圈出输入侧与输出侧各一组 AXI-S 三件套（`vld`/`rdy`/`dat`），指出哪一对是「向上游」、哪一对是「向下游」。再用一句话说明 `rdy_o` 为何是寄存器输出。
3. **`use_rdy_g` 行为预测**：分别预测 `use_rdy_g=true` 和 `false` 时，组件在「下游持续拉低 `rdy_i`」下的行为差异，并指出哪种模式下数据可能进入影子寄存器。
4. **TDM 可达性判断**：判断 pl_stage 能否被直接用在一条等速率 TDM 流上（提示：结合 4.3 节「TDM 对组合逻辑透明」的结论，以及 pl_stage 是否引入通道指示信号）。

> 参考结论要点：命名全部合规（snake_case + 方向后缀，架构为 `rtl`）；输入侧 `vld_i`/`rdy_o`/`dat_i` 朝上游，输出侧 `vld_o`/`rdy_i`/`dat_o` 朝下游；`rdy_o` 寄存是为了切断长组合路径，影子寄存器兜底反压延迟；`use_rdy_g=false` 时不处理反压、无影子寄存器；pl_stage 不带任何通道指示，按 TDM 约定它**可以**直接用于等速率 TDM 流。

## 6. 本讲小结

- psi_common 用一套**命名规范**统一全库：`snake_case`、端口方向后缀 `_i/_o/_io`、接口前缀、架构名限定 `behav/struc/rtl`、显式 `end entity;` 等，权威出处是 [doc/README.md:L8-L16](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L8-L16)。
- 全库采用 **AXI4-Stream 握手**：传输发生在 VLD 与 RDY 同时为高的那一拍 \(\((\mathrm{VLD}=1)\land(\mathrm{RDY}=1)\)\)；源端必须自主拉 VLD 且握手前不可撤，宿端可灵活进出 RDY。psi_common 用 `vld`/`rdy`/`dat` 简写，但不严格遵循 AXI-S 标准命名（[ch1 §1.5](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L109-L133)）。
- **TDM 约定**：等速率多路信号 TDM 传输时**不加通道指示信号**，通道按 0-1-2-… 隐式循环；只有速率不同时才加通道编号。这让 TDM 对所有组合逻辑组件透明，最大化复用（[ch1 §1.6](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L135-L145)）。
- `psi_common_pl_stage` 把上述规范全部落地，并展示了库的**二进程 record 设计法**（`r`/`r_next` + `p_comb`/`p_seq`）。
- `use_rdy_g` 是 AXI-S「反压可选」的直接开关：`true` 走带影子寄存器的完整握手实现，`false` 走无反压的简化实现。
- 测试平台用 generic `handle_rdy_g` 映射到 `use_rdy_g`，真假各跑一遍以覆盖两条分支（[TB:L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L29), [TB:L70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L70)）。

## 7. 下一步学习建议

本讲结束后，你已经掌握了读任意 psi_common 组件所需的「通用语言」。建议：

- **进入 U2 基础包**：先读 [psi_common_math_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd)，理解 `log2`/`log2ceil` 等编译期函数如何推导位宽——这是后续 RAM/FIFO 章节反复用到的工具。
- **提前观察二进程法**：在进入 U3（存储）之前，可以再翻一遍 pl_stage 的 `p_comb`/`p_seq`，因为 `sync_fifo`、`async_fifo` 等都会用同样的 record 设计法。
- **验证 TDM 直觉**：等学到 U8 的 `par_tdm`/`tdm_par` 时，回过头对照本讲 4.3 节，确认「隐式通道循环」在转换组件里的具体实现。
- **如果想立刻动手**：按 u1-l3 的方法 `source ./run.tcl` 跑一遍 pl_stage 的回归测试，观察 `handle_rdy_g` 真假两版的报告差异（需要 PsiSim/psi_tb 环境就绪）。
