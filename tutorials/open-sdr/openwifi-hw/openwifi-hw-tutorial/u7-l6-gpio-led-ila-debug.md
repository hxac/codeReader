# GPIO/LED 调试、ILA 与 ENABLE_DBG

## 1. 本讲目标

当 openwifi 跑在真实的 Zynq 芯片上时，物理层（PL）里成千上万个触发器都是「埋在硅片里的」——你没法拿示波器探头去碰 `backoff_done` 或 `phy_tx_started`。本讲解决「设计上了板之后，怎么看里面发生了什么」这个工程问题。读完本讲你应当能够：

- 看懂 [gpio_led.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/gpio_led.md) 里每块板卡「LED/GPIO 引脚 ↔ FPGA 内部信号」的映射表，并理解 **flip** 与 **raw** 两种指示风格的差别。
- 追踪一个 LED 信号从 `xpu.v`（或 `tx_intf.v`/`rx_intf.v`）的输出端口，经过 `openwifi_ip` 层级、block design，最终落到物理引脚的完整路径。
- 理解 `edge_to_flip` 模块如何用「上升沿翻转」把高速事件变成肉眼可辨的闪烁。
- 掌握 `ENABLE_DBG` 宏 → `DEBUG_PREFIX`（`mark_debug`）→ Vivado ILA 片上逻辑分析仪的完整调试链路，并用一条 `create_ip_repo.sh` 命令把探针打开。
- 知道 `.ltx` 调试探针文件是怎么生成、怎么随 `sdk_update.sh` 交付给宿主机的。

本讲是「进阶开发」单元的最后一篇，定位于**片上调试（on-chip debug）**，属于 advanced 层级。

## 2. 前置知识

在进入源码前，先用三段大白话建立直觉。

**两类可观测性。** 调试嵌入式 FPGA 设计，本质是在「快/慢」和「定性/定量」之间做取舍：

- **LED / GPIO**：慢、定性。优点是零成本——板子上本来就焊了 LED 与 PMOD 接口，只要把某个状态信号接到引脚上，用眼就能看「收发机活没活、在不在发包」。缺点是只能看「有没有」，看不出时序细节，而且人眼分辨不出每秒上千次的事件。
- **ILA（Integrated Logic Analyzer）**：快、定量。它是 Xilinx 提供的「片上示波器」IP 核，跟你的设计一起综合进同一颗 FPGA，在时钟节拍上采样你指定的内部信号，存进一段片上 RAM，再由宿主机的 Vivado Hardware Manager 通过 JTAG 读回画成波形。优点是能看到纳秒级的时序；缺点是要额外占用 BRAM/逻辑资源，并且需要重新综合一次。

**为什么不能直接「引出」内部信号？** 综合（synthesis）与布局布线（place & route）会 aggressively 地优化、合并、重命名网络。一个你写的 `wire backoff_done`，到比特流里可能已经被合并进某个 LUT、甚至连名字都没了。所以必须有机制告诉工具「这些网络请保留、不要优化掉」——这就是 `` `mark_debug="true" `` 与 `DONT_TOUCH` 属性的作用，也是本讲后半部分的主角。

**两种 LED 风格。** 这是本讲前半部分的核心概念，先记住一句话定义（来自 [gpio_led.md:1-2](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/gpio_led.md#L1-L2)）：

- **flip**：信号每来一个上升沿，LED 状态翻转一次（亮变灭、灭变亮）。
- **raw**：LED 实时反映信号的原始 0/1 电平。

为什么需要 flip？因为像「检测到一个有效包」这种事件可能每秒发生成百上千次，若用 raw 风格，LED 会一直亮着（人眼分辨不出），你就看不出到底有没有在动；用 flip 把它变成「每事件翻转一次」，事件率被「除以 2」后降到一个能看见闪烁的频率，状态变化就一目了然了。

> 本讲承接 [u5-l1（xpu 控制核心总览）](u5-l1-xpu-overview.md)：那些 `cycle_start0`、`sig_valid`、`phy_tx_started`、`slice_en`、`tx_bb_is_ongoing` 等信号都是 xpu 及其子模块产出的「状态脉搏」，本讲就讲它们如何被「观测出去」。同时承接 [u7-l2（条件编译与 Verilog 宏体系）](u7-l2-conditional-compile-macros.md)里的 `` `define ``/`` `ifdef `` 机制——`ENABLE_DBG` 正是靠它注入的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `gpio_led.md` | 各板卡「物理 LED/GPIO 引脚 ↔ FPGA 内部信号」的语义映射表，附 flip/raw 风格标注。**人维护的权威文档**。 |
| `README.md` | 第 131–156 行讲条件编译宏怎么传；第 24 行提到 `.ltx`（ila 探针文件）；第 14 行链接到 `gpio_led.md`。 |
| `ip/xpu/src/xpu.v` | 产出绝大多数被观测信号（`cycle_start0_led`、`sig_valid_led`、`phy_tx_started_led`、`demod_is_ongoing_led`、`slice_en`、`tx_bb_is_ongoing`、`tx_rf_is_ongoing`），并例化 `edge_to_flip`。同时定义 `XPU_ENABLE_DBG`/`DEBUG_PREFIX`。 |
| `ip/xpu/src/edge_to_flip.v` | 实现 flip 风格的小模块：上升沿翻转。 |
| `ip/tx_intf/src/tx_intf.v` | 产出 `tx_itrpt_led`、`tx_end_led`，同样例化 `edge_to_flip`。 |
| `ip/rx_intf/src/rx_intf.v` | 产出 `fcs_ok_led`，例化 `edge_to_flip`。 |
| `ip/openwifi_ip_ultra_scale.tcl` | UltraScale+ 版把 xpu 的 led/gpio 输出暴露成层级端口（`led0..5`、`gpio_pmod1_0..2`）并连到 xpu——这是「信号如何穿出层级」的样本。 |
| `boards/create_ip_repo.sh` | 把命令行 `ENABLE_DBG` 参数翻译成各 IP 的 `` `define IP_NAME_ENABLE_DBG ``。 |
| `boards/sdk_update.sh` | 综合后把 `.ltx` 调试探针文件随 `.xsa` 一起拷给软件/镜像仓库。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 LED/GPIO 映射**（慢速、定性观测）与 **4.2 ILA 调试**（快速、定量观测）。二者构成 openwifi 在硬件层面「从粗到细」的完整观测手段。

### 4.1 LED/GPIO 映射

#### 4.1.1 概念说明

openwifi 的低层 MAC（xpu）和收发接口（tx_intf/rx_intf）在工作时会产生大量「状态脉搏」信号：开始一个发送周期、检测到有效包的 SIGNAL 字段、PHY 开始发射、CRC 校验通过、某个队列被门控打开……这些信号本身就是 1 bit，天然适合直接驱动一颗 LED 或一个 GPIO 引脚。

`gpio_led.md` 就是把这些「FPGA 内部信号名」与「板卡上具体哪颗 LED、哪个 PMOD 引脚」一一对应起来的对照表，并标注了用哪种风格（flip/raw）去显示。它的价值在于：**当你看着板子上某颗 LED 在闪，你能立刻知道它在代表 FPGA 内部的哪个事件**——这是现场调试（「收发机到底活没活？」）最快的第一手信息。

#### 4.1.2 核心流程

一个被观测信号从产生到点亮 LED，经过这样一条链路：

1. **产生**：某子模块（如 xpu 内部）算出一个事件信号，例如 `sig_valid = pkt_header_valid_strobe & pkt_header_valid`（「收到了一个包头有效的帧」）。
2. **风格化**：
   - 若该信号在 `gpio_led.md` 里标为 **flip**，则经过一个 `edge_to_flip` 实例，把「事件脉冲」转成「翻转电平」。
   - 若标为 **raw**，则直接取信号本尊（例如 `slice_en[3:0]`、`tx_bb_is_ongoing`）。
3. **穿出层级**：该 LED 信号作为 xpu（或 tx_intf/rx_intf）的 `output` 端口，经 `openwifi_ip` 层级暴露成一个层级端口（如 `led3`、`gpio_pmod1_0`）。
4. **接引脚**：在 block design（`system.bd`）里把这个层级端口接到顶层端口，再由 `system.xdc` 把顶层端口绑定到物理封装引脚（PACKAGE_PIN）。
5. **点亮**：比特流下载后，物理 LED / PMOD 引脚即反映该信号。

**flip 的数学本质**：`edge_to_flip` 是一个被「输入信号上升沿」触发的 T 触发器（toggle flip-flop）。设输入为 `d`、输出为 `y`，其递推关系为：

\[
y_n = y_{n-1} \oplus \bigl(d_n \cdot \overline{d_{n-1}}\bigr)
\]

即「当且仅当本拍 `d=1` 且上拍 `d=0`（上升沿）时，输出翻转」。若事件以频率 \(f\) 到来，LED 翻转频率为 \(f/2\)。对每秒上千次的事件，\(f/2\) 仍远高于人眼融合频率（约几十 Hz），LED 看似常亮；但只要事件率有波动（例如突然收到一批包），闪烁节奏就会变化，肉眼即可感知「在动」。

#### 4.1.3 源码精读

**先看权威映射表。** `gpio_led.md` 顶部先定义两种风格（[gpio_led.md:1-2](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/gpio_led.md#L1-L2)）：

```text
- style: flip -- the line/led flip per rising edge of the FPGA signal event
- style: raw  -- the line/led always reflects the raw 1/0 state of the FPGA signal
```

以 `adrv9361z7035` 板为例（[gpio_led.md:8-14](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/gpio_led.md#L8-L14)），可以看到三类典型映射：

| 板上标识 | FPGA 内部信号 | 来源文件 | 风格 | 含义 |
|----------|---------------|----------|------|------|
| LED_GPIO_3 (DS6) | `cycle_start0_led` | xpu.v | flip | 一个发送周期开始 |
| LED_GPIO_2 (DS5) | `sig_valid_led` | xpu.v | flip | 接收端检测到有效包 SIGNAL 字段 |
| LED_GPIO_1 (DS4) | `phy_tx_started_led` | xpu.v | flip | 开始发射一个包 |
| IO_L23_13_JX2_N 等 | `slice_en[0..3]` | xpu.v | raw | 4 个队列的门控（0 关 / 1 开） |

注意 `slice_en` 用的是 **raw**——因为它表达的是「当前状态」（队列开/关），而不是「事件」，所以直接反映电平即可。这就是 flip 与 raw 选用准则：**事件用 flip，状态用 raw**。

UltraScale+ 板 `zcu102_fmcs2` 则用了更多 LED 与 PMOD 引脚（[gpio_led.md:23-30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/gpio_led.md#L23-L30)），增加了 `fcs_ok_led`（CRC 通过）、`tx_itrpt_led`（发完包中断）、`tx_end_led`（I/Q 生成完成）以及 PMOD 上的 `tx_bb_is_ongoing`/`tx_rf_is_ongoing`/`demod_is_ongoing` 三个 raw 状态。

**再看信号在 xpu 里怎么产生并风格化。** xpu 的端口区声明了这些被观测输出（[ip/xpu/src/xpu.v:68-72](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L68-L72)）：

```verilog
// led (flip per event) & gpio (raw)
output wire demod_is_ongoing_led,
output wire cycle_start0_led,
output wire phy_tx_started_led,
output wire sig_valid_led,
```

以及 raw 风格的状态输出（[ip/xpu/src/xpu.v:87-90](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L87-L90)）：

```verilog
output wire [3:0] slice_en,
output wire backoff_done,
output wire tx_bb_is_ongoing,
output wire tx_rf_is_ongoing,
```

`sig_valid` 这个事件信号由组合逻辑给出（[ip/xpu/src/xpu.v:346](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L346)）：

```verilog
assign sig_valid = (pkt_header_valid_strobe&pkt_header_valid);
```

四个 flip 风格的 LED 都由同一段 `edge_to_flip` 例化产生，且整体被 `XPU_DISCONNECT_LED` 宏包裹（[ip/xpu/src/xpu.v:394-422](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L394-L422)），其中 `sig_valid_i` 一例：

```verilog
`ifndef XPU_DISCONNECT_LED
  ...
  edge_to_flip sig_valid_i (
      .clk(s00_axi_aclk),
      .rstn(s00_axi_aresetn),
      .data_in(sig_valid),
      .flip_output(sig_valid_led)
  );
  ...
`endif
```

> `XPU_DISCONNECT_LED` 是一个「裁剪开关」：定义它就能把四个 `edge_to_flip` 实例整体摘掉（比如某块小板 LED 不够、或你想省这几十个寄存器）。它和后面要讲的 `XPU_ENABLE_DBG` 一样，都是靠 [u7-l2](u7-l2-conditional-compile-macros.md) 讲过的 `_pre_def.v` 条件编译机制注入的。

**`edge_to_flip` 的实现极其简短**（[ip/xpu/src/edge_to_flip.v:16-25](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/edge_to_flip.v#L16-L25)）：

```verilog
reg data_in_reg;
always @(posedge clk) begin
    if (~rstn) begin
        data_in_reg <= 0;
        flip_output <= 0;
    end else begin
        data_in_reg <= data_in;
        flip_output <= ((data_in==1 && data_in_reg==0)?(~flip_output):flip_output);
    end
end
```

这就是上一节那个递推式的直接翻译：用 `data_in_reg` 记住上一拍，当「本拍 1 且上拍 0」时把 `flip_output` 取反，否则保持。tx_intf 与 rx_intf 也用同一个模块来产生各自的 LED（[ip/tx_intf/src/tx_intf.v:278-289](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L278-L289) 给 `tx_itrpt_led`/`tx_end_led`；[ip/rx_intf/src/rx_intf.v:301-305](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L301-L305) 给 `fcs_ok_led`）。

**最后看信号如何穿出 `openwifi_ip` 层级。** 普通 Zynq 版的参考 `openwifi_ip.tcl` 主要关注数据/控制/中断通路，并未把所有 debug LED 暴露成端口；而 UltraScale+ 版的 [ip/openwifi_ip_ultra_scale.tcl:140-148](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L140-L148) 则显式建了 `led0..5` 与 `gpio_pmod1_0..2` 这些层级输出端口，并在 [ip/openwifi_ip_ultra_scale.tcl:386](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L386)、[:406](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L406)、[:409](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L409) 把它们连到 xpu 输出：

```tcl
connect_bd_net -net xpu_0_demod_is_ongoing_led [get_bd_pins led3] [get_bd_pins xpu_0/demod_is_ongoing_led]
...
connect_bd_net -net xpu_0_tx_bb_is_ongoing [get_bd_pins gpio_pmod1_0] ... [get_bd_pins xpu_0/tx_bb_is_ongoing]
connect_bd_net -net xpu_0_tx_rf_is_ongoing [get_bd_pins gpio_pmod1_1] ... [get_bd_pins xpu_0/tx_rf_is_ongoing]
```

这就是「层级端口 ← xpu 输出」的接线样本。这些 `ledN`/`gpio_pmod1_*` 再在 `system.bd` 里接到顶层端口，并由 `system.xdc` 绑定到封装引脚（例如 `neptunesdr` 的约束把 `gpio_bd[k]` 绑到 `LED_GPIO_*` 物理引脚）。

> ⚠️ **一个关于「权威性」的提醒**：`gpio_led.md`（人维护的语义文档）与 `openwifi_ip_ultra_scale.tcl`（`write_bd_tcl` 导出的参考脚本）对「哪个 `ledN` 接哪个信号」可能并不完全一致——比如参考 tcl 里 `led3` 接的是 `demod_is_ongoing_led`，而 `gpio_led.md` 里 `LED3` 写的是 `fcs_ok_led`。这类导出脚本与文档随版本会各自漂移。**结论：想知道你手上那块板、那个比特流的某颗 LED 到底代表什么，以 `gpio_led.md` 为准，并结合你自己那次综合所用的源码核对。** 这本身就是一个重要的调试习惯——别盲信任何单一来源。

#### 4.1.4 代码实践

**实践目标**：学会用 `gpio_led.md` 这张表，把「板子上的灯」翻译成「FPGA 里的信号」。

**操作步骤**：

1. 打开 [gpio_led.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/gpio_led.md)，选定一块板（推荐 `zcu102_fmcs2`，因为它 LED/PMOD 最多）。
2. 任选至少 3 行，整理出「板上标识 → FPGA 信号名 → 来源 .v 文件 → 风格 → 含义」四列。
3. 对每个 flip 风格的信号，去对应源码（如 `ip/xpu/src/xpu.v`）里 `grep` 该信号的 `edge_to_flip` 例化，确认它的 `data_in` 接的是哪个事件；对 raw 风格的，确认它直接来自哪个子模块输出。

**示例答案（以 `zcu102_fmcs2` 为例）**：

- `LED3 DS40` → `fcs_ok_led` → `rx_intf.v` → flip → 「openofdm_rx 对一个包给出 CRC 正确」（`edge_to_flip` 的 `data_in` 接 `fcs_ok`）。
- `LED4 DS41` → `demod_is_ongoing_led` → `xpu.v` → flip → 「openofdm_rx 正在解一个包」。
- `PMOD1_0 (J87 1)` → `tx_bb_is_ongoing` → `xpu.v` → raw → 「基带正在发送 I/Q」（高电平期间为发送窗口）。

**需要观察的现象**：如果你手上有板子并能下载比特流，发起一次 `ping`，应能看到 `phy_tx_started_led`/`tx_end_led` 随发包翻转、`fcs_ok_led` 随收包翻转，而 `tx_bb_is_ongoing` 这类 raw 引脚用万用表/逻辑分析仪可量到与发送窗口对应的高电平。**（若无硬件，本步为「待本地验证」。）**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `slice_en[3:0]` 用 raw 而不用 flip？

**参考答案**：`slice_en` 表达的是「队列当前是否被门控打开」这个**持续状态**（0 关 / 1 开），需要随时反映电平高低；flip 只在状态切换瞬间翻转一次，之后电平不再随状态变化，无法表达「现在到底是开还是关」。状态量用 raw，事件量用 flip。

**练习 2**：`edge_to_flip` 复位时 `flip_output` 清 0。若上电后从未发生事件，LED 是亮还是灭？发生 3 次上升沿后呢？

**参考答案**：未发生事件时 `flip_output=0`（取决于 LED 接法是高电平点亮还是低电点亮，这里只讨论逻辑电平）。每来一次上升沿翻转一次，3 次后 `0→1→0→1`，逻辑电平为 1。

---

### 4.2 ILA 调试（ENABLE_DBG + mark_debug）

#### 4.2.1 概念说明

LED/GPIO 只能告诉你「有没有在动」，但调试物理层时你常常需要回答更精细的问题：`backoff_done` 相对于 `phy_tx_started` 提前还是落后了几个时钟？`tx_control_state` 在等 ACK 时卡在哪个状态？CSMA/CA 的退避计数器 `num_slot_random` 在一次冲突中数到了几？这些都需要看**纳秒级的波形**。

ILA（Integrated Logic Analyzer）就是干这个的。它是 Xilinx 提供的调试 IP，和你自己的设计一起综合进同一颗 FPGA，本质是「一段片上采样 RAM + 触发逻辑」：你在设计里把若干信号标记为「待观测」，综合后 Vivado 自动插入 ILA 探针，把它们接到一个调试核；运行时 ILA 按时钟节拍采样这些信号、存进片上 RAM，遇到你设的触发条件（某信号某值、某边沿）就冻结一段窗口，再由宿主机经 JTAG 读回、画成波形——和真实示波器几乎一样的体验，只是探头在芯片内部。

openwifi 没有把 ILA 探针写死在代码里，而是用一套**条件编译开关**让你按需打开：定义 `XPU_ENABLE_DBG`（或其它 IP 的对应宏）后，源码里一批信号会被加上 `mark_debug` 属性，Vivado 据此保留这些网络供 ILA 采样。

#### 4.2.2 核心流程

完整的「开探针 → 抓波形」流程如下：

1. **开宏**：用 `create_ip_repo.sh` 给想调试的 IP 传 `ENABLE_DBG` 参数。脚本会把它翻译成 `` `define XPU_ENABLE_DBG ``（写进 `ip_config/xpu_pre_def.v`）。
2. **加属性**：源码顶部 `` `ifdef XPU_ENABLE_DBG `` 生效，把 `` `DEBUG_PREFIX `` 展开成 `(*mark_debug="true",DONT_TOUCH="TRUE"*)`。凡是用 `` `DEBUG_PREFIX `` 前缀声明的信号（如 `` `DEBUG_PREFIX wire cycle_start0; ``）都被打上这两个属性。
3. **综合**：跑 `openwifi.tcl` 建顶层工程并 Generate Bitstream。综合阶段 Vivado 看到 `mark_debug`，会把这些网络标记为「待调试」，**不优化、不合并、保留可路由性**；`DONT_TOUCH` 进一步防止它们被吸收进更高层逻辑。
4. **插核（Set Up Debug）**：综合完成后，在 Vivado 里打开 Synthesized Design → Set Up Debug，把标记的网络分配到一个或多个 ILA 核（设置采样深度、触发时钟），保存为一个 debug 布局。
5. **生成 `.ltx`**：完成实现（implementation）并生成比特流时，Vivado 同时产出一个 `.ltx` 文件——它是「调试探针布局描述」，告诉 Hardware Manager 这些探针对应哪些信号、位宽、地址。
6. **交付**：`sdk_update.sh` 把 `.ltx` 随 `.xsa` 一起拷到镜像目录（见 4.2.3）。
7. **抓波形**：宿主机用 Vivado Hardware Manager 打开器件，刷新设备时会要求提供 `.ltx`；提供后即可在 ILA 窗口设触发、抓波形。

**为什么是 `mark_debug` + `DONT_TOUCH` 两个属性？** `mark_debug` 是「请保留这个网络并接进调试核」的请求；但综合器仍可能把它合并进等价逻辑。`DONT_TOUCH="TRUE"` 是更强的「禁止触碰」约束，确保该寄存器/网络原样保留。两者合用，才能保证你标记的信号在比特流里真实存在、且能被 ILA 路由到。

**资源代价**：每个 ILA 探针都要消耗 BRAM（做采样缓冲）与少量逻辑。采样深度越深、探针越多，占用越大。所以 `ENABLE_DBG` 默认是关的——只有调试时才开，调试完会去掉宏重新综合以释放资源。

#### 4.2.3 源码精读

**宏到属性的定义。** 以 xpu 为例（[ip/xpu/src/xpu.v:5-9](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L5-L9)）：

```verilog
`ifdef XPU_ENABLE_DBG
`define DEBUG_PREFIX (*mark_debug="true",DONT_TOUCH="TRUE"*)
`else
`define DEBUG_PREFIX
`endif
```

未定义 `XPU_ENABLE_DBG` 时，`` `DEBUG_PREFIX `` 展开成「空」，于是 `` `DEBUG_PREFIX wire foo; `` 就等价于普通的 `wire foo;`，不产生任何调试开销。这正是条件编译的妙处——同一份源码，调试时是「带探针版」，发布时是「精简版」。tx_intf（[ip/tx_intf/src/tx_intf.v:8-12](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L8-L12)）以及 rx_intf、side_ch 等都用完全一样的模板，只是宏名换成各自 IP（`TX_INTF_ENABLE_DBG`、`RX_INTF_ENABLE_DBG`、`SIDE_CH_ENABLE_DBG`）。

**属性的用法。** 在 xpu 端口区，几个对 MAC 调试极关键的信号被加上了前缀（[ip/xpu/src/xpu.v:40-41](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L40-L41) 与 [ip/xpu/src/xpu.v:83-84](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L83-L84)）：

```verilog
`DEBUG_PREFIX output wire [(TSF_TIMER_WIDTH-1):0]  tsf_runtime_val,
`DEBUG_PREFIX output wire tsf_pulse_1M,
...
`DEBUG_PREFIX output wire start_retrans,
`DEBUG_PREFIX output wire start_tx_ack,
```

内部 wire 也有（[ip/xpu/src/xpu.v:277-281](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L277-L281)）：

```verilog
`DEBUG_PREFIX wire cycle_start0;
`DEBUG_PREFIX wire slice_en0;
`DEBUG_PREFIX wire slice_en1;
`DEBUG_PREFIX wire slice_en2;
`DEBUG_PREFIX wire slice_en3;
```

开宏后，这些就是抓 CSMA/CA 与重传时序的「黄金探针」：`tsf_pulse_1M` 是 1µs 心跳（[u5-l4](u5-l4-tsf-rx-parse-filter.md)），`start_retrans`/`start_tx_ack` 是重传与回 ACK 的脉冲（[u5-l3](u5-l3-tx-control-retrans-ack.md)），`cycle_start0`/`slice_en*` 是周期与队列门控（[u5-l1](u5-l1-xpu-overview.md)）。

**怎么把宏传进去。** README 在「Conditional compile by verilog macro」一节给了现成命令（[README.md:153-156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L153-L156)）：

```bash
./create_ip_repo.sh $XILINX_DIR xpu ENABLE_DBG tx_intf ENABLE_DBG rx_intf ENABLE_DBG openofdm_tx ENABLE_DBG openofdm_rx ENABLE_DBG side_ch ENABLE_DBG
```

这条命令的背后逻辑在 `create_ip_repo.sh` 里（[boards/create_ip_repo.sh:51-73](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L51-L73)）：脚本扫参数，遇到一个 IP 名（如 `xpu`）就开始往该 IP 的 `ip_config/xpu_pre_def.v` 追加；其后的每个非 IP 名参数 `ARG`，被拼成 `` `define ${MODULE_NAME}_${ARG} `` 写入（[boards/create_ip_repo.sh:65-70](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L65-L70)），其中 `MODULE_NAME` 由 `${ARGUMENT^^}` 转大写得到（即 `xpu` → `XPU`）。所以参数 `xpu ENABLE_DBG` 最终产出 `` `define XPU_ENABLE_DBG ``——正好匹配源码里的 `` `ifdef XPU_ENABLE_DBG ``。这套「IP 名 + DEF」的拼接约定正是 [u7-l2](u7-l2-conditional-compile-macros.md) 讲过的核心契约。

**`.ltx` 的交付。** 综合实现完成后，`sdk_update.sh` 在拷 `.xsa` 的同时，检查 `.ltx` 是否存在并一并拷走（[boards/sdk_update.sh:30-34](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh#L30-L34)）：

```bash
if [ -f "$BOARD_NAME/.../impl_1/system_top.ltx" ]; then
    cp .../system_top.ltx $TARGET_SDK_DIR/ -rf
else
    echo "No debug probe file found."
fi
```

可见 `.ltx` 是**可选**的——只有当你在那次综合里插了 ILA 探针，它才会生成；没插则脚本只打印一句 `No debug probe file found.`。README 在介绍预编译镜像时也点明了这一点（[README.md:24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L24)）：镜像目录里「有 FPGA bit file、ila .ltx file **(if ila inserted)** 及其它初始化文件」。

> 关于构建流程里**没有**自动插 ILA 这一点要说清楚：仓库的 Tcl 脚本里搜不到 `create_debug_core` / `set_up_debug` / ILA 例化——`ENABLE_DBG` 只负责「给信号打 `mark_debug` 属性」。真正「把标记的网络装进 ILA 核、生成 `.ltx`」这一步，需要你在 Vivado GUI 里对 Synthesized Design 手动跑一次 **Set Up Debug**（或用 Tcl 的 `create_debug_core` 等命令）。这是 Vivado 的标准片上调试流程，不是 openwifi 特有的。

#### 4.2.4 代码实践

**实践目标**：把 xpu 的几个关键 MAC 信号标记为可调试，并说明在 Vivado 里如何把它们变成 ILA 波形。

**操作步骤**：

1. **开宏**（在 `boards/$BOARD_NAME` 目录下执行）：

   ```bash
   cd boards/zcu102_fmcs2   # 或你的板卡目录
   ../../boards/create_ip_repo.sh $XILINX_DIR xpu ENABLE_DBG
   ```

   这会生成 `ip_config/xpu_pre_def.v`，其中含有 `` `define XPU_ENABLE_DBG ``。用 `cat ip_config/xpu_pre_def.v` 核对（**示例命令，需本地有 Vivado 环境才能真正跑通后续 `vivado -source`，此处可先只看脚本生成的 `_pre_def.v` 内容**）。

2. **建顶层工程并综合**：`create_ip_repo.sh` 末尾会 `source ip_repo_gen.tcl`，再由 `openwifi.tcl` 建工程。在 Vivado GUI 里跑 Run Synthesis。

3. **Set Up Debug**：综合完成后 Open Synthesized Design → 菜单 Tools → Set Up Debug。向导会自动列出所有 `mark_debug=true` 的网络（即 `tsf_pulse_1M`、`start_retrans`、`start_tx_ack`、`cycle_start0`、`slice_en0..3` 等）。选一个触发时钟（一般用 `s00_axi_aclk`），设采样深度（如 1024 或 4096），保存为 debug 布局。

4. **生成比特流与 `.ltx`**：Run Implementation → Generate Bitstream。完成后 `impl_1/` 下应同时出现 `system_top.bit` 与 `system_top.ltx`。

5. **抓波形**：Open Hardware Manager → Open Target → Program Device，此时 Vivado 会提示选择 `.ltx`，选中后 ILA 窗口出现。设触发（如 `start_retrans == rising edge`），run trigger 后发起一次需要重传的发送，即可捕获到 `backoff_done`→`start_retrans`→`phy_tx_started` 的时序波形。

**需要观察的现象**：ILA 波形窗口里应能看到 `tsf_pulse_1M` 每 1µs 一个脉冲，`tx_control_state`（也可一并标记）在 IDLE 与等 ACK 状态之间跳转，重传发生时 `start_retrans` 出现脉冲。**以上步骤依赖本地 Vivado 2022.2 与硬件，若无环境则为「待本地验证」。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DEBUG_PREFIX` 要同时带 `mark_debug="true"` 和 `DONT_TOUCH="TRUE"`，只留 `mark_debug` 行不行？

**参考答案**：`mark_debug` 是向工具「申请」保留并调试该网络，但综合器仍可能把它合并进等价逻辑或吸收进上一级寄存器，导致探针失效。`DONT_TOUCH="TRUE"` 是更强约束，禁止工具对该对象做任何优化/合并，确保信号原样保留、可被 ILA 路由到。两者搭配才稳妥；只留 `mark_debug` 在简单信号上通常也能工作，但在易被优化的组合逻辑上可能失效。

**练习 2**：如果只想抓 side_ch 的 CSI 输出波形，应该传哪个宏？为什么对 `openofdm_rx` 传 `ENABLE_DBG` 也「无害」？

**参考答案**：传 `side_ch ENABLE_DBG`，生成 `` `define SIDE_CH_ENABLE_DBG ``。对 `openofdm_rx` 传 `ENABLE_DBG` 之所以无害，是因为它的源码（外部子模块）里并没有消费 `OPENOFDM_RX_ENABLE_DBG` 的 `` `ifdef `` 块——多写一个没人用的 `` `define `` 不会改变任何代码裁剪结果（参见 [u7-l2](u7-l2-conditional-compile-macros.md) 关于「只有四个自研 IP 真正消费 `*_ENABLE_DBG`」的结论）。所以 README 那条「全开」命令可以无脑照抄。

## 5. 综合实践

**任务**：把本讲两套观测手段串起来，对「一次发送 + 等待 ACK」做一次端到端的调试设计。

要求：

1. **定性侧（LED）**：依据 [gpio_led.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/gpio_led.md)，为你的板卡列出与「发送」相关的全部指示——`phy_tx_started_led`（开始发射）、`tx_end_led`（I/Q 生成完成）、`tx_itrpt_led`（发完中断），以及 raw 风格的 `tx_bb_is_ongoing`/`tx_rf_is_ongoing`。说明每颗灯/引脚在「发一个包」过程中应亮的先后顺序。

2. **定量侧（ILA）**：用 `create_ip_repo.sh $XILINX_DIR xpu ENABLE_DBG` 打开 xpu 的调试探针，在 Vivado Set Up Debug 里把 `tsf_pulse_1M`、`cycle_start0`、`backoff_done`、`start_retrans`、`start_tx_ack`、`phy_tx_started` 一起标记（注意 `phy_tx_started` 来自 openofdm_tx，若也要抓需给 `openofdm_tx` 传 `ENABLE_DBG` 并在其源码确认有对应 `` `DEBUG_PREFIX ``）。

3. **对照**：在抓到的 ILA 波形里，标注出 LED 上每个事件（`phy_tx_started_led` 翻转、`tx_end_led` 翻转）对应的时刻，验证「LED 看到的翻转」与「ILA 抓到的上升沿」是同一个事件。

4. **结论**：写一段说明——当 ACK 超时发生重传时，哪些 LED 会额外翻转、ILA 波形里 `start_retrans` 何时出现、`tx_control_state` 如何跳转。结合 [u5-l3](u5-l3-tx-control-retrans-ack.md) 的七状态机给出解释。

> 这个任务把「粗观测（LED）」与「细观测（ILA）」对齐到同一组事件上，是真实芯片调试中最常用的工作流：先用 LED 判断「大致在哪个阶段」，再用 ILA 精确定位时序问题。若无硬件/许可，则退化为纯源码阅读版——即把上述信号在 `xpu.v`/`tx_intf.v` 里的产生与消费关系画成一张时序图。

## 6. 本讲小结

- **两类观测手段**：LED/GPIO 是慢速、定性、零成本的「活没活」指示；ILA 是快速、定量、占资源的「片上示波器」。openwifi 二者都提供了入口。
- **flip 与 raw**：事件型信号（如收/发包）用 flip——靠 `edge_to_flip`（T 触发器）把上升沿转成翻转，事件率除以 2 后肉眼可辨；状态型信号（如 `slice_en`、`*_is_ongoing`）用 raw，直接反映电平。
- **权威映射表**：`gpio_led.md` 是「物理引脚 ↔ FPGA 信号」的人维护文档；信号从 `xpu.v`/`tx_intf.v`/`rx_intf.v` 产出，经 `openwifi_ip` 层级端口（UltraScale+ 版可见 `led0..5`、`gpio_pmod1_*`）接到顶层、由 `system.xdc` 绑定物理引脚。文档与导出脚本可能漂移，以 `gpio_led.md` + 本次源码为准。
- **ENABLE_DBG → mark_debug**：定义 `XPU_ENABLE_DBG` 等宏后，`` `DEBUG_PREFIX `` 展开成 `(*mark_debug="true",DONT_TOUCH="TRUE"*)`，给选定信号打上「保留并供调试」属性。开关由 `create_ip_repo.sh` 的 `ENABLE_DBG` 参数注入（约定为 `` `define IP_NAME_ENABLE_DBG ``）。
- **ILA 流程**：开宏 → 综合后 `mark_debug` 网络被标记 → Vivado Set Up Debug 手动插核 → 生成 `.bit` 与 `.ltx` → `sdk_update.sh` 把 `.ltx` 随镜像交付 → Hardware Manager 加载 `.ltx` 抓波形。仓库脚本不自动插 ILA，最后一步需手动完成。
- **资源取舍**：`ENABLE_DBG` 默认关闭，只在调试时打开、调试后关掉重新综合以释放 BRAM/逻辑。

## 7. 下一步学习建议

- **回到 xpu 内部**：本讲的探针（`backoff_done`、`start_retrans`、`tx_control_state`）都指向 xpu 的子模块。想看这些信号背后的状态机逻辑，重读 [u5-l2（CSMA/CA）](u5-l2-csma-ca.md) 与 [u5-l3（TX 控制与重传）](u5-l3-tx-control-retrans-ack.md)，把 ILA 波形和源码里的状态跳转一一对应。
- **结合 side_ch**：ILA 抓的是「信号波形」，而 [u6-l1（side_ch）](u6-l1-side-channel.md) 的 side_ch 抓的是「数据（CSI/RSSI/IQ）」。两者互补——ILA 看 MAC 时序，side_ch 看物理层数据。注意 side_ch 的关键输出也带了 `SIDE_CH_ENABLE_DBG`/`mark_debug`。
- **寄存器级调试**：ILA 之外，软件侧还能通过 AXI 寄存器读状态（如 xpu 的 `slv_reg57` 聚合了 RSSI/CCA/发送状态位）。回看 [u7-l1（AXI 寄存器映射）](u7-l1-axi-register-map.md) 与 [u5-l5](u5-l5-cca-rssi-spi.md)，理解「软件轮询寄存器」这条更轻量、但更慢的观测路径，与 ILA 形成第三种手段。
- **自己加探针**：若现有 `` `DEBUG_PREFIX `` 标记的信号不够用，可在 `xpu.v` 里给你关心的 wire 再加一个 `` `DEBUG_PREFIX `` 前缀，重跑 [u7-l4](u7-l4-modify-package-custom-ip.md) 的「改源码 → 重新打包 → 集成」流水线即可。这是把本讲学到的机制用起来的一次实战。
