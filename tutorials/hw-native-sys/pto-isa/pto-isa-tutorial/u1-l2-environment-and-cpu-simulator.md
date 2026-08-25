# 环境搭建与 CPU 模拟器快速上手

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立配置 PTO-ISA 的 CPU 开发环境（Python / CMake / C++20 编译器 / numpy）。
2. 熟练使用 `tests/run_cpu.py` 一键完成：构建 CPU 模拟器 ST 测试、运行单个用例、构建并运行 GEMM / FlashAttention demo。
3. 说清楚「CPU 模拟器」「NPU 模拟器（sim）」「板端（npu）」三种运行模式的区别，以及各自需要什么环境。
4. 读懂 demo 程序打印的校验结果与 `perf:` 性能行，并理解两层计时（脚本总耗时 vs demo 内部迭代耗时）的差异。

本讲承接上一讲（u1-l1）：你已经知道 PTO 是「一份 Tile 抽象、多套后端实现」的虚拟 ISA，本讲就带你把其中最容易上手的一套后端——CPU 模拟器——真正跑起来。

## 2. 前置知识

### 2.1 什么是「模拟器」与「板端」

- **板端（board）**：真实的 Ascend 昇腾 NPU 硬件（如 910B/910C）。代码编译成真正的昇腾内核在芯片上执行，需要安装 NPU 驱动、固件和 CANN 工具链。
- **NPU 模拟器（sim）**：CANN 工具链自带的行为级模拟器，不需要插着真实的卡，但仍然要装 CANN，只能在 Linux 上用。它对硬件流水线、事件时序的模拟比 CPU 模拟器更接近真实芯片。
- **CPU 模拟器（CPU-SIM）**：PTO-ISA 自己实现的跨平台模拟后端。因为 PTO 是 header-only（只有头文件）的模板库，只要在编译时定义 `__CPU_SIM` 宏，同一段内核代码就会在**你本机的 CPU 上**用 C++ 语义解释执行——macOS、Linux、Windows 都能跑，不需要任何昇腾环境。这是初学者最推荐的起步路径。

> 术语解释：**header-only 库**——整个库只有 `.hpp` 头文件、没有 `.so`/`.a` 二进制库，使用时 `#include` 头文件并和你的代码一起编译即可。

### 2.2 CMake 与构建目录

CMake 是 C/C++ 的构建系统生成器：它读取 `CMakeLists.txt`，在「构建目录」里生成平台对应的工程文件（Makefile 等），再由 `cmake --build` 完成实际编译。PTO-ISA 的 CPU 路径完全通过 CMake 驱动，产物（可执行文件）会落在构建目录下的 `bin/` 子目录。

### 2.3 GoogleTest（gtest）

ST（Single-instruction Test，单指令测试）用例基于 GoogleTest 框架编写，每个用例是一个 `TEST_F` 宏展开的测试类，编译成独立的可执行文件。本机没装 gtest 也没关系，构建脚本会自动从网上拉取源码编译。

### 2.4 三种运行模式对照表

| 模式 | 入口命令 | 需要的环境 | 支持平台 | 典型用途 |
|---|---|---|---|---|
| CPU 模拟器 | `python3 tests/run_cpu.py` | Python ≥ 3.11、CMake ≥ 3.16、clang++ ≥ 15 或 g++ ≥ 13、numpy | macOS / Linux / Windows | 功能验证、学习指令语义、日常开发 |
| NPU 模拟器 | `python3 tests/script/run_st.py -r sim -v a3` | Linux + CANN ≥ 8.5 | Linux | 接近硬件行为的验证 |
| 板端 | `python3 tests/script/run_st.py -r npu -v a3` | Linux + CANN + NPU 驱动/固件 | Linux（真机） | 性能实测、最终验收 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/getting-started.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md) | 官方环境指南：CPU 路径（Part 1）与 NPU 路径（Part 2）的权威说明 |
| [tests/run_cpu.py](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py) | CPU 路径的一键驱动脚本：自动探测编译器、CMake 构建、生成 golden 数据、运行 gtest / demo |
| [build.sh](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/build.sh) | 仓库级总入口脚本：封装 CPU ST、demo、NPU sim/npu、打包等所有分支 |
| [tests/cpu/st/CMakeLists.txt](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt) | CPU ST 工程的顶层 CMake：`__CPU_SIM` 宏、C++ 标准选择、gtest 解析 |
| [tests/cpu/st/testcase/CMakeLists.txt](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt) | 用例清单 `ALL_TESTCASES` 与单用例可执行文件的生成函数 |
| [demos/cpu/gemm_demo/gemm_demo.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp) | GEMM demo：32×16×32 矩阵乘，自带朴素参考实现与耗时统计 |
| [demos/cpu/flash_attention_demo/flash_attention_demo.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/flash_attention_demo/flash_attention_demo.cpp) | FlashAttention demo：完整 tile 化注意力内核与逐点校验 |
| [demos/cpu/gemm_demo/CMakeLists.txt](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/CMakeLists.txt) | demo 的 CMake：如何把 `__CPU_SIM` 传给一个普通可执行程序 |

## 4. 核心概念与源码讲解

### 4.1 环境准备：两条路径与三种运行模式

#### 4.1.1 概念说明

PTO-ISA 的官方指南把环境分成两条路径：

1. **CPU 路径（推荐初学者）**：跨平台模拟器，不需要昇腾硬件，装好 Python + CMake + C++20 编译器即可。
2. **NPU 路径（进阶）**：面向 Ascend 910B/910C（A2/A3）与 A5 平台，需要 Linux + CANN 工具链（≥ 8.5.0），板端还要装驱动和固件。

为什么 CPU 路径能跨平台？因为 PTO 是模板头文件库：`#include <pto/pto-inst.hpp>` 之后，同一份内核源码在不同宏定义下会被编译到不同后端。`__CPU_SIM` 宏把所有指令（TADD、TMATMUL……）切到 CPU 模拟实现，于是「内核」退化成一个普通的 C++ 程序，在任何有 C++20 编译器的机器上都能运行并校验数值。

#### 4.1.2 核心流程

环境准备的核心流程可以用伪代码描述：

```text
检查环境:
    python3 --version        # 期望 >= 3.11（CPU 路径）
    cmake --version          # 期望 >= 3.16（缺失时 run_cpu.py 可自动 pip 补装）
    g++ --version            # 期望 >= 13
    clang++ --version        # 或 >= 15，两者满足其一即可
    python3 -c "import numpy"  # gen_data.py 生成 golden 数据需要

若走 NPU 路径（可选）:
    安装 CANN toolkit >= 8.5.0
    source /usr/local/Ascend/cann/bin/setenv.bash
    校验 bisheng -v 与 gcc -v 版本一致
```

#### 4.1.3 源码精读

**（1）CPU 路径的依赖清单。** 官方文档明确列出：Python ≥ 3.11、CMake ≥ 3.16、支持 C++20 的编译器（Linux 上 GCC 13+ 或 Clang 15+，GCC ≥ 14 才默认启用 bfloat16 支持），以及 numpy 等三个 Python 包：

- [docs/getting-started.md:20-31](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md#L20-L31) —— CPU 模拟器的必备依赖：Git、Python、CMake、C++20 编译器、numpy/ml_dtypes/en_dtypes（后两个 dtypes 包只有个别测试需要）。

**（2）Python 虚拟环境。** 官方推荐用 venv 隔离依赖，只需安装 numpy（ml_dtypes、en_dtypes 按需）：

- [docs/getting-started.md:97-108](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md#L97-L108) —— macOS/Linux 下创建 `.venv-mkdocs` 虚拟环境并安装 numpy 的标准命令。

**（3）NPU 路径（本讲只要求「看得懂」，不要求搭建）。** NPU 路径的系统要求与 CPU 路径刻意不同——Python 只要求 ≥ 3.9、GCC ≥ 7.3，但额外要求 CANN ≥ 8.5 和 Linux：

- [docs/getting-started.md:199-207](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md#L199-L207) —— NPU 路径系统要求：Linux、Python ≥ 3.9、GCC ≥ 7.3、CMake ≥ 3.16。
- [docs/getting-started.md:231-244](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md#L231-L244) —— 安装 CANN 后必须 `source .../setenv.bash` 并用 `bisheng -v` / `gcc -v` 确认两边 GCC 版本一致（bisheng 是昇腾内核的编译器前端，版本不一致会导致链接失败）。
- [docs/getting-started.md:331-344](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md#L331-L344) —— NPU/sim 模式的标准命令 `run_st.py -r [sim|npu] -v [a3|a5]`，并说明 `-v a3` 同时覆盖 A2/A3 家族。

**（4）sim 模式的一个特殊准备。** 在 NPU 模拟器上跑全量 ST 之前要先放大文件描述符上限：

- [docs/getting-started.md:353-356](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md#L353-L356) —— `ulimit -n 65536` 后再执行 `./tests/run_st.sh --a3 --sim --all`。

对应地，总入口脚本在 sim 分支里也做了同样的事：

- [build.sh:301-305](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/build.sh#L301-L305) —— `build.sh` 的 `main()` 一进来就判断 `RUN_TYPE == "sim"` 并执行 `ulimit -n 65535`。这是 sim 与板端在**运行准备**上的一个可观察差异：模拟器会同时打开大量进程/文件句柄。

#### 4.1.4 代码实践

**实践目标**：确认你的机器满足 CPU 路径要求，并理解脚本会自动补什么。

1. 依次执行：
   ```bash
   python3 --version
   cmake --version ; ctest --version
   g++ --version ; clang++ --version
   python3 -c "import numpy; print(numpy.__version__)"
   ```
2. 对照 [tests/run_cpu.py:177-198](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L177-L198) 的自动探测逻辑：脚本**先找 clang++（要求 ≥ 15），再找 g++（要求 ≥ 13）**，两者都不满足才报错退出。
3. 如果本机没有 cmake，先不手动安装，直接运行任意 `run_cpu.py` 命令，观察它会尝试 `pip install --user cmake`（见 [tests/run_cpu.py:97-106](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L97-L106)）；加 `--no-install` 则跳过这一步。

**需要观察的现象**：日志中出现 `Selected compiler pair: ...` 或 `cmake/ctest not found, installing via pip...`。
**预期结果**：版本满足要求时脚本能选出编译器对；不满足时会打印出明确的 `Requirements: clang++ >= 15 OR g++ >= 13` 错误信息。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么文档说 CPU 路径 Python 要 ≥ 3.11，NPU 路径却只要求 ≥ 3.9？
**答案**：两条路径的 Python 角色不同。CPU 路径的 Python 承担 `run_cpu.py` 驱动脚本本身（用了 `dict[str, tuple]` 等较新语法）和 golden 数据生成；NPU 路径的 Python 只做轻量的测试编排，真正的编译交给 bisheng/CANN 工具链，因此要求更宽松。

**练习 2**：`sim` 模式和板端 `npu` 模式都用 `run_st.py`，它们对环境要求的最大区别是什么？
**答案**：板端需要真实 NPU 硬件并安装驱动 + 固件 + CANN；sim 只需要 CANN 工具链（模拟器随 CANN 发布），不需要插卡，但仍然限定 Linux。两者编译出的内核都要经 CANN 工具链处理，这与完全自研、跨平台的 CPU 模拟器（`run_cpu.py`，无需 CANN）是本质不同的三条链路。

**练习 3**：你的机器只有 g++ 12，运行 `run_cpu.py` 会发生什么？
**答案**：自动探测会先尝试 clang++（≥ 15）再尝试 g++（≥ 13），都失败后抛出 `RuntimeError`，错误信息里列出两条可行要求（见 [tests/run_cpu.py:191-198](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L191-L198)）。也可以用 `--cxx=/path/to/newer/compiler` 显式指定一个新编译器绕过探测（[tests/run_cpu.py:223-236](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L223-L236)）。

### 4.2 run_cpu.py：CPU 路径的一键驱动

#### 4.2.1 概念说明

`tests/run_cpu.py` 是 CPU 路径的唯一入口脚本，它把「探测编译器 → CMake 配置 → 编译 → 生成 golden 数据 → 运行 gtest → 汇总」整条流水线包成了一个命令。它有两种互斥的工作模式：

- **测试模式（默认）**：构建并运行 `tests/cpu/st` 下的全部或单个 ST 用例。
- **demo 模式（`--demo`）**：只构建并运行 `demos/cpu` 下的示例程序，**不会**跑 ST 测试（argparse 帮助文本明确写了 "demo runs alone (does not run CPU ST)"，见 [tests/run_cpu.py:468-471](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L468-L471)）。

> 一个重要的学习习惯：**当文档与代码不一致时，以驱动脚本为准**。文档的 Next Steps 一节提到 `tests/cpu/demos/`，而实际 demo 目录是 `demos/cpu/`——真正的映射关系写在脚本的 `demo_map` 字典里（见下文）。

#### 4.2.2 核心流程

`main()` 的整体分支：

```text
main()
 ├── parse_arguments()          # 解析 --demo / --testcase / --gtest_filter / --clean ...
 ├── setup_environment()        #必要时自动补装 cmake/ctest
 ├── detect_compilers()         # $CXX/--cxx 优先，否则自动探测 clang++/g++
 ├── 若指定 --demo → run_demo_mode()
 │     └── 对每个 demo: build_and_run_demo()   # 擦除重建 build 目录 → cmake 配置 → 编译 → 运行
 └── 否则 → run_test_mode()
       ├── determine_need_build()   # 判断是否需要重新 cmake（TEST_CASE 缓存失配/缺二进制/--clean）
       ├── perform_build()          # cmake configure + cmake --build
       └── execute_tests()          # 对每个用例: gen_data.py 生成 golden → 运行 gtest 二进制 → 汇总表
```

#### 4.2.3 源码精读

**（1）参数总表。** 所有能力都暴露在 `parse_arguments` 里：

- [tests/run_cpu.py:439-485](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L439-L485) —— 完整参数定义。常用项整理如下：

| 参数 | 作用 |
|---|---|
| `--testcase tadd` | 只构建并运行名为 tadd 的用例 |
| `--gtest_filter 'TADDTest.*'` | 透传给 gtest 的过滤表达式 |
| `--demo gemm / flash_attn / mla / all` | 构建 demo 并单独运行 |
| `--verbose` | 打印 cmake/gtest 的完整输出（默认静默，只打结构化日志） |
| `--clean` / `--rebuild` | 删除构建目录重建 / 强制重新配置 |
| `--no-build` | 跳过编译，只运行已有二进制 |
| `--no-gen` | 跳过 gen_data.py |
| `--cxx` / `--cc` / `--generator` / `--cmake_prefix_path` | 指定编译器与 CMake 生成器（Windows 必填 generator） |
| `--enable-bf16` | 打开 BF16 覆盖，要求编译器支持 C++23 `std::bfloat16_t` |
| `--trace-mode` | 打开 CPU-SIM 指令 trace（`PTO_CPU_SIM_TRACE_MODE`） |

**（2）两种模式的总调度。**

- [tests/run_cpu.py:735-749](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L735-L749) —— `main()` 末尾按 `args.demo` 二选一分发到 `run_demo_mode` 或 `run_test_mode`。
- [tests/run_cpu.py:515-533](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L515-L533) —— demo 模式：`--demo all` 会依次跑 gemm、flash_attn、mla 三个 demo，并用 `time.perf_counter()` 计时打印 `[PASS] demo: <name> (X.XXs)`。**注意这个耗时包含 cmake 配置和编译**，不是纯运行时间。

**（3）测试模式的三段式。**

- [tests/run_cpu.py:536-557](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L536-L557) —— `run_test_mode`：源码目录固定为 `tests/cpu/st`，构建目录默认 `tests/cpu/st/build`（可用 `--build-dir` 覆盖）；流程是 `determine_need_build → perform_build → execute_tests`。
- [tests/run_cpu.py:583-617](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L583-L617) —— 智能重构建判断：读取 CMake 缓存里的 `TEST_CASE` 变量，若上次是「只建单用例」而这次要跑全量（或反之），会强制重新 configure，保证「跑全部」时真的构建了全部用例。
- [tests/run_cpu.py:683-725](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L683-L725) —— 每个用例运行前先执行该用例目录下的 `gen_data.py` 生成输入与 golden 二进制，再运行 gtest 二进制并记录 `[PASS]/[FAIL]` 与耗时。

**（4）golden 数据的生成位置。**

- [tests/run_cpu.py:271-282](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L271-L282) —— `generate_golden` 把用例的 `gen_data.py` 拷贝到构建目录里，把仓库根加进 `PYTHONPATH` 后在构建目录中执行它——所以输入/期望数据落在 `tests/cpu/st/build/` 下，与二进制同处一个目录树。

**（5）结果汇总。**

- [tests/run_cpu.py:728-732](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L728-L732) —— 最后打印 `Target | Status | Time` 的 ASCII 表格和 TOTAL 行。

#### 4.2.4 代码实践

**实践目标**：体验「增量构建」与「单用例运行」两个最常用工作流。

1. 全量跑通一次（首次会比较久，需要编译所有 ST 二进制）：
   ```bash
   python3 tests/run_cpu.py --clean --verbose
   ```
2. 再单独跑 tadd，观察是否跳过构建：
   ```bash
   python3 tests/run_cpu.py --testcase tadd
   ```
3. 只跑 tadd 里某个 gtest 用例：
   ```bash
   python3 tests/run_cpu.py --testcase tadd --gtest_filter 'TADDTest.*'
   ```
4. 查看帮助里的示例：
   ```bash
   python3 tests/run_cpu.py --help
   ```

**需要观察的现象**：第 1 步日志依次出现 `[STEP] cmake configure`、`[STEP] cmake build`、`[STEP] gen_data: tadd`、`[PASS] tadd (...)`；第 2 步因为上次已按单用例配置过/或已存在二进制，可能直接进入 TESTS 段。
**预期结果**：SUMMARY 表中 tadd 状态为 PASS。具体各用例耗时待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`--demo gemm` 会不会顺带跑 ST 测试？
**答案**：不会。`main()` 中 `args.demo` 非空就直接 `return run_demo_mode(...)`，不会落入 `run_test_mode`（[tests/run_cpu.py:746-749](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L746-L749)）。

**练习 2**：为什么先用 `--testcase tadd` 再不带参数跑全量时，脚本会重新 configure？
**答案**：`determine_need_build` 读到缓存变量 `TEST_CASE=tadd` 非空，而这次没指定 `--testcase`，判定为 config_mismatch 触发重建（[tests/run_cpu.py:590-597](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L590-L597)）——否则「跑全部」实际上只会跑到上次那一个用例。

**练习 3**：`--no-install` 影响什么？
**答案**：它让 `setup_environment` 跳过 `ensure_cmake_tools`，即不再自动 pip 补装 cmake（[tests/run_cpu.py:488-492](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L488-L492)）。注意：当前代码里自动安装只覆盖 cmake/ctest；文档提到的 numpy 自动安装并不在该函数中，numpy 仍需按 getting-started 手动装好。

### 4.3 CPU 模拟器构建：CMake、`__CPU_SIM` 与测试二进制

#### 4.3.1 概念说明

「构建 CPU 模拟器」听起来像要编译一个模拟器程序，实际上恰恰相反：**没有任何独立的模拟器可执行文件**。你要做的是把 PTO 头文件库 + 每个测试的 `main.cpp` 编译成一个普通的 gtest 程序，只是在编译时全局定义 `__CPU_SIM` 宏，使 `pto-inst.hpp` 里的每条指令选择 CPU 模拟实现。换句话说，「CPU 模拟器」是编译进每个测试二进制里的一套头文件实现。

构建的输入是 `tests/cpu/st/CMakeLists.txt`，它负责三件事：选 C++ 标准、定义 `__CPU_SIM`、解析 gtest 依赖。

#### 4.3.2 核心流程

构建产物的目录布局：

```text
tests/cpu/st/build/            # 默认构建目录（--build-dir 可改）
 ├── bin/                      # 所有 ST 可执行文件（cmake 的 RUNTIME_OUTPUT_DIRECTORY）
 │    ├── tadd
 │    ├── tmatmul
 │    └── ...
 ├── gen_data.py               # 每次运行前被当前用例的 gen_data.py 覆盖并在此执行
 ├── input*.bin / golden*.bin  # 生成的输入与期望数据（待本地验证具体文件名）
 └── CMakeCache.txt            # 记录 TEST_CASE 等配置，供下次判断是否重配
```

执行链：`cmake -S tests/cpu/st -B tests/cpu/st/build -DTEST_CASE=... → cmake --build --parallel → gen_data.py → ./bin/<testcase>`。

#### 4.3.3 源码精读

**（1）顶层 CMake 的三个关键动作。**

- [tests/cpu/st/CMakeLists.txt:14-16](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L14-L16) —— 声明三个开关：`PTO_CPU_SIM_ENABLE_BF16`（BF16 覆盖）、`PTO_CPU_SIM_TRACE_MODE`（指令 trace）、`PTO_CPU_SIM_PREFER_FETCH_GTEST`（强制源码拉取 gtest）。
- [tests/cpu/st/CMakeLists.txt:18-24](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L18-L24) —— C++ 标准选择逻辑：开了 BF16 或 GCC ≥ 14 用 C++23，否则 C++20。这正对应文档里「GCC ≥ 14 才启用 bfloat16 支持」的说明。
- [tests/cpu/st/CMakeLists.txt:32](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L32) —— `add_definitions(-D__CPU_SIM)`：**一行宏决定了整套指令走 CPU 模拟后端**，这是整个 CPU 路径的开关。

**（2）gtest 的兜底拉取。**

- [tests/cpu/st/CMakeLists.txt:95-107](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L95-L107) —— 找不到系统 gtest 时用 FetchContent 从 GitHub 拉 googletest v1.14.0 源码自行编译（这也是「可选：联网」依赖的来源）。

**（3）Release 优化。**

- [tests/cpu/st/CMakeLists.txt:151-156](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L151-L156) —— `Release`/`MinSizeRel` 构建追加 `-O2`。`run_cpu.py` 默认 `--build-type Release`，所以本讲所有计时都是在 `-O2` 下得到的。

**（4）单用例二进制如何生成。**

- [tests/cpu/st/testcase/CMakeLists.txt:11-35](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L11-L35) —— `pto_cpu_sim_st(NAME)` 函数：每个用例目录编译成同名可执行文件（`main.cpp`，若存在 `<name>_kernel.cpp` 则一并编入），头文件搜索路径指向仓库 `include/`，并链接 gtest_main 与线程库。
- [tests/cpu/st/testcase/CMakeLists.txt:39-47](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L39-L47) —— `ALL_TESTCASES` 清单的开头，`tadd` 就在第 46 行；这份清单也是 `run_cpu.py` 判断「全量二进制是否齐全」的依据（见 `parse_expected_testcases`）。

**（5）run_cpu.py 如何把这些开关传给 CMake。**

- [tests/run_cpu.py:620-652](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L620-L652) —— `perform_build` 组装 cmake 命令行：`-DTEST_CASE=<name>`、`-DPTO_CPU_SIM_ENABLE_BF16=ON/OFF`、`-DPTO_CPU_SIM_TRACE_MODE=ON/OFF`，随后 `cmake --build --parallel`。

#### 4.3.4 代码实践

**实践目标**：亲手完成一次「单用例」构建并找到产物。

1. 运行：
   ```bash
   python3 tests/run_cpu.py --testcase tadd --verbose
   ```
2. 在日志里找到 `[STEP] cmake configure` 一行，确认其中含有 `-DTEST_CASE=tadd`。
3. 构建完成后列出产物：
   ```bash
   ls tests/cpu/st/build/bin/
   ```
4. （可选）打开指令 trace 再跑一次，观察 CPU 模拟器逐条打印的指令序列：
   ```bash
   python3 tests/run_cpu.py --testcase tadd --trace-mode
   ```

**需要观察的现象**：第 3 步能看到 `tadd` 可执行文件；第 4 步日志中应出现逐指令的 trace 输出。
**预期结果**：`bin/` 下只有一个 `tadd`（因为 `-DTEST_CASE` 限定了只编这一个）；trace 模式的具体输出格式待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么说「CPU 模拟器不是被编译出来的一个程序」？
**答案**：因为 PTO 是 header-only 库，`__CPU_SIM` 只是让 `#include <pto/pto-inst.hpp>` 的内核代码在编译期实例化 CPU 模拟实现；每个 ST/demo 二进制自身就「包含」了模拟器，没有独立的模拟器可执行文件。

**练习 2**：`--enable-bf16` 为什么可能要求换编译器？
**答案**：BF16 覆盖需要 C++23 的 `std::bfloat16_t`（头文件 `<stdfloat>`），顶层 CMake 会先用一段 check_cxx_source_compiles 探测，不支持直接 `FATAL_ERROR`（[tests/cpu/st/CMakeLists.txt:18-55](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L18-L55)）；`run_cpu.py` 侧也会调用 `detect_bfloat16_cxx` 重选编译器（[tests/run_cpu.py:494-502](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L494-L502)）。

**练习 3**：gtest 二进制从 `build/bin` 目录启动，为什么？
**答案**：`run_gtest_binary` 以二进制所在目录为 cwd 启动，因为测试内部用相对路径（形如 `../<suite.case>/input1.bin`）访问构建目录里的数据文件（注释见 [tests/run_cpu.py:333-338](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L333-L338)）。手动运行二进制时也要先 `cd tests/cpu/st/build/bin`。

### 4.4 demo 运行：GEMM 与 FlashAttention

#### 4.4.1 概念说明

demo 与 ST 用例的区别：ST 用例是 gtest 程序，靠框架判定 PASS/FAIL；demo 是**带 `main()` 的普通可执行程序**，自己完成「跑内核 → 与朴素参考实现逐点比对 → 打印 perf 行 → 用退出码报告成败」。因此 demo 更适合观察性能数字与完整算子的指令流。

目前 `run_cpu.py` 支持 3 个 demo：`gemm`、`flash_attn`、`mla`（本讲聚焦前两个）。即使是静默模式，脚本也总会把以 `perf:` 开头的行打出来——这是通过 `always_print_patterns=[r"^perf:"]` 实现的（[tests/run_cpu.py:400-407](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L400-L407)），所以不加 `--verbose` 也能看到性能行。

#### 4.4.2 核心流程

`build_and_run_demo` 对每个 demo 执行四步：

```text
1. 定位源码目录 demos/cpu/<demo_name>     # demo_map 字典硬编码映射
2. 若 build/ 已存在 → 整目录删除          # 保证每次全新构建
3. cmake -S <demo> -B <demo>/build → cmake --build --parallel
4. 运行 <demo>/build/<exe>，cwd 设为 exe 所在目录
```

demo 内部的执行流（两个 demo 共用的模式）：

```text
构造输入 → 定义 GlobalTensor/Tile 类型 → TASSIGN 绑定片上地址
→ TLOAD 搬入 → 若干条计算指令 → TSTORE 写回
→ (warmup 2 次 + 正式 N 次计时) → 与朴素参考实现比对 → 打印 max_abs_diff 与 perf 行 → 退出码
```

#### 4.4.3 源码精读

**（1）demo 的注册表与「每次全新构建」。**

- [tests/run_cpu.py:348-370](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L348-L370) —— `demo_map` 把 `gemm→demos/cpu/gemm_demo`、`flash_attn→demos/cpu/flash_attention_demo`、`mla→demos/cpu/mla_attention_demo` 绑定到源码目录与可执行名；随后如果 `<demo>/build` 存在就 `shutil.rmtree` 整个删掉再重建。**产物路径固定为 `demos/cpu/<demo>/build/<exe>`**。

**（2）demo 的 CMake：如何启用 CPU 后端。**

- [demos/cpu/gemm_demo/CMakeLists.txt:23-29](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/CMakeLists.txt#L23-L29) —— 通过相对路径 `../../..` 找到仓库根，把 `include/` 加入头文件路径，并给目标定义 `__CPU_SIM __PTO_AUTO__` 两个宏。这两行就是「任意 C++ 程序接入 PTO CPU 模拟器」的最小配置模板。

**（3）GEMM demo：一个完整内核 + 计时 + 校验。**

- [demos/cpu/gemm_demo/gemm_demo.cpp:52-68](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp#L52-L68) —— 问题规模常量：M=32、K=16、N=32；阈值 `kDiffThreshold = 1e-3`；预热 2 次、正式计时 50 次。
- [demos/cpu/gemm_demo/gemm_demo.cpp:83-95](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp#L83-L95) —— 用 `GlobalTensor` 描述 GM 上的 A/B/C 三个矩阵，用 `TileLeft/TileRight/TileAcc` 描述片上的操作数与累加 tile（这些类型的细节在单元二展开，本讲只需认识「先描述数据、再发指令」的写法）。
- [demos/cpu/gemm_demo/gemm_demo.cpp:103-121](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp#L103-L121) —— 指令链本体：`TASSIGN` 把 tile 绑到模拟地址，`TLOAD` 从 GM 搬入，`TMOV` 在片上搬运，`TMATMUL` 计算并计时，最后 `TSTORE` 写回。计时只包裹 TMATMUL 循环。
- [demos/cpu/gemm_demo/gemm_demo.cpp:123-142](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp#L123-L142) —— 性能与校验输出。吞吐按每次迭代的有效浮点量计算：

  \[ \text{GFLOPS} = \frac{2 \cdot M \cdot K \cdot N}{t_{\text{avg}} \cdot 10^{9}} \]

  其中 \( t_{\text{avg}} \) 是 50 次迭代的平均秒数。随后与三重循环的 `gemm_naive` 参考实现比 `max_abs_diff`，小于 `1e-3` 才返回 0。若设置了环境变量 `PTO_CPU_PEAK_GFLOPS`，输出还会追加 `mfu=`（机器利用率 = gflops / peak）。

**（4）FlashAttention demo：更长的指令链与更严的校验。**

- [demos/cpu/flash_attention_demo/flash_attention_demo.cpp:32-41](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L32-L41) —— 规模 B=1、H=2、S=64、D=32，容差 `kRefEps = 2e-4`，预热 2 次、计时 20 次。
- [demos/cpu/flash_attention_demo/flash_attention_demo.cpp:235-256](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L235-L256) —— 完整 tile 化注意力指令链：`TLOAD` 三份输入 → `TTRANS` 转置 K → `TMATMUL` 算 QK^T → `TMULS` 缩放 → `TROWMAX / TROWEXPANDSUB / TEXP / TROWSUM / TROWEXPANDDIV` 组成在线 softmax → 再一次 `TMATMUL` 乘 V → `TSTORE`。这一段是「用十几条 PTO 指令表达一个经典算子」的直观样本。
- [demos/cpu/flash_attention_demo/flash_attention_demo.cpp:297-300](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L297-L300) —— 注意力的估算浮点量按 \[ F = 4 \cdot B \cdot H \cdot S^{2} \cdot D \] 计算（两个 matmul 各计 2·S·S·D 每头）。
- [demos/cpu/flash_attention_demo/flash_attention_demo.cpp:302-320](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L302-L320) —— 校验比 GEMM 更严：除了 `max_abs_diff`，还做 `verify_allclose`（abs_tol=rel_tol=2e-4）统计 bad_count、rmse 等，出现 NaN/Inf 或 bad_count 非零即失败。
- [demos/cpu/flash_attention_demo/flash_attention_demo.cpp:327-338](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L327-L338) —— 输出 `perf:` 行、checksum，最后显式打印 `[PASS] flash_attention_demo`（GEMM demo 没有这行，只靠退出码表达成功）。

**（5）CI 视角的总入口。** 仓库级脚本把 CPU 全流程串在一起：

- [build.sh:233-245](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/build.sh#L233-L245) —— `run_cpu_st()` 依次执行：全量 CPU ST（`--clean --verbose`）→ gemm demo → flash_attn demo → mla demo → costmodel 测试脚本。执行 `./build.sh --cpu` 即触发这条完整链路（分发逻辑见 [build.sh:301-327](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/build.sh#L301-L327)）。

#### 4.4.4 代码实践

**实践目标**：利用 `PTO_CPU_PEAK_GFLOPS` 环境变量读懂 perf 行的构成。

1. 先正常运行一次：
   ```bash
   python3 tests/run_cpu.py --demo gemm
   ```
   记下 `perf: avg_ms=... gflops=...` 这一行（静默模式也会打印）。
2. 带上限再运行：
   ```bash
   PTO_CPU_PEAK_GFLOPS=100 python3 tests/run_cpu.py --demo gemm
   ```
3. 对比两次输出：第 2 次应多出 ` peak_gflops=100 mfu=<gflops/100>` 字段（依据 [demos/cpu/gemm_demo/gemm_demo.cpp:134-140](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp#L134-L140)）。

**需要观察的现象**：perf 行在加与不加环境变量时的差异；`avg_ms` 数量级。
**预期结果**：mfu 字段出现且等于 gflops 除以 100；`max_abs_diff` 远小于 1e-3。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：脚本打印的 `[PASS] demo: gemm (3.20s)` 和 demo 自己打印的 `perf: avg_ms=0.05` 分别测的是什么？
**答案**：前者是 `run_demo_mode` 里包住 `build_and_run_demo`（含 cmake configure + 编译 + 运行）的墙钟时间（[tests/run_cpu.py:526-532](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L526-L532)）；后者是 demo 内部 50 次 TMATMUL 迭代的平均耗时（[demos/cpu/gemm_demo/gemm_demo.cpp:116-126](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp#L116-L126)）。前者主要是编译时间，不能用来比性能。

**练习 2**：为什么每次执行 `--demo` 都是全新构建？
**答案**：`build_and_run_demo` 在配置前会删除已存在的 `<demo>/build` 目录（[tests/run_cpu.py:367-370](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L367-L370)），保证产物一定对应当前源码，代价是每次都要重新编译。

**练习 3**：两个 demo 的「校验失败判定」有何不同？
**答案**：GEMM 只看 `max_abs_diff < 1e-3` 决定退出码；FlashAttention 除此（阈值 2e-4）之外还要求 `verify_allclose` 的 bad_count 为 0 且无 NaN/Inf，失败会打印 `[FAIL] ...` 到 stderr（[flash_attention_demo.cpp:313-320](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L313-L320)）。

## 5. 综合实践

**任务**：完整跑通两个 demo，产出一份可复现的记录表，并正确对比两者的运行时间。这是本讲的核心实践。

**操作步骤**：

1. 确认环境（见 4.1.4），然后在仓库根目录执行：
   ```bash
   python3 tests/run_cpu.py --demo gemm --verbose
   python3 tests/run_cpu.py --demo flash_attn --verbose
   ```
2. 对每个 demo，从 `--verbose` 日志中摘录并填入下表（模板）：

   | 记录项 | gemm | flash_attn |
   |---|---|---|
   | 源码目录 | `demos/cpu/gemm_demo` | `demos/cpu/flash_attention_demo` |
   | 构建产物路径 | `demos/cpu/gemm_demo/build/gemm_demo` | `demos/cpu/flash_attention_demo/build/flash_attention_demo` |
   | 规模行输出 | `gemm_demo: M=32 K=16 N=32` | `flash_attention_demo: B=1 H=2 S=64 D=32 (non-causal)` |
   | max_abs_diff | 待本地验证 | 待本地验证 |
   | 其他校验输出 | 无 | verify_allclose 行、checksum 行、`[PASS] flash_attention_demo` |
   | `perf: avg_ms / gflops` | 待本地验证 | 待本地验证 |
   | 脚本总耗时 `[PASS] demo: ... (X)` | 待本地验证 | 待本地验证 |

3. **时间对比（关键）**：分别记录两层时间——
   - demo 内部 `perf: avg_ms`（gemm 计 50 次 TMATMUL 迭代均值；flash_attn 计 20 次完整注意力迭代均值）；
   - 脚本级 `[PASS] demo: <name> (X.XXs)`（含编译）。
   先用 `ls -l` 比较两个可执行文件的编译产物大小，再回答：脚本级耗时的差异主要由什么造成？两个 demo 的 `gflops` 是否可以直接比较？（提示：flash_attn 的 flops 按 4·B·H·S²·D 估算，包含了 softmax 等非 matmul 开销的近似。）

**预期结果**：
- 两个 demo 均成功结束（gemm 退出码 0，flash_attn 打印 `[PASS]`）；
- `max_abs_diff` 分别远小于 1e-3 与 2e-4；
- 结论应能指出：脚本级耗时以编译为主、不可用于算子性能对比；`avg_ms`/`gflops` 才是有效指标，但两个 demo 的 gflops 口径不同只能作数量级参考。
- 具体数值均待本地验证。

**故障排查提示**（结合 getting-started）：
- 找不到编译器 → 检查 clang++ ≥ 15 / g++ ≥ 13，或用 `--cxx` 显式指定；
- gtest 拉取失败 → 检查网络，或预装 gtest 并用 `--cmake_prefix_path` 指向；
- Linux 下动态库找不到 → `export LD_LIBRARY_PATH=/path_to_compiler/lib64:$LD_LIBRARY_PATH`（[docs/getting-started.md:180-184](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md#L180-L184)）。

## 6. 本讲小结

- PTO 的运行环境分两条路径：CPU 路径（跨平台模拟器，`run_cpu.py` 驱动）与 NPU 路径（CANN 工具链，`run_st.py -r sim|npu`）；三种子模式对硬件、操作系统、Python 版本的要求各不相同。
- CPU 模拟器没有独立可执行文件——`__CPU_SIM` 宏让 header-only 的 PTO 库在每个测试/demo 二进制内实例化 CPU 模拟实现。
- `run_cpu.py` 有两种互斥模式：默认的 ST 测试模式（探测编译器 → 智能判断重构建 → gen_data 生成 golden → gtest → 汇总表）与 `--demo` 模式（擦除重建 demo 的 build 目录后编译运行）。
- demo 是自带 `main()` 与朴素参考实现的普通程序：GEMM（32×16×32，50 次迭代，阈值 1e-3）与 FlashAttention（B1·H2·S64·D32，20 次迭代，allclose 2e-4 + NaN 检查），perf 行即使在静默模式也会打印。
- 计时要分清两层：脚本的 `[PASS] demo: (X)` 含编译时间，demo 的 `perf: avg_ms` 才是有效性能指标；`PTO_CPU_PEAK_GFLOPS` 可让 demo 追加输出 mfu。
- `./build.sh --cpu` 是 CI 风格的 CPU 全量入口：全量 ST + 三个 demo + costmodel 测试。

## 7. 下一步学习建议

- 下一讲（u1-l3 仓库地图）将系统梳理 include/kernels/demos/tests 的目录职责，帮你建立「任意一条指令 → 各后端实现文件 → 测试用例」的定位能力。
- 在进入下一讲前，建议先完成本讲综合实践，并顺手浏览 `tests/cpu/st/testcase/` 下都有哪些用例目录（对照 `ALL_TESTCASES` 清单）。
- 之后 u1-l4 会带你逐行精读 `tadd` 用例的内核与 `main.cpp`——那正是本讲 4.3.4 中你编译运行过的那个二进制的源码。
