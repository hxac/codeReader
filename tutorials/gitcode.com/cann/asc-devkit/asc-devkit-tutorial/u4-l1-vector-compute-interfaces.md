# 矢量计算接口体系（双目/单目/比较选择）

## 1. 本讲目标

上一讲（u3-l3）我们已经能把数据在 GM 与 UB 之间搬来搬去，并学会了用 `LocalMemAllocator` 自主管理 UB 内存。但「搬运」本身不产生计算结果——真正让算子有意义的，是把 UB 里的数据「算」一遍。

本讲就打开 Ascend C 基础 API 的**矢量计算接口体系**，读完本讲你应当能够：

- 说清**双目接口**（`Add`/`Sub`/`Mul`/`Div`/`Max`/`Min` 等）、**单目接口**（`Exp`/`Sqrt`/`Abs` 等）、**比较选择类接口**（`Compare`/`Select`）各自的作用与典型用法。
- 理解贯穿三类接口的两个关键设计：接口**分级**（Level 2 的 `count` 模式 vs Level 0 的 `mask/repeat` 模式），以及**元素计数 `count` 的含义与约束**。
- 看懂为什么矢量计算之间需要 `PipeBarrier<PIPE_V>()`，并能够用基础 API 把多步矢量运算（如 `z = exp(x+y)`）串起来。

## 2. 前置知识

本讲默认你已经掌握 u3 系列建立的认知，下面三句话复习关键点：

- **LocalTensor 是 UB 上的已分配缓冲**：矢量计算接口的输入输出几乎都是 `LocalTensor<T>`，即数据必须先在片上 UB 里，才能被 Vector 计算单元处理。
- **三条独立流水线**：搬运 `DataCopy` 走 MTE2/MTE3 流水线，矢量计算走 **V 流水线**。它们可以并行、乱序执行，因此跨流水线、甚至同一条 V 流水线上的「写后读」依赖都需要显式 `PipeBarrier` 来保障顺序。
- **自主式范式**：基础 API 由你自己分配内存（`LocalMemAllocator`）、自己加同步（`PipeBarrier`），框架不会替你做。

还有一个术语先点透：**SIMD（Single Instruction Multiple Data）**。Ascend C 的矢量计算接口本质是 SIMD 指令——一条指令同时处理一批元素（一个「块」block）。这就是为什么参数里总是出现 `count`（算多少个元素）、`mask`（这一次算哪些元素）、`repeat`（重复多少轮）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/basic_api/kernel_operator_vec_binary_intf.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_binary_intf.h) | **双目接口**声明：`Add`/`Sub`/`Mul`/`Div`/`Max`/`Min`/`And`/`Or` 及融合型 `AddRelu`/`MulAddDst` 等 |
| [include/basic_api/kernel_operator_vec_unary_intf.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_unary_intf.h) | **单目接口**声明：`Exp`/`Ln`/`Sqrt`/`Rsqrt`/`Abs`/`Reciprocal`/`Relu`/`Neg`/`Not` |
| [include/basic_api/kernel_operator_vec_cmpsel_intf.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_cmpsel_intf.h) | **比较选择接口**声明：`Compare`/`Compares`/`Select` |
| [include/basic_api/kernel_struct_binary.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_struct_binary.h) | `BinaryRepeatParams` 结构体（Level 0 的步长参数） |
| [impl/basic_api/utils/kernel_utils_mode.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/utils/kernel_utils_mode.h) | `CMPMODE`（比较模式）/`SELMODE`（选择模式）枚举 |
| [impl/basic_api/kernel_operator_vec_binary_intf_impl.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_operator_vec_binary_intf_impl.h) | 双目接口的 Level 2 实现，能看到 `count` 如何被校验与下发 |
| [examples/.../01_add/add/add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) | 矢量加法样例，本讲的主改写对象 |
| [examples/.../gelu_high_performance/gelu.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/05_best_practices/02_reg_compute/gelu_high_performance/gelu.asc) | 真实的多步矢量计算样例（`Mul`→`Add`→`Exp` 链），佐证本讲的串联写法 |

> 提示：这三个头文件都被主入口 `kernel_operator.h` 间接聚合，所以样例里只需 `#include "kernel_operator.h"` 即可使用全部矢量计算接口。

## 4. 核心概念与源码讲解

### 4.1 双目计算接口与矢量计算分级模型

#### 4.1.1 概念说明

**双目接口**处理「两个输入 Tensor 逐元素运算、得到一个输出 Tensor」的场景，是出现频率最高的一类矢量算子。代表接口与语义：

| 接口 | 语义 | 备注 |
| --- | --- | --- |
| `Add` | `dst = src0 + src1` | 算术加 |
| `Sub` | `dst = src0 - src1` | 算术减 |
| `Mul` | `dst = src0 * src1` | 算术乘 |
| `Div` | `dst = src0 / src1` | 算术除（3510/5102 多一个 `DivConfig`） |
| `Max` | `dst = src0 > src1 ? src0 : src1` | 逐元素取大 |
| `Min` | `dst = src0 > src1 ? src1 : src0` | 逐元素取小 |
| `And` / `Or` | 按位与 / 按位或 | 整型按位运算 |
| `MulAddDst` | `dst = src0 * src1 + dst` | **融合型**，一条指令完成乘加 |

除基本运算外，头文件还提供一批**融合型双目接口**——把两步基本运算压成一条硬件指令，既省指令数也省中间来回，如 `AddRelu(dst = Relu(src0+src1))`、`SubRelu`、`MulAddRelu(dst = relu(src0*dst+src1))`、`AbsSub(dst = abs(src0-src1))`、`ExpSub(dst = e^(src0-src1))` 等。初学时记住「有同名融合接口就优先用」即可，性能章节（U13）会再细讲。

更重要的是，所有矢量接口都遵循一个统一的**分级模型**，这是理解参数 `count` 的钥匙：

- **Level 2（count 模式）**：最常用、最易写。你只告诉接口「算 `count` 个连续元素」，底层自动算出 mask、repeat 次数和默认步长。
- **Level 0（mask/repeat 模式）**：面向极致性能与非常规数据布局。你手动指定 `mask`（每轮算哪些元素）、`repeatTime`（重复几轮）、`repeatParams`（块间/轮间步长），可以处理跨步、分块等复杂布局。

本讲面向初学者，**全部使用 Level 2 的 count 模式**；Level 0 留到性能与搬运优化章节（U13）展开。

#### 4.1.2 核心流程

一次 Level 2 双目计算（以 `Add` 为例）的执行流程：

```text
Add(dst, src0, src1, count)
        │
        ▼
①  CheckVectorTensor(dst, src0, src1)   // 校验都是 UB 上的 LocalTensor
②  CheckCalcount(count)                  // 校验 count >= 0
        │
        ▼
③  AddImpl(dst.GetPhyAddr(), src0.GetPhyAddr(), src1.GetPhyAddr(), count)
        │  （把 count 内部换算成 mask + repeatTime + 默认步长）
        ▼
④  下发一条 V 流水线的 SIMD 指令，按块（block）批量处理 count 个元素
        │
        ▼
⑤  返回；指令异步执行，若后续要用 dst，必须 PipeBarrier<PIPE_V>()
```

几个要点：

- **`count` 是「元素个数」而非字节数**。对 `LocalTensor<float>` 传 `count=2048` 表示算 2048 个 float（即 8192 字节）。
- **`count` 的硬约束是非负**，见下方源码精读里的 `CheckCalcount`。出于效率，`count` 一般取「自然块大小」（32 字节，即 8 个 float 或 16 个 half）的整数倍；不足一块的零头由实现处理，但连续、对齐的大块性能最佳。
- **V 流水线依赖要手动屏障**。`Add` 写 `dst`、紧接着的 `Exp` 读同一个 `dst`，属于同一条 V 流水线上的「写后读（RAW）」，必须插 `PipeBarrier<PIPE_V>()`。从搬运（MTE2）到计算（V）的跨流水线依赖，则用 `PipeBarrier<PIPE_ALL>()`（样例 add.asc 的写法）。

把 count 换算成 mask/repeat 的「自然块」直觉可以这样理解（仅作示意，具体由底层换算）：

\[ \text{repeatTime} = \left\lceil \frac{\text{count}}{\text{每轮元素数}} \right\rceil,\qquad \text{mask} = \text{每轮内有效元素位} \]

其中「每轮元素数」由数据类型位宽决定（位宽越小，一轮能装下的元素越多）。

#### 4.1.3 源码精读

**(1) Add 的 Level 2 声明** —— 三个 `LocalTensor` 入参 + 一个 `count`，这就是最常用的形态：

[include/basic_api/kernel_operator_vec_binary_intf.h:73-83](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_binary_intf.h#L73-L83) —— 声明 `Add(dst, src0, src1, count)`，注释明确 `count` 是「Number of data involved in calculation」（参与计算的元素个数）。

与之对照的 **Level 0 声明**：

[include/basic_api/kernel_operator_vec_binary_intf.h:46-71](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_binary_intf.h#L46-L71) —— 多出 `mask`/`mask[]`、`repeatTime`、`BinaryRepeatParams repeatParams`，用于精细控制每轮算哪些元素、块与块之间如何跨步。`BinaryRepeatParams` 的字段定义在：

[include/basic_api/kernel_struct_binary.h:45-68](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_struct_binary.h#L45-L68) —— 含 `dstBlkStride/src0BlkStride/src1BlkStride`（块间步长）与 `dstRepStride/src0RepStride/src1RepStride`（轮间步长），均有默认值。默认值来自 `DEFAULT_BLK_NUM=8`、`DEFAULT_BLK_STRIDE=1`、`DEFAULT_REPEAT_STRIDE=8`（见 `kernel_utils_constants.h:28-33`）。

**(2) Max / Min** —— 比较类双目，语义是逐元素取大/取小：

[include/basic_api/kernel_operator_vec_binary_intf.h:302-311](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_binary_intf.h#L302-L311) 是 `Max` 的 Level 2 声明；`Min` 的 Level 2 声明在 [include/basic_api/kernel_operator_vec_binary_intf.h:343-353](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_binary_intf.h#L343-L353)。

**(3) Level 2 的实现：count 如何被校验与下发** —— 这是看「count 约束」最直接的地方：

[impl/basic_api/kernel_operator_vec_binary_intf_impl.h:124-144](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_operator_vec_binary_intf_impl.h#L124-L144) —— Level 2 的 `Add` 先 `CheckVectorTensor` 校验三个 Tensor 都在 UB，再 `CheckCalcount(count)`，最后把裸 `__ubuf__` 指针和 `count` 一并下发给 `AddImpl`。其中 `CheckCalcount` 的定义只断言一件事：

[impl/basic_api/kernel_npu_debug.h:349-357](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_npu_debug.h#L349-L357) —— `calcount >= 0`。这印证了 count 的 API 层硬约束就是「非负」，至于对齐与块大小带来的效率差异由底层换算处理。

**(4) 真实样例 add.asc 中的 Add 调用** —— 把上面所有概念落到一行代码：

[examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc:42-47](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L42-L47) —— 先 `DataCopy` 把 x、y 从 GM 搬到 UB（MTE2 流水线），`PipeBarrier<PIPE_ALL>()` 等搬运完成，再 `Add(zLocal, xLocal, yLocal, blockLength)` 做 Vector 计算（`blockLength` 即 count=2048）。注意这里的 `PipeBarrier<PIPE_ALL>()` 是为「下一步若复用 zLocal」预留的安全屏障。

> 顺带注意：双目接口支持的数据类型以 `half / float / int16_t / int32_t / uint16_t / uint32_t` 为主（可在 `impl/basic_api/dav_l311/kernel_operator_vec_binary_impl.h` 的 `SupportType<...>` 断言里查到）。`Add` 这种基础运算对 `half/float/int32` 都直接支持。

#### 4.1.4 代码实践

**实践目标**：亲手把 add 样例的双目运算换一种，验证「改语义→改 golden→重算」的闭环，并体会 count 的含义。

**操作步骤**：

1. 复制 add 样例目录：`cp -r examples/01_simd_cpp_api/00_introduction/01_add/add /tmp/mul_test`。
2. 打开 `/tmp/mul_test/add/add.asc`，把第 46 行的 `AscendC::Add(zLocal, xLocal, yLocal, blockLength);` 改为 `AscendC::Mul(zLocal, xLocal, yLocal, blockLength);`（逐元素乘）。
3. 同步修改 Host 侧 golden：把第 147 行 `golden[i] = x[i] + y[i];` 改为 `golden[i] = x[i] * y[i];`。
4. 按样例 README 编译运行（`source set_env.sh` → `cmake` → `make` → `./demo`）。

**需要观察的现象**：终端先打印 `Output:` 与 `Golden:` 各前 20 个元素，例如第一个元素 `x[0]=0, y[0]=0`，相乘仍为 `0`；第二个 `x[1]=0.1, y[1]=0.2`，相乘为 `0.02`。

**预期结果**：因为输入确定、float 乘法在 Host/Device 都是同一套 IEEE-754，`std::equal` 精确比对应输出 `test pass!`。若你只改了 Kernel 忘了改 golden，会得到 `test failed!`——这正说明 `count` 个元素确实被逐个相乘了。

> 若本地无 NPU，可改用 CPU 仿真模式（`CMAKE_ASC_RUN_MODE=cpu`，切换前 `rm CMakeCache.txt`）观察同样的逻辑结果。无法确认运行环境时，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Add` 的 Level 2 声明里 `count` 是元素个数还是字节数？如果 `LocalTensor<float>` 想算 1024 个元素，`count` 传多少？

**答案**：元素个数。传 `1024`（不是 `4096`）。

**练习 2**：为什么不建议在「`Add` 写 zLocal」之后立刻「`Exp` 读 zLocal」而不加任何屏障？

**答案**：两者都在 V 流水线上，构成写后读（RAW）依赖；Ascend C 的 V 流水线可乱序执行，不加 `PipeBarrier<PIPE_V>()` 可能导致 `Exp` 读到未完成的旧值。

**练习 3**：`AddRelu` 相比「先 `Add` 再 `Relu`」有什么优势？

**答案**：`AddRelu` 是融合型接口，把加法和 ReLU 压成一条指令，省一次指令下发与一次中间读写，指令数更少、性能更高。

---

### 4.2 单目计算接口

#### 4.2.1 概念说明

**单目接口**处理「一个输入 Tensor 逐元素变换、得到一个输出 Tensor」的场景，常见于激活、初等函数与取值运算。代表接口：

| 接口 | 语义 |
| --- | --- |
| `Exp` | `dst[i] = e^(src[i])` |
| `Ln` | `dst[i] = ln(src[i])` |
| `Sqrt` | `dst[i] = src[i]^0.5` |
| `Rsqrt` | `dst[i] = 1/sqrt(src[i])` |
| `Reciprocal` | `dst[i] = 1/src[i]` |
| `Abs` | `dst[i] = abs(src[i])` |
| `Relu` | `dst[i] = src[i] < 0 ? 0 : src[i]` |
| `Neg` | `dst[i] = -src[i]` |
| `Not` | `dst[i] = ~src[i]`（按位取反） |

单目接口同样遵循 Level 2 / Level 0 分级：Level 2 形如 `Exp(dst, src, count)`，Level 0 形如 `Exp(dst, src, mask, repeatTime, repeatParams)`（单目用的是 `UnaryRepeatParams`，比双目少一个源操作数的步长）。初学全部用 Level 2 即可。

两个值得注意的细节：

- **精度与数据类型**：像 `Exp`/`Sqrt` 这类非线性运算，硬件通常以更高精度内部计算再写回。头文件注释里能看到端倪——例如 `ExpSub` 注明「当 T 为 half 时，先 `cast_f16_to_f32` 再做指数」。所以在 3510/5102 上，`Exp`/`Ln`/`Sqrt`/`Div`/`Reciprocal`/`Rsqrt` 的 Level 2 声明会多一个 `Config` 模板参数（如 `ExpConfig`），默认值 `DEFAULT_EXP_CONFIG` 即可，无需关心。
- **dst 与 src 可以是同一个 Tensor**（原地计算）。后面 gelu 样例就会看到 `Exp(yLocal, yLocal, n)` 的原地写法。

#### 4.2.2 核心流程

单目 Level 2 的流程与双目几乎一致，只是输入少一个：

```text
Exp(dst, src, count)
   → CheckCalcount(count)            // 非负校验
   → ExpImpl(dst, src, count)        // count → mask + repeat
   → V 流水线 SIMD 指令（一批元素并行做指数）
   → 若后续依赖 dst，需 PipeBarrier<PIPE_V>()
```

当需要把多个单目/双目运算串成一条表达式（如 Gelu、Softmax），就形成一条**V 流水线计算链**：每一步计算都依赖上一步结果，因此两两之间都要插 `PipeBarrier<PIPE_V>()`。

#### 4.2.3 源码精读

**(1) Exp 的 Level 2 声明**：

[include/basic_api/kernel_operator_vec_unary_intf.h:116-129](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_unary_intf.h#L116-L129) —— `Exp(dst, src, count)`，语义 `dst[i] = exp(src[i])`。注意 3510/5102 分支（第 123-125 行）多了 `const ExpConfig& config = DEFAULT_EXP_CONFIG` 模板参数，其他架构（第 127-128 行）没有，用默认即可。

**(2) 其余单目接口**：`Sqrt` 在 [include/basic_api/kernel_operator_vec_unary_intf.h:364-377](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_unary_intf.h#L364-L377)；`Abs` 在 [include/basic_api/kernel_operator_vec_unary_intf.h:205-213](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_unary_intf.h#L205-L213)；`Relu` 在 [include/basic_api/kernel_operator_vec_unary_intf.h:71-79](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_unary_intf.h#L71-L79)。形态完全一致。

**(3) 真实计算链：gelu.asc** —— 这是 Ascend C 仓库里 GELU 激活函数的高性能实现，它的「基础版本」恰好是一条标准的 V 流水线计算链，能完美佐证本讲的串联写法：

[examples/01_simd_cpp_api/05_best_practices/02_reg_compute/gelu_high_performance/gelu.asc:63-81](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/05_best_practices/02_reg_compute/gelu_high_performance/gelu.asc#L63-L81) —— 关键片段（保留行号语义）：

```cpp
AscendC::Mul(yLocal, xLocal, xLocal, n);     // y = x^2        （双目）
AscendC::PipeBarrier<PIPE_V>();              // V→V 屏障
AscendC::Mul(yLocal, yLocal, xLocal, n);     // y = x^3
AscendC::PipeBarrier<PIPE_V>();
AscendC::Muls(yLocal, yLocal, COEFF_A, n);   // y *= 0.5       （标量乘）
AscendC::PipeBarrier<PIPE_V>();
AscendC::Add(yLocal, xLocal, yLocal, n);     // y = x + 0.5*x^3
AscendC::PipeBarrier<PIPE_V>();
AscendC::Muls(yLocal, yLocal, COEFF_B, n);
AscendC::PipeBarrier<PIPE_V>();
AscendC::Exp(yLocal, yLocal, n);             // y = exp(...)   （单目，原地）
AscendC::PipeBarrier<PIPE_V>();
AscendC::Adds(yLocal, yLocal, (float)1.0, n);// y += 1
```

这段代码同时展示了三件事：双目（`Mul`/`Add`）、单目（`Exp`）、原地写（`Exp(yLocal, yLocal, n)`），以及**每一步之间都成对出现 `PipeBarrier<PIPE_V>()`**——这就是「V 流水线计算链」的标准范式。（`Muls`/`Adds` 是「张量×标量」变体，来自 `kernel_operator_vec_binary_scalar_intf.h`，不在本讲三大类的主干里，作扩展了解。）

#### 4.2.4 代码实践

**实践目标**：通过阅读 gelu.asc，亲手把一条「双目 + 单目」的计算链对上号，建立「读真实算子源码」的信心。

**操作步骤**：

1. 打开 [gelu.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/05_best_practices/02_reg_compute/gelu_high_performance/gelu.asc)，定位到 `GeluVfBasic` 函数（约第 60-85 行）。
2. 列一张表：每一行的接口名、类别（双目/单目/标量变体）、三个操作数分别是谁、`count` 是哪个变量（应为 `n`）。
3. 数一下这段链里一共有多少个 `PipeBarrier<PIPE_V>()`，并解释为什么数量恰好等于「计算步数」（首步前不需要屏障，之后每步前各一道）。

**需要观察的现象**：你会发现 `count` 全程是同一个 `n`，操作数在 `xLocal`/`yLocal` 之间流转，且屏障与计算严格交替。

**预期结果**：你能用一句话概括 GELU 的多项式近似公式 `0.5x(1 + tanh(...))` 是如何被拆解成上面这条 `Mul→Mul→Muls→Add→Muls→Exp→Adds` 链的。这是「源码阅读型实践」，无需运行即可完成。

#### 4.2.5 小练习与答案

**练习 1**：想求每个元素的平方根倒数（常见于向量归一化），用哪个接口？它的语义是什么？

**答案**：`Rsqrt`，语义 `dst[i] = 1/sqrt(src[i])`。它比「`Sqrt` 再 `Reciprocal`」更快，因为是一条指令。

**练习 2**：`Exp(dst, src, count)` 里 dst 和 src 能不能指向同一个 `LocalTensor`？依据是什么？

**答案**：能。gelu.asc 里就有 `Exp(yLocal, yLocal, n)` 的原地写法，说明单目接口允许 `dst == src`。

**练习 3**：为什么 `Exp`/`Sqrt` 在 3510/5102 上多了一个 `ExpConfig`/`SqrtConfig` 模板参数？

**答案**：这类非线性运算涉及精度取舍（如 half 是否先升精度到 float 再算），`Config` 参数让开发者控制这些精度/行为选项，默认值 `DEFAULT_*_CONFIG` 已是常用配置。

---

### 4.3 比较选择类接口

#### 4.3.1 概念说明

**比较选择类接口**服务于「按条件逐元素筛选」的场景，是 ReLU、掩码（mask）、where 等算子的底层实现。它分两步、两类接口：

- **比较（Compare/Compares）**：逐元素比较两个 Tensor（或 Tensor 与标量），结果不是「真假 Tensor」，而是一个**位掩码（bit mask）**——每位对应一个元素，真为 1，假为 0。比较模式由枚举 `CMPMODE` 选择：`LT/GT/EQ/LE/GE/NE`（小于/大于/等于/小于等于/大于等于/不等于）。
- **选择（Select）**：根据一个位掩码，从两个源 Tensor（或源 Tensor 与标量）里逐元素「挑」结果，等价于 `dst[i] = mask[i] ? src0[i] : src1[i]`。选择模式由枚举 `SELMODE` 选择。

之所以拆成「先比较产生 mask、再按 mask 选择」两步，是因为硬件的比较指令产出的是紧凑位掩码（每 8 个元素压成 1 字节 `uint8_t`），`Select` 直接消费这种掩码。`Max`/`Min` 可以看作这个机制的「封装快捷方式」——当你只是想取大/取小，直接用双目 `Max`/`Min` 即可，不必手动 Compare+Select。

> 补充：还有一类「张量与标量」的快捷比较 `Compares(dst, src0, scalarValue, cmpMode, count)`（旧名 `CompareScalar`，已建议改用 `Compares`），用于「每个元素和一个固定标量比」的常见场景。

#### 4.3.2 核心流程

比较选择最典型的两步用法（实现 `dst = src0 > src1 ? srcA : srcB`）：

```text
①  Compare(maskTensor, src0, src1, CMPMODE::GT, count)
        // 逐元素比较，把第 i 位的 0/1 写入 maskTensor（uint8_t 类型）
②  PipeBarrier<PIPE_V>()                      // Compare 写掩码、Select 读掩码，V→V 依赖
③  Select(dst, maskTensor, srcA, srcB, SELMODE, count)
        // 掩码位为 1 取 srcA，为 0 取 srcB
```

注意类型约定：被比较/被选择的源是 `half` 或 `float`，而掩码 Tensor 的元素类型是 `uint8_t`（8 个 half/float 的比较结果压成 1 字节）。这正是 `Compare`/`Select` 模板里出现 `typename T, typename U` 两个类型参数的原因——`T` 是源数据类型，`U` 是掩码类型。

#### 4.3.3 源码精读

**(1) CMPMODE / SELMODE 枚举**：

[impl/basic_api/utils/kernel_utils_mode.h:111-124](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/utils/kernel_utils_mode.h#L111-L124) —— `CMPMODE` 含 `LT/GT/EQ/LE/GE/NE`；`SELMODE` 含 `VSEL_CMPMASK_SPR`（按比较掩码选，最常用）、`VSEL_TENSOR_SCALAR_MODE`、`VSEL_TENSOR_TENSOR_MODE`。

**(2) Compare 的 Level 2 声明**：

[include/basic_api/kernel_operator_vec_cmpsel_intf.h:82-93](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_cmpsel_intf.h#L82-L93) —— `Compare(dst, src0, src1, cmpMode, count)`，其中 `dst` 是 `LocalTensor<U>`（掩码，`uint8_t`），`src0/src1` 是 `LocalTensor<T>`（源）。注释「If true, the corresponding bit is 1, otherwise it is 0」点明了「位掩码」语义。

**(3) Select 的 Level 2 声明**：

[include/basic_api/kernel_operator_vec_cmpsel_intf.h:215-230](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_cmpsel_intf.h#L215-L230) —— `Select(dst, selMask, src0, src1, selMode, count)`，根据 `selMask` 的位值从 `src0`/`src1` 里挑元素写入 `dst`。注意文件第 166-168 行的注释：源 `T` 一般是 `half`/`float`，掩码 `U` 是 `uint8_t`。

**(4) Compares（张量 vs 标量）**：

[include/basic_api/kernel_operator_vec_cmpsel_intf.h:145-156](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_operator_vec_cmpsel_intf.h#L145-L156) —— `Compares(dst, src0, src1Scalar, cmpMode, count)`，把每个元素和一个标量比，适合实现「`x > 0`」这类阈值判断（ReLU 的底层）。

#### 4.3.4 代码实践

**实践目标**：用 Compare + Select 手写一个 `where` 语义的掩码选择，理解「位掩码」这一中间产物。这是源码阅读 + 骨架编写型实践。

**操作步骤**：

1. 阅读上面 `Compare`/`Select` 的 Level 2 声明，确认参数顺序与类型约定（掩码 `uint8_t`、源 `half/float`）。
2. 在 add.asc 的 Kernel 里（不影响主流程的前提下，或复制一份新样例），新增两段输入 `aGm`/`bGm` 与输出 `zGm`，构思如下骨架（示例代码，非项目原有）：

   ```cpp
   // 示例代码：实现 z = (x > y) ? a : b，逐元素
   AscendC::LocalTensor<uint8_t> mask = ubAllocator.Alloc<uint8_t, blockLength / 8>();
   AscendC::Compare(mask, xLocal, yLocal, AscendC::CMPMODE::GT, blockLength);
   AscendC::PipeBarrier<PIPE_V>();
   AscendC::Select(zLocal, mask, aLocal, bLocal, AscendC::SELMODE::VSEL_CMPMASK_SPR, blockLength);
   AscendC::PipeBarrier<PIPE_V>();
   ```

3. 解释三件事：(a) 为什么掩码 Tensor 的元素数是 `blockLength / 8`（8 个 float 的比较压成 1 字节）；(b) 为什么 `Compare` 与 `Select` 之间必须有屏障；(c) 与直接调用 `Max(zLocal, aLocal, bLocal, blockLength)` 相比，Compare+Select 多解决了什么问题（答：任意条件 + 任意两路来源，而不只是取大）。

**需要观察的现象**：掩码 Tensor 的长度短了 8 倍；`Select` 的掩码参数类型是 `uint8_t` 而源是 `float`。

**预期结果**：你能说清「位掩码」是 Compare 与 Select 之间的契约。运行结果待本地验证（需自行准备 a/b 输入与 golden）。

#### 4.3.5 小练习与答案

**练习 1**：想实现 `dst[i] = x[i] > 0 ? x[i] : 0`（即 ReLU），有哪两种写法？

**答案**：(a) 直接用单目 `Relu(dst, src, count)`；(b) 用 `Compares(mask, x, 0.0f, CMPMODE::GT, count)` 生成掩码，再 `Select(dst, mask, x, zeroTensor, SELMODE, count)`。显然 (a) 更简洁。

**练习 2**：`Compare` 的输出 Tensor 元素类型为什么是 `uint8_t` 而不是 `bool` 或 `float`？

**答案**：因为硬件比较产出的是紧凑位掩码——8 个元素的比较结果压缩成 1 字节，每位 0/1。用 `uint8_t` 直接承载这种位packed 结构，`Select` 能原样消费。

**练习 3**：`CMPMODE::GE` 表示什么比较？

**答案**：Greater or Equal，即 `src0 >= src1` 时该位为 1。

---

## 5. 综合实践

把本讲三类接口合到一起，完成课程指定的融合算子：**在 add 样例基础上扩展 Kernel，先做 `Add` 再做 `Exp`，实现 `z = exp(x + y)`，并验证结果正确性。**

这道题同时用到「双目（Add）」和「单目（Exp）」，并检验你是否理解了 V 流水线依赖屏障。

**操作步骤**：

1. 复制样例：`cp -r examples/01_simd_cpp_api/00_introduction/01_add/add /tmp/add_exp`。
2. 修改 Kernel（[add.asc:46-47](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L46-L47) 处），在 `Add` 之后插入 `Exp`，并把屏障调整为 `PIPE_V`：

   ```cpp
   AscendC::Add(zLocal, xLocal, yLocal, blockLength);   // 第 46 行：双目，z = x + y
   AscendC::PipeBarrier<PIPE_V>();                       // V→V 写后读依赖屏障
   AscendC::Exp(zLocal, zLocal, blockLength);            // 新增：单目原地，z = exp(z)
   AscendC::PipeBarrier<PIPE_ALL>();                     // 第 47 行屏障保留：V→MTE3
   ```

   要点：`Add` 写 `zLocal`、`Exp` 读 `zLocal`，是同一条 V 流水线上的 RAW 依赖，必须 `PipeBarrier<PIPE_V>()`；最后的 `PipeBarrier<PIPE_ALL>()` 保障随后 `DataCopy(zGm, zLocal, ...)` 把 V 流水线结果安全搬回 GM。

3. 在文件顶部补 `#include <cmath>`（Host 侧算 golden 要用 `expf`）。
4. 修改 Host 侧 golden（[add.asc:145-148](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L145-L148)）：

   ```cpp
   for (uint32_t i = 0; i < totalLength; ++i) {
       golden[i] = expf(x[i] + y[i]);   // 由 x+y 改为 exp(x+y)
   }
   ```

5. 编译运行：`source set_env.sh` → `cmake -S . -B build` → `cmake --build build` → `./build/demo`（具体目标名以样例 CMakeLists 为准）。

**需要观察的现象**：`Output:` 前几项应为 `exp(0)=1`、`exp(0.1+0.2)=exp(0.3)≈1.34986`、`exp(0.2+0.4)=exp(0.6)≈1.82212`……（输入 `x[i]=i*0.1, y[i]=i*0.2`）。

**预期结果**：由于 `exp` 是非线性函数、且 Device 的 `Exp` 可能使用与 Host `expf` 略不同的近似多项式，**float 下可能仍精确相等，也可能出现末位差异**。若 `std::equal` 报 `test failed!`，把校验改为带容差比对（如 `fabs(out-golden) < 1e-4` 或 `np.isclose`）即可判定通过——这正是 u2-l3 讲过的「随机/低精度输入应改用带容差比对」。运行结果与是否需要容差，待本地验证。

**进阶（可选）**：把 `Add + Exp` 两步替换为一次带 `count` 的实验——尝试把 `blockLength` 改成非 8 的倍数（如 2045），观察 `count` 非对齐时是否仍能得到正确结果，体会 Level 2 接口对零头的内部处理。

## 6. 本讲小结

- 矢量计算接口分三大类：**双目**（`Add/Sub/Mul/Div/Max/Min/And/Or` 及融合型 `AddRelu/MulAddDst` 等）、**单目**（`Exp/Ln/Sqrt/Rsqrt/Abs/Reciprocal/Relu/Neg/Not`）、**比较选择**（`Compare/Compares` 产生位掩码、`Select` 按掩码挑选）。
- 三类接口都遵循统一的**分级模型**：Level 2 用 `count`（元素个数，非负）描述连续计算，初学首选；Level 0 用 `mask/repeatTime/repeatParams` 精细控制布局，留给性能优化。
- 所有矢量计算都跑在 **V 流水线**上，与搬运的 MTE2/MTE3 流水线相互独立；**同一条 V 流水线上的写后读依赖必须 `PipeBarrier<PIPE_V>()`**，跨流水线用 `PipeBarrier<PIPE_ALL>()`。gelu.asc 是这条「V 计算链」范式的真实范例。
- `count` 是元素个数而非字节数；接口层硬约束是 `count >= 0`，对齐到自然块大小（32 字节）性能最佳。
- 多步运算可以通过「双目 + 单目」串联表达复杂表达式（如 `z=exp(x+y)`、GELU 多项式近似）；能用融合接口（`AddRelu` 等）时优先用。

## 7. 下一步学习建议

本讲只用了「算什么」最直观的 Level 2 接口，刻意留下两块未深挖：

- **数据类型与精度转换**：`Exp`/`Sqrt` 在 half 下会先升精度，那 `half` 与 `float` 之间如何显式互转？`Cast`/`vconv` 接口、类型宏 `DTYPE`、不同类型的对齐与 repeat 约束，请接着学 **u4-l2 数据类型、内置类型与精度转换**。
- **Level 0 的 `mask/repeat/stride`**：当你需要处理跨步、分块、非连续布局，或追求极致性能时，Level 0 的精细控制才上场。这部分放到 **U13 性能优化** 与 **U8 C API 的 repeat/stride 高级搬运计算** 中结合实例展开。
- 如果你想看看「不用自己管内存和同步」的写法，可以跳读 **u5-l1 TPipe/TQue 框架编程**，对比框架式与本章自主式两种范式。
