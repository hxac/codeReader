# 融合 MoE 后端（cutlass/trtllm）

## 1. 本讲目标

本讲承接 u6-l1（MoE 基础与统一 API）与 u5-l2（FP8 GEMM），把「融合 MoE 后端」这一摊事讲透。学完后你应当能够：

- 说清 FlashInfer 里「CUTLASS 后端」与「TensorRT-LLM(trtllm-gen) 后端」两套实现的来源、定位与差异。
- 看懂 `flashinfer/jit/fused_moe.py` 里「按 SM 版本选 `gen_*` 函数」的分派表，理解为何 `sm89/sm90/sm100/sm103/sm120` 各有一个生成函数。
- 理解 trtllm 后端「编译一个启动器 + 下载预编译 cubin」与 CUTLASS 后端「JIT 现生成 CUTLASS 模板实例化」两种截然不同的产物机制。
- 了解第三条 NVFP4 路径：基于 CuTe-DSL 的 `cute_dsl_fused_moe_nvfp4` / `CuteDslMoEWrapper`（SM100/SM103 专用），以及它与前两者的区别。
- 在同一份 `MoEConfig` 下切换 BF16 / FP8 后端，对比输出与耗时。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（在 u6-l1、u5-l2、u2-l3 中已建立）：

- **融合 MoE 的五步流程**：`gate → topk → dispatch → 专家 FFN(gemm1+act+gemm2) → combine`。FlashInfer 的融合 kernel 把后三步压成极少次 launch。
- **GEMM1 / GEMM2**：专家 FFN 的两次矩阵乘。GEMM1 把 hidden 投影到 intermediate（门控激活），GEMM2 再投回 hidden。
- **TunableRunner 与 AutoTuner**（u5-l1）：每个后端都包成一个 `TunableRunner`，`AutoTuner.choose_one` 在一组 tactic（tile/cluster 配置）里实测选最优。
- **JitSpec 与 `gen_*_module` 五步**（u2-l3）：算 URI → 建生成目录 → 渲染/生成源 → 拷贝源 → `gen_jit_spec` 装配。本讲会看到这条模式在 MoE 上的两个变体。
- **NVFP4 / FP8 块缩放**（u5-l2、u5-l3）：低精度乘法靠 per-tensor 或 groupwise/block 缩放还原动态范围。
- **`@supported_compute_capability` 与 `BackendOptions`**（u6-l1）：声明式后端硬件门控与有序候选列表。

一个关键直觉：FlashInfer 的「后端（backend）」在不同算子上语义略有差别。GEMM 里 backend 指 cuBLAS/cuDNN/CUTLASS/CuTe-DSL 等「实现提供方」；MoE 里则特指 **CUTLASS MoE GEMM** 与 **TensorRT-LLM 生成的 fused MoE kernel** 两套来源不同的代码库，外加 CuTe-DSL 这条纯 Python DSL 路径。本讲讲的就是后者这层「后端」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [flashinfer/jit/fused_moe.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/fused_moe.py) | MoE 的 JIT 代码生成层。含 5 个 `gen_cutlass_fused_moe_sm{89,90,100,103,120}_module` 与 1 个 `gen_trtllm_gen_fused_moe_sm100_module`，以及共用的 `gen_cutlass_fused_moe_module`。 |
| [flashinfer/fused_moe/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py) | MoE 的 Python API 与运行期逻辑。含 `get_cutlass_fused_moe_module`（SM 分派）、`cutlass_fused_moe`、`get_trtllm_moe_sm100_module`、`trtllm_{bf16,fp8_block_scale,fp8_per_tensor_scale,mxint4_block_scale,fp4_block_scale}_moe` 等入口，以及 `MoERunner`/`AutoTuner` 集成。 |
| [flashinfer/fused_moe/cute_dsl/fused_moe.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py) | CuTe-DSL NVFP4 融合 MoE。函数式 `cute_dsl_fused_moe_nvfp4` 与类式 `CuteDslMoEWrapper`，核心管线 `_moe_core_impl`。 |
| [flashinfer/fused_moe/api.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py) | 统一 API 的后端配置类（`CutlassConfig`/`TrtllmBf16Config`/`TrtllmFp8BlockConfig`/`TrtllmFp4Config`/`CuteDslConfig` 等），含 `supported(arch)` 门控与默认候选顺序 `_DEFAULT_BACKEND`。 |

辅助理解：`csrc/fused_moe/cutlass_backend/flashinfer_cutlass_fused_moe_binding.cu`（CUTLASS 后端的 TVM-FFI 绑定）、`csrc/trtllm_fused_moe_runner.cu` 与 `csrc/trtllm_fused_moe_kernel_launcher.cu`（trtllm 后端的启动器），它们正是上面 gen 函数拷贝/编译的源文件。

## 4. 核心概念与源码讲解

本讲三个最小模块分别对应三套后端实现：**CUTLASS MoE**、**trtllm MoE**、**CuTe-DSL MoE**。

### 4.1 CUTLASS MoE 后端

#### 4.1.1 概念说明

CUTLASS 后端是 FlashInfer MoE 的「通用兜底」实现，源自从 TensorRT-LLM 开源（OSS）的 CUTLASS MoE GEMM。它的特点：

- **路由与计算解耦**：`cutlass_fused_moe` 只接收**已经算好的** `token_selected_experts`（topk_ids）和 `token_final_scales`（topk 权重），即 `RoutingInputMode.UnpackedPrecomputed`。路由（gate+topk）由调用方在外部完成（u6-l1 讲的 `fused_topk_deepseek` 等），本后端只做 dispatch + GEMM1 + 激活 + GEMM2 + combine。
- **广架构覆盖**：从 SM89（Ada）一路到 SM120（Blackwell 消费级）都有对应实现，是 `CutlassConfig` 能作为「universal fallback」的原因。
- **精度档全**：BF16 / FP16 / FP8 / FP4 / INT4 / 混合精度（如 BF16×FP4）都由一组 `moe_gemm_kernels_<a>_<b>.cu` 文件覆盖，每个文件对应一种激活/权重 dtype 组合。
- **现生成 CUTLASS 实例化**：JIT 时会调用 CUTLASS 代码生成器，把若干 tile/cluster 配置的 GEMM 模板**实例化成 `.generated.cu` 源文件**再编译，这些实例化就是 autotuner 候选的 tactic。

#### 4.1.2 核心流程

CUTLASS MoE 的一次调用经过两层分派：

```text
Python: cutlass_fused_moe(input, topk_ids, topk_weights, w1, w2, ...)
   │  1. torch.cuda.get_device_capability() → device_arch 字符串(如 "100")
   ▼
get_cutlass_fused_moe_module(device_arch)          # @functools.cache
   │  2. 按 device_arch 选 gen_cutlass_fused_moe_sm{89,90,100,103,120}_module
   │     → .build_and_load() 编译并加载 JIT 模块
   ▼
module.cutlass_fused_moe(...)                       # TVM-FFI 路由到 C++
   │  3. C++ 侧 AutoTuner 选 tactic（gemm1/gemm2 各选一次）
   ▼
run_moe: dispatch → GEMM1 → act(SwiGLU) → GEMM2 → finalize(combine)
```

关键设计：**SM 分派发生在两处**——Python 侧 `get_cutlass_fused_moe_module` 按架构选 gen 函数，gen 函数内部又通过 `supported_major_versions` 把全局架构集收窄到该 kernel 支持的几代（u2-l4 讲过的机制）。

#### 4.1.3 源码精读

**Python 入口算出架构并分派**：[flashinfer/fused_moe/core.py:1003-1004](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L1003-L1004) 把 `get_device_capability()` 折算成 `"100"` 这样的字符串，再在 [core.py:1035](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L1035) 取对应模块并调用其 `cutlass_fused_moe` 符号。

```python
major, minor = torch.cuda.get_device_capability()
device_arch = f"{major * 10 + minor}"
...
return get_cutlass_fused_moe_module(device_arch).cutlass_fused_moe(output, input, ...)
```

**模块加载层的 SM 分派表**：[flashinfer/fused_moe/core.py:277-289](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L277-L289) 是本讲最核心的一张表。注意它把 `"120"/"121"` 都映射到 sm120 模块、`"100"/"110"` 都映射到 sm100 模块——同一 major 内的微小差异共用一份生成代码：

```python
def get_cutlass_fused_moe_module(backend: str = "100", use_fast_build: bool = False):
    if backend in ("120", "121"):
        module = gen_cutlass_fused_moe_sm120_module(use_fast_build).build_and_load()
    elif backend == "103":
        module = gen_cutlass_fused_moe_sm103_module(use_fast_build).build_and_load()
    elif backend in ("100", "110"):
        module = gen_cutlass_fused_moe_sm100_module(use_fast_build).build_and_load()
    elif backend == "90":
        module = gen_cutlass_fused_moe_sm90_module(use_fast_build).build_and_load()
    elif backend == "89":
        module = gen_cutlass_fused_moe_sm89_module(use_fast_build).build_and_load()
    else:
        raise ValueError(f"Invalid backend: {backend}")
```

加载后还会给模块挂上 DeepGEMM 的 JIT include 目录（[core.py:294-297](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L294-L297)），因为 CUTLASS MoE 在 FP8 块缩放路径上会用到 DeepGEMM。

**gen 函数的差异只在 flags 与架构范围**：对比 sm120 与 sm90 两个 gen 函数。[gen_cutlass_fused_moe_sm120_module](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/fused_moe.py#L58-L73) 把 `supported_major_versions=[12]`、并启用 `COMPILE_BLACKWELL_SM120_TMA_GROUPED_GEMMS`：

```python
nvcc_flags += current_compilation_context.get_nvcc_flags_list(
    supported_major_versions=[12]
)
return gen_cutlass_fused_moe_module(nvcc_flags, "120", use_fast_build)
```

而 [gen_cutlass_fused_moe_sm90_module](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/fused_moe.py#L113-L124) 用 Hopper 的 `sm90a_nvcc_flags`、`COMPILE_HOPPER_TMA_GEMMS`，且 FP8 block scale / FP4 仅在 CUDA ≥ 12.8 时才编进来：

```python
nvcc_flags = sm90a_nvcc_flags + [
    "-DCOMPILE_HOPPER_TMA_GEMMS",
    "-DCOMPILE_HOPPER_TMA_GROUPED_GEMMS",
    "-DENABLE_BF16",
    "-DENABLE_FP8",
    "-DENABLE_FP8_BLOCK_SCALE" if is_cuda_version_at_least("12.8") else "",
    "-DENABLE_FP4" if is_cuda_version_at_least("12.8") else "",
    ...
]
```

**共用的装配函数：现生成 CUTLASS 实例化**：[gen_cutlass_fused_moe_module](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/fused_moe.py#L137-L159) 是 CUTLASS 后端的「五步」落点。它先在可写区 `FLASHINFER_GEN_SRC_DIR/cutlass_instantiations/{arch}` 调 `generate_gemm_operations` 把 CUTLASS 模板实例化成 `.generated.cu`：

```python
output_dir = jit_env.FLASHINFER_GEN_SRC_DIR / f"cutlass_instantiations/{device_arch}"
output_dir.mkdir(parents=True, exist_ok=True)
generate_gemm_operations(output_dir, f"{device_arch};{device_arch}-real")
```

然后用 glob 把这些生成的源文件连同大量 TRT-LLM 内部 `.cu`（每个 dtype 组合一个，如 `moe_gemm_kernels_bf16_bf16.cu`、`moe_gemm_kernels_fp8_fp4.cu`）一起塞进 `gen_jit_spec`（[jit/fused_moe.py:204-205](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/fused_moe.py#L204-L205)）：

```python
*(output_dir / kernel for kernel in output_dir.rglob("*.generated.cu")),
```

> 这正是 CUTLASS 后端与 trtllm 后端最本质的区别：CUTLASS 的 tactic 是 **JIT 现场实例化的 CUTLASS 模板源码**，编译产物是普通的 `.cuda.o`；而下一节将看到 trtllm 的 tactic 是 **预先编译好的 cubin 二进制**，JIT 只编译一个加载它们的启动器。

#### 4.1.4 代码实践

**实践目标**：验证「同一段 BF16 MoE 逻辑可在 CUTLASS 后端跑通」，并观察 SM 分派表如何决定编译哪一个模块。

**操作步骤**（待本地验证，需 SM80+ GPU 与已 `pip install -e .` 的 FlashInfer）：

1. 在 Python 中查询本机架构：

   ```python
   import torch
   major, minor = torch.cuda.get_device_capability()
   print(f"device_arch = {major*10+minor}")   # 例如 90 / 100 / 120
   ```

2. 设置 `export FLASHINFER_JIT_VERBOSE=1`，调用 CUTLASS 后端的一个最简 BF16 MoE（构造 `topk_ids`/`topk_weights` 后喂给 `cutlass_fused_moe`）。
3. 观察日志里编译的模块名：应当形如 `fused_moe_{arch}`（见 `gen_jit_spec(f"fused_moe_{device_arch}", ...)`，[jit/fused_moe.py:161-162](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/fused_moe.py#L161-L162)），并能在 `~/.cache/flashinfer/.../generated/cutlass_instantiations/{arch}/` 下看到 `*.generated.cu`。

**需要观察的现象**：

- 首次调用触发一次较长的 CUTLASS 实例化编译（数十秒到数分钟）；第二次调用直接命中进程内 `@functools.cache`。
- `device_arch` 决定了哪一个 `gen_cutlass_fused_moe_sm*_module` 被调用，从而决定编译进哪些 `moe_gemm_kernels_*.cu`。

**预期结果**：模块名与架构一致；生成的 `.generated.cu` 数量与 tactic 数量正相关。若在非支持架构上调用，`get_cutlass_fused_moe_module` 会落到 `else` 抛 `ValueError`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_cutlass_fused_moe_module` 要把 `"100"` 和 `"110"` 都映射到 `gen_cutlass_fused_moe_sm100_module`？
**答案**：SM100 与 SM110 同属 Blackwell（major=10），二进制兼容，可共用同一份 CUTLASS MoE GEMM 代码；gen 函数内部已用 `supported_major_versions=[10,11,12]` 允许这一代架构编译。只有 major 不同（如 9 vs 10 vs 12）才需要不同的 gen 函数，因为它们需要不同的 TMA/MMA 指令集与 nvcc flags。

**练习 2**：若把 `cutlass_fused_moe` 的 `use_deepseek_fp8_block_scale=True` 用在 SM100 上，会发生什么？
**答案**：会抛 `NotImplementedError("FP8 block scaling not yet implemented for Blackwell.")`，见 [core.py:1009-1013](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L1009-L1013)。FP8 块缩放在 CUTLASS 后端目前仅 SM90（且 CUDA≥12.8）支持；Blackwell 上要走 trtllm 后端的 `trtllm_fp8_block_scale_moe`。

### 4.2 trtllm MoE 后端

#### 4.2.1 概念说明

trtllm（trtllm-gen）后端源自 NVIDIA TensorRT-LLM 的「generated MoE kernel」。与 CUTLASS 后端相比，它有三个鲜明不同：

- **路由融合进 kernel**：trtllm 后端的入口既可接收 `routing_logits`（kernel 内部做 softmax/sigmoid+topk），也可接收预算好的 `topk_ids`，即 `RoutingInputMode` 三种模式都支持。这是它与 CUTLASS 后端（只吃预算 topk）的最大 API 差异。
- **预编译 cubin 机制**：trtllm-gen 的 GEMM kernel 是**预先离线编译好的 cubin 二进制**，FlashInfer 在 JIT 时只编译一个「启动器 + 路由 kernel」的薄 C++ 层，运行时由它加载并跳转到对应 cubin。cubin 缺失则从在线 kernel 缓存下载。
- **窄架构、高优化**：trtllm 后端只在 SM100（Blackwell）系列上提供，由 `is_trtllm_moe_supported` 门控（major≥10）。它在 Blackwell 上通常是 BF16/FP8/FP4 的首选低延迟实现。

入口按精度分一组：`trtllm_bf16_moe`、`trtllm_fp8_block_scale_moe`、`trtllm_fp8_per_tensor_scale_moe`、`trtllm_mxint4_block_scale_moe`、`trtllm_fp4_block_scale_moe`，以及对应的 `*_routed_moe`（预算路由版）。

#### 4.2.2 核心流程

trtllm 后端的编译与运行：

```text
JIT（首次）:
  gen_trtllm_gen_fused_moe_sm100_module()
    ├─ get_artifact(): 下载 flashinferMetaInfo.h（含 tllmGenBatchedGemmList）
    ├─ get_artifact(): 下载 BMM export headers + cubins
    ├─ gen_jit_spec(): 编译启动器(trtllm_fused_moe_runner.cu 等)
    │                   带 -DTLLM_GEN_GEMM_CUBIN_PATH="..." 指向 cubin 目录
    └─ build_and_load() + setup_cubin_loader()

运行（每次）:
  trtllm_bf16_moe(routing_logits, ..., tactic)
    ├─ MoERunner.get_valid_tactics(): moe_op.trtllm_get_valid_moe_configs(...)
    ├─ AutoTuner.choose_one(): 在 tactic 列表里实测选最优
    └─ moe_op.trtllm_bf16_moe(...): 启动器加载对应 cubin 跑 GEMM1/act/GEMM2/finalize
```

注意：trtllm 的 tactic 列表来自 `trtllm_get_valid_moe_configs`（向 C++ 查询可用 cubin 配置），而 CUTLASS 的 tactic 来自 `get_gemm1/gemm2_tactic_count`（来自 JIT 实例化的模板数）。两者都套同一个 `AutoTuner` 框架。

#### 4.2.3 源码精读

**硬件门控**：[is_trtllm_moe_supported](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L104-L135) 第一道闸就是 `arch[0] < 10` 直接返回 `False`，并限定权重 dtype 必须是 BF16/E4m3/E2m1/MxE2m1 之一、且激活与权重 dtype 要匹配：

```python
arch = get_compute_capability(torch.cuda.current_device())
if arch[0] < 10:
    return False
if dtype_weights not in [Bfloat16, E4m3, E2m1, MxE2m1]:
    return False
```

**cubin 下载与装配**：[gen_trtllm_gen_fused_moe_sm100_module](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L246-L280)（在 `flashinfer/jit/fused_moe.py`，下同）先从在线缓存取 `flashinferMetaInfo.h`——这个头文件里的 `tllmGenBatchedGemmList` 就是「可用的预编译 kernel 清单」：

```python
metainfo = get_artifact(f"{include_path}/{header_name}.h", meta_hash)
assert metainfo, f"{header_name}.h not found"
```

再把一组 BMM 导出头通过符号链接挂到 cubin 目录，供 C++ `#include`。最后 [jit/fused_moe.py:310-320](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe.py#L310-L320) 用 `-DTLLM_GEN_GEMM_CUBIN_PATH` 把 cubin 目录路径**烧进编译产物**：

```python
extra_cuda_cflags=[
    "-DTLLM_GEN_EXPORT_INTERFACE",
    "-DTLLM_GEN_EXPORT_FLASHINFER",
    ...
    f'-DTLLM_GEN_GEMM_CUBIN_PATH=\\"{ArtifactPath.TRTLLM_GEN_BMM}\\"',
] + nvcc_flags,
```

注意它编译的源文件清单里没有 `moe_gemm_kernels_*.cu`——那些是 CUTLASS 后端才有的；trtllm 后端编的是 `trtllm_fused_moe_runner.cu`、`trtllm_fused_moe_kernel_launcher.cu` 与一组 `fused_moe/trtllm_backend/trtllm_fused_moe_routing_*.cu`（路由 kernel），见 [jit/fused_moe.py:289-309](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe.py#L289-L309)。架构范围 `supported_major_versions=[10, 12]`（[jit/fused_moe.py:283-285](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe.py#L283-L285)）。

**加载后注册 cubin loader**：[get_trtllm_moe_sm100_module](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L1183-1187) 在 `build_and_load` 之后调用 `setup_cubin_loader`，让运行期知道去哪找 cubin：

```python
module = gen_trtllm_gen_fused_moe_sm100_module()
moe_op = module.build_and_load()
setup_cubin_loader(str(module.get_library_path()))
```

**运行期按 dtype 分派到不同 C++ 入口**：trtllm 后端的 `MoERunner.forward` 是一张 dtype 大分派表（[core.py:1431-1631](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L1431-L1631)）。BF16 走 `moe_op.trtllm_bf16_moe`、FP8 块缩放走 `trtllm_fp8_block_scale_moe`、FP8 per-tensor 走 `trtllm_fp8_per_tensor_scale_moe`、MXINT4 走 `trtllm_mxint4_block_scale_moe`、其余（NVFP4）走 `trtllm_fp4_block_scale_moe`：

```python
if self.dtype_weights == DtypeTrtllmGen.Bfloat16:
    moe_op.trtllm_bf16_moe(routing_logits, ...)
elif (self.dtype_act == E4m3 and self.dtype_weights == E4m3) or ...:
    if DeepSeekFp8 or MxFp8:
        moe_op.trtllm_fp8_block_scale_moe(...)
    else:
        moe_op.trtllm_fp8_per_tensor_scale_moe(...)
elif dtype_act == Bfloat16 and dtype_weights == MxInt4:
    moe_op.trtllm_mxint4_block_scale_moe(...)
else:
    moe_op.trtllm_fp4_block_scale_moe(...)
```

**高层 BF16 入口的形状约定**：[trtllm_bf16_moe](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L2836-L2863) 的签名揭示了 trtllm 后端与 CUTLASS 后端的另一差异——权重必须是 **BlockMajorK 重排布局** `[num_experts, K//128, Mn, 128]`，而非自然布局。这是因为 trtllm-gen kernel 的 TMA 加载要求按 128B 块排布：

```python
def trtllm_bf16_moe(
    routing_logits, routing_bias, hidden_states,
    gemm1_weights, gemm2_weights,   # 必须是 BlockMajorK 重排后的形状
    num_experts, top_k, n_group, topk_group,
    intermediate_size, local_expert_offset, local_num_experts,
    routed_scaling_factor=None, routing_method_type=0,
    use_shuffled_weight=True, weight_layout=WeightLayout.BlockMajorK,
    ...
)
```

#### 4.2.4 代码实践

**实践目标**：理解 trtllm 后端「编译启动器 + 下载 cubin」的产物机制，并与 CUTLASS 后端的「现生成 `.generated.cu`」做对比。

**操作步骤**（待本地验证，需 SM100+ Blackwell GPU）：

1. 清缓存后设置 `FLASHINFER_JIT_VERBOSE=1`，首次调用任意 trtllm 后端（如 `trtllm_bf16_moe`）。
2. 观察日志：应能看到 `get_artifact` 下载 `flashinferMetaInfo.h` 与 BMM 头/cubin 的过程，以及编译 `trtllm_fused_moe_runner.cu` 等启动器源（**不会**看到 CUTLASS 那种 `*.generated.cu`）。
3. 在 `~/.cache/flashinfer/.../cubins/` 下找到下载的 cubin 文件；在 `cached_ops/` 下找到编译出的启动器 `.so`。

**需要观察的现象**：

- 首次编译明显**比 CUTLASS 后端快**（因为只编薄启动器，不实例化大量 CUTLASS 模板），但需要联网下载 cubin（或预装 `flashinfer-cubin` 包）。
- 若设置 `FLASHINFER_NO_DOWNLOAD` 且本地无 cubin，会硬失败——这印证了 cubin 是运行期必需的外部产物。

**预期结果**：trtllm 模块名为 `fused_moe_trtllm_sm100`（[jit/fused_moe.py:287-288](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe.py#L287-L288)），与 CUTLASS 的 `fused_moe_{arch}` 命名截然不同，便于在缓存目录里一眼区分两套后端的产物。

#### 4.2.5 小练习与答案

**练习 1**：CUTLASS 后端与 trtllm 后端对「路由（topk）」的处理有何本质区别？
**答案**：CUTLASS 后端（`cutlass_fused_moe`）只接收**已算好的** `topk_ids`/`topk_weights`，路由必须在外部完成；trtllm 后端（`trtllm_bf16_moe` 等）的 C++ kernel **内置路由**，可直接吃 `routing_logits`（`FromLogits` 模式），也支持预算路由（`PackedPrecomputed`/`UnpackedPrecomputed`，对应 `*_routed_moe` 入口）。所以 trtllm 后端的融合程度更高（少一次路由 launch）。

**练习 2**：为什么 trtllm 后端的 tactic 列表用 `trtllm_get_valid_moe_configs` 查询，而不是像 CUTLASS 那样数实例化模板？
**答案**：因为 trtllm 的 GEMM kernel 是**预编译 cubin**，可用配置由 `flashinferMetaInfo.h` 里的 `tllmGenBatchedGemmList` 枚举，运行期只能「查询有哪些 cubin 可用」，不能现场实例化新的；CUTLASS 的 tactic 是 JIT 现生成的模板实例化，数量由 `generate_gemm_operations` 决定，可由 `get_gemm1/gemm2_tactic_count` 直接报出。

### 4.3 CuTe-DSL MoE 路径

#### 4.3.1 概念说明

CuTe-DSL NVFP4 MoE 是第三条独立路径，与前两套 C++ 后端有根本不同：

- **纯 Python DSL kernel**：它的 GEMM kernel 不是 C++/CUDA，而是用 NVIDIA 的 CuTe-DSL（`nvidia-cutlass-dsl` 包）以 Python 描述的 kernel，**不经 JIT 编译 C++**。这与 CLAUDE.md 强调的「CuTe-DSL kernel 依赖 `nvidia-cutlass-dsl` pip 包，与 `3rdparty/cutlass` 子模块无关」一致。
- **仅 NVFP4 + Blackwell SM100/SM103**：由 `@supported_compute_capability([100, 103])` 硬门控，只做 NVFP4 权重 + NVFP4/MXFP8 激活的 W4A4/W4A8 风格 MoE。
- **显式四段管线**：与 C++ 后端把一切塞进一个 kernel 不同，CuTe-DSL 把 MoE 拆成 `moe_sort → GEMM1+act → async memset → GEMM2+finalize` 四步，其中 memset 与 GEMM1 在不同 stream 上重叠（async-memset overlap），并为 CUDA Graph 预分配资源。
- **两种 API 形态**：函数式 `cute_dsl_fused_moe_nvfp4`（简单、支持 `autotune()` 上下文）与类式 `CuteDslMoEWrapper`（持常驻 stream/event，CUDA Graph 友好）。

> 仓库里另有一个面向 SM12x（Blackwell 消费级）的 `b12x_fused_moe`/`B12xMoEWrapper`（见 `flashinfer/fused_moe/cute_dsl/blackwell_sm12x/`），与本节的 SM100/103 NVFP4 路径并列，是 CuTe-DSL 家族在更新架构上的对应实现。

#### 4.3.2 核心流程

CuTe-DSL MoE 的核心实现 [_moe_core_impl](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L112-L157) 把一次前向明确分成四步：

```text
Step 1: moe_sort(token_selected_experts, token_final_scales, ...)
        → tile_idx_to_expert_idx, permuted_idx_to_expanded_idx, ...
Step 2: blockscaled_contiguous_gather_grouped_gemm_act_fusion_nvfp4(x, w1, ...)
        → intermediate, intermediate_sf   (GEMM1 + SwiGLU 融合)
Step 3: moe_output_memset_inplace(moe_output)   (在 aux_stream 上与 GEMM1 重叠)
Step 4: blockscaled_contiguous_grouped_gemm_finalize_fusion_nvfp4(intermediate, w2, ...)
        → moe_output   (GEMM2 + 原子 scatter finalize 融合)
```

之所以 Step 3 要清零，是因为 Step 4 的 finalize 用**原子 scatter-add** 把各专家输出累加到 `moe_output`，每次调用必须从零开始。把 memset 放到 aux stream 并用 event 同步，是为了让它与主 stream 的 GEMM1 时间重叠。

#### 4.3.3 源码精读

**架构门控**：函数式入口 [cute_dsl_fused_moe_nvfp4](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L740-L742) 与类式 `CuteDslMoEWrapper.__init__`（[fused_moe.py:380](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L380)）都挂 `@supported_compute_capability([100, 103])`：

```python
@supported_compute_capability([100, 103])
@flashinfer_api(trace=cute_dsl_fused_moe_nvfp4_trace)
def cute_dsl_fused_moe_nvfp4(x, x_sf, token_selected_experts, token_final_scales, ...):
```

**四步管线的落点**：Step 1 的 `moe_sort` 调用在 [fused_moe.py:244-253](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L244-L253)；Step 2 的 GEMM1+act 融合在 [fused_moe.py:261-286](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L261-L286)；Step 3 的 async memset 在 [fused_moe.py:302-309](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L302-L309)；Step 4 的 GEMM2+finalize 在 [fused_moe.py:312-327](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L312-L327)。memset 的注释清楚解释了「为什么要清零 + 为何放 aux stream」（[fused_moe.py:288-301](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L288-L301)）。

**tactic 由 CuTe-DSL runner 管理**：与 C++ 后端不同，这里 tactic（`gemm1_mma_tiler_mn`/`cluster_shape_mn` 等）是一组 Python 元组，候选列表 `ALL_MOE_TACTICS` 在 `tuner.py` 里定义，`AutoTuner.choose_one` 同样负责选优（[fused_moe.py:656-663](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L656-L663)）。

**模块文档对两种 API 的定位**：[cute_dsl/fused_moe.py:17-51](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L17-L51) 的模块 docstring 明确建议——简单场景/调优用函数式，生产推理 + CUDA Graph 用 `CuteDslMoEWrapper`。

#### 4.3.4 代码实践

**实践目标**：理解 CuTe-DSL 后端「不经 C++ JIT、kernel 来自 pip 包」的特征。

**操作步骤**（源码阅读型实践，无需运行）：

1. 对比 `gen_cutlass_fused_moe_module`（编译几十个 `.cu`）与 CuTe-DSL 路径：在 `flashinfer/fused_moe/cute_dsl/` 下**找不到** `gen_*_module`、`build_and_load`、`.cu` 源——它的 kernel 全部从 `nvidia-cutlass-dsl` 包导入。
2. 阅读 [_moe_core_impl](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/fused_moe.py#L112-L157)，画出四步管线及其 stream/event 同步关系。
3. 在 `tests/moe/test_cute_dsl_fused_moe.py` 中找一个调用 `cute_dsl_fused_moe_nvfp4` 的用例，确认其输入是 NVFP4 量化后的 `x`/`x_sf` 与外部算好的 `topk_ids`/`topk_weights`（即 CuTe-DSL 后端像 CUTLASS 一样吃预算路由）。

**需要观察的现象**：

- CuTe-DSL 后端不产生 `fused_moe_*` 的 JIT 模块名，因此 `flashinfer list-modules` 里看不到它——它是纯 Python + DSL runtime。
- 它的「finalize 是原子 scatter」决定了输出在 `use_fused_finalize=True` 时**非确定性**（run-to-run 可能略有差异），这一点与 CUTLASS 后端的 `use_fused_finalize` 注释（[core.py:978-982](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L978-L982)）同源。

**预期结果**：能说清「CuTe-DSL 后端 = SM100/103 专用 + 纯 Python DSL kernel + 四步显式管线 + 吃预算路由」，并指出它与 CUTLASS/trtllm 两个 C++ 后端在工程形态上的根本区别。

#### 4.3.5 小练习与答案

**练习 1**：CuTe-DSL MoE 后端为什么不出现 `@functools.cache` 装饰的 `get_*_module` 函数？
**答案**：因为它的 kernel 不是 C++/CUDA 模板、不需要 JIT 编译成 `.so`，而是由 `nvidia-cutlass-dsl` pip 包在运行期直接构造；没有 `JitSpec`、没有 `build_and_load`，自然也没有模块加载缓存。它复用的是 CuTe-DSL runtime 自身的编译/缓存机制，而非 FlashInfer 的 JIT 三层架构。

**练习 2**：CuTe-DSL 后端与 CUTLASS 后端在「吃路由」的方式上更接近哪一个？
**答案**：更接近 CUTLASS 后端——两者都接收**已算好的** `token_selected_experts`/`token_final_scales`，不在 kernel 内做路由；trtllm 后端则可内置路由。所以从 API 形态看，CUTLASS 与 CuTe-DSL 是「计算后端」，trtllm 是「路由+计算融合后端」。

## 5. 综合实践

把三个模块串起来：在同一份 `MoEConfig` 下切换后端，对比 BF16 与 FP8 的输出与耗时。

**任务背景**：u6-l1 讲过统一 API 用 `BackendOptions` 表达候选后端，每个后端配置类有 `supported(arch)` 门控（[api.py:226-339](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L226-L339)）。默认候选顺序是 trtllm 系优先、CUTLASS 兜底（[api.py:420-430](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L420-L430)）：

```python
_DEFAULT_BACKEND = BackendOptions(
    candidates=(
        TrtllmFp4Config(), TrtllmFp8BlockConfig(), TrtllmFp8PerTensorConfig(),
        TrtllmBf16Config(), TrtllmMxInt4Config(), CutlassConfig(), CuteDslConfig(),
    )
)
```

`BackendOptions.valid_for(arch)` 会过滤掉当前架构不支持的候选（[api.py:404-406](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L404-L406)）。注意各后端的架构门槛差异很大：`TrtllmBf16Config`/`TrtllmFp4Config`/`TrtllmMxInt4Config` 要 SM100+，`TrtllmFp8BlockConfig`/`TrtllmFp8PerTensorConfig` 只要 SM80+，`CutlassConfig` 恒为 `True`，`CuteDslConfig` 仅 SM100/103。

**操作步骤**（待本地验证，推荐 SM100+ Blackwell 以同时跑通 BF16 与 FP8 的 trtllm 后端；若只有 SM80/89，BF16 会落到 CUTLASS 后端、FP8 走 trtllm FP8）：

1. 构造一份**相同的** `MoEConfig`（同样的 `routing`/`experts`/`activation` 等），只改 `quant.variant`（`QuantVariant.BF16` vs `QuantVariant.DeepSeekFp8`）与 `backend` 候选。
2. 用 `MoELayer`（u6-l4 会详讲）或统一入口 `fused_moe` 跑两次前向，分别强制走 BF16 后端与 FP8 后端。
3. 用 `flashinfer.testing.bench_gpu_time`（u10-l3）或 `time` 测两次耗时；把两者输出转 float 后计算最大误差。
4. 同时记录实际命中的后端名：可设 `FLASHINFER_LOGLEVEL=1` 看 API 名，或检查编译出的模块名（`fused_moe_trtllm_sm100` = trtllm；`fused_moe_{arch}` = CUTLASS）。

**需要观察的现象与预期结果**：

- **精度**：BF16 输出应与 FP32 参考高度一致（atol 较小）；FP8 块缩放输出有 ~1e-2 量级相对误差（见 `tests/moe/trtllm_gen_fused_moe_utils.py` 里 `FP8BlockScaleMoe.get_tolerances` 返回 `{"atol": 0.1, "rtol": 0.85, "percent": 0.79}`，[utils:1368-1370](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/moe/trtllm_gen_fused_moe_utils.py#L1368-L1370)）。
- **性能**：FP8 后端在 Blackwell 上应明显快于 BF16（低精度 + 块缩放利用 tensor core 的吞吐优势）。
- **后端命中**：在 SM100 上 BF16 会优先命中 `TrtllmBf16Config`（trtllm 后端），FP8 命中 `TrtllmFp8BlockConfig`；若把 `backend` 显式限定为 `(CutlassConfig(),)`，则两者都走 CUTLASS 后端（`fused_moe_100`），可用于隔离「后端差异」与「精度差异」两个变量。

> 这个实践把本讲三个模块连成一线：你会在日志里看到 trtllm（下载 cubin）与 CUTLASS（生成 `.generated.cu`）两种截然不同的首次编译行为，并切身感受到「同 config 不同后端」的精度/性能权衡——这正是 FlashInfer「多后端选优」哲学在 MoE 上的具体落地。

## 6. 本讲小结

- FlashInfer 的融合 MoE 有**三套并立后端**：CUTLASS（OSS MoE GEMM，广架构兜底）、trtllm-gen（TRT-LLM 生成 kernel，SM100 专用、内置路由、用预编译 cubin）、CuTe-DSL（SM100/103 NVFP4，纯 Python DSL kernel）。
- **SM 分派是 CUTLASS 后端的核心机制**：`get_cutlass_fused_moe_module` 用 `device_arch` 字符串在 `sm89/sm90/sm100/sm103/sm120` 五个 gen 函数间二选一，差异只在 nvcc flags 与 `supported_major_versions`；同 major 共用一份代码（100≈110、120≈121）。
- **两套 C++ 后端的产物机制截然不同**：CUTLASS 在 JIT 时 `generate_gemm_operations` 现场实例化 CUTLASS 模板成 `*.generated.cu` 再编译；trtllm 只编译薄启动器，运行期加载/下载预编译 cubin（`flashinferMetaInfo.h` 列清单、`-DTLLM_GEN_GEMM_CUBIN_PATH` 烧路径）。
- **路由融合程度不同**：CUTLASS 与 CuTe-DSL 后端只吃预算好的 `topk_ids`/`topk_weights`（计算后端）；trtllm 后端可内置路由（`FromLogits`）也支持预算路由（`*_routed_moe`），融合度更高。
- **三套后端共用 `AutoTuner` + `TunableRunner` 框架**，但 tactic 来源不同：CUTLASS 数实例化模板、trtllm 查 `trtllm_get_valid_moe_configs`（可用 cubin）、CuTe-DSL 用 Python 元组表 `ALL_MOE_TACTICS`。
- 统一 API 用 `BackendOptions` + 每个配置类的 `supported(arch)` 做声明式门控与有序选优，默认 trtllm 优先、CUTLASS 兜底、CuTe-DSL 仅 SM100/103。

## 7. 下一步学习建议

- **u6-l3（路由方法）**：本讲多次提到 `routing_logits`/`topk_ids` 与 `RoutingInputMode`，下一讲深入 DeepSeek-V3/Llama-4/top-k 等路由算法本身，以及 `fused_topk_deepseek` 如何喂给本讲的计算后端。
- **u6-l4（量化 MoE 与 MoELayer 派发）**：本讲的「同 config 切后端」在 `MoELayer` 里被自动化——下一讲讲 `MoELayer` 如何作为状态化跨后端派发器、`runners.py` 的 runner 适配器如何对接 autotune。
- **u9-l4（AOT 与预编译包）**：本讲看到 trtllm 后端依赖在线 cubin 下载，u9-l4 会讲 `flashinfer-cubin`/`flashinfer-jit-cache` 两个预编译包如何把这套依赖离线化。
- **源码延伸阅读**：`csrc/fused_moe/cutlass_backend/flashinfer_cutlass_fused_moe_binding.cu`（CUTLASS 后端 TVM-FFI 导出）、`csrc/trtllm_fused_moe_runner.cu`（trtllm cubin 加载与启动）、`flashinfer/fused_moe/cute_dsl/tuner.py`（CuTe-DSL tactic 表），对照本讲可形成「Python → C++/DSL」的完整闭环。
