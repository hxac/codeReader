# C++ 系统测试：以 CPU 黄金参考校验 GPU 算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `tests/cvcuda/system` 目录下「参数化系统测试」的完整套路：**随机数据 → CPU 黄金参考 → GPU 算子 → 逐字节比对**。
2. 理解 `NVCV_TEST_SUITE_P` 宏、`ValueList` 值表、MD5 参数后缀这套自制参数化框架，以及它为什么不直接用 googletest 原生方案。
3. 掌握 `run_tests.sh` 的生成机制与「标签过滤」语法，能精确运行 `cvcuda,cpp` 里的单个算子用例。
4. 区分四类测试（Tensor 正例、变长批正例、planar 奇偶校验、负例）各自守护的正确性面。
5. 亲手为一个算子补充一个未覆盖的 dtype 组合——包括在必要时同步扩展 CPU 黄金参考。

## 2. 前置知识

- **系统测试（system test）与单元测试的区别**：本仓库 `tests/cvcuda` 下有两个子目录，`system/` 通过**公开 API**（`cvcuda::Flip` 等 C++ 类）驱动算子做端到端校验；`unit/` 则直接测试算子内部工具。本讲聚焦前者。
- **黄金参考（gold reference）**：一份与 GPU 实现完全独立的 CPU 参考实现。GPU 算子改得再快，只要语义对，输出就必须与 CPU 参考逐字节一致。这是 CV-CUDA「敢重构内核」的底气。
- **参数化测试（value-parameterized test）**：同一份测试逻辑，套在不同参数组合上各跑一遍。googletest 原生提供 `TEST_P` + `INSTANTIATE_TEST_SUITE_P`，本仓库在其上又包了一层 DSL（领域专用语言），让参数表读起来像一张契约表。
- **NVCV 状态码与 `nvcv::ProtectCall`**：C++ 侧算子失败抛 `nvcv::Exception`；`ProtectCall` 把 lambda 中抛出的异常翻译成 `NVCVStatus` 返回码（详见 u6-l2）。负例测试正是靠它断言「必须报 `NVCV_ERROR_INVALID_ARGUMENT`」。
- **Limitations 契约表**：每个算子公开 C 头文件里写明支持的布局/通道/dtype。测试参数表应当是这张表的**子集且尽量逼近全集**——本讲的实践就要沿这条纪律走。
- **前置讲义**：u5-l2 讲过的 `exportData` / `TensorDataAccess` 在本讲会以测试视角再次出现；u6-l1 的 C++ 类调用方式（`cvcuda::Flip flipOp; flipOp(stream, in, out, flipCode);`）是测试代码的直接调用对象。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tests/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md) | 测试套件总入口：怎么编译、怎么装依赖、怎么运行 |
| [tests/run_tests.sh.in](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/run_tests.sh.in) | 测试驱动脚本**模板**，构建期被 CMake 展开成 `run_tests.sh` |
| [tests/CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt) | 定义 `nvcv_add_test` 宏：注册 ctest + 向驱动脚本追加 `run` 行 |
| [tests/cvcuda/system/CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/CMakeLists.txt) | 登记全部 `TestOp*.cpp`，产出 `cvcuda_test_system` 可执行文件并打标签 |
| [tests/cvcuda/system/TestOpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp) | **本讲主角**：Flip 的四类参数化测试 |
| [tests/cvcuda/system/FlipUtils.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp) / [FlipUtils.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.hpp) | Flip 的 CPU 黄金参考 `FlipCPU` |
| [tests/common/ValueTests.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/ValueTests.hpp) | `NVCV_TEST_SUITE_P` 宏与 MD5 测试名后缀生成器 |
| [tests/common/ValueList.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/ValueList.hpp) | 值表 DSL：组合、去重排序、类型归一 |
| [tests/Main.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/Main.cpp) | 所有 C++ 测试二进制共用的 `main`：全局事件监听器 |
| [tests/cvcuda/system/TestAPI.cpp.in](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestAPI.cpp.in) | 头文件兼容性测试**模板**（仅一行 `@ALL_HEADERS@`） |
| [cmake/ConfigCompiler.cmake](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigCompiler.cmake) | `add_header_compat_test`：把模板展开成「包含所有公共头」的编译测试 |
| [src/cvcuda/include/cvcuda/OpFlip.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h) | Flip 的 Limitations 契约表（测试参数表的依据） |
| [src/cvcuda/priv/legacy/flip.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu) | 被测的 GPU 内核及其 dtype×通道分派表 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：①测试怎么跑起来（驱动脚本机制）；②参数化框架；③CPU 黄金参考；④四类测试用例精读；⑤TestAPI 模板生成。

### 4.1 模块一：run_tests.sh 是怎么来的

#### 4.1.1 概念说明

仓库里**找不到** `tests/run_tests.sh` 这个文件——它由 `tests/run_tests.sh.in` 模板在构建期生成，落在 `build-rel/bin/run_tests.sh`。生成的脚本能做两件事：

1. 按调用者给的**标签集合**决定运行/跳过哪些测试二进制；
2. 用 `NVCV_LEAK_DETECTION=abort` 环境变量执行每个二进制，让句柄泄漏直接让测试失败。

#### 4.1.2 核心流程

```text
bash build.sh -DBUILD_TESTS=1
        │
        ├─ CMake: configure_file(run_tests.sh.in → bin/run_tests.sh, @ONLY)   # 生成骨架（run 函数定义）
        ├─ 每个 nvcv_add_test(cvcuda_test_system cvcuda cpp)
        │        └─ file(APPEND run_tests.sh "run cvcuda_test_system cvcuda cpp\n")
        ▼
build-rel/bin/run_tests.sh [过滤标签,...]
        │  把 $1 按逗号切成 test_set 数组（默认 "all"）
        ▼
对每个 run 行：每个过滤器都必须命中该二进制的至少一个标签
        ├─ 全命中 → NVCV_LEAK_DETECTION=abort 执行；失败记入 failure_sets
        └─ 否则   → Skipping <testexec> test suite...
        ▼
EXIT trap：若 failure_sets 非空 → "Tests FAILED: ..." 退出码 1
```

#### 4.1.3 源码精读

**第一步：构建开测试。** README 给出的快速通道：

- [tests/README.md:14-16](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md#L14-L16) —— `bash build.sh -DBUILD_TESTS=1` 打开整套测试构建（回忆 u1-l3：`BUILD_TESTS=ON` 会传染性地强制开启 C++/Python 子开关）。
- [tests/README.md:35-39](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md#L35-L39) —— 运行 `build-rel/bin/run_tests.sh`，并明确说明该脚本是 CMake 从 `run_tests.sh.in` 生成的，构建成功前不存在。

**第二步：模板骨架。** 驱动脚本的核心是 `run` 函数，它接收「二进制名 + 若干标签」：

- [tests/run_tests.sh.in:39-43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/run_tests.sh.in#L39-L43) —— 第一个命令行参数按逗号拆成 `test_set` 数组；不给参数时默认 `all`（L21）。
- [tests/run_tests.sh.in:36-37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/run_tests.sh.in#L36-L37) —— 修正 `LD_LIBRARY_PATH`，让独立测试容器能找到 `libnvcv_types.so` / `libcvcuda.so`（注释解释了 stale RPATH 问题）。
- [tests/run_tests.sh.in:77-99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/run_tests.sh.in#L77-L99) —— **标签匹配是「与」语义**：`test_set` 里每个过滤器都必须匹配该二进制的至少一个标签；`all` 匹配一切。所以 `run_tests.sh cvcuda,cpp` 只会跑「同时带 `cvcuda` 和 `cpp` 标签」的二进制。
- [tests/run_tests.sh.in:101-109](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/run_tests.sh.in#L101-L109) —— 命中则以 `NVCV_LEAK_DETECTION=abort` 执行；非零退出码记入 `failure_sets`。
- [tests/run_tests.sh.in:45-52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/run_tests.sh.in#L45-L52) —— `on_exit` 钩子汇总所有失败集合统一报告。**注意：失败不会中断后续套件**，而是一路收集到最后。

泄漏检测的另一端在库里：[src/nvcv/src/priv/HandleManagerImpl.hpp:336-348](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/HandleManagerImpl.hpp#L336-L348) —— 句柄管理器清理时若仍有存活对象（`usedCount > 0`），读取 `NVCV_LEAK_DETECTION` 环境变量决定行为；驱动脚本把它设成 `abort`，于是「测试跑完还握着句柄」会直接终止进程报错。

**第三步：谁往脚本里追加 `run` 行。** `nvcv_add_test` 宏一手包办 ctest 注册与脚本追加：

- [tests/CMakeLists.txt:63-102](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt#L63-L102) —— 宏定义。其中 [L85-91](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt#L85-L91) 把 ARGN 里的标签用空格拼接后 `file(APPEND)` 一行 `run <name> <tags>` 到驱动脚本；[L79-82](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt#L79-L82) 同时给 ctest 设 620 秒超时。
- [tests/cvcuda/system/CMakeLists.txt:128](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/CMakeLists.txt#L128) —— `nvcv_add_test(cvcuda_test_system cvcuda cpp)`：主系统测试二进制的两个标签就是 `cvcuda` 和 `cpp`。同目录 [L109](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/CMakeLists.txt#L109) 的 smoke 版、[tests/cvcuda/unit/CMakeLists.txt:54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/unit/CMakeLists.txt#L54) 的 unit 版标签相同；Python 版（[tests/cvcuda/python/CMakeLists.txt:58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/CMakeLists.txt#L58)）标签为 `cvcuda python`；nvcv 侧各二进制标签为 `nvcv cpp`。

于是常用的过滤组合：

| 命令 | 实际运行 |
|------|----------|
| `run_tests.sh` 或 `run_tests.sh all` | 全部二进制 |
| `run_tests.sh cvcuda,cpp` | `cvcuda_test_system`、`cvcuda_test_system_smoke`、`cvcuda_test_unit` |
| `run_tests.sh cvcuda,python` | `cvcuda_test_python` |
| `run_tests.sh nvcv,cpp` | 全部 `nvcv_test_*` 二进制 |

要进一步缩小到单个算子，直接调二进制加 googletest 过滤器：`build-rel/bin/cvcuda_test_system --gtest_filter='*Flip*'`。

#### 4.1.4 代码实践

1. **实践目标**：亲手生成 `run_tests.sh` 并看清它的内容与过滤行为。
2. **操作步骤**：
   - 在有 CUDA 工具链的机器上执行 `bash build.sh release build-rel -DBUILD_TESTS=1`（依赖与 preset 细节见 u1-l3）；
   - 打开生成的 `build-rel/bin/run_tests.sh`，找到模板骨架（`run` 函数）与构建期追加的 `run cvcuda_test_system cvcuda cpp` 行；
   - 运行 `build-rel/bin/run_tests.sh cvcuda,cpp`，观察各套件的 Running/Skipping 输出；
   - 再运行 `build-rel/bin/cvcuda_test_system --gtest_filter='*OpFlip*'`，只跑 Flip 相关用例。
3. **需要观察的现象**：`run_tests.sh cvcuda,cpp` 会跳过所有 `nvcv_test_*` 与 `cvcuda_test_python`；`--gtest_filter='*OpFlip*'` 只执行 OpFlip / OpFlipPlanar / OpFlip_Negative 三组。
4. **预期结果**：Flip 全部用例 PASSED（参数化后缀是一串十六进制哈希）。本讲义撰写环境无 GPU，以上属**待本地验证**。
5. 若无法构建，对照 `run_tests.sh.in` 手工推演一遍标签匹配逻辑同样完成本实践。

#### 4.1.5 小练习与答案

**练习 1**：`run_tests.sh cpp`（单个标签）会运行哪些二进制？会跳过哪些？
**答案**：会运行所有带 `cpp` 标签的二进制——cvcuda 侧三个 C++ 套件加 nvcv 侧全部 `nvcv_test_*`；跳过 `cvcuda_test_python`（标签是 `cvcuda python`，不含 `cpp`）。过滤是「每个过滤器命中至少一个标签」的与语义，单标签 `cpp` 等于「所有 C++ 套件」。

**练习 2**：为什么 `run_tests.sh` 里单个套件失败后其他套件还会继续跑？
**答案**：[run_tests.sh.in:103-108](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/run_tests.sh.in#L103-L108) 执行前 `set +e`、失败只把名字追加进 `failure_sets`，汇总推迟到 `on_exit`（L45-52）。这样一次运行能拿到完整的失败清单，而不是停在第一个错误。

**练习 3**：`NVCV_LEAK_DETECTION=abort` 由谁消费？
**答案**：由 `libnvcv_types` 的句柄管理器消费（[HandleManagerImpl.hpp:342](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/HandleManagerImpl.hpp#L342)）。测试二进制退出时若仍有未销毁句柄，该变量取值 `abort` 会让进程直接中止，把「句柄泄漏」变成可见的测试失败；Debug 构建下不设该变量时默认 `warn`。

### 4.2 模块二：NVCV_TEST_SUITE_P 参数化框架

#### 4.2.1 概念说明

CV-CUDA 每个算子要在「布局 × 通道 × dtype × 尺寸 × 参数」的笛卡尔积里挑代表性组合测试。原生 googletest 的参数化写法冗长且测试名后缀是自增序号，本仓库因此在 `tests/common` 里造了一层薄 DSL：

- `ValueList<T...>`：一行一个 `std::tuple` 的值表；
- `NVCV_TEST_SUITE_P(名, 值表)`：定义 suite 类 + 自动实例化；
- MD5 参数后缀：测试名与参数内容强绑定，跨平台稳定。

[tests/CMakeLists.txt:19-27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt#L19-L27) 的注释道破设计意图：让「新增参数范围的用例」变得容易、让「被测参数集」在视觉上与参考文档对齐——测试即契约文档。

#### 4.2.2 核心流程

```text
NVCV_TEST_SUITE_P(OpFlip, ValueList<int,int,int,NVCVImageFormat,int>{ {...}, {...} })
   │
   ├─ 1. UniqueSort(值表)：按 tuple 排序并去重（重复参数自动合并）
   ├─ 2. class OpFlip : public testing::TestWithParam<值类型>  + protected GetParamValue<I>()
   └─ 3. NVCV_INSTANTIATE_TEST_SUITE_P(_, OpFlip, 值表, TestSuffixPrinter())
          └─ 后缀 = MD5(参数) 折叠成 32 位十六进制
```

后缀哈希的计算：把参数喂进 MD5 得到 128 位，折成两个 64 位字 \( w_0, w_1 \)，再

\[ \text{code}_{32} = (w_0 \oplus w_1) \,\&\, \texttt{0xFFFFFFFF} \,\oplus\, ((w_0 \oplus w_1) \gg 32) \]

#### 4.2.3 源码精读

- [tests/cvcuda/system/TestOpFlip.cpp:44-66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L44-L66) —— OpFlip 正例的值表。五列分别是 `width, height, batches, format, flipCode`，列含义写在注释行里；每行行尾注释说明**为什么**选这组参数（float3 走保守内核、宽度被 4 整除走 VEC=4 向量化路径、宽度 255 走标量尾部）。这张表正是「参数集视觉对齐文档」理念的体现。
- [tests/common/ValueTests.hpp:222-233](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/ValueTests.hpp#L222-L233) —— 宏展开：全局值表 `g_OpFlip_Params` 经 `UniqueSort` 去重排序；suite 继承 `TestWithParam`；`GetParamValue<I>()` 按**编译期下标**取第 I 列（比 `std::get<I>(GetParam())` 多一层 `ParamValue` 解包，兼容带名字的 `Param<...>` 包装类型）。
- [tests/common/ValueTests.hpp:215-220](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/ValueTests.hpp#L215-L220) —— 实例化宏，用 `ValuesIn(UniqueSort(...))` 喂参，并指定自定义 `TestSuffixPrinter`。
- [tests/common/ValueTests.hpp:199-211](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/ValueTests.hpp#L199-L211) —— 为什么不用 googletest 默认自增后缀：序号与参数内容**没有**绑定，若某平台缺一个参数，序号就会指向另一个参数；哈希后缀保证「同一测试名永远对应同一组参数」。
- [tests/common/ValueList.hpp:422-457](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/ValueList.hpp#L422-L457) —— `UniqueSort` 的两段实现（带提取器版本与便捷版本），是值表去重的落点。

#### 4.2.4 代码实践

1. **实践目标**：验证「测试名 ↔ 参数」的强绑定，学会按哈希后缀定位参数。
2. **操作步骤**：
   - 运行 `build-rel/bin/cvcuda_test_system --gtest_filter='*OpFlip*' --gtest_list_tests`，抄下几个完整测试名（形如 `OpFlip/correct_output/3f2a1b0c`）；
   - 在测试输出里故意制造一次失败（例如临时把某行值表的 `flipCode` 改成与断言不符的值再编译），观察失败项的哈希后缀；
   - 对着值表数行号，确认该后缀对应的正是你改的那行。
3. **需要观察的现象**：列表中的测试数量 ≤ 值表行数（重复行被 `UniqueSort` 合并）；失败报告只落在被改的那一个参数实例上。
4. **预期结果**：哈希后缀在改参数前后发生变化，且不受其它行增删影响。改动属临时实验，结束务必还原。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：值表里写两行完全相同的参数会怎样？
**答案**：`UniqueSort` 会去重（[ValueTests.hpp:223](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/ValueTests.hpp#L223)），只实例化一次。所以不用担心手滑重复。

**练习 2**：为什么值表要排序而不保持书写顺序？
**答案**：排序让参数集合有规范形式（canonical order）：不同开发者以不同顺序添加参数时，实例化顺序与哈希命名保持稳定，测试输出可比较、增量可审阅。

**练习 3**：`GetParamValue<3>()` 返回什么类型？
**答案**：返回值表第 4 列（下标从 0 计）经 `ParamValue` 解包后的值。OpFlip 表中第 4 列声明为 `NVCVImageFormat`，故返回 `NVCVImageFormat`（测试体在 [TestOpFlip.cpp:79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L79) 用它构造 `nvcv::ImageFormat`）。

### 4.3 模块三：CPU 黄金参考 FlipUtils.cpp

#### 4.3.1 概念说明

`FlipCPU` 是 Flip 的**独立** CPU 实现：不调用任何被测代码，只用标准 C++ 按_stride 寻址_逐像素重排。它必须与 GPU 实现来自不同的「思维路径」——GPU 侧是向量化 kernel（见 u5-l3），CPU 侧是最朴素的三重循环——两边逐字节一致才有意义。

黄金参考放在独立的 `FlipUtils.cpp`（而非测试文件里），因为它同时服务正例与 planar 奇偶校验等多处；头文件 [FlipUtils.hpp:30-31](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.hpp#L30-L31) 只暴露一个自由函数 `nvcv::test::FlipCPU`。

#### 4.3.2 核心流程

```text
FlipCPU(hDst, dstStrides, hSrc, srcStrides, shape, format, flipCode)
   │
   ├─ 断言 format.numPlanes() == 1
   ├─ switch (format.planeDataType(0))        // 格式 → 像素 C++ 类型
   │     U8→uint8_t  U16→ushort  3U8→uchar3  4U8→uchar4  4F32→float4  3F32→float3
   │     default: break;                        // ⚠ 未登记类型静默跳过 → 金标全零
   └─ detail::flip<T>: for b { for y { for x {
          hDst[b,y,x] = SaturateCast(hSrc[b, FlippedY(y), FlippedX(x)])
      } } }
```

坐标翻转规则与 OpenCV 对齐：`flipCode == 0` 上下翻（x 不动）、`flipCode > 0` 左右翻（y 不动）、负值双轴。

#### 4.3.3 源码精读

- [FlipUtils.cpp:30-38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L30-L38) —— `FlippedX`/`FlippedY` 两个纯函数实现坐标映射，翻转语义的唯一权威定义。
- [FlipUtils.cpp:40-50](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L40-L50) —— `ValueAt<T>`：按 `b*pitches.x + y*pitches.y + x*pitches.z` 字节偏移做 `reinterpret_cast`，即 u2-l1 讲过的 stride 寻址公式的直接复用；金标因此天然支持任意非紧凑 stride。
- [FlipUtils.cpp:59-77](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L59-L77) —— 模板三重循环本体。注意它复用了 `cvcuda/cuda_tools` 里的 `SaturateCast`/`BaseType`/`DropCast`（见文件头 [L20-24](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L20-L24)）：类型转换语义与 GPU 侧共用同一套定义，避免「金标与实现各自理解饱和转换」的偏差。
- [FlipUtils.cpp:79-92](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L79-L92) —— `NVCV_TEST_INST` 宏**显式实例化**六种像素类型（uint8_t、ushort、uchar3、uchar4、float4、float3）。因为 `flip<T>` 的消费者在别的编译单元，必须显式实例化才能链接。
- [FlipUtils.cpp:96-120](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L96-L120) —— `FlipCPU` 按 `format.planeDataType(0)` 分派到对应实例。`default: break`（[L117-118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L117-L118)）是**静默的**：未登记 dtype 时金标缓冲保持全零——这正是本讲综合实践要踩的坑。

与契约表对照：[src/cvcuda/include/cvcuda/OpFlip.h:80-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L80-L90) 声明 Tensor 版 Flip 支持 8U/16U/32S/32F；而金标只覆盖 U8/U16/3U8/4U8/3F32/4F32——**32S 是契约允许、金标未覆盖的空档**（综合实践将补上它）。GPU 侧的对应分派表在 [src/cvcuda/priv/legacy/flip.cu:425-432](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L425-L432)（行类型 × 通道数的函数指针矩阵，含 `flipSingleChannel<int32_t>`），dtype 白名单校验在 [L397-402](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L397-L402)。

#### 4.3.4 代码实践

1. **实践目标**：在不跑 GPU 的前提下，用金标预判「新增未登记 dtype」的后果。
2. **操作步骤**：
   - 通读 `FlipCPU` 的 switch，列出已覆盖的六个 `NVCV_DATA_TYPE_*`；
   - 假设在 `TestOpFlip.cpp` 值表加一行 `{64, 48, 2, NVCV_IMAGE_FORMAT_S32, 1}`（S32 = 单通道 32 位有符号，[ImageFormat.h:84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L84)）；
   - 推演：`planeDataType(0)` 会落到 switch 哪个分支？`goldVec` 内容是什么？GPU 输出是什么？`EXPECT_EQ` 结果如何？
3. **需要观察的现象**：这是纯纸面推演，不需要机器。
4. **预期结果**：S32 落入 `default: break`，`goldVec` 保持构造时的零值；GPU 侧正常翻转，`testVec` 非零 → 断言失败，且失败的是**金标**而非算子。结论：给正例值表加新 dtype 时，必须同步扩展 `FlipUtils.cpp` 的实例化与分派（见第 5 节综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FlipCPU` 断言 `format.numPlanes() == 1`？
**答案**：金标的寻址模型是「单平面 + (b,y,x) 三维 stride」（[FlipUtils.cpp:99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L99)）。多平面格式（如 NV12）的平面分辨率不同，不能塞进这个模型；planar（NCHW）输入的校验走的是第 4.4 节的「奇偶校验」路径而非金标直比。

**练习 2**：金标为什么借用 `cuda_tools` 的 `SaturateCast` 而不自己写转换？
**答案**：让「数值语义」（饱和、类型提升）在 CPU 参考与 GPU kernel 中是同一份代码，金标校验的才是「实现正确性」而不是「两份转换实现的相等性」。Flip 是纯重排、实际不改变数值，但这一约定对 normalize/brightness_contrast 等有运算的算子至关重要。

**练习 3**：`ValueAt` 为什么用字节级 `long3` stride 而不是元素级下标？
**答案**：nvcv 的 stride 语义以**字节**为单位（u2-l1），且张量行距可能对齐到设备纹理边界而插入 padding；字节级寻址让金标能原样消费 `exportData` 导出的真实 stride，与 GPU kernel 看到的内存完全一致。

### 4.4 模块四：TestOpFlip.cpp 的四类用例

#### 4.4.1 概念说明

一个 TestOp*.cpp 通常包含四类用例，各守护一面：

| 类别 | 用例名 | 守护的正确性面 |
|------|--------|----------------|
| Tensor 正例 | `OpFlip.correct_output` | GPU 结果 == CPU 金标（逐字节） |
| 变长批正例 | `OpFlip.varshape_correct_output` | 逐图随机尺寸 + 参数张量化路径下的正确性 |
| Planar 奇偶 | `OpFlipPlanar.tensor/varshape_matches_interleaved` | NCHW 输出与 NHWC 输出逐位一致（仓库不变量：双布局支持） |
| 负例 | `OpFlip_Negative.*` | 非法输入必须被拒（返回 `NVCV_ERROR_INVALID_ARGUMENT`）而非崩溃 |

#### 4.4.2 核心流程

Tensor 正例的固定五段式（几乎所有 TestOp*.cpp 共用）：

```text
① 造流：cudaStreamCreate
② 造数据：CreateTensor(N,w,h,fmt) ×2 → exportData<TensorDataStridedCuda>() → 算 stride/size
③ 造金标：随机 inVec → FlipCPU(goldVec, ...)
④ 跑算子：H2D 拷贝 → flipOp(stream,in,out,flipCode) → cudaStreamSynchronize
⑤ 判定：D2H 拷贝 → EXPECT_EQ(testVec, goldVec)
```

变长批正例把 ②③ 换成「逐图随机尺寸 + 逐图金标」，把 flipCode 升级为 N 元素 S32 张量。

#### 4.4.3 源码精读

**Tensor 正例** [TestOpFlip.cpp:70-133](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L70-L133)：

- [L85-86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L85-L86) —— 测试工具 `nvcv::util::CreateTensor(batches, width, height, format)` 造输入/输出张量（声明与语义见 [tests/common/TensorDataUtils.hpp:213-222](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/common/TensorDataUtils.hpp#L213-L222)：N==1 造 HWC，否则 NHWC）。
- [L88-107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L88-L107) —— u5-l2 的主角在测试中现身：`exportData<nvcv::TensorDataStridedCuda>()` 判空后，用 `TensorDataAccessStridedImagePlanar` 取出 sample/row/col 三级 stride，算出主机侧缓冲大小。测试对 stride 的处理与 kernel 完全同构。
- [L109-116](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L109-L116) —— 固定种子（`default_random_engine randEng(0)`）生成 0-255 随机字节，再调 `test::FlipCPU` 生成金标。**固定种子保证失败可复现**。
- [L122-123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L122-L123) —— 与 u6-l1 完全相同的调用姿势：栈上构造 `cvcuda::Flip`，函数调用操作符提交到流；`EXPECT_NO_THROW` 确保正常路径不抛异常。
- [L129-132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L129-L132) —— 同步后 D2H 取回，`EXPECT_EQ(testVec, goldVec)` 对两个 `std::vector<uint8_t>` 做**整缓冲逐字节**比较——不是抽样、不是容差，Flip 这类纯重排算子要求位级一致。

**变长批正例** [L135-240](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L135-L240)：

- [L149-151](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L149-L151) —— 每张图尺寸在 `[0.8·w, 1.1·w] × [0.8·h, 1.1·h]` 随机，真正制造「批内尺寸不一」（u2-l3 的变长批语义）。
- [L153-189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L153-L189) —— 逐图 `nvcv::Image` + `cudaMemcpy2DAsync` 按各自行距上传，`pushBack` 进 `ImageBatchVarShape`；输出批逐图同尺寸重建。
- [L191-201](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L191-L201) —— 变长批入口的参数张量化：flipCode 不再是标量，而是 `{N}` 布局的 `TYPE_S32` 张量（u3-l1 讲过的「可逐图变化的参数升级为张量」）。
- [L212-238](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L212-L238) —— 逐图取回、逐图生成金标、逐图断言；`SCOPED_TRACE(i)`（L214）把失败定位到具体某张图。

**Planar 奇偶校验** [L242-321](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L242-L321)：

- [L244-249](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L244-L249) —— 注释阐明原理：Flip 与通道无关，planar 输入逐平面翻转后**必须**与 interleaved 输出逐位相同。这对应仓库不变量「图像算子默认同时支持 NHWC 与 NCHW」（AGENTS.md）。
- [L256-266](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L256-L266) —— 共享脚手架 `test::planar::RunTensorParity` 负责「同一份数据按两种布局各跑一遍 + 重排后比对」，测试体只用 lambda 绑定算子调用；变长批版在 [L269-290](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L269-L290)。
- [L296-307](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L296-L307) —— `OpFlipPlanar` 值表：planar 格式与 interleaved 格式**成对**出现（如 `FMT_RGB8p` 配 `FMT_RGB8`），覆盖 RGB8/RGBA8 及三种 float 组合。

**负例** [L324-471](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L324-L471)：

- [L324-329](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L324-L329) —— 负例值表成对给出「输入格式 / 输出格式」：dtype 不同、interleaved 进 planar 出、不支持的 2 通道、不支持的 F16。
- [L347-348](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L347-L348) —— `nvcv::ProtectCall` 捕获算子抛出的异常并翻译成状态码，断言**必须等于** `NVCV_ERROR_INVALID_ARGUMENT`（异常↔状态码互译见 u6-l2）。变长批版在 [L403-404](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L403-L404)。
- [L407-466](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L407-L466) —— 变长批特有负例：批内混入一张不同格式的图（u2-l3 说过容器允许混格式，但算子要求 uniqueformat），同样必须被拒。
- [L468-471](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L468-L471) —— 最小负例：`cvcudaFlipCreate(nullptr, 2)` 直接对 C API 传 NULL 句柄，断言返回 `NVCV_ERROR_INVALID_ARGUMENT`。一句话测掉 u6-l1 讲过的句柄校验防线。

顺带一提全局兜底：[tests/Main.cpp:42-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/Main.cpp#L42-L57) 的事件监听器在每个用例结束时检查 `cudaGetLastError()` 并做设备级同步——任何用例「漏出的」CUDA 错误都会记到当前用例头上，脏状态不会流到下家（[L29-37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/Main.cpp#L29-L37) 在用例开始时吞掉历史错误）。

#### 4.4.4 代码实践

本讲的主实践（对应任务书）：**给正例值表补一个未覆盖的 dtype 组合**。

1. **实践目标**：为 `OpFlip` 值表新增 BGR8 用例——它被 Limitations 支持（8U × 3 通道）、被 kernel 分派表实现（`flip<uchar3>`），但值表缺席。
2. **操作步骤**：
   - 核对契约：[OpFlip.h:61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L61) 布局含 NHWC、通道 `[1,3,4]`、[L82](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L82) 8bit Unsigned = Yes；
   - 核对金标：BGR8 的 `planeDataType(0)` 是 `3U8`，已在 `FlipCPU` 的 case 里（[FlipUtils.cpp:110](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L110)），**无需改金标**；
   - 在 [TestOpFlip.cpp:52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L52) 之后插入一行（示例代码，非仓库原有）：

     ```cpp
     {     96,     64,       3,  NVCV_IMAGE_FORMAT_BGR8, -1},  // BGR8, both axes
     ```

   - 重新编译并运行 `build-rel/bin/cvcuda_test_system --gtest_filter='*OpFlip*'`。
3. **需要观察的现象**：新实例的哈希后缀出现并 PASSED；Tensor 与变长批两个用例各多一条通过记录（同一值表喂两个 `TEST_P`）。
4. **预期结果**：新增用例通过。若失败，优先怀疑金标未覆盖该 dtype（对照 4.3 的 switch），其次怀疑 kernel 分派表空档。注意 `TestOpFlip.cpp` 已有 SPDX 头，只改内容无需新增；**若新建文件**（本实践不需要），须按仓库规范加 `Copyright (c) 2026` 的 NVIDIA Apache 2.0 SPDX 头。本实践需 CUDA 环境，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：正例为什么用 `EXPECT_NO_THROW` + 事后 `EXPECT_EQ`，而不是直接比较？
**答案**：两段断言各管一半：`EXPECT_NO_THROW`（[L123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L123)）保证正常路径没有把错误藏进异常；`EXPECT_EQ`（L132）保证数值正确。分开写让失败报告能区分「算子报错」与「结果错误」。

**练习 2**：为什么变长批正例不重用 `CreateTensor`，而是手工造 `nvcv::Image`？
**答案**：`CreateTensor` 只能造规则批（批内同尺寸）；变长批需要每张图尺寸独立随机（[L158-177](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L158-L177)），必须逐图 `nvcv::Image` 再 `pushBack` 进 `ImageBatchVarShape`。

**练习 3**：负例值表里 `{FMT_2S16, FMT_2S16}` 期望什么错误？哪一层拦下它？
**答案**：期望 `NVCV_ERROR_INVALID_ARGUMENT`。拦截发生在 priv 层 → legacy 内核的通道数白名单：[flip.cu:410-414](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L410-L414) 明确拒绝 `C == 2`，错误码沿 u6-l2 的翻译链变成 `INVALID_ARGUMENT` 抛回、再被 `ProtectCall` 译回状态码供断言。

### 4.5 模块五：TestAPI 头文件兼容性测试（模板生成）

#### 4.5.1 概念说明

「TestAPI 模板生成」指的是另一类测试：`tests/cvcuda/system/TestAPI.c.in` 与 `TestAPI.cpp.in` 不是手写源码，而是**占位模板**——构建时被展开成「`#include` 全部公共头文件」的翻译单元，分别以 C11 与 C++11 编译。它守护的是：**每个公开头单独/集体包含时都能在最低标准下编译通过**（API 承诺的最低语言标准见各头文件的兼容性声明）。

#### 4.5.2 核心流程

```text
file(GLOB_RECURSE include/*.h / *.hpp)          # 收集全部公共头
        ▼
add_header_compat_test(SOURCE TestAPI.cpp STANDARD c++11 ...)
        ├─ 把每个头拼成 "#include <hdr>\n" 存入 ALL_HEADERS
        ├─ configure_file 两次 → a_TestAPI.cpp / b_TestAPI.cpp
        └─ 对每个可用编译器（gcc/clang…）：
             编译 a_、b_ 两个翻译单元并链接成 .so
             （编两次是为了抓「多翻译单元包含导致重复定义」的链接错误）
```

#### 4.5.3 源码精读

- [tests/cvcuda/system/TestAPI.cpp.in:18](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestAPI.cpp.in#L18) —— 模板本体只有一行 `@ALL_HEADERS@`：SPDX 头 + 占位符，其余全部交给 CMake 展开。
- [tests/cvcuda/system/CMakeLists.txt:134-151](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/CMakeLists.txt#L134-L151) —— 用 `file(GLOB_RECURSE)` 抓取 `src/cvcuda/include` 下全部 `.h`/`.hpp`（排除 C++17 的 `cuda_tools/`），分别以 `c11`/`c++11` 标准 `add_header_compat_test`。**GLOB 意味着新增公共头自动纳入测试，无需登记**。
- [cmake/ConfigCompiler.cmake:108-136](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigCompiler.cmake#L108-L136) —— 函数实现：[L127-130](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigCompiler.cmake#L127-L130) 把头列表拼成 include 行；[L132-136](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigCompiler.cmake#L132-L136) 注释点明「编译链接两次以捕捉多重定义」。

#### 4.5.4 代码实践

1. **实践目标**：看懂一个「由模板生成的测试」在构建目录里的真身。
2. **操作步骤**：构建后在 `build-rel` 下搜索 `a_TestAPI.cpp`（例如 `rg --files build-rel | rg 'TestAPI'`），打开查看展开后的完整 include 列表；再对照 `src/cvcuda/include/cvcuda/` 目录数一数头文件数量是否吻合。
3. **需要观察的现象**：展开文件是一长串 `#include <cvcuda/OpXxx.h>`；`.c` 版与 `.cpp` 版列表一致。
4. **预期结果**：include 数量与公开头数量一致；这解释了「为什么新增算子不需要手动登记头测试」。也可在构建日志中找到对应的编译命令。**待本地验证**（需要一次完整构建）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 C 头用 `c11`、C++ 头用 `c++11` 编译，而不是用项目实际的 C++20？
**答案**：这是**最低承诺标准**测试：验证公共头不意外依赖更高语言特性，保证下游老编译器用户也能包含。项目自身代码用 C++20（[tests/CMakeLists.txt:27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt#L27)），但那是实现侧的自由。

**练习 2**：`cuda_tools/` 头为何被排除（[system/CMakeLists.txt:144-145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/CMakeLists.txt#L144-L145)）？
**答案**：注释写明这些头是 C++17 的，不参与 `c++11` 兼容承诺，过滤掉以免误报。

## 5. 综合实践

**任务：为 Tensor 版 Flip 补齐「32 位有符号单通道（S32）」覆盖——一次需要同时动值表与金标的完整贡献。**

背景：契约表（[OpFlip.h:87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L87)）允许 32S，GPU 分派表也实现了它（[flip.cu:430](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L430) 的 `flipSingleChannel<int32_t>`），但正例值表与 CPU 金标双双缺席。补齐它要走四步：

1. **先写失败**（遵循仓库 bugfix/测试纪律）：
   - 仅在 `TestOpFlip.cpp` 值表加一行（示例代码）：`{64, 48, 2, NVCV_IMAGE_FORMAT_S32, 1},`
   - 编译运行 `--gtest_filter='*OpFlip*'`，确认新用例**失败**且差异是「GPU 输出非零 vs 金标全零」——这正是 4.3.4 预判的 `default: break` 静默路径。注意：S32 像素占 4 字节，随机字节流经 `reinterpret_cast<int>` 读出可能为负，这没有关系——Flip 是纯重排，金标与 GPU 对任意位模式行为一致。
2. **扩展金标**（`tests/cvcuda/system/FlipUtils.cpp`，示例代码）：
   - 在显式实例化区（[L85-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L85-L90) 附近）加 `NVCV_TEST_INST(int32_t);`
   - 在 `FlipCPU` 的 switch（[L108-113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/FlipUtils.cpp#L108-L113) 附近）加 `NVCV_TEST_CASE(S32, int32_t);`（宏 `NVCV_DATA_TYPE_S32` 存在于 [DataType.h:105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.h#L105)）。
3. **复跑转绿**：重新编译，确认新增的 Tensor 与变长批两个实例都 PASSED。若变长批版失败而 Tensor 版通过，对照 [OpFlip.h:133](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L133) 检查两条入口的 dtype 契约差异，并解释原因。
4. **按贡献规范收尾**：
   - 两个文件都已有 SPDX 头，无需新增；若你另建了新文件，必须加 `Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.` 的 Apache-2.0 SPDX 头；
   - 跑 `pre-commit run --files tests/cvcuda/system/TestOpFlip.cpp tests/cvcuda/system/FlipUtils.cpp`；
   - 用 `git diff` 自查：改动是否只包含这一组覆盖的添加，有没有夹带格式化噪音。

本综合实践需要 CUDA GPU 与完整构建环境；在无 GPU 环境下，可完成全部代码修改与纸面推演，运行结果**待本地验证**。

## 6. 本讲小结

- `run_tests.sh` 是**构建期生成物**：`run_tests.sh.in` 提供骨架，每个 `nvcv_add_test(<exe> <tags>)` 追加一行 `run`；过滤是「每个过滤器命中至少一个标签」的与语义，`cvcuda,cpp` 即三件 C++ 套件。
- 测试驱动以 `NVCV_LEAK_DETECTION=abort` 执行二进制，句柄泄漏会被库侧 HandleManager 在退出时转化为硬失败。
- `NVCV_TEST_SUITE_P` + `ValueList` 是自制参数化 DSL：值表即契约文档，`UniqueSort` 去重排序，MD5 折叠哈希做测试名后缀使「名字↔参数」跨平台强绑定。
- 系统测试的核心范式是**五段式**：造流 → 造张量/算 stride → 固定种子随机数据 + 独立 CPU 金标 → 跑算子并同步 → 逐字节 `EXPECT_EQ`；变长批正例再叠加逐图随机尺寸与参数张量化。
- 四类用例各守一面：Tensor 正例（对金标位级一致）、变长批正例（逐图校验）、planar 奇偶（NCHW 与 NHWC 逐位一致，仓库双布局不变量）、负例（非法输入必须返回 `NVCV_ERROR_INVALID_ARGUMENT`，借 `ProtectCall` 断言）。
- **给正例加新 dtype 时，金标必须同步扩展**：`FlipCPU` 的 `default: break` 是静默的，漏配会表现为「金标全零」的假失败；TestAPI 模板测试则保证全部公共头在最低语言标准下可独立编译。

## 7. 下一步学习建议

- **u7-l2（Python 测试）**：对照本讲看 `tests/cvcuda/python/test_opflip.py`——Python 侧如何用 numpy 复刻同一套「金标比对」思路，以及 `cvcuda_tools.py` 提供的白盒工具；两套测试的分工与互补。
- **补一个真贡献**：沿第 5 节的路线，挑一个 TestOp*.cpp 里「契约允许、值表缺席」的组合提一个小 PR（先看 `.agents/guidance/REVIEW_OP_GUIDELINES.md` 与 `tools/review_op.py <Op> --domain test`，它会确定性列出测试覆盖缺口）。
- **阅读相邻文件**：`tests/cvcuda/system/PlanarParityUtils.hpp`（奇偶校验脚手架的完整实现）与 `tests/common/ValueList.hpp` 的组合子（`Concat`、笛卡尔积等），理解 DSL 的表达力上限。
- **下一站 u7-l3（基准测试）**：从「对不对」转向「快慢」，看 `bench/` 如何用 nvbench 与 Python 基准回答性能问题。
