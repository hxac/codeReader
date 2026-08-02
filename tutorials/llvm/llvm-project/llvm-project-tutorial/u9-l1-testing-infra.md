# 测试体系：lit、FileCheck 与单元测试

## 1. 本讲目标

读完后你应当能够：

- 说清 LLVM 测试基础设施由哪几大类构成、各自落在仓库的哪个目录，以及为什么这样分工。
- 理解 `lit` 是什么：它如何发现测试、如何用 `lit.cfg.py` 把命令行工具「装」进测试、又如何把单个测试文件当成一条 shell 流水线来执行。
- 读懂并手写一段 `FileCheck` 的 `CHECK` / `CHECK-NEXT` / `CHECK-NOT` / `CHECK-LABEL` 校验规则，理解它「按顺序在输出里找模式」的工作方式与退出码约定。
- 认识 LLVM 的单元测试体系：基于 Google Test / Google Mock，位于 `llvm/unittests`，并由 `llvm/Testing/Support` 提供针对 `Error`/`Expected` 的专用匹配宏。
- 独立编写一个 `.ll` + `FileCheck` 的回归测试，并用 `llvm-lit` 把它跑通。

本讲是 [u4-l4 编写你自己的 LLVM Pass](u4-l4-write-your-own-pass.md) 的直接延续——你写完一个 pass 之后，唯一靠谱的交付方式就是给它配一套能自动跑、能挡回归的测试。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要专门讲测试？** LLVM 是一个有数千名贡献者、每个工作日合并上百个补丁的项目。任何一次优化改动都可能「治好一个 bug、同时碰坏另一个」。靠人肉肉眼审查 IR 变化是不可行的，唯一可行的方式是：每改一处，就让机器自动跑成千上万个「输入 → 期望输出」的对照点。这套自动化对照的底座就是 `lit` + `FileCheck`。

**两类测试的分工。** 「回归测试（regression test）」用一小段 `.ll`/`.c`/`.s` 输入，跑某个工具（如 `opt`、`llc`），再用 `FileCheck` 校验输出里有没有期望的关键行——它擅长测「变换与分析的正确性」。「单元测试（unit test）」用 Google Test 写 C++，直接调用库的 API、断言返回值——它擅长测「支持库与数据结构本身的正确性」。两者互补，缺一不可。

**三个关键名词。** 「`lit`」是 LLVM Integrated Tester，一个用 Python 写的测试调度器，负责发现、过滤、并行执行测试；「`FileCheck`」是一个独立的 C++ 工具，负责「拿一份模式文件去校验另一份输出」；「`gtest`/`gmock`」是 Google 的 C++ 单元测试与匹配框架，LLVM 把它 vendored 进 `third-party/unittest`，并额外提供了 `llvm/Testing/Support` 这一层胶水。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `llvm/utils/lit/lit.py` | `lit` 的命令行入口（极薄的包装，转调 `lit.main`）。 |
| `llvm/utils/lit/lit/main.py` | `lit` 的真正主流程：解析参数 → 发现测试 → 过滤 → 执行 → 汇报。 |
| `llvm/utils/lit/lit/formats/shtest.py` | ShTest 格式：把一个测试文件解释成「若干条 shell 流水线 + 断言」。 |
| `llvm/test/lit.cfg.py` | LLVM 回归测试套件的配置：注册工具替换、可用特性（features）、替换变量。 |
| `llvm/test/Unit/lit.cfg.py` | LLVM 单元测试套件的配置：用 GoogleTest 格式驱动 `unittests/` 下的可执行文件。 |
| `llvm/utils/FileCheck/FileCheck.cpp` | `FileCheck` 工具本体：读模式文件、逐条匹配、按退出码汇报。 |
| `llvm/docs/CommandGuide/FileCheck.rst` | `FileCheck` 的权威手册，定义了 `CHECK` 家族指令的语义。 |
| `llvm/test/Transforms/InstCombine/2008-05-31-AddBool.ll` | 一个最简、完整的 `.ll` 回归测试范例。 |
| `llvm/include/llvm/Testing/Support/Error.h` | 给 `Error`/`Expected` 用的 gmock 匹配宏（`EXPECT_THAT_ERROR` 等）。 |
| `llvm/lib/Testing/Support/Error.cpp` | 上述宏的运行期支撑（`TakeError` 把 `Error` 拆成可匹配的结构）。 |
| `llvm/unittests/Support/ErrorTest.cpp` | `Error` 类的单元测试范例，展示 `TEST(...)` 写法。 |
| `llvm/examples/Bye/CMakeLists.txt` | 示例 pass 插件的构建脚本，演示一个可被 lit 测试的产物如何产出。 |
| `llvm/docs/TestingGuide.md` | LLVM 测试基础设施总览文档。 |

## 4. 核心概念与源码讲解

### 4.1 测试体系总览：三大类测试与目录约定

#### 4.1.1 概念说明

在动手写测试之前，先建立一张「测试都在哪里」的地图。官方文档把 LLVM 的测试明确分成三大类，理解这三类的边界，你才知道自己新写的代码该配哪种测试。

- **单元测试（unit tests）**：位于 `llvm/unittests`，用 Google Test 写 C++，直接调库 API。文档原话是「单元测试一般只留给支持库和通用数据结构」。
- **回归测试（regression tests）**：位于 `llvm/test`，用 `lit` 驱动，绝大多数是「一小段 IR/汇编 + `FileCheck` 校验」。文档原话是「我们更倾向于用回归测试来测 IR 上的变换与分析」。
- **整程序测试（whole programs / test-suite）**：不在本仓库，而在独立的 `llvm-test-suite` 仓库，跑真实程序来度量编译质量与性能。

本讲只覆盖前两类——它们是「每次提交前都必须通过」的那部分。

#### 4.1.2 核心流程

一个典型的「修 bug / 加特性」工作流如下：

1. 在 `llvm/test` 下找到一个能复现问题的最小 `.ll`（或新建一个）。
2. 在文件里写 `; RUN: ...` 指明「用什么工具、什么参数跑这条输入」。
3. 用 `; CHECK: ...` 写出「期望输出里应当出现 / 不应当出现」的关键行。
4. 用 `llvm-lit <文件或目录>` 跑它，看到 `PASS` 即通过。
5. 如果改的是某个 C++ 数据结构 / 支持库 API（而不是 IR 变换），则在 `llvm/unittests` 下补一个 `TEST(...)`，用 `llvm-lit llvm/test/Unit` 或直接运行编译出的可执行文件来跑。

#### 4.1.3 源码精读

三类测试的官方定义写在测试总览文档里：

[llvm/docs/TestingGuide.md:25-29](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/TestingGuide.md#L25-L29) —— 这段把测试分成 unit / regression / whole-program 三大类，并强调前两类「期望永远通过、每次提交前都该跑」。

其中「单元测试用 Google Test、放在 `llvm/unittests`」见：

[llvm/docs/TestingGuide.md:38-45](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/TestingGuide.md#L38-L45) —— 明确单元测试针对支持库与数据结构，变换与分析交给回归测试。

而「回归测试由 lit 驱动、位于 `llvm/test`」见：

[llvm/docs/TestingGuide.md:47-53](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/TestingGuide.md#L47-L53) —— 说明回归测试的语言随被测部分而定，并点名 lit 是其驱动工具。

#### 4.1.4 代码实践

**目标：** 在仓库里亲手核对这三类测试的物理位置，建立空间感。

**步骤：**

1. 列出单元测试目录：`ls llvm/unittests`，你会看到 `Support/`、`IR/`、`Bitcode/` 等子目录，每个对应一个被测模块。
2. 列出回归测试目录：`ls llvm/test`，你会看到 `Transforms/`、`CodeGen/`、`Analysis/`、`Feature/` 等。
3. 确认整程序测试不在本仓库：在本仓库里搜索不到 `test-suite` 目录（它在独立的 GitHub 仓库）。

**需要观察的现象：** `llvm/unittests` 下的文件几乎都是 `*Test.cpp`；`llvm/test` 下的文件几乎都是 `.ll` / `.c` / `.s` / `.test`。

**预期结果：** 你能凭目录名直接判断「这个测试在测什么、用什么框架」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 LLVM 不把所有东西都用单元测试来测，而要专门分出回归测试？

**参考答案：** 单元测试要直接调 C++ API，适合测「这个函数/数据结构对不对」；但优化 pass 的正确性是「给定一段 IR，跑完后输出是否等价且更优」这种**端到端**性质，用 `.ll` + `FileCheck` 写一条对照，比手写一堆 C++ 断言更短、更贴近真实使用、也更稳定（不绑内部 API 的实现细节）。

**练习 2：** 你给 `InstCombine` 修了一个化简 bug，应该把测试放在哪？

**参考答案：** 放在 `llvm/test/Transforms/InstCombine/` 下，写一个最小化的 `.ll`，用 `; RUN: opt -passes=instcombine ... | FileCheck %s` 校验化简后的 IR。

---

### 4.2 lit 与 FileCheck：回归测试的发现、执行与校验

#### 4.2.1 概念说明

`lit` 与 `FileCheck` 是一对搭档，但职责完全不同，初学者常把它们混为一谈。

- **`lit` 是「调度器」**：它不知道任何 LLVM 细节，只做四件事——在目录里**发现**测试文件、按后缀/特性**过滤**、把每个测试**交给某种格式（format）去执行**、收集结果并**汇报** PASS/FAIL。它由纯 Python 写成，源码就在 `llvm/utils/lit`。
- **`FileCheck` 是「校验器」**：它是一个独立的 C++ 工具（源码 `llvm/utils/FileCheck/FileCheck.cpp`），读两份输入——一份是「模式文件」（你的测试里的 `; CHECK:` 行），另一份是「待校验输出」（通常是某个工具的 stdout）——然后逐条按顺序匹配，全部命中返回退出码 0，否则返回 1。

一条回归测试把两者串起来：`; RUN: opt ... | FileCheck %s` 里，`lit` 负责把这条 shell 命令跑起来、看退出码决定 PASS/FAIL，而真正「判对错」的是管道末端的 `FileCheck`。

#### 4.2.2 核心流程

`lit` 的主流程可以概括为一条直线（对应 `lit/main.py:main`）：

```
解析命令行参数
        │
        ▼
构造 LitConfig（一个全局配置对象）
        │
        ▼
discovery.find_tests_for_inputs(...)   ← 沿传入的路径扫描文件
        │
        ▼
按 --filter / features / xfail 过滤、排序
        │
        ▼
run_tests(...)                          ← 多线程执行，逐个 test 调 format.execute()
        │
        ▼
汇报 PASS / FAIL / UNSUPPORTED / XPASS
```

每个被发现的测试文件如何执行，取决于它所属测试套件的 `test_format`。对回归测试，这个格式是 **ShTest**：它把测试文件里的 `; RUN:` 行当成一条条 shell 命令，在受控的内部 shell 里执行，命令的退出码决定测试结果。

ShTest 之所以能「无配置」地找到 `opt`、`llc`、`FileCheck` 这些工具，靠的是 `lit.cfg.py` 里登记的**替换（substitutions）**与**工具替换（tool substitutions）**：例如把裸字符串 `opt` 替换成构建目录里 `opt` 的绝对路径。同时，`lit.cfg.py` 还会向 `config.available_features` 里登记一组「特性」（如 `asserts`、`x86-registered-target`），测试文件可用 `; REQUIRES:` / `; UNSUPPORTED:` / `; XFAIL:` 据此声明「本测试只在某特性存在/不存在时才有意义」。

`FileCheck` 这边的工作方式则是一条**有序扫描**：它从模式文件里收集所有 `CHECK` 家族指令，按出现顺序排好，然后在待校验输出里**从上到下、逐条**寻找匹配；每条 `CHECK` 匹配成功后，扫描游标就前进到该匹配点之后，下一条 `CHECK` 只能在此之后继续找。这种「单调前进」是它区别于 `grep` 的关键——`grep` 只问「有没有」，`FileCheck` 还问「按不按顺序、相邻不相邻」。退出码约定为：0 表示全部匹配，1 表示有未匹配项，2 表示用法错误。

#### 4.2.3 源码精读

**`lit` 的命令行入口**只是一个转发：

[llvm/utils/lit/lit.py:1-6](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/lit/lit.py#L1-L6) —— 这个 `lit.py` 仅 `import` 并调用 `lit.main.main`，自身不含任何逻辑。真正干活的是 `main.py`。

**`lit` 的主流程**集中在 `main()` 里，可清晰分成「配置 → 发现 → 过滤 → 执行」四段：

[llvm/utils/lit/lit/main.py:25-49](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/lit/lit/main.py#L25-L49) —— 先 `parse_args()` 解析命令行，再用结果构造一个 `LitConfig`（progname、是否 Windows、参数字典、超时等全局设置）。这个 `LitConfig` 会贯穿后续所有阶段。

[llvm/utils/lit/lit/main.py:51-56](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/lit/lit/main.py#L51-L56) —— `find_tests_for_inputs` 沿用户传入的路径递归扫描文件，按各套件配置的后缀筛选出测试；若一个都没发现就直接以退出码 2 报错。

[llvm/utils/lit/lit/main.py:119-120](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/lit/lit/main.py#L119-L120) —— 经过过滤、排序后，`run_tests(...)` 真正多线程执行所有选中测试并计时。

**ShTest 格式**把测试文件解释成 shell 流水线：

[llvm/utils/lit/lit/formats/shtest.py:7-17](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/lit/lit/formats/shtest.py#L7-L17) —— 类文档串直白说明：ShTest 是「一文件一测试」的主格式，文件里包含「若干条类 shell 命令流水线 + 对输出的断言」。

[llvm/utils/lit/lit/formats/shtest.py:37-44](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/lit/lit/formats/shtest.py#L37-L44) —— `execute()` 仅把执行委托给 `lit.TestRunner.executeShTest(...)`，后者才是解析 `; RUN:` 并在内部 shell 跑命令的核心。

**回归测试套件的配置**——这是理解「为什么测试里写裸 `opt` 就能跑」的关键：

[llvm/test/lit.cfg.py:18](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L18) —— 本套件名为 `"LLVM"`。

[llvm/test/lit.cfg.py:29](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L29) —— 用 `ShTest` 作为测试格式。

[llvm/test/lit.cfg.py:33](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L33) —— 声明哪些后缀算测试文件：`.ll`、`.c`、`.test`、`.txt`、`.s`、`.mir`、`.yaml`、`.spv`。这就是为什么你随手建一个 `.ll` 就会被 lit 认作测试。

[llvm/test/lit.cfg.py:88](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L88) —— `test_exec_root` 设为构建目录下的 `test/`，即测试**执行**时的当前工作目录（区别于源码里的 `test_source_root`）。这解释了为什么相对路径工具能被找到。

[llvm/test/lit.cfg.py:226-241](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L226-L241) —— `tools` 列表登记了所有需要做路径替换的工具名，比如 `ToolSubst("%lli", FindTool("lli"), ...)` 会把测试里的 `%lli` 替换成构建产物里 `lli` 的绝对路径；后面那一长串裸字符串（`"opt"`、`"llc"`、`"llvm-as"` …）也会被自动替换。

[llvm/test/lit.cfg.py:487](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L487) —— `add_tool_substitutions(tools, config.llvm_tools_dir)` 真正把上面的登记落实到替换表里。

**特性（features）与 `REQUIRES` 机制**——让测试能按构建环境自我裁剪：

[llvm/test/lit.cfg.py:491-494](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L491-L494) —— 每个被编译进来的目标架构都会登记一个 `<arch>-registered-target` 特性。于是测试可以写 `; REQUIRES: x86-registered-target`，表示「只有编了 X86 后端时本测试才有意义」，否则 lit 会把它标记为 `UNSUPPORTED` 而非 `FAIL`。

[llvm/test/lit.cfg.py:754-759](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/lit.cfg.py#L754-L759) —— 用 `llvm-config --assertion-mode` / `--build-mode` 的输出动态登记 `asserts`、`debug` 特性，让测试能在「仅断言构建」或「仅 Debug 构建」时才运行。

**`FileCheck` 工具本体**——先看它如何读入两份文件、如何用退出码表态：

[llvm/utils/FileCheck/FileCheck.cpp:9-14](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/FileCheck/FileCheck.cpp#L9-L14) —— 顶部注释写死了退出码契约：用法错误返回 2、全部匹配返回 0、有未匹配返回 1。这正是 `lit` 据以判定 PASS/FAIL 的依据。

[llvm/utils/FileCheck/FileCheck.cpp:33-38](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/FileCheck/FileCheck.cpp#L33-L38) —— `CheckFilename`（位置参数，模式文件，即你的测试 `%s`）与 `InputFilename`（待校验输入，默认 stdin）。这正是「读两份文件」的两个入口。

[llvm/utils/FileCheck/FileCheck.cpp:40-46](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/utils/FileCheck/FileCheck.cpp#L40-L46) —— `--check-prefixes` 选项：默认模式前缀是 `CHECK`，但你可以改用别的前缀（如 `CHECK-X86`），从而在同一份输入上做多套互不干扰的校验。

**`CHECK` 家族指令的语义**在手册里有逐条定义，这里挑最常用的四条：

- `CHECK:` —— 在当前游标之后寻找本行模式（默认任意位置匹配，不必整行相等）。
- `CHECK-NEXT:` —— 期望上一条 `CHECK` 命中的**紧接着的下一行**就是本模式（强约束相邻）。
- `CHECK-NOT:` —— 在两条正向检查之间（或首条之前 / 末条之后）**不得**出现本模式，用于断言「某个东西被消除了」。
- `CHECK-LABEL:` —— 一个可「切割」输出的锚点：遇到 `CHECK-LABEL` 会把游标重置，常用于把函数级输出分段校验，避免不同函数的相似行互相误匹配。

[llvm/docs/CommandGuide/FileCheck.rst:464-485](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/CommandGuide/FileCheck.rst#L464-L485) —— 手册对 `CHECK-NOT` 的说明与典型用例：用一段 IR 展示如何断言「某个 `load` 被变换消除了」。

[llvm/docs/CommandGuide/FileCheck.rst:419-443](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/CommandGuide/FileCheck.rst#L419-L443) —— 用 `CHECK-SAME:` 在同一逻辑块内做更稳健校验的示例，说明了「为什么光写 `CHECK: Value: 1` 是脆弱测试」。

**一个最简、完整的回归测试范例**——把上述概念一次性看全：

[llvm/test/Transforms/InstCombine/2008-05-31-AddBool.ll:1-13](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/Transforms/InstCombine/2008-05-31-AddBool.ll#L1-L13) —— 这是本讲最重要的一个文件。逐行拆解：

- 第 1 行 `; NOTE: Assertions have been autogenerated by utils/update_test_checks.py` —— 说明本文件的 `CHECK` 行不是手写的，而是由 `update_test_checks.py` 工具自动生成（LLVM 的常见做法，详见 4.2.4）。
- 第 2 行 `; RUN: opt < %s -passes=instcombine -S | FileCheck %s` —— 把本文件 `%s` 喂给 `opt`，跑 `instcombine`，`-S` 输出文本 IR，再管道给 `FileCheck %s`（`%s` 仍指本文件，FileCheck 从中读取 `CHECK` 模式）。整条命令的退出码就是测试结果。
- 第 6–9 行 `CHECK-LABEL` / `CHECK-SAME` / `CHECK-NEXT` —— 用 `CHECK-LABEL: define i1 @test(` 把校验锚定到 `@test` 这个函数；`[[A:%.*]]` 是「捕获变量」语法（`%.*` 是匹配一个 SSA 名的正则，捕获进变量 `A` 复用），`CHECK-NEXT` 断言化简后紧接着就是 `xor i1` 且 `ret`。
- 第 11–12 行 `%r = add i1 %a, %b` / `ret i1 %r` —— 输入：两个 i1 相加。InstCombine 会把 `add i1` 化简为 `xor i1`（因为对 1 位整数，加法与异或在无进位语义下等价），这正是上面 `CHECK` 期望的输出。

#### 4.2.4 代码实践

**目标：** 从零写一个 `.ll` + `FileCheck` 回归测试，并用 `llvm-lit` 跑通它。

**前置：** 你需要一份已构建好的 LLVM（得到 `opt`、`FileCheck`、`llvm-lit`），参见 [u1-l3 构建系统](u1-l3-build-system.md)。`llvm-lit` 是构建目录里的一个脚本（源码模板见 `llvm/utils/llvm-lit/llvm-lit.in`，它把构建目录的路径「焊」进 lit 配置）。

**步骤：**

1. 在构建目录的测试树里（或任意会被 lit 扫描的位置）新建文件 `constfold.ll`，内容如下（**示例代码**，非项目原有文件）：

   ```ll
   ; RUN: opt -passes=instcombine -S < %s | FileCheck %s

   define i32 @f() {
   ; CHECK-LABEL: define i32 @f
   ; CHECK-NEXT:   ret i32 3
     %a = add i32 1, 2
     ret i32 %a
   }
   ```

2. 用 `llvm-lit` 运行它（在构建目录下）：

   ```bash
   llvm-lit -v constfold.ll
   ```

3. （可选）想看「不写 CHECK 会怎样」：临时把 `; CHECK-NEXT: ret i32 3` 改成 `; CHECK-NEXT: ret i32 999`，再跑一次。

**需要观察的现象：**

- 步骤 2 应输出 `Expected Passes    : 1`，且该测试标记为 `PASS`。`-v` 会打印出 lit 实际执行的命令、退出码以及（失败时的）FileCheck 带注解的输入转储。
- 步骤 3 应变成 `FAIL`，并附上 FileCheck 的差异提示（指出哪一条 `CHECK` 没匹配上、实际输出是什么）。

**预期结果：** `instcombine` 会把常量 `add i32 1, 2` 折叠成 `3`，所以函数体只剩 `ret i32 3`，`CHECK-NEXT: ret i32 3` 命中。**待本地验证**：不同版本/目标的 `opt` 可能给 `CHECK-LABEL` 那行的精确形式略有差异（如是否带 `#0` 之类的属性），若 LABEL 行匹配失败，可放宽为 `; CHECK-LABEL: @f`。

> **进阶提示：** 真实 LLVM 仓库里的 `CHECK` 行几乎都用 `llvm/utils/update_test_checks.py`（简称 UTC）自动生成，而非手写。写好 `; RUN:` 与函数体后，执行 `python llvm/utils/update_test_checks.py --opt-bin opt constfold.ll`，UTC 会替你把正确的 `CHECK-LABEL`/`CHECK-NEXT` 行填进去，避免人眼数行号。本讲第 4.2.3 节引用的范例文件第 1 行的 `NOTE` 正是 UTC 留下的标记。

#### 4.2.5 小练习与答案

**练习 1：** 把 `; RUN: opt < %s -passes=instcombine -S | FileCheck %s` 里的 `| FileCheck %s` 去掉，这个测试还能「判对错」吗？

**参考答案：** 仍能跑，但基本失去意义。去掉 FileCheck 后，lit 只看 `opt` 自身的退出码——只要 `opt` 没崩溃就 PASS，哪怕它把 IR 化简错了也察觉不到。校验逻辑必须靠 FileCheck（或别的断言工具）。

**练习 2：** 为什么仓库里的测试常用 `CHECK-LABEL: define ... @func(` 开头，而不是直接 `CHECK: define ...`？

**参考答案：** `CHECK-LABEL` 会切割输出、重置游标。当一个 `.ll` 里有多个函数、且它们的 IR 行彼此相似时，纯 `CHECK:` 可能把 A 函数的输出错配到 B 函数的期望上；用 `CHECK-LABEL` 把校验按函数分段，能避免跨函数误匹配。

**练习 3：** `; REQUIRES: x86-registered-target` 与 `; XFAIL: *` 有何不同？

**参考答案：** `REQUIRES` 声明「不满足条件就跳过（UNSUPPORTED）」，根本不跑；`XFAIL` 声明「预期失败」——仍然会跑，但若失败了记为 `XFAIL`（不算违规），若意外通过了反而记为 `XPASS`（提醒你这条期望可能过时了）。

---

### 4.3 单元测试框架：gtest/gmock 与 llvm/Testing/Support

#### 4.3.1 概念说明

回归测试擅长「输入 IR → 期望输出」，但当你要测的是一个**纯 C++ API**（比如 `Error` 的移动语义、`APInt` 的算术、某种小数据结构的复杂度）时，用 `.ll` 去间接覆盖就太绕了。这时用 Google Test 直接写 C++ 断言更直接。

LLVM 的单元测试有三层：

1. **Google Test（gtest）**：提供 `TEST(套件名, 用例名) { ... }` 写测试、`EXPECT_*` / `ASSERT_*` 做断言。`EXPECT_*` 失败后继续跑本用例剩余语句，`ASSERT_*` 失败立即终止本用例。
2. **Google Mock（gmock）**：在 gtest 之上提供「匹配器（matcher）」与 `EXPECT_THAT(值, 匹配器)` 语法，让断言更可读、错误信息更丰富。
3. **`llvm/Testing/Support`**：LLVM 自己加的一层薄胶水，把 `llvm::Error` / `llvm::Expected<T>` 这种「可成功也可失败」的类型，适配成 gmock 能匹配的对象，于是你可以写 `EXPECT_THAT_ERROR(foo(), Succeeded())`。

这些单元测试编译成可执行文件（每个 `llvm/unittests/<模块>` 一个），由 lit 用 `GoogleTest` 格式驱动——也就是说，**单元测试也跑在 lit 之下**，只是用了与回归测试不同的 format。

#### 4.3.2 核心流程

一个单元测试从编写到运行：

```
在 llvm/unittests/<模块>/FooTest.cpp 里写 TEST(Foo, Bar) { EXPECT_EQ(...) }
              │
              ▼
CMake 把它链上 gtest/gmock + 被测库，产出一个可执行文件（如 SupportTests）
              │
              ▼
llvm/test/Unit/lit.cfg.py 用 GoogleTest 格式发现并执行这些可执行文件
              │
              ▼
gtest 可执行文件自身输出 PASS/FAIL，lit 汇总
```

针对 `Error`/`Expected`，典型断言借助 `llvm/Testing/Support/Error.h` 提供的宏与匹配器：

- `EXPECT_THAT_ERROR(E, Succeeded())` —— 断言 `Error E` 是成功。
- `EXPECT_THAT_ERROR(E, Failed())` —— 断言 `Error E` 是失败。
- `EXPECT_THAT_ERROR(E, FailedWithMessage("..."))` —— 断言失败且错误信息匹配。
- `EXPECT_THAT_EXPECTED(ValOrErr, HasValue(42))` —— 断言 `Expected<T>` 成功且值等于 42。

其底层由 `TakeError(Error)` 把「只能移动、不可拷贝」的 `Error` 拆成一个可被 gmock 匹配的 `ErrorHolder`（一组 `ErrorInfoBase`），从而绕开 `Error` 不能拷贝的限制。

#### 4.3.3 源码精读

**单元测试套件如何被 lit 驱动**——用 GoogleTest 格式，而非 ShTest：

[llvm/test/Unit/lit.cfg.py:11](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/Unit/lit.cfg.py#L11) —— 本套件名为 `"LLVM-Unit"`，与回归套件 `"LLVM"` 区分开。

[llvm/test/Unit/lit.cfg.py:14](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/Unit/lit.cfg.py#L14) —— `config.suffixes = []`：单元测试**不按文件后缀**发现，因为它的「测试」是一个个编译出的可执行文件，由 GoogleTest 格式自行枚举。

[llvm/test/Unit/lit.cfg.py:18-19](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/Unit/lit.cfg.py#L18-L19) —— `test_exec_root` 指向构建目录下的 `unittests/`，源码根与执行根相同（单元测试没有「源码侧」与「执行侧」的分离）。

[llvm/test/Unit/lit.cfg.py:22-26](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/Unit/lit.cfg.py#L22-L26) —— `test_format = lit.formats.GoogleTest(...)`：GoogleTest 格式会扫描执行目录里的可执行文件（约定名字形如 `*Tests`），以 gtest 自身的分片/过滤机制驱动它们。lit 主流程里也确实 `from lit.formats.googletest import GoogleTest` 导入了它。

**`llvm/Testing/Support` 提供的匹配宏**——让 gmock 能匹配 `Error`：

[llvm/include/llvm/Testing/Support/Error.h:163-166](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Testing/Support/Error.h#L163-L166) —— `EXPECT_THAT_ERROR(Err, Matcher)` 宏：本质是 `EXPECT_THAT(llvm::detail::TakeError(Err), Matcher)`。注意它先把 `Err` 喂给 `TakeError`，因为 `Error` 只能移动、不能被 gmock 直接持有。

[llvm/include/llvm/Testing/Support/Error.h:189-192](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Testing/Support/Error.h#L189-L192) —— `EXPECT_THAT_EXPECTED` 宏：对 `Expected<T>` 做同样的「取值 + 匹配」，注释里还附了一段 `myDivide` 的示例，展示 `Succeeded()` / `HasValue(2)` / `Failed()` / `FailedWithMessage(...)` 的典型用法。

[llvm/include/llvm/Testing/Support/Error.h:194-195](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Testing/Support/Error.h#L194-L195) —— `MATCHER(Succeeded, "")` 与 `MATCHER(Failed, "")`：用 gmock 的 `MATCHER` 宏定义两个最常用匹配器，分别判断 `ErrorHolder` 是否成功。

**`TakeError` 的实现**——把不可拷贝的 `Error` 拆成可匹配结构：

[llvm/lib/Testing/Support/Error.cpp:13-20](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Testing/Support/Error.cpp#L13-L20) —— `TakeError` 用 `handleAllErrors` 把 `Error` 里携带的每个 `ErrorInfoBase` 抽出来塞进 `vector<shared_ptr<ErrorInfoBase>>`，再包成 `ErrorHolder` 返回。这样 gmock 匹配器拿到的就是一个普通可拷贝的对象，从而能去匹配「错误类型」「错误信息」等属性。

**一个真实的单元测试范例**——`ErrorTest.cpp` 展示了标准写法：

[llvm/unittests/Support/ErrorTest.cpp:103-106](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/unittests/Support/ErrorTest.cpp#L103-L106) —— 最简用例：`TEST(Error, CheckedSuccess) { Error E = Error::success(); EXPECT_FALSE(E) << ...; }`。`TEST(套件, 用例)` 定义一个测试点；`EXPECT_FALSE(E)` 在 `E` 是成功 `Error` 时为真（成功 `Error` 转布尔为 false）。这条测试还在守护一个不变量：成功的 `Error` 必须被「检查」过，否则在断言构建里会 abort。

[llvm/unittests/Support/ErrorTest.cpp:27-55](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/unittests/Support/ErrorTest.cpp#L27-L55) —— 自定义错误类型 `CustomError`：继承 `ErrorInfo<CustomError>`，实现 `log()` 与 `convertToErrorCode()`，并持有静态 `ID`。这是「定义一种可被 `Error` 携带的自定义错误」的标准范式，后续用例（如 `IsAHandling`）正是基于它来测试 `Error::isA<T>` 与匹配器。

**一个可被 lit 测试的产物如何构建**——以示例 pass 插件 Bye 为例：

[llvm/examples/Bye/CMakeLists.txt:9-19](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/CMakeLists.txt#L9-L19) —— `add_llvm_pass_plugin(Bye Bye.cpp ...)` 把 `Bye.cpp` 编译成一个 pass 插件（动态库）。注意它故意**不**链 `Support`/`Core` 库——这些符号将由「加载该插件的进程」（即 `opt`）在运行期提供。这个插件正是回归测试里 `%loadnewpmbye` 替换的目标（见 `lit.cfg.py` 中对 `%loadbye`/`%loadnewpmbye` 的登记），把「构建产物」与「lit 测试」串了起来。本例同时是 [u4-l4](u4-l4-write-your-own-pass.md) 与下一讲 [u9-l2 Pass 插件机制](u9-l2-pass-plugins.md) 的伏笔。

#### 4.3.4 代码实践

**目标：** 通过阅读已有单元测试，掌握 `TEST(...)` + `EXPECT_THAT_ERROR` 的写法（无需编译即可完成；若你有构建环境，可进一步编译运行）。

**步骤：**

1. 打开 [llvm/unittests/Support/ErrorTest.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/unittests/Support/ErrorTest.cpp)，定位 `TEST(Error, CheckedSuccess)`（第 103 行起）与 `TEST(Error, IsAHandling)`（第 159 行起）。
2. 阅读这两个用例，回答：它们各用了哪种断言（`EXPECT_FALSE` / `EXPECT_THAT_ERROR` + 匹配器）？为什么 `CheckedSuccess` 不需要匹配器？
3. （可选，需构建环境）在构建目录下执行 `llvm-lit llvm/test/Unit/Support` 或直接运行编译出的 `SupportTests` 可执行文件，观察 gtest 风格的 `[==========] Running N tests` 输出。

**需要观察的现象：**

- `CheckedSuccess` 只关心「成功与否」，所以用 `EXPECT_FALSE(E)`（成功 Error 转 bool 为 false）即可，不必取错误信息。
- `IsAHandling` 要区分「错误的具体类型」，所以必须用 `EXPECT_THAT_ERROR(E, Failed<CustomError>(...))` 这种带类型的匹配器。

**预期结果：** 你能说清「何时用裸 `EXPECT_*`、何时用 `EXPECT_THAT_ERROR` + 匹配器」。**待本地验证**：不同构建下 `unittests` 可执行文件的具体路径与名字可能略有不同（如 `SupportTests` vs `LLVMSupportTests`），以你构建目录下 `unittests/Support/` 实际产物为准。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 LLVM 要专门写一层 `llvm/Testing/Support/Error.h`，而不是直接用 gtest 的 `EXPECT_TRUE`？

**参考答案：** `llvm::Error` 是只能移动、不可拷贝的类型，且「成功/失败」之外还携带错误类型与信息。gtest 原生断言既无法直接持有它，也无法表达「失败且错误信息等于 X」这种语义。`Testing/Support` 用 `TakeError` 把它拆成可拷贝的 `ErrorHolder`，再配上 `Succeeded()`/`Failed()`/`FailedWithMessage(...)` 等匹配器，才能写出既正确又可读的断言。

**练习 2：** `EXPECT_*` 与 `ASSERT_*` 在单元测试里该如何选择？

**参考答案：** 当后续断言**依赖**当前断言成立时用 `ASSERT_*`（比如先断言指针非空，再解引用——若为空应立即停止本用例，避免段错误）；当各断言相互独立、希望一次看到全部失败时用 `EXPECT_*`。

**练习 3：** 单元测试可执行文件是「自己跑」还是「lit 跑」？

**参考答案：** 两者皆可，但 LLVM 的官方入口是 lit：`llvm/test/Unit/lit.cfg.py` 用 `GoogleTest` 格式发现 `unittests/` 下的可执行文件并驱动它们，于是 `llvm-lit llvm/test/Unit` 会把所有单元测试纳入统一的 PASS/FAIL 汇总。直接运行单个可执行文件（如 `./SupportTests`）则在调试单个用例时更方便。

---

## 5. 综合实践

把本讲三块知识串成一个完整闭环：**给一个 pass 配回归测试 + 给一个 API 配单元测试**。

**背景：** 假设你写了（或在 [u4-l4](u4-l4-write-your-own-pass.md) 里写过）一个最简单的「统计函数内指令条数」的 pass。现在要为它建立两层防护。

**任务 A（回归测试，主任务）：**

1. 新建 `count-insns.ll`（**示例代码**）：

   ```ll
   ; RUN: opt -passes=your-pass-name -stats -S < %s 2>&1 | FileCheck %s

   define i32 @g(i32 %x) {
   ; CHECK-LABEL: define i32 @g
     %a = add i32 %x, 1
     %b = mul i32 %a, 2
     ret i32 %b
   }
   ; CHECK: {{.*}} instructions
   ```

   - 若你的 pass 用 `-stats` 打印了形如 `N instructions` 的统计行（参见 [u4-l4](u4-l4-write-your-own-pass.md) 中的 `STATISTIC` 宏），上面的 `; CHECK: {{.*}} instructions` 会命中。
   - 把 `your-pass-name` 换成你 pass 的注册名；若 pass 还未注册到 `-passes`，可先用 `opt -load-pass-plugin=...`（见 [u9-l2](u9-l2-pass-plugins.md)）的方式加载。

2. 用 `llvm-lit -v count-insns.ll` 运行，确认 `PASS`。
3. 故意把函数体里删掉一条指令，重跑，观察统计数变化是否被 `CHECK` 捕捉（若你的 `CHECK` 写死了具体数字，此时应 `FAIL`，这正是回归测试的价值）。

**任务 B（单元测试，扩展任务）：**

如果你的 pass 暴露了某个可单独测试的辅助函数（例如「给定一条指令，判断它是否可被安全计数」），在 `llvm/unittests/<你的模块>/` 下新建 `MyPassTest.cpp`，写一个 `TEST(MyPass, CountsCorrectly) { EXPECT_EQ(countInsns(...), 2u); }`，仿照 [ErrorTest.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/unittests/Support/ErrorTest.cpp) 的结构（include gtest、`using namespace llvm;`、`TEST(...)`）。用 `llvm-lit llvm/test/Unit` 验证。

**完成标志：** 你能用一条 `llvm-lit` 命令同时跑通回归测试与单元测试，并在故意引入 bug 时看到相应的 `FAIL`。

## 6. 本讲小结

- LLVM 测试分三大类：`llvm/unittests` 的单元测试（gtest）、`llvm/test` 的回归测试（lit + FileCheck）、以及独立仓库的整程序测试（test-suite）；前两类「每次提交前必过」。
- `lit` 是纯 Python 的**调度器**，主流程为「解析参数 → 发现测试 → 过滤 → 执行 → 汇报」；它本身不懂 LLVM，靠 `lit.cfg.py` 里的工具替换与特性登记把测试环境配齐。
- 回归测试用 **ShTest** 格式，把 `; RUN:` 行当 shell 流水线执行；裸 `opt`/`llc` 之所以能被找到，是因为 `lit.cfg.py` 把它们替换成了构建目录里的绝对路径。
- `FileCheck` 是独立的**校验器**，靠「单调前进的有序扫描」工作，退出码 0/1/2 分别表示全匹配 / 有未匹配 / 用法错误；`CHECK` 家族（`CHECK`/`CHECK-NEXT`/`CHECK-NOT`/`CHECK-LABEL`/`CHECK-DAG` 等）覆盖了相邻、否定、分段等校验需求。
- 单元测试基于 gtest/gmock，`llvm/Testing/Support` 提供了把不可拷贝的 `Error`/`Expected` 适配为可匹配对象的胶水（`TakeError` + `EXPECT_THAT_ERROR` 等宏）；单元测试也跑在 lit 之下，由 `GoogleTest` 格式驱动。
- `REQUIRES`/`UNSUPPORTED`/`XFAIL` 配合 `available_features`，让测试能按构建环境自我裁剪；真实仓库的 `CHECK` 行多由 `update_test_checks.py` 自动生成。

## 7. 下一步学习建议

- **[u9-l2 Pass 插件机制](u9-l2-pass-plugins.md)：** 本讲的 Bye 示例（`add_llvm_pass_plugin`）会在那里展开成「不重编 LLVM 即可把 pass 注入 `opt`」的完整机制，并继续用 `%loadnewpmbye` 这类 lit 替换做端到端测试。
- **深入 lit：** 阅读 `llvm/utils/lit/lit/TestRunner.py`（`executeShTest` 的实现）与 `llvm/utils/lit/lit/formats/googletest.py`，理解内部 shell 的命令解析与 gtest 分片细节。
- **深入 FileCheck：** 通读 `llvm/docs/CommandGuide/FileCheck.rst` 的「Numeric Variables」「CHECK-DAG 排序约束」等小节，并在 `llvm/utils/FileCheck/FileCheck.cpp` 中对照其实现。
- **测试编写规范：** 阅读各 `llvm/test/*/lit.local.cfg.py`，理解子目录如何收窄后缀与特性；参考 `llvm/docs/TestingGuide.md` 的「Regression Tests」「How to write a test」章节。
