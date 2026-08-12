# 构建系统与 Makefile 目标

## 1. 本讲目标

本讲带你读懂本仓库的构建系统。读完之后，你应该能够：

- 说出 `TARGET`（`hw` / `hw_emu` / `sw_emu`）和 `PLATFORM` 这两个核心构建变量的含义、来源，以及 Makefile 对它们的校验逻辑。
- 看懂根 `Makefile` 里 10 个左右的 make 目标（`aie` / `pl` / `host` / `package` / `run` / `plsim_router` / `aiesim` / `aiesim_profile` / `aiesim_xpe` / `metrics` / `clean`）各自干什么、产物落在哪个目录。
- 画出四个关键构建产物（`libadf.a`、`dma_pkt_router.xo`、`sar_backproject.elf`、`.xsa`）之间的依赖与链接顺序，并解释为什么 host 目标必须依赖 AIE 构建产物。
- 把 `v++`（编译/链接/打包）和 `aiesimulator`（仿真）的调用链对号入座。

本讲只讲「怎么把设计搭起来」，不深入任何内核算法；算法留给后续单元。

## 2. 前置知识

在继续之前，请确认你已掌握 [u1-l2](u1-l2-repo-structure-and-test-data.md) 的内容，特别是：

- 仓库按 Versal 三引擎域分层：`design/aie/`（AI Engine）、`design/pl/`（FPGA 上的 HLS 内核）、`design/host/`（ARM 控制程序），三者共享 `design/common.h`。
- 本仓库需要用到的依赖（Yocto SDK、DSP 库、平台文件等）大多来自 `helper_scripts/env_setup.sh` 指向的外部安装路径。

此外，你只需要一点点背景知识：

- **GNU Make 基础**：Makefile 由「变量」「规则」组成。规则写成 `产物: 依赖` + 一条 shell 命令。当依赖比产物新时，命令就会执行。本讲里所谓的「直接目标」（如 `make aie`）是你手动敲的入口，而「间接目标」（如 `build/.../libadf.a`）是真实文件名，会被自动触发。
- **AMD Vitis 工具链**：本项目里所有跨域的编译/链接/打包都靠一个叫 `v++` 的命令完成，它有多种「模式」（`--mode aie` / `--mode hls` / `-l` 链接 / `-p` 打包）。AIE 仿真则用 `aiesimulator`。这些命令的具体行为会在第 4 章逐条解释，你不用现在就懂参数。

一句话理解本讲：**这个 Makefile 是一个「把 C++/ADF/HLS 源码，经三套工具链，变成一块能塞进 VCK190 板卡的 SD 卡启动镜像」的自动化脚本。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [Makefile](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile) | 根构建脚本。定义变量、校验、直接目标、间接目标，串联 AIE/PL/Host/打包/仿真全流程。本讲主线。 |
| [design/host/Makefile](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile) | 子 Makefile，只负责把 ARM 主机程序交叉编译成 `sar_backproject.elf`，并链接 AIE 编译器自动生成的控制代码。 |
| [helper_scripts/env_setup.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh) | 环境脚本，需要 `source` 它来导出 `PLATFORM`、`SDK_PATH`、`DSPLIB_VITIS`、`DTB` 等变量。Makefile 离开它无法工作。 |
| [design/aie/aiecompiler.cfg](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/aiecompiler.cfg) | AIE 编译器配置文件，被根 Makefile 传给 `v++ --mode aie`。 |
| [design/pl/pkt_router_config.cfg](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/pkt_router_config.cfg) | PL HLS 配置文件，声明顶层函数、时钟、综合目标，被根 Makefile 传给 `v++ --mode hls`。 |

> 提示：`design/system_cfgs/system.cfg` 在 `git ls-files` 里只有占位的 `.gitkeep`，真正的 `system.cfg` 是构建时由 Makefile **自动生成**的（详见 4.3），这也是它没有被提交进仓库的原因。

---

## 4. 核心概念与源码讲解

### 4.1 TARGET / PLATFORM 变量与校验

#### 4.1.1 概念说明

本仓库的构建有两条「全局开关」，它们决定了你要为哪种运行环境编译：

- **`TARGET`**：决定编译出来的设计跑在哪里。三选一：
  - `hw`：真实硬件（VCK190 板卡）。编译最慢，但产物能上板。
  - `hw_emu`：硬件仿真。仍走 RTL 级仿真，速度慢，但带时序信息。
  - `sw_emu`：软件仿真（x86）。最快，只验证功能，没有时序精度。
- **`PLATFORM`**：目标平台描述文件（`.xpfm`）。本项目用的是 VCK190 基础平台（`xilinx_vck190_base_202410_1`），它告诉工具链芯片型号、可用 DDR bank、时钟和可用接口。

这两个变量不是凭空出现的：`TARGET` 可以在命令行覆盖（`make TARGET=sw_emu`），`PLATFORM` 则必须由 `env_setup.sh` 提前导出。

#### 4.1.2 核心流程

变量定义与派生流程：

1. `TARGET` 默认为 `hw`，但用 `?=` 赋值，允许命令行覆盖。
2. 由 `TARGET` 派生出 `AIE_TARGET` 和 `PL_TARGET`：只有当 `TARGET=sw_emu` 时，两者才分别变成 `x86sim` 和 `x86`，否则都是 `hw`。
3. 一组「构建目录」变量根据 `TARGET` 分桶，把不同目标的产物隔离到 `build/${TARGET}/...` 下，互不污染。
4. 三道校验关卡拦截非法组合（见 4.1.3）。

伪代码概览：

```text
TARGET = hw  (默认，可被 make TARGET=sw_emu 覆盖)
if TARGET == sw_emu:
    AIE_TARGET = x86sim ; PL_TARGET = x86
else:
    AIE_TARGET = hw     ; PL_TARGET = hw

BUILD_DIR = build/${TARGET}        # 所有产物按 TARGET 隔离
# 校验: TARGET ∈ {hw, hw_emu, sw_emu} 且 PLATFORM 已设置
```

#### 4.1.3 源码精读

变量定义区在 [Makefile:19-26](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L19-L26)：

- `TARGET ?= hw` —— 默认硬件，可覆盖。
- `XSA = sar_backproject_${TARGET}.xsa` —— 产物文件名内嵌 TARGET，所以 `hw` 和 `hw_emu` 的 XSA 不会撞名。
- `HOST_EXE = sar_backproject.elf` —— 主机可执行文件名固定。
- `EMU_LAUNCH_FILE = launch_${TARGET}.sh` —— 仿真启动脚本名随 TARGET 变化。

`TARGET` 到子域目标的派生在 [Makefile:27-33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L27-L33)。注意 `sw_emu` 是唯一会把 AIE 降级到 `x86sim` 的情况；`hw_emu` 仍然走 `hw` 的 AIE 编译路径（因为 AIE 仿真另由 `aiesimulator` 承担）。

构建目录分桶在 [Makefile:35-44](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L35-L44)。这是一条很值得借鉴的工程习惯：用 TARGET 做目录前缀，避免你在 `sw_emu` 和 `hw` 之间切换时残留旧产物。

三道校验关卡：

1. **TARGET 合法性**：[Makefile:49-52](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L49-L52)，用 `$(filter ...)` 判断 TARGET 是否在白名单里，否则 `$(error ...)` 立刻终止。
2. **PLATFORM 必须设置**：[Makefile:54-57](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L54-L57)。如果你忘了 `source env_setup.sh`，这里会直接报错并提示你执行该脚本。
3. **禁止 aiesim + sw_emu**：[Makefile:59-70](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L59-L70)。原因是 `aiesimulator` 做的是 AIE 阵列的**周期精确**仿真，必须配 `hw` / `hw_emu`；而 `sw_emu` 走的是 x86 功能仿真，没有时序信息，二者语义冲突。
4. **禁止 run + hw**：[Makefile:72-77](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L72-L77)。`run` 目标是跑仿真（`launch_${TARGET}.sh`），真实硬件 `hw` 不在主机上跑仿真，所以只允许 `hw_emu` / `sw_emu`。

`PLATFORM` 的真正来源在 [helper_scripts/env_setup.sh:47-49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh#L47-L49)：脚本把 `tgt_plat=xilinx_vck190_base_202410_1` 拼成完整 `.xpfm` 路径并 `export PLATFORM=...`。同一个脚本还导出了 `SDK_PATH`、`DSPLIB_VITIS`、`DTB`、`BL31_ELF`、`UBOOT`、`IMAGE`、`ROOTFS`（[env_setup.sh:12-29](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh#L12-L29)），这些变量分别会在 host 交叉编译（sysroot）、AIE DSP 库引用、打包启动镜像时被用到。

> 小术语：**sysroot** 是目标 OS（这里是 ARM 上的 Linux）的头文件和库的集合，交叉编译主机程序时必须指给它，这样 `#include <xrt/...>` 才能找到板卡上实际存在的库。

#### 4.1.4 代码实践

**实践目标**：亲手触发校验关卡，理解 Makefile 是如何「挡住错误用法」的。

**操作步骤**：

1. 不 source 环境脚本，直接在一个干净 shell 里运行 `make aie`。
2. 观察报错信息。
3. 然后 `source helper_scripts/env_setup.sh`（前提是你已按 README 安装好 Xilinx 工具链），再尝试 `make TARGET=zzz aie`、`make TARGET=sw_emu aiesim`、`make TARGET=hw run` 三条非法命令。

**需要观察的现象**：
- 步骤 1 应当命中 [Makefile:54-57](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L54-L57)，提示 `PLATFORM is not set`。
- 步骤 3 的三条命令应分别命中 TARGET 白名单、aiesim+sw_emu、run+hw 三道关卡，并给出针对性的中文/英文解释。

**预期结果**：四条命令均在编译真正开始**之前**就被 `$(error ...)` 终止，没有任何工具被调用。这正说明校验发生在解析阶段（Makefile 读取阶段），而非执行阶段。

> 待本地验证：如果你当前没有 Xilinx 工具链，步骤 3 仍可通过纯文本阅读 [Makefile:59-77](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L59-L77) 推断每条命令会命中哪条 `ifeq` 分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `XSA` 文件名里要嵌入 `${TARGET}`（即 `sar_backproject_hw.xsa` vs `sar_backproject_hw_emu.xsa`），而 `HOST_EXE` 却不嵌入？

**参考答案**：XSA 是与目标硬件/仿真强绑定的可重配置镜像，`hw` 和 `hw_emu` 的内容完全不同；嵌入 TARGET 可以让两种产物并存于 `build/` 下而不互相覆盖。主机 `sar_backproject.elf` 在不同 TARGET 下的源码一致（只是链接的 AIE 控制代码不同），且它按 `BUILD_DIR=build/${TARGET}/host` 已经在目录层面隔离，文件名无需再重复区分。

**练习 2**：`hw_emu` 时 `AIE_TARGET` 的值是什么？为什么不像 `sw_emu` 那样降级为 `x86sim`？

**参考答案**：`hw_emu` 时 `AIE_TARGET=hw`（见 [Makefile:27-33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L27-L33) 的 else 分支）。原因是 `hw_emu` 的 AIE 时序仿真由独立的 `aiesimulator` 负责，AIE 编译本身仍产出硬件级产物；只有纯功能性的 `sw_emu` 才需要把 AIE 也编译成 x86 可跑的 `x86sim` 形式。

---

### 4.2 直接目标与间接目标

#### 4.2.1 概念说明

根 Makefile 的目标可以分两类：

- **直接目标**（`.PHONY` 声明的「动作」）：你直接敲的那批，如 `make aie`、`make host`、`make package`、`make run`、`make clean`。它们本身不是文件，只是一组动作的入口。
- **间接目标**（真实文件名）：形如 `build/.../libadf.a`、`build/.../dma_pkt_router.xo`、`build/.../sar_backproject.elf`、`build/.../.xsa`。这些是 make 依赖机制真正追踪的产物文件。

直接目标通常「依赖」某个间接目标，比如 `aie: ${AIE_BUILD_DIR}/libadf.a`。这样当你敲 `make aie`，make 会去检查 `libadf.a` 的依赖（源码）是否比它新，从而决定是否重新编译。

理解这条「直接 → 间接」的桥接，是看懂整个构建链的关键。

#### 4.2.2 核心流程

四个间接产物构成主依赖链（箭头表示「依赖于」）：

```text
design/aie/* ──v++ --mode aie──▶ libadf.a ──────────────┐
                                                         ├──▶ HOST_EXE (host/Makefile)
                                          (生成 aie_control_xrt.cpp)
design/pl/*.cpp ──v++ --mode hls──▶ dma_pkt_router.xo ──┐
        (同时生成 system.cfg)                            ├──▶ XSA (v++ -g -l)
libadf.a ───────────────────────────────────────────────┘
XSA + libadf.a + HOST_EXE ──v++ -p──▶ package (SD 卡镜像)
package ──launch_${TARGET}.sh──▶ run   (仅 hw_emu/sw_emu)
```

要点：

1. `libadf.a`（AIE）和 `dma_pkt_router.xo`（PL）可以并行构建，互不依赖。
2. `HOST_EXE` 依赖 `libadf.a`，因为 AIE 编译器会自动生成 host 需要的控制代码（见 4.2.4）。
3. `XSA` 依赖 `libadf.a` + `dma_pkt_router.xo` + `system.cfg`，是「链接」步骤的产物。
4. `package` 把 `XSA` + `libadf.a` + `HOST_EXE` + 测试数据 + 启动脚本一起打包成 SD 卡镜像。
5. `run` 只在仿真时可用，它跑 `launch_${TARGET}.sh`。

#### 4.2.3 源码精读

**直接目标**集中在 [Makefile:82-195](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L82-L195)，要点如下：

| 目标 | 依赖 | 干什么 | 关键行 |
|------|------|--------|--------|
| `package` | `.xo` + `libadf.a` + `HOST_EXE` + `XSA` | 调 `v++ -p` 打包 SD 镜像 | [L91-L113](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L91-L113) |
| `run` | `package` | 跑 `launch_${TARGET}.sh` 仿真 | [L116-L121](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L116-L121) |
| `pl` | `dma_pkt_router.xo` | 仅构建 PL 内核 | [L123-L124](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L123-L124) |
| `plsim_router` | （若缺 CSV 则先 `aiesim`） | 跑 PL 包路由器仿真 | [L128-L138](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L128-L138) |
| `aie` | `libadf.a` | 仅构建 AIE 库 | [L141-L142](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L141-L142) |
| `host` | `HOST_EXE` | 仅构建主机程序 | [L144-L145](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L144-L145) |
| `aiesim` | `libadf.a` | AIE 功能仿真 | [L147-L156](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L147-L156) |
| `aiesim_profile` | `libadf.a` | AIE 仿真 + profile + dump-vcd | [L159-L169](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L159-L169) |
| `aiesim_xpe` | `libadf.a` + `aie.vcd` | 由 vcd 生成功耗估算 `.xpe` | [L174-L184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L174-L184) |
| `metrics` | `XSA` | Vivado 批量跑资源/功耗报告 | [L186-L195](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L186-L195) |

**间接目标**在 [Makefile:197-289](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L197-L289)，这是依赖链的真正核心：

1. **`libadf.a`（AIE 库）**：[Makefile:229-244](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L229-L244)
   - 输入：`design/aie/*`、`common.h`、`aiecompiler.cfg`。
   - 命令：`v++ -c --mode aie ... design/aie/graph.cpp`。
   - 输出：`build/${TARGET}/aie/${AIE_TARGET}/libadf.a`，以及 `Work/` 目录（含自动生成的控制代码）。

2. **`dma_pkt_router.xo`（PL 内核）**：[Makefile:199-227](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L199-L227)
   - 输入：`design/pl/dma_pkt_router.cpp`、`.h`、`pkt_router_config.cfg`、`common.h`。
   - 命令：先用一段 shell **自动生成 `design/system_cfgs/system.cfg`**（用 `grep` 从 `common.h` 读出 `AIE_SWITCHES`，再用循环写出 `nk=`/`stream_connect=`/`sp=` 行），再 `v++ -c --mode hls`。
   - 输出：`build/${TARGET}/pl/${TARGET}/dma_pkt_router.xo` + `system.cfg`。

3. **`HOST_EXE`（主机可执行）**：[Makefile:246-254](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L246-L254)
   - 输入：`libadf.a`、`${BUILD_DIR}/Work/ps/c_rts/aie_control.cpp`、`design/host/*`、`common.h`。
   - 命令：递归 `$(MAKE) -C design/host/ ...`（进入子 Makefile）。
   - 输出：`build/${TARGET}/host/sar_backproject.elf`。

4. **`XSA`（可重配置镜像）**：[Makefile:256-270](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L256-L270)
   - 输入：`dma_pkt_router.xo` + `libadf.a` + `system.cfg`。
   - 命令：`v++ -g -l`（链接），传入 `--config system.cfg`。
   - 输出：`build/${TARGET}/xsa/sar_backproject_${TARGET}.xsa`。

注意 `package` 目标在 [Makefile:91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L91) 把四个间接产物全部列为依赖，并且还会**用 `grep` 实时从 `common.h` 读 `RC_SAMPLES`**（[Makefile:92](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L92)），用来挑选正确的 GOTCHA phdata CSV（`gotcha_phdata_${RC_SAMPLES}-out-of-424-...`）。这正好呼应 [u1-l2](u1-l2-repo-structure-and-test-data.md) 里「Makefile 依 `common.h` 自动选对 phdata 文件」的说法。

> 小术语：**XSA**（Xilinx Support Archive）是把 PL 比特流、AIE 图、系统连接描述打包到一起的镜像；**libadf.a** 是 ADF（Adaptive Data Flow）图的归档库；**.xo** 是单个 PL/HLS 内核的目标文件，地位类似于普通编译里的 `.o`。

#### 4.2.4 代码实践

**实践目标**：把四条间接规则的「输入 → 工具 → 输出」整理成一张表，并解释 host 为何依赖 AIE。

**操作步骤（源码阅读型实践）**：

1. 打开根 [Makefile:197-270](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L197-L270)，按下表填空并核对行号：

   | 间接产物 | 输入文件 | 工具/命令 | 输出路径 |
   |----------|----------|-----------|----------|
   | `libadf.a` | `design/aie/*`、`common.h`、`aiecompiler.cfg` | `v++ -c --mode aie` | `build/${TARGET}/aie/${AIE_TARGET}/libadf.a` |
   | `dma_pkt_router.xo` | `design/pl/dma_pkt_router.cpp/.h`、`pkt_router_config.cfg`、`common.h` | `v++ -c --mode hls`（先写 `system.cfg`） | `build/${TARGET}/pl/${TARGET}/dma_pkt_router.xo` |
   | `HOST_EXE` | `libadf.a`、`Work/ps/c_rts/aie_control.cpp`、`design/host/*` | `make -C design/host` | `build/${TARGET}/host/sar_backproject.elf` |
   | `XSA` | `dma_pkt_router.xo`、`libadf.a`、`system.cfg` | `v++ -g -l` | `build/${TARGET}/xsa/sar_backproject_${TARGET}.xsa` |

2. 回答关键问题：**为什么 host 目标依赖 AIE 产物？** 请阅读 [design/host/Makefile:9-10](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile#L9-L10) 和 [design/host/Makefile:53-54](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile#L53-L54)。

**需要观察的现象**：
- 子 Makefile 的 `HOST_OBJ` 里有一个特殊的对象 `aie_control_xrt.o`，它编译自 `AIE_CTRL_CPP = ${BUILD_DIR}/Work/ps/c_rts/aie_control_xrt.cpp`。
- 规则 [design/host/Makefile:53-54](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile#L53-L54) 把这个由 AIE 编译器自动生成的 `aie_control_xrt.cpp` **拷贝**进 host 构建目录，再参与链接。
- 子 Makefile 的链接选项里有 `-ladf_api_xrt`、`-lxrt_coreutil`（[design/host/Makefile:25-39](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile#L25-L39)），这些是驱动 AIE 图所需的 XRT 运行库。

**预期结果**：你会得出结论——host 程序并非独立。它在运行时要通过 XRT API（`xrt::graph` 等）去**打开、运行、控制 AIE 图**，而这些控制用的 C++ 接口代码（`aie_control_xrt.cpp`）是 AIE 编译器在生成 `libadf.a` 时一并吐出的。因此 host 的编译必须排在 AIE 之后，根 Makefile 才会在 [Makefile:247](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L247) 把 `libadf.a` 和 `aie_control.cpp` 列为 `HOST_EXE` 的前置依赖。

> 一个值得注意的细节：根 Makefile 第 247 行的依赖写的是 `Work/ps/c_rts/aie_control.cpp`，而子 Makefile 实际编译用的是 `Work/ps/c_rts/aie_control_xrt.cpp`（带 `_xrt` 后缀，见 [design/host/Makefile:10](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile#L10)）。两者都是 AIE 编译器在 `Work/ps/c_rts/` 下自动生成的控制源码（一个偏传统 aie flow、一个面向 XRT），思路一致：host 离不开 AIE 编译产物。

#### 4.2.5 小练习与答案

**练习 1**：如果只改了 `design/pl/dma_pkt_router.cpp`，重跑 `make package`，会触发哪几个间接规则？

**参考答案**：会触发 `dma_pkt_router.xo` 重新综合；因为该规则还会重新生成 `system.cfg`，而 `XSA` 依赖 `.xo` 和 `system.cfg`，所以 `XSA` 也会重新链接；`package` 依赖 `XSA`，于是重新打包。但 `libadf.a` 不会被触发（AIE 源码没动），`HOST_EXE` 也不会（它只依赖 `libadf.a`，而 `libadf.a` 没变）。

**练习 2**：为什么 `package` 目标要用 `grep` 现场读 `common.h` 里的 `RC_SAMPLES`，而不是写死一个数字？

**参考答案**：因为 `RC_SAMPLES` 是 [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) 里可改的全局配置（可选 64/128/256/512），改了它就要用对应那份 GOTCHA phdata CSV 打包。现场 `grep` 保证「构建选的数据文件」和「代码编译时用的宏」永远一致，避免写死后二者不一致导致运行时崩溃。

**练习 3**：`.PHONY` 里声明了 `clean`，但 `clean` 并不依赖任何文件。如果项目根目录下恰好有一个叫 `clean` 的文件，会出什么问题？声明 `.PHONY` 解决了什么？

**参考答案**：若存在同名文件，make 会认为 `clean` 这个目标「已经是最新的」（因为它的依赖为空、文件已存在），从而**不执行**清理命令。`.PHONY: clean` 显式告诉 make「clean 是个动作而非文件」，无论是否存在同名文件都强制执行其命令。

---

### 4.3 v++ 与 aiesimulator 调用链

#### 4.3.1 概念说明

前两节讲清了「变量」和「目标骨架」，本节把骨架里实际被调用的**外部工具命令**单独拎出来讲。整个构建只用到两类核心工具：

- **`v++`（Vitis 编译器）**：一个多模式命令，贯穿 AIE 编译、PL 综合、系统链接、系统打包四个阶段。模式由参数决定：
  - `v++ -c --mode aie`：编译 ADF 图，产出 `libadf.a`。
  - `v++ -c --mode hls`：综合 HLS 内核，产出 `.xo`。
  - `v++ -g -l`：把 `.xo` + `libadf.a` 链接成 `XSA`。
  - `v++ -p`：把 `XSA` + 主机 + 启动文件打包成 SD 镜像。
- **`aiesimulator`（AIE 仿真器）**：对 AIE 阵列做周期精确仿真，可带 `--profile` / `--dump-vcd`。配套还有 `vcdanalyze`（把 vcd 转成 `.xpe` 功耗估算）、`vivado`（跑资源/功耗 Tcl 报告）、`vitis-run`（跑 PL testbench）。

理解这一点后你会发现：Makefile 的复杂其实只是「在合适的目录、用合适的模式调用 `v++` 或 `aiesimulator`，并把产物搬到下一个目标能找到的位置」。

#### 4.3.2 核心流程

完整的「源码 → 镜像」调用链：

```text
design/aie/graph.cpp
   └─(v++ -c --mode aie, --config aiecompiler.cfg)─▶ libadf.a + Work/(含 aie_control_xrt.cpp)

design/pl/dma_pkt_router.cpp
   └─(v++ -c --mode hls, --config pkt_router_config.cfg)─▶ dma_pkt_router.xo (+ system.cfg)

libadf.a + aie_control_xrt.cpp + design/host/*.cpp
   └─(make -C design/host, aarch64 交叉编译 + 链接 -ladf_api_xrt 等)─▶ sar_backproject.elf

dma_pkt_router.xo + libadf.a + system.cfg
   └─(v++ -g -l, --config system.cfg)─▶ sar_backproject_${TARGET}.xsa

XSA + libadf.a + sar_backproject.elf + 测试CSV + run_script + xrt.ini + DTB/UBOOT/IMAGE/ROOTFS
   └─(v++ -p, --package.*)─▶ SD 卡启动镜像（sd_card.img 等）
```

仿真分支（不产出镜像，用于验证）：

```text
libadf.a
   └─(aiesimulator [--profile --dump-vcd aie])─▶ aiesim 输出 / aie.vcd
aie.vcd
   └─(vcdanalyze --xpe)─▶ 功耗 .xpe
XSA
   └─(vivado -mode batch -source report_metrics.tcl)─▶ 资源/功耗报告
```

#### 4.3.3 源码精读

**AIE 编译**：[Makefile:233-240](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L233-L240)。注意它通过 `--config aiecompiler.cfg` 引入栈大小等选项（见 [aiecompiler.cfg:1-3](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/aiecompiler.cfg)），并通过 `--include` 同时引入项目源码目录和 DSP 库的三个子目录（`L1/src/aie`、`L1/include/aie`、`L2/include/aie`）——这印证了 u1-l2 提到的「依赖外部 DSP 库」。入口源文件是 `design/aie/graph.cpp`。

**PL HLS 综合**：[Makefile:200-223](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L200-L223)。这里有两件事：
1. 前半段（[L201-L217](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L201-L217)）是 shell 脚本，用 `grep '^#define AIE_SWITCHES' common.h | awk '{print $3}'` 读出 `AIE_SWITCHES`，再用 `seq 0 $((AIE_SWITCHES-1))` 循环写出 `nk=`（内核实例数）、`stream_connect=`（AIE→PL 流连接）、`sp=`（PL→DDR 端口）三组行。这就是 `system.cfg` 的自动生成过程。
2. 后半段调 `v++ -c --mode hls --config pkt_router_config.cfg`，配置文件里写明了顶层函数 `dma_pkt_router`、时钟 `312.5MHz`、综合成 `.xo`（见 [pkt_router_config.cfg:1-8](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/pkt_router_config.cfg)）。

**主机交叉编译**：根 [Makefile:246-250](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L246-L250) 只是 `$(MAKE) -C design/host/ BUILD_DIR=... -B` 进入子 Makefile。真正的编译细节在子 Makefile：
- 编译选项 [design/host/Makefile:15-23](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile#L15-L23)：`-std=c++17`、`--sysroot` 指向 Yocto sysroot、`-D__AIE_ARCH__=10`（声明 AIE1 架构）、`-fopenmp`，并 include 了 `xrt/`、`aietools/include`、DSP 库头文件。
- 链接选项 [design/host/Makefile:25-39](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/Makefile#L25-L39)：`-ladf_api_xrt`、`-lxrt_coreutil`、`-lxilinxopencl`、`-lpthread`、`-lrt` 等，全是驱动 AIE/PL 所需的运行时库。

> 小术语：**交叉编译**（cross compile）指在 x86 主机上编译出给 ARM 架构运行的程序。这里的 `CXX` 由 `env_setup.sh` source 的 `environment-setup-cortexa72-cortexa53-poky-linux` 脚本设置成 ARM 版 g++，所以最终 `sar_backproject.elf` 能在 VCK190 的 Cortex-A72 上跑。

**系统链接（生成 XSA）**：[Makefile:257-266](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L257-L266)。`v++ -g -l` 把 PL 的 `.xo` 和 AIE 的 `libadf.a` 按 `system.cfg` 描述的连接关系链接成单一镜像，`--save-temps` / `--verbose` 用于调试。

**打包（生成 SD 镜像）**：[Makefile:91-109](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L91-L109)。`v++ -p` 通过一堆 `--package.*` 选项把引导链（`BL31_ELF`/`UBOOT`/`IMAGE`/`ROOTFS`）、DTB、主机 elf、测试数据、运行脚本、`xrt.ini` 全部打进 SD 卡镜像；`--package.defer_aie_run` 表示 AIE 图不在启动时自动跑，而由 host 程序显式启动。注意 DTB 只在 `hw` 时打包（[Makefile:86-90](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L86-L90)），因为 QEMU 仿真用自定义 DTB 会导致内核 panic。

**AIE 仿真**：[Makefile:147-156](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L147-L156)（无 profile）、[L160-L169](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L160-L169)（带 profile + vcd）。注意它用 `--pkg-dir=${...}/Work` 指向 AIE 编译产物，并 `--input-dir` 指向 PL 仿真输出目录——这就是为什么 `aiesim` 依赖 `libadf.a`，且与 PL 仿真存在数据往来。

**功耗/资源度量**：[Makefile:174-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L174-L184) 用 `vcdanalyze --xpe` 把 `aie.vcd` 转成功耗文件；[Makefile:186-195](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L186-L195) 用 `vivado` 批量跑 `report_metrics.tcl`，因此 `metrics` 目标要求 `TARGET=hw`（注释 [Makefile:186](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L186) 明确写了 `TARGET must be HW`）。

#### 4.3.4 代码实践

**实践目标**：把每条间接规则对应到具体的 `v++` / `aiesimulator` 调用，建立「目标 ↔ 工具 ↔ 模式」的直觉。

**操作步骤（源码阅读型实践）**：

1. 在根 Makefile 里搜索所有 `v++` 调用，按下表归类（答案见预期结果）：

   | 规则位置 | `v++` 模式/标志 | 作用 |
   |----------|----------------|------|
   | [L233-L240](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L233-L240) | `-c --mode aie` | 编译 ADF 图 → `libadf.a` |
   | [L220-L222](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L220-L222) | `-c --mode hls` | 综合 PL 内核 → `.xo` |
   | [L260-L266](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L260-L266) | `-g -l` | 链接 → `XSA` |
   | [L95-L109](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L95-L109) | `-p` | 打包 → SD 镜像 |

2. 再搜索所有 `aiesimulator` / `vcdanalyze` / `vivado` 调用，确认它们都集中在仿真与度量目标里，**不参与** `package` 的主链路。

3. 选一个练习目标 `make TARGET=sw_emu aiesim`，预测它会被哪条校验挡住（提示：见 4.1.3）。

**需要观察的现象**：
- 四种 `v++` 模式刚好对应「编译 AIE / 综合 PL / 链接 / 打包」四个生命周期阶段，井然有序。
- 仿真类工具（`aiesimulator`、`vcdanalyze`）只读 `libadf.a` / `aie.vcd`，不会修改主链路产物。
- `make TARGET=sw_emu aiesim` 会被 [Makefile:59-70](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L59-L70) 挡下。

**预期结果**：你会清楚看到 `v++` 是「一条命令贯穿全流程」的统一入口，不同模式由参数区分；而 `aiesimulator` 等是围绕 `v++` 产物的「验证旁路」。这正是 AMD Vitis 工具链的设计哲学。

> 待本地验证：如果你装了 Vitis，可以尝试 `make -n aie`（`-n` 表示只打印命令不执行），在不真正编译的情况下亲眼看到完整 `v++ -c --mode aie ...` 命令行。

#### 4.3.5 小练习与答案

**练习 1**：`make aiesim` 与 `make aiesim_profile` 产出的差异是什么？后者多了哪些用途？

**参考答案**：`aiesim` 只做功能仿真（[Makefile:147-156](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L147-L156)）；`aiesim_profile` 额外加了 `--profile` 和 `--dump-vcd aie`（[Makefile:163-165](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L163-L165)），既让内核里的 `printf` 能输出到控制台，又生成 `aie.vcd` 供 `aiesim_xpe` 做功耗估算。

**练习 2**：`metrics` 目标为什么强制 `TARGET=hw`？

**参考答案**：`metrics` 跑的是 Vivado 对 XSA 的资源/功耗报告（[Makefile:186-195](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L186-L195)），只有 `hw` 的 XSA 含真实的 PL 比特流和布局布线信息；仿真用的 XSA 不含这些，跑出来的资源/功耗数字没有参考价值。

**练习 3**：PL 综合规则（[Makefile:199-227](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L199-L227)）为什么要把「生成 system.cfg」和「v++ HLS 综合」写在同一条规则里？

**参考答案**：因为 `system.cfg` 里的 `nk=dma_pkt_router:${AIE_SWITCHES}:...` 行的实例数直接取自 `common.h` 的 `AIE_SWITCHES`，与 PL 内核的实例化是同一件事的两面。把它们放在同一条规则里、用同一个 `AIE_SWITCHES` 变量，能保证「综合出的内核实例数」与「连接描述里的实例数」永远一致，避免后续 `v++ -g -l` 链接时因数量不匹配而失败。

---

## 5. 综合实践

**任务**：为根 Makefile 绘制一张完整的「依赖图 + 工具标注」图，并用它解释一次 `make package` 的执行轨迹。

**要求**：

1. 画出一个有向无环图（DAG），节点包括：源码文件（`design/aie/*`、`design/pl/dma_pkt_router.cpp`、`design/host/*`、`common.h`、两个 `.cfg`）、四个间接产物（`libadf.a`、`dma_pkt_router.xo`、`sar_backproject.elf`、`.xsa`）、以及最终产物 `package`（SD 镜像）。
2. 在每条边上标注触发的工具与模式（如 `v++ --mode aie`、`v++ --mode hls`、`make -C design/host`、`v++ -g -l`、`v++ -p`）。
3. 用不同颜色或线型标出「AIE 分支」「PL 分支」「主机分支」，并标出它们在 `XSA` 和 `package` 处的汇合点。
4. 在图下方写出：当你执行 `make package` 时，make 会按什么顺序解析这些依赖、哪些分支可以并行、`package` 最后一步用什么命令把哪些文件打进去。

**验收标准**：
- 图中 `HOST_EXE` 节点必须有来自 `libadf.a`（经 `aie_control_xrt.cpp`）的依赖边，且你能口头解释「为什么」。
- `XSA` 节点必须同时依赖 `libadf.a` 和 `dma_pkt_router.xo`（以及 `system.cfg`）。
- `package` 节点必须列出 `XSA` + `libadf.a` + `sar_backproject.elf` + GOTCHA CSV + `run_script_${TARGET}.sh` + `xrt.ini` 等输入，并标注它由 `v++ -p` 触发。

> 待本地验证：如果你有 Vitis 环境，可在画完图后用 `make -n package TARGET=hw_emu > dryrun.log` 把 dry-run 命令导出，对照你的图逐条核对工具调用顺序。

## 6. 本讲小结

- 本仓库构建由 **`TARGET`（hw/hw_emu/sw_emu）** 和 **`PLATFORM`** 两个全局变量驱动；前者决定运行环境，后者由 `helper_scripts/env_setup.sh` 导出。Makefile 用三道校验拦截非法组合。
- 构建分**直接目标**（`aie`/`pl`/`host`/`package`/`run`/仿真/`metrics`/`clean` 等 `.PHONY` 动作）和**间接目标**（`libadf.a`/`dma_pkt_router.xo`/`sar_backproject.elf`/`.xsa` 等真实产物文件），前者桥接到后者。
- 四个间接产物的依赖链是：AIE 源码 → `libadf.a`；PL 源码 → `dma_pkt_router.xo`（同时自动生成 `system.cfg`）；两者 → `XSA`（`v++ -g -l`）；再加上 host elf → `package`（`v++ -p` 打包成 SD 镜像）。
- **host 依赖 AIE** 是因为 AIE 编译器会自动生成 host 控制图所必需的 `aie_control_xrt.cpp`（及 XRT 运行库），host 没有它就无法编译链接。
- 全流程几乎只用 `v++` 一个命令的四种模式（`--mode aie` / `--mode hls` / `-l` / `-p`），外加 `aiesimulator` 等仿真旁路工具。
- 产物按 `build/${TARGET}/...` 分目录隔离，且 `system.cfg`、`aie_control_xrt.cpp` 都是构建时自动生成、不入库的派生文件。

## 7. 下一步学习建议

本讲只讲了「怎么搭」，没有讲「主机程序到底怎么控制 AIE 和 PL」。建议：

- 接下来读 **u1-l4（全局配置中心：design/common.h）**，弄清 `PULSES` / `RC_SAMPLES` / `AIE_SWITCHES` / `IMG_SOLVERS` 这些宏如何同时约束三域源码——它们正是本讲里 `grep` 读取的对象。
- 之后再进入**第 2 单元（Versal 平台与 AIE 编程模型）**，补齐 ADF 图、GMIO/PLIO、RTP 等概念，为第 3 单元（主机应用）和第 4 单元（AIE 图拓扑）做铺垫。
- 想提前感受主机控制流，可先扫一眼 [design/host/main.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp) 的阶段划分，但不必深究，留到 u3-l1 系统讲。
