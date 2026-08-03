# 编写与扩展 testbench

## 1. 本讲目标

学完本讲，你应当能够：

- 独立为 verilog-ethernet 中的任意一个 RTL 模块新建一套可运行的 cocotb 仿真工程；
- 在 `Makefile` 里正确声明 `VERILOG_SOURCES`、`TOPLEVEL`、`MODULE`，并用 `PARAM_` 约定注入模块参数；
- 理解「现代 cocotb 流程中 DUT 自身就是顶层、无需 `.v` 封装」，同时掌握传统 Verilog 封装的写法与适用场景；
- 用 `cocotbext-eth` / `cocotbext-axi` 端点编写「发送—接收—断言」用例，并用 `TestFactory` 把它铺开成回归矩阵。

本讲是上一讲 [u13-l1](u13-l1-cocotb-testbench-arch.md)（cocotb 仿真平台架构）的动手篇：上一讲讲清了「平台长什么样」，本讲授讲「我怎么照着它给一个新模块搭一套」。

## 2. 前置知识

在动手之前，确认你已理解以下概念（均来自前置讲义）：

- **AXI-Stream 握手**（[u1-l3](u1-l3-axi-stream-interface.md)）：`tvalid`/`tready` 同拍为 1 才发生一次传输，`tlast` 划帧尾。端点（endpoint）就是用 Python 把这套握手封装成「喂一帧 / 收一帧」的高层 API。
- **cocotb 协程模型**（[u13-l1](u13-l1-cocotb-testbench-arch.md)）：`await RisingEdge(clk)` 等的是仿真时间而非墙钟时间；测试是 `async` 协程。
- **两代 testbench 之别**（[u1-l4](u1-l4-testbench-and-simulation.md)）：仓库根 `tb/test_*.v` / `tb/test_*.py` 是 myhdl 时代的历史遗留，**当前流程不再编译它们**；真正在用的是子目录 `tb/<模块>/` 下的现代 cocotbext 版本。
- **FCS 即 CRC-32**（[u2-l1](u2-l1-lfsr-crc-engine.md)、[u2-l2](u2-l2-ethernet-fcs.md)）：标准测试串 `"123456789"` 的 CRC-32 为 `0xcbf43926`，Python 的 `zlib.crc32` 与之一致。本讲的实践任务要用到这一点。

本讲不会重复讲解这些内容，直接在此基础上动手。

## 3. 本讲源码地图

本讲以 `eth_mac_1g` 的现代 testbench 为范本，涉及以下文件：

| 文件 | 作用 | 是否当前在用 |
|------|------|--------------|
| `tb/eth_mac_1g/Makefile` | 声明编译什么 RTL、顶层是谁、加载哪个 Python 模块、注入哪些参数 | ✅ 在用 |
| `tb/eth_mac_1g/test_eth_mac_1g.py` | 现代双用途测试：cocotb 协程用例 + pytest/cocotb-test 入口 | ✅ 在用 |
| `tb/test_eth_mac_1g.v` | 传统 Verilog 封装：例化 DUT、声明 reg/wire（含已废弃的 myhdl 钩子） | ❌ 历史遗留，仅作「DUT 例化」范例 |
| `tb/test_eth_mac_1g.py` | 与上同期的 myhdl 版 Python（`from myhdl import *`） | ❌ 历史遗留，不作范本 |
| `tb/eth_ep.py` | `EthFrame.calc_fcs()` 等 FCS / 帧辅助函数，供断言对比 | ✅ 在用 |
| `tox.ini` | 锁定依赖版本、用 pytest 并行跑 `tb/` 与 `example/` | ✅ 在用 |
| `rtl/axis_eth_fcs.v` | 实践任务的目标 DUT：纯 FCS 计算旁路 | ✅ 在用 |

> **关于规格中「三件套」的辨析**：历史上一个 testbench 由「`Makefile` + `test_*.v` + `test_*.py`」三件组成。但当前现代流程里，cocotb 直接驱动 DUT 端口，**DUT 自身就是顶层**，因此 `tb/<模块>/` 下只有 `Makefile` + `test_*.py` 两件。本讲会同时讲清楚「两件（现代、推荐）」与「三件（传统、含 `.v` 封装）」，让你既会用推荐方式，也看得懂老代码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**Makefile 源文件声明**、**DUT 例化**、**Python 用例与断言**。

### 4.1 Makefile 源文件声明

#### 4.1.1 概念说明

`Makefile` 是 testbench 的「总装配单」。它回答三个问题：

1. **编译哪些 RTL？** iverilog 不会像 Vivado/Quartus 那样递归自动找子模块——`eth_mac_1g` 例化了 `axis_gmii_rx`，后者又例化了 `lfsr`，但你必须在 `VERILOG_SOURCES` 里把这条依赖链上的每一个 `.v` 都显式列出来，否则编译报 `unknown module`。
2. **顶层是谁？加载哪个 Python？** 由 `TOPLEVEL` 与 `MODULE` 指定。现代流程里 `TOPLEVEL` 就是 DUT 本身（如 `eth_mac_1g`），`MODULE` 是不含扩展名的 Python 文件名（如 `test_eth_mac_1g`）。
3. **参数怎么传？** 用统一的 `PARAM_<名字>` 环境变量约定，经 `-P` 开关注入到 iverilog。

#### 4.1.2 核心流程

`make` 执行的链路如下：

```text
读取 VERILOG_SOURCES（手工列全 RTL 依赖）
        │
        ▼
设定 TOPLEVEL=DUT、MODULE=test_<DUT>
        │
        ▼
导出 PARAM_* 环境变量（Make 内部展开派生参数）
        │
        ▼
COMPILE_ARGS = 对每个 PARAM_* 生成 -P <TOPLEVEL>.<name>=<value>
        │
        ▼
include cocotb 的 Makefile.sim  →  启动 iverilog 编译 + 加载 cocotb
        │
        ▼
cocotb 把 MODULE.py 作为测试模块，驱动 TOPLEVEL 的端口
```

#### 4.1.3 源码精读

**顶层语言与仿真器选择**——固定用 Verilog，默认 iverilog：

[tb/eth_mac_1g/Makefile:21-27](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L21-L27) 设定 `TOPLEVEL_LANG=verilog`、`SIM ?= icarus`，并定义仿真时间单位/精度 `1ns/1ps`（与 RTL 的 `` `timescale 1ns / 1ps `` 对齐，否则 cocotb 里按 `units="ns"` 算的时间会失真）。

**三要素：DUT、顶层、Python 模块**：

[tb/eth_mac_1g/Makefile:29-31](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L29-L31) 用三个变量把「被测模块名」串起来：`DUT=eth_mac_1g`、`TOPLEVEL=$(DUT)`、`MODULE=test_$(DUT)`。注意 `TOPLEVEL` 直接等于 DUT——**没有 `.v` 封装**，cocotb 直接驱动 `eth_mac_1g` 的端口。

**显式列出全部 RTL**（iverilog 不自动找子模块）：

[tb/eth_mac_1g/Makefile:32-39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L32-L39) 把 `eth_mac_1g` 及其所有被例化的子模块（`axis_gmii_rx/tx`、`mac_ctrl_rx/tx`、`mac_pause_ctrl_rx/tx`、`lfsr`）逐个列出。给新模块写 Makefile 时，你需要先翻一遍该模块的例化关系，把依赖链补全。

**参数注入：`PARAM_` 约定**：

[tb/eth_mac_1g/Makefile:42-54](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L42-L54) 用 `export PARAM_<名字> := <值>` 声明参数。这里有几个要点：

- 派生参数用 Make 的内建函数算，例如第 47 行 `PARAM_PTP_TS_WIDTH := $(if $(filter-out 1,$(PARAM_PTP_TS_FMT_TOD)),64,96)`——`PTP_TS_FMT_TOD=1` 时取 96，否则 64，与 RTL 默认值口径一致。
- `PARAM_` 前缀是 cocotb/iverilog 约定的「参数通道」，下游的 `foreach` 会扫所有 `PARAM_` 开头的变量。

**`-P` 注入与 `Makefile.sim`**：

[tb/eth_mac_1g/Makefile:56-64](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L56-L64) 在 icarus 分支里，用 `$(foreach v,$(filter PARAM_%,$(.VARIABLES)),-P $(TOPLEVEL).$(subst PARAM_,,$(v))=$($(v)))` 把每个 `PARAM_X` 展开成 `-P eth_mac_1g.X=value`，iverilog 据此覆写模块参数。最后由 [tb/eth_mac_1g/Makefile:75](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L75) 的 `include $(shell cocotb-config --makefiles)/Makefile.sim` 接管，启动编译与仿真。`WAVES=1` 时额外编一个 `iverilog_dump.v` dump 波形（第 61-64、77-83 行）。

#### 4.1.4 代码实践

**目标**：掌握「复制并裁剪 Makefile」的套路。

**步骤**：

1. 把 `tb/eth_mac_1g/Makefile` 复制到一个新目录（如 `tb/axis_eth_fcs/`）。
2. 把 `DUT` 改成 `axis_eth_fcs`。
3. 用 `grep -n 'module\s\|(\s*\|^)' rtl/axis_eth_fcs.v` 或直接阅读源码，列出它依赖的子模块（`axis_eth_fcs` 内部例化了 `lfsr`），据此增删 `VERILOG_SOURCES`。
4. 删掉与本模块无关的 `PARAM_`（如 `PTP_TS_*`、`PFC_ENABLE`），只留 `axis_eth_fcs` 真正有的参数（`DATA_WIDTH`、`KEEP_ENABLE`、`KEEP_WIDTH`）。
5. 在该目录执行 `make`。

**预期结果**：iverilog 报告编译通过、cocotb 开始加载 `test_axis_eth_fcs`（此时该 Python 文件尚未写，会报 `ModuleNotFoundError`——这正是下一步要做的事）。

> 若运行结果不确定，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `VERILOG_SOURCES` 里漏掉 `../../rtl/lfsr.v`，`make` 会在哪一步、报什么错？

**答案**：在 iverilog 编译阶段报 `axis_eth_fcs.v: unknown module: lfsr`（或 `eth_crc_8` 等经由 `lfsr` 的下游模块）。这印证了「iverilog 不自动找子模块」，必须手工列全。

**练习 2**：`PARAM_PTP_TS_WIDTH` 在 Makefile 里是如何由 `PTP_TS_FMT_TOD` 派生的？

**答案**：用 `$(if $(filter-out 1,$(PARAM_PTP_TS_FMT_TOD)),64,96)`——`FMT_TOD=1`（ToD 格式）取 96，否则取 64，保证与 RTL 的 `PTP_TS_WIDTH` 默认推导一致。

---

### 4.2 DUT 例化

#### 4.2.1 概念说明

「例化」（instantiation）指在一个上层模块里放一个下层模块的实例，并把信号接到它的端口上。

关键认知（本讲最重要的一点）：

> **现代 cocotb 流程不需要你写 `.v` 封装。** cocotb 通过 VPI 直接读写 DUT 的端口信号，所以把 DUT 本身设为 `TOPLEVEL` 即可，Python 端用 `dut.tx_axis_tdata` 这样的句柄直接驱动/观察端口。

那么什么时候才需要写一个 `.v` 封装？三种典型场景：

1. **需要胶水逻辑**：例如要给 DUT 套一个时钟分频、或把两路 AXI 合并；
2. **多 DUT 互联**：一次仿真要测「A 的输出喂给 B」这种级联；
3. **想观察 DUT 内部非端口的信号**（虽然 cocotb 也能用层次化路径访问，但封装里声明成 `wire` 更直观）。

仓库里的 `tb/test_eth_mac_1g.v` 是「传统手写封装」的完整范例，但它属于已废弃的 myhdl 流程，我们只取它的「例化写法」作教学，**不要照搬它的 `$from_myhdl` 部分**。

#### 4.2.2 核心流程

写一个 Verilog 封装的标准步骤：

```text
1. 声明与 DUT 一致的 parameter（用 localparam 或 parameter）
        │
        ▼
2. 声明输入为 reg（带初值），输出为 wire
        │
        ▼
3. module #(.P1(P1), .P2(P2), ...) 实例名 ( .port(signal), ... );
        │  （命名端口连接，信号名与端口名可不同）
        ▼
4. （现代 cocotb 省略此封装；此处仅供需要胶水逻辑时使用）
```

#### 4.2.3 源码精读

**参数与信号声明**：

[tb/test_eth_mac_1g.v:34-69](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.v#L34-L69) 先用 `parameter` 声明 `DATA_WIDTH`、`ENABLE_PADDING` 等（第 35-45 行），再用 `reg` 声明所有输入并给初值（第 48-69 行，如 `reg clk = 0;`、`reg [DATA_WIDTH-1:0] tx_axis_tdata = 0;`），用 `wire` 声明所有输出（第 72-87 行）。注意 `TX_USER_WIDTH` 等是**由其它参数派生**的局部表达式（第 44-45 行），与 Makefile 里 `PARAM_TX_USER_WIDTH` 的推导口径一致。

**命名端口连接的实例**：

[tb/test_eth_mac_1g.v:138-186](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.v#L138-L186) 是范本的核心：`eth_mac_1g #(.DATA_WIDTH(DATA_WIDTH), ...) UUT (.rx_clk(rx_clk), .tx_axis_tdata(tx_axis_tdata), ...);`。`.端口名(信号名)` 的写法让端口顺序无关，是工程实践的标准做法。实例名 `UUT`（Unit Under Test）是约定俗成。

**已废弃的 myhdl 钩子（仅作辨识，勿仿写）**：

[tb/test_eth_mac_1g.v:89-136](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.v#L89-L136) 里的 `$from_myhdl(...)` / `$to_myhdl(...)` 是 myhdl 时代的信号桥接系统任务，**当前 cocotb 流程根本不编译这个文件**（见 [tox.ini:36](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tox.ini#L36) 的 `--ignore-glob=tb/test_*.py` 与目录布局）。你只需能认出「这是老代码」即可。

#### 4.2.4 代码实践

**目标**：用现代方式（不写 `.v`）直接驱动 DUT，对比理解传统封装的必要性。

**步骤**：

1. 打开 `tb/eth_mac_1g/test_eth_mac_1g.py`，找到 `TB.__init__` 里对 `dut.tx_axis_tdata` 等端口的引用。
2. 确认：**没有任何 `.v` 文件出现在 [tb/eth_mac_1g/Makefile:32-39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L32-L39) 的 `VERILOG_SOURCES` 中**——DUT 直接是顶层。
3. （选做）若要测「`axis_eth_fcs` 的输出再喂给一个 `axis_eth_fcs_check`」这种级联，才需要写一个 `.v` 把两者连起来；此时参照 [tb/test_eth_mac_1g.v:138-186](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_mac_1g.v#L138-L186) 的写法。

**预期结果**：你应能清楚说出「单 DUT 仿真为何不需要 `.v` 封装」，以及「何时该写封装」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tb/eth_mac_1g/Makefile` 的 `VERILOG_SOURCES` 里没有 `test_eth_mac_1g.v`？

**答案**：因为现代 cocotb 流程把 DUT `eth_mac_1g` 直接作为 `TOPLEVEL`，cocotb 通过 VPI 驱动其端口，不需要 Verilog 层的封装模块。`tb/test_eth_mac_1g.v` 是 myhdl 时代的遗留物，已不在编译清单内。

**练习 2**：在 `.v` 封装里，为什么输入声明成 `reg` 而输出声明成 `wire`？

**答案**：封装模块本身要驱动输入（赋初值、或被 testbench 拉高拉低），能被过程语句赋值的必须是 `reg` 类型；输出由 DUT 实例连续驱动，封装侧只读取，所以是 `wire`。

---

### 4.3 Python 用例与断言

#### 4.3.1 概念说明

`tb/<模块>/test_<模块>.py` 是一份**双用途**文件：

1. **作为 cocotb 模块**：被 `make`/仿真器加载时，里面的 `async def run_test_*` 协程就是测试用例；
2. **作为 pytest 入口**：文件末尾的 `def test_<模块>(...)` 调用 `cocotb_test.simulator.run(...)`，让 `pytest`/`tox` 能编译并启动仿真。

这种「双用途」设计是 verilog-ethernet 把单模块仿真（`make`）和全套回归（`tox`）统一到同一份文件的关键。

一份典型测试文件由四块组成：

| 组成 | 作用 |
|------|------|
| `TB` 类 | 封装 DUT：起时钟、创建端点（Source/Sink）、初始化配置信号、`reset()` |
| `run_test_*` 协程 | 一个测试场景：构造帧 → `send` → `recv` → `assert` |
| `TestFactory` | 把 `run_test_*` 的参数做笛卡尔积，自动生成一批子用例 |
| `test_*` + `cocotb_test.simulator.run` | pytest 入口，声明源文件/参数，启动 cocotb-test |

#### 4.3.2 核心流程

一个 `run_test_*` 协程的执行流程：

```text
TB(dut) 构造：起 Clock、建 GmiiSource/AxiStreamSource/Sink、配置信号初值
        │
        ▼
await tb.reset()           # 同步复位几个时钟
        │
        ▼
for length in payload_lengths():    # 遍历「边界扫描」长度列表
        data  = payload_data(length)
        frame = 构造帧（可带 FCS / 前导）
        await tb.<source>.send(frame)
        │
        ▼
rx_frame = await tb.<sink>.recv()   # 收回环回来的帧
        │
        ▼
assert rx_frame.tdata == data       # 数据一致
assert rx_frame.check_fcs()         # FCS 正确
assert <sink>.empty()               # 没有多余帧
```

`TestFactory` 再把 `payload_lengths`、`payload_data`、`enable_gen`、`mii_sel` 等选项做笛卡尔积，每个组合生成一个独立用例，从而用少量代码铺出高覆盖率回归。

#### 4.3.3 源码精读

**导入与时钟**：

[tb/eth_mac_1g/test_eth_mac_1g.py:36-44](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L36-L44) 导入 cocotb 核心（`Clock`、`RisingEdge`、`TestFactory`）与端点库（`cocotbext.eth` 的 `GmiiFrame/GmiiSource/GmiiSink/PtpClockSimTime`、`cocotbext.axi` 的 `AxiStreamBus/AxiStreamSource/AxiStreamSink`）。[tb/eth_mac_1g/test_eth_mac_1g.py:65-66](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L65-L66) 用 `cocotb.start_soon(Clock(dut.rx_clk, 8, units="ns").start())` 起收发两个 8 ns（125 MHz）时钟。

**用 `define_stream` 现场造端点**：

[tb/eth_mac_1g/test_eth_mac_1g.py:47-50](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L47-L50) 为 PTP 时间戳旁带总线（`ts`/`ts_valid` + 可选 `ts_tag`/`ts_ready`）现场生成 `PtpTsSource/Sink/Monitor`。这对应 [u11-l3](u11-l3-ptp-timestamp-tagging.md) 讲的「TX 时间戳走旁带总线」——标准端点库没有现成驱动，故用 `define_stream` 临时造一个。

**端点绑定（核心一行）**：

[tb/eth_mac_1g/test_eth_mac_1g.py:73-74](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L73-L74) 用 `AxiStreamBus.from_prefix(dut, "tx_axis")` 自动归集 `tx_axis_tdata/tvalid/tready/tlast/tuser` 五根信号成一个总线对象，再交给 `AxiStreamSource`/`AxiStreamSink`。**这是驱动 AXI 接口 DUT 的标准写法**——只要端口遵循 `s_axis_*`/`m_axis_*` 前缀命名，一行即可接好。

**配置初值与复位**：

[tb/eth_mac_1g/test_eth_mac_1g.py:93-131](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L93-L131) 用 `dut.cfg_xxx.setimmediatevalue(0)` 把所有配置/使能信号先置 0，避免仿真开始时出现 X 态。[tb/eth_mac_1g/test_eth_mac_1g.py:133-145](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L133-L145) 的 `reset()` 是教科书式的同步复位：拉高 `rst` 两个上升沿再释放，跨两个时钟域（`rx_rst`/`tx_rst`）。

**「发送—接收—断言」范式**：

以 `run_test_rx`（接收方向）为例，[tb/eth_mac_1g/test_eth_mac_1g.py:203-225](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L203-L225)：

```python
test_frame = GmiiFrame.from_payload(test_data, tx_complete=tx_frames.append)
await tb.gmii_source.send(test_frame)        # 从 GMII 侧喂帧
...
rx_frame = await tb.axis_sink.recv()          # 从 AXI 侧收帧
assert rx_frame.tdata == test_data            # 数据透传一致
assert frame_error == 0                       # tuser 坏帧位为 0
```

这里 `GmiiFrame.from_payload` 自动加前导码/SFD 与 FCS（见 [u13-l1](u13-l1-cocotb-testbench-arch.md)），把 MAC 当黑盒测端到端行为。发送方向的 `run_test_tx` 用 [tb/eth_mac_1g/test_eth_mac_1g.py:264-266](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L264-L266) 的 `assert rx_frame.check_fcs()` 直接复用端点库的 FCS 校验，省去手算。

**`TestFactory` 笛卡尔积铺开**：

[tb/eth_mac_1g/test_eth_mac_1g.py:671-697](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L671-L697) 用 `factory.add_option("payload_lengths", [size_list])` 等给每个形参提供候选生成器，再 `factory.generate_tests()` 自动产出全部组合。注意它被 `if cocotb.SIM_NAME:` 包住——**只在真实仿真器里运行**，pytest 做静态收集时不触发。第 691 行还用 `cocotb.top.PFC_ENABLE.value` 读取 RTL 参数，按需跳过 PFC 用例，实现「参数驱动用例选择」。`size_list`（[第 659-660 行](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L659-L660)）刻意覆盖 60–127 字节边界、512、1514 与多帧 60，是典型的「边界扫描」长度集。

**双用途的 pytest 入口**：

[tb/eth_mac_1g/test_eth_mac_1g.py:700-754](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L700-L754) 是与 Makefile 对偶的另一条启动路径。`test_eth_mac_1g` 函数用 `@pytest.mark.parametrize("pfc_en", [1, 0])` 参数化，再调 `cocotb_test.simulator.run(verilog_sources=..., toplevel=..., module=..., parameters=..., extra_env=...)`。注意 [第 741 行](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L741) 的 `extra_env = {f'PARAM_{k}': str(v) ...}`——它把 Python 里算的 `parameters` 转成 `PARAM_` 环境变量，**与 Makefile 的 `PARAM_` 约定完全镜像**，从而两条路径（`make` 与 `pytest`）注入的参数口径一致。

#### 4.3.4 代码实践

**目标**：参照 `run_test_rx`，为 `axis_eth_fcs` 写一个最小用例。

先看清 DUT 端口——`axis_eth_fcs` 是纯 FCS 计算旁路：吃进 AXI 字节流，在 `tlast` 拍从 `output_fcs` 输出 `~crc_state`（即标准 FCS），数据流本身不对外（无 `m_axis`）。端口见 [rtl/axis_eth_fcs.v:44-63](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L44-L63)：输入 `s_axis_*`、输出 `s_axis_tready`/`output_fcs[31:0]`/`output_fcs_valid`。

下面是一份**示例代码**（不在仓库中，供你照抄改造）：

```python
# 示例代码：tb/axis_eth_fcs/test_axis_eth_fcs.py 的最小骨架
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotbext.axi import AxiStreamBus, AxiStreamSource, AxiStreamFrame
import zlib

class TB:
    def __init__(self, dut):
        self.dut = dut
        cocotb.start_soon(Clock(dut.clk, 8, units="ns").start())
        # DUT 的 AXI 输入用 s_axis_ 前缀，一行接好
        self.axis_source = AxiStreamSource(
            AxiStreamBus.from_prefix(dut, "s_axis"), dut.clk, dut.rst)

    async def reset(self):
        dut = self.dut
        dut.rst.setimmediatevalue(0)
        await RisingEdge(dut.clk)
        dut.rst.value = 1
        await RisingEdge(dut.clk); await RisingEdge(dut.clk)
        dut.rst.value = 0
        await RisingEdge(dut.clk)

async def run_test_fcs(dut, length=64):
    tb = TB(dut)
    await tb.reset()
    data = bytes((k & 0xff) for k in range(length))   # 任意字节流
    await tb.axis_source.send(AxiStreamFrame(data))    # 单帧，末拍自动 tlast
    # 等待 output_fcs_valid 拉高（tlast 后一拍）
    for _ in range(1000):
        await RisingEdge(dut.clk)
        if dut.output_fcs_valid.value.integer:
            break
    expected = zlib.crc32(data) & 0xffffffff          # 标准以太网 FCS
    assert dut.output_fcs.value.integer == expected, \
        f"FCS mismatch: got {dut.output_fcs.value.integer:#010x}, want {expected:#010x}"
```

**步骤**：

1. 把上述骨架存为 `tb/axis_eth_fcs/test_axis_eth_fcs.py`。
2. 在文件末尾追加 cocotb 注册（见下一节「综合实践」会补全 `TestFactory` 与 pytest 入口）。
3. `make` 运行。

**需要观察的现象**：`output_fcs_valid` 在每帧 `tlast` 之后恰好拉高一个时钟周期；`output_fcs` 的 32 位值与 Python `zlib.crc32` 完全相等。

**预期结果**：断言通过，说明 RTL 的 CRC-32（`~crc_state`）与 Python 标准库一致。

> 若本地未装好 cocotb/iverilog，运行结果标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `if cocotb.SIM_NAME:` 要包住 `TestFactory` 调用？

**答案**：`TestFactory.generate_tests()` 依赖运行中的仿真器（它要注册协程为用例）。pytest 在静态收集阶段（没有仿真器）也会 import 这个文件，此时 `cocotb.SIM_NAME` 为空，跳过注册，避免 pytest 阶段报错；真正由 cocotb-test 启动仿真器时 `SIM_NAME` 非空，才展开用例。

**练习 2**：`AxiStreamBus.from_prefix(dut, "s_axis")` 会归集哪些信号？

**答案**：归集 `s_axis_tdata`、`s_axis_tvalid`、`s_axis_tready`、`s_axis_tlast`、`s_axis_tuser`（以及宽位宽时的 `s_axis_tkeep`）。前提是 DUT 端口严格遵循 `s_axis_` 前缀命名——这也是全库端口命名的约定。

**练习 3**：`run_test_tx` 里为什么直接 `assert rx_frame.check_fcs()` 而不手算？

**答案**：`cocotbext-eth` 的帧对象内置 `check_fcs()`，按同样的 CRC-32 约定校验，等价于手算 `zlib.crc32`，但更省事且不易错。复用端点库的能力是编写简洁 testbench 的关键。

---

## 5. 综合实践

**任务**：参照 `tb/eth_mac_1g`，为 `axis_eth_fcs` 搭一套完整的现代 testbench（两件套：`Makefile` + `test_axis_eth_fcs.py`），发送若干随机长度帧并断言 FCS 计算正确，最后用 `TestFactory` 把长度参数铺开。

### 步骤 1：建目录与 Makefile

1. 新建 `tb/axis_eth_fcs/` 目录。
2. 复制 `tb/eth_mac_1g/Makefile` 过来，做如下裁剪：
   - `DUT = axis_eth_fcs`；
   - `VERILOG_SOURCES` 只留 `../../rtl/axis_eth_fcs.v` 与它依赖的 `../../rtl/lfsr.v`（`axis_eth_fcs` 内部经 `eth_crc_8` 用到 `lfsr`，确认依赖后保留）；
   - 删去 `PTP_TS_*`、`PFC_ENABLE`、`PAUSE_ENABLE` 等无关 `PARAM_`，只保留：
     ```makefile
     export PARAM_DATA_WIDTH := 8
     ```
   - `WAVES`、`SIM`、`include Makefile.sim` 等保持不变。

### 步骤 2：写 Python 测试

在 `tb/axis_eth_fcs/test_axis_eth_fcs.py` 中，基于 4.3.4 的骨架，补全三块：

- **`run_test_fcs` 协程**：参数化 `payload_lengths` 与 `payload_data`，循环发送多帧，逐帧断言 `output_fcs == zlib.crc32(data)`。
- **`TestFactory` 展开**（仿 [tb/eth_mac_1g/test_eth_mac_1g.py:671-681](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L671-L681)）：
  ```python
  if cocotb.SIM_NAME:
      factory = TestFactory(run_test_fcs)
      factory.add_option("payload_lengths", [lambda: list(range(1, 64)) + [256, 1518]])
      factory.add_option("payload_data", [incrementing_payload])
      factory.generate_tests()
  ```
- **pytest 入口**（仿 [tb/eth_mac_1g/test_eth_mac_1g.py:700-754](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L700-L754)）：写一个 `test_axis_eth_fcs(request)` 函数，调用 `cocotb_test.simulator.run(...)`，`verilog_sources`/`toplevel`/`module`/`parameters` 与 Makefile 对齐，`extra_env` 用 `PARAM_` 镜像。

### 步骤 3：运行与验证

- 单跑：`cd tb/axis_eth_fcs && make`；
- 纳入回归：在仓库根 `pytest tb/axis_eth_fcs`（`tox.ini` 的 `testpaths` 已含 `tb`，会自动发现新的 `test_*` 函数）。

### 需要观察的现象

- 对每个长度，`output_fcs_valid` 在帧尾后一拍拉高；
- `output_fcs` 与 `zlib.crc32(data)` 逐位相等，包括长度为 1、63、256、1518 这些边界；
- `pytest` 输出显示 `TestFactory` 生成的多个子用例全部 PASSED。

### 预期结果

全部断言通过。若某长度断言失败，最常见原因是「源还没把整帧送完就读了 `output_fcs`」——确保用 `output_fcs_valid` 而非固定延时来对齐采样点。

> 完整命令的运行输出「待本地验证」。

---

## 6. 本讲小结

- **现代 cocotb 流程是两件套**：`tb/<模块>/` 下只需 `Makefile` + `test_<模块>.py`，DUT 自身即 `TOPLEVEL`，cocotb 经 VPI 直接驱动端口，无需 `.v` 封装；历史「三件套」中的 `test_*.v`/`test_*.py`（根目录）是 myhdl 遗留，已被 `tox.ini` 排除。
- **`Makefile` 三件事**：用 `VERILOG_SOURCES` 手工列全 RTL 依赖（iverilog 不自动找子模块），用 `TOPLEVEL`/`MODULE` 指定顶层与 Python 模块，用 `PARAM_` 前缀 + `-P` 开关注入参数（派生参数在 Make 内用函数算）。
- **`.v` 封装的写法与时机**：参数块 → `reg` 输入（带初值）→ `wire` 输出 → `module #(.P(v)) UUT (.port(sig));` 命名端口连接；仅当需要胶水逻辑、多 DUT 级联或观测内部信号时才写。
- **Python 测试四块结构**：`TB` 类（时钟 + 端点 + 配置初值 + `reset`）、`run_test_*` 协程（send→recv→assert）、`TestFactory`（笛卡尔积铺开覆盖率）、`test_*`+`cocotb_test.simulator.run`（与 Makefile 镜像的 pytest 入口）。
- **端点是简洁性的关键**：`AxiStreamBus.from_prefix(dut, "s_axis")` 一行接好 AXI，`GmiiFrame.from_payload`/`check_fcs`/`zlib.crc32` 复用现成 CRC-32，避免手算。
- **断言要对齐采样点**：用状态信号（如 `output_fcs_valid`）而非固定延时来决定何时读结果。

## 7. 下一步学习建议

- **横向铺开覆盖率**：把本讲的 `TestFactory` 套路用到你关心的任意模块，参照 `tb/eth_mac_1g/test_eth_mac_1g.py` 的 `cycle_en`、`run_test_tx_underrun`（欠载注入）、`run_test_tx_error`（坏帧注入）扩展异常场景。
- **纵向深入 PTP/10G testbench**：阅读 `tb/ptp_clock/test_ptp_clock.py`、`tb/eth_mac_10g/test_eth_mac_10g.py`，看 `define_stream` 如何为 PTP 时间戳旁带造端点、10G 测试如何处理 64 位 `tkeep` 与 DIC。
- **回归与持续验证**：配合 `tox.ini`，把你新建的 `tb/axis_eth_fcs` 纳入 `tox` 全量回归，确保后续改动不会悄悄破坏 FCS 行为。
- **迁移到 taxi**：本仓库已停止维护、继任者为 taxi（架构与接口高度相似）。掌握本讲的 testbench 套路后，迁移到 taxi 的测试体系几乎零成本，建议在新项目上直接用 taxi。
