# NanoVNA 是什么：项目定位与硬件全景

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 NanoVNA 是什么（一台掌上矢量网络分析仪的**固件**源码仓库），以及它能测量哪些量（反射系数 S11、传输系数 S21）。
2. 对照硬件框图，说出固件中每个主要源码文件对应驱动的是哪个硬件模块（si5351 时钟发生器、tlv320aic3204 音频编解码器、ili9341 LCD……）。
3. 理解本项目「以 `nanovna.h` 为唯一公共头文件」的模块间接口约定，并学会把 `nanovna.h` 当作全项目的"地图"来读。

本讲**不需要**你拥有 NanoVNA 硬件，所有实践都在 PC 上完成。

## 2. 前置知识

### 2.1 什么是网络分析仪

想象你有一段电缆、一个天线或者一个滤波器，你想知道它的"射频性格"——它在各个频率上是把信号反射回来，还是顺利传过去？网络分析仪（Network Analyzer）就是干这个的：

1. 向被测件（DUT，Device Under Test）发出一个已知频率的正弦信号；
2. 测量**反射回来**多少、**穿过去**多少；
3. 换一个频率，重复，直到扫完整个频段。

### 2.2 反射系数 Γ 与 S 参数

传输线理论里，如果线路特性阻抗为 \( Z_0 \)（NanoVNA 是 50Ω），负载阻抗为 \( Z_L \)，则负载处的**反射系数**为：

\[
\Gamma = \frac{Z_L - Z_0}{Z_L + Z_0}
\]

- 理想开路（\( Z_L \to \infty \)）：\( \Gamma = 1 \)（全反射，同相）；
- 理想短路（\( Z_L = 0 \)）：\( \Gamma = -1 \)（全反射，反相）；
- 完美匹配的 50Ω 负载：\( \Gamma = 0 \)（无反射）。

\( \Gamma \) 是**复数**，模 \( |\Gamma| \) 表示反射了多少，辐角表示反射波的相移。由它还能推出驻波比：

\[
\mathrm{VSWR} = \frac{1+|\Gamma|}{1-|\Gamma|}
\]

S 参数（散射参数）是把上述概念推广到多端口后的标准描述。NanoVNA 关心其中两个：

| 参数 | 含义 | 俗称 |
|---|---|---|
| \( S_{11} \) | 端口 1 的反射系数 | 回波损耗、驻波比的天源 |
| \( S_{21} \) | 从端口 1 到端口 2 的传输系数 | 增益 / 插入损耗 |

### 2.3 "矢量"是什么意思

只测 \( |\Gamma| \) 的仪器叫**标量**网络分析仪；同时测幅度**和相位**的叫**矢量**网络分析仪（VNA）。相位信息让你能把 \( \Gamma \) 画在史密斯圆图上、换算成阻抗 \( Z_L \)、做时域变换……本固件中所有测量结果都以"实部 + 虚部"两个 float 保存，这就是"矢量"在代码里的样子（后面 4.3 节会看到 `measured[2][POINTS_COUNT][2]` 这个数组）。

### 2.4 需要的一点嵌入式常识

- **MCU**：单片机。本项目主控是 ST 的 STM32F072，内核 ARM Cortex-M0，主频 48MHz，片上 Flash 128KB / RAM 16KB 左右——一台 2010 年代初性能水平的电脑还不如的"小脑"，却要跑完整个测量 + 绘图 + USB 交互。
- **RTOS**：实时操作系统。本项目用 ChibiOS（作为 git 子模块引入），提供线程、互斥量、硬件抽象层（HAL）。
- **I2C / SPI / I2S**：三种常见外设总线。I2C 用来"配置"芯片（写寄存器），SPI 用来高速刷屏幕，I2S 专为传输数字音频采样——本项目中它搬运的正是中频采样数据。

如果以上某些词还陌生，不必担心，本讲会结合代码再解释一遍。

## 3. 本讲源码地图

先看仓库顶层结构（`git ls-files` 的主干）：

```
NanoVNA/
├── README.md              # 项目说明：构建、烧录、文档链接
├── Makefile               # 构建脚本（ChibiOS 构建系统）
├── STM32F072xB.ld         # 链接脚本：Flash/RAM 地址布局
├── nanovna.h              # ★ 全项目公共头文件（本讲主角之一）
├── main.c                 # 入口 main()、扫频线程、shell 命令、校准（最大的文件）
├── si5351.c / si5351.h    # 时钟发生器驱动（激励信号 + 本振）
├── tlv320aic3204.c        # 音频编解码器驱动（中频采样）
├── dsp.c                  # 数字正交解调，算出复数 Γ
├── fft.h                  # FFT 实现（时域变换用）
├── ili9341.c              # LCD 驱动（SPI + DMA）
├── plot.c                 # 轨迹/网格/marker 绘制逻辑
├── ui.c                   # 触摸、拨轮、菜单、数字输入
├── adc.c                  # ADC：电池电压监测、触摸读取
├── flash.c                # 配置与校准数据的掉电保存
├── usbcfg.c / usbcfg.h    # USB CDC（虚拟串口）配置
├── chprintf.c             # 裁剪版 printf（shell 输出用）
├── Font5x7.c              # 5x7 点阵字体位图
├── numfont20x22.c         # 20x22 数字大字体位图
├── chconf.h / halconf.h / mcuconf.h   # ChibiOS 内核/HAL/MCU 配置
├── NANOVNA_STM32_F072/    # 板级支持包（引脚定义等）
├── ChibiOS/               # RTOS 子模块（git submodule）
├── python/                # PC 端 Python 上位机与 Jupyter 示例
└── doc/                   # 原理图、PCB 照片、★硬件框图 blockdiagram
```

本讲重点读三个东西：

| 文件 | 作用 |
|---|---|
| `README.md` | 项目定位、构建烧录方法、参考文档入口 |
| `doc/nanovna-blockdiagram.png` | 硬件框图：所有芯片和连接关系一图流 |
| `nanovna.h` | 全项目公共头文件 = 模块接口契约 + 常量定义 |

## 4. 核心概念与源码讲解

### 4.1 README 与文档：这个项目到底是什么

#### 4.1.1 概念说明

读任何开源项目的第一步都是 README。NanoVNA 的 README 告诉我们三件事：

1. **它是什么**：一个"非常小的掌上矢量网络分析仪"，带 LCD、带电池、可独立工作；本仓库是它的**固件源码**。
2. **怎么构建**：arm-none-eabi 交叉工具链 + `make`（或 docker 镜像），依赖 ChibiOS 子模块。
3. **哪里有更深资料**：原理图、PCB 照片、硬件框图都在 `doc/` 目录。

一个容易被忽略的细节：README 明确说硬件设计资料是公开的（"Hardware design material is disclosed"），所以我们可以**一边看固件一边对照原理图和框图**，这是学习嵌入式射频的绝佳材料。

#### 4.1.2 核心流程

从 README 出发建立一个项目的"使用观"：

```
git clone + git submodule update --init     ← 拿源码（含 ChibiOS）
        ↓
make（本地工具链 或 docker edy555/arm-embedded:8.2）
        ↓
build/ch.bin
        ↓
让设备进入 DFU 模式（BOOT0 跳线 或 Config->DFU 菜单）
        ↓
dfu-util / make flash 烧录
        ↓
USB 连上 PC → 虚拟串口 → shell 命令 / python 上位机
```

#### 4.1.3 源码精读

README 开头的定位描述——"standalone with lcd display, portable device with battery"，点明了它与传统"仪器 + 上位机"式 VNA 的区别：

- [README.md:13-20](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L13-L20)：About 一节，说明 NanoVNA 是掌上独立 VNA，本仓库是其固件源码。

构建与烧录相关：

- [README.md:43-49](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L43-L49)：克隆代码并初始化子模块。**注意必须执行 `git submodule update --init --recursive`**，否则 `ChibiOS/` 是空目录，编译必然失败。
- [README.md:51-62](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L51-L62)：`make` 直接编译；不想装工具链可以用 docker 镜像 `edy555/arm-embedded:8.2`。
- [README.md:64-77](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L64-L77)：进入 DFU 模式的两种方法（BOOT0 跳线供电、或固件菜单 Config→DFU），然后 `dfu-util` 或 `make flash` 烧录。

文档与参考资料（本讲实践要用的框图就在这里）：

- [README.md:90-99](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L90-L99)：Reference 一节，链接了原理图 `doc/nanovna-sch.pdf`、PCB 照片和**硬件框图 `doc/nanovna-blockdiagram.png`**，还有官方 kit 购买入口。
- [README.md:79-88](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L79-L88)：生态一览——NanoVNASharp、WebSerial 客户端、Android App，以及本仓库自带的 `python/` 目录（第 5 单元会专门讲）。

最后确认一下 MCU 型号。板级头文件里写的是：

- [NANOVNA_STM32_F072/board.h:23-37](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h#L23-L37)：板名 `NanoVNA`，MCU 宏 `STM32F072xB`（xB 后缀 = 128KB Flash），外部 8MHz 晶振（`STM32_HSECLK 8000000`）。

#### 4.1.4 代码实践

**实践 1：把仓库"摸"一遍（纯 PC，约 10 分钟）**

1. **实践目标**：不编译、不烧录，只用文件系统命令建立仓库全貌，并验证 README 里的说法真实存在。
2. **操作步骤**：
   - 在仓库根目录执行 `git ls-files`，数一数顶层 `.c`/`.h` 文件有几个（答案：15 个左右）。
   - 执行 `ls doc/`，确认 README 引用的三个文件 `nanovna-sch.pdf`、`nanovna-pcb-photo.jpg`、`nanovna-blockdiagram.png` 都在。
   - 执行 `cat .gitmodules`，确认子模块指向 ChibiOS 仓库；再 `ls ChibiOS/` 看它是否已初始化（若为空，说明还没跑 `git submodule update --init`，不影响本讲阅读）。
   - 打开 `doc/nanovna-blockdiagram.png`，把图上每一个带文字的方块抄在笔记里。
3. **需要观察的现象**：README 中提到的每个文件/工具链/文档都能在仓库里找到对应物；框图上的芯片名称与源码文件名一一呼应（`Si5351A` ↔ `si5351.c`，`TLV320AIC3204` ↔ `tlv320aic3204.c`）。
4. **预期结果**：你会得到一张手抄的芯片清单，这正是 4.2 节的素材。

#### 4.1.5 小练习与答案

**练习 1**：为什么克隆仓库后必须执行 `git submodule update --init --recursive`，否则编译会失败？

**答案**：NanoVNA 没有把 RTOS 源码直接放进仓库，而是以 git 子模块形式引用 ChibiOS（见 `.gitmodules`）。`git clone` 默认只创建空的 `ChibiOS/` 目录，不拉取内容；而 Makefile 要编译 ChibiOS 的内核与 HAL 源文件，找不到文件自然失败。README 第 43-49 行明确写了这一步。

**练习 2**：README 说这个设备 "standalone"（独立工作），固件里哪一处最能体现这一点？

**答案**：固件自带 LCD 驱动（`ili9341.c`）、完整的绘图模块（`plot.c`）和触摸/菜单交互（`ui.c`），测量结果直接画在设备屏幕上，PC 只是可选的增强（USB shell 与 python 上位机）。传统 VNA 往往只输出数据、显示全靠上位机。

**练习 3**：`doc/` 目录下有三个硬件资料文件，各自适合什么场景？

**答案**：`nanovna-blockdiagram.png` 是框图，适合快速理解芯片间连接（本讲用）；`nanovna-sch.pdf` 是原理图，适合查某个引脚/阻容值的细节；`nanovna-pcb-photo.jpg` 是电路板照片，适合把原理图对应到实体器件位置。

### 4.2 硬件框图：一块 Cortex-M0 如何造出 VNA

#### 4.2.1 概念说明

打开 `doc/nanovna-blockdiagram.png`，你会看到这些带文字标注的方块（建议亲手打开对照，以下以图中标签为准）：

- **DUT**：被测件，接在 CH0/CH1 端口上；
- **Si5351A**：时钟发生器（由 26MHz 晶振供参考），产生射频激励与本振；
- 电桥（bridge）与混频电路：比较入射波与反射波，并把射频搬移到低频；
- **I2S Codec TLV320AIC3204**：把模拟中频变成数字采样；
- **STM32 F072（Cortex-M0, 48MHz）**：运行本固件，全系统的指挥中心；
- **SPI LCD**：320x240 的 ili9341 屏幕；
- **MCP73831 + LDO**：锂电池充电与稳压电源；
- RF 开关（图中标注 x5 的一组）：切换射频路径。

VNA 的经典难题是：**48MHz 的 Cortex-M0 不可能直接采样 900MHz 的射频信号**。NanoVNA 的解法非常巧妙——**外差到音频频段**：

1. si5351 的 CLK1 输出频率为 \( f \) 的激励信号送入电桥和 DUT；
2. si5351 的 CLK0 输出频率为 \( f + 5\,\mathrm{kHz} \) 的本振信号；
3. 两路信号在混频器中相乘，差频项落在 **5kHz**——人耳可听的音频范围；
4. 5kHz 的中频由音频编解码器以 48kHz 采样，彻底变成数字问题；
5. 固件用 `dsp.c` 做数字正交解调，得到复数 \( \Gamma \)。

这套"把射频测量降维成音频 DSP"的思路，是理解整个固件架构的钥匙，也是第 2 单元全部内容的伏笔。

#### 4.2.2 核心流程

整机信号链（箭头方向即信号流向）：

```
            26MHz 晶振
                │
            ┌───▼───┐  I2C（STM32 写寄存器配置分频比）
            │Si5351A│◄──────────────────────────┐
            └───┬───┘                           │
    CLK1: f     │      CLK0: f + 5kHz           │
  （激励信号）   │    （本振信号）  CLK2: 8MHz 固定 │
        ┌───────▼──────────┐                    │
        │ 电桥 + 混频 (射频) │◄── DUT（被测件）    │
        └───────┬──────────┘                    │
                │ 两路 5kHz 模拟中频（参考/采样）  │
        ┌───────▼──────────┐                    │
        │ TLV320AIC3204    │ I2C 配置寄存器──────┤
        │ 音频编解码器      │                    │
        └───────┬──────────┘                    │
                │ I2S，48kHz 立体声采样，DMA 搬运 │
        ┌───────▼──────────────────────────┐    │
        │ STM32F072  Cortex-M0 @48MHz      │────┘
        │  dsp.c 正交解调 → 复数 Γ           │
        │  main.c sweep() 扫频/校准          │──► USB CDC（shell/python）
        │  plot.c/ui.c 轨迹与交互            │
        └───────┬──────────────────────────┘
                │ SPI（+DMA）
        ┌───────▼──────────┐      ┌──────────┐
        │ ili9341 SPI LCD  │      │ 电池:     │
        └──────────────────┘      │ MCP73831 │ ← adc.c 监测电压
                                  └──────────┘
```

#### 4.2.3 源码精读

**关键常量都在 `nanovna.h` 开头**，先看频率范围和那个著名的 5kHz：

- [nanovna.h:29-38](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L29-L38)：定义 `START_MIN 50000`（最低 50kHz）、`STOP_MAX 2700000000U`（最高 2.7GHz）、`FREQUENCY_OFFSET 5000`（**激励与本振相差 5kHz**，注释特别说明 dsp.c 的 sincos 表就是按这个偏移生成的，改它必须重新生成表）。这四行常量就是 4.2.1 节外差原理的直接代码证据。

**si5351 的三路输出分工**，注释写得明明白白：

- [si5351.c:28-31](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L28-L31)：晶振频率 `XTALFREQ 26000000U`，`CLK2_FREQUENCY 8000000U` 固定 8MHz。
- [si5351.c:379-385](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L379-L385)：`si5351_set_frequency()` 的注释——**CLK0 输出 frequency + offset（本振）、CLK1 输出 frequency（激励）、CLK2 固定 8MHz**。紧接其后的函数体就是干这件事的。
- [si5351.c:350-370](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L350-L370)：一张 ASCII 频段表，解释 50kHz~2.7GHz 如何被拆成"直接模式 x1 + 谐波模式 x3/x5/x7/x9/x11"覆盖——Si5351 本身只能到 300MHz，更高频率靠谐波。表中 `f` 为 CLK1 基波、`of` 为 CLK0 偏移后的频率。细节留到第 2 单元（u2-l2）精讲。
- [si5351.c:371-377](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L371-L377)：`si5351_get_band()`，三行代码把频率切成 1/2/3 三个频段。

**主控如何通过 I2C 掌控这两颗芯片**（初始化序列，位于 `main()` 内）：

- [main.c:2377-2378](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2377-L2378)：`i2cStart(&I2CD1, &i2ccfg)` 启动 I2C 总线，紧接着 `si5351_init()` 按寄存器表配置时钟发生器。
- [tlv320aic3204.c:24](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L24)：编解码器的 I2C 地址 `AIC3204_ADDR 0x18`，与 si5351 挂在同一条 I2CD1 上。
- [main.c:2420-2424](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2420-L2424)：`tlv320aic3204_init()` 配置 codec，随后 `i2sStart(&I2SD2, ...)` + `i2sStartExchange()` 启动 I2S 双向数据流（DMA 持续往内存搬采样）。
- [main.c:2400](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2400)：`ili9341_init()` 初始化 SPI LCD。

**测量主循环里两路通道的切换**（CH0 反射 / CH1 传输）：

- [main.c:857-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L857-L897)：`sweep()` ——固件的心脏。每个频点先 `set_frequency()`（内部调 si5351），然后 `tlv320aic3204_select(0)` 测 CH0、`tlv320aic3204_select(1)` 测 CH1，最后把结果写进 `measured[]`。
- [main.c:866](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L866)：注释直说 `// 60 CH0:REFLECT, reset and begin measure`——切到**反射**通道。
- [main.c:875](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L875)：`// 60 CH1:TRANSMISSION`——切到**传输**通道。行内那些数字（5300、700、1900……）是作者标注的各阶段耗时，具体单位需结合 `START/STOP_PROFILE` 实测确认（第 2 单元 u2-l5 的实践正好会做这件事）。
- [nanovna.h:40-41](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L40-L41)：`#define POINTS_COUNT 101` 与 `extern float measured[2][POINTS_COUNT][2]`——一次扫描 101 个频点，2 个通道，每点实部/虚部两个 float。**这行声明就是"S11/S21 矢量测量"在内存里的形态。**

**采集下来的数字样本如何变成 Γ**（只看入口，细节在第 2 单元）：

- [dsp.c:29](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L29)：`const int16_t sincos_tbl[48][2]` ——48 对正弦/余弦值，本质是 5kHz 中频的**数字正交本振表**（48kHz 采样下 5kHz 正好一个周期 9.6 个样本，48 点覆盖 5 个整周期）。
- [dsp.c:49-50](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L49-L50) 与 [dsp.c:88-89](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L88-L89)：`dsp_process()` 做混频累积，`calculate_gamma()` 做复数除法（采样÷参考）得到归一化的 Γ。

把上面的代码点串起来，就得到完整的「源码文件 → 硬件模块 → 职责」对照表：

| 源码文件 | 硬件模块（框图） | 总线 | 职责一句话 |
|---|---|---|---|
| `si5351.c` | Si5351A 时钟发生器 | I2C | 生成激励信号（CLK1）、本振（CLK0，+5kHz）与固定 8MHz（CLK2） |
| `tlv320aic3204.c` | TLV320AIC3204 音频 codec | I2C + I2S | 采集 5kHz 中频模拟信号并数字化，切换 CH0/CH1 通道 |
| `dsp.c` | （纯软件） | — | 对采样做正交解调，算出复数 Γ |
| `main.c` | STM32F072 本体 | — | 初始化、sweep 扫频、校准、shell 命令 |
| `ili9341.c` | SPI LCD | SPI(+DMA) | 像素/图形原语与文字绘制 |
| `plot.c` | （纯软件） | — | 网格、轨迹、marker 的绘制逻辑 |
| `ui.c` | 触摸屏、拨轮、按键 | GPIO/EXTI | 菜单树、数字输入、事件分发 |
| `adc.c` | ADC 外设 | — | 电池电压监测、触摸位置读取 |
| `flash.c` | 片上 Flash | — | 配置与 5 个校准槽的掉电保存 |
| `usbcfg.c` | USB 外设 | USB | 虚拟串口（CDC），shell 的物理载体 |
| `Font5x7.c` / `numfont20x22.c` | （字库数据） | — | 5x7 ASCII 字体、20x22 数字大字体位图 |

#### 4.2.4 代码实践

**实践 2：用 grep 顺着框图"点验"信号链（纯 PC，约 10 分钟）**

1. **实践目标**：不靠记忆，用检索工具证明框图上的每条连接在代码里都有对应调用。
2. **操作步骤**：
   - `grep -n "si5351_" main.c | head` —— 找到 `main()` 里对时钟发生器的初始化与每次变频的调用点。
   - `grep -n "tlv320aic3204_select" main.c` —— 确认 CH0/CH1 切换只发生在 `sweep()` 和 `cmd_port` 两处。
   - `grep -n "i2sStart\|i2sStartExchange" main.c` —— 确认 I2S 只在初始化时启动一次。
   - `grep -n "ili9341_init\|ui_init\|plot_init" main.c` —— 确认显示三件套的初始化顺序。
3. **需要观察的现象**：每条 grep 的命中行号都落在 `main()`（2370 行起）或 `sweep()`（857 行起）附近——初始化集中、调用点少，说明模块边界干净。
4. **预期结果**：你会得到一组行号清单，与 4.2.3 节给出的链接互相印证。**待本地验证**：不同环境下 grep 输出顺序一致，行号应与本文相同（基于 HEAD `d02db79`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CLK0 要输出 \( f + 5\,\mathrm{kHz} \) 而不是正好 \( f \)？

**答案**：若本振与激励同频，混频后差频为 0Hz（直流），直流耦合、放大和 ADC 的零漂都会淹没信号，且无法区分相位。偏移 5kHz 把有用的中频搬到音频频段，避开直流，还能让一个普通的音频 codec（直流性能很差、但音频段动态范围很好）胜任采样任务。对应代码：`nanovna.h:33-34` 的 `FREQUENCY_OFFSET 5000`。

**练习 2**：Si5351 最高只能输出约 300MHz，为什么 NanoVNA 能标称到 2.7GHz？

**答案**：利用谐波模式。300MHz 以上固件让 si5351 输出基波 \( f/3, f/5, \dots \)，实际使用其 3/5/7/9/11 次谐波去激励 DUT，同时本振 CLK0 做相应倍增。`si5351.c:350-370` 的频段表和 `main.c:82` 的 `FREQ_HARMONICS`（默认 300MHz，见 `main.c:797` 的 `.harmonic_freq_threshold = 300000000`）共同实现这一策略。

**练习 3**：`measured[2][POINTS_COUNT][2]` 三个维度各是什么？

**答案**：第一维是通道（0 = CH0 反射 S11，1 = CH1 传输 S21，对应 `sweep()` 里 `tlv320aic3204_select(0/1)` 两次测量）；第二维是频点索引（`POINTS_COUNT` = 101）；第三维是复数的实部与虚部——这就是"矢量"二字的内存表达（`nanovna.h:40-41`）。

### 4.3 nanovna.h：一个头文件统治整个项目

#### 4.3.1 概念说明

大多数稍大的项目会有几十个头文件、复杂的 include 树。NanoVNA 反其道而行：**只有一个公共头 `nanovna.h`，所有 `.c` 都包含它**。它同时承担四个角色：

1. **模块接口契约**：每个 `.c` 对外提供的函数原型集中声明在这里，并按模块用注释分区；
2. **全局配置常量**：频点数、频率上下限、屏幕尺寸、颜色默认值……改一处全局生效；
3. **核心数据结构博物馆**：`trace_t`（轨迹）、`marker_t`（标记）、`config_t`（全局配置）、`properties_t`（可保存的仪器状态）全在这；
4. **项目地图**：从上往下读 `nanovna.h`，注释里的 `main.c` / `dsp.c` / `plot.c` / `ili9341.c` / `flash.c` / `ui.c` / `adc.c` 分区标题，就是一张现成的模块清单。

这种做法的取舍很典型：小项目里它极大降低了维护成本（不用管依赖关系），代价是任何文件改动都触发大范围重编译、命名空间容易拥挤。读熟它，后面每一讲找接口都只需回到这一个文件。

#### 4.3.2 核心流程

`nanovna.h` 自上而下的分区结构：

```
nanovna.h
├── #include "ch.h"                 ← 引入 ChibiOS（类型、线程、HAL）
├── /* main.c */    区：频率常量、POINTS_COUNT、measured、校准状态位、sweep_mode
├── /* dsp.c */     区：AUDIO_BUFFER_LEN、STATE_LEN/SAMPLE_LEN、dsp 函数原型
├── /* tlv320aic3204.c */ 区：codec 三个函数原型
├── /* plot.c */    区：屏幕几何、trace_type 枚举、trace_t/marker_t、REDRAW 标志
├── /* ili9341.c */ 区：RGB565 宏、SPI_BUFFER_SIZE、绘图原语原型
├── /* flash.c */   区：保存地址、properties_t/config_t、current_props 字段别名宏
├── /* ui.c */      区：OP_* 操作请求标志、uistat_t、触摸接口
├── /* adc.c */     区：ADC 接口
└── 杂项：plot_printf、PULSE、START/STOP_PROFILE 性能测量宏
```

其中有一个**非常值得注意的宏技巧**：固件把"当前仪器状态"整体放在结构体 `current_props` 里，再用宏把常用字段"拍平"成全局变量名，例如 `sweep_points` 其实就是 `current_props._sweep_points`。这样业务代码写起来像全局变量，保存/读取校准槽时又整块拷贝，两全其美。

#### 4.3.3 源码精读

- [nanovna.h:20-23](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L20-L23)：文件唯一 include 的 `ch.h`（ChibiOS 内核头），以及 `__USE_DISPLAY_DMA__` 开关——打开后 LCD 像素传输走 DMA，CPU 得以在刷屏时并行做别的（第 5 单元 u5-l3 详述）。
- [nanovna.h:43-66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L43-L66)：校准相关的状态位 `CALSTAT_*` 与五个误差项编号 `ETERM_ED/ES/ER/ET/EX`（直效率、源匹配、反射跟踪、传输跟踪、隔离度）。**现在只需知道：校准 = 用已知标准件测出五个复数误差项，再把它们从测量中扣除**，数学推导在第 3 单元（u3-l2）。
- [nanovna.h:103-123](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L103-L123)：`/* dsp.c */` 分区。`AUDIO_BUFFER_LEN 96`（48kHz 采样下 5ms = 96 个样本，注释原文 "5ms @ 48kHz"）、`STATE_LEN 32`、`SAMPLE_LEN 48`——这三个数与 5kHz 中频严格耦合，理解它们等于理解外差架构（u2-l3/u2-l4 展开）。
- [nanovna.h:195-198](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L195-L198)：`enum trace_type` 列出全部 12 种显示格式：LOGMAG、PHASE、DELAY、SMITH、POLAR、LINEAR、SWR、REAL、IMAG、R、X、Q（外加 OFF）。一个复数 Γ 能被解读成这么多视图，正是"矢量"测量的价值。
- [nanovna.h:212-219](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L212-L219)：`trace_t` 结构——每条轨迹由 `enabled/type/channel` 加 `scale/refpos` 描述，最多同时 4 条（`TRACES_MAX`，第 193 行）。
- [nanovna.h:225-237](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L225-L237)：`config_t`——与测量无关的持久配置（DAC 值、各颜色、触摸校准、谐波阈值、电池偏置），由 `flash.c` 保存。
- [nanovna.h:291-297](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L291-L297)：`REDRAW_*` 标志位与 `volatile uint8_t redraw_request`——UI 采用"请求-响应"式局部重绘：任何模块只置位，绘制统一由 `draw_all()` 消费（u4-l4 专讲）。
- [nanovna.h:299-327](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L299-L327)：`/* ili9341.c */` 分区。`RGB565(r,g,b)` 宏把三通道颜色压成 16 位并**反转字节序**以匹配 SPI 总线时序；`SPI_BUFFER_SIZE 2048` 定义了那个被多处复用的像素缓冲 `spi_buffer`。
- [nanovna.h:363-387](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L363-L387)：`properties_t`——仪器完整状态（频段、101 个频点、5 组校准数据、轨迹、marker、带宽……），注释标明 `sizeof(properties_t) == 0x1200`（4608 字节），恰好放进一个 Flash 保存槽。
- [nanovna.h:395-410](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L395-L410)：前述"字段别名宏"——`#define sweep_points current_props._sweep_points` 等 15 个宏，把结构体字段映射成顺手的短名字。**读 main.c 时看到的全局变量，多半来自这里。**
- [nanovna.h:492-493](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L492-L493)：`START_PROFILE/STOP_PROFILE` 一对性能测量宏，用系统时钟差直接把耗时画到屏幕上——嵌入式性能调优的极简范例，第 2 单元实践会借用它。

再看 shell 命令表，它是"固件对外接口"的清单，也解释了 README 里各种上位机能做什么：

- [main.c:2153-2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208)：`static const VNAShellCommand commands[]`——30 余条命令（`version`、`scan`、`data`、`capture`、`cal`、`save`/`recall`、`marker`、`transform`……），每项第三列的 `CMD_WAIT_MUTEX` 标志决定该命令是否要等测量线程空闲才执行（u5-l1 详述）。`python/nanovna.py` 上位机本质就是往这个表里发命令。

#### 4.3.4 代码实践

**实践 3：亲手数一数 nanovna.h 的"分区"（纯 PC，约 10 分钟）**

1. **实践目标**：验证"nanovna.h 按模块分区"这一说法，并产出你自己的模块清单。
2. **操作步骤**：
   - `grep -n "^ \* [a-z0-9_]*\.c" nanovna.h` 或直接肉眼浏览，找出所有 `/* xxx.c */` 注释分区及其行号。
   - 对每个分区，记下它声明的**函数原型数量**和**最重要的一个宏或类型**。
   - 用 `wc -l nanovna.h main.c plot.c ui.c` 感受各文件规模（nanovna.h 约 497 行，main.c 约 2500 行）。
   - 挑一个宏做"追踪实验"：在 `main.c` 里 `grep -n "sweep_points"`，观察它作为 `current_props._sweep_points` 别名被多少处使用。
3. **需要观察的现象**：`sweep_points` 在 main.c 出现几十次，但真实存储只有 `current_props` 一份——别名宏让你用着像全局变量，存着像结构体成员。
4. **预期结果**：得到一张「分区 → 行号范围 → 关键导出」表。这是你后续每一讲回来查接口的索引页。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NanoVNA 敢用"单一公共头文件"这种在大项目里被认为是反模式的结构？

**答案**：因为项目小（约 15 个源文件、单个 `.c` 最多几千行）、模块间耦合本来就不低（plot 依赖 measured、ui 依赖 plot 的接口）、且由少数维护者开发。单一头文件消除了 include 依赖管理成本，代价（全量重编译、命名拥挤）在小规模下可接受。这是"架构匹配规模"的典型例子。

**练习 2**：`REDRAW_FREQUENCY`、`REDRAW_BATTERY` 这样的标志位为什么声明为 `volatile uint8_t redraw_request`？

**答案**：因为这些标志会被不同执行流（测量线程、UI 事件、shell 命令）读写，`volatile` 强制编译器每次都真正读写内存、不做寄存器缓存优化，避免一个线程置位后另一个线程永远看不到。这是嵌入式多线程间无锁通信的基础手法（u2-l5、u4-l4 展开）。

**练习 3**：`config_t` 和 `properties_t` 都是掉电保存的数据，为什么要分成两个结构、两个 Flash 区域？

**答案**：两者生命周期不同。`config_t` 是与测量无关的设备级偏好（颜色、触摸校准、电池偏置），只有一份；`properties_t` 是完整的仪器状态（频段 + 校准 + 轨迹 + marker），有 5 个槽位（`SAVE_PROP_CONFIG_0_ADDR` ~ `_4_ADDR`，见 `nanovna.h:355-361`），便于保存多组测量场景并快速切换。分开存放让"清空校准"不会误伤"屏幕颜色"这类用户偏好。

## 5. 综合实践

**编写 `print_modules.py`：把"源码 → 硬件 → 职责"地图固化成脚本**（对应本讲核心实践任务，纯 PC，约 20 分钟）

1. **实践目标**：用约 30 行 Python 把 4.2.3 节的对照表写成可执行脚本，并对照 `doc/nanovna-blockdiagram.png` 自查是否有遗漏的硬件模块——把"读过的"变成"可检索、可复查的"。

2. **操作步骤**：

   在仓库**外**的任意目录（遵守"不改源码"的约定，也可放在你自己的笔记目录）创建 `print_modules.py`：

   ```python
   #!/usr/bin/env python3
   # 示例代码：NanoVNA 固件模块地图（对应 doc/nanovna-blockdiagram.png）
   MODULES = {
       "si5351.c":         ("Si5351A 时钟发生器", "I2C",  "生成激励信号(CLK1)、本振(CLK0=+5kHz)、固定8MHz(CLK2)"),
       "tlv320aic3204.c":  ("TLV320AIC3204 codec", "I2C+I2S", "采样 5kHz 中频并数字化，切换 CH0/CH1"),
       "dsp.c":            ("(纯软件)",            "-",     "正交解调，算出复数反射/传输系数"),
       "main.c":           ("STM32F072 本体",      "-",     "初始化、sweep 扫频、校准、shell 命令"),
       "ili9341.c":        ("SPI LCD",             "SPI",   "像素块传输与文字绘制(DMA 加速)"),
       "plot.c":           ("(纯软件)",            "-",     "网格/轨迹/marker 绘制与脏矩形管理"),
       "ui.c":             ("触摸屏/拨轮/按键",     "GPIO",  "菜单树、数字输入、事件分发"),
       "adc.c":            ("ADC 外设",            "-",     "电池电压监测、触摸位置读取"),
       "flash.c":          ("片上 Flash",          "-",     "config 与 5 个校准槽的掉电保存"),
       "usbcfg.c":         ("USB 外设",            "USB",   "CDC 虚拟串口，承载 shell 协议"),
       "Font5x7.c":        ("(字库数据)",          "-",     "5x7 ASCII 字体位图"),
       "numfont20x22.c":   ("(字库数据)",          "-",     "20x22 数字大字体位图"),
   }

   def main():
       print(f"{'源码文件':<20}{'硬件模块':<24}{'总线':<9}职责")
       print("-" * 88)
       for src, (hw, bus, role) in MODULES.items():
           print(f"{src:<20}{hw:<24}{bus:<9}{role}")
       print(f"\n共 {len(MODULES)} 个模块；对照 doc/nanovna-blockdiagram.png 检查：")
       for chip in ["Si5351A", "TLV320AIC3204", "STM32", "SPI LCD", "MCP73831", "LDO"]:
           print(f"  框图芯片 {chip:<16} -> {'已覆盖' if chip.split()[0].lower() in str(MODULES).lower() else '未覆盖(电源/充电类，固件不直接驱动或仅 adc.c 监测)'}")

   if __name__ == "__main__":
       main()
   ```

   运行 `python3 print_modules.py`。

3. **需要观察的现象**：表格逐行输出；末尾的自查清单会提示 MCP73831（充电管理）和 LDO（稳压）是纯电源器件，固件不直接驱动（仅通过 `adc.c` 读电池电压），所以不在 `.c` 文件对照表中——这正是自查的意义：**区分"固件驱动的模块"与"硬件自带的模块"**。

4. **预期结果**：输出 12 行模块表 + 6 行框图自查。若你想更进一步，可以把每个模块的"入口函数 + 所在行号"（如 `si5351.c` → `si5351_set_frequency` → L386）加进字典，做成你自己的固件导航器。

5. **进阶（可选，有真机者）**：烧录固件后用 USB 串口终端连接，输入 `help`，把 [main.c:2153-2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208) 命令表里的命令逐条试一遍，体会"固件接口清单"的含义。

## 6. 本讲小结

- NanoVNA 是一台**掌上矢量网络分析仪**，本仓库是它的 STM32F072（Cortex-M0 @48MHz）固件：能测复数反射系数 \( S_{11} \) 与传输系数 \( S_{21} \)，结果以 `measured[2][101][2]`（通道 × 频点 × 实虚部）保存在内存中。
- 它的核心架构技巧是**外差到音频**：激励 \( f \) 与本振 \( f+5\,\mathrm{kHz} \) 混频得到 5kHz 中频，用音频 codec 以 48kHz 采样，把射频测量变成纯数字信号处理（`FREQUENCY_OFFSET 5000`、`AUDIO_BUFFER_LEN 96`）。
- 硬件与源码一一对应：`si5351.c` 管信号源（CLK0/CLK1/CLK2 三路输出）、`tlv320aic3204.c` 管采样、`dsp.c` 管解调、`ili9341.c`/`plot.c`/`ui.c` 管显示交互、`flash.c` 管掉电保存，全部挂在 ChibiOS RTOS 之上。
- `nanovna.h` 是全项目唯一的公共头文件，按模块分区，兼具接口契约、常量定义、数据结构博物馆和项目地图四重身份；其中 `current_props` 字段别名宏是读懂 main.c 全局变量的钥匙。
- README 指向的 `doc/` 目录（框图、原理图、PCB 照片）是硬件侧的权威参考，读固件时应当常开对照。

## 7. 下一步学习建议

下一讲（u1-l2《搭建工具链：编译、烧录与 CI 流程》）将拆解 `Makefile`：ChibiOS 构建系统如何拼装启动代码、HAL 与业务源码，如何用 docker 完成一次编译，以及 DFU 烧录与 CircleCI 自动发布。建议在继续之前：

1. 把 `nanovna.h` 从头到尾**通读一遍**（不到 500 行，只看注释和分区标题即可），这是后续所有讲义的"字典"。
2. 用 `git log --oneline -20` 浏览最近提交，感受项目的演进节奏（最近的提交涉及 TD 窗口损耗补偿、Q 值格式等，都是后续单元的话题）。
3. 如果手头有真机，先按 README 完成 `make flash` 一次，建立"改代码 → 上屏"的反馈回路；没有真机也完全不影响后续学习——本手册的实践大多可在 PC 上复现固件算法。
