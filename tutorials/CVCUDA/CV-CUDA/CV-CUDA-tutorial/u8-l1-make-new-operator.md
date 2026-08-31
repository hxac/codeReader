# 新增一个算子：mkop 脚手架与完成门禁

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整说出 CV-CUDA 新增一个算子要走的全部环节：规格审批 → 脚手架 → 实现 → 测试 → 基准 → 文档 → 完成门禁 → 性能移交。
2. 独立运行 `tools/mkop/mkop.sh` 生成全套骨架，并能逐个说明 11 个生成文件与 11 处接线修改的作用。
3. 理解 `tools/make_op.py` 两个阶段（`scaffold` / `done`）各自的检查项，特别是「头文件契约驱动测试覆盖」的闭环，以及为什么完成门禁故意让未实现的骨架保持红色。
4. 为一个假想算子写出合格的规格：语义定义、参考 oracle、支持矩阵。

## 2. 前置知识

本讲是「二次开发」单元的第一讲，假设你已完成 u5-l1 与 u7-l1。在此基础上补充几个新概念：

- **脚手架（scaffold）**：自动生成一个新模块所需的全部文件骨架，并把它们接入构建系统的脚本产出。CV-CUDA 里新增一个算子要新建约 11 个文件、修改约 11 个现有文件，这些机械动作由 `tools/mkop/mkop.sh` 一次完成。
- **门禁（gate）**：一道机器可复查的「完成定义」。CI 或本地工具用它判定工作是否达标——不达标就非零退出。与「评审清单」的区别在于：门禁是**确定性检查器**，无网络、无时钟、无随机数，同一输入永远得到同一输出（见 [MAKE_OP_GUIDELINES.md:81-82](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L81-L82)）。
- **参考 oracle**：算子语义的权威外部出处，例如「mimic `torchvision.transforms.v2.functional.invert`: `out = dtype_max − in`」、OpenCV `cv::bitwise_not`，或一条显式公式。它的作用是让「正确」有据可依，而不是由实现者自说自话。
- **支持矩阵（Limitations 契约表）**：你在 u3-l1 已经读过它——每个算子 C 头文件里那张 Layout / Channels / Data Type 允许表。本讲的关键新认知是：这张表**同时是人读的文档和机器读的数据库**，完成门禁解析它来决定测试必须覆盖什么。
- **占位符（placeholder）**：模板文件里的 `__OPNAME__`、`__OPNAMELOW__` 等记号，由脚手架按算子名替换后落盘。
- **幂等（idempotent）**：同一脚本重复运行产生相同结果。`mkop.sh` 每次插入前先 `grep -q` 检查是否已存在，所以重复运行不会插出重复行。

承接 u5-l1：算子四层链路（Python 绑定 → C API → priv 实现 → kernel）里的每一层文件，脚手架都会生成；承接 u7-l1：C++ 系统测试的五段式「随机数据 → CPU 金标 → GPU 算子 → 比对」范式的骨架位置，脚手架也会生成——留给你填的正是金标。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`.agents/guidance/MAKE_OP_GUIDELINES.md`](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md) | 新增算子的**唯一权威清单**：spec 步骤、两个阶段的全部检查项（SPEC/SCF/IMP/COV/EXEC/RDY）与注意事项 |
| [`docs/sphinx/advanced/make_operator.rst`](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/make_operator.rst) | 叙事版教程（五步走），偏人工手动流程 |
| [`tools/mkop/mkop.sh`](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh) | 脚手架生成器：模板替换 + 接线插入 |
| `tools/mkop/Public.h` 等 11 个模板 | 骨架的原始材质（本讲精读其中 6 个） |
| [`tools/make_op.py`](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py) | 确定性门禁检查器，实现上面清单的机器可查部分 |
| [`tools/review_op.py`](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py) | 被 `make_op.py` 复用的覆盖检查原语（算子名解析、Limitations 表解析） |

一句话关系：**指南文档定规则，mkop.sh 造骨架，make_op.py 验收**。三者都围绕同一个「契约」——`Op<Name>.h` 头文件。

## 4. 核心概念与源码讲解

### 4.1 契约先行：规格审批与「头文件即单一事实来源」

#### 4.1.1 概念说明

大型算子库最常见的腐烂方式是「支持矩阵漂移」：实现顺手支持了某个 dtype，测试恰好只测了它，文档又写了第三种说法。CV-CUDA 的对策是把顺序倒过来——**先写契约、经人批准、再写代码**：

1. 契约（spec）= 一句话语义 + 引用的参考 oracle + 支持矩阵（dtype × 通道 × 布局 × 容器）。
2. 契约记录在公开 C 头文件 `Op<Name>.h` 的 Doxygen 注释里：`@brief` 写语义，`Reference:` 行写 oracle 出处，Limitations 表写批准过的矩阵。
3. 此后**测试覆盖（COV）、CPU 金标、文档（DOC）全部镜像这份契约**。头文件成为单一事实来源（见 [MAKE_OP_GUIDELINES.md:53-59](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L53-L59)）。

官方工作流分两种模式（[MAKE_OP_GUIDELINES.md:27-40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L27-L40)）：

- **(A) 全程模式** `/make-op <Name>`：提规格 → 用户批准 → 脚手架 → 在头文件记录契约 → 结构门禁 → 实现 → 完成门禁 → 移交性能优化。
- **(B) 仅脚手架模式** `/make-op-scaffold <Name> [--bare]`：生成完整、接线、可编译的骨架后停下，实现留给人类或另一个 AI。`--bare` 表示连规格也一并委托（此时 SPEC 项报 `MANUAL` 而非 `GAP`）。

#### 4.1.2 核心流程

```text
提规格（语义 + oracle + 支持矩阵）
   │
   ▼
用户批准（可修改语义 / 改矩阵 / 显式豁免 oracle 并记录理由）
   │
   ▼
把契约写进 Op<Name>.h（@brief / Reference: / Limitations 表）
   │
   ▼
mkop.sh 生成骨架 ──► make_op.py --phase scaffold（结构与契约门禁）
   │
   ▼
实现（kernel、测试、基准、文档内容）
   │
   ▼
make_op.py --phase done --run（完成门禁）──► 移交 /optimize-op
```

注意一个重要性质：**契约不是越大越好**。头文件里声明的每一个 dtype、每一个通道数，都会在 done 阶段变成一条必须存在的测试与基准（详见 4.4）。所以支持矩阵应按真实需求收窄，而不是照抄「全都要」。

#### 4.1.3 源码精读

先看指南对 Part 0 的三步定义：

- [MAKE_OP_GUIDELINES.md:42-52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L42-L52) 规定：代理**提出**规格（名称、一行语义、**引用的参考 oracle**、API 参数、目标支持矩阵），并在脚手架之前**询问用户批准**；用户可修改语义/oracle/矩阵，或对真正新颖的自定义算子**显式豁免 oracle**（豁免与理由都要记录）。指南原文举的 oracle 例子恰好就是本讲综合实践要用的：`mimic torchvision.transforms.v2.functional.invert: out = dtype_max − in`、OpenCV `cv::bitwise_not`、或显式公式。
- [MAKE_OP_GUIDELINES.md:53-59](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L53-L59) 规定契约落在 C 头文件三处：`@brief` = 语义；`Reference:` 行 = oracle 引用（或 `Reference: custom — oracle waived (<理由>)`）；Limitations 表 = 批准的矩阵。并强调图像算子**默认要含交错（NHWC/HWC）与平面（NCHW/CHW）两种布局**——不适用时必须在头文件里声明 `Planar image layouts: Not applicable` 并给理由。

门禁如何机器检查这三处？看 `make_op.py` 的 `check_spec`：

- [make_op.py:180-181](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L180-L181) 定义了桩短语 `STUB_BRIEF = "Defines types and functions to handle the"`——这正是模板 `Public.h` 里 `@brief` 的原始文本。[make_op.py:209-219](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L209-L219) 的 SPEC-BRIEF 检查：头文件里只要还残留这个短语或 `TBD args`，就判「@brief 仍是 mkop 桩」。
- [make_op.py:222-233](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L222-L233) 的 SPEC-ORACLE 用正则 `Reference:|\bmatches\b|\bmimics\b|oracle waived|no external reference|custom operator` 扫头文件——**写文档时的措辞直接决定机器能否识别**，所以 `Reference:` 行必须按约定格式写。
- [make_op.py:241-257](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L241-L257) 的 SPEC-MATRIX 调用 `review_op.parse_limitations` 解析 Limitations 表，并检查该区域不再含 `TODO`。
- [make_op.py:259-269](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L259-L269) 的 SPEC-CORRECT（「语义确实匹配 oracle、金标确实独立实现」）**永远是 MANUAL**——机器验不了语义等价性，必须人来读。这是整个体系里「确定性与判断力」的分界线。

`parse_limitations` 是「头文件即数据库」的解析端，值得精读（[review_op.py:342-379](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L342-L379)）：

- `Data Layout: [kNHWC, kHWC]` → 布局集合 `{NHWC, HWC}`（正则见 L354-L363，还兼容 `NVCV_TENSOR_[N]CHW` 散文式写法）；
- `Channels: [1, 3, 4]` → 通道集合 `{1, 3, 4}`（L364-L368）；
- dtype 表里每个 `8bit Unsigned | Yes` 行 → 集合加入 `u8`（L369-L376，只认 `Yes`）。

这就是为什么 u3-l2 反复强调「Limitations 契约表格式不能随意改动」——空格、竖线、`Yes/No` 的写法都是解析器的输入。

#### 4.1.4 代码实践

**实践目标**：为假想算子 `invert3` 写出一份合格规格（只写在草稿里，先不进仓库）。

**操作步骤**：

1. 语义定义：`invert3` 对输入图像逐通道取反，输出与输入同形、同 dtype、同布局。
2. 参考 oracle：引用官方指南中现成的例子——`Reference: torchvision.transforms.v2.functional.invert`，即 `out = dtype_max − in`；对 8 位无符号等价于 OpenCV `cv::bitwise_not`。
3. 支持矩阵（练习版，故意保守）：
   - Layout：`[kNHWC, kHWC, kNCHW, kCHW]`（图像算子默认双布局）
   - Channels：`[3]`（假想算子只服务三通道图）
   - Data Type：仅 `8bit Unsigned = Yes`，其余 `No`
   - 容器：Tensor 与变长批 `ImageBatchVarShape` 都支持
4. 把以上内容整理成三段，对应将来头文件里的 `@brief` / `Reference:` / Limitations 表。

**需要观察的现象**：对照 4.1.3 的解析器规则自查——你的矩阵里声明了 1 个 dtype、1 个通道数、2 种容器，将来 done 门禁就会要求恰好这些覆盖，一个不多、一个不少。

**预期结果**：得到一张「契约 → 检查项」映射表（dtype `u8` → COV-1 要求测试里出现 `FMT_RGB8` 类记号、COV-2 要求基准配置含 `uchar3`；通道 3 → COV-CHAN；双布局 → COV-PARITY/COV-5）。

**待本地验证**：规格本身无需运行；映射关系可在 4.4 的实践中用真实门禁输出核对。

顺带一个真实工程提醒：仓库里**已经存在** `Invert` 算子（模板注释与 `TestOpInvert.cpp` 都提到它，见 [PythonWrap.cpp:18-23](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/PythonWrap.cpp#L18-L23)）。动手前先检索现有算子避免重复造轮子；`invert3` 在本讲只作练习假想名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SPEC-CORRECT 被设计成永远 MANUAL，而 SPEC-MATRIX 可以机器判定？

**答案**：SPEC-MATRIX 检查的是「表里有没有 TODO、格式能否解析」这类结构性事实，正则即可判定；SPEC-CORRECT 检查的是「声明的语义是否真的等价于引用的 oracle、金标是否独立实现」，这是语义判断，机器无法可靠完成，所以留给人工评审（[make_op.py:259-269](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L259-L269)）。

**练习 2**：一个算子头文件里写了 `Reference: custom — oracle waived (novel HDR tone mapping)`，SPEC-ORACLE 会通过吗？

**答案**：会。SPEC-ORACLE 的正则显式接受 `oracle waived` 记号（[make_op.py:222-233](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L222-L233)），这对应 Part 0 允许的「显式豁免 + 记录理由」路径；豁免不是逃避检查，而是把「无外部参照」这一事实本身写成可审计的记录。

---

### 4.2 mkop.sh：从 11 个模板生成全套骨架并接线

#### 4.2.1 概念说明

`tools/mkop/mkop.sh` 是一个约 350 行的 bash 脚本，做两类事：

1. **生成新文件**：把 11 个模板按算子名替换占位符后写到四层链路、测试、基准的正确位置——u5-l1 讲过的四层（绑定 → C API → priv → kernel 位置）一次配齐。
2. **修改现有文件**：向 6 个 CMakeLists、Python 模块注册处、基准清单、分类清单和 3 个文档文件里插入新算子的条目——u1-l4 讲过的「定义、声明、注册三站」全部自动完成。

配套的叙事文档 [`make_operator.rst`](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/make_operator.rst#L50-L115) 把它总结为「Step 1: Generate the Scaffold」。调用方式（[mkop.sh:294-310](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L294-L310)）：

```bash
tools/mkop/mkop.sh <OperatorName> [CVCUDA根目录]   # 不给根目录则默认 ../..
```

首字母自动大写：`clahe` 与 `Clahe` 都规范化为 `Clahe`。**注意**：`mkop.sh` 只接受 1 或 2 个位置参数，不接受 `--bare`——`--bare` 是 `/make-op-scaffold` 技能与 `make_op.py` 的旗标（表示「规格也一并委托」，见 [make-op-scaffold SKILL.md:11-17](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/skills/make-op-scaffold/SKILL.md#L11-L17)），两者不要混淆。

#### 4.2.2 核心流程

```text
mkop.sh <Name>
   ├─ 名字规范化：Name（PascalCase）/ namelower（小写）
   ├─ 11 次 modify_and_update_template：模板 ──sed 替换──► 目标文件
   │      占位符：__OPNAME__ / __OPNAMELOW__ / __OPNAMECAP__ /
   │              __OPNAMESPACE__ / __OPNAMEUPPER__
   ├─ 3 次 add_after_set_anchor：三个 CMake 的 set(...) 源列表
   ├─ 3 次 Python 注册：Main.cpp / Operators.hpp / python CMakeLists
   ├─ 2 次 bench 接线 + bench_params.json + operator_categories.json
   └─ 3 次文档接线：operator_list.rst / operators.rst / 最新 relnote
```

五个占位符的派生规则在 [mkop.sh:30-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L30-L55)（以 `Invert3` 为例）：

| 占位符 | 派生规则 | Invert3 的结果 | 典型用途 |
|---|---|---|---|
| `__OPNAME__` | 原名 | `Invert3` | 类名、文件名 |
| `__OPNAMELOW__` | 全小写 | `invert3` | Python 函数名、文件名 |
| `__OPNAMECAP__` | 每个大写字母前加 `_` 再全大写 | `_INVERT3` | 头文件保护宏、Doxygen 组名 |
| `__OPNAMESPACE__` | 小写→大写边界插空格 | `Invert3` | 文档中的可读名称 |
| `__OPNAMEUPPER__` | 全大写无分隔 | `INVERT3` | 基准代码生成宏 `BENCH_INVERT3_*` |

（`upper_name` 的注释明确说明它要匹配基准代码生成的宏命名，见 [mkop.sh:44-46](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L44-L46)。）

生成文件清单（[mkop.sh:312-347](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L312-L347)）：

| 模板 | 落盘位置 | 层（呼应 u5-l1） |
|---|---|---|
| `Public.h` | `src/cvcuda/include/cvcuda/Op<Name>.h` | C API 头（契约所在） |
| `Public.hpp` | `src/cvcuda/include/cvcuda/Op<Name>.hpp` | C++ RAII 类头 |
| `CImpl.cpp` | `src/cvcuda/Op<Name>.cpp` | C API 实现 |
| `PrivateImpl.cpp` | `src/cvcuda/priv/Op<Name>.cpp` | priv 实现（写 kernel 时可改名 `.cu`） |
| `PrivateImpl.hpp` | `src/cvcuda/priv/Op<Name>.hpp` | priv 类声明 |
| `CppTest.cpp` | `tests/cvcuda/system/TestOp<Name>.cpp` | C++ 系统测试 |
| `PythonWrap.cpp` | `python/mod_cvcuda/operators/Op<Name>.cpp` | pybind11 绑定 |
| `PythonTest.py` | `tests/cvcuda/python/test_op<name>.py` | Python 测试 |
| `Bench.cpp` | `bench/cpp/ops/Bench<Name>.cpp` | nvbench C++ 基准 |
| `BenchPy.py` | `bench/python/ops/bench_<name>.py` | Python 基准 |
| `BenchConfig.json` | `bench/config/operators/<name>.json` | 共享基准配置 |

被修改的现有文件（[mkop.sh:325-352](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L325-L352)）：`src/cvcuda{,/priv}/CMakeLists.txt`、`tests/cvcuda/system/CMakeLists.txt`、`python/mod_cvcuda/{Main.cpp, operators/Operators.hpp, CMakeLists.txt}`、`bench/{cpp,python}/CMakeLists.txt`、`bench/config/{bench_params.json, operator_categories.json}`、`docs/sphinx/operator_list.rst`、`docs/sphinx/modules/python/operators.rst`、最新 relnote。

#### 4.2.3 源码精读

**(a) 模板替换与年份刷新**（[mkop.sh:30-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L30-L55)）：`modify_and_update_template` 先 `sed "s/__OPNAME__/$name/g"` 整体替换，再依次替换其余占位符；L49 那行 sed 把 SPDX 头的年份改写为当前年——这对应仓库不变量「新文件用创建年份」（u1-l4 讲过）。

**(b) 锚点插入的防御性**：脚本头部专门定义了 `die`（[mkop.sh:22-28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L22-L28)），注释解释了为什么必须用 `exit` 而不是 `return`：这些辅助函数是裸调用的，没有 `set -e`，`return 1` 会被吞掉——**宁可失败退出，也不带着不完整的接线「绿色」返回**。这是脚手架脚本的可靠性设计。

**(c) CMake 接线**（[mkop.sh:129-145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L129-L145)）：`add_after_set_anchor` 在第一个匹配锚点（如 `set(CV_CUDA_OP_FILES`）的下一行插入缩进条目，插入前 `grep -qF` 保证幂等。三个锚点分别是 `set(CV_CUDA_PRIV_OP_FILES`、`set(CV_CUDA_OP_FILES`、`set(CVCUDA_TEST_SOURCES`（[mkop.sh:325-327](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L325-L327)）。

**(d) Python 三站注册**（[mkop.sh:96-127](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L96-L127) 与 [mkop.sh:329-335](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L329-L335)）：`add_to_python_main` 在 `Main.cpp` 调 `ExportOp<Name>(m);`，`add_to_python_operators` 在 `Operators.hpp` 加声明，`add_to_cmake_python` 把源文件挂进 `python/mod_cvcuda/CMakeLists.txt`——正是 u1-l4 总结的「定义、声明、注册」三站。L101-L107 的注释还专门解释了锚点为什么要用**带缩进的** `// CV-CUDA Operators` 标记行：因为该短语在文件里出现两次，锚错就会把调用插进 include 注释区。

**(e) JSON 清单的内嵌 Python 修改**（[mkop.sh:147-201](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L147-L201)）：`bench_params.json`（基准清单）与 `operator_categories.json`（RGB 基准指南分类，默认给 `A` 类）都用 `python3 - <<'PYEOF'` heredoc 读改写 JSON 并按字母序重排键——结构化文件用结构化解析器改，不用 sed 硬啃。

**(f) 文档三处接线**：`add_to_oplist`（[mkop.sh:203-233](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L203-L233)）用 awk 按显示名排序插入 operator_list 表格行；`add_to_autofunction`（[mkop.sh:235-261](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L235-L261)）插入 `cvcuda-autofunction` 两条指令（`fn` 与 `fn_into`，呼应 u3-l3 的两种变体）；`add_to_relnote`（[mkop.sh:263-292](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L263-L292)）先从根 `CMakeLists.txt` 的缩进 `VERSION X.Y.Z` 行解析当前版本（注释解释了为什么锚定缩进：避免匹配到 `cmake_minimum_required(VERSION ...)`），再找对应 relnote 插入 `Added the \`<Name>\` operator` 条目。

**(g) 模板里「故意留红」的坑**——这是本模块最重要的精读点。脚手架的哲学是：**生成一个能编译但绝不会误通过的骨架**，每个待实现位置都留了门禁能抓到的显式标记：

- C++ 测试：[CppTest.cpp:82-91](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/CppTest.cpp#L82-L91) 的金标还是 `std::generate(goldVec...)` 随机占位（`// TODO populate gold vector with expected results`），[CppTest.cpp:104-105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/CppTest.cpp#L104-L105) 的比对是 `ASSERT_EQ(goldVec, testVec)`——五段式金标范式的中间段留空。
- Python 测试：[PythonTest.py:61-62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/PythonTest.py#L61-L62) 末尾 `t.fail("Test failed intentionally")`，前面 L28-L59 已是完整的元数据契约断言（`shape`/`layout`/`dtype`/`_into` 原样返回——正是 u7-l2 讲过的「Python 只断言元数据契约」）。
- 头文件契约：[Public.h:52-96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/Public.h#L52-L96) 的 Limitations 表全是 `[TODO]` 与 `TODO`。
- 基准驱动：[Bench.cpp:39-47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/Bench.cpp#L39-L47) 对非 Tensor 输入与非 NHWC 布局直接 `state.skip("TODO(make-op): ...")`；[BenchConfig.json:24](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/BenchConfig.json#L24) 的描述里也带 `TODO(make-op)` 校准提示（1–2 ms 原则，呼应 u7-l3）。
- 绑定 docstring：[PythonWrap.cpp:73-92](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/PythonWrap.cpp#L73-L92) 的 `Args: TBD args`；而 L39-L65 的 `Into`/allocating 包装、ResourceGuard 三段锁（READ 输入 / WRITE 输出 / NONE 算子，呼应 u4-l1/u4-l3）已经是可用的成品代码。

模板还内置了「少走弯路」提示：[CppTest.cpp:18-23](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/CppTest.cpp#L18-L23) 与 [PythonWrap.cpp:18-27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/PythonWrap.cpp#L18-L27) 都提示：一元逐像素算子优先用共享的 `ElementwiseOpHarness.hpp` / `UnaryElementwiseOp.hpp`，避免每算子复制粘贴触发 SonarQube 重复度门禁。

另外两点：

- priv 实现生成的是 `.cpp`；只有直接在文件里写 CUDA 设备代码时才改名 `.cu` 并同步 `priv/CMakeLists.txt`（[make_operator.rst:108-128](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/make_operator.rst#L108-L128)）。
- C API 模板已是「Sonar-clean」成品：[CImpl.cpp:27-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/CImpl.cpp#L27-L55) 里 `Create` 的 `new` 带 `// NOSONAR`（所有权转移给 C 句柄），`Submit` 体首行是 `CVCUDA_NVTX_RANGE("cvcuda__OPNAME__Submit")`（NVTX 埋点纪律，呼应 u7-l4）——这两处都是为后面的 IMP 门禁与 SonarQube 零容忍预铺的。

#### 4.2.4 代码实践

**实践目标**：亲手生成一次骨架并核对全部产物。

**操作步骤**：

1. 确认工作树干净后建专用分支：`git switch -c tutorial/invert3-scaffold`（脚手架会改仓库文件，务必在分支上做）。
2. 在仓库根运行：`tools/mkop/mkop.sh Invert3`。
3. `git status --short` 查看变化，与 4.2.2 的两张清单逐条对照：应有 11 个未跟踪新文件与约 11 个已修改文件。
4. 打开 `src/cvcuda/include/cvcuda/OpInvert3.h`，确认占位符已被替换（`@brief` 仍是桩短语、Limitations 全是 TODO）；打开 `python/mod_cvcuda/Main.cpp` 与 `operators/Operators.hpp`，找到新插入的 `ExportOpInvert3` 注册与声明。
5. `git diff bench/config/bench_params.json bench/config/operator_categories.json` 观察 JSON 条目的字母序插入。
6. 浏览结束后清理：`git restore . && git clean -fd src/cvcuda tests python bench docs`（或直接删除分支）。**不要把练习骨架留在工作树里。**

**需要观察的现象**：11 个新文件恰好覆盖 u5-l1 的四层 + 测试 + 基准；所有插入条目都出现在既有条目的字母序位置；重复运行 `mkop.sh Invert3` 不会产生重复插入（幂等）。

**预期结果**：得到一张「模板 → 落盘路径 → 所属层」的实物对照表。

**待本地验证**：本环境未实际运行 `mkop.sh`（它会修改仓库文件），以上步骤与文件清单依据 [mkop.sh:312-352](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L312-L352) 与 [make_operator.rst:63-107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/make_operator.rst#L63-L107) 推得，请在本机验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mkop.sh` 修改 JSON 清单时用内嵌 `python3` 而不像改 CMake 那样用 sed/awk？

**答案**：CMakeLists 的 `set(...)` 列表是行式文本，锚点下一行插入即可；JSON 是嵌套结构化数据，还要维持键的字母序与缩进——用 `json.load`/`json.dump`（[mkop.sh:153-171](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/mkop.sh#L153-L171)）才能保证语法正确且幂等，这也呼应仓库规则「有结构化解析器时不要用文本硬替换」。

**练习 2**：`Bench.cpp` 模板对 `VarShape` 输入调用 `state.skip(...)` 而不是删掉那条路径，为什么？

**答案**：`state.skip` 让该配置在基准报告中显式记录为「跳过」而非静默消失；同时 `TODO(make-op)` 字符串是 BEN-DRV 门禁的抓捕目标（[make_op.py:800-828](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L800-L828)）——结构检查会因「文件存在」而通过，这个标记保证「驱动其实没实现」仍然被发现。

**练习 3**：如果把 priv 实现改名 `OpInvert3.cu`，还需要做什么？

**答案**：同步更新 `src/cvcuda/priv/CMakeLists.txt` 里的文件名（[make_operator.rst:120-128](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/make_operator.rst#L120-L128)）。SCF-4 门禁对 `.cpp` 与 `.cu` 二选一都放行（作者的选择），但 CMake 登记必须与实际文件名一致。

---

### 4.3 make_op.py 的 scaffold 门禁：结构完备性检查

#### 4.3.1 概念说明

`tools/make_op.py` 是实现指南清单的确定性检查器。它的模块 docstring 开宗明义（[make_op.py:16-35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L16-L35)）：**它不写算子（那是人 + mkop.sh 的事），它验收算子**——对照算子声明的契约（C 头文件）检查完备性、接线与回归严格度。用法：

```bash
python3 tools/make_op.py <Operator> --phase scaffold|done \
    [--bare] [--format md|json] [--out report.json] [--run]
```

每个检查项输出四元组：**状态 + 字面证据 + 检查项 id + 修复建议**。状态词汇表（[MAKE_OP_GUIDELINES.md:71-79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L71-L79)）：

| 状态 | 含义 | 影响退出码 |
|---|---|---|
| `PASS` | 检查通过 | 否 |
| `GAP` | 检查失败，可行动的缺失 | **是（非零退出）** |
| `N-A` | 对该算子不适用 | 否 |
| `MANUAL` | 需要人读代码；检查器指出位置 | 否（但会被呈现） |
| `RECOMMENDATION` | 建议性跟进 | 否 |

退出码语义在 [make_op.py:1421](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1421)：`return 1 if any(f.status == GAP for f in findings) else 0`——**任何一个 GAP 都让命令失败**，这就是它能当 CI 门禁的原因。

`scaffold` 阶段只跑两个域：`spec`（契约是否已写）与 `scaffold`（骨架是否完整接线），外加说明「实现类检查留给 done 阶段」——所以刚生成、尚未实现的骨架**应该**在 scaffold 阶段是绿的，这是设计意图（[make_op.py:1283-1287](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1283-L1287) 的注释）。

#### 4.3.2 核心流程

SCF 域 14 项检查分四组：

```text
文件存在性（SCF-1..9）
  C 头 / C++ 头 / C API 实现 / priv 实现(.cpp|.cu 二选一) / priv 头 /
  C++ 系统测试 / Python 绑定(必须在 operators/ 下) / Python 测试 /
  基准三件套(C++ + Python + config)
接线（SCF-10..12）
  三个 CMake 源列表 / Python 三站注册 / 基准两处 CMake + bench_params 清单
文档与许可（SCF-13..14）
  operator_list 行 + autofunction 两条 + relnote 条目 / 全部新文件 SPDX 头
```

注意 SCF-7 有个历史细节：绑定文件**必须**在 `python/mod_cvcuda/operators/` 子目录而非模块根（[make_op.py:311-319](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L311-L319)），对应 u1-l4 讲过的绑定层两线组织。

#### 4.3.3 源码精读

**(a) 路径推导**：`extra_paths`（[make_op.py:141-159](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L141-L159)）由算子名机械推导出全部应存在的路径——`Op<Name>.cpp`、`priv/Op<Name>.{cpp,cu}`、各 CMakeLists 等；算子名本身的解析（`Op`/`op`/`pyname` 及头文件定位）复用 `review_op.resolve_op`（[review_op.py:209-227](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L209-L227)，对头文件名大小写不敏感）。「组合而非重复」的导入关系见 [make_op.py:47-63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L47-L63)。

**(b) 存在性与接线的两个小助手**：`present()`（[make_op.py:278-288](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L278-L288)）把「路径存在 → PASS / 缺失 → GAP」模板化；`wired()`（[make_op.py:339-350](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L339-L350)）对指定文件跑正则。SCF-4 是特例：`.cpp` 与 `.cu` 是作者的选择，任一存在即过（[make_op.py:295-309](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L295-L309)）。

**(c) 组合型接线检查**：SCF-10 要三处 CMake 同时登记（[make_op.py:352-369](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L352-L369)）；SCF-11 要 `Main.cpp` 的 `ExportOp<Name>` 调用、`Operators.hpp` 的声明、python CMake 的源文件三站齐全（[make_op.py:370-389](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L370-L389)）；SCF-12 要基准两个 CMake 加 `bench_params.json` 清单条目（[make_op.py:390-408](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L390-L408)）。证据字段把三个布尔值原样打印（如 `Main=True Operators.hpp=True CMake(operators/)=False`），失败时一眼看出断在哪一站。

**(d) 文档四件套**：SCF-13（[make_op.py:409-437](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L409-L437)）查 operator_list 的 `:py:func:` 行、autofunction 的 `fn` 与 `fn_into` 两条、以及最新 relnote 里出现算子名；「最新 relnote」的版本号同样从根 `CMakeLists.txt` 的 `VERSION` 解析（`latest_relnote`，[make_op.py:162-172](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L162-L172)）。

**(e) SPDX 检查的宽严**：SCF-14（[make_op.py:438-470](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L438-L470)）只看每个新文件**前 600 字节**内有没有 `SPDX-License-Identifier`，`.json` 豁免——与仓库「新文件必须带 SPDX 头」的不变量对应。

**(f) 报告渲染与判定**：`render_md`（[make_op.py:1311-1361](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1311-L1361)）按域分组输出，每域给出 `verdict: PASS / GAPS / NEEDS-REVIEW`（L1333-L1338：有 GAP 为 GAPS，只有 MANUAL 为 NEEDS-REVIEW），总体判定同理；scaffold 报告末尾会注明「implementation outstanding (IMP/COV/EXEC)」（L1351-L1355）。

#### 4.3.4 代码实践

**实践目标**：在未实现的骨架上跑 scaffold 门禁，验证「结构绿 + 契约红/委托」的语义。

**操作步骤**（紧接 4.2.4 的实践，骨架仍在工作树时进行）：

1. 运行 `python3 tools/make_op.py Invert3 --phase scaffold --bare`：预期 14 项 SCF 全部 `PASS`，而 SPEC-BRIEF / SPEC-ORACLE / SPEC-MATRIX 为 `MANUAL`（`--bare` 下 `spec_finding` 直接返回 MANUAL「spec delegated」，见 [make_op.py:188-197](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L188-L197)）。
2. 再运行不带 `--bare` 的 `python3 tools/make_op.py Invert3 --phase scaffold`：预期同样 14 项 SCF 绿，但 SPEC 三项变 `GAP`（头文件还是桩短语、TODO 矩阵）。
3. 回声检查：`echo $?` 应为 1（存在 GAP）。用 `--format json --out /tmp/invert3.json` 再跑一遍，检查 JSON 里的 `counts` 与 `exit_gap` 字段。
4. 把 4.1.4 写好的 invert3 契约填进 `OpInvert3.h`（`@brief` 一句话、`Reference: torchvision.transforms.v2.functional.invert`、Limitations 表按声明矩阵填 `Yes/No`），第三次运行 scaffold 门禁，观察 SPEC 三项转绿。

**需要观察的现象**：`--bare` 与非 `--bare` 的唯一差异在 SPEC 域；SCF 域的证据字段直接给出文件路径与匹配的正则。

**预期结果**：SCF-1..14 全绿证明「骨架完整且接线」；SPEC 状态随契约填写过程从 MANUAL/GAP 变为 PASS，直观展示「契约是独立于骨架的一等检查对象」。

**待本地验证**：本环境未运行该命令，输出形态依据 `render_md` 源码推得，请在本机核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 scaffold 阶段故意不跑 IMP/COV 检查？

**答案**：scaffold 阶段的定义是「骨架完整且接线」（刚跑完 `mkop.sh` 的时刻），此时模板必然带着 `TODO`/`t.fail` 标记，跑实现类检查必然全红、毫无信息量。把它们留给 done 阶段，才能让「scaffold 绿」本身成为一个有意义的里程碑——契约已批准、骨架已就位（[make_op.py:1283-1287](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1283-L1287) 注释明说这一点）。

**练习 2**：SCF-11 报告 `Main=True Operators.hpp=True CMake(operators/)=False`，最可能的成因是什么？

**答案**：`python/mod_cvcuda/CMakeLists.txt` 的 `SOURCES` 列表里没有 `operators/Op<Name>.cpp`。三个布尔分别对应三站（[make_op.py:370-389](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L370-L389)）；若用的是 `mkop.sh`，这通常意味着手工挪动过文件或 CMake 被回退。

---

### 4.4 done 门禁：从「文件都在」到「正确、完备、可运行」

#### 4.4.1 概念说明

`--phase done` 是最终回归清单，指南的定义（[MAKE_OP_GUIDELINES.md:134-137](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L134-L137)）：绿色 ⇒ **正确、完备、有文档、进了 relnotes、可移交优化**。完成（completion）= 复跑显示**零 GAP 且零未解决 MANUAL**；最终绿还需要 `--run`（测试编译、执行、通过）与 CI 播种的基线。

它的组织原则是**组合而非重复**（[MAKE_OP_GUIDELINES.md:21-25](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L21-L25) 的层级图）：

```text
review_op.py        覆盖原语（support / test / bench / docs 四域）
   └─ optimize_op.py --phase preflight   = review_op(test+bench) + 基线 + 剖析就绪
        └─ make_op.py                     = review_op(全部四域) + preflight + SPEC + SCF
                                            + IMP + COV + DOC-REL + EXEC
```

`run_done` 的调用序列印证了这一点（[make_op.py:1290-1308](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1290-L1308)）：spec → scaffold → impl → coverage → bench-guidelines → review_op 的 support/test/bench/docs → execution → readiness，共十个域。

三条不可违反规则（[MAKE_OP_GUIDELINES.md:84-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L84-L90)）贯穿所有域：**绝不伪造基线**（缺基线就是 `GAP "requires CI"`，走 CI regen 流程）；**位精确是默认**（不允许悄悄引入或放宽容差，无解释的 `EXPECT_NEAR` 在这里是 GAP——比 `review_op` 更严）；**布局支持缺口是作者的工作**（机器只报告缺失，不替你实现；平面布局默认必需，只有算子局部的 C 头声明加理由才能豁免）。

#### 4.4.2 核心流程

done 阶段新增的域及其核心问题：

| 域 | 检查项 | 核心问题 |
|---|---|---|
| implementation | IMP-1/2/3、NVTX-1 | 桩标记清零了吗？多 GPU 安全吗？NVTX 埋点还在吗？ |
| coverage | COV-GOLD/BITEXACT/1/CHAN/2/3/PARITY/5/NEG/MATRIX、BEN-DRV/BEN-GUIDE | 头文件声明的每个变体都被位精确地证明了吗？ |
| docs | DOC-REL（+review_op 的 DOC-*） | 进最新 relnote 了吗？ |
| execution | EXEC-1/2（需 `--run`） | 测试真的编译、运行、通过了吗？ |
| readiness | RDY-1 | `optimize_op.py --phase preflight` 绿了吗？ |

其中 coverage 域的闭环最值得记住：**COV 只遍历「声明的」dtype/通道**（[MAKE_OP_GUIDELINES.md:151-153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L151-L153)）——纯整型算子永远不会被强制加浮点。头文件声明即测试义务：

\[ \text{测试义务} = \text{声明的 dtype 集合} \times \text{声明的通道集合} \times \text{声明的容器集合} \]

#### 4.4.3 源码精读

**(a) IMP-1 桩标记正则**（[make_op.py:475-477](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L475-L477)）：

```python
STUB_MARKERS = (r"\bTODO\b|t\.fail\(|\bno-?op\b|std::generate\(goldVec|Test failed intentionally")
```

把它与 4.2.3 (g) 的模板对照：`std::generate\(goldVec` 抓 C++ 测试的占位金标，`Test failed intentionally` 抓 Python 测试的 `t.fail`——**每个标记都精确对应脚手架故意留下的一个坑**。检查范围覆盖 priv、C API、两个测试、绑定五个文件（[make_op.py:483-511](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L483-L511)）。

**(b) IMP-3 多 GPU 安全**（[make_op.py:534-562](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L534-L562)）：priv 里若出现 `cudaMalloc`，就必须出现 `PerDeviceResource`。背景在叙事教程的 Multi-GPU Safety 一节（[make_operator.rst:186-257](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/make_operator.rst#L186-L257)）：构造函数里裸 `cudaMalloc` 会把内存分配在「构造时刻恰好当前」的 GPU 上，之后换卡调用就是隐性损坏——`PerDeviceResource<T>` 按设备惰性创建、析构时切回正确设备。无设备分配则报 `N-A`。

**(c) NVTX-1**（[make_op.py:564-632](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L564-L632)）：从头文件正则抓出全部 `cvcuda<Name>...Submit` 声明，再要求 `Op<Name>.cpp` 中**每个** `CVCUDA_DEFINE_API` 定义的函数体**第一条语句**就是同名的 `CVCUDA_NVTX_RANGE("...")`（L576-L595 的 `_submit_definition_marked`）；标记写在别处或定义缺失都不算。这是 u7-l4 所讲「每个 Submit 首行埋点」纪律的机器守护。检查器注释还指出：Python 侧的等价测试 `tests/cvcuda/python/test_nvtx_markers.py` 用源码扫描（声明/定义正则见 [test_nvtx_markers.py:33-39](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L33-L39)）做同样的事；指南 L146 描述的 `OPERATORS` 注册表是 always-on NVTX 合入后的目标形态，当前实现以源扫描为准。

**(d) COV-GOLD 与 COV-BITEXACT**（[make_op.py:649-695](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L649-L695)）：前者要求测试文件里存在 `Gold`/`Reference`/`naive`/`CPU` 类符号（独立 CPU 金标——u7-l1 五段式的第二段）；后者先扫 `EXPECT_NEAR|ASSERT_NEAR`——**只要出现一处未解释的 NEAR 就是 GAP**，否则要求存在 `EXPECT_EQ/ASSERT_EQ`。位精确默认的直接机器化。

**(e) COV-1 / COV-CHAN：声明 dtype 与通道必须被测试**（[make_op.py:697-761](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L697-L761)）：对头文件解析出的每个声明 dtype，用 `DTYPE_TEST_RE`（[make_op.py:82-94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L82-L94)）检查测试文本里是否出现对应的 `FMT_*` / `TYPE_*` 记号。这个正则表写得很讲究：无符号行用负向后顾（`(?<![Ss])8`）避免把 `FMT_S8` 误读成 `u8` 覆盖——注释明说是为了防止无关记号（如 flip 码的 `TYPE_S32`）误导。通道推断 `fmt_channels` 从 `FMT_RGBA/RGB/其他` 归出 4/3/1（[make_op.py:126-138](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L126-L138)）。

**(f) COV-2：声明 dtype 必须被基准**（[make_op.py:763-798](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L763-L798)）：读基准配置 JSON 各 config 的 `dtypes` 数组，经 `bench_dtype_canon`（[make_op.py:97-123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L97-L123)，`uchar3`→`u8` 等最长词干优先映射）归一后，必须覆盖全部声明 dtype。呼应 u7-l3 的共享配置体系。

**(g) BEN-DRV 与 BEN-GUIDE**：前者（[make_op.py:800-828](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L800-L828)）抓基准驱动里的 `TODO(make-op)`——结构性 BEN-* 检查会被「全部 skip 的空壳」骗过，这项保证驱动真的跑声明的布局与容器；后者（[make_op.py:1212-1280](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1212-L1280)）实际运行 `bench/tests/` 下两个静态测试（RGB 基准指南 + 配置键行程线），把「未分类 / 分错 tier / 行数漂移」在本地约一小时的 CI 烧机之前就拦下。

**(h) COV-PARITY / COV-5 / COV-NEG**：等价布局奇偶（原生 planar 与「reformat→交错算子→reformat」位精确相等，[make_op.py:859-934](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L859-L934)，正是 u7-l1 讲过的 planar 奇偶校验，头文件声明不适用时双双 `N-A`）；补集负例（`_Negative` 套件断言 `NVCV_ERROR_INVALID_ARGUMENT`，[make_op.py:936-955](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L936-L955)）——「支持矩阵说不的每一格，都要有一条测试证明它确实说不」。

**(i) EXEC 与 RDY**：EXEC（[make_op.py:1011-1137](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1011-L1137)）不加 `--run` 时报 MANUAL；加了则找 `build-rel`/`build` 构建目录，构建 `cvcuda_test_system` 并以 `--gtest_filter=Op<Name>*:*<Name>*` 跑该算子用例，再跑对应 pytest。无 GPU 环境报 GAP/MANUAL 并明确「defer to CI」——检查器不假装验证过。RDY-1（[make_op.py:1140-1208](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1140-L1208)）以子进程运行 `optimize_op.py <Op> --phase preflight --format json` 并把其退出码与 GAP 数折叠成一项——完成门禁的最后一关直接衔接 u8-l4 的性能工具链。

最后是两个工程现实：**基线只能来自 CI**——本地绝对耗时因 SKU 而异，本地校准好配置并跑绿后，触发 CI 的 `baseline-regen` 工作流在参考 SKU 上播种基线，再用 `bench/_internal/update_baseline.py` 导入，`BEN-7`/`RDY-1` 才会绿（[MAKE_OP_GUIDELINES.md:231-239](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L231-L239)）；**SonarQube 零容忍**——MR 上任何新告警即失败，老算子有豁免而新算子没有，常见规则（禁 `[&]` 默认捕获、`Create` 里的 `new` 保留 `// NOSONAR`、一语句一声明等）见 [MAKE_OP_GUIDELINES.md:240-259](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L240-L259)。

#### 4.4.4 代码实践

**实践目标**：在**未实现**的骨架上跑 done 门禁，学会读 GAP 清单并把它逐条映射回模板中的具体 TODO 行——理解「红是设计的一部分」。

**操作步骤**（紧接 4.3.4，契约已填好、实现未做）：

1. 运行 `python3 tools/make_op.py Invert3 --phase done > /tmp/invert3-done.md; echo exit=$?`（预期非零退出）。
2. 统计各域的 GAP：`grep -c '❌' /tmp/invert3-done.md`（Markdown 输出中 GAP 的图标，见 [make_op.py:1312](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L1312)）。
3. 建立映射表，预期至少包括：

| GAP | 对应模板中的桩 | 应做的实现动作 |
|---|---|---|
| IMP-1 | [CppTest.cpp:86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/CppTest.cpp#L86) `std::generate(goldVec...`、[PythonTest.py:62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/PythonTest.py#L62) `t.fail` | 实现算子与真实金标，删掉占位 |
| COV-GOLD | 同上（无 `Gold` 符号） | 写独立 CPU 金标（u7-l1 范式） |
| COV-NEG | 模板没有 `_Negative` 套件 | 按声明矩阵的补集加负例 |
| COV-PARITY / COV-5 | 模板值表只有交错 U8 一行 | 加 planar 奇偶用例 + NCHW/NCHW_FAKE 基准 |
| COV-2 | [BenchConfig.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/BenchConfig.json#L2-L27) 的 dtypes 需与声明矩阵一致 | 核对/裁剪配置 |
| BEN-DRV | [Bench.cpp:41-46](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/mkop/Bench.cpp#L41-L46) `state.skip("TODO(make-op)...")` | 实现真实基准驱动 |
| EXEC-1/2 | —（未加 `--run`） | 实现后加 `--run` 验证 |
| RDY-1 | 基线缺失 `GAP [requires CI]` | CI regen + 导入 |

4. 观察 verdict 行：各域应为 `GAPS` 或 `NEEDS-REVIEW`（有 MANUAL 时），总体非 PASS。
5. 实践结束后按 4.2.4 步骤 6 清理工作树。

**需要观察的现象**：done 报告的每一条 GAP 都能在某个模板文件里找到对应的 `TODO`/占位根源——没有一条是「凭空的」；`RECOMMENDATION` 与 `N-A` 不影响退出码。

**预期结果**：得到一份「GAP → 桩位置 → 待办动作」的完整实现工作清单，这就是 done 门禁作为「可执行 backlog」的价值。

**待本地验证**：本环境未运行 done 阶段（无构建目录、无 GPU），上表依据检查器源码与模板内容推得；真实输出请在本机核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 COV-BITEXACT 把任何 `EXPECT_NEAR` 都判为 GAP，连「合理的浮点容差」也不放过？

**答案**：这是有意为之的强规则（[MAKE_OP_GUIDELINES.md:86-88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/MAKE_OP_GUIDELINES.md#L86-L88)）：默认要求 GPU 结果与独立 CPU 金标**位精确**相等。若某算子确实无法位精确（如迭代求解），正确做法是在评审中显式论证并记录，而不是悄悄写个容差——无解释的 NEAR 会掩盖回归。检查器的职责是逼出每一次容差放宽的理由。

**练习 2**：你的算子声明了 `f16` 但基准配置只写了 `float3`，哪个检查项红？为什么映射表要写那么长？

**答案**：COV-2 红。基准配置里的 dtype 名（`float3`/`uchar3`）是 nvbench 侧的写法，而头文件声明是 `f16` 这类规范记号，`bench_dtype_canon` 的最长词干优先表（`float16`→`f16` 必须先于 `float`→`f32` 匹配，[make_op.py:97-123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/make_op.py#L97-L123)）负责把两边归一到同一词表——映射表长正是为了不误判前缀。

**练习 3**：done 阶段全绿后，为什么还要「移交 `/optimize-op`」而不是直接合并？

**答案**：done 门禁证明的是**正确性与完备性**（含基准存在且跑得通、噪声达标），不证明**性能达标**；RDY-1 的 preflight 只是「优化就绪」（有基准覆盖、有基线、可剖析）。性能战役（先基准后改码、证据门禁）是 `/optimize-op` 的职责——u8-l4 展开。

---

## 5. 综合实践

把本讲全部内容串成一个「规格 → 骨架 → 门禁」的迷你旅程（不必实现算子本身）：

1. **写规格**（4.1.4 的产出）：`invert3` 语义一行话、`Reference: torchvision.transforms.v2.functional.invert`（`out = dtype_max − in`，对 uint8 等价于 OpenCV `cv::bitwise_not`）、支持矩阵 NHWC/HWC+NCHW/CHW、通道 [3]、dtype 仅 u8、容器 Tensor+变长批。
2. **生成骨架**：新分支上 `tools/mkop/mkop.sh Invert3`，用 `git status` 核对 4.2.2 两张清单。
3. **记录契约**：把规格填进 `OpInvert3.h` 的 `@brief` / `Reference:` / Limitations 表。
4. **结构门禁**：`python3 tools/make_op.py Invert3 --phase scaffold`，确认 SCF 全绿、SPEC 全绿。
5. **完成门禁读红**：`python3 tools/make_op.py Invert3 --phase done`，把 GAP 清单整理成实现 backlog（4.4.4 的映射表）。
6. **收尾**：清理分支；写下三行总结——契约驱动覆盖的闭环是什么、脚手架故意留红的桩有哪些、`--bare` 与非 `--bare` 的差别在哪。

**验收标准**：你能不看讲义说出「头文件 Limitations 表的每一行 Yes，最终会变成哪几条检查项」。

## 6. 本讲小结

- 新增算子的官方流程是**契约先行**：规格（语义 + 参考 oracle + 支持矩阵）经用户批准后记录在 `Op<Name>.h`，头文件是测试覆盖、金标与文档共同镜像的单一事实来源。
- `tools/mkop/mkop.sh` 用 5 种占位符把 11 个模板铺满四层链路 + 测试 + 基准，并幂等地接线约 11 个现有文件（CMake、Python 三站注册、基准清单、文档三处）；`--bare` 是 `make_op.py`/技能层的旗标，不是 `mkop.sh` 的参数。
- 脚手架的每个待实现位置都留了显式桩（`TODO`、`std::generate(goldVec`、`t.fail`、`state.skip("TODO(make-op)")`、`TBD args`），门禁的 IMP-1/BEN-DRV/SPEC 正则与之一一对应——**骨架能编译，但绝不会误通过**。
- `make_op.py --phase scaffold` 验收结构与契约（SPEC + SCF-1..14）；`--phase done` 组合 `review_op` 四域与 `optimize_op` preflight，加上 IMP/COV/DOC-REL/EXEC/RDY，构成零 GAP + 零未解决 MANUAL + `--run` + CI 基线的完成定义。
- COV 域只遍历**声明**的矩阵：dtype/通道/容器的每一格都要有位精确的正例、planar 奇偶用例与补集负例，且每个声明 dtype 都要进基准——契约不是越大越好。
- 三条铁律：不伪造基线（缺基线走 CI regen）、位精确是默认、布局缺口是作者的工作；另有 SonarQube 对新算子的零容忍。

## 7. 下一步学习建议

- **u8-l2（Python 绑定解剖）**：本讲的 `PythonWrap.cpp` 模板只展示了最小形态；下一讲以真实 `OpFlip.cpp` 为模板，讲 Main.cpp 导出顺序、`NvtxTrace` 包装与四连函数的完整写法——正好接住本讲留下的 `TBD args` docstring。
- **u8-l4（算子工程工具链）**：深挖 `review_op.py` / `optimize_op.py` / `refactor_op.py` 三个检查器本身，理解 preflight/evidence 阶段门禁如何防止性能回归——本讲 RDY-1 只是它们的入口。
- **自行阅读**：对照一个「刚毕业」的真实算子（如 `OpInvert` 相关文件与 `TestOpInvert.cpp`）看模板桩被填实后的样子；再读 [make_operator.rst:186-319](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/make_operator.rst#L186-L319) 的多 GPU 安全与测试章节，补齐本讲未展开的实现细节。
