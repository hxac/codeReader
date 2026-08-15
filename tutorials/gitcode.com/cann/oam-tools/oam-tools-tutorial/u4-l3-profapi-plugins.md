# profapi 插件体系：多数据源采集插件

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `profapi` 目录下各插件头文件的职责划分：谁是抽象基类、谁是管理器、谁对应哪个数据源。
2. 理解 `ProfPluginManager` 的插件加载机制：惰性默认插件 + 可替换指针。
3. 区分 acl / cann / runtime / tx / atls / avp / device 各插件接入的数据来源与接入方式（虚函数继承 vs `dlsym` 符号加载）。
4. 掌握「新增一个数据源插件」需要实现的接口形态，能仿照现有插件写出接口头文件骨架。

## 2. 前置知识

本讲是 msprof 单元的第三讲，进入 C++ 侧最「纯接口」的一层。需要先理解几个基础概念：

- **dlopen / dlsym**：Linux 动态库加载接口。`dlopen("libxxx.so", flags)` 在运行时打开一个动态库并返回句柄，`dlsym(handle, "函数名")` 按名字查出函数地址，转成函数指针后即可像普通函数一样调用。这叫「运行期符号加载」，好处是编译期不依赖对方头文件与库文件，库不存在时程序仍能启动（只是该功能退化）。
- **纯虚基类（抽象类）**：C++ 中只声明 `virtual ... = 0` 的类定义了「接口契约」，派生类必须实现全部纯虚函数才能实例化。调用方持有基类指针，即可无视具体实现类型。
- **pthread_once**：POSIX 线程工具，保证某个初始化函数在整个进程生命周期内只被执行一次，且线程安全。本讲会看到它被大量用于「惰性且只加载一次」的符号解析。
- **单例（Singleton）**：一个类全局只有一个实例。本仓用 `analysis::dvvp::common::singleton::Singleton<T>` 模板实现（u4-l1 讲过三个 so 分层时提过）。
- **函数指针类型别名**：如 `using ProfInitFunc = int32_t (*)(uint32_t, void*, uint32_t);` 描述「从动态库里查出来的函数长什么样」，是 dlsym 风格插件的标配。

一个必须先交代的事实：**当前仓库中 `profapi` 目录只有 `inc/` 头文件和 `CMakeLists.txt`，没有 `src/` 实现文件**。提交 `64ebbaa`（"删除不参与编译的文件"）删除了 `profapi/src/` 下全部 `.cpp`。因此本讲的「源码精读」以**头文件契约**为主，涉及实现细节时会标注，并教你用 `git show 64ebbaa^:<路径>` 从 git 历史里读到被删除的实现——这本身就是一次很好的源码考古练习。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/msprof/collector/dvvp/profapi/inc/prof_plugin.h` | 插件抽象基类 `ProfPlugin`，定义全部纯虚接口与模块回调注册表 |
| `src/msprof/collector/dvvp/profapi/inc/prof_plugin_manager.h` | 插件管理器 `ProfPluginManager`，持有当前生效的插件指针 |
| `src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h` | 宿主侧主插件 `ProfCannPlugin`，dlopen `libprofimpl.so` 的那一层 |
| `src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h` | ACL 数据源插件 `ProfAclPlugin`（算子耗时、订阅、transport 注册） |
| `src/msprof/collector/dvvp/profapi/inc/prof_runtime_plugin.h` | Runtime 数据源插件 `ProfRuntimePlugin`（打点接口 `rtProfilerTraceEx`） |
| `src/msprof/collector/dvvp/profapi/inc/prof_tx_plugin.h` | 用户打点（TX，Range/Mark/Stamp）插件 `ProfTxPlugin` |
| `src/msprof/collector/dvvp/profapi/inc/prof_atls_plugin.h` | Atlas 设备形态插件 `ProfAtlsPlugin`（实现了 `ProfPlugin` 接口） |
| `src/msprof/collector/dvvp/profapi/inc/prof_avp_plugin.h` | 轻量形态插件 `ProfAvpPlugin`（对应 `libprofapi_lite.so`） |
| `src/msprof/collector/dvvp/profapi/inc/prof_device_api.h` | 设备侧上报接口 `ProfDevApi` |
| `src/msprof/collector/dvvp/profapi/inc/prof_load_api.h` | dlsym 符号加载小工具 `ProfLoadApi` |
| `src/msprof/collector/dvvp/profapi/inc/prof_utils.h` | 跨平台 `PthreadOnce` 封装 |
| `src/msprof/collector/dvvp/profapi/inc/prof_inner_api.h` | 对外导出的 C 接口层（`extern "C"`），插件能力的门面 |
| `src/msprof/collector/dvvp/profapi/CMakeLists.txt` | 定义 `libprofapi.so`（完整版）与 `libprofapi_lite.so`（轻量版）两个构建目标 |

架构定位回顾（承接 u4-l1）：`profapi.so` 是 profiling 数据采集的**接口层**，它通过 dlopen 加载 `profimpl.so` 实现层；对动态库大小敏感的生产态可以不部署 `profimpl.so`，只留 `profapi.so` 保证上层业务组件的 so 正常加载。

## 4. 核心概念与源码讲解

### 4.1 profapi 插件家族全景

#### 4.1.1 概念说明

「插件」这个词在本目录有两条不同的技术路线，初学者最容易混淆：

1. **继承路线**：以 `ProfPlugin` 纯虚基类为契约，派生类（`ProfCannPlugin`、`ProfAtlsPlugin`）实现全部接口，调用方通过基类指针多态调用。这条路线解决的是「**同一套接口、不同设备形态的实现可替换**」。
2. **符号加载路线**：类不继承任何基类，成员是一组 `nullptr` 初始化的函数指针，首次调用时用 `dlsym` 从某个动态库里查出真实函数地址再转发。`ProfAclPlugin`、`ProfRuntimePlugin`、`ProfTxPlugin`、`ProfAvpPlugin` 都属此类。这条路线解决的是「**本编译单元不链接对方库，运行期按需取符号**」。

两条路线合起来，才把 runtime、GE、HCCL、ACL、用户打点等多个数据源接进同一条采集流水线。

#### 4.1.2 核心流程

以一次典型的 profiling 数据上报为例（概念流程，细节在各模块展开）：

```text
业务组件(runtime/GE/HCCL/用户代码)
    │ 调用 profapi 导出的 C 接口 (prof_inner_api.h)
    ▼
ProfPluginManager::GetProfPlugin()   ──取当前生效插件（默认 ProfCannPlugin）
    │  虚函数分发
    ▼
ProfCannPlugin::ProfReport*()        ──写入无锁上报缓冲 ReportBuffer
    │  另一侧由注册进去的 pop 回调取走
    ▼
libprofimpl.so (dlopen 加载的实现层) ──格式化、落盘，交给 analyze 模块(下一讲)
```

#### 4.1.3 源码精读

先看构建脚本，确认这套插件编译成什么。`profApiCpp` 列出了完整版 `libprofapi.so` 的全部源文件（注意：这些 `src/*.cpp` 已不在当前仓库，见第 2 节说明）：

- [src/msprof/collector/dvvp/profapi/CMakeLists.txt:L41-L52](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/CMakeLists.txt#L41-L52) —— 完整版插件的源文件清单：`prof_plugin_manager.cpp`、`prof_cann_plugin.cpp`、`prof_atls_plugin.cpp`、`prof_plugin.cpp`、`prof_tx_plugin.cpp`、`prof_acl_plugin.cpp`、`prof_runtime_plugin.cpp` 等。
- [src/msprof/collector/dvvp/profapi/CMakeLists.txt:L114-L124](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/CMakeLists.txt#L114-L124) —— `add_library(profapi_share SHARED ...)` 编出 `libprofapi.so`，即架构文档说的接口层。
- [src/msprof/collector/dvvp/profapi/CMakeLists.txt:L167-L183](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/CMakeLists.txt#L167-L183) —— 轻量版 `profapi_lite_share`，只编 avp 三个文件，输出名同为 `profapi` 但落在 `proflib/` 子目录——这就是「裁剪 profimpl 的生产态」对应的 lite 形态。

目录内 12 个头文件的角色分档：

| 头文件 | 角色 | 数据来源 / 职责 |
| --- | --- | --- |
| `prof_plugin.h` | 契约 | 定义插件必须实现的约 20 个纯虚方法 |
| `prof_plugin_manager.h` | 管理 | 持有并分发当前生效的 `ProfPlugin*` |
| `prof_cann_plugin.h` | 主插件（继承） | CANN 软件栈宿主侧数据；dlopen `libprofimpl.so` |
| `prof_atls_plugin.h` | 备选插件（继承） | Atlas 设备形态，经注册的回调句柄上报 |
| `prof_avp_plugin.h` | 轻量插件（符号加载） | lite 形态，仅保留最小上报集 |
| `prof_acl_plugin.h` | 数据源插件 | ACL 层：算子耗时查询、模型订阅、transport 注册 |
| `prof_runtime_plugin.h` | 数据源插件 | Runtime 层：`rtProfilerTraceEx` 打点 |
| `prof_tx_plugin.h` | 数据源插件 | 用户打点：Range/Mark/Stamp 系列 |
| `prof_device_api.h` | 设备接口 | 设备侧批量附加信息上报 |
| `prof_load_api.h` / `prof_utils.h` | 工具 | dlsym 封装、pthread_once 封装 |
| `prof_inner_api.h` | 门面 | `extern "C"` 导出给业务组件的稳定 ABI |

#### 4.1.4 代码实践

**实践目标**：亲手整理出上面那张「插件清单表」，并验证 CMake 与之一一对应。

**操作步骤**：

1. `ls src/msprof/collector/dvvp/profapi/inc/` 列出全部头文件。
2. 对每个头文件，用 `grep -n "^class" src/msprof/collector/dvvp/profapi/inc/*.h` 找到主类名，并判断它是否继承 `ProfPlugin`（继承路线）还是持有函数指针成员（符号加载路线）。
3. 对照 `CMakeLists.txt` 的 `profApiCpp` 与 `profapi_lite_share` 源文件清单，标注每个类进了哪个 so。

**需要观察的现象**：`profapi_lite_share` 只包含 `prof_avp_inner_api.cpp`、`prof_avp_plugin.cpp`、`prof_plugin.cpp` 三个文件，完整版则包含全部——两张 so 的「胖瘦」差异一目了然。

**预期结果**：得到一张三列（插件名 / 所属构建目标 / 接入路线）的对照表。本实践纯源码阅读，无需设备，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：`prof_plugin.h` 中 `ProfPlugin` 类定义被注释掉的 `// : public Singleton<ProfPlugin>` 说明什么设计意图？

**答案**：说明基类本身不强制单例——是否单例由派生类自行决定。事实上 `ProfCannPlugin`、`ProfAtlsPlugin` 都同时继承了 `ProfPlugin` 和 `Singleton<各自>`，而管理器 `ProfPluginManager` 只依赖 `ProfPlugin*` 指针，不关心实例化方式，降低了契约与生命周期策略的耦合。

**练习 2**：为什么 `libprofapi.so` 与 `libprofapi_lite.so` 的 `OUTPUT_NAME` 都叫 `profapi` 却不冲突？

**答案**：两者 `LIBRARY_OUTPUT_DIRECTORY` 不同——完整版落在构建目录本身，lite 版落在其 `proflib/` 子目录（[CMakeLists.txt:L167-L183](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/CMakeLists.txt#L167-L183)），部署时按场景二选一，同名恰好保证了上层链接关系不用改。

### 4.2 ProfPlugin 基类与 ProfPluginManager：插件加载机制

#### 4.2.1 概念说明

`ProfPlugin` 是整条继承路线的根契约：它把「一个 profiling 插件必须会做的事」写成约 20 个纯虚函数——生命周期（Init/Start/Stop/Finalize）、配置（SetConfig）、数据上报（ReportData/ReportApi/ReportEvent/...）、设备映射与迭代步进信息等。`ProfPluginManager` 则是最简化的管理器：一个单例，攥着一个 `ProfPlugin*` 指针，向全系统提供「当前该用哪个插件」的答案。

#### 4.2.2 核心流程

```text
调用方需要插件
    │
    ▼
ProfPluginManager::GetProfPlugin()
    ├── profPlugin_ 已被 SetProfPlugin() 设置过？ → 直接返回该指针
    └── 否则 → 惰性默认为 ProfCannPlugin::instance()（宿主 CANN 环境的缺省实现）
```

这套「默认 + 可替换」结构是一个极小但完整的策略模式：缺省策略是 CANN 插件，特殊设备形态（如 Atlas）在初始化时调 `SetProfPlugin` 换成自己的实现，其余代码对此无感知。

回调注册则是基类的另一职责：

```text
业务模块( moduleId = runtime/GE/HCCL... )
    │ ProfRegisterCallback(moduleId, handle)
    ▼
moduleCallbacks_ : map< moduleId, set<ProfCommandHandle> >   (+ callbackMutex_ 保护)
    │ Prof 命令下发时遍历该 moduleId 的 handle 集合
    ▼
各模块收到 Start/Stop 等命令，开始/停止上报数据
```

#### 4.2.3 源码精读

- [src/msprof/collector/dvvp/profapi/inc/prof_plugin.h:L40-L48](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_plugin.h#L40-L48) —— `ProfPlugin` 类声明与生命周期/分发核心接口：`ProfInit`、`ProfStart`、`ProfStop`、`ProfSetConfig`、`ProfRegisterCallback`、`ProfReportData` 全部为纯虚。
- [src/msprof/collector/dvvp/profapi/inc/prof_plugin.h:L49-L60](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_plugin.h#L49-L60) —— 数据上报接口族：`ProfReportApi`（API 调用耗时）、`ProfReportEvent`（事件）、`ProfReportCompactInfo`（紧凑信息）、`ProfReportAdditionalInfo`（附加信息）、`ProfReportGetHashId`（字符串信息换 64 位哈希 id，避免重复落盘长字符串）。注意它们的参数形态是 C 风格指针 + 长度，方便跨 so 边界。
- [src/msprof/collector/dvvp/profapi/inc/prof_plugin.h:L62-L65](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_plugin.h#L62-L65) —— 静态成员 `moduleCallbacks_`（moduleId → 回调集合的映射）与保护它的 `callbackMutex_`，以及 `ReadProfCommandHandle()`（返回已注册模块数）。
- [src/msprof/collector/dvvp/profapi/inc/prof_plugin_manager.h:L22-L28](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_plugin_manager.h#L22-L28) —— 管理器全部内容：一个 `PROF_PLUGIN_PTR` 别名、`GetProfPlugin`/`SetProfPlugin` 两个方法和裸指针 `profPlugin_`，外加 `Singleton<ProfPluginManager>` 继承。管理器小到只有 6 行有效代码——这是「接口归接口、策略归策略」的刻意克制。

加载机制的实现细节在当前 HEAD 已删除，但 git 历史可考（以下为**历史版本代码**，可用 `git show 64ebbaa^:src/msprof/collector/dvvp/profapi/src/prof_plugin_manager.cpp` 自行验证）：

```cpp
// 历史版本代码（当前仓库已删除该文件）
PROF_PLUGIN_PTR ProfPluginManager::GetProfPlugin(void)
{
    if (profPlugin_ == nullptr) {
        profPlugin_ = ProfAPI::ProfCannPlugin::instance();   // 缺省策略：CANN 插件
    }
    return profPlugin_;
}
```

这正印证了上面「默认 + 可替换」的流程图。

#### 4.2.4 代码实践

**实践目标**：从 git 历史恢复插件加载实现，验证头文件契约与实现一致。

**操作步骤**：

1. `git log --oneline -- src/msprof/collector/dvvp/profapi/` 找到删除实现的提交 `64ebbaa`。
2. `git show 64ebbaa^:src/msprof/collector/dvvp/profapi/src/prof_plugin_manager.cpp` 阅读完整实现。
3. `git show 64ebbaa^:src/msprof/collector/dvvp/profapi/src/prof_plugin.cpp` 观察 `ProfPlugin` 静态成员 `moduleCallbacks_` 的定义与 `ReadProfCommandHandle()` 的实现。
4. 对照 `prof_plugin.h` 的纯虚函数列表，数一数 `ProfCannPlugin`（prof_cann_plugin.h L67-L108）override 了几个、又新增了几个（如 `ProfReportRegDataFormat`、`ProfReportBatchAdditionalInfo`）。

**需要观察的现象**：`prof_plugin.cpp` 历史实现只有约 10 行——两个静态成员的定义和一个返回 map 大小的方法，其余复杂度全在派生类。

**预期结果**：确认「基类定契约、静态成员存回调、派生类做实事、管理器只管指针」的分工。纯 git 只读操作，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：`GetProfPlugin` 的惰性默认为什么选 `ProfCannPlugin` 而不是 `ProfAtlsPlugin`？

**答案**：profapi 主要部署在 CANN 宿主软件栈（runtime/GE/HCCL 所在进程）里，CANN 插件是覆盖面最全的通用实现；Atlas 形态是特殊场景，由初始化代码显式 `SetProfPlugin` 替换。缺省值应取「最常见环境的实现」，这样多数调用路径零配置即可工作。

**练习 2**：`moduleCallbacks_` 为什么是 `map<uint32_t, set<ProfCommandHandle>>` 而不是单个回调？

**答案**：键是 moduleId，同一模块可能注册多个命令处理函数，不同模块（runtime、GE、HCCL…）各自注册互不干扰；`set` 天然去重，配合 `callbackMutex_` 保证多线程注册/遍历安全。这让一条 Prof 命令可以广播给所有相关模块。

### 4.3 ProfCannPlugin：宿主侧主插件

#### 4.3.1 概念说明

`ProfCannPlugin` 是继承路线的主实现，也是 `libprofapi.so` 的心脏。它同时做三件事：

1. **实现 `ProfPlugin` 全部接口**，并扩展了批量上报、变长块缓冲等设备侧（devprof）能力。
2. **dlopen `libprofimpl.so`** 并把实现层的函数逐个 `dlsym` 出来——这就是 u4-l1 讲的「接口层 dlopen 实现层」分层落点。
3. **维护无锁上报缓冲**（`ReportBuffer`/`BlockBuffer`/`VariableBlockBuffer` 三种模板队列），把「业务线程写数据」与「落盘线程取数据」解耦。

#### 4.3.2 核心流程

```text
首次使用 ProfCannPlugin
    │ ProfApiInit()
    ▼
dlopen("libprofimpl.so")                    ← 接口层→实现层的唯一接缝
    │ dlsym 逐个取 msProfInit/msProfStart/... 
    ▼
业务线程: ProfReportApi(...) ──写──▶ apiBuffer_ (ReportBuffer<MsprofApi>)
                                          │ 注册进实现层的 pop 回调按批取走
批量/变长场景: ProfReportBatchAdditionalInfo / ProfVarBlockBufBatchPop
    ▼
实现层 libprofimpl.so 格式化 → 落盘 → analyze 模块（下一讲）
```

缓冲的「生产—消费」通过回调咬合：`ProfCannPlugin` 把 `TryPopApiBuf`、`TryPushAdditionalBuf`、`TryMarkEx` 等自由函数注册给实现层（见头文件底部声明），实现层在自己的落盘线程里反查这些函数取数据——两个 so 之间只有函数指针，没有编译期依赖。

#### 4.3.3 源码精读

- [src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h:L63-L72](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h#L63-L72) —— `class ProfCannPlugin : public ProfPlugin, public Singleton<ProfCannPlugin>`，双重继承 + `ProfApiInit()` + override 全套生命周期接口。
- [src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h:L87-L93](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h#L87-L93) —— 上报缓冲的初始化/判空/出队接口：`ProfInitReportBuf`、`ProfIfReportBufEmpty`，以及针对 `MsprofApi`、`MsprofCompactInfo`、`MsprofAdditionalInfo` 三种数据的 `ProfReportBufPop` 重载。
- [src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h:L138-L140](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h#L138-L140) —— 三个无锁上报缓冲成员：`apiBuffer_`、`compactBuffer_`、`additionalBuffer_`，模板参数即元素类型。
- [src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h:L100-L108](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h#L100-L108) —— devprof（设备侧）扩展段：批量附加信息上报、批量缓冲出队/游标前移、原始数据订阅（`ProfSubscribeRawData`），这些是对基类接口的超越——只有完整版插件具备。
- [src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h:L162-L172](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h#L162-L172) —— 提供给实现层的自由函数：`TryPopApiBuf`、`TryPopCompactBuf`、`TryPopAdditionalBuf`、`IsReportBufEmpty`、`TryPushAdditionalBuf`、`TryMarkEx` 等，命名统一 `Try*` 前缀暗示「非阻塞、失败即返回」的语义。

dlopen 的目标库在头文件里看不到，历史实现可考（`git show 64ebbaa^:src/msprof/collector/dvvp/profapi/src/prof_cann_plugin.cpp`）：

```cpp
// 历史版本代码（当前仓库已删除该文件）
const std::string MSPROFILER_LIB_PATH = "libprofimpl.so";
msProfLibHandle_ = dlopen(MSPROFILER_LIB_PATH.c_str(), RTLD_LAZY | RTLD_NODELETE);
```

#### 4.3.4 代码实践

**实践目标**：搞清「三个缓冲各装什么、谁生产谁消费」。

**操作步骤**：

1. 在 [prof_cann_plugin.h:L138-L157](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_cann_plugin.h#L138-L157) 中找到 `apiBuffer_`、`compactBuffer_`、`additionalBuffer_`、`batchAdditionalBuffer_`、`variableAdditionalBuffer_` 五个缓冲成员，抄下它们的模板类型。
2. 对每个缓冲，在类内找到与之配对的「push 入口方法」和「pop 出口方法」（例如 `additionalBuffer_` ↔ `ProfReportAdditionalInfo` ↔ `ProfReportBufPop(uint32_t&, MsprofAdditionalInfo&)`）。
3. 用 `grep -rn "ReportBuffer\|BlockBuffer\|VariableBlockBuffer" src/msprof/collector/dvvp/common/` 定位缓冲模板的实现头文件，浏览其 `Push`/`Pop` 是否无锁。

**需要观察的现象**：五种缓冲按「定长/块/变长块」三档分化，分别服务 API 耗时、紧凑信息、附加信息、批量附加信息、shape 变长信息五类数据。

**预期结果**：画出一张「数据类型 → 缓冲 → push 方法 → pop 方法」对照表。纯源码阅读，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ProfCannPlugin` 的上报走缓冲队列，而不是把数据直接递给 `libprofimpl.so`？

**答案**：上报发生在业务线程（runtime/GE 的调用栈里），直接递数据会让业务线程承担格式化与 IO 的延迟；缓冲把「写」压缩到近乎无锁的一次入队，格式化与落盘由实现层自己的线程异步消费，兼顾了业务性能与数据完整性。

**练习 2**：`RTLD_LAZY | RTLD_NODELETE` 两个 dlopen 标志各有什么含义？

**答案**：`RTLD_LAZY` 延迟符号绑定——用到某个函数时才解析，加快启动且允许库内存在暂时不用的未定义符号；`RTLD_NODELETE` 保证后续 `dlclose` 时库不被真正卸载，避免插件函数指针悬垂（profiling 可能反复 Start/Stop，若库被卸载再重载，先前 `dlsym` 出的地址会失效）。

### 4.4 ProfAclPlugin 与 ProfRuntimePlugin：符号加载双范例

#### 4.4.1 概念说明

这两个插件是「符号加载路线」的两个典型样本，都不继承 `ProfPlugin`：

- **`ProfAclPlugin`**：面向 ACL（Ascend Computing Language）层的数据源。它的特点是「一个类、一批函数指针、一批 `PTHREAD_ONCE_T` 一次性加载标志」——每个对外方法首次被调用时才 `dlsym` 对应符号，此后直接转发。它还承载算子级查询能力：取算子耗时、取算子属性、取兼容特性集。
- **`ProfRuntimePlugin`**：更小的样本，只做一件事——从 `libruntime.so` 里找出打点函数 `rtProfilerTraceEx`，供 `ProfMarkEx`（迭代/步进打点）转发调用。它展示了「一个插件可以薄到只代理一个符号」。

#### 4.4.2 核心流程

`ProfAclPlugin` 的惰性加载模式（每个方法同构）：

```text
外部调用 ProfAclStart(type, config)
    │ PthreadOnce(&profAclStartFlag_, LoadProfAclStart)   ← 首次触发
    │     └── dlsym(msProfLibHandle_, "msProfStart") → profAclStart_
    ▼
此后每次调用: return profAclStart_(type, config)          ← 纯转发
```

`ProfRuntimePlugin` 的流程：

```text
RuntimeApiInit()
    ├── dlopen("libruntime.so", RTLD_NOW|RTLD_GLOBAL|RTLD_NODELETE) → runtimeLibHandle_
    └── PthreadOnce: LoadRuntimeApi() 对 g_runtimeApiSet 中每个名字 dlsym
                     → runtimeApiInfoMap_ (map<string, {funcName, funcAddr}>)

ProfMarkEx(indexId, modelId, tagId, stm)
    └── GetPluginApiFunc("rtProfilerTraceEx") 查 map 取地址 → 转发调用
```

注意差异：Acl 插件「一个方法一个 once 标志 + 一个成员指针」，Runtime 插件「集中把一组符号装进 map 再按名查」——两种组织方式各有取舍，前者省内存查找，后者便于统一管理与日志。

#### 4.4.3 源码精读

- [src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h:L28-L47](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h#L28-L47) —— ACL 插件的函数指针类型清单：初始化/启停（`ProfAclInitFunc` 等）、模型订阅与退订（`ProfAclSubscribeFunc`）、算子级查询（`ProfAclGetOpTimeFunc`、`ProfAclGetOpValFunc`、`ProfGetOpAttriValFunc`），以及 transport 工厂注册（`ProfRegisterTransportFunc`）。
- [src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h:L50-L68](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h#L50-L68) —— 对外方法区。`ProfAclApiInit(VOID_PTR handle)` 接收外部传入的动态库句柄（而非自己 dlopen），说明 ACL 插件搭的是别人（如 acl 进程内已加载的库）的便车；`ProfAclGetOpTime` 一族是算子耗时分析的数据出口。
- [src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h:L72-L87](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h#L72-L87) —— 17 个 `PTHREAD_ONCE_T` 成员，与 L89-L106 的函数指针成员一一对应：每个对外能力配一个「只加载一次」的标志，这是惰性加载模式的指纹。
- [src/msprof/collector/dvvp/profapi/inc/prof_utils.h:L21-L36](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_utils.h#L21-L36) —— `PthreadOnce` 跨平台封装：Linux 下直接 `pthread_once`，Windows 下用 bool 模拟。配合上面的一群 once 标志阅读。
- [src/msprof/collector/dvvp/profapi/inc/prof_runtime_plugin.h:L28-L33](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_runtime_plugin.h#L28-L33) —— Runtime 插件的全部「数据模型」：一个打点函数指针类型 `RtProfilerTraceExFunc` 和一个 `RuntimeApiInfo{funcName, funcAddr}` 结构——小到极致。
- [src/msprof/collector/dvvp/profapi/inc/prof_runtime_plugin.h:L35-L49](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_runtime_plugin.h#L35-L49) —— 类本体：`RuntimeApiInit`（dlopen+集中装载）、`GetPluginApiFunc`（按名查地址）、`ProfMarkEx`（打点转发），私有成员是库句柄、once 标志和 `runtimeApiInfoMap_`。

打点函数最终从哪里来，历史实现可考（`git show 64ebbaa^:src/msprof/collector/dvvp/profapi/src/prof_runtime_plugin.cpp`）：

```cpp
// 历史版本代码（当前仓库已删除该文件）
const std::string RUNTIME_LIB_PATH = "libruntime.so";
static std::set<std::string> g_runtimeApiSet = { "rtProfilerTraceEx" };
runtimeLibHandle_ = dlopen(RUNTIME_LIB_PATH.c_str(), RTLD_NOW | RTLD_GLOBAL | RTLD_NODELETE);
```

即 Runtime 插件代理的是 CANN runtime 库的打点入口，这正是训练脚本里 step 级打点能在 timeline 上显示的底层通道。

#### 4.4.4 代码实践

**实践目标**：验证「惰性加载」模式在两个插件里的一致性与差异。

**操作步骤**：

1. 数一数 [prof_acl_plugin.h:L72-L87](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h#L72-L87) 的 once 标志数量，再数 [L89-L106](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_acl_plugin.h#L89-L106) 的函数指针数量，确认一一对应（transport 两个指针由 `LoadProfCreateTransport` 统一处理，不必严格配对）。
2. `git show 64ebbaa^:src/msprof/collector/dvvp/profapi/src/prof_tx_plugin.cpp | head -60`，观察 `ProftxCreateStamp` 如何在指针为空时 `LoadProfTxApi<decltype(proftxCreateStamp_)>("ProfAclCreateStamp")`。
3. 对比 Runtime 插件的 map 集中装载，写下你认为各适合什么场景（一段话即可）。

**需要观察的现象**：两种风格都遵循「首次调用触发解析 → 成员指针缓存 → 后续纯转发」，解析失败的返回值处理（返回 `PROFILING_FAILED` 或 `nullptr`）也一致。

**预期结果**：能口头复述惰性加载三步曲，并举出「按方法缓存」与「按 map 集中缓存」各一个真实用例。步骤 2 依赖 git 历史只读命令，可直接完成。

#### 4.4.5 小练习与答案

**练习 1**：`ProfAclPlugin` 为什么不自己 dlopen 库，而由 `ProfAclApiInit(VOID_PTR handle)` 外部传入句柄？

**答案**：ACL 插件运行的进程里，目标库往往已经被宿主（如 libascendcl）加载。传入现成句柄可以复用已加载实例、避免同库被 dlopen 两次带来的符号与状态分裂，也让 profapi 不必硬编码「去哪找这个库」的路径策略。

**练习 2**：`ProfRuntimePlugin::GetPluginApiFunc` 查不到时返回 `nullptr`，调用它的 `ProfMarkEx` 会怎样？

**答案**：从历史实现看，`ProfMarkEx` 先经 `GetPluginApiFunc("rtProfilerTraceEx")` 取函数，取不到则不调用并返回失败——打点被静默丢弃而不是崩溃。这是 u2-l3 讲过的 asys「失败退化」哲学在 msprof C++ 侧的对应物：可选用能力缺失时优雅降级。

### 4.5 ProfTxPlugin 与其余成员：打点、设备与门面

#### 4.5.1 概念说明

- **`ProfTxPlugin`（用户打点）**：TX 即用户代码里主动标注的时间区段（类似 NVTX）。提供 `Stamp`（打点句柄）的创建/销毁、`Push/Pop`（线程压栈）、`RangeStart/RangeStop`（带 id 区段）、`Mark/MarkEx`（瞬时标记）、`SetStampTraceMessage`（打点附文本）等。注意它没走 Singleton 模板，而是 Meyers 单例 `GetProftxInstance()`。
- **`ProfAtlsPlugin`（Atlas 形态插件）**：继承路线的另一实现，接口与 `ProfCannPlugin` 同构，但数据经 `ProfRegisterReporter/ProfRegisterCtrl/ProfRegisterDeviceNotify` 注册进来的三个句柄上报，适配 Atlas 设备形态的采集通道。
- **`ProfAvpPlugin`（轻量插件）**：只保留最小接口集（Init/Finalize/Notify/ReportApi/ReportEvent/...），是 `libprofapi_lite.so` 的主体。
- **`ProfDevApi`（设备接口）**：设备侧上报附加信息、批量上报、字符串换 id（`ProfStr2Id`）。
- **`prof_inner_api.h`（门面）**：把上述能力以 `extern "C"` + `MSVP_PROF_API`（默认可见性导出）的形式暴露成稳定 C ABI，业务组件只需声明函数原型即可调用，不受 C++ 名字修饰影响。

#### 4.5.2 核心流程

用户打点从训练脚本到落盘的通道（结合 u4-l5 将讲到的 `PROFILING_OPTIONS`）：

```text
用户脚本调用打点 API (如 msprof_tx RangeStart)
    ▼
prof_inner_api.h 导出的 C 接口
    ▼
ProfTxPlugin::ProftxRangeStart(stamp, &rangeId)
    ├── 首次: dlsym "ProfAclRangeStart" 得真实地址
    └── 转发调用 → 数据进入采集链路 → timeline 上呈现一个区段
```

#### 4.5.3 源码精读

- [src/msprof/collector/dvvp/profapi/inc/prof_tx_plugin.h:L24-L35](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_tx_plugin.h#L24-L35) —— 打点函数指针族：`ProftxCreateStampFunc` 到 `ProftxSetStampPayloadFunc` 共 12 个，覆盖打点对象的全生命周期。
- [src/msprof/collector/dvvp/profapi/inc/prof_tx_plugin.h:L39-L58](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_tx_plugin.h#L39-L58) —— 类本体。注意 L41-L45 的 Meyers 单例（函数内 `static ProfTxPlugin plugin;`），与全仓其他插件的 `Singleton<T>` 模板不同——函数局部 static 天然线程安全（C++11 起）且初始化时机最晚。
- [src/msprof/collector/dvvp/profapi/inc/prof_load_api.h:L42-L49](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_load_api.h#L42-L49) —— `ProfLoadApi::LoadApi`：完整句柄为空时的行为是返回 `nullptr` 而不是崩溃，配合模板方法 `LoadProfTxApi<T>` 做类型转换。TX 插件的全部符号解析都经它。
- [src/msprof/collector/dvvp/profapi/inc/prof_atls_plugin.h:L46-L51](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_atls_plugin.h#L46-L51) —— Atlas 插件独有的一组注册接口：`ProfRegisterReporter`（上报句柄）、`ProfRegisterCtrl`（控制句柄）、`ProfRegisterDeviceNotify`（设备通知句柄）——数据通道由外部注册注入，而非自己 dlopen。
- [src/msprof/collector/dvvp/profapi/inc/prof_avp_plugin.h:L34-L47](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_avp_plugin.h#L34-L47) —— 轻量插件的方法集：只保留 Init/Finalize/Notify/RegisterCallback 与几个 Report，方法多为 `const`（无状态转发），与 lite so 的瘦身定位一致。
- [src/msprof/collector/dvvp/profapi/inc/prof_device_api.h:L24-L39](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_device_api.h#L24-L39) —— `ProfDevApi`：设备侧的 Init/RegisterCallback/Finalize、`ProfStr2Id`（字符串换 id，与宿主侧 `ProfReportGetHashId` 呼应）、批量附加信息上报。
- [src/msprof/collector/dvvp/profapi/inc/prof_inner_api.h:L28-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_inner_api.h#L28-L43) —— 门面层样本：`extern "C"` 块 + `MSVP_PROF_API __attribute__((visibility("default")))`，`ProfAclInit/ProfAclStart/ProfAclStop...` 一组 C 函数把插件能力以稳定 ABI 导出。这与类内 `-fvisibility=hidden` 编译选项（见 CMakeLists）配合：只有标了宏的符号对外可见。

#### 4.5.4 代码实践

**实践目标**：写出一个假想「自定义算子耗时插件」的接口头文件骨架（本讲综合实践的预热，此处先做最小版）。

**操作步骤**：

1. 复读 [prof_tx_plugin.h:L24-L58](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_tx_plugin.h#L24-L58)，抄下它的三段式结构：函数指针类型区 → 类声明（含单例）→ 私有函数指针成员区。
2. 仿照写 `prof_myop_plugin.h`（示例代码，非项目原有文件）：

```cpp
// 示例代码：仿照 prof_tx_plugin.h 的结构写的假想插件骨架
#ifndef PROF_MYOP_PLUGIN_H
#define PROF_MYOP_PLUGIN_H
#include <cstdint>
#include "prof_load_api.h"

namespace ProfAPI {
// 1) 函数指针类型区：描述要从目标库解析出的符号
using ProfMyopStartFunc  = int32_t (*)(uint32_t opType);
using ProfMyopStopFunc   = int32_t (*)(uint32_t opType);
using ProfMyopReportFunc = int32_t (*)(const char *opName, uint64_t durationNs);

// 2) 类声明：Meyers 单例 + 惰性转发方法
class ProfMyopPlugin {
public:
    static ProfMyopPlugin &GetInstance()
    {
        static ProfMyopPlugin plugin;
        return plugin;
    }
    void MyopApiInit(VOID_PTR handle) { loadApi_.ProfLoadApiInit(handle); }
    int32_t MyopStart(uint32_t opType);
    int32_t MyopStop(uint32_t opType);
    int32_t MyopReport(const char *opName, uint64_t durationNs);
private:
    ProfLoadApi loadApi_;                          // 复用现成的 dlsym 工具
    ProfMyopStartFunc  myopStart_{nullptr};        // 首次调用时才解析
    ProfMyopStopFunc   myopStop_{nullptr};
    ProfMyopReportFunc myopReport_{nullptr};
};
}
#endif
```

3. 把骨架与真实插件逐行对照：单例方式、`_` 后缀命名的空指针初始化、`LoadProfTxApi` 式解析（可在 git 历史的 `prof_tx_plugin.cpp` 里看到样板）是否都仿到了。

**需要观察的现象**：仿写过程中你会发现「新增一个数据源插件」的成本极低——一个头文件、一组函数指针类型、一组转发方法，不需要动基类也不需要动管理器。

**预期结果**：得到一个风格与 `prof_tx_plugin.h` 高度一致、可直接被 `.cpp` 实现接续的骨架头文件。无法编译验证（无对应实现库），属预期内。

#### 4.5.5 小练习与答案

**练习 1**：`ProfTxPlugin` 用 Meyers 单例而不用全仓通行的 `Singleton<T>` 模板，可能的原因是什么？

**答案**：Meyers 单例（函数局部 static）在首次调用时才构造，且 C++11 保证其初始化线程安全；TX 打点可能发生在进程很早期、其他基础设施未就绪时，最晚构造时机最安全。同时它不需要与 `Singleton` 模板的友元/继承机制耦合，独立成文件也便于按需裁剪。（此为基于代码形态的合理推断，确切动机待确认。）

**练习 2**：`prof_inner_api.h` 为什么用 `extern "C"` 而不是普通 C++ 函数？

**答案**：`extern "C"` 关闭 C++ 名字修饰，符号名就是函数名本身。这样业务组件（甚至不同编译器/语言编写的组件）只要声明同名原型、链接同一个 so 就能调用，不受编译器版本、命名空间或重载规则影响——这是跨 so 稳定 ABI 的标准做法，与 dlsym 按名取符号的机制天然契合。

## 5. 综合实践

**任务：产出《profapi 插件全景手册》并设计一个新插件。**

1. **清单表**：通读 `src/msprof/collector/dvvp/profapi/inc/` 下全部 12 个头文件，整理一张四列表格——插件/类名、接入路线（继承 ProfPlugin / dlsym 符号加载 / 门面 C 接口 / 工具类）、数据来源（libprofimpl.so、libruntime.so、外部注册句柄、宿主传入句柄…）、所属构建目标（profapi_share / profapi_lite_share / 两者）。4.1.3 节已给出三列版参考答案，第四列需要你读 CMakeLists 自行补全。
2. **调用链追踪**：从 `prof_inner_api.h` 里任选一个导出函数（如 `ProfAclGetOpTime`），在头文件与 git 历史（`git show 64ebbaa^:src/msprof/collector/dvvp/profapi/src/prof_acl_plugin.cpp`）中追踪它最终转发的符号名，写出「导出名 → 插件方法 → dlsym 符号名」三级链。
3. **新插件设计**：把 4.5.4 的骨架扩成完整的「自定义算子耗时插件」设计：补充 `PTHREAD_ONCE_T` 一次加载标志（对照 prof_acl_plugin.h 的 17 个标志）、补充解析函数声明区、并用一段伪代码说明 `MyopReport` 首次调用与后续调用的差别。
4. **回答一道开放题**：如果这个新插件的数据要进入 timeline 落盘，它应该把数据交给谁？（提示：回顾 4.3 的 `ReportBuffer` 与「注册给实现层的 TryPop 回调」机制，你的插件应复用 `ProfCannPlugin` 的上报通道而不是自建落盘。）

产出物：一张表格、一条调用链、一个头文件骨架、一段设计说明。

## 6. 本讲小结

- profapi 的「插件」有两条路线：**继承路线**（`ProfPlugin` 纯虚基类 + `ProfCannPlugin`/`ProfAtlsPlugin` 多态实现）解决设备形态可替换，**符号加载路线**（dlopen/dlsym + 惰性函数指针）解决运行期按需取符号。
- `ProfPluginManager` 只做一件事：持有当前 `ProfPlugin*`，缺省惰形指向 `ProfCannPlugin`，可被 `SetProfPlugin` 替换——一个 6 行的策略模式。
- `ProfCannPlugin` 是主插件：dlopen `libprofimpl.so` 打通接口层→实现层，并用三类无锁缓冲（Report/Block/VariableBlock）把业务线程的上报与实现层的异步落盘解耦。
- `ProfAclPlugin`（17 个 once 标志逐方法惰性解析）与 `ProfRuntimePlugin`（map 集中装载，只代理 `rtProfilerTraceEx`）是符号加载路线的两个风格样本；`ProfTxPlugin` 面向用户打点，`ProfAvpPlugin`/lite so 对应裁剪形态。
- 本仓当前只保留 profapi 的头文件契约，实现 `.cpp` 已在提交 `64ebbaa` 中删除，可用 `git show 64ebbaa^:<路径>` 考古——读接口、考历史实现是本讲的方法论。
- 新增数据源插件的成本极低：一个头文件（函数指针类型 + 转发方法 + 可选单例）即可，数据出口复用主插件的上报缓冲通道。

## 7. 下一步学习建议

数据被采集上来之后去哪？下一讲 **u4-l4 analyze 模块：原始性能数据的分类分析器** 将精读 `src/msprof/collector/dvvp/analyze/inc/` 下的 `analyzer_base.h` 与 rt/hwts/ge/ts/ffts 等派生分析器，看 `libprofimpl.so` 落盘的原始数据如何被分类解析成 timeline 与统计结果——与本讲的 4.3 节正好首尾相接。如果你更关心用户侧如何触发这些采集，可以先跳读 u4-l5（msprof 命令、环境变量与 acl.json 采集方式），再回来读 analyze。
