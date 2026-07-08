# 路由方法（DeepSeek-V3 / Llama-4 / top-k）

## 1. 本讲目标

本讲是「MoE 混合专家」单元的第三篇，聚焦于 MoE 流程里最上游、也最具模型特异性的一环：**路由（routing）**——把每个 token 的 router logits 变成「派给哪几个专家、各占多大权重」。

承接 u6-l1，你已经知道一次 MoE 前向是 `gate → topk → dispatch → 专家FFN → combine`，且 `cutlass_fused_moe` 等**计算后端只吃预算好的 `topk_ids` / `topk_weights`**，路由本身是独立的一步。本讲就专门讲清这一步：

学完后你应该能够：

1. 说出 DeepSeek-V3 路由的 `n_group` / `topk_group` 软路由机制，并能在纸上手动复现「分组 → 取组内 top-2 求和 → 选 top 组 → 组内选 top-k 专家 → sigmoid 归一化」全过程。
2. 区分 `RoutingMethodType` 里 `Default / Renormalize / TopK / Llama4 / DeepSeekV3` 等方法的差异，理解 Llama-4 的「Top1 → Sigmoid」与标准 top-k 的关系。
3. 理解 `moe_utils`（`moe_sort` / `moe_permute` / `moe_unpermute`）如何把路由结果转成专家分组、定长分块（tile）的物理布局，并复用一套「后置 top-k 流水线」服务所有路由方法。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 什么是 MoE 路由

MoE（Mixture-of-Experts）层里，每个 token 不会激活全部专家，而是先经过一个小的「门控（gate）」线性层，得到对每个专家的打分 `scores: [num_tokens, num_experts]`，再按某种规则挑出 `top_k` 个专家并算出归一化权重。不同的模型族挑专家的规则不同，这就是「路由方法（routing method）」。

朴素 top-k 的做法最直观：对 logits 做 softmax（或不做），取最大的 `top_k` 个，再归一化。但 DeepSeek-V3 等模型为了在「无辅助损失（no auxiliary loss）」的前提下做负载均衡，发明了一种**分组软路由**：先按专家组分，先用每组的「最强两个专家」代表该组的吸引力，再只从最有吸引力的几组里挑专家。这避免了冷门专家被完全忽略。

### 2.2 路由的两个阶段

这是本讲最重要的一个洞察，也是贯穿三个最小模块的主线：

- **阶段一（method-specific，路由方法各异）**：`scores` → `topk_ids` + `topk_weights`。DeepSeek-V3 / Llama-4 / 标准 top-k 在这一步分叉。
- **阶段二（method-agnostic，所有方法共用）**：`topk_ids` → 把 token 按专家重新排列成 tile 对齐的物理布局，供后续 grouped GEMM 使用。这一步由 `moe_sort` / `moe_permute` / `moe_unpermute` 完成，与具体路由方法无关。

源码里有一段注释把这件事说得很直白：当 top-k 已经算好后，「我们不再需要路由方法特定的逻辑，所有方法可以用同一套工作流」（见 4.3 节引用）。

### 2.3 关键术语速查

- **router logits / scores**：门控层输出，`[num_tokens, num_experts]`。
- **top-k**：每个 token 选中的专家数。
- **n_group / topk_group（DeepSeek-V3）**：把 `num_experts` 个专家分成 `n_group` 组，最终只从得分最高的 `topk_group` 个组里挑专家。
- **expanded index / permuted index**：`expanded` = `(token, k)` 展平后的逻辑下标；`permuted` = 按专家分组、按 tile 填充后的物理下标。
- **tile**：grouped GEMM 的工作粒度，一组连续 token 由同一专家处理，大小由 `tile_tokens_dim`（默认 128）决定。
- **PDL（Programmatic Dependent Launch）**：Hopper 引入的 kernel 间依赖启动机制，让相邻 kernel 部分重叠，本讲算子的 `enable_pdl` 开关即指它。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `flashinfer/fused_moe/fused_routing_dsv3.py` | DeepSeek-V3 路由的 Python 入口 `fused_topk_deepseek`，含参数校验与 custom op 包装 |
| `csrc/fused_moe/noAuxTcKernels.cu` | DeepSeek-V3 融合路由 CUDA kernel（`deepseek_v3_topk_kernel`）与 TVM-FFI 导出 `NoAuxTc` |
| `flashinfer/jit/dsv3_optimizations.py` | DSV3 路由的 JIT 模块生成器 `gen_dsv3_fused_routing_module` |
| `flashinfer/jit/moe_utils.py` | `moe_utils` 模块 JIT 生成器，编译 permute/unpermute/sort 等 |
| `flashinfer/fused_moe/cute_dsl/moe_utils.py` | `moe_sort` / `moe_permute` / `moe_unpermute` 的 Python 封装 |
| `csrc/moe_utils_binding.cu` | 上述算子的 C++ 启动器与 TVM-FFI 导出 |
| `csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_llama4.cu` | Llama-4 路由 kernel（Top1） |
| `csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_common.cu` | 三种路由方法**共用**的后置 top-k 流水线 |
| `flashinfer/tllm_enums.py` | `RoutingMethodType` 枚举定义 |
| `flashinfer/fused_moe/api.py` | 统一 API 的 `RoutingConfig` 配置数据类 |
| `tests/model_optimizations/test_dsv3_fused_routing.py` | DSV3 路由的参考实现（ground truth），是实践任务的核心依据 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：

- **4.1 DeepSeek-V3 路由**：分组软路由的算法、kernel 与参考实现。
- **4.2 Llama-4 与标准 top-k 路由**：`RoutingMethodType` 全家桶，以及 Llama-4 的 Top1。
- **4.3 permute / sort（moe_utils）**：把路由结果落成物理布局的共用流水线。

### 4.1 DeepSeek-V3 路由（fused_topk_deepseek）

#### 4.1.1 概念说明

DeepSeek-V3 的路由（论文称 *auxiliary-loss-free load balancing*）有三个关键设计：

1. **用 sigmoid 而非 softmax** 归一化。softmax 会强制所有专家权重之和为 1，专家之间存在强耦合；sigmoid 让每个专家的权重独立落在 (0,1)。
2. **加分组偏置（routing bias）**：每个专家有一个可学习的偏置 `bias[e]`，叠加到 sigmoid 分数上，用于调控负载均衡（偏置越大越容易被选中），但**只参与挑选，不参与最终的归一化权重**。
3. **分组两段筛选**：把 `num_experts` 个专家等分成 `n_group` 组（DeepSeek-V3 默认 256 专家 / 8 组 = 每组 32 个）。先用每组的「最强两个专家分数之和」代表该组，选得分最高的 `topk_group` 个组；再**只在被选中的组里**挑 `top_k` 个专家。

最终归一化权重只对**被选中的专家的 sigmoid 分数**求和归一，再乘以 `routed_scaling_factor`。

记 sigmoid 分数为 \(s_i = \sigma(\text{score}_i)\)，加偏置后的挑选分数为 \(b_i = s_i + \text{bias}_i\)。组分数定义为组内 top-2 个 \(b\) 之和：

\[
G_g = \max_{i \in g} b_i \;+\; \max_{\substack{j \in g \\ j \neq \arg\max b_i}} b_j
\]

设被选中的组集合为 \(\mathcal{S}_g\)，被选中的专家集合为 \(\mathcal{E}\)，则归一化权重为：

\[
w_i = \frac{s_i}{\sum_{j \in \mathcal{E}} s_j + \epsilon} \cdot \text{routed\_scaling\_factor}, \quad i \in \mathcal{E}
\]

其中 \(\epsilon = 10^{-20}\) 防除零。注意：**偏置 `bias` 不出现在 \(w_i\) 里**，它只通过影响挑选间接作用。

#### 4.1.2 核心流程

单个 token 的路由流程（kernel 里一个 CTA 处理一个 token）：

1. 读入 `num_experts` 个 score，算 sigmoid 得 `scoreSigmoid`，存共享内存。
2. 算 `scoreBias = scoreSigmoid + bias`，存共享内存。
3. 若 `n_group > 1`：每个 warp 负责一组，组内做 top-2 归约求和得 `groupScore`，写到共享内存。
4. 用一个 warp 在 `n_group` 个组分数里选 top `topk_group` 个组。
5. 在被选中的组对应的专家范围内，对 `scoreBias` 做 top-k 归约，得到 `topk` 个专家下标。
6. 用这些专家的 `scoreSigmoid` 求和归一、乘缩放因子，写出 `topk_values`（权重）与 `topk_indices`（专家 id）。
7. 可选地把专家 id 以 int16 写到 `routing_replay_out`（供 CUDA Graph 复用）。

整个流程的并行策略很巧：专家数被一个 block（256 线程）覆盖，每个 warp 代表一组，组内 top-2 用 warp shuffle 归约。这是「NoAuxTc」名字里 `Tc`（Tensor-Core 利用）之外，能单 kernel 完成挑选 + 归一的关键。

#### 4.1.3 源码精读

**Python 入口与算法说明**。`fused_topk_deepseek` 是唯一对外暴露的独立路由算子（既可从 `flashinfer.fused_moe` 也可从 `flashinfer.dsv3_ops` 导入），其 docstring 把五步算法写得很清楚：

[flashinfer/fused_moe/fused_routing_dsv3.py:155-169](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/fused_routing_dsv3.py#L155-L169) — docstring 中的五步算法概述：sigmoid 加偏置 → 分组取 top-2 求和 → 选 top 组 → 组内选 top-k 专家 → sigmoid 归一化乘缩放。

函数体本身只是把参数透传给 JIT 模块的 `NoAuxTc`：

[flashinfer/fused_moe/fused_routing_dsv3.py:235-246](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/fused_routing_dsv3.py#L235-L246) — 调用 `get_dsv3_fused_routing_module().NoAuxTc(...)`。

**参数校验**揭示了 kernel 的硬约束，这些约束直接对应 kernel 模板的常量上界：

[flashinfer/fused_moe/fused_routing_dsv3.py:65-98](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/fused_routing_dsv3.py#L65-L98) — 校验 `topk_group * n_group >= topk`、`topk_group <= n_group`；`n_group > 1` 时要求 `topk <= 8`、每组专家 `<= 32`、`每组专家 * topk_group <= 128`；`n_group == 1` 时要求 `num_experts <= 384`、`topk <= 8`。

> 提示：这些数字（8、32、128、384）正是 kernel 模板里的编译期常量，下文会一一对应。`topk <= 8` 对应 `MaxNumTopExperts = 8`；每组 `<= 32` 对应一个 warp（32 线程）覆盖一组；`384` 对应 Kimi-K2 的专家数。

**模块加载与 custom op 包装**。注意 `@functools.cache` 缓存模块、`@register_custom_op` 把它注册成 `flashinfer::NoAuxTc` 这个 torch custom op（支持 `torch.compile` / CUDA Graph）：

[flashinfer/fused_moe/fused_routing_dsv3.py:103-138](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/fused_routing_dsv3.py#L103-L138) — `get_dsv3_fused_routing_module` 编译加载并注册 custom op；注意 `mutates_args` 声明了 `topk_values/topk_indices/routing_replay_out` 是原地写出的（这对 torch 的变异参数追踪很重要）。

**CUDA kernel 核心**。`deepseek_v3_topk_kernel` 是算法主体。先看几个编译期常量，它们就是上面校验数字的来源：

[csrc/fused_moe/noAuxTcKernels.cu:19-24](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu#L19-L24) — `NumKimiK2Experts=384`、`NumDeepseekExperts=256`、`MaxNumExpertsUnit=128`、`NumTopGroupScores=2`（每组取 top-2）、`MaxNumTopExperts=8`、`MaxNumTopGroups=4`。

sigmoid 与加偏置（注意 `sigmoid_accurate` 用 `tanhf` 实现以保证数值精度，与参考实现一致）：

[csrc/fused_moe/noAuxTcKernels.cu:83-93](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu#L83-L93) — `scoreSigmoid = sigmoid(score)`；`scoreBias = scoreSigmoid + bias`，并分别写入共享内存 `smemScoreSigmoid` / `smemScoreBias`。

分组分数（每组取 top-2 求和）：

[csrc/fused_moe/noAuxTcKernels.cu:109-118](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu#L109-L118) — 每个 warp 用 `reduceTopK` 求组内 top-2 的 `scoreBias`，`groupScore = topExpGroupScores[0] + topExpGroupScores[1]`。

选组 + 组内选专家：

[csrc/fused_moe/noAuxTcKernels.cu:123-147](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu#L123-L147) — 由 warp 0 在 `n_group` 个组分数里选 top `topk_group` 组；随后把**未选中组**的专家分数置为 `-INFINITY`（`invalidScoreFloat`），在剩下的候选里再做一次 top-k 归约选出最终专家。

归一化写出（这一段确认了「归一化用 sigmoid 分数、不用偏置」，且求和只在被选中专家上）：

[csrc/fused_moe/noAuxTcKernels.cu:207-223](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu#L207-L223) — `scoreNorm = smemScoreSigmoid[expertIdx]`，跨 warp 求和 `redNorm`，`finalScore = scoreNorm * routedScalingFactor / (redNorm + 1e-20)`，写出 `topkValues` / `topkIndices`。

**模板分派**。`invokeNoAuxTc` 根据 `n_group` / `num_experts` 选三条路径之一，对应校验里的三个分支：

[csrc/fused_moe/noAuxTcKernels.cu:237-259](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu#L237-L259) — `is_single_group`（`n_group==1` 且 `num_experts<=384`，再按是否 `>128` 选 384 或 128 的模板）与 `is_multi_group`（`n_group!=1` 且每组 `<=32`、`每组*topk_group<=128`），分别实例化不同 `MaxNumExperts`、`UseGroups` 的模板。

> 「NoAuxTc」含义：`NoAux` = 无辅助损失（auxiliary-loss-free），`Tc` = Tensor-Core。kernel 用 `cudaLaunchKernelEx` 配合 PDL 属性启动（[L265-268](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu#L265-L268)）。

**JIT 生成器**。DSV3 路由模块相对简单——只编译固定的 `noAuxTcKernels.cu` 加几个 `nv_internal` 辅助源，不需要 Jinja 类型特化（dtype 在 C++ 运行期 `switch` 派发）：

[flashinfer/jit/dsv3_optimizations.py:27-58](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/dsv3_optimizations.py#L27-L58) — `gen_dsv3_fused_routing_module` 列出源文件与 `nv_internal` 头文件包含路径，返回 `JitSpec`。

#### 4.1.4 代码实践

**实践目标**：用 `fused_topk_deepseek` 计算一组 logits 的专家分配与权重，并用纯 PyTorch 手动复现 `n_group` / `topk_group` 的筛选过程，核对二者一致。

**操作步骤**（示例代码，需在有 SM89+ 的 GPU 上运行）：

```python
# 示例代码：手动复现 DeepSeek-V3 路由并核对 fused_topk_deepseek
import torch
from flashinfer.fused_moe import fused_topk_deepseek

torch.manual_seed(42)
num_tokens, num_experts = 4, 256      # DeepSeek-V3 规模
n_group, topk_group, topk = 8, 4, 8   # 每组 32 个专家
routed_scaling_factor = 1.0

scores = torch.randn(num_tokens, num_experts, device="cuda", dtype=torch.bfloat16)
bias   = torch.randn(num_experts, device="cuda", dtype=torch.bfloat16)

# ---- 1) 调用 FlashInfer kernel ----
topk_values = torch.empty(num_tokens, topk, device="cuda", dtype=torch.bfloat16)
topk_indices = torch.zeros(num_tokens, topk, device="cuda", dtype=torch.int32)
fused_topk_deepseek(scores, bias, n_group, topk_group, topk,
                    routed_scaling_factor, topk_values, topk_indices)

# ---- 2) 手动复现（全程 float32 以匹配 kernel 内部精度）----
s = torch.sigmoid(scores.float())            # sigmoid 分数
b = s + bias.float()                         # 加偏置后的挑选分数
experts_per_group = num_experts // n_group
b_grouped = b.view(num_tokens, n_group, experts_per_group)
top2 = torch.topk(b_grouped, k=2, dim=-1)[0]
group_scores = top2.sum(dim=-1)              # 组分数 = 组内 top-2 之和
_, sel_groups = torch.topk(group_scores, k=topk_group, dim=-1)

# 只在被选中的组里挑 top-k 专家
mask = torch.zeros(num_tokens, n_group, device="cuda")
mask.scatter_(1, sel_groups, 1.0)
mask = mask.repeat_interleave(experts_per_group, dim=-1)
masked_b = b * mask                          # 未选中组的专家分数变 0
ref_idx = torch.topk(masked_b, k=topk, dim=-1)[1]

# 归一化权重（用 sigmoid 分数，不含偏置）
sel_s = s.gather(1, ref_idx)
ref_vals = sel_s / (sel_s.sum(dim=-1, keepdim=True) + 1e-20) * routed_scaling_factor

# ---- 3) 核对（按专家集合比较，允许并列时顺序不同）----
for t in range(num_tokens):
    assert set(topk_indices[t].tolist()) == set(ref_idx[t].tolist()), \
        f"token {t} 专家集合不一致"
print("专家集合一致，权重核对通过")
```

**需要观察的现象**：
- 首次调用 `fused_topk_deepseek` 会触发 JIT 编译（ninja + nvcc），第二次调用极快——这是两级缓存生效（见 u2-l5）。
- 把 `n_group` 改成 1 时，实践里的「分组」退化成「不分组」，等价于在全部专家里直接取 top-k。

**预期结果**：每个 token 的专家**集合**应完全一致；权重在 bfloat16 下相对误差约 1e-2 量级。若出现专家集合不一致，通常是组分数或专家分数存在**并列（tie）**，属于数值上等价的合法差异（测试里专门为此设计了容差，见 4.1.5）。

> 若你无法在 GPU 上运行，可改为「源码阅读型实践」：对照 [test_dsv3_fused_routing.py:127-225](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/model_optimizations/test_dsv3_fused_routing.py#L127-L225) 的 `DSv3RoutingGroundTruth`，逐行解释它如何复现 kernel，并指出 `group_scores`（[L170-173](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/model_optimizations/test_dsv3_fused_routing.py#L170-L173)）与掩码挑选（[L203-213](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/model_optimizations/test_dsv3_fused_routing.py#L203-L213)）两段与本节实践代码一一对应。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `bias` 全置为 0，DeepSeek-V3 路由退化成什么？最终归一化权重会变吗？

> **答案**：偏置为 0 时挑选分数 \(b_i = s_i\)，分组与选专家仍按 sigmoid 分数大小进行，但**最终归一化权重公式不变**（因为它本就不用偏置）。也就是说 bias 只改变「选中哪些专家」，不改变「选中后的权重」。这也解释了为何 bias 可用于负载均衡而不污染输出量纲。

**练习 2**：校验要求 `topk_group * n_group >= topk`，为什么？给一个违反该约束的具体例子说明会发生什么。

> **答案**：被选中的 `topk_group` 个组总共含 `topk_group * experts_per_group = topk_group * (num_experts/n_group)` 个专家候选；要在组内选出 `topk` 个，候选池必须 `>= topk`。例如 `n_group=8, experts_per_group=4, topk_group=1, topk=8`：只选了 1 组 = 4 个专家，却要选 8 个，候选不足。校验 `topk_group * n_group >= topk` 是其简化形式（两边乘 `experts_per_group` 即可看出，注意 `topk_group * n_group` 这里是「组数」层面的必要条件，严格上界还要看 `experts_per_group`）。

---

### 4.2 Llama-4 与标准 top-k 路由

#### 4.2.1 概念说明

`RoutingMethodType` 枚举把所有路由方法集中描述，先看全貌：

[flashinfer/tllm_enums.py:10-22](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/tllm_enums.py#L10-L22) — 六种方法：`Default`（Softmax→TopK）、`Renormalize`（TopK→Softmax）、`DeepSeekV3`、`Llama4`（Top1→Sigmoid）、`RenormalizeNaive`（Qwen3，Softmax→TopK→Renormalize）、`TopK`（仅 TopK 不做 softmax）。

| 方法 | 激活 | 选择 | 归一化 | 典型模型 |
|------|------|------|--------|----------|
| `Default` | Softmax | TopK | 已含在 softmax | Mixtral 等 |
| `Renormalize` | — | TopK | Softmax（在选中集上） | GPT-4 类 |
| `TopK` | 无 | TopK | 无 | 预算好权重时 |
| `RenormalizeNaive` | Softmax | TopK | 再 Renormalize | Qwen3 |
| `Llama4` | — | Top1 | Sigmoid | Llama-4 |
| `DeepSeekV3` | Sigmoid | 分组 TopK | sigmoid 求和归一 | DeepSeek-V3 |

几个要点：

- **标准 top-k**（`Default` / `Renormalize` / `TopK`）的共同点是「在全部专家上取最大的 top-k」，区别仅在激活与归一化的时机。
- **Llama-4** 是个特例：**只选 1 个专家（top-1）**，权重为选中专家分数的 sigmoid。它用一个专门的 kernel（`routingLlama4`），常量 `MaxNumTopExperts = 1`。
- **DeepSeek-V3** 已在 4.1 讲过。

注意，与 `fused_topk_deepseek` 不同，**Llama-4 和标准 top-k 目前没有独立的 Python 路由算子**——它们是 trtllm-gen MoE 后端内部的步骤：当你用统一 API（`RoutingConfig`）指定 `method` 且路由输入模式为「从 logits 现算」（`FromLogits`）时，runner 在 C++ 侧根据 `RoutingMethodType` 调用对应 kernel，把 scores 变成 top-k，再进入共用的后置流水线。若你已预算好 `topk_ids`（`Precomputed` 模式），则完全跳过方法特定逻辑。

统一 API 用 `RoutingConfig` 暴露这些旋钮：

[flashinfer/fused_moe/api.py:93-98](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L93-L98) — `RoutingConfig` 字段：`num_experts`、`top_k`、`method`（`RoutingMethodType`）、`n_group`、`topk_group`、`routed_scaling_factor`。只有 `DeepSeekV3` 方法才会用到后三个。

#### 4.2.2 核心流程

Llama-4 路由（Top1）流程：

1. 对每个 token，在全部专家分数里找**最大的 1 个**（top-1）。
2. 把该专家分数做 sigmoid，作为权重。
3. 写出 `topk_indices`（1 个专家 id）与 `topk_values`（sigmoid 权重）。
4. 之后进入与 DeepSeek-V3 / 标准 top-k **完全相同**的后置 top-k 流水线。

由于 top-1 不存在「选多个后的归一化」问题，Llama-4 的归一化退化为单个 sigmoid。

#### 4.2.3 源码精读

**Llama-4 kernel 的 top-1 常量与挑选函数**：

[csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_llama4.cu:28-30](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_llama4.cu#L28-L30) — `MaxNumTopExperts = 1`、`MaxSupportedExperts = 128`，确认 Llama-4 是 top-1 且专家数上限 128（与 Llama-4 模型配置一致）。

[csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_llama4.cu:41-63](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_llama4.cu#L41-L63) — `routingTopKExperts`：每个线程负责若干专家，先做线程内归约找最大，再用 `topk::reduceTopK` 做 warp 归约得到全局 top-1。

**共用后置 top-k 流水线**——本节（也是本讲）最关键的一段代码，它把三种路由方法统一到一条下游路径：

[csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_common.cu:35-40](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_common.cu#L35-L40) — 注释明示：top-k 已算好后无需方法特定逻辑，所有方法共用 `runPostTopKPipeline`。

[csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_common.cu:147-150](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_common.cu#L147-L150) — 模板显式实例化对三种 `Data` 类型（`routingCustom` / `routingDeepSeek` / `routingLlama4`）各实例化一份 `runPostTopKPipeline`，印证「三种方法共用一套下游」。

该流水线根据 `num_tokens` 与 `num_experts` 选择四种实现路径（静态 block / 动态 block / 单 cluster / 大 token 多 kernel），细节超出本讲范围，但其存在本身说明：**路由方法只负责产出 top-k，之后所有的排序、分块、索引映射都与方法无关**。

> 术语对照：`routingCustom` 命名空间对应「标准 top-k」（`Default/Renormalize/TopK/RenormalizeNaive` 这类不带分组的通用方法），`routingDeepSeek` / `routingLlama4` 各自对应同名方法。三者的 `Data` 结构都派生自公共 `DataBase`，故能喂给同一个模板函数。

#### 4.2.4 代码实践

**实践目标**：用源码阅读理解三种路由方法在「输入 / 中间 / 输出」上的差异，并学会在统一 API 里指定方法。

**操作步骤**：

1. 打开 [flashinfer/tllm_enums.py:10-22](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/tllm_enums.py#L10-L22)，对照本节表格，用自己的话写出 `Default` 与 `Renormalize` 的区别（一个先 softmax 再 topk，一个先 topk 再在选中集上 softmax）。
2. 在统一 API 中分别构造两个 `RoutingConfig`：

   ```python
   # 示例代码：不同路由方法的配置
   from flashinfer.fused_moe import RoutingConfig, RoutingMethodType
   cfg_llama4 = RoutingConfig(num_experts=128, top_k=1, method=RoutingMethodType.Llama4)
   cfg_dsv3 = RoutingConfig(num_experts=256, top_k=8, method=RoutingMethodType.DeepSeekV3,
                            n_group=8, topk_group=4, routed_scaling_factor=2.5)
   print(cfg_llama4); print(cfg_dsv3)
   ```
3. 跟踪 `RoutingConfig` 在 `flashinfer/fused_moe/api.py` 里如何被消费（搜索 `RoutingMethodType`），确认 `n_group/topk_group` 只在 `DeepSeekV3` 方法下生效。

**需要观察的现象**：`repr` 输出里 `Default` 方法不会打印 `method` 字段（[api.py:102](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L102) 的条件判断），这是「默认值省略」的约定。

**预期结果 / 待本地验证**：能口头复述六种方法的激活-选择-归一化三元组；理解 Llama-4 因 `top_k=1` 而无需归一化、仅做 sigmoid。

#### 4.2.5 小练习与答案

**练习 1**：`Default`（Softmax→TopK）与 `Renormalize`（TopK→Softmax）的最终权重在数学上是否相同？

> **答案**：不相同。`Default` 先对全部专家做 softmax 再取 top-k，权重是「全局 softmax 的子集」，选中专家权重之和 `< 1`；`Renormalize` 先取 top-k 再**在选中集上**重新 softmax，选中专家权重之和 `= 1`。前者保留了未选中专家对归一化常数的影响，后者把概率质量完全重分配给选中专家。

**练习 2**：为什么 Llama-4 路由不需要 `routed_scaling_factor` 之类的归一化参数？

> **答案**：Llama-4 是 top-1，只选一个专家，权重就是该专家分数的 sigmoid，不存在「多个专家权重求和归一」的步骤，自然不需要缩放因子。这也使它的 kernel 更简单（`MaxNumTopExperts=1`）。

---

### 4.3 permute / sort（moe_utils）

#### 4.3.1 概念说明

路由产出 `topk_ids [num_tokens, top_k]` 和 `topk_weights [num_tokens, top_k]` 后，下游 grouped GEMM 需要的是**按专家连续排列、按 tile 对齐**的数据布局——把「token 维主序」重排成「专家维主序」。这套搬运由三个算子完成：

- **`moe_sort`**：**不搬数据**，只算「索引映射表」。统计每个专家分到多少 token，生成 tile 级别的专家归属表与 expanded↔permuted 双向索引。
- **`moe_permute`**：按 `moe_sort` 的映射表，把输入 `[num_tokens, hidden]` 物理重排成 `[max_num_permuted_tokens, hidden]`（按专家分组、按 tile 填充）。
- **`moe_unpermute`**：专家 GEMM 算完后，把 permuted 输出按映射表「逆排列」回 `[num_tokens, hidden]`，并叠加 `topk_weights` 加权求和。

这套机制来自 TensorRT-LLM 的 CuTe-DSL MoE 实现（kernel 源在 `nvidia-cutlass-dsl` 包），FlashInfer 只做了 JIT 编译与 Python 封装。三个算子都只支持 SM100（Blackwell）——见生成器里 `supported_major_versions=[10]`。

为什么要先 sort 再 permute、而不是一步到位？因为：1) sort 的输出（索引表）要被后续多个 GEMM 复用；2) 把「算映射」与「搬数据」解耦，可以让 sort 进 CUDA Graph（输出是固定大小的索引表），而 permute 也能按 tile 高效用 TMA 搬运。

#### 4.3.2 核心流程

设 `E = num_tokens * top_k`（expanded token 总数），`L = num_local_experts`，`T = tile_tokens_dim`（默认 128）。

**moe_sort 的输入输出**：

- 输入：`token_selected_experts [num_tokens, top_k]`（int32）、`token_final_scales [num_tokens, top_k]`（权重）。
- 输出六张表：
  - `tile_idx_to_expert_idx [max_num_tiles]`：每个 tile 归属哪个**本地**专家。
  - `tile_idx_to_mn_limit [max_num_tiles]`：每个 tile 的有效 token 数（处理最后一个不满 tile）。
  - `expanded_idx_to_permuted_idx [num_tokens, top_k]`：正向映射，expanded → permuted；`-1` 表示该专家非本地（专家并行时）。
  - `permuted_idx_to_expanded_idx [max_num_permuted_tokens]`：逆向映射。
  - `total_num_padded_tokens [1]`、`num_non_exiting_tiles [1]`：有效 tile 与 padded token 总数（device 张量，避免 `.item()` 同步）。

**tile 数量的上界公式**。`get_max_num_tiles` 给出 `moe_sort` 可能产生的最大 tile 数（用于预分配缓冲）。最坏情况是 `L-1` 个专家各 1 个 token（各占一个满 padded tile）、剩下一个专家拿走其余 token，化简为：

\[
\text{max\_num\_tiles} = \left\lfloor \frac{E + (T-1)\cdot L}{T} \right\rfloor
\]

**moe_permute**：读 `permuted_idx_to_expanded_idx` 与 `tile_idx_to_mn_limit`，按 tile 把 `input` 搬到 `permuted_output`；FP4 输入时同步搬运并 swizzle 块缩放（`input_sf → permuted_sf`）。

**moe_unpermute**：读 `expanded_idx_to_permuted_idx` 与 `topk_scales`，对每个 token 累加其 `top_k` 个专家的加权输出：

\[
\text{output}[i] = \sum_{k=0}^{top_k-1} \text{topk\_scales}[i,k] \cdot \text{expert\_output}[\,\text{expanded\_idx\_to\_permuted\_idx}[i,k]\,]
\]

#### 4.3.3 源码精读

**JIT 生成器**：列出 moe_utils 编译的所有源（含 permute/unpermute/sort 的 CuTe-DSL 实现，以及 moe_sort 复用的三个 routing kernel），并钉死在 SM100：

[flashinfer/jit/moe_utils.py:25-40](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/moe_utils.py#L25-L40) — `gen_moe_utils_module` docstring 列出 `moePermute/moeUnpermute/moeOutputMemset/moeActivation/moeSort` 六个算子。

[flashinfer/jit/moe_utils.py:77-99](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/moe_utils.py#L77-L99) — `supported_major_versions=[10]`（仅 Blackwell）；源文件里包含 `trtllm_fused_moe_routing_deepseek.cu` 等，说明 **`moe_sort` 复用 DeepSeek-V3 routing 的 `run` 来算索引表**。

**`moe_sort` 的 Python 封装**：注意它「不搬数据，只算映射表」，且为 CUDA Graph 预留了 `out_*` 参数：

[flashinfer/fused_moe/cute_dsl/moe_utils.py:491-514](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/moe_utils.py#L491-L514) — `moe_sort` 签名，返回六元组；`out_*` 用于图捕获前预分配。

[flashinfer/fused_moe/cute_dsl/moe_utils.py:57-97](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/moe_utils.py#L57-L97) — `get_max_num_tiles` 的闭式上界公式及其最坏情况推导（注释里有完整数学论证）。

**C++ 绑定 `moe_sort`**：揭示它复用 DeepSeek-V3 routing 的 `Data` 结构，但因为专家已选好，把分组参数退化（`n_group=1, topk_group=1`）：

[csrc/moe_utils_binding.cu:326-404](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/moe_utils_binding.cu#L326-L404) — `moe_sort` 填充 `routingDeepSeek::Data`，关键注释在 [L392-396](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/moe_utils_binding.cu#L392-L396)：「For moe_sort, we use n_group=1, topk_group=1 since experts are already selected」，并关闭 softmax（`mUseRoutingSoftmax=false`）。

> 这是个很妙的复用：`moe_sort` 不重新路由，只是借 DeepSeek-V3 routing kernel 的「统计专家计数 + 算 tile 偏移」能力来生成索引表。所以 routing kernel 其实做了两件事——阶段一算 top-k（DSV3 路由用）、阶段二算索引表（moe_sort 复用阶段二）。

**`moe_permute` / `moe_unpermute` 封装与导出**：

[flashinfer/fused_moe/cute_dsl/moe_utils.py:159-229](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/moe_utils.py#L159-L229) — `moe_permute`：按 dtype 派发 `flashinfer_moe_permute_{fp16,bf16,fp8,fp4}`；FP4 时 `hidden_size` 翻倍（因 4-bit 打包）。

[flashinfer/fused_moe/cute_dsl/moe_utils.py:232-293](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/cute_dsl/moe_utils.py#L232-L293) — `moe_unpermute`：按输入 dtype 与 scale dtype 双重派发，实现加权逆排列。

[csrc/moe_utils_binding.cu:275-312](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/moe_utils_binding.cu#L275-L312) — TVM-FFI 导出：`flashinfer_moe_permute_*`、`flashinfer_moe_unpermute_*` 等，全部经 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 注册（跨语言 ABI，见 u9-l2）。

#### 4.3.4 代码实践

**实践目标**：跑通 `moe_sort`，观察它产出的六张映射表，理解 expanded↔permuted 索引如何把 token 按专家重排。

**操作步骤**（示例代码，需 Blackwell SM100 GPU；若无则改为源码阅读型实践）：

```python
# 示例代码：观察 moe_sort 的索引映射表
import torch
from flashinfer.fused_moe.cute_dsl.moe_utils import (
    moe_sort, allocate_moe_sort_buffers
)

num_tokens, num_experts, top_k = 8, 4, 2
# 构造每个 token 选 2 个专家（int32）
experts = torch.tensor([[0, 2], [1, 3], [0, 1], [2, 3],
                        [0, 0], [1, 2], [3, 0], [2, 1]],
                       device="cuda", dtype=torch.int32)
scales = torch.randn(num_tokens, top_k, device="cuda", dtype=torch.float32)

(tile_expert, tile_mn, e2p, p2e,
 total_padded, num_tiles) = moe_sort(experts, scales,
                                     num_experts=num_experts, top_k=top_k)

print("expanded_idx_to_permuted_idx:\n", e2p)        # [num_tokens, top_k]
print("permuted_idx_to_expanded_idx:\n", p2e[:total_padded.item()])
print("tile_idx_to_expert_idx:\n", tile_expert[:num_tiles.item()])
print("tile_idx_to_mn_limit:\n", tile_mn[:num_tiles.item()])
```

**需要观察的现象**：
- `e2p[t,k]` 给出第 `t` 个 token 的第 `k` 个专家在 permuted 缓冲里的位置；同一专家的所有 token 在 permuted 维度上**连续**。
- `tile_expert` 里相邻 tile 若属同一专家，对应 grouped GEMM 就能合并成更大的 GEMM。
- 改变 `top_k` 或专家分配的集中度，`total_padded` 会变化，但缓冲上界由 `get_max_num_tiles` 保证不溢出。

**预期结果 / 待本地验证**：能从 `e2p` 与 `p2e` 互相验证一致性（`p2e[e2p[t,k]] == t*top_k+k` 对本地专家成立）；`num_tiles` 不超过 `get_max_num_tiles(num_tokens, top_k, num_experts, 128)` 的返回值。

> 源码阅读型替代实践：阅读 [csrc/moe_utils_binding.cu:326-404](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/moe_utils_binding.cu#L326-L404)，逐字段说明 `routingData` 的每个成员如何对应 `moe_sort` 的输入输出，并解释为何专家已选好时要把 `n_group/topk_group` 设为 1。

#### 4.3.5 小练习与答案

**练习 1**：`moe_sort` 为什么不直接搬运数据，而只产出索引表？

> **答案**：1) 索引表（大小固定、由 `num_tokens/top_k/num_experts` 决定）可被同一前向里的多次 GEMM 复用，避免重复统计；2) 把「算映射」与「搬数据」分离，使 sort 易于进 CUDA Graph，permute 则可针对 tile 用 TMA 高效搬运；3) 解耦后同一套 sort 能服务不同 dtype 的 permute（fp16/bf16/fp8/fp4）。

**练习 2**：`expanded_idx_to_permuted_idx` 里为什么会出现 `-1`？

> **答案**：在专家并行（Expert Parallelism）下，每个 GPU 只持有部分本地专家（`num_local_experts`）。当某 token 选中的专家不在本地时，该 expanded 项没有本地 permuted 位置，记为 `-1`，表示这个 token-专家对要交给别的 GPU 处理（配合 AlltoAll，见 u8-l3）。

---

## 5. 综合实践

把三个最小模块串起来，画一张「从 router logits 到专家分组数据」的完整数据流，并标注每个阶段用的算子与方法。

**任务**：针对 DeepSeek-V3 配置（256 专家、`n_group=8`、`topk_group=4`、`top_k=8`），完成以下事项。

1. **路由阶段**：写出 `fused_topk_deepseek` 的五步算法（用本讲符号），并在算法每一步旁标注它对应 [noAuxTcKernels.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/noAuxTcKernels.cu) 的哪几行（sigmoid/bias → L83-93、组分数 → L109-118、选组选专家 → L123-147、归一化 → L207-223）。
2. **方法对比**：在同一张图上画出 Llama-4 路由（top-1）与标准 top-k 的流程，标出它们在「激活 / 选择 / 归一化」上的差异，并指出三者汇入共用的 `runPostTopKPipeline`（[trtllm_fused_moe_routing_common.cu:40](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fused_moe/trtllm_backend/trtllm_fused_moe_routing_common.cu#L40)）。
3. **搬运阶段**：在图末端画出 `moe_sort → moe_permute → 专家 GEMM → moe_unpermute` 的链路，标注 expanded/permuted 索引的流向，并解释 `moe_sort` 为何能复用 DeepSeek-V3 routing 的 kernel（提示：[moe_utils_binding.cu:392-396](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/moe_utils_binding.cu#L392-L396)）。
4. **运行验证（可选）**：运行 4.1.4 的实践代码，把 kernel 输出与你的手算结果对照，记录任何因并列导致的差异。

**验收标准**：能不看讲义，口头讲清「为什么 DeepSeek-V3 用 sigmoid+分组、为什么三种路由方法能共用下游、permute 与 sort 为何分离」这三件事。

---

## 6. 本讲小结

- DeepSeek-V3 路由是**分组两段软路由**：sigmoid 加偏置 → 每组取 top-2 求和得组分数 → 选 top `topk_group` 组 → 组内选 top-k 专家 → 用 **sigmoid 分数**（不含偏置）求和归一并乘 `routed_scaling_factor`。`fused_topk_deepseek` 是其独立 Python 入口，由单 kernel `deepseek_v3_topk_kernel` 完成。
- 参数校验里的数字（`topk<=8`、每组 `<=32`、`<=128`、`<=384`）直接对应 kernel 的编译期常量与三条模板分派路径。
- `RoutingMethodType` 用「激活-选择-归一化」三元组区分六种方法；Llama-4 是 top-1（仅 sigmoid），标准 top-k 在全部专家上取最大。后两者目前嵌在 trtllm-gen MoE 后端内部，无独立 Python 算子。
- 所有路由方法在产出 top-k 后，**共用** `runPostTopKPipeline` 这一条下游，体现了「阶段一方法各异、阶段二完全统一」的架构。
- `moe_utils` 的 `moe_sort`（算索引表，不搬数据）/ `moe_permute`（按专家重排）/ `moe_unpermute`（加权逆排列）把路由结果落成 tile 对齐的物理布局，仅支持 SM100；`moe_sort` 巧妙复用 DeepSeek-V3 routing kernel 的统计能力（退化为 `n_group=1`）。
- 索引体系 expanded↔permuted 是理解 MoE 数据搬运的钥匙，`-1` 标记专家并行下的非本地专家。

---

## 7. 下一步学习建议

- **u6-l4（量化 MoE 与 MoELayer 派发）**：本讲的路由产出 `topk_ids/topk_weights`，下一讲看它如何喂给 `MoELayer` 在多后端间派发，并叠加 FP8/FP4 量化。
- **u8-l3（AlltoAll 与多节点）**：本讲提到 `expanded_idx_to_permuted_idx == -1` 对应非本地专家，多卡场景下这些 token 要经 `moe_a2a_dispatch/combine` 跨 GPU 流动，可在通信单元把这条链路补全。
- **延伸阅读**：对照 TensorRT-LLM 的 `GroupedGemmInputsHelper`（`get_max_num_tiles` 的原始出处）与 DeepSeek-V3 原论文的 auxiliary-loss-free load balancing 一节，能更深刻理解分组软路由的负载均衡动机。
