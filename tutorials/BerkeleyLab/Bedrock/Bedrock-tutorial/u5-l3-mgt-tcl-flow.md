# TCL 驱动的 MGT（多千兆收发器）配置流程

> 讲义 id：u5-l3 ｜ 阶段：advanced ｜ 依赖：u5-l1（serial_io：8b/10b、GMII 与链路层）
> 参考工程：`projects/comms_top/`（仅兼容 QF2_PRE 硬件）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 Bedrock 用 TCL 驱动 Xilinx Quad MGT（多千兆收发器）配置的**整体动机与三段式结构**：一份「声明式配置脚本」+ 一个「生成引擎」+ 一份「宏驱动的 Verilog 包装器」。
- 读懂入口脚本 `gtx_comms_top.tcl`，能用一句话说清它声明了「Quad 0 的哪几个通道跑什么协议」。
- 逐行解释 `mgt_gen.tcl` 中 `add_gt_protocol` / `add_aux_ip` / `add_gtcommon` 三个过程（procedure）做了什么，以及它如何通过 `verilog_define` 把配置「翻译」成编译期宏。
- 理解 `qgt_wrap.v` 如何用 `Q_REDEFINE` 与 `GTi_PORTS` 等宏，让**同一份 stub 代码被包含 4 次**、每次按 Quad 生成不同的端口表与实例。
- 看懂 `comms_top.v` 如何把以太网-over-fiber 与 ChitChat 两条**速率不同、编码方式不同**的协议装进**同一个 Quad**，并通过 `comms_top_regbank.v` 让 Host 经 Local Bus 控制/观察它们。

## 2. 前置知识

本讲面向已学过 u5-l1（8b/10b、GMII 链路层）的读者。补充几个 Xilinx 高速收发器术语：

- **MGT（Multi-Gigabit Transceiver）/ GTX / GTP**：FPGA 芯片内的高速串行收发器硬核。Bedrock 主要用 7 系列 Kintex-7 上的 **GTX**；Artix 类芯片用更轻量的 **GTP**。它们把并行数据（如 16/20 位）串行化到几 Gbps 的线路上。
- **Quad（四通道组）**：Xilinx 把每 4 个 MGT 通道（GT0~GT3）打包成一个 Quad，共享参考时钟引脚（REFCLK0/REFCLK1）与公共 PLL。
- **PLL：CPLL vs QPLL**：每个通道有一个专属的通道 PLL（**CPLL**），一个 Quad 还有两个公共的 Quad PLL（**QPLL**）。低速链路（< 10 Gbps 量级）通常用 CPLL，本讲的以太网 1.25 GBd 与 ChitChat 2.5 GBd 都用 CPLL。
- **8b/10b 编码**：见 u5-l1。关键在于：8b/10b **既可以在 MGT 硬核内部完成**（打开 `en8b10b`，MGT 自动编解码并给出 `rxcharisk` 指示符），也可以**在 MGT 外部由用户逻辑完成**（关闭 `en8b10b`，把 20 位「已编码」的原始比特直接喂给 MGT）。本讲两条链路恰好各选其一，是理解 `qgt_wrap.v` 端口差异的钥匙。
- **Vivado IP / `gtwizard`**：Xilinx 的图形化向导（Wizard）可以点选参数生成一个 MGT IP 实例，产出大量 `.xci/.v` 文件。Bedrock 的做法是**绕开图形向导**，用 TCL 脚本调用 `create_ip` 批量生成。
- **`verilog_define`**：Vivado 工程的一个属性，等价于给 Verilog 编译传一组 `-D` 宏定义。这是 TCL 与 Verilog 之间「传递配置」的桥梁。
- **TCL（Tool Command Language）**：Vivado 的脚本语言。本讲的 TCL 文件本质是「在被 Vivado 批处理 `source` 时，按顺序调用若干过程」。

一句话总结设计哲学：**Bedrock 把图形向导能做的事，全部用 TCL + Verilog 宏「文本化」，让 MGT 配置可 diff、可 review、可纳入 CI**。

## 3. 本讲源码地图

| 文件 | 角色 | 行数 |
|---|---|---|
| [projects/comms_top/gtx_comms_top.tcl](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/gtx_comms_top.tcl) | **配置入口**：声明 Quad 0 各通道的协议 + 调用 `add_gt_protocol`/`add_aux_ip` | 39 |
| [fpga_family/mgt/mgt_gen.tcl](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl) | **生成引擎**：`add_define`/`gen_ip`/`add_gt_protocol`/`add_aux_ip`/`add_gtcommon` | 142 |
| [fpga_family/mgt/qgt_wrap.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap.v) | **宏驱动 Verilog 包装器**：声明 `q{0,1,2,3}_gt_wrap` 四个模块 | 187 |
| [fpga_family/mgt/qgt_wrap_pack.vh](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_pack.vh) | **宏定义 1**：`GTi_PORTS`（端口表）与 `Q_REDEFINE`（按 Quad 重定义宏） | 119 |
| [fpga_family/mgt/qgt_wrap_stub.vh](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_stub.vh) | **宏定义 2**：模块体（实例化向导生成的 GT、布线） | 168 |
| [fpga_family/mgt/qgtx_pack.vh](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgtx_pack.vh) | **GTX 专用宏**：`GTi_PORT_MAP`（把 `qgt_wrap` 端口连到向导实例） | 88 |
| [fpga_family/mgt/gtx_ethernet.tcl](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/gtx_ethernet.tcl) | 以太网 MGT 参数字典（1.25 GBd、外部 8b/10b、20 位） | 63 |
| [fpga_family/mgt/gtx_chitchat.tcl](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/gtx_chitchat.tcl) | ChitChat MGT 参数字典（2.5 GBd、内部 8b/10b、16 位） | 57 |
| [projects/comms_top/comms_top.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v) | **顶层**：实例化 `q0_gt_wrap` + 以太网桥 + ChitChat 包装 | 472 |
| [projects/comms_top/comms_top_regbank.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v) | Local Bus 寄存器解码（手写，非 newad.py 生成） | 133 |
| [projects/comms_top/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/README.md) | 工程说明与 TCL 流程文档 | 129 |

> 说明：本讲规约里 `mgt_gen.tcl` 路径标注为「待确认」，现已确认实际位于 [fpga_family/mgt/mgt_gen.tcl](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl)。

## 4. 核心概念与源码讲解

本讲按**数据流方向**拆成 5 个最小模块，正好对应「设计者搭建这样一个工程时的工作顺序」：

4.1 写配置入口 `gtx_comms_top.tcl` → 4.2 引擎 `mgt_gen.tcl` 读它 → 4.3 产出宏包装器 `qgt_wrap.v` → 4.4 顶层 `comms_top.v` 实例化它 → 4.5 寄存器银行让 Host 控制。

### 4.1 gtx_comms_top.tcl：MGT 配置的入口（声明「谁跑什么」）

#### 4.1.1 概念说明

`gtx_comms_top.tcl` 是设计者**唯一需要手写**的 MGT 相关文件。它本质是一份「声明书」：用注释画出 Quad 的通道分配图，然后用几行 `add_gt_protocol` / `add_aux_ip` 把这张图落实成对生成引擎的调用。它不写任何 Verilog，只负责「告诉引擎：Quad 0 的 GT0 跑以太网、GT1 跑 ChitChat、外加一个时钟 MMCM 辅助 IP」。

#### 4.1.2 核心流程

```
gtx_comms_top.tcl 的执行流程（被 Vivado 在 project_proc.tcl 里 source）：
  1. 定位 MGT 配置目录：set MGT_CONFIG_DIR "../../fpga_family/mgt"
  2. source 生成引擎：  source $MGT_CONFIG_DIR/mgt_gen.tcl   ← 把过程定义装载进来
  3. 设置公共变量：      set gt_type "GTX"
  4. 为每个通道调用：
       add_gt_protocol GTX  gtx_ethernet.tcl  quad=0 gt=0 en8b10b=0 endrp=0 CPLL   ← 以太网
       add_gt_protocol GTX  gtx_chitchat.tcl  quad=0 gt=1 en8b10b=1 endrp=0 CPLL   ← ChitChat
  5. 加辅助 IP：         add_aux_ip clk_wiz  mgt_eth_clk.tcl  mgt_eth_mmcm          ← 以太网 MMCM
```

#### 4.1.3 源码精读

文件开头的注释就是最直观的「通道分配图」：

[gtx_comms_top.tcl:2-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/gtx_comms_top.tcl#L2-L12)：用注释画出 Quad GTX 0 的分配——GT0=以太网（1.25 Gbps）、GT1=ChitChat（2.5 Gbps）、GT2/GT3 留空，外加一个以太网 MMCM 辅助 IP。**这张注释图就是整个工程的 MGT 蓝图。**

[gtx_comms_top.tcl:14-15](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/gtx_comms_top.tcl#L14-L15)：定位到 `fpga_family/mgt` 目录并 `source mgt_gen.tcl`，这一行把 `add_gt_protocol` 等过程定义装载进当前 TCL 环境。

[gtx_comms_top.tcl:23-28](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/gtx_comms_top.tcl#L23-L28)：配置 Quad 0 / GT0 跑以太网。注意 `en8b10b=0`——以太网的 8b/10b 由 `eth_gtx_bridge` 在 MGT **外部**完成，所以 GT 只当一根「20 位原始管道」。

[gtx_comms_top.tcl:30-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/gtx_comms_top.tcl#L30-L35)：配置 Quad 0 / GT1 跑 ChitChat。注意 `en8b10b=1`——ChitChat 的 8b/10b 在 GT **内部**完成（见 u5-l2），所以 GT 暴露 16 位数据 + charisk 指示符。

[gtx_comms_top.tcl:37-38](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/gtx_comms_top.tcl#L37-L38)：用 `add_aux_ip` 生成一个时钟向导（`clk_wiz`），把以太网 GTX 的 62.5 MHz 输出时钟倍频成 125 MHz GMII 时钟。

> ⚠️ **README 与实际签名的小出入**：[README.md:88](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/README.md#L88) 把过程签名写成 `add_gt_protocol {config_file quad_num gt_num en8b10b pll_type}`（5 个参数），但实际定义（见 4.2.3）是 7 个参数 `{gt_type config_file quad_num gt_num en8b10b endrp pll_type}`，多了首参数 `gt_type` 和第 6 参数 `endrp`（DRP 使能）。本调用里 `gt_type=GTX`、`endrp=0`。以源码为准。

#### 4.1.4 代码实践

1. **实践目标**：把 `gtx_comms_top.tcl` 当成「配置清单」来读，建立「通道 ↔ 协议 ↔ 参数文件」的三元映射。
2. **操作步骤**：打开 `gtx_comms_top.tcl`，对照它的 5 个 `set`/`add_gt_protocol` 块，填写下表：

   | 调用 | quad | gt | en8b10b | pll_type | 参数字典文件 | 含义 |
   |---|---|---|---|---|---|---|
   | 第 1 个 | 0 | 0 | 0 | CPLL | gtx_ethernet.tcl | 以太网 |
   | 第 2 个 | ? | ? | ? | ? | ? | ChitChat |

3. **需要观察的现象**：两个协议共享 `quad=0` 与同一个 `gt_type=GTX`，但 `en8b10b`、速率、参数文件不同。
4. **预期结果**：第 2 行应为 `quad=0, gt=1, en8b10b=1, pll_type=CPLL, gtx_chitchat.tcl`。这正是「同一 Quad 内配置多个不同协议」的体现。
5. **运行结果**：本实践为纯源码阅读，无需运行，结论可直接从文件得到。

#### 4.1.5 小练习与答案

**练习 1**：如果把 ChitChat 从 GT1 挪到 GT2，需要改 `gtx_comms_top.tcl` 的哪一处？还需要同步改哪些文件？
**答**：把第二个 `add_gt_protocol` 的 `gt` 由 1 改为 2；同步要改 `comms_top.v` 里 `q0_gt_wrap` 的端口连接（把 `gt1_*` 系列信号改接到 `gt2_*`），以及 QSFP 物理通道引脚（GT2 对应不同的 `K7_QSFP1_RX/TX` 引脚对）。

**练习 2**：为什么以太网用 `en8b10b=0` 而 ChitChat 用 `en8b10b=1`？
**答**：以太网链路在 `eth_gtx_bridge` 里**自己**做 8b/10b（因为还要复用现成的 PCS/GMII 逻辑），所以 GT 只透传 20 位已编码原始比特；ChitChat 没有这层外部逻辑，直接把 16 位数据 + charisk 交给 GT **内部**编解码。两种选择各自最优，本讲 4.3 会看到它如何改变 `qgt_wrap.v` 的端口表。

### 4.2 mgt_gen.tcl：TCL 驱动的代码生成引擎

#### 4.2.1 概念说明

`mgt_gen.tcl` 是一个**纯过程库**：它自己 `source` 时不做任何事，只定义若干 `proc`，等入口脚本（如 `gtx_comms_top.tcl`）来调用。它解决的核心问题是：**把「声明式配置」翻译成两类产物**——(a) 通过 Vivado API 真正生成 Xilinx IP 实例；(b) 通过 `verilog_define` 往 Verilog 编译环境里塞一组宏，让 `qgt_wrap.v` 据此展开成正确的代码。

#### 4.2.2 核心流程

`add_gt_protocol` 的内部流程（最关键的过程）：

```
add_gt_protocol(gt_type, config_file, quad_num, gt_num, en8b10b, endrp, pll_type):
  1. module_name = "q${quad_num}_gt${gt_num}"          ← 例如 q0_gt0、q0_gt1
  2. config_dict = source $config_file                  ← 读取参数字典（如 gtx_chitchat.tcl）
  3. config_dict[gt0_usesharedlogic] = 0                ← 强制不生成 gt{x,p}_common（共享逻辑）
  4. gen_ip("gtwizard", module_name, config_dict)       ← 调 Vivado API 生成 GT IP 实例
  5. add_define "Q${quad}_GT${gt}_ENABLE"               ← 告诉 qgt_wrap.v：这个通道要启用
  6. if en8b10b: add_define "Q${quad}_GT${gt}_8B10B_EN" ← 启用 8b/10b 相关端口
     if endrp:   add_define "Q${quad}_GT${gt}_DRP_EN"   ← 启用 DRP 端口
  7. 校验并 add_define "GT_TYPE__GTX"(或 GTP)           ← 选 GTX/GTP 宏包
  8. 校验并 add_define "Q${quad}_GT${gt}_CPLL"(或 QPLL) ← 选 PLL 类型宏
```

注意第 5~8 步：TCL 在此**只产生宏名**，真正的「展开」留给 Verilog 预处理器在 `qgt_wrap.v` 里完成。这就是「TCL 负责 IP 生成 + 宏开关，Verilog 宏负责端口/连线代码」的分工。

#### 4.2.3 源码精读

[mgt_gen.tcl:8-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L8-L19)：`add_define` 是 TCL↔Verilog 的桥。它读出当前 Vivado fileset 已有的 `verilog_define` 列表，把新宏 append 进去再写回——等价于逐个累加 `-DXXX`。

[mgt_gen.tcl:21-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L21-L35)：`gen_ip` 是「绕开图形向导」的核心。它依次 `create_ip`（创建 gtwizard 实例）→ `set_property -dict $config_dict`（把参数字典一次性灌进去）→ `generate_target`（生成综合用文件）→ `add_files`（加入工程文件集）。这一段把「点向导」变成了「可版本控制的 TCL 调用」。

[mgt_gen.tcl:47-60](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L47-L60)：`add_gt_protocol` 的真正签名是 `{gt_type config_file quad_num gt_num en8b10b endrp pll_type}`（7 参）。第 49 行把模块名拼成 `q${quad_num}_gt${gt_num}`——这正是 `qgt_wrap_stub.vh` 里 `Q_GT_MODULE(I)` 宏（见 4.3）要展开成的实例名。第 57 行强制 `gt0_usesharedlogic=0`，避免每个通道各生成一份公共逻辑（公共逻辑交给 `add_gtcommon` 统一管理）。

[mgt_gen.tcl:62-79](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L62-L79)：根据 `en8b10b`/`endrp` 决定是否加 `_8B10B_EN`/`_DRP_EN` 宏；再用 `switch` 校验 `gt_type` 只能是 GTX 或 GTP，并加 `GT_TYPE__GTX` 宏。

[mgt_gen.tcl:81-97](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L81-L97)：同理校验 PLL 类型——GTX 只能用 CPLL/QPLL，GTP 只能用 PLL0/PLL1，否则 `exit`。这层校验让错误配置尽早暴露。

[mgt_gen.tcl:37-45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L37-L45) 与 [mgt_gen.tcl:100-142](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L100-L142)：`add_aux_ip` 用同样的 `gen_ip` 生成任意辅助 IP（如时钟向导）；`add_gtcommon` 则把 `usesharedlogic` 强制为 1，生成 Quad 级的公共 QPLL 逻辑并加 `GTCOMMON_ENABLE` 宏（comms_top 未使用，因为两条链路都用 CPLL）。

#### 4.2.4 代码实践

1. **实践目标**：验证「调用 `add_gt_protocol` 两次 → 应产生哪些 `verilog_define`」。
2. **操作步骤**：对照 `gtx_comms_top.tcl` 的两次调用，人工模拟 `add_gt_protocol` 的执行，列出它最终往 `verilog_define` 里塞的全部宏名。
3. **需要观察的现象**：两次调用各自贡献 `Q0_GT0_*` 与 `Q0_GT1_*` 前缀的宏；`GT_TYPE__GTX` 与 PLL 宏是否重复添加（`add_define` 是 append，不去重）。
4. **预期结果**：应得到（顺序大致为）`Q0_GT0_ENABLE`、`Q0_GT0_CPLL`、`Q0_GT1_ENABLE`、`Q0_GT1_8B10B_EN`、`Q0_GT1_CPLL`，以及两次 `GT_TYPE__GTX`。注意只有 GT1（ChitChat）有 `_8B10B_EN`，因为只有它 `en8b10b=1`。
5. **运行结果**：纯源码推演，无需运行。若想真实验证，需在装有 Vivado 的机器上 `vivado -mode batch -source gtx_comms_top.tcl` 后用 `get_property verilog_define [current_fileset]` 查看——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`add_gt_protocol` 为什么要强制 `gt0_usesharedlogic=0`？
**答**：`usesharedlogic=1` 会让**每个**通道实例各自生成一份 Quad 公共逻辑（如 QPLL），导致同一 Quad 内多通道时公共逻辑被重复实例化、引发冲突。强制为 0 后，公共逻辑改由 `add_gtcommon` 单独生成一次（comms_top 用 CPLL 故不需要），通道实例只保留通道私有部分。

**练习 2**：如果误把 `pll_type` 写成 `"QPLL"` 而 `gt_type` 是 `"GTP"`，会发生什么？
**答**：[mgt_gen.tcl:84-87](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L84-L87) 的 `switch` 会打印 "QPLL not supported by GTP" 并 `exit`，让错误在配置阶段就中断，而不是等到综合阶段才报一堆晦涩错误。

### 4.3 qgt_wrap.v：宏驱动的 Verilog 包装器

#### 4.3.1 概念说明

`qgt_wrap.v` 是 TCL 流程的**Verilog 侧产物容器**。它声明了 `q0_gt_wrap`、`q1_gt_wrap`、`q2_gt_wrap`、`q3_gt_wrap` 四个模块（最多覆盖 4 个 Quad），每个模块的端口表和实例体**完全由宏展开决定**——而宏是否激活，正是 4.2 里 `add_gt_protocol` 通过 `verilog_define` 设的那些 `Q0_GT0_ENABLE` 之类。

它的精妙之处在于：**四个 Quad 模块共用同一份 stub 代码**（`qgt_wrap_stub.vh`），靠 `Q_REDEFINE` 宏在进入每个模块前「把 `Q0_GT0_*` 重映射成 `GT0_*`」，于是同一份 stub 被 `include` 4 次却每次生成不同的内容。

#### 4.3.2 核心流程

```
qgt_wrap.v 的结构（非 SIMULATE 分支）：
  include qgt_wrap_pack.vh              ← 定义 GTi_PORTS、Q_REDEFINE 等宏
  对每个 Quad Q ∈ {0,1,2,3}：
    `define QQ ;  `Q_REDEFINE(Q)         ← 把 "Q{Q}_GT{n}_*" 重映射为 "GT{n}_*"
    module q{Q}_gt_wrap(...):
       端口表：对每个 GTi ∈ {0,1,2,3}：`ifdef GTi_ENABLE → `GTi_PORTS(i, GTi_WI)
       `include qgt_wrap_stub.vh          ← 模块体（实例化向导 GT、连时钟缓冲）
    endmodule
    `undef QQ
```

`Q_REDEFINE` 的核心映射关系（以 Quad 0 为例）：

\[ \texttt{Q0\_GT1\_8B10B\_EN} \;\xrightarrow{\texttt{Q\_REDEFINE(0)}}\; \texttt{GT1\_8B10B\_EN} \]

于是 stub 里只需写 `` `ifdef GT1_8B10B_EN ``，而每个 Quad 进入前都会把自己的 `Q{q}_GT1_8B10B_EN` 翻译成统一的 `GT1_8B10B_EN`。**这是用宏实现「参数化 include」的标准技巧**。

#### 4.3.3 源码精读

[qgt_wrap.v:10](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap.v#L10)：先 `include "qgt_wrap_pack.vh"` 装载所有宏定义。

[qgt_wrap.v:14-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap.v#L14-L17)：进入 Quad 0 前的「换挡」——`\`define Q0` 标记当前是 Quad 0，紧接着调用 `\`Q_REDEFINE(0)` 把 `Q0_*` 系列宏翻译成无 Quad 前缀的通用宏。

[qgt_wrap.v:31-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap.v#L31-L42)：`q0_gt_wrap` 的端口表。每个 GTi 通道的端口由 `` `ifdef GTi_ENABLE `` 门控，激活时用 `\`GTi_PORTS(i, GTi_WI)` 宏展开成完整端口列表。未启用的通道（如 comms_top 的 GT2/GT3）整段消失。

[qgt_wrap_pack.vh:6-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_pack.vh#L6-L42)：`GTi_PORTS` 宏展开成一组端口：必有的收发数据/时钟/复位状态；外加两段条件端口——`` `ifdef GT``GTi``_8B10B_EN `` 时多出 `rxcharisk`/`rxchariscomma`/`txcharisk`/`rxdisperr`/`rxnotintable`/`rxbyteisaligned`（[qgt_wrap_pack.vh:24-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_pack.vh#L24-L31)）；`` `ifdef GT``GTi``_DRP_EN `` 时多出 DRP 端口。**这正是 4.1 练习 2 的落点**：以太网 `en8b10b=0` 没有这些端口，ChitChat `en8b10b=1` 才有 `gt1_rxcharisk_out`/`gt1_txcharisk_in`——对应 `comms_top.v` 里 `gt1_txk/gt1_rxk` 信号。

[qgt_wrap_pack.vh:44-118](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_pack.vh#L44-L118)：`Q_REDEFINE` 宏。它先 `\`undef` 一组通用宏（`GT0_ENABLE`...`GT3_DRP_EN` 等），再用一组嵌套 `` `ifdef Q``Qi``_GT{n}_XXX \`define GT{n}_XXX `` 把当前 Quad 的专用宏「投影」成通用宏。这份「先清零再投影」的模式让四个模块互不串扰。

[qgt_wrap.v:46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap.v#L46)：每个模块体都 `include "qgt_wrap_stub.vh"`——**同一份 stub，靠进入前 `Q_REDEFINE` 的不同而展开出不同内容**。

[qgt_wrap_stub.vh:79-104](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_stub.vh#L79-L104)：stub 体里对每个启用的 GTi 先 `\`GTi_WIRES(i)` 声明内部线网，再实例化向导生成的模块 `\`Q_GT_MODULE(i)`（它在 [qgt_wrap_stub.vh:88-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_stub.vh#L88-L92) 里按当前 `Q0/Q1/Q2/Q3` 展开成 `q0_gt0`/`q0_gt1`/...），端口连接由 `\`GTi_PORT_MAP(i)` 完成。

[qgtx_pack.vh:11-81](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgtx_pack.vh#L11-L81)：GTX 专用的 `GTi_PORT_MAP`，把 `qgt_wrap` 的端口（`gt``GTi``_*`）一一连到向导实例的 `gt0_*` 引脚（注意向导侧永远是 `gt0_` 前缀，因为每个 `q${quad}_gt${gt}` 实例内部只有「自己的第 0 个通道」）。

[qgt_wrap.v:163-186](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap.v#L163-186)：`\`else // SIMULATE` 分支——仿真时只声明一个空的 `qgt_wrap` 模块（带 dummy `qgtp_common_wrap`，见 [qgt_wrap_stub.vh:159-167](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap_stub.vh#L159-L167)），因为 iverilog 无法综合 Xilinx 原语。`comms_top.v` 用 `\`ifndef SIMULATE` 在「真实模块名 `q0_gt_wrap`」与「仿真模块名 `qgt_wrap`」间切换（见 4.4.3）。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「宏展开」的效果——把 `qgt_wrap.v` 跑一遍 Verilog 预处理器，观察 `GTi_PORTS` 被替换成真实端口。
2. **操作步骤**：Bedrock 的思路是用 iverilog 的 `-E`（仅预处理）把宏展开后导出。**注意**：[README.md:105](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/README.md#L105) 写的是 `make qgt_template`，但当前 HEAD 中：
   - 唯一存在的模板目标是 [fpga_family/rules.mk:13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/rules.mk#L13) 的 `qgtx_template`；
   - 它引用的 `GTX_DIR=$(FPGA_FAMILY_DIR)/gtx` 与 `qgtx_wrap.v` **在当前仓库中并不存在**（实际文件在 `fpga_family/mgt/` 下、名为 `qgt_wrap.v`）。
   
   因此 `make qgt_template` **按字面无法直接运行**。可行的等价做法是手动用 iverilog 预处理（在仓库根目录）：

   ```bash
   iverilog -E -DQ0 \
     -DQ0_GT0_ENABLE -DQ0_GT0_CPLL \
     -DQ0_GT1_ENABLE -DQ0_GT1_CPLL -DQ0_GT1_8B10B_EN \
     -DGT_TYPE__GTX \
     -Ifpga_family/mgt fpga_family/mgt/qgt_wrap.v \
   | grep '[^ ]' > /tmp/q0_gt_wrap.expanded.v
   ```

3. **需要观察的现象**：展开后的文件里，`q0_gt_wrap` 模块应包含 GT0（以太网，无 charisk 端口）和 GT1（ChitChat，含 `gt1_rxcharisk_out`/`gt1_txcharisk_in`）两组端口；GT2/GT3 整段缺失；模块体里应看到 `q0_gt0 i_gt0 (...)` 与 `q0_gt1 i_gt1 (...)` 两个实例。
4. **预期结果**：端口数量与 `comms_top.v` 里 `i_q0_gt_wrap` 的端口连接完全对应——这就是宏系统的「自洽性」证明。
5. **运行结果**：本仓库环境**未确认是否装有 iverilog**，上述命令的精确输出**待本地验证**；若 `-E` 因 stub 内 Xilinx 原语报错，可只关注 `q0_gt_wrap` 的 `module` 头部端口列表部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `qgt_wrap.v` 要为 4 个 Quad 各写一个 `module`，而不是写一个参数化的单模块？
**答**：因为不同 Quad、不同通道的启用情况、8b/10b、DRP、PLL 配置各异，而 Xilinx 向导生成的是「每个实例一个独立模块名」（`q0_gt0`、`q0_gt1`...）。用宏 + `Q_REDEFINE` 让「同一份 stub 被 include 4 次」兼顾了「每个 Quad 独立的 module 名/实例」与「代码不重复」。

**练习 2**：`GTi_PORTS` 宏里 `[(DWI/8)-1:0] gt``GTi``_rxcharisk_out` 的宽度从哪来？
**答**：宽度 = `DWI/8`，其中 `DWI` 是模块参数 `GTi_WI`（见 [qgt_wrap.v:18-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/qgt_wrap.v#L18-L21)）。ChitChat 的 `GT1_WI=16`，所以 `rxcharisk_out` 宽 16/8=2 位——正好对应 `comms_top.v` 里 `gt1_txk/gt1_rxk` 的 2 位宽度（[comms_top.v:130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L130)）。

### 4.4 comms_top.v：把两条协议装进同一个 Quad

#### 4.4.1 概念说明

`comms_top.v` 是把前面三块积木（配置 → 引擎 → 宏包装）**用起来**的顶层。它做三件事：(1) 把 50 MHz 系统时钟与 125 MHz 参考时钟接好；(2) 实例化 `q0_gt_wrap`，把 Quad 0 的 GT0 接给以太网桥、GT1 接给 ChitChat 包装；(3) 用一个测试图案发生器喂 ChitChat，再用 `comms_top_regbank` 让 Host 经 Local Bus 观察结果。本模块聚焦 (1)(2)，(3) 放到 4.5。

#### 4.4.2 核心流程

```
comms_top.v 的数据通路：
  sys_clk_p/n (50MHz差分) ──ds_clk_buf──> sys_clk
  K7_MGTREFCLK0 (125MHz) ──ds_clk_buf(GTX)──> gtrefclk0
                                              │
            ┌─────────────────────────────────┴──────────────────────────┐
            │                      q0_gt_wrap (i_q0_gt_wrap)              │
            │   GT0 (GTX_ETH_WIDTH=20, 以太网)      GT1 (GTX_CC_WIDTH=16, ChitChat) │
            └────────┬──────────────────────────────────┬─────────────────┘
                     │ gt0_rxd/txd, 62.5MHz             │ gt1_rxd/txd + gt1_rxk/txk
                     ▼                                  ▼
            eth_gtx_bridge ──> Local Bus (lb_*)      chitchat_txrx_wrap
                     │                                  │
                     ▼                                  ▼
              comms_top_regbank  <──统一 Local Bus──>  (Host 经以太网 UDP 访问)
```

两条链路的速率与编码对比（从 TCL 参数字典读出）：

| | 以太网 (GT0) | ChitChat (GT1) |
|---|---|---|
| 线速率 | 1.25 GBd | 2.5 GBd |
| 8b/10b | 外部（`en8b10b=0`） | 内部（`en8b10b=1`） |
| 数据宽度 | 20 位 | 16 位（+2 位 charisk） |
| CPLL `out_div` | 4 | 2 |

线速率与输出分频成反比：在相同 125 MHz 参考与 `fbdiv=4` 下，把 `out_div` 从 4 减半到 2，速率正好翻倍。

\[ \frac{f_{\text{ChitChat}}}{f_{\text{Ethernet}}} = \frac{2.5}{1.25} = \frac{4}{2} = \frac{(\text{out\_div})_{\text{Eth}}}{(\text{out\_div})_{\text{CC}}} \]

#### 4.4.3 源码精读

[comms_top.v:45-46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L45-L46)：固定 IP/MAC 地址。Host 通过这个 IP（192.168.1.173）经以太网访问 FPGA。

[comms_top.v:83-96](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L83-L96)：时钟接入。`ds_clk_buf` 把差分 50 MHz 系统时钟变成单端 `sys_clk`；带 `.GTX(1)` 参数的版本把 MGT 参考时钟差分对变成 `gtrefclk0`。

[comms_top.v:104-118](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L104-L118)：实例化 `mgt_eth_clks`——这就是 4.1 里 `add_aux_ip` 生成的那个时钟向导（参数见 [mgt_eth_clk.tcl:1-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_eth_clk.tcl#L1-L17)）。它把 GTX 输出的 62.5 MHz `txoutclk` 倍频成 125 MHz `gmii_clk`。它出现两份（TX/RX 各一），对应 README 说的「两个时钟管理器」。

[comms_top.v:128-130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L128-L130)：用 `comms_pack.vh` 里的常量声明收发数据线——`GTX_ETH_WIDTH=20`（以太网）、`GTX_CC_WIDTH=16`（ChitChat），charisk 宽度 `GTX_CC_WIDTH/8=2`（见 [comms_pack.vh:6-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_pack.vh#L6-L7)）。

[comms_top.v:141-148](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L141-L148)：实例化 `q0_gt_wrap`。`\`ifndef SIMULATE` 选真实模块 `q0_gt_wrap`，`\`else`（仿真）选空壳 `qgt_wrap`（呼应 4.3 的 SIMULATE 分支）。参数 `GT0_WI=20`、`GT1_WI=16` 直接传给宏展开的端口宽度。

[comms_top.v:155-173](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L155-L173)：GT0（以太网）端口连接——参考时钟、收发时钟、数据 `gt0_rxd/txd`、QSFP 物理引脚、复位/缓冲状态。**没有** charisk 端口（因 `en8b10b=0`）。

[comms_top.v:175-196](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L175-L196)：GT1（ChitChat）端口连接——结构与 GT0 类似，但**多了** `gt1_rxcharisk_out`/`gt1_txcharisk_in`/`gt1_rxbyteisaligned`（因 `en8b10b=1`，正是 4.3 里 `GTi_PORTS` 宏 `8B10B_EN` 段展开出的端口）。注意 GT1 的 `txusrclk_in/txusrclk_in` 直接用 `gt1_tx_out_clk`（自供时钟），而 GT0 用 MMCM 出来的 `gt0_tx_usr_clk`。

[comms_top.v:214-248](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L214-L248)：`eth_gtx_bridge`——把 Packet Badger（见 u4-l4）与 PCS/PMA 逻辑、GTX 接口合在一起。它在 `gmii_tx_clk`（125 MHz）域给出 Local Bus 接口（`lb_valid/lb_rnw/lb_addr/lb_wdata/lb_rdata`），并要求**固定延迟的读响应**（呼应 u4-l2/u4-l3 的存储网关思想）。

[comms_top.v:277-327](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L277-L327)：`chitchat_txrx_wrap`——见 u5-l2。它缝合 `tx_clk`(sys_clk 50 MHz)、`rx_clk`/`lb_clk`(125 MHz)、`gtx_tx_clk`/`gtx_rx_clk` 五个时钟域，这里把 `gtx_tx_d/gtx_tx_k/gtx_rx_d/gtx_rx_k` 接到 GT1 的 `gt1_txd/txk/rxd/rxk`。

[comms_top.v:345-385](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L345-L385)：`generate` 循环例化两个 `patt_gen`——一个发测试图案喂给 ChitChat TX，一个检查 RX（本工程 RX 正确性暂不严格测试，见 [README.md:113](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/README.md#L113)）。

#### 4.4.4 代码实践

1. **实践目标**：建立「Quad 物理通道 ↔ QSFP 引脚 ↔ 协议」的完整对应。
2. **操作步骤**：在 `comms_top.v` 中，跟踪 GT0 与 GT1 各自的 `rxp_in/rxn_in/txp_out/txn_out` 接到哪个 `K7_QSFP1_*` 引脚；再对照顶层声明 [comms_top.v:22-30](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L22-L30)。
3. **需要观察的现象**：GT0 与 GT1 各占 QSFP 的一个通道（lane 0 与 lane 1）。
4. **预期结果**：GT0（以太网）→ `K7_QSFP1_RX0/TX0`；GT1（ChitChat）→ `K7_QSFP1_RX1/TX1`。这与 `gtx_comms_top.tcl` 里 `gt=0`/`gt=1` 一一对应，说明「TCL 里的逻辑通道号」与「顶层物理引脚」是设计者手动对齐的。
5. **运行结果**：纯源码阅读，结论可直接核对。若想仿真，可进入 `projects/comms_top/test/` 跑 `make`（仿真用 `qgt_wrap` 空壳，不需要真实 MGT）——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GT1（ChitChat）的 `txusrclk_in` 可以直接接自己的 `gt1_tx_out_clk`，而 GT0（以太网）要经过 MMCM？
**答**：以太网侧需要严格的 125 MHz GMII 时钟与 62.5 MHz GTX 时钟两个域，且要 90° 相位偏移（见 [mgt_eth_clk.tcl:11-14](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_eth_clk.tcl#L11-L14)），所以必须用 MMCM 综合出多路时钟；ChitChat 侧只需单一的 GTX 恢复时钟驱动数据，对相位无特殊要求，直接回环即可。

**练习 2**：`comms_top.v` 在仿真（`\`define SIMULATE`）与综合时，`i_q0_gt_wrap` 实例的模块名有何不同？为什么？
**答**：仿真时是 `qgt_wrap`（单模块空壳），综合时是 `q0_gt_wrap`（含真实 Xilinx 原语的展开模块）。因为 iverilog 无法综合 GTX 原语，仿真走空壳即可；而综合走 Vivado 时需要 `q0_gt_wrap` 这个被宏完整展开、含向导实例的模块。

### 4.5 comms_top_regbank.v：Local Bus 寄存器解码

#### 4.5.1 概念说明

`comms_top_regbank.v` 是控制平面：Host 通过以太网 → `eth_gtx_bridge` → Local Bus 访问到它，它再把这些读写翻译成对 ChitChat、测试图案发生器、状态信号的观测与控制。注意它与 u2-l3 的区别——**它是手写的解码器，不是 newad.py 自动生成的**。对小规模、定制化的寄存器集，Bedrock 有时直接手写更直观。

#### 4.5.2 核心流程

```
comms_top_regbank 的读路径（固定延迟读，配合无握手 localbus）：
  拍 N  ：lb_valid && lb_rnw 到来 → 把 lb_addr 锁进 lb_addr_r
  拍 N+1：case(lb_addr_r) → 把对应输入信号选进 lb_rdata_reg
  → lb_rdata 在下一拍可用（1 拍寄存读，延迟确定）

写路径：
  拍 N  ：lb_valid && !lb_rnw → case(lb_addr) 直接把 lb_wdata 写进对应输出寄存器
```

#### 4.5.3 源码精读

[comms_top_regbank.v:8-48](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L8-L48)：模块声明。`LB_AWI=24`/`LB_DWI=32` 对应 u2-l2 的 localbus 宽度。输入侧是一堆观测信号（`rx_frame_counter_i` 等），输出侧是控制寄存器（`tx_transmit_en_o`/`pgen_*` 等）。注意 [comms_top_regbank.v:18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L18) 注释「`lb_renable` Ignored in this module」——读靠 `lb_valid && lb_rnw` 触发。

[comms_top_regbank.v:54-83](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L54-L83)：手写的地址映射表。读寄存器从地址 0 开始（`INFO0_RD_REG=0` 起返回 "QF2P"/"COMM" 等），写寄存器单独编号（`TX_LOCATION_WR_REG=0` 起）。其中 [comms_top_regbank.v:73](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L73) 的 `CTR_MEM_OUT_RD_MEM = 'h1????` 是一个用通配符表示的地址区（CTRace 调试内存），用 `casez` 匹配。

[comms_top_regbank.v:87-88](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L87-L88)：读地址先打一拍——`if (lb_valid && lb_rnw) lb_addr_r <= lb_addr;`。这是实现「1 拍寄存读」的关键，让数据在下一拍就绪，延迟确定（呼应 u4-l2 的固定延迟读要求）。

[comms_top_regbank.v:93-114](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L93-L114)：读解码 `case(lb_addr_r)`。例如地址 0 返回 ASCII `"QF2P"`（[L95](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L95)），地址 2 返回 `rx_frame_counter_i`（[L97](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L97)）——让 Host 能读到 ChitChat 收到的帧数。未命中返回 `32'hdeadf00d`（[L113](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L113)），方便调试时一眼识别「读错地址」。

[comms_top_regbank.v:118-128](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L118-L128)：写解码。当 `lb_valid && !lb_rnw` 时，按 `lb_addr` 把 `lb_wdata` 写进对应控制寄存器（如 `TX_TRANSMIT_EN_WR_REG` → `tx_transmit_en_o`），从而让 Host 远程开关 ChitChat 发送、配置测试图案速率等。

[comms_top.v:425-460](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L425-L460)：顶层把 regbank 接在 Local Bus 上——`lb_*` 来自 `eth_gtx_bridge`，观测输入来自 ChitChat/图案发生器，控制输出送回 ChitChat/图案发生器。这条「以太网 UDP → Local Bus → regbank → ChitChat」就是 Host 控制整条 fiber 链路的通路。

#### 4.5.4 代码实践

1. **实践目标**：用 `comms_top_test.py` 的命令文件语法，构造一次「读 INFO0 寄存器确认是 QF2P」的访问。
2. **操作步骤**：阅读 [README.md:118-124](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/README.md#L118-L124) 的命令格式，写一行 `CMP :0 "QF2P"`（比较地址 0 是否等于 ASCII "QF2P"）。注意 Local Bus 地址是字地址，`INFO0_RD_REG=0` 对应 `:0`。
3. **需要观察的现象**：`CMP` 通过表示 Host 经以太网 → `eth_gtx_bridge` → regbank 正确读回了 `INFO0_RD_REG`。
4. **预期结果**：地址 0 读回 32 位值，按字节拼成 ASCII 应为 `Q`(0x51)`F`(0x46)`2`(0x32)`P`(0x50)。`CMP` 命令应判定匹配。
5. **运行结果**：此为**硬件测试**（`make hwtest`），需真实 QF2_PRE 板卡与 fiber 链路，**待本地验证**。无硬件时，可改为源码阅读：确认 [comms_top_regbank.v:95](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top_regbank.v#L95) 的 `"QF2P"` 字符串字面量在 32 位寄存器中的字节序（Verilog 字符串字面量按大端填充）。

#### 4.5.5 小练习与答案

**练习 1**：为什么读路径要把 `lb_addr` 打一拍（`lb_addr_r`），写路径却直接用 `lb_addr`？
**答**：读需要「先确认要读哪个地址、再在下一拍把数据送上总线」，打一拍让 `lb_rdata` 在确定的下一拍就绪，实现固定延迟读（配合 u4-l2 的存储网关）。写则是「地址和数据同一拍有效」，直接当场写进寄存器即可，无需额外对齐。

**练习 2**：这个 regbank 与 u2-l3 的 newad.py 生成器相比，手写有什么取舍？
**答**：手写对小规模、定制寄存器集更直观、可读性好（地址表就在文件顶部），但每次增删寄存器都要手动改地址表与 case 分支、容易出错；newad.py 适合大规模、需软件地址表、需自动解码器的场景。comms_top 寄存器不多且高度定制（混杂了 ASCII 标识、CTRace 内存），故选手写。

## 5. 综合实践：追踪一次「Host 读 ChitChat 帧计数」的全链路

把本讲 5 个模块串起来，画出并解释一次完整的跨域访问。

**任务**：Host 发一个 UDP 包，想读「ChitChat 已收到的帧数」（`RX_FRAME_COUNTER_RD_REG`，地址 2）。请按下列顺序，把每一跳对应的源码位置与涉及的模块写出来：

1. UDP 包到达光纤 → 哪个 GT 接收？（提示：[comms_top.v:155-173](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L155-L173)）
2. GT 把 20 位原始比特交给谁做 8b/10b 解码与以太网解析？（提示：`eth_gtx_bridge`，u4-l4 Packet Badger）
3. 解析出 Local Bus 读请求（`lb_valid=1, lb_rnw=1, lb_addr=2`）→ 进入哪个模块？（提示：[comms_top.v:425-460](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/comms_top.v#L425-L460)）
4. 读地址打一拍 → `case` 命中地址 2 → 返回 `rx_frame_counter_i`。这个值从哪来？（提示：`chitchat_txrx_wrap` 的 `rx_frame_counter` 输出）
5. 读数据沿原路经 `eth_gtx_bridge` 打回 UDP 回包 → 经同一个 GT0 发回 fiber。

**产出**：一张包含「模块名 → 源码行号 → 该跳做了什么」的表格，并标注沿途经过了哪些时钟域（`gmii_tx_clk`、`gtx_tx_clk`、`sys_clk` 等）。这张表就是 comms_top 的「控制平面总览」。

**预期结果**：你能看到一条「UDP → GT0 → eth_gtx_bridge → localbus(gmii_tx_clk) → regbank → chitchat(lb_clk)」的完整往返链路，且读延迟由 regbank 的「1 拍寄存读」与 `eth_gtx_bridge`/存储网关的固定延迟共同决定——这正是 u4-l2 强调的「无握手 localbus 靠固定延迟读出」的真实工程落地。

## 6. 本讲小结

- Bedrock 的 MGT 配置是**三段式**：入口脚本（`gtx_comms_top.tcl`）声明「谁跑什么」→ 引擎（`mgt_gen.tcl`）生成 Xilinx IP + 设 `verilog_define` 宏 → 宏包装器（`qgt_wrap.v`）按宏展开成正确端口与实例。
- `add_gt_protocol` 的真实签名是 7 参 `{gt_type config_file quad_num gt_num en8b10b endrp pll_type}`（README 的 5 参描述略陈旧）；它做两件事——调 `gen_ip` 生成 GT IP、调 `add_define` 开一组 `Q{quad}_GT{gt}_*` 宏。
- `qgt_wrap.v` 的核心技巧是 `Q_REDEFINE` 宏：让同一份 stub（`qgt_wrap_stub.vh`）被 `include` 进 4 个 Quad 模块时，每次把当前 Quad 的专用宏「投影」成通用宏，从而用宏实现「参数化 include」。
- `en8b10b` 是端口差异的总开关：`en8b10b=0`（以太网，外部编码）→ 20 位原始端口；`en8b10b=1`（ChitChat，内部编码）→ 16 位数据 + charisk 端口，由 `GTi_PORTS` 宏的条件段生成。
- comms_top 把两条**速率（1.25/2.5 GBd）、编码方式都不同**的协议装进**同一 Quad 0**，靠 CPLL 输出分频 4→2 实现速率翻倍。
- `comms_top_regbank.v` 是**手写**（非 newad.py）的固定延迟读寄存器银行，让 Host 经以太网 UDP → localbus 控制并观测 ChitChat；未命中地址返回 `0xdeadf00d` 便于调试。

## 7. 下一步学习建议

- **横向扩展 TCL 流程**：阅读 `fpga_family/mgt/` 下的其他 TCL 字典（`gtp_ethernet.tcl`、`gtp_common_*.tcl`）与 [mgt_gen.tcl:100-142](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/mgt/mgt_gen.tcl#L100-L142) 的 `add_gtcommon`，理解 GTP（Artix）与 QPLL 公共逻辑的配置。
- **进入工程集成**：本讲是 u7-l4「工程集成实战」的前奏。学完本讲后，建议直接进入 u7-l4，看 `projects/` 下的完整工程如何把 localbus、Packet Badger、外设、板级支持与本讲的 MGT 流程组装成可上板的设计。
- **复习 CDC**：`chitchat_txrx_wrap` 缝合 5 个时钟域（u5-l2）与本讲 GT 的多时钟输出（`gt*out_clk`、`gmii_*_clk`、`sys_clk`）紧密相关，可结合 u4-l1（CDC 基础）再读一遍 `comms_top.v` 的时钟段。
- **若手头有 QF2_PRE 硬件**：按 [README.md:18-33](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/comms_top/README.md#L18-L33) 走 `make comms_top.bit` → `program_kintex_7` → `make hwtest`，用 `comms_top_test.py` 实际读写 regbank，把本讲的源码理解与真机行为对齐。
