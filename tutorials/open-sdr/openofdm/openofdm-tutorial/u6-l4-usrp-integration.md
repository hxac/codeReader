# USRP N210 集成与 usrp2 模块

## 1. 本讲目标

OpenOFDM 不是一段只能在仿真器里跑的"算法演示代码"，它是一个真正可综合、并且已经在 Ettus Research USRP N210 板子上跑通过的 802.11 解码器。本讲要回答的核心问题是：**这个 `dot11` 模块到底该插到 USRP 的哪一段电路上、host 怎么控制它、解码出来的字节又怎么回到 host**。

学完本讲你应该能够：

- 画出 USRP N210 FPGA 接收链（`rx_frontend → ddc_chain → dsp_rx_glue → vita_rx_chain`）的结构，并指出 `dot11` 应插入的位置（DDC 之后、VITA RX 之前）。
- 说清 `dsp_rx_glue` 占位模块、`RX_DSP0_MODULE` 宏、自定义 Makefile 这三件事如何配合，把 `dot11` "挂"进接收链。
- 从"平台依赖"的视角深读 `verilog/usrp2/setting_reg.v` 与 `verilog/usrp2/ram_2port.v` 这两个 USRP 胶水模块，理解它们为何与作者自写 RTL 分属不同目录。
- 解释 host 端通过 UHD 的 `set_user_reg` 配置 OpenOFDM 的机制，并说明为何 UHD 驱动、ZPU 固件都不需要改、而 `rx_samples_to_file` 却失效了。

## 2. 前置知识

本讲默认你已经具备前置讲义建立的认知，不再重复细节，只做承接：

- **u1-l4 / dot11 的接口与时序**：`dot11` 顶层端口分控制、配置总线、I/Q 输入、字节输出、FCS 校验等若干组；输入为 32 位 I/Q 样本（高 16 位 I、低 16 位 Q），采样率 20 MSPS、时钟 100 MHz，故"每 5 拍来一个样本"。
- **u4-l4 / 配置寄存器机制 setting_reg.v**：USRP 风格的"配置总线"由 `set_stb`（写选通）、`set_addr`（8 位地址，最多 256 个）、`set_data`（32 位数据）三根线组成，**写专用、无读通道**，模型是"广播 + 自认领"。`SR_*` 地址定义在 `common_params.v`，现用地址 3–6。本讲只复用这个结论，不重列地址表。
- **u6-l3 / Xilinx IP 与 coregen 依赖**：`dot11_modules.list` 是 iverilog 的命令文件（`-c`），分四段（Xilinx 库搜索路径、手写 RTL、usrp2 平台模块、coregen IP 仿真模型）；`coregen/` 下是 Xilinx 黑盒 IP（FFT、Viterbi、除法器、各 LUT），用 `.v` 行为模型仿真，依赖 `unisims` 与 `XilinxCoreLib` 两套库。本讲会用到"usrp2 平台模块"这一段。

下面几个术语对本讲很关键，先统一一下：

- **USRP N210**：Ettus Research（现 NI）的一款软件无线电平台，FPGA 是 Spartan 3A-DSP。OpenOFDM 就是在它上面验证的。
- **DDC / DUC**：数字下变频 / 数字上变频。接收侧 DDC 把射频前端采下来的高速信号搬频、降采样成基带 I/Q。
- **VITA RX / TX**：USRP 里把采样数据打包成 VITA 帧并通过以太网发给 host（或反向）的收发链。
- **ZPU**：USRP FPGA 内部的一个软核处理器，参与 host 与 FPGA 之间的命令/寄存器交互。
- **UHD**：USRP 的 host 端驱动库（USRP Hardware Driver），host 通过它和 FPGA 通信。
- **平台胶水模块（platform glue）**：为了让作者自写的解码逻辑能落在 USRP 这块具体板子上，而从 USRP 官方代码库里"借"过来的、非算法性的支撑模块。`usrp2/` 目录下的两个文件就是这类。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 | 归属 |
|------|------|------|
| [docs/source/usrp.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst) | 集成指南：USRP N2x0 FPGA 结构、`dsp_rx_glue` 占位机制、自定义 Makefile、`dot11` 插入点 | 文档 |
| [Readme.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst) | 项目定位 + FAQ（UHD 是否要改、`set_user_reg`、ZPU 固件） | 文档 |
| [verilog/usrp2/setting_reg.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v) | 配置总线终端原语：按地址匹配锁存参数（USRP 平台代码，GPL） | 平台胶水 |
| [verilog/usrp2/ram_2port.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v) | 真双口 RAM 模板，供 equalizer/deinterleave/moving_avg 等做缓存（USRP 平台代码，GPL） | 平台胶水 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | `SR_*` 寄存器地址表（host 与 RTL 共享的地址契约） | 自写 RTL |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层模块：端口声明、配置总线扇出、子模块例化 | 自写 RTL |
| [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v) | 仿真测试台：在仿真里扮演 host，直接驱动配置总线 | 测试台 |

## 4. 核心概念与源码讲解

### 4.1 USRP N210 FPGA 架构与 dot11 的插入点

#### 4.1.1 概念说明

OpenOFDM 是一个"纯接收"解码器：吃 I/Q 样本，吐 802.11 字节。它本身不抓射频、不打包以太网帧。所以单独一个 `dot11` 模块没法工作——它必须被嵌进 USRP 既有的接收链路里，让上游给它喂样本，下游把它吐出的字节送回 host。

理解集成的第一步，是先看懂 USRP N210 这块板子的 FPGA 里，接收数据是怎么流动的。`usrp.rst` 给出了权威描述：N2x0（N200 与 N210 共用一套）的顶层模型在 `top/N2x0/u2plus.v`，它例化了核心模块 `u2plus_core`，而 `u2plus_core` 里就包含完整的接收链与发射链。

#### 4.1.2 核心流程

USRP N2x0 接收链的信号流向（来自 usrp.rst 的描述）：

```text
射频前端 ADC
     │  (高速数字样本)
     ▼
rx_frontend          ── 射频前端处理（增益/滤波等）
     │
     ▼
ddc_chain            ── 数字下变频：搬频 + 降采样到基带 I/Q
     │  ddc_out / ddc_out_strobe   ← 这里就是 20 MSPS 基带 I/Q
     ▼
dsp_rx_glue          ── 占位扩展点（默认直通）
     │     ▲
     │     └── dot11 就插在这一段
     ▼
vita_rx_chain        ── 打包成 VITA 帧，经以太网发回 host
```

关键结论：**`dot11` 应插在 DDC 之后、VITA RX 之前**。换句话说，`dot11` 的输入 `sample_in / sample_in_strobe` 直接对接 `ddc_out / ddc_out_strobe`。这正好与 u1-l4 建立的时序约定吻合——DDC 输出的就是 20 MSPS、按 strobe 节拍来的基带 I/Q，每 5 个 100 MHz 时钟来一对。

需要注意一个"文档只规定了一半"的事实：`usrp.rst` 明确写了**输入侧**的接法（`ddc_out → sample_in`），却没有明确规定 `dot11` 吐出的字节如何回到 host。这是因为标准 USRP 下游的 `vita_rx_chain` 期望的是"采样流"，而 `dot11` 吐出的是稀疏的解码字节脉冲——两者维度对不上。所以**输出路径是集成者的开放任务**（需要把 `byte_out` 自行打包送回 host），这也正是 FAQ 里 `rx_samples_to_file` 失效的根本原因（见 4.4）。

#### 4.1.3 源码精读

USRP N2x0 顶层与核心模块的包含关系，以及接收/发射链的组成，`usrp.rst` 是这样写的：

> The top level model of USRP N2x0 (N200 and N210) can be found in `top/N2x0/u2plus.v`. It instantiates the `u2plus_core` module, which contains the core modules such as the receiver and transmit chain. In particular, the receive chain includes `rx_frontend`, `ddc_chain` and `vita_rx_chain`.

——见 [docs/source/usrp.rst:8-16](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L8-L16)。这段把接收链三件套（`rx_frontend`、`ddc_chain`、`vita_rx_chain`）点了出来。

插入点的明确说法：

> To integrate |project|, we only need to *insert* it after the DDC but before VITA RX module. That is, the `sample_in/sample_in_strobe` of the `dot11` module should be connected to the `ddc_out/ddc_out_strobe` signal.

——见 [docs/source/usrp.rst:50-52](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L50-L52)。这是本讲最重要的一句集成指令。

输入约定的源头则在 Readme：

> the top level `dot11` Verilog module takes 32-bit I/Q samples (16-bit each) as input ... The sampling rate is 20 MSPS and the clock rate is 100 MHz. This means this module expects one pair of I/Q sample every 5 clock ticks.

——见 [Readme.rst:27-30](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L27-L30)。这解释了为什么 DDC 输出恰好能直接喂给 `dot11`：DDC 的输出采样率就是 20 MSPS。

而 `dot11` 顶层把 I/Q 输入和配置总线都开成了对外端口，见 [verilog/dot11.v:13-15](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L13-L15)（`sample_in[31:0]` 与 `sample_in_strobe`）。集成者要做的，就是在 `custom_dsp_rx.v` 里把 DDC 的两根线接到这两根线上。

#### 4.1.4 代码实践

**实践目标**：用一张图把"插入点"钉死，作为后续上板改造的导航。

**操作步骤**：

1. 重读 [docs/source/usrp.rst:8-21](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L8-L21)。
2. 画出从 ADC 到 host 的完整接收通路，标出 `rx_frontend / ddc_chain / dsp_rx_glue / vita_rx_chain` 四个方框。
3. 在 `dsp_rx_glue` 方框内部再画一个 `dot11` 小方框，用箭头标注：输入 `ddc_out/ddc_out_strobe → sample_in/sample_in_strobe`。
4. 在图上用问号标注输出侧：`byte_out` 如何回到 host 是开放问题。

**需要观察的现象**：你会清楚看到"输入侧定义清楚、输出侧留白"的不对称结构。

**预期结果**：一张能直接指导后续连线改造的数据通路图，并能在图上指出 `rx_samples_to_file` 这类"期望采样流"的工具会在哪一段断掉。

> 待本地验证：实际 USRP FPGA 源码（`u2plus_core.v` 等）不在本仓库内，本实践的依据是 `usrp.rst` 的文字描述；若要核对接线，需要另取 USRP 官方 `usrp_fpga` 仓库对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dot11` 必须插在 DDC 之后，而不能插在 DDC 之前（即射频前端 ADC 之后）？
**答案**：`dot11` 期望的是 20 MSPS 的**基带** I/Q（中心频率已搬到 0），而 ADC 之后是高速、带中频/射频的样本。DDC 正是完成"搬频 + 降采样到 20 MSPS 基带"的那一级，所以必须在它之后。

**练习 2**：`usrp.rst` 说"only need to insert it after the DDC"，但只写了输入接法。`dot11` 的 `byte_out` 应该接到哪里？
**答案**：文档没有规定。标准 `vita_rx_chain` 期望采样流，与字节流不匹配，因此输出路径需要集成者自行设计（例如自定义一个把 `byte_out` 打包成自定义帧送回 host 的包装模块）。这正是下一节 `rx_samples_to_file` 失效的原因之一。

---

### 4.2 dsp_rx_glue 占位机制与自定义编译

#### 4.2.1 概念说明

光知道"插在哪"还不够，还得知道"怎么让 USRP 的构建系统把 `dot11` 真正例化进去"。USRP 的 FPGA 代码库为此预留了**占位模块**（placeholder）：`dsp_rx_glue` 和 `dsp_tx_glue`。它们默认是纯直通（输入直接连输出，不做任何信号处理），由 Verilog 编译宏控制是否替换成自定义模块。

这个设计很聪明：官方构建不会因为你的自定义代码而坏掉，而想做扩展的人只要"在编译时把宏定义成自己的模块名"，占位模块就会自动例化它。OpenOFDM 正是利用了这个官方扩展点，从而**不需要改动 USRP 接收链的核心代码**，只改一份 Makefile。

#### 4.2.2 核心流程

把 `dot11` 挂进接收链的编译流程：

```text
1. 复制 top/N2x0/Makefile.N210R4 → Makefile.N210R4.custom
2. 改 BUILD_DIR      → 独立的 custom 构建目录
3. 注释掉 CUSTOM_SRCS / CUSTOM_DEFS（改到单独 Makefile 里定义）
4. 改 Verilog Macros → 定义:
     RX_DSP0_MODULE=custom_dsp_rx
     RX_DSP1_MODULE=custom_dsp_rx
     TX_DSP0_MODULE=custom_dsp_tx
     TX_DSP1_MODULE=custom_dsp_tx
     LVDS=1 | FIFO_CTRL_NO_TIME=1
5. dsp_rx_glue 检测到 RX_DSP0_MODULE 宏 → 例化 custom_dsp_rx
6. 在 custom/custom_dsp_rx.v 内把 dot11 接到 ddc_out
7.（可选）注释掉 u2plus_core 里第二条 RX 链以省 FPGA 资源
```

第 5 步是关键：`dsp_rx_glue` 内部会检查 `RX_DSP0_MODULE` 这个宏，找到了就把对应模块例化进来，否则保持直通。这正是"占位 + 宏驱动替换"的实现方式。

#### 4.2.3 源码精读

占位模块的存在与默认直通行为：

> The code base contains placeholder modules (`dsp_rx_glue` and `dsp_tx_glue`) for extension. These modules are controlled by Verilog compilation flags and by default they are simply pass-through and have no effect on the signal processing at all.

——见 [docs/source/usrp.rst:18-21](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L18-L21)。

宏驱动例化的机制与 Makefile 改法：

> inside `dsp_rx_glue` module, it checks the `RX_DSP0_MODULE` macro and instantiates it if found ... change it to `"LVDS=1|RX_DSP0_MODULE=custom_dsp_rx|RX_DSP1_MODULE=custom_dsp_rx|TX_DSP0_MODULE=custom_dsp_tx|TXDSP1_MODULE=custom_dsp_tx|FIFO_CTRL_NO_TIME=1"`.

——见 [docs/source/usrp.rst:27-42](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L27-L42)。注意它一次性把收发两条链的占位都指向了 `custom_dsp_rx` / `custom_dsp_tx`（因为 USRP 有两路天线，可配 TX/RX 或 RX/RX）。

省资源的提示：

> two receive chains are defined in `u2plus_core` module, so that the two antenna ports can be configured in TX/RX or RX/RX mode. To save FPGA resource, you may want to comment out one of the RX chains to make more room for |project|.

——见 [docs/source/usrp.rst:54-57](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L54-L57)。这条很实在：Spartan 3A-DSP 资源有限，OpenOFDM 又是占资源大户（FFT、Viterbi、双口 RAM 一堆），注释掉一路 RX 链是常见的腾地方手段。

#### 4.2.4 代码实践

**实践目标**：把"上板要改哪些 USRP 侧文件"列成一张可执行的清单。

**操作步骤**：

1. 阅读 [docs/source/usrp.rst:24-42](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L24-L42)。
2. 整理一张表，列出三处 Makefile 修改（`BUILD_DIR`、`CUSTOM_SRCS/CUSTOM_DEFS`、`Verilog Macros`）各自的"原文 → 改后"。
3. 单独列一行：`custom/custom_dsp_rx.v` 需要新建/修改，把 `ddc_out` 接到 `dot11.sample_in`。
4. 标注"可选"项：在 `u2plus_core` 里注释掉第二条 RX 链。

**需要观察的现象**：你会看到整个集成**不触碰 USRP 接收链的任何核心 RTL 文件**，改的只是构建配置 + 一个 `custom_dsp_rx.v` 包装。

**预期结果**：一份"只动两个文件（Makefile.N210R4.custom、custom_dsp_rx.v）+ 一处可选注释"的最小改动清单。

> 待本地验证：`Makefile.N210R4` 与 `custom_dsp_rx.v` 都在 USRP 官方 `usrp_fpga` 仓库，不在本仓库，需另取源码核对字段名。

#### 4.2.5 小练习与答案

**练习 1**：为什么官方要设计成"占位模块默认直通、用宏替换"，而不是直接让用户改接收链核心代码？
**答案**：保持官方构建的可复现性与稳定性——不集成自定义代码时行为完全不变；同时把扩展点收敛到一个受控的位置（`dsp_rx_glue`），降低用户改坏核心链路的风险。

**练习 2**：宏里同时定义了 `RX_DSP0_MODULE` 和 `RX_DSP1_MODULE` 都为 `custom_dsp_rx`，为什么是两个？
**答案**：USRP 有两路接收天线，`u2plus_core` 里定义了两条独立的 RX 链（分别对应 RX0/RX1），每条链各有一个 `dsp_rx_glue`，所以需要两个宏分别驱动两路的占位模块。

---

### 4.3 usrp2/ 平台胶水模块深读：setting_reg 与 ram_2port

#### 4.3.1 概念说明

u4-l4 已经从"配置机制"的角度讲过 `setting_reg`，本节换一个视角——**平台依赖**。要点是：`verilog/usrp2/` 下的两个文件**不是作者写的解码算法**，而是从 USRP 官方代码库里搬过来的、让 OpenOFDM 能在 N210 上落地生根的"胶水"。

证据就在文件头：两个文件的版权声明都是 `Copyright 2011 Ettus Research LLC`，许可证是 GPL v3（见 [verilog/usrp2/setting_reg.v:1-16](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v#L1-L16) 与 [verilog/usrp2/ram_2port.v:1-16](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L1-L16)），而 OpenOFDM 主体是 Apache 2.0。这种"许可证不一致"恰恰说明它们是 vendored（原样引入）的平台代码。这也解释了 u1-l3 建立的分类法：手写 RTL / Xilinx IP / USRP 平台代码三类源文件，本节落在第三类。

两个模块的分工：

- **`setting_reg`**：配置总线的"终端"。把广播出来的 `set_stb/set_addr/set_data` 按地址认领，锁存成本模块用的参数。
- **`ram_2port`**：一个参数化的真双口 RAM 模板。解码链里凡是要缓存一个 OFDM 符号或一段样本的地方（信道估计、解交织、滑动平均等），都用它当存储。

#### 4.3.2 核心流程

**`setting_reg` 的工作流程**（承接 u4-4 的"广播 + 自认领"模型）：

```text
每个时钟上升沿:
  if (rst):
      out <= at_reset        // 复位载入默认值
      changed <= 0
  else if (strobe & (my_addr == addr)):   // 总线在写，且地址是我
      out <= in              // 抓走数据
      changed <= 1           // 单拍脉冲：我被改写了
  else:
      changed <= 0
```

注意它**只写不读**：`out` 是寄存器输出（下游组合逻辑可直接用），但 host 没有任何通道把 `out` 读回来确认。这是 USRP 配置总线的一个固有约束，意味着参数校验只能靠观察解码行为。

**`ram_2port` 的工作流程**：两套独立的端口 A/B，各自有时钟、使能、写使能、地址、数据输入、数据输出。典型用法是"A 口写、B 口读"（写新数据的同时从另一地址读旧数据），两个端口可同频异址。

```text
端口 A（posedge clka）:
  if (ena):
      if (wea) ram[addra] <= dia   // 写
      doa <= ram[addra]            // 读（带一级寄存器）
端口 B（posedge clkb）: 对称同理
```

读出带一级寄存意味着"本拍给地址、下一拍出数据"，调用方需用 `delayT` 把配套的 strobe 错相位一拍对齐（这点 u6-l2 已建立）。

#### 4.3.3 源码精读

**`setting_reg` 模块声明**——三个参数（`my_addr` 本模块地址、`width` 位宽、`at_reset` 复位默认值）加标准配置总线端口：

[verilog/usrp2/setting_reg.v:20-25](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v#L20-L25)

```verilog
module setting_reg
  #(parameter my_addr = 0, 
    parameter width = 32,
    parameter at_reset=32'd0)
    (input clk, input rst, input strobe, input wire [7:0] addr,
     input wire [31:0] in, output reg [width-1:0] out, output reg changed);
```

匹配锁存的核心逻辑——复位优先、否则按 `strobe & (my_addr==addr)` 抓数据并拉一拍 `changed`：

[verilog/usrp2/setting_reg.v:27-40](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v#L27-L40)

```verilog
always @(posedge clk)
  if(rst)
    begin out <= at_reset; changed <= 1'b0; end
  else
    if(strobe & (my_addr==addr))
      begin out <= in; changed <= 1'b1; end
    else
      changed <= 1'b0;
```

它的真实用法以 `power_trigger.v` 为例——三处例化分别挂 `SR_POWER_THRES / SR_POWER_WINDOW / SR_SKIP_SAMPLE`，复位默认值 100 / 80 / 5,000,000：见 [verilog/power_trigger.v:35-47](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L35-L47)；`sync_short.v` 再挂一个 `SR_MIN_PLATEAU`：见 [verilog/sync_short.v:78-80](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L78-L80)。这些 `SR_*` 地址定义在 [verilog/common_params.v:17-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L17-L22)（地址 3–6）。这就是 host 与 RTL 之间唯一的"地址契约"。

**`ram_2port` 模块声明**——参数化位宽 `DWIDTH` 与地址位宽 `AWIDTH`，两套对称端口：

[verilog/usrp2/ram_2port.v:20-39](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L20-L39)

存储体与上电清零（`initial` 块把整块 RAM 清 0、输出寄存器清 0——这是仿真模型行为，综合时会映射到 BRAM）：

[verilog/usrp2/ram_2port.v:41-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L41-L48)

端口 A 的写后读（注意 `if(wea) ram[addra]<=dia` 与 `doa<=ram[addra]` 同拍执行——写优先语义：同址时读到的是新值）：

[verilog/usrp2/ram_2port.v:50-57](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L50-L57)，端口 B 对称：[verilog/usrp2/ram_2port.v:58-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L58-L65)。

它的真实用法以 `equalizer.v` 为例——缓存两段 LTS 做信道估计：A 口在 `lts_in_stb` 时写、B 口按 `lts_raddr` 读，深度 64（`AWIDTH=6`）：

[verilog/equalizer.v:138-151](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L138-L151)。

这两个文件都被纳入编译清单，单列一段：[verilog/dot11_modules.list:33-34](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_modules.list#L33-L34)。

#### 4.3.4 代码实践

**实践目标**：厘清"平台胶水模块 vs coregen IP 黑盒"的分工，理解为何 OpenOFDM 要混用这两种存储/配置设施。

**操作步骤**：

1. 对比 [verilog/usrp2/ram_2port.v:41-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L41-L48) 与 u6-l3 讲过的 `coregen/` 下 `BLK_MEM_GEN_V4_2` 黑盒封装（如 `rot_lut.v`）。
2. 回答两个问题：(a) 为什么通用双口 RAM 用 `ram_2port`（可读可写的运行时存储），而固定的查找表用 coregen ROM 黑盒？(b) 为什么这两个文件放在 `usrp2/` 而不和作者自写 RTL 放一起？
3. 在仓库里统计 `ram_2port` 的例化次数（用 Grep），确认它在 `equalizer / deinterleave / sync_long / moving_avg / delay_sample` 等处的广泛复用。

**需要观察的现象**：`ram_2port` 是"运行时可写存储"的统一模板，被多个算法模块复用；coregen ROM 是"固定内容、综合时初始化"的黑盒。两者职责互补。

**预期结果**：一句话总结——平台胶水模块解决"在 N210 上落地"的可综合性/可移植性，coregen IP 解决"重运算/大表"的实现效率。

#### 4.3.5 小练习与答案

**练习 1**：`setting_reg` 的 `out` 是 `reg` 型且模块没有读通道，host 改完一个参数后怎么确认它真的生效了？
**答案**：无法直接读回确认。只能通过观察解码行为（例如改 `SR_POWER_THRES` 后看是否还能触发包检测）来间接验证。这是 USRP 配置总线"只写不读"的固有约束。

**练习 2**：`ram_2port` 的 `doa <= ram[addra]` 读出带一级寄存，调用方（如 equalizer）如何保证 strobe 与数据对齐？
**答案**：用一个 `delayT` 把读地址/使能对应的 strobe 延时一拍，对齐到数据真正出现在 `doa` 的那一拍（详见 u6-l2 的"错相位"原语）。

---

### 4.4 Host 通信机制：UHD、set_user_reg 与 ZPU

#### 4.4.1 概念说明

集成不只改 FPGA——还要让 host 能用起来。好在 OpenOFDM 的设计哲学是"尽量复用 USRP 既有机制"，所以这一侧几乎零改动。三个 FAQ 把话说得很透：

1. **UHD 驱动要不要改？**——不用。OpenOFDM 依赖现有的 UHD–USRP 通信机制。
2. **host 怎么和 FPGA 里的 OFDM 核通信？**——通过 USRP 的"用户设置寄存器"`set_user_reg`，地址定义在 `common_params.v`。
3. **ZPU 固件要不要改？**——不用。

但有一条重要的副作用：因为 FPGA 的行为变了（不再把 RF 采样原样回传 host，而是改成吐解码字节），那些假设"USRP 就是采样回传设备"的工具会失效，典型就是 `rx_samples_to_file`。

#### 4.4.2 核心流程

host 配置一个 OpenOFDM 参数的完整通路（以"改包检测门限"为例）：

```text
host (Python/C++)
  │  usrp.set_user_reg(SR_POWER_THRES=3, 200)
  ▼
UHD 驱动（无需改）
  │  把写请求编成 USRP 控制包，经以太网发给 FPGA
  ▼
ZPU 软核（固件无需改）
  │  解包，驱动"配置总线"三根线
  ▼  set_stb=1, set_addr=3, set_data=200
配置总线（广播）
  │
  ▼  power_trigger 里的 setting_reg 命中 my_addr==3
setting_reg(.my_addr(SR_POWER_THRES))
  │  out <= 200
  ▼
power_trigger 的 power_thres 变成 200 → 改变检测灵敏度
```

这条链路的精妙之处：`set_user_reg` 是 USRP 早就提供的通用机制，`setting_reg` 是 USRP 早就提供的通用终端，OpenOFDM 只是在 `common_params.v` 里挑了几个地址（3–6）来用。所以 UHD 和 ZPU 固件都不用动——它们本来就是"地址透传"的通用基础设施。

#### 4.4.3 源码精读

UHD 无需改动 + `rx_samples_to_file` 失效的根因：

> No. In fact OpenOFDM relies on the current UHD-USRP communication mechanism. However, since the logic of the FPGA is changed in OpenOFDM, its behavior is also different. For instance, utilities such as `rx_samples_to_file` do not work as expected since the FPGA in OpenOFDM does not dumping RF signals back to host.

——见 [Readme.rst:41-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L41-L48)。这一段是"为什么 UHD 不改但行为变了"的权威解释：FPGA 不再把 RF 采样回传 host，而是改成解码后输出。

host 通信入口与寄存器地址所在：

> OpenOFDM FPGA module is configurable via USRP user setting registers (`set_user_reg` function). The register address definition is in `common_params.v` ... It is supposed to be placed in the receive chain of the USRP (e.g., `custom_dsp_rx.v`).

——见 [Readme.rst:50-58](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L50-L58)。

ZPU 固件无需改动，见 [Readme.rst:61-63](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L61-L63)。

寄存器地址表（host 与 RTL 共享的契约），见 [verilog/common_params.v:17-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L17-L22)：

```verilog
localparam SR_POWER_THRES   = 3;
localparam SR_POWER_WINDOW  = 4;
localparam SR_SKIP_SAMPLE   = 5;
localparam SR_MIN_PLATEAU   = 6;
```

那么仿真里怎么扮演 host？`dot11_tb.v` 直接把三根配置总线接成 `reg` 并手动驱动——这等价于 host 在上电时把 `SR_SKIP_SAMPLE` 改成 0（不跳过初始样本）：见 [verilog/dot11_tb.v:107-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114)。对照上图，测试台就是"绕过 UHD 和 ZPU，直接戳配置总线"。

而 `dot11` 顶层对配置总线只做**透传扇出**，把 `set_stb/set_addr/set_data` 原样分发给每个子模块（如 `power_trigger_inst`、`sync_long_inst`），自己不做地址译码——见 [verilog/dot11.v:8-11](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L8-L11)（端口）、[verilog/dot11.v:265-267](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L265-L267)（透传给 power_trigger）、[verilog/dot11.v:300-302](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L300-L302)（透传给 sync_long）。这印证了 u4-l4 的结论：新增参数无需改顶层。

#### 4.4.4 代码实践

**实践目标**：在仿真里亲手扮演一次 host，体会"配置总线 = host 控制 OpenOFDM 的唯一通道"。

**操作步骤**：

1. 打开 [verilog/dot11_tb.v:99-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L99-L114)。
2. 读这段 `initial` 逻辑：先 `reset=1`、`#20` 后放手，再 `set_stb=1` 并保持，`#20` 后给 `set_addr=SR_SKIP_SAMPLE`、`set_data=0`，再 `#20` 撤销 `set_stb`。
3. 模仿这套写法，在脑中（或在本地拷贝里）追加一段：用同样的方式把 `SR_POWER_THRES` 写成一个更小的值（例如 50），观察是否会让包检测更早触发。
4. 对照 `power_trigger.v` 里 `setting_reg #(.my_addr(SR_POWER_THRES)...)` 的例化，确认你写的地址会被这个实例命中。

**需要观察的现象**：写 `SR_SKIP_SAMPLE=0` 后，`power_trigger` 不再跳过 5,000,000 个初始样本，于是仿真刚启动就可能进入 `S_IDLE` 等待触发——这对应 host 上电配置的等价行为。

**预期结果**：理解"仿真驱动配置总线 = host 调 `set_user_reg`"的等价关系，并能解释为何新增一个可调参数只要：(a) 在 `common_params.v` 加一个 `SR_*` 地址；(b) 在某子模块里例化一个 `setting_reg`；(c) host 端用对应地址调 `set_user_reg`。三处都不需要动 UHD、ZPU 或 `dot11` 顶层。

> 待本地验证：本仓库不含 UHD host 示例代码，`set_user_reg` 的确切 Python/C++ 调用签名需查 UHD 文档（不同 UHD 版本接口名略有差异，新版多为 `set_user_settings_reg`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 UHD 驱动和 ZPU 固件都不用改？
**答案**：因为 OpenOFDM 复用的是 USRP 早就提供的通用机制——`set_user_reg`（host 侧）和配置总线 + `setting_reg`（FPGA 侧）。这两者本来就是"按地址透传"的通用基础设施，OpenOFDM 只是在 `common_params.v` 里挑了几个地址（3–6）来用，没有引入任何需要 UHD/ZPU 感知的新协议。

**练习 2**：`rx_samples_to_file` 为什么失效？失效的根因在 UHD 还是在 FPGA？
**答案**：根因在 FPGA。OpenOFDM 改变了 FPGA 接收链的行为——不再把 RF 采样原样回传 host（数据被 `dot11` 消化成了字节），所以 host 端"收采样"的工具自然收不到预期样本流。UHD 本身没改，只是它面对的 FPGA 行为变了。

**练习 3**：`dot11` 顶层为什么对配置总线只做透传、不译码？
**答案**：因为地址认领发生在各子模块内部的 `setting_reg`（用 `my_addr==addr` 自认领）。顶层透传使得"新增参数"完全本地化到子模块，不需要改顶层——这是可扩展性的关键。

---

## 5. 综合实践：编写"OpenOFDM 上板清单"

把本讲四个模块串起来，产出一份可直接交付的上板集成清单。这是本讲的主实践任务。

**实践目标**：假设你要把 OpenOFDM 部署到一台全新的 USRP N210 上，写出一份覆盖"FPGA 接收链改动、ZPU 固件、host 通信、失效工具"四方面的清单。

**操作步骤**：

1. **FPGA 接收链改动**（依据 4.1、4.2）：
   - 复制 `top/N2x0/Makefile.N210R4` 为 `Makefile.N210R4.custom`，改 `BUILD_DIR`、注释 `CUSTOM_SRCS/CUSTOM_DEFS`、把 Verilog Macros 改成带 `RX_DSP0_MODULE=custom_dsp_rx` 等的那一长串（见 [usrp.rst:33-42](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L33-L42)）。
   - 修改/新建 `custom/custom_dsp_rx.v`，把 `ddc_out/ddc_out_strobe` 接到 `dot11` 的 `sample_in/sample_in_strobe`（见 [usrp.rst:50-52](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L50-L52)）。
   - （可选）在 `u2plus_core` 注释掉第二条 RX 链省资源（见 [usrp.rst:54-57](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/usrp.rst#L54-L57)）。
   - 列出"开放问题"：`byte_out` 如何送回 host 需要自行设计输出包装。
2. **ZPU 固件**（依据 4.4）：明确写"无需改动"，并解释原因（复用既有 `set_user_reg` 机制）。
3. **host 通信**（依据 4.4）：
   - 读写寄存器：通过 UHD 的 `set_user_reg`，地址表见 [common_params.v:17-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L17-L22)（只写不读）。
   - 仿真等价做法见 [dot11_tb.v:107-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114)。
4. **失效工具**（依据 4.4）：列出 `rx_samples_to_file` 不再适用，解释根因是 FPGA 不再回传 RF 采样（见 [Readme.rst:41-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L41-L48)）。

**需要观察的现象**：清单应当清楚地显示"改动集中在两份 USRP 侧文件 + 一个输出路径设计题"，而 host 侧和 ZPU 侧几乎零改动。

**预期结果**：一份结构化清单（建议用表格），列"方面 / 要不要改 / 改什么 / 依据链接"四列，能直接指导一次真实的上板尝试。

> 待本地验证：清单中涉及 USRP 官方 `usrp_fpga` 仓库的文件（`Makefile.N210R4`、`custom_dsp_rx.v`、`u2plus_core.v`）不在本仓库，实际字段名与行号需以 USRP 官方源码为准；本清单的依据是 OpenOFDM 仓库内的 `usrp.rst` 与 `Readme.rst`。

## 6. 本讲小结

- OpenOFDM 通过 USRP 官方预留的 `dsp_rx_glue` 占位扩展点接入 N210 接收链，`dot11` 应插在 **DDC 之后、VITA RX 之前**，输入直接对接 `ddc_out/ddc_out_strobe`。
- 集成只需改一份 `Makefile.N210R4.custom`（定义 `RX_DSP0_MODULE=custom_dsp_rx` 等宏）+ 一个 `custom_dsp_rx.v` 包装，**不触碰 USRP 接收链核心 RTL**；可选注释掉第二条 RX 链以省 FPGA 资源。
- `verilog/usrp2/setting_reg.v` 与 `ram_2port.v` 是从 USRP 官方 vendored 来的**平台胶水模块**（GPL），前者是配置总线的"地址认领终端"，后者是被 equalizer/deinterleave/moving_avg 等广泛复用的真双口 RAM 模板。
- host 通过 UHD 的 `set_user_reg` 配置 OpenOFDM，地址契约在 `common_params.v`（现用 3–6），配置总线**只写不读**；UHD 驱动与 ZPU 固件均无需改动，因为复用的是既有通用机制。
- 由于 FPGA 行为改变（不再回传 RF 采样、改吐解码字节），`rx_samples_to_file` 等假设"采样回传"的工具失效；`byte_out` 如何回到 host 是集成者要自行解决的输出路径问题。

## 7. 下一步学习建议

- 若你想继续围绕"把 OpenOFDM 变成一个能用的 sniffing 设备"，下一步应解决本讲遗留的**输出路径**开放问题：参考 USRP 的 `vita_rx_chain` 设计思路，把 `byte_out/byte_out_strobe` 自行打包成以太网帧送回 host。这部分本仓库不提供源码，需要结合 USRP 官方 `usrp_fpga` 仓库学习。
- 若你更关心"如何在 OpenOFDM 基础上扩展能力"，请进入 **u6-l5 扩展实践：新增调制/MCS/带宽支持**，它会盘点新增一种调制方式需要同步改动的模块清单。
- 若你想复习本讲涉及的"配置总线"细节（地址表、`at_reset` 默认值、`changed` 脉冲），可回看 **u4-l4 配置寄存器机制 setting_reg.v**；想复习"coregen IP 与平台模块在编译清单里的分工"，可回看 **u6-l3 Xilinx IP core 与 coregen 依赖**。
- 若你对"如何在仿真里验证上板行为"感兴趣，可结合 **u5-l3 仿真测试台 dot11_tb.v** 理解测试台如何扮演 host 驱动配置总线（本讲 4.4.4 已触及）。
