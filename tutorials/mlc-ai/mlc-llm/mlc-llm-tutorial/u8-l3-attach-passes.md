# 运行时函数附加 pass

## 1. 本讲目标

本讲深入 MLC LLM 编译流水线中的「附加类 pass（attach passes）」。学完后你应该能够：

- 说清「附加（attach）」与上一讲的「派发（dispatch）」**本质区别**：派发改写已存在的占位调用，附加则是**往 IRModule 里新增**一个全新函数，留给运行期 C++ 引擎按名字调用。
- 读懂四个附加 pass 的源码：`AttachGPUSamplingFunc`（GPU 采样）、`AttachLogitProcessFunc`（logit 偏置/惩罚/掩码）、`AttachSpecDecodeAuxFuncs`（推测解码辅助）、`AttachAllocEmbeddingTensorFunc`（embedding 张量分配）。
- 理解为什么这些函数**必须在编译期生成**，而不是写死在运行期：形状随 target 变、索引表达式需被 TVM 优化、要融入 CUDA graph。
- 串起「编译期生成函数 → 导出模型库 → 运行期 FunctionTable 按名查找」这条 Python↔C++ 契约链。

承接 [u7-l2](./u7-l2-pass-pipeline-overview.md)（五阶段流水线总览）与 [u8-l2](./u8-l2-dispatch-passes.md)（派发 pass）。那里建立了「编译期记下、运行期消费」的总框架，本讲打开 Phase 0 里最典型的一组「纯新增」pass。

## 2. 前置知识

### 2.1 「附加」是什么：不碰旧代码，只加新函数

[u8-l2](./u8-l2-dispatch-passes.md) 的派发 pass 做的是**改写**：模型代码里已经有一个 `mlc.triton.*` 占位调用，派发 pass 把它换成具体的 `call_tir`。它们处理的是「模型层发出、但没绑定实现」的调用。

附加 pass（attach passes）则**完全不同**：

- 模型代码里**根本没有**这些函数——采样、logit 偏置、推测解码的概率搬运，都不是模型架构（Transformer 前向）的一部分，而是**推理引擎**的服务职责。
- 这些函数也不该写死在 C++ 引擎里：它们的内部是「按词表大小分块的规约」「按 target 选线程块大小」这类**与形状、与硬件强相关**的计算，最适合用 TIR 写出来、交给 TVM 编译优化。
- 于是折中：**编译期**用 TIR/Relax 生成这些函数、塞进 IRModule、随模型库一起导出；**运行期** C++ 引擎用 `mod->GetFunction("函数名")` 按名取出调用。

一句话：**派发是「替换」，附加是「增添」**。附加 pass 执行后，IRModule 的函数数量是严格增加的。

### 2.2 为什么不直接在运行期用 C++ 写这些 kernel

这是本讲的核心问题，先把答案摆出来（后面每个 pass 都会印证）：

| 理由 | 说明 |
|---|---|
| **形状/target 特化** | 词表大小、batch、线程块上限都随模型与硬件变。编译期把这些 bake 成常量或上界，TVM 能据此做循环展开、向量化、shared memory 规划 |
| **复用 TVM 的优化与调度** | `chunk_lse` 这种分块 log-sum-exp，靠 TVM 的 dlight 调度自动绑线程轴、做 warp 归约，比手写 CUDA 更易维护 |
| **融入 CUDA graph** | 这些采样/规约 kernel 要进 CUDA graph 整图捕获（见 [u8-l4](./u8-l4-lowbatch-memory-pipeline.md)），必须是 VM 可调度的 Relax/TIR 函数，而非外部裸指针调用 |
| **跨后端复用** | 同一份 TIR 既能在 cuda 上绑 `threadIdx`，也能在 llvm 上 `T.serial`，靠 `target.kind.name` 选不同写法 |
| **内存规划** | 函数的输出张量要进 `StaticPlanBlockMemory` 的显存池复用，前提是它是 IRModule 里「可见」的 Relax 函数 |

### 2.3 关键 TVM 概念速查

| 概念 | 含义 |
|---|---|
| `IRModule` | 整个模型的中间表示，装着一组 Relax 函数 + TIR PrimFunc |
| `@tvm.transform.module_pass` | 把类变成「作用于整个 IRModule 的 pass」，需实现 `transform_module(mod, ctx)` |
| `BlockBuilder` | 用「开函数 → 开 dataflow → emit → 收尾」的命令式风格**新建** Relax 函数的构建器 |
| `bb.add_func(prim_func, name)` | 把一个 TIR PrimFunc 以 `name` 注册进 IRModule（`attach_sampler` 大量用此） |
| `call_tir` / `call_tir_inplace` | Relax 调用 TIR 函数的方式；`inplace` 版允许把输出写回某个输入张量 |
| `T.prim_func(s_tir=True)` | 新版 TIR DSL（tirx）写 PrimFunc 的装饰器，本讲所有 kernel 长这样 |
| `tir_var_upper_bound` / `tir_non_negative_var` | 附加在函数上的属性：声明某符号变量的**上界**与**非负性**，供调度器与下游 pass 使用 |
| `target.kind.name` | 取 target 名字（`cuda`/`llvm`/`vulkan`/`metal`/`webgpu`），attach pass 据此决定生成哪种实现 |

### 2.4 四个 pass 在流水线里的位置

它们**全部在 Phase 0**（附加元数据与运行时函数），紧挨着排成一串（[pipeline.py:L107-L116](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L107-L116)）：

```python
DispatchKVCacheCreation(target, flashinfer, metadata),
AttachSoftmaxWithTemperature(target, metadata),   # 同族：softmax 带 temperature
...
AttachLogitProcessFunc(target),                    # 本讲 4.3
AttachAdditionalPrimFuncs(additional_tirs),
AttachAllocEmbeddingTensorFunc(metadata),          # 本讲 4.5
AttachGPUSamplingFunc(target, variable_bounds),    # 本讲 4.2
AttachSpecDecodeAuxFuncs(tensor_parallel_shards),  # 本讲 4.4
```

为什么都在 Phase 0？因为这些新增函数**要参与后续所有阶段**：Phase 2 的 `LegalizeOps`/`FuseOps`、Phase 4 的 dlight 调度与内存规划、CUDA graph 捕获——它们必须最早进 IRModule。这与 `DispatchKVCacheCreation` 同理（详见 [u8-l2](./u8-l2-dispatch-passes.md) 的 4.3.5）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `python/mlc_llm/compiler_pass/attach_sampler.py` | 附加 GPU 采样相关函数（multinomial / argsort / top-p 采样 / draft 校验 等） |
| `python/mlc_llm/compiler_pass/attach_logit_processor.py` | 附加 logit 处理函数（logit bias / presence+frequency+repetition penalty / 词表掩码） |
| `python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py` | 附加推测解码辅助函数（概率 scatter/gather、hidden states 搬运） |
| `python/mlc_llm/compiler_pass/attach_embedding_allocator.py` | 附加 embedding 张量分配函数（同族代表，作为对照） |
| `python/mlc_llm/compiler_pass/pipeline.py` | 四个 pass 在 Phase 0 的装配位置 |
| `python/mlc_llm/op/top_p_pivot.py` | top-p 截断的 pivot 二分 + 重归一化 TIR 生成器（被采样 pass 调用） |
| `python/mlc_llm/op/batch_spec_verify.py` | 推测解码的 draft token 校验 TIR 生成器（被采样 pass 调用） |
| `cpp/serve/function_table.cc` | **运行期消费侧**：按名字 `GetFunction(...)` 取出这些附加函数 |
| `cpp/serve/sampler/gpu_sampler.cc` | **运行期消费侧**：GPU 采样器调用附加出的采样函数 |
| `cpp/serve/logit_processor.cc` | **运行期消费侧**：C++ 端调用附加出的 logit 处理函数 |

## 4. 核心概念与源码讲解

### 4.1 附加 pass 的统一形态

#### 4.1.1 概念说明

四个附加 pass 长得几乎一模一样，可以抽象成一个「附加 pass 模板」：

- 是个 `@tvm.transform.module_pass`，`__init__` 吃 `target`（或 `metadata`）；
- `transform_module` 里 **`mod.clone()`** 或直接用 `BlockBuilder(mod)` 接管模块；
- 根据 `target.kind.name` 选不同实现（GPU 版绑线程轴、CPU 版走 `T.serial`/`range`）；
- 用 `bb.add_func(prim_func, "函数名")` 或 `with bb.function(...)` 往模块里**塞新函数**；
- 返回扩充后的 `mod`。

它们和派发 pass 的外形差异在于：派发 pass 通常用一个 `_Rewriter(PyExprMutator)` **遍历改写**已有表达式；附加 pass 则是「照着图纸造一批新函数塞进去」，**不遍历、不改写模型前向**。

#### 4.1.2 核心流程

```text
AttachXxxPass.__init__(target / metadata):
    记下 target / metadata

transform_module(mod, ctx):
    mod = mod.clone()                         # 防止污染上游
    按 target.kind.name 选实现（CPU/GPU/WebGPU/...）
    for 要附加的每个函数 f:
        bb.add_func(f, name=f_name)           # 或 with bb.function(name): ...
    return bb.finalize()  /  return mod
```

四个 pass 的「图纸」各不相同，下面逐个拆。

#### 4.1.3 源码精读：以 `AttachAllocEmbeddingTensorFunc` 为最小范例

这个 pass 最小，先用它建立「附加」的直觉。它的全部职责：如果模型有 `embed` 函数，就额外附加一个**无参**函数 `alloc_embedding_tensor`，运行期调一次它就拿到一块预分配好的 embedding 缓冲。

pass 类与主流程：[attach_embedding_allocator.py:L9-L39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_embedding_allocator.py#L9-L39)。核心三步：

```python
# 1. 找到模型的 embed 函数，取其输出形状最后一维 = hidden_size
for gv, func in mod.functions_items():
    if gv.name_hint == "embed":
        embed_func = func
if embed_func is None:
    return mod                                # 没有就 no-op，不报错

hidden_size = embed_func.ret_ty.shape[-1]
# 2. 用 BlockBuilder 新建一个无参函数
with bb.function("alloc_embedding_tensor", []):
    bb.emit_func_output(
        bb.emit(relax.op.builtin.alloc_tensor(
            relax.ShapeExpr([self.metadata["prefill_chunk_size"], hidden_size]),
            dtype, runtime_device_index=0,
        ))
    )
```

注意它的两个特征——**第一，条件附加**：`embed_func is None` 时直接 `return mod`，即「模型不提供 embed 就什么都不加」。这是附加 pass 的通用容错风格（对照后面 `prefill_to_last_hidden_states` 不存在时跳过 hidden states 附加）。**第二，吃 metadata**：缓冲的第一维是 `prefill_chunk_size`，这个值来自编译期 metadata，所以分配大小在编译期就定死。

> 同族的 `AttachSoftmaxWithTemperature`（[attach_softmax_with_temperature.py:L14-L28](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_softmax_with_temperature.py#L14-L28)）把一次性 softmax 改写成「分块求 log-sum-exp + 合并」的两阶段 kernel，原理与本讲四个 pass 一致——为数值稳定性与 GPU 并行度，编译期生成专门的 TIR。它略偏「改写」，本讲不展开，留作扩展阅读。

#### 4.1.4 代码实践

**实践目标**：确认「附加」是纯新增、不改旧函数。

**操作步骤**（源码阅读型）：

1. 看 [attach_embedding_allocator.py:L19-L24](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_embedding_allocator.py#L19-L24)：遍历 `mod.functions_items()` 只为**查找** `embed`，没有任何对它的改写。
2. 设想一个 IRModule 含 `prefill`、`decode`、`embed` 三个函数，运行此 pass 后函数数量从 3 变成几个？
3. 对照 [u8-l2](./u8-l2-dispatch-passes.md) 的 `DispatchTritonKernel`：它用 `PyExprMutator` 遍历改写 `mlc.triton.*` 调用点，函数总数不一定增加（可能复用）。说出「附加」与「派发」各自动作集的差别。

**预期结果**：

- 第 2 步：从 3 个变成 4 个，新增 `alloc_embedding_tensor`；原三个函数一字未改。
- 第 3 步：派发的动作集 = {遍历、匹配、替换}；附加的动作集 = {查条件、新建函数、注册}。派发可能「不增函数只换实现」，附加「必然增函数」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `alloc_embedding_tensor` 要做成一个**无参函数**，而不是在 C++ 里直接 `malloc`？

**参考答案**：返回的张量要进入 TVM 的 `StaticPlanBlockMemory` 显存池、参与 CUDA graph 捕获与生命周期管理（KillAfterLastUse）。用 `relax.op.builtin.alloc_tensor` 让这块内存由 VM 统一规划与复用，而不是游离的 C++ 堆分配；同时 `prefill_chunk_size` 在编译期已知，可静态确定形状。

**练习 2**：附加 pass 与派发 pass 在 Phase 0/Phase 1 的分布有何规律？

**参考答案**：附加 pass（采样、logit、spec decode、embedding、softmax）**全在 Phase 0**，因为新增函数要参与后续所有优化与内存规划；派发 pass 多在 Phase 1（KV cache 创建除外，因它要写 metadata 而被提前到 Phase 0，见 [u8-l2](./u8-l2-dispatch-passes.md) 4.3.5）。

---

### 4.2 GPU 采样函数附加：`AttachGPUSamplingFunc`

#### 4.2.1 概念说明

采样（sampling）是把模型输出的 logits 概率分布变成一个具体 token id 的过程。MLC 支持多种采样策略：

- **multinomial（多项采样）**：按概率随机抽一个 token，即最普通的随机采样；
- **top-p（nucleus sampling）**：只从累计概率达 `p` 的最小 token 集合里抽，靠 `argsort` 排序 + 截断实现；
- **draft token 校验**：推测解码里，大模型校验小模型起草的 token 是否可接受（见 [u10-l4](./u10-l4-speculative-decoding.md)）。

MLC 在 C++ 端有 CPU 与 GPU 两套采样器（详见 [u10-l3](./u10-l3-sampler.md)）。GPU 采样器不把这些 kernel 写死在 C++ 里，而是**期望模型库里就带有**一组按名的采样函数。`AttachGPUSamplingFunc` 就是负责在编译期把它们造出来塞进去的 pass。

#### 4.2.2 核心流程

```text
AttachGPUSamplingFunc.__init__(target, variable_bounds):
    max_batch_size = variable_bounds["batch_size"]
    self.variable_bounds = {                    # 给符号变量配上界
        "batch_size":   max_batch_size,
        "num_samples":  max_batch_size,
        "num_positions": 6 * max_batch_size,    # RWKV -1 max_seq_len 需特别处理
    }
    self.non_negative_var = ["vocab_size"]

transform_module(mod, ctx):
    if target_kind not in {cuda, vulkan, metal, webgpu}:
        return mod                              # 非 GPU 后端：不附加，留给 CPU 采样器
    bb = BlockBuilder(mod)
    if target_kind == webgpu:
        附加 [argsort, sample_with_top_p]        # WebGPU 只附加不含 int8 的函数
    else:
        附加 [multinomial, argsort, sample_with_top_p,
              take_probs, batch_verifier, renormalize_by_top_p]
    for 每个 gv_name:
        给函数打上 tir_var_upper_bound / tir_non_negative_var 属性
    return bb.finalize()
```

注意三个设计点：

1. **target 守卫**：只在 `cuda/vulkan/metal/webgpu` 上附加。`llvm`（CPU）直接 `return mod`——因为 CPU 走的是 C++ 端的 `cpu_sampler`，不需要这些 GPU kernel。
2. **WebGPU 子集**：WebGPU 不支持 i8s 运算，故只附加两个不含 int8 的函数（`argsort_probs`、`sample_with_top_p`），其余四个跳过。
3. **变量上界属性**：附加后给每个函数打 `tir_var_upper_bound`，让下游调度器（dlight）知道 `batch_size` 等符号变量的上限，从而能选出更优线程配置。

#### 4.2.3 源码精读

pass 类与 target 分发：[attach_sampler.py:L14-L66](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L14-L66)。重点看 L31-L34 的守卫与 L37-L57 的两分支附加清单。

六个被附加的函数（非 WebGPU 全集）及其作用：

| 函数名 | 生成器 | 作用 |
|---|---|---|
| `multinomial_from_uniform` | `_attach_multinomial_sampling_func` | 用一组均匀随机数对概率分布做多项采样 |
| `argsort_probs` | `_attach_argsort_func` | 把概率按降序排序，返回 (排序后概率, 排序索引) |
| `sample_with_top_p` | `_attach_sample_with_top_p` | 在已排序分布上做 top-p / top-k 截断采样 |
| `sampler_take_probs` | `_attach_take_probs_func` | 采样后取出被选 token 的概率与 top 概率（用于返回 logprobs） |
| `sampler_verify_draft_tokens` | `_attach_batch_verifier` | 推测解码：校验 draft token 是否接受 |
| `renormalize_by_top_p` | `_attach_renormalize_by_top_p` | top-p 截断后对保留集合重归一化 |

以最典型的 `multinomial_from_uniform` 为例看「附加函数怎么构造」：[attach_sampler.py:L69-L115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L69-L115)。

```python
batch_size = tirx.Var("batch_size", "int64")
vocab_size = tirx.Var("vocab_size", "int64")
probs = relax.Var("probs", relax.TensorType((batch_size, vocab_size), "float32"))
uniform_samples = relax.Var("uniform_samples", relax.TensorType((num_samples,), "float32"))
sample_indices = relax.Var("sample_indices", relax.TensorType((num_samples,), "int32"))
with bb.function("multinomial_from_uniform", [probs, uniform_samples, sample_indices]):
    with bb.dataflow():
        ...
        result_tensor = nn.multinomial_from_uniform(
            probs_tensor, uniform_samples_tensor, sample_indices_tensor, "int32", ...)
        ...
    gv = bb.emit_func_output(output)
return gv
```

要点：

- 函数签名里 `batch_size`/`vocab_size` 都是**符号变量**（`tirx.Var`），形状在运行期才确定——这正是后续要打 `tir_var_upper_bound` 的原因。
- 内部把 relax 张量 `nn.wrap_nested` 包成 nn 层张量，再调用 `nn.multinomial_from_uniform`（TVM nn 内置算子），最后 reshape 回原形状。
- 整个函数返回一个 `gv`（GlobalVar），`transform_module` 里收集这些 `gv.name_hint`，再统一打属性。

再看一个直接用 TIR 写的：`renormalize_by_top_p`，它调用了独立的 op 生成器：[attach_sampler.py:L229-L255](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L229-L255)。

```python
with bb.function("renormalize_by_top_p", [probs, top_p, init_pivots]):
    with bb.dataflow():
        cutoff_output = bb.emit(relax.call_tir(
            bb.add_func(top_p_pivot(num_pivots, target), "top_p_pivot_cutoff"),
            args=[probs, top_p, init_pivots],
            out_ty=[top_p.ty, top_p.ty],
        ))
        ...
        renormalized_probs = bb.emit_output(relax.call_tir(
            bb.add_func(top_p_renorm(target), "top_p_renorm_after_cutoff"),
            args=[probs, final_pivot, renorm_sum], out_ty=probs.ty,
        ))
```

这里 `top_p_pivot` 与 `top_p_renorm` 是 [top_p_pivot.py:L11](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/top_p_pivot.py#L11) / [top_p_pivot.py:L268](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/top_p_pivot.py#L268) 提供的 TIR 生成器，按 `num_pivots`（pivot 二分个数，固定 3）和 `target` 即时生成一个 PrimFunc，再用 `bb.add_func` 注册进 IRModule。这把「top-p 截断用 pivot 二分逼近」的算法细节完全留在了 `op/` 目录，attach pass 只负责拼装。

draft token 校验函数则调用 [batch_spec_verify.py:L8](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/batch_spec_verify.py#L8) 的 `batch_spec_verify(vocab_size)`，并用 `call_tir_inplace` 把结果就地写回 `model_probs` 与 `token_tree_parent_ptr`，避免额外显存分配：[attach_sampler.py:L346-L390](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L346-L390)。

最后看属性附加：[attach_sampler.py:L60-L65](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L60-L65)。

```python
for gv_name in gv_names:
    mod[gv_name] = (
        mod[gv_name]
        .with_attr("tir_var_upper_bound", self.variable_bounds)
        .with_attr("tir_non_negative_var", self.non_negative_var)
    )
```

这让 Phase 4 的 dlight 调度知道「`batch_size` 不会超过 `max_batch_size`、`vocab_size` 非负」，从而敢于选更激进的分块策略。

**运行期怎么消费**：在 [function_table.cc:L269-L293](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L269-L293)，`FunctionTable` 初始化时（仅当 `Sampler::SupportGPUSampler` 成立）用 `mod->GetFunction("multinomial_from_uniform", true).value_or(...)` 等按名取出这些函数存进成员变量；之后 GPU 采样器在 [gpu_sampler.cc:L587](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L587)、[gpu_sampler.cc:L596](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L596)、[gpu_sampler.cc:L620](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L620) 直接调用它们。注意 `value_or(Function(nullptr))` + 构造时的 `defined()` 检查（[gpu_sampler.cc:L66-L69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L66-L69)）：如果模型库里没附加这些函数（比如编译成了 `llvm`），取出来就是空，C++ 端据此判断「这个模型不支持 GPU 采样」并退回 CPU 采样器。这就是「编译期可选附加 → 运行期按存在性选路」的契约。

#### 4.2.4 代码实践（本讲指定实践任务）

**实践目标**：列出 `attach_sampler.py` 附加的全部采样相关函数，并说清它们为何要在编译期而非运行期生成。

**操作步骤**：

1. 打开 [attach_sampler.py:L46-L57](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L46-L57)，逐个写出非 WebGPU 分支附加的 6 个函数名（`gv_names`）。
2. 对每个函数，定位其生成器函数（如 `multinomial_from_uniform` ← `_attach_multinomial_sampling_func`），用一句话描述输入输出。
3. 回答「为何编译期生成」，逐条对照本讲 2.2 节的五条理由，给每个函数指出它最依赖的那条（如 `renormalize_by_top_p` 依赖「形状/target 特化」，因为它调用的 `top_p_pivot` 接受 `target` 参数生成不同 TIR）。
4. 验证契约：查 [function_table.cc:L270-L273](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L270-L273)，确认 C++ 端用的 `GetFunction` 名字与第 1 步列出的**完全一致**。

**需要观察的现象 / 预期结果**：

- 6 个函数：`multinomial_from_uniform`、`argsort_probs`、`sample_with_top_p`、`sampler_take_probs`、`sampler_verify_draft_tokens`、`renormalize_by_top_p`（WebGPU 仅前两个中的 `argsort_probs` + `sample_with_top_p`）。
- 「为何编译期」的核心答案：这些 kernel 的形状（`vocab_size`、`batch_size`）随模型与请求变、且其内部循环结构需要被 TVM 调度（绑 `threadIdx`/warp 归约）并融入 CUDA graph，写死在 C++ 里既无法跨后端复用也进不了显存池；编译期生成还能把 `max_batch_size` 等上界作为属性喂给下游调度器。

> 待本地验证：用 `--target cuda --debug-dump <dir>` 编译一个模型，在 `debug-phase0.py` 里搜索这 6 个函数名，确认它们在 Phase 0 结束时已出现在 IRModule 中；再换 `--target llvm`，确认这些函数**不存在**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 WebGPU 只附加 `argsort_probs` 与 `sample_with_top_p` 两个函数？

**参考答案**：[attach_sampler.py:L37-L45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L37-L45) 注释写明「WebGPU 只附加不含 i8s 的函数」。其余四个函数的 TIR 里用到了 8 位整数相关运算，WebGPU/WGSL 对此支持不佳，故舍弃，运行期 WebGPU 路径只用排序 + top-p 采样的组合完成采样。

**练习 2**：`AttachGPUSamplingFunc.__init__` 里 `num_positions` 为何设成 `6 * max_batch_size`？

**参考答案**：见 [attach_sampler.py:L19-L25](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L19-L25) 的注释——为兼容 RWKV 这类含 `-1`（无限）`max_seq_len` 的工作负载，用一个与 batch 相关的有限上界 `6 * max_batch_size` 代替，使符号变量有界可调度。`sampler_take_probs` 里 `num_positions` 正是 top 概率位置数，与采样返回的 logprobs 上限挂钩。

---

### 4.3 logit 处理附加：`AttachLogitProcessFunc`

#### 4.3.1 概念说明

logit 处理指在采样**之前**对 logits 矩阵（`[batch, vocab_size]`）做的三类就地调整：

- **logit bias**：给指定 `(seq, token_id)` 位置加一个偏置（强制鼓励/禁止某些 token）；
- **penalty（惩罚）**：OpenAI 协议里的 `presence_penalty`、`frequency_penalty`、`repetition_penalty`——根据已出现 token 的次数压低其概率，避免重复；
- **bitmask（词表掩码）**：用位图标记哪些 token 允许出现，未标记的置为 `-inf`（即 logit 最小值），用于约束解码（与 [xgrammar](https://github.com/mlc-ai/xgrammar) 结构化输出配合）。

这三个操作都是**逐 token、逐位置的散列写**——`logits[seq_id, token_id] += ...`，典型的 gather/scatter 访问。它们同样不应写死 C++：批大小、词表大小、线程块上限都随环境变，且要进 CUDA graph。

#### 4.3.2 核心流程

```text
AttachLogitProcessFunc.__init__(target):
    self.target = target

transform_module(mod, ctx):
    mod = mod.clone()
    if target.kind.name == "llvm":
        mod["apply_logit_bias_inplace"]  = _get_apply_logit_bias_inplace_cpu()
        mod["apply_penalty_inplace"]     = _get_apply_penalty_inplace_cpu()
        mod["apply_bitmask_inplace"]     = _get_apply_bitmask_inplace_cpu()
    else:
        mod["apply_logit_bias_inplace"]  = _get_apply_logit_bias_inplace(target)   # 绑线程轴
        mod["apply_penalty_inplace"]     = _get_apply_penalty_inplace(target)
        mod["apply_bitmask_inplace"]     = _get_apply_bitmask_inplace(target)
    return mod
```

与采样 pass 不同，这里**不论 target 都会附加这三个函数**（CPU 用 `T.serial`，GPU 用 `thread_binding`）。因为 logit 处理对 CPU 后端同样必要——CPU 推理也要支持惩罚与掩码。

#### 4.3.3 源码精读

pass 类与 CPU/GPU 分支：[attach_logit_processor.py:L13-L38](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_logit_processor.py#L13-L38)。注意它直接用 `mod["名字"] = prim_func` 的字典写法注册——和 `bb.add_func` 等价，都是往 IRModule 塞一个以 `global_symbol` 为键的函数。

看 GPU 版的线程块自适应：[attach_logit_processor.py:L72-L109](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_logit_processor.py#L72-L109)。

```python
tx = 1024                                          # 默认每块 1024 线程
max_num_threads_per_block = get_max_num_threads_per_block(target)
tx = min(tx, max_num_threads_per_block)            # 按 target 上限钳制
check_thread_limits(target, bdx=tx, bdy=1, bdz=1)  # 校验是否越界

@T.prim_func(s_tir=True)
def _apply_logit_bias_inplace(var_logits, var_pos2seq_id, var_token_ids, var_logit_bias) -> None:
    ...
    for p0 in T.thread_binding(0, (num_token + tx - 1) // tx, "blockIdx.x"):
        for p1 in T.thread_binding(0, tx, "threadIdx.x"):
            with T.sblock("block"):
                vp = T.axis.spatial(num_token, p0 * tx + p1)
                T.where(p0 * tx + p1 < num_token)
                logits[pos2seq_id[vp], token_ids[vp]] += logit_bias[vp]
```

这是「为何编译期生成」最直观的例子：`tx`（每块线程数）**取决于 target**——CUDA 一般 1024，某些后端更小。如果写死在 C++ 里就得手写多份；编译期生成则用 `get_max_num_threads_per_block(target)`（[max_thread_check.py:L6](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/max_thread_check.py#L6)）查 target 上限、用 `check_thread_limits` 校验合法性（[max_thread_check.py:L18](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/max_thread_check.py#L18)），把 `tx` 直接编进 `thread_binding`，TVM 据此 lower 出最优的线程网格。

惩罚函数实现了 OpenAI 三种惩罚的复合公式（[attach_logit_processor.py:L190-L205](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_logit_processor.py#L190-L205)）：

\[
\text{logit} \;\mathrel{-}=\; \text{presence} + \text{count} \cdot \text{frequency}
\]

\[
\text{logit} \;\leftarrow\;
\begin{cases}
\text{logit} \cdot \text{repetition} & \text{若 } \text{logit} < 0 \\
\text{logit} \,/\, \text{repetition} & \text{若 } \text{logit} \geq 0
\end{cases}
\]

其中 `penalties` 是 `[num_seq, 3]` 的张量，三列分别是 presence/frequency/repetition。bitmask 函数则把位图 `bitmask[seq, word]` 按 `>> (vv % 32)) & 1` 解包，命中位为 1 保留原值、为 0 置 `min_value("float32")`（即 `-inf`，采样概率归零）：[attach_logit_processor.py:L273-L283](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_logit_processor.py#L273-L283)。

**运行期消费**：[logit_processor.cc:L77-L81](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/logit_processor.cc#L77-L81) 用硬编码字符串 `"apply_logit_bias_inplace"` / `"apply_penalty_inplace"` / `"apply_bitmask_inplace"` 从模型库取函数，若取不到就报错 `"Function ... not found in model"`。这正是编译期与运行期靠「函数名字符串」订立的契约——任何一边改名另一边必须同步。

#### 4.3.4 代码实践

**实践目标**：理解「同一名函数，CPU/GPU 两份实现」的按 target 切换机制。

**操作步骤**：

1. 对照 [attach_logit_processor.py:L41-L69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_logit_processor.py#L41-L69)（CPU 版 `apply_logit_bias_inplace`，用 `for i in range(num_token)`）与 [attach_logit_processor.py:L78-L109](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_logit_processor.py#L78-L109)（GPU 版，用 `T.thread_binding`）。找出两者**计算逻辑**与**循环结构**的差别。
2. 在 GPU 版里定位 `tx` 是怎么被钳制的（`min(tx, max_num_threads_per_block)`），并说明若不钳制、在每块线程上限 < 1024 的后端上会怎样。
3. 验证三个函数的 `global_symbol` 与 C++ 端查找名一致：对照 [logit_processor.cc:L77-L81](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/logit_processor.cc#L77-L81)。

**预期结果**：

- 计算逻辑完全相同（同一句 `logits[pos2seq_id[i], token_ids[i]] += logit_bias[i]`），差别只在循环：CPU 用 `range`（标量串行/向量化），GPU 用两层 `thread_binding`（外层 `blockIdx.x`、内层 `threadIdx.x`）+ `T.where` 处理尾部。
- 不钳制 `tx` 则 `thread_binding(0, tx, "threadIdx.x")` 在线程上限 < 1024 的后端会越界，`check_thread_limits` 会抛错；钳制后 `tx` 适应当前硬件。

> 待本地验证：分别用 `--target cuda` 与 `--target llvm` 编译，`--debug-dump` 看 `apply_penalty_inplace` 的 IR，确认前者带 `thread_binding` 而后者是纯 `serial`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 logit 处理三个函数 CPU 与 GPU 都附加，而采样函数只在 GPU 后端附加？

**参考答案**：logit 处理是采样前的通用预处理，CPU 与 GPU 推理都需要（惩罚、掩码、bias 是服务端协议要求），故两份实现都给。而 GPU 采样 kernel（`multinomial`、`argsort` 等）是为 GPU 并行优化的，CPU 走的是 C++ 端独立的 `cpu_sampler`（不调用这些函数），故 CPU 后端不附加，省得 IRModule 里塞无用函数。

**练习 2**：bitmask 函数里 `bitmask` 的形状为什么是 `(batch_size, (vocab_size + 31) // 32)`？

**参考答案**：每个 32 位整数可编码 32 个 token 的允许/禁止（1 位代表 1 个 token）。词表大小 `vocab_size` 需要 `(vocab_size + 31) // 32` 个整数向上取整。查询时用 `(bitmask[seq, vv // 32] >> (vv % 32)) & 1` 取第 `vv` 个 token 的允许位，见 [attach_logit_processor.py:L279-L282](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_logit_processor.py#L279-L282)。

---

### 4.4 推测解码辅助函数附加：`AttachSpecDecodeAuxFuncs`

#### 4.4.1 概念说明

推测解码（speculative decoding，详见 [u10-l4](./u10-l4-speculative-decoding.md)）用小模型起草多个 token、大模型批量校验，能显著提升吞吐。它的运行期流程里有一些**「数据搬运」**性质的工作：

- **概率 scatter/gather**：把按 batch 收集的概率分布散列到全局缓冲、或反向取回；
- **hidden states 搬运**：EAGLE 等方案需要把 prefill 的最后隐状态（last hidden states）在 draft 模型与主模型之间搬动，尤其多 GPU 张量并行时要把索引广播到所有卡。

这些搬运 kernel 同样按 target/张量并行度不同而不同，故编译期生成。这个 pass 的特别之处是它要**读 IRModule 里已有的函数**（`prefill_to_last_hidden_states`）来推断 hidden states 的 dtype——附加的不是凭空造，而是要看模型提供了什么。

#### 4.4.2 核心流程

```text
AttachSpecDecodeAuxFuncs.__init__(tensor_parallel_shards):
    self.tensor_parallel_shards = tensor_parallel_shards

transform_module(mod, ctx):
    mod = mod.clone()
    bb = BlockBuilder(mod)
    bb.add_func(scatter_2d_inplace("float32", "scatter_probs"), "scatter_probs")
    bb.add_func(gather_2d_inplace("float32", "gather_probs"),   "gather_probs")
    if "prefill_to_last_hidden_states" in mod:                    # 模型提供 hidden states 才附加
        dtype = mod["prefill_to_last_hidden_states"].ret_ty.fields[0].dtype
        _add_gather_hidden_states (bb, tensor_parallel_shards, dtype)
        _add_scatter_hidden_states(bb, tensor_parallel_shards, dtype)
    return bb.finalize()
```

两个设计点：

1. **条件附加**：只有模型定义了 `prefill_to_last_hidden_states`（EAGLE/推测解码需要）才附加 hidden states 相关函数，否则只附加概率搬运——这是「按模型能力按需附加」。
2. **张量并行感知**：`tensor_parallel_shards > 1` 时，hidden states 搬运要先 `relax.op.ccl.broadcast_from_worker0(indices)` 把索引广播到所有 worker，保证各卡一致。

#### 4.4.3 源码精读

pass 类与主流程：[attach_spec_decode_aux_funcs.py:L9-L35](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py#L9-L35)。`tensor_parallel_shards` 来自 metadata（`pipeline.py` L100 `metadata.get("tensor_parallel_shards", 1)`），即 convert_weight/compile 时定的张量并行度。

最底层的搬运 kernel `_scatter_2d` / `_gather_2d`：[attach_spec_decode_aux_funcs.py:L38-L71](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py#L38-L71)。

```python
@T.prim_func(s_tir=True)
def _scatter_2d(var_src: T.handle, var_indices: T.handle, var_dst: T.handle):
    T.func_attr({"global_symbol": global_symbol, "tirx.noalias": True})
    batch_size, m, n = T.int32(), T.int32(), T.int32()
    src     = T.match_buffer(var_src,     (batch_size, n), dtype)
    indices = T.match_buffer(var_indices, (batch_size,),  "int32")
    dst     = T.match_buffer(var_dst,     (m, n),         dtype)
    for b, j in T.grid(batch_size, n):
        with T.sblock("scatter_2d"):
            vb, vj = T.axis.remap("SS", [b, j])
            dst[indices[vb], vj] = src[vb, vj]      # 按索引散列
```

这是典型的「按索引写」散列操作——`dst[indices[b], j] = src[b, j]`，gather 反之 `dst[b, j] = src[indices[b], j]`。注意 `T.axis.remap("SS", ...)` 声明两个空间轴、`T.grid(batch_size, n)` 二维并行——TVM 会按 target 自动调度成线程网格。

张量并行感知的 Relax 封装：[attach_spec_decode_aux_funcs.py:L74-L97](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py#L74-L97)。

```python
def _add_scatter_hidden_states(bb, tensor_parallel_shards, dtype):
    ...
    with bb.function("scatter_hidden_states", [src, indices, dst]):
        with bb.dataflow():
            if tensor_parallel_shards > 1:
                indices = relax.op.ccl.broadcast_from_worker0(indices)  # 多卡广播索引
            output = bb.emit_output(
                relax.op.call_tir_inplace(
                    bb.add_func(_get_scatter_2d_inplace(dtype, "_scatter_hidden_states"),
                                "_scatter_hidden_states"),
                    [src, indices, dst], 2, dst.ty,
                ))
```

要点：

- 这是一个 **Relax 函数**（不是纯 TIR），内部组合了「可选 CCL 广播 + `call_tir_inplace` 调底层 TIR」两层。
- `broadcast_from_worker0` 是 TVM 的 CCL（集体通信库）算子，多 GPU 时把 worker 0 的索引广播给所有 worker——保证各卡用同一份 indices 做散列。单卡时这步被跳过，无开销。
- `call_tir_inplace(..., 2, dst.ty)` 的 `2` 是「就地写回第 2 个参数（`dst`）的存储位置」，省一次显存分配。

`gather_hidden_states` 同构（[attach_spec_decode_aux_funcs.py:L100-L123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py#L100-L123)）。dtype 推断见 [attach_spec_decode_aux_funcs.py:L30-L34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py#L30-L34)：`mod["prefill_to_last_hidden_states"].ret_ty.fields[0].dtype`——从已有函数的返回类型里「读」出 hidden states 的精度，使附加函数的 dtype 与模型一致。

**运行期消费**：[function_table.cc:L292-L295](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L292-L295) 取 `gather_probs` / `scatter_probs` / `gather_hidden_states` / `scatter_hidden_states`，推测解码动作链（见 [u10-l4](./u10-l4-speculative-decoding.md)）在起草与校验之间调用它们搬动数据。

#### 4.4.4 代码实践

**实践目标**：理解附加 pass 如何「读已有函数」+「按张量并行度分支」。

**操作步骤**：

1. 看 [attach_spec_decode_aux_funcs.py:L30-L34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py#L30-L34)，回答：`scatter_hidden_states` 的 dtype 从哪里来？为什么不能写死成 `"float32"`？
2. 在 [attach_spec_decode_aux_funcs.py:L83-L84](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_spec_decode_aux_funcs.py#L83-L84) 找到张量并行分支，说明 `tensor_parallel_shards == 1` 时这步是否产生额外开销。
3. 对照 `_get_scatter_2d_inplace` 与 `_add_scatter_hidden_states`，说出「TIR kernel」与「Relax 封装」的分工。

**预期结果**：

- dtype 来自 `prefill_to_last_hidden_states` 的返回类型；不同模型（bf16/f16）hidden states 精度不同，写死会导致类型不匹配。概率搬运固定 `float32` 因为采样概率永远是 fp32。
- `tensor_parallel_shards == 1` 时 `if` 不进入，无 `broadcast_from_worker0` 调用，零开销；多卡时才广播。
- 分工：TIR kernel（`_scatter_2d`）只管「按索引散列」的纯计算；Relax 封装（`scatter_hidden_states`）负责「广播索引 + 调 TIR + 就地写回」的编排，把通信与计算组合起来。

> 待本地验证：在 `--tensor-parallel-shards 2` 下编译一个 EAGLE 推测解码模型，dump 确认 `scatter_hidden_states` 的 IR 里含 `ccl.broadcast_from_worker0`；单卡编译时确认它不含。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `scatter_probs` / `gather_probs` 无条件附加，而 `scatter_hidden_states` / `gather_hidden_states` 是条件附加？

**参考答案**：概率搬运（scatter/gather probs）是推测解码校验阶段通用的——无论何种方案都要把各序列的概率聚拢/散回。而 hidden states 搬运只用于 EAGLE 这类需要「复用主模型隐状态喂给 draft 模型」的方案；若模型根本没暴露 `prefill_to_last_hidden_states`，就不需要这些函数，按需跳过可减小模型库体积。

**练习 2**：`call_tir_inplace` 相比 `call_tir` 节省了什么？

**参考答案**：`call_tir` 会为输出分配一块新显存；`call_tir_inplace(..., inplace_indices, out_ty)` 允许把输出直接写回某个输入张量已有的存储（这里是 `dst`）。推测解码里这些搬运极其频繁，省掉每次分配能显著降低显存碎片与分配开销，也更容易被纳入 CUDA graph。

---

### 4.5（补充）同族速览与对照

至此本讲的三个最小模块（GPU 采样附加、logit 处理附加、推测解码辅助附加）已讲完。把四个附加 pass（含作为范例的 embedding allocator）放一起对照：

| pass | 附加函数数 | target 守卫 | 改图机制 | 是否读已有函数 | 是否吃 metadata |
|---|---|---|---|---|---|
| `AttachAllocEmbeddingTensorFunc` | 0 或 1 | 无（无条件，但依赖 `embed` 存在） | `BlockBuilder` 新建 | 是（查 `embed`） | 是（`prefill_chunk_size`） |
| `AttachGPUSamplingFunc` | 2 或 6 | cuda/vulkan/metal/webgpu | `BlockBuilder` + op 生成器 | 否 | 是（`variable_bounds`） |
| `AttachLogitProcessFunc` | 3 | 无（CPU/GPU 都附加，实现不同） | `mod["name"] = prim_func` | 否 | 否 |
| `AttachSpecDecodeAuxFuncs` | 2 或 4 | 无（但 TP>1 时多一层 CCL） | `bb.add_func` + Relax 封装 | 是（读 hidden states dtype） | 是（`tensor_parallel_shards`） |

共同点：**全部在 Phase 0**；**全部按函数名与 C++ 端 `GetFunction` 订约**；**全部为「服务端职责」而非「模型前向」**。

## 5. 综合实践

**任务**：把本讲四个附加 pass 与运行期消费串成一条完整的「契约链」，并预测一个具体配置下模型库里会出现哪些附加函数。

**操作步骤**：

1. 自制一张「附加函数注册表」：左列写函数名（`multinomial_from_uniform`、`apply_penalty_inplace`、`scatter_probs`、`alloc_embedding_tensor` 等），中列写生成它的 pass 与 Python 源码行号，右列写 C++ 端在哪个文件、用哪个字符串查找它（参考 [function_table.cc:L228-L295](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L228-L295) 与 [logit_processor.cc:L77-L81](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/logit_processor.cc#L77-L81)）。
2. 选定配置 `--target cuda --tensor-parallel-shards 1`（非 EAGLE、非推测解码模型），预测模型库里会出现本讲哪些附加函数、哪些不会出现。
3. 改成 `--target llvm`，重新预测采样相关函数的存在性，并解释为什么 CPU 采样仍能正常工作。
4. 改成 `--target webgpu`，预测采样函数的子集，并指出被排除的那几个函数名。

**参考答案（要点）**：

- 第 2 步：`cuda` 下出现 `multinomial_from_uniform`、`argsort_probs`、`sample_with_top_p`、`sampler_take_probs`、`sampler_verify_draft_tokens`、`renormalize_by_top_p`（6 个）、3 个 logit 处理函数、`scatter_probs`/`gather_probs`（2 个，hidden states 那 2 个因无 `prefill_to_last_hidden_states` 不出现）、`alloc_embedding_tensor`（若模型有 `embed`）。
- 第 3 步：`llvm` 下采样 6 函数**全部不出现**（`AttachGPUSamplingFunc` 直接 `return mod`）；logit 处理 3 函数仍出现（CPU 版）；CPU 推理时 `Sampler::SupportGPUSampler` 为假，C++ 端走 `cpu_sampler`，不依赖这些 GPU kernel，故采样照常。
- 第 4 步：WebGPU 只附加 `argsort_probs` 与 `sample_with_top_p`；被排除的 4 个是 `multinomial_from_uniform`、`sampler_take_probs`、`sampler_verify_draft_tokens`、`renormalize_by_top_p`（含 i8s 运算）。

> 待本地验证：用 `--debug-dump <dir>` 在三种 target 下编译同一模型，对 `debug-phase0.py` 做函数名 grep，核对预测。

## 6. 本讲小结

- **附加（attach）≠ 派发（dispatch）**：派发改写已存在的占位调用，附加往 IRModule 里**新增**函数，执行后函数数严格增加。
- 四个附加 pass（采样、logit 处理、推测解码辅助、embedding 分配）**全部位于 Phase 0**，因为新增函数要参与后续所有优化、内存规划与 CUDA graph 捕获。
- `AttachGPUSamplingFunc` 在 cuda/vulkan/metal/webgpu 上附加 6 个（WebGPU 仅 2 个）采样函数，并给每个函数打 `tir_var_upper_bound` 属性喂给下游调度器；非 GPU 后端不附加，C++ 端退回 CPU 采样器。
- `AttachLogitProcessFunc` 不论 target 都附加 3 个 logit 处理函数，但 CPU 用 `T.serial`、GPU 用 `thread_binding` 且按 target 钳制线程块大小——这是「编译期生成」最直观的收益。
- `AttachSpecDecodeAuxFuncs` 附加概率与 hidden states 的 scatter/gather，**按张量并行度**决定是否插入 CCL 广播，并**读已有函数**推断 dtype。
- **编译期生成而非运行期写死**的统一理由：形状/target 特化、复用 TVM 调度、融入 CUDA graph、跨后端复用、纳入显存池——这五条贯穿所有 attach pass。
- 编译期与运行期靠**函数名字符串**订立契约：Python 用 `global_symbol`/`bb.function(name)` 注册，C++ 用 `mod->GetFunction(name)` 按名取，任何一边改名另一边必须同步。

## 7. 下一步学习建议

- 顺读 [u8-l4](./u8-l4-lowbatch-memory-pipeline.md)：看 `StaticPlanBlockMemory`、`RewriteCUDAGraph` 等 Phase 4/5 pass 如何消费本讲附加出的函数——理解为何这些函数必须最早进 IRModule。
- 结合 [u10-l3](./u10-l3-sampler.md)：看 C++ 端 `Sampler::SupportGPUSampler` 如何决定用 GPU 还是 CPU 采样器，与本讲「编译期条件附加」形成运行期/编译期双重开关。
- 结合 [u10-l4](./u10-l4-speculative-decoding.md)：把本讲附加的 `sampler_verify_draft_tokens`、`scatter_probs`、`gather_hidden_states` 放回推测解码的起草-校验时间线，理解它们被调用的时机。
- 想动手扩展：若要新增一种「服务端 kernel」（如自定义采样策略），最简模板是 [attach_embedding_allocator.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_embedding_allocator.py)——写一个 `@tvm.transform.module_pass`，在 `transform_module` 里 `bb.add_func`，然后在 `pipeline.py` Phase 0 装配、在 `cpp/serve/function_table.cc` 加一行 `GetFunction` 即可。
