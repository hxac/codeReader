# 环境搭建与构建运行：Makefile、Vitis 与 XRT 工具链

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 uBench 官方要求的软件环境（Vitis 2020.2 + XRT 2020.2 + Ubuntu 18.04），以及构建系统实际检查哪些环境变量。
2. 逐条解释 `make all` / `build` / `exe` / `check` / `sd_card` / `clean` 各目标分别做什么。
3. 跟踪 `TARGET`（`sw_emu`/`hw_emu`/`hw`）与 `DEVICE` 两个命令行参数如何从 make 命令一路传递到 `v++` 命令、目录名和运行方式。
4. 在没有 Alveo/U280 真机的情况下，理解（并在装有 Vitis 的机器上跑通）`sw_emu` 软件仿真路径。
5. 手写一份「从 C++ 源码到 `ubench.xclbin` 再到运行 `ubench`」的完整命令流程说明。

本讲只讲**构建与运行系统**，不深入内核与主机代码本身——那是 u1-l4 和单元 2 的任务。

## 2. 前置知识

在阅读本讲之前，你需要了解以下概念（已学过 u1-l1、u1-l2 更好，没学过也能读懂）：

- **FPGA 加速的基本分工**：一个 Vitis 加速工程分为两部分——运行在 FPGA 上的**内核**（kernel，用 HLS C++ 写）和运行在 x86/ARM 主机上的**主机程序**（host，用 OpenCL API 写）。两者分别编译，最后由主机程序加载编译好的 FPGA 位流并启动内核。
- **`v++` 编译器**：Xilinx Vitis 的命令行驱动。它有两个核心子步骤：
  - `v++ -c`：把 HLS C++ 内核源码**编译**成 `.xo` 对象文件（综合成 RTL）；
  - `v++ -l`：把一个或多个 `.xo` **链接**成 `.xclbin`（含 FPGA 位流的容器文件），链接阶段还能通过 `--config` 指定内存连接关系。
- **XRT（Xilinx Runtime）**：主机侧的运行时库。主机程序链接 `-lOpenCL`，底层由 XRT 真正驱动 FPGA。所以主机编译需要 `XILINX_XRT` 指向 XRT 安装路径。
- **三种构建目标 `TARGET`**：
  - `sw_emu`：软件仿真。内核代码被编译成在主机 CPU 上跑的模型，**不综合 RTL**，速度最快，只验证功能正确性；
  - `hw_emu`：硬件仿真。真实综合 RTL 并在模拟器里跑，能看时序/握手行为，但很慢；
  - `hw`：真实位流，只能在插了对应 FPGA 卡的机器上运行。
  > 注意：`sw_emu`/`hw_emu` 下测出的「带宽」**没有物理意义**，仿真只用来验证数据通路和程序逻辑。
- **Makefile 基础**：目标（target）、依赖（prerequisite）、配方（recipe）三要素；`VAR := value` 是变量赋值；命令行上 `make VAR=xxx` 传入的变量**优先级高于** Makefile 内的赋值（这是 `make TARGET=sw_emu` 能覆盖默认值 `hw` 的原因）；`include` 可以把另一个 `.mk` 文件的内容拼进来。
- **x86 与交叉编译**：数据中心版 uBench 主机程序跑在 x86 服务器上；若目标平台是 ZCU104 这类 SoC，主机程序必须用 `aarch64-linux-gnu-g++` 交叉编译，并且需要目标根文件系统（sysroot）。

## 3. 本讲源码地图

本讲以 `read/DDR/2ports_512bit` 这个示例工程为主线（仓库中所有 datacenter 手写工程共用同一套模板，只差参数）：

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L1-L163) | 工程主构建脚本：定义 `all/build/check/sd_card/clean` 等目标、内核与主机的编译规则、仿真运行方式 |
| [utils.mk](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L1-L93)（工程内副本） | 环境守门与公共函数：检查 `XILINX_VITIS`/`XILINX_XRT`/`DEVICE`/`SYSROOT`、选择编译器、`device2xsa` 命名函数、`PROFILE`/`DEBUG` 开关 |
| [common/utils.mk](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utils.mk#L1-L93) | 仓库级公共版 `utils.mk`。**经 md5 校验，它与工程内那份逐字节相同**，但本工程 `include` 的是本地副本（见 4.1 的说明） |
| [common/includes/opencl/opencl.mk](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/opencl/opencl.mk#L1-L15) | 注入 OpenCL 头文件与库的编译/链接变量（`opencl_CXXFLAGS`、`opencl_LDFLAGS`） |
| [common/includes/xcl2/xcl2.mk](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.mk#L1-L4) | 把 `xcl2.cpp/hpp` 公共库追加进主机源码列表与头文件搜索路径 |
| [common/utility/parse_platform_list.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/parse_platform_list.py#L1-L13) | 在 `PLATFORM_REPO_PATHS` 环境变量里查找平台目录，被 `utils.mk` 调用 |

一句话概括分工：**Makefile 负责「做什么」，utils.mk 负责「能不能做」（环境检查与编译器选择），两个 `.mk` 片段负责「往主机编译命令里塞什么」**。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**环境变量检查** → **v++ 编译与链接目标** → **仿真运行路径**。它们正好对应一次 `make check TARGET=sw_emu` 从解析到运行的时间顺序。

### 4.1 环境变量检查：utils.mk 的守门逻辑

#### 4.1.1 概念说明

`utils.mk` 是从 Xilinx Vitis 示例仓库（Vitis_Example）继承来的公共片段，它解决的问题是：**在真正开始编译前，尽早发现环境没配好**。FPGA 编译动辄几十分钟到几小时，如果编到一半才发现缺 sysroot 或交叉编译器，代价太高。

它检查的东西分两类：

- **解析期检查**（make 读文件时就执行，任何 `make` 命令都会触发，包括 `make help`）；
- **目标期检查**（写成某个目标的配方，只有该目标被构建时才触发）。

另外，`utils.mk` 还负责三件与「环境」强相关的事：按 `HOST_ARCH` 选择编译器（x86 的 g++ / aarch64 / aarch32 交叉编译器）、把 `DEVICE` 名转换成目录名友好的 `XSA` 名、以及提供 `PROFILE`/`DEBUG` 两个可开关的链接选项。

**一个容易踩的坑**：这个工程目录里有一份 `utils.mk`，仓库根的 `common/` 下也有一份。经校验两者内容完全相同（md5 一致），但 [Makefile:37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L37) 写的是 `include ./utils.mk`——**生效的是工程内那份副本**。如果你修改 `common/utils.mk`，这些手写工程并不会受影响。

#### 4.1.2 核心流程

`make` 读取 Makefile → `include ./utils.mk` → **立刻执行解析期检查**：

```
读取 utils.mk
 ├─ XILINX_VITIS 未设置？ ──是──> $(error) 立即退出（连 make help 都会失败）
 ├─ HOST_ARCH 不是 aarch64/aarch32/x86 之一？ ──是──> $(error) 退出
 ├─ HOST_ARCH ≠ x86 且 SYSROOT 未设置？ ──是──> $(error) 退出
 └─ 通过 ──> 按 HOST_ARCH 选定 CXX（g++ / aarch64-linux-gnu-g++ / arm-linux-gnueabihf-g++）
             （x86 且 g++ < 5.0 时：报错，或退回用 XILINX_VIVADO 自带的 gcc 6.2.0）
```

之后的**目标期检查**（构建到相应目标时才执行）：

| 检查目标/时机 | 条件 | 失败动作 |
| --- | --- | --- |
| `check-devices`（`all` 的第一个依赖） | `DEVICE` 未设置 | 报错退出 |
| `check-xrt`（主机可执行文件的依赖） | `XILINX_XRT` 未设置 | 报错退出 |

#### 4.1.3 源码精读

**① 解析期就检查 `XILINX_VITIS`**——注意下面这段不在任何目标的配方里，而是位于 `utils.mk` 顶层，所以 make 一解析到这里就会执行：

[utils.mk:28-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L28-L31)

```make
#Checks for XILINX_VITIS
ifndef XILINX_VITIS
$(error XILINX_VITIS variable is not set, please set correctly and rerun)
endif
```

这就是为什么环境没 source Vitis 的 `settings64.sh`（该脚本会导出 `XILINX_VITIS`、`XILINX_VIVADO` 等变量；仓库 README 未写明此步骤，属于 Vitis 通用做法）时，连 `make help` 都会直接失败。

**② 目标期检查 `XILINX_XRT` 与 `HOST_ARCH`、`SYSROOT`**：

[utils.mk:40-56](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L40-L56)

```make
check-xrt:
ifndef XILINX_XRT
	$(error XILINX_XRT variable is not set, please set correctly and rerun)
endif

#Checks for Correct architecture
ifneq ($(HOST_ARCH), $(filter $(HOST_ARCH),aarch64 aarch32 x86))
$(error HOST_ARCH variable not set, please set correctly and rerun)
endif

#Checks for SYSROOT
ifneq ($(HOST_ARCH), x86)
ifndef SYSROOT
$(error SYSROOT variable is not set, please set correctly and rerun)
endif
endif
```

三个要点：
- `check-xrt` 是一个**目标**，`ifndef` 是 make 的条件指令、在解析配方时求值；它在 [Makefile:103](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L103) 被列为 `$(EXECUTABLE)`（主机程序）的依赖，所以编译主机程序前必然先过这道门。
- `filter` 的用法：`$(filter $(HOST_ARCH),aarch64 aarch32 x86)` 从合法列表里挑出等于 `HOST_ARCH` 的词，空则说明非法。
- **sysroot 只对 SoC 平台强制**：x86 主机程序直接用本机 g++ 编译；一旦 `HOST_ARCH=aarch64/aarch32`，就必须提供目标板的根文件系统路径，供交叉编译器找头文件和库。

**③ 按 `HOST_ARCH` 选择编译器（含 x86 的 g++ 版本兜底）**：

[utils.mk:58-72](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L58-L72)

```make
ifeq ($(HOST_ARCH), x86)
ifneq ($(shell expr $(shell g++ -dumpversion) \>= 5), 1)
ifndef XILINX_VIVADO
$(error [ERROR]: g++ version older. Please use 5.0 or above.)
else
CXX := $(XILINX_VIVADO)/tps/lnx64/gcc-6.2.0/bin/g++
$(warning [WARNING]: g++ version older. Using g++ provided by the tool : $(CXX))
endif
endif
else ifeq ($(HOST_ARCH), aarch64)
CXX := $(XILINX_VITIS)/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-g++
else ifeq ($(HOST_ARCH), aarch32)
CXX := $(XILINX_VITIS)/gnu/aarch32/lin/gcc-arm-linux-gnueabi/bin/arm-linux-gnueabihf-g++
endif
```

x86 分支用 `g++ -dumpversion` + `expr` 做版本比较：低于 5.0 时，若装了 Vivado 就借用其自带的 gcc 6.2.0，否则报错。本讲的 datacenter 示例走 x86 分支；两个交叉分支服务于 ZCU104 类平台（详见 u4-l3）。

**④ `DEVICE` 未设置的检查**：

[utils.mk:74-77](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L74-L77)

```make
check-devices:
ifndef DEVICE
	$(error DEVICE not set. Please set the DEVICE properly and rerun. Run "make help" for more details.)
endif
```

`check-devices` 是 `all` 目标的第一个依赖（[Makefile:86](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L86)），保证忘了传 `DEVICE=` 时不会编到一半才挂。

**⑤ `device2xsa`：把平台名变成目录名**：

[utils.mk:79-81](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L79-L81)

```make
#   device2xsa - create a filesystem friendly name from device name
#   $(1) - full name of device
device2xsa = $(strip $(patsubst %.xpfm, % , $(shell basename $(DEVICE))))
```

它取 `DEVICE` 的 basename，用 `patsubst` 去掉 `.xpfm` 后缀再去掉首尾空格。例如 `DEVICE=xilinx_u200_xdma_201830_2` → `XSA=xilinx_u200_xdma_201830_2`；`DEVICE=/opt/xilinx/platforms/xilinx_u280_xdma_201910_3/xilinx_u280_xdma_201910_3.xpfm` → `XSA=xilinx_u280_xdma_201910_3`。这个值会进入构建目录名（见 4.2.3 ①）。

**⑥ 平台路径解析与 `PLATFORM_REPO_PATHS`**：

[utils.mk:13-26](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L13-L26)

```make
B_TEMP = `$(ABS_COMMON_REPO)/common/utility/parse_platform_list.py $(DEVICE)`

#Setting Platform Path
ifeq ($(findstring xpfm, $(DEVICE)), xpfm)
	B_NAME = $(shell dirname $(DEVICE))
else
	B_NAME = $(B_TEMP)/$(DEVICE)
endif
```

`DEVICE` 有两种给法：完整 `.xpfm` 路径（则平台目录就是其 dirname），或短名（则在 `PLATFORM_REPO_PATHS` 列出的目录里找）。短名查找由 [parse_platform_list.py:5-13](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/parse_platform_list.py#L5-L13) 完成——它遍历该环境变量的每个路径，找到「路径/设备名」是目录的那一项并打印：

```python
def main ():
    dev = sys.argv[1]
    if "PLATFORM_REPO_PATHS" in os.environ:
        plist = os.environ['PLATFORM_REPO_PATHS'].split(":")
        for shell in plist:
            if os.path.isdir(shell + "/" + dev):
                return shell
```

`B_NAME` 只在 `sd_card` 目标里用到（见 4.3.3 ④），x86 数据中心流程不依赖它。

**⑦ `PROFILE` / `DEBUG` 两个开关**：

[utils.mk:5-19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L5-L19)

```make
PROFILE := no

#Generates profile summary report
ifeq ($(PROFILE), yes)
LDCLFLAGS += --profile_kernel data:all:all:all
endif

DEBUG := no

#Generates debug summary report
ifeq ($(DEBUG), yes)
LDCLFLAGS += --dk list_ports
endif
```

`make PROFILE=yes` 会给**链接**命令追加 `--profile_kernel data:all:all:all`，让 XRT 运行时生成 `profile_summary.csv` 性能报告（与 4.3.3 ③ 的 `perf_analyze` 呼应，是 u7-l3 测量方法学的伏笔）。`DEBUG=yes` 则给所有 AXI 端口插调试核。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「环境守门」在不同缺失条件下的报错顺序，理解解析期与目标期检查的区别。

**操作步骤**：

1. 进入示例工程目录：
   ```bash
   cd ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit
   ```
2. 在**未 source Vitis 环境**（即 `XILINX_VITIS`、`XILINX_XRT` 均未设置）的终端里执行：
   ```bash
   make help
   ```
3. 只设置 `XILINX_VITIS` 再试：
   ```bash
   XILINX_VITIS=/opt/xilinx/Vitis/2020.2 make help
   ```
4. 单独运行平台查找脚本，观察短名解析：
   ```bash
   python3 ../../../../../common/utility/parse_platform_list.py xilinx_u200_xdma_201830_2
   echo "PLATFORM_REPO_PATHS=$PLATFORM_REPO_PATHS"
   ```

**需要观察的现象**：

- 步骤 2：`make` 在打印任何 help 文本之前就报 `XILINX_VITIS variable is not set, please set correctly and rerun` 并退出——证明这是解析期错误，`help` 目标根本没机会执行。
- 步骤 3：help 文本能正常打印（它只依赖解析通过）；此时若继续 `make check TARGET=sw_emu DEVICE=...`，会在构建主机程序的 `check-xrt` 依赖处报 `XILINX_XRT ... is not set`。
- 步骤 4：环境里没有 `PLATFORM_REPO_PATHS` 时脚本输出 `None`；设置了且目录存在时输出对应的 shell 路径。

**预期结果**：能画出 4.1.2 的检查流程图，并说出每个错误分别在哪一行被触发。

> 本环境（讲义编写环境）未安装 Vitis 且沙箱限制无法执行 make/python，以上现象由源码逐行推导得出，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make help` 在没设置 `XILINX_VITIS` 时也会失败，而 `DEVICE` 没设置时 `make help` 却能成功？

**答案**：`XILINX_VITIS` 的检查写在 `utils.mk` 顶层（不在任何目标里），属于解析期求值，make 读到该行立即 `$(error)` 退出；`DEVICE` 的检查写在 `check-devices` 目标的配方里，只有该目标被构建（`all` 的依赖）时才触发，`make help` 不会构建它。

**练习 2**：交叉编译 ZCU104 版本时，`make` 命令需要额外提供哪两个变量？为什么 x86 不需要？

**答案**：`HOST_ARCH=aarch64`（或 aarch32）和 `SYSROOT=<路径>`。因为 SoC 的主机程序要跑在 ARM 上，必须用 Vitis 自带的 `aarch64-linux-gnu-g++` 交叉编译，链接时用 `--sysroot` 指向目标板根文件系统才能找到正确的 glibc/OpenCL 头文件与库；x86 主机程序就在本机运行，直接用本机 g++，无需 sysroot。

**练习 3**：`device2xsa` 输入 `/ platforms/xilinx_u280_xdma_201910_3.xpfm`（basename 为 `xilinx_u280_xdma_201910_3.xpfm`），输出是什么？这个输出用在哪里？

**答案**：输出 `xilinx_u280_xdma_201910_3`（去掉路径、去掉 `.xpfm` 后缀、去首尾空格）。它被 [Makefile:39-41](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L39-L41) 用来拼出 `TEMP_DIR=./_x.$(TARGET).$(XSA)` 与 `BUILD_DIR=./build_dir.$(TARGET).$(XSA)`，让不同平台/目标的产物互不覆盖。

### 4.2 v++ 编译与链接：从 HLS 源码到 xclbin

#### 4.2.1 概念说明

一个 uBench 工程要产出**两个可运行的东西**：

1. `ubench.xclbin`——FPGA 侧镜像，由内核源码 `src/krnl_ubench.cpp` 经「编译 → 链接」两步生成；
2. `ubench`——主机侧可执行文件，由 `src/host.cpp` 加上仓库公共库 `xcl2.cpp` 一起用 g++ 编译。

Makefile 用 GNU make 的**隐式规则变量约定**组织这件事：`CLFLAGS`（内核编译公共参数）、`LDCLFLAGS`（内核链接专属参数）、`CXXFLAGS`/`LDFLAGS`（主机编译/链接参数）、`HOST_SRCS`（主机源码清单）。两个公共 `.mk` 片段（opencl.mk、xcl2.mk）以「追加变量」的方式把 OpenCL 头文件/库和 xcl2 库注入进来——这是 Xilinx 示例仓库的标准组件复用模式：**主 Makefile 只写工程特有的部分，公共路径全部由片段注入**。

其中最容易迷惑的是变量叠加顺序：`CXXFLAGS` 在主 Makefile 里被 opencl、xcl2、pthread、警告级别等**多次 `+=`**，最终一次性传给 g++。

#### 4.2.2 核心流程

```
src/krnl_ubench.cpp ──(v++ -c, CLFLAGS)──> _x.$(TARGET).$(XSA)/krnl_ubench.xo
                                              │
              ubench.ini ──(--config)─────────┤
                                              v
                          (v++ -l, CLFLAGS+LDCLFLAGS)
              build_dir.$(TARGET).$(XSA)/ubench.xclbin

src/host.cpp ─┐
src/krnl_config.h（以源码形式列入依赖）─┤──(g++, CXXFLAGS+LDFLAGS)──> ./ubench
common/.../xcl2.cpp（由 xcl2.mk 注入）─┘
```

注意三条编译链**互相独立**：改内核源码只重建 `.xo`→`.xclbin`，改主机源码只重建 `ubench`（但依赖里有 `krnl_config.h`，所以改配置头会触发主机重编）。

#### 4.2.3 源码精读

**① 构建目录与工具名**：

[Makefile:39-44](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L39-L44)

```make
XSA := $(call device2xsa, $(DEVICE))
TEMP_DIR := ./_x.$(TARGET).$(XSA)
BUILD_DIR := ./build_dir.$(TARGET).$(XSA)

VPP := v++
```

`TEMP_DIR` 存编译中间产物（`.xo`），`BUILD_DIR` 存链接产物（`.xclbin`）。目录名里带 `TARGET` 和 `XSA`，意味着同一目录下 `sw_emu` 与 `hw`、U200 与 U280 的产物会分开存放，切换目标不会互相污染。`VPP := v++` 只是把编译器命令抽象成变量。

**② 公共片段注入主机编译变量**：

[Makefile:46-59](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L46-L59)

```make
#Include Libraries
include $(ABS_COMMON_REPO)/common/includes/opencl/opencl.mk
include $(ABS_COMMON_REPO)/common/includes/xcl2/xcl2.mk
CXXFLAGS += $(xcl2_CXXFLAGS)
LDFLAGS += $(xcl2_LDFLAGS)
HOST_SRCS += $(xcl2_SRCS)
CXXFLAGS += -pthread
CXXFLAGS += $(opencl_CXXFLAGS) -Wall -O0 -g -std=c++11
LDFLAGS += $(opencl_LDFLAGS)

HOST_SRCS += src/host.cpp src/krnl_config.h
# Host compiler global settings
CXXFLAGS += -fmessage-length=0
LDFLAGS += -lrt -lstdc++
```

注意 `ABS_COMMON_REPO` 来自 [Makefile:28-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L28-L31)：

```make
# Points to top directory of Git repository
COMMON_REPO = ../../../../../
PWD = $(shell readlink -f .)
ABS_COMMON_REPO = $(shell readlink -f $(COMMON_REPO))
```

`COMMON_REPO = ../../../../../`——从 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit` 往上数正好五级回到仓库根。这解释了 u1-l2 讲过的约定：**把工程目录挪位置或改层级深度，make 会找不到 `common/`**。另外可看到主机程序用 `-O0 -g` 编译（未优化，u7-l3 会讨论其对测量的影响），并链接 `-lrt`（host.cpp 用 `clock_gettime` 计时）和 `-lstdc++`。

两个片段本体非常短。[xcl2.mk:1-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.mk#L1-L4)：

```make
xcl2_SRCS:=${COMMON_REPO}/common/includes/xcl2/xcl2.cpp
xcl2_HDRS:=${COMMON_REPO}/common/includes/xcl2/xcl2.hpp

xcl2_CXXFLAGS:=-I${COMMON_REPO}/common/includes/xcl2
```

它定义三个变量：把 `xcl2.cpp` 加进源码清单、把头文件目录加进搜索路径（`xcl2_LDFLAGS` 未定义，主 Makefile 里 `LDFLAGS += $(xcl2_LDFLAGS)` 实际追加的是空串）。[opencl.mk:1-15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/opencl/opencl.mk#L1-L15)：

```make
xrt_path = $(XILINX_XRT)
ifneq ($(HOST_ARCH), x86)
	xrt_path =  $(SYSROOT)/usr/
endif

OPENCL_INCLUDE:= $(xrt_path)/include
ifneq ($(HOST_ARCH), x86)
	OPENCL_INCLUDE:= $(xrt_path)/include/xrt
endif

VIVADO_INCLUDE:= $(XILINX_VIVADO)/include
opencl_CXXFLAGS=-I$(OPENCL_INCLUDE) -I$(VIVADO_INCLUDE)
OPENCL_LIB:= $(xrt_path)/lib
opencl_LDFLAGS=-L$(OPENCL_LIB) -lOpenCL -lpthread
```

注意它的平台分支：x86 时 OpenCL 头文件在 `$XILINX_XRT/include`；交叉编译时头文件在 sysroot 里的 `/usr/include/xrt`，库在 sysroot 的 `/usr/lib`——这就是 4.1 中强制 `SYSROOT` 的直接消费者。

**③ 内核编译参数 `CLFLAGS` 与链接参数 `LDCLFLAGS`**：

[Makefile:65-73](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L65-L73)

```make
# Kernel compiler global settings
CLFLAGS += -t $(TARGET) --platform $(DEVICE) --save-temps 
ifneq ($(TARGET), hw)
	CLFLAGS += -g
endif


# Kernel linker flags
LDCLFLAGS += --config ./ubench.ini
```

这就是 `TARGET` 与 `DEVICE` 的**传递终点**之一：命令行上的 `make TARGET=sw_emu DEVICE=xxx` → Makefile 变量 → `v++ -t sw_emu --platform xxx`。`-t` 选仿真档位，非 `hw` 时加 `-g` 方便调试；`--save-temps` 保留中间文件。链接专属的 `--config ./ubench.ini` 把内存连接关系（`sp`/`slr`/`nk` 三条指令，u3-l3 精读）交给链接器。

> ⚠️ 一个文档与代码不一致之处：datacenter 版 [README 第 6 节](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L74-L79) 示例的 `CLFLAGS` 里有 `--kernel_frequency 300`，但当前 HEAD 的示例 Makefile（上面的第 66 行）**没有**这一项——想固定 300MHz 需要按 README 手动加上。承接 u1-l2 的结论：README 滞后处以 `src/` 与构建脚本为准（同一份 README 还把配置头写成 `krnl_ubench.h`，实际文件是 `src/krnl_config.h`）。

**④ 产物声明**：

[Makefile:75-81](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L75-L81)

```make
EXECUTABLE = ubench
CMD_ARGS = $(BUILD_DIR)/ubench.xclbin
EMCONFIG_DIR = $(TEMP_DIR)
EMU_DIR = $(SDCARD)/data/emulation

BINARY_CONTAINERS += $(BUILD_DIR)/ubench.xclbin
BINARY_CONTAINER_ubench_OBJS += $(TEMP_DIR)/krnl_ubench.xo
```

`BINARY_CONTAINERS` 声明要产出的 xclbin 容器，`BINARY_CONTAINER_ubench_OBJS` 声明容器 `ubench` 由哪些 `.xo` 组成——命名约定是 `BINARY_CONTAINER_<容器名>_OBJS`。本工程只有一个内核一份对象，多内核工程（如 KNN 的 14 个 PE）会在这里列出更多 `.xo`。

**⑤ 两条 v++ 规则——本模块的核心**：

[Makefile:94-100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L94-L100)

```make
# Building kernel
$(TEMP_DIR)/krnl_ubench.xo: src/krnl_ubench.cpp
	mkdir -p $(TEMP_DIR)
	$(VPP) $(CLFLAGS) --temp_dir $(TEMP_DIR) -c -k krnl_ubench -I'$(<D)' -o'$@' '$<'
$(BUILD_DIR)/ubench.xclbin: $(BINARY_CONTAINER_ubench_OBJS)
	mkdir -p $(BUILD_DIR)
	$(VPP) $(CLFLAGS) --temp_dir $(BUILD_DIR) -l $(LDCLFLAGS) -o'$@' $(+)
```

展开后（以 `TARGET=hw`、U200 为例）：

```bash
v++ -t hw --platform xilinx_u200_xdma_201830_2 --save-temps \
    --temp_dir ./_x.hw.xilinx_u200_xdma_201830_2 \
    -c -k krnl_ubench -I'src' \
    -o'_x.hw.xilinx_u200_xdma_201830_2/krnl_ubench.xo' 'src/krnl_ubench.cpp'

v++ -t hw --platform xilinx_u200_xdma_201830_2 --save-temps \
    --temp_dir ./build_dir.hw.xilinx_u200_xdma_201830_2 \
    -l --config ./ubench.ini \
    -o'build_dir.hw.xilinx_u200_xdma_201830_2/ubench.xclbin' ...  # $(+) = 全部 .xo
```

要点：`-c` 编译 / `-l` 链接；`-k krnl_ubench` 指明顶层内核函数名；`-I'$(<D)'` 把**依赖文件所在目录**（即 `src/`）加进头文件搜索路径，所以 `krnl_ubench.cpp` 才能 `#include "krnl_config.h"`；自动变量 `$@`=目标、`$<`=第一个依赖、`$(+)`=当前容器全部对象。第二条规则 `TARGET=sw_emu` 时展开成 `v++ -t sw_emu -g ...`，做的是软件仿真模型而非 RTL。

**⑥ 主机程序规则**：

[Makefile:102-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L102-L104)

```make
# Building Host
$(EXECUTABLE): check-xrt $(HOST_SRCS) $(HOST_HDRS)
	$(CXX) $(CXXFLAGS) $(HOST_SRCS) $(HOST_HDRS) -o '$@' $(LDFLAGS)
```

依赖里的 `check-xrt` 把 4.1 的环境检查挂进主机构建；`HOST_SRCS` 此时等于 `common/.../xcl2.cpp src/host.cpp src/krnl_config.h`（头文件列入源码清单，改配置头必触发重编）。交叉编译时 `LDFLAGS` 还会被 [Makefile:61-63](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L61-L63) 追加 `--sysroot=$(SYSROOT)`。

#### 4.2.4 代码实践

**实践目标**：不执行任何编译，仅用 make 的干跑模式拿到完整命令序列——这是无 Vitis 环境下读懂构建流程最有效的手段。

**操作步骤**：

1. 进入 `read/DDR/2ports_512bit` 目录。
2. 干跑内核链接目标（`-n` 只打印命令不执行；`XILINX_VITIS/XILINX_XRT` 设成占位值以通过 4.1 的解析期检查）：
   ```bash
   XILINX_VITIS=/tmp XILINX_XRT=/tmp \
     make -n build TARGET=sw_emu DEVICE=xilinx_u200_xdma_201830_2
   ```
3. 再干跑主机目标：
   ```bash
   XILINX_VITIS=/tmp XILINX_XRT=/tmp \
     make -n exe TARGET=hw DEVICE=xilinx_u200_xdma_201830_2
   ```
4. 把打印出的两条 `v++` 命令和一条 `g++` 命令抄下来，逐个参数标注含义（对照 4.2.3 的展开示例）。

**需要观察的现象**：

- `make -n build TARGET=sw_emu ...` 打印的 `v++` 命令里带 `-t sw_emu -g`，且输出路径形如 `_x.sw_emu.xilinx_u200_xdma_201830_2/krnl_ubench.xo`；
- `make -n exe TARGET=hw ...` 打印一条很长的 `g++` 命令，其中能看到 `-I/tmp/include`（opencl.mk 注入）、`-I.../common/includes/xcl2`（xcl2.mk 注入）、`-O0 -g -std=c++11`、`-lOpenCL -lrt`；
- 换 `TARGET=hw` 干跑 build，命令里 `-g` 消失、目录名变成 `build_dir.hw.<XSA>`。

**预期结果**：得到一张「目标 → 实际命令 → 关键参数来源」的对照表（即综合实践的半成品）。

> 占位路径只用于通过检查，`-n` 模式不会真正调用 v++/g++，因此不会因 `/tmp` 下没有工具链而失败。本沙箱无法执行 make，以上输出为按规则推导，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HOST_SRCS` 里要放一个头文件 `src/krnl_config.h`？

**答案**：make 依赖列表决定重建时机。主机程序 `#include "krnl_config.h"`（其中的 `DWIDTH` 等宏同时驱动主机与内核），把它列进 `HOST_SRCS` 后，只要改动配置头，`./ubench` 就会被重新编译，避免「改了参数还跑旧二进制」的测量事故。

**练习 2**：`make build TARGET=sw_emu` 和 `make build TARGET=hw` 产出的 `.xclbin` 放在同一个文件里吗？为什么这样设计？

**答案**：不会。产物路径分别是 `build_dir.sw_emu.<XSA>/ubench.xclbin` 与 `build_dir.hw.<XSA>/ubench.xclbin`——目录名由 [Makefile:40-41](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L40-L41) 用 `$(TARGET).$(XSA)` 拼出，使不同目标/平台的产物隔离，切换 TARGET 无需 clean。

**练习 3**：`xcl2.mk` 定义了 `xcl2_SRCS/CXXFLAGS/LDFLAGS` 三个变量，但主 Makefile 里 `LDFLAGS += $(xcl2_LDFLAGS)` 加的是什么？

**答案**：加的是空串——xcl2.mk 并未定义 `xcl2_LDFLAGS`（xcl2 是纯源码库，无专属链接项）。这行是模板为兼容可能带链接需求的库片段而保留的统一注入点。

### 4.3 仿真运行路径：check、emconfig 与 sd_card

#### 4.3.1 概念说明

构建出 `ubench`（主机）与 `ubench.xclbin`（FPGA 镜像）之后，还差「怎么跑起来」。Vitis 加速工程的运行有一套固定仪式：

- 主机程序命令行参数是 xclbin 路径（本工程 `CMD_ARGS = $(BUILD_DIR)/ubench.xclbin`）；
- **仿真模式**下，XRT 靠两个东西找到「假设备」：当前目录的 `emconfig.json`（描述仿真的平台/内存拓扑，由 `emconfigutil` 生成）和环境变量 `XCL_EMULATION_MODE`（告诉 XRT 当前是 `sw_emu` 还是 `hw_emu`）；
- **真机模式**（`hw`）下什么都不用设，XRT 直接扫描 PCIe 上的 Alveo 卡。

Makefile 的 `check` 目标把「构建 + 运行」串成一条命令，并按 `TARGET` × `HOST_ARCH` 二维分支选择运行方式；`sd_card` 目标则是给 SoC 平台打包 SD 卡启动内容用的。

#### 4.3.2 核心流程

`make check TARGET=sw_emu DEVICE=<平台>` 触发的完整决策树：

```
check ──> all ──> check-devices          （DEVICE 设了吗？）
              ├──> ubench                （g++ 编译主机，先过 check-xrt）
              ├──> build_dir.../ubench.xclbin （v++ -c 再 v++ -l）
              ├──> emconfig              （emconfigutil 生成 emconfig.json）
              └──> sd_card               （x86 下配方体为空，仅触发上述依赖）
然后按 TARGET 分支运行：
  TARGET ∈ {sw_emu, hw_emu}
      └─ HOST_ARCH=x86：cp emconfig.json . ；XCL_EMULATION_MODE=<TARGET> ./ubench <xclbin>
      └─ 否则（SoC）：打包 qemu 仿真镜像，launch_emulator 启动
  TARGET = hw
      └─ HOST_ARCH=x86：./ubench <xclbin>（直接上真机）
  最后（x86）：perf_analyze profile -i profile_summary.csv -f html
```

#### 4.3.3 源码精读

**① 三个默认变量与 help 文本**：

[Makefile:33-37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L33-L37)

```make
TARGET := hw
HOST_ARCH := x86
SYSROOT := 

include ./utils.mk
```

默认目标是 `hw`、x86 主机。GNU make 规则：命令行变量覆盖文件内赋值，所以 `make TARGET=sw_emu` 有效。[help 目标](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L1-L26)（`make help` 打印）列出了六个入口的用法，汇总成表：

| make 目标 | 作用 | 典型命令 |
| --- | --- | --- |
| `all` | 依次完成 check-devices → 主机程序 → xclbin → emconfig → sd_card | `make all TARGET=hw DEVICE=<平台>` |
| `build` | 只构建 xclbin（内核编译+链接） | `make build TARGET=sw_emu DEVICE=<平台>` |
| `exe` | 只构建主机可执行文件 | `make exe` |
| `check` | `all` 之后自动**运行**程序（仿真或真机），x86 下再跑 perf_analyze | `make check TARGET=sw_emu DEVICE=<平台>` |
| `sd_card` | 为 SoC 平台打包 SD 卡内容（x86 下为空操作） | `make sd_card TARGET=hw HOST_ARCH=aarch64 SYSROOT=...` |
| `clean` / `cleanall` | 清理生成物 / 连构建目录一起清 | `make cleanall` |

**② `all` 与其依赖链**：

[Makefile:85-92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L85-L92)

```make
.PHONY: all clean cleanall docs emconfig
all: check-devices $(EXECUTABLE) $(BINARY_CONTAINERS) emconfig sd_card

.PHONY: exe
exe: $(EXECUTABLE)

.PHONY: build
build: $(BINARY_CONTAINERS)
```

`all` 的五个依赖就是 4.3.2 决策树的前半段。`check: all`（下文）意味着 **`make check` 包含完整构建**——第一次调用它会自动编译一切。

**③ emconfig 与 check 的运行分支**：

[Makefile:106-135](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L106-L135)

```make
emconfig:$(EMCONFIG_DIR)/emconfig.json
$(EMCONFIG_DIR)/emconfig.json:
	emconfigutil --platform $(DEVICE) --od $(EMCONFIG_DIR)

check: all
ifeq ($(findstring samsung, $(DEVICE)), samsung)
$(error This example is not supported for $(DEVICE))
endif
ifeq ($(findstring zc, $(DEVICE)), zc)
$(error This example is not supported for $(DEVICE))
endif

ifeq ($(TARGET),$(filter $(TARGET),sw_emu hw_emu))
ifeq ($(HOST_ARCH), x86)
	$(CP) $(EMCONFIG_DIR)/emconfig.json .
	XCL_EMULATION_MODE=$(TARGET) ./$(EXECUTABLE) $(BUILD_DIR)/ubench.xclbin
else
	mkdir -p $(EMU_DIR)
	$(CP) $(XILINX_VITIS)/data/emulation/unified $(EMU_DIR)
	mkfatimg $(SDCARD) $(SDCARD).img 500000
	launch_emulator -no-reboot -runtime ocl -t $(TARGET) -sd-card-image $(SDCARD).img -device-family $(DEV_FAM)
endif
else
ifeq ($(HOST_ARCH), x86)
	./$(EXECUTABLE) $(BUILD_DIR)/ubench.xclbin
endif
endif
ifeq ($(HOST_ARCH), x86)
	perf_analyze profile -i profile_summary.csv -f html
endif
```

逐段解读：

- **emconfig**：`emconfigutil --platform $(DEVICE) --od $(TEMP_DIR)` 为指定平台生成 `emconfig.json`（仿真内存拓扑描述），输出到 `_x.<TARGET>.<XSA>/` 下。
- **平台白名单**：`findstring samsung/zc` 两段解析期检查把三星与 Zynq（`zc` 开头，如 zcu104）平台直接挡掉——这是 **datacenter 专用工程**，嵌入式版本在 `ubench/*/embedded/` 另有一套（u4-l3）。
- **仿真 × x86 分支（本讲主路径）**：先把 `emconfig.json` 复制到当前目录（XRT 在主机可执行文件旁边找它），再用 `XCL_EMULATION_MODE=sw_emu` 前缀启动 `./ubench build_dir.sw_emu.<XSA>/ubench.xclbin`。
- **仿真 × SoC 分支**：把 Vitis 的 unified 仿真数据拷进 `sd_card/data/emulation`，`mkfatimg` 做 FAT 镜像，`launch_emulator` 起 QEMU 全系统仿真（`DEV_FAM` 来自 utils.mk 的 `Ultrascale`/`7Series`）。
- **真机分支**：`TARGET=hw` 且 x86 时直接 `./ubench <xclbin>`，XRT 自动发现 PCIe 上的卡。
- **perf_analyze**：x86 跑完后无条件尝试把 `profile_summary.csv` 转成 HTML 报告。注意该 CSV 只有 `PROFILE=yes` 链接（utils.mk 的 `--profile_kernel`）并运行后才存在；否则这行会因找不到文件而报错——**具体报错形态待本地验证**。

**④ `sd_card`：SoC 打包目标**：

[Makefile:137-152](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L137-L152)

```make
sd_card: $(EXECUTABLE) $(BINARY_CONTAINERS) emconfig
ifneq ($(HOST_ARCH), x86)
	mkdir -p $(SDCARD)/$(BUILD_DIR)
	$(CP) $(B_NAME)/sw/$(XSA)/boot/generic.readme $(B_NAME)/sw/$(XSA)/xrt/image/* xrt.ini $(EXECUTABLE) $(SDCARD)
	$(CP) $(BUILD_DIR)/*.xclbin $(SDCARD)/$(BUILD_DIR)/
ifeq ($(TARGET),$(filter $(TARGET),sw_emu hw_emu))
	$(ECHO) 'cd /mnt/' >> $(SDCARD)/init.sh
	$(ECHO) 'export XILINX_VITIS=$$PWD' >> $(SDCARD)/init.sh
	$(ECHO) 'export XCL_EMULATION_MODE=$(TARGET)' >> $(SDCARD)/init.sh
	$(ECHO) './$(EXECUTABLE) $(CMD_ARGS)' >> $(SDCARD)/init.sh
	$(ECHO) 'reboot' >> $(SDCARD)/init.sh
else
	[ -f $(SDCARD)/BOOT.BIN ] && echo "INFO: BOOT.BIN already exists" || $(CP) $(BUILD_DIR)/sd_card/BOOT.BIN $(SDCARD)/
	$(ECHO) './$(EXECUTABLE) $(CMD_ARGS)' >> $(SDCARD)/init.sh
endif
endif
```

整个配方体包在 `ifneq ($(HOST_ARCH), x86)` 里——**x86 下 `sd_card` 是纯空目标**（只剩依赖会触发），这就是 `make all` 在服务器上不会去打包 SD 卡的原因。SoC 分支把平台 boot 文件、XRT 镜像、`xrt.ini`、可执行文件、xclbin 拷进 `sd_card/`，并生成板上开机自运行的 `init.sh`（仿真模式还要导出 `XCL_EMULATION_MODE`；真机模式则先拷 `BOOT.BIN`）。

> 细节佐证「datacenter 示例不面向 SD 卡路径」：本目录下**并没有 `xrt.ini`**（`ubench.ini` ≠ `xrt.ini`，前者是连接配置、后者是 XRT 运行时配置），上述 `$(CP) ... xrt.ini ...` 在此工程中会失败；`xrt.ini` 是 embedded 工程（如 `ubench/offchip_latency/embedded/32bit_per_access/xrt.ini`）才带的文件，u4-l3 再展开。

**⑤ 清理目标**：

[Makefile:154-162](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L154-L162)

```make
clean:
	-$(RMDIR) $(EXECUTABLE) $(XCLBIN)/{*sw_emu*,*hw_emu*} 
	-$(RMDIR) profile_* TempConfig system_estimate.xtxt *.rpt *.csv 
	-$(RMDIR) src/*.ll *v++* .Xil emconfig.json dltmp* xmltmp* *.log *.jou *.wcfg *.wdb

cleanall: clean
	-$(RMDIR) build_dir* sd_card*
	-$(RMDIR) _x.* *xclbin.run_summary qemu-memory-_* emulation/ _vimage/ pl* start_simulation.sh *.xclbin
```

`clean` 清运行垃圾（报告、日志、emconfig.json 副本），`cleanall` 连 `build_dir*`、`_x.*` 构建目录一起删。切换 `TARGET` 不需要 clean（产物分目录），但切换后建议 `make clean` 清掉旧的 `profile_*`/`emconfig.json`。

#### 4.3.4 代码实践

**实践目标**：在装有 Vitis 2020.2 的机器上跑通 `sw_emu` 仿真，理解 `XCL_EMULATION_MODE` 与 `emconfig.json` 的作用。

**操作步骤**：

1. 配置环境（Vitis 通用做法，仓库 README 未写明此步）：
   ```bash
   source /opt/xilinx/Vitis/2020.2/settings64.sh
   ```
   确认 `echo $XILINX_VITIS $XILINX_XRT` 均非空。
2. 进入示例目录并执行一条命令完成「构建+仿真运行」：
   ```bash
   cd ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit
   make check TARGET=sw_emu DEVICE=xilinx_u200_xdma_201830_2
   ```
3. 观察终端：先是 v++/g++ 的编译输出，随后主机程序打印各 `payload` 档位下的带宽。
4. 运行结束后查看工作目录变化：`ls emconfig.json _x.sw_emu.*/ build_dir.sw_emu.*/`。
5. 试着不经过 make 直接手动复现运行：
   ```bash
   cp _x.sw_emu.xilinx_u200_xdma_201830_2/emconfig.json .
   XCL_EMULATION_MODE=sw_emu ./ubench build_dir.sw_emu.xilinx_u200_xdma_201830_2/ubench.xclbin
   ```

**需要观察的现象**：

- 步骤 3：程序按 payload 从小到大打印测试结果（1KB→1MB 共 11 档，u2-l3 精读该循环）；
- 步骤 4：当前目录多了 `emconfig.json`（check 从 `TEMP_DIR` 拷来）；`_x.sw_emu.<XSA>/` 里有 `krnl_ubench.xo` 与 `emconfig.json` 原件；`build_dir.sw_emu.<XSA>/` 里有 `ubench.xclbin`；
- 步骤 5：手动方式与 `make check` 的运行结果一致——证明 make 做的只是「拷 json + 设环境变量 + 执行」三件事；
- **关键认知**：`sw_emu` 打印的「带宽」数值不反映真实 FPGA 内存系统（内核跑在主机 CPU 模型上），它只验证「主机能加载 xclbin、参数能传进内核、数据能读回来」这条功能链路。

**预期结果**：得到一条可复现的仿真运行路径，并能说出 `TARGET=sw_emu` 从 make 变量到 `XCL_EMULATION_MODE` 环境变量的完整传递链：`make TARGET=sw_emu` → `CLFLAGS += -t $(TARGET)`（构建软件仿真模型）→ check 配方 `XCL_EMULATION_MODE=$(TARGET) ./ubench ...`（XRT 按软件仿真模式加载）。

> 本讲义编写环境无 Vitis 且沙箱禁止执行 make，以上步骤与现象由 Makefile 源码逐行推导，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`make check TARGET=sw_emu` 运行时，XRT 依据什么知道这是一次软件仿真？`emconfig.json` 又是干什么用的？

**答案**：依据环境变量 `XCL_EMULATION_MODE=sw_emu`（[Makefile:121](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L121) 设置）；`emconfig.json` 由 `emconfigutil --platform $(DEVICE)` 生成，描述被仿真平台的内存拓扑等信息，XRT 要求它位于主机可执行文件所在/当前目录，所以 check 先执行 `cp $(EMCONFIG_DIR)/emconfig.json .`。

**练习 2**：把 `DEVICE` 设成 `xilinx_zcu104_base_202020_1` 去跑这个 datacenter 工程，会发生什么？为什么？

**答案**：make 在解析 `check` 时命中 `findstring zc` 分支，直接 `$(error This example is not supported for ...)` 退出。因为该工程面向 Alveo 数据中心卡（DDR bank 绑定、x86 主机），ZCU104 需用 `ubench/offchip_bandwidth/embedded/` 下的嵌入式版本（交叉编译 + SD 卡启动，见 u4-l3）。

**练习 3**：`make all` 在 x86 服务器上会不会生成 `sd_card/` 目录内容？为什么 `all` 还要依赖 `sd_card`？

**答案**：不会。`sd_card` 的整个配方体在 `ifneq ($(HOST_ARCH), x86)` 内，x86 下是空配方；但作为 `all` 的依赖，它仍然触发 `$(EXECUTABLE) $(BINARY_CONTAINERS) emconfig` 的构建，保证 `make all` 一次把三样产物全部备齐。

## 5. 综合实践

**任务**：为 `read/DDR/2ports_512bit` 工程撰写一份《构建与运行命令流程说明文档》——这是无真机、无 Vitis 环境下本讲最重要的产出；如果你手头有 Vitis 2020.2，则进一步实际跑通仿真。

**第一步：推导命令链（必做，纯源码阅读即可完成）。** 把下表补全成你自己的文档（左列已给出答案框架，右列请你按 4.2.3/4.3.3 的规则展开成完整命令）：

| 阶段 | 触发规则 | 实际执行的命令（展开 TARGET/DEVICE/XSA 后） |
| --- | --- | --- |
| 环境检查 | utils.mk 顶层 + `check-devices`/`check-xrt` | （无命令，失败即 `$(error)` 退出） |
| 内核编译 | `$(TEMP_DIR)/krnl_ubench.xo: src/krnl_ubench.cpp` | `mkdir -p _x.sw_emu.<XSA>`；`v++ -t sw_emu -g --platform <DEVICE> --save-temps --temp_dir _x.sw_emu.<XSA> -c -k krnl_ubench -I'src' -o .../krnl_ubench.xo src/krnl_ubench.cpp` |
| 内核链接 | `$(BUILD_DIR)/ubench.xclbin: $(..._OBJS)` | `mkdir -p build_dir.sw_emu.<XSA>`；`v++ -t sw_emu -g --platform <DEVICE> --save-temps --temp_dir build_dir.sw_emu.<XSA> -l --config ./ubench.ini -o .../ubench.xclbin .../krnl_ubench.xo` |
| 主机编译 | `$(EXECUTABLE): check-xrt $(HOST_SRCS)` | `g++ <CXXFLAGS 全量> common/.../xcl2.cpp src/host.cpp src/krnl_config.h -o ubench <LDFLAGS 全量>`（其中 CXXFLAGS 含 `-I$XILINX_XRT/include`、`-I.../xcl2`、`-pthread -Wall -O0 -g -std=c++11`；LDFLAGS 含 `-L$XILINX_XRT/lib -lOpenCL -lpthread -lrt -lstdc++`） |
| 仿真配置 | `emconfig` | `emconfigutil --platform <DEVICE> --od _x.sw_emu.<XSA>` |
| 运行 | `check` 配方 | `cp _x.sw_emu.<XSA>/emconfig.json .`；`XCL_EMULATION_MODE=sw_emu ./ubench build_dir.sw_emu.<XSA>/ubench.xclbin` |
| 报告 | `check` 末尾 | `perf_analyze profile -i profile_summary.csv -f html`（需 `PROFILE=yes` 才有 CSV） |

要求：文档中每个命令注明**来源行号**（如「内核编译来自 Makefile:95-97」），`<XSA>` 用你选的平台的 `device2xsa` 结果代入。

**第二步：干跑核对（有 GNU make 即可）。** 用 4.2.4 的 `make -n` 方法分别对 `build`、`exe`、`check` 干跑，对照你写的命令链，修正不一致处（常见偏差：忘了 `-g` 只在非 hw 时追加、忘了 `$(+)` 展开成全部 `.xo`）。

**第三步：真机仿真验证（装有 Vitis 2020.2 时）。** 执行 4.3.4 的 `make check TARGET=sw_emu DEVICE=<你的平台>`，确认：编译零错误、程序按 payload 倍增打印结果、退出码为 0。若在 `perf_analyze` 处报缺文件，改用 `make check TARGET=sw_emu DEVICE=<平台> PROFILE=yes` 再跑一次对比（PROFILE 会延长链接时间）。

**预期结果**：一份与 Makefile 逐行对应的命令流程文档 + （可选）一次成功的 sw_emu 运行记录。完成后你对「uBench 五件套中 Makefile 这一环」的理解就从「会敲 make」升级到「能徒手拆解 make」。

## 6. 本讲小结

- uBench 官方环境为 **Ubuntu 18.04 + Vitis 2020.2 + XRT 2020.2**；构建真正硬性检查的环境变量是 `XILINX_VITIS`（解析期）、`XILINX_XRT`（`check-xrt`）、`DEVICE`（`check-devices`），SoC 目标另需 `SYSROOT` 与交叉编译器。
- 工程构建产出两条独立链：`src/krnl_ubench.cpp` → `v++ -c` → `.xo` → `v++ -l --config ubench.ini` → `ubench.xclbin`；`src/host.cpp` + 公共库 `xcl2.cpp` → `g++` → `ubench`。产物目录名带 `$(TARGET).$(XSA)`，天然隔离不同目标/平台。
- `make all` = check-devices + 主机程序 + xclbin + emconfig + sd_card（x86 下最后一项为空配方）；`make check` 在此基础上**自动运行**：仿真模式靠 `cp emconfig.json .` + `XCL_EMULATION_MODE=<TARGET>`，真机模式直接执行。
- `TARGET` 三档的传递链：命令行 → Makefile 变量 → `v++ -t $(TARGET)`（决定构建产物形态）→ check 配方里的 `XCL_EMULATION_MODE=$(TARGET)`（决定 XRT 运行形态）。
- 工程内 `utils.mk` 是 `common/utils.mk` 的逐字节副本且被 `include ./utils.mk` 优先使用；datacenter README 存在文档滞后（`--kernel_frequency` 未在示例 Makefile 中、配置头文件名写错），以源码为准。
- 无 Vitis 环境下，`make -n` 干跑 + 手写命令流程文档是掌握构建系统的等效实践路径；`sw_emu` 只验证功能链路，其带宽数值无物理意义。

## 7. 下一步学习建议

构建系统打通后，下一讲 **u1-l4「解剖一个最小微基准工程」** 将进入这五个文件的内容本身：`krnl_config.h` 如何用一套宏同时驱动内核与主机、`krnl_ubench.cpp` 的双端口读循环、`host.cpp` 从加载 xclbin 到 `enqueueTask` 的完整时序。建议按以下顺序继续：

1. 先做本讲综合实践（命令流程文档），因为 u1-l4 要画的调用时序图以「主机程序如何被编译、加载什么参数」为前提；
2. 读 [src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp) 的 `main()` 开头 50 行，找到 `argv[1]`（xclbin 路径，即本讲 `CMD_ARGS` 传入的那个）被消费的位置；
3. 若你对 `--config ./ubench.ini` 背后的 `sp/slr/nk` 指令好奇，可以先跳读 [ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L1-L6)（共 6 行、4 条指令），系统讲解在 u3-l3。
