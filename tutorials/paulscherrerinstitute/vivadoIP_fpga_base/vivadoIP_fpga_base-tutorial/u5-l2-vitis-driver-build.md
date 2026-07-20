# Vitis 驱动构建与元数据文件

## 1. 本讲目标

在上一篇 u5-l1 中，我们已经读过 `fpga_base.c` / `fpga_base.h`，知道裸机程序通过 `Xil_Out32` / `Xil_In32` 访问寄存器，函数形如 `fpga_base_version(uint32_t base_addr)`。但当时我们刻意留下了两个问题没有回答：

- `base_addr` 这个基地址**究竟从哪里来**？
- 那个 `fpga_base.c` 文件**是怎么变成**可以和应用程序链接在一起的库的？

本讲就回答这两个问题。读完本讲，你应当能够：

1. 说清楚 `drivers/fpga_base/src/Makefile` 是一个**模板**：哪些变量被故意留空、由谁在何时注入，以及它如何把每个 `.c` 编译成目标文件、把头文件拷贝到 BSP 的 `include/`。
2. 理解 `drivers/fpga_base/data/fpga_base.tcl` 如何在 BSP 生成阶段**自动写出** `xparameters.h` 中关于 fpga_base 的宏（`NUM_INSTANCES`、`DEVICE_ID`、`C_BASEADDR`、`C_HIGHADDR`）。
3. 看懂 `drivers/fpga_base/data/fpga_base.mdd` 这个驱动描述文件如何把“一段 C 代码”**绑定到** `fpga_base` 这个 IP 外设上。
4. 把这三件事串成一条完整链路：从 Vivado Block Design 里放进一个 fpga_base 实例，到 Vitis 里 `#include <xparameters.h>` 拿到基地址，再到调用 `fpga_base_print()`。

## 2. 前置知识

本讲是软件构建侧的内容，不涉及 HDL 电路。需要你大致了解以下概念（不熟也没关系，下面会顺带解释）：

- **Vitis / 裸机（bare-metal）程序**：直接运行在处理器（如 MicroBlaze、ARM）上、没有操作系统的程序。它和硬件外设打交道的方式就是读写内存地址。
- **BSP（Board Support Package，板级支持包）**：Vitis 为一个硬件设计自动生成的“底层支持包”，里面包含 `libxil.a`（Xilinx 提供的底层库）、`xparameters.h`（描述硬件设计里有哪些外设、各自地址是什么）以及各外设的驱动。
- **libxil.a**：一个静态归档库（archive），所有外设驱动的 `.o` 目标文件最终都被收进它。
- **内存映射 IO**：u5-l1 已讲，`Xil_In32`/`Xil_Out32` 本质是带 `volatile` 的指针解引用。
- **GNU Make**：模式规则（pattern rule，如 `%.o: %.c`）、自动变量（`$<`、`$@`）、`wildcard`、`include` 指令。
- **IP-XACT 与 fileSet**：u1-l2 / u4-l1 已讲，`component.xml` 里把文件按用途分组，其中有一个专门给软件驱动的文件集。

一句话定位：本讲的三个文件都不是“业务逻辑”，而是**让 Vitis 工具链认识、编译、装配这个驱动**的“说明书与施工图”。

## 3. 本讲源码地图

| 文件 | 行数 | 角色 | 谁来读它 |
|------|------|------|----------|
| [drivers/fpga_base/src/Makefile](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile) | 41 | 编译模板：把 `.c` 编成 `.o`、把 `.h` 拷进 `include/` | Vitis 的 `make` |
| [drivers/fpga_base/data/fpga_base.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.tcl) | 6 | 代码生成脚本：产出 `xparameters.h` 片段 | Vitis 的 BSP 生成器（libgen） |
| [drivers/fpga_base/data/fpga_base.mdd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.mdd) | 11 | 驱动描述：声明本驱动支持哪个外设、如何拷贝 | Vitis 的驱动识别逻辑 |
| drivers/fpga_base/src/fpga_base.c / .h | — | 驱动源码本体（u5-l1 已讲） | C 编译器 |
| component.xml 中的 `xilinx_softwaredriver_view_fileset` | — | 把上述 5 个驱动文件登记进 IP 包（u4-l1 已讲） | Vivado 打包器 |

关键认知：**`.mdd` 是身份证、`.tcl` 是地址生成器、`Makefile` 是施工图**。三者缺一不可，但都不含任何运行期业务逻辑。

## 4. 核心概念与源码讲解

### 4.1 驱动 Makefile：一个被 Vitis 注入变量的模板

#### 4.1.1 概念说明

先建立一个直觉：Xilinx 的驱动 Makefile 不是给人手动敲 `make` 用的，而是给 **Vitis 的 BSP 构建系统**调用的。Vitis 在调用它之前，会先把交叉编译器、归档器、编译选项等“环境变量”注入进来。所以你在仓库里看到的这份 Makefile 故意留了一堆空变量——它们是**占位符**，等 Vitis 在 BSP 构建时填值。

这和 u3-l3 里讲的 `$$tag$$` 占位符注入是同一种设计哲学：**把“会变的部分”留空，由上游工具在固定时机回填**。

#### 4.1.2 核心流程

当 Vitis 为某个硬件设计生成 BSP 时，对每一个外设驱动都会走这样一遍：

```
Vitus 读取 .mdd → 识别出 fpga_base 驱动
        │
        ▼
执行 data/fpga_base.tcl 的 generate → 写出 xparameters.h 片段
        │
        ▼
设置环境变量：COMPILER=mb-gcc、ARCHIVER=mb-ar、COMPILER_FLAGS=...
        │
        ▼
make -f drivers/fpga_base/src/Makefile libs
        │     ├── 模式规则：每个 .c  → RELEASEDIR/*.o
        │     └── （老版本还会：ARCHIVER -r libxil.a *.o）
        ▼
make ... include
        │     └── 模式规则：每个 .h  → INCLUDEDIR/*.h
        ▼
顶层 BSP Makefile 把所有驱动的 .o 汇总归档成 libxil.a
```

注意一个关键变化：**当前版本的 Makefile 不再自己把 `.o` 归档进 `libxil.a`**，而是只产出 `.o` 文件，由顶层 Makefile 统一归档。这一点我们在 4.1.3 用 git 历史佐证。

#### 4.1.3 源码精读

**（1）变量声明区：故意留空 + 库名**

[drivers/fpga_base/src/Makefile:L1-L7](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile#L1-L7) 定义了驱动库版本号、留空的工具链变量和库名：

```makefile
DRIVER_LIB_VERSION = 1.0
COMPILER=
ARCHIVER=
CP=cp
COMPILER_FLAGS=
EXTRA_COMPILER_FLAGS=
LIB=libxil.a
```

- `COMPILER`、`ARCHIVER`、`COMPILER_FLAGS`、`EXTRA_COMPILER_FLAGS` 全部为空——它们由 Vitis 在调用时按目标处理器（如 MicroBlaze 的 `mb-gcc` / `mb-ar`，或 ARM 的 `arm-none-eabi-gcc`）注入。
- `CP=cp` 是唯一写死的工具，因为拷贝头文件不依赖交叉编译器。
- `LIB=libxil.a` 是目标归档库的名字（在新版里仅作命名约定，实际归档由顶层完成）。
- `DRIVER_LIB_VERSION = 1.0` 是 2023 年新增的一行，用于让较新的 Vitis 版本识别驱动库版本（与 `.mdd` 里 `VERSION = 1.0` 对应，注意它和 IP 本身的 `1.4` 不是一回事）。

**（2）输出目录：相对路径锚定 BSP 树**

[drivers/fpga_base/src/Makefile:L12-L14](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile#L12-L14):

```makefile
RELEASEDIR=../../../lib/
INCLUDEDIR=../../../include/
INCLUDES=-I./. -I$(INCLUDEDIR)
```

`RELEASEDIR` 是 `.o` 目标文件的落脚点，`INCLUDEDIR` 是 `.h` 被拷贝去的地方。两者都用 `../../../`，意味着“从 `drivers/fpga_base/src/` 往上三级”。在仓库里往上三级是仓库根（那里并没有 `lib/`），这是因为**这份 Makefile 不是在仓库里跑的**——Vitis 会把驱动按 Xilinx 约定布局拷进 BSP 目录树，在那里 `../../../lib/` 与 `../../../include/` 才会命中 BSP 的公共 `lib/`、`include/`。

**（3）源文件收集与目标文件清单**

[drivers/fpga_base/src/Makefile:L16-L21](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile#L16-L21):

```makefile
SRCFILES:=$(wildcard *.c)

OBJECTS = $(addprefix $(RELEASEDIR), $(addsuffix .o, $(basename $(wildcard *.c))))

libs: $(OBJECTS)
	echo "Compiling fpga_base..."
```

`SRCFILES` 用 `wildcard` 抓取当前目录所有 `.c`——本目录只有 `fpga_base.c`。`OBJECTS` 用三重函数把 `fpga_base.c` 变换成 `../../../lib/fpga_base.o`（`basename` 去扩展名 → `addsuffix .o` → `addprefix` 加输出目录）。`libs` 目标依赖所有这些 `.o`，触发下面的模式规则。

**（4）编译模式规则 + 依赖注入**

[drivers/fpga_base/src/Makefile:L23-L30](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile#L23-L30):

```makefile
DEPFILES := $(SRCFILES:%.c=$(RELEASEDIR)%.d)

include $(wildcard $(DEPFILES))

include $(wildcard ../../../../dep.mk)

$(RELEASEDIR)%.o: %.c
	${COMPILER} $(CC_FLAGS) $(ECC_FLAGS) $(INCLUDES) $(DEPENDENCY_FLAGS) $< -o $@
```

- 第 29–30 行的模式规则 `$(RELEASEDIR)%.o: %.c` 才是真正调用编译器的地方：`${COMPILER}`（Vitis 注入的交叉编译器）+ `CC_FLAGS`（= `COMPILER_FLAGS`）+ `ECC_FLAGS`（= `EXTRA_COMPILER_FLAGS`）+ 头文件搜索路径 + `DEPENDENCY_FLAGS`，把 `$<`（源文件）编成 `$@`（目标 `.o`）。
- `DEPENDENCY_FLAGS` 不在本文件定义，来自第 27 行 `include $(wildcard ../../../../dep.mk)`。`dep.mk` 是 Xilinx BSP 提供的片段，通常展开成 `-MMD -MP` 之类用于自动生成头文件依赖。用 `wildcard` 包裹表示“**找不到也无所谓**”——这就是为什么仓库里看不到 `dep.mk` 却不会报错。
- 第 23–25 行的 `DEPFILES`/`include` 是把上一轮编译自动写出的 `.d` 依赖文件再 include 进来，实现增量编译时的头文件依赖追踪。

**（5）头文件拷贝**

[drivers/fpga_base/src/Makefile:L32-L36](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile#L32-L36):

```makefile
.PHONY: include
include: $(addprefix $(INCLUDEDIR),$(wildcard *.h))

$(INCLUDEDIR)%.h: %.h
	$(CP) $< $@
```

`include`（注意它被 `.PHONY` 声明为伪目标，避免和 `make` 的 `include` 指令混淆）依赖 `../../../include/fpga_base.h`，由第 35–36 行的模式规则用 `cp` 把当前目录的 `fpga_base.h` 拷过去。这样应用程序 `#include "fpga_base.h"` 就能找到它。

**（6）关键演化：不再自己归档 libxil.a**

[drivers/fpga_base/src/Makefile:L38-L40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile#L38-L40) 是 `clean` 目标。值得注意的是当前 `Makefile` 全文**没有** `$(ARCHIVER) -r ... libxil.a` 这一行。用 git 历史可以看清这个改动。提交 `cf31d23`（“Update driver Makefile to support newer Vitis versions”，2023 年）之前，老版本是这样写的：

```makefile
libs:
	echo "Compiling fpga_base..."
	$(COMPILER) $(COMPILER_FLAGS) $(EXTRA_COMPILER_FLAGS) $(INCLUDES) $(LIBSOURCES)
	$(ARCHIVER) -r ${RELEASEDIR}/${LIB} ${OBJECTS} ${ASSEMBLY_OBJECTS}
	make clean
```

也就是说，**老版本**由驱动自己调用归档器 `$(ARCHIVER) -r` 把 `.o` 塞进 `libxil.a`；**新版本**改成只产出 `.o`，把归档交给顶层 BSP 的 Makefile 统一做。这正是“support newer Vitis versions”的实质：较新的 Vitis 改变了 BSP 构建的组织方式，要求每个驱动只交付 `.o` 而不要自行归档，否则会和顶层归档步骤冲突（Windows 下尤其明显，对应更早的 bugfix 提交 `4587974`）。

#### 4.1.4 代码实践

**实践目标**：在不安装 Vitis 的前提下，纯靠阅读 Makefile 与 git 历史，复原“一份 `.c` 是如何变成 `libxil.a` 中一个成员”的全过程。

**操作步骤**：

1. 打开 [drivers/fpga_base/src/Makefile](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile)，对照第 16、18 行，写出 `SRCFILES` 和 `OBJECTS` 在本仓库（只有一个 `fpga_base.c`）下展开后的实际字符串。
2. 运行 `git show cf31d23 -- drivers/fpga_base/src/Makefile`，找到被删除的那行 `$(ARCHIVER) -r ...`。
3. 对照 [drivers/fpga_base/data/fpga_base.mdd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.mdd) 里的 `VERSION = 1.0` 与 Makefile 第 1 行 `DRIVER_LIB_VERSION = 1.0`，确认两者一致。

**需要观察的现象 / 预期结果**：

- 第 1 步：`SRCFILES` = `fpga_base.c`；`OBJECTS` = `../../../lib/fpga_base.o`。
- 第 2 步：能看到老版本里 `$(ARCHIVER) -r ${RELEASEDIR}/${LIB} ${OBJECTS} ${ASSEMBLY_OBJECTS}` 这一行，而当前版本已删除。
- 第 3 步：驱动库版本（`1.0`）独立于 IP 版本（`component.xml` 里是 `1.4`），两者不是一回事。

> 由于本仓库不包含交叉编译器，以上为**源码阅读型实践**，无需真正执行编译；若你本机有 Vitis 工程，可在生成的 BSP 目录里 `make -n libs`（`-n` 只打印不执行）观察 Vitis 注入的完整命令。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `COMPILER`、`ARCHIVER` 这些变量故意写成空？

> **答**：因为这份 Makefile 是模板，要支持多种处理器（MicroBlaze / ARM 等）。具体用哪个交叉编译器由 Vitis 在 BSP 构建时按目标处理器注入，所以仓库里留空。

**练习 2**：第 27 行 `include $(wildcard ../../../../dep.mk)` 为什么用 `wildcard` 包裹？

> **答**：`wildcard` 在文件不存在时返回空字符串，使 `include` 不报“找不到文件”的错。`dep.mk` 是 Xilinx BSP 环境提供的片段（定义 `DEPENDENCY_FLAGS`），在仓库源码树里并不存在，所以必须容忍它缺失。

**练习 3**：当前 Makefile 把 `.o` 产出到 `../../../lib/`，那么是谁把它们收进 `libxil.a` 的？

> **答**：是 Vitis 顶层 BSP 的 Makefile。当前驱动 Makefile 不再自行调用 `$(ARCHIVER) -r`（老版本会），这是为兼容较新 Vitis 版本而做的改动。

---

### 4.2 xparameters.h 的自动生成：data/fpga_base.tcl

#### 4.2.1 概念说明

在 u5-l1 里，所有驱动函数都接收一个 `base_addr` 参数。这个基地址不是手写死在代码里的，而是来自 `xparameters.h`——这个头文件由 Vitis **根据你当前的硬件设计自动生成**：你在 Block Design 里把 fpga_base 放在哪个地址，生成的宏就指向哪个地址；你放两份 fpga_base，它就生成两套宏。

那么 Vitis 怎么知道要为 fpga_base 生成哪些宏？答案就是本文件：一段极短的 TCL，告诉 BSP 生成器“请帮我把这个驱动的实例信息写进 `xparameters.h`”。

#### 4.2.2 核心流程

```
Vivado 导出硬件设计（.xsa）→ 含 fpga_base 实例的地址分配
        │
        ▼
Vitis 读 .xsa，为每个外设找匹配驱动（靠 4.3 的 .mdd）
        │
        ▼
调用该驱动 data/*.tcl 的 generate 过程
        │
        ▼
generate 调用 xdefine_include_file，传入：
   文件名 "xparameters.h"、驱动名 "fpga_base"、参数列表
        │
        ▼
xdefine_include_file（Xilinx BSP TCL 库提供，不在本仓库）
   按约定写出 #define XPAR_FPGA_BASE_* 宏
```

#### 4.2.3 源码精读

整个文件只有一个过程，共 6 行，全部有效内容在 [drivers/fpga_base/data/fpga_base.tcl:L3-L5](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.tcl#L3-L5):

```tcl
proc generate {drv_handle} {
    xdefine_include_file $drv_handle "xparameters.h" fpga_base "NUM_INSTANCES" "DEVICE_ID"  "C_BASEADDR" "C_HIGHADDR"
}
```

逐个参数解释 `xdefine_include_file`（这是 Xilinx BSP TCL 库提供的过程，**不在本仓库**，是 Vitis 自带的）：

| 参数位置 | 实参 | 含义 |
|----------|------|------|
| 1 | `$drv_handle` | 当前驱动的句柄，由 Vitis 传入，代表“这次要为哪个驱动实例生成代码” |
| 2 | `"xparameters.h"` | 要写入的目标头文件名 |
| 3 | `fpga_base` | 驱动/外设名字，决定宏前缀（大写化为 `FPGA_BASE`） |
| 4..7 | `"NUM_INSTANCES" "DEVICE_ID" "C_BASEADDR" "C_HIGHADDR"` | 要为每个实例定义的参数名 |

按 Xilinx 的标准约定，`xdefine_include_file` 会为 fpga_base 的第 0 个实例写出（宏名大写化、驱动名作前缀，这是 Xilinx 通行规则）：

```c
/* 示例代码：以下为 xdefine_include_file 按约定产出的典型内容，
   具体数值取决于你的 Block Design 地址分配，待本地验证 */
#define XPAR_FPGA_BASE_NUM_INSTANCES    1
#define XPAR_FPGA_BASE_0_DEVICE_ID      0
#define XPAR_FPGA_BASE_0_BASEADDR       0x40000000
#define XPAR_FPGA_BASE_0_HIGHADDR       0x400000FF
```

这套命名和 u5-l1 里的用法完全对得上：u5-l1 中提到的基地址来自 `XPAR_FPGA_BASE_0_BASEADDR`，正是这里的 `C_BASEADDR` 参数生成出来的。四个参数的含义：

- **`NUM_INSTANCES`**：硬件设计里 fpga_base 的实例总数（放了几份）。
- **`DEVICE_ID`**：每个实例的唯一编号，供运行期枚举查找。
- **`C_BASEADDR`** / **`C_HIGHADDR`**：该实例 AXI 地址空间的起止地址，由 Vivado 在 Block Design 里分配（对应 `component.xml` 中 range=256 的那段地址映射，见 u2-l2）。

注意第 3 个参数 `fpga_base` 与 4.3 节 `.mdd` 里 `supported_peripherals = (fpga_base)`、以及 `component.xml` 里 `<spirit:name>fpga_base</spirit:name>` 必须三处一致，Vitis 才能把“硬件里的这个 IP”正确路由到“这份驱动脚本”。

#### 4.2.4 代码实践

**实践目标**：确认 `data/fpga_base.tcl` 声明的四个参数如何映射成 `xparameters.h` 中的宏名，并与 u5-l1 的调用对齐。

**操作步骤**：

1. 打开 [drivers/fpga_base/data/fpga_base.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.tcl)，确认 `generate` 只调用了一次 `xdefine_include_file`。
2. 在 [drivers/fpga_base/src/fpga_base.c](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c) 里搜索 `XPAR`，看驱动本体是否直接用了这些宏（提示：本驱动把基地址交给了应用层，自身不直接 `#include <xparameters.h>`，这是它的设计选择）。
3. 假设你在 Block Design 里放了**两个** fpga_base 实例，试写出第二个实例的 `BASEADDR` 宏名。

**预期结果**：

- 第 3 步：宏名应为 `XPAR_FPGA_BASE_1_BASEADDR`（实例编号 0→1），这正是 `NUM_INSTANCES` 会变成 2 的意义。

> 本实践为源码阅读型。要真正看到生成的 `xparameters.h`，需在 Vitis 里基于含 fpga_base 的硬件设计生成 BSP 后查看，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate` 要把 `C_BASEADDR` 列进参数表，而不是写死一个地址？

> **答**：因为地址由使用者在 Vivado Block Design 里分配，每次工程都可能不同。把它列为参数，BSP 生成时才会按实际地址填值，保证驱动与硬件一致。

**练习 2**：如果我把 Block Design 里的 fpga_base 删掉，重新生成 BSP，`xparameters.h` 会有什么变化？

> **答**：`XPAR_FPGA_BASE_NUM_INSTANCES` 会变成 0，对应的 `DEVICE_ID` / `BASEADDR` / `HIGHADDR` 宏都不会生成——因为这些宏是按实际实例动态产出的。

**练习 3**：`xdefine_include_file` 这个过程定义在哪里？

> **答**：不在本仓库。它是 Xilinx Vitis BSP TCL 库自带的过程，BSP 生成器（libgen）在调用驱动 `data/*.tcl` 之前已经把它加载进 TCL 解释器。本仓库只负责“调用它”。

---

### 4.3 驱动描述文件 .mdd：把驱动绑定到外设

#### 4.3.1 概念说明

`.mdd`（Driver Description File，驱动描述文件）是 Xilinx 的 **PSF（Peripheral Support File，外设支持文件）** 格式之一。它回答两个问题：

1. **这份驱动是为哪个 IP 外设服务的？**（`supported_peripherals`）
2. **Vitus 生成 BSP 时，这份驱动的文件该怎么处理？**（`copyfiles`）

可以这样理解：如果说 `data/fpga_base.tcl` 是“施工方法”，`src/Makefile` 是“施工图”，那么 `.mdd` 就是挂在工地门口的“**施工许可证**”——上面写明“本施工队（驱动）负责的楼号（外设）是 fpga_base，所有材料全部进场（copyfiles=all）”。没有这张证，Vitis 不会让施工队进场。

#### 4.3.2 核心流程

```
Vitus 读硬件设计 → 遇到一个 fpga_base 外设
        │
        ▼
在已知的驱动库里寻找 .mdd 中 supported_peripherals 命中 "fpga_base" 的驱动
        │
        ▼
命中 → 按 copyfiles 策略把驱动 src/ 与 data/ 拷进 BSP
        │
        ▼
执行 data/*.tcl（4.2）+ 调 src/Makefile（4.1）
```

#### 4.3.3 源码精读

[drivers/fpga_base/data/fpga_base.mdd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.mdd) 全文：

```xilinx-mdd
OPTION psf_version = 2.1;

BEGIN DRIVER fpga_base
    OPTION supported_peripherals = (fpga_base);
    OPTION copyfiles = all;
    OPTION VERSION = 1.0;
    OPTION NAME = fpga_base;
END DRIVER
```

逐行解释（对应 [drivers/fpga_base/data/fpga_base.mdd:L3-L10](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.mdd#L3-L10)）：

| 行 | 语句 | 含义 |
|----|------|------|
| 3 | `OPTION psf_version = 2.1;` | 声明本文件遵循的 PSF 格式版本，供 Vitis 解析器识别语法 |
| 5 | `BEGIN DRIVER fpga_base` | 开启一个驱动定义块，块名 `fpga_base` |
| 6 | `OPTION supported_peripherals = (fpga_base);` | **关键字段**：本驱动服务的 IP 外设名是 `fpga_base`，必须与 `component.xml` 的 `<spirit:name>` 一致 |
| 7 | `OPTION copyfiles = all;` | 拷贝策略：把驱动所有文件都拷进 BSP（也可取更保守的值以减小体积） |
| 8 | `OPTION VERSION = 1.0;` | 驱动版本，与 Makefile 第 1 行 `DRIVER_LIB_VERSION = 1.0` 对应（**不是** IP 的 1.4） |
| 9 | `OPTION NAME = fpga_base;` | 驱动名 |
| 10 | `END DRIVER` | 结束驱动定义块 |

最关键的是第 6 行 `supported_peripherals`。它是 Vitis“为外设找驱动”的匹配键：硬件设计里出现一个名为 `fpga_base` 的 IP 实例时，Vitis 就在驱动库中查找谁的 `supported_peripherals` 命中 `fpga_base`，找到本驱动后才会去执行它的 `.tcl` 与 `Makefile`。

> 命名一致性提醒：`fpga_base` 这个名字至少要在这四处保持一致——`component.xml` 的 `<spirit:name>`、`.mdd` 的 `supported_peripherals`、`.tcl` 第 3 个参数、顶层 VHDL `entity` 名。任何一处拼错都会导致“Vitis 找不到驱动”或“生成不出宏”。

这三个 `data/` 与 `src/` 文件之所以能被 Vitis 看到，是因为它们被登记进了 `component.xml` 的软件驱动文件集。参见 [component.xml:L1296-L1317](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1296-L1317)（`xilinx_softwaredriver_view_fileset`），里面依次列出了 `fpga_base.mdd`、`fpga_base.tcl`、`Makefile`、`fpga_base.c`、`fpga_base.h` 这五个文件——正是本讲讨论的全部驱动文件。该文件集的标识见 [component.xml:L422-L427](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L422-L427)，`envIdentifier` 为 `:vivado.xilinx.com:sw.driver`，告诉 Vivado“这是一个软件驱动文件集”。

#### 4.3.4 代码实践

**实践目标**：验证 `.mdd`、`component.xml`、`.tcl` 三处对外设名的命名一致性。

**操作步骤**：

1. 打开 [drivers/fpga_base/data/fpga_base.mdd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.mdd)，记下 `supported_peripherals` 与 `NAME` 的值。
2. 在 [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml) 顶部确认 `<spirit:name>` 的值（应为 `fpga_base`）。
3. 在 [drivers/fpga_base/data/fpga_base.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.tcl) 确认 `xdefine_include_file` 的第 3 个参数。
4. 做一个破坏性思想实验：如果把 `.mdd` 第 6 行改成 `supported_peripherals = (fpga_xyz);`，预测会发生什么。

**预期结果**：

- 第 1–3 步：三处都是 `fpga_base`，完全一致。
- 第 4 步：Vitis 在生成 BSP 时将**找不到**匹配 `fpga_base` 外设的驱动（因为驱动现在声称自己服务的是 `fpga_xyz`），于是不会执行 `data/fpga_base.tcl`，`xparameters.h` 里也就不会出现 `XPAR_FPGA_BASE_*` 宏——这正是命名必须一致的工程意义。

> 本实践为源码阅读 + 思想实验型，请勿真的修改 `.mdd` 提交（本讲规则禁止改源码）。

#### 4.3.5 小练习与答案

**练习 1**：`.mdd` 里 `VERSION = 1.0`，但 IP 是 1.4 版，这矛盾吗？

> **答**：不矛盾。这是**驱动库**的版本，不是 IP 的版本。驱动可以长期稳定在 1.0，而 IP 硬件迭代到 1.4，两者是独立演进的版本号，分别记录在 `.mdd`/Makefile 与 `component.xml` 中。

**练习 2**：`copyfiles = all` 与 `copyfiles` 取其它值（如仅拷必要文件）相比，有什么取舍？

> **答**：`all` 保证 BSP 里驱动完整可用（含源码、头、脚本），便于现场调试和重新编译；代价是 BSP 体积更大。对空间敏感的发布可以改为只拷必需文件，但可能在需要重编时缺料。

**练习 3**：如果想让同一个驱动同时支持两个名字不同的 IP，该改 `.mdd` 的哪一行？

> **答**：改 `supported_peripherals`，写成 `(ip_a, ip_b)` 这种列表形式即可（PSF 支持多外设名）。这样 Vitis 遇到 `ip_a` 或 `ip_b` 都会匹配到本驱动。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画出一张“从 Block Design 到 `fpga_base_print()` 调用”的完整时序与数据流图，并回答 u5-l1 遗留的那个问题——**`base_addr` 到底从哪来**。

**建议步骤**：

1. **身份证阶段**：Vitis 读硬件设计，发现一个 `fpga_base` 外设实例 → 凭 [drivers/fpga_base/data/fpga_base.mdd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.mdd) 第 6 行 `supported_peripherals = (fpga_base)` 命中，按第 7 行 `copyfiles = all` 把驱动 5 个文件拷进 BSP（清单见 [component.xml:L1296-L1317](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1296-L1317)）。
2. **地址生成阶段**：Vitis 调用 [drivers/fpga_base/data/fpga_base.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/data/fpga_base.tcl) 的 `generate`，经 `xdefine_include_file` 在 `xparameters.h` 写出 `XPAR_FPGA_BASE_0_BASEADDR` 等宏——**`base_addr` 的源头就在这里**。
3. **编译阶段**：Vitus 注入 `COMPILER` 等变量后跑 [drivers/fpga_base/src/Makefile](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/Makefile) 的 `libs`（第 20 行）把 `fpga_base.c` 编成 `../../../lib/fpga_base.o`，跑 `include`（第 33 行）把 `fpga_base.h` 拷进 `../../../include/`；最后由顶层 BSP Makefile 把所有 `.o` 归档进 `libxil.a`。
4. **应用调用阶段**：应用程序 `#include <xparameters.h>` 与 `#include "fpga_base.h"`，用 `XPAR_FPGA_BASE_0_BASEADDR` 作 `base_addr` 调用 `fpga_base_print()`（u5-l1 已讲其内部用 `Xil_In32` 读寄存器）。

**产出**：一张包含上述四个阶段的流程图（可用文字框 + 箭头），并在“应用调用”那一格特别标注：`base_addr = XPAR_FPGA_BASE_0_BASEADDR`，而它由阶段 2 的 `.tcl` 生成。

**预期结果**：你能用自己的话向别人解释清楚——为什么只要在 Vivado 里把 fpga_base 连进 Block Design、导出硬件，Vitis 里就能直接 `#include` 并调用，完全不用手写任何地址常量。这就是这三个看似不起眼的小文件（`.mdd` 11 行、`.tcl` 6 行、`Makefile` 41 行）撑起的整套自动化。

## 6. 本讲小结

- `src/Makefile` 是一个**模板**：`COMPILER`/`ARCHIVER`/`*_FLAGS` 故意留空，由 Vitis 按 CPU 注入；它用模式规则把每个 `.c` 编成 `RELEASEDIR` 下的 `.o`、把 `.h` 拷进 `INCLUDEDIR`，并通过可选的 `dep.mk` 注入依赖生成开关。
- 当前版本的 Makefile **不再自己归档 `libxil.a`**（老版本用 `$(ARCHIVER) -r`），只交付 `.o`，归档交给顶层 BSP Makefile——这是 2023 年为兼容新 Vitis 而做的改动（提交 `cf31d23`）。
- `data/fpga_base.tcl` 的 `generate` 调用 Xilinx 自带的 `xdefine_include_file`，为每个 fpga_base 实例在 `xparameters.h` 写出 `NUM_INSTANCES`/`DEVICE_ID`/`C_BASEADDR`/`C_HIGHADDR` 四类宏——这就是 u5-l1 里 `base_addr` 的真正来源。
- `data/fpga_base.mdd` 是 PSF 驱动描述文件，`supported_peripherals = (fpga_base)` 是 Vitis “为外设找驱动”的匹配键，必须与 `component.xml`、`.tcl`、`entity` 名四处一致。
- 这三个文件通过 `component.xml` 的 `xilinx_softwaredriver_view_fileset` 一起打包进 IP，构成“识别 → 生成地址 → 编译装配”的完整自动化链路。
- 贯穿全讲的命名约定：`fpga_base` 这个名字是连接 HDL（u2）、打包（u4）与软件（u5）三层的关键契约。

## 7. 下一步学习建议

- **继续 u5-l3（硬件调试 TCL 与 EPICS 集成）**：本讲讲的是“软件经 CPU 访问寄存器”的链路，下一篇将讲“不经 CPU、直接用 JTAG-to-AXI 调试寄存器”以及把寄存器接入 EPICS 控制系统，与本讲共用同一张寄存器映射（u2-l3）。
- **回顾 u4-l1（PsiIpPackage 打包）**：如果你对本讲的 `xilinx_softwaredriver_view_fileset` 如何被写进 `component.xml` 还有疑问，回到 u4-l1 看 `add_drivers_relative` 如何把驱动目录聚合进 IP 包。
- **动手延伸**：若你有 Vitis 工程，在生成的 BSP 目录里找到 `xparameters.h`，搜索 `FPGA_BASE`，亲手验证本讲推断的宏名；再用 `make -n` 观察 Vitis 注入的完整编译命令，把“留空变量”全部填上真实值。
