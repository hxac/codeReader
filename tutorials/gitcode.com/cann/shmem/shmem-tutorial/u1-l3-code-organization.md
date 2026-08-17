# u1-l3 目录结构与代码组织

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出仓库顶层 `include/`、`src/`、`examples/`、`tests/`、`docs/`、`scripts/`、`tools/` 各自的职责边界。
2. 理解 `include/` 下 `host/`、`device/`、`host_device/` 三分法，以及 `gm2gm`、`ub2gm`、`engine`、`team`、`data_plane` 等目录命名的含义。
3. 掌握「从 API 名字到头文件、从头文件到实现文件」的定位方法，能独立整理一张 API 到文件的速查表。
4. 知道官方文档中的目录树与真实代码树可能存在滞后，养成「以代码为准」的源码阅读习惯。

## 2. 前置知识

本讲建立在 u1-l1 已建立的概念之上，先快速回顾，再补充本讲需要的新概念。

**回顾（来自 u1-l1）：**

- SHMEM 是面向昇腾 NPU 的对称内存分布式通信库，接口分 **Host 侧**（CPU 控制面）与 **Device 侧**（AICore 数据面）。
- API 前缀规则：`aclshmem_` 是标准接口，`aclshmemx_` 是扩展接口。
- PE 是通信参与者的编号；对称堆保证各 PE 上「相同分配顺序 → 相同堆内偏移」。

**本讲新增的基础概念：**

- **条件编译**：C/C++ 预处理器根据宏（如 `__CCE_AICORE__`）决定哪些代码参与编译。SHMEM 用它让同一份 `shmem.h` 在 Host 编译器和 AscendC（AICore）编译器下分别展开不同接口集。
- **内部接口命名**：源码中以 `shmemi_` / `aclshmemi_` 开头（`i` 即 internal）的符号是库内部实现细节，不对外承诺兼容；对外接口才用 `aclshmem_` / `aclshmemx_`。
- **头文件 / 实现文件**：`.h` 里放声明（函数签名），`.cpp` / `.hpp` 里放实现（函数体）。找 API 用法先看 `.h` 的注释，追执行逻辑再看 `.cpp`。
- **可见性宏**：`ACLSHMEM_HOST_API` 展开为 `__attribute__((visibility("default")))`，把符号标记为「从动态库导出」，是识别「这是对外 API」的可靠信号。

## 3. 本讲源码地图

真实代码树（仅列本讲涉及的关键路径，`└──` 为目录）：

```text
shmem/                       # 项目根目录
├── docs/                    # 文档；本讲主要参考 code_organization.md
├── examples/                # 30 个左右样例工程，每个子目录一个可编译样例
├── include/                 # 全部对外头文件（API 声明都在这里）
│   ├── shmem.h              # 所有对外 API 的汇总入口
│   ├── device/              # Device 侧头文件
│   │   ├── shmem_def.h      # Device 侧公共定义
│   │   ├── gm2gm/           # gm2gm 数据面（高阶 + engine 低阶）
│   │   ├── ub2gm/           # ub2gm 数据面（高阶 + engine 低阶）
│   │   └── team/            # Device 侧通信域接口
│   ├── device_simt/         # SIMT 变体接口（镜像 device/ 结构）
│   ├── host/                # Host 侧头文件
│   │   ├── shmem_host_def.h # Host 侧公共定义（错误码、枚举、结构体）
│   │   ├── init/            # 初始化接口
│   │   ├── mem/             # 内存/堆管理接口
│   │   ├── team/            # 通信域管理接口
│   │   ├── data_plane/      # Host 侧 CPU 驱动数据面接口
│   │   └── utils/           # 日志、异常等 DFX 接口
│   └── host_device/         # Host/Device 共用（公共类型、宏）
├── scripts/                 # build.sh、set_env.sh、run_examples.sh 等
├── src/                     # 源码实现（与 include 大体镜像）
│   ├── device/              # Device 侧实现（gm2gm/ub2gm/team/utils）
│   ├── device_simt/         # SIMT 变体实现
│   ├── host/                # Host 侧实现（init/mem/sync/team/bootstrap/transport/...）
│   ├── host_device/         # 共用实现
│   └── python/              # Python 包源码
├── tests/                   # 测试用例
│   ├── examples/            # 样例级功能测试
│   ├── package_smoke/       # 包冒烟测试
│   └── unittest/            # 单元测试（host/ 与 device/ 两棵子树）
└── tools/                   # rootinfo 等辅助工具
```

| 文件 | 作用 |
| --- | --- |
| `docs/code_organization.md` | 官方代码组织文档，本讲的对照基准 |
| `README.md` | 项目自述，其「三、代码结构」一节是另一份目录树 |
| `include/shmem.h` | 对外 API 汇总入口，本讲剖析其条件编译结构 |
| `include/host/shmem_host_def.h` | Host 侧公共定义：错误码、bootstrap 模式、传输引擎枚举 |
| `include/host/mem/shmem_host_heap.h` | 堆管理 API 声明（`aclshmem_malloc` 在此声明） |
| `src/host/mem/shmem_mm.cpp` | 堆管理 API 实现（`aclshmem_malloc` 在此实现） |
| `include/device/gm2gm/shmem_device_rma.h` | Device 侧 gm2gm RMA 接口（宏生成 put/get 家族） |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**include**、**src**、**examples**、**tests**。

### 4.1 模块一：include —— 对外 API 的分层规则

#### 4.1.1 概念说明

`include/` 存放全部对外头文件，即「使用者能看见什么」。它解决的问题是：一个库同时服务两类调用方（Host 上的 C/C++ 程序、AICore 上的 AscendC kernel），还要按数据通路和接口层级继续细分。SHMEM 的组织规则可以概括为三条：

1. **按调用方分**：`host/`（CPU 侧）、`device/`（kernel 侧）、`host_device/`（两侧共用的纯类型/宏定义）。
2. **Device 侧再按数据通路分**：`gm2gm/`（远端 Global Memory 到本地 Global Memory）、`ub2gm/`（本地 Unified Buffer 直达远端 Global Memory）；每条通路下再分高阶接口与 `engine/` 直驱低阶接口。
3. **Host 侧再按功能分**：`init/`、`mem/`、`team/`、`data_plane/`、`utils/`。

理解了这三条规则，任何一个头文件路径都能「望文生义」。例如 `include/device/ub2gm/engine/shmem_device_mte.h` 一眼可读出：Device 侧、ub2gm 通路、直驱引擎层、MTE 引擎的接口。

#### 4.1.2 核心流程

使用者只写一行 `#include "shmem.h"`，展开过程如下：

```text
#include "shmem.h"
    │
    ├─ 若在 AICore 编译环境（定义了 __CCE_AICORE__ 或 __CCE_KT_TEST__）
    │      └─ 展开 device/ 分支：
    │           gm2gm 高阶（rma/amo/so/cc/p2p_sync/mo）
    │           gm2gm/engine 低阶（mte/rdma/sdma/udma）
    │           ub2gm 高阶 + ub2gm/engine 低阶
    │           device/shmem_def.h、device/team/
    │           （若再定义 USE_SIMT，追加 device_simt/ 一组）
    │
    └─ 无论哪种编译环境，都展开 host/ 分支：
           shmem_host_def.h、mem/、init/、utils/、data_plane/、team/
```

也就是说：**Device 分支是条件编译的，Host 分支是无条件编译的**。同一个 `shmem.h` 在两种编译器下呈现两套接口面。

目录命名速查表：

| 目录名 | 含义 |
| --- | --- |
| `gm2gm` | global memory → global memory，kernel 里两端的 `__gm__` 缓冲区互拷 |
| `ub2gm` | unified buffer → global memory，数据从 AICore 的 UB 直发远端 GM |
| `engine` | 直驱通信引擎的低阶接口，绕过高阶封装 |
| `team` | 通信域（communication domain）管理 |
| `data_plane` | Host 侧 CPU 直接驱动的数据面（put/get/信号/同步/集合） |
| `host_device` | 两侧共用的公共类型与宏，不含逻辑 |
| `device_simt` | SIMT 执行模式的接口变体，目录结构镜像 `device/` |

#### 4.1.3 源码精读

**入口文件的条件编译骨架。** [include/shmem.h:15-39](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h#L15-L39) 用 `#if defined(__CCE_AICORE__) || defined(__CCE_KT_TEST__)` 圈出 Device 侧头文件集合——只有在 AscendC 编译器环境里，这一批 `device/...` 头文件才会被引入：

```c
#if defined(__CCE_AICORE__) || defined(__CCE_KT_TEST__)
#include "device/gm2gm/shmem_device_amo.h"
#include "device/gm2gm/shmem_device_rma.h"
...
#include "device/ub2gm/shmem_device_rma.h"
// simt
#if defined(USE_SIMT)
#include "device_simt/gm2gm/shmem_device_simt_rma.h"
...
#endif
#endif
```

而 [include/shmem.h:41-50](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/shmem.h#L41-L50) 的 Host 侧包含不在任何 `#if` 内，任何环境都可见。这就是「单一总入口、双侧条件展开」的实现方式。

**识别对外 API 的信号。** [include/host/shmem_host_def.h:42-44](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L42-L44) 定义了可见性宏：

```c
/// \def ACLSHMEM_HOST_API
/// \brief A macro that identifies a function on the host side.
#define ACLSHMEM_HOST_API __attribute__((visibility("default")))
```

在 `include/host/` 下看到 `ACLSHMEM_HOST_API` 修饰的函数声明，就可以确定它是导出给用户的 Host 侧 API；Device 侧对应的修饰符是 `ACLSHMEM_DEVICE`。同一文件还集中放了错误码枚举、bootstrap 模式枚举（[include/host/shmem_host_def.h:106-112](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L106-L112)）和传输引擎枚举（[include/host/shmem_host_def.h:129-135](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L129-L135)，MTE/ROCE/SDMA/UDMA 四种）——「公共定义收口在一个 def 头文件」也是本仓库的惯例。

**声明 + 短名别名模式。** [include/host/mem/shmem_host_heap.h:28-29](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L28-L29) 是 `aclshmem_malloc` 的声明处：

```c
ACLSHMEM_HOST_API void* aclshmem_malloc(size_t size);
#define shmem_malloc aclshmem_malloc
```

下一行的 `#define` 把 ISO SHMEM 风格的短名 `shmem_malloc` 别名到 `aclshmem_malloc`，方便从标准 SHMEM 迁移过来的代码少改拼写。这个「声明 + 别名成对出现」的模式贯穿所有对外头文件。

**Device 侧的宏生成模式。** [include/device/gm2gm/shmem_device_rma.h:334-335](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h#L334-L335) 声明了按字节的 `aclshmem_putmem`（注意参数是 `__gm__` 指针，这是 AscendC 的 Global Memory 地址空间标记）：

```c
ACLSHMEM_DEVICE void aclshmem_putmem(__gm__ void* dst, __gm__ void* src, uint32_t elem_size, int32_t pe);
#define shmem_putmem aclshmem_putmem
```

而按类型的 put 家族不是逐个手写的：[include/device/gm2gm/shmem_device_rma.h:365-370](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h#L365-L370) 用一个宏模板加一行 `ACLSHMEM_TYPE_FUNC(...)` 批量生成 13 种数据类型（half/float/double/int8.../bfloat16，类型清单定义在 [include/device/gm2gm/shmem_device_rma.h:40-54](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/device/gm2gm/shmem_device_rma.h#L40-L54)）的同名函数：

```c
#define ACLSHMEM_PUT_TYPENAME_MEM(NAME, TYPE) \
    ACLSHMEM_DEVICE void aclshmem_##NAME##_put(__gm__ TYPE* dst, __gm__ TYPE* src, uint32_t elem_size, int32_t pe)

/** \cond */
ACLSHMEM_TYPE_FUNC(ACLSHMEM_PUT_TYPENAME_MEM);
/** \endcond */
```

所以在头文件里 grep `aclshmem_float_put` 只能找到别名 `#define`，找不到函数体——它的「声明」是宏展开的产物。理解这一点，Device 侧头文件的阅读障碍就消除了一大半。

#### 4.1.4 代码实践

**实践目标**：用 grep 在 `include/` 中定位 API 声明，体会「API 名 → 头文件」的定位方法。

**操作步骤**：

1. 在仓库根目录执行下面的命令（shell 命令，非示例代码）：

   ```bash
   grep -rn "aclshmem_malloc" include/ | head
   grep -rn "ACLSHMEM_HOST_API" include/host/mem/shmem_host_heap.h
   ```

2. 观察第一条命令的输出：唯一的函数声明落在 `include/host/mem/shmem_host_heap.h:28`，其余行是注释或 `#define` 别名。
3. 打开该文件浏览全部声明，记录哪些是 `aclshmem_` 标准接口（malloc/calloc/align/free）、哪些是 `aclshmemx_` 扩展接口（带 `mem_type` 参数的 `aclshmemx_malloc` 等，见 [include/host/mem/shmem_host_heap.h:71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L71)）。

**需要观察的现象**：grep 结果里每一行都带行号；声明行一定有 `ACLSHMEM_HOST_API` 前缀；声明下一行几乎总跟着一个 `#define shmem_xxx aclshmem_xxx` 别名。

**预期结果**：你能不借助文档，直接说出「`aclshmem_malloc` 声明于 `include/host/mem/shmem_host_heap.h` 第 28 行，是 Host 侧堆管理标准接口」。

（本实践为纯源码阅读型，无需 NPU 环境，命令已可本地复现。）

#### 4.1.5 小练习与答案

**练习 1**：`include/host_device/` 目录下有哪些文件？为什么它们不放在 `host/` 或 `device/` 下？

**参考答案**：`shmem_common_types.h` 与 `shmem_common_macros.h`。它们存放 Host 与 Device 两侧都要用的公共数据结构（如 `data_op_engine_type_t`）和公共宏，不含任何一侧独有的逻辑；放共用目录可以避免两侧互相包含对方头文件。

**练习 2**：`include/device/gm2gm/shmem_device_rma.h` 和 `include/device/ub2gm/shmem_device_rma.h` 同名，如何区分？

**参考答案**：靠目录名区分数据通路——`gm2gm/` 下的是远端 GM 到本地 GM 的搬运接口（源也是 `__gm__` 指针）；`ub2gm/` 下的是本地 Unified Buffer 直达远端 GM 的接口（源是 `__ub__`/UB 侧缓冲）。C++ 头文件按 include 路径寻址，两者路径前缀不同，互不冲突。

**练习 3**：为什么 `aclshmem_my_pe()` 这种看似「初始化信息查询」的接口声明在 `include/host/team/shmem_host_team.h:80` 而不在 `init/` 下？

**参考答案**：因为 `my_pe`/`n_pes` 本质是通信域（team）概念——PE 编号是相对于某个 team 的，默认 team 是 `ACLSHMEM_TEAM_WORLD`，查询全局 `my_pe` 等价于查询在 WORLD 里的位置。把 PE 查询和 team 管理放同一个头文件，语义上更内聚（可对照 [include/host/team/shmem_host_team.h:80-90](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/team/shmem_host_team.h#L80-L90)，`aclshmem_my_pe` 与 `aclshmem_n_pes` 相邻声明）。

### 4.2 模块二：src —— 实现源码的目录镜像

#### 4.2.1 概念说明

`src/` 存放实现代码，即「库内部怎么做的」。它与 `include/` 大体镜像（`src/device` 对应 `include/device`，`src/host` 对应 `include/host`），但多出几类只有实现才需要的东西：

- `src/host/mem/heap/`：HYBM（Hybrid Memory，混合内存）对称堆的实现。官方文档 [docs/code_organization.md:61-73](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/code_organization.md#L61-L73) 把它记作 `src/host/hybm`，**真实代码已迁移到 `src/host/mem/heap/`**（内有 `hybm_mem_segment.cpp`、`hybm_mem_slice.cpp`、`hybm_user_buffer_heap.h` 等）——这是「文档滞后于代码」的典型例子。
- `src/host/entity/`：内存实体（mem_entity）抽象与工厂，供 HYBM 按后端创建不同类型的内存对象。
- `src/host/bootstrap/`：初始化建链的控制面实现（config store、MPI、UID 三种模式）。
- `src/host/transport/`：建链与传输层，按引擎分子目录 `device_rdma/`、`device_sdma/`、`device_udma/`，外加 `topo/`（拓扑发现）和组合管理器。
- `src/host/utils/`、`src/host/data_plane/`、`src/host/sync/`、`src/host/python_wrapper/`、`src/host_device/`、`src/python/`。

另一个值得注意的不对称：Device 侧实现里，`src/device/gm2gm/` 的直驱实现在 `engine/` 子目录，而 `src/device/ub2gm/` 的直驱实现在 `mte/` 子目录（如 `src/device/ub2gm/mte/shmem_device_mte.hpp`）。README 的目录树（[README.md:144-163](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/README.md#L144-L163)）记录的就是这一真实形态。

#### 4.2.2 核心流程

从 API 到实现的通用定位流程（本讲最重要的方法论）：

```text
第 1 步：拿到 API 名（如 aclshmem_malloc）
第 2 步：grep -rn "API 名" include/          → 找到声明头文件（.h），读注释懂语义
第 3 步：grep -rn "返回类型 API 名(" src/     → 找到实现文件（.cpp/.hpp），读函数体懂机制
第 4 步：如遇 shmemi_/aclshmemi_ 前缀的内部函数，继续沿调用链向下追
```

以 `aclshmem_malloc` 为例的映射关系：

| 层 | 位置 |
| --- | --- |
| 总入口可见 | `include/shmem.h` → `host/mem/shmem_host_heap.h` |
| 声明 | `include/host/mem/shmem_host_heap.h:28` |
| 实现 | `src/host/mem/shmem_mm.cpp:37` |
| 底层堆管理 | `src/host/mem/shmem_mgr.cpp`、`src/host/mem/heap/`（HYBM） |

#### 4.2.3 源码精读

**`aclshmem_malloc` 的实现。** [src/host/mem/shmem_mm.cpp:37-62](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L37-L62) 展示了 Host 侧一个「简单 API」背后的完整动作：

```cpp
void* aclshmem_malloc(size_t size)
{
    if (aclshmemi_memory_manager == nullptr) {
        SHM_LOG_ERROR("Memory Heap Not Initialized.");
        return nullptr;
    }

    void* ptr = aclshmemi_memory_manager->allocate(size);
    SHM_LOG_DEBUG("aclshmem_malloc(" << size << ")" << " ptr: " << ptr);
    auto ret = aclshmemi_control_barrier_all();
    if (ret != 0) {
        SHM_LOG_ERROR("malloc mem barrier failed, ret: " << ret);
        if (ptr != nullptr) {
            aclshmemi_memory_manager->release(ptr);
            ptr = nullptr;
        }
    }
    ...
    return ptr;
}
```

逐行解读这段代码做了什么：

1. 先检查内部堆管理器 `aclshmemi_memory_manager` 是否就绪（未初始化直接返回空指针）——注意 `aclshmemi_` 前缀，它是模块级内部全局变量。
2. 调用 `allocate(size)` 在本地对称堆上划出内存。
3. **关键**：调用 `aclshmemi_control_barrier_all()` 做一次控制面全局 barrier。这正是 u1-l1 讲过的「各 PE 必须同序同大小分配」的保证机制——barrier 确保所有 PE 都执行完同一次 malloc，堆内偏移才保持一致；barrier 失败则回滚本次分配。
4. `DEBUG_MODE` 下还会调用 `is_alloc_size_symmetric(size)` 校验各 PE 分配大小是否对称（见同文件条件编译块）。

这个函数是「头文件 1 行声明 → 实现里横跨堆管理与控制面同步」的典型样本，也提前为 u2-l4（堆 API）和 u2-l3（bootstrap 控制面）埋下伏笔。

**实现侧的初始化函数。** 同文件 [src/host/mem/shmem_mm.cpp:15-27](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L15-L27) 的 `memory_manager_initialize` 在 SHMEM 初始化建堆阶段被调用，按 `mem_type` 分别构造 device 侧与 host 侧两个 `memory_manager`——由此可知「堆」同时可以有一份 DEVICE_SIDE 和一份 HOST_SIDE。

#### 4.2.4 代码实践

**实践目标**：完整走一遍「API 名 → 声明 → 实现 → 内部调用链」的定位流程。

**操作步骤**：

1. 执行 `grep -n "aclshmem_malloc" src/host/mem/shmem_mm.cpp`，确认实现入口在第 37 行。
2. 在实现中找到内部函数 `aclshmemi_control_barrier_all`，再执行 `grep -rn "aclshmemi_control_barrier_all" src/ | head` 找到它的定义文件。
3. 同样追一下 `aclshmemi_memory_manager` 的类型 `memory_manager` 定义在哪（提示：`src/host/mem/` 下的头文件）。
4. 把三步结果记成一条调用链：`aclshmem_malloc → memory_manager::allocate + aclshmemi_control_barrier_all → ...`。

**需要观察的现象**：每往下一层，函数名前缀多一个 `i`（internal），且实现文件逐渐从「面向用户的 API 文件」移向「模块内部文件」（`shmem_mm.cpp` → bootstrap 目录）。

**预期结果**：得到一条 3 到 4 层的调用链文字记录。整个过程不需要运行程序，属源码阅读型实践（运行时验证待本地完成，可在 u1-l4 跑通 init 示例后再回来加日志观察）。

#### 4.2.5 小练习与答案

**练习 1**：官方文档说 HYBM 实现在 `src/host/hybm`，你如何在 1 分钟内验证真实位置？

**参考答案**：`ls src/host/` 看不到 `hybm`，再 `find src -name "hybm*"` 即可命中 `src/host/mem/heap/` 下的 `hybm_mem_segment.cpp` 等文件。结论：HYBM 逻辑现位于 `src/host/mem/heap/`，文档滞后。

**练习 2**：`src/host/mem/` 下同时有 `shmem_mm.cpp`、`shmem_mgr.cpp`、`shmem_rma.cpp` 和 `heap/` 子目录，各自偏什么职责？

**参考答案**：`shmem_mm.cpp` 实现对外堆 API（malloc/calloc/align/free）并对齐控制面 barrier；`shmem_mgr.cpp`/`shmemi_mgr.h` 是堆管理器与堆信息管理；`shmem_rma.cpp` 支撑 Host 侧 RMA 的地址换算等；`heap/` 是 HYBM 对称堆的段（segment）、切片（slice）与用户 buffer 堆实现。命名规则：`shmemi_` 开头的头文件（如 `shmemi_mgr.h`）是模块内部头，不对外。

**练习 3**：为什么 `src/` 下还有 `src/python/` 与 `src/host/python_wrapper/` 两个 Python 相关目录？

**参考答案**：分工不同——`src/host/python_wrapper/` 是 C++ 侧的 pybind11 封装（把 C++ API 暴露成 `_pyshmem` 扩展模块），`src/python/` 是纯 Python 包源码（`shmem/` 包的 `__init__.py`、core 模块等）。一个生成扩展模块，一个是包本体，分别在编译期和安装期起作用。

### 4.3 模块三：examples —— 样例工程的组织

#### 4.3.1 概念说明

`examples/` 下每个子目录都是一个可独立编译运行的样例工程，通常包含 `main.cpp`（Host 侧拉起进程、初始化、下发 kernel）、kernel 源文件（AscendC 侧执行通信）、`run.sh`（多进程拉起脚本），部分还有独立 `README.md`。官方对每个样例的一句话说明见 [docs/code_organization.md:77-103](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/code_organization.md#L77-L103)，但该列表同样略滞后——真实 `examples/` 下现有 30 个左右子目录，比文档多出 `aclgraph_demo`、`rdma_sync_barrier_demo`、`shmem_mega_moe`、`shmem_perftest`、`simt_rma_ub2gm`、`udma_qp_demo` 等。

样例命名规则非常有信息量，可分为四类：

| 命名模式 | 含义 | 代表样例 |
| --- | --- | --- |
| 功能直述 | 表达该样例演示的能力 | `init`、`allgather`、`notifywait`、`multi_instance` |
| 引擎前缀 | 强调所用传输引擎 | `rdma_demo`、`udma_demo`、`sdma`、`udma_atomic_add` |
| 业务场景 | 对齐真实业务 | `combine`/`dispatch`（MoE）、`kv_shuffle`（KV Cache）、`shmem_mega_moe` |
| 生态集成 | 与框架/特性结合 | `python_extension`、`torch_binding`、`rdma_aclgraph_demo` |

#### 4.3.2 核心流程

一个典型样例工程从编译到运行的路径：

```text
scripts/build.sh（总构建，含 examples）
    → examples/CMakeLists.txt（注册各子目录）
    → examples/<name>/（生成可执行文件）
运行：examples/<name>/run.sh 或 scripts/run_examples.sh
    → 以指定 pesize 拉起 N 个进程（每个进程绑定一个 PE）
    → main.cpp: acl 初始化 → SHMEM 初始化 → 分配对称内存 → 下发 kernel → 校验 → finalize
```

#### 4.3.3 源码精读

**样例清单的组织方式。** [docs/code_organization.md:78-103](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/code_organization.md#L78-L103) 以注释树形式给每个样例标注定位，例如：

```text
├── init                                   // shmem初始化流程样例
├── notifywait                             // sdma notify/wait同步样例
├── combine                                // moe combine样例
├── python_extension                       // Python扩展与torch调用样例
```

选样例时优先按这张表（结合上面的命名四分类）定位，再进子目录看 `main.cpp` 与 kernel 文件的分工。`examples/utils/` 是样例公共工具（公共校验、公共拉起逻辑），不是可运行样例。

**样例与头文件的对应。** 读任何一个样例前，先看它 include 了哪些 `include/` 头文件，就能反推它演练的是哪一层接口。例如 `examples/rdma_demo` 的 kernel 会包含 `device/gm2gm/shmem_device_rma.h`（高阶 RMA），而需要直驱引擎的样例会包含 `device/gm2gm/engine/` 下的头文件。

#### 4.3.4 代码实践

**实践目标**：建立「样例 ↔ 目录 ↔ 接口头文件」的三方映射感。

**操作步骤**：

1. 执行 `ls examples/`，对照 4.3.1 的命名四分类，把每个子目录归入一类。
2. 任选一个样例（建议 `examples/init`，u1-l4 会精读），执行 `ls examples/init/` 查看文件构成。
3. 对 `examples/init/main.cpp` 执行 `grep -n "#include" examples/init/main.cpp`，记录它用了 `include/` 下哪些头文件。
4. 再挑一个引擎类样例（如 `examples/udma_demo`），重复第 3 步，对比两者 include 的差异。

**需要观察的现象**：功能类样例主要 include `host/init/`、`host/mem/` 下的头；引擎类/数据面类样例还会 include `device/gm2gm/...` 或 `device/gm2gm/engine/...` 的头。

**预期结果**：得到两份 include 清单的对比结论，例如「`init` 样例只用 Host 侧头文件，`udma_demo` 额外用了 Device 侧 engine 头文件」。编译运行样例需要 NPU 环境，本实践先做静态分析（运行为待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：想学习「kernel 内信号等待」应该看哪个样例？想学习 MoE 场景的通算融合呢？

**参考答案**：信号等待看 `examples/notifywait`（notify/wait 同步样例）；MoE 通算融合看 `examples/combine`、`examples/dispatch`，进阶看 `examples/shmem_mega_moe`（端到端综合样例，u8-l7 精读）。

**练习 2**：`examples/utils/` 为什么不算一个样例？

**参考答案**：它存放样例公共工具代码（供其他样例复用的辅助函数/脚本），没有自己的 `main` 与运行入口，不会被当作独立工程注册运行。

**练习 3**：docs 的样例树里没有 `shmem_perftest`，你怎么确认它存在且是性能测试？

**参考答案**：`ls examples/shmem_perftest` 直接确认存在；进入目录看 `README.md`/源码中按消息步长循环搬运并计时的逻辑即可确认用途（该样例将在 u8-l8 性能测试一讲精读）。

### 4.4 模块四：tests —— 测试体系组织

#### 4.4.1 概念说明

`tests/` 分三块：`examples/`（样例级功能测试）、`package_smoke/`（安装包冒烟测试）、`unittest/`（单元测试）。官方文档 [docs/code_organization.md:107-115](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/code_organization.md#L107-L115) 记录的是早期四模块结构（init/mem/sync/team），**真实代码已重组为 host/device 两棵子树**：

```text
tests/unittest/
├── host/          # Host 侧单测（有 main_test.cpp 汇总入口）
│   ├── bootstrap/ # bootstrap 控制面
│   ├── init/      # 初始化（含 user_buffer_heap_test.cpp）
│   ├── mem/       # 堆与内存
│   ├── sync/      # 同步原语
│   ├── team/      # 通信域
│   ├── topo/      # 拓扑（rootinfo/xml_parser）
│   ├── transport/ # 传输层（composite 等）
│   ├── multi_instance/、simt_rma/
│   └── CMakeLists.txt、main_test.cpp
├── device/        # Device 侧单测（mem/sync/team/multi_instance/simt_rma）
├── team/          # team 级用例（team、team_allgather）
└── include/       # 测试公共头（unittest/ 辅助宏）
```

测试目录与被测模块几乎一一对应：想找某个模块的用法示例或行为规格，去对应的测试目录往往比看文档更准——**测试断言就是接口的行为契约**。

#### 4.4.2 核心流程

测试的编译与运行入口：

```text
编译：scripts/build.sh 打开 UT 编译开关（详见 u8-l6）
    → tests/unittest/CMakeLists.txt
    → tests/unittest/host/CMakeLists.txt（googletest 目标）
运行：scripts/run.sh（执行入口，含 host/device 两侧用例）
```

日常阅读顺序建议：先看 `tests/unittest/host/main_test.cpp` 了解用例如何被组织，再进具体模块目录挑一个测试文件读断言。

#### 4.4.3 源码精读

**host 侧用例的物理布局。** 以初始化模块为例，`tests/unittest/host/init/` 下现有四个文件：`init_host_test.cpp`（基础初始化流程）、`bootstrap_test.cpp`（建链模式）、`init_finalize_loop_test.cpp`（反复初始化/销毁）、`user_buffer_heap_test.cpp`（用户 buffer 堆）。文件名直接对应被测能力，查找时按「模块目录 + 能力名」猜测文件名命中率很高。

**host/device 两棵树的对照。** `tests/unittest/host/mem` 测 Host 侧堆 API（如 `shmem_host_put_stream_test.cpp` 这类按 API 名命名的文件），`tests/unittest/device/mem` 测 kernel 侧数据面。对比两棵树的同名子目录（mem/sync/team），正好呼应 `include/` 的 host/device 三分法——**目录结构在三处（include、src、tests）保持了同一套切分维度**，这是本讲最值得带走的结论。

#### 4.4.4 代码实践

**实践目标**：用测试目录反查接口行为规格。

**操作步骤**：

1. 执行 `ls tests/unittest/host/ tests/unittest/device/`，记录两侧的模块子目录。
2. 执行 `ls tests/unittest/host/init/`，浏览 `init_host_test.cpp` 开头的 include 与 TEST 用例名（`grep -n "TEST" tests/unittest/host/init/init_host_test.cpp | head`）。
3. 找到一个与堆相关的测试文件名（提示：在 `tests/unittest/host/mem/` 下），从文件名反推它测的是哪个 API。

**需要观察的现象**：用例名（`TEST(TestSuiteName, CaseName)`）通常就是一句行为描述，例如围绕 init/finalize 成对出现的用例；测试文件的 include 列表能告诉你被测头文件。

**预期结果**：说出「host 侧 mem 测试至少覆盖了哪些 API」，并能打开其中一个文件指出它断言了什么。编译运行 UT 需要 CANN 环境与 UT 开关（u8-l6 详述），本实践先静态阅读（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：docs 说 tests/unittest 下是 init/mem/sync/team 四个目录，真实结构是什么？这对读代码的人意味着什么？

**参考答案**：真实结构是 `host/` 与 `device/` 两棵子树，其下再分 bootstrap/init/mem/sync/team/topo/transport 等模块目录，另有 `team/`、`include/` 辅助目录。意味着阅读时应以 `find tests -maxdepth 3 -type d` 的实际输出为准，文档仅作背景参考——这也是本讲反复强调的方法论。

**练习 2**：想了解「用户自带 buffer 构建对称堆」的正确用法，最快的途径是什么？

**参考答案**：直接读 `tests/unittest/host/init/user_buffer_heap_test.cpp`——测试代码展示的参数填充与调用顺序就是官方推荐用法，且断言写明了预期行为（该机制在 u8-l3 精讲）。

**练习 3**：`tests/examples/`、`tests/package_smoke/`、`tests/unittest/` 三者的测试粒度有何不同？

**参考答案**：`unittest` 是函数/模块级单元测试（googletest）；`examples` 是样例级功能测试（跑完整样例验证端到端行为）；`package_smoke` 是安装包级冒烟测试（验证打包产物可用性）。粒度从细到粗，分别服务日常开发、集成验证与发布检查。

## 5. 综合实践

**任务：整理一张「API → 文件」速查表。** 这是本讲的收官实践，要求把四个模块的知识串起来：从 API 名出发，在 `include/` 找声明、在 `src/` 找实现、在 `examples/` 找用法、在 `tests/` 找规格。

**操作步骤**：

1. 选取下表左列的 API（也可自行补充），逐一执行：

   ```bash
   grep -rn "API名" include/ | grep -v "^Binary"   # 找声明
   grep -rln "API名" src/                          # 找实现文件
   grep -rln "API名" examples/ tests/              # 找用法与测试
   ```

2. 把结果填入四列表格：API 名 / 声明位置（文件:行号）/ 实现位置 / 用法或测试位置。
3. 参考答案（已替你验证过的前三行，其余留给你完成）：

   | API | 声明 | 实现 | 用法/测试线索 |
   | --- | --- | --- | --- |
   | `aclshmem_malloc` | `include/host/mem/shmem_host_heap.h:28` | `src/host/mem/shmem_mm.cpp:37` | u1-l4 的 init 示例、`tests/unittest/host/mem/` |
   | `aclshmem_putmem`（Host 侧） | `include/host/data_plane/shmem_host_rma.h:543` | `src/host/data_plane/` | u3-l1 |
   | `aclshmem_putmem`（Device 侧） | `include/device/gm2gm/shmem_device_rma.h:334` | `src/device/gm2gm/` | `examples/rdma_demo` |
   | `aclshmemx_init_attr` | `include/host/init/shmem_host_init.h:146` | （待你查找） | `examples/init` |
   | `aclshmem_my_pe`（Host 侧） | `include/host/team/shmem_host_team.h:80` | （待你查找） | （待你查找） |
   | `aclshmem_float_put`（宏生成） | 宏展开，见 `include/device/gm2gm/shmem_device_rma.h:365` | （待你查找） | （待你查找） |

4. 注意 `aclshmem_putmem` 在 Host 侧与 Device 侧各有一个声明（参数类型不同：Host 侧 `void*`，Device 侧 `__gm__ void*`），这正是双侧接口体系的体现，表格中应分两行记录。

**预期结果**：一张至少 6 行、四列齐全的速查表，之后学习 u2~u8 任何一讲时都能直接查表定位源码。全程无需 NPU 环境；若要运行表中示例验证行为，待本地环境就绪后进行（待本地验证）。

## 6. 本讲小结

- 仓库顶层按职责分为 `include/`（对外 API）、`src/`（实现）、`examples/`（样例）、`tests/`（测试）、`docs/`（文档）、`scripts/`（构建运行脚本）、`tools/`（rootinfo 等工具）。
- `include/` 遵循「host / device / host_device 三分 + gm2gm、ub2gm、engine、data_plane、team 按通路与功能细分」的规则；`include/shmem.h` 是唯一总入口，Device 分支靠 `__CCE_AICORE__` 条件编译展开。
- `src/` 与 `include/` 大体镜像，但多出 `mem/heap/`（HYBM 堆）、`entity/`（内存实体工厂）、`bootstrap/`、`transport/` 等纯实现目录；`shmemi_` 前缀标识内部符号。
- `aclshmem_malloc` 的映射链：声明于 `include/host/mem/shmem_host_heap.h:28`，实现于 `src/host/mem/shmem_mm.cpp:37`，实现内含保证各 PE 堆偏移一致的控制面 barrier。
- examples 按「功能 / 引擎 / 业务场景 / 生态集成」四类命名；tests 已重组为 host/device 两棵子树，测试目录与被测模块一一对应，断言即行为规格。
- 官方文档（`docs/code_organization.md`、README）中的目录树存在滞后（如 HYBM 位置、tests 结构），定位代码时务必以 `find`/`grep` 的实际结果为准。

## 7. 下一步学习建议

下一讲 u1-l4《运行第一个示例：init 示例解析》将把本讲的目录地图变成动手经验：编译并运行 `examples/init`，逐行精读 `main.cpp` 中 aclInit → `aclshmemx_init_attr` → `shmem_finalize` 的完整生命周期。在那之前，建议你：

1. 用本讲综合实践的速查表，预先查好 `aclshmemx_init_attr`（[include/host/init/shmem_host_init.h:146](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/init/shmem_host_init.h#L146)）的声明注释。
2. 浏览 `examples/init/` 目录（`main.cpp` 与 `run.sh`），尝试用 4.3 节的样例结构知识预判每个文件的职责。
3. 若想提前接触实现层，可翻看 `src/host/init/shmem_init.cpp` 的函数列表，为 u2 单元的初始化主链路走读做铺垫。
