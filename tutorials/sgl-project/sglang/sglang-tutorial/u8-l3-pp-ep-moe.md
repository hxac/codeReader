# 流水线/专家并行与 MoE

## 1. 本讲目标

本讲承接 [u8-l1 张量并行与并行状态](u8-l1-tensor-parallelism.md)，把视线从「一条前向里的跨卡 all-reduce」扩展到三种更大尺度的并行——**流水线并行（PP）**、**专家并行（EP）**，以及承载它们的 **MoE（Mixture of Experts）层**实现。

学完本讲，你应当能够：

- 说清楚 PP / EP 各自切的是什么、与 TP / DP 如何正交组合；
- 跟着源码走完一条 MoE 前向：`router logits → top-k 选专家 → dispatch 分发 → 各专家计算 → combine 汇聚`；
- 看懂 `select_experts` / `fused_topk` / `grouped_topk_gpu` 这套「路由树」是如何按模型（DeepSeek 分组路由 vs 普通 softmax）和硬件（AMD AITER vs CUDA sgl-kernel vs 统一 Triton 路由器）分发的；
- 理解 `token_dispatcher` 的抽象：为什么需要一个 `dispatch` / `combine` 接口把「token 该送到哪张卡的哪个专家」这件事插件化；
- 掌握 EP 模式下专家在多 GPU 上的分布方式（`num_local_experts` / `local_expert_mapping`），以及本轮新增的 **decode 阶段 AITER expert mask 复用** 优化（#31889）。

> 本讲对应代码增量（`977ea336..40b2119b`）主要有两处：`topk.py` 把一批 `from sglang.jit_kernel.*` 导入迁移到 `sglang.kernels.ops.moe.*`（Phase 4 batch-3，#32045，属机械式导入路径变更，路由逻辑不变）；`token_dispatcher/standard.py` 新增 decode 阶段复用 `expert_mask_gpu`（#31889）。本讲按当前代码状态讲解，并在 4.4 重点拆解后者。

## 2. 前置知识

### 2.1 MoE（混合专家）层是什么

一个稠密 MLP 层会对**每一个 token** 跑完整的前馈网络。MoE 层则不同：它内部有 N 个「专家」（每个专家就是一个小 MLP），外加一个「路由器（router / gate）」。每个 token 先过路由器得到一组 logits，再据此**只挑出 top-k 个专家**去计算，最后把这几个专家的输出按路由权重加权求和。

直观好处：用「稀疏激活」换「大容量」——参数总量可以做得很大（几十上百个专家），但每个 token 实际算的专家只有 k 个，计算量可控。DeepSeek-V3、Mixtral、Qwen3-MoE 等都是这类结构。

一个 MoE 层的一次前向可以抽象成三步：

```
hidden_states ──► router ──► topk_weights, topk_ids   （路由：每个 token 选 k 个专家）
      │
      └──► dispatch(topk) ──► 把 token 送到对应专家 ──► 各专家计算 ──► combine ──► 输出
```

`topk_ids[i]` 是第 i 个 token 被分配到的专家编号列表；`topk_weights[i]` 是对应的加权权重。

### 2.2 四种并行各切什么

| 并行 | 切分对象 | 典型通信 |
|------|----------|----------|
| TP（张量并行） | 把每个算子的权重矩阵按维度切开 | 每个 block 末尾 all-reduce |
| PP（流水线并行） | 把**不同的层**分到不同 GPU | 相邻 stage 之间点对点传激活 |
| EP（专家并行） | 把**不同的专家**分到不同 GPU | dispatch/combine 的 all-to-all |
| DP（数据并行） | 复制整个模型，各卡处理不同请求 | 反向梯度 all-reduce（推理期不涉及） |

它们彼此正交，可以组合成 `TP×PP×EP×DP`。本讲聚焦 EP 与承载它的 MoE 层；PP 主要在模型层做层段划分（参见 [u5-l5 精读 Llama](u5-l5-reading-llama.md) 里提到的 `PPMissingLayer` / `PPProxyTensors` 占位），与 MoE 是正交关系。

### 2.3 关键数据结构速览

- `TopKOutput`：路由结果的统一协议（一个 `format` 属性 + 多个 NamedTuple 子类）。
- `DispatchOutput` / `CombineInput`：分发器进出两端的协议。
- `BaseDispatcher`：所有 token 分发器的抽象基类，定义 `dispatch` / `combine` 两个抽象方法。

这些会在下面逐一精读。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/sglang/srt/layers/moe/topk.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py) | **top-k 路由**：`select_experts` 入口、`fused_topk` / `grouped_topk_gpu` / `biased_grouped_topk` 等多种实现，按模型与硬件分发 |
| [python/sglang/srt/layers/moe/token_dispatcher/base.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/base.py) | 分发器**抽象基类** `BaseDispatcher`，以及 `DispatchOutput` / `CombineInput` 协议与前后钩子 |
| [python/sglang/srt/layers/moe/token_dispatcher/standard.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py) | `StandardDispatcher`：无 A2A 通信的标准分发器，做本地专家映射（含本轮 expert mask 复用优化） |
| [python/sglang/srt/layers/moe/token_dispatcher/__init__.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/__init__.py) | 分发器**家族总览**：DeepEP / Mooncake / NixL / MoriEP / FlashInfer / AscendTP / Standard |
| [python/sglang/srt/layers/moe/fused_moe_triton/layer.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py) | `FusedMoE` 基类：EP 拓扑计算（`num_local_experts`）、`create_moe_dispatcher`、`forward_impl` 三段式 |
| [python/sglang/srt/layers/moe/ep_moe/layer.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/ep_moe/layer.py) | `DeepEPMoE`：基于 DeepEP all-to-all 的 EP 专用 MoE，`get_moe_impl_class` 在此选型 |
| [python/sglang/srt/layers/moe/utils.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/utils.py) | `MoeA2ABackend` / `MoeRunnerBackend` 枚举与 `get_moe_a2a_backend()` / `get_moe_runner_backend()` 访问器 |

## 4. 核心概念与源码讲解

### 4.1 并行范式：PP/EP 如何与 TP/DP 组合，MoE 在多卡上的拓扑

#### 4.1.1 概念说明

EP（专家并行）的核心思想是：**把模型里的 N 个专家分散到多张 GPU 上，每张卡只持有其中一部分专家（本地专家）**。这样一个有 256 个专家的巨型 MoE 可以摊到多卡上放得下。代价是：每个 token 想要的专家不一定在本地，于是需要一个 **all-to-all 通信**——把 token 按 `topk_ids` 派发（dispatch）到持有目标专家的卡，算完再汇聚（combine）回来。

- **PP 与 EP 正交**：PP 沿「层」方向切（layer 0~10 在 GPU0，layer 11~20 在 GPU1），EP 沿「专家」方向切（专家 0~63 在 rank0，专家 64~127 在 rank1）。一个模型可以同时 `PP=2, EP=4, TP=2`。
- **EP 与 TP 在 MoE 内部叠加**：一个专家的权重矩阵还可以再用 TP 切（`moe_tp_size`），于是 `intermediate_size` 会被 `moe_tp_size` 整除切分。

当 EP 关闭（`moe_ep_size == 1`）时，所有专家都在本地，分发器退化为不做跨卡通信、只做「全局专家号 → 本地专家号」映射的 `StandardDispatcher`。

#### 4.1.2 核心流程：专家在多卡上如何分布

设全局 routed 专家数为 `G`，EP 并行度为 `E`，则每张卡持有 `G / E` 个本地 routed 专家。第 `r` 个 EP rank 持有全局专家区间 `[r * (G/E), (r+1) * (G/E))`。若还有「融合共享专家（fused shared experts）」，它们通常作为全局共享槽追加在本地专家末尾。

```
全局专家编号:   0   1   2 ...  G-1
EP rank 0:    [0 ... G/E-1]                 + 共享专家
EP rank 1:    [G/E ... 2G/E-1]              + 共享专家
...
EP rank E-1:  [(E-1)G/E ... G-1]            + 共享专家
```

#### 4.1.3 源码精读

`FusedMoE.__init__` 计算这套拓扑。注意它通过命名空间访问器 `get_parallel()` 读取实时并行拓扑（参见 [u2-l5 RuntimeContext](u2-l5-runtime-context-config-bags.md)）：

[FusedMoE.__init__ 的 EP 拓扑计算:228-255](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L228-L255) —— 这段先读 `moe_ep_size` / `moe_ep_rank` / `moe_tp_size`，再算出 `_num_global_routed`（全局 routed 专家数，需扣除共享槽），最终得到 `num_local_experts = _num_local_routed + num_fused_shared_experts`（每卡持有的本地专家数）。关键断言 `self._num_global_routed % storage_ep_size == 0` 保证专家能被均匀切分。

整段可以浓缩成一句伪代码：

```python
num_local_routed = (num_experts - num_shared_slots) // ep_size
num_local_experts = num_local_routed + num_fused_shared_experts
```

`num_local_experts` 之后会写进 `MoeRunnerConfig` 并传给分发器，是「本地专家集合大小」这一贯穿全流程的常量。

而 `FusedMoE.forward_impl` 是 MoE 层的「三段式」主流程：

[FusedMoE.forward_impl:1348-1380](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L1348-L1380) —— 三步：`dispatcher.dispatch(...)` 把 token 送去算 → `run_moe_core(...)` 跑各专家 GEMM → `dispatcher.combine(...)` 汇聚回原顺序。末尾若 `moe_tp_size > 1 or moe_ep_size > 1`，还要做一次 `tensor_model_parallel_all_reduce`（因为 EP 的 combine 在每个 EP rank 上只汇聚了本地专家的 partial sum，需要跨 rank 求和）。

#### 4.1.4 代码实践

1. **实践目标**：理解 EP 下「全局专家数」与「本地专家数」的关系。
2. **操作步骤**：
   - 在 [fused_moe_triton/layer.py:228-255](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L228-L255) 找到 `_num_global_routed` 与 `num_local_experts` 的计算。
   - 假设一个 DeepSeek-V3 风格模型：`num_experts=256`，`num_fused_shared_experts=1`，无 per-rank shared slot 折算（`num_shared_slots=1`）。
3. **需要观察的现象**：手工计算 `ep_size=1 / 4 / 8` 时 `_num_global_routed` 和 `num_local_experts` 各是多少。
4. **预期结果**：
   - `_num_global_routed = 256 - 1 = 255`。
   - `ep_size=1`：`_num_local_routed=255`，`num_local_experts=256`（全部在本地）。
   - `ep_size=4`（需 255 % 4，不整除 → 实际部署会调整共享槽或专家数使整除；这里说明断言 `assert _num_global_routed % storage_ep_size == 0` 的作用）。
   - 实际 DeepSeek-V3 routed=256、shared 单独处理，可被常见 EP 整除。
5. 若想运行验证：用 `--ep-size`、`--tp-size` 启动一个 MoE 模型并 grep 启动日志中的 expert 分布信息；具体输出格式**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：EP=4、TP=2、PP=1、全局专家 64、无共享专家。一张 GPU 持有多少本地专家？其 GEMM 的 `intermediate_size_per_partition` 与原始 `intermediate_size` 是什么关系？

**参考答案**：本地 routed 专家 `64/4=16` 个；`intermediate_size_per_partition = intermediate_size // moe_tp_size = intermediate_size // 2`（TP 再把每个专家的中间维对半切，参见 [layer.py:260-261](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L260-L261)）。

**练习 2**：为什么 `forward_impl` 末尾在 `moe_ep_size > 1` 时也要做 `all_reduce`，即使 EP 已经用 all-to-all 把 token 送到正确的专家了？

**参考答案**：EP 的 dispatch 把每个 token 发给「持有它所需专家的那张卡」算，combine 后每张 EP rank 只得到**该 token 在本地专家上的 partial 加权和**；一个 token 的 top-k 专家往往跨多张卡，故必须跨 EP rank 求和（all-reduce）才能得到完整结果。

---

### 4.2 top-k 路由：从 router logits 到专家选择

#### 4.2.1 概念说明

路由器输出 `router_logits`，形状 `[num_tokens, num_experts]`——每个 token 对每个专家打一个分。top-k 路由就是从中挑出得分最高的 k 个专家，并得到归一化权重。这件事看似简单，但实际有两大流派：

1. **普通 softmax 路由**：对 logits 做 softmax 后取 top-k，权重再归一化。Mixtral 等用这套。
2. **分组路由（grouped top-k）**：DeepSeek-V2/V3 系列。把专家分成若干组，先按「组内最大分」挑出 `topk_group` 个组，再在被选组里取 top-k。这是一种「分组预筛」，能在超大专家数下控制路由质量。还可能带 `correction_bias`（偏置修正）。

此外，不同硬件有不同的最佳 kernel：CUDA 上有 AOT 的 `sgl-kernel.topk_softmax`、AMD ROCm 上有 `aiter.fused_topk`、还有一个**统一 Triton 路由器** `moe_fused_gate`（用环境变量 `SGLANG_OPT_USE_JIT_KERNEL_FUSED_TOPK` 开关，逐渐取代旧 AOT kernel）。

#### 4.2.2 核心流程：select_experts 的分发树

`select_experts` 是模型层调用的统一入口（被 MoE 层在拿到 `router_logits` 后调用）。它读 `TopKConfig` 决定走哪条路径，伪代码如下：

```
select_experts(hidden_states, router_logits, topk_config):
    if use_grouped_topk:           # DeepSeek 系列
        if correction_bias is None:  grouped_topk(...)        # 普通 grouped
        else:                        biased_grouped_topk(...) # 带偏置 grouped
    elif torch_native:             fused_topk_native(...)
    else:                          # 普通 ungrouped
        if scoring in (sqrtsoftplus, sigmoid) [+jit 开关]:  biased_topk_(jit_)impl(...)
        elif flashinfer_trtllm_routed:        fused_topk_softmax_torch_raw_logits(...)
        else:                                fused_topk(...)   # 最常见
```

每条路径最终都返回 `(topk_weights, topk_ids)`，再被包成 `StandardTopKOutput`。注意一个细节：当 `_use_aiter`（AMD AITER）为真时，传给 grouped 路径的 `topk` 是 `num_routed_topk`（仅 routed 专家数）而非 `top_k`，因为 AITER 路径对共享专家有特殊处理。

#### 4.2.3 源码精读

先看统一入口 `select_experts`：

[select_experts 入口与 grouped 分支:1981-2049](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L1981-L2049) —— 注意它先做 `expert_location_dispatch.transform_select_experts_inputs(...)`（一种对 logits 的位置变换），再按 `use_grouped_topk` 分流。`num_routed_topk = top_k - num_fused_shared_experts`，AITER 分支用前者、其余用后者。

路由结果的协议层——`TopKOutput` 与 `StandardTopKOutput`：

[TopKOutput 协议与 StandardTopKOutput:263-282](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L263-L282) —— `TopKOutput` 是一个 `@runtime_checkable Protocol`，只要求有 `format` 属性；`StandardTopKOutput` 是最常见的 NamedTuple，含 `topk_weights / topk_ids / router_logits` 三字段。除此之外还有 `TritonKernelTopKOutput`、`PackedTopKOutput`（FlashInfer TRT-LLM 打包格式）、`BypassedTopKOutput`（延迟物化）等，由 `TopKOutputChecker.format_is_*()` 做类型收窄。

最常用的 `fused_topk`（ungrouped softmax/sigmoid 路径）：

[fused_topk:778-853](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L778-L853) —— 按 `scoring_func` 与硬件层层分发：
- `_use_aiter` → `aiter_fused_topk`（AMD）；
- 实验性 LoRA 融合打包 → `topk_softmax_pack`（注意本轮把导入从 `sglang.jit_kernel.trtllm_lora_temp...` 迁到 `sglang.kernels.ops.moe.trtllm_lora_temp...`，见 [L816](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L816)）；
- `SGLANG_OPT_USE_JIT_KERNEL_FUSED_TOPK` 开 → 统一 Triton 路由器 `moe_fused_gate`（导入同样从 `jit_kernel.moe_fused_gate` 迁到 `kernels.ops.moe.moe_fused_gate`，见 [L829-L833](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L829-L833)）；
- 否则 → AOT `topk_softmax`（导入自 `sglang.kernels.ops.moe`，见 [L181](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L181)）。

> **本轮迁移提示（#32045 Phase 4 batch-3）**：`topk.py` 顶部与函数内多处 `from sglang.jit_kernel.*` 已统一改为 `from sglang.kernels.ops.moe.*`（参见 [L181-186](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L181-L186) 的 `topk_softmax` / `topk_sigmoid`，以及前述 `moe_fused_gate` / `topk_softmax_pack`）。这是 RFC #29630 算子迁移的延续：旧的 `jit_kernel/` 正退化为兼容 shim，真实实现迁入 `kernels/ops/`（详见 [u11-l2 统一算子体系](u11-l2-sgl-kernel-jit-kernel.md)）。**路由算法逻辑完全没变，只是 import 路径换了。**

DeepSeek 风格的分组路由参考实现 `grouped_topk_gpu`（用纯 torch 写出「组内取 max → 选 topk_group 个组 → 组内 top-k」的过程，便于理解原理）：

[grouped_topk_gpu:923-991](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L923-L991) —— 关键四步：
1. `scores = softmax(gating_output)`（或 sigmoid）；
2. `group_scores = scores.view(n, n_group, -1).max(dim=-1)` 得到每 token 各组的代表分；
3. `group_idx = topk(group_scores, k=topk_group)` 选出最强的若干组，构造 `group_mask`；
4. 把不在被选组里的专家分数 mask 成 0，再 `torch.topk(..., k=topk)` 得到最终 `(topk_weights, topk_ids)`。

最后看一眼 `TopKConfig` 这个路由配置容器：

[TopKConfig dataclass:204-222](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L204-L222) —— 它聚合了 `top_k`、`use_grouped_topk`、`topk_group`、`num_expert_group`、`renormalize`、`scoring_func`、`correction_bias` 等所有路由参数，由模型在构造 MoE 层时一次性算好，`select_experts` 只是读它。这正是「把 init 期静态值提取成命名属性」的惯例（见项目 `general-code-style` 规则）。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一条普通 Mixtral 风格请求的路由路径。
2. **操作步骤**：
   - 在 [select_experts:1981](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L1981) 设阅读断点（或加 `print`），确认 `topk_config.use_grouped_topk` 为 `False`、`scoring_func == "softmax"`。
   - 跟到 [fused_topk:778](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L778)，确认 CUDA 非实验路径走到 [L848 的 `topk_softmax(...)`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L848)。
3. **需要观察的现象**：`topk_weights` 形状 `[M, topk]`、`topk_ids` 形状 `[M, topk]` 且 dtype 为 `int32`，`topk_weights` dtype 为 `float32`。
4. **预期结果**：`topk_ids` 的每个元素是 `[0, num_experts)` 内的整数；`topk_weights` 每行求和为 1（`renormalize=True` 时）。
5. 实际运行需 GPU 与一个 MoE 模型权重；若仅阅读，按上述断言理解即可，**待本地验证**具体数值。

#### 4.2.5 小练习与答案

**练习 1**：DeepSeek-V3 的 `grouped_topk_gpu` 里，如果把 `topk_group` 设得等于 `num_expert_group`（选所有组），分组路由会退化成什么？

**参考答案**：`group_mask` 全 1，等价于不做组筛选，直接对所有专家取 top-k，即退化成普通 ungrouped top-k。

**练习 2**：`SGLANG_OPT_USE_JIT_KERNEL_FUSED_TOPK` 这个开关切换的是什么？两条路径的算子分别来自哪里？

**参考答案**：切换「AOT sgl-kernel 的 `topk_softmax`」与「统一 Triton 路由器 `moe_fused_gate`」。前者导入自 `sglang.kernels.ops.moe`（[L181](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L181)），后者导入自 `sglang.kernels.ops.moe.moe_fused_gate`（[L831](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L831)）。Triton 路由器旨在逐步取代退休的 AOT kernel。

---

### 4.3 Token 分发器：dispatch / combine 与本地专家映射

#### 4.3.1 概念说明

路由只告诉每个 token「你该去哪些专家」，但**专家可能不在本地**。把 token 真正送到目标专家、算完再收回来的工作，由 **token dispatcher（分发器）** 负责。

分发器是 SGLang MoE 里最关键的抽象：它把「token 在 EP 拓扑下如何搬运」这件事插件化，使得：
- 无 EP（`moe_ep_size==1`）→ `StandardDispatcher`：不跨卡，只做本地映射；
- 有 EP，用 DeepEP all-to-all → `DeepEPDispatcher`；
- 用 Mooncake / NixL / Mori 传输 → 对应的 `MooncakeEPDispatcher` / `NixlEPDispatcher` / `MoriEPDispatcher`；
- 用 FlashInfer 的 EP → `FlashinferDispatcher`；
- 昇腾 NPU → `AscendTPDispatcher`。

所有分发器实现同一个接口：`dispatch(hidden_states, topk_output) -> DispatchOutput` 与 `combine(combine_input) -> Tensor`。模型层（`FusedMoE`）只依赖这个接口，不关心具体传输后端。

#### 4.3.2 核心流程

一次 MoE 前向里分发器的角色：

```
hidden_states, topk_output
        │
        ▼
dispatcher.dispatch(...)  ──► DispatchOutput   （token 已派发/映射好）
        │
        ▼
run_moe_core(dispatch_output)  ──► 各专家 GEMM  ──► CombineInput
        │
        ▼
dispatcher.combine(combine_input)  ──► 汇聚回原 token 顺序的 hidden_states
```

对 `StandardDispatcher`（无 A2A）来说，`dispatch` 的核心工作是**本地专家映射（local expert mapping）**：把 `topk_ids` 里的「全局专家号」改写成「本地专家号」，并标记哪些全局专家不在本地（映射成 -1）。EP 模式下这一步必不可少，因为路由器产出的是全局号，而本地 kernel 只认本地号。

#### 4.3.3 源码精读

先看分发器家族的选型工厂：

[create_moe_dispatcher:108-148](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L108-L148) —— 按 `get_moe_a2a_backend()` 选型：`NONE`（且非 NPU）→ `StandardDispatcher`；`DEEPEP/MOONCAKE/MORI/NIXL` → `MaybeTboDeepEPDispatcher`（DeepEP 系，可叠加 TBO 两批重叠）；`FLASHINFER` → `FlashinferDispatcher`。分发器在 `FusedMoE.__init__` 里由 [L362 的 `self.dispatcher = create_moe_dispatcher(self.moe_runner_config)`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L362) 创建。

A2A 后端枚举（决定用哪种传输）：

[MoeA2ABackend 枚举:28-87](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/utils.py#L28-L87) —— `NONE/DEEPEP/MOONCAKE/NIXL/MORI/ASCEND_*/FLASHINFER/MEGAMOE/CUSTOMIZED`。其中 `supports_aiter()`（[L80-87](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/utils.py#L80-L87)）列出能与 AMD AITER runner 共存的几种后端。访问器 `get_moe_a2a_backend()` / `get_moe_runner_backend()` 在 [utils.py:316-327](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/utils.py#L316-L327)，从运行期 flags 读取（`initialize_moe_config` 在启动时把 `server_args` 的相关字段灌进 flags）。

抽象基类 `BaseDispatcher`：

[BaseDispatcher 与 dispatch/combine 抽象方法:280-325](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/base.py#L280-L325) —— 两个 `@abstractmethod`：`dispatch`（[L298](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/base.py#L298)）与 `combine`（[L323](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/base.py#L323)）。基类还内置了一套可插拔钩子（`_pre_dispatch_hooks` / `_post_dispatch_hooks` / `_pre_combine_hooks` / `_post_combine_hooks`），允许在不改分发器实现的前提下插入自定义处理（如 overlap、量化注入），通过 `register_*_hook` 注册，并用 `_override_dispatch_func` 把 `self.dispatch` 替换成带钩子的版本。

`StandardDispatcher` 的构造（注意它也用 `get_parallel()` 读 EP 拓扑）：

[StandardDispatcher.__init__:87-119](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L87-L119) —— 读 `moe_ep_size` / `moe_ep_rank`，判定 `use_aiter_moe_runner`（AMD AITER runner 保留全局专家号，故 Triton 需要本地重映射而 AITER 不需要），并初始化两个懒构造字段 `local_expert_mapping = None`、`expert_mask_gpu = None`（后者是本轮 #31889 优化的载体）。

`StandardDispatcher.dispatch` 做本地专家映射的核心段：

[本地专家映射表的构造:173-200](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L173-L200) —— 仅当 `moe_ep_size > 1` 且不跳过映射时执行：先建一张长度 `num_experts`、初值 -1 的 int32 表 `local_expert_mapping`，再把本 rank 持有的全局专家区间 `[ep_rank*L, (ep_rank+1)*L)` 映射成连续本地号 `0..L-1`，末尾追加共享专家。这样后续只需一次 `local_expert_mapping[topk_ids]` 就能把全局号翻译成本地号（不在本地的 → -1）。

[使用映射表改写 topk_ids:202-218](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L202-L218) —— 这里有两条分支，是理解 #31889 的关键（详见 4.4）：
- `use_aiter_moe_runner` 且 `expert_mask_gpu is None`：构造一个 `expert_mask_gpu`（标记哪些专家在本地），交给 AITER runner 内部处理全局号；
- 非 AITER：直接 `topk_output._replace(topk_ids=local_expert_mapping[topk_ids])`，把全局号就地改成本地号。

`combine` 阶段（无 A2A 时很轻量）：

[StandardDispatcher.combine:226-238](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L226-L238) —— 标准路径直接返回 `hidden_states`；只有 FlashInfer CUTLASS FP4 all-gather 路径才做 `reduce_scatterv`。

最后看一眼分发器家族全貌，感受「插件化」的规模：

[token_dispatcher/__init__.py:1-49](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/__init__.py#L1-L49) —— 一份完整的分发器清单（`DeepEPDispatcher` / `MooncakeEPDispatcher` / `NixlEPDispatcher` / `MoriEPDispatcher` / `FlashinferDispatcher` / `AscendTPDispatcher` / `StandardDispatcher`），每种对应一种 EP 传输或「无传输」。

#### 4.3.4 代码实践

1. **实践目标**：理解 `local_expert_mapping` 如何把全局专家号翻译成本地号。
2. **操作步骤**：
   - 在 [standard.py:178-200](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L178-L200) 处阅读 `local_expert_mapping` 的构造。
   - 手工模拟：`num_experts=8`、`num_local_routed=2`（即 `ep_size=4`）、`ep_rank=1`、无共享专家。
3. **需要观察的现象**：写出该 rank 的 `local_expert_mapping`（长度 8 的 int32 数组）。
4. **预期结果**：`[-1, -1, 0, 1, -1, -1, -1, -1]`——全局专家 2、3 在本 rank，分别映射成本地 0、1；其余为 -1。于是若某 token 的 `topk_ids=[3, 6]`，经映射后变成 `[1, -1]`（专家 6 不在本地）。
5. EP=1（`StandardDispatcher` 无 EP）时这段代码不执行，`local_expert_mapping` 保持 `None`，分发器近乎透传。

#### 4.3.5 小练习与答案

**练习 1**：为什么 AITER runner 不需要像 Triton 那样把 `topk_ids` 改成本地号？

**参考答案**：因为 AITER runner 内部**保留全局专家号**（见 [standard.py:95-99](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L95-L99) 的注释与 [L203 的分支](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L203)）：它用一个 `expert_mask_gpu` 告诉 kernel 哪些全局专家在本地，由 kernel 自己处理；而 Triton runner 只认本地号，所以必须先改写 `topk_ids`。

**练习 2**：`BaseDispatcher` 的钩子机制（`register_pre_dispatch_hook` 等）解决了什么问题？

**参考答案**：让 overlap（CPU/GPU 重叠）、量化配置注入、TBO 等横切逻辑能**在不修改每个具体分发器的前提下**插入到 dispatch/combine 前后。基类用 `_override_dispatch_func` 把原方法包一层，符合「组合优于继承 / 避免 mixin」的项目风格（见 `general-code-style` 规则）。

---

### 4.4 EP MoE 与 decode 阶段 expert mask 复用优化

#### 4.4.1 概念说明

当 EP 真正启用（用 DeepEP / Mooncake / NixL / Mori 等 A2A 后端做跨卡 all-to-all）时，模型不再用 `FusedMoE`，而是用其子类 **`DeepEPMoE`**（位于 `ep_moe/layer.py`）。它和基类共享 `forward_impl` 的三段式骨架，但 `run_moe_core` 会按 dispatch 输出格式（DeepEP normal / low-latency）选不同的 kernel。

本模块的重点是本轮的一个**性能优化**——#31889「Cache AITER expert mask across decode」。

**问题背景**：在 AMD ROCm + AITER runner 下，`StandardDispatcher.dispatch` 每次 forward 都会为 AITER 构造一个 `expert_mask_gpu` 张量（标记哪些全局专家在本地）。但在 **decode 阶段**（逐 token 生成），每个请求每步只产生 1 个 token，`local_expert_mapping` 本身是**固定不变的**（EP 拓扑在整次服务期间不变），所以每步重算 `expert_mask_gpu` 是纯浪费。

**优化思路**：第一次（`expert_mask_gpu is None`）算出来后缓存到 `self` 上，后续 decode 步直接复用。这就是把「init 期就固定、运行期不变」的派生值提取成实例属性的标准做法。

#### 4.4.2 核心流程

优化前（每步都重算）：

```
for each decode step:
    dispatch():
        if use_aiter_moe_runner:
            expert_mask_gpu = compute(...)   # 每步重新构造张量、重新上 GPU ❌
```

优化后（首次构造、后续复用）：

```
for each decode step:
    dispatch():
        if use_aiter_moe_runner and expert_mask_gpu is None:   # 仅首次
            expert_mask_gpu = compute(...)
        # 后续 step：expert_mask_gpu 已存在，跳过，直接复用 self.expert_mask_gpu ✅
```

这个改动非常小（2 行条件），但它是 decode 低延迟场景下「积少成多」的典型——decode 每步 token 数少、kernel launch 开销占比高，省掉一次无谓的 tensor 构造与 H2D 拷贝能直接降低单 token 延迟。

#### 4.4.3 源码精读

EP 专用 MoE 层 `DeepEPMoE`：

[DeepEPMoE 类与 forward_impl:49-193](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/ep_moe/layer.py#L49-L193) —— 继承 `FusedMoE`，[forward_impl:177-193](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/ep_moe/layer.py#L177-L193) 同样是 `dispatch → run_moe_core → combine` 三段。它的 `__init__` 用一连串条件判定是否走 `deprecate_flag`（退回基类实现），覆盖 humming / aiter / npu / deep_gemm / flashinfer_cutedsl / bf16 低延迟等多种量化与后端组合。

EP MoE 的选型工厂：

[get_moe_impl_class:279-288](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/ep_moe/layer.py#L279-L288) —— 当 A2A 后端是 `MORI/DEEPEP/MOONCAKE/NIXL` 之一时返回 `DeepEPMoE`，否则返回基类 `FusedMoE`。这就是「是否启用 EP 专用层」的总开关。

> 注意：`expert_mask_gpu` 复用优化落在 `StandardDispatcher`（`standard.py`），而 `StandardDispatcher` 主要用于**无 A2A 通信**或 AITER fast-path-on-but-runner-Triton 的场景。即便 EP 通过 DeepEP 等后端做跨卡传输，AITER runner 仍可能在本地分发阶段读取这个 mask。换言之，这个缓存优化对「AMD AITER runner 启用」的所有路径都生效，不局限于 `ep_size==1`。

**本轮 #31889 的核心改动**——`StandardDispatcher.dispatch` 里构造 `expert_mask_gpu` 的条件：

[expert_mask_gpu 的按需构造（#31889）:202-218](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L202-L218)：

```python
if self.local_expert_mapping is not None and not self.skip_local_expert_mapping:
    if self.use_aiter_moe_runner and self.expert_mask_gpu is None:   # ← 新增 and ...
        self.expert_mask_gpu = (                                     # 仅首次构造并缓存
            ((self.local_expert_mapping >= 0)
             & (self.local_expert_mapping < self.num_local_experts))
            .to(torch.int32).to(device="cuda")
        )
    elif not self.use_aiter_moe_runner:                              # 非 AITER 仍即时改写 topk_ids
        if TopKOutputChecker.format_is_standard(topk_output):
            topk_output = topk_output._replace(
                topk_ids=self.local_expert_mapping[topk_output.topk_ids]
            )
```

对比改动前：原代码是 `if self.use_aiter_moe_runner:`（无条件每次重算）与 `else:`。改动点有二：
1. AITER 分支加上 `and self.expert_mask_gpu is None`——只在首次（缓存为空）时构造，后续 decode 步命中缓存直接跳过；
2. 把 `else:` 收紧成 `elif not self.use_aiter_moe_runner:`——避免在「AITER 但 mask 已存在」时意外落入改写 `topk_ids` 的分支（这会破坏 AITER 保留全局号的契约）。

这个改动的正确性前提是：**`local_expert_mapping` 在 dispatcher 生命周期内不变**。这一点由 EP 拓扑固定 + [standard.py:178](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L178) 的「仅当为 None 时才构造」共同保证。它正是项目规则 `general-code-style` 里「Extract init-static values at construction」的落地：派生值（mask）的输入（mapping）在对象生命周期内冻结，故可缓存。

#### 4.4.4 代码实践

1. **实践目标**：定位本轮 #31889 的优化点，并解释它为何安全。
2. **操作步骤**：
   - 打开 [token_dispatcher/standard.py:202-218](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L202-L218)。
   - 用 `git log -p python/sglang/srt/layers/moe/token_dispatcher/standard.py` 找到 commit `40b2119b`（「[AMD] Cache AITER expert mask across decode」），对照改动前后。
3. **需要观察的现象**：改动只有两行——`if self.use_aiter_moe_runner:` → `if self.use_aiter_moe_runner and self.expert_mask_gpu is None:`，以及 `else:` → `elif not self.use_aiter_moe_runner:`。
4. **预期结果**：能口述「首次 decode 步构造 `expert_mask_gpu` 并缓存到 `self`；之后每步因 `is not None` 而跳过构造，省去一次张量创建 + H2D 拷贝」。
5. **思考验证**：尝试回答——如果运行期通过 `get_context().override(...)` 改变了 EP 拓扑（例如弹性 EP 扩缩容），这个缓存会不会出错？提示：`local_expert_mapping` 的构造在 [L178](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L178) 只判 `is None`，拓扑若真的热变，旧缓存会失效——但目前 EP 拓扑不在请求期热改范畴，故实际安全。**待本地验证**弹性 EP 场景下是否有专门的失效路径。

#### 4.4.5 小练习与答案

**练习 1**：为什么这个缓存优化对 **decode** 阶段收益最大，而对 **prefill** 阶段收益不明显？

**参考答案**：prefill 一次处理大量 token，构造一次 `expert_mask_gpu` 相对总计算量微不足道；decode 每步只有 batch_size 个 token，计算量小，**固定的 Python/CUDA 启动开销与张量构造开销占比高**，省掉它对单 token 延迟改善明显。

**练习 2**：改动把 `else:` 改成 `elif not self.use_aiter_moe_runner:` 是为了防止什么？

**参考答案**：防止「`use_aiter_moe_runner=True` 且 `expert_mask_gpu` 已缓存」的情况意外落入 `else` 分支，从而把 `topk_ids` 用 `local_expert_mapping` 改写成本地号——这会破坏 AITER runner「保留全局专家号」的契约（它期望拿到全局号 + mask 自行判断）。

---

## 5. 综合实践

把本讲三个最小模块串起来：**路由 → 分发 → EP MoE 选型**。

**任务**：阅读源码，为一条在 `--ep-size 4 --tp-size 2`、A2A 后端为 DeepEP、AMD GPU 且 `SGLANG_USE_AITER=1` 的 MoE 模型上的 decode 请求，画出「一个 token 从 router_logits 到最终 hidden_states」的完整调用链，并标注：

1. 路由：`select_experts` 走哪条分支（参考 [topk.py:1981](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/topk.py#L1981)），最终调用哪个 topk kernel；
2. MoE 层选型：`get_moe_impl_class` 返回 `DeepEPMoE` 还是 `FusedMoE`（参考 [ep_moe/layer.py:279](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/ep_moe/layer.py#L279)），分发器是哪一种（参考 [fused_moe_triton/layer.py:108](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L108)）；
3. 分发器中 AITER expert mask 的复用：标注 [standard.py:203](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/standard.py#L203) 在第 N 步 decode 时命中缓存、跳过构造；
4. 末尾 all-reduce：为什么 `moe_ep_size>1` 时仍需 `tensor_model_parallel_all_reduce`（参考 [fused_moe_triton/layer.py:1377](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/fused_moe_triton/layer.py#L1377)）。

**进阶**：尝试在本讲代码里回答——如果要让 `expert_mask_gpu` 在弹性 EP 扩容时正确失效，需要在哪些地方加一行 `self.expert_mask_gpu = None` 的重置？（提示：关注 `local_expert_mapping` 何时会被重建。）

## 6. 本讲小结

- **PP/EP 与 TP/DP 正交**：PP 切层、EP 切专家、TP 切算子内部维度；MoE 层在 `FusedMoE.__init__` 里由 `moe_ep_size` / `moe_tp_size` 计算出每卡持有的 `num_local_experts`。
- **MoE 前向是三段式**：`dispatch → run_moe_core → combine`，外加 EP/TP 时的末尾 all-reduce；这套骨架在 `FusedMoE.forward_impl` 与 `DeepEPMoE.forward_impl` 中一致。
- **top-k 路由是一棵分发树**：`select_experts` 按 `use_grouped_topk`（DeepSeek 分组）vs 普通、`scoring_func`、硬件（AITER / sgl-kernel AOT / 统一 Triton 路由器）层层选路，产出统一的 `TopKOutput`。本轮 #32045 把多处 `jit_kernel.*` 导入迁到 `kernels.ops.moe.*`，逻辑不变。
- **分发器是 EP 的插件点**：`BaseDispatcher` 定义 `dispatch`/`combine` 接口 + 钩子机制；`create_moe_dispatcher` 按 `MoeA2ABackend` 选 DeepEP / Mooncake / NixL / Mori / FlashInfer / Standard 等具体实现。
- **本地专家映射**：`StandardDispatcher` 用 `local_expert_mapping` 把全局专家号翻译成本地号（非 AITER 路径），或用 `expert_mask_gpu` 告诉 AITER runner 哪些专家在本地。
- **本轮 #31889 优化**：在 AITER runner 下，`expert_mask_gpu` 首次构造后跨 decode 步复用（`and self.expert_mask_gpu is None`），省去 decode 阶段每步的无谓张量重建，前提是 EP 拓扑在 dispatcher 生命期内不变。

## 7. 下一步学习建议

- **深入 EP 通信实现**：阅读 [token_dispatcher/deepep.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/deepep.py) 与 [moriep.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/moe/token_dispatcher/moriep.py)，理解 all-to-all dispatch/combine 的 normal 与 low-latency 两种模式如何与 [u9-2 KV 传输与连接器](u9-l2-kv-transfer-connectors.md) 的 connector 协同。
- **TBO/SBO 重叠**：本讲多次提到 `MaybeTboDeepEPDispatcher` 与 single/two batch overlap，可结合 [u3-l4 调度组件与 CPU-GPU 重叠](u3-l4-scheduler-components-overlap.md) 理解 MoE 的通信-计算重叠。
- **算子体系**：本轮把 topk 相关算子迁入 `kernels.ops.moe.*`，建议接着读 [u11-l2 统一算子体系](u11-l2-sgl-kernel-jit-kernel.md)，搞清楚 `KernelSpec` / `selector` / `BaseFusedOp` 如何统一管理 AOT 与 JIT 算子。
- **量化与 MoE**：MoE 的 GEMM 常与 FP8 / W4A8 / CUTLASS 量化耦合（见 `DeepEPMoE.run_moe_core` 里的 `forward_cutlass_w4afp8*`），可结合 [u11-l1 量化方案](u11-l1-quantization.md) 继续深入。
