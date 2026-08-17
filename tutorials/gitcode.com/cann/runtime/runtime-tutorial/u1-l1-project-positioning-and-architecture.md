# 项目定位与总体架构

## 1. 本讲目标

本讲是整本学习手册的第一讲，目标是让从未接触过本仓的读者建立起全局认知。学完本讲，你应该能够：

1. 说出本仓（CANN Runtime）在昇腾软件栈中的定位，以及 Runtime 组件与维测组件（msprof / adump / log）各自的职责。
2. 理解 Host-Device 异构编程模型：主机负责任务编排，设备负责计算，两者通过 Stream 异步协作。
3. 画出 `acl（对外接口层）→ runtime（核心层）→ driver（驱动适配层）→ 内核态驱动` 的分层关系，并且知道每一层对应仓库里的哪个目录。
4. 沿着 `aclrtSetDevice` 这一条真实调用链，在源码中亲眼看到「分层」不是文档口号，而是可逐行验证的事实。

## 2. 前置知识

本讲从零开始，但以下几个基础概念会帮助你更快理解，我们用通俗语言逐一解释：

- **NPU（神经网络处理器）**：专门为 AI 计算设计的加速芯片。华为的昇腾（Ascend）系列 NPU 内部包含 AI Core（矩阵/向量计算单元）、AI CPU 等多种计算单元。
- **异构计算**：程序的一部分跑在 CPU（主机）上，另一部分跑在加速芯片（设备）上，两者分工协作。本仓就是管理「设备侧」的那一层软件。
- **运行时（Runtime）**：介于「应用/框架」和「芯片驱动」之间的系统软件。类比关系：CUDA Runtime 之于 NVIDIA GPU，大致就是 CANN Runtime 之于昇腾 NPU。
- **用户态与内核态**：应用代码和本仓的代码都运行在用户态；真正操作硬件的驱动模块（`ascend_km`）运行在内核态，用户态通过 `/dev/davinci*` 设备文件与之交互。
- **AI 框架与算子**：PyTorch、MindSpore 等框架把神经网络拆成一个个「算子」（Operator，如加法、矩阵乘）下发给 NPU 执行。本仓不实现算子的数学逻辑，而是负责把算子「搬运」到硬件上执行。
- **Dump / Profiling（维测）**：当计算结果不对（精度问题）或跑得慢（性能问题）时，需要把中间数据抓出来看（Dump）、把各阶段耗时采出来看（Profiling）。这类「维护与测试」能力称为维测（DFX）。

不需要任何昇腾开发经验，本讲只要求会读 C/C++ 代码和 Linux 基本命令。

## 3. 本讲源码地图

本讲涉及的关键文件如下（后文所有源码精读都围绕这些文件展开）：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md) | 项目门面：一句话定位、目录结构、环境搭建与编译入口 |
| [docs/zh/quick_start/Runtime_overview.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_overview.md) | 官方入门：以向量加法为例展示 Runtime 完整调用流程与功能模块划分 |
| [docs/zh/quick_start/Runtime_programming_model.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md) | 编程模型：Host/Device 关系、Stream/Task、Context、同步与异步 |
| [docs/zh/design/architecture.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md) | 架构设计：四层逻辑架构、部署视图、设计原则 |
| `src/` 顶层目录 | 所有模块源代码所在地，是本讲「代码地图」的主角 |
| `src/acl/`、`src/runtime/`、`src/dfx/` | 三个最重要的顶层模块：acl 对外接口层、runtime 核心层、dfx 维测组件 |
| `include/external/acl/` | 本仓对外发布的头文件（用户 `#include "acl/acl.h"` 时用到） |
| `example/` | 基于 acl 接口开发的可运行样例，入门首选 `example/0_quickstart/0_hello_cann/` |

## 4. 核心概念与源码讲解

### 4.1 项目定位：Runtime 组件 + 维测组件

#### 4.1.1 概念说明

打开仓库第一件事是回答三个问题：这是什么？给谁用？边界在哪？

- **这是什么**：本仓提供华为昇腾 NPU 的**运行时组件**和**维测功能组件**。运行时组件是「让 AI 计算任务真正跑在芯片上」的核心软件；维测组件是「跑错了、跑慢了帮你查问题」的工具集。
- **给谁用**：上层 AI 框架（PyTorch、MindSpore、TensorFlow）和加速库（算子库、HCCL 通信库、图引擎等）通过本仓的 API 使用硬件。终端用户一般不直接写 Runtime API，但样例和定制开发会直接用到。
- **边界在哪**：本仓不含算子数学实现（那在 CANN ops 算子包里），也不含内核态驱动（那是独立的驱动包）；它位于两者之间。

#### 4.1.2 核心流程

仓库对自身能力的划分可以用下面这张表概括（对应 README「概述」章节）：

```
runtime 仓
├── Runtime 组件（核心）
│   └── 设备管理、流管理、Event 管理、内存管理、任务调度
└── 维测功能组件（DFX）
    ├── msprof：性能调优，采集各运行阶段性能指标，定位性能瓶颈
    ├── adump：精度调试，Dump 算子/模型的输入输出数据、异常数据
    └── log：日志，进程打印与落盘，msnpureport 命令行工具
```

#### 4.1.3 源码精读

**① 项目的自我定位**，见 README 概述部分。这段话是全仓最权威的一句话定位：

- [README.md:L8-L16](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L8-L16)：说明本仓 = Runtime 组件（设备/流/Event/内存/任务调度）+ 维测组件（msprof 性能调优、adump 精度调试、log 日志）。

**② 官方目录结构**，README 用一棵树画出了关键目录：

- [README.md:L22-L53](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L22-L53)：定义了 `include`（对外头文件）、`src`（各模块源代码，含 `acl`/`dfx`/`runtime`/`mmpa`）、`example`（样例）、`tests`（UT 用例）、`build.sh`（编译脚本）等关键目录的职责。

**③ 维测组件在源码中的落点**，README 中提到的 msprof/adump/log 对应 `src/dfx/` 下的真实目录：

```
src/dfx/
├── adump          # 精度调试（Dump 输入输出数据）
├── error_manager  # 错误信息管理
├── log            # 日志
├── msprof         # 性能数据采集
└── trace          # trace 追踪
```

#### 4.1.4 代码实践

**实践：用 git 命令亲手验证目录地图**

1. 实践目标：不依赖记忆，用只读命令确认 README 里描述的目录真实存在，并统计各顶层模块的规模，建立「哪里是什么」的肌肉记忆。
2. 操作步骤（在仓库根目录执行）：

   ```bash
   # 1. 列出 src 下的全部顶层模块
   ls src/
   # 2. 查看 README 中目录树提到的三个关键目录
   ls src/acl/ src/runtime/ src/dfx/
   # 3. 粗略感受各模块代码量（文件数）
   git ls-files src/acl | wc -l
   git ls-files src/runtime | wc -l
   git ls-files src/dfx | wc -l
   ```

3. 需要观察的现象：`src/` 下除了 `acl`、`runtime`、`dfx` 外，还有 `mmpa`（跨平台进程/线程抽象）、`platform`、`queue_schedule`、`tsd` 等目录；`src/runtime` 与 `src/dfx` 的文件数量都明显多于 `src/acl`。
4. 预期结果：你会得到与 4.1.3 中目录树一致的输出。若某目录不存在，说明你查看的分支/标签与本讲 HEAD（`44a689408`）不一致。
5. 该实践为纯只读命令，无环境依赖，可直接执行。

#### 4.1.5 小练习与答案

**练习 1**：本仓「Runtime 组件」包含哪五类核心功能？
**答案**：设备管理、流管理、Event 管理、内存管理、任务调度（见 [README.md:L11](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L11)）。

**练习 2**：msprof、adump、log 三个维测组件分别解决什么问题？对应 `src/` 下哪个目录？
**答案**：msprof 采集性能指标定位性能瓶颈，adump 抓取算子/模型输入输出数据定位精度问题与 AI Core Error，log 提供进程打印与落盘日志（含 msnpureport 工具）；三者都在 `src/dfx/` 目录下。

**练习 3**：判断对错：「本仓包含昇腾算子的数学实现。」
**答案**：错。算子实现在 CANN ops 算子包中，本仓负责把算子任务加载、调度并下发到硬件执行（README 也指出运行样例时须另行安装 ops 算子包）。

### 4.2 Host-Device 异构编程模型

#### 4.2.1 概念说明

要理解 Runtime 的接口为什么长那样（为什么先 SetDevice、为什么有 Stream、为什么要显式 Memcpy），必须先理解它的编程模型——**主机-设备异步并行**：

- **主机（Host）**：X86/ARM 服务器 CPU，负责任务编排与下发。
- **设备（Device/NPU）**：通过 PCIe、HCCS 等总线与主机相连的 AI 处理器，负责真正的计算。
- 两者**各自拥有独立内存空间**，数据必须显式拷贝（Host→Device→计算→Device→Host）。
- 任务**异步下发**：主机调用算子接口后立即返回，任务进入 Stream 队列，设备异步执行；主机需要结果时再**显式同步**。

此外还有三个贯穿全仓的核心对象，先把关系记住（N:1 表示多对一）：

| 概念 | 一句话解释 | 关系 |
| --- | --- | --- |
| Device | 对一块 AI 处理器设备的抽象 | Host : Device = 1 : N |
| Context | Device 上的逻辑运行环境，管理 Stream/Event 等资源生命周期，互相隔离 | Context : Device = N : 1 |
| Stream | 逻辑任务执行队列，同 Stream 内任务严格 FIFO，跨 Stream 并行 | Stream : Context = N : 1 |
| Task | 被加入 Stream 的执行任务（计算/拷贝/事件同步） | Task : Stream = N : 1 |

#### 4.2.2 核心流程

官方文档给出的典型执行流程（共 10 步）可以浓缩为四段：

```
① 建环境：aclInit → aclrtSetDevice(0)
   （SetDevice 内部：创建 Device 对象 → 创建默认 Context → 创建默认 Stream → 启动 Device 侧 CPU 执行器）
② 备数据：aclrtMallocHost / aclrtMalloc 申请两侧内存
          → aclrtMemcpy(HOST_TO_DEVICE) 把数据搬上设备
③ 下任务：myKernel<<<numBlocks, nullptr, stream1>>>(devPtr)  // 异步，立即返回
          设备侧调度器按绝对优先级从任务队列取任务，分发到 AI Core / AI CPU 等加速单元
④ 收结果：aclrtSynchronizeStream(stream1) 阻塞等待
          → aclrtMemcpy(DEVICE_TO_HOST) 取回结果 → 释放资源
```

同步/异步的判断口诀：**带 Stream 参数的 API 通常异步（如 `aclrtMemcpyAsync`、`aclrtLaunchKernel`），不带 Stream 的通常同步（如 `aclrtMemcpy`）**。

#### 4.2.3 源码精读

**① 官方入门样例流程**（向量加法 `result = x + alpha * y` 的完整 9 步伪码）：

- [Runtime_overview.md:L12-L61](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_overview.md#L12-L61)：从 `aclInit` 到 `aclFinalize` 的完整调用序列，并注明真实可运行代码在 `example/0_quickstart/0_hello_cann/main.cpp`。
- [Runtime_overview.md:L68-L78](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_overview.md#L68-L78)：把 9 个步骤映射到 Runtime 的能力模块（全局管理 / Device / Stream / Memory / Kernel）。
- [Runtime_overview.md:L84-L90](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_overview.md#L84-L90)：核心编程模式三要素——主机设备分离、异步任务下发、显式同步。

**② Host/Device 与异步执行的定义**：

- [Runtime_programming_model.md:L3-L7](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md#L3-L7)：Host 与 Device 的正式定义，以及 HCCS（华为缓存一致性系统）的含义。
- [Runtime_programming_model.md:L15-L25](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md#L15-L25)：三条总结——独立内存空间、异步并行执行、Stream 内保序/Stream 间并行。

**③ 核心对象与依赖关系**：

- [Runtime_programming_model.md:L200-L210](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md#L200-L210)：Host/Device/Context/Stream/Task 五个概念的权威定义与 N:1 关系。
- [Runtime_programming_model.md:L51-L59](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md#L51-L59)：同步/异步 API 的区分规则与典型示例（`aclrtMemcpy` vs `aclrtMemcpyAsync`）。

#### 4.2.4 代码实践

**实践：阅读 hello_cann 样例，把 9 步流程「对号入座」**

1. 实践目标：把 4.2.2 中的抽象流程落到一份真实可编译的代码上。
2. 操作步骤：
   1. 打开 [example/0_quickstart/0_hello_cann/main.cpp](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/example/0_quickstart/0_hello_cann/main.cpp)，通读一遍。
   2. 对照 [Runtime_overview.md:L68-L78](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_overview.md#L68-L78) 的表格，在代码旁标注每一步属于哪个能力模块（全局管理/Device/Stream/Memory/Kernel）。
   3. 找出代码中所有「带 Stream 参数」的调用，判断哪些是异步的。
3. 需要观察的现象：代码结构应与文档 9 步一一对应；`aclrtSynchronizeStream` 之前的调用都不会阻塞等待计算完成。
4. 预期结果：你能画出一张「代码行号 → 能力模块」的对照清单。（如需实际运行该样例，须先安装驱动/固件与 CANN 包，运行步骤见样例目录下 `run.sh` 与 README；本讲不依赖运行，纯阅读即可完成。）

#### 4.2.5 小练习与答案

**练习 1**：Kernel1、Kernel3 在 Stream A，Kernel2 在 Stream B，主机按 1→2→3 顺序下发，执行关系是什么？
**答案**：Kernel3 等待 Kernel1 完成（同 Stream 严格 FIFO）；Kernel2 与 Kernel1/Kernel3 可并行（不同 Stream 之间并行）。（见 [Runtime_programming_model.md:L29-L34](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md#L29-L34)）

**练习 2**：为什么大多数 Runtime API 不带 deviceId 参数？
**答案**：API 作用的 Device 从「调用线程关联的 Context」中获取；线程必须先关联 Context（如 `aclrtSetDevice` 会创建默认 Context 并关联线程），同一时刻只能关联一个。（见 [Runtime_programming_model.md:L212-L218](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md#L212-L218)）

**练习 3**：`aclrtMemcpy` 与 `aclrtMemcpyAsync` 的本质区别是什么？
**答案**：前者不带 Stream，同步执行，阻塞主机直到拷贝完成；后者带 Stream，异步执行，立即返回，拷贝在指定 Stream 中执行，需要后续同步才能保证数据就绪。

### 4.3 四层分层架构与 src 顶层目录

#### 4.3.1 概念说明

架构文档把 Runtime 划分为四层，这是理解全仓代码组织的钥匙：

| 层 | 职责 | 典型接口/目录 |
| --- | --- | --- |
| Runtime 接口层 | 对外 C/C++ API，统一参数校验与错误码转换 | `rt` 前缀底层接口（`rtSetDevice`）、`aclrt` 前缀功能接口（`aclrtLaunchKernel`）、`aclmdlRI` 前缀 Graph/Model 接口 |
| Runtime 特性层 | 基于核心层的可插拔特性 | ACL Graph（流捕获）、Model、Fusion（算子融合）、Snapshot（进程快照） |
| Runtime 核心层 | 核心实现主体 | Device/Context/Stream（SQ-CQ 异步机制）/Event/Notify、任务调度（Engine/TaskFactory）、内存（MemoryPool）、内核执行（Kernel/Program） |
| 驱动适配层 | 屏蔽不同代际芯片与驱动的差异 | `driver/v100`、`driver/v200` 等版本目录 + `config/`（910、310P、950 等芯片配置）+ HAL 驱动接口 |

从部署视角看（这是分层的另一半真相）：Runtime 以**用户态动态库**形式部署在应用进程内（`libacl_rt.so`、`libruntime.so`、`libruntime_v100/v200.so` 等），向下通过运行在内核态的驱动模块 `ascend_km` 暴露的 `/dev/davinci*` 设备文件操作硬件。

#### 4.3.2 核心流程

分层调用关系与源码目录的对应：

```
应用 / AI 框架（PyTorch、MindSpore...）
        │  #include "acl/acl.h"（头文件在 include/external/acl/）
        ▼
┌─ Runtime 接口层 ───────── src/acl/ ────────────  aclrtXxx → rtXxx 转换、参数校验、错误码转换
│               ┌─ src/runtime/api/ ──  rtXxx C 接口入口
├─ 特性层 ────── │  src/runtime/feature/（aclgraph/fusion/snapshot/model...）
├─ 核心层 ────── │  src/runtime/core/src/（device/context/stream/task/memory/kernel...）
├─ 驱动适配层 ── │  src/runtime/driver/（v100/、v200/...）+ src/runtime/config/（各芯片配置）
│               └─ src/platform/、src/mmpa/ 等公共支撑
        ▼
内核态驱动 ascend_km（/dev/davinci*，不在本仓）
        ▼
昇腾 NPU 硬件（910、310P、950 等）
```

`src/` 顶层其余目录的角色：`dfx/` 是维测组件（见 4.1）；`mmpa/` 提供跨平台的进程/线程/文件操作抽象，让同一份代码能在 Linux/Windows 等平台编译；`queue_schedule/`、`tsd`、`aicpu_sched/` 与任务调度/设备侧服务相关；`cmodel_driver/`、`runtime_compact/`、`tprt/` 是面向特定形态的精简运行时与仿真支撑（初学阶段只需知道名字，后续单元再深入）。

#### 4.3.3 源码精读

**① 四层逻辑架构的权威定义**：

- [architecture.md:L11-L14](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L11-L14)：依次定义接口层（rt/aclrt/aclmdlRI 三类前缀）、特性层（ACL Graph/Model/Fusion/Snapshot）、核心层（Device/Context/Stream/任务调度/内存/内核执行）、驱动适配层（driver/v100、v200 多代际 + config/ 芯片配置 + HAL 统一入口）。

**② 部署视图：每层跑在哪里**：

- [architecture.md:L23-L31](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L23-L31)：部署层级表——应用层（用户态进程）、Runtime 层（用户态动态库，列出 `libacl_rt.so`/`libruntime.so`/`libruntime_v100/v200.so` 等）、驱动层（内核态 `ascend_km`，`/dev/davinci*`）、硬件层（910/310P/950 等芯片）。

**③ 进程模型与设计原则**：

- [architecture.md:L32-L39](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L32-L39)：单进程多设备/多上下文/多 Stream、多进程隔离。
- [architecture.md:L375-L382](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L375-L382)：六条架构设计原则，其中「分层设计」一行明确指出目录结构按层级划分（api/、feature/、core/src/、driver/），这正是 4.3.2 那张对应图的出处。

**④ 分层在源码目录中的真实落点**（用只读命令即可验证）：

```text
src/runtime/
├── api/        # 接口层 rtXxx 入口：api_c_device.cc、api_c_stream.cc、api_c_memory.cc...
├── feature/    # 特性层：aclgraph、fusion、snapshot、model、ccu...
├── core/       # 核心层：core/src 下有 device/ context/ stream/ task/ memory/ kernel/...
├── driver/     # 驱动适配层：npu_driver.cc + v100/ v200/ v201/ 版本目录
├── config/     # 芯片特性配置：910_B_93、bs9sx1a、950、as31xm1...
└── inc/        # 内部公共头文件（common/device/sqe）
```

#### 4.3.4 代码实践

**实践：把「设计原则」与「目录现实」逐条对照**

1. 实践目标：验证架构文档不是纸上谈兵——文档说的每个分层目录都真实存在。
2. 操作步骤：

   ```bash
   # 1. 验证接口层/特性层/核心层/驱动适配层目录
   ls src/runtime/api src/runtime/feature src/runtime/core src/runtime/driver
   # 2. 验证核心层内部模块与架构文档 1.3 节的对应关系
   ls src/runtime/core/src
   # 3. 感受「多代际芯片适配」：看看 config 下有多少种芯片
   ls src/runtime/config | head -20
   ```

3. 需要观察的现象：`core/src` 下应出现 `device`、`context`、`stream`、`task`、`memory`、`kernel`、`event`、`engine` 等目录，与架构文档 1.3 节「核心模块介绍」一一对应；`config` 下是大量以芯片型号命名的目录。
4. 预期结果：四层目录全部存在，说明「分层设计」原则（[architecture.md:L377](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L377)）直接体现在目录结构上。纯只读命令，可直接执行。

#### 4.3.5 小练习与答案

**练习 1**：`aclrtLaunchKernel`、`rtSetDevice`、`aclmdlRICaptureBegin` 三个接口名分别属于接口层的哪一类？
**答案**：`aclrt` 前缀的 ACL 运行时功能接口；`rt` 前缀的底层运行时接口；`aclmdlRI` 前缀的 ACL Graph/Model 特性接口。（见 [architecture.md:L11](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L11)）

**练习 2**：Runtime 通过什么方式与内核态驱动交互？相关设备文件是什么？
**答案**：Runtime 是用户态动态库，通过内核态驱动模块 `ascend_km` 暴露的 `/dev/davinci*` 设备文件交互。（见 [architecture.md:L29](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L29)）

**练习 3**：为什么需要 `src/runtime/driver/v100`、`v200` 这样的版本目录和 `config/` 目录？
**答案**：不同代际芯片的硬件与驱动接口存在差异，驱动适配层用版本目录适配不同代际、用 config/ 目录（按芯片型号组织）管理芯片特性配置，从而对核心层屏蔽差异。（见 [architecture.md:L14](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md#L14)）

### 4.4 一条真实调用链：aclrtSetDevice 的分层穿越

#### 4.4.1 概念说明

前三节都在「看地图」，这一节「上路」：以最常用的 `aclrtSetDevice` 为例，看一次设备设置如何从应用代码出发，穿过接口层、核心层，走向驱动。读完本节你就掌握了本仓最重要的源码阅读方法论——**顺着接口名找实现，顺着实现找下一层**。

`aclrtSetDevice` 内部会：创建并初始化 Device 对象 → 创建默认 Context → 创建默认 Stream → 启动 Device 侧 CPU 执行器进程（见 [Runtime_programming_model.md:L86-L90](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/quick_start/Runtime_programming_model.md#L86-L90)）。本节聚焦其中经手的源码文件。

#### 4.4.2 核心流程

调用链全景（自上而下四跳）：

```
应用代码
  │ aclrtSetDevice(0)
  ▼
① include/external/acl/acl_rt.h          —— 对外声明（用户可见的 API 契约）
  ▼
② src/acl/aclrt_c/runtime/device.c       —— 接口层实现：调 rtSetDevice，做日志与错误转换
  ▼
③ src/runtime/api/api_c_device.cc        —— runtime C 接口入口：rtSetDevice → Api 单例
  ▼
④ src/runtime/core/src/api_impl/api_impl.cc —— 核心层实现：ApiImpl::SetDevice
       │ Runtime::Instance()->PrimaryContextRetain(devId)   —— 取/建设备主上下文
       │ InnerThreadLocalContainer::SetCurRef(context)      —— 线程绑定 Context
       ▼
   再往下：Device/Context/Stream 对象创建 → 驱动适配层(src/runtime/driver) → 内核态驱动
```

#### 4.4.3 源码精读

**① 对外契约**——用户侧看到的只是头文件里的一行声明：

- [include/external/acl/acl_rt.h:L1538-L1546](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/include/external/acl/acl_rt.h#L1538-L1546)：`aclrtSetDevice(int32_t deviceId)` 的对外声明与注释（成功返回 `ACL_SUCCESS`），这就是 `#include "acl/acl.h"` 后用户能看到的全部。

**② 接口层（acl 层）**——典型的一层「薄封装」：

- [src/acl/aclrt_c/runtime/device.c:L39-L53](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt_c/runtime/device.c#L39-L53)：`aclrtSetDevice` 的实现只有两个动作——调用底层 `rtSetDevice(deviceId)` 并在失败时记录日志返回；成功后再通知 profiling 组件（`GeNofifySetDevice`）。这一层不做真正的设备操作，只负责转发与错误码/日志处理。

```c
// 示例代码（摘自上述链接，有删节）
aclError aclrtSetDevice(int32_t deviceId)
{
    const rtError_t rtErr = rtSetDevice(deviceId);   // 转发给 runtime 核心层
    if (rtErr != RT_ERROR_NONE) {
        ACL_LOG_CALL_ERROR("open device %d failed, rt ret = %d.", deviceId, (int32_t)(rtErr));
        return rtErr;
    }
    ...
    return ACL_SUCCESS;
}
```

**③ runtime C 接口入口**——`rt` 前缀接口统一走 `Api` 单例：

- [src/runtime/api/api_c_device.cc:L76-L85](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/api/api_c_device.cc#L76-L85)：`rtSetDevice` 拿到 `Api::Instance()` 单例，把工作交给 `apiInstance->SetDevice(devId)`。注意同文件 L73 还有一个 `rtsSetDevice` 直接转发到 `rtSetDevice`——`rts` 前缀是兼容性别名，这是全仓常见的命名规律。

```cpp
// 示例代码（摘自上述链接，有删节）
rtError_t rtSetDevice(int32_t devId)
{
    GLOBAL_STATE_WAIT_IF_LOCKED();
    Api* const apiInstance = Api::Instance();        // 核心层单例入口
    NULL_RETURN_ERROR_WITH_EXT_ERRCODE(apiInstance);
    const rtError_t error = apiInstance->SetDevice(devId);
    ERROR_RETURN_WITH_EXT_ERRCODE(error);
    return ACL_RT_SUCCESS;
}
```

**④ 核心层实现**——真正创建/绑定资源的地方：

- [src/runtime/core/src/api_impl/api_impl.cc:L3440-L3466](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/core/src/api_impl/api_impl.cc#L3440-L3466)：`ApiImpl::SetDevice` 的关键三步——`Runtime::Instance()->PrimaryContextRetain(devId)` 获取（或首次创建）该设备的主上下文并增加引用计数；`InnerThreadLocalContainer::SetCurRef(context)` 把当前线程关联到该 Context（呼应 4.2 练习 2「线程关联 Context」）；最后校验默认 Stream 存在并设置饱和模式。`PrimaryContextRetain` 内部继续向 Device/驱动层走，即 4.3 图中的驱动适配层。

> 小结方法论：`aclrtXxx`（src/acl）→ `rtXxx`（src/runtime/api）→ `Api::Xxx`（src/runtime/core/src/api_impl）→ 具体对象（Device/Context/Stream...）。这三跳模式适用于仓里绝大多数接口，后续单元会反复用到。

#### 4.4.4 代码实践

**实践：用 grep 亲自动手走一遍 aclrtSetDevice 调用链**

1. 实践目标：不借助本讲文字，仅用搜索工具独立复现 4.4.2 的调用链，掌握「顺接口找实现」的技能。
2. 操作步骤：

   ```bash
   # 1. 找到 aclrtSetDevice 的声明（对外头文件）
   grep -n "aclrtSetDevice" include/external/acl/acl_rt.h
   # 2. 找到它的实现（acl 接口层）
   grep -rn "^aclError aclrtSetDevice" src/acl/
   # 3. 实现里调用了 rtSetDevice，再找 runtime 层入口
   grep -n "^rtError_t rtSetDevice" src/runtime/api/api_c_device.cc
   # 4. 入口调用了 Api::SetDevice，再找核心层实现
   grep -n "ApiImpl::SetDevice" src/runtime/core/src/api_impl/api_impl.cc
   ```

3. 需要观察的现象：每一步 grep 的命中行号应与本讲 4.4.3 给出的行号一致（L1546 / L39 / L76 / L3440）；打开每处代码，确认「上一层调用下一层」的函数名对应关系。
4. 预期结果：你将得到一条完整的四跳链路。以后遇到任何 `aclrtXxx` 接口，都可以用同样的四步定位到核心实现。纯只读操作，可直接执行。

#### 4.4.5 小练习与答案

**练习 1**：`aclrtSetDevice` 与 `rtSetDevice` 是什么关系？
**答案**：前者是 acl 接口层对用户的 API，实现在 `src/acl/aclrt_c/runtime/device.c`，内部直接调用 runtime 核心层的 `rtSetDevice`（`src/runtime/api/api_c_device.cc`），并附加 profiling 通知与错误日志。

**练习 2**：`ApiImpl::SetDevice` 中 `InnerThreadLocalContainer::SetCurRef(context)` 这一步为什么必不可少？
**答案**：Runtime 大多数 API 不带 deviceId，设备信息来自「调用线程关联的 Context」；这一步把当前线程绑定到刚获取的主 Context，之后的 API 调用才能知道作用在哪个设备上。

**练习 3**：如果想知道 `aclrtCreateStream` 的完整实现路径，你会怎么做？
**答案**：套用同款四步法：先在 `include/external/acl/acl_rt.h` 找声明，再在 `src/acl/` 下搜实现，顺着其中的 `rtCreateStream` 到 `src/runtime/api/api_c_stream.cc`，最后在 `src/runtime/core/src/api_impl/` 里搜 `ApiImpl::CreateStream`。

## 5. 综合实践

**综合实践：自绘「aclrtSetDevice 分层穿越框图」**（本讲的总实践任务）

1. 实践目标：把本讲三个知识模块（项目定位、分层架构、真实调用链）融合成一张你自己的分层框图，作为后续所有单元的「导航图」。
2. 操作步骤：
   1. 准备一张白纸或任意画图工具，画出 5 个水平层，从上到下依次是：应用层、acl 接口层（`src/acl/`）、runtime 核心层（`src/runtime/api/` + `src/runtime/core/`）、驱动适配层（`src/runtime/driver/` + `src/runtime/config/`）、内核态驱动与硬件（不在本仓：`ascend_km`、NPU）。
   2. 在框图上为 `aclrtSetDevice` 画一条自上而下的箭头，在每个被穿越的层上标注：函数名 + 源码文件路径 + 行号（即 4.4.3 的四个代码点：`acl_rt.h:L1546` → `device.c:L39` → `api_c_device.cc:L76` → `api_impl.cc:L3440`）。
   3. 在框图右侧补两个旁注：维测组件 `src/dfx/`（msprof/adump/log）贯穿记录各层；对外头文件统一放在 `include/external/acl/`。
   4. 用一句话在框图底部写清每层的「一句话职责」。
3. 需要观察的现象：画完后自检——是否能不看讲义说出每一层的目录、职责，以及调用链上每一跳的函数名？
4. 预期结果：得到一张类似下图的分层框图（ASCII 示意，供核对）：

```
┌──────────────────────────────────────────────────────────────────┐
│ 应用 / AI 框架（PyTorch、MindSpore...）                            │
└───────────────┬──────────────────────────────────────────────────┘
                │ aclrtSetDevice(0)          声明: include/external/acl/acl_rt.h:1546
┌───────────────▼──────────────────────────────────────────────────┐
│ acl 接口层  src/acl/aclrt_c/runtime/device.c:39  → 调 rtSetDevice │
├──────────────────────────────────────────────────────────────────┤
│ runtime 接口入口  src/runtime/api/api_c_device.cc:76              │
│ runtime 核心层    src/runtime/core/src/api_impl/api_impl.cc:3440  │
│   └ PrimaryContextRetain → 默认 Context/Stream → 线程绑定         │
├──────────────────────────────────────────────────────────────────┤
│ 驱动适配层  src/runtime/driver/（v100/v200）+ src/runtime/config/  │
├──────────────────────────────────────────────────────────────────┤
│ 内核态驱动 ascend_km（/dev/davinci*）→ 昇腾 NPU 硬件（不在本仓）   │
└──────────────────────────────────────────────────────────────────┘
        旁注：维测组件 src/dfx/（msprof / adump / log）贯穿各层
```

## 6. 本讲小结

- 本仓 = **Runtime 组件**（设备/流/Event/内存/任务调度）+ **维测组件**（msprof 性能、adump 精度、log 日志，位于 `src/dfx/`）。
- Runtime 采用**主机-设备异步并行**编程模型：独立内存空间、异步任务下发到 Stream、显式同步取结果；Device:Context:Stream:Task 逐层 N:1。
- 架构分四层：**接口层（src/acl + src/runtime/api）→ 特性层（src/runtime/feature）→ 核心层（src/runtime/core）→ 驱动适配层（src/runtime/driver + config）**，以用户态动态库形式部署在应用进程内，经 `/dev/davinci*` 进入内核态驱动。
- `aclrtSetDevice` 的四跳调用链（`acl_rt.h:1546` → `device.c:39` → `api_c_device.cc:76` → `api_impl.cc:3440`）是全仓通用的阅读范式：**aclrtXxx → rtXxx → Api::Xxx → 具体对象**。
- 头文件契约在 `include/external/acl/`，可运行入门样例在 `example/0_quickstart/0_hello_cann/`。

## 7. 下一步学习建议

- 下一讲（u1-l2「环境搭建与源码编译」）将动手完成依赖安装与 `bash build.sh` 编译，产出可安装的 run 包——建议先把本讲的目录地图放在手边，编译时对照观察每个源码目录参与构建的方式。
- 若想立刻看到 Runtime「动起来」，可提前浏览 [example/0_quickstart/0_hello_cann/main.cpp](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/example/0_quickstart/0_hello_cann/main.cpp) 与 [example/README.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/example/README.md)。
- 延伸阅读（本讲已引用，值得完整通读）：[docs/zh/design/architecture.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/design/architecture.md) 的「1.3 核心模块介绍」与「2 特性功能介绍」，它们是第二、三单元各讲义的预告片。
- 带着问题进入下一单元：`ApiImpl::SetDevice` 里那句 `PrimaryContextRetain` 最终如何创建出 Device 和 Context？这条线将在 u2（acl 层初始化与分层调用）和 u3（Device/Context 管理）中展开。
