# 调用链全景：Python 到 CUDA kernel

## 1. 本讲目标

在 [u1-l4](u1-l4-python-api-quickstart.md) 里，我们已经会用 `flash_mla_with_kvcache`、`flash_mla_sparse_fwd`、`flash_attn_varlen_func` 这些 Python 函数调用 FlashMLA。但点击这些函数往里看，它们最终都消失在一个名为 `flash_mla_cuda` 的模块里——这是用 C++/CUDA 写的 PyTorch 扩展，Python 看不到里面发生了什么。

本讲要做的，就是把这条「黑盒」彻底打开。学完后你应当掌握：

1. 能画出一条完整的调用链：**Python 包装函数 → pybind 绑定 → C++ 接口函数 → kernel 命名空间**。
2. 知道 `flash_mla.cuda` 这个扩展模块对外暴露的 **5 个绑定**分别叫什么、对应哪个接口函数。
3. 能根据「架构 × 阶段 × 稀疏性」判断一次调用最终会落到哪个 kernel 命名空间（如 `sm90::`、`sm100::decode::head64::`、`smxx::decode::` 等）。
4. 理解 FlashMLA 在接口层用的两种派发风格：**直接调用**（dense decode / dense fwd/bwd）与 **基于 feature 集合的 ImplBase 派发器**（sparse decode / sparse fwd）。

本讲是后续所有 kernel 深入讲义（u3～u7）的「地图」。先有全局，再钻细节。

## 2. 前置知识

阅读本讲前，你应当已经了解（来自 [u1-l1](u1-l1-project-overview-and-mla.md)～[u1-l4](u1-l4-python-api-quickstart.md)）：

- **四类 kernel**：dense decode、sparse decode、sparse prefill、dense prefill（含 forward/backward）。
- **支持矩阵**：dense decode 仅 SM90、dense prefill 仅 SM100、sparse 在两架构都有。
- **Python 接口层**：`flash_mla/` 包导出的 6 个函数对应这四类 kernel。

本讲会引入几个新概念，先用一句话预热，后面结合源码细讲：

- **pybind11**：一个把 C++ 函数「暴露成 Python 可调用对象」的库。FlashMLA 用它把 C++ 接口函数注册成 `flash_mla.cuda` 模块上的方法。
- **CUDAExtension**：PyTorch 提供的构建辅助，把 `.cu/.cpp` 编译成一个 `.so`，加载后就是 Python 里的 `flash_mla.cuda`（详见 [u1-l2](u1-l2-build-and-install.md)）。
- **接口函数（interface function）**：写在 `csrc/api/*.h` 里的 C++ 函数，负责张量校验、参数装配、选 kernel 并启动。它是 Python 和 CUDA 之间的「中间层」。
- **kernel 命名空间**：真正启动 CUDA kernel 的 C++ 函数，按 `sm{90,100,xx}::<阶段>::...` 组织。命名空间名本身就编码了「这块代码跑在哪代 GPU、属于哪个阶段」。
- **派发（dispatch）**：根据运行时条件（架构、头数、head_dim）选择具体实现的过程。

## 3. 本讲源码地图

本讲横跨 Python 层与 C++ 层，涉及的关键文件如下：

| 文件 | 层次 | 作用 |
|---|---|---|
| [flash_mla/flash_mla_interface.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py) | Python 包装层 | 用户调用的函数，做轻量校验与参数整理，再调用 `flash_mla_cuda.*` |
| [csrc/api/api.cpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp) | pybind 桥梁 | 用 `PYBIND11_MODULE` 注册 5 个绑定，连接 Python 与 C++ 接口函数 |
| [csrc/api/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h) | 公共基础设施 | `Arch` 架构检测、`int64_stride_to_int` 溢出保护、`DISPATCH_*` 宏、`ImplBase` 派发基类 |
| [csrc/api/dense_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h) | dense decode 接口 | `dense_attn_decode_interface`：校验、装配 `DenseAttnDecodeParams`、直接调用 SM90 kernel |
| [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) | sparse decode 接口 | `sparse_attn_decode_interface` + 4 个 `Decode_*_Impl`，用 ImplBase 派发 |
| [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) | sparse prefill 接口 | `sparse_attn_prefill_interface` + 4 个 `Fwd_*_Impl`，用 ImplBase 派发 |
| [csrc/api/dense_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_fwd.h) | dense prefill 接口入口 | 仅 `#include` 真正的接口头文件 |
| [csrc/sm100/prefill/dense/interface.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/interface.h) | dense prefill 接口 | 声明 `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun` |

> 记忆要点：`csrc/api/` 下每个 `.h` 文件 = 一类 kernel 的接口层；`api.cpp` 把它们汇总注册成 5 个 Python 可见的绑定。

## 4. 核心概念与源码讲解

### 4.1 Python 包装层：从用户函数到 `flash_mla_cuda`

#### 4.1.1 概念说明

FlashMLA 的 Python 层是一个**薄壳**：它不参与任何数值计算，只做三件事——

1. **轻量校验与参数整理**：比如从 `indices` 的形状推出 `topk`、补默认的 `softmax_scale`、维护 `FlashMLASchedMeta` 的「首次初始化、后续复用」状态。
2. **二分派发**：根据是否传入 `indices`，决定走 sparse 还是 dense 解码路径。
3. **转交给扩展模块**：把所有张量和标量原样传给 `flash_mla_cuda` 上的对应方法，真正的校验和 kernel 启动都在 C++ 侧。

这个薄壳通过一行 import 拿到 C++ 扩展模块：

[flash_mla/flash_mla_interface.py:1-6](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L1-L6) —— 第 6 行 `import flash_mla.cuda as flash_mla_cuda`。`flash_mla.cuda` 就是 setup.py 编译出来的那个 `.so`（见 [u1-l2](u1-l2-build-and-install.md)），在 Python 里它表现为一个「模块对象」，其上的属性就是 pybind 注册的绑定。

#### 4.1.2 核心流程

以 `flash_mla_with_kvcache` 为例，Python 层的流程是：

```
flash_mla_with_kvcache(q, k_cache, block_table, cache_seqlens, ...)
   │
   ├─ 整理 topk / extra_topk / softmax_scale 等标量
   ├─ 维护 sched_meta（首次初始化，后续校验一致性）
   │
   ├─ if topk is not None:        # sparse 解码
   │     flash_mla_cuda.sparse_decode_fwd(...)
   │
   └─ else:                       # dense 解码
         flash_mla_cuda.dense_decode_fwd(...)
```

注意：**Python 层并不判断 GPU 架构**，也不判断 head_dim 是否被支持——这些都被推迟到 C++ 接口函数里。Python 只判断「有没有 `indices`」这一个语义层面的分支。

#### 4.1.3 源码精读

sparse 解码分支（第 151-160 行）在断言 `is_fp8_kvcache` 必须为真后，调用 `flash_mla_cuda.sparse_decode_fwd`：

[flash_mla/flash_mla_interface.py:151-160](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L151-L160) —— 把 `q / k_cache / indices / topk_length / attn_sink / sched_meta 里的两块张量 / extra_* / head_dim_v / softmax_scale` 按固定顺序传给绑定；返回 `out, lse, new_tile_scheduler_metadata, new_num_splits` 四元组。

dense 解码分支（第 165-170 行）走另一个绑定：

[flash_mla/flash_mla_interface.py:165-170](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L165-L170) —— 调用 `flash_mla_cuda.dense_decode_fwd`，参数列表更短（dense 不需要 `indices`）。

另两个 Python 函数也各自对应一个绑定：`flash_mla_sparse_fwd` 调 `sparse_prefill_fwd`（[第 208-210 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L208-L210)）；`_flash_attn_varlen_forward` / `_flash_attn_varlen_backward` 分别调 `dense_prefill_fwd`（[第 242-256 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L242-L256)）与 `dense_prefill_bwd`（[第 305-323 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L305-L323)）。

> 一个关键细节：第 171-172 行把 C++ 返回的 `new_tile_scheduler_metadata / new_num_splits` 写回 `sched_meta`。这就是「首次调用初始化、后续复用」模式的落点——Python 在第一次调用时把 `None` 传下去，C++ 侧分配好真实张量后回传，后续调用就复用这两块张量，避免每步解码都重新分配。

#### 4.1.4 代码实践

**实践目标**：在不实际运行 kernel 的前提下，验证 Python 层只是一个薄壳。

**操作步骤**：

1. 打开 `flash_mla_interface.py`，分别定位 5 处对 `flash_mla_cuda.*` 的调用点（sparse decode、dense decode、sparse prefill、dense prefill fwd、dense prefill bwd）。
2. 给每一处加一行注释，标注「这个调用点对应 api.cpp 里的哪个 `m.def`」。

**需要观察的现象**：你会发现 Python 层几乎没有 if-else 之外的业务逻辑，所有「重活」都在传参之后交给 C++。

**预期结果**：5 个调用点与 api.cpp 的 5 个绑定一一对应（见 4.2）。若你的 GPU 上没装好扩展，`import flash_mla.cuda` 会直接失败——这正好说明 Python 层完全依赖那个 `.so`。

> 待本地验证：若你已按 [u1-l2](u1-l2-build-and-install.md) 编译安装成功，可在 Python 里执行 `import flash_mla.cuda; print(dir(flash_mla.cuda))`，应能看到 5 个绑定方法名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `flash_mla_with_kvcache` 不在 Python 层判断 GPU 是 SM90 还是 SM100？

**参考答案**：因为 Python 层只负责语义分支（sparse vs dense），架构判断需要读取 CUDA 设备属性（`cudaDeviceProp`），这件事在 C++ 侧的 `Arch` 结构里做更自然、更高效，也便于在不支持的架构上立刻 `TORCH_CHECK` 报错。

**练习 2**：`flash_mla_with_kvcache` 的返回值是 `(out, lse)` 两个张量，但它调用的 `flash_mla_cuda.dense_decode_fwd` 却返回 4 个值，多出来的两个去哪了？

**参考答案**：多出来的 `new_tile_scheduler_metadata / new_num_splits` 被 Python 层写回了 `sched_meta`（第 171-172 行），用于后续解码步复用，不暴露给用户。

---

### 4.2 pybind 桥梁：`api.cpp` 的五个绑定

#### 4.2.1 概念说明

`api.cpp` 是整个 C++ 扩展里**最短、却最关键**的文件：它只有十几行，却定义了 Python 世界与 C++ 世界的「接缝」。它用 pybind11 的 `PYBIND11_MODULE` 宏注册一个模块，模块名就是 `TORCH_EXTENSION_NAME`（在 FlashMLA 里被 setup.py 设为 `flash_mla.cuda`）。

`m.def(名字, &C++函数)` 的语义是：「在模块 `m` 上创建一个叫 `名字` 的方法，调用它时执行这个 C++ 函数」。pybind11 会自动处理 Python 对象与 C++ 对象之间的类型转换——比如把 `torch.Tensor` 转成 `at::Tensor&`、把 Python `int` 转成 C++ `int`、把 `None` 转成 `std::optional<at::Tensor>` 的空值。

#### 4.2.2 核心流程

```
api.cpp
  │
  ├─ #include 四个接口头文件（dense_decode.h / sparse_decode.h / sparse_fwd.h / dense_fwd.h）
  │     → 这一步把 5 个接口函数的声明引入当前编译单元
  │
  └─ PYBIND11_MODULE(flash_mla.cuda, m)
        ├─ m.def("sparse_decode_fwd",  &sparse_attn_decode_interface)
        ├─ m.def("dense_decode_fwd",   &dense_attn_decode_interface)
        ├─ m.def("sparse_prefill_fwd", &sparse_attn_prefill_interface)
        ├─ m.def("dense_prefill_fwd",  &FMHACutlassSM100FwdRun)
        └─ m.def("dense_prefill_bwd",  &FMHACutlassSM100BwdRun)
```

注意 `dense_prefill_fwd / dense_prefill_bwd` 绑定的不是某个 `*_interface` 函数，而是 `FMHACutlassSM100FwdRun / FMHACutlassSM100BwdRun`——这两个函数声明在 CUTLASS 那一侧的 `interface.h` 里（见 4.4）。这说明 dense prefill 的接口层风格与其他三类不同，它直接复用了 CUTLASS 项目自带的接口入口。

#### 4.2.3 源码精读

整个文件的核心就是这一段：

[csrc/api/api.cpp:1-15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp#L1-L15) —— 第 3-6 行 include 四个接口头文件（`dense_fwd.h` 又会间接 include `sm100/prefill/dense/interface.h`，从而引入 `FMHACutlassSM100*Run`）；第 8 行的 `PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)` 是模块注册入口；第 10-14 行逐一注册 5 个绑定。

把这 5 个绑定与 4.1 里的 5 个 Python 调用点对齐，就得到一张**绑定对照表**：

| Python 调用 | 绑定名 (`m.def`) | 绑定的 C++ 函数 | 所在头文件 |
|---|---|---|---|
| `flash_mla_cuda.sparse_decode_fwd` | `sparse_decode_fwd` | `sparse_attn_decode_interface` | `sparse_decode.h` |
| `flash_mla_cuda.dense_decode_fwd` | `dense_decode_fwd` | `dense_attn_decode_interface` | `dense_decode.h` |
| `flash_mla_cuda.sparse_prefill_fwd` | `sparse_prefill_fwd` | `sparse_attn_prefill_interface` | `sparse_fwd.h` |
| `flash_mla_cuda.dense_prefill_fwd` | `dense_prefill_fwd` | `FMHACutlassSM100FwdRun` | `sm100/prefill/dense/interface.h` |
| `flash_mla_cuda.dense_prefill_bwd` | `dense_prefill_bwd` | `FMHACutlassSM100BwdRun` | `sm100/prefill/dense/interface.h` |

> 这张表是本讲最重要的「索引」。后面读任何一篇 kernel 讲义，只要知道用户调的是哪个 Python 函数，就能顺藤摸瓜定位到 C++ 接口函数，再找到 kernel 命名空间。

#### 4.2.4 代码实践

**实践目标**：亲手确认 pybind 注册的绑定名与 Python 调用名完全一致。

**操作步骤**：

1. 读 `csrc/api/api.cpp:10-14`，把 5 个绑定名抄下来。
2. 在 `flash_mla_interface.py` 里搜索 `flash_mla_cuda.`，核对每个调用点用的方法名是否与绑定名一字不差。
3. 注意参数顺序：Python 调用的实参顺序必须与 C++ 函数的形参顺序一致（pybind 按位置传参）。

**需要观察的现象**：绑定名与方法名严格相等；若哪天有人改了 `m.def` 的第一个参数而忘了改 Python 侧，运行时会报 `AttributeError` 或参数不匹配。

**预期结果**：5 个绑定名与 5 个 Python 调用点完全对齐，参数个数与顺序也一一对应。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `api.cpp` 里没有 `#include "common.h"`，但接口函数却能用 `Arch`、`TORCH_CHECK`？

**参考答案**：因为 `api.cpp` 直接 include 的是四个接口头文件（`dense_decode.h` 等），而这些头文件自己又 include 了 `common.h`、`<torch/extension.h>` 等。依赖关系是层层传递的，`api.cpp` 本身只需要知道 5 个接口函数的签名。

**练习 2**：如果我想新增一个 Python 可调用的 kernel 入口，需要改 `api.cpp` 的哪一行？

**参考答案**：需要新增一行 `m.def("新名字", &新的C++接口函数);`，并确保对应的接口头文件已被 include（或在新头文件里声明该函数并 include 它）。

---

### 4.3 接口头文件：校验、参数装配与两套派发风格

#### 4.3.1 概念说明

跨过 pybind 这层接缝，就进入 `csrc/api/*.h` 里的**接口函数**。每个接口函数都遵循同一个套路：

1. **架构检查**：用 `Arch arch = Arch();` 读取当前 GPU，判断是否支持。
2. **张量校验**：检查维度、dtype、设备、连续性、形状（用 `KU_CHECK_*` 系列宏）。
3. **参数装配**：把一堆 `at::Tensor` 的 `data_ptr()`、`stride()`、`size()` 填进一个 POD 参数结构（如 `DenseAttnDecodeParams`）。
4. **派发并启动 kernel**：根据架构 / 头数 / head_dim 选出具体实现，调用 kernel 命名空间里的启动函数。

这一节的重点是第 4 步里 FlashMLA 用的**两套截然不同的派发风格**：

- **风格 A：直接调用**。dense decode 和 dense prefill 用这种方式——接口函数里直接 `if (arch.is_sm90a()) { sm90::run_...<T>(params); }`，一个 if-else 搞定。
- **风格 B：基于 feature 集合的 ImplBase 派发器**。sparse decode 和 sparse prefill 用这种方式——把「实现 = 支持的 feature 集合」建模成类，运行时收集请求需要的 features，再让派发器挑一个能覆盖这些 features 的实现类。

风格 B 更灵活（一个请求可能同时需要 `HEAD_128 + HEAD_DIM_576 + ATTN_SINK + TOPK_LENGTH` 等多个 feature），代价是多了一层抽象。本讲只建立对两套风格的直观认识，细节（feature 校验、enum 名字反射）留到 [u2-l4](u2-l4-implbase-dispatcher.md)。

#### 4.3.2 核心流程

**风格 A（dense decode，直接调用）** 的流程：

```
dense_attn_decode_interface(...)
  ├─ Arch arch; if (!arch.is_sm90a()) TORCH_CHECK(false)   # 仅 SM90
  ├─ 校验 dtype / device / layout / shape
  ├─ 重排 q 的 head 维（详见 [u3-l4](u3-l4-dense-decode-interface.md)）
  ├─ 计算 num_sm_parts，分配 split-KV 缓冲（lse_accum / out_accum）
  ├─ 装配 DenseAttnDecodeParams params
  ├─ sm90::run_flash_splitkv_mla_kernel<T>(params)          # 启动主 kernel
  └─ smxx::decode::run_flash_mla_combine_kernel<T>(combine_params)  # 归并
```

**风格 B（sparse decode，ImplBase 派发）** 的流程：

```
sparse_attn_decode_interface(...)
  ├─ Arch arch; 校验
  ├─ 收集请求需要的 features 向量（HEAD_64/128, HEAD_DIM_*, ATTN_SINK, ...）
  ├─ 根据架构 + h_q + d_qk 选一个 Impl 类：
  │     arch.is_sm100f() + h_q==64   → Decode_Sm100_Head64_Impl
  │     arch.is_sm100f() + h_q==128  → Decode_Sm100_Head64x2_Impl 或 Head128_Impl
  │     arch.is_sm90a()              → Decode_Sm90_Impl
  ├─ impl->get_meta(h_q, s_q)   # 问实现要 num_sm_parts 等元数据
  ├─ 装配 SparseAttnDecodeParams params
  ├─ impl->run(params, features)  # 内部先校验 features，再调 run_
  │     └─ run_ 里再 DISPATCH_MODEL_TYPE / DISPATCH_NUM_HEADS 编译期化，最后启动 kernel
  └─ smxx::decode::run_flash_mla_combine_kernel<bf16>(combine_params)
```

两种风格都用到了 `common.h` 里的公共设施，所以先看 `common.h` 提供了什么。

#### 4.3.3 源码精读

**(a) 公共基础设施 `common.h`**

`Arch` 结构在构造时一次性读取当前 GPU 的全部信息：

[csrc/api/common.h:21-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L21-L41) —— 构造函数里调用 `at::cuda::getCurrentDeviceProperties()` 拿到 `major/minor/multiProcessorCount`；`is_sm90a()` 判断 `major==9 && minor==0`，`is_sm100f()` 判断 `major==10`。注意 SM100 只看 major，因为 Blackwell 的 minor 用于区分不同子型号，FlashMLA 统一当作 `sm100f` 处理。

`int64_stride_to_int` 把 PyTorch 的 int64 步长压成 int32，并在溢出时报错：

[csrc/api/common.h:44-49](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L44-L49) —— 这是必要的，因为 kernel 内部用 int32 算地址偏移，超大的 KV cache 步长可能溢出。

`ImplBase` 是风格 B 的核心，`run()` 方法先校验 features 再调 `run_`：

[csrc/api/common.h:226-230](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L226-L230) —— `run` 是对外入口，它调用 `check_if_all_features_are_supported_and_abort`（第 192-224 行，不满足时打印一份漂亮的「需要 vs 支持 vs 缺失」对照表后 abort），通过后才进入子类实现的 `run_`。这种「基类把关、子类干活」的设计让每个具体实现只关心自己擅长的那条路径。

**(b) 风格 A 实例：`dense_decode.h`**

架构检查只允许 SM90：

[csrc/api/dense_decode.h:26-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L26-L29) —— 这正是支持矩阵「dense decode 仅 SM90」在代码里的硬编码。

随后是密集的校验（dtype、layout、shape），再装配 `DenseAttnDecodeParams`（第 126-173 行），最后**直接**按 dtype 调用 SM90 主 kernel：

[csrc/api/dense_decode.h:175-185](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L175-L185) —— `sm90::run_flash_splitkv_mla_kernel<cutlass::bfloat16_t>(params)`；fp16 分支被 `FLASH_MLA_DISABLE_FP16` 宏守护。紧接着第 209-217 行调用 `smxx::decode::run_flash_mla_combine_kernel` 做 split-KV 归并。

注意 dense decode 没有任何「选实现类」的逻辑——它就是一个直球 `if (sm90a)` + dtype 分支，这是风格 A 的典型样子。

**(c) 风格 B 实例：`sparse_decode.h`**

先看 feature 枚举与 Impl 基类：

[csrc/api/sparse_decode.h:14-42](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L14-L42) —— `DecodeFeatures` 枚举列出了 sparse decode 关心的所有维度（头数、head_dim、KV cache 格式、attn_sink、topk_length、extra KV）；`DecodeImplBase` 用 `ImplBase<SparseAttnDecodeParams, DecodeFeatures>` 模板实例化出派发基类，并加了一个纯虚 `get_meta`。

每个具体实现类用 `DECLARE_SUPPORTED_FEATURES(...)` 声明自己支持哪些 feature。例如 SM90 实现声明支持**全部** feature：

[csrc/api/sparse_decode.h:44-76](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L44-L76) —— `Decode_Sm90_Impl` 的 `run_`（第 69-75 行）里嵌套了两个 `DISPATCH_*` 宏：先 `DISPATCH_MODEL_TYPE` 把运行时的 `params.model_type` 变成编译期常量 `MODEL_TYPE`，再 `DISPATCH_NUM_HEADS` 把 `params.h_q` 变成编译期常量 `NUM_HEADS`，最后才调 `sm90::decode::sparse_fp8::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE, NUM_HEADS>(params)`。

接口函数末尾的「选实现 + 运行」逻辑：

[csrc/api/sparse_decode.h:362-381](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L362-L381) —— 按 `arch.is_sm100f()` 优先、`arch.is_sm90a()` 其次选 Impl；SM100 + h_q==128 时还要按 `d_qk` 再分 `Head64x2`（V3.2 形状，把 head128 拆成两次 head64）与 `Head128`（MODEL1，复用 small_topk prefill kernel）。第 468 行 `impl->run(params, features)` 触发 ImplBase 的校验+派发流程；第 490 行调用公共的 combine kernel。

> 对比两段代码能看出风格差异：dense decode 的派发就是几个 `TORCH_CHECK` + 一个 if-else；sparse decode 的派发则是一组「实现类 + feature 集合」的对象模型。后者更复杂，但能干净地表达「某个实现支持 head128 但不支持 head_dim_576」这类组合约束（见 `Decode_Sm100_Head128_Impl` 的 feature 列表，[第 156-165 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L156-L165)）。

**(d) sparse prefill 的派发（`sparse_fwd.h`）**

结构完全平行：`FwdFeatures` 枚举 + 4 个 `Fwd_*_Impl` + 接口函数里按架构选实现。唯一特别的是 SM100 head128 路径有**两个候选实现**，需要运行时二选一：

[csrc/api/sparse_fwd.h:213-240](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L213-L240) —— 当 `topk <= 1280` 且 small_topk 实现支持所需 features 时优先用它，否则回退到普通 head128 实现。这种「双实现 + 阈值选择」的细节会在 [u6-l4](u6-l4-sparse-fwd-interface.md) 详讲，本讲只要知道它发生在接口层即可。

#### 4.3.4 代码实践

**实践目标**：用 `git grep` 把两套派发风格的调用点都找出来，体会它们的代码长相不同。

**操作步骤**：

1. 在仓库根目录执行 `git grep "impl->run("`，观察哪些接口文件用了 ImplBase 风格（应得到 sparse_decode.h、sparse_fwd.h）。
2. 执行 `git grep "run_flash_splitkv_mla_kernel"`，观察 dense decode 是如何在接口函数里**直接**调用 kernel 命名空间函数的（风格 A）。
3. 对比两类调用点周围的代码：风格 A 周围是一串 dtype if-else；风格 B 周围是 `new Decode_*_Impl()` + `features.push_back(...)`。

**需要观察的现象**：风格 A 的 kernel 调用紧贴在参数装配之后；风格 B 的 kernel 调用藏在 Impl 子类的 `run_` 里，接口函数只看到 `impl->run(...)`。

**预期结果**：能清晰区分「接口函数直接调 kernel」与「接口函数调 Impl 派发器、派发器再调 kernel」两种结构。

#### 4.3.5 小练习与答案

**练习 1**：`Arch` 的 `is_sm100f()` 只判断 `major == 10`，忽略 minor。这样设计有什么好处和风险？

**参考答案**：好处是简单——Blackwell 不同子型号（如 GB200 的不同配置）都按 `sm100f` 统一处理，避免为每个 minor 写分支；风险是若未来出现 major==10 但能力差异较大的子型号，可能需要细化判断。目前 FlashMLA 的 SM100 kernel 对所有 major==10 的卡都用同一份代码，所以这是合理简化。

**练习 2**：为什么 sparse decode 用 ImplBase 派发，而 dense decode 用直接调用？

**参考答案**：sparse decode 的实现空间大得多——架构（SM90/SM100）× 头数（64/128）× head_dim（512/576）× 各种可选 feature（attn_sink/topk_length/extra KV），而且不同实现支持的能力子集不同（如 SM100 head128 不支持 head_dim_576）。ImplBase 的「实现 = feature 集合」模型能干净表达这些组合约束并自动校验；dense decode 只有 SM90 一条路、约束简单，直接调用更直观。

---

### 4.4 kernel 命名空间地图：每个绑定最终落到哪

#### 4.4.1 概念说明

接口层做完校验和派发后，最终都会调用一个**kernel 命名空间里的启动函数**。这些函数名通常以 `run_` 开头（如 `run_flash_splitkv_mla_kernel`），内部负责计算 grid/block、设置 shared memory、真正发起 `kernel<<<...>>>` 调用（kernel 本体的精读留给 u3～u7）。

FlashMLA 的命名空间编码了「架构 + 阶段 + 子分类」三层信息，读懂命名空间就等于读懂了这块代码的归属：

- `sm90::` —— Hopper 专用（dense decode、sparse decode、sparse prefill 都有 SM90 版）。
- `sm100::` —— Blackwell 专用。
- `smxx::decode::` —— 两架构共用的解码辅助 kernel（tile scheduler 元数据、split-KV combine），与具体架构无关。

#### 4.4.2 核心流程

把 4.1～4.3 串起来，得到一张**端到端落地表**（本讲的综合地图）：

| Python 函数 | 绑定 → 接口函数 | 主 kernel 命名空间::函数 | 辅助 kernel |
|---|---|---|---|
| `flash_mla_with_kvcache`（dense） | `dense_decode_fwd` → `dense_attn_decode_interface` | `sm90::run_flash_splitkv_mla_kernel<T>` | `smxx::decode::run_get_decoding_sched_meta_kernel` + `smxx::decode::run_flash_mla_combine_kernel<T>` |
| `flash_mla_with_kvcache`（sparse） | `sparse_decode_fwd` → `sparse_attn_decode_interface` | `sm90::decode::sparse_fp8::run_flash_splitkv_mla_fp8_sparse_kernel<M,N>` **或** `sm100::decode::head64::run_flash_splitkv_mla_fp8_sparse_kernel<M>` **或** `sm100::fwd_for_small_topk::head128::run_fwd_for_small_topk_phase1_kernel<...>` | 同上两个 `smxx::decode::` 辅助 kernel |
| `flash_mla_sparse_fwd` | `sparse_prefill_fwd` → `sparse_attn_prefill_interface` | `sm90::fwd::run_fwd_phase1_kernel<D,T>` **或** `sm100::fwd::head64::run_fwd_phase1_kernel<D>` **或** `sm100::fwd::head128::run_fwd_phase1_kernel<D>` **或** `sm100::fwd_for_small_topk::head128::run_fwd_for_small_topk_phase1_kernel<...>` | 无（prefill 不做 split-KV） |
| `flash_attn_varlen_func`（forward） | `dense_prefill_fwd` → `FMHACutlassSM100FwdRun` | SM100 dense prefill CUTLASS device kernel（在 `csrc/sm100/prefill/dense/` 下） | 无 |
| `flash_attn_varlen_func`（backward） | `dense_prefill_bwd` → `FMHACutlassSM100BwdRun` | SM100 dense prefill CUTLASS backward kernel | 无 |

几点值得记住的规律：

- **decode 路径都配两个辅助 kernel**：先 `get_decoding_sched_meta`（把 batch 均衡切给各 SM），主 kernel 跑完 split-KV 后再 `combine` 归并。这条「sched_meta → 主 kernel → combine」三段式是所有解码路径的共性（详见 Unit 4）。
- **prefill 路径没有 combine**：因为 prefill 的 Q 序列较长，单次就能覆盖足够多的 KV，不需要 split-KV。
- **dense prefill 走 CUTLASS**：它的接口函数（`FMHACutlassSM100*Run`）不是 `csrc/api/` 里的薄封装，而是 CUTLASS 那套 device/kernel/collective 多层结构的最外入口（详见 [u7-l1](u7-l1-cutlass-integration.md)）。这也是为什么 `dense_fwd.h` 只有一行 `#include "sm100/prefill/dense/interface.h"`。

#### 4.4.3 源码精读

下面把上表中几条关键的「接口函数 → kernel 命名空间」连接，回指到真实代码行：

- **dense decode 主 kernel**：`dense_decode.h` 第 176 行 `sm90::run_flash_splitkv_mla_kernel<cutlass::bfloat16_t>(params)`，函数声明见 [csrc/sm90/decode/dense/splitkv_mla.h:8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.h#L8)（命名空间为 `sm90::`）。
- **dense decode 辅助 kernel（sched_meta）**：`dense_decode.h` 第 113 行 `smxx::decode::run_get_decoding_sched_meta_kernel(...)`，声明见 [csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.h:7](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.h#L7)。
- **dense decode 辅助 kernel（combine）**：`dense_decode.h` 第 210 行 `smxx::decode::run_flash_mla_combine_kernel<...>(...)`，声明见 [csrc/smxx/decode/combine/combine.h:8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.h#L8)。
- **sparse decode 主 kernel（SM90 分支）**：`sparse_decode.h` 第 72 行 `sm90::decode::sparse_fp8::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE, NUM_HEADS>(params)`，声明见 [csrc/sm90/decode/sparse_fp8/splitkv_mla.h:5-8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.h#L5-L8)（命名空间 `sm90::decode::sparse_fp8::`）。
- **sparse decode 主 kernel（SM100 分支）**：`sparse_decode.h` 第 104、149 行 `sm100::decode::head64::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE>(...)`，声明见 [csrc/sm100/decode/head64/kernel.h:5-8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head64/kernel.h#L5-L8)。
- **sparse prefill 主 kernel（SM90）**：`sparse_fwd.h` 第 44 行 `sm90::fwd::run_fwd_phase1_kernel<HEAD_DIM_QK, HAVE_TOPK_LENGTH>(params)`，声明见 [csrc/sm90/prefill/sparse/phase1.h:5-8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.h#L5-L8)。
- **dense prefill fwd/bwd 入口**：`FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun` 声明在 [csrc/sm100/prefill/dense/interface.h:5-14](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/interface.h#L5-L14)，实现分别在 [csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu:31](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L31) 与 [csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu:29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L29)。

> 阅读建议：现在不必钻进任何一个 kernel 本体。只要记住「接口函数末尾那个 `sm*::run_*` 调用就是 CUDA kernel 的启动入口」即可。后续每个 Unit 都会从某个 `run_*` 函数开始往下钻。

#### 4.4.4 代码实践

**实践目标**：画出本讲规格要求的那张调用链图——把 5 个绑定逐一连到 C++ 接口函数与最终 kernel 命名空间。

**操作步骤**：

1. 准备一张白纸或文本文件，左边列出 5 个绑定：`dense_decode_fwd`、`sparse_decode_fwd`、`sparse_prefill_fwd`、`dense_prefill_fwd`、`dense_prefill_bwd`。
2. 对每个绑定，从 `api.cpp` 出发，画一个箭头到它绑定的接口函数（参照 4.2.3 的对照表）。
3. 再从每个接口函数画箭头到它最终调用的 `sm*::run_*` 命名空间（参照 4.4.2 的落地表）。对 sparse 的两个绑定，要画出「Impl 派发器」这个中间节点，并标注它会分裂到多个实现类。
4. 用不同颜色标注两类辅助 kernel：`smxx::decode::run_get_decoding_sched_meta_kernel`（仅 decode）和 `smxx::decode::run_flash_mla_combine_kernel`（仅 decode）。

**需要观察的现象**：dense decode 和 sparse decode 都有「三段式」（sched_meta + 主 kernel + combine）；sparse prefill 只有主 kernel；dense prefill 直接是 CUTLASS 入口、且只有 SM100 一条路。

**预期结果**：得到一张与 4.4.2 落地表一致的调用链图。图中每个箭头都能在源码里指出具体行号（如 dense_decode.h:176）。

> 待本地验证：若你想动态核对这张图，可在 C++ 接口函数的关键调用点（如 `dense_decode.h:176` 的 `run_flash_splitkv_mla_kernel` 前）临时加一行 `printf`，重新编译后跑一个最小示例，观察输出顺序是否符合「sched_meta → 主 kernel → combine」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `smxx::decode::` 下的 combine 和 sched_meta kernel 用 `smxx`（xx）而不是 `sm90` 或 `sm100`？

**参考答案**：因为这两个辅助 kernel 与具体架构无关——它们只做 batch 均衡切分和跨 split 的归并，不依赖 Hopper/Blackwell 专有指令（如 WGMMA、TMA cluster）。所以写成「两架构共用」的一份代码，由 decode 主 kernel（架构相关）调用。`xx` 表示「跨架构」。

**练习 2**：用户调用 `flash_attn_varlen_func` 做前向，调用链上会出现 `combine` kernel 吗？

**参考答案**：不会。`flash_attn_varlen_func` 走的是 dense prefill（`dense_prefill_fwd`），prefill 路径不做 split-KV，因此没有 combine 归并步骤。只有 decode 路径（dense/sparse decode）才需要 combine。

## 5. 综合实践

把本讲的所有最小模块串起来，完成一份**「调用链审计报告」**。

任务：假设有用户报告「我在 SM90 上调用 `flash_mla_with_kvcache`，传入 `indices`，结果报错」。请你基于本讲建立的调用链，写出这份调用的完整路径推测报告，要求包含：

1. **Python 层分支**：因为传了 `indices`，`flash_mla_with_kvcache` 会走哪个 if 分支、调用哪个绑定？（引用 `flash_mla_interface.py` 的行号）
2. **pybind 层**：这个绑定在 `api.cpp` 里注册到哪个 C++ 接口函数？
3. **接口层派发**：在 `sparse_attn_decode_interface` 里，SM90 架构会被分到哪个 Impl 类？（引用 `sparse_decode.h` 的行号）
4. **kernel 落地**：这个 Impl 类的 `run_` 最终调用哪个 kernel 命名空间函数？会不会调用 combine？
5. **可能的报错点**：如果 `indices` 非法（比如 sparse 解码但 `is_fp8_kvcache=False`），错误最可能在哪一层被拦截？（提示：看 `flash_mla_interface.py:154` 的断言）

参考要点：

- 第 1 步：`flash_mla_interface.py:151-160`，走 sparse 分支，调 `flash_mla_cuda.sparse_decode_fwd`。
- 第 2 步：`api.cpp:10`，绑定到 `sparse_attn_decode_interface`。
- 第 3 步：`sparse_decode.h:377-378`，SM90 选 `Decode_Sm90_Impl`。
- 第 4 步：`sparse_decode.h:72`，`sm90::decode::sparse_fp8::run_flash_splitkv_mla_fp8_sparse_kernel`；之后 `sparse_decode.h:490` 调 `smxx::decode::run_flash_mla_combine_kernel`。
- 第 5 步：`is_fp8_kvcache=False` 会在 Python 层 `flash_mla_interface.py:154` 的 `assert is_fp8_kvcache` 处直接抛 `AssertionError`，根本到不了 C++。

完成这份报告后，你就真正掌握了「给一个 Python 调用，预测它会走完整条链的哪一条分支、最终启动哪些 kernel」的能力——这正是阅读后续 kernel 讲义前必须具备的全局观。

## 6. 本讲小结

- FlashMLA 的调用链是分层的：**Python 包装函数（薄壳）→ pybind 绑定（api.cpp 的 5 个 `m.def`）→ C++ 接口函数（`csrc/api/*.h`）→ kernel 命名空间（`sm*::run_*`）**。
- `flash_mla.cuda` 扩展模块只暴露 **5 个绑定**：`dense_decode_fwd`、`sparse_decode_fwd`、`sparse_prefill_fwd`、`dense_prefill_fwd`、`dense_prefill_bwd`，每个绑定一一对应一个 C++ 接口函数。
- 接口函数统一做「架构检查 → 张量校验 → 参数装配 → 派发启动」，但派发有两种风格：dense 路径用**直接调用**（一个 if-else），sparse 路径用**基于 feature 集合的 ImplBase 派发器**（实现 = 支持的 feature 子集）。
- kernel 命名空间编码了归属：`sm90::`/`sm100::` 是架构专用，`smxx::decode::` 是两架构共用的解码辅助 kernel（sched_meta + combine）。
- 所有 decode 路径都是「sched_meta → 主 kernel → combine」三段式；prefill 路径只有主 kernel；dense prefill 直接走 CUTLASS 的 `FMHACutlassSM100*Run` 入口。
- `common.h` 是接口层的公共地基：`Arch` 做架构检测、`int64_stride_to_int` 防溢出、`DISPATCH_*` 宏把运行时值编译期化、`ImplBase` 提供派发与 feature 校验（后者详见 u2-l4）。

## 7. 下一步学习建议

本讲建立了全局调用图，下一步可以从两个方向深入：

1. **横向打地基（推荐先走）**：继续 Unit 2 的剩余讲义——[u2-l2](u2-l2-params-structs.md)（参数结构 `params.h`）、[u2-l3](u2-l3-arch-and-dispatch-macros.md)（`DISPATCH_*` 宏与架构检测）、[u2-l4](u2-l4-implbase-dispatcher.md)（ImplBase 派发器细节）。它们把本讲里一笔带过的 `params`、`DISPATCH_*`、`ImplBase` 讲透，是读懂所有接口函数的前提。
2. **纵向钻 kernel（打完地基后）**：选一个最感兴趣的 kernel 家族深入。建议从 Unit 3 的 [u3-l1](u3-l1-compute-bound-analysis.md) 开始，沿「理论 → config → seesaw 调度 → 接口编排」逐层钻进 SM90 dense decode 主 kernel（即本讲里的 `sm90::run_flash_splitkv_mla_kernel`）。

阅读源码时，推荐把本讲的「4.4.2 落地表」常备手边——任何时候读到某个 `run_*` 函数，都可以回这张表确认它在调用链里的位置。
