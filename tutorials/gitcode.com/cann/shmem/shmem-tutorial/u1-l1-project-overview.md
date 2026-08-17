# 项目总览：什么是对称内存通信库

## 1. 本讲目标

本讲是整套 SHMEM 学习手册的第一讲，读完本讲你应该能够：

1. 用一句话说清 SHMEM 是什么：一个面向昇腾（Ascend）NPU 平台、基于对称内存的多机多卡分布式通信加速库。
2. 理解「Host 侧接口」与「Device 侧接口」的分工：CPU 负责初始化与内存管理，AICore kernel 负责真正的远程数据搬运。
3. 掌握最核心的一批术语：PE、对称内存（Symmetric Memory）、对称堆（Symmetric Heap）、Team、RMA、AMO、信号（SO）、MTE/xDMA 通信引擎。
4. 会看 `include/shmem.h` 这个 API 总入口，能根据函数名前缀（`aclshmem_` / `aclshmemx_`）和所在头文件目录判断一个 API 属于哪一层。
5. 完成本讲的代码实践：手工绘制一张 Host/Device 接口分层图，并标注 5 个自己最感兴趣的 API。

本讲不要求你写代码，重点是把「地图」画在脑子里，后续每一讲都会在这张地图上深入一个局部。

## 2. 前置知识

本讲假设读者具备基本的 C/C++ 阅读能力，并了解以下概念（不了解也不影响，我们用通俗语言解释）：

- **Host 与 Device**：在昇腾异构计算里，Host 指 CPU 侧（控制面），Device 指 NPU/AICore 侧（数据面）。程序通常在 Host 上启动，把计算密集的工作下发给 Device 上的 kernel 执行。
- **分布式训练中的「卡间通信」**：多张 NPU 协同训练一个模型时，卡与卡之间要频繁交换梯度、参数等数据。传统做法是 MPI 两边都参与（send/recv 成对出现），而 SHMEM 提供的是「单边」方式：一方直接读写另一方的内存，对方可以完全不参与。
- **对称内存（Symmetric Memory）的直觉**：想象 8 个进程（每个管一张卡），每个进程都在自己卡上开辟一块大小相同、布局相同的内存区域。这样「我这块内存的第 1000 字节」和「你那块内存的第 1000 字节」天然对应，我把数据写到你的第 1000 字节，就像写自己的第 1000 字节一样简单。这块「大家按同样规则开辟的内存」就是对称内存。
- **OpenSHMEM**：一个标准化的 SHMEM 编程模型规范，定义了 PE、对称对象、RMA、AMO、信号、集合通信等概念。本项目的 API 命名与之一脉相承（同时提供 `shmem_xxx` 的别名宏），学习时可以互相印证。
- **CANN**：昇腾异构计算架构软件栈（Compute Architecture for Neural Networks），SHMEM 的编译、运行、Device kernel 都依赖它。

## 3. 本讲源码地图

本讲涉及的关键文件如下，请先在仓库里找到它们的位置：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md) | 项目门面：定位、核心功能、代码结构、典型场景、FAQ |
| [docs/quickstart.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md) | 快速开始：软件架构、环境要求、安装方式、样例执行 |
| [docs/glossary.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md) | 术语表：所有缩写与名词的权威中文解释 |
| [include/shmem.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h) | 全部对外 API 的汇总入口头文件（只有 include，没有实现） |
| [include/host/shmem_host_def.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h) | Host 侧公共定义：错误码、初始化模式、传输类型枚举、初始化属性结构体 |
| [include/host_device/shmem_common_types.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h) | Host/Device 共享的类型：引擎枚举、Team 结构、全局状态结构 `aclshmem_device_host_state_t` |

阅读建议：README 和 quickstart 通读一遍即可，glossary 建议打印或常开在浏览器侧栏——它是你读后续所有讲义的「字典」。`include/shmem.h` 只有 53 行，但它是整张 API 地图的索引。

## 4. 核心概念与源码讲解

### 4.1 模块一：项目定位与核心价值

#### 4.1.1 概念说明

SHMEM 的全称定位写在 README 标题里：**基于对称内存的昇腾分布式内存通信加速库**。拆开理解：

- **面向昇腾平台**：只适配昇腾 NPU（Atlas 800I A2/A3、800T A2/A3、Ascend 950 系列），不支持 NVIDIA GPU 等其他硬件。
- **多机多卡**：既覆盖单机内多卡（卡间通过 HCCS/PCIe 等互联），也覆盖多机之间（通过 RoCE 网络远程访问）。
- **对称内存**：通信建立在「各参与方按相同规则分配的内存」之上，见第 2 节的直觉解释。
- **通信加速库**：它是一个库（libshmem.so + Device 侧头文件），不是框架。你可以在自己的算子、训练脚本里直接调用它的 API。

它的核心价值（README 总结了四条）：支持 AICore 直驱 MTE、xDMA 引擎完成 D2D/D2H/H2D/D2rH/rH2D 通信；简化分布式卡间通信逻辑；与 CANN 生态深度适配、支持通算融合算子快速部署。

#### 4.1.2 核心流程

一个典型 SHMEM 程序的生命周期如下（本讲只需建立整体印象，细节在后续单元展开）：

```text
① 准备     安装 SHMEM、配置 CANN 环境、拉起 N 个进程（每个进程是一个 PE）
② 初始化   每个 PE 调用 aclshmemx_init_attr(...) → 建立 PE 间连接、创建对称堆
③ 分配     各 PE 以「相同顺序、相同大小」调用 aclshmem_malloc 分配对称内存
④ 通信     Host 侧或 Device kernel 中调用 put/get/AMO/信号 等接口单边访问远端
⑤ 同步     barrier / wait_until / quiet 等保证数据可见与操作完成
⑥ 收尾     aclshmem_free 释放对称内存 → aclshmem_finalize 退出
```

其中 ④ 是 SHMEM 与 MPI 最大的差别：put/get 是**单边操作**，发起方直接读写对端对称内存，对端无需配对调用 receive。

#### 4.1.3 源码精读

先看 README 对项目的「一句话定位」与核心价值：

- [README.md:L64-L71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L64-L71)：项目简介段落，明确「SHMEM 是面向昇腾平台的多机多卡内存通信库，通过封装 Host 侧与 Device 侧接口，实现跨设备的高效内存访问与数据同步」，并列出四条核心价值。

- [README.md:L77-L82](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L77-L82)：核心功能第 1 条「双侧接口体系」——Host 侧负责初始化、内存堆管理、Team 创建及全局同步；Device 侧提供远程内存访问（RMA）、设备级同步及通信域操作。这两句话就是本讲 4.3 节的提纲。

- [README.md:L166-L174](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L166-L174)：典型使用场景，共四类：通算融合类算子开发（如 matmul+allreduce）、多机多卡数据同步、低延迟卡间通信（AI 推理）、Python 分布式训练适配（替代 MPI）。

再看 quickstart 里的软件架构描述，它是官方对「双侧接口」最简洁的划分：

- [docs/quickstart.md:L7-L12](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L7-L12)：接口分为 host 与 device 两部分——host 侧提供初始化、内存管理、通信域管理以及同步功能；device 侧提供内存访问、同步以及通信域管理功能。

硬件与工具链要求（决定你能不能跑起来）：

- [docs/quickstart.md:L30-L34](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L30-L34)：支持 Atlas 800I/800T A2/A3 与 Ascend 950 系列，CPU 架构兼容 aarch64 与 x86。
- [docs/quickstart.md:L36-L42](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L36-L42)：工具链依赖（gcc ≥ 7.3.0、cmake ≥ 3.19、GLIBC ≥ 2.28、Python ≥ 3.9，以及随 CANN toolkit 提供的 bisheng 编译器）。
- [docs/quickstart.md:L203-L207](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L203-L207)：三种安装方式的选型表——源码编译适合开发者（产物在 `install/`）、二进制 run 包适合部署（装到 `/usr/local/Ascend/shmem` 等）、pip/wheel 适合 Python 使用者。

#### 4.1.4 代码实践

**实践 1：通读 README 并确认环境基线（源码阅读型实践，约 20 分钟）**

1. 实践目标：确认自己是否有条件编译/运行 SHMEM，并知道差距在哪里。
2. 操作步骤：
   - 对照 [docs/quickstart.md:L36-L42](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L36-L42) 在本机执行 `gcc --version`、`cmake --version`、`ldd --version`、`python3 --version`。
   - 执行 `npu-smi info` 检查是否有可见的昇腾设备（无 NPU 时仅能做编译验证，见 [README.md:L206-L208](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L206-L208) 的 FAQ Q8）。
3. 需要观察的现象：各项工具版本是否达标；`npu-smi info` 是否列出设备。
4. 预期结果：整理出一张「我的环境清单」，标注每一项「满足 / 不满足」。无 NPU 环境不影响本手册前几讲的源码阅读型实践。
5. 编译与运行结果：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：SHMEM 与 MPI 最本质的编程模型差异是什么？

**答案**：MPI 的点对点通信是双边操作（send 必须与 recv 配对）；SHMEM 的 put/get 是单边操作——发起方 PE 直接读写远端 PE 的对称内存，远端进程不需要调用任何配对接口。这使得「通信」可以完全融合进计算 kernel 中。

**练习 2**：README 中列出的四类典型使用场景分别是什么？各举一个对应的项目内样例目录。

**答案**：四类场景见 [README.md:L166-L174](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L166-L174)：通算融合算子开发（对应 `examples/shmem_mega_moe`）、多机多卡数据同步（对应 `examples/allgather`）、低延迟卡间通信（对应 `examples/rdma_demo`、`examples/rdma_perftest_demo`）、Python 分布式训练适配（对应 `examples/python_extension`、`examples/torch_binding`）。

**练习 3**：SHMEM 支持在 x86 通用服务器或 NVIDIA GPU 上运行吗？

**答案**：不支持。CPU 架构上虽然兼容 x86（作为 Host CPU），但 [README.md:L251-L256](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L251-L256) 的「注意事项」明确本项目仅适配昇腾平台，不支持其他硬件架构。

### 4.2 模块二：核心术语速查——PE、对称内存与通信语义

#### 4.2.1 概念说明

glossary 是官方术语表（[docs/glossary.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md)），本模块挑出贯穿全手册的几组：

- **PE（Processing Element）**：SHMEM 通信参与者的编号，通常一个进程/一个 rank 对应一个 PE。`my_pe` 是「我是谁」，`n_pes` 是「一共多少人」。
- **对称内存 / 对称堆**：各 PE 按相同规则分配、可被 SHMEM 接口远程访问的内存区域；由 `aclshmem_malloc`/`aclshmem_free` 管理的那部分叫对称堆（Symmetric Heap）。
- **Team（通信域）**：一组 PE 组成的子集合，用于限定同步与通信的范围。初始化后默认的全局通信域是 `ACLSHMEM_TEAM_WORLD`。
- **通信语义类缩写**：RMA（远程内存访问，put/get）、AMO（原子内存操作，add/cas 等）、SO（信号操作）、P2P Sync（点对点同步）、CC（集合通信）、MO（内存保序）。
- **数据通路缩写**：D2D（设备到设备）、D2H/H2D（设备↔本端主机）、D2rH/rH2D（设备↔远端主机）。

#### 4.2.2 核心流程

理解对称内存的关键是「偏移一致」这四个字。各 PE 的对称堆虽然在不同设备上、基址各不相同，但只要分配顺序与大小一致，同一个「堆内偏移」在所有 PE 上指向逻辑上相同的位置。于是远端地址换算可以形式化为：

\[ \text{remote\_addr}(pe) = \text{heap\_base}(pe) + \big(\text{local\_addr} - \text{heap\_base}(\text{my\_pe})\big) \]

这就是为什么 put/get 接口里传的是「本地视角的对称地址 + 目标 pe 编号」：库负责用上式把本地地址翻译成目标 PE 上的实际地址（`aclshmem_ptr` 接口甚至把这个翻译能力直接暴露给用户，见 4.3.3）。

一个最小的心智模型：

```text
PE0 对称堆 [base0 + 0 .. base0 + N]     PE1 对称堆 [base1 + 0 .. base1 + N]
              │                                       ▲
              │  put(dst=偏移100, src=本地, pe=1)      │
              └──────────── 数据直达对端偏移 100 ───────┘
```

#### 4.2.3 源码精读

术语表中与本模块直接相关的行：

- [docs/glossary.md:L15](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L15)：`SHMEM` 词条——面向昇腾平台的多机多卡内存通信库；同表还给出 `OpenSHMEM` 规范词条（L16），说明本项目 API 命名与该规范的渊源。
- [docs/glossary.md:L19-L22](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L19-L22)：`PE`、`Team`、`Symmetric Memory`、`Symmetric Heap` 四个词条。注意 Team 词条明确 `ACLSHMEM_TEAM_WORLD` 是初始化后的默认全局通信域。
- [docs/glossary.md:L28-L33](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L28-L33)：RMA / AMO / SO / P2P Sync / CC / MO 六个通信语义词条，覆盖了本手册第 3、4 单元的全部接口类别。
- [docs/glossary.md:L60-L66](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L60-L66)：D2D / D2H / H2D / D2rH / rH2D 五条数据通路，以及 gm2gm（Global Memory 到 Global Memory）、ub2gm（Unified Buffer 到 Global Memory）两个 Device 侧数据面命名。
- [docs/glossary.md:L131-L132](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L131-L132)：**低阶接口**（`engine/` 目录下直接驱动引擎的 `aclshmemx_udma_*`、`aclshmemx_roce_*` 等）与**高阶接口**（`gm2gm/`、`host/data_plane/` 下屏蔽引擎细节的 `aclshmem_put_*` 等）的区分——这个二分法在第 5 单元会反复出现。

术语在代码里的「实体化」可以看共享类型头文件：

- [include/host_device/shmem_common_types.h:L73](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L73)：`enum aclshmem_team_index_t { ACLSHMEM_TEAM_INVALID = -1, ACLSHMEM_TEAM_WORLD = 0 };` ——默认通信域 WORLD 的编号是 0。
- [include/host_device/shmem_common_types.h:L136](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L136)：`ACLSHMEM_MAX_PES` 为 16384，即一个 SHMEM 任务最多支持 16384 个 PE。
- [include/host_device/shmem_common_types.h:L377-L426](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L377-L426)：全局状态结构 `aclshmem_device_host_state_t`，包含 `mype`/`npes`、本地与各 PE 的堆基址数组（`p2p_device_heap_base` 等）、team 池、同步池、各引擎配置。这个结构是「Host 侧把世界画像交给 Device 侧」的载体，第 4 单元（u4-l1）会专门剖析它，本讲只需知道它存在。
- Host 侧查询 PE 编号的接口：[include/host/team/shmem_host_team.h:L80-L90](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/team/shmem_host_team.h#L80-L90) 定义 `aclshmem_my_pe()`（返回 0 到 npes-1 的本 PE 编号）与 `aclshmem_n_pes()`（返回 PE 总数），并且每个 API 旁边都有一条 `#define shmem_my_pe aclshmem_my_pe` 别名宏，方便熟悉 OpenSHMEM 的用户按标准命名书写。

#### 4.2.4 代码实践

**实践 2：建立自己的术语卡（阅读型实践，约 15 分钟）**

1. 实践目标：把 glossary 中高频术语固化为个人速查卡。
2. 操作步骤：打开 [docs/glossary.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md)，将「编程模型与接口」「通信语义与同步」「通信引擎、协议与数据通路」三张表抄录/摘录为一张自制的三列卡片（术语 / 展开 / 一句话理解）。
3. 需要观察的现象：哪些术语在 README 的代码结构图（如 `gm2gm`、`ub2gm`、`engine/`）里同时是目录名？这些「术语即目录」的对应关系是理解仓库布局的捷径。
4. 预期结果：得到至少覆盖 PE、Team、对称堆、RMA、AMO、SO、MO、MTE、RDMA、SDMA、UDMA、gm2gm、ub2gm、高阶/低阶接口的一张卡片。
5. 本实践为纯阅读任务，无需运行验证。

#### 4.2.5 小练习与答案

**练习 1**：`ACLSHMEM_TEAM_WORLD` 是什么？它在枚举里的值是多少？

**答案**：初始化后默认存在的全局通信域，包含全部 PE；定义在 [include/host_device/shmem_common_types.h:L73](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L73)，枚举值为 0（`ACLSHMEM_TEAM_INVALID` 为 -1）。

**练习 2**：`put` 和 `get` 方向上有什么区别？「对称内存」约束施加在哪个操作数上？

**答案**：`put` 是本地 PE 主动把数据**写入**远端 PE 的对称地址；`get` 是本地 PE 主动从远端 PE 的对称地址**读取**数据到本地（见 [docs/glossary.md:L34-L35](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L34-L35)）。约束施加在「远端那一侧」的操作数上：put 的目的地址、get 的源地址必须位于对称堆，因为库要做堆内偏移换算；本地一侧的缓冲可以是任意合法地址（详见 4.3.3 中 putmem/getmem 的注释）。

**练习 3**：`NBI` 后缀（如 `putmem_nbi`）代表什么？使用它必须配合什么？

**答案**：Non-Blocking Immediate，非阻塞立即操作：调用后立即返回，不等待操作完成；必须配合 `quiet()` 或 `fence()` 等内存序屏障才能确保完成状态可靠送达与全局顺序，见 [docs/glossary.md:L36-L37](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L36-L37)。

### 4.3 模块三：双侧接口体系与 shmem.h 总入口

#### 4.3.1 概念说明

`include/shmem.h` 是所有对外 API 的汇总入口，它本身不含任何实现，只做条件包含。阅读它就能看出接口体系的两条主轴：

- **按执行位置分**：Host 侧接口在 CPU 上调用（前缀宏 `ACLSHMEM_HOST_API`）；Device 侧接口在 AICore kernel 里调用（前缀宏 `ACLSHMEM_DEVICE`），且参数中大量出现 `__gm__` 修饰的指针。
- **按功能分**：初始化（init）、内存堆（mem/heap）、数据面（data_plane：RMA/SO/P2P 同步/CC）、通信域（team）、工具（utils），Host 与 Device 两侧各有一套相近的能力划分。

另一个值得注意的命名规律：`aclshmem_` 前缀多为「标准风格」接口（并有 `shmem_` 别名），`aclshmemx_` 前缀多为「扩展风格」接口（如带扩展属性、多实例、引擎配置等）。

#### 4.3.2 核心流程

`include/shmem.h` 的包含逻辑可以画成：

```text
# include "shmem.h"
├── 若在 AICore 编译环境（__CCE_AICORE__ 或 __CCE_KT_TEST__）
│   ├── device/gm2gm/*            ← Device 高阶数据面：amo/cc/mo/p2p_sync/rma/so
│   ├── device/gm2gm/engine/*     ← Device 低阶直驱：mte/rdma/sdma/udma
│   ├── device/ub2gm/*            ← Device ub2gm 数据面及其 engine
│   ├── device/shmem_def.h
│   └── device/team/*             ← Device 侧通信域
└── 无条件包含（Host 侧）
    ├── host/shmem_host_def.h     ← 公共定义
    ├── host/mem/shmem_host_heap.h        ← 对称堆分配
    ├── host/init/shmem_host_init.h       ← 初始化/终止
    ├── host/utils/shmem_host_exception.h
    ├── host/data_plane/*         ← Host 数据面：rma/so/p2p_sync/cc
    ├── host/team/shmem_host_team.h
    └── host/utils/shmem_log.h
```

也就是说：同一份 `shmem.h`，在 Host 编译单元里展开成 Host API 声明，在 AscendC kernel 编译单元里额外展开 Device API 声明——这就是「双侧接口、单入口」的实现方式。

#### 4.3.3 源码精读

- [include/shmem.h:L15-L39](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h#L15-L39)：Device 侧头文件的条件包含块。第 15 行的 `#if defined(__CCE_AICORE__) || defined(__CCE_KT_TEST__)` 是关键——只有用 bisheng 编译 AscendC kernel 时这些宏才成立，Host 侧编译时整块被跳过。其中 `device_simt/` 系列（L32-L38）仅在定义 `USE_SIMT` 时引入，是 SIMT 编程模式的扩展。

- [include/shmem.h:L41-L50](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h#L41-L50)：Host 侧头文件无条件包含列表，与 4.3.2 的树状图逐一对应。

Host 侧代表性 API（本讲认脸即可，后续单元逐个精读）：

- 初始化：[include/host/init/shmem_host_init.h:L146](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L146) 声明 `aclshmemx_init_attr(bootstrap_flags, attributes)`，一切从这里开始；[L195](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L195) 与 [L208](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L208) 分别是单实例 `aclshmem_finalize` 与多实例 `aclshmemx_finalize` 的终止接口。初始化属性结构体 `aclshmemx_init_attr_t`（含 `my_pe`、`n_pes`、`ip_port`、`local_mem_size` 等）定义在 [include/host/shmem_host_def.h:L181-L195](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L181-L195)。

- 对称堆：[include/host/mem/shmem_host_heap.h:L28](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L28) `aclshmem_malloc(size)`——分配对称内存（未初始化）；[L71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L71) `aclshmemx_malloc(size, mem_type)` 是可指定 Host/Device 侧的扩展版本；[L114](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L114) `aclshmemx_get_heap_base` 返回本地对称堆基址——正是 4.2.2 那个地址换算公式里的 `heap_base(my_pe)`。

- Host 数据面：[include/host/data_plane/shmem_host_rma.h:L543](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/data_plane/shmem_host_rma.h#L543) `aclshmem_putmem(dst, src, elem_size, pe)` 与 [L560](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/data_plane/shmem_host_rma.h#L560) `aclshmem_getmem(...)`，注意注释里明确「dst/src 中指向远端的那一侧必须位于对称内存，因为它要被翻译为 pe 上的对应地址」；[L73](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/data_plane/shmem_host_rma.h#L73) `aclshmem_ptr(ptr, pe)` 直接把这个地址翻译能力开放给用户。

- 宏生成模式：[include/host/data_plane/shmem_host_rma.h:L98](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/data_plane/shmem_host_rma.h#L98) 展示了 `aclshmem_##NAME##_put(...)` 的宏模板——库用宏为每种数据类型（float/int32/...）批量生成同名接口，所以你会在头文件里看到大量「函数名 + 类型后缀」的组合，而不是每类手写一份。

Device 侧代表性 API：

- [include/device/gm2gm/shmem_device_rma.h:L42-L55](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h#L42-L55)：`ACLSHMEM_TYPE_FUNC` 宏列举 Device 接口支持的 13 种数据类型（half/float/double/int8..uint64/char/bfloat16）。
- [include/device/gm2gm/shmem_device_rma.h:L99-L104](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h#L99-L104)：`aclshmem_NAME_p` 单元素低时延 put，签名是 `(__gm__ TYPE* dst, const TYPE value, int pe)`——注意 `__gm__` 指针与 `ACLSHMEM_DEVICE` 修饰，这是 kernel 内接口的两个标志特征。
- [include/device/gm2gm/shmem_device_rma.h:L172](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h#L172)：Device 侧 `aclshmem_getmem`，注释说明底层支持 MTE、RDMA、SDMA 或 UDMA 传输——「高阶接口自动选择引擎」这一设计会贯穿第 4、5 单元。
- `ACLSHMEM_DEVICE` 宏本体定义在 [include/host_device/shmem_common_types.h:L35](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L35)，本质是 `__attribute__((always_inline)) __aicore__ __inline__`，即「Device 内联函数」。

#### 4.3.4 代码实践

**实践 3：手工绘制 Host/Device 接口分层图（本讲主实践）**

1. 实践目标：不看本讲义，独立画出 SHMEM 的接口分层结构，并标注 5 个自己最感兴趣的 API。
2. 操作步骤：
   - 打开 [include/shmem.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h)（53 行），把所有 `#include` 按 Device 侧 / Host 侧分成两栏抄下来。
   - 为每个头文件标注功能类别（初始化 / 堆 / RMA / 信号 / 同步 / 集合 / Team / 低阶引擎）。
   - 从下列入口任选方式浏览 API 声明：Host 侧看 [shmem_host_init.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h)、[shmem_host_heap.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h)、[shmem_host_rma.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/data_plane/shmem_host_rma.h)；Device 侧看 [shmem_device_rma.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h)。
   - 挑出 5 个你最感兴趣的 API（建议覆盖不同类别），为每个写一句话用途说明。
   - 把成果画在纸上或画图工具里，形如：

   ```text
   ┌─────────────────────── Host（CPU 控制面）───────────────────────┐
   │ init: aclshmemx_init_attr / aclshmem_finalize                    │
   │ heap: aclshmem_malloc / aclshmemx_get_heap_base                  │
   │ data_plane: aclshmem_putmem / getmem / put_signal / barrier ...  │
   │ team: aclshmem_my_pe / team_split ...                            │
   └────────────────────────────┬────────────────────────────────────┘
                                │ 初始化时下发全局状态(device_host_state)
   ┌────────────────────────────▼────────────────────────────────────┐
   │ Device（AICore 数据面）                                          │
   │ 高阶: aclshmem_getmem / aclshmem_int32_p / atomic / wait_until   │
   │ 低阶: engine/ 下 mte / rdma / sdma / udma 直驱接口               │
   └──────────────────────────────────────────────────────────────────┘
   ```

3. 需要观察的现象：Device 侧头文件几乎都 `#include "kernel_operator.h"`（如 [shmem_device_rma.h:L15](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h#L15)），这是 AscendC 编程的头文件，说明这些接口只能在 kernel 代码里用；Host 侧头文件则 include 的是 `acl/acl_rt.h` 等 ACL 运行时头（见 [shmem_common_types.h:L15](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L15)）。
4. 预期结果：一张两层的分层图 + 5 条 API 说明。例如参考选法：`aclshmemx_init_attr`（一切的开始）、`aclshmem_malloc`（对称堆分配）、`aclshmem_putmem`（Host 单边写）、`aclshmem_my_pe`（我是谁）、`aclshmem_getmem`（Device kernel 内读远端）。
5. 本实践为源码阅读型任务，无需运行验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么同一份 `shmem.h` 在 Host 程序和 AscendC kernel 里都能 include，却不会把 Device 声明泄漏到 Host 编译单元？

**答案**：因为 [include/shmem.h:L15](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h#L15) 用 `#if defined(__CCE_AICORE__) || defined(__CCE_KT_TEST__)` 把 Device 侧头文件整体包起来；该宏只在 bisheng 编译 AICore 代码时定义，Host 编译器下条件不成立，整块被预处理掉。

**练习 2**：`aclshmem_malloc` 与 `aclshmemx_malloc` 有什么区别？

**答案**：`aclshmem_malloc(size)` 只有一个大小参数（声明见 [shmem_host_heap.h:L28](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L28)）；`aclshmemx_malloc(size, mem_type)` 多了 `mem_type` 参数，可指定分配在 Device 侧还是 Host 侧（默认 `DEVICE_SIDE`，声明见 [L71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L71)）。`mem_type` 的两个取值 `HOST_SIDE`/`DEVICE_SIDE` 定义在 [shmem_common_types.h:L89-L92](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L89-L92)。这体现了 `aclshmemx_` 扩展前缀接口比 `aclshmem_` 标准前缀接口能力更丰富的命名规律。

**练习 3**：Device 侧接口签名里的 `__gm__` 是什么意思？

**答案**：`__gm__` 标识指针指向 Device 的 Global Memory（NPU 全局可寻址内存），与指向片上 Unified Buffer 的 `__ubuf__` 相对；术语见 [docs/glossary.md:L81-L82](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L81-L82)。gm2gm 接口族的名称就来自「GM 到 GM」的数据通路。

### 4.4 模块四：通信引擎与数据通路

#### 4.4.1 概念说明

SHMEM 的高阶接口（put/get/AMO）之所以能「一份代码跑多种硬件通路」，是因为底层抽象了多种传输引擎：

- **MTE（Memory Transfer Engine）**：AICore 侧内存搬运引擎，支持 D2D、D2H、H2D、D2rH、rH2D 等通路。
- **xDMA**：对高速 DMA 类引擎的统称，具体包括 RDMA（Remote DMA，跨节点 RoCE 网络直访远端内存）、SDMA（System DMA，仅 A3 平台）、UDMA（Unified DMA，Ascend950 平台）。

平台与引擎的支持关系是**有条件的**：例如 SDMA 仅支持 A3，UDMA 仅支持 Ascend950 且依赖 HCOMM 资源接口；这些约束写在 quickstart 的 CANN 版本表中。选哪个引擎由初始化属性里的 `data_op_engine_type` 指定（可按位或组合），运行时按目标 PE 与拓扑自动分派。

#### 4.4.2 核心流程

引擎在代码中的两处映射关系：

```text
① 传输引擎枚举（host 侧库内部）
   aclshmem_transport_t: ACLSHMEM_TRANSPORT_MTE / ROCE / SDMA / UDMA
   （shmem_host_def.h）

② 数据操作引擎枚举（用户初始化属性 option_attr.data_op_engine_type）
   data_op_engine_type_t: ACLSHMEM_DATA_OP_MTE=0x01 / SDMA=0x02 / ROCE=0x04 / UDMA=0x08
   （shmem_common_types.h，可按位组合）

   aclshmemx_init_attr(ACLSHMEMX_INIT_WITH_DEFAULT, &attr)
        └── attr.option_attr.data_op_engine_type 决定本次任务启用哪些引擎
              └── 高阶 put/get 运行时按目标 PE 选择已启用的引擎执行
```

D2D / D2H / H2D / D2rH / rH2D 五条通路的方向示意：

```text
        本节点                                   远端节点
   ┌─────────┐  H2D ─▶ ┌───────┐          ┌─────────┐
   │ Host 内存│ ◀── D2H │ NPU GM │ ◀─ D2rH ─│ Host 内存│
   └─────────┘         └───────┘   rH2D ─▶ └─────────┘
                          │  ▲
                          D2D（本机卡间 / 跨机卡间，MTE 或 RDMA/SDMA/UDMA）
```

#### 4.4.3 源码精读

- [README.md:L100-L110](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L100-L110)：README「通信通路覆盖」一节，给出全链路通路图（以 910A3 为例），并列出 MTE Engine（支持 D2D/D2H/H2D/D2rH/rH2D）与 xDMA Engine 两大类。

- [docs/quickstart.md:L60-L65](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L60-L65)：CANN 版本支持矩阵——不同 HDK/CANN 版本下 D2D、D2H/H2D、D2rH/rH2D 各通路可用的引擎组合不同；例如 9.1.0 行明确「SDMA（仅支持 A3）」「UDMA（仅支持 Ascend950）」。

- [docs/glossary.md:L47-L52](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L47-L52)：MTE、DMA、xDMA、SDMA、UDMA、RDMA 六个引擎词条，说明 SDMA/UDMA/RDMA 均可作为 RMA/AMO 的底层传输后端。

- [include/host/shmem_host_def.h:L129-L134](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L129-L134)：库内传输引擎枚举 `aclshmem_transport_t`，MTE/ROCE/SDMA/UDMA 各占一个比特。

- [include/host_device/shmem_common_types.h:L78-L84](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L78-L84)：用户可见的 `data_op_engine_type_t` 枚举，取值 0x01/0x02/0x04/0x08，为按位组合留了空间；它正是初始化属性 `option_attr.data_op_engine_type` 的类型（该字段见 [include/host/shmem_host_def.h:L186-L192](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L186-L192)，默认值 `ACLSHMEM_DATA_OP_MTE`）。

- [docs/quickstart.md:L570-L574](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L570-L574)：性能调优建议中「优先使用 Device 侧接口（减少 Host-Device 交互）」「大批次数据传输时开启 RDMA 协议（编译时加 `-DSHMEM_RDMA=ON`）」，从工程角度印证了引擎选择的现实意义。

#### 4.4.4 代码实践

**实践 4：制作平台-引擎-条件对照表（阅读型实践，约 15 分钟）**

1. 实践目标：搞清楚「我的芯片上能用哪些引擎」，避免后续学传输层时被平台条件绕晕。
2. 操作步骤：
   - 细读 [docs/quickstart.md:L60-L65](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L60-L65) 的表格，逐行摘出「CANN 版本 / D2D 引擎 / D2H/H2D 引擎 / D2rH/rH2D 引擎 / 附加依赖」。
   - 对照 `examples/` 目录名（如 `udma_demo`、`sdma`、`rdma_demo`、`cmo`），为每个引擎找一个对应的示例工程名填入表中。
3. 需要观察的现象：哪些单元格是空的（说明该 CANN 版本下此通路无引擎支持）；哪些引擎带括号限制（SDMA 仅 A3、UDMA 仅 Ascend950）。
4. 预期结果：一张五行以内的对照表，例如第一行「CANN 9.1.0 / D2D: MTE+RDMA+SDMA(A3)+UDMA(950) / D2H/H2D: MTE+SDMA(A3) / 依赖: SDMA 需 ops 包，UDMA 需 HCOMM 资源接口」。
5. 本实践为纯阅读任务，无需运行验证；若要实际运行 `examples/udma_demo` 等，需具备对应平台硬件——待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`data_op_engine_type_t` 中的枚举值为什么取 0x01/0x02/0x04/0x08 而不是 0/1/2/3？

**答案**：因为引擎类型支持按位或组合（例如 `ACLSHMEM_DATA_OP_MTE | ACLSHMEM_DATA_OP_ROCE` 表示同时启用 MTE 与 RDMA），每个取值占独立比特才能互不干扰；定义见 [include/host_device/shmem_common_types.h:L78-L84](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L78-L84)。

**练习 2**：SDMA 和 UDMA 分别支持哪些平台？启用它们各有什么附加条件？

**答案**：SDMA 仅支持 A3，需要安装与 toolkit 版本和设备类型匹配的 ops 包；UDMA 仅支持 Ascend950，需要 Ascend950 对应的 toolkit 包和 ops 包，并依赖 CANN 9.1.0 提供的 HCOMM 资源接口。出处：[docs/quickstart.md:L62](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L62)。

**练习 3**：README 说「支持 AICore 直驱 MTE、xDMA 使能 D2D/D2H/H2D/D2rH/rH2D 通信」，其中「直驱」对应 glossary 里的哪个概念？

**答案**：对应「低阶接口」（engine-specific API）：直接暴露底层传输引擎通信语义、位于 `engine/` 目录下的接口（如 `aclshmemx_udma_*`、`aclshmemx_roce_*`），与屏蔽引擎细节的「高阶接口」相对；见 [docs/glossary.md:L131-L132](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md#L131-L132)。README 最新动态（[L14-L18](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L14-L18)）提到的 v1.3.0「AICore 直驱能力」即指此。

## 5. 综合实践

**综合任务：产出你的《SHMEM 第一印象报告》（纯源码阅读，约 45 分钟）**

把本讲四个模块的产出整合成一页文档，要求包含四部分：

1. **定位陈述**：用不超过 3 句话向一位没听过 SHMEM 的同事介绍这个项目（依据 [README.md:L64-L71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L64-L71)）。
2. **接口分层图**：实践 3 的成果——Host/Device 双层图 + 5 个 API 的一句话说明（依据 [include/shmem.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h) 及其包含的各头文件）。
3. **术语卡**：实践 2 的成果，至少 14 个术语（依据 [docs/glossary.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md)）。
4. **平台-引擎对照表**：实践 4 的成果，并回答「如果我在 A3 平台、CANN 9.1.0 环境下开发，初始化时理论上可以启用哪些引擎？」（依据 [docs/quickstart.md:L60-L65](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/quickstart.md#L60-L65)；参考答案：MTE、RDMA、SDMA——UDMA 仅限 Ascend950）。

验收标准：把这份报告拿给同组的同学看，对方不打开仓库也能说出「SHMEM 是什么、接口分几层、引擎有几种」。这份报告也是你后续学习的对照底稿——每学完一讲，回来给图上的对应位置补细节。

## 6. 本讲小结

- SHMEM 是面向昇腾 NPU 的、基于对称内存的多机多卡通信加速库，靠「单边 put/get」替代双边 send/recv，核心价值是简化卡间通信并支撑通算融合算子（[README.md:L64-L71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L64-L71)）。
- 接口分 Host/Device 双侧：Host 管初始化、对称堆、Team、全局同步（CPU 控制面）；Device 管 RMA、AMO、设备级同步（AICore 数据面），同一入口 `include/shmem.h` 通过 `__CCE_AICORE__` 条件编译分别展开（[include/shmem.h:L15-L50](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h#L15-L50)）。
- 对称内存的本质是「偏移一致」：远端地址 = 对端堆基址 + 本地堆内偏移，`aclshmem_ptr`/`aclshmemx_get_heap_base` 是理解这一换算的两个入口。
- 核心术语体系：PE / Team / 对称堆是编程模型三件套；RMA / AMO / SO / P2P Sync / CC / MO 是通信语义六件套；glossary 是权威字典。
- 通信引擎分 MTE 与 xDMA（RDMA/SDMA/UDMA）两族，平台支持条件不同（SDMA 仅 A3、UDMA 仅 Ascend950），用户通过 `option_attr.data_op_engine_type` 按位组合启用，高阶接口运行时自动分派引擎。
- 命名规律：`aclshmem_` 标准风格接口带 `shmem_` 别名宏；`aclshmemx_` 扩展风格接口能力更丰富（多实例、引擎配置、指定 mem_type 等）。

## 7. 下一步学习建议

- 下一讲（u1-l2「环境准备与源码编译」）：学习 `scripts/build.sh`、CMakeLists 与三种安装方式，动手把仓库编译出 `libshmem.so`。
- 若想先「看到效果」，可以直接跳到 u1-l4（init 示例解析）再回头补编译细节；但建议按顺序学完 u1-l2，因为后续所有实践都依赖编译产物。
- 建议持续阅读的源码：`include/host/shmem_host_def.h`（所有枚举与初始化结构体，是后续 u2 单元的预习材料）和 `include/host_device/shmem_common_types.h`（全局状态结构 `aclshmem_device_host_state_t`，是理解 Host→Device 状态下发的一把钥匙）。
- 遇到陌生缩写随时回查 [docs/glossary.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/glossary.md)；官方完整文档站见 README 顶部徽章链接（https://shmem-doc.pages.dev/）。
