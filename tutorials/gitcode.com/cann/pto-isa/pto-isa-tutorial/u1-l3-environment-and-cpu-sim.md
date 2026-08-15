# 环境搭建与 CPU 仿真快速上手

## 1. 本讲目标

学完本讲，你应该能够：

1. 在自己的电脑（macOS / Linux / Windows 均可）上准备好 PTO 的 CPU 仿真开发环境。
2. 看懂 `tests/run_cpu.py` 这个"一键构建 + 一键运行"脚本做了什么。
3. 了解 `build.sh` 这个 NPU 侧总入口脚本的结构，以及它如何复用 CPU 路径。
4. 跑通（或至少完整理解）GEMM 与 Flash Attention 两个 CPU 仿真演示。

本讲承接 u1-l2 建立的"目录地图"：你已经知道仓库是 header-only 的、入口是 `include/pto/pto-inst.hpp`、后端由编译宏路由。本讲就回答下一个自然的问题——"我到底怎么把它跑起来？"

## 2. 前置知识

- **CPU 仿真（CPU Simulator）**：PTO 的指令在真实昇腾 NPU 上由硬件执行；但在普通电脑上，仓库提供了一套用普通 C++ 写的"仿真实现"（`include/pto/cpu/`）。编译时定义 `__CPU_SIM` 宏，同一份 kernel 代码就会被编译成在 CPU 上跑的等价程序。这让你不依赖昇腾硬件也能验证算法逻辑。
- **CMake**：C/C++ 的构建系统。本仓库所有测试和演示都用 CMake 组织，`run_cpu.py` 本质上是在帮你拼 `cmake` 命令行。
- **GoogleTest（gtest）**：C++ 单元测试框架。CPU ST（System Test）用例都是 gtest 二进制，支持 `--gtest_filter` 过滤。
- **ST 用例**：仓库对"一条指令一个可执行测试"的称呼，位于 `tests/cpu/st/testcase/` 下，每个目录（如 `tadd`）包含 `main.cpp`、`<name>_kernel.cpp`、`gen_data.py` 和 `CMakeLists.txt`。
- **CANN**：昇腾的计算软件栈。只有走 NPU 路径（真机或 NPU 模拟器）才需要安装它；CPU 路径完全不需要。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/getting-started.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/getting-started.md) | 官方上手指南，CPU 与 NPU 两条路径的环境要求和命令都在这里 |
| [tests/run_cpu.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py) | CPU 路径的一键脚本：自动找编译器、调 CMake 构建 ST 用例并运行，也能构建运行演示 |
| [build.sh](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh) | 仓库顶层总入口脚本，主要面向 NPU（打包、跑全部 ST），其中 `--cpu` 分支会转发到 `run_cpu.py` |
| [demos/cpu/gemm_demo/gemm_demo.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp) | GEMM 演示：一个完整的最小 PTO 程序（TLOAD → TMOV → TMATMUL → TSTORE） |
| [demos/cpu/gemm_demo/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt) | 演示的构建脚本，能看到 `__CPU_SIM` 宏是怎么打开的 |

## 4. 核心概念与源码讲解

### 4.1 环境准备

#### 4.1.1 概念说明

PTO 有两条运行路径，环境要求差别巨大：

| | CPU 仿真路径 | NPU 路径 |
| --- | --- | --- |
| 操作系统 | macOS / Linux / Windows | 仅 Linux |
| 硬件 | 无特殊要求 | 昇腾 910B/910C（A2/A3）等 |
| 额外软件 | 无（只需编译器 + CMake + Python） | CANN toolkit ≥ 8.5.0、驱动固件 |
| 适合人群 | 初学者、逻辑验证 | 真机验证、性能测试 |

官方文档明确建议初学者从 CPU 模拟器开始（"Most users should start with the CPU simulator"）：

[docs/getting-started.md:L7-L12](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/getting-started.md#L7-L12) —— 文档把上手分成 CPU Simulator（推荐初学者、跨平台）和 NPU Environment（进阶、需 CANN）两个场景。

#### 4.1.2 核心流程

CPU 路径的准备流程：

```text
安装 Git / Python>=3.11 / CMake>=3.16 / 支持 C++20 的编译器
        │  (Linux: GCC≥13 或 Clang≥15；macOS: AppleClang；Windows: VS2022)
        ▼
git clone https://gitcode.com/cann/pto-isa.git && cd pto-isa
        ▼
（可选）创建 venv 并安装 numpy / ml_dtypes / en_dtypes
        ▼
python3 tests/run_cpu.py --clean --verbose     # 构建并运行全部 CPU ST
```

NPU 路径的准备流程（本讲只要求了解，不需要实际操作）：

```text
Linux + GCC≥7.3 + CMake≥3.16 + Python≥3.9
        ▼
安装 GoogleTest 1.14.0 + CANN toolkit（或用 scripts/install_pto.sh 一步装齐）
        ▼
source /usr/local/Ascend/cann/bin/setenv.bash
        ▼
./build.sh --run_all --a3 --sim  或  python3 tests/script/run_st.py ...
```

#### 4.1.3 源码精读

CPU 路径的依赖清单：

[docs/getting-started.md:L20-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/getting-started.md#L20-L32) —— 列出必需工具：Git、Python ≥ 3.11、CMake ≥ 3.16、支持 C++20 的编译器（Linux 上 GCC 13+ / Clang 15+），以及 numpy 等可选 Python 包。注意这句关键设计：`tests/run_cpu.py` 能自动帮你装 numpy（除非传 `--no-install`），甚至 cmake 缺失时也会尝试 `pip install cmake`（见下文 `ensure_cmake_tools`）。

为什么需要这么新的编译器？因为 PTO 大量使用 C++20 模板特性来描述 tile 形状和指令派发，旧编译器编不过。

编译器版本不够时脚本的兜底检测：

[tests/run_cpu.py:L178-L199](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L178-L199) —— `_auto_detect_compilers` 先尝试 Clang（要求 ≥15），再尝试 GCC（要求 ≥13），都不满足则报错退出。版本号通过 `get_compiler_major_version`（解析 `--version` 输出）获得，见 [tests/run_cpu.py:L124-L154](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L124-L154)。

NPU 路径的关键差异：

[docs/getting-started.md:L199-L244](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/getting-started.md#L199-L244) —— NPU 路径要求 Linux、CANN ≥ 8.5.0，需要手动安装 GoogleTest 1.14.0，且装完 toolkit 后要 `source .../setenv.bash` 并核对 `bisheng -v` 与 `gcc -v` 版本一致（毕昇编译器是 CANN 使用的 C++ 编译器）。

#### 4.1.4 代码实践

1. **实践目标**：确认本机满足 CPU 仿真路径的全部前置条件。
2. **操作步骤**：
   ```bash
   git --version          # Git 存在
   python3 --version      # 应 ≥ 3.11
   cmake --version        # 应 ≥ 3.16
   g++ --version          # 应 ≥ 13；或 clang++ --version 应 ≥ 15
   ```
   若缺依赖，Linux 上可执行 `sudo apt-get install -y build-essential cmake ninja-build python3 python3-pip python3-venv git`（对应 [docs/getting-started.md:L56-L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/getting-started.md#L56-L61)）。
3. **需要观察的现象**：四条命令都输出版本号且满足要求。
4. **预期结果**：版本达标即可进入 4.2。若 g++ 版本低于 13，`run_cpu.py` 会在自动检测阶段直接报 "Could not find a suitable compiler"。
5. 本机输出请自行记录；不同环境输出不同，属"待本地验证"。

#### 4.1.5 小练习与答案

**练习 1**：为什么文档推荐初学者用 CPU 模拟器而不是直接上 NPU？

**答案**：CPU 模拟器跨平台（macOS/Linux/Windows）、不需要昇腾硬件和 CANN 工具链，环境门槛最低；同时 PTO 的设计保证"固定 tile 形状下行为正确"，所以先在 CPU 上验证 kernel 逻辑，再迁移到 NPU 验证性能，是官方推荐的工作流。

**练习 2**：如果你的 GCC 是 12，`run_cpu.py` 会发生什么？

**答案**：`_auto_detect_compilers` 会先试 `clang++`（≥15 可用则用 Clang），Clang 也不满足时报错 "Could not find a suitable compiler"，列出 clang++ ≥ 15 或 g++ ≥ 13 的要求后退出。也可以用 `--cxx=/path/to/compiler` 显式指定一个达标编译器绕过自动检测。

---

### 4.2 构建脚本

#### 4.2.1 概念说明

CPU 路径只有一个入口：`tests/run_cpu.py`。它是一个约 750 行的 Python 脚本，职责是：

1. 解析命令行参数（测试名、gtest 过滤、编译器、demo 名等）。
2. 自动检测/安装工具链（cmake、numpy、编译器）。
3. 调 CMake 配置并构建 `tests/cpu/st` 下的 ST 用例，或构建 `demos/cpu/` 下的演示。
4. 逐个运行生成的二进制，输出 PASS/FAIL 汇总表。

`build.sh` 则是仓库顶层总入口，主要服务于 NPU 路径（全量 ST、打包 `.run` 安装包），它的 `--cpu` 分支只是转发调用 `run_cpu.py`。

#### 4.2.2 核心流程

`run_cpu.py` 的整体调度：

```text
main()
 ├── parse_arguments()        # 解析 --testcase/--demo/--cxx/--clean 等
 ├── setup_environment()      # 补 PATH、确保 cmake/ctest、装 numpy
 ├── detect_compilers()       # 选定 CXX/CC
 ├── 若指定 --demo ──► run_demo_mode()
 │                       └── build_and_run_demo("gemm"|"flash_attn"|"mla")
 └── 否则 ────────────► run_test_mode()
                         ├── determine_need_build()   # 判断是否需要重新 cmake
                         ├── perform_build()          # cmake configure + build
                         └── execute_tests()          # gen_data.py 造数 + 跑 gtest
```

`build.sh` 的选项分发（`checkopts` 解析 → `main` 按开关调用对应函数）：

```text
--run_simple → run_simple_st()   # NPU 精简 ST
--run_all    → run_all_st()      # NPU 全量 ST
--pkg        → build_package()   # 打 .run/rpm/deb 包
--build      → build_only()      # 只构建不运行
--cpu        → run_cpu_st()      # CPU 全量：ST + 三个 demo + costmodel 测试
--comm       → run_comm_st()     # 通信指令 ST
```

#### 4.2.3 源码精读

**参数全景**：

[tests/run_cpu.py:L440-L481](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L440-L481) —— `parse_arguments` 定义了全部常用开关：`--verbose`（显示 cmake/gtest 完整输出）、`-t/--testcase`（跑单个用例如 `tadd`）、`-g/--gtest_filter`、`--cxx/--cc`、`--build-type`（默认 Release）、`--clean`（删 build 目录重建）、`--no-build`（只跑已有二进制）、`--demo`（可选 `gemm`/`flash_attn`/`mla`/`all`）等。

**工具链自愈**：

[tests/run_cpu.py:L98-L107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L98-L107) —— `ensure_cmake_tools`：若 `cmake`/`ctest` 不在 PATH，就用 `pip install --user cmake>=3.16` 自动补装，再把用户 Scripts 目录加进 PATH。这就是文档说"无需手动装 cmake 也常常能跑"的原因。

**ST 构建过程**：

[tests/run_cpu.py:L615-L646](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L615-L646) —— `perform_build` 拼出 `cmake -S tests/cpu/st -B <build_dir> -DCMAKE_BUILD_TYPE=... [-DTEST_CASE=...]` 再 `cmake --build --parallel`。指定 `--testcase tadd` 时只编一个用例（`-DTEST_CASE=tadd`），否则用 `-UTEST_CASE` 取消缓存变量以构建全部。

**运行前的"造数"步骤**：

[tests/run_cpu.py:L272-L282](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L272-L282) —— `generate_golden` 把每个用例目录里的 `gen_data.py` 拷到 build 目录执行，用它生成输入数据和期望输出（golden），gtest 二进制再读这些数据做比对。这是"测试四件套"中 gen_data 的消费方。

**build.sh 的 CPU 分支**：

[build.sh:L233-L245](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L233-L245) —— `run_cpu_st` 依次执行：全量 CPU ST（`--clean --verbose`）、三个 demo（gemm / flash_attn / mla）、以及 `tests/run_costmodel_tests.sh`（CostModel 性能模拟测试）。也就是说 `./build.sh --cpu` 一条命令可以覆盖 CPU 侧的全部验证。

[build.sh:L301-L327](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L301-L327) —— `main` 函数按 `checkopts` 设置的开关调用对应子函数；`--sim` 时还会先 `ulimit -n 65535` 提高文件描述符上限（NPU 模拟器需要）。注意各开关之间是"组合"关系，可同时触发多个子函数。

#### 4.2.4 代码实践

1. **实践目标**：不真正构建，只通过阅读掌握 `run_cpu.py` 的帮助信息与 `build.sh` 的选项。
2. **操作步骤**：
   ```bash
   python3 tests/run_cpu.py --help        # 阅读 CPU 脚本全部选项
   chmod +x build.sh && ./build.sh --help # 阅读顶层脚本选项
   ```
3. **需要观察的现象**：`--help` 输出的 Examples 一节（对应 [tests/run_cpu.py:L443-L451](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L443-L451)）列出了 `--testcase`、`--demo` 等典型用法。
4. **预期结果**：能说出"跑单个 tadd 用例"和"跑 gemm demo"分别用哪条命令。（待本地验证：不同终端下帮助文本排版可能略有差异。）

#### 4.2.5 小练习与答案

**练习 1**：`python3 tests/run_cpu.py --testcase tadd` 之后又直接运行 `python3 tests/run_cpu.py`（不带参数），脚本会重新 cmake 配置吗？

**答案**：会。`determine_need_build`（[tests/run_cpu.py:L578-L612](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L578-L612)）发现"上次配置只编了单个用例（缓存里有 TEST_CASE）而本次要跑全部"时，把 `config_mismatch` 置真强制重新配置，保证"run all"真的构建全部用例。

**练习 2**：`build.sh --cpu` 和 `python3 tests/run_cpu.py --clean --verbose` 是什么关系？

**答案**：前者是超集。`build.sh` 的 `run_cpu_st` 先执行后者（全量 CPU ST），随后额外跑三个 demo（gemm、flash_attn、mla）和 costmodel 测试脚本。

---

### 4.3 演示运行

#### 4.3.1 概念说明

`run_cpu.py --demo` 背后是 `demos/cpu/` 下的三个独立 CMake 工程：`gemm_demo`、`flash_attention_demo`、`mla_attention_demo`。它们与 ST 用例不同：不是 gtest，而是带 `main()` 的普通可执行程序，自带朴素参考实现（naive golden）做正确性比对，还会打印 `perf:` 开头的性能行。

这是初学者最重要的入口：一个 demo 就是一个"最小但完整"的 PTO 程序——声明 GlobalTensor、声明 Tile、TLOAD 搬入、计算、TSTORE 写回。

#### 4.3.2 核心流程

demo 模式的执行流程（`build_and_run_demo`）：

```text
run_demo_mode(--demo gemm)
 └── build_and_run_demo("gemm")
      ├── 定位 demos/cpu/gemm_demo
      ├── 删除并重建 demos/cpu/gemm_demo/build
      ├── cmake -S ... -B build -DCMAKE_CXX_COMPILER=<cxx>
      ├── cmake --build build --parallel
      └── 运行 build/gemm_demo，always_print_patterns=["^perf:"]
```

gemm_demo 内核的计算流程：

```text
准备 A[32,16]、B[16,32]、C[32,32]（CPU std::vector）
 ├── 定义 GlobalTensor 视图（GlobalA/GlobalB/GlobalC）
 ├── 定义 Tile 类型（TileMatA/TileMatB + LeftTile/RightTile/AccTile）
 ├── TASSIGN 绑定片上缓冲地址
 ├── TLOAD(aMat, aGlobal) / TLOAD(bMat, bGlobal)   # GM → tile
 ├── TMOV(aTile, aMat) / TMOV(bTile, bMat)          # tile → 计算用 tile
 ├── 循环 TMATMUL(cTile, aTile, bTile)              # 计时 50 次
 ├── TSTORE(cGlobal, cTile)                         # tile → GM
 └── 与 gemm_naive 参考结果比对 max_abs_diff < 1e-3
```

#### 4.3.3 源码精读

**demo 名到目录的映射**：

[tests/run_cpu.py:L349-L366](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L349-L366) —— `build_and_run_demo` 用 `demo_map` 把 `"gemm"` 映射到 `demos/cpu/gemm_demo`、`"flash_attn"` 映射到 `demos/cpu/flash_attention_demo`、`"mla"` 映射到 `demos/cpu/mla_attention_demo`，然后独立地 cmake configure + build + 运行。

**demo 的运行与性能行透出**：

[tests/run_cpu.py:L401-L407](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L401-L407) —— 非 verbose 模式下，demo 输出里只有匹配 `^perf:` 的行会被打印。所以即使不加 `--verbose`，你也能看到性能统计行。

**`__CPU_SIM` 宏在这里打开**：

[demos/cpu/gemm_demo/CMakeLists.txt:L20-L24](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt#L20-L24) —— `target_compile_definitions(gemm_demo PRIVATE __CPU_SIM __PTO_AUTO__)` 加上 `target_include_directories(... "${PTO_TILE_LIB_REPO_ROOT}/include")`。这正是 u1-l2 讲过的后端路由：定义 `__CPU_SIM` 后，包含 `pto/pto-inst.hpp` 的代码会走 CPU 仿真实现；`__PTO_AUTO__` 表示 Auto 模式（编译器自动插缓冲分配与同步，详见 u9-l1）。

**demo 的指令序列**：

[demos/cpu/gemm_demo/gemm_demo.cpp:L103-L121](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L103-L121) —— 一段浓缩了 PTO 全部核心抽象的代码：`TASSIGN` 绑定缓冲、`TLOAD` 从 GlobalTensor 搬入 tile、`TMOV` 在 tile 间搬移、`TMATMUL` 做矩阵乘、`TSTORE` 写回；中间用 `std::chrono` 对 50 次迭代计时。类型定义部分（[L83-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L83-L95)）展示了 `GlobalTensor<float, Shape<...>, Stride<...>>` 与 `Tile<TileType::Mat, ...>` 的写法，这些会在单元二展开。

**正确性自检与性能输出**：

[demos/cpu/gemm_demo/gemm_demo.cpp:L128-L142](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L128-L142) —— 用 `gemm_naive`（[L35-L46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L35-L46) 的三重循环参考实现）算出期望值，要求 `max_abs_diff < 1e-3` 才返回成功；随后打印 `max_abs_diff=` 与 `perf: avg_ms=... gflops=...`，若设置了环境变量 `PTO_CPU_PEAK_GFLOPS` 还会输出 MFU（算力利用率）。

**官方推荐的运行命令**：

[docs/getting-started.md:L120-L152](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/getting-started.md#L120-L152) —— 文档给出的常用命令：`--clean --verbose` 全量跑、`--testcase tadd` 单用例、`--gtest_filter` 单 gtest case、`--demo gemm --verbose` 和 `--demo flash_attn --verbose` 两个演示。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：亲手跑通 GEMM 与 Flash Attention 两个 CPU 仿真演示，拿到性能行并留档。
2. **操作步骤**（在仓库根目录）：
   ```bash
   # GEMM 演示
   python3 tests/run_cpu.py --demo gemm --verbose

   # Flash Attention 演示
   python3 tests/run_cpu.py --demo flash_attn --verbose
   ```
   首次运行会自动 cmake configure + build，耗时与机器相关。若想一并跑第三个演示，可加 `--demo all` 或 `--demo mla`。
3. **需要观察的现象**：
   - 日志中出现 `[STEP] demo: cmake configure`、`[STEP] demo: cmake build`、`[STEP] demo: run gemm_demo` 三步。
   - gemm 演示输出形如 `gemm_demo: M=32 K=16 N=32`、`max_abs_diff=<很小的数>`、`perf: avg_ms=... matmul_flops=... gflops=...`。
   - flash_attn 演示输出自己的规模参数与 `perf:` 行。
4. **预期结果**：两条命令均以 `[PASS] demo: ...` 结束，`max_abs_diff` 小于阈值（gemm 为 1e-3）。把终端输出保存为文本或截图留档，供后续讲义对照（例如 u5-l2 会再回到 GEMM）。
5. 本讲义写作环境未执行构建，具体数值（gflops 等）随机器不同，**待本地验证**。若报编译器找不到，用 `--cxx=` 显式指定；若链接报错，可按文档设置 `export LD_LIBRARY_PATH=/path_to_compiler/lib64:$LD_LIBRARY_PATH`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 demo 里 `TMATMUL` 要先跑 2 次 warmup 再计时 50 次？

**答案**：见 [demos/cpu/gemm_demo/gemm_demo.cpp:L113-L120](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L113-L120)。warmup 排除首次执行时缓存预热、分支预测、页表分配等一次性开销，让 `avg_ms` 更接近稳态性能。这是性能测量的通用做法。

**练习 2**：不加 `--verbose` 运行 demo，还能看到 `perf:` 行吗？

**答案**：能。`run_command` 的 `always_print_patterns=[r"^perf:"]`（[tests/run_cpu.py:L401-L407](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L401-L407)）保证即使安静模式也会透出以 `perf:` 开头的行。

**练习 3**：`--demo gemm` 会顺带跑 CPU ST 测试吗？

**答案**：不会。`--demo`（及别名 `--demo-only`）进入 `run_demo_mode` 后直接返回（[tests/run_cpu.py:L740-L743](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L740-L743)），参数帮助里也注明 "demo runs alone (does not run CPU ST)"。要跑 ST 需单独执行不带 `--demo` 的调用。

## 5. 综合实践

把本讲三个模块串起来做一次"环境体检 + 全链路冒烟"：

1. 按 4.1 的清单核对工具链版本并记录。
2. 执行 `python3 tests/run_cpu.py --clean --verbose`，构建并运行全部 CPU ST 用例，保存最后的 `== SUMMARY ==` 表格（含每个用例的 PASS/FAIL 与耗时，由 [tests/run_cpu.py:L722-L726](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L722-L726) 生成）。
3. 挑一个最快的用例（如 `tadd`）用 `--testcase tadd --gtest_filter 'TADDTest.*'` 单独重跑，对比全量运行中该用例的耗时。
4. 执行 `python3 tests/run_cpu.py --demo gemm --verbose` 与 `--demo flash_attn --verbose`，记录 `perf:` 行。
5. 写一段 5 行以内的笔记回答：ST 用例路径（`tests/cpu/st/testcase/`）和 demo 路径（`demos/cpu/`）在"构建方式、正确性校验、输出内容"三点上有什么不同？

预期结果：全量 ST 全部 PASS；能说出差异——ST 由 `tests/cpu/st` 的统一 CMake 工程构建、靠 `gen_data.py` 生成 golden 比对、输出 gtest 结果；demo 是独立 CMake 工程、自带 naive 参考实现、额外输出 `perf:` 性能行。构建耗时长短因机器而异，待本地验证。

## 6. 本讲小结

- PTO 有 CPU 仿真与 NPU 两条路径：CPU 路径跨平台、零硬件依赖，是初学者和逻辑验证的首选；NPU 路径仅限 Linux 且需要 CANN ≥ 8.5.0。
- CPU 路径的编译器门槛是 C++20：Linux 需 GCC ≥ 13 或 Clang ≥ 15，`run_cpu.py` 的 `_auto_detect_compilers` 会自动检测并在不达标时报错。
- `tests/run_cpu.py` 是 CPU 路径唯一入口：自动补装 cmake/numpy、按需增量构建 `tests/cpu/st` 的 ST 用例、运行前先用每个用例的 `gen_data.py` 造 golden，最后输出 PASS/FAIL 汇总表。
- `--demo` 走完全独立的 `demos/cpu/` CMake 工程，`gemm_demo` 用 TASSIGN→TLOAD→TMOV→TMATMUL→TSTORE 完整演示了 PTO 编程模型，并与朴素实现比对正确性、输出 `perf:` 性能行。
- 顶层 `build.sh` 是 NPU 侧总入口（`--run_all/--run_simple/--pkg` 等），其 `--cpu` 分支转发到 `run_cpu.py` 并额外跑三个 demo 与 costmodel 测试。
- demo 工程 CMake 里 `target_compile_definitions(... __CPU_SIM __PTO_AUTO__)` 是后端路由的实证：宏决定同一份代码走 CPU 仿真还是 NPU。

## 7. 下一步学习建议

下一讲（u1-l4）将逐行精读 `demos/baseline/add` 这个最小算子，把本讲"跑起来的东西"变成"读懂的代码"。在此之前建议：

- 重读 [demos/cpu/gemm_demo/gemm_demo.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp)，混个眼熟：GlobalTensor、Tile、TLOAD/TMOV/TMATMUL/TSTORE 这些名字会在后续单元逐一展开。
- 浏览 `tests/README.md` 与 `docs/coding/cpu_sim.md`（若存在），了解测试体系的整体设计（u10-l1 会深入）。
- 如果手头有昇腾环境，可以对照 [docs/getting-started.md:L329-L343](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/getting-started.md#L329-L343) 尝试用 `tests/script/run_st.py` 在 NPU/模拟器上跑同一个用例，感受两条路径的命令差异。

---

本讲义覆盖了三个最小模块：**环境准备**（CPU/NPU 双路径依赖与编译器检测）、**构建脚本**（`tests/run_cpu.py` 与 `build.sh` 的分工与流程）、**演示运行**（`--demo gemm/flash_attn` 背后的构建执行链与 gemm_demo 源码结构）。
