# 测试体系：FileCheck、unittests 与 targettests（含平台跳过标记）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 CUDA-Q 仓库里 `cudaq/test/`、`unittests/`、`targettests/`、`python/tests/` 这四类测试各自测什么、用什么框架、放在哪里。
- 用 `ctest -R` 与 `python3 -m pytest` 精确地运行单个测试，而不是每次跑全套。
- 为自己新增的代码选对测试位置：编译器改动配 FileCheck、运行时改动配 GoogleTest、端到端行为配 targettests、Python 前端配 pytest。
- 理解「平台相关跳过 / 预期失败」标记的作用，特别是 2026 年 7 月新增的 `skip_arm64_jit` 标记（覆盖 macOS arm64 与 Linux aarch64），以及它为什么用 `skip` 而不是 `xfail`。

## 2. 前置知识

- **测试金字塔的直觉**。越靠近底层的测试越快、越聚焦；越靠近顶层的测试越慢、覆盖面越广。CUDA-Q 把这条原则落成了四个物理目录。
- **FileCheck 是什么**。LLVM 生态里的一个命令行工具，读取一组 `CHECK:` 断言，按顺序在程序输出里「逐行匹配」，匹配失败就报错。它非常适合断言「编译器吐出来的文本里有没有我期望的那一行 IR / 汇编」。
- **GoogleTest（gtest）是什么**。C++ 单元测试框架，用 `TEST(Suite, Case)` 或自定义宏写断言（`EXPECT_EQ`、`EXPECT_TRUE` 等），`gtest_discover_tests` 会把每个用例注册成一个独立的 `ctest` 条目。
- **lit 是什么**。LLVM 的测试执行器（`llvm-lit`），把一个目录里的测试文件按其顶部的 `RUN:`、`CHECK:`、`REQUIRES:`、`XFAIL:` 等指令组织成测试套件，targettests 与 cudaq/test 都跑在 lit 上。
- **`xfail` 与 `skip` 的区别**。`xfail`（expected fail）= 这个测试「预期会失败」，跑了但失败算通过；`skip` = 这个测试「根本不跑」。二者看似都让 CI 变绿，但语义完全不同——后面 `test_adjoint_bug` 的例子会讲清这条区别为什么致命。

> 本讲承接 u1-l3（构建与运行 CUDA-Q）。如果你还没在 `build/` 目录里跑过 `ctest`，建议先回顾那一讲的构建步骤。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [cudaq/test/README.txt](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/README.txt) | 官方对四类测试目录分工的权威说明 |
| [cudaq/test/Translate/openqasm2_loop.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/openqasm2_loop.cpp) | 一个典型的 FileCheck 编译器测试样例 |
| [unittests/CMakeLists.txt](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/CMakeLists.txt) | 把 GoogleTest 可执行文件注册进 ctest 的地方 |
| [unittests/output_record/CMakeLists.txt](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/CMakeLists.txt) | 一个最小 gtest 子目录的写法模板 |
| [unittests/output_record/RecordParserTester.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/RecordParserTester.cpp) | 典型的运行时单元测试写法（断言 QIR 输出记录解析） |
| [targettests/lit.cfg.py](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/targettests/lit.cfg.py) | targettests 测试套件的 lit 配置，含平台 feature 注入 |
| [targettests/execution/adjoint.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/targettests/execution/adjoint.cpp) | 用 `XFAIL: *` 标记整端到端测试预期失败的例子 |
| [python/tests/conftest.py](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/conftest.py) | pytest 共享 fixture 与自定义 marker，含 `skip_arm64_jit` 的实现 |
| [python/tests/kernel/test_kernel_features.py](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/kernel/test_kernel_features.py) | `test_adjoint_bug` 用例所在文件，`xfail` + `skip_arm64_jit` 双标记的现场 |
| [Developing.md](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/Developing.md) | 贡献者文档，含「如何运行测试」一节 |

## 4. 核心概念与源码讲解

### 4.1 四类测试的分工（全局地图）

#### 4.1.1 概念说明

CUDA-Q 仓库里和「测试」相关的目录有四个，它们测的不是同一个东西，跑起来的代价也天差地别。官方在 `cudaq/test/README.txt` 里给了一段权威说明，这是判断「我的新测试该放哪」的第一依据：

> [cudaq/test/README.txt:8-43](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/README.txt#L8-L43) —— 把仓库测试分成 `test`（小而快的编译器回归测试）、`unittests`（gtest 单元测试）、`targettests`（端到端、可慢、可依赖网络）、`python/tests`（Python 前端，历史原因单独成目录）四类。

一句话概括分工：

| 目录 | 测什么 | 框架 | 速度 | 入口 |
| --- | --- | --- | --- | --- |
| `cudaq/test/` | 编译器输出（Quake/CC/QIR 文本） | lit + FileCheck | 很快 | `llvm-lit cudaq/test` 或 `ctest` |
| `unittests/` | 运行时库的 C++ 单元 | GoogleTest | 快 | `ctest` |
| `targettests/` | 源码→真实/模拟后端的端到端 | lit + `nvq++` | 慢 | `llvm-lit targettests` 或 `ctest` |
| `python/tests/` | Python 前端、`@cudaq.kernel` | pytest | 中 | `python3 -m pytest` |

#### 4.1.2 核心流程

判断一个新测试放哪里的决策树：

```text
我的改动改变了……
├─ 编译器吐出的 IR / QIR / OpenQASM 文本？  → cudaq/test/  (FileCheck)
├─ 运行时库某个函数的行为（不经过 nvq++）？   → unittests/   (gtest)
├─ 从 C++/Python 源码到一个后端的完整行为？  → targettests/ (lit + nvq++)
└─ Python 装饰器 / ast_bridge / Python API？ → python/tests/ (pytest)
```

注意三个目录（`cudaq/test`、`unittests`、`targettests`）都会被 CMake 注册进 `ctest`，所以日常本地验证最常用的还是 `cd build && ctest -R <名字>`。Python 测试则用 `pytest` 单独跑。

#### 4.1.3 源码精读

`Developing.md` 的「Testing and debugging」一节给出了官方推荐的运行命令：

> [Developing.md:210-232](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/Developing.md#L210-L232) —— 明确：「影响编译器代码的改动应配 FileCheck 测试（放 `test` 目录）；运行时库代码的改动应配 gtest 测试（放 `unittests`）」，并给出 `ctest`、`ctest -R <name>`、`python3 -m pytest -v python/tests/` 三条入口命令。

这段话是本讲后面三个模块的总纲——它把「改动类型 → 测试位置」的映射写死了。下面三个模块分别拆开讲。

#### 4.1.4 代码实践

1. **目标**：建立「四类测试」的肌肉记忆。
2. **步骤**：在仓库根目录依次 `ls cudaq/test unittests targettests python/tests`，数一数每个目录下有多少个子目录。
3. **观察**：`cudaq/test` 与 `targettests` 都按功能分子目录（如 `Transforms`、`Translate`、`execution`、`Target/IQM`），而 `unittests` 与 `python/tests` 也各自分子目录。
4. **预期结果**：你会直观感受到「编译器测试数量最多、最碎；端到端测试最少、最重」的金字塔形态。

#### 4.1.5 小练习与答案

- **练习 1**：你给 `QuakeToLLVM.cpp` 加了一条新的 lowering 规则，应该把测试放在哪四个目录中的哪一个？
- **答案**：放 `cudaq/test/`，用 FileCheck 断言新生成的 QIR 文本。因为这是「编译器输出」层面的改动，符合 `Developing.md` 的规定。
- **练习 2**：为什么 `python/tests` 不像其他三个目录那样被并进 `cudaq/test` / `unittests` / `targettests`？
- **答案**：`README.txt` 说明这是历史原因——Python 前端测试目前单独成目录，官方计划在 C++ 与 Python 前端整合后再合并。短期内 Python 相关测试仍放 `python/tests`。

---

### 4.2 FileCheck 编译器测试（cudaq/test）

#### 4.2.1 概念说明

`cudaq/test/` 下全是 lit 驱动的「回归测试」：每个测试文件就是一个带 `// RUN:` 与 `// CHECK:` 注释的源文件。lit 读到 `RUN:` 行后，把其中的 `%s` 替换成当前文件路径、把管道串起来执行，最后用 `FileCheck` 比对输出。它的特点是**极快**——只跑编译器工具链（`cudaq-quake` / `cudaq-opt` / `cudaq-translate`），不真正执行量子线路，所以适合成百上千条地堆。

#### 4.2.2 核心流程

一个 FileCheck 测试的生命周期：

```text
源文件(.cpp/.qke)
   │  顶部 // RUN: cudaq-quake %s | cudaq-opt ... | cudaq-translate ... | FileCheck %s
   ▼
lit 执行 RUN 行（%s = 本文件，%t = 临时可执行文件）
   │
   ▼
FileCheck 逐行读 // CHECK: 断言，按序在 stdout 里匹配
   │
   ▼
全部 CHECK 命中 → PASS；任一未命中 → FAIL
```

关键约定：`// CHECK:` 是「按出现顺序、逐条向下匹配」的，不是全文字符串相等。所以 `CHECK` 行的顺序必须和真实输出顺序一致。

#### 4.2.3 源码精读

以 `openqasm2_loop.cpp` 为例，这是一个把 C++ 内核编译成 OpenQASM 2.0 并断言输出的典型 FileCheck 测试：

> [cudaq/test/Translate/openqasm2_loop.cpp:9-11](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/openqasm2_loop.cpp#L9-L11) —— `RUN:` 行声明了完整的编译管道：`cudaq-quake %s | cudaq-opt --unrolling-pipeline | cudaq-translate --convert-to=openqasm2 | FileCheck %s`。注意 `// clang-format off/on` 包住 `RUN:` 是为了防止格式化工具破坏 lit 指令。

源文件里照常写 `__qpu__` 内核与 `main()`：

> [cudaq/test/Translate/openqasm2_loop.cpp:24-34](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/openqasm2_loop.cpp#L24-L34) —— 定义了 `crystal_5_kernel`，含一个 4 次迭代的受控 X 门循环和一个 Toffoli（`x<cudaq::ctrl>(q[0], q[2], q[1])`）。这是被编译的输入。

然后是断言部分：

> [cudaq/test/Translate/openqasm2_loop.cpp:41-54](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/openqasm2_loop.cpp#L41-L54) —— 一连串 `// CHECK:` 行断言生成的 OpenQASM：先有版本声明、`qelib1.inc`，再断言 `cx var0[0], var0[1]` 等门、最后 `ccx`（Toffoli）和 `measure`。这正是「循环展开成 4 个 cx、Toffoli 落到 ccx」的编译行为契约。

> **目录补充**：`cudaq/test/` 下还有大量 `.qke`（Quake Kernel Example）文件，如 `Transforms/apply_op_specialization.qke`，它们以序列化的 Quake MLIR 作为输入、只跑 `cudaq-opt` 的某个 Pass，用来回归测试单个优化 Pass 的行为。这与 `.cpp` 类测试的区别仅在于「输入是 Quake 文本而非 C++ 源码」。

#### 4.2.4 代码实践

1. **目标**：亲手跑一条 FileCheck 测试，看清 `CHECK` 如何裁定通过 / 失败。
2. **步骤**：在 `build/` 目录运行 `ctest -R openqasm2_loop`（名字可只写一段前缀，`-R` 是正则匹配）。如果想看完整管道，可直接在仓库源目录手动跑 `RUN` 行里的命令链。
3. **观察**：通过的测试几乎无输出；如果想看 `FileCheck` 的匹配过程，给管道末尾的 `FileCheck` 加上 `--match-full-lines --comment` 之类的调试开关再手动运行。
4. **预期结果**：手动篡改某个 `CHECK` 行（比如把 `cx var0[0], var0[1]` 改成 `cx var0[0], var0[9]`），重跑会得到一条 `expected string not found in input` 的失败报告，并标出实际输出里最接近的行——这就是 FileCheck 的诊断风格。

> 待本地验证：若你尚未完成 u1-l3 的构建，`cudaq-quake` / `cudaq-opt` / `cudaq-translate` 不在 `PATH` 中，上述命令会报「command not found」。请先确保构建产物已安装或在 `build/` 下用 `ctest` 调度。

#### 4.2.5 小练习与答案

- **练习 1**：如果你只想测试 `cudaq-opt` 的 `--unrolling-pipeline` 这一个 Pass，应该用 `.cpp` 还是 `.qke` 文件作输入？为什么？
- **答案**：用 `.qke`（序列化 Quake MLIR）更合适。因为只测中端 Pass 时不需要走 `cudaq-quake` 前端，直接喂 Quake 文本给 `cudaq-opt` 即可，更快也更聚焦。
- **练习 2**：`CHECK` 行写错了顺序（把后出现的门写到前面）会发生什么？
- **答案**：FileCheck 是按顺序向下扫描的，前面的 `CHECK` 已经把扫描游标推进到后面，再要求匹配一个「实际在更前面出现」的行就会找不到，报 `expected string not found`。所以 `CHECK` 顺序必须与真实输出一致。

---

### 4.3 运行时单元测试（unittests，GoogleTest）

#### 4.3.1 概念说明

`unittests/` 是 C++ 运行时库的单元测试，用 GoogleTest 写。和 FileCheck 测试最大的区别是：它**真的编译并链接进运行时库**，在主机上直接调用 `cudaq::sample`、`RecordLogParser` 等真实 API，不走 `nvq++` / `cudaq-quake`。`unittests/CMakeLists.txt` 顶部有一段重要注释点明了这种「库模式」测试的本质：

> [unittests/CMakeLists.txt:19-26](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/CMakeLists.txt#L19-L26) —— 这些测试可执行文件直接实例化 `__qpu__` 内核仿函数结构体并从 C++ 测试代码里调用 `cudaq::sample(myKernel)`，**没有 nvq++/cudaq-quake 步骤，AST bridge 根本不运行**，门与测量调用直接落到 `runtime/cudaq/qis/qubit_qis.h` 的内联函数体。为此该目录强制定义了宏 `CUDAQ_LIBRARY_MODE`。

这条注释非常关键：它解释了为什么 `unittests` 里的 `__qpu__` 内核能被「当普通 C++ 跑」——因为不走编译器前端，门调用走的是头文件里的库实现路径（参见 u1-l4 关于 ExecutionManager 记录门指令的讲解）。

#### 4.3.2 核心流程

一个 gtest 测试从源码到 ctest 的链路：

```text
写 Tester.cpp（CUDAQ_TEST(Suite, Case){...} 或 TEST(Suite,Case){...}）
   │
   ▼ CMake
add_executable(test_xxx main.cpp Tester.cpp ...) + target_link_libraries(... cudaq ... gtest_main)
   │
   ▼ gtest_discover_tests(test_xxx)
把每个 TEST 用例注册成独立的 ctest 条目（含 LABELS / RESOURCE_LOCK / PROCESSORS 等属性）
   │
   ▼ ctest -R test_xxx
调度执行
```

`gtest_discover_tests` 是连接「gtest 可执行文件」与「ctest」的桥梁：它在构建期运行一次测试程序，枚举出所有用例名，再把这些名字注册成 ctest 条目，于是你可以用 `ctest -R <用例名>` 精确到单个用例。

#### 4.3.3 源码精读

先看一个「最小 gtest 子目录」是怎么注册的——`output_record` 子目录（测试 QIR 输出记录解析器 `RecordLogParser`，正是 u4-l7 讲的那个）：

> [unittests/output_record/CMakeLists.txt:9-23](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/CMakeLists.txt#L9-L23) —— 三步走：`add_executable(test_record RecordParserTester.cpp)` 建可执行文件；`target_link_libraries` 链接 `cudaq`、`cudaq-common`、`gtest_main` 与默认平台；`gtest_discover_tests(test_record ...)` 注册进 ctest。这是给新运行时模块加测试时最值得照抄的模板。

再看 `RecordParserTester.cpp` 里一个真实用例，体会断言风格：

> [unittests/output_record/RecordParserTester.cpp:21-37](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/RecordParserTester.cpp#L21-L37) —— `CUDAQ_TEST(ParserTester, checkSingleBoolean)` 构造一条 `OUTPUT\tBOOL\ttrue\ti1\n` 文本，喂给 `cudaq::RecordLogParser`，再把解析出的缓冲区 `memcpy` 成 `bool` 并 `EXPECT_EQ(true, value)`。这正好对应 u4-l7 讲的「设备→宿主」输出记录文本格式与解析路径，是典型的「给一个输入、断言一个输出」的单元测试。

`unittests/CMakeLists.txt` 里还能看到更复杂的注册方式——给耗资源测试加标签与锁：

> [unittests/CMakeLists.txt:195](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/CMakeLists.txt#L195) —— `gtest_discover_tests(test_dynamics PROPERTIES LABELS "gpu_required" RESOURCE_LOCK "gpu")`：动力学测试需要 GPU，故打 `gpu_required` 标签并用 `RESOURCE_LOCK "gpu"` 串行化，避免多个 GPU 测试抢卡。

> [unittests/CMakeLists.txt:210](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/CMakeLists.txt#L210) —— `gtest_discover_tests(test_utils PROPERTIES PROCESSORS ${CUDAQ_TEST_OMP_SLOTS} ...)`：用 `PROCESSORS` 告诉 ctest 这个 OpenMP 并行测试占几个 CPU 槽位，配 `CUDAQ_TEST_OMP_SLOTS`（默认 1，CI 设 2）使用，防止 `ctest -j N` 同时拉起 N 个各自开多线程的测试把机器打爆。

> **目录补充**：`unittests/` 下还有 `operators`、`spin_op`、`dynamics`、`nvqpp`、`qir`、`target_config`、`logger`、`device_call` 等子目录，分别对应运行时的各个子系统；`add_subdirectory(output_record)` 这类语句把它们逐个挂进构建（见 [unittests/CMakeLists.txt:243](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/CMakeLists.txt#L243)）。

#### 4.3.4 代码实践

1. **目标**：跑一个运行时单元测试，并读懂它的断言。
2. **步骤**：在 `build/` 目录运行 `ctest -R test_record`（对应 `output_record` 子目录的 `test_record` 可执行文件）。然后用 `ctest --output-on-failure -R test_record` 让失败时打印完整输出。
3. **观察**：每个 `CUDAQ_TEST(ParserTester, ...)` 都是一个独立用例；用 `ctest -R checkSingleBoolean` 甚至能精确到单个用例名（`gtest_discover_tests` 会把 `Suite.Case` 也注册成可匹配名）。
4. **预期结果**：全部通过。如果你想看它「失败时长什么样」，临时把 `RecordParserTester.cpp` 里的 `EXPECT_EQ(true, value)` 改成 `EXPECT_EQ(false, value)`，重新 `cmake --build` 后再跑，会看到 gprint 出的期望值 / 实际值对照。
5. **若无法确定运行结果**：标注「待本地验证」——这取决于你本机的构建是否启用了 `CUDAQ_BUILD_TESTS`（u1-l3 讲过，默认 TRUE）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `unittests` 里的内核可以直接被 `cudaq::sample` 调用，而不需要 `nvq++`？
- **答案**：因为 `unittests/CMakeLists.txt` 定义了 `CUDAQ_LIBRARY_MODE`，测试不走 AST bridge，门调用直接落到 `qubit_qis.h` 的内联库实现，由 ExecutionManager 记录并由后端解释（参见 u1-l4）。
- **练习 2**：`gtest_discover_tests` 的 `RESOURCE_LOCK "gpu"` 解决了什么问题？
- **答案**：它让所有带该锁的测试串行执行，避免多个 GPU 测试同时争抢同一块卡导致 OOM 或超时失败。

---

### 4.4 targettests：跨目标端到端测试

#### 4.4.1 概念说明

`targettests/` 是最「重」的一类测试：它走完整链路——`nvq++` 把 `.cpp` 编译成可执行文件、再实际跑起来、对真实或模拟后端采样，最后用 FileCheck 或 shell 断言输出。`README.txt` 明确这类测试「可能很慢、可能依赖网络」（比如远程 IQM/IonQ 后端）。它的 lit 配置在 `targettests/lit.cfg.py`。

#### 4.4.2 核心流程

targettests 也是 lit + FileCheck 体系，但 `RUN:` 行里是 `nvq++ %s -o %t && %t`（编译并执行），而不是只跑编译器工具链：

```text
.cpp（顶部 RUN: nvq++ --target <T> [--emulate] %s -o %t && %t [| FileCheck %s]）
   │
   ▼ lit
nvq++ 编译 → 临时可执行 %t → 执行 %t（可能采样）→（可选）FileCheck 断言 stdout
   │
   ▼
PASS / FAIL / XFAIL / UNSUPPORTED
```

lit 还支持四条指令控制「在什么环境下跑」：

- `RUN:` —— 执行命令。
- `CHECK:` —— FileCheck 断言（配合 `RUN` 末尾的 `| FileCheck %s`）。
- `REQUIRES:` —— 仅当列出的 feature 全部可用时才跑（如 `REQUIRES: c++17`）。
- `XFAIL:` —— 预期失败；命中则记 XFAIL（算通过），不命中（居然通过了）记 XPASS。
- `UNSUPPORTED:` —— 在列出平台上完全不跑。

#### 4.4.3 源码精读

`targettests/lit.cfg.py` 定义了这个套件如何识别平台、如何替换路径变量：

> [targettests/lit.cfg.py:24-27](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/targettests/lit.cfg.py#L24-L27) —— `config.test_format = lit.formats.ShTest(...)`（shell 测试格式），`config.suffixes = ['.cpp', '.config']`（只有这两种后缀的文件被当作测试）。

平台 feature 注入是本模块的重点——它让 `XFAIL: darwin-arm64`、`REQUIRES: ...` 这类指令能生效：

> [targettests/lit.cfg.py:58-60](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/targettests/lit.cfg.py#L58-L60) —— 当 `platform.system() == 'Darwin'` 且 `platform.machine() == 'arm64'` 时，向 `config.available_features` 注册一个名为 `darwin-arm64` 的 feature。于是测试文件里写 `// XFAIL: darwin-arm64` 就只会在 macOS ARM64 上「预期失败」。

来看两个真实用法。第一个是「无条件预期失败」——已知 bug 还没修：

> [targettests/execution/adjoint.cpp:9-13](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/targettests/execution/adjoint.cpp#L9-L13) —— `// XFAIL: *`（星号 = 所有平台预期失败），下面注释解释：`ApplyOpSpecialization` 还不能处理多参数循环里的 `cudaq.adjoint`，关联 issue #3818，修好后再去掉 `XFAIL`。注意这与 4.5 节 Python 端的 `test_adjoint_bug` 是**同一个根因**——一个用 C++ targettest 的 `XFAIL:*` 表达，一个用 Python pytest 的 `xfail` 表达。

第二个是「仅在 macOS ARM64 上预期失败」：

> [targettests/execution/estimate_resources_sample_in_choice.cpp:10-13](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/targettests/execution/estimate_resources_sample_in_choice.cpp#L10-L13) —— `// XFAIL: darwin-arm64`，注释说明这是 macOS ARM64 上的已知 LLVM bug。这正是上面 lit.cfg.py 注册的 `darwin-arm64` feature 被消费的地方。

> **关键对比**：C++ targettest 这边用 lit 的 `XFAIL: darwin-arm64` 表达「在 ARM64 上预期失败」；而 Python 端的同类问题（见 4.5 节）却必须用 `skip` 而不能用 `xfail`——这是下一节的核心。

#### 4.4.4 代码实践

1. **目标**：跑一个 targettest，看清端到端链路。
2. **步骤**：在 `build/` 目录 `ctest -R adjoint`（注意它会显示为 XFAIL）。或在源码目录用 `llvm-lit targettests/execution/adjoint.cpp` 单独跑。
3. **观察**：终端会标注该测试为 `XFAIL`（预期失败，CI 算通过）。这说明 lit 把「预期失败」和「真正失败」区分开了。
4. **预期结果**：`adjoint.cpp` 当前在所有平台都 XFAIL；`estimate_resources_sample_in_choice.cpp` 只在 macOS ARM64 上 XFAIL，其它平台正常通过。
5. 待本地验证：targettests 慢且依赖完整 `nvq++` 工具链，未构建好时会直接报错。

#### 4.4.5 小练习与答案

- **练习 1**：`XFAIL: *` 和「直接把这个测试文件删掉」有什么区别？
- **答案**：删掉就彻底不再验证该场景，将来 bug 修好了也没人知道；`XFAIL: *` 则让测试**继续跑**，一旦它居然通过了（bug 被意外修复），lit 会报 `XPASS` 提醒你「该把 XFAIL 去掉了」。这是一种「负向回归保护」。
- **练习 2**：`targettests/lit.cfg.py` 里注册 `darwin-arm64` feature 的判断条件，和 Python `conftest.py` 里判断 ARM64 的条件，完全一样吗？
- **答案**：不一样。lit 这边只认 `Darwin + arm64`（macOS）；Python `conftest.py` 的 `skip_arm64_jit` 还要覆盖 Linux 的 `aarch64`（见 4.5 节）。这正是 2026 年 7 月那处修复要解决的缺口。

---

### 4.5 pytest 平台跳过标记（skip_macos_arm64_jit 与新增的 skip_arm64_jit）

> **本节是本次更新的重点。** 2026 年 7 月的提交 `9a96717378`（PR #4790）在 `python/tests/conftest.py` 新增了 `skip_arm64_jit` 标记，并把 `test_adjoint_bug` 从 `skip_macos_arm64_jit` 换成 `skip_arm64_jit`，原因是旧标记只覆盖 macOS ARM64、漏掉了 Linux aarch64，导致该用例在 Linux ARM64 CI 上直接崩掉 pytest worker。

#### 4.5.1 概念说明

Python 前端测试用 pytest。平台相关的「跳过」由 `python/tests/conftest.py` 统一管理：它注册自定义 marker，并在 `pytest_collection_modifyitems` 钩子里、在用例被收集后但执行前，根据当前平台给特定用例动态追加 `pytest.mark.skip`。

这里必须先讲清一个底层根因——**为什么 ARM64 上需要特殊处理**。commit 信息说得很明白：在 ARM64 上，C++ 异常穿过 LLVM JIT 编译出来的栈帧时不会正常传播回 Python，而是触发 `std::terminate()` 直接杀掉整个 pytest worker 进程（上游 bug `llvm-project#49036`）。这带来的后果是：一个本应被 `xfail` 捕获的 `RuntimeError`，在 ARM64 上根本到不了 `xfail` 的手——进程已经死了。

#### 4.5.2 核心流程

`xfail` 在 ARM64 上为什么失效、`skip` 为什么有效：

```text
正常平台（x86-64）：
   用例抛 RuntimeError → pytest 捕获 → xfail 命中 → 标记 XPASS/XFAIL → CI 绿 ✅

ARM64 平台（旧，用 xfail）：
   用例抛 RuntimeError → 异常穿 LLVM JIT 帧 → std::terminate() → worker 进程死
   → pytest 根本没机会处理 → 整个 worker 崩溃 ❌（不是失败，是消失）

ARM64 平台（新，用 skip_arm64_jit）：
   收集阶段即给用例加 skip → 用例根本不执行 → 没有 terminate → CI 绿 ✅
```

所以 `skip` 与 `xfail` 的区别在本场景下是**致命的**：`xfail` 仍要求「执行用例并捕获其异常」，而执行本身就足以杀死进程；只有 `skip`（不执行）才能躲开这个上游 bug。

#### 4.5.3 源码精读

先看 marker 的注册：

> [python/tests/conftest.py:22-33](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/conftest.py#L22-L33) —— `pytest_configure` 注册两个 marker：旧的 `skip_macos_arm64_jit`（仅 macOS ARM64）和新增的 `skip_arm64_jit`（任意 ARM64：macOS 的 `arm64` 或 Linux 的 `aarch64`）。注意注册时只声明 marker 文档，真正「跳过」的动作在下一个钩子里。

再看实际施加 skip 的逻辑——本次修复的核心：

> [python/tests/conftest.py:36-60](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/conftest.py#L36-L60) —— `pytest_collection_modifyitems` 先算出 `is_arm64 = platform.machine() in ('arm64', 'aarch64')` 与 `is_darwin`。然后两条规则并行：旧 `skip_macos_arm64_jit` 仍只在 `is_darwin and is_arm64` 时生效（行为不变）；新 `skip_arm64_jit` 在**任意** `is_arm64`（含 Linux aarch64）时生效。关键是它遍历每个 item、按 marker 名精确添加 `pytest.mark.skip`。

修复的关键就是这一行判断——把「macOS only」放宽到「任意 ARM64」：

> [python/tests/conftest.py:40](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/conftest.py#L40) —— `is_arm64 = platform.machine() in ('arm64', 'aarch64')`。`platform.machine()` 在 macOS ARM64 返回 `'arm64'`，在 Linux ARM64 返回 `'aarch64'`，两个字符串都进集合，于是 Linux aarch64 也被识别为 ARM64。旧代码只查 `sys.platform == 'darwin' and platform.machine() == 'arm64'`，恰好漏掉 Linux。

> [python/tests/conftest.py:53-60](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/conftest.py#L53-L60) —— 新规则块：只要 `is_arm64` 且该用例带 `skip_arm64_jit` marker，就追加 `pytest.mark.skip(reason="JIT exception handling broken on ARM64 (llvm-project#49036)")`。

最后看消费端——`test_adjoint_bug` 用例本身，它同时挂着 `xfail` 与 `skip_arm64_jit`：

> [python/tests/kernel/test_kernel_features.py:2925-2929](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/kernel/test_kernel_features.py#L2925-L2929) —— 顶部 TODO 注释关联 issue #3818（`ApplyOpSpecialization` 尚不支持多参数循环的 `cudaq.adjoint`）；`@pytest.mark.xfail(raises=RuntimeError)` 表示「在能正常抛异常的平台上，预期抛 RuntimeError」；紧随其后的 `@pytest.mark.skip_arm64_jit`（本次由 `skip_macos_arm64_jit` 改来）表示「在 ARM64 上别跑」。

用例体里正是那个会触发问题的 `cudaq.adjoint` 调用：

> [python/tests/kernel/test_kernel_features.py:2944-2954](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/kernel/test_kernel_features.py#L2944-L2954) —— 内核 `kernel(withAdj)` 调 `cudaq.kernels.uccsd(...)` 后，当 `withAdj=True` 再调 `cudaq.adjoint(cudaq.kernels.uccsd, ...)`；正是这条 adjoint 路径在 ARM64 上抛出的 RuntimeError 穿过 JIT 帧导致 terminate。

> **修复范围说明（来自 commit）**：本次只把 `test_adjoint_bug` 这一个用例从 `skip_macos_arm64_jit` 换成 `skip_arm64_jit`，其余所有 `skip_macos_arm64_jit` 用例（如 `test_trap_fail`、`test_stim.py`、`test_IonQ.py` 等）保持不变——因为它们在 Linux aarch64 上并未出现 worker 崩溃，只受 macOS ARM64 限制。所以仓库里两个 marker 并存：`skip_macos_arm64_jit`（仅 macOS）与 `skip_arm64_jit`（任意 ARM64），按用例的实际崩溃范围选用。

#### 4.5.4 代码实践

1. **目标**：找到 `skip_arm64_jit` 用例，理解它为何「跳过」而非「预期失败」。
2. **步骤**：
   - 在仓库根目录运行 `grep -rn "skip_arm64_jit" python/tests/`，确认目前只有 `test_adjoint_bug` 一个用例用它（其余用 `skip_macos_arm64_jit`）。
   - 运行 `python3 -m pytest -v python/tests/kernel/test_kernel_features.py -k test_adjoint_bug`（建议加 `-rxs` 让 skip 原因也打印出来）。
3. **观察**：
   - 在 x86-64 Linux 上：用例实际执行并抛 `RuntimeError`，pytest 标记 `XFAIL`，附 reason「ApplyOpSpecialization ...」。
   - 在 ARM64（macOS arm64 或 Linux aarch64）上：用例在收集阶段被 `conftest.py` 追加 skip，根本不执行，标记 `SKIPPED`，附 reason「JIT exception handling broken on ARM64 (llvm-project#49036)」。
4. **预期结果**：两种平台 CI 都变绿，但绿色来自不同机制——x86 走 `xfail`，ARM64 走 `skip`。这正是 commit 里那张结果表的含义（x86 XFAIL ✅ / macOS ARM64 SKIP ✅ / Linux aarch64 修复前 CRASH ❌、修复后 SKIP ✅）。
5. 待本地验证：skip/xfail 的具体标签取决于你的 `platform.machine()`；若你不在 ARM64 机器上，只能观察到 `XFAIL` 分支。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `test_adjoint_bug` 在 ARM64 上不能用 `xfail`，必须用 `skip`？
- **答案**：因为 ARM64 上 C++ 异常穿过 LLVM JIT 帧会触发 `std::terminate()` 杀死 pytest worker 进程（`llvm-project#49036`），异常根本到不了 pytest 的 `xfail` 捕获逻辑。`xfail` 仍需「执行用例」，而执行本身就崩了；只有 `skip`（不执行）能规避。
- **练习 2**：新标记为什么叫 `skip_arm64_jit` 而不是直接把旧的 `skip_macos_arm64_jit` 改成覆盖 Linux？
- **答案**：因为其它挂 `skip_macos_arm64_jit` 的用例（如 `test_trap_fail`、`test_stim`、`test_IonQ`）在 Linux aarch64 上并没有崩溃，只受 macOS ARM64 限制。直接放宽旧标记会把这些用例在 Linux aarch64 上也误跳过、缩小覆盖面。新增独立 marker 让「崩溃范围」与「跳过范围」精确对齐，只动真正崩溃的那一个用例。
- **练习 3**：如果你新增的 Python 用例在 Linux aarch64 上也出现了 worker 崩溃，该怎么标？
- **答案**：先确认根因是否同样是 JIT 异常处理（`llvm-project#49036`）；若是，给该用例加 `@pytest.mark.skip_arm64_jit`，无需改 `conftest.py`（钩子已就位）。

---

## 5. 综合实践

把本讲四类测试串起来，完成一次「为新改动选测试位置并加平台保护」的演练：

**场景**：假设你修了 `RecordLogParser` 对 `inf`/`nan` 浮点的解析（这正是 u4-l7 提到的 PR #4832 加固方向），并发现该修复在 Linux aarch64 的 JIT 路径上有一个边界用例会抛 C++ 异常。

请按以下步骤设计测试方案（先在草稿上写，再对照源码核对）：

1. **运行时单元测试**：在 `unittests/output_record/RecordParserTester.cpp` 里加一个 `CUDAQ_TEST(ParserTester, checkInfNan)`，构造含 `OUTPUT\tDOUBLE\tinf\tf64\n` 与 `nan` 的输入，断言解析出的缓冲区经 `memcpy` 后是 `std::numeric_limits<double>::infinity()` / NaN。说明：这是「运行时库行为」改动，放 `unittests` 符合 `Developing.md` 的规定（4.1.3）。
2. **编译器 / 端到端覆盖（可选）**：若修复也改了 CodeGen 生成的记录格式文本，则在 `cudaq/test/` 加一条 FileCheck 断言生成文本（4.2），或在 `targettests/` 加一条 `nvq++ %s && %t` 端到端用例（4.4）。
3. **平台保护**：判断那个 aarch64 上的异常用例是否会杀死 pytest worker。若会（异常穿 JIT 帧），给对应 pytest 用例加 `@pytest.mark.skip_arm64_jit`（4.5.3），并写清 reason 引用 `llvm-project#49036`；若只是普通断言失败、异常能正常传播，则用 `@pytest.mark.xfail` 并关联 issue。
4. **本地验证**：`ctest -R test_record` 验证 gtest；`python3 -m pytest -v -rxs python/tests/...` 验证 pytest 的 skip/xfail 标签。

> 这个练习的关键不是写出能跑的代码，而是训练「改动类型 → 测试目录」「崩溃机制 → skip vs xfail」这两条判断链——它们是 CUDA-Q 贡献者日常 PR review 里最常被追问的两点。

## 6. 本讲小结

- CUDA-Q 有四类测试，分工明确：`cudaq/test/`（FileCheck 编译器回归）、`unittests/`（gtest 运行时单元）、`targettests/`（lit+nvq++ 端到端）、`python/tests/`（pytest 前端）——`Developing.md` 与 `cudaq/test/README.txt` 是权威依据。
- FileCheck 测试靠文件顶部的 `// RUN:` 与 `// CHECK:` 工作，按序匹配、极快，适合堆量测编译器输出；`gtest_discover_tests` 把每个 gtest 用例注册成独立 ctest 条目，可用 `ctest -R` 精确运行。
- targettests 走完整 `nvq++` 链路，lit 用 `REQUIRES` / `XFAIL` / `UNSUPPORTED` 控制环境；`targettests/lit.cfg.py` 通过注册 `darwin-arm64` feature 让 `XFAIL: darwin-arm64` 生效。
- **平台跳过的关键认知**：`skip`（不执行）与 `xfail`（执行并预期失败）语义不同。ARM64 上 C++ 异常穿 LLVM JIT 帧会 `std::terminate` 杀死 pytest worker（`llvm-project#49036`），使 `xfail` 失效；必须用 `skip` 规避。
- **本次更新**：2026 年 7 月 PR #4790 新增 `skip_arm64_jit` 标记（`conftest.py` 用 `platform.machine() in ('arm64','aarch64')` 同时覆盖 macOS 与 Linux），并把 `test_adjoint_bug` 从仅 macOS 的 `skip_macos_arm64_jit` 换过来，修复了 Linux aarch64 CI 上的 worker 崩溃；其余用例维持原标记不动。

## 7. 下一步学习建议

- **接 u8-l2（调试与日志）**：本讲只讲「测试怎么跑」，下一讲讲「测试失败后怎么定位」。`CUDAQ_LOG_LEVEL` / `CUDAQ_LOG_FILE` 与 `cudaq-opt` 逐步检查中间 IR 是排查编译器测试失败的核心手段。
- **接 u8-l3（架构取舍）**：理解四类测试的分层后，可以从「测试边界」反推 CUDA-Q 的子系统边界（编译器 / 运行时 / 前端 / 后端），这正是架构总结篇的切入点。
- **延伸阅读源码**：
  - 想写新 gtest：照抄 [unittests/output_record/CMakeLists.txt](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/CMakeLists.txt) 这个最小模板。
  - 想写新 FileCheck：参考 [cudaq/test/Translate/openqasm2_loop.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/openqasm2_loop.cpp) 的 `RUN`/`CHECK` 结构。
  - 想加平台保护：通读 [python/tests/conftest.py](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/tests/conftest.py) 两个钩子，按崩溃范围在 `skip_macos_arm64_jit` 与 `skip_arm64_jit` 之间二选一。
