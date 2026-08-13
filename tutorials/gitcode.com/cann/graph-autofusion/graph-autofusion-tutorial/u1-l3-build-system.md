# 一键构建系统 build.sh 与 CMake 工程

## 1. 本讲目标

本讲是 graph-autofusion 的「构建系统」讲。读完本讲后，你应该能够：

- 看懂 `build.sh` 提供的全部选项，并知道不加任何选项时它会报错、默认值分别是什么。
- 理解「模块（module）× 实现（impl）× 动作（ut/st）」三维选择体系，能写出只跑某一个模块某一种测试的命令。
- 理解 `-f <变更文件清单>` 如何根据改动落在 `super_kernel/` 还是 `autofuse/` 来**智能跳过**无关组件的构建与测试。
- 解释为什么项目约定编译时加 `-j 8`（Autofuse 是大型 C++ 工程，并行度过高会 OOM），以及 `build.sh` 如何把 `-j` 与 `--no-autofuse` 透传给底层 CMake。
- 建立 `build.sh`（Bash 编排层）与 `CMakeLists.txt`（构建描述层）之间的桥接关系。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念。本讲承接 [u1-l2 仓库目录结构与组件关系](u1-l2-repo-structure.md) 已经建立的认知：仓库有两个自包含组件 `autofuse/` 与 `super_kernel/`，顶层 `CMakeLists.txt` 用 `add_subdirectory` 把它们装配起来，其中 `autofuse` 受 `BUILD_AUTOFUSE` 开关控制。

- **构建系统（build system）**：把「人类写的源码」变成「机器能装的软件包」的自动化流水线。本项目有两层：外层是 Bash 脚本 `build.sh`，负责解析命令行选项、决定「要不要编译、要不要测试、要不要打包」；内层是 CMake 工程，负责真正的编译链接。`build.sh` 是「调度员」，CMake 是「施工队」。
- **选项（option/flag）**：命令行里以 `-` 或 `--` 开头的参数，例如 `--pkg`、`-u`、`-j 8`，用来告诉脚本你想干什么。
- **UT / ST**：UT（Unit Test，单元测试）测单个函数/类；ST（System Test，系统测试）测端到端行为。本项目的测试还区分 `py`（Python 实现）和 `cpp`（C++ 实现）两种「实现（impl）」。
- **run 包（`.run`）**：CANN 生态特有的一种自解压、自安装的可执行包，扩展名为 `.run`。`build.sh --pkg` 的最终产物就是一个 `.run` 包。
- **OOM（Out Of Memory，内存耗尽）**：进程申请的内存超过机器可用内存，被系统杀死。Autofuse 是体量很大的 C++ 工程，并行编译时每个 `g++` 进程都会吃很多内存，并发数太高就容易 OOM。

> 一个贯穿全讲的直觉：`build.sh` 本质是一张「**模块 × 动作 → 处理函数**」的路由表加上一套「**按改动范围决定跳过谁**」的智能判断。抓住这两点，整个脚本就清晰了。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `build.sh` | 外层 Bash 编排脚本，整个构建系统的入口 | 选项解析、模块/实现选择、智能跳过、`-j` 约束、调用 CMake |
| `CMakeLists.txt` | 顶层 CMake 工程描述 | `add_subdirectory` 装配两个组件、`BUILD_AUTOFUSE` 开关 |
| `docs/zh/build.md` | 官方构建说明文档 | 编译命令样例、`.run` 产物、UT/ST/覆盖率命令、第三方依赖下载 |
| `docs/zh/skill-reuse-guide.md` | 工程效率指南 | 明确提出「所有编译命令必须加 `-j 8` 避免 OOM」的约定 |
| `scripts/test/run_autofuse_test.sh` | Autofuse 测试的实际执行脚本 | `build.sh` 最终会把 autofuse 的测试委派给它 |

> 提示：本讲引用的所有行号基于当前 HEAD `00627d97`。`build.sh` 是一个单文件脚本，全文 764 行，逻辑自顶向下：变量初始化 → 工具函数 → 选项解析 → 各动作函数 → `main` 编排。

## 4. 核心概念与源码讲解

### 4.1 build.sh 选项解析与默认行为

#### 4.1.1 概念说明

`build.sh` 是仓库根目录的一个 Bash 脚本，第一行的 `set -e`（[build.sh:12](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L12)）意味着**任何命令返回非零就立即退出**——这是构建脚本常见的「出错即停」策略，避免错误被后续输出淹没。

脚本的选项解析遵循一个朴素而严格的设计：

- **不传任何选项直接报错**。`build.sh` 不会「猜」你想干什么，必须显式声明至少一个动作（打包 / 测试 / 跑示例）。
- **每个开关都有明确默认值**，集中定义在 `checkopts()` 函数开头。
- **用 `getopt` 做规范解析**，支持短选项（`-u`）和长选项（`--ut`）两种写法。

#### 4.1.2 核心流程

选项解析的核心流程（伪代码）：

```
main "$@"                          # 入口
  └─ checkopts "$@"                # 解析选项
       ├─ 若 $# -eq 0 → 报错退出    # 不允许空选项
       ├─ 初始化各开关默认值        # ENABLE_BUILD_PACKAGE="off" ...
       ├─ getopt 规范化参数         # 把 -u/--ut 统一成可遍历的 token
       ├─ while 遍历每个 token
       │    └─ case 分发到对应开关
       ├─ normalize_test_selection # 把 module×impl×ut/st 展开成动作清单
       └─ 若既不打包也无动作 → 报错退出
```

完整的选项清单（来自 usage 文本）如下：

| 选项 | 作用 | 默认 |
|---|---|---|
| `-h, --help` | 打印用法 | — |
| `--pkg` | 构建 run 包 | off |
| `--no-autofuse` | 跳过 autofuse 后端的构建/打包/产物 | off（即默认开启 autofuse） |
| `-j <N>` | 编译线程数 | 见 4.4 节 |
| `-u, --ut` | 跑单元测试 | off |
| `-s, --st` | 跑系统测试 | off |
| `-c, --coverage` | 测试时生成覆盖率报告 | off |
| `--impl=<py\|cpp\|all>` | 选择实现 | all |
| `--module=<name>` | 选择模块 | all |
| `--test_case=<过滤>` | C++ UT 的 gtest 过滤器 | 空 |
| `--run_example` | 跑模块示例 | off |
| `--output_path=<PATH>` | 产物输出目录 | `./build_out` |
| `--cann_3rd_lib_path=<PATH>` | 第三方依赖路径 | `./output/third_party` |
| `--build-type=<Debug\|Release>` | 构建类型 | Release |
| `--pkg-type=<run\|rpm\|deb>` | 包类型 | run |
| `-f <FILE>` | 变更文件清单（触发智能跳过，见 4.3） | 空 |

#### 4.1.3 源码精读

**默认值集中定义**。所有开关的初值在 `checkopts()` 开头一目了然，便于阅读：

[build.sh:311-321](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L311-L321) —— 这里把打包、UT、ST、覆盖率、示例、实现模式、目标模块、Autofuse 开关、变更文件、包类型全部初始化为安全默认值（默认 `ENABLE_AUTOFUSE="on"`，即默认会构建 Autofuse）。

**用 getopt 做规范解析**：

[build.sh:323](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L323) —— `getopt -a -o j:huscf: -l help,pkg,autofuse,no-autofuse,impl:,module:,test_case:,run_example,ut,st,coverage,output_path:,cann_3rd_lib_path:,build-type:,pkg-type:`。其中 `-o j:huscf:` 定义短选项（`:` 表示该选项需要带参数，如 `j:` 表示 `-j` 后面要跟数字、`f:` 表示 `-f` 后面要跟文件名），`-l` 定义长选项。`getopt` 会把用户输入规范化成统一的 token 序列，再交给后面的 `while/case` 循环分发。

**空选项直接报错**：

[build.sh:305-309](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L305-L309) —— `if [ $# -eq 0 ]` 时打印「没有可用选项」并退出。这解释了为什么不能直接 `sh build.sh`。

**「无动作可执行」的最终校验**：解析完所有选项后，如果既没有 `--pkg`、展开后的动作清单 `EXEC_ACTIONS` 也为空，就报错：

[build.sh:462-466](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L462-L466) —— 这能捕获「选了一个不支持某动作的模块」这类错误，例如对 `autofuse_e2e` 请求 `cpp_ut`（该模块只有 `all_st`，没有 cpp ut）。

#### 4.1.4 代码实践

**实践目标**：亲手读一遍真实的 usage 输出，而不是只看讲义转述。

操作步骤：

1. 在仓库根目录执行 `sh build.sh --help`（或 `bash build.sh -h`）。
2. 对照本讲 4.1.2 的选项表，逐条核对终端打印的选项是否一致。
3. 再执行一次 `sh build.sh`（不带任何参数），观察报错信息。

需要观察的现象：

- `--help` 输出的「Options」段会列出全部选项，其中 `-f <FILE>` 的说明里写明了四种智能跳过场景（见 4.3 节）。
- 不带参数执行时，会打印 `ERROR: 'build.sh' has no options available...` 并退出码非 0。

预期结果：你能仅凭 `--help` 输出复述出至少 8 个选项的用途。

> 待本地验证：不同 shell（`sh` vs `bash`）下 `getopt` 的行为可能略有差异；若遇到解析异常，建议统一用 `bash build.sh ...`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build.sh` 设计成「不传选项就报错」，而不是默认执行某个动作？

**参考答案**：构建/测试/打包是耗时且副作用不同的操作，脚本无法安全地猜测用户意图。强制显式声明动作（[build.sh:305-309](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L305-L309)）可以避免「我以为只是看看，结果触发了一次完整编译」这类意外。

**练习 2**：`--build-type` 接受哪些值？默认是什么？

**参考答案**：接受 `Debug` 或 `Release`，默认 `Release`（默认值见 [build.sh:20](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L20)，校验见 [build.sh:406-410](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L406-L410)）。

---

### 4.2 模块与实现选择

#### 4.2.1 概念说明

`build.sh` 把「要测什么」拆成三个正交维度：

- **模块（module）**：顶层逻辑单元。脚本支持四个模块：`superkernel`、`autofuse_framework`、`autofuse_ascendc_api`、`autofuse_e2e`。
- **实现（impl）**：`py`（Python）、`cpp`（C++）、`all`（两者都跑）。
- **动作**：`ut`（单元测试）、`st`（系统测试）、`py_run_example`（跑示例）。

并非所有「模块 × 实现 × 动作」组合都存在。例如 `superkernel` 有 `py_ut`、`cpp_ut`、`py_st`，但 `autofuse_e2e` 只有 `all_st`。脚本用一张**路由表** `MODULE_ACTION_HANDLERS` 声明哪些组合有效、各自交给哪个函数处理。

#### 4.2.2 核心流程

模块选择的核心是「声明有效组合 → 展开用户请求 → 跳过无效组合」：

```
1. SUPPORTED_MODULES         # 声明 4 个合法模块
2. MODULE_ACTION_HANDLERS    # 声明 "模块:动作" → 处理函数 的映射
3. 用户传 --module / --impl / -u / -s
4. normalize_test_selection:
     for 每个 impl in (py/cpp/all):
       for 每个 module in (选中的模块):
         action = impl_suite         # 如 "py_ut"
         if 路由表里有 module:action:
            EXEC_ACTIONS += "module:action"
5. main 遍历 EXEC_ACTIONS，调用对应处理函数
```

#### 4.2.3 源码精读

**合法模块清单**：

[build.sh:31](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L31) —— `SUPPORTED_MODULES=("superkernel" "autofuse_framework" "autofuse_ascendc_api" "autofuse_e2e")`。注意 autofuse 被进一步拆成三个测试模块（framework / ascendc_api / e2e），而 superkernel 是一个整体。

**路由表（最关键的数据结构）**：

[build.sh:32-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L32-L42) —— 这是一个 Bash 关联数组（`declare -A`），键是 `"模块:动作"`，值是对应的处理函数名。例如 `["superkernel:py_ut"]="superkernel_py_ut"` 表示「superkernel 模块的 Python 单元测试」交给函数 `superkernel_py_ut()` 处理；`["autofuse_framework:all_ut"]="autofuse_module_test_suite"` 表示 autofuse framework 的（不分 py/cpp 的）单元测试统一交给 `autofuse_module_test_suite()`。

**`--module` 校验与冲突检测**：

[build.sh:375-386](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L375-L386) —— 先用 `is_supported_module` 检查模块名是否合法，再检查是否与之前的选择冲突（不能同时指定两个不同模块）。

**`--impl` 取值校验**：

[build.sh:366-374](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L366-L374) —— 只接受 `py`/`cpp`/`all`，否则报错。

**三维展开（normalize_test_selection）**：

[build.sh:258-301](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L258-L301) —— 这段把用户的高层意图（「我要 superkernel 的 py ut」）展开成具体的动作清单 `EXEC_ACTIONS`。其中 [build.sh:284-288](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L284-L288) 把 `--impl` 的值映射成待遍历的 impl 列表（`all` → `(py cpp all)`），[build.sh:293-295](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L293-L295) 用 `has_module_action_entry` 跳过路由表里不存在的组合，避免「请求了一个不存在的动作」。

**覆盖率的隐式展开**：

[build.sh:265-268](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L265-L268) —— 如果只传了 `-c`（覆盖率）而没传 `-u`/`-s`，脚本会隐式地同时开启 UT 和 ST，再走正常的展开逻辑。这就是为什么 `bash build.sh -c` 也能跑起测试。

#### 4.2.4 代码实践

**实践目标**：通过源码推断一条命令实际会展开成哪些动作。

操作步骤：

1. 阅读 [build.sh:32-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L32-L42) 的路由表。
2. 推断 `bash build.sh -u --module=autofuse_framework` 会展开成哪个动作（提示：默认 `--impl=all`，autofuse 模块的动作键是 `all_ut` 而非 `py_ut`/`cpp_ut`）。
3. 推断 `bash build.sh -u --module=autofuse_framework --impl=py` 是否能跑起来（提示：路由表里有 `autofuse_framework:py_ut` 吗？）。

需要观察的现象：第 3 步会因为路由表里没有 `autofuse_framework:py_ut` 而展开出空动作清单，最终命中 [build.sh:462-466](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L462-L466) 报错 `No supported actions for the requested selection`。

预期结果：理解 autofuse 系列模块用「`all_ut`/`all_st`」整体键，而 superkernel 用「`py_ut`/`cpp_ut`」细分键——这是两者测试组织方式不同的体现。

> 待本地验证：在第 3 步实际运行确认报错信息与你推断一致。

#### 4.2.5 小练习与答案

**练习 1**：写出「只跑 superkernel 的 Python 单元测试」的命令。

**参考答案**：`bash build.sh -u --module=superkernel --impl=py`。必须加 `--impl=py`，否则默认 `--impl=all` 会同时展开 `superkernel:py_ut` 和 `superkernel:cpp_ut`（路由表见 [build.sh:32-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L32-L42)）。

**练习 2**：为什么 `autofuse_e2e` 模块在路由表里只有 `all_st`，没有 `all_ut`？

**参考答案**：`autofuse_e2e` 是端到端（end-to-end）测试模块，按定义只做系统级验证（ST），不提供单元测试。路由表如实地反映了这一业务事实——只登记了 `["autofuse_e2e:all_st"]`（[build.sh:41](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L41)）。

---

### 4.3 智能跳过构建

#### 4.3.1 概念说明

仓库里两个组件 `super_kernel/` 和 `autofuse/` 是解耦的（见 u1-l1、u1-l2）。如果你这次只改了 `super_kernel/` 下的代码，却还要重新编译整个 Autofuse、跑全部测试，纯属浪费时间。`build.sh` 提供了 `-f <FILE>` 选项，传入一个「本次变更文件清单」，脚本会**根据改动落在哪个组件，自动跳过无关组件的构建与测试**。这是 CI（持续集成）场景下节省算力的关键机制。

#### 4.3.2 核心流程

智能跳过的判断流程：

```
-f FILE 提供"变更文件清单"
   │
   ▼
analyze_changed_modules: 逐行扫描每个文件路径
   │   ├─ 命中 README.md/CONTRIBUTING.md/AGENTS.md/docs//examples//.claude//.opencode/ → 忽略
   │   ├─ 命中 super_kernel/  → CHANGED_SUPERKERNEL=true
   │   ├─ 命中 autofuse/      → CHANGED_AUTOFUSE=true
   │   └─ 其它                → CHANGED_OTHER=true
   ▼
apply_module_selection: 根据三个布尔标志组合决策
   ├─ 只有 super_kernel 变 → 关 autofuse 构建 + 跳过 autofuse 测试
   ├─ 只有 autofuse 变     → 跳过 superkernel 测试
   ├─ 只有文档/示例变      → exit 200（完全不构建）
   └─ 其它（含混合/其它）  → 正常构建
```

退出码 `200` 是脚本约定的「无需构建」语义码，CI 可以据此判断「本次改动不需要跑流水线」。

#### 4.3.3 源码精读

**`-f` 选项读取变更清单**：

[build.sh:422-430](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L422-L430) —— `-f` 后跟一个文件路径，脚本用 `cat` 把整个文件内容读进 `CHANGED_FILES` 变量（每行一个文件路径）。

**变更分类（analyze_changed_modules）**：

[build.sh:119-169](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L119-L169) —— 函数先初始化三个标志为 `false`，然后逐个文件用 `grep` 判断路径前缀。注意 [build.sh:133-159](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L133-L159) 把若干「非代码」路径显式跳过（`continue`）：`README.md`、`CONTRIBUTING.md`、`AGENTS.md`、`docs/`、`examples/`、`.claude/`、`.opencode/`。也就是说，只改文档或示例不会触发任何组件的构建标志。随后 [build.sh:161-167](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L161-L167) 按 `super_kernel/` 与 `autofuse/` 前缀置位对应标志。

**决策（apply_module_selection）**：

[build.sh:171-187](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L171-L187) —— 这是「智能」的核心。三个分支：

1. **只有 super_kernel 变**（[build.sh:172-176](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L172-L176)）：`ENABLE_AUTOFUSE="off"` 且 `SKIP_AUTOFUSE_TESTS="on"`——不仅跳测试，连 Autofuse 的编译都整个关掉，这是最强的跳过。
2. **只有 autofuse 变**（[build.sh:177-180](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L177-L180)）：只 `SKIP_SUPERKERNEL_TESTS="on"`。
3. **全是文档/示例**（[build.sh:181-185](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L181-L185)）：`return 200`，调用方据此 `exit 200` 完全不构建。

**main 中应用决策**：

[build.sh:652-709](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L652-L709) —— `main` 调用上述两个函数后，还会做一层更细的判断：即使没命中 apply_module_selection 的强分支，只要某个组件确实没改动（如 [build.sh:663-666](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L663-L666) 检测到没有 autofuse 文件变更就跳过 autofuse 测试）。随后 [build.sh:745-753](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L745-L753) 在真正执行动作循环时，再次根据 `SKIP_*` 标志 `continue` 跳过被排除模块的动作。

#### 4.3.4 代码实践

**实践目标**：用最简单的方式触发并观察智能跳过，理解退出码 200。

操作步骤（纯文件操作，不依赖昇腾环境）：

1. 在仓库根目录创建一个临时清单文件，只含一行文档改动：
   ```bash
   printf 'docs/zh/build.md\n' > /tmp/changed_only_docs.txt
   ```
2. 执行 `bash build.sh -f /tmp/changed_only_docs.txt -u`。
3. 紧接着执行 `echo $?` 查看退出码。
4. 再创建一个只含 super_kernel 改动的清单：
   ```bash
   printf 'super_kernel/src/aot/super_kernel.cpp\n' > /tmp/changed_only_sk.txt
   ```
5. 执行 `bash build.sh -f /tmp/changed_only_sk.txt -u`（如果环境没有 CANN，构建步骤会失败，但你可以先观察日志里是否打印了 `[INFO] Only super_kernel changed, skipping autofuse build and autofuse tests.`）。

需要观察的现象：

- 第 3 步：退出码应为 `200`，且日志打印 `Changed files only contain docs/...`（来自 [build.sh:182-184](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L182-L184)）。
- 第 5 步：日志开头出现 `[INFO] Only super_kernel changed, skipping autofuse build and autofuse tests.`（来自 [build.sh:173](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L173)），证明 Autofuse 构建被整体跳过。

预期结果：你能在不完整编译的情况下，仅凭日志和退出码确认「智能跳过」生效。

> 待本地验证：第 5 步是否真正进入 superkernel 测试取决于是否装好 Python 依赖；但「跳过 autofuse」的 INFO 日志会在任何 CANN 状态下都先打印出来。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 PR 同时改了 `super_kernel/README.md` 和 `autofuse/codegen/codegen.cpp`，`build.sh -f` 会怎么决策？

**参考答案**：`super_kernel/README.md` 路径不以 `super_kernel/` 为前缀吗？注意分类逻辑匹配的是 `^super_kernel/`（[build.sh:161](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L161)），`super_kernel/README.md` 命中该前缀，但它本身又是文档——然而分类函数**只对顶层 `README.md`/`CONTRIBUTING.md`/`AGENTS.md` 做忽略**（[build.sh:133-159](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L133-L159) 用的是 `^README\.md$` 锚定整行），子目录里的 README 不会被忽略。因此 `CHANGED_SUPERKERNEL=true`、`CHANGED_AUTOFUSE=true`，两者都变 → 正常全量构建。

**练习 2**：为什么用退出码 `200` 而不是 `0` 来表示「无需构建」？

**参考答案**：`0` 通常表示「成功完成了一次构建」。用 `200` 这种「非 0 但非典型错误」的码，可以让 CI 区分三种结局：真正成功（0 级别）、无需构建（200）、构建失败（其它非 0）。调用方在 [build.sh:656-658](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L656-L658) 捕获 200 后直接 `exit 200`，把这一语义向上传递。

---

### 4.4 并行度 -j 约束与 CMake 工程桥接

#### 4.4.1 概念说明

本模块讲两件紧密相关的事：`-j`（编译并行度）的约束，以及 `build.sh` 如何把所有高层开关翻译成底层 CMake 调用。

**为什么 `-j` 需要约束**：Autofuse 是一个体量很大的 C++ 工程（图 IR、ASCIR、optimize、ATT、codegen 等大量模板代码）。并行编译时，每个 `g++` 进程都要解析头文件、实例化模板，内存占用动辄数 GB。如果 `-j` 设得太高（例如等于 CPU 逻辑核数 64），几十个 `g++` 同时跑，机器内存会被瞬间打爆（OOM），编译进程被系统杀死。因此项目文档明确约定：**所有编译命令必须加 `-j 8` 来限制并行度，避免 OOM**。

`build.sh` 一方面会校验并钳制 `-j`，另一方面把 `-j`、`--no-autofuse`、`--build-type` 等开关翻译成 CMake 的 `-D` 参数，驱动底层工程。

#### 4.4.2 核心流程

`-j` 与 CMake 桥接的流程：

```
1. 启动时 CPU_NUM = 逻辑核数              # /proc/cpuinfo 探测
   THREAD_NUM 默认 = CPU_NUM
2. 用户传 -j N
   └─ check_param_j(N):
        N 必须是正整数（否则报错）
        若 N > CPU_NUM → 钳制为 CPU_NUM
3. cmake_config():
     组装 cmake_option = 安装路径 + BUILD_TYPE + ...
     若 ENABLE_AUTOFUSE=on  → 追加 -DBUILD_AUTOFUSE=ON
     若 ENABLE_AUTOFUSE=off → 追加 -DBUILD_AUTOFUSE=OFF
     执行 cmake .. <所有选项>
4. build(target):
     cmake --build . --target <target> -j ${THREAD_NUM}
```

#### 4.4.3 源码精读

**CPU 核数探测与默认线程数**：

[build.sh:17-18](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L17-L18) —— `CPU_NUM` 通过统计 `/proc/cpuinfo` 里 `processor` 行的数量得到（Linux 逻辑核数）；`THREAD_NUM` 默认就等于 `CPU_NUM`。

> ⚠️ 文档与实现的细微差异：usage 文本 [build.sh:65](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L65) 写的是「default is 16」，但代码实际默认是「CPU 核数」。以代码为准：在一台 32 核机器上，不传 `-j` 时 `THREAD_NUM=32`，并非 16。这进一步说明：**默认值偏高，所以项目才在文档里强烈建议手动加 `-j 8`**。

**`-j` 校验与钳制（check_param_j）**：

[build.sh:89-106](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L89-L106) —— 三层校验：①必须非空（[build.sh:91-95](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L91-L95)）；②必须是正整数，拒绝 0/负数/非数字（[build.sh:96-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L96-L100)，正则 `^[1-9][0-9]*$`）；③若超过 `CPU_NUM` 则**向下钳制**到 `CPU_NUM`（[build.sh:102-104](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L102-L104)）。注意：脚本只钳制「上限」防止超过物理核数，但**不会主动把默认值降到 8**——降并行度是留给用户的职责。

**OOM 约定的文档证据**：

[docs/zh/skill-reuse-guide.md:147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/skill-reuse-guide.md#L147) —— `af-build-runner` 一栏明确写：「所有编译命令必须加 `-j 8` 限制并行度，避免 OOM」。这是项目级约定，解释了为什么实践中几乎所有 `build.sh` 调用都带 `-j 8`。

**cmake_config：把开关翻译成 CMake 参数**：

[build.sh:469-481](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L469-L481) —— 这是 Bash 层与 CMake 层的「翻译器」。它把 `BUILD_TYPE`、`CANN_3RD_LIB_PATH`、`PACKAGE_TYPE` 拼进 `cmake_option`，并根据 `ENABLE_AUTOFUSE` 追加 `-DBUILD_AUTOFUSE=ON` 或 `-DBUILD_AUTOFUSE=OFF`（[build.sh:473-478](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L473-L478)）。这正是 `--no-autofuse` 落到 CMake 层的最终形式。

> 承接 u1-l2：CMake 顶层用 `option(BUILD_AUTOFUSE "..." ON)` 定义这个开关（[CMakeLists.txt:57](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L57)），并用 `if(BUILD_AUTOFUSE) add_subdirectory(autofuse) endif()`（[CMakeLists.txt:58-60](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L58-L60)）决定是否把 autofuse 装配进工程。`build.sh` 的 `--no-autofuse` 通过 `cmake_config` 注入 `-DBUILD_AUTOFUSE=OFF`，从而让 CMake 跳过整个 `add_subdirectory(autofuse)`。

**build：把 -j 透传给 cmake**：

[build.sh:483-487](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L483-L487) —— `cmake --build . --target ${target} -j ${THREAD_NUM}`。所有经过钳制的 `THREAD_NUM` 都在这里传给 CMake 的构建器。同理，autofuse 测试也会把 `-j` 透传给 `run_autofuse_test.sh`（见 [build.sh:632](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L632)，`test_args+=("${test_option}" -m "${test_module}" -j "${THREAD_NUM}")`）。

**main 中 Autofuse 与打包的实际触发**：

[build.sh:724-735](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L724-L735) —— `if ENABLE_AUTOFUSE == on` 则 `cmake_config` + `build all` 编译 autofuse；`if ENABLE_BUILD_PACKAGE == on` 则 `build_package` 打 `.run` 包。这两段是「构建执行」的主体，注意 autofuse 的编译产物会被打包步骤复用。

#### 4.4.4 代码实践

**实践目标**：理解 `-j` 钳制行为，并验证 `--no-autofuse` 是否真的让 CMake 收到 `BUILD_AUTOFUSE=OFF`。

操作步骤：

1. 查看本机逻辑核数：`grep -c '^processor' /proc/cpuinfo`，记为 `C`。
2. 尝试一个超大 `-j`：`bash build.sh --pkg -j 9999 --no-autofuse 2>&1 | head -30`（注意：这会真正开始打包 superkernel，可在看到 cmake 配置日志后 Ctrl-C 中断）。
3. 在 cmake 配置日志里找两处信息：
   - `Info: cmake config ...` 行（来自 [build.sh:479](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L479)），确认其中包含 `-DBUILD_AUTOFUSE=OFF`。
   - 后续 `cmake --build ... -j N` 行（来自 [build.sh:486](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L486)），确认 `N` 被钳制成了第 1 步的 `C`，而不是 9999。

需要观察的现象：

- `-j 9999` 不会报错（因为 [build.sh:102-104](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L102-L104) 把它钳制到了 CPU 核数）。
- cmake 配置日志里能看到 `-DBUILD_AUTOFUSE=OFF`，证明 `--no-autofuse` 成功穿透到 CMake。

预期结果：你亲眼看到「Bash 选项 → CMake `-D` 参数」的翻译过程。

> 待本地验证：`-j` 钳制后的实际值取决于本机核数；若想在编译阶段避免 OOM，应主动使用 `-j 8`。

#### 4.4.5 小练习与答案

**练习 1**：为什么不传 `-j` 时反而更危险（相比显式 `-j 8`）？

**参考答案**：不传 `-j` 时 `THREAD_NUM` 默认等于 CPU 逻辑核数（[build.sh:17-18](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L17-L18)），在高核数机器上会触发过多并行 `g++`，极易 OOM。显式 `-j 8` 把并行度压到安全水位（约定见 [docs/zh/skill-reuse-guide.md:147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/skill-reuse-guide.md#L147)）。

**练习 2**：`--no-autofuse` 从命令行到最终影响 CMake 工程，中间经过哪几次「翻译」？

**参考答案**：① `checkopts` 把 `--no-autofuse` 置 `ENABLE_AUTOFUSE="off"`（[build.sh:344-347](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L344-L347)）；② `cmake_config` 据此追加 `-DBUILD_AUTOFUSE=OFF`（[build.sh:476-478](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L476-L478)）；③ 顶层 `CMakeLists.txt` 用 `if(BUILD_AUTOFUSE)` 决定是否 `add_subdirectory(autofuse)`（[CMakeLists.txt:56-61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L56-L61)）。三层接力完成解耦。

**练习 3**：`-j 0` 或 `-j -3` 会怎样？

**参考答案**：`check_param_j` 的正则 `^[1-9][0-9]*$` 会拒绝它们，脚本打印 `ERROR: -j only support positive integers...` 并 `exit 1`（[build.sh:96-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L96-L100)）。

---

## 5. 综合实践

**任务**：完成本讲开篇给出的那条综合实践，把本讲四个模块串起来。请先阅读 `sh build.sh --help` 的真实输出，然后写出并解释以下三条命令（不必实际执行完整编译，重点是论证选项的正确性）。

**命令一：只打包（构建 run 包，且为安全起见限制并行度）**

```bash
bash build.sh --pkg -j 8
```

- `--pkg` 触发 [build.sh:733-735](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L733-L735) 的 `build_package`，最终在 `build_out/` 产出 `cann-graph-autofusion_${version}_linux-${arch}.run`（产物路径见 [docs/zh/build.md:152](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L152)）。
- `-j 8` 把并行度限制在安全水位，避免 Autofuse 编译 OOM（约定见 [docs/zh/skill-reuse-guide.md:147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/skill-reuse-guide.md#L147)）。

**命令二：只跑 superkernel 的 Python UT**

```bash
bash build.sh -u --module=superkernel --impl=py
```

- `-u` 开启 UT；`--module=superkernel` 把范围限定在 superkernel；`--impl=py` 进一步限定只跑 Python 实现。
- 之所以必须加 `--impl=py`：superkernel 在路由表里同时登记了 `py_ut` 和 `cpp_ut`（[build.sh:32-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L32-L42)），默认 `--impl=all` 会两者都跑。`--impl=py` 让 `normalize_test_selection`（[build.sh:258-301](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L258-L301)）只展开出 `superkernel:py_ut` 一个动作。
- 该动作最终由 `superkernel_py_ut()`（[build.sh:545-560](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L545-L560)）执行：`pip install -e .[dev]` 后用 `pytest tests/ut -m ut -n auto` 跑测试。

**命令三：跳过 autofuse 打包**

```bash
bash build.sh --pkg --no-autofuse -j 8
```

- `--no-autofuse` 置 `ENABLE_AUTOFUSE="off"`（[build.sh:344-347](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L344-L347)），导致两件事：① [build.sh:724-731](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L724-L731) 的 autofuse 编译段被跳过；② `cmake_config` 向 CMake 传入 `-DBUILD_AUTOFUSE=OFF`（[build.sh:476-478](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L476-L478)），CMake 因此跳过 `add_subdirectory(autofuse)`（[CMakeLists.txt:56-61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L56-L61)）。最终只打包 superkernel 产物。

**关于 `-j 8` 必要性的总结**（综合实践要求）：Autofuse 是大型 C++ 工程，并行编译时每个 `g++` 进程内存占用高；不限制并行度会导致同时运行过多编译进程而 OOM。`build.sh` 默认 `THREAD_NUM=CPU核数`（[build.sh:17-18](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L17-L18)），在高核机器上偏高，因此项目约定手动加 `-j 8`（[docs/zh/skill-reuse-guide.md:147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/skill-reuse-guide.md#L147)）。即便用 `--no-autofuse` 只编译 superkernel，保持 `-j 8` 也是稳妥习惯。

> 实操提醒：若要真正执行命令二/三的测试，请先参照 [docs/zh/build.md:262-263](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md#L262-L263) 的警告——**执行 autofuse 的 UT/ST 前必须先安装编译生成的 `.run` 包**，否则会因加载到旧动态库而报 `undefined symbol`。superkernel 的 Python UT 相对独立，但仍需先 `pip install -e .[dev]`（脚本会自动做）。

## 6. 本讲小结

- `build.sh` 是构建系统的 Bash 编排层：不传选项会报错；所有开关默认值集中在 `checkopts()` 开头（[build.sh:311-321](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L311-L321)），默认 `ENABLE_AUTOFUSE="on"`。
- 「测什么」由「模块 × 实现 × 动作」三维决定，合法组合登记在路由表 `MODULE_ACTION_HANDLERS`（[build.sh:32-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L32-L42)），由 `normalize_test_selection`（[build.sh:258-301](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L258-L301)）展开成动作清单。
- `-f <变更文件清单>` 触发智能跳过：按改动落在 `super_kernel/` 还是 `autofuse/` 决定跳过谁，纯文档/示例改动直接 `exit 200`（[build.sh:171-187](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L171-L187)）。
- `-j` 经 `check_param_j` 校验为正整数并钳制到 CPU 核数（[build.sh:89-106](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L89-L106)）；项目约定手动用 `-j 8` 避免 Autofuse 编译 OOM（[docs/zh/skill-reuse-guide.md:147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/skill-reuse-guide.md#L147)）。
- `cmake_config`（[build.sh:469-481](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L469-L481)）是 Bash 层到 CMake 层的翻译器，把 `--no-autofuse`、`--build-type` 等翻译成 `-D` 参数，最终驱动顶层 `CMakeLists.txt` 的 `add_subdirectory` 装配。

## 7. 下一步学习建议

掌握了构建系统后，建议按以下顺序继续：

1. **u1-l4 环境搭建与快速上板运行**：本讲只讲了「怎么编译」，下一讲讲「怎么把编译出来的 `.run` 包装好、配好 CANN/torch_npu 环境、跑通第一个 Autofuse 用例」，是从「能编译」到「能运行」的关键一步。
2. **动手读 `scripts/test/run_autofuse_test.sh`**：本讲提到 autofuse 的测试最终委派给它（[build.sh:643](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L643)）。它是 Autofuse 测试体系（u12-l1）的核心，可以先浏览它的 `usage()`，对比 `build.sh` 的选项体系异同。
3. **回头看顶层 `CMakeLists.txt` 全文**：本讲只聚焦了 `add_subdirectory` 与 `BUILD_AUTOFUSE`，建议通读这 64 行，理解 `fetch_cann_cmake`、`dependencies.cmake`、`package.cmake` 等公共脚本的引入方式，为后续进入 autofuse 内部模块的 CMake 组织做铺垫。
