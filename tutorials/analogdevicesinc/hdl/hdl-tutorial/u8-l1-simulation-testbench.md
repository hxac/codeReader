# 仿真与测试平台

## 1. 本讲目标

本讲带你进入 ADI HDL 仓库的「验证侧」。前面几讲我们一直在读 RTL 与构建脚本，关注的是「综合成比特流」的那条路。但一个 IP 在上板之前，还需要经过**仿真（simulation）**——用测试平台（testbench，简称 tb）驱动它、喂入激励、检查输出是否正确。

学完本讲，你应当能够：

- 看懂 `library/<ip>/tb/` 目录的组织规律：哪些文件是测试本体、哪些是验证模型、哪个脚本负责把仿真跑起来。
- 理解共享基座 `tb_base.v` 如何用「文本包含」而不是「模块例化」的方式，为每个测试平台注入时钟、复位、超时与裁定（SUCCESS/FAILED）逻辑。
- 掌握 `axi_slave` / `axi_read_slave` / `axi_write_slave` 这一组 AXI 总线功能模型（BFM）如何在仿真中扮演外部存储器。
- 区分两类专项测试：`regmap_tb` 验证寄存器面、`dma_read_tb` / `dma_write_tb` 验证数据面——恰好对应 u5-l1 讲过的「axi_dmac = regmap 壳 + transfer 壳」的分层。
- 独立用 Icarus Verilog（或 ModelSim / XSim）跑起一个最小仿真，并读懂它的 SUCCESS/FAILED 判定。

## 2. 前置知识

本讲默认你已经学过：

- **u5-l1（axi_dmac 深入）**：知道 axi_dmac 顶层是「瘦壳」，内部由寄存器面 `axi_dmac_regmap` 与数据面 `axi_dmac_transfer` 组成，数据面有 src/dest 两种可插拔通道（AXI-MM / AXI-Stream / FIFO）。
- **u4-l5（寄存器映射与 up_axi）**：知道 CPU 经 AXI4-Lite 写 `up_*` 寄存器来控制 IP，`*_regmap.v` 按字地址用 `case` 分派读写，并有上电复位值。

下面补充几个本讲要用到的、可能还不熟悉的术语：

- **测试平台（testbench, tb）**：一段只为仿真存在的 Verilog 代码，它产生时钟与激励、例化被测设计（Design Under Test, DUT）、检查 DUT 的输出。它不会被综合成硬件。
- **总线功能模型（Bus Functional Model, BFM）**：一个简化的、行为级的总线伙伴模型。仿真时不需要真的接一片 DDR，只需一个「会按 AXI 协议回应读写的模型」即可。本讲的 `axi_slave.v` 就是这样的 BFM。
- **VCD（Value Change Dump）**：仿真波形的标准文件格式，由 `$dumpfile` / `$dumpvars` 生成，可用 GTKWave 等工具查看。
- **Icarus Verilog（iverilog）**：一个开源的轻量 Verilog 仿真器，是本仓库 tb 的默认仿真器。

> 说明：本讲聚焦的是 **library IP 级别的单元仿真**（每个 IP 自带的 `tb/` 目录、用 Icarus/ModelSim/XSim 直接跑）。仓库里另有一套**工程级整系统仿真**（Lattice Propel + Questasim 的 `make sim`，见 `docs/user_guide/build_hdl.rst` 的 `_lattice_simulation_flow` 小节），那套面向整板、需要块设计与内存初始化文件，不在本讲范围内。

## 3. 本讲源码地图

本讲以 `axi_dmac` 为样本，涉及以下文件：

| 文件 | 作用 |
| --- | --- |
| [library/axi_dmac/tb/tb_base.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v) | 共享测试基座：时钟、复位、超时、`failed` 裁定、VCD 转储。被所有 `*_tb.v` 用 `\`include` 文本包含。 |
| [library/axi_dmac/tb/regmap_tb.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v) | 寄存器面专项测试：验证 `axi_dmac_regmap` 的复位值、读写、中断清除、传输状态机与复位恢复。 |
| [library/axi_dmac/tb/dma_read_tb.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v) | 数据面专项测试：AXI-MM 源 → FIFO 目的的读 DMA，校验搬出的数据模式正确。 |
| [library/axi_dmac/tb/dma_write_tb.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_write_tb.v) | 数据面专项测试：FIFO 源 → AXI-MM 目的的写 DMA，由写从模型自校验落库数据。 |
| [library/axi_dmac/tb/axi_slave.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/axi_slave.v) | AXI 从端 BFM 内核：管理地址握手、随机延迟、突发节拍调度。 |
| [library/axi_dmac/tb/axi_read_slave.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/axi_read_slave.v) / [axi_write_slave.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/axi_write_slave.v) | 在 `axi_slave` 之上分别实现读通道（R）与写通道（W/B）的专用 BFM。 |
| [library/common/tb/run_tb.sh](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/tb/run_tb.sh) | 公共运行脚本：按 `SIMULATOR` 环境变量分派到 Icarus / ModelSim / XSim / Xcelium。 |
| [library/axi_dmac/tb/dma_read_tb](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb)（无扩展名） | 运行器：一个可执行 bash 脚本，手工列出仿真所需的全部源文件后 `source run_tb.sh`。 |

补充：同一目录下还有 `dma_read_shutdown_tb.v`、`dma_write_shutdown_tb.v`、`reset_manager_tb.v` 三个相关测试，本讲会顺带提及。

## 4. 核心概念与源码讲解

### 4.1 tb 目录组织与运行方式

#### 4.1.1 概念说明

仓库里**不是每个 library IP 都有测试**。用 `find library -type d -name tb` 可以看到，只有 4 个模块带 `tb/` 目录：`axi_dmac`、`common`、`jesd204`、`util_pack`。也就是说，仿真是「按需提供」的——越核心、越被广泛复用的 IP（如 axi_dmac 这个全仓 DMA 引擎），测试越完整。

`axi_dmac/tb/` 目录里有两类截然不同的文件：

1. **`*_tb.v`**：测试本体。每个文件是一个顶层测试平台模块（如 `regmap_tb`、`dma_read_tb`），里面例化 DUT、施加激励、做检查。
2. **无扩展名的可执行文件**（如 `dma_read_tb`、`regmap_tb`）：运行器 bash 脚本。注意它和同名 `.v` 文件**只差一个后缀**——脚本 `dma_read_tb` 负责跑测试 `dma_read_tb.v`。

此外还有一组验证模型：`axi_slave.v`、`axi_read_slave.v`、`axi_write_slave.v`，它们是「扮演外部世界的演员」，被多个测试复用。

#### 4.1.2 核心流程

跑一个测试的流程是：

```
用户执行 ./tb/dma_read_tb        # 运行器脚本
        │
        │ 1. 用 SOURCE+=" ..." 逐行拼出全部源文件清单
        │    （DUT 的所有子模块 + 验证模型 + util/common 依赖）
        │ 2. cd 到 tb 目录
        ▼
source ../../common/tb/run_tb.sh  # 公共脚本
        │
        │ 3. 读 NAME（运行器去扩展名的 basename）与 SOURCE
        │ 4. 按 $SIMULATOR 分派：
        ▼
   ┌─ modelsim : vlib/vlog/vsim
   ├─ xsim     : xvlog/xelab/xsim
   ├─ xcelium  : xmvlog/xmelab/xmsim
   └─ 默认(Icarus): iverilog 编译 → 执行 → 产出 VCD
```

关键点：**仿真没有用 Make 自动发现依赖**，而是由运行器脚本**手工、扁平地**列出每一个要编译的 `.v` 文件。这是因为仿真器需要一份完整的扁平文件清单（不像综合有工程文件来管理）。这也意味着：如果给 IP 新增了一个子模块，必须同步更新对应运行器的 `SOURCE` 列表，否则仿真会报「找不到模块」。

#### 4.1.3 源码精读

先看运行器脚本 `dma_read_tb` 如何拼装依赖清单：

[library/axi_dmac/tb/dma_read_tb:3-18](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb#L3-L18) —— 这段先把测试本体 `dma_read_tb.v`、验证模型 `axi_read_slave.v axi_slave.v`、DUT 的整条数据面子模块链（`axi_dmac_transfer.v`、`request_arb.v`、`request_generator.v`、`data_mover` 相关、`src_axi_mm.v` 等）、以及 util/common 依赖（`util_axis_fifo.v`、`sync_bits.v`、`ad_mem_asym.v`）逐行累加进 `SOURCE`，最后切到脚本所在目录并 `source` 公共脚本。

注意 `cd $(dirname $0)` 与 `source ../../common/tb/run_tb.sh` 这两行让脚本**无论从哪里调用都能正确定位**：相对路径以 tb 目录为基准。

再看公共脚本如何分派仿真器：

[library/common/tb/run_tb.sh:12-48](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/tb/run_tb.sh#L12-L48) —— `case "$SIMULATOR"` 四选一。默认分支（Icarus）最简单：

```bash
mkdir -p run vcd
iverilog -o run/run_${NAME} -I.. ${SOURCE} $1 || exit 1
cd vcd
../run/run_${NAME}
```

即：在 `run/` 下编译出一个可执行文件 `run_dma_read_tb`，再到 `vcd/` 下运行它。`-I..` 把上一级目录（IP 根目录）加入 include 搜索路径，这样 `\`include "tb_base.v"` 之类才能找到。`NAME` 由 [run_tb.sh:6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/tb/run_tb.sh#L6) 的 `basename $0` 得到，所以运行器脚本名必须与测试模块名一致。

#### 4.1.4 代码实践

**实践目标**：弄清「敲一条命令到仿真跑起来」之间发生了什么。

**操作步骤**：

1. 打开 `library/axi_dmac/tb/regmap_tb`（运行器）与 `library/axi_dmac/tb/dma_read_tb`（运行器），对比两者的 `SOURCE` 清单。
2. 思考：`regmap_tb` 只列了 `axi_dmac_regmap.v`、`up_axi.v`、`util_axis_fifo.v` 等少数文件，而 `dma_read_tb` 列了十几文件——为什么差距这么大？

**需要观察的现象**：`regmap_tb` 只测寄存器面，DUT 是 `axi_dmac_regmap`，它的依赖树很浅；`dma_read_tb` 测数据面，DUT 是 `axi_dmac_transfer`，要拉进整条 src/dest 通道与仲裁逻辑，依赖树深得多。**SOURCE 清单的长度，直接反映了被测模块的依赖深度。**

**预期结果**：能口头说出「运行器名 == 测试模块名」「SOURCE 是手工依赖清单」「run_tb.sh 按 SIMULATOR 分派」三件事。

> 待本地验证：若你装了 iverilog，可在 `library/axi_dmac/tb/` 下执行 `./regmap_tb`，终端最后应打印一行 `SUCCESS` 或 `FAILED`。

#### 4.1.5 小练习与答案

**练习 1**：如果你给 `axi_dmac_transfer` 新增了一个名为 `axi_dmac_new_stage.v` 的子模块，仿真时要改什么？

> **答案**：必须在 `dma_read_tb` 与 `dma_write_tb` 两个运行器脚本的 `SOURCE` 列表里都加上 `../axi_dmac_new_stage.v`，否则 iverilog 在精化（elaboration）阶段会报模块未定义。

**练习 2**：为什么运行器用 `cd $(dirname $0)` 而不是直接写死绝对路径？

> **答案**：让脚本无论被谁、从哪个目录调用，都能以自身所在目录为基准解析 `../` 相对路径，保证可移植。

---

### 4.2 测试基座 tb_base.v：时钟、复位与裁定

#### 4.2.1 概念说明

每个测试平台都需要三样东西：一个跑起来的时钟、一段上电复位、一个「测试通过与否」的裁定机制。如果每个 `*_tb.v` 都重写一遍这三样，会非常啰嗦。ADI 的做法是把这些公共逻辑写进 `tb_base.v`，然后用 Verilog 的 `\`include` 指令**文本粘贴**进每个测试模块。

注意：这里是 `\`include`，**不是模块例化**。`\`include "tb_base.v"` 会把文件内容原样插进当前模块的 `{ }` 内部，于是 `tb_base.v` 里声明的 `clk`、`reset`、`failed`、`initial` 块都成了**当前测试模块自己的成员**。这是本讲最关键的一个设计技巧。

#### 4.2.2 核心流程

`tb_base.v` 提供的「服务」可以归纳为四项：

```
┌─────────────────────────────────────────────────┐
│  tb_base.v（被文本包含进每个 *_tb 模块）          │
├─────────────────────────────────────────────────┤
│ ① clk          自激振荡，周期 20ns（50MHz）       │
│ ② reset/resetn  上电复位移位寄存器，自动 deassert │
│ ③ failed       全局失败标志，各测试用 |= 置位      │
│ ④ initial      转 VCD → 等 TIMEOUT → 打印裁定     │
│    do_trigger_reset  运行中重新拉复位的任务        │
└─────────────────────────────────────────────────┘
```

裁定逻辑特别值得注意：到超时时刻，**只看 `failed` 这一个标志位**——为 0 打印 `SUCCESS`，非 0 打印 `FAILED`。整个测试体系不依赖 `$error`/`$stop`，而是「数据驱动」地把所有检查汇聚到一个标志位上。

#### 4.2.3 源码精读

**时钟生成**采用一个自激振荡的写法：

[library/axi_dmac/tb/tb_base.v:60](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v#L60) —— `always @(*) #10 clk <= ~clk;`。这里 `@(*)` 对 `clk` 自身敏感：每次 `clk` 翻转都触发本块，10ns 后再翻转一次，于是形成周期 20ns 的稳定方波。这是 Icarus 友好的经典时钟 idiom。

**复位**用一个 4 位移位寄存器实现「上电后自动撤销」：

[library/axi_dmac/tb/tb_base.v:37-L69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v#L37-L69) —— `reset_shift` 初值 `4'b1111`，`reset = reset_shift[3]` 在仿真开始即为 1（复位有效）；每个上升沿把一个 0 移入最高位，4 拍后 `reset_shift[3]` 变 0，复位撤销。`resetn = ~reset`。这样 DUT 一上电就经历了一段确定的复位脉冲，无需测试代码手动拉复位。

**运行中重新复位**靠一个任务：

[library/axi_dmac/tb/tb_base.v:71-L76](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v#L71-L76) —— `do_trigger_reset` 拉高 `trigger_reset` 一拍，使移位寄存器重新载入 `3'b111`，从而在仿真中途再产生一次复位脉冲。`regmap_tb` 正是用它来测试「写脏寄存器后复位能否恢复默认值」。

**裁定与超时**在唯一的 `initial` 块里：

[library/axi_dmac/tb/tb_base.v:44-L58](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v#L44-L58) —— 先 `$dumpfile(VCD_FILE)` / `$dumpvars` 把波形存盘；然后延时 `TIMEOUT`（若测试 `\`define TIMEOUT 1000000` 则用之，否则默认 100000 个时间单位）；最后依 `failed` 打印 `SUCCESS` 或 `FAILED`，并 `$finish` 结束仿真。`failed` 这个标志本身声明在 [tb_base.v:42](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v#L42)。

#### 4.2.4 代码实践

**实践目标**：体会 `\`include` 文本包含与模块例化的区别。

**操作步骤**：

1. 打开 `regmap_tb.v`，看第 38–42 行：

   ```verilog
   module regmap_tb;
     parameter VCD_FILE = {"regmap_tb.vcd"};
     `define TIMEOUT 1000000
     `include "tb_base.v"
   ```

2. 想象 `\`include` 把 `tb_base.v` 全部内容粘贴到这里——于是 `regmap_tb` 模块内部直接拥有了 `clk`、`reset`、`failed`、`do_trigger_reset` 与那个裁定 `initial`。
3. 在 `regmap_tb.v` 里搜索 `failed`，你会看到测试代码直接写 `failed <= 1'b1`——它用的就是 `tb_base.v` 带来的那个标志。

**需要观察的现象**：`failed` 没有跨模块连接，因为 `tb_base.v` 的内容就在 `regmap_tb` 模块体内。

**预期结果**：能解释「为什么 `tb_base.v` 不是 `module tb_base(...)` 而是一堆松散的 reg/initial」——因为它注定要被文本塞进别的模块里。

#### 4.2.5 小练习与答案

**练习 1**：如果某个测试想用更长的超时，怎么做？

> **答案**：在 `\`include "tb_base.v"` **之前**写 `\`define TIMEOUT <数值>`，这样 `tb_base.v` 里 `\`ifdef TIMEOUT` 分支命中，用自定义值；否则用默认 100000。`regmap_tb.v` 正是 `\`define TIMEOUT 1000000`。

**练习 2**：为什么裁定只看 `failed` 一个位，而不让每个检查直接 `$finish`？

> **答案**：单一汇聚标志让测试可以把所有检查跑完、把所有不匹配都暴露在日志里，而不是遇到第一个错误就停。同时也让「成功」有一条统一判据，便于 CI 自动 grep `SUCCESS`/`FAILED`。

---

### 4.3 AXI 仿真模型：axi_slave / axi_read_slave / axi_write_slave

#### 4.3.1 概念说明

数据面测试需要一个「对端」：当 axi_dmac 作为主设备去读写内存时，谁来扮演那块内存？真接一个 DDR 控制器太重，也不必要。ADI 提供了一组**行为级 AXI 从端模型**（BFM），它们不建模存储，只建模「AXI 协议的握手时序 + 可预测的数据模式」，足以验证 DMA 的搬移正确性。

三个文件是分层关系：

```
axi_slave.v          ← 内核：地址握手 + 随机延迟 + 突发节拍调度（与读/写无关）
   ├── axi_read_slave.v   ← 在内核上加 R 通道：按 beat_addr 生成可预测读数据
   └── axi_write_slave.v  ← 在内核上加 W/B 通道：接收写数据并自校验、回 B 响应
```

#### 4.3.2 核心流程

`axi_slave` 内核维护一个深度 16 的请求 FIFO（`req_fifo`），用「接纳度（ACCEPTANCE）」限制在途事务数：

```
主设备发 addr(valid) ──▶ 若 req_fifo 未满则 ready，把 {addr,len, 到期时间戳} 入队
                              │  到期时间戳 = 当前时间 + [MIN_LATENCY, MAX_LATENCY] 随机
                              ▼
                         时间走到「到期」时，开始按 beat 产生 beat_stb
                              │  每拍 beat_addr 累加 DATA_WIDTH/8，末拍 beat_last=1
                              ▼
                         主设备逐拍 beat_ack，末拍后出队，处理下一个请求
```

读模型 `axi_read_slave` 把 `beat_addr[7:0]` 直接拼成 `rdata`（每字节 = 地址低 8 位 + 字节偏移），所以**读任意地址都能算出期望数据**；写模型 `axi_write_slave` 则维护一份相同的期望模式，逐拍比对实际 `wdata`，不一致就置 `failed`。

#### 4.3.3 源码精读

**内核的接纳度与入队**：

[library/axi_dmac/tb/axi_slave.v:72-L89](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/axi_slave.v#L72-L89) —— `ready = req_fifo_level < ACCEPTANCE` 控制在途事务不超上限；`valid && ready` 时把到期时间戳（`timestamp + 随机[MIN,MAX]`）与 `{addr,len}` 一并写入 `req_fifo[req_fifo_wr]`。这正是 AXI 协议「接纳度」概念的直接体现。

**按延迟节拍调度**：

[library/axi_dmac/tb/axi_slave.v:91-L95](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/axi_slave.v#L91-L95) —— `beat_stb` 在「队列非空且当前时间超过队首到期时间」时拉高；`beat_addr` 由首地址按 `DATA_WIDTH/8` 递增；`beat_last` 在节拍计数等于 `len` 时拉高。随机延迟让 DMA 经受真实存储器那样的「不固定回包延迟」压力。

**读模型生成可预测数据**：

[library/axi_dmac/tb/axi_read_slave.v:70-L75](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/axi_read_slave.v#L70-L75) —— 每个 8 位字节 = `beat_addr[7:0] + i/8`。于是「向地址 A 发起长度为 L 的读，第 k 字节的值 = (A + k) & 0xFF」是确定的，测试平台用同样的公式复算即可校验。

**写模型自校验**：

[library/axi_dmac/tb/axi_write_slave.v:127-L142](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/axi_write_slave.v#L127-L142) —— 维护 `data_cmp` 期望值，每个写节拍按 `wstrb` 逐字节比对 `wdata`，不匹配则 `failed <= 1'b1`。注意它把检查职责放在 BFM 内部，测试平台只需读取 `i_write_slave.failed` 汇总即可。

#### 4.3.4 代码实践

**实践目标**：理解读/写 BFM 如何把「数据正确性」分别交给测试平台和 BFM 自己。

**操作步骤**：

1. 在 `dma_read_tb.v` 中找到 `fifo_rd_dout_cmp` 的计算（[dma_read_tb.v:155-L186](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L155-L186)），确认它用的公式与 `axi_read_slave` 的 `beat_addr[7:0] + i/8` 一致——校验发生在**测试平台侧**。
2. 在 `dma_write_tb.v` 中找到 `failed <= failed | i_write_slave.failed | fifo_wr_overflow`（[dma_write_tb.v:180-L188](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_write_tb.v#L180-L188)），确认写通道的校验发生在 **BFM 侧**。

**需要观察的现象**：读 DMA 与写 DMA 的校验位置不对称——读要测试平台自己算期望值，写则由 BFM 代劳。

**预期结果**：能用一句话说出「`axi_read_slave` 只产数据不判对错，`axi_write_slave` 既收数据又判对错」。

> 待本地验证：修改 `axi_read_slave.v` 里 `data[i+:8] <= beat_addr[7:0] + i/8` 的公式（例如改成 `+ 1`），重新跑 `dma_read_tb`，应看到 `FAILED`，因为测试平台的期望公式与 BFM 不再一致。

#### 4.3.5 小练习与答案

**练习 1**：`ACCEPTANCE` 参数（默认 3）调大或调小，分别压测 DMA 的什么能力？

> **答案**：调大允许更多在途 AXI 事务，压测 DMA 处理并发突发的能力；调小（甚至 1）则让回包更「卡顿」，压测 DMA 在低接纳度下的停顿与恢复。

**练习 2**：为什么读数据模式要设计成「与地址相关」的可预测函数，而不是随机数？

> **答案**：可预测模式让测试平台无需保存「我发了什么」就能反算「我该收到什么」，省掉一份参考存储；随机模式则需要 BFM 与测试平台之间同步随机种子，复杂得多。

---

### 4.4 专项测试一：regmap_tb —— 寄存器面验证

#### 4.4.1 概念说明

`regmap_tb` 专门验证 u4-l5 / u5-l1 讲过的寄存器面 `axi_dmac_regmap`。它**不碰数据通路**，只关心：CPU 经 AXI4-Lite 读写寄存器时，复位值、读写、中断清除、传输状态机的「软件可见行为」是否全部正确。

它的核心技巧是维护一份**黄金参考（golden model）**——一个软件数组 `expected_reg_mem[]`，记录「每个寄存器此刻应当是什么值」。每做一步操作，就更新参考；然后遍历全部寄存器，把实际读回值与参考逐个比对。

#### 4.4.2 核心流程

测试主序列（在 `initial` 块里）如下：

```
上电等 resetn ──▶ initialize_expected_reg_mem()       // 种入上电默认值
               ──▶ check_all_registers("Initial")     // 验复位值
   写 scratch  ──▶ 更新参考 ──▶ check_all
   写 IRQ mask ──▶ 更新参考 ──▶ check_all
   配传输寄存器 ──▶ 更新参考 ──▶ check_all("Transfer setup")
   启动传输    ──▶ 更新参考 ──▶ check_all("Transfer submitted")
   接受请求    ──▶ 更新参考 ──▶ check_all("Transfer accepted")
   完成传输    ──▶ 更新参考 ──▶ check_all("Transfer completed")
   清中断      ──▶ 更新参考 ──▶ check_all
   ★ 复位测试：
     do_trigger_reset() ─▶ initialize ─▶ check_all("Reset 1")
     invert_all_registers() ─▶ do_trigger_reset() ─▶ check_all("Reset 2")
```

「复位测试」是亮点：先用 `invert_all_registers` 把每个寄存器写成反值（污染），再 `do_trigger_reset` 重新复位，验证它们能恢复默认——确保复位逻辑覆盖**全部**寄存器，而不只是几个。

#### 4.4.3 源码精读

**AXI4-Lite 主设备写在测试里**：测试平台自己用 `task` 实现 `write_reg` / `read_reg`，扮演 CPU：

[library/axi_dmac/tb/regmap_tb.v:80-L98](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v#L80-L98) —— `write_reg` 同时拉高 `awvalid`/`wvalid` 并送地址数据，循环等到 `awready`/`wready` 分别握手完成。这是最朴素的 AXI-Lite 写时序。

**黄金参考与上电默认值**：

[library/axi_dmac/tb/regmap_tb.v:166-L186](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v#L166-L186) —— 关键默认值：地址 `0x00` 是版本寄存器（`0x00040565`）；地址 `0x0c` 是「魔数寄存器」`0x444d4143`，其 ASCII 正是 `DMAC`（0x44='D', 0x4d='M', 0x41='A', 0x43='C'），用来在软件里识别「这是一个 axi_dmac」；地址 `0x10` 是接口描述寄存器。这些值都与 u4-l5 讲的 regmap 一一对应。

**全量比对**：

[library/axi_dmac/tb/regmap_tb.v:188-L196](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v#L188-L196) —— `check_all_registers` 以 4 字节为步长遍历 `NUM_REGS*4` 字节地址空间，逐个 `read_reg_check`。任何一处不匹配都会经 `read_match` 拉低，进而由 [regmap_tb.v:152-L156](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v#L152-L156) 把 `failed` 置 1。

**DUT 只例化 regmap**：

[library/axi_dmac/tb/regmap_tb.v:361-L425](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v#L361-L425) —— 例化的是 `axi_dmac_regmap`，把 `request_*` / `response_*` 侧接到测试自己驱动的简单激励（如 `request_ready`、`response_eot`），完全不接真实数据通路。这正应了 u5-l1 的分层：寄存器面可以脱离数据面独立验证。

#### 4.4.4 代码实践

**实践目标**：说出 `regmap_tb` 到底验证了哪些行为。

**操作步骤**：

1. 读 [regmap_tb.v:256-L359](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v#L256-L359) 的主序列，给每个 `check_all_registers("XXX")` 标注它前一步操作验证了什么。
2. 重点关注 [regmap_tb.v:336-L346](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/regmap_tb.v#L336-L346) 两段 `write_reg('h84, ...)`：先写 `0x01` 再写 `0x02` 清中断，对应 RW1C（写 1 清零）语义。

**需要观察的现象**：每次「启动传输 → 接受请求 → 完成」都会让一批寄存器（如传输挂起位 `0x408`、状态 `0x428`、已传字节 `0x448`）按状态机推进变化，参考数组也同步更新。

**预期结果**：能归纳出 `regmap_tb` 验证的 5 类行为——① 上电复位值；② 普通读写；③ RW1C 中断清除；④ 传输状态机在寄存器上的反映；⑤ 复位对全部寄存器的恢复能力。

> 待本地验证：跑 `./regmap_tb`，在 `vcd/regmap_tb.vcd` 里观察地址 `0x408`（TRANSFER_PENDING）在「submitted/accepted/completed」三阶段的电平翻转。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `check_all_registers` 要遍历**所有**寄存器，而不是只查被改动的那个？

> **答案**：一次写操作可能影响别的寄存器（例如写某控制位间接改了状态位），全量扫描能捕捉这种「意外的副作用」与未初始化的寄存器漏网。

**练习 2**：`invert_all_registers` 这一招在测什么？

> **答案**：把每个可写寄存器写成当前值的反，制造「最脏」状态，再复位，验证每个寄存器都能回到默认值——防止某个寄存器漏接复位导致「复位后仍残留旧值」。

---

### 4.5 专项测试二：dma_read_tb / dma_write_tb —— 数据面验证

#### 4.5.1 概念说明

这两个测试验证 u5-l1 讲的数据面 `axi_dmac_transfer`。它们在 src/dest 两个通道上各选一种配置：

- `dma_read_tb`：src = **AXI-MM 读**（`DMA_TYPE_SRC=0`），dest = **FIFO**（`DMA_TYPE_DEST=2`）。即「从内存读出，流到 FIFO」——模拟「DMA 把 DDR 里的数据搬给外设」。
- `dma_write_tb`：src = FIFO/Stream，dest = **AXI-MM 写**。即「从 FIFO 收数据，写进内存」——模拟「外设采样被搬进 DDR」。

两者都**持续不断地发请求**，施加重压，并检查搬移的正确性与溢出处理。

#### 4.5.2 核心流程

以 `dma_read_tb` 为例：

```
测试平台持续发 req_valid（长度每次 +4）
        │  DUT = axi_dmac_transfer (MM→FIFO)
        ▼
   axi_read_slave 扮演内存：按 AXI 读协议回 rdata（可预测模式）
        │
        ▼
   DUT 把数据从 R 通道搬进内部 FIFO，再从 dest 侧 fifo_rd_dout 吐出
        │
        ▼
   测试平台读 fifo_rd_dout，与自算期望值 fifo_rd_dout_cmp 比对
        └─ 不匹配 → fifo_rd_dout_mismatch → failed
```

`dma_write_tb` 方向相反：测试往 `fifo_wr_din` 灌数据，DUT 写出到 `axi_write_slave`，由该 BFM 内部自校验。

#### 4.5.3 源码精读

**持续递增长度的请求源**：

[library/axi_dmac/tb/dma_read_tb.v:68-L73](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L68-L73) —— 只要上一笔被接受（`req_ready`），就立刻发下一笔，且长度 `req_length += REQ_LEN_INC`（每次加 4）。这给 DMA 制造了「请求源源不断、且长度递增」的压力。

**DUT 配置为 MM→FIFO**：

[library/axi_dmac/tb/dma_read_tb.v:105-L153](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L105-L153) —— `DMA_TYPE_SRC(0)` 选 AXI-MM 源、`DMA_TYPE_DEST(2)` 选 FIFO 目的；`m_src_axi_*` 接到 `axi_read_slave`，`fifo_rd_*` 是输出侧。注意 `req_dest_address` / `req_src_address` 都由 `TRANSFER_ADDR` 派生，这是 4.3 节可预测数据模式的基准地址。

**读回数据自校验**：

[library/axi_dmac/tb/dma_read_tb.v:155-L190](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L155-L190) —— 测试平台在 `fifo_rd_valid` 时把 `fifo_rd_dout` 与 `fifo_rd_dout_cmp` 比较，不匹配置位 `fifo_rd_dout_mismatch`，最终 `failed <= failed | fifo_rd_dout_mismatch`。

**写测试的失败汇总**：

[library/axi_dmac/tb/dma_write_tb.v:180-L188](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_write_tb.v#L180-L188) —— `failed` 由三路汇聚：测试自身的累计、写从模型的 `i_write_slave.failed`（落库数据错）、以及 `fifo_wr_overflow`（源侧 FIFO 溢出）。三类失败任一发生即判 FAILED。

**相关：shutdown 测试**：同目录的 `dma_read_shutdown_tb.v` 专门验证「DMA 被禁用（`ctrl_enable=0`）后能否干净收尾」——见 [dma_read_shutdown_tb.v:98-L106](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_shutdown_tb.v#L98-L106)：禁用后状态应稳定在 `12'h701`（idle）且从模型请求队列清空。这是对 u5-l1 所述「请求/响应闭环」的关停测试。

#### 4.5.4 代码实践

**实践目标**：分别说出 `dma_read_tb` 与 `dma_write_tb` 验证的行为，并据此描述一个最小仿真如何搭起来。

**操作步骤**：

1. 读 `dma_read_tb.v`，确认它验证的是「**MM→FIFO 读 DMA 在持续高压、长度递增下，搬出的数据模式始终正确**」。
2. 读 `dma_write_tb.v`，确认它验证的是「**FIFO→MM 写 DMA 在随机输入节拍下，落库数据正确且不溢出**」。
3. 把「最小仿真如何搭建」总结成一句：运行器列出 DUT 全部源文件 → `run_tb.sh` 调 iverilog 编译执行 → `tb_base.v` 提供时钟/复位/裁定 → BFM 扮演对端 → 测试平台发激励并比对。

**需要观察的现象**：两个测试都依赖 `tb_base.v` 带来的 `failed`、`clk`、`resetn`；DUT 都是 `axi_dmac_transfer`（不是顶层 `axi_dmac`），印证数据面可单独验证。

**预期结果**：能口头复述「读测试在平台侧校验、写测试在 BFM 侧校验」这一不对称设计，并说清运行链路。

> 待本地验证：若装了 iverilog，在 `tb/` 目录分别执行 `./dma_read_tb` 与 `./dma_write_tb`，期望两者均打印 `SUCCESS`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `dma_read_tb` 把 `TRANSFER_ADDR` 的低位砍掉（`TRANSFER_ADDR[31:$clog2(WIDTH_DEST/8)]`）？

> **答案**：地址必须按「每拍字节数」对齐，砍掉低位就是做对齐——DMA 的地址生成要求起始地址是 beat 大小的整数倍。

**练习 2**：`dma_write_tb` 里 `fifo_wr_rq <= (($random % 4) == 0)` 起什么作用？

> **答案**：以约 1/4 概率随机产生写请求，让源侧 FIFO 的输入呈「随机间隔」而非连续，压测 DMA 对输入节奏抖动的容忍与 FIFO 的溢出处理。

---

## 5. 综合实践

把本讲知识串起来，做一个「**读懂并改写一个测试**」的小任务：

1. **读图**：在 `library/axi_dmac/tb/` 下，画出运行器脚本 `dma_read_tb`、公共脚本 `run_tb.sh`、测试本体 `dma_read_tb.v`、基座 `tb_base.v`、BFM `axi_read_slave.v`/`axi_slave.v`、DUT `axi_dmac_transfer` 之间的调用/包含/例化关系图（用箭头标注「source」「include」「instantiate」）。

2. **追踪裁定链**：从 `fifo_rd_dout` 不匹配开始，追出一条到终端打印 `FAILED` 的完整数据通路——依次经过 `fifo_rd_dout_mismatch`（[dma_read_tb.v:181-L189](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L181-L189)）→ `failed`（来自 [tb_base.v:42](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v#L42)）→ 裁定 `initial`（[tb_base.v:53-L57](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/tb_base.v#L53-L57)）。

3. **改写观察（源码阅读型，不修改仓库源码）**：假设你想给 `dma_read_tb` 增加一个「奇数次传输长度翻倍」的变体，请回答：
   - 应该改哪个文件？（提示：[dma_read_tb.v:68-L73](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L68-L73) 的请求生成逻辑）
   - 期望公式（`fifo_rd_dout_cmp`）要不要同步改？为什么？（提示：`axi_read_slave` 的数据模式只依赖地址，与长度无关，故期望公式**不需要**改，只需关注末拍边界。）

4. **命令清单**：写出在本地用 Icarus 跑 `regmap_tb` 的最小命令序列（或说明直接 `./regmap_tb` 即可），并指出产物 `vcd/regmap_tb.vcd` 如何用 GTKWave 查看。

> 本任务全部为源码阅读与推理，无需也不应修改仓库源码；涉及「运行」的步骤标注「待本地验证」。

## 6. 本讲小结

- 仓库的 IP 级仿真集中在少数核心模块的 `tb/` 目录（全仓仅 `axi_dmac`、`common`、`jesd204`、`util_pack` 四处），**按需提供**。
- 每个测试由三件套组成：**运行器 bash 脚本**（手工列扁平依赖）、**公共 `run_tb.sh`**（按 `SIMULATOR` 分派 Icarus/ModelSim/XSim/Xcelium）、**测试本体 `*_tb.v`**。
- `tb_base.v` 用 **`\`include` 文本包含**（非例化）为每个测试注入时钟、上电复位移位寄存器、`do_trigger_reset` 任务、`failed` 标志与「超时→打印 SUCCESS/FAILED」的裁定。
- AXI 验证模型分两层：`axi_slave` 内核管接纳度与随机延迟节拍调度，`axi_read_slave` 产可预测数据、`axi_write_slave` 自校验落库数据。
- 测试沿 u5-l1 的「regmap 壳 + transfer 壳」分层切开：`regmap_tb` 用黄金参考全量比对验证寄存器面（含复位恢复），`dma_read_tb`/`dma_write_tb` 持续施压验证数据面（含 shutdown 收尾）。
- 整套验证是**数据驱动**的：所有检查汇聚到单一 `failed` 位，由 `tb_base.v` 统一裁定，便于 CI 与波形定位。

## 7. 下一步学习建议

- **横向迁移**：用本讲的方法读 `library/jesd204/tb/` 与 `library/util_pack/tb/`，看 JESD204 链路层与 pack/unpack IP 的测试是否复用了同一套 `tb_base.v` + `run_tb.sh` 模式。
- **纵向深入 reset_manager**：`reset_manager_tb.v` 用三个不同频率时钟（`clk_a/clk_b/clk_c`）验证 `axi_dmac_reset_manager` 的跨时钟域使能/暂停握手，是理解 u5-l3（CDC）的好材料。
- **工程级仿真**：若关心整板仿真，转去读 `docs/user_guide/build_hdl.rst` 的 `_lattice_simulation_flow` 小节，了解 Lattice Propel + Questasim 的 `make sim` / `make sim-cli` 流程与本讲的 IP 级仿真有何不同。
- **贡献一个测试**：参照 `hdl_coding_guidelines.rst` 的 D10 条（测试平台应使用合适 ``timescale``），为你熟悉的某个尚无 `tb/` 的 IP 起草一个最小测试骨架：复制 `axi_dmac/tb` 的运行器 + `tb_base.v` + 一个 `*_tb.v`，先让它打印 `SUCCESS`。
