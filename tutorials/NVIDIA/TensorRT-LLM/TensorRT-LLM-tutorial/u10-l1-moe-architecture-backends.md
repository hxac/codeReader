# MoE 架构与后端

> 阅读提示：本讲是专家层（高级优化特性）的第一讲。它把前面讲义里一直当作黑盒的「FFN/MLP 子层」彻底打开——当模型有上百个专家、每个 token 只用其中几个时，TensorRT-LLM 是如何用一套「可组合」的框架把它高效跑起来的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 MoE（Mixture of Experts，混合专家）层在做什么、路由（routing）解决什么问题，以及它与一个普通 FFN 的关系。
- 理解 `ConfigurableMoE` 这个「编排器（orchestrator）」如何用**组合而非继承**的方式，把「后端（计算）+ 通信（分布式）+ EPLB（负载均衡）+ 调度器（前向策略）」四个独立组件拼起来。
- 掌握 MoE 后端家族（Cutlass / TRTLLMGen / DeepGemm / CuteDSL / DenseGEMM / Marlin / Triton / MegaMoE 等）的差异与工厂选择逻辑，能根据「量化方案 + GPU 架构（SM 版本）」判断该用哪个后端。
- 理解 `EXTERNAL_COMM` 与 `FUSED_COMM` 两套调度器在前向执行上的**刻意相反**的不变量，以及调度器与编排器的职责边界。
- 了解 EPLB（Expert Parallel Load Balancing，专家并行负载均衡）如何把热点专家在 GPU 之间动态迁移，以及它与调度器、通信策略如何挂钩。

## 2. 前置知识

本讲默认你已掌握前面讲义建立的几条主线，这里只做最小回顾：

- **注意力层是 token 在「时间/序列」维度上的混合，FFN 是 token 在「特征」维度上的非线性变换。** 一层 decoder 大致是 `hidden → Attention → FFN → hidden`（见 u6-l1、u5-l1）。本讲讨论的就是其中的 FFN 子层被换成 MoE 后的事。
- **一个普通 FFN（SwiGLU 激活）长这样：** `FC1` 把 `hidden_size` 投影到 `2*intermediate_size`（其中一半是 gate、一半是 up），点乘做门控激活，再 `FC2` 投影回 `hidden_size`。MoE 的「专家」本质就是一组这样的 FFN。
- **分布式推理有两堵墙：显存墙与算力墙**（见 u9-l1）。MoE 主要靠**专家并行（EP）**翻越显存墙：把 N 个专家切到多个 rank 上，每个 rank 只持有 N/ep_size 个专家的权重；token 要找的专家若不在本地，就通过 all-to-all 类通信把激活搬过去（见 u9-l2 的 MoeAlltoAll 与数据面通信概念）。
- **`ModelConfig` 是运行时的「冻结快照」**（见 u4-l3、u3-l3）：它包裹 HF 的 `pretrained_config`，并携带 `mapping`（并行拓扑）、`quant_config`（量化方案）、`moe_backend`（选哪个 MoE 后端）等「checkpoint 不记录、部署期确定」的开关。

几个本讲会反复用到的术语，先给出通俗定义：

| 术语 | 含义 |
|------|------|
| 专家（expert） | MoE 里一组并列的 FFN 中的一个 |
| 路由 / 门控（router / gate） | 一个小线性层，给每个 token 对每个专家打分，再选 top-k |
| top-k | 每个 token 最终被送去 k 个专家计算 |
| EP（专家并行） | 不同专家分布在不同 rank 上 |
| slot（槽位） | EPLB 引入的逻辑编号，一个 slot 当前承载某个专家的权重；EPLB 关闭时 slot 就是 expert |
| 融合 GEMM | 把「分组（per-expert）的两个矩阵乘 + 激活」融合进一个或少数几个 kernel |
| EPLB | Expert Parallel Load Balancing，动态地把热点专家在 GPU 间迁移 |

## 3. 本讲源码地图

本讲全部围绕 `tensorrt_llm/_torch/modules/fused_moe/` 这个目录展开（「fused MoE」即融合 MoE）。

| 文件 | 作用 | 本讲角色 |
|------|------|---------|
| [MOE_DEVELOPER_GUIDE.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md) | MoE 架构开发者指南，改 MoE 代码前的「必读契约」 | 全讲的导航地图与约束清单 |
| `configurable_moe.py` | 编排器 `ConfigurableMoE`：拼装后端+通信+EPLB+调度器，拥有生命周期 | 核心模块 1 |
| `interface.py` | 基类 `MoE` 与枚举（`MoESchedulerKind`、`AlltoallMethodType` 等） | 所有后端的抽象契约 |
| `create_moe.py` | 工厂：按 `model_config.moe_backend` 选后端类、构造实例 | 核心模块 2（后端选择） |
| `moe_scheduler.py` | 前向执行策略（`ExternalCommMoEScheduler` / `FusedCommMoEScheduler`） | 核心模块 3（调度） |
| `moe_load_balancer.py` | EPLB 实现（`MoeLoadBalancer` / `SingleLayerMoeLoadBalancer`） | 核心模块 3（均衡） |
| `routing.py` | 路由方法（`BaseMoeRoutingMethod`、`DefaultMoeRoutingMethod`、DeepSeek 系等） | 模块 1 的前置铺垫 |
| `communication/communication_factory.py` | 通信策略自动选择（NVLink / DeepEP / AllGather…） | 模块 3 的通信编排 |
| `fused_moe_*.py`（如 `fused_moe_cutlass.py`、`fused_moe_deepgemm.py`、`fused_moe_triton.py`） | 各具体后端 | 模块 2 的实例 |

> 提醒：开发者指南里有一句话很重要——**「All new features should target ConfigurableMoE + Backend + Scheduler architecture」**。本讲会反复回到这条规则。

## 4. 核心概念与源码讲解

### 4.1 MoE 基础：稀疏门控专家与路由

#### 4.1.1 概念说明

普通 FFN 对每个 token 都走同一条 `FC1 → 激活 → FC2`，参数量随宽度线性增长。MoE 的核心想法是**稀疏激活**：准备 \(N\) 个并列的 FFN（专家），但每个 token 只调用其中 \(k\) 个（\(k \ll N\)）。这样可以在「总参数量」极大的同时，把「每次前向的实际计算量」压在很小的比例上——这正是 DeepSeek-V3、Mixtral、Qwen3-MoE 等大模型用更少算力达到更强效果的关键。

一个最小 MoE 层的数学表达：

\[
\text{out}(x) = \sum_{e \in \text{topk}_k(\text{router}(x))} s_e \cdot \text{FFN}_e(x)
\]

其中 \(\text{router}(x)\) 给出该 token 对每个专家的 logits，\(\text{topk}_k\) 选出分数最高的 \(k\) 个专家，\(s_e\) 是归一化后的路由权重，\(\text{FFN}_e\) 是第 \(e\) 个专家本身。

开发者指南用一张图描述了它在模型里的位置（路由与计算可共享专家可选地并行）：

- 入参 `Input Hidden States` 一方面进 `fc_gate (Router)` 算路由，一方面（可选）进 `Shared Expert`（共享专家，所有 token 都走）；
- 路由结果交给 `Fused-MoE`（内部做 Routing → MoE Backends 的 `FC1→Act→FC2` → Apply Weights）；
- 最后把各专家输出与共享专家输出加权求和（Combine Outputs），得到 `Final Hidden States`。

#### 4.1.2 核心流程

把上面的图翻译成执行步骤：

1. **路由打分**：`router_logits = fc_gate(hidden_states)`，形状 `[num_tokens, num_experts]`。
2. **选 top-k**：对每个 token 取分数最高的 \(k\) 个专家，得到 `token_selected_experts`（`[num_tokens, k]`，int32）和归一化权重 `token_final_scales`（`[num_tokens, k]`，float32）。
3. **分发（dispatch）**：把每个 token 的激活送到它选中的专家所在的 rank（专家并行下专家可能不在本地）。
4. **专家计算**：每个专家对自己的 token 跑 `FC1 → 激活 → FC2`。
5. **合并（combine）**：把各专家的输出按路由权重加权求和，送回原 token 所在的 rank。

> 关键观察：步骤 2 之后，「哪些 token 去哪个专家」已经确定；步骤 3、5 都是大张量的跨 rank 搬运（数据面通信，承接 u9-l2），步骤 4 是「分组 GEMM」。MoE 的全部复杂性，本质都在「如何把 3、4、5 这三步在不同硬件、不同量化方案下排得最快」。

#### 4.1.3 源码精读

路由方法统一实现 `BaseMoeRoutingMethod.apply`，它的契约就是「输入 router_logits，输出 (选中的专家 id, 最终权重)」：

- 路由基类 `apply` 契约与 `requires_separated_routing` 标志：[tensorrt_llm/_torch/modules/fused_moe/routing.py:257-L290](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/routing.py#L257-L290) —— `apply` 返回 `(token_selected_experts: int32, token_final_scales: float32)`；`requires_separated_routing` 决定路由是「在 Python 里提前算好」还是「融进 C++ kernel 内部算」。
- 默认路由 = softmax + topk：[tensorrt_llm/_torch/modules/fused_moe/routing.py:297-L333](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/routing.py#L297-L333) —— 专家数 ≤512 且 top_k ≤16 时走融合算子 `default_moe_routing_op`，否则退回纯 PyTorch `torch.topk`。
- MoE 在模型里的位置（路由 → 后端 → 合并）：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:7-L32](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L7-L32) —— 这就是本小节那张文字流程图的出处。

> 命名澄清：仓库里 `top_k`（路由方法的属性）和 `experts_per_token` 是同一回事——`experts_per_token` property 直接返回 `self.top_k`（见 routing.py:278-280）。后文混用时它们等价。

#### 4.1.4 代码实践

1. **实践目标**：用最少的代码，亲手跑通「路由 + 专家选择」这一步，理解 router_logits 的形状与 top-k 的含义。
2. **操作步骤**：写一段独立 Python（**示例代码**，非项目原有）：

   ```python
   import torch
   import torch.nn.functional as F
   num_tokens, num_experts, top_k = 4, 8, 2
   router_logits = torch.randn(num_tokens, num_experts)   # 模拟 fc_gate 输出
   probs = F.softmax(router_logits, dim=-1)
   topk_vals, topk_idx = torch.topk(probs, k=top_k, dim=-1)
   print("每个 token 选中的专家 id:", topk_idx.int())      # [num_tokens, top_k]
   print("对应的路由权重:", topk_vals)                      # [num_tokens, top_k]
   ```
3. **需要观察的现象**：`topk_idx` 每行有 `top_k=2` 个不同的专家编号；`topk_vals` 每行是两个最大的 softmax 概率。
4. **预期结果**：输出两个形状均为 `[4, 2]` 的张量；每行的两个权重之和通常远小于 1（因为其余 6 个专家也分走了一部分概率）。
5. **若无法确定运行结果**：上述为标准 PyTorch 行为，可本地直接运行验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `num_experts` 从 8 改成 1024，`DefaultMoeRoutingMethod.apply` 内部走的分支会变吗？为什么？

> **答案**：会变。routing.py:325 的条件是 `num_experts > 512 or self.top_k > 16` 时退回 `apply_pytorch`（纯 `torch.topk`），否则走融合算子 `default_moe_routing_op`。1024 > 512，故走纯 PyTorch 分支。

**练习 2**：为什么 MoE 能在「参数量极大」的同时保持「单次前向算力可控」？

> **答案**：因为稀疏激活——每个 token 只激活 \(k\) 个专家（\(k \ll N\)）。总参数量是全部 \(N\) 个专家之和（大），但单次前向的实际浮点量只与 \(k\) 个专家相关（小）。代价是需要路由与跨 rank 分发。

---

### 4.2 ConfigurableMoE：组合式编排器（最小模块 1）

#### 4.2.1 概念说明

有了上一节的「路由 + 专家计算 + 合并」骨架，现在的问题是：**这棵骨架在不同后端、不同通信硬件、是否开负载均衡时，排列组合非常多，怎么写才不乱？**

仓库给出的答案是 `ConfigurableMoE`——一个**编排器**。它的设计哲学是「**组合优于继承（Composition over inheritance）**」：不把「计算/通信/均衡/调度」做成一层层继承，而是做成四个**独立、可替换**的组件，由编排器在构造时把它们拼起来。

```
ConfigurableMoE
├── Backend        纯计算：routing → quantize → FC1 → act → FC2
├── Communication  分布式（可选）：dispatch tokens → compute → combine
├── EPLB           （可选）：跨 GPU 动态迁移专家
└── MoEScheduler   前向执行策略：分块、EPLB 钩子顺序、通信编排
```

关键职责切分（务必记住这条边界）：

- **编排器拥有「模块生命周期」**：构造后端、装权重、建通信策略、推进 `repeat_idx`、记录 DWDP。这些事无论哪种前向路径都要做，所以集中在一处。
- **调度器拥有「前向执行决策」**：分块（chunking）、EPLB 钩子的触发时机、dispatch/combine 的先后、何时调 `backend.run_moe`。这些事与「通信放在 kernel 内还是 kernel 外」强相关，所以拆到调度器里。

`ConfigurableMoE.forward_impl` 因此非常薄：解析 `output_dtype` → 委托 `self.scheduler.forward(...)` → 做一些两种调度器都共享的簿记（DWDP、`repeat_idx`）。

#### 4.2.2 核心流程

构造期（`__init__`）按固定顺序拼装四个组件：

1. 调 `MoE.__init__`（基类）算出真正的 `layer_idx`、负载均衡器、EP 分片等**编排器级**属性。
2. `_create_and_sync_backend`：用 `resolve_moe_cls` 选后端类，**临时跳过权重创建**地构造后端（`layer_idx=None`、`init_load_balancer=False`、`without_comm=True`），再把编排器算好的 EPLB 属性「镜像」到后端，最后才让后端 `create_weights()`。
3. （可选）启用 DWDP。
4. `_create_comm_strategy_auto`：建通信策略。**`FUSED_COMM` 后端直接返回 `None`**（通信已融进 kernel），否则交给 `CommunicationFactory` 自动选。
5. 计算 `moe_max_num_tokens`、准备用于分块重叠的辅助 stream。
6. `create_moe_scheduler(self)`：按后端的 `scheduler_kind` 选 `ExternalCommMoEScheduler` 或 `FusedCommMoEScheduler`。

前向期（`forward_impl`）只做三件事：填 `output_dtype` → `self.scheduler.forward(...)` → DWDP 记录与 `repeat_idx` 自增。

#### 4.2.3 源码精读

- 编排器的设计原则与组件划分：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:34-L67](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L34-L67) —— 这段直接给出「组合优于继承」等 6 条核心原则。
- 类 docstring（说明它是「薄包装」，委托给具体后端）：[tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py:75-L115](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py#L75-L115)。
- 构造函数：按顺序拼装后端 → DWDP → 通信 → 分块配置 → 校验 → 调度器：[tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py:146-L241](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py#L146-L241)。
- 镜像到后端的属性清单 `_BACKEND_SYNC_ATTRS`：[tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py:53-L65](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py#L53-L65) —— 后端先用空壳构造，再把这些属性（`layer_idx`、`num_slots`、`layer_load_balancer` 等）同步过去，**新增 EPLB 派生属性时必须在这里登记**，否则会静默漂移。
- 后端构造的「小步舞」：临时跳过权重创建 → 空壳构造 → 镜像属性 → 真正 `create_weights`：[tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py:267-L349](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py#L267-L349)。
- 通信策略自动创建（`FUSED_COMM`/DWDP 返回 `None`）：[tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py:514-L544](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py#L514-L544)。
- 薄薄的 `forward_impl`：委托调度器 + 共享簿记：[tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py:546-L605](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py#L546-L605)。
- 分块数计算 `calculate_num_chunks`：[tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py:420-L436](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/configurable_moe.py#L420-L436) —— 按 `moe_max_num_tokens` 把一步的 token 切成若干块，是调度器分块的依据。

> 一个常被忽略的细节：`MoE.forward`（基类）是 `@final` 的，子类**不准覆盖** `forward`，只能实现 `forward_impl`；当处于 `torch.compile` 且 `register_to_config` 时，它会把整层包成自定义算子 `trtllm::moe_custom_op` 以进入图（见 interface.py 的 `moe_custom_op`）。编排器/后端都只实现 `forward_impl`。

#### 4.2.4 代码实践

1. **实践目标**：通过阅读源码，画出 `ConfigurableMoE.__init__` 里四个组件的**构造时序**，并标注「为什么后端要先空壳构造再镜像属性」。
2. **操作步骤**：
   - 打开 configurable_moe.py，从 `__init__`（L146）读到 `create_moe_scheduler`（L241）。
   - 单独精读 `_create_and_sync_backend`（L267-L349）的注释，理解 `_temporarily_skip_weight_creation` 这个上下文管理器为何要「临时把 `skip_create_weights_in_init` 翻成 True 再恢复」。
3. **需要观察的现象**：后端构造时的三个特殊参数 `layer_idx=None`、`init_load_balancer=False`、`without_comm=True`，恰好对应「后端不要自己注册负载均衡器、不要自己建通信」——这些是编排器的职责。
4. **预期结果**：你能用自己的话讲清楚——权重分配依赖 `layer_load_balancer`/`initial_local_expert_ids`/`num_slots`，而这些只有在编排器侧的 `MoE.__init__` 跑完后才已知，所以后端必须**先空壳、后同步、再建权重**。
5. **若无法确定运行结果**：本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ConfigurableMoE` 的 `can_implement` 永远返回 `(False, "Query the specific backend directly")`？

> **答案**：因为它只是一个「薄包装/编排器」，自身不做计算；能力由它内部委托的具体后端（`CutlassFusedMoE`、`TRTLLMGenFusedMoE` 等）决定。要查能力应直接问后端类。见 configurable_moe.py:117-144。

**练习 2**：如果新增了一个「由编排器算出、后端也需要读」的 EPLB 派生属性，必须改哪里？

> **答案**：必须把它加进 `_BACKEND_SYNC_ATTRS`（configurable_moe.py:53-65）。否则 `_create_and_sync_backend` 末尾的镜像循环不会同步它，后端会读到旧值/空值，且不会报错——这正是注释里强调的「保持同步在一处、不让 `__init__` 静默漂移」。

---

### 4.3 MoE 后端家族与工厂选择（最小模块 2）

#### 4.3.1 概念说明

「后端（Backend）」是 `ConfigurableMoE` 里**唯一负责纯计算**的组件：路由（若不在外部提前算）、量化、`FC1 → 激活 → FC2`、应用路由权重。它**不含通信逻辑、不含 EPLB 逻辑**（唯一的例外是 `FUSED_COMM` 后端，通信被融进了它的 fused kernel，那时它「拥有」kernel 内的 SymmBuffer 集合通信）。

TensorRT-LLM 的 MoE 后端是一大家子，每个后端都面向特定的「**量化方案 × GPU 架构（SM 版本）× 场景**」组合。选择不在运行时随机，而是由 `model_config.moe_backend` 这个字符串经工厂 `get_moe_cls` 决定；当一个后端无法服务当前环境（量化不匹配、SM 不支持、缺少依赖）时，工厂会**回退到 `CutlassFusedMoE`** 并打 warning。

下表摘自开发者指南（精简版），帮你建立「哪个后端管哪块硬件」的全景：

| 后端类 | 硬件 | 典型场景 | 调度器 |
|--------|------|----------|--------|
| `CutlassFusedMoE` | SM80+ | 量化支持最全的高吞吐主力 | EXTERNAL_COMM |
| `TRTLLMGenFusedMoE` | SM100/103 | Blackwell 上低延迟 + 高吞吐 | EXTERNAL_COMM |
| `DeepGemmFusedMoE` | SM100/103 | Blackwell 上 FP8 Block Scales | EXTERNAL_COMM |
| `CuteDslFusedMoE` | SM100/103 | NVFP4 高吞吐，通常快于 Cutlass | EXTERNAL_COMM |
| `DenseGEMMFusedMoE` | SM100/103 | NVFP4 低延迟；把所有专家塞进一个矩阵 | EXTERNAL_COMM |
| `MarlinFusedMoE` | SM90 | Hopper 上 W4A16 NVFP4 | EXTERNAL_COMM |
| `TritonFusedMoE` | SM90 | Hopper 上 GPT-OSS（旧路径） | legacy |
| `MegaMoEDeepGemm` / `MegaMoECuteDsl` | SM100/103 | W4A8_MXFP4_MXFP8 / NVFP4，通信融进 kernel | FUSED_COMM |
| `WideEPMoE` / `VanillaMoE` | 全部 | 正在淘汰 / 仅调试 | legacy |

每个后端通过两个「声明式」机制参与系统：

- `can_implement(quant_algo, dtype_activation, swiglu_gptoss_style, ...)`：类方法，声明「我能否实现这种量化 + 激活 dtype + SM 组合」，返回 `(bool, reason)`。它是后端能力矩阵的**唯一真相源**。
- `scheduler_kind`：类属性，声明走 `EXTERNAL_COMM` 还是 `FUSED_COMM`，从而决定编排器给配哪种调度器、建不建通信策略。

#### 4.3.2 核心流程

后端的选择链路是：

```
model_config.moe_backend (字符串, 默认 "CUTLASS")
        │
        ▼
get_moe_cls(model_config, quant_config, layer_idx)   # 工厂: 按 .upper() 分派
        │  (量化/SM 不匹配时回退到 CutlassFusedMoE + warning)
        ▼
resolve_moe_cls(...)   # 叠加 LoRA 可用性等二次校验
        │
        ▼
create_moe(...)        # 若该类被 ConfigurableMoE 支持 → 包成 ConfigurableMoE
        │                否则走 legacy create_moe_backend
        ▼
实例化后端 (空壳)  →  ConfigurableMoE 镜像 EPLB 属性  →  create_weights
```

`get_moe_cls` 内部是一个长串的 `if moe_backend.upper() == "XXX"`，每个分支都做了「量化是否匹配、SM 是否支持、依赖是否可用」的检查，不匹配就回退。例如 `DEEPGEMM` 直接返回 `DeepGemmFusedMoE`；`DENSEGEMM` 在非 NVFP4 或非 SM100/103 时回退到 Cutlass；`MEGAMOE_*` 还会调 `can_implement` 探测环境，失败也回退到 Cutlass。

#### 4.3.3 源码精读

- 后端能力矩阵（量化支持的全表，**对比 triton 与 deepgemm 的权威依据**）：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:200-L214](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L200-L214)。
- 后端文件总览（硬件/场景/调度器一栏）：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:143-L156](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L143-L156)。
- 工厂 `get_moe_cls`（按 `moe_backend` 分派 + 回退）：[tensorrt_llm/_torch/modules/fused_moe/create_moe.py:56-L208](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/create_moe.py#L56-L208) —— 注意 `TRITON` 分支（L159-L160）直接返回 `TritonFusedMoE`（legacy 路径），`DEEPGEMM` 分支（L115-L116）返回 `DeepGemmFusedMoE`。
- `create_moe` 决定是否包成 `ConfigurableMoE`：[tensorrt_llm/_torch/modules/fused_moe/create_moe.py:535-L669](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/create_moe.py#L535-L669) —— `ENABLE_CONFIGURABLE_MOE` 默认为 `"1"`；`TritonFusedMoE` 不在受支持列表里（L607-L609），故走 legacy。
- 基类 `can_implement` 抽象契约：[tensorrt_llm/_torch/modules/fused_moe/interface.py:236-L268](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/interface.py#L236-L268)。
- `scheduler_kind` 类属性（默认 `EXTERNAL_COMM`）：[tensorrt_llm/_torch/modules/fused_moe/interface.py:225-L228](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/interface.py#L225-L228)。
- `moe_backend` 字段的默认值与 `AUTO` 解析：[tensorrt_llm/_torch/model_config.py:161-L163](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L161-L163)（默认 `CUTLASS`）与 [tensorrt_llm/_torch/model_config.py:332-L348](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L332-L348)（`resolve_moe_backend` 把 `AUTO` 按架构解析成具体后端）。

#### 4.3.4 代码实践（对应总任务的「对比 triton 与 deepgemm」）

1. **实践目标**：依据真实源码，对比 `TritonFusedMoE` 与 `DeepGemmFusedMoE` 两个后端的**适用场景**，并解释为何二者「不在同一条主路径上」。
2. **操作步骤**：
   - 在 [能力矩阵 MOE_DEVELOPER_GUIDE.md:200-L214](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L200-L214) 里找到 `Triton` 列与 `DeepGemm` 列，逐行比较它们支持的量化方案。
   - 在 [后端总览 MOE_DEVELOPER_GUIDE.md:143-L156](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L143-L156) 里比较二者的硬件、调度器栏（注意 Triton 标注为 legacy）。
   - 在 [架构过渡表 MOE_DEVELOPER_GUIDE.md:118-L123](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L118-L123) 确认：`DeepGemmFusedMoE` 在新路径（ConfigurableMoE）里，`TritonFusedMoE` 仍在旧路径（standalone + 内嵌通信）。
3. **需要观察的现象 / 预期结论**（**待本地以实际版本核对**，下表是据当前 HEAD 源码归纳）：

   | 维度 | `TritonFusedMoE` | `DeepGemmFusedMoE` |
   |------|------------------|--------------------|
   | 硬件 | 仅 SM90（Hopper） | SM100/103（Blackwell） |
   | 量化 | BF16、FP8 QDQ、FP8 Block Scales、W4A16 MXFP4、W4A8 MXFP4 FP8（均 SM90） | 仅 FP8 Block Scales |
   | 典型模型 | GPT-OSS（需 `swiglu_gptoss_style=True`） | Blackwell 上 FP8 Block Scales 的 MoE |
   | 架构路径 | legacy（standalone，内嵌通信） | 新路径（ConfigurableMoE + EXTERNAL_COMM） |
   | 调度器 | legacy 路径 | `EXTERNAL_COMM` |

   一句话总结：**Triton 是 Hopper 上 GPT-OSS 的专用 legacy 后端；DeepGemm 是 Blackwell 上 FP8 Block Scales 的新路径后端**。两者面向不同 GPU 代际、几乎不重叠的量化集合，且分属新旧两套架构。
4. **若无法确定运行结果**：量化/硬件支持表以源码为准，建议本地打开上表引用的两个 Markdown 表格逐格核对。

#### 4.3.5 小练习与答案

**练习 1**：用户把 `moe_backend` 设成 `DENSEGEMM`，但模型是 BF16 未量化的。工厂最终会返回哪个后端类？为什么？

> **答案**：返回 `CutlassFusedMoE`。因为 `DENSEGEMM` 分支检查「`quant_config` 非 NVFP4 就回退 Cutlass」（create_moe.py:117-123）。BF16 未量化不满足 NVFP4，故回退并打 warning。

**练习 2**：为什么说「不要为了图省事，让新后端继承 `CutlassFusedMoE`」？

> **答案**：这是「历史捷径」——当前很多后端为了复用负载均衡器、权重管理、TP/EP 基础设施而继承了 `CutlassFusedMoE`，但官方明确这将被重构，未来会抽出一个专门的 `MoEBackend` 接口。新后端应直接继承 `MoE`（见 MOE_DEVELOPER_GUIDE.md:241 的 Note）。

---

### 4.4 MoE 调度器：EXTERNAL_COMM 与 FUSED_COMM（最小模块 3 之调度）

#### 4.4.1 概念说明

编排器把「前向怎么走」交给了调度器。调度器的分歧点只有一个：**跨 rank 的专家并行交换（EP exchange）发生在哪里？**

- **`EXTERNAL_COMM`（外部通信）**：通信是 kernel **之外**、由 host 编排的独立步骤。调度器显式地依次调 `Communication.dispatch`（把激活散到目标 rank）和 `Communication.combine`（把结果收回来）。Cutlass、DeepGemm、CuteDSL、DenseGEMM、TRTLLMGen 都走这条。
- **`FUSED_COMM`（融合通信）**：通信被**融进了后端的 fused kernel**（DeepGEMM 的 `fp8_fp4_mega_moe` 风格，即「MegaMoE」），通过 NVLink SymmBuffer（对称缓冲）在 kernel 内完成跨 rank 交换。host 侧**没有** dispatch/combine。MegaMoE 系列走这条。

这两种路径有**刻意相反的不变量**（这是本讲最容易踩坑的地方）：

| 不变量 | EXTERNAL_COMM | FUSED_COMM |
|--------|---------------|------------|
| `use_dp_padding`（DP 填充） | 遵守 | 忽略 |
| ADP padding（注意力 DP 的填充行） | 保留 | 在切分前剥掉 |
| 空 chunk（某 rank 0 token） | 用 chunk-0 替代空 chunk | 仍要启动 kernel（让对端能跨过 kernel 内的 NVLink barrier） |
| 多流分块重叠 | 允许（`aux_stream` 可用且非 alltoall） | 禁止（lockstep 启动） |

通信策略本身（当走 EXTERNAL_COMM 时）由 `CommunicationFactory` 按硬件优先级**自动选择**，优先级为：

`NVLinkOneSided > NVLinkTwoSided > NcclEP > DeepEP > DeepEPLowLatency > AllGatherReduceScatter（兜底，永远可用）`

这些通信策略承接 u9-l2 的「数据面通信」概念——它们搬的就是 MoE 的激活大张量。

#### 4.4.2 核心流程

`ExternalCommMoEScheduler.forward` 的骨架（每步前向）：

1. 填充/规整 `all_rank_num_tokens`（含 0-token rank 的死锁修复：强制开 DP padding）。
2. `moe.calculate_num_chunks` 算分块数；`moe.determine_communication_method` 校验/回退通信策略（AllToAll 不可行时回退 AllGather）。
3. 单块走 `_forward_single_chunk`，多块走 `_forward_multiple_chunks`（可选 `aux_stream` 重叠）。
4. 截掉 DP padding。

而每块的核心 `_forward_chunk_impl` 是一个**九步**流水（这是本讲最该背下来的流程）：

```
1. EPLB start_wait_gpu_stage      （首个 chunk/repeat 才触发）
2. 路由 routing                    （若需分离路由：routing_method.apply）
3. EPLB update_statistic + route   （把专家 id 重映射成 slot id）
4. 通信 prepare_dispatch            （仅 NVLink 双侧）
5. 量化 + dispatch                  （自适应顺序：post-quant 量化再发 / pre-quant 发原始值）
6. backend.run_moe                 （真正的 FC1→Act→FC2）
7. EPLB start_set_cpu_stage        （末个 chunk/repeat 才触发）
8. 通信 combine                     （把结果收回来）
9. EPLB done_set_cpu_stage
```

`FusedCommMoEScheduler` 的每块流程更短（没有 host 侧 dispatch/combine），但有一个硬要求：**每个 chunk（含 0-token chunk）都必须在所有 EP rank 上启动 kernel**，否则对端会在 kernel 内的 NVLink barrier 上死等。

#### 4.4.3 源码精读

- 调度器选择表（`MoESchedulerKind` × 调度器类 × 后端 × 是否跨 rank 交换）：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:62-L67](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L62-L67)。
- EXTERNAL_COMM 的九步流程图：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:71-L83](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L71-L83)。
- FUSED_COMM 的流程与「零 token 也要启动」约定：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:87-L95](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L87-L95)。
- `MoESchedulerKind` 枚举定义：[tensorrt_llm/_torch/modules/fused_moe/interface.py:110-L131](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/interface.py#L110-L131)。
- 调度器模块 docstring（职责边界：调度器只读编排器状态、禁写 `repeat_idx`）：[tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py:16-L41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py#L16-L41)。
- `ExternalCommMoEScheduler.forward`（含 0-token 死锁修复）：[tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py:128-L224](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py#L128-L224)。
- 九步 `_forward_chunk_impl`（带详细注释）：[tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py:344-L567](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py#L344-L567)。
- `FusedCommMoEScheduler` 类 docstring（九条不变量）：[tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py:845-L868](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py#L845-L868)。
- 工厂 `create_moe_scheduler`：[tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py:1220-L1231](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py#L1220-L1231)。
- 通信策略自动选择（优先级链）：[tensorrt_llm/_torch/modules/fused_moe/communication/communication_factory.py:52-L96](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/communication/communication_factory.py#L52-L96)（docstring 列出优先级）与 [tensorrt_llm/_torch/modules/fused_moe/communication/communication_factory.py:134-L167](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/communication/communication_factory.py#L134-L167)（实际 try-catch 选择）。

> 细节：九步流程里有一处「自适应量化/分发顺序」——若通信策略支持 `supports_post_quant_dispatch()`，就「先量化再分发」（发量化后的数据，省带宽）；否则「先分发再量化」（发原始值，本地量化）。这是 EXTERNAL_COMM 在带宽与算力间权衡的体现。

#### 4.4.4 代码实践

1. **实践目标**：把 `_forward_chunk_impl` 的九步流程，对照源码逐一上色——标出哪几步是「调度器独有」（路由/EPLB/通信编排），哪几步是「真正算」（`backend.run_moe`）。
2. **操作步骤**：
   - 打开 [moe_scheduler.py:344-L567](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_scheduler.py#L344-L567)。
   - 找到 `Step 1` 到 `Step 9` 的注释块，为每一步写一句话：它做什么、依赖谁、产出什么。
   - 特别留意 Step 5（量化+dispatch）里 `supports_post_quant` 的 if/else 分支。
3. **需要观察的现象**：你会发现 9 步里只有 **Step 6 (`backend.run_moe`)** 是真正的 FC1→Act→FC2 计算；其余 8 步都在为它「准备输入、收尾输出、维护负载均衡统计」。这正是「后端 = 纯计算、调度器 = 编排」的具象化。
4. **预期结果**：得到一张九步表格，标注每步归属（EPLB / 路由 / 通信 / 计算）。
5. **若无法确定运行结果**：源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `FusedCommMoEScheduler` 在某 rank 本 chunk 有 0 个 token 时，**不能**像 EXTERNAL_COMM 那样用 chunk-0 替代，而是仍要启动一次 kernel？

> **答案**：因为 FUSED_COMM 的跨 rank 交换是**融在 kernel 内**的，通过 NVLink SymmBuffer 做集合通信。所有 EP rank 必须在 kernel 内的 NVLink barrier 处「会合」；若某 rank 跳过本次启动，对端就会在 barrier 上无限等待（死锁）。所以即便 0 token 也要启动，让对端能跨过 barrier。见 moe_scheduler.py:1042-1056 与 FusedCommMoEScheduler 类 docstring 第 7 条不变量。

**练习 2**：调度器允许写 `moe.repeat_idx` 吗？为什么？

> **答案**：不允许。`repeat_idx` 是编排器级状态，在 `forward_impl` 末尾按「每次前向 +1，与 chunk 数无关」推进（见 configurable_moe.py:603）。调度器可能在单块或多块路径下被进入，若由它推进会造成重复自增。这是 moe_scheduler.py:25-29 明确规定的契约。

---

### 4.5 EPLB：专家并行负载均衡（最小模块 3 之均衡）

#### 4.5.1 概念说明

专家并行有一个天然痛点：**路由是不均衡的**。某些「热门专家」会被大量 token 选中，持有它的那个 rank 成为瓶颈，而其他 rank 空转。EPLB（Expert Parallel Load Balancing）就是用来解决这个问题的——它**动态地把热点专家在 GPU 之间迁移**，让每个 rank 的实际负载趋于均衡。

EPLB 引入了 **slot（槽位）** 这个间接层：

- 物理上，每个 rank 持有若干个 slot，每个 slot 当前承载某个专家的权重；
- 路由算出的「专家 id」会被 EPLB 重映射成「slot id」，token 实际是被送到 slot；
- EPLB 周期性地统计各专家的负载，在 CPU 侧把热点专家的权重搬到负载轻的 rank 的 slot 上，再更新「slot → 专家」的映射。

EPLB 分两种模式：

- **静态路由（static routing）**：`layer_updates_per_iter == 0`，只做一次初始分配，运行时不变。
- **动态路由（dynamic routing）**：运行时持续统计 + 迁移权重。这才是真正的负载均衡。

动态 EPLB 的运行时开销被精心设计成与 MoE 前向重叠：统计在 GPU 上做、权重迁移在 CPU 辅助流上做，二者通过事件（event）同步。

#### 4.5.2 核心流程

动态 EPLB 在每次前向里的生命周期，恰好嵌在调度器的九步流程中（4.4.2）：

```
[GPU 阶段]                          [CPU 阶段]
start_wait_gpu_stage   ← Step 1     start_set_cpu_stage   ← Step 7
  (等上一轮的统计/迁移完成)            (在本轮统计基础上，开始决定新的专家→slot 映射)
routing → update_statistic ← Step 3
  (用本轮路由结果更新各专家负载统计)
route (expert id → slot id)
  ... backend.run_moe ...           done_set_cpu_stage    ← Step 9
                                      (权重迁移完成，下一轮生效)
```

注意时序要点：

- `start_wait_gpu_stage` / `done_wait_gpu_stage` 只在**首个** chunk/repeat 触发；
- `start_set_cpu_stage` / `done_set_cpu_stage` 只在**末个** chunk/repeat 触发；
- 这保证了一轮前向里，统计只算一次、迁移只发起一次，与 chunk 数无关。

EPLB 的开关由配置 `model_config.moe_load_balancer` 控制，且**只有特定模型架构 + EP>1 + 非智能路由**才会真正创建。它在多 rank 间用 MPI 共享内存（`HostMoeTensorSharer`）搬运权重张量。

#### 4.5.3 源码精读

- EPLB 的调度器/EPLB 约束（FUSED_COMM 用 `ignore_allreduce=False` 等）：[tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md:217-L225](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L217-L225)。
- 基类里 EPLB 的初始化与 slot/专家分片：[tensorrt_llm/_torch/modules/fused_moe/interface.py:514-L646](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/interface.py#L514-L646) —— 注意 `num_slots % ep_size == 0` 的硬约束（每个 rank 持有相同数量的 slot）。
- 基类里的 `_load_balancer_*` 钩子（对应九步里的 EPLB 步骤）：[tensorrt_llm/_torch/modules/fused_moe/interface.py:692-L759](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/interface.py#L692-L759)。
- `SingleLayerMoeLoadBalancer`（每层一个，包 C++ 实现）：[tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py:298-L372](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py#L298-L372)。
- `start_wait_gpu_stage`（GPU 阶段同步，含多流重叠）：[tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py:518-L534](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py#L518-L534)。
- 全局 `MoeLoadBalancer`（线程本地单例、上下文管理器、迭代生命周期）：[tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py:766-L1010](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py#L766-L1010)。
- 支持的模型架构白名单 `moe_model_arch_list`：[tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py:1013-L1034](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py#L1013-L1034) —— 只有 DeepSeek-V3/V4、GPT-OSS、Mixtral、Qwen3-MoE、Llama4 等架构才会触发 EPLB。
- `maybe_create_moe_load_balancer`（创建门槛：受支持架构 + EP>1 + 非智能路由 + 配置非空）：[tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py:1037-L1062](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py#L1037-L1062)。

> 与 DWDP 互斥：动态 EPLB 和 DWDP（一种权重 prefetch 方案）不能同时用——DWDP 运行时会 swap `param.data` 到一个组合虚拟地址张量，EPLB 的再均衡会把它冲掉。基类 `_init_dwdp_expert_layout` 里有断言强制这一点（见 interface.py:450-452）。

#### 4.5.4 代码实践

1. **实践目标**：搞清楚「EPLB 在一次前向里到底被触发了哪几个钩子、各在九步流程的哪个位置」，并验证 `repeat_idx`/chunk 对触发时机的影响。
2. **操作步骤**：
   - 对照 4.4 的九步流程与 interface.py 的 `_load_balancer_*` 钩子（[interface.py:692-L759](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/interface.py#L692-L759)），标注每个钩子的 `is_first_call` / `is_last_call` 守卫。
   - 在 `moe_load_balancer.py` 里看 `start_wait_gpu_stage` 等方法内部的 `func_called_count` 断言（[moe_load_balancer.py:518-L534](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/moe_load_balancer.py#L518-L534)）——它们用计数器保证「每个阶段在一轮里只被调一次」。
3. **需要观察的现象**：`func_called_count` 是一个 dict，记录每个阶段被调次数；`done_set_cpu_stage` 末尾会把所有计数器清零（moe_load_balancer.py:571-572），开启下一轮。这是一种「用断言把隐式状态机显式化」的防御式编程。
4. **预期结果**：你能解释「为什么 start_wait_gpu 只在 first call、start_set_cpu 只在 last call」——因为多 chunk 时，统计只需算一次（首 chunk 的路由结果即可聚合），而权重迁移必须在最后一次计算结束后才能安全开始。
5. **若无法确定运行结果**：源码阅读型实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：EPLB 开启时，`token_selected_experts`（路由选中的专家 id）和最终送进 `backend.run_moe` 的 `token_selected_experts` 是同一个张量吗？

> **答案**：通常不是。调度器 Step 3 会调用 `_load_balancer_route`，把「专家 id」重映射成「slot id」（见 interface.py:729-736 与 moe_scheduler.py:442）。所以 `run_moe` 收到的是 slot id（落在 `[0, num_slots)`）。EPLB 关闭时二者相同（slot id == expert id）。

**练习 2**：为什么 EPLB 要求 `num_slots % ep_size == 0`？

> **答案**：因为 EPLB 提供的是**均匀** slot 划分——每个 rank 必须持有相同数量的 slot（`num_slots // ep_size`），这样所有后端 kernel 看到的本地 slot 数才一致。若 `num_slots` 不能被 `ep_size` 整除，就无法均匀切分。见 interface.py:557-562 的断言。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**源码追踪型**综合任务（对应总任务的「阅读 MOE_DEVELOPER_GUIDE.md，列出修改 MoE 代码前必须了解的架构约束」）。

**任务**：假设你要给 MoE 加一个「新的低精度量化方案 Q」，请按 `ConfigurableMoE + Backend + Scheduler` 架构，规划改动清单并说明约束。

**建议步骤**：

1. **先读契约**：通读 [MOE_DEVELOPER_GUIDE.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md) 的「Core Design Principles」「Anti-Patterns」「Canonical Examples」三节，列出修改 MoE 代码前必须遵守的架构约束。至少应包含：
   - 后端只做纯计算，**不**在 backend 里写通信/EPLB/前向执行策略；
   - 新功能必须走 `ConfigurableMoE + Backend + Scheduler`，**不**改 legacy `XXFusedMoE`；
   - 每个 backend 必须实现 `can_implement` 并如实声明能力；新 backend 继承 `MoE`（而非 `CutlassFusedMoE`）；
   - 新测试只加到 `test_moe_backend.py` / `test_moe_module.py`，**不**加到 legacy `test_fused_moe.py` / `test_moe.py`；
   - `FUSED_COMM` 后端必须让 `quantize_input` 容忍 `x.shape[0]==0`，且配 `distributed_tuning_strategy=PARALLEL`。
2. **选定后端**：根据 Q 的精度与目标 GPU 代际，从能力矩阵（[MOE_DEVELOPER_GUIDE.md:200-L214](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/MOE_DEVELOPER_GUIDE.md#L200-L214)）判断是扩展现有后端（首选）还是新建后端；若是新建，参照 `CutlassFusedMoE`（EXTERNAL_COMM）或 `MegaMoEDeepGemm`（FUSED_COMM）的范式。
3. **走工厂**：若新建后端类，要在 `get_moe_cls`（[create_moe.py:56-L208](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/create_moe.py#L56-L208)）加分支、在 `create_moe`（[create_moe.py:607-L609](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/create_moe.py#L607-L609)）的 ConfigurableMoE 受支持列表里登记，否则会被当成 legacy。
4. **检查调度器/EPLB 兼容**：确认新量化在九步流程的「量化」环节（Step 5 自适应顺序）是否需要特殊处理；若新后端是 `FUSED_COMM`，确认九条不变量都能满足。
5. **产出**：一份「改动文件清单 + 每个文件改什么 + 触及哪些约束」的简表。

**验收标准**：你的清单里，计算逻辑只落在 backend、通信只在 `communication/`、前向编排只在 scheduler、EPLB 钩子通过基类 helper 调用——四者泾渭分明，没有任何跨界。

## 6. 本讲小结

- **MoE = 稀疏激活**：用路由给每个 token 选 top-k 个专家，以「大总参数量、小单次算力」翻越算力墙；它在模型里就是替换掉普通 FFN 的那一段。
- **`ConfigurableMoE` 是编排器，靠组合而非继承**：它拼装「后端（纯计算）+ 通信（分布式）+ EPLB（均衡）+ 调度器（前向策略）」四个独立组件，拥有模块生命周期，前向委托给调度器。
- **后端家族面向「量化 × SM 代际 × 场景」**：由 `model_config.moe_backend` 经 `get_moe_cls` 选择，不匹配时回退 `CutlassFusedMoE`；每个后端用 `can_implement` 声明能力、用 `scheduler_kind` 声明调度路径。**Triton 是 Hopper/GPT-OSS 的 legacy 后端，DeepGemm 是 Blackwell/FP8 Block Scales 的新路径后端**，二者几无交集。
- **两套调度器有刻意相反的不变量**：`EXTERNAL_COMM`（host 编排 dispatch/combine、支持多流重叠）与 `FUSED_COMM`（通信融进 kernel、lockstep 启动、零 token 也要启动）。
- **EPLB 用 slot 间接层动态迁移热点专家**：统计在 GPU、迁移在 CPU 辅助流，二者通过 event 重叠；其运行时钩子嵌在调度器九步流程的首/末 chunk·首/末 repeat。
- **改 MoE 代码的硬规矩**：所有新功能走 `ConfigurableMoE + Backend + Scheduler`；backend 不含通信/EPLB/前向策略；新 backend 继承 `MoE`；新测试只进 `test_moe_backend.py`/`test_moe_module.py`。

## 7. 下一步学习建议

- **横向——其他高级优化**：本讲是 u10（高级优化特性）的第一讲。后续可学 u10-l2（量化机制，理解 `QuantConfig`/`QuantAlgo` 如何驱动本讲的 `can_implement`）、u10-l3（投机解码）、u10-l4（CUDA Graph / torch.compile，理解 `moe_custom_op` 为何要被包成自定义算子进图）。
- **纵向——MoE 深水区**：想深入通信策略实现，读 `communication/nvlink_one_sided.py`、`communication/deep_ep.py`，对照 u9-l2 的 MoeAlltoAll；想深入 fused kernel，读 `mega_moe/` 下的 `CHUNKING_DESIGN.md`、`COMMUNICATION_COMPARISON.md`。
- **上手——加一个新量化/后端**：按本讲「综合实践」的清单，以 `quantization.py` 里的 `FP8QDQFusedMoEMethod` 为范式实现一个新量化方法，并在 `test_moe_backend.py` 补 `can_implement`/`run_moe` 单测。
- **配套阅读**：`AGENTS.md` 里专门指出「read `MOE_DEVELOPER_GUIDE.md` before modifying MoE code」——在动任何 MoE 代码前，把它和 `MOE_SCHEDULER_DESIGN.md` 再过一遍。
