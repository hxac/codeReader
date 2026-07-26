# cuBLAS 参考基准（CUDA C++ 测试床）

## 1. 本讲目标

本讲解剖 tilelang-benchmark 里**编号为 `0.` 的参考基线**——cuBLAS 基准。它是整套对比框架的「标尺」：所有 TileLang / Triton / BitBLAS 内核的 TFlops，最终都要除以 cuBLAS 的延迟才能看出谁快谁慢。

学完本讲，你应当能够：

1. 看懂 `CMakeLists.txt` 如何查找 CUDA/cuBLAS 依赖、如何设置目标 GPU 架构，并解释「默认 89、目标 80」这个历史遗留点。
2. 读懂 `tensor.h` 提供的设备张量工具：`cudaMalloc` + `shared_ptr` 自动释放、`rand/zeros/fill`、以及 int8 的 `pad_dim` 对齐。
3. 逐行讲清 `time_gemm` 的**计时核心**：用 `std::chrono` 在 host 侧计时、用 `minimal_repeat_ms` 做「自适应重复次数」、以及它为什么对本项目的大 shape 几乎总是退化成下限 5。
4. 理解 `cublasGemmEx` 的一条调用如何分派出 fp32 / fp16 / int8 三种精度路径，以及 Tensor Core 与普通路径的切换。
5. 把 cuBLAS 打印的 CSV 各列与下游 `data/*.py` 解析逻辑对应起来，理解「表头写 usec、实际是 ms」的单位陷阱。

## 2. 前置知识

阅读本讲前，你应当已经掌握 [u2-l4 性能度量方法论](u2-l4-benchmark-methodology.md) 中的两条结论：

- **运算量与延迟换算**：GEMM 运算量为 \(2MNK\)，由延迟（ms）换算 TFlops 的统一公式为

  \[
  \text{TFlops} = \frac{2MNK}{\text{latency\_ms}} \times 10^{-9}
  \]

- **测量稳定性**：cuBLAS 基线用「1 次 warmup + 自适应重复次数」计时，下限 5 次；这与 TileLang/Triton 的固定 `warmup`/`rep` 不同。本讲就要讲清这套自适应逻辑到底怎么实现的。

此外需要一点 CUDA 基础词汇：

- **cuBLAS**：NVIDIA 官方的线性代数库，`cublasGemmEx` 是它的「多精度通用矩阵乘」接口，一个函数覆盖 fp32/fp16/int8。
- **Tensor Core**：Volta 以后 GPU 上的专用矩阵乘加速单元，cuBLAS 通过 `algo` 参数与 math mode 选择是否使用。
- **host 侧计时**：在 CPU 上用 `std::chrono` 记录时间戳，配合 `cudaDeviceSynchronize()` 等待 GPU 跑完，两次时间戳之差就是 GPU 耗时。
- **列主序（column-major）**：cuBLAS 沿用 Fortran 习惯，矩阵按列连续存储；这与 C/C++/PyTorch 的行主序相反，后面会看到代码为此做了「A/B 对调」的小技巧。

最后回忆 [u1-l3 运行一次基准测试](u1-l3-running-a-benchmark.md) 的结论：cuBLAS 走**编译型**路径，`compile_and_run.sh` 用 CMake + make 把 `.cu` 编译成可执行文件 `cublas_benchmark`，再把它的标准输出 `tee` 到 `benchmark_results.log`。本讲就是拆开这个可执行文件的源码。

## 3. 本讲源码地图

本讲聚焦 `hopper_benchmark/dense_matmul/0.cublas-benchmark/` 目录下的三个文件，外加一个对照样本：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [CMakeLists.txt](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt) | 73 | 构建脚本：查找 CUDA/cuBLAS/cuRAND 依赖、设置 GPU 架构、链接库、产出可执行文件 |
| [tensor.h](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h) | 117 | 头文件：`Tensor<T>` 设备张量类、`rand/zeros/fill` 填充工具、`pad_dim` 对齐工具 |
| [cublas_benchmark.cu](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu) | 311 | 主程序：`inference_server_set` 形状集、`time_gemm` 计时函数、`main` 里 5 条精度路径 |
| （对照）[ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu) | 377 | Ada 架构的低精度变体：改用 `cublasLtMatmul`，多出 fp8（e5m2/e4m3）路径，供对照阅读 |

数据衔接文件（下游解析 cuBLAS 日志）：[data/data_float16_gemm.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py)。

---

## 4. 核心概念与源码讲解

### 4.1 构建系统：CMakeLists 与依赖查找

#### 4.1.1 概念说明

cuBLAS 基线是一个 **CUDA C++ 程序**，必须先编译成可执行文件才能跑。构建由 CMake 驱动，它要解决三件事：

1. **找依赖**：CUDA 运行时（`cudart`）、cuBLAS（`cublas`）、cuRAND（`curand`，用于生成随机数据）的头文件和库在哪里。
2. **选架构**：把 `.cu` 编译成哪个 SM（流多处理器）版本的可执行文件。GPU 架构用数字表示，例如 80 = Ampere（A100）、89 = Ada（RTX 4090）、90 = Hopper（H100）。
3. **产出可执行文件**并链接库。

这一节的看点是一个**历史遗留不一致**：CMake 默认架构是 89，但本目录（hopper）的目标属性又被改写成 80。这种「字面值与实际不符」的现象在 tilelang-benchmark 里很常见，读源码时必须留意。

#### 4.1.2 核心流程

```text
cmake_minimum_required(VERSION 3.20)
  ↓
若未指定 CMAKE_CUDA_ARCHITECTURES → 默认设为 89            # 默认架构
  ↓
project(Cuda_Gemm C CXX CUDA)                              # 启用 CUDA 语言
  ↓
CUBLAS_HINTS  ← CUDA_ROOT / 环境变量                      # 用户自定的查找提示
CUBLAS_PATHS  ← /usr /usr/local /usr/local/cuda           # 系统默认路径
  ↓
find_path(CUBLAS_INCLUDE_DIRS cublas_v2.h cuda.h)         # 找头文件
find_library(CUDA_LIBRARIES  cudart)                      # 找 cudart 库
find_library(CUBLAS_LIBRARIES cublas)                     # 找 cublas 库
  ↓
add_executable(cublas_benchmark cublas_benchmark.cu)      # 编译目标
set_target_properties(... CUDA_ARCHITECTURES "80")        # ← 覆盖为 80
target_link_libraries(... cudart cublas curand)           # 链接
```

#### 4.1.3 源码精读

**默认架构设为 89**（仅当用户没在命令行指定时生效）：

[CMakeLists.txt:L3-L5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L3-L5) —— 若调用 cmake 时没传 `-DCMAKE_CUDA_ARCHITECTURES=...`，就把全局默认设为 89（Ada）。

**依赖查找的两套路径**：`CUBLAS_HINTS` 是「用户提示」（读 `CUDA_ROOT` 等环境变量），`CUBLAS_PATHS` 是「系统兜底」(`/usr/local/cuda` 等)。`find_path`/`find_library` 会先查 HINTS 再查 PATHS：

[CMakeLists.txt:L10-L19](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L10-L19) —— 定义查找路径集合，兼容不同发行版的 CUDA 安装位置。

[CMakeLists.txt:L22-L47](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L22-L47) —— 分别查找头文件目录（`cublas_v2.h`、`cuda.h`）、`cudart` 库、`cublas` 库。`PATH_SUFFIXES` 兼容 `lib`/`lib64`/`lib/x86_64` 等目录布局。

**判定是否找到**用标准模块 `FindPackageHandleStandardArgs`：

[CMakeLists.txt:L63-L64](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L63-L64) —— 三个变量（include、cudart、cublas）都找到才算成功。

**最关键的三行——编译目标、覆盖架构、链接库**：

[CMakeLists.txt:L68-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L68-L72) —— 注意第 69 行把 `cublas_benchmark` 这个目标的 `CUDA_ARCHITECTURES` **强制设为 "80"**，覆盖了第 4 行的全局默认 89。也就是说，虽然这个目录名叫 `hopper_benchmark`（H100 = 架构 90），但它编译出的可执行文件目标是 sm_80（Ampere）。第 70–71 行开启 `CUDA_SEPARABLE_COMPILATION`（可分离编译，便于设备链接），第 72 行链接 `cudart`、`cublas`、`curand`。

> **读源码注意点**：架构数字与目录名并不严格对应。Hopper 目录里编译目标反而是 80。这是因为 cuBLAS 是闭源库，`.cu` 里没有架构专属指令，编译成 sm_80 的可执行文件在 sm_80/89/90 上都能跑（向后兼容），所以作者没在这里纠结架构号。这与 TileLang 内核里 `target="cuda"`/架构号必须精确匹配形成对比。

#### 4.1.4 代码实践

**目标**：亲手用 CMake 跑一遍构建流程，观察架构覆盖现象（需要一台装了 CUDA 的机器）。

**步骤**：

1. 进入目录 `hopper_benchmark/dense_matmul/0.cublas-benchmark/`。
2. 阅读 [compile_and_run.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/compile_and_run.sh)，它依次执行 `mkdir build && cd build && cmake .. && make -j`，然后 `./build/cublas_benchmark | tee benchmark_results.log`。
3. 在 `build/` 目录里执行 `cmake -LA .. | grep -i ARCH`，观察 `CMAKE_CUDA_ARCHITECTURES` 的最终取值。
4. 执行 `make VERBOSE=1 2>&1 | grep -E "arch=compute|gencode"`，查看 `nvcc` 实际传给编译器的架构标志。

**需要观察的现象**：

- 即使你不传 `-DCMAKE_CUDA_ARCHITECTURES`，第 4 行的全局默认把它设为 89；但第 69 行的目标属性又改写成 80。
- `nvcc` 命令行里应出现类似 `-arch=sm_80` 的标志。

**预期结果**：可执行文件 `build/cublas_benchmark` 生成；`make` 输出里能看到目标架构为 80。若机器上没装 CUDA，则 `cmake` 阶段会打印 "Could NOT find cuBLAS library"（见 [CMakeLists.txt:L52-L60](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L52-L60)）并链接失败——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CMakeLists 里有「默认 89」与「目标 80」两套架构号？以哪个为准？
**答案**：第 4 行的 89 是 `CMAKE_CUDA_ARCHITECTURES` 全局变量的默认值（仅当外部未指定时生效）；第 69 行的 80 是 `cublas_benchmark` 这个 target 的属性，优先级更高。最终 `nvcc` 用的是 target 属性 80。

**练习 2**：如果要把这个可执行文件改成专为 Hopper（sm_90）编译，应该改哪一行？
**答案**：改 [CMakeLists.txt:L69](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L69) 的 `CUDA_ARCHITECTURES "80"` 为 `"90"`，或直接在命令行 `cmake -DCMAKE_CUDA_ARCHITECTURES=90 ..`（但后者会被第 69 行覆盖，所以必须改源文件）。

---

### 4.2 设备张量工具：tensor.h

#### 4.2.1 概念说明

基准测试需要三类数据：随机输入矩阵 A、B，以及清零的输出矩阵 C。`tensor.h` 是一个**只有头文件**的小工具库，封装了「在 GPU 上分配一块显存、填上随机数或零、用完自动释放」的模板类 `Tensor<T>`，让 `main` 函数能像写 Python 一样简洁地构造数据。

它的设计要点有三个：

1. **RAII 自动释放**：用 `std::shared_ptr` + 自定义删除器持有 `cudaMalloc` 出来的设备指针，对象析构时自动 `cudaFree`，不会泄漏显存。
2. **模板特化生成随机数**：`rand<T>` 对 `float` 直接调用 cuRAND；对 `half/uint8_t/uint16_t` 则先生成 float 再用一个 CUDA kernel 转换类型。
3. **int8 对齐工具**：`pad_dim` 把维度补齐到某个倍数（int8 Tensor Core 要求 m 是 4 的倍数）。

#### 4.2.2 核心流程

```text
Tensor<T>(dims)
  ├─ size_ = 所有维度连乘
  ├─ cudaMalloc(size_ * sizeof(T))         # 申请显存
  └─ shared_ptr(ptr, deleteCudaPtr)        # 析构时 cudaFree

rand<T>(dims, gen):
  ├─ 若 T == float:  curandGenerateUniform 直接填
  └─ 否则:           curandGenerateUniform 填到 temp float tensor
                     → convertType<<<>>> kernel 转成 T

zeros<T>(dims):  thrust::fill 填 0
pad_dim(dim, 4): dim 向上补齐到 4 的倍数
```

#### 4.2.3 源码精读

**`Tensor<T>` 类与自动释放**：

[tensor.h:L34-L66](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L34-L66) —— 重点看 [L40-L46](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L40-L46) 的 `deleteCudaPtr` 仿函数（调用 `cudaFree`），以及 [L53-L60](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L53-L60) 构造函数里 `cudaMalloc` 后用 `ptr_.reset(tmp_ptr, deleteCudaPtr())` 把裸指针托管给 `shared_ptr`。`begin()` 返回设备指针供 cuBLAS 使用，`dims()` 返回形状向量供计算 m/n/k。

**`rand` 的模板特化**：

[tensor.h:L86-L107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L86-L107) —— 用 `std::enable_if` 做编译期分派：`float` 版（[L86-L93](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L86-L93)）直接 `curandGenerateUniform`；其他类型版（[L95-L107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L95-L107)）先填一个 float 临时张量，再用 `convertType<<<(size+255)/256, 256>>>` 转换。类型转换 kernel 定义在 [tensor.h:L14-L22](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L14-L22)，就是逐元素 `static_cast`。

**int8 对齐工具 `pad_dim`**：

[tensor.h:L109-L116](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L109-L116) —— 把 `dim` 向上补到 `pad_v` 的倍数。这个函数在 `main` 里用于 int8 路径：把 m 补到 4 的倍数（见 4.4 节），因为 int8 Tensor Core 的 `dp4a` 指令一次处理 4 个 int8 元素。

> **为什么需要特化而不是统一处理？** cuRAND 的 `curandGenerateUniform` 只支持 float/double。要生成 half 或 int8 的随机数据，最简单的办法就是先生成 float 再转换。`convertType` kernel 让这一步在 GPU 上并行完成，避免把数据搬回 CPU。

#### 4.2.4 代码实践

**目标**：通过阅读源码理解 `Tensor` 的生命周期，不实际编译也能画出内存流转。

**步骤**：

1. 在 [tensor.h:L53-L60](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L53-L60) 找到 `cudaMalloc` 调用，确认分配的字节数公式。
2. 追踪一个 `auto a = rand<uint16_t>({8192, 8192}, gen);` 的执行路径：它走的是哪个 `rand` 重载？经过了几次显存分配？
3. 思考：如果 `main` 函数里 `a`、`b`、`c` 是局部变量，离开作用域后显存会被释放吗？靠的是哪一行代码？

**需要观察的现象 / 预期结果**：

- 字节数 = `8192 * 8192 * sizeof(uint16_t)` = 128 MiB。
- `rand<uint16_t>` 走的是 [L95-L107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L95-L107) 的通用版本，**两次** `cudaMalloc`：一次给临时 float 张量，一次给最终 uint16_t 张量。
- 局部变量离开作用域 → `shared_ptr` 引用计数归零 → 触发 `deleteCudaPtr` → `cudaFree`。靠的是 [L48](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L48) 的 `std::shared_ptr<T> ptr_` 与 [L59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L59) 的 `reset(tmp_ptr, deleteCudaPtr())`。

#### 4.2.5 小练习与答案

**练习 1**：`rand<uint8_t>` 生成的 int8 数据范围是多少？为什么？
**答案**：范围是 \([0, 1]\) 的浮点数被 `static_cast<uint8_t>` 截断，所以实际只取 0 或 1（因为 `curandGenerateUniform` 生成 \([0,1)\)，乘 1 后截断为整数几乎都是 0，偶尔为 1）。这是基准测试的常见简化——**只测延迟、不关心数值正确性**，所以数据分布无所谓。若需真实分布要改用 `scaleType` kernel（[tensor.h:L24-L32](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L24-L32)）做缩放。

**练习 2**：`pad_dim(m, 4)` 当 `m = 8193` 时返回多少？
**答案**：`8193 % 4 = 1`，补 `4 - 1 = 3`，得 `8196`。见 [tensor.h:L109-L116](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/tensor.h#L109-L116)。

---

### 4.3 计时核心：time_gemm 与自适应重复次数

#### 4.3.1 概念说明

`time_gemm` 是整个 cuBLAS 基线的**心脏**：它接收两个输入张量 A、B 和输出张量 C，调用一次 `cublasGemmEx` 做矩阵乘，并返回这次乘法的平均延迟（微秒）。

它的计时哲学是「**自适应重复次数**」：先用 1 次调用测个大概耗时，再估算「要跑多少次才能让总耗时填满一个 100 ms 的测量窗口」，最后用这个次数循环、取平均。这样做是为了让快内核（几十微秒）和慢内核（几毫秒）都能测得稳定——快内核多跑几次、慢内核少跑几次。

但本节最重要的发现是：**这段「自适应」逻辑里有个 `*1e3` 的放大因子，导致对本项目这些大 shape，重复次数几乎总是退化成下限 5。** 也就是说，理论上自适应、实际几乎恒为 5 次。这是本讲的核心读码练习。

#### 4.3.2 核心流程

`time_gemm<T1, T2>(A, B, C, a_t, b_t, handle, use_tensor_core)` 的执行过程：

```text
1. 由 A/B 的 dims 与 a_t/b_t 推导 cuBLAS 的 (m, n, k)
2. 由模板类型 T1 决定 A/B/C/compute 的 cudaDataType_t        # 4.4 节详讲
3. algo = use_tensor_core ? TENSOR_OP : DFALT
4. warmup：
     start = steady_clock::now()
     cublasGemmEx(...)            # 跑 1 次
     cudaDeviceSynchronize()      # 等 GPU 完成
     end   = steady_clock::now()
     periter = (int)(end - start 的微秒数) * 1e3
5. numRepeats = max(5, (int)(minimal_repeat_ms / periter))
6. 正式计时：
     start = steady_clock::now()
     for i in [0, numRepeats): cublasGemmEx(...)
     cudaDeviceSynchronize()
     end   = steady_clock::now()
     return (end - start 的微秒数) / numRepeats
```

**自适应重复次数的公式**（重点）：

\[
\text{periter\_duration} = \lfloor t_{\text{warmup}}(\mu s) \rfloor \times 10^{3}
\]

\[
\text{numRepeats} = \max\!\left(5,\ \left\lfloor \frac{\text{minimal\_repeat\_ms}}{\text{periter\_duration}} \right\rfloor\right),\quad \text{minimal\_repeat\_ms}=100
\]

代入一个大 shape 看看：假设 warmup 测得 \(t_{\text{warmup}} = 500\,\mu s\)（典型的 8192³ fp16 GEMM）：

\[
\text{periter\_duration} = 500 \times 10^3 = 500000,\qquad
\text{numRepeats} = \max(5,\, \lfloor 100/500000 \rfloor) = \max(5, 0) = 5
\]

结果就是下限 5。只有当单次耗时低于约 \(0.017\,\mu s\) 时（即 17 皮秒，对 GEMM 不可能），`numRepeats` 才会超过 5。因此**对本项目的所有大 shape，numRepeats 实际恒为 5**。

> **这是 bug 还是 feature？** 从变量名 `minimal_repeat_ms` 看，作者的本意确实是「填满 100 ms 窗口」；但 `*1e3` 这个因子把 `periter_duration` 放大了 1000 倍，使商几乎永远小于 5。可能是作者本想用毫秒做单位却写成了微秒、再用 `*1e3`「修正」，结果修正方向反了。对延迟测量的影响是：每个 shape 只跑 5 次，统计样本偏少，但因为 GEMM 本身很稳定、5 次平均的波动可接受。精确的每 shape 重复次数**待本地验证**。

#### 4.3.3 源码精读

**函数签名与 m/n/k 推导**：

[cublas_benchmark.cu:L70-L82](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L70-L82) —— 模板参数 `T1`（输入类型）与 `T2`（输出/累加类型）。[L82](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L82) 定义 `minimal_repeat_ms = 100`。

> **读源码注意点（m/n/k 对调）**：cuBLAS 是**列主序**接口，而 `Tensor` 里存的是行主序数据。代码用了一个经典技巧——行主序的 \(C = A\cdot B\) 等价于列主序的 \(C^{T} = B^{T}\cdot A^{T}\)。因此 [L77-L79](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L77-L79) 推导出的局部 `m`、`n` 与 `main` 里打印的问题 `m`、`n` 是**对调的**，并且在 [L110-L114](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L110-L114) 调用 `cublasGemmEx` 时把 **B 作为第一个矩阵、A 作为第二个矩阵**传入，transpose 参数也跟着对调。这不影响计时结果，但读源码时不要被局部变量名迷惑。

**warmup 计时与自适应重复**（本讲核心）：

[cublas_benchmark.cu:L108-L129](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L108-L129) —— 逐行解读：

- [L108](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L108) `warmup_start`：记录开始时间戳。
- [L110-L114](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L110-L114) 调一次 `cublasGemmEx` 做 warmup（让缓存、驱动、JIT 等稳定）。
- [L120](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L120) `cudaDeviceSynchronize()`：**关键**——cuBLAS 调用是异步的，必须同步等待才能真正测到 GPU 耗时，否则测到的只是「入队时间」。
- [L122-L125](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L122-L125) 计算 `periter_duration`：用 `std::chrono::duration<double, std::micro>` 取微秒数，`static_cast<int>` 截断成整数微秒，**再 `*1e3`**——就是这一步让后面 `numRepeats` 退化。
- [L127](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L127) `numRepeats = std::max(5, int(minimal_repeat_ms / periter_duration))`：下限 5、上限「100 ms 窗口」。

**正式计时循环**：

[cublas_benchmark.cu:L131-L151](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L131-L151) —— 注意 [L145](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L145) 的 `cudaDeviceSynchronize()` 放在**整个循环之后**：循环把 `numRepeats` 次 `cublasGemmEx` 全部异步入队，再一次同步等待全部完成。这样测到的是「`numRepeats` 个内核串行执行的总耗时」，除以 `numRepeats` 得到平均单次耗时。[L149-L151](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L149-L151) 返回值单位是**微秒**（`std::micro`）。

> **与 TileLang/Triton 计时的对比**（承接 u2-l4）：TileLang 用固定 `warmup=3, rep=20`；cuBLAS 用「1 次 warmup + 自适应重复」，但如前所述实际恒为 5。两者都用 host 侧墙钟 + `cudaDeviceSynchronize` 屏障，单位上 cuBLAS 返回微秒、TileLang/Triton 返回毫秒——跨框架对比前必须统一单位。

#### 4.3.4 代码实践（本讲主实践）

**目标**：定位「决定最少运行 100 ms 的自适应重复次数」逻辑，亲手代入一个数值，验证它对本项目的大 shape 实际退化成下限 5。

**步骤**：

1. 打开 [cublas_benchmark.cu:L70-L152](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L70-L152)。
2. 找到 `minimal_repeat_ms` 的定义行（[L82](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L82)），记下它的值与含义。
3. 找到计算 `periter_duration` 的三行（[L122-L125](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L122-L125)），解释 `static_cast<int>(...微秒...)` 后再 `*1e3` 的效果。
4. 找到 `numRepeats` 的赋值行（[L127](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L127)）。
5. 代入假想值：设 warmup 单次耗时为 `t_us` 微秒（例如 100、500、2000），手算 `periter_duration` 与 `numRepeats`，填入下表。

| warmup 单次耗时 \(t_{us}\)（µs） | `periter_duration` | `100 / periter_duration` | `numRepeats` |
| --- | --- | --- | --- |
| 100 | \(100 \times 10^3 = 100000\) | 0.001 | 5 |
| 500 | 500000 | 0.0002 | 5 |
| 2000 | 2000000 | 0.00005 | 5 |

**需要观察的现象**：无论代入哪个现实中的 GEMM 单次耗时（几十到几千微秒），`numRepeats` 都等于 5。

**预期结果**：你会得出结论——`minimal_repeat_ms` 在变量命名上表达了「填满 100 ms 测量窗口」的意图，但由于 `*1e3` 放大因子，实际效果是「每个 shape 固定跑 5 次」。要让它真正按 100 ms 窗口自适应，应把 [L125](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L125) 的 `*1e3` 去掉（让 `periter_duration` 保持微秒），并把 `minimal_repeat_ms` 改为 `100000`（100 ms = 100000 µs）。精确的每 shape 实测重复次数**待本地验证**。

> **读源码注意点（alpha/beta 类型）**：[L74-L75](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L74-L75) 把 `alpha`、`beta` 声明为 `const int` 却赋 `1.f`，类型与各精度路径的 `compute_type` 并不严格匹配；且 `beta=1` 表示 \(C = \alpha\cdot op(A)\cdot op(B) + C\) 的「累加」语义。由于本程序只测延迟、不校验数值正确性，这对计时无影响，但读者不应照抄这段写法到需要正确结果的代码里。

#### 4.3.5 小练习与答案

**练习 1**：`cudaDeviceSynchronize()` 在 `time_gemm` 里出现了两次（[L120](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L120) 与 [L145](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L145)）。去掉哪一个会让计时严重失真？为什么？
**答案**：两个都不能去。[L120](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L120) 同步保证 warmup 真正完成后再测 `periter_duration`（否则测到的是入队时间≈0，会让 `numRepeats` 爆炸或除零）；[L145](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L145) 同步保证正式循环的全部内核执行完毕后再取 `end` 时间戳（否则测到的只是「N 次入队」的 CPU 时间，远小于真实 GPU 耗时）。

**练习 2**：如果把 [L125](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L125) 的 `*1e3` 删掉，对一个 500 µs 的内核，`numRepeats` 会变成多少？
**答案**：`periter_duration = 500`（微秒），`numRepeats = max(5, int(100/500)) = max(5, 0) = 5`，仍是 5。要真正填满 100 ms 窗口还需把 `minimal_repeat_ms` 改成 `100000`，此时 `numRepeats = max(5, int(100000/500)) = 200`。

**练习 3**：`time_gemm` 的返回值单位是什么？`main` 里打印时又做了什么换算？
**答案**：返回微秒（`std::micro`，见 [L149-L151](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L149-L151)）。`main` 里 `std::cout << time_us / 1000.0`（见 4.4 节）除以 1000 换算成毫秒——但 CSV 表头却写 `(usec)`，这是著名的「单位陷阱」。

---

### 4.4 多精度 GEMM 调用与 main 编排

#### 4.4.1 概念说明

`time_gemm` 是个**函数模板** `time_gemm<T1, T2>`，靠 C++ 模板在**编译期**就分派出 fp32 / fp16 / int8 三条精度路径——同一份函数体，传不同模板参数就走不同的 `cudaDataType_t` 设置。这是 cuBLAS 基线能「一个程序测五种精度组合」的关键。

`main` 函数则负责**编排**：定义一组测试 shape（`inference_server_set`），对每个 shape 依次跑 5 条精度路径（fp32、fp16、int8、fp16-TensorCore、int8-TensorCore），把每条路径的延迟拼成一行 CSV 打印。这行 CSV 最终被 `tee` 到 `benchmark_results.log`，再由 `data/*.py` 解析进入可视化管线。

#### 4.4.2 核心流程

```text
inference_server_set = [(m, n, k, a_t, b_t), ...]      # 14 个推理 shape，全部 a_t=F, b_t=T

for problem in inference_server_set:
    打印 "m,n,k,a_t,b_t"
    cublasSetMathMode(DEFAULT_MATH)                     # 关闭 Tensor Core
    ├── fp32:  time_gemm<float,float>(...use_tensor_core=false)
    ├── fp16:  time_gemm<uint16_t,uint16_t>(...)
    └── int8:  pad_dim(m,4); time_gemm<uint8_t,int>(...)
    cublasSetMathMode(TENSOR_OP_MATH)                   # 开启 Tensor Core
    ├── fp16 TC: time_gemm<half,half>(...use_tensor_core=true)
    └── int8 TC: pad_dim(m,4); time_gemm<uint8_t,int>(...)
    打印换行
```

**精度分派表**（由模板类型 `T1` 决定）：

| 模板 `T1` | A/B 类型 | C 类型 | compute 类型 | 对应路径 |
| --- | --- | --- | --- | --- |
| `float` | CUDA_R_32F | CUDA_R_32F | CUDA_R_32F | fp32 |
| `uint16_t` / `half` | CUDA_R_16F | CUDA_R_16F | CUDA_R_16F | fp16 |
| `uint8_t` | CUDA_R_8I | CUDA_R_32I | CUDA_R_32I | int8（输入 8 位、累加 32 位） |

> 注意 int8 路径里 **C 与 compute 都是 32 位整数**——这是 int8 Tensor Core 的典型设置：输入用 8 位省带宽、用 `dp4a` 指令把 4 个 int8 点积累加到 32 位整数里，避免溢出。这正是 [u2-l4](u2-l4-benchmark-methodology.md) 讲过的「精度越低、峰值 TFlops 越高」的硬件根源。

#### 4.4.3 源码精读

**形状集 `inference_server_set`**：

[cublas_benchmark.cu:L22-L38](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L22-L38) —— 一个 `vector<tuple<int,int,int,bool,bool>>`，每条记录是 `(m, n, k, a_t, b_t)`。这 14 条 shape 模拟大语言模型推理场景的 GEMM（如 `(8192, 8192, 8192)`、`(1024, 28672, 8192)` 等），全部 `a_t=false, b_t=true`（权重矩阵 B 转置，是推理框架的常见布局）。

> **与 README shape 表的关系**（承接 u2-l4）：`inference_server_set` 是 cuBLAS 这个 C++ 程序**自己硬编码**的 shape 集；而 README 的 V/M/FA/CC/CT 五族 shape 是**可视化层**的命名约定。两者并不一一对应——下游 `data/*.py` 会在日志里按 `(m,n,k)` 字符串匹配来捞取 cuBLAS 结果（见本节末尾）。复现某个图时，**以 `data/*.py` 里枚举的 shape 列表为准**。

**精度分派（模板 → cudaDataType_t）**：

[cublas_benchmark.cu:L85-L107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L85-L107) —— 三段 `if (std::is_same<T1, ...>::value)` 分别设置 fp16（[L91-L97](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L91-L97)）、int8（[L99-L105](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L99-L105)）的类型；都不命中则保持 [L85-L88](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L85-L88) 的 fp32 默认。[L107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L107) 根据 `use_tensor_core` 选择算法：`CUBLAS_GEMM_DFALT_TENSOR_OP`（请求 Tensor Core）或 `CUBLAS_GEMM_DFALT`（普通路径）。

**`cublasGemmEx` 调用**：

[cublas_benchmark.cu:L110-L114](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L110-L114) —— 一个函数调用覆盖所有精度：`transa/transb`（B/A 对调，见 4.3 注意点）、`(m,n,k)`、`alpha`、`B` 指针与其类型与 leading dim、`A` 指针与其类型与 leading dim、`beta`、`C` 指针与其类型与 leading dim、`compute_type`、`algo`。cuBLAS 根据 `A_type/B_type/C_type/compute_type` 自动选择对应硬件指令。

**CSV 表头**：

[cublas_benchmark.cu:L203-L206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L203-L206) —— 定义 10 列：`m,n,k,a_t,b_t,fp32 time (usec),fp16 time (usec),int8 time (usec),fp16 tensor core time (usec),int8 tensor core time (usec)`。

> **单位陷阱**：表头写 `(usec)`，但每列实际打印的是 `time_us / 1000.0`（见 [L237](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L237) 等处），单位是**毫秒**。所以这一行的 5 个小数列实际依次是：fp32(ms)、fp16(ms)、int8(ms)、fp16-TC(ms)、int8-TC(ms)。

**5 条精度路径的执行**：

[cublas_benchmark.cu:L223-L301](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L223-L301) —— 关键节点：

- [L224](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L224) `cublasSetMathMode(..., CUBLAS_DEFAULT_MATH)`：前三条路径关闭 Tensor Core。
- [L230-L238](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L230-L238) fp32 路径：构造 `rand<float>` 的 A、B 与 `zeros<float>` 的 C，调 `time_gemm<float,float>(..., use_tensor_core=false)`，打印 `time_us / 1000.0`。
- [L240-L248](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L240-L248) fp16 路径：用 `uint16_t`（fp16 的位等价整数类型）。
- [L250-L266](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L250-L266) int8 路径：先用 `pad_dim(pad_m, 4)` 把 m 补到 4 的倍数（[L252-L258](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L252-L258)），再 `time_gemm<uint8_t,int>`。
- [L269](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L269) `cublasSetMathMode(..., CUBLAS_TENSOR_OP_MATH)`：后两条路径开启 Tensor Core。
- [L275-L283](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L275-L283) fp16 Tensor Core：用 `half` 类型、`use_tensor_core=true`。
- [L285-L301](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L285-L301) int8 Tensor Core：同样 `pad_dim` 后调 `time_gemm<uint8_t,int>(..., true)`。

> **为什么 int8 要 pad 而 fp32/fp16 不要？** int8 的 Tensor Core 指令（`dp4a`/`mma`）以 4 元素为一组处理，要求 m 维是 4 的倍数；fp32/fp16 的指令对齐要求不同，无需此步。

**与下游数据管线的衔接**：

[data/data_float16_gemm.py:L15-L23](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L15-L23) —— `get_and_print_cublas(m, n, k, log)` 逐行读 `benchmark_results.log`，找到包含 `"{m},{n},{k}"` 的那一行，用正则 `\d+\.\d+` 捞出所有小数，取 `[-2]`（**倒数第二个**小数）。对照上面的列顺序，5 个小数依次是 fp32、fp16、int8、fp16-TC、int8-TC，所以 `[-2]` 取的是 **fp16 Tensor Core** 那一列。也就是说，fp16 GEMM 对比图里 cuBLAS 这条曲线，用的正是 cuBLAS 的 fp16 Tensor Core 延迟——这是合理的选择，因为它是 cuBLAS 在 fp16 上的最快路径。这个解析细节会在 [u2-l7 数据提取与可视化](u2-l7-data-extraction-and-plotting.md) 详细展开。

#### 4.4.4 代码实践

**目标**：对照阅读 cuBLAS 与下游解析脚本，确认「CSV 第几列被画进图里」。

**步骤**：

1. 在 [cublas_benchmark.cu:L203-L206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L203-L206) 数清楚 CSV 有几个小数列、分别叫什么。
2. 打开 [data/data_float16_gemm.py:L21](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L21)，确认正则 `r"\d+\.\d+"` 与 `[-2]` 的取值。
3. 回答：如果某天你想把对比图的 cuBLAS 曲线换成 **fp32** 而非 fp16-TC，应该把 `[-2]` 改成什么下标？
4. （可选）对照阅读 [ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu)：它改用 `cublasLtMatmul`，多出 fp8（e5m2/e4m3）路径，并按 `deviceProp.major/minor >= 8.9` 判断是否启用 fp8（[L280-L285](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L280-L285)）。注意它的 `time_gemm` 里同样有 `minimal_repeat_ms = 100` 与 `*1e3`（[L106](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L106)、[L208-L212](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L208-L212)），说明这个「自适应退化成 5」的现象在两个架构上是一致的。

**需要观察的现象 / 预期结果**：

- CSV 有 5 个小数列：fp32、fp16、int8、fp16-TC、int8-TC。
- `[-2]` = fp16-TC 列（第 4 个小数）。
- 若改用 fp32，应把 `[-2]` 改成 `[-5]`（倒数第五，即第一个小数）。若用 int8-TC 则是 `[-1]`。

#### 4.4.5 小练习与答案

**练习 1**：`time_gemm<uint8_t, int>` 的两个模板参数分别对应什么？为什么 C 用 `int` 而不是 `uint8_t`？
**答案**：`T1=uint8_t` 是输入 A/B 的类型（8 位整数），`T2=int` 是输出 C 与累加的类型（32 位整数）。因为 int8 GEMM 把多次乘积累加到 C，8 位会很快溢出，必须用 32 位累加器（对应 cuBLAS 的 `CUDA_R_32I` compute type 与 `dp4a` 指令）。

**练习 2**：`main` 里 fp16 路径用了 `uint16_t`，而 fp16 Tensor Core 路径用了 `half`。两者在 `time_gemm` 里走的是同一个 `is_same` 分支吗？
**答案**：是的。[L91](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L91) 的条件是 `std::is_same<T1, uint16_t>::value || std::is_same<T1, half>::value`，两者都命中 fp16 分支。`uint16_t` 和 `half` 都是 16 位，区别只在类型系统层面，对 cuBLAS 的位级操作完全等价。

**练习 3**：为什么 `main` 里要先 `CUBLAS_DEFAULT_MATH` 跑三条、再 `CUBLAS_TENSOR_OP_MATH` 跑两条，而不是混在一起？
**答案**：math mode 是 handle 级别的全局状态，设置后会影响后续所有 `cublasGemmEx` 调用。作者把「不用 TC」的三条路径集中在一次设置下、把「用 TC」的两条路径集中在另一次设置下，避免每次调用前重复切换状态，逻辑也更清晰（见 [L224](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L224) 与 [L269](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L269)）。

---

## 5. 综合实践

**任务**：把本讲学到的「构建 → 计时 → 多精度 → CSV → 解析」整条链路串起来，画一张完整的「cuBLAS 基线数据流图」，并用公式手算一个 shape 的 TFlops。

**步骤**：

1. **构建侧**：列出 `CMakeLists.txt` 找到的三个库（cudart、cublas、curand）与最终目标架构（80），说明为什么 hopper 目录却编译成 sm_80。
2. **数据侧**：说明 `Tensor<uint16_t>` 如何经 `rand` → `curandGenerateUniform` → `convertType` kernel 得到设备上的 fp16 随机矩阵，并靠 `shared_ptr` 自动释放。
3. **计时侧**：对 shape `(m,n,k) = (8192, 8192, 8192)`、假设 cuBLAS fp16-TC 单次延迟测得 `time_us = 500`（µs）：
   - 手算 `periter_duration` 与 `numRepeats`（应得 5）。
   - 用 [u2-l4](u2-l4-benchmark-methodology.md) 的公式算 TFlops：先把返回值换算成 ms（`500/1000 = 0.5 ms`），再代入
     \[
     \text{TFlops} = \frac{2 \times 8192^3}{0.5} \times 10^{-9} \approx 2199 \text{ TFlops}
     \]
     （此为假设延迟下的算例，真实数值**待本地验证**。）
4. **输出侧**：写出这个 shape 在 CSV 里那一行的 5 个小数列分别是什么含义，并指出 `data_float16_gemm.py` 的 `[-2]` 取的是哪一列。
5. **对比侧**：用一句话总结 cuBLAS 计时（1 次 warmup + 实际 5 次重复、返回 µs）与 TileLang 计时（warmup=3、rep=20、返回 ms）的两处关键差异。

**交付物**：一张数据流图（手绘或文字描述）+ 一组手算式。这张图会让你在后续学习 Triton（[u2-l6](u2-l6-triton-baseline.md)）、TileLang（[u3-l8](u3-l8-tilelang-kernel-skeleton.md)）内核时，时刻清楚「它们的延迟是在和谁比、怎么比」。

---

## 6. 本讲小结

- cuBLAS 基线是一个 **CMake 编译的 CUDA C++ 程序**：`CMakeLists.txt` 查找 cudart/cublas/curand 依赖，目标架构被覆盖为 80（尽管默认与目录名都暗示别的数字），产出可执行文件 `cublas_benchmark`。
- `tensor.h` 的 `Tensor<T>` 用 `cudaMalloc` + `shared_ptr` 自定义删除器实现显存 RAII；`rand` 用模板特化让 cuRAND 间接支持 half/int8；`pad_dim` 为 int8 Tensor Core 把 m 补到 4 的倍数。
- `time_gemm` 是**计时心脏**：host 侧 `std::chrono` + `cudaDeviceSynchronize` 屏障，warmup 1 次、自适应重复次数 `numRepeats = max(5, 100 / (periter_us * 1e3))`——由于 `*1e3` 放大因子，对本项目大 shape **实际恒为 5**。
- `cublasGemmEx` 一个函数覆盖 fp32/fp16/int8：靠模板类型 `T1` 在编译期分派 `cudaDataType_t`，靠 `algo`（`DFALT_TENSOR_OP` vs `DFALT`）与 `cublasSetMathMode` 切换 Tensor Core。
- `main` 把 5 条精度路径拼成一行 CSV（`m,n,k,a_t,b_t,fp32,fp16,int8,fp16-TC,int8-TC`），`tee` 到 `benchmark_results.log`；**表头写 `(usec)` 但实际打印的是毫秒**，这是跨框架对比前必须统一的单位陷阱。
- 下游 `data/*.py` 用正则 `\d+\.\d+` 取 `[-2]`，即 **fp16 Tensor Core** 那一列作为 cuBLAS 在 fp16 图里的代表值。

## 7. 下一步学习建议

本讲让你彻底读懂了「编号 0」的参考基线。接下来：

1. **[u2-l6 Triton 基线](u2-l6-triton-baseline.md)**：看 Python 侧的基线如何用 `triton.testing.do_bench` 计时、用 `@triton.autotune` 调优，理解它与本讲 cuBLAS 在抽象层级与计时约定上的差异。
2. **[u2-l7 数据提取与可视化](u2-l7-data-extraction-and-plotting.md)**：深入本讲末尾提到的 `data/*.py` 与 `plot/*.py`，把「日志 → 数据 → 图表」的完整管线走一遍。
3. **[u3-l8 TileLang 内核骨架](u3-l8-tilelang-kernel-skeleton.md)**：进入 TileLang DSL，看它的 `@autotune`/`@jit` 如何用 `warmup=3, rep=20` 计时——与本讲的 `time_gemm` 形成对照。
4. **横向对照阅读**：打开 [ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu)，看 `cublasLtMatmul` 如何扩展到 fp8（e5m2/e4m3），为后续 [u4-l12 int8 与多精度 GEMM](u4-l12-int8-multiprecision-gemm.md) 做铺垫。
