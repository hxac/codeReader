# 混合专家（MoE）：路由、专家与负载均衡

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清一个 MoE 层的完整数据流：router 打分 → top-k 选专家 → permute 重排 → 专家计算 → unpermute 加权合并。
2. 解释 `MoERouter` 的路由决策细节：gating 函数、top-k 选取、`bias_gamma` 无辅助损失的负载均衡、路由指标收集。
3. 写出负载均衡损失（load balancing loss）与 router Z-loss 的公式，并说明「负载均衡损失 ≈ 1 代表均衡、趋近 num_experts/top_k 代表坍缩」这一数值含义。
4. 对比容量型 `MoE`（会丢 token）与 `DroplessMoE`（不丢 token）两种实现的取舍。
5. 用 `TransformerConfig` 的 MoE 工厂方法（`smallmoe`、`llama_like_moe`、`olmoe_1B_7B`）把一个 dense 小模型改造成 MoE 版本。

## 2. 前置知识

**稀疏激活的直觉。** u3-l4 里我们见过 dense 前馈层 `FeedForward`：每个 token 都流过同一组 SwiGLU 权重，公式为

\[
y = W_2\big(\mathrm{SiLU}(W_1 x) \odot W_3 x\big)
\]

参数量约 \(3 d h\)（\(d\) 是 `d_model`，\(h\) 是 `hidden_size`）。MoE（Mixture of Experts，混合专家）的想法是：**与其让每个 token 都流过同一个 MLP，不如准备 \(N\) 个结构相同的"专家" MLP，再由一个轻量 router 为每个 token 只挑选其中 \(K\) 个**（\(K \ll N\)）。这样总参数量可以做到很大（容量），而每个 token 的实际计算量（FLOPs）只相当于 \(K\) 个专家（稀疏激活）。OLMoE、DeepSeek、Mixtral 等模型都采用这一结构。

**几个关键术语：**

- **专家（expert）**：一个普通的 SwiGLU MLP，本项目中是 `MoEMLP` / `DroplessMoEMLP` 里的批量权重 `w1/w2/w3`（每个专家各一份）。
- **router（网关/路由器）**：一个线性层 + softmax，给每个 token 对 \(N\) 个专家打分，输出 shape 为 `(B, S, num_experts)`。
- **top-k 路由**：每个 token 只取分数最高的 \(K\) 个专家，输出加权和作为该 token 的 MoE 输出。
- **共享专家（shared expert）**：一个所有 token 都必经的 dense MLP，与路由分支并联，见 `MoEConfig.shared_mlp`。
- **容量（capacity）与丢 token**：容量型 MoE 给每个专家分配固定大小的缓冲区，超出容量的 token 会被「丢掉」（输出置零）；dropless MoE 则动态按需分组，不丢 token。
- **负载均衡损失（load balancing loss）**：router 若学会「把所有 token 都塞给少数专家」，其余专家就白养了。这个辅助损失惩罚不均衡的路由分布。
- **辅助损失的注入**：MoE 的辅助损失并不加进语言模型损失里，而是通过一个自定义 autograd 函数「挂」在被路由的张量上，反向传播时自动生效——这是本讲最巧妙的一个源码点。

**与上一讲的衔接**：MoE 层在 `TransformerBlock` 里占的正是 u3-l4 中 `FeedForward` 的位置；`MoETransformerBlock` 只是把 `feed_forward` 换成 `feed_forward_moe`，注意力分支完全不变。建议先回顾 u3-l4 的 SwiGLU 与 RMSNorm 再读本讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/olmo_core/nn/moe/moe.py` | MoE 层本体：`MoEConfig`（配置）、`MoEBase`（组装 router + experts + shared MLP）、`MoE`（容量型）与 `DroplessMoE` 两种实现 |
| `src/olmo_core/nn/moe/router.py` | `MoERouterConfig` 与 `MoERouter` / `MoELinearRouter`：路由打分、top-k 选取、辅助损失计算与指标收集 |
| `src/olmo_core/nn/moe/mlp.py` | 专家本体：`MoEMLP`（批量 `torch.bmm`）与 `DroplessMoEMLP`（grouped GEMM），以及专家并行分片 `apply_ep` |
| `src/olmo_core/nn/moe/parallel_mlp.py` | `ParallelMLP` / `ParallelDroplessMLP`：token 重排（permute）→ 专家计算 → 还原（unpermute）的编排层，含专家并行的 all-to-all |
| `src/olmo_core/nn/moe/loss.py` | `load_balancing_loss` 与 `router_z_loss` 两个损失函数，以及损失粒度枚举 |
| `src/olmo_core/ops/__init__.py` | `attach_auxiliary_loss`：把辅助损失挂进计算图的 autograd 技巧 |
| `src/olmo_core/nn/transformer/config.py` | `TransformerConfig` 的 MoE 侧入口：`smallmoe` / `small_hybrid_moe` / `olmoe_1B_7B` / `llama_like_moe` 工厂方法 |
| `src/olmo_core/nn/transformer/block.py` | `MoETransformerBlock`：把 dense block 的前馈位换成 MoE |
| `src/olmo_core/nn/transformer/model.py` | `MoETransformer`：逐层汇聚 MoE 指标、应用专家并行 |
| `src/examples/moe/train.py` | 官方 MoE 训练示例（流水线并行 + FSDP） |
| `src/test/nn/moe/moe_test.py` | MoE 单元测试：前向、指标、专家并行一致性 |

## 4. 核心概念与源码讲解

### 4.1 MoE 层总览：MoEConfig 与 MoEBase

#### 4.1.1 概念说明

`MoEBase` 是 MoE 层的组装车间，它把三样东西拼在一起：

1. **router**：决定每个 token 去哪儿（4.2 节）；
2. **experts**：真正干活的 \(N\) 个专家 MLP，由 `ParallelMLPBase` 子类包装，负责 token 的搬运与计算（4.3 节）；
3. **shared_mlp（可选）**：人人有份的共享专家，与路由分支并联。

配置入口是 `MoEConfig`。它有两个实现，由 `name` 字段（`MoEType` 枚举）切换：

- `MoEType.default` → `MoE`：容量型，每个专家固定容量缓冲区，溢出 token 被丢弃；
- `MoEType.dropless` → `DroplessMoE`：按实际路由数量动态分组，不丢 token，用 grouped GEMM 计算。

#### 4.1.2 核心流程

`MoEBase.forward(x)` 的流程（`x` 的 shape 为 `(*, d_model)`）：

```
x ──► router(x) ──► expert_weights (B,S,top_k)
│                  ├► expert_indices (B,S,top_k)
│                  ├► batch_size_per_expert (num_experts,)
│                  └► router_aux_loss（可选，LB loss + Z loss）
│
├──► (可选) latent_down_proj：降到低维潜空间
├──► attach_auxiliary_loss：把 aux loss 挂进计算图
│
├──► shared_mlp(x)（可选共享专家）
│
└──► experts(x, weights, indices, counts)   # 重排 + 专家计算 + 还原
        │
        └──► (可选) latent_up_proj：升回 d_model
        │
        └──► 与 shared_out 按 top_k/(top_k+1) 加权合并
```

参数量记账也内置在配置里，无需实例化即可预算：

- 总参数：router 的 \(d \cdot N\) + 专家的 \(3 \cdot d \cdot h \cdot N\) + 共享专家；
- **活跃参数**（`num_active_params`）：把「全部 \(N\) 个专家」换成「每个 token 实际用到的 \(K\) 个」，即总参数 \(-3dhN + 3dhK\)。这是 MoE 论文里「总参数 7B、每个 token 激活 1B」这类说法的来源。

#### 4.1.3 源码精读

**MoEConfig 的字段**（[src/olmo_core/nn/moe/moe.py:L92-L110](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L92-L110)）：`num_experts`（专家数 \(N\)，默认 1）、`hidden_size`（每个专家的 FFN 隐层宽度 \(h\)，默认 256）、`capacity_factor`（容量系数，仅容量型用）、`router`（嵌套的 `MoERouterConfig`）、`shared_mlp`（可选共享专家）、`lb_loss_weight`（LB 损失权重，默认 0.01）、`z_loss_weight`（Z 损失权重）、`dtype`。注意这又是一个「Config 套 Config」的嵌套结构（第 2 单元模式的复现）。

**参数量/活跃参数量**（[src/olmo_core/nn/moe/moe.py:L112-L129](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L112-L129)）：`num_params` 累加 router、专家（\(3 \times \text{latent\_dim} \times h \times N\)）与共享专家；`num_active_params` 把专家那一项的 \(N\) 换成 `router.top_k`。

**build 分发**（[src/olmo_core/nn/moe/moe.py:L131-L164](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L131-L164)）：与 u3-l4 的组件族一致，`name` 枚举 + `build()` 分发到 `MoE` 或 `DroplessMoE`，传错字段会命中 `except TypeError` 被转成友好的 `OLMoConfigurationError`。

**构造时的损失按层缩放**（[src/olmo_core/nn/moe/moe.py:L192-L196](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L192-L196)）：`scale_loss_by_num_layers=True`（默认）时，`lb_loss_weight` 与 `z_loss_weight` 都除以 `n_layers`。这样无论多少层，**全模型**的辅助损失总权重约等于配置值，而不是随深度翻倍。

**组装三件套**（[src/olmo_core/nn/moe/moe.py:L227-L249](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L227-L249)）：先 `router.build(...)` 传入损失权重与粒度，再由 `_init_parallel_mlp`（抽象方法，由子类实现）构建专家，最后构建可选的 `shared_mlp`。

**forward 主链**（[src/olmo_core/nn/moe/moe.py:L294-L327](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L294-L327)）：路由（L308-L310）→ 若有 aux loss 则 `attach_auxiliary_loss(routed_x, router_aux_loss)`（L313-L314）→ 专家计算（L320）→ 共享分支合并。共享专家的合并不是简单相加：`shared_out / (top_k + 1)` 加上 `out * top_k / (top_k + 1)`（L323-L325），即共享专家占 \(1/(K{+}1)\) 的权重、路由分支占 \(K/(K{+}1)\)，避免分支数量影响输出量级。

**两种实现只差 `_init_parallel_mlp`**：`MoE` 构造 `ParallelMLP`（多一个 `capacity_factor` 参数，[src/olmo_core/nn/moe/moe.py:L489-L511](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L489-L511)）；`DroplessMoE` 构造 `ParallelDroplessMLP`（[src/olmo_core/nn/moe/moe.py:L514-L539](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L514-L539)）。

#### 4.1.4 代码实践

**实践目标**：不实例化模型，仅凭配置预算一个小 MoE 的总参数与活跃参数，感受「容量大、激活少」。

**操作步骤**（CPU 即可，纯配置运算）：

```python
# 示例代码：保存为 moe_params_probe.py，在仓库根目录运行 python moe_params_probe.py
from olmo_core.nn.transformer import TransformerConfig

cfg = TransformerConfig.smallmoe(vocab_size=50304)  # d_model=768, 12 层, 32 专家 top-4
print(f"整模型总参数量: {cfg.num_params:,}")

moe = cfg.block.feed_forward_moe   # 每个 block 的 MoE 配置
total = moe.num_params(768)
active = moe.num_active_params(768)
print(f"单个 MoE 层总参数:   {total:,}")
print(f"单个 MoE 层活跃参数: {active:,}")
print(f"激活比例: {active / total:.2%}")   # 预期约等于 (top_k 相关的缩减)
print(f"专家数 N={moe.num_experts}, top_k={moe.router.top_k}, "
      f"专家隐宽 h={moe.hidden_size}, 共享专家={'有' if moe.shared_mlp else '无'}")
```

**需要观察的现象**：总参数与活跃参数的差距来自 \(3dh(N - K)\) 这一项；对 `smallmoe`（\(N=32, K=4\)）专家部分的激活比例约为 \(4/32 = 12.5\%\)，加上 router 与共享专家后整体比例会略高。

**预期结果**：脚本打印出约几千万级的 `num_params`，`active/total` 明显小于 1。（具体数值待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`MoEConfig.num_active_params` 为什么不减去 router 的参数？
**答案**：router 是一个 \(d \times N\) 的线性层，**每个 token 都要过**，属于稠密激活部分；「活跃/非活跃」的区分只针对 \(N\) 个专家中每个 token 实际只经过 \(K\) 个这一事实。共享专家同理也是全激活。

**练习 2**：把 `lb_loss_weight=0.01` 的 12 层模型改成 48 层（其他不变），每层的实际 LB 损失权重是多少？
**答案**：`scale_loss_by_num_layers` 默认为 True，构造时权重除以 `n_layers`，所以每层分别是 `0.01/12 ≈ 8.3e-4` 与 `0.01/48 ≈ 2.1e-4`，全模型合计都近似 0.01。这正是该开关的设计目的。

**练习 3**：`shared_mlp` 为什么默认输出要除以 `top_k + 1`？
**答案**：路由分支有 \(K\) 份加权输出叠加，量级约与 \(K\) 成正比。合并时给共享专家 \(1/(K{+}1)\)、路由分支整体 \(K/(K{+}1)\) 的份额，使输出量级对 \(K\) 不敏感，切换 top-k 时不需要重调下游尺度。

### 4.2 Router：把每个 token 交给哪些专家

#### 4.2.1 概念说明

router 是 MoE 的「调度台」，本质极简：一个线性层把 \(x \in \mathbb{R}^{d}\) 映射为 \(N\) 个专家的 logits，再经 softmax（或 sigmoid）得到分数，取 top-\(K\)。但训练好一个 router 并不容易，源码里围绕这个主干做了一大堆工程处理：

- **输入抖动 `jitter_eps`**：训练时给输入乘上 \([1-\epsilon, 1+\epsilon]\) 的均匀噪声，防止 router 过度自信；
- **门控函数 `gating_function`**：`softmax`（默认，分数天然归一）或 `sigmoid`（独立打分，需加 \(10^{-7}\) 防零）；
- **`bias_gamma`**：DeepSeek-v3 式「无辅助损失负载均衡」——不改损失函数，而是给每个专家的选路分数加一个可自适应的偏置，每个 batch 结束后按负载增减；
- **指标收集**：实时累计每个专家收到的 token 数、LB 损失、Z 损失，供训练日志观察路由健康度。

基类 `MoERouter` 只实现通用逻辑，具体的「打分函数」留给子类；当前唯一实现是线性 router `MoELinearRouter`。

#### 4.2.2 核心流程

`MoERouter.forward(x)`（`x` shape 为 `(B, S, d_model)`）：

```
x
 ├─ jitter(x)                       # 训练时加乘性噪声
 ├─ logits = get_expert_logits(x)   # 线性投影, (B, S, N)，float32
 ├─ scores = softmax(logits)        # 或 sigmoid + 1e-7
 ├─ (可选) 随机化 scores             # random_expert_assignment，仅供吞吐基准测试
 ├─ get_top_k(scores)               # → expert_weights (B,S,K), expert_indices (B,S,K)
 ├─ (可选) 按 p-范数归一化 expert_weights
 ├─ batched_histc(expert_indices)   # 统计每专家 token 数 c_i, (N,)
 └─ 若 training 且 grad 开启:
     ├─ lb_loss = load_balancing_loss(...)   # 4.4 节
     ├─ z_loss  = router_z_loss(...)         # 4.4 节
     ├─ 累计进指标累加器（对训练日志可见）
     └─ aux_loss = w_lb·lb_loss + w_z·z_loss
 返回 (expert_weights, expert_indices, batch_size_per_expert, aux_loss)
```

`bias_gamma` 分支的 top-k 稍有不同：**选专家**时用 `scores + score_bias`（no grad），但**加权**仍用原始 `scores`；每个 batch 结束后 `post_batch()` 把偏置朝「欠载专家 +γ、超载专家 −γ」方向推一步，形成无梯度的负载均衡反馈环。

#### 4.2.3 源码精读

**MoERouterConfig 字段**（[src/olmo_core/nn/moe/router.py:L80-L97](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/router.py#L80-L97)）：`top_k`（默认 1）、`jitter_eps`、`normalize_expert_weights`（p-范数，如 `2.0`）、`uniform_expert_assignment` / `random_expert_assignment`（基准测试用）、`bias_gamma`（DeepSeek-v3 式偏置均衡，推荐量级 0.0001）、`gating_function`。

**top-k 选取**（[src/olmo_core/nn/moe/router.py:L345-L365](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/router.py#L345-L365)）：`top_k == 1` 时直接 `max`（更快），否则 `torch.topk`；`bias_gamma` 分支在 no-grad 下对 `scores + score_bias` 取 top-k、再用 `gather` 取回真实分数作为权重。

**forward 主干**（[src/olmo_core/nn/moe/router.py:L422-L527](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/router.py#L422-L527)）：打分与 softmax 在 L440-L448（注意 logits 先 `.float()`，数值稳定优先）；top-k 在 L456；token 直方图统计在 L468-L475，用的是纯 PyTorch 的 `scatter_add_` 实现（[src/olmo_core/ops/moe.py:L482-L489](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/ops/moe.py#L482-L489)）；辅助损失计算与指标累计在 L479-L525——只在 `self.training and torch.is_grad_enabled()` 时进行，所以**推理/eval 模式零损失开销**。

**bias_gamma 的 batch 间更新**（[src/olmo_core/nn/moe/router.py:L309-L334](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/router.py#L309-L334)）：`bias_delta = γ · sign(理想均分 − 实际)`，欠载专家偏置上调、超载下调，随后清零累加器。这是「用调度反馈替代辅助损失」的另一条均衡路线。

**MoELinearRouter**（[src/olmo_core/nn/moe/router.py:L546-L587](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/router.py#L546-L587)）：权重存成一维的 `(num_experts * d_model,)`（L562-L564）——注释解释了原因：FSDP 按第一维分片，参数的第一维必须足够大；初始化用 `trunc_normal_(std=0.02)`（L571-L573）；`get_expert_logits` 里 forward 时再 `view(N, d)` 并全程 float32（L578-L581）。

**指标接口**（[src/olmo_core/nn/moe/router.py:L376-L412](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/router.py#L376-L412)）：`compute_metrics()` 返回三组指标——`load imbalance`（每专家 token 数的 max/mean，**等于 1 即完美均衡**）、`load balancing loss`（加权与未加权两个版本）、`router Z loss`。这些指标经 `MoETransformer.compute_auxiliary_metrics`（[src/olmo_core/nn/transformer/model.py:L1435-L1474](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1435-L1474)）逐层汇聚后进入训练日志。

#### 4.2.4 代码实践

**实践目标**：在 CPU 上单独驱动 router，亲眼看到「每专家 token 分配数」和负载不均衡指标。

**操作步骤**：router 是纯 PyTorch（线性层 + softmax + topk + scatter_add），无需 GPU：

```python
# 示例代码：保存为 router_probe.py 运行
import torch
from olmo_core.nn.moe import MoERouterConfig
from olmo_core.utils import seed_all

seed_all(42)
router = MoERouterConfig(top_k=2).build(
    d_model=128, num_experts=8, lb_loss_weight=0.01
)
router.train()  # 指标与 aux loss 只在训练模式累计

x = torch.randn(4, 32, 128)          # B=4, S=32, d=128
weights, indices, counts, aux = router(x)

print("expert_indices[0, 0]:", indices[0, 0].tolist())   # 第 1 个 token 选中的 2 个专家
print("每专家分配数 c_i:", counts.tolist())
print("总分配数（应等于 B*S*top_k = 256）:", counts.sum().item())
print("load imbalance（max/mean）:", (counts.max() / counts.float().mean()).item())
print("aux loss:", None if aux is None else aux.item())

metrics = router.compute_metrics()   # 读取并清零指标累加器
for name, (val, _) in metrics.items():
    print(f"{name}: {val.item():.4f}")
```

**需要观察的现象**：随机初始化的 router 分配并不均匀——某些专家可能收到远超均值 32 的 token（`counts.mean() = 256/8 = 32`），`load imbalance` 明显大于 1；`aux loss` 非零。

**预期结果**：`counts.sum() == 256`；`load imbalance > 1`（典型 1.5～3）。随机种子不同数值不同，属正常现象。（具体数值待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gating_function=sigmoid` 时要先 `F.sigmoid(logits) + 1e-7`，计算 LB 损失前还要再除以 `scores.sum(dim=-1)`？
**答案**：sigmoid 输出可能为 0，加 \(10^{-7}\) 防止后续取对数/归一化出 NaN；softmax 天然每行和为 1，而 sigmoid 每行和任意，若不归一化，LB 损失的「分数和」假设不成立（源码 router.py L484-L486 的注释明确要求「scores 必须归一，否则 LB loss 效果不好」）。

**练习 2**：`uniform_expert_assignment` 和 `random_expert_assignment` 都能打散路由，区别是什么？
**答案**：`uniform`（router.py L361-L363）用自定义 autograd 函数把专家索引直接改成 `token_id % num_experts` 的轮转分配，完全绕过学习到的分数；`random`（L450-L453）用 `scores * 0 + torch.rand_like(scores)` 保留计算图但把分数替换为随机数。两者都用于吞吐基准测试：前者保证完美均衡，后者保留与真实路径相同的 autograd 开销。

**练习 3**：`MoELinearRouter` 为什么不支持 bias？
**答案**：权重被压平成一维 `(N*d,)` 以便 FSDP 沿第一维分片（第一维必须够大），bias 没有「大第一维」可言，分片后难以高效管理，索性不支持（router.py L559-L562 的注释）。

### 4.3 专家计算：从 permute 到 grouped GEMM

#### 4.3.1 概念说明

router 只给出「谁去哪儿」，真正的问题是**怎么高效地算**。GPU 上的批量矩阵乘要求规整的形状，而 top-k 路由的分组是参差的：专家 0 可能分到 100 个 token，专家 1 只有 3 个。解决思路是经典的 **permute → compute → unpermute** 三段式：

1. **permute**：按专家 ID 排序 token，把同一专家的 token 挪到一起（`torch.sort` + gather）；
2. **compute**：对每组连续的 token 做普通 SwiGLU MLP（批量 `bmm` 或 grouped GEMM）；
3. **unpermute**：把结果按 `expert_weights` 加权散回原位置，同一 token 的 \(K\) 份结果求和。

两种实现对「规整」的定义不同，这正是它们的分水岭：

| | `MoE`（容量型） | `DroplessMoE` |
| --- | --- | --- |
| 分组方式 | 每专家固定容量 `capacity` 的缓冲区 | 按实际每专家数量，无 padding |
| 超 capacitiy 的 token | **被丢弃**（scatter 回写 0） | 不存在丢弃 |
| 计算内核 | `torch.bmm`（形状规整 `(N, cap, d)`） | grouped GEMM（按组大小分块） |
| 依赖 | 无额外依赖 | 建议安装 `grouped_gemm`，否则退化为 Python 循环慢路径 |
| 适用 | 早期 MoE（Switch/GRIN 风格）、简单可靠 | 现代主流（OLMoE、Mixtral 风格），无信息丢失 |

#### 4.3.2 核心流程

容量（仅容量型）的计算：

\[
\text{capacity} = \text{ep} \cdot \mathrm{mult}_8\!\left(\left\lfloor c_f \cdot \frac{K \cdot T_{\text{loc}}}{N} \right\rfloor\right)
\]

其中 \(c_f\) 是 `capacity_factor`（默认 1.2），\(T_{\text{loc}}\) 是本卡 token 数，\(\mathrm{mult}_8\) 表示向上取 8 的倍数（内核对齐要求），\(\text{ep}\) 是专家并行世界大小。理想情况下每专家分到 \(K T/N\) 个 token，容量系数留出余量；但仍可能溢出——溢出即丢弃。

专家本体的 SwiGLU 与 u3-l4 的 dense 版完全同构，只是升了一个「专家批量维」：

\[
Y_e = W_2^{(e)}\big(\mathrm{SiLU}(W_1^{(e)} X_e) \odot W_3^{(e)} X_e\big), \quad e = 1, \dots, N
\]

#### 4.3.3 源码精读

**permute 的索引构造**（[src/olmo_core/nn/moe/parallel_mlp.py:L102-L125](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L102-L125)）：`indices_and_bins` 对展平后的 `(B·S·K,)` 专家 ID 做 `torch.sort`——排序值 `bin_ids` 告诉我们每个槽位属于哪个专家，排序索引 `indices` 就是 gather/scatter 用的置换表；`bins` 是各专家段的前缀和边界。

**编排层 forward**（[src/olmo_core/nn/moe/parallel_mlp.py:L127-L183](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L127-L183)）：展平 → 构造置换索引 → 未开专家并行走 `forward_once`，开了则走 `parallel_forward_once`（本地 permute → all-to-all 跨卡交换 → 计算 → 反向 all-to-all → unpermute，见 L206-L285 的注释，步骤说明非常清晰）。

**容量型三段式**（[src/olmo_core/nn/moe/parallel_mlp.py:L608-L631](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L608-L631)）：`binned_gather`（按 bins 装箱，padding 到 capacity）→ `self.mlp(x)` → `binned_scatter`（加权散回，**溢出槽位被丢弃**）。容量公式在 [src/olmo_core/nn/moe/parallel_mlp.py:L388-L407](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L388-L407)。

**容量型专家本体 `MoEMLP`**（[src/olmo_core/nn/moe/mlp.py:L123-L195](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/mlp.py#L123-L195)）：\(W_1, W_3\) 形状 `(N·d, h)`、\(W_2\) 形状 `(N·h, d)`，前两维压平同样是为了 FSDP 分片（L138-L139 注释）；forward 把权重 `view` 回 `(N, d, h)` 后一条 `torch.bmm(F.silu(torch.bmm(x, w1)) * torch.bmm(x, w3), w2)` 完成全部专家计算（L195）。初始化是 `kaiming_uniform_(a=√5)`，与 `nn.Linear` 默认一致（L166-L171）。

**dropless 专家本体 `DroplessMoEMLP`**（[src/olmo_core/nn/moe/mlp.py:L198-L292](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/mlp.py#L198-L292)）：`gmm` 方法优先调用 `grouped_gemm`（仅支持 BF16，L256-L262），未安装则退化为逐专家切片 matmul 的 Python 循环并发出警告（L241-L246, L264-L270）；forward 即三次 grouped GEMM：`gmm(x, w1) → silu ⊙ gmm(x, w3) → gmm(·, w2)`（L289-L292）。

**dropless 三段式**（[src/olmo_core/nn/moe/parallel_mlp.py:L851-L871](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L851-L871)）：与容量型同构，但 gather/scatter 按**真实**每专家数量 `batch_size_per_expert` 进行（这就是「dropless」的全部含义），跨卡版本通过带 `send_counts/recv_counts` 的 all-to-all 交换，文档警告其中有一次 host-device 同步（L638-L640）。

**FLOPs 估算**（[src/olmo_core/nn/moe/parallel_mlp.py:L335-L347](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L335-L347)）：`6 * 专家参数总量 * top_k / num_experts`——每个 token 平均「触碰」\(K/N\) 比例的专家参数。

**重要运行前提**：`binned_gather/binned_scatter/gather/scatter` 都断言自定义 CUDA kernels 可用（[src/olmo_core/ops/moe.py:L6-L13](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/ops/moe.py#L6-L13) 中 `kernels = None` 时 [L37](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/ops/moe.py#L37) 会 `assert kernels is not None`）。所以 **MoE 层的完整前向需要编译好的 CUDA 扩展和 GPU**——这也解释了为什么 `test_moe` 标记了 `@requires_gpu`。

#### 4.3.4 代码实践

**实践目标**：在 CPU 上用纯 PyTorch 复现 `indices_and_bins` 的置换逻辑，吃透 permute 的本质。

**操作步骤**：

```python
# 示例代码：保存为 permute_probe.py 运行
import torch

torch.manual_seed(0)
N, T, K, d = 4, 10, 2, 3

# 模拟 router 输出：每个 token 的 K 个专家 ID
expert_indices = torch.randint(0, N, (T * K,))
print("排序前的专家 ID:", expert_indices.tolist())

bin_ids, indices = torch.sort(expert_indices)     # parallel_mlp.py L118 的核心一步
counts = torch.bincount(expert_indices, minlength=N)
bins = torch.cumsum(counts, 0)                    # 各专家段的右边界
print("置换表 indices:", indices.tolist())
print("排序后分组:", bin_ids.tolist())
print("每专家数量:", counts.tolist(), " 段边界 bins:", bins.tolist())

# 模拟 permute：同一专家的 token 挪到一起
x = torch.randn(T * K, d)
permuted = x[indices]
# 按段切给专家（这里仅打印形状）
for e in range(N):
    lo = 0 if e == 0 else bins[e - 1]
    print(f"专家 {e} 收到 {permuted[lo:bins[e]].shape[0]} 个 token")
```

**需要观察的现象**：`indices` 把属于同一专家的行聚到一起；`bins` 是单调递增的段边界；各段长度之和等于 `T*K`。

**预期结果**：打印的分组与 `counts` 一致。可再对照 [src/olmo_core/nn/moe/parallel_mlp.py:L102-L125](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L102-L125) 逐行验证。

#### 4.3.5 小练习与答案

**练习 1**：容量型 MoE 中 `capacity_factor` 调大到 2.0 会怎样？
**答案**：每专家缓冲区翻倍，丢弃的 token 减少（训练信号更完整），但 padding 浪费的计算与显存同步增加——`torch.bmm` 会对缓冲区里的 padding 位置照算不误。这是一个精度/算力的权衡旋钮。

**练习 2**：为什么 `DroplessMoEMLP` 的权重形状是 `(N·h, d)` 而 `MoEMLP` 的 `w1/w3` 是 `(N·d, h)`？
**答案**：容量型用 `bmm`，需要 `(N, d, h)` 与 `(N, cap, d)` 直接相乘，`w1` view 后是 `(N, d, h)`；dropless 用 grouped GEMM 且 `trans_b=True`（[src/olmo_core/nn/moe/mlp.py:L289-L290](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/mlp.py#L289-L290)），权重按 `(N, h, d)` 存放、转置后参与分组乘法。两者都是为了配合各自内核的内存布局。

**练习 3**：unpermute 时对同一 token 的 \(K\) 份结果是怎么合并的？
**答案**：`binned_scatter`/`scatter` 在散回时把每份结果乘上对应的 `expert_weights`（router 给的 top-k 分数）再累加到原位置，等价于 \(\sum_{k=1}^{K} g_k \cdot y_{e_k}(x)\)，其中 \(g_k\) 是第 \(k\) 个专家的门控分数。

### 4.4 负载均衡损失与 Router Z-loss

#### 4.4.1 概念说明

**为什么需要负载均衡损失？** 如果不加约束，router 会陷入「赢家通吃」：少数专家分到绝大多数 token，其余专家得不到梯度、无法变强，进一步加剧集中——最终模型退化成一个小得多的 dense 模型，白付了 \(N\) 个专家的参数与显存。LB 损失用一个可微的代理指标惩罚这种集中。

**Router Z-loss** 是另一个正则项：惩罚 router logits 的 \(\log\sum\exp\) 的平方，把 logits 的量级往小压，稳定路由数值（softmax 对大 logits 极敏感，微小扰动会翻转 top-k 选择）。

**辅助损失怎么进梯度？** MoE 的 `forward` 返回的是**激活值**而非损失，语言模型的交叉熵损失里并不包含 LB/Z 损失。`attach_auxiliary_loss` 用一个自定义 autograd 函数解决：forward 时原样返回激活，backward 时给 `aux_loss` 这个输入回传梯度 1.0——数学上完全等价于把 `aux_loss` 加进了总损失，但不需要改任何上层训练代码。

#### 4.4.2 核心流程

以默认的 `local_batch` 粒度为例。设 \(N\) 为专家数、\(K\) 为 top_k、\(T = B \cdot S\) 为 token 总数、\(c_i\) 为分给专家 \(i\) 的（token, 槽位）数（\(\sum_i c_i = T K\)）、\(\bar{s}_i = \frac{1}{T}\sum_t s_{t,i}\) 为专家 \(i\) 的平均路由分数。则

\[
\mathcal{L}_{\mathrm{lb}} = \frac{N}{K} \cdot \frac{1}{T} \sum_{i=1}^{N} c_i \, \bar{s}_i
\]

**数值含义**（这就是训练日志里该指标的读法）：

- **完美均衡**（\(c_i = TK/N\)）时，\(\mathcal{L}_{\mathrm{lb}} = \frac{N}{K} \cdot \frac{1}{T} \cdot \frac{TK}{N} \sum_i \bar{s}_i = \sum_i \bar{s}_i = 1\)（softmax 每行和为 1，均值亦然）。**指标 = 1 就是理想值**；
- **完全坍缩**（所有 token 的 top-k 永远落在同一批 \(K\) 个专家上，且分数集中）时，\(\mathcal{L}_{\mathrm{lb}} \approx \frac{N}{K} \cdot \frac{1}{T} \cdot T \cdot 1 \cdot \frac{K}{\text{...}}\) 的极端情形趋近 \(\frac{N}{K}\)。例如 \(N=8, K=2\) 时指标逼近 4；
- 所以 **日志里 `load balancing loss unscaled` 从 1 到 \(N/K\) 的区间直接量化路由健康度**，配套的 `load imbalance`（max/mean）是同一件事的非可微版本。

Z-loss 公式（\(z_{t,i}\) 为专家 logits）：

\[
\mathcal{L}_z = \frac{1}{T} \sum_{t=1}^{T} \Big( \log \sum_{i=1}^{N} e^{z_{t,i}} \Big)^2
\]

两种粒度 `MoELoadBalancingLossGranularity`：

- `local_batch`（默认）：在**本卡 batch** 上计算，忽略并行切分，最小化任何并行策略下的丢 token；
- `instance`：逐序列实例计算，会按 TP/CP 进程组做 all-reduce 归一，适合更严格的全局均衡。

#### 4.4.3 源码精读

**粒度枚举与文档**（[src/olmo_core/nn/moe/loss.py:L11-L26](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/loss.py#L11-L26)）。

**load_balancing_loss 的 local_batch 分支**（[src/olmo_core/nn/moe/loss.py:L83-L105](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/loss.py#L83-L105)）：`loss_div_factor` 默认取 \(B \cdot S\)（开 TP 时乘以 TP 世界大小，L89-L92）；分数在 token 维求均值得 \(\bar{s}\)（L99-L101）；核心一行 `torch.dot(batch_size_per_expert, expert_scores) / loss_div_factor`（L103）；最后乘 \(\frac{N}{K}\) 缩放（L109-L111）。`instance` 分支在 L50-L82，逐序列求均值并处理 TP/CP 归约，细节留作延伸阅读。

**router_z_loss**（[src/olmo_core/nn/moe/loss.py:L114-L138](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/loss.py#L114-L138)）：一行 `torch.logsumexp(expert_logits, dim=-1).square().sum() / loss_div_factor`（L134）即上式。

**辅助损失注入**（[src/olmo_core/ops/__init__.py:L4-L28](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/ops/__init__.py#L4-L28)）：`AutoAuxiliaryLoss.forward` 原样返回 `activation`（L10-L12）；`backward` 对 `aux_loss` 梯度输入回传 `torch.ones_like(aux_loss)`（L15-L18）——即在反传图中等效于 \(\partial \mathcal{L}_{\text{total}} / \partial \mathcal{L}_{\text{aux}} = 1\)，恰好等于「把 aux loss 加进总损失」的求导效果。调用点在 [src/olmo_core/nn/moe/moe.py:L313-L314](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L313-L314)。

**指标累计与读取**（[src/olmo_core/nn/moe/router.py:L479-L525](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/router.py#L479-L525)）：损失张量 `detach()` 后累进隐藏张量（`hide_from_torch` 是为了不让 FSDP/torch.compile 看到这些非参数状态，L215-L217 注释）；训练日志最终读到的 `load balancing loss` 已乘上 `lb_loss_weight / n_layers`（L392-L401）。

**测试佐证**（[src/test/nn/moe/moe_test.py:L40-L88](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/moe/moe_test.py#L40-L88)）：`test_moe` 前向后断言 `load balancing loss`、`router Z loss` 有限，并对 `output.sum()` 反传——间接验证了辅助损失的 autograd 路径通畅；随后的专家并行测试（L290-L368）进一步断言**多卡 EP 下的损失与单卡一致**。

#### 4.4.4 代码实践

**实践目标**：在 CPU 上直接调用 `load_balancing_loss`，用手工构造的「完美均衡」与「完全坍缩」两组数据验证公式的数值含义。

**操作步骤**：

```python
# 示例代码：保存为 lb_loss_probe.py 运行
import torch
from olmo_core.nn.moe.loss import (
    MoELoadBalancingLossGranularity, load_balancing_loss,
)

N, K, B, S = 8, 2, 1, 64          # 8 专家 top-2, 64 个 token
T = B * S
gran = MoELoadBalancingLossGranularity.local_batch

def make_inputs(counts_per_expert, scores_uniform=True):
    # batch_size_per_expert: 每专家分到的 token 数, 和应为 T*K
    c = torch.tensor(counts_per_expert, dtype=torch.float32)
    # batched: (B, N), 这里 B=1
    batched = c.unsqueeze(0)
    # expert_scores: (B, S, N)，均衡时每行均匀 1/N
    if scores_uniform:
        scores = torch.full((B, S, N), 1.0 / N)
    return scores, c, batched

# 场景 A: 完美均衡 —— 每专家恰好 T*K/N = 16
scores, c, batched = make_inputs([T * K // N] * N)
loss_balanced = load_balancing_loss(
    num_experts=N, top_k=K, expert_scores=scores,
    batch_size_per_expert=c, batched_batch_size_per_expert=batched,
    granularity=gran,
)
print(f"完美均衡的 LB loss = {loss_balanced.item():.4f}")   # 预期 1.0

# 场景 B: 完全坍缩 —— 全部 token 只去前 K 个专家, 且分数也集中在前 K 个
c_collapse = torch.tensor([T] * K + [0] * (N - K), dtype=torch.float32)
scores_collapse = torch.zeros(B, S, N)
scores_collapse[..., :K] = 1.0 / K
loss_collapse = load_balancing_loss(
    num_experts=N, top_k=K, expert_scores=scores_collapse,
    batch_size_per_expert=c_collapse,
    batched_batch_size_per_expert=c_collapse.unsqueeze(0),
    granularity=gran,
)
print(f"完全坍缩的 LB loss = {loss_collapse.item():.4f}")   # 预期 N/K = 4.0
```

**需要观察的现象**：均衡场景恰好得 1；坍缩场景恰好得 \(N/K = 4\)。

**预期结果**：两个打印值分别是 `1.0000` 与 `4.0000`（这两个数由公式直接保证，可精确验证；若因版本改动不一致，请对照 loss.py L83-L111 复核）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 LB 损失要乘 \(\frac{N}{K}\) 这个缩放因子？
**答案**：让指标的「理想值」归一为 1、与 \(N, K\) 无关。无缩放时完美均衡的原始值为 \(K/N\)，随配置漂移，日志无法横向比较；乘 \(\frac{N}{K}\) 后任何配置下都是 1 = 均衡、\(\frac{N}{K}\) = 坍缩，权重 `lb_loss_weight` 的量级也与结构解耦。

**练习 2**：`attach_auxiliary_loss` 的办法与「把 aux loss 加到语言模型损失上再反传」相比，有什么实际优势？
**答案**：MoE 层的 `forward` 接口保持纯净（返回激活值），`Transformer`、`Trainer`、损失归约等上层代码完全不用感知辅助损失的存在；同时 `Trainer` 里按 token 归一的语言损失与按层缩放的辅助损失各自管理，不会互相污染分母。autograd 层面两者严格等价（backward 对 aux_loss 回传梯度 1）。

**练习 3**：如果把 `lb_loss_weight` 设成 `None`（默认 `MoEConfig` 字段是 0.01，但可设 None），路由均衡还能靠什么机制维持？
**答案**：可以开启 `router.bias_gamma`（DeepSeek-v3 式偏置均衡）：不加损失，而是每个 batch 后按 `sign(理想均分 − 实际)` 调整各专家的选路偏置（router.py L309-L334），用调度反馈替代梯度信号。两条路线也可同时使用。

## 5. 综合实践

**任务**：把 dense 的 OLMo2 小模型改造成 8 专家 top-2 的 MoE 版本，比较参数量，并在（有 GPU 的）环境里打印第一层的专家路由分布、解释负载均衡损失的读数。

**第 1 步（CPU 可跑）：dense 基线与 MoE 改造。**

```python
# 示例代码：保存为 olmo2_to_moe.py
import torch
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.utils import seed_all

seed_all(12536)

# dense 基线：官方 190M 配置（u3-l2 学过的工厂方法）
dense = TransformerConfig.olmo2_190M(vocab_size=50304)
print(f"dense 总参数:  {dense.num_params:,}")
print(f"dense 非嵌入参数: {dense.num_non_embedding_params:,}")

# MoE 改造：llama_like_moe 直接暴露 MoE 超参
moe_cfg = TransformerConfig.llama_like_moe(
    d_model=768, vocab_size=50304, n_layers=12, n_heads=12,
    num_experts=8, top_k=2, expert_hidden_size=1152,   # 8 专家 top-2
    reordered_norm=True,                                # OLMo2 风格的 norm 摆位
)
print(f"MoE 总参数:    {moe_cfg.num_params:,}")
ff_moe = moe_cfg.block.feed_forward_moe
print(f"每层 MoE: 总 {ff_moe.num_params(768):,} / "
      f"活跃 {ff_moe.num_active_params(768):,}")
```

改造要点：dense 的 `olmo2_190M` 走 `feed_forward`（SwiGLU），MoE 版走 `feed_forward_moe`（`TransformerBlockConfig` 的这个字段见 [src/olmo_core/nn/transformer/config.py:L185-L190](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L185-L190)）；`llama_like_moe` 的签名在 [src/olmo_core/nn/transformer/config.py:L1813-L1831](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1813-L1831)，模型类型被设为 `TransformerType.moe`，`build()` 相应分发到 `MoETransformer`（[src/olmo_core/nn/transformer/config.py:L464-L481](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L464-L481)）。

**第 2 步（需要 GPU + 编译好的 CUDA kernels，待本地验证）：前向并观察路由。**

```python
# 示例代码：接第 1 步（GPU 环境）
model = moe_cfg.build()          # 或 init_device="meta" 后按训练流程搬移
model.train()                    # 指标只在训练模式累计

input_ids = torch.randint(0, 50304, (2, 64), device="cuda")
out = model(input_ids)           # 返回 LMOutputWithLoss

block0 = model.blocks["0"]       # blocks 是 nn.ModuleDict
router = block0.router
counts = router.batch_size_per_expert
print("第 0 层每专家分配数:", counts.tolist())
print(f"load imbalance = {(counts.max() / counts.float().mean()).item():.3f}")

metrics = model.compute_auxiliary_metrics()
print(f"全局 LB loss:  {metrics['load balancing loss'][0].item():.4f}")
print(f"全局 LB loss (未加权): {metrics['load balancing loss unscaled'][0].item():.4f}")
```

**验收标准与解释**：

1. `counts.sum()` 应等于该 rank 的 token 总数 × top_k（示例里是 2×64×2 = 256）；
2. 刚初始化的模型 `load balancing loss unscaled` 会落在 1 与 \(N/K = 4\) 之间且偏向大于 1（随机 router 尚未学会均衡）；随训练推进应向 1 回落——**这就是该指标的数值含义**：1 为理想均衡，\(N/K\) 为完全坍缩上限；
3. `load balancing loss` = `unscaled × lb_loss_weight / n_layers`（本例 0.01/12）。

**无 GPU 的替代验证**：第 2 步的前向依赖 CUDA kernels（见 4.3.3 的运行前提说明）。没有 GPU 时可用 4.2.4 的 router 单元实践 + 4.4.4 的损失公式实践覆盖同一组知识点；也可以在 GPU 机器上直接跑官方测试 `pytest -v src/test/nn/moe/moe_test.py -k test_moe`（会自动跳过无 GPU 的部分），以及阅读 [src/examples/moe/train.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/moe/train.py)——它用 `TransformerConfig.smallmoe`（[L85-L87](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/moe/train.py#L85-L87)）配 2 卡流水线并行 + FSDP（[L122-L128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/moe/train.py#L122-L128)）训练一个 32 专家 top-4 模型，启动方式为 `torchrun --nproc-per-node=4 src/examples/moe/train.py run_name`（[L1-L7](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/moe/train.py#L1-L7)）。

## 6. 本讲小结

- MoE 层 = **router（选路）+ N 个专家 MLP（计算）+ 可选共享专家**，在 `TransformerBlock` 中顶替 dense `FeedForward` 的位置；总参数可以很大，但每个 token 只激活 \(K\) 个专家（`num_active_params`）。
- router 主干是「线性打分 → softmax/sigmoid → top-k」，外围有 jitter、分数归一化、`bias_gamma` 无损失均衡、基准测试开关等工程细节；指标（`load imbalance`、LB/Z loss）只在训练模式下累计。
- 专家计算走 **permute → compute → unpermute** 三段式；容量型 `MoE` 用固定容量缓冲区 + `bmm`（会丢溢出 token），`DroplessMoE` 按真实分组 + grouped GEMM（不丢 token，是现代主流）。
- 负载均衡损失 \(\mathcal{L}_{\mathrm{lb}} = \frac{N}{K T}\sum_i c_i \bar{s}_i\) 的读数含义：**1 = 完美均衡，趋近 \(N/K\) = 路由坍缩**；Z-loss 压制 router logits 量级以稳定 top-k。
- 辅助损失通过 `attach_auxiliary_loss` 的自定义 autograd 函数「挂」在激活上参与反传，上层训练代码零感知；损失权重默认按层数缩放（`scale_loss_by_num_layers`）。
- MoE 完整前向依赖编译的 CUDA kernels（gather/scatter 断言），router 与损失函数则可在 CPU 上单独实验。

## 7. 下一步学习建议

- **专家并行（EP）**：本讲多次出现 `apply_ep`、all-to-all、`num_local_experts`，其完整机制在第 7 单元 `u7-l2`（张量/流水线/上下文/专家并行）展开；届时回头读 [src/olmo_core/nn/moe/parallel_mlp.py:L206-L285](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/parallel_mlp.py#L206-L285) 的五步注释会非常顺畅。
- **MoE 的 CUDA 内核**：`u10-l2` 讲解 `src/olmo_core/kernels/` 与 grouped GEMM 的实现，是本讲 4.3 节的底层延伸。
- **官方 MoE 模型配方**：对比 [smallmoe](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1089-L1110)（容量型 + 共享专家）与 [olmoe_1B_7B](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1137-L1157)（dropless、64 专家 top-8）两代配方差异，体会演进方向。
- **源码延伸阅读**：`src/test/nn/moe/moe_test.py` 的专家并行一致性测试（单卡 vs 多卡结果逐项比对）是理解 EP 语义的最佳注释；`LatentMoEConfig`（[src/olmo_core/nn/moe/moe.py:L60-L89](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/moe/moe.py#L60-L89)）提供了把路由分支压到低维潜空间的变体，可作为自选题。
