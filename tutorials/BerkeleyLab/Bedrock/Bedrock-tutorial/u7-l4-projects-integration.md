# 工程集成实战：Marble/oscope/trigger_capture

## 1. 本讲目标

本讲是「SoC 软核、外设驱动与平台工程集成」单元的收尾，也是整本手册的"合龙"篇。前面几讲我们分别学过了 localbus（u2-l2）、Packet Badger（u4-l4）、外设驱动（u7-l2）和板级/厂家抽象（u7-l3）。本讲要回答最后一个问题：**这些积木如何被组装成一个能综合、能上板、能被网络访问的真实工程？**

学完后你应当能够：

- 说清 `projects/` 下一个完整 Bedrock 工程的目录与 Makefile 是怎么组织的。
- 画出 Marble 工程的「四层壳」分层（引脚壳 → 时钟与 GMII → CDC 壳 → base 集成），并指出每一层各自实例化了哪些子系统。
- 完整描述一个以太网 UDP 包从网线进入、被 Packet Badger 解析、最终读写到某个 localbus 寄存器、再把读数据封包返回的全过程。
- 理解 `ctrace`/`wctrace` 这类「片上逻辑分析仪」如何用差分压缩捕获波形，以及它如何挂到 localbus 上被网络读回。
- 知道 `projects/common` 这类公共脚本目录的真实定位。

---

## 2. 前置知识

本讲默认你已经读过以下讲义（否则部分术语会跟不上）：

- **u2-l2 localbus**：知道 `lb_clk/lb_addr/lb_strobe/lb_rd/lb_write/lb_data_out/lb_data_in` 这组无握手总线信号。
- **u4-l4 Packet Badger**：知道 `rtefi_blob`（Bedrock 对 Packet Badger 顶层核的叫法）由 `scanner → construct → xformer → client` 流水线组成，`mem_gateway` 是把 localbus 暴露给 UDP 的标准 client。
- **u4-l2 mem_gateway / LASS**：知道「固定延迟读」让无握手的 localbus 能被 UDP 可靠读出。
- **u7-l3 板级/厂家抽象**：知道 `board_support/<板>/` 与 `fpga_family/` 如何把板卡差异、厂家原语从应用 RTL 中剥离。

几个本讲会用到的术语：

| 术语 | 含义 |
|------|------|
| **MMC** | Marble 板上的微控制器（Microcontroller），经 SPI 与 FPGA 通信，负责上电、电源管理、IP 地址下发。 |
| **GMII / RGMII** | 千兆以太网 MAC 与 PHY 之间的接口；GMII 是 8 位并行，RGMII 是 4 位 DDR，二者经 `gmii_to_rgmii` 转换。 |
| **LEEP** | Bedrock 的一套「Live Ethernet Evaluation/访问」约定：每个网络可达的 FPGA 在固定地址（如 `0x4000`）放一个识别 ROM，主机据此发现设备。 |
| **rtefi** | Packet Badger 内部的协议处理核（Real-Time Ethernet Frame Interface 的缩写习惯），`rtefi_blob` 是它的顶层包装。 |
| **p3/p4 端口** | `rtefi_blob` 暴露给 client 的编号通道：p3 是 localbus 通道（`mem_gateway`），p4 是 SPI Flash 通道（`spi_flash`）。 |

---

## 3. 本讲源码地图

本讲涉及的真实源码文件如下（注意：集成的"心脏"并不在 `projects/` 目录里，而在 `board_support/`，这是本讲最重要的一个发现）：

| 文件 | 作用 |
|------|------|
| [projects/test_marble_family/marble_top.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_top.v) | **综合顶层（引脚壳）**：只声明 FPGA 物理引脚，通过 3 个 `include` 把逻辑分发下去。 |
| [projects/test_marble_family/marble_mid.vh](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_mid.vh) | **中间层**：时钟 PLL、GMII↔RGMII 转换、实例化 `marble_base`。 |
| [projects/test_marble_family/marble_base_shell.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_base_shell.v) | **CDC 检查壳**：仅给 `cdc_snitch` 用的薄包装，把所有跨域边界信号寄存一拍并打 `magic_cdc`。 |
| [board_support/marblemini/marble_base.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v) | **集成心脏**：把 localbus、Packet Badger、MMC 邮箱、外设从属、MAC 全部连起来。 |
| [projects/test_marble_family/lb_marble_slave.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/lb_marble_slave.v) | **localbus 从属大杂烩**：I2C 桥、DAC、GPS、`ctrace`、频率计、LED 等地址解码与实例化。 |
| [projects/test_marble_family/marble_features.yaml](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_features.yaml) | **特性开关**：用 YAML 锚点定义 marble / marblemini 两个变体的宏与参数。 |
| [projects/test_marble_family/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile) | **工程构建脚本**：测试、CDC 检查、XDC 生成、Vivado 综合、上板加载全在这。 |
| [projects/ctrace/wctrace.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace.v) | **宽位 ctrace 核心**：参数化位宽的差分压缩波形捕获器。 |
| [projects/ctrace/wctrace_top.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v) | **wctrace 演示顶层**：把 wctrace + `mem_gateway` + 识别 ROM 拼成一个网络可达的"片上示波器"。 |
| [projects/ctrace/config.in](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/config.in) | **信号映射配置**：把 `wctrace` 数据矢量的每个比特对应到真实信号名。 |
| [projects/ctrace/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/README.md) | wctrace 用法与 ctracer.py 实时演示说明。 |
| [projects/common/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/common/README.md) | 公共脚本目录说明（目前仅含一个 banyan 缓冲数据采集助手）。 |

---

## 4. 核心概念与源码讲解

### 4.1 marble_top 顶层集成：四层壳结构

#### 4.1.1 概念说明

一个能上板的 FPGA 工程远不止"一段 RTL"。它至少要解决四件事：(1) 把信号绑到正确的物理引脚上；(2) 产生工作时钟；(3) 处理好跨时钟域；(4) 把应用逻辑（DSP/控制/网络）连成一片。

Bedrock 的 Marble 工程把这四件事拆成**四个层次**，每一层只管一件事，层与层之间靠明确的信号接口对接。这样做的好处是：换板子只动引脚壳和时钟层，应用逻辑（`marble_base`）原封不动——这正是 u7-l3 讲过的「换板不改 RTL」哲学在工程层的落地。

一个关键而容易踩坑的点：**集成的核心文件 `marble_base.v` 并不在 `projects/test_marble_family/` 里**，而在 `board_support/marblemini/` 下。Makefile 里有一句注释直接点破：

> `marble_base.v is over in board_support/marblemini, but most of the infrastructure, including lb_marble_slave.v, is here.`

也就是说：板级基础设施（`marble_base`）跟着板卡走，工程只是"调用"它并加上项目特有的测试与上板脚本。这是 Bedrock 区分「板」与「工程」的核心约定。

#### 4.1.2 核心流程

四层壳的调用关系如下（自顶向下）：

```text
marble_top.v          ── 引脚壳：只声明 FPGA 物理引脚
   └─ include marble_mid.vh     ── 时钟 PLL + gmii_to_rgmii + 实例化 base
         └─ marble_base  (board_support/marblemini/)  ── 真正的集成心脏
               ├─ rtefi_blob        (Packet Badger：网络收发 + client)
               ├─ mmc_mailbox       (SPI 从机，与板上 MMC 通信)
               ├─ lb_marble_slave   (localbus 从属：I2C/DAC/GPS/ctrace/...)
               ├─ base_rx_mac       (以太网接收 MAC + DPRAM)
               └─ freq_demo / LEDs / ltm_sync ...

(并列的) marble_base_shell.v   ── 不参与综合，仅给 cdc_snitch 做 CDC 检查的薄包装
```

注意：综合路径里 `marble_top` 经 `marble_mid.vh` **直接**实例化 `marble_base`；而 `marble_base_shell` 是一条**独立的 CDC 验证支路**，它把 `marble_base` 包了一层、在所有跨域边界上插一拍寄存器并打 `magic_cdc` 属性，专门供 u6-l1 的 `cdc_snitch` 分析用。两者不要混淆。

#### 4.1.3 源码精读

**(a) 引脚壳 `marble_top.v`：几乎只有端口**

[marble_top.v:6-101](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_top.v#L6-L101) 定义了全部物理引脚（GT 参考时钟、RGMII、SPI 到 MMC、boot flash、I2C、FMC、LED、Pmod……），并用 `ifdef MARBLE_V2 / MARBLE_MINI / USE_SI570` 让同一份壳服务多个硬件变体。真正的逻辑只有 3 个 include 与几行 localbus 占位：

```verilog
`include "marble_features_defs.vh"     // 由 marble_features.yaml 生成的宏
// ... 全是端口声明 ...
`include "marble_features_params.vh"   // 参数
wire lb_clk, lb_strobe, lb_rd, lb_write, lb_rd_valid;
wire [23:0] lb_addr;
wire [31:0] lb_data_out, lb_din;
assign lb_din = 32'hfaceface;          // 外部应用读数据的占位
`include "marble_mid.vh"               // 真正的实现
```

[marble_top.v:103-121](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_top.v#L103-L121) 这段说明：`marble_top` 把 localbus 总线**对外暴露**（`lb_din/lb_data_out/...`），给将来插在 `marble_top` 之外的应用逻辑用；当前没有外部应用，于是把读数据 `lb_din` 接成花字 `0xfaceface` 当占位。这是一种"预留扩展点"的常见写法。

**(b) 中间层 `marble_mid.vh`：时钟与 GMII，再调用 base**

[marble_mid.vh:97-118](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_mid.vh#L97-L118) 用 Xilinx 7 系列 MMCM（`xilinx7_clocks`）把 125 MHz 参考时钟倍频/分频出 `tx_clk`（125M）、`tx_clk90`（相移 90°，给 RGMII）、`clk200`（标定 IDELAY）：

```verilog
xilinx7_clocks #(.CLKIN_PERIOD(8), .MULT(8), .DIV0(8) /*=125M*/,
                 `ifdef USE_IDELAYCTRL .DIV1(5) /*=200M*/ `else ... `endif)
  clocks_i(.sysclk_p(clk125), ...);
```

[marble_mid.vh:136-162](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_mid.vh#L136-L162) 调用 `gmii_to_rgmii`（u5-l1 讲过的 8 位 SDR↔4 位 DDR 转换）把 FPGA 内部的 GMII（`vgmii_*`）与物理 RGMII 引脚对接。

[marble_mid.vh:219-255](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_mid.vh#L219-L255) 才是本层主角：实例化 `marble_base`（实例名 `base`），把 GMII 信号、SPI、I2C、FMC、localbus 等通通往下接。注意它把几个特性参数透传下去：

```verilog
marble_base #(.USE_I2CBRIDGE(C_USE_I2CBRIDGE),
              .MMC_CTRACE(C_MMC_CTRACE),
              .GPS_CTRACE(C_GPS_CTRACE), ...) base( ... );
```

这些 `C_*` localparam 来自 [marble_mid.vh:199-215](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_mid.vh#L199-L215)，由宏 `USE_I2CBRIDGE/MMC_CTRACE/GPS_CTRACE` 派生，而宏又来自 YAML——这就是 4.4 节要讲的特性开关链。

**(c) 集成心脏 `marble_base.v`：子系统大集合**

打开 [board_support/marblemini/marble_base.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v)，你会看到它实例化了几乎所有"看得见摸得着"的功能单元。本节先列清单（数据通路留到 4.2 节细讲）：

| 行号 | 实例 | 角色 |
|------|------|------|
| [130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L130) | `mmc_mailbox` | SPI 从机：与板上 MMC 通信，下发 badger 配置（IP/MAC 等） |
| [180](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L180) | `lb_marble_slave` | localbus 从属：I2C/DAC/GPS/ctrace/频率计/LED |
| [240](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L240) | `base_rx_mac` | 以太网接收 MAC（带 DPRAM 缓存） |
| [258](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L258) | `mac_compat_dpram` | 发送 MAC 的 DPRAM 桥（host 写、tx_clk 读） |
| [283](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L283) | `rtefi_blob` | **Packet Badger 顶层**：网络收发 + client 分发 |
| [168](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L168) | `freq_count` | 测 SI570 频率 |
| [332](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L332) | `freq_demo` | 把多路频率经 UART 报出（README 里的"频率彩蛋"） |
| [338-345](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L338-L345) | `packet_categorize` + `data_xdomain` | 收包分类统计，跨域搬到 lb_clk |
| [374](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L374) | `ltm_sync` | LTM4673 电源同步 |

注意 [marble_base.v:1-3](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L1-L3) 的注释强调它 `Needs to be kept 100% portable/synthesizable`——所以它不含任何 Xilinx 原语，厂家相关的东西（PLL、IDELAYCTRL、STARTUPE2）都被推到了 `marble_mid.vh` 与 `marble_top.v` 里。

**(d) 特性开关 YAML**

工程支持 marble 与 marblemini 两个变体，靠 [marble_features.yaml](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_features.yaml) 用 YAML 锚点（`&base_defs`/`*base_defs`）复用公共配置：

```yaml
__base_defs: &base_defs
    USE_I2CBRIDGE: 1
    MMC_CTRACE: 1
    GPS_CTRACE: 0
marblemini:
    defs: { <<: *base_defs, MARBLE_MINI: 1, USE_SI570: 0 }
    params: { carrier: "Marble Mini", sysclk_src: "gt_ref_clk" }
```

由 `gen_features.py`（见 [Makefile:86-88](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L86-L88)）展开成 `marble_features_defs.vh`（宏）与 `marble_features_params.vh`（参数），供上面两层 `include`。`defs` 进 Verilog 宏（`` `ifdef ``），`params` 进 localparam 并写进 LEEP ROM 元数据。

#### 4.1.4 代码实践

> **实践目标**：用静态阅读确认 marble_top 实例化了哪些子系统，体会"四层壳"分工。

1. 操作步骤：
   - 打开 `projects/test_marble_family/marble_top.v`，确认它**没有**任何 `module xxx (` 形式的子系统实例化（只有端口和 include）。
   - 打开 `marble_mid.vh`，用编辑器搜索 `^	[a-z_]+ #` 或肉眼找出所有"实例化"（形如 `模块名 #(...) 实例名(`）。把它们记成一张表：模块名、实例名、所在行。
   - 再到 `board_support/marblemini/marble_base.v` 重复一次，你会看到真正的子系统大集合。
2. 需要观察的现象：`marble_top.v` 里几乎没有可综合逻辑；子系统实例化集中在 `marble_mid.vh`（少数几个：时钟、gmii_to_rgmii、base）与 `marble_base.v`（一大批）。
3. 预期结果：你会得到类似「marble_mid.vh 实例化：xilinx7_clocks、gmii_to_rgmii、marble_base、（可选）tmds_test」与「marble_base.v 实例化：mmc_mailbox、lb_marble_slave、rtefi_blob、base_rx_mac、mac_compat_dpram、freq_demo、…」两张清单。
4. 待本地验证：如果你的编辑器支持"按模块名跳转"，可逐个跳进 `rtefi_blob`/`lb_marble_slave` 看它们的下一层实例，画出至少三层的实例树。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `marble_base.v` 要放在 `board_support/marblemini/` 而不是 `projects/test_marble_family/`？

> **答案**：因为 `marble_base` 是"这块板能干什么"的硬件抽象（网络、MMC、I2C、LED……），属于**板级基础设施**，跟着板卡走；而 `projects/test_marble_family/` 是"在这块板上跑哪个测试/工程"，属于**项目层**。把二者分开，同一块板可以被多个项目复用，换板时也只需换 `board_support/<板>/`。

**练习 2**：`marble_base_shell.v` 在综合路径里被用到了吗？

> **答案**：没有。综合路径是 `marble_top → marble_mid.vh → marble_base`（直接实例化 `marble_base`）。`marble_base_shell` 只在 Makefile 的 CDC 检查目标 `marble_base_yosys.json`/`marble_base_cdc.txt`（[Makefile:144-145](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L144-L145)）里使用，作用是把 `marble_base` 的所有跨域边界信号寄存一拍并打 `magic_cdc`，方便 `cdc_snitch` 判定时钟域。

---

### 4.2 从以太网 UDP 包到 localbus 寄存器的完整数据通路

#### 4.2.1 概念说明

这是本讲的主线，也是实践任务的核心。前面几讲我们分别学了"半条链路"：u4-l4 讲了 Packet Badger 如何把 UDP 包解析成 client 选通；u2-l2 讲了 localbus 如何读写寄存器。本节把这两半**首尾相连**，让你看到一封 UDP 读请求是如何穿透 `marble_base`、抵达某个寄存器、再带着数据原路返回的。

关键在于理解一个设计选择：**Packet Badger 是 localbus 的"主机"（master），而 `lb_marble_slave` 等是从机（slave）**。`marble_base` 的工作就是把 Badger 的 master 端口（`p3_*`）翻译成标准 localbus 信号（`lb_*`），并在地址空间里把不同地址段分发给不同从机。

#### 4.2.2 核心流程

一次"主机经 UDP 读 localbus 寄存器"的往返如下（编号对应下图）：

```text
        主机 PC
          │ UDP 读请求包（LASS 协议，见 u4-l2）
          ▼ ① RGMII 引脚 ── gmii_to_rgmii ──> vgmii_rxd (rx_clk)
   ┌──────────────────────────────────────────────────────────┐
   │ marble_base                                              │
   │   ② rtefi_blob (Packet Badger)                           │
   │        scanner 比对 UDP 字段 → udp_port_cam 命中 mem_gateway│
   │        mem_gateway 解 LASS，发起 localbus 周期：          │
   │            p3_addr / p3_control_strobe / p3_control_rd    │
   │            / p3_control_rd_valid / p3_data_out            │
   │   ③ 翻译 p3_* → lb_*（本节源码 a）                        │
   │        lb_addr = p3_addr; lb_strobe=p3_strobe; ...        │
   │   ④ 地址译码：lb_addr[23:20] 选从机（本节源码 b）         │
   │        0x0_xxxxx → lb_marble_slave（I2C/ctrace/DAC…）     │
   │        0x1_xxxxx → 外部应用 / Tx MAC                      │
   │        0x2_xxxxx → mmc_mailbox                            │
   │   ⑤ 从机返回读数据（如 lb_slave_data_read）               │
   │   ⑥ 读数据经多路选择回喂 mem_gateway 的 p3_data_in        │
   │        mem_gateway 在"固定延迟"后采样，填回回包占位段      │
   │   ⑦ construct 合成回包头 + 算 IP 校验和 → vgmii_txd        │
   └──────────────────────────────────────────────────────────┘
          ▲
          │ ⑧ RGMII Tx ──> 网线 ──> 主机收到与请求等长的回包
```

整条链路里**没有一处握手**：u4-l2 讲过的「固定延迟读」让无握手的 localbus 能被网络可靠读出；这里正是它的工程实例。

#### 4.2.3 源码精读

**(a) p3_\* → lb_\* 翻译**

[marble_base.v:113-120](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L113-L120) 把 Packet Badger 的 master 端口翻译成标准 localbus：

```verilog
wire lb_control_strobe, lb_control_rd, lb_control_rd_valid;  // 来自 rtefi_blob 的 p3_*
assign lb_clk = tx_clk;
assign lb_strobe = lb_control_strobe;
assign lb_write = lb_control_strobe & ~lb_control_rd;   // strobe 且非读 => 写
assign lb_rd_valid = lb_control_rd_valid;
assign lb_rd = lb_control_rd;
```

注意 `lb_write` 是组合派生的：一次 `strobe` 配合 `rd=0` 即写、`rd=1` 即读——这正是 u2-l2 讲的「strobe + 读否」两信号表达读/写。`lb_control_*` 这组线在 [marble_base.v:319-321](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L319-L321) 连到 `rtefi_blob` 的 `p3_*` 端口：

```verilog
.p3_addr(lb_addr), .p3_control_strobe(lb_control_strobe),
.p3_control_rd(lb_control_rd), .p3_control_rd_valid(lb_control_rd_valid),
.p3_data_out(lb_data_out), .p3_data_in(p3_lb_data_in),
```

即 `mem_gateway`（Badger 的 p3 通道 client）发的地址、选通、写数据经 `p3_data_out → lb_data_out` 送出；读数据经 `p3_lb_data_in → p3_data_in` 回收。

**(b) 地址译码：高位 4 bit 选从机**

[marble_base.v:213-221](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L213-L221) 是整个数据通路的"十字路口"——一个读数据多路选择器，按 `lb_addr[23:20]`（地址最高 4 位，即 16 个 1 MiB 的段）选从机：

```verilog
reg [23:0] p3_lb_addr_d;
reg p3_use_app_rd=0, p3_use_mbox_rd=0;
always @(posedge lb_clk) begin
    p3_lb_addr_d <= lb_addr;
    p3_use_app_rd <= p3_lb_addr_d[23:20] == 1;   // 0x1_xxxxx
    p3_use_mbox_rd <= p3_lb_addr_d[23:20] == 2;  // 0x2_xxxxx
end
wire [31:0] p3_lb_data_in = p3_use_mbox_rd ? mbox_out2
                       : p3_use_app_rd ? lb_data_in
                                       : lb_slave_data_read;
```

解读：

- `0x0_xxxxx`（最高位段为 0）：走 `lb_slave_data_read`，即 `lb_marble_slave`（[第 180 行实例](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L180)）。绝大多数寄存器（I2C、DAC、ctrace、频率计、LED 模式……）都在这里。
- `0x1_xxxxx`：走 `lb_data_in`，即 `marble_top` 暴露给外部应用的读端口（当前接 `0xfaceface` 占位）；同时这一段也是 Tx MAC 主机的写入区（见 [229-231 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L229-L231)）。
- `0x2_xxxxx`：走 `mbox_out2`，即 `mmc_mailbox`——给板上微控制器留的邮箱。

注意这里用 `p3_lb_addr_d`（地址打了一拍）来做选择，是为了让读数据时序对齐：localbus 读在 `lb_strobe` 后由从机组合/时序产出，打一拍再选通能让 `mem_gateway` 的固定延迟采样正好对上。

**(c) 收发两端：rx_mac 与 rtefi_blob 的连接**

物理接收侧：`vgmii_rxd`（rx_clk 域）直接进 `rtefi_blob`，[marble_base.v:284-285](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L284-L285)；同时一份原始字节经 `base_rx_mac` 落到 DPRAM 供主机回看（[240-249 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L240-L249)）。发送侧：`rtefi_blob` 产出 `vgmii_txd/tx_en`（[286-287 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L286-L287)），由 `marble_mid.vh` 的 `gmii_to_rgmii` 转成 RGMII 引脚信号送出 PHY。

于是一个完整的"UDP 读 `lb_marble_slave` 内某寄存器"的包，就在 `marble_base` 内部走完了 ②→③→④(0x0 段)→⑤→⑥→⑦ 的全程。

#### 4.2.4 代码实践

> **实践目标**：在不一定有硬件的情况下，用仿真核实这条数据通路。

1. 操作步骤：
   - 阅读 [Makefile:167-176](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L167-L176) 的 `Vmarble_base` 目标——它用 Verilator 把 `marble_base` 编成一个可挂到（模拟）网络的模型，IP 为 192.168.7.4。
   - 若本地装了 Verilator，运行 `make Vmarble_base read_trx.dat && ./Vmarble_base +trace`。
   - 另开终端按 Makefile 注释里的 Recipe 操作：`ping 192.168.7.4`，再 `python3 lbus_access.py -a 192.168.7.4 -t 3 mem 2097200:8`（读 8 个字）、`python3 lbus_access.py -a 192.168.7.4 -t 3 reg 327686=1`（写一个寄存器）。
2. 需要观察的现象：地址 `2097200` 的十六进制是 `0x200028`，最高 4 位是 `0x2`——按本节译码它应命中 `mmc_mailbox`；而 `327686 = 0x50006`，最高 4 位 `0x5`，不在 0/1/2 之内，会落到 `default`（`lb_slave_data_read` 的默认或未用段）。
3. 预期结果：读 `0x200028` 能看到邮箱字节；写命令的回包结构与请求包等长（LASS 特性）。
4. 待本地验证：若没有 Verilator v5，可改为源码阅读型实践——把 `0x200028` 与 `0x50006` 两个地址按 [marble_base.v:218-221](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L218-L221) 的译码逻辑人工推导出各自命中的从机，并与上面现象对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lb_write` 用 `lb_control_strobe & ~lb_control_rd` 而不是单独一根写信号？

> **答案**：localbus 的设计哲学是"最少信号"：用 `strobe` 表达"本次周期有效"，用 `rd` 表达方向（1 读 0 写）。这样写 = `strobe & ~rd`、读 = `strobe & rd`，省掉一根专用写信号，时序也更简单（见 u2-l2）。

**练习 2**：若主机发一个写地址 `0x10004`（写外部应用段）的数据，会落到哪里？

> **答案**：`0x10004` 最高 4 位是 `0x1`，命中 `p3_use_app_rd` 段。该段读走 `lb_data_in`（外部应用，当前 `0xfaceface`），写则同时作为 Tx MAC 主机的写入区（[marble_base.v:229](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L229) `host_write = lb_control_strobe & ~lb_control_rd & (lb_addr[23:20]==1)`）。所以这个写会进 `mac_compat_dpram` 的发送缓冲，可用来让 FPGA 主动发以太网帧。

---

### 4.3 ctrace / wctrace：片上触发与波形追踪

#### 4.3.1 概念说明

FPGA 调试的痛点是"看不见内部信号"。商用工具（如 Xilinx ILA）能抓波形，但跟工程绑定死、不便脚本化。Bedrock 自带了两种轻量替代：

- **`ctrace`**（[homeless/ctrace.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/homeless/ctrace.v)）：窄位（数据位宽 `dw` 较小）的片上捕获器，直接嵌在 `lb_marble_slave` 里，挂 localbus。Marble 用它在 MMC 的 SPI 活动时抓 4 位调试信号（见 [lb_marble_slave.v:131](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/lb_marble_slave.v#L131)）。
- **`wctrace`**（wide ctrace，[projects/ctrace/wctrace.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace.v)）：参数化位宽版本，数据 + 时间戳总宽可达 256 位，专门做成可被网络（LEEP）读回的"片上示波器"，配 Python 客户端 `ctracer.py` 还原成 VCD。

两者的核心思想相同：**只在被监视信号发生变化时才往 trace 存储器写一条**，每条记录"距上次变化过了多少拍 + 当时的信号值"。这是一种类似逻辑分析仪的**差分压缩**——信号静止时一行都不写，存储器只装"事件"而非"逐拍快照"。

#### 4.3.2 核心流程

`wctrace` 的三个参数：

- `AW`：地址位宽，决定存储深度 \(\;2^{AW}\;\) 条记录；
- `DW`：数据位宽，被监视信号的总位数；
- `TW`：时间戳位宽，记录"距上一事件多少拍"，溢出周期 \(\;2^{T_{W}}\;\) 拍；约束 \(\;DW + TW \le 256\)。

捕获状态机（ clk 域）：

```text
IDLE ──start──> RUN(pc=0, count=1)
RUN: 每拍 data1←data, data2←data1, diff←(data1 != data)
     wen = running & (diff | of)            // 数据变了 或 时间戳溢出
     if wen:  写 {count, data2} 到 dpram[pc]; pc++; count←1; pc 满(全1)则停
     else:    count++（带溢出标志 of）
读出（lb_clk 域）：lb_addr 索引 dpram；按 DW+TW 总宽选择 32 位切片返回
```

每条记录由 `[TW+DW-1:0]` 位拼成：高 `TW` 位是"距上一事件的时间戳 count"，低 `DW` 位是"事件发生前一拍的旧数据 data2"。用 data2（而不是 data）是为了让"变化"可还原：读到一条记录，就知道"在 count 拍之后，信号从 data2 变成了下一条记录的 data2"。

读出侧有一个 `generate` 分支树（`normal/wide0..wide6`），因为记录总宽往往不是 32 的整数倍，需要按 `DW+TW` 落在哪个区间、用地址低位选择正确的 32 位切片——这部分是纯组合的位拼。

#### 4.3.3 源码精读

**(a) 差分捕获的判断逻辑**

[wctrace.v:35-59](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace.v#L35-L59) 是核心：

```verilog
reg [TW-1:0] count = 0;
reg [DW-1:0] data1 = 0, data2 = 0;   // 两级流水
reg diff = 0;
reg of = 0;                           // 时间戳溢出
wire wen = running_r & (diff | of);
always @(posedge clk) begin
  data1 <= data;  data2 <= data1;
  diff <= data1 != data;              // 上一拍到本拍是否变化
  if (start) begin pc<=0; running_r<=1; count<=1; of<=0; end
  else if (wen) begin                 // 有事件：落一条记录
    count <= 1; pc <= pc + 1;
    if (&pc) running_r <= 0;          // 存满 (pc 全 1) 停止
    of <= 0;
  end else {of,count} <= count + 1;   // 无事件：时间戳自增
end
```

[wctrace.v:61-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace.v#L61-L70) 把 `{count, data2}` 写进双端口 `dpram`（A 口 clk 写、B 口 lb_clk 读），实现"采集域与读出域"分离：

```verilog
wire [DW+TW-1:0] saveme = {count, data2};
dpram #(.dw(DW+TW), .aw(AW)) xmem(
  .clka(clk), .clkb(lb_clk),
  .addra(pc), .dina(saveme), .wena(wen),
  .addrb(addrb), .doutb(doutb));
```

**(b) 读出切片选择**

[wctrace.v:72-137](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace.v#L72-L137) 的 `generate` 树按总宽选切片。以最窄的 `normal`（\(\,DW+TW\le 32\)）为例（[第 132-136 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace.v#L132-L136)）：地址与字一对一、直接高位补零输出；越宽的分支（`wide1` 用 2 个字、`wide2` 用 4 个字……`wide6` 用 8 个字）地址右移越多、并用地址低位选 32 位切片。

**(c) 把 wctrace 挂上网络：wctrace_top**

[wctrace_top.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v) 是把 wctrace 做成"网络可达示波器"的演示顶层。它复用了 u4-l2/u4-l4 学过的 `mem_gateway` 当 localbus 主桥（[第 26-39 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v#L26-L39)），再用一段**手写地址译码**（注释明说"must agree with the hand-written wctrace_top_regmap.json"，没用 newad.py）把 localbus 周期分发到各寄存器（[第 72-89 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v#L72-L89)）：

```verilog
casez (addr[15:0])
  16'h1000: start <= data_out[0];          // 写：启动捕获（单拍 strobe）
  ...
  16'b0100_0???_????_????: lb_din <= {16'h0000, config_rom_out}; // 0x4000: LEEP ROM
  16'h1002: lb_din <= {..., pc_mon};       // 读：当前写指针
  16'h1001: lb_din <= {..., running};      // 读：是否在采集
  16'h000???: lb_din <= wctrace_lb_out;    // 读：trace 数据
  default:   lb_din <= 32'hdeadbeef;
endcase
```

这段恰好是 4.2 节数据通路的一个"迷你版"：UDP 包 → `mem_gateway` → `control_*` → 这里手写译码 → `wctrace` 的 `start/lb_addr/lb_out`。也就是说，`wctrace_top` 自己就是"UDP 读片上波形"的完整工程缩影。

**(d) 信号映射 config.in**

主机端怎么知道某条记录里的 20 位数据各对应哪个信号？靠 [config.in](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/config.in)。它声明参数（`F_CLK/AW/DW/TW`）和位映射：

```ini
[15:0]  = counter        # wctrace data 的第 15..0 位 = counter 信号
[19:16] = strobes        # 第 19..16 位 = strobes 信号
```

`ctracer.py` 据此把读出的原始字流还原成带信号名的 VCD（见 [ctrace/README.md:37-66](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/README.md#L37-L66)）。映射语法支持单比特、矢量片段、带 scope（`top.dut.foo`）等形式。

#### 4.3.4 代码实践

> **实践目标**：跑通 wctrace 的多种位宽测试，并对照源码确认"差分压缩"行为。

1. 操作步骤：
   - 进入 `projects/ctrace`，运行 `make -C projects/ctrace`（等价于 [Makefile:13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/Makefile#L13) 的 `all`，含 `wctrace_w2/w1/w0/n_check` 四种位宽 + `wctrace_live`）。
   - 若只装了 iverilog 没装 Verilator，可单独跑 `make wctrace_n_check`、`make wctrace_w2_check` 等；`wctrace_live` 会被跳过。
2. 需要观察的现象：四个 `_check` 分别对应 \(\,DW+TW\) 落在 `normal/wide0/wide1/wide2` 不同分支（见 [Makefile:15-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/Makefile#L15-L18) 的 `-DTEST_*`），用来覆盖 `generate` 树的不同路径。
3. 预期结果：四种位宽都 `PASS`（具体断言在 `wctrace_tb.v` 内，待本地验证）。
4. 进阶（待本地验证，需 Verilator）：按 [README.md:12-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/README.md#L12-L24) 跑实时演示：一个终端 `make wctrace_live && ./wctrace_live +udp_port=3010`，另一个终端 `PYTHONPATH=../common python3 ctracer.py get leep://localhost:3010 -c config.in -o test.vcd --runtime 1`，再用 gtkwave 打开 `test.vcd`，应看到 `counter` 与 `strobes` 的波形（来自 [wctrace_top.v:104-105](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v#L104-L105) 的假数据发生器）。

#### 4.3.5 小练习与答案

**练习 1**：如果把一个静止不动的信号接给 wctrace，存储器会被写满吗？

> **答案**：不会很快写满，但仍会缓慢写入。因为 `wen = diff | of`：信号不变时 `diff=0`，但时间戳 `count` 数到溢出后 `of=1`，仍会落一条记录。也就是说 wctrace 的最长静止间隔上限是 \(\,2^{T_W}\) 拍——超过就得靠溢出事件"续命"，避免时间戳回卷造成歧义。

**练习 2**：`wctrace_top` 的地址译码为什么是"手写"的，而 Marble 工程里很多译码是 newad.py 生成的？

> **答案**：因为 `wctrace_top` 是一个最小演示工程，寄存器很少、变动不频繁，手写 `casez` 更直观（[wctrace_top.v:71](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v#L71) 注释明说"must agree with the hand-written wctrace_top_regmap.json"）。真实大工程（如 Marble）端口多、易错，才用 u2-l3 的 newad.py 自动生成解码器与地址表，减少样板与手写错误。

---

### 4.4 projects/common 与工程构建/调试工具链

#### 4.4.1 概念说明

本节澄清一个可能的误解：讲义大纲把 `projects/common` 描述为"公共脚本"，但打开 [projects/common/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/common/README.md) 你会发现它目前**非常小**——只有一个 `get_raw_adcs.py`，用途是从 banyan 缓冲里取 ADC 数据。也就是说，`projects/common` 当前并不是"所有工程共享的工具大本营"，而是一个**尚未充分填充的共享目录**。

Bedrock 真正的"工程构建公共件"其实在两处：一是 `build-tools/`（`top_rules.mk`、`newad.py`、`cdc_snitch` 等，前面讲义已覆盖），二是**每个工程自带**的脚本（如 `projects/ctrace/ctracer.py`、`projects/test_marble_family/testcase.py`）。`projects/common` 只放那些明确需要被多个工程 `import` 的少量代码。理解这一点，能避免你在 `common` 里徒劳地找某个其实属于具体工程的脚本。

#### 4.4.2 核心流程

一个 Bedrock 工程（以 Marble 为典型）的构建生命周期是分层的，每一步都对应 Makefile 里的若干目标：

```text
1. 配置     : CONFIG=marblemini|marble  → 选板、选 IP
2. 特性生成 : marble_features.yaml --gen_features.py--> *_defs.vh / *_params.vh
3. 仿真测试 : make marble_base_tb / lb_marble_slave_tb / ...（iverilog + vvp）
4. CDC 检查 : make marble_base_cdc.txt（yosys + cdc_snitch，经 marble_base_shell）
5. Lint     : make marble_top_lint / marble_base_lint（Verilator --lint-only）
6. 约束生成 : pin_map.csv + meta-xdc.py --> $(CONFIG).xdc
7. 综合     : make bit  --> Vivado --> $(CONFIG).bit
8. 上板     : make hwload (openocd) ; make hwtest (ping + UDP 回环)
```

注意第 6 步正是 u7-l3 讲过的「`pin_map.csv` + `meta-xdc.py` 把硬件引脚名改写成应用端口名」的工程落地（见 [Makefile:152-157](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L152-L157)）。

#### 4.4.3 源码精读

**(a) projects/common 的真实内容**

[projects/common/README.md:1-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/common/README.md#L1-L7) 只说"This directory for now only contains software required to capture data from banayan memory"，并介绍 `get_raw_adcs.py` 是"从 banyan buffer 取数据"的库。所以本讲对"公共脚本"的定位是：**它是一个预留的、跨工程共享的数据采集助手目录，目前内容很少**；不要把它当成工程构建的核心。

**(b) 工程构建骨架：三段 include + 本地规则**

[Makefile:1-5](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L1-L5) 是 u1-l3 讲过的"三段式 Makefile"开头：先 include `dir_list.mk` 拿到各子系统绝对路径，再 include 各外设的 `*_rules.mk`，最后 include `top_rules.mk`。本工程额外把 I2C 桥的规则也拉进来（[第 2-3 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L2-L3)）。

**(c) Packet Badger 的 client 列表怎么挂进工程**

[Makefile:71-74](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L71-L74) 是本讲的"点睛"之一——它揭示了 Marble 工程给 Packet Badger 配了哪些 UDP 服务：

```makefile
RTEFI_CLIENT_LIST = hello.v speed_test.v mem_gateway.v spi_flash.v
RTEFI_EXTRA_V    = spi_flash_engine.v
include $(BADGER_DIR)/rules.mk
```

也就是说，这个工程在网上暴露了 4 个 UDP 端口插件：`hello`（连通性测试）、`speed_test`（吞吐测速）、`mem_gateway`（读写 localbus 寄存器，4.2 节主角）、`spi_flash`（读写 boot Flash）。`include $(BADGER_DIR)/rules.mk` 会把这些 client 的源码与 `rtefi_blob` 编到一起。这正是 u4-l4「最多 8 个 client 插件」在工程层的具体配置。

**(d) 从仿真到上板**

仿真：[Makefile:125-128](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L125-L128) 定义了 `marble_base_tb` 的源码清单 `MARBLE_BASE_V`，把 `marble_base.v` 与所有 `rtefi`/`ctrace`/`lb_marble_slave` 等凑齐。

上板：[Makefile:185-189](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L185-L189) 的 `bit` 目标调 Vivado 跑 `marble.tcl` 综合成 `$(CONFIG).bit`；[Makefile:194-206](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L194-L206) 的 `hwload`/`hwtest` 用 openocd 灌比特流、再 ping + UDP 收发 + 校验 gitid 做回归。

#### 4.4.4 代码实践

> **实践目标**：在不动手综合的前提下，把 Marble 工程的构建目标与产物梳理清楚。

1. 操作步骤：
   - 通读 [projects/test_marble_family/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile)，把所有 `.PHONY` 目标与文件目标分类成「测试 / Lint / CDC / 综合 / 上板」五组。
   - 特别留意 `all` 目标（[第 63 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L63)）默认依赖了哪些 testbench，这决定了 `make`（不带参数）会跑什么。
2. 需要观察的现象：`all` 不包含 `bit`——也就是说默认 `make` 只跑仿真/Lint，不触发长达数十分钟的 Vivado 综合，这是有意为之的"快速反馈"设计。
3. 预期结果：你会得到一张「目标 → 依赖 → 工具 → 产物」表，例如 `bit → marble.tcl + $(CONFIG).d + $(CONFIG).xdc → Vivado → $(CONFIG).bit`。
4. 待本地验证：若装了 iverilog，运行 `make -C projects/test_marble_family marble_base_tb` 与 `make -C projects/test_marble_family no_multiple_drivers_check`（后者用 awk 扫描仿真输出的"多驱动"警告，见 [Makefile:130-133](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L130-L133)），记录是否 PASS。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `make`（默认目标）不把 `bit` 也包含进去？

> **答案**：因为 Vivado 综合很慢（分钟级），而开发者改一行 RTL 后最想要的是"测试有没有破"。所以 `all` 只挂仿真与 Lint 目标（[Makefile:63](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L63)），把昂贵的综合留给显式的 `make bit`。这是 Bedrock 「快速反馈优先」的一贯风格（参见 u1-l2 的 selftest 同理）。

**练习 2**：`RTEFI_CLIENT_LIST` 里增删一个 `.v` 文件，会对网络侧产生什么影响？

> **答案**：`mem_gateway`、`spi_flash` 等是 Packet Badger 的 UDP 端口插件（u4-l4）。在 `RTEFI_CLIENT_LIST` 增删文件，就是增删 FPGA 对外暴露的 UDP 服务端口。例如去掉 `spi_flash.v`，网络上就不再能读写 boot Flash；加一个自定义 `my_svc.v` 并实现 client 接口约定，就能新增一个 UDP 服务。`include $(BADGER_DIR)/rules.mk`（[Makefile:74](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L74)）负责把它们与 `rtefi_blob` 装配在一起。

---

## 5. 综合实践

把本讲三处要点串起来，完成下面这个"纸上追踪"任务（无须硬件，但鼓励有条件者上机）：

**任务**：假设你有一块 Marble Mini，FPGA IP 为 192.168.19.31。主机发一个 UDP 读请求，要读 `lb_marble_slave` 内、地址 `0x0000A` 的 ctrace 状态寄存器（`{ctrace_arm, ctrace_running}`，见 [lb_marble_slave.v:149](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/lb_marble_slave.v#L149)）。请：

1. **列子系统**：对照 [marble_top.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/marble_top.v) 与 [marble_base.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v)，列出这个请求从网线进入到寄存器返回要依次穿越的模块实例名（`gmii_to_rgmii_i` → `rtefi` → `slave` → …）。
2. **画数据通路**：画出 4.2 节那样的框图，标出 `0x0000A` 在 [marble_base.v:218-221](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marblemini/marble_base.v#L218-L221) 命中哪一支（提示：最高 4 位 `0x0`）。
3. **解释 ctrace**：根据 4.3 节，说明为什么这个状态寄存器能反映"片上捕获器是否还在跑"，以及它的值经 `dpram` 读出与直接组合读出有何区别。
4. **(选做，待本地验证)**：若本地有 Verilator，按 [Makefile:167-176](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L167-L176) 跑 `Vmarble_base`，用 `lbus_access.py` 实际读这个地址，与你纸上的推导对照。

**参考要点**：

- 穿越顺序：PHY → `gmii_to_rgmii_i`（marble_mid.vh）→ `rtefi` 的 scanner/`udp_port_cam`/`mem_gateway`（p3 通道）→ `marble_base` 的 p3↔lb 翻译 → 地址 `0x0000A` 最高 4 位为 0 → `lb_marble_slave`（实例 `slave`）→ 返回 `ctrace_status` → 经 `p3_lb_data_in` 多路选择 → `mem_gateway` 固定延迟采样 → `construct` 封包 → `rtefi` 的 tx 侧 → `gmii_to_rgmii_i` → PHY → 主机。
- `0x0000A` 命中 `lb_slave_data_read` 支（`p3_use_app_rd`/`p3_use_mbox_rd` 都为 0）。
- 状态寄存器是组合/时序产生的标志位，反映 `running_r`/`ctrace_arm`；而 trace **数据**本身经双端口 `dpram` 读出（采集在 clk 域、读出在 lb_clk 域），这是"控制寄存器组合读、波形数据经 DPRAM 跨域读"的典型分工。

---

## 6. 本讲小结

- Bedrock 工程采用**四层壳**结构：`marble_top.v`（引脚壳）→ `marble_mid.vh`（时钟 + GMII + 调用 base）→ `marble_base.v`（集成心脏，位于 `board_support/` 而非 `projects/`）→ 各子系统；外加一个独立的 `marble_base_shell.v` 仅供 CDC 检查。
- `marble_base` 是集成心脏，实例化了 `rtefi_blob`（Packet Badger）、`mmc_mailbox`、`lb_marble_slave`、`base_rx_mac`、`freq_demo` 等子系统；它被刻意保持 100% 可移植、不含厂家原语。
- UDP→localbus 的完整数据通路是：`rtefi_blob` 的 `mem_gateway` 作 master 发起 `p3_*` 周期 → `marble_base` 翻译成 `lb_*` → 按 `lb_addr[23:20]` 在 `lb_marble_slave`/外部应用/`mmc_mailbox` 间分发 → 读数据原路回喂 `mem_gateway` → 经固定延迟采样后封包返回。全程无握手。
- `ctrace`/`wctrace` 是 Bedrock 自带的片上逻辑分析仪，采用**差分压缩**（只在信号变化或时间戳溢出时落一条 `{count, data2}` 记录），经双端口 `dpram` 跨域读出；`wctrace_top` 用 `mem_gateway` + 手写译码把它做成网络可达的"片上示波器"，配 `ctracer.py` + `config.in` 还原 VCD。
- `projects/common` 目前只是预留的共享数据采集目录（仅 `get_raw_adcs.py`），真正的工程构建公共件在 `build-tools/`，工程特有脚本随工程走。
- 工程构建生命周期为「配置 → 特性生成 → 仿真 → CDC → Lint → 约束 → 综合 → 上板」，默认 `make` 只跑快速反馈（仿真/Lint），昂贵的 Vivado 综合留给显式 `make bit`。

---

## 7. 下一步学习建议

至此，你已经从「Bedrock 是什么」（u1-l1）一路走到「把整套东西装上板」（本讲），完整闭环。接下来可以：

- **横向对照 comms_top（u5-l3）**：`projects/comms_top` 是另一个完整工程，它把高速串行链路（以太网-over-fiber + ChitChat）装进同一个 Quad MGT。把它的顶层与 `marble_top` 对照，体会「同一套 localbus + Badger 方法论如何服务于完全不同的物理层」。
- **深入 LiteX 流派**：`projects/trigger_capture` 与 `projects/oscope` 走的是 LiteX/Python 流（`capture.py` 用 `litex.RemoteClient` 经 wishbone 访问），与 Marble 的 LEEP/UDP 路径不同。如果你想理解 Bedrock 与 LiteX 生态如何并存，可从这两个工程入手。
- **亲手加一个 UDP 服务**：在 `projects/test_marble_family/Makefile` 的 `RTEFI_CLIENT_LIST` 仿照 `hello.v` 加一个最小 client，重新综合上板，用 `lbus_access.py` 访问——这是把本讲所有知识化为肌肉记忆的最快路径。
- **回到方法学源头**：若想再夯实基础，可重读 u2-l2（localbus）、u4-l2（mem_gateway 固定延迟读）、u4-l4（Packet Badger client 接口）——本讲的每一条数据线最终都能追溯到这三讲里的某个设计决策。
