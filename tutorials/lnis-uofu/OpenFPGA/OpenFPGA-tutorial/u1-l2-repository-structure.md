# 仓库目录结构解析

## 1. 本讲目标

上一讲我们已经知道 OpenFPGA 是一个「帮你造 FPGA」的开源 EDA 框架。但在动手编译和跑流程之前，你必须先看懂它的仓库长什么样——因为 OpenFPGA 不是单一的可执行文件，而是一个由**核心引擎 + 十几个可复用库 + 第三方子模块 + 流程脚本与数据**组装起来的庞大工程。

学完本讲，你将能够：

1. 说出仓库顶层每个目录（`openfpga/`、`libs/`、`openfpga_flow/`、`vtr-verilog-to-routing/`、`yosys/`、`docs/`、`dev/`）各自的职责。
2. 区分「核心引擎源码 `openfpga/src`」与「可复用支撑库 `libs/`」的不同定位。
3. 在 `openfpga_flow/` 下快速定位流程脚本、架构文件、回归任务与基准测试。
4. 通过 `CMakeLists.txt` 和 `Makefile` 验证你对目录职责的判断（构建系统是目录结构的「证人」）。

## 2. 前置知识

- **源码目录（source tree）**：一个项目在磁盘上的文件组织方式。大型 C++ 项目通常会把「程序入口」「业务逻辑」「可复用库」「数据/配置」分层存放。
- **子模块（git submodule）**：一个 Git 仓库里嵌套引用的另一个 Git 仓库。OpenFPGA 不自己实现综合器（Yosys）和布局布线器（VPR），而是把它们作为子模块引入，所以克隆后这些目录默认是**空的**，需要额外命令拉取。
- **构建系统（build system）**：把源码编译成可执行程序的工具。OpenFPGA 用 **CMake** 生成 Makefile，再用一个顶层 **Makefile** 包装常用命令。
- **引擎（engine）与库（library）**：「引擎」指最终产出 `openfpga` 可执行文件的主程序代码；「库」指被引擎调用、但本身可以独立复用的模块化代码。二者分离是良好工程的标志。

> 阅读建议：本讲不需要你理解任何 C++ 细节，重点是把目录当「地图」来记。每一条结论我都会用构建脚本里的真实行号来佐证。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `README.md` | 项目入口说明，指向编译与文档 | 确认项目定位与外部链接 |
| `CMakeLists.txt` | 顶层 CMake 配置，定义子项目装配顺序 | 从 `add_subdirectory(...)` 反推目录职责 |
| `Makefile` | 包装 CMake 的便捷命令 | 看 `make checkout`、`make compile` 如何串联子模块与源码 |
| `openfpga.sh` | 环境变量与快捷 bash 函数 | 看它引用了哪些 `openfpga_flow/` 子目录 |
| `.gitmodules` | 声明第三方子模块 | 确认 `yosys/`、`vtr-verilog-to-routing/` 是子模块 |
| `VERSION.md` | 单一版本号数据源 | 当前版本 `1.2.4307` |
| `openfpga/src/` | 核心引擎源码 | 按子目录归类引擎能力 |
| `libs/` | 可复用支撑库 | 列出库清单与各自职责 |
| `openfpga_flow/` | 流程脚本、架构、任务、基准 | 识别流程运行所需的全部数据 |

---

## 4. 核心概念与源码讲解

### 4.1 仓库顶层布局总览

#### 4.1.1 概念说明

OpenFPGA 仓库顶层可以划分为 **5 个功能区**：

| 功能区 | 顶层目录 | 一句话职责 |
| --- | --- | --- |
| 核心引擎 | `openfpga/` | 编译产出 `openfpga` 主程序的业务代码 |
| 支撑库 | `libs/` | 13 个可被引擎复用、也可独立使用的库 |
| 流程与数据 | `openfpga_flow/` | Python 流程脚本、架构 XML、回归任务、基准测试 |
| 第三方子模块 | `vtr-verilog-to-routing/`、`yosys/`、`yosys-slang/` | VPR 布局布线器、Yosys 综合器等外部工具 |
| 文档与工程化 | `docs/`、`dev/`、`.github/`、`docker/`、`cmake/` | 文档、开发者工具、CI、容器、CMake 宏 |

顶层还有若干配置/说明文件：`README.md`、`VERSION.md`、`LICENSE`、`CMakeLists.txt`、`Makefile`、`openfpga.sh`、`Dockerfile`、`requirements.txt`、`vcpkg.json` 等。

#### 4.1.2 核心流程

克隆仓库后，读者第一次面对的目录流转大致是：

```text
git clone  →  顶层目录（openfpga/ libs/ openfpga_flow/ ... 子模块目录为空）
            ↓
make checkout   ←—— 拉取 yosys/、vtr-verilog-to-routing/ 等子模块
            ↓
make compile    ←—— CMake 读取顶层 CMakeLists.txt，按顺序编译 vtr → libs → openfpga
            ↓
source openfpga.sh  ←—— 设置 OPENFPGA_PATH，注册 run-task 等快捷函数
            ↓
run-task ...    ←—— 读 openfpga_flow/tasks 下的任务，跑 openfpga_flow/scripts 里的脚本
```

关键点：**子模块目录在克隆后是空的**。本环境里 `vtr-verilog-to-routing/` 和 `yosys/` 的文件数都是 0，必须靠 `make checkout` 才能填充。这是初学者最常踩的坑。

#### 4.1.3 源码精读

**（1）`.gitmodules` 声明三个子模块。**

[.gitmodules:1-9](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/.gitmodules#L1-L9) 告诉我们 `yosys`、`vtr-verilog-to-routing`、`yosys-slang` 是外部仓库的子模块，分别指向 YosysHQ/yosys、verilog-to-routing/vtr-verilog-to-routing、povik/yosys-slang。这就是为什么这些目录默认为空——它们的内容来自别的仓库。

**（2）顶层 `CMakeLists.txt` 是目录职责的「证人」。**

[CMakeLists.txt:56-56](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L56-L56) 把整个工程命名为 `OpenFPGA-tool-suites`（注意是「工具套件」而非单一程序），暗示它是多组件组装的。

真正决定「哪些目录被编译、以什么顺序」的是三条 `add_subdirectory`：

- [CMakeLists.txt:316-316](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L316-L316)：`add_subdirectory(vtr-verilog-to-routing)`，最先编译 VPR（因为 OpenFPGA 依赖它）。
- [CMakeLists.txt:331-331](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L331-L331)：`add_subdirectory(libs)`，编译全部支撑库。
- [CMakeLists.txt:332-332](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L332-L332)：`add_subdirectory(openfpga)`，最后编译核心引擎（它要链接前面的库与 VPR）。

顺序很重要：**VPR → libs → openfpga**，体现了「引擎依赖库、库依赖 VPR」的层次。

**（3）`VERSION.md` 是版本号单一数据源。**

[VERSION.md:1-1](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/VERSION.md#L1-L1) 内容是 `1.2.4307`。[CMakeLists.txt:70-74](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L70-L74) 在编译时读取它并拆成主/次/修订号，这样版本信息只维护一处。

#### 4.1.4 代码实践

**实践目标**：用构建脚本验证你对顶层目录职责的判断，而不是靠记忆。

**操作步骤**：

1. 打开 `CMakeLists.txt`，搜索所有 `add_subdirectory(` 出现的位置，记录它们对应的目录与行号。
2. 打开 `.gitmodules`，列出每个子模块的 `path` 和来源 `url`。
3. 在本环境执行 `ls yosys/ | wc -l` 与 `ls vtr-verilog-to-routing/ | wc -l`。

**需要观察的现象**：

- `add_subdirectory` 只显式编译了 `vtr-verilog-to-routing`、`libs`、`openfpga` 三个目录——其余顶层目录（如 `openfpga_flow/`、`docs/`）**不参与 C++ 编译**，它们是数据/脚本/文档。
- 两个子模块目录的文件数都是 0。

**预期结果**：你得到一张「编译期目录 vs 非编译期目录」的对照表，这与本节的结论一致。若子模块已 checkout，文件数会大于 0。

#### 4.1.5 小练习与答案

**练习 1**：为什么克隆后 `yosys/` 目录是空的？要怎么填充？
**答案**：因为它是 git 子模块（见 `.gitmodules`），克隆不会自动拉取其内容；需要执行 `make checkout`（即 `git submodule update --init --recursive`）来填充。

**练习 2**：顶层 `CMakeLists.txt` 里 `add_subdirectory(libs)` 为什么必须早于 `add_subdirectory(openfpga)`？
**答案**：核心引擎 `openfpga/` 依赖 `libs/` 里的库（如 `libopenfpgashell`、`libarchopenfpga`），被依赖者必须先编译，所以顺序是 `libs` → `openfpga`。

---

### 4.2 `openfpga/`：核心引擎源码

#### 4.2.1 概念说明

`openfpga/` 是产出 `openfpga` 可执行文件的主程序源码所在地。它的内部进一步分成「引擎入口」和「按功能划分的子系统目录」。理解这些子系统目录，等于提前认识了后续讲义会逐一展开的核心模块（fabric 构建、比特流、网表输出等）。

`openfpga/` 顶层只有两个东西：`CMakeLists.txt`（定义引擎如何编译）和 `src/`（全部源码）。

#### 4.2.2 核心流程

引擎源码 `openfpga/src/` 的子目录按「一条 FPGA 生成流程的阶段」组织：

```text
main.cpp              ← 程序入口
base/                 ← shell、context、命令注册、流程管理（流程的「骨架」）
vpr_wrapper/          ← 把 VPR 的能力封装成 OpenFPGA 命令
annotation/           ← 在不修改 VPR 的前提下，给 VPR 结果打标注
fabric/               ← 构建 fabric 模块图（ModuleManager）
mux_lib/              ← 多路选择器库
repack/               ← 逻辑→物理 pb 重打包
tile_direct/          ← tile 间直连
fpga_bitstream/       ← 比特流生成
fpga_verilog/         ← Verilog 网表生成
fpga_spice/           ← SPICE 网表生成
fpga_sdc/             ← SDC 时序约束生成
utils/                ← 跨子系统复用的工具函数
```

另有 `openfpga_shell.i`（SWIG 接口文件，用于生成 Python/Tcl 绑定）和 `ctag_src.sh`（生成 ctags 索引的脚本）。仅 `openfpga/src/` 下就有约 **452 个 `.cpp/.h` 文件**，规模很大。

#### 4.2.3 源码精读

**程序入口**：[openfpga/src/main.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/main.cpp) 是整个引擎的 `main()` 所在地，是阅读引擎代码的起点。

**子系统目录的职责**（这里给出定位，具体实现会在进阶/专家层讲义精读）：

| 子目录 | 职责 | 代表头文件 |
| --- | --- | --- |
| `base/` | shell 入口、OpenfpgaContext 全局数据、命令注册模板 | `openfpga/src/base/openfpga_shell.cpp` |
| `fabric/` | ModuleManager 与 fabric 构建 | `openfpga/src/fabric/module_manager.h` |
| `fpga_bitstream/` | 两级比特流模型与生成 | `openfpga/src/fpga_bitstream/fabric_bitstream.h` |
| `fpga_verilog/` | fabric Verilog 网表写出 | `openfpga/src/fpga_verilog/verilog_api.cpp` |
| `annotation/` | VPR 结果标注子系统 | `openfpga/src/annotation/device_rr_gsb.h` |

> 提示：这些子目录的命名与上一讲提到的四大部分（FPGA-Verilog / FPGA-Bitstream / FPGA-SPICE / FPGA-SDC）几乎一一对应，只是这里多了「引擎骨架」`base/` 和「构建」`fabric/`。

#### 4.2.4 代码实践

**实践目标**：把 `openfpga/src/` 的子目录与「流程阶段」对应起来。

**操作步骤**：

1. 列出 `openfpga/src/` 下所有子目录。
2. 针对每个子目录，猜一个「它对应 FPGA 生成流程的哪个阶段」。
3. 打开 `openfpga/src/base/main.cpp` 附近，确认入口在 `base/` 之外（它在 `src/` 根下），而 shell/context 在 `base/` 内。

**需要观察的现象**：子目录名大多以 `fpga_*` 或业务名词开头，能直观反映职责。

**预期结果**：得到一张「子目录 → 流程阶段」映射表。例如 `fpga_verilog/ → 网表生成阶段`、`fabric/ → fabric 构建阶段`。

#### 4.2.5 小练习与答案

**练习 1**：引擎的程序入口 `main.cpp` 在哪个目录？
**答案**：直接在 `openfpga/src/main.cpp`，即 `src/` 根目录下，不在任何子目录里。

**练习 2**：如果你想找「Verilog 网表生成」的代码，应该进哪个子目录？
**答案**：`openfpga/src/fpga_verilog/`。

---

### 4.3 `libs/`：可复用支撑库

#### 4.3.1 概念说明

`libs/` 存放 **13 个可复用库**。它们与 `openfpga/src/` 的区别在于定位：库是**模块化、低耦合**的，理论上可以被其他项目复用；而 `openfpga/src/` 是把这些库粘合起来、实现完整流程的引擎。

把通用能力下沉为库，是 OpenFPGA 控制大型代码库复杂度的关键手段。例如端口解析、二进制解码、XML 解析这些「到处都要用」的能力，都放在 `libopenfpgautil` 里，避免在引擎各处重复实现。

#### 4.3.2 核心流程

`libs/` 自身有一个 [libs/CMakeLists.txt](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/CMakeLists.txt)，用一串 `add_subdirectory(...)` 把 13 个库逐个纳入编译。库之间的依赖关系（例如 `libarchopenfpga` 依赖 `libopenfpgautil`）在各库自己的 `CMakeLists.txt` 里声明。

#### 4.3.3 源码精读

**库清单与职责**（共 13 个）：

| 库名 | 职责 |
| --- | --- |
| `libarchopenfpga` | openfpga_arch 数据模型与 XML 解析（架构库） |
| `libclkarchopenfpga` | 时钟网络架构数据模型 |
| `libopenfpgashell` | `Shell<T>` 命令框架（命令注册、解析、执行） |
| `libopenfpgautil` | 通用工具：BasicPort 端口、二进制解码、pb 路径解析、通配符 |
| `libfpgabitstream` | 比特流数据结构（device 级 BitstreamManager） |
| `libfabrickey` | fabric key 读写 |
| `libpcf` | 物理约束：pin constraints、io pin table 等 |
| `libbusgroup` | 总线到引脚映射 |
| `libmif` | 存储器初始化文件（Memory Initialization File） |
| `libnamemanager` | 模块名 / IO 名映射 |
| `libtileconfig` | tile 配置 |
| `libini` | ini 配置文件读写 |
| `libopenfpgacapnproto` | capnproto 序列化（unique blocks 二进制缓存） |

仅 `libs/` 下就有约 **258 个 `.cpp/.h` 文件**，规模同样可观。

**装配证据**：[libs/CMakeLists.txt](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/CMakeLists.txt#L1-L14) 开头就是一连串 `add_subdirectory(libini)`、`add_subdirectory(libopenfpgashell)` …… 把上述 13 个库依次纳入构建。

> 提示：库的命名遵循 `lib<主题>` 约定，从名字基本能猜出职责。这些库会在后续进阶层（u5 架构数据模型、u10 支撑库）逐一精读。

#### 4.3.4 代码实践

**实践目标**：验证「库是可复用、低耦合」的设计。

**操作步骤**：

1. 列出 `libs/` 下全部子目录，确认共 13 个。
2. 打开 `libs/CMakeLists.txt`，数 `add_subdirectory` 的数量是否与目录数一致。
3. 任选一个库（如 `libopenfpgautil`），观察它是否有 `src/` 子目录与自己的 `CMakeLists.txt`，即「每个库是独立编译单元」。

**需要观察的现象**：每个库目录内部结构相似（通常含 `src/`、`CMakeLists.txt`），彼此独立。

**预期结果**：确认 13 个库各自独立可编译，构成引擎的「积木」。

#### 4.3.5 小练习与答案

**练习 1**：端口解析、二进制解码这类「到处都要用」的能力，放在哪个库？
**答案**：`libopenfpgautil`（通用工具库）。

**练习 2**：`libarchopenfpga` 与 `openfpga/src/` 里某个子目录职责高度相关，是数据与解析的关系。请指出对应关系。
**答案**：`libarchopenfpga` 提供 openfpga_arch 的**数据模型与 XML 解析**（只读数据），而 `openfpga/src/` 引擎则**消费**这些数据来驱动流程。

---

### 4.4 `openfpga_flow/`：流程脚本与数据

#### 4.4.1 概念说明

如果说 `openfpga/` 和 `libs/` 是「代码」，那么 `openfpga_flow/` 就是「**怎么用这些代码**」——它存放驱动整个 FPGA 生成流程的 Python 脚本、各种架构/配置 XML、回归测试任务和基准设计。绝大多数用户的日常操作（跑任务、改架构、加回归测试）都集中在这个目录。

> 重要区分：`openfpga_flow/` 下的内容**不参与 C++ 编译**（顶层 `CMakeLists.txt` 没有 `add_subdirectory(openfpga_flow)`）。它是数据与脚本层。

#### 4.4.2 核心流程

`openfpga_flow/` 内部子目录及职责：

```text
scripts/                    ← Python 流程脚本：run_fpga_task.py（批量任务）、run_fpga_flow.py（单次流程）等
tasks/                      ← 回归测试任务（每个任务一个 config/task.conf）
benchmarks/                 ← 基准设计（and2、MCNC、vtr_benchmark 等用户 Verilog）
vpr_arch/                   ← VPR 架构 XML（描述器件结构）
openfpga_arch/              ← openfpga_arch XML（描述电路级物理实现）
openfpga_shell_scripts/     ← .openfpga 脚本示例（canonical 流程脚本在这里）
openfpga_cell_library/      ← 标准单元库（Verilog/SPICE 子电路）
openfpga_simulation_settings/ ← 仿真设置 XML
regression_test_scripts/    ← 回归测试 shell 脚本（被 CI 调用）
arch_bitstreams/            ← 已生成的架构比特流样本
fabric_keys/                ← fabric key 样本
openfpga_yosys_techlib/     ← Yosys 工艺库
tech/                       ← 工艺相关数据
misc/                       ← 杂项辅助
docs/                       ← 流程相关文档
```

#### 4.4.3 源码精读

**（1）`openfpga.sh` 指明了脚本与任务目录的位置。**

[openfpga.sh:15-16](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L15-L16) 设置了两个关键环境变量：

- `OPENFPGA_SCRIPT_PATH="${OPENFPGA_PATH}/openfpga_flow/scripts"`，即脚本目录。
- `OPENFPGA_TASK_PATH="${OPENFPGA_PATH}/openfpga_flow/tasks"`，即任务目录。

这说明 `openfpga.sh` 提供的快捷函数全都依赖 `openfpga_flow/` 的这两个子目录。

**（2）快捷函数把请求转发给 Python 脚本。**

[openfpga.sh:66-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L66-L68) 的 `run-task` 实际上就是调用 `run_fpga_task.py`：

```bash
run-task () {
    $PYTHON_EXEC $OPENFPGA_SCRIPT_PATH/run_fpga_task.py "$@"
}
```

类似地，[openfpga.sh:82-84](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L82-L84) 的 `run-flow` 调用的是 `run_fpga_flow.py`。所以 `openfpga_flow/scripts/` 是流程的「大脑」。

**（3）两类架构文件分目录存放。**

- [openfpga_flow/vpr_arch/](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch) 存放 VPR 架构 XML（如 `k4_N4_tileable_40nm.xml`），描述器件结构。
- [openfpga_flow/openfpga_arch/](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch) 存放 openfpga_arch XML（如 `k4_N4_40nm_cc_openfpga.xml`），描述电路级实现。

这两套文件的职责边界是下一单元（u3）的核心内容，本讲只需记住「它们分两个目录」。

**（4）回归任务按特性分类。**

[openfpga_flow/tasks/](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks) 下分为 `basic_tests/`、`fpga_bitstream/`、`fpga_verilog/`、`fpga_spice/`、`fpga_sdc/`、`compilation_verification/`、`benchmark_sweep/`、`quicklogic_tests/`、`template_tasks/` 等类别，每个类别下又是一个个带 `config/task.conf` 的任务。

#### 4.4.4 代码实践

**实践目标**：把 `openfpga_flow/` 的子目录与「跑流程时用到的资源」对应起来。

**操作步骤**：

1. 列出 `openfpga_flow/scripts/` 下的 `.py` 文件，找到 `run_fpga_task.py` 与 `run_fpga_flow.py`。
2. 任选一个任务，例如 `openfpga_flow/tasks/basic_tests/`，观察它的子目录结构。
3. 对比 `openfpga_flow/vpr_arch/` 与 `openfpga_flow/openfpga_arch/` 里文件名的差异（注意 `cc`、`bank`、`frame` 等后缀）。

**需要观察的现象**：脚本目录里 Python 脚本是流程入口；任务目录里每个任务都有 `config/task.conf`；两套架构文件命名风格不同（vpr_arch 多含 `tileable`，openfpga_arch 多含配置协议后缀如 `cc`）。

**预期结果**：你能说出「跑一个任务需要：scripts 里的脚本 + tasks 里的 task.conf + vpr_arch + openfpga_arch + benchmarks 里的设计」。

#### 4.4.5 小练习与答案

**练习 1**：`run-task` 这个 bash 函数背后调用的是哪个 Python 脚本？
**答案**：`openfpga_flow/scripts/run_fpga_task.py`（见 `openfpga.sh` 第 67 行）。

**练习 2**：VPR 架构 XML 和 openfpga_arch XML 分别放在哪两个目录？
**答案**：分别在 `openfpga_flow/vpr_arch/` 和 `openfpga_flow/openfpga_arch/`。

---

## 5. 综合实践

**任务**：在仓库根目录下制作一张**目录树速查表（cheat sheet）**，作为你后续阅读源码和跑流程时的导航。

**操作步骤**：

1. 在仓库根目录执行目录列举（例如 `ls openfpga/src`、`ls libs`、`ls openfpga_flow`）。
2. 用 Markdown 表格记录下列 6 个关键路径，分别写出「它存放什么」「典型文件举例」「是否参与 C++ 编译」：

   | 路径 | 存放什么 | 典型文件举例 | 是否编译 |
   | --- | --- | --- | --- |
   | `openfpga/src` | 核心引擎源码（按子系统分目录） | `main.cpp`、`base/`、`fabric/` | 是 |
   | `libs` | 13 个可复用支撑库 | `libopenfpgautil`、`libarchopenfpga` | 是 |
   | `openfpga_flow/scripts` | 流程 Python 脚本 | `run_fpga_task.py`、`run_fpga_flow.py` | 否（脚本） |
   | `openfpga_flow/openfpga_arch` | openfpga_arch XML（电路级物理实现） | `k4_N4_40nm_cc_openfpga.xml` | 否（数据） |
   | `openfpga_flow/vpr_arch` | VPR 架构 XML（器件结构） | `k4_N4_tileable_40nm.xml` | 否（数据） |
   | `openfpga_flow/tasks` | 回归测试任务（每个含 `config/task.conf`） | `basic_tests/full_testbench/...` | 否（配置） |

3. 在表下用一句话写出：当你想「跑一个 FPGA 设计流」时，需要从这 6 个路径中的哪几个各取什么。

**预期结果**：得到一张可直接打印的速查表，并且能口述「跑流程 = scripts 的脚本 + tasks 的 task.conf + vpr_arch + openfpga_arch + benchmarks 的设计，由 openfpga/src 编译出的引擎来执行」。若你尚未编译引擎，可标注「待本地验证：先 `make compile` 生成 `openfpga` 二进制」。

## 6. 本讲小结

- 仓库顶层分 5 大功能区：核心引擎 `openfpga/`、支撑库 `libs/`、流程与数据 `openfpga_flow/`、第三方子模块（`vtr-verilog-to-routing/`、`yosys/`、`yosys-slang/`）、文档与工程化（`docs/`、`dev/`、`.github/` 等）。
- 顶层 `CMakeLists.txt` 的三条 `add_subdirectory`（vtr → libs → openfpga）是目录职责与编译依赖顺序的权威证据；`openfpga_flow/`、`docs/` 等不参与 C++ 编译。
- `openfpga/src/` 按流程阶段拆成 `base/`、`fabric/`、`fpga_bitstream/`、`fpga_verilog/`、`fpga_spice/`、`fpga_sdc/`、`annotation/` 等子系统目录，约 452 个源文件。
- `libs/` 是 13 个低耦合、可复用的库（如 `libopenfpgautil`、`libarchopenfpga`、`libopenfpgashell`），是引擎的「积木」。
- `openfpga_flow/` 是日常操作中心：`scripts/` 放流程脚本，`tasks/` 放回归任务，`vpr_arch/` 与 `openfpga_arch/` 分别放两套架构 XML，`benchmarks/` 放基准设计。
- 子模块目录克隆后为空，必须 `make checkout` 填充——这是初学者最常踩的坑。

## 7. 下一步学习建议

- **本单元下一讲（u1-l3）**：动手 `make checkout` + `make compile`，把这里的目录结构「跑活」，亲眼看到 `openfpga` 二进制诞生在 `build/` 下。
- **衔接 u2**：编译完成后，进入 `openfpga/src/base/` 与 `openfpga/src/main.cpp`，从引擎入口开始读 shell 启动链路。
- **延伸阅读**：随手翻一翻 `openfpga_flow/tasks/basic_tests/` 里某个任务的 `config/task.conf`，提前感受下一讲会遇到的「任务」概念；这能让你在学 u1-l4 跑流程时更有方向感。
