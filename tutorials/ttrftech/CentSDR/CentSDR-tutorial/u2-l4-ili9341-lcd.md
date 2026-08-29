# 画面输出：ILI9341 LCD 驱动与字库

## 1. 本讲目标

学完本讲，你应该能够：

1. 说明 STM32F303 是如何通过 SPI 向 ILI9341 屏幕发送命令和像素数据的，以及 `send_command()` 的「命令/数据」引脚协议。
2. 解释 `ili9341_draw_bitmap()` 如何用「设窗口 + 一次 DMA」的方式批量传输一整个矩形区域的像素，以及 `spi_buffer` 这个 8KB 公共缓冲的容量约束。
3. 读懂 `Font5x7.c` 中 5×7 点阵字库的位编码方式，以及 `numfont*.c`、`icons.c` 按行取模的大字号字库结构（`font_t`）。
4. 掌握 `lcd` 命令实现 180 度旋转的寄存器级原理（MADCTL / 0x36 寄存器）。

本讲是单元二的最后一讲外设驱动。至此四条硬件链路（本振、编解码器、I2S 数据流、LCD）就齐全了，单元三将进入解调算法。

## 2. 前置知识

### 2.1 SPI 是什么

SPI（Serial Peripheral Interface，串行外设接口）是一种主从式同步串行总线，最少用 4 根线：

- **SCLK**：时钟线，由主机驱动，每一位数据伴随一个时钟沿；
- **MOSI**：主机输出、从机输入（数据线）；
- **MISO**：主机输入、从机输出（本讲只发不收，不用）；
- **CS**（片选）：拉低表示「现在跟这台从设备说话」。

CentSDR 中 SPI1 专门接 ILI9341 屏幕，只使用发送方向。回忆 [u1-l1](u1-l1-project-overview.md) 的总线分工：**I2C 管配置、I2S 管数据流、SPI 管屏幕**。

### 2.2 ILI9341 是什么

ILI9341 是一颗常见的 240×320 彩色 TFT 液晶控制器。它内部有一块显存（GRAM），主机通过写寄存器来控制它，最重要的几条命令：

- `0x2A`（CASET）：设置「列地址窗口」的起止；
- `0x2B`（PASET）：设置「行地址窗口」的起止；
- `0x2C`（RAMWR）：之后的字节全部当作像素颜色依次填入窗口；
- `0x36`（MADCTL）：控制扫描方向、行列交换、颜色顺序——180 度旋转就靠它。

「命令」和「数据」共用同一根 MOSI 线，靠一根额外的 **DC（Data/Command）** 引脚区分：DC 为低时总线上的字节是命令，为高时是数据。

### 2.3 RGB565 像素格式

每个像素 16 位：红 5 位、绿 6 位、蓝 5 位（绿色多 1 位是因为人眼对绿更敏感）。\( 2^5 \times 2^6 \times 2^5 = 32768 \) 色。屏幕 320×240 全屏约有 \( 320 \times 240 \times 2 = 153600 \) 字节，远超 STM32F303 的 RAM，所以**不能全屏帧缓冲，只能一小块一小块地搬**——这是理解本讲所有设计的钥匙。

### 2.4 与前面讲义的联系

- [u1-l3](u1-l3-main-init-flow.md) 中我们走过 `main()`：`ili9341_init()` 在 I2S 之后被调用（main.c:1020），随后 `disp_init()` 和 `ili9341_set_direction()`（main.c:1028-1029）。
- [u2-l2](u2-l2-audio-codec.md) 中 TLV320AIC3204 的初始化用了「(len, reg, data) 字节表 + 哨兵结尾 + 解释器循环」的编码方式——ILI9341 的初始化序列用的是**同一种模式**，学会一个就通了两个。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `ili9341.c` | ILI9341 驱动主体 | SPI 底层、初始化序列、批量绘制、字符/字库渲染、旋转 |
| `Font5x7.c` | 5×7 点阵 ASCII 字库（convbdf 生成） | 每个 字符 7 个 `uint16_t` 的位编码 |
| `numfont20x24.c` / `numfont32x24.c` | 大字号数字字库（`font_t` 格式） | 按行取模的 32 位字数据 |
| `icons.c` | 48×20 模式/AGC 图标库（`font_t` 格式） | `stride=2` 的宽字形编码 |
| `nanosdr.h` | 全局共享头文件 | `font_t` 结构、`RGB565` 宏、字库 extern 声明 |
| `main.c` | 命令注册与初始化调用点 | `cmd_lcd`、`ili9341_init()` 调用位置 |
| `display.c` | 驱动的「消费者」 | `spi_buffer` 如何被上层复用、图标如何被引用 |

## 4. 核心概念与源码讲解

### 4.1 模块一：SPI 底层传输与屏幕初始化

#### 4.1.1 概念说明

驱动屏幕的第一件事是把「SPI 能发字节」这个能力建立起来，然后把一串寄存器配置灌进 ILI9341。这个模块解决三个问题：

1. **谁提供时序**——SPI1 外设 + 发送 DMA 通道；
2. **命令怎么发**——CS/DC 两根 GPIO 控制线配合 8 位模式发命令字节；
3. **初始化配置怎么存**——编译期常量表，省 RAM、可读性好。

#### 4.1.2 核心流程

```
spi_init()
  ├─ rccEnableSPI1()            打开 SPI1 时钟
  ├─ 申请 TX DMA 流（内存→外设，半字宽度）
  ├─ SPI1->CR1 = MSTR|SSM|SSI    主机模式 + 软件从机管理
  ├─ SPI1->CR2 = 8bit | TXDMAEN  允许 DMA 搬运发送
  └─ SPI1->CR1 |= SPE            使能 SPI

ili9341_init()
  ├─ spi_init()
  ├─ 硬件复位：RESET 拉低 10ms 再放开
  ├─ 软件复位 (0x01)、关显示 (0x28)
  ├─ for (p = init_seq; *p; )     ← 解释器循环
  │    send_command(命令, 长度, 数据)
  │    p += 2 + 长度              ← 跳到下一条
  ├─ 等 100ms
  └─ 开显示 (0x29)
```

#### 4.1.3 源码精读

GPIO 控制线用宏直白地封装，注意 RESET 在 PA15、CS 在 PB6、DC 在 PB7：

[ili9341.c:24-29](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L24-L29)

```c
#define RESET_ASSERT	palClearPad(GPIOA, 15)
#define RESET_NEGATE	palSetPad(GPIOA, 15)
#define CS_LOW			palClearPad(GPIOB, 6)
#define CS_HIGH			palSetPad(GPIOB, 6)
#define DC_CMD			palClearPad(GPIOB, 7)
#define DC_DATA			palSetPad(GPIOB, 7)
```

公共像素缓冲 `spi_buffer`，4096 个半字 = 8KB，是驱动与显示模块共享的「搬运工作台」：

[ili9341.c:31](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L31)

```c
uint16_t spi_buffer[4096];
```

三个底层收发函数。`ssp_wait_slot()` 检查的是状态寄存器中发送 FIFO 水位域（掩码 `0x1800`），FIFO 将满时自旋等待，直到腾出空位再写入——这就是「等槽位」的含义；`ssp_senddata16()` 直接把 16 位像素写进 DR，是后续填充循环的主力：

[ili9341.c:40-62](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L40-L62)

```c
void
ssp_wait_slot(void)
{
  while ((SPI1->SR & 0x1800) == 0x1800)
    ;
}

void
ssp_senddata(uint8_t x)
{
  *(uint8_t*)(&SPI1->DR) = x;
  while (SPI1->SR & SPI_SR_BSY)
    ;
}

void
ssp_senddata16(uint16_t x)
{
  ssp_wait_slot();
  SPI1->DR = x;
}
```

`spi_init()` 里的 SPI 配置：软件从机管理（SSM|SSI）意味着**不用硬件 NSS 线**，片选完全由上面的 CS 宏手动控制；CR2 里 `0x0700` 把数据长度设为 8 位，同时打开 TXDMAEN 为后续 DMA 批量传输铺路：

[ili9341.c:87-110](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L87-L110)

```c
void
spi_init(void)
{
  rccEnableSPI1(FALSE);

  dmatx     = STM32_DMA_STREAM(STM32_SPI_SPI1_TX_DMA_STREAM);
  txdmamode = STM32_DMA_CR_CHSEL(SPI1_TX_DMA_CHANNEL) |
    ... /* M2P、半字宽度等 */
  dmaStreamAllocate(dmatx, ...);
  dmaStreamSetPeripheral(dmatx, &SPI1->DR);

  SPI1->CR1 = 0;
  SPI1->CR1 = SPI_CR1_MSTR | SPI_CR1_SSM | SPI_CR1_SSI;
  SPI1->CR2 = 0x0700 | SPI_CR2_TXDMAEN;
  SPI1->CR1 |= SPI_CR1_SPE;
}
```

`send_command()` 是所有寄存器写入的唯一通道，四步舞：拉低 CS → DC 拉低声明「这是命令」→ 切 8 位模式发命令字节 → DC 拉高后连发数据字节。注意末尾 `CS_HIGH` 被注释掉了——SPI1 总线上只有屏幕一个设备，片选常低省一次 GPIO 翻转：

[ili9341.c:112-124](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L112-L124)

```c
void
send_command(uint8_t cmd, int len, const uint8_t *data)
{
	CS_LOW;
	DC_CMD;
    ssp_databit8();
	ssp_senddata(cmd);
	DC_DATA;
	while (len-- > 0) {
		ssp_senddata(*data++);
	}
	//CS_HIGH;
}
```

初始化序列是一张 `(命令, 长度, 数据..., 命令, 长度, 数据..., 0)` 的扁平字节表，以 0 作哨兵结尾。摘录几条关键的：`0x36` 写 0x28 选横屏、`0x3A` 写 0x55 选 16 位像素、`0x2A/0x2B` 预设 320×240 地址窗口、最后 `0x11` 退出睡眠：

[ili9341.c:126-184](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L126-L184)

```c
const uint8_t ili9341_init_seq[] = {
		// cmd, len, data...,
		// Power control B
		0xCF, 3, 0x00, 0x83, 0x30,
		...
		// MEMORY_ACCESS_CONTROL
		//0x36, 1, 0x48, // portlait
		0x36, 1, 0x28, // landscape
		// COLMOD_PIXEL_FORMAT_SET : 16 bit pixel
		0x3A, 1, 0x55,
		...
		// Column Address Set
	    0x2A, 4, 0x00, 0x00, 0x01, 0x3f, // width 320
	    // Page Address Set
	    0x2B, 4, 0x00, 0x00, 0x00, 0xef, // height 240
		...
		// sleep out
		0x11, 0,
		0 // sentinel
};
```

`ili9341_init()` 先硬件复位（拉低 10ms），再软件复位、关显示，然后跑解释器循环灌入上表——`p += 2 + p[1]` 正是「跳过命令字节+长度字节+数据区」的前进逻辑，与 TLV320AIC3204 驱动的表解释器如出一辙：

[ili9341.c:186-210](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L186-L210)

```c
void
ili9341_init(void)
{
  spi_init();
  DC_DATA;
  RESET_ASSERT;
  chThdSleepMilliseconds(10);
  RESET_NEGATE;

  send_command(0x01, 0, NULL); // SW reset
  chThdSleepMilliseconds(5);
  send_command(0x28, 0, NULL); // display off

  const uint8_t *p;
  for (p = ili9341_init_seq; *p; ) {
    send_command(p[0], p[1], &p[2]);
    p += 2 + p[1];
    chThdSleepMilliseconds(5);
  }

  chThdSleepMilliseconds(100);
  send_command(0x29, 0, NULL); // display on
}
```

#### 4.1.4 代码实践：PC 上解释初始化序列

1. **实践目标**：不靠硬件，验证你真正理解了「(cmd, len, data...) + 哨兵」表格式。
2. **操作步骤**：把 `ili9341_init_seq` 数组抄进一个 Python 列表（或 C 数组），写 10 行解释器：循环读取 cmd、len，打印 `cmd=0x%02X len=%d data=[...]`，然后 `p += 2 + len`，直到读到 0。
3. **需要观察的现象**：打印出的每条命令与源码注释一一对应；循环在 `0x11`（sleep out）之后正常终止而不是越界。
4. **预期结果**：共解释出 **22 条命令**（含 sleep out），整张表含哨兵共 **117 字节**。若你的数字不同，回来逐条核对。
5. 已在源码层面人工核对过命令条数；解释器运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `send_command()` 里发命令前要 `ssp_databit8()`，而后续模块发像素时要用 `ssp_databit16()`？

**答案**：命令和寄存器参数以字节为单位，8 位模式一次写一个字节；像素是 RGB565 的 16 位值，16 位模式一次写 DR 就是一个完整像素，吞吐翻倍。

**练习 2**：`ili9341_init()` 里硬件复位（RESET 管脚）和软件复位（`0x01` 命令）都做了，是否冗余？

**答案**：不冗余。硬件复位保证芯片从确定的上电状态开始（即使它之前被异常配置过），软件复位是 ILI9341 数据手册建议的流程补充；两者作用域不同，且成本只是几毫秒延时。

**练习 3**：`p += 2 + p[1]` 中如果某条命令的 len 字段被误写成 0xFF，会发生什么？

**答案**：指针会一次跳过 257 字节，越过表尾哨兵和后续数据，读到表外的内存。解释器循环没有长度校验，这是这种「信任数据」的紧凑格式的固有风险——好在表是编译期常量，写错会在第一次调试时就暴露。

### 4.2 模块二：矩形窗口与批量像素传输

#### 4.2.1 概念说明

有了字节通道，怎么画一块区域？ILI9341 的窗口机制是关键：先用 `0x2A/0x2B` 在显存里圈出一个矩形，再发 `0x2C`，之后流入的每个像素自动**按行优先顺序**填进窗口并自动换行。于是「画一个矩形」=「设窗口 + 连续灌 W×H 个像素」，不需要逐像素发地址——这就是批量传输（bulk）的本质。

> **阅读提示**：`nanosdr.h:209` 声明了 `void ili9341_bulk(int x, int y, int w, int h);`，但整个仓库里**没有它的实现**（`ili9341_test` 同样只有 main.c:1021-1022 的注释调用）。真正承担「批量传一块矩形」职责的是 `ili9341_draw_bitmap()`。声明遗留自其他项目，链接器不会报错，因为无人调用——读开源代码时要学会识别这种「化石声明」。

#### 4.2.2 核心流程

```
画任意矩形 (x, y, w, h)：
  ├─ CASET(0x2A): [x, x+w-1]      列窗口
  ├─ PASET(0x2B): [y, y+h-1]      行窗口
  ├─ RAMWR(0x2C)                  进入写入模式
  └─ 连续送 w*h 个 16 位像素
       ├─ ili9341_fill:   CPU 循环发同一个颜色（纯色块）
       └─ ili9341_draw_bitmap: DMA 从内存 buf 搬 w*h 个半字（位图）
```

地址字节采用大端 16 位：`{x>>8, x, (x+w-1)>>8, (x+w-1)}` 就是「起始列高 8 位、低 8 位、结束列高 8 位、低 8 位」。

#### 4.2.3 源码精读

单像素函数 `ili9341_pixel()`——设一个窗口再写一个颜色。注意它写的窗口是 `[x, x+1]`（两个像素宽）却只送了一个像素的数据，按源码逐字阅读即可发现这个细节；实际固件中显示刷新都走下面的 fill/draw_bitmap 路径：

[ili9341.c:212-221](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L212-L221)

```c
void ili9341_pixel(int x, int y, int color)
{
	uint8_t xx[4] = { x >> 8, x, (x+1) >> 8, (x+1) };
	uint8_t yy[4] = { y >> 8, y, (y+1) >> 8, (y+1) };
	uint8_t cc[2] = { color >> 8, color };
	send_command(0x2A, 4, xx);
    send_command(0x2B, 4, yy);
    send_command(0x2C, 2, cc);
}
```

纯色填充 `ili9341_fill()`——设完窗口后 CPU 死循环发同一个颜色值，`len = w * h` 次。简单但占用 CPU：

[ili9341.c:225-235](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L225-L235)

```c
void ili9341_fill(int x, int y, int w, int h, int color)
{
	uint8_t xx[4] = { x >> 8, x, (x+w-1) >> 8, (x+w-1) };
	uint8_t yy[4] = { y >> 8, y, (y+h-1) >> 8, (y+h-1) };
    int len = w * h;
	send_command(0x2A, 4, xx);
    send_command(0x2B, 4, yy);
    send_command(0x2C, 0, NULL);
    while (len-- > 0) 
      ssp_senddata16(color);
}
```

**本模块的主角** `ili9341_draw_bitmap()`：设完窗口后，把 `buf` 指向的 `w*h` 个半字交给 DMA 一次性搬走（`MINC` = 内存地址自增，`dmaWaitCompletion` 同步等完）。与 fill 的区别一目了然——数据源从「CPU 反复发同一个值」变成「DMA 扫一段内存」：

[ili9341.c:237-252](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L237-L252)

```c
void ili9341_draw_bitmap(int x, int y, int w, int h, uint16_t *buf)
{
	uint8_t xx[4] = { x >> 8, x, (x+w-1) >> 8, (x+w-1) };
	uint8_t yy[4] = { y >> 8, y, (y+h-1) >> 8, (y+h-1) };
    int len = w * h;

	send_command(0x2A, 4, xx);
	send_command(0x2B, 4, yy);
	send_command(0x2C, 0, NULL);

    dmaStreamSetMemory0(dmatx, buf);
    dmaStreamSetTransactionSize(dmatx, len);
    dmaStreamSetMode(dmatx, txdmamode | STM32_DMA_CR_MINC);
    dmaStreamEnable(dmatx);
    dmaWaitCompletion(dmatx);
}
```

`spi_buffer` 的容量约束是所有上层绘制函数的天花板：待画矩形的像素数必须满足 \( w \times h \le 4096 \)。display.c 里有一段现成的「算给你看」的注释（瀑布图按 46×88 分块，4048 < 4096）：

[display.c:869](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L869)

```c
/* 46 * 88 = 4048 pixels < sizeof spi_buffer (4096) */
```

display.c 甚至把 `spi_buffer` 直接重铸成二维数组用（频谱图块），说明它就是全固件共用的显存暂存区：

[display.c:797](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L797)

```c
	uint16_t (*block)[32] = (uint16_t (*)[32])spi_buffer;
```

颜色由 `RGB565` 宏打包。**注意参数顺序是 (b,g,r)**，而位分配是 r 在高位——读代码时别想当然：

[nanosdr.h:189](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L189)

```c
#define RGB565(b,g,r)     ( (((r)<<8)&0xf800) | (((g)<<3)&0x07e0) | (((b)>>3)&0x001f) )
```

display.c 用它定义界面配色，例如深红底色与亮绿前景：

[display.c:536-539](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L536-L539)

```c
#define BG_ACTIVE RGB565(15,10,10)
#define FG_ACTIVE RGB565(128,255,128)
```

#### 4.2.4 代码实践：手算 RGB565 与容量红线

1. **实践目标**：建立对 16 位颜色值和缓冲容量的数量级直觉。
2. **操作步骤**：
   - 在 PC 上（Python 或 C）按 `nanosdr.h:189` 的宏**原样**实现 `rgb565(b,g,r)`；
   - 计算 `RGB565(15,10,10)` 和 `RGB565(128,255,128)`，与 display.c:536/539 的用途对照；
   - 再计算几个候选矩形（如 64×64、46×88、96×48、320×16）的像素数，判断哪些能放进 `spi_buffer`。
3. **需要观察的现象**：`RGB565(15,10,10)` 应得到高位为红的暗色值（\( (15 \ll 8) \& 0xf800 = 0x7800 \)，加上绿 \( (10 \ll 3) \& 0x7e0 = 0x0500 \)、蓝 \( 15 \gg 3 = 1 \)，合计 **0x7D01**）；`RGB565(128,255,128)` 应得到接近**亮绿**的 0x87F0。
4. **预期结果**：像素数 ≤ 4096 的矩形（64×64=4096、46×88=4048、320×16=5120？——注意 320×16=5120 **超限**）里，只有前两者能直接用 `spi_buffer` 一次画完；320×16 必须像 display.c:1026 那样按行切分（`ili9341_draw_bitmap(0, vsa, 320, 1, spi_buffer)` 一次只画 1 行）。
5. 数值可手工复核，宏实现运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ili9341_fill()` 不用 DMA？

**答案**：DMA 需要一段**各不相同的**内存数据才有意义；fill 发送的是 \( w \times h \) 个相同的值，直接 CPU 循环写 DR 即可，反而省去了先在内存里铺满同值的开销。（若追求极致可以配置 DMA 不带 MINC 从单个地址重复搬，但收益有限。）

**练习 2**：如果调用 `ili9341_draw_bitmap(x, y, w, h, buf)` 时 `w*h > 4096` 且 `buf` 就是 `spi_buffer`，会发生什么？

**答案**：DMA 事务长度是 `w*h` 个半字，会从 `spi_buffer` 起始地址一路搬出数组边界，读越界内存——屏幕上出现乱码像素，且可能踩坏其他全局变量。这就是 display.c 所有绘制函数都先做分块的原因。

**练习 3**：`send_command(0x2C, 0, NULL)` 之后再 `send_command(0x2A, ...)` 开始新矩形，需要显式终止上一次写入吗？

**答案**：不需要。每次写 CASET/PASET 都会重置地址指针和窗口，ILI9341 收到新命令即结束上一轮 RAMWR 数据流；驱动正是无状态地反复「设窗口→写」。

### 4.3 模块三：Font5x7.c——5×7 点阵字库与字符渲染

#### 4.3.1 概念说明

屏幕上最小的文字是 5×7 点阵：每个字符占 5 列 × 7 行的点。`Font5x7.c` 是一张从 X11 BDF 字体由 `convbdf` 工具生成于 2000 年的经典字库（文件头注释写明），覆盖 0x00–0xFF 共 256 个字符，每个字符 7 个 `uint16_t`（每行一个字）。它被用于状态栏文字、刻度标注等处（display.c:1084 等大量调用 `ili9341_drawstring_5x7`）。

#### 4.3.2 核心流程

字模编码规则（MSB 在左）：

```
字符 'A'（0x41）的 7 个字：
  0x6000 → 0110 0000 ... → 第 1 行第 2、3 列点亮
  0x9000 → 1001 ...       → 第 2 行第 1、4 列点亮
  0x9000
  0xF000 → 1111 ...       → 第 4 行整行点亮（横杠）
  0x9000
  0x9000
  0x0000                  → 第 7 行空（字体的 descent 行）

渲染一个字符：
  for 每行 c (0..6):
     取 x5x7_bits[ch*7 + c]
     for 每列 r (0..4):
        缓冲[c][r] = (bit15 == 1) ? fg : bg    ← 位左移逐次取出
  ili9341_draw_bitmap(x, y, 5, 7, spi_buffer)  ← 整字符一次 DMA
```

即：**最高位 bit15 对应最左列，向低位方向依次是第 2~5 列，bit11 以下不使用**。

#### 4.3.3 源码精读

字库数组定义与典型的字模注释块——`convbdf` 为每个字符生成了 ASCII 艺术图，是读位编码最好的教材：

[Font5x7.c:12](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Font5x7.c#L12)

```c
/* Font character bitmap data. */
const uint16_t x5x7_bits [] =
{
```

[Font5x7.c:1250-1267](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Font5x7.c#L1250-L1267)

```c
  /* Character (0x41):
     bbw=5, bbh=7, bbx=0, bby=-1, width=5
     +----------------+
     | **             |
     |*  *            |
     |*  *            |
     |****            |
     |*  *            |
     |*  *            |
     |                |
     +----------------+ */
  0x6000,
  0x9000,
  0x9000,
  0xf000,
  0x9000,
  0x9000,
  0x0000,
```

字符 '0'（0x30）同理，末行 0x0000 同样空出：

[Font5x7.c:927-944](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Font5x7.c#L927-L944)

```c
  /* Character (0x30):
     | **    |
     |*  *   |
     |* **   |
     |** *   |
     |*  *   |
     | **    |
     |       |   */
  0x6000, 0x9000, 0xb000, 0xd000, 0x9000, 0x6000, 0x0000,
```

字符渲染函数：双重循环把 7×5 个像素展开到 `spi_buffer`，`0x8000 & bits` 测试当前最左可用位，然后 `bits <<= 1` 让下一位登场；最后**一个字符一次 DMA**：

[ili9341.c:254-268](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L254-L268)

```c
void
ili9341_drawchar_5x7(uint8_t ch, int x, int y, uint16_t fg, uint16_t bg)
{
  uint16_t *buf = spi_buffer;
  uint16_t bits;
  int c, r;
  for(c = 0; c < 7; c++) {
    bits = x5x7_bits[(ch * 7) + c];
    for (r = 0; r < 5; r++) {
      *buf++ = (0x8000 & bits) ? fg : bg;
      bits <<= 1;
    }
  }
  ili9341_draw_bitmap(x, y, 5, 7, spi_buffer);
}
```

字符串函数只是逐字符推进 x 坐标（步进 5，无字间距）：

[ili9341.c:270-278](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L270-L278)

```c
void
ili9341_drawstring_5x7(const char *str, int x, int y, uint16_t fg, uint16_t bg)
{
  while (*str) {
    ili9341_drawchar_5x7(*str, x, y, fg, bg);
    x += 5;
    str++;
  }
}
```

一个巧妙设计：控制字符区（0x19–0x1F）被这个定制字库放进了 π、µ、Ω、°、→ 等电台界面常用符号，`nanosdr.h` 给它们起了名字，让 C 字符串里可以直接写 `S_DEGREE` 拼出「dBµV」之类的单位：

[nanosdr.h:176-180](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L176-L180)

```c
#define S_PI    "\034"
#define S_MICRO "\035"
#define S_OHM   "\036"
#define S_DEGREE "\037"
#define S_RARROW "\033"
```

#### 4.3.4 代码实践：手工展开一个字模

1. **实践目标**：确认你能双向转换「位图 ↔ 16 进制字模」。
2. **操作步骤**：
   - 在纸上画 7×5 方格；从 [Font5x7.c:1250](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Font5x7.c#L1250) 抄下 'A' 的 7 个字；
   - 逐行把 `0x6000` 展开成二进制 `0110 0000 0000 0000`，在最左 5 格里点出第 2、3 格；
   - 完成后与文件里的 ASCII 艺术图逐格对照；
   - 反向再来一次：自己给字符 'T' 设计 7 行点阵，按 bit15→bit11 编码写出 7 个 16 进制值。
3. **需要观察的现象**：'A' 展开后是一个「两竖一横」的塔形；'T' 应得到形如 `0xF800, 0x0800(仅中列), ...` 的序列（具体值以你画的图为准）。
4. **预期结果**：正反两个方向都能闭环，说明编码规则已吃透。
5. 纯纸面推导，无需运行；'T' 的标准答案不在本仓库字库之外的地方，**以你自己的点阵设计为答案**。

#### 4.3.5 小练习与答案

**练习 1**：`x5x7_bits` 数组总共占多少字节？

**答案**：256 字符 × 7 字 × 2 字节 = 3584 字节，存放在 Flash（`const`）。

**练习 2**：为什么每行用一个 16 位字而不是 8 位字（5 位就够）？

**答案**：MSB 对齐（bit15 为第 1 列）让「取位」变成 `0x8000 & bits` + 左移，无需掩码运算；8 位字也能做到但移位测试位不同。另外该字库是 convbdf 针对通用 framebuffer 字体的历史产物，16 位字是其固定输出格式，驱动沿用了它。

**练习 3**：`ili9341_drawstring_5x7` 每个字符调用一次 `draw_bitmap`，一个 20 字符的字符串要多少次窗口设置和 DMA？

**答案**：20 次「CASET+PASET+RAMWR+DMA」。对小字号文本这是可接受的开销；更大的数字/图标则用下一模块的 `font_t` 一次画更大的块，减少相对开销。

### 4.4 模块四：font_t 大字号数字与图标字库

#### 4.4.1 概念说明

主频率显示需要 32×48 的大数字，模式指示需要 48×20 的图标。这些大字形若沿用「每字符一个函数硬编码」会很难维护，于是固件抽象出一个通用字库描述结构 `font_t`：**字宽、字高、垂直缩放、每字符字数（slide）、每行字数（stride）+ 位图基址**，一套渲染函数 `ili9341_drawfont()` 吃所有 `font_t`。数字字库按行取模、每行是一个（或多个）32 位字，位序同样是 MSB 在左。

#### 4.4.2 核心流程

`font_t` 各字段的几何关系：

- 每行像素占 \( \lceil width / 32 \rceil \) 个 32 位字，这个值就是 `stride`；
- 每字符总字数 = `stride ×` 行数（源数据行数），记作 `slide`；
- 实际显示高度 = 源数据行数 × `scaley`（垂直放大：每行重复画 `scaley` 遍）；
- 字符 ch 的位图起点 = `bitmap + slide × ch`。

```
ili9341_drawfont(ch, font, ...):
  bitmap = &font->bitmap[slide * ch]
  for 源数据行 c = 0; c < slide; c += stride:      ← 每次跳一行
    for j = 0..scaley-1:                            ← 垂直放大
      cc = c
      for r = 0..width-1:                           ← 一行内的像素
        bits = bitmap[cc++]                         ← 取下一个 32 位字
        逐位（MSB 先）展开 32 个像素进缓冲
  draw_bitmap(x, y, width, height, spi_buffer)
```

#### 4.4.3 源码精读

`font_t` 结构定义：

[nanosdr.h:191-198](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L191-L198)

```c
typedef struct {
	uint16_t width;
	uint16_t height;
	uint16_t scaley;
	uint16_t slide;
	uint16_t stride;
	const uint32_t *bitmap;
} font_t;
```

四个现成实例——注意 `NF32x48` 复用 `numfont32x24` 的位图、仅把 `scaley` 设为 2 实现「拉高变体」；`ICON48x20` 因为 48 像素宽超过 32 位，`stride` 必须为 2：

[ili9341.c:282-285](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L282-L285)

```c
const font_t NF20x24 = { 20, 24, 1, 24, 1, (const uint32_t *)numfont20x24 };
const font_t NF32x24 = { 32, 24, 1, 24, 1, (const uint32_t *)numfont32x24 };
const font_t NF32x48 = { 32, 48, 2, 24, 1, (const uint32_t *)numfont32x24 };
const font_t ICON48x20 = { 48, 20, 1, 40, 2, (const uint32_t *)icons48x20 };
```

通用渲染函数——三层嵌套循环完成「行 → 垂直放大 → 行内像素」，`0x80000000UL & bits` 与 5×7 版本如出一辙，只是位宽从 16 变 32：

[ili9341.c:287-308](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L287-L308)

```c
void
ili9341_drawfont(uint8_t ch, const font_t *font, int x, int y, uint16_t fg, uint16_t bg)
{
	uint16_t *buf = spi_buffer;
	uint32_t bits;
	const uint32_t *bitmap = &font->bitmap[font->slide * ch];
	int c, r, j, b;

	for (c = 0; c < font->slide; c += font->stride) {
		for (j = 0; j < font->scaley; j++) {
			int cc = c;
			for (r = 0; r < font->width;) {
				bits = bitmap[cc++];
				for (b = 0; b < 32 && r < font->width; b++,r++) {
					*buf++ = (0x80000000UL & bits) ? fg : bg;
					bits <<= 1;
				}
			}
		}
	}
    ili9341_draw_bitmap(x, y, font->width, font->height, spi_buffer);
}
```

字符串接口把「字符」翻译成「字形槽位号」：数字直接映射 0–9；**控制字符 \001–\006 映射到槽位 10–15**（`c + 9`）；'.'→10、'-'→11；其余字符画空块。**槽位 10 之后放什么图案由各字库文件自己约定**，两份数字字库的内容并不相同（numfont32x24.c 的注释把槽位 10 标为 Hz、11 标为 k；display.c 调用 NF20x24 时注释把槽位 13 当 dB、22/23 当 d/Bm 用），读代码时要「字库文件 + 调用处」两边对看：

[ili9341.c:310-327](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L310-L327)

```c
void
ili9341_drawfont_string(const char *str, const font_t *font, int x, int y, uint16_t fg, uint16_t bg)
{
  while (*str) {
    char c = *str++;
    if (c >= '0' && c <= '9')
      ili9341_drawfont(c - '0', font, x, y, fg, bg);
    else if (c > 0 && c < 7)
      ili9341_drawfont(c + 9, font, x, y, fg, bg);
    else if (c == '.')
      ili9341_drawfont(10, font, x, y, fg, bg);
    else if (c == '-')
      ili9341_drawfont(11, font, x, y, fg, bg);
    else
      ili9341_fill(x, y, font->width, font->height, bg);
    x += font->width;
  }
}
```

两份数字字库的位图按行取模，每行一个 32 位字、二进制字面量直接可读（这是 C99 `0b` 扩展写法，GCC 支持）。槽位 0 是数字 '0'，槽位 10/11 分别被注释标为 Hz 和 k：

[numfont32x24.c:23-50](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/numfont32x24.c#L23-L50)

```c
const uint32_t numfont32x24[][24] = {
		{	// 0
		0b00000000001111111111000000000000,
		0b00000001111111111111111000000000,
		...
```

[numfont32x24.c:304-332](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/numfont32x24.c#L304-L332)

```c
		{	// Hz = \001
		...
		{	// k
```

图标库同样按行取模，但每行 **2 个** 32 位字（48 像素宽），注释直接标明图标顺序——CW、LSB、USB、AM、FM、STEREO、OFF、SLOW、MID、FAST，另有几个 LSB 变体。`nanosdr.h` 的 `ICON_AGC_OFF` 值为 6，正对应第 7 个图标「OFF」：

[icons.c:23-27](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/icons.c#L23-L27)

```c
const uint32_t icons48x20[][2*20] = {
		{	// CW
		0b00001111111111111111111111111111, 0b11111111111100001000000000000000,
		...
```

[nanosdr.h:182](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L182)

```c
#define ICON_AGC_OFF 6
```

上层 display.c 用「下标即语义」的方式取图标：调制模式图标直接用 `uistat.modulation` 作槽位（0=CW、1=LSB、2=USB、3=AM、4=FM、5=STEREO），AGC 图标用 `agcmode + ICON_AGC_OFF`（0/1/2/3 档映射到 OFF/SLOW/MID/FAST 槽位 6–9）：

[display.c:1279-1283](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1279-L1283)

```c
	ili9341_drawfont(uistat.modulation, &ICON48x20, x+2, y+2, fg, bg);
	...
	ili9341_drawfont(uistat.agcmode + ICON_AGC_OFF, &ICON48x20, x+2, y+2, fg, bg);
```

#### 4.4.4 代码实践：font_t 字段自洽性校验

1. **实践目标**：验证你理解的 `font_t` 几何关系与四个实例的数据完全自洽。
2. **操作步骤**：对 `NF20x24`、`NF32x24`、`NF32x48`、`ICON48x20` 逐一计算：
   - `stride` 是否等于 \( \lceil width / 32 \rceil \)；
   - `slide` 是否等于 `stride ×` 位图数组的第二维长度（numfont 系列是 24 行、icons 是 20 行）；
   - `height` 是否等于「行数 × scaley」；
   - 渲染一个字符需要的 `spi_buffer` 像素数 \( width \times height \) 是否 ≤ 4096。
3. **需要观察的现象**：四个实例全部满足关系式，例如 ICON48x20：\( \lceil 48/32 \rceil = 2 = stride \)，\( 2 \times 20 = 40 = slide \)，\( 20 \times 1 = 20 = height \)，\( 48 \times 20 = 960 \le 4096 \)。
4. **预期结果**：四个实例均自洽；NF32x48 像素数 \( 32 \times 48 = 1536 \) 也远在容量内。若任一关系不成立，说明你对某个字段的理解有偏差，回到 4.4.2 的流程图重新推。
5. 纯推导可纸面完成；如写脚本核对数组维度，运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：想新增一个 64×32、8 行 2 列……不，按本固件思路新增一个 64 像素宽、24 像素高（1 倍高）的字库，`font_t` 各字段应填什么？

**答案**：\( stride = \lceil 64/32 \rceil = 2 \)；位图每字符 \( 2 \times 24 = 48 \) 个字，即 `slide = 48`；`scaley = 1`；所以 `{ 64, 24, 1, 48, 2, bitmap }`。注意渲染时 \( 64 \times 24 = 1536 \le 4096 \)，缓冲放得下。

**练习 2**：`NF32x48` 和 `NF32x24` 共享同一份位图，这样做的取舍是什么？

**答案**：省一份约 24 字 × 若干槽位 × 4 字节的 Flash（每槽位 96 字节）；代价是「拉高」只是简单重复每行，字形是纵向拉伸而非重新设计的高字身观感。对频率显示这种大数字，拉伸观感可接受。

**练习 3**：`ili9341_drawfont_string` 里 `c > 0 && c < 7` 的分支为什么不用可读性更好的判断（如 `isprint`）？

**答案**：这是一张极小的「字符→槽位」映射表，控制字符 \001–\006 被刻意用作「单位符号/图标」的转义入口（配合 `c + 9` 落到槽位 10–15），分支顺序保证数字优先、转义其次、'.'/'-' 再其次、其余一律空白填充，逻辑紧凑且无默认字形需求。

### 4.5 模块五：lcd 命令与 180 度旋转

#### 4.5.1 概念说明

整机装进外壳时屏幕可能上下颠倒（USB 口朝向不同），需要 180 度旋转显示。这**不需要重写任何绘制代码**：ILI9341 的 MADCTL（0x36）寄存器控制显存扫描方向，翻转 X、Y 两个方向的扫描位，整幅画面就倒过来了——坐标系层面的旋转，一次寄存器写入完成。

#### 4.5.2 核心流程

MADCTL（0x36）各 bit 的作用（仅列本讲用到的）：

| bit | 名称 | 作用 |
|---|---|---|
| bit7 | MY | 翻转 Y（行）扫描方向 |
| bit6 | MX | 翻转 X（列）扫描方向 |
| bit5 | MV | 行列交换（横屏/竖屏切换的关键位） |
| bit3 | BGR | RGB/BGR 色序选择 |

```
默认横屏：      0x36 ← 0x28        (MV=1, BGR=1)
lcd 1（旋转）：  0x36 ← 0x28|0xC0 = 0xE8   (再叠加 MY|MX)
效果：所有后续写入的矩形窗口在屏幕上的物理位置中心对称
```

注意 `MY|MX` 同时翻转等效于坐标系绕中心旋转 180°，而 MV 保持 1 仍是横屏。

#### 4.5.3 源码精读

`ili9341_set_direction()`——一个参数、三行逻辑，把 0xC0 叠加到横屏基值上：

[ili9341.c:329-338](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L329-L338)

```c
void
ili9341_set_direction(int rot180)
{
  char value = 0x28; // landscape
  if (rot180) {
    value |= 0xc0; // reverse X and Y axis
  }

  send_command(0x36, 1, &value);
}
```

shell 的 `lcd` 命令：解析参数后旋转、**把选择记入 config 以便掉电保存**（呼应 [u4-l5](u4-l5-flash-config.md) 的 Flash 持久化）、再 `disp_init()` 全量重画（因为旋转后旧画面位置全错，必须整屏刷新）：

[main.c:862-872](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L862-L872)

```c
static void cmd_lcd(BaseSequentialStream *chp, int argc, char *argv[])
{
  if (argc != 1) {
    chprintf(chp, "usage: lcd {rotate 180}\r\n");
    return;
  }
  int rot = atoi(argv[0]);
  ili9341_set_direction(rot);
  config.lcd_rotation = rot;
  disp_init(); // refresh all
}
```

开机时在 `main()` 里按存储的配置恢复方向（在 `disp_init()` 之后调用，画完再转，避免半途变换）：

[main.c:1020-1029](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1020-L1029)

```c
  /*
   * SPI LCD Initialize
   */
  ili9341_init();
  ...
  /*
   * Initialize display
   */
  disp_init();
  ili9341_set_direction(config.lcd_rotation);
```

顺带一提，初始化序列里也能看到方向位的「出厂值」——`0x36` 写 0x28（横屏），旁边注释保留了竖屏 0x48 的备选：

[ili9341.c:150-152](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L150-L152)

```c
		// MEMORY_ACCESS_CONTROL
		//0x36, 1, 0x48, // portlait
		0x36, 1, 0x28, // landscape
```

#### 4.5.4 代码实践：预测 MADCTL 的值并上机验证

1. **实践目标**：把寄存器位操作变成可预判的确定性计算。
2. **操作步骤**：
   - 在 PC 上写一行代码 `value = 0x28 | (rot ? 0xC0 : 0)`，对 `rot = 0/1` 分别求值；
   - 手工展开二进制，标出 MY/MX/MV/BGR 各位；
   - 有硬件的话：通过 shell 依次执行 `lcd 1` 和 `lcd 0`（用法见 main.c:865 的 usage 提示），观察屏幕方向翻转。
3. **需要观察的现象**：`rot=0` → 0x28（0b0010_1000，MV=1、BGR=1）；`rot=1` → **0xE8**（0b1110_1000，再叠加 MY=MX=1）。上机时 `lcd 1` 后整个界面（频谱、频率数字、图标）应整体上下颠倒，文字仍可读（不是镜像）。
4. **预期结果**：寄存器值预测与源码一致；旋转是「旋转」而非「镜像」——因为 MY、MX 同时置位。断电重启后方向保持（config 已保存）。
5. 寄存器值可离线确定；屏幕实际效果**待本地验证**（需要硬件）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `cmd_lcd` 末尾要调用 `disp_init()`，而 `ili9341_set_direction()` 本身不重画？

**答案**：驱动层只管寄存器、无画面概念（分层原则）；旋转后已画内容物理位置全部错位，重画是**显示模块**的职责，所以命令层在改完方向和 config 后调用 `disp_init()` 触发全量刷新。

**练习 2**：如果只置 MY 不置 MX（value = 0x68），画面会怎样？

**答案**：只翻转一个轴，得到**上下镜像**（不可读的文字），而不是 180° 旋转。要旋转必须 MY、MX 成对置位。（固件未提供此选项，但你可以本地改 `ili9341_set_direction` 实验。）

**练习 3**：`config.lcd_rotation` 在哪两个时刻被读取？

**答案**：开机 `main()` 中 `ili9341_set_direction(config.lcd_rotation)`（main.c:1029）恢复上次选择；`cmd_lcd` 执行时写入新值。它随 `config_t` 一起被 Flash 持久化（u4-l5 展开）。

## 5. 综合实践

把本讲三个核心（字模位编码、spi_buffer 展开、批量绘制）串成一个任务：**仿照 `ili9341_drawchar_5x7` 写一个 `ili9341_drawstring_vert`，让 5×7 字符串竖排显示在屏幕左上角**（第 1 个字符在最上方，后续字符向下排）。

### 5.1 实践目标

- 检验你能否独立复用「取位→填缓冲→一次 draw_bitmap」的渲染范式；
- 体会坐标推进方向的自由性：横排是 `x += 5`，竖排只是改成 `y += 7`。

### 5.2 操作步骤

1. 在 `ili9341.c` 中仿照 [ili9341.c:270-278](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L270-L278) 新增（示例代码，非项目原有）：

```c
/* 示例代码：竖排绘制 5x7 字符串 */
void
ili9341_drawstring_vert(const char *str, int x, int y, uint16_t fg, uint16_t bg)
{
  while (*str) {
    ili9341_drawchar_5x7(*str, x, y, fg, bg);   /* 复用单字符渲染 */
    y += 7;                                     /* 改为向下推进一个字高 */
    str++;
  }
}
```

2. 在 `nanosdr.h` 的 ili9341.c 声明区（[nanosdr.h:212-213](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L212-L213) 附近）加上原型，并在某条 shell 命令（如自拟的 `hello`）里调用它画 `"CENTSDR"`。
3. **没有硬件时的 PC 模拟验证**：把 `ili9341_drawchar_5x7` 的双层循环提取成纯 C/Python 函数，用 `x5x7_bits` 的真实数据（从 Font5x7.c 抄 'C','E','N','T','S','D','R' 各 7 个字）填一张 7 列 × 49 行的字符矩阵，1 用 `#`、0 用 `.` 打印出来。
4. 模拟器中同样实现「y += 7」的竖排版本，打印 7 个字符竖着排的位图。

### 5.3 需要观察的现象

- PC 模拟输出中，每个字符 5 列宽、7 行高，字符间垂直方向紧贴（无行距）；
- 'C' 的字模两端开口、'E' 三横一竖等形态特征清晰可辨；
- 竖排矩阵总高 \( 7 \times 7 = 49 \) 行。

### 5.4 预期结果

- PC 模拟打印出的每个字形与 Font5x7.c 注释里的 ASCII 艺术图逐格一致，证明「位数据 + 取位逻辑」理解无误；
- 烧录后（如有硬件）屏幕左上角出现自上而下排列的 `CENTSDR` 竖排文字，每个字符一次 DMA，共 7 次。
- 硬件效果**待本地验证**；PC 模拟部分可直接运行验证。

## 6. 本讲小结

- **SPI 驱动**：`send_command()` 用 CS/DC 两根 GPIO 配合 8 位 SPI 写寄存器；初始化序列是「(cmd, len, data...) + 哨兵」扁平表，由解释器循环灌入，与 TLV320AIC3204 驱动同款模式。
- **批量绘制**：ILI9341 的窗口机制（CASET/PASET/RAMWR）让一个矩形只需设一次地址、连灌 \( w \times h \) 个像素；`ili9341_fill` 用 CPU 循环发同色，`ili9341_draw_bitmap` 用 DMA 搬内存位图——后者是全固件画图的主力。`nanosdr.h` 里声明的 `ili9341_bulk` 并无实现，是遗留的「化石声明」。
- **公共缓冲**：`spi_buffer`（4096 像素 = 8KB）是驱动与 display.c 共享的工作台，任何单次绘制的矩形都必须满足 \( w \times h \le 4096 \)。
- **5×7 字库**：`x5x7_bits` 每字符 7 个 `uint16_t`、MSB 对齐最左列；控制字符区被定制成 π/µ/Ω/°/→ 符号（`S_PI` 等宏）。
- **font_t 字库**：`width/height/scaley/slide/stride` 五个字段完整描述任意宽度字形的按行取模布局，`drawfont` 一套循环渲染数字、图标、拉伸变体；图标槽位与 `uistat.modulation`、`agcmode + ICON_AGC_OFF` 直接对应。
- **180° 旋转**：MADCTL（0x36）寄存器在横屏基值 0x28 上叠加 MY|MX（0xC0）得 0xE8，一次写入整屏掉头；`lcd` 命令还负责把选择写入 config 并全量重画。

## 7. 下一步学习建议

- **横向**：阅读 display.c 中 `draw_spectrogram`/`draw_waterfall`（u4-l1、u4-l2）如何把频谱柱和伪彩色行塞进 `spi_buffer` 分块送屏，那是本讲批量绘制机制最大的消费者。
- **纵向（下一单元）**：屏幕上跳动的波形来自 [u2-l3](u2-l3-i2s-audio-stream.md) 讲过的 I2S 解调回调；单元三首篇 [u3-l1](u3-l1-nco-fixed-point.md) 将进入 `dsp.c`，从定点数与 NCO 开始拆解这些数据的来源。
- **动手**：完成综合实践后，可尝试给 `font_t` 设计一个 8×16 的小字库替换 5×7（生成工具可自写脚本），体会「字库即数据、渲染即循环」的分层乐趣。
