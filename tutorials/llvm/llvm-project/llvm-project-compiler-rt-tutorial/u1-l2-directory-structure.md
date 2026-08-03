# 目录结构与子库全景

## 1. 本讲目标

上一讲我们建立了「compiler-rt 是一组运行时库、替代 libgcc」的整体印象。本讲带你真正「打开仓库」，把顶层每一个目录的职责弄清楚，并把 `lib/` 下约 28 个子库按用途分类，建立一张可以长期使用的心智地图。

学完本讲，你应当能够：

- 说出 compiler-rt 顶层每个目录（`lib/`、`include/`、`test/`、`cmake/`、`tools/`、`docs/`、`unittests/`、`utils/`、`www/`）分别存放什么。
- 把 `lib/` 下的每一个子库归入正确的功能类别（builtins、sanitizer 公共设施、各类 sanitizer、分配器与加固、剖析与覆盖、模糊测试、函数追踪、JIT 运行时等）。
- 理解「一个运行时库 = `lib/` 实现 + `include/` 公共头 + `test/` 测试套件」的三位一体对应关系，并能解释其中的少数例外。
- 看懂顶层 `CMakeLists.txt` 用 `COMPILER_RT_BUILD_*` 开关控制「构建哪些库」的基本机制。

## 2. 前置知识

在阅读本讲前，你需要了解以下基础概念（不熟悉也没关系，下面会展开）：

- **运行时库（runtime library）**：程序运行期间被调用、但通常不由程序员主动 `#include` 或调用的库。例如 32 位机器上做 64 位除法时，编译器会偷偷插入对 `__udivdi3` 的调用，这个函数就由运行时库提供。
- **CMake**：compiler-rt 使用的构建系统。配置阶段会扫描源码目录、探测平台能力，决定要生成哪些库文件。
- **子目录约定**：大型 C/C++ 项目常把「实现」「对外头文件」「测试」分目录存放。compiler-rt 遵循这一约定，所以我们会在 `lib/`、`include/`、`test/` 三个地方看到相似的名字。
- **sanitizer（消毒器/检测器）**：一类在程序运行时检查内存错误、数据竞争、未定义行为等问题的工具，例如 ASan（地址消毒器）、TSan（线程消毒器）、UBSan（未定义行为消毒器）。它们各自的运行时代码就是 compiler-rt 的一个子库。

如果你还没读过上一讲《项目总览与定位》，建议先读一遍，了解 compiler-rt 在 LLVM 工具链中的位置与「替代 libgcc」的定位，再进入本讲。

## 3. 本讲源码地图

本讲涉及的目录与文件如下：

| 路径 | 作用 |
| --- | --- |
| `CMakeLists.txt` | 顶层构建入口，定义 `COMPILER_RT_BUILD_*` 组件开关，并按顺序 `add_subdirectory` 各顶层目录。 |
| `lib/CMakeLists.txt` | `lib/` 的构建入口，根据开关条件性地把每个子库加入构建。 |
| `lib/asan/README.txt` | ASan 子库自带说明，给出了「实现 + 依赖库 + 测试」的典型结构，可作为理解其它子库的样板。 |
| `include/CMakeLists.txt` | 公共头文件的清单与安装规则，体现「每个 sanitizer 一份 `*_interface.h`」的对应关系。 |
| `test/lit.common.cfg.py` | 所有 lit 测试套件共享的公共配置，是 `test/` 目录组织的关键。 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. 顶层目录职责
2. `lib/` 子库分类
3. `include/` 与 `test/` 的对应关系

### 4.1 顶层目录职责

#### 4.1.1 概念说明

compiler-rt 的仓库根目录并不只有源码，而是按「职责」切分成了若干顶层目录。理解每个目录放什么，是阅读整个项目的第一步。常见的顶层目录有：`lib/`（库实现）、`include/`（对外头文件）、`test/`（功能测试）、`cmake/`（构建模块）、`tools/`（辅助可执行工具）、`docs/`（文档）、`unittests/`（单元测试）、`utils/`（开发期脚本）、`www/`（项目网页）。

这种切分的好处是：当你想找「某个 sanitizer 的实现」就去 `lib/`，想找「它对程序暴露的接口」就去 `include/`，想找「它的测试用例」就去 `test/`，三者互不干扰、各司其职。

#### 4.1.2 核心流程

顶层 `CMakeLists.txt` 在配置阶段会按固定顺序把各顶层目录加入构建。简化后的流程是：

```text
读取顶层 CMakeLists.txt
  ├─ 定义所有 COMPILER_RT_BUILD_* 组件开关（默认值）
  ├─ include(config-ix)         探测平台能力，决定能构建哪些库
  ├─ add_subdirectory(include)  处理公共头文件
  ├─ add_subdirectory(lib)      根据 switch 构建各运行时库
  ├─ add_subdirectory(unittests) 构建 C++ 单元测试（若开启测试）
  ├─ add_subdirectory(test)     注册 lit 功能测试套件（若开启测试）
  └─ add_subdirectory(tools)    构建辅助工具
```

注意 `unittests/` 和 `test/` 只在开启测试时才加入构建；而 `docs/`、`utils/`、`www/` 等目录并不通过 `add_subdirectory` 进入构建，它们是纯文档/脚本/网页资源。

#### 4.1.3 源码精读

顶层 `CMakeLists.txt` 末尾依次把各目录加入构建，下面这五行决定了「哪些顶层目录真正参与编译」：

- 顶层目录加入构建的顺序：[CMakeLists.txt:L841-L936](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L841-L936) —— 依次 `add_subdirectory(include)`、`add_subdirectory(lib)`，并在 `COMPILER_RT_INCLUDE_TESTS` 为真时加入 `unittests` 与 `test`，最后加入 `tools`。这一段解释了为什么 `docs/`、`utils/`、`www/` 不出现在构建产物里——它们没有 `add_subdirectory`。

下面这张表把各顶层目录的职责逐一列清（基于实际仓库内容整理）：

| 顶层目录 | 职责 | 关键内容举例 |
| --- | --- | --- |
| `lib/` | 所有运行时库的实现源码（约 28 个子库） | `lib/asan/`、`lib/builtins/`、`lib/profile/` 等 |
| `include/` | 对外公共头文件，供程序与编译器使用 | `include/sanitizer/asan_interface.h` 等 |
| `test/` | 基于 lit 的功能测试套件，每个库一个子目录 | `test/asan/`、`test/builtins/` 等 |
| `cmake/` | 构建用的 CMake 模块与平台探测脚本 | `config-ix.cmake`、`Modules/AddCompilerRT.cmake` |
| `tools/` | 辅助可执行工具 | `tools/gwp_asan/`（GWP-ASan 辅助工具） |
| `docs/` | 项目文档 | `BuildingCompilerRT.md`、`TestingGuide.md` |
| `unittests/` | C++（GoogleTest）单元测试的公共配置 | `lit.common.unit.cfg.py` |
| `utils/` | 开发维护用的脚本 | `generate_netbsd_syscalls.awk` |
| `www/` | 项目网页资源 | `index.html`、`content.css` |

其中 `lib/asan/README.txt` 用几行文字给出了一个 sanitizer 子库的典型组成，非常适合作为样板来理解其它子库：

- ASan 子库自带说明：[lib/asan/README.txt:L1-L18](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/asan/README.txt#L1-L18) —— 它说明本目录放 ASan 运行时源码（`asan_*.{cc,h}`），并且依赖 `lib/interception/`（函数拦截）与 `lib/sanitizer_common/`（各 sanitizer 共享代码）。这揭示了 sanitizer 之间并非彼此孤立，而是层层依赖。

#### 4.1.4 代码实践

**实践目标**：亲手确认各顶层目录的内容，验证上表的描述。

**操作步骤**：

1. 在 compiler-rt 根目录执行 `ls -1` 列出所有顶层条目。
2. 对 `cmake/`、`docs/`、`tools/`、`utils/`、`www/` 五个目录分别执行 `ls -1 <目录>`，查看里面到底有哪些文件。
3. 打开 `CMakeLists.txt` 的第 841 行附近，确认 `add_subdirectory` 只覆盖了 `include`、`lib`、`unittests`、`test`、`tools`，而没有 `docs`/`utils`/`www`。

**需要观察的现象**：

- `docs/` 里只有三个 `.md` 文件；`tools/` 里只有一个 `gwp_asan/` 子目录；`utils/` 里是两个 `.awk` 脚本；`www/` 里是网页文件。
- `cmake/` 下既有 `config-ix.cmake` 等平台探测脚本，也有一个 `Modules/` 子目录存放可复用的 CMake 宏。

**预期结果**：你会直观看到「源码在 `lib/`、头文件在 `include/`、测试在 `test/`、构建逻辑在 `cmake/`、其余是文档与辅助资源」的清晰分工。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `docs/` 目录不会出现在构建产物（如 `.a`/`.so` 文件）里？

**参考答案**：因为顶层 `CMakeLists.txt` 没有对 `docs/` 调用 `add_subdirectory`，CMake 不会把它当作构建目标处理；它只是纯文档资源，供人阅读。

**练习 2**：根据 `lib/asan/README.txt`，ASan 运行时除了自身源码还依赖哪两个库？这说明 sanitizer 之间是什么关系？

**参考答案**：依赖 `lib/interception/`（函数拦截机制）与 `lib/sanitizer_common/`（sanitizer 共享代码）。这说明 sanitizer 并非各自为政，而是建立在公共地基之上的——这正是下一节分类时「sanitizer 公共设施」单独成类的原因。

### 4.2 lib/ 子库分类

#### 4.2.1 概念说明

`lib/` 是 compiler-rt 的核心，包含约 28 个子库。它们体量、用途差异极大：有的（如 `builtins`）提供最底层的算术帮手函数，有的（如 `asan`）是完整的错误检测运行时，还有的（如 `orc`）为 JIT 编译提供执行支持。如果不分类，面对 28 个名字会无从下手。

分类的核心思路是按「解决什么问题」分组：先分出最基础的 builtins 与 sanitizer 公共设施，再把各类 sanitizer 按检测对象（内存/线程/未定义行为）归类，最后是分配器与加固、剖析与覆盖、模糊测试、函数追踪、JIT 运行时等「工具型」运行时。

#### 4.2.2 核心流程

`lib/CMakeLists.txt` 负责把各子库有条件地加入构建。它先把公共设施（`sanitizer_common`）和 builtins 单独处理，再用一个 `compiler_rt_build_runtime` 辅助函数按开关逐个构建其余库：

```text
lib/CMakeLists.txt
  ├─ 若开启 sanitizer/xray/memprof/ctx_profile 之一 → 加入 sanitizer_common（公共地基）
  ├─ 若开启 builtins        → 加入 builtins
  ├─ 若开启 sanitizers/memprof → 加入 interception（拦截公共设施）
  ├─ 若开启 sanitizers      → 加入 sanitizer_ignorelists、stats、lsan、ubsan
  │                          并循环加入各具体 sanitizer
  ├─ 若开启 profile         → 加入 profile
  ├─ 若开启 ctx_profile     → 加入 ctx_profile
  ├─ 若开启 xray            → 加入 xray
  ├─ 若开启 libfuzzer       → 加入 fuzzer
  ├─ 若开启 memprof         → 加入 memprof
  └─ 若开启 orc             → 加入 orc
```

关键点是：**「开关」与「子库」并非一一对应**。例如 `sanitizer_common` 没有独立的 `COMPILER_RT_BUILD_SANITIZER_COMMON` 开关，而是由「是否构建任一 sanitizer/xray/memprof/ctx_profile」推导而来；`lsan`、`ubsan` 即便对应的检测器未被单独启用，也会被构建（因为它们含有被其它运行时复用的公共部分）。

#### 4.2.3 源码精读

顶层 `CMakeLists.txt` 用一连串 `option(...)` 定义了组件开关，这是控制「构建哪些库」的总开关：

- 组件开关定义：[CMakeLists.txt:L79-L100](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L79-L100) —— 这里依次定义了 `COMPILER_RT_BUILD_BUILTINS`、`COMPILER_RT_BUILD_SANITIZERS`、`COMPILER_RT_BUILD_XRAY`、`COMPILER_RT_BUILD_LIBFUZZER`、`COMPILER_RT_BUILD_PROFILE`、`COMPILER_RT_BUILD_CTX_PROFILE`、`COMPILER_RT_BUILD_MEMPROF`、`COMPILER_RT_BUILD_ORC`、`COMPILER_RT_BUILD_GWP_ASAN` 等，默认大多为 `ON`。修改这些开关即可裁剪构建范围。

`lib/CMakeLists.txt` 则把这些开关翻译成「加入哪个子目录」。最关键的几段如下：

- 公共地基的推导：[lib/CMakeLists.txt:L11-L18](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/CMakeLists.txt#L11-L18) —— `sanitizer_common` 仅在「构建 sanitizer 或 xray 或 memprof 或 ctx_profile」且平台支持时才加入；`builtins` 独立由 `COMPILER_RT_BUILD_BUILTINS` 控制。这段代码印证了 sanitizer_common 是「底座」。

- 辅助构建函数与各运行时的开关映射：[lib/CMakeLists.txt:L34-L79](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/CMakeLists.txt#L34-L79) —— `interception` 在 `COMPILER_RT_BUILD_SANITIZERS OR COMPILER_RT_BUILD_MEMPROF` 时加入；`sanitizer_ignorelists`、`stats`、`lsan`、`ubsan` 在 `COMPILER_RT_BUILD_SANITIZERS` 下统一加入；`profile`、`ctx_profile`、`xray`、`fuzzer`、`memprof`、`orc` 各自对应一个开关。注意 `tsan` 与 `scudo_standalone` 还会被 `compiler_rt_build_runtime` 特殊处理（分别指向 `tsan/dd` 与 `scudo/standalone`）。

基于上述逻辑与各子库的实际用途，下表把全部 28 个子库归入功能类别：

| 类别 | 子库 | 说明 |
| --- | --- | --- |
| builtins | `builtins` | libgcc 替代品，提供整数/浮点等低级运算帮手 |
| sanitizer 公共设施 | `sanitizer_common`、`interception`、`sanitizer_ignorelists`、`stats` | 所有 sanitizer 共享的底座、拦截机制、忽略名单与统计 |
| 内存相关 sanitizer | `asan`、`asan_abi`、`hwasan`、`msan`、`lsan`、`memprof` | 检测地址错误、未初始化读、泄漏、内存剖析 |
| 线程 sanitizer | `tsan` | 检测数据竞争（内含独立的死锁检测器 `tsan/dd`） |
| 未定义行为/类型检测 | `ubsan`、`ubsan_minimal`、`cfi`、`tysan` | 检测 UB、控制流完整性、类型混淆 |
| 分配器与加固 | `scudo`、`gwp_asan`、`safestack` | 安全分配器、采样守卫分配、栈隔离 |
| 剖析与覆盖 | `profile`、`ctx_profile` | 插桩计数采集、上下文敏感剖析 |
| 模糊测试 | `fuzzer` | libFuzzer 覆盖率导向模糊测试引擎 |
| 函数追踪 | `xray` | 低开销函数调用追踪 |
| JIT 运行时 | `orc` | 为 LLVM ORC JIT 提供执行端支持 |
| 其它专用运行时 | `dfsan`、`nsan`、`rtsan`、`BlocksRuntime` | 数据流追踪、数值精度、实时性检测、Apple Blocks 语法支持 |

> 说明：上表中「其它专用运行时」的四个子库并不在实践任务给定的 10 个分组里——它们是更专用或较新的运行时。`dfsan`（数据流）、`nsan`（数值）、`rtsan`（实时性）本质上也属 sanitizer 家族，但检测对象比较特殊；`BlocksRuntime` 则与 Apple 的 Blocks 语法相关，独立而精简。把它们单列是为了不与主流 sanitizer 混淆。

#### 4.2.4 代码实践

**实践目标**：把 `lib/` 下所有子库按 10 个功能分组归类，画出一张分类图。

**操作步骤**：

1. 在 compiler-rt 根目录执行 `ls -1 lib/` 列出全部子库（应得到约 28 项，含一个 `CMakeLists.txt`）。
2. 按下列 10 个分组，把每个子库归位：builtins、sanitizer 公共设施、内存 sanitizer、线程 sanitizer、未定义行为检测、分配器与加固、剖析与覆盖、模糊测试、函数追踪、JIT 运行时。
3. 对归不进这 10 组的子库（如 `dfsan`、`nsan`、`rtsan`、`BlocksRuntime`），单独开一个「其它专用运行时」分组。
4. 用任意画图工具（或纯文本缩进）画出树状分类图。

**需要观察的现象**：

- `builtins` 是唯一一个不属于 sanitizer 体系的基础库。
- `sanitizer_common` 与 `interception` 服务于几乎所有 sanitizer，体量大且被频繁依赖。
- `scudo` 目录下其实是一个 `standalone/` 子目录（真正的实现），这与 `lib/CMakeLists.txt` 里 `scudo/standalone` 的特殊处理对应。

**预期结果**：得到一张覆盖全部 28 个子库的分类图，主流 sanitizer（内存/线程/UB）占据主体，公共设施与分配器作为支撑层存在。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sanitizer_common` 没有自己独立的 `COMPILER_RT_BUILD_*` 开关？

**参考答案**：因为它是「被需要时才构建」的公共底座。`lib/CMakeLists.txt` 用「是否构建 sanitizer 或 xray 或 memprof 或 ctx_profile」来推导是否需要 `sanitizer_common`——只要有任一上层运行时需要它，它就会被加入，因此不需要单独开关。

**练习 2**：`lsan` 和 `ubsan` 在 `COMPILER_RT_BUILD_SANITIZERS=ON` 时一定会被构建，即便用户没单独启用泄漏或 UB 检测。为什么？

**参考答案**：因为这两个目录里含有被其它运行时复用的公共部分（如 `lsan/lsan_common` 被 ASan 内嵌用于泄漏检测，`ubsan` 含 `RTUbsan` 公共代码）。所以它们作为「公共依赖」无条件构建，注释中也明确写了这一点。

**练习 3**：`scudo` 与其它子库在目录结构上有什么不同？构建时如何体现？

**参考答案**：`scudo` 下有一个 `standalone/` 子目录存放真正的实现（Scudo 独立分配器）。`lib/CMakeLists.txt` 的 `compiler_rt_build_runtime` 函数对 `scudo_standalone` 做了特殊处理，直接 `add_subdirectory(scudo/standalone)` 而不是 `scudo`。

### 4.3 include/ 与 test/ 的对应关系

#### 4.3.1 概念说明

一个成熟的运行时库通常由三部分组成：**实现**（`lib/`）、**对外接口**（`include/`）、**测试**（`test/`）。compiler-rt 严格遵循这一约定，所以你会发现 `lib/`、`include/`、`test/` 三个目录下出现大量相似的名字——它们是同一个运行时库的三个侧面。

- `include/` 存放「公共头文件」，即运行时库暴露给程序和编译器的接口。例如 `include/sanitizer/asan_interface.h` 声明了 ASan 运行时供用户代码调用的函数（如手动解毒内存的 `__asan_poison_memory_region`）。
- `test/` 存放「功能测试套件」，基于 LLVM 的 lit 测试框架，用真实的 `.c`/`.cpp` 程序验证运行时行为。
- 每个 `test/` 子目录都有自己的 `lit.cfg.py`，而 `test/lit.common.cfg.py` 提供所有套件共享的公共规则。

#### 4.3.2 核心流程

三者对应关系可以用下面这个「三位一体」模型概括：

```text
某个运行时 X（例如 asan）
  ├─ lib/X/             实现源码 → 编译成 libclang_rt.X*.a / .so
  ├─ include/<组>/X_interface.h  对外公共头（程序与编译器据此调用运行时）
  └─ test/X/            lit 功能测试（编译并运行真实小程序验证行为）
```

公共头文件并不是无条件安装，而是按开关分组：开启 sanitizers 时安装一组 sanitizer 头；开启 xray/orc/profile 时分别安装各自头。测试套件同理，`test/` 下大多数子目录与 `lib/` 一一对应，但存在少数例外。

#### 4.3.3 源码精读

`include/CMakeLists.txt` 清楚地展示了「公共头按开关分组、每个 sanitizer 一份接口头」的对应关系：

- sanitizer 与 fuzzer 头清单：[include/CMakeLists.txt:L1-L24](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/include/CMakeLists.txt#L1-L24) —— 在 `COMPILER_RT_BUILD_SANITIZERS` 下定义 `SANITIZER_HEADERS`，列出 `sanitizer/asan_interface.h`、`msan_interface.h`、`tsan_interface.h`、`ubsan_interface.h`、`dfsan_interface.h` 等每个 sanitizer 一份接口头，外加 `FUZZER_HEADERS`（`fuzzer/FuzzedDataProvider.h`）。可以看到 `include/sanitizer/` 子目录正是「每个 sanitizer 一个 `*_interface.h`」。

- 其余运行时的头与汇总：[include/CMakeLists.txt:L40-L67](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/include/CMakeLists.txt#L40-L67) —— `XRAY_HEADERS`、`ORC_HEADERS`、`PROFILE_HEADERS` 分别由各自开关控制，最后汇成 `COMPILER_RT_HEADERS` 并复制到构建树。这说明 `include/` 的子目录（`sanitizer/`、`fuzzer/`、`xray/`、`orc_rt/`、`profile/`）与功能分类基本对应。

`test/lit.common.cfg.py` 是所有测试套件的公共底座，它负责定位「编译器的运行时库目录」，让测试能链接到正确的运行时：

- 测试公共配置定位运行时目录：[test/lit.common.cfg.py:L26-L65](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L26-L65) —— 函数 `find_compiler_libdir` 通过调用 `clang -print-runtime-dir`（旧版 Apple 平台退化为 `-print-file-name=lib`）来找到 compiler-rt 库所在目录。这解释了测试是如何「找到刚刚构建的运行时库」的——本讲的后续讲义会专门讲测试基础设施。

`test/` 与 `lib/` 的目录大多一一对应，但有两类值得注意的例外（可用 `ls` 自行核对）：

| 情况 | 目录 | 含义 |
| --- | --- | --- |
| 只在 `test/` 出现 | `test/metadata`、`test/shadowcallstack` | 测试的是 Clang 编译器特性（元数据、影子调用栈），而非某个 `lib/` 子库 |
| 只在 `lib/` 出现 | `lib/sanitizer_ignorelists`、`lib/stats` | 这两个是辅助/公共组件，其测试归入其它套件或不单独建测试目录 |

#### 4.3.4 代码实践

**实践目标**：验证「一个运行时库 = lib 实现 + include 头 + test 套件」的对应关系，并找出其中的例外。

**操作步骤**：

1. 执行 `ls -1 include/`，确认公共头分为 `fuzzer/`、`orc_rt/`、`profile/`、`sanitizer/`、`xray/` 五组。
2. 执行 `ls -1 include/sanitizer/ | grep _interface.h`，观察是否「每个 sanitizer 一份 `*_interface.h`」。
3. 分别执行 `ls -1 lib/` 与 `ls -1 test/`（过滤掉 `CMakeLists.txt`、`*.py`、`*.in`），用 `comm` 或肉眼对比，找出：哪些目录只在 `test/` 出现，哪些只在 `lib/` 出现。
4. 打开任一 `test/` 子目录（如 `test/asan/`），确认里面有一个 `lit.cfg.py`，并大致浏览其中的测试用例文件。

**需要观察的现象**：

- `include/sanitizer/` 下确实有 `asan_interface.h`、`msan_interface.h`、`tsan_interface.h`、`ubsan_interface.h` 等，几乎每个 sanitizer 一份。
- 对比 `lib/` 与 `test/`：绝大多数目录同名（如 `asan`、`builtins`、`xray`、`fuzzer`、`profile`、`orc`）；只有 `metadata`、`shadowcallstack` 只在 `test/`，`sanitizer_ignorelists`、`stats` 只在 `lib/`。

**预期结果**：建立「三位一体」对应直觉，并理解少数例外源于「某些目录测试的是编译器特性」或「某些库是公共辅助组件」。

#### 4.3.5 小练习与答案

**练习 1**：`include/sanitizer/` 下的头文件命名有什么规律？为什么这样命名？

**参考答案**：规律是 `<sanitizer>_interface.h`（如 `asan_interface.h`、`msan_interface.h`）。因为每个 sanitizer 都需要把自己「可供程序或编译器调用的接口」单独声明出来——例如让用户手动调用 `__asan_poison_memory_region`，或让编译器生成的检查代码调用对应入口。一份接口头对应一个 sanitizer，便于按需暴露。

**练习 2**：为什么 `test/shadowcallstack` 在 `test/` 下却没有对应的 `lib/shadowcallstack`？

**参考答案**：因为影子调用栈（Shadow Call Stack）是 Clang 编译器侧实现的安全特性，运行时主要靠编译器生成的代码与少量已有运行时支持，并不需要独立的 `lib/` 子库。`test/` 下的目录验证的是「编译器生成的代码在运行时的行为」，所以会出现「有测试、无独立 lib」的情况。

**练习 3**：`test/lit.common.cfg.py` 中的 `find_compiler_libdir` 解决了什么问题？

**参考答案**：它解决「测试程序要链接到哪个 compiler-rt 库」的问题。通过询问 `clang -print-runtime-dir`，测试框架能找到当前这套 clang 对应的运行时库目录（无论是随编译器安装的，还是刚刚本地构建的），从而保证测试链接到正确版本的运行时。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务：**为 compiler-rt 绘制一张「目录总览 + 子库分类 + 三位一体」知识地图**。

具体做法：

1. **顶层骨架**：画一个根节点 `compiler-rt/`，向外连出 9 个顶层目录节点（`lib/`、`include/`、`test/`、`cmake/`、`tools/`、`docs/`、`unittests/`、`utils/`、`www/`），每个节点旁用一句话标注职责。
2. **lib 展开**：把 `lib/` 节点展开为 11 个功能类别（builtins、sanitizer 公共设施、内存 sanitizer、线程 sanitizer、UB/类型检测、分配器与加固、剖析与覆盖、模糊测试、函数追踪、JIT 运行时、其它专用），再把约 28 个子库挂到对应类别下。
3. **三位一体连线**：挑选一个具体运行时（如 `asan`），用三条线分别指向 `lib/asan/`（实现）、`include/sanitizer/asan_interface.h`（接口）、`test/asan/`（测试），并标注「同类一一对应，但有 `metadata`/`shadowcallstack` 等例外」。
4. **构建开关标注**：在 `lib/` 旁注明「由顶层 `COMPILER_RT_BUILD_*` 开关控制，详见 `lib/CMakeLists.txt`」。

完成后，这张图就是你后续阅读具体子库源码时的「导航图」。建议把它保存下来，随着学习深入不断补充细节。

## 6. 本讲小结

- compiler-rt 顶层目录按职责清晰分工：源码在 `lib/`、公共头在 `include/`、功能测试在 `test/`、构建逻辑在 `cmake/`，其余 `docs/`/`utils/`/`www/` 等为文档与辅助资源，不参与编译。
- `lib/` 下约 28 个子库可归为 11 类：builtins、sanitizer 公共设施、内存 sanitizer、线程 sanitizer、UB/类型检测、分配器与加固、剖析与覆盖、模糊测试、函数追踪、JIT 运行时，以及少量其它专用运行时。
- 「构建哪些库」由顶层 `CMakeLists.txt` 的 `COMPILER_RT_BUILD_*` 开关控制，`lib/CMakeLists.txt` 把开关翻译成 `add_subdirectory`；注意 `sanitizer_common` 是被推导构建的公共底座，`lsan`/`ubsan` 含被复用的公共代码。
- 一个运行时库通常呈现「三位一体」：`lib/<X>/` 实现 + `include/sanitizer/<X>_interface.h` 接口 + `test/<X>/` 测试套件；`test/lit.common.cfg.py` 为所有套件提供公共规则并定位运行时库目录。
- 少数目录打破一一对应：`test/metadata`、`test/shadowcallstack` 测试的是编译器特性（无独立 lib），`lib/sanitizer_ignorelists`、`lib/stats` 是公共辅助组件。

## 7. 下一步学习建议

本讲给出了「在哪里」的地图，接下来的学习可以按两条线推进：

- **构建与测试线**：先读 `CMakeLists.txt` 与 `cmake/config-ix.cmake`，理解平台探测与构建配置；再读 `test/lit.common.cfg.py` 与某个 `lit.cfg.py`，掌握如何运行测试。对应后续讲义「构建系统入门」与「测试基础设施」。
- **源码深入线**：从最独立、最基础的 `lib/builtins/` 入手（它不依赖 sanitizer 体系，是理解「编译器插入的帮手函数」的最佳起点），再逐步进入 `sanitizer_common` 与具体 sanitizer。对应后续讲义「builtins 概览」与「sanitizer_common 概览」。

建议在进入下一篇前，确保自己能凭记忆说出每个顶层目录的职责，并把 `lib/` 的分类图大致画出来——这是后续所有讲义的基础。
