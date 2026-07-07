# Arch 检测与 DISPATCH 宏

## 1. 本讲目标

本讲聚焦 `csrc/api/common.h` 这个被所有接口函数共享的头文件，拆解其中三类「胶水」机制：

1. **`Arch` 结构**：在运行时检测当前 GPU 的架构（SM90a 还是 SM100f）以及 SM 数量，让接口函数能据此选择正确的 kernel 家族。
2. **`int64_stride_to_int`**：把 PyTorch 给出的 `int64_t` 步长安全地收窄成 `int32_t`，防止超长序列下的整数溢出。
3. **`DISPATCH_*` 宏**：把「运行时才知道的值」（如 `head_dim`、`num_heads`、`model_type`）转换成「编译期常量」，从而为每个取值生成一份独立的、高度优化的模板 kernel。

学完后你应当能够：

- 说清楚 `Arch` 是怎么拿到 `major/minor/num_sms` 的，以及 `is_sm90a()/is_sm100f()` 的判定逻辑。
- 看懂 `int64_stride_to_int` 的溢出检查，并估算什么规模的张量会触发它。
- 读懂 `DISPATCH_HEAD_DIM` / `DISPATCH_NUM_HEADS` / `DISPATCH_BOOLEAN_FLAG` / `DISPATCH_MODEL_TYPE` 四个宏，并能解释「立即调用的 lambda（IIFE）」这种写法为什么能为每个枚举值生成独立的模板特化。
- 追踪一个不支持的 `head_dim`（如 256）会在哪一层被拦下、报什么错。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义 u1 / u2-l1 / u2-l2）：

- **四类 kernel 与支持矩阵**：FlashMLA 按「阶段（prefill/decode）× 稀疏性（dense/sparse）」分成四类 kernel，且支持矩阵不对称——dense decode 仅 SM90、dense prefill 仅 SM100、sparse 两架构都有。
- **调用链分层**：Python 包装 → pybind 绑定（`api.cpp`）→ C++ 接口函数（`csrc/api/*.h`）→ kernel 命名空间（`sm90::/sm100::/smxx::`）。本讲处在第三层「接口函数」内部。
- **`params.h` 参数契约**：接口函数把指针、步长、调度元数据打包成 POD 结构透传给 kernel（见 u2-l2）。其中 `SparseAttnDecodeParams.model_type` 是 `ModelType` 枚举（V32 / MODEL1），`d_qk` 取 576 或 512。
- **为什么 CUDA kernel 要模板特化**：`head_dim`、`num_heads` 这些值若能在编译期确定，编译器就能展开循环、固定 shared memory 布局、绑定 TMA 描述符，性能远高于「运行时动态循环」。这正是 `DISPATCH_*` 宏存在的根本原因。

下面用到但需要先解释的两个术语：

- **SM（Streaming Multiprocessor）**：GPU 的计算单元块，一块 H800 有 80 个 SM，一块 B200 有 148 个 SM。`num_sms` 决定了能并行启动多少个 CTA（线程块）。
- **IIFE（Immediately Invoked Function Expression，立即调用的 lambda）**：C++ 里写成 `[&](){ ... }()`，定义一个 lambda 并马上调用它。本讲的 DISPATCH 宏大量使用这种模式来制造一个「带局部 `constexpr` 作用域」的表达式。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| [csrc/api/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h) | **本讲核心**。所有接口共享的 `Arch`、`int64_stride_to_int`、`DISPATCH_*` 宏、`ImplBase` 派发基类 | 全文 |
| [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) | sparse prefill 接口；含 `DISPATCH_HEAD_DIM` + `DISPATCH_BOOLEAN_FLAG` 调用点 | 第 42–46、112–115、131 行 |
| [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) | sparse decode 接口；含 `DISPATCH_MODEL_TYPE` + `DISPATCH_NUM_HEADS` 调用点 | 第 70–75、201、363–381 行 |
| [csrc/api/dense_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h) | dense decode 接口；含最朴素的「`Arch` 检查 + 直接调用」派发 | 第 26–29 行 |
| [csrc/params.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h) | `ModelType` 枚举与各 Params 结构定义 | 第 5–8 行 |

## 4. 核心概念与源码讲解

### 4.1 Arch 结构：运行时 GPU 架构检测

#### 4.1.1 概念说明

`Arch` 解决的问题是：**接口函数在运行时才知道自己跑在哪块 GPU 上**。Python 用户可能在一台 H800 上调用，也可能在一台 B200 上调用；而 dense decode kernel 只在 SM90a 上编译、dense prefill kernel 只在 SM100f 上编译。接口函数必须先问清楚「我现在在哪」，才能：

1. 决定走哪条实现路径（`sm90::` 还是 `sm100::`）。
2. 拿到这块卡的 SM 数量 `num_sms`，用来计算要把序列切成多少份（`num_sm_parts`），这是 Split-KV 调度的关键输入。

`Arch` 把这两件事打包成一个结构体：构造时一次性查好 `major / minor / num_sms`，并提供两个判定函数 `is_sm90a()` / `is_sm100f()`。

#### 4.1.2 核心流程

`Arch` 的使用流程非常直白：

1. 在接口函数开头构造一个 `Arch arch = Arch();`，构造函数内部调用 PyTorch 的 `at::cuda::getCurrentDeviceProperties()` 拿到当前设备的 `cudaDeviceProp`。
2. 从 `device_prop` 里取出 `major`、`minor`、`multiProcessorCount`，缓存进成员变量。
3. 后续用 `arch.is_sm90a()` / `arch.is_sm100f()` 做架构分支，用 `arch.num_sms` 做切分计算。

伪代码：

```text
Arch arch = Arch();              // 查询当前 GPU
if (arch.is_sm100f()) {          // Blackwell
    选择 SM100 实现;
} else if (arch.is_sm90a()) {    // Hopper
    选择 SM90 实现;
} else {
    报错：不支持的架构;
}
num_sm_parts = max(arch.num_sms / ..., 1);
```

#### 4.1.3 源码精读

`Arch` 结构定义在 [csrc/api/common.h:20-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L20-L41)：

```cpp
struct Arch {
    int major;
    int minor;
    int num_sms;
    cudaDeviceProp* device_prop;

    Arch() {
        device_prop = at::cuda::getCurrentDeviceProperties();
        major = device_prop->major;
        minor = device_prop->minor;
        num_sms = device_prop->multiProcessorCount;
    }

    bool is_sm90a() const { return major == 9 && minor == 0; }
    bool is_sm100f() const { return major == 10; }
};
```

要点：

- `at::cuda::getCurrentDeviceProperties()` 是 PyTorch 提供的封装，返回当前默认 CUDA 设备的属性指针，省去了手写 `cudaGetDeviceProperties`。
- `is_sm90a()` 判定 `major==9 && minor==0`，对应 Hopper（H800）。后缀 `a`（见 u1-l2 的 `-gencode arch=compute_90a`）表示启用了 WGMMA / TMA 等架构专有指令；这里只用 major/minor 数字判定，指令集是否启用是在 **编译期** 由 `setup.py` 的 gencode 决定的。
- `is_sm100f()` 只看 `major==10`，对应 Blackwell（B200），不限定 minor。

架构判定在接口里的真实用法。dense decode 最朴素——只允许 SM90a，直接 `TORCH_CHECK`（[csrc/api/dense_decode.h:26-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L26-L29)）：

```cpp
Arch arch = Arch();
if (!arch.is_sm90a()) {
    TORCH_CHECK(false, "Dense decode MLA is only supported on SM90a architecture");
}
```

sparse prefill 则两架构都支持，但必须二选一（[csrc/api/sparse_fwd.h:112-115](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L112-L115)）：

```cpp
Arch arch = Arch();
bool is_sm90a = arch.is_sm90a();
bool is_sm100f = arch.is_sm100f();
TORCH_CHECK(is_sm90a || is_sm100f, "Sparse Attention Forward Kernel is only supported on SM90a and SM100f architectures.");
```

`num_sms` 的用法——sparse decode 的 `Decode_Sm90_Impl::get_meta` 用它算 `num_sm_parts`（[csrc/api/sparse_decode.h:59-66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L59-L66)）：

```cpp
DecodeImplMeta get_meta(int h_q, int s_q) override {
    Arch arch = Arch();
    return { std::max(arch.num_sms / s_q / (h_q/64), 1), 5, 64 };
}
```

> 这里 `/ s_q / (h_q/64)` 的含义：把 SM 按「每个请求的 query 行数」和「每个 KV head 对应的 64 头组」两次均分，得到能给单次 kernel 启动用的 SM part 数；至少为 1。这部分细节会在 Unit 4（tile scheduler）展开，本讲只需理解 `num_sms` 是这个切分的输入。

#### 4.1.4 代码实践

**实践目标**：验证你对 `major/minor → is_sm90a/is_sm100f` 映射的理解。

**操作步骤**：

1. 阅读 [csrc/api/common.h:34-40](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L34-L40) 的两个判定函数。
2. 若有 GPU 环境，运行下面这段 Python（无 GPU 则纯做纸面推演）：

   ```python
   # 示例代码（非项目原有）
   import torch
   if torch.cuda.is_available():
       major, minor = torch.cuda.get_device_capability()
       print("compute capability:", (major, minor))
       print("is_sm90a:", major == 9 and minor == 0)
       print("is_sm100f:", major == 10)
       print("num_sms:", torch.cuda.get_device_properties(0).multi_processor_count)
   ```

3. 对照 [README](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md) 的支持矩阵，判断这台机器能跑哪几条 kernel 路径。

**需要观察的现象 / 预期结果**：

- H800：`compute capability = (9, 0)` → `is_sm90a=True`、`is_sm100f=False` → 可跑 dense decode、sparse decode/prefill，**不可**跑 dense prefill（仅 SM100）。
- B200：`compute capability = (10, ...)` → `is_sm100f=True` → 可跑 dense prefill、sparse decode/prefill，**不可**跑 dense decode（仅 SM90）。

> 待本地验证：具体 `multi_processor_count` 数值（H800 为 80，B200 为 148）取决于实际硬件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_sm100f()` 不需要检查 `minor`，而 `is_sm90a()` 需要？

**答案**：Blackwell 系列（SM10x）的多个 minor 都属于 FlashMLA 目标的 `sm_100f` 编译目标，所以只看 `major==10` 即可；而 SM9 家族里只有 `9.0`（Hopper）是 FlashMLA 支持的对象（`sm_90a`），其它 SM9.x 不支持，因此必须同时卡 `minor==0`。

**练习 2**：如果在一块 SM8.9（Ada）的卡上调用 dense decode 接口，会在哪一行报什么错？

**答案**：在 [csrc/api/dense_decode.h:27-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L27-L29)，`is_sm90a()` 为 false，命中 `TORCH_CHECK(false, "Dense decode MLA is only supported on SM90a architecture")`，向 Python 抛出该消息的异常。

---

### 4.2 int64 → int32 stride 溢出保护

#### 4.2.1 概念说明

PyTorch 张量的 `.stride()` 返回 `int64_t`，因为现代大模型（尤其长上下文）的张量元素总数可能超过 32 位整数的表示范围。但 CUDA kernel 内部为了节省寄存器、加快地址运算，习惯用 `int32_t` 步长。`int64_stride_to_int` 就是这两者之间的「安全闸门」：在接口层把 `int64_t` 收窄成 `int` 时，先检查是否溢出，溢出就直接报错，而不是让一个被截断的错误步长悄悄进入 kernel。

#### 4.2.2 核心流程

`int32` 的最大值是

\[
2^{31}-1 = 2{,}147{,}483{,}647 \approx 2.15\times 10^{9}
\]

任何一个元素级步长（单位是「元素个数」，不是字节）超过这个数就会溢出。流程：

1. 接口函数从张量取 `tensor.stride(i)`（`int64_t`）。
2. 调用 `int64_stride_to_int(...)`。
3. 若 `orig_stride > INT_MAX`，`TORCH_CHECK(false, ...)` 抛错；否则 `static_cast<int>` 返回。

伪代码：

```text
inline int int64_stride_to_int(int64_t s):
    if s > INT_MAX: TORCH_CHECK(false, "Stride exceeds int32 limit: ", s)
    return (int)s
```

什么样的张量会触发？以 dense prefill 的 KV 为例：元素总数 = `s_kv × num_kv_heads × head_dim`。取 `s_kv = 65536`、`num_kv_heads = 128`、`head_dim = 128`，得

\[
65536 \times 128 \times 128 = 1{,}073{,}741{,}824 \approx 1.07\times 10^{9}
\]

已接近一半上限；再叠加 batch 或更长上下文，最外层步长很容易越过 \(2^{31}-1\)。所以这个检查并非摆设。

> 注意：`DenseAttnDecodeParams`（[csrc/params.h:19-61](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L19-L61)）内部仍把 stride 存成 `index_t = int64_t`，**不**经过收窄；而 `SparseAttnDecodeParams`（[csrc/params.h:63-103](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L63-L103)）的 stride 字段是 `int`，必须经过 `int64_stride_to_int`。这是因为两套 kernel 对步长宽度的假设不同。

#### 4.2.3 源码精读

定义在 [csrc/api/common.h:43-49](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L43-L49)：

```cpp
// Convert int64_t stride to int32_t, with overflow check.
inline int int64_stride_to_int(int64_t orig_stride) {
    if (orig_stride > std::numeric_limits<int>::max()) {
        TORCH_CHECK(false, "[FlashMLA] Stride exceeds int32 limit: ", orig_stride);
    }
    return static_cast<int>(orig_stride);
}
```

要点：

- `std::numeric_limits<int>::max()` 即 `INT_MAX`，需要 `<limits>` 头（经 `torch/extension.h` 间接引入）。
- 只检查「正向溢出」。步长在 FlashMLA 里不会是负数（最后一维强制 contiguous、其余维顺序排布），所以无需处理负值。
- `TORCH_CHECK(false, ...)` 会把第二个及之后的参数拼进错误消息（类似 `fmt::format`），最终在 Python 端变成一条 `RuntimeError`。

真实调用点——sparse decode 装配 `SparseAttnDecodeParams` 时，所有步长都经过它（[csrc/api/sparse_decode.h:404-413](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L404-L413)）：

```cpp
int64_stride_to_int(q.stride(0)), int64_stride_to_int(q.stride(1)), int64_stride_to_int(q.stride(2)),
int64_stride_to_int(kv.stride(0)), int64_stride_to_int(kv.stride(1)),
...
have_extra_kcache ? int64_stride_to_int(extra_kv->stride(0)) : 0,
```

注意「可空张量」的写法：当 `extra_kv` 不存在时，三元表达式直接给 `0` 占位，**不**调用 `int64_stride_to_int`（否则会对空 optional 解引用崩溃）。sparse prefill 同样如此（[csrc/api/sparse_fwd.h:179-181](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L179-L181)）。

#### 4.2.4 代码实践

**实践目标**：手算一个会触发溢出的张量规模，并预测错误消息。

**操作步骤**：

1. 阅读 [csrc/api/common.h:43-49](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L43-L49)。
2. 构造一个元素总数超过 \(2^{31}-1\) 的假设张量，例如形状 `[8, 4096, 256, 512]`（元素总数 = \(8 \times 4096 \times 256 \times 512 = 4.29 \times 10^{9}\)），其 `stride(0) = 4096 \times 256 \times 512 = 5.37 \times 10^{8}`（不溢出），但 `stride(0)` 若再放大一档即会越过上限。请自行设计一个 `stride(0)` 越过 \(2^{31}-1\) 的形状。
3. 追踪这个张量进入 `sparse_attn_decode_interface` 后，会在哪一行触发 `TORCH_CHECK`。

**需要观察的现象 / 预期结果**：

- 一旦某条 `tensor.stride(i)` 超过 `INT_MAX`，对应那一行 `int64_stride_to_int(...)` 会抛出 `[FlashMLA] Stride exceeds int32 limit: <实际值>`，Python 端收到 `RuntimeError`。
- 该检查发生在 kernel 启动**之前**，所以不会产生半截计算结果。

> 待本地验证：在真实大 shape 上的具体错误输出文本。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `int64_stride_to_int` 不需要检查负数？

**答案**：FlashMLA 接口在调用它之前，已经用 `KU_CHECK_LAST_DIM_CONTIGUOUS` / `KU_CHECK_CONTIGUOUS` 等宏约束了张量布局，使各维步长均为非负；且张量维度本身由 `size()` 决定，不会出现负步长。

**练习 2**：如果把 `DenseAttnDecodeParams` 的 stride 也改成 `int` 并套上 `int64_stride_to_int`，dense decode 在什么场景下会比现在更早报错？

**答案**：当 KV cache 的最外层元素步长超过 \(2^{31}-1\)（即元素总数 > ~21 亿）时。目前 dense decode 走 `int64_t` 步长能承载更大的张量；改成 `int` 后，超大上下文 + 多 batch 的场景会被这道闸门拦下。

---

### 4.3 DISPATCH 宏：把运行时值编译期化

#### 4.3.1 概念说明

这是本讲最核心、也最精巧的部分。问题陈述：

> CUDA kernel 的 `head_dim` / `num_heads` / `model_type` 等参数，**运行时**才知道（它们来自用户传入的张量形状）；但 kernel 想要高性能，就必须把这些值变成**编译期模板参数**，让编译器为每个取值生成一份专门的代码（循环展开、shared memory 静态布局、TMA 描述符固定形状）。

`DISPATCH_*` 宏就是连接这两个世界的桥梁：用一个**运行时的 `if` 分支**，在每个分支里定义一个**编译期常量**（`static constexpr`），再调用用户传入的 lambda（lambda 里把这些常量当模板参数用）。这样：

- 运行时只走命中的一条分支；
- 编译期则会实例化**所有**分支里的模板，因此每个取值都有一份特化 kernel 被编进 `.so`。

FlashMLA 提供四个这样的宏：

| 宏 | 输入运行时值 | 编译期常量类型 | 支持的取值 | 出错消息 |
|----|------------|--------------|-----------|---------|
| `DISPATCH_NUM_HEADS` | `num_heads` | `int` | 128 / 64 | `Unsupported num_heads_q` |
| `DISPATCH_HEAD_DIM` | `head_dim` | `int` | 576 / 512 | `Unsupported head_dim_qk` |
| `DISPATCH_BOOLEAN_FLAG` | 任意 bool 表达式 | `bool` | true / false | （无 else 报错） |
| `DISPATCH_MODEL_TYPE` | `ModelType` 枚举 | `ModelType` | V32 / MODEL1 | `Unsupported model type` |

#### 4.3.2 核心流程

四个宏的结构完全一致，都是「立即调用的 lambda（IIFE）」。以 `DISPATCH_HEAD_DIM` 为例，其骨架是：

```text
[&] () {
    if (HEAD_DIM == 576) {
        static constexpr int CONSTEXPR_NAME = 576;
        return __VA_ARGS__();        // 用户 lambda 被立即调用
    } else if (HEAD_DIM == 512) {
        static constexpr int CONSTEXPR_NAME = 512;
        return __VA_ARGS__();
    } else {
        TORCH_CHECK(false, "Unsupported head_dim_qk: ", HEAD_DIM);
    }
} ();
```

关键机制：

1. **外层 IIFE**：`[&](){ ... }()` 让整段宏成为一个「表达式」，可以出现在任意语句位置，并把内部 `return` 的值作为整个宏的值。
2. **每个分支一个独立的 `constexpr` 作用域**：`static constexpr int CONSTEXPR_NAME = 576` 让 `CONSTEXPR_NAME` 在该分支内是一个真正的编译期常量，可以作为模板实参。
3. **`__VA_ARGS__()`**：用户传进来的 `[&](){ run_kernel<HEAD_DIM_QK>(params); }` 本身也是一个 lambda，宏在分支里用 `()` 立即调用它。调用时，lambda 体内引用的 `HEAD_DIM_QK` 解析到**当前分支**的那个 `constexpr`，于是模板实参就被绑定到了一个编译期值。
4. **编译期实例化所有分支**：尽管运行时只走一条分支，但 C++ 编译器会编译每一条分支里的代码，所以 `run_kernel<576>` 和 `run_kernel<512>` **两份特化都会被生成**并链接进产物。这正是「为每个枚举值生成独立模板特化」的实现原理。

`DISPATCH_MODEL_TYPE` 略有不同：它比较的是枚举值 `ModelType::V32` / `ModelType::MODEL1`（见 [csrc/params.h:5-8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L5-L8)），常量类型是 `ModelType` 而非 `int`。`DISPATCH_BOOLEAN_FLAG` 最简单——只有 true/false 两个分支，因此**没有** `else` 报错分支。

#### 4.3.3 源码精读

四个宏定义在 [csrc/api/common.h:51-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L51-L99)。先看 `DISPATCH_HEAD_DIM`（[common.h:64-75](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L64-L75)）：

```cpp
#define DISPATCH_HEAD_DIM(HEAD_DIM, CONSTEXPR_NAME, ...) \
[&] () { \
    if (HEAD_DIM == 576) { \
        static constexpr int CONSTEXPR_NAME = 576; \
        return __VA_ARGS__(); \
    } else if (HEAD_DIM == 512) { \
        static constexpr int CONSTEXPR_NAME = 512; \
        return __VA_ARGS__(); \
    } else { \
        TORCH_CHECK(false, "Unsupported head_dim_qk: ", HEAD_DIM); \
    } \
} ();
```

`DISPATCH_BOOLEAN_FLAG`（[common.h:77-86](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L77-L86)）只有两个分支，无 else 报错：

```cpp
#define DISPATCH_BOOLEAN_FLAG(FLAG, CONSTEXPR_NAME, ...) \
[&] () { \
    if (FLAG) { static constexpr bool CONSTEXPR_NAME = true;  return __VA_ARGS__(); } \
    else     { static constexpr bool CONSTEXPR_NAME = false; return __VA_ARGS__(); } \
} ();
```

`DISPATCH_MODEL_TYPE`（[common.h:88-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L88-L99)）比较枚举：

```cpp
#define DISPATCH_MODEL_TYPE(MODEL_TYPE, CONSTEXPR_NAME, ...) \
[&] () { \
    if (MODEL_TYPE == ModelType::V32) {
        static constexpr ModelType CONSTEXPR_NAME = ModelType::V32; return __VA_ARGS__(); \
    } else if (MODEL_TYPE == ModelType::MODEL1) { \
        static constexpr ModelType CONSTEXPR_NAME = ModelType::MODEL1; return __VA_ARGS__(); \
    } else { \
        TORCH_CHECK(false, "Unsupported model type: ", (int)MODEL_TYPE); \
    } \
} ();
```

宏的**嵌套调用**是常见用法——把多个运行时维度逐层编译期化。sparse decode 的 `Decode_Sm90_Impl::run_` 嵌套了 `DISPATCH_MODEL_TYPE` 与 `DISPATCH_NUM_HEADS`（[csrc/api/sparse_decode.h:70-75](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L70-L75)）：

```cpp
void run_(const SparseAttnDecodeParams &params, const std::vector<FeatureT> &required_features) override {
    DISPATCH_MODEL_TYPE(params.model_type, MODEL_TYPE, [&]() {
        DISPATCH_NUM_HEADS(params.h_q, NUM_HEADS, [&]() {
            sm90::decode::sparse_fp8::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE, NUM_HEADS>(params);
        });
    });
}
```

这里 `MODEL_TYPE`（外层 constexpr）和 `NUM_HEADS`（内层 constexpr）共同作为 kernel 的模板参数，于是 SM90 sparse decode 实际会实例化出 \(2 \times 2 = 4\) 份特化 kernel（V32/MODEL1 × 64/128 头）。

sparse prefill 的 `Fwd_Sm90_Impl::run_` 则嵌套 `DISPATCH_HEAD_DIM` 与 `DISPATCH_BOOLEAN_FLAG`（[csrc/api/sparse_fwd.h:42-46](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L42-L46)）：

```cpp
DISPATCH_HEAD_DIM(params.d_qk, HEAD_DIM_QK, [&]() {
    DISPATCH_BOOLEAN_FLAG(params.topk_length != nullptr, HAVE_TOPK_LENGTH, [&]() {
        sm90::fwd::run_fwd_phase1_kernel<HEAD_DIM_QK, HAVE_TOPK_LENGTH>(params);
    });
});
```

`HAVE_TOPK_LENGTH` 是一个 `bool` 模板参数，决定 kernel 是否编译「带 topk_length 掩码」的代码路径。

> 旁注：`common.h` 还有一段「枚举名反射」工具（[common.h:101-141](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L101-L141)），用 `__PRETTY_FUNCTION__` 把枚举值反解成字符串名。它服务于 `ImplBase` 在派发失败时打印可读的 feature 列表，本讲暂不展开，留到 u2-l4。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：追踪一个**未支持的 `head_dim`（如 256）** 进入 `DISPATCH_HEAD_DIM` 后，会在哪一行、报什么错；并解释 IIFE 风格如何为每个枚举值生成独立模板特化。

**操作步骤**：

1. 选定调用点 [csrc/api/sparse_fwd.h:42-46](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L42-L46) 的 `DISPATCH_HEAD_DIM(params.d_qk, HEAD_DIM_QK, ...)`。
2. 假设 `params.d_qk == 256`。手工展开宏，得到：

   ```cpp
   // 示例代码：DISPATCH_HEAD_DIM 宏展开后的等价形式
   [&] () {
       if (params.d_qk == 576) {              // 256 == 576 → false
           static constexpr int HEAD_DIM_QK = 576;
           return [&](){ run_fwd_phase1_kernel<576>(params); }();
       } else if (params.d_qk == 512) {       // 256 == 512 → false
           static constexpr int HEAD_DIM_QK = 512;
           return [&](){ run_fwd_phase1_kernel<512>(params); }();
       } else {
           TORCH_CHECK(false, "Unsupported head_dim_qk: ", params.d_qk);  // 命中这里
       }
   } ();
   ```

3. 定位命中行：两个 `if` 都不成立，进入 `else`，即 [csrc/api/common.h:72-74](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e457e089f888280e0/csrc/api/common.h#L72-L74)，其中 [第 73 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L73) 执行 `TORCH_CHECK(false, "Unsupported head_dim_qk: ", HEAD_DIM)`。

**需要观察的现象 / 预期结果**：

- 错误消息为 `Unsupported head_dim_qk: 256`（`TORCH_CHECK` 会把 `HEAD_DIM` 的值 256 拼到消息里），Python 端收到 `RuntimeError`。
- **注意前置防线**：实际上 `sparse_attn_prefill_interface` 在到达宏之前，已在 [csrc/api/sparse_fwd.h:131](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L131) 用 `TORCH_CHECK(d_qk == 576 || d_qk == 512, "Invalid d_qk: ", d_qk)` 拦截了非法值，报的是 `Invalid d_qk: 256`。宏内的 `else` 是**纵深防御**——万一某个实现没在接口层做校验，宏本身也不会放行。

**解释 IIFE 如何生成独立特化**：

- 外层 `[&](){ ... }()` 把宏变成一个表达式，并允许每个 `if` 分支用 `return` 产出值。
- 每个分支里的 `static constexpr int HEAD_DIM_QK = <值>` 是**该分支独有**的编译期常量。
- 用户传入的 lambda `[&](){ run_fwd_phase1_kernel<HEAD_DIM_QK>(params); }` 在每个分支里被立即调用（`__VA_ARGS__()`），调用点对 `HEAD_DIM_QK` 的名字查找会绑定到**当前分支**的 constexpr。
- 于是编译器看到两个真实的模板实参 `<576>` 和 `<512>`，分别实例化出两份 `run_fwd_phase1_kernel` 代码；运行时只执行命中的那一份。**这就是「为每种枚举值生成独立模板特化」的全部秘密。**

> 待本地验证：在有 CUDA 的环境编译并人为构造 `d_qk=256`，确认 Python 端实际收到的异常文本（应先命中 [sparse_fwd.h:131](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L131)）。

#### 4.3.5 小练习与答案

**练习 1**：`DISPATCH_BOOLEAN_FLAG` 为什么没有 `else` 报错分支，而另外三个都有？

**答案**：布尔值只有 `true` / `false` 两种取值，两个 `if/else` 分支已穷尽所有可能，不可能落入「未支持」的情形；而 `num_heads`、`head_dim`、`model_type` 的合法取值只是「所有可能 int/枚举值」的一个子集（如 `head_dim` 还可能是 256、128 等），所以需要一个 `else` 来拒绝未覆盖的值。

**练习 2**：如果把 `DISPATCH_NUM_HEADS` 和 `DISPATCH_HEAD_DIM` 嵌套使用，最终会实例化出多少份 kernel 特化？为什么运行时不会有性能损失？

**答案**：\(2 \times 2 = 4\) 份（128/64 头 × 576/512 维）。运行时损失可忽略：外层是一个普通 `if/else` 跳转到命中分支，内层再一个 `if/else`，两次整数比较 + 一次跳转，相对 kernel 本身的执行时间可以忽略；而换来的收益是每份特化都拥有编译期已知的维度，循环完全展开、shared memory 静态分配。

**练习 3**：宏里的 `__VA_ARGS__` 后面为什么一定要跟 `()`（即写成 `__VA_ARGS__()`）？

**答案**：因为调用方传入的是**一个 lambda**（如 `[&](){ ... }`），宏需要在这个分支里**立即调用**它，才能让 lambda 体内的代码在「当前分支的 constexpr 作用域」里执行，并把 `HEAD_DIM_QK` 等名字解析到该分支的常量。若漏掉 `()`，lambda 只是被定义却从不调用，kernel 不会启动，且模板实参绑定也不会发生。

---

## 5. 综合实践

**任务**：把本讲三个机制（`Arch` 检测、stride 溢出保护、`DISPATCH_*` 编译期化）串起来，完整追踪一次 sparse prefill 从 Python 到 kernel 特化的「安全 + 派发」全链路，并画出每个机制的拦截位置。

**操作步骤**：

1. 打开 [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) 的 `sparse_attn_prefill_interface`，按代码顺序标注下列三道关卡的位置（行号）：
   - **架构关卡**：`Arch` 构造 + `is_sm90a/is_sm100f` 检查（第 112–115 行）。
   - **维度关卡**：`d_qk == 576 || 512` 的前置 `TORCH_CHECK`（第 131 行）。
   - **步长关卡**：装配 `SparseAttnFwdParams` 时一连串 `int64_stride_to_int`（第 179–181 行）。
2. 继续标注 **DISPATCH 关卡**：`Fwd_Sm90_Impl::run_` 内的 `DISPATCH_HEAD_DIM` + `DISPATCH_BOOLEAN_FLAG`（第 42–46 行），以及它在宏内展开后的 `else` 报错行（[common.h:73](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L73)）。
3. 用一张表填写：给定一组输入 `(arch, d_qk, q.stride(0))`，会在哪一道关卡被拦下、报什么错。例如：

   | 输入 | 命中关卡 | 行号 | 报错消息 |
   |------|---------|------|---------|
   | SM8.9, d_qk=576, stride 正常 | 架构关卡 | sparse_fwd.h:115 | `Sparse Attention Forward Kernel is only supported on SM90a and SM100f architectures.` |
   | SM90a, d_qk=256, stride 正常 | 维度关卡 | sparse_fwd.h:131 | `Invalid d_qk: 256` |
   | SM90a, d_qk=576, stride 溢出 | 步长关卡 | sparse_fwd.h:179（→ common.h:46） | `[FlashMLA] Stride exceeds int32 limit: <值>` |
   | （绕过前置校验）d_qk=256 进入宏 | DISPATCH else | common.h:73 | `Unsupported head_dim_qk: 256` |

4. 最后写出：当输入合法（SM90a, d_qk=576, topk_length 非空）时，`DISPATCH_HEAD_DIM` 与 `DISPATCH_BOOLEAN_FLAG` 会实例化出哪几份 `run_fwd_phase1_kernel` 特化。

**预期结果**：

- 你应当能用一张「关卡表」说清：架构、维度、步长、模板派发四道关卡**层层递进、互为纵深防御**，任何非法输入都尽早被拦在 kernel 启动之前。
- 合法输入下，仅 SM90 sparse prefill 一个实现就会预编译 \(2 \times 2 = 4\) 份 phase1 kernel（576/512 × 有/无 topk_length），运行时只执行命中的一份。

## 6. 本讲小结

- **`Arch`**（[common.h:20-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L20-L41)）：构造时通过 `at::cuda::getCurrentDeviceProperties()` 查询当前 GPU，缓存 `major/minor/num_sms`，提供 `is_sm90a()` / `is_sm100f()` 两个判定，是接口函数做架构分支与 `num_sm_parts` 切分的依据。
- **`int64_stride_to_int`**（[common.h:43-49](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L43-L49)）：把 PyTorch 的 `int64_t` 步长安全收窄为 `int32_t`，越过 \(2^{31}-1\) 即报错，是长上下文场景下防止整数溢出的安全闸门（仅用于 `int` 步长的 sparse 路径）。
- **`DISPATCH_*` 宏**（[common.h:51-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L51-L99)）：用「立即调用的 lambda」把运行时的 `head_dim` / `num_heads` / `model_type` / bool 编译期化，每个分支定义独有的 `constexpr`，从而为每个合法取值实例化一份独立的模板 kernel。
- **IIFE 是关键**：外层 `[&](){ ... }()` 让宏成为表达式，内层 `__VA_ARGS__()` 立即调用用户 lambda 并绑定到当前分支的 constexpr；运行时只走一条分支，编译期却生成全部特化。
- **纵深防御**：架构检查、维度校验、stride 溢出、DISPATCH 的 `else` 报错层层把关，非法值绝不会走到 kernel 内部；这也解释了为何 DISPATCH 的 `else` 即便极少触发也必须存在。

## 7. 下一步学习建议

- **u2-l4 ImplBase 派发框架与 Feature 声明**：本讲的 `DISPATCH_*` 宏解决了「把单个运行时值编译期化」，而 `ImplBase` 解决的是「在一组候选实现里挑出支持当前 feature 集合的那一个」。两者协同：接口先用 `Arch` + feature 集合选出 Impl，Impl 的 `run_` 里再用 `DISPATCH_*` 做模板特化。建议接着读 `DECLARE_SUPPORTED_FEATURES`、`check_if_all_features_are_supported` 以及 [common.h:101-141](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L101-L141) 的枚举名反射。
- **Unit 4 Split-KV / tile scheduler**：本讲只提到 `arch.num_sms` 用于算 `num_sm_parts`，真正的切分算法在 `get_decoding_sched_meta` 与 `combine` 里，读完后你会明白 `num_sm_parts` 如何决定 `total_num_splits` 与每份局部 `lse` 的合并。
- **任一 kernel 家族（Unit 3/5/6）**：本讲解释了「模板特化如何被触发」，当你读某个具体 kernel（如 SM90 dense decode 的 `splitkv_mla.cuh`）时，回头看它的模板参数 `HEAD_DIM` / `NUM_HEADS`，就能对应到接口层 `DISPATCH_*` 宏注入的编译期常量，把调用链彻底打通。
