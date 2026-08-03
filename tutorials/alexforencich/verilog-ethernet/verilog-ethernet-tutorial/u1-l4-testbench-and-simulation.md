# 测试框架与仿真运行方式

## 1. 本讲目标

本讲是入门单元（u1）的第四篇。在 u1-l2 里我们已经建立了仓库的目录地图，知道 `rtl/` 是被测试的中心、`tb/` 是仿真测试。本讲要回答一个紧接着的问题：**这些 IP 核到底怎么验证、怎么跑起来？**

学完本讲你应该能够：

1. 说清 verilog-ethernet 的仿真体系是基于 **cocotb + cocotbext-eth + cocotbext-axi + Icarus Verilog** 的，并理解每个组件扮演的角色。
2. 打开 `tb/` 下任意一个模块目录，看懂它的测试套件由哪些文件组成、各自负责什么。
3. 用三种方式之一（`make`、`pytest`、`tox`）把一个 testbench 跑起来，并能解释 `tox.ini` 与 `Makefile` 各自控制了什么。
4. 识别出哪些文件是「当前在用」的 cocotb 测试，哪些是**历史遗留**的 myhdl 式测试台——这是初学者最容易踩坑的地方。

本讲只讲「框架与运行方式」，不深入如何编写复杂的驱动与断言（那是专家层 u13「测试方法学」的内容）。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l2 仓库结构与目录组织**：知道 `rtl/`、`tb/`、`example/`、`lib/` 各自的职责。
- **u1-l3 AXI-Stream 接口约定**：知道 `tvalid/tready/tlast/tuser` 的握手语义，因为 testbench 里有大量驱动这些信号的代码。

下面几个术语本讲会用到，先做最简解释：

| 术语 | 一句话解释 |
| --- | --- |
| **仿真器（simulator）** | 把 Verilog 代码当「数字电路」来跑、观察波形与信号值的工具。本项目用 **Icarus Verilog（iverilog）**，一个开源 Verilog 仿真器。 |
| **testbench** | 给被测模块（DUT, Design Under Test）施加激励、检查输出的「测试台」。传统做法是用 Verilog 写，本项目用 **Python** 写。 |
| **cocotb** | *Coroutine based Co-simulation TestBench* 的缩写。它让你用 Python 协程写 testbench，通过 VPI 接口驱动 HDL 仿真器。 |
| **cocotbext-axi / cocotbext-eth** | 预先写好的「驱动库」。比如 `AxiStreamSource` 能像模像样地按握手协议往 DUT 推一帧数据，`GmiiSink` 能从 GMII 管脚采集字节流，省得你手写时序。 |
| **cocotb-test** | 让 `pytest` 能够直接驱动 cocotb 仿真的桥接库，使我们能用一条 `pytest` 命令跑回归。 |
| **tox / pytest** | Python 的测试运行器：`pytest` 负责发现并执行用例，`tox` 负责在一个干净虚拟环境里装好固定版本的依赖再跑 `pytest`。 |

如果你完全没接触过硬件仿真，可以这样建立直觉：仿真器是一台「可暂停、可单步」的虚拟电路，cocotb 则是一个站在电路旁边的「Python 机器人」，它能在每个时钟上升沿醒来、读写电路信号、再睡到下一个上升沿。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `README.md`（Testing 段，L594-596） | 官方对「需要哪些工具、有哪几种运行方式」的权威说明。 |
| `tox.ini` | 定义 `tox` 虚拟环境、**锁定全部依赖版本**、配置 `pytest` 的测试搜索范围与忽略规则。 |
| `tb/eth_mac_1g/Makefile` | 单个模块的 cocotb Makefile，声明 `VERILOG_SOURCES`、模块参数、仿真器选择，并 `include` cocotb 自带的 `Makefile.sim`。 |
| `tb/eth_mac_1g/test_eth_mac_1g.py` | 千兆 MAC 的 cocotb 测试模块，**双用途**：既能被 `make` 加载，又能被 `pytest` 当作测试入口。 |
| `tb/test_eth_mac_1g.v` | 历史遗留的 myhdl 式 Verilog 测试台封装（`$from_myhdl/$to_myhdl`），当前流程不再使用。 |
| `tb/test_eth_mac_1g.py`（顶层） | 历史遗留的 myhdl 式 Python 测试脚本（`from myhdl import *`），已被 `tox.ini` 显式忽略。 |

> 提示：第 4.2 节会专门讲清「在用」与「遗留」两组文件的区别，这是本讲最容易混淆的点，请留意。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**cocotb 仿真体系**、**tb 测试套件组织**、**pytest/tox 运行方式**。

### 4.1 cocotb 仿真体系

#### 4.1.1 概念说明

verilog-ethernet 的 RTL 是可综合的 Verilog，但 RTL 本身不会自己产生输入、也不会断言「结果对不对」。验证它需要一个 testbench。

本项目没有采用「用 Verilog 写 testbench」的传统路线，而是用 **cocotb**：用 Python 写测试逻辑。这样做的好处是：

- Python 表达力强，构造随机帧、解析协议头、写断言都比 Verilog 的 `task/initial` 块轻松得多。
- 可以直接复用成熟的 Python 库，例如本项目就用 **scapy** 来构造/解析以太网、IP、UDP 报文。
- 有现成的总线驱动库 **cocotbext-axi**（驱动 AXI-Stream）和 **cocotbext-eth**（驱动 GMII/XGMII 等以太网物理接口），你不必手写每一拍的握手时序。

底层仿真器用的是开源的 **Icarus Verilog（iverilog）**。cocotb 通过 VPI（Verilog Procedural Interface）把 Python 协程与 iverilog 的仿真时间轴绑定在一起。

README 的 Testing 段把这套依赖说得很清楚：

> Running the included testbenches requires cocotb, cocotbext-axi, cocotbext-eth, and Icarus Verilog. The testbenches can be run with pytest directly (requires cocotb-test), pytest via tox, or via cocotb makefiles.

参见 [README.md:L594-L596](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L594-L596)（官方列出的工具链与三种运行方式）。

#### 4.1.2 核心流程

一次 cocotb 仿真的执行流程可以用下面的伪代码描述：

```
1. iverilog 编译 VERILOG_SOURCES 列出的全部 .v 文件
   → 生成可仿真模型，并以 TOPLEVEL 指定的模块为顶层
2. iverilog 加载 cocotb 提供的 VPI 共享库，把仿真时间轴交给 cocotb
3. cocotb 加载 MODULE 指定的 Python 文件（如 test_eth_mac_1g.py）
4. cocotb 扫描该文件里所有用 @cocotb.test() 装饰的协程函数
   （若用到 TestFactory.generate_tests()，还会自动展开成多个参数化用例）
5. 逐个运行测试协程：
     - 协程 await RisingEdge(clk)  →  让出，仿真器前进到下一个时钟上升沿
     - 协程恢复后读写 dut.xxx 信号、驱动源/检查汇
     - 遇到下一个 await 再让出 ……  如此循环
6. 所有测试结束 → 汇总 pass/fail → 仿真器退出
```

关键直觉：cocotb 的测试协程与仿真器的时钟是**同步**的。`await RisingEdge(clk)` 不是「睡一段墙钟时间」，而是「让仿真器跑到下一个 `clk` 上升沿再唤醒我」。这让 Python 代码能精确地按硬件节拍操作信号。

#### 4.1.3 源码精读

以千兆 MAC 的测试为例，先看它的 import 段，这里把整条工具链都露出来了：

[tb/eth_mac_1g/test_eth_mac_1g.py:L31-L44](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L31-L44) —— 引入 scapy（构造报文）、pytest、cocotb-test、cocotb 本体、以及 cocotbext-eth/cocotbext-axi 的现成驱动：

```python
from scapy.layers.l2 import Ether
import pytest
import cocotb_test.simulator
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotb.regression import TestFactory
from cocotbext.eth import GmiiFrame, GmiiSource, GmiiSink, PtpClockSimTime
from cocotbext.axi import AxiStreamBus, AxiStreamSource, AxiStreamSink, AxiStreamFrame
```

再看测试主体里 `TB` 类的构造函数，它把 DUT 包成了一个「能直接操作的对象」：建两个 8 ns 周期的时钟，再用现成驱动把 GMII 管脚和 AXI-Stream 端口接上：

[tb/eth_mac_1g/test_eth_mac_1g.py:L53-L78](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L53-L78) —— 建时钟、接 GMII/AXI 源与汇、挂 PTP 时钟：

```python
cocotb.start_soon(Clock(dut.rx_clk, 8, units="ns").start())
cocotb.start_soon(Clock(dut.tx_clk, 8, units="ns").start())

self.gmii_source = GmiiSource(dut.gmii_rxd, dut.gmii_rx_er, dut.gmii_rx_dv,
    dut.rx_clk, dut.rx_rst, dut.rx_clk_enable, dut.rx_mii_select)
self.gmii_sink   = GmiiSink(dut.gmii_txd, dut.gmii_tx_er, dut.gmii_tx_en,
    dut.tx_clk, dut.tx_rst, dut.tx_clk_enable, dut.tx_mii_select)
self.axis_source = AxiStreamSource(AxiStreamBus.from_prefix(dut, "tx_axis"), dut.tx_clk, dut.tx_rst)
self.axis_sink   = AxiStreamSink(AxiStreamBus.from_prefix(dut, "rx_axis"), dut.rx_clk, dut.rx_rst)
```

这段代码体现了 cocotb 体系的核心收益：发送一帧数据只要 `await self.axis_source.send(frame)`，接收只要 `frame = await self.axis_sink.recv()`，握手时序、`tvalid/tready` 的配合全部由驱动库处理。这正是本项目能用 Python 高效验证 90+ 个 RTL 文件的根本原因。

#### 4.1.4 代码实践

**实践目标**：确认本机已具备 cocotb 仿真所需的最小工具链。

**操作步骤**：

1. 安装依赖（版本号参见 4.3 节的 `tox.ini`，或直接 `pip install cocotb cocotbext-axi cocotbext-eth cocotb-test`）。
2. 安装 Icarus Verilog（各发行版包名通常为 `iverilog`）。
3. 验证 cocotb 能找到自带的 Makefile 模板：

```bash
cocotb-config --makefiles
```

**需要观察的现象**：命令应打印一个类似 `.../cocotb/share/makefiles/` 的路径——这正是各模块 `Makefile` 末尾 `include` 的目标（见 4.2.3）。

**预期结果**：打印出一个存在的目录路径。**待本地验证**（本讲撰写环境未安装 cocotb-config，无法代你确认输出）。

#### 4.1.5 小练习与答案

**练习 1**：cocotb 里的 `await RisingEdge(dut.tx_clk)` 到底「等」的是什么？是墙钟时间还是仿真时间？

> **答案**：等的是**仿真时间**里的下一个 `tx_clk` 上升沿。它让 cocotb 协程暂停，把控制权交回 iverilog，由仿真器前进到下一个上升沿再把协程唤醒。

**练习 2**：为什么本项目要引入 `cocotbext-axi` 和 `cocotbext-eth`，而不是直接用 cocotb 原生 API 驱动信号？

> **答案**：因为 AXI-Stream 的 `tvalid/tready` 握手和 GMII 的字节流时序都需要逐拍精确配合，手写容易出错。这两个库提供了 `AxiStreamSource/Sink`、`GmiiSource/Sink` 等现成驱动，把一帧数据封装成对象即可收发，大幅降低测试代码量。

---

### 4.2 tb 测试套件组织（含「在用」与「遗留」的辨析）

#### 4.2.1 概念说明

`tb/` 目录里，**每个被测模块对应一个子目录**，子目录名就是模块名，例如 `tb/eth_mac_1g/`、`tb/axis_gmii_rx/`、`tb/ptp_clock/`。初学者很容易以为「三件套」= `Makefile + test_*.v + test_*.py` 三个文件都在用，但实际需要区分两组文件：

| 文件 | 位置 | 状态 | 作用 |
| --- | --- | --- | --- |
| `Makefile` | `tb/<模块>/` | **在用** | cocotb makefile 流的入口，声明源文件、参数、仿真器。 |
| `test_<模块>.py` | `tb/<模块>/` | **在用** | cocotb 测试模块（被 `make` 加载），**同时**也是 pytest 用例入口（含 `cocotb_test.simulator.run()`）。即「双用途」。 |
| `test_<模块>.v` | `tb/`（顶层） | **遗留** | 早期 myhdl 式 Verilog 封装，用 `$from_myhdl/$to_myhdl` 暴露信号，当前流程不再引用。 |
| `test_<模块>.py` | `tb/`（顶层） | **遗留** | 早期 myhdl 式 Python 脚本（`from myhdl import *`），已被 `tox.ini` 的 `--ignore-glob` 显式排除。 |

换句话说，**当前真正在用的「两件」是子目录里的 `Makefile` 与 `test_<模块>.py`**；顶层的 `test_*.v` 和 `test_*.py` 是 myhdl 时代留下的旧测试台。这一点很重要：当你想跑或改一个测试时，去子目录，不要去改顶层的遗留文件。

为什么 `tox.ini` 要特意忽略顶层的 `tb/test_*.py`？因为同一模块名下存在「旧 myhdl 版（顶层）」和「新 cocotb 版（子目录）」两份同名 Python，必须把旧的那份排除，pytest 才不会重复收集或报错（详见 4.3.3）。

#### 4.2.2 核心流程

`make` 命令消费 `Makefile` 的流程：

```
1. 读 Makefile，得到：
     DUT       = eth_mac_1g          （被测模块名）
     TOPLEVEL  = $(DUT)              （仿真顶层 = 模块本身，cocotb 直接实例化它）
     MODULE    = test_$(DUT)         （要加载的 Python 测试文件名）
     VERILOG_SOURCES = rtl/ 下若干 .v （要编译的源文件）
     PARAM_*   = 一组模块参数         （如 DATA_WIDTH=8）
2. 根据 SIM 变量（默认 icarus）拼出编译参数：
     - icarus: 用 iverilog 的 -P 参数覆盖：-P eth_mac_1g.DATA_WIDTH=8 ...
     - verilator: 用 -G 参数覆盖
3. include cocotb-config 提供的 Makefile.sim
   → 它负责真正调用 iverilog 编译 + 启动 cocotb + 跑 MODULE 里的测试
```

注意一个贯穿全库的约定：**参数覆盖统一走 `PARAM_` 前缀**。Makefile 用 shell 变量 `PARAM_DATA_WIDTH` 经 `-P` 传给 iverilog；cocotb-test 用环境变量 `PARAM_DATA_WIDTH` 传（见 4.3.3）。两边命名一致，所以同一份参数表能两种方式跑。

#### 4.2.3 源码精读

先看 `Makefile` 的核心声明段：

[tb/eth_mac_1g/Makefile:L21-L39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L21-L39) —— 选定语言/仿真器、指定顶层与测试模块、列出要编译的 RTL 源文件：

```makefile
TOPLEVEL_LANG = verilog
SIM ?= icarus
COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

DUT      = eth_mac_1g
TOPLEVEL = $(DUT)
MODULE   = test_$(DUT)
VERILOG_SOURCES += ../../rtl/$(DUT).v
VERILOG_SOURCES += ../../rtl/axis_gmii_rx.v
VERILOG_SOURCES += ../../rtl/axis_gmii_tx.v
VERILOG_SOURCES += ../../rtl/mac_ctrl_rx.v
VERILOG_SOURCES += ../../rtl/mac_ctrl_tx.v
VERILOG_SOURCES += ../../rtl/mac_pause_ctrl_rx.v
VERILOG_SOURCES += ../../rtl/mac_pause_ctrl_tx.v
VERILOG_SOURCES += ../../rtl/lfsr.v
```

要点：

- `SIM ?= icarus`：默认用 Icarus Verilog，但 `make SIM=verilator` 也能切到 Verilator（见下文同文件 L65-72），说明这套框架不绑死单一仿真器。
- `TOPLEVEL = $(DUT)`：**直接把 DUT 当顶层**，cocotb 自己实例化它，不需要额外的 Verilog 封装——这正是顶层 `test_*.v` 被淘汰的原因。
- `VERILOG_SOURCES`：注意它只列 RTL，**不包含** `test_*.v`；而且除了 `eth_mac_1g.v` 本身，还把它的全部子模块（`axis_gmii_rx/tx`、`mac_ctrl_*`、`mac_pause_ctrl_*`、`lfsr`）都显式列出，因为 iverilog 不会跨目录自动找文件。

接着是参数段：

[tb/eth_mac_1g/Makefile:L42-L54](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L42-L54) —— 一组 `PARAM_*` 模块参数（节选）：

```makefile
export PARAM_DATA_WIDTH := 8
export PARAM_ENABLE_PADDING := 1
export PARAM_MIN_FRAME_LENGTH := 64
export PARAM_PTP_TS_ENABLE := 1
export PARAM_PTP_TS_FMT_TOD := 1
...
export PARAM_PFC_ENABLE := 1
```

再看这些参数如何被注入仿真器（icarus 分支）：

[tb/eth_mac_1g/Makefile:L56-L75](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L56-L75) —— 把每个 `PARAM_*` 转成 iverilog 的 `-P toplevel.param=value` 覆盖，最后 `include` cocotb 的 `Makefile.sim`：

```makefile
ifeq ($(SIM), icarus)
    PLUSARGS += -fst
    COMPILE_ARGS += $(foreach v,$(filter PARAM_%,$(.VARIABLES)),-P $(TOPLEVEL).$(subst PARAM_,,$(v))=$($(v)))
    ...
endif
include $(shell cocotb-config --makefiles)/Makefile.sim
```

`foreach ... -P $(TOPLEVEL).$(subst PARAM_,,$(v))=$($(v))` 这一句展开后大致是 `-P eth_mac_1g.DATA_WIDTH=8 -P eth_mac_1g.ENABLE_PADDING=1 ...`，这正是 iverilog 覆盖模块 parameter 的语法。

> 旁支：`WAVES ?= 0`（L24）为 1 时会把 `iverilog_dump.v` 加入源文件并 dump 波形到 `.fst`（见 L61-64 与 L77-83 的自动生成规则），调试时很有用。

最后，作为对照，看一眼**遗留**的 Verilog 封装长什么样，理解它为何被淘汰：

[tb/test_eth_mac_1g.v:L89-L131](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.v#L89-L131) —— 用 myhdl 的 PLI 调用 `$from_myhdl(...)` / `$to_myhdl(...)` 把 reg/wire 信号导出给外部 Python：

```verilog
initial begin
    $from_myhdl(clk, rst, current_test, ... );   // 输入信号
    $to_myhdl(tx_axis_tready, rx_axis_tdata, ... ); // 输出信号
    $dumpfile("test_eth_mac_1g.lxt");
    $dumpvars(0, test_eth_mac_1g);
end
```

它依赖的是早已不再使用的 myhdl 工作流，而当前 cocotb 流程根本不编译这个 `.v`（`VERILOG_SOURCES` 里没有它）。看到这类 `$from_myhdl/$to_myhdl`，就可以判定是遗留文件。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `Makefile`，掌握一个 testbench「编译了哪些文件、以谁为顶层」。

**操作步骤**：

1. 打开 [tb/eth_mac_1g/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile)。
2. 找到 `VERILOG_SOURCES` 段（L32-39）。
3. 把它列出的每个文件对应到 `rtl/` 下的实际路径，并说明为什么 `eth_mac_1g` 需要连带列出 `axis_gmii_rx/tx`、`mac_ctrl_*`、`mac_pause_ctrl_*`、`lfsr`。

**需要观察的现象**：你会看到 8 个 `.v` 文件，全部位于 `../../rtl/`。

**预期结果**：一份「`VERILOG_SOURCES` 文件 → 它在 `eth_mac_1g` 中的角色」对照表，例如：

| 源文件 | 角色 |
| --- | --- |
| `eth_mac_1g.v` | 被测顶层（DUT）本身 |
| `axis_gmii_rx.v` / `axis_gmii_tx.v` | MAC 内部 GMII↔AXI-Stream 互转子模块 |
| `mac_ctrl_rx.v` / `mac_ctrl_tx.v` | MAC 控制帧收发子模块 |
| `mac_pause_ctrl_rx.v` / `mac_pause_ctrl_tx.v` | PAUSE/PFC 流控子模块 |
| `lfsr.v` | FCS/CRC 计算依赖的底层 LFSR |

原因：iverilog 不跨目录自动递归找模块，所以子模块必须显式列入；这也提醒我们，从 `VERILOG_SOURCES` 能反推出一个模块的依赖关系。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Makefile` 里 `TOPLEVEL = $(DUT)` 而不是某个 `test_*` 模块？

> **答案**：cocotb 直接把 DUT（`eth_mac_1g`）实例化为仿真顶层，Python 端通过 `dut.端口名` 访问其端口，因此不需要 Verilog 封装层。这也是顶层遗留 `test_*.v` 被淘汰的原因。

**练习 2**：如果你在 `tb/test_eth_mac_1g.v` 里看到 `$from_myhdl(...)`，能得出什么结论？

> **答案**：这是 myhdl 时代的遗留测试台封装，当前的 cocotb makefile 流和 cocotb-test 流都不编译它（`VERILOG_SOURCES` 不含它）。不要把它当成在用的测试。

**练习 3**：把 `make WAVES=1` 加到 `make` 命令上会发生什么？

> **答案**：`Makefile` 会把自动生成的 `iverilog_dump.v` 加入源文件（L62-63），编译时 dump 信号到 `<TOPLEVEL>.fst` 波形文件，便于用 GTKWave 等工具查看波形（见 L61-64、L77-83）。

---

### 4.3 pytest/tox 运行方式

#### 4.3.1 概念说明

单跑一个模块用 `make` 很方便，但项目有几十个 testbench，需要「一键回归」。verilog-ethernet 用 Python 生态的两件套来组织回归：

- **tox**：在一个隔离的虚拟环境里，**按固定版本**装好全部依赖，再调用 `pytest`。好处是任何人、任何机器跑出来的依赖版本一致，结果可复现。
- **pytest**：负责发现用例、参数化、并行执行、汇总 pass/fail。
- **cocotb-test**：让 `pytest` 能驱动 cocotb 仿真——每个测试函数内部调用 `cocotb_test.simulator.run(...)`，由它去编译 RTL、启动 iverilog、跑 cocotb。

这三者构成「`tox` → `pytest` → `cocotb-test` → cocotb/iverilog」的调用链。

#### 4.3.2 核心流程

```
tox
 └─ 创建虚拟环境，按 tox.ini [testenv].deps 装固定版本依赖
 └─ 执行 commands: pytest -n auto --verbose
     └─ pytest 按 [pytest].testpaths 在 tb/ 和 example/ 下收集 test_*.py
        （--ignore-glob=tb/test_*.py 排除顶层遗留脚本；norecursedirs=lib 跳过第三方 lib）
     └─ 对每个收集到的测试函数（如 test_eth_mac_1g）：
        └─ 函数内调用 cocotb_test.simulator.run(verilog_sources, toplevel, module, parameters, ...)
           └─ 编译 RTL → 启动 iverilog → 加载 cocotb → 跑 module 里的 @cocotb.test()
           └─ 把 cocotb 的 pass/fail 上报给 pytest
```

`-n auto` 来自 `pytest-xdist`，作用是按 CPU 核数并行跑多个 testbench，显著缩短整轮回归时间。

#### 4.3.3 源码精读

先看 `tox.ini` 锁定的依赖版本——这是「可复现环境」的关键：

[tox.ini:L13-L26](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tox.ini#L13-L26) —— `[testenv]` 的依赖与命令：

```ini
[testenv]
deps =
    pytest == 7.2.1
    pytest-xdist == 3.1.0
    pytest-split == 0.8.0
    cocotb == 1.7.2
    cocotb-bus == 0.2.1
    cocotb-test == 0.2.4
    cocotbext-axi == 0.1.20
    cocotbext-eth == 0.1.22
    scapy == 2.5.0
    jinja2 == 3.1.2

commands =
    pytest {posargs:-n auto --verbose}
```

可以看到 4.1 节讲到的 cocotb、cocotbext-axi、cocotbext-eth 都在这里，并且**每个都锁了精确版本**。如果你想本地复现官方回归，最好也装这些版本。

再看 pytest 的搜索与忽略规则：

[tox.ini:L29-L37](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tox.ini#L29-L37) —— pytest 配置：

```ini
[pytest]
testpaths =
    tb
    example
norecursedirs =
    lib
addopts =
    --ignore-glob=tb/test_*.py
    --import-mode importlib
```

三条规则各自的意义：

- `testpaths = tb example`：只在 `tb/`（单元/模块测试）和 `example/`（板级参考设计的测试）里找用例。
- `norecursedirs = lib`：不要进 `lib/`（第三方 vendoring 的 verilog-axis）收集测试。
- `--ignore-glob=tb/test_*.py`：**忽略顶层的遗留 myhdl 脚本**——这就是 4.2.1 说的「旧的那份同名文件被排除」的落点。子目录里的 `tb/<模块>/test_*.py` 不受影响，照常收集。

最后，看一个测试函数如何被 pytest 调用并最终进入 cocotb：

[tb/eth_mac_1g/test_eth_mac_1g.py:L708-L754](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L708-L754) —— 一个普通的 pytest 测试函数，内部调用 `cocotb_test.simulator.run(...)`：

```python
@pytest.mark.parametrize("pfc_en", [1, 0])
def test_eth_mac_1g(request, pfc_en):
    dut = "eth_mac_1g"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = dut
    verilog_sources = [ ... ]          # 与 Makefile 的 VERILOG_SOURCES 一致
    parameters = { 'DATA_WIDTH': 8, ... }
    extra_env = {f'PARAM_{k}': str(v) for k, v in parameters.items()}  # PARAM_ 约定
    sim_build = os.path.join(tests_dir, "sim_build", request.node.name.replace('[','-').replace(']',''))
    cocotb_test.simulator.run(
        python_search=[tests_dir],
        verilog_sources=verilog_sources,
        toplevel=toplevel,
        module=module,
        parameters=parameters,
        sim_build=sim_build,
        extra_env=extra_env,
    )
```

注意两点呼应：

1. 这里的 `verilog_sources`、`toplevel`、`module`、`parameters` 与 4.2 节 `Makefile` 里的内容**一一对应**——同一份测试，两种入口。
2. `extra_env = {f'PARAM_{k}': str(v) ...}` 用的还是 `PARAM_` 前缀，与 `Makefile` 的 `PARAM_*` 约定完全一致。这就是「双用途」能成立的根本：参数注入机制统一。

#### 4.3.4 代码实践

**实践目标**：用 pytest 跑单个模块的测试（比 `tox` 轻，适合日常开发）。

**操作步骤**：

1. 按 4.1.4 装好工具链（含 `cocotb-test`）。
2. 在仓库根目录执行，只跑千兆 MAC：

```bash
pytest tb/eth_mac_1g -v
```

3. 想跑整套回归（含 `example/`），直接：

```bash
tox
```

**需要观察的现象**：pytest 打印收集到的用例（注意 `test_eth_mac_1g[pfc_en]` 这类参数化展开），随后每个用例都会触发一次 iverilog 编译 + cocotb 仿真，最后给出 `passed`/`failed` 统计。

**预期结果**：`tb/eth_mac_1g` 下全部用例 passed。**待本地验证**（本讲撰写环境未安装 iverilog/cocotb，无法代你确认；首次运行会因编译 RTL 而较慢，属正常现象）。

> 小贴士：若只想验证 pytest 的「收集」是否正确而暂不跑仿真，可先执行 `pytest --collect-only tb/eth_mac_1g`，确认它收集到的是子目录里的新测试、而非顶层遗留脚本。

#### 4.3.5 小练习与答案

**练习 1**：`tox.ini` 里 `commands = pytest {posargs:-n auto --verbose}` 中的 `{posargs}` 是什么意思？

> **答案**：`{posargs}` 是 tox 的占位符，会把 `tox` 命令行上 `--` 之后的额外参数透传给 `pytest`。例如 `tox -- -k eth_mac` 会变成 `pytest -n auto --verbose -k eth_mac`，从而只跑名字匹配 `eth_mac` 的用例。如果你不传任何参数，就用默认的 `-n auto --verbose`。

**练习 2**：为什么 `tox.ini` 要写 `--ignore-glob=tb/test_*.py`？删掉它会怎样？

> **答案**：因为顶层 `tb/test_*.py` 是 myhdl 时代的遗留脚本（`from myhdl import *`，见 4.2.3 对 `tb/test_eth_mac_1g.py` 的说明），与子目录里的新 cocotb 同名测试冲突。删掉它，pytest 会同时收集两份同名测试，遗留那份会因缺少 myhdl 等依赖而报错。

**练习 3**：`cocotb-test` 在整条调用链里起什么作用？

> **答案**：它是 pytest 与 cocotb 之间的桥梁。pytest 调用测试函数，函数再调用 `cocotb_test.simulator.run(...)`，由 cocotb-test 负责编译 RTL、启动 iverilog、加载 cocotb 并运行 `@cocotb.test()`，最后把结果回传给 pytest 汇总。

---

## 5. 综合实践

本实践把本讲三块内容串起来：**亲手用 cocotb makefile 跑通一个 testbench，并理解它编译了哪些文件**。这是后续每一篇讲义里「代码实践」的通用入口，务必跑通一次。

**实践目标**：在 `tb/eth_mac_1g` 下用 `make` 完成一次千兆 MAC 的 cocotb 仿真，并解释 `VERILOG_SOURCES`。

**操作步骤**：

1. 按 4.1.4 安装 cocotb、cocotbext-axi、cocotbext-eth、cocotb-test 与 iverilog。
2. 进入测试目录：

```bash
cd tb/eth_mac_1g
```

3. 直接运行（默认 `SIM=icarus`）：

```bash
make
```

4. 想要波形，可改为：

```bash
make WAVES=1
```

5. 跑完用 `make clean` 清理产物。

**需要观察的现象与预期结果**：

- cocotb 会先调用 iverilog 编译 `VERILOG_SOURCES` 列出的 8 个 RTL 文件（见 4.2.3），随后加载 `test_eth_mac_1g.py`、逐个运行其中的 `@cocotb.test()`（注意 `TestFactory` 还会把每个测试展开成多个参数化变体，例如不同 `ifg`、`mii_sel` 组合）。
- 末尾应出现类似 `Results: ... passed ... failed` 的汇总行，全部 passed 即为通过。
- 仿真的时钟周期来自 `TB` 类里的 `Clock(dut.rx_clk, 8, units="ns")`（4.1.3），即 125 MHz，对应千兆速率。

**关键说明**：本讲撰写环境未安装 iverilog 与 cocotb，以上为预期流程，**具体输出待本地验证**。请把你实际看到的一次「仿真通过」输出贴到笔记里，作为基线。

**延伸思考（用本讲知识回答）**：

- 这次 `make` 编译了哪些文件？为什么是这 8 个而不是只有 `eth_mac_1g.v`？（答：iverilog 不跨目录自动找子模块，故 MAC 用到的 `axis_gmii_rx/tx`、`mac_ctrl_*`、`mac_pause_ctrl_*`、`lfsr` 都要显式列出，见 4.2.4。）
- 同样的测试，如果改用 `pytest tb/eth_mac_1g` 跑，走的代码路径有何不同？（答：走的是 `test_eth_mac_1g()` 函数里的 `cocotb_test.simulator.run(...)`，而非 `Makefile`，但编译的源文件和参数一致，见 4.3.3。）

## 6. 本讲小结

- verilog-ethernet 用 **cocotb（Python 测试）+ cocotbext-axi/cocotbext-eth（现成总线驱动）+ Icarus Verilog（仿真器）** 验证全部 RTL，README 的 Testing 段是权威依据。
- `tb/` 下每个被测模块有一个子目录，**当前在用的是 `Makefile` + `test_<模块>.py` 两件**；后者是「双用途」文件，既被 `make` 加载，又被 `pytest` 当入口。
- 顶层的 `tb/test_<模块>.v` 与 `tb/test_<模块>.py` 是 **myhdl 时代的历史遗留**，当前流程不再编译/收集（`tox.ini` 用 `--ignore-glob` 排除顶层 `.py`）。
- `Makefile` 通过 `VERILOG_SOURCES` 显式列出全部待编译 RTL、通过 `PARAM_*` + iverilog 的 `-P` 注入模块参数、最后 `include cocotb-config` 的 `Makefile.sim` 真正起仿真。
- 回归测试走 **tox → pytest(-n auto) → cocotb-test → cocotb/iverilog**；`tox.ini` 锁定了全部依赖的精确版本，保证可复现。
- 想跑单个模块：`make`（子目录）或 `pytest tb/<模块>`；想跑全套：`tox`。

## 7. 下一步学习建议

- **立刻可做**：按本讲第 5 节跑通 `tb/eth_mac_1g` 的 `make`，建立「编译→仿真→pass/fail」的体感；这是之后所有讲义里代码实践的通用入口。
- **学习路线下一站**：进入第 2 单元 **u2-l1（lfsr：通用并行 LFSR/CRC 引擎）**。它是全库 FCS 与各类校验和的底层基础，也是接下来以太网成帧、MAC 等模块测试中频繁出现的依赖（例如本讲 `eth_mac_1g` 就编译了 `lfsr.v`）。
- **想深入了解测试本身**：本讲只讲了「框架与运行」。若想学会**编写与扩展** cocotb testbench（驱动、断言、随机化测试套路），请跳到专家层 **u13（测试方法学）**，那里会剖析 `tb/axis_ep.py`、`tb/eth_ep.py`、`tb/gmii_ep.py` 等端点驱动与 `test_*.py` 的用例组织。
