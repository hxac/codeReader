# u13-l1 cocotb 仿真平台架构

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 verilog-ethernet 的仿真栈由哪几层工具拼成（cocotb + cocotbext-eth + cocotbext-axi + scapy + Icarus Verilog），以及它们各自承担什么职责。
- 读懂任意一个 `tb/<模块>/test_<模块>.py`：知道它如何用 `cocotbext.eth` 的 `GmiiSource`/`XgmiiSource` 驱动物理层、用 `cocotbext.axi` 的 `AxiStreamSource`/`AxiStreamSink` 驱动 AXI-Stream，并用 `define_stream` 为 PTP 时间戳等侧带信号生成专属端点。
- 理解 `TestFactory` 如何把一个 `run_test_*` 函数「组合爆炸」成覆盖变长帧、时钟使能反压、MII/GMII 双模的成百上千个用例，以及欠载（underrun）、坏帧注入、PAUSE/PFC 流控等专项场景是怎么构造的。
- 辨析仓库里「两代端点」：`tb/` 顶层的 `*_ep.py`（myhdl 时代遗留）与子目录里基于 `cocotbext` 的现代端点，避免打开错文件。

本讲是测试方法学单元（u13）的第一篇，承接 u1-l4 已建立的「cocotb + cocotbext-eth + iverilog」认知，往下深入到端点驱动的写法与覆盖率套路；下一篇 u13-l2 将教你从零为新模块写一份 testbench。

## 2. 前置知识

本讲假设你已经掌握（u1-l1 ~ u1-l4 已建立）：

- **DUT 与 testbench 的关系**：被测 RTL 模块叫 DUT（Design Under Test），testbench 负责给它喂激励、采集输出、做断言。
- **AXI-Stream 信号语义**：`tdata`/`tvalid`/`tready`/`tlast`/`tuser`（坏帧位在 bit0）/`tkeep`，以及「`tvalid & tready` 同拍为 1 才是一次传输（transfer）」的握手规则。
- **GMII/XGMII 物理接口**：GMII 是 8 位 + `tx_en`/`rx_dv`/`rx_er`；XGMII 是 64 位数据 `rxd` + 8 位控制 `rxc`，用控制字符 `START`/`TERM` 在一拍内定界帧。
- **cocotb 的协程模型**：`await RisingEdge(clk)` 等待的是仿真时间而非墙钟时间，Python 协程与仿真器经 VPI 绑定。

下面几个术语本讲会反复用到，先建立直觉：

- **端点（endpoint / ep）**：在 Python 侧把一组 RTL 信号「包装」成一个可 `send`/`recv` 的对象。例如 `GmiiSource` 包住 `gmii_rxd/rx_dv/rx_er`，你只需 `await source.send(frame)`，它内部就按 GMII 时序逐拍驱动信号——你不必手写每个时钟沿的赋值。
- **帧对象（Frame）**：端点之间传递的单位。`GmiiFrame`、`XgmiiFrame`、`AxiStreamFrame` 都是带元数据的字节容器，可由原始 payload 一行构造（`GmiiFrame.from_payload(data)`），自动补前导码/SFD/FCS。
- **侧带总线（side-band bus）**：除了主数据流，模块还有「并行头」或「时间戳回送」等独立小总线（如 `tx_axis_ptp_ts`/`tx_axis_ptp_ts_valid`）。cocotbext 用 `define_stream` 给它们现场造一对专属 Source/Sink。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tb/eth_mac_1g/test_eth_mac_1g.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py) | **本讲主角**：千兆 MAC 的现代 cocotb testbench，集中展示 cocotbext-eth/axi 端点、`define_stream`、`TestFactory` 与各类测试场景。 |
| [tb/eth_mac_1g/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile) | cocotb 「三件套」里的 Makefile：声明 RTL 源、用 `PARAM_` 注入参数、include cocotb 的 `Makefile.sim`。 |
| [tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py) | 10G 侧示例：`XgmiiSource`（数据+控制双总线）与 156.25 MHz（6.4 ns）时钟。 |
| [tb/axis_ep.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_ep.py) | **遗留**：myhdl 时代的 `AXIStreamFrame`/`AXIStreamSource`/`AXIStreamSink`，是 cocotbext-axi 的「前身」。 |
| [tb/eth_ep.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_ep.py) | **遗留**：myhdl 时代的 `EthFrame`，手写 `build_axis`/`parse_axis`/`calc_fcs`，现已被 scapy `Ether` + `GmiiFrame` 取代。 |
| [tb/test_eth_mac_1g.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.py) | **遗留**：顶层的 myhdl 版 testbench（`vvp -m myhdl`），与子目录同名但已是历史文件，不进回归。 |
| [tox.ini](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tox.ini) | 回归入口：锁定全部依赖版本、用 pytest 并行跑 `tb` 与 `example`、用 `--ignore-glob` 排除遗留顶层测试。 |

> 提示：仓库里每个被测模块在 `tb/<模块名>/` 下都有一对 `Makefile` + `test_<模块>.py`，结构完全一致。本讲以 `eth_mac_1g` 为样本，其余可类推。

## 4. 核心概念与源码讲解

### 4.1 仿真栈全景与 testbench「三件套」

#### 4.1.1 概念说明

verilog-ethernet 的 RTL 验证不靠手写 Verilog testbench，而靠一条 **Python 驱动 RTL** 的工具链：

- **cocotb**：核心框架。把 Python 协程注册成仿真器的回调，每个时钟沿唤醒一次协程，协程用 `await` 表达「等下一个上升沿」。
- **cocotbext-eth**：以太网专用扩展，提供 `GmiiSource/Sink`、`XgmiiSource/Sink`、`MiiPhy`/`GmiiPhy`/`RgmiiPhy`（把整块 PHY IO 封装成一个对象）以及 `PtpClockSimTime`（用仿真墙钟生成 PTP 时间戳）。
- **cocotbext-axi**：通用 AXI 扩展，提供 `AxiStreamSource/Sink` 和一个强大的 `define_stream`——给任意一组信号现场生成一对端点。
- **scapy**：用 `Ether(...) / payload` 一行构造真实格式的以太网报文，省去手拼字节。
- **Icarus Verilog（iverilog）**：实际跑 RTL 的仿真器；cocotb 经 VPI 与它通信。

`tox.ini` 把这些依赖的版本**钉死**，保证任何人、任何机器回归结果可复现。

#### 4.1.2 核心流程

一个 cocotb 用例的运行链路：

1. **编译期**：`Makefile` 用 `VERILOG_SOURCES` 显式列出全部待编译 RTL（iverilog 不会跨目录自动找子模块），用 `PARAM_` 前缀经 `-P` 注入模块参数，末尾 `include` cocotb 的 `Makefile.sim` 启动仿真。
2. **装载期**：cocotb 按 `MODULE = test_<dut>` 加载 Python 文件，例化 `TOPLEVEL = <dut>` 指定的 RTL 顶层。
3. **运行期**：Python 端的 `TB` 类把 DUT 的信号包成各类端点；`run_test_*` 协程 `send` 激励、`await ... recv()` 采集、`assert` 断言。
4. **回归期**：`tox` → `pytest -n auto` → 发现每个 `test_<module>.py` 里以 `test_` 开头的函数 → 调 `cocotb_test.simulator.run(...)` 起一次仿真。

注意 u1-l4 提到的「双用途文件」在现代 testbench 里依然成立：`tb/eth_mac_1g/test_eth_mac_1g.py` 既能被 cocotb 的 `make` 直接加载（文件顶部那段 `if cocotb.SIM_NAME:` 注册用例），又在文件底部定义了 `def test_eth_mac_1g(...)` 供 pytest 调用——同一份代码、两个入口。

#### 4.1.3 源码精读

**回归入口 `tox.ini`**——钉死版本、排除遗留文件：

[tox.ini:13-23](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tox.ini#L13-L23) 锁定 cocotb 1.7.2、cocotbext-axi 0.1.20、cocotbext-eth 0.1.22、scapy 2.5.0 等精确版本；[tox.ini:30-37](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tox.ini#L30-L37) 把测试根设为 `tb` 与 `example`，并用 `--ignore-glob=tb/test_*.py` 把顶层那些 myhdl 遗留文件排除在回归之外——这正是为什么顶层 `tb/test_eth_mac_1g.py` 不会被执行。

**`Makefile` 三件套之编译声明**——以 `tb/eth_mac_1g/Makefile` 为例：

[tb/eth_mac_1g/Makefile:23](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L23) 默认仿真器是 icarus；[tb/eth_mac_1g/Makefile:32-39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L32-L39) 逐行列出 MAC 及其全部子模块的 RTL（漏一个 iverilog 就报 missing instance）；[tb/eth_mac_1g/Makefile:42-54](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L42-L54) 用 `export PARAM_<名字>:=<值>` 声明参数，再由 [tb/eth_mac_1g/Makefile:59](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L59) 的 `-P $(TOPLEVEL).<名字>=<值>` 注入；[tb/eth_mac_1g/Makefile:75](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L75) `include $(shell cocotb-config --makefiles)/Makefile.sim` 把 cocotb 的整套仿真流程接进来。

**两代端点的对照**——这是最容易踩坑的地方。顶层遗留文件用 myhdl：

[tb/test_eth_mac_1g.py:26](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.py#L26) 是 `from myhdl import *`，[tb/test_eth_mac_1g.py:164](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.py#L164) 用 `Cosimulation("vvp -m myhdl ...")` 联通仿真器——典型的 myhdl 时代写法。而子目录里的现代版直接用 cocotbext：

```python
# tb/eth_mac_1g/test_eth_mac_1g.py（现代 cocotb 版）
import cocotb
from cocotb.clock import Clock
from cocotb.regression import TestFactory
from cocotbext.eth import GmiiFrame, GmiiSource, GmiiSink, PtpClockSimTime
from cocotbext.axi import AxiStreamBus, AxiStreamSource, AxiStreamSink, AxiStreamFrame
```

对照可见命名上的「血统」：遗留 `AXIStreamSource`（[tb/axis_ep.py:249](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_ep.py#L249)）→ 现代 `AxiStreamSource`；遗留 `GMIISource` → 现代 `GmiiSource`；遗留 `EthFrame`（[tb/eth_ep.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_ep.py)）→ 现代 scapy `Ether` + `GmiiFrame`。API 形似但底层从 myhdl 换成了 cocotbext，功能也丰富得多（自带反压、FCS、PTP）。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次现代 testbench，确认仿真栈通。

**操作步骤**（前提：已按 u1-l4 装好 cocotb、cocotbext-eth/axi、iverilog）：

1. 进入目录 `tb/eth_mac_1g/`。
2. 直接 `make`（等价于 `SIM=icarus make sim`）。
3. 也可在仓库根目录 `pytest tb/eth_mac_1g/test_eth_mac_1g.py -k "rx and gmii"` 只跑收向、GMII 模式的子集。

**需要观察的现象**：终端先打印 iverilog 编译若干 `.v`，随后 cocotb 逐个用例输出 `RX frame PTP TS: ... ns` 之类的日志，最后 `PASS`。

**预期结果**：`make` 退出码 0，且能看到被 `VERILOG_SOURCES` 列出的 RTL 文件（如 `eth_mac_1g.v`、`axis_gmii_rx.v`、`lfsr.v` 等）确实参与了编译。

> 若本机未装齐工具链，运行结果**待本地验证**；可退而用「源码阅读型实践」：打开 `tb/eth_mac_1g/Makefile`，数一数 `VERILOG_SOURCES` 列了几个文件，思考为什么 `axis_gmii_rx.v` 必须显式列出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tox.ini` 要用 `--ignore-glob=tb/test_*.py`？
**答案**：顶层 `tb/test_*.py` 是 myhdl 时代的遗留（`from myhdl import`、`vvp -m myhdl`），当前 cocotb 流程不编译它们；不排除会导致 pytest 误收集、报 import 错误。

**练习 2**：现代 `test_eth_mac_1g.py` 为什么「既是 cocotb 用例又是 pytest 用例」？
**答案**：文件顶部 `if cocotb.SIM_NAME:` 块在仿真器内注册用例（供 `make` 用），底部 `def test_eth_mac_1g(...)` 调 `cocotb_test.simulator.run(...)` 供 pytest 用——同一份驱动代码，两个触发入口。

---

### 4.2 cocotbext-eth：物理层驱动与仿真 PTP 时钟

#### 4.2.1 概念说明

`cocotbext-eth` 把「物理接口」抽象成端点，让你用高层帧对象驱动底层逐拍信号：

- **`GmiiSource`/`GmiiSink`**：包住 GMII 的 `rxd`/`rx_dv`/`rx_er`（或 TX 侧 `txd`/`tx_en`/`tx_er`），并吃进 `rx_clk_enable`/`rx_mii_select`，所以三模（10/100/1000M）速率适配对测试代码是透明的。
- **`XgmiiSource`/`XgmiiSink`**：包住 10G 的数据总线 `xgmii_rxd` 与控制总线 `xgmii_rxc`，自动处理 `START`/`TERM` 控制字符与 lane 对齐。
- **`MiiPhy`/`GmiiPhy`/`RgmiiPhy`**：把整块 PHY（含 PHY↔MAC 的全部 IO 与时钟）封成一个对象，常用于带 `_fifo`/`_phy` 后缀的整机 testbench。
- **`PtpClockSimTime`**：把仿真器的墙钟时间换算成 96 位 ToD 写到 `ptp_ts` 信号上，充当「硬件 PTP 时钟」的替身，用来校验 DUT 的时间戳精度。
- **`GmiiFrame`/`XgmiiFrame`**：高层帧对象，`from_payload(data)` 一行完成「补前导码/SFD + 算 FCS」，并在发送时回调记录 SFD 的仿真时刻（`sim_time_sfd`），供 PTP 校验。

#### 4.2.2 核心流程

接收方向测试（驱动 PHY 侧、采集 AXI 侧）：

1. 构造 `GmiiSource` 绑到 `gmii_rxd/rx_dv/rx_er` 与 `rx_clk`，并传入 `rx_clk_enable`/`rx_mii_select`。
2. `GmiiFrame.from_payload(payload, tx_complete=列表.append)` 造帧；`tx_complete` 回调把每帧的 `sim_time_sfd`（SFD 离开源的时刻）存起来，作为 PTP「真值」。
3. `await gmii_source.send(frame)` 逐拍驱动 PHY 输入。
4. 在 AXI 侧 `await axis_sink.recv()` 收帧，从 `tuser` 高位取出 DUT 打的时间戳，与 `sim_time_sfd` 比对，误差应小于一个采样精度。

10G 侧完全同构，只换 `XgmiiSource(dut.xgmii_rxd, dut.xgmii_rxc, ...)`，并注意 `start_lane`（帧从 lane 0 还是 lane 4 起）会引入半拍偏移。

#### 4.2.3 源码精读

**GMII 端点与 PTP 时钟的创建**（千兆 MAC testbench）：

[tb/eth_mac_1g/test_eth_mac_1g.py:65-71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L65-L71) 启动 8 ns（125 MHz）的 `rx_clk`/`tx_clk`，并把 `GmiiSource`/`GmiiSink` 绑到 GMII 信号上——注意它把 `rx_clk_enable`/`rx_mii_select` 也一并交给端点，所以测试代码无需关心速率分频：

```python
self.gmii_source = GmiiSource(dut.gmii_rxd, dut.gmii_rx_er, dut.gmii_rx_dv,
    dut.rx_clk, dut.rx_rst, dut.rx_clk_enable, dut.rx_mii_select)
self.gmii_sink   = GmiiSink(dut.gmii_txd, dut.gmii_tx_er, dut.gmii_tx_en,
    dut.tx_clk, dut.tx_rst, dut.tx_clk_enable, dut.tx_mii_select)
```

[tb/eth_mac_1g/test_eth_mac_1g.py:76-78](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L76-L78) 用 `PtpClockSimTime(ts_tod=dut.rx_ptp_ts, clock=dut.rx_clk)` 把仿真时间灌成 96 位 ToD，充当硬件 PTP 时钟的「金标准」。

**接收侧用 `from_payload` 造帧并校验时间戳**：

[tb/eth_mac_1g/test_eth_mac_1g.py:204-205](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L204-L205) 一行造帧，`tx_complete=tx_frames.append` 把发送回调挂上；[tb/eth_mac_1g/test_eth_mac_1g.py:211-223](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L211-L223) 从收帧的 `tuser` 拆出坏帧位（`& 1`）与 PTP 时间戳（`>> 1`），换算成纳秒后与 `sim_time_sfd` 比对，断言误差 `< 0.01` ns：

```python
frame_error = rx_frame.tuser & 1
ptp_ts = rx_frame.tuser >> 1
ptp_ts_ns = ptp_ts / 2**16
tx_frame_sfd_ns = get_time_from_sim_steps(tx_frame.sim_time_sfd, "ns")
assert abs(ptp_ts_ns - tx_frame_sfd_ns - (32 if enable_gen else 8)) < 0.01
```

**XGMII 端点（10G 侧）**：

[tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py:38](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py#L38) 导入 `XgmiiFrame, XgmiiSource, PtpClockSimTime`；[tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py:49-54](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py#L49-L54) 用 6.4 ns（156.25 MHz）时钟，并把 `XgmiiSource` 同时绑到数据 `xgmii_rxd` 与控制 `xgmii_rxc`——这是它与 GMII 端点的关键区别。[tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py:96-98](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py#L96-L98) 处理 lane swap：当 `start_lane == 4` 时 SFD 多报告了一整拍，要减去半个时钟周期（3.2 ns）。

> 对比遗留：旧 `tb/gmii_ep.py` 的 `GMIISource` 只能逐字节驱动，前导码/FCS 要测试代码自己拼；现代 `GmiiFrame.from_payload` 一行搞定，这正是 cocotbext-eth 的价值。

#### 4.2.4 代码实践

**实践目标**：理解 GMII 端点如何吞掉速率适配细节。

**操作步骤**：

1. 打开 [tb/eth_mac_1g/test_eth_mac_1g.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py)，定位 `run_test_rx`（[L184](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L184)）。
2. 找到 `GmiiFrame.from_payload`（[L204](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L204)），确认它没有手动拼前导码/SFD/FCS。
3. 跟踪 `rx_clk_enable` 是怎么传给 `GmiiSource` 的（[L68-69](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L68-L69)）。

**需要观察的现象**：测试代码里看不到任何 `0x55` 前导码、`0xD5` SFD 或 FCS 字节，也看不到分频计数器。

**预期结果**：你会确认——所有物理层细节（前导码、FCS、clk_enable 分频、半字节拼接）都被 `GmiiSource`/`GmiiFrame` 封装了，测试只关心 payload 与时间戳。结果**待本地运行验证**。

#### 4.2.5 小练习与答案

**练习 1**：`GmiiSource` 为什么要吃进 `rx_clk_enable` 和 `rx_mii_select`？
**答案**：千兆 MAC 在单一 125 MHz 时钟下用 `clk_enable`（分频跳周期）与 `mii_select`（切 4 位半字节）覆盖 10/100/1000M；端点吃进这两根线后，测试代码对三种速率写法一致，端点内部按使能节拍驱动。

**练习 2**：`run_test_rx` 里 PTP 时间戳的「真值」从哪来？
**答案**：来自 `GmiiFrame` 发送时的回调 `tx_complete`，它把 SFD 离开 `GmiiSource` 的仿真时刻记进 `tx_frame.sim_time_sfd`；这是软件侧的「发送时刻」，用来校验硬件 `tuser` 里打的时间戳是否准确。

---

### 4.3 cocotbext-axi：AXI-Stream 端点与 define_stream 侧带总线

#### 4.3.1 概念说明

`cocotbext-axi` 提供两类能力：

- **`AxiStreamSource`/`AxiStreamSink`**：驱动/采集 AXI-Stream。`AxiStreamBus.from_prefix(dut, "tx_axis")` 会按前缀自动收集 `tx_axis_tdata/tvalid/tready/tlast/tuser/...` 信号，省得逐根列；`AxiStreamFrame(data, tuser=...)` 是高层帧对象。
- **`define_stream`**：本项目大量模块除主数据流外，还有「并行头握手」（如 `eth_axis_rx` 的 `hdr_valid/hdr_ready` + 头字段）或「时间戳回送」（如 `tx_axis_ptp_ts` + `tx_axis_ptp_ts_valid` + `ts_tag`）这类独立小总线。`define_stream` 给这种总线现场生成一对 `XxxSource/XxxSink/XxxBus/XxxTransaction`，写法和 AXI 端点完全一致。

`AxiStreamSink` 还内置 AXI-Stream 合法性检查（`tkeep` 连续无空洞、末拍对齐等），相当于一个免费的协议监视器。

#### 4.3.2 核心流程

发送方向测试（驱动 AXI 侧、采集 PHY 侧 + 时间戳侧带）：

1. `AxiStreamSource(AxiStreamBus.from_prefix(dut, "tx_axis"), dut.tx_clk, dut.tx_rst)` 绑定 TX AXI 接口。
2. 用 `define_stream` 给 `tx_axis_ptp_*` 侧带总线造一个 `PtpTsSink`。
3. `await axis_source.send(AxiStreamFrame(data, tuser=2))` 发帧（`tuser=2` 即 bit0=0 好帧、bit1=1 请求打时间戳，对应 `TX_PTP_TS_CTRL_IN_TUSER`）。
4. `rx_frame = await gmii_sink.recv()` 收线路帧；`ptp_ts = await tx_ptp_ts_sink.recv()` 收配套的旁带时间戳，二者按顺序配对。

#### 4.3.3 源码精读

**AXI 端点与前缀绑定**：

[tb/eth_mac_1g/test_eth_mac_1g.py:73-74](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L73-L74) 用 `AxiStreamBus.from_prefix(dut, "tx_axis")` / `"rx_axis"` 自动归集同前缀的全部 AXI 信号：

```python
self.axis_source = AxiStreamSource(AxiStreamBus.from_prefix(dut, "tx_axis"), dut.tx_clk, dut.tx_rst)
self.axis_sink   = AxiStreamSink(AxiStreamBus.from_prefix(dut, "rx_axis"), dut.rx_clk, dut.rx_rst)
```

**用 `define_stream` 现场造 PTP 时间戳端点**：

[tb/eth_mac_1g/test_eth_mac_1g.py:47-50](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L47-L50) 一行 `define_stream` 就为 `tx_axis_ptp_ts/ts_valid/ts_tag` 这组侧带信号生成整套端点类型：

```python
PtpTsBus, PtpTsTransaction, PtpTsSource, PtpTsSink, PtpTsMonitor = define_stream("PtpTs",
    signals=["ts", "ts_valid"],
    optional_signals=["ts_tag", "ts_ready"])
```

[tb/eth_mac_1g/test_eth_mac_1g.py:78](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L78) 用它造 sink 绑到 `tx_axis_ptp` 前缀，于是 `await self.tx_ptp_ts_sink.recv()` 就能像收 AXI 帧一样收旁带时间戳。同一手法在 [tb/arp/test_arp.py:45-57](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/arp/test_arp.py#L45-L57) 用来造 `EthHdr`/`ArpReq`/`ArpResp` 端点——驱动 ARP 模块的「并行头 + 请求/应答」侧信道。

**发帧时把 PTP 请求塞进 `tuser`**：

[tb/eth_mac_1g/test_eth_mac_1g.py:250](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L250) `AxiStreamFrame(test_data, tuser=2)`——bit0 是坏帧位、bit1 是「请打时间戳」请求，正好对上 u4-l3 讲的 `TX_PTP_TS_CTRL_IN_TUSER` 机制。

> 对比遗留：旧 `tb/axis_ep.py` 的 `AXIStreamFrame`（[L29](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_ep.py#L29)）/`AXIStreamSource`（[L249](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_ep.py#L249)）是手写的 myhdl 驱动，没有 `from_prefix` 自动归集、没有 `define_stream`、也没有内置 `tkeep` 合法性断言；旧 `tb/eth_ep.py` 还要手写 `build_axis`/`calc_fcs`（[L63-80](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_ep.py#L63-L80)）。现代 cocotbext 把这些都内化了。

#### 4.3.4 代码实践

**实践目标**：体会 `define_stream` 对「侧带总线」的简化。

**操作步骤**：

1. 读 [tb/eth_mac_1g/test_eth_mac_1g.py:47-50](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L47-L50)，看清 `signals` 与 `optional_signals` 的区别。
2. 再打开 [tb/arp/test_arp.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/arp/test_arp.py)，看它如何为 `EthHdr`、`ArpReq`、`ArpResp` 三组侧带各调一次 `define_stream`。
3. 想象：如果没有 `define_stream`，你要手写多少行 `await RisingEdge` + 信号赋值才能驱动一组 `arp_request`/`arp_response` 握手？

**需要观察的现象**：侧带总线的驱动代码与 AXI 主流的驱动代码「长得一样」（都是 `Source.send`/`Sink.recv`）。

**预期结果**：你会确认 `define_stream` 让任意一组「valid/ready + 字段」信号都获得与 AXI-Stream 同等的待遇，这是本库 testbench 能保持高度一致风格的根因。

#### 4.3.5 小练习与答案

**练习 1**：`AxiStreamBus.from_prefix(dut, "tx_axis")` 帮你省了什么事？
**答案**：它按 `tx_axis_` 前缀自动收集 `tdata/tvalid/tready/tlast/tuser/...` 全部信号并绑成一个 Bus 对象，免去你逐根 `dut.tx_axis_tdata` 地列，也避免漏绑某根导致驱动不完整。

**练习 2**：为什么 PTP 时间戳回送要用 `define_stream` 而不是 AXI-Stream 端点？
**答案**：TX 时间戳是发送结束后才经旁带总线（`tx_axis_ptp_ts` + `ts_valid` + `ts_tag`）异步回送的，不是随主数据流的带内 `tuser`；它的信号集与 AXI-Stream 不同，用 `define_stream` 按其实际信号现场造一对专属端点最贴切。

---

### 4.4 随机化与覆盖率测试套路

#### 4.4.1 概念说明

verilog-ethernet 不靠「写一个超级用例覆盖一切」，而靠 **参数组合 × 边界扫描** 自动铺开覆盖率：

- **`TestFactory`**：给一个 `run_test_*` 协程，声明每个参数有哪几个取值（如 `enable_gen=[None, cycle_en]`、`mii_sel=[False, True]`），它做笛卡尔积，每个组合生成一个独立 cocotb 用例。
- **`size_list`**：帧长扫描表，刻意覆盖边界——最小帧长附近（`range(60,128)`）、中长（`512`）、超长（`1514`，接近 MTU）、并重复多次最小帧（`[60]*10`）来撞填充/PAD 逻辑。
- **两种「反压」**：① `enable_gen=cycle_en`（`itertools.cycle([0,0,0,1])`）驱动 `rx/tx_clk_enable`，每 4 拍禁 1 拍，模拟 MII 速率节流；② `axis_source.pause = True` 直接掐断 AXI 源，模拟上游供不上数据 → 触发 TX **欠载（underrun）**。
- **坏帧注入**：发帧时设 `tuser=1`（坏帧位），验证 DUT 在线路侧把错误传播到 `gmii_tx_er`。
- **scapy 构造报文**：流控测试用 `Ether(...) / struct.pack(...)` 现拼 PAUSE/PFC 控制帧，比手写字节直观。

#### 4.4.2 核心流程

以 `run_test_tx` 的覆盖铺开为例：

1. 定义取值表：`size_list`（80 种长度）、`incrementing_payload`（递增字节）、`cycle_en`（节流波形）。
2. `TestFactory(run_test_tx).add_option("payload_lengths", [size_list])` …… 给 5 个参数各列取值。
3. `factory.generate_tests()` 自动生成全部组合，每个组合成一个形如 `run_test_tx_001`、`run_test_tx_002` 的用例。
4. 单个用例内部 `for test_data in test_frames:` 把 80 个长度一次性发完、收完、逐帧断言——所以「覆盖」是参数轴 × 帧长轴两维叠加。

参数轴的用例数可写成笛卡尔积：

\[
N_{\text{用例}} = |\text{payload\_lengths}| \times |\text{payload\_data}| \times |\text{ifg}| \times |\text{enable\_gen}| \times |\text{mii\_sel}|
\]

对 `run_test_rx/tx`：\(1 \times 1 \times 1 \times 2 \times 2 = 4\) 个用例，每个内部跑 `size_list` 的 80 种长度，共覆盖 \(4 \times 80 = 320\) 种「参数×帧长」组合。

#### 4.4.3 源码精读

**取值表与波形生成器**：

[tb/eth_mac_1g/test_eth_mac_1g.py:659-660](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L659-L660) 是 `size_list`——`range(60,128)`（68 个，扫最小帧长边界）+ `[512, 1514]`（2 个，中长/接近 MTU）+ `[60]*10`（10 个，反复撞 PAD）；[tb/eth_mac_1g/test_eth_mac_1g.py:663-668](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L663-L668) 定义递增载荷与 `cycle_en` 节流波形：

```python
def size_list():
    return list(range(60, 128)) + [512, 1514] + [60]*10
def incrementing_payload(length):
    return bytearray(itertools.islice(itertools.cycle(range(256)), length))
def cycle_en():
    return itertools.cycle([0, 0, 0, 1])
```

**`TestFactory` 笛卡尔积**：

[tb/eth_mac_1g/test_eth_mac_1g.py:671-697](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L671-L697) 对每个 `run_test_*` 注册取值并 `generate_tests()`；注意 [L691](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L691) 用 `if cocotb.top.PFC_ENABLE.value:` 按 RTL 参数决定是否生成 LFC/PFC 用例——测试规模随 DUT 配置自适应。

**欠载（underrun）场景——中途掐断 AXI 源**：

[tb/eth_mac_1g/test_eth_mac_1g.py:275-323](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L275-L323) `run_test_tx_underrun`：先连发 3 帧，发到第 2 帧中途 `tb.axis_source.pause = True`（[L303](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L303)），使 MAC 在帧内供不上数据，断言该帧在线路侧带 `error[-1]==1`（[L317](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L317)），其余两帧正常。

**坏帧注入场景——`tuser=1`**：

[tb/eth_mac_1g/test_eth_mac_1g.py:329-363](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L329-L363) `run_test_tx_error`：发 3 帧，第 2 帧设 `test_frame.tuser = 1`（[L350](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L350)），断言该帧在线路侧 `error[-1]==1`（[L357](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L357)），验证坏帧位被正确传播成 `gmii_tx_er`。

**PAUSE/PFC 流控场景——用 scapy 现拼控制帧**：

[tb/eth_mac_1g/test_eth_mac_1g.py:441-446](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L441-L446)（LFC）与 [tb/eth_mac_1g/test_eth_mac_1g.py:592-597](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L592-L597)（PFC）用 `Ether(...) / struct.pack('!HH', 0x0001, 100)` 一行构造目的为组播地址 `01:80:C2:00:00:01`、EtherType `0x8808` 的真实 PAUSE 控制帧，注入 RX 侧验证 MAC 是否暂停发送。

#### 4.4.4 代码实践（本讲指定实践任务）

**实践目标**：通读 `test_eth_mac_1g.py`，列出它用到的全部端点与至少三种测试场景。

**操作步骤**：

1. 打开 [tb/eth_mac_1g/test_eth_mac_1g.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py)，先看 `TB.__init__`（[L53-L131](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L53-L131)），列出创建的端点。
2. 再依次定位 `run_test_rx`/`run_test_tx`/`run_test_tx_underrun`/`run_test_tx_error`/`run_test_lfc`/`run_test_pfc`，各用一句话概括场景。
3. 最后看 [L671-L697](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L671-L697) 的 `TestFactory`，数每个 `run_test_*` 被展开成几个用例。

**需要观察的现象（参考答案）**：

- **端点清单**：`GmiiSource`、`GmiiSink`（物理层）；`AxiStreamSource`、`AxiStreamSink`（AXI-Stream 主流）；`PtpClockSimTime` ×2（rx/tx 仿真 PTP 时钟）；`PtpTsSink`（`define_stream` 造的旁带时间戳端点）。
- **至少三种场景**：
  1. **变长帧收发**（`run_test_rx`/`run_test_tx`，`size_list` 扫 80 种长度，校验 payload、FCS、PTP 时间戳）。
  2. **TX 欠载**（`run_test_tx_underrun`，中途 `axis_source.pause=True`，断言该帧带 `error`）。
  3. **坏帧注入**（`run_test_tx_error`，`tuser=1`，断言线路侧 `gmii_tx_er`）。
  4. （进阶）**PAUSE/PFC 流控**（`run_test_lfc`/`run_test_pfc`，scapy 注入控制帧，断言 MAC 暂停发送、并回发对应控制帧）。

**预期结果**：你能用一张表把「端点 → 绑定的信号前缀 → 作用」对应清楚，并能说出每种场景注入了什么故障、断言了什么。

#### 4.4.5 小练习与答案

**练习 1**：`size_list` 为什么特意把 `[60]*10` 重复 10 次？
**答案**：60 字节正好是最小帧长边界，`ENABLE_PADDING` 会让短帧补零到 64；重复多次是为了反复撞 PAD/FCS 边界，提高该临界逻辑的命中率，避免偶发 bug 漏网。

**练习 2**：`cycle_en`（`itertools.cycle([0,0,0,1])`）和 `axis_source.pause` 各模拟什么？
**答案**：`cycle_en` 驱动 `rx/tx_clk_enable`，每 4 拍禁 1 拍，模拟 MII 半速率节流（一种线速反压）；`axis_source.pause` 直接停掉 AXI 源的 `tvalid`，模拟上游供数不足，用来触发 TX 欠载。前者改变的是物理层节拍，后者改变的是 AXI 源行为，二者层次不同。

**练习 3**：为什么 LFC/PFC 用例要用 `if cocotb.top.PFC_ENABLE.value:` 包起来？
**答案**：当 RTL 参数 `PFC_ENABLE=0` 时流控子模块不综合、相关端口不存在，强行跑会报信号缺失；按参数门控让测试规模随 DUT 配置自适应。

## 5. 综合实践

**任务**：把本讲三类套路（cocotbext-eth 物理端点、cocotbext-axi/`define_stream` 主流与侧带、`TestFactory` 覆盖铺开）串起来，对照阅读「收」与「发」两条通路，画一张 testbench 结构图。

**操作步骤**：

1. 在 `tb/eth_mac_1g/test_eth_mac_1g.py` 中，分别画出 `run_test_rx`（PHY→AXI）与 `run_test_tx`（AXI→PHY + 旁带时间戳）的数据流向图，标注每一段用哪个端点 `send`/`recv`。
2. 标出两条通路各自的「时间戳真值」来源：RX 路用 `GmiiFrame` 的 `sim_time_sfd`，TX 路用 `PtpTsSink` 收到的旁带 `ts`。
3. 在图上用虚线标出 `enable_gen`/`cycle_en` 与 `axis_source.pause` 这两条「反压注入」分别作用在哪一段。
4. 列出 `TestFactory` 给 `run_test_rx` 展开的 4 个用例的 `(enable_gen, mii_sel)` 组合，确认它们正是 `2×2` 的笛卡尔积。

**验收标准**：

- 图中能清晰区分「主流（AXI-Stream/GMII）」与「侧带（PtpTs）」两类端点。
- 能指出 `run_test_tx` 里 `await tb.gmii_sink.recv()` 与 `await tb.tx_ptp_ts_sink.recv()` 是**成对**出现的（每帧一个线路帧 + 一个旁带时间戳）。
- 能解释为何 `size_list` 同时含边界长度与重复长度。

> 这是纯阅读型实践，无需运行仿真即可完成；若本地工具链就绪，可额外用 `pytest tb/eth_mac_1g -v` 观察自动展开出的用例名（形如 `run_test_rx_011`），与你的笛卡尔积核对。

## 6. 本讲小结

- verilog-ethernet 的仿真栈是 **cocotb + cocotbext-eth + cocotbext-axi + scapy + Icarus Verilog**；`tox.ini` 钉死全部版本、用 `--ignore-glob=tb/test_*.py` 排除 myhdl 遗留，回归走 `pytest -n auto`。
- `cocotbext-eth` 把 GMII/XGMII/PHY 物理接口封成 `GmiiSource`/`XgmiiSource`/`*Phy`，吃进 `clk_enable`/`mii_select` 后测试代码对三档速率写法一致；`PtpClockSimTime` 用仿真墙钟当 PTP 时钟金标准。
- `cocotbext-axi` 用 `AxiStreamBus.from_prefix` 自动归集 AXI 信号，`AxiStreamSink` 内置 `tkeep` 合法性检查；`define_stream` 为 PTP 时间戳、以太网头、ARP 请求/应答等侧带总线现场造端点，使全库 testbench 风格统一。
- 覆盖率靠 **`TestFactory` 笛卡尔积 × `size_list` 边界扫描** 自动铺开；两类反压（`cycle_en` 节流、`axis_source.pause` 欠载）与坏帧注入（`tuser=1`）构成专项故障场景。
- 务必辨析「两代端点」：顶层 `tb/axis_ep.py`/`eth_ep.py`/`gmii_ep.py`/`test_*.py` 是 myhdl 遗留，**当前在用的是子目录 `tb/<模块>/test_<模块>.py`**；二者 API 形似（`AXIStreamSource`→`AxiStreamSource`）但底层不同，别开错文件。
- 现代 testbench 仍是「双用途」：顶部 `if cocotb.SIM_NAME:` 供 `make` 加载，底部 `def test_<module>` 调 `cocotb_test.simulator.run` 供 pytest 收集。

## 7. 下一步学习建议

- **下一篇 u13-l2《编写与扩展 testbench》**：动手为一个新模块（如 `axis_eth_fcs`）从零写一份三件套——照搬 `tb/eth_mac_1g` 的 `Makefile`/`TB` 类/`TestFactory` 模板，把本讲的三类端点真正用起来。
- **横向对比**：读 `tb/eth_mac_10g_fifo/test_eth_mac_10g_fifo.py` 看 10G 侧如何用 `XgmiiSource`/`XgmiiSink` + `define_stream` 组合，与千兆版对照体会位宽差异。
- **协议栈层**：读 `tb/udp_complete/test_udp_complete.py`（若存在）或 `tb/arp/test_arp.py`，看上层协议 testbench 如何用多层 `define_stream` 端点（`EthHdr`/`ArpReq`/`ArpResp`）驱动模块的并行头侧信道。
- **深入 cocotbext**：有空可读 cocotbext-eth/axi 的源码（pip 安装的包），理解 `GmiiSource` 内部如何把一帧拆成逐拍 GMII 时序——这能帮你日后写自定义端点。
