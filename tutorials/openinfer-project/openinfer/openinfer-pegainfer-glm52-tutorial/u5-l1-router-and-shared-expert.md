# u5-l1 路由器 noaux_tc 与共享专家

## 1. 本讲目标

GLM5.2 有 78 层，其中前 3 层是 dense MLP，后 75 层是 MoE（Mixture of Experts，混合专家）层。本讲拆开一个 MoE 层里「不发生跨 GPU 通信」的那一半：

- 看懂 `topk_method=noaux_tc` 路由的完整数学与 CUDA kernel：sigmoid 打分 → 偏置校正选 top-8 → `norm_topk_prob` 归一化 → `routed_scaling_factor=2.5` 何时折进权重、何时延后施加。
- 看懂共享专家（shared expert）：它是一个普通的 fp8 SwiGLU MLP，每个 rank 全量持有、就地执行，并在 decode 时分叉到 aux 流与 DeepEP 集合通信重叠。
- 掌握 `Glm52MoeExpertBank` 的布局：W13/W2 形状常量、checkpoint 128×128 f32 块 scale 到 DeepGEMM UE8M0 布局的转换、以及 256 个专家按 rank 的分片。

学完本讲，你应该能独立回答：一个 token 的 hidden 状态如何变成 8 个「专家 id + 归一化权重」对，这 64 字节路由信息如何决定每个 rank 收到多少行，以及为什么这些计数永远不需要回读主机。

本讲是 u5 单元（MoE 子系统）的第一篇：只讲路由器、共享专家和专家库的数据结构；DeepEP dispatch/combine 的完整链路留给 u5-l2。

## 2. 前置知识

- **MoE 是什么**：把一个大的 FFN 换成 N 个小「专家」FFN 加一个路由器。每个 token 只激活其中 k 个专家，总计算量远小于激活全部专家。GLM5.2 的配置是 N=256 个路由专家 + 1 个共享专家，k=8。
- **noaux_tc**：`no auxiliary loss, top-k with correction`。DeepSeek-V3 一系的路由方案——训练时不加辅助负载均衡损失，而是用一个可学习的偏置向量 `e_score_correction_bias` 在推理（和训练路由）时校正打分。**偏置只决定「选谁」，不进入权重**，这是本讲 kernel 里最容易被忽略的细节。
- **sigmoid 打分与 norm_topk_prob**：路由分数取 `sigmoid(logit)`（不是 softmax）；选出 top-8 后，把 8 个 sigmoid 分数归一化（和为 1）作为组合权重，即 `norm_topk_prob=true`。
- **routed_scaling_factor = 2.5**：最终路由输出在残差相加前的整体缩放。
- **承接 u3-l1**：共享专家复用的 `ProjWeight`、`pack_proj_pair`（gate|up 打包成一个 `[2*n, k]` 投影）、`fp8_mlp_into`（一次 GEMV + SwiGLU + 一次 GEMV）都在那一讲精读过，本讲直接引用。
- **承接 u2-l2**：256 个路由专家按 `[rL, (r+1)L)` 区间切给各 rank，loader 在 H2D 时按步长常量直接落位成「region」——本讲的 `Glm52MoeExpertBank::from_regions` 就是这些 region 的消费端。
- **承接 u7-l1（可后置）**：free-running 纪律——引擎永不静默等待，padding 行的字节会到达对端，所以空输入也必须给确定性的零值缓冲。

## 3. 本讲源码地图

| 文件 | 职责（本讲视角） |
| --- | --- |
| `pegainfer-glm52/src/moe_decode.rs` | **主角**。路由器权重/scratch、共享专家、专家库 `Glm52MoeExpertBank`、EP 与 rows 两条路由入口 |
| `pegainfer-glm52/src/moe_ep8.rs` | 一个 EP MoE 层的权重三件套打包：router + shared + bank |
| `pegainfer-glm52/src/config.rs` | 路由相关常量（256/8/1/2.5）与 `probe_config_json` 对这些字段的 fail-fast 校验 |
| `pegainfer-glm52/src/weights.rs` | checkpoint 张量契约：router 的 `gate.weight` / `e_score_correction_bias` |
| `pegainfer-glm52/src/model/build.rs` | 逐层组装：EP 层如何从 loader 拿 router/shared/bank 三份权重 |
| `pegainfer-glm52/src/model/step_body.rs` | decode 时一个 MoE 层的执行编排：aux 流分叉 + ×2.5 汇合 |
| `pegainfer-glm52/src/fp8.rs` | u3-l1 已讲：`pack_proj_pair` / `fp8_mlp_into`（共享专家的前向引擎） |
| `pegainfer-glm52/src/moe_ep.rs` | 路由结果的消费端：dispatch → `psum_expert` → 元数据（本讲只取头部图示与 psum 消费点） |
| `pegainfer-kernels/src/ops/glm52/router.rs` | Rust 侧启动包装与 `Glm52RouterConfig`（scale 语义在此定义） |
| `pegainfer-kernels/csrc/glm52/glm52_router.cu` | **kernel 本体**：sigmoid + 偏置 + 秩计数 top-k + 归一化 |
| `pegainfer-kernels/src/ops/deepep.rs` | `psum_expert` 的类型与文档（设备端前缀和） |
| `pegainfer-kernels/csrc/deepep/deepep_config_glm52.cuh` | EP8 shim 常量：32 本地专家、`expert_alignment=64` |
| `pegainfer-kernels/tests/glm52_router_smoke.rs` | 路由 kernel 的设备门测试（本讲实践锚点） |

## 4. 核心概念与源码讲解

### 4.1 router config 与 noaux_tc kernel

#### 4.1.1 概念说明

路由器是 MoE 层的「分诊台」：输入是一个 token 的 norm 后 hidden（6144 维 bf16），输出是 8 个「全局专家 id + 归一化权重」。它本身非常轻——一个 `[256, 6144]` 的 bf16 门矩阵加一个 256 维 f32 偏置，总共约 3 MiB，78 层每层一份、**每个 rank 全量复制**（因为每个 rank 都要为自己的 token 算路由）。

GLM5.2 的路由配置在 config.json 里被 `probe_config_json` 钉死：`n_routed_experts=256`、`num_experts_per_tok=8`、`n_shared_experts=1`、`n_group=1`、`topk_group=1`、`topk_method=noaux_tc`、`scoring_func=sigmoid`、`norm_topk_prob=true`、`routed_scaling_factor=2.5`。注意 `n_group=1` 意味着 DeepSeek 式的「先选组再选专家」两级选择被退化掉了——只剩一步全局 top-8。

#### 4.1.2 核心流程

一个 token 的路由分四步（编号对应 4.1.3 的源码位置）：

1. **打分 GEMV**：`logits[e] = ⟨gate_weight[e], hidden⟩`，得到 256 维 f32。
2. **sigmoid + 偏置校正**：

   \[ s_e = \sigma(\text{logit}_e) = \frac{1}{1+e^{-\text{logit}_e}}, \qquad c_e = s_e + b_e \]

   \( s_e \) 是用于归一化的分数，\( c_e \) 是用于**排序**的选择分数，\( b_e \) 是 `e_score_correction_bias`。
3. **确定性 top-8**：按 \( (c_e \text{ 降序}, e \text{ 升序}) \) 的全序取前 8。平局用专家 id 决出，因此结果是确定性的（CUDA Graph 重放语义要求）。
4. **归一化加权**：

   \[ w_i = \frac{s_i}{\sum_{j \in \text{top8}} s_j} \cdot \gamma, \qquad \gamma = \begin{cases} 1.0 & \text{EP decode（}\texttt{glm52\_unscaled}\text{）} \\ 2.5 & \text{TP prefill（}\texttt{glm52}\text{）} \end{cases} \]

   分子是**不带偏置**的 sigmoid 分数 \( s_i \)——偏置只改变选中的集合，不改变权重比例。

EP 与 TP 的 scale 策略差异是一条关键分流：

- **EP decode** 用 `route_scale = 1.0`，即 topk_weight 只是归一化权重；2.5 在 combine 之后由收尾加法一次性施加（见 4.2）。
- **TP prefill rows**（`run_router_rows_into`）用 `route_scale = 2.5`，直接折进 topk_weight。

为什么 EP 不折？因为 DeepEP 的 combine 只做逐槽求和，把 scale 留在收尾加法可以让「路由权重 × 专家输出」与「共享专家输出」在同一次加法里合并成 `2.5·Σ + shared`，少一次逐元素乘法，也与 vLLM 的 EP 实现对齐。

#### 4.1.3 源码精读

**常量与探针**。路由的四个数字是编译期常量，且 `probe_config_json` 逐字段核对 checkpoint：

- [src/config.rs:L41-L46](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L41-L46)：`GLM52_EXPERT_INTERMEDIATE=2048`、`GLM52_ROUTED_EXPERTS=256`、`GLM52_TOPK=8`、`GLM52_SHARED_EXPERTS=1`、`GLM52_ROUTED_SCALING_FACTOR=2.5`。
- [src/config.rs:L129-L151](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L129-L151)：探针校验 `n_routed_experts`、`num_experts_per_tok`、`n_shared_experts`、`n_group=1`、`topk_group=1`、`topk_method="noaux_tc"`、`scoring_func="sigmoid"`、`norm_topk_prob=true`、`routed_scaling_factor≈2.5`——任何一项不符即拒绝启动。

**路由权重与 checkpoint 契约**。router 权重以原始字节驻留，构造时只校验字节数：

- [src/moe_decode.rs:L52-L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L52-L75)：`Glm52MoeRouterWeights` 持有 `gate_weight`（bf16 `[256, 6144]`）和 `e_score_bias`（f32 字节 `[256]`），`ensure!` 钉死 `256*6144*2` 与 `256*4` 字节。
- [src/weights.rs:L603-L609](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L603-L609)：张量契约按后缀名匹配 `.mlp.gate.weight`（BF16 `[256,6144]`）与 `.mlp.gate.e_score_correction_bias`（F32 `[256]`），是 u2-l2 装载三关校验的第一关。

**路由输出与 scratch**。路由结果永不回读主机：

- [src/moe_decode.rs:L281-L296](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L281-L296)：`RoutedTopk { topk_idx: i32, topk_weight: f32 }`——注释明确「device-resident (never read back to host)」，topk_idx 是**全局**专家 id；`Glm52RouterScratch` 持有 logits（`capacity*256` f32）与 route 两个缓冲。
- [src/moe_decode.rs:L298-L314](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L298-L314)：容量取 `tokens.max(GLM52_TP_TOKENS)`（TP 路径的 8 行 ABI），指针在启动期一次分配、步内原地覆写——这是整步 CUDA Graph 捕获的前提（u6-l3）。

**两条入口与 scale 语义**：

- [src/moe_decode.rs:L316-L356](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L316-L356)：`run_ep_router_into` 是 decode 入口，固定用 `Glm52RouterConfig::glm52_unscaled()`（scale=1.0），并把 `active_tokens == padded_tokens == s.tokens` 传给 kernel。
- [src/moe_decode.rs:L358-L402](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L358-L402)：`run_router_rows_into` 是 prefill rows 入口（TP prefill 使用）：先用 `gemm_bf16_f32` 算 padded 行的 logits（行数可达万级，GEMV 不划算），再调 `glm52_router_select_launch` 且配置是 `Glm52RouterConfig::glm52()`（scale=2.5 折进权重）。`active < padded` 时 padding 行只算 logits、不产生路由（「select 静默」）。
- [pegainfer-kernels/src/ops/glm52/router.rs:L15-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/router.rs#L15-L18)：注释原文写明了两条路径的 scale 约定；[L29-L45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/router.rs#L29-L45) 是 `glm52()` / `glm52_unscaled()` 两个 const 构造器，[L47-L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/router.rs#L47-L68) 的 `validate()` 把 hidden/专家数/topk 钉死为 6144/256/8——**路由形状在这个 crate 里没有自由度**。

**kernel 本体**（一个 block 处理一个 token，256 线程每线程一个专家）：

- [pegainfer-kernels/csrc/glm52/glm52_router.cu:L38-L46](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L38-L46)：每个专家线程算 `score = 1/(1+expf(-logit))`（sigmoid）与 `choice_scores = score + bias`；`token >= active_tokens` 的 padding 行在 `__syncthreads()` 前返回——**select 静默**，padding 行不写任何 topk 输出。
- [pegainfer-kernels/csrc/glm52/glm52_router.cu:L16-L20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L16-L20)：`better_router_choice` 定义严格全序（值降序、id 升序）。注释（[L49-L58](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L49-L58)）指出 sigmoid∈(0,1) 加有限偏置使 `choice_scores` 恒有限，NaN 无法破坏排序，且每个专家的「秩」（严格更优者的个数）与逐轮 argmax 的挑选顺序一一对应——所以单遍秩计数与旧的 8 轮掩码归约**逐位一致**，同时把每调用从 12.2µs 降到 8.2µs（ncu 实测，注释原文）。
- [pegainfer-kernels/csrc/glm52/glm52_router.cu:L59-L69](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L59-L69)：秩计数选择——线程统计自己被多少专家严格压过，`rank < topk` 即入选，写入 `topk_idx[token*topk + rank]`（输出位置=秩，天然降序），并记下**不带偏置**的 `scores[expert]`。
- [pegainfer-kernels/csrc/glm52/glm52_router.cu:L72-L79](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L72-L79)：线程 0 求 `selected_sum = Σ selected_scores`，`topk_weight[i] = selected_scores[i] * (route_scale / selected_sum)`——正是 4.1.2 的公式；`selected_sum==0` 时 scale 取 0（防御 NaN 输入）。
- [pegainfer-kernels/csrc/glm52/glm52_router.cu:L103-L135](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L103-L135)：`glm52_router_noaux_tc_cuda` 入口——先跑 min-GEMV 打分（[L87-L97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L87-L97)，为 `[256,6144]` 专门实例化的低延迟 kernel），再启动选择 kernel；`glm52_router_select_cuda`（[L137-L155](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L137-L155)）是只做选择的后半段，供已算好 logits 的 prefill rows 复用。

#### 4.1.4 代码实践

**实践 A（有 GPU）：运行路由设备门测试。**

1. 实践目标：验证「打分 GEMV + noaux_tc 选择」的数值契约在真实 GPU 上成立。
2. 操作步骤：

   ```bash
   cargo test --release -p pegainfer-kernels --features glm52 \
     --test glm52_router_smoke -- --nocapture
   ```

3. 需要观察的现象：测试对 `padded_tokens ∈ {1,2,3,4,5,6,7,8}` 的每种组合断言三件事——logits 对 f64 点积参考的误差 < 5e-3、`topk_idx` 与主机参考**逐位相等**、`topk_weight` 误差 < 1e-4。参考实现在 [pegainfer-kernels/tests/glm52_router_smoke.rs:L39-L60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/glm52_router_smoke.rs#L39-L60)（f64 主机参考）与 [L100-L182](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/glm52_router_smoke.rs#L100-L182)（逐组合断言）。
4. 预期结果：全部通过；无 GPU 环境（如本讲义撰写环境）时**待本地验证**——该测试受 `#![cfg(feature = "glm52")]` 门控且需要 CUDA 工具链编译 `csrc/*.cu`。

**实践 B（无 GPU，源码阅读型）：跟踪 decode 路由的一条调用链。**

从 [src/model/step_body.rs:L218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L218) 的 `run_ep_router_into` 出发，沿 `glm52_router_noaux_tc_launch` → `glm52_router_noaux_tc_cuda` → `router_scores_topk_normalize_kernel` 走一遍，写出每个函数对「一个 token」读写的数据与形状。预期你会得到一张三行表：GEMV 读 `[6144]` bf16 写 `[256]` f32；选择读 `[256]` f32 + `[256]` 偏置写 `[8]` i32 + `[8]` f32。这是后续所有 MoE 篇章的公共出发点。

#### 4.1.5 小练习与答案

**练习 1**：为什么选择用 `sigmoid(logit) + bias` 排序，而归一化只除以 `Σ sigmoid(logit)`（不含 bias）？

**参考答案**：`e_score_correction_bias` 是训练出来做负载均衡的「选择偏置」——它应当影响哪些专家被选中，但不应该改变被选中专家之间的输出比例。若把 bias 混进归一化分母，被 bias 推高的专家会系统性获得超出其匹配度的权重，破坏模型输出。kernel 中 `choice_scores`（带 bias）只用于算秩，`scores`（不带 bias）才进 `selected_sum`（[glm52_router.cu:L59-L79](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L59-L79)）。

**练习 2**：两个专家的 choice 分数完全相等时，谁入选？

**参考答案**：专家 id 较小者。`better_router_choice` 在 `value == best_value` 时比较 `expert < best_expert`（[glm52_router.cu:L16-L20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_router.cu#L16-L20)），使全序严格、结果确定——CUDA Graph 重放要求同一输入永远产生同一路由。

**练习 3**：EP decode 和 TP prefill 的 `topk_weight` 数值差多少倍？各在哪里补上这个差？

**参考答案**：差 2.5 倍。TP prefill 在 kernel 里就把 `route_scale=2.5` 折进权重（`Glm52RouterConfig::glm52()`，[moe_decode.rs:L391-L401](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L391-L401)）；EP decode 用 `glm52_unscaled()` 只产归一化权重，2.5 在 combine 之后的收尾加法 `add_scaled_bf16_into(combined, 2.5, shared_out, …)` 里施加（见 4.2.3）。

### 4.2 共享专家前向：就地执行与 aux 流汇合

#### 4.2.1 概念说明

`n_shared_experts=1`：除了 256 个路由专家，每个 MoE 层还有一个**所有 token 无条件经过**的共享专家——一个中间维 2048 的普通 SwiGLU MLP。它的存在让路由专家只需承担「增量」部分，模型即使某个 token 的 top-8 都不匹配，也有一份基础 FFN 输出保底。

与路由专家的三点本质区别：

1. **不路由**：每个 rank 全量持有它的权重（非专家栈全量复制的 u2-l2 原则），token 在哪个 rank，它就在哪个 rank 就地算，**不经过 dispatch/combine**。
2. **不 fp8-DeepGEMM**：它复用 u3-l1 的 `fp8_mlp_into`（weight-only GEMV/小 GEMM 路径），与 dense MLP、q_a/kv_a 投影共用同一族原语。
3. **不缩放**：2.5 只乘路由和，共享专家输出原样相加。

#### 4.2.2 核心流程

一个 MoE 层 MLP 半场的执行（decode、EP 拓扑）：

```text
normed2 = RMSNorm(residual)              # 注意力半场结尾已产出
├── [aux 流]   shared.forward_into(normed2) → shared_out      # fork
└── [主 流]    router(normed2) → topk_idx/weight
               dispatch → 专家 GEMM → combine → combined      # u5-l2 的地盘
收尾（主 流）: mlp_out = 2.5 · combined + shared_out           # join
```

数学上：

\[ \text{mlp\_out} = \gamma \sum_{i \in \text{top8}} w_i \, E_i(x) + \text{Shared}(x), \qquad \gamma = 2.5 \]

其中 \( w_i \) 是 4.1 的归一化权重。fork/join 用 CUDA event 表达；图捕获期录下的 event 成为 graph 边，重放时两条分支保持并行。

#### 4.2.3 源码精读

**数据结构**（权重 + 一行前向）：

- [src/moe_decode.rs:L77-L83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L77-L83)：`Glm52MoeSharedExpert` 只有两个 `ProjWeight`——`gate_up`（fp8 `[4096, 6144]`，gate|up 已在构建期打包）和 `down`（fp8 `[6144, 2048]`）。构造函数（[L85-L108](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L85-L108)）逐项 `ensure!` 三个投影的形状后调 `pack_proj_pair`（u3-l1 精读过：要求第一个 n 是 128 的倍数，保证 scale 行对齐）。
- [src/moe_decode.rs:L110-L120](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L110-L120)：`forward_into` 是对 `fp8_mlp_into` 的薄封装——一次 GEMV 写出 `[gate | up]` 布局，SwiGLU 吃掉，再一次 GEMV 投影回 6144。`GLM52_SHARED_EXPERT_INTERMEDIATE = 2048`（[L123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L123)）用来给 decode scratch 的 `shared_mlp` 定宽（[src/scratch.rs:L125-L127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs#L125-L127)）。
- [src/fp8.rs:L510-L538](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L510-L538)：`fp8_mlp_into` 的分支——窄 bucket 走 gate|up GEMV（f32 k-切片 partial）+ 吸收归约的融合 SwiGLU + down GEMV；宽 bucket（#812 后）两个投影都走 fp8 GEMM、SwiGLU 独立成核。共享专家按 bucket 自动落进对应分支。

**逐层组装**：共享专家权重从 checkpoint 的 `model.layers.N.mlp.shared_experts.{gate,up,down}_proj` 来：

- [src/model/build.rs:L152-L180](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L152-L180)：EP 层的三件套在此组装——`router`（两个 take_tensor）、`shared`（三个 take_proj，形状 2048/6144）与 `bank`（`from_regions`，见 4.3）。对照 [src/moe_ep8.rs:L11-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep8.rs#L11-L18)：`Glm52MoeEp8LayerWeights { router, shared, bank }` 的文档注释写明了分工——router 和 shared 在 token 所在的 rank 就地跑，bank 只装本 rank 的专家。TP 分支（[L143-L150](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L143-L150)）首趟只带 router——TP4 把共享专家折进切片银行（见 4.3 末尾）。

**执行编排**（fork/join 的全部秘密就在这 45 行）：

- [src/model/step_body.rs:L195-L239](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L195-L239)：`glm52_moe_ep_layer`。
  - [L204-L216](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L204-L216)：fork——在主 stream 录 `normed_ready` 事件，aux stream 等它后跑共享专家写 `shared_out`，再录 `shared_done`。注释解释了动机：DeepEP 协作集合通信只占固定 SM 切片、大部分时间在等对端，GPU 其余部分正好给共享专家用。
  - [L218-L228](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L218-L228)：主 stream 依次跑路由器和 `routed_forward`（u5-l2 主角）。
  - [L229-L238](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L229-L238)：join——主 stream 等 `shared_done`，然后 `add_scaled_bf16_into(combined, 2.5, shared_out, …, mlp_out)` 一发完成 `2.5·路由和 + 共享`。这就是 4.1 说的「EP 的 2.5 延后到收尾」的落点。

#### 4.2.4 代码实践

**源码阅读型实践：给共享专家的 decode 前向列 kernel 清单。**

1. 实践目标：说出共享专家在窄/宽两种 bucket 下各发射几个 kernel，并解释为什么 fork/join 不需要任何 host 同步。
2. 操作步骤：
   - 读 [src/fp8.rs:L533-L549](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L533-L549) 的两个分支，为每个分支列出 launch 序列（提示：窄路径 3 个——gate|up partials、吸收归约的融合 silu·mul、down GEMV；宽路径 3 个——gate|up GEMM、独立 SwiGLU、down GEMM）。
   - 再读 [src/model/step_body.rs:L208-L216](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L208-L216)，注意全部同步原语是 `record_event`/`wait` 两个 stream 操作——host 侧零阻塞。
3. 需要观察的现象：fork/join 只操纵 CUDA event，没有任何 `cudaStreamSynchronize` 或回读。
4. 预期结果：能解释「事件在图捕获期被录成 graph 边，重放保持两分支并行」——即注释「The events recorded here during capture become graph edges; replay keeps the parallel branches」的含义。
5. 精确 kernel 名与计数无法在无 GPU 环境下用 nsys 验证，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：共享专家为什么不需要进入 dispatch/combine？

**参考答案**：它被每个 token 无条件激活且很小——每层约 36 MiB fp8 权重（与一个路由专家同量级），每 rank 都装得下，没有「按专家分片」的需求，token 所在 rank 全量持有权重即可就地算。dispatch/combine 的意义在于 256 个路由专家总量庞大（每层全 fleet 约 9 GiB、75 层共约 700 GiB），单卡放不下，必须分片并搬运 token。

**练习 2**：如果把共享专家放在主 stream、路由之前串行执行，损失什么？

**参考答案**：损失与 DeepEP 协作集合通信的重叠。集合通信占用固定 SM 切片并大量时间在等对端（free-running 纪律决定它必须恒定发射），共享专家挪到 aux stream 可以吃掉这些空闲 SM；串行则每层多付出整个共享专家前向时长。fork/join 的 event 只在图捕获期生效，重放时零 host 开销。

**练习 3**：`mlp_out = 2.5·combined + shared_out` 里，若 2.5 被错误地也乘在 shared_out 上，probe 阶段能拦住吗？

**参考答案**：不能。`probe_config_json` 只校验 checkpoint 的 `routed_scaling_factor` 数值（[config.rs:L146-L151](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L146-L151)），而「只乘路由和」是 kernel 编排语义，要靠 u10-l1 的 oracle 精度门（对 HF 参考逐层比对）这类端到端数值门才能抓到——这正是 oracle 门存在的理由。

### 4.3 Glm52MoeExpertBank：rank 分片与 UE8M0 布局

#### 4.3.1 概念说明

`Glm52MoeExpertBank` 是一个 rank 持有的**本层**路由专家库。EP 拓扑下 256 个专家按区间切分：`ep_local_experts = 256 / EP 宽度`（EP8 → 32，EP4 → 64），rank r 拥有全局专家 `[32r, 32(r+1))`——这正是 u2-l2 的 rank bundle 切分。bank 不是「新分配」的内存，而是**收养**（adopt）loader 在 H2D 时已按 expert-major 布局落位的 region，再把 scale 从 checkpoint 格式原地转换成 DeepGEMM 的 UE8M0 契约。

三个布局事实必须分清：

| 张量 | checkpoint 布局 | bank 驻留布局 |
| --- | --- | --- |
| W13 权重 | fp8 `[2048, 6144]` + `[2048, 6144]`（gate、up 各一） | fp8 `[n_local, 4096, 6144]`（gate\|up 串接） |
| W13 scale | f32 `[2048/128, 6144/128]` ×2（128×128 块） | UE8M0 i32 `[n_local, 6144/512, 4096]`（按输出列展开、512-K 组） |
| W2 | fp8 `[6144, 2048]` | fp8 `[n_local, 6144, 2048]`，scale 同理 `[n_local, 2048/512, 6144]` |

UE8M0 指「8 位指数、尾数为 0」——scale 被规范成 2 的幂存进 i32。这是 SM100 DeepGEMM 的权重 scale 契约（u4-l3 讲过它的写入侧必须向上取整的教训）。

#### 4.3.2 核心流程

bank 的构建与消费：

```text
loader（u2-l3）: 专家张量按 expert_placement 直落位进四个 region
                （H2D 拷贝时完成 expert-major 打包，无重排副本）
    ↓
from_regions: n_experts = region 大小 / 每专家步长   ← bank 与 loader 不可能漂移
    ↓
glm52_fp8_weight_ue8m0_prepare_launch × 2: 权重原地准备 + f32 块 scale → UE8M0
    ↓
decode 每层: dispatch 收到 expert-major 对齐行 → psum_expert（设备端）
             → 元数据 kernel 换算 expert_offsets/masked_m/row_map → 分组 GEMM
```

**psum_expert 为什么是「设备端真值」**：dispatch 是集合通信 kernel，每个 rank 实际收到多少行、每个本地专家几行，只有 dispatch kernel 自己知道。传统做法是回读计数到 host 再算 GEMM 形状——这会打断 CUDA Graph。这里 `psum_expert` 是 dispatch 写入设备缓冲的**对齐前缀和**（exclusive 形式，`n_local+1` 个 i32），后续元数据 kernel 直接在设备上消费它，host 从头到尾不知道行数。

#### 4.3.3 源码精读

**形状常量**：

- [src/moe_decode.rs:L28-L42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L28-L42)：`W13_N = 2×2048 = 4096`（gate|up）、`W13_K = 6144`、`W2_N = 6144`、`W2_K = 2048`，以及四个 scale 维：`HIDDEN_SCALE_COLS=48`、`W13_SCALE_ROWS=32`、`W2_SCALE_COLS=16`、`W2_SCALE_ROWS=48`（checkpoint 的 128×128 块网格）。

**bank 结构与构造**：

- [src/moe_decode.rs:L125-L132](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L125-L132)：四个连续缓冲——`w13_weight: fp8 [n_experts, W13_N, W13_K]`、`w13_scale: UE8M0 [n_experts, W13_K/512, W13_N]`（注意注释里转置的展开布局：K 组在外、输出列 N 在内）、`w2_*` 同理。
- [src/moe_decode.rs:L134-L189](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L134-L189)：`new` 先按 `n_experts × …` 校验四段长度，再对 W13/W2 各跑一次 `glm52_fp8_weight_ue8m0_prepare_launch`——权重与 scale 都以 `&mut` 传入，**原地**转成 DeepGEMM 契约，不产生第二份 36 MiB/专家 的副本。
- [src/moe_decode.rs:L255-L274](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L255-L274)：`from_regions` 的关键一行是 `n_experts = region 长度 / 每专家步长`——注释点明 loader 写入的正是 `pack_from_host`（测试路径，[L193-L248](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L193-L248)）的打包布局，专家数从 region 尺寸反推，「bank 和 loader 不可能漂移」。

**每 rank 多少专家**：

- [src/lib.rs:L272-L278](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L272-L278)：`ep_local_experts() = 256 / EP 宽度`。
- [pegainfer-kernels/csrc/deepep/deepep_config_glm52.cuh:L13-L20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/deepep/deepep_config_glm52.cuh#L13-L20)：EP8 shim 的编译期常量——8 rank、256 专家、32 本地、topk 8、hidden 6144、`kExpertAlignment = 64`（注释：TRTLLM grouped-GEMM 的 M-tile 粒度）。每专家每层权重约 36 MiB fp8 + 约 0.28 MiB UE8M0 scale，EP8 每 rank 每层约 1.13 GiB，75 个 MoE 层约 85 GiB——正是 u2-l2 说过的「专家板」体量。

**psum_expert：生产者、类型、消费者**：

- [pegainfer-kernels/src/ops/deepep.rs:L10-L14](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/deepep.rs#L10-L14)：布局契约原文——recv 行按 (expert, token) 槽排列，每个本地专家的段连续且对齐到 `expert_alignment`，`psum_expert` 是对齐后的运行偏移（exclusive 形式）；combine 不施加路由权重（由调用方先加权）。
- [pegainfer-kernels/src/ops/deepep.rs:L439-L451](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/deepep.rs#L439-L451)：`DeepEpDispatchScratch` 中 `psum_expert: CudaSlice<i32>`，`n_local + 1` 项；前 `n_local` 项在 dispatch 内部当原子计数器用。
- [pegainfer-kernels/csrc/deepep/deepep.h:L75-L91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/deepep/deepep.h#L75-L91)：C 侧契约同样写明 `psum_expert` 是 dispatch 的**输出**参数（`out [num_local_experts + 1]`）。
- [src/moe_ep.rs:L314-L342](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L314-L342)：消费点——dispatch 之后立即把 `&state.scratch.psum_expert` 喂给 `glm52_deepgemm_sm100_grouped_fp8_metadata_launch`，产出 `expert_offsets/masked_m/row_map` 供分组 GEMM；行数上界用协议最大值 `bound_rows`（`min(decode_worst_expanded_tokens, expanded_rows + (64-1)·min(expanded_rows, n_local))`，[L309-L312](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L309-L312)），其中 `decode_worst_expanded_tokens` 的推导见 [deepep_config_derived.cuh:L56-L60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/deepep/deepep_config_derived.cuh#L56-L60)（每个本地专家最多浪费 `align−1` 行）。
- [src/moe_ep.rs:L170-L174](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L170-L174) 与 [L316-L319](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L316-L319)：没有 token 的 rank 也必须参加集合通信（free-running：无人可以等待），所以备有 `zero_x/zero_topk_idx/zero_topk_weight` 零值输入——padding 行作为协议面必须构造性确定。
- 模块头部的全链路图示在 [src/moe_ep.rs:L1-L22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L1-L22)（dispatch → psum → 掩码行元数据 → UE8M0 量化 → W13 → silu·up → W2 → 加权回填 → combine），u5-l2 会逐步展开。

**TP4 的对照（预告 u5-l4）**：TP 拓扑没有 bank 分片，而是每 rank 持全部 257 个专家的**中间维切片**，共享专家折叠在银行下标 256：

- [src/moe_tp.rs:L58-L71](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_tp.rs#L58-L71)：`Glm52MoeTpSliceBank` 是 `[257, 512, 6144]` 的切片银行，`bank_idx`「路由专家 id，或 256 表示共享」。
- [src/moe_tp.rs:L241-L244](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_tp.rs#L241-L244)：装载表最后把 `shared_experts` 压进 `BANK - 1 = 256` 号槽（`BANK = GLM52_TP_BANK_EXPERTS = 257`，[L52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_tp.rs#L52)）——这就是本讲标题里「bank 256 折叠」的出处：共享专家在 TP 路径不再是独立 `ProjWeight`，而是混进专家银行当第 257 个「专家」一起做分组计算。

#### 4.3.4 代码实践

**手工模拟一张 8-token 路由表，推到 psum_expert**（本讲核心实践，纸笔即可）。

1. 实践目标：把 4.1 的路由输出和 4.3 的 recv 布局/前缀和串成一条链，证明你能预测 dispatch 之后每个 rank 的缓冲状态。
2. 操作步骤：
   - 设一个 EP8 集群，rank r 拥有全局专家 `[32r, 32(r+1))`。假设 8 个 token 都在本 rank 的一个 decode 步（bucket=8）里，假想路由表（每 token 恰好 top-8，id 互异）：

     | token | 选中的 8 个全局专家 id |
     | --- | --- |
     | t0, t1, t2, t4, t6 | {3, 17, 42, 100, 128, 200, 240, 255} |
     | t3, t5, t7 | {5, 9, 42, 77, 128, 150, 201, 255} |

   - 第一步：数出每个全局专家被多少 token 命中（行数）。
   - 第二步：把行数按 `[32r, 32(r+1))` 归到各 rank，得到每个 rank 的「真实 recv 行数」。
   - 第三步：按 `expert_alignment = 64` 把每个本地专家的段向上取整到 64 的倍数，写出 rank 0 与 rank 7 的 `psum_expert`（exclusive 前缀和，`n_local+1 = 33` 项）。
3. 需要观察的现象 / 预期结果（参考答案）：
   - 全局计数：42/128/255 各 8 行；3/17/100/200/240 各 5 行；5/9/77/150/201 各 3 行。合计 3×8 + 5×5 + 5×3 = 64 行 = 8 token × top-8 ✓。
   - 各 rank recv 行数：rank0（3,5,9,17）= 5+3+3+5 = **16**；rank1（42）= **8**；rank2（77）= **3**；rank3（100）= **5**；rank4（128,150）= 8+3 = **11**；rank5 = **0**；rank6（200,201）= 5+3 = **8**；rank7（240,255）= 5+8 = **13**。合计 16+8+3+5+11+0+8+13 = **64** ✓。
   - rank 0 的对齐布局：本地专家 3/5/9/17 各占 1 个 64 行段（本地 id 4..16、18..31 均为空段，长度 0）。`psum[i]`（本地专家 i 的段起始）为：`i ≤ 3` → 0；`i = 4, 5` → 64；`i = 6..9` → 128；`i = 10..17` → 192；`i ≥ 18` → 256（末项 `psum[32] = 256` 即总槽位）。
   - rank 7 的对齐布局：本地 16（5 行）与本地 31（8 行）各一段。`psum[i]`：`i ≤ 16` → 0；`17 ≤ i ≤ 31` → 64；末项 `psum[32] = 128`。
   - 空 rank（rank 5）的 `psum` 全 0——它仍必须参加 dispatch（传零值输入）。
   - **为什么是设备端真值**：上面这些计数在真实运行里没有一个是 host 算的——dispatch kernel 在通信时原子累加出每个本地专家的行数并对齐（`DeepEpDispatchScratch` 注释：前 `n_local` 项当原子计数器用），随后 `glm52_deepgemm_sm100_grouped_fp8_metadata_launch` 在设备上消费 `psum_expert` 生成 `masked_m/row_map`。host 只持有协议最大值 `bound_rows` 作缓冲上界；任何回读都会在整步 CUDA Graph（u6-l3）里撕开一个 host 同步点，这正是 free-running 纪律禁止的。
4. 验证方式：本题可在无 GPU 环境下完成全部手算；若要设备验证，可参考 u5-l2 的 `topology_tests` 或在 `moe_ep.rs` 的 dispatch 之后加只读日志打印 `psum_expert`（**改动仅限本地实验，勿提交**），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `from_regions` 用「region 长度 ÷ 每专家步长」反推 `n_experts`，而不是从拓扑常量直接算？

**参考答案**：让 bank 和 loader 不可能漂移。loader 按 `expert_placement` 落位的 region 就是事实来源；如果 bank 另用自己的 `n_experts` 常量，一旦切分逻辑改动（例如新增 EP 宽度），两边可能不一致而在 GEMM 形状处爆出难查的错位。从 region 尺寸反推把不一致变成启动期的 `ensure!` 失败（[moe_decode.rs:L259-L265](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L259-L265)）。

**练习 2**：bank 里 `w13_scale` 的形状是 `[n_local, W13_K/512, W13_N]`，与 checkpoint 的 `[W13_N/128, W13_K/128]` 有两点不同，分别是什么、为什么？

**参考答案**：一是**量化组从 128×128 块变成 512-K 列组**（`6144/512 = 12` 行），且按输出列 N 展开（每个输出列一组）；二是**dtype 从 f32 变成 UE8M0 i32**（2 的幂指数）。两者都是 SM100 DeepGEMM masked grouped GEMM 的 kernel 契约，由 `glm52_fp8_weight_ue8m0_prepare_launch` 在构建期一次性原地转换（[moe_decode.rs:L162-L181](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L162-L181)），运行期不再付转换成本。

**练习 3**：一个本步没有任何 token 的 rank（比如手算表里的 rank 5）在 dispatch 时传什么？为什么不能直接跳过？

**参考答案**：传 `zero_x/zero_topk_idx/zero_topk_weight` 零值缓冲（[moe_ep.rs:L316-L319](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L316-L319)）。不能跳过，因为 DeepEP dispatch 是每步固定节拍的集合通信——free-running 三纪律之首「任何 rank 都不许停或等」，跳过会让其他 rank 在同一步的 collective 上挂死；padding/零输入是协议面的一部分，必须构造性确定。

## 5. 综合实践

**任务：为「路由 → 分片 → 对齐 → 元数据」手算一条完整账，并给出源码证据链。**

设定 EP8、bucket=8，用 4.3.4 的路由表。完成以下四问，每一问都要附上你引用的源码行号：

1. **权重账**：从 `W13_N/W13_K/W2_N/W2_K`（[moe_decode.rs:L34-L37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L34-L37)）算出每专家每层 fp8 权重字节数，再乘 32（EP8 本地专家）与 75（MoE 层数），验证「每 rank 约 85 GiB 专家板」的说法；再从 `deepgemm_scale_bytes_per_layer`（[moe_ep.rs:L120-L125](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L120-L125)）算出 UE8M0 scale 的额外开销（约每层 9 MiB）。
2. **路由账**：写出 t0 的 8 个 `topk_idx`（全局 id）与 `topk_weight`（归一化，未乘 2.5），说明 EP decode 下这 8 个权重和为 1 而非 2.5 的源码依据（`glm52_unscaled` 与 `add_scaled_bf16_into` 两处）。
3. **recv 账**：完成 4.3.4 的手算，并额外回答——rank 4 的 11 个真实行落进 recv 缓冲后占据多少个对齐槽位？（答案：两个专家各一段，11 行 → 2×64 = 128 槽。）
4. **证据链**：用 `git grep -n "psum_expert"` 找出它的全部出现点，按「生产者（shim dispatch）→ 存储（DeepEpDispatchScratch）→ 消费者（metadata launch）」分类，说明为什么这条链上没有任何一处 host 回读。

预期产出：一页手算表 + 一份行号对照清单。全部可在无 GPU 环境完成；运行期数值验证**待本地验证**。

## 6. 本讲小结

- **noaux_tc 路由**：`sigmoid(logit)` 打分，`sigmoid + e_score_correction_bias` 排序取确定性 top-8（值降序、id 升序决平局），归一化只用不带偏置的 sigmoid 分数；单个 kernel（秩计数法）完成，与逐轮 argmax 逐位一致且更快（8.2µs vs 12.2µs）。
- **scale 的两条路**：EP decode 用 `glm52_unscaled()` 只产归一化权重，2.5 延后到收尾 `add_scaled_bf16_into`；TP prefill rows 用 `glm52()` 把 2.5 折进 `topk_weight`——语义在 `Glm52RouterConfig` 注释中钦定。
- **路由结果永不回读主机**：`RoutedTopk` 的 id/权重与 padding 行的 select 静默（`token >= active_tokens` 提前返回）共同保证整步可被 CUDA Graph 捕获。
- **共享专家**：每 rank 全量持有的 fp8 SwiGLU MLP（gate|up 打包），decode 时 fork 到 aux 流与 DeepEP 集合通信重叠，join 时以 `2.5·路由和 + shared` 一次加法合流；TP4 路径则把它折叠进 257 槽切片银行的 256 号位。
- **专家库布局**：`Glm52MoeExpertBank` 收养 loader 落位的 region（`n_experts` 从 region 尺寸反推），W13 `[n,4096,6144]` / W2 `[n,6144,2048]` fp8 权重配 `[n,K/512,N]` 的 UE8M0 i32 scale，是 SM100 DeepGEMM 的契约格式。
- **psum_expert 是 recv 侧行数的设备端真值**：dispatch 原子累加并按 64 对齐产出 exclusive 前缀和（`n_local+1` 项），元数据 kernel 在设备上消费它生成分组 GEMM 的掩码布局——host 只认识协议最大值，这让「通信决定形状」与「整步 graph 化」同时成立。

## 7. 下一步学习建议

- **下一讲 u5-l2《EP MoE：DeepEP dispatch/combine 与 SM100 DeepGEMM》**：本讲刻意停在 dispatch 门口。下一讲走进 [src/moe_ep.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs) 的完整链路——expert-major 对齐布局、psum 掩码元数据、UE8M0 激活量化、W13/W2 grouped GEMM、route_weight 回填与 combine，并解释「缓冲启动期固定 + host 静默」如何让 collective 可被图捕获。
- **横向对照**：带着本讲的 `psum_expert` 与 `bound_rows` 去读 [pegainfer-kernels/src/ops/deepep.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/deepep.rs) 与 [csrc/deepep/deepep.h](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/deepep/deepep.h)，体会「协议最大值 + 设备端真值」这对组合如何替代传统 MoE 的 host 同步。
- **回看**：若对「为什么空 rank 也要 dispatch」还有疑问，回头读 u7-l1 的 free-running 三纪律（`docs/models/glm52/free-running-dp.md`）；对 UE8M0 的取整教训，回看 u4-l3。
- **实践补完**：有 GPU 时补跑 4.1.4 的 `glm52_router_smoke`，并用 `cargo test --release -p pegainfer-glm52 --features glm52 --lib topology_tests` 观察 u5-l3 将讲的拓扑分片单测。
