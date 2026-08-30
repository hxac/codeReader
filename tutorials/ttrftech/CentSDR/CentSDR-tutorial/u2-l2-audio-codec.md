# 前端增益与采样：TLV320AIC3204 音频编解码器

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `(len, reg, data)` 哨兵结尾的寄存器配置表——这是嵌入式固件里"用字节数组描述一串寄存器写入"的常用编码方式。
2. 理解 TLV320AIC3204 的时钟树：8MHz 基准时钟如何经过 PLL 和多级分频得到 48/96/192kHz 三档采样率，以及切换采样率时的停机时序。
3. 掌握几组增益接口的作用范围：MICPGA 模拟增益（PGA gain）、ADC 数字增益（digital gain）、耳机音量（volume）、输入阻抗（impedance）。
4. 理解 AGC 配置结构体 `tlv320aic3204_agc_config_t` 各字段（`target_level`、`attack`、`decay`、`maximum_gain` 等）的物理含义，以及固件如何读回当前 AGC 增益并用于功率补偿。
5. 看懂芯片内 mini-DSP 一阶 IIR 滤波器的系数来源：从 `python/TLV320AIC3204-1st-IIR-HPF.ipynb` 的 scipy 设计到 `dsp 侧` 寄存器字节的完整换算路径，以及它同时承担 DC 抑制和 IQ 幅度平衡两个任务的设计技巧。

## 2. 前置知识

在进入源码之前，先用通俗语言澄清几个概念。

**编解码器（Codec）在 SDR 里的角色。** CentSDR 不像传统收音机那样用模拟电路解调，而是把正交检波器输出的 IQ 基带音频当成"声音"送进一颗音频编解码器 TLV320AIC3204。它内部有两个 ADC（把 I、Q 两路模拟信号数字化）和两个 DAC（把解调后的立体声音频变回模拟信号驱动耳机）。对 STM32 来说，这颗芯片就是 IQ 样本的"源头"和音频的"出口"。上一讲（u2-l1）我们看到 SI5351 默认输出 8MHz——那正是这颗编解码器的基准时钟输入。

**I2C 与"页（Page）"寄存器模型。** TLV320AIC3204 的寄存器地址只有 7 位（128 个），不够用，于是被分成 256 个"页"。每次读写寄存器前，先向第 0 页的 0 号寄存器写入页号，后续的寄存器访问都落在当前页上。本讲会看到 Page 0（时钟/数字/AGC）、Page 1（模拟路由/增益/电源）、Page 8/9（左右声道 ADC 的 mini-DSP 系数缓冲区）四种页。

**PGA、数字增益、音量的区别。** 信号从引脚进来到耳机出去，要经过一条增益链：输入路由电阻（决定源阻抗）→ MICPGA（模拟可变增益放大器）→ ADC（数字化）→ 数字音量 → DAC → 耳机驱动增益。靠前的环节在模拟域，直接影响信噪比和过载点；靠后的环节在数字域，只改变数值大小。理解"在链路的哪一端加增益"是射频/音频工程的共同基本功。

**AGC（自动增益控制）。** 接收机面对的信号强弱差可达 100dB 以上。AGC 是一个反馈环：测量输出电平，与"目标电平"比较，超过就压低增益、不足就抬高增益。压低要快（attack，防止强信号瞬间过载），恢复要慢（decay，防止话音间隙增益乱跑）。固件把这组参数打包在一个结构体里，通过 I2C 一次性下发给芯片——AGC 环路本身跑在芯片硬件里，不占用 STM32 的算力。

**定点系数。** 芯片内 mini-DSP 的滤波器系数是 24 位有符号定点数，取值范围 \([-(2^{23}), 2^{23}-1]\)，对应数学值 \([-1, 1)\)。换算方法是把浮点系数乘以 \(2^{23}\) 再取整。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tlv320aic3204.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c) | 编解码器驱动的全部实现：I2C 读写、配置表、时钟、增益、AGC、mini-DSP 系数下发 |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | 对外声明 `tlv320aic3204_*` 函数原型、`tlv320aic3204_agc_config_t` 结构体、粘滞标志位掩码 |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | 驱动的调用方：初始化顺序、`set_fs` 切换时序、`agc`/`gain`/`volume`/`imp`/`iqbal` 等 shell 命令、AGC 增益读回与功率补偿 |
| [python/TLV320AIC3204-1st-IIR-HPF.ipynb](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/TLV320AIC3204-1st-IIR-HPF.ipynb) | 用 scipy 设计 DC 抑制高通滤波器，并把 b0/b1/a1 换算成 24 位十六进制系数的 Jupyter notebook |

上一讲已经建立了整体信号链（天线→正交检波→codec ADC→STM32 DSP→DAC/LCD）；本讲深入其中的 codec 一环。下一讲（u2-l3）会从 I2S 接口继续往下走。

## 4. 核心概念与源码讲解

### 4.1 寄存器访问层与配置表编码

#### 4.1.1 概念说明

驱动一颗复杂芯片，八成代码其实是"按顺序往一堆寄存器里写值"。如果每条写入都写成一次函数调用，代码会被几百行 `write(0x04, 0x43);` 淹没。嵌入式固件的惯用解法是：**把寄存器写入序列编码成字节数组，用一个小循环解释执行**。数组的每条记录是 `(长度, 数据...)`，结尾放一个 0 作为哨兵（sentinel）终止符。这样一张表就是一段"寄存器写入程序"，既紧凑又容易和数据手册的推荐配置序列逐行对照。

#### 4.1.2 核心流程

```
tlv320aic3204_config(data):
    p = data
    while *p != 0:            # 哨兵 0 表示表结束
        len = *p++            # 取本条记录的字节数
        bulk_write(p, len)    # 一次 I2C 事务写出这 len 个字节
        p += len
```

每次 `bulk_write` 是一次完整的 I2C 传输：START → 芯片地址 0x18 → len 个数据字节 → STOP。由于表里每条记录都是 `(reg, data)` 两字节，所以 len 恒为 2；但格式本身允许更长的事务（比如一次写一串连续寄存器）。

#### 4.1.3 源码精读

最底层的三个 I2C 原语，注意它们都用 `i2cAcquireBus`/`i2cReleaseBus` 包裹——因为 ChibiOS 的 I2C 是共享总线（SI5351 也挂在上面，见 u2-l1）：

- [tlv320aic3204.c:L9-L17](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L9-L17)：`tlv320aic3204_write(reg, dat)`——写单个寄存器，组包 `{reg, dat}` 两字节发出。芯片从机地址 `AIC3204_ADDR = 0x18` 定义在 [tlv320aic3204.c:L5](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L5)。
- [tlv320aic3204.c:L19-L26](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L19-L26)：`tlv320aic3204_bulk_write`——一次事务发出任意长度的字节串，配置表解释器靠它工作。
- [tlv320aic3204.c:L28-L37](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L28-L37)：`tlv320aic3204_read(d0)`——先写一个字节（寄存器地址）再读一个字节，用于 AGC 增益、粘滞标志等状态回读。

配置表解释器本体只有 7 行：

- [tlv320aic3204.c:L40-L49](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L40-L49)：`tlv320aic3204_config(data)`——`while (*p)` 循环读长度字节、批量写出、前进指针，直到哨兵 0。

第一张表是 PLL 配置，也是"表格式寄存器程序"的标准样板：

- [tlv320aic3204.c:L51-L78](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L51-L78)：`conf_data_pll[]`。每行注释解释一个写入：`0x00←0x00` 切到 Page 0；`0x01←0x01` 软件复位；`0x04←0x43` 选择 PLL 时钟源为 MCLK；随后按 `REFCLK_8000KHZ` 条件编译写入 PLL 的 P/R/J/D 分频参数。文件开头的 `#define REFCLK_8000KHZ`（[tlv320aic3204.c:L4](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L4)）决定了 8MHz 这一组参数生效——这正是 SI5351 开机默认输出的频率，两颗芯片在此"握手"。

注意表里还有一种变体：4.5 节的 `adc_iir_filter_*` 表用的是 `(len, page, reg, data...)` 格式，由 `tlv320aic3204_config_adc_filter` 自己解释（见 4.5.3），并不经过上面的 `tlv320aic3204_config`。两种表格式并存，读代码时要看清解释器是谁。

#### 4.1.4 代码实践

**实践目标**：亲手"反汇编"一张配置表，验证你理解的编码格式与解释器一致。

**操作步骤**：

1. 打开 [tlv320aic3204.c:L51-L78](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L51-L78) 的 `conf_data_pll`。
2. 在纸上（或 PC 端写十行 Python）把字节数组按 `(len, ...)` 切片，还原出完整的 `(reg, value)` 写入序列。
3. 数一数：哨兵之前一共有多少条记录？每条的 len 是多少？

**需要观察的现象**：切出来的每条记录恰好是 2 字节 `(reg, data)`；第一条永远是 `0x00←页号`；最后 standalone 的 `0` 不会产生任何写入。

**预期结果**：`conf_data_pll` 在 `REFCLK_8000KHZ` 分支下共 8 条写入（Page 0 选择、软件复位、时钟源、PLL 电源/P/R、J、D 高字节、D 低字节），第 9 个字节是哨兵 0。

**待本地验证**：若用 Python 复现，把该数组抄进去跑一遍切片即可机械验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么哨兵可以安全地用 0？会不会出现某条合法记录的长度恰好是 0？

**答案**：每条记录至少要包含 `(reg, data)` 两个字节，所以合法的 len ≥ 2，长度 0 永远不会出现在数据中间，可以被无条件当作"表结束"标志。

**练习 2**：`tlv320aic3204_write` 和 `tlv320aic3204_bulk_write` 在总线行为上有什么区别？为什么配置表要用后者？

**答案**：前者每次调用完成一次完整的 I2C 事务（START/地址/2 字节/STOP），写 N 个寄存器就有 N 次事务；后者把多个字节合并进一次事务。合并可以减少总线仲裁和地址开销，也让"软件复位后紧跟一串初始化"的时序更紧凑。注意本驱动的配置表每条记录仍只有 2 字节，合并发生在"跨记录"层面并不存在——收益主要是代码组织而非速度；真正的长事务优势会在 4.5 节按系数连写时体现（那里反而用了多次单写）。

**练习 3**：`tlv320aic3204_write` 里对 `i2cMasterTransmitTimeout` 的返回值做了 `(void)` 强制丢弃。这种写法有什么隐患？

**答案**：I2C 出错（无应答、仲裁丢失、超时）会被静默吞掉，初始化"看起来成功"但芯片可能根本没配置上。对教学型固件这是可接受的简化；产品级固件应当至少计数错误并暴露诊断手段（本固件用 `stat` 命令读粘滞溢出标志是一种类似的思路，见 4.4.3）。

### 4.2 时钟树：从 8MHz 到 48/96/192kHz

#### 4.2.1 概念说明

音频 ADC 需要一个稳定的采样时钟。TLV320AIC3204 的时钟树是：**REFCLK/MCLK（8MHz，来自 SI5351）→ PLL 锁定到一个高频 → 多级整数分频 → DAC/ADC 采样时钟与 I2S 的 BCLK/WCLK**。PLL 把 86.016MHz 这么"怪"的频率造出来，是为了让各级分频之后恰好落在 48kHz 的整数倍上。采样率切换（48→96→192kHz）不改 PLL，只改分频比，因此切换快、抖动特性不变。

还有一个关键事实：**这颗 codec 是 I2S 主机**。它自己产生 BCLK（位时钟）和 WCLK（字时钟/帧同步），STM32 的 I2S 外设反过来做从机。这解释了为什么换采样率时必须先让 codec 停时钟、再复位 STM32 的 I2S（见 4.2.3）。

#### 4.2.2 核心流程

PLL 输出频率：

\[ f_{PLL} = \frac{f_{MCLK}}{P \cdot R} \times \left( J + \frac{D}{10000} \right) \]

代入本机参数 \( f_{MCLK} = 8\,\text{MHz} \)、\( P=R=1 \)、\( J=10 \)、\( D=7520 \)：

\[ f_{PLL} = 8\,\text{MHz} \times 10.7520 = 86.016\,\text{MHz} \]

DAC 采样率：

\[ f_s = \frac{f_{PLL}}{ NDAC \times MDAC \times DOSR } \]

48kHz 档代入 \( NDAC=2, MDAC=7, DOSR=128 \)：

\[ f_s = \frac{86.016\,\text{MHz}}{2 \times 7 \times 128} = 48\,\text{kHz} \]

BCLK 由独立的分频器给出，且恒为 \( 64 f_s \)（32 位 × 2 声道的 I2S 帧）：48kHz 档 \( BCLK_N = 28 \)，\( f_{BCLK} = 86.016/28 = 3.072\,\text{MHz} = 64 \times 48\,\text{kHz} \)。

三档采样率对照（按寄存器实际写入值计算，非注释文字）：

| 档位 | 表 | NDAC | MDAC | DOSR | BCLK_N | BCLK | ADC 侧 NADC×MADC×AOSR |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 48kHz | `conf_data_clk` | 2 | 7 | 128 | 28 | 3.072MHz | 2×7×128 |
| 96kHz | `conf_data_clk_96kHz` | 2 | 7 | 64 | 14 | 6.144MHz | 2×7×64 |
| 192kHz | `conf_data_clk_192kHz` | 2 | 7 | 32 | 7 | 12.288MHz | 1×7×64 |

三档都满足 \( f_s = 86.016/(\text{NDAC}\cdot\text{MDAC}\cdot\text{DOSR}) \)。另外 DAC 的处理块（PRB）随档位更换：48k 用 PRB_P25、96k 用 PRB_P8、192k 用 PRB_P17（注释标注 "reduce resource"，即换成省资源的结构）。

#### 4.2.3 源码精读

- [tlv320aic3204.c:L80-L97](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L80-L97)：`conf_data_clk[]`（默认 48kHz 档）。`0x0b←0x82` 上电 NDAC=2；`0x0c←0x87` 上电 MDAC=7；`0x0d/0x0e←0x00/0x80` 设 DAC OSR=128；`0x3c←25` 选 PRB_P25；**`0x1b←0x0c` 把 BCLK、WCLK 设为输出——这就是"codec 做 I2S 主机"的寄存器证据**；`0x1e←0x80+28` 上电 BCLKN=28；`0x12/0x13/0x14` 是 ADC 侧对应分频；`0x3d←0x01` 选 ADC 处理块 PRB_R1（这是 4.5 节 mini-DSP 滤波器能生效的前提）。
- [tlv320aic3204.c:L99-L114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L99-L114)：96kHz 档。注意 `0x12←0x82` 的注释写着 "NADC divider with value 7"，但寄存器值 0x82 的低 7 位是 **2**——按 2×7×64=96kHz 才能算通，注释是笔误。这提醒我们：读这类代码要以寄存器值 + 数据手册为准，注释只是线索。
- [tlv320aic3204.c:L116-L132](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L116-L132)：192kHz 档。ADC 侧改为 NADC=1（`0x12←0x81`）。
- [tlv320aic3204.c:L197-L211](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L197-L211)：`tlv320aic3204_set_fs(fs)`——校验 fs 只能取 48/96/192，选择对应时钟表下发，等待 10ms 让分频器稳定，再执行 `conf_data_unmute` 重新上电 ADC/DAC 通道。
- 停机表 [tlv320aic3204.c:L174-L181](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L174-L181)：`conf_data_divoff[]` 先关 ADC 通道、再依次关 NDAC/MDAC/NADC/MADC 分频器——分频器一停，作为主机的 BCLK/WCLK 也就停了。
- 切换时序在调用方：[main.c:L203-L226](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L203-L226) 的 `set_fs()`。顺序是：`tlv320aic3204_stop()`（停 codec 时钟）→ `i2sStopExchange`（停 STM32 I2S DMA）→ 睡 40ms（注释吐槽 20ms 不够）→ `i2sStartExchange` 重启 DMA → `tlv320aic3204_set_fs(fs)` 下发新分频并解除静音。为什么这个顺序？因为时钟源在 codec 一侧：必须先停时钟再动从机，否则从机会采样到半截波形。
- 上电初始化 [tlv320aic3204.c:L183-L190](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L183-L190)：`tlv320aic3204_init()` 按 PLL → 时钟 → 路由 → 等 40ms（给内部基准电容充电，对应路由表里 "REF charging time 40ms"）→ 解静音 的顺序执行。它在 [main.c:L1007](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1007) 被调用，紧跟其后才启动 I2S（[main.c:L1009-L1012](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1009-L1012)）——主机先出时钟，从机再进场。

#### 4.2.4 代码实践

**实践目标**：用 PC 端计算验证三张时钟表，体会"寄存器值 → 频率"的换算。

**操作步骤**：

1. 写一个十几行的 Python 函数：输入 `(ndac, mdac, dosr, bclkn)`，输出 \( f_s \) 和 \( f_{BCLK} \)。
2. 把上面表格中三档的参数喂进去（PLL 固定 86.016MHz）。
3. 再验证 4.1 表里 `REFCLK_12000KHZ` 分支的注释：\( 12\,\text{MHz} \times 7.1680 = 86.016\,\text{MHz} \)，且 \( J=7, D=1680 \) 对应 \( 7 + 1680/10000 = 7.168 \)。

**需要观察的现象**：三档算出的 \( f_s \) 应分别为 48000、96000、192000（整差），\( f_{BCLK} = 64 f_s \) 恰好成立。

**预期结果**：全部吻合；12MHz 分支的 J/D 与注释互洽。若想进一步验证 BCLK 与 I2S 帧长关系，可结合下一讲 `AUDIO_BUFFER_LEN` 与 5ms 回调周期一起推。

**待本地验证**：无硬件也可完成本实践，纯计算即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 48k 与 192k 档的 DOSR 一个是 128 一个是 32？OSR（过采样率）变小对 ADC 意味着什么？

**答案**：分频链要维持 \( f_s = f_{PLL}/(NDAC \cdot MDAC \cdot DOSR) \)，\( f_s \) 翻 4 倍而 NDAC/MDAC 不变，DOSR 就得除以 4。OSR 是 Σ-Δ ADC 的过采样倍数：OSR 越高，量化噪声被推得离音频带越远、带内信噪比越好；OSR 降低换来了带宽（192kHz 采样覆盖 96kHz 带宽），代价是噪底抬升。

**练习 2**：如果不调用 `tlv320aic3204_stop()` 直接改时钟寄存器，可能会发生什么？

**答案**：BCLK/WCLK 由本芯片产生，改分频器瞬间输出时钟可能毛刺或频率跳变，做从机的 STM32 I2S 可能采样到非法帧、DMA 拿到错位数据。先停时钟再切换，是从"确定无输出"的状态开始重新建立时序。

**练习 3**：`set_fs()` 中 40ms 的等待为什么写在 `i2sStopExchange` 之后、`i2sStartExchange` 之前？

**答案**：给 codec 侧时钟完全停止留出时间（代码注释注明实测 20ms 不够）。若不等就重启 I2S，从机在主机尚未输出时钟时启动交换，帧同步会处于未定义状态。这是"时钟源在对方"的系统中典型的握手式重启。

### 4.3 模拟路由、增益与音量控制

#### 4.3.1 概念说明

`conf_data_routing` 这张表干的是"接线"的活：把左右声道的 DAC 输出接到耳机驱动器（HPL/HPR），把外部输入引脚 IN2/IN3 通过可选电阻接到 MICPGA 的正负输入，再给每级设定初始增益并上电。理解它的钥匙是分清三个域：

- **模拟输入侧（Page 1 的路由寄存器）**：IN2L→LEFT_P、IN2R→LEFT_N、IN3R→RIGHT_P、IN3L→RIGHT_N。I 路和 Q 路各占一个差分对。路由代码（1/2/3）同时决定接入电阻（源码注释确认代码 1 = 10kΩ；2/3 按数据手册惯例对应更大阻值，待对照确认）——这就是 `imp` 命令改"输入阻抗"的原理。
- **MICPGA 增益（0x3b/0x3c，0~95）**：模拟增益，对应 shell 的 `gain` 第一个参数，也即 `uistat.rfgain`（"射频增益"实际落在这颗音频芯片上）。
- **耳机驱动增益（0x10/0x11）与数字音量**：前者是 `volume` 命令（−6~29dB，低于 −6dB 写 0x40 进静音位），后者是 DAC 数字域音量（0x40←0x00 解除静音）。

另外还有一组 ADC 数字增益（0x53/0x54，−24~40dB）和精细调节（相位 0x55、细增益 0x52），用于 IQ 两路的微小失配校正。

#### 4.3.2 核心流程

上电路由流程（`conf_data_routing` 的逻辑顺序）：

```
切到 Page 1
  ├─ 关内部粗糙 AVdd LDO（用外部供电时）
  ├─ 使能主模拟电源；设基准充电时间 40ms
  ├─ 耳机 pop 抑制（软步进参数）
  ├─ 设共模电压：输入 0.9V / 耳机输出 1.65V
  ├─ 左右 DAC → HPL/HPR；HPL/HPR 增益 0dB；上电耳机驱动
  ├─ 输入路由：IN2→左差分对、IN3→右差分对（10kΩ）
  ├─ MICPGA 解静音并设初始增益（写入 72）
  └─ 上电 MIC 偏置 2.5V
切到 Page 8 → 使能自适应滤波模式（见 4.5）
```

运行期四个独立接口都遵循同一模式：**切页 → 写寄存器 → 切回 Page 0**。

#### 4.3.3 源码精读

- [tlv320aic3204.c:L134-L162](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L134-L162)：`conf_data_routing[]` 全表。几个关键行：`0x0c←0x08 / 0x0d←0x08` 把左右 DAC 各自路由到同侧耳机驱动；`0x0a←0x33` 设输入共模 0.9V、耳机共模 1.65V；`0x34←0x10`（= 路由代码 1 左移 4 位到该寄存器的 IN2L 位域）把 IN2L 以 10kΩ 接到 LEFT_P，`0x36/0x37/0x39` 同理；`0x3b←72 / 0x3c←72` 解静音左右 MICPGA 并设初值（注释称对应 32dB 增益使通道增益为 0dB；按数据手册 0.5dB/步换算 72≈36dB，注释与换算的差可能与路由电阻分压有关，**待确认**，请对照数据手册）；`0x33←0x60` 上电 2.5V MIC 偏置；表尾 `0x00←0x08 / 0x01←0x04` 切 Page 8 开自适应滤波，为 4.5 节做铺垫。
- [tlv320aic3204.c:L164-L172](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L164-L172)：`conf_data_unmute[]`——上电左右 DAC（0x3f）与 ADC（0x51）通道、解除数字音量静音（0x40/0x52）、配置耳机插入检测去抖（0x43）。它是 `init` 与 `set_fs` 收尾共用的一张表。
- [tlv320aic3204.c:L213-L222](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L213-L222)：`tlv320aic3204_set_impedance(imp)`——`imp &= 3` 取 1~3 档，分别移位到 0x34/0x36 的 `[7:4]` 位域和 0x37/0x39 的 `[3:2]` 位域（两对寄存器位域位置不同，这正是移位量一个是 `<<4` 一个是 `<<2` 的原因）。shell 侧入口是 [main.c:L469-L479](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L469-L479) 的 `cmd_impedance`。
- [tlv320aic3204.c:L225-L236](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L225-L236)：`tlv320aic3204_set_gain(g1, g2)`——夹取 0~95 后写左右 MICPGA 增益寄存器 0x3b/0x3c。shell 的 `gain` 命令（[main.c:L481-L504](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L481-L504)）第一个参数落到这里，同时更新 `uistat.rfgain`。
- [tlv320aic3204.c:L238-L248](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L238-L248)：`tlv320aic3204_set_digital_gain(g1, g2)`——夹取 −24~40dB，`&0x7f` 后写 ADC 数字音量 0x53/0x54（Page 0）。注意第二个参数可以加 `adjust` 偏移（见 `cmd_gain` 的三参数形式），用于 IQ 幅度微调；dB 与寄存器值的精确映射请对照数据手册（待确认）。
- [tlv320aic3204.c:L250-L263](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L250-L263)：`tlv320aic3204_set_volume(gain)`——耳机音量 −6~29dB；低于 −6dB 写 0x40（该寄存器的静音位），否则 `gain &= 0x3f` 写 0x10/0x11。（函数内注释 "Unmute Left MICPGA" 是复制粘贴残留，实际写的是耳机驱动增益。）shell 入口 [main.c:L542-L554](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L542-L554)。
- IQ 微调接口：[tlv320aic3204.c:L428-L431](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L428-L431) 的 `set_adc_phase_adjust`（写 0x55，两声道采样相位微调）与 [tlv320aic3204.c:L433-L436](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L433-L436) 的 `set_adc_fine_gain_adjust`（0x52 高低半字节各放一路 ±dB 级细增益），对应 shell 的 `phase`/`finegain` 命令。

#### 4.3.4 代码实践

**实践目标**：把 `conf_data_routing` 中 5 条关键写入各翻译成一句话（路由 / 增益 / 电源三类），建立"寄存器字节 ↔ 硬件行为"的翻译能力。这正是本讲规格指定的主实践的前半部分。

**操作步骤**：

1. 打开 [tlv320aic3204.c:L134-L162](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L134-L162)。
2. 选出这 5 条：`2, 0x0c, 0x08`、`2, 0x34, 0x10`、`2, 0x3b, 72`、`2, 0x09, 0x30`、`2, 0x33, 0x60`。
3. 对照 TLV320AIC3204 数据手册（TI 官网可下载）Page 1 寄存器表，为每条写一句话说明，标注它属于路由、增益还是电源类。
4. （可选，需硬件）用 `imp 1`、`imp 2`、`imp 3` 切换输入阻抗，观察弱信号台声音大小的变化，验证"接入电阻越大、送到 PGA 的信号越弱"。

**需要观察的现象**（第 4 步）：阻抗档位调大后同样的电台声音变小——因为分压比变了。

**预期结果**（参考翻译，供你核对）：

| 写入 | 类别 | 一句话说明 |
| --- | --- | --- |
| `0x0c←0x08` | 路由 | 把左声道 DAC 输出接到左耳机驱动器 HPL |
| `0x34←0x10` | 路由 | 把 IN2L 引脚经 10kΩ 电阻接到左 MICPGA 正输入 LEFT_P |
| `0x3b←72` | 增益 | 解除左 MICPGA 静音并设初始增益初值 72 |
| `0x09←0x30` | 电源 | 同时上电左右耳机驱动器 HPL/HPR |
| `0x33←0x60` | 电源 | 使能 2.5V MIC 偏置电压 |

**待本地验证**：涉及具体 dB 数值的部分以数据手册为准。

#### 4.3.5 小练习与答案

**练习 1**：`volume` 低于 −6dB 时为什么写 0x40 而不是写更大的负数？

**答案**：0x10/0x11 耳机驱动增益寄存器的线性范围只到 −6dB，再小没有编码；位 6（0x40）是静音位。所以"更小"只能直接静音，用一个条件分支实现软下限。

**练习 2**：`set_impedance` 里同一个 `imp` 为什么一处 `<<4`、一处 `<<2`？

**答案**：0x34/0x36 寄存器中 IN2 路由到 LEFT_P/LEFT_N 的位域在 `[7:4]`，而 0x37/0x39 中 IN3 路由到 RIGHT_P/RIGHT_N 的位域在 `[3:2]`。同一位模式在不同寄存器里位置不同，移位量必须分别匹配——这也解释了路由表初值一个是 0x10、一个是 0x04。

**练习 3**：为什么"射频增益"（`uistat.rfgain`）最终写进了一颗音频编解码器？

**答案**：CentSDR 的正交检波器把射频直接搬到音频基带，天线后的第一级可调增益器件就是这颗 codec 的 MICPGA。命名沿用了收信机传统（RF gain），物理上它控制的是基带模拟增益。这是"零中频/直采架构改变增益链归属"的好例子。

### 4.4 AGC：配置结构体、档位与增益读回

#### 4.4.1 概念说明

AGC 的全部可调参数被建模成一个 C 结构体 `tlv320aic3204_agc_config_t`（定义在 nanosdr.h），并通过指针传给 `tlv320aic3204_agc_config()`。传 `NULL` 表示关闭 AGC（控制寄存器写 0）。各字段物理含义：

| 字段 | 位宽/范围 | 物理含义 |
| --- | --- | --- |
| `target_level` | 3 位，0~7 | 目标输出电平档：AGC 试图把 ADC 输出电平维持在该档对应的 dB 值（档位到 dB 的映射见数据手册，待对照确认） |
| `gain_hysteresis` | 2 位，0~3 | 增益迟滞：电平在目标附近小幅波动时不立即动作，防止增益来回抖动 |
| `attack` + `attack_scale` | 5 位 + 3 位 | 起控（压增益）时间及其时间刻度因子：信号突然变强时增益下降的速度 |
| `decay` + `decay_scale` | 5 位 + 3 位 | 恢复（升增益）时间及刻度：信号变弱后增益爬回的速度 |
| `maximum_gain` | 0~116（usage 提示） | AGC 可抬升的最大增益上限，0.5dB/步 |

直觉版理解：`target_level` 是"想要的音量"，`attack`/`decay` 是"手劲多快"，`maximum_gain` 是"最多放大多少倍"，`gain_hysteresis` 是"别一惊一乍"。

固件还定义了四档预设：`manual / slow / mid / fast`。看下面的源码会发现，四档之间**唯一的差别是 decay 参数**。

#### 4.4.2 核心流程

`tlv320aic3204_agc_config(conf)` 的寄存器映射（左右声道各一套，左 0x56~0x5d、右 0x5e~0x65）：

```
若 conf == NULL:
    0x56/0x5e ← 0            # 关闭左右 AGC，直接返回
否则:
    ctrl = 0x80               # bit7: AGC 使能
         | target_level << 4  # bit6:4
         | gain_hysteresis    # bit1:0
    0x56/0x5e ← ctrl
    0x59/0x61 ← (attack << 3) | attack_scale
    0x5a/0x62 ← (decay << 3) | decay_scale
    0x58/0x60 ← maximum_gain
```

读回路径：寄存器 0x5d/0x65 是当前 AGC 增益标志，单位 0.5dB。`stat` 命令直接打印它；`measure_power_dbm()` 用它把被 AGC 压掉的增益加回功率读数。

#### 4.4.3 源码精读

- [nanosdr.h:L56-L64](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L56-L64)：`tlv320aic3204_agc_config_t` 结构体定义——七个 int 字段即上表。它在 [nanosdr.h:L289-L299](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L289-L299) 被整个嵌进 `config_t`，因此 AGC 参数随 Flash 配置一起掉电保存（见 u4-l5）。
- [nanosdr.h:L66-L84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L66-L84)：本驱动全部对外原型的声明处（`init`/`set_gain`/`set_digital_gain`/`set_volume`/`agc_config`/`set_fs`/`stop`/`config_adc_filter`/阻抗与读回接口），是 nanosdr.h"按来源模块分节"组织方式的一节。
- [tlv320aic3204.c:L265-L292](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L265-L292)：`tlv320aic3204_agc_config()`——按上面伪代码逐组写寄存器；注意 `ctrl == 0`（即传 NULL）时写完控制寄存器就返回，其余时间/增益寄存器不动。
- [main.c:L630-L655](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L630-L655)：`set_agc_mode()`。manual 档传 NULL 关 AGC；fast/mid/slow 三档分别设 `(decay=0, scale=0)`、`(7, 0)`、`(31, 4)` 后统一调 `tlv320aic3204_agc_config(&config.agc)`。**attack 从不被档位修改**（默认配置里 `attack=0`），因为压增益要快是共识，收音场景只需调节"恢复多慢"。
- [main.c:L120-L126](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L120-L126)：出厂默认 `config.agc`——`target_level = 6`、`maximum_gain = 127`，其余字段为 0。注意 127 超出 `agc` 命令 usage 提示的 0~116 范围，写入芯片后的实际效果建议对照数据手册或实测确认（**待确认**）。
- [main.c:L580-L628](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L580-L628)：`cmd_agc`——子命令 `manual/slow/mid/fast/enable/disable`、参数子命令 `level/hysteresis/attack/decay/maxgain`，全部修改 `config.agc` 后整体重下发。注意前缀匹配用 `strncmp` 两三个字符（如 `"sl"` 也能匹配 slow），这是上一讲见过的 shell 命令习惯。
- [tlv320aic3204.c:L418-L426](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L418-L426)：`tlv320aic3204_get_left_agc_gain()` / `get_right_agc_gain()`——读 0x5d/0x65，返回 `int8_t`。
- [main.c:L423-L438](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L423-L438)：`cmd_stat` 末尾读出并打印 `agc gain: %d %d`——本讲实践用它观察 AGC 行为。
- [main.c:L380-L393](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L380-L393)：`measure_power_dbm()`——功率补偿的精髓：手动模式用 `uistat.rfgain`，AGC 模式改用**读回的当前 AGC 增益**，然后 `dbm -= (agcgain << 7)`。`<<7` 即 ×128，因为在 8.8 定点里 0.5dB（AGC 步进）= 128；整个表达式 \( dbm = 6\log_2(rms) - 0.5\,g_{agc} - 116 \) 把"AGC 压了多少增益"从测量值中扣回来，让功率计显示的是天线口的真实电平。
- 相关的溢出监测：[tlv320aic3204.c:L413-L416](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L413-L416) 读粘滞标志寄存器 0x2a，[nanosdr.h:L86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L86) 定义 `AIC3204_STICKY_ADC_OVERFLOW 0x0c`（左右 ADC 溢出两位），[main.c:L918-L922](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L918-L922) 的 Thread2 每 10ms 轮询一次累加 `stat.overflow_count`——增益链调得太过头时，这是最先亮起的红灯。
- 开机时机：[main.c:L1045-L1046](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1045-L1046) 在 UI 初始化后调用 `update_iqbal()` 与 `update_agc()`（[main.c:L241-L245](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L241-L245)），后者按 `uistat.agcmode`（从 Flash 恢复，默认 `AGC_MID`，见 [main.c:L136](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L136)）下发 AGC。模式名到字符串的对照表在 [main.c:L109-L111](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L109-L111)。

#### 4.4.4 代码实践

**实践目标**：通过 shell 的 `agc` 与 `stat` 命令，观察 slow/fast 两档下 AGC 增益读数的差异，把 4.4.1 的参数表落到可测量的行为上。这是本讲规格指定的主实践的后半部分。

**操作步骤**（需要硬件；无硬件替代方案见后）：

1. 连接 USB，用串口终端（或 `python/centsdr.py`）进入 shell，`tune` 到一个本地强台（如默认信道 0 的 567kHz AM）。
2. `agc slow`，等待几秒让环路稳定，执行 `stat`，记下 `agc gain:` 后的两个数（左右声道）。
3. `agc fast`，重复 `stat` 记录。
4. `agc manual`，再 `stat` 一次作为基线。
5. 进阶：`agc decay 15 2` 直接改参数后再 `agc fast`，看读数是否按 fast 的 `(0,0)` 覆盖。

**需要观察的现象**：切换档位后 `agc gain` 读数不同；slow 档下手动拧 `gain` 或信号起伏后读数变化缓慢，fast 档下几乎立即跟上；manual 档下读数不再反映真实增益（AGC 已关）。左右两路读数应接近（同一颗芯片两套相同配置的环）。

**预期结果**：得到一张 three-row 记录表（manual / slow / fast × 左右增益）。由于读数取决于当时的信号强度，绝对值因环境而异，重点观察的是**变化速度与档位的对应关系**。

**无硬件替代方案**：在 PC 上把 [main.c:L630-L655](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L630-L655) 的 `set_agc_mode` 抄成一个独立 C 函数，桩替换 `tlv320aic3204_agc_config` 为"打印寄存器字节"的假函数，调用四种模式，验证输出与 4.4.2 的寄存器映射逐位一致。

**待本地验证**：带硬件部分需实测。

#### 4.4.5 小练习与答案

**练习 1**：为什么 fast/mid/slow 只调 `decay` 不调 `attack`？

**答案**：AGC 首要职责是防过载。强信号一来必须立刻压增益（attack 快是安全需求），而恢复速度则是收听体验问题：太快会让衰落间隙的噪声被放大，太慢会跟不上信号回升。所以预设档位只在 decay 一个维度上分化——fast `(0,0)`、mid `(7,0)`、slow `(31,4)`。

**练习 2**：`measure_power_dbm()` 里为什么手动模式用 `uistat.rfgain`、AGC 模式用读回值？两者含义有何不同？

**答案**：功率计需要知道"从天线到 ADC 之间总共加了多少增益"才能反推入口电平。手动模式增益就是人为设定的 `rfgain`，是静态已知量；AGC 模式下增益由芯片环路动态决定，固件并不知道，只能读 0x5d/0x5d 实时回显。一个是设定值，一个是实测值。

**练习 3**：`agcgain << 7` 为什么等价于 0.5dB？

**答案**：功率值采用 8.8 定点（低 8 位是小数），1dB = 256 = \(2^8\)。AGC 步进是 0.5dB，对应 \( 256 \times 0.5 = 128 = 2^7 \)，故左移 7 位。与 [main.c:L388-L391](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L388-L391) 的 `6 * log2_q31(...)`（6dB/bit）和 `116 << 8`（116dB 整数部分）同一套定点记法。

### 4.5 芯片内 mini-DSP：DC 抑制与 IQ 平衡的一阶 IIR

#### 4.5.1 概念说明

TLV320AIC3204 的 ADC 处理块 PRB_R1 里藏着一个可编程的一阶 IIR 滤波器，系数放在 Page 8（左声道，C4~C6）和 Page 9（右声道，C36~C38）的系数缓冲区里。CentSDR 用它做两件事：

1. **DC 抑制（高通）**：零中频接收的 IQ 里混着直流偏移（本振泄漏、ADC 失调），不滤掉会把后面 DSP 的低频段糊住。一个截止频率 2.4Hz 的一阶高通就能去掉直流而几乎不碰语音频段。
2. **IQ 平衡/频谱倒置**：把右声道（Q 路）滤波器的两个分子系数同乘一个系数 `adj`，就在不改变滤波功能的前提下给这一路加了幅度微调（甚至乘 −1 实现倒相）。I/Q 两路幅度或极性失配会引起镜像信号，这个技巧用同一组寄存器顺手把它修了。

设计流程不在固件里做，而在 `python/TLV320AIC3204-1st-IIR-HPF.ipynb` 里完成——这是 CentSDR"算法在 PC 上设计、系数落到固件"工作流的最小实例（u5-l5 会推广到 SSB/CW 滤波器）。

#### 4.5.2 核心流程

滤波器设计链路（notebook 逐格对应）：

\[ H(z) = \frac{b_0 + b_1 z^{-1}}{1 - a_1 z^{-1}} \quad\left(a_1 \text{ 为正时对应低频截止}\right) \]

1. `signal.iirfilter(1, [0.0001], btype='highpass')` → \( b = [0.99984295, -0.99984295] \)，\( a = [1, -0.99968589] \)。归一化截止 0.0001（×fs/2），fs=48kHz 时 \( f_c = 2.4\,\text{Hz} \)。
2. 量化到 24 位定点：系数 × \(2^{23}\) 并取整 → `b0=8387291, b1=-8387290, a1=-8385973`。
3. 变成芯片要的三字节十六进制：`0x7ffada, 0x800526, 0x7ff5b5`。**注意 a1 取了负号**（notebook 里 `to_hex(-al[1])`）——scipy 的分母是 \(1 - a_1 z^{-1}\)，而芯片系数按累加方向定义，符号约定相反。
4. 固件里写成 4 字节（3 字节系数 + 0x00 填充，即 32 位表示 `0x7ffada00`），从 Page 8 Reg 24（左）/Page 9 Reg 32（右）开始连写。

`adj` 的作用：右声道 \( b_0' = b_0 \cdot adj,\ b_1' = b_1 \cdot adj \)。高频增益 ≈ \( adj \)（分子分母同度缩放，只有整体增益变了，滤波形状不变）。

#### 4.5.3 源码精读

- [tlv320aic3204.c:L294-L311](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L294-L311)：`adc_iir_filter_dcreject[]`——第一版 DC 抑制系数表。开头注释标明格式 `/* len, page, reg, data.... */`：`12, 8, 24` 表示"12 个数据字节，Page 8，从寄存器 24 起"，随后正是 notebook 算出的 `0x7f,0xfa,0xda,0x00 / 0x80,0x05,0x26,0x00 / 0x7f,0xf5,0xb5,0x00`；右声道 `12, 9, 32` 起同值。
- [tlv320aic3204.c:L313-L330](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L313-L330)：`adc_iir_filter_dcreject2[]`——第二版，**右声道系数不同**（`0x80,0x06,0x37...`），这是给 IQ 平衡留出的微调版本，也是 `tlv320aic3204_config_adc_filter(1)` 实际启用的表。
- [tlv320aic3204.c:L332-L348](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L332-L348)：`adc_iir_filter_default[]`——旁路表：C4=0x7fffff、C5=C6=0，即全通直通，用于关闭滤波。
- [tlv320aic3204.c:L350-L367](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L350-L367)：`tlv320aic3204_config_adc_filter(enable)`——第二种表格式的解释器：读 `len/page/reg` 三元组，切页后按寄存器自增连写 len 个字节（每字节一次单写事务）；收尾写 `Page 8 / 0x01←0x05` 让系数缓冲区"在下一帧边界切换"——**实时切换滤波器而不打断音频流**的关键机制。shell 入口 `dcreject {0|1}` 在 [main.c:L556-L565](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L556-L565)。
- [tlv320aic3204.c:L369-L411](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L369-L411)：`tlv320aic3204_config_adc_filter2(double adj)`——运行时版本。左声道写死 `b0=0x7ffada00, b1=0x80052600, a1=0x7ff5b500`；右声道先 `b0 *= adj; b1 *= adj` 再写。每个系数拆成 4 次单字节写（`>>24/>>16/>>8/0`）。同一个函数承担"DC 抑制 + IQ 增益微调 + 倒相"三重身份。
- [main.c:L234-L239](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L234-L239)：`update_iqbal()`——`adj = config.freq_inverse - uistat.iqbal/10000.0`。默认 `freq_inverse = -1`（[main.c:L161](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L161)），开机 `adj = -1`，即右声道整体倒相——用来补偿正交检波器输出频谱倒置；`iqbal` 命令（[main.c:L532-L540](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L532-L540)）在 ±10000 刻度上微调幅度。`update_iqbal()` 在 [main.c:L1045](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1045) 开机即被调用——**倒相补偿是常开的**（[main.c:L1047-L1048](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1047-L1048) 的旧式 `config_adc_filter` 调用已被注释停用）。
- 设计源头：[python/TLV320AIC3204-1st-IIR-HPF.ipynb](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/TLV320AIC3204-1st-IIR-HPF.ipynb)。注意第 8 格用 `np.vectorize(int)`（向零截断）而非四舍五入，所以 `b1` 的 24 位补码是 `0x800526` 而非四舍五入的 `0x800525`——与固件字节严丝合缝，这个小细节可以证明固件系数确实出自这份 notebook。

#### 4.5.4 代码实践

**实践目标**：亲手复现 notebook 的系数换算链，验证"scipy → 24 位补码 → 固件字节"每一环。

**操作步骤**：

1. 在装了 numpy/scipy 的 Python 环境里运行 notebook 的核心三行：

   ```python
   from scipy import signal
   b, a = signal.iirfilter(1, [0.0001], btype='highpass')
   al = [int(x * 2**23) for x in a]
   bl = [int(x * 2**23) for x in b]
   ```

2. 用 notebook 第 9-11 格同样的 `to_bytes`/`to_hex`（或自己写：取 24 位补码的低 3 字节）打印 `bl[0]`、`bl[1]`、`-al[1]` 的十六进制。
3. 与 [tlv320aic3204.c:L298-L302](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L298-L302) 的字节逐个对照。
4. 把截止频率换成 `0.001`（即 fs=48kHz 时 24Hz），重新生成一组系数，回答：这组新系数如果写进芯片，人声会怎样？

**需要观察的现象**：第 2 步输出应为 `0x7f,0xfa,0xda`、`0x80,0x05,0x26`、`0x7f,0xf5,0xb5`——与固件完全一致。

**预期结果**：换 `0.001` 后 \( f_c=24\,\text{Hz} \)，仍低于语音基带，理论上是把低频哼声压得更狠；代价是对 30~50Hz 附近的分量开始有影响，且一阶高通每倍频程只衰减 6dB，滚降很缓。

**待本地验证**：第 4 步的听感结论需上机实验；换算部分纯 PC 可验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `a1` 在写入芯片前要取负，而 `b0/b1` 不用？

**答案**：scipy 的传递函数是 \( H(z) = (b_0 + b_1 z^{-1}) / (1 - a_1 z^{-1}) \)，分母里是"减"；芯片的差分方程按 \( y[n] = b_0 x[n] + b_1 x[n-1] + a_1 y[n-1] \) 的"加"约定存系数。两种约定相差一个符号，所以 \( a_1 \) 必须取反，分子系数不受影响。

**练习 2**：`update_iqbal()` 里 `adj = -1` 时右声道发生什么？为什么这能修正频谱倒置？

**答案**：`b0、b1` 同乘 −1，右声道（Q 路）输出整体反相。对复信号 \( I + jQ \) 而言，Q 取反等价于共轭 \( I - jQ \)，在频域表现为正负频率互换（频谱镜像翻转）。当正交检波器本振相位接反导致解出的频谱倒置时，这一操作恰好把它翻回来。

**练习 3**：为什么收尾要写 `Page 8 / Reg 0x01 ← 0x05`（系数在下一帧边界切换），而不是立即生效？

**答案**：ADC 正在实时采样，若系数在两个样本之间被换掉，正在计算中的差分方程会前后不一致，产生一个毛刺。让新旧系数缓冲区在帧边界原子交换，保证滤波器状态与系数始终配套，音频流无感切换。

## 5. 综合实践

**任务：给 CentSDR 的增益链做一次"全景体检"。**

把本讲四个模块（配置表、时钟、增益路由、AGC）串成一条完整的实验线。有硬件走 A 线，无硬件走 B 线。

**A 线（需要硬件）**：

1. **基线**：`agc manual`、`gain 0`，`stat` 记录 `agc gain` 与 `rms`，`power` 记录功率读数。
2. **PGA 增益刻度**：依次 `gain 20 / 40 / 60 / 95`，每步记录 `power` 的变化量（dBm），推断 MICPGA 每寄存器步长对应多少 dB，并与你查数据手册得到的结论对照。
3. **AGC 闭环**：保持强台，`agc slow` 等 10 秒记录读数；`agc fast` 再记录；解释两档读数稳定性差异。
4. **AGC 补偿验证**：比较第 3 步与第 2 步相同音量下 `power` 的读数——`measure_power_dbm` 是否真的把 AGC 压掉的增益扣回去了（读数应接近手动模式同增益时的值）。
5. **溢出监测**：`gain 95` 加强台，反复 `stat` 观察 `overflow` 是否开始增长；若增长，说明增益链过载，回到 `gain 40`。
6. 把以上数据整理成一张表，写一段结论：这台机器"安全增益区间"在哪里。

**B 线（纯 PC）**：

1. 写一个约 60 行的 Python 脚本 `aic_decode.py`：内置 4.1 节格式的解析器，把 `conf_data_pll`、`conf_data_clk`、`conf_data_routing` 三张表从源文件中抄入，反汇编成 `(页, 寄存器, 值)` 清单并按页分组打印。
2. 扩展同款解析器支持 `(len, page, reg, data)` 格式，反汇编 `adc_iir_filter_dcreject2`，并把 12 字节数据重组回三个 24 位系数（小端字节序、最高字节在前），换算回浮点（÷\(2^{23}\)），与 4.5 节 notebook 的 \( b/a \) 值对照。
3. 输出应能机械验证本讲正文的所有表格。

**预期结果**：A 线得到增益刻度与 AGC 行为的实测数据；B 线得到与正文完全一致的反汇编清单和系数。两线都完成后，你应该能不查讲义、只凭数据手册和源码，说出任何一个 shell 增益命令最终落在哪颗芯片的哪个寄存器。

## 6. 本讲小结

- TLV320AIC3204 的初始化被编码成若干 `(len, reg, data)` 哨兵结尾的字节表，由 [tlv320aic3204_config()](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L40-L49) 解释执行；mini-DSP 系数表另用 `(len, page, reg, data)` 格式。
- 时钟树为 8MHz MCLK ×10.752 = 86.016MHz PLL，再经 NDAC×MDAC×OSR 分频得到 48/96/192kHz，BCLK 恒为 64fs；codec 是 I2S 主机，切采样率必须先停它的时钟再复位 STM32 的 I2S。
- 增益链横跨模拟与数字域：输入路由电阻（`imp`）→ MICPGA 0~95（`gain`，即"射频增益"）→ ADC 数字增益 −24~40dB → DAC 数字音量 → 耳机驱动 −6~29dB（`volume`），各接口都是"切页→写寄存器→切回"。
- AGC 参数由 `tlv320aic3204_agc_config_t` 建模、整组下发，fast/mid/slow 三档只差 decay；当前增益可从 0x5d/0x65 读回，并被 `measure_power_dbm()` 以 0.5dB/步补偿进功率计。
- 芯片内 mini-DSP 的一阶 IIR 一箭三雕：DC 抑制（scipy notebook 设计的 2.4Hz 高通，系数为 24 位补码、a1 取反）、右声道幅度微调（`adj` 乘分子系数）与开机常开的频谱倒置补偿（`adj=-1`）。

## 7. 下一步学习建议

- **下一讲（u2-l3）**：顺着 BCLK/WCLK 往 STM32 一侧走——I2S DMA 双缓冲、每 5ms 的 `i2s_end_callback`、`signal_process` 函数指针如何消费本讲配置出的 IQ 样本流。你会看到 `AUDIO_BUFFER_LEN=480` 与本讲的 48kHz 采样率正好构成 5ms 周期。
- **往回复习**：若对"codec 是 I2S 主机、MCLK 来自 SI5351"的时钟关系还有模糊，回到 u2-l1 看 8MHz 默认输出的来源。
- **继续阅读源码**：通读 [tlv320aic3204.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c) 全文（仅 456 行），重点补看本讲未展开的 [tlv320aic3204_beep()](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L438-L456)（被 [ui.c:L262](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L262) 在按键时调用，用芯片自带正弦发生器发提示音）。
- **对照数据手册**：TI 官网的 TLV320AIC3204 Application Reference Guide 是本讲所有"待确认"项的最终裁决；建议重点翻 Page 0 的 AGC 寄存器一节和 Page 1 的模拟路由一节。
