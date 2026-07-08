# kerutils host / device 工具层

## 1. 本讲目标

本讲专门讲解 FlashMLA 的底层工具库 `csrc/kerutils/`。它是一组 header-only 的「胶水代码」，夹在「PyTorch 张量 / CUDA Runtime」与「各代架构的 attention kernel」之间，把重复的、与硬件强绑定的琐事集中收口。

学完本讲你应该能够：

- 说出 `KU_CHECK_*` 系列张量校验宏与 `get_optional_tensor_ptr` 的定义位置、作用机制，并能在 `csrc/api/*.h` 里找到真实调用点。
- 理解 device 公共工具（`CacheHint`、`PrefetchSize`、基础类型别名、`KERUTILS_ENABLE_SM*` 架构开关）以及 `sm80 / sm90 / sm100` 三层 intrinsics/helpers 的分层逻辑。
- 理解 `csrc/defines.h` 中的基础向量类型（`bf16`、`fp8`、`int32x8_t`、`float8`、`bf16x8`）与 barrier 别名（`transac_bar_t`），以及 `csrc/utils.h` 中的 `CHECK_CUDA`、`FLASH_ASSERT`、`RingBufferState` 等全局工具。

本讲是 Unit 8 的第一篇，前置依赖为 u2-l1（调用链全景）。kerutils 是上一讲反复出现的「张量校验」「架构判定」背后的实现，本讲把它们一次性讲透。

## 2. 前置知识

阅读本讲前，建议你已了解以下概念（不熟悉也没关系，下面会顺手解释）：

- **header-only 库**：所有代码都写在 `.h / .cuh` 头文件里，被使用方 `#include` 后直接参与编译，没有独立的 `.cpp` 需要链接。kerutils 就是这样，`setup.py` 不单独编译它，而是被各 kernel 拉进去一起编。
- **PTX / inline asm**：CUDA 的底层指令集叫 PTX。kerutils 里大量 `asm volatile("...")` 是直接手写 PTX 指令，因为有些新硬件指令编译器内置函数（intrinsic）尚未暴露，只能内联汇编。
- **`__host__` / `__device__`**：CUDA 函数修饰符，标注函数能在 CPU 上跑、GPU 上跑、或两者都能跑。`CUTE_DEVICE` 是 CuTe 库的等价宏，表示「device 端函数」。
- **可选张量（optional tensor）**：PyTorch 的 `std::optional<at::Tensor>`。FlashMLA 的接口里很多输入是「可空」的（如 `attn_sink`、`topk_length`），传 `std::nullopt` 表示禁用该功能。

一个贯穿全讲的总思路：**kerutils 把「与 PyTorch 打交道的 host 校验」和「与 GPU 硬件打交道的 device 原语」分成两条线**，前者给 `csrc/api/*.h` 用，后者给 `csrc/sm*/...` 的 kernel 用，两者都挂在 `kerutils`（别名 `ku`）命名空间下。

## 3. 本讲源码地图

| 文件 | 作用 | 服务对象 |
|------|------|----------|
| [csrc/kerutils/include/kerutils/host/host.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/host/host.h) | host 侧：异常类 `KUException`、`KU_CUDA_CHECK`/`KU_CUTLASS_CHECK`/`KU_ASSERT`、`ceil_div`/`ceil`、TMA 描述符构造 `make_tensor_map` | api 层、host 启动代码 |
| [csrc/kerutils/include/kerutils/supplemental/torch_tensors.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/supplemental/torch_tensors.h) | 张量校验宏 `KU_CHECK_*` 与取指针模板 `get_optional_tensor_ptr` | api 层（`csrc/api/common.h:8` 直接 include） |
| [csrc/kerutils/include/kerutils/common/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/common/common.h) | 全局：`KU_PRINTLN`、命名空间别名 `ku = kerutils` | 所有 kerutils 文件 |
| [csrc/kerutils/include/kerutils/device/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h) | device 公共：`CacheHint`/`PrefetchSize` 枚举、`nvbf16`/`bf16` 类型别名、`KERUTILS_ENABLE_SM*` 架构开关 | 所有 device 文件 |
| [csrc/kerutils/include/kerutils/device/device.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/device.cuh) | device 聚合头：一次性 include 全部架构层 | kernel 文件 |
| [csrc/kerutils/include/kerutils/device/sm80/{intrinsics,helpers}.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm80/intrinsics.cuh) | SM80 基线原语：`cp.async.cg`、cache policy、`get_sm_id`、`LDG.128` | SM80+ 通用（SM90/100 向下兼容） |
| [csrc/kerutils/include/kerutils/device/sm90/{intrinsics,helpers}.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm90/intrinsics.cuh) | SM90/Hopper 原语：DSM `st_async`/`get_peer_addr`、cluster barrier、`wgmma` | dense decode、FP8 sparse decode |
| [csrc/kerutils/include/kerutils/device/sm100/{intrinsics,helpers,gemm,tma_cta_group2_nosplit}.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm100/intrinsics.cuh) | SM100/Blackwell 原语：`tma_gather4`、UMMA arrive、TMEM load/store、`UTCMMA`、2-SM TMA | SM100 sparse prefill/decode |
| [csrc/defines.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/defines.h) | 全项目基础类型：`bf16`/`fp16`/`transac_bar_t`/向量结构体 | 整个 csrc |
| [csrc/utils.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h) | 全项目 host/device 宏：`CHECK_CUDA`/`FLASH_ASSERT`/`FLASH_DEVICE_ASSERT`/`RingBufferState` | 整个 csrc |

一个值得记住的结构事实：device 侧的 `device.cuh` 是个**聚合头**，它把 `common.h` + sm80 + sm90 + sm100 全部 include 进来（见 [device.cuh:1-13](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/device.cuh#L1-L13)）。而 `kerutils.cuh` 又把 host 与 device 两者聚合（[kerutils.cuh:1-5](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/kerutils.cuh#L1-L5)）。所以 kernel 只要 `#include <kerutils/kerutils.cuh>` 就拿到了全部工具，架构差异则靠 `__CUDA_ARCH__` 在编译期自动裁剪。

## 4. 核心概念与源码讲解

### 4.1 host 工具与张量校验宏

#### 4.1.1 概念说明

kerutils 的 host 线（`host.h` + `torch_tensors.h`）解决两类问题：

1. **「我拿到的 PyTorch 张量符不符合 kernel 的预期？」**——形状、维数、dtype、所在设备、内存是否连续，这些校验如果每个接口函数都手写一遍 `TORCH_CHECK`，会非常啰嗦且容易漏。kerutils 把它们收成一组 `KU_CHECK_*` 宏，并支持「可选张量」（传 `std::nullopt` 时自动跳过校验、视为合法）。
2. **「我调的 CUDA / CUTLASS API 成功了吗？」**——CUDA 驱动 API 与 CUTLASS 都用返回码表示成败，每次都写 `if (err != success) {...}` 很烦。kerutils 提供 `KU_CUDA_CHECK` / `KU_CUTLASS_CHECK` / `KU_ASSERT`，失败时抛一个带文件名行号的 `KUException`。

一个关键设计：校验宏内部用 `std::function` 回调统一处理「张量」与「可选张量」两种类型，因此同一套宏对两种入参都适用——这就是 `_check_optional_tensor` 模板的作用。

#### 4.1.2 核心流程

`KU_CHECK_*(tensor, ...)` 的执行链路：

1. 宏展开成一条 `TORCH_CHECK(ku::_check_optional_tensor(tensor, lambda), "报错信息")`。
2. `_check_optional_tensor` 用 `if constexpr` 判断入参类型：
   - 若是 `at::Tensor`：直接对该张量跑校验 lambda；
   - 若是 `std::optional<at::Tensor>`：有值才跑，无值直接返回 `true`（合法）。
3. 校验 lambda 是宏用 `[&]` 捕获「期望值」生成的，例如 `KU_CHECK_NDIM(t, 4)` 捕获 `ndim=4`，lambda 体是 `t.dim() == 4`。
4. 不通过则 `TORCH_CHECK` 抛 `std::runtime_error`，PyTorch 端收到的是一条清晰的中文/英文错误，而不是 kernel 里的乱跑或段错误。

取指针则走 `get_optional_tensor_ptr<T>(tensor_or_opt)`：返回 `(T*)tensor.data_ptr()`，若可选张量为空或无存储则返回 `nullptr`——这个 `nullptr` 正是 kernel 用来判断「某功能是否启用」的开关（参见 u2-l2 中「可空张量统一用 `nullptr` 表示禁用」）。

#### 4.1.3 源码精读

**统一的可选张量校验**，[torch_tensors.h:14-25](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/supplemental/torch_tensors.h#L14-L25)：用 `if constexpr` 在编译期分叉，是整套宏能同时吃 `Tensor` 与 `optional<Tensor>` 的根。

```cpp
template<typename T>
static inline bool _check_optional_tensor(const T& tensor_or_opt,
        const std::function<bool(const at::Tensor&)>& check_fn) {
    if constexpr (std::is_same<T, at::Tensor>::value) {
        return check_fn(tensor_or_opt);          // 普通张量：直接校验
    } else {
        if (tensor_or_opt.has_value()) return check_fn(tensor_or_opt.value());
        else return true;                          // 可选且为空：视为合法
    }
}
```

**取指针模板**，[torch_tensors.h:40-51](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/supplemental/torch_tensors.h#L40-L51)：空可选或无存储都回 `nullptr`，正是 params 结构里「`nullptr` = 禁用」约定的来源。

**六个校验宏**，[torch_tensors.h:56-71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/supplemental/torch_tensors.h#L56-L71)：注意末维连续的判定 `t.size(-1)==1 || t.stride(-1)==1`——允许末维大小为 1（退化为标量）或步长为 1（真正连续）两种情况。

```cpp
#define KU_CHECK_DEVICE(tensor)        TORCH_CHECK(ku::_check_optional_tensor(tensor, \
    [](const at::Tensor& t){ return t.is_cuda(); }), #tensor " must be on CUDA")
#define KU_CHECK_NDIM(tensor, ndim)    TORCH_CHECK(ku::_check_optional_tensor(tensor, \
    [&](const at::Tensor& t){ return t.dim() == (ndim); }), ...)
#define KU_CHECK_SHAPE(tensor, ...)    ... t.sizes() == torch::IntArrayRef({__VA_ARGS__}) ...
#define KU_CHECK_LAST_DIM_CONTIGUOUS(tensor) ... t.size(-1)==1 || t.stride(-1)==1 ...
#define KU_CHECK_DTYPE(tensor, target_dtype) ... t.dtype() == (target_dtype) ...
```

**host 侧的错误处理与工具**，[host.h:17-36](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/host/host.h#L17-L36)：`KUException` 用变参模板 + fold expression 拼接错误串，`THROW_KU_EXCEPTION` 自动带上 `__FILE__`/`__LINE__`。

```cpp
class KUException final : public std::exception {
    template<typename... Args>
    explicit KUException(const char *name, const char* file, const int line, Args&&... args) {
        std::ostringstream oss;
        oss << name << " error (" << file << ":" << line << "): ";
        (oss << ... << args);          // fold expression: 把所有参数拼进流
        message = oss.str();
    }
};
```

[host.h:38-69](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/host/host.h#L38-L69) 是 `KU_CUDA_CHECK`（包 `cudaError_t`）、`KU_CUTLASS_CHECK`（包 `cutlass::Status`）、`KU_ASSERT`（不受 `-DNDEBUG` 影响，永远生效）和 `KU_CHECK_KERNEL_LAUNCH()`（=`KU_CUDA_CHECK(cudaGetLastError())`，kernel 启动后兜底抓异步错误）。

`make_tensor_map`（[host.h:82-143](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/host/host.h#L82-L143)）是 TMA（Tensor Memory Accelerator）描述符的 host 端构造器：它把 size/stride/box_size 等参数喂给驱动 API `cuTensorMapEncodeTiled`，得到一个 `CUtensorMap`。注释特别提醒 **stride 以字节为单位**（而 params 结构里 stride 以元素为单位，见 u2-l2），失败时会逐项打印所有输入辅助调试。配套的 `make_stride_helper`（[host.h:146-153](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/host/host.h#L146-L153)）把「元素步长」转成「字节步长」喂给它。

#### 4.1.4 代码实践

**实践目标**：验证 `KU_CHECK_*` 与 `get_optional_tensor_ptr` 的定义位置，并找到它们在 `csrc/api/*.h` 中的真实调用点，理解 api 层如何复用这套校验。

**操作步骤**：

1. 确认 api 层的入口 include。打开 [csrc/api/common.h:8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L8)，你会看到 `#include <kerutils/supplemental/torch_tensors.h>`——这就是 `KU_CHECK_*` 进入 api 层的通道，所有 `csrc/api/*.h` 都间接（经 common.h）拿到了这套宏。
2. 看 dense decode 的校验段落。打开 [csrc/api/dense_decode.h:40-53](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L40-L53)，这是 `KU_CHECK_DEVICE` / `KU_CHECK_CONTIGUOUS` 的密集调用点。
3. 看形状校验。同一文件 [dense_decode.h:80-85](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L80-L85)，`KU_CHECK_SHAPE(q, batch_size, q_seq_per_hk, num_heads, head_size_k)` 在 Q 做 head 维重排之后再次校验形状，确保重排结果符合 kernel 预期。
4. 看「可选张量取指针」的真实用法。打开 [csrc/api/sparse_decode.h:394-402](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L394-L402)，`ku::get_optional_tensor_ptr<int>(topk_length)` 把可能为空的可选张量变成 `int*`（空则 `nullptr`），再塞进 `SparseAttnDecodeParams`。

**需要观察的现象**：

- 步骤 2 里 `KU_CHECK_DEVICE(q)` 等宏只写一行，但背后已经包含「可选张量分叉 + lambda 校验 + TORCH_CHECK」三件事，对比同段中仍手写的 `TORCH_CHECK(q.stride(-1) == 1, ...)`（[dense_decode.h:48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L48)），就能直观看出宏的省事之处。

**预期结果**：

- 你能复述：api 层不自己写形状/dtype/device 校验循环，而是全部委托给 `KU_CHECK_*`；可选输入一律经 `get_optional_tensor_ptr` 转 `nullptr`-able 指针。这是 u2-l1 中「接口函数统一遵循校验→装配→派发」套路的实现根基。

#### 4.1.5 小练习与答案

**练习 1**：`KU_CHECK_LAST_DIM_CONTIGUOUS(t)` 的判定是 `t.size(-1)==1 || t.stride(-1)==1`，为什么要把 `size(-1)==1` 单独列为合法？

**答案**：当末维大小为 1 时，该维只有一个元素，「是否连续」无意义（步长可为任意值）。把它判为合法，是为了让形如 `[..., 1]` 的退化张量也能通过校验，避免误杀。

**练习 2**：`KU_ASSERT` 与标准 `assert` 有何关键区别？为什么 kerutils 要自己造一个？

**答案**：标准 `assert` 在 `-DNDEBUG` 下会被编译掉（ release 构建里完全不检查），而 `KU_ASSERT` 注释明确写了「triggered no matter if compiled with `-DNDEBUG` or not」，即永远生效。kernel 库对参数合法性零容忍，不能因为 release 构建就关掉校验，所以要自造一个不受 NDEBUG 影响的断言。

---

### 4.2 device 公共工具与架构开关

#### 4.2.1 概念说明

device 线的「公共层」是 [device/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h)，它定义两样所有架构都用得上的东西：

1. **与硬件无关的枚举**：`CacheHint`（L2 cache 驱逐策略：`EVICT_FIRST`/`EVICT_NORMAL`/`EVICT_LAST`/`EVICT_UNCHANGED`/`NO_ALLOCATE`）与 `PrefetchSize`（预取粒度 `B64`/`B128`/`B256`）。这些是 PTX `createpolicy` / `cp.async` 指令的参数枚举，提取出来避免到处写魔法数字。
2. **架构开关宏**：一组 `KERUTILS_ENABLE_SM80` / `_SM90` / `_SM90A` / `_SM100` / `_SM100A`，由 `__CUDA_ARCH__` 在编译期自动定义。各架构层（sm80/sm90/sm100）的 intrinsics 用这些宏把「只有某代架构才有的指令」包起来，编译到哪代 GPU 就只激活哪一代的原语。

还有一个 subtle 但重要的点：`common/common.h` 里有一行 `namespace ku = kerutils;`（[common.h:7](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/common/common.h#L7)），所以全项目里 `ku::xxx` 与 `kerutils::xxx` 是同一个东西的两种写法，kernel 里普遍用更短的 `ku::`。

#### 4.2.2 核心流程

架构开关的判定逻辑（[device/common.h:42-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h#L42-L62)）：

```
__CUDA_ARCH__ >= 800   → 定义 KERUTILS_ENABLE_SM80      （Ampere 及以上）
__CUDA_ARCH__ >= 900   → 定义 KERUTILS_ENABLE_SM90      （Hopper 及以上）
__CUDA_ARCH__ ∈ [900,1000) → 定义 KERUTILS_ENABLE_SM90A （Hopper 专属，含 WGMMA/TMA）
__CUDA_ARCH__ >= 1000  → 定义 KERUTILS_ENABLE_SM100     （Blackwell 及以上）
__CUDA_ARCH__ ∈ [1000,1200) → 定义 KERUTILS_ENABLE_SM100A（Blackwell 专属）
__CUDA_ARCH__ < 800    → static_assert(false)            （不支持 SM80 以下）
```

注意 `_SMxx`（无 A）是「及以上」（向下兼容），`_SMxxA`（带 A）是「仅这一代」（精确架构）。这呼应 u1-l2 讲过的 `sm_90a` / `sm_100f` gencode：带 `a` 后缀才启用 WGMMA/TMA/UMMA 这类专有指令，kerutils 用 `KERUTILS_ENABLE_SM90A` / `_SM100A` 在源码层做同样区分。

另一处方便 IDE 的设计：当检测到 `__CLION_IDE__` 或 `__VSCODE_IDE__`（[common.h:64-70](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h#L64-L70)），一次性定义全部架构宏，让 IDE 的代码索引在 host 端也能解析所有 device 原语，不被 `__CUDA_ARCH__` 未定义而灰掉。

#### 4.2.3 源码精读

**类型别名与枚举**，[device/common.h:16-39](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h#L16-L39)：

```cpp
enum class CacheHint { EVICT_FIRST, EVICT_NORMAL, EVICT_LAST, EVICT_UNCHANGED, NO_ALLOCATE };
enum class PrefetchSize { B64, B128, B256 };

using nvbf16    = __nv_bfloat16;       // CUDA 原生 bf16
using nvbf16x2  = __nv_bfloat162;      // 打包 2 个 bf16
using nve4m3    = __nv_fp8_e4m3;       // FP8 e4m3
using bf16      = cutlass::bfloat16_t; // CUTLASS 版 bf16（device 端常用）
using transac_bar_t = cutlass::arch::ClusterTransactionBarrier;  // cluster 事务屏障
```

这里出现了两套 bf16 别名：`nvbf16`（CUDA 驱动原生）与 `bf16`（CUTLASS 包装）。kernel 里混用两者，`transac_bar_t` 则是 SM90 cluster 同步的核心原语类型（crossover / DSM 都靠它，见 u5-l3）。

**架构开关**，[device/common.h:42-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h#L42-L62)：每一段都是 `#if (defined(__CUDA_ARCH__) && (__CUDA_ARCH__ >= XXX))` 形式，编译期常量，零运行时开销。

#### 4.2.4 代码实践

**实践目标**：理解架构开关如何让同一份 kerutils 源码安全地编进不同 gencode。

**操作步骤**：

1. 打开 [device/common.h:42-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h#L42-L62)，确认五档阈值。
2. 思考：当 `setup.py` 用 `-gencode arch=compute_90a,sm_90a` 编译时，`__CUDA_ARCH__` 被定义为 `900`，于是 `SM80`、`SM90`、`SM90A` 三档全部激活，但 `SM100*` 不激活——这就是 SM90 kernel 能调用 `wgmma`（依赖 `SM90A`）但不会误触 SM100 UMMA 指令的原因。
3. 对比 [u1-l2](u1-l2-build-and-install.md) 讲的 `get_arch_flags`：host 端用 NVCC 的 gencode 决定生成哪代机器码，device 端用 `__CUDA_ARCH__` 让源码自适应——两层机制配合，实现「一份源码、多架构编译」。

**需要观察的现象 / 预期结果**：

- 你应能解释：为何 kerutils 顶部有一句 `static_assert(false, "kerutils doesn't support SM architectures below SM80")`（[common.h:44-45](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/common.h#L44-L45)）——它是硬底线，编译到 SM80 以下直接报错，防止有人在老卡上误用。

#### 4.2.5 小练习与答案

**练习 1**：`KERUTILS_ENABLE_SM90` 与 `KERUTILS_ENABLE_SM90A` 的区别是什么？一个 SM100 kernel 编译时，哪个会被定义？

**答案**：`_SM90` 是 `__CUDA_ARCH__ >= 900`（Hopper 及以上，含 Blackwell），`_SM90A` 是 `900 <= __CUDA_ARCH__ < 1000`（仅 Hopper）。编译 SM100（`__CUDA_ARCH__==1000`）时，`_SM90` 会被定义（因为 1000≥900），但 `_SM90A` 不会（因为 1000 不在 [900,1000) 区间）——这正确地阻止了 SM100 kernel 调用 Hopper 专属的 WGMMA。

**练习 2**：为什么 `common.h` 要为 IDE 单独定义全部架构宏？

**答案**：IDE 的代码索引运行在 host 端，`__CUDA_ARCH__` 未定义，导致所有 device 原语被预处理器跳过、显示为灰色未解析。给 IDE 一份「全部宏都打开」的假环境，是为了让跳转/补全正常工作，不影响真实编译（真实编译时这些 IDE 宏不存在）。

---

### 4.3 sm{80,90,100} intrinsics / helpers 分层

#### 4.3.1 概念说明

这是 kerutils device 线的主体，按 GPU 架构分三层目录：

- **sm80**（Ampere 基线）：最通用的原语——`cp.async.cg` 异步拷贝、cache policy 生成、`get_sm_id()`、`LDG.128/256`。因为 SM90/SM100 向下兼容 SM80，这一层是所有架构共享的基础设施。
- **sm90**（Hopper）：cluster / DSM 相关原语——`st_async`（异步写共享内存并扣 mbarrier 字节计数）、`get_peer_addr`（DSM 对端地址）、cluster barrier、`wgmma`/`wgmma_ss`/`wgmma_rs`（WGMMA 矩阵乘封装）。
- **sm100**（Blackwell）：TMEM / UMMA 相关——`tma_gather4`（稀疏索引的 TMA gather，sparse attention 的关键）、`umma_arrive_*`（UMMA 完成通知）、`tmem_ld_*`/`tmem_st_*`（TMEM 读写）、`gemm.cuh` 里的 `SM100_MMA_*_NOELECT` 自定义 MMA atom、`tma_cta_group2_nosplit.cuh` 里的 2-SM multicast TMA。

每层都分 `intrinsics.cuh`（裸 PTX 封装）与 `helpers.cuh`（更高层的组合工具）。`intrinsics` 更贴近单条指令，`helpers` 把多条指令或 CuTe 对象组合成可复用流程。

#### 4.3.2 核心流程

以 sparse attention 解码为例，三层原语的协作链路：

```
SM80 层：create_simple_cache_policy<EVICT_LAST>()  →  生成 64 位 cache hint
                                      │
SM100 层：tma_gather4_cta_group_2(desc, mbar, smem, col, rows, hint)
                                      │  用上面的 hint 做带索引的 TMA gather
SM100 层：umma_arrive_multicast_2x1SM_noelect(bar, mask)
                                      │  通知 2-SM cluster 的 UMMA 完成
SM100 层：tmem_ld_32dp32bNx<N>(addr, data)   →  从 TMEM 读结果进寄存器做 softmax
```

注意每条原语都附带 [PTX 文档链接注释](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm100/intrinsics.cuh#L7)，这是阅读 kerutils 的最佳入口——遇到不懂的指令直接点注释里的 NVIDIA PTX 手册锚点。

#### 4.3.3 源码精读

**SM80 基线：cache policy 与 SM id**，[sm80/intrinsics.cuh:80-95](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm80/intrinsics.cuh#L80-L95)：`create_simple_cache_policy<CACHE_HINT>()` 直接返回一个写死的 64 位常量（如 `EVICT_FIRST` → `0x12F0000000000000`），等价于 `createpolicy.fractional` 指令在 `fraction=1.0` 时的结果，省去运行时发指令。

[sm80/intrinsics.cuh:112-126](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm80/intrinsics.cuh#L112-L126) 的 `get_sm_id()` 读特殊寄存器 `%smid`，注释里有一段重要警告：PTX 手册说 `%smid` 编号不保证连续，但实测 sm90/sm100f 上 `%nsmid = 物理 SM 数 - 1`，因此推荐用 helpers 里的 `get_sm_id_with_range_check()` 做范围校验（[sm80/helpers.cuh:9-16](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm80/helpers.cuh#L9-L16)）。

**SM90：DSM 与 crossover 的核心**，[sm90/intrinsics.cuh:22-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm90/intrinsics.cuh#L22-L29)：

```cpp
static constexpr int PEER_ADDR_MASK = 16777216;   // 1<<24
template<typename T>
CUTE_DEVICE T* get_peer_addr(const T* p) {
    return (T*)((int64_t)(p) ^ PEER_ADDR_MASK);   // 异或第 24 位得到对端 CTA 的同位置地址
}
```

这是 u5-l3 crossover 技术的地址基石：cluster 内两个 CTA 的共享内存地址只差第 24 位，异或一下就指向对端。`st_async`（[sm90/intrinsics.cuh:7-20](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm90/intrinsics.cuh#L7-L20)）则是把数据写入共享内存的同时给 mbarrier 记一笔字节数，配合消费端的 `arrive_and_expect_tx` 实现「到达计数 + 字节计数」的事务屏障。

**SM90：WGMMA 封装**，[sm90/helpers.cuh:46-108](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm90/helpers.cuh#L46-L108)：`wgmma` / `wgmma_ss` / `wgmma_rs` 三个模板函数封装了 Hopper 的 warpgroup MMA。注意它们都正确处理了 `warpgroup_fence_operand`（操作数生命周期围栏）与 `accumulate_` 首块清零、后续累加的语义——这正是 u3-l3 seesaw 调度里两套 WGMMA（O_L/O_R）调用的底层封装。

**SM100：sparse gather 与 UMMA**，[sm100/intrinsics.cuh:9-21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm100/intrinsics.cuh#L9-L21) 的 `tma_gather4` 是 sparse attention 的命脉——它用 `cp.async.bulk.tensor.2d...tile::gather4` 一条指令按 `int4 row_idxs`（4 个行索引）做稀疏 gather，这正是 u6 讲的 token-level sparse attention 能高效取数的原因。注释警告 gather4 坐标是 `int32`，序列极长时可能溢出。

[sm100/intrinsics.cuh:217-258](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm100/intrinsics.cuh#L217-L258) 的 `umma_arrive_*` 系列封装 `tcgen05.commit`，通知 UMMA（Blackwell 的 Tensor Core）完成；[sm100/intrinsics.cuh:272-299](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm100/intrinsics.cuh#L272-L299) 的 `tmem_ld_32dp32bNx<N>` 用一串 `if constexpr` + CuTe 内置 `SM100_TMEM_LOAD_32dp32bNx::copy` 把 TMEM（Tensor Memory）数据读进寄存器——`N` 是重复次数，编译期特化。

**SM100：自定义 MMA atom**，[sm100/gemm.cuh:13-43](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm100/gemm.cuh#L13-L43)：注释直白地写了「CuTe don't support UTCMMA with .ws」且「CuTe's UTCMMA has an `elect_one_sync()` inside which is really disgusting」——所以 kerutils 造了不带 `elect_one_sync` 的 `SM100_MMA_F16BF16_*_NOELECT` 系列，给 warp-specialized mainloop 用（u7-l2）。这是「工具库补标准库之不足」的典型范例。

#### 4.3.4 代码实践

**实践目标**：在真实 kernel 里找到这些 device 原语的调用点，建立「kerutils 定义 → kernel 消费」的对应关系。

**操作步骤**：

1. **TMEM 读写 + UMMA**：打开 [csrc/sm100/decode/head64/kernel.cuh:169-173](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head64/kernel.cuh#L169-L173)，看 `ku::tmem_ld_32dp32bNx<B_TOPK/2>(tmem_cols::P, p)` 把 TMEM 里的 P 矩阵读进寄存器；同文件 [:514](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head64/kernel.cuh#L514) 与 [:573](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head64/kernel.cuh#L573) 是 `ku::umma_arrive_noelect` 通知 UMMA 完成。
2. **cache policy + 2-SM gather**：打开 [csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh:408](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L408) `ku::create_simple_cache_policy<ku::CacheHint::EVICT_LAST>()`，再看到 [:438](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L438) 的 `ku::tma_gather4_cta_group_2<true>(...)` 把这个 cache hint 用上。
3. **DSM crossover**：打开 [csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh:507-513](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L507-L513)，`get_peer_addr(&(plan.bar_k_remote_ready[buf_idx]))` 与 `get_peer_addr(sK_nope_base)` 取对端 CTA 的屏障与 KV 地址——这就是 u5-l3 crossover 的现场。

**需要观察的现象**：

- 步骤 3 里的 `get_peer_addr` 在 sparse_fp8 kernel 里其实有一份**本地副本**（[components/helpers.h:102-107](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/helpers.h#L102-L107)），与 kerutils 的 [sm90/intrinsics.cuh:22-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm90/intrinsics.cuh#L22-L29) 完全等价。这说明 kerutils 是「规范的收口处」，但个别 kernel 在历史上可能先有本地实现、后才有 kerutils——读代码时要意识到这种重复。

**预期结果**：

- 你能画出 `ku::tmem_ld_*` / `ku::umma_arrive_*` / `ku::tma_gather4*` / `ku::create_simple_cache_policy` / `get_peer_addr` 五类原语在 kernel 里的真实落点，理解 kerutils 是「kernel 的标准零件库」。

#### 4.3.5 小练习与答案

**练习 1**：`get_peer_addr` 用异或 `PEER_ADDR_MASK`（1<<24）得到对端地址。为什么是异或而不是加法？

**答案**：cluster 内两个 CTA 的共享内存地址在地址空间里是对称分布的，只差第 24 位（一个为 0、一个为 1）。异或同一个掩码可实现「0↔1 翻转」且可逆（再异或一次回到自己），比加法更安全（加法可能进位到高位、破坏地址）。这也是 `get_cta0_addr` 用「与 `0xFEFFFFFF`」把第 24 位清零、强制指向 CTA0 的道理。

**练习 2**：`gemm.cuh` 为什么要自己造 `SM100_MMA_F16BF16_*_NOELECT` 而不直接用 CuTe 内置的 UTCMMA？

**答案**：两个原因——CuTe 内置 UTCMMA 不支持 `.ws`（warp-specialized）变体；且其内部包含 `elect_one_sync()`（选出一个线程执行），这对 warp-specialized mainloop 里精心设计的角色分工是干扰（注释原话「really disgusting」）。去掉 `elect_one_sync` 后，调用方可以自己控制哪个线程/warp 发指令，更贴合 Blackwell mainloop 的流水设计。

---

### 4.4 基础类型 defines

#### 4.4.1 概念说明

最后一块是位于 `csrc/` 根目录的两个全局头：`defines.h` 与 `utils.h`。它们不属于 `kerutils/` 目录，但作用相似——为整个 csrc 提供最基础的数据类型与宏，被几乎所有文件 include。可以理解为「比 kerutils 更底层、更全局」的基础设施。

- **`defines.h`**：定义全项目共用的类型别名（`bf16`、`fp8`、`transac_bar_t`）与打包向量结构体（`int32x8_t`、`float8`、`bf16x8`），以及 barrier 相关的 CUTLASS 别名。
- **`utils.h`**：定义 host/device 通用的宏（`CHECK_CUDA`、`FLASH_ASSERT`、`FLASH_DEVICE_ASSERT`）与小型工具（`ceil_div`、`RingBufferState`）。

注意它们与 kerutils 有部分功能重叠（如 `ceil_div`、assert 宏），属于历史演进的痕迹——kerutils 是较新的、更系统的收口，而 `defines.h`/`utils.h` 是更早的全局头。读代码时两者都会遇到。

#### 4.4.2 核心流程

向量结构体的设计直觉：GPU 上单条指令能处理「多个打包元素」（如一条指令算 2 个 bf16 或 8 个 fp32），所以定义 `bf16x8`（4 个 `__nv_bfloat162`，共 8 个 bf16）这样的结构体，让 kernel 一次操作一整条向量，匹配硬件的 SIMD 宽度。

`RingBufferState`（[utils.h:58-82](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L58-L82)）则是流水线（pipeline）环形缓冲的状态机：维护一个 `cur_block_idx`，`get<NUM_STAGES>()` 返回当前该用哪个 stage 槽位以及正反相位（phase），是多级异步流水（如 TMA copy-GEMM 重叠）的通用计数器。

#### 4.4.3 源码精读

**类型别名与向量结构体**，[defines.h:6-26](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/defines.h#L6-L26)：

```cpp
using bf16 = cutlass::bfloat16_t;
using fp8  = cutlass::float_e4m3_t;
using transac_bar_t = cutlass::arch::ClusterTransactionBarrier;  // cluster 事务屏障
using cutlass::arch::NamedBarrier;                                // Hopper 命名屏障

struct int32x8_t { int a0,a1,a2,a3,a4,a5,a6,a7; };   // 8 个 int32 打包
struct float8   { float2 a01,a23,a45,a67; };          // 8 个 float32 打包
struct bf16x8   { __nv_bfloat162 a01,a23,a45,a67; };  // 8 个 bf16 打包
```

`transac_bar_t` 与 `NamedBarrier` 的 `using` 直接把 CUTLASS 的屏障类型引入全局命名空间——u3-l2 讲过的「5 个 NamedBarrier」「19 个 TMABarrier」就分别用这两个类型。`bf16`/`fp8` 是项目里统一的精度类型别名（注意与 device/common.h 里的 `nvbf16`/`nve4m3` 区分：这里是 CUTLASS 版，那里是 CUDA 原生版）。

**全局宏与工具**，[utils.h:5-23](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L5-L23)：`CHECK_CUDA`（包 cudaError_t，失败 `exit(1)`）与 `FLASH_ASSERT`（host/device 通用断言，失败也 `exit(1)`）。注意它们用 `exit(1)` 而非抛异常，比 kerutils 的 `KUException` 更「硬」——这是更早期、更粗暴的错误处理风格。

**device 端断言**，[utils.h:26-32](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L26-L32)：`FLASH_DEVICE_ASSERT` 失败时先 `printf` 再 `asm("trap;")` 让 GPU 当场停机——因为 kernel 里没法抛 C++ 异常，只能用 `trap` 指令自杀。`TRAP_ONLY_DEVICE_ASSERT`（[utils.h:41-55](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L41-L55)）是它的「静默版」，只 trap 不打印，用于对性能敏感的热路径（u4-3 末尾的 `FLASH_DEVICE_ASSERT` 兜底就用这类）。

**ceil_div 与 RingBufferState**，[utils.h:36-39](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L36-L39) 的 `ceil_div(a,b)=(a+b-1)/b` 是向上取整，kernel grid 计算的常客；[utils.h:58-82](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L58-L82) 的 `RingBufferState::get<NUM_STAGES>()` 返回 `{stage_idx = cur_block_idx % N, phase = (cur_block_idx/N)&1}`——`stage_idx` 选槽位、`phase` 处理「同一槽位跨轮复用时的屏障相位翻转」，这是异步流水正确性的关键。

#### 4.4.4 代码实践

**实践目标**：搞清 `defines.h` / `utils.h` 与 kerutils 的关系，避免在读代码时混淆两套并存的工具。

**操作步骤**：

1. 对比两套断言：[utils.h:5-23](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L5-L23) 的 `CHECK_CUDA`/`FLASH_ASSERT`（`exit(1)` 风格）与 [host.h:38-67](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/host/host.h#L38-L67) 的 `KU_CUDA_CHECK`/`KU_ASSERT`（抛 `KUException` 风格）。
2. 对比两套 `ceil_div`：[utils.h:36-39](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/utils.h#L36-L39) 与 [host.h:71-74](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/host/host.h#L71-L74)，两者实现一致，只是命名空间不同。
3. 在 kernel 里搜 `RingBufferState` 或 `FLASH_DEVICE_ASSERT` 的使用，体会这些全局工具在流水线与边界校验中的角色。

**需要观察的现象 / 预期结果**：

- 你会发现两套工具长期并存：新代码（尤其 api 层）倾向用 kerutils 的 `KU_*`（抛异常、信息更友好），老代码与 device 热路径仍用 `FLASH_*`/`CHECK_CUDA`（exit/trap、更直接）。读代码时按 include 头判断当前文件用的是哪一套即可，不必强求统一。

> 说明：本实践为「源码阅读型实践」，无需运行；若你想验证 `RingBufferState` 行为，可在本地写一个最小 host 程序模拟 `cur_block_idx` 从 0 递增、打印 `get<3>()` 的 `{stage_idx, phase}` 序列，预期看到 stage 在 0/1/2 循环、phase 每 3 步翻转一次。

#### 4.4.5 小练习与答案

**练习 1**：`bf16x8` 由 4 个 `__nv_bfloat162` 组成。为什么不打包成 1 个连续数组，而要拆成 4 个「2 元组」？

**答案**：因为 GPU 的 bf16 计算指令（如 `HFMA2`）原生以 `__nv_bfloat162`（2 个 bf16 打包）为操作单位，一条指令算一对。把 8 个 bf16 显式拆成 4 个 `b162`，让结构体布局直接对齐指令的操作数边界，避免 kernel 里再做拆包/重组。这也呼应 u5-l2 提到的 `HFMA2` 指令吞吐分析。

**练习 2**：`RingBufferState::get<NUM_STAGES>()` 同时返回 `stage_idx` 和 `phase`。`phase` 解决了什么问题？

**答案**：环形缓冲里同一个 stage 槽位会被多轮复用（第 0 轮和第 NUM_STAGES 轮都用 stage 0）。但 mbarrier 这类同步原语有「相位」概念——连续两次 arrive/wait 必须相位相反才算两次独立同步。`phase = (cur_block_idx/NUM_STAGES)&1` 让同一槽位在相邻两轮里相位翻转，保证流水线里对同一槽位的多次同步不会互相混淆。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，写一份「kerutils 速查表 + 调用关系图」。

**步骤**：

1. **建一张分层表**：按「host 线 / device 公共 / sm80 / sm90 / sm100 / 全局 defines」六行，每行列出该层最有代表性的 3 个工具（如 host 线 = `KU_CHECK_SHAPE`、`get_optional_tensor_ptr`、`make_tensor_map`）。
2. **画调用关系图**：以一次 sparse decode 调用为例，从 [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) 的 `KU_CHECK_*`（host 线）开始，到 `ku::get_optional_tensor_ptr` 取指针，再到 kernel 内的 `ku::umma_arrive_*` / `get_peer_addr`（device 线），用箭头标出「哪个 kerutils 工具被哪一行调用」。
3. **找一处重复**：确认 `get_peer_addr` 在 kerutils（[sm90/intrinsics.cuh:22-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm90/intrinsics.cuh#L22-L29)）与 sparse_fp8 本地（[components/helpers.h:102-107](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/helpers.h#L102-L107)）各有一份，思考：如果让你重构，会把哪一份删掉、依据是什么？

**预期产出**：一份能让你（或队友）日后读到任何 `ku::*` 调用时，都能秒查到「它定义在哪一层、解决什么问题、对应哪条 PTX 指令」的速查文档。

**待本地验证**：若你有 GPU 且已按 u1-l2 编译安装，可写一个最小 CUDA 程序 include `kerutils/kerutils.cuh`，在 device kernel 里调用 `ku::get_sm_id()` 与 `ku::create_simple_cache_policy<EVICT_FIRST>()` 打印结果，验证 device 原语可独立使用；无 GPU 环境则止步于源码阅读与文档产出。

## 6. 本讲小结

- kerutils 是 header-only 工具库，分 **host 线**（`host.h` + `torch_tensors.h`，服务 api 层的张量校验与错误处理）与 **device 线**（`device/` 下，服务 kernel 的硬件原语）。
- `KU_CHECK_*` 宏 + `_check_optional_tensor` 让同一套校验同时处理 `Tensor` 与 `optional<Tensor>`；`get_optional_tensor_ptr` 把可选张量统一转成 `nullptr`-able 指针，是 params 结构「`nullptr` = 禁用」约定的来源。
- device 线按 `sm80 → sm90 → sm100` 三层组织，由 `__CUDA_ARCH__` 与 `KERUTILS_ENABLE_SM*` 在编译期自动裁剪；sm80 是基线，sm90 加 DSM/WGMMA，sm100 加 TMEM/UMMA/TMA-gather。
- `device.cuh` / `kerutils.cuh` 是聚合头，一次性拉入全部架构层；`ku = kerutils` 是全项目通用的命名空间别名。
- `csrc/defines.h` 提供全局类型别名（`bf16`/`fp8`/`transac_bar_t`）与打包向量结构体；`csrc/utils.h` 提供 `CHECK_CUDA`/`FLASH_ASSERT`/`FLASH_DEVICE_ASSERT`/`ceil_div`/`RingBufferState`，与 kerutils 部分功能重叠、长期并存。
- 读 kerutils 的最佳入口是每个原语上方注释里的 NVIDIA PTX 手册链接，遇到陌生指令直接点进去查语义。

## 7. 下一步学习建议

- **横向**：回到 u2-l1/u2-l3 重新看 api 层的校验与派发，现在你能看到「校验一行 = `KU_CHECK_*` 宏展开 = `_check_optional_tensor` 分叉」的完整链条。
- **向 device 深入**：本讲只点了 `get_peer_addr`/`wgmma`/`tma_gather4`/`umma_arrive` 的定义，它们的真实用法分别在 u5-l3（crossover）、u3-l3（seesaw）、u6-l3（sparse prefill）、u7-l2（mainloop）——带着本讲建立的「定义在哪」的认知去读那些讲义的「怎么用」，会顺畅得多。
- **下一篇**：u8-l2 将讲解测试体系（`tests/lib.py` 的用例生成、`ref.py` 的参考实现、三类测试用例与容差判定），那里的 `ref.py` 是验证 kernel 正确性的黄金标准，与本讲的「校验宏」一前一后共同守护 FlashMLA 的质量。
