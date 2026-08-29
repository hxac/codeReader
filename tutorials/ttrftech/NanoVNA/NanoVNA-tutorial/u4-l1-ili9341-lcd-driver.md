# ili9341 LCD 驱动：SPI、DMA 与字体渲染

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `ili9341_init()` 的初始化流程：SPI1 外设寄存器配置、复位时序、长度前缀式的初始化命令表。
2. 理解「命令/数据」双相位 SPI 传输协议：DC 引脚如何区分命令字节与像素数据，以及 `send_command()` 中为什么要等待 `SPI_IS_BUSY`。
3. 掌握 `ili9341_fill()` / `ili9341_bulk()` 两个像素块传输接口：`spi_buffer` 这个 2048 像素全局缓冲的契约，以及 `__USE_DISPLAY_DMA__` 打开后的 DMA 加速实现。
4. 说清 `RGB565(r,g,b)` 宏为什么不是「标准的 R5G6B5 位排布」——它和「SPI 8 位模式下 16 位写 DR 会先发低字节」这一硬件特性配套。
5. 理解两种点阵字体的编码格式：5x7 小字体（宽度编码藏在首字节的低 3 位）与 16x22 大数字字体，以及 `blit8BitWidthBitmap()` 如何把 1 位位图展开成 16 位颜色。

本讲只讲「怎么把像素和字符送到屏幕上」；「什么时候送、送什么」（轨迹绘制、脏矩形重绘）属于 u4-l2 和 u4-l4 的内容。

## 2. 前置知识

### 2.1 四线 SPI 液晶接口

ILI9341 是一块 320x240 的液晶控制芯片，NanoVNA 通过 SPI 总线与它通信。除了标准 SPI 的三根线（SCK 时钟、MOSI 主出从入、CS 片选），还多了一根 **DC（Data/Command）线**：

- DC 拉低时，接下来发送的字节被面板解释为「命令号」（如 0x2A = 设置列地址）；
- DC 拉高时，发送的字节被解释为「命令的参数/数据」（如列地址的 4 个坐标字节，或一连串像素颜色）。

这套协议称为「4 线 SPI」（CS + SCK + MOSI + DC）。面板内部有显存（GRAM），你只需告诉它「往哪个矩形区域写」，然后连续灌像素即可。

### 2.2 为什么没有帧缓冲

一块 320x240x16bit 的完整帧缓冲需要 320 × 240 × 2 = **150 KB** RAM，而 STM32F072 总共只有 16 KB SRAM（见 u1-l2 讲过的链接脚本）。所以 NanoVNA **不可能**像 PC 那样「先画到内存再整屏刷新」，只能：

- 用一个 2048 像素（4 KB）的小缓冲 `spi_buffer` 充当「搬运单元」；
- 每次把一小块区域（一个字符、一个菜单格子、一段轨迹）展开进 `spi_buffer`，再用一次块传输送到面板显存。

这是理解本讲所有接口设计的总前提：**一切绘制都被拆成小块的块传输**。

### 2.3 RGB565 像素格式

16 位色常用 RGB565 编码：红 5 位、绿 6 位、蓝 5 位。但本讲会看到，NanoVNA 的 `RGB565()` 宏生成的位排布是「变形」的——原因在 4.2.3 节详解。你只需先记住：**固件里所有颜色都必须用 `RGB565(r,g,b)` 或 `RGBHEX(0xRRGGBB)` 宏生成**，不要手写 `0xF800` 这类「标准值」。

### 2.4 点阵字体

点阵字体就是一张「每行用位表示亮灭」的位图。例如 5x7 字体中，字母 A 占 5 列 7 行，每行 1 个字节，最高位对应最左列像素。字体数据以 `const` 数组形式存在 Flash 里（不占宝贵的 RAM）。

### 2.5 与前面讲义的衔接

- u1-l3 中我们见过 `main()` 初始化链里的 `ili9341_init()`——本讲解释它内部做了什么。
- u2-l5 中我们说过绘制流水线是 `plot_into_index` → `draw_all`，最终所有像素都经由本讲的 `ili9341_bulk`/`ili9341_fill` 落到屏幕。
- u5-l1 会讲 shell 命令表——本讲的综合实践要往表里加一条命令，正好提前热身。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [ili9341.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c) | LCD 驱动主体（约 730 行） | SPI 初始化、命令表、fill/bulk、DMA、字体渲染 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | 公共头文件 | `__USE_DISPLAY_DMA__`、`RGB565`、`SPI_BUFFER_SIZE`、驱动接口声明 |
| [Font5x7.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Font5x7.c) | 5x7 点阵小字体 | 127 个字符的位图与宽度编码 |
| [numfont20x22.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/numfont20x22.c) | 16x22 大数字字体（文件名里的 20 是历史遗留） | 频率读数用的大数字位图 |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 命令表与实践挂接点 | `commands[]` 表、`cmd_capture` 复用 `spi_buffer` |

## 4. 核心概念与源码讲解

### 4.1 模块一：ili9341_init 初始化——寄存器级 SPI、命令协议与初始化序列表

#### 4.1.1 概念说明

`ili9341_init()` 要把面板从上电状态带到「可写入像素」状态，分三件事：

1. **初始化 SPI1 外设**——注意不是用 ChibiOS 的 SPI 驱动，而是直接操作寄存器；
2. **硬件复位**——拉低 RESET 引脚 10ms 再释放；
3. **下发一串初始化命令**——电源、伽马、像素格式、扫描方向等，面板厂商手册规定的序列。

为什么绕开 ChibiOS 的 `SPIDriver`？因为这个驱动需要三种 ChibiOS 抽象不直接支持的能力：8 位帧与 16 位帧混用（命令是 8 位、像素按 16 位写）、自定义的双向 DMA、以及极低的开销。源码作者选择了直接寄存器编程，这是嵌入式驱动里常见的取舍。

#### 4.1.2 核心流程

```text
ili9341_init()
 ├─ spi_init()                 # 直接配置 SPI1 寄存器
 │   ├─ rccEnableSPI1          # 开 SPI1 时钟
 │   ├─ CR1: 主模式 + 软件从机管理
 │   ├─ CR2: 8 位帧 + FRXTH
 │   ├─ (DMA 使能时) 分配 TX/RX DMA 流，挂到 &SPI1->DR
 │   └─ CR1 |= SPE             # 最后才使能外设
 ├─ DC_DATA; RESET_ASSERT;     # 复位脉冲
 │   sleep 10ms; RESET_NEGATE
 └─ 遍历 ili9341_init_seq[]    # 每条命令间 sleep 5ms
      └─ send_command(cmd, len, data)
```

初始化序列表是「长度前缀」格式，和 u2-l2 讲过的 `si5351_configs[]` 是同一手法：

```text
[命令号][参数长度 n][参数 × n] [命令号][参数长度 n] ... [0 哨兵]
```

遍历代码 `p += 2 + p[1]` 即「跳过 命令号 + 长度字节 + n 个参数」，遇到 0 结束。

#### 4.1.3 源码精读

先看引脚宏与硬件复位。[ili9341.c:L138-L143](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L138-L143) 用宏封装了 4 个 GPIO 操作：复位脚 PA15、片选脚 PB6、命令/数据脚 PB7。`palClearPad`/`palSetPad` 是 ChibiOS PAL（GPIO）驱动的接口。特别注意 `DC_CMD` 是拉**低**、`DC_DATA` 是拉**高**——DC 低电平表示命令。

SPI 外设的寄存器级配置在 [ili9341.c:L225-L250](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L225-L250)：`SPI_CR1_MSTR` 设主模式；`SSM|SSI` 用软件管理片选，把 NSS 引脚省出来做普通 IO；`SPI_CR2_8BIT`（即 [ili9341.c:L156](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L156) 定义的 `0x0700`，对应 CR2 的 DS[3:0]=0111，8 位数据帧）加上 `FRXTH` 让 RXNE 标志每 8 位就置位；最后才置 `SPE` 使能。若定义了 `__USE_DISPLAY_DMA__`，还会在此分配两条 DMA 流并把外设地址固定到 `&SPI1->DR`。

**命令/数据双相位协议**的核心是 [ili9341.c:L253-L270](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L253-L270) 的 `send_command()`：

- `CS_LOW` 后先 `DC_CMD`，写入命令号；
- 关键的一步是 `while (SPI_IS_BUSY);`——**必须等命令字节完全移出**，才能把 DC 切到 `DC_DATA`。否则 DC 在命令字节还在移位寄存器里时就变了，面板会把命令号当成数据；
- 之后逐字节写入参数，每个字节前等 TXE（发送 FIFO 有空位）；
- 末尾的 `//CS_HIGH;` 被注释掉了：**CS 在整个驱动里长期保持低电平**，省去每次传输的 GPIO 翻转开销。只有读显存的路径（`ili9341_read_memory` 末尾）会拉高 CS 结束读会话。

初始化序列表在 [ili9341.c:L272-L334](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L272-L334)，以 `0` 哨兵结尾（[ili9341.c:L333](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L333)）。几个值得认识的条目：

- [ili9341.c:L302](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L302)：`MEMORY_ACCESS_CONTROL` 写入 `DISPLAY_ROTATION_0`，即横屏（landscape）。旋转常数的位含义在 [ili9341.c:L121-L133](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L121-L133)：MY/MX/MV 控制行进方向与行列交换，**BGR 位控制面板按 BGR 还是 RGB 顺序解释像素**——这个位和 4.2 节的 `RGB565` 宏是一对搭档；
- [ili9341.c:L304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L304)：`PIXEL_FORMAT_SET, 1, 0x55`——两个 0x5 分别把接口与显存都设成 16 位色；
- 表里被注释掉的伽马校正、亮度条目说明作者调试过多个版本，保留注释作参考。

主入口 [ili9341.c:L336-L349](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L336-L349) 把上述流程串起来：`spi_init()` → DC 拉高 → 复位脉冲 10ms → 逐条下发命令、每条间隔 5ms（给面板内部电源/振荡稳定的时间）。

另外提醒：`ili9341_test()` 被整体包在 `#if 0` 里（[ili9341.c:L683-L733](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L683-L733)），是死代码，但作为「如何调用 fill/line/drawfont 画测试图」的参考示例很好读。

#### 4.1.4 代码实践

**实践目标**：验证你真正读懂了初始化序列表的「长度前缀」格式与遍历逻辑。

**操作步骤**（PC 端，无需硬件）：

1. 通读 [ili9341.c:L272-L334](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L272-L334) 的序列表，手工模拟 `ili9341_init()` 中 `p += 2 + p[1]` 的遍历；
2. 写一个 20 行的 Python 脚本，把表中**未被注释**的命令逐条解析并打印为 `(命令名, 参数字节数, 参数列表)`。命令名可用 `ILI9341_xxx` 宏名，你需要把用到的宏定义抄进脚本；
3. 统计：一共有多少条命令？其中参数长度为 0 的有几条？

**需要观察的现象**：脚本输出的条目数应与源码表里从 `ILI9341_SOFTWARE_RESET` 到 `ILI9341_DISPLAY_ON` 的实际条目一致；`SLEEP_OUT` 与 `DISPLAY_ON` 是最后两条且都无参数。

**预期结果**：解析到最后一个条目 `ILI9341_DISPLAY_ON, 0`，随后遇到哨兵 0 停止。若你的循环越界，说明忘了哨兵或长度计算错位。

（可选真机实践，待本地验证：把 [ili9341.c:L302](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L302) 的 `DISPLAY_ROTATION_0` 临时换成 `DISPLAY_ROTATION_90` 重新编译烧录，开机画面应整体旋转 90°，字符方向也会变——直观感受 MADCTL 的作用。）

#### 4.1.5 小练习与答案

**练习 1**：`send_command()` 里 `while (SPI_IS_BUSY);` 删掉会怎样？

**答案**：命令字节可能还停留在移位寄存器中没有发出，DC 线就已被切换为数据相位，面板会把命令号本身解释为第一个数据字节，导致后续参数整体错位一位，屏幕行为不可预期。（`SPI_IS_BUSY` 即 SR 寄存器的 BSY 位，见 [ili9341.c:L173](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L173)。）

**练习 2**：为什么 CS 可以长期保持低电平，而读显存后必须拉高？

**答案**：写路径只有「命令→数据」一个方向，面板状态机在 MEMORY_WRITE 之后会持续吞像素，保持 CS 低不影响后续命令的相位（每次都有 DC 重新声明）。而读显存（MEMORY_READ）的长度由 CS 拉高终止，不拉高面板会认为读会话仍在继续；拉高也让主机与面板的读时序重新对齐，见 [ili9341.c:L418](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L418) 与 [ili9341.c:L503](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L503)。

**练习 3**：`spi_init()` 里为什么把 `SPI_CR1_SPE` 放在所有 CR1/CR2 配置之后？

**答案**：SPE 是外设使能位。STM32 的外设寄存器大多要求在禁止状态下配置，先配好模式再使能，避免使能期间半配置的状态产生错误波形。

### 4.2 模块二：ili9341_fill / ili9341_bulk——像素块传输、spi_buffer 契约与 DMA 加速

#### 4.2.1 概念说明

整块驱动只有两个「写屏幕」原语，其余所有绘制函数都建立在它们之上：

- `ili9341_fill(x, y, w, h, color)`：把一个 `w×h` 矩形区域全部刷成同一颜色；
- `ili9341_bulk(x, y, w, h)`：把 `spi_buffer` 里的前 `w×h` 个 16 位像素搬到该矩形区域。

两者的协议前半段完全一样，就是面板手册里经典的「设定窗口→连续写」三步：

1. `COLUMN_ADDRESS_SET`（0x2A）带 4 个参数：x 起始/结束（大端 16 位）；
2. `PAGE_ADDRESS_SET`（0x2B）带 4 个参数：y 起始/结束；
3. `MEMORY_WRITE`（0x2C）之后连续写入像素，面板自动在窗口内换行。

`spi_buffer` 是 [ili9341.c:L24](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L24) 定义的全局数组，大小由 [nanovna.h:L308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L308) 的 `SPI_BUFFER_SIZE = 2048` 决定（4096 字节）。它是「驱动与上层绘图代码之间的唯一画布」：**调用 `ili9341_bulk` 之前，调用方必须先把像素填进 `spi_buffer`**，这是隐式契约，驱动不做任何检查。

#### 4.2.2 核心流程

```text
ili9341_fill(x,y,w,h,color)          ili9341_bulk(x,y,w,h)
 ├─ 2A/2B 设窗口（__REV16 打包）      ├─ 2A/2B 设窗口
 ├─ 0xC  进入写显存                   ├─ 0x2C 进入写显存
 └─ DMA：源地址=&color（不递增）      └─ DMA：源地址=spi_buffer（MINC 递增）
     同一半字重复 w*h 次                  连续读 w*h 个半字
```

DMA 版的整屏填充还会触发一个细节：320×240 = 76800 个传输，超过 DMA 传输计数器 16 位上限 65535，所以 `dmaStreamFlush()` 要分两段（65535 + 11265）完成。

#### 4.2.3 源码精读

**窗口地址的字节序魔法**。DMA 分支的 `ili9341_fill` 在 [ili9341.c:L426-L437](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L426-L437)：

```c
uint32_t xx = __REV16(x | ((x + w - 1) << 16));
send_command(ILI9341_COLUMN_ADDRESS_SET, 4, (uint8_t *)&xx);
```

面板要求参数顺序是 `x_hi, x_lo, x1_hi, x1_lo`（每对 16 位大端）。代码先把两个 16 位值拼进一个 32 位数的低/高半字，再用 `__REV16()`（Cortex 内建指令，反转**每个半字内部**的字节序）调整。在内存小端环境下，`&xx` 指向的 4 个字节按地址递增恰好就是面板要的大端顺序。一行位运算替代了 4 次移位拼装，这是嵌入式代码里很值得学的紧凑写法（被注释掉的 [ili9341.c:L354-L355](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L354-L355) 是它的直觉版本）。

**DMA 发送**。[ili9341.c:L458-L471](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L458-L471) 的 `ili9341_bulk` 把源地址设为 `spi_buffer`，模式里加了 `STM32_DMA_CR_MINC`（内存地址自增），外设/内存宽度都是 HWORD（16 位），然后交给 [ili9341.c:L212-L222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L212-L222) 的 `dmaStreamFlush()`：按 65535 上限分段、启动流、忙等完成。而 `ili9341_fill` 的 DMA 源地址是 `&color` 且**不加 MINC**——同一个地址反复读 76800 次，等效于把一个半字复制 w×h 遍。

**为什么整个文件有两套 fill/bulk/read_memory？** 因为 `__USE_DISPLAY_DMA__` 宏（[nanovna.h:L23](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L23)）把文件切成两半：`#ifndef` 分支（[ili9341.c:L351-L419](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L351-L419)）是 CPU 逐半字轮询发送的朴素版本，`#else` 分支（[ili9341.c:L420-L518](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L420-L518)）是 DMA 版本。注意 nanovna.h 中该宏是无条件定义的，所以本仓库实际编译的总是 DMA 版；朴素版本保留下来便于理解协议（也便于在没有 DMA 的芯片上移植）。DMA 版省下的是 CPU 等待 TXE 的空转——CPU 在 DMA 搬运期间可以继续准备下一块数据。

**RGB565 宏的字节序之谜**。[nanovna.h:L304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L304)：

```c
#define RGB565(r,g,b)  ( (((g)&0x1c)<<11) | (((b)&0xf8)<<5) | ((r)&0xf8) | (((g)&0xe0)>>5) )
```

它生成的 16 位值按位拆开是（注释 `gggBBBbb RRRrrGGG` 描述的就是它）：绿色被**拆成两半**放在值的最高 3 位和最低 3 位，蓝在 bit12..8，红在 bit7..3。代入 (255,255,255) 验证：0xE000|0x1F00|0xF8|0x07 = 0xFFFF，白色正确。代入纯红 (255,0,0)：得到 **0x00F8** 而不是教科书 RGB565 的 0xF800。

为什么这样设计？两处源码合起来给出答案：

1. [ili9341.c:L148-L157](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L148-L157) 的注释说明：SPI 配置为 **8 位帧模式**时，对 DR 做 16 位写会**先发低字节、再发高字节**。DMA HWORD 模式写 DR 同理，线路上第一个字节是 16 位值的**低**字节；
2. 于是 `RGB565` 把「应该在第一个线上字节出现的 RRRRRGGG」放进低字节，配合面板 MADCTL 里的 BGR 位（[ili9341.c:L129-L133](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L129-L133)），最终显示的颜色与宏参数一致。

实践推论：如果你绕过宏手写 `0xF800`（教科书红色），线路首字节会落进绿/蓝字段，屏幕上显示的将是偏青色而不是红色（此推断欢迎真机验证）。同一原理的逆变换出现在 `capture` 命令里——回读显存后固件要把面板字节序转回 RGB565 存放（见 [ili9341.c:L514](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L514) 的 `RGB565(r,g,b)` 调用）。

**spi_buffer 的复用**。这个 4KB 缓冲是全项目最大的一块连续 RAM 之一，所以被多处「借」用：驱动本身用它当像素画布；`cmd_capture` 用它当读显存的接收缓冲（[main.c:L727-L739](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L727-L739)）；时域变换也曾复用它。`ili9341_read_memory` 的 DMA 版（[ili9341.c:L475-L517](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L475-L517)）注释警告「缓冲必须 ≥ 3×len+1 字节」——面板回读的是每像素 3 字节（18 位色），`capture` 一次读 2 行 640 像素：640×3+1 = 1921 ≤ 2048，刚好塞下，所以它每次只读两行，循环 120 次铺满 240 行。

**顺带一提**：连画直线 `ili9341_line()`（[ili9341.c:L634-L681](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L634-L681)）也是用一连串小 `ili9341_fill` 矩形拼出来的，不做逐像素写——再次印证「一切皆块传输」的设计哲学。

#### 4.2.4 代码实践

**实践目标**：在 PC 上验证 `RGB565` 宏与 `__REV16` 窗口打包的位运算，把两个「魔法」变成可计算的白盒。

**操作步骤**：

1. 把宏原样翻译成 Python：

```python
# 示例代码：RGB565 与 __REV16 的 host 端复现
def rgb565(r, g, b):
    return (((g & 0x1c) << 11) | ((b & 0xf8) << 5) | (r & 0xf8) | ((g & 0xe0) >> 5))

def rev16(v):           # 反转 16 位内的字节序
    return ((v & 0xff) << 8) | (v >> 8)

def cas_bytes(x, x1):   # COLUMN_ADDRESS_SET 的 4 个线上字节
    packed = rev16(x) | (rev16(x1) << 16)   # 等价于 __REV16(x | x1<<16)
    return packed.to_bytes(4, "little")     # send_command 按地址递增取字节
```

2. 打印 `rgb565(255,255,255)`、`rgb565(255,0,0)`、`rgb565(0,255,0)`、`rgb565(0,0,255)` 的十六进制；
3. 打印 `cas_bytes(300, 319)`，与协议要求的 `[0x01, 0x2C, 0x01, 0x3F]`（即 300、319 的大端表示）对比；
4. 再打印 `rgb565(0,0,255)` 与教科书蓝 `0x001F` 的差异，说明字节序「错位」发生在哪一位。

**需要观察的现象**：白 = 0xFFFF；「红」= 0x00F8；窗口字节序列完全匹配大端协议。

**预期结果**：`cas_bytes(300,319)` 输出 `01 2c 01 3f`；四个纯色的值没有一个是标准 R5G6B5 排布，但都在宏的位域定义内自洽。若第 3 步不匹配，检查你是否漏了 `<<16` 前后各自的半字内反转。

#### 4.2.5 小练习与答案

**练习 1**：`ili9341_bulk(x, y, w, h)` 没有颜色参数，颜色从哪来？

**答案**：从全局 `spi_buffer` 的前 `w×h` 个 `uint16_t` 元素来。调用方（如 `blit8BitWidthBitmap`、plot.c 的轨迹绘制）必须先填满这块缓冲再调用，见 [ili9341.c:L458-L471](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L458-L471)。

**练习 2**：一次 `ili9341_bulk(0, 0, 320, 8)` 需要多少像素？一次能传的最大高度（全宽 320）是多少？

**答案**：320×8 = 2560 像素——**超过**了 `SPI_BUFFER_SIZE`(2048)，这是一次非法调用。全宽 320 时最大高度为 2048/320 = 6.4，即最多 6 行；驱动不检查这个约束，超限会读到 `spi_buffer` 之外的内存，产生花屏甚至更糟的行为。

**练习 3**：`dmaStreamFlush` 为什么要按 65535 分段？

**答案**：DMA 通道的传输计数寄存器 NDTR 是 16 位的，单次传输最多 65535 个数据单元。整屏填充 320×240 = 76800 超限，故 [ili9341.c:L212-L222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L212-L222) 用循环把长传输拆成 65535 + 余数两段。

### 4.3 模块三：drawstring / drawfont——两种点阵字体的编码与 blit 渲染

#### 4.3.1 概念说明

固件里有两种字体，服务于完全不同的场景：

| | Font5x7（小字体） | numfont16x22（大数字字体） |
|---|---|---|
| 用途 | 菜单文字、标注、顶部频率栏（plot.c 的 `draw_frequencies`） | 触屏数字键盘的按键字符与数值输入回显（ui.c） |
| 字符集 | 127 个字符（ASCII 范围） | 10 个数字 + 少量符号 |
| 单字符尺寸 | 每行 1 字节 × 7 行 = 7 字节 | 每行 1 个 `uint16_t` × 22 行 = 44 字节 |
| 宽度 | 1~8 像素，**每个字符可变宽** | 固定 16 像素 |
| 访问宏 | `FONT_GET_DATA/WIDTH/HEIGHT` | `NUM_FONT_GET_DATA/WIDTH/HEIGHT` |

小字体最巧的设计是**可变宽度编码**：5x7 字体每个字符 7 字节，其中第一个字节的**低 3 位不存像素，而存宽度补码**。这样「i」这样的窄字符能省下像素间距，让 `ili9341_drawstring` 输出的文字更紧凑，且不需要额外的宽度表。

#### 4.3.2 核心流程

```text
ili9341_drawstring("ABC", x, y)
 对每个字符 ch：
   ├─ FONT_GET_DATA(ch)   → &x5x7_bits[ch*7]
   ├─ FONT_GET_WIDTH(ch)  → 8 - (首字节 & 7)
   └─ blit8BitWidthBitmap(x, y, w, 7, data)
        ├─ 逐行取 1 字节，逐位判断 MSB
        ├─ 亮位→foreground_color，暗位→background_color
        ├─ 展开后的 16 位像素写入 spi_buffer
        └─ ili9341_bulk(x, y, w, 7)   ← 最终落到 4.2 的块传输
```

大字体路径相同，只是位图元素换成 `uint16_t`（16 列），走 `blit16BitWidthBitmap`。注意字体渲染**自带背景色**：字符的暗像素也写屏，因此文字会覆盖它所在的矩形区域，不存在透明叠加。

#### 4.3.3 源码精读

**5x7 字体的访问宏与宽度编码**在 [Font5x7.c:L12-L24](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Font5x7.c#L12-L24)：

```c
#define FONT_GET_DATA(ch)   (&x5x7_bits[ch*7])
#define FONT_GET_WIDTH(ch)  (8-x5x7_bits[ch*7]&7)
```

`CHAR5x7_WIDTH_5px = 0x03`，于是宽度 = 8 − 3 = 5。能这样「藏在首字节」是因为字模像素按 MSB 左对齐，首行低 3 位本来就用不到。字模数组 [Font5x7.c:L27](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Font5x7.c#L27) 是 `x5x7_bits[127*7]`，每字符固定 7 字节、以字符码线性索引（`'A'` 在偏移 65×7 处）。文件里每个字符上方都有 ASCII art 注释（如 [Font5x7.c:L49-L66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Font5x7.c#L49-L66)），人读源码就能直接「看见」字形。

**大数字字体**在 [numfont20x22.c:L25](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/numfont20x22.c#L25)：数组名 `numfont16x22[]`、文件名却是 `numfont20x22.c`——16 才是真实宽度（见 [nanovna.h:L177-L180](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L177-L180) 的 `NUM_FONT_GET_WIDTH 16` / `NUM_FONT_GET_HEIGHT 22`），命名出入提醒我们**以宏为准**。每个字符 22 个 `uint16_t`，`ch*22` 索引；文件开头 0 号字模的位图（[numfont20x22.c:L27-L48](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/numfont20x22.c#L27-L48)）一眼就能认出是数字 "0" 的轮廓。

**位图展开**由 [ili9341.c:L542-L554](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L542-L554) 的 `blit8BitWidthBitmap` 完成：外层循环每行取 1 字节，内层从 MSB 到 LSB 扫描每一位，按位选前景/背景色写入 `spi_buffer`，最后调用 `ili9341_bulk` 送出。16 位宽版本 [ili9341.c:L556-L568](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L556-L568) 结构完全相同，只是判 `0x8000`。这两个函数把「1 位字模」放大成「16 位彩色像素」，是典型的软件 blit（位块传送）。

**对外接口**集中在 [ili9341.c:L570-L620](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L570-L620)：

- `ili9341_drawchar`（L570-L573）：单字符，宽度由宏计算；
- `ili9341_drawstring`（L575-L584）：逐字符绘制并 `x += w` 前进，**没有字间距字幅**，紧凑排布；
- `ili9341_drawstringV`（L586-L591）：竖排文字的小把戏——临时把面板旋到 `DISPLAY_ROTATION_270` 画完再转回来，复用旋转寄存器而不用写竖排光栅逻辑（u2-l5 讲过的 `STOP_PROFILE` 宏就靠它把耗时数据竖着打在屏幕边上）；
- `ili9341_drawchar_size` / `ili9341_drawstring_size`（L593-L620）：整数倍放大，每个字模像素重复 `size×size` 次（ui.c:384 以 size=4 放大显示 info 页文字）；
- `ili9341_drawfont`（L610-L614）：大数字字体单字符，ui.c 的数字键盘按键与输入回显都用它。

顺带纠正一个容易想当然的点：屏幕**顶部**的 START/STOP 频率文字用的是 5x7 小字体的 `ili9341_drawstring`（[plot.c:L1650-L1651](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1650-L1651)），16x22 大字体只在触屏数字键盘场景出现（[ui.c:L1272](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1272)、[ui.c:L1317-L1319](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1317-L1319)）。

颜色由 [ili9341.c:L525-L533](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L525-L533) 的 `ili9341_set_foreground/background` 全局变量控制，默认调色板见 [nanovna.h:L310-L322](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L310-L322)（网格灰、四条轨迹各一色、电量绿/红等）。

#### 4.3.4 代码实践

**实践目标**：用 Python 当「字体查看器」，亲手解码一个 5x7 字模，验证你对编码格式的理解。

**操作步骤**：

1. 从 [Font5x7.c:L49-L66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Font5x7.c#L49-L66) 抄出字符 0x01 的 7 个字节数据；
2. 运行下面的解码脚本：

```python
# 示例代码：5x7 字模解码器（数据抄自 Font5x7.c 的 0x01 字符）
data = [0b00000000 | 0x03,   # 首行低 3 位 = 宽度补码 0x03
        0b00100000, 0b01110000, 0b11111000,
        0b01110000, 0b00100000, 0b00000000]
w = 8 - (data[0] & 7)        # FONT_GET_WIDTH：8 - (首字节&7) = 5
for row in data:             # MSB 是最左列像素
    print(''.join('*' if (row << i) & 0x80 else '.' for i in range(w)))
```

3. 把输出与源码注释里的 ASCII art 逐行比对；
4. （可选进阶）写一个通用函数 `show(ch)`，从仓库的 Font5x7.c 文本里正则抽取任意字符的 7 字节，打印 `'A'`、`'Z'`、`'0'` 的字模。

**需要观察的现象**：脚本先打印 `w = 5`，然后输出一个 5 列 × 7 行、向左对齐的实心三角形（第 3 行整行点亮）。

**预期结果**：输出与 [Font5x7.c:L49-L66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Font5x7.c#L49-L66) 注释中的图形逐行一致。若图形左右颠倒，说明你把 LSB 当成了最左列——正确方向是 **MSB 在最左**。

（可选真机实践，待本地验证：烧录固件后进菜单让屏幕显示频率大数字，对照 `numfont16x22[]` 中 22 行位图感受 16 像素宽字模的锯齿轮廓。）

#### 4.3.5 小练习与答案

**练习 1**：`ili9341_drawstring` 画完一个字符后光标前进多少像素？

**答案**：前进 `FONT_GET_WIDTH(ch)` 即该字符自身的字模宽度（1~8 像素），见 [ili9341.c:L575-L584](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L575-L584) 的 `x += w;`。由于字模本身最右列通常是空列，视觉上自带 1~2 像素间隔，无需额外字距。

**练习 2**：为什么 5x7 字体每字符 7 字节而不是 5×7=35 位≈5 字节？

**答案**：为了随机访问与编码简单：每行固定 1 字节（列数 ≤8），7 行 7 字节，`ch*7` 即得首地址，无需位级打包偏移计算；宽度信息再借用首字节低 3 位免费携带。在 16KB RAM 的约束下，用一点点 Flash 换取解码的简单与速度，是合理的取舍。

**练习 3**：想在屏幕上叠加「透明背景」的文字（只画亮点、不覆盖背景），现有接口能做到吗？

**答案**：不能。`blit8BitWidthBitmap` 对暗位也写入 `background_color`，文字矩形必然覆盖原有内容。要透明叠加需要先 `ili9341_read_memory` 读回区域、在 `spi_buffer` 里合成、再 `ili9341_bulk` 写回——驱动已具备全部积木，但上层没有这么用（读显存比写慢得多，不值得）。

## 5. 综合实践

**任务：给固件添加 `grid_test` 命令，一次调用检验本讲全部三个模块。**

这个命令要在屏幕四角画 4 个不同颜色的 20x20 方块（检验 4.2 的 `fill` + `RGB565`），并在屏幕中央显示一个标识字符串（检验 4.3 的 `drawstring`）；添加命令本身则复习 u1-l3/u2-l5 的线程模型与命令表（4.1 的 `send_command` 在底层被自动用到）。

**操作步骤**：

1. 在 [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) 中 `cmd_version`（[main.c:L2024-L2029](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2024-L2029)）附近添加（示例代码）：

```c
// 示例代码：练习用 shell 命令，画四角色块 + 中央字符串
VNA_SHELL_FUNCTION(cmd_grid_test)
{
  (void)argc;
  (void)argv;
  // 四角 20x20 方块：左上红、右上绿、左下蓝、右下黄
  ili9341_fill(  0,   0, 20, 20, RGB565(255,   0,   0));
  ili9341_fill(300,   0, 20, 20, RGB565(  0, 255,   0));
  ili9341_fill(  0, 220, 20, 20, RGB565(  0,   0, 255));
  ili9341_fill(300, 220, 20, 20, RGB565(255, 255,   0));
  // 中央标识：9 字符 × 5~6 像素 ≈ 50px 宽，7px 高
  ili9341_set_foreground(DEFAULT_FG_COLOR);
  ili9341_set_background(DEFAULT_BG_COLOR);
  ili9341_drawstring("GRID TEST", 132, 116);
}
```

2. 在 `commands[]` 表（[main.c:L2153-L2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208)）的 `{"version", ...}` 一行旁仿照添加：

```c
{"grid_test"   , cmd_grid_test   , 0},
```

3. 按 u1-l2 的流程编译（有真机则 `make flash` 烧录），USB 串口终端里先敲 `pause` 暂停扫描，再敲 `grid_test`。

**需要观察的现象**：屏幕四角出现红/绿/蓝/黄四个 20x20 实心方块，屏幕正中偏上出现白底黑字的 `GRID TEST` 字样；`help` 输出的命令列表里多了 `grid_test`。

**预期结果与检验点**：

- 四个方块颜色与 RGB 参数一致 → 说明 `RGB565` 用法正确；若红色方块显示成青色，说明你手写了 `0xF800` 之类的裸值（回到 4.2.3）；
- 字符清晰、位置在 (132,116) 附近 → 说明前景/背景色设置与 `drawstring` 的前进逻辑理解无误；
- 命令敲入立即生效、系统不死机 → 说明直接在 shell 线程画屏对这种一次性小区域绘制是安全的（更大面积或高频绘制才需要 `CMD_WAIT_MUTEX` 移交 sweep 线程，见 u2-l5）；
- 之后任何触发重绘的操作（如恢复扫描）会逐渐覆盖这些方块 → 印证 u2-l5 讲的「请求-响应」局部重绘模型：`grid_test` 没有走 `redraw_request` 体系，画的内容只是「直接写进了显存」。

（无硬件读者：本实践同样可以作为**代码阅读练习**完成——把上述代码在脑中/纸上走一遍，回答：`GRID TEST` 里的空格字符宽度是多少？（查 Font5x7.c 中 0x20 字模首字节低 3 位）；`ili9341_drawstring` 总共发起几次 `ili9341_bulk`？（每字符一次，共 9 次）。）

## 6. 本讲小结

- NanoVNA 没有帧缓冲（150KB vs 16KB RAM），一切绘制都拆成「设窗口（0x2A/0x2B）→ 连续写（0x2C）」的小块传输，`spi_buffer[2048]` 是驱动与上层绘图之间唯一的像素画布，`bulk` 调用前必须先填满它。
- `ili9341.c` 直接寄存器编程驱动 SPI1：`send_command()` 以 DC 线区分命令/数据相位，切相位前必须等 `SPI_IS_BUSY` 清零；CS 长期保持低电平，只有读显存后拉高。
- 初始化序列表采用「命令号+长度+参数」的长度前缀格式、0 哨兵结尾，横屏方向与 BGR 位由 MADCTL 一次设定。
- `RGB565(r,g,b)` 宏生成的是「字节序交换过」的 16 位颜色——因为 SPI 工作在 8 位帧模式、对 DR 的 16 位写会先发低字节；宏与 MADCTL 的 BGR 位配套，保证颜色正确。永远用宏，别手写标准 RGB565 值。
- `__USE_DISPLAY_DMA__`（本仓库恒开）把 fill/bulk 交给 DMA：fill 用「固定地址重复读」刷纯色，bulk 用 MINC 读 `spi_buffer`；长传输按 65535 分段。
- 两种字体都是 1 位位图：5x7 小字体每字符 7 字节、宽度藏在首字节低 3 位（`8-(b&7)`）；16x22 大数字字体每字符 22 个 `uint16_t`。`blit8/16BitWidthBitmap` 把它们按前景/背景色展开进 `spi_buffer` 再 `bulk` 送出。

## 7. 下一步学习建议

本讲解决了「像素如何上屏」，下一讲 **u4-l2 轨迹系统：12 种显示格式与坐标换算** 将回答「上屏的内容从哪来」：`plot.c` 的 `trace_into_index` 如何把 `measured[]` 复数按 LOGMAG/SMITH/SWR 等格式换算成屏幕坐标。之后 **u4-l4 markmap 脏矩形重绘** 会解释「为什么改一个像素不用重画全屏」，与本讲的块传输原语首尾衔接。若你对本讲的命令表实践意犹未尽，可提前跳读 **u5-l1 USB CDC Shell** 了解 `VNAShell_executeLine` 与 `CMD_WAIT_MUTEX` 的完整机制。
