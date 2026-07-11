# MoE 模块与专家负载均衡

## 1. 本讲目标

本讲聚焦 LMDeploy PyTorch 后端对 **MoE（Mixture of Experts，混合专家）** 模型的支持。学完后你应当能够：

1. 说清 MoE 的「门控路由（gate / route）+ 多专家 FFN」基本结构，以及为什么每个 token 只激活 `top_k` 个专家。
2. 看懂 `nn/moe/` 下 MoE 算子的统一抽象：`FusedMoEBase` 的 `dispatch → gemm → combine` 三段流水线，以及 `Default / DSAsyncDecode / DSAsyncPrefill` 三种执行模式。
3. 看懂按权重量化类型自动派发的三种 MoE 变体：`FusedMoE`（FP16/BF16）、`FusedMoEW8A8`（SmoothQuant）、`FusedMoEBlockedF8`（分块 FP8）。
4. 理解 **EPLB（Expert-Level Load Balancing，专家级负载均衡）** 如何通过复制热点专家，减少专家并行（EP）all-to-all 通信的负载不均。

本讲是 u5-l2（线性层与量化变体）的直接延续：线性层那套「薄包装 + 委托 + 按 `quant_method` 派发」的模式在这里被原样搬到了 MoE 上，只是规模从一个矩阵变成了「每个专家一组矩阵」。

## 2. 前置知识

### 2.1 什么是 MoE

普通 Transformer 的每一层有一个 FFN（前馈网络），所有 token 都走同一个 FFN。MoE 层则把单个 FFN 换成 **N 个并列的「专家 FFN」**，并加一个 **门控（gate / router）** 网络。对每个 token，门控计算它对每个专家的得分，然后只选 **得分最高的 `top_k` 个专家** 去处理它，最后把这 `top_k` 个专家的输出按得分加权求和。

直观理解：

- **稀疏激活**：一个 token 只动用 `top_k` 个专家（如 DeepSeek-V2 的 `top_k=6`，256 个路由专家），计算量远小于让所有专家都跑一遍。这就是 MoE「参数多、算得快」的根本原因。
- **路由（routing）**：决定「这个 token 该去哪个专家」的过程。
- **专家分发（dispatch）**：把 token 按「它选中的专家」重新分组送到对应专家的 FFN。
- **聚合（combine）**：各专家算完后，把结果按原顺序拼回去并加权求和。

### 2.2 与上一讲的衔接

在 u5-l2 我们看到，普通线性层统一采用「薄包装 + 委托」桥接模式：

```
构造: get_backend() → get_layer_impl_builder(OpType.X) → self.impl
前向: self.impl.forward(...)
```

MoE 层把多个线性层打包成一个「融合 MoE」算子，但**桥接模式完全一致**：`nn/moe/` 下每个类都通过 `OpType.FusedMoE / OpType.FusedMoEBlockedF8 / OpType.RouterNoauxTC / OpType.SoftmaxTopK` 从 `backends` 取一个 `impl`，真正的 kernel 藏在 `backends/cuda`、`backends/default` 等设备目录里。本讲只讲「nn 包装层怎么用」，不深入 kernel。

### 2.3 几个分布式概念

MoE 在多卡上常用 **专家并行（Expert Parallelism, EP）**：把 N 个专家分散到多张卡上，每张卡只持有部分专家。此时 token 要送到「持有它选中专家的那张卡」上计算，于是产生 **all-to-all 通信**（每张卡都要和别的卡互换 token）。EPLB 要解决的就是这种通信的负载不均。

此外还有 **张量并行（TP）**（一个专家的矩阵切到多张卡）和 **数据并行（DP）**。`config.py` 的 `DistConfig` 用 `tp / dp / ep` 三个旋钮描述这套拓扑（u3-l2 已讲）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/pytorch/nn/moe/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py) | MoE 抽象基类 `FusedMoEBase`、执行模式枚举 `MoeType`、`SoftmaxTopK`、DP-TP 分轮 GEMM、跨卡 gather/reduce 工具 |
| [lmdeploy/pytorch/nn/moe/route.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/route.py) | DeepSeek 风格的 `NoauxTCRouter` 路由（带偏置与分组） |
| [lmdeploy/pytorch/nn/moe/\_\_init\_\_.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/__init__.py) | `build_fused_moe` 工厂：按 `quant_method` 选 MoE 变体 |
| [lmdeploy/pytorch/nn/moe/default.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/default.py) | `FusedMoE`（默认 FP16/BF16）与专家权重容器 `LinearWeights` |
| [lmdeploy/pytorch/nn/moe/blocked_fp8.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/blocked_fp8.py) | `FusedMoEBlockedF8`（分块 FP8）与在线量化权重加载 |
| [lmdeploy/pytorch/nn/moe/w8a8.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/w8a8.py) | `FusedMoEW8A8`（SmoothQuant，int8 权重+激活） |
| [lmdeploy/pytorch/nn/eplb.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py) | EPLB 专家负载均衡：复制热点专家、逻辑→物理映射 |
| [lmdeploy/pytorch/backends/default/moe_router.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/default/moe_router.py) | 路由 impl 真身：`noaux_tc` 的 top-k 选择与重归一化 |
| [lmdeploy/pytorch/models/deepseek_v2.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py) | 真实模型如何把 gate、FusedMoE、EPLB 拼成一个 MoE 层 |
| [lmdeploy/pytorch/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py) | `DistConfig`：`ep / dp / enable_eplb` 等拓扑字段 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**MoE base（融合 MoE 的三段流水线）**、**FusedMoE route（门控路由与 top-k 选择）**、**EPLB（专家负载均衡）**，并在第一模块末尾附带「MoE 量化变体」的派发说明。

### 4.1 MoE base：融合 MoE 与 dispatch/gemm/combine 三段流水线

#### 4.1.1 概念说明

「融合 MoE（Fused MoE）」的思想是：不把 N 个专家实现成 N 个独立的 `nn.Linear`，而是把所有专家的权重堆成一个三维张量 `(num_experts, out_features, in_features)`，再用一个融合 kernel 一次性完成「分发 + 各专家 GEMM + 聚合」。这样能大幅减少访存与 kernel launch 开销。

但 MoE 的前向并非铁板一块。在 EP（专家并行）下，token 要跨卡送到对应专家，这一步是 **通信密集** 的；而各专家的 GEMM 是 **计算密集** 的。为了把通信与计算重叠，LMDeploy 把 MoE 前向拆成三段：

1. **dispatch（分发）**：把 token 按选中的专家送到位（单卡时只是整理顺序，多卡时是 all-to-all）。
2. **gemm（矩阵乘）**：各专家在自己的位置上做 gate_up 与 down 两组 GEMM。
3. **combine（聚合）**：把各专家输出按得分加权、按原 token 顺序收回来（多卡时是反向 all-to-all / reduce-scatter）。

这三段在不同执行模式下有不同实现：同步的 `Default` 模式用普通 kernel；DeepSeek 异步模式 `DSAsyncDecode / DSAsyncPrefill` 用 `dispatch_async / combine_async` 让通信与计算重叠（依赖 DeepEP 之类的通信库）。

#### 4.1.2 核心流程

`FusedMoEBase` 只定义抽象骨架，把 `dispatch / gemm / combine / wait` 留给子类实现。默认同步前向 `forward_default` 把这三段串起来：

```
forward_default(hidden_states, topk_weights, topk_idx):
    state = {hidden_states, topk_idx, topk_weights, moe_type=Default}
    state = self.dispatch(state)      # 分发：单卡整理，多卡 all-to-all
    state = self.gemm(state)          # 计算：各专家 gate_up + down
    state = self.combine(state)       # 聚合：加权求和，多卡 reduce
    return state['hidden_states']
```

执行模式由 `MoeType` 枚举决定：`base.py` 顶部定义了三种。

#### 4.1.3 源码精读

先看执行模式枚举与三段流水线的抽象方法签名：

[base.py:16-21](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py#L16-L21) —— `MoeType` 枚举：`Default`（同步、单 kernel）、`DSAsyncDecode`（DeepSeek 异步 decode，低延迟模式）、`DSAsyncPrefill`（DeepSeek 异步 prefill）。

[base.py:274-292](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py#L274-L292) —— `FusedMoEBase` 把 `before_dispatch / dispatch / gemm / combine / wait` 都声明为 `raise NotImplementedError`，强制子类按量化类型各自实现。注意它接收的是 `state` 字典而非零散参数，这样异步模式可以把 `handle / event / hook`（通信句柄与同步钩子）塞进同一个字典在阶段间传递。

默认同步前向与总入口：

[base.py:299-317](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py#L299-L317) —— `forward_default` 串起 dispatch→gemm→combine；`forward` 根据 `tp_mode` 决定走 `forward_dptp`（DP-TP 分轮 GEMM，见下）还是 `forward_default`。

`MoeType` 之所以重要，是因为同一个 `FusedMoE` 子类的 `dispatch/gemm/combine` 里要按它分三个分支：异步 prefill 调 `dispatch_async`、异步 decode 调低延迟版的 `dispatch_async`、默认调纯本地的 `moe_gather_inputs`。这点在 4.3 的 `blocked_fp8.py` 里会清楚看到。

**DP-TP 分轮 GEMM**（选读，理解通信-计算重叠的关键）。当 `tp_mode == TPMode.DP_TP` 时，MoE 前向走 `MoEForwardDPTP`：它把一批 token 切成若干「轮」，每轮先 `all_gather`（把各 TP 份 token 收齐）→ 算一轮 GEMM → `reduce_scatter`（把结果散回各卡），并用 `async_op=True` 的 handle 把「下一轮的 gather」与「这一轮的 GEMM+reduce」重叠起来：

[base.py:137-193](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py#L137-L193) —— `MoEForwardDPTP.forward` 的主循环：`pre（先取一轮）→ while 还有剩余：next_inputs = 取下一轮；gemm+reduce_scatter(当前轮)`。这种「预取下一批输入写在等待之前」的思路，和 u4-l2 EngineLoop 的「预取与 forward 重叠」是同一种工程手法。

跨卡 gather/reduce 的工具函数也很关键，它们是 `dispatch/combine` 在 `Default` 模式下处理多卡的方式：

[base.py:54-91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py#L54-L91) —— `moe_gather_inputs`（dispatch 侧，`TPMode.DEFAULT` 时原样返回、`DP_TP` 时按 `moe_tp_sizes` gather）与 `moe_reduce`（combine 侧，`DEFAULT` 用 `all_reduce`、`DP_TP` 用 `reduce_scatter_by_tp_sizes`）。

#### 4.1.4 代码实践

1. **实践目标**：建立「融合 MoE = dispatch/gemm/combine 三段」的直观认识，能对着源码说出每一步做了什么。
2. **操作步骤**：
   - 打开 [base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py)，定位 `FusedMoEBase.forward`（L312）与 `forward_default`（L299）。
   - 打开 [default.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/default.py) 的 `FusedMoE.dispatch / gemm / combine`（L205 / L271 / L317），对照 `MoeType.Default` 分支，分别确认它们在 `Default` 模式下调用了 `moe_gather_inputs / self.impl.forward / moe_reduce`。
3. **需要观察的现象**：三个方法都通过 `state['moe_type']` 做分支，`Default` 分支最简单（纯本地），`DSAsync*` 分支带 `handle/event/hook`。
4. **预期结果**：你能用一句话复述——「`Default` 模式下，dispatch 整理输入、gemm 调融合 kernel、combine 做 all_reduce；`DSAsync*` 模式把这三段换成异步通信以重叠计算」。
5. 若无法本地运行，标注「待本地验证」并完成纯源码阅读即可。

#### 4.1.5 小练习与答案

**练习 1**：`FusedMoEBase` 为什么把 `dispatch/gemm/combine` 设计成接收并返回 `state` 字典，而不是一组张量参数？

**答案**：因为异步模式（`DSAsync*`）需要在三段之间传递通信句柄 `handle`、同步事件 `event`、低延迟钩子 `hook` 等非张量状态；用字典可以在不改变方法签名的前提下，让不同 `MoeType` 各自塞入/读取所需字段，保持接口统一。

**练习 2**：`MoEForwardDPTP` 的主循环里，`__slice_and_gather()` 既在循环开头调用、又在循环内 `next_inputs = __slice_and_gather()` 调用，为什么要「取两次」？

**答案**：这是「预取」手法——循环内先发起**下一轮**的 gather（通信），紧接着对**当前轮**做 GEMM+reduce_scatter（计算）。gather 的通信与 GEMM 的计算因此重叠，隐藏了 all-gather 延迟。循环开头那一次是为第一轮准备输入。

---

### 4.2 FusedMoE route：门控路由与 top-k 专家选择

#### 4.2.1 概念说明

4.1 讲的是「给定 `topk_idx` 与 `topk_weights` 之后，MoE 怎么算」。本节回答更前面的问题：**`topk_idx` 和 `topk_weights` 从哪来？** 答案是 **门控（gate / router）**。

门控本身是一个很小的线性层：对 hidden_states 做一次投影得到每个专家的 `router_logits`，再按某种规则选 `top_k` 个。但「怎么选」在不同模型里花样繁多：

- **最朴素**：softmax 后直接全局 top-k（`SoftmaxTopK`）。
- **DeepSeek-V2 的 `group_limited_greedy`**：先把专家分组，先选「最好的几组」，再在组内选 top-k，避免某些组被冷落。
- **DeepSeek-V3 的 `noaux_tc`**：在得分上加一个可学习的偏置 `e_score_correction_bias`（auxiliary-loss-free 负载均衡），再做分组 top-k。

LMDeploy 把这些路由策略抽象成两个 nn 模块：`SoftmaxTopK`（基础）与 `NoauxTCRouter`（DeepSeek-V3 风格），二者都委托给 `backends` 的 impl。

#### 4.2.2 核心流程

以 DeepSeek-V3 的 `noaux_tc` 为例，完整的 gate 计算与 top-k 选择流程是：

```
# 1. 算路由 logits（模型里的 MoEGate.forward）
router_logits = Linear(hidden_states, gate_weight)   # [num_tokens, num_experts]

# 2. 算每个专家的得分（sigmoid 或 softmax）
scores = sigmoid(router_logits)            # 或 softmax

# 3. 加上偏置，用于「无辅助损失的负载均衡」
scores_for_choice = scores + e_score_correction_bias

# 4. 分组：先在每组里选 per_group_top_k，再合并（或 group_limited_greedy）
#    得到 topk_idx（每个 token 选中的 top_k 个专家编号）

# 5. 用原始 scores（不带偏置）按 topk_idx 取权重
topk_weight = scores.gather(topk_idx)

# 6. 重归一化（可选）+ 乘 routed_scaling_factor
topk_weight = topk_weight / topk_weight.sum() * routed_scaling_factor
```

关键点：**第 3 步的偏置只用于「选谁」，不进入最终的权重**。这是 `noaux_tc`（no auxiliary loss, top-k with correction bias）的精髓——用偏置引导选择以均衡负载，但输出仍是干净的 sigmoid 得分，避免辅助损失对主任务的干扰。

#### 4.2.3 源码精读

nn 包装层只有薄薄一层委托：

[route.py:8-38](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/route.py#L8-L38) —— `NoauxTCRouter`：构造时从 `backends` 取 `OpType.RouterNoauxTC` 的 impl，`forward(router_logits, e_score_correction_bias)` 直接转交 `self.impl.forward`。它把 `scoring_func / top_k / n_group / topk_group / n_routed_experts / routed_scaling_factor / renormalize / router_n_groups` 这些路由超参透传给 impl。

`SoftmaxTopK` 同样是薄包装：

[base.py:23-34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py#L23-L34) —— `SoftmaxTopK(top_k, n_groups)`，委托 `OpType.SoftmaxTopK` 的 impl。

真正的 top-k 选择算法在 `backends/default/moe_router.py`，这是「gate 计算与 top-k 流程」的 impl 真身：

[backends/default/moe_router.py:92-104](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/default/moe_router.py#L92-L104) —— `DefaultRouterNoauxTCImpl.forward`：算 scores → `scores + bias` → 按 `router_n_groups > 0` 走「分组选」或默认「group_limited_greedy」→ `renorm`（重归一化 + 乘 scaling factor）。这正是 4.2.2 流程图的代码实现。

[backends/default/moe_router.py:66-80](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/default/moe_router.py#L66-L80) —— `_forward_default`（即 `group_limited_greedy`）：先用每组的 top-2 之和选出 `topk_group` 个组，组外得分置 0，再在剩余专家里全局 `torch.topk` 选 `top_k`，最后用原始 `scores.gather` 取权重。注意 `topk_weight` 取的是 `scores`（第 78 行）而非 `scores_for_choice`，体现了「偏置只用于选择」。

[backends/default/moe_router.py:82-90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/default/moe_router.py#L82-L90) —— `renorm`：当 `renormalize=True` 时把权重归一化到和为 1，再统一乘 `routed_scaling_factor`。

**真实模型如何把 gate 与 FusedMoE 拼起来**——看 DeepSeek-V2 的 `MoEGate`：

[models/deepseek_v2.py:639-672](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py#L639-L672) —— `MoEGate.forward`：先用 `F.linear` 算 `router_logits`（L641），再按 `topk_method`（`greedy` / `group_limited_greedy` / `noaux_tc`）分别调 `softmax_topk` 或 `noaux_tc_router` 得到 `topk_weight, topk_idx`。最后若启用了 EPLB（`self.eplb_dispatch_info is not None`），把逻辑专家 ID 翻译成物理专家 ID（L669-670，详见 4.4）。

[models/deepseek_v2.py:734-754](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py#L734-L754) —— `DeepseekV2MoE.forward`：`topk_weights, topk_ids = self.gate(hidden_states)`（路由）→ `out_states = self.experts(hidden_states, topk_weights, topk_ids)`（融合 MoE 计算）→ 叠加 shared expert → 必要时 `all_reduce`。这就是「gate 产 top-k、experts 消费 top-k」的完整衔接。

#### 4.2.4 代码实践

> 这是本讲指定的两项实践之一。

1. **实践目标**：在 `moe/route.py` 与 `backends/default/moe_router.py` 中梳理出 top-k 专家选择与 gate 计算流程。
2. **操作步骤**：
   - 读 [route.py:35-38](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/route.py#L35-L38)，确认 `NoauxTCRouter.forward(router_logits, e_score_correction_bias)` 的两个输入分别是「门控线性层的输出」和「每个专家的偏置」。
   - 读 [backends/default/moe_router.py:92-104](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/default/moe_router.py#L92-L104)，画出 `router_logits → scores → +bias → 分组topk → renorm → (topk_weight, topk_idx)` 的数据流。
   - 读 [models/deepseek_v2.py:641-665](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py#L641-L665)，确认 `router_logits` 由 `F.linear(hidden_states, self.weight)` 产生，并对比 `greedy / group_limited_greedy / noaux_tc` 三条分支的差异。
3. **需要观察的现象**：`topk_weight` 来自不带偏置的 `scores`，而「选哪些专家」用了带偏置的 `scores_for_choice`。
4. **预期结果**：你能写出流程图，并解释「为什么偏置只影响选择、不影响权重」——这是 `noaux_tc` 实现无辅助损失负载均衡的关键。
5. 本实践为源码阅读型，不需要 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`NoauxTCRouter` 构造参数里的 `n_group` 与 `topk_group` 各是什么含义？

**答案**：`n_group` 是把全部 `n_routed_experts` 个专家分成多少组；`topk_group` 是先从这些组里选出「最好的几组」（按每组 top-2 得分之和排序），只在被选中的组里再做最终 top-k。这是一种「先粗筛组、再细筛专家」的两级选择，迫使路由分散到不同组、缓解负载倾斜。

**练习 2**：`router_n_groups > 0` 时走 `_forward_router_n_groups`，它和默认的 `_forward_default` 有何区别？

**答案**：`_forward_router_n_groups`（[moe_router.py:54-64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/default/moe_router.py#L54-L64)）是「每组强制选 `per_group_top_k = top_k // router_n_groups` 个专家」的均衡策略，保证每组都有专家被选中；而 `_forward_default` 是先选组、组外置零再全局 topk，是 DeepSeek-V2 的 `group_limited_greedy`。

---

### 4.3 MoE 量化变体：build_fused_moe 的派发与权重布局

#### 4.3.1 概念说明

承接 u5-l2：普通线性层按 `quant_method`（`None / awq / smooth_quant / fp8`）选不同的 Python 类。MoE 完全沿用这套机制——`build_fused_moe` 是 MoE 版的 `build_linear`：

| `quant_method` | 选中的类 | 权重 dtype | 额外参数 |
| --- | --- | --- | --- |
| `None` | `FusedMoE`（default.py） | FP16/BF16 | 无 |
| `smooth_quant` | `FusedMoEW8A8`（w8a8.py） | int8 | 每专家 per-output-channel `scale` |
| `fp8` | `FusedMoEBlockedF8`（blocked_fp8.py） | fp8_e4m3 | 每 128×128 块的 `weight_scale_inv` |

三者都继承 `FusedMoEBase`，复用 4.1 的 dispatch/gemm/combine 骨架；区别只在「权重怎么存、怎么加载、gemm 调哪个 impl」。注意 MoE 目前**不支持 AWQ**（`build_fused_moe` 没有 `awq` 分支），量化只覆盖 `smooth_quant` 与 `fp8`。

#### 4.3.2 核心流程

```
build_fused_moe(..., quant_config, prefix):
    quant_method = quant_config.get_quant_method(prefix, module_kind='moe')
    if quant_method is None:      → FusedMoE          (OpType.FusedMoE)
    elif quant_method == 'smooth_quant': → FusedMoEW8A8
    elif quant_method == 'fp8':           → FusedMoEBlockedF8 (OpType.FusedMoEBlockedF8)
```

`quant_method` 的判定与 u5-l2 一致：由 `QuantizationConfig.get_quant_method(prefix, module_kind='moe')` 决定，受 `fp8_quant_scope`（如 `'moe_only'`）等影响——所以一个 MoE 模型里「dense MLP 走 FP16、MoE 层走 FP8」是常见组合。

每个变体内部都有两个「专家权重容器」：`gate_up`（融合的 gate + up 投影，对应 4.1 里两个并排矩阵）与 `down`（下投影）。容器的布局随量化方式变化。

#### 4.3.3 源码精读

派发工厂：

[\_\_init\_\_.py:11-83](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/__init__.py#L11-L83) —— `build_fused_moe`：先 `get_quant_method(prefix, module_kind='moe')`，再按结果 `import` 对应变体并构造。注意是**延迟导入**（函数内 `from .default import FusedMoE`），避免在不需要量化的路径上加载额外依赖。

**默认变体 `FusedMoE`** 的权重容器是 `LinearWeights`：

[default.py:15-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/default.py#L15-L58) —— `LinearWeights`：权重形状 `(num_experts, out_features, in_features)`，`half_out = out_features // 2` 把 gate/up 两段拼在 `out_features` 维。`setup_weight_loader` 按 `expert_list` 是否为空选择 **EP 切分**（`weight_loader_ep`，按专家分卡）或 **TP 切分**（`weight_loader_tp`，按 dim 分卡）。这里的 `weight_loader` 与 u3-l5 的权重加载契约对接——HF 权重经 `shard_id`（`gate/up/down`）与 `expert_id` 精确落到三维张量的对应位置。

[default.py:67-106](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/default.py#L67-L106) —— `weight_loader_tp`：`gate/up` 按 `dim=0`（输出维）切分到各卡，`down` 按 `dim=1`（输入维）切分。这与 u5-l2 普通线性层的 colwise/rowwise 切分是同一套，只是多了个 `expert_id` 维。

**FP8 变体 `FusedMoEBlockedF8`** 多出每块 scale 与在线量化：

[blocked_fp8.py:41-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/blocked_fp8.py#L41-L53) —— `LinearWeightsBlockedF8` 在权重之外注册 `weight_scale_inv`，形状 `(num_experts, out//128, in//128)`，即每 128×128 块一个 fp32 scale（`block_size=128`）。这与 u5-l2 的 `BlockedF8Linear` 布局一致。

[blocked_fp8.py:128-140](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/blocked_fp8.py#L128-L140) —— `weight_loader_with_quant`：若加载进来的权重 `dtype` 与目标 `param.dtype`（fp8）不符，就在加载期调用 `quant_blocked_fp8` **在线压成 fp8** 并算出 scale，再委托 base loader 落位。这是「FP8 模型用 FP16 权重保存、加载时再量化」这条省盘空间路径的 MoE 版本。

[blocked_fp8.py:143-227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/blocked_fp8.py#L143-L227) —— `FusedMoEBlockedF8.__init__`：固定 `block_size=128`，从 `OpType.FusedMoEBlockedF8` 取 impl，并按 `ep_size > 1` 决定是否按专家切分（`expert_list`）。

**W8A8 变体 `FusedMoEW8A8`** 的 scale 是 per-output-channel：

[w8a8.py:32-34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/w8a8.py#L32-L34) —— `LinearWeightsW8A8` 注册 `scale`，形状 `(num_experts, out_features, 1)`，即每个专家每个输出通道一个 int8 scale（SmoothQuant 风格）。这与 `W8A8Linear`（u5-l2）的 per-output-channel scale 一致。

**真实模型对 `build_fused_moe` 的调用**：

[models/deepseek_v2.py:706-717](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py#L706-L717) —— `DeepseekV2MoE.__init__` 把 `quantization_config` 透传给 `build_fused_moe`，由后者按 config 自动选 FP16/W8A8/FP8 实现；模型文件本身**不写 `if quant` 分支**，这与 u3-l4 的 Llama 重写哲学完全相同。

#### 4.3.4 代码实践

1. **实践目标**：对比三种 MoE 变体的权重布局，理解「同一骨架、不同权重容器」的设计。
2. **操作步骤**：
   - 在 [\_\_init\_\_.py:33-80](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/__init__.py#L33-L80) 列出 `build_fused_moe` 的三个分支及各自构造的类。
   - 对比 [default.py:28](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/default.py#L28)（`weight: (E, out, in)`，FP16）、[w8a8.py:32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/w8a8.py#L32)（多 `scale: (E, out, 1)`，int8）、[blocked_fp8.py:41-44](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/blocked_fp8.py#L41-L44)（多 `weight_scale_inv: (E, out//128, in//128)`，fp8）三者额外张量。
3. **需要观察的现象**：三个变体的 `dispatch/combine` 代码几乎一样（继承自基类），差异集中在「权重容器」与「gemm 里调哪个 impl」。
4. **预期结果**：你能填出下面这张表：

| 变体 | weight dtype | scale 形状 | scale 粒度 |
| --- | --- | --- | --- |
| FusedMoE | fp16/bf16 | 无 | — |
| FusedMoEW8A8 | int8 | (E, out, 1) | per-output-channel |
| FusedMoEBlockedF8 | fp8_e4m3 | (E, out//128, in//128) | per 128×128 block |

5. 本实践为源码阅读型。

#### 4.3.5 小练习与答案

**练习**：为什么 `FusedMoEBlockedF8` 的 `gate/up` 切分用 `_chunk_weight_tp(..., align=self.block_size)`，而 `FusedMoE` 的 `gate/up` 切分只用普通 `chunk(..., dim=0)`？

**答案**：FP8 分块量化的 scale 是按 128 对齐的块组织的（见 [blocked_fp8.py:113-126](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/blocked_fp8.py#L113-L126) 的 `weight_loader_scale_tp` 用 `half_out = half_out // block_size`）。若不按 128 对齐切分权重，权重块与 scale 块的边界就会错位，导致反量化用错 scale。所以 FP8 必须用 `split_size(..., align=block_size)`（[base.py:45-51](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/moe/base.py#L45-L51)）保证切分点落在块边界。普通 FP16 没有块结构，直接 `chunk` 即可。

---

### 4.4 EPLB：专家级负载均衡

#### 4.4.1 概念说明

**问题**：MoE 的路由是数据相关的——某些「热门专家」会被大量 token 选中，而冷门专家几乎没人用。在 EP（专家并行）下，热门专家所在的那张 GPU 会成为瓶颈：它收到的 token 远多于别的卡，all-to-all 通信与计算都严重不均，整体吞吐被最慢的卡拖垮（木桶效应）。传统的「辅助损失（auxiliary loss）」在训练时缓解这个问题，但推理时路由已固定，无法再用梯度调整。

**EPLB（Expert-Level Load Balancing）的思路**：既然不能改路由，那就**复制热门专家**。给每个 GPU 分配比「逻辑专家数 / GPU 数」更多的「物理专家」槽位（`num_physical_experts = num_routed_experts + num_redundant_experts`），把高频逻辑专家复制成多份物理副本，分散到不同 GPU 上；路由命中某个逻辑专家时，就近（或随机）挑它的一份物理副本来算，从而把负载摊平。

这里出现一对关键概念：

- **逻辑专家（logical expert）**：模型原本的专家编号，路由输出的 `topk_idx` 是逻辑 ID。
- **物理专家（physical expert）**：实际存在某张 GPU 上的专家槽位。一个逻辑专家可对应多个物理副本（`logcnt ≥ 1`）。

EPLB 要维护的核心是一张 **逻辑→物理的映射表**，并用统计出的专家负载（`weight`）决定「复制谁、放哪张卡」。负载统计来自一份离线采集的 JSON（每个 layer × 每个专家被命中的 token 数）。

一句话回答实践任务：**EPLB 通过把高频（被命中最多）的逻辑专家复制成多份物理副本并均匀分散到各 GPU，让每张 GPU 在 EP all-to-all 时收发的 token 数大致相等，从而消除通信与计算的负载不均。**

#### 4.4.2 核心流程

EPLB 的离线规划（构造映射表）与在线查表（推理时翻译 `topk_idx`）两部分：

```
# 离线规划（引擎启动时，EPLBMetadata.init）
1. 读取专家负载统计 weight: [num_layers, num_routed_experts]
2. rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus):
     - replicate_experts: 把高频逻辑专家复制成多份物理副本
     - balanced_packing: 把物理副本近似均衡地装到各 GPU（装箱问题）
   产出 phy2log: [num_layers, num_physical_experts]  （每个物理槽→逻辑专家）
3. compute_logical_to_rank_dispatch_physical_map:
   为每个 (gpu, layer, logical_expert) 选一个本地优先的物理副本
   产出 logical_to_rank_dispatch_physical_map: [num_gpus, num_layers, num_logical]

# 在线查表（每次 forward，gate 之后）
topk_ids_logical_to_physical(topk_ids, dispatch_info):
    # 把路由选出的「逻辑专家 ID」随机映射到它的某个「物理副本 ID」
    chosen = randint % num_valid_copies(topk_id)
    topk_ids = logical_to_all_physical_map[topk_ids, chosen]
```

「装箱」用的 `balanced_packing` 是一个贪心算法：把专家按负载从大到小排序，逐个放入「当前累计负载最小」的包（GPU），使各包总负载尽量接近。

#### 4.4.3 源码精读

**复制热点专家**：

[eplb.py:44-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L44-L58) —— `replicate_experts`：从 `num_log` 到 `num_phy`，每次循环用 `weight / logcnt`（已复制次数摊薄后的「单位负载」）找当前最重的逻辑专家，把第 `i` 个冗余物理槽分给它，并 `logcnt += 1`。直观说：谁被命中最多、且副本还少，就再复制一份。

**贪心装箱**：

[eplb.py:17-41](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L17-L41) —— `balanced_packing`：把每个 layer 的专家组按负载降序排，依次塞进「累计负载最小」的包。这是经典的「最小负载优先」装箱（LPT-like），目标是最小化最大包负载。

**层级化重均衡**（多节点场景）：

[eplb.py:67-99](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L67-L99) —— `rebalance_experts_hierarchical`：先在「节点」层做装箱（`balanced_packing(tokens_per_group, num_nodes)`），再在节点内「GPU」层做装箱，两段映射复合出最终的 `phy2log`。这样多机部署时通信尽量留在节点内（NVLink 域）。

**总入口**：

[eplb.py:102-118](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L102-L118) —— `rebalance_experts`：根据 `num_groups % num_nodes` 是否整除，选择层级化或简单复制，最终返回 `phy2log`（物理→逻辑）、`log2phy`（逻辑→物理，反向）、`logcnt`（每个逻辑专家的副本数）。

**在线翻译**（推理时把逻辑 ID 翻成物理 ID）：

[eplb.py:294-302](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L294-L302) —— `topk_ids_logical_to_physical`：对每个被选中的逻辑专家，在其有效物理副本数范围内随机挑一个（`randint % num_valid`），再查 `logical_to_all_physical_map` 得到物理 ID。随机选是为了把同一逻辑专家的多次命中均匀分摊到它的多个副本上。

**元数据与全局管理**：

[eplb.py:218-250](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L218-L250) —— `EPLBMetadata.init`：从 `eplb_experts_statistic_file` 读负载统计（读不到则用默认的递减序列做演示），调 `rebalance_experts` 算映射，再算每个 GPU 的派发表。

[eplb.py:311-331](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L311-L331) —— `EPLBManager`：对外门面，`init_global_eplb_metadata`（启动时建表）、`num_physical_experts`（注意开了 EPLB 后 `num_experts` 变成物理专家数，见 [deepseek_v2.py:702](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py#L702)）、`topk_ids_logical_to_physical`（推理时查表）、`get_dispatch_info`（取本卡本层的派发信息）。

**开关与环境变量**：

[config.py:157-164](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L157-L164) —— `DistConfig` 的 `ep`（专家并行度）与 `enable_eplb`（是否启用 EPLB）。注意 [config.py:193](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L193) 的推导：`moe_tp = moe_tp or (1 if ep > 1 else mlp_tp)`——一旦走 EP，MoE 层就不再做 TP（因为专家已经分卡，没必要再切矩阵）。

[envs.py:168-172](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/envs.py#L168-L172) —— 四个 EPLB 环境变量：`LMDEPLOY_EPLB_NUM_GROUPS`（默认 4，分组数）、`LMDEPLOY_EPLB_EXPERTS_STATISTIC_FILE`（负载统计 JSON 路径）、`LMDEPLOY_EPLB_RANKS_PER_NODE`（默认 8，每节点 GPU 数）、`LMDEPLOY_EPLB_NUM_REDUNDANT_EXPERTS`（默认 32，冗余物理专家数）。

用户侧开关在 `PytorchEngineConfig`：

[messages.py:429-457](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L429-L457) —— `ep` 与 `enable_eplb` 字段（L429、L457），构造 `PytorchEngineConfig(ep=N, enable_eplb=True)` 即可启用。

#### 4.4.4 代码实践

> 这是本讲指定的第二项实践。

1. **实践目标**：用一句话解释 EPLB 如何减少 MoE all-to-all 通信不均，并能在源码中指证「复制 + 装箱 + 查表」三步。
2. **操作步骤**：
   - 读 [eplb.py:44-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L44-L58)（`replicate_experts`，复制热点专家）与 [eplb.py:17-41](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L17-L41)（`balanced_packing`，均衡装箱）。
   - 读 [eplb.py:294-302](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/eplb.py#L294-L302)（`topk_ids_logical_to_physical`，推理时把逻辑 ID 翻译成物理副本）。
   - 读 [models/deepseek_v2.py:669-670](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py#L669-L670)，确认「gate 算完 top-k 后，立刻调 `EPLBManager.topk_ids_logical_to_physical` 把逻辑 ID 翻成物理 ID，再交给 FusedMoE」。
3. **需要观察的现象**：复制只发生在「负载最重」的逻辑专家上；翻译时用 `randint % num_valid` 在多个副本间随机挑一个。
4. **预期结果**：你能写出这句话——「EPLB 把高频逻辑专家复制成多份物理副本并均衡地分散到各 GPU，路由命中时随机选一份本地副本，使各卡 all-to-all 的收发量大致相等，从而消除木桶效应」。
5. （可选，需多卡）若条件允许，可阅读 [models/deepseek_v2.py:979-981](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/deepseek_v2.py#L979-L981) 确认 EPLB 元数据在引擎启动、`enable_eplb` 且 `ep_size > 1` 时才初始化；本地无多卡环境则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`replicate_experts` 里选「下一个该复制的逻辑专家」用的是 `weight / logcnt` 的 argmax，而不是直接用 `weight` 的 argmax，为什么？

**答案**：`logcnt` 是该逻辑专家已经被复制（出现）的次数。用 `weight / logcnt` 表示「平均到每个副本的单位负载」，每次复制都选「单位负载最高的」专家。若直接用 `weight`，某个极端热门专家可能被无限制复制、占据所有冗余槽；除以 `logcnt` 后，每复制一次它的单位负载就下降，从而让其他热门专家也有机会被复制，最终各副本负载更均衡。

**练习 2**：EPLB 在线翻译 `topk_ids_logical_to_physical` 里用 `torch.randint` 随机选副本，会不会让同一个 token 两次推理得到不同结果？这与「确定性推理」冲突吗？

**答案**：对单个逻辑专家而言，它的所有物理副本是同一份权重的拷贝，算出的结果在数学上等价（数值上可能有微小 FP 差异），所以随机选副本主要影响的是「负载落在哪张卡」，不改变模型语义。它是性能优化、不是采样，不与确定性推理冲突。注意 `eplb.py` 顶部的随机源是 Python `random.Random(seed)`（用于离线 `compute_logical_to_rank_dispatch_physical_map`），而在线 `torch.randint` 是 GPU 随机，二者作用阶段不同。

---

## 5. 综合实践

把本讲四个模块串起来，跟踪一个 DeepSeek-V2 MoE 层从 hidden_states 到输出的完整链路，画成一张图并用源码行号标注：

```
hidden_states
   │
   ├─[MoEGate.forward, deepseek_v2.py:639]
   │     │
   │     ├─ router_logits = F.linear(hidden_states, gate_weight)   [L641]
   │     │     （gate 计算：门控线性层）
   │     │
   │     ├─ noaux_tc_router(router_logits, e_score_correction_bias)  [L665]
   │     │     → route.py NoauxTCRouter.forward → backends/default/moe_router.py:92
   │     │        scores=sigmoid(logits) → +bias → 分组topk → renorm
   │     │     → topk_weight, topk_idx   （top-k 专家选择）
   │     │
   │     └─ [可选] EPLBManager.topk_ids_logical_to_physical(topk_idx)  [L670]
   │           → eplb.py:294  逻辑专家 ID → 物理副本 ID
   │
   ├─ experts(hidden_states, topk_weights, topk_ids)  [deepseek_v2.py:740]
   │     │  = build_fused_moe(...) 选出的 FusedMoE / W8A8 / BlockedF8  [__init__.py:11]
   │     │
   │     └─ FusedMoEBase.forward → forward_default  [base.py:312/299]
   │           ├─ dispatch:  moe_gather_inputs / dispatch_async       [base.py:54]
   │           ├─ gemm:      self.impl.forward(融合 kernel)            [default.py:305]
   │           └─ combine:   moe_reduce / combine_async               [base.py:76]
   │
   ├─ + shared_experts(hidden_states)   [deepseek_v2.py:747]  （共享专家，所有 token 都走）
   └─ all_reduce (若 dp==1 且 world_size>1)  [deepseek_v2.py:751]
```

**任务**：

1. 按上图逐个打开链接，确认每个箭头对应的代码行。
2. 在图中标注三处「分支点」：① gate 的 `topk_method` 三分支；② `build_fused_moe` 的 `quant_method` 三分支；③ EPLB 是否启用的分支。
3. 写一段话回答：如果用户构造 `PytorchEngineConfig(ep=8, enable_eplb=True)`，这条链路上相比默认会发生哪两处变化？（提示：`MoEGate` 多了 EPLB 查表；`build_fused_moe` 的 `num_experts` 变成了物理专家数，专家按 `ep_expert_list` 分卡。）

**预期结果**：你能脱稿讲清「gate 算 top-k →（EPLB 翻译）→ FusedMoE 三段流水线 → 叠加 shared → all_reduce」整条链路，并能指出量化与 EPLB 各在哪两个分支点改变行为。

## 6. 本讲小结

- **MoE = 门控路由 + 多专家 FFN**：每个 token 经 gate 选 `top_k` 个专家，只激活这部分专家，实现「参数多、计算少」的稀疏激活。
- **融合 MoE 用 dispatch/gemm/combine 三段流水线**（`FusedMoEBase`）：`MoeType.Default` 同步串行，`DSAsync*` 用 `dispatch_async/combine_async` 让 EP 通信与 GEMM 计算重叠；DP-TP 模式还用 `MoEForwardDPTP` 分轮重叠 all-gather 与 reduce-scatter。
- **路由在 `route.py`，算法在 `backends/default/moe_router.py`**：`NoauxTCRouter` 实现 DeepSeek-V3 的 `noaux_tc`——偏置 `e_score_correction_bias` 只参与「选谁」，权重取自不带偏置的 sigmoid 得分，从而实现无辅助损失的负载均衡。
- **MoE 量化变体沿用 u5-l2 的派发模式**：`build_fused_moe` 按 `quant_method` 选 `FusedMoE / FusedMoEW8A8 / FusedMoEBlockedF8`，三者共享 `FusedMoEBase` 骨架，差异只在权重容器（`LinearWeights` / 多 `scale` / 多 `weight_scale_inv`）与 impl；FP8 支持「FP16 权重加载时在线量化」。
- **EPLB 解决 EP 的负载不均**：离线用「复制热点专家（`replicate_experts`）+ 贪心装箱（`balanced_packing`）」建逻辑→物理映射，在线在 gate 之后用 `topk_ids_logical_to_physical` 把逻辑专家 ID 随机映射到某个物理副本，使各卡 all-to-all 收发量大致相等。
- **桥接模式贯穿始终**：`nn/moe/` 所有类都通过 `OpType`（`FusedMoE / FusedMoEBlockedF8 / RouterNoauxTC / SoftmaxTopK`）从 `backends` 取 impl，与 u5-l1/u5-l2 的普通 nn 积木是同一套工程范式。

## 7. 下一步学习建议

- **u5-l4（算子后端分发 backends）**：本讲反复出现的 `OpType.FusedMoE / FusedMoEBlockedF8 / RouterNoauxTC` 是怎么在 `backends/selector.py` 里按「设备 × 量化」分发的？去 `backends/cuda/moe.py`、`backends/default/moe_router.py` 看 impl 真身。
- **u5-l5（Triton/CUDA Kernel）**：融合 MoE 的 GEMM 真正落在哪个 kernel？关注 `kernels/` 下与 MoE 相关的 Triton 实现。
- **u9-l4（张量并行与分布式）**：本讲的 EP/TP/DP、`moe_tp_group`、`gather_group` 来自 `distributed.py` 与 `DistConfig`，那里讲清了进程组拓扑如何建起。
- **u9-l5（PD 分离）**：DeepSeek 异步 MoE（`DSAsync*`）依赖的 DeepEP 通信库，与 PD 分离的 KV 传输共享同一类 all-to-all 基础设施，可对照阅读 `pytorch/disagg/`。
- **延伸阅读**：若想理解 `noaux_tc` 与 `group_limited_greedy` 的设计动机，建议读 DeepSeek-V2 / V3 论文中关于 auxiliary-loss-free load balancing 的章节，再回看 `backends/default/moe_router.py` 会豁然开朗。
