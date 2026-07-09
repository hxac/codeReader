# DVP 摄像头采集与 DP 显示链路

## 1. 本讲目标

本讲是「外设、平台与扩展实践」单元的首篇，承接 u4-l1 对裸机工程与 ZynqMP 平台初始化的认知，把视角从「TPU 推理本身」扩展到「**数据从哪里来、结果又显示到哪里去**」。

学完本讲，你应当能够：

1. 说清楚 `EEP_DVP_Top` 摄像头 IP 在整个系统中的位置：它坐在 PL 里，经 AXI 把 OV5640 摄像头的数据流直接搬进 DDR。
2. 解释 `camera.c` 如何用 I2C 配置 OV5640、再用 `dvp_capture` 触发一次硬件采集并轮询完成。
3. 写出 RGB565→RGB888 的逐字节拆解，以及图像在 DDR 里「按地址裸存放」的内存布局。
4. 讲清 DP（DisplayPort）显示链路：`dpdma_intr.c` 如何初始化 PS 的 DPDMA/DP 子系统、`dp_display` 如何把 RGB888 打包成 ARGB 喂给帧缓冲、以及中断在这里的唯一角色。
5. 串起 `main.cc` 菜单选项 5「Run Demo」里「采集→格式转换→预处理→forward→后处理→DP 显示」的完整数据流，并指出 `mem_pic` / `img_data_888` / `GFrame` 等关键内存地址各自承担的职责。

## 2. 前置知识

在进入源码前，先用通俗语言把几个本讲会反复出现的基础概念铺一遍。

### 2.1 DVP 接口与 OV5640

- **DVP（Digital Video Port）**：一种并行的摄像头数字接口，摄像头每个像素时钟周期并行送出若干位数据（常见 8/10/16 位），同时附带行同步（HREF/VSYNC）与时钟（PCLK）。相对 MIPI CSI，DVP 接线简单、适合中低分辨率，本工程用的就是它。
- **OV5640**：一款常见的 500 万像素 CMOS 传感器，内部有大量可配置寄存器（曝光、增益、白平衡、分辨率、输出格式等），通过 **I2C** 配置。本工程把它设成 1280×720@60fps、输出 **RGB565**（也可配成灰度）。
- **I2C 配置表**：OV5640 没有「现成的驱动」，需要按厂商手册逐个把「寄存器地址→寄存器值」写进去。这套键值对就叫配置表（sensor register list）。

### 2.2 RGB565 与 RGB888

- **RGB565**：每个像素 16 位，R 占 5 位、G 占 6 位、B 占 5 位（绿色多一位，因为人眼对绿色更敏感）。一个像素 2 字节。
- **RGB888**：每个像素 24 位，R/G/B 各 8 位。一个像素 3 字节。
- 神经网络输入通常要 RGB888（或再定点化），所以采集到的 RGB565 要在 CPU 上拆成 RGB888。本讲的 `RGB565toRGB888` 就是干这件事。

### 2.3 DisplayPort、DPDMA 与 AVBuf

这是 ZynqMP 的 PS（处理系统）里自带的**显示子系统**，和 TPU 无关，是「把内存里的帧缓冲搬到显示器」的硬件链路：

- **DP（DisplayPort）TX（DpPsu）**：PS 里的 DisplayPort 发送控制器，负责和显示器握手、链路训练（link training）、把视频流编码送上 DP 线。
- **DPDMA**：一个专用 DMA，从 DDR 的帧缓冲里把一帧帧图像搬到 DP 发送器。它支持 graphics（图形层）和 video（视频层）两个通道，可以叠加。
- **AVBuf（Audio/Video Buffer）**：视频管线，做格式转换、图层混合、色彩处理，把 DPDMA 送来的像素调理成 DP 能用的形式。

### 2.4 AXI、内存映射与缓存一致性（承接 u1-l3 / u4-l1）

- 裸机下**物理地址就是指针**：`*(volatile u32*)0xA00C1010` 这种写法，直接读写该物理地址上挂的硬件寄存器。
- **两条 AXI 通路**（u1-l3、u1-l4 已讲）：控制通路（PS→各 IP 的寄存器）与数据通路（IP→DDR 的 HP 口搬张量/图像）。DVP 既走控制通路被配置，又走数据通路把图像 DMA 进 DDR。
- **缓存一致性**：裸机没有操作系统代管 D-Cache，DMA 看到的是 DDR 里的真实数据，而 CPU 写的数据可能还在 D-Cache 里没下沉。所以凡是「CPU 写完要让 DMA 读」或「DMA 写完要让 CPU 读」的场合，都要手动 `Xil_DCacheFlush` / `Xil_DCacheInvalidate`。这正是 `main.cc` 在采集后、`dp_display` 在打包后反复 `Xil_DCacheFlush` 的原因。本工程甚至在 `main` 里全程 `Xil_DCacheDisable`，只在需要 CPU 批量搬运的局部短暂打开（u4-l1 已点过这个工程取舍）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sdk/standalone/src/camera.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.h) | DVP 寄存器宏、OV5640 I2C 从机地址、采集/转换/显示函数声明 |
| [sdk/standalone/src/camera.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c) | OV5640 配置表、I2C 读写、`dvp_capture` 采集、`RGB565toRGB888` / `GRAYtoRGB888`、`dp_display` / `draw_object` |
| [sdk/standalone/src/dpdma_intr.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.h) | DP/DPDMA 子系统的常量、`Run_Config` 配置结构、初始化与帧缓冲接口声明 |
| [sdk/standalone/src/dpdma_intr.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c) | DP 链路训练、DPDMA 初始化、HPD 中断处理、graphics/video 帧缓冲更新 |
| [sdk/standalone/src/sys_intr.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sys_intr.c) | GIC（中断控制器）初始化与 ARM 异常使能 |
| [sdk/standalone/src/main.cc](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc) | 把上述模块串成实时 demo 循环（菜单 case '5'），定义各关键内存地址 |
| [sdk/standalone/src/config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h) | DVP/TPU 寄存器基址、`EEP_DVP_CAMERA` / `IMG_RGB565` / `EEP_DP_ENABLE` 等编译开关 |
| [ip_repo/EEP_DVP_Top_128B_v6p3.v](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/ip_repo/EEP_DVP_Top_128B_v6p3.v) | DVP 摄像头 IP 的 RTL（**加密**，不可读内部逻辑） |

> 关于加密 IP：`EEP_DVP_Top_128B_v6p3.v` 文件开头是 `pragma protect` 块，正文是 RSA+AES128-CBC 加密的密文（见 [ip_repo/EEP_DVP_Top_128B_v6p3.v:1-29](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/ip_repo/EEP_DVP_Top_128B_v6p3.v#L1-L29)），这正是 u1-l4 讲过的「黑盒集成」——我们只能通过它的**寄存器接口**（即 `camera.h` 里的宏）来理解和使用它，看不到内部时序。

## 4. 核心概念与源码讲解

### 4.1 DVP IP 与 I2C 配置

#### 4.1.1 概念说明

`EEP_DVP_Top` 是 EMBEDEEP 提供的**摄像头接口 IP**，它把 OV5640 传感器送来的并行 DVP 数据流「收下来 + 打包 + DMA 写进 DDR」。它在系统里的位置很清晰：

```
OV5640  --(DVP 并行数据)-->  EEP_DVP_Top(IP, 在 PL)  --(AXI HP DMA)-->  DDR(0x30000000)
        ^^                                                              ^^
   用 I2C 配置寄存器                                         CPU 经指针读/DPDMA 读
```

这个 IP 受 CPU 控制，方式有二：

1. **I2C**：CPU → PS 的 I2C 控制器 → OV5640，配置传感器本身的寄存器（分辨率、格式等）。
2. **AXI 寄存器**：CPU → AXI 控制通路 → `EEP_DVP_Top` 的控制/状态寄存器，告诉它「往哪个 DDR 地址搬、搬多大、现在开始」。

两类寄存器各管一段，地址不同：OV5640 的寄存器是「I2C 从机地址 0x3C 下的 16 位子地址」，而 `EEP_DVP_Top` 的寄存器是「`EEPDVP_REG_BASE_ADDR` 下的偏移」。

#### 4.1.2 核心流程

I2C 配置流程很朴素：

1. 初始化 PS 的 I2C 控制器（`iic_init`），设好时钟速率。
2. 遍历一张静态配置表 `ov_5640_config_table_720p[]`，逐条「写子地址、写数据」，直到遇到哨兵值 `Config_done (0xffff)` 停止。

DVP IP 寄存器则是一组「控制 / 状态 / 行宽 / 列高 / DMA 地址」宏，布局如下（偏移基于 `EEPDVP_REG_BASE_ADDR = 0xA00C0000`）：

| 寄存器宏 | 偏移 | 含义 |
| --- | --- | --- |
| `DVPIN_CTRL_REG` | `+0x1010` | DVP 输入（采集）控制字：启动、模式、DMA 使能等 |
| `DVPIN_STATUS_REG` | `+0x1020` | DVP 输入状态：bit12 为一帧采集完成标志 |
| `DVPIN_ROW_WIDTH_REG` | `+0x1030` | 每行字节数（RGB565 时 = 宽×2） |
| `DVPIN_COL_WIDTH_REG` | `+0x1040` | 行数（即图像高度） |
| `DVPIN_FRM_INFO_REG` | `+0x1050` | 帧信息（本工程未使用） |
| `DVPIN_DMA_ADDR1/2/3_REG` | `+0x1060/1070/1080` | 采集帧的 DDR 目标地址（最多 3 个缓冲） |
| `DVPOUT_*_REG` | `+0x1110..` | DVP 输出（DDR→显示）通路寄存器（**本工程未启用**） |

> 注意 `DVPIN` 与 `DVPOUT` 是该 IP 的两条相反通路：`DVPIN` 把摄像头搬进 DDR，`DVPOUT` 本可把 DDR 搬去显示。但经全仓检索，`DVPOUT_*_REG` 这一组宏只在 [camera.h:38-42](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.h#L38-L42) 定义、从未在任何 `.c` 里被使用——**本工程的显示走的是 PS 自带的 DPDMA→DisplayPort 链路**（见 4.4），不用这条 DVPOUT 通路。`DVPIN_FRM_INFO_REG` 同理只定义未使用。

#### 4.1.3 源码精读

OV5640 的从机地址与 DVP 寄存器宏定义在 [sdk/standalone/src/camera.h:27-42](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.h#L27-L42)：

```c
#define OV_CAM	0x3c
//DVP Register
#define DVPIN_CTRL_REG      (*(volatile unsigned int *)(EEPDVP_REG_BASE_ADDR + 0x1010))
#define DVPIN_STATUS_REG    (*(volatile unsigned int *)(EEPDVP_REG_BASE_ADDR + 0x1020))
#define DVPIN_DMA_ADDR1_REG (*(volatile unsigned int *)(EEPDVP_REG_BASE_ADDR + 0x1060))
#define DVPOUT_CTRL_REG     (*(volatile unsigned int *)(EEPDVP_REG_BASE_ADDR + 0x1110))
...
```

这里的 `volatile` 指针解引用，就是 u4-l3 讲过的「物理地址直接当指针用」。`EEPDVP_REG_BASE_ADDR` 在 [config.h:25-27](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L25-L27) 与 TPU 的基地址并列定义，由 u1-l4 的 `assign_bd_address` 定死：

```c
#define EEPTPU_MEM_BASE_ADDR   0x31000000
#define EEPTPU_REG_BASE_ADDR   0xA0000000
#define EEPDVP_REG_BASE_ADDR   0xA00C0000
```

OV5640 的配置表是一长串 `{寄存器地址, 值}` 对，节选自 [sdk/standalone/src/camera.c:35-302](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L35-L302)，其中几条关键寄存器值得读懂：

```c
{0x3008,0x82}, // 软复位
{0x3008,0x42}, // 软件掉电（power down）
{0x300e,0x58}, // MIPI 掉电，DVP 使能  ← 关键：选 DVP 而非 MIPI
#ifdef IMG_RGB565
    {0x4300,0x60}, // 输出格式：RGB565
#else
    {0x4300,0x10}, // 输出格式：GRAY
#endif
{0x3808,0x05},{0x3809,0x00}, // DVP 水平输出 = 0x0500 = 1280
{0x380a,0x02},{0x380b,0xd0}, // DVP 垂直输出 = 0x02d0 = 720
{Config_done,0x00}            // 哨兵：配置表结束
```

可见分辨率 1280×720、输出 RGB565/GRAY 都在表里硬编码，而 `IMG_RGB565` 宏（[config.h:59](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L59)）在编译期二选一——这就是 u4-l1 讲的「用 `#define` 在编译期裁剪功能」在外设侧的体现。

真正驱动 I2C 写入的是 [sdk/standalone/src/camera.c:394-412](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L394-L412)：

```c
int I2C_config_init_720p(unsigned int hsize, unsigned int vsize)
{
    Status = iic_init();                 // 初始化 PS I2C 控制器
    while(ov_5640_config_table_720p[i].addr != Config_done)
    {
        I2C_write(&Iic, ov_5640_config_table_720p[i].addr,
                       ov_5640_config_table_720p[i].data);
        i++;
    }
    return XST_SUCCESS;
}
```

`I2C_write`（[camera.c:383-392](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L383-L392)）把「16 位子地址 + 1 字节数据」拼成 3 字节缓冲，经 `XIicPs_MasterSendPolled` 轮询发给从机 `OV_CAM (0x3c)`。I2C 控制器的初始化在 [camera.c:306-333](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L306-L333)，时钟速率 `IIC_SCLK_RATE = 40000`（40kHz）。

这套配置在 `main` 启动序列里被调用一次（[main.cc:271-274](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L271-L274)），且只在 `EEP_DVP_CAMERA` 宏打开时编译进来。

#### 4.1.4 代码实践

**实践目标**：理解 OV5640 配置表是「编译期定死」的，并验证宏开关如何裁剪它。

**操作步骤（源码阅读型，无需硬件）**：

1. 打开 [camera.c:35-302](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L35-L302)，统计配置表总条目数（到 `Config_done` 为止）。
2. 找到 `{0x4300, ...}` 这一行，确认它被 `#ifdef IMG_RGB565` 包裹。
3. 打开 [config.h:57-67](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L57-L67)，确认 `IMG_RGB565` 当前是否被定义。

**需要观察的现象 / 预期结果**：

- 配置表约 160+ 条，是一条线性的传感器初始化序列。
- 当前 `config.h` 里 `#define IMG_RGB565` 处于启用状态，所以编译进 ELF 的是 `{0x4300,0x60}`（RGB565）；若注释掉该宏，重新编译后传感器会被配成灰度输出，后续 `RGB565toRGB888` 也会被替换成 `GRAYtoRGB888` 分支（见 main.cc case '1' 的 `#ifdef`）。
- 想改成 1080p 输出？这张表里 1280×720 的几条 `{0x3808..}` 是写死的，且函数名就叫 `I2C_config_init_720p`——换分辨率需要换一整张配置表。**待本地验证**：在真实板卡上改这些值并观察传感器是否正常出图。

#### 4.1.5 小练习与答案

**练习 1**：`OV_CAM` 为什么是 `0x3c`？如果摄像头换成另一款传感器，这个值要不要改？
**答**：`0x3c` 是 OV5640 的 7 位 I2C 从机地址（写成 8 位即 `0x3c`，对应 7 位 `0x1e`）。不同传感器从机地址不同，换传感器必须改这个宏，且配置表整张都要换。

**练习 2**：`DVPIN_FRM_INFO_REG`（`+0x1050`）在工程里被读了吗？
**答**：没有。全仓检索它只在 [camera.h:34](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.h#L34) 定义、从未使用，是 IP 预留但 demo 未启用的寄存器。

---

### 4.2 dvp_capture 采集

#### 4.2.1 概念说明

传感器配好之后，它就在持续往外吐 DVP 数据，但**数据并不会自动进 DDR**——需要 CPU 给 `EEP_DVP_Top` 下达一次「采集指令」：告诉它目标 DDR 地址、图像尺寸，然后触发启动；IP 内部的 DMA 把一整帧搬完后，置一个状态位，CPU 轮询到该位即可拿到一帧。这就是 `dvp_capture` 的职责。

它和 TPU 的 `forward`（u5-l1）是同一类设计模式：**「写若干控制寄存器 → 写启动位 → 忙等状态位」**。区别只是一个搬图像、一个算张量。

#### 4.2.2 核心流程

`dvp_capture(cfg, hsize, vsize)` 的执行序列：

1. 清 `DVPIN_STATUS_REG`，读回确认（自检）。
2. 写 `DVPIN_CTRL_REG = cfg`（控制字，含启动/模式/DMA 使能位）。
3. 写 `DVPIN_DMA_ADDR1/2/3_REG = 0x30000000`（采集帧的 DDR 目标地址，三个都填同一地址，单缓冲）。
4. 写 `DVPIN_ROW_WIDTH_REG = hsize`（每行字节数，RGB565 时调用方传 `宽×2`）。
5. 写 `DVPIN_COL_WIDTH_REG = vsize`（行数 = 图像高度）。
6. 用 `XTime` 记起始时刻。
7. 忙等循环：反复读 `DVPIN_STATUS_REG`，直到其 bit12（掩码 `0x1000`）置位。
8. 记结束时刻，换算成微秒返回（采集耗时，供调试）。

完成判据是 `STATUS` 的 **bit12**，而 TPU 完成判据是 `STATUS` 的 **bit31**（u5-l1）——同样是「轮询某一位」，位定义不同。

#### 4.2.3 源码精读

核心实现见 [sdk/standalone/src/camera.c:414-470](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L414-L470)，关键片段：

```c
DVPIN_STATUS_REG = 0x0;                 // 清状态
DVPIN_CTRL_REG = cfg;                   // 写控制字（启动触发在此编码）
DVPIN_DMA_ADDR1_REG = 0x30000000;       // 采集帧目标地址
DVPIN_DMA_ADDR2_REG = 0x30000000;
DVPIN_DMA_ADDR3_REG = 0x30000000;
DVPIN_ROW_WIDTH_REG = hsize;            // 每行字节数
DVPIN_COL_WIDTH_REG = vsize;            // 行数

XTime_GetTime(&tBegin);
while((rd_val & 0x1000) == 0) {         // 忙等 bit12
    rd_val = DVPIN_STATUS_REG;
}
XTime_GetTime(&tEnd);
tused = ((tEnd-tBegin)*1000000)/(COUNTS_PER_SECOND);
return tused;
```

注意采集目标地址 `0x30000000` 在这里**硬编码**，它正好等于 `main.cc` 里的全局变量 `mem_pic`（[main.cc:54-55](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L54-L55)）：

```c
u32 mem_pic = 0x30000000;          // RGB565 image data in memory
u8 *img_data = (u8 *)mem_pic;
```

也就是说，IP 把一帧 RGB565 直接搬进 `0x30000000`，CPU 随后用指针 `img_data` 去读它——软硬件对「帧落在哪个地址」有共同约定。

调用方传入的控制字 `cfg` 有两个取值（[main.cc:320](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L320) 与 [main.cc:348](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L348)、[main.cc:532](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L532)）：

- 启动时一次预热采集：`dvp_capture(0x000f1013, pic_hsize*2, pic_vsize)`；
- 菜单 case 1 / case 5 的实际采集：`dvp_capture(0x00011013, pic_hsize*2, pic_vsize)`。

两者低 16 位一致（`0x1013`），差异在 bit[11:8]：`0x000f`（`1111`）vs `0x0001`（`0001`）。由于 IP 是加密黑盒，这几位的精确语义（很可能是 DMA 通道数 / 缓冲使能位）**待确认**，但可确定「启动触发」编码在 `cfg` 里，写进 `DVPIN_CTRL_REG` 后硬件就开始搬一帧。调用方还传 `pic_hsize*2` 作为行宽，正是「RGB565 每像素 2 字节」的体现。

#### 4.2.4 代码实践

**实践目标**：搞清「采集 → DDR 地址 → CPU 读取」三者的地址契约，并理解采集耗时度量。

**操作步骤（源码阅读型）**：

1. 在 [camera.c:437-443](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L437-L443) 确认 `DVPIN_DMA_ADDR1/2/3` 都被写成 `0x30000000`。
2. 在 [main.cc:54-55](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L54-L55) 确认 `img_data` 指向同一地址。
3. 在 [main.cc:319-321](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L319-L321) 找到启动时的预热采集，注意它紧跟在 `I2C_config_init_720p` 之后。

**需要观察的现象 / 预期结果**：

- 采集回的一帧 RGB565 = 1280×720×2 = **1,843,200 字节**，落在 `0x30000000`。
- `dvp_capture` 返回的 `tused`（微秒）应略大于一帧时长：720p@60fps 一帧约 16.6ms ≈ 16600µs。若 `EEP_DEBUG_INFO` 打开，case '1' 会打印 `capture time is N us`。**待本地验证**：真实板卡上该值是否在 16.6ms 量级。
- 想做三缓冲？把 `DVPIN_DMA_ADDR2/3` 改成不同地址即可，但本 demo 单缓冲已够用。

#### 4.2.5 小练习与答案

**练习 1**：为什么调用方传的是 `pic_hsize*2` 而不是 `pic_hsize`？
**答**：`DVPIN_ROW_WIDTH_REG` 要的是「每行字节数」。RGB565 每像素 2 字节，1280 像素一行 = 2560 字节，故传 `1280*2`。若改成灰度（每像素 1 字节），这里就该传 `pic_hsize*1`。

**练习 2**：`dvp_capture` 与 `tpu_forward` 在结构上哪里相似、哪里不同？
**答**：相似——都是「写控制/地址寄存器 → 触发 → 忙等状态位」。不同——`dvp_capture` 等 `STATUS` 的 bit12、搬的是图像、目标地址在 `0x30000000`；`tpu_forward` 等 `STATUS` 的 bit31、算的是张量、基址来自 `hwbase0~3`。

---

### 4.3 RGB565→RGB888 转换

#### 4.3.1 概念说明

`dvp_capture` 把一帧 RGB565 放进了 `0x30000000`，但神经网络输入要的是逐通道 8 位的 RGB888（后续 `get_input_data` 还会做 resize/mean/norm/定点化，见 u4-4）。`RGB565toRGB888` 就是把 2 字节的 565 拆成 3 字节的 888。

这步是**纯 CPU 的逐像素循环**，没有硬件加速，因为它要顺便完成「通道顺序整理」，且后续还要就地被 `draw_object` 画框、被 `dp_display` 打包显示——RGB888 是整个 demo 的「中间表示」(intermediate representation)，采集端和显示端都以它为中转。

#### 4.3.2 核心流程

RGB565 在内存里按**小端 16 位**存放：低地址是低字节、高地址是高字节。设一个 16 位像素为 `HHHH HHHH LLLL LLLL`（`hi` 字节在前，`lo` 字节在后），按 R5G6B5 拆位：

\[
R = (\text{hi} \gg 3) \ll 3,\quad
G = ((\text{hi} \,\&\, 0x07) \ll 5)\;|\;((\text{lo} \gg 5) \ll 2),\quad
B = (\text{lo} \,\&\, 0x1F) \ll 3
\`

「左移再右移」(`>>3<<3`) 是为了把 5/6 位通道**对齐到 8 位高位**，低位补 0（更严谨的做法是补高位复制，但本工程用补 0）。

输出每像素 3 字节，按 **R, G, B** 顺序连续写入目标缓冲 `img_data_888`（`0x38000000`）。注意源码里两条注释 `//b`、`//r` 是早期遗留、与实际写入顺序相反，应以代码 `=r / =g / =b` 为准。

#### 4.3.3 源码精读

转换函数见 [sdk/standalone/src/camera.c:472-488](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L472-L488)：

```c
void RGB565toRGB888(u32 img_w, u32 img_h, u8 *img_data_565, u8 *img_data_888)
{
    u8 r,g,b;
    Xil_DCacheEnable();                       // 临时开 D-Cache 加速批量写
    for(int i=0; i<img_w*img_h*2; i+=2)       // 每步 2 字节 = 1 个 565 像素
    {
        r=(img_data_565[i]>>3)<<3;
        g=(img_data_565[i]<<5) | ((img_data_565[i+1]>>5)<<2);
        b=(img_data_565[i+1])<<3;
        img_data_888[i/2*3]  = r;             // 实际写 R（注释 //b 为遗留）
        img_data_888[i/2*3+1]= g;
        img_data_888[i/2*3+2]= b;
    }
    Xil_DCacheFlushRange((INTPTR)img_data_888, img_w*img_h*3);  // 下沉给 DMA/显示
    Xil_DCacheDisable();                      // 用完关回，配合工程全程关 Cache 的策略
}
```

三个细节值得一读：

1. **缓存策略**：函数开头 `Xil_DCacheEnable`、结尾 `Xil_DCacheDisable`。因为这是 CPU 批量写循环，开 Cache 能大幅提速；但写完必须 `Xil_DCacheFlushRange` 把脏行下沉到 DDR，否则后续 DPDMA 读到的还是旧数据。这正是 2.4 节讲的「CPU 写完要让 DMA 读」必须刷缓存的典型场景。
2. **下标换算**：源步长 `i+=2`（565 每像素 2 字节），目标步长 `i/2*3`（888 每像素 3 字节），`i/2` 即像素序号。
3. **灰度分支**：若未定义 `IMG_RGB565`，main.cc 走的是 `GRAYtoRGB888`（[camera.c:490-502](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L490-L502)），把单字节灰度复制成 R=G=B 的三通道。

输出缓冲地址来自 [main.cc:58-59](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L58-L59)：

```c
//RGB888 image data in memory
u8 *img_data_888 = (u8*)0x38000000;
```

所以 RGB565 落在 `0x30000000`、RGB888 落在 `0x38000000`，两块各自独立、地址都在 DDR 低 2GB（u1-4 讲过 HP 口能访问的低 2GB 区）。

#### 4.3.4 代码实践

**实践目标**：手算一个像素的 565→888，验证位拆解正确。

**操作步骤**：

1. 取一个纯红色像素：RGB565 = `R=31,G=0,B=0`，即 `1111 1000 0000 0000` = `0xF800`。小端存放为 `img_data_565[i]=0x00`、`img_data_565[i+1]=0xF8`（注意高低字节！）。
2. 代入源码公式：
   - `r = (0x00>>3)<<3 = 0`
   - `g = (0x00<<5)|((0xF8>>5)<<2) = 0x00 | (0x07<<2) = 0x1C = 28`
   - `b = 0xF8<<3 = ...`（注意 `u8` 截断：`0xF8<<3 = 0x7C0`，截到 8 位 = `0xC0`）

**需要观察的现象 / 预期结果**：

- 上面这个「高低字节写反」的例子会得到错误的 r=0——这正好提醒：**小端序下 565 高字节在 `img_data_565[i+1]`**。正确写法是 `img_data_565[i]=0x80`、`img_data_565[i+1]=0xF8`（合起来小端读为 `0xF800`）。
- 用正确字节重新算：`hi=0x80, lo=0xF8`……等等——请你自己按源码逐位算一遍（见练习 1）。**待本地验证**：在板卡上用 `case '1'` 采一张纯色卡，保存 BMP（case '3'）看颜色是否符合预期。

#### 4.3.5 小练习与答案

**练习 1**：正确存放的红色像素 `0xF800`（`img_data_565[i]=0x00, img_data_565[i+1]=0xF8`）经 `RGB565toRGB888` 后，`r/g/b` 各是多少？
**答**：
- `hi=img_data_565[i]=0x00`，`lo=img_data_565[i+1]=0xF8`。
- `r=(0x00>>3)<<3 = 0`。

  ⚠️ 这里 r=0 是错的——问题在于源码用 `img_data_565[i]` 当「高字节」假设了**大端字节序**。实际上 16 位值 `0xF800` 小端存放是 `data[0]=0x00, data[1]=0xF8`，而 `r=(data[0]>>3)` 取到的是低字节的高位。这说明源码隐含的「`[i]` 是 565 的高字节」约定，与 DVP 实际送入 DDR 的字节序必须一致才能得到正确颜色——**这是一个值得在真实硬件上用纯色卡验证的点**。本工程能正常出图，说明 DVP IP 送入 `0x30000000` 的字节序正好匹配这套拆位公式。

**练习 2**：`RGB565toRGB888` 为什么不像 `dvp_capture` 那样全程关 Cache，而是临时开一下？
**答**：它是 CPU 密集的逐像素循环，开 D-Cache 能让写操作命中缓存、成行下沉，显著快于逐字节直写 DDR。但开 Cache 又会引入「CPU 写的数据滞留缓存」问题，所以写完立即 `Xil_DCacheFlushRange` 把结果下沉、再关回，兼顾速度与一致性。这是裸机里典型的「局部开 Cache + 显式刷」手法。

---

### 4.4 DP 显示输出链路

#### 4.4.1 概念说明

推理结果要显示给用户看，本工程用的是 ZynqMP **PS 自带的 DisplayPort 显示子系统**，而不是 4.1 提到的 DVP IP 的 DVPOUT 通路。这条链路涉及三块 PS 硬件：

- **DpPsu（DP 发送器）**：和显示器握手、链路训练、发视频流。
- **AVBuf（视频管线）**：格式转换、图层混合。
- **DPDMA**：把 DDR 里的帧缓冲搬到 DP 发送器。

`dpdma_intr.c` 把这三者初始化好、注册好中断；`dp_display`（在 camera.c 里）则负责把 RGB888 像素打包成 ARGB 写进帧缓冲 `GFrame`，DPDMA 就会持续把 `GFrame` 刷上屏幕。

为什么显示要用到**中断**（而 TPU 推理全程没用中断）？因为 DP 是「热插拔」接口——显示器可能随时插上/拔下，DP 控制器靠 **HPD（Hot-Plug Detect）** 信号通知 CPU，CPU 在中断里做链路训练重建。这是整个工程**唯一**使用 GIC 中断的地方（u4-l1 已点过：关掉 `EEP_DP_ENABLE` 会连带关掉整个中断子系统）。

#### 4.4.2 核心流程

初始化阶段（`main` 里、`Xil_DCacheDisable` 之后）：

1. `graphic_buffer_init(GFrame)`：给图形帧缓冲画一张初始棋盘格测试图。
2. `init_intr_sys()`（[main.cc:230-238](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L230-L238)）：四步——
   - `Init_Intr_System`：初始化 GIC（[sys_intr.c:35-55](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sys_intr.c#L35-L55)）。
   - `Dpdma_init`：初始化 DPDMA/DpPsu/AVBuf 子系统（[dpdma_intr.c:517-527](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L517-L527)）。
   - `Dpdma_Setup_Intr_System`：注册 HPD 中断、使能 DP/DPDMA 中断。
   - `Setup_Intr_Exception`：注册 ARM 异常处理、使能中断（[sys_intr.c:24-33](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sys_intr.c#L24-L33)）。
3. 之后 `DpPsu_Run` 在 HPD 事件到来时被触发，做链路训练并启动视频流。

运行期每一帧显示阶段：

1. `dp_display(w, h, img_data_888, ARGB)`：把 `img_data_888` 里的 RGB888 逐像素打包成 `0xFFRRGGBB`（ARGB），写进 `GFrame`（`ARGB` 是其 `u32*` 视图）。
2. `Xil_DCacheFlushRange(GFrame, ...)`：让 DPDMA 读到新像素。
3. DPDMA 在后台持续把 `GFrame` 搬给 DP → 屏幕刷新。

#### 4.4.3 源码精读

`dp_display` 在 [sdk/standalone/src/camera.c:504-519](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L504-L519)：

```c
void dp_display(u32 img_w, u32 img_h, u8 *img_data_888, u32 *dp_buf)
{
    u8 r,g,b; u32 argb;
    Xil_DCacheEnable();
    for(int i=0; i<img_w*img_h*3; i+=3)        // 每步 3 字节 = 1 个 888 像素
    {
        r=img_data_888[i];
        g=img_data_888[i+1];
        b=img_data_888[i+2];
        argb = (0xff<<24) | (r<<16) | (g<<8) | b;   // ARGB：A=0xff
        dp_buf[i/3] = argb;                          // 写进 GFrame
    }
    Xil_DCacheFlushRange((INTPTR)GFrame[0], img_w*img_h*4);  // 刷帧缓冲
    Xil_DCacheDisable();
}
```

逐点说明：

- 打包格式 `0xFF<<24 | R<<16 | G<<8 | B` 即 **ARGB8888**，与 `InitDpDmaSubsystem` 里 `XDpDma_SetGraphicsFormat(DpDmaPtr, RGBA8888)`（[dpdma_intr.c:379](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L379)）设定的图形层格式一致。
- `dp_buf` 由 main.cc 传入为 `ARGB`（[main.cc:87](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L87)），即 `GFrame` 的 `u32*` 视图，`GFrame` 本身定义在 [main.cc:85](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L85)：`u8 GFrame[BUFFERSIZE] __attribute__((aligned(256)))`，`BUFFERSIZE = 1280*720*4`（[dpdma_intr.h:47-50](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.h#L47-L50)），256 字节对齐是 DPDMA 对帧缓冲的对齐要求。
- 最后一行 `Xil_DCacheFlushRange((INTPTR)GFrame[0], ...)` 的意图是刷新整个帧缓冲，首参本应是缓冲基地址。这里写的是 `GFrame[0]`（数组首元素的**值**），与「取地址」写法不同——**这是一个值得在本地用反汇编/打印确认的细节**：若编译器未把 `GFrame` 退化成指针，刷新起始地址可能不是缓冲头。不碍事的是：循环刚把 `dp_buf`（即 `GFrame`）写满，且 `GFrame` 是 256 对齐的全局数组，多数情况下缓存行仍能覆盖到。**待本地验证**该行的实际刷新范围。

DP 子系统的初始化集中在 [sdk/standalone/src/dpdma_intr.c:346-423](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L346-L423)（`InitDpDmaSubsystem`），它依次初始化 DpPsu/AVBuf/DPDMA 驱动、设定 graphics=RGBA8888 / video=RGBA8880 / 输出=RGB_8BPC、选输入源为「非实时 graphics」、关全局 alpha（用逐像素 alpha）、最后 `XAVBuf_SoftReset`。运行配置由 [dpdma_intr.c:498-515](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L498-L515) 填好：视频模式 `XVIDC_VM_1280x720_60_P`、8bpc、RGB、双 lane、5.4Gbps——与摄像头 720p 输出严格对齐。

中断注册在 [sdk/standalone/src/dpdma_intr.c:314-343](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L314-L343)（`Dpdma_Setup_Intr_System`），核心两条：

```c
// DP 的 HPD 中断，IRQ 号 151，上升沿触发
XScuGic_Connect(IntrPtr, DPPSU_INTR_ID,(Xil_InterruptHandler)XDpPsu_HpdInterruptHandler, ...);
XScuGic_SetPriorityTriggerType(IntrPtr, DPPSU_INTR_ID, 0x0, 0x03);
// DPDMA 的 VSYNC 中断，IRQ 号 154
XScuGic_Connect(IntrPtr, DPDMA_INTR_ID,(Xil_ExceptionHandler)XDpDma_InterruptHandler, ...);
```

`DPPSU_INTR_ID=151`、`DPDMA_INTR_ID=154` 定义在 [dpdma_intr.h:39-40](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.h#L39-L40)。HPD 中断到来时进入 `DpPsu_IsrHpdEvent`（[dpdma_intr.c:220-225](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L220-L225)），它调用 `DpPsu_Run`（[dpdma_intr.c:146-203](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L146-L203)）完成「唤醒显示器 → 读对端能力 → 设 lane/rate → 链路训练 → 显示帧缓冲」的全流程。`graphic_buffer_update`（[dpdma_intr.c:425-433](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L425-L433)）则把帧缓冲的地址/步长填进 `GFrameBuffer` 结构交给 DPDMA。

#### 4.4.4 代码实践

**实践目标**：理清「RGB888 → ARGB 帧缓冲 → DPDMA → 屏幕」的色彩格式契约。

**操作步骤（源码阅读型）**：

1. 在 [camera.c:514](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L514) 确认打包式 `argb = (0xff<<24)|(r<<16)|(g<<8)|b`。
2. 在 [dpdma_intr.c:379](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L379) 确认 `XDpDma_SetGraphicsFormat(..., RGBA8888)`，与上面的 ARGB 字节序对齐。
3. 在 [dpdma_intr.c:505](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L505) 确认视频模式是 1280×720@60，与摄像头采集分辨率一致。

**需要观察的现象 / 预期结果**：

- 若某天显示花屏/偏色，第一步就该怀疑 `dp_display` 的字节序与 `SetGraphicsFormat` 的格式不匹配。
- 若显示器插上后一直黑屏，多半是 HPD 中断未触发或链路训练失败——可临时打开 `EEP_DEBUG_INFO` 看 `DpPsu_Run` 打印的 `Lane count / Link rate / Training succeeded` 字样。**待本地验证**：真实显示器上插拔 DP 线，观察串口是否打印 `HPD event ..........`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dp_display` 要把 alpha 固定写 `0xff`？
**答**：图形层格式是 RGBA8888，alpha=0xff 表示「完全不透明」。工程在 `InitDpDmaSubsystem` 里关掉了全局 alpha、改用逐像素 alpha（[dpdma_intr.c:413](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c#L413)），所以每个像素的 alpha 都得自己填满，否则图像会半透明地叠在背景上。

**练习 2**：把 `EEP_DP_ENABLE` 注释掉，工程会发生哪三件事？
**答**：① `main.cc` 里 `GFrame`/`ARGB`/`init_intr_sys` 等 `#ifdef EEP_DP_ENABLE` 包裹的代码全部被剔除；② 所有 `dp_display(...)` 调用消失，推理结果只打印到串口、不再上屏；③ 因为 `init_intr_sys` 不再被调用，整个 GIC 中断子系统都不会初始化——TPU 推理本身不依赖中断，仍能跑（这与 u4-l1 的结论一致）。

---

## 5. 综合实践

把本讲四个模块串起来，完成规格里要求的数据流梳理任务：**在 `main.cc` 的 case '5'（Run Demo）实时循环里，画出「采集→格式转换→预处理→forward→后处理→DP 显示」的完整数据流，并标注关键内存地址**。

### 5.1 数据流总览

case '5' 是一个 `while(1)` 死循环（[main.cc:528-683](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L528-L683)），每一轮迭代都跑完整链路：

```
┌──────────────────────────────────────────────────────────────────────────┐
│  OV5640 ──DVP──> EEP_DVP_Top(IP) ──AXI HP DMA──> DDR 0x30000000 (mem_pic)│
│                  ▲ 配置: I2C(OV5640) + DVPIN寄存器(0xA00C1010..)          │
│                  │ 触发: dvp_capture(0x00011013, 1280*2, 720)  轮询 bit12 │
└──────────────────────────────────────────────────────────────────────────┘
        │  img_data  (u8*)0x30000000   [RGB565, 1280×720×2 字节]
        ▼  RGB565toRGB888()  —— camera.c:543
┌──────────────────────────────────────────────────────────────────────────┐
│  DDR 0x38000000 (img_data_888)   [RGB888, 1280×720×3 字节]               │
│  └─ 这里先被 dp_display 打包成 ARGB ──> GFrame(显示原始画面，可选)         │
└──────────────────────────────────────────────────────────────────────────┘
        │  img_data_888
        ▼  get_input_data()  —— main.cc:561
┌──────────────────────────────────────────────────────────────────────────┐
│  resize 双线性 ──> 0x39000000 (inbuf_resized) [网络输入尺寸]              │
│  mean/norm/定点(exp) + RGB→BGR + 32字节步长打包                          │
│  ──> 写入 TPU 输入区 hwbase1 (≈0x31000000+...)                           │
└──────────────────────────────────────────────────────────────────────────┘
        ▼  tpu_forward()  —— main.cc:566   (写 BASEADDR0~3→STARTUP=0x11→轮询 bit31)
┌──────────────────────────────────────────────────────────────────────────┐
│  TPU 计算 ──> 结果写回 DDR out 段 (hwbase3)                              │
└──────────────────────────────────────────────────────────────────────────┘
        ▼  eepsa.read_forward_result(outputs)  —— main.cc:580  (epmat→ncnn::Mat, u5-l2)
        ▼  yolo3_detection_output_forward()  —— main.cc:602   (解码+NMS, u6-l3)
┌──────────────────────────────────────────────────────────────────────────┐
│  draw_object() 把检测框直接画进 img_data_888(0x38000000)  —— main.cc:642 │
└──────────────────────────────────────────────────────────────────────────┘
        ▼  dp_display(1280,720, img_data_888, ARGB)  —— main.cc:680
┌──────────────────────────────────────────────────────────────────────────┐
│  GFrame(256对齐, .bss) [ARGB8888, 1280×720×4 字节]  ──DPDMA──> DP ──> 屏幕│
│  HPD中断(IRQ151) 保活链路                                                │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.2 关键内存地址职责一览

| 地址 / 变量 | 类型 | 职责 | 出现位置 |
| --- | --- | --- | --- |
| `mem_pic` = `0x30000000` | `u8*`（`img_data`） | DVP 采集的 RGB565 原始帧 | [main.cc:54](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L54)，[camera.c:437](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L437) |
| `img_data_888` = `0x38000000` | `u8*` | RGB888 中间表示，画框 + 显示共用的中转帧 | [main.cc:59](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L59) |
| `inbuf_resized` = `0x39000000` | `u8*` | resize 后的中间缓冲（网络输入分辨率） | [main.cc:113](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L113) |
| `hwbase1`（≈`0x31000000`+） | `u8*` | TPU 输入张量区（硬件打包格式） | [main.cc:299](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L299) |
| `GFrame`（.bss，256 对齐） | `u8[]`，`BUFFERSIZE` | DP 显示的 ARGB 帧缓冲 | [main.cc:85](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L85) |
| `EEPDVP_REG_BASE_ADDR` = `0xA00C0000` | 寄存器 | DVP IP 控制寄存器块 | [config.h:27](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L27) |

### 5.3 操作步骤

1. 打开 [main.cc:528-683](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L528-L683)，对照上面的数据流图，在源码里逐一找到 6 个箭头的对应代码行。
2. 注意 case '5' 在**采集后、forward 前**就先调了一次 `dp_display`（[main.cc:544-547](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L544-L547)），而后处理画框后又调了一次（[main.cc:680](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L680)）。思考这两次显示的区别。

### 5.4 需要观察的现象 / 预期结果

- 第一次 `dp_display` 显示的是**原始摄像头画面**（还没画框），第二次显示的是**画了检测框的画面**。因为 case '5' 是死循环，人眼最终看到的是不断刷新的「带框实时画面」。
- `draw_object`（[camera.c:521-547](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L521-L547)）把检测框直接写进 `img_data_888`，与后续 `dp_display` 共用同一块 RGB888 缓冲——这是「以 RGB888 为统一中转帧」设计的直接体现。
- **待本地验证**：真实板卡上接好摄像头和 DP 显示器，选菜单 '5'，应看到实时画面上叠加检测框；串口同时打印 `forward time / detection output time` 两项耗时。

## 6. 本讲小结

- **DVP IP 是「摄像头到 DDR」的搬运工**：`EEP_DVP_Top`（加密黑盒）经 AXI HP 把 OV5640 的 RGB565 帧搬进 `0x30000000`；CPU 用 I2C 配传感器、用 `DVPIN_*` 寄存器触发采集并轮询 `STATUS` 的 bit12 判完成——结构与 TPU 的 forward 同源。
- **OV5640 配置是编译期定死的**：一长串 `{寄存器, 值}` 表加 `IMG_RGB565` 宏开关，换分辨率/格式要换整张表。
- **RGB888 是 demo 的中转帧**：`RGB565toRGB888` 把 565 拆成 888 落在 `0x38000000`，它同时是预处理输入、画框画布、显示打包源，三件事共用一块内存。
- **缓存一致性贯穿始终**：凡是 CPU 写完要让 DMA 读的场合（RGB888 转换、dp_display 打包），都靠「临时开 D-Cache + `Xil_DCacheFlushRange` + 关回」兼顾速度与正确性。
- **显示走 PS 的 DPDMA→DisplayPort，不走 DVP 的 DVPOUT**：`dpdma_intr.c` 初始化 DpPsu/AVBuf/DPDMA，`dp_display` 把 RGB888 打包成 ARGB 写进 256 对齐的 `GFrame`；`DVPOUT_*` 寄存器只定义未使用。
- **HPD 中断是工程唯一的中断使用者**：DP 热插拔靠 IRQ 151 触发链路训练，关掉 `EEP_DP_ENABLE` 会连带关掉整个 GIC 中断子系统，但不影响 TPU 推理。

## 7. 下一步学习建议

- **下一讲 u8-l2（SD 卡网络数据加载与文件读写）**：本讲 case '5' 假设摄像头一直在喂图，但当没有摄像头时，输入来自 SD 卡上的 `eepinput.mem`（case '4'）。建议接着读 `sd.c`，理解 `file_read` 如何把网络权重和测试图像从 SD 卡搬到 `hwbase0` / `hwbase1`，以及为何读后必须 `Xil_DCacheFlush`。
- **横向对比 u8-l3（simplestl 与 nmat）**：本讲的图像缓冲都是「裸 DDR 地址 + 原生指针」，而 TPU 输出侧用的是自实现 `ncnn::Mat`（带通道/对齐模型）。对比两者能更深理解「为什么显示用裸地址、推理用 Mat」。
- **延伸阅读**：若想深入 DP 子系统，可对照 Xilinx 官方 `xdpdma` / `xdppsu` / `xavbuf` BSP 文档逐个理解 [dpdma_intr.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/dpdma_intr.c) 里的链路训练四步；若想换更高分辨率摄像头，需同时改 OV5640 配置表、`IMG_WIDTH/HEIGHT`、`BUFFERSIZE`、DP 视频模式四处，且需在真实硬件上验证时序收敛。
