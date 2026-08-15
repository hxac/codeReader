# msprof 总体架构：C++ collector 与分析脚本的分工

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 msprof 的两大组成部分——C++ 侧 collector（`basic`、`dvvp`）与 Python 分析 wheel（`msprof-0.0.1-py3-none-any.whl`）——各自负责什么、边界在哪里。
2. 对照官方架构设计文档，把文档中的七大核心模块（命令行、开关处理、Host 采集、AICPU 采集、驱动采集、数据处理、Platform 管理）映射到 `src/msprof/collector/dvvp` 下的真实源码目录。
3. 解释安装后 `${ASCEND_INSTALL_PATH}/tools/profiler/profiler_tool/` 目录是怎么来的：从 gitcode 上的 msprof 子仓拉取 → 构建 wheel → 拷入 msprofbin 目录 → 构建期解包 → install 规则释放的完整链路。
4. 独立画出一条「业务进程 → collector → 原始数据 → 分析脚本 → 结果」的数据流图，并为每个环节标注源码位置。

本讲是 u4 单元（msprof 源码解析）的第一讲，只看"骨架"不抠实现细节——msprofbin 入口、profapi 插件、analyze 分析器将分别在 u4-l2、u4-l3、u4-l4 中精读。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（入门单元已建立）：

- **CANN 与昇腾 NPU**：CANN 是华为昇腾 AI 处理器的异构计算架构，包含 runtime、GE（图引擎）、HCCL、ACL 等软件组件。msprof 采集的性能数据很大一部分正是这些 Host 侧软件组件主动上报的。
- **profiling（性能调优）的基本概念**：在 AI 任务运行时记录"哪个算子执行了多久、AICore 利用率多少、内存带宽多少"等指标，用于定位性能瓶颈。本讲会接触到几个术语：
  - **AICore**：昇腾 NPU 上执行矩阵/向量运算的核心单元，其 PMU（性能监控单元）寄存器是硬件指标的数据来源。
  - **AICPU**：Device（NPU）侧的辅助 CPU 进程，负责执行 aicpu 算子与集合通信算子。
  - **Host / Device**：Host 指主机侧（CPU + CANN 软件栈），Device 指 NPU 卡侧。
  - **PMU**：Performance Monitoring Unit，硬件性能计数器。
- **构建体系**：oam-tools 用 `build.sh` 驱动 CMake，产物打成 `.run` 包安装到 CANN 目录的 `tools/` 下（u1-l2、u1-l3 已讲）。本讲会追踪 msprof 组件在这条流水线上的完整路径。
- **动态库分层**：`dlopen` 是运行期加载共享库的系统调用。msprof 的部署形态用了「接口层 so + 实现层 so」的技巧，靠 dlopen 解耦。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [docs/zh/design/profiling/architecture.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md) | 官方架构设计文档：系统总览、七大核心模块、部署图、特性说明，是本讲的"地图" |
| [README.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md) | 项目总 README，L242 起的 msprof 小节给出组件组成与 profiler_tool 的安装位置 |
| [src/msprof/CMakeLists.txt](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/CMakeLists.txt) | msprof 组件的顶层 CMake：挂载 collector、定义头文件目标与安装规则 |
| [src/msprof/collector/](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/CMakeLists.txt) | C++ collector 的总目录，下分 `basic`（OS 抽象层）与 `dvvp`（主体实现） |
| [src/msprof/collector/dvvp/CMakeLists.txt](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt) | dvvp 子目录装配线：列出 `libprofimpl.so` 的全部源文件，是看目录职责的最佳入口 |
| [src/msprof/collector/dvvp/msprofbin/CMakeLists.txt](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/CMakeLists.txt) | `msprof` 命令行可执行文件的构建与安装规则，含 wheel 的打包与解包逻辑 |
| [cmake/build_submodules.cmake](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake) | 构建期从 gitcode 拉取 msprof 子仓并产出 wheel 的脚本 |
| [src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp) | C++ 侧定位并校验 Python 分析脚本的代码——两大组件的"接缝" |
| [scripts/package/oam_tools/oam_tools.xml](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/oam_tools.xml) | .run 包清单，声明 `tools/profiler/profiler_tool` 目录及权限 |
| [scripts/package/oam_tools/scripts/msprof_install.sh](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/msprof_install.sh) | 安装期处理 msprof wheel 的安装脚本 |

## 4. 核心概念与源码讲解

### 4.1 架构设计文档：七大核心模块与数据流

#### 4.1.1 概念说明

msprof 是昇腾的整网性能优化工具。官方设计文档把它拆成七个核心模块，理解这七个模块的职责与协作，就理解了 msprof 的数据流。README 对组件组成的一句话概括是：msprof 由 C++ 侧 collector（`basic`、`dvvp`）和 `msprof` Python wheel（分析脚本）组成。

需要注意：**这七个模块全部属于 C++ collector 侧**；Python 分析 wheel 不在架构图的七个模块里，它是部署图中 collector 流水线末端的消费者（见 4.4 节）。

#### 4.1.2 核心流程

架构文档描述的运行期数据流可以概括为：

```text
用户（多种使能方式：msprof 命令行 / ACL 接口 / acl.json / 环境变量）
   │
   ▼
① msprof 命令行（拉起业务进程）
   │
   ▼
② 开关处理：把差异化输入统一收敛为 ProfileParams 结构
   │
   ├──────────────────────────────┐
   ▼                              ▼
③ Host 数据采集                 ⑤ Device 驱动数据采集
   runtime/GE/HCCL 组件回调上报     prof_drv_start 打开驱动通道
   → 无锁 buffer                   → ChannelPoll 后台线程轮询
   → 后台线程取出                  （AICore PMU、算子调度等硬件数据）
   │                              │
   └──────────────┬───────────────┘
   ▼                              ▲
④ AICPU 数据采集 ─────────────────┘（同样经驱动通道回到 Host）
   │
   ▼
⑥ 数据处理：落盘 / 回调上报 / 在线解析（原始性能数据文件）
   │
   ▼
Python 分析 wheel（profiler_tool/analysis/msprof/msprof.py）
   │
   ▼
可视化结果（op summary 等表格）

⑦ Platform 管理：贯穿始终，抽象芯片差异（支持哪些特性、metrics 映射哪些 PMU 事件）
```

关键设计思想有三条：

1. **多入口单收敛**：命令行、ACL 接口、acl.json、环境变量四种使能方式，最终都收敛为统一的 `ProfileParams`，后续采集链路只面向这个结构编程。
2. **上报与落盘解耦**：组件上报数据写入无锁缓冲 buffer，后台线程异步取出处理，保证上报接口低开销（profiling 打开时对业务性能的膨胀要小）。
3. **Platform 抽象隔离芯片差异**：某芯片支持哪些采集特性、某组 metrics 对应哪些 PMU 事件，都由 Platform 层回答，新增芯片符合开闭原则。

#### 4.1.3 源码精读

功能概述与整体定位：

- [docs/zh/design/profiling/architecture.md:L1-L5](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L1-L5) —— 文档开头给出 msprof 的功能概述：接收 runtime/GE/HCCL 等 Host 侧软件组件主动上报的数据，同时通过驱动提供的 profiling channel 采集 Device 侧软件（AICPU）和硬件数据（stars 任务调度、AICore metrics 等），芯片形态差异通过 Platform 抽象隔离。

七个模块的职责声明（每段都是「组件职责 / 核心流程 / 设计考量」三段式）：

- [docs/zh/design/profiling/architecture.md:L12-L16](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L12-L16) —— ① msprof 命令行：可执行工具，拉起 app 或训练/推理脚本并设置采集参数。
- [docs/zh/design/profiling/architecture.md:L18-L22](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L18-L22) —— ② 开关处理：多入口收敛为统一 `ProfileParams`。
- [docs/zh/design/profiling/architecture.md:L24-L28](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L24-L28) —— ③ Host 数据采集：组件注册回调 → 组装上报 → 无锁 buffer → 后台线程取数据。
- [docs/zh/design/profiling/architecture.md:L30-L34](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L30-L34) —— ④ AICPU 数据采集：接口封装与 Host 采集一致，数据经 profiling driver 回到 Host。
- [docs/zh/design/profiling/architecture.md:L36-L40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L36-L40) —— ⑤ Device 驱动数据采集：`prof_drv_start` 打开驱动通道，后台线程 `ChannelPoll` 轮询，不同类型数据封装到不同 profiling channel。
- [docs/zh/design/profiling/architecture.md:L42-L46](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L42-L46) —— ⑥ 数据处理：落盘、回调上报、在线解析。
- [docs/zh/design/profiling/architecture.md:L48-L52](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L48-L52) —— ⑦ Platform 管理：虚接口 + `PLATFORM_REGISTER` 反射注册。

部署形态（三个 so 的分层）：

- [docs/zh/design/profiling/architecture.md:L54-L59](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/design/profiling/architecture.md#L54-L59) —— 部署图说明：`msprofiler.so` 提供 acl prof 接口能力；`profapi.so` 是采集的接口层，通过 dlopen 加载 `profimpl.so`；`profimpl.so` 是采集的实现层。不需要 profiling 的生产态可以只部署 profapi.so 不部署 profimpl.so，上层业务 so 仍能正常加载——这是"接口与实现分离"的部署学。

#### 4.1.4 代码实践

**实践目标**：把架构文档的七个模块"对号入座"到源码目录，建立文档→代码的映射能力。

**操作步骤**：

1. 打开 `src/msprof/collector/dvvp/CMakeLists.txt` 的源文件清单（见 4.3 节精读），它像一张"零件明细表"列出了所有参与编译的 `.cpp`。
2. 对照下表（结合各目录命名与 CMake 清单归纳），在磁盘上逐一 `ls` 确认每个目录存在：

| 架构模块 | 对应源码目录（均在 `src/msprof/collector/dvvp/` 下） | 依据 |
| --- | --- | --- |
| ① msprof 命令行 | `msprofbin/`（入口 `src/msprof_bin.cpp`） | CMake 将该目录编成名为 `msprof` 的可执行文件 |
| ② 开关处理 | `task_handle/`（`prof_params_adapter.cpp`、`prof_manager.cpp`）+ `msprofbin/src/msprof_params_adapter.cpp` | 目录名与参数适配类名 |
| ③ Host 数据采集 | `msprof/engine/`、`msprof/msproftx/`、`message/` | `prof_acl_mgr.cpp`、`msprof_tx_manager.cpp`、`codec.cpp` 等上报链路源文件 |
| ④ AICPU 数据采集 | `profimpl/aicpu/` | `prof_aicpu_api.cpp`、`prof_hal_plugin.cpp` |
| ⑤ Device 驱动数据采集 | `driver/`、`transport/` | `ai_drv_prof_api.cpp`（驱动通道）、`prof_channel.cpp` |
| ⑥ 数据处理 | `analyze/`、`transport/`（uploader 系列）、`msprof/engine/src/uploader_dumper.cpp` | 分析器与落盘/上传源文件 |
| ⑦ Platform 管理 | `common/platform/`、`profimpl/platform/` | `platform.cpp` 与十余个 `*_platform.cpp` 芯片平台实现 |

3. 注意 `profimpl/platform/` 下 `modena_platform.cpp`、`cloud_platform.cpp`、`mdc_platform.cpp` 等文件名——它们正是 ⑦ 中「PLATFORM_REGISTER 反射注册」的一批具体芯片/形态平台类。

**需要观察的现象**：dvvp 下每个子目录都能在架构文档中找到一句对应的职责描述；没有哪个目录是文档未覆盖的"孤儿"（`acp`、`adprof` 等较新目录文档未逐一展开，可先记为待确认）。

**预期结果**：得到一张如上所示的「模块 → 目录」对照表，后续精读任一模块时可直接定位。

#### 4.1.5 小练习与答案

**练习 1**：架构文档说"多入口单收敛"，具体指什么？收敛到哪里？

**答案**：指 msprof 命令行、ACL/Ascend Graph API、acl.json 配置文件、环境变量（`PROFILING_MODE`/`PROFILING_OPTIONS`）等多种使能方式，都先经过各自的参数解析/校验，最终构造出统一的 `ProfileParams` 结构；之后的采集链路只面向 `ProfileParams` 编程，不再关心用户用的是哪种入口（见 architecture.md 第 ② 节）。

**练习 2**：为什么 Host 侧数据上报要用"无锁 buffer + 后台线程"而不是直接落盘？

**答案**：上报接口运行在业务进程的关键路径上（runtime/GE/HCCL 调用），直接落盘会让"打开 profiling"本身成为性能瓶颈，污染被测数据。无锁队列把上报开销压到最低，落盘这种慢操作交给后台线程异步完成——即文档所说的"上报与落盘解耦"。

**练习 3**：生产环境不需要 profiling 时，为什么可以不部署 `profimpl.so`？

**答案**：因为 `profapi.so`（接口层）与 `profimpl.so`（实现层）是分离的，profapi 通过 dlopen 在需要时才加载 profimpl。只部署 profapi 时上层业务组件链接的符号仍然可用，只是真正发起采集时会失败——用"运行期可选"换"部署态裁剪"（见 architecture.md 部署图说明）。

### 4.2 collector 目录结构：basic 与 dvvp 的分工

#### 4.2.1 概念说明

`src/msprof/collector/` 是 C++ collector 的全部家当，只有两个子目录：

- **`basic/`**：极薄的一层，只有 OS 抽象（osal，Operating System Abstraction Layer）。它把线程、内存、文件等操作系统原语封装成统一接口，供上层使用——因为 collector 的代码可能被 Host 侧和 Device 侧两种环境联编（Device 侧环境的系统调用可用面更窄）。
- **`dvvp/`**：collector 主体。dvvp 目录下再分 `msprofbin`（命令行可执行文件）、`profapi`（采集接口层）、`profimpl`（采集实现层）、`msprof`（进程内采集引擎与 msproftx）、`analyze`（原始数据解析）、`driver`/`transport`（驱动通道与数据传输）、`task_handle`、`msprofiler`、`profhal`、`acp`、`adprof`、`app`、`common`、`depend`、`message`、`proto` 等十余个子目录——正好对应 4.1 节的模块映射表。

注意一个容易混淆的点：`collector/dvvp/msprof/` 这个**目录**是 C++ 进程内采集引擎（engine、msproftx、dynamic_profiling 等），与**外部 msprof 子仓**（Python 分析 wheel 的来源，见 4.4 节）是两回事，重名但不同物。

#### 4.2.2 核心流程

collector 的构建组织：

```text
src/msprof/CMakeLists.txt
  └── add_subdirectory(collector)
        └── collector/CMakeLists.txt
              ├── add_subdirectory(basic)   → 只导出 PROF_BASIC_DIR 变量，供 dvvp 引用 osal 源码
              └── add_subdirectory(dvvp)
                    ├── add_subdirectory(acp / adprof / msprof / msprofbin / profapi / msprofiler / profhal)
                    └── 本目录直接定义 libprofimpl.so 目标（汇总所有子模块源文件）
```

产物形态（结合部署图）：

- `msprof`（可执行文件，来自 msprofbin）—— 架构模块 ①
- `libprofapi.so`（接口层）—— 部署图中的 profapi.so
- `libmsprofiler.so`（acl prof 接口能力）—— 部署图中的 msprofiler.so
- `libprofimpl.so`（实现层，CMake 目标名 `profimpl_fwk_share`）—— 部署图中的 profimpl.so

#### 4.2.3 源码精读

- [src/msprof/collector/CMakeLists.txt:L17-L18](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/CMakeLists.txt#L17-L18) —— collector 总装配：仅两行 `add_subdirectory(basic)` 与 `add_subdirectory(dvvp)`，说明 collector 就是 basic + dvvp 两块。
- [src/msprof/collector/basic/CMakeLists.txt:L17](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/basic/CMakeLists.txt#L17) —— basic 目录的全部构建逻辑只有一行：把本目录路径设为父作用域变量 `PROF_BASIC_DIR`。basic 本身不产出目标，只是让 dvvp 的 CMake 能引用 `${PROF_BASIC_DIR}/osal/osal.c` 等源文件。
- [src/msprof/collector/basic/osal/](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/basic/osal/osal.c) —— osal 目录含 `osal.c`、`osal_linux.c`、`osal_linux_mem.c`、`osal_thread.h` 等，是对 Linux 线程/内存/文件原语的薄封装。
- [src/msprof/collector/dvvp/CMakeLists.txt:L17-L27](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt#L17-L27) —— dvvp 开头的「device 侧联编场景守卫」注释与 7 个 `add_subdirectory`。这个守卫说明：本目录可能被 device 侧单独作为联编入口，因此要先 include 补齐依赖的公共 cmake 模块。
- [src/msprof/collector/dvvp/CMakeLists.txt:L37-L98](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt#L37-L98) —— `profimplCpp` 源文件清单的开头部分：`analyze/src/analyzer*.cpp`（⑥ 数据处理的解析器）、`common/` 公共设施、`driver/channel/ai_drv_prof_api.cpp`（⑤ 驱动通道）、`profimpl/collect/job_wrapper/src/` 下 30 余个 `prof_*_job.cpp`（各类采集任务：aicore、aicpu、l2cache、memory、互联带宽等，与架构文档"特性功能介绍"一一呼应）。
- [src/msprof/collector/dvvp/CMakeLists.txt:L205-L209](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt#L205-L209) —— 定义共享库目标 `profimpl_fwk_share`。
- [src/msprof/collector/dvvp/CMakeLists.txt:L236-L240](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt#L236-L240) —— `OUTPUT_NAME` 在 Linux 下设为 `profimpl`，即产出 `libprofimpl.so`，对应部署图中的实现层。
- [src/msprof/collector/dvvp/CMakeLists.txt:L282-L284](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/CMakeLists.txt#L282-L284) —— `libprofimpl.so` 的 install 规则，装入 `.run` 包的库目录。

#### 4.2.4 代码实践

**实践目标**：用"数源文件"的方式验证 basic/dvvp 的厚薄分工，并认识 `job_wrapper` 采集任务族。

**操作步骤**：

1. 在仓库根目录执行（本实践为源码阅读型，无需昇腾设备）：

   ```bash
   ls src/msprof/collector/basic/osal/
   ls src/msprof/collector/dvvp/profimpl/collect/job_wrapper/src/ | head -40
   ```

2. 数一数 `job_wrapper/src/` 下 `prof_*_job.cpp` 文件的数量，并与架构文档「特性功能介绍」（architecture.md L67 起的动态 Profiling、task-time、AICore metrics、l2、memory、互联带宽等小节）逐个对号。

**需要观察的现象**：`basic/osal` 只有约 8 个文件；`job_wrapper/src` 下有 30 个左右的 job 源文件，文件名直接表明采集内容（如 `prof_aicore_job.cpp`、`prof_aicpu_job.cpp`、`prof_l2cache_job.cpp`、`prof_inter_connection_job.cpp`）。

**预期结果**：basic 是"一个 osal 目录 + 一行 CMake"的极薄抽象层；dvvp 集中了几乎全部采集实现，其中 `job_wrapper` 是"每类采集指标一个 job 类"的任务族，与文档特性小节基本一一对应（个别新 job 如 `prof_adprof_job.cpp` 文档未展开，待确认）。

#### 4.2.5 小练习与答案

**练习 1**：`basic` 目录的 CMakeLists 为什么只有一行 `set(... PARENT_SCOPE)`？

**答案**：basic 不需要产出独立的构建目标，它的价值是提供 osal 源文件与头文件路径。通过把 `PROF_BASIC_DIR` 导出到父作用域，dvvp 的 `profimplCpp`/`msprofbinCpp` 清单里可以直接写 `${PROF_BASIC_DIR}/osal/osal.c` 把 osal 源码编进各自目标——这是一种"目录即依赖"的轻量做法。

**练习 2**：`collector/dvvp/msprof/` 目录与 gitcode 上的 `Ascend/msprof` 子仓是什么关系？

**答案**：没有直接关系。`collector/dvvp/msprof/` 是本仓内的 C++ 进程内采集引擎（含 engine、msproftx、dynamic_profiling 等子模块，被编进 `libprofimpl.so` 和 `msprofbin`）；而 `Ascend/msprof` 子仓是外部 Python 分析脚本的来源，构建期被克隆到 `submodule/msprof` 并打成 wheel（见 4.4 节）。两者重名纯属历史命名。

### 4.3 src/msprof/CMakeLists.txt：组件顶层装配与安装规则

#### 4.3.1 概念说明

`src/msprof/CMakeLists.txt` 是 msprof 组件在 u1-l2 所讲"根 CMakeLists 总装配线"上的挂载点。它做三件事：

1. 定义头文件接口目标 `msprof_headers`，统一暴露 `inc/`、`inc/toolchain/`、`inc/external/` 三个头文件目录；
2. `add_subdirectory(collector)` 挂载全部 C++ 构建（真正干活的是 collector 下的 CMake）；
3. 定义 `profiling` 自定义目标与安装规则。

另外它还内置了一个 `protobuf_generate` 函数，负责把 `proto/` 下的 `.proto` 文件用 protoc 编译成 C++ 源码——msprof 内部数据结构（如 `profiler.proto`）的序列化都靠它。

#### 4.3.2 核心流程

```text
根 CMakeLists.txt（u1-l2 已讲，探测 CANN 目录、拉 cann-cmake）
  └── add_subdirectory(src/msprof)
        ├── msprof_headers（INTERFACE 目标：头文件路径）
        ├── add_subdirectory(collector)        → 产出 msprof / libprofimpl.so / libprofapi.so / libmsprofiler.so
        ├── add_custom_target(profiling)       → hiprof 联编入口（把本目录拷到安装配置目录再执行 build_for_hiprof.sh）
        └── install(DIRECTORY .../profiling)   → 装包规则
```

#### 4.3.3 源码精读

- [src/msprof/CMakeLists.txt:L21-L80](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/CMakeLists.txt#L21-L80) —— `protobuf_generate` 函数：对每个 `.proto` 文件生成一条自定义命令，调用 `${PROTOC_PROGRAM} --cpp_out=...` 产出 `.pb.cc/.pb.h`，并把 proto 所在父目录名编进输出路径（`proto` 目录直出、其他子目录带层级）。
- [src/msprof/CMakeLists.txt:L168-L180](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/CMakeLists.txt#L168-L180) —— 记录 `MSPROF_DIR`；定义 INTERFACE 目标 `msprof_headers`，构建期指向本仓 `inc`、`inc/toolchain`、`inc/external`，安装期（`$<INSTALL_INTERFACE:>`）改指 `include/msprof` 等安装路径——同一个目标名在两种场景下解析出不同路径，是 CMake 的惯用法；最后 `add_subdirectory(collector)` 挂载 collector。
- [src/msprof/CMakeLists.txt:L185-L198](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/CMakeLists.txt#L185-L198) —— `profiling` 自定义目标：把整个 msprof 源目录拷贝到安装配置目录下，再进入其 `build/build_hiprof` 执行 `build_for_hiprof.sh`（hiprof 联编场景使用，日常 oam-tools 构建不触发）；随后 `install(DIRECTORY ...)` 把该目录装入 `.run` 包的 `msprof/lib` 下。

再往下钻一级，看命令行可执行文件如何定义：

- [src/msprof/collector/dvvp/msprofbin/CMakeLists.txt:L78-L90](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/CMakeLists.txt#L78-L90) —— `msprofbinCpp` 源清单开头：`src/msprof_bin.cpp`（main 所在，u4-l2 精读）、`input_parser.cpp`（参数解析）、`running_mode.cpp`（运行模式），并大量复用 `../` 兄弟目录（app、common、driver、profimpl、transport、task_handle）的源文件——msprofbin 是"集大成"的可执行文件。
- [src/msprof/collector/dvvp/msprofbin/CMakeLists.txt:L178-L180](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/CMakeLists.txt#L178-L180) —— `add_executable(msprofbin ...)`。
- [src/msprof/collector/dvvp/msprofbin/CMakeLists.txt:L234-L249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/CMakeLists.txt#L234-L249) —— 关键一行：`OUTPUT_NAME msprof`，目标名 `msprofbin` 但产物文件名是 `msprof`——这就是用户在命令行敲的 `msprof` 命令；install 目的地在 oam 包场景为 `tools/profiler/bin`。

#### 4.3.4 代码实践

**实践目标**：搞清「用户敲的 `msprof` 命令」对应哪个 CMake 目标、装到哪里。

**操作步骤**：

1. 阅读 [src/msprof/collector/dvvp/msprofbin/CMakeLists.txt:L234-L249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/CMakeLists.txt#L234-L249)，注意 `OUTPUT_NAME msprof` 与两种 install 分支（`PKG_NAME` 为 `oam` 时装 `tools/profiler/bin`，否则装 `profiler/bin`）。
2. 用 grep 确认二进制名与安装路径：

   ```bash
   grep -n "OUTPUT_NAME\|tools/profiler" src/msprof/collector/dvvp/msprofbin/CMakeLists.txt
   ```

3. 如本地已完成安装（待本地验证），执行 `which msprof` 与 `ls ${ASCEND_INSTALL_PATH}/tools/profiler/`，应看到 `bin/`（内有 `msprof` 二进制）与 `profiler_tool/`（分析脚本）两个并列目录。

**需要观察的现象**：CMake 目标名（msprofbin）≠ 产物文件名（msprof）≠ 安装目录名（tools/profiler/bin）。

**预期结果**：理解 oam-tools 安装后 `tools/profiler/` 下的布局——`bin/msprof` 是 C++ 命令行入口，`profiler_tool/` 是 Python 分析脚本，两者共同构成完整的 msprof 工具。

#### 4.3.5 小练习与答案

**练习 1**：`protobuf_generate` 为什么要把 proto 输出路径编到 `${CMAKE_BINARY_DIR}/proto/<comp>/...` 而不是源码目录？

**答案**：生成文件不属于源码，放进 `CMAKE_BINARY_DIR` 可以保持源码树干净（构建后 `git status` 不出现 `.pb.cc/.pb.h`），同时用 `<comp>`（如 `msprofbin_proto`）隔离子目录，避免多个子项目同名 proto 互相覆盖。`set_source_files_properties(... GENERATED TRUE)` 再把这些文件标记为生成物，让 CMake 正确处理依赖顺序。

**练习 2**：用户敲 `msprof --help` 时，最终执行的是仓库里哪个源文件编出的程序？

**答案**：`src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp`（main 函数所在）。CMake 目标 `msprofbin` 经 `OUTPUT_NAME msprof` 改名、install 到 `tools/profiler/bin`，成为 PATH 中的 `msprof` 命令。

### 4.4 profiler_tool 目录的来源：wheel 的拉取、构建与安装链路

#### 4.4.1 概念说明

安装后 `${ASCEND_INSTALL_PATH}/tools/profiler/profiler_tool/` 下的 Python 分析脚本，**源码不在本仓**，而在 gitcode 的 `Ascend/msprof` 子仓。README 明确说明（L244）：`bash build.sh` 完成后，wheel（`msprof-0.0.1-py3-none-any.whl`）会被拷贝到 `src/msprof/collector/dvvp/msprofbin/` 并打包进 `.run` 安装包；安装时自动解包到 `tools/profiler/profiler_tool/`，无需手动 `pip install`。

分析脚本由 msprof collector 流水线**内部调用**（入口 `profiler_tool/analysis/msprof/msprof.py`），不会在 PATH 中注册独立命令——也就是说，C++ collector 采完数据后自己转身去调 Python 脚本做分析，这是"采集与分析解耦、但入口统一"的设计。

#### 4.4.2 核心流程

wheel 从外部子仓到用户机器的完整生命线（五步）：

```text
① 取源   cmake/build_submodules.cmake
          优先用兄弟目录 ../../mindstudio/msprof（开发联调），
          否则 git clone --depth 1 https://gitcode.com/Ascend/msprof.git → submodule/msprof/
② 构建   python3 build/setup.py bdist_wheel → dist/msprof-0.0.1-py3-none-any.whl
③ 拷贝   file(COPY) 把 whl 放到 src/msprof/collector/dvvp/msprofbin/ 下
④ 打包   msprofbin/CMakeLists.txt：
          a) whl 本体 install(PROGRAMS) → tools/profiler/profiler_tool/
          b) 构建期 pip3 install -t 解包 whl，install(DIRECTORY) 释放解包内容 → 同目录
⑤ 安装   .run 包安装脚本（msprof_install.sh / oam_tools.xml）在用户机器上落位并设权限
```

运行期 C++ 侧怎么找到分析脚本：msprofbin 通过 `Utils::GetSelfPath()` 拿到自身可执行文件路径，向上一级推出 profiler 安装目录，再拼上固定相对路径 `profiler_tool/analysis/msprof/msprof.py`，校验文件存在且可执行。

#### 4.4.3 源码精读

- [cmake/build_submodules.cmake:L81-L88](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake#L81-L88) —— `oam_build_msprof_analysis` 开头：优先探测兄弟目录 `mindstudio/msprof`（内部开发联编场景），否则调用 `oam_populate_submodule` 从 `https://gitcode.com/Ascend/msprof.git` 浅克隆。
- [cmake/build_submodules.cmake:L37-L45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake#L37-L45) —— 取源守卫：目标目录存在且非空才视为就绪；空目录（上一轮取源失败留下的残壳）会重新取源，避免把失败推迟成难定位的构建报错。
- [cmake/build_submodules.cmake:L90-L98](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake#L90-L98) —— 用 `python3 build/setup.py bdist_wheel --python-tag=py3 --py-limited-api=cp37` 构建 wheel，失败即 `FATAL_ERROR`。
- [cmake/build_submodules.cmake:L100-L112](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake#L100-L112) —— 显式校验产物 `dist/msprof-0.0.1-py3-none-any.whl` 存在后，先删后拷到 `src/msprof/collector/dvvp/msprofbin/`。注释解释了两个防御点：`file(COPY)` 对不存在的源会静默跳过（必须显式校验）；旧 whl 可能只读（直接覆盖会失败）。
- [src/msprof/collector/dvvp/msprofbin/CMakeLists.txt:L274-L283](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/CMakeLists.txt#L274-L283) —— 打包第一步：whl 本体以 555 权限 `install(PROGRAMS)` 到 `tools/profiler/profiler_tool`。
- [src/msprof/collector/dvvp/msprofbin/CMakeLists.txt:L286-L318](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/CMakeLists.txt#L286-L318) —— 打包第二步：构建期用 `pip3 install --no-deps -t` 把 whl 解包到构建目录（先删后建，防 pip 因版本恒定判 already satisfied 跳过导致旧代码残留），清掉 `__pycache__`，再 `install(DIRECTORY)` 释放解包内容。`if(EXISTS)` 守卫兼容"单独联编无 whl"的场景。
- [scripts/package/oam_tools/oam_tools.xml:L107-L108](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/oam_tools.xml#L107-L108) —— .run 包清单声明 `tools/profiler/profiler_tool` 与其 `analysis` 子目录，安装权限 750。
- [scripts/package/oam_tools/scripts/msprof_install.sh:L48-L49](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/package/oam_tools/scripts/msprof_install.sh#L48-L49) —— 安装期调用 `install_msprof_whl_package` 处理 `tools/profiler/profiler_tool/msprof-0.0.1-py3-none-any.whl`。
- [src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp:L549-L564](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L549-L564) —— 运行期接缝：`RunningMode::CheckAnalysisEnv()` 由自身路径推出 profiler 安装目录，拼接常量 `profiler_tool/analysis/msprof/msprof.py`（L559），校验脚本存在且具可执行位，否则告警退出——这就是 C++ collector 找到 Python 分析脚本的现场。
- [README.md:L244-L249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L244-L249) —— README 对上述链路的用户视角总结，并给出手动运行分析脚本的方式：`python3 ${ASCEND_INSTALL_PATH}/tools/profiler/profiler_tool/analysis/msprof/msprof.py -h`。

#### 4.4.4 代码实践

**实践目标**：亲手验证「whl 是构建期产物而非仓库静态文件」以及安装后的目录形态。

**操作步骤**：

1. 确认仓库中当前是否有 whl（预期没有，它是 `bash build.sh` 时才生成的）：

   ```bash
   ls src/msprof/collector/dvvp/msprofbin/*.whl 2>/dev/null || echo "whl 尚未构建，符合预期"
   ```

2. 追踪取源链路：`grep -rn "Ascend/msprof.git" cmake/` 应只在 `build_submodules.cmake` 命中一处。
3. （待本地验证，需已安装 oam-tools 的环境）查看安装产物：

   ```bash
   ls ${ASCEND_INSTALL_PATH}/tools/profiler/
   ls ${ASCEND_INSTALL_PATH}/tools/profiler/profiler_tool/ | head
   python3 ${ASCEND_INSTALL_PATH}/tools/profiler/profiler_tool/analysis/msprof/msprof.py -h
   ```

**需要观察的现象**：第 1 步在未构建的仓库中应打印"whl 尚未构建"；第 3 步（若环境可用）应看到 `profiler_tool/` 下既有 `msprof-0.0.1-py3-none-any.whl` 本体，也有构建期解包释放的 `analysis/` 等目录，且 `-h` 能打印分析脚本的帮助。

**预期结果**：理解 profiler_tool 不是某个开发者手工提交的目录，而是「gitcode 子仓 → bdist_wheel → 拷贝 → 解包 → install 规则 → .run 包清单」这条机器流水线的产物；仓库里能看到的只有流水线的"管道"（CMake 与安装脚本），看不到"水"（whl）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 wheel 既要 `install(PROGRAMS)` 装 whl 本体，又要构建期解包后再 `install(DIRECTORY)` 一次？

**答案**：装 whl 本体保留了离线重装/分发能力（安装脚本 `msprof_install.sh` 可以在目标机上用该 whl 处理安装）；构建期解包则让 `.run` 包安装后直接具备可运行的分析脚本目录（`analysis/` 等），不依赖目标机 pip。两条路径并存，兼顾"可再分发"与"开箱即用"。

**练习 2**：`build_submodules.cmake` 中 `oam_populate_submodule` 为什么强调"空目录需重新取源"？

**答案**：`git clone` 半途失败可能留下空目录。若只判目录存在就视为就绪，构建会带着空 submodule 继续走，错误被推迟到 wheel 构建或拷贝阶段才爆发，定位成本高。在取源处显式拦截（`file(GLOB)` 检查目录非空），把失败暴露在最早、信息最全的位置——这与 asys「失败早暴露」的防御式风格一致。

**练习 3**：C++ collector 与 Python 分析脚本之间通过什么机制衔接？

**答案**：文件系统路径约定 + 子进程调用。C++ 侧 `CheckAnalysisEnv()` 用自身可执行文件位置推出安装目录，按固定相对路径 `profiler_tool/analysis/msprof/msprof.py` 找到脚本并校验存在性与可执行位；采集落盘后由 msprof 流水线内部以 python3 调用该脚本分析原始数据。两侧零代码级依赖，只共享目录布局契约。

## 5. 综合实践

**任务：画出 msprof 完整数据流图并标注源码位置（本讲规格指定的代码实践任务）。**

请综合本讲四个模块的内容，画一张从「业务进程」到「分析结果」的端到端数据流图（手绘或 Mermaid 均可），**每个环节标注对应源码目录/文件或外部仓**。参考骨架与标注要点：

```text
[业务进程：训练/推理脚本]
   │ 拉起
   ▼
[msprof 命令行]                     ← ① 本仓 src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp
   │ ProfileParams（多入口单收敛）    ← ② task_handle/ + msprofbin/src/msprof_params_adapter.cpp
   ▼
[Host 侧采集]                       ← ③ dvvp/msprof/engine/、msprof/msproftx/（runtime/GE/HCCL 回调上报，无锁 buffer）
[Device 侧采集]                     ← ⑤ driver/、transport/prof_channel.cpp（prof_drv_start + ChannelPoll 轮询）
[AICPU 采集]                        ← ④ profimpl/aicpu/（经 profiling driver 回 Host）
   │ 芯片差异判断贯穿全程             ← ⑦ common/platform/、profimpl/platform/*_platform.cpp
   ▼
[数据处理：落盘原始性能数据]          ← ⑥ analyze/、transport/uploader*.cpp、msprof/engine/src/uploader_dumper.cpp
   │ 子进程调用 python3
   ▼
[Python 分析脚本]                   ← 外部仓 gitcode.com/Ascend/msprof（构建期 bdist_wheel，
   │                                  装到 tools/profiler/profiler_tool/analysis/msprof/msprof.py，
   │                                  接缝代码：msprofbin/src/running_mode.cpp 的 CheckAnalysisEnv）
   ▼
[分析结果：op summary 等性能表格]
```

操作步骤：

1. 先自己凭记忆画，再对照 `docs/zh/design/profiling/architecture.md` 与 4.2 节的模块映射表逐环节核对。
2. 对图中每个框，用 `ls` / `grep` 确认你标注的源码目录或文件真实存在（例如 `ls src/msprof/collector/dvvp/profimpl/aicpu/`）。
3. 标出图中唯一一个"跨界"环节（C++ → Python），并写下它的两份契约代码：`running_mode.cpp` 的 `ANALYSIS_SCRIPT_PATH` 常量与 `build_submodules.cmake` 的 whl 拷贝目的地。

预期结果：一张可长期使用的"msprof 全景导航图"。以后阅读任一 msprof 源文件时，先在图中定位它属于哪个环节，再判断它的上下游是谁。

## 6. 本讲小结

- msprof 由 **C++ collector**（本仓 `src/msprof/collector`，分 `basic` OS 抽象层与 `dvvp` 主体）和 **Python 分析 wheel**（外部仓 `gitcode.com/Ascend/msprof`，构建期打成 `msprof-0.0.1-py3-none-any.whl`）两部分组成，采集与分析解耦、入口统一。
- 架构文档的七大模块可全部映射到 dvvp 子目录：命令行→`msprofbin`、开关处理→`task_handle`、Host 采集→`msprof/engine`、AICPU 采集→`profimpl/aicpu`、驱动采集→`driver`+`transport`、数据处理→`analyze`+`transport`、Platform→`profimpl/platform`（十余个 `*_platform.cpp` 反射注册）。
- 部署形态是三个 so 的分层：`msprofiler.so`（acl prof 接口）、`profapi.so`（接口层，dlopen 实现层）、`profimpl.so`（实现层，CMake 目标 `profimpl_fwk_share`），生产态可裁剪 profimpl。
- 用户敲的 `msprof` 命令 = CMake 目标 `msprofbin` 经 `OUTPUT_NAME msprof` 改名、安装到 `tools/profiler/bin` 的可执行文件，main 在 `msprof_bin.cpp`。
- `tools/profiler/profiler_tool/` 目录不是仓库静态文件，而是「git clone 子仓 → bdist_wheel → 拷入 msprofbin 目录 → whl 与解包内容双路 install → .run 包清单与安装脚本落位」五步流水线的产物。
- C++ 与 Python 的唯一接缝是路径契约：`running_mode.cpp` 按自身位置拼接固定相对路径 `profiler_tool/analysis/msprof/msprof.py` 并校验存在性。

## 7. 下一步学习建议

本讲只搭了 msprof 的骨架。下一讲 **u4-l2「msprofbin：命令行入口与任务管理」**将进入骨架的第一环，精读 `msprof_bin.cpp` 的 main 初始化顺序、`input_parser.cpp` 的命令行解析与 `msprof_manager.cpp`/`msprof_task.cpp` 的任务调度。建议在进入下一讲前：

1. 通读一遍 [docs/zh/profiling/msprof_cmd/msprof_cmd.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/msprof_cmd.md)，从用户视角认识 `msprof` 命令的常用选项（u4-l5 会系统讲采集方式）。
2. 浏览 `src/msprof/collector/dvvp/msprofbin/src/` 目录（只有 7 个左右源文件），对入口规模建立直觉。
3. 若想先看"数据处理"环节，可跳读 `analyze/inc/analyzer_base.h`，为 u4-l4 做铺垫。
