# 常用 RFNoC 块：Radio / DDC / DUC / FFT / Replay

## 1. 本讲目标

本讲是 RFNoC 单元（u3）的收尾。前面三讲（u3-l1 会话、u3-l2 块控制器、u3-l3 连接与 commit）建立了「块是什么、怎么造、怎么连」的框架；本讲进入「具体每一个常用块能干什么、怎么用」。

学完后你应该能够：

- 说出 Radio、DDC、DUC、FFT、Replay 五类块各自的射频/数字信号处理职责。
- 区分「终端块（终止图）」与「透传块（在图中间转发属性/动作）」的转发策略差异。
- 看懂 DDC/DUC 的抽样率变换原理（半带 + CIC）及其属性传播 resolver。
- 知道如何在 `rfnoc_graph` 里取到类型化块控制器、调用其方法并 `commit`。
- 把 `rfnoc_rx_to_file` 这个真实示例里的「取块 → 配置 → 连接 → 提交 → 流式」读通。

---

## 2. 前置知识

本讲默认你已掌握前几讲的概念，重点是：

- **块控制器与 block_id**（u3-l2）：每个块有 NoC ID（硬件标识）和 block_id（如 `0/DDC#0`，软件寻址）。
- **属性与 resolver**（u3-l2、u3-l5）：块暴露 `property_t` 属性，属性之间用 `add_property_resolver` 声明依赖与派生计算；`commit()` 触发拓扑序求解。
- **边、动作、转发策略**（u3-l3）：块之间靠边连接，靠 `action_info` 传递 `stream_cmd`、`tune_request`、`rx_event` 等动作；转发策略（forwarding policy）决定属性/动作/MTU 是否穿过该块。
- **抽样率/频率**：基带采样率（samp_rate）、本振频率（LO）、数字频偏（DDS freq）。

下面补充三个本讲会用到的数字信号处理常识，初学者可先建立直觉：

| 概念 | 直觉说明 |
|------|----------|
| **抽样率变换** | 改变数据流的样本率。降低叫「抽取（decimation, ↓D）」、升高叫「插值（interpolation, ↑I）」。 |
| **半带滤波器（HB）** | 一种高效的 2 倍抽取/插值滤波器，每级只能做 ×2 或 ÷2。 |
| **CIC 滤波器** | 无乘法的级联积分梳状滤波器，可做任意整数倍抽取/插值，但有通带滚降。 |
| **DDS / NCO** | 数控振荡器，用一个相位增量产生复正弦，实现数字频移。 |
| **FFT / iFFT** | 离散傅里叶正/反变换，时域↔频域。 |
| **循环前缀（CP）** | OFDM 符号前复制一段尾部样本，用于抵抗多径，OFDM 收发的核心操作。 |

> 提示：DDC = Digital DownConverter（数字下变频），DUC = Digital UpConverter（数字上变频），二者在发送/接收链路上对称。

---

## 3. 本讲源码地图

本讲涉及的源码文件及作用：

| 文件 | 作用 |
|------|------|
| [host/include/uhd/rfnoc/radio_control.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/radio_control.hpp) | Radio 块控制器的**抽象公共接口**：采样率、频率、增益、天线、传感器、流命令等纯虚方法。 |
| [host/lib/rfnoc/radio_control_impl.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_impl.cpp) | Radio 块的**设备无关基类实现**：构造时读 FPGA 寄存器、登记动作处理器、`issue_stream_cmd` 写命令字。各设备族（X410 等）的 radio 子类继承它。 |
| [host/include/uhd/rfnoc/ddc_block_control.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/ddc_block_control.hpp) | DDC 块公共接口：`set_output_rate`、`set_freq`、`set_input_rate`、`issue_stream_cmd`。 |
| [host/lib/rfnoc/ddc_block_control.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp) | DDC 实现：构造期读半带数/CIC 最大抽取、生成合法抽取表、注册属性 resolver、寄存器读写。 |
| [host/lib/rfnoc/duc_block_control.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp) | DUC 实现：与 DDC 对称，做插值而非抽取。 |
| [host/include/uhd/rfnoc/fft_block_control.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/fft_block_control.hpp) | FFT 块公共接口：长度、方向、幅度格式、移位、缩放、循环前缀列表、旁路模式。 |
| [host/lib/rfnoc/fft_block_control.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/fft_block_control.cpp) | FFT 实现：兼容 v1/v2 两个 NoC ID、解码能力寄存器、配置 Xilinx FFT IP。 |
| [host/include/uhd/rfnoc/replay_block_control.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/replay_block_control.hpp) | Replay 块公共接口：录制（record）、回放（play/config_play/stop）、内存与缓冲状态、异步元数据。 |
| [host/lib/rfnoc/replay_block_control.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp) | Replay 实现：把 DRAM 当环形缓冲，录制/回放由寄存器命令驱动，DROP 终端策略。 |
| [host/examples/rfnoc_rx_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp) | 综合示例：取 Radio/DDC 块 → 配置 → 连接到 rx_streamer → `commit` → 收样本写文件。 |

---

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，每块对应一类 RFNoC 块控制器。

### 4.1 radio_control：射频前端的总控

#### 4.1.1 概念说明

Radio 块是 RFNoC 图里最「重」的块：它直接控制 FPGA 内的射频前端——采样率、收发频率（经本振 LO）、增益、天线、带宽、传感器（如 `lo_locked`）、GPIO、EEPROM，以及接收流命令（`issue_stream_cmd`）的真正执行点。可以把它理解为「数字侧能碰到的、离天线最近的一层」。

`radio_control` 是一个**抽象公共基类**，它多重继承了若干能力接口：

- [host/include/uhd/rfnoc/radio_control.hpp:101-L104](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/radio_control.hpp#L101-L104)：`radio_control` 同时继承 `noc_block_base`、`rf_control::core_iface`（射频核心能力）、`power_reference_iface`（功率参考）和 `discoverable_feature_getter_iface`（可发现特性）。

也就是说，Radio 块用一个类把「块基础设施 + 射频控制 + 功率 + 可发现特性」四类能力聚合在一起。所有方法都是纯虚函数，由设备族相关的子类落地。

> 重要：Radio 是图中的**终端块**。它的属性/动作/MTU 转发策略都是 `DROP`——它不把上游属性透传到下游，因为它自己就是射频链的端点（详见 4.1.3）。

#### 4.1.2 核心流程

Radio 块的生命周期与一次接收点火的过程：

1. **构造**：读 FPGA 兼容版本与 radio 宽度寄存器，解析出 SPC（每时钟采样数）与样本位宽；设置 DROP 转发策略；登记 `ACTION_KEY_STREAM_CMD` 与 `ACTION_KEY_RX_RESTART_REQ` 动作处理器。
2. **配置**（由用户在 `commit` 后调用）：`set_rate` 设采样率、`set_rx_frequency` 设频率、`set_rx_gain` 设增益等——多数方法把值缓存到成员或转发到子板属性树。
3. **点火**：`issue_stream_cmd(stream_cmd, port)` 把流命令翻译成命令字写入 `REG_RX_CMD` 寄存器，FPGA 据此开始/停止送样本。
4. **溢出处理**：FPGA 检测到 overrun 时回送异步消息，控制器记日志 `'O'` 并向下游 post `rx_event_action_info`；连续流模式下经 `RX_RESTART_REQ` 与 rx_streamer 握手重启（见头文件 4.1.3 引用的 Overrun Handling 说明）。

#### 4.1.3 源码精读

**公共接口骨架**——`RFNOC_DECLARE_BLOCK` 声明这是可注册块，随后是分组的纯虚方法：

- [host/include/uhd/rfnoc/radio_control.hpp:111-L111](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/radio_control.hpp#L111-L111)：`RFNOC_DECLARE_BLOCK(radio_control)` 声明块身份。
- [host/include/uhd/rfnoc/radio_control.hpp:116-L119](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/radio_control.hpp#L116-L119)：`set_rate(rate)` 设采样率并回传实际值。
- [host/include/uhd/rfnoc/radio_control.hpp:346-L347](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/radio_control.hpp#L346-L347)：`issue_stream_cmd(stream_cmd, port)` 点火接收。

**头文件里那段很长的注释**值得读一遍，它讲清了 RFNoC 的溢出处理协议（图拓扑编译期未知，故用动作消息而非函数返回值传递 overrun）：

- [host/include/uhd/rfnoc/radio_control.hpp:27-L100](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/radio_control.hpp#L27-L100)：Overrun Handling 说明——radio 把 overrun 以 `rx_event_action_info` 形式 post 给下游，连续流时由 rx_streamer 与 radio 握手重启。

**实现基类构造**——设备无关部分：

- [host/lib/rfnoc/radio_control_impl.cpp:57-L67](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_impl.cpp#L57-L67)：构造函数从 `REG_RADIO_WIDTH` 寄存器解析出样本位宽 `_samp_width`（高 16 位）与 SPC `_spc`（低 16 位）。SPC>1 表示该 radio 每时钟处理多个样本（宽带场景）。

- [host/lib/rfnoc/radio_control_impl.cpp:79-L81](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_impl.cpp#L79-L81)：把属性/动作/MTU 三类转发策略都设为 `DROP`——这就是 Radio 作为终端块的本质，它不透传任何连接协商信息。

- [host/lib/rfnoc/radio_control_impl.cpp:82-L104](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_impl.cpp#L82-L104)：登记 `ACTION_KEY_STREAM_CMD` 处理器，校验动作来自输出端口后转交 `issue_stream_cmd`。

**流命令的真正执行**——这是 Radio 与 DDC/Replay 都有但实现不同的关键方法：

- [host/lib/rfnoc/radio_control_impl.cpp:1066-L1134](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_impl.cpp#L1066-L1134)：把 `stream_mode` 映射成命令字（`RX_CMD_CONTINUOUS/STOP/FINITE`）；有限采集时把 `num_samps` 折算成 48 位字数（且必须是 SPC 的整数倍，否则向上取整并告警）；定时命令时把 `time_spec` 换算成 ticks 写入时间寄存器并置 `RX_CMD_TIMED_POS` 位；最后写 `REG_RX_CMD`。

> 对比要点：**Radio 的 `issue_stream_cmd` 真正驱动 FPGA 收样本**；而下面要讲的 DDC、Replay 的 `issue_stream_cmd` 只是把命令在图里转发/调度，本身不产生样本。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，理解 Radio 块的「终端」特性与 `issue_stream_cmd` 的寄存器级实现。

**操作步骤**：

1. 打开 `host/lib/rfnoc/radio_control_impl.cpp`，找到构造函数中的三处 `forwarding_policy_t::DROP`（行 79-81）。
2. 跟着 `issue_stream_cmd`（行 1066）读一遍，注意 `stream_mode_to_cmd_word` 这个映射表如何把高层枚举翻译成 FPGA 命令字。
3. 在 `host/examples/rfnoc_rx_to_file.cpp` 中搜索 `radio_ctrl->`，观察一个真实程序如何调用 `set_rx_gain` / `set_rx_bandwidth` / `set_rx_antenna` / `get_rx_sensor`。
   - 参考 [host/examples/rfnoc_rx_to_file.cpp:501-L501](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L501-L501)。

**需要观察的现象**：示例里设增益后总是「请求值 → 再回读实际值」成对出现（`set_rx_gain` 后立即 `get_rx_gain` 打印），这印证了 u1-l6/u2-l3 的结论——硬件会矫正请求值，必须回读。

**预期结果**：能口头解释「为什么 Radio 的转发策略是 DROP、而 DDC 是 ONE_TO_ONE」。若无硬件，此为源码阅读型实践，结论「待本地验证」硬件行为。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `radio_control` 要同时继承 `rf_control::core_iface` 和 `power_reference_iface`，而不是把这些方法直接写进 `radio_control`？

**参考答案**：接口分离（ISP 原则）。`core_iface` 定义射频核心控制（频率/增益/天线），`power_reference_iface` 定义功率参考校准；这样非 radio 的块或测试桩也可以只实现其中一个接口，且便于通过 `discoverable_feature_getter_iface` 按特性查询能力。

**练习 2**：`issue_stream_cmd` 里有限采集 `num_samps` 不是 SPC 整数倍时会怎样？

**参考答案**：会把字数 `num_words` 向上取整并打印告警，实际返回 `num_words * _spc` 个样本（多于请求），见 [radio_control_impl.cpp:1089-L1097](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_impl.cpp#L1089-L1097)。

---

### 4.2 ddc_block_control：数字下变频（抽取 + 频移）

#### 4.2.1 概念说明

DDC 块是接收链上紧跟 Radio 之后的数字信号处理块。它做两件事：

1. **数字频移**：用一个 DDS（NCO）把感兴趣的窄带搬到零频。
2. **抽取（decimation）**：降低采样率，使主机只需处理真正需要的带宽。

抽取由两级级联实现：若干级**半带滤波器（HB，每级 ×2 抽取）** + 一级 **CIC 滤波器（任意整数倍抽取）**。合法抽取值集合 = \(\{2^{hb}\cdot c \mid hb \in [0, \text{num\_halfbands}],\ c \in [1, \text{cic\_max\_decim}]\}\)。

关键术语：

- **输入率（input_rate）**：从 Radio 进入 DDC 的率（= master clock rate）。
- **输出率（output_rate）**：\(\text{output\_rate} = \text{input\_rate} / \text{decim}\)。
- **freq**：DDS 频移，单位 Hz，范围 \([-\text{input\_rate}/2,\ +\text{input\_rate}/2]\)。

> DDC 的频移发生在抽取**之前**（在输入率上做），因此频率范围以输入率为界（见 4.2.3）。

#### 4.2.2 核心流程

DDC 把所有可配置项建模为**属性**，并用 resolver 维护一致性（这是 u3-l5 experts 在块层的体现）：

```
属性：samp_rate_in(边) | samp_rate_out(边) | decim(用户) | freq(用户) | scaling(边) | type(边)
```

主要 resolver 依赖（简化）：

1. **decim resolver**：用户改 decim → 矫正到合法值 → `set_decim` 写寄存器 → 由 input_rate 重算 output_rate。
2. **samp_rate_in resolver**：上游（Radio）改输入率 → 保持 output_rate 尽量不变 → 反推 decim → 同时更新 DDS 相位增量与 scaling。
3. **freq resolver**：用户改 freq → `_set_freq` 把 Hz 换算成相位增量字写入 `SR_FREQ_ADDR`。
4. **type resolver**：DDC 只处理 `sc16`，type 被强制为常量。

动作方面，DDC 还处理两类穿越它的动作：

- **stream_cmd**：有限采集的 `num_samps` 在穿过抽取时**乘以 decim**（下游要 N 个输出样本，上游需送 N×decim 个输入样本）。
- **tune_request**：把 DDC 的数字频偏纳入整体调谐计算（AUTO 策略下防止 CORDIC 旋出基带）。

#### 4.2.3 源码精读

**构造期：从 FPGA 读能力并生成合法抽取表**

- [host/lib/rfnoc/ddc_block_control.cpp:59-L61](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L59-L61)：构造时读 `RB_NUM_HB`（半带级数）与 `RB_CIC_MAX_DECIM`（CIC 最大抽取）——这正是 u3-l2 说的「块能力来自 FPGA 全局寄存器，而非 YAML」。

- [host/lib/rfnoc/ddc_block_control.cpp:82-L90](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L82-L90)：双重循环生成合法抽取值集合，存入 `_valid_decims`（一个 `meta_range_t`），后续 `coerce_decim` 用 `.clip()` 把任意请求就近夹到合法值。

**设输出率：高层 API 入口**

- [host/lib/rfnoc/ddc_block_control.cpp:169-L181](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L169-L181)：`set_output_rate(rate, chan)` 先用 `coerce_decim(input_rate / rate)` 算出最接近的抽取，再 `set_property<int>("decim", ...)` 触发 resolver，最后返回实际 output_rate。这是示例里设采样率时真正调用的方法（见 4.2.4）。

**抽取的寄存器级实现**

- [host/lib/rfnoc/ddc_block_control.cpp:573-L620](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L573-L620)：`set_decim` 把请求抽取不断除 2、最多用 `_num_halfbands` 级半带，剩余奇数部分交给 CIC；然后把 `(hb_enable << 8) | cic_decim` 写入 `SR_DECIM_ADDR`，并算 CIC 增益做幅度补偿（`update_scaling`）。

  注意 CIC 增益公式（抽取）：

  \[\text{cic\_gain} = (R\cdot M)^{N},\quad R=\text{cic\_decim},\ M=1,\ N=4\]

  并被 FPGA 的位移补偿除以 \(2^{\lceil\log_2(\text{cic\_gain})\rceil}\)，残差留给主机 `_residual_scaling` 在 scaling 属性里修正。

**频移与频率范围**

- [host/lib/rfnoc/ddc_block_control.cpp:131-L136](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L131-L136)：`get_frequency_range` 返回 \([-\text{input\_rate}/2,\ +\text{input\_rate}/2]\)，证实 DDS 在输入率上工作。

**stream_cmd 穿越抽取时的样本数换算**

- [host/lib/rfnoc/ddc_block_control.cpp:442-L448](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L442-L448)：从输出端口（下游）来时 `num_samps *= decim`，从输入端口（上游）来时 `/= decim`——保证整条链上「下游要 N 个样本」与「上游送多少」一致。

**块注册**

- [host/lib/rfnoc/ddc_block_control.cpp:698-L699](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L698-L699)：`UHD_RFNOC_BLOCK_REGISTER_DIRECT(ddc_block_control, 0xDDC00000, "DDC", ...)`——NoC ID `0xDDC00000`、块名 `"DDC"`（构成 block_id 的名字段）。

#### 4.2.4 代码实践

**实践目标**：列出 DDC 块的关键公共方法，并说明如何在流图里配置它（这正是本讲指定任务）。

**操作步骤**：

1. 打开 `host/include/uhd/rfnoc/ddc_block_control.hpp`，列出关键公共方法及其语义：

   | 方法 | 作用 |
   |------|------|
   | `set_output_rate(rate, chan)` | 设输出率（最常用），返回实际值 |
   | `get_output_rate(chan)` / `get_input_rate(chan)` | 读输出/输入率 |
   | `set_freq(freq, chan, time)` / `get_freq(chan)` | 设/读 DDS 数字频移 |
   | `get_output_rates(chan)` | 查询合法输出率范围 |
   | `issue_stream_cmd(cmd, port)` | 转发流命令 |

   - 对应声明见 [host/include/uhd/rfnoc/ddc_block_control.hpp:72-L74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/ddc_block_control.hpp#L72-L74)（`set_freq`）、[:135-L135](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/ddc_block_control.hpp#L135-L135)（`set_output_rate`）。

2. 阅读示例 [host/examples/rfnoc_rx_to_file.cpp:585-L591](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L585-L591)：当存在 DDC 块时，采样率设在 DDC（`ddc_ctrl->set_output_rate(rate, ddc_chan)`），否则设在 Radio——这就是 DDC 的典型用法。

3. **在流图中配置 DDC 的最小伪代码**（示例代码，非项目原样）：

   ```cpp
   // 1. 取 DDC 块控制器（块名 "DDC" 来自注册宏）
   auto ddc = graph->get_block<uhd::rfnoc::ddc_block_control>(
       uhd::rfnoc::block_id_t(0, "DDC", 0));
   // 2. 连接 Radio -> DDC -> rx_streamer（用 connect_through_blocks 或显式 connect）
   // 3. commit 后再设率（利用属性传播）
   graph->commit();
   double actual = ddc->set_output_rate(target_rate, 0); // chan 0
   ```

**需要观察的现象**：设 `set_output_rate` 后回读值可能不等于请求值（被 `coerce_decim` 夹到合法抽取）。

**预期结果**：能指出「DDC 必须在 `commit()` 后配置输出率，因为属性传播需要拓扑已提交」。硬件行为待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：DDC 的 `set_freq` 提供了可选的 `time` 参数，而直接 `set_property<double>("freq", ...)` 没有。为什么推荐用 `set_freq`？

**参考答案**：`set_freq` 会临时设置命令时间（`set_command_time`）再触发属性传播，从而支持定时改频；`set_property` 不处理命令时间。见 [ddc_block_control.cpp:111-L124](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L111-L124)。

**练习 2**：若请求抽取为奇数（如 3），DDC 会给出什么告警？

**参考答案**：因为奇数无法启用任何半带（`hb_enable=0`），会打印 warning 提示「passband CIC rolloff」，建议选偶数抽取以启用半带，见 [ddc_block_control.cpp:597-L607](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L597-L607)。

---

### 4.3 duc_block_control：数字上变频（插值 + 频移）

#### 4.3.1 概念说明

DUC 是 DUC/DDC 这对孪生块里发送侧的那个，结构与 DDC 几乎对称，区别在于方向：DUC 做**插值（interpolation, ↑I）**提升采样率，把主机送来的低率基带搬到 Radio 所需的高率。术语对应：input_rate（主机侧低率）→ ×interp → output_rate（Radio 侧高率）。

关键不对称点（与 DUC 对照记忆）：

| 方面 | DDC（接收） | DUC（发送） |
|------|-------------|-------------|
| 变换 | 抽取 ↓D | 插值 ↑I |
| 频移位置 | 输入侧（高率上） | 输出侧（高率上） |
| 频率范围 | \(\pm\text{input\_rate}/2\) | \(\pm\text{output\_rate}/2\) |
| CIC 增益 | \((R M)^N\) | \((R M)^{N-1}\) |
| DDS 增益 | 2.0 | 1.0 |
| DSP 频率符号 | 正常 | 取反（`TX_SIGN=-1.0`） |

#### 4.3.2 核心流程

DUC 的属性/resolver 与 DDC 镜像：

- **interp resolver**：用户改 interp → 矫正 → `set_interp` 写寄存器 → 由 output_rate 反算 input_rate。
- **samp_rate_out resolver**：下游（Radio）改输出率 → 保持 input_rate → 反推 interp。
- 高层入口是 `set_input_rate(rate, chan)`（设主机侧率），它内部算出 `coerce_interp(output_rate/rate)` 再设 interp 属性。

stream_cmd 穿越插值时样本数换算方向也与 DDC 相反：

- 从**输入端口**（上游主机侧）来 → `num_samps *= interp`；
- 从**输出端口**（下游 Radio 侧）来 → `num_samps /= interp`。

#### 4.3.3 源码精读

**设输入率：高层入口**

- [host/lib/rfnoc/duc_block_control.cpp:168-L181](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L168-L181)：`set_input_rate(rate, chan)` 用 `coerce_interp(output_rate/rate)` 设插值，返回实际 input_rate。注意此处判断的是 `_samp_rate_out` 是否有效（与 DDC 镜像）。

**插值的寄存器级实现**

- [host/lib/rfnoc/duc_block_control.cpp:569-L612](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L569-L612)：`set_interp` 把插值不断除 2 分配给半带，剩余给 CIC，写 `SR_INTERP_ADDR`。

  注意 CIC 增益公式（插值）：

  \[\text{cic\_gain} = (R\cdot M)^{N-1},\quad R=\text{cic\_interp},\ M=1,\ N=4\]

  即 \((R M)^3\)，比抽取的 \((R M)^4\) 低一阶——这是 CIC 滤波器的标准特性（抽取在梳状级前丢样本导致多一级积分增益）。

**频率范围与发射符号**

- [host/lib/rfnoc/duc_block_control.cpp:130-L135](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L130-L135)：`get_frequency_range` 返回 \([-\text{output\_rate}/2,\ +\text{output\_rate}/2]\)——DDS 在输出（高率）侧。
- [host/lib/rfnoc/duc_block_control.cpp:29-L29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L29-L29) 与 [:495-L495](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L495-L495)：发射侧 DSP 频率取反（`TX_SIGN = -1.0`），即「上旋 vs 下旋」方向相反。

**stream_cmd 样本数换算（方向相反）**

- [host/lib/rfnoc/duc_block_control.cpp:434-L438](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L434-L438)：输入边 `*= interp`、输出边 `/= interp`。

**块注册**

- [host/lib/rfnoc/duc_block_control.cpp:690-L691](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L690-L691)：NoC ID `0xD0C00000`、块名 `"DUC"`。

#### 4.3.4 代码实践

**实践目标**：通过「镜像阅读」法快速掌握 DUC，不必逐行读。

**操作步骤**：

1. 用对比表把 DDC 的 `set_output_rate` 对应到 DUC 的 `set_input_rate`，把 `decim` 对应到 `interp`。
2. 在 `duc_block_control.cpp` 里搜索 `coerce_interp`、`set_interp`、`cic_gain`，对照 4.3.3 的行号确认实现差异。
3. （源码阅读型）画一张「主机 → DUC(↑I, 频移) → Radio → 天线」的数据流图，标出 input_rate/output_rate/freq 各自在哪一端。

**预期结果**：能用一句话说清「为什么 DUC 的 CIC 增益比 DDC 少一阶」。

#### 4.3.5 小练习与答案

**练习 1**：DUC 的合法插值集合是怎么生成的？与 DDC 是否相同？

**参考答案**：相同算法——`{(1<<hb)*c | hb∈[0,num_halfbands], c∈[1,cic_max_interp]}`，只是把 `decim` 换成 `interp`，见 [duc_block_control.cpp:81-L89](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/duc_block_control.cpp#L81-L89)。

**练习 2**：为什么 DUC 的 `get_frequency_range` 以 output_rate 为界，而 DDC 以 input_rate 为界？

**参考答案**：DUC 的 DDS 是流水线最后一级（在插值之后、高率侧），DUC 头文件注释明确「the frequency shifter is the last component」；DDC 的 DDS 是第一级（抽取之前、高率侧）。两者都在高率侧，但 DUC 的「高率端」是 output、DDC 的「高率端」是 input。

---

### 4.4 fft_block_control：FFT/iFFT 与循环前缀

#### 4.4.1 概念说明

FFT 块在 FPGA 内完成离散傅里叶正/反变换，输入输出均为 `sc16`（有符号复 16 位）。它封装了 Xilinx FFT IP 核，并在此基础上叠加了若干面向 OFDM 的能力：

- **方向**：`FORWARD`（FFT）或 `REVERSE`（iFFT）。
- **长度**：\(N=2^m\)，会被矫正到「不超过请求值的最大 2 的幂」。
- **幅度格式**：`COMPLEX`（复数）/ `MAGNITUDE`（模）/ `MAGNITUDE_SQUARED`（模平方，用于功率谱）。
- **移位（shift）**：把 DC 搬到频谱中央（`NORMAL`/`REVERSE`/`NATURAL`/`BIT_REVERSE`）。
- **缩放（scaling）**：Xilinx FFT 的逐级缩放掩码，防止溢出；提供 `set_scaling_factor(factor)` 便利方法自动分配。
- **循环前缀（CP）**：插入（发送 iFFT 后加 CP）/ 移除（接收 FFT 前去 CP），用「CP 列表」按符号索引循环取值，专为 OFDM 设计。
- **旁路（bypass）**：直通不做变换。

> 重要限制：FFT 块**不支持定时命令（timed commands）**——所有运行期属性都是「尽快生效」，不能精确到某个时间点。这在头文件里明确写出（见 4.4.3）。

该块有两个 FPGA 版本：v1（老，无 CP、最大 4096）和 v2（新，带 CP、NIPC 多采样、最大可至 64k）。软件通过两个 NoC ID 同时注册。

#### 4.4.2 核心流程

1. **构造**：根据 NoC ID 判定 v1/v2，从能力寄存器 `REG_CAPABILITIES_ADDR` / `REG_CAPABILITIES2_ADDR` 解码出 max_length、max_cp_length、NIPC、是否支持 magnitude/shift/bypass 等。
2. **属性注册**：length、direction、magnitude、scaling、scaling_factor、shift、bypass_mode、cp_insertion_list、cp_removal_list，各自挂 resolver。
3. **求解**：`set_length` 触发 `_set_length`（矫正 + 写 `REG_LENGTH_LOG2_ADDR` + 回读校验）；`set_scaling_factor` 触发 `_get_default_scaling` 把缩放因子均摊到各级再写寄存器。
4. **NIPC 对齐**：FFT 可每时钟处理 NIPC 个样本（宽带），CP 长度与包长必须是 NIPC 的整数倍。

#### 4.4.3 源码精读

**枚举与属性键**

- [host/include/uhd/rfnoc/fft_block_control.hpp:14-L16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/fft_block_control.hpp#L14-L16)：`fft_shift`、`fft_direction`、`fft_magnitude` 三个枚举。

**v1/v2 判定与能力解码**

- [host/lib/rfnoc/fft_block_control.cpp:80-L99](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/fft_block_control.cpp#L80-L99)：NoC ID 为 v1 时硬编码能力（max 4096、无 CP），否则读 `REG_CAPABILITIES_ADDR` 并按位移解码出 `_max_length`、`_max_cp_length`、列表长度上限、`_nipc`、各能力位。

**设长度（矫正 + 回读校验）**

- [host/lib/rfnoc/fft_block_control.cpp:301-L340](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/fft_block_control.cpp#L301-L340)：`_set_length` 检查范围 \([ \max(8, 2\cdot\text{NIPC}),\ \text{max\_length} ]\)，取最高有效位算 `length_log2`（即矫正到最大 2 的幂），写寄存器后**回读**验证——若回读不符抛 `value_error`。这种「写后回读」是 FFT 块的稳健性设计，length/magnitude/direction/scaling 都这么做。

**缩放因子便利方法**

- [host/lib/rfnoc/fft_block_control.cpp:691-L699](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/fft_block_control.cpp#L691-L699)：`scaling_factor` resolver 把 `factor`（如 1/256）转成分母，调 `_get_default_scaling` 把缩放均摊到各级，再写 `REG_SCALING_ADDR`。

**循环前缀列表（OFDM 核心）**

- [host/include/uhd/rfnoc/fft_block_control.hpp:642-L654](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/fft_block_control.hpp#L642-L654)：`set_cp_insertion_list(cp_lengths)` 文档说明 CP 长度按 `cp_length[n mod m]` 循环——这正是 OFDM 不同符号可有不同 CP 长度的建模方式。
- [host/lib/rfnoc/fft_block_control.cpp:467-L520](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/fft_block_control.cpp#L467-L520)：`_set_cp_insertion_list` 校验长度（≤ max_cp_length、必须是 NIPC 倍数）、清 FIFO、逐个写入并回读校验占用数。

**不支持定时命令**

- [host/include/uhd/rfnoc/fft_block_control.hpp:387-L390](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/fft_block_control.hpp#L387-L390)：明确声明「The FFT block does not support timed commands」。

**多 NoC ID 注册**

- [host/lib/rfnoc/fft_block_control.cpp:868-L870](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/fft_block_control.cpp#L868-L870)：用 `std::vector<noc_id_t>{FFT_BLOCK_V1, FFT_BLOCK_V2}` 同时注册两个 NoC ID，块名 `"FFT"`——这是 u3-l2 注册表「同一控制器可对应多个 NoC ID」的实例。

#### 4.4.4 代码实践

**实践目标**：用 Python 绑定在流图里插入一个 FFT 块并配置它（UHD 提供了现成的 Python 示例）。

**操作步骤**：

1. 阅读官方示例 `host/examples/python/rfnoc_txrx_fft_block_loopback.py`（头文件注释里专门指向它，见 [fft_block_control.hpp:59-L60](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/fft_block_control.hpp#L59-L60)）。
2. 在示例里找到取 FFT 块、设长度/方向/移位的调用，对应到 C++ 接口。
3. （源码阅读型）对照 C++ 示例 [host/examples/rfnoc_rx_to_file.cpp:295-L299](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L295-L299)，注意用 `--block-id 0/FFT#0` 可把 FFT 块插在 DDC 与 RxStreamer 之间：`Radio#0==>DDC#0-->FFT#0-->RxStreamer`。

**需要观察的现象**：设 `set_length(1000)` 会被矫正为 512（最大 2 的幂）；设 `set_length` 后属性传播会联动调整 atomic_item_size（见 `_atomic_item_size_check`）。

**预期结果**：能解释「为什么 FFT 块的输入输出类型只能是 sc16」。硬件行为待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 FFT 块要支持两个 NoC ID（v1、v2）？

**参考答案**：为了在同一份主机软件里同时兼容老 FPGA 镜像（v1，无 CP、能力有限）和新镜像（v2，带 CP、NIPC、更大 FFT）。构造函数按 NoC ID 分支决定能力来源（硬编码 vs 读寄存器）。

**练习 2**：`set_scaling_factor(1.0/N)` 与手动调 `set_scaling` 有何关系？

**参考答案**：前者是后者的便利封装：它把目标分母 N 经 `_get_default_scaling` 均摊成各级缩放掩码，再写入同一个 `REG_SCALING_ADDR`，最终也更新 `_scaling` 属性。

---

### 4.5 replay_block_control：DRAM 录制与回放

#### 4.5.1 概念说明

Replay 块把设备端的 DRAM 当作一块共享内存，可以**录制（record）**到来的样本、也可以**回放（play）**内存里的样本。典型用途：

- 把一段波形预先录到 DRAM，再以精确时间反复发射（无需主机持续供数）。
- 把接收数据先存 DRAM 再慢慢读回主机（降低对主机吞吐的实时要求）。
- 作 follow 模式下的「DRAM 缓冲」，让回放跟随录制位置（流式去抖动）。

每个端口都能访问完整内存空间，用户通过 `offset` + `size` 划分各端口的录/放缓冲区，并自行避免重叠。`offset`/`size` 必须按**内存字宽（word_size）**对齐，`size` 还要按**样本大小（item_size）**对齐。

> Replay 与 Radio 一样是**终端块**：属性/动作/MTU 都用 `DROP` 策略（见 4.5.3）。它不把上游的 `type`/`samp_rate` 透传到下游，而是自己确定录/放类型。

#### 4.5.2 核心流程

**录制**：`record(offset, size, port)` 设缓冲区并重启写指针 → 上游（如 Radio）送来的样本被写入 DRAM → 缓冲区满后背压（`get_record_fullness` 查进度）→ `record_restart` 重置写指针重新录。

**回放**：两条等价路径——
- 细粒度：`config_play(offset, size, port)` 设回放区，再 `issue_stream_cmd` 启动；
- 便捷：`play(offset, size, port, time, iterations)` = `config_play` + `issue_stream_cmd`。

`issue_stream_cmd` 把 `stream_mode` 映射成 FPGA 命令字（`PLAY_CMD_FINITE/CONTINUOUS/STOP`），可选附加定时位、无 EOB 位、follow 模式位，写入 `REG_PLAY_CMD_ADDR`。

**异步元数据**：当 Replay 上游是 Radio（录制）或下游是 Radio（回放），Radio 产生的 overrun/underrun 不会经 rx/tx_streamer 返回，而是被 Replay 缓存在环形队列里，用 `get_record_async_metadata` / `get_play_async_metadata` 取出。

#### 4.5.3 源码精读

**构造：终端策略 + 内存参数**

- [host/lib/rfnoc/replay_block_control.cpp:120-L123](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L120-L123)：属性/动作/MTU 全部 `DROP`——Replay 是终端。
- [host/lib/rfnoc/replay_block_control.cpp:97-L100](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L97-L100)：从 `REG_MEM_SIZE_ADDR` 解码出 `_word_size`（高 16 位 /8）与 `_mem_size`（\(2^{\text{低16位}}\) 字节）。

**录制 API**

- [host/lib/rfnoc/replay_block_control.cpp:223-L231](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L223-L231)：`record` 设 offset/size 属性后调 `record_restart`；`record_restart` 校验缓冲区边界后写 `REG_REC_RESTART_ADDR`（任意值即触发重启）。
- [host/lib/rfnoc/replay_block_control.cpp:303-L306](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L303-L306)：`get_record_fullness` 读 `REG_REC_FULLNESS_LO_ADDR`（已录字节数）。

**回放 API（play = config_play + issue_stream_cmd）**

- [host/lib/rfnoc/replay_block_control.cpp:254-L271](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L254-L271)：`play` 先 `config_play`，再按 `iterations` 构造 stream_cmd（`PLAY_CONTINUOUS` → 连续；否则 `NUM_SAMPS_AND_DONE`，样本数 = `iterations * size / item_size`），最后 `issue_stream_cmd`。
- [host/include/uhd/rfnoc/replay_block_control.hpp:156-L156](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/replay_block_control.hpp#L156-L156)：`PLAY_CONTINUOUS = size_t::max()` 用作「无限循环」哨兵。

**回放命令字组装（寄存器级）**

- [host/lib/rfnoc/replay_block_control.cpp:436-L506](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L436-L506)：`issue_stream_cmd` 把 stream_mode 映射成命令字；有限模式算 `num_words`；按 `stream_now`/`NUM_SAMPS_AND_MORE`/follow_mode 拼接定时位、NO_EOB 位、follow 位；写 `REG_PLAY_CMD_ADDR`。这里还做了命令 FIFO 容量检查（v1.1+），满了抛 `op_failed`。

**follow 模式校验**

- [host/lib/rfnoc/replay_block_control.cpp:463-L477](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L463-L477)：follow 模式要求 record 与 play 在同端口、同 offset、且 record_size ≥ play_size，且不能与连续回放同用。

**异步元数据队列**

- [host/lib/rfnoc/replay_block_control.cpp:778-L806](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L778-L806)：RX/TX 事件动作被转成 `rx_metadata_t`/`async_metadata_t`，`push_with_pop_on_full` 压入有界队列（容量 `ASYNC_MSG_QUEUE_SIZE=128`，满了丢最旧）。

**块注册**

- [host/lib/rfnoc/replay_block_control.cpp:841-L842](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L841-L842)：NoC ID `REPLAY_BLOCK`、块名 `"Replay"`。

#### 4.5.4 代码实践

**实践目标**：理解 Replay 的「终端」身份与录/放两阶段模型。

**操作步骤**：

1. 阅读 `host/examples/rfnoc_replay_samples_from_file.cpp`（一个从文件加载样本→经 Replay 块回放→Radio 发射的完整示例）。
2. 对照本节行号，确认「record 设缓冲 → 上游供数 → get_record_fullness 等满 → config_play 设回放区 → issue_stream_cmd 启动」的序列。
3. （源码阅读型）在 `replay_block_control.cpp` 搜 `DROP`，确认它与 Radio 一样是终端，并解释「为什么 Replay 不需要像 DDC 那样转发 samp_rate」。

**需要观察的现象**：录制满后若上游继续送数会被背压，可能触发上游 Radio overrun——这正是 `get_record_async_metadata` 存在的原因。

**预期结果**：能说出 `play` 与 `config_play + issue_stream_cmd` 等价。硬件行为待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 Replay 的 `offset` 和 `size` 必须按 word_size 对齐，而 `size` 还要按 item_size 对齐？

**参考答案**：word_size 是 DRAM 物理字宽（FPGA 每次读写单位），不对齐无法寻址；item_size 是单个样本字节数，回放按样本计数，size 不是 item 整数倍会回放不完整样本。校验逻辑见 [_set_record_size](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L678-L685) 与 [_set_play_size](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L698-L709)。

**练习 2**：follow 模式有什么用？它对配置有什么硬约束？

**参考答案**：follow 模式让回放引擎受限于同端口的录制位置，把 DRAM 当 record/play 之间的去抖缓冲（适合流式跟随）。约束：record 与 play 必须同端口、同 offset、record_size ≥ play_size，且不能用于连续回放，见 [:463-L477](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/replay_block_control.cpp#L463-L477)。

---

## 5. 综合实践

**任务**：读懂 `rfnoc_rx_to_file.cpp` 这条贯穿本讲所有概念的真实链路，并用伪代码复述它的「取块 → 配置 → 连接 → 提交 → 设率 → 点火 → 收数」七步。

**关键源码定位（按执行顺序）**：

1. **取 Radio 块**：[host/examples/rfnoc_rx_to_file.cpp:427-L429](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L427-L429)——用 `block_id_t(0, "Radio", radio_id)` 构造 ID，`graph->get_block<radio_control>(...)` 取类型化控制器。
2. **沿静态边发现并连接 DDC**：[:438-L454](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L438-L454)——用 `get_block_chain` 从 Radio 出发遍历静态边，`connect_through_blocks` 自动连，若遇到 `"DDC"` 则取其控制器。
3. **配置 Radio（增益/带宽/天线）**：[:501-L520](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L501-L520)。
4. **造 rx_streamer 并连到链尾、提交**：[:539-L547](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L539-L547)——`create_rx_streamer(1, stream_args)`、`connect(last_block, port, rx_stream, 0)`、`commit()`、`enumerate_active_connections()` 打印形如 `0/Radio#0:0==>0/DDC#0:0-->RxStreamer#0:0`。
5. **提交后设频率与采样率**：[:554-L593](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L554-L593)——频率经 `tune_request_action_info` 以动作形式 post 给流器；采样率设在 DDC（`ddc_ctrl->set_output_rate`）或 Radio。
6. **点火 + 收数**：见示例 `recv` 循环（参考 [:68-L68](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L68-L68) 与 [:148-L148](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L148-L148) 的 `rx_stream->issue_stream_cmd`）。

**你的产出**：用一张图把本讲五个块摆进同一条链，例如典型接收链：

```
天线 → Radio(set_rate/set_rx_freq/issue_stream_cmd) ==> DDC(set_output_rate/set_freq)
     --> [可选 FFT(set_length/set_direction)] --> RxStreamer(recv)
```

并回答：

- 为什么 DDC 用 `==>`（静态边）连 Radio，而用 `-->` 连 RxStreamer？（提示：Static 边在 FPGA 综合期焊死，Stream 边连主机流器。）
- 若想在链里加 Replay 做「先录 DRAM 再回放」，应插在哪两端？为什么 Replay 两端都用 DROP？
- 为什么采样率必须在 `commit()` 之后再设？

> 本综合实践以源码阅读为主。若你有 RFNoC 设备（如 X410），可编译运行 `rfnoc_rx_to_file` 并加 `--block-id 0/FFT#0` 观察 FFT 插入效果；无硬件则标注「待本地验证」。

---

## 6. 本讲小结

- 五类块各司其职：**Radio**（射频前端总控，终端块）、**DDC**（接收数字下变频：抽取+频移）、**DUC**（发送数字上变频：插值+频移，与 DDC 镜像）、**FFT**（FFT/iFFT + 循环前缀，OFDM 友好，不支持定时命令）、**Replay**（DRAM 录制/回放，终端块）。
- **终端块（Radio/Replay）转发策略为 DROP**，不透传属性/动作/MTU；**透传块（DDC/DUC/FFT）用 ONE_TO_ONE**，让 samp_rate/type/MTU 等沿图协商——这是理解块行为的关键维度。
- DDC/DUC 的合法抽取/插值集合由 FPGA 寄存器读出的半带级数与 CIC 最大值决定，CIC 增益公式抽取为 \((RM)^N\)、插值为 \((RM)^{N-1}\)。
- 块的能力（端口数、max FFT、NIPC、内存大小）**来自 FPGA 寄存器读回，而非 YAML**——这是现代 RFNoC 块控制器的一致设计。
- 高层便利方法（DDC 的 `set_freq`、FFT 的 `set_scaling_factor`、Replay 的 `play`）都封装了命令时间/缩放分配/流命令等细节，应优先于直接 `set_property`。
- 标准用法学自 `rfnoc_rx_to_file`：`get_block` 取控制器 → `connect`/`commit` 建图 → **`commit` 后**配置率与频率（依赖属性传播）→ 点火收发。

---

## 7. 下一步学习建议

本讲讲完了「常用块怎么用」。后续建议：

- **u4-l1 样本格式转换 convert 子系统**：本讲多处提到 DDC/FFT 的类型固定为 `sc16`、scaling 残差留给主机修正，下一讲深入 `cpu_format ↔ otw_format` 的转换与 SSE/AVX 优化，解释这些「残差」最终如何在主机侧校正。
- **u4-l2 / u4-l3 传输层与 VRT 包**：本讲的样本最终以 CHDR/VRT 包在 Radio↔主机间搬运；下一讲讲清包格式与 zero-copy 传输。
- **u3-l5 experts 框架回顾**：本讲看到的 DDC/DDC resolver 是「块层属性传播」，可与子板层的 experts DAG 对照，加深对两套机制边界的理解。
- 想动手扩展：阅读 `rfnoc_replay_samples_from_file.cpp` 与 `rfnoc_radio_loopback.cpp` 两个示例，尝试画出各自的完整流图。
