# CUDA kernel 体：CHECK 宏与 dtype 分发

## 1. 本讲目标

上一篇 u2-l2 我们停在「分派到 C++ 函数门口」：一行 `torch.ops.sgl_kernel.<name>.default(...)` 经过 `m.impl` 绑定，最终来到了一个被 `TORCH_LIBRARY_FRAGMENT` 注册的 C++ 函数面前。本讲要**推开门走进函数体内部**，看看一个 CUDA 算子的 C++ 实现到底做了哪三件事：

1. **校验输入**：用一套 `CHECK_*` 宏确保张量在 GPU 上、内存连续、维度形状正确。
2. **分派 dtype**：用 `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_*` 宏把运行时的 PyTorch dtype 映射到编译期的 C++ 类型，再调用同一份模板逻辑。
3. **取 stream 并调用底层**：取出 PyTorch 当前 CUDA stream，传给 vendored 的 FlashInfer 模板实现，并把 CUDA 错误码翻译回 Python。

学完本讲，你应该能够：

- 读懂任意一个 sgl-kernel CUDA 算子函数体的「校验 → 分派 → 调用」三段式结构。
- 知道 `CHECK_INPUT` / `CHECK_DIM` / `CHECK_EQ` 这些宏各自检查什么、为什么必须检查。
- 理解 `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16` 为什么用一个 `switch` + `using c_type = ...` 就能完成「运行时 dtype → 编译期类型」的桥接。
- 解释 `at::cuda::getCurrentCUDAStream()` 的作用，以及为什么要把 CUDA 错误码 `TORCH_CHECK` 一道。

本讲全程以 [`fused_add_rms_norm_kernel.cu`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu) 这个真实算子为样本。

## 2. 前置知识

### 什么是 RMSNorm

RMSNorm（Root Mean Square Normalization）是 LLM 里最常见的归一化层，比 LayerNorm 省去了「减均值」那一步。对一个长度为 \(H\) 的向量 \(x\)，它先算均方根，再做缩放：

\[
\mathrm{RMS}(x) = \sqrt{\frac{1}{H}\sum_{i=1}^{H} x_i^2 + \varepsilon}
\]

\[
\mathrm{out}_i = \frac{x_i}{\mathrm{RMS}(x)} \cdot w_i
\]

其中 \(w\) 是可学习权重，\(\varepsilon\) 是防止除零的小常数（默认 `1e-6`）。

### 什么是 fused_add_rmsnorm

在 Transformer 的残差结构里，常见两步：先把上一层的输出 `input` 加到残差流 `residual` 上，再对新残差做归一化。fused 版把这两步融进**同一个 kernel**，避免多一次显存往返：

```
Step 1:  residual[i] += input[i]
Step 2:  input[i] = (residual[i] / RMS(residual)) * weight[i]
```

注意两个细节：Step 1 是**原地改 `residual`**；Step 2 是**原地改 `input`**（用归一化结果覆盖它）。这就是 u2-l2 讲过的 `Tensor!` 可变语义在算法层面的体现——调用者预分配好 `input`/`residual`，kernel 直接往里写，省掉高频显存分配。

### 为什么要在 C++ 函数体里做这么多检查

Python 侧的 `torch.ops.sgl_kernel.<name>` 是一个「裸」入口，它不会自动帮你检查张量是不是在 GPU 上、形状对不对。如果错误数据直接喂给 GPU kernel，你往往只会得到一个不知所云的 `CUDA error: an illegal memory access`，甚至程序在几百毫秒后才在**另一个无关 kernel** 上崩溃。所以 sgl-kernel 在 C++ 函数体的最前面统一做防御式校验，把错误**前置、明确化**，变成一条能直接看懂的 Python 异常。

### 一个小术语：data_ptr 与连续性

PyTorch 张量有个 `data_ptr()` 方法，返回一块裸显存的首地址。CUDA kernel 靠指针算术（如 `ptr[row * stride + col]`）寻址。这要求张量在内存里是**连续排布**（contiguous）的——否则指针算术就读到错位的数据。这就是后面 `CHECK_CONTIGUOUS` 存在的根本原因。

## 3. 本讲源码地图

本讲涉及四个文件，分别对应算子链路的不同环节（回顾 u1-l2 的六步流水线）：

| 文件 | 在链路中的环节 | 本讲作用 |
| --- | --- | --- |
| [`csrc/elementwise/fused_add_rms_norm_kernel.cu`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu) | ① CUDA/C++ 实现 | 本讲主角：三段式函数体 |
| [`include/utils.h`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h) | ② 头文件声明（公共工具） | `CHECK_*` 宏与 `DISPATCH_*` 宏的定义 |
| [`include/sgl_kernel_ops.h`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h) | ② 头文件声明（算子原型） | `sgl_fused_add_rmsnorm` 的函数原型 |
| [`python/sgl_kernel/elementwise.py`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py) | ④ Python 包装 | 调用 `torch.ops.sgl_kernel.fused_add_rmsnorm.default` |

辅助阅读（注册环节，回顾 u2-l2）：

- [`csrc/common_extension.cc`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc)：`fused_add_rmsnorm` 的 `m.def`/`m.impl` 注册（L67-L68）。

## 4. 核心概念与源码讲解

### 4.1 输入校验宏体系

#### 4.1.1 概念说明

任何一个从 Python 进入 C++ 的张量，在喂给 GPU 之前都要回答三个问题：

1. **它在 GPU 上吗？**（device 检查）
2. **它在内存里连续吗？**（contiguity 检查）
3. **它的维度、形状符合预期吗？**（dim / shape 检查）

sgl-kernel 在 [`include/utils.h`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h) 里定义了一套 `CHECK_*` 宏来回答这些问题。它们都**薄薄地包了一层 PyTorch 的 `TORCH_CHECK`**：条件不满足时，`TORCH_CHECK` 抛出一个 C++ 异常，PyTorch 的绑定层会把它翻译成 Python 的 `RuntimeError`，于是用户在 Python 侧就能看到一条清晰的报错，附带出错张量的名字和实际值。

这套宏的设计哲学是「**越早失败、越明确越好**」：与其让错误数据流到 GPU 里引发难定位的崩溃，不如在 C++ 函数体第一行就用一行宏拦住。

#### 4.1.2 核心流程

`fused_add_rmsnorm` 的校验流程可以拆成四步：

```
┌─ ① 基本属性校验 ────────────────────────────────────────┐
│  CHECK_INPUT(input)      // 在 GPU + 连续                          │
│  CHECK_INPUT(residual)   // 在 GPU + 连续                          │
│  CHECK_INPUT(weight)     // 在 GPU + 连续                          │
└──────────────────────────────────────────────────────────┘
┌─ ② 设备一致性校验 ──────────────────────────────────────┐
│  device = input.device()                                           │
│  CHECK_EQ(residual.device(), device)  // 三个张量在同一张卡         │
│  CHECK_EQ(weight.device(),  device)                                │
└──────────────────────────────────────────────────────────┘
┌─ ③ 维度校验 ────────────────────────────────────────────┐
│  CHECK_DIM(2, input)     // input/residual 是 2D                   │
│  CHECK_DIM(2, residual)                                            │
│  CHECK_DIM(1, weight)    // weight 是 1D                           │
└──────────────────────────────────────────────────────────┘
┌─ ④ 形状兼容性校验 ──────────────────────────────────────┐
│  CHECK_EQ(input.size(0), residual.size(0))  // batch 相等          │
│  CHECK_EQ(input.size(1), residual.size(1))  // hidden 相等         │
│  CHECK_EQ(input.size(1), weight.size(0))    // hidden 对齐权重     │
└──────────────────────────────────────────────────────────┘
```

为什么要把「设备一致性」单独拎出来？因为单张张量在 GPU 上还不够，**多张量必须在同一张 GPU 上**——跨卡的指针算术和 kernel launch 都是不允许的。

#### 4.1.3 源码精读

先看 `fused_add_rmsnorm` 函数体里使用这些宏的部分（[fused_add_rms_norm_kernel.cu:24-39](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L24-L39)）：

```cpp
void sgl_fused_add_rmsnorm(
    torch::Tensor input, torch::Tensor residual, torch::Tensor weight,
    double eps, bool enable_pdl) {
  CHECK_INPUT(input);
  CHECK_INPUT(residual);
  CHECK_INPUT(weight);
  auto device = input.device();
  CHECK_EQ(residual.device(), device);
  CHECK_EQ(weight.device(), device);
  CHECK_DIM(2, input);     // input: (batch_size, hidden_size)
  CHECK_DIM(2, residual);  // residual: (batch_size, hidden_size)
  CHECK_DIM(1, weight);    // weight: (hidden_size)
  CHECK_EQ(input.size(0), residual.size(0));
  CHECK_EQ(input.size(1), residual.size(1));
  CHECK_EQ(input.size(1), weight.size(0));
  unsigned int batch_size  = input.size(0);
  unsigned int hidden_size = input.size(1);
  // ...（后续取 stream + 调用底层，见 4.3）
}
```

这里用到的每个宏，都能在 [`utils.h`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h) 里找到定义。先看「基本属性」三件套（[utils.h:193-212](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L193-L212)）：

```cpp
#define CHECK_CUDA(x)       TORCH_CHECK(x.is_cuda(), #x " must be a CUDA tensor")
#define CHECK_CONTIGUOUS(x) TORCH_CHECK(x.is_contiguous(), #x " must be contiguous")

#define CHECK_INPUT(x) \
  CHECK_CUDA(x);       \
  CHECK_CONTIGUOUS(x)

#define CHECK_DIM(d, x) TORCH_CHECK(x.dim() == d, #x " must be a " #d "D tensor")
#define CHECK_EQ(a, b)  TORCH_CHECK((a) == (b), "CHECK_EQ(" #a ", " #b ") failed. ", a, " vs ", b)
#define CHECK_GE(a, b)  TORCH_CHECK((a) >= (b), "CHECK_GE(" #a ", " #b ") failed. ", a, " vs ", b)
```

要点：

- `CHECK_INPUT` 就是 `CHECK_CUDA` + `CHECK_CONTIGUOUS` 的组合宏——最常见的「裸输入」默认检查。
- `#x` 是预处理器的「字符串化」：把宏参数 `input` 变成字符串 `"input"`，这样报错信息里会带上出错的张量名，例如 `input must be a CUDA tensor`。
- `CHECK_DIM(d, x)` 里的 `#d` 同样把 `2` 字符串化成 `"2"`，拼出 `must be a 2D tensor`。
- `CHECK_EQ(a, b)` 失败时会打印两边的实际值（`a vs b`），这对调试形状不匹配极其有用。

还有一组「连续性」的细分宏和形状检查工具（[utils.h:196-208](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L196-L208)），供那些不要求整张连续、但要求最后一维连续的算子使用：

```cpp
#define CHECK_LAST_DIM_CONTIGUOUS(x) \
  TORCH_CHECK(x.strides()[x.strides().size() - 1] == 1, #x "must be contiguous at last dimension")

#define CHECK_LAST_DIM_CONTIGUOUS_INPUT(x) \
  CHECK_CUDA(x);                           \
  CHECK_LAST_DIM_CONTIGUOUS(x)

#define CHECK_SHAPE(a, b) check_shape(a, b, #a, #b)
```

`CHECK_SHAPE(a, b)` 调用的是头文件里的内联函数 [`check_shape`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L173-L178)，它逐维比较两个张量的 `size`，任一维不等就报错：

```cpp
inline void check_shape(const at::Tensor& a, const at::Tensor& b,
                        const char* a_name, const char* b_name) {
  TORCH_CHECK(a.dim() == b.dim(), a_name, ".dim() != ", b_name, ".dim(). ", a.dim(), " vs ", b.dim());
  for (int i = 0; i < a.dim(); ++i) {
    TORCH_CHECK(a.size(i) == b.size(i), a_name, ".size(", i, ") != ", b_name, ".size(", i, ")");
  }
}
```

> 小结：sgl-kernel 的校验体系就是「`TORCH_CHECK` + 预处理器字符串化 `#x`」的组合，外加几个常用组合宏（`CHECK_INPUT` / `CHECK_SHAPE`）。它把「防御式编程」变成了几乎零成本的几行宏调用。

#### 4.1.4 代码实践

**实践目标**：亲手触发 `fused_add_rmsnorm` 的各种校验宏，观察它们的报错信息，从而验证「校验确实前置在 C++ 函数体里」。

**操作步骤**（需要一台装好 sgl-kernel 与 GPU 的机器；若无，则按「源码阅读型实践」处理，见步骤 5）：

1. 用一个 CPU 张量调用算子，触发 `CHECK_CUDA`：

```python
import torch
from sgl_kernel import fused_add_rmsnorm  # 或 from sgl_kernel.elementwise import fused_add_rmsnorm

x = torch.randn(4, 8)              # 注意：在 CPU 上
r = torch.randn(4, 8)
w = torch.randn(8)
fused_add_rmsnorm(x, r, w, eps=1e-6)   # 预期抛出 "... must be a CUDA tensor"
```

2. 把 `x` 换成 1D 张量，触发 `CHECK_DIM`：

```python
x = torch.randn(8, device="cuda")      # 维度不对
fused_add_rmsnorm(x, r, w, eps=1e-6)   # 预期 "input must be a 2D tensor"
```

3. 让 `weight` 的长度与 `hidden_size` 不匹配，触发 `CHECK_EQ`：

```python
x = torch.randn(4, 8, device="cuda")
r = torch.randn(4, 8, device="cuda")
w = torch.randn(7, device="cuda")      # 应为 8
fused_add_rmsnorm(x, r, w, eps=1e-6)   # 预期 "CHECK_EQ(...) failed. 8 vs 7"
```

4. **需要观察的现象**：每次报错信息都**带张量名、带实际值**，而且是在 Python 侧以 `RuntimeError` 形式抛出，说明校验发生在 C++ 函数体、并通过 `TORCH_CHECK` 翻译了回来。

5. **源码阅读型实践（无 GPU 时）**：打开 [fused_add_rms_norm_kernel.cu:26-37](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L26-L37)，列出 `input`/`residual`/`weight` 各自接受了哪些校验，并对照 [utils.h:193-212](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L193-L212) 逐条写出「不满足时会报什么错」。

**预期结果**：能复现上述三类报错；无法在本机运行时，明确写「待本地验证」并把每条 `CHECK_*` 对应的报错文案列出来。

#### 4.1.5 小练习与答案

**练习 1**：`CHECK_INPUT(x)` 展开后等价于哪两条检查？为什么 `fused_add_rmsnorm` 三个张量都要过它？

> **答案**：等价于 `CHECK_CUDA(x); CHECK_CONTIGUOUS(x);`，即「在 GPU 上 + 内存连续」。三个张量都要过，是因为 kernel 用 `data_ptr()` 裸指针寻址——任何一个不在 GPU 或不连续，都会读到错误内存。

**练习 2**：`CHECK_DIM(1, weight)` 里 `1` 这个字面量是怎么出现在最终报错信息里的？

> **答案**：通过预处理器的字符串化运算符 `#d`，宏参数 `1` 被转成字符串 `"1"`，再拼进 `"must be a 1D tensor"`。所以这是**编译期**的字符串拼接，不产生运行时开销。

---

### 4.2 dtype 编译期分派宏

#### 4.2.1 概念说明

校验完张量的形状和设备后，下一个难题是**数据类型（dtype）**。

底层 CUDA 模板（这里借用自 FlashInfer 的 `norm::FusedAddRMSNorm`）是**按具体 C++ 类型实例化**的——比如 `FusedAddRMSNorm<float*>`、`FusedAddRMSNorm<nv_bfloat16*>` 是几段不同的机器码。模板里的向量化加载（一次读多个元素、用 `uint4` 拼）依赖**元素的字节大小**，而字节大小必须是编译期常量，所以类型必须在编译期确定。

可是 PyTorch 张量的 dtype 是**运行时**的值——`input.scalar_type()` 返回一个 `at::ScalarType` 枚举（如 `Float`、`Half`、`BFloat16`），要等到运行才知道。这就产生了一个「运行时枚举 → 编译期类型」的鸿沟。

`DISPATCH_PYTORCH_DTYPE_TO_CTYPE_*` 这族宏（借鉴自 FlashInfer）就是用来填这道鸿沟的桥：它在运行时 `switch` 这个枚举，在每个 `case` 分支里用 `using c_type = <具体类型>;` 把一个**编译期类型别名**绑定出来，然后立刻执行你给的代码块（一个 lambda）。这样你的代码块就能像「类型已经确定」一样调用模板了。

#### 4.2.2 核心流程

宏展开后是一个**立即调用的 lambda**（IIFE），返回 `bool`：

```cpp
[&]() -> bool {
  switch (pytorch_dtype) {        // 运行时的 dtype 枚举
    case at::ScalarType::Float: {
      using c_type = float;       // 绑定编译期类型
      return __VA_ARGS__();       // 执行你传入的 lambda，c_type 在作用域内可见
    }
    _DISPATCH_CASE_F16(...)       // 展开成 case Half:    { using c_type = nv_half;     ... }
    _DISPATCH_CASE_BF16(...)      // 展开成 case BFloat16:{ using c_type = nv_bfloat16; ... }
    default:
      TORCH_CHECK(false, "... failed to dispatch data type ...");  // 不支持的 dtype 明确报错
      return false;
  }
}()
```

三个关键点：

1. **`switch` 是运行时的**，由 `scalar_type()` 决定走哪个 `case`。
2. **每个 `case` 里的 `using c_type = ...` 是编译期的**，于是该分支内的模板实例化是确定的、可向量化的。
3. **`default` 分支用 `TORCH_CHECK(false, ...)`**，对不支持的 dtype（比如 `int`、`double`）抛出带 `__PRETTY_FUNCTION__`（当前函数签名）的明确错误，而不是悄悄走错。

sgl-kernel 提供了多种「分派子集」，对应不同算子支持的类型组合：

| 宏 | 支持的 dtype |
| --- | --- |
| `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FP16` | Half + BFloat16 |
| `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FP8` | Float8_e4m3fn + Float8_e5m2 |
| `DISPATCH_PYTORCH_DTYPE_TO_CTYPE` | Half + BFloat16 + 两种 FP8 |
| `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16` | **Float** + Half + BFloat16（本算子用） |

`fused_add_rmsnorm` 选了 `..._FLOAT_FP16`，因为它要支持 float32、float16、bfloat16 三种（见源码注释 `// support float16, bfloat16 and float32`）。

#### 4.2.3 源码精读

先看 `fused_add_rmsnorm` 里如何使用（[fused_add_rms_norm_kernel.cu:43-58](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L43-L58)）：

```cpp
// support float16, bfloat16 and float32
DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16(input.scalar_type(), c_type, [&] {
  cudaError_t status = norm::FusedAddRMSNorm(
      static_cast<c_type*>(input.data_ptr()),
      static_cast<c_type*>(residual.data_ptr()),
      static_cast<c_type*>(weight.data_ptr()),
      batch_size, hidden_size,
      input.stride(0), residual.stride(0),
      eps, enable_pdl, torch_current_stream);
  TORCH_CHECK(status == cudaSuccess,
      "FusedAddRMSNorm failed with error code " + std::string(cudaGetErrorString(status)));
  return true;
});
```

阅读要点：

- 第一个参数 `input.scalar_type()` 是**运行时输入**，决定走哪个 `case`。
- 第二个参数 `c_type` 是**占位名**：宏内部会 `using c_type = <某类型>;`，于是 lambda 体内的 `static_cast<c_type*>(...)` 就拿到了具体的指针类型（如 `nv_bfloat16*`）。
- 第三个参数 `[&]{ ... }` 是你真正想执行的逻辑，捕获方式 `[&]` 按引用拿到外层的 `input`/`batch_size`/`stream` 等。
- lambda **必须 `return true;`**，因为宏定义的 lambda 签名是 `-> bool`（见下文）。

再看宏本身的定义（[utils.h:306-321](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L306-L321)）：

```cpp
#define DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16(pytorch_dtype, c_type, ...)           \
  [&]() -> bool {                                                                        \
    switch (pytorch_dtype) {                                                             \
      case at::ScalarType::Float: {                                                      \
        using c_type = float;                                                            \
        return __VA_ARGS__();                                                            \
      }                                                                                  \
        _DISPATCH_CASE_F16(c_type, __VA_ARGS__)                                          \
        _DISPATCH_CASE_BF16(c_type, __VA_ARGS__)                                         \
      default:                                                                           \
        std::ostringstream oss;                                                          \
        oss << __PRETTY_FUNCTION__ << " failed to dispatch data type " << pytorch_dtype; \
        TORCH_CHECK(false, oss.str());                                                   \
        return false;                                                                    \
    }                                                                                    \
  }()
```

`_DISPATCH_CASE_F16` / `_DISPATCH_CASE_BF16` 是构件积木，定义在 ROCm 条件块之外（[utils.h:44-61](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L44-L61)），每个把一种 PyTorch dtype 映射到对应的 NVIDIA 类型（`Half → nv_half`，`BFloat16 → nv_bfloat16`）：

```cpp
#ifdef FLASHINFER_ENABLE_F16
#define _DISPATCH_CASE_F16(c_type, ...) \
  case at::ScalarType::Half: {          \
    using c_type = nv_half;             \
    return __VA_ARGS__();               \
  }
#endif

#ifdef FLASHINFER_ENABLE_BF16
#define _DISPATCH_CASE_BF16(c_type, ...) \
  case at::ScalarType::BFloat16: {       \
    using c_type = nv_bfloat16;          \
    return __VA_ARGS__();                \
  }
#endif
```

> 注意两处位置：构件积木 `_DISPATCH_CASE_*` 在 `#ifndef USE_ROCM`（[utils.h:41-217](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L41-L217)）块**内**；而拼装好的 `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16` 定义在该块**外**（[utils.h:306](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L306)）。这是因为 ROCm 后端有自己的等价积木（用 `__half`/`__hip_bfloat16`），通过同样的宏名复用拼装逻辑——这是 u3-l2 多后端架构的一个伏笔。

#### 4.2.4 代码实践

**实践目标**：把「运行时 dtype → 编译期类型」的映射关系，手画成一张映射表。

**操作步骤**（源码阅读型实践）：

1. 打开 [utils.h:306-321](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L306-L321)，把 `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16` 的三个 `case` 抄下来。
2. 对照 [utils.h:44-61](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h#L44-L61)，补全 `_DISPATCH_CASE_F16` / `_DISPATCH_CASE_BF16` 的展开。
3. 完成下表：

| PyTorch dtype（运行时枚举） | `case` 来源 | 绑定的 C++ 类型（`c_type`） |
| --- | --- | --- |
| `at::ScalarType::Float` | 宏内直接写 | `float` |
| `at::ScalarType::Half` | `_DISPATCH_CASE_F16` | `nv_half` |
| `at::ScalarType::BFloat16` | `_DISPATCH_CASE_BF16` | `nv_bfloat16` |

4. 思考：为什么这三个 `case` 调用的是**同一个** lambda 体（`norm::FusedAddRMSNorm` 模板），却能产生**多份不同的机器码**？

**预期结果**：你能解释清楚——lambda 体内的 `static_cast<c_type*>(...)` 和 `norm::FusedAddRMSNorm<c_type*>` 会随 `c_type` 不同，被编译器实例化成多份模板特化，从而各自走最优的向量化加载宽度。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 `double` 类型的张量传给 `fused_add_rmsnorm`，会发生什么？

> **答案**：`switch` 不命中 `Float`/`Half`/`BFloat16`，落入 `default` 分支，`TORCH_CHECK(false, oss.str())` 抛出错误，信息里带 `__PRETTY_FUNCTION__`（函数签名）和「failed to dispatch data type Double」。用户在 Python 侧看到一条明确报错。

**练习 2**：`DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16` 里的 lambda 为什么必须 `return true;`？

> **答案**：宏定义把整个 lambda 的签名写成了 `[&]() -> bool { ... }()`，要求返回 `bool`。`return true;` 表示「成功执行了该 dtype 分支」。如果忘了 return，编译会因返回值类型不匹配而报错——这是一个防止误用的「契约」。

---

### 4.3 stream 获取与底层调用

#### 4.3.1 概念说明

校验和分派都搞定后，最后一段是**真正发起 GPU 计算**。这一步要解决两个问题：

1. **在哪个队列上跑？** CUDA 用「stream」（流/队列）串起 GPU 任务。同一 stream 上的 kernel 严格按提交顺序执行；不同 stream 间可以并行。PyTorch 为每个线程维护一个「当前 stream」，sgl-kernel 必须取这个 stream 并传给底层，才能保证本算子和同一请求里其它算子按正确顺序排在一起。
2. **怎么报告 GPU 错误？** 底层 FlashInfer 函数 `norm::FusedAddRMSNorm` 返回一个 `cudaError_t` 状态码。如果直接忽略，错误可能被后续某个无关 kernel「撞见」，极难定位。所以要用 `TORCH_CHECK` 把它翻译成 Python 异常。

#### 4.3.2 核心流程

```
┌─ 取 stream ─────────────────────────────────────────────┐
│  cudaStream_t stream = at::cuda::getCurrentCUDAStream();           │
│  // 取 PyTorch 当前线程的默认 CUDA stream                          │
└──────────────────────────────────────────────────────────┘
┌─ 在 DISPATCH lambda 内调用底层模板 ──────────────────────┐
│  cudaError_t status = norm::FusedAddRMSNorm(                       │
│      <c_type*>input.data_ptr(),                                   │
│      <c_type*>residual.data_ptr(),                                │
│      <c_type*>weight.data_ptr(),                                  │
│      batch_size, hidden_size,                                     │
│      input.stride(0), residual.stride(0),   // 行步长             │
│      eps, enable_pdl,                     // 数值 + Hopper 特性    │
│      stream);                              // 队列                 │
└──────────────────────────────────────────────────────────┘
┌─ 错误码翻译 ─────────────────────────────────────────────┐
│  TORCH_CHECK(status == cudaSuccess, "FusedAddRMSNorm failed ..."); │
│  return true;   // 满足 DISPATCH lambda 的 bool 返回契约           │
└──────────────────────────────────────────────────────────┘
```

关于 `enable_pdl`：PDL 全称 **Programmatic Dependent Launch**，是 NVIDIA Hopper（compute capability ≥ 9.0）引入的特性，允许「消费方 kernel」在「生产方 kernel」快结束时就开始启动，从而重叠相邻 kernel 的启动开销。它默认在 Hopper 上自动开启——这个判断在 Python 侧由 `is_arch_support_pdl()` 完成（见 [python/sgl_kernel/utils.py:58-66](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/utils.py#L58-L66)），把布尔值传进 C++ 再透传到底层模板。完整论述留到 u4-l1。

关于 `stride(0)`：对一个 2D 行主序连续张量，`stride(0)`（行步长）等于 `hidden_size`。这里虽然 `CHECK_INPUT` 已保证连续，但**显式传入行步长**能让底层模板对「整张连续、但行步长非默认」的情况依然正确，是一种稳健写法。

#### 4.3.3 源码精读

取 stream 只有一行（[fused_add_rms_norm_kernel.cu:41-58](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L41-L58)）：

```cpp
cudaStream_t torch_current_stream = at::cuda::getCurrentCUDAStream();
// support float16, bfloat16 and float32
DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16(input.scalar_type(), c_type, [&] {
  cudaError_t status = norm::FusedAddRMSNorm(
      static_cast<c_type*>(input.data_ptr()),
      static_cast<c_type*>(residual.data_ptr()),
      static_cast<c_type*>(weight.data_ptr()),
      batch_size,
      hidden_size,
      input.stride(0),
      residual.stride(0),
      eps,
      enable_pdl,
      torch_current_stream);
  TORCH_CHECK(
      status == cudaSuccess,
      "FusedAddRMSNorm failed with error code " + std::string(cudaGetErrorString(status)));
  return true;
});
```

`at::cuda::getCurrentCUDAStream()` 来自 PyTorch 的 CUDA 上下文头文件（文件顶部 `#include <ATen/cuda/CUDAContext.h>`）。它返回的是 `c10::cuda::CUDAStream`，隐式转换成裸的 `cudaStream_t`（CUDA Runtime 的句柄类型），这正是 FlashInfer 模板所期望的。

`norm::FusedAddRMSNorm` 来自 vendored 的 FlashInfer 头文件（文件顶部 `#include <flashinfer/norm.cuh>` 与 `using namespace flashinfer;`，[fused_add_rms_norm_kernel.cu:16-22](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L16-L22)）。也就是说：sgl-kernel 这一层 C++ 函数 **本身不含 kernel 实现**，它只负责「校验 + 分派 + 取 stream」，然后把活儿转交给 FlashInfer 的模板。这与 u1-l2 讲的「vendored FlashInfer 模板」完全吻合。

最后看一眼 C++ 函数的原型声明（[sgl_kernel_ops.h:138-139](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h#L138-L139)）：

```cpp
void sgl_fused_add_rmsnorm(
    torch::Tensor input, torch::Tensor residual, torch::Tensor weight, double eps, bool enable_pdl);
```

注意函数返回 `void`——它不返回结果张量，因为输出是**原地写回** `input`/`residual`（可变语义）。这正好对应注册侧 schema 里的 `Tensor! input, Tensor! residual ... -> ()`（[common_extension.cc:67-68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L67-L68)）：

```cpp
m.def("fused_add_rmsnorm(Tensor! input, Tensor! residual, Tensor weight, float eps, bool enable_pdl) -> ()");
m.impl("fused_add_rmsnorm", torch::kCUDA, &sgl_fused_add_rmsnorm);
```

至此，u2-l2 停下的「门口」已经被完全打通：schema → `m.impl` 绑定 → `sgl_fused_add_rmsnorm` 函数体（校验 + 分派 + 取 stream + 调用底层）。

#### 4.3.4 代码实践

**实践目标**：把「Python 调用 → C++ 校验/分派/stream → 底层模板」的完整调用链画成时序图。

**操作步骤**（源码阅读型实践）：

1. 从 [elementwise.py:155-162](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L155-L162) 的 Python 包装出发，追到 [`_fused_add_rmsnorm_internal`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L31-L42)：

```python
def _fused_add_rmsnorm_internal(input, residual, weight, eps, enable_pdl):
    if enable_pdl is None:
        enable_pdl = is_arch_support_pdl()        # Python 侧判定 Hopper→True
    torch.ops.sgl_kernel.fused_add_rmsnorm.default(
        input, residual, weight, eps, enable_pdl  # 进入 C++ 的门口
    )
```

2. 顺着 `torch.ops.sgl_kernel.fused_add_rmsnorm.default` 找到注册（[common_extension.cc:67-68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L67-L68)）→ 绑定到 `sgl_fused_add_rmsnorm`。
3. 进入函数体（[fused_add_rms_norm_kernel.cu:24-59](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L24-L59)），把 4.1/4.2/4.3 三段画成时序：

```
Python:  fused_add_rmsnorm(x, r, w, eps)
   │  (enable_pdl 默认由 is_arch_support_pdl() 决定)
   ▼
torch.ops.sgl_kernel.fused_add_rmsnorm.default(...)
   │  m.impl(Tensor!,Tensor!,Tensor,float,bool)->()
   ▼
C++: sgl_fused_add_rmsnorm(input, residual, weight, eps, enable_pdl)
   ├─ CHECK_INPUT × 3         （4.1 校验）
   ├─ CHECK_EQ device × 2
   ├─ CHECK_DIM × 3
   ├─ CHECK_EQ shape × 3
   ├─ getCurrentCUDAStream()  （4.3 取 stream）
   └─ DISPATCH_FLOAT_FP16(    （4.2 dtype 分派）
        norm::FusedAddRMSNorm<c_type*>(..., stream)   → 底层 FlashInfer 模板
        TORCH_CHECK(status==cudaSuccess)
      )
```

4. **需要观察的现象**：你能指出 `enable_pdl` 在哪一层被决定（Python），又在哪一层被消费（底层模板），中间 C++ 函数体只做透传。

**预期结果**：画出上述时序图，并能解释每一层「加了什么价值」（Python：默认值兜底 + FlashInfer 回退；注册层：schema 契约；C++ 函数体：校验 + 分派 + stream；底层模板：真正的 GPU 向量化计算）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sgl_fused_add_rmsnorm` 返回 `void`，而 Python 用户却能看到归一化后的结果？

> **答案**：因为结果是通过可变参数 `Tensor! input`（以及 `Tensor! residual`）**原地写回**的。Python 侧传入的 `input`/`residual` 张量的显存被直接覆盖，函数不需要、也没有返回值。这正是 `-> ()` + `Tensor!` 模式（u2-l2）的具体体现。

**练习 2**：如果把 `TORCH_CHECK(status == cudaSuccess, ...)` 删掉，最坏会发生什么？

> **答案**：底层 kernel 若失败（返回非 `cudaSuccess`），错误码会被静默丢弃。由于 CUDA 错误是「粘性」的，这个错误可能被几百毫秒后某个**完全无关**的 kernel 撞到并抛出，导致真正的出错点极难定位。所以这道 `TORCH_CHECK` 是把错误**就地、及时**暴露出来的关键防线。

**练习 3**：`input.stride(0)` 在什么情况下不等于 `hidden_size`？这里为什么仍能正确工作？

> **答案**：对一个 2D 行主序连续张量，`stride(0) == hidden_size`。若张量是从更大的张量「切」出来的（如某些转置视图），`stride(0)` 可能更大——但此时 `CHECK_INPUT` 的 `is_contiguous()` 检查会先把它拦下。显式传 `stride(0)` 是给底层模板的稳健约定，让它按实际行步长寻址。

---

## 5. 综合实践

**任务**：以 `fused_add_rmsnorm` 为样本，给「一个 sgl-kernel CUDA 算子函数体」写一份**三段式结构说明书**，并把它的数值行为和纯 PyTorch 参考实现对齐。

**操作步骤**：

1. **结构说明书**（源码阅读 + 归纳）：打开 [fused_add_rms_norm_kernel.cu:24-59](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L24-L59)，用一张表归纳三段式：

   | 段 | 代码行 | 做了什么 | 用到的宏/函数 |
   | --- | --- | --- | --- |
   | 校验 | L26-L37 | 设备/连续/维度/形状 | `CHECK_INPUT`/`CHECK_DIM`/`CHECK_EQ` |
   | 分派 | L43 | 运行时 dtype→编译期类型 | `DISPATCH_..._FLOAT_FP16` |
   | 调用 | L41, L44-L58 | 取 stream + 调底层模板 + 错误翻译 | `getCurrentCUDAStream`/`norm::FusedAddRMSNorm`/`TORCH_CHECK` |

2. **数值对齐**（需要 GPU；若无则写「待本地验证」）：用纯 PyTorch 写 `fused_add_rmsnorm` 的参考实现，再和 sgl-kernel 结果比对：

```python
import torch
from sgl_kernel import fused_add_rmsnorm  # 或对应导入路径

def ref_fused_add_rmsnorm(input, residual, weight, eps):
    new_residual = residual + input                                  # Step 1
    rms = torch.sqrt((new_residual.float() ** 2).mean(dim=-1, keepdim=True) + eps)
    out = (new_residual.float() / rms).to(input.dtype) * weight      # Step 2
    return out, new_residual

torch.manual_seed(0)
H = 64
x = torch.randn(4, H, dtype=torch.bfloat16, device="cuda")
r = torch.randn(4, H, dtype=torch.bfloat16, device="cuda")
w = torch.randn(H, dtype=torch.bfloat16, device="cuda")

x_ref = x.clone()
r_ref = r.clone()
out_ref, r_ref_new = ref_fused_add_rmsnorm(x_ref, r_ref, w, eps=1e-6)

x_k = x.clone()
r_k = r.clone()
fused_add_rmsnorm(x_k, r_k, w, eps=1e-6)   # 原地：x_k 变为归一化结果，r_k 变为新残差

print("residual max abs diff:", (r_k.float() - r_ref_new.float()).abs().max().item())
print("output   max abs diff:", (x_k.float() - out_ref.float()).abs().max().item())
```

3. **需要观察的现象**：
   - sgl-kernel 的 `x_k` 与参考 `out_ref`、`r_k` 与参考 `r_ref_new` 在 `rtol=1e-3` 量级内一致（bfloat16 精度有限，差异主要来自累加顺序）。
   - 调用后**原张量被原地修改**，印证 `Tensor!` 可变语义。
4. **思考**：把 `dtype` 换成 `torch.float32` 再跑一次，误差应显著变小；换成 `torch.int32` 则应触发 4.2 的 `default` 分支报错——验证 dtype 分派的边界。

**预期结果**：交付一份结构说明书 + 一段能跑（或标注「待本地验证」）的对齐脚本，并解释三段式各自防住了哪类错误。

## 6. 本讲小结

- sgl-kernel 的 CUDA 算子函数体遵循 **「校验 → 分派 → 调用」三段式**，`fused_add_rms_norm_kernel.cu` 是这一模式的范本。
- **校验段**靠 [`utils.h`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/utils.h) 里的 `CHECK_INPUT`/`CHECK_DIM`/`CHECK_EQ` 等宏，本质是 `TORCH_CHECK` + 预处理器字符串化 `#x`，把错误前置、明确化。
- **分派段**靠 `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16` 等宏，用运行时 `switch(scalar_type())` + 每个 `case` 里 `using c_type = <类型>;` 的手法，桥接「运行时 dtype 枚举」与「编译期 C++ 类型」，从而复用同一份模板逻辑、实例化出 float/half/bfloat16 多份机器码。
- **调用段**用 `at::cuda::getCurrentCUDAStream()` 取队列、透传 `enable_pdl`（Hopper 特性）与行步长，转交给 vendored 的 FlashInfer 模板 `norm::FusedAddRMSNorm`，再用 `TORCH_CHECK(status==cudaSuccess, ...)` 把 CUDA 错误码翻译成 Python 异常。
- sgl-kernel 这一层 C++ 函数**本身不含 kernel 实现**，只做包装；真正的向量化 GPU 计算在 FlashInfer 模板里。
- 函数返回 `void` + 可变 `Tensor!`，对应注册 schema 的 `-> ()`，与 u2-l2 的可变语义闭环。

## 7. 下一步学习建议

本讲打通了「单算子从 Python 到 C++ 函数体」的完整链路。接下来可以：

- **横向**：用同样的三段式框架去读其它算子的函数体，例如 [`csrc/elementwise/activation.cu`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/activation.cu)（`silu_and_mul`，u4-l2 会讲它的 16 字节对齐约束）或 [`csrc/gemm/fp8_gemm_kernel.cu`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/gemm/fp8_gemm_kernel.cu)，练习「看见任意 `.cu` 就能定位校验/分派/调用三段」。
- **纵向（U3）**：本讲提到 `_DISPATCH_CASE_*` 积木在 `#ifndef USE_ROCM` 块内、而拼装宏在块外——这是多后端架构的伏笔。u3-l1（多扩展拆分）和 u3-l2（CUDA/ROCm/MUSA/CPU/Metal 多后端）会展开讲 sgl-kernel 如何用条件编译隔离不同 GPU 后端。
- **深入 RMSNorm**：想完整理解 `enable_pdl`、FlashInfer 回退策略与 `gemma` 变体的 `(1+w)` 差异，请继续阅读 u4-l1（RMSNorm 家族与 FlashInfer 回退）。
- **贡献流程**：当你想自己加一个新算子时，本讲的三段式就是你要往 `.cu` 文件里填的「函数体模板」；u11-l3 会把「写 CUDA → 头文件 → `m.def`/`m.impl` → Python 包装 → `__init__` → CMake → 测试」的完整六步串起来。
