# 数值类型与基础容器

> 本讲承接 [u1-l1 项目总览](u1-l1-project-overview.md)。你已经知道 CUTLASS 是一个 header-only 的 CUDA C++ 模板库，本讲我们把镜头拉近到最底层的一块砖：**CUTLASS 是怎样用 C++ 类型来表达 half、bf16、fp8、tf32 这些数值的**，以及它们被装进哪个容器里搬运。

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `cutlass::half_t`、`cutlass::bfloat16_t`、`cutlass::tfloat32_t`、`cutlass::float_e4m3_t` 等 类型各自的位宽与含义；
- 理解 `sizeof_bits<T>` 这个贯穿全库的尺寸元函数，以及「子字节（sub-byte）类型」的概念；
- 掌握 `cutlass::Array<T, N>` 容器的两个特化分支（寄存器尺寸 / 打包），并能推断 `Array<half_t, 4>` 占多少字节；
- 用 `NumericConverter` / `NumericArrayConverter` 在不同数值类型之间安全转换，并知道如何指定舍入方式。

## 2. 前置知识

- **浮点数的位组成**：一个浮点数由「符号位（sign）+ 指数位（exponent）+ 尾数位（mantissa）」三部分组成。例如常见的 IEEE 754 单精度 `float` 是 1+8+23 = 32 位。
- **模板偏特化（partial specialization）**：CUTLASS 大量用模板偏特化为「不同类型 / 不同尺寸」生成不同的实现。本讲你会看到 `Array` 与 `NumericConverter` 都靠偏特化分派。
- **`alignas` 与对齐**：C++ 可以用 `alignas(N)` 强制一个类型按 N 字节对齐，这会影响 `sizeof`、结构体布局以及能否安全放进 `union`。
- 如果你已读过 [u1-l1](u1-l1-project-overview.md)，应当记得 CUTLASS 支持从 FP64 到 FP4 的多种精度、以及它需要配合不同 GPU 架构的 Tensor Core。本讲正是这些精度在源码层的具体写法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/cutlass/numeric_types.h` | 数值类型的**统一入口**，聚合所有类型头文件 |
| `include/cutlass/half.h` | 定义 IEEE 半精度类型 `half_t`，以及 float↔half 的软件 / 硬件转换 |
| `include/cutlass/bfloat16.h` | 定义 16 位脑浮点类型 `bfloat16_t` |
| `include/cutlass/tfloat32.h` | 定义 TF32 类型 `tfloat32_t` |
| `include/cutlass/float8.h` | 定义 8 位浮点 `float_e4m3_t`、`float_e5m2_t` 等（FP8） |
| `include/cutlass/float_subbyte.h` | 定义 4 位 / 6 位等更窄浮点（如 `float_e2m1_t`）及它们的 `sizeof_bits` |
| `include/cutlass/numeric_size.h` | 定义核心尺寸元函数 `sizeof_bits<T>` 与 `is_subbyte<T>` |
| `include/cutlass/array.h` | 定义基础容器 `Array<T, N>`（寄存器尺寸分支）与 `AlignedArray` |
| `include/cutlass/array_subbyte.h` | 定义 `Array<T, N>` 的**打包分支**（用于 <32 位元素） |
| `include/cutlass/numeric_conversion.h` | 定义 `NumericConverter` 与 `NumericArrayConverter` |

## 4. 核心概念与源码讲解

### 4.1 半精度（FP16）与 BF16 类型

#### 4.1.1 概念说明

深度学习与科学计算里，32 位 `float` 经常「太宽」，会浪费显存和带宽。两种 16 位浮点应运而生，它们都只用 2 字节，但侧重点不同：

- **FP16（half）**：1 符号 + 5 指数 + 10 尾数。尾数多、精度高，但指数范围小（最大约 65504），容易溢出。
- **BF16（Brain Float 16）**：1 符号 + 8 指数 + 7 尾数。指数和 `float` 完全一致，相当于「把 float 砍掉低 16 位尾数」，所以**动态范围大、与 float 互转极快**，但精度较低。

CUTLASS 没有直接用 CUDA 内置的 `__half`，而是定义了自家的、可在 **host 和 device** 上同样使用的等价类型：`cutlass::half_t` 与 `cutlass::bfloat16_t`。这样同一个内核可以在 CPU 上做单元测试、在 GPU 上跑。

#### 4.1.2 核心流程

一个自定义浮点类型要「能用」，至少要解决三件事：

1. **存储**：用一个无符号整数把整个位模式装下来（half/bf16 都用 `uint16_t`）。
2. **构造与转换**：能从 `float` 构造、也能转回 `float`（通常四舍五入到最近偶数，round-to-nearest-even）。
3. **与硬件对接**：在设备代码里，尽量映射到 CUDA 的 `__half` / `__half2` 或 PTX 指令以获得加速。

#### 4.1.3 源码精读

`half_t` 的本体非常简洁——`alignas(2)` 保证 2 字节对齐，内部就是一个 `uint16_t`：

> [include/cutlass/half.h:166-175](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/half.h#L166-L175) —— `half_t` 的存储定义：一个 2 字节、按 2 字节对齐的结构体，内部用 `uint16_t storage` 保存 16 位位模式。

float → half 的转换 `convert(float)` 是理解整个库「跨精度」思路的范本：它**优先用硬件指令**（`__float2half_rn`，需要 SM≥530），否则退化到一段手写的、按 round-to-nearest-even 的软件实现：

> [include/cutlass/half.h:188-198](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/half.h#L188-L198) —— `static half_t convert(float const&)`：在支持半精度的 GPU（`__CUDA_ARCH__ >= 530`）上直接调用硬件 `__float2half_rn`；否则走下面的软件舍入路径。

反向 `convert(half_t) → float` 同样是「硬件优先、软件兜底」：

> [include/cutlass/half.h:301-304](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/half.h#L301-L304) —— `static float convert(half_t const&)`：把 16 位还原成 32 位浮点。

`half_t` 还提供了便捷的隐式接口：从 `float` 的显式构造、以及 `operator float()`：

> [include/cutlass/half.h:373-376](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/half.h#L373-L376) —— `explicit half_t(float x)`：通过上面的 `convert` 完成构造。

> [include/cutlass/half.h:421-424](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/half.h#L421-L424) —— `operator float()`：让 `half_t` 能方便地转回 `float` 打印 / 参与运算。

`bfloat16_t` 的整体设计与 `half_t` 几乎一致（同样是 `alignas(2)` + `uint16_t` 存储 + 软件/硬件转换），区别只在位分配（8 指数 + 7 尾数）：

> [include/cutlass/bfloat16.h:57-58](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/bfloat16.h#L57-L58) —— `bfloat16_t` 的定义起点，同样按 2 字节对齐。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：体会「硬件优先、软件兜底」这条贯穿 CUTLASS 的工程哲学。
2. **步骤**：打开 [include/cutlass/half.h:200-272](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/half.h#L200-L272) 的软件实现，找到处理「溢出到无穷」「非规格化数（subnormal）」「round-to-nearest-even 的 sticky bit」的分支。
3. **观察**：软件实现里有 `sign`、`exp`、`mantissa` 三段位移，对应把 32 位浮点的 1+8+23 重新打包成 1+5+10。
4. **预期**：你能用自己的话说出「为什么 `exp > 15` 要置成无穷」——因为 half 的指数只有 5 位，正规模的最大指数是 15。
5. 运行结果：本实践为阅读型，无需运行。

#### 4.1.5 小练习与答案

- **Q1**：half 和 bf16 都是 16 位，为什么训练里常常更偏爱 bf16？
  - **A**：bf16 的指数位与 float 相同（8 位），动态范围大、不易溢出，且与 float 互转只需截断/补零，转换成本极低；half 尾数更细但最大值只有约 65504，容易上溢。
- **Q2**：`half_t` 为什么要 `alignas(2)`？
  - **A**：保证它 2 字节对齐，使得两个 `half_t` 能安全重解释成 32 位的 `__half2`（见 4.3 的 Array 打包），也保证在 `union` 里的布局可预测。

### 4.2 FP8 / TF32 等窄精度与子字节类型

#### 4.2.1 概念说明

为了把算力与带宽推到极致，更新的 GPU 引入了更窄的浮点：

- **TF32（`tfloat32_t`）**：1+8+10 = 19 位有效，但**占用 32 位存储**。它是 Ampere（SM80）Tensor Core 上「用 float 输入、内部按 19 位相乘」的格式，兼顾兼容性与吞吐。
- **FP8**：8 位浮点，主要有两种编码：
  - `float_e4m3_t`：1+4+3，**精度优先**（尾数多一位），是 GEMM 输入的主力；
  - `float_e5m2_t`：1+5+2，**动态范围优先**（指数多一位），常用于梯度。
- **更窄（子字节）类型**：4 位的 `float_e2m1_t`、4 位整数 `int4b_t` / `uint4b_t`、2 位 `int2b_t` / `uint2b_t`、1 位 `bin1_t` 等。这些「不足 1 字节」的类型在存储上是**位打包（bit-packed）**的，需要特殊处理。

这里有一个关键点：C++ 的 `sizeof(T)` 单位是字节，无法表达「4 位」。CUTLASS 因此定义了一个以**位**为单位的尺寸元函数 `sizeof_bits<T>`。

#### 4.2.2 核心流程

`sizeof_bits<T>` 的求值规则：

1. **通用情况**：`sizeof_bits<T>::value = sizeof(T) * 8`。对 `half_t` 就是 \(2 \times 8 = 16\)。
2. **不足整字节的子字节类型**：提供显式偏特化，直接写死位数，例如 `float_e2m1_t` 写成 4。

有了位宽，就能判断一个类型是不是「子字节」：

\[ \texttt{is\_subbyte<T>} \iff \texttt{sizeof\_bits<T>::value} < 8 \]

这个判断直接决定了下一节 `Array` 用哪条特化分支。

#### 4.2.3 源码精读

`numeric_types.h` 是所有数值类型的**总入口**，它依次 `#include` 了每种类型的头文件：

> [include/cutlass/numeric_types.h:39-48](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_types.h#L39-L48) —— 一行 `#include "cutlass/half.h"`、`"cutlass/bfloat16.h"`、`"cutlass/tfloat32.h"`、`"cutlass/float8.h"`……就把全部数值类型拉进同一个头文件；使用者只需 `#include "cutlass/numeric_types.h"`。

各类型的定义（注意 `alignas` 决定了 `sizeof`，进而决定 `sizeof_bits` 的通用值）：

> [include/cutlass/tfloat32.h:53-54](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tfloat32.h#L53-L54) —— `tfloat32_t`，`alignas(4)`，占满 4 字节（`sizeof_bits` = 32）。
>
> [include/cutlass/float8.h:411-412](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float8.h#L411-L412) —— `float_e4m3_t`，`alignas(1)`，占 1 字节（`sizeof_bits` = 8）。
>
> [include/cutlass/float8.h:626-627](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float8.h#L626-L627) —— `float_e5m2_t`，同样 `alignas(1)`、8 位。

`sizeof_bits` 的通用模板就在这里（注意单位是**位**）：

> [include/cutlass/numeric_size.h:47-50](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_size.h#L47-L50) —— `sizeof_bits<T>::value = sizeof(T) * 8`，并对 `T const` / `T volatile` 透明传递。
>
> [include/cutlass/numeric_size.h:89-91](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_size.h#L89-L91) —— `is_subbyte<T>`：当 `sizeof_bits<T>::value < 8` 时为真。

对真正「不足整字节」的类型，靠显式偏特化给出位数（`sizeof` 在这里会失真）：

> [include/cutlass/float_subbyte.h:136-138](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float_subbyte.h#L136-L138) —— `sizeof_bits<float_e2m1_t>::value = 4`，写死 4 位（一个字节里塞两个）。

> **类型名小贴士**：本讲的实践大纲里提到过 `float8_e4m3_t` 这个名字，但**当前仓库里实际使用的名字是 `float_e4m3_t` / `float_e5m2_t`**（不带 `8`）。读源码时请以实际名字为准，不要被旧命名误导。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把「位宽 → 对齐 → sizeof_bits」这条链在脑子里打通。
2. **步骤**：对 `half_t`、`bfloat16_t`、`tfloat32_t`、`float_e4m3_t` 四个类型，分别写出 `alignas`、`sizeof`、`sizeof_bits` 的值。
3. **预期表格**（参考答案）：

| 类型 | alignas | sizeof（字节） | sizeof_bits（位） | is_subbyte |
| --- | --- | --- | --- | --- |
| `half_t` | 2 | 2 | 16 | false |
| `bfloat16_t` | 2 | 2 | 16 | false |
| `tfloat32_t` | 4 | 4 | 32 | false |
| `float_e4m3_t` | 1 | 1 | 8 | false |

4. 运行结果：本实践为阅读型，无需运行。

#### 4.2.5 小练习与答案

- **Q1**：`float_e4m3_t` 是 8 位，为什么 `is_subbyte` 是 `false`？
  - **A**：`is_subbyte` 的判据是 `sizeof_bits < 8`，8 位不满足「严格小于 8」，所以不是子字节类型；它正好占满一个字节，`sizeof` 即可表达。
- **Q2**：`int4b_t`（4 位整数）一个字节能装几个？
  - **A**：两个。\(8 \div 4 = 2\)，这正是下一节 `Array` 打包时「一个存储字里塞 kElementsPerStoredItem 个元素」的来源。

### 4.3 cutlass::Array 容器与对齐

#### 4.3.1 概念说明

Tensor Core 一次要吃进一小批数据（比如 8 个或 16 个 half），CUTLASS 需要一个「定长、可放 `union`、能容纳任意数值类型」的小数组容器。这就是 `cutlass::Array<T, N>`。它的设计目标是：

- **逻辑上**：看作 N 个 `T` 类型元素，可以 `arr[i]` 访问。
- **物理上**：当元素位数 ≥ 32 时，一个元素占一个寄存器，直接连续存放；当元素位数 < 32 时，把多个元素**打包进一个 32 位字**，以匹配寄存器宽度。

这种「按元素位宽走两条不同实现」的做法，正是靠模板偏特化实现的。

#### 4.3.2 核心流程

`Array` 的主模板声明带第三个模板参数 `RegisterSized`，默认值由元素位宽决定：

\[ \texttt{RegisterSized}_{\text{default}} = (\texttt{sizeof\_bits<T>::value} \ge 32) \]

于是：

- `Array<float, 4>`、`Array<tfloat32_t, 8>` → `RegisterSized = true` → 走 [array.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h) 中的 `true` 分支：`Storage = T`，连续 `N` 个元素。
- `Array<half_t, 4>`、`Array<float_e4m3_t, 16>`、`Array<int4b_t, 8>` → `RegisterSized = false` → 走 [array_subbyte.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array_subbyte.h) 中的 `false` 分支：把元素打包进更宽的 `Storage`（`uint32_t` / `uint16_t` / `uint8_t`），用代理 `reference` 读写每一位。

对 `Array<half_t, 4>` 推算一下物理大小：总位宽 \(16 \times 4 = 64\) 位，能整除 32，所以 `Storage = uint32_t`，每个存储字装 \(32/16 = 2\) 个元素，需要 \(\lceil 4/2 \rceil = 2\) 个存储字，即 `uint32_t[2]`，共 **8 字节**。

#### 4.3.3 源码精读

`Array` 的主模板声明，注意第三个参数的默认值：

> [include/cutlass/array.h:45-51](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h#L45-L51) —— `template <typename T, int N, bool RegisterSized = sizeof_bits<T>::value >= 32> struct Array;`。这一行是理解「两条分支」的钥匙。

寄存器尺寸分支（`true`）：存储就是 N 个连续元素：

> [include/cutlass/array.h:97-101](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h#L97-L101) —— `Array<T, N, true>` 特化，`Storage = T`。
>
> [include/cutlass/array.h:356-357](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h#L356-L357) —— 内部存储 `Storage storage[kElements];`，即直接连续 N 个 T。
>
> [include/cutlass/array.h:405-413](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h#L405-L413) —— `data()` / `raw_data()` 返回底层指针，便于和 CUDA API 或 memcpy 对接。

打包分支（`false`）：根据总位宽选 `Storage`，并用 `reference` 代理做位插入/提取：

> [include/cutlass/array_subbyte.h:47-63](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array_subbyte.h#L47-L63) —— `Array<T, N, false>`：`kSizeBits = sizeof_bits<T>::value * N`，再按能否被 32 / 16 整除，把 `Storage` 选成 `uint32_t` / `uint16_t` / `uint8_t`。
>
> [include/cutlass/array_subbyte.h:94-108](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array_subbyte.h#L94-L108) —— `reference` 代理：写一个元素时，用掩码 `kMask` 把它塞进存储字的对应位段（`*ptr_ = (*ptr_ & kUpdateMask) | (item << idx_*bits)`），从而让你像普通数组一样 `arr[i] = x`。

`Array` 自身也有 `sizeof_bits` 偏特化（返回的是**整个数组**占的位数）：

> [include/cutlass/array.h:73-76](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h#L73-L76) —— `sizeof_bits<Array<T,N>> = sizeof(Array<T,N>) * 8`。

工具函数与对齐版本：

> [include/cutlass/array.h:519-542](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h#L519-L542) —— `make_Array(...)` 工厂，方便构造 1~4 个元素的数组。
>
> [include/cutlass/array.h:2858-2870](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array.h#L2858-L2870) —— `AlignedArray`：默认对齐量 \((\texttt{sizeof\_bits<T>::value} \times N + 7) / 8\) 字节，用 `alignas` 保证缓存行 / 向量加载友好。

#### 4.3.4 代码实践

1. **目标**：亲手验证「`Array<half_t, 4>` 走打包分支、占 8 字节」这一结论。
2. **操作步骤**：在 build 目录旁新建一个测试源（**示例代码**，并非仓库原有文件）：

   ```cpp
   // 文件名：probe_array.cu （示例代码）
   #include <iostream>
   #include "cutlass/numeric_types.h"
   #include "cutlass/array.h"

   int main() {
     std::cout << "sizeof_bits<half_t>   = "
               << cutlass::sizeof_bits<cutlass::half_t>::value << "\n";
     std::cout << "sizeof(Array<half_t,4>) = "
               << sizeof(cutlass::Array<cutlass::half_t, 4>) << " bytes\n";
     std::cout << "sizeof(Array<float,4>)  = "
               << sizeof(cutlass::Array<float, 4>) << " bytes\n";
     return 0;
   }
   ```

   编译运行（只需 CUDA 工具链，无需 GPU，因为全程是 host 代码）：

   ```bash
   nvcc -std=c++17 -I include probe_array.cu -o probe_array
   ./probe_array
   ```

3. **需要观察的现象**：`sizeof_bits<half_t>` 是 16；`Array<half_t,4>` 是 8 字节；`Array<float,4>` 是 16 字节。
4. **预期结果**：

   ```
   sizeof_bits<half_t>   = 16
   sizeof(Array<half_t,4>) = 8 bytes
   sizeof(Array<float,4>)  = 16 bytes
   ```
5. 运行结果：**待本地验证**（按 4.3.2 的位运算推导，应为上述输出）。

#### 4.3.5 小练习与答案

- **Q1**：为什么 `Array<half_t, 4>` 走的是 `RegisterSized=false` 的打包特化，而 `Array<float, 4>` 走的是 `true`？
  - **A**：因为第三参数默认值是 `sizeof_bits<T>::value >= 32`。`half_t` 是 16 位（<32）→ false；`float` 是 32 位（≥32）→ true。
- **Q2**：`sizeof(Array<half_t, 4>)` 为什么是 8 而不是 4？
  - **A**：4 是「元素个数」；每个 half 2 字节，4 个共 8 字节。打包分支把它们放进 `uint32_t[2]`，物理大小仍为 8 字节。
- **Q3**：如果换成 `Array<int4b_t, 8>`（8 个 4 位整数），物理大小是多少？
  - **A**：8 × 4 位 = 32 位 = 4 字节（`Storage = uint32_t`，1 个存储字）。

### 4.4 数值类型转换

#### 4.4.1 概念说明

有了多种数值类型，就需要在它们之间安全地搬数据，例如：把主机上的 `float` 权重转成 `half_t` 喂给内核，或把 `half_t` 的计算结果转回 `float` 做累加。CUTLASS 把这件事统一交给两个模板：

- **`NumericConverter<T, S, Round>`**：转换单个标量，从源类型 `S` 转到目标类型 `T`，可指定舍入方式 `Round`。
- **`NumericArrayConverter<T, S, N, Round>`**：转换 `Array<S, N>` → `Array<T, N>`，内部能向量化（例如一次处理 2 个 half）。

为什么不让用户直接 `static_cast`？因为：① 不同精度之间的舍入策略（向零、向最近偶数、饱和等）需要显式可控；② 子字节类型的转换需要位操作，普通 `static_cast` 做不到；③ 在设备代码里要尽量映射成单条硬件指令（如 `__float22half2_rn`）。

#### 4.4.2 核心流程

转换的舍入方式由枚举 `FloatRoundStyle` 决定，常用项：

- `round_to_nearest`：四舍五入到最近偶数（默认）。
- `round_toward_zero`：向零取整。
- `round_to_nearest_satfinite`：四舍五入并饱和到目标类型的有限范围。
- 其它：`round_toward_infinity`、`round_half_ulp_truncate` 等。

调用方式很统一：定义一个转换器对象，像函数一样调用它（它重载了 `operator()`），或调用静态 `convert`。`NumericArrayConverter` 的默认实现就是一个循环，对每个元素调用对应的标量 `NumericConverter`；对常见组合（如 float↔half）则有向量化特化。

#### 4.4.3 源码精读

舍入风格枚举：

> [include/cutlass/numeric_conversion.h:56-65](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_conversion.h#L56-L65) —— `enum class FloatRoundStyle`，列出全部舍入模式。

标量转换器主模板：默认就用 `static_cast`（对 half_t 来说会触发其 `explicit half_t(float)` 构造，等价于 4.1 的 round-to-nearest 转换）：

> [include/cutlass/numeric_conversion.h:69-90](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_conversion.h#L69-L90) —— `NumericConverter<T, S, Round>`：暴露 `result_type` / `source_type` / `round_style`，提供静态 `convert()` 与 `operator()`，默认 `static_cast`。

数组转换器主模板：循环逐元素调用标量转换器：

> [include/cutlass/numeric_conversion.h:842-882](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_conversion.h#L842-L882) —— `NumericArrayConverter<T, S, N, Round, Transform>`：`result_type = Array<T,N>`、`source_type = Array<S,N>`，`convert()` 内对每个 `i` 调 `NumericConverter<T,S,Round>`，并支持可选的 `Conjugate` 变换。

向量化特化：float↔half 的「一次两个」版本，在 GPU 上映射到 `__float22half2_rn`：

> [include/cutlass/numeric_conversion.h:921-946](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_conversion.h#L921-L946) —— `NumericArrayConverter<half_t, float, 2>`：设备端用 `__float22half2_rn`，host 端退化到逐元素 `NumericConverter<half_t,float>`。
>
> [include/cutlass/numeric_conversion.h:993-1026](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_conversion.h#L993-L1026) —— `NumericArrayConverter<half_t, float, N>`：把数组按「每 2 个一组」交给上面的向量化版本，剩余的奇数个元素用标量转换器收尾。

#### 4.4.4 代码实践

这是本讲的主干实践，把「数值类型 → Array → 转换」三者串起来。

1. **实践目标**：定义 `cutlass::Array<half_t, 4>`，用 `NumericArrayConverter` 把一个 `float` 数组转换成 `half_t` 并打印，验证转换正确性。
2. **操作步骤**：新建源文件（**示例代码**，非仓库原有文件）：

   ```cpp
   // 文件名：convert_to_half.cu （示例代码）
   #include <iostream>
   #include <iomanip>
   #include "cutlass/numeric_types.h"
   #include "cutlass/array.h"
   #include "cutlass/numeric_conversion.h"

   int main() {
     // 1. 准备一个 float 数组（这 4 个值都能被 half 精确表示）
     cutlass::Array<float, 4> src;
     float data[4] = {1.5f, -2.25f, 3.0f, 0.125f};
     for (int i = 0; i < 4; ++i) src[i] = data[i];

     // 2. 用 NumericArrayConverter 把 float -> half_t（round to nearest）
     using Converter = cutlass::NumericArrayConverter<
         cutlass::half_t, float, 4, cutlass::FloatRoundStyle::round_to_nearest>;
     Converter converter;
     cutlass::Array<cutlass::half_t, 4> dst = converter(src);

     // 3. 打印：half_t 经 operator float() 转回 float 打印
     std::cout << std::fixed << std::setprecision(4);
     std::cout << "float -> half_t 转换结果:\n";
     for (int i = 0; i < 4; ++i) {
       std::cout << "  " << float(src[i]) << " -> " << float(dst[i]) << "\n";
     }

     // 4. 顺带打印 Array 的物理大小（呼应 4.3）
     std::cout << "sizeof(Array<float,4>)    = "
               << sizeof(src) << " bytes\n";
     std::cout << "sizeof(Array<half_t,4>)   = "
               << sizeof(dst) << " bytes\n";
     return 0;
   }
   ```

   编译运行（全程 host 代码，无需 GPU）：

   ```bash
   nvcc -std=c++17 -I include convert_to_half.cu -o convert_to_half
   ./convert_to_half
   ```

3. **需要观察的现象**：1.5、-2.25、3.0、0.125 在 half 下仍能精确表示，转换前后数值一致；Array 大小与 4.3 推导一致。
4. **预期结果**：

   ```
   float -> half_t 转换结果:
     1.5000 -> 1.5000
     -2.2500 -> -2.2500
     3.0000 -> 3.0000
     0.1250 -> 0.1250
   sizeof(Array<float,4>)    = 16 bytes
   sizeof(Array<half_t,4>)   = 8 bytes
   ```
5. 运行结果：**待本地验证**。1.5、-2.25、3.0、0.125 都是 2 的幂次相关的「干净」小数，二进制下可被 half 精确表示，因此转换无损失。

> **延伸尝试**：把 `data` 里换一个 half 无法精确表示的值（如 `0.1f`），观察 `float(dst[i])` 与原值的微小差异——这就是窄精度转换不可避免的舍入误差，也是 `FloatRoundStyle` 之所以重要的原因。

#### 4.4.5 小练习与答案

- **Q1**：如何让上面的转换改成「向零舍入」？
  - **A**：把 `using Converter` 的第 4 个模板实参从 `round_to_nearest` 改成 `cutlass::FloatRoundStyle::round_toward_zero`。
- **Q2**：为什么 `NumericArrayConverter` 要为 `half_t ↔ float` 专门写一个 `N=2` 的特化？
  - **A**：因为硬件有一条「一次转两个 half」的指令 `__float22half2_rn`，`N=2` 特化正是把它暴露出来；通用 `N` 版本则每 2 个一组地复用这个向量化特化，剩余奇数个用标量收尾，从而在设备端获得更高吞吐。

## 5. 综合实践

把本讲三个要点（多种数值类型 / Array 容器 / 转换）合到一个小任务里：

**任务**：写一个程序，读入一个 `cutlass::Array<float, 8>`（其中故意混入「能精确表示」和「不能精确表示」的数，例如 `{1.0f, 0.5f, 0.1f, -3.75f, 2.0f, 0.2f, 7.0f, 1e-3f}`），分别用两个转换器把它转成 `Array<half_t, 8>` 和 `Array<bfloat16_t, 8>`，并打印三行对照表（float 原值 / half 转回值 / bf16 转回值），最后再打印 `sizeof_bits` 与 `sizeof(Array<...>)` 的结果。

要求：

1. 复用本讲学到的 `NumericArrayConverter<T, float, N>`；
2. 找出哪些数在 half 下出现了偏差、哪些在 bf16 下出现了偏差，并用本讲的位分配知识解释（half 尾数 10 位、bf16 尾数 7 位，谁的尾数少，谁对「小数」更不敏感）；
3. 在打印前先用 `static_assert(cutlass::sizeof_bits<cutlass::half_t>::value == 16, "...")` 做一次编译期自检。

**验收标准**：能正确编译运行；能解释清楚「同一组数在 half 与 bf16 下误差不同」的原因；能说出 `Array<half_t,8>` 与 `Array<bfloat16_t,8>` 的物理大小（都应是 16 字节，因为 8×2=16）。

> 运行结果：**待本地验证**。如果你没有 CUDA 工具链，也可以退化为「源码阅读 + 纸笔推算」型实践——按 4.3 的位运算手算各 `Array` 大小，按 4.4 的转换语义手算 half/bf16 的舍入结果。

## 6. 本讲小结

- CUTLASS 用 `alignas` + 整数存储 的方式，把 half / bf16 / tf32 / fp8 / 子字节等数值都实现成「host/device 通用」的 C++ 类型，统一入口是 [numeric_types.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_types.h)。
- `sizeof_bits<T>` 是以**位**为单位的尺寸元函数；对不足整字节的类型（如 `float_e2m1_t`、`int4b_t`）靠显式偏特化给出位数。`is_subbyte<T> ≡ sizeof_bits<T>::value < 8`。
- `cutlass::Array<T, N>` 按元素位宽自动分派：`sizeof_bits<T>::value >= 32` 走连续存储的 `true` 分支，否则走 [array_subbyte.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/array_subbyte.h) 的位打包 `false` 分支（`half_t` 走这一支）。
- 跨类型转换统一交给 `NumericConverter`（标量）和 `NumericArrayConverter`（数组），舍入方式由 `FloatRoundStyle` 指定；常见组合有硬件向量化特化。
- 工程哲学：**「硬件优先、软件兜底」**与**「用模板偏特化按位宽分派」**这两条思路会贯穿后续所有讲义。

## 7. 下一步学习建议

- 想看这些类型如何被组织进「矩阵」？下一讲 [u1-l5 矩阵布局基础](u1-l5-matrix-layouts.md) 会讲 `layout::RowMajor/ColumnMajor` 与 `TensorRef`，把这里的数值元素铺成二维矩阵。
- 想亲手跑一个完整 GEMM？[u1-l6 第一个 GEMM](u1-l6-first-gemm.md) 会用 `device::Gemm` 把本讲的类型（如 `half_t` 输入、`float` 累加）实例化成一个真正可运行的内核。
- 想深入转换机制？后续进阶层会再次遇到 `NumericConverter`——它正是 epilogue（结果后处理）阶段把累加器（如 `float`）写回成 `half_t`/`int8_t` 输出的关键部件。
