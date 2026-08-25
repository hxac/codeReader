# u3-l5 MTP 投机解码与采样器

## 1. 本讲目标

本讲是 openPangu 模型实现精读单元的最后一讲。前面四讲我们搞清楚了「一个 token 是怎么被算出来的」，本讲回答另一个问题：**能不能一次 decode 算出多个 token？**

读完本讲，你应该能够：

1. 解释 MTP（Multi-Token Prediction，多 token 预测）draft 层的结构、权重来源与加载路径。
2. 说出 `--num-speculative-tokens` 从 ansible 模板到 vLLM 引擎的完整参数传递链，以及它如何控制 draft 长度。
3. 读懂 NPU 采样器三件套：`NPUSamplerV1`、`NPUTopKTopPSampler`、`NPURejectionSampler`。
4. 手推投机解码中拒绝采样的接受条件，并解释它为什么能保证输出分布与 target 模型一致。

## 2. 前置知识

### 2.1 自回归 decode 为什么慢

decode 阶段每生成一个 token，都要把整个模型跑一遍，但每次前向只处理 1 个 token。此时矩阵乘法的形状极小，NPU 的算力大量闲置，**瓶颈是访存**：每一步都要把全部权重从 HBM 读一遍，读出来的数据却只服务一个 token。

于是有一个朴素的想法：既然算力闲着，能不能「猜」出后面几个 token，让 target 模型一次前向同时验证多个猜测？这就是投机解码（speculative decoding）。

### 2.2 投机解码的两角色

| 角色 | 职责 | 本项目中的实现 |
| --- | --- | --- |
| draft（草稿）模型 | 小而快，负责「猜」出 k 个候选 token | `OpenPanguV2MTP`，权重与主模型同库 |
| target（目标）模型 | 大而准，一次前向验证 k 个候选 + 多算 1 个 bonus token | `OpenPanguV2ForCausalLM` |

一轮投机解码的流程：

```
1. draft 依当前上下文连续生成 k 个候选 token（cheap）
2. 把这 k 个 token 连同原上下文拼成 k+1 长度的输入，喂给 target 一次前向（贵但只跑一次）
3. 用 target 的输出逐位验证 draft 的候选：
   - 前面全部猜对 → 这 k 个 token 全部采纳，再白拿 1 个 bonus token
   - 第 i 位猜错 → 采纳前 i-1 个，第 i 位换成按 target 分布采样的「恢复 token」
4. 重复
```

关键性质：**无论 draft 猜得多差，输出分布始终与单独跑 target 一致**（这是拒绝采样保证的，见 4.4 节）。draft 猜得准只是加速，猜不准只是白费算力，不会降低生成质量。

### 2.3 MTP：draft 层就藏在主模型权重里

传统投机解码（如 EAGLE）需要单独训练一个 draft 模型。MTP 则是训练时就在主模型后面挂了几个「预测下一层」的轻量层，推理时直接复用同一份 checkpoint：

- checkpoint 里 `model.layers.0` ~ `model.layers.{N-1}` 是主模型（target）的 N 层；
- `model.layers.{N}` ~ `model.layers.{N+K-1}` 是 K 个 MTP draft 层（`num_nextn_predict_layers = K`）；
- 加载时 target 模型**跳过** MTP 层的权重，draft 模型**只收** MTP 层的权重。

### 2.4 采样与 top-k / top-p

模型输出的是每个 token 的 logits（未归一化分数）。采样决定「选哪个 token」：

- **贪心（greedy）**：直接取 argmax，确定性输出。
- **top-k**：只在 logits 最大的 k 个 token 里采样。
- **top-p（核采样）**：把 token 按概率从大到小排序，取累积概率达到 p 的最小集合，在这个集合里采样。
- **温度**：`logits / T`，T→0 等价贪心，T 大则分布更平。

### 2.5 Gumbel-max 技巧：不用 multinomial 的采样

从概率向量 `probs` 里按类别采样，直觉做法是 `torch.multinomial`，但它会触发主机-设备同步，慢。等价做法是「指数竞赛」：

\[ P\left(\arg\max_i \frac{p_i}{q_i} = i\right) = p_i, \quad q_i \overset{\text{iid}}{\sim} \mathrm{Exp}(1) \]

因为 \(-\log q_i\) 服从 Gumbel 分布，而 `argmax(log p_i - log q_i) = argmax(p_i / q_i)`，这正是 Gumbel-max 定理。本讲的采样代码里反复出现的 `q.exponential_()` + `probs.div_(q).argmax()` 就是这个技巧（见 4.3.3）。它的另一个好处：随机数 `q` 可以在**另一条流上提前生成**（代码里叫 dsa_stream / side stream），与主计算重叠。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py` | MTP draft 模型全部实现：层结构、前向、权重改名与加载 |
| `components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py` | target 主模型；`load_weights` 里用 `get_spec_layer_idx_from_weight_name` 跳过 MTP 权重 |
| `components/omni-npu/src/omni_npu/v1/models/__init__.py` | 把 `OpenPanguV2MTPModel` 注册进 vLLM ModelRegistry |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_speculative.py` | 补丁：告诉 vLLM「openpangu_v2 的 draft 用 MTP」 |
| `components/omni-npu/src/omni_npu/sample/sampler.py` | `NPUSamplerV1`：普通采样入口 |
| `components/omni-npu/src/omni_npu/sample/ops/topk_topp_sampler.py` | `NPUTopKTopPSampler`：top-k/top-p + Gumbel-max 采样的 NPU 实现 |
| `components/omni-npu/src/omni_npu/sample/rejection_sampler.py` | `NPURejectionSampler`：投机解码的验证器 |
| `components/omni-npu/src/omni_npu/worker/npu_model_runner.py` | 装配点：创建 sampler/rejection_sampler、为 MTP 层包 ACLGraph |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eagle.py` | 补丁：draft 模型与 target 共享 embedding/lm_head |
| `tools/scripts/pd_run.sh`、`tools/scripts/start_api_servers.py`、`tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | 部署参数链 |
| `components/omni-npu/tests/unit/sample/test_rejection_sampler.py` | 拒绝采样单测（无需 NPU，mock 了 torch_npu） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**MTP 投机解码**、**采样器接口**、**拒绝采样**。部署参数链作为第一个模块的实践部分穿插讲解。

### 4.1 MTP 投机解码：draft 层的结构与权重来源

#### 4.1.1 概念说明

openPangu-2.0 的 MTP draft 层不是从零搭的小模型，而是「**复刻一个主模型 DecoderLayer + 三件套适配件**」：

- `enorm` / `hnorm`：两个 RMSNorm，分别归一化「当前 token 的 embedding」和「上一步的隐状态」；
- `eh_proj`：一个把两者拼接后投影回 hidden_size 的线性层（`2*hidden → hidden`）；
- `mtp_block`：直接复用主模型的 `OpenPanguV2DecoderLayer`（含注意力、MoE、mHC 一整套）；
- `shared_head`：输出头，推理时与主模型共享 lm_head 权重。

为什么需要 enorm/hnorm/eh_proj？因为 draft 每一步的输入是「**新 token 的 embedding + 上一个位置的隐状态**」两路信息——隐状态由上一轮 decode 免费送来，这正是 MTP 比外挂 draft 模型快的原因：它不用从头算表征。

#### 4.1.2 核心流程

一次 draft 前向（单个 MTP 层）：

```
输入: input_ids, positions, previous_hidden_states(上一轮主/草模型隐状态), inputs_embeds
1. h = enorm(inputs_embeds);             # 归一化 embedding
2. s = hnorm(previous_hidden_states);    # 归一化隐状态
3. x = eh_proj(concat([h, s]));          # 融合两路信息
4. (可选) 序列并行 padding
5. 从 mtp_block.self_attn 取 rotary cos/sin
6. x = mhc_head(x) → mtp_block(x);       # 完整 DecoderLayer 前向
7. (可选) unpadding
输出: 新的隐状态 → 交给 shared_head + logits_processor 出 logits → 采样出候选 token
```

外层 `OpenPanguV2MultiTokenPredictor` 管 K 个 MTP 层的轮转：第 `spec_step_idx` 步用第 `spec_step_idx % K` 层（K = `num_nextn_predict_layers`）。

#### 4.1.3 源码精读

**(1) draft 层的三件套与主模型复刻**

[pangu_v2_moe_mtp.py:L68-L98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L68-L98) 构造 `OpenPanguV2MultiTokenPredictorLayer`：先建 `enorm`/`hnorm`/`eh_proj`（`ReplicatedLinear`，不切 TP）与 `SharedHead`，然后用 `OpenPanguV2DecoderLayer(config, prefix, vllm_config)` 直接实例化一个完整的主模型层作为 `mtp_block`，并手工设置 `_tail_refs` 把它标记为「模型尾部」（关闭尾部 mhc 传递，layernorm 置为 no-op）。

**注意**：draft 层的 config 取自 `vllm_config.speculative_config.draft_model_config.hf_config`（第 73 行），即 vLLM 为投机解码单独准备的 draft 配置。

**(2) draft 层前向**

[pangu_v2_moe_mtp.py:L104-L135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L104-L135) 实现 4.1.2 的流程：`enorm` → `hnorm` → `eh_proj(concat)` → 可选 padding → `mhc_head` → `mtp_block` → 可选 unpadding。其中 `_maybe_padding_and_slice` / `_maybe_gather_and_unpadding` 是从主模型文件导入的序列并行辅助函数（TP>1 且通信后端非 allreduce 时给 token 数补齐到 TP 整数倍，见第 98 行的 `need_tp_padding` 判断）。

**(3) K 层轮转与多流**

[pangu_v2_moe_mtp.py:L138-L166](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L138-L166) 构造 `OpenPanguV2MultiTokenPredictor`：`ModuleDict` 以 `"N"`, `"N+1"`, ... 字符串为键建 K 个 draft 层（`N = config.num_hidden_layers`，键直接沿用权重里的层号，方便对位加载）；`enable_multi_stream` 时为 draft 层配置专属的 side/fetch 流。

[pangu_v2_moe_mtp.py:L171-L194](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L171-L194) 前向按 `spec_step_idx % self.num_mtp_layers` 选层（第 181 行）——如果 draft 长度 k 大于 MTP 层数 K，同一层会被循环复用；第 183-186 行优先走 `wrapped_layers`（被 ACLGraphWrapper 包装后的层，见本模块第 (7) 点）。

**(4) draft 的 logits**

[pangu_v2_moe_mtp.py:L196-L207](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L196-L207) 的 `compute_logits`：`shared_head`（内部是个 RMSNorm，见 [pangu_v2_moe_mtp.py:L64-L65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L64-L65)）先归一化，再用 `logits_processor(shared_head.head, hidden)` 出 logits——这里的 `head` 就是 lm_head。

**(5) 权重来源：同一份 checkpoint 的分流**

这是本模块最关键的一环。target 侧，[pangu_v2_moe.py:L2421-L2432](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2421-L2432) 定义 `get_spec_layer_idx_from_weight_name`：如果权重名以 `model.layers.{N+i}.` 开头（N 为主模型层数，i < K），返回该 draft 层号，否则返回 `None`。

[pangu_v2_moe.py:L2300-L2305](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2300-L2305) 在 target 模型 `load_weights` 的 `_skip_weight` 里调用它：**MTP 层权重对主模型一律跳过**（与跳过 `rotary_emb.inv_freq`、tied lm_head 并列的第三个跳过条件）。

draft 侧，[pangu_v2_moe_mtp.py:L246-L256](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L246-L256) 的 `get_spec_layer` 做镜像判断：名字里的层号减去 `num_hidden_layers` 落在 `[0, K)` 才收。于是同一份权重流被两个模型「分拣」：

```
checkpoint 权重流
 ├─ model.layers.0 ~ N-1   → target 模型（draft 侧 get_spec_layer 返回 None → continue 跳过）
 └─ model.layers.N ~ N+K-1 → draft 模型（target 侧 _skip_weight 返回 True → 跳过）
```

**(6) 权重改名：`_rewrite_spec_layer_name`**

draft 模型的 Python 结构与 checkpoint 命名并不一一对应，[pangu_v2_moe_mtp.py:L364-L394](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L364-L394) 负责翻译：

- 名字含 `enorm` / `hnorm` / `eh_proj` / `shared_head` 的：直接落到本层（第 370-376 行的白名单）；
- 名字含 `embed_tokens` 的共享权重：改写到顶层 `model.`；
- 其余（attention、MoE 等）：在层号后插入 `.mtp_block.`，例如
  `model.layers.60.self_attn.q_a_proj.weight` → `model.layers.60.mtp_block.self_attn.q_a_proj.weight`。

随后的 [pangu_v2_moe_mtp.py:L258-L362](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L258-L362) `load_weights` 与主模型的加载器同构：`gate/up` 堆叠映射 + `FusedMoE.make_expert_params_mapping` 专家映射 + 普通参数兜底，逐个调用参数自带的 `weight_loader` 完成 TP/EP 切分。

**(7) 共享权重省显存**

[pangu_v2_moe_mtp.py:L396-L403](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe_mtp.py#L396-L403) 的 `set_shared_weight` 把 draft 的 `embed_tokens` 与每层的 `shared_head.head` **删除后指向 target 模型的同名对象**。调用方在 [patch_eagle.py:L451-L464](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eagle.py#L451-L464)：检测到 MTP 模型时无条件 `share_lm_head = True` 并调用它。词表 × hidden 的两份大矩阵因此只存一份。

**(8) draft 模型如何被 vLLM 认识**

两步：

- [v1/models/__init__.py:L17-L22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/__init__.py#L17-L22) 把 `OpenPanguV2MTPModel` 注册到 vLLM `ModelRegistry`（u2-l1 讲过的 `omni_custom_models` 入口的落地处）；
- [patch_speculative.py:L48-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_speculative.py#L48-L83) 补丁替换 `SpeculativeConfig.hf_config_override`：把 draft 的 `model_type` 从 `openpangu_v2` 改写为 `mtp`，并把 `n_predict` 设为 `num_nextn_predict_layers`、`architectures` 设为 `["OpenPanguV2MTPModel"]`——这正是 vLLM 内置 MTP 提议器（EagleProposer 家族）识别 draft 的方式。同文件 [patch_speculative.py:L31-L45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_speculative.py#L31-L45) 还把 `"mtp"` 扩进了 vLLM 的 `MTPModelTypes` 类型白名单。

**(9) draft 的图捕获**

[npu_model_runner.py:L697-L728](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L697-L728)：`n_predict == 1` 时把整个 drafter 包进 `ACLGraphWrapper`；`n_predict > 1` 时逐个 MTP 层包图，装入 `drafter.model.model.wrapped_layers`——正好被 4.1.3 第 (3) 点的 `wrapped_layers` 分支消费。图编译原理留待 u5-l2。

#### 4.1.4 代码实践

**实践目标**：亲手走通「`--num-speculative-tokens 3` 从 ansible 到 vLLM 引擎」的传递链，并追踪 MTP 权重的分流路径。

**操作步骤**（源码阅读型，无需 NPU）：

1. 打开 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L147](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L147)，确认 prefill 命令里有 `--num-speculative-tokens 3`（decode 命令第 243 行同样有）。
2. 追到 [tools/scripts/pd_run.sh:L391-L412](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L391-L412)：`NUM_SPECULATIVE_TOKENS != 0` 时自动追加 `--enable-mtp`（第 392-394 行），并把数值透传给 `start_api_servers.py`。
3. 追到 [tools/scripts/start_api_servers.py:L196-L197](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L196-L197)：`--enable-mtp` 最终被拼成 vLLM 的 `--speculative_config '{"method": "mtp", "num_speculative_tokens": 3}'`。
4. 在引擎侧确认 `num_speculative_tokens`（记作 k）的两个消费点：
   - [npu_model_runner.py:L190-L195](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L190-L195) 按 k 预分配 spec decode 的 pinned buffer；
   - [npu_model_runner.py:L218-L231](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L218-L231) `_calc_spec_decode_metadata` 用带注释的数值例子计算三组索引。
5. 手工画权重分流表：假设 `num_hidden_layers=90`、`num_nextn_predict_layers=1`，写出 `model.layers.90.eh_proj.weight` 与 `model.layers.89.mlp.gate_proj.weight` 各自被哪个模型加载、经过哪次改名。

**需要观察的现象 / 预期结果**：

- 步骤 3 的 JSON 里 `method` 是 `mtp`；对照 omni-npu 自带示例 [serve-pd-disaggregate.sh:L132](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/examples/serve-pd-disaggregate.sh#L132) 使用的是 `deepseek_mtp` 且 k=1——两者走不同 `MTPModelTypes` 分支，但 draft 加载逻辑同源。
- `_calc_spec_decode_metadata` 注释里的例子：`num_draft_tokens=[3,0,2,0,1]` 时 `num_sampled_tokens = num_draft_tokens + 1 = [4,1,3,1,2]`，`bonus_logits_indices = [3,4,7,8,10]`——每个请求恰好一个 bonus 位置，即「最后一个被验证 token 的下一位」。
- 步骤 5 的预期：`model.layers.90.eh_proj.weight` 被 draft 模型加载（白名单直落，不改名）；`model.layers.89.mlp.gate_proj.weight` 被 target 模型加载（对 draft 的 `get_spec_layer` 返回 None）。
- 权重中 `num_nextn_predict_layers` 的实际取值由具体 checkpoint 的 config.json 决定，**待本地验证**（部署模板用的是 k=3）。

**k 如何影响吞吐（推导）**：设第 i 位 draft 的接受概率为 \(\alpha_i\)，则每步期望采纳 token 数为

\[ E[\text{tokens per step}] = \sum_{i=1}^{k} \prod_{j=1}^{i} \alpha_j + 1 \]

（连续接受 i 个再算上 bonus）。k 越大、接受率越高，一次前向摊薄的访存越多；但 target 每步要算 k+1 个 token，k 过大或接受率低时算力浪费反噬。所以 k 是「接受率 × 算力余量」的折中——这就是模板选 3 而不是 10 的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MTP draft 层要用 `eh_proj` 拼接 embedding 和上一步隐状态，而不是只吃 token id？

**参考答案**：上一轮 decode 已经算出了每个位置的完整隐状态，它包含了比 embedding 丰富得多的上下文信息。MTP 的设计让 draft 「站在主模型的肩膀上」预测，等于免费拿到一层深度；只用 token id 则相当于让 draft 从头建立表征，需要更多层才能达到同等接受率。

**练习 2**：把 `--num-speculative-tokens` 从 3 改成 1，draft 侧和图捕获侧各会发生什么变化？

**参考答案**：k=1 时每步 draft 只提议 1 个 token，target 每步前向 2 个 token（1 draft + 1 bonus），接受收益上限降为每步 2 token；图捕获上没有变化——`ACLGraphWrapper` 的分支依据是 `n_predict`（即 checkpoint 的 `num_nextn_predict_layers`，[npu_model_runner.py:L698-L699](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L698-L699)），不是 k。k 与 MTP 层数是两个不同的旋钮。

**练习 3**：draft 模型里 `wrapped_layers` 为什么设计成 `dict[str, ACLGraphWrapper]` 而不是像主模型那样整模型包一层？

**参考答案**：多个 MTP 层在一步投机解码中是**串行依赖**的（第 i 层的输出是第 i+1 层的输入，中间还要过采样），无法整体捕获成一张静态图；每层单独包图（键就是层号字符串），层间数据流留在图外，既保住层内算子融合收益，又不破坏「出 logits → 采样 → 再进下一层」的动态控制流。`n_predict == 1` 时整模型只有一层 draft 且无层间切换，才允许整模型包图。

### 4.2 采样器接口：NPUSamplerV1 与 top-k/top-p

#### 4.2.1 概念说明

采样器是「logits → token id」的最后一公里。omni-npu 没有重写采样，而是**继承 vLLM V1 的 `Sampler` 并做 NPU 特化**：

- 把 CPU 友好的 `torch.multinomial` 路径换成昇腾融合算子 `torch_npu.npu_top_k_top_p_sample`（一步完成 top-k/top-p 截断 + 采样 + 返回处理后的 logits）；
- 随机数 `q` 的生成挪到独立 NPU 流（dsa_stream）上，与主计算流重叠；
- 提供可选的「惩罚缓存」路径：把 presence/frequency/repetition penalty 的统计从全局 logits 矩阵换成按请求维护的 token 计数表，省显存省带宽。

#### 4.2.2 核心流程

普通 decode 的采样路径（非投机）：

```
logits [num_reqs, vocab]
 → (可选) logits processors
 → (可选) penalties（惩罚缓存开启时）
 → all_greedy ? argmax
             : 温度缩放 → NPUTopKTopPSampler（top-k/top-p 截断 + Gumbel-max 采样）
 → 温度≈0 的请求逐个用 argmax 覆盖（贪心与随机混批）
 → sampled_token_ids [num_reqs, 1]（int32）
```

#### 4.2.3 源码精读

**(1) 采样器的构造与装配**

[sampler.py:L47-L51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/sampler.py#L47-L51) `NPUSamplerV1.__init__`：建 dsa_stream 并实例化 `NPUTopKTopPSampler`。装配点在 [npu_model_runner.py:L141-L144](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L141-L144)：runner 始终建 `NPUSamplerV1`；**开启投机解码且当前是流水线最后一秩时**才建 `NPURejectionSampler(self.sampler)`——拒绝采样器内部复用同一个采样器实例（含同一条 dsa_stream）。

**(2) 双路径开关**

[sampler.py:L60-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/sampler.py#L60-L62) 的 `if not ENABLE_NPU_PENALTY_CACHE: return super().forward(...)` 是总开关（环境变量 `OMNI_NPU_PENALTY_CACHE=1` 打开本地路径，默认关，见 [sampler.py:L18-L19](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/sampler.py#L18-L19)）。

**(3) 贪心 / 温度 / 采样三段**

[sampler.py:L92-L116](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/sampler.py#L92-L116)：

- 全贪心批：直接 `logits.argmax`（第 92-93 行）；
- 否则先除温度（温度小于 FP32_EPS 的视为 0，不除，第 95-97 行），再调 `topk_topp_sampler`（第 99-104 行）；
- 混批兜底：温度为 0 的请求用贪心结果逐行覆盖（第 106-114 行）——因为 top-k/top-p 融合算子按随机采样处理所有行。

**(4) top-k/top-p 的 NPU 融合实现**

[topk_topp_sampler.py:L26-L43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/ops/topk_topp_sampler.py#L26-L43) `apply_top_k_top_p_npu`：logits/k/p 统一转 bfloat16，缺省 k 补成词表大小、缺省 p 补成 1.0（即「不截断」），然后一次调用 `torch_npu.npu_top_k_top_p_sample(logits, k, p, q=None, is_need_logits=True)`。这个 CANN 融合算子替代了 vLLM 原生的「sort → cumsum → mask」全词表排序路径——词表 15 万级时排序是采样的大头。

**(5) Gumbel-max 与旁路流**

[topk_topp_sampler.py:L47-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/ops/topk_topp_sampler.py#L47-L68) `generate_coins`：在 dsa_stream 上生成指数分布随机数 `q`（有独立 seed 的请求用各自 generator 覆盖对应行，保证可复现），随后 `default_stream().wait_stream(stream)` 建立同步。纯 torch 回退版 [topk_topp_sampler.py:L137-L181](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/ops/topk_topp_sampler.py#L137-L181) `random_sample` 末尾的 `probs.div_(q).argmax(dim=-1)` 就是 2.5 节推导的指数竞赛。

**(6) 完整融合路径与硬件分叉**

[topk_topp_sampler.py:L184-L231](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/ops/topk_topp_sampler.py#L184-L231)：`NPUTopKTopPSampler.__init__` 按硬件分叉——Ascend950 走继承来的 `forward_native`，否则把 `apply_top_k_top_p` 换成 NPU 版并绑定 `forward_npu`。`forward_npu` 有两档：配置 `disable_npu_top_k_top_p_sample` 时退回「截断 + softmax + 指数竞赛」的组合作业（第 201-211 行）；默认在第 222-223 行先 `generate_coins` 再把 `q` 一起喂给融合算子——随机数生成与算子解耦，是流重叠的前提。

#### 4.2.4 代码实践

**实践目标**：通过单测理解采样器的调用契约（无需 NPU）。

**操作步骤**：

1. 阅读 [tests/unit/sample/test_sampler.py:L66](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/sample/test_sampler.py#L66) 附近：测试用 `MagicMock(return_value=(torch.tensor([[2]]), None))` 替换 `sampler.topk_topp_sampler`，断言 `NPUSamplerV1.forward` 把 `(logits, generators, top_k, top_p)` 原样传给它并采回 token 2。
2. 在 `components/omni-npu/` 下运行采样相关单测（注意：`test_sampler.py` 顶部直接 `import omni_npu.sample.sampler`，而该模块内部 `import torch_npu`，因此**要求环境能 import torch_npu**——部署容器内天然满足，普通 x86 开发机需已安装 torch_npu 包但无需真实 NPU 设备，具体调用已被 `patch('omni_npu.sample.sampler.torch_npu', MagicMock())` 替换；相比之下，4.3.4 的 `test_rejection_sampler.py` 在 fixture 里把伪造 torch_npu 塞进 `sys.modules` 后才 import，任何 x86 主机都能跑）：

   ```bash
   cd components/omni-npu
   python -m pytest tests/unit/sample/test_sampler.py -v
   ```

3. 修改实践（可选）：在 `NPUSamplerV1.forward` 的贪心分支（[sampler.py:L92-L93](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/sampler.py#L92-L93)）临时加一行 `print`，重跑单测观察该分支何时命中。

**需要观察的现象 / 预期结果**：步骤 2 全部用例通过（如 `test_bypass_when_cache_disabled`、`test_forward_complex_paths`）；步骤 3 应看到贪心分支仅在 `all_greedy=True` 的用例中打印。若本机无法 import torch_npu，标注**待本地验证**（可在部署容器内执行同样命令）。

#### 4.2.5 小练习与答案

**练习 1**：融合算子 `npu_top_k_top_p_sample` 已经能自己采样，为什么还要在外面先生成 `q` 再传进去？

**参考答案**：把随机数生成从算子里拆出来，才能把它挪到独立的 dsa_stream 上与主计算重叠（`generate_coins` 的 stream 分支）；同时 `q` 的生成要遵守逐请求的 generator/seed（可复现性要求），这段逻辑通用且需要 Python 侧循环，放进 C++ 算子里反而僵化。

**练习 2**：`NPUSamplerV1` 与 `NPURejectionSampler` 为什么要共享同一个 `sampler` 实例？

**参考答案**：[npu_model_runner.py:L143-L144](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L143-L144) 把 `self.sampler` 传进 `NPURejectionSampler`。拒绝采样里「bonus token」与「恢复 token」的采样（见 4.4 节）本质仍是普通采样，复用同一实例即复用同一条 dsa_stream 与同一套 top-k/top-p 配置，避免重复建流与行为不一致。

### 4.3 拒绝采样：验证 draft 并保证分布正确

#### 4.3.1 概念说明

拒绝采样（rejection sampling）是投机解码「不降质量」的数学保证。设 target 分布为 \( p \)，draft 提议分布为 \( q \)，draft 给出的候选 token 为 \( t \sim q \)。验证规则：

- **贪心请求**：\( t \) 被接受当且仅当 \( t = \arg\max p \)（draft 必须猜中 target 的 argmax）。
- **随机请求**：取 \( u \sim \mathrm{U}(0,1) \)，接受条件为

\[ \frac{p(t)}{q(t)} \ge u \quad\Longleftrightarrow\quad u \le \min\!\left(1, \frac{p(t)}{q(t)}\right) \]

即接受概率是 \( \min(1, p(t)/q(t)) \)：draft 越「确信」（q 大）而 target 越「否定」（p 小）的 token 越容易被拒。

- **拒绝后的恢复采样**：从残差分布采样一个替代 token。代码实现是对

\[ r(x) = p(x) - q(x) \]

做指数竞赛（`argmax(r/q)`）；draft 处的 \( r \) 被压低，target 偏好而 draft 低估的 token 被抬高，归一化后恰是标准残差分布 \( (p-q)_+ / Z \)。draft 无概率输出时（如 ngram 草稿），代码把 draft token 位置的 \( p \) 减 1 达到同样的压制效果。

- **bonus token**：无论接受多少，每个请求每步**至少**前进 1 个 token——在「最后一个被接受 token 的下一位」由 target 采样出的 token 就是 bonus。这就是 4.1.4 里 `bonus_logits_indices` 每请求恰一个的原因。

三条合起来可以证明：最终输出的每个 token 都精确服从 \( p \)——投机解码是**无损加速**。

#### 4.3.2 核心流程

`NPURejectionSampler.forward` 的输入输出形状（注释原文）：

```
draft_probs  : [num_tokens, vocab]         每个草稿 token 的提议概率（可为 None）
logits       : [num_tokens + batch, vocab]  target 对 k+1 个位置的 logits
输出          : [batch, max_spec_len + 1]    每请求一行，被拒位填 PLACEHOLDER
```

流程：

```
1. 从 logits 里切出 bonus_logits（每请求 1 行）→ 普通采样得 bonus_token_ids
2. 切出 target_logits（每请求 k 行）→ 温度/top-k/top-p 处理成 target_probs
3. 分两路：
   a. draft_probs == None（无概率草稿）:
      compute_probs_and_sample 直接采出 target_token_ids
      → simple_verify：贪心式逐位比对 draft_token_ids == target_token_ids
   b. draft_probs 存在（MTP 走这条）:
      compute_probs 得 target_probs
      → rejection_sample：
         - 贪心请求: accepted = (draft == target_argmax)
         - 随机请求: 采样恢复 token；accepted = (target_prob/draft_prob >= u)
         - select_tokens_by_accepted: 前缀截断 + 填恢复/bonus token
4. （可选）计算 logprobs
```

#### 4.3.3 源码精读

**(1) 构造与硬件分叉**

[rejection_sampler.py:L35-L42](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L35-L42)：继承 vLLM `RejectionSampler`，复用传入 sampler 的 dsa_stream；`use_npu_sample` 在「配置禁用融合算子」或「Ascend950」时为 False，走纯 torch 路径。

**(2) forward：切三段、分两路**

[rejection_sampler.py:L77-L97](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L77-L97) 用 `bonus_logits_indices` / `target_logits_indices`（即 `_calc_spec_decode_metadata` 算出的索引）从拼接的 logits 里切出两段，bonus 段直接走普通采样器；[rejection_sampler.py:L116-L158](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L116-L158) 按 `draft_probs` 是否为 None 分成 simple_verify / rejection_sample 两路。

**(3) 接受条件的代码落点**

[rejection_sampler.py:L503-L540](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L503-L540) `rejection_random_sample_native`：

- 第 526 行 `draft_prob = draft_probs.gather(1, draft_token_ids...)` 取出「draft 对它实际提议的那个 token」的提议概率；第 528 行同理取 target 概率；
- **第 529 行 `accepted = target_prob / draft_prob >= uniform_probs` 就是 4.3.1 的接受条件 \( p(t)/q(t) \ge u \)**，`uniform_probs` 由 vLLM 的 `generate_uniform_probs` 产生；
- 第 538 行把 `~is_greedy` 作为填充掩码传入——混批时贪心请求的结果已经在前面算好，这里只覆盖随机请求的行。

贪心对应物在 [rejection_sampler.py:L480-L500](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L480-L500)：`accepted = draft_token_ids == target_argmax`（第 489 行）。

**(4) 恢复 token 的残差采样**

[rejection_sampler.py:L450-L477](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L450-L477) `sample_recovered_tokens_native`：第 463-468 行无 draft_probs 时 `sample_probs = target_probs.clone()` 并把 draft token 位置减 1；第 470 行有 draft_probs 时 `sample_probs = target_probs - draft_probs`（即 \( p - q \)）；第 477 行 `(sample_probs / q).argmax()`——残差分布上的指数竞赛。随机数 `q` 的生成在 [rejection_sampler.py:L392-L447](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L392-L447)，同样支持旁路流与逐请求 generator。

**(5) 前缀截断与 bonus 回填**

[rejection_sampler.py:L543-L602](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L543-L602) `select_tokens_by_accepted` 是纯张量版的「逐位验证」：

- 第 572-577 行把 accepted 摆回 `[batch, max_spec_len+1]` 矩阵后做 `cumprod`——连乘实现「**一旦某位被拒，后面全部作废**」；
- 第 575 行 `accepted_num = accepted_mat.sum(-1)` 得每请求接受数；
- 第 579-586 行恢复矩阵在 `num_draft_tokens` 位置预放 bonus；
- 第 588-597 行：被拒位置填 `PLACEHOLDER_TOKEN_ID`，然后在第 `accepted_num` 列统一放入「恢复矩阵该列的值」——若全接受，该列恰好是 bonus；若有拒绝，该列恰好是拒绝位的恢复 token。一个 `scatter` 同时覆盖两种情况。

**(6) 贪心温度的归一化技巧**

[rejection_sampler.py:L605-L678](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L605-L678) `compute_probs_and_sample`：把 vLLM 的 `GREEDY_TEMPERATURE`（0）替换为 [rejection_sampler.py:L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L32) 的 `NEW_GREEDY_TEMPERATURE = 1e-6`，让贪心请求也走「温度极小的随机采样」，从而整批统一进 `npu_top_k_top_p_sample` 融合算子（第 671-678 行），不必为贪心单独开分支；温度 1e-6 下 softmax 近似 one-hot，argmax 语义不变。

**(7) 主流程编队**

[rejection_sampler.py:L177-L283](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L177-L283) `rejection_sample` 把上述部件按「先贪心、后随机」编队：第 217-234 行先做贪心请求的验证（全贪心批直接返回）；第 238-253 行在旁路流上生成 uniform 概率；第 257-266 行采恢复 token；第 269-282 行做随机请求的验证与合并。`expand_batch_to_tokens`（[rejection_sampler.py:L352-L389](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L352-L389)）负责把每请求的采样参数（温度、top-k、top-p）复制扩展到该请求的每个 draft 位置——注释里的例子 `[a,b,c] + cu=[2,5,6] → [a,a,b,b,b,c]`。

#### 4.3.4 代码实践

**实践目标**：用真实单测验证接受条件与残差采样的行为（CPU 可跑）。

**操作步骤**：

1. 阅读 fixture [test_rejection_sampler.py:L29-L87](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/sample/test_rejection_sampler.py#L29-L87)：它用 `types.ModuleType` 伪造了 `torch_npu`（`npu_top_k_top_p_sample` 等全为 lambda），使模块能在无 NPU 机器上 import。
2. 精读两个用例：
   - [test_rejection_sampler.py:L189-L205](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/sample/test_rejection_sampler.py#L189-L205) `test_rejection_greedy_sample_native_basic`：构造 draft 与 target argmax，断言输出里接受位/拒绝位/占位符的排布；
   - [test_rejection_sampler.py:L206-L229](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/sample/test_rejection_sampler.py#L206-L229) `test_rejection_random_sample_native_with_draft_probs`：带 draft_probs 的随机路径。
3. 运行：

   ```bash
   cd components/omni-npu
   python -m pytest tests/unit/sample/test_rejection_sampler.py -v
   ```

4. 数值实验（示例代码，非项目原有）：

   ```python
   import torch
   torch.manual_seed(0)
   # 词表 4，draft 提议 token 2，q(2)=0.8，target p(2)=0.4
   draft_token_ids = torch.tensor([2])
   draft_probs = torch.tensor([[0.1, 0.05, 0.8, 0.05]])
   target_probs = torch.tensor([[0.3, 0.1, 0.4, 0.2]])
   # 接受概率 = p/q = 0.4/0.8 = 0.5
   u = torch.tensor([0.6])          # u > 0.5 → 拒绝
   accepted = (target_probs.gather(1, draft_token_ids.view(-1, 1)).view(-1)
               / draft_probs.gather(1, draft_token_ids.view(-1, 1)).view(-1)) >= u
   print(accepted)                   # tensor([False])
   # 残差分布 p-q = [0.2, 0.05, -0.4, 0.15]，token 0 胜出
   print((target_probs - draft_probs).argmax(-1))   # tensor([0])
   ```

**需要观察的现象 / 预期结果**：步骤 3 全绿；步骤 4 打印 `False` 与 `0`——与手推一致：该 draft token 以概率 0.5 被接受，本例 u=0.6 被拒，恢复采样落在残差最大的 token 0。

#### 4.3.5 小练习与答案

**练习 1**：若 draft 与 target 完全一致（\( q = p \)），接受概率是多少？若 draft 均匀提议（q 为常数 1/V）呢？

**参考答案**：\( q = p \) 时 \( p(t)/q(t) = 1 \ge u \) 恒成立，接受概率 1（理想情况，k 个全收 + 1 个 bonus，每步 k+1 token）。均匀 draft 时接受概率为 \( V \cdot p(t) \)，target 越尖锐（max p 大）越高，但平均只有 \( V \int p^2 \) 量级，词表大时极低——所以 draft 质量直接决定投机解码收益。

**练习 2**：`select_tokens_by_accepted` 里的 `cumprod` 起什么作用？换成 `cumsum` 会怎样？

**参考答案**：`cumprod` 沿序列连乘 0/1 接受标记，任何一位为 0 之后所有位都变 0，实现「首位拒绝即全部作废」的前缀语义。`cumsum` 只计数不截断，后面侥幸「接受」的 token 会被错误保留——它们的 target 概率是在错误前缀下算出的，不再服从 target 分布。

**练习 3**：为什么 bonus token 能保证「每步至少前进 1 个」，且不破坏分布正确性？

**参考答案**：bonus 位取的是 target logits 在「最后一个被采纳 token 的下一位」的采样（[rejection_sampler.py:L85-L97](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/sample/rejection_sampler.py#L85-L97)）。由于 accepted_num 与恢复矩阵的对位设计（第 597 行的 `scatter_`），全接受时它排在 k 个 draft 之后，有拒绝时它被同一列的恢复 token 替换——无论哪种情形，输出序列每一步的最后一个 token 都精确来自 target 在正确前缀下的采样。

## 5. 综合实践

**任务：端到端追踪一次「三 token 投机」的数据流。**

以部署模板的 `--num-speculative-tokens 3`（k=3）为线索，串联本讲全部知识点，产出一份分析文档：

1. **参数链**（对应 4.1.4）：写出 k=3 从 ansible 模板 → `pd_run.sh` → `start_api_servers.py` → `--speculative_config` JSON 的每一跳，标注文件与行号。
2. **draft 侧**（对应 4.1）：画出 `OpenPanguV2MultiTokenPredictorLayer` 前向的数据流图，标出 enorm/hnorm/eh_proj/mtp_block/shared_head 的输入输出形状；说明 `spec_step_idx % num_mtp_layers` 在 k=3、`num_nextn_predict_layers=1` 与 `=3` 两种假设下的层选择差异（实际值**待本地验证**：查看权重目录 config.json 的 `num_nextn_predict_layers`）。
3. **验证侧**（对应 4.3）：对下面这组手造数据（示例数据）完整手推一遍 `rejection_sample`：
   - 单请求，k=3，draft token = `[A, B, C]`；
   - target argmax = `[A, X, C]`（第 2 位不一致）；draft_probs 与 target_probs 自定（如练习中的构造）；
   - 写出输出 `[batch, 4]` 矩阵的每一列：哪些位是 draft token、哪位是恢复 token、bonus 放在哪。
4. **有环境时上机验证**：在已部署服务（u1-l4 的 1P1D BF16 环境）上发一个固定 seed 的流式请求，把模板中 `--num-speculative-tokens` 改为 1 重启 `run_server` tag，对比两个配置下的 tokens/s 与日志中 draft 相关启动行（如 `Wrapped ... mtp layers`）。无环境则标注**待本地验证**。

**预期结果**：第 3 步输出应为 `[A, 恢复token(R), PLACEHOLDER, PLACEHOLDER]` 之外再在「第 accepted_num=1 列」放恢复 token 的形式——具体为 `[A, R, -1, -1]`（-1 表 PLACEHOLDER），其中 R 来自残差分布 \( p - q \) 的采样；bonus 因第 2 位被拒而不出现（恢复 token 已占位）。第 4 步预期 k=1 时吞吐下降、逐 token 延迟基本不变。

## 6. 本讲小结

- openPangu-2.0 的 MTP 把 draft 层藏在主模型 checkpoint 里：层号 ≥ `num_hidden_layers` 的权重属于 draft；target 的 `_skip_weight` 与 draft 的 `get_spec_layer` 互为镜像完成分流，`_rewrite_spec_layer_name` 负责结构对位。
- draft 层 = enorm + hnorm + eh_proj + 复用的 `OpenPanguV2DecoderLayer`（mtp_block）+ shared_head；embedding 与 lm_head 经 `set_shared_weight` 与 target 共享，省两份大矩阵显存。
- `--num-speculative-tokens`（k）经 ansible → pd_run.sh → start_api_servers.py 变成 `--speculative_config '{"method":"mtp","num_speculative_tokens":k}'`；k 与 checkpoint 的 MTP 层数是两个独立旋钮，层选择用 `spec_step_idx % K` 轮转。
- NPU 采样器把「top-k/top-p 截断 + 采样」交给融合算子 `npu_top_k_top_p_sample`，随机数在独立 dsa_stream 上预生成，贪心请求用 1e-6 温度归一进随机路径。
- 拒绝采样接受条件：贪心为 draft == target argmax，随机为 \( p(t)/q(t) \ge u \)；拒绝后从残差 \( p - q \) 采样恢复 token，`cumprod` 保证前缀截断，bonus token 保证每步至少前进 1 个——三者合力使输出分布与 target 严格一致。

## 7. 下一步学习建议

- **u5-l2（图编译）**：本讲多次出现 `ACLGraphWrapper` 与 `wrapped_layers`，draft 层为什么只能逐层包图、ACL Graph 如何捕获含流同步的前向，在那一讲展开。
- **u10-l2（测试体系）**：本讲的实践大量依赖 `tests/unit/sample/` 的无 NPU 单测，下一阶段应学会用 `run_tests.sh` 跑全量 unit 层并仿写用例。
- **镜内延伸阅读**：在部署容器里阅读 vLLM 上游 `vllm/v1/spec_decode/eagle.py`（`EagleProposer`）与 `vllm/v1/sample/rejection_sampler.py`，对照 omni-npu 版本看「native 重写替换 CUDA kernel」这一适配手法的原始形态。
- **回看 u3-l1 的 `load_weights` 四分支**：本讲 draft 的加载器与主模型同构，读完本讲再回看主模型加载器会有「同一套路两处落地」的贯通感。
