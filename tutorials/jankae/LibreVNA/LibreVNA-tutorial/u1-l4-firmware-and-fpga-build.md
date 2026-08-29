# 固件与 FPGA 工具链：构建、烧写与版本演进

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LibreVNA 两份「设备端镜像」——MCU 固件（STM32CubeIDE 产出 `VNA_embedded.bin`）与 FPGA bitstream（Xilinx ISE 产出 `top.bin`）——各自的工具链与构建入口。
2. 逐字段解释 [AssembleFirmware.py](AssembleFirmware.py) 如何把这两份二进制拼成带 24 字节头的 `combined.vnafw`，并说明头部字段如何与固件端、GUI 端代码一一对应。
3. 梳理「FPGA 配置存在板载 flash、由 MCU 通过 USB 更新」的免 JTAG 方案：从 GUI 固件升级对话框，到 USB 命令，到 flash 写入，再到上电自举时 MCU 给 FPGA 喂数据、必要时自我更新。
4. 了解仓库中的硬件版本与变更记录，知道遇到旧硬件时该查哪份文档。

本讲仍然**不需要实体设备**：核心实践是用「假文件」在本地完整跑一遍固件组装流程并解析产物。

## 2. 前置知识

### 2.1 两份镜像，两个世界

LibreVNA 的设备端软件其实有两份完全不同的「镜像」：

- **MCU 固件**：运行在 STM32G431 微控制器上的 C/C++ 程序（上一讲已知它内部还带着 FreeRTOS）。它是普通的可执行代码，编译产物是一个二进制映像 `VNA_embedded.bin`，可以直接写进 MCU 的内部 flash。
- **FPGA bitstream**：FPGA（Spartan 6）本身是「空白的」——它没有固件概念，上电后需要被灌入一份「电路描述数据」才能变成我们想要的逻辑（采样、DFT、扫描控制等）。这份数据叫 bitstream，是 VHDL 源码经过「综合 → 布局布线 → 生成编程文件」后得到的 `top.bin`。

**关键区别**：MCU 断电后程序仍在内部 flash 里；FPGA 的 bitstream 在断电后会丢失，必须存在别处（LibreVNA 用一颗板载 SPI flash 芯片），每次上电重新加载。

### 2.2 本讲会用到的术语

| 术语 | 通俗解释 |
|---|---|
| SWD / ST-Link | STM32 的调试接口与调试器。通过 PCB 上的 SWD 焊盘直连 MCU 内部 flash，是「最后一次物理烧写」的手段 |
| JTAG | FPGA 常用的配置/调试接口。LibreVNA 的设计目标是**完全不需要**它 |
| STM32CubeIDE | ST 官方 IDE，集成了交叉编译工具链和图形化工程配置 |
| Xilinx ISE | 老 Xilinx 的 FPGA 开发套件（ISE 14.7 是支持 Spartan 6 的最后版本），本仓库用它的 Project Navigator |
| `.xise` | ISE 的工程文件，XML 格式，列出了参与综合的源文件 |
| `.xco` | ISE 的 IP 核（Core Generator）描述文件，如 ROM、PLL、DDS |
| CRC32 | 循环冗余校验，一段数据的 32 位「指纹」，用于检测传输/存储中的损坏 |
| 外部 flash | 板上一颗独立的 SPI flash 存储芯片（容量至少 1 MiB，见后文），存放 bitstream + MCU 固件镜像副本 |

### 2.3 承接上一讲的关键结论

u1-l1 已经指出：**「FPGA bitstream 存于 Flash、由 MCU 写入，整机纯 USB 即可升级，无需 JTAG」**。本讲就是把这句话拆成代码级证据：谁来拼文件（Python 脚本）、谁来传文件（GUI 对话框 + USB 协议）、文件存哪（外部 flash）、上电后谁来读（MCU 自举 + FPGA 配置）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Documentation/DeveloperInfo/BuildAndFlash.md](Documentation/DeveloperInfo/BuildAndFlash.md) | 官方构建与烧写说明书：三个工具链、四步「点亮新板」流程 |
| [AssembleFirmware.py](AssembleFirmware.py) | 仓库根目录的组装脚本：拼接两份镜像 → `combined.vnafw` |
| [FPGA/VNA/VNA.xise](FPGA/VNA/VNA.xise) | ISE 工程文件：FPGA 侧参与综合/仿真的文件清单 |
| [Documentation/DeveloperInfo/VersionsAndModifications.md](Documentation/DeveloperInfo/VersionsAndModifications.md) | 硬件版本差异与必改/可选改动记录 |
| [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp](Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp) | GUI 侧固件升级对话框：擦除→分块传输→触发更新的状态机 |
| [Software/VNA_embedded/Application/App.cpp](Software/VNA_embedded/Application/App.cpp) | 固件侧命令处理：`ClearFlash` / `FirmwarePacket` / `PerformFirmwareUpdate` |
| [Software/VNA_embedded/Application/Firmware.cpp](Software/VNA_embedded/Application/Firmware.cpp) | 固件侧自举：校验 flash 头部、必要时从 RAM 里自我刷写 |
| [Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp](Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) | 上电时把 flash 里的 bitstream 灌进 FPGA |
| [Software/VNA_embedded/Application/Communication/PacketConstants.h](Software/VNA_embedded/Application/Communication/PacketConstants.h) | USB 包常量，含固件分块大小 `FW_CHUNK_SIZE = 256` |

## 4. 核心概念与源码讲解

### 4.1 STM32CubeIDE 构建流程：产出 MCU 固件

#### 4.1.1 概念说明

MCU 固件工程是 `Software/VNA_embedded`。它一半是 STM32CubeMX 生成的启动代码与外设初始化（`Src/`、`Middlewares/`），一半是手写业务代码（`Application/`）。构建它不需要命令行——官方文档直接给出 IDE 操作步骤，因为工程配置（交叉编译器选项、链接脚本、构建目录名）都在 IDE 的元数据里。

#### 4.1.2 核心流程

1. 打开 STM32CubeIDE，workspace 设为 `Software/`。
2. 导入既有工程 `VNA_embedded`（File > Import > Existing Projects into Workspace）。
3. Project > Build Project，产出 `VNA_embedded.bin` 于 `Software/VNA_embedded/Debug/`（或 `Release/`）。
4. 只有「全新空板」或调试时才需要 SWD 直连烧写一次；之后一律走 USB。

#### 4.1.3 源码精读

官方说明中的 MCU 构建段落（IDE 导入 + 构建）见 [Documentation/DeveloperInfo/BuildAndFlash.md:28-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/BuildAndFlash.md#L28-L36)。其中第 34–36 行明确：SWD 烧写「只在新硬件或调试时需要一次」，且要拆掉铝壳才能接 PCB 上的 SWD 焊盘——这就是为什么整套设计要极力避免物理烧写。

链接入口我们在 u1-l2 见过：CubeMX 生成的默认任务在 `USER CODE 5` 区调用项目自己的 `App_Start()`，见 [Software/VNA_embedded/Src/main.c:789-799](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Src/main.c#L789-L799)。这行调用就是「生成代码」与「业务代码」的交接棒——重新生成 CubeMX 代码也不会把它冲掉，因为它写在受保护的 USER CODE 区里。

而本讲真正要盯住的产物路径，写在组装脚本开头的候选列表里：

```python
MCU_FW = ["Software/VNA_embedded/Debug/VNA_embedded.bin",
          "Software/VNA_embedded/Release/VNA_embedded.bin",
          "Software/VNA_embedded/build/VNA_embedded.bin"]
```

见 [AssembleFirmware.py:7](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L7)。注意脚本**不区分 Debug/Release**，三个路径谁修改时间最新就用谁——这是一个方便但值得警惕的设定（见练习 2）。

#### 4.1.4 代码实践

1. **实践目标**：确认固件产物的预期路径与脚本假设一致。
2. **操作步骤**：
   - 在仓库里 `ls Software/VNA_embedded/`，看看是否已有 `Debug/`、`Release/` 或 `build/` 目录（源码仓库通常没有，它们是本地构建后才出现的）。
   - 再看一下 `Software/VNA_embedded/` 下的 `.project` / `.cproject` 文件是否存在——它们就是 CubeIDE 导入时所读的工程元数据。
3. **需要观察的现象**：仓库中不存在任何 `.bin` 产物（源码仓库不带构建产物），但工程元数据存在。
4. **预期结果**：你会确信「`VNA_embedded.bin` 只能由本地构建产生」，因此下一节的组装脚本在没有 CubeIDE 的机器上无法直接跑通——这正好引出 4.3 节用「假文件」实践的思路。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MCU_FW` 列表里有三个候选路径，而不是一个固定路径？

**答案**：不同环境/构建配置会把产物放在不同目录：CubeIDE 的 Debug 配置输出到 `Debug/`，Release 配置输出到 `Release/`，某些命令行构建（如 CI 中）输出到 `build/`。脚本用「取修改时间最新者」的策略（[AssembleFirmware.py:17-28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L17-L28)）自动适配，代价是你必须保证想要的那个文件确实是最新编译出来的。

**练习 2**：如果你昨天编译了 Debug 版、今天改用 Release 版但忘了重新构建，脚本会怎么选？可能造成什么后果？

**答案**：会选中昨天那个更新的 Debug 产物（mtime 更新）。后果是把带调试符号、可能未优化/行为不同的固件刷进设备，且你毫无察觉。脚本只打印一行 `Using ... as MCU firmware`（[AssembleFirmware.py:28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L28)），刷机前务必核对这个提示。

### 4.2 ISE 综合流程：从 top.vhd 到 top.bin

#### 4.2.1 概念说明

FPGA 侧工程在 `FPGA/VNA/`，入口是 `VNA.xise`。ISE 的工作流是：以 `top.vhd` 为顶层做「综合（Synthesize）→ 实现（Implement，含翻译/映射/布局布线）→ Generate Programming File」，最后产出 `top.bin`。`.xise` 本身不包含任何逻辑，它只是一份「文件清单 + 工具选项」的 XML。

#### 4.2.2 核心流程

1. 用 ISE（14.7）打开 `FPGA/VNA/VNA.xise`。
2. Design 标签选中 implementation 视图中的 `top - Behavioral`。
3. 双击 Processes 里的「Generate Programming File」，几分钟后在 `FPGA/VNA/` 得到 `top.bin`。

#### 4.2.3 源码精读

官方说明见 [Documentation/DeveloperInfo/BuildAndFlash.md:38-41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/BuildAndFlash.md#L38-L41)。

工程文件声明了 ISE 版本为 14.7（支持 Spartan 6 的最后一个大版本），见 [FPGA/VNA/VNA.xise:15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L15)：

```xml
<version xil_pn:ise_version="14.7" xil_pn:schema_version="2"/>
```

清单的第一个 VHDL 文件就是顶层 `top.vhd`，紧随其后的是管脚约束文件 `top.ucf`（`FILE_UCF` 类型，把信号绑定到封装引脚），见 [FPGA/VNA/VNA.xise:18-24](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L18-L24)。

清单里还有三类值得注意的条目：

- **IP 核**（`FILE_COREGEN`）：如扫描配置存储 `SweepConfigMem.xco`（[FPGA/VNA/VNA.xise:39-42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L39-L42)）、正余弦查找表 `SinCos.xco`（[FPGA/VNA/VNA.xise:47-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L47-L50)）、时钟管理 `PLL.xco`（[FPGA/VNA/VNA.xise:69-72](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L69-L72)）。它们不是手写 VHDL，而是 ISE 的 Core Generator 产出的参数化块。
- **testbench**（`Test_*.vhd`）：每个文件只带 `BehavioralSimulation` / `PostMapSimulation` 等仿真关联，**没有** `Implementation` 关联——即只参与仿真、不会综合进 bitstream。例如 [FPGA/VNA/VNA.xise:29-34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L29-L34) 的 `Test_MCP33131.vhd`。
- **功能模块**：`Sweep.vhd`、`Sampling.vhd`、`spi_slave.vhd` 等，带 `Implementation` 关联，是 bitstream 的真实组成（单元 6 会逐个精读）。

#### 4.2.4 代码实践

1. **实践目标**：学会把 `.xise` 当作「FPGA 工程的 `.pro` 文件」来读——不用打开 ISE 也能知道什么会进 bitstream。
2. **操作步骤**：
   - 用文本编辑器打开 `FPGA/VNA/VNA.xise`，统计含 `FILE_VHDL` 的行，按「带 Implementation 关联 / 只有仿真关联」分成两列。
   - 再统计 `FILE_COREGEN` 条目，列出 IP 核名字。
3. **需要观察的现象**：功能模块数量与 testbench 数量大致相当——这个仓库对每个功能块都配了仿真。
4. **预期结果**：得到一张「综合文件清单」和一张「仅仿真清单」。`top.vhd` 是唯一被综合的顶层。若你本地没有 ISE，这份清单也是阅读 FPGA 源码的最好入口（配合 u1-l2 的结论）。
5. 由于本实践只是读文件，结果可直接核对，无需「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果不小心把某个 `Test_*.vhd` 加上了 `Implementation` 关联，会发生什么？

**答案**：testbench 通常包含自激励（时钟生成、信号赋值）且没有可综合的顶层端口语义，综合器要么报错、要么警告后产生错误逻辑。`.xise` 用关联（association）机制把它们隔离在仿真视图里，正是为了避免这种情况。

**练习 2**：为什么 `top.ucf` 只在 `Implementation` 里出现？

**答案**：`.ucf` 是物理约束（引脚位置、时序约束），只在映射/布局布线阶段有意义；行为仿真不关心引脚。见 [FPGA/VNA/VNA.xise:22-24](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L22-L24)。

### 4.3 AssembleFirmware.py：合成 combined.vnafw

#### 4.3.1 概念说明

现在手上有两份镜像：`top.bin`（FPGA）和 `VNA_embedded.bin`（MCU）。USB 升级时设备需要知道「FPGA 数据从哪开始、多长；MCU 镜像从哪开始、多长；数据是否完好」。`AssembleFirmware.py` 就是为它们做一个**带目录（header）的容器文件** `combined.vnafw`。它只有约 70 行，却是连接「构建」与「烧写」两讲内容的枢纽。

#### 4.3.2 核心流程

```
输入: FPGA/VNA/top.bin                (bitstream)
      MCU_FW 候选中 mtime 最新者       (MCU 固件)
输出: combined.vnafw

布局 (小端序):
┌─────────┬──────────────────────────────┐
│ 0x00    │ magic "VNA!"                 │ 4 字节
│ 0x04    │ FPGA 起始地址 (= 24)         │ 4 字节
│ 0x08    │ FPGA 大小                    │ 4 字节
│ 0x0C    │ MCU 起始地址 (= 24+size_FPGA)│ 4 字节
│ 0x10    │ MCU 大小                     │ 4 字节
│ 0x14    │ 链式 CRC32                   │ 4 字节
├─────────┼──────────────────────────────┤
│ 0x18    │ bitstream 原始字节           │
│ ...     │ MCU 固件原始字节             │
│ 末尾    │ 0x00 填充到 256 的整数倍      │
└─────────┴──────────────────────────────┘
```

关键点：

- **CRC 是链式的**：先对 bitstream 算 CRC32（初值 `0xFFFFFFFF`），再把结果作为初值继续对 MCU 固件算——相当于对「两段拼接后的数据」一次算完。
- **末尾按 256 字节对齐填充**。为什么是 256？因为 USB 传输时固件按 256 字节一块写入 flash（见 4.4 节的 `FW_CHUNK_SIZE`），文件尺寸不是 256 的整数倍就无法整块传输。

#### 4.3.3 源码精读

常量定义：bitstream 路径与 24 字节头部大小，见 [AssembleFirmware.py:6-9](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L6-L9)。

写入 magic 并挑选最新 MCU 固件（拿不到任何候选就报错退出），见 [AssembleFirmware.py:11-29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L11-L29)。

头部四个 32 位小端字段（起始地址由常量推导，不需要手工填），见 [AssembleFirmware.py:36-44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L36-L44)：

```python
f.write((HEADER_SIZE).to_bytes(4, byteorder='little'))              # FPGA 起始 = 24
f.write(size_FPGA.to_bytes(4, byteorder='little'))                  # FPGA 大小
f.write((HEADER_SIZE + size_FPGA).to_bytes(4, byteorder='little'))  # MCU 起始
f.write(size_MCU.to_bytes(4, byteorder='little'))                   # MCU 大小
```

链式 CRC32 计算并写入，见 [AssembleFirmware.py:47-56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L47-L56)。`binascii.crc32(buf, initial_CRC)` 的第二个参数是「上一次的 CRC 结果」，两次调用串起来即链式校验。

防御性检查：如果实际写出的头部不等于 `HEADER_SIZE`（说明字段数变了），直接失败，见 [AssembleFirmware.py:59-61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L59-L61)。

最后写入两段载荷并按 256 字节对齐补零，见 [AssembleFirmware.py:63-68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L63-L68)：

```python
f.write(bitstream.read())
f.write(firmware.read())
if f.tell() % 256 != 0:
    padding = 256 - f.tell() % 256
    f.write(bytearray(padding))
```

**与消费端的对应关系**（这是本讲最重要的一张对照表）：

| header 字段 | 生成处（Python） | 消费处（固件 C++） |
|---|---|---|
| `magic[4] = "VNA!"` | [AssembleFirmware.py:12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L12) | `memcmp(&h.magic, "VNA!", 4)` 校验，[Firmware.cpp:29-30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L29-L30) |
| `FPGA_start / FPGA_size` | [AssembleFirmware.py:38-40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L38-L40) | 上电后交给 `FPGA::Configure` 灌 FPGA，[App.cpp:85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L85) |
| `CPU_start / CPU_size` | [AssembleFirmware.py:42-44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L42-L44) | 与运行中固件逐字节比对，决定是否自我更新，[Firmware.cpp:53-66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L53-L66) |
| `crc` | [AssembleFirmware.py:53-56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L53-L56) | 从 flash 连续读 `FPGA_size+CPU_size` 字节重算比对，[Firmware.cpp:36-51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L36-L51) |

固件端的 `Header` 结构体（`__attribute__((packed))` 保证无填充、逐字节对上）见 [Firmware.cpp:14-21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L14-L21)。Python 与 C++ 两侧字段顺序、位宽、小端序完全一致——因为 4.3 节这个脚本与 4.4 节这份固件是**同一份文件格式的一体两面**。

#### 4.3.4 代码实践（本讲主实践）

> 对应大纲任务：阅读 AssembleFirmware.py，写出它读取哪些输入文件、产出什么，以及它与 GUI 内固件升级对话框的关系；如果没有 ISE，说明 bitstream 如何从 Release 获取。

1. **实践目标**：不依赖 CubeIDE/ISE/硬件，用假文件完整跑一遍组装脚本，并用十六进制工具亲手验证 header。
2. **操作步骤**（示例命令，请在你自己的临时目录里操作，不要改动仓库）：
   1. 复制 `AssembleFirmware.py` 到临时目录 `/tmp/fwtest`，并在该目录下按脚本期望的相对路径造假文件：
      ```bash
      mkdir -p /tmp/fwtest/FPGA/VNA /tmp/fwtest/Software/VNA_embedded/Debug
      # 假 bitstream：1024 字节伪随机数据
      head -c 1024 /dev/urandom > /tmp/fwtest/FPGA/VNA/top.bin
      # 假 MCU 固件：1000 字节
      head -c 1000 /dev/urandom > /tmp/fwtest/Software/VNA_embedded/Debug/VNA_embedded.bin
      cd /tmp/fwtest && python3 AssembleFirmware.py
      ```
   2. 观察脚本打印的三行信息（选中的 MCU 固件、两个尺寸、CRC）。
   3. 解析产物头部（示例代码，非项目代码）：
      ```python
      import struct, binascii
      data = open("combined.vnafw","rb").read()
      magic, fpga_start, fpga_size, cpu_start, cpu_size, crc = struct.unpack("<4sIIIII", data[:24])
      print(magic, fpga_start, fpga_size, cpu_start, cpu_size, hex(crc))
      # 自行复算链式 CRC 验证
      c = binascii.crc32(data[fpga_start:fpga_start+fpga_size], 0xFFFFFFFF)
      c = binascii.crc32(data[cpu_start:cpu_start+cpu_size], c)
      print("match:", c == crc, "filesize:", len(data), "len%256:", len(data)%256)
      ```
3. **需要观察的现象**：
   - `fpga_start` 应为 24，`cpu_start` 应为 24+1024=1048；
   - 文件总长应为 24+1024+1000=2048，恰好已是 256 的整数倍，**不会**触发填充——若把假 MCU 固件改成 999 字节，总长应变为 2048（含 1 字节补零）；
   - 复算 CRC 与 header 中一致。
4. **预期结果**：你会得到一张与 4.3.2 节布局图完全对应的实际字节截图/输出，并确认「脚本产出的文件在格式上就是固件端 `Header` 结构体的序列化」。本实践依赖本地 Python 环境，具体输出数值**待本地验证**。
5. **没有 ISE 怎么获得真 bitstream**：官方 Release 的 zip 里已包含编译好的 GUI 与设备固件（`combined.vnafw`），见 [README.md:51-52](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L51-L52)——每个 commit 的 CI 构建版本也在 GitHub Actions 产物中。也就是说：**只有要改 FPGA/MCU 源码时才需要这两个工具链，普通升级直接下载即可**。

#### 4.3.5 小练习与答案

**练习 1**：脚本为什么先写 bitstream 再写 MCU 固件？顺序重要吗？

**答案**：重要。header 中的 `CPU_start = HEADER_SIZE + size_FPGA` 是由这个顺序推导的；固件端校验 CRC 时也是从 `FPGA_start` 起连续读 `FPGA_size + CPU_size` 字节（[Firmware.cpp:39-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L39-L47)），隐含假设两段数据在 flash 中连续且 bitstream 在前。交换顺序会导致校验失败、设备判为「空 flash」。

**练习 2**：脚本对 bitstream 的最大尺寸没有任何检查，但固件端有 `FPGA_MAXSIZE 512000` 和 `CPU_MAXSIZE 131072`。如果 bitstream 超过 512000 字节会怎样？

**答案**：脚本照样打包（它不检查），但刷进设备后上电自检时 `GetFlashContentInfo` 的 sanity check 会因 `h.FPGA_size > FPGA_MAXSIZE` 判定「Invalid content, probably empty FLASH」，设备拒绝加载并点亮错误 LED 2。见 [Firmware.cpp:11-12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L11-L12) 与 [Firmware.cpp:29-34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L29-L34)。

**练习 3**：`CPU_MAXSIZE 131072` 正好是 128 KiB，这暗示了什么？

**答案**：这是 MCU（STM32G431）内部 flash 的容量上限——外部 flash 里的 MCU 镜像最终要被完整复制进内部 flash（见 4.4 节），超过 128 KiB 就装不下。512000 字节则对应 Spartan 6 bitstream 的合理上限。

### 4.4 免 JTAG 烧写闭环：GUI → USB → flash → 自举

#### 4.4.1 概念说明

本节回答学习目标 3。整个方案的地基是：**MCU 能读写那颗外部 flash，而 FPGA 的配置数据也放在同一颗 flash 里**（README 明确说明这一点，见 [README.md:87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L87)）。于是「更新固件」变成「通过 USB 让 MCU 把新文件写进外部 flash，再重启」。三个角色分工：

- **GUI 对话框**：读文件、切块、按协议发送、等 Ack；
- **固件命令处理**：把收到的块写 flash；
- **固件自举**：上电时校验 flash 内容、给 FPGA 灌 bitstream、必要时把自己更新掉。

#### 4.4.2 核心流程

GUI 侧状态机（对话框架构）：

```
Idle → ErasingFLASH ──Ack──→ TransferringData ──每块Ack，传完──→ TriggeringUpdate
          │ 发送 ClearFlash       │ 循环发 FirmwarePacket        │ 发送 PerformFirmwareUpdate
          └──Nack/超时──→ 报错退出 │                              └─Ack──→ WaitingForReboot
                                                                  （轮询设备重新枚举）→ 重连 → Idle
```

固件侧启动流程：

```
上电 → App::init → 检测外部 flash → 读 header + 校验 CRC
     ├─ 无效 → LED Error 2（新空板的正常现象）
     ├─ 有效 → FPGA::Configure(从 flash 灌 bitstream)
     │         失败 → LED Error 3
     └─ CPU 镜像与运行中固件不同 → （自我更新路径，见下）
```

#### 4.4.3 源码精读

**GUI 侧**：对话框在启动时校验文件尺寸必须是 256 的整数倍、读取前 24 字节并检查 magic 字符串，见 [firmwareupdatedialog.cpp:70-110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L70-L110)。其中：

```cpp
if(file->size() % PacketConstants::FW_CHUNK_SIZE != 0) {
    abortWithError("Invalid file size");
    return;
}
```

`FW_CHUNK_SIZE = 256` 定义在 [PacketConstants.h:31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h#L31)——这就是 4.3 节脚本按 256 对齐的原因：**脚本与对话框隔着 USB 线约定了同一个分块单位**。

发送侧状态机 `receivedAck()`：擦除 Ack 后开始传数据；每块 Ack 后推进进度；传完发送 `PerformFirmwareUpdate`；收到 Ack 后断开、轮询设备重新枚举、自动重连。见 [firmwareupdatedialog.cpp:180-219](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L180-L219)。分块发送函数把文件偏移直接作为 flash 地址：

```cpp
p.firmware.address = transferredBytes;   // 文件内偏移 == flash 内偏移
file->read((char*) &p.firmware.data, PacketConstants::FW_CHUNK_SIZE);
```

见 [firmwareupdatedialog.cpp:234-241](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L234-L241)。注意它发的第一个块就是那 24 字节 header——**整个 `combined.vnafw` 被原样镜像到 flash 的 0 地址**，所以 Python 脚本里的「文件内偏移」与固件端的「flash 内地址」是同一套数字。

**固件侧命令处理**（都在 `App_Process` 的 USB 包分发 switch 里，且包在 `#ifdef HAS_FLASH` 中）：

- `ClearFlash`：先转 Idle 模式，擦除 0 到 `Firmware::maxSize`（1 MiB，[Firmware.hpp:15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.hpp#L15)）的 flash 范围，回 Ack，见 [App.cpp:202-213](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L202-L213)；
- `FirmwarePacket`：把 256 字节数据按给出的地址写入 flash，逐块 Ack，见 [App.cpp:214-222](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L214-L222)；
- `PerformFirmwareUpdate`：重新调用 `GetFlashContentInfo()` 校验完整性，有效则 Ack、延时让通信收尾，然后 `Firmware::PerformUpdate(fw_info)`，见 [App.cpp:223-235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L223-L235)。

**自举侧**：上电时 `App::init` 检查 flash 是否存在、调用 `GetFlashContentInfo` 校验；无效则 `LED::Error(2)`——这正是官方文档「点亮新板」第 2 步说的「Booting LED 指示错误码 2（flash 内容无效）」，见 [App.cpp:74-96](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L74-L96) 与 [BuildAndFlash.md:49-56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/BuildAndFlash.md#L49-L56)。

`FPGA::Configure` 是「MCU 当 FPGA 的编程器」的现场：先通过 `PROGRAM_B`/`INIT_B` 引脚握手复位 FPGA，然后循环「从外部 flash 读 256 字节 → 经 SPI 灌给 FPGA」，最后检查 `DONE` 引脚确认配置成功，见 [FPGA.cpp:38-86](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L38-L86)。FPGA 侧不需要任何 JTAG——它的配置线就挂在 MCU 的 SPI 外设上。

最精彩的是 MCU 自我更新：`GetFlashContentInfo` 会把外部 flash 里的 CPU 镜像与**当前正在运行**的固件（从 `0x8000000`，即内部 flash 起址）逐字节比较，发现差异就置 `CPU_need_update`，见 [Firmware.cpp:52-66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L52-L66)。真正执行更新的 `copy_flash` 被放进 `.data` 段（RAM 中运行，[Firmware.cpp:76-86](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L76-L86)）——因为它要先**擦除整个内部 flash**，再从外部 flash 逐字节搬回新固件，全程不允许任何函数调用（代码注释原话：「!NO FUNCTION CALLS AT ALL ARE ALLOWED IN HERE！」），最后触发软件复位，见 [Firmware.cpp:86-160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L86-L160)。这就是「升级 MCU 固件也不需要 SWD」的实现代价与巧妙之处。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：把「一次 USB 固件升级」整理成一份带文件行号的时序清单，验证你对三个角色分工的理解。
2. **操作步骤**：从 [firmwareupdatedialog.cpp:120-124](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L120-L124)（发送 `ClearFlash`）开始，沿着「GUI 动作 → USB 包类型 → 固件 case 分支 → flash 操作 → Ack 回到 GUI 哪个 case」的线索，手工列出前 5 步（擦除 + 前 3 块 + 触发更新），每步标注两个文件的具体行号。
3. **需要观察的现象**：你会发现「GUI 的每个 `state` 恰好对应固件的一个 `case`」，两边靠 Ack/Nack + 超时定时器保持同步；块地址 `transferredBytes` 在两端含义相同。
4. **预期结果**：一张两列（GUI 侧 / 固件侧）时序表。这是纯代码阅读，可直接对照源码核对。
5. 若想进一步动手（可选）：在 [App.cpp:214-222](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L214-L222) 的 `FirmwarePacket` 分支已有 `LOG_INFO("Writing firmware packet at address %u", ...)` 日志，真实刷机时 USB 日志通道就能看到每块地址——**待本地验证**（需要设备）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `PerformFirmwareUpdate` 处理里先 `Ack` 再延时 100ms 才执行更新？

**答案**：`PerformUpdate` 一旦开始就会擦除自身所在的 flash 并最终复位，绝无机会补发应答。所以必须先把 Ack 发出去，留时间让 USB 通信真正收尾，然后才动刀。见 [App.cpp:223-233](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L223-L233)。

**练习 2**：GUI 状态机里 `WaitingForReboot` 靠什么判断「设备回来了」？

**答案**：靠轮询 `GetAvailableDevices()` 看该序列号的设备是否重新出现在设备列表（USB 重新枚举），出现后再等 3 秒让固件完成初始化才重连。见 [firmwareupdatedialog.cpp:150-173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L150-L173)。

**练习 3**：上电自举中 `CPU_need_update` 为真时的自我更新调用在 [App.cpp:81-84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L81-L84) 处于什么状态？这说明更新主要发生在什么时机？

**答案**：该调用目前被注释掉了（源码中 `// Firmware::PerformUpdate(...)` 一行被注释，注释保留说明「函数不会返回」）。这说明当前版本的自我更新路径主要经由 `PerformFirmwareUpdate` 命令在**收到 GUI 指令时**触发，而非每次上电自动比对触发；上电路径保留了比较逻辑与标记（`CPU_need_update`），便于需要时恢复自动更新。

### 4.5 版本与变更记录

#### 4.5.1 概念说明

开源硬件项目会持续改版。LibreVNA 至今有两个 PCB 原型版本，**固件对两者通用**，但第一版有必须手工修补的错误。做固件/驱动开发前先确认手里的板子是哪一版，能省下大量排错时间——这份「版本地图」就是为此存在的。

#### 4.5.2 核心流程

1. 看 USB-C 旁边有没有 DC 电源插座：有 = 第二版，无 = 第一版。
2. 第一版：检查两项必改（MOSI/CLK 交换、底部裸露走线贴 kapton 胶带）。
3. 对照可选改动决定是否升级性能。

#### 4.5.3 源码精读

版本总述与识别方法见 [VersionsAndModifications.md:1-5](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/VersionsAndModifications.md#L1-L5)。

第一版的致命错误与必改项：FPGA 配置的 MOSI/CLK 在 PCB 上接反了（这正是 4.4 节配置链路的物理层事故——不改的话固件无法启动）、底部走线穿过铝屏蔽罩需绝缘，见 [VersionsAndModifications.md:12-19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/VersionsAndModifications.md#L12-L19)。有意思的是，u1-l1 讲过的 RF 框图知识在这里呼应：第一版还在铝壳上打四个额外螺丝孔以改善隔离度（可选改动）。

第二版相对第一版的改动清单（DC 插座、更多螺丝、数字控制线 RC 低通、一本振馈线衰减器）见 [VersionsAndModifications.md:36-41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/VersionsAndModifications.md#L36-L41)；遗留问题（DC 插座引脚接反不可用、低频放大器问题与第一版相同）见 [VersionsAndModifications.md:42-46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/VersionsAndModifications.md#L42-L46)。

代码里同样有版本分叉：`App.cpp` 用 `#if HW_REVISION == 'A'/'B'` 区分两版硬件的电源/USB 使能差异，见 [App.cpp:97-100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L97-L100) 与 [App.cpp:109-112](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L109-L112)——「两版固件相同」准确说是「同一份固件源码，按宏编译出适配各自硬件的行为」。

#### 4.5.4 代码实践

1. **实践目标**：把「点亮新板」官方流程与 LED 错误码对上号。
2. **操作步骤**：把 [BuildAndFlash.md:49-56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/BuildAndFlash.md#L49-L56) 的四步流程抄下来，在每步旁边标注：这一步对应本讲哪个源码位置（提示：第 2 步对应 `App.cpp` 的 `LED::Error(2)`；第 3 步对应 4.4 节整条链路）。
3. **需要观察的现象**：流程第 1 步（SWD 烧 MCU 固件）是全程唯一需要物理接触的步骤，之后完全走 USB。
4. **预期结果**：一张「步骤 ↔ 源码」对照表；无硬件时不产生实际 LED 现象，属纯阅读任务，结论可直接核对。

#### 4.5.5 小练习与答案

**练习 1**：为什么第一版 PCB 的 MOSI/CLK 交换错误会导致「固件无法启动」，而不是「测量结果错误」？

**答案**：这对信号是 4.4 节讲的 FPGA 配置 SPI（MCU 给 FPGA 灌 bitstream 的通道），不是测量数据通路。配置数据线接反意味着 bitstream 一位都灌不进去，`FPGA::Configure` 检查的 `DONE` 引脚永远不置位，设备停在配置失败（LED 错误 3）。见 [VersionsAndModifications.md:13](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/VersionsAndModifications.md#L13) 与 [FPGA.cpp:51-54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L51-L54)。

**练习 2**：两版硬件「固件相同」在代码层面是怎么实现的？

**答案**：通过 `HW_REVISION` 宏条件编译：同一份 `App.cpp` 在 'A' 版使能 USB 供电开关、在 'B' 版使能 6V 射频电源，其余逻辑共用。文档层面则声明「两版差异很小、固件对两版通用」（[VersionsAndModifications.md:3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/VersionsAndModifications.md#L3)）。

## 5. 综合实践

**任务：做一次「纸上固件升级」的全链路演练。**

目标：把本讲四个环节（两份镜像 → 组装 → USB 分块传输 → 自举校验）串成一条你亲手验证过的数据流。

步骤：

1. 按 4.3.4 节方法在临时目录用假文件生成 `combined.vnafw`，记录脚本打印的两个尺寸与 CRC。
2. 用 4.3.4 节的 `struct.unpack` 脚本解析 header，手工验算：`fpga_start == 24`、`cpu_start == 24 + fpga_size`、`文件总长 == cpu_start + cpu_size`（必要时加上 256 对齐填充）。用公式表达：

   \[
   L_{\text{file}} = 256 \times \left\lceil \frac{24 + S_{\text{FPGA}} + S_{\text{MCU}}}{256} \right\rceil
   \]

3. 打开固件端 [Firmware.cpp:14-21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L14-L21) 的 `Header` 结构体，逐字段核对你解析出的 6 个值，确认「Python 写的 = C++ 读的」。
4. 再对照 [firmwareupdatedialog.cpp:234-241](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L234-L241) 的 `sendNextFirmwareChunk`，计算你的假文件会被切成多少个 256 字节的块、第一块和最后一块的 `address` 字段各是多少。
5. 写 200 字总结：如果烧到一半 USB 断了，设备处于什么状态？下次上电会发生什么？（提示：答案在 `ClearFlash` 与 `GetFlashContentInfo` 的交互里。）

验收标准：第 2 步的每个等式都成立；第 4 步块数 = 文件总长 / 256；第 5 步能指出「半成品 flash 会导致 CRC/magic 校验失败 → LED 错误 2 → 但 MCU 里的旧固件仍在运行，可再次升级」。具体数值依赖你的假文件尺寸，**待本地验证**。

## 6. 本讲小结

- LibreVNA 设备端有两份镜像：STM32CubeIDE 产出的 `VNA_embedded.bin`（MCU）与 Xilinx ISE 14.7 产出 `top.bin`（FPGA bitstream）；普通用户可直接用 GitHub Release 里已组装好的固件。
- [AssembleFirmware.py](AssembleFirmware.py) 把两份镜像拼成 `combined.vnafw`：4 字节 magic + 四个 32 位地址/长度字段 + 链式 CRC32 共 24 字节头，末尾按 256 字节对齐填充；该格式与固件端 `Header` 结构体逐字节对应。
- 免 JTAG 方案的地基是「bitstream 与 MCU 镜像副本同存于 MCU 可访问的外部 flash」：GUI 对话框按 `ClearFlash → FirmwarePacket×N → PerformFirmwareUpdate` 三段式把文件原样镜像进 flash，文件内偏移即 flash 地址。
- 上电自举时固件校验 header/CRC（失败 = LED 错误 2），然后 `FPGA::Configure` 经 SPI 把 bitstream 灌进 FPGA（`PROGRAM_B/INIT_B/DONE` 握手），MCU 自我更新则由一段必须在 RAM 中运行、禁止任何函数调用的 `copy_flash` 完成。
- 256 字节这个数字在三个地方闭环：脚本的对齐填充、`FW_CHUNK_SIZE` 常量、flash 写块大小——跨 Python/C++/USB 的隐式契约。
- 硬件有两版原型，固件用 `HW_REVISION` 宏适配；第一版有必须手工修补的 MOSI/CLK 交换错误，升级前先查 [VersionsAndModifications.md](Documentation/DeveloperInfo/VersionsAndModifications.md)。

## 7. 下一步学习建议

- 下一讲（u2-l1）回到 PC 端，进入 `main.cpp` → `AppWindow` 的 GUI 启动流程；本讲的固件升级对话框 `FirmwareUpdateDialog::FirmwareUpdate()` 静态方法正是被 AppWindow 的 Device 菜单调用的，届时你会看到它在主流程中的位置。
- 若你对设备端更感兴趣，可以先跳到单元 4：`App.cpp` 里那个巨大的 `switch(recv_packet.type)`（本讲只用了其中 3 个 case）就是 USB 协议的固件侧全貌，配套阅读 [Documentation/DeveloperInfo/USB_protocol_v12.tex](Documentation/DeveloperInfo/USB_protocol_v12.tex)。
- 想深挖 `copy_flash` 这类「RAM 中自刷写」技巧的读者，建议先补 STM32 内部 flash 擦写时序与 C 启动文件（`.data`/`.bss` 段加载）的基础知识，再回头看 [Firmware.cpp](Software/VNA_embedded/Application/Firmware.cpp)。
- FPGA 侧的 `top.vhd` 及 `.xise` 清单中列出的功能块，将在单元 6 逐块精读；在此之前保持「知道 bitstream 是怎么生成、怎么进 FPGA 的」即可。
