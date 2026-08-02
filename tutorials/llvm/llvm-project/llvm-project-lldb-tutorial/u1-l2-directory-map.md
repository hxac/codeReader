# 目录结构与模块地图

## 1. 本讲目标

LLDB 是一个体量巨大、组件繁多的代码库。如果你一开始就跳进某个具体类去读源码，很容易「只见树木不见森林」。本讲的目标是给你一张**整个代码库的心智地图**，学完后你应当能够：

- 说出 `lldb/` 顶层每个目录（`source`、`include`、`tools`、`bindings`、`docs`、`test`、`examples`、`cmake`）的职责。
- 记住 `source/` 下每个子模块（API、Core、Interpreter、Commands、Target、Symbol、Expression 等）负责什么。
- 理解 `include/lldb/`（公共头）与 `source/`（内部实现）之间的**公私分层**关系。
- 识别 `tools/` 下各个可执行入口（`lldb`、`lldb-server`、`lldb-dap` 等）的用途与差异。
- 了解 `plugins/` 的分类方式，以及 `bindings/` 的作用。

本讲承接 [u1-l1 LLDB 项目总览与定位](u1-l1-project-overview.md) 中已经建立的「LLDB = 组件化调试器 + 公共 SB API」认知，把那套组件在文件系统里**落了地**。

## 2. 前置知识

阅读本讲前，你应当先了解以下概念（均在 u1-l1 中建立）：

- **LLDB 的定位**：LLVM 生态中的下一代调试器，复用 Clang、LLVM，并把能力封装成公共 C++ API（`SB*` 类）。
- **两个命名空间**：`lldb`（公共 API）与 `lldb_private`（内部实现）。本讲会反复用「公共 / 内部」这对概念。
- **SB API 的设计约束**：`SB*` 类不继承、无虚函数、只有一个成员（共享指针代理）。
- **CMake 构建**：LLDB 用 CMake + Ninja 构建，整个工程由若干 `add_subdirectory` 串起来。

如果你还不熟悉上述内容，建议先读 u1-l1。本讲不需要你懂任何调试器内部算法，只需要有「目录 = 模块边界」的工程直觉。

## 3. 本讲源码地图

本讲涉及的关键文件如下，阅读时可以对照源码加深印象：

| 文件 / 目录 | 作用 |
| --- | --- |
| `docs/resources/overview.md` | 官方对 LLDB 各代码分组的逐段说明，是本讲的权威依据。 |
| `CMakeLists.txt`（顶层） | 决定顶层目录如何被构建系统组织（`add_subdirectory`）。 |
| `source/CMakeLists.txt` | 决定 `source/` 下各模块的构建顺序，揭示模块依赖关系。 |
| `source/API/CMakeLists.txt` | `liblldb` 的链接清单，证明「API 模块链接了几乎所有其它模块」。 |
| `source/Plugins/Plugins.def.in` | 插件注册表模板，构建时生成，集中声明全部插件。 |
| `cmake/modules/LLDBLayeringCheck.cmake` | 模块分层校验逻辑，理解「为什么 API 最后构建」。 |
| `tools/driver/CMakeLists.txt` 等 | 各可执行工具的链接清单，用于实践任务。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. 顶层目录地图（`lldb/` 全貌）
2. `source/` 源码分组
3. `include/` 与 `source/` 的公私分层
4. 插件系统目录与 `Plugins.def.in` 注册表
5. `tools/` 可执行入口与依赖关系

---

### 4.1 顶层目录地图

#### 4.1.1 概念说明

打开 `lldb/` 仓库根目录，你会看到十几个顶层条目。这些条目可以分为三类：

- **构建与文档类**：`CMakeLists.txt`、`cmake/`、`docs/`、`scripts/`、`utils/`、`resources/`、`packages/`。
- **源码类**：`include/`（公共头）、`source/`（内部实现）、`bindings/`（脚本绑定）。
- **产物与示例类**：`tools/`（可执行入口）、`test/`（测试）、`unittests/`（单元测试）、`examples/`（示例）。

理解的关键是：**LLDB 既是一个可执行的调试器（`lldb` 命令），也是一个可被复用的库（`liblldb`）**。所以源码、可执行入口、绑定三者分工明确。

#### 4.1.2 核心流程

整个仓库的构建由顶层 `CMakeLists.txt` 用一系列 `add_subdirectory` 串起来，大致顺序如下（伪代码）：

```
顶层 CMakeLists.txt
 ├─ add_subdirectory(utils/TableGen)   # 先生成 tablegen 工具
 ├─ add_subdirectory(source)           # 构建 liblldb 及各内部静态库
 ├─ add_subdirectory(tools)            # 构建各可执行入口
 ├─ add_subdirectory(docs)             # 文档
 └─ add_subdirectory(test / unittests) # 测试（仅 LLDB_INCLUDE_TESTS 时）
```

注意 `source` 永远在 `tools` 之前构建——因为工具（如 `lldb`、`lldb-dap`）要链接 `source` 产出的 `liblldb`。

#### 4.1.3 源码精读

顶层 `CMakeLists.txt` 的核心 `add_subdirectory` 片段：

[顶层 CMakeLists.txt:L170-L173](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L170-L173) —— 这四行决定了核心构建顺序：TableGen → source → tools → docs。

测试相关构建被 `LLDB_INCLUDE_TESTS` 开关保护：

[顶层 CMakeLists.txt:L201-L207](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L201-L207) —— 说明 `test`、`unittests`、`utils` 这几个目录只在开启测试时构建。

脚本绑定 `bindings` 也受开关保护，只有启用 Python 或 Lua 时才构建：

[顶层 CMakeLists.txt:L141-L143](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L141-L143) —— `bindings` 目录与 `LLDB_ENABLE_PYTHON OR LLDB_ENABLE_LUA` 绑定。

#### 4.1.4 代码实践

**实践目标**：建立顶层目录的第一手印象。

**操作步骤**：

1. 在仓库根目录 `lldb/` 下用 `ls -1` 列出全部顶层条目。
2. 对照本节「三类划分」，把每个条目归到「构建与文档类 / 源码类 / 产物与示例类」。

**需要观察的现象**：你会看到 `source`、`include`、`tools`、`bindings`、`test`、`unittests`、`examples`、`docs`、`cmake`、`scripts`、`utils`、`resources`、`packages` 等目录，以及 `CMakeLists.txt`、`LICENSE.TXT`、`Maintainers.md` 等文件。

**预期结果**：你应当能不查资料地说出「想读源码去 `source/`，想看公共接口去 `include/lldb/`，想找可执行程序去 `tools/`」。

#### 4.1.5 小练习与答案

**练习 1**：`tools/` 目录里的程序和 `source/` 里的代码是什么关系？
**答案**：`source/` 编译出 `liblldb` 及若干内部静态库（如 `lldbHost`、`lldbTarget`）；`tools/` 里的每个可执行程序是一个**薄壳入口**，链接这些库来提供具体功能（命令行、调试服务器、DAP 适配器等）。

**练习 2**：为什么 `bindings/` 要受 `LLDB_ENABLE_PYTHON OR LLDB_ENABLE_LUA` 开关保护？
**答案**：因为绑定是用 SWIG 把公共 SB API 翻译成 Python / Lua 模块的代码，只有在用户选择启用某种脚本语言时才需要生成和编译，关闭时构建它是浪费。

---

### 4.2 `source/` 源码分组

#### 4.2.1 概念说明

`source/` 是 LLDB 的**内部实现**所在地。它按职责拆成了十几个并列的子目录，每个子目录就是一个「静态库模块」，最终被 `liblldb` 聚合。理解这些分组，等于掌握了 LLDB 的功能版图。

官方文档 `docs/resources/overview.md` 对这些分组有逐段说明，是本节权威依据。

#### 4.2.2 核心流程

`source/CMakeLists.txt` 用 `add_subdirectory` 列出了全部子模块的构建顺序，这个顺序本身反映了**自底向上的依赖关系**：

```
Utility → Host → Core → ... → Interpreter → Commands → Target → Symbol → Expression → ... → Plugins
                                                                                       ↓
                                                                                  API（最后）
```

依赖方向的直觉是：**底层模块（Utility/Host）几乎不依赖别人；上层模块（API）依赖几乎所有模块**。

#### 4.2.3 源码精读

`source/CMakeLists.txt` 完整地列出了各子模块，并在注释中点明「API 最后构建」的原因：

[source/CMakeLists.txt:L1-L22](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/CMakeLists.txt#L1-L22) —— 注意末尾注释：「Build API last. Since liblldb needs to link against every other target, it needs those targets to have already been created.」

下面这张表把 `source/` 下每个子模块的职责总结出来（结合 `overview.md` 与实际目录）：

| 子目录 | 职责（一句话） | 代表类 |
| --- | --- | --- |
| `Utility` | 最底层通用设施，与调试无强绑定 | `FileSpec`、`Stream`、`Status`、`StructuredData`、`DataExtractor` |
| `Host` | 宿主操作系统抽象层 | `HostInfo`、`MainLoop`、`NativeProcess`（供 lldb-server 用） |
| `Core` | 核心功能 + `Debugger` 本身 | `Debugger`、`Address`、`Broadcaster/Event/Listener`、`Communication`、`Module` |
| `ValueObject` | 变量的「值对象」（值 + 类型 + 地址） | `ValueObject`、`ValueObjectChild` |
| `Interpreter` | 命令基础类、命令解释器、选项系统 | `CommandInterpreter`、`CommandObject`、`OptionValue` |
| `Commands` | 一条条具体命令的实现 | `CommandObjectBreakpoint`、`CommandObjectExpression` |
| `Symbol` | 对象文件 / 调试符号解析 | `ObjectFile`、`SymbolFile`、`SymbolContext`、`LineTable` |
| `Target` | 调试目标对象层级 | `Target`、`Process`、`Thread`、`StackFrame`、`Platform`（基类） |
| `Breakpoint` | 断点与监视点 | `Breakpoint`、`BreakpointResolver`、`Watchpoint` |
| `Expression` | 表达式求值 | `UserExpression`、`DWARFExpression`、`IRExecutionUnit` |
| `DataFormatters` | 变量展示的自定义格式化 | `DataVisualization`、`FormatManager` |
| `Protocol` | 协议相关辅助 | — |
| `Initialization` | 系统初始化 | `SystemInitializer` |
| `Version` | 版本信息 | `Version` |
| `Plugins` | **所有插件实现**（见 4.4） | Process/SymbolFile/ObjectFile/... 各类插件 |
| `API` | 公共 C++ API（`SB*` 类），最后构建 | `SBDebugger`、`SBTarget`、`SBValue` |

`overview.md` 对几个关键分组有更细致的文字说明，例如 `Utility` 一节明确指出它是「最低层」：

[overview.md:L182-L204](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L182-L204) —— Utility 提供路径、数据缓冲、日志、JSON、Stream 等与调试无关的通用设施，且明确声明「提供通用 C++ 库不是本模块的目标」。

`Host` 一节解释了它如何隔离操作系统差异，并为 lldb-server 提供基类：

[overview.md:L116-L129](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L116-L129) —— Host 抽象了三元组、进程启动、管道/套接字等 OS 原语，并包含 `NativeProcess/Thread` 层次（供 lldb-server 使用）。

`Target` 一节列出了调试目标相关的全部对象：

[overview.md:L144-L154](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L144-L154) —— Target/Process/Thread/StackFrame/ABI/ExecutionContext 都在此。

值得特别注意的是 `Platform`：官方说明它「散布在代码库中，基类位于 `Target`」，与 `lldb-server` 的 platform 模式协作：

[overview.md:L156-L167](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L156-L167) —— 解释了 Platform 基类在 `source/Target/`，而各平台插件在 `source/Plugins/Platform/`。

#### 4.2.4 代码实践

**实践目标**：亲手把 `source/` 子模块的职责表格整理出来，加深记忆。

**操作步骤**：

1. 打开 `docs/resources/overview.md`，逐段阅读 API / Breakpoint / Commands / Core / Data Formatters / Expression / Host / Interpreter / Symbol / Target / Platform / Utility 各节。
2. 打开 `source/CMakeLists.txt`，把 `add_subdirectory` 列出的目录与 overview 的章节做对照。
3. 用 `ls -1 source/<子目录>/` 抽查几个子目录，确认其中的代表类文件（例如 `source/Core/` 下应有 `Debugger.cpp`、`Address.cpp`）。

**需要观察的现象**：overview 描述的「概念分组」与 `source/` 的「物理目录」大致一一对应，但也有细微差异——例如 overview 把「Value objects」归在 Core 一节里描述，但实际代码已经独立出 `source/ValueObject/` 目录；`Protocol`、`Initialization`、`Version` 这几个目录在 overview 中没有专门段落，但确实存在于 `source/`。

**预期结果**：你能产出一张类似上表的「子目录 → 职责 → 代表类」对照表。**待本地验证**：抽查到的具体文件名以你本地 checkout 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `API` 模块要在 `source/` 中**最后**构建？
**答案**：因为 `liblldb`（由 `source/API/` 构建）需要链接几乎所有其它模块（`lldbCore`、`lldbTarget`、`lldbSymbol`、`lldbExpression`……），CMake 要求这些被链接的 target 必须先被创建。`source/CMakeLists.txt` 末尾的注释明确说明了这一点。

**练习 2**：我想读「设置断点后如何随共享库加载不断重新解析」的代码，该去哪个子目录？
**答案**：去 `source/Breakpoint/`，重点看 `BreakpointResolver` 及其子类（如 `BreakpointResolverFileLine`）。overview 的 Breakpoint 一节对此机制有描述。

**练习 3**：`Commands` 和 `Interpreter` 两个目录有什么区别？
**答案**：`Interpreter` 提供**命令系统的基础设施**（`CommandInterpreter` 解析与分发、`CommandObject` 基类、`OptionValue` 选项体系）；`Commands` 则是**一条条具体命令的实现**（`CommandObjectBreakpoint`、`CommandObjectExpression` 等），它们继承自 `Interpreter` 提供的基类。

---

### 4.3 `include/` 与 `source/` 的公私分层

#### 4.3.1 概念说明

LLDB 严格区分**公共接口**与**内部实现**：

- `include/lldb/API/`：公共 `SB*` 类的头文件，对外承诺 ABI 稳定。
- `include/lldb/<其它>/`：内部模块（Core/Target/Symbol/...）的头文件，属于 `lldb_private`，不保证稳定。
- `source/`：这些头文件的 `.cpp` 实现。

一个关键观察：**`include/lldb/` 的子目录与 `source/` 的子目录高度镜像，但 `include/lldb/` 下没有 `Plugins/`**——因为插件是纯内部实现，不对外暴露任何公共头。

#### 4.3.2 核心流程

依赖方向可以用一条单向箭头概括：

```
tools/ 与 bindings/  ──只允许依赖──▶  include/lldb/API/  （公共、稳定）
       include/lldb/<内部>/  ──仅供──▶  source/ 内部相互引用
                                        ▲
                                  source/Plugins/（无公共头）
```

也就是说：**外部使用者（无论是命令行 driver、DAP 适配器，还是 Python 绑定）只应当看到 `lldb::SB*` 公共 API；`lldb_private::` 的任何东西都可能随版本变化**。

#### 4.3.3 源码精读

`include/lldb/` 下的子目录列表与 `source/` 镜像（API/Breakpoint/Core/.../Version），外加一批根级头文件：

`include/lldb/` 根级头文件承担「全局声明」职责，例如：

- `lldb-forward.h`：所有主要类的**前向声明**，打破循环依赖。
- `lldb-enumerations.h` / `lldb-types.h`：公共枚举与类型。
- `lldb-private.h`：一次性引入所有内部头，仅供内部使用。

注意 `include/lldb/` 下**没有 Plugins 目录**——这是「插件是纯内部实现」的最直接证据。你可以对照 `source/CMakeLists.txt`（有 Plugins）与 `include/lldb/`（无 Plugins）确认这一点。

公共 API 模块（`source/API/`）构建 `liblldb`，其链接清单证明它聚合了所有内部模块：

[source/API/CMakeLists.txt:L36-L141](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/CMakeLists.txt#L36-L141) —— 这是 `liblldb` 的 `add_lldb_library` 定义。其中第 128–141 行的 `LINK_LIBS` 列出了它链接的全部内部库：

[source/API/CMakeLists.txt:L128-L141](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/CMakeLists.txt#L128-L141) —— 可以看到 `lldbBreakpoint`、`lldbCore`、`lldbDataFormatters`、`lldbExpression`、`lldbHost`、`lldbInitialization`、`lldbInterpreter`、`lldbSymbol`、`lldbTarget`、`lldbUtility`、`lldbValueObject`、`lldbVersion` 以及 `${LLDB_ALL_PLUGINS}`（全部插件）都在其中。这正印证了「API 链接几乎所有模块」。

`liblldb` 的导出符号策略也强化了公私边界——默认只导出 `lldb` 命名空间（公共 API），`lldb_private` 的符号并不保证导出：

[source/API/CMakeLists.txt:L203-L209](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/CMakeLists.txt#L203-L209) —— 默认分支用 `liblldb.exports` 只导出 `lldb` 命名空间，并注释「Only the SB API is guaranteed to be stable.」（见上文 L168、L173 的 WARNING）。

公共 API 设计的五条规则在 overview 的 API 一节有明确说明：

[overview.md:L9-L29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L9-L29) —— 不继承、无虚函数、兼容 SWIG、单成员、接口最小化，以保证 ABI 稳定。

#### 4.3.4 代码实践

**实践目标**：亲手验证「公共 / 内部」分层。

**操作步骤**：

1. `ls -1 include/lldb/`，列出全部子目录与根级头文件。
2. 对比 `ls -1 source/`，找出「source 有、include 没有」的目录（答案是 `Plugins`）。
3. 打开任意一个 `include/lldb/API/SB*.h`（例如 `SBDebugger.h`），确认它处于 `namespace lldb`，且类无基类、无虚函数（可在文件内搜索 `virtual` 应为空）。
4. 再打开任意一个 `include/lldb/Target/*.h`（例如 `Process.h`），确认它处于 `namespace lldb_private`。

**需要观察的现象**：公共 `SB*` 头里看不到 `virtual` 关键字，且类通常只有一个 `std::shared_ptr` 成员；内部头（如 `Process.h`）则大量使用虚函数与继承，处于 `lldb_private` 命名空间。

**预期结果**：你能用自己的话说明「为什么外部代码只应依赖 `include/lldb/API/`」。**待本地验证**：具体头文件行号以本地为准，但「SB 类无虚函数」这一规律是稳定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `include/lldb/` 下没有 `Plugins/` 子目录？
**答案**：因为插件属于**纯内部实现**，不对外暴露公共头。插件通过 `Plugins.def.in` 注册表和 `PluginManager` 在运行时被组织，外部使用者无需（也不应）直接 `#include` 任何插件头。

**练习 2**：`lldb-forward.h` 这种「前向声明集中文件」解决了什么问题？
**答案**：它集中声明所有主要类，让各头文件之间可以用前向声明互相引用，而不必直接 `#include` 对方完整定义，从而**打破循环依赖、加速编译**。

---

### 4.4 插件系统目录与 `Plugins.def.in` 注册表

#### 4.4.1 概念说明

LLDB 的可扩展性核心是**插件机制**。`source/Plugins/` 下按「插件类别」分成三十多个子目录，每个子目录里又有一到多个具体插件。例如：

- `ObjectFile/` 下有 `ELF`、`Mach-O`、`PECOFF`、`wasm` 等，对应不同的可执行文件格式。
- `SymbolFile/` 下有 `DWARF`、`PDB`、`NativePDB`、`Breakpad`、`CTF` 等，对应不同的调试信息格式。
- `Process/` 下有 `gdb-remote`、`Linux`、`FreeBSD`、`elf-core`、`scripted` 等，对应不同的进程后端。
- `Platform/` 下有 `Linux`、`MacOSX`、`Windows`、`gdb-server` 等，对应不同的执行环境。
- `TypeSystem/` 与 `ExpressionParser/` 目前都只有 `Clang` 一个实现（因为 LLDB 复用 Clang 处理 C/C++/ObjC 类型与表达式）。
- `ScriptInterpreter/` 下有 `Python`、`Lua`、`None`。

这些插件在构建时被一张**注册表**集中声明，那就是 `source/Plugins/Plugins.def.in`。

#### 4.4.2 核心流程

插件从「源码」到「可用」的流程是：

```
cmake 配置阶段：扫描各插件目录 → 填充 @LLDB_ENUM_PLUGINS@ → 生成 Plugins.def
构建阶段：      各插件编译成静态库（lldbPlugin*）
运行时初始化：  SystemInitializerFull 遍历 Plugins.def，对每个 LLDB_PLUGIN(name) 调用 name::Initialize()
运行时使用：    PluginManager 按「插件类别 + 优先级」挑选合适插件（如根据文件魔数选 ObjectFile 插件）
```

关键点：`Plugins.def.in` 是一个**模板**，真正的插件列表是在 CMake 配置阶段由 `@LLDB_ENUM_PLUGINS@` 占位符替换生成的，所以**不要手动编辑它**（文件头注释也这么警告）。

#### 4.4.3 源码精读

`Plugins.def.in` 文件本身非常短，核心是让外部定义 `LLDB_PLUGIN(name)` 宏后，通过包含本文件来**枚举全部插件**：

[Plugins.def.in:L24-L37](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Plugins/Plugins.def.in#L24-L37) —— 文件开头要求调用者必须先定义 `LLDB_PLUGIN` 宏，否则报错；中间的 `@LLDB_ENUM_PLUGINS@` 是 CMake 在配置阶段填充的占位符；末尾还保留 `@LLDB_PROCESS_WINDOWS_PLUGIN@`、`@LLDB_PROCESS_GDB_PLUGIN@` 两个平台相关的占位符。

文件头的注释明确说明本文件是配置时生成的：

[Plugins.def.in:L17-L21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Plugins/Plugins.def.in#L17-L21) —— 「The set of plugins supported by LLDB is generated at configuration time... Do not modify this header directly.」

插件之间的依赖关系也受构建系统约束。`cmake/modules/LLDBLayeringCheck.cmake` 实现了「插件分层校验」：每个插件声明自己允许依赖哪些类别的插件，越界依赖会让构建报错：

[LLDBLayeringCheck.cmake:L20-L54](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBLayeringCheck.cmake#L20-L54) —— `check_lldb_plugin_layering()` 遍历所有插件，检查其 `LINK_LIBRARIES` 是否落在「可接受 / 容忍」的插件类别内，否则发出 `SEND_ERROR`。这保证插件依赖图是受控的（参见 `contributing.rst` 中关于插件 kind 的说明）。

各插件类别下的具体插件，可以实际列出来。以三大「格式插件」为例：

- **ObjectFile**（解析可执行文件格式）：`Breakpad`、`COFF`、`ELF`、`JSON`、`Mach-O`、`Minidump`、`PDB`、`PECOFF`、`Placeholder`、`XCOFF`、`wasm`
- **SymbolFile**（解析调试信息）：`Breakpad`、`CTF`、`DWARF`、`JSON`、`NativePDB`、`PDB`、`Symtab`
- **Process**（进程后端）：`gdb-remote`、`Linux`、`FreeBSD`、`AIX`、`NetBSD`、`POSIX`、`Windows`、`MacOSX-Kernel`、`elf-core`、`mach-core`、`minidump`、`scripted`、`wasm` 等

完整的插件类别目录（约三十多个）包括：`ABI`、`Architecture`、`Disassembler`、`DynamicLoader`、`ExpressionParser`、`Instruction`、`InstrumentationRuntime`、`JITLoader`、`Language`、`LanguageRuntime`、`MemoryHistory`、`ObjectContainer`、`ObjectFile`、`OperatingSystem`、`Platform`、`Process`、`Protocol`、`REPL`、`ScriptInterpreter`、`StructuredData`、`SymbolFile`、`SymbolLocator`、`SymbolVendor`、`SystemRuntime`、`Trace`、`TraceExporter`、`TypeSystem`、`UnwindAssembly` 等（可在本地用 `ls -1 source/Plugins/` 复核）。

#### 4.4.4 代码实践

**实践目标**：建立对插件分类的第一手认识，并理解注册表的生成机制。

**操作步骤**：

1. `ls -1 source/Plugins/`，数一数共有多少个「插件类别」目录。
2. 任选三个类别（如 `ObjectFile`、`SymbolFile`、`Process`），分别 `ls -1 source/Plugins/<类别>/`，看看每个类别下有多少个具体插件。
3. 打开 `source/Plugins/Plugins.def.in`，通读全文，确认它确实只是「模板 + 占位符」。
4.（可选）在你的构建目录里找到生成后的 `Plugins.def`（通常在 `build/` 下），对比 `.in` 与生成结果，看 `@LLDB_ENUM_PLUGINS@` 被替换成了什么。

**需要观察的现象**：`.in` 文件里只有 `@LLDB_ENUM_PLUGINS@` 一个占位符；生成后的 `Plugins.def` 会展开成一大串 `LLDB_PLUGIN(lldbPluginProcessLinux)` 之类的宏调用。

**预期结果**：你能解释「为什么不能直接改 `Plugins.def.in`」——因为它是构建系统生成的。**待本地验证**：生成后的 `Plugins.def` 内容取决于你启用的 CMake 选项（不同平台启用的插件不同）。

#### 4.4.5 小练习与答案

**练习 1**：`TypeSystem/` 和 `ExpressionParser/` 目录下目前只有 `Clang` 一个插件，这反映了 LLDB 的什么设计哲学？
**答案**：反映了 u1-l1 讲过的「组件复用」哲学——LLDB 不重新发明类型系统与表达式编译器，而是直接复用 Clang 来处理 C/C++/ObjC 的类型与表达式。所以这两个类别只有 Clang 一个实现。

**练习 2**：`Process/` 下的 `gdb-remote` 和 `scripted` 两个插件分别解决什么问题？
**答案**：`gdb-remote` 是最常见的远程调试后端，通过 GDB Remote 协议与 `lldb-server` 通信；`scripted` 则允许用 Python 实现「虚拟进程」（如从 crash log 回放出一个可调试进程），是脚本化扩展能力的体现（后续 u13 会专门讲）。

**练习 3**：`LLDBLayeringCheck.cmake` 为什么要校验插件之间的依赖？
**答案**：为了让插件依赖图保持受控、避免形成混乱的耦合。每个插件声明它能依赖哪些类别的插件（`LLDB_ACCEPTABLE_PLUGIN_DEPENDENCIES`），越界依赖直接构建报错，从而保持架构整洁。

---

### 4.5 `tools/` 可执行入口与依赖关系

#### 4.5.1 概念说明

`tools/` 下每一个（子）目录对应一个**可执行程序**。它们都是「薄壳入口」：解析命令行参数、初始化 LLDB，然后把控制权交给库。理解它们各自链接了哪些 `source` 模块，能帮你区分这些工具的定位。

最重要的几个入口：

| 工具 | 用途 | 链接的核心库 |
| --- | --- | --- |
| `driver`（即 `lldb`） | 命令行调试器主程序 | `liblldb` + `lldbHost` + `lldbUtility` |
| `lldb-server` | 远程调试的服务端（platform / gdbserver 两种模式） | **不含 `liblldb`**，直接链接 `lldbHost`/`lldbInitialization`/`lldbVersion` + 若干插件 |
| `lldb-dap` | VS Code 等 IDE 的 DAP 调试适配器 | `lldbDAP`（object 库）+ `liblldb` + `lldbHost` |
| `lldb-mcp` | 面向 LLM/Agent 的 MCP 接口 | `liblldb` 等 |
| `debugserver` | macOS 上的底层调试服务器（仅 Darwin） | 独立实现 |

#### 4.5.2 核心流程

一个有趣且重要的对比是 **`lldb`（driver）链接了 `liblldb`，而 `lldb-server` 不链接 `liblldb`**。原因是：

- `lldb` 是完整调试器前端，需要全套 SB API → 链接庞大的 `liblldb`。
- `lldb-server` 是**精简的服务端**，它只需要 `lldb_private` 的部分能力（Host/Initialization + 具体 Process/ObjectFile 插件），不需要公共 SB API 层，所以刻意不链接 `liblldb`，从而体积更小、启动更快。

这正好呼应了 4.3 讲的「公私分层」：前端工具用公共 API，服务端直接用内部模块。

#### 4.5.3 源码精读

`lldb` 命令行驱动（driver）的链接清单很简短，主要就是 `liblldb`：

[tools/driver/CMakeLists.txt:L31-L48](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/driver/CMakeLists.txt#L31-L48) —— `LLDB_DRIVER_LINK_LIBS` 只有 `liblldb`、`lldbHost`、`lldbUtility` 三个，源文件只有 `Driver.cpp` 与 `Platform.cpp`。可见 driver 是个薄壳。

`lldb-server` 的链接清单则完全不同——**没有 `liblldb`**，而是直接链接内部库与具体插件：

[tools/lldb-server/CMakeLists.txt:L62-L84](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/lldb-server/CMakeLists.txt#L62-L84) —— `LINK_LIBS` 是 `lldbHost`、`lldbInitialization`、`lldbVersion`、`${LLDB_PLUGINS}`（如 `lldbPluginProcessLinux`、`lldbPluginObjectFileELF` 等）、以及若干 `lldbPluginInstruction*` 插件。注意其中**没有 `liblldb`**。

`lldb-dap` 通过一个 object 库 `lldbDAP` 组织大量源文件，最终可执行程序链接它加上 `liblldb`：

[tools/lldb-dap/tool/CMakeLists.txt:L5-L15](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/lldb-dap/tool/CMakeLists.txt#L5-L15) —— `LLDB_DAP_LINK_LIBS` 为 `lldbDAP liblldb lldbHost`。`lldbDAP` 本身是一个 `OBJECT` 库（见同目录上级 `CMakeLists.txt` 第 7 行 `add_lldb_library(lldbDAP OBJECT ...)`），把几十个 Handler/Protocol 源文件编译成对象文件共享给工具与单元测试。

`tools/CMakeLists.txt` 还揭示了各工具的构建条件——例如 `lldb-server` 需要 `LLDB_CAN_USE_LLDB_SERVER`，`debugserver` 仅在 Darwin 且未用系统 debugserver 时构建：

[tools/CMakeLists.txt:L21-L33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/CMakeLists.txt#L21-L33) —— 可以看到平台相关的条件分支。

#### 4.5.4 代码实践

**实践目标**：通过链接清单，区分三个工具的定位差异。

**操作步骤**：

1. 打开 `tools/driver/CMakeLists.txt`，记录 `lldb` 链接的库（应有 `liblldb`）。
2. 打开 `tools/lldb-server/CMakeLists.txt`，记录 `lldb-server` 链接的库（**应没有 `liblldb`**，而是直接链接 `lldbHost`/`lldbInitialization` 与具体插件）。
3. 打开 `tools/lldb-dap/tool/CMakeLists.txt`，记录 `lldb-dap` 链接的库（应有 `liblldb` 与 object 库 `lldbDAP`）。
4. 用一句话分别概括三者：谁用了公共 SB API，谁直接用内部模块。

**需要观察的现象**：`lldb` 与 `lldb-dap` 的链接清单里都有 `liblldb`；`lldb-server` 的链接清单里**找不到** `liblldb`。

**预期结果**：你能得出结论——「`lldb-server` 是精简服务端，刻意避开庞大的公共 API 层，直接用内部库与插件」。这正是 LLDB 前后端分离架构的体现。

#### 4.5.5 小练习与答案

**练习 1**：如果我只想用 Python 脚本调用 LLDB，需要 `lldb`（driver）这个可执行程序吗？
**答案**：不需要。Python 通过 `import lldb` 直接加载 `liblldb`（以及 SWIG 生成的 Python 绑定模块）即可，driver 只是 `liblldb` 的一个命令行前端壳子。这也印证了 LLDB「既是调试器也是库」的双重身份。

**练习 2**：为什么 `lldb-dap` 要把大量源文件放进一个 `OBJECT` 库 `lldbDAP`，而不是直接编进可执行程序？
**答案**：因为 object 库可以被**可执行程序与单元测试（DAPTests）共享**，避免重复编译；同时 object 库不声明自己的链接依赖，让每个消费者（工具 / 测试）各自声明 `LINK_LIBS`，避免静态 LLVM 组件符号与 `liblldb` 重导出符号冲突（见 `tools/lldb-dap/CMakeLists.txt` 顶部注释）。

---

## 5. 综合实践

把本讲五个模块串起来，完成下面这个「全库巡礼」任务：

1. **画目录树**：在纸上（或文本文件里）画出 `lldb/` 的两级目录树，标注每个顶层目录的类别（构建文档 / 源码 / 产物示例）。
2. **填模块表**：参照 4.2 的表格，结合本地 `source/` 实际内容，补全每个 `source/<子模块>` 的「职责 + 代表类」。
3. **连线分层**：用箭头画出依赖方向，标出「`include/lldb/API/`（公共）← tools/bindings」「`source/Plugins/`（纯内部，无公共头）」。
4. **工具对比**：随机挑选 `tools/` 下三个目录（建议 `driver`、`lldb-server`、`lldb-dap`），打开各自的 `CMakeLists.txt`，记录它们链接了哪些 `source` 模块，并用一句话说明差异（重点：谁链接了 `liblldb`、谁没有）。
5. **插件清点**：`ls -1 source/Plugins/` 数出插件类别总数，并各挑一个类别列出其下的具体插件名。

完成后再回头看 u1-l1 里那张「Utility/Host/Core/API/Symbol/Target/...」的分组描述，你会发现它已经从抽象概念变成了你能定位到的真实目录。这个心智地图是后续每一讲（无论是 SBAPI、命令系统、还是 Target 模型）的导航基础。

## 6. 本讲小结

- `lldb/` 顶层分三类：构建文档类（`cmake`/`docs`/`scripts`/`utils`）、源码类（`include`/`source`/`bindings`）、产物示例类（`tools`/`test`/`unittests`/`examples`）。
- `source/` 按职责拆成十几个并列静态库模块（Utility/Host/Core/Interpreter/Commands/Symbol/Target/Breakpoint/Expression/...），`API` 模块**最后构建**并聚合为 `liblldb`，因为它要链接几乎所有其它模块。
- `include/lldb/` 与 `source/` 子目录高度镜像，但**没有 `Plugins/`**——插件是纯内部实现；公共 `SB*` 类（`include/lldb/API/`）承诺 ABI 稳定，`lldb_private` 则不保证。
- 插件按「类别」组织在 `source/Plugins/<类别>/<具体插件>/` 下，共三十多个类别；所有插件由 `Plugins.def.in`（配置时生成的注册表）集中声明，受 `LLDBLayeringCheck.cmake` 的分层校验约束。
- `tools/` 下每个目录是一个薄壳可执行入口；关键差异是 `lldb`（driver）与 `lldb-dap` **链接 `liblldb`**，而 `lldb-server` **不链接 `liblldb`**，直接用内部库与插件，体现前后端分离。
- LLDB 既是可执行调试器（`lldb`），也是可复用库（`liblldb`），Python/Lua 通过 `bindings/` 生成的 SWIG 绑定直接使用同一套 SB API。

## 7. 下一步学习建议

有了这张目录地图，接下来建议：

- **想跑起来**：进入 [u1-l3 从源码构建与运行 LLDB](u1-l3-build-and-run.md)，用 CMake + Ninja 真正构建一次，亲眼看到 `liblldb`、`lldb`、`lldb-server` 这些产物。
- **想从入口读起**：进入 [u1-l4 第一次运行：lldb CLI 驱动入口](u1-l4-cli-driver-entry.md)，跟随 `tools/driver/Driver.cpp` 的 `main()` 走通启动主链路。
- **想理解公共 API**：进入第 2 单元（[u2-l1 SBAPI 设计哲学](u2-l1-sbapi-philosophy.md)），深入 `include/lldb/API/` 与 `source/API/` 的 `SB*` 类。
- **继续源码精读的建议路径**：建议按 `tools/driver` → `source/API`（SBDebugger）→ `source/Core`（Debugger）→ `source/Interpreter`（CommandInterpreter）的顺序，先把「命令如何被接收与分发」这条链路打通，再深入 Target/Symbol 等数据模型。
