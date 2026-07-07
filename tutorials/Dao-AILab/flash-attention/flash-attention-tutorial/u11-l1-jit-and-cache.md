# JIT 编译与缓存机制

## 1. 本讲目标

本讲回答一个贯穿 FA4 全部使用场景的问题：**为什么 `flash_attn_func` 第一次调用很慢、之后却几乎“秒回”？kernel 到底什么时候被编译、又把编译结果藏在了哪里？**

学完后你应当能够：

- 说清 FA4 的 CuTeDSL kernel 从「一段 Python 代码」到「GPU 上可执行的字节码」经历了哪几步，以及它和 FA2「安装期 `nvcc` 编译」的根本区别。
- 逐项说出 `compile_key` 缓存键由哪些字段构成、哪些字段改了会触发重编译、哪些不会，并理解源码指纹 `_compute_source_fingerprint` 如何在“你改了源码”时自动让旧缓存失效。
- 看懂两级缓存（进程内 `dict` + 可选磁盘持久化）与文件锁的协作，掌握 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED`、`FLASH_ATTENTION_FAKE_TENSOR` 等环境变量各自的作用。

本讲是「编译、测试、基准与调试」单元的第一篇，承接 [u2-l1](u2-l1-public-api.md) 讲过的公共 API 与 `compile_key` 概念，为后续 [u11-l2 Constexpr 特化](u11-l2-constexpr-specialization.md)、[u11-l3 测试体系](u11-l3-tests-and-reference.md)、[u11-l5 GPU 调试](u11-l5-debugging-ptx-sass.md) 打下编译侧的基础。

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（前几讲已建立）：

- **FA4 = Python + CuTeDSL**：FA4 的 kernel 不是 C++/CUDA，而是写在 `flash_attn/cute/*.py` 里的 Python 代码，用 CuTeDSL（NVIDIA CUTLASS DSL）描述 tile、MMA、流水线等，再在**运行时**编译成 GPU 机器码。详见 [u1-l2 仓库结构](u1-l2-repo-structure.md) 与 [u1-l4 FA2 vs FA4](u1-l4-fa2-vs-fa4-coexistence.md)。
- **`flash_attn_func` 的调用链**：用户函数 → `FlashAttnFunc.apply`（autograd Function）→ `_flash_attn_fwd`。kernel 的编译与启动都发生在 `_flash_attn_fwd` 内部。详见 [u2-l1](u2-l1-public-api.md)。
- **`compile_key`**：一个决定“是否需要重新编译”的元组，已经在 [u2-l1](u2-l1-public-api.md) 里初识。本讲会把它彻底拆开。
- **`Constexpr` / `@cute.jit`**：编译期常量与可编程回调（`score_mod`/`mask_mod`），它们会被“内联”进 kernel。本讲解释**为什么**换一个回调就会重编译（因为它的哈希进了缓存键）。

补充几个本讲用到的基础术语：

- **PTX**：NVIDIA 的并行线程执行中间指令集（一种类汇编的文本）。GPU 源码先编译到 PTX，再由 `ptxas` 汇编成特定架构的 **CUBIN**（CUDA binary，GPU 机器码）。
- **JIT（Just-In-Time）**：运行时编译，相对 AOT（Ahead-Of-Time，安装/构建期编译）。FA2/FA3 是 AOT（`pip install` 时 `nvcc` 编译），FA4 是 JIT（运行时编译）。
- **`enable_tvm_ffi`**：CuTeDSL 的一个编译选项（`--enable-tvm-ffi`），让编译产物导出为可被 TVM FFI 调用的 C 函数。FA4 的磁盘缓存依赖这个选项。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`flash_attn/cute/cache_utils.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py) | **本讲主角之一**。定义两级缓存类 `JITCache`（内存）、`JITPersistentCache`（内存+磁盘）、工厂 `get_jit_cache`、源码指纹 `_compute_source_fingerprint`、文件锁 `FileLock`，以及控制开关的环境变量。 |
| [`flash_attn/cute/cute_dsl_utils.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_utils.py) | 编译侧的“胶水”工具：torch↔cute 张量转换、对齐假设、广播维探测、可哈希类型集合 `StaticTypes`，并保存了被 patch 过的原始 `cute.compile` / `load_cubin_module_data` 引用。 |
| [`flash_attn/cute/interface.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 公共 API 与编译调度。这里组装 `compile_key`、调用 `cute.compile`、按 `is_fake_mode()` 决定是否真正启动 kernel，并为每个 kernel 挂一个 `compile_cache`。 |
| [`flash_attn/cute/utils.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py) | `hash_callable` / `_compute_base_hash`：把 `score_mod`/`mask_mod` 这类 Python 回调的**源码**哈希成字符串，作为缓存键的一部分。 |
| [`flash_attn/cute/fa_logging.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py) | 统一日志（`FA_LOG_LEVEL`）。缓存命中/未命中、磁盘导入/导出的提示都走这里的 `fa_log`。 |
| [`flash_attn/cute/cute_dsl_ptxas.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py) | 可选的“用系统 `ptxas` 替换内嵌 `ptxas`”补丁，与 PTX/SASS 调试相关（本讲略提，详见 u11-l5）。 |

---

## 4. 核心概念与源码讲解

### 4.1 CuTeDSL 的 JIT 编译流程：从 Python 到 CUBIN

#### 4.1.1 概念说明

FA4 最反直觉的一点是：**它的 kernel 是 Python**。`FlashAttentionForwardSm80`、`Softmax`、`BlockInfo` 这些类就躺在 `flash_attn/cute/*.py` 里，和普通业务代码没有区别。但 GPU 不能直接执行 Python，于是 CuTeDSL 在**你调用 `flash_attn_func` 的那一刻**，把这段 Python 翻译成 GPU 机器码。

这与 FA2/FA3 的 AOT 模式截然不同：

| 维度 | FA2 / FA3（AOT） | FA4（JIT） |
| --- | --- | --- |
| kernel 语言 | C++ / CUDA | Python + CuTeDSL |
| 编译时机 | `pip install` 时由 `nvcc` 编译 | 运行时首次调用时编译 |
| 编译产物 | 预编译的 `.so`（如 `flash_attn_2_cuda`） | 进程内函数对象 / 磁盘 `.o` |
| 安装耗时 | 慢（可能几十分钟） | 极快（纯 Python，不调用 `nvcc`） |
| 首次运行 | 快（已编译） | 慢（现场编译） |
| 后续运行 | 快 | 命中缓存后快 |

正因为编译发生在运行时，FA4 才能做到“安装秒装、第一次调用慢、之后秒回”——而“之后秒回”正是本讲要拆解的缓存机制的功劳。

#### 4.1.2 核心流程

把一次 `flash_attn_func` 调用拆开，编译相关的步骤是：

```
flash_attn_func(q, k, v, causal=True)
        │
        ▼
_flash_attn_fwd(...)
        │
   ① 组装 compile_key（dtype/hdim/causal/... 一大串）
        │
   ② if compile_key not in cache:        ← 缓存查找
        │       是 miss：
        │       ③ cute.compile(kernel, tensors..., "--enable-tvm-ffi")
        │            └─ CuTeDSL：Python → PTX → ptxas → CUBIN → 注册到 GPU
        │       ④ cache[compile_key] = 编译出的函数对象
        │            └─ 若开启磁盘缓存：同时 export_to_c 导出到磁盘
        │
   ⑤ if not is_fake_mode():              ← fake 模式下不启动
        │       cache[compile_key](*真实张量)   ← 真正在 GPU 上跑
```

两个关键设计：

1. **编译与执行分离**：步骤 ②③④（编译）无条件发生，步骤 ⑤（启动）受 `is_fake_mode()` 守卫。这意味着你可以在**没有 GPU、不分配显存**的情况下触发编译——这就是“免 GPU 编译”的基础（见 4.1.4）。
2. **编译一次、调用多次**：编译出的函数对象被存进 `cache[compile_key]`，后续相同配置的调用直接走步骤 ⑤，跳过昂贵的 ②③④。

编译产物内部链路（由 CuTeDSL 完成，FA4 只调用 `cute.compile`）：Python kernel → 生成 PTX 文本 → `ptxas` 汇编为 CUBIN 字节码 → `cudaLibraryLoadData` 把 CUBIN 注册成 GPU 可调用模块。FA4 用 `--enable-tvm-ffi` 选项让产物额外导出为带统一 C ABI 的函数，这样它既能被 TVM FFI 直接调用，也能用 `export_to_c` 序列化到磁盘。

#### 4.1.3 源码精读

编译调度的“指挥中心”在 `_flash_attn_fwd` 内。先看缓存查找与编译这一段（注意它在 `if compile_key not in _flash_attn_fwd.compile_cache:` 守卫之内）：

[`interface.py:769`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L769)（缓存未命中才进入编译分支）

紧随其后是真正的编译调用，对标准（非 MLA）前向：

[`interface.py:1017-1019`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1017-L1019) —— 调用 `cute.compile(*compile_args, options="--enable-tvm-ffi")`，把 kernel 类 + 一组 cute 张量 + 标量参数交给 CuTeDSL 编译，结果写回 `compile_cache[compile_key]`。

而 kernel 的**启动**被单独包在一个 fake-mode 守卫里：

[`interface.py:1021`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1021)（`if not is_fake_mode():` 之后才用真实张量调用 kernel）

这正是“编译发生、但不执行”的开关。`is_fake_mode()` 来自 [`testing.py:486-487`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L486-L487)，它通过 `active_fake_mode()` 判断当前是否处于 PyTorch 的 `FakeTensorMode` 上下文。

每个 kernel 都有自己的缓存实例，在模块加载时一次性创建（注意它是函数对象的属性，进程级单例）：

[`interface.py:1104`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1104) —— `_flash_attn_fwd.compile_cache = get_jit_cache("fwd")`。同理反向、combine、preprocess、postprocess 各有一个，名字分别是 `"bwd"`、`"fwd_combine"`、`"bwd_pre"`、`"bwd_post"` 等（见 [`interface.py:1242`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1242)、[`interface.py:1291`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1291)、[`interface.py:2004`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2004)、[`interface.py:2999`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2999)）。这个 `name` 只是磁盘缓存下的子目录名，用来把不同 kernel 的产物隔开。

> 一个易混点：`cute_dsl_utils.py` 顶部保存了 `cute_compile_og = cute.compile` 和 `load_cubin_module_data_og`（[`cute_dsl_utils.py:21-22`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_utils.py#L21-L22)）。这是为了在需要 dump SASS 时能 patch 掉原始的 `cute.compile` / CUBIN 加载函数，而仍保留 `_og` 原版做回退。普通使用下 `cute.compile` 就是上游原版。

#### 4.1.4 代码实践：用 FakeTensorMode 体验“免 GPU 编译”

**实践目标**：验证“编译与执行分离”——在 `FakeTensorMode` 下，`flash_attn_func` 会编译 kernel、填充缓存，但**不分配显存、不执行**。

**操作步骤**：

```python
# fake_compile_demo.py —— 不需要真实 GPU 即可编译（但 import 仍需 torch+cutlass-dsl 环境）
import torch
from torch._subclasses.fake_tensor import FakeTensorMode
import flash_attn.cute as fa4  # FA4 入口

q = torch.randn(2, 512, 8, 64, dtype=torch.float16, device="cuda")
k = torch.randn(2, 512, 8, 64, dtype=torch.float16, device="cuda")
v = torch.randn(2, 512, 8, 64, dtype=torch.float16, device="cuda")

with FakeTensorMode():
    out, lse = fa4.flash_attn_func(q, k, v, causal=True)
    print("compile_cache 大小:", len(fa4.interface._flash_attn_fwd.compile_cache.cache))
```

**需要观察的现象**：

1. 进入 `FakeTensorMode` 后，`q/k/v` 被替换为只携带“形状/dtype/对齐”元数据的 FakeTensor。
2. `_flash_attn_fwd` 仍然走到 `cute.compile`（步骤 ③），但 `is_fake_mode()` 为真，所以**跳过**步骤 ⑤（不调用真实 kernel）。
3. `compile_cache` 里多出一个条目，证明编译确实发生了。

**预期结果**：在不消耗 GPU 计算资源的前提下，缓存被填充。CI 正是利用这一点做“两段式测试”——Pass 1 用大量 worker 并行编译（`FLASH_ATTENTION_FAKE_TENSOR=1`，无需 GPU），Pass 2 用缓存好的产物在 GPU 上跑（`FLASH_ATTENTION_FAKE_TENSOR=0`）。详见 [`tools/ci/run_fa4_ci.py:100-112`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tools/ci/run_fa4_ci.py#L100-L112)。

> **待本地验证**：上述脚本能否完全脱离 GPU 运行取决于 torch/cutlass-dsl 是否在校验阶段要求一个 CUDA 设备。若无 GPU 环境，可改为阅读 `tests/cute/test_flash_attn.py:80` 处 `USE_FAKE_TENSOR = int(os.getenv("FLASH_ATTENTION_FAKE_TENSOR", 0)) == 1` 与 [`testing.py:468`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L468) 的 `maybe_fake_tensor_mode` 装饰器，理解测试如何靠环境变量在两种模式间切换。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FA4 “安装很快但首次调用很慢”，而 FA2 恰好相反？

> **答案**：FA4 是纯 Python 包，`pip install` 不调用 `nvcc`，所以安装快；但 kernel 要在运行时 JIT 编译，首次调用慢。FA2 在安装期就用 `nvcc` 把 C++/CUDA 编译成 `.so`，所以安装慢、首次调用快。

**练习 2**：如果把 `interface.py:1021` 的 `if not is_fake_mode():` 守卫去掉，在 FakeTensorMode 下会发生什么？

> **答案**：编译仍正常完成，但随后会用 FakeTensor 去真正启动 kernel。FakeTensor 不携带真实数据指针，启动会报错或产生无意义结果。守卫的意义就是“只编译、不执行”。

---

### 4.2 缓存键（compile_key）与源码指纹

#### 4.2.1 概念说明

JIT 编译很贵（单个 kernel 可能要数秒到十几秒）。要让“第二次调用秒回”，就必须**记住**之前编过的产物，并在下次用**完全相同的配置**时直接复用。这个“配置”的规范化表达，就是 `compile_key`。

`compile_key` 必须满足一个硬约束：**两个 key 相等 ⟺ 编译出的 kernel 行为完全一致**。换句话说：

- 任何会改变 kernel **生成代码**的因素（dtype、head_dim、causal、tile 尺寸、架构、`score_mod` 源码……）都必须进 key。
- 任何只改变**运行期数值**而不改变生成代码的因素（`softmax_scale` 的具体值、`window_size` 的具体像素数、输入张量的 batch/seqlen）都**不应**进 key——否则缓存永远命中不了。

这条线画在哪里，直接决定了“换什么参数会触发重编译”。FA4 把它画得很精细：`softmax_scale` 不进 key（它只是个乘到分数上的标量），但 `causal` 进 key（它通过 `const_expr` 改变 kernel 里保留/裁剪了哪些分支）。

#### 4.2.2 核心流程

`compile_key` 是一个很长的元组（`tuple[Hashable, ...]`），大致分六类：

```
compile_key = (
    # ① 数值精度与形状常量
    dtype, head_dim, head_dim_v, qhead_per_kvhead,
    # ② 掩码/打分类开关（注意很多是“是否提供”的布尔，而非具体值）
    causal, score_mod_hash, mask_mod_hash, use_block_sparsity,
    block_sparse_broadcast_pattern, aux_tensor_metadata, aux_scalar_metadata,
    lse is None, cu_seqlens_q is None, cu_seqlens_k is None,
    seqused_q is None, seqused_k is None, page_table is not None,
    window_size_left is not None, window_size_right is not None,
    learnable_sink is not None, q_descale is not None, k_descale is not None,
    v_descale is not None, block_sparse_tensors ... is None (×2),
    # ③ tile / 线程 / 流水级数（编译期特化参数）
    tile_m, tile_n, q_stage, num_threads,
    # ④ 功能开关
    is_split_kv, pack_gqa, arch, page_size not in [None, tile_n],
    use_2cta_instrs, q_subtile_factor, mma_pv_is_rs, intra_wg_overlap,
    use_clc_scheduler,
    # ⑤ MLA / top-k 等扩展路径的存在性
    q is not None, qv is not None, p is not None, row_max is not None,
    gather_kv_length, sparse_kv, disable_sparse_kv_bitmask,
    # ⑥ 日志级别（因为它改变 kernel 里保留了多少 printf）
    fa_logging.get_fa_log_level(),
)
```

观察几个关键设计：

- **`causal` 是布尔值本身**，而 **`window_size_left/right` 只看“是否为 `None`”**。这意味着把 `window_size_left` 从 128 改成 256 **不会**重编译（窗口大小只是运行期参数），但把 `causal` 从 `False` 改成 `True` **会**重编译。
- **`score_mod_hash` / `mask_mod_hash`** 是用户回调的**源码哈希**（字符串），不是回调对象本身。换一个 `score_mod`（哪怕只是改了 `ALiBi` 的斜率常数）就会得到不同哈希 → 不同 key → 重编译。这解释了 [u4-l2](u4-l2-score-mod.md) 里“换回调即重编译”的结论。
- **`arch`** 进 key：不同 GPU 架构编译出不同的 CUBIN，必须分开存。
- **`fa_logging.get_fa_log_level()`** 进 key：日志级别通过 `const_expr` 决定 kernel 里保留多少 `cute.printf`（见 [`fa_logging.py:21-26`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py#L21-L26) 的注释），所以改日志级别也要重编译。

除了 `compile_key`（区分**不同 kernel 配置**），还有一个**源码指纹** `_compute_source_fingerprint`：它区分的是“**源码本身变了**”（你改了 `flash_attn/cute/*.py`、升级了 cutlass、换了 Python 版本）。指纹作用在磁盘缓存的**目录层**，让源码一变，整个旧目录作废。

#### 4.2.3 源码精读

先看 `compile_key` 的完整构造（六类字段都在这里）：

[`interface.py:720-767`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L720-L767) —— 注意末尾的 `fa_logging.get_fa_log_level()`，以及大量 `... is None` / `... is not None` 形式的布尔字段。

再看 `score_mod`/`mask_mod` 的哈希怎么来的：

[`interface.py:615-616`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L615-L616) —— `utils.hash_callable(score_mod) if score_mod is not None else False`。注意没传回调时用的是布尔 `False`（一个确定的哈希占位），保证 key 始终可比。

`hash_callable` 的核心是把 Python 回调的**源码**喂给 SHA-256：

[`utils.py:102-118`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L102-L118) —— `_compute_base_hash`：优先用 `inspect.getsource(func)`（拿到回调的源代码文本），取不到源码才退回到字节码 `co_code`；再把闭包里捕获的自由变量（如 `softcap_val`）的 `repr` 一并混入。这正是“改了 `softcap` 的常数就会改变闭包值 → 改变哈希 → 重编译”的实现原因。

[`utils.py:121-156`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L121-L156) —— `hash_callable`：若回调已有缓存属性 `__cute_hash__` 则直接复用（避免重复 `inspect`），否则调用 `_compute_base_hash`，并把可变元数据 dunders 混入得到最终哈希。

源码指纹 `_compute_source_fingerprint` 则粒度更粗、作用在磁盘层：

[`cache_utils.py:51-78`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L51-L78) —— 它用 `@lru_cache(maxsize=1)` 标注，**每个进程只算一次**。哈希内容包含三部分：Python 主次版本号、`cutlass` 与 `tvm_ffi` 的版本号，以及对 `flash_attn/cute/` 下**所有 `.py` 文件**做 `rglob` 后逐个喂入（相对路径 + 内容长度 + 内容字节）。这意味着只要你改动了 cute 目录里任何一个 kernel 文件、或升级了 cutlass、或换了 Python 小版本，指纹就变，磁盘缓存自动换到新目录，旧的不会被误用。注意它**不哈希测试/示例/benchmark**，只覆盖 `flash_attn/cute/`。

#### 4.2.4 代码实践：观察哪些参数变化触发重编译

**实践目标**：通过监视 `compile_cache` 的大小变化，亲手验证“改什么会重编译、改什么不会”。

**操作步骤**：

```python
# recompile_probe.py（需 GPU）
import torch, flash_attn.cute as fa4
from flash_attn.cute import interface

def cache_size():
    return len(interface._flash_attn_fwd.compile_cache.cache)

q = torch.randn(1, 512, 8, 128, dtype=torch.float16, device="cuda")
k = torch.randn(1, 512, 8, 128, dtype=torch.float16, device="cuda")
v = torch.randn(1, 512, 8, 128, dtype=torch.float16, device="cuda")

fa4.flash_attn_func(q, k, v);                 print("after call 1:", cache_size())  # +1（首次编译）
fa4.flash_attn_func(q, k, v);                 print("after call 2:", cache_size())  #  不变（命中）
fa4.flash_attn_func(q, k, v, causal=True);    print("after causal :", cache_size())  # +1（causal 进 key）
fa4.flash_attn_func(q, k, v, softmax_scale=0.5); print("after scale :", cache_size())  #  不变（scale 不进 key）
```

**需要观察的现象**：

- 改变输入的 `batch`/`seqlen`：缓存大小不变（形状不进 key，只走运行期）。
- `causal=False → True`：+1（重编译）。
- 仅改 `softmax_scale`：不变。

**预期结果**：与上面注释一致。把 4.2.2 里“六类字段”的判断逐一对账：凡进 key 的改了就 +1，不进 key 的改了就不变。

> **待本地验证**：具体编译耗时与是否真的发生 `nvcc`/`ptxas` 调用，可用 `FA_LOG_LEVEL=1` 运行，观察日志里是否出现新的 “Exporting compiled function to disk” 或磁盘 miss 提示。

#### 4.2.5 小练习与答案

**练习 1**：把 `window_size_left` 从 `None` 改成 `512`，再从 `512` 改成 `1024`，分别会不会触发重编译？

> **答案**：`None → 512` 会重编译（`window_size_left is not None` 这个布尔从 `False` 变 `True`，进了 key）；`512 → 1024` 不会重编译（key 里只有“是否为 None”，具体数值不进 key）。

**练习 2**：为什么 `fa_logging.get_fa_log_level()` 要进 `compile_key`，而 `softmax_scale` 不要？

> **答案**：日志级别通过 `const_expr` 在**编译期**决定 kernel 保留多少 `cute.printf` 指令，会改变生成的 PTX/CUBIN，所以必须进 key。`softmax_scale` 只是运行期乘到分数上的一个标量，不改生成代码，所以不进 key（详见 [u4-l2](u4-l2-score-mod.md) 关于 `softmax_scale` 双重身份的讨论）。

**练习 3**：你改了 `flash_attn/cute/softmax.py` 里的一行注释，磁盘缓存会失效吗？

> **答案**：会。`_compute_source_fingerprint` 哈希的是文件的**字节内容**（`src.read_bytes()`），改注释也改变了字节，指纹随之改变，磁盘缓存换到新目录。

---

### 4.3 两级缓存：进程内 dict + 磁盘持久化

#### 4.3.1 概念说明

有了 `compile_key`，缓存实现就是“`key → 编译产物`”的映射。FA4 提供两级：

1. **进程内缓存 `JITCache`**：本质就是一个 Python `dict`，存活在解释器进程的生命周期内。命中速度极快（一次字典查找），但进程一退出就没了。
2. **磁盘持久化缓存 `JITPersistentCache`**：继承自 `JITCache`，在内存 `dict` 之外，把编译产物序列化成 `.o` 文件落盘。下次新进程启动时，即使内存 `dict` 是空的，也能从磁盘把产物捞回来，跳过编译。

> **关于“LRU”的说明**：项目文档（CLAUDE.md）把这层称作 “in-memory LRU”，但源码里的 [`JITCache.cache`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L155) 是一个**没有容量上限、没有淘汰策略的普通 `dict`**（对比 `_compute_source_fingerprint` 上货真价实的 `@lru_cache`）。因此严格说它是“无界内存字典缓存”，并非真正的 LRU。读源码时要忠于实现，不要被文档措辞误导。

是否启用磁盘层，由环境变量 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED` 决定（默认关闭）。开关在读模块时一次性求值，所以要在**导入 `flash_attn.cute` 之前**就设好。

#### 4.3.2 核心流程

工厂函数 `get_jit_cache(name)` 根据开关返回两种实现之一：

```
get_jit_cache("fwd")
   │
   ├─ CUTE_DSL_CACHE_ENABLED == 0  →  JITCache()              （仅内存 dict）
   │
   └─ CUTE_DSL_CACHE_ENABLED == 1  →  JITPersistentCache(path) （内存 dict + 磁盘）
            path = /tmp/${USER}/flash_attention_cute_dsl_cache/
                   └─ <source_fingerprint>/      ← 源码指纹目录（源码变就换目录）
                        └─ <name>/               ← "fwd" / "bwd" / ...
                             ├─ <sha256>.o       ← 序列化的编译产物
                             └─ <sha256>.lock    ← 文件锁
```

`JITPersistentCache` 的读写协议（关键是“先内存、后磁盘”，且**返回 True 时保证内存已填充**）：

- **查（`__contains__`）**：先查内存 `dict`；内存没有就 `_try_load_from_storage` 从磁盘加载并**顺便填进内存**。
- **读（`__getitem__`）**：先调 `__contains__`（确保内存被填好），再从内存 `dict` 取。
- **写（`__setitem__`）**：先写内存 `dict`，再 `_try_export_to_storage` 导出到磁盘。

磁盘层用 `FileLock`（基于 `fcntl.flock` 的建议锁）做并发保护：

- **加载（读）**：持**共享锁**（`LOCK_SH`），防止别的进程同时正在写这个 key。
- **导出（写）**：持**排他锁**（`LOCK_EX`），且写之前先检查 `.o` 是否已存在（“另一个进程已经导出了”就跳过）。

这套锁让多进程（比如 pytest-xdist 的 64 个 worker、或多个训练进程）可以安全共享同一份磁盘缓存，不会因并发写同一个 `.o` 而损坏。

#### 4.3.3 源码精读

先看两个环境变量与缓存目录：

[`cache_utils.py:33-34`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L33-L34) —— `CUTE_DSL_CACHE_ENABLED`，读 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED`，默认 `"0"`（关闭）。

[`cache_utils.py:37-39`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L37-L39) —— `CUTE_DSL_CACHE_DIR`，可用 `FLASH_ATTENTION_CUTE_DSL_CACHE_DIR` 自定义目录。

[`cache_utils.py:42-48`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L42-L48) —— `get_cache_path`：默认目录是 `/tmp/${USER}/flash_attention_cute_dsl_cache/`（`tempfile.gettempdir() / getuser() / ...`）。

工厂函数把“源码指纹”接进路径：

[`cache_utils.py:264-281`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L264-L281) —— `get_jit_cache`：开启时路径为 `get_cache_path() / _compute_source_fingerprint() / name`；关闭时返回纯内存 `JITCache`。注释明确说明“代码或依赖变化时自动失效旧条目”。

内存层 `JITCache`（注意 `cache` 是普通 `dict`）：

[`cache_utils.py:149-170`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L149-L170) —— 提供 `__setitem__`/`__getitem__`/`__contains__`/`clear`，就是对 `self.cache` 这个 dict 的薄封装。

磁盘层 `JITPersistentCache` 的核心三方法：

[`cache_utils.py:196-201`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L196-L201) —— `__contains__`：内存命中直接返回 True；否则 `_try_load_from_storage`，返回 True 时**保证内存已被填充**（这是 `__getitem__` 能安全随后读内存的前提）。

[`cache_utils.py:203-225`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L203-L225) —— `_try_load_from_storage`：把 key 经 `_key_to_hash` 转成 sha256 文件名，持**共享锁**检查 `.o` 是否存在；存在则用 `cute.runtime.load_module(..., enable_tvm_ffi=True)` 加载、取出导出符号 `func`、填进内存并返回 True。

[`cache_utils.py:227-246`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L227-L246) —— `_try_export_to_storage`：持**排他锁**，若 `.o` 已存在则跳过（“另一个进程已经导出了”），否则调 `fn.export_to_c(object_file_path=..., function_name="func")` 把编译产物序列化落盘。

key 到文件名的转换与锁路径：

[`cache_utils.py:248-249`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L248-L249) —— `_key_to_hash`：`hashlib.sha256(pickle.dumps(key)).hexdigest()`。这里用 `pickle` 把整个 `compile_key` 元组（包括字符串哈希、布尔、`Constexpr` 等）序列化后再哈希，得到稳定的文件名。

[`cache_utils.py:251-252`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L251-L252) —— `_lock_path`：每个 key 一把锁，锁文件名 `<sha256>.lock`，与 `.o` 同目录。

文件锁本身（轮询 + 超时）：

[`cache_utils.py:117-140`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L117-L140) —— `FileLock.__enter__`：用 `fcntl.flock(..., LOCK_NB)` 非阻塞尝试，失败就 `sleep(0.1)` 轮询，直到拿到或超时（默认 15 秒）抛 `RuntimeError`。

最后，`cache_utils.py` 顶部还有一段容易被忽略但很关键的初始化：

[`cache_utils.py:26-28`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L26-L28) —— 在导入时就把 CuTeDSL 运行时库以 `RTLD_GLOBAL` 预加载，使其符号（如 `_cudaLibraryLoadData`）对后续 `dlopen` 加载的磁盘 `.o` 模块可见；否则从磁盘加载缓存 kernel 时会报 “undefined symbol”。这是磁盘缓存能正常工作的隐藏前提。

#### 4.3.4 代码实践：启用磁盘缓存，对比首次与二次耗时

**实践目标**：亲手验证“磁盘缓存让第二个进程跳过编译”，并理解清缓存后为何又会重新编译。

**操作步骤**：

```bash
# 1) 开启磁盘缓存（必须在 import flash_attn.cute 之前设好）
export FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1

# 2) 首次运行：冷启动，会编译并落盘
python -c "
import time, torch, flash_attn.cute as fa4
q=torch.randn(1,512,8,128,dtype=torch.float16,device='cuda')
k=torch.randn(1,512,8,128,dtype=torch.float16,device='cuda')
v=torch.randn(1,512,8,128,dtype=torch.float16,device='cuda')
t=time.perf_counter(); fa4.flash_attn_func(q,k,v,causal=True); print('cold (s):', time.perf_counter()-t)
"

# 3) 第二次运行：新进程，内存缓存为空，但从磁盘命中
python -c "
import time, torch, flash_attn.cute as fa4
q=torch.randn(1,512,8,128,dtype=torch.float16,device='cuda')
k=torch.randn(1,512,8,128,dtype=torch.float16,device='cuda')
v=torch.randn(1,512,8,128,dtype=torch.float16,device='cuda')
t=time.perf_counter(); fa4.flash_attn_func(q,k,v,causal=True); print('warm (s):', time.perf_counter()-t)
"

# 4) 查看磁盘产物（路径里能看到源码指纹目录）
ls /tmp/$USER/flash_attention_cute_dsl_cache/

# 5) 清空磁盘缓存后再次运行：又会冷启动编译
rm -rf /tmp/$USER/flash_attention_cute_dsl_cache/
```

**需要观察的现象**：

- 步骤 2 日志（`FA_LOG_LEVEL=1`）出现 “Exporting compiled function to disk”。
- 步骤 3 日志出现 “Loading compiled function from disk”，且耗时显著低于步骤 2。
- 步骤 4 能看到一长串 hex 命名的目录（即源码指纹）和 `.o` 文件。
- 步骤 5 清掉后，又回到冷启动。

**预期结果**：`cold` 耗时 ≫ `warm` 耗时；两次运行的数值输出一致（缓存只改实现速度，不改数学结果）。

> **待本地验证**：本实践需要一块支持的 GPU（Hopper/Blackwell/Ampere）。无 GPU 时，可改为阅读 [`tests/cute/test_cache_utils.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_cache_utils.py) —— 它在 `tmp_path` 里造一个 `JITPersistentCache`，mock 掉 `cute.runtime.load_module`，断言“`FA_LOG_LEVEL>=1` 时才打印 ‘Loading compiled function from disk’”，无需 GPU 即可验证磁盘命中/日志路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `JITPersistentCache.__getitem__` 要先调一次 `__contains__`，而不是直接读磁盘？

> **答案**：`__contains__` 在内存未命中时会顺手把磁盘产物加载**填进内存 dict**（见 `_try_load_from_storage` 末尾的 `JITCache.__setitem__`）。这样 `__getitem__` 随后只需从内存读，且同一 key 在同一进程内的后续命中都走内存，不必反复读磁盘。注释明确：“When returning True, guarantees the in-memory cache is populated.”

**练习 2**：两个 pytest-xdist worker 同时第一次编译同一个 kernel，会重复写 `.o` 吗？

> **答案**：不会损坏，但可能各自编译一次。`_try_export_to_storage` 持**排他锁**，且写之前检查 `.o` 是否已存在（[`cache_utils.py:237-240`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cache_utils.py#L237-L240)）。先拿到锁的 worker 导出；后到的 worker 拿到锁时发现文件已存在就跳过。极端情况下两个 worker 几乎同时 miss、各自编译，但落盘由锁串行化，不会写出坏文件。

**练习 3**：磁盘缓存目录为什么要按 `_compute_source_fingerprint` 分层？

> **答案**：源码指纹随“任何 `.py` 改动 / cutlass 版本 / Python 版本”变化。把它作为目录前缀，意味着源码一变，新进程会去新目录找缓存（找不到→重编译），旧目录的产物不会被错误复用——这是一种“粗粒度、自动的整体失效”，与 `compile_key` 的“细粒度、按配置失效”互补。

---

## 5. 综合实践：跑通“两段式编译-执行”工作流

把本讲三个最小模块串起来，复现 FA4 CI 使用的“两段式测试”思路，亲历缓存从无到有、再到跨进程复用的全过程。

**任务**：对 `fp16 / head_dim=128 / causal=True` 的前向，完成下面四步，并记录每步耗时与缓存状态。

1. **冷启动 + 磁盘缓存开启**：

   ```bash
   export FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1
   export FA_LOG_LEVEL=1
   time python -c "import torch,flash_attn.cute as f;q=torch.randn(1,2048,8,128,device='cuda',dtype=torch.float16);k=v=q.clone();f.flash_attn_func(q,k,v,causal=True)"
   ```
   预期日志：`Exporting compiled function to disk`；耗时较长。

2. **跨进程命中**：再次运行同一条命令。预期日志：`Loading compiled function from disk`；耗时显著下降。

3. **免 GPU 编译（Pass 1 风格）**：清空磁盘缓存后，用 FakeTensor 重新“喂”出缓存：

   ```bash
   rm -rf /tmp/$USER/flash_attention_cute_dsl_cache/
   FLASH_ATTENTION_FAKE_TENSOR=1 FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1 \
     python -m pytest -n 4 -x tests/cute/test_flash_attn.py -k test_flash_attn_output
   ```
   预期：4 个 worker 并行把各配置的 kernel 编译并落盘，**不占用 GPU 计算**（仅编译、不执行）。

4. **真实执行（Pass 2 风格）**：再用真实模式跑同一批用例：

   ```bash
   FLASH_ATTENTION_FAKE_TENSOR=0 FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1 \
     python -m pytest -x tests/cute/test_flash_attn.py -k test_flash_attn_output
   ```
   预期：Pass 1 落盘的产物被加载，跳过编译，直接在 GPU 上执行。

**交付物**：

- 一张表，记录四步各自的 wall-clock 时间与观察到的日志关键字（`Exporting` / `Loading` / 均无）。
- 一段话解释：为什么 Pass 1 可以“无 GPU 并行编译”，而 Pass 2 必须有 GPU？（提示：编译产物落盘靠 `export_to_c`，执行靠 `cudaLibraryLoadData` 把 CUBIN 注册到一块真实 GPU。）
- 用 `ls /tmp/$USER/flash_attention_cute_dsl_cache/` 截图或列出源码指纹目录，说明它如何随你（可选地）改一行 `softmax.py` 注释而变化。

> **待本地验证**：步骤 3/4 依赖 GPU 与完整 cutlass-dsl 环境；若无，请退化为“源码阅读型实践”——对照 [`tools/ci/run_fa4_ci.py:100-112`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tools/ci/run_fa4_ci.py#L100-L112) 写出两段式流程的数据依赖图，并解释 `FLASH_ATTENTION_FAKE_TENSOR` 如何经 [`test_flash_attn.py:80`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L80) 的 `USE_FAKE_TENSOR` 与 [`testing.py:468`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L468) 的 `maybe_fake_tensor_mode` 装饰器传入 `_flash_attn_fwd`。

## 6. 本讲小结

- FA4 是 **JIT**：kernel 是 Python，运行时由 `cute.compile` 翻译为 PTX → CUBIN 并注册到 GPU；这与 FA2/FA3 的 AOT（安装期 `nvcc`）相反，造就了“安装秒装、首次调用慢”的体感。
- 编译与执行在 `_flash_attn_fwd` 里被**分离**：`cute.compile` 无条件发生，真实 kernel 启动受 `if not is_fake_mode()` 守卫——这就是免 GPU 编译（FakeTensor 两段式测试）的根因。
- `compile_key` 是一个精细的元组：进 key 的字段（`causal`、dtype、hdim、tile、arch、`score_mod` 源码哈希、日志级别……）改了会重编译；不进 key 的（`softmax_scale` 数值、`window_size` 具体值、输入形状）改了不会。
- `score_mod`/`mask_mod` 的哈希来自 [`utils.hash_callable`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L121-L156)——它哈希**源码 + 闭包值**，所以改回调逻辑或其捕获的常数都会换 key。
- 两级缓存：进程内 `JITCache`（普通 `dict`，非真正 LRU）+ 可选磁盘 `JITPersistentCache`（`.o` 文件 + `fcntl` 文件锁，多进程安全共享），由 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED` 开启。
- 源码指纹 `_compute_source_fingerprint`（哈希 `flash_attn/cute/**/*.py` + cutlass/tvm_ffi/Python 版本）作为磁盘目录前缀，实现“源码一变、旧缓存整体失效”。

## 7. 下一步学习建议

- 想彻底理解“为什么某个参数进 key”？继续读 [u11-l2 Constexpr 特化与 @cute.jit 注入](u11-l2-constexpr-specialization.md)，看 `cutlass.Constexpr` 如何在编译期裁剪分支，从而把“参数变化”变成“生成代码变化”。
- 想看缓存机制在测试里如何被压榨？读 [u11-l3 测试体系与参考实现](u11-l3-tests-and-reference.md)，结合本讲的 `FLASH_ATTENTION_FAKE_TENSOR` 与 `FA_LOG_LEVEL` 理解两段式测试与 OOM 重试。
- 想深入“编译产物长什么样”？读 [u11-l5 GPU Kernel 调试与 PTX/SASS](u11-l5-debugging-ptx-sass.md)，用 `CUTE_DSL_KEEP_PTX=1` 导出 PTX、用 `cute_dsl_ptxas.py` 接管 `ptxas`，把本讲的 `cute.compile` 链路在中间产物层面看个清楚。
- 直接验证你猜得对不对：拿本讲 [4.2.4](#424-代码实践观察哪些参数变化触发重编译) 的 probe 脚本，对每一个 `compile_key` 字段做一次“改值 → 看缓存是否 +1”的对账练习。
