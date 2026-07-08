# 贯穿全项目的 AXI-Stream 式握手约定

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 hdl-modules 里「流式接口」的统一长相：`ready`/`valid`/`data`/`last`/`strobe` 各自的语义与组合规则。
- 解释为什么「组合逻辑路径」会威胁时序收敛，以及 `handshake_pipeline` 如何用寄存器级把它打断。
- 对照三个 generic（`full_throughput`、`pipeline_control_signals`、`pipeline_data_signals`）讲出 `handshake_pipeline` 六种模式各自的吞吐量、面积与适用场景。
- 读懂 `handshake_mux` 的「锁定到一个输入直到包结束」的仲裁状态机，并知道它的局限。
- 用现成的 VUnit 测试台与 netlist 构建数据，亲手验证不同模式的吞吐量与资源差异。

## 2. 前置知识

本讲默认你已经读过 [u1-l2 仓库布局与单个模块的目录约定](u1-l2-repo-and-module-layout.md)，知道 `src/` 是可综合源码、`test/` 是测试台、库名等于模块名。下面用通俗语言补两个概念。

**握手（handshake）。** 数字电路里，数据从 A 流向 B 时，B 不一定每个时钟周期都吃得下。于是双方约定两个控制信号：发送方拉高 `valid` 表示「我这周期有有效数据」；接收方拉高 `ready` 表示「我这周期收得下」。只有同一时钟上升沿上 `valid` 与 `ready` 同时为 1，这一拍的数据才真正完成传递（称为一次事务 / transaction / beat）。这就是 AXI 总线里 ready/valid 握手的核心，hdl-modules 把它作为整个项目流式接口的通用约定。

**组合逻辑路径与时序。** 如果 `valid`、`ready`、`data` 之间没有任何寄存器缓冲，信号会从输入端口「一路穿过组合逻辑」直接到达输出端口，这条路径叫组合路径（combinational path）。路径越长，关键路径延迟越大，电路能跑的最高时钟频率就越低。解决办法是插入寄存器级（pipeline stage），把长组合路径切断成几段短路径。`handshake_pipeline` 这个实体就是干这件事的——它本身不改变数据内容，只是「在握手通路上加缓冲寄存器，改善时序」。

> 术语速查：beat（一拍数据/一次事务）、packet（由若干 beat 组成的一个包，最后一拍用 `last` 标记）、strobe（字节有效位，类似 AXI-Stream 的 `tkeep`/`tstrb`）、关键路径（critical path）、逻辑级数（logic level，组合路径穿越的 LUT 层数）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [modules/common/src/handshake_pipeline.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd) | 握手流水线，用 3 个 generic 切换 6 种「加寄存器」模式，是项目里最常被复用的时序收敛工具。 |
| [modules/common/src/handshake_mux.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_mux.vhd) | 多路复用器，把多路 AXI-Stream 式输入汇成一路，按包粒度锁定仲裁。 |
| [modules/common/src/types_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd) | 提供 `slv_vec_t`（`std_ulogic_vector` 的数组类型），`handshake_mux` 用它表达「多路数据总线」。 |
| [modules/common/test/tb_handshake_pipeline.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/test/tb_handshake_pipeline.vhd) | `handshake_pipeline` 的 VUnit 测试台，含 `test_full_throughput` 吞吐量断言。 |
| [modules/common/module_common.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py) | 注册测试与 netlist 构建；其中 `_get_handshake_pipeline_build_projects` 列出了每种 generic 组合的真实 LUT/FF/逻辑级数。 |
| [doc/sphinx/getting_started.rst](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst) | 仓库使用说明；本讲的接口约定散见于各实体头注释而非单独章节。 |

## 4. 核心概念与源码讲解

### 4.1 AXI-Stream 式握手约定：ready / valid / data / last / strobe

#### 4.1.1 概念说明

hdl-modules 没有把握手约定写成一份独立规范文档，而是把它固化进每个流式实体的端口签名里。你只要看一眼 `handshake_pipeline` 的端口，就能记住整套约定。它解决的问题是：**让 FIFO、宽度转换、DMA、AXI-Stream 等所有模块共享同一套数据流接口，从而可以像搭积木一样串接**。

约定包含五类信号：

- `valid`（主→从）：本拍数据有效。
- `ready`（从→主）：本拍可以接收。
- `data`（主→从）：负载数据，位宽由 `data_width` 决定。
- `last`（主→从，可选）：本拍是一个包的最后一拍。
- `strobe`（主→从，可选）：字节级有效位，类似「这一拍里哪些字节是真数据」。

关键规则（继承自 AXI/AXI-Stream）：**`valid` 一旦拉高就不能因为 `ready` 没来而组合地撤下**；反过来 `ready` 允许组合地依赖 `valid`。这避免了两个模块互相等待的死锁环。

#### 4.1.2 核心流程

一次成功的事务用伪代码描述：

```
每个 clk 上升沿：
  if input_valid == 1 and input_ready == 1 then
      // 一拍数据完成传递
      data 送达下游
      如果本拍 input_last == 1，则一个 packet 结束
```

把吞吐量形式化：若接口每个时钟周期都能完成一拍，则吞吐量为 1（满吞吐）；若平均每 \(k\) 个周期才完成一拍，则吞吐量为 \(1/k\)。注意「延迟（latency）」和「吞吐（throughput）」是两件事：插入一级寄存器会加 1 拍延迟，但若仍能每周期传一拍，吞吐仍为 1。

#### 4.1.3 源码精读

约定最完整的体现是 `handshake_pipeline` 的实体声明。注意 `input_last`、`input_data`、`input_strobe` 都带有默认值（`:= (others => '-')`），所以**这些信号是可选的**——这正是「约定统一、按需接线」的体现：

- [handshake_pipeline.vhd:38-56](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L38-L56)：完整端口。`input_ready`/`input_valid` 必接，`input_last`/`input_data`/`input_strobe` 可选。
- [handshake_pipeline.vhd:33-37](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L33-L37)：`strobe_unit_width` 默认 8，即「字节 strobe」；当数据被打包（packed）时可改大。strobe 位宽 = `data_width / strobe_unit_width`。

`strobe` 的语义可以对照 AXI-Stream 的 `tkeep`：它告诉下游「这一拍的哪些字节单元是有效的」。在 [handshake_mux.vhd:45-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_mux.vhd#L45-L46) 里，strobe 位宽被写死成 `data_width / 8`（即字节粒度），说明 mux 假设的是典型字节 strobe 场景。

> 说明：上面 `:= (others => '-')` 里的 `'-'` 是 VHDL 的 don't care 值，表示「不接时这个输入被综合工具视为无关」，便于工具优化。

#### 4.1.4 代码实践

**实践目标：** 用肉眼确认约定统一。

1. 打开 `modules/fifo/src/fifo.vhd`、`modules/axi_stream/src/axi_stream_fifo.vhd` 任意一个的端口声明。
2. 找到成对出现的 `ready`/`valid`，以及可选的 `last`/`strobe`。
3. 列表对比这些端口名与 `handshake_pipeline` 是否一一对应。

**预期结果：** 你会发现 FIFO、AXI-Stream FIFO 等模块的端口命名与方向几乎完全一致，这正是「约定统一」的证据。（具体端口名因模块而异，是否完全同名「待本地验证」，但 ready/valid/data/last/strobe 这五类信号一定都在。）

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `valid` 不允许组合地依赖 `ready`，而 `ready` 却可以组合地依赖 `valid`？

> **参考答案：** 如果两侧都把自己的输出组合地建立在对方输出之上，就会形成一个组合环（A 等 B、B 等 A），可能造成振荡或死锁。AXI 规定 `valid` 由发送方独立决定，`ready` 可由接收方根据当前状态（包括 `valid`）决定，环就被打破成了单向依赖。

**练习 2：** `strobe_unit_width` 设为 16、`data_width` 设为 64 时，`strobe` 信号位宽是多少？

> **参考答案：** 位宽 = `data_width / strobe_unit_width` = 64 / 16 = 4 位。表示把 64 位数据看成 4 个 16 位单元，每个单元一个有效位。

---

### 4.2 handshake_pipeline：用寄存器级改善握手时序

#### 4.2.1 概念说明

`handshake_pipeline` 是一个「不改变数据、只在握手通路上加寄存器」的实体。它解决的核心问题是 **时序收敛**：当上游到下游的组合路径太长、跑不到目标时钟频率时，在中间插一级寄存器把路径切成两段。

它的巧妙之处在于：插寄存器不是免费的——会让 `ready` 的反向路径变长、或让吞吐量下降、或让面积变大。于是作者用三个 boolean generic 把「在哪插、插到什么程度」拆成 6 种模式，让你按需取舍。

#### 4.2.2 核心流程

三个 generic 决定模式：

- `full_throughput`：是否要求每周期都能传一拍（吞吐 = 1）。
- `pipeline_control_signals`：是否给 `valid`/`ready` 加寄存器（切断控制信号组合路径）。
- `pipeline_data_signals`：是否给 `data`/`last`/`strobe` 加寄存器（切断数据组合路径）。

6 种合法组合（其中 `full_throughput=T, pipeline_control=T, pipeline_data=F` 被代码用 `assert` 禁掉，见下方源码）：

| 模式 | full_throughput | pipeline_control | pipeline_data | 吞吐 | 典型用途 |
| --- | :-: | :-: | :-: | :-: | --- |
| ① skid buffer | T | T | T | 1 | 数据/控制都关键，要最好时序 |
| ② 数据流水（ready 组合） | T | F | T | 1 | 数据网复杂需流水，控制不关键 |
| ③ 全寄存 | F | T | T | 1/3 | 时序最紧，能接受降吞吐 |
| ④ 数据寄存（ready 组合） | F | F | T | 1/2 | 折中 |
| ⑤ 仅控制流水 | F | T | F | 1/3 | 只想切断控制路径 |
| ⑥ 直通 | * | F | F | 1 | 不加寄存器，对比基线 |

模式 ① 就是有名的 **skid buffer（打滑缓冲）**：用一个旁路寄存器在下游暂时不收时把数据「打滑」存一拍，从而既给所有输出加寄存器、又不丢吞吐。

#### 4.2.3 源码精读

模式 ① skid buffer 用一个三状态机实现满吞吐：

- [handshake_pipeline.vhd:64-81](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L64-L81)：声明 skid 旁路寄存器 `input_data_skid` 与三状态机 `wait_for_input_valid / full_handshake_throughput / wait_for_output_ready`。
- [handshake_pipeline.vhd:118-125](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L118-L125)：当输入有事务、输出却不收时，把数据存入 skid 寄存器并暂停接收（`input_ready_int <= '0'`），保证不丢数据。
- [handshake_pipeline.vhd:127-137](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L127-L137)：等下游 ready 后，把 skid 里的数据送出。

模式 ②（满吞吐但 `ready` 走组合逻辑）只有一行组合赋值，面积远小于 skid buffer：

- [handshake_pipeline.vhd:159](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L159)：`input_ready <= output_ready or not output_valid;` —— 只要输出端空着（`not output_valid`）或下游收得下，就允许上游送。这正是 4.1 里说的「ready 可组合依赖 valid/output 状态」。

模式 ③（1/3 吞吐，全寄存）用三条寄存器赋值实现，面积最小、但降吞吐：

- [handshake_pipeline.vhd:183-195](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L183-L195)：注释明确「能维持 1/3 吞吐」，因为 `valid` 和 `ready` 各有一拍寄存器延迟，事务后要 stall 两拍。

最后两种模式里，模式 ⑤ 用 `assert` 禁掉了「只流水控制却不流水数据还要满吞吐」这种自相矛盾的组合：

- [handshake_pipeline.vhd:238-240](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L238-L240)：`assert not full_throughput report "Does not support full throughput when only pipelining control signals" severity failure;`
- [handshake_pipeline.vhd:281-292](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L281-L292)：模式 ⑥ 直通，`input_ready <= output_ready`、`output_valid <= input_valid`，零面积，用来对比加寄存器前后的时序差异。

**真实资源数据（来自 netlist 构建，`data_width=32`）**，直接证明「满吞吐 vs 降吞吐」的面积代价：

| 模式 | generic 组合 (FT/PCS/PDS) | LUT | FF | 最大逻辑级数 |
| --- | :-: | :-: | :-: | :-: |
| ① skid buffer | T/T/T | 41 | 78 | 2 |
| ② 满吞吐·ready 组合 | T/F/T | 1 | 38 | 2 |
| ⑥ 直通 | T/F/F | 0 | 0 | 0 |
| ③ 全寄存(1/3) | F/T/T | 1 | 39 | 2 |
| ⑤ 仅控制流水 | F/T/F | 2 | 3 | 2 |
| ④ 1/2 吞吐 | F/F/T | 2 | 38 | 2 |
| ⑥ 直通 | F/F/F | 0 | 0 | 0 |

数据来源：[module_common.py:326-332](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L326-L332)。可以看到：同样是满吞吐，skid buffer（41 LUT/78 FF）比模式 ②（1 LUT/38 FF）贵得多——这就是「切断 ready 组合路径」的代价。

#### 4.2.4 代码实践

**实践目标：** 亲手看到「满吞吐」与「降吞吐」两种模式在吞吐量与组合逻辑路径上的差异。

操作步骤：

1. 配好工具链（见 [u1-l3](u1-l3-toolchain-and-deps.md)），在仓库根目录运行仿真入口，只跑 `tb_handshake_pipeline` 的 `test_full_throughput` 这个用例。该用例会送 1024 拍连续数据并计时。
2. 阅读 [tb_handshake_pipeline.vhd:128-136](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/test/tb_handshake_pipeline.vhd#L128-L136)：`check_relation(time_diff < (full_throughput_num_beats + 4) * clk_period)` 断言 1024 拍必须在不到 1028 个时钟周期内传完——这正是「满吞吐」的可执行定义。
3. 注意这个用例只会被 `full_throughput=True` 的配置触发（见 [module_common.py:137-140](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L137-L140) 的过滤逻辑）。
4. 再对照上面资源表里模式 ①（41 LUT）与模式 ③（1 LUT、1/3 吞吐）：用 `tools/synthesize.py` 对这两组 generic 各综合一次，比较资源报告。

**需要观察的现象：**

- 满吞吐配置下，1024 拍数据耗时 ≈ 1024 个周期（吞吐 ≈ 1）。
- 模式 ③（1/3 吞吐）下，连续数据会出现周期性 stall，平均每 3 拍才传 1 拍。
- 两种模式的「最大逻辑级数」都是 2，但 skid buffer 多花的 LUT 全用来做打滑缓冲与状态机。

**预期结果：** 仿真通过即证明满吞吐成立；资源对比证明「切断 ready 组合路径」要付出约 40 个 LUT 的代价。**具体仿真命令与日志格式「待本地验证」（取决于本地 VUnit/Vivado 安装）**，但断言逻辑是确定的。

#### 4.2.5 小练习与答案

**练习 1：** 为什么模式 ② 能用 1 个 LUT 就维持满吞吐，而模式 ① 却要 41 个 LUT？

> **参考答案：** 模式 ② 的 `input_ready` 是纯组合函数 `output_ready or not output_valid`，不需要保存任何状态，因此几乎零逻辑；但它的代价是 `input_ready` 有一条组合路径直达上游，时序上不如模式 ①。模式 ① 为了让 `input_ready` 也由寄存器驱动、彻底切断组合路径，引入了 skid 旁路寄存器和三状态机，逻辑量大增。

**练习 2：** 模式 ③ 的吞吐为什么是 1/3 而不是 1/2？

> **参考答案：** 因为 `valid` 寄存器有 1 拍延迟、`ready` 寄存器也有 1 拍延迟，源码注释（[L188-L191](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_pipeline.vhd#L188-L191)）说明事务后要 stall 两拍让上游更新数据与 valid，所以每完成 1 拍事务需要 3 个周期，吞吐 = 1/3。

**练习 3：** 如果某条数据总线的 fanout 很大、时序失败，但控制信号时序充裕，该选哪种模式？

> **参考答案：** 选模式 ②（`full_throughput=T, pipeline_control=F, pipeline_data=T`）：给数据加寄存器切断数据组合路径，同时保持满吞吐、不浪费逻辑在控制信号上。

---

### 4.3 handshake_mux：基于握手的多路复用与包级仲裁

#### 4.3.1 概念说明

`handshake_mux` 把多路（`num_inputs` 路）AXI-Stream 式输入汇成一路输出。它解决的问题是 **多个数据源共享一条下游通路**，例如多个传感器流复用同一个 DMA 写端口。

它的设计取向和 `handshake_pipeline` 一样——「尽量简单、面积优先」。源码头注释明确把它定位为「simple」实现，并公开声明了两个局限（见下方源码），这对使用者是非常关键的提醒。

#### 4.3.2 核心流程

mux 用一个两状态机做**包级锁定**仲裁：

```
状态 wait_for_valid_input：
  扫描所有输入，选一个 valid 的输入 input_select，进入 wait_for_data_packet_done
状态 wait_for_data_packet_done：
  把 input_select 这一路的数据/握手原样接到输出
  直到输出侧完成一拍且 result_last=1（一个包传完），才回到 wait_for_valid_input
```

也就是说：**mux 一旦选中某路，就会锁住它，直到这一路的整个 packet（由 `last` 标记结尾）传完**，中途不会切换到别的路。`result_id` 输出当前选中的输入索引，让下游知道数据来自哪一路。

#### 4.3.3 源码精读

头注释明确列出两条局限，使用前必须知道：

- [handshake_mux.vhd:9-24](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_mux.vhd#L9-L24)：① 若某路 packet 中间有「洞」（valid 中途撤下），mux 会白白停住，即便别的路有数据也不切换；② 仲裁是最简固定优先级，一路持续发包会饿死其他路。建议用 packet 模式的 FIFO 规整上游。

仲裁状态机：

- [handshake_mux.vhd:60-88](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_mux.vhd#L60-L88)：`wait_for_valid_input` 中的 `for` 循环遍历所有输入，遇到 valid 就把 `input_select` 赋为该索引；`wait_for_data_packet_done` 持续到 `result_ready and result_valid and result_last`。

> 关于优先级方向的一个源码细节：`for input_idx in input_valid'range loop` 按索引升序遍历，循环里对 `input_select` 多次赋值，按 VHDL 信号「最后赋值生效」的语义，最终生效的是**最高索引**的有效输入。所以严格说这是「最高索引优先」的固定优先级，不是轮询（round-robin）。无论方向如何，关键性质是「固定优先级 + 一路可持续霸占」，与头注释的警告一致。

组合数据通路：

- [handshake_mux.vhd:91-109](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_mux.vhd#L91-L109)：数据用 `input_data(input_select)` 做纯组合选择送出；`input_ready` 默认全 0，只有在锁定状态才把 `result_ready` 回传给被选中的那一路。这保证了未被选中的输入不会误以为被接收。

多路总线类型来自 `types_pkg`：

- [types_pkg.vhd:20](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L20)：`type slv_vec_t is array (integer range <>) of std_ulogic_vector;` —— 这是一个「数组里每个元素都是 `std_ulogic_vector`」的类型，`handshake_mux` 用它一次表达 `num_inputs` 路数据总线（[handshake_mux.vhd:44](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_mux.vhd#L44)）。这也是为什么该文件开头有 `use work.types_pkg.all;`。

#### 4.3.4 代码实践

**实践目标：** 体会「包级锁定」与「固定优先级」带来的现象。

1. 打开 [tb_handshake_mux.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/test/tb_handshake_mux.vhd)：它用 4 个 `axi_stream_master` BFM 随机向 4 路输入塞 packet，再用 `axi_stream_slave` BFM 校验输出。注意 [L179-L185](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/test/tb_handshake_mux.vhd#L179-L185) 的 `assign_handshake` 进程：由于仲裁顺序不确定，输出侧必须按 `result_id` 把握手信号路由到对应的参考队列。
2. 运行该测试台（默认 `num_inputs=4`），观察日志里 `result_id` 是否会在一个 packet 内保持不变、传完一个包后才可能跳变。
3. 思考实验（不用真跑）：如果让 input 0 持续发无 `last` 的数据流，依据源码预言其余 3 路是否还能被服务。

**需要观察的现象 / 预期结果：** `result_id` 在每个 packet 期间稳定；持续发送的高优先级路会霸占输出，其他路被饿死——这正是头注释警告的行为。若想避免，应在上游加 packet 模式 FIFO（本讲不展开，留到 [u4-l1 同步 FIFO](u4-l1-synchronous-fifo.md)）。运行细节「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1：** mux 在 `wait_for_data_packet_done` 状态下，未被选中的输入的 `input_ready` 是什么值？为什么？

> **参考答案：** 是 `'0'`（见 [L94](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/handshake_mux.vhd#L94) 默认赋全 0，且只有 `input_select` 那一路会被赋 `result_ready`）。这样未被选中的上游 master 看到不 ready，就不会错误地认为自己的数据已被接收而推进内部状态。

**练习 2：** 为什么测试台要为每一路输入各准备一个独立的参考队列，而不是共用一个？

> **参考答案：** 因为仲裁顺序不确定，packet 到达输出的先后与送入的先后不一定相同（见 [L176-L178](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/test/tb_handshake_mux.vhd#L176-L178) 注释）。每路一个参考队列，并由 `result_id` 选择当前该比对哪个队列，才能正确校验每包数据。

---

## 5. 综合实践

把本讲三个知识点（握手约定、流水线模式取舍、多路复用）串起来做一个「读源码 + 跑数据」的小任务：

1. **读端口**：打开 `handshake_pipeline.vhd` 与 `handshake_mux.vhd`，各列出 5 类握手信号的方向与是否可选，确认两者共用同一套约定。
2. **选模式**：假设你有一个时序失败的数据通路——数据总线 fanout 大、`ready` 路径不紧张。对照 4.2 的资源表，写出你会选择的 3 个 generic 取值，并说明理由（提示：模式 ②）。
3. **验证吞吐**：运行 `tb_handshake_pipeline` 的 `test_full_throughput`，确认满吞吐断言通过；再用 `tools/synthesize.py` 对模式 ① 与模式 ③ 各综合一次，把实际 LUT/FF 与本讲给出的表格（41 vs 1）对账。
4. **理解局限**：在 `handshake_mux.vhd` 头注释里找到两条 `.. warning::`，用一句话复述「packet 中间有洞」和「固定优先级饿死」两个风险，并各给一个规避建议。

完成后，你应当能用一句话回答：「为什么 hdl-modules 的所有流式模块都能即插即用地串在一起？」——因为它们共享同一套 ready/valid/data/last/strobe 约定，而 `handshake_pipeline` 让你在不破坏约定的前提下灵活调节时序。

## 6. 本讲小结

- hdl-modules 的流式接口统一长成 `ready`/`valid`/`data`/`last`/`strobe` 五类信号，规则继承自 AXI/AXI-Stream：`valid` 不得组合依赖 `ready`，`ready` 可组合依赖 `valid`。
- `handshake_pipeline` 用 `full_throughput`/`pipeline_control_signals`/`pipeline_data_signals` 三个 generic 切换 6 种「加寄存器」模式，是在不改变数据的前提下调节时序与面积的核心工具。
- 满吞吐的 skid buffer（模式 ①）要 41 LUT，而同为满吞吐但让 `ready` 走组合逻辑的模式 ② 只要 1 LUT——「切断 ready 组合路径」的面积代价是真实的、可量化的。
- 降吞吐模式（1/3、1/2）用更少逻辑换时序裕量，适合能容忍 stall 的场景；其中「满吞吐 + 只流水控制 + 不流水数据」被 `assert` 禁用。
- `handshake_mux` 用包级锁定的固定优先级仲裁把多路汇成一路，简单但有两个已知局限：packet 有洞会空等、强输入会饿死弱输入。
- 五类信号都通过端口默认值实现「可选」，配合 `slv_vec_t` 这类数组类型，让流式模块像积木一样可拼接。

## 7. 下一步学习建议

- 想看握手约定在「存储」上的应用，进入 [u4-l1 同步 FIFO](u4-l1-synchronous-fifo.md)：FIFO 的读写端口正是本讲的 ready/valid 接口，`last`/`strobe` 还会被 generic 进一步开关。
- 想看握手约定在「总线」上的应用，进入 [u5-l1 AXI-Stream 模块与包定义](u5-l1-axi-stream.md)：AXI-Stream 是本讲约定的标准化版本。
- 想深入 `types_pkg` 提供的更多数组类型与工具函数，进入 [u2-l2 common 基础包](u2-l2-common-packages.md)。
- 如果对「组合路径影响时序」仍觉得抽象，建议结合 [u8-l3 资源占用回归](u8-l3-resource-utilization-regression.md) 看 `MaximumLogicLevel` 这个 checker 如何把逻辑级数纳入 CI 回归。
