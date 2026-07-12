# 后端派发 pass

## 1. 本讲目标

本讲深入 MLC LLM 编译流水线中的「派发类 pass」。学完后你应该能够：

- 说清「派发（dispatch）」在编译器里的含义：把模型代码里写下的**占位/通用调用**，在编译期改写为**某个具体后端实现**。
- 读懂三个派发 pass 的源码：`BLASDispatch`（cuBLAS/hipBLAS）、`DispatchTritonKernel`（Triton FP8 GEMM）、`DispatchKVCacheCreation`（FlashInfer vs TIR 通用 KV cache）。
- 理解它们各自「在什么 target、什么配置下生效」，以及不生效时的回退路径。
- 区分两种改图机制：**声明式模式匹配**（`FuseOpsByPattern`）与**手动表达式改写**（`PyExprMutator`）。

承接 [u7-l2](./u7-l2-pass-pipeline-overview.md)：那里我们只看了五阶段流水线的「编排与设计意图」，本讲打开 Phase 0 / Phase 1 里三个最典型的派发 pass。

## 2. 前置知识

### 2.1 为什么要「派发」

MLC 模型用 Relax nn 写出来时（见 [u3-l2](./u3-l2-relax-nn-model.md)），代码里会出现一些**「占位调用」**：作者只声明「这里要做一个 FP8 block-scale 矩阵乘」「这里要创建一个分页 KV cache」，但**不绑定具体实现**。例如算子层只发出一个带全局符号的外部调用：

```python
out = nn.extern("mlc.triton.w8a8_block_fp8_matmul", args=[...])  # 通用占位
```

这种「占位」的好处是模型定义与后端解耦：同一个模型，在 CUDA 上可以替换成 Triton/CUTLASS kernel，在别的后端上走通用 TIR 路径。**派发 pass 就是编译期那个「根据 target 把占位换成具体实现」的步骤。**

### 2.2 关键 TVM 概念速查

| 概念 | 含义 |
|---|---|
| `IRModule` | 整个模型的中间表示，里面装着一组 Relax 函数 + TIR 函数 |
| `@tvm.transform.module_pass` | 把一个类变成「作用于整个 IRModule 的 pass」的装饰器，需实现 `transform_module(mod, ctx)` |
| `call_tir` / `call_dps_packed` | Relax 里调用 TIR 函数（destination-passing style）的方式；`ExternFunc` 则调用一个外部命名函数 |
| `PyExprMutator` | 手动遍历并改写 Relax 表达式的基类（上一讲 `FuseAddRMSNorm` 用过） |
| `FuseOpsByPattern` | TVM 内置的**声明式**融合 pass：喂一组「模式」进去，它自动匹配并融合/标注 |
| `external_mods` | IRModule 上的属性，存放「外部编译模块」（如 Triton jit 出来的 kernel），最终随模型库一起导出 |
| `target` | 编译目标，如 `cuda`、`rocm`、`llvm`、`vulkan`、`metal`、`webgpu`；`target.kind.name` 取其名字 |

### 2.3 三个 pass 在流水线里的位置

派发 pass 分散在两个阶段（这一点很重要，不要笼统说成「都在 Phase 1」）：

- **Phase 0**（附加元数据与运行时函数）：`DispatchKVCacheCreation` —— 因为它要把 KV cache 维度写进 `metadata`，必须在所有优化前完成（[pipeline.py:L107](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L107)）。
- **Phase 1**（高层算子图优化）：`DispatchTritonKernel` 与 `BLASDispatch` —— 它们改写的是高层 matmul/外部调用（[pipeline.py:L123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L123)、[pipeline.py:L126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L126)）。

三者的共同外形：都是 `@tvm.transform.module_pass`，`__init__` 里吃 `target`，`transform_module` 里做改写；都遵循 u7-l2 提到的「外层三元（target 控制）」机制——在非目标后端上要么 no-op、要么抛错、要么由更外层的 `OptimizationFlags` 提前关掉。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `python/mlc_llm/compiler_pass/blas_dispatch.py` | 把 GEMM 派发到 cuBLAS / hipBLAS |
| `python/mlc_llm/compiler_pass/dispatch_triton_kernel.py` | 把 `mlc.triton.*` 占位调用改写为具体 Triton TIR kernel |
| `python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py` | 把 KV cache 创建占位函数改写为 TIR / FlashInfer 两种实现 |
| `python/mlc_llm/compiler_pass/pipeline.py` | 三个 pass 的装配位置（Phase 0 / Phase 1） |
| `python/mlc_llm/interface/compiler_flags.py` | `OptimizationFlags`：`flashinfer`/`cublas_gemm` 等开关的 target 矫正 |
| `python/mlc_llm/op/triton.py` | Triton kernel 的 TIR 生成器（被 `DispatchTritonKernel` 调用） |
| `python/mlc_llm/nn/kv_cache.py` | `PagedKVCache.create_generic`，发出 `mlc.create_paged_kv_cache_generic` 占位调用 |

## 4. 核心概念与源码讲解

### 4.1 BLAS 派发：cuBLAS / hipBLAS

#### 4.1.1 概念说明

BLAS（Basic Linear Algebra Subprograms）是事实标准的矩阵运算库接口。NVIDIA GPU 上对应 **cuBLAS**，AMD ROCm 上对应 **hipBLAS**。它们对「大矩阵乘法（尤其 prefill 阶段的大 GEMM）」有厂商深度优化的实现，往往比 TVM 自己生成的 kernel 更快。

`BLASDispatch` 的职责：在编译期，把模型里那些**未被量化的、规模够大的 GEMM**识别出来，标注成「交给 cuBLAS/hipBLAS codegen」，随后 TVM 的 `RunCodegen` 会把这些标注点替换成对外部库的实际调用。

#### 4.1.2 核心流程

```text
__init__(target):
    if target == cuda:   检查 relax.ext.cublas 可用 → 加载 "cublas" 模式表
    elif target == rocm: 检查 relax.ext.hipblas 可用 → 加载 "hipblas" 模式表
    else:                直接抛异常 "Unsupported target"

transform_module(mod):
    1. 收集所有 relax 函数名
    2. 过滤掉「单 batch decode」（小 GEMV，BLAS 不擅长，走专用路径）
    3. FuseOpsByPattern(patterns, annotate_codegen=True)  # 声明式匹配并标注 codegen
    4. RunCodegen(...)                                    # 把标注点落成外部调用
```

注意第 2 步的过滤条件是本 pass 的精髓：BLAS 只接「批量的或非 decode 的」函数，把单 batch 的 decode（典型的小 GEMV）留给后续 `LowBatchGemvSpecialize`（u8-l4）。

#### 4.1.3 源码精读

pass 类与 target 分发：[blas_dispatch.py:L15-L31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/blas_dispatch.py#L15-L31)。`__init__` 里 cuda 分支取 `cublas` 模式、rocm 分支取 `hipblas` 模式，其余 target 直接 `raise Exception`。

过滤「单 batch decode」并跑两步声明式 pass：[blas_dispatch.py:L35-L50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/blas_dispatch.py#L35-L50)。关键那行：

```python
model_names = [name for name in model_names if "batch" in name or "decode" not in name]
```

即「名字里含 `batch`」或「名字里不含 `decode`」的函数才交给 BLAS。`prefill`（无 decode）✅、`batch_decode`（含 batch）✅、`decode`（单 batch 解码）❌。

但这里有个**双层保护**的问题：`__init__` 会在非 cuda/rocm 上抛异常，那为什么流水线不会在 `llvm`/`vulkan` 上崩？因为装配处用 `cublas_gemm` 开关包了一层：

```python
BLASDispatch(target) if cublas_gemm else tvm.transform.Sequential([])
```

见 [pipeline.py:L126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L126)。而 `cublas_gemm` 又被 `OptimizationFlags.update` 矫正过——非 cuda/rocm 或量化格式不符时强制为 `False`：[compiler_flags.py:L103-L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L103-L113)。

```python
def _cublas_gemm(target, quantization) -> bool:
    if target.kind.name not in ["cuda", "rocm"]:
        return False
    if not (
        quantization.name in ["q0f16", "q0bf16", "q0f32"]
        or "e4m3" in quantization.name
        or "e5m2" in quantization.name
    ):
        return False
    return self.cublas_gemm
```

也就是说：`cublas_gemm` 只在「cuda/rocm」+「未量化或 FP8 量化」时才可能为真。`__init__` 里的抛错分支在实际运行中几乎是死代码，属于防御式编程。

#### 4.1.4 代码实践

**实践目标**：搞清 `BLASDispatch` 到底在哪些 target/量化下生效，并验证它对 `decode` 的过滤。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [blas_dispatch.py:L19-L31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/blas_dispatch.py#L19-L31)，列出 `__init__` 的三个分支与各自行为。
2. 对照 [compiler_flags.py:L103-L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L103-L113)，回答：在 `--target cuda` + `--quantization q4f16_1`（int4 group quant）下，`cublas_gemm` 即便用户传 `--opt cublas_gemm=1` 也会被矫正成什么？
3. 设想一个 IRModule 含 `prefill`、`decode`、`batch_decode`、`batch_verify` 四个 relax 函数，按 [blas_dispatch.py:L39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/blas_dispatch.py#L39) 的规则，手算哪些会进入 `model_names`。

**预期结果**：

- 第 2 步：`q4f16_1` 不在白名单、也不含 `e4m3`/`e5m2`，`_cublas_gemm` 返回 `False`，BLAS 派发整体不生效。
- 第 3 步：`prefill`（无 decode）✅、`decode`（单 batch，被排除）❌、`batch_decode`（含 batch）✅、`batch_verify`（含 batch）✅。

> 待本地验证：在有 cuBLAS 的机器上对一个 `q0f16` 模型分别用 `--opt cublas_gemm=0/1` 编译，对比导出 `.so` 中 `relax.ext.cublas` 符号的引用差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BLASDispatch` 要主动排除单 batch 的 `decode` 函数？

**参考答案**：单 batch decode 的矩阵乘退化为「矩阵-向量乘（GEMV）」，是 memory-bound，cuBLAS 的 GEMM kernel 在此规模下并无优势；MLC 另有 `LowBatchGemvSpecialize`（见 u8-l4）为小 batch 生成专门的 GEMV kernel，性能更好，故把 decode 让出去。

**练习 2**：`BLASDispatch` 用的是「声明式模式匹配」还是「手动 mutator」？

**参考答案**：声明式。它调用 TVM 内置 `relax.transform.FuseOpsByPattern(self.patterns, ...)` + `RunCodegen(...)`，自己只负责喂模式和入口函数名，不手写遍历逻辑。

---

### 4.2 Triton kernel 派发

#### 4.2.1 概念说明

[Triton](https://triton-lang.org/) 是一种「用 Python 写 GPU kernel」的语言/编译器，特别适合写高性能 GEMM。MLC 在 block-scale FP8 量化（见 [u5-l3](./u5-l3-other-quantization-schemes.md)）里，为 CUDA 后端准备了 Triton 实现的「w8a8 block FP8 GEMM」——权重和激活都是 8-bit FP（w8a8），并带 block 级 scale。

但模型代码并不直接内联 Triton kernel，而是发出一个**带保留前缀的占位外部调用** `mlc.triton.*`。`DispatchTritonKernel` 的工作就是：扫描这些占位调用，按其参数（形状、block size、dtype 等）**即时生成对应的 Triton TIR kernel**并替换调用点。

#### 4.2.2 核心流程

```text
transform_module(mod):
    if target != cuda: return mod          # 非 CUDA 直接 no-op
    _Rewriter(mod, target).transform()

_Rewriter.visit_call_(call):
    if call 是 call_dps_packed
       and 第一个参数是 ExternFunc
       and 名字以 "mlc.triton." 开头:
           按 global_symbol 分派：
             "mlc.triton.w8a8_block_fp8_matmul"        → 生成普通 GEMM kernel
             "mlc.triton.w8a8_block_fp8_group_matmul"  → 生成 MoE group GEMM kernel
           把原 call 替换为 call_tir(生成的 TIR 函数, 输入张量)
    收集 Triton extern_mods，最后挂到 IRModule 的 "external_mods" 属性
```

占位调用从哪来？看 `op/triton.py` 的算子封装：模型层调用 `fp8_groupwise_scaled_gemm`，其内部 `nn.extern("mlc.triton.w8a8_block_fp8_matmul", ...)`，见 [triton.py:L641-L662](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/triton.py#L641-L662)。这个 `nn.extern` 最终会 lower 成一个 `call_dps_packed(ExternFunc("mlc.triton.w8a8_block_fp8_matmul"), ...)`——正是 `_Rewriter` 拦截的目标。

#### 4.2.3 源码精读

pass 类与「非 CUDA 直接跳过」：[dispatch_triton_kernel.py:L159-L176](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_triton_kernel.py#L159-L176)。

拦截与分派逻辑：[dispatch_triton_kernel.py:L42-L58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_triton_kernel.py#L42-L58)。注意三层守卫：必须是 `relax.call_dps_packed`、第一个参数必须是 `ExternFunc`、名字必须以 `mlc.triton.` 开头。命中后按 `global_symbol` 精确匹配两个已知 kernel，未知符号抛 `ValueError`。

以普通 GEMM 为例看「占位 → 具体」的落点：[dispatch_triton_kernel.py:L60-L104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_triton_kernel.py#L60-L104)。

```python
prim_func, func_name = get_tir_w8a8_block_fp8_matmul(
    N, K, block_n, block_k, in_dtype, out_dtype,
    BLOCK_SIZE_M, BLOCK_SIZE_N, BLOCK_SIZE_K, GROUP_SIZE_M,
    num_warps, num_stages, self.extern_mods,
)
if prim_func is None:
    gv = self.builder_.get().get_global_var(func_name)  # 已生成过，复用
else:
    gv = self.builder_.add_func(prim_func, func_name)   # 首次出现，加入 IRModule
return relax.call_tir(gv, [x, weight, x_scale, weight_scale], out_ty=out_ty)
```

两个要点：

1. **去重**：同一个 `(N, K, block_n, block_k, in_dtype, out_dtype)` 组合只会生成一次 kernel。`get_tir_*` 内部先扫 `extern_mods`，若已有同名 Triton kernel 就返回 `[None, tir_name]`，于是这里走 `get_global_var` 复用（[triton.py:L295-L297](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/triton.py#L295-L297)）。kernel 名字按形状参数拼后缀，如 `triton_w8a8_block_fp8_gemm_N4096_K4096_...`。
2. **真正的 kernel 在 `op/triton.py` 里生成**：`get_tir_w8a8_block_fp8_matmul` 用 `@I.ir_module` 构造一个 TIR PrimFunc，其内部用 `T.call_kernel(triton.jit(triton_kernel), ...)` 把 Python 写的 Triton kernel 嵌进去（[triton.py:L334-L363](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/triton.py#L334-L363)）。生成的 extern module 被 append 进 `extern_mods`。

最后，`_Rewriter.transform` 把收集到的 `extern_mods` 挂回 IRModule：[dispatch_triton_kernel.py:L36-L40](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_triton_kernel.py#L36-L40)。这些外部模块会随流水线末尾的 `AttachExternModules` 一起写进模型库。

> 与 CUTLASS 的关系：block-scale 量化层在「CUTLASS 可用且 `cutlass.groupwise_scaled_gemm_e4m3fn_e4m3fn` 存在」时优先用 CUTLASS，否则才走 Triton 路径——见 [block_scale_quantization.py:L299-L318](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L299-L318)。也就是说模型层就做了一次「CUTLASS vs Triton」选择，而 `DispatchTritonKernel` 只负责把 Triton 那条分支落地。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `mlc.triton.*` 占位调用从「模型层发出」到「被改写成 `call_tir`」的完整链路。

**操作步骤**（源码阅读型）：

1. 从 [block_scale_quantization.py:L311](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L311) 的 `triton.fp8_groupwise_scaled_gemm(...)` 进入。
2. 跳到 [triton.py:L641-L662](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/triton.py#L641-L662)，确认它发出 `nn.extern("mlc.triton.w8a8_block_fp8_matmul", args=[...16 个参数])`。数一下 args：前 4 个是张量（x, weight, x_scale, weight_scale），后 12 个是形状/block 配置 + dtype 字符串。
3. 跳到 [dispatch_triton_kernel.py:L66-L80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_triton_kernel.py#L66-L80)，核对 `assert len(args) == 16` 与解包顺序一致。

**需要观察的现象**：占位调用里的 16 个参数被拆成「4 个输入张量 + 10 个整数配置 + 2 个 dtype 字符串」；改写后调用点变成 `call_tir(gv, [x, weight, x_scale, weight_scale], out_ty)`，配置参数被烘进生成的 TIR kernel 名字与内部 constexpr。

> 待本地验证：在装了 Triton 的 CUDA 机器上，对一个 DeepSeek/Hopper 的 block-scale FP8 模型 `compile`，在 `--debug-dump` 输出的 `debug-phase1.py` 里搜索 `tir_w8a8_block_fp8_matmul` 与 `call_tir`，确认替换发生。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DispatchTritonKernel` 在非 CUDA target 上直接 `return mod` 而不报错？

**参考答案**：Triton 当前只面向 CUDA。非 CUDA 后端若用到 block-scale FP8，应在模型层就走非 Triton 的实现（或直接不被支持）。这里 no-op 是安全的「无匹配占位即不改图」；若 IRModule 里真残留了 `mlc.triton.*` 占位却没被改写，后续 `LegalizeOps` 等阶段会因找不到实现而报错，问题会暴露在更下游。

**练习 2**：两次相同形状的 FP8 GEMM 调用，会生成两个 TIR 函数吗？

**参考答案**：不会。`get_tir_w8a8_block_fp8_matmul` 先遍历 `extern_mods`，发现已有同名 kernel（名字含 `N/K/block_n/block_k/dtype` 后缀）就返回 `[None, tir_name]`，改写器走 `get_global_var` 复用已注册的那个 `gv`，避免重复生成。

---

### 4.3 KV cache 创建派发：FlashInfer vs TIR 通用实现

#### 4.3.1 概念说明

分页 KV cache 是 MLC 推理引擎的核心数据结构（详见 [u10-l1](./u10-l1-paged-kv-cache.md)）。它的**创建函数**需要知道一堆维度参数：层数、头数、head_dim、page_size、RoPE 配置、是否分离式推理……这些参数只有到具体模型 + 具体运行配置时才确定。

于是模型层（每个架构的 `create_paged_kv_cache` 方法）只发出一个**通用占位调用** `mlc.create_paged_kv_cache_generic(...)`，把 18 个参数打包传出去，见 [nn/kv_cache.py:L55-L90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/nn/kv_cache.py#L55-L90)。`DispatchKVCacheCreation` 在编译期把这个占位函数「展开」成**最多两个具体实现**：

- `create_tir_paged_kv_cache`：TIR 通用实现，**总是生成**，覆盖所有后端；
- `create_flashinfer_paged_kv_cache`：基于 [FlashInfer](https://github.com/flashinfer-ai/flashinfer) 的高性能实现，**仅在满足条件时生成**，否则回退到 TIR。

#### 4.3.2 核心流程

```text
transform_module(mod):
    1. 从 IRModule 里找出名为 "create_paged_kv_cache" 的 relax 函数（占位）
       找不到 → 直接 return（模型可能不使用 paged kv cache）
    2. extract_creation_args(func)：把占位调用里的 18 个参数解析成 kwargs 字典
    3. attach_kv_cache_metadata(kwargs)：把层数/头数/head_dim 写进 model metadata
    4. create_tir_paged_kv_cache(...)        # 一定生成
       create_flashinfer_paged_kv_cache(...) # 满足条件才生成，否则空
    5. 把两者的 extern_mods 挂到 IRModule
```

FlashInfer 是否生成的判定（任一命中即跳过、回退 TIR）：

\[
\text{skip} = \neg\,\text{flashinfer} \;\lor\; (\text{target}\neq\text{cuda}) \;\lor\; (\text{dtype}\notin\{\text{f16},\text{bf16}\}) \;\lor\; \big(\text{rope\_mode}=\text{INLINE} \land (\text{rotary\_dim}\neq\text{qk\_head\_dim} \lor \text{qk\_head\_dim}\neq\text{v\_head\_dim})\big)
\]

此外还有一层 `try/except`：即便上述条件都满足，构造 `FlashInferPagedKVCache` 时若抛错（如 FlashInfer 运行时未装），也会记日志并回退 TIR。

#### 4.3.3 源码精读

pass 类签名：注意它同时吃 `flashinfer` 开关和可变的 `metadata` 字典：[dispatch_kv_cache_creation.py:L81-L108](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L81-L108)。docstring 明确点出「metadata 会在此 pass 里被改写」。

主流程：[dispatch_kv_cache_creation.py:L110-L139](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L110-L139)。先把 `create_paged_kv_cache` 从模块里摘出来（`func_dict` 收集其余函数），再用 `BlockBuilder` 重建一个不含占位的新模块，最后调用两个 `create_*` 方法。

参数解析 `extract_creation_args`：[dispatch_kv_cache_creation.py:L16-L78](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L16-L78)。它用一连串 `assert` 锁定占位调用的精确结构：必须是单个 DataflowBlock、单个 binding、`call_pure_packed`、ExternFunc 名字恰为 `mlc.create_paged_kv_cache_generic`、共 18 个参数。注意 `attn_kind` 既可能是单个 `"mha"/"mla"`，也可能是**逐层列表** `["mha", "mla", "mha_sliding"]`（支持混合注意力，如 Gemma3 的滑动窗口）——这是它支持 MLA / 滑动窗口的关键。

把维度写进 metadata：[dispatch_kv_cache_creation.py:L141-L148](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L141-L148)。`metadata["kv_cache"]` 最终会随模型库一起导出，供运行期 C++ 引擎读取（详见 u9-l4 的 FunctionTable）。

TIR 实现（永远生成）：[dispatch_kv_cache_creation.py:L150-L186](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L150-L186)。用 `BlockBuilder` 定义一个名为 `create_tir_paged_kv_cache` 的 relax 函数，内部实例化 `kv_cache.TIRPagedKVCache(target=self.target, **kwargs)`。

FlashInfer 实现 + 全部回退条件：[dispatch_kv_cache_creation.py:L188-L243](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L188-L243)。

```python
if (
    not self.flashinfer
    or self.target.kind.name != "cuda"
    or str(kwargs["dtype"]) not in ["float16", "bfloat16"]
    or (
        kwargs["rope_mode"] == RopeMode.INLINE
        and (
            kwargs["rotary_dim"] != kwargs["qk_head_dim"]
            or kwargs["qk_head_dim"] != kwargs["v_head_dim"]
        )
    )
):
    return []   # 不生成 FlashInfer，回退 TIR
```

之后还包了 `try/except Exception`，构造失败时 `logger.info(...)` 提示「将回退到 TIR」并 `return []`（[dispatch_kv_cache_creation.py:L235-L241](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L235-L241)）。

`flashinfer` 开关本身也受 target 矫正：`OptimizationFlags._flashinfer` 要求 target 为 cuda 且所有架构 ≥ 80（Ampere 及以上），见 [compiler_flags.py:L87-L101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L87-L101)。

```python
def _flashinfer(target) -> bool:
    if not self.flashinfer:
        return False
    if target.kind.name != "cuda":
        return False
    arch_list = detect_cuda_arch_list(target)
    for arch in arch_list:
        if arch < 80:
            logger.warning("flashinfer is not supported on CUDA arch < 80")
            return False
    return True
```

> 编译期 vs 运行期：此 pass 在编译期把「TIR」与「FlashInfer」**两个**实现都写进模型库（FlashInfer 满足条件时）；运行期 C++ 引擎再根据自身条件选用其中一个——所以即便编译时 FlashInfer 合格，运行期没装 FlashInfer 也能退回 TIR。这是 MLC「编译期尽量多生成、运行期按需选」的典型设计。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：说清 `DispatchKVCacheCreation` 如何根据配置在 FlashInfer 与通用 TIR 之间选择；并指出 `BLASDispatch` 在何种 target 下不生效。

**操作步骤**：

1. 打开 [dispatch_kv_cache_creation.py:L195-L207](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L195-L207)，列出 `create_flashinfer_paged_kv_cache` 跳过 FlashInfer 的全部条件。
2. 检查 [dispatch_kv_cache_creation.py:L222-L241](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/dispatch_kv_cache_creation.py#L222-L241)，描述构造失败时的二次回退。
3. 回答 BLAS 问题：查 [blas_dispatch.py:L30-L31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/blas_dispatch.py#L30-L31)（`__init__` 抛错分支）+ [compiler_flags.py:L105-L106](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L105-L106)（矫正为 False 的 target）。

**需要观察的现象 / 预期结果**：

- **FlashInfer 选择**：编译期只在「`flashinfer=True`（且经 `_flashinfer` 矫正确认是 cuda、arch≥80）+ target=cuda + dtype ∈ {f16, bf16} + 不违背 INLINE RoPE 约束」时才生成 `create_flashinfer_paged_kv_cache`；任一不满足则只生成 TIR 版本。运行期引擎再在「TIR / FlashInfer」两者中选用其一，FlashInfer 不可用时仍退回 TIR。
- **BLASDispatch 不生效的 target**：`__init__` 对 `cuda`/`rocm` 以外的任何 target（`llvm`/`vulkan`/`metal`/`webgpu`/`opencl` 等）都会抛 `Unsupported target`。实际不崩，是因为 `OptimizationFlags._cublas_gemm` 已把这些 target 的 `cublas_gemm` 矫正为 `False`，流水线装配处改插空 `Sequential([])`。所以「BLAS 派发只在 cuda/rocm 上、且量化格式为 q0f16/q0bf16/q0f32 或 FP8 时才真正生效」。

> 待本地验证：分别在 `--target cuda`（开/关 `--opt flashinfer=1`）与 `--target vulkan` 下编译同一模型，用 `--debug-dump` 看 Phase 0 产物里是否出现 `create_flashinfer_paged_kv_cache` 函数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DispatchKVCacheCreation` 放在 Phase 0，而不是和另外两个派发 pass 一起放 Phase 1？

**参考答案**：它除了改图，还要**写 metadata**（`attach_kv_cache_metadata` 把 `num_hidden_layers`/`num_attention_heads`/`head_dim` 写进 `metadata["kv_cache"]`）。metadata 会被 Phase 0 的其它 pass（如 `AttachMemoryPlanAttr`、`AttachSequenceLengthPaddingFactor`）以及后续阶段消费，因此必须最早执行。

**练习 2**：FlashInfer 的 INLINE RoPE 约束（`rotary_dim == qk_head_dim` 且 `qk_head_dim == v_head_dim`）不满足时会怎样？

**参考答案**：`create_flashinfer_paged_kv_cache` 直接 `return []`，不生成 FlashInfer 实现函数；模型库只剩 `create_tir_paged_kv_cache`，运行期自然走 TIR 通用 KV cache。这是优雅降级，而非报错。

**练习 3**：`extract_creation_args` 里 `attn_kind` 为什么既可能是字符串又可能是列表？

**参考答案**：为了支持**逐层异构注意力**。单种注意力（普通 MHA 或 MLA）用单个字符串 `"mha"`/`"mla"`；而像 Gemma3 这样「部分层用滑动窗口注意力」的模型，需要逐层指定 `["mha", "mha_sliding", ...]`，长度等于隐藏层数（代码用 `assert len(args[0].fields) == args[3].value` 校验）。

## 5. 综合实践

**任务**：画一张「三个派发 pass 的对照表 + 改图机制」总结图，并据此预测一个具体配置下的行为。

**操作步骤**：

1. 自制一张表，列含：pass 名、所在 Phase、target 守卫、改图机制（声明式 `FuseOpsByPattern` / 手动 `PyExprMutator` / `BlockBuilder` 重建）、回退对象、是否写 metadata。把本讲三个 pass 填进去（参考答案见下）。
2. 选定配置：`--target cuda --quantization q3f16_0 --opt O2`，预测：
   - `BLASDispatch` 是否生效？为什么？（提示：`q3f16_0` 不在 cuBLAS 白名单。）
   - `DispatchTritonKernel` 是否有 `mlc.triton.*` 可改写？（提示：只有 block-scale FP8 量化才发出该占位。）
   - `DispatchKVCacheCreation` 是否会生成 FlashInfer 版本？（提示：O2 preset 里 `flashinfer=True`，且经 `_flashinfer` 矫正需 cuda arch≥80。）
3. 再换成 `--target vulkan --quantization q0f16`，重做三个预测。

**参考答案表**：

| pass | Phase | target 守卫 | 改图机制 | 回退 | 写 metadata |
|---|---|---|---|---|---|
| `BLASDispatch` | 1 | cuda/rocm（`cublas_gemm` 矫正） | 声明式 `FuseOpsByPattern`+`RunCodegen` | 不生效→留 TVM 自生成 GEMM | 否 |
| `DispatchTritonKernel` | 1 | 仅 cuda | 手动 `PyExprMutator` | 无 `mlc.triton.*` 则不改图 | 否 |
| `DispatchKVCacheCreation` | 0 | 无（TIR 全后端；FlashInfer 需 cuda） | `BlockBuilder` 重建函数 | FlashInfer 不合格→只留 TIR | 是（`kv_cache`） |

> 待本地验证：第 2、3 步的预测最好在真实编译中用 `--debug-dump` 的 `debug-phase0.py` / `debug-phase1.py` 核对。

## 6. 本讲小结

- **派发 = 编译期把占位调用换成具体后端实现**。模型层只写「要做什么」，后端选择留给派发 pass。
- `BLASDispatch` 用**声明式 `FuseOpsByPattern`** 把大 GEMM 交给 cuBLAS/hipBLAS，主动排除单 batch decode，只在 cuda/rocm + 未量化/FP8 量化下生效。
- `DispatchTritonKernel` 用**手动 `PyExprMutator`** 拦截 `mlc.triton.*` 占位，按形状即时生成去重的 Triton TIR kernel；仅 cuda 生效。
- `DispatchKVCacheCreation` 把 `mlc.create_paged_kv_cache_generic` 占位展开为「TIR（必有）+ FlashInfer（条件满足才有）」两个实现，并顺带把 KV cache 维度写进 metadata。
- 三者都用 **target 守卫 + `OptimizationFlags` 矫正**实现优雅降级：非目标后端要么 no-op、要么由开关提前关掉、要么回退通用实现，绝不硬崩。
- 派发 pass **跨阶段分布**：KV cache 创建在 Phase 0（因要写 metadata），BLAS/Triton 在 Phase 1。

## 7. 下一步学习建议

- 顺读 [u8-l3](./u8-l3-attach-passes.md)：看另一类「附加类 pass」（采样、logit 处理、推测解码辅助函数）如何用类似机制把运行时函数挂进 IRModule。
- 结合 [u10-l1](./u10-l1-paged-kv-cache.md) / [u10-l2](./u10-l2-prefix-cache-radix-tree.md)：理解这里生成的 `create_tir_paged_kv_cache` 在 C++ 引擎里如何被 FunctionTable 调用、如何与 Radix Tree 前缀缓存协作。
- 想动手扩展：若要加入一种新的「外部 GEMM 后端」（如某个厂商库），可参照 `BLASDispatch`（声明式）或 `DispatchTritonKernel`（手动改写）二选一作为模板，并在 `OptimizationFlags` 里加一个开关、在 `pipeline.py` Phase 1 装配。
