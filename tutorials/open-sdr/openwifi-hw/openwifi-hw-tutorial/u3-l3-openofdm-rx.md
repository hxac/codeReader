# openofdm_rx：OFDM 接收机（外部子模块）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `openofdm_rx` 在 openwifi 接收链路中扮演的角色，以及它为什么是「外部 git 子模块」而不是本仓库自研的源码。
- 看懂 `get_ip_openofdm_rx.sh` 与 `.gitmodules` 如何把上游 [open-sdr/openofdm](https://github.com/open-sdr/openofdm) 的 `dot11zynq` 分支拉到 `ip/openofdm_rx` 目录。
- 从 `ip/openwifi_ip.tcl` 的 block design 接线中，读出 `openofdm_rx` 暴露给 `rx_intf`、`xpu` 的真实端口（样点输入、字节输出、FCS、包头、RSSI 等）。
- 解释 `boards/ip_repo_gen.tcl` 在打包 `openofdm_rx` 时，与其余 5 个自研 IP 的关键差异——即「`_pre_def.v` 追加模式」，并理解为什么必须用追加而不是覆盖。

本讲只覆盖一个最小模块：**openofdm_rx 子模块**。它承接 u3-l1（rx_intf 总览）与 u3-l2（rx_intf 子模块），把视角从「接收接口」推进到接口背后那个真正做 OFDM 解调的物理层核。

## 2. 前置知识

在进入本讲前，请先确认你理解下面几个概念（它们在前置讲义中已建立）：

- **PHY（物理层）与 MAC**：PHY 负责把无线电波变成比特（接收）或把比特变成无线电波（发射）；MAC 负责这些比特「什么时候发、发给谁、错了怎么办」。`openofdm_rx` 属于接收方向的 PHY。
- **OFDM（正交频分复用）**：802.11 a/g/n 把数据分到几十个子载波上并行传输，每个 OFDM 符号在时域上是一段 I/Q 样点。接收端要把它「反过来」处理：找包、对齐、做 FFT、均衡、解映射、译码。
- **子模块（git submodule）**：一个 git 仓库里嵌套记录的「另一个仓库的地址 + 某次 commit」。克隆主仓库后，子模块目录默认是空的，必须显式 `init` + `update` 才会拉取内容。这是理解本讲「为什么 `ip/openofdm_rx` 现在是空的」的关键。
- **PS / PL**：Zynq 芯片上 ARM（PS）与 FPGA（PL）两侧。`openofdm_rx` 运行在 PL 侧，PS 通过 AXI 寄存器读写它、通过中断获知它的状态。
- **采样率与每采样时钟数**：openwifi 基带采样率固定为 20 MHz（`SAMPLING_RATE_MHZ = 20`），基带时钟通常是 100 MHz，于是每采样点占用的时钟数为 \(\text{NUM\_CLK\_PER\_SAMPLE} = \text{NUM\_CLK\_PER\_US}/\text{SAMPLING\_RATE\_MHZ} = 100/20 = 5\)。这个 5 倍过采样关系是 `openofdm_rx` 与 `rx_intf` 之间逐样点交互的节奏基础。

## 3. 本讲源码地图

本讲涉及的文件都在 **openwifi-hw 主仓库**内（`openofdm_rx` 的内部源码位于外部子模块，本仓库不直接收录）：

| 文件 | 作用 |
| --- | --- |
| [.gitmodules](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/.gitmodules) | 声明 `ip/openofdm_rx` 子模块的 URL 与挂载路径。 |
| [get_ip_openofdm_rx.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_ip_openofdm_rx.sh) | 一次性脚本：`git submodule init/update` 拉取 `openofdm_rx`。 |
| [boards/ip_repo_gen.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl) | 循环打包六个 IP；对 `openofdm_rx` 走「不覆盖源 + 追加 pre_def」的特殊分支。 |
| [boards/create_ip_repo.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh) | 顶层构建入口，生成各 IP 的 `_pre_def.v`，再调用 `ip_repo_gen.tcl`。 |
| [ip/openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl) | block design 蓝图：实例化 `openofdm_rx_0` 并把它的端口连到 `rx_intf`、`xpu`。 |
| [ip/create_vivado_proj.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/create_vivado_proj.sh) | 单 IP 工程创建脚本；对 `openofdm_rx`，第三个参数是仿真用的 `SAMPLE_FILE`。 |
| [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md) | 说明 openofdm_rx 的来源、仿真方式与条件编译特例。 |

> 提示：`ip/openofdm_rx/` 在克隆后是空目录（子模块未填充）。本讲所有「源码精读」都基于主仓库可见的文件；凡是涉及子模块内部 Verilog 的描述，都会明确标注其依据（README 记载的仿真层级 / 上游 openofdm 项目），不会编造行号。

## 4. 核心概念与源码讲解

### 4.1 openofdm_rx 是什么：外部子模块与物理层定位

#### 4.1.1 概念说明

在 u3-l2 里我们看到，`rx_intf` 自己并不做 OFDM 算法：它只是把 ADC 送来的 I/Q 样点「原样转交」出去，再把对方吐回来的字节拼成 AXI-Stream 送给 DMA。那个「对方」就是 `openofdm_rx`——一个完整的 802.11 OFDM 接收机。

`openofdm_rx` 之所以特殊，是因为它**不是本仓库写的**。README 在结尾明确说明：

> The 802.11 ofdm receiver is based on [openofdm project](https://github.com/jhshi/openofdm). You can find our improvements in our openwifi fork (dot11zynq branch) which is mapped to ip/openofdm_rx.

也就是说：

- 它源自 Jianxiong Shi（jhshi）的 [openofdm](https://github.com/jhshi/openofdm) 开源项目（一个用 Verilog 实现的 802.11 a/g/d OFDM 接收机）。
- openwifi 团队 fork 了一份，在 `dot11zynq` 分支上做了适配 802.11 全速率、对接 Zynq/AD9361 的改进。
- 这个 fork 以 **git 子模块**的形式挂到 `ip/openofdm_rx`。

把 `openofdm_rx` 做成子模块的好处是：上游 openofdm 的更新可以被追踪和拉取，而 openwifi-hw 主仓库只记录「我们用的是它的哪一次 commit」，保持主仓库精简。

#### 4.1.2 核心流程

一个 OFDM 接收机要完成的事情，按时间顺序大致是（这是 802.11 OFDM 的通用处理链，openofdm_rx 的功能划分与此对应）：

```text
ADC I/Q 样点流
   │
   ▼
1) 包检测      ── 在背景噪声中识别出"来包了"（基于 STF 短训练序列的能量/自相关）
   │
   ▼
2) 同步与频偏  ── 确定符号边界、估计并校正载波频率偏移(CFO)
   │
   ▼
3) FFT         ── 把时域样点变回频域子载波
   │
   ▼
4) 信道均衡    ── 用 LTF 长训练序列估计信道，对每个子载波做复数除法补偿
   │
   ▼
5) 解映射      ── 把每个子载波的复数点还原成比特（BPSK/QPSK/16/64-QAM）
   │
   ▼
6) 解交织/解打孔/卷积译码(Viterbi) ── 把比特流还原成原始字节
   │
   ▼
7) FCS(CRC32) 校验 ── 输出字节流 + fcs_ok（帧是否完好）
```

我们并不能在本仓库里直接读到上面每一步的 Verilog（它们在子模块里），但有两处证据能印证这条链的结构：

- README 的仿真小节给出的 Vivado 信号层级是 `dot11_tb → dot11_inst → ofdm_decoder_inst → viterbi_inst`。这说明 `openofdm_rx` 的顶层叫 `dot11`，内部含 `ofdm_decoder`（OFDM 解调）和 `viterbi`（卷积译码）等模块——与上面的「FFT/均衡」和「Viterbi」两段对应。
- 它对外的输出端口（`byte_out`、`pkt_rate`、`pkt_len`、`fcs_ok`、`pkt_header_valid` 等，见 4.3）正好是这条链的产物。

#### 4.1.3 源码精读

README 末尾的来源声明（[README.md:194](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L194)）：

> `openofdm_rx` 基于 openofdm 项目，openwifi 的改进在 fork 的 `dot11zynq` 分支，映射到 `ip/openofdm_rx`。

README 的仿真小节（[README.md:116-130](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L116-L130)）以 `openofdm_rx` 为例，说明仿真时能看到顶层 testbench `dot11_tb`，并给出层级示例 `dot11_tb → dot11_inst → ofdm_decoder_inst → viterbi_inst`，这正是 `openofdm_rx` 内部结构的「官方自述」。

此外，`rx_intf.v` 里有一个版本标识位，间接说明 `openofdm_rx` 支持的速率集（[ip/rx_intf/src/rx_intf.v:268](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L268)）：

```verilog
assign slv_reg31[31] = 1'b0; // 0 to indicate this old rx_intf and openofdm_rx support a/g/n
```

这一行把寄存器位清零，向软件表明当前这套 `rx_intf` + `openofdm_rx` 支持 802.11 **a/g/n**（而不是只支持更老的 b）。`openofdm_rx` 因此被定位为「全速率 OFDM 接收机」。

#### 4.1.4 代码实践（源码阅读型）

**目标**：从公开信息确认 `openofdm_rx` 的血统，而不是凭空记忆。

**步骤**：

1. 打开本仓库的 `.gitmodules`，找到 `ip/openofdm_rx` 条目，记录它的 URL（应指向 `open-sdr/openofdm`）。
2. 在浏览器打开该 URL，确认它确实是 openofdm 项目的 fork；查看它的分支列表，确认存在 `dot11zynq` 分支。
3. 回到本仓库，读 README 的来源声明（第 194 行附近）和仿真小节（第 116–130 行），把「顶层模块名 = `dot11`」「关键内部模块 = `ofdm_decoder`、`viterbi`」记到你的笔记里。

**需要观察的现象**：`.gitmodules` 里的 URL 与 README 声明一致；上游 fork 里能看到 `dot11zynq` 分支。

**预期结果**：你能用一句话回答「`openofdm_rx` 从哪来、谁维护、openwifi 改了什么分支」。如果某一步无法联网验证，标注「待本地/联网验证」即可，不要伪造。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `openofdm_rx` 不像 `xpu`、`tx_intf` 那样直接放在本仓库的版本管理里，而要用子模块？
  - **答案**：因为它源自独立的 openofdm 项目，有自己的上游演进。用子模块可以在主仓库里只记录「依赖哪一次 commit」，既保持主仓库精简，又能在需要时拉取/升级上游，同时保留 openwifi 在 `dot11zynq` 分支上的定制改动。
- **练习 2**：README 仿真层级里出现了 `viterbi_inst`，它对应 4.1.2 处理链中的哪一步？
  - **答案**：第 6 步「卷积译码（Viterbi）」。它是把解映射后的软比特还原成原始字节的关键模块。

### 4.2 子模块的引入：get_ip_openofdm_rx.sh 与 .gitmodules

#### 4.2.1 概念说明

`openofdm_rx` 的挂载关系写在 `.gitmodules` 里，这是 git 子模块的「登记表」。但登记表只记地址和路径，**不会**在 `git clone` 主仓库时自动把内容拉下来——所以你刚克隆完 openwifi-hw 时，`ip/openofdm_rx/` 是个空目录。要填充它，需要运行一次 `get_ip_openofdm_rx.sh`。

这条「只跑一次」的脚本属于 u1-l4 讲过的「准备阶段」三件套之一（另两个是 `prepare_adi_lib.sh`、`prepare_adi_board_ip.sh`）。

#### 4.2.2 核心流程

```text
get_ip_openofdm_rx.sh
   │  cd ip/
   ▼
git submodule init   openofdm_rx   ── 在 .git/config 里登记这个子模块
   │
git submodule update openofdm_rx   ── 按 .gitmodules 记录的 commit 拉取内容到 ip/openofdm_rx/
   │
cd 回主目录
```

注意脚本里有两行被注释掉的命令：

```bash
# cd openofdm_rx
# git checkout dot11zynq
# git pull origin dot11zynq
```

这说明在「常规填充」流程里，`update` 会直接拉到子模块指针记录的那次 commit；只有当你想主动切换到 `dot11zynq` 分支最新并更新指针时，才需要手动取消注释执行那三行（这会改写主仓库记录的子模块 commit，属于升级 openofdm 的操作，普通构建不必做）。

#### 4.2.3 源码精读

子模块登记（[.gitmodules:4-6](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/.gitmodules#L4-L6)）：

```ini
[submodule "ip/openofdm_rx"]
    path = ip/openofdm_rx
    url = https://github.com/open-sdr/openofdm.git
```

填充脚本全文很短（[get_ip_openofdm_rx.sh:1-13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_ip_openofdm_rx.sh#L1-L13)）：

```bash
home_dir=$(pwd)
set -x
cd ip/
git submodule init openofdm_rx
git submodule update openofdm_rx
# cd openofdm_rx
# git checkout dot11zynq
# git pull origin dot11zynq
cd $home_dir
```

第 6 行先 `cd ip/`（子模块名 `openofdm_rx` 是相对 `ip/` 目录的），第 7、8 行完成登记与拉取。README 也把它列为构建准备步骤（[README.md:71-74](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L71-L74)）：

> Get the openofdm_rx into ip directory (only run once after openofdm is updated): `./get_ip_openofdm_rx.sh`

#### 4.2.4 代码实践（命令实操型，待本地验证）

**目标**：亲手填充子模块，观察 `ip/openofdm_rx` 从空变满。

**步骤**：

1. 在仓库根目录运行 `ls ip/openofdm_rx/`，确认它目前是空的（这是子模块未填充的正常状态）。
2. 运行 `./get_ip_openofdm_rx.sh`（需要联网）。
3. 再次运行 `ls ip/openofdm_rx/`，观察是否出现了源码目录。
4. 运行 `git submodule status`，查看 `openofdm_rx` 前面的大串哈希（即主仓库锁定的那次 commit）。

**需要观察的现象**：第 1 步目录为空；第 3 步出现 `src/` 等子目录；第 4 步状态行以空格开头（非 `-` 非 `+`），表示内容与记录的 commit 完全一致。

**预期结果**：`openofdm_rx` 被成功拉取，可参与后续打包。若无法联网，则标注「待本地验证」，但你仍可读懂脚本的每一步意图。

> 安全提示：本仓库环境当前 `ip/openofdm_rx/` 为空目录（未填充），因此本讲无法对该目录内的文件给出永久链接与行号——所有内部细节均以上游项目与 README 自述为依据。

#### 4.2.5 小练习与答案

- **练习 1**：如果不运行 `get_ip_openofdm_rx.sh` 直接去打包，会发生什么？
  - **答案**：`ip/openofdm_rx/` 为空，`ip_repo_gen.tcl` 在打包该 IP 时找不到源码与 `openofdm_rx.tcl`，Vivado 会报错失败。所以这一步是构建的前置条件。
- **练习 2**：脚本里被注释的 `git checkout dot11zynq` 三行，什么情况下才需要启用？
  - **答案**：当你想把子模块升级到 `dot11zynq` 分支的最新提交、并把主仓库的子模块指针前移时。日常构建用不到。

### 4.3 openofdm_rx 在接收链路中的真实接口

#### 4.3.1 概念说明

虽然 `openofdm_rx` 的内部 Verilog 在子模块里，但**它对外的端口是本仓库 block design 固定的**——这些端口由 `ip/openwifi_ip.tcl` 在「实例化 + 连线」时确定。换句话说，对 openwifi-hw 而言，`openofdm_rx` 是一个「VLNV 为 `user.org:user:openofdm_rx:1.0` 的黑盒 IP」，我们只需关心它和 `rx_intf`、`xpu` 之间的握手信号。

这一点很重要：它让我们**不必打开子模块源码**也能讲清接收链路的数据流。在 u3-l2 里我们已经从 `rx_intf` 一侧看到了这些信号，本讲从 `openofdm_rx` 一侧再确认一次，把两侧对齐。

#### 4.3.2 核心流程

`openofdm_rx` 在接收链路中的输入/输出可以分成三组：

```text
输入（来自 rx_intf / xpu）
  sample_in          ← 基带 I/Q 样点（rx_intf 转交的 ADC 数据）
  sample_in_strobe   ← 样点有效脉冲（= rx_intf 的 sample_strobe / xpu 的 ddc_iq_valid）
  rssi_half_db       ← 与 xpu 共享的 RSSI（半 dB 单位）网络

输出（送给 rx_intf 与 xpu，常常一拖二同时连两边）
  byte_out / byte_out_strobe / byte_count   ← 解出的字节流（rx_intf 据此拼 AXIS；xpu 据此解析 MAC 头）
  fcs_ok / fcs_out_strobe                    ← CRC32 校验结果（帧是否完好）
  pkt_header_valid / pkt_header_valid_strobe ← 包头解析完成（含速率/长度等）
  pkt_rate / pkt_len                         ← 该帧的调制速率与字节长度
  ht_unsupport                              ← 是否为不支持的 HT(11n) 帧
  demod_is_ongoing                          ← 解调进行中（给 xpu 做 MAC 控制用）

寄存器
  s00_axi  ← AXI4-Lite，PS 经此配置/读取 openofdm_rx
```

注意 `openofdm_rx` 的字节输出「一拖二」同时接到 `rx_intf` 和 `xpu`（见源码精读）：数据面上 `rx_intf` 把字节拼包送 DMA；控制面上 `xpu` 拿同一份字节去解析 MAC 头、做地址过滤、判重传。这正是 u2-l2 提到的「`openofdm_rx` 输出常一拖二同时送 `rx_intf` 与 `xpu`」。

#### 4.3.3 源码精读

实例化与 VLNV（[ip/openwifi_ip.tcl:194-196](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L194-L196)）：

```tcl
# Create instance: openofdm_rx_0, and set properties
set openofdm_rx_0 [ create_bd_cell -type ip -vlnv user.org:user:openofdm_rx:1.0 openofdm_rx_0 ]
```

`user.org:user:` 前缀表明这是自研/自打包 IP（区别于 `xilinx.com:ip:` 标准件，见 u2-l2）。它的寄存器口接到 AXI 互连的第 5 路从口（[ip/openwifi_ip.tcl:283](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L283)）：

```tcl
connect_bd_intf_net -intf_net axi_interconnect_1_M05_AXI \
  [get_bd_intf_pins axi_interconnect_1/M05_AXI] [get_bd_intf_pins openofdm_rx_0/s00_axi]
```

字节与状态输出「一拖二」（[ip/openwifi_ip.tcl:300-310](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L300-L310)）：

```tcl
connect_bd_net -net openofdm_rx_0_byte_count      [get_bd_pins openofdm_rx_0/byte_count]      [get_bd_pins rx_intf_0/byte_count] [get_bd_pins xpu_0/byte_count]
connect_bd_net -net openofdm_rx_0_byte_out        [get_bd_pins openofdm_rx_0/byte_out]        [get_bd_pins rx_intf_0/byte_in]    [get_bd_pins xpu_0/byte_in]
connect_bd_net -net openofdm_rx_0_byte_out_strobe [get_bd_pins openofdm_rx_0/byte_out_strobe] [get_bd_pins rx_intf_0/byte_in_strobe] [get_bd_pins xpu_0/byte_in_strobe]
connect_bd_net -net openofdm_rx_0_fcs_ok          [get_bd_pins openofdm_rx_0/fcs_ok]          [get_bd_pins rx_intf_0/fcs_ok]    [get_bd_pins xpu_0/fcs_ok]
connect_bd_net -net openofdm_rx_0_pkt_rate        [get_bd_pins openofdm_rx_0/pkt_rate]        [get_bd_pins rx_intf_0/pkt_rate]  [get_bd_pins xpu_0/pkt_rate]
connect_bd_net -net openofdm_rx_0_pkt_len         [get_bd_pins openofdm_rx_0/pkt_len]         [get_bd_pins rx_intf_0/pkt_len]   [get_bd_pins xpu_0/pkt_len]
# ……以及 demod_is_ongoing、ht_unsupport、pkt_header_valid(strobe)、fcs_out_strobe 等
```

样点输入来自 `rx_intf`（[ip/openwifi_ip.tcl:324-325](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L324-L325)）：

```tcl
connect_bd_net -net rx_intf_0_sample        [get_bd_pins openofdm_rx_0/sample_in]        [get_bd_pins rx_intf_0/sample] [get_bd_pins xlslice_0/Din] [get_bd_pins xlslice_1/Din]
connect_bd_net -net rx_intf_0_sample_strobe [get_bd_pins openofdm_rx_0/sample_in_strobe] [get_bd_pins rx_intf_0/sample_strobe] [get_bd_pins xpu_0/ddc_iq_valid]
```

同一个 `sample` 总线还被 `xlslice_0`/`xlslice_1` 切出 I、Q 分量（供 xpu 的 RSSI/CCA 链使用，详见 u5-l5）。RSSI 半 dB 网络则把 `openofdm_rx_0` 与 `xpu_0` 连在一起（[ip/openwifi_ip.tcl:359](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L359)）：

```tcl
connect_bd_net -net xpu_0_rssi_half_db [get_bd_pins openofdm_rx_0/rssi_half_db] [get_bd_pins xpu_0/rssi_half_db]
```

> 说明：单从这一行无法确定 `rssi_half_db` 的方向（网络名是 Vivado 自动取的，不代表驱动方）。在 openwifi 的设计里，RSSI 的计算主要在 `xpu`（见 u5-l5 的 rssi.v/iq_rssi_to_db.v 链路），`openofdm_rx` 在包检测阶段也会提供能量信息，二者通过这根共享线协同。具体方向以子模块源码与 xpu 实现为准（待结合 u5-l5 确认）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：把 `openofdm_rx` 的端口按「输入/输出/寄存器」三类整理清楚，并与 u3-l2 从 `rx_intf` 一侧看到的信号对齐。

**步骤**：

1. 打开 [ip/openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl)，搜索所有 `openofdm_rx_0/` 出现的行。
2. 把每一行的 `openofdm_rx_0/<pin>` 与它连接的对端（`rx_intf_0/`、`xpu_0/`、`xlslice_*`）抄成一张表。
3. 对照 u3-l2 里 `rx_intf` 的 `byte_in`、`fcs_ok`、`pkt_rate` 等，确认两侧信号名是否一一对应。

**需要观察的现象**：每个 `openofdm_rx_0/` 输出端口都至少连到 `rx_intf_0`，多数还同时连到 `xpu_0`；样点输入 `sample_in` 来自 `rx_intf_0/sample`。

**预期结果**：你得到一张完整的「`openofdm_rx` 对外端口表」，并能解释每个输出最终被谁消费。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `byte_out` 要同时连到 `rx_intf` 和 `xpu` 两个 IP？
  - **答案**：`rx_intf` 负责把字节拼成帧、加包头后经 DMA 上报 PS（数据面）；`xpu` 负责从同一份字节里解析 MAC 头、做地址过滤和重传/ACK 判定（控制面）。两者职责不同，所以都 需要 `byte_out`。
- **练习 2**：`openofdm_rx` 的寄存器接口 `s00_axi` 接在 `axi_interconnect_1` 的第几路从口？PS 如何找到它？
  - **答案**：第 5 路（`M05_AXI`）。PS 经 M_AXI_GP1 → `axi_interconnect_1` → M05_AXI 访问它（参见 u2-l3 的寄存器通路），具体地址由 BD 的地址映射决定。

### 4.4 打包集成差异：pre_def 的「追加模式」

#### 4.4.1 概念说明

这是本讲最重要、也最容易踩坑的一处差异。在 u1-l4 我们知道，`ip_repo_gen.tcl` 会循环打包六个 IP（`openofdm_rx openofdm_tx rx_intf tx_intf xpu side_ch`）。对**其余五个自研 IP**，打包前会把一批「板级配置文件」（`board_def.v`、`clock_speed.v`、`spi_command.v`、`fpga_scale.v`、`has_side_ch_flag.v`、`openwifi_hw_git_rev.v`）以及当次生成的 `_pre_def.v` **复制进它们的 `src/` 目录**，然后再打包——因为这些 IP 的源码就在本仓库，覆盖它们是安全的。

但 `openofdm_rx` 不一样：

1. 它的源码是**外部子模块**，`ip/openofdm_rx/src/` 里已经自带了它自己的 `openofdm_rx_pre_def.v`（其中含仿真用的 `SAMPLE_FILE` 等定义，README 第 142 行有说明）。如果像别的 IP 那样把本仓库生成的 `pre_def.v` **覆盖**过去，就会抹掉子模块自带的定义。
2. 因此 `ip_repo_gen.tcl` 对 `openofdm_rx` 走两个特例：① 打包前**不复制**任何板级配置文件进它的 `src/`；② 打包后，把本仓库生成的板级 `pre_def.v` 用 **`cat >>`（追加）** 的方式拼到它自带 `pre_def.v` 的末尾。

这个「追加而非覆盖」的写法，正是仓库近期提交 `b6a3231` 修复的：`Change new to append mode for _pre_def.v: to avoid overwrite previous defines`——把 `>` 改成 `>>`，避免覆盖 `openofdm_rx` 之前的宏定义。

#### 4.4.2 核心流程

打包循环里对每个 IP 的处理逻辑（伪代码）：

```text
for ip_name in [openofdm_rx, openofdm_tx, rx_intf, tx_intf, xpu, side_ch]:
    确保存在 ip_config/<ip_name>_pre_def.v        # 板级条件编译宏（BOARD_NAME、可选 DEF）
    if ip_name != openofdm_rx:                     # ← 自研 IP：覆盖式同步板级配置
        把 board_def.v / clock_speed.v / spi_command.v / fpga_scale.v /
            has_side_ch_flag.v / openwifi_hw_git_rev.v / <pre_def.v>
            复制进 ip/<ip_name>/src/
    source package_ip_complex.tcl 打包该 IP 到 ip_repo/<ip_name>/
    if ip_name == openofdm_rx:                     # ← 外部子模块：追加式注入板级宏
        cat  ip_config/openofdm_rx_pre_def.v >> ip_repo/openofdm_rx/src/openofdm_rx_pre_def.v
```

注意两个「特例分支」的方向相反：自研 IP 是「**打包前覆盖源**」，`openofdm_rx` 是「**打包后追加到产物**」。这样 `openofdm_rx` 既保留了自己子模块里的全部定义，又获得了本仓库的 `BOARD_NAME` 等宏。

`_pre_def.v` 本身是哪来的？它由 `boards/create_ip_repo.sh` 根据命令行参数生成（见 4.4.3）：默认写入 `` `define <BOARD_NAME> ``，若调用时带了 `openofdm_rx <DEF>`，则追加 `` `define OPENOFDM_RX_<DEF> ``。

#### 4.4.3 源码精读

打包列表与循环（[boards/ip_repo_gen.tcl:72-75](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L72-L75)），注意 `openofdm_rx` 排在第一个：

```tcl
set ip_name_list "openofdm_rx openofdm_tx rx_intf tx_intf xpu side_ch"
# loop and generate all ip
set i 0
foreach ip_name $ip_name_list {
```

「自研 IP 打包前覆盖源、`openofdm_rx` 跳过」的判断（[boards/ip_repo_gen.tcl:81-89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L81-L89)）：

```tcl
if {$ip_name != "openofdm_rx"} {
    exec cp ./ip_repo/openwifi_hw_git_rev.v ../../ip/$ip_name/src/ -f
    exec cp ./ip_repo/board_def.v            ../../ip/$ip_name/src/ -f
    exec cp ./ip_repo/clock_speed.v          ../../ip/$ip_name/src/ -f
    exec cp ./ip_repo/spi_command.v          ../../ip/$ip_name/src/ -f
    exec cp ./ip_repo/fpga_scale.v           ../../ip/$ip_name/src/ -f
    exec cp ./ip_repo/has_side_ch_flag.v     ../../ip/$ip_name/src/ -f
    exec cp ./ip_config/$ip_name\_pre_def.v  ../../ip/$ip_name/src/ -f
}
```

打包后的「追加」（注意是 `>>`，[boards/ip_repo_gen.tcl:93-95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L93-L95)）：

```tcl
if {$ip_name == "openofdm_rx"} {
    exec cat ./ip_config/$ip_name\_pre_def.v >> ./ip_repo/$ip_name/src/$ip_name\_pre_def.v
}
```

而 `_pre_def.v` 的内容由 `create_ip_repo.sh` 决定。它先为每个 IP 写一行 `` `define <BOARD_NAME> ``（[boards/create_ip_repo.sh:42-49](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L42-L49)）：

```bash
IP_NAME_ALL="xpu tx_intf rx_intf openofdm_tx openofdm_rx side_ch"
for IP_NAME in $IP_NAME_ALL ; do
    filename_to_write=ip_config/$IP_NAME"_pre_def.v"
    echo "//Naming pre_def.v differently for all IPs." > $filename_to_write
    ...
    echo "\`define $BOARD_NAME" >> $filename_to_write
done
```

如果命令行带了 `openofdm_rx <DEF>`，则解析时把它转成 `` `define OPENOFDM_RX_<DEF> `` 追加进去（[boards/create_ip_repo.sh:54-71](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L54-L71)，`openofdm_rx` 在允许名单里）。

README 还点出 `openofdm_rx` 的一个仿真特例：当用 `create_vivado_proj.sh` 单独建工程时，它的第 3 个参数不是普通宏，而是仿真用的 `SAMPLE_FILE`（[ip/create_vivado_proj.sh:15](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/create_vivado_proj.sh#L15)）：

```text
- the 3rd exception: in the case of openofdm_rx, it indicates SAMPLE_FILE for simulation.
  Can be changed later in openofdm_rx_pre_def.v
```

#### 4.4.4 代码实践（命令实操型，待本地验证）

**目标**：观察「追加模式」实际产生的 `_pre_def.v`，验证它确实保留了子模块自带定义、又叠加了板级宏。

**步骤**：

1. 在某板卡目录执行一次完整打包（前置：已运行 `get_ip_openofdm_rx.sh`）：
   ```bash
   cd openwifi-hw/boards/<BOARD_NAME>
   ../create_ip_repo.sh $XILINX_DIR
   ```
2. 打包完成后，查看 `boards/<BOARD_NAME>/ip_repo/openofdm_rx/src/openofdm_rx_pre_def.v` 的内容。
3. 对比 `boards/<BOARD_NAME>/ip_config/openofdm_rx_pre_def.v`（本仓库生成、被追加的那部分），确认前者的末尾多了后者。
4. 作为对照，再看一个自研 IP（例如 `ip_repo/xpu/src/xpu_pre_def.v`），观察它是否被本仓库的板级文件直接覆盖。

**需要观察的现象**：`openofdm_rx_pre_def.v` 里**同时**含有子模块自带的内容（如 `SAMPLE_FILE` 相关定义）和末尾的 `` `define <BOARD_NAME> ``；自研 IP 的 `_pre_def.v` 则是「覆盖式」的单一内容。

**预期结果**：你能指出「追加模式」保全了子模块原定义，这是它区别于其他五个 IP 的关键。若本地无 Vivado 环境无法实跑，则改为阅读 `ip_repo_gen.tcl` 第 81–95 行与 `create_ip_repo.sh` 第 42–71 行，口述每一步产物，并标注「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：如果 `ip_repo_gen.tcl` 第 94 行误把 `>>` 写成 `>`，会对 `openofdm_rx` 造成什么后果？
  - **答案**：`>` 会覆盖（截断重写）`ip_repo/openofdm_rx/src/openofdm_rx_pre_def.v`，抹掉子模块自带的全部宏定义（包括仿真用的 `SAMPLE_FILE` 等），导致仿真或编译行为异常。这正是提交 `b6a3231` 要修复的问题。
- **练习 2**：为什么对 `openofdm_rx`「打包前不复制 `board_def.v` 等文件」，而自研 IP 却要复制？
  - **答案**：自研 IP 的源码在本仓库，需要这些板级常量（采样率、时钟、规模开关）才能正确编译，且覆盖是安全的。`openofdm_rx` 是外部子模块，有自己的源码组织与配置文件，盲目复制会破坏其内容；它需要的板级信息（如 `BOARD_NAME`）改用「追加 pre_def」的方式注入。

## 5. 综合实践

把本讲三处知识点串起来，完成一次「`openofdm_rx` 集成走查」：

1. **来源**：打开 `.gitmodules` 与 README 第 194 行，写出 `openofdm_rx` 的上游仓库、分支与挂载路径。
2. **填充**：解释克隆后 `ip/openofdm_rx/` 为何是空的，并说明用哪个脚本、哪两条 `git` 命令填充它。
3. **接口**：从 `ip/openwifi_ip.tcl` 整理出 `openofdm_rx` 的输入（样点）、输出（字节/FCS/包头/RSSI）与寄存器口（`s00_axi` 经 M05_AXI），并标注哪些输出「一拖二」同时送给 `rx_intf` 和 `xpu`。
4. **打包差异**：在 `ip_repo_gen.tcl` 里找到两处针对 `openofdm_rx` 的特例分支，解释「打包前不覆盖源 / 打包后追加 pre_def」的原因，并说明 `>>` 之所以不能写成 `>` 的道理。

**交付物**：一张「`openofdm_rx` 集成笔记」，包含上游来源、填充命令、对外端口表、打包特例说明四部分。完成后，你就掌握了 openwifi 接收链路 PHY 核与本仓库的「接缝」。

> 进阶可选：若有 Vivado 环境，按 README「Simulate IP cores」小节用 `create_vivado_proj.sh $XILINX_DIR openofdm_rx.tcl` 建工程，跑 `dot11_tb` 行为级仿真，在波形里沿 `dot11_inst → ofdm_decoder_inst → viterbi_inst` 观察一个完整接收过程（待本地验证）。

## 6. 本讲小结

- `openofdm_rx` 是 openwifi 接收链路的 OFDM 物理层核，完成包检测、同步、FFT、信道均衡、解映射与 Viterbi 译码，输出字节流、`fcs_ok`、`pkt_rate/len` 等；它不是本仓库自研，而是源自上游 openofdm 项目的 `dot11zynq` 分支。
- 它以 git 子模块形式挂在 `ip/openofdm_rx`，克隆后为空，需运行一次 `get_ip_openofdm_rx.sh`（内部 `git submodule init/update`）填充。
- 它的对外端口由 `ip/openwifi_ip.tcl` 固定：样点从 `rx_intf` 输入，字节与状态「一拖二」同时送给 `rx_intf`（拼包上报）和 `xpu`（MAC 解析/过滤），寄存器口经 `M05_AXI` 暴露给 PS。
- 打包时它与五个自研 IP 处理方式不同：`ip_repo_gen.tcl` 对它「打包前不覆盖源、打包后用 `cat >>` 追加板级 `_pre_def.v`」，以保留子模块自带定义——这是提交 `b6a3231` 修复的关键点。
- 单独建仿真工程时，它的第 3 个参数是仿真用的 `SAMPLE_FILE`，而非普通条件编译宏。
- 接收链路的「口岸」（`rx_intf`，u3-l1/u3-l2）与「PHY 核」（本讲 `openofdm_rx`）到此闭合：样点进去、字节出来，由 `xpu` 调度。

## 7. 下一步学习建议

- **向控制面延伸**：本讲看到 `openofdm_rx` 的字节与包头大量送给 `xpu`，建议进入 **u5-l1（xpu 控制核心总览）**，看 `xpu` 如何消费 `byte_in`/`pkt_header_valid` 做 MAC 头解析与地址过滤（u5-l4）。
- **向发射面对照**：接收用的 `openofdm_rx` 是外部子模块，而发射用的 `openofdm_tx` 是本仓库自研。进入 **u4-l1（openofdm_tx 总览）** 对照阅读，体会「收（外购子模块）vs 发（自研）」的设计取舍。
- **向二次开发延伸**：若你想修改/升级 `openofdm_rx`，需要掌握单 IP 工程与条件编译机制，建议接着读 **u7-l2（条件编译与 Verilog 宏体系）** 与 **u7-l3（IP 仿真与 testbench 实践）**——后者正好以 `openofdm_rx` 的 `dot11_tb` 仿真为例。
- **深入 PHY 算法**：若想真正读懂 OFDM 接收机内部，需要切换到上游 [open-sdr/openofdm](https://github.com/open-sdr/openofdm) 的 `dot11zynq` 分支，从 `dot11.v` 顶层出发，逐级进入 `ofdm_decoder`、`viterbi` 等模块（超出本仓库范围，建议作为独立子项目学习）。
