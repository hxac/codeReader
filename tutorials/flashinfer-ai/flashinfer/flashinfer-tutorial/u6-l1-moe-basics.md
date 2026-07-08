# MoE 基础与统一 API（MoEConfig）

> 前置讲义：本讲承接 u5-l1《GEMM 全景与 mm_* API》。我们已经知道 FlashInfer 如何用声明式装饰器（`@backend_requirement` / `@supported_compute_capability`）和「Python wrapper → JIT 模块符号 → csrc → include kernel」的调用链来组织一个 GEMM 算子。MoE（Mixture of Experts，混合专家）可以看作「一组带门控的小 GEMM」，因此 GEMM 单元里的后端选择、低精度、JIT 等概念会全部复用，本讲只在它们之上新增「路由」这一层。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 MoE 一个前向的完整五步流程：**门控（gate）→ 选专家（top-k）→ 分发（dispatch）→ 专家 FFN → 合并（combine）**，并指出哪些步被融合进了 FlashInfer 的 kernel。
- 读懂 `flashinfer/fused_moe/api.py` 里的配置数据类层次：`MoEConfig` 如何由 `RoutingConfig` / `QuantConfig` / `ExpertConfig` / `ActivationConfig` / `BackendOptions` / `ExecutionConfig` 六个子配置组合而成，以及「冻结 dataclass + `**config` 解包 + `repr` 往返」的设计意图。
- 解释 `RoutingInputMode` 的三种取值（`FromLogits` / `PackedPrecomputed` / `UnpackedPrecomputed`），理解它们如何决定「路由是 kernel 现算」还是「外部预算后喂进 kernel」。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

- **MoE（Mixture of Experts，混合专家）**：一种稀疏模型结构。与「一个 FFN 处理所有 token」不同，MoE 内部有 \(E\) 个并行的「专家 FFN」，每个 token 只激活其中 \(k\) 个（\(k \ll E\)）。这样可以在「几乎不增加单 token 计算量」的前提下，把模型总参数量做得很大。DeepSeek-V3、Mixtral、Llama-4 等都用了 MoE。
- **门控 / 路由器（gate / router）**：一个把 token 的隐状态 \(x \in \mathbb{R}^{H}\) 投影成 \(E\) 个分数 \(g \in \mathbb{R}^{E}\) 的小线性层（`x @ W_gate.T`），用来决定「这个 token 该送给哪几个专家」。
- **top-k 选择**：从 \(E\) 个分数里挑出最大的 \(k\) 个，得到「选中的专家编号」`topk_ids` 与「对应的权重」`topk_weights`。
- **专家 FFN**：每个专家就是一个标准的前馈网络，门控 FFN（gated FFN）由两个 GEMM 夹一个激活组成：先 `gemm1` 升维、做 `silu(gate) * up` 这样的门控激活，再 `gemm2` 降维。这部分正是 u5-l1 讲过的 GEMM。
- **分页 / 分发（dispatch）**：把「每个 token 该给哪些专家」落实成「把 token 的数据搬运到对应专家的输入缓冲」。在单卡上是按专家重排，在多卡（专家并行 EP）上是 AlltoAll。
- **合并（combine / finalize）**：把多个专家的输出按 `topk_weights` 加权求和，还原成 `[num_tokens, hidden_size]` 的单一张量。
- **dataclass（数据类）**：Python 用 `@dataclass` 自动生成构造、相等、打印等方法的「纯数据容器」。本讲的配置类大量使用 `frozen=True`（不可变）。
- **`**config` 解包**：Python 的字典解包语法 `f(**d)` 把字典 `d` 的键值当作关键字参数传给 `f`。`MoEConfig` 实现了 `keys()`/`__getitem__` 协议，因此可以直接 `fused_moe(tensors, **config)`。

一句话直觉：**MoE = 门控路由 + 一堆 GEMM + 加权合并**。FlashInfer 的活就是把「路由」与「专家 GEMM + 合并」融合成尽可能少的 kernel launch。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flashinfer/fused_moe/api.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py) | **本讲主角**。统一 API 的全部配置数据类（`MoEConfig` 及六个子配置）与张量打包（`MoEActivationPack` / `MoEWeightPack`）。 |
| [flashinfer/fused_moe/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py) | 旧式扁平参数 API（`cutlass_fused_moe`、`trtllm_*_moe`），以及 `RoutingInputMode` 枚举定义。 |
| [flashinfer/fused_moe/__init__.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/__init__.py) | 包导出，把「统一 API」与「旧式扁平 API」并列暴露。 |
| [flashinfer/fused_moe/fused_routing_dsv3.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/fused_routing_dsv3.py) | DeepSeek-V3 风格的融合路由 kernel `fused_topk_deepseek`（门控 + top-k + 归一化）。 |
| [flashinfer/fused_moe/layer.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/layer.py) | `MoELayer`：状态化、跨后端、带 autotune 的派发器（统一 API 的执行入口）。 |
| [flashinfer/tllm_enums.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/tllm_enums.py) | 跨算子共享的枚举 `RoutingMethodType` / `ActivationType`，是 kernel ABI 的「唯一事实源」。 |
| [tests/moe/test_trtllm_cutlass_fused_moe.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/moe/test_trtllm_cutlass_fused_moe.py) | `cutlass_fused_moe` 的参考实现与测试，含可读的 `compute_routing` / `compute_with_experts`，是理解流程的最佳教材。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**MoE 执行流程**、**配置数据类**、**路由输入模式**。

### 4.1 MoE 的执行流程：gate → topk → dispatch → 专家 FFN → combine

#### 4.1.1 概念说明

一个 MoE 层接收 `[num_tokens, hidden_size]` 的隐状态 \(X\)，输出同形状的张量。它由两部分组成：

- **路由器（router / gate）**：决定「谁去哪个专家」。
- **专家 FFN（expert FFN）**：每个被选中的专家对 token 做一次门控前馈变换。

之所以值得专门做一个 kernel 库，是因为朴素实现里「dispatch → 逐专家 GEMM → combine」会产生大量小 kernel launch 与显存搬运，成为 MoE 推理的主要瓶颈。FlashInfer 把后三步（甚至加上路由）融合成尽可能少的 kernel。

#### 4.1.2 核心流程

设 token 数为 \(M\)、专家数为 \(E\)、每个 token 激活 \(k\) 个专家、专家中间维 \(I\)、隐层维 \(H\)。一次前向的五步：

1. **门控（gate）**：路由器是一个线性层，产出 logits
   \[ g = X W_{\text{gate}}^{\top} \in \mathbb{R}^{M \times E} \]

2. **选专家（top-k + 归一化）**：从 \(g\) 选出每行最大的 \(k\) 个，得到选中专家编号 `topk_ids` \(\in \mathbb{Z}^{M \times k}\) 与权重 `topk_weights` \(\in \mathbb{R}^{M \times k}\)。归一化方式因模型而异（softmax / sigmoid / 重归一化），见 4.1.3 的 `RoutingMethodType`。

3. **分发（dispatch）**：把每个 token 复制 \(k\) 份送到它选中的专家。逻辑上得到 `[M*k, H]` 的「展平 token × 专家」矩阵。

4. **专家 FFN（gemm1 → 激活 → gemm2）**：对选中专家 \(e\)，门控 FFN 为
   \[ y_e = \big(\,\mathrm{act}(X_e W_1^{\top}) \odot (X_e W_3^{\top})\,\big) W_2^{\top} \]
   其中 \(W_1\) 是门控分支、\(W_3\) 是线性分支（二者常打包成 `w31 = [W_3; W_1]`，形状 `[2I, H]`），\(W_2\) 形状 `[H, I]`，\(\mathrm{act}\) 对 SwiGLU 是 `silu`。

5. **合并（combine / finalize）**：把每个 token 的 \(k\) 个专家输出按权重加权求和
   \[ \text{out}[m] = \sum_{i=1}^{k} w_{m,i} \cdot y_{e_{m,i}} \]

FlashInfer 的取舍：第 1～2 步可以独立成一个路由 kernel（如 `fused_topk_deepseek`），也可以**融合进** MoE kernel（`RoutingInputMode.FromLogits`，见 4.3）；第 3～5 步通常融合成一个 kernel（`cutlass_fused_moe` / `trtllm_*_moe`），把 dispatch、两个 GEMM、激活、加权合并都做掉。

#### 4.1.3 源码精读

**路由的参考实现（gate → topk）**。测试里有一个极好读的 `compute_routing`，它就是「softmax → topk → 重归一化」的标准路由：

[tests/moe/test_trtllm_cutlass_fused_moe.py:157-176](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/moe/test_trtllm_cutlass_fused_moe.py#L157-L176) —— 对 router logits 先做 softmax 得到全部 \(E\) 个专家的概率，再 `torch.topk` 取最大的 \(k\) 个，最后对这 \(k\) 个权重重新归一化（除以它们的和）。返回 `(routing_weights, selected_experts)`，正是后续 kernel 需要的 `topk_weights` 与 `topk_ids`。

**路由方式枚举**。`compute_routing` 用的是最朴素的「softmax → topk → renorm」，但不同模型路由方式不同，这些都收敛进共享枚举 `RoutingMethodType`：

[flashinfer/tllm_enums.py:10-22](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/tllm_enums.py#L10-L22) —— 列出 6 种路由方法，例如 `Default = Softmax→TopK`、`DeepSeekV3 = Sigmoid→加偏置→组内 Top2→选 Top4 组→组内 Top8`、`Llama4 = Top1→Sigmoid`。注释里直接写清了每种方法的步骤序列，这是「API 直接说 kernel 的语言」原则的体现——枚举值本身就是 kernel ABI 的整数（`IntEnum`），不另造一套镜像。

**融合路由 kernel（DeepSeek-V3 风格）**。`fused_topk_deepseek` 把上面五步里的「打分 + 选专家 + 归一化」压成单个 kernel：

[flashinfer/fused_moe/fused_routing_dsv3.py:155-170](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/fused_routing_dsv3.py#L155-L170) —— 文档串明确给出 5 步：①算带偏置分数 `sigmoid(scores)+bias`；②按组求每组的「组内 top-2 之和」作为组分数；③取最大的 `topk_group` 个组；④在选中组里按带偏置分数取 `topk` 个专家；⑤把选中专家权重归一化为 `sigmoid_scores / sum(sigmoid_scores) * routed_scaling_factor`。注意它就地写入 `topk_values` / `topk_indices`，返回 `None`——这正是 4.3 里「预算模式」要把这两个张量作为输入喂进 MoE kernel 的来源。

**专家 FFN + 合并的融合 kernel**。`cutlass_fused_moe` 是 dispatch + gemm1 + 激活 + gemm2 + 合并的融合入口，它的签名揭示了 MoE 需要的全部输入：

[flashinfer/fused_moe/core.py:824-856](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L824-L856) —— 关键参数：`input` 是 `[M, H]` 隐状态；`token_selected_experts`（即 `topk_ids`）与 `token_final_scales`（即 `topk_weights`）是**已经算好的路由结果**——也就是说 `cutlass_fused_moe` 本身**不做路由**，只做 dispatch+FFN+combine；`fc1_expert_weights` / `fc2_expert_weights` 是 `[E, 2I, H]` / `[E, H, I]` 的专家权重；`activation_type` 默认 `Swiglu`。文档（行 857 起）说明它把「专家选择 + 专家计算 + 输出合并合成单次操作」。

**测试如何串起整条链**。`test_moe` 把「参考路由 + 参考专家计算」与「`cutlass_fused_moe`」对拍：

[tests/moe/test_trtllm_cutlass_fused_moe.py:424-447](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/moe/test_trtllm_cutlass_fused_moe.py#L424-L447) —— 先用 `compute_routing(router_logits, top_k)` 算出 `routing_weights`、`selected_experts`，再喂给 `cutlass_fused_moe`，最后与逐专家循环的参考实现 `compute_with_experts` 用 `rtol=1e-2, atol=1e-2` 对拍。这段是理解「路由在外、FFN+合并融合在内」最直接的一手材料。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：用一张图把 MoE 前向的五步对应到 FlashInfer 的具体函数。
2. **步骤**：
   - 打开 `tests/moe/test_trtllm_cutlass_fused_moe.py`，读 `compute_routing`（行 157）与 `compute_with_experts`（行 312 附近），理解 gate→topk 与逐专家 FFN+合并的朴素实现。
   - 打开 `flashinfer/fused_moe/core.py` 的 `cutlass_foused_moe` 签名（行 824），确认它接收的 `token_selected_experts` / `token_final_scales` 正是 `compute_routing` 的两个返回值。
3. **观察**：你会看到「路由」与「FFN+合并」是**解耦**的——`cutlass_fused_moe` 不接受 `router_logits`，只接受已经选好的专家与权重。
4. **预期结果**：能画出 `router_logits →[compute_routing / fused_topk_deepseek] topk_ids, topk_weights →[cutlass_fused_moe] output` 这条数据流。
5. 运行结果「待本地验证」（需要 SM89+ 的 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `top_k` 从 8 改成 1，MoE 退化成什么？  
**答案**：每个 token 只激活 1 个专家，加权合并退化为「直接取该专家输出乘以 1（权重归一化后为 1）」，等价于一个「按 token 动态选择 FFN」的稀疏结构，没有加权求和。

**练习 2**：`cutlass_fused_moe` 为什么不内置路由，而要外部传入 `topk_ids` / `topk_weights`？  
**答案**：路由方式因模型差异巨大（softmax/sigmoid/分组，见 `RoutingMethodType` 的 6 种），把它与「FFN+合并」解耦后，同一个高效 FFN kernel 可以搭配任意路由方式；同时也支持「预算好路由再喂进来」的场景（如外部已有路由结果、专家并行的 AlltoAll 之后），这正是 4.3 的 `RoutingInputMode`。

### 4.2 配置数据类：MoEConfig 与子配置

#### 4.2.1 概念说明

FlashInfer 同时存在两套 MoE 入口：

- **旧式扁平 API**（`core.py`）：如 `trtllm_fp4_block_scale_moe(routing_logits, ..., 30+ 位置参数)`，后端由函数名硬编码，参数一多就极易出错（第 18 个参数类型错只能等 C++ 段错误才发现）。
- **统一 API**（`api.py`，2026 年新增）：用**冻结的 dataclass** 把配置组织成树状结构，把「配置（纯数据）」与「数据（运行期张量）」分离，支持 `**config` 解包、`repr` 往返序列化，并让后端选择从函数体里抽出来变成声明式。

本模块只讲配置层（`api.py`），不涉及具体后端 kernel（那是 u6-l2 的事）。

#### 4.2.2 核心流程

`MoEConfig` 是顶层容器，由六个各管一摊的子配置组合：

| 子配置 | 管什么 | 关键字段 |
| --- | --- | --- |
| `RoutingConfig` | 路由 | `num_experts`, `top_k`, `method`, `n_group`, `topk_group`, `routed_scaling_factor` |
| `QuantConfig` | 量化方案 | `variant`（一个旋钮统管 dtype+粒度+scale 约定）, `swizzled_scale_factors`, `per_token_scale` |
| `ExpertConfig` | 专家几何 | `intermediate_size`, `local_expert_offset`, `local_num_experts`（专家并行分片） |
| `ActivationConfig` | gemm1/gemm2 之间的融合激活 | `type`（Swiglu/Geglu/Relu2/Identity） |
| `BackendOptions` | 后端候选（有序列表） | `candidates`（按序尝试，autotuner/heuristic 选优） |
| `ExecutionConfig` | 运行期执行参数 | `do_finalize`, `enable_pdl`, `tune_max_num_tokens` |

三条贯穿性设计原则：

1. **冻结不可变**：所有配置类都是 `@dataclass(frozen=True)`，要变体用 `dataclasses.replace(cfg, quant=...)`，永不原地改。
2. **`**config` 解包协议**：`MoEConfig` 实现 `keys()` 与 `__getitem__`，因此 `fused_moe(tensors, **config)` 与 `MoELayer(**config)` 都成立——同一份配置既能喂给无状态的 eager 函数，也能喂给有状态的 `MoELayer`。
3. **`repr` 往返可序列化**：`repr(config)` 直接还原成合法的构造语法（依赖冻结 dataclass + 枚举的 eval-safe `__repr__`），用于 repro 日志。注释里明确说「故意不随包发反序列器」，因为 eval 解析是安全坏味道，留待 repro 工具一起落地。

后端候选用 `BackendOptions` 而非单个后端：每个后端配置（`CutlassConfig`、`TrtllmFp4Config` 等）用类方法 `supported(arch)` 声明自己的硬件前提，`BackendOptions.valid_for(arch)` 过滤出当前 GPU 能跑的候选。

#### 4.2.3 源码精读

**顶层容器 `MoEConfig`**：

[flashinfer/fused_moe/api.py:433-476](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L433-L476) —— 组合六个子配置；`keys()` 返回字段名生成器、`__getitem__` 转发到 `getattr`，二者合起来实现 `**config` 解包（行 460-466）。文档注释（行 441-448）给了典型用法：构造一个 DeepSeek 风格 config 后 `fused_moe(tensors, **config)`。注释（行 468-476）解释了序列化策略。

**路由配置 `RoutingConfig`**：

[flashinfer/fused_moe/api.py:73-110](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L73-L110) —— 必填 `num_experts` / `top_k`，可选 `method`（默认 `RoutingMethodType.Default`）以及 DeepSeek-V3 专用的 `n_group` / `topk_group` / `routed_scaling_factor`。自定义 `__repr__`（行 100-110）只打印非默认字段，保证 `repr` 简洁可往返。

**专家几何 `ExpertConfig`**：

[flashinfer/fused_moe/api.py:164-188](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L164-L188) —— `intermediate_size` 是专家 FFN 的中间维（gemm1 的 \(N\)）；`local_expert_offset` 与 `local_num_experts` 表达专家并行（EP）分片——本 rank 只负责全局专家里的一个连续段。

**量化配置 `QuantConfig` 与 `QuantVariant`**：

[flashinfer/fused_moe/api.py:53-65](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L53-L65) —— `QuantVariant` 是「dtype + 粒度 + scale 约定」的单一旋钮（`BF16`/`FP8PerTensor`/`DeepSeekFp8`/`MxFp8`/`NVFP4`/`MXFP4`/`MxInt4`），避免用三个独立字段拼出非法组合。它与 u5 单元讲过的 FP8/FP4 格式一一对应（见 u5-l2/u5-l3/u5-l5）。

[flashinfer/fused_moe/api.py:113-135](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L113-L135) —— `QuantConfig` 在 `variant` 之外还有 `swizzled_scale_factors` / `per_token_scale` 两个布尔（`None` 表示走后端默认）。注释（行 122-127）解释为何用布尔而非更细的 `SfLayout` 枚举：`SfLayout` 没有 eval-safe `__repr__`，暴露它会破坏 `repr` 往返——这是「可序列化」约束反向作用于 API 设计的典型例子。

**激活配置 `ActivationConfig`**：

[flashinfer/fused_moe/api.py:138-161](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L138-L161) —— 直接复用共享枚举 `ActivationType`（`tllm_enums.py:40-51`，含 `Swiglu`/`Geglu`/`Relu2`/`Identity` 等），并预留了 `swiglu`/`geglu`/`relu2`/`identity` 四个单例方便书写；`is_gated` 属性转发到 `ActivationType.is_gated`。

**后端候选与硬件前提**：

[flashinfer/fused_moe/api.py:398-430](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L398-L430) —— `BackendOptions.valid_for(arch)` 用每个后端的 `supported(arch)` 过滤；`_DEFAULT_BACKEND`（行 420-430）给出默认搜索顺序：`TrtllmFp4 → TrtllmFp8Block → TrtllmFp8PerTensor → TrtllmBf16 → TrtllmMxInt4 → Cutlass → CuteDsl`。

[flashinfer/fused_moe/api.py:317-326](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L317-L326) —— `CutlassConfig.supported` 恒返回 `True`，是「万能兜底」；对照 [flashinfer/fused_moe/api.py:229-235](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/api.py#L229-L235) 的 `TrtllmFp4Config.supported` 要求 `arch >= 100`（Blackwell）。这种「每个后端自报家门」的声明式架构门控，与 u5-l1 / u3-l5 讲过的 `@backend_requirement` 同源。

**配置如何驱动 `MoELayer`**：

[flashinfer/fused_moe/layer.py:80-110](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/layer.py#L80-L110) —— `MoELayer.__init__` 遍历 `config.backend` 里每个候选，用 `supported(arch)` 过滤硬件，再查 `_BACKEND_RUNNERS` 表拿到对应的 runner 类实例化；若候选为空就抛错。这把「后端选择」从函数体里彻底抽到了配置 + 声明表。

#### 4.2.4 代码实践（无需 GPU，纯配置）

1. **目标**：构造一个最小 `MoEConfig`，验证 `**config` 解包与 `repr` 往返。
2. **步骤**（纯 Python，不需要 CUDA）：

```python
# 示例代码：仅演示配置构造，不触发任何 kernel
import dataclasses
from flashinfer.fused_moe import (
    MoEConfig, RoutingConfig, QuantConfig, QuantVariant,
    ExpertConfig, ActivationConfig, CutlassConfig, BackendOptions,
)
from flashinfer.tllm_enums import RoutingMethodType

cfg = MoEConfig(
    routing=RoutingConfig(num_experts=8, top_k=2),       # 少量专家
    quant=QuantConfig(variant=QuantVariant.BF16),        # BF16
    experts=ExpertConfig(intermediate_size=64),
)
print(cfg)                                              # 看 repr
print(dict(**cfg)["routing"].top_k)                     # 解包后取值
bf16_keys = [k for k in cfg.keys()]                     # keys() 协议
```

3. **观察**：`print(cfg)` 输出形如 `MoEConfig(routing=RoutingConfig(num_experts=8, top_k=2), quant=QuantConfig(...), experts=ExpertConfig(...))`，`method` 因取默认值被省略——这正是自定义 `__repr__` 的效果。
4. **预期结果**：`dict(**cfg)` 能成功解包成含 `routing/quant/experts/activation/backend/execution` 六个键的字典，说明 `keys()`/`__getitem__` 协议生效。
5. 若 `flashinfer` 未安装，则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MoEConfig` 要实现 `keys()` 和 `__getitem__`，而不是直接继承 `dict`？  
**答案**：因为它是冻结的 `dataclass`，继承 `dict` 会破坏不可变性，也无法享受 dataclass 的 `field` 默认值与 `repr` 生成。实现映射协议（`keys` + `__getitem__`）是让「冻结对象」支持 `**` 解包的标准 Python 惯用法（`OrderedDict`/`Mapping` 协议）。

**练习 2**：想从一个 BF16 config 派生出 FP8 版本，应该怎么改？  
**答案**：用 `dataclasses.replace(cfg, quant=QuantConfig(variant=QuantVariant.DeepSeekFp8))`，绝不在原对象上改字段（冻结类也改不了）。这是「不可变 + replace 派生」的设计意图。

**练习 3**：`CutlassConfig.supported(arch)` 恒返回 `True` 有什么意义？  
**答案**：它是 `BackendOptions` 候选列表里的「万能兜底」——当所有高性能专用后端（要求 Blackwell 等）都因架构不满足被 `valid_for` 过滤掉时，`CutlassConfig` 仍能用，保证 `MoELayer` 至少有一个可跑的后端。

### 4.3 路由输入模式：RoutingInputMode

#### 4.3.1 概念说明

`RoutingInputMode` 回答一个关键问题：**路由（选专家 + 算权重）到底由谁做？** 是 MoE kernel 自己从 logits 现算，还是外部预算好之后再喂进 kernel？这是旧式扁平 API（`trtllm_*_moe`）层面的一个旋钮，它直接决定 `topk_ids` / `topk_weights` 这两个张量是 kernel 的**输入**还是**输出**。

#### 4.3.2 核心流程

三种模式：

| 模式 | 值 | 路由谁做 | `topk_ids` | `topk_weights` |
| --- | --- | --- | --- | --- |
| `FromLogits` | 0 | **kernel 内部现算**（输入 `routing_logits`） | **输出**（写回专家编号） | **输出**（写回权重） |
| `PackedPrecomputed` | 1 | 外部预算，**打包**成一个张量 | **输入**（高 16 位专家号 \| 低 16 位权重） | **输出**（解包出的权重） |
| `UnpackedPrecomputed` | 2 | 外部预算，**分离**两个张量 | **输入**（专家编号 int32） | **输入**（权重） |

直觉：

- `FromLogits` 把路由融合进 MoE kernel，省一次 launch，但路由方式被 kernel 内部写死（如 trtllm-gen 的内置 softmax-topk）。
- `PackedPrecomputed` / `UnpackedPrecomputed` 都表示「我已经在外面把路由算好了」（比如用 4.1 的 `fused_topk_deepseek` 或外部框架的路由），kernel 只负责 dispatch+FFN+combine。两者差别仅在「专家号和权重是打包成一个张量，还是分开两个张量」。打包格式 `(expert_id << 16) | weight` 可省一次全局访存、契合某些 kernel 的内部表示。

#### 4.3.3 源码精读

**枚举定义**：

[flashinfer/fused_moe/core.py:82-101](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L82-L101) —— 三种模式的逐字定义，注释写明每种模式下 `topk_ids` / `topk_weights` 是输入还是输出。其中 `PackedPrecomputed`（行 90-96）注释给出打包格式：高 16 位是 `int16` 专家号、低 16 位是 `float16/bfloat16` 权重，对应 C++ 侧 `PackedScoreIdx`。文件顶部注释（行 83）提醒「要与 `csrc/trtllm_fused_moe_kernel_launcher.cu` 里的对应定义保持同步」——这是跨语言 ABI 的典型约束。

**Python 侧如何自动选模式**：

[flashinfer/fused_moe/core.py:4170-4178](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L4170-L4178) —— 当用户传进来的 `topk_ids` 是一个「二元组 `(topk_ids, topk_weights)`」时，判定为分离格式，选 `UnpackedPrecomputed`；否则当作「打包的单张量」，选 `PackedPrecomputed`。这是一个用「参数是 tuple 还是不是 tuple」来隐式区分模式的便利设计。

**`FromLogits` 的默认用法**：

[flashinfer/fused_moe/core.py:3984-3990](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L3984-L3990) —— 当只提供 `routing_logits`、不提供预算结果时，传 `RoutingInputMode.FromLogits`，此时 `topk_ids` / `topk_weights` 传 `None`（因为它们是输出，由 kernel 内部分配/写回）。

**`UnpackedPrecomputed` 的 workspace 约束**：

[flashinfer/fused_moe/core.py:2348-2354](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/fused_moe/core.py#L2348-L2354) —— 注释指出对 Mode 3（`UnpackedPrecomputed`），`topk_ids` / `topk_weights` 是用户提供的**输入**，因此必须非空（有 `assert`）。这从侧面印证「输入/输出角色」对调用者的实际影响。

> 说明：`RoutingInputMode` 目前只在**旧式扁平 API**（`trtllm_*_moe`）这一层显式出现；统一 API（`api.py` 的 `MoEConfig`）通过「你是否提供 `MoEActivationPack.selected_experts` / `final_scales`」隐式表达了同样的「现算 vs 预算」抉择，把旋钮收敛进了数据打包。理解 `RoutingInputMode` 有助于看清统一 API 的设计动机。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：用 `RoutingInputMode` 三种模式，把「谁做路由」这件事讲透。
2. **步骤**：
   - 在 `core.py` 中检索 `routing_input_mode` 的所有出现（见上面三处），分别对应「默认现算」「自动判断打包/分离」「分离模式断言」。
   - 对照 4.1 的 `compute_routing`，理解「外部预算」对应的就是它在 Python 里算出 `selected_experts` / `routing_weights` 后，以 `UnpackedPrecomputed` 的精神喂给 kernel（`cutlass_fused_moe` 本身就接收分离的 `token_selected_experts` / `token_final_scales`，等价于分离模式）。
3. **观察**：`FromLogits` 让 kernel 一站式做完路由+FFN+合并；两种 `*Precomputed` 把路由前置，换取路由算法的灵活性。
4. **预期结果**：能说清「`cutlass_fused_moe` 的 `token_selected_experts` + `token_final_scales` 两个参数，在 `RoutingInputMode` 语境下就是 `UnpackedPrecomputed` 模式的两个输入张量」。
5. 实际运行「待本地验证」（需要 GPU 与编译好的 trtllm-gen 模块）。

#### 4.3.5 小练习与答案

**练习 1**：什么场景下你会优先选 `FromLogits`，什么场景选 `UnpackedPrecomputed`？  
**答案**：当模型路由方式恰好是 kernel 内置的那种（如标准 softmax-topk）、且想省一次 launch 时，选 `FromLogits`；当路由方式更复杂（DeepSeek-V3 分组、Llama-4、或带偏置/auxiliary-free），或路由结果来自外部框架/专家并行 AlltoAll 之后，选 `UnpackedPrecomputed`，把路由与 FFN 解耦。

**练习 2**：`PackedPrecomputed` 的打包格式 `(expert_id << 16) | weight` 为什么把专家号放高 16 位、权重放低 16 位？  
**答案**：专家编号是非负整数（`int16` 范围足够覆盖几百个专家），天然适合占高位；权重是小浮点数，放低 16 位可直接当 `float16/bfloat16` 解读。这样一次访存同时取到「去哪个专家」和「权重多大」，契合 kernel 内部按 token 遍历的访存模式，减少一次全局内存往返。

**练习 3**：为什么枚举定义上方注释要写「请与 `csrc/trtllm_fused_moe_kernel_launcher.cu` 保持同步」？  
**答案**：`RoutingInputMode` 的整数值会被原样传过 TVM-FFI 边界、当作 C++ kernel launcher 的参数（它就是 kernel ABI 的 int）。一旦 Python 与 C++ 两侧的整数含义不一致（例如顺序调换），kernel 就会用错模式去解释张量、产生静默错误。这是跨语言 ABI 必须人工保持同步的典型约束（也是 u9-l2 TVM-FFI 讲义会展开的话题）。

## 5. 综合实践

把三个最小模块串起来：**用 `MoEConfig` 描述一个最小 BF16 MoE，用 `fused_topk_deepseek` 或 `compute_routing` 思路做路由，再用 `cutlass_fused_moe` 跑一次前向**。

任务拆解：

1. **用统一 API 描述配置**（无需 GPU）：构造
   ```python
   cfg = MoEConfig(
       routing=RoutingConfig(num_experts=8, top_k=2),
       quant=QuantConfig(variant=QuantVariant.BF16),
       experts=ExpertConfig(intermediate_size=64),
       activation=ActivationConfig.swiglu,
   )
   ```
   从 `cfg` 读出 `num_experts=8`、`top_k=2`、`intermediate_size=64`、BF16，这些值将决定下面张量的形状与 dtype。

2. **准备数据**（对应 `RoutingInputMode.UnpackedPrecomputed` 精神）：仿照 [tests/moe/test_trtllm_cutlass_fused_moe.py:403-445](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/moe/test_trtllm_cutlass_fused_moe.py#L403-L445)，造 `x=[M,H]`、`router_logits=[M,E]`、`w31_weight=[E,2I,H]`、`w2_weight=[E,H,I]`，用 `compute_routing(router_logits, top_k)` 算出 `routing_weights`、`selected_experts`。

3. **跑融合 kernel**：调用 `cutlass_fused_moe(x, selected_experts.to(int), routing_weights, w31_weight, w2_weight, output_dtype, quant_scales=None)`，它与参考实现对拍（参考 `test_moe`，行 424-447）。

4. **解释 `RoutingInputMode`**：在第 3 步里，你把分离的 `selected_experts` / `routing_weights` 喂给 kernel——这正是 `UnpackedPrecomputed` 模式；如果你改为只传 `routing_logits` 让 kernel 自己选专家，那就是 `FromLogits`。

**注意**：第 2～3 步需要 SM89+（Ada/Hopper/Blackwell）的 GPU 并完成 JIT 编译，运行耗时与正确性「待本地验证」。第 1 步纯配置可在任意环境完成。完成本任务后，你应当能回答：「`MoEConfig` 描述了什么、`cutlass_fused_moe` 消费了什么、`RoutingInputMode` 在两者之间起了什么作用。」

## 6. 本讲小结

- MoE 一次前向是 **gate → topk → dispatch → 专家 FFN（gemm1+激活+gemm2）→ combine** 五步；FlashInfer 把后三步（甚至含路由）融合成极少 kernel，路由（`fused_topk_deepseek`）与 FFN+合并（`cutlass_fused_moe`）通常解耦。
- `cutlass_fused_moe` 是**旧式扁平 API**，接收**已算好的** `token_selected_experts` / `token_final_scales`，自己只做 dispatch+FFN+combine；它支持 SM89～SM121。
- 路由方式有 6 种，收敛在共享枚举 `RoutingMethodType`（`tllm_enums.py`），枚举值即 kernel ABI 整数，是「唯一事实源」。
- **统一 API**（`api.py`）用冻结 dataclass 把配置组织成 `MoEConfig = routing + quant + experts + activation + backend + execution`；支持 `**config` 解包（映射协议）、`repr` 往返、`dataclasses.replace` 派生。
- 后端用 `BackendOptions`（有序候选）+ 每个后端的 `supported(arch)` 声明硬件前提，`CutlassConfig` 是恒可用的兜底；`MoELayer` 据此实例化 runner。
- `RoutingInputMode` 三种取值（`FromLogits` / `PackedPrecomputed` / `UnpackedPrecomputed`）决定「路由是 kernel 现算还是外部预算」，并决定 `topk_ids` / `topk_weights` 是输入还是输出；统一 API 把这个旋钮隐式收敛进了 `MoEActivationPack`。

## 7. 下一步学习建议

- **u6-l2《融合 MoE 后端（cutlass/trtllm）》**：深入 `core.py` 的 `cutlass_fused_moe` / `trtllm_*_moe` 各后端实现与 JIT 生成（`gen_cutlass_fused_moe_sm*_module`、`gen_trtllm_gen_fused_moe_sm100_module`），看它们如何按 SM 版本选 gen 函数。
- **u6-l3《路由方法（DeepSeek-V3/Llama-4/top-k）》**：精读 `fused_routing_dsv3.py` 与 `moe_utils` 的 permute/sort，把本讲略过的 `n_group`/`topk_group` 路由细节补全。
- **u6-l4《量化 MoE 与 MoELayer 派发》**：看 `MoELayer` 如何跨 `CuteDslNvfp4Runner` / `TrtllmFp4RoutedRunner` / `TrtllmBf16RoutedRunner` 做 autotune 选优（`runners.py`、`layer.py`）。
- 若想横向对照「配置驱动后端选择」这一模式，可回看 u5-l1 的 `@backend_requirement` 与 u3-l5 的 `determine_attention_backend`——它们与本讲的 `BackendOptions` + `supported(arch)` 是同一思想的两种表达。
