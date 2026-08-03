# 模糊测试与基准测试

## 1. 本讲目标

本讲是「测试、模糊测试与基准测试」单元的第三讲，承接 u5-l2（mem\* 框架）中学过的内存函数实现，回答两个工程问题：

- **正确性兜底**：除了单元测试（u10-l1）和高精度对照（u10-l2），项目如何用**模糊测试（fuzzing）**持续喂入海量随机/畸形输入，去挖掘那些手写测试想不到的边界 bug？
- **性能可观测**：实现改了之后是变快还是变慢？项目如何用**微基准（microbenchmark）**精确测量 `memcpy` 这类只跑几个周期的低延迟函数，并把测量结果变成可比较、可复现的数据？

学完本讲，你应当能够：

1. 理解 `fuzzing/` 目录如何镜像 `src/` 结构为各组件提供模糊测试，区分**差分模糊（differential fuzz）**与**纯属性模糊**两种写法。
2. 掌握 `benchmarks/` 自带的微基准框架如何用「指数增长的批量重测 + 收敛判停」算法，把几周期的函数测到 1% 精度。
3. 看懂内存函数如何用从真实生产负载采样得到的**大小概率分布**做随机化，使测量既代表性又可复现。
4. 认识框架与 Google Benchmark 的集成方式，以及用 Python 分析脚本把 JSON 结果渲染成图。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应前置讲义）：

- **入口点与构建目标**（u2-l1/u2-l3）：每个公开函数是一个 entrypoint，CMake 里通过点分全限定名（如 `libc.src.string.memcpy`）引用其 object library。
- **mem\* 框架**（u5-l2）：`memcpy`/`memset`/`memmove`/`memcmp`/`bcmp`/`bzero` 用 `block`/`tail`/`head_tail`/`loop_and_tail` 构建块按尺寸分派实现——这正是基准测试要测的对象。
- **自包含测试体系**（u10-l1）：libc 在 GPU/baremetal 上没有宿主 C++ 运行时，所以测试/基准设施大多自带依赖。

本讲会用到几个通用概念，先用一句话解释：

- **模糊测试（fuzzing）**：用一个带覆盖引导的「模糊器（fuzzer）」自动生成大量输入喂给目标函数，一旦触发崩溃/断言失败就报告。LLVM 自带的是 libFuzzer，编译期加 `-fsanitize=fuzzer` 即可同时插桩并提供驱动 `main`。
- **差分测试（differential testing）**：把两份实现（如 libc 内部版与系统 libc 版）喂相同输入，比对输出，输出不一致即视为 bug。它不需要预先知道「正确答案」。
- **微基准（microbenchmark）**：测量一小段代码（如一次 `memcpy`）的耗时。难点在于：被测代码太短，测量误差与被测量同级，需要重复很多次取统计量。
- **SNR（信噪比）**：有用信号与噪声的比值。要测准一个 10 周期的函数，得让它跑上千次，使总耗时远大于单次测量误差。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`fuzzing/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/fuzzing/CMakeLists.txt) | 模糊测试总入口：开启 `-fsanitize=fuzzer`，按 `src/` 同构方式 `add_subdirectory` 各函数族。 |
| [`fuzzing/string/memcmp_fuzz.cpp`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/fuzzing/string/memcmp_fuzz.cpp) | 自带参考实现的差分模糊样例。 |
| [`fuzzing/stdlib/strtointeger_differential_fuzz.cpp`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/fuzzing/stdlib/strtointeger_differential_fuzz.cpp) | 对系统 libc 做 `atoi`/`strtol` 一族的差分模糊样例。 |
| [`cmake/modules/LLVMLibCTestRules.cmake`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCTestRules.cmake) | 定义 `add_libc_fuzzer` 规则，把 fuzzer 源文件链上入口点 object 文件。 |
| [`benchmarks/README.md`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/README.md) | 基准工具使用说明（随机模式 / 扫描模式）。 |
| [`benchmarks/RATIONALE.md`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/RATIONALE.md) | 微基准设计原理：为何放弃硬件性能计数器、改用「重复取均值」。 |
| [`benchmarks/LibcBenchmark.h`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcBenchmark.h) | 核心 `benchmark()` 函数模板与停机条件 `BenchmarkOptions`。 |
| [`benchmarks/LibcMemoryBenchmark.h`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmark.h) | 内存函数专用的缓冲区/参数/分布设施。 |
| [`benchmarks/MemorySizeDistributions.h`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/MemorySizeDistributions.h) | 大小概率分布的对外接口。 |
| [`benchmarks/LibcMemoryBenchmarkMain.cpp`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmarkMain.cpp) | 内存基准的可执行入口，串起分布/扫描两种模式并输出 JSON。 |
| [`benchmarks/libc-benchmark-analysis.py3`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/libc-benchmark-analysis.py3) | 把 JSON 结果渲染成对比图的 Python 脚本。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**模糊测试组织**、**微基准框架**、**内存大小分布**、**结果分析**。

### 4.1 模糊测试组织

#### 4.1.1 概念说明

模糊测试用「随机输入 + 自动驱动」去撞击函数，目标是让手写测试漏掉的边界情况自己暴露出来。libc 的模糊测试有两个鲜明特点：

1. **目录镜像 `src/`**：`fuzzing/` 下的子目录（`math`、`stdio`、`stdlib`、`string`、`arpa`、`__support`）与 `src/` 一一对应。找一个函数的 fuzzer，只要照着 `src/` 的路径在 `fuzzing/` 下找同名文件即可。
2. **差分测试为主**：libc 实现的正确性往往「没有标准答案可算」，于是采用差分策略——把它和**参考实现**（要么是手写的朴素版，要么是系统 libc 的同名函数）喂相同输入，输出不一致就 `__builtin_trap()`。

底层引擎是 LLVM 的 **libFuzzer**：编译时加 `-fsanitize=fuzzer`，它会一边对被测代码做覆盖引导插桩，一边提供 `main`，反复调用你写的入口 `LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)`。

#### 4.1.2 核心流程

一个 fuzzer 的运行流程：

1. libFuzzer 生成一段随机字节流 `data`（长度 `size` 可变）。
2. 调用 `LLVMFuzzerTestOneInput(data, size)`，你的代码把字节流**解释**成函数的输入（如拆成「base + 字符串」「两段缓冲区」）。
3. 调用被测函数，与参考实现比对。
4. 不一致 → `__builtin_trap()` 触发崩溃，libFuzzer 保存触发崩溃的输入（corpus）。
5. 一致 → 返回 0，libFuzzer 根据覆盖率反馈变异出下一批更有价值的输入。

#### 4.1.3 源码精读

**总入口开启 fuzzer 插桩并镜像 src 结构。** [`fuzzing/CMakeLists.txt:1-11`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/fuzzing/CMakeLists.txt) 在目录级别把 `-fsanitize=fuzzer` 加进 `CMAKE_CXX_FLAGS`，这同时完成「插桩」与「注入 libFuzzer 的 `main`」；随后 `add_subdirectory` 各函数族目录，并定义聚合目标 `libc-fuzzer` 便于一次性构建全部 fuzzer。

```cmake
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fsanitize=fuzzer")
add_custom_target(libc-fuzzer)
add_subdirectory(__support)
add_subdirectory(string)
# ... math / stdlib / stdio / arpa
```

**自带参考实现的差分模糊——以 `memcmp` 为例。** [`fuzzing/string/memcmp_fuzz.cpp:31-59`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/fuzzing/string/memcmp_fuzz.cpp) 把输入字节流对半切成两段缓冲区 `a`/`b`，同时调用 libc 内部版 `LIBC_NAMESPACE::memcmp` 与一个朴素的 `reference_memcmp`（带 `__attribute__((no_builtin))` 防止编译器把它优化成对 `memcmp` 的调用），用 `sign` 只比较三者符号（C 标准只规定 `memcmp` 返回值的符号），不一致就打印诊断并 trap。

```cpp
extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  // ...
  const auto count = size / 2;
  const char *a = reinterpret_cast<const char *>(data);
  const char *b = reinterpret_cast<const char *>(data) + count;
  const int actual = LIBC_NAMESPACE::memcmp(a, b, count);
  const int reference = reference_memcmp(a, b, count);
  if (sign(actual) == sign(reference))
    return 0;
  // ... 打印输入与期望/实际值 ...
  __builtin_trap();
}
```

**对照系统 libc 的差分模糊——以 `strtol` 一族为例。** [`fuzzing/stdlib/strtointeger_differential_fuzz.cpp:46-85`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/fuzzing/stdlib/strtointeger_differential_fuzz.cpp) 把输入首字节当进制 `base`、其余当待解析字符串，同时喂给 `LIBC_NAMESPACE::strtol` 与 `::strtol`（系统 libc）等 7 个函数比对。它还演示了一个 fuzzer 工程技巧：纯随机字节里超过一半的字符会立即终止数字解析，导致长数字几乎测不到，因此提供 `LIBC_COPT_FUZZ_ATOI_CLEANER_INPUT` 选项把字节先映射到「合法字符表」再 fuzz。

```cpp
StringToNumberOutputDiff<long>(&LIBC_NAMESPACE::strtol, &::strtol, container, size);
```

**差分比对逻辑被抽成可复用模板。** [`fuzzing/math/SingleInputSingleOutputDiff.h:21-37`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/fuzzing/math/SingleInputSingleOutputDiff.h) 把「把字节重解释为 `T`、调两个函数、比对结果」封装成模板，数学函数（`sin`、`sqrt`、…）的多个 fuzzer 都复用它，避免重复样板代码。

```cpp
template <typename T>
void SingleInputSingleOutputDiff(SingleInputSingleOutputFunc<T> func1,
                                 SingleInputSingleOutputFunc<T> func2,
                                 const uint8_t *data, size_t size) {
  if (size < sizeof(T)) return;
  T x = *reinterpret_cast<const T *>(data);
  if (!ValuesEqual(func1(x), func2(x))) __builtin_trap();
}
```

**`add_libc_fuzzer` 把源文件链上入口点 object 文件。** [`cmake/modules/LLVMLibCTestRules.cmake:482-494`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCTestRules.cmake) 展示了 fuzzer 可执行体的链接方式：用 `get_object_files_for_test` 把 `DEPENDS` 列出的入口点展开成 object 文件直接链入，并支持 `NEED_MPFR` 选项额外链上 `mpfr`/`gmp`（用于需要高精度参考的 fuzzer，如 `strtofloat_fuzz`）。目标都用 `EXCLUDE_FROM_ALL` 标记，因此默认不构建，需显式指定。

#### 4.1.4 代码实践

> **实践目标**：用目录镜像约定定位一个 fuzzer，并读懂它的差分逻辑。

1. 在 `fuzzing/string/` 下找到 `strcmp_fuzz.cpp`，阅读它的 `LLVMFuzzerTestOneInput`。
2. 确认它如何从字节流构造两个以 `\0` 结尾的字符串、分别喂给 `LIBC_NAMESPACE::strcmp` 与参考实现。
3. 思考：为什么 `strcmp` 的 fuzzer 需要自己保证 `\0` 终止，而 `memcmp` 的 fuzzer 不需要？
4. **预期结果**：你能用一句话说清 `strcmp_fuzz` 的「输入解释方式」与「比对策略」。
5. 实际运行 fuzzer 需要 `-fsanitize=fuzzer` 的完整构建环境，具体能否跑通**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `memcmp_fuzz.cpp` 里参考实现要加 `__attribute__((no_builtin))`？
**答案**：若不加，编译器识别到「手写的逐字节比较」模式后，可能把它直接优化成一次对 `memcmp` 的调用，于是「被测」与「参考」变成同一个函数，差分失去意义。

**练习 2**：`strtointeger_differential_fuzz` 同时比对 7 个函数，这样做到底是省事还是埋雷？
**答案**：省事（一个 fuzzer 覆盖整族入口、共享变异语料）；潜在风险是若其中某个函数对某些输入本就与系统 libc 有「已知的、被允许的」差异（如对溢出的处理），会被误报为 bug，因此差分断言要贴合 C 标准允许的差异范围。

---

### 4.2 微基准框架

#### 4.2.1 概念说明

内存函数是「低延迟代码」——拷贝几十字节可能只花几个 CPU 周期。直接测一次的误差（中断、上下文切换、读时钟本身的耗时）可能比被测量还大。`benchmarks/LibcBenchmark.h` 里的 `benchmark()` 函数模板用一套**自适应重复测量**算法解决精度问题：把函数重复跑很多次凑够总时长，再用「按迭代次数加权」的均值逼近单次耗时，直到估计值收敛。

`RATIONALE.md` 解释了为何不直接用硬件性能计数器（cycle 计数）：它跨微架构语义不一致、依赖内核系统调用本身就很贵、在现代乱序超标量处理器上「测两次同一段代码结果都不同」。结论是改用更通用的时钟，靠**大量重复**把信噪比拉起来。

#### 4.2.2 核心流程

`benchmark()` 的自适应循环（伪代码）：

```
iterations = initial_iterations   # 例如 1
循环:
    batch   = 生成 iterations 个随机参数            # 随机化防分支预测器作弊
    start   = 计时()
    对 batch 中每个参数: production = foo(参数); 阻止优化(production)
    elapsed = 计时() - start
    用 {iterations, elapsed} 更新「单次耗时加权估计」best_guess
    change_ratio = |best_guess 相对上次估计的变化|
    若 (总时长 ≥ min_duration 且 样本数 ≥ min_samples 且 change_ratio < epsilon):
        停机理由 = PrecisionReached
    否则若 样本数/总时长/迭代数 任一达上限: 对应停机
    若已停机: 返回
    iterations *= scaling_factor   # 几何增长，例如 ×1.4
```

关键设计：

- **按迭代次数加权**：第 \(k\) 个样本提供了 \(N_k\) 次观测，于是把所有样本的 `total_time / total_iterations` 作为整体估计，长样本天然权重更高。
- **几何增长迭代数**：每次把批量大小乘以 `scaling_factor`（默认 1.4），既快速能跑够总时长、又能在前期用小批量快速试错。
- **多种停机条件**：精度达标（`PrecisionReached`）、超时（`MaxDurationReached`）、迭代数封顶（`MaxIterationsReached`）、样本数封顶（`MaxSamplesReached`）。

#### 4.2.3 源码精读

**停机条件全部外化在 `BenchmarkOptions`。** [`benchmarks/LibcBenchmark.h:58-78`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcBenchmark.h) 把 `min_duration`/`max_duration`/`initial_iterations`/`max_iterations`/`min_samples`/`max_samples`/`epsilon`/`scaling_factor` 都做成可配置字段，默认 `epsilon=0.01`（1% 精度）、`scaling_factor=1.4`、`max_duration=10s`。

```cpp
struct BenchmarkOptions {
  Duration min_duration = std::chrono::seconds(0);
  Duration max_duration = std::chrono::seconds(10);
  uint32_t initial_iterations = 1;
  uint32_t max_iterations = 10000000;
  uint32_t min_samples = 4;
  uint32_t max_samples = 1000;
  double epsilon = 0.01;        // 1% 默认精度
  double scaling_factor = 1.4;  // 迭代数几何增长率
  // ...
};
```

**加权均值由 `RefinableRuntimeEstimation` 维护。** [`benchmarks/LibcBenchmark.h:140-154`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcBenchmark.h) 累加每个样本的耗时与迭代数，返回累计均值，这正是「按迭代次数加权」的实现。

```cpp
Duration update(const Measurement &m) {
  total_time += m.elapsed;
  total_iterations += m.iterations;
  return total_time / total_iterations;
}
```

**主循环 `benchmark()` 模板。** [`benchmarks/LibcBenchmark.h:202-270`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcBenchmark.h) 是框架核心：它接收 `ParameterProvider`（生成随机参数批量）、`Function foo`（被测函数）、`Clock`（计时器）。注意 222-225 行用 `benchmark::DoNotOptimize(production)` 阻止编译器把无副作用的 `foo` 调用优化掉——这是借用 Google Benchmark 提供的屏障。

```cpp
const auto start_time = Clock.now();
for (const auto parameter : batch) {
  auto production = foo(parameter);
  benchmark::DoNotOptimize(production);
}
const auto end_time = Clock.now();
```

**构建期为每个内存函数派生一个 benchmark 可执行体。** [`benchmarks/CMakeLists.txt:208-236`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/CMakeLists.txt) 的 `add_libc_multi_impl_benchmark` 遍历某函数的所有已注册实现，对当前 CPU 能跑的每一个都生成 `libc.src.string.<func>_benchmark` 可执行体，并注入宏 `LIBC_BENCHMARK_FUNCTION_MEMCPY=LIBC_NAMESPACE::memcpy` 让同一份 `LibcMemoryBenchmarkMain.cpp` 适配不同函数。

```cmake
add_libc_multi_impl_benchmark(memcpy)
add_libc_multi_impl_benchmark(memset)
# ... memmove / memcmp / bcmp / bzero
```

> **小贴士**：框架复用了 Google Benchmark 的频率检测、缓存层级探查与 `DoNotOptimize`，但**没有**用它的 `BENCHMARK` 宏注册机制——`RATIONALE.md` 的 FAQ 解释了原因：内存基准的参数是「框架参数」与「被测函数参数」的混合，用宏静态注册会很笨重，故改用命令行参数驱动（见 4.4）。

#### 4.2.4 代码实践

> **实践目标**：读懂停机条件，预测它在「极快函数」与「极慢函数」下的行为差异。

1. 打开 `LibcBenchmark.h`，对照 `benchmark()` 的停机分支（236-244 行）回答：对一个单次仅几纳秒的函数，最先触发的是哪种停机？对一个拷贝数 MB 的函数呢？
2. **预期结果**：极快函数靠 `PrecisionReached`（`change_ratio < epsilon`）停机；极慢函数因「总耗时不再随迭代数线性增长」，`epsilon` 难以达到，转而靠 `MaxDurationReached` 或 `MaxIterationsReached` 停机——这与 `RATIONALE.md` 第 138-142 行「大缓冲区退化为单样本 + 多次重复求置信区间」的说明一致。
3. 实际运行需先按 `README.md` 完成 `LIBC_INCLUDE_BENCHMARKS=Yes` 的构建，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`scaling_factor` 设成 1.0 会怎样？设成 1.4 呢？
**答案**：设成 1.0 时迭代数永不增长，框架会因「`iterations *= scaling_factor` 等幂」触发 `report_fatal_error`（见 262-266 行的保护）；1.4 是几何增长，能快速逼近目标总时长又保持平滑。

**练习 2**：为什么每个批量要先 `randomize()` 参数，而不是反复调用同一组参数？
**答案**：反复用同一参数会让 CPU 的分支预测器完美命中，测出「理想化」的不真实结果（见 `RATIONALE.md`「Effect of branch prediction」）。随机化参数使分支模式不可预测，测量更接近真实负载。

---

### 4.3 内存大小分布

#### 4.3.1 概念说明

知道「测一个尺寸」还不够——内存函数在真实程序里被调用的**尺寸分布**极度偏向小尺寸。`README.md` 给出 Google 生产环境的统计：`memcpy` 有 96% 的调用尺寸 ≤128 字节、99% ≤1024 字节。如果基准只测几个固定大尺寸，会严重高估真实开销。

为此框架引入 **`MemorySizeDistribution`**：一个「尺寸 → 出现概率」的数组，从真实二进制（服务器、数据库、日志、存储等负载）里采样得到。基准时按这个分布**随机抽样**尺寸，得到的是「该负载下的平均每次调用耗时」，既代表性好、又可复现。

这与 4.2 的「随机化参数防分支预测」是同一机制的两个目的：随机化既防作弊、又能按指定分布采样。

#### 4.3.2 核心流程

内存基准有两种测法，对应两个模式：

- **随机/分布模式（Stochastic，默认）**：按选定的 `MemorySizeDistribution` 抽样尺寸，随机化缓冲区偏移，测出「该分布下的平均每次耗时」。最贴近真实负载。
- **扫描模式（Sweep）**：从 0 到 `--sweep-max-size` 逐个尺寸测一遍，得到「单尺寸耗时曲线」，便于看清实现的优势/劣势区间；缺点是同尺寸反复调用，分支预测器会介入，结果偏理想。

二者在 [`LibcMemoryBenchmarkMain.cpp`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmarkMain.cpp) 里分别由 `MemfunctionBenchmarkDistribution` 与 `MemfunctionBenchmarkSweep` 实现。

#### 4.3.3 源码精读

**分布就是一个概率数组。** [`benchmarks/MemorySizeDistributions.h:27-42`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/MemorySizeDistributions.h) 定义了 POD 结构 `MemorySizeDistribution`（名字 + 概率数组），并按函数族提供 `getMemcpySizeDistributions()` 等接口。`getDistributionOrDie` 按名字查表，找不到就报错——这正是命令行 `--size-distribution-name` 背后的查表逻辑。

```cpp
struct MemorySizeDistribution {
  StringRef name;                 // 如 "memcpy Google A"
  ArrayRef<double> probabilities; // 下标即尺寸，值即概率
};
```

**真实负载数据存成 CSV。** `benchmarks/distributions/` 下每个 `MemcpyGoogleA.csv` 等就是一行逗号分隔的概率值（下标 = 尺寸）。[`benchmarks/distributions/README.md`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/distributions/README.md) 说明这些负载对应 automemcpy 论文里的真实工作负载（`GoogleA`↔service 4、`GoogleB`↔database 1……），另有合成的 `Uniform384To4096`。把大数据外置成文件是为了不撑爆编辑器，也方便其他工具分析。

**`StudyConfiguration` 把「测什么」全部参数化。** [`benchmarks/LibcMemoryBenchmark.h:33-70`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmark.h) 定义了一次「研究（Study）」的配置：测哪个函数、试多少次、是否扫描模式、扫描上限、用哪个分布、缓冲区对齐方式、`memcmp` 的失配位置。

```cpp
struct StudyConfiguration {
  std::string function;        // 'memcpy' / 'memset' / 'memcmp' ...
  uint32_t num_trials = 1;     // 重复次数，供分析工具算置信区间
  bool is_sweep_mode = false;
  uint32_t sweep_mode_max_size = 0;
  std::string size_distribution_name;
  MaybeAlign access_alignment = std::nullopt;  // 缓冲区对齐/随机
  uint32_t memcmp_mismatch_at = 0;
};
```

**参数批量与各函数的 Setup。** [`benchmarks/LibcMemoryBenchmark.h:182-214`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmark.h) 定义了「一次调用参数」`ParameterType`（16 位偏移 + 16 位尺寸），并保证批量数据全部塞进 L1 缓存（这是 `RATIONALE.md`「数据留在 L1」决策的落地）。`CopySetup` 持有源/目缓冲区，`call()` 把参数翻译成一次 `memcpy(dst+offset, src+offset, size)`。

```cpp
struct ParameterBatch {
  struct ParameterType {
    unsigned offset_bytes : 16; // max : 16 KiB - 1
    unsigned size_bytes : 16;   // max : 16 KiB - 1
  };
  // ...
};

struct CopySetup : public ParameterBatch {
  inline void *call(ParameterType parameter, MemcpyFunction memcpy_func) {
    return memcpy_func(dst_buffer + parameter.offset_bytes,
                       src_buffer + parameter.offset_bytes,
                       parameter.size_bytes);
  }
};
```

**分布模式用 `std::discrete_distribution` 按概率抽样尺寸。** [`benchmarks/LibcMemoryBenchmarkMain.cpp:208-243`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmarkMain.cpp) 的 `MemfunctionBenchmarkDistribution` 把概率数组喂给 `std::discrete_distribution`，每次 `randomize()` 给批量里每个参数抽一个尺寸和一个随机偏移，再交给 4.2 的 `benchmark()` 模板测量。

```cpp
MemfunctionBenchmarkDistribution(MemorySizeDistribution distribution_arg)
    : distribution(distribution_arg),
      probabilities(distribution_arg.probabilities),
      size_sampler(probabilities.begin(), probabilities.end()),
      offset_sampler(...) {}
```

#### 4.3.4 代码实践

> **实践目标**：用真实分布理解「为什么测小尺寸更重要」。

1. 打开 `benchmarks/distributions/MemcpyGoogleA.csv`，观察前若干个概率值（对应尺寸 0、1、2、…）。注意尺寸 1 的概率约 0.066，远高于尾部尺寸的 ~1.7e-5。
2. 估算：尺寸 ≤16 的概率之和占总概率的多少？（求和前 17 个值）
3. **预期结果**：前十几个尺寸的概率合计远超 50%，印证「绝大多数调用都是小尺寸」。
4. 据此说明：如果实现把 1–3 字节的快路径写得很差，即使大尺寸拷贝再快，真实负载下的平均耗时也会很差。
5. 精确求和可用任意脚本辅助，但分布数据本身已在源码中可见；实际构建/运行**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：随机模式与扫描模式分别适合什么场景？
**答案**：随机模式按真实分布抽样、且随机化防分支预测，适合评估「真实负载下的平均性能」，是日常对比的首选；扫描模式逐尺寸测量、能看清实现在不同尺寸区间的强弱（如某实现在小尺寸领先、大尺寸落后），但因同尺寸反复调用、分支预测器介入，结果偏理想。

**练习 2**：为什么所有批量数据要被框在 L1 缓存里？
**答案**：访问 L2/RAM 的代价远大于小尺寸拷贝本身，若不限制，测到的将是「内存子系统延迟」而非「函数实现性能」，破坏了可比性与可复现性（见 `RATIONALE.md`「Effect of the memory subsystem」）。

---

### 4.4 结果分析

#### 4.4.1 概念说明

测完只是第一步，还得把结果**存下来、能对比、能可视化**。框架的做法是：每次「研究」的配置与测量值序列化成一个 JSON 文件（`Study` 结构），再用独立的 Python 脚本 `libc-benchmark-analysis.py3` 把一个或多个 JSON 渲染成图。把「测量」与「分析」解耦，使得同一份数据能用不同方式复盘，也便于把多次运行结果并排比较。

之所以另外写分析脚本而不用 Google Benchmark 的内置报告器，是因为内存基准的关键参数（分布名、扫描范围、对齐方式）需要随图一起展示，自定义报告器更顺手（`RATIONALE.md` FAQ）。

#### 4.4.2 核心流程

1. **运行** benchmark 可执行体，命令行指定 `--study-name`、`--size-distribution-name`（或 `--sweep-mode`）、`--num-trials`、`--output`。
2. **序列化**：`writeStudy()` 把 `Study`（含主机信息、配置、测量值）写成 JSON。
3. **分析**：`python3 libc-benchmark-analysis.py3 a.json b.json ...` 读取多个 JSON，用 pandas 整理、seaborn/matplotlib 画图。
4. **解读**：扫描模式多 trial 时显示 95% 置信区间；同机多报告自动并排对比；Y 轴单位可用 `--mode` 切换为 `time`/`cycles`/`bytespercycle`。

#### 4.4.3 源码精读

**命令行参数驱动一切。** [`benchmarks/LibcMemoryBenchmarkMain.cpp:39-74`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmarkMain.cpp) 用 LLVM 的 `cl::opt` 定义 `--study-name`（必填）、`--size-distribution-name`、`--sweep-mode`/`--sweep-min-size`/`--sweep-max-size`、`--aligned-access`、`--num-trials`、`--output` 等开关，把 4.3 提到的所有参数都暴露到命令行。

**`main()` 做参数校验并分派两种模式。** [`benchmarks/LibcMemoryBenchmarkMain.cpp:258-276`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmarkMain.cpp) 先 `checkRequirements()`（确认 Release 构建、CPU 在 performance 调频），校验 `--aligned-access` 是 2 的幂、扫描与分布互斥，然后按 `--sweep-mode` 二选一构造 `MemfunctionBenchmarkSweep` 或 `MemfunctionBenchmarkDistribution`，运行后写 JSON。

```cpp
if (SweepMode)
  Benchmark.reset(new MemfunctionBenchmarkSweep());
else
  Benchmark.reset(new MemfunctionBenchmarkDistribution(
      getDistributionOrDie(BenchmarkSetup::get_distributions(),
                           SizeDistributionName)));
writeStudy(Benchmark->run());
```

**`writeStudy()` 把结果写进 JSON。** [`benchmarks/LibcMemoryBenchmarkMain.cpp:245-256`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/LibcMemoryBenchmarkMain.cpp) 用 `json::OStream` 把 `Study` 序列化到 `--output` 指定文件（默认标准输出），这就是下游 Python 脚本的输入格式。

**分析脚本读 JSON 画图。** [`benchmarks/libc-benchmark-analysis.py3:1-10`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmark-analysis.py3) 的文档串说明了用法：依赖 `matplotlib`/`pandas`/`seaborn`，传入一个或多个 JSON 文件即可。脚本内部（如 `displaySweepData`）用 pandas 把扫描模式的扁平测量数组重整成 `(size, trial)` 多级索引，从而能算每个尺寸的均值与置信区间。`README.md` 进一步说明：同机多报告自动并排、Y 轴单位可切换。

```python
"""Reads JSON files produced by the benchmarking framework and renders them.
Run:
> python3 libc/benchmarks/libc-benchmark-analysis.py3 <files>
"""
```

> **小贴士**：`--num-trials` 的意义在分析阶段才完全显现——多次重复让脚本能为扫描模式画出 95% 置信区间，告诉你「两条曲线的差异到底是真信号还是噪声」。

#### 4.4.4 代码实践

> **实践目标**：跑通「测量 → JSON → 分析图」的完整链路（或至少读懂它）。

1. 按 [`benchmarks/README.md:13-17`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/README.md) 配置 `-DLIBC_INCLUDE_BENCHMARKS=Yes` 构建。
2. 运行随机模式（命令见 `README.md:41-46`），把结果写到 `/tmp/benchmark_result.json`。
3. 再跑一次扫描模式（`README.md:69-74`），加 `--num-trials=30` 以便分析工具算置信区间。
4. 用 `python3 libc/benchmarks/libc-benchmark-analysis.py3 /tmp/*.json` 渲染。
5. **预期结果**：得到两张图——随机模式给出该负载下的平均每次耗时；扫描模式给出「耗时 vs 尺寸」曲线，多 trial 时带置信区间。
6. 该流程依赖 Google Benchmark 子模块、Python 依赖与 `performance` 调频模式，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `LibcMemoryBenchmarkMain.cpp` 末尾有 `#error For reproducibility benchmarks should not be compiled in DEBUG mode.`？
**答案**：Debug 构建（`NDEBUG` 未定义）下既没有优化（测到的不是真实性能），又可能含昂贵断言，结果不可复现、也没有代表性；强制 Release 才能保证基准数据有可比性。

**练习 2**：Y 轴 `--mode cycles` 与 `bytespercycle` 各在什么时候更有用？
**答案**：`cycles` 用 CPU 频率把时间换算成周期数，便于跨不同频率的机器横向理解；`bytespercycle`（仅扫描模式）把耗时除以尺寸，直观反映「吞吐」，特别适合比较大尺寸区间的实现优劣。

---

## 5. 综合实践

> **任务**：为 `memcpy` 设计一个简单的微基准方案，测量若干典型尺寸（1/16/64/4096 字节）的耗时，并说明如何用大小分布提升代表性。把本讲四个模块串起来。

### 步骤一：选模式——为什么这两种尺寸要用不同模式

- **1/16/64 字节**：属于小尺寸，正是 `RATIONALE.md` 强调的「低延迟、数据留 L1」区间。用**随机模式**最合适：随机化偏移防分支预测、数据留 L1 保证测的是函数本身。
- **4096 字节**：已接近/超过 L1 一个页面，随机模式的「数据留 L1」前提开始动摇；可改用**扫描模式**单独看这个尺寸点，或用 `Uniform384To4096` 分布在随机模式下评估该区间。

### 步骤二：随机模式下用分布覆盖这些尺寸

直接构造一个「只在 {1,16,64,4096} 上均匀采样」的自定义分布并非框架的推荐用法——框架的优势正是用**真实负载分布**。正确做法是：

1. 用 `--size-distribution-name="memcpy Google A"`（service 负载）跑随机模式，得到该负载下的平均每次耗时。
2. 对照 [`benchmarks/distributions/README.md`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/distributions/README.md) 的负载映射，再选 `Google B`（database）等代表不同场景的分布各跑一次。
3. 因为这些真实分布天然把概率大量集中在你关心的 1/16/64 字节上（见 4.3.4），所以测出的平均耗时**自动被小尺寸主导**，代表性远胜于「只测几个固定尺寸」。

### 步骤三：用扫描模式精确定位单尺寸

如果想精确知道「1 字节到底花几个周期」「64 与 65 字节之间是否有阶跃」（对应 u5-l2 里 `head_tail` 构建块的尺寸分派边界），用扫描模式：

```shell
libc.src.string.memcpy_benchmark \
    --study-name="memcpy sweep" \
    --sweep-mode --sweep-max-size=128 \
    --num-trials=30 \
    --output=/tmp/sweep.json
```

### 步骤四：分析与解读

1. 用 `libc-benchmark-analysis.py3` 渲染随机模式的多个分布结果（并排对比不同负载）。
2. 渲染扫描模式结果，观察 64→65 字节附近是否有耗时跳变——若有，对应 u5-l2 里从 `block`/`head_tail` 切换到更大构建块的尺寸分派点。
3. **预期结论**：随机模式告诉你「真实负载下的平均性能」（受小尺寸主导），扫描模式告诉你「实现的结构性优劣区间」，两者互补。

### 步骤五：用模糊测试兜底正确性

性能测完，别忘了正确性。改动 `memcpy` 实现后，跑 `fuzzing/string/` 下相关 fuzzer 确认差分比对不再 trap。性能（基准）与正确性（模糊）共同构成对内存函数改动的完整验收。

> 说明：以上命令的精确输出、数值与图表**待本地验证**——它们依赖具体 CPU、调频模式与构建环境。本实践的重点是掌握「模式选择 → 分布抽样 → 扫描定位 → 分析解读 → 模糊兜底」的方法链路。

## 6. 本讲小结

- **模糊测试镜像 `src/`**：`fuzzing/` 与 `src/` 同构，每个 fuzzer 实现 `LLVMFuzzerTestOneInput`，以**差分测试**（与手写参考或系统 libc 比对）为主，由 `-fsanitize=fuzzer` 提供插桩与驱动，`add_libc_fuzzer` 把源文件链上入口点 object 文件。
- **微基准靠重复取均值**：`benchmark()` 用「迭代数几何增长的批量重测 + 按迭代数加权均值 + ε 收敛」算法，把几周期的函数测到 1% 精度，避开硬件性能计数器的跨架构不一致问题。
- **真实尺寸分布驱动随机化**：`MemorySizeDistribution` 是从 Google 生产负载采样得到的「尺寸 → 概率」数组；随机模式按它抽样得到代表性平均耗时，扫描模式逐尺寸画出性能曲线，二者互补。
- **测量与分析解耦**：结果序列化为 `Study` JSON，由独立 Python 脚本渲染成图，支持多 trial 置信区间、多报告并排、多种 Y 轴单位。
- **防作弊三件套**：随机化参数（防分支预测）、数据留 L1（隔离内存子系统）、强制 Release + performance 调频（保证可复现）。
- **正确性与性能成对验收**：改完内存函数，用基准看性能、用 fuzzer 守正确性，缺一不可。

## 7. 下一步学习建议

- **动手扩展一个 fuzzer**：参考 `fuzzing/math/SingleInputSingleOutputDiff.h` 的模板，为某个还没有 fuzzer 的简单函数（如某个 ctype 辅助）写一个差分模糊，巩固 4.1。
- **深入 automemcpy 思路**：阅读 `benchmarks/distributions/README.md` 提到的 automemcpy 论文，理解这些分布如何被用来**自动生成**优化的内存函数实现——这是把「基准 + 分布」从「评估」推向「自动调参」的进阶方向。
- **回头看 mem\* 实现的尺寸分派**：用本讲的扫描模式去验证 u5-l2 里 `block`/`head_tail`/`loop_and_tail` 的尺寸分派点，把「源码里的分派逻辑」与「实测曲线上的跳变」对上号。
- **衔接贡献流程**：当你准备按 u11-l3 贡献一个新函数时，记得同时补上单元测试（u10-l1）、（若涉及数学）MPFR 对照（u10-l2）与本讲的模糊/基准测试，让一次贡献在正确性与性能两个维度都站得住。
