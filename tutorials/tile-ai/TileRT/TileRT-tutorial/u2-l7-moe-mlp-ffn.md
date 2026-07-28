# MoE / MLP 前馈模块：专家路由、门控与 allreduce

## 1. 本讲目标

在上一讲（u2-l6）里，我们看完了 transformer 层的「注意力半边」——MLA 与 NSA 稀疏选择。本讲进入另一半：**前馈网络（Feed-Forward Network，FFN）**。DeepSeek-V3.2 的 FFN 有两种形态：

- 前 3 层是 dense（稠密）MLP；
- 后面 58 层是 **MoE（Mixture of Experts，混合专家）**——每层有 256 个候选专家，但每个 token 只激活其中 8 个。

学完本讲，你应该能够：

1. 画出 MoE 前馈的**三段融合算子链**（`RMSNormExpertProj` → `ExpertSelectUpGateSiLU` → `ExpertDownAllReduce`），并说清每段读写哪些 temp_vars 槽位（`SCORES`/`SEL_PROBS`/`SEL_INDICES`/`UP_GATE`/`EXP_OUT`）。
2. 理解 dense `Mlp` 与 `Moe` 的**结构对应**——为什么 dense MLP 可以看成「9 个专家全部常开」的退化 MoE，从而能复用同一批 temp_vars 槽位。
3. 看懂 FP8 权重的 **weights / scales 配对**如何被离线 `device_sharding` 切到 8 张卡，以及为什么 down 投影要和 **allreduce** 融合成一个算子。

## 2. 前置知识

- **FFN（前馈网络）**：注意力层之后的两层全连接，形状是 `dim → inter_dim → dim`。先放大维度做非线性，再缩回原维度。DeepSeek-V3.2 里 `dim = 7168`。
- **MoE（混合专家）**：把单个大 FFN 换成若干个「小 FFN（专家）」的集合，再由一个 **gate（门控/路由）** 决定每个 token 用哪几个专家。DeepSeek-V3.2 有 256 个候选专家（`n_routed_experts`），每个 token 激活 8 个（`n_activated_experts`），外加 1 个所有 token 共享的 `shared_expert`。
- **FP8 量化**：权重从 bf16 压成 8 位浮点（`torch.float8_e4m3fn`）省显存、提带宽；因为 FP8 动态范围小，每个权重组都配一个 **scale（缩放因子，`weight_scale_inv`）**，运算时用 `weight_dequant(w, scale)` 还原。所以每个权重张量都是「权重 + scale」成对出现。
- **SiLU**：激活函数 \(\text{SiLU}(x) = x \cdot \sigma(x)\)，常见的前馈非线性。
- **temp_vars / Idx**：复习 u2-l5，后端执行契约把所有激活临时变量压成一个扁平列表，`Idx` 枚举给下标起名。本讲要用到 `SCORES=17`、`X_MLP_IN=18`、`UP_GATE=19`、`SEL_PROBS=20`、`SEL_INDICES=21`、`EXP_OUT=22`。
- **register_op / exec_seq**：复习 u2-l4，容器用 `register_op(prefix, suffix)` 挂子算子，运行时 `init_tilert_weights` 按 `prefix + 短别名 + suffix` 从扁平 state_dict 取权重。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tilert/models/deepseek_v3_2/modules/moe.py` | MoE 容器：`Moe`（三段算子链）与 `MoeBlock`（MLA + MoE）。 |
| `tilert/models/deepseek_v3_2/modules/mlp.py` | dense MLP 容器：`Mlp`（两段算子链）与 `MlpBlock`（MLA + Mlp）。 |
| `tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py` | 核心融合算子：选专家 + up/gate 投影 + SiLU，融成一步。 |
| `tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py` | down 投影 + 专家加权求和 + 跨卡 allreduce，融成一步。 |
| `tilert/models/deepseek_v3_2/ops/rmsnorm_expert_proj.py` | RMSNorm + gate 投影，产出专家路由打分 SCORES。 |
| `tilert/models/deepseek_v3_2/ops/rmsnorm_up_gate_silu.py` / `ops/down_allreduce.py` | dense 版的两段算子，结构与 MoE 对应。 |
| `tilert/models/deepseek_v3_2/modules/dsa.py` | 层循环（前 3 层 MlpBlock、其余 MoeBlock）与 `get_temp_vars` 槽位定义。 |
| `tilert/models/deepseek_v3_2/temp_var_indices.py` | `Idx` 枚举，FFN 相关槽位下标。 |
| `tilert/models/deepseek_v3_2/model_args.py` | MoE 超参（专家数、路由方式、inter_dim 等）。 |

---

## 4. 核心概念与源码讲解

### 4.1 MoE 融合算子链：RMSNormExpertProj → ExpertSelectUpGateSiLU → ExpertDownAllReduce

#### 4.1.1 概念说明

MoE 前馈把传统 FFN 的「一次全连接」拆成三个职责清晰的阶段，再把每个阶段融合成一个后端算子（`.so` 里的 `torch.ops.tilert.*`）：

1. **算路由分（RMSNormExpertProj）**：对注意力输出做 RMSNorm，再用一个 `gate` 矩阵投影到 256 维，得到该 token 对每个专家的「打分」。
2. **选专家 + up/gate/SiLU（ExpertSelectUpGateSiLU）**：根据打分选 top-8 个专家（再加 1 个共享专家），对这 9 个专家同时做 `gate` 投影、`up` 投影，并融合 `SiLU(gate) * up`，输出中间激活。
3. **down + 加权 + allreduce（ExpertDownAllReduce）**：把每个专家的中间激活 down 投影回 `dim`，按专家权重加权求和，再把 8 张卡的部分和跨卡 allreduce 求总，最后加残差。

「融合」是这里的关键词：选专家、做矩阵乘、做激活本可以是三步，TileRT 把它们写进同一个 C++ kernel，省掉中间结果落地显存的往返（这正是 tile 级运行时降延迟的手段，见 u1-l1）。

#### 4.1.2 核心流程

一次 MoE 前馈（单层、单卡视角，`bs=1`，`seq` 个 token）：

```
注意力输出 UNPROJ_O (= 残差 x_in)
        │
        ▼
[RMSNormExpertProj]
   ├─ RMSNorm(x_in, gamma)  ──► X_MLP_IN   (归一化后的隐状态)
   └─ linear(X_MLP_IN, W_gate) ──► SCORES   ([1, seq, 256] 对 256 个专家的打分)
        │
        ▼
[ExpertSelectUpGateSiLU]   输入: X_MLP_IN, SCORES
   ├─ 选专家: topk(SCORES, 8) + 偏置 ──► SEL_INDICES [1,seq,8], SEL_PROBS [1,seq,8]
   └─ 对 (shared + 8 选中) 共 9 个专家:
        SiLU(X_MLP_IN @ W_gate_i) * (X_MLP_IN @ W_up_i) ──► UP_GATE  [1, seq, 9, 256]
        │
        ▼
[ExpertDownAllReduce]      输入: UP_GATE, SEL_INDICES, SEL_PROBS, x_in(残差)
   ├─ 每专家: UP_GATE[...,i] @ W_down_i ──► [dim]
   ├─ 路由专家按 SEL_PROBS 加权，shared 权重=1，9 个专家求和
   ├─ 跨 8 卡 allreduce 求和
   └─ + x_in(残差) ──► EXP_OUT  [1, seq, 7168]
```

三个算子之间不传 Python tensor，而是通过统一的 temp_vars 槽位 `SCORES → UP_GATE → EXP_OUT` 串联（槽位定义见 4.1.3）。

#### 4.1.3 源码精读

**容器装配**：`Moe` 用 `register_op` 把三个算子挂进 `exec_seq`，顺序就是上面的执行顺序。

[tilert/models/deepseek_v3_2/modules/moe.py:L24-L46](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L24-L46) —— `Moe.__init__` 依次注册三个融合算子。注意三个算子的 `algorithm` 都是 `BF16MMA`（DSv3.2 在 B200 上用 bf16 矩阵乘核，见 4.3）。

而 `MoeBlock` 把 MLA 注意力和 MoE 前馈拼成一整层：

[tilert/models/deepseek_v3_2/modules/moe.py:L55-L84](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L55-L84) —— `MoeBlock = MLA + Moe`，两个子模块都 `register_op`。这与 u2-l4 讲的层循环对应：`dsa.py` 在第 4 层之后构造 `MoeBlock`，prefix 为 `layer_{i}_`。

**算子① RMSNormExpertProj**：归一化 + 产出路由打分。

[tilert/models/deepseek_v3_2/ops/rmsnorm_expert_proj.py:L142-L149](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_expert_proj.py#L142-L149) —— 参考实现：先 RMSNorm，再用 `mlp.gate.weight`（形状 `[256, 7168]`）做线性投影得到 256 维 `scores`。生产路径调 `torch.ops.tilert.rmsnorm_expert_proj_op`（[L151-L169](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_expert_proj.py#L151-L169)），同时输出归一化隐状态 `hidden_out` 和 `scores_out`，分别落到 `X_MLP_IN` 与 `SCORES`。

**算子② ExpertSelectUpGateSiLU**：本讲的灵魂算子，三件事融成一步。先看它注册到后端的入口与输出：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L694-L713](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L694-L713) —— `tilert_forward` 把归一化隐状态 `x_in`、打分 `scores`、以及**预分配好的输出缓冲**（`hidden_out`→`UP_GATE`、`expert_probs`→`SEL_PROBS`、`expert_indices`→`SEL_INDICES`）一起喂给 `torch.ops.tilert.expert_select_up_gate_silu_op`。输出缓冲由调用方传入（out-of-place 风格），这正是 temp_vars「槽位复用」的体现。

融合的具体语义在参考实现里最清楚：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L654-L692](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L654-L692) —— `golden_forward` 是纯 PyTorch 参考实现（用于数值对拍）。可以看到三件事：(1) 由 `scores` 选出 `indices`；(2) `ref_gate[0]`/`ref_up[0]` 是共享专家，`ref_gate[1:][indices]`/`ref_up[1:][indices]` 是 8 个选中专家；(3) `hidden_out = SiLU(x @ W_gate) * (x @ W_up)`，9 个专家的中间激活堆在 `UP_GATE` 的第 2 维（共 9 个）。生产 kernel 把这三步合进一次 `expert_select_up_gate_silu_op` 调用，省掉中间落地。

专家选择（打分→topk）的逻辑，参考实现里 GLM-5 版本在文件内可见：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L642-L652](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L642-L652) —— `_ref_expert_select_glm5`：sigmoid 打分 → 加偏置 `e_score_correction_bias` → `topk(k=n_activated_experts=8)` → 权重归一化 → 乘 `route_scale=2.5`。DeepSeek-V3.2 版用的是 softmax（骨架一致，详见 GLM-5 镜像副本）。两种打分函数的差异见 u2-l2。输出的 `weights`→`SEL_PROBS`、`indices`→`SEL_INDICES`。

**算子③ ExpertDownAllReduce**：down + 加权 + 跨卡归约。它的 docstring 把张量形状讲得最全：

[tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py:L23-L63](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py#L23-L63) —— 输入 `vec_in [1,seq,8,256]` 是 UP_GATE 的路由专家部分，`mat_in [experts, dim, 256]` 是 down 权重，`indices/scores` 来自选择阶段，`x_in` 是残差；输出 `vec_out [1,seq,dim]` 落到 `EXP_OUT`。一个算子同时完成「down 投影 + 按专家加权 + allreduce + 残差相加」。

参考实现确认了「shared 专家不加权、路由专家按 `scores` 加权」的语义：

[tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py:L445-L468](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py#L445-L468) —— `golden_forward`：先算 shared 专家 `vec_in[0,s,0] @ W_down[0]`（权重 1），再对 8 个选中路由专家 `vec_in[0,s,i+1] @ W_down_sel[i] * scores[i]`，最后 `sum(dim=0)` 跨专家求和。（参考实现只做单卡求和，跨 8 卡的 allreduce 在生产 kernel 里完成，见 4.3。）

**槽位定义**：上面三个算子的输入输出在 `dsa.get_temp_vars` 里被分配成 temp_vars 槽位，形状由 `ModelArgs` 决定：

[tilert/models/deepseek_v3_2/modules/dsa.py:L171-L177](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L171-L177) —— FFN 链的关键槽位。注意 `UP_GATE` 的形状是 `[bs, seq, n_total_experts, moe_inter_dim]`，其中 `n_total_experts = n_activated_experts(8) + n_shared_experts(1) = 9`，`moe_inter_dim = moe_inter_dim // num_devices = 2048//8 = 256`（[L144-L145](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L144-L145)）。这 9×256 正好与 dense MLP 的布局共享，是 4.2 的伏笔。

槽位下标本身定义在枚举里：

[tilert/models/deepseek_v3_2/temp_var_indices.py:L35-L41](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py#L35-L41) —— `SCORES=17`、`X_MLP_IN=18`、`UP_GATE=19`、`SEL_PROBS=20`、`SEL_INDICES=21`、`EXP_OUT=22`。Python 与 C++ 后端必须对这串下标逐字段一致（u2-l5 讲过的 ABI 一致性）。

#### 4.1.4 代码实践

**实践目标**：把 SCORES/SEL_PROBS/SEL_INDICES/UP_GATE/EXP_OUT 五个槽位与三段算子对上号，画出 MoE 前馈数据流。

**操作步骤**：

1. 打开 `tilert/models/deepseek_v3_2/modules/dsa.py`，找到 `get_temp_vars` 中的 [L171-L177](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L171-L177)，把每个槽位的 dtype 与末维记下来：
   - `SCORES`：fp32，末维 256（`n_routed_experts`）
   - `SEL_PROBS`：fp32，末维 8（`n_activated_experts`）
   - `SEL_INDICES`：int32，末维 8
   - `UP_GATE`：bf16，形状末两维 `[9, 256]`
   - `EXP_OUT`：bf16，末维 7168（`dim`）
2. 打开 `expert_sel_up_gate_silu.py` 的 [tilert_forward（L694-L713）](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L694-L713)，确认它的输入是 `(X_MLP_IN, SCORES)`、输出填进 `(UP_GATE, SEL_PROBS, SEL_INDICES)`。
3. 画一张方框图：`UNPROJ_O →[RMSNormExpertProj]→ (X_MLP_IN, SCORES) →[ExpertSelectUpGateSiLU]→ (UP_GATE, SEL_PROBS, SEL_INDICES) →[ExpertDownAllReduce]+x_in → EXP_OUT`。

**需要观察的现象 / 预期结果**：你会看到 `UP_GATE` 同时被 `ExpertSelectUpGateSiLU` 写、被 `ExpertDownAllReduce` 读；`SEL_PROBS`/`SEL_INDICES` 是「选择阶段产出、down 阶段消费」的中间结果，且只对路由专家有意义（共 8 个）。这正是「融合算子」的边界：每个算子把能合并的读写合并，跨算子边界才落地 temp_vars。

> 说明：本实践是源码阅读型，不需要 GPU；若要在真机上验证 dtype/shape，可在 `get_temp_vars` 末尾临时 `print(temp_vars[Idx.UP_GATE].shape, temp_vars[Idx.UP_GATE].dtype)`，但**不要提交这个改动**（本仓库源码不可修改）。

#### 4.1.5 小练习与答案

**练习 1**：`ExpertSelectUpGateSiLU` 的输出有三个（`UP_GATE`/`SEL_PROBS`/`SEL_INDICES`）。为什么 `SEL_PROBS` 和 `SEL_INDICES` 的末维都是 8 而不是 9？

**参考答案**：因为 `UP_GATE` 的 9 个专家里，第 0 个是共享专家（所有 token 都用，无需选择），只有后 8 个是「从 256 个候选里 topk 选出来的路由专家」，选择概率和下标只对这 8 个有意义，所以 `SEL_PROBS`/`SEL_INDICES` 末维是 `n_activated_experts=8`。

**练习 2**：`expert_sel_up_gate_silu_op` 的输出缓冲（`hidden_out`/`expert_probs_out`/`expert_indices_out`）由调用方传入而非函数内创建，这样做有什么好处？

**参考答案**：这样这些缓冲就是 temp_vars 里的固定槽位，地址稳定，能被捕获进 CUDA Graph 反复复用（见 u2-l3 的 `prepare_money`），而不会每次 forward 都重新分配——对超低延迟推理至关重要。

---

### 4.2 Mlp 与 MoE 的结构对应：「9 个专家全部常开」就是 dense

#### 4.2.1 概念说明

很多人以为 dense MLP 和 MoE 是两套完全不同的代码。但在 TileRT 里，**dense MLP 被实现成一个「所有专家永远激活、权重恒为 1」的退化 MoE**，两者共用同一批 temp_vars 槽位（`UP_GATE`/`EXP_OUT`）和同样的 allreduce 通信结构。

关键洞察：DeepSeek-V3.2 里 dense 层的中间维度 `inter_dim = 18432`，正好等于 `9 × 2048 = 9 × moe_inter_dim`。如果把 dense 的 `inter_dim` 按 `moe_inter_dim=2048` 切成 9 份，每一份就是一个「专家」。于是 dense FFN 在结构上等价于「9 个专家全开」，和 MoE 的「1 shared + 8 routed = 9 个专家」凑巧相同——这不是巧合，而是为了让 dense 和 MoE 共享算子链与槽位而刻意设计的（见 `n_experts` 推导）。

#### 4.2.2 核心流程

dense MLP 的两段算子链：

```
注意力输出 (= 残差 x_in)
        │
        ▼
[RMSNormUpGateSiLU]   输入: x_in
   对全部 9 个"专家"(常开)做 RMSNorm + gate/up + SiLU ──► UP_GATE [1, seq, 9, 256]
        │
        ▼
[DownAllReduce]        输入: UP_GATE, x_in(残差)
   每专家 down 投影, 9 个专家等权(=1)求和 + 跨 8 卡 allreduce + 残差 ──► EXP_OUT [1, seq, 7168]
```

对比 MoE：

| 维度 | dense MLP | MoE |
|------|-----------|-----|
| 前置打分 | 无（不需选专家） | `RMSNormExpertProj` 产出 `SCORES` |
| up/gate/SiLU | `RMSNormUpGateSiLU`（RMSNorm 内嵌） | `ExpertSelectUpGateSiLU`（多了选专家） |
| 专家数 | `n_experts = 9`（= `inter_dim_per_device / moe_inter_dim_per_device`，全部常开） | 1 shared + 8 routed = 9 |
| down | `DownAllReduce` | `ExpertDownAllReduce`（多按 `SEL_PROBS` 加权） |
| 复用槽位 | `UP_GATE [bs,seq,9,256]`、`EXP_OUT` | 同左 |
| allreduce | 有 | 有 |

两者末段都用 `UP_GATE [bs,seq,9,256]`，所以 down 算子结构一致，差异仅在「是否按概率加权」。

#### 4.2.3 源码精读

**dense 容器**：`Mlp` 只有两段算子，比 `Moe` 少了「打分」段。

[tilert/models/deepseek_v3_2/modules/mlp.py:L16-L35](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mlp.py#L16-L35) —— `Mlp.__init__` 注册 `RMSNormUpGateSiLU`（algorithm 设为 `FP16MMA`）和 `DownAllReduce`。注意 dense 第一段把 RMSNorm 也融了进去（MoE 的 RMSNorm 拆在 `RMSNormExpertProj` 里，因为它要顺便算 gate 打分）。

`MlpBlock` 同样是 `MLA + Mlp`：

[tilert/models/deepseek_v3_2/modules/mlp.py:L38-L74](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mlp.py#L38-L74) —— 与 `MoeBlock` 镜像，构造参数几乎一样（`mlp` vs `moe`），方便 `dsa.py` 的层循环统一调用。

**「9 个专家」的由来**：dense 第一段算子里 `n_experts` 是算出来的，不是写死的。

[tilert/models/deepseek_v3_2/ops/rmsnorm_up_gate_silu.py:L112-L116](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_up_gate_silu.py#L112-L116) —— `n_experts = inter_dim_per_device // moe_inter_dim_per_device`。代入 DSv3.2 数值：\( (18432/8) / (2048/8) = 2304 / 256 = 9 \)。这就是 dense 的「专家数」，与 MoE 的 `8+1=9` 对齐，使两者 `UP_GATE` 形状完全一致。

> 这一行也解释了 u2-l4 里为什么 dense 层和 MoE 层能共用 `dsa.get_temp_vars` 的同一套槽位：dense 的 `RMSNormUpGateSiLU` 输出 `[bs,seq,9,256]`，与 MoE 的 `ExpertSelectUpGateSiLU` 输出同形。

**dense 的 down 不加权**：`DownAllReduce` 的参考实现对所有专家等权求和（权重恒为 1）。

[tilert/models/deepseek_v3_2/ops/down_allreduce.py:L293-L319](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/down_allreduce.py#L293-L319) —— `golden_forward`：对 9 个专家各做 down 投影后 `torch.sum(dim=0)`，没有按概率加权（对比 `ExpertDownAllReduce` 里 `* scores`）。这就是「全部常开、等权」。

**dense 复用 MoE 的权重转换器**：dense 的 down 转换器和 MoE 是同一个类，体现两者布局一致。

[tilert/models/deepseek_v3_2/ops/down_allreduce.py:L68](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/down_allreduce.py#L68) —— `DownAllReduceWeightsConverter = ExpertDownAllReduceWeightsConverter`，直接复用。`RMSNormUpGateSiLU` 同样复用 `ExpertSelectUpGateSiLUWeightsConverter`（[rmsnorm_up_gate_silu.py:L55](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_up_gate_silu.py#L55)）。dense 只是把权重视为「单个大专家展开成 9 份」。

**层循环的分界**：`dsa.py` 用 `n_dense_layers=3` 决定哪些层用 dense。

[tilert/models/deepseek_v3_2/modules/dsa.py:L63-L85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L63-L85) —— `layer_idx < n_dense_layers` 用 `MlpBlock`，否则用 `MoeBlock`，两者用相同的 prefix/suffix 契约（`layer_{i}_..._dev_{d}`）注册，所以权重键名规则一致（u2-l4、u1-l6）。

#### 4.2.4 代码实践

**实践目标**：验证 dense 和 MoE 的 `UP_GATE` 槽位形状完全一致，从数值层面理解「dense = 退化 MoE」。

**操作步骤**：

1. 在 `model_args.py` 确认 `inter_dim=18432`、`moe_inter_dim=2048`、`n_activated_experts=8`、`n_shared_experts=1`（[model_args.py:L66-L68](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L66-L68)，并对照 [L59-L61](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L59-L61)）。
2. 手算 dense 的 `n_experts`：\( \lfloor (18432/8) / (2048/8) \rfloor = 9 \)，与 MoE 的 `8+1=9` 对比。
3. 写一段伪代码（**示例代码，非项目原有**）模拟两者输出对齐：

```python
# 示例代码：dense 与 MoE 的 UP_GATE 布局一致性
import torch
bs, seq = 1, 1
n_total_experts, moe_inter_dim_per_dev = 9, 256   # MoE: 1 shared + 8 routed
up_gate_moe  = torch.zeros(bs, seq, n_total_experts, moe_inter_dim_per_dev, dtype=torch.bfloat16)
up_gate_dense = torch.zeros(bs, seq, n_total_experts, moe_inter_dim_per_dev, dtype=torch.bfloat16)
assert up_gate_moe.shape == up_gate_dense.shape   # 同一个 temp_vars[Idx.UP_GATE] 槽位可通用
```

**需要观察的现象 / 预期结果**：两个 `UP_GATE` 形状完全相同，证明 dense 和 MoE 在 `dsa.get_temp_vars` 里共用同一个槽位定义。差异只在「谁写它」：dense 由 `RMSNormUpGateSiLU` 写（9 份全填），MoE 由 `ExpertSelectUpGateSiLU` 写（只填 1 shared + 8 selected 的位置）。

#### 4.2.5 小练习与答案

**练习 1**：dense 层的 `n_experts = 9`。如果某天 `inter_dim` 改成 `20480`（仍 `moe_inter_dim=2048`、`num_devices=8`），dense 还能与 MoE 共用 `UP_GATE` 槽位吗？

**参考答案**：新 `n_experts = (20480/8) / (2048/8) = 2560/256 = 10`，而 MoE 仍是 9，两者不再相等，`UP_GATE` 形状不一致就不能共用槽位。这正说明 `inter_dim` 的取值是「被 MoE 专家数约束」的——dense 维度必须能凑成与 MoE 相同的专家数，共享槽位才能成立。

**练习 2**：`Mlp` 的第一段是 `RMSNormUpGateSiLU`，把 RMSNorm 融进去了；而 `Moe` 把 RMSNorm 单独放在 `RMSNormExpertProj`。为什么有这个差别？

**参考答案**：MoE 的 RMSNorm 之后还要用 `gate` 矩阵算 256 维路由打分（`SCORES`），所以 RMSNorm 必须和「算打分」分开成一个独立算子 `RMSNormExpertProj`，输出归一化隐状态给后续选专家用；dense 不需要选专家，RMSNorm 可以直接和 up/gate/SiLU 融在一起省一次落地。

---

### 4.3 FP8 权重 / scales 配对与跨卡 allreduce

#### 4.3.1 概念说明

本模块讲两个「让 MoE 在 8 卡上跑得起来」的关键设计：

1. **FP8 权重 + scales 配对**：每个专家的 `gate_proj`/`up_proj`/`down_proj` 权重都是 FP8，并各自配一个 `weight_scale_inv`。权重和 scale 是「成对」的，缺一不可。离线 `device_sharding` 把所有专家（含 shared）的某类权重堆叠成一个张量（如 `exp_gate_weights`），并按 `num_devices` 切成 8 份。

2. **down 阶段的 allreduce**：因为 up/gate/down 都沿 `moe_inter_dim`（=2048）维度切成 8 份分到 8 张卡，每张卡只算 1/8 的中间激活和对应的 down 部分和。要得到最终的 `dim=7168` 输出，必须把 8 张卡的部分和**跨卡求和**——这就是 `ExpertDownAllReduce` / `DownAllReduce` 名字里 AllReduce 的由来，也是它和残差相加一起融进单个算子的原因。

#### 4.3.2 核心流程

**权重布局**（以 `ExpertSelectUpGateSiLU` 的 gate 权重为例，单个专家的 `gate_proj` 形状 `[moe_inter_dim=2048, dim=7168]`）：

- 离线 `device_sharding`：沿输出维 `moe_inter_dim` 切 8 份 → 每卡 `[256, 7168]`。
- 把 257 个专家（1 shared + 256 routed）的切片沿专家维堆叠 → 每卡 `exp_gate_weights [257, 256, 7168]`。
- scales 同理堆叠 → 每卡 `exp_gate_scales`。
- 运行时 `init_tilert_weights` 再调用 `convert_to_<algo>` 把权重 swizzle 成 B200 MMA 核喜欢的内存排布（FP8 tile 重排），并把 weights 与 scales 拼进相邻内存（kernel 一次读取）。

**allreduce 的数学含义**：设 down 投影在卡 \(d\) 上的部分和为 \(y^{(d)}\)，则最终输出（不含残差）

\[
y_{\text{final}} = \sum_{d=0}^{7} y^{(d)}, \qquad
y^{(d)} = \sum_{\text{experts }i} w_i \cdot \mathrm{down}_i\!\left( \mathrm{upgate}_i^{(d)} \right)
\]

其中 \(w_i\) 对路由专家是 `SEL_PROBS`、对 shared 是 1。跨卡求和与跨专家求和可交换，所以一个融合 kernel 能一次做完。

#### 4.3.3 源码精读

**算法枚举与权重别名**：先看算子声明了哪些 MMA 算法、哪些权重短名。

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L101-L106](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L101-L106) —— `ExpertSelectUpGateSiLUAlgorithm`：`FP8MMA`/`FP16MMA`/`BF16MMA` 三种矩阵乘核。DSv3.2 在 `Moe` 里用的是 `BF16MMA`（[moe.py:L36](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L36)）。算法的字符串值（如 `"bf16mma"`）通过 `dispatch` 映射到 `convert_to_bf16mma` 方法（u3-l1 详讲）。

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L77-L98](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L77-L98) —— TileRT 权重短别名：`exp_bias`、`exp_gate_weights`、`exp_gate_scales`、`exp_up_weights`、`exp_up_scales`。**weights 与 scales 成对**（gate 有 weights+scales，up 也有 weights+scales）。

**离线分片 `device_sharding`**：把 HF 的 257 个专家权重堆叠 + 切 8 卡。

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L468-L520](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L468-L520) —— 先取 shared 专家（`shared_experts`），再循环 256 个 routed 专家，逐个 `process_gate_up_weights` 把单个专家权重 `reshape(num_devices, 1, in_dim_per_device, dim)` 切 8 份，最后 `torch.cat` 沿专家维堆叠。输出 dict 的键就是上面的短别名，值第一维是 `num_devices=8`（u1-l6 的 `*_dev_{id}` 分区键据此取卡）。

**运行时加载 `init_tilert_weights`**：从扁平 state_dict 取出已分片权重，调转换器做 MMA swizzle。

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L558-L563](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L558-L563) —— 按 `tilert_weights_alias` 取出 `[bias, gate_weights, gate_scales, up_weights, up_scales]` 五项，交给 `ExpertSelectUpGateSiLUWeightsConverter.dispatch(algorithm, ...)`。`dispatch` 根据算法字符串拼出 `convert_to_bf16mma` 方法名调用（[base.py:L30-L32](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L30-L32)）。转换器内部把 FP8 权重按 16×32/16×16 tile 重排，并把 scales 塞进权重张量尾部（`weights_and_scales`），让 kernel 一次读齐（[L290-L301](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L290-L301)）。

**down 的 allreduce**：down 权重沿 `moe_inter_dim` 切 8 卡，所以输出必须跨卡求和。

[tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py:L354-L377](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py#L354-L377) —— `device_sharding`：`process_down_weights` 把每个专家的 `down_proj [dim=7168, moe_inter_dim=2048]` 沿 `moe_inter_dim` 切 8 份（每卡 `[7168, 256]`），堆叠 257 个专家。因为切的是 down 的输入维，每卡 down 出来只是部分和，必须 allreduce。

[tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py:L470-L492](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py#L470-L492) —— `tilert_forward` 把 `vec_in`(UP_GATE)、down 权重/scales、`indices`/`scores`、**残差 `x_in`**、`flag` 全喂进 `expert_down_allreduce_op`，一个 kernel 完成「down + 加权 + allreduce + 残差」。dense 的 `DownAllReduce` 同构（[down_allreduce.py:L321-L339](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/down_allreduce.py#L321-L339)），只是少了 `indices`/`scores`。

#### 4.3.4 代码实践

**实践目标**：追踪一个专家的 `gate_proj.weight` 从 HF checkpoint 到「每卡 FP8 + scale」的完整旅程，理解 weights/scales 配对。

**操作步骤**：

1. 在 HF 端，单个专家权重的键名是 `mlp.experts.{i}.gate_proj.weight` 与 `mlp.experts.{i}.gate_proj.weight_scale_inv`（见 [expert_sel_up_gate_silu.py:L62-L71](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L62-L71) 的 `ref_tensor_alias`）。
2. 追踪 `device_sharding`（[L468-L520](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L468-L520)）：257 个专家的 gate 权重堆成 `exp_gate_weights [8, 257, 256, 7168]`，scales 堆成 `exp_gate_scales`。
3. 离线转换写出后，键名变成 `layer_{i}_exp_gate_weights_dev_{d}` / `..._exp_gate_scales_dev_{d}`（u1-l6 的命名模板）。weights 与 scales 总是成对出现、成对分卡。
4. 写一段伪代码（**示例代码**）演示 down 必须 allreduce 的原因：

```python
# 示例代码：为什么 down 之后要 allreduce
# down_proj 权重沿 moe_inter_dim 切到 8 卡: 每卡只持有 1/8 的输入维
# per_device down 输出只是完整 down 的 1/8 部分和
partial_out_dev0 = up_gate_dev0 @ down_weight_dev0.T   # [dim], 但只是 256 个输入维的贡献
# 需要 allreduce 把 8 张卡的部分和加起来 = 完整 down_proj 的结果
final = sum_over_8_devices(partial_out_dev_d) + residual
```

**需要观察的现象 / 预期结果**：你会看到三类权重（gate/up/down）都遵循「沿 moe_inter_dim 切 8 卡 → 每卡 weights + scales 成对」的统一模式；其中只有 down 的切法导致输出需要 allreduce（gate/up 的切法只影响中间激活，不需要跨卡归约，因为它们的输出会被 down 在「按 moe_inter_dim 求和」时隐式聚合）。

> 待本地验证：若手头有转换好的权重，可在 Python 里 `torch.load` 一个 `exp_gate_weights_dev_0` 与 `exp_gate_scales_dev_0`，确认两者第一维都是 257（含 shared）、权重 dtype 是 `float8_e4m3fn`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gate_proj`/`up_proj` 切到 8 卡后**不需要** allreduce，而 `down_proj` 需要？

**参考答案**：gate/up 沿 `moe_inter_dim`（输出维）切，每卡算出的是各自那 256 维的中间激活，这些激活接着喂给 down。down 沿 `moe_inter_dim`（输入维）切，每卡只对属于自己的 256 维做矩阵乘，得到的是 `[dim]` 的**部分和**；要还原完整 down 投影结果，必须把 8 卡的部分和相加（allreduce）。gate/up 的「切片」在 down 的求和过程里被自然聚合，无需单独 allreduce。

**练习 2**：`ExpertDownAllReduce` 的 kernel 同时把「残差相加」也做了（输入里有 `x_in`）。把残差相加留到 kernel 外做（先 allreduce 再在 Python 里 `+ x_in`）会有什么问题？

**参考答案**：会多一次 `[dim]` 张量的显存读写往返，且打破 CUDA Graph 的单算子边界。把它融进 allreduce kernel，残差相加几乎是「免费」的（访存已经为了 allreduce 发生），这正是 tile 级运行时「计算与访存重叠、减少落地」思想的体现（u1-l1）。

---

## 5. 综合实践

**任务**：给定一个 `layer_idx = 10` 的 MoE 层，把它在 8 卡上的完整前馈数据流画成一张图，并标注每一段算子、它读写的 temp_vars 槽位、以及跨卡通信点。

**要求**：

1. 标出该层在 `dsa.py` 里以 `layer_10_..._dev_{d}` 注册（[dsa.py:L63-L85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L63-L85)）。
2. 按 `MLA → RMSNormExpertProj → ExpertSelectUpGateSiLU → ExpertDownAllReduce` 的顺序，每段标出：
   - 读哪个槽位作为输入（如 `ExpertSelectUpGateSiLU` 读 `X_MLP_IN` + `SCORES`）；
   - 写哪个槽位作为输出（写 `UP_GATE`/`SEL_PROBS`/`SEL_INDICES`）；
   - 涉及哪些 FP8 权重/scales（如 `exp_gate_weights`/`exp_gate_scales`）。
3. 标出**唯一的跨卡通信点**：`ExpertDownAllReduce` 的 allreduce（gate/up 不需要）。说明为什么这是 8 卡 MoE 唯一需要跨卡归约的地方。
4. 对比 `layer_idx = 1` 的 dense 层，标出它少了哪一段（无 `RMSNormExpertProj`/无选择）、`UP_GATE` 仍由 9 个专家填满但全部等权。

**预期产出**：两张数据流图（dense 一张、MoE 一张），能清楚说明「dense 是退化 MoE、共享 `UP_GATE`/`EXP_OUT` 槽位、共享 down+allreduce 结构、唯一区别是有无专家选择」。

## 6. 本讲小结

- DeepSeek-V3.2 的 MoE 前馈是三段融合算子链：`RMSNormExpertProj`（算路由分 `SCORES`）→ `ExpertSelectUpGateSiLU`（选 8 个路由专家 + 1 shared，做 up/gate/SiLU，产出 `UP_GATE`/`SEL_PROBS`/`SEL_INDICES`）→ `ExpertDownAllReduce`（down + 加权 + allreduce + 残差 → `EXP_OUT`）。
- dense `Mlp` 是两段链（`RMSNormUpGateSiLU` → `DownAllReduce`），本质是「9 个专家全部常开、等权」的退化 MoE，`n_experts = (inter_dim/8) / (moe_inter_dim/8) = 9` 恰好等于 MoE 的 `8 routed + 1 shared`。
- 正因为 dense 与 MoE 的 `UP_GATE [bs,seq,9,256]` 与 `EXP_OUT` 形状一致，两者在 `dsa.get_temp_vars` 里共用同一批槽位，dense 还直接复用 MoE 的权重转换器（`DownAllReduceWeightsConverter = ExpertDownAllReduceWeightsConverter`）。
- FP8 权重永远是「weights + scales 成对」，gate/up/down 各一组；离线 `device_sharding` 沿 `moe_inter_dim` 切 8 卡、堆叠 257 个专家（含 shared），键名走 `layer_{i}_exp_*_dev_{d}`。
- 8 卡 MoE 的**唯一跨卡归约点**是 `ExpertDownAllReduce`：因为 down 权重沿 `moe_inter_dim` 切卡，每卡只产部分和，必须 allreduce 求总；残差相加被融进同一个 kernel 以省一次落地。
- 融合算子的输出缓冲由调用方（temp_vars 槽位）传入，地址稳定，可被 CUDA Graph 捕获复用——这是超低延迟的关键（承接 u2-l3 的 `prepare_money`）。

## 7. 下一步学习建议

- **u3-l1（算子层设计）**：本讲多次提到 `algorithm` 枚举与 `convert_to_<algo>` 的 dispatch。下一讲会系统讲解 ops 层的统一骨架、`dispatch` 如何按算法字符串选转换方法、以及 `device_sharding` 的双用途（离线转换 + 运行时加载）。
- **u3-l2（生成主循环）**：本讲讲的是「单层 FFN 内部」的数据流；下一阶段的生成主循环会把这些层串成逐 token 解码，你会看到 `EXP_OUT` 如何流向下一层、`CUR_POS` 如何推进。
- **延伸阅读**：若想理解 FP8 tile swizzle 的细节（`_swizzle_qmma_16x32` 等），可对照 `expert_sel_up_gate_silu.py` 的 `convert_to_mma` 方法（[L214-L301](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L214-L301)），这部分与 B200 的 MMA 指令内存排布强相关，属专家级内容。
