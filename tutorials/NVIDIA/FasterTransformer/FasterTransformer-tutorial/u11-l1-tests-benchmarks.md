# 测试与基准：tests 与 benchmarks

> 本讲属于「工程实践与扩展」单元（u11），依赖 u3-l2（注意力 kernel）与 u8-l3（Sampling 层）。我们将从「写完 kernel/layer 之后，FT 如何保证它没改坏」这个问题出发，拆解仓库里两套相互独立的验证机制：`tests/` 下的单元测试（验证**正确性**）与 `benchmarks/` 下的脚本（测量**性能**）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂 `tests/` 目录的 CMake 组织，知道哪些测试被打进哪个可执行文件，以及哪些文件其实**没有**被编译。
2. 区分 FT 单元测试的三种写法：真正的 GoogleTest（`TEST`/`TYPED_TEST`）、手写「假 gtest」宏、以及带独立 `main()` 的批处理测试，并能说出它们的取舍。
3. 掌握 FT 验证 kernel 正确性的通用套路：**GPU kernel 输出 vs. CPU 参考实现 + 几乎相等比较（`almostEqual`/`checkResult`）**，并理解容差（atol/rtol）与「允许 1% 元素不匹配」的设计。
4. 读懂 `benchmarks/bert/` 下 `pyt_benchmark.sh` 与 `tf_benchmark.sh` 的循环结构，能列出运行 `pyt_benchmark.sh` 的全部前置条件。
5. 当一个 kernel 出现回归时，知道该去哪个测试文件定位、如何复现。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l2（注意力 kernel）**：知道 `invokeMaskedSoftmax`、`decoder_masked_multihead_attention` 这类 `invokeXxx` kernel 的两层结构（`__global__` 设备函数 + host 启动函数）。
- **u8-l3（Sampling 层）**：知道 `DynamicDecodeLayer`、Top-K/Top-P sampling 的统一 forward 接口与 `runtime_top_k`/`runtime_top_p` 等运行期参数。
- **u2-l1（Tensor）**：知道 `Tensor` 是非拥有的描述符、`TensorMap` 按名字索引、`MEMORY_CPU`/`MEMORY_GPU` 标记数据位置。
- **u2-l2（Allocator）**：知道 `Allocator<AllocatorType::CUDA>` 与 `deviceMalloc`/`cudaH2Dcpy` 的用法。

几个本讲会用到的术语：

- **gtest（GoogleTest）**：C++ 主流单元测试框架。核心宏是 `TEST(测试套件名, 用例名)`、`TYPED_TEST`（模板参数化用例）、`EXPECT_TRUE`/`EXPECT_EQ`/`EXPECT_THROW`（断言）。`gtest_main` 提供 `main()`，自动发现并运行所有用例。
- **fixture（测试夹具）**：继承 `testing::Test` 的类，用 `SetUp()`/`TearDown()` 在每个用例前后做环境准备/清理。
- **参考实现（reference / oracle）**：一个被信任的、通常是 CPU 单线程朴素版本的正确结果，用来跟 GPU kernel 的输出做对比。
- **容差比较（almostEqual）**：浮点数不能直接 `==`，而要判 \(\lvert a-b\rvert \le \text{atol}+\text{rtol}\cdot\lvert b\rvert\)（同 NumPy 的 `isclose`）。
- **回归（regression）**：改动后某个原本正确的功能变错了。

## 3. 本讲源码地图

本讲涉及的文件分两组。**测试构建与用例**：

| 文件 | 作用 |
| --- | --- |
| [tests/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/CMakeLists.txt) | 顶层测试入口，`add_subdirectory(unittests)` 并按需加入量化相关测试。 |
| [tests/unittests/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/CMakeLists.txt) | 定义 `unittest`、`test_gemm`、`test_gpt_kernels`、`test_activation`、`test_context_decoder_layer` 五个可执行文件，并 `FetchContent` 拉取 GoogleTest 1.12.1。 |
| [tests/unittests/gtest_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/gtest_utils.h) | 真正 gtest 用的公共头：`FtTestBase` 夹具、`createTensor`/`toHost`/`toDevice`、`almostEqual`/`checkResult`、`initRandom`。 |
| [tests/unittests/unittest_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/unittest_utils.h) | 手写「假 gtest」公共头：自定义 `EXPECT_TRUE`（抛异常）、另一份 `checkResult`、`initRandom`、`tile`。 |
| [tests/unittests/test_tensor.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_tensor.cu) | 纯 gtest 用例，验证 `TensorMap`/`Tensor` 接口（最小、最适合作为 gtest 入门样例）。 |
| [tests/unittests/test_attention_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu) | gtest `TYPED_TEST`，验证 `invokeMaskedSoftmax` 等注意力 kernel，内含 CPU 参考与 benchmark 用例。 |
| [tests/unittests/test_sampling.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu) | 独立 `main()`，验证 `DynamicDecodeLayer` 的采样/累积对数概率（构造 logits、跑解码、对比 CPU 参考）。 |
| [tests/unittests/test_gemm.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_gemm.cu) | 独立 `main()`，验证 `cublasMMWrapper` 各 GEMM 接口（参考实现本身是 `cublasGemmEx`）。 |

**基准脚本**：

| 文件 | 作用 |
| --- | --- |
| [benchmarks/bert/pyt_benchmark.sh](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh) | PyTorch 端 BERT 基准，遍历 batch×seq×精度，比较 TorchScript / FT / EFF-FT 的延迟与加速比。 |
| [benchmarks/bert/tf_benchmark.sh](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/tf_benchmark.sh) | TensorFlow 端 BERT 基准，结构与 PyTorch 版类似，用 `$FT_REPO_PATH` 定位仓库。 |

## 4. 核心概念与源码讲解

### 4.1 测试与基准的分工：构建组织与三类测试风格

#### 4.1.1 概念说明

FT 是一个 GPU 推理库，「改对没改对」和「快没快」是两个独立问题，仓库用两套独立工具回答：

- `tests/`：单元测试，回答**正确性**。每个测试构造一组输入 → 调被测 kernel/layer → 拿 GPU 输出与「参考结果」比对 → 不匹配就失败。适合在 CI 上每次提交跑一遍，定位回归。
- `benchmarks/`：基准脚本，回答**性能**。每个脚本遍历若干 (batch, seq, 精度) 组合 → 调端到端 example（如 `bert_example.py`）测延迟 → 算出相对原生框架（TorchScript / TF）的加速比，输出 Markdown 表格。

一个常见的误解是「基准脚本也算测试」。它们**不验证数值正确性**，只测时间；如果担心结果对，应该去跑对应的 `tests/`，而不是 `benchmarks/`。

#### 4.1.2 核心流程

测试构建的组装流程（自顶向下）：

1. 顶层 `CMakeLists.txt` 在 [CMakeLists.txt:305](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L305) 处 `add_subdirectory(tests)`。
2. [tests/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/CMakeLists.txt) 无条件加入 `unittests`，并在 `BUILD_PYT` 开启时额外加入 `gemm_dequantize`、`moe`、`int8_gemm`（CUTLASS/量化专属测试）。
3. [tests/unittests/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/CMakeLists.txt) 定义具体可执行文件，并通过 `FetchContent` 拉取 GoogleTest。

读完这一层会发现 FT 单元测试其实有**三种风格**，混在同一目录里：

| 风格 | 代表文件 | 是否用真 gtest | 入口 | 打进的目标 |
| --- | --- | --- | --- | --- |
| 真 gtest（fixture + TYPED_TEST） | `test_attention_kernels.cu`、`test_tensor.cu` | 是 | `gtest_main` 自动 | `unittest` |
| 手写「假 gtest」宏 | `test_gemm.cu`（及经 `unittest_utils.h`） | 否（自定义 `EXPECT_TRUE` 抛异常） | 自己的 `main()` | `test_gemm` 等独立 exe |
| 独立 `main()` 批处理 | `test_sampling.cu`、`test_gpt_kernels.cu` | 否 | 自己的 `main()` 遍历 `vector<TestCase>` | `test_sampling.cu` **未被任何目标编译**（见下） |

#### 4.1.3 源码精读

测试总入口按 `BUILD_PYT` 条件化（[tests/CMakeLists.txt:15-20](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/CMakeLists.txt#L15-L20)）——说明 CUTLASS/MoE/int8 的测试是「PyTorch 构建专属」，因为它们依赖 `gemm_dequantize` 等 PyTorch 路径才编译进来的库。

GoogleTest 是用 `FetchContent` 在配置期联网拉取的（[tests/unittests/CMakeLists.txt:17-27](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/CMakeLists.txt#L17-L27)），固定 tag `release-1.12.1`。这意味着**首次配置测试需要联网**，且 gtest 源码不在本仓库里。

`unittest` 这个可执行文件聚合了 6 个真 gtest 源文件，并链接 `gtest_main` 提供自动测试发现（[tests/unittests/CMakeLists.txt:29-39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/CMakeLists.txt#L29-L39)）：

```cmake
add_executable(unittest
    test_attention_kernels.cu
    test_logprob_kernels.cu
    test_penalty_kernels.cu
    test_sampling_kernels.cu
    test_sampling_layer.cu
    test_tensor.cu)
target_link_libraries(unittest PUBLIC "${TORCH_LIBRARIES}" gtest_main)
```

其余四个文件各自独立成可执行文件、各有自己的 `main()`（[tests/unittests/CMakeLists.txt:67-82](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/CMakeLists.txt#L67-L82)）：`test_gemm`、`test_gpt_kernels`、`test_activation`、`test_context_decoder_layer`。

> **一个容易被坑的事实**：`test_sampling.cu` **没有出现在任何 `add_executable` 里**——它有完整的独立 `main()`（见 [test_sampling.cu:1472](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L1472)），但 CMake 不编译它。真正被打进 `unittest` 的采样测试是 `test_sampling_kernels.cu` 与 `test_sampling_layer.cu`。所以如果你想「跑 sampling 的端到端测试」，直接 `./test_sampling` 是跑不起来的——要么手动把它加进构建，要么跑 `unittest` 里的 `*Sampling*` 用例。本讲 4.3 节仍会把它当作「如何写采样参考对比」的优秀样例来精读。

#### 4.1.4 代码实践

1. **实践目标**：在本地构建产物里确认哪些测试可执行文件存在。
2. **操作步骤**：在仓库根目录按 u1-l2 的方式编译（`cmake -DSM=xx .. && make -j`），构建完成后进入 `build/`，列出可执行文件。
   ```bash
   ls build/bin/ | grep -E 'test|gemm'
   ```
3. **需要观察的现象**：应能看到 `unittest`、`test_gemm`、`test_gpt_kernels`、`test_activation`、`test_context_decoder_layer`、以及工具 `bert_gemm`，但**不会**有 `test_sampling`。
4. **预期结果**：`test_sampling` 缺席印证了 4.1.3 的结论。如果待本地验证（环境无 GPU/未装依赖），也可以直接对照 [tests/unittests/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/CMakeLists.txt) 文本确认：它的源文件清单里没有 `test_sampling.cu`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gemm_dequantize`、`moe`、`int8_gemm` 三个测试目录要被 `if(BUILD_PYT)` 守卫，而 `unittests` 不用？

**答案**：`unittests` 测的是与框架外壳无关的核心 kernel/layer（attention、sampling、gemm、tensor），这些库在任何构建配置下都存在；而 `gemm_dequantize` 等测的是 CUTLASS weight-only/int8/MoE 的混合精度 GEMM，这些库只在 `BUILD_PYT=ON`（或相应 CUTLASS 选项）时才编译进 `transformer-shared`，关掉就没有符号可链接，所以必须守卫。

**练习 2**：`unittest` 链接了 `gtest_main`，而 `test_gemm` 没有。这带来什么运行行为差异？

**答案**：`gtest_main` 提供标准 `main()`，会自动注册并按gtest机制发现/运行所有 `TEST`/`TYPED_TEST` 用例，可用 `--gtest_filter` 过滤；`test_gemm` 没有 `gtest_main`，靠自己的 `main()`（[test_gemm.cu:853](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_gemm.cu#L853)）手动遍历 `testcases`，无法用 gtest 过滤参数，跑就是全跑。

---

### 4.2 单元测试骨架：gtest fixture 与 TYPED_TEST

#### 4.2.1 概念说明

`test_tensor.cu` 和 `test_attention_kernels.cu` 是「真 gtest」的代表。它们解决两类问题：

- **接口契约测试**（`test_tensor.cu`）：不碰 GPU，只验证 `TensorMap`/`Tensor` 的纯逻辑——插入重复 key 抛异常、空张量取 min 抛异常、slice 越界抛异常等。这类测试快、无副作用，是 TDD（测试驱动）里最常见的形态。
- **GPU kernel 正确性测试**（`test_attention_kernels.cu`）：每个用例要在 GPU 上建 stream、分配显存、跑 kernel、回拷到 CPU 比较。这种「每个用例都要准备 GPU 环境」的需求，正是 **fixture（夹具）** 的用武之地。

`TYPED_TEST` 让同一个用例对多种数据类型（`float`、`half`，开启 BF16 时再加 `__nv_bfloat16`）各跑一遍，对应 FT 全库「模板实例化多精度」的现实。

#### 4.2.2 核心流程

一个 GPU 测试用例的标准生命周期（伪代码）：

```
FtTestBase::SetUp()              // 建 stream + Allocator<CUDA>
  └─ AttentionKernelTest::SetUp()// 子夹具再建 curand 随机数发生器
TYPED_TEST 体:
  runTestMaskedSoftmax(param):
    1. createTensor(...) 在 GPU 上分配 qk、attn_mask
    2. utils::normal(curng, qk)  // curand 灌随机数
    3. toHost(qk) -> h_qk        // 复制一份到 CPU 当参考输入
    4. invokeMaskedSoftmax(...)  // 被测 GPU kernel
    5. computeQkSoftmax(h_qk...) // CPU 参考实现，原地写 h_qk
    6. checkResult(gpu_qk, h_qk) // 几乎相等比较
AttentionKernelTest::TearDown() // 销毁 curand
FtTestBase::TearDown()           // 释放显存 + 销毁 stream
```

关键点：「参考输入」必须在调 GPU kernel **之前**就 `toHost` 复制一份，因为 `invokeMaskedSoftmax` 是原地覆写 `qk` 的（参考实现与被测 kernel 共用同一块缓冲，见 4.2.3）。

#### 4.2.3 源码精读

公共夹具 `FtTestBase`（[gtest_utils.h:168-249](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/gtest_utils.h#L168-L249)）统一了 GPU 环境：`SetUp` 建 stream 与 CUDA Allocator，`TearDown` 自动释放所有 CPU 缓冲并销毁 allocator/stream。它还提供 `createTensor`/`toHost`/`toDevice`/`copyTensor` 一组便利方法，让用例几乎不写裸 `cudaMalloc`/`cudaMemcpy`：

```cpp
class FtTestBase: public testing::Test {
public:
    void SetUp() override {
        int device = 0;
        cudaGetDevice(&device);
        cudaStreamCreate(&stream);
        allocator = new ft::Allocator<ft::AllocatorType::CUDA>(device);
        allocator->setStream(stream);
    }
    ...
    ft::Tensor createTensor(const ft::MemoryType mtype, const ft::DataType dtype,
                            const std::vector<size_t> shape) { /* 分配 + 包装成 Tensor */ }
    template<typename T> ft::Tensor toHost(ft::Tensor& device_tensor) { /* D2H 拷贝 */ }
    template<typename T> ft::Tensor toDevice(ft::Tensor& host_tensor) { /* H2D 拷贝 */ }
};
```

注意 `createTensor` 里 CPU 内存走 `malloc` 并登记进 `allocated_cpu_buffers_`（`TearDown` 统一 `free`），GPU 内存走 `allocator->malloc`（由 allocator 管理），呼应 u2-l2 的「Tensor 非拥有、allocator 才拥有显存」。

类型集合用 `testing::Types` 定义，并随 `ENABLE_BF16` 切换（[gtest_utils.h:160-166](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/gtest_utils.h#L160-L166)）：

```cpp
typedef testing::Types<float, half> FloatAndHalfTypes;
#ifndef ENABLE_BF16
typedef FloatAndHalfTypes SupportTypes;
#else
typedef testing::Types<float, half, __nv_bfloat16> FloatHalfBf16Types;
typedef FloatHalfBf16Types SupportTypes;
#endif
```

`test_tensor.cu` 是最干净的 gtest 入门样例。它直接 `#include <gtest/gtest.h>`（[test_tensor.cu:5](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_tensor.cu#L5)），用 `TEST(套件名, 用例名)` 写用例，断言用原生 `EXPECT_TRUE`/`EXPECT_THROW`。例如验证 `TensorMap` 拒绝重复 key（[test_tensor.cu:60-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_tensor.cu#L60-L69)）：

```cpp
TEST(TensorMapTest, InsertDoesNotAllowDuplicatedKey) {
    int* v1 = new int[4]{1, 10, 20, 30};
    Tensor t1 = Tensor(MEMORY_CPU, TYPE_INT32, {4}, v1);
    Tensor t2 = Tensor(MEMORY_CPU, TYPE_INT32, {2}, v1);
    TensorMap map({{"t1", t1}});
    EXPECT_TRUE(map.size() == 1);
    // forbid a duplicated key.
    EXPECT_THROW(map.insert("t1", t2), std::runtime_error);
    delete[] v1;
}
```

而 `TYPED_TEST` 版本（[test_tensor.cu:154-242](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_tensor.cu#L154-L242)）让 `MaxCorrectness`/`MinCorrectness`/`SliceCorrectness` 等对 `int8_t`、`int`、`float` 三种类型各跑一遍。

`test_attention_kernels.cu` 是 GPU 用例的代表。它的夹具 `AttentionKernelTest` 继承 `FtTestBase` 并加一个 `curand` 随机数发生器（[test_attention_kernels.cu:195-247](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L195-L247)）。参数结构 `AttentionKernelTestParam`（[test_attention_kernels.cu:34-46](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L34-L46)）打包 `batch_size/q_length/k_length/head_num/size_per_head` 等，让一个 `runTestMaskedSoftmax` 函数复用到多个用例。用例本身极简，只传参数（[test_attention_kernels.cu:451-453](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L451-L453)）：

```cpp
TYPED_TEST_SUITE(AttentionKernelTest, SupportTypes);

TYPED_TEST(AttentionKernelTest, MaskedSoftmax_NoPrompt) {
    this->runTestMaskedSoftmax({1, 12, 12, 1, 32, false, 0, false});
}
```

同一批用例里还有 `Benchmark_*` 前缀的变体（如 [test_attention_kernels.cu:484-497](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L484-L497)），它们把第二个参数 `is_benchmark=true` 传入，从而**跳过参考计算与比对**（见 4.3.3），只测 kernel 速度——这是「单元测试内嵌微基准」的写法。

#### 4.2.4 代码实践

1. **实践目标**：跑通 `unittest` 里 `AttentionKernelTest` 的用例，并用 gtest 过滤只跑 masked softmax。
2. **操作步骤**（需 GPU 环境）：
   ```bash
   cd build
   ./unittest --gtest_filter='AttentionKernelTest.MaskedSoftmax*'
   ```
3. **需要观察的现象**：终端应打印每个 `TYPED_TEST` 实例（`float` 与 `half` 各一组）的 `[ PASSED ]` 行；若开了 `FT_LOG_LEVEL=INFO`，还会看到 `check... ....OK : MaskedSoftmax (...)` 字样。
4. **预期结果**：全部用例 PASSED。若只想看 ALiBi 相关用例，换成 `--gtest_filter='AttentionKernelTest.Alibi*'`，体现 gtest 自动发现 + 过滤的价值——这是「手写 main」的 `test_gemm` 做不到的。

#### 4.2.5 小练习与答案

**练习 1**：`FtTestBase::TearDown` 注释说「不用管 GPU 缓冲，因为 allocator 管着它们」。可是 allocator 在 `TearDown` 里被 `delete` 了，那些 GPU 缓冲会泄漏吗？

**答案**：不会。`Allocator<AllocatorType::CUDA>` 的析构会释放它记账（`pointer_mapping_`，见 u2-l2）的所有显存；`createTensor` 分配的 GPU 指针都在 allocator 名下，所以 `delete allocator` 时一并回收。CPU 缓冲因为是 `malloc` 出来的、不归 allocator 管，所以要单独在 `allocated_cpu_buffers_` 里登记并手动 `free`。

**练习 2**：为什么 `computeQkSoftmax` 调用前要先 `Tensor h_qk = toHost<T>(qk)`，而不是在 kernel 跑完再 `toHost`？

**答案**：`invokeMaskedSoftmax` 把 softmax 结果**原地写回** `qk`（输入输出同 buffer，见 [test_attention_kernels.cu:295-296](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L295-L296)）。kernel 跑完后 `qk` 已经是「被测结果」，原输入丢了；CPU 参考需要原始 `QK^T` 分数，所以必须在调用 kernel **之前**先把原值 `toHost` 复制成 `h_qk`，再让 CPU 在 `h_qk` 上算参考、最后用 `qk`（GPU 结果）比对 `h_qk`（CPU 结果）。

---

### 4.3 正确性验证套路：CPU 参考 + 几乎相等比较

#### 4.3.1 概念说明

「断言 GPU 结果 == 期望」对浮点是行不通的。FT 的统一套路是 **三件套**：

1. **参考实现（reference）**：一个被信任的正确答案。理想是独立的 CPU 朴素实现；退一步也可以用「另一条等价路径」（如 cuBLAS 原生 API）。
2. **几乎相等比较**：`almostEqual(a, b) := |a - b| ≤ atol + rtol·|b|`，与 NumPy `isclose` 同义，同时容忍绝对误差（`atol`）和相对误差（`rtol`）。
3. **批量通过率**：允许极少数元素超出容差，FT 取「允许 1% 元素不匹配」，避免个别边界点的随机抖动让整批用例误报。

这条套路贯穿了 `test_attention_kernels.cu`（注意力 softmax）、`test_sampling.cu`（采样累积对数概率）和 `test_gemm.cu`（矩阵乘），只是各自的「参考」不同。

#### 4.3.2 核心流程

比较判定的数学表达：

\[ \text{almostEqual}(a, b) \;\Longleftrightarrow\; |a - b| \le \text{atol} + \text{rtol}\cdot|b| \]

其中 \(b\) 是参考值（注意公式是**非对称**的）。整个批量的通过条件：

\[ \text{passed} \;\Longleftrightarrow\; \text{failures} \le 0.01 \cdot \text{size} \]

容差按数据类型分档（来自 `checkResult` 的便捷重载）：

| 类型 | atol | rtol |
| --- | --- | --- |
| FP32（`sizeof(T)==4`） | \(10^{-4}\) | \(10^{-2}\) |
| FP16 / BF16 | \(10^{-3}\) | \(10^{-1}\) |

低精度放宽容差，是因为半精度本身舍入误差更大。

#### 4.3.3 源码精读

`almostEqual` 与容差判定的核心在 [gtest_utils.h:22-72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/gtest_utils.h#L22-L72)，要点是「允许 1% 元素不匹配」（[gtest_utils.h:65-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/gtest_utils.h#L65-L71)）：

```cpp
// Allow not matched up to 1% elements.
size_t tol_failures = (size_t)(0.01 * size);
if (failures > tol_failures) {
    FT_LOG_ERROR("%s (failures: %.2f%% ...)", name.c_str(), ...);
}
return failures <= tol_failures;
```

`test_attention_kernels.cu` 的 CPU 参考 `computeQkSoftmax` 是教科书式的 masked softmax 朴素实现（[test_attention_kernels.cu:133-193](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L133-L193)）：三遍循环分别求 max、求 sum（用 max 做数值稳定的 \(\exp\)）、再归一化，被 mask 屏蔽的位置直接置 0。被测 kernel 是 u3-l2 提到的 `invokeMaskedSoftmax`，二者在 `runTestMaskedSoftmax` 里被组装到一起并比对（[test_attention_kernels.cu:290-331](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L290-L331)）：

```cpp
invokeMaskedSoftmax(softmax_param, stream);      // 被测 GPU kernel
...
if (!is_benchmark) {
    computeQkSoftmax(h_qk.getPtr<T>(), h_qk.getPtr<T>(),
                     h_attn_mask.getPtr<T>(), (T*)nullptr,
                     batch_size, head_num, q_length, k_length, scale);  // CPU 参考
    bool passed = checkResult("MaskedSoftmax", qk.getPtr<T>(), h_qk.getPtr<T>(), qk.size());
    EXPECT_TRUE(passed);
}
```

`is_benchmark` 分支正是 4.2 提到的「微基准」开关：为真时既不 `toHost`、也算参考、也不比对，纯跑 kernel。

`test_sampling.cu`（虽未编译进目标，但写法最具教学价值）演示了**针对随机性结果**的验证思路——采样输出是随机的，不能直接比 token，于是改比「采样到某 token 的累积对数概率」。它在 CPU 上用 `computeLogProb` 算参考对数概率（[test_sampling.cu:73-89](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L73-L89)），构造 `logits`（[test_sampling.cu:154](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L154) 用 `initRandom` 灌随机数），把采样参数装进 `TensorMap`（[test_sampling.cu:184-190](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L184-L190)）：

```cpp
TensorMap input_tensors({{"random_seed", {MEMORY_CPU, TYPE_INT32, {1}, &seed}},
                         {"runtime_top_k", {MEMORY_CPU, TYPE_UINT32, {1}, &top_k}},
                         {"runtime_top_p", {MEMORY_CPU, TYPE_FP32, {1}, &top_p}},
                         {"temperature", ...}, {"len_penalty", ...},
                         {"repetition_penalty", ...}});
dynamic_decode_layer->setup(batch_size, beam_width, &input_tensors);
```

然后逐步推进解码，每步把「实际采样到的 token」对应的 CPU 对数概率累加成 `expected_cum_log_probs`，最后与 GPU 侧 `cum_log_probs` 比对（[test_sampling.cu:234-268](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L234-L268)）。这种「比概率不比 token」是验证随机采样正确性的标准技巧。其测试用例集在 `main()` 里用 `vector<TestCase>` 枚举 topk/topp/topk_topp 三类（[test_sampling.cu:1474-1492](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L1474-L1492)），并对 `float`/`half` 各跑一遍。

`test_gemm.cu` 的参考则是「退一步」的写法：它**没有**自己写朴素矩阵乘，而是用 cuBLAS 原生 API `cublasGemmEx` 当参考（[test_gemm.cu:141-183](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_gemm.cu#L141-L183)），再去比 FT 的 `cublasMMWrapper::Gemm`。这类测试严格说叫「**一致性测试**（consistency）」而非「正确性测试」——它只能证明 FT 封装与原生 cuBLAS 结果一致，不能独立证明 cuBLAS 本身对。`main()` 里函数名也印证了这点（[test_gemm.cu:869-883](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_gemm.cu#L869-L883)）有 `testGemmCorrectnessMatmul`（对比 `computeReference`，但因参考也是 cuBLAS，本质是自洽）与 `testGemmConsistencyMatmul`（对比另一配置）。

> 注意：`test_gemm.cu` 用的是 `unittest_utils.h`（[unittest_utils.h:52-57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/unittest_utils.h#L52-L57)）里**自定义的** `EXPECT_TRUE` 宏——它不是 gtest 的，而是「条件不满足就抛 `TestFailureError`」。因为该文件有自己的 `main()` 不链 `gtest_main`，所以用异常做失败信号。这是 FT「假 gtest」风格的典型，读源码时别和真 gtest 混淆。

#### 4.3.4 代码实践

1. **实践目标**：用源码阅读方式，说清 `test_sampling.cu` 如何构造输入、调用 FT 解码层、与参考比对。
2. **操作步骤**（源码阅读型，无需运行）：
   - 读 [test_sampling.cu:55-89](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L55-L89)，写出 `computeProb`/`computeLogProb` 这两个 CPU 参考实现的数学含义。
   - 读 [test_sampling.cu:134-190](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L134-L190)，列出它构造了哪些 buffer、哪些 `TensorMap` 字段。
   - 读 [test_sampling.cu:192-268](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_sampling.cu#L192-L268)，标出「逐步 forward → 取 output_ids → 累加参考对数概率 → 最终比对」这条链。
3. **需要观察的现象**：你应该能用一句话总结「为什么它比 `cum_log_probs` 而不是比 `output_ids`」。
4. **预期结果**：因为采样带随机性（`random_seed`），同一 seed 下 token 选择依赖随机数流，逐 token 比对脆弱；而对数概率是 logits 经 softmax 的确定函数，采样到哪个 token 就累加该 token 的对数概率，最终累积值可与 CPU 独立计算的结果严格比对，从而把「随机性」与「数值正确性」解耦。

#### 4.3.5 小练习与答案

**练习 1**：把 `checkResult` 的容差判据写成：FP16 时 atol=1e-3、rtol=1e-1，并允许 1% 元素失败。假如某次 softmax 测试有 1000 个输出元素、12 个超出容差，会判通过还是失败？

**答案**：判 **失败（FAILED）**。允许失败数 `tol_failures = 0.01 × 1000 = 10`，实际 12 个失败超过 10 的上限（判据为 `failures > tol_failures` 即失败）。注意别算反方向：是「失败数必须 ≤ 上限」才算 OK。

**练习 2**：`test_gemm.cu` 用 `cublasGemmEx` 当参考，这种「一致性测试」能发现 FT 的哪类 bug，发现不了哪类？

**答案**：能发现 FT 在 `cublasMMWrapper::Gemm` **封装层**引入的 bug——比如 transpose 参数填反、leading dimension 算错、alpha/beta 用错精度、batched/strided 调用拼错。发现不了 cuBLAS **自身**的数值 bug（因为参考与被测都建立在 cuBLAS 之上，共因会被抵消），也发现不了「FT 与 cuBLAS 都错但错得一样」的情况。

---

### 4.4 基准脚本：benchmarks 如何测量端到端加速比

#### 4.4.1 概念说明

`benchmarks/` 不测数值，只测**延迟与加速比**。脚本的核心思想：对每个 (batch, seq, 精度) 组合，分别用「原生框架」和「FT / 去了 padding 的 EFF-FT」跑同一模型，记录各自延迟，算 `speedup = 原生延迟 / FT延迟`，最后汇总成一张 Markdown 表。

两个脚本对应两种框架外壳（承接 u10）：

- `pyt_benchmark.sh`：PyTorch，对比对象是 TorchScript；用**相对路径**（`../build/...`），意味着必须在 `benchmarks/bert/` 目录里运行。
- `tf_benchmark.sh`：TensorFlow；用 `$FT_REPO_PATH` **绝对路径**，开头检查该变量未设就退出。

两者都在脚本里 `export NVIDIA_TF32_OVERRIDE=0`，关闭 Ampere 的 TF32 加速，确保 FP32 跑的是真 FP32（否则 FP32 数字会因 TF32 而偏快，结果不可比）。

#### 4.4.2 核心流程

`pyt_benchmark.sh` 的循环骨架（伪代码）：

```
pip install transformers==2.5.1            # 一次性依赖
export NVIDIA_TF32_OVERRIDE=0              # 关 TF32
for precision in fp16 fp32:
  for batch_size in 1 8 32:
    for seq_len in 32 128 384:
      rm gemm_config.in
      ../build/bin/bert_gemm $bs $seq 12 64 $prec_num 0   # 离线调优生成 gemm_config.in
      python ../examples/pytorch/bert/bert_example.py $bs 12 $seq 12 64 --data_type ... --time
      # 从 example 输出里 tail/awk 抽取 ths_time / ft_time / eff_ft_time
      ft_speedup = ths_time / ft_time        # 用 bc 算浮点除法
```

关键依赖链：**每个组合都要先跑 `bert_gemm` 做离线 GEMM 调优**（u2-l4 讲过 `gemm_config.in`），否则 FT 跑不到最优；然后 `bert_example.py --time` 负责跑 TorchScript vs FT vs EFF-FT 并打印三段延迟。

#### 4.4.3 源码精读

`pyt_benchmark.sh` 开头装依赖、关 TF32（[pyt_benchmark.sh:18-22](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L18-L22)），精度循环把字符串映射成 `precision_num`（fp16→1、fp32→0，正是 u1-l4 讲的 `data_type` 枚举，[pyt_benchmark.sh:25-33](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L25-L33)）。

双层循环里先删旧的 `gemm_config.in`、调 `bert_gemm` 生成新配置、再跑 example（[pyt_benchmark.sh:46-60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L46-L60)）：

```bash
for batch_size in 1 8 32 ;
do
for seq_len in 32 128 384 ;
do
  if [ -f "gemm_config.in" ] ; then rm gemm_config.in ; fi
  ../build/bin/bert_gemm ${batch_size} ${seq_len} 12 64 ${precision_num} 0
  ...
  python ../examples/pytorch/bert/bert_example.py ${batch_size} 12 ${seq_len} 12 64 --data_type fp16 --time 2>&1 | tee $tmp_log_ths
```

延迟用 `tail | head | awk` 从日志里抽取（依赖 example 固定的输出格式，[pyt_benchmark.sh:61-66](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L61-L66)），加速比用 `bc` 做浮点除法——这就是脚本顶部注释「`apt-get install bc`」的原因（[pyt_benchmark.sh:15-16](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L15-L16)）。

`tf_benchmark.sh` 结构同构，区别是用 `$FT_REPO_PATH` 绝对路径（[tf_benchmark.sh:15-20](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/tf_benchmark.sh#L15-L20) 检查变量、[tf_benchmark.sh:54-57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/tf_benchmark.sh#L54-L57) 使用），并要求 TF 构建产物（承接 u10-l2 的 `BUILD_TF`/`TF_PATH`）。

#### 4.4.4 代码实践

1. **实践目标**：列出在本地运行 `benchmarks/bert/pyt_benchmark.sh` 的全部前置条件，并解释每条的出处。
2. **操作步骤**：先逐条核对，再（可选）实跑。
3. **前置条件清单**（均可在源码中找到出处）：

   | # | 前置条件 | 出处 / 说明 |
   | --- | --- | --- |
   | 1 | 用 `BUILD_PYT=ON` 编译 FT | 脚本调 `../build/bin/bert_gemm` 与 PyTorch 扩展 `libth_transformer.so`，需要 PyTorch 构建产物（u1-l2、u10-l1）。 |
   | 2 | 安装 `transformers==2.5.1` | 脚本 [pyt_benchmark.sh:18](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L18) 自己 `pip install`；需一个能装该旧版本的环境（Python/torch 版本要匹配）。 |
   | 3 | 安装 `bc` | 脚本用 `bc` 算加速比浮点除法，[pyt_benchmark.sh:15-16](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L15-L16) 注释提示 `apt-get install bc`；缺了会在算 `ft_speedup` 时报错。 |
   | 4 | **必须在 `benchmarks/bert/` 目录运行** | 脚本用相对路径 `../build/bin/bert_gemm`、`../examples/pytorch/bert/bert_example.py`，[pyt_benchmark.sh:53-59](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L53-L59)。换目录就找不到文件。 |
   | 5 | 可用 GPU + 匹配的 CUDA/torch | FT kernel 必须能在 GPU 上跑；GPU 架构要对上编译时的 `-DSM`。 |
   | 6 | 提供 `libth_transformer.so` 可加载路径 | `bert_example.py` 的 `--ths_path` 默认 `./lib/libth_transformer.so`（[examples/pytorch/bert/bert_example.py:75](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/bert/bert_example.py#L75)），脚本未传该参数，需保证该 `.so` 可被加载（或自行软链/调整）。 |
   | 7 | 关闭 TF32 | 脚本 [pyt_benchmark.sh:20](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/benchmarks/bert/pyt_benchmark.sh#L20) 自己 `export NVIDIA_TF32_OVERRIDE=0`，无需手动设。 |

4. **一个好消息**：**不需要真实 BERT checkpoint**。脚本不传 `--weight_path`，而 `bert_example.py` 该参数默认 `None`，此时走随机权重（`ft_weights._generated_weights = True`，[examples/pytorch/bert/bert_example.py:159-166](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/bert/bert_example.py#L159-L166)）。基准只测速度，随机权重完全够用。
5. **预期结果**：跑完后在 `benchmarks/bert/bert-base-log-<precision>/all-log.log` 得到一张 Markdown 表，含 TorchScript / FT / EFF-FT 三档延迟与两档加速比。

> 若上述条件无法在本地满足（无 GPU、装不上 `transformers==2.5.1` 等），则标注「待本地验证」并仅做源码层面的流程理解——这完全不妨碍读懂脚本逻辑。

#### 4.4.5 小练习与答案

**练习 1**：`pyt_benchmark.sh` 在循环里每次都先 `rm gemm_config.in` 再调 `bert_gemm`，为什么？

**答案**：`gemm_config.in` 是为特定 (batch, seq, 精度) 离线调优出的 cuBLAS 算法表（u2-l4）。换 batch 或 seq，最优算法就变了；不删旧表，`bert_gemm` 可能读到错误的旧配置，导致 FT 用次优甚至不匹配的算法，测出的延迟不可信。

**练习 2**：`tf_benchmark.sh` 开头有一段检查 `$FT_REPO_PATH`，未设就 `exit`；而 `pyt_benchmark.sh` 没有类似检查。这隐含了什么运行约定？

**答案**：`tf_benchmark.sh` 用绝对路径 `${FT_REPO_PATH}/...`，必须显式知道仓库在哪，所以强制要求环境变量；`pyt_benchmark.sh` 用相对路径 `../...`，依赖「当前目录是 `benchmarks/bert/`」这一隐含约定来定位仓库根，所以不检查变量、但要求你站在正确目录。两种脚本用不同方式解决同一问题（找到仓库根）。

---

## 5. 综合实践

**任务**：假设你改动了 `invokeMaskedSoftmax`（u3-l2）的某个 reduction 逻辑，担心引入数值回归。请用本讲学到的工具设计一套「先验正确性、再验性能」的验证流程，并指出每一步用到哪个文件/命令。

**建议步骤**：

1. **定位单元测试**：找到直接覆盖 `invokeMaskedSoftmax` 的用例——即 `unittest` 里的 `AttentionKernelTest.MaskedSoftmax*`（[test_attention_kernels.cu:451-481](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L451-L481)）。
2. **跑正确性**：`cd build && ./unittest --gtest_filter='AttentionKernelTest.MaskedSoftmax*'`。若改动破坏了数值，`checkResult` 会打印 `FAILED (failures: xx% ...)`，并给出前 4 个不匹配元素的 found/expected/tol（[gtest_utils.h:50-55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/gtest_utils.h#L50-L55)）。
3. **理解参考**：对照 CPU 参考 `computeQkSoftmax`（[test_attention_kernels.cu:133-193](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L133-L193)），确认你的新 reduction 逻辑与「三遍循环：求 max → 求 sum → 归一化」等价，特别注意被 mask 位置应置 0。
4. **必要时加用例**：如果你的改动只影响长序列（如 4096），可在 `Benchmark_*` 用例旁边复制一个 `MaskedSoftmax_LongSequence8192` 用例（参数结构见 [test_attention_kernels.cu:34-46](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_attention_kernels.cu#L34-L46)），让正确性覆盖也延伸到长序列。
5. **再验性能**：正确性通过后，用 `Benchmark_*` 用例（`is_benchmark=true`，跳过参考）或在 `benchmarks/bert/pyt_benchmark.sh` 流程里对比改前改后的延迟，确认没有性能退化。

**思考题**：为什么步骤 2 用 gtest 过滤、而步骤 5 用 benchmark？因为这正对应本讲的核心分工——gtest 负责正确性的可复现、可过滤定位，benchmark 负责性能的横向对比。

## 6. 本讲小结

- FT 用 `tests/`（正确性）和 `benchmarks/`（性能）两套独立工具，前者靠单元测试比对 GPU 结果与参考，后者靠脚本测端到端延迟与加速比。
- 测试构建入口 `tests/CMakeLists.txt` → `unittests`（无条件）+ `gemm_dequantize`/`moe`/`int8_gemm`（`BUILD_PYT` 守卫）；`unittests` 又拉取 GoogleTest 1.12.1 并产出 5 个可执行文件。
- 单元测试有三种风格：真 gtest（`test_tensor`/`test_attention_kernels`，进 `unittest`）、手写「假 gtest」宏（`test_gemm`）、独立 `main()` 批处理（`test_sampling` 等）；其中 **`test_sampling.cu` 实际未被 CMake 编译**。
- 验证正确性的三件套是「CPU 参考实现 + `almostEqual`（atol/rtol）+ 允许 1% 元素不匹配」；随机性结果（如采样）改比累积对数概率而非比 token。
- `benchmarks/bert/pyt_benchmark.sh` 的运行前提是：`BUILD_PYT` 产物、`transformers==2.5.1`、`bc`、在 `benchmarks/bert/` 目录运行、GPU 环境、可加载的 `libth_transformer.so`；不需要真实 checkpoint（用随机权重）。

## 7. 下一步学习建议

- **若要新增模型/算子的测试**：参照 `templates/adding_a_new_model/README.md`（u11-l2），为新 kernel 写一个 `TYPED_TEST` + CPU 参考，并加进 [tests/unittests/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/CMakeLists.txt) 的 `unittest` 源文件清单与链接库。
- **深入 sampling 测试**：读 `test_sampling_kernels.cu`、`test_sampling_layer.cu`（这两个才是真正编译进 `unittest` 的采样测试），对比它们与 `test_sampling.cu` 的写法差异。
- **量化与 CUTLASS 测试**：在 `BUILD_PYT=ON` 后进入 `tests/gemm_dequantize`、`tests/int8_gemm`、`tests/moe`，那里验证 u9 讲的 weight-only / w8a8 / MoE GEMM 的正确性。
- **更大规模基准**：从 `benchmarks/bert` 扩展到 `benchmarks/gpt`、`benchmarks/t5`、`benchmarks/decoding`，它们用类似循环结构测大模型/解码模型的端到端性能。
