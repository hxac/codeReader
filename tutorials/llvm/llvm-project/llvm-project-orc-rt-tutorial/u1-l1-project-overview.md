# 项目定位：orc-rt 是什么

## 1. 本讲目标

本讲是 orc-rt（LLVM ORC Runtime）学习手册的第一讲，目标是让从零开始的读者在不动手编译的前提下，建立对整个项目的高层认识。学完后你应该能够：

- 说出 orc-rt 解决了什么问题：它是「运行 JIT 代码的进程」所需要的运行时支持代码。
- 区分 **controller（控制端）** 与 **executor（执行端）** 两个角色，并能说明 orc-rt 与 LLVM 自带的 ORC 库分别运行在哪一侧。
- 知道 orc-rt 目前仍是实验性项目，ABI/API 尚不稳定，从而理解「构建时必须与 ORC 库同源」这条使用纪律。
- 说出 orc-rt 对 C++ 标准与编译器版本的最低要求。

本讲不要求你已经有任何 JIT、编译原理或 LLVM 开发经验；我们会把必要的背景讲清楚。

## 2. 前置知识

阅读本讲前，最好大致了解以下几个名词。如果你完全没听过也没关系，下面会逐一解释。

- **JIT（Just-In-Time）编译**：程序运行过程中，把「还只是中间表示（如 LLVM IR）的代码」现场翻译成机器码，并立即执行。与之相对的是 AOT（Ahead-Of-Time，提前编译）。
- **LLVM ORC**：LLVM 提供的一套用于 JIT 的 API（`llvm::orc` 命名空间）。ORC 是 "On Request Compilation"（按需编译）的缩写。常见入口是 `llvm::orc::LLJIT`。
- **进程（process）**：操作系统里一个独立运行的程序实例。本讲的关键是：controller 和 executor **可以是不同的进程**，甚至运行在不同机器上。
- **ABI（Application Binary Interface）**：二进制接口，决定编译出的库在二进制层面如何被调用。ABI 不稳定意味着升级后可能不兼容旧调用方。

## 3. 本讲源码地图

本讲主要依赖文档与构建脚本（不涉及复杂源码实现），为后续讲义建立全局心智模型。

| 文件 | 作用 |
| --- | --- |
| `docs/index.md` | 项目首页：一句话定位、当前状态、平台/编译器支持的入口。 |
| `docs/Design.md` | 设计文档：交代 controller/executor 概念，并逐一介绍 Session、ControllerAccess、Service、WrapperFunction 等核心抽象。本讲反复引用它的「Background」与各组件小节。 |
| `CMakeLists.txt` | 顶层构建脚本：声明 C++ 标准、语言运行时选项（RTTI/异常）、日志后端等，是「平台与编译器要求」这一节的源码依据。 |

> 后续讲义会进入 `include/orc-rt/`、`lib/executor/`、`test/` 等目录的源码细节，本讲先把全局图景立起来。

## 4. 核心概念与源码讲解

### 4.1 项目背景与现状

#### 4.1.1 概念说明

orc-rt 的全称是 **LLVM ORC Runtime**（ORC 运行时）。要理解它，先要理解一个事实：LLVM 的 ORC API 可以在「一个进程里编译代码、在另一个进程里执行代码」。为了让那台执行代码的进程能够正常工作，需要一套配套的运行时支持代码——这就是 orc-rt。

一句话概括它的定位（来自项目首页）：

> The ORC runtime provides **executor-side** support code for the LLVM ORC APIs.

注意关键词 **executor-side（执行端）**。它告诉我们 orc-rt 运行的「位置」是在执行 JIT 代码的那一侧，而不是编译 JIT 代码的那一侧。这一点会在 4.2 节展开。

#### 4.1.2 核心流程

把一个 JIT 程序跑起来，整体路径大致是：

1. **控制端**用 LLVM ORC 库（如 `llvm::orc::LLJIT`）编译、链接出 JIT 代码。
2. 这些 JIT 代码（机器码）被搬运到 **执行端**。
3. 执行端需要一系列「现场支持」：分配可执行内存、注册动态库、处理跨进程调用、管理生命周期等——这些就是 orc-rt 提供的能力。
4. 执行端的 `orc_rt::Session` 对象成为这套运行时的根，协调各项资源。

本讲只要求你建立「orc-rt 在第 3 步」这个位置感，具体机制后面再讲。

#### 4.1.3 源码精读

首页顶部一句话点明 orc-rt 的身份——**为 LLVM ORC API 提供执行端支持代码**：

[docs/index.md:L5-L5](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/index.md#L5)

紧接着「Current Status」一节明确告诉我们这个项目还很年轻、**ABI 和 API 都不稳定**，并给出一条关键使用纪律——执行端运行时必须和控制端 ORC 库来自同一次构建：

[docs/index.md:L15-L19](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/index.md#L15-L19)

这段话的实践含义是：如果你升级了 LLVM，就要用对应版本的 orc-rt 重新构建执行端，不能混用不同版本的二进制。设计文档开篇同样把 executor/controller 的对立讲得很清楚：

[docs/Design.md:L1-L6](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L1-L6)

注意它强调 orc-rt 同时支持「JIT'd code 本身」以及「使用 JIT'd code 的代码」——也就是说，执行端既管被编译出来的代码，也管那些调用这些代码的宿主逻辑。

#### 4.1.4 代码实践

**实践目标**：亲手从源码确认 orc-rt 的「实验性 / 不稳定」状态，避免日后误以为它是一套已经冻结的稳定 API。

**操作步骤**：

1. 打开 `docs/index.md`，定位到 `### Current Status` 小节。
2. 找到包含 "neither the ABI nor API are stable" 的段落。
3. 再打开 `docs/index.md` 的 `### Platform and Compiler Support` 和 `### Notes and Known Issues`，注意其中多处写着 `* TODO`。

**需要观察的现象**：你会发现「平台支持」「已知问题」「讨论渠道」等多个小节都还是 `TODO`，这从侧面印证项目尚处早期。

**预期结果**：你能用自己的话复述——「orc-rt 是新的、实验性的项目，ABI/API 不稳定，必须与同一次构建的 LLVM ORC 库配套使用」。

> 待本地验证：若你已克隆仓库，可用 `grep -n "TODO" docs/index.md` 直接列出所有尚未填写的条目。

#### 4.1.5 小练习与答案

**练习 1**：orc-rt 首页用哪三个词描述它的成熟度？为什么这会影响你选择版本？

> **参考答案**：用 "new, experimental" 描述，并明确 "neither the ABI nor API are stable"。因为二进制接口不稳定，跨版本混用执行端运行时和控制端 ORC 库可能调用约定不匹配，所以必须配套同源构建。

**练习 2**：如果你在生产环境用了某个 LLVM 版本构建的 controller，执行端能否随意用一个更新版本的 orc-rt？为什么？

> **参考答案**：不建议。首页要求 "use an ORC Runtime from the same build as their LLVM ORC libraries"。不同版本的 ABI 可能已经变化，混用会带来难以排查的兼容性问题。

### 4.2 controller / executor 概念（核心心智模型）

#### 4.2.1 概念说明

这是本讲最重要的一节。理解了 controller 和 executor，你就理解了 orc-rt 存在的根本理由。

- **controller（控制端）**：负责 **定义和链接（define and link）** JIT 代码的进程。它链接的是 LLVM 的 ORC 库（如 `LLJIT`），干的是「编译 + 链接」的活。
- **executor（执行端）**：负责 **执行（execute）** JIT 代码的进程。它链接的是 orc-rt，干的是「把机器码跑起来 + 提供运行时支持」的活。

两者可以是同一个进程（in-process），也可以是不同进程甚至不同机器（cross-process）。orc-rt 的全部设计都围绕「执行端要独立于控制端工作」这一前提。

#### 4.2.2 核心流程

执行端运行时的几大组件及其关系如下（本讲只需建立印象，细节在后续讲义展开）：

```
        controller 进程                       executor 进程
   ┌─────────────────────┐              ┌──────────────────────────┐
   │  llvm::orc          │   RPC(按地址) │  orc_rt::Session (根对象) │
   │  ExecutionSession   │ ───────────▶ │   ├── ControllerAccess    │
   │  / LLJIT            │              │   │    (双向 RPC 桥)        │
   │                     │ ◀─────────── │   ├── Service: 内存管理    │
   │  编译 + 链接 JIT 代码 │   RPC(按 tag) │   ├── Service: 动态库加载  │
   │                     │              │   └── TaskDispatcher       │
   └─────────────────────┘              └──────────────────────────┘
```

注意通信的**不对称性**（这是 orc-rt 设计的关键点，详见 4.2.3）：

- **controller → executor**：按 **地址（address）** 调用，即 controller 可以调用执行端里任意一段代码。
- **executor → controller**：按 **标签（tag）** 调用，tag 是执行端里与控制端处理器关联的地址，确保执行端只能调用「被刻意暴露」的控制端入口。

#### 4.2.3 源码精读

设计文档的「Background」小节正式定义了两个角色，并指出 controller 链接 ORC 库、executor 构造 `orc_rt::Session`：

[docs/Design.md:L10-L17](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L10-L17)

「Session」小节说明执行端的根对象是 `orc_rt::Session`，它拥有若干 `Service`（管理 JIT 内存、unwind 信息、动态库句柄等资源），并且 **必须先于任何 JIT 代码创建、并在所有 JIT 代码执行完毕后才能销毁**：

[docs/Design.md:L20-L31](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L20-L31)

「ControllerAccess」小节给出 RPC 的不对称语义——控制端按地址调、执行端按 tag 调，从而保证执行端不会越权调用控制端内部：

[docs/Design.md:L42-L47](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L42-L47)

设计文档还顺带点名了几个组件，本讲你只要记住它们是「执行端运行时的组成部分」即可：`Service`（资源管理接口）在 [docs/Design.md:L53-L68](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L53-L68)；托管代码执行与关闭在 [docs/Design.md:L70-L93](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L70-L93)；任务分发在 [docs/Design.md:L95-L101](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L95-L101)；统一调用签名「wrapper function」在 [docs/Design.md:L103-L111](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L103-L111)。

#### 4.2.4 代码实践

**实践目标**：把上面那张架构图变成自己脑中的稳定模型，并理解通信的不对称性。

**操作步骤**：

1. 在纸上画出两个方框，分别标 `controller 进程` 与 `executor 进程`。
2. 在 controller 方框内写 `LLVM ORC 库（LLJIT / ExecutionSession）`，在 executor 方框内写 `orc_rt::Session + Services`。
3. 画两条方向相反的箭头：从 controller 指向 executor 的箭头标注「按 address 调用」；从 executor 指向 controller 的箭头标注「按 tag 调用」。
4. 对照 [docs/Design.md:L33-L51](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L33-L51) 检查你的箭头方向和标签是否一致。

**需要观察的现象**：你会注意到执行端「只能调用刻意暴露的 controller 入口」，这是一种**最小权限**设计。

**预期结果**：你能在不看资料的情况下，向别人讲清楚「为什么执行端调控制端要用 tag 而不是任意地址」——因为 tag 限制了执行端只能命中被显式注册的处理器，避免越权。

> 待本地验证：这是一道阅读 + 作图题，不需要编译。

#### 4.2.5 小练习与答案

**练习 1**：判断对错并说明理由——「controller 和 executor 必须运行在不同机器上。」

> **参考答案**：错。它们可以跨进程、跨机器，但也可以在同一个进程内（in-process）。orc-rt 提供了 `InProcessControllerAccess` 这种同进程桥（后续讲义会讲）来支持后者。

**练习 2**：为什么 controller→executor 用地址、而 executor→controller 用 tag？请用一句话解释这种不对称的安全性意义。

> **参考答案**：controller 是可信方，可按地址调用执行端任意代码；而执行端相对不可信，只能通过预先注册的 tag 调用控制端刻意暴露的入口，从而避免执行端越权访问控制端内部逻辑。

**练习 3**：执行端的「根对象」叫什么？它至少要满足哪两条生命周期约束？

> **参考答案**：根对象是 `orc_rt::Session`。约束是：① 必须在添加任何 JIT 代码之前创建；② 必须比所有相关 JIT 代码的执行活得更久（见 [docs/Design.md:L20-L31](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L20-L31)）。

### 4.3 平台与编译器要求

#### 4.3.1 概念说明

虽然首页的「Platform and Compiler Support」一节还写着 `TODO`，但项目仍有一些**可从源码直接确认**的硬性要求：C++ 标准版本、CMake 版本、编译器版本，以及一组语言运行时开关（RTTI、异常）和日志后端开关。理解这些，你才能判断「我的工具链能不能构建 orc-rt」。

需要先解释两个术语：

- **RTTI（Run-Time Type Information）**：C++ 运行时类型信息，`dynamic_cast`、`typeid` 等依赖它。orc-rt 默认开启 RTTI，但也允许关闭（`-fno-rtti`），为此它自带了一套可扩展的「自定义 RTTI」（后续讲义会讲）。
- **异常（exceptions）**：C++ 异常机制。orc-rt 默认开启异常，但同样允许关闭；它的错误处理用自研的 `Error`/`Expected<T>`，在边界处才与异常互转。

#### 4.3.2 核心流程

构建 orc-rt 的决策链：

1. 确认 CMake ≥ 3.20（C++ 标准固定为 C++17）。
2. 推荐使用 Clang 16 及以上（首页说明，更老的版本「可能」能跑但不保证）。
3. 选择语言运行时开关：`ORC_RT_ENABLE_RTTI`、`ORC_RT_ENABLE_EXCEPTIONS`（默认都 `ON`）。
4. 选择日志后端 `ORC_RT_LOG_BACKEND`（`none`/`printf`/`os_log`，默认 `none`）与日志级别 `ORC_RT_LOG_LEVEL`（默认 `info`）。
5. 这些选择最终被写入生成的 `config.h`，编译进库。

#### 4.3.3 源码精读

CMake 最低版本与项目语言（C/C++/汇编）要求：

[CMakeLists.txt:L7-L7](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L7)

[CMakeLists.txt:L20-L20](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L20)

C++ 标准固定为 C++17 且强制要求（`CMAKE_CXX_STANDARD_REQUIRED YES`，无编译器扩展）：

[CMakeLists.txt:L33-L35](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L33-L35)

语言运行时开关——RTTI 与异常默认开启，关闭时会传递 `-fno-rtti` / `-fno-exceptions`：

[CMakeLists.txt:L55-L71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L55-L71)

日志后端与级别选项（注意 `os_log` 仅限 Apple 平台，且默认后端为 `none` 表示日志被完全编译掉）：

[CMakeLists.txt:L73-L101](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L73-L101)

首页对编译器版本的要求（推荐 Clang 16+，更老「可能」能工作但不保证）：

[docs/index.md:L25-L29](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/index.md#L25-L29)

#### 4.3.4 代码实践

**实践目标**：在没有运行 cmake 的情况下，仅通过阅读 `CMakeLists.txt`，列出 orc-rt 的全部「语言运行时」与「日志」相关开关及其默认值。

**操作步骤**：

1. 打开 [CMakeLists.txt:L55-L71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L55-L71)，找到 `ORC_RT_ENABLE_RTTI` 与 `ORC_RT_ENABLE_EXCEPTIONS` 两个 `option(...)`，记录它们的默认值。
2. 打开 [CMakeLists.txt:L73-L101](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L73-L101)，记录 `ORC_RT_LOG_BACKEND` 与 `ORC_RT_LOG_LEVEL` 的可选值与默认值。
3. 注意第 98-100 行：`os_log` 后端在非 Apple 平台会直接 `FATAL_ERROR`。

**需要观察的现象**：默认后端是 `none`，意味着开箱即用时**所有日志都被编译掉**，看不到任何 `ORC_RT_LOG` 输出。

**预期结果**：你应得到类似下表的结论（建议自己整理）：

| 开关 | 默认值 | 可选值 |
| --- | --- | --- |
| `ORC_RT_ENABLE_RTTI` | `ON` | `ON` / `OFF` |
| `ORC_RT_ENABLE_EXCEPTIONS` | `ON` | `ON` / `OFF` |
| `ORC_RT_LOG_BACKEND` | `none` | `none` / `printf` / `os_log`（仅 Apple） |
| `ORC_RT_LOG_LEVEL` | `info` | `error` / `warning` / `info` / `debug` |

> 待本地验证：如需亲眼看到日志，构建时需显式指定 `-DORC_RT_LOG_BACKEND=printf`（详见下一讲 u1-l2）。

#### 4.3.5 小练习与答案

**练习 1**：orc-rt 要求 C++ 标准是哪个版本？是否允许使用编译器扩展（如 GNU 扩展）？

> **参考答案**：C++17（`CMAKE_CXX_STANDARD 17`），且 `CMAKE_CXX_EXTENSIONS NO`、`CMAKE_CXX_STANDARD_REQUIRED YES`，即不允许编译器扩展。

**练习 2**：默认配置下，`ORC_RT_LOG(...)` 调用会产生任何输出吗？为什么？

> **参考答案**：不会。默认 `ORC_RT_LOG_BACKEND=none`，所有日志在编译期被移除，级别设置此时无效（CMake 还会对此给出 WARNING）。

**练习 3**：如果想在 Linux 上把日志后端设为 `os_log`，会发生什么？

> **参考答案**：CMake 配置阶段会 `FATAL_ERROR`，因为 `os_log` 是 Apple 平台专属（见 [CMakeLists.txt:L98-L100](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L98-L100)）。

## 5. 综合实践

**任务**：阅读 `docs/index.md` 与 `docs/Design.md` 的 Background 小节，用自己的话写一段 **200 字以内** 的说明，回答两个问题：

1. orc-rt 为执行端提供了哪些能力？（提示：管理 JIT 内存、动态库、跨进程调用、生命周期等。）
2. orc-rt 与 LLVM ORC 库各自运行在哪一侧？（提示：谁是 controller，谁是 executor。）

**参考写作要点（请先自己动笔再对照）**：

- orc-rt 运行在 **executor（执行端）**，为运行 JIT 代码的进程提供运行时支持：用 `orc_rt::Session` 作为根对象，通过若干 `Service` 管理 JIT 内存、unwind 信息、动态库句柄等资源；通过 `ControllerAccess` 与控制端做双向 RPC；通过 wrapper function 实现统一的「字节进、字节出」调用签名。
- LLVM ORC 库（如 `LLJIT`）运行在 **controller（控制端）**，负责定义与链接 JIT 代码。
- 两者必须来自同一次构建（ABI 不稳定）。
- 通信不对称：控制端按地址调执行端，执行端按 tag 调控制端。

> 这道综合实践是「源码阅读型」任务，不需要编译运行；目的是让你把 4.1～4.3 三个模块的知识融成一段连贯表述。

## 6. 本讲小结

- orc-rt 是 LLVM ORC JIT 的**执行端（executor）运行时**，首页用一句话定位：「provides executor-side support code for the LLVM ORC APIs」。
- **controller** 链接 LLVM ORC 库、负责编译链接 JIT 代码；**executor** 链接 orc-rt、负责执行 JIT 代码并管理其运行时资源。两者可同进程也可跨进程。
- 执行端的根对象是 `orc_rt::Session`，必须先于 JIT 代码创建、后于 JIT 代码销毁。
- 跨进程通信**不对称**：控制端→执行端按 **地址**，执行端→控制端按 **tag**，保证执行端只能调用刻意暴露的控制端入口。
- orc-rt 是**实验性项目**，ABI/API 不稳定，必须与同一次构建的 ORC 库配套使用。
- 构建要求：CMake ≥ 3.20、C++17（无编译器扩展）、推荐 Clang 16+；默认开启 RTTI 与异常、默认日志后端为 `none`（日志被编译掉）。

## 7. 下一步学习建议

本讲建立了「controller/executor + Session/Service/ControllerAccess」的全局心智模型。建议下一步：

1. **动手构建一次** orc-rt，把它从抽象文档变成可见产物——这正是下一讲 **u1-l2「构建、配置与测试」** 的内容，它会讲清 CMake 的 runtimes 集成方式与各项 `ORC_RT_*` 编译选项。
2. 想先看清代码组织，可以先读 **u1-l3「目录结构与源码布局」**，区分 `include/orc-rt`（C++ 头）、`include/orc-rt-c`（C 头）、`lib/executor`（实现）、`test`（测试）。
3. 想继续深化心智模型，进入 **u2 单元**：先看 **u2-l1「Controller–Executor 架构全景」** 把本讲的概念串成完整数据流，再看 **u2-l2「ExecutorAddr」** 与 **u2-l3「错误处理模型概览」** 铺垫两套执行端基础设施。

建议先做 u1-l2，再按需在 u1-l3 与 u2 单元之间选择。
