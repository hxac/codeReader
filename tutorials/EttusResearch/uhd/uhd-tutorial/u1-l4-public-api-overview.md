# 公共 API 头文件全景

## 1. 本讲目标

本讲带你把 UHD **公共头文件的「地图」**装进脑子里。学完后你应该能够：

1. 说出 `host/include/uhd/` 目录的顶层文件与子目录分别对应哪个功能模块。
2. 区分 `config.hpp` 与 `config.h`、`version.hpp.in` 与 `version.h` 的不同角色。
3. 理解 `build_info.hpp` 提供的构建期元数据从哪里来、怎么查。
4. 建立 **C++ API** 与 **C API** 两套并行头文件之间的映射概念。
5. 看到一个头文件路径，能立刻判断它属于哪个子系统（types / usrp / rfnoc / transport…），从而为后续阅读 `device.hpp`、`stream.hpp` 等核心文件打下导航基础。

> 本讲只做「门牌号」级别的导览，不深入任何模块的实现逻辑。设备工厂、流式 API、RFNoC 等会在后续讲义（u2、u3）展开。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1-l1 ~ u1-l3）：

- **UHD 是什么**：运行在主机的 USRP 驱动库 `libuhd`，用统一接口屏蔽不同型号硬件差异。
- **仓库结构**：本讲全部聚焦 `host/` 目录，其中 `host/include/uhd/` 是对外暴露的 **公共头文件**（Public API Headers）。
- **CMake 构建链**：构建时用 `configure_file` 把模板文件注入版本号变量（见 u1-l3）。

补充两个本讲会用到的术语：

- **公共头文件（Public Headers）**：随 `libuhd` 一起安装、供下游应用程序 `#include` 的头文件。它们是 UHD 与外界之间的「契约」，改名或删除都会破坏 ABI/源码兼容性。
- **ABI（Application Binary Interface）**：编译后的二进制接口约定。头文件里类的内存布局、虚表结构变化会改变 ABI；这就是 `version.hpp.in` 里那个 `UHD_VERSION_ABI_STRING` 存在的意义。

## 3. 本讲源码地图

| 文件 / 目录 | 角色 |
| --- | --- |
| `host/include/uhd.h` | **C API 的「伞式头文件」**，一次性聚拢所有 C 语言头文件 |
| `host/include/uhd/CMakeLists.txt` | 决定哪些头文件被安装、`version.hpp` 如何生成 |
| `host/include/uhd/config.hpp` | C++ 跨平台编译宏（导出符号、平台判定、弃用标记等） |
| `host/include/uhd/config.h` | C 语言版跨平台编译宏 |
| `host/include/uhd/version.hpp.in` | C++ 版本头文件 **模板**，构建期被替换成 `version.hpp` |
| `host/include/uhd/version.h` | C 语言版版本查询函数 |
| `host/include/uhd/build_info.hpp` | 构建期元数据（编译器、依赖、组件等）查询接口 |
| `host/include/uhd/device.hpp` | 设备基类入口，整个设备体系的根（后续 u2 深入） |
| `host/include/uhd/{types,usrp,rfnoc,transport,cal,experts,features,utils}/` | 各功能子系统的头文件目录 |

## 4. 核心概念与源码讲解

### 4.1 include/uhd 模块：公共 API 目录全貌

#### 4.1.1 概念说明

`host/include/uhd/` 是 UHD 对外暴露的全部公共头文件集合。它有两个层级：

1. **顶层头文件**：放在 `include/uhd/` 根下的若干「总入口」，如 `device.hpp`、`stream.hpp`、`convert.hpp`、`property_tree.hpp`、`rfnoc_graph.hpp`、`exception.hpp`。
2. **子目录**：按子系统归类的头文件，如 `types/`（基础类型）、`usrp/`（USRP 设备类）、`rfnoc/`（RFNoC 架构）、`transport/`（传输层）、`cal/`（校准）、`experts/`（属性传播框架）、`features/`（可发现特性接口）、`utils/`（工具函数）。

这种「顶层总入口 + 子目录」的布局让使用者既能从高层（`device.hpp`）进入，也能深入具体子系统（`transport/udp_zero_copy.hpp`）。

#### 4.1.2 核心流程

一条头文件从源码到被下游使用的路径：

1. 源码树里的头文件位于 `host/include/uhd/**`。
2. `host/include/uhd/CMakeLists.txt` 用 `UHD_INSTALL(FILES …)` 声明哪些头文件要安装（即被「公开」）。
3. CMake 在构建期执行 `configure_file` 把 `version.hpp.in` 渲染成 `version.hpp`。
4. `make install` 把这些头文件复制到 `${CMAKE_INSTALL_INCLUDEDIR}/uhd`。
5. 下游程序通过 `#include <uhd/xxx.hpp>` 引用，链接器再把它和 `libuhd.so` 对接。

#### 4.1.3 源码精读

安装清单与版本头生成逻辑在 `host/include/uhd/CMakeLists.txt` 中：

- 子目录递归处理（每个子系统自己声明要安装哪些头文件）：

  [host/include/uhd/CMakeLists.txt:9-18](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/CMakeLists.txt#L9-L18)
  ——这 10 行 `add_subdirectory` 对应 10 个子系统目录：`cal`、`experts`、`extension`、`features`、`rfnoc`、`transport`、`types`、`usrp`、`usrp_clock`、`utils`。

- 顶层 C++ 头文件的安装清单：

  [host/include/uhd/CMakeLists.txt:25-38](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/CMakeLists.txt#L25-L38)
  ——注意第 35 行安装的是 `${CMAKE_CURRENT_BINARY_DIR}/version.hpp`，即构建产物，而非源码树里的 `version.hpp.in`。

- C 语言头文件 **仅当 `ENABLE_C_API` 开启** 才安装：

  [host/include/uhd/CMakeLists.txt:40-48](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/CMakeLists.txt#L40-L48)
  ——这说明 C API 是「可选组件」，是构建期开关 `ENABLE_C_API` 决定的（与 u1-l3 讲的组件机制呼应）。

顶层头文件的快速归类表（按子系统）：

| 顶层头文件 | 所属子系统 / 作用 |
| --- | --- |
| `device.hpp` | 设备体系根，`uhd::device` 基类（发现/制造/流） |
| `stream.hpp` | 流式传输（`stream_args_t`、`rx_streamer`、`tx_streamer`） |
| `convert.hpp` | 样本格式转换注册 |
| `property_tree.hpp` / `.ipp` | 设备配置中枢「属性树」 |
| `rfnoc_graph.hpp` | RFNoC 流图会话 |
| `exception.hpp` | C++ 异常类型 |
| `image_loader.hpp` | 固件/FPGA 镜像加载 |
| `build_info.hpp` | 构建元数据 |

每个子目录则承载更细的头文件，例如：

- `types/device_addr.hpp`、`types/metadata.hpp`、`types/ranges.hpp` —— 基础数据类型。
- `usrp/multi_usrp.hpp` —— 易用高层封装（u2-l3 主角）。
- `rfnoc/noc_block_base.hpp`、`rfnoc/mb_controller.hpp` —— RFNoC 块控制器（u3 主角）。
- `transport/udp_zero_copy.hpp`、`transport/vrt_if_packet.hpp` —— 传输层（u4 主角）。

#### 4.1.4 代码实践

> **实践目标**：在 `host/include/uhd/` 下找出 5 个最核心的头文件，说明它们分别属于哪个子系统。

操作步骤：

1. 进入 `host/include/uhd/` 目录，浏览顶层文件与各子目录。
2. 挑选 5 个头文件，填入下表（给出参考答案）：

| 头文件路径 | 所属子系统 | 一句话作用 |
| --- | --- | --- |
| `types/device_addr.hpp` | types | 设备地址键值对 |
| `usrp/multi_usrp.hpp` | usrp | USRP 高层易用封装 |
| `rfnoc/radio_control.hpp` | rfnoc | RFNoC 射频控制块 |
| `transport/udp_zero_copy.hpp` | transport | UDP 零拷贝传输 |
| `stream.hpp`（顶层） | 顶层总入口 | 收发流器与流参数 |

3. 观察现象：会发现**没有 `uhd.hpp` 这个 C++ 伞式头文件**——C++ 程序必须按需 `#include` 具体头文件。这和 C API 存在 `uhd.h` 伞式头（见 4.5）形成鲜明对比。

预期结果：能够凭路径前缀（`types/`、`usrp/`、`rfnoc/`、`transport/`…）立即判断任意 UHD 头文件归属的子系统。

#### 4.1.5 小练习与答案

**练习 1**：`host/include/uhd/` 顶层（非子目录）一共有多少个会被安装的 C++ 头文件？依据是什么？

> **答案**：9 个（`build_info.hpp`、`config.hpp`、`convert.hpp`、`device.hpp`、`exception.hpp`、`property_tree.hpp`、`property_tree.ipp`、`rfnoc_graph.hpp`、`stream.hpp`，外加构建产物 `version.hpp`）。依据是 `CMakeLists.txt` 第 25–38 行的 `UHD_INSTALL(FILES …)` 清单。

**练习 2**：`property_tree.ipp` 中的 `.ipp` 后缀是什么含义？

> **答案**：`.ipp` 是「内联实现文件」（inline implementation），通常被对应的 `.hpp` 用 `#include` 进来，用于存放模板或内联函数的实现，避免头文件过长。`property_tree.hpp` 与 `property_tree.ipp` 是一对。

---

### 4.2 config 模块：跨平台编译配置宏

#### 4.2.1 概念说明

UHD 是跨平台库，要在 Linux、Windows（MSVC / MinGW）、macOS、BSD 上用 GCC、Clang、MSVC 等不同编译器构建。不同编译器表达「导出符号」「标记弃用」「强制内联」「对齐」的方式各不相同。`config.hpp`（C++）和 `config.h`（C）就是用来抹平这些差异的「翻译层」。

它定义的宏分为几类：

- **可见性宏**：`UHD_EXPORT`、`UHD_IMPORT`，决定符号是否导出/导入动态库。
- **API 声明宏**：`UHD_API`、`UHD_API_HEADER`，标注在类/函数前，是使用者最常「见到」的宏。
- **能力宏**：`UHD_INLINE`、`UHD_FORCE_INLINE`、`UHD_DEPRECATED`、`UHD_ALIGNED(x)`、`UHD_UNUSED(x)`。
- **平台判定宏**：`UHD_PLATFORM_LINUX`、`UHD_PLATFORM_WIN32`、`UHD_PLATFORM_MACOS`、`UHD_PLATFORM_BSD`。
- **杂项**：`UHD_FALLTHROUGH`、字符串化宏 `STR` / `XSTR`。

#### 4.2.2 核心流程

`UHD_API` 是怎么在「导出」和「导入」之间切换的？关键在构建期定义的两个宏：

```text
构建 libuhd.so 时  → 定义 UHD_DLL_EXPORTS → UHD_API = UHD_EXPORT（导出符号）
使用 libuhd.so 时  → 不定义 UHD_DLL_EXPORTS → UHD_API = UHD_IMPORT（导入符号）
静态链接时        → 定义 UHD_STATIC_LIB → UHD_API = 空（无可见性要求）
```

判断逻辑是嵌套的 `#ifdef`：先看 `UHD_STATIC_LIB`，再看 `UHD_DLL_EXPORTS`。这套机制让**同一份头文件**既能被库自身构建时使用（导出），也能被下游应用包含时使用（导入）。

#### 4.2.3 源码精读

C++ 版 `UHD_API` 的定义：

[host/include/uhd/config.hpp:123-142](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/config.hpp#L123-L142)
——`UHD_API` 用于有 `.cpp` 实现并编进动态库的类；`UHD_API_HEADER` 用于纯头文件（header-only）实现的类。二者区分是为了在某些平台上正确处理「跨 DLL 边界」的可见性。

跨平台属性宏（节选 GCC/Clang 分支）：

[host/include/uhd/config.hpp:79-93](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/config.hpp#L79-L93)
——可见 GCC 和 Clang 都用 `__attribute__((visibility("default")))` 导出、`__attribute__((deprecated))` 标弃用、`__attribute__((always_inline))` 强制内联。而 MSVC 分支（[L55-L66](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/config.hpp#L55-L66)）则改用 `__declspec(dllexport)` 等。`config.hpp` 就是把「同一语义、不同语法」统一到一个宏名下。

平台判定宏：

[host/include/uhd/config.hpp:144-157](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/config.hpp#L144-L157)
——这组宏让头文件里可以写 `#ifdef UHD_PLATFORM_LINUX …` 来做平台相关的条件编译。

C 语言版 `config.h` 与 C++ 版的关键差异：

- `config.h` 用 `__GNUC__` 判断 GCC，`config.hpp` 用 `__GNUG__`（后者只在 C++ 下定义）。
- `config.hpp` 多了 C++ 专属宏，例如 `UHD_FALLTHROUGH`（[L52](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/config.hpp#L52)）、`UHD_FUNCTION` / `UHD_PRETTY_FUNCTION`、`UHD_FORCE_INLINE`。
- `config.hpp` 引入 `<ciso646>`（C++），`config.h` 引入 `<iso646.h>`（C），都是为了在 MSVC 下能用 `and`/`or`/`not` 关键字。

可对照阅读：[host/include/uhd/config.h:71-90](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/config.h#L71-L90)（C 版 `UHD_API`），结构与 C++ 版完全平行，但更精简。

#### 4.2.4 代码实践

> **实践目标**：理解 `UHD_API` 在「构建库」与「使用库」两种场景下的取值。

操作步骤：

1. 打开 `host/include/uhd/config.hpp` 第 131–142 行，找到三段 `#ifdef` 分支。
2. 假想三种构建命令，写下 `UHD_API` 的展开结果：
   - 静态库构建（`-DUHD_STATIC_LIB`）→ `UHD_API` 展开为 **空**。
   - 动态库自身构建（`-DUHD_DLL_EXPORTS`）→ `UHD_API` 展开为 **导出符号**（如 `__attribute__((visibility("default")))`）。
   - 下游应用包含该头文件 → `UHD_API` 展开为 **导入符号**。
3. 观察现象：在任意一个公共类声明（如 `device.hpp` 的 `class UHD_API device`）前都能看到 `UHD_API`，它就是 `libuhd.so` 符号导出的总开关。

预期结果：能解释为什么「下游程序什么都不用定义，`#include` 进来就能链接到 `libuhd.so` 的符号」。若无法本地构建验证，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么要在头文件里区分 `UHD_API` 和 `UHD_API_HEADER` 两种可见性宏？

> **答案**：`UHD_API` 用于在 `.cpp` 中实现、编进动态库的类/函数；`UHD_API_HEADER` 用于纯头文件（header-only）实现。某些平台（尤其 Windows DLL）对「跨 DLL 边界使用、但实现不在 DLL 内」的类有特殊可见性要求，所以需要两套宏分别标注。

**练习 2**：在 `config.hpp` 里搜索 `UHD_FALLTHROUGH`，它为什么被标记为 `deprecated`？

> **答案**：因为现代 C/C++ 已有标准的 `[[fallthrough]]` 属性，`UHD_FALLTHROUGH` 只是为向后兼容保留的别名（见 [config.hpp:48-52](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/config.hpp#L48-L52)），新代码应直接用 `[[fallthrough]]`。

---

### 4.3 version 模块：版本信息的生成与查询

#### 4.3.1 概念说明

UHD 的版本信息有两副面孔：

1. **编译期宏**（`UHD_VERSION`、`UHD_VERSION_ABI_STRING`）：写死在头文件里，供下游代码用 `#if UHD_VERSION >= …` 做版本兼容判断。
2. **运行期函数**（`get_version_string()` 等）：返回当前运行的 `libuhd` 的版本字符串，可用来在运行时打印或与编译期版本对比。

这里最特别的是源码文件名带 `.in` 后缀——`version.hpp.in` 是 **CMake 模板**，源码树里看不到最终的 `version.hpp`，它是在构建目录里由 `configure_file` 生成的。这正好承接 u1-l3 讲过的「`configure_file` 数据链」。

#### 4.3.2 核心流程

版本号从 CMake 变量流到下游代码的过程：

```text
CMakeCache (UHD_VERSION_MAJOR/API/ABI/PATCH)
        │  configure_file(version.hpp.in → version.hpp)
        ▼
version.hpp 中的宏：
   UHD_VERSION          = @UHD_VERSION_ADDED@      （整数）
   UHD_VERSION_ABI_STRING = "主.API.ABI"           （字符串）
        │  #include <uhd/version.hpp>
        ▼
下游代码：#if UHD_VERSION >= 4090000  ...
```

版本整数的编码公式为：

\[ \text{UHD\_VERSION} = \text{MAJOR}\times 10^{6} + \text{API}\times 10^{4} + \text{ABI}\times 10^{2} + \text{PATCH} \]

例如 UHD 3.10.0.1 对应 \(3\times10^6 + 10\times10^4 + 0\times10^2 + 1 = 3100001\)。这样用一个整数就能完整编码四段版本号，便于 `#if` 比较。

#### 4.3.3 源码精读

版本宏模板与 C++ 查询函数全部在 `version.hpp.in` 中：

- ABI 版本字符串（取前三段，决定 `libuhd.so` 的 SOVERSION 兼容性）：

  [host/include/uhd/version.hpp.in:10-16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/version.hpp.in#L10-L16)
  ——`@UHD_VERSION_MAJOR@.@UHD_VERSION_API@.@UHD_VERSION_ABI@` 三个占位符会被 CMake 替换。注释说明这是「最老兼容版本的 API - ABI 兼容号」。

- 编译期版本整数宏及公式注释：

  [host/include/uhd/version.hpp.in:19-24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/version.hpp.in#L19-L24)
  ——`@UHD_VERSION_ADDED@` 是 CMake 预先算好的整数，直接填入宏定义。

- C++ 运行期查询函数：

  [host/include/uhd/version.hpp.in:34-42](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/version.hpp.in#L34-L42)
  ——`get_version_string()`（点分版本 + 构建信息）、`get_abi_string()`（ABI 兼容串）、`get_component()`（编译时启用的组件串）。

C 语言版版本查询接口在 `version.h` 中：

[host/include/uhd/version.h:18-22](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/version.h#L18-L22)
——注意 C 版的风格差异：返回值是错误码 `uhd_error`，字符串通过「出参缓冲区 + 长度」回填（`char* out, size_t buffer_len`）。这是 C API 的通用约定，所有 C 函数都用「返回错误码 + 出参填结果」模式，对应 C++ 版则直接返回 `std::string`。

`version.h` 顶部还体现了 C 头文件的统一结构：

[host/include/uhd/version.h:14-16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/version.h#L14-L16)
——`extern "C"` 包裹，确保 C++ 代码也能正确链接这些 C 函数符号。

#### 4.3.4 代码实践

> **实践目标**：跟踪 `UHD_VERSION` 从 CMake 到 `version.hpp` 的完整生成链。

操作步骤：

1. 在 `host/include/uhd/version.hpp.in` 第 24 行确认 `UHD_VERSION` 的值来自 `@UHD_VERSION_ADDED@`。
2. 在 `host/CMakeLists.txt` 或 `host/cmake/` 下搜索 `UHD_VERSION_ADDED` 与 `configure_file`，理解这个整数是如何由四段版本号计算并写入的（这是 u1-l3 讲过的版本管理模块）。
3. 确认 `host/include/uhd/CMakeLists.txt` 第 20–23 行的 `configure_file(version.hpp.in → version.hpp)`（见 4.1.3）。
4. 观察现象：源码树里搜不到 `version.hpp`，因为它只存在于构建目录 `${CMAKE_CURRENT_BINARY_DIR}/version.hpp`，并被安装清单（第 35 行）引用。

预期结果：能画出「CMake 变量 → `.in` 模板占位符 → 生成的 `version.hpp` → 下游 `#include`」这条数据流。若本地未构建，无法看到生成的 `version.hpp`，可标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么源码里是 `version.hpp.in` 而不是 `version.hpp`？

> **答案**：因为版本号在构建期才确定（来自 CMake 缓存变量），所以用 `.in` 模板 + `configure_file` 在构建时把 `@UHD_VERSION_xxx@` 占位符替换成真实值，生成最终的 `version.hpp`。源码树里不应手写具体版本号。

**练习 2**：C++ 的 `uhd::get_version_string()` 与 C 的 `uhd_get_version_string()` 在 API 风格上有什么本质区别？

> **答案**：C++ 版直接返回 `std::string`；C 版返回错误码 `uhd_error`，并通过 `char* out, size_t buffer_len` 出参回填字符串。这是 C ABI「不能用异常、不能用 STL 类型」带来的必然设计——所有 C API 都遵循「返回错误码 + 出参」模式。

---

### 4.4 build_info 模块：构建期元数据

#### 4.4.1 概念说明

`build_info.hpp` 回答一个问题：**「我手头这个 `libuhd` 到底是怎么编出来的？」**。它提供一组纯查询函数，返回构建期固化的元数据：用哪个编译器、哪些编译选项、链接了哪个版本的 Boost / libusb / DPDK、启用了哪些组件、安装前缀是什么。

这些信息在排查「同样的代码在我机器上能跑、在你机器上不行」时极其有用，也是命令行工具 `uhd_config_info`（见 u1-l5）输出的数据来源之一。

#### 4.4.2 核心流程

`build_info` 的数据流向：

```text
CMake 检测到的依赖与选项
        │  写入构建期常量（编译进 libuhd）
        ▼
build_info.cpp 中的函数实现  ←  读取这些常量
        │  运行期被调用
        ▼
uhd::build_info::enabled_components() 等
        │
        ▼
uhd_config_info 工具 / 用户程序打印
```

注意：头文件 `build_info.hpp` 只声明接口，实现与「常量从哪来」在 `build_info.cpp` 里（由 CMake `configure_file` 把构建选项注入），本讲只看头文件的接口契约。

#### 4.4.3 源码精读

`build_info.hpp` 把所有查询函数放在嵌套命名空间 `uhd::build_info` 下：

[host/include/uhd/build_info.hpp:12-46](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/build_info.hpp#L12-L46)
——每个函数都返回 `const std::string`，且都标注 `UHD_API`（说明实现编进了动态库）。关键函数含义：

| 函数 | 返回内容 |
| --- | --- |
| `boost_version()` | 构建时所用 Boost 版本 |
| `build_date()` | 构建 GMT 时间 |
| `c_compiler()` / `cxx_compiler()` | C / C++ 编译器 |
| `c_flags()` / `cxx_flags()` | 编译选项 |
| `enabled_components()` | 启用的组件（逗号分隔，对应 u1-l3 的组件机制） |
| `install_prefix()` | CMake 安装前缀 |
| `libusb_version()` / `dpdk_version()` | 传输依赖版本 |
| `pkg_data_dir()` | 构建期 `PKG_DATA_DIR` 值 |

这些函数都是「无参数、返回常量字符串」，属于最简单的查询接口，是阅读 UHD 头文件的良好起点。

#### 4.4.4 代码实践

> **实践目标**：用 `build_info` 接口写一段最小的 C++ 版本/构建信息打印程序。

操作步骤：

1. 新建一个示例代码（**非项目原有代码，标注为示例代码**）：

   ```cpp
   // 示例代码：打印当前 libuhd 的版本与构建信息
   #include <uhd/version.hpp>      // get_version_string()
   #include <uhd/build_info.hpp>   // uhd::build_info::*
   #include <iostream>

   int main() {
       std::cout << "Version: " << uhd::get_version_string() << "\n";
       std::cout << "ABI:     " << uhd::get_abi_string() << "\n";
       std::cout << "Boost:   " << uhd::build_info::boost_version() << "\n";
       std::cout << "Comps:   " << uhd::build_info::enabled_components() << "\n";
       std::cout << "CXX:     " << uhd::build_info::cxx_compiler() << "\n";
       return 0;
   }
   ```

2. （可选）若已构建 UHD，编译并链接 `UHD::uhd` 后运行；若未构建，仅阅读接口，标注「待本地验证」。
3. 观察现象：`enabled_components()` 的输出应与 `uhd_config_info --enabled-components`（u1-l5）一致。

预期结果：理解 `version.hpp` 与 `build_info.hpp` 是「运行期自描述」的两个入口，程序无需额外配置就能打印自己链接的库的来历。

#### 4.4.5 小练习与答案

**练习 1**：`build_info::enabled_components()` 返回的字符串，和 `CMakeLists.txt` 里的什么机制直接相关？

> **答案**：与 u1-l3 讲的 `LIBUHD_REGISTER_COMPONENT` 组件注册机制相关。CMake 构建时把所有「已启用组件」名拼接成字符串注入 `build_info`，所以运行期查到的组件列表就是构建期 `--enabled-components` 的结果。

**练习 2**：为什么 `build_info.hpp` 的函数都返回 `const std::string` 而不是 `std::string_view`？

> **答案**：UHD 公共 API 长期保持对老 C++ 标准和 ABI 的稳定兼容；`std::string_view` 是 C++17 才引入的类型，跨 DLL 边界返回它对 ABI 稳定性更脆弱。返回 `const std::string` 是更保守、兼容性更好的选择。

---

### 4.5 C++ 与 C 两套 API 的映射

> 本节不是独立的最小模块，而是把前四节建立的认知串联成一个关键结论。它对应学习目标里的「建立 C++ 与 C 两套 API 的映射概念」。

UHD 同时提供 **C++ API**（主力，功能最全）和 **C API**（可选组件，由 `ENABLE_C_API` 开关控制，见 4.1.3 的安装清单）。两套 API 在头文件层面是平行对应的：

| C++ 头文件 | C 头文件 | 关系 |
| --- | --- | --- |
| `config.hpp` | `config.h` | 跨平台宏，C++ 版功能更全 |
| `version.hpp`（由 `.in` 生成） | `version.h` | 版本查询，风格不同（返回 `std::string` vs 错误码+出参） |
| `exception.hpp`（C++ 异常类） | `error.h`（`uhd_error` 错误码） | 错误处理：C++ 用异常，C 用错误码 |
| `device.hpp` / `usrp/multi_usrp.hpp` | `usrp/usrp.h` | 设备/USRP 控制，C 版封装了 C++ `multi_usrp` |
| `types/*.hpp`（device_addr、metadata、ranges…） | `types/*.h` | 基础类型的 C 绑定 |

最关键的差别在 **「伞式头文件」**：

- **C API 有伞式头 `host/include/uhd.h`**，一次性 include 全部 C 头文件：

  [host/include/uhd.h:11-31](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd.h#L11-L31)
  ——可以看到它依次引入 `config.h`、`error.h`、`version.h`、`types/*.h`、`usrp/usrp.h`、`usrp_clock/usrp_clock.h`、`utils/log.h` 等。C 程序只需 `#include <uhd.h>` 一行。

- **C++ API 没有伞式头**（不存在 `uhd.hpp`）。C++ 程序必须按需 include，例如 `#include <uhd/usrp/multi_usrp.hpp>`、`#include <uhd/stream.hpp>`。

这套映射也是 u5-l1（C API 绑定）的预习：C API 本质是 `usrp_c.cpp` 等封装层对 C++ `multi_usrp` 的逐函数包装，把异常翻译成错误码、把 `std::string` 翻译成出参缓冲区。

## 5. 综合实践

**任务：画出 UHD 公共头文件的「门牌号地图」，并用一张表验证 C++ ↔ C 的映射关系。**

1. **目录普查**：在 `host/include/uhd/` 下，列出所有顶层文件和全部子目录，标注每个的子系统归属（参考 4.1.3 的表格）。
2. **配对练习**：找出至少 3 对「C++ 头文件 ↔ C 头文件」，填入下表并写出风格差异：

   | C++ 头 | C 头 | 风格差异 |
   | --- | --- | --- |
   | `version.hpp` | `version.h` | 返回 `std::string` vs 错误码 + `char*` 出参 |
   | `config.hpp` | `config.h` | C++ 版多 `UHD_FALLTHROUGH`、`UHD_FUNCTION` 等 |
   | `exception.hpp` | `error.h` | C++ 异常 vs `uhd_error` 错误码 |
   | `types/metadata.hpp` | `types/metadata.h` | 结构体用 C++ 类 vs 纯 C struct |

3. **追溯一个宏**：从 `device.hpp` 里的 `class UHD_API device` 出发，回溯 `UHD_API` → `config.hpp` 第 131–142 行 → `UHD_DLL_EXPORTS` / `UHD_STATIC_LIB`，解释同一份 `device.hpp` 如何既服务库自身构建又服务下游。
4. **对照构建链**：确认 `version.hpp` 不在源码树而在构建目录（`version.hpp.in` + `configure_file`），并用 `uhd::get_version_string()` 说明运行期如何再次查到这个版本号。

完成本任务后，你应能在不打开实现文件的前提下，仅凭头文件路径就判断它属于哪个子系统、是 C++ 还是 C、由谁安装。

## 6. 本讲小结

- `host/include/uhd/` 是 UHD 全部公共头文件的集合，布局为「顶层总入口（`device.hpp`、`stream.hpp`、`convert.hpp` 等）+ 子目录（types/usrp/rfnoc/transport/cal/experts/features/utils）」。
- 头文件是否公开由 `host/include/uhd/CMakeLists.txt` 的 `UHD_INSTALL` 决定，C 头文件还额外受 `ENABLE_C_API` 开关控制。
- `config.hpp` / `config.h` 是跨平台编译宏翻译层，核心是 `UHD_API` 符号可见性宏，它在「库构建（导出）」与「下游使用（导入）」间切换。
- `version.hpp.in` 是 CMake 模板，构建期由 `configure_file` 注入版本号生成 `version.hpp`；`UHD_VERSION` 整数按 `MAJOR×1e6+API×1e4+ABI×1e2+PATCH` 编码。
- `build_info.hpp` 提供构建期元数据（编译器、依赖版本、启用组件）的运行期查询，是 `uhd_config_info` 的数据来源之一。
- UHD 同时维护 C++ API（无伞式头，按需 include）与 C API（有伞式头 `uhd.h`，可选组件），二者头文件平行对应、风格不同（异常 vs 错误码、`std::string` vs 出参缓冲）。

## 7. 下一步学习建议

本讲已建立「头文件地图」。后续建议：

- **u1-l5 命令行工具导览**：看 `uhd_config_info` 如何把 `build_info`、`version` 这些头文件接口的输出呈现给命令行用户，是本讲 4.4 的直接延伸。
- **u1-l6 第一个示例**：进入 `rx_samples_to_file.cpp`，第一次写出会 `#include <uhd/...>` 的完整程序。
- **u2-l1 设备工厂**：深入本讲只点到为止的 `device.hpp`，理解 `register_device` / `find` / `make` 的发现-制造链路。
- 建议继续精读的头文件：`device.hpp`（设备根）、`stream.hpp`（流式 API）、`types/device_addr.hpp`（设备地址），它们是第二单元的主线起点。
