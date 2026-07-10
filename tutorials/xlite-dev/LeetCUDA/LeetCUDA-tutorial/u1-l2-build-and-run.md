# 编译运行第一个 CUDA kernel

## 1. 本讲目标

本讲承接 [u1-l1 项目定位](./u1-l1-project-overview.md)，把「LeetCUDA 是什么」推进到「我怎么把它跑起来」。学完本讲你应该能够：

- 看懂 README 里给出的三条 `nvcc` 编译命令，并知道每个参数在做什么。
- 区分 `sm_89`（Ada）与 `sm_90a`（Hopper）两类架构选项，以及 `NOTES_V2_ENABLE_CUTE`、`NOTES_V2_ENABLE_WGMMA` 两个条件编译宏开关的作用。
- 运行 `notes-v2.cu` 的 verification harness，并能解读表格里的 `Max Err` 与 `Pass` 为什么这样取值、为什么误差是「可接受的」。

本讲的练习不要求你必须有 GPU：我们把「有 GPU 就亲手编译运行」和「没有 GPU 就做源码阅读型实践」两条路径都给出。

## 2. 前置知识

在进入编译之前，先用大白话对齐几个概念：

- **nvcc**：NVIDIA 提供的 CUDA C/C++ 编译器，作用类似 `gcc`/`clang`，但它能把 `.cu` 文件里「 host 端的 C++ 」和「 device 端的 kernel 」分开编译，最后链接成一个可执行文件。本仓库的 `notes-v2.cu` 是一个**单文件、自带 `main()` 的自包含程序**，因此可以直接用一行 `nvcc` 命令编译，不需要 `Makefile` 或 `setup.py`。
- **架构（arch / compute capability）**：不同代际的 GPU 支持的指令集不同。`sm_89` 对应 Ada Lovelace（如 RTX 4090、L20），`sm_90a` 对应 Hopper（如 H100、H200）。`-arch=sm_89` 表示「为 Ada 生成机器码」；`sm_90a` 末尾的 `a` 表示开启 Hopper Tensor Core 的高级特性（WGMMA、TMA）。
- **条件编译（宏开关）**：用 `-DXXX` 在命令行定义一个宏，源码里 `#if defined(XXX)` 的段落才会被编译进去。LeetCUDA 用这个机制把「依赖 CUTLASS 头文件的 CuTe 代码」和「依赖 Hopper 硬件的 WGMMA 代码」做成可选编译，从而让一份源码能在不同 GPU、不同依赖环境下都能编过。
- **链接库 `-l`**：`-lcublas` 链接 cuBLAS（NVIDIA 官方 BLAS 库，harness 用它当参考答案），`-lcuda` 链接 CUDA Driver API（TMA 的 `cuTensorMapEncodeTiled` 需要）。
- **verification harness（验证脚手架）**：一个 `main()` 函数，依次运行几十个 kernel，每个都和「CPU 参考实现或 cuBLAS 实现」比对，算出逐元素最大误差 `Max Err`，再按阈值判 `PASS`/`FAIL` 打印成一张表。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md) | 项目首页，`Quick Start` 小节给出编译命令与一份示例 harness 输出表。 |
| [kernels/interview/notes-v2.cu](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu) | 单文件面试笔记程序，包含 Phase 0~8 的 kernel 实现与 `main()` 验证脚手架，文件末尾有「Quick build & run reference」注释。 |

本讲重点引用 `notes-v2.cu` 的三处：文件头部的 `#include` 区、`main()` 验证脚手架、以及文件末尾的编译命令注释。

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块：

1. **Quick Start 编译命令与 nvcc 参数**：先把程序编出来。
2. **条件编译宏：CuTe / WGMMA 与架构 sm_89 / sm_90a**：理解为什么有「三种编译产物」。
3. **verification harness：Max Err / Pass 是怎么来的**：理解那张输出表。

### 4.1 Quick Start 编译命令与 nvcc 参数

#### 4.1.1 概念说明

`notes-v2.cu` 是一个「单文件、自包含」的程序：它自己实现了所有 kernel，也自带 `main()` 来逐个验证。这意味着我们**不需要任何构建系统**，一条 `nvcc` 命令就能从源码得到可执行文件。这是 LeetCUDA 给初学者降低门槛的关键设计——你可以把全部注意力放在 kernel 本身，而不是工程配置上。

README 的 `Quick Start` 小节给出了三条命令，分别对应「纯 sm_89」、「sm_89 + CuTe」、「sm_90a + WGMMA」三种编译产物。

#### 4.1.2 核心流程

编译运行的整体流程是：

1. 拉取 CUTLASS 子模块（因为 CuTe 编译需要它的头文件）。
2. `cd kernels/interview` 进入源码目录（命令里的相对路径 `-I ../../third-party/cutlass/include` 依赖这个工作目录）。
3. 选一条 `nvcc` 命令编译，得到 `.bin`。
4. 直接 `./notes_v2_sm89.bin` 运行，终端会打印验证表。

用伪代码描述 `nvcc` 一行命令的语义：

```
nvcc <语言标准> <优化级别> <目标架构> [可选宏] [可选头文件路径] <链接库> <源文件> -o <输出>
```

#### 4.1.3 源码精读

README 的三条命令就在 Quick Start 代码块里：[README.md:L41-L47](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L41-L47)。这里逐个参数对照：

```bash
# 1) 先更新子模块（CUTLASS 头文件所在）
git submodule update --init --recursive --force && cd kernels/interview

# 2) sm_89 纯编译（Ada）
nvcc -std=c++20 -O2 -arch=sm_89 -lcublas -lcuda notes-v2.cu -o notes_v2_sm89.bin

# 3) sm_89 + CuTe
nvcc -std=c++20 -O2 -arch=sm_89 -DNOTES_V2_ENABLE_CUTE \
  -I ../../third-party/cutlass/include -lcublas -lcuda notes-v2.cu -o notes_v2_cute_sm89.bin

# 4) sm_90a + WGMMA（Hopper）
nvcc -std=c++20 -O2 -gencode arch=compute_90a,code=sm_90a -DNOTES_V2_ENABLE_WGMMA \
  -lcublas -lcuda notes-v2.cu -o notes_v2_sm90.bin
```

各参数含义如下表：

| 参数 | 含义 | 为什么需要 |
| --- | --- | --- |
| `-std=c++20` | 使用 C++20 标准 | 源码用了较新的 C++ 特性 |
| `-O2` | 开启 O2 优化 | 性能 |
| `-arch=sm_89` | 为 Ada 架构生成代码 | 决定能用哪些 PTX 指令 |
| `-gencode arch=compute_90a,code=sm_90a` | 显式指定 Hopper 架构 + `a` 后缀 | WGMMA/TMA 需要 `sm_90a` |
| `-DNOTES_V2_ENABLE_CUTE` | 定义 CUTE 宏 | 启用 CuTe（CUTLASS DSL）相关代码段 |
| `-DNOTES_V2_ENABLE_WGMMA` | 定义 WGMMA 宏 | 启用 Hopper WGMMA/TMA 相关代码段 |
| `-I ../../third-party/cutlass/include` | 增加 CUTLASS 头文件搜索路径 | CuTe 的 `#include <cute/tensor.hpp>` 在这里 |
| `-lcublas` | 链接 cuBLAS | harness 用 cuBLAS 当 GEMM 参考答案 |
| `-lcuda` | 链接 CUDA Driver API | TMA 的 `cuTensorMapEncodeTiled` 属于 Driver API |

> 提示：如果你跳过了 `git submodule update`，`third-party/cutlass/include` 目录会是空的（本仓库在未初始化子模块时就是如此），那么命令 3（CuTe）会因找不到 `<cute/tensor.hpp>` 而编译失败。命令 2（纯 sm_89）不依赖 CUTLASS，不受影响。

源码文件末尾还把这几条命令作为注释留了一份「Quick build & run reference」，方便你不查 README 也能编译：[kernels/interview/notes-v2.cu:L4935-L4955](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4935-L4955)。

#### 4.1.4 代码实践

**实践目标**：亲手把 `notes-v2.cu` 编译并运行，得到验证表（或在无 GPU 时完成等价的源码阅读任务）。

**操作步骤（有 GPU 路径）**：

1. 在仓库根目录执行 `git submodule update --init --recursive --force`（若只想编纯 sm_89 可跳过）。
2. `cd kernels/interview`。
3. 执行命令 2：`nvcc -std=c++20 -O2 -arch=sm_89 -lcublas -lcuda notes-v2.cu -o notes_v2_sm89.bin`。
4. 运行 `./notes_v2_sm89.bin`（可选传 3 个参数覆盖 GEMM 的 M/N/K）。

**操作步骤（无 GPU / 源码阅读路径）**：

1. 打开 [kernels/interview/notes-v2.cu:L4938-L4955](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4938-L4955) 的注释块。
2. 把 4 条命令抄下来，逐条标注每个参数的作用（参考上面的参数表）。
3. 解释：为什么命令 4（WGMMA）用 `-gencode arch=compute_90a,code=sm_90a` 而不是 `-arch=sm_90`？

**需要观察的现象**：终端打印 `=== notes-v2.cu verification harness ===` 开头的表格，最后以 `=== All tests done ===` 收尾。

**预期结果**：所有 `Pass` 列应为 `PASS`（README 给出的参考输出里全部为 `PASS`）。`Max Err` 列在不同 kernel 上数量级差异很大（从 `0.0e+00` 到 `1.6e-04`），这是正常的——4.3 节会解释原因。**待本地验证**：实际数值会因驱动/cuBLAS 版本略有浮动。

#### 4.1.5 小练习与答案

**练习 1**：为什么命令里要 `-std=c++20` 而不是默认标准？

> **答**：`notes-v2.cu` 使用了 C++20 特性（例如 `<cuda/barrier>` 协作同步原语及其相关写法）。若用更老的标准，编译器会在这些地方报错。

**练习 2**：把命令 2 里的 `-arch=sm_89` 改成 `-arch=sm_70`，会发生什么？

> **答**：sm_70（Volta）不支持很多本文件用到的指令（如 Ampere 起的 `cp.async`、Hopper 的 WGMMA/TMA，以及 Tensor Core 的部分 PTX）。编译期或运行期会报「架构不支持」类错误。这正是为什么 README 为不同 GPU 给出了不同命令。

### 4.2 条件编译宏：CuTe / WGMMA 与架构 sm_89 / sm_90a

#### 4.2.1 概念说明

`notes-v2.cu` 把「能在任何 SM80+ GPU 上跑的代码」和「只有装了 CUTLASS 头文件才能编的 CuTe 代码」「只有 Hopper 硬件才能跑的 WGMMA/TMA 代码」都放进同一个文件，靠两个宏来切换：

- `NOTES_V2_ENABLE_CUTE`：开启 CuTe（CUTLASS 的 DSL）实现的 HGEMM，需要额外提供 CUTLASS 头文件路径。
- `NOTES_V2_ENABLE_WGMMA`：开启 Hopper 的 WGMMA（warpgroup 级矩阵乘）和 TMA（Tensor Memory Accelerator），需要 `sm_90a` 架构和 CUDA Driver API。

这样做的好处：读者可以先不管高级特性，用最简单的命令 2 跑通大部分 kernel；等需要时再加宏、换架构，解锁更高级的实现。

#### 4.2.2 核心流程

条件编译的运行逻辑：

```
nvcc 命令行是否带 -DNOTES_V2_ENABLE_CUTE ?
  是 -> 源码里 #if defined(NOTES_V2_ENABLE_CUTE) ... #endif 段被编译
        （需要 -I 指向 cutlass/include 才能找到 <cute/tensor.hpp>）
  否 -> 该段被预处理掉，CUTLASS 头文件也不必存在

同理 NOTES_V2_ENABLE_WGMMA 控制是否编译 WGMMA/TMA 段
  且 main() 里对应的 cuInit(0) 与 test_hgemm_wgmma() 也被同一宏门控
```

#### 4.2.3 源码精读

**宏门控 CuTe 的代码**：CuTe 的头文件和 HGEMM CuTe kernel 都包在同一对 `#if/#endif` 里，[kernels/interview/notes-v2.cu:L1951-L1953](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L1951-L1953)：

```cpp
#if defined(NOTES_V2_ENABLE_CUTE)
#include <cute/tensor.hpp>   // 需要 -I ../../third-party/cutlass/include
...
```

> 这段中文说明：只有命令行定义了 `NOTES_V2_ENABLE_CUTE`，编译器才会去 `#include <cute/tensor.hpp>`，因此也才需要 `-I` 指向 CUTLASS 头文件目录。

**宏门控 WGMMA/TMA 的代码**：TMA descriptor 的创建走的是 CUDA Driver API，[kernels/interview/notes-v2.cu:L2904-L2911](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L2904-L2911) 的面试要点注释直接点明了「需 `#include <cuda.h>` 并链接 `-lcuda`」，并且 `cuTensorMapEncodeTiled` 只在 `sm_90a+` 可用。

**`main()` 也被宏门控**：`main` 入口里对 WGMMA 的初始化和测试调用都包在宏里，[kernels/interview/notes-v2.cu:L4896-L4901](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4896-L4901)：

```cpp
int main(int argc, char *argv[]) {
#if defined(NOTES_V2_ENABLE_WGMMA)
  cuInit(0); // Driver API init required for cuTensorMapEncodeTiled (TMA, sm_90a+)
#endif
  ...
```

> 这段中文说明：只有编译时定义了 WGMMA 宏，才会调用 `cuInit(0)` 初始化 Driver API——因为 TMA descriptor 依赖它。这也解释了为什么 `-lcuda` 是必需的链接库。

对应的测试调用同样被宏门控，[kernels/interview/notes-v2.cu:L4923-L4929](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4923-L4929)：

```cpp
#if (defined(NOTES_V2_ENABLE_CUTE))
  test_hgemm_cute(M, N, K);
#endif
#if (defined(NOTES_V2_ENABLE_WGMMA))
  test_hgemm_wgmma(M, N, K);
#endif
```

> 这段中文说明：harness 表格里的 `HGEMM CuTe` 和 `HGEMM WGMMA` 两行只有在开启对应宏时才会出现。所以你用命令 2 编译的输出表会比 README 的示例少这两行——这是预期的，不是 bug。

#### 4.2.4 代码实践

**实践目标**：通过「故意改宏」验证条件编译的效果（源码阅读型）。

**操作步骤**：

1. 阅读 [kernels/interview/notes-v2.cu:L4896-L4933](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4896-L4933) 的整个 `main()`。
2. 假设你用命令 2（只有 sm_89，无任何宏）编译。请预测：输出表里会出现 `HGEMM CuTe`、`HGEMM WGMMA` 这两行吗？
3. 再假设你用命令 4（`-DNOTES_V2_ENABLE_WGMMA` + `sm_90a`）编译。请预测：输出表会出现哪一行、不出现哪一行？（注意命令 4 没有定义 CUTE 宏。）

**需要观察的现象 / 预期结果**：

- 命令 2 的表：**不**含 `HGEMM CuTe`、**不**含 `HGEMM WGMMA`；其余 kernel 行正常。
- 命令 4 的表：**含** `HGEMM WGMMA`，但**仍不含** `HGEMM CuTe`（因为没定义 CUTE 宏）。

**待本地验证**：在真实 Hopper 机器上分别用命令 2、命令 4 编译并对比两张表，确认上述预测。

#### 4.2.5 小练习与答案

**练习 1**：如果想同时评测 CuTe 和 WGMMA，该用哪条命令？

> **答**：用文件末尾注释里的「sm_90a + CuTe + WGMMA」命令，即同时带 `-DNOTES_V2_ENABLE_CUTE -DNOTES_V2_ENABLE_WGMMA`、`-gencode arch=compute_90a,code=sm_90a` 并 `-I ../../third-party/cutlass/include`。参考 [kernels/interview/notes-v2.cu:L4951-L4955](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4951-L4955)。

**练习 2**：为什么 CuTe 路径需要 `-I`，而 WGMMA 路径不需要？

> **答**：CuTe 是 CUTLASS 提供的 **header-only DSL**，源码里 `#include <cute/tensor.hpp>`，必须用 `-I` 告诉编译器去哪找这个头文件；WGMMA/TMA 用的是 CUDA 自带的 Driver API（`<cuda.h>`）和 PTX 内联汇编，随 CUDA Toolkit 安装，无需额外头文件路径。

### 4.3 verification harness：Max Err / Pass 是怎么来的

#### 4.3.1 概念说明

`notes-v2.cu` 的 `main()` 是一个 verification harness（验证脚手架）。它的设计哲学和 LeetCUDA 全书一致（见 u1-l1）：**不要求 kernel 输出与参考答案逐比特相同，只要求「最大逐元素误差」小于一个与算子相关的阈值**。这是 GPU 浮点编程的常态——`f16` 矩阵乘累加 1024 次本就会有可见误差，关键在于误差是否在「可用范围」内。

harness 会为每个 kernel 做四件事：①准备输入；②跑自己的 kernel；③跑参考实现（CPU 公式或 cuBLAS）；④逐元素求最大绝对误差 `Max Err`，按阈值判 `PASS`/`FAIL`。

#### 4.3.2 核心流程

单个 `test_xxx` 函数的内部流程：

```
1. malloc / cudaMalloc 分配 host + device 内存
2. 随机或固定模式初始化输入 h_x
3. cudaMemcpy 把输入拷到 device
4. kernel<<<grid,block>>>(...)  跑自己的实现
5. cudaDeviceSynchronize()       等跑完
6. cudaMemcpy 把结果拷回 host h_y
7. 在 host 上算 expected（CPU 参考实现，或调 cuBLAS）
8. for i in N: max_err = max(max_err, |h_y[i] - expected[i]|)
9. printf("| Name | max_err | max_err < 阈值 ? PASS : FAIL |")
```

阈值不是统一的，而是**按算子的精度特性分别设定**：

| 算子类别 | 阈值 | 为什么这样取 |
| --- | --- | --- |
| `MatTranspose` | `1e-6` | 纯数据搬运，应几乎精确 |
| `ReLU` / `ElemwiseAdd` / `Histogram` | `1e-4` | `f32` 逐元素，误差极小 |
| `Softmax` / `Norm` / `RoPE` | `1e-4` | `f32`，含归约/超越函数 |
| `Dot` / `SGEMV` / `SGEMM` | `1e-2` | `f32` 乘加累加，阈值放宽 |
| `HGEMM MMA/Swizzle/CuTe/WGMMA` | `1.0` | `f16` 输入累加，误差大 |
| `FlashAttn-SplitQ` | `1e-1` | `f16` + online softmax 多次修正 |

#### 4.3.3 源码精读

**harness 的总入口**：[kernels/interview/notes-v2.cu:L4896-L4933](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4896-L4933) 打印表头，然后依次调用所有 `test_xxx`，最后打印 `=== All tests done ===`：

```cpp
printf("=== notes-v2.cu verification harness ===\n");
printf("| %-35s | %-12s | %-4s |\n", "Kernel", "Max Err", "Pass");
...
test_relu(1024);
test_softmax(256);
test_sgemm(M, N, K);
...
printf("=== All tests done ===\n");
```

> 这段中文说明：`main()` 支持 `./bin M N K` 三个可选命令行参数来覆盖 GEMM 的规模（默认 1024×1024×1024），其余 kernel 用固定的小规模输入验证正确性。

**一个完整的「算误差 + 判 PASS」范例**：ReLU 测试，[kernels/interview/notes-v2.cu:L3700-L3711](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L3700-L3711)：

```cpp
relu<<<grid256, block256>>>(d_x, d_y, N);
check(cudaDeviceSynchronize(), "relu sync");
check(cudaMemcpy(h_y, d_y, (size_t)N * sizeof(float), cudaMemcpyDeviceToHost), "relu D2H");
float max_err = 0.0f;
for (int i = 0; i < N; i++) {
  float expected = fmaxf(0.0f, h_x[i]);   // CPU 参考实现
  float err = fabsf(h_y[i] - expected);
  if (err > max_err) max_err = err;
}
printf("| %-35s | %.6e | %-4s |\n", "ReLU", max_err, max_err < 1e-4f ? "PASS" : "FAIL");
```

> 这段中文说明：`expected = fmaxf(0.0f, h_x[i])` 就是 CPU 算的 ReLU 参考答案；`max_err < 1e-4f` 是 ReLU 的判据。对纯 `f32` 逐元素运算，ReLU 实测 `Max Err` 通常是 `0.0e+00`（与 README 示例一致）。

**阈值随精度放宽的范例**：HGEMM MMA 用 `f16` 累加，阈值放到 `1.0`，[kernels/interview/notes-v2.cu:L4518-L4523](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4518-L4523)：

```cpp
float max_err = 0.0f;
for (int i = 0; i < M * N; i++) {
  float err = fabsf(__half2float(h_c[i]) - __half2float(h_c_ref[i]));
  if (err > max_err) max_err = err;
}
printf("| %-35s | %.6e | %-4s |\n", "HGEMM MMA", max_err, max_err < 1.0f ? "PASS" : "FAIL");
```

> 这段中文说明：参考答案 `h_c_ref` 来自 cuBLAS（这就是为什么要 `-lcublas`）。`f16` 在 K=1024 上反复乘加累加，单元素误差可能到零点几甚至接近 1，所以阈值取 `1.0`。即使如此，README 示例里它仍报 `0.0e+00`——这是因为该 kernel 用了高精度累加路径，误差很小，离 `1.0` 阈值非常远。

**最宽松的阈值**：FlashAttention 因为是 `f16` + online softmax 多块状态合并，阈值放到 `1e-1`，[kernels/interview/notes-v2.cu:L4888](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4888)，这也解释了 README 示例里 `FlashAttn-SplitQ` 的 `1.646988e-04` 是完全正常的、远小于阈值的 PASS。

#### 4.3.4 代码实践

**实践目标**：从一张 harness 输出表里，挑选 3 个 kernel，解释它们 `Max Err` 的数量级为何「可接受」。

**操作步骤**：

1. 打开 README 的 Quick Start 输出表：[README.md:L48-L81](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L48-L81)。
2. 挑这 3 个 kernel 记录 `Max Err` 与 `Pass`：
   - `MatTranspose`：`0.000000e+00` / `PASS`
   - `SafeSoftmax`：`1.862645e-09` / `PASS`
   - `FlashAttn-SplitQ`：`1.646988e-04` / `PASS`
3. 对照 4.3.2 的阈值表，逐个解释为什么这些误差可接受。

**需要观察的现象 / 预期结果**：

- `MatTranspose` 阈值 `1e-6`，实测 `0`：转置是纯搬运，理应精确，通过理所当然。
- `SafeSoftmax` 阈值 `1e-4`，实测 `~1.9e-9`：`f32` 归约+`exp` 的舍入误差，远小于阈值。
- `FlashAttn-SplitQ` 阈值 `1e-1`，实测 `~1.6e-4`：`f16` + 多块 online softmax 合并的累积误差，仍比阈值小 3 个数量级。

**结论**：所谓「可接受」= `Max Err` 远小于该算子的阈值；阈值本身是按算子的精度特性（`f32`/`f16`、是否累加、是否多块合并）设定的合理工程上限，而不是 0。

> 说明：以上数值取自 README 示例输出。**待本地验证**：你本机的实测值会因 GPU 型号、驱动、cuBLAS 版本不同而浮动，但只要 `Pass` 列为 `PASS` 即代表正确。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MatTranspose` 的阈值是 `1e-6`，而 `HGEMM MMA` 的阈值是 `1.0`？

> **答**：转置不改变数值、不做算术，只是搬位置，`f32` 下应几乎无误差，所以阈值很严（`1e-6`）；HGEMM 是 `f16` 输入在 K 维反复乘加累加，`f16` 的精度有限，单元素误差天然较大，所以阈值放到 `1.0`。阈值反映的是「该算子在它的精度下能合理达到的准确度」。

**练习 2**：如果你改了某个 kernel 后，它的 `Max Err` 从 `1e-7` 变成了 `5e-2`，但 `Pass` 仍是 `PASS`（阈值 `1e-1`），你应该放心吗？

> **答**：不能完全放心。虽然没跌破阈值，但误差骤升 5 个数量级，通常意味着改动引入了精度退化（比如把 `f32` 累加换成了 `f16` 累加，或漏了 safe softmax 的减最大值）。`PASS` 只说明「还在可接受范围」，误差数量级的突变是回归信号，值得排查。

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「从命令到结论」的小任务：

**任务**：为你的目标 GPU 选定一条编译命令，预测并解释它会产生的 harness 输出表。

1. **选命令**：假设你的机器是一张 RTX 4090（Ada）。从 4.1 的三条命令里选出合适的一条（提示：4090 是 `sm_89`，且你想要最大覆盖，所以应该带上 CUTE 宏，但不需要 WGMMA）。
2. **列依赖**：写出这条命令必须的两个前提条件（子模块、工作目录）。
3. **预测表**：根据 4.2 的条件编译门控，预测输出表里**会出现** `HGEMM CuTe` 行吗？**会出现** `HGEMM WGMMA` 行吗？
4. **解读一行**：在该表的 `HGEMM MMA` 行，`Max Err` 即使是 `0.0e+00`，阈值仍是 `1.0`。用一句话说明为什么阈值设得这么宽松（参考 4.3）。

**参考答案**：

1. 命令 3（`sm_89 + CUTE`）。
2. ①先 `git submodule update --init --recursive --force` 拉 CUTLASS 头文件；②`cd kernels/interview`（因为 `-I ../../third-party/cutlass/include` 是相对当前目录的）。
3. **会出现** `HGEMM CuTe`（定义了 CUTE 宏）；**不会出现** `HGEMM WGMMA`（未定义 WGMMA 宏，且 4090 也不是 Hopper）。
4. 因为 HGEMM 是 `f16` 输入在 K 维反复乘加累加，`f16` 精度有限、单元素误差天然较大，阈值取 `1.0` 是给该精度留的合理工程余量；实测为 `0` 只说明该实现恰好用了高精度累加路径。

## 6. 本讲小结

- `notes-v2.cu` 是**单文件自包含**程序，一条 `nvcc` 命令即可编译运行，无需构建系统。
- 三条命令分别对应 `sm_89`、`sm_89 + CuTe`、`sm_90a + WGMMA`；核心参数是 `-arch`/`-gencode`（架构）、`-D`（宏）、`-I`（CUTLASS 头文件）、`-lcublas`/`-lcuda`（链接库）。
- `NOTES_V2_ENABLE_CUTE` 和 `NOTES_V2_ENABLE_WGMMA` 是条件编译开关，分别门控 CuTe 段（需 CUTLASS 头文件）和 WGMMA/TMA 段（需 `sm_90a` + Driver API `cuInit`）。
- harness 的 `main()` 会逐个跑 kernel，与 CPU/cuBLAS 参考实现比对 `Max Err`，按**算子相关阈值**判 `PASS`/`FAIL`。
- 阈值随精度放宽：转置 `1e-6`、`f32` 算子 `1e-4`/`1e-2`、`f16` GEMM `1.0`、FlashAttention `1e-1`；「可接受」= 远小于对应阈值。
- 用命令 2（无宏）编译的表会比 README 示例少 `HGEMM CuTe`、`HGEMM WGMMA` 两行——这是条件编译的预期结果。

## 7. 下一步学习建议

- 下一讲 [u1-l3 目录结构与 kernel 模块约定](./u1-l3-directory-and-module-convention.md) 会从「单文件」回到「多模块」视角，讲解 `kernels/<算子>/` 下 `README.md + <kernel>.cu + <kernel>.py` 三件套的工程约定。
- 想深入参数与硬件对应关系，可阅读 [kernels/interview/notes-v2.cu:L26-L40](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L26-L40) 的 GPU 架构与内存层次速查注释（Phase 0）。
- 想了解 `cuTensorMapEncodeTiled` / WGMMA 细节，可先读 [kernels/interview/notes-v2.cu:L2904-L2977](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L2904-L2977) 的 TMA 段，对应后续专家层 U13。
