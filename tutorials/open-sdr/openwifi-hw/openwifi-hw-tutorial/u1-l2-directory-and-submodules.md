# 目录结构、子模块与软硬件边界

> 本讲承接 [u1-l1 项目总览](./u1-l1-project-overview.md)。上一篇我们弄清了「openwifi 是什么」「openwifi-hw 在系统中的角色」和「授权模式」。这一篇我们把仓库「拆开」:看清楚每个目录装的是什么、两个 git 子模块指向哪里,以及哪些代码属于 FPGA 侧(PL)、哪些属于 ARM 侧(PS)。

## 1. 本讲目标

学完本讲,你应该能够:

1. 画出 `openwifi-hw` 仓库的顶层目录树,说出每个目录/脚本的作用。
2. 列出 `ip/` 下的 **6 个自定义 WiFi IP 核心**目录,并各自用一句话说明职责。
3. 说清 `adi-hdl` 与 `ip/openofdm_rx` 两个 git 子模块分别来自哪个仓库、为什么需要它们、以及为什么克隆后这两个目录是空的。
4. 区分 **PL(FPGA 硬件)** 与 **PS(ARM 软件)** 的代码边界——知道本仓库里哪些东西最终会变成 bitstream,哪些不会。

## 2. 前置知识

在上一篇已经建立的概念基础上,这里补充几个看目录时必须先懂的词:

- **子模块(git submodule)**:一个 git 仓库里「嵌入」了另一个 git 仓库。子模块只记录「对方仓库的地址」和「停在哪个 commit」,并不会在你 `git clone` 主仓库时自动把对方的内容下载下来——所以克隆后子模块目录常常是空的,需要单独初始化。本仓库有**两个**子模块,这是本讲的第二个核心模块。
- **PL / PS**:这是 Xilinx Zynq 系列 SoC 的术语。**PL(Programmable Logic)**=FPGA 可编程逻辑,跑的是用 Verilog 写的硬件电路,最终编译成 bitstream;**PS(Processing System)**=芯片里的 ARM CPU 核,跑 Linux 和驱动程序。本仓库(`openwifi-hw`)的产物几乎全是 **PL 侧**的,PS 侧的驱动代码在另一个仓库 `openwifi`。
- **IP 核心(IP core)**:在 FPGA 设计里,「IP」指一个可复用的、封装好的硬件模块,就像软件里的「库」。openwifi 的物理层就是由 6 个自定义 IP 拼起来的。
- **BOARD_NAME**:贯穿整个构建流程的环境变量(如 `zc706_fmcs2`、`antsdr`),决定使用哪块板卡的工程。上一篇已经介绍过它,本讲会看到它在目录结构上的体现——`boards/` 下每个板卡一个子目录。

## 3. 本讲源码地图

本讲涉及的文件都很短,但它们是理解仓库全貌的「地图」:

| 文件 | 作用 | 本讲用来证明什么 |
| --- | --- | --- |
| `README.md` | 项目主页说明 | 确认软硬件关系、6 个 IP 名单、两个子模块的来源 |
| `.gitmodules` | git 子模块配置文件 | 证明 `adi-hdl` 与 `ip/openofdm_rx` 是子模块及其 URL |
| `ip/board_def.v` | 板级公共宏定义 | 证明 `ip/` 下除了 IP 目录还有「跨 IP 共享」的配置文件 |
| `get_ip_openofdm_rx.sh` | 拉取 openofdm_rx 的脚本 | 证明子模块需要被单独初始化 |
| `LICENSE` / `CONTRIBUTING.md` | 授权与贡献协议 | 上一篇已讲,本讲仅作为目录树的一部分提及 |

## 4. 核心概念与源码讲解

### 4.1 目录结构

#### 4.1.1 概念说明

打开一个你不熟悉的项目,第一步永远是「看目录」。openwifi-hw 的顶层目录可以分为三类:

1. **说明与授权文件**:`README.md`、`LICENSE`、`CONTRIBUTING.md`、几张图片。
2. **顶层脚本**:负责准备依赖、拉取子模块、导出镜像。
3. **两大核心目录**:`ip/`(WiFi 硬件逻辑)与 `boards/`(板卡工程与构建脚本)。

理解目录的关键,是先建立「这个仓库最终要产出什么」的直觉。README 开篇一句话就把边界划清楚了:

> This repository includes Hardware/FPGA design. To be used together with **openwifi** repository (driver and software tools).

也就是说:**本仓库只产硬件设计(PL 侧),软件/驱动(PS 侧)在另一个仓库 `openwifi`**。产物不是可执行程序,而是 FPGA 镜像 `.xsa`/`.ltx`(见 [README.md:20](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L20) 与 [README.md:24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L24))。这条边界决定了:本仓库里几乎所有 `.v`(Verilog)、`.tcl`(Vivado 脚本)、`.bd`(block design)都属于 PL 侧;你找不到 Linux 驱动的 C 代码,因为那在 `openwifi` 仓库。

#### 4.1.2 核心流程(目录如何组织)

下面是仓库的真实目录树(以关键内容为准,省略图片等):

```
openwifi-hw/
├── README.md / LICENSE / CONTRIBUTING.md       # 说明、授权、贡献协议
├── adi-hdl/                                      # 【子模块】ADI HDL 参考设计(克隆后默认为空)
├── prepare_adi_lib.sh                            # 准备 ADI HDL 库(只跑一次)
├── prepare_adi_board_ip.sh                       # 准备 ADI 板级 IP(每块板跑一次)
├── get_ip_openofdm_rx.sh                         # 初始化 ip/openofdm_rx 子模块
├── gpio_led.md                                   # 各板卡 LED/GPIO 到 FPGA 信号的映射表
│
├── boards/                                       # 板卡工程 + 顶层构建脚本
│   ├── openwifi.tcl                              #   顶层 Vivado 工程脚本(建工程入口)
│   ├── create_ip_repo.sh / ip_repo_gen.tcl       #   把各 IP 打包成 ip_repo
│   ├── package_ip.tcl / package_ip_complex.tcl   #   单个 IP 打包
│   ├── sdk_update.sh                             #   导出 .xsa/.ltx 给软件仓库
│   ├── zc706_fmcs2/  zed_fmcs2/  zc702_fmcs2/    #   ← 每个 BOARD_NAME 一个工程目录
│   ├── adrv9361z7035/ adrv9364z7020/ zcu102_fmcs2/
│   ├── antsdr/  antsdr_e200/  e310v2/  sdrpi/
│   └── neptunesdr/
│
└── ip/                                           # 自定义 WiFi IP 核心 + 集成脚本
    ├── board_def.v                               #   跨 IP 共享的板级宏(采样率/时钟)
    ├── openwifi_ip.tcl / openwifi_ip_ultra_scale.tcl      # 把 6 个 IP 拼成 openwifi_ip 层级
    ├── connect_openwifi_ip.tcl / ..._ultra_scale.tcl      # 把 openwifi_ip 接到 Zynq PS
    ├── parse_board_name.tcl / create_vivado_proj.sh       # 板名解析 / 单 IP 建工程
    │
    ├── xpu/          # ① 控制核心 / 低层 MAC(CSMA/CA、重传、TSF、解析过滤)
    ├── tx_intf/      # ② 发射接口(DMA→BRAM→DAC)
    ├── rx_intf/      # ③ 接收接口(ADC→openofdm_rx→DMA)
    ├── openofdm_tx/  # ④ OFDM 发射机(编码/交织/IFFT/调制)
    ├── openofdm_rx/  # ⑤ OFDM 接收机【子模块,来自 openofdm】
    └── side_ch/      # ⑥ 侧信道(CSI/RSSI/IQ 捕获,可观测性)
```

读这张图的方法:

- **`ip/` 下有 6 个 IP 核心目录**,它们才是 WiFi 物理层的本体。这 6 个名字不是我们自己数的,README 在讲条件编译时白纸黑字列出了合法名单:「only xpu/tx_intf/rx_intf/openofdm_tx/openofdm_rx/side_ch are allowed」(见 [README.md:150](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L150))。
- **`ip/` 里还有「不在 6 个 IP 名单内」的文件**(`board_def.v`、若干 `.tcl`、`create_vivado_proj.sh`),它们是跨 IP 共享的「胶水」:配置、拼接、打包。
- **`boards/` 下每个板卡一个目录**(`zc706_fmcs2/`、`antsdr/` 等),对应 README 里 `BOARD_NAME` 表的每一行;板级工程入口是 `boards/openwifi.tcl`。
- 顶层那几个 `prepare_*.sh` / `get_*.sh` 是「一次性准备依赖」的脚本,构建前才用。

#### 4.1.3 源码精读

**① 软硬件边界的「官方说法」**

[README.md:20](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L20)(英文,译文见上)说明本仓库是硬件设计,需配合 `openwifi` 软件仓库使用。这是 PL/PS 边界最直接的证据:**FPGA 逻辑在此,驱动在彼**。

**② 6 个 IP 名单的铁证**

[README.md:150](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L150) 在 `create_ip_repo.sh` 的用法说明里写道:

```
-IP_NAME: only xpu/tx_intf/rx_intf/openofdm_tx/openofdm_rx/side_ch are allowed
```

这一行同时干了两件事:确认了 6 个 IP 的确切名字,也告诉我们这 6 个名字是构建脚本里**硬编码的合法集合**——你不能随便造一个新名字塞进去。

**③ `ip/` 不只是 IP 目录——还有共享配置 `board_def.v`**

[board_def.v:1-13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L1-L13) 是 `ip/` 根目录下的一个 Verilog 文件,但它**不属于任何一个 IP**,而是被所有 IP `include` 的公共板级定义:

```verilog
// board specific definitions
// clock_speed.v has NUM_CLK_PER_US. The value is determined by .tcl (_high.tcl or _low.tcl)
//`define NUM_CLK_PER_US         250 // 250MHz clock for ultrascale+ FPGA
//`define NUM_CLK_PER_US         200 // 200MHz clock for fast FPGA ...
//`define NUM_CLK_PER_US         100 // 100MHz clock for slow FPGA ...

`define SAMPLING_RATE_MHZ       20
`define ASSUMED_COUNTER_CLK_MHZ 10
`define NUM_CLK_PER_SAMPLE     ((`NUM_CLK_PER_US)/`SAMPLING_RATE_MHZ)
`define COUNT_TOP_1M           ((`NUM_CLK_PER_US)-1)
`define COUNT_SCALE            ((`NUM_CLK_PER_US)/(`ASSUMED_COUNTER_CLK_MHZ))
```

读这段要知道:第一行注释 `board specific definitions` 说明它是「板级相关定义」;`SAMPLING_RATE_MHZ` 固定为 20(baseband 20MHz 采样,802.11 a/g/n 的典型基带速率);而 `NUM_CLK_PER_US`(每微秒多少个时钟,即基带时钟频率)由 `.tcl` 脚本在构建时注入,可选 250/200/100(对应不同 FPGA 等级)。

这里引出一个跨 IP 共享的事实:**采样率和时钟是全局参数,所以放在 `ip/` 根目录共享给所有 IP**,而不是每个 IP 各写一份。(这些宏的精确含义和换算我们留到 [u2-l4 板级配置与时钟体系](./u2-l4-board-config-clock.md) 详解,本讲只需知道「`ip/` 下有这种共享文件」。)

> 小提示:`NUM_CLK_PER_SAMPLE = NUM_CLK_PER_US / 20`。当 `NUM_CLK_PER_US = 100`(100MHz)时,`NUM_CLK_PER_SAMPLE = 5`,即「每个 20MHz 的采样点占 5 个时钟周期」。这是 PL 侧时序的基础,PS 侧驱动并不直接关心它。

#### 4.1.4 代码实践:画出目录树并标注 6 个 IP

**实践目标**:用真实命令核对本讲的目录树,亲手标出 6 个自定义 IP,建立「看图—对代码」的习惯。

**操作步骤**:

1. 在仓库根目录运行(只读命令):

   ```bash
   ls -F                          # 看顶层
   ls -F ip/                      # 看 ip/ 下有哪些目录与文件
   ls -F boards/                  # 看有哪些板卡工程目录
   ```

2. 对照本讲 4.1.2 的目录树,在纸上(或笔记里)把它默画一遍,并在 `ip/` 下的 6 个目录旁写上一句话职责。
3. 用一条命令一次性把 6 个 IP 核对出来:

   ```bash
   ls -d ip/xpu ip/tx_intf ip/rx_intf ip/openofdm_tx ip/openofdm_rx ip/side_ch
   ```

**需要观察的现象**:

- `ls -F ip/` 的输出里,既有以 `/` 结尾的目录(6 个 IP + `openofdm_rx`),也有不以 `/` 结尾的文件(`board_def.v`、若干 `.tcl`、`create_vivado_proj.sh`)。
- `ls -F boards/` 里能看到与 README `BOARD_NAME` 表几乎一一对应的板卡目录(如 `zc706_fmcs2`、`antsdr`、`zcu102_fmcs2` 等)。

**预期结果**:`ls -d ip/xpu ip/tx_intf ...` 应当全部存在、无报错,6 个目录都列得出来——这就是「6 个自定义 IP」的客观依据。

> 说明:本沙箱里 `git submodule status` 这类命令可能需要额外授权;若无法运行,直接读 `.gitmodules` 也能确认子模块清单(见 4.2)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `boards/` 下要为每块板卡建一个单独的子目录,而不是像 `ip/` 那样把所有东西摊在一层?

> **答案**:因为 `ip/` 里的 6 个 IP 对所有板卡是**共用**的(物理层算法一样),而每块板卡的 FPGA 芯片型号、引脚约束(`system.xdc`)、block design(`system.bd`)、射频前端连接都不同,所以必须按 `BOARD_NAME` 分目录存放板级差异。

**练习 2**:`ip/board_def.v` 属于 6 个 IP 中的哪一个?

> **答案**:它**不属于任何一个 IP**。它放在 `ip/` 根目录,是被所有 IP 共享 `include` 的公共板级宏定义。这提醒我们:`ip/` 目录 = 6 个 IP 子目录 + 跨 IP 共享的配置/脚本文件。

**练习 3**:在本仓库里能找到 Linux Wi-Fi 驱动的 C 源码吗?为什么?

> **答案**:找不到。本仓库(`openwifi-hw`)只产 **PL 侧**的 FPGA 硬件设计,驱动属于 **PS 侧**,在另一个仓库 `openwifi` 里(README:20 明确说明「To be used together with openwifi repository」)。

---

### 4.2 git 子模块

#### 4.2.1 概念说明

本仓库不「从零发明一切」,它站在两个巨人的肩膀上,而这两个巨人就是以 **git 子模块**形式引入的:

| 子模块路径 | 来源仓库 | 它提供什么 |
| --- | --- | --- |
| `adi-hdl/`(在顶层) | `https://github.com/analogdevicesinc/hdl.git` | Analog Devices 的 HDL 参考设计:AD9361 射频前端的数据通路、AXI DMA、时钟等基础设施 |
| `ip/openofdm_rx/`(在 ip 下) | `https://github.com/open-sdr/openofdm.git` | OFDM 接收机(包检测、同步、FFT、均衡、Viterbi),来自 openofdm 项目的 `dot11zynq` 分支 |

为什么用子模块而不是把代码复制进来?因为这两个上游项目都在持续更新,openwifi 只是「在它们之上做增量」。README 在结尾两段加粗声明里说得很直白:

- openwifi 是在 **Analog Devices HDL 参考设计**之上加了必要的模块/修改(见 [README.md:192](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L192))。
- OFDM 接收机基于 **openofdm 项目**,改进放在 openofdm fork 的 `dot11zynq` 分支,映射到 `ip/openofdm_rx`(见 [README.md:194](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L194))。

#### 4.2.2 核心流程(子模块的生命周期)

子模块有一个初学者最容易踩的坑:**克隆主仓库后,子模块目录是空的**。它的生命周期是:

```text
git clone openwifi-hw          # 主仓库下来,但 adi-hdl/ 与 ip/openofdm_rx/ 是空目录
        │
        ├── adi-hdl/            → 需要: git submodule update --init adi-hdl
        │                         (或经 prepare_adi_lib.sh 间接使用其库)
        │
        └── ip/openofdm_rx/     → 需要: ./get_ip_openofdm_rx.sh
                                  (内部就是 git submodule init/update openofdm_rx)
```

- `.gitmodules` 只声明「子模块在哪、地址是什么」,不下载内容。
- 真正把内容拉下来,要执行子模块初始化命令。openwifi 为 `openofdm_rx` 准备了专门的脚本 `get_ip_openofdm_rx.sh`;`adi-hdl` 则在 `prepare_adi_lib.sh` 流程中被用到。
- 因此,如果你的 `adi-hdl/` 或 `ip/openofdm_rx/` 是空的,**不是仓库坏了**,而是子模块还没初始化。

#### 4.2.3 源码精读

**① `.gitmodules`——两个子模块的「身份证」**

整个文件只有 6 行,定义了两个子模块(见 [.gitmodules:1-6](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/.gitmodules#L1-L6)):

```ini
[submodule "adi-hdl"]
    path = adi-hdl
    url = https://github.com/analogdevicesinc/hdl.git
[submodule "ip/openofdm_rx"]
    path = ip/openofdm_rx
    url = https://github.com/open-sdr/openofdm.git
```

注意两个细节:第一,`adi-hdl` 的 `path` 是顶层目录,而 `ip/openofdm_rx` 的 `path` 嵌在 `ip/` 下——子模块可以放在仓库的任意位置。第二,`url` 指向的是上游真实仓库,这就是「openwifi 站在巨人肩膀上」的字面证据。

**② `get_ip_openofdm_rx.sh`——子模块为什么需要单独初始化**

[get_ip_openofdm_rx.sh:6-8](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_ip_openofdm_rx.sh#L6-L8) 的核心就两条命令:

```bash
cd ip/
git submodule init openofdm_rx
git submodule update openofdm_rx
```

`init` 把 `.gitmodules` 里关于 `openofdm_rx` 的配置登记到本地 `.git/config`,`update` 才真正按记录的 commit 把内容拉下来。注释掉的 `git checkout dot11zynq` / `git pull origin dot11zynq`(见 [get_ip_openofdm_rx.sh:9-11](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_ip_openofdm_rx.sh#L9-L11))暗示了 openofdm 使用的分支是 `dot11zynq`,与 README:194 的说明一致。

**③ README 里的子模块使用入口**

- `adi-hdl` 经 `prepare_adi_lib.sh` 进入构建流程(见 [README.md:58-63](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L58-L63) 的「Prepare Analog Devices HDL library」)。
- `openofdm_rx` 经 `get_ip_openofdm_rx.sh` 进入 `ip/` 目录(见 [README.md:71-74](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L71-L74))。

#### 4.2.4 代码实践:确认两个子模块

**实践目标**:用两种方式确认仓库依赖的两个子模块,并亲手体会「子模块默认是空的」。

**操作步骤**:

1. 方式 A——读配置文件(最可靠,不依赖 git 状态):

   ```bash
   cat .gitmodules
   ```

2. 方式 B——用 git 命令(若环境允许):

   ```bash
   git submodule status
   ```

3. 检查这两个目录在「未初始化」时的真实状态:

   ```bash
   ls -A adi-hdl/            # 列出含隐藏文件
   ls -A ip/openofdm_rx/
   ```

**需要观察的现象**:

- `.gitmodules` 应输出 4.2.3 里那 6 行,明确列出 `adi-hdl` 与 `ip/openofdm_rx` 及其 URL。
- 若子模块未初始化,`git submodule status` 每行前缀是一个减号 `-`,且 commit hash 可能为空;`ls -A adi-hdl/` 几乎没有输出(空目录)。
- 执行 `./get_ip_openofdm_rx.sh` 后,`ip/openofdm_rx/` 才会被填充。

**预期结果**:确认存在两个子模块,来源分别是 `analogdevicesinc/hdl` 与 `open-sdr/openofdm`;并且理解「空目录 ≠ 仓库损坏,而是子模块待初始化」。

> 待本地验证:`git submodule status` 的精确输出格式取决于本机是否已 `init`/`update`;如果命令被环境拦截,以方式 A(`cat .gitmodules`)的结论为准。

#### 4.2.5 小练习与答案

**练习 1**:`adi-hdl` 和 `ip/openofdm_rx` 这两个子模块分别提供什么能力?

> **答案**:`adi-hdl`(Analog Devices HDL)提供 AD9361 射频前端的数据通路、AXI DMA、时钟等基础设施——它是「射频到基带」的底座;`ip/openofdm_rx`(openofdm)提供 OFDM 接收机的物理层算法(包检测、同步、FFT、均衡、Viterbi)。两者都是 openwifi「增量构建」在其上的上游项目。

**练习 2**:克隆 openwifi-hw 后,`ip/openofdm_rx/` 是空的,该怎么填充它?

> **答案**:在仓库根目录运行 `./get_ip_openofdm_rx.sh`。该脚本内部执行 `git submodule init openofdm_rx` 与 `git submodule update openofdm_rx`(见 get_ip_openofdm_rx.sh:6-8)。

**练习 3**:为什么这些第三方代码用子模块引入,而不是直接拷进仓库?

> **答案**:因为它们是独立维护、持续更新的上游项目,openwifi 只在它们之上做修改(README:192/194)。用子模块既能固定到某个经过验证的 commit,又能在上游更新时方便地跟踪,避免把第三方代码与本仓库改动混在一起、难以合并上游变更。

---

## 5. 综合实践

把本讲两个模块串起来,完成一份「仓库导览卡」:

1. **画一张目录树**:把 4.1.2 的树重新画一遍,但这次只凭记忆,画完再用 `ls -F ip/`、`ls -F boards/` 校对。
2. **标注 6 个 IP**:在树上的 `ip/` 下,为 `xpu`、`tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch` 各写一句中文职责,并特别标记 `openofdm_rx` 是子模块。
3. **画软硬件边界线**:用两种颜色(或左右两栏)把仓库内容分成「PL 侧(FPGA,会编进 bitstream)」和「PS 侧(ARM,本仓库没有)」。具体地:
   - 把 `ip/*`、`boards/*/src/*.v`、`boards/*/src/*.bd`、`*.tcl` 归入 PL 侧;
   - 在 PS 侧写上「驱动/软件 → 在 openwifi 仓库」,并标注本仓库产物 `.xsa`/`.ltx` 最终交给谁。
4. **核验子模块**:在导览卡上写下两个子模块的 `path` 与 `url`,以及「克隆后默认为空,需 `get_ip_openofdm_rx.sh` / 子模块初始化」这一提醒。

完成这张卡,你就建立了后续所有源码讲义的「空间坐标系」——后面讲 `rx_intf`、`xpu` 时,你都能立刻知道它们在树的哪个位置、和谁相连。

## 6. 本讲小结

- 仓库顶层 = 说明/授权文件 + 一次性准备脚本(`prepare_*.sh`、`get_ip_openofdm_rx.sh`) + 两大核心目录 `ip/` 与 `boards/`。
- `ip/` 下有且仅有 **6 个自定义 WiFi IP**:`xpu`、`tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch`(README:150 白名单确认);此外还有跨 IP 共享的 `board_def.v` 与若干 `.tcl`/脚本。
- `boards/` 下**每块板卡一个目录**(对应 `BOARD_NAME`),板级工程入口是 `boards/openwifi.tcl`。
- 仓库依赖两个 git 子模块:`adi-hdl`(顶层,来自 `analogdevicesinc/hdl`)和 `ip/openofdm_rx`(来自 `open-sdr/openofdm` 的 `dot11zynq` 分支),分别提供射频前端底座和 OFDM 接收机。
- **子模块默认不下载内容**,克隆后这两个目录是空的,需要 `./get_ip_openofdm_rx.sh`(`openofdm_rx`)或子模块初始化(`adi-hdl`)来填充。
- **软硬件边界**:本仓库是纯 PL(FPGA)侧设计,产物是 `.xsa`/`.ltx`;PS(ARM)侧的驱动在另一个仓库 `openwifi`(README:20)。

## 7. 下一步学习建议

- 想了解每块板卡的区别和构建环境要求,继续看 [u1-l3 支持的板卡与开发运行环境](./u1-l3-boards-and-environment.md)。
- 想动手走一遍「从脚本到 bitstream」的完整流程,看 [u1-l4 FPGA 构建全流程实战](./u1-l4-fpga-build-flow.md),那里会把本讲提到的 `prepare_*.sh`、`get_ip_openofdm_rx.sh`、`create_ip_repo.sh`、`openwifi.tcl`、`sdk_update.sh` 串成一条链。
- 想知道 6 个 IP 如何被拼成一个整体、`board_def.v` 里的时钟宏到底什么意思,进入第二单元的 [u2-l1 顶层 system_top 与 block design](./u2-l1-system-top-block-design.md) 与 [u2-l4 板级配置与时钟体系](./u2-l4-board-config-clock.md)。
