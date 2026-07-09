# standalone 工程结构与 ZynqMP 平台初始化

## 1. 本讲目标

本讲从 Linux 路线（高层 `EEPTPU` API）切换到**裸机（standalone）路线**，目标是让读者：

- 看懂 `sdk/standalone/` 工程的文件组织方式，知道每个目录/文件负责什么。
- 理解裸机程序不经过 Linux、不链接 `libeeptpu_pub`，而是直接读写 TPU 寄存器驱动硬件。
- 掌握 `main.cc` 中「上电初始化 → 串口菜单循环」的整体骨架，以及菜单 1~5 各选项的含义。
- 认识 ZynqMP 平台初始化（`init_platform`、GIC 中断、DP 显示）的调用顺序。
- 学会用 `config.h` 中的编译开关（`NET_TYPE`、`SD_CARD_IS_READY`、`EEP_DP_ENABLE` 等）裁剪功能。

本讲是 U4 单元（裸机路径）的入口，后续 u4-l2 寄存器协议、u4-l3 接口抽象、u4-l4 输入预处理都建立在本讲的工程地图之上。

## 2. 前置知识

在进入本讲前，读者应已具备以下认知（来自前置讲义，此处只做一句话回顾，不再展开）：

- **ZynqMP 是 PS+PL 的 SoC**：ARM Cortex-A53（PS）与 FPGA（PL）封在同一颗芯片，TPU IP 放在 PL，ARM 经地址映射驱动它（u1-l2、u1-l3）。
- **两条部署路线**：Linux 路线用高层 `EEPTPU` 类（`init/load_bin/set_input/forward`），裸机路线用 `EEPTPU_SA` 直接读写寄存器；两条路线的两个魔法地址必须一致（u2-l1）。
- **裸机的网络数据从哪来**：编译器产出 `*.pub.bin` 后，`eepBinCvt` 把它转成 `eepnet.h`（元数据，编译期 `#include`）和 `eepnet.mem`/`eepinput.mem`（权重/样例输入，运行期从 SD 卡加载）（u3-l2）。
- **`eepnet_config[]` 数组**：`eepnet.h` 里那段自描述元数据，`eeptpu_init` 用「指针游走法」解析出输入输出 shape、DDR 偏移、mean/norm（u3-l3）。

本讲新增的几个**裸机专属**基础概念：

| 术语 | 含义 |
| --- | --- |
| **BSP** | Board Support Package，板级支持包。Xilinx 为 ZynqMP 提供的底层驱动库（如 `xil_cache`、`xscugic`、`xdpdma`），裸机程序依赖它，作用类似 Linux 的内核驱动。 |
| **GIC** | Generic Interrupt Controller，ARM 通用中断控制器。ZynqMP 里所有外设中断（这里主要是 DP 显示）都先汇聚到 GIC，再分发给 CPU。 |
| **FSBL** | First Stage Boot Loader，一级启动加载器。上电后 Boot ROM 加载 FSBL，FSBL 配好 PS、灌好 bitstream 后跳转到我们的裸机 ELF（u1-l3）。本讲的 `main()` 就是被 FSBL 跳转进去的入口。 |
| **DMA 与缓存一致性** | TPU 和 DVP 摄像头都是 DMA 主设备，直接读写 DDR；若 ARM 的 D-cache 开着，CPU 写的数据可能还停在缓存里，DMA 会读到 DDR 里的旧值。裸机没有操作系统做缓存同步，必须手动 `Xil_DCacheFlush/Disable`。 |

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `sdk/standalone/src/` 下）：

| 文件 | 作用 |
| --- | --- |
| [main.cc](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc) | **程序入口**。包含上电初始化序列、全局变量、`get_input_data`/`tpu_forward` 辅助函数，以及串口菜单主循环。 |
| [config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h) | **编译开关集中地**。TPU 寄存器地址、网络类型、SD/DP/摄像头开关都在这里用 `#define` 控制。 |
| [platform.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/platform.c) / [platform.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/platform.h) | Xilinx 标准「平台初始化」模板：`init_platform()`/`cleanup_platform()`，负责缓存与串口。 |
| [sys_intr.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sys_intr.c) | GIC 中断控制器初始化：`Init_Intr_System()` 配置 GIC，`Setup_Intr_Exception()` 打开 ARM 全局中断。 |

此外，`dpdma_intr.c/.h`（DP 显示）、`camera.c/.h`（摄像头）、`sd.c/.h`（SD 卡）会在本讲的「调用链」中被提及，但其内部实现分别属于 u8 单元的讲义，本讲只关心它们在初始化序列里的位置。

## 4. 核心概念与源码讲解

### 4.1 工程结构与构建

#### 4.1.1 概念说明

裸机工程和 Linux demo 最大的区别是：**它没有操作系统、没有文件系统、没有动态加载器**。这意味着：

- 不能在运行时 `fopen("xxx.pub.bin")` 加载网络——网络要么在编译期烧进 ELF（`eepnet.h`），要么在运行期从 SD 卡裸读到固定物理地址（`eepnet.mem`）。
- 不能 `#include <vector>` 用标准库——标准 C++ 库依赖操作系统，所以工程自带精简版 `simplestl`（u8-l3 会讲）。
- 不能依赖内核做中断和缓存管理——这些都要程序自己用 BSP 接口显式处理。

因此整个 `sdk/standalone/` 目录被组织成「**编译/转换工具链**」+「**裸机源码 src**」两块。

#### 4.1.2 目录组织

`sdk/standalone/` 下分两个子目录：

```
sdk/standalone/
├── net_model/              # 模型编译与转换工具链（在 x86 主机上跑）
│   ├── compiler/eeptpu_compiler        # 编译器二进制
│   ├── eepBinCvt/eepBinCvt             # bin→mem/header 转换器
│   ├── scripts/                        # setting.ini / b_yolo4tiny.sh / eepbin_cvt.sh
│   └── models/yolov4tiny/              # cfg + weights + 测试图
└── src/                    # 裸机 C++ 源码（交叉编译到 ARM，上板运行）
    ├── main.cc             # 入口
    ├── config.h            # 编译开关
    ├── platform.c/.h       # Xilinx 平台模板
    ├── sys_intr.c/.h       # GIC 中断
    ├── dpdma_intr.c/.h     # DP 显示输出
    ├── camera.c/.h         # DVP 摄像头采集
    ├── sd.c/.h             # SD 卡读写
    ├── resize.cpp/.h       # 双线性缩放
    ├── simplestl.cpp/.h    # 精简 STL
    ├── utils.cpp/.h、public.h
    ├── eeptpu/             # EEPTPU_SA 类 + 接口抽象 + nmat
    ├── layers/             # 软件后处理算子（yolo3_detection_output）
    ├── post_process/       # 分类 topk 等
    └── net_data/           # eepnet.h / eepnet.mem / eepinput.mem
```

`net_model/` 这一块对应 u3-l1/u3-l2 已经讲过的编译链路，它的产物（`eepnet.h`、`eepnet.mem`）就放在 `src/net_data/` 下被 `src/` 消费。本讲关注的是 `src/` 这块。

#### 4.1.3 构建方式：Vitis 工程

仓库里**没有 Makefile 或 CMakeLists**——裸机工程的标准构建方式是用 Xilinx **Vitis** IDE：

1. 用 u1-l3 讲过的 `system_wrapper.xsa`（硬件导出包，含 bitstream 和地址映射）在 Vitis 里创建一个 Platform Project，它会自动生成对应这块硬件的 BSP（`xparameters.h` 等就是这时产生的）。
2. 在该平台上创建 Application Project，把 `src/` 下所有 `.c/.cc/.cpp/.h` 加进去。
3. Vitis 用 ARM 交叉工具链（aarch64）编译，产出裸机 ELF。
4. 把 ELF 和 bitstream 打包进 BOOT.BIN，由 FSBL 在上电后加载并跳转执行（u1-l3）。

> 由于 Vitis 工程文件（`.project`、`.cproject`）通常不入库，仓库只保留了源码与数据，读者需自行在 Vitis 里重建工程。具体步骤标注为「待本地验证（需 Vitis + xsa）」。

#### 4.1.4 源码精读：入口与全局对象

`main.cc` 顶部先把所有需要的头文件包含进来，重点是 `eeptpu_sa.h`（裸机 TPU 对象）、`eepnet.h`（网络元数据）、平台与中断头文件：

[main.cc:20-44](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L20-L44) —— 包含标准头、`eeptpu_sa.h`、`config.h`、`eepnet.h`、`platform.h`、`sys_intr.h`、`dpdma_intr.h`。注意第 32 行 `#include "net_data/eepnet.h"`：网络元数据在**编译期**就被链进 ELF，这是裸机路线的关键特征。

紧接着是全局变量，其中最关键的是裸机 TPU 对象 `eepsa` 和几个记录硬件地址的变量：

[main.cc:90-98](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L90-L98) —— 声明全局 `EEPTPU_SA eepsa;`（第 91 行），以及 `waddr`（网络权重地址）、`sd_input_addr`（输入张量地址）、`tpu_hwbase2/3`、`tpu_algbase` 等。这些地址在 `eeptpu_init` 之后才被赋值，`tpu_forward` 会把它们写进 TPU 的基地址寄存器。

#### 4.1.5 代码实践：画出工程依赖图

1. **实践目标**：建立「工具链产物 → 裸机源码 → 可执行 ELF」的依赖关系直觉。
2. **操作步骤**：
   - 在 `sdk/standalone/src/` 中确认 `net_data/eepnet.h`、`net_data/eepnet.mem`、`net_data/eepinput.mem` 三个文件都存在。
   - 用 `Grep` 在 `main.cc` 里搜索 `eepnet` 与 `eepinput`，确认它们分别被「编译期 include」和「运行期 file_read」两种方式消费。
3. **需要观察的现象**：`eepnet.h` 出现在文件顶部的 `#include` 区；`eepinput.mem`/`eepnet.mem` 出现在 `file_read(...)` 调用里。
4. **预期结果**：你能指出「元数据走 header（小、需代码可见）、权重走 mem（大、可替换）」这一 u3-l2 的结论在源码里的落点。
5. 待本地验证（不需硬件，纯阅读）。

#### 4.1.6 小练习与答案

- **练习 1**：为什么裸机工程不能像 Linux demo 那样直接 `load_bin("xxx.pub.bin")`？
  - **答**：裸机没有文件系统和 `libeeptpu_pub` 运行时解析器，无法在运行期打开并解析 bin。所以把解析工作前置到主机：元数据用 `eepBinCvt --output header` 转成 `eepnet.h` 在编译期 include，权重用 `--output mem` 转成 `eepnet.mem` 在运行期从 SD 卡裸读到固定地址。
- **练习 2**：`src/` 下哪个文件是「Xilinx 标准、几乎可以原样复用到任何 ZynqMP 裸机工程」的？
  - **答**：`platform.c/.h`。它是 Xilinx 官方模板，`init_platform/cleanup_platform` 只做缓存和串口的通用初始化。

---

### 4.2 main.cc 菜单流程

#### 4.2.1 概念说明

裸机程序没有图形界面，和用户交互的唯一通道是**串口（UART）**。`main.cc` 用一个经典的「`while` 循环 + `switch` 菜单」结构：每次循环打印选项列表，用 `inbyte()` 从串口读一个字符，根据字符执行对应功能。这种结构让开发者可以**分步调试**整条推理链路（先单独采一帧、再单独跑一次 forward），而不必一上来就跑实时循环。

#### 4.2.2 核心流程

菜单主循环的骨架（伪代码）：

```
exit_flag = 0
while exit_flag != 1:
    打印菜单 (1=Get 1 Frame, 2=Forward Result, [3=Save Image], 4=Read Test Image, 5=Run Demo, 0=Exit)
    choice = inbyte()               # 从串口读一个字符
    switch(choice):
        '0': exit_flag = 1          # 退出
        '1': 采集一帧 + RGB转换 + 预处理
        '2': forward + 读结果 + 后处理
        '3': (仅 SD 就绪) 把当前帧存成 BMP
        '4': 读测试输入 (SD 的 eepinput.mem 或编译期 eepinput 数组)
        '5': while(1) 实时循环: 采集→预处理→forward→后处理→显示
cleanup_platform()
```

关键观察：选项 1 和 2 是**分离的**——可以先按 1 采集并预处理一帧（数据进 TPU 输入区），再按 2 触发推理。这把「输入准备」和「推理执行」解耦，方便定位问题。选项 5 则是「一条龙」实时循环。

#### 4.2.3 源码精读：菜单打印与选项分发

菜单打印与字符读取：

[main.cc:323-339](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L323-L339) —— 循环条件 `exit_flag != 1`；逐行 `xil_printf` 打印选项；第 329 行的「3: Save Image to SD Card」被 `#ifdef SD_CARD_IS_READY` 包住（SD 关掉时这一行不打印）；第 335 行 `choice = inbyte()` 读串口字符；第 336-338 行若是字母则转大写（兼容大小写输入）。

选项分发：

[main.cc:341-345](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L341-L345) —— `switch(choice)`，`case '0'` 把 `exit_flag` 置 1 退出循环。

各选项功能（对照源码行号）：

| 选项 | 功能 | 源码位置 | 说明 |
| --- | --- | --- | --- |
| `1` | Get 1 Frame | [main.cc:345-378](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L345-L378) | `dvp_capture` 采一帧 → `RGB565toRGB888` → `dp_display`（可选）→ `get_input_data` 做 resize/mean/norm/打包进 TPU 输入区。 |
| `2` | Forward Result | [main.cc:379-498](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L379-L498) | `tpu_forward`（写寄存器+轮询）→ `read_forward_result` → 按 `NET_TYPE` 做分类 topk 或 yolo3 检测后处理 → 画框 → DP 显示。 |
| `3` | Save Image to SD Card | [main.cc:499-513](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L499-L513) | 仅 `SD_CARD_IS_READY` 时存在；`bmp_write` 把 `img_data_888` 存成 `pic_N.bmp`，按分辨率选 BMP 模式。 |
| `4` | Read Test Image | [main.cc:514-527](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L514-L527) | SD 就绪时 `file_read("eepinput.mem", eepinput_addr, INPUTDATA_SIZE)` 从卡读输入；否则 `eepsa.eeptpu_input(eepinput, ...)` 用编译期数组。 |
| `5` | Run Demo | [main.cc:528-683](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L528-L683) | 内嵌 `while(1)` 死循环，把选项 1+2 的流程连起来反复跑：采集→预处理→forward→后处理→显示。 |

> ⚠️ **重要细节**：选项 5 的循环是 `while(1)`（[main.cc:529](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L529)），**没有退出条件**。一旦选了 5，就再也无法回到主菜单（只能复位板卡）。这是阅读源码才能发现的「陷阱」，初学者容易以为还能退出来。

#### 4.2.4 代码实践：列出菜单选项功能

1. **实践目标**：通过阅读 `while` 循环与 `switch`，准确说出每个选项做了什么、依赖哪些宏。
2. **操作步骤**：
   - 打开 [main.cc:323-686](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L323-L686)。
   - 为选项 1~5 各写一句话功能描述，并标注它是否依赖 `EEP_DVP_CAMERA`、`SD_CARD_IS_READY`、`EEP_DP_ENABLE`、`NET_TYPE`。
3. **需要观察的现象**：选项 1/5 里有 `#ifdef EEP_DVP_CAMERA ... #else xil_printf("No camera !!!")`；选项 3 整个被 `#ifdef SD_CARD_IS_READY` 包住。
4. **预期结果**：得到一张「选项 → 依赖宏」对照表（参考本讲 4.4.3 的表格）。
5. 待本地验证（纯阅读）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么选项 1（采集）和选项 2（forward）要拆成两步？
  - **答**：为了分步调试。选项 1 只负责把一帧图像预处理后写进 TPU 的输入地址区（`hwbase1`），选项 2 才触发推理。这样如果推理结果不对，可以先单独验证「采集+预处理」是否正确（比如用选项 3 存盘看图），再验证 forward，便于定位故障环节。
- **练习 2**：选项 4 的两条分支（SD 读 mem vs 用 `eepinput` 数组）分别对应什么部署场景？
  - **答**：SD 就绪时从卡读 `eepinput.mem`——输入可随时替换，适合开发期换图测试；SD 不可用时退回编译期烧进 ELF 的 `eepinput` 数组——输入固定，适合无 SD 卡的精简部署。

---

### 4.3 平台与中断初始化

#### 4.3.1 概念说明

`main()` 在进入菜单循环之前，要先把「运行环境」搭好：缓存、串口、SD 卡、摄像头、DP 显示、中断控制器、TPU 配置。这一串初始化的顺序很重要——比如必须先 `eeptpu_init` 解析出地址、再 `file_read` 把权重搬到那个地址；必须先把 DP 的中断在 GIC 里注册好、再使能 ARM 全局中断。

裸机里没有「驱动自动加载」这回事，每一步都是 `main()` 显式调用的函数调用，所以读者能在源码里看到一条**线性的、可追溯的**启动序列——这正是裸机相对 Linux 的可读性优势。

#### 4.3.2 核心流程：main 启动序列

`main()` 从入口到进入菜单，依次做这些事（伪代码，标注源码行号）：

```
init_platform()                          # L260  平台/缓存/串口（Xilinx 模板）
if SD_CARD_IS_READY:  SD_Init()          # L264-267  初始化 SD 卡
if EEP_DVP_CAMERA:    I2C_config_init_720p(...)   # L271-274  I2C 配摄像头寄存器
Xil_DCacheDisable()                      # L279  关 D-cache（DMA 一致性）
if EEP_DP_ENABLE:
    graphic_buffer_init(GFrame)          # L282  清/初始化 DP 帧缓冲
    init_intr_sys()                      # L283  配 GIC + DP + 使能全局中断
eepsa.eeptpu_init(0x10000000, ...)       # L289  解析 eepnet_config
Xil_DCacheFlush()                        # L295  把可能脏的缓存行刷回 DDR
填充 waddr/sd_input_addr/...              # L298-304  从 eepsa 取硬件地址
if SD_CARD_IS_READY:
    file_read("eepnet.mem", eepnet, NET_SIZE)   # L314  从 SD 搬权重
    Xil_DCacheFlush()                    # L315
if EEP_DVP_CAMERA:  dvp_capture(...)     # L320  开局先采一帧
进入 while 菜单循环                       # L323
```

#### 4.3.3 源码精读

`init_platform()` 是 Xilinx 标准模板，在 ZynqMP 上几乎是空操作：

[platform.c:78-92](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/platform.c#L78-L92) —— 调 `enable_caches()` 和 `init_uart()`。但看 [platform.c:39-53](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/platform.c#L39-L53)：`enable_caches` 里的代码全被 `#ifdef __PPC__` / `#ifdef __MICROBLAZE__` 包住，**ZynqMP 是 ARM Cortex-A53，这两个宏都不定义**，所以这段实际不执行；[platform.c:68-76](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/platform.c#L68-L76) 的 `init_uart` 里 `STDOUT_IS_16550` 也不定义（见 `platform_config.h` 用的是 `STDOUT_IS_PSU_UART`），所以也是空操作，串口波特率 115200 由 FSBL/BSP 已配好。结论：`init_platform()` 在本工程里主要是「占位 + 保留标准骨架」，真正的缓存控制在 `main()` 里用 `Xil_DCache*` 显式做。

中断系统初始化 `init_intr_sys()`（仅 `EEP_DP_ENABLE` 时编译）：

[main.cc:230-238](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L230-L238) —— 四步顺序：
1. `Init_Intr_System(&Intc)` —— 初始化 GIC 控制器；
2. `Dpdma_init(&RunCfg, &Intc)` —— 配置 DP 显示子系统；
3. `Dpdma_Setup_Intr_System(&RunCfg)` —— 注册 DP 的中断处理函数；
4. `Setup_Intr_Exception(&Intc)` —— 打开 ARM 全局中断。

其中第 1、4 步在 `sys_intr.c`：

[sys_intr.c:35-55](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sys_intr.c#L35-L55) —— `Init_Intr_System` 用 `XScuGic_LookupConfig` 按 `INTC_DEVICE_ID` 找到 GIC 配置，再 `XScuGic_CfgInitialize` 初始化。

[sys_intr.c:24-33](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sys_intr.c#L24-L33) —— `Setup_Intr_Exception` 做 ARM 异常层面的三件事：`Xil_ExceptionInit`、把 GIC 中断处理函数注册到 ARM 的中断异常向量、`Xil_ExceptionEnable` 打开中断。注意全局 GIC 实例 `XScuGic Intc;` 定义在 [sys_intr.c:22](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sys_intr.c#L22)。

> 🔑 **关键结论（贯穿本讲）**：整个工程里**只有 DP 显示用到中断**，`init_intr_sys()` 是唯一设置 GIC 的地方。这意味着——**关掉 `EEP_DP_ENABLE` 会连带关掉整个中断子系统**。这一点在 4.4 的实践中会用到。

关于缓存一致性，看 `main.cc` 里这几处显式控制：[main.cc:279](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L279) 在初始化后 `Xil_DCacheDisable()` 关掉 D-cache，让后续 CPU 对 DDR 的写直接落内存，TPU/DVP 这些 DMA 主设备能立刻读到；[main.cc:295](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L295) 和 [main.cc:315](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L315) 在写完关键数据后 `Xil_DCacheFlush()` 把脏行刷回。在 yolo3 后处理阶段又会临时 `Xil_DCacheEnable()` 加速 CPU 浮点运算、算完再 `Flush`+`Disable`（[main.cc:416-422](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L416-L422)）。这是一套「默认关 cache 保 DMA 一致、需要 CPU 算力时临时开」的典型裸机策略。

#### 4.3.4 代码实践：跟踪启动序列

1. **实践目标**：验证「`init_intr_sys` 依赖 DP、关 DP 即关中断」这一结论。
2. **操作步骤**：
   - 在 `main.cc` 中搜索所有 `init_intr_sys`、`Init_Intr_System`、`Setup_Intr_Exception` 的调用点。
   - 确认它们是否全部出现在 `#ifdef EEP_DP_ENABLE` 内部。
   - 再搜索 `XScuGic`、`Xil_ExceptionEnable`，看除 DP 外有没有别处用中断。
3. **需要观察的现象**：所有 GIC/异常相关调用都在 `EEP_DP_ENABLE` 保护下；工程里没有第二个中断源（摄像头采集 `dvp_capture` 是轮询而非中断）。
4. **预期结果**：得出「DP 是唯一中断使用者」的结论，从而预测关掉 `EEP_DP_ENABLE` 后中断代码会被条件编译整段剔除。
5. 待本地验证（纯阅读）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `file_read("eepnet.mem", ...)` 之后要紧跟一句 `Xil_DCacheFlush()`？
  - **答**：`file_read` 由 CPU 把权重字节写入 DDR 里的 `eepnet`（即 `waddr`）区域。若 D-cache 此刻处于开启或半开启状态，这些写可能还停留在缓存行里；TPU 作为 DMA 主设备会直接去 DDR 读，可能读到旧值。`Xil_DCacheFlush` 把脏行强制刷回 DDR，保证 TPU 看到的是最新权重。
- **练习 2**：`init_platform()` 在 ZynqMP 上几乎什么都不做，为什么还要保留它？
  - **答**：它是 Xilinx 官方裸机模板的标准入口，保留它便于跨芯片家族复用（同一份代码改到 MicroBlaze/PPC 时 `enable_caches` 就会生效），也让熟悉 Xilinx 流程的开发者一眼认出启动入口。

---

### 4.4 config.h 编译开关

#### 4.4.1 概念说明

裸机工程没有运行时配置文件，所有「功能裁剪」都靠 `config.h` 里的 `#define` 在**编译期**决定。改一个宏、重新编译，就能让同一个 `main.cc` 长出或剪掉一整块功能。理解这些开关，就掌握了「这份代码到底会编译成什么样」。

`config.h` 里的宏大致分四类：TPU 地址/寄存器、网络类型与尺寸、功能开关（摄像头/SD/DP/调试）、输入数据组织。

#### 4.4.2 核心流程：四类开关

| 类别 | 宏 | 作用 |
| --- | --- | --- |
| TPU 地址 | `EEPTPU_MEM_BASE_ADDR`、`EEPTPU_REG_BASE_ADDR`、`EEPDVP_REG_BASE_ADDR` | 数据区与控制寄存器区物理地址，与硬件 `assign_bd_address` 一致（u1-l3） |
| TPU 寄存器 | `EEPTPU_*_REG`（BASEADDR0-3/ALGOADDR/STARTUP/STATUS/RUNTIMER） | 把寄存器偏移封装成「可写的 volatile 变量」，u4-l2 详解 |
| 网络类型 | `NET_TYPE`（Classify/Object_Detect/Segmentation）、`NET_SIZE`、`INPUTDATA_SIZE` | 决定后处理分支与 SD 加载的字节数 |
| 输入组织 | `FG_INPUT_DATA_SEPERATED` | 1=输入数据单独存 `eepinput.h`，0=合并进 `eepnet.h` |
| 功能开关 | `EEP_DVP_CAMERA`、`IMG_RGB565`、`IMG_HEIGHT/WIDTH`、`SD_CARD_IS_READY`、`EEP_DP_ENABLE`、`EEP_DEBUG_INFO` | 摄像头/图像格式/分辨率/SD/DP/调试日志 |

#### 4.4.3 源码精读

地址与寄存器定义：

[config.h:24-36](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L24-L36) —— `EEPTPU_MEM_BASE_ADDR 0x31000000`（数据区）、`EEPTPU_REG_BASE_ADDR 0xA0000000`（控制寄存器区，与 u2-l4 的 SoC zone 完全一致）；下面一串 `(*(volatile unsigned int *)(...))` 宏把每个寄存器偏移（如 `+0x50` 是 BASEADDR0、`+0x34` 是 STARTUP、`+0xC` 是 STATUS）映射成一个可读写的「寄存器变量」。注意 `0xA00C0000` 是 DVP 摄像头 IP 的寄存器区（u1-l3 提到的另一条控制通路）。

网络类型与尺寸：

[config.h:40-52](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L40-L52) —— `NetType_Classify=0`、`NetType_Object_Detect=1`、`NetType_Semantic_Segmentation=2`；当前 [config.h:45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L45) 启用的是 `NET_TYPE = NetType_Object_Detect`（yolov4-tiny），分类那行被注释掉；`NET_SIZE=12240064` 是 `eepnet.mem` 权重字节数，`INPUTDATA_SIZE=5537792` 是 `eepinput.mem` 字节数——这两个数必须和 `eepBinCvt` 实际产出的文件大小一致，否则 `file_read` 会读错。`YOLO3_DETECTION_OUTPUT` 仅在检测类型时置 1，控制是否编译 yolo3 软件后处理（u6-l3）。

功能开关：

[config.h:54-67](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L54-L67) ——
- `EEP_DEBUG_INFO`（默认注释掉）：打开后 `main.cc` 里大量 `#ifdef EEP_DEBUG_INFO` 包住的调试打印（寄存器值、耗时）会输出。
- `EEP_DVP_CAMERA` + `IMG_RGB565` + `IMG_HEIGHT 720`/`IMG_WIDTH 1280`：启用摄像头、采 RGB565、分辨率 1280×720。
- `SD_CARD_IS_READY`：启用 SD 卡（加载网络/输入、存 BMP）。
- `EEP_DP_ENABLE`：启用 DP 显示输出（连带启用中断子系统）。

#### 4.4.4 代码实践：关掉 SD 卡与 DP 显示

这是本讲的核心实践，也是把「编译开关 → 代码裁剪」串起来的最佳练习。

1. **实践目标**：精确说出要改哪些宏，以及改完后 `main.cc` 里哪些代码会消失。
2. **操作步骤**：
   - **关掉 SD 卡**：把 [config.h:64](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L64) 的 `#define SD_CARD_IS_READY` 注释掉。
   - **关掉 DP 显示**：把 [config.h:67](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L67) 的 `#define EEP_DP_ENABLE` 注释掉。
3. **需要观察的现象**（在 `main.cc` 里用 `#ifdef` 对照）：
   - 关 SD 后消失的代码：`SD_Init()`（[main.cc:264-267](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L264-L267)）、菜单「3: Save Image」行（[main.cc:328-330](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L328-L330)）、整个 `case '3'`（[main.cc:499-513](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L499-L513)）、`eepnet.mem` 加载（[main.cc:311-317](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L311-L317)）；`case '4'` 会从 SD 分支切换到 `eepinput` 数组分支（[main.cc:514-526](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L514-L526)）。**注意副作用**：关了 SD，网络权重无法从卡加载，必须改用编译期烧入的 `eepnet.h`/`eepinput.h`，且要保证 `FG_INPUT_DATA_SEPERATED` 与实际数据组织一致。
   - 关 DP 后消失的代码：DP 全局变量 `GFrame/ARGB` 等（[main.cc:78-88](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L78-L88)）、`graphic_buffer_init`+`init_intr_sys`（[main.cc:281-284](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L281-L284)）、所有 `dp_display(...)` 调用、整个 `init_intr_sys()` 函数体（[main.cc:230-238](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L230-L238)。**注意副作用**：因为 DP 是唯一的中断使用者，关 DP 会同时关掉 GIC 中断子系统。
4. **预期结果**：得到一份「改一个宏 → 剔除哪些代码 → 有什么副作用」的清单，能预测编译后的程序行为。
5. 待本地验证（改宏后需在 Vitis 重新编译观察；纯阅读可完成对照分析）。

#### 4.4.5 小练习与答案

- **练习 1**：如果把 `NET_TYPE` 从 `NetType_Object_Detect` 改成 `NetType_Classify`，菜单选项 2 的行为会怎么变？
  - **答**：`YOLO3_DETECTION_OUTPUT` 不再置 1（[config.h:50-52](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L50-L52) 的 `#if` 不成立），yolo3 软件后处理代码被剔除；`#if (NET_TYPE == NetType_Classify)` 成立（[main.cc:400](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L400)），改成调用 `get_topk` 打印 top5 分类结果。同时 `eepnet.h`/`eepnet.mem` 也必须换成分类网络的产物，`NET_SIZE`/`INPUTDATA_SIZE` 同步改。
- **练习 2**：`NET_SIZE` 和 `INPUTDATA_SIZE` 这两个数字从哪里来？填错了会怎样？
  - **答**：它们等于 `eepBinCvt` 转出的 `eepnet.mem` 和 `eepinput.mem` 的实际文件大小（字节数）。`file_read` 按这个长度从 SD 读到内存，填大了会越界读到相邻内存区、填小了网络权重不完整，都会导致推理结果错误或硬件异常。换网络时必须同步更新。

---

## 5. 综合实践

**任务：为「无摄像头、无 SD 卡、无 DP 显示」的最小推理配置裁剪工程，并预测启动序列。**

背景：假设你拿到一块只有 TPU IP、没接摄像头/DP 显示器、也没焊 SD 卡的精简板子，需要让裸机程序仍能跑一次分类推理。

要求：

1. 打开 [config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h)，列出需要注释掉的宏（提示：`EEP_DVP_CAMERA`、`SD_CARD_IS_READY`、`EEP_DP_ENABLE`），并说明每个宏关掉后 `main()` 启动序列里哪几步会消失（对照 4.3.2 的伪代码行号）。
2. 在此配置下，重新写出 `main()` 从入口到进入菜单循环之间**实际会执行**的语句序列（即把被 `#ifdef` 剔除的步骤划掉）。
3. 推断：此时菜单里还会出现哪些选项？选项 1、4 分别会走哪个分支？（提示：选项 1 会打印 "No camera !!!"；选项 4 会走 `eepsa.eeptpu_input(eepinput, ...)` 用编译期数组。）
4. 指出这个最小配置下「网络权重和测试输入从哪里来」——答案应是编译期 `#include` 的 `eepnet.h` 和 `eepinput.h`（受 `FG_INPUT_DATA_SEPERATED` 控制），并说明为什么这要求 `eepnet.mem`/`eepinput.mem` 的内容必须事先以数组形式编进头文件。

预期产出：一份裁剪后的宏清单 + 精简启动序列 + 菜单可用选项表 + 数据来源说明。完成后，你就真正理解了「编译开关如何驱动整份裸机代码的形态」。本实践不需要硬件，纯源码阅读即可完成；若要验证，需在 Vitis 中重新编译并查看链接后的 ELF 大小变化（待本地验证）。

## 6. 本讲小结

- `sdk/standalone/` 分 `net_model/`（x86 上的编译/转换工具链）和 `src/`（裸机 C++ 源码）；`src/net_data/` 里的 `eepnet.h` 编译期 include、`*.mem` 运行期从 SD 加载，体现裸机「无文件系统」的约束。
- 工程用 Xitis IDE 基于 `system_wrapper.xsa` 构建，无 Makefile；产物是裸机 ELF，由 FSBL 上电后加载跳转。
- `main.cc` 是「上电初始化序列 + 串口 `while` 菜单循环」结构；菜单选项 1（采集）、2（forward）、3（存盘，仅 SD）、4（读输入）、5（实时循环）把推理链路拆成可分步调试的环节；**选项 5 是 `while(1)` 死循环，无法回到主菜单**。
- 平台初始化是一条线性可追溯序列：`init_platform`（ZynqMP 上近乎空操作）→ SD → 摄像头 I2C → 关 D-cache → DP 帧缓冲与中断 → `eeptpu_init` → 加载权重 → 开局采一帧。
- 中断子系统完全由 DP 显示驱动：`init_intr_sys()` 四步（`Init_Intr_System`→`Dpdma_init`→`Dpdma_Setup_Intr_System`→`Setup_Intr_Exception`）是工程里唯一的 GIC 配置点；**关掉 `EEP_DP_ENABLE` 会连带关掉整个中断**。
- `config.h` 用 `#define` 在编译期裁剪功能：地址/寄存器类（`EEPTPU_*_ADDR/BASEADDRx_REG`）、网络类（`NET_TYPE/NET_SIZE/INPUTDATA_SIZE`）、开关类（`EEP_DVP_CAMERA/SD_CARD_IS_READY/EEP_DP_ENABLE/EEP_DEBUG_INFO`）；改一个宏会通过条件编译剔除 `main.cc` 里一整段代码。
- 裸机靠 `Xil_DCacheDisable/Flush` 手动管理 DMA 缓存一致性：默认关 cache 保 TPU/DVP 数据新鲜，CPU 密集计算时临时开 cache 提速。

## 7. 下一步学习建议

本讲建立了裸机工程的「骨架与开关」，接下来应进入肌肉与神经：

- **u4-l2 EEPTPU_SA 类与 TPU 寄存器协议**：本讲只提到 `eepsa.eeptpu_init()` 和 `tpu_forward()`，下一讲深入 `eeptpu_sa.cpp`，讲清 `config.h` 里那串 `EEPTPU_BASEADDR0-3/ALGOADDR/STARTUP/STATUS` 寄存器在 `tpu_forward` 里「写地址→写 0x11 启动→轮询 STATUS 第 31 位」的完整时序。
- **u4-l3 eep_interface：AXI 内存与寄存器读写**：讲清 `EEPTPU_SA` 之下那一层 `EEP_INTERFACE` 抽象（`mem_read/write`、`register_read/write/wait`），理解分层设计。
- 想提前了解「上电之前的硬件怎么来的」，可回顾 u1-l3（BOOT.BIN/xsa）与 u1-l4（Vivado 工程构建）。
- 想了解 `eepnet_config` 数组到底装了什么、`eeptpu_init` 怎么解析，回顾 u3-l3。
