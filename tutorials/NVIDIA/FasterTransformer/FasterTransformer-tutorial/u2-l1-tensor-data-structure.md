# 统一张量抽象：Tensor / TensorMap / DataType

## 1. 本讲目标

FasterTransformer（以下简称 FT）的内核（kernel）、层（layer）、模型（model）有上千个函数，它们的输入输出几乎全是同一种东西——一个描述「数据放在哪、是什么类型、什么形状、指针是多少」的小结构体。这个结构体就是本讲的主角 `Tensor`，以及把它按名字打包起来的 `TensorMap`。

学完本讲你应该能够：

- 看懂 `DataType` 枚举的取值，并理解 `getTensorType<T>()` 如何把 C++ 模板类型（`float`/`half`/`int`…）映射成这个枚举。
- 说清楚 `Tensor` 的五个字段（`where`/`type`/`shape`/`data`/`offsets`）各自的作用，特别是 `where` 如何标记 CPU/GPU，以及 **`Tensor` 本身并不拥有（own）显存/内存** 这一关键事实。
- 理解为什么模型 `forward` 接口统一用 `TensorMap*`（或老的 `std::vector<Tensor>*`），并能读懂 `input_tensors->at("input_ids")` 这种按名字取张量的写法。
- 能动手构造一个 `TensorMap`，放进若干命名张量。

本讲是整个进阶层的「数据载体」基石。后续讲显存分配（u2-l2）、矩阵乘封装（u2-l3）、乃至所有模型 forward，都会反复用到这里的术语和约定。

## 2. 前置知识

阅读本讲前，你需要具备：

- **C++ 模板与 `std::is_same`**：FT 大量用模板 `<typename T>` 把同一份逻辑编译成 `float`/`half`/`__nv_bfloat16` 多个版本，再用 `std::is_same<T, float>::value` 在编译期判断 `T` 到底是哪种类型。这是 CUDA 推理库的常见套路。
- **CUDA 的「主机—设备」内存分离**：CPU 内存（host）和 GPU 显存（device）是两块物理上独立的存储。一个指针到底指向哪一块，必须显式记录，否则把 CPU 指针当成 GPU 指针传给 kernel 会直接段错误。
- **FT 的「枚举 → 模板」dispatch 套路**（来自 u1-l4）：示例 `main` 里先用 `data_type` 枚举（0=FP32, 1=FP16, 2=BF16）做 `if/else`，分发到 `model.forward<T>(...)` 的具体模板实例。本讲的 `DataType` 枚举就是这套机制底层的统一类型标识。
- **FT 的目录三层抽象**（来自 u1-l3）：kernel 是 GPU 上单件事的并行函数；layer 把 kernel 和矩阵乘组合成 attention/FFN 等语义模块；model 串联各层。`Tensor` 是贯穿这三层的唯一数据载体。

不需要你已经写过 CUDA 代码，只要理解「指针 + 类型 + 形状」这三样东西足以描述一块多维数据即可。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `src/fastertransformer/utils/` 下：

| 文件 | 作用 |
| --- | --- |
| [Tensor.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h) | 声明 `DataType` 枚举、`getTensorType<T>()` 模板、`MemoryType` 枚举、`Tensor` 结构体和 `TensorMap` 类。本讲的核心文件。 |
| [Tensor.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc) | `Tensor` 与 `TensorMap` 的实现：`size()`、`sizeBytes()`、`getTypeSize()`、`slice()`、各种构造函数，以及 `.npy` 文件读写。 |
| [convert_data_type.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/convert_data_type.h) | 一个小的标量类型转换辅助：`float_to_int8_rn_host`，用于把 `float` 四舍五入成 `int8`（量化场景用）。 |
| [models/bert/Bert.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.h) / [Bert.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc) | 用作「真实模型如何用 `TensorMap`」的范例。 |

> 提醒：本讲只讲数据结构本身，不讲显存是谁分配的——那是下一讲 u2-l2（`IAllocator`）的主题。但我们会反复强调一个结论：**`Tensor` 只描述内存，不分配也不释放内存**。

## 4. 核心概念与源码讲解

### 4.1 数据类型枚举 DataType 与 getTensorType\<T\> 模板映射

#### 4.1.1 概念说明

FT 同时支持 FP32、FP16、BF16、INT8、FP8 等多种精度。一个问题立刻出现：C++ 里 `float`、`half`、`__nv_bfloat16`、`int8_t` 是**不同的类型**，模板函数 `forward<T>` 会被实例化成不同版本；但在「运行期」——比如示例 main 解析到 `data_type=1` 时，或在 `.npy` 文件头里读到类型描述时——我们又需要一个**统一的、与模板参数无关的类型标识**，才能用 `if/else` 做分发，才能把张量存进同一个容器。

这个统一的运行期标识就是 `DataType` 枚举。它把 FT 里所有可能出现的数据类型列成一个枚举值表。配套的 `getTensorType<T>()` 则是一座桥：给定编译期的 C++ 类型 `T`，返回对应的 `DataType` 枚举值。于是「编译期类型」和「运行期类型」就可以互相打通。

#### 4.1.2 核心流程

`DataType` 与 `getTensorType` 的协作流程：

1. 模型/层以模板 `T` 编译（例如 `Bert<half>`）。
2. 在需要拿到运行期类型标识的地方，调用 `getTensorType<T>()`，编译期展开成一串 `std::is_same` 判断，返回如 `TYPE_FP16`。
3. 这个枚举值存进 `Tensor.type` 字段，后续 `sizeBytes()` 算字节数、`.npy` 读写选 numpy 描述符，都靠它。

#### 4.1.3 源码精读

`DataType` 枚举的定义，注意它覆盖了布尔、各种宽度的整型/无符号、FP16/FP32/FP64、BF16、FP8，以及 `TYPE_BYTES`（字节串）、`TYPE_STR`、`TYPE_VOID` 等特殊用途值：

[DataType 枚举定义 — src/fastertransformer/utils/Tensor.h:38-57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L38-L57)

```cpp
typedef enum datatype_enum {
    TYPE_INVALID, TYPE_BOOL, TYPE_UINT8, TYPE_UINT16, TYPE_UINT32, TYPE_UINT64,
    TYPE_INT8, TYPE_INT16, TYPE_INT32, TYPE_INT64,
    TYPE_FP16, TYPE_FP32, TYPE_FP64, TYPE_BYTES, TYPE_BF16, TYPE_FP8_E4M3,
    TYPE_STR, TYPE_VOID,
} DataType;
```

`getTensorType<T>()` 是一个纯模板函数，里面就是一长串 `if (std::is_same<T, ...>::value) return TYPE_XXX;`。关键细节有两个：一是它同时匹配 `T` 和 `const T`（所以对 `const float` 也能识别成 `TYPE_FP32`）；二是 BF16 和 FP8 被包在 `#ifdef ENABLE_BF16` / `#ifdef ENABLE_FP8` 里——这两个宏由 CUDA 版本/架构条件编译控制（见 u1-l2），没开启时这两个分支根本不存在，对应的 `getTensorType<__nv_bfloat16>` 会落到最后的 `return TYPE_INVALID`：

[getTensorType 模板 — src/fastertransformer/utils/Tensor.h:59-99](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L59-L99)

```cpp
template<typename T>
DataType getTensorType() {
    if (std::is_same<T, float>::value || std::is_same<T, const float>::value) return TYPE_FP32;
    else if (std::is_same<T, half>::value || std::is_same<T, const half>::value) return TYPE_FP16;
#ifdef ENABLE_BF16
    else if (std::is_same<T, __nv_bfloat16>::value ...) return TYPE_BF16;
#endif
    // ... int / int8_t / bool / char 等
    else return TYPE_INVALID;
}
```

> 直觉记忆：`getTensorType` 是「C++ 类型 → 枚举」的单向翻译表，编译期求值，零运行期开销。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `getTensorType<T>()` 的映射，并体会条件编译的影响。

**操作步骤**（源码阅读型）：

1. 打开 [Tensor.h:59-99](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L59-L99)。
2. 写一张映射表：`float`→? `half`→? `int`→? `int8_t`→? `bool`→? `char`→?
3. 思考：`__nv_bfloat16` 在没定义 `ENABLE_BF16` 时会返回什么？

**预期结果 / 待本地验证**：

| C++ 类型 `T` | `getTensorType<T>()` |
| --- | --- |
| `float` | `TYPE_FP32` |
| `half` | `TYPE_FP16` |
| `int` | `TYPE_INT32` |
| `int8_t` | `TYPE_INT8` |
| `bool` | `TYPE_BOOL` |
| `char` | `TYPE_BYTES` |
| `__nv_bfloat16`（未开 `ENABLE_BF16`） | `TYPE_INVALID` |

> 若想本地确认，可在 `examples/cpp` 任意示例的 `main` 里临时加一行 `printf("%d\n", fastertransformer::getTensorType<half>());`，编译运行后应打印 `9`（`TYPE_FP16` 在枚举里的序号，从 0 起数）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `getTensorType<T>()` 要同时判断 `T` 和 `const T`？
**答案**：因为同一个张量的数据指针在很多 API 里会被传成 `const T*`（只读视图）。如果不匹配 `const T`，对一个 `const half` 指针调用就会误判成 `TYPE_INVALID`。

**练习 2**：`TYPE_INVALID` 是枚举的第一个值，它的整数值是多少？为什么默认构造的 `Tensor`（见 4.3）要把 `type` 设成它？
**答案**：枚举首个值默认为 `0`。默认构造的 `Tensor` 表示「空/无效张量」，用 `TYPE_INVALID` 标记类型未定，便于下游用 `isValid()` 判空（`size()==0` 或 `data==nullptr`）。

### 4.2 类型转换工具：字节大小、NumPy 描述互转与 convert_data_type.h

#### 4.2.1 概念说明

光有 `DataType` 枚举还不够。围绕它还有三个常见需求：

1. **算字节数**：知道形状和类型后，这块数据到底占多少字节？（分配显存、拷贝都要用。）
2. **与 NumPy 互操作**：FT 经常用 `.npy` 文件保存/加载权重和测试数据，需要把 `DataType` 翻译成 numpy 的类型描述符（如 `"f4"` = float32、`"i4"` = int32），反之亦然。
3. **标量精度转换**：把高精度浮点（`float`）舍入成低精度整型（`int8`），这是量化推理的底层动作之一。

前两个由 `Tensor` 的静态方法 `getTypeSize`、`getNumpyTypeDesc`、`typeFromNumpyDesc` 提供；第三个在 `convert_data_type.h` 里有一个最小实现 `float_to_int8_rn_host`。

#### 4.2.2 核心流程

字节数的计算是本讲唯一的公式，理解它对后续所有显存计算都有用。设张量形状为 \(\text{shape}=(d_0, d_1, \dots, d_{n-1})\)，每个元素的字节大小为 \(s = \text{getTypeSize}(\text{type})\)，则：

\[
\text{size} = \prod_{i=0}^{n-1} d_i, \qquad
\text{sizeBytes} = \text{size} \times s
\]

即「元素总数 × 每元素字节数」。`getTypeSize` 内部就是一张 `DataType → sizeof(...)` 的表，BF16/FP8 同样被 `#ifdef` 守卫。

#### 4.2.3 源码精读

`size()` 把 shape 各维连乘（空 shape 或空指针返回 0）：

[Tensor::size — src/fastertransformer/utils/Tensor.cc:164-170](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L164-L170)

```cpp
size_t Tensor::size() const {
    if (data == nullptr || shape.size() == 0) return 0;
    return std::accumulate(shape.begin(), shape.end(), (size_t)1, std::multiplies<size_t>());
}
```

`sizeBytes()` 直接复用 `size()` 乘以 `getTypeSize(type)`：

[Tensor::sizeBytes — src/fastertransformer/utils/Tensor.cc:172-175](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L172-L175)

`getTypeSize` 是一张 `DataType → sizeof` 的静态表，注意 BF16/FP8 同样条件编译：

[Tensor::getTypeSize — src/fastertransformer/utils/Tensor.cc:232-254](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L232-L254)

NumPy 描述符的双向转换（`getNumpyTypeDesc` 把枚举变 numpy 字符串，`typeFromNumpyDesc` 反过来），用于 `.npy` 文件头解析：

[typeFromNumpyDesc — src/fastertransformer/utils/Tensor.cc:214-230](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L214-L230)

最后是 `convert_data_type.h`。这个文件**很小**，只定义了一个 host 端的标量舍入函数，把 `float` 按「四舍五入 + 钳位到 \([-127, 127]\)」转成 `int8`（量化时常用）：

[float_to_int8_rn_host — src/fastertransformer/utils/convert_data_type.h:22-37](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/convert_data_type.h#L22-L37)

```cpp
int8_t float_to_int8_rn_host(float x) {
    int32_t tmp;
    if (x >= 0) { tmp = int(x + 0.5); tmp = tmp > 127 ? 127 : tmp; }
    else        { tmp = int(x - 0.5); tmp = tmp < -127 ? -127 : tmp; }
    return int8_t(tmp);
}
```

> 诚实说明：文件名 `convert_data_type` 容易让人以为它包含大型类型转换框架，但实际上 FT 里「类型转换」的核心机制是本节讲到的 `getTensorType` / `getTypeSize` / numpy 描述互转（都在 `Tensor.h/.cc`），而本文件只是量化用的一枚标量小工具。大规模的 batch 量化/反量化实际由专门的 GPU kernel 完成（u9-l1 会讲）。

#### 4.2.4 代码实践

**实践目标**：手算一个张量的字节数，验证公式。

**操作步骤**：

1. 给定一个 `Tensor`：`where=MEMORY_GPU, type=TYPE_FP16, shape={2, 8, 64}, data=<某指针>`。
2. 用公式算 `size` 和 `sizeBytes`。

**预期结果**：

\[
\text{size} = 2 \times 8 \times 64 = 1024, \qquad
\text{sizeBytes} = 1024 \times \text{sizeof(half)} = 1024 \times 2 = 2048 \text{ 字节}
\]

**需要观察的现象**：若把 `type` 改成 `TYPE_FP32`，`sizeBytes` 应翻倍为 4096；改成 `TYPE_INT8` 则减半为 1024。这正是低精度推理省显存/省带宽的来源。

#### 4.2.5 小练习与答案

**练习 1**：一个 `shape={batch, seq_len, hidden}` 的 `TYPE_FP16` 张量，`sizeBytes` 公式是什么？
**答案**：`batch × seq_len × hidden × 2` 字节。

**练习 2**：`getNumpyTypeDesc(TYPE_BF16)` 会返回什么？为什么？
**答案**：返回 `"x"`（无效标记），并打一条 WARNING。因为 NumPy 至今不原生支持 bfloat16（见代码 [Tensor.cc:273-277](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L273-L277)），所以 BF16 张量无法用普通 `.npy` 直接落盘。

### 4.3 MemoryType 与 Tensor：位置标记与「不拥有内存」的描述符

#### 4.3.1 概念说明

`Tensor` 是 FT 里最重要、出现频率最高的数据结构。但它的设计哲学可能和初学者直觉相反：**它是一个非常轻量的「描述符/视图」，而不是一个会自己管理内存的对象。**

理解这一点是本讲（也是整个进阶层）的关键。`Tensor` 做的全部事情，就是把四样信息打包在一起：

- `where`：数据在 CPU、CPU 锁页内存，还是 GPU 显存。
- `type`：元素的 `DataType`。
- `shape`：各维大小。
- `data`：一个 `const void*` 指针，指向真正的数据缓冲。

它**不分配**这块缓冲，**不释放**这块缓冲，也**不持有**它的所有权。真正分配/回收显存的是 `IAllocator`（下一讲 u2-l2）或外部框架（torch/tf 的张量）。`Tensor` 只是「指过去」。这种设计让 `Tensor` 可以极便宜地拷贝、切片、传递，而无需担心双重释放（double free）。

#### 4.3.2 核心流程

`Tensor` 的生命周期里发生什么：

1. 某处用 `allocator->malloc(...)` 拿到一块 GPU 显存指针 `buf`。
2. 构造一个 `Tensor(MEMORY_GPU, TYPE_FP16, {batch, seq, hidden}, buf)`，它只是把这四样信息记下来。
3. 把这个 `Tensor`（按值或按指针）传给 kernel/layer/forward；它们用 `tensor.getPtr<T>()` 取出 `(T*)data` 喂给 CUDA。
4. 缓冲的真正释放由 `allocator`（或框架）负责，与 `Tensor` 无关。

`MemoryType` 枚举只有三个值：

[MemoryType 枚举 — src/fastertransformer/utils/Tensor.h:101-105](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L101-L105)

```cpp
typedef enum memorytype_enum {
    MEMORY_CPU,
    MEMORY_CPU_PINNED,  // 锁页内存，可被 DMA 直接访问，常用于异步 H2D/D2H 拷贝
    MEMORY_GPU
} MemoryType;
```

#### 4.3.3 源码精读

`Tensor` 结构体的字段定义。注意 **所有字段都是 `const`**——这意味着一个 `Tensor` 构造好之后，它的 `where/type/shape/data` 都不可再赋值（`updateShape` 用 `const_cast` 绕过，是受控的例外）。这进一步说明它是「不可变的描述视图」：

[Tensor 结构体字段 — src/fastertransformer/utils/Tensor.h:107-120](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L107-L120)

```cpp
struct Tensor {
    const MemoryType          where;
    const DataType            type;
    const std::vector<size_t> shape;
    const void*               data;     // 仅持有指针，不拥有内存
    const std::vector<size_t> offsets = std::vector<size_t>{};
    // ...
};
```

三个构造函数：默认构造出「空张量」（`TYPE_INVALID`/`nullptr`）；另两个接受 `(where, type, shape, data[, offsets])`，都只是在初始化列表里抄写参数，**没有任何 `malloc`/`cudaMalloc`**：

[Tensor 构造函数实现 — src/fastertransformer/utils/Tensor.cc:36-58](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L36-L58)

`Tensor` 提供带类型检查的取值/取指针模板。`getPtr<T>()` 会比较 `getTensorType<T>()` 与 `type` 是否一致，不一致只打 DEBUG 日志（不硬报错，留有 `const` 间转换的余地），然后返回 `(T*)data`：

[getVal / getPtr 模板 — src/fastertransformer/utils/Tensor.h:135-173](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L135-L173)

`slice()` 是 `Tensor` 「零拷贝视图」能力的体现：它返回一个新的 `Tensor`，复用同一块 `data` 指针（仅偏移 `offset` 个元素），换一个新 `shape`。这正是「不拥有内存、只描述内存」带来的红利——切片几乎零成本：

[Tensor::slice — src/fastertransformer/utils/Tensor.cc:334-346](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L334-L346)

> 一个所有权上的诚实提醒：`Tensor` 没有析构函数去 `free(data)`。因此像 `loadNpy()`（[Tensor.cc:135-162](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L135-L162)）这种**自己** `malloc`/`cudaMalloc` 出来的缓冲，构造完 `Tensor` 返回后，调用方必须自己记得释放——FT 没有替你管。这反过来说明：**常规路径下显存管理由 `IAllocator`（u2-l2）统一负责，`Tensor` 始终只是旁观者。**

#### 4.3.4 代码实践

**实践目标**：用源码证据确认「`Tensor` 不拥有内存」。

**操作步骤**：

1. 在 [Tensor.h:107-302](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L107-L302) 的 `struct Tensor` 定义里查找：有没有析构函数 `~Tensor()`？有没有任何 `malloc`/`cudaMalloc`/`cudaFree`？
2. 在 [Tensor.cc:36-58](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L36-L58) 的构造函数里查找分配语句。

**预期结果**：`struct Tensor` 内**没有** `~Tensor()`，构造函数里**没有**任何分配。所有字段都是 `const`，`data` 是 `const void*`。结论：`Tensor` 是非拥有（non-owning）描述符。

**需要观察的现象**：这解释了为什么 FT 可以放心地把 `Tensor` 按值塞进 `std::vector<Tensor>` 和 `std::unordered_map` 而不会触发深拷贝或双重释放——拷贝的只是「四样描述信息 + 一个指针」。

#### 4.3.5 小练习与答案

**练习 1**：`getVal<T>(index)` 里有一行 `FT_CHECK(where == MEMORY_CPU);`，为什么？如果对一个 `MEMORY_GPU` 张量调 `getVal` 会怎样？
**答案**：`getVal` 要直接读 `((T*)data)[index]`，这是 CPU 解引用。GPU 指针不能在主机端直接读，所以强行要求 `where == MEMORY_CPU`（含 PINNED 之外的主机内存）。对 GPU 张量调用会触发断言失败（DEBUG 下）或未定义行为。

**练习 2**：`slice()` 改变的是 `data` 指针的拥有权吗？两个切片张量共享什么？
**答案**：不改变拥有权（本来就没有）。两个切片共享同一块底层 `data` 缓冲，只是 `shape` 不同、起点 `offset` 不同。

### 4.4 TensorMap：命名集合与统一 forward 接口

#### 4.4.1 概念说明

单个 `Tensor` 描述一块数据。但一次模型前向需要**很多块**数据：输入有 `input_ids`、`sequence_lengths`、`attention_mask`…输出有 `output_tokens`、`output_log_probs`…。怎么把这些张量打包传给 `forward`？

早期 FT 用 `std::vector<Tensor>`，靠**位置顺序**约定每个槽位是谁——第 0 个是输入 id、第 1 个是 seq len……这种方式一旦有人在中间插一个张量，所有索引都得改，非常脆弱。

新接口改用 `TensorMap`：一个 `std::unordered_map<std::string, Tensor>`，**按名字**存取张量。于是 `forward` 的签名变成 `forward(TensorMap* output_tensors, TensorMap* input_tensors, ...)`，内部用 `input_tensors->at("input_ids")` 取值。加字段、改顺序都不影响已有代码，可读性也强得多。这也是把 FT 接进 Triton backend 时用到的接口形态（u10-l3）。

#### 4.4.2 核心流程

`TensorMap` 的工作流：

1. 调用方构造若干 `Tensor`，用 `insert("name", tensor)` 或初始化列表塞进 `TensorMap`。
2. `insert` 会校验：key 不能重复、tensor 必须有效（`size()>0` 且 `data!=nullptr`）。
3. 把 `TensorMap*` 传给 `model.forward`。
4. 模型内部用 `at("name")` 取引用，或用模板版 `getPtr<T>("name")` 直接拿到类型化指针喂给 kernel。
5. 可选地用 `isExist("name")` 判断某个可选输入是否提供（很多 runtime 参数这么传）。

#### 4.4.3 源码精读

`TensorMap` 类的本质就是一个 `unordered_map<string, Tensor>` 加一组便捷方法：

[TensorMap 类与成员 — src/fastertransformer/utils/Tensor.h:304-338](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L304-L338)

```cpp
class TensorMap {
private:
    std::unordered_map<std::string, Tensor> tensor_map_;
    inline bool isValid(const Tensor& tensor) { return tensor.size() > 0 && tensor.data != nullptr; }
public:
    inline void insert(const std::string& key, const Tensor& value) {
        FT_CHECK_WITH_INFO(!isExist(key), ...);          // 禁止重复 key
        FT_CHECK_WITH_INFO(isValid(value), ...);          // 禁止空张量
        tensor_map_.insert({key, value});
    }
    ...
};
```

注意一个很用心的 API 设计：`at(int)` 和 `at(size_t)` 被 `= delete`，目的是**阻止**整数被隐式转成字符串当 key 用（否则 `at(0)` 会悄悄变成 `at("0")` 而不报错）：

[禁止隐式整型→字符串转换 — src/fastertransformer/utils/Tensor.h:353-354](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L353-L354)

`TensorMap` 还提供 `getPtr<T>("name")`、`getVal<T>("name")`、`getVal<T>("name", default)` 等模板方法，找不到 key 时有的会断言报错并列出所有现有 key，有的会返回默认值——后者正是「可选运行期参数」的传参方式：

[getPtr 模板（带默认值） — src/fastertransformer/utils/Tensor.h:458-465](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L458-L465)

`TensorMap` 支持三种构造方式：从 `unordered_map`、从 `vector<Tensor>`（自动以 `"0"/"1"/...` 为 key，兼容老接口）、从初始化列表：

[TensorMap 三种构造函数 — src/fastertransformer/utils/Tensor.cc:348-377](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L348-L377)

真实模型如何用：BERT 提供了两套 `forward` 重载——老的按位置 `vector<Tensor>` 和新的按名字 `TensorMap`：

[Bert::forward 两套重载 — src/fastertransformer/models/bert/Bert.h:127-130](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.h#L127-L130)

在 `Bert.cc` 的实现里，到处都是这种「按名字取张量、再取带偏移的类型化指针」的写法：

[按名字取张量并取指针 — src/fastertransformer/models/bert/Bert.cc:393-403](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L393-L403)

```cpp
input_tensors->at("sequence_lengths").getPtrWithOffset<int>(ite * local_batch_size)
...
bert_input_ptr  = input_tensors->at("input_hidden_state").getPtrWithOffset<T>(hidden_offset);
bert_output_ptr = output_tensors->at("output_hidden_state").getPtrWithOffset<T>(hidden_offset);
```

> 这种「`output_tensors->at("xxx")`」就是 FT 新接口里最常见的句式。看懂它，你就看懂了几乎所有模型的 forward 入口。

#### 4.4.4 代码实践

**实践目标**：读懂一段 `TensorMap` 的真实使用，并仿写一段。

**操作步骤**：

1. 打开 [Bert.cc:393-403](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L393-L403)。
2. 解释这行做了什么：`input_tensors->at("sequence_lengths").getPtrWithOffset<int>(offset)`。
3. 仿写一段：从 `input_tensors` 取出名为 `input_ids` 的 INT32 张量的 `int*` 指针。

**预期结果**：第 2 步——先用 `at("sequence_lengths")` 拿到那个 `Tensor` 的引用，再用 `getPtrWithOffset<int>(offset)` 返回 `((int*)data) + offset`，即从第 `offset` 个 int 开始的指针。第 3 步示例代码：

```cpp
// 示例代码（仅示意调用形式，非项目原有代码）
int* ids_ptr = input_tensors->at("input_ids").getPtr<int>();
```

**需要观察的现象**：如果把 key 字符串写错（如 `"seq_len"`），运行时 `at()` 会断言失败，并打印出当前 `TensorMap` 里所有合法的 key（见 [Tensor.h:359-363](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L359-L363)），非常便于排错。

#### 4.4.5 小练习与答案

**练习 1**：`TensorMap::at(int)` 为什么被声明为 `= delete`？
**答案**：防止 `at(0)` 把整数 `0` 隐式转成字符串 `"0"` 去查找，从而掩盖「写错 key」的 bug。删掉后，`at(0)` 会编译报错，强制你用字符串 key。

**练习 2**：老接口 `forward(std::vector<Tensor>*, ...)` 和新接口 `forward(TensorMap*, ...)` 各自的优缺点？
**答案**：`vector` 按位置约定，简单但脆弱（插一个张量全乱）、可读性差；`TensorMap` 按名字约定，健壮（顺序无关）、自描述、便于传可选参数，代价是字符串查找的微小开销（推理热路径上几乎可忽略）。FT 新模型普遍用 `TensorMap`。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个综合任务（对应本讲的 practice_task）。

**任务**：写一段伪代码，构造一个 `TensorMap`，放入名为 `input_ids` 的 INT32 GPU 张量和名为 `output_tokens` 的 INT32 GPU 张量；然后说明 `Tensor` 如何区分 CPU/GPU、以及它是否拥有内存。

**参考伪代码**（示例代码，非项目原有代码）：

```cpp
// ---- 假设外部已经分配好两块 GPU 显存（实际由 IAllocator 负责，见 u2-l2）----
int*   d_input_ids   = nullptr;   // 指向 GPU 上 batch×seq_len 个 int
int*   d_output_tok  = nullptr;   // 指向 GPU 上 batch×max_gen_len 个 int
size_t batch = 2, seq_len = 16, max_gen_len = 32;

// ---- 1. 构造两个 Tensor：只描述，不分配 ----
Tensor input_ids_tensor(
    MEMORY_GPU,                          // where：数据在 GPU
    TYPE_INT32,                          // type：getTensorType<int>() == TYPE_INT32
    std::vector<size_t>{batch, seq_len}, // shape
    (void*)d_input_ids);                 // data：外部已有的指针

Tensor output_tokens_tensor(
    MEMORY_GPU,
    TYPE_INT32,
    std::vector<size_t>{batch, max_gen_len},
    (void*)d_output_tok);

// ---- 2. 装进 TensorMap（按名字）----
TensorMap input_map({
    {"input_ids",     input_ids_tensor},
    {"output_tokens", output_tokens_tensor},
});

// ---- 3. 交给模型 forward（示意）----
// model.forward(&output_map, &input_map, &weights);

// ---- 4. 内部按名字取回类型化指针 ----
int* ids = input_map.at("input_ids").getPtr<int>();        // ((int*)data)
int* out = input_map.at("output_tokens").getPtrWithOffset<int>(0);
```

**需要你在伪代码旁回答的两个问题**：

1. **`Tensor` 如何区分 CPU/GPU？**
   答：靠 `where` 字段（`MemoryType` 枚举：`MEMORY_CPU` / `MEMORY_CPU_PINNED` / `MEMORY_GPU`）。本例两个张量都标成 `MEMORY_GPU`，kernel 才会把 `data` 当作显存指针使用。

2. **`Tensor` 是否拥有内存？**
   答：**不拥有**。`Tensor` 的所有字段都是 `const`，`data` 是 `const void*`，没有析构函数，构造函数里也没有任何 `malloc`/`cudaMalloc`。它只是「指过去」的描述符。`d_input_ids` / `d_output_tok` 这两块显存的分配与释放，由 `IAllocator`（u2-l2）或外部框架负责，与这两个 `Tensor` 对象无关。这也是为什么 `TensorMap` 销毁时（[Tensor.cc:379-382](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L379-L382)）只是 `clear()` map，而不去 free 任何 `data`。

**延伸观察（待本地验证）**：在伪代码末尾加一行 `printf("%s\n", input_map.at("input_ids").toString().c_str());`，应打印类似 `Tensor[where=GPU, type=INT32, shape=[2, 16], data=0x7f...]` 的自描述字符串——这正来自 [Tensor.cc:184-212](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.cc#L184-L212) 的 `toString()`，可用来快速核对 `where/type/shape` 是否符合预期。

## 6. 本讲小结

- `DataType` 是 FT 的统一运行期类型标识，覆盖 FP16/FP32/BF16/INT8/FP8 等；`getTensorType<T>()` 在编译期把 C++ 类型映射成它，是「枚举→模板」dispatch 机制的反向桥梁。
- `Tensor` 是一个非常轻量的**非拥有**描述符：`where`(CPU/CPU_PINNED/GPU) + `type` + `shape` + `data`(指针) + `offsets`，全部 `const`，不分配也不释放内存；`sizeBytes() = size() × getTypeSize(type)`。
- `TensorMap` 是按名字索引的 `unordered_map<string, Tensor>`，提供 `at("name")`、`getPtr<T>("name")`、`isExist`、`insert` 等方法，并刻意 `delete` 了整型 `at` 以防隐式转换 bug。
- 模型 `forward` 的新接口统一为 `forward(TensorMap* output, TensorMap* input, ...)`，内部到处用 `input_tensors->at("name").getPtr<T>()`，比老的按位置 `vector<Tensor>` 更健壮、更自描述。
- 类型转换的核心机制（`getTensorType`/`getTypeSize`/NumPy 描述互转）集中在 `Tensor.h/.cc`；`convert_data_type.h` 只是一个量化的标量小工具 `float_to_int8_rn_host`。
- 因为 `Tensor` 不持有内存，所以真正的显存管理交给下一讲的 `IAllocator`——这是理解后续所有 buffer 复用的前提。

## 7. 下一步学习建议

- **强烈建议接着学 u2-l2（Allocator 与 memory_utils）**：本讲反复强调「`Tensor` 不拥有内存」，那么内存到底由谁分配回收？答案就是 `IAllocator` 与 `ReallocType`。学完它，你才能把 `Tensor.data` 的指针来历彻底讲清楚。
- 之后学 **u2-l3（cublasMMWrapper）** 时，会看到 GEMM 的输入输出同样是 `Tensor` 持有的指针；那时你会真切体会到本讲的抽象如何被复用。
- 想立刻看 `TensorMap` 在端到端场景里的威力，可以跳读 [Bert.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc) 的 forward，数一数里面有多少处 `at("...")`。
- 如果你对条件编译（`ENABLE_BF16`/`ENABLE_FP8`）如何影响 `getTensorType` 和 `getTypeSize` 感兴趣，可回看 u1-l2 的 CMake 选项部分，把「构建开关 → 宏 → 类型表」这条链补全。
