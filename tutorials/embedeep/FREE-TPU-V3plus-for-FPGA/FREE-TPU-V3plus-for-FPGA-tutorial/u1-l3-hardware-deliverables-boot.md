# 硬件交付物与上板运行

## 1. 本讲目标

上一讲我们建立了仓库的「全局地图」，知道 `hardware/` 目录里装着三个拿来即用的二进制文件：`BOOT.BIN`、`image.ub`、`system_wrapper.xsa`。本讲要回答的是一个非常具体的问题：

> **这三个文件分别是什么？把一块空白的 ZynqMP 板卡变成一个能在串口里敲命令的 Linux 系统，它们各扮演什么角色？**

读完本讲，你应该能够：

1. 说清楚 `BOOT.BIN`、`image.ub`、`xsa` 三者各自打包了什么内容、谁先生效、谁后生效。
2. 用一条「启动链」描述从上电到看到 Linux shell 的全过程，并标注每一步由哪个交付物负责。
3. 理解 `xsa` 不仅是「硬件包」，它还决定了软件里那些「魔法地址」（如 `0xA0000000`）从何而来——并用仓库里的真实源码**亲手验证**这一对应关系。
4. 知道详细的烧录与上板步骤应该去 `doc/` 下哪本手册查阅。

本讲不会教你具体敲哪条烧录命令（那需要真实板卡和手册），而是帮你**看懂这三个文件在系统中的位置**，这样后续读软件代码时，你脑中会有一张「软件地址 ↔ 硬件设计」的对照表。

## 2. 前置知识

本讲假设你已读过 [u1-l1](u1-l1-project-overview.md) 与 [u1-l2](u1-l2-directory-structure.md)，知道 FREE-TPU V3+ 跑在 Xilinx **ZynqMP** SoC 上（ARM PS + FPGA PL 同一颗芯片），并且知道 `hardware/` 里是「二进制交付物」。在此基础上，再补三个 ZynqMP 上板相关的概念：

- **启动镜像（Boot Image）/ BOOT.BIN**：ZynqMP 的片上 Boot ROM 上电后会去一个固定位置（通常是 SD 卡的第一个分区）找一个名为 `BOOT.BIN` 的文件。这个文件是 Xilinx 工具 `bootgen` 按 **BIF（Boot Image Format）** 打包出来的容器，里面可以塞好几样东西。把它理解成「上电后芯片吃进去的第一口饭」。
- **FSBL / SSBL**：
  - **FSBL**（First Stage Boot Loader，第一阶段加载器）是 BOOT.BIN 里最重要的一段程序，跑在 ARM 上，负责初始化 PS（DDR、时钟、外设引脚）、把 **bitstream** 加载到 PL（即「配置 FPGA」）、然后把第二阶段加载器拉起来。
  - **SSBL**（Second Stage Boot Loader）通常是 **U-Boot**，由 FSBL 加载，负责再加载 Linux 内核。所以启动是分两级的：Boot ROM → FSBL → U-Boot → Linux。
- **bitstream 与 xsa**：bitstream 是把 FPGA 设计「编译」出来的二进制配置数据，灌进 PL 后 PL 才变成你设计的电路（本仓库里就是 TPU + DVP 摄像头接口那一套）。**xsa** 是 Vivado 导出的硬件设计包，里面**包含 bitstream、PS 初始化代码、以及一张「地址映射表」**——软件工程师正是依据 xsa 来写驱动的。

> 一句话区分：`BOOT.BIN` 让板子**能启动**，`image.ub` 让板子**跑起 Linux**，`xsa` 给软件工程师**交代硬件长什么样**。

## 3. 本讲源码地图

本讲表面上讲三个二进制文件，但我们要用仓库里的**真实源码**去佐证它们的内容，避免空谈。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 官方对 `hardware` 目录的权威说明（"xsa file, prebuild BOOTbin"） |
| `script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl` | Vivado 块设计脚本——**xsa / bitstream 的「源头」**，描述 PL 里有哪些 IP、PS 怎么配、地址怎么分 |
| `sdk/standalone/src/config.h` | 裸机工程的地址与开关定义——**软件侧的地址**，可与 TCL 的地址分配一一对照 |
| `sdk/standalone/src/platform.c` | 裸机平台初始化（开缓存、串口），体现「FSBL 之后、应用 main 之前」做的事 |
| `sdk/standalone/src/main.cc` | 裸机主程序，体现上板后看到的菜单（启动链的终点之一） |
| `doc/demo_readme.pdf` | 上板运行的官方步骤手册（需本地打开阅读） |

`hardware/` 目录下实际的三个文件（可用 `git ls-files hardware` 确认）：

```text
hardware/
├── BOOTbin/
│   ├── BOOT.BIN                  （约 20 MB，启动镜像）
│   └── image.ub                  （约 14 MB，Linux 内核镜像）
└── xsa/
    └── system_wrapper.xsa        （约 6.5 MB，Vivado 硬件导出包）
```

## 4. 核心概念与源码讲解

### 4.1 BOOT.BIN：上电后芯片吃进去的第一口饭

#### 4.1.1 概念说明

`BOOT.BIN` 是 ZynqMP 的**启动镜像容器**。芯片里的 Boot ROM 是硬连线逻辑，上电后它只做一件确定的事：根据启动模式引脚（SD / QSPI / JTAG …）去对应介质找 `BOOT.BIN`，解析它的分区表，把第一个分区（FSBL）加载进 PS 的 RAM 并跳过去执行。

在标准的 PetaLinux / Vitis 工作流里，`BOOT.BIN` 通常打包三样东西：

1. **FSBL**：初始化 PS、加载 bitstream、加载 U-Boot。
2. **bitstream**（`.bit`）：把 PL 配置成 TPU 设计。
3. **U-Boot**（`u-boot.elf`）：第二阶段加载器，待会儿去加载 `image.ub`。

需要强调：这三段是 **ZynqMP 的标准组成**，不是我们凭空说的；但**本仓库这个具体的 `BOOT.BIN` 内部到底分了几个分区、各自多大**，属于二进制内部细节，单看文件名无法确认，需用 Xilinx 工具（如 `bootgen -dump`）或在手册中核实——下文涉及具体分区大小时都标注「待本地验证」。

#### 4.1.2 核心流程

```text
上电
 │
 ▼
Boot ROM（硬连线）──读启动模式引脚──► 找到 SD 卡上的 BOOT.BIN
 │
 ▼
解析 BOOT.BIN 分区表，加载并运行 FSBL
 │
 ├──► FSBL 初始化 PS（DDR/时钟/MIO）
 ├──► FSBL 把 bitstream 灌进 PL  ◄── 此时 PL 变成 TPU + DVP 设计
 └──► FSBL 加载并跳转到 U-Boot
```

也就是说，`BOOT.BIN` 把「让芯片从一无所知变成 PS+PL 都就绪、并交棒给 U-Boot」这一整段工作打包在了一起。它之所以有 ~20 MB，主要是 bitstream（ZU15EG 这种规模器件的 bitstream 通常十几 MB）占了大头。

#### 4.1.3 源码精读

README 对 `hardware` 目录的一句说明，点明了它装的就是「预编译好的 BOOTbin 和 xsa」：

[README.md:L55-L63](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L55-L63) —— 目录结构表里 `hardware` 一栏写作「xsa file, prebuild BOOTbin」，即这个目录提供两类上板交付物：预编译启动镜像与硬件导出包。

而 `BOOT.BIN` 里的 bitstream，源头正是 `script/` 下那一份 Vivado 块设计脚本。脚本开头的器件名和版本，决定了这份 bitstream 是为哪颗芯片、哪个 Vivado 版本生成的：

[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:L10-L15](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L10-L15) —— 工程名 `TPU_DVP_prj_N1_DP_2021`，目标器件 `xczu15eg-ffvb1156-2-i`（即 Zynq UltraScale+ **ZU15EG**），与上一讲 ip_repo 文件名里的 `ZU15EG` 相互印证。

[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:L30-L31](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L30-L31) —— 脚本声明它是在 **Vivado 2021.1** 下生成的，若用别的版本跑会被警告。这也提示：`hardware/BOOTbin/BOOT.BIN` 里的 bitstream 是 2021.1 工具链的产物。

#### 4.1.4 代码实践

1. **实践目标**：确认 BOOT.BIN / image.ub / xsa 三个文件的真实大小与存在性，建立量感。
2. **操作步骤**：在仓库根目录执行
   ```bash
   git ls-files hardware
   ls -lh hardware/BOOTbin hardware/xsa
   ```
3. **需要观察的现象**：`BOOT.BIN` 约 20 MB（最大，因含 bitstream），`image.ub` 约 14 MB（Linux 内核 + dtb + ramdisk），`xsa` 约 6.5 MB（硬件设计压缩包）。
4. **预期结果**：你能把三个文件按大小排出「bitstream 占大头 → Linux 镜像其次 → 硬件导出包最小」的顺序，这与 4.1.1 讲的组成一致。
5. 想进一步确认 BOOT.BIN 内部分区，需在装了 Xilinx 工具的环境执行 `bootgen -image <xxx.bif> -arch zynqmp -dump`，**待本地验证**（本仓库未提供 .bif 文件，分区细节以 `doc` 手册为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BOOT.BIN`（~20 MB）比 `image.ub`（~14 MB）还大？按本节讲的组成推测原因。

> **答案**：`BOOT.BIN` 里塞了一段十几 MB 的 bitstream（用来把 ZU15EG 这种较大规模的 PL 配置成 TPU 设计），再加 FSBL 和 U-Boot；而 `image.ub` 主要是 Linux 内核 + 设备树 + 小型根文件系统。两者内容完全不同，不能按大小简单类比。

**练习 2**：如果启动模式引脚设成 SD 卡启动，但 SD 卡里没有 `BOOT.BIN`，会发生什么？

> **答案**：Boot ROM 找不到启动镜像，无法加载 FSBL，板卡就停在 Boot ROM 阶段（通常会刷错误日志或进入回退流程），后面所有步骤都不会发生——这正是「第一口饭」没吃上的后果。

### 4.2 image.ub：Linux 内核的 U-Boot 包装

#### 4.2.1 概念说明

`image.ub` 是 **U-Boot 的 legacy image 格式**（`mkimage` 打包的 `FIT`/legacy image），名字里的 `.ub` 就暗示它是给 U-Boot 吃的。U-Boot 被 FSBL 拉起来后，会去 SD 卡上找这个文件，校验头部的 magic、把内核加载到 DDR、传启动参数，然后跳进内核。

一个典型的 `image.ub` 里往往不止内核本身，而是把三样东西打成一个包：

1. **Linux 内核**（`Image`，ARM64 的可执行内核）。
2. **设备树**（`.dtb`，描述「这颗板子上有哪些硬件、地址是多少」，内核靠它认识外设）。
3. **根文件系统 ramdisk**（可选，`rootfs.cpio.gz`，提供一个最小的初始根目录）。

为什么要把内核 + dtb 打成一个 `image.ub`？因为嵌入式板子往往没有 PC 那样的通用 BIOS/UEFI，U-Boot 需要一个「自带说明书」的镜像，设备树就和内核绑在一起走。

#### 4.2.2 核心流程

```text
U-Boot 启动
 │
 ▼
读 SD 卡上的 image.ub
 │
 ▼
解包：内核 Image + 设备树 dtb (+ ramdisk)
 │
 ├──► 把内核加载到 DDR 某地址
 ├──► 把 dtb 加载到 DDR 另一地址
 └──► 设置 bootargs（启动参数：根设备、console=ttyPS0 等），跳进内核
 │
 ▼
Linux 内核启动 → 挂载根文件系统 → 跑 init → 登录 shell
```

设备树里描述的外设，必须和硬件设计里 PS 实际打开的外设对上号。下面 4.2.3 我们就从 TCL 里看看这颗板子的 PS 打开了哪些外设——它们就是 Linux 启动后会认到的设备（串口、网口、USB、SD 卡等）。

#### 4.2.3 源码精读

TCL 脚本里有一长串 `PSU_MIO_TREE_PERIPHERALS`，把 PS 的每个 MIO 引脚分配给了某个外设。这一行虽然很长，却直接决定了 `image.ub` 里的设备树要描述哪些设备：

[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:L839-L845](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L839-L845) —— 这一长串把 MIO 引脚依次分给：**Quad SPI Flash**（存固件）、**CAN1**、**SD0 与 SD1**（两张 SD 接口，启动介质与数据盘）、**DPAUX**（DisplayPort 显示）、**UART0**（串口控制台）、**USB0**、**Gem3**（千兆以太网）等。这正好解释了：为什么上板后你能用串口（UART0）看到 Linux shell，也能插网线（Gem3）ssh 上去。

其中串口波特率在裸机侧的注释里也点过：

[sdk/standalone/src/platform.c:L68-L76](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/platform.c#L68-L76) —— 注释写明「Bootrom/BSP configures PS7/PSU UART to **115200 bps**」。所以连接串口工具时，波特率要设成 **115200**，否则看到的是乱码。Linux 侧的 `console=ttyPS0,115200` 与此一致（具体 bootargs 以手册为准，**待本地验证**）。

#### 4.2.4 代码实践

1. **实践目标**：从硬件设计反推「Linux 启动后会多出哪些设备节点」。
2. **操作步骤**：用只读方式查看 TCL 里被打开的外设：
   ```bash
   git show HEAD:script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl | grep -oE 'PSU__USB0|USB 0|Gem 3|UART 0|SD 0|SD 1|Quad SPI|DPAUX|CAN 1'
   ```
3. **需要观察的现象**：会看到 `USB 0`、`Gem 3`、`UART 0`、`SD 0`、`SD 1`、`Quad SPI`、`DPAUX` 等都被列出。
4. **预期结果**：你能在脑中预测——上板后 `ls /dev` 里大概率会出现 `ttyPS0`（串口）、`mmcblk0/1`（SD 卡）、`eth0`（Gem3 网口）、USB 相关设备。这是从硬件设计读出软件可见设备的好习惯。
5. 具体设备树内容（`*.dts`）本仓库未直接提供，它被打包进了 `image.ub`，**待本地验证**：可在板上的 `/boot/` 或 `/proc/device-tree` 查看实际设备树。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `image.ub` 里要把「设备树」和「内核」打包在一起，而不像 PC 那样分开？

> **答案**：嵌入式 SoC 的外设随硬件设计而变（这颗板子的 PS 打开了 UART0/Gem3/USB0，换块板可能完全不同），内核无法内置一份「通用说明书」。把针对本硬件设计的 dtb 与内核一起打包，能保证 U-Boot 加载内核时一并喂给它正确的硬件描述。

**练习 2**：串口连上去全是乱码，最可能的原因是什么？

> **答案**：波特率不对。这颗板的 UART 配成 115200 bps（见 platform.c 注释），串口工具若设成 9600 等其它值就会乱码。

### 4.3 xsa：给软件工程师的「硬件说明书」

#### 4.3.1 概念说明

`system_wrapper.xsa` 是 Vivado 导出的**硬件设计包**（Xilinx Software Archive）。它和 `BOOT.BIN` 的区别很关键：

- `BOOT.BIN` 是给**芯片**吃的（启动用）。
- `xsa` 是给**软件工具/工程师**看的（开发用）。

`xsa` 里至少包含三样东西：

1. **bitstream**（`.bit`）：和 BOOT.BIN 里那份一样，配置 PL 的电路。
2. **PS 初始化代码**（`psu_init.*`）：FSBL 正是用它来初始化 DDR、时钟、MIO 的。
3. **地址映射表**：PL 里每个 IP 的寄存器，被映射到了 PS 可见的哪个物理地址。

**第 3 点是本节的核心**：软件代码里那些看起来像「魔法数字」的地址（比如 `0xA0000000`），其实是硬件设计在 xsa 里**定死了**的。Vitis 在生成裸机工程时，会读 xsa 里的地址表，生成 `xparameters.h`；而本仓库的 `config.h` 则是把这套地址**人肉抄写**了一遍。下面我们就用源码证明这一点。

#### 4.3.2 核心流程

TCL 里用 `assign_bd_address` 命令把每个 IP 的寄存器空间挂到 PS 的地址空间上。对 TPU 而言有两条关键地址通路：

```text
(1) 寄存器通路（PS → TPU，软件用来「下命令」）：
   PS 的 M_AXI_HPM0_FPD  ──►  EEP_TPU_0/s00_axi/reg0
   映射到物理地址 0xA0000000，range 0x40000（256 KB）

(2) 数据通路（TPU → DDR，TPU 用来「搬张量」）：
   EEP_TPU_0/M00_AXI  ──►  PS 的 HP0（High-Performance）口  ──►  DDR
   映射到 DDR 偏移 0x0，range 0x80000000（低 2 GB）
```

寄存器地址是软件驱动的「门牌号」，数据通路则是 TPU 把权重/特征图放在 DDR 哪里都能访问到的「高速公路」。软件里 `EEPTPU_MEM_BASE_ADDR = 0x31000000` 选定的就是 DDR 低 2 GB 内的一块区域（\(0x31000000 < 0x80000000\)），交给 TPU 存网络权重与输入。

地址范围的对齐可以用一个简单式子表达。一个 IP 的寄存器段在 PS 地址空间里占据：

\[
[\,\text{offset},\ \text{offset}+\text{range}\,) = [\,0xA0000000,\ 0xA0040000\,)
\]

即 TPU 寄存器段占 \(\text{range}=0x40000=256\text{ KB}\)，而 `config.h` 里那些 `EEPTPU_*_REG` 宏用的偏移（`+0x50`、`+0x34`、`+0xC` 等）都落在这 256 KB 之内。

#### 4.3.3 源码精读

**这是本讲最关键的一处对照。** 先看 TCL 里给 TPU 寄存器段分配的地址：

[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:L1882-L1886](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L1882-L1886) —— 这几行是整份硬件设计的「地址分配结算单」：`EEP_TPU_0/M00_AXI` 通过 HP0 口映射到 DDR 低 2 GB（offset 0），`EEP_TPU_0/s00_axi/reg0` 映射到 **0xA0000000**，`EEP_DVP_Top_0/s00_axi/reg0`（摄像头接口寄存器）映射到 **0xA00C0000**，range 都是 0x40000。

再看软件侧 `config.h` 抄写的地址：

[sdk/standalone/src/config.h:L25-L35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L25-L35) —— `EEPTPU_REG_BASE_ADDR = 0xA0000000`、`EEPDVP_REG_BASE_ADDR = 0xA00C0000`，与上面 TCL 的 `assign_bd_address` **逐位相同**；下面的 `EEPTPU_STATUS_REG`（+0xC）、`EEPTPU_STARTUP_REG`（+0x34）、`EEPTPU_BASEADDR0_REG`（+0x50）等，都是在这段寄存器空间内的偏移。

> 这就是 `xsa` 与软件的真正接口：**硬件设计定了 0xA0000000，软件就老老实实用 0xA0000000**。如果你换了硬件设计（重新分配地址），`config.h` 这一串宏就得跟着改，否则软件写的命令会落到不存在的地址上。

至于数据通路，TPU 通过 HP0 口直接访问 DDR，所以 `EEPTPU_MEM_BASE_ADDR = 0x31000000` 这块「软件拿来放权重和输入」的 DDR 区域，TPU 自己也能用 M00_AXI 读到。这就把 4.1 讲的「bitstream 里是 TPU 设计」与「软件往 DDR 写数据」连成了一片。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「软件地址 = 硬件设计地址」，建立 xsa 与代码的对应感。
2. **操作步骤**：
   ```bash
   # 硬件侧（TCL 里的地址分配）
   git show HEAD:script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl | grep 'assign_bd_address'
   # 软件侧（config.h 里的基地址）
   git show HEAD:sdk/standalone/src/config.h | grep -E 'BASE_ADDR'
   ```
3. **需要观察的现象**：两边都会出现 `0xA0000000`（TPU 寄存器）和 `0xA00C0000`（DVP 寄存器）。
4. **预期结果**：你会得到一张「TCL assign_bd_address ↔ config.h 宏」的对照表，证明软件的魔法地址完全由硬件设计决定。
5. 进阶：思考一下，若把 TPU 寄存器段在 Vivado 里改映射到 `0xB0000000`，`config.h` 里哪几个宏必须改？（答：`EEPTPU_REG_BASE_ADDR` 一改，下面所有 `EEPTPU_*_REG` 因为是「基地址 + 偏移」会自动跟着走——这正是用基地址+偏移组织寄存器的好处。）

#### 4.3.5 小练习与答案

**练习 1**：`xsa` 和 `BOOT.BIN` 里都含 bitstream，它们是同一份吗？为什么要放两处？

> **答案**：通常是同一份 bitstream 的不同副本。`BOOT.BIN` 里的 bitstream 用于**上电时由 FSBL 灌进 PL**；`xsa` 里的 bitstream 是 Vivado 导出的开发交付物，供 Vitis 重建工程或软件调试时使用。两处都保留，是为了「能上板」和「能开发」各自独立。

**练习 2**：为什么 `EEPTPU_MEM_BASE_ADDR` 是 `0x31000000` 而不能随便写成 `0xA0000000`？

> **答案**：`0xA0000000` 是 TPU 的**寄存器段**（软件下命令用，仅 256 KB）；而 `EEPTPU_MEM_BASE_ADDR` 指向的是 **DDR 里存权重/输入的大块数据区**，必须落在 TPU 的 HP0 数据通路能访问的 DDR 低 2 GB（offset 0 ~ 0x80000000）内，且不能和寄存器段、内核镜像等占用冲突。`0x31000000` 是软件选定的一个安全空闲区域。

### 4.4 上板启动流程：把三个文件串成一条链

#### 4.4.1 概念说明

有了前三个模块，现在把 `BOOT.BIN`、`image.ub`、`xsa`（以及它们背后的 FSBL / bitstream / U-Boot / 内核）串成一条完整的启动链。本仓库其实提供**两条运行路线**（见 [u2-l1](u2-l1-sdk-overview.md)），它们的启动过程略有不同：

- **Linux 路线**：启动链一直走到 Linux 用户态，然后在 shell 里跑 demo（`classify`/`yolo` 等 ELF 程序）。
- **裸机（Bare Metal）路线**：FSBL 配好 PL 后，不跑 Linux，而是直接跑 `standalone/src/main.cc` 编译出来的裸机 ELF。

两条路线的前半段（Boot ROM → FSBL → 加载 bitstream）是相同的，区别在于「FSBL 之后交给谁」。

#### 4.4.2 核心流程

```text
┌──────────────────────────────────────────────────────────────────────┐
│  1. 上电：Boot ROM 按启动模式引脚，从 SD 卡读 BOOT.BIN               │
│  2. Boot ROM 加载并运行 BOOT.BIN 里的 FSBL                            │
│  3. FSBL：初始化 PS（DDR/时钟/MIO）  ── 用到 xsa 导出的 psu_init      │
│  4. FSBL：把 bitstream 灌进 PL  ── PL 变成 TPU + DVP 设计             │
│  5. FSBL：加载并跳转到 U-Boot（BOOT.BIN 里的第二段）                  │
│  6. U-Boot：读 image.ub，加载内核 + dtb 到 DDR，跳进内核              │
│  7. Linux 内核启动 → 挂载根文件系统 → 登录 shell（串口 ttyPS0）       │
│  8. 用户在 shell 里跑 demo ELF（Linux 路线）                          │
└──────────────────────────────────────────────────────────────────────┘
        裸机路线则在第 5 步之后改为：直接运行 standalone ELF，
        它从 init_platform() 开始，最终打印出「Choose Feature to Test」菜单
```

注意第 3 步里的 `psu_init`：FSBL 初始化 PS 的依据正是 xsa 导出的 PS 配置（TCL 里那一大段 `PSU__*` 参数）。所以三个交付物的关系其实是：**xsa 提供 PS/PL 的「长相」→ BOOT.BIN 把它变成「启动动作」→ image.ub 在其上「跑 Linux」**。

#### 4.4.3 源码精读

裸机路线下，启动链的终点是 `main.cc`。它的开头体现了「FSBL 交棒之后，应用做的第一件事」：

[sdk/standalone/src/main.cc:L260-L277](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L260-L277) —— `init_platform()` 开缓存与串口（即 4.2 讲的 115200 UART），随后按编译开关依次：`SD_Init()` 初始化 SD 卡（若 `SD_CARD_IS_READY`）、`I2C_config_init_720p()` 配置摄像头（若 `EEP_DVP_CAMERA`）。这说明裸机程序运行时，PS 与 PL 已经由 FSBL 配好了，应用直接用即可。

随后打印出交互菜单——这就是裸机路线上板后，串口里能看到的画面：

[sdk/standalone/src/main.cc:L323-L340](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L323-L340) —— 菜单提供 `1: Get 1 Frame`、`2: Forward Result`、`3: Save Image to SD Card`、`4: Read Test Image`、`5: Run Demo`、`0: Exit`。看到这个菜单，就说明 BOOT.BIN（含 bitstream）已生效、TPU 已就位。

而这些菜单项依赖的硬件能力（SD 卡、摄像头、DP 显示）能否出现，由 `config.h` 的开关决定：

[sdk/standalone/src/config.h:L57-L67](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L57-L67) —— `EEP_DVP_CAMERA`（摄像头）、`IMG_HEIGHT 720 / IMG_WIDTH 1280`（720p 采集分辨率）、`SD_CARD_IS_READY`（SD 卡输入）、`EEP_DP_ENABLE`（DP 显示输出）四个开关。它们必须和硬件设计实际打开的外设对齐——又一次回到「软件开关要跟着 xsa 走」这条主线上。

> 小提示：本仓库 README 的「Run steps」一节并没有展开具体命令，而是直接指向文档：

[README.md:L52-L53](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L52-L53) —— 「Please refer to the document.」意味着真正的烧录步骤要去 `doc/demo_readme.pdf` 查阅。

#### 4.4.4 代码实践

1. **实践目标**：在不碰板卡的前提下，把「启动链的每一步」对应到本仓库的一个文件/证据上，做一份带证据的启动流程表。
2. **操作步骤**：照着 4.4.2 的 8 步，逐条在仓库里找证据并填表。例如：
   - 第 2~3 步（FSBL/PS 初始化）→ 证据：`platform.c` 注释里提到的 `psu_init`（来自 xsa）。
   - 第 4 步（bitstream）→ 证据：TCL 的器件名 `xczu15eg` 与 IP 实例 `EEP_TPU_0`。
   - 第 6~7 步（Linux）→ 证据：TCL 里 `UART0 / Gem3 / SD0/SD1` 外设。
   - 第 8 步 / 裸机终点 → 证据：`main.cc` 的菜单。
3. **需要观察的现象**：每一步都能在仓库里找到至少一处文字证据，除了「具体烧录命令」需要查 PDF。
4. **预期结果**：得到一张「启动步骤 → 证据文件:行号 → 是否可在仓库内确认」的表，未确认的标「待本地验证」。
5. 这是典型的「源码阅读型实践」——哪怕没有板卡，也能把启动链理解到「每一步对应哪段代码/哪个交付物」的程度。

#### 4.4.5 小练习与答案

**练习 1**：Linux 路线和裸机路线的启动链，从哪一步开始分叉？

> **答案**：在前 4 步（Boot ROM → FSBL → 配置 PS → 灌 bitstream）完全相同；分叉发生在 FSBL 之后——Linux 路线由 U-Boot 加载 `image.ub` 跑 Linux；裸机路线则直接运行 standalone ELF，从 `init_platform()` 走到 `main.cc` 的菜单。

**练习 2**：如果想让裸机程序支持「从 SD 卡读网络」与「DP 显示输出」，`config.h` 里哪两个宏必须打开？

> **答案**：`SD_CARD_IS_READY`（控制 SD 初始化与 `eepnet.mem` 读取）和 `EEP_DP_ENABLE`（控制 `graphic_buffer_init` 与显示中断初始化）。当然前提是硬件设计（xsa）里确实打开了 SD 控制器与 DP 显示通路。

## 5. 综合实践

本讲的实践任务是规格里指定的「文档查阅 + 启动步骤整理」。由于 `doc/demo_readme.pdf` 需在本地用 PDF 阅读器打开（仓库环境内无法渲染其内容），下面给出**可在本地完成**的步骤清单模板，请边读 PDF 边填空，无法确认的项标「待确认」。

**实践目标**：整理出一份「从拿到 BOOT.BIN 到在串口看到（Linux 或裸机菜单）输出」的最小步骤清单。

**操作步骤**：

1. 打开 `doc/demo_readme.pdf`（英文版为 `doc/demo_readme-English.pdf`），找到「烧录 / 启动 / Boot」相关章节。
2. 准备一张 SD 卡与本仓库的三个交付物：`hardware/BOOTbin/BOOT.BIN`、`hardware/BOOTbin/image.ub`、`hardware/xsa/system_wrapper.xsa`。
3. 按下面的清单逐项填写（括号内是预期内容，需以 PDF 实际为准）：

   ```text
   □ 步骤1：把 BOOT.BIN 和 image.ub 拷贝到 SD 卡第一个分区（FAT32）  （待确认是否还需其它文件）
   □ 步骤2：设置板卡启动模式为 SD 启动                                  （待确认具体拨码位置）
   □ 步骤3：插 SD 卡、接串口（USB-TTL）、设波特率 115200               （已由 platform.c 注释佐证）
   □ 步骤4：上电，串口应陆续看到：U-Boot 日志 → 内核启动日志           （待本地验证）
   □ 步骤5：内核挂载根文件系统后出现登录提示符 / shell                  （待本地验证）
   □ 步骤6（裸机路线）：若烧的是 standalone ELF，则看到
            「Choose Feature to Test / 1:Get 1 Frame ...」菜单        （已由 main.cc 佐证）
   ```

4. **需要观察的现象**：串口先打印 Boot ROM/FSBL/U-Boot 阶段日志（这一段速率切换时可能短暂乱码，属正常），然后是 Linux 内核 `Booting Linux ...` 的日志，最后出现 shell；或裸机菜单。
5. **预期结果**：得到一份与上面清单对应的、填好实际命令的「上板步骤说明书」。若某项 PDF 未提及，请明确写「待确认」而不要编造命令。
6. **加分项**：完成上面的「源码阅读型实践」（4.4.4），把启动链每一步的证据写到清单备注里，让它变成一份「看得见源码」的步骤说明书。

## 6. 本讲小结

- `hardware/` 下三个文件各司其职：`BOOT.BIN` 是启动镜像容器（FSBL + bitstream + U-Boot），`image.ub` 是 U-Boot 包装的 Linux 内核 + 设备树，`xsa` 是给软件工程师的硬件设计包。
- ZynqMP 启动是一条分级链：Boot ROM → FSBL（配 PS、灌 bitstream）→ U-Boot → Linux 内核 → shell；裸机路线在 FSBL 之后改为直接跑 standalone ELF。
- **xsa 决定软件地址**：TCL 里 `assign_bd_address` 把 TPU 寄存器映射到 `0xA0000000`、DVP 寄存器映射到 `0xA00C0000`，与 `config.h` 里的 `EEPTPU_REG_BASE_ADDR`/`EEPDVP_REG_BASE_ADDR` 逐位相同——这是硬件设计与软件代码最直接的接口。
- TPU 有两条 AXI 通路：寄存器通路（PS→TPU，下命令，256 KB）与数据通路（TPU→DDR 的 HP0 口，搬张量，覆盖低 2 GB）；`EEPTPU_MEM_BASE_ADDR=0x31000000` 就落在后者范围内。
- 串口波特率为 115200 bps（见 `platform.c` 注释），启动后能看到的外设（UART0/Gem3/USB0/SD0/SD1 等）由 TCL 里 PS 的 MIO 分配决定。
- 具体烧录命令不在 README 中，README 直接指向 `doc/demo_readme.pdf`；二进制内部的分区细节需用 Xilinx 工具或在手册核实，相关结论均标注「待本地验证」。

## 7. 下一步学习建议

学完本讲，你已经知道「板子怎么跑起来」以及「软件的魔法地址从哪来」。下一步建议：

1. **想自己重建硬件工程**：进入 [u1-l4 FPGA 工程构建与 IP 集成](u1-l4-fpga-project-and-ip.md)，精读 `script/create_prj.sh` 与 `system_rtl_*.tcl`，看这份产出 `xsa` 与 `BOOT.BIN` 的块设计是怎么用脚本搭起来的。
2. **想转入软件主线**：进入 [u2-l1 SDK 全景](u2-l1-sdk-overview.md)，从 `sdk/Readme.md` 出发，正式开始「编译出 bin → 加载 bin 推理」的软件学习路线。
3. **手头没板卡**：可以跳过本讲的综合实践，直接从 u1-l4 的脚本与约束阅读起步，软件部分只需交叉编译环境即可上手。
