# 测试体系：UT、ST 与编译性能

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ATVOSS 的 `tests/` 目录是如何分成 **UT（单元测试，Unit Test）** 与 **ST（系统测试，System Test）** 两大类的，以及它们各自验证什么。
- 用 `bash scripts/build.sh --host_ut` 与 `bash scripts/build.sh --st` 分别构建并运行这两类测试，并能解读它们的成功标志。
- 读懂 UT 中四种子类型（`host`、`builtin_kernel`、`builtin_tiling`、`compile_perf`）各自的写法，尤其是「在编译期用类型相等性断言」与「度量编译耗时」这两种对初学者很新鲜的测试手法。
- 读懂 ST 中按 `test_op_*` / `test_compute_*` / `test_tile_*` / `test_block_*` 命名划分的四大测试维度，并掌握 ST 用例「Config + `Run()` 十步样板」的固定写法模式。
- 理解为什么一个以「编译期表达式模板」为核心的库，需要专门用一个 `compile_perf` 测试来守护**编译期性能**，而不仅仅是运行期正确性。

本讲是专家篇的收尾，假设你已经读完了 u1-l2（构建运行）、u2-l3（运算符库）以及 u2/u3 中关于 `Compute()`、`Config`、`ArgumentsBuilder`、`CalculateTiling`、表达式线性化的内容。本讲不再解释这些机制本身，只解释**它们如何被测试**。

## 2. 前置知识

### 2.1 什么是 UT 与 ST

- **UT（Unit Test，单元测试）**：针对**单个模块或函数**的测试，粒度小、跑得快、不依赖完整硬件环境。ATVOSS 的 UT 几乎全部跑在 **Host CPU** 上，用 GoogleTest（gtest）框架组织，能在没有真实 NPU 的机器上验证编译期逻辑。
- **ST（System Test，系统测试）**：针对**端到端算子**的测试，要走完「ACL 初始化 → 分配 GM → 搬数据 → `deviceOp.Run` → 同步 → 校验精度」的全链路。ATVOSS 的 ST 编译成真实的 NPU 可执行文件，在真机或 `cannsim` 仿真器上运行，成功标志是日志里出现 `Accuracy verification passed`。

### 2.2 为什么 ATVOSS 的测试很特别

ATVOSS 的核心是一套**编译期表达式模板**（见 u2-l1）：用户在 `Compute()` 里写下的 `out = (in1 + in1) * in2`，在编译期就被展开成一棵嵌套的 C++ 类型树，再经线性化、DAG、Alloc/Free 等 Pass 翻译成 Ascend C 指令。这意味着：

1. **很多「行为」其实发生在编译期**——比如「这个表达式是否被正确线性化」是一个**类型**问题，可以用 `std::is_same_v` 在编译期断言，根本不需要运行。
2. **编译本身可能很慢**——120 个节点的级联表达式可能让编译器跑几十秒甚至上百秒。一旦某次改动让编译时间退化，开发体验就会崩溃，所以必须有专门的**编译耗时**守护测试。

这两点决定了 ATVOSS 的测试体系比普通项目多了两种独特手法：**编译期类型断言**与**编译性能度量**。

### 2.3 关键术语速查

| 术语 | 含义 |
|------|------|
| gtest / gtest_main | GoogleTest 框架，`TEST_F` 宏定义测试用例，链接 `gtest_main` 后无需手写 `main` |
| `EXPECT_EQ` / `EXPECT_TRUE` / `EXPECT_LE` / `ASSERT_TRUE` | gtest 断言宏；`EXPECT_*` 失败继续、`ASSERT_*` 失败终止当前用例 |
| bisheng | 昇腾专用 C++ 编译器，取代 gcc，理解 `--npu-arch`、`-xasc` 等 Ascend C 语法 |
| `--npu-arch=dav-2201` | Host 仿真用的小型架构，配合 `-xasc` 让 Ascend C 代码能在 CPU 上编译/模拟 |
| `--npu-arch=dav-3510` | Ascend 950 的真实架构，ST 用它编译出可在 NPU/cannsim 上运行的产物 |
| cannsim | 昇腾指令级仿真器，无真机时用它跑 ST 并产出日志 |
| ICPU_RUN_KF | `tikicpulib` 提供的宏，在 Host CPU 上**真正模拟执行**一次 AICore kernel |
| ASCEND_HOME_PATH | 指向 CANN 安装目录的环境变量，`build.sh` 启动的硬性前置（见 u1-l2） |

## 3. 本讲源码地图

本讲涉及的测试组织文件与典型用例文件如下：

| 文件 | 作用 |
|------|------|
| `tests/CMakeLists.txt` | 测试总入口，引入 gtest 并挂载 `ut`、`st` 两个子目录 |
| `tests/ut/CMakeLists.txt` | UT 总入口，挂载 `compile_perf`、`builtin_tiling`、`builtin_kernel`、`host` 四个子目录 |
| `tests/ut/host/CMakeLists.txt` | 用 `file(GLOB *.cpp)` 自动收集 host 用例，聚合成 `host_ut` 目标并 POST_BUILD 自动运行 |
| `tests/ut/host/test_arguments.cpp` | 验证 `ArgumentsBuilder` 把入参放进 tuple 的正确位置 |
| `tests/ut/host/test_expr_linearizer.cpp` | 验证手写扁平表达式与 `ToLinearizerExpr` 输出**类型完全相同** |
| `tests/ut/host/test_elewise_tiling.cpp` | 验证 `CalculateTiling` 成功且 tiling 结构体大小为 56 字节 |
| `tests/ut/host/test_utility.cpp` | 验证 `Unique_t` 对 `TypeList` 去重的正确性 |
| `tests/ut/builtin_kernel/test_builtin_kernel.cpp` | 用 `ICPU_RUN_KF` 在 CPU 上真正模拟执行 abs kernel 并校验数值 |
| `tests/ut/builtin_kernel/abs_config.h` | UT 共享的 `AbsConfig` 配置头文件 |
| `tests/ut/builtin_tiling/test_builtin_tiling.cpp` | 复用 `abs_config.h`，单独验证 tiling 计算与结构体大小 |
| `tests/ut/compile_perf/compile_perf_test.cpp` | 用 `system()` 调 bisheng 编译大表达式，用 chrono 度量**编译耗时** |
| `tests/ut/compile_perf/cases/120_expr_op.cpp` | 120 个节点的级联表达式，编译性能压测素材 |
| `tests/st/CMakeLists.txt` | 用宏 `atvoss_example_add_executable` 把每个 `test_*.cpp` 编成独立 NPU 可执行文件 |
| `tests/st/test_op_adds_lhs.cpp` | ST「单算子功能验证」代表：标量在左的加法 |
| `tests/st/test_compute_cascade.cpp` | ST「Compute 表达式结构验证」代表：级联表达式 `(in1+in1)*in2*(in1+in2)` |
| `tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp` | ST「策略对比」代表：含冗余表达式 + `MemMngPolicy::MANUAL` |
| `tests/st/test_cast_elimination.cpp` | ST 验证冗余 Cast 消除 Pass 的端到端正确性 |
| `tests/st/test_block_cast1.cpp` | ST「Block 层 cast」代表：带命令行 shape 解析的 cast |
| `scripts/build.sh` | 编排 `--host_ut` / `--example` / `--st` 三种互斥模式的统一入口 |

## 4. 核心概念与源码讲解

### 4.1 测试体系总览与 build.sh 三模式

#### 4.1.1 概念说明

ATVOSS 的测试目录是一棵两层树：

```
tests/
├── CMakeLists.txt          # 引入 gtest，挂载 ut 与 st
├── ut/                     # 单元测试（Host 侧）
│   ├── CMakeLists.txt      # 挂载四个子目录
│   ├── host/               # 编译期逻辑单测（4 个 .cpp）
│   ├── builtin_kernel/     # CPU 仿真执行 kernel（ICPU_RUN_KF）
│   ├── builtin_tiling/     # 单独验证 tiling 计算
│   └── compile_perf/       # 编译耗时度量
└── st/                     # 系统测试（端到端，跑在 NPU/cannsim）
    ├── CMakeLists.txt      # 每个 test_*.cpp → 一个可执行文件
    └── test_*.cpp          # 约 50 个用例，按 op/compute/tile/block 分类
```

UT 与 ST 的根本差异在于**运行环境**与**验证对象**：

| 维度 | UT（host/builtin/compile_perf） | ST（test_*） |
|------|----------------------------------|--------------|
| 运行环境 | Host CPU（含 `ICPU_RUN_KF` CPU 仿真） | NPU 真机或 `cannsim` 仿真器 |
| 编译架构 | `--npu-arch=dav-2201 -xasc` | `--npu-arch=dav-3510`（ascend950 真实架构） |
| 验证对象 | 编译期类型、tiling 结构体、CPU 仿真数值、编译耗时 | 端到端算子精度（与 golden 对比） |
| 框架 | gtest（`TEST_F` + `EXPECT_*`） | 普通 `main()`，用 `VerifyResults` 打印 passed/failed |
| 成功标志 | gtest 的 `[  PASSED  ]` | 日志含 `Accuracy verification passed` |
| 是否需要真机 | 否 | 是（或 cannsim） |

#### 4.1.2 核心流程：build.sh 的三种互斥模式

`scripts/build.sh` 是统一入口，用 `--host_ut` / `--example` / `--st` 三个**互斥**模式编排行为。模式解析逻辑如下：

[scripts/build.sh:82-93](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L82-L93)：三个模式互斥（同时只能给一个），模式名后可跟一个可选的 `target_name`。

[scripts/build.sh:131-144](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L131-L144)：模式到 CMake target 的映射规则——

- `--host_ut`：默认 target 为 `host_ut`（聚合目标，构建并运行全部 host 用例）；若指定名字则只构建那一个。
- `--example`：默认 target 为 `atvoss_examples`。
- `--st`：默认 target 为 `st`（聚合全部 ST 用例）；若指定名字则只构建那一个。

构建完成后，针对 host_ut 还有一段「编译单个 UT 后立即运行」的逻辑：

[scripts/build.sh:187-193](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L187-L193)：当 `MODE == host_ut` 且指定了 `TARGET_NAME` 时，直接执行 `build/tests/ut/host/<name>` 这个可执行文件。

而 `--st`（不带名字）则会在 cannsim 里跑三个代表性用例，并 grep 日志里的成功标志：

[scripts/build.sh:242-274](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L242-L274)：跑 `test_block_cast12`、`test_tile_rms_norm_14`、`test_compute_buffer_reuse` 三个用例，用 `cannsim record` 录制执行，最后用 `grep "Accuracy verification passed"` 判定成败。

> 注意：`build.sh` 一开头就强制检查 `ASCEND_HOME_PATH` 环境变量（[scripts/build.sh:63-67](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L63-L67)），未设置直接退出。这是所有编译的硬性前置。

#### 4.1.3 常用命令速查

```bash
# 必须先设置 CANN 安装目录
export ASCEND_HOME_PATH=/usr/local/Ascend/ascend-toolkit/latest

# UT：构建并运行全部 host 单测（聚合 target host_ut）
bash scripts/build.sh -DSOC=ascend950 --host_ut

# UT：只构建并运行单个 host 单测（编译后自动执行）
bash scripts/build.sh -DSOC=ascend950 --host_ut test_arguments

# ST：构建全部系统测试（聚合 target st），并用 cannsim 跑 3 个代表用例
bash scripts/build.sh -DSOC=ascend950 --st

# ST：只构建某个系统测试（不自动跑仿真）
bash scripts/build.sh -DSOC=ascend950 --st test_op_adds_lhs
```

#### 4.1.4 代码实践

**实践目标**：在不实际编译的前提下，通过阅读 `build.sh` 画出「命令 → CMake target → 运行行为」的映射表。

**操作步骤**：

1. 阅读 [scripts/build.sh:44-56](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L44-L56) 的 `show_help`，确认三种模式互斥。
2. 阅读 [scripts/build.sh:131-144](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L131-L144)，确认默认 target。
3. 阅读 [scripts/build.sh:187-193](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L187-L193) 与 [scripts/build.sh:242-274](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L242-L274)，确认运行后处理。

**需要观察的现象**：你应该能在脑中填出下表（答案见 4.1.5）。

**预期结果**：

| 命令 | CMake target | 编译后是否自动运行 |
|------|--------------|---------------------|
| `--host_ut` | `host_ut` | 是（POST_BUILD 跑全部） |
| `--host_ut test_arguments` | `test_arguments` | 是（build.sh 直接执行） |
| `--st` | `st` | 是（cannsim 跑 3 个代表） |
| `--st test_op_adds_lhs` | `test_op_adds_lhs` | 否（仅编译） |

> 上述运行结果中，`--host_ut`、`--st` 的实际编译与运行依赖真实的 CANN 环境（`ASCEND_HOME_PATH` + bisheng + cannsim）。本环境不一定具备，若无法运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build.sh` 规定 `--host_ut`/`--example`/`--st` 三个模式互斥？

**参考答案**：因为它们映射到不同的 CMake target 与不同的「编译后运行」策略（host_ut 跑 gtest、example/st 跑 cannsim），混用会让 target 名与运行行为无法对应；[scripts/build.sh:82-86](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L82-L86) 在检测到重复模式时直接报错退出。

**练习 2**：`--st`（不带名字）实际只 cannsim 了 3 个用例，但 `st` target 明明编译了全部约 50 个用例，这矛盾吗？

**参考答案**：不矛盾。`st` 聚合 target 负责把全部 `test_*.cpp` 都编译并 install 到 `output/bin/`（见 4.5.3）；而 [scripts/build.sh:243-247](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L243-L247) 只是**挑选**了 3 个代表性用例做 cannsim 回归。其余用例需手动指定名字或在真机上运行。

### 4.2 UT 之 host：编译期类型断言

#### 4.2.1 概念说明

`tests/ut/host/` 下的 4 个用例是最「便宜」的测试：它们编译成 Host 可执行文件、用 gtest 跑、不碰 NPU，却能在**编译期**就锁死一大批关键不变量（invariant）。秘诀在于：ATVOSS 的很多「行为」是编码在 C++ **类型**里的，于是可以用 `std::is_same_v` 比较两个类型是否相等，再用 `EXPECT_TRUE` 把这个编译期常量塞进运行期断言。

#### 4.2.2 核心流程

1. CMake 用 `file(GLOB *.cpp)` 自动收集 `tests/ut/host/*.cpp`，每个 `.cpp` 编成一个独立可执行文件。
2. 所有 target 名收集进 `ALL_UT_TARGETS`，再聚合成一个 `host_ut` 自定义目标。
3. `host_ut` 的 POST_BUILD 钩子会**依次执行**每一个用例可执行文件。

[tests/ut/host/CMakeLists.txt:25-67](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/CMakeLists.txt#L25-L67)：GLOB 收集 + 逐个建 target + 链接 gtest。注意编译选项里用的是 `--npu-arch=dav-2201 -xasc`（[tests/ut/host/CMakeLists.txt:51-56](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/CMakeLists.txt#L51-L56)），让 Ascend C 语法能在 Host 上编译。

[tests/ut/host/CMakeLists.txt:72-90](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/CMakeLists.txt#L72-L90)：`host_ut` 聚合 target + POST_BUILD 逐个运行用例。这就是 `bash scripts/build.sh --host_ut` 默认会跑全部 host 用例的根源。

#### 4.2.3 源码精读

**用例 A：`test_arguments` —— 验证 ArgumentsBuilder 的 tuple 布局**

[tests/ut/host/test_arguments.cpp:88-100](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_arguments.cpp#L88-L100)：构造 3 个 `Tensor<float>` 与一个标量 `a = 1.0f`，用 `ArgumentsBuilder{}.inputOutput(t1, t2, a, t3).build()` 组装入参，然后断言「第 0 层 tuple 的第 2 号元素等于 1.0f」。

关键两行：

```cpp
auto arguments = Atvoss::ArgumentsBuilder{}.inputOutput(t1, t2, a, t3).build();
auto arg = std::get<2>(std::get<0>(arguments));   // 第0层tuple的第2号 = a
EXPECT_EQ(arg, 1.0f);
```

这正好印证了 u2-l6 的结论：`.build()` 产出两层 `std::tuple`，第 0 位是 inputOutput 实参元组，且**顺序与传入顺序一致**——所以 `std::get<2>` 取到的就是第 3 个参数 `a`。这个用例把「入参顺序契约」变成了一个可运行的数值断言。

**用例 B：`test_expr_linearizer` —— 编译期类型相等性断言**

这是本讲最有「ATVOSS 特色」的测试。它要验证：用户写出的嵌套表达式 `xx`，经过 `ToLinearizerExpr` 线性化后，得到的类型**与手写扁平形式 `xx1` 完全一致**。

[tests/ut/host/test_expr_linearizer.cpp:44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp#L44)：用户原始写法（含自动临时变量 `_1`/`_2`/...）：

```cpp
auto xx = (out = in2 * _5, out2 = in2 + _5, out3 = in2 / _4y);
```

[tests/ut/host/test_expr_linearizer.cpp:46-50](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp#L46-L50)：手写的、已用 `PlaceHolderTmpLike` 显式展开成 `temp`/`temp1`/... 的「标准答案」形式 `xx1`。

[tests/ut/host/test_expr_linearizer.cpp:52-53](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp#L52-L53)：断言两者类型相同：

```cpp
constexpr bool sameType = std::is_same_v<decltype(xx1), decltype(Atvoss::ToLinearizerExpr(xx))>;
EXPECT_TRUE(sameType);
```

> 这里的精妙之处：整个比较在**编译期**就完成（`std::is_same_v` 返回 `constexpr bool`），`EXPECT_TRUE` 只是把编译期结果搬到运行期报告。如果哪天 `ToLinearizerExpr` 的线性化逻辑改坏了，`sameType` 就会变成 `false`，用例失败。这是一种「以类型为 oracle（标准答案）」的测试范式。

**用例 C：`test_utility` —— 验证 `Unique_t` 去重**

[tests/ut/host/test_utility.cpp:18-22](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_utility.cpp#L18-L22)：构造含重复元素的 `TypeList<int, float, int, bool, char, char, long>`，断言 `Unique_t` 后等于 `TypeList<int, float, bool, char, long>`。`Unique_t` 是 u2-l2 里收集 Param/LocalVar 时去重的底层工具，这里直接对它做单元测试。

**用例 D：`test_elewise_tiling` —— 验证 tiling 结构体**

[tests/ut/host/test_elewise_tiling.cpp:42-54](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_elewise_tiling.cpp#L42-L54)：调用 `CalculateTiling<KernelOp>(arguments, cfg)`，断言成功 `ASSERT_TRUE(result)` 且 `sizeof(cfg) == 56`。这把 u2-l7/u2-l8 讲的「Host 侧一次性算 tiling」落成了一个可断言的事实：tiling 结构体（含 kernelParam + blockParam）在 ascend950 上固定占 56 字节。

#### 4.2.4 代码实践

**实践目标**：亲手写一个最小的编译期类型断言用例，体会「以类型为 oracle」的写法。

**操作步骤**（源码阅读型实践，不修改项目源码）：

1. 仿照 [tests/ut/host/test_utility.cpp:18-22](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_utility.cpp#L18-L22)，在草稿纸上写一个 gtest 用例：构造 `TypeList<int, int, int>`，断言 `Unique_t` 后等于 `TypeList<int>`。
2. 推断：如果 `Unique_t` 实现错误（比如没去重），`std::is_same_v` 会得到什么？用例会怎样？
3. 阅读 [tests/ut/host/test_expr_linearizer.cpp:52-53](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp#L52-L53)，回答：为什么这里用 `EXPECT_TRUE(sameType)` 而不是直接比较数值？

**需要观察的现象**：你会意识到这类测试**不需要构造运行期数据**（`test_arguments` 里的 `Tensor` 甚至传了 `nullptr` 当指针，见 [test_arguments.cpp:93](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_arguments.cpp#L93)），因为被测对象是类型/结构，不是数值。

**预期结果**：理解「编译期断言 + gtest 报告」的组合，是 ATVOSS 用最小的运行期代价锁定编译期不变量的手法。

#### 4.2.5 小练习与答案

**练习 1**：`test_arguments` 里三个 `Tensor` 都用 `nullptr` 构造，为什么用例还能通过？

**参考答案**：因为该用例只验证 `ArgumentsBuilder` 把入参**按顺序**放进两层 tuple 的布局（`std::get<2>(std::get<0>(arguments))`），并不解引用设备指针。`Tensor` 的指针值不参与断言，所以 `nullptr` 无妨（见 [test_arguments.cpp:93-99](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_arguments.cpp#L93-L99)）。

**练习 2**：把 `test_expr_linearizer` 里的 `constexpr bool sameType` 改成普通 `bool`，用例还能正确工作吗？

**参考答案**：能。`std::is_same_v` 本身返回 `bool`，`constexpr` 只是让它可在编译期求值；去掉 `constexpr` 后比较仍在编译期内联完成、运行期只是读一个常量。但保留 `constexpr` 更能体现「这是编译期结果」的意图。

### 4.3 UT 之 builtin_kernel / builtin_tiling：CPU 仿真执行

#### 4.3.1 概念说明

`host/` 下的用例只验证「编译期结构」，不真正执行计算。而 `builtin_kernel` 与 `builtin_tiling` 更进一步：它们借助 `tikicpulib`（Ascend C 的 CPU 仿真库）与 `ICPU_RUN_KF` 宏，在 **Host CPU 上真正模拟执行一次 AICore kernel**，并校验输出数值。这介于「纯编译期单测」与「需要真机的 ST」之间，是一个性价比很高的中间层。

二者共享一个配置头文件 [tests/ut/builtin_kernel/abs_config.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/builtin_kernel/abs_config.h)，里面定义了 `AbsConfig`——其结构与 example 里的 `Config` 完全同构（TileShape、Compute、BlockOp/KernelOp），只是被抽出供 UT 复用。

#### 4.3.2 核心流程

1. 在 `__global__ __aicore__` 函数里用 `REGISTER_TILING_DEFAULT` / `GET_TILING_DATA_WITH_STRUCT` 解析 tiling，再调用 `KernelOp::Run`。
2. 在 gtest 用例里：先用 `CalculateTiling` 算出 tiling 结构体；再用 `AscendC::GmAlloc` 分配仿真 GM、填入输入；最后用 `ICPU_RUN_KF` 触发 kernel 在 CPU 上执行；取出输出与期望值比较。
3. `builtin_tiling` 只做第 2 步的前半段（算 tiling + 查 sizeof），不执行 kernel。

#### 4.3.3 源码精读

**builtin_kernel：真正跑一次 abs kernel**

[tests/ut/builtin_kernel/test_builtin_kernel.cpp:22-31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/builtin_kernel/test_builtin_kernel.cpp#L22-L31)：定义被测的 `AbsKernel`，内部 `KernelOp op; op.Run(tilingData, x, y);`——这正是 u2-l8 讲的 Kernel 层入口。

[tests/ut/builtin_kernel/test_builtin_kernel.cpp:43-62](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/builtin_kernel/test_builtin_kernel.cpp#L43-L62)：用例主体——算 tiling、分配 GM、填 `-2.1f`、触发仿真、断言输出全为 `2.1f`（即 `abs(-2.1)=2.1`）。

关键三步：

```cpp
auto result = Atvoss::CalculateTiling<KernelOp>(arguments, cfg);  // 算 tiling
// ... GmAlloc + 填入 x = -2.1f ...
ICPU_RUN_KF(AbsKernel<0>, cfg.kernelParam.blockNum,
            x_data.get(), y_data.get(), nullptr, reinterpret_cast<uint8_t*>(&cfg));  // CPU 仿真执行
EXPECT_EQ(y, expectedValue);  // 期望全 2.1f
```

`ICPU_RUN_KF` 的第二个参数 `cfg.kernelParam.blockNum` 决定了**模拟多少个核**（见 u2-l8），`nullptr` 是 workspace，最后把 tiling 结构体的裸指针传进去——与真机启动 `KernelCustom<<<blockNum>>>` 的参数一一对应。

**builtin_tiling：只验 tiling，不跑 kernel**

[tests/ut/builtin_tiling/test_builtin_tiling.cpp:17-29](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/builtin_tiling/test_builtin_tiling.cpp#L17-L29)：它 `#include "../builtin_kernel/abs_config.h"` 复用配置，只调用 `CalculateTiling` 并断言 `sizeof(tilingData) == 56`。这等价于 `host/test_elewise_tiling`，但配置来自共享头文件，体现了 UT 内部的代码复用。

#### 4.3.4 代码实践

**实践目标**：对照 `builtin_kernel` 与 `host/test_elewise_tiling`，理解「跑 kernel」与「只算 tiling」两个验证粒度的差别。

**操作步骤**：

1. 阅读 [test_builtin_kernel.cpp:56-57](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/builtin_kernel/test_builtin_kernel.cpp#L56-L57)，找到 `ICPU_RUN_KF`，记下它传了几个参数、各代表什么。
2. 阅读 [test_builtin_tiling.cpp:27-29](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/builtin_tiling/test_builtin_tiling.cpp#L27-L29)，确认它没有 `ICPU_RUN_KF`。
3. 思考：如果 abs 的 `Abs` 求值器（见 u3-l1）写错了符号（比如算成负数），哪个用例会失败？

**需要观察的现象**：`builtin_kernel` 会失败（输出不是 `2.1f`），而 `builtin_tiling` 仍会通过——因为它压根没执行计算。这说明两者覆盖的缺陷类型不同：前者覆盖「计算正确性」，后者只覆盖「tiling 计算与结构布局」。

**预期结果**：建立「验证粒度」的概念——`host/`（编译期结构）< `builtin_tiling`（tiling 正确性）< `builtin_kernel`（CPU 仿真数值）< `st`（端到端真机/仿真精度）。

> 实际运行需 CANN 的 `tikicpulib` 与 `--npu-arch=dav-2201 -xasc` 工具链。若本地无该环境，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`ICPU_RUN_KF` 的第二个参数为什么是 `cfg.kernelParam.blockNum`，而不是写死成 1？

**参考答案**：因为 Kernel 层是多核并行的（见 u2-l8），`blockNum` 是 `CalculateTiling` 根据总元素数与 `CORE_NUM` 算出来的核数。`ICPU_RUN_KF` 需要按这个核数循环模拟每个核的 `GetBlockIdx()` 行为，才能忠实复现多核切分；写死成 1 会漏掉多核调度逻辑。

**练习 2**：`builtin_tiling` 复用 `builtin_kernel/abs_config.h` 而不是自己重写一份 `AbsConfig`，这样做的好处是什么？

**参考答案**：避免配置漂移——保证「跑 kernel 的 UT」与「算 tiling 的 UT」用的是同一份 Compute 表达式与 TileShape，若配置改了只需改一处。这是 UT 内部 DRY（Don't Repeat Yourself）原则的体现。

### 4.4 UT 之 compile_perf：度量编译期开销

#### 4.4.1 概念说明

这是整个测试体系里**最独特**的一类。普通项目度量「运行多快」，ATVOSS 还要度量「**编译多快**」。原因在于：表达式模板把计算逻辑全部塞进 C++ 类型系统，120 个节点的级联表达式会让 bisheng 编译器在模板实例化上花费数十秒。一旦某次重构让编译时间悄悄翻倍，所有开发者的迭代速度都会受损，却没有任何功能用例能捕捉到——因为结果仍然正确。

`compile_perf` 就是为守护这条「隐形基线」而存在：它用 `system()` 调用 bisheng 去编译几个超大表达式用例，用 `std::chrono` 测墙钟时间，再用 `EXPECT_LE(time, 阈值)` 断言「编译没有变慢」。

#### 4.4.2 核心流程

1. 测试程序本身用普通 g++ 编译（见 [compile_perf/CMakeLists.txt:13-14](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/CMakeLists.txt#L13-L14)），它只是一个「驱动器」。
2. 运行时，驱动器用 `system("bisheng ... -c cases/<file>.cpp")` 编译某个 case 文件，并用 `std::chrono::steady_clock` 测耗时。
3. 用 `EXPECT_LE(耗时, 阈值)` 判定。阈值旁的注释记录了历史观测值（如 wsl 72-81s、linux 77-81s），便于人工评估漂移。

#### 4.4.3 源码精读

**编译命令的构造**

[tests/ut/compile_perf/compile_perf_test.cpp:31-50](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/compile_perf_test.cpp#L31-L50)：`GetIncludePath` 把 CANN 的若干 include 子目录与项目 `include/` 拼成一长串 `-I` 选项——这串路径通过 CMake 的 `-DASCEND_DIR=...` / `-DPROJECT_DIR=...` 宏注入（见 [CMakeLists.txt:22-27](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/CMakeLists.txt#L22-L27)）。

[tests/ut/compile_perf/compile_perf_test.cpp:52-70](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/compile_perf_test.cpp#L52-L70)：`GetCompileCmd` 拼出完整的 bisheng 命令，关键编译选项是：

```cpp
"-g -fPIE -fdiagnostics-color=always -O3 -w -std=gnu++17 "
"--npu-arch=dav-2201 "
"-xasc";
```

注意 `-xasc` 与 `--npu-arch=dav-2201`，和 host UT 一致——目的是让 case 文件里的 Ascend C 表达式能被 bisheng 正确解析。

**耗时度量**

[tests/ut/compile_perf/compile_perf_test.cpp:82-96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/compile_perf_test.cpp#L82-L96)：用 `steady_clock` 在 `system()` 前后取样，差值转为秒：

```cpp
auto start = std::chrono::steady_clock::now();
auto result = ExecCompileCmd(cmd);
auto end = std::chrono::steady_clock::now();
auto usedTime = std::chrono::duration_cast<std::chrono::seconds>(end - start).count();
```

**阈值断言：三个优化档位的对比**

[tests/ut/compile_perf/compile_perf_test.cpp:99-121](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/compile_perf_test.cpp#L99-L121)：四个用例及其阈值：

| 用例 | case 文件 | 阈值 | 含义 |
|------|-----------|------|------|
| `long_expr_op` | `120_expr_op.cpp` | ≤90s | 120 节点基线，无特殊优化 |
| `long_expr_op_with_bind_buff` | `120_expr_op_with_bind_buff.cpp` | ≤80s | 带 bind buffer |
| `long_expr_op_with_auto` | `120_expr_op_with_auto.cpp` | ≤50s | 带 auto 策略（最快） |
| `expr_linearizer_perf` | `expr_linearizer_perf.cpp` | ≤50s | 线性化器本身的编译开销 |

> 阈值的递减（90 → 80 → 50）直观反映了 u3-l8 讲的 `MemMngPolicy::AUTO` 相对 MANUAL 的编译期优势——auto 策略走 `FullAutoDag`，能消除冗余中间量，模板实例化更少，编译更快。这是一条「编译期性能」与「图优化策略」挂钩的隐藏证据链。

**case 文件长什么样**

[tests/ut/compile_perf/cases/120_expr_op.cpp:55-104](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/cases/120_expr_op.cpp#L55-L104)：把 RMSNorm 的 10 个表达式**重复 12 次**得到 120 个节点，全部塞进一个 `return (...)`。这种「刻意造大」的文件就是给编译器施压的压测素材。

#### 4.4.4 代码实践

**实践目标**：理解 compile_perf 如何把「编译时间」变成一条可断言的回归基线。

**操作步骤**（源码阅读型实践）：

1. 阅读 [compile_perf_test.cpp:99-114](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/compile_perf_test.cpp#L99-L114)，对比三个 `120_expr_op*` 用例的阈值。
2. 打开 [cases/120_expr_op.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/cases/120_expr_op.cpp)，数一下 `return (...)` 里有多少个逗号分隔的表达式（应为 120 个）。
3. 思考：如果有人把 `FullAutoDag` 的某个 Pass 改成了 \(O(n^2)\) 的低效实现，功能用例（test_compute_*）全过，哪个测试会变红？

**需要观察的现象**：你会意识到 `long_expr_op_with_auto` 的阈值（50s）远低于 `long_expr_op`（90s），这正是 auto 策略消除冗余带来的编译期红利。

**预期结果**：理解「编译性能回归」是一种**功能正确但开发体验恶化**的退化，必须用专门的计时测试守护，普通精度测试无法发现。

> 实际运行需 bisheng 与完整 CANN include。编译单个 case 耗时数十秒，全套跑完可能数分钟。若无环境，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compile_perf` 用 `EXPECT_LE(time, 阈值)` 而不是 `EXPECT_EQ`？

**参考答案**：编译时间受机器负载、CPU 频率、缓存状态等影响，每次都有抖动（注释里写「偶尔能上到 114s」就是例证），不可能等于某个固定值。用上界 `EXPECT_LE` 容忍正常抖动，只捕捉「明显变慢」的回归。

**练习 2**：`compile_perf` 测试程序自身用 g++ 编译（[CMakeLists.txt:13](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/compile_perf/CMakeLists.txt#L13)），但它驱动的 case 用 bisheng 编译。为什么不用同一个编译器？

**参考答案**：驱动器只是个「计时 + 调 system()」的普通 C++ 程序，用系统自带 g++ 即可，无需 Ascend 工具链；而被测的 case 文件含 Ascend C 语法（`-xasc`、`--npu-arch`），必须用 bisheng 才能编译。两者职责不同，自然用不同编译器。

### 4.5 ST 系统测试：四大测试维度

#### 4.5.1 概念说明

`tests/st/` 下约有 50 个 `test_*.cpp`，每个都被编成一个独立的 NPU 可执行文件，走完整 ACL 链路并在真机/cannsim 上验证精度。按文件名前缀，它们自然分成四大维度：

| 前缀 | 维度 | 典型用例 | 验证目标 |
|------|------|----------|----------|
| `test_op_*` | 单算子功能 | `test_op_adds_lhs.cpp` | 单个算子（add/sub/mul/div/exp/power...）在标量左/右不同位置时的数值正确性 |
| `test_compute_*` | Compute 表达式结构 | `test_compute_cascade.cpp` | 复杂/级联/含标量/含冗余的 Compute 表达式，以及不同 `MemMngPolicy` 下的正确性与缓冲复用 |
| `test_tile_rms_norm_*` | Tile 层归约 | `test_tile_rms_norm_14.cpp` | 二维 TileShape 下含 ReduceSum/Broadcast 的归约算子（rms_norm 各种变体） |
| `test_block_cast*` | Block 层类型转换 | `test_block_cast1.cpp` | Block 层 Cast（f16↔f32 等）在不同 tileShapeLen 下的正确性 |

另有 `test_cast_elimination.cpp`（验证冗余 Cast 消除 Pass）与 `test_scalar_*`（标量入参顺序）等专项用例。

#### 4.5.2 核心流程：CMake 自动建 target

[tests/st/CMakeLists.txt:54-95](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L54-L95)：定义宏 `atvoss_example_add_executable(NAME)`，干了几件事——

- 清空 `CMAKE_CXX_FLAGS`，强制用 bisheng 作编译器/链接器（[CMakeLists.txt:61-63](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L61-L63)）。
- 给源文件打上 `--npu-arch=${NPU_ARCH} -xasc` 编译属性（[CMakeLists.txt:69-78](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L69-L78)），其中 ascend950 → `dav-3510`（[CMakeLists.txt:17-24](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L17-L24)）。
- 链接 `ascendcl/platform/register/tiling_api/runtime`（[CMakeLists.txt:39-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L39-L45)）。
- 把 target 挂到聚合 `st` 上（`add_dependencies(st ${NAME})`），并 install 到 `bin/`，同时打两个 COMPONENT：自身名字与 `st`（[CMakeLists.txt:92-94](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L92-L94)）——后者让 `cmake --install --component st` 能一次安装全部 ST。

[tests/st/CMakeLists.txt:98-102](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L98-L102)：用 `file(GLOB test_*.cpp)` 自动收集，对每个文件调宏建 target。**新增 ST 用例只需丢一个 `test_*.cpp` 进目录，无需改 CMake。**

#### 4.5.3 源码精读：两类典型用例的写法差异

本小节对照「单算子功能验证」与「Compute 表达式结构验证」，这是本讲代码实践任务的核心。

**A. 单算子功能验证：`test_op_adds_lhs.cpp`**

[tests/st/test_op_adds_lhs.cpp:22-40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp#L22-L40)：`AddsLhsConfig` 的 `Compute()` 极简——只有一个 `return (out = other + in)`，专门测「标量在加号左边」这一种情形。

```cpp
auto in    = Atvoss::PlaceHolder<1, Tensor<T1>, Atvoss::ParamUsage::IN>();
auto out   = Atvoss::PlaceHolder<2, Tensor<T2>, Atvoss::ParamUsage::OUT>();
auto other = Atvoss::PlaceHolder<3, T1, Atvoss::ParamUsage::IN>();   // 标量
return (out = other + in);   // 标量 + 张量
```

[tests/st/test_op_adds_lhs.cpp:42-103](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp#L42-L103)：标准 `Run()` 十步样板（ACL 初始化→SetDevice→Context→Stream→Malloc→Memcpy→构造 Args→`deviceOp.Run`→Sync+Memcpy 回→校验）。输入填 `3`、标量 `other=5`，于是 golden = `8`（[test_op_adds_lhs.cpp:97](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp#L97)）。这类用例的 Compute 简单到一目了然，重点是**把一种算子在一种操作数排布下的数值钉死**。

**B. Compute 表达式结构验证：`test_compute_cascade.cpp`**

[tests/st/test_compute_cascade.cpp:23-40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_cascade.cpp#L23-L40)：`Compute()` 是一个含括号嵌套的级联表达式：

```cpp
return (out = (in1 + in1) * in2 * (in1 + in2));
```

输入 `in1=3, in2=2`，于是 \((3+3)\times2\times(3+2)=6\times2\times5=60\)，golden = `60`（[test_compute_cascade.cpp:103](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_cascade.cpp#L103)）。这类用例的重点不是单个算子，而是**表达式树的结构**（嵌套括号、复用同一 Param、混合运算）能否被线性化器、DAG、求值器正确处理。

**C. 策略对比：`test_compute_autobuffer_redundant_with_manupolicy.cpp`**

[tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp:29-40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp#L29-L40)：表达式里故意写了冗余量 `_1 = in1+in2`（最终没用），并显式用 `MemMngPolicy::MANUAL`：

```cpp
auto _1 = in1 + in2;       // 冗余，最终未参与输出
auto _2 = in1 * in2;
return (out = _2);
```

`blockPolicy{TileShape{}, Atvoss::MemMngPolicy::MANUAL}`（[第40行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp#L40)）。输入 `in1=2, in2=3`，golden = `6`。对照同目录的 `..._with_autopolicy.cpp`，可观察 AUTO（消除冗余）与 MANUAL（保留冗余）两种策略下结果一致、缓冲分配不同——这正是 u3-l8 的端到端佐证。

**D. Cast 消除与 Block cast**

[tests/st/test_cast_elimination.cpp:43-51](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_cast_elimination.cpp#L43-L51)：表达式里堆了大量 `Cast<CAST_NONE, T>`，用来压测 u3-4 的冗余 Cast 消除 Pass：若 Pass 工作正常，最终精度仍为 golden = `2.0f`。

[tests/st/test_block_cast1.cpp:36-42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_block_cast1.cpp#L36-L42)：Block 层 cast（f16→f32），带 `MemMngPolicy::MANUAL` 与命令行 shape 解析（`Options` + `command_line.h`），用于覆盖不同 shape/tileShapeLen 组合。

#### 4.5.4 代码实践（本讲主实践任务）

**实践目标**：通过对照阅读，总结 ST 中「单算子功能验证」与「Compute 表达式结构验证」两类用例的写法差异。

**操作步骤**：

1. 打开 [tests/st/test_op_adds_lhs.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp) 与 [tests/st/test_compute_cascade.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_cascade.cpp)。
2. 分别记录两者的：① `Compute()` 表达式复杂度；② 输入数据/golden；③ `Run()` 样板是否几乎一致。
3. 用下表总结差异（答案见 4.5.5）。

**需要观察的现象**：你会发现两者的 `Run()` 十步样板**几乎逐字相同**，差异**只在 `Compute()` 的 return 表达式与 golden 值**。这印证了 u1-l4 的结论——ATVOSS 算子的差异被压缩到了一行公式。

**预期结果**：

| 维度 | `test_op_adds_lhs`（单算子） | `test_compute_cascade`（表达式结构） |
|------|------------------------------|---------------------------------------|
| `Compute()` | `out = other + in`（一个运算） | `out = (in1+in1)*in2*(in1+in2)`（嵌套级联） |
| 验证重点 | 单算子在标量左操作数下的数值 | 复杂表达式树被正确线性化/求值 |
| 输入/golden | in=3, other=5 → 8 | in1=3, in2=2 → 60 |
| `Run()` 样板 | 十步 ACL 样板 | 十步 ACL 样板（几乎相同） |
| 参数个数 | 3（in, out, scalar） | 3（in1, in2, out） |

**第二步（运行 host 测试）**：

```bash
export ASCEND_HOME_PATH=<你的 CANN 安装目录>
bash scripts/build.sh -DSOC=ascend950 --host_ut
```

**需要观察的现象**：构建系统会依次编译 `test_arguments`/`test_expr_linearizer`/`test_elewise_tiling`/`test_utility` 并在 POST_BUILD 阶段逐个执行，最终汇总 gtest 的 `[  PASSED  ]` / `[  FAILED  ]` 统计。

**预期结果**：四个 host 用例全部 PASSED。若本地无 CANN 工具链（bisheng / dav-2201 `-xasc` 支持），此步无法完成，标注「待本地验证」，但仍可凭源码完成上表的差异分析。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `test_op_adds_lhs` 与 `test_op_adds_rhs` 要分成两个文件，而不是在一个文件里测两种排布？

**参考答案**：因为 ST 用例的 `Compute()` 写在 `Config` 结构体里，一种表达式对应一种 `Compute` 类型，进而对应一种 `DeviceOp` 类型。分成两个文件让每个文件自成体系、CMake 各建一个 target、可独立 cannsim，符合「每个 `test_*.cpp` 一个可执行文件」的目录约定（见 [CMakeLists.txt:98-102](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L98-L102)）。

**练习 2**：`test_compute_autobuffer_redundant_with_manupolicy` 里 `_1 = in1+in2` 是冗余的（没用），为什么 golden 仍是确定的 `6`？

**参考答案**：冗余量只影响**缓冲分配**（MANUAL 会为 `_1` 保留一块 UB，AUTO 会消除它），不影响**数值结果**——因为 `_1` 没有参与 `out = _2`，无论它是否被分配缓冲，输出都只由 `_2 = in1*in2 = 2*3 = 6` 决定。这正是 u3-l8 所说「AUTO 与 MANUAL 数值一致、差别仅在缓冲」的体现。

### 4.6 ST 用例的 Config 写法模式

#### 4.6.1 概念说明

虽然 ST 有 50 个用例，但它们的骨架高度统一：一个 `XxxConfig` 结构体（封装 Compute + Builder 三件套）+ 一个 `Run()` 函数模板（固定十步 ACL 样板）+ 一个 `main()`。掌握这个模式，就能快速读懂任意一个 ST 用例，也能照葫芦画瓢写新的。

#### 4.6.2 核心流程：Config 与 Run 的分工

```
XxxConfig<T1, T2, ...>          ← 编译期：Compute 表达式 + 三级 Builder
   ├── XxxCompute::Compute()     ← 用户唯一写公式处
   ├── TileShape / blockPolicy   ← 含 MemMngPolicy（默认 AUTO）
   ├── kernelPolicy              ← 默认 UniformSegment
   └── BlockOp/KernelOp/DeviceOp ← 三级套娃

Run<T1, T2, ...>()               ← 运行期：固定十步
   aclInit → SetDevice → CreateContext → CreateStream
   → Malloc → Memcpy(H2D) → ArgumentsBuilder → deviceOp.Run
   → Sync → Memcpy(D2H) → VerifyResults

main()                           ← 选具体类型调用 Run<...>()
```

#### 4.6.3 源码精读

**Config 的三种典型形态**

形态一（最简，无显式 blockPolicy）：[test_op_adds_lhs.cpp:36-39](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp#L36-L39)，`BlockBuilder` 只传 `Compute` 与 `ArchTag`，blockPolicy/kernelPolicy 全走默认（AUTO + UniformSegment）。

形态二（二维 TileShape + 显式 policy）：[test_cast_elimination.cpp:54-62](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_cast_elimination.cpp#L54-L62)，`TileShape = Shape<HEIGHT, WIDTH>`（归约/广播用二维），`blockPolicy`/`kernelPolicy` 显式写出，`BlockBuilder` 传满四个模板实参。

形态三（带 MemMngPolicy::MANUAL）：[test_block_cast1.cpp:42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_block_cast1.cpp#L42) 与 [test_compute_autobuffer_redundant_with_manupolicy.cpp:40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp#L40)，`DefaultBlockPolicy<TileShape>{TileShape{}, Atvoss::MemMngPolicy::MANUAL}`——这是切换到 `ManualDag` 的唯一开关（见 u3-l8）。

**Run() 十步样板与 RAII**

以 [test_op_adds_lhs.cpp:45-103](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp#L45-L103) 为模板，每一步 ACL 资源都用 `ReleaseSource(...)` 造的 RAII 守卫托管（其定义见 [examples/common/example_common.h:58-71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L58-L71)），靠 C++ 栈 LIFO 析构得到正确的释放顺序——这正是 u1-l5 讲过的模式，ST 全部沿用。

**精度校验**

[examples/common/example_common.h:36-56](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/common/example_common.h#L36-L56)：`IsClose` 用「绝对容差 OR 相对容差」判定，`VerifyResults` 逐元素比对并在失败时打印首个不匹配下标。成功则打印 `Accuracy verification passed`——这正是 cannsim 日志里 grep 的成功标志。

#### 4.6.4 代码实践

**实践目标**：照着模式，在纸上为一个新的 ST 用例搭骨架（不实际新增文件）。

**操作步骤**：

1. 假设要测「`out = in1 - in2`（逐元素减）」，参照 [test_op_adds_lhs.cpp:22-40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp#L22-L40) 写出 `SubConfig` 的 `Compute()`。
2. 确定输入与 golden：若 `in1=7, in2=3`，golden 应是多少？
3. 确认 `Run()` 十步样板可原样复用，唯一要改的是 `using DeviceOp = typename SubConfig<...>::DeviceOp;`。
4. 思考：若想同时验证 AUTO 与 MANUAL 两种策略，应该改哪里？

**需要观察的现象**：你会发现新增一个 ST「单算子」用例，真正要动的只有 `Compute()` 一行 + golden 值 + `main()` 里的类型实参；其余样板可整段复制。

**预期结果**：golden = `4`（7-3）；切策略只需把 blockPolicy 的第二个参数改成 `Atvoss::MemMngPolicy::MANUAL`（参照形态三），其余不变。

> 此实践为源码阅读 + 纸面推导，无需运行；若要真正编译，把写好的文件丢进 `tests/st/` 命名为 `test_op_sub_mine.cpp`，CMake 会自动建 target（见 4.5.2），但实际编译需 CANN 环境，标注「待本地验证」。

#### 4.6.5 小练习与答案

**练习 1**：ST 用例的 `Run()` 为什么写成函数模板 `Run<T1, T2>()`，而不是直接固定类型？

**参考答案**：为了让同一个用例能在 `main()` 里用不同数据类型（如 `Run<int32_t, int32_t>()` 或 `Run<float, float>()`）调用，覆盖多种 dtype 而不重复样板代码。例如 `test_op_adds_lhs.cpp` 的 `main` 目前只调 `Run<int32_t,int32_t>()`（[第107行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_op_adds_lhs.cpp#L107)），但骨架支持随时加一行 float 实例化。

**练习 2**：`test_block_cast1` 用了 `command_line.h` 解析 `Options.shape`，而 `test_op_adds_lhs` 把 shape 写死成 `{8,0,...}`。两种做法各有什么取舍？

**参考答案**：写死 shape 简单直接，适合功能验证；命令行解析 shape 灵活，可在不改代码的前提下用 `--shape=...` 跑不同规模（如 `build.sh` 跑 examples 时就用了 `--shape=32`，见 [build.sh:221](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L221)），更适合覆盖多规模的回归。Block cast 涉及不同 tileShapeLen 与 shape 的组合，故选用命令行参数化。

## 5. 综合实践

把本讲四块内容串起来，完成一次「测试体系巡检」：

1. **读图**：画出 `tests/` 的目录树，标注 UT（4 子类）与 ST（4 维度）的归属，并在每个叶子旁写上「跑在哪（Host CPU / cannsim / 真机）」与「成功标志（gtest PASSED / Accuracy verification passed）」。
2. **命令映射**：凭 [build.sh](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh) 填出四条命令（`--host_ut`、`--host_ut test_arguments`、`--st`、`--st test_op_adds_lhs`）各自的 CMake target 与是否自动运行。
3. **缺陷定位**：针对下列每种缺陷，判断哪个（些）测试会变红——
   - `ArgumentsBuilder` 把入参顺序弄反了 → \_\_\_\_
   - `ToLinearizerExpr` 线性化结果类型变了 → \_\_\_\_
   - abs 求值器把负数算成了正数的相反数 → \_\_\_\_
   - `FullAutoDag` 某个 Pass 退化为 \(O(n^2)\) → \_\_\_\_
   - 冗余 Cast 消除 Pass 把不该删的 Cast 删了 → \_\_\_\_
4. **运行**（若本地有 CANN 环境）：执行 `bash scripts/build.sh -DSOC=ascend950 --host_ut`，记录四个 host 用例的 PASSED/FAILED 数；否则标注「待本地验证」并凭源码完成前三步。

> 参考答案（第 3 步）：① `test_arguments`；② `test_expr_linearizer`；③ `test_builtin_kernel`（CPU 仿真数值错）以及相关 `test_op_abs` 类 ST；④ `compile_perf` 的 `long_expr_op_with_auto`（编译时间超阈值）；⑤ `test_cast_elimination`（精度偏离 golden）。

## 6. 本讲小结

- ATVOSS 的测试分 **UT**（Host 侧、gtest、快）与 **ST**（端到端、NPU/cannsim、验精度）两大类，统一由 `scripts/build.sh` 的 `--host_ut` / `--st` 等互斥模式驱动。
- UT 又分四子类：`host/`（编译期类型与结构断言）、`builtin_kernel`（用 `ICPU_RUN_KF` 在 CPU 上真正仿真执行 kernel）、`builtin_tiling`（单独验 tiling）、`compile_perf`（用 chrono 度量 bisheng 编译耗时）。
- `host/` 用例体现了 ATVOSS 最具特色的测试范式：用 `std::is_same_v` 把「线性化结果类型」「TypeList 去重」等编译期不变量变成可断言的事实，`Tensor` 甚至可传 `nullptr`。
- `compile_perf` 守护的是**编译期性能**这条隐形基线——auto 策略（50s）明显快于基线（90s），是图优化策略在编译期的直接红利。
- ST 按文件名分四大维度：`test_op_*`（单算子）、`test_compute_*`（表达式结构/策略）、`test_tile_rms_norm_*`（归约）、`test_block_cast*`（类型转换）；CMake 用 `file(GLOB test_*.cpp)` 自动建 target，新增用例零配置。
- 所有 ST 用例共用「Config + `Run()` 十步 ACL 样板 + RAII 释放」的固定模式，算子差异被压缩到 `Compute()` 一行公式与 golden 值。

## 7. 下一步学习建议

- 本讲是专家篇的收尾。若你想亲手扩展测试体系，建议：仿照 `test_op_adds_lhs` 为一个尚无 ST 覆盖的算子（如 `Max`，注意 u2-l3 提到它暂无求值器特化）补一个用例，体验「一行公式 + golden」的开发节奏。
- 想深入理解被测对象本身，可回头精读：u3-l1（求值器，对应 `builtin_kernel` 跑的内容）、u3-l4（线性化与图优化 Pass，对应 `test_expr_linearizer` 与 `compile_perf` 守护的对象）、u3-l8（MemMngPolicy，对应 `test_compute_*_with_*policy` 成对用例）。
- 若关注工程化，可阅读 `tests/ut/scripts/generate_cpp_cov.sh` 与 `util.sh`（覆盖率脚本），思考如何为编译期模板库做代码覆盖率度量——这本身是个有挑战的课题。
- 至此你已完成 ATVOSS 学习手册全部讲义，建议从 u1-l1 重读一遍 README 与 abs 样例，体会「初看懵懂、再读通透」的闭环。
