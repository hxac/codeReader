# 目录结构与模块全景

## 1. 本讲目标

读完本讲，你应当能够：

- 在脑海里建立 **offload 子项目的整体目录地图**，知道每个一级目录分别负责什么。
- 理解项目的 **分层架构**：编译器入口层、OpenMP 运行时层、设备插件层、新统一 API 层、工具与测试层。
- 能根据一个功能需求（例如「我想加一个新设备后端」「我想看内核是怎么启动的」）**快速定位到对应的源码目录**。
- 画出一条贯穿所有层的调用路径：**编译器调用 → libomptarget 入口 → PluginManager → 具体插件 → 设备**。

本讲是「地图课」，不深入任何单一机制的实现细节，只帮你建立「去哪里找代码」的能力。后续每一篇讲义都会在这张地图的某个格子里深入。

## 2. 前置知识

本讲承接 [u1-l1 项目定位与 OpenMP 目标卸载概念](./u1-l1-project-overview.md) 与 [u1-l2 构建系统与依赖](./u1-l2-build-system.md)。在继续之前，请确认你已经理解下面这些在前面讲义里已经建立的概念（本讲不再重复解释）：

- **offload 子项目的定位**：为加速器/协处理器提供工具、运行时和 API，让代码运行在与主机架构可能不同的设备上；对 OpenMP 卸载用户已成熟，统一 API 仍在开发。
- **主机/设备两分法**：主机（host）发起卸载，设备（device）执行内核；运行时负责「搬运数据 + 启动内核」。
- **libomptarget 与 liboffload 的关系**：libomptarget 是绑定 OpenMP 的成熟运行时，liboffload 是不绑定 OpenMP 的开发中通用新 API，二者**共享同一套底层设备插件**（plugins-nextgen）。
- **构建组装方式**：顶层 `CMakeLists.txt` 通过若干 `add_subdirectory(...)` 把各模块拼装起来，并用 `LIBOMPTARGET_PLUGINS_TO_BUILD` 选择要构建哪些插件。

下面几个术语在本讲会用到，先给一句话解释：

| 术语 | 一句话解释 |
| --- | --- |
| 入口函数（entry） | 编译器在生成的代码里直接调用的运行时函数，名字以 `__tgt_` 开头。 |
| 插件（plugin） | 针对某一种设备后端（CPU/CUDA/AMDGPU/Level Zero）的具体实现。 |
| 调用链（call chain） | 一次卸载请求从主机函数层层下沉到设备硬件所经过的代码路径。 |

## 3. 本讲源码地图

本讲主要阅读下列文件，它们共同构成项目的「骨架」：

| 文件 | 作用 |
| --- | --- |
| [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt) | 顶层构建脚本，用 `add_subdirectory(...)` 决定各模块的组装顺序。 |
| [`include/omptarget.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h) | 编译器与运行时之间的「契约」头文件，定义 `__tgt_*` 入口与核心数据结构。 |
| [`include/PluginManager.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h) | 声明 `PluginManager`，统筹管理所有插件与设备。 |
| [`libomptarget/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/CMakeLists.txt) | 定义 `omptarget` 共享库由哪些源文件组成、如何链接各插件。 |
| [`libomptarget/interface.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp) | `__tgt_*` 入口的实现，是调用链的「大门」。 |

先用一张「鸟瞰表」给出全部一级目录（后续 4.x 会逐个展开）：

| 一级目录 | 层次 | 一句话职责 |
| --- | --- | --- |
| `include/` | 契约层 | 共享头文件：编译器-运行时契约、核心数据结构、跨模块公共定义。 |
| `libomptarget/` | 运行时层 | OpenMP 目标运行时 `omptarget.so`：实现 `__tgt_*`、数据映射、内核启动。 |
| `plugins-nextgen/` | 插件层 | 设备插件框架（`common/`）与各后端（`host`/`cuda`/`amdgpu`/`level_zero`）。 |
| `liboffload/` | 新 API 层 | 不绑定 OpenMP 的统一 Offload API（开发中），构建在插件之上。 |
| `tools/` | 工具层 | `offload-tblgen`（代码生成）、`deviceinfo`、`kernelreplay`。 |
| `test/`、`unittests/` | 测试层 | lit 功能测试与 C++ 单元/一致性测试。 |
| `cmake/`、`ci/`、`docs/`、`utils/` | 辅助 | 依赖探测、CI 脚本、Sphinx 文档、辅助脚本。 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录地图与构建组装（全局视角）

#### 4.1.1 概念说明

要理解一个多模块项目，最有效的方式不是一头扎进某个文件，而是先看 **构建脚本如何把它拼起来**。offload 的顶层 `CMakeLists.txt` 就是这样一张「组装图」：它用一连串 `add_subdirectory(...)` 告诉我们「项目由哪几个模块构成、它们之间的依赖顺序是什么」。

记住一个核心心智模型——**分层 + 调用链**：

- **分层**：从上到下是「编译器 → libomptarget（运行时）→ PluginManager → 具体插件 → 设备硬件」。上层依赖下层提供的抽象，下层不知道上层的存在。
- **调用链**：一次 `target` 卸载请求，会沿着这条路径从顶端的入口函数一路下沉到设备。

liboffload 是一个**横向并列**的新 API 层：它和 libomptarget 一样构建在插件之上，但不绑定 OpenMP。

#### 4.1.2 核心流程

构建时的模块组装顺序（也就是依赖顺序）如下，可直接对照顶层 `CMakeLists.txt` 末尾的 `add_subdirectory` 块：

```
1. tools/offload-tblgen   ← 先生成代码（liboffload 的 API 头/文档）
2. plugins-nextgen        ← 再构建各设备插件（被上层依赖）
3. tools                  ← deviceinfo / kernelreplay（依赖运行时）
4. docs                   ← 文档
5. libomptarget           ← 运行时，链接所有插件
6. liboffload             ← 新 API，构建在插件之上
7. test / unittests       ← 最后是测试
```

一次卸载请求的**运行期调用链**（这是本讲最重要的图，4.x 各模块都会对应其中一段）：

```
用户程序里的 #pragma omp target
        │  （Clang 在编译期生成）
        ▼
libomptarget/interface.cpp 的 __tgt_* 入口      ← 4.3 节
        │  （调用单例 PM 的方法）
        ▼
PluginManager（统筹所有插件与设备）              ← 4.3 节
        │  （把设备号映射到某个插件）
        ▼
具体插件 GenericPluginTy / GenericDeviceTy       ← 4.4 节
        │  （落到驱动/硬件）
        ▼
设备硬件
```

而 `include/`（4.2 节）提供贯穿全链路的契约与数据结构；`liboffload`（4.5 节）是 plugin 之上、与 libomptarget 并列的另一条上层路径；`tools`（4.6 节）是辅助工具。

#### 4.1.3 源码精读

组装顺序写在顶层 `CMakeLists.txt` 的这一段，每一个 `add_subdirectory` 对应表里的一级目录：

[CMakeLists.txt:331-348](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L331-L348) —— 顶层构建脚本末尾的 `add_subdirectory` 块，决定了各模块的拼装与依赖顺序（先 tblgen 与插件，再运行时与新 API，最后测试）。

这段里还定义了两个贯穿全局的高层目标：`offload`（构建全部运行时与插件，供 CI 预提交使用）和 `install-offload`（按 `offload` 组件安装）。这两者把分散在各子目录的产物统一成一个可构建、可安装的整体。

#### 4.1.4 代码实践

**实践目标**：用构建脚本自己核对一遍「目录 → 模块」的对应关系。

**操作步骤**：

1. 打开顶层 [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt) 第 331–348 行。
2. 逐行把每个 `add_subdirectory(X)` 与本节「鸟瞰表」里的目录对照。
3. 注意第 338–340 行：`libomptarget` 被 `if(BUILD_LIBOMPTARGET)` 包住——回忆 [u1-l2](./u1-l2-build-system.md)，Windows（MSVC）下会关闭它。

**需要观察的现象**：你会发现组装顺序不是随意的——`tools/offload-tblgen` 和 `plugins-nextgen` 必须先于 `libomptarget`/`liboffload`，因为后者要链接前者产出的库。

**预期结果**：能在不看书的情况下，口述出「7 个 add_subdirectory 的先后顺序与各自产物」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `libomptarget` 的 `add_subdirectory` 排在 `plugins-nextgen` 之后？

> **参考答案**：因为运行时库 `omptarget.so` 在链接阶段需要把所有要构建的插件（`omptarget.rtl.<plugin>`）作为依赖连进来（见 4.3 节源码），所以插件必须先于运行时构建。

**练习 2**：`offload` 这个 custom target 和某个具体子目录是什么关系？

> **参考答案**：它是一个聚合目标，本身不含源码，只是用 `add_dependencies` 把 `omptarget`、`LLVMOffload` 等子产物串起来，方便 CI 一条命令构建全部。

---

### 4.2 include/ —— 共享头文件层（编译器-运行时契约）

#### 4.2.1 概念说明

`include/` 是整个项目的「公共语言」。它解决的问题有三个：

1. **编译器要知道调用哪些函数**：Clang 在为 `#pragma omp target` 生成代码时，需要知道运行时入口的名字和签名——这些就定义在 `include/omptarget.h`。
2. **各层之间要共享数据结构**：比如「一段设备镜像长什么样」「map 子句有哪些类型」这类定义，运行时和插件都要用。
3. **公共工具定义集中管理**：环境变量、调试宏、引用计数等跨模块复用的小工具。

它本身不含实现，只有声明与定义，因此是最稳定的「契约」层。

#### 4.2.2 核心流程

`include/` 内部又分三个子目录，按「被谁使用」组织：

```
include/
├── omptarget.h, device.h, PluginManager.h, ...   ← 运行时核心契约
├── Shared/    ← 跨模块共享：APITypes.h, Environment.h, RPCOpcodes.h ...
├── OpenMP/    ← OpenMP 专属：omp.h, Mapping.h, OMPT/...
└── Utils/     ← 通用工具：ExponentialBackoff.h
```

- 顶层文件是**编译器-运行时契约**与运行时核心抽象（`PluginManager.h`、`device.h`）。
- `Shared/` 是**所有层共享**的纯定义（数据结构、环境变量、错误码），插件和运行时都会 include。
- `OpenMP/` 只服务 OpenMP 语义（用户头 `omp.h`、映射 `Mapping.h`、工具回调 `OMPT/`）。

#### 4.2.3 源码精读

`omptarget.h` 的文件头注释直接点明了它的身份——「Clang 在 target region 代码生成期间使用的接口」：

[include/omptarget.h:1-12](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L1-L12) —— 文件头注释，说明本头文件是 Clang 代码生成 target region 时调用的运行时接口契约。

同一个文件里定义了 `tgt_map_type` 枚举，它把 OpenMP 的 `map(to:/from:/alloc/...)` 语义编码成运行时可判断的位标志：

[include/omptarget.h:49-91](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L49-L91) —— `tgt_map_type` 枚举，每一位代表一种数据映射属性（TO/FROM/ALWAYS/PRIVATE/...），是编译期 map 子句到运行期的「翻译表」。

`PluginManager.h` 声明了统筹全局的 `PluginManager` 结构体，以及一个全局单例指针 `PM`：

[include/PluginManager.h:43](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L43) —— `struct PluginManager` 的声明起点，运行时通过它统一管理所有插件与设备。

[include/PluginManager.h:192](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L192) —— `extern PluginManager *PM;` 全局单例，入口函数通过它访问管理器（调用链里的关键一跳）。

#### 4.2.4 代码实践

**实践目标**：感受「契约」的稳定性——同一个头文件被链路两端共用。

**操作步骤**：

1. 打开 [`include/omptarget.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h)，浏览 49–91 行的 `tgt_map_type`。
2. 记住其中 `OMP_TGT_MAPTYPE_TO = 0x001`、`OMP_TGT_MAPTYPE_FROM = 0x002` 两个值。
3. 稍后在 4.3 节会看到运行时如何依据这些位标志决定是否搬运数据。

**需要观察的现象**：这些枚举值既是编译器生成调用时填进去的「参数」，也是运行时判断逻辑的依据——一份定义，两边共用。

**预期结果**：理解为什么 `include/` 被称为「契约层」——改这里的接口，编译器和运行时要同步改动。

**待本地验证**：若你本地有完整 LLVM 源码，可用 `grep -rn "OMP_TGT_MAPTYPE_TO" libomptarget/` 看运行时在多少处用到它。

#### 4.2.5 小练习与答案

**练习 1**：`include/Shared/` 和 `include/OpenMP/` 的分工区别是什么？

> **参考答案**：`Shared/` 放与 OpenMP 无关、所有层（含插件、liboffload）都复用的通用定义；`OpenMP/` 只放 OpenMP 语义相关的头文件（如用户 API 头 `omp.h`、映射 `Mapping.h`）。

**练习 2**：为什么把 `__tgt_*` 入口签名放在 `include/omptarget.h` 而不是 `libomptarget/` 里？

> **参考答案**：因为 Clang 编译器（在 LLVM 另一个子项目里）也需要这份签名来生成调用，放在公共 `include/` 才能被编译器和运行时同时包含，确保两边签名一致。

---

### 4.3 libomptarget/ —— OpenMP 目标运行时

#### 4.3.1 概念说明

`libomptarget/` 是项目的「心脏」：它编译出 `omptarget.so` 共享库，实现了全部 `__tgt_*` 入口、主机-设备数据映射、内核启动编排。它对上承接编译器生成的调用，对下通过 `PluginManager` 调度各设备插件。

它是**绑定 OpenMP** 的运行时——所有逻辑都围绕 OpenMP 卸载语义（target data、map 子句、target region、nowait 等）展开。

#### 4.3.2 核心流程

`libomptarget/CMakeLists.txt` 用一份源文件清单精确告诉我们 `omptarget.so` 由什么组成：

```
libomptarget/
├── interface.cpp        ← __tgt_* 入口实现（调用链大门）
├── omptarget.cpp        ← target data / kernel 主流程
├── device.cpp           ← DeviceTy：上层↔插件桥梁
├── PluginManager.cpp    ← PluginManager 实现
├── DeviceImage.cpp      ← 设备镜像抽象
├── OffloadRTL.cpp       ← 插件注册/枚举
├── LegacyAPI.cpp        ← 旧版兼容 API
├── OpenMP/              ← OpenMP 专属：API.cpp(omp_target_*), Mapping.cpp, OMPT/
└── KernelLanguage/      ← 内核语言相关：API.cpp
```

调用链在 libomptarget 内部这一段是：**入口函数 → 单例 `PM` → `PluginManager` 的方法 → 某个 `DeviceTy`**。

#### 4.3.3 源码精读

`libomptarget/CMakeLists.txt` 第 7–22 行定义共享库的源文件清单，正是上面目录结构的体现：

[libomptarget/CMakeLists.txt:7-22](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/CMakeLists.txt#L7-L22) —— `add_library(omptarget SHARED ...)` 的源文件清单，逐个对应运行时的各个职责模块。

而第 44–46 行是运行时「链接所有插件」的关键——这正是它排在 `plugins-nextgen` 之后构建的原因：

[libomptarget/CMakeLists.txt:44-46](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/CMakeLists.txt#L44-L46) —— `foreach` 把每个要构建的插件 `omptarget.rtl.<plugin>` 链接进 `omptarget` 库，使运行时能在运行期调度各后端。

调用链的大门在 `interface.cpp`。它包含 `PluginManager.h` 并使用单例 `PM`：

[libomptarget/interface.cpp:18](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L18) —— `#include "PluginManager.h"`，入口层引入管理器。

几个最典型的入口：初始化、二进制注册、设备初始化：

[libomptarget/interface.cpp:86-101](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L86-L101) —— `__tgt_rtl_init`/`__tgt_rtl_deinit`（运行时初始化）、`__tgt_register_lib`（把设备镜像二进制描述符注册到运行时，内部调用 `PM->registerLib(Desc)`）、`__tgt_init_all_rtls`（初始化所有插件的设备）。

注意第 96 行 `PM->registerLib(Desc)`：这就是调用链从「入口」跳到「管理器」的那一跳。

管理器这一侧，`PluginManager.cpp` 负责真正去初始化各插件里的设备、并按设备号取回设备对象：

[libomptarget/PluginManager.cpp:123](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L123) —— `PluginManager::initializeAllDevices`，遍历所有插件完成设备初始化。

[libomptarget/PluginManager.cpp:553](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L553) —— `PluginManager::getDevice(DeviceNo)`，把一个 OpenMP 设备号映射成具体的 `DeviceTy &`（调用链里「设备号 → 具体设备」的一跳）。

#### 4.3.4 代码实践

**实践目标**：用源码确认「入口 → 管理器 → 设备」这三跳真实存在。

**操作步骤**：

1. 打开 [`libomptarget/interface.cpp:86-101`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L86-L101)，确认 `__tgt_register_lib` 内部调用了 `PM->registerLib(Desc)`。
2. 打开 [`libomptarget/PluginManager.cpp:123`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L123)，确认管理器会遍历插件初始化设备。
3. 打开 [`libomptarget/PluginManager.cpp:553`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L553)，确认 `getDevice` 返回的是 `DeviceTy &`。

**需要观察的现象**：三段代码分别属于「入口层 / 管理器层」，但通过 `PM` 单例和 `DeviceTy` 串成一条线。

**预期结果**：能在源码上指出调用链在 libomptarget 内部的完整轨迹。

#### 4.3.5 小练习与答案

**练习 1**：`libomptarget/OpenMP/` 子目录与 `libomptarget/` 根目录的源文件为何要分开？

> **参考答案**：根目录放与 OpenMP 无关或更底层的运行时机制（入口、管理器、设备抽象）；`OpenMP/` 放 OpenMP 专属逻辑（用户面 `omp_target_*` API、映射、OMPT 回调）。这样非 OpenMP 的上层（如 liboffload）未来更易复用底层部分。

**练习 2**：为什么 `interface.cpp` 要用全局单例 `PM` 而不是把 `PluginManager` 作为参数传进每个入口函数？

> **参考答案**：因为入口函数的签名是**编译器契约**（由 `__tgt_*` 规定，不能随意改），无法增加参数；用一个进程级单例来持有状态是保持入口签名稳定的常见做法。

---

### 4.4 plugins-nextgen/ —— 设备插件框架与各后端

#### 4.4.1 概念说明

`plugins-nextgen/` 是「设备后端」的家。它解决的问题是：**不同加速器（CPU、NVIDIA GPU、AMD GPU、Intel GPU）的驱动和编程模型完全不同，但运行时不想为每种设备写一套上层逻辑**。于是项目抽象出一套通用插件框架（`common/`），每个具体后端只需实现框架要求的少数接口。

回忆 [u1-l1](./u1-l1-project-overview.md) 的关键结论：**libomptarget 与 liboffload 共享同一套底层插件**，就在这个目录里。

#### 4.4.2 核心流程

目录按「框架 + 后端」组织：

```
plugins-nextgen/
├── common/              ← 通用框架（抽象基类）
│   ├── include/         ← PluginInterface.h, GlobalHandler.h, MemoryManager.h, RPC.h, JIT.h, RecordReplay.h ...
│   └── src/             ← 上述框架的实现
├── host/                ← CPU 参考实现（最简单，src/rtl.cpp）
├── cuda/                ← NVIDIA（src/rtl.cpp + dynamic_cuda 动态加载）
├── amdgpu/              ← AMD（src/rtl.cpp + dynamic_hsa + utils）
└── level_zero/          ← Intel（src/ + include/，模块化 L0* 设计）
```

框架的核心是几个「泛型」基类，每个后端都继承它们：`GenericPluginTy`（插件）、`GenericDeviceTy`（设备）、`GenericKernelTy`（内核）、`GenericGlobalHandlerTy`（全局变量）。上层 `DeviceTy` 持有的就是这些基类指针，从而与具体后端解耦。

#### 4.4.3 源码精读

通用框架的核心抽象都集中在 `PluginInterface.h`：

[plugins-nextgen/common/include/PluginInterface.h:1462](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1462) —— `struct GenericPluginTy`，所有后端插件的基类，定义 `init`/`loadBinary`/`synchronize` 等虚函数契约。

[plugins-nextgen/common/include/PluginInterface.h:888](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L888) —— `struct GenericDeviceTy`，设备抽象基类（继承 `DeviceAllocatorTy`），规定内存分配、数据搬运、内核启动等接口。

[plugins-nextgen/common/include/PluginInterface.h:430](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L430) —— `struct GenericKernelTy`，内核对象的抽象，封装 init/launch 等。

各后端的入口都约定叫 `rtl.cpp`，便于对照阅读：

- [`plugins-nextgen/host/src/rtl.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/host/src/rtl.cpp) —— host（CPU）插件，最简参考实现，继承上述基类在主机上「卸载」并执行 ELF 镜像。
- [`plugins-nextgen/cuda/src/rtl.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/cuda/src/rtl.cpp) —— NVIDIA CUDA 后端。
- [`plugins-nextgen/amdgpu/src/rtl.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/amdgpu/src/rtl.cpp) —— AMD 后端。
- [`plugins-nextgen/level_zero/src/L0Plugin.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/level_zero/src/L0Plugin.cpp) —— Intel Level Zero 后端（注意它额外拆分出 `L0Device`/`L0Queue`/`L0Memory` 等模块）。

#### 4.4.4 代码实践

**实践目标**：验证「同一份框架被多个后端复用」。

**操作步骤**：

1. 打开 [`plugins-nextgen/common/include/PluginInterface.h:1462`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1462) 记下 `GenericPluginTy` 的名字。
2. 在后端目录里搜索它的派生类（例如 host 插件里的 `HostPluginTy` 或类似命名）。

**需要观察的现象**：每个后端的 `rtl.cpp` 都定义一个继承 `GenericPluginTy` 的类，并 override 少数关键方法。

**预期结果**：理解「加一个新后端 = 继承框架基类并实现若干虚函数」，上层运行时代码无需改动。

**待本地验证**：`grep -n "GenericPluginTy" plugins-nextgen/host/src/rtl.cpp` 看继承关系。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `level_zero` 后端要拆成 `L0Plugin`/`L0Device`/`L0Queue`/`L0Memory` 等多个文件，而 `host` 后端只有一个 `rtl.cpp`？

> **参考答案**：Level Zero 的驱动模型本身就区分 device/queue/memory/context 等多种对象，逻辑复杂，拆分有助于维护；host 后端在主机上执行 ELF，逻辑最简单，单文件足够。复杂度不同，组织方式也不同。

**练习 2**：运行时上层（`DeviceTy`）如何做到「不关心具体是哪种 GPU」？

> **参考答案**：上层只持有框架基类 `GenericDeviceTy` 的指针/引用，调用其虚函数；具体行为由各后端的派生类在运行期通过虚分派决定，这就是典型的多态解耦。

---

### 4.5 liboffload/ —— 新统一 API 层

#### 4.5.1 概念说明

`liboffload/` 是项目里**正在开发**的新一层。它的目标在 README 里说得很直白：构建在现有插件之上，但提供一个**不绑定 OpenMP** 的抽象，方便未来实现多种卸载语言运行时（而不仅仅是 OpenMP）。

可以把它理解为「libomptarget 的同类，但更通用、更面向对象」。两者并列站在插件层之上。

#### 4.5.2 核心流程

```
liboffload/
├── README.md            ← 定位与用法（含 OFFLOAD_TRACE、check-offload-unit）
├── src/                 ← OffloadImpl.cpp / OffloadLib.cpp / Helpers.hpp（实现）
├── include/             ← OffloadImpl.hpp（内部对象模型）
└── API/                 ← *.td 表定义（Platform/Device/Context/Queue/Kernel/Memory/Event/Program/Symbol）
```

它的对外 API 不是手写的，而是由 `API/*.td`（TableGen 格式）经 `tools/offload-tblgen` **生成**——这是它与 libomptarget 在工程方式上的一个显著区别。对象模型也更「现代化」：Platform / Device / Context / Queue / Kernel / Memory / Event。

#### 4.5.3 源码精读

定位说明在 README 开头：

[liboffload/README.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md) —— 明确写着这是「work-in-progress new API」，构建在已有插件之上，提供适合实现多种卸载语言运行时的单一抽象层（而非仅 OpenMP）。

实现主体与对外 API 定义：

[liboffload/src/OffloadImpl.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/src/OffloadImpl.cpp) —— liboffload 的实现主体，在插件之上实现 Platform/Device/Queue/Kernel 等对象。

[liboffload/API/OffloadAPI.td](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/API/OffloadAPI.td) —— 对外 API 的 TableGen 定义入口，由 offload-tblgen 生成头文件与文档。

#### 4.5.4 代码实践

**实践目标**：通过 README 的「追踪（trace）」功能直观感受 liboffload 的对象模型。

**操作步骤**：

1. 打开 [`liboffload/README.md`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md)，阅读「Testing liboffload」与 `OFFLOAD_TRACE` 段落。
2. 注意它提到主测试目标是 `check-offload-unit`，且 `OFFLOAD_TRACE=1` 可打印形如 `---> olInit(nullptr)-> OL_SUCCESS` 的调用序列。

**需要观察的现象**：trace 输出里的 `olInit`、`OL_SUCCESS` 等符号，正是 liboffload 对外 API（`ol*` 前缀）与返回码。

**预期结果**：理解 liboffload 是一套独立的、对象化的 API，与 libomptarget 的 `__tgt_*` 是两套并存的对外接口。

**待本地验证**：若本地已构建，可运行 `OFFLOAD_TRACE=1 ./offload.unittests` 观察 trace（README 注明 host 插件暂不支持）。

#### 4.5.5 小练习与答案

**练习 1**：liboffload 和 libomptarget 各自构建在什么之上？它们互相依赖吗？

> **参考答案**：二者都构建在 `plugins-nextgen` 插件层之上，彼此**不直接依赖**——它们是插件层之上的两条并列上层路径，只是 libomptarget 成熟且绑定 OpenMP，liboffload 通用且仍在开发。

**练习 2**：为什么 liboffload 的对外 API 用 TableGen（`.td`）定义而不是直接写 C 头文件？

> **参考答案**：因为同一份 `.td` 描述可以由 `offload-tblgen` 同时生成 C 头文件、文档、入口点胶水代码等多份产物，避免手写多份一致的内容，是 LLVM 子项目里常见的代码生成做法。

---

### 4.6 tools/ 工具层与 test/unittests 测试目录

#### 4.6.1 概念说明

`tools/` 里是三个独立可执行工具，分别在「构建期」和「运行期」辅助项目：

- `offload-tblgen`：**构建期**代码生成器，把 liboffload 的 `.td` 转成 API 头/文档。
- `deviceinfo`（`llvm-offload-device-info`）：**运行期**诊断工具，列出运行时可见的设备。
- `kernelreplay`（`llvm-omp-kernel-replay`）：**运行期**工具，重放录制好的内核执行。

`test/` 与 `unittests/` 则是质量保障：前者是 lit 端到端功能测试，后者是 C++ 单元测试与 API 一致性测试。

#### 4.6.2 核心流程

```
tools/
├── offload-tblgen/   ← offload-tblgen.cpp + APIGen/DocGen/EntryPointGen/...
├── deviceinfo/       ← llvm-offload-device-info.cpp
└── kernelreplay/     ← llvm-omp-kernel-replay.cpp

test/          ← api, offloading, mapping, ompt, jit, env, libc, tools, unified_shared_memory, unit, Inputs
unittests/     ← OffloadAPI/{init,platform,device,context,queue,kernel,memory,event,program,symbol,...}
                Conformance/{tests,lib,include,device_code}
```

工具与运行时的关系：`deviceinfo`/`kernelreplay` 会链接运行时库，因此 `tools` 的 `add_subdirectory` 排在 `libomptarget` 之后（见 4.1 节组装顺序里 `tools` 在前其实是指 `tools/offload-tblgen`，而 `deviceinfo`/`kernelreplay` 作为运行期工具依赖运行时）。

#### 4.6.3 源码精读

三个工具的入口源文件：

[tools/offload-tblgen/offload-tblgen.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/offload-tblgen/offload-tblgen.cpp) —— 代码生成器入口，配合同目录的 `Generators.hpp`/`APIGen.cpp` 等把 `.td` 转成产物。

[tools/deviceinfo/llvm-offload-device-info.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/llvm-offload-device-info.cpp) —— 设备信息诊断工具入口。

[tools/kernelreplay/llvm-omp-kernel-replay.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/kernelreplay/llvm-omp-kernel-replay.cpp) —— 内核重放工具入口。

测试目录的组织可直接看一级子目录：`test/` 下按功能分（`offloading` 端到端、`mapping` 数据映射、`ompt` 工具回调、`jit` 即时编译、`api` 用户 API 等）；`unittests/OffloadAPI/` 下按 liboffload 的对象模型分（`platform`/`device`/`queue`/`kernel`/`memory`/`event` 等），印证了 4.5 节的对象划分。

#### 4.6.4 代码实践

**实践目标**：把工具目录与 [u1-l4](./u1-l4-toolchain-and-run.md) 将要讲的运行期用法对应起来。

**操作步骤**：

1. 打开 [`tools/deviceinfo/llvm-offload-device-info.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/tools/deviceinfo/llvm-offload-device-info.cpp) 的 `main`，看它如何初始化运行时并枚举设备。
2. 浏览 `test/` 与 `unittests/` 的一级子目录名，把它们与前面讲的层对应（例如 `test/mapping` ↔ 4.3 数据映射，`unittests/OffloadAPI/queue` ↔ 4.5 liboffload）。

**需要观察的现象**：测试目录的划分几乎就是项目功能模块的镜像——每个功能模块都有自己的测试子目录。

**预期结果**：建立「功能模块 ↔ 测试目录」的对应直觉，方便日后定位测试。

**待本地验证**：`ls test/ unittests/OffloadAPI/` 对照本节目录树。

#### 4.6.5 小练习与答案

**练习 1**：`offload-tblgen` 和另外两个工具（deviceinfo/kernelreplay）在「何时运行」上有什么区别？

> **参考答案**：`offload-tblgen` 是**构建期**工具，在编译时生成代码（产物被编进源码树）；`deviceinfo`/`kernelreplay` 是**运行期**工具，由用户在运行时执行，链接运行时库。

**练习 2**：想验证一个 `map(to:)` 的行为，应该去哪个测试目录找现成用例？

> **参考答案**：去 `test/mapping/` 目录，那里专门放数据映射相关的 lit 测试。

---

## 5. 综合实践

**任务**：绘制一张贯穿全部层的 **分层调用路径图**，把本讲所有模块串起来。这是本讲的核心交付物。

**要求**：

1. 画出从「用户源码里的 `#pragma omp target`」到「设备硬件」的完整纵向路径，至少包含下列节点，并在每个节点旁**标注它对应的源码目录**：
   - 编译器（Clang，项目外）生成的 `__tgt_*` 调用；
   - `libomptarget/interface.cpp` 的入口函数；
   - 单例 `PM` 与 `PluginManager`（`libomptarget/` + `include/PluginManager.h`）；
   - `DeviceTy`（`libomptarget/device.cpp` + `include/device.h`）；
   - 具体插件的 `GenericPluginTy` / `GenericDeviceTy`（`plugins-nextgen/`）；
   - 设备硬件。
2. 在图的一侧，**横向画出 liboffload** 这条并列路径：`liboffload/OffloadImpl.cpp` → `plugins-nextgen`，标注它与 libomptarget 共享插件层但不互相依赖。
3. 在图上用虚线标出 `include/` 如何为整条链路提供契约（`omptarget.h`、`PluginManager.h`、`Shared/`）。
4. 在图下方用一行写出构建组装顺序（参考 4.1.2 的 7 步），并解释为什么插件要先于运行时构建。

**参考画法**（文字版骨架，建议你在纸上或绘图工具里重画成更清晰的图）：

```
        用户 #pragma omp target
               │ (Clang 生成)
               ▼
   ┌─ libomptarget/interface.cpp (__tgt_*)        [libomptarget/]
   │        │ PM->registerLib / getDevice
   │        ▼
   │   PluginManager (PM)                          [libomptarget/ + include/PluginManager.h]
   │        │
   │        ▼
   │   DeviceTy                                    [libomptarget/device.cpp + include/device.h]
   │        │ 虚分派
   ▼        ▼
   plugins-nextgen/<backend> (GenericPluginTy/GenericDeviceTy)   [plugins-nextgen/]
               │
               ▼
          设备硬件 (CPU/CUDA/AMDGPU/Intel)

   并列上层：liboffload/OffloadImpl.cpp ──────────────┐
        （不绑定 OpenMP，复用同一插件层）              │ 共享 plugins-nextgen
                                                      ┘
   契约层（虚线贯穿全图）：include/  omptarget.h / PluginManager.h / Shared/
```

**验收标准**：图上每一个节点都标注了正确的目录；能对着图解释「为什么是这样分层」以及「构建顺序为何如此」。这张图建议你保留，后续每读一篇讲义，就在对应节点上补充更细的细节。

## 6. 本讲小结

- offload 子项目按 **契约层（include）/ 运行时层（libomptarget）/ 插件层（plugins-nextgen)/ 新 API 层（liboffload)/ 工具与测试层（tools/test/unittests）** 分层组织。
- 顶层 `CMakeLists.txt` 的 `add_subdirectory` 顺序揭示了模块依赖：**tblgen 与插件先构建，运行时与新 API 后构建，测试最后**。
- 调用链是：**编译器 → `libomptarget/interface.cpp` 的 `__tgt_*` → 单例 `PM`/`PluginManager` → `DeviceTy` → 具体插件 `GenericDeviceTy` → 设备**。
- `include/omptarget.h` 是编译器-运行时契约，`tgt_map_type` 等定义被链路两端共用。
- libomptarget 与 liboffload 是插件层之上的**两条并列上层路径**，前者绑定 OpenMP 且成熟，后者通用且开发中。
- 测试目录（`test/`、`unittests/`）的划分基本是功能模块的镜像，可用于快速定位用例。

## 7. 下一步学习建议

现在你有了整张地图，下一步建议**沿着调用链向深处走**：

- 想理解「编译器到底生成了哪些调用、数据结构长什么样」→ 读 [u1-l5 编译器-运行时契约与核心数据结构](./u1-l5-compiler-runtime-contract.md)（深入 `include/`）。
- 想理解「运行时如何初始化、如何注册设备镜像」→ 进入第二单元，读 [u2-l1 运行时初始化与库注册入口](./u2-l1-runtime-entry.md)（深入 `libomptarget/interface.cpp`）。
- 想先动手跑一个程序、用工具看设备 → 读 [u1-l4 工具链、编译运行与设备信息](./u1-l4-toolchain-and-run.md)。

无论选哪条路，记得带着本讲画的「分层调用路径图」一起读——在每个节点处停下来，对照源码确认你的理解。
