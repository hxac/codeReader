# 性能、扩展点与二次开发

> 这是整本手册的收官篇。前面六单元我们已经把 SynthID Text 从「概念 → 施加 → 集成 → 检测 → 训练 → 理论/测试」走了一遍。本讲不再引入新机制，而是换一个视角：**站在二次开发者的角度，看这个参考实现在哪里可以改、在哪里该取舍、在哪里必须止步并转向官方实现**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 SynthID Text 暴露的**三类扩展面**（水印配置、打分函数、检测器训练超参），并知道各自改哪个文件、改哪个函数。
2. 理解「稀疏 top_k」这一贯穿施加侧的设计**如何把延迟与显存从词表级压到 top_k 级**，以及它的代价（必须重写采样循环、必须做下标回映）。
3. 说清楚为什么这个仓库**不适合直接用于生产**（静态公开密钥、非密码学哈希、子类粗糙边缘），以及**生产化的正确路径**是 HuggingFace Transformers 官方实现。
4. 独立设计一个**不改主流程**的二次开发方案：用自定义加权打分替换 Mean 打分。

## 2. 前置知识

本讲是综合性的，默认你已经读过下面两讲（本讲的依赖）：

- **u3-l3 得分更新：锦标赛与 distortionary 变体**：`num_leaves` 如何作为「强度—失真」旋钮，在 `update_scores` 与 `update_scores_distortionary` 之间派发。
- **u6-l3 数据处理与端到端检测 API**：`train_best_detector` 的超参搜索、`BayesianDetector.score` 的端到端用法。

此外需要两个贯穿全手册的共识：

- **框架即分水岭**：施加侧 PyTorch、检测侧 JAX/Flax，二者靠 **g 值**（形状 `[batch, seq, depth]` 的二进制指纹）衔接。
- **文档与源码冲突时以源码为准**：本讲会再次遇到 README/Notebook 与源码 API 漂移的真实案例，这是「参考实现」最需要警惕的地方。

三个术语复习：

- **depth（水印深度）**：等于 `len(keys)`，默认配置里是 30。
- **top_k**：每一步只在水印前 top_k 个候选 token 上施加水印，而不是整个词表。
- **g 值**：由 ngram 上下文 + 水印密钥经哈希取位得到的 0/1 比特，是打分的唯一输入。

## 3. 本讲源码地图

| 文件 | 归属 | 本讲关注什么 |
| --- | --- | --- |
| `src/synthid_text/synthid_mixin.py` | 施加侧 / PyTorch | 静态配置 `DEFAULT_WATERMARKING_CONFIG`、warper 注入、稀疏采样循环 |
| `src/synthid_text/logits_processing.py` | 施加侧 / PyTorch | 处理器构造旋钮、`num_leaves` 派发、`apply_top_k` 逃生阀 |
| `src/synthid_text/detector_mean.py` | 检测侧 / JAX | 免训练打分函数的**签名契约**与 `weights` 扩展参数 |
| `src/synthid_text/detector_bayesian.py` | 检测侧 / JAX | 训练超参搜索、显存注记、端到端 `score` API |
| `README.md` | 文档 | 生产化免责声明、与源码不一致之处 |
| `notebooks/synthid_text_huggingface_integration.ipynb` | 示例 | 打分函数的真实调用点（二次开发的落点） |

> 说明：`detector_mean.py` 不在任务规格的「关键源码」清单里，但它正是「自定义打分」要落地的文件，且本讲作者已通读，故纳入地图。

---

## 4. 核心概念与源码讲解

### 4.1 配置与打分扩展点

#### 4.1.1 概念说明

「扩展点（extension point）」是指**架构上预留的、可以替换而不牵动主流程的接缝**。SynthID Text 虽然体量不大，但因为施加与检测被 g 值彻底解耦，它天然留出了三类扩展面：

1. **水印配置面**（施加侧）：决定「这次水印长什么样」——`ngram_len`、`keys`、`context_history_size`、`num_leaves` 等。
2. **打分函数面**（检测侧·免训练）：决定「怎么把 g 值聚合成一个分数」——`mean_score`、`weighted_mean_score`，以及任何同签名的新函数。
3. **检测器训练面**（检测侧·贝叶斯）：决定「怎么训出更强的检测器」——`l2_weights`、`n_epochs`、截断长度等超参网格。

关键直觉：**检测侧的打分/训练完全不触碰施加侧主流程**。你换一个打分函数，绝不会影响 `watermarked_call` 怎么埋水印；反之亦然。这种解耦是二次开发能「不改主流程」的根本原因。

#### 4.1.2 核心流程

三类扩展面的旋钮与落点：

```
扩展面                旋钮                            落点函数/位置
─────────────────────────────────────────────────────────────────
水印配置              ngram_len, keys,               SynthIDLogitsProcessor.__init__
                      context_history_size,
                      num_leaves, apply_top_k,
                      skip_first_ngram_calls
打分函数(免训练)       聚合方式 / weights             detector_mean.mean_score /
                                                     weighted_mean_score (可替换)
检测器训练(贝叶斯)     l2_weights, n_epochs,          BayesianDetector.train_best_detector
                      learning_rate,
                      pos/neg_truncation_length
```

「换配置/换打分/调超参」三件事互不耦合：你可以同时用自定义 keys、num_leaves=3 的水印，配一个自定义加权打分，再用一组自己调过的 `l2_weights` 训贝叶斯检测器。

#### 4.1.3 源码精读

**(a) 水印配置：一个静态常量 + 运行时合并**

仓库把默认水印配置硬编码成一个不可变字典：

[synthid_mixin.py:27-67](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L27-L67) —— `DEFAULT_WATERMARKING_CONFIG`：用 `immutabledict` 包裹，含 `ngram_len=5`（对应论文 H=4）、30 个 `keys`（故 depth=30）、`context_history_size=1024`、`device`。这是「最想自定义」的对象：换 keys 就是换一种全新水印。

而这个静态配置是在构造 warper 时与运行时参数合并的：

[synthid_mixin.py:73-83](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L73-L83) —— `_construct_warper_list` 用 `**DEFAULT_WATERMARKING_CONFIG, **extra_params` 实例化**唯一一个** `SynthIDLogitsProcessor`。注意 warper 列表长度恒为 1：SynthID 把温度/top_k/水印全收编进这一个 processor，替换掉了 HF 默认的一整组 warper。

**(b) 处理器构造函数：所有旋钮的集中地**

真正生效的配置旋钮全部集中在处理器的关键字构造函数里：

[logits_processing.py:135-202](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L135-L202) —— `SynthIDLogitsProcessor.__init__`。可调字段：

- `ngram_len`、`keys`、`context_history_size`、`device`：来自配置；
- `temperature`、`top_k`：运行时采样参数（构造期即校验 `temperature>0`、`top_k>1`，见 L182、L199）；
- `skip_first_ngram_calls`、`apply_top_k`：两个**逃生阀**（见 4.2）；
- `num_leaves`：锦标赛叶子数，默认 2。

其中 `num_leaves` 直接决定走哪条打分路径，是「强度—失真」的唯一旋钮（承接 u3-l3）：

[logits_processing.py:299-304](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L299-L304) —— `_num_leaves == 2` 时走省算的标准 `update_scores`，否则走通用 `update_scores_distortionary`。想加大水印强度？把 `num_leaves` 设成 3 即可（但失真也更大）。

**(c) 打分函数：签名契约就是扩展点**

免训练打分函数集中在 `detector_mean.py`，它们的**函数签名本身就是扩展契约**：

[detector_mean.py:22-41](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L22-L41) —— `mean_score(g_values, mask)`：对未屏蔽的 g 值做算术平均，无任何可学习参数。逐样本公式为

\[
\text{mean\_score}_i = \frac{\sum_{t,d} g_{i,t,d}\, m_{i,t}}{D \cdot \sum_t m_{i,t}}
\]

其中 \(D\) 为 depth，\(m\) 为 mask。

[detector_mean.py:44-77](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L44-L77) —— `weighted_mean_score`：多了一个 `weights` 参数（L47），默认是从 10 线性递减到 1（L66），并归一化为「和等于 D」以保持与 Mean 同量纲（L69）。**这个 `weights` 参数就是仓库自带的扩展点**：想换一套深度方向加权，传一个形状 `[depth]` 的数组即可，零代码改动。

更关键的是：**任何签名为 `(g_values: [B,L,D], mask: [B,L]) -> scores: [B]` 的新函数都是 drop-in 替换**。这正是综合实践（第 5 节）要利用的契约。

**(d) 检测器训练：超参网格**

贝叶斯检测器的训练入口把「调超参」做成了显式旋钮：

[detector_bayesian.py:985-1068](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L985-L1068) —— `BayesianDetector.train_best_detector` 类方法。可调旋钮：`pos_truncation_length`、`neg_truncation_length`、`n_epochs`、`learning_rate`、`l2_weights`（L995-L1001）。

其中 `l2_weights` 是一个**网格**，每个候选 `l2_weight` 独立训练一轮、再内部选最优 epoch，最后留 CV loss 最低者（双重选择，承接 u6-l3）：

[detector_bayesian.py:940-983](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L940-L983) —— `train_best_detector_given_g_values`，默认网格 `np.logspace(-3, -2, num=4)`（L954）。

源码注释也直言这些是「该为你的数据调一调」的旋钮：

[detector_bayesian.py:1005-1008](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L1005-L1008) ——「tuning pos_truncation_length, neg_truncation_length, n_epochs, learning_rate and l2_weights can help improve the performance」。

#### 4.1.4 代码实践

**实践目标**：体验「打分函数扩展点」——不改任何源码，仅靠传参改变打分行为。

**操作步骤**（源码阅读 + 可选运行）：

1. 打开 `detector_mean.py`，确认 `weighted_mean_score` 的第三个参数 `weights` 默认是 `jnp.linspace(10, 1, depth)`。
2. 构造一段假的 g 值与 mask（形状 `[1, 20, 30]` 与 `[1, 20]`），分别调用：
   - `mean_score(g_values, mask)`
   - `weighted_mean_score(g_values, mask)`（默认权重）
   - `weighted_mean_score(g_values, mask, weights=jnp.linspace(1, 10, 30))`（**反转权重**：浅层权重低、深层权重高）

**需要观察的现象**：三次调用返回不同的 `[batch]` 分数；反转权重后分数会明显偏离默认加权的结果。

**预期结果**：默认加权（浅层权重高）与反转加权（深层权重高）给出不同分数，说明 `weights` 是一个**生效的、零改动的扩展点**。

> 如果本地没有 JAX 环境，可只做源码阅读：对照 L66 与 L69，口算「权重全为 1 时 `weighted_mean_score` 退化成 `mean_score`」，验证二者是同一族打分。

**待本地验证**：具体数值依赖你的随机 g 值，无法预判。

#### 4.1.5 小练习与答案

**练习 1**：想把水印从「不可见但温和」改成「更强但更失真」，最该动哪个旋钮？为什么？

> **答案**：`num_leaves`。它从 2 改成 3 会让单层推力从约 2 倍升到约 3 倍（u3-l3），g 值均值从约 0.75 升到约 0.875（u7-l1），更易检测但对 LM 分布扭曲更大。注意：工程上合理取值仅 2 与 3。

**练习 2**：`weighted_mean_score` 的 `weights` 传成「全 1 数组」后，结果与 `mean_score` 有何关系？

> **答案**：完全等价。因为权重经 L69 归一化为「和等于 D」，全 1 归一化后仍是 1，等权平均即 `mean_score`。这说明 `mean_score` 是 `weighted_mean_score` 的特例。

**练习 3**：为什么换一个打分函数不会影响水印施加？

> **答案**：施加侧（`watermarked_call`）与检测侧（打分函数）的唯一接口是 g 值这个不可变数据。打分函数只读 g 值与 mask，不回写任何状态，故二者完全解耦。

---

### 4.2 稀疏 top_k 性能取舍

#### 4.2.1 概念说明

「稀疏（sparse）」是 `SynthIDSparseTopKMixin` 类名里就写明的设计取向。它的核心想法是：**水印不必施加在整个词表上，只施加在 top_k 个最可能的候选 token 上就够了**。

为什么这样做能提速？默认配置 `depth=30`，词表 V 动辄几万到几十万。如果对每个候选都算 30 层 g 值并更新得分，单步成本约为 \(O(V \cdot D)\)；而只在 top_k（典型 40）个候选上做，成本降到 \(O(\text{top\_k} \cdot D)\)，缩小了 \(V/\text{top\_k}\) 倍。这对生成延迟是实打实的优化。

但天下没有免费的午餐。稀疏化的代价是：

- 标准 HF `LogitsProcessor.__call__` 是「稠密进、稠密出」的契约，承载不了「顺便返回下标映射」这种额外输出，所以必须禁用 `__call__`、改用带状态的 `watermarked_call`，并整体接管采样循环（见 u4-l1/u4-l2）。
- 在 top_k 子集上采样得到的是**局部下标** \([0, \text{top\_k})\)，必须再回映成**词表下标** \([0, V)\) 才能拼回序列。

#### 4.2.2 核心流程

施加侧单步的稀疏数据流：

```
稠密 logits [B, V]
   │  ÷ temperature
   ▼
scores_processed [B, V]
   │  torch.topk(k=top_k)
   ▼
scores_top_k [B, top_k]   +   top_k_indices [B, top_k]   ← 稀疏化在这里发生
   │  _compute_keys → get_gvals → update_scores (只在 top_k 上)
   ▼
updated_scores [B, top_k]            ← 仍是稀疏
   │  multinomial 采样
   ▼
next_tokens (局部下标 [B], 取值 0..top_k-1)
   │  torch.vmap(torch.take)(top_k_indices, next_tokens)
   ▼
真实词表 id [B]                       ← 回映到稠密空间，拼回 input_ids
```

两个「稀疏」关键点：第 3 步把计算量从词表级压到 top_k 级；最后一步必须把局部下标回映成词表 id。二者严格成对，缺一不可。

#### 4.2.3 源码精读

**(a) 设计意图写在 docstring 里**

源码两次明确说出「为了降延迟」的动机：

[synthid_mixin.py:90-97](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L90-L97) —— `_get_logits_warper` 的 docstring：「Only the SynthIDLogitsProcessor warper is constructed ... This is to improve the latency impact by watermarking by only considering the top_k indices for watermarking.」

[synthid_mixin.py:149-156](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L149-L156) —— `_sample` 的 docstring：「to preserve the top_k indices separately without making the logits dense ... This removes extra overhead of considering all possible indices for watermarking.」

**(b) 稀疏化发生在 `watermarked_call` 内部**

[logits_processing.py:245-259](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L245-L259) —— 先 `scores / temperature`，再 `torch.topk(scores_processed, k=self.top_k, dim=1)` 取稀疏候选，后续所有 g 值计算与得分更新都只在这 top_k 个候选上进行。函数最终返回的就是稀疏三元组：

[logits_processing.py:240-242](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L240-L242) —— 返回 `(updated_watermarked_scores [B, top_k], top_k_indices [B, top_k], scores_top_k [B, top_k])`，三者全是稀疏的 `[B, top_k]`。

**(c) 逃生阀 `apply_top_k`：想关掉稀疏化**

稀疏化不是强制的，构造函数留了开关：

[logits_processing.py:249-259](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L249-L259) —— 当 `apply_top_k=False` 时，`scores_top_k` 取全部词表得分，`top_k_indices` 退化为 `arange(vocab_size)`（恒等映射）。此时水印施加在整个词表上，最忠实于论文但最慢。注意：源码里 `torch.topk` 仍会在分支前先算一遍（即便不用），这是参考实现的一个小冗余。

**(d) 回映：从局部下标到词表 id**

稀疏采样的最后一步必须把局部下标翻译回真实词表 id：

[synthid_mixin.py:294-298](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L294-L298) —— 在改写的 `_sample` 中调用 `watermarked_call`，拿到 `indices_mapping`。

[synthid_mixin.py:335-339](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L335-L339) —— 用 `torch.vmap(torch.take, in_dims=0, out_dims=0)(indices_mapping, next_tokens)` 把局部下标回映成稠密词表 id。`assert indices_mapping is not None`（L335）放在 `do_sample` 之外，实质强制采样路径、杜绝让水印失效的贪心路径（承接 u4-l2）。

**(e) 性能的另一面：检测侧的显存**

稀疏 top_k 优化的是**生成延迟**；但**检测器训练**的显存瓶颈在别处——depth 的平方：

[detector_bayesian.py:542-544](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L542-L544) —— `train` 的 docstring 注记：一个 minibatch 约需

\[
\approx 32 \cdot \text{minibatch\_size} \cdot \text{seq\_len} \cdot D \cdot D \quad \text{比特}
\]

那个 \(D \cdot D\) 来自 `delta` 参数矩阵（形状 `[1,1,D,D]`，见 u6-l1）。所以**加大 keys（即加大 depth）会以平方级膨胀训练显存**——这是「想加深水印」时必须权衡的性能代价。

#### 4.2.4 代码实践

**实践目标**：源码阅读型——理解 `apply_top_k` 这个逃生阀的语义。

**操作步骤**：

1. 阅读 [logits_processing.py:249-259](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L249-L259)。
2. 回答：若构造 `SynthIDLogitsProcessor(..., apply_top_k=False, top_k=40)`，返回的 `top_k_indices` 形状与取值分别是什么？此时 `top_k=40` 还有没有意义？

**需要观察的现象**：`apply_top_k=False` 时 `top_k_indices` 形状仍是 `[B, top_k]` 吗？

**预期结果**：不是。`apply_top_k=False` 时 `top_k_indices = torch.stack([torch.arange(vocab_size) ...])`，形状变成 `[B, vocab_size]`，`top_k` 参数被完全忽略。这说明 `apply_top_k` 不是「调大小」，而是「开/关稀疏化」的总开关——关掉后等于在全词表上水印。

**待本地验证**：可写 3 行代码实例化处理器并打印 `watermarked_call` 返回的 `top_k_indices.shape` 证实。

#### 4.2.5 小练习与答案

**练习 1**：稀疏 top_k 把单步水印成本从 \(O(V \cdot D)\) 降到 \(O(\text{top\_k} \cdot D)\)。这个优化在检测侧（重算 g 值）还成立吗？

> **答案**：不成立。检测侧的 `compute_g_values` 对整条序列的所有 ngram 重算 g 值（见 u2-l3），不涉及 top_k。稀疏 top_k 是**生成期**的优化；检测期没有「候选 token」的概念，只有已生成的确定序列。

**练习 2**：为什么 `watermarked_call` 必须额外返回 `top_k_indices`，而标准 `LogitsProcessor.__call__` 做不到？

> **答案**：标准 `__call__` 只返回修改后的 scores（稠密 `[B, V]`），没有位置承载「下标映射」。稀疏化后采样发生在 top_k 子集上，必须把局部下标回映成词表 id，所以需要额外返回 `top_k_indices`。这正是 `__call__` 被显式禁用（抛 `NotImplementedError`）、改用 `watermarked_call` 的原因。

**练习 3**：加大 `len(keys)`（即 depth）会让生成更慢、训练显存更大，分别为什么？

> **答案**：生成侧 g 值与得分更新都沿 depth 维循环（`for i in range(depth)`），depth 越大步数越多，故更慢；训练侧 `delta` 矩阵形状含 \(D \times D\)，显存随 depth **平方**增长（见上面的显存注记）。

---

### 4.3 生产化与官方实现

#### 4.3.1 概念说明

这一节回答一个现实问题：**我能把这个仓库直接拿去线上用吗？**

答案是：**不能，也不应该**。仓库在多个地方反复声明它是「参考实现（reference implementation）」，仅供研究复现。挡在生产化路上的有三块硬伤：

1. **静态公开密钥**：`DEFAULT_WATERMARKING_CONFIG` 里的 30 个 keys 是写死在源码里的公开值。所有用这个 Mixin 的人生成的水印都用同一把钥匙，**密钥不可保密、水印不可隔离**。
2. **非密码学哈希**：`accumulate_hash` 基于线性同余生成器（LCG），线性可逆，**不提供任何密码学安全保证**。
3. **子类的粗糙边缘**：README/Notebook 与源码 API 已经漂移，训练入口还有自相矛盾的设备守卫——这是「参考实现」典型的维护状态。

生产化的正确路径是 README 指向的 **HuggingFace Transformers 官方 SynthID Text 实现**，它解决了密钥管理、性能与工程化问题。

#### 4.3.2 核心流程

判断「能否生产化」的核对清单：

```
维度              参考实现状态                     生产化需求
──────────────────────────────────────────────────────────────
密钥保密          公开静态 keys（不可保密）  →     需可保密/可轮换的密钥
哈希安全          LCG，非密码学安全         →     需更强保证或更低依赖
API 一致性        README/Notebook 与源码漂移 →     需稳定、文档化的 API
工程化            研究复现导向，有粗糙边缘   →     需测试覆盖、性能优化、部署支持
                   ─────────────────────────
                   ↓
         结论：转用 HuggingFace Transformers 官方实现
```

#### 4.3.3 源码精读

**(a) 官方的三段免责声明**

[README.md:38-45](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L38-L45) —— 「This implementation is for reference and research reproducibility purposes only ... The subclasses introduced herein are not designed to be used in production systems. Check out the official SynthID Text implementation in [Hugging Face Transformers] for a production-ready implementation.」

[README.md:126-127](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L126-L127) —— 「the mix-in provided by this library uses a static watermarking configuration, making it unsuitable for production use.」（静态配置不适合生产）

[README.md:47-49](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L47-L49) —— `accumulate_hash()` 「does not provide any guarantees of cryptographic security.」（非密码学安全）

**(b) 贝叶斯检测器必须「一钥一训」**

即便不计前面的硬伤，贝叶斯检测器还有一个生产约束——**每把水印密钥都要单独训练**：

[README.md:181-185](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L181-L185) —— 「The Bayesian detector must be trained for each unique watermarking key, and the training data used for this detector model should be independent from, but representative of the expected character and quality of the text content the system will generate in production.」这意味着生产中每换一次 keys，就要重新采集代表性数据、重训检测器——这是一笔持续的运维成本。

**(c) 文档与源码漂移之一：训练入口名不对**

这是「以源码为准」原则最典型的案例。README 写的训练入口在源码里**根本不存在**：

[README.md:187-214](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L187-L214) —— README 示例调用 `train_detector_bayesian.optimize_model(...)`（L206），并 `from synthid_text import train_detector_bayesian`（L189）。但仓库里没有 `train_detector_bayesian.py` 这个模块，也没有 `optimize_model` 函数。真实的训练入口是：

[detector_bayesian.py:985-1068](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L985-L1068) —— `BayesianDetector.train_best_detector(...)` 类方法（承接 u6-l3）。

**(d) 文档与源码漂移之二：Notebook 的 `score()` 签名过时**

Notebook 调用贝叶斯打分时传了**两个**参数：

[notebooks/synthid_text_huggingface_integration.ipynb:846-847](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb#L846-L847)（ipynb 文件行）—— `bayesian_detector.score(wm_g_values.cpu().numpy(), wm_mask.cpu().numpy())`。

但当前源码的 `BayesianDetector.score` 只接收**一个**参数（原始 token 序列），在内部自己重算 g 值与 mask：

[detector_bayesian.py:724-758](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L724-L758) —— `def score(self, outputs)`：内部依次算 `eos_token_mask`、`context_repetition_mask`、`combined_mask`、`g_values`，再调 `BayesianDetectorModule.score(g_values, mask)`。注意：两参数的 `score` 属于**模块级** `BayesianDetectorModule`（L443-L450），与包装类 `BayesianDetector` 的单参数 `score` 是两回事——Notebook 似乎停留在旧 API 上。这是参考实现 API 漂移的又一例，**用前务必对照源码签名**。

**(e) 训练入口里的自相矛盾设备守卫**

即便在源码内部，也能看到参考实现的粗糙边缘。`train_best_detector` 开头有一段守卫：

[detector_bayesian.py:1031-1035](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L1031-L1035) —— `if torch_device.type in ("cuda", "tpu"): raise ValueError("We have found the training unstable on CPUs; ... Use GPU or TPU for training.")`。

这段代码的**条件与提示自相矛盾**：条件是在 GPU/TPU 上时触发，但报错信息却说「训练在 CPU 上不稳定、请用 GPU 或 TPU」。换句话说，按字面执行，你用 GPU 训练反而会被拒绝。这是参考实现里一处疑似逻辑错误的粗糙边缘——**生产前必须本地验证其实际行为**，不能盲信注释或报错文字。（待确认：该守卫的真实意图，可能是条件写反了。）

**(f) 生产化的正确出口**

[README.md:334-336](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L334-L336) —— README 把 `[transformers-blog]` 链到 `huggingface.co/blog/synthid-text`，即 HuggingFace Transformers 官方的 SynthID Text 实现，那才是 production-ready 的落点。

#### 4.3.4 代码实践

**实践目标**：源码阅读型——亲手把 README 的错误训练调用纠正过来。

**操作步骤**：

1. 读 [README.md:206](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L187-L214)，记下 README 声称的调用：`train_detector_bayesian.optimize_model(train_g, train_m, train_l, test_g, test_m, test_l)`。
2. 用 `Glob`/`Grep` 在 `src/synthid_text/` 下查找 `train_detector_bayesian` 模块与 `optimize_model` 函数——确认它们不存在。
3. 查找真实的训练入口，确认是 `BayesianDetector.train_best_detector`。

**需要观察的现象**：搜索 `optimize_model` 是否有命中？搜索 `train_best_detector` 是否命中？

**预期结果**：`optimize_model` 无命中（或仅 README 文本命中）；`train_best_detector` 命中于 `detector_bayesian.py:986`。据此写出**正确**的调用签名：

```python
# 示例代码（基于真实源码签名，非 README 的过时写法）
detector, loss = detector_bayesian.BayesianDetector.train_best_detector(
    tokenized_wm_outputs=wm_outputs,
    tokenized_uwm_outputs=uwm_outputs,
    logits_processor=logits_processor,
    tokenizer=tokenizer,
    torch_device=torch_device,
)
```

**待本地验证**：可在仓库根目录实际执行 `grep -rn "optimize_model" .` 与 `grep -rn "train_best_detector" .` 复核。

#### 4.3.5 小练习与答案

**练习 1**：为什么「静态公开 keys」会让水印无法用于生产隔离？

> **答案**：keys 写死在源码里且对外公开，所有用户共享同一把钥匙。任何知道 keys 的人都能伪造或剥离该水印，无法做到「不同租户/不同模型用不同且保密的水印」。生产隔离要求密钥可保密、可轮换、可按主体分发。

**练习 2**：README 的 `train_detector_bayesian.optimize_model` 与源码真实入口 `BayesianDetector.train_best_detector` 不一致。这种漂移会带来什么实际风险？

> **答案**：照 README 抄代码会直接 `ImportError`/`AttributeError`；更隐蔽的风险是让人怀疑「是不是我装错了版本」而浪费排错时间。它也削弱了对整个仓库文档的信任——所以遇到任何 API 都该回源码核对签名。这也是本手册反复强调「以源码为准」的由来。

**练习 3**：如果只想做**最小可行性**的水印验证（不上生产），本仓库够用吗？要注意什么？

> **答案**：够用，这正是它的定位（研究复现）。但要注意：用公开 keys 意味着水印不保密；贝叶斯检测器要为该 keys 单独训；务必对照源码而非 README 写调用；CPU 上训练可能不稳定（见设备守卫，且该守卫本身自相矛盾，需本地验证）。

---

## 5. 综合实践

> 这是本讲的核心任务：**设计**一个二次开发方案——在不改主流程的前提下，把 Mean 打分替换成自定义加权打分。仅设计，不实现。

### 5.1 任务背景

你在用本仓库做内部评测，发现默认的 `mean_score` 对短文本区分度不够，而 `weighted_mean_score` 的「10→1 线性递减」权重也不是你想要的——你希望给「中间层」更高的权重（你怀疑首尾层噪声更大）。目标：换上自定义加权打分，但**不碰** `logits_processing.py`、`synthid_mixin.py`、`detector_bayesian.py` 这些主流程文件。

### 5.2 关键洞察：打分函数的签名契约

所有免训练打分函数共享同一个契约（见 4.1.3）：

```
score_fn(g_values: [batch, seq_len, depth],
         mask:     [batch, seq_len]) -> scores: [batch]
```

只要新函数满足这个签名，它就是 `mean_score` 的 drop-in 替换。而且打分发生在**检测侧**，只读 g 值与 mask，与施加侧主流程完全解耦——这正是「不改主流程」可行的根本原因。

### 5.3 设计方案（两选一）

**方案 A：零代码（推荐优先尝试）**

直接利用 `weighted_mean_score` 自带的 `weights` 扩展参数。把调用点

```python
# notebooks/...ipynb 第 662 行附近的现状
wm_mean_scores = detector_mean.mean_score(
    wm_g_values.cpu().numpy(), wm_mask.cpu().numpy()
)
```

改成

```python
# 示例代码：传入自定义「中间高、两端低」的权重（形状 [depth]）
import jax.numpy as jnp
my_weights = jnp.linspace(1, 10, 15).tolist() + jnp.linspace(10, 1, 15).tolist()  # 仅示意
wm_scores = detector_mean.weighted_mean_score(
    wm_g_values.cpu().numpy(), wm_mask.cpu().numpy(), weights=my_weights
)
```

- **要改的函数**：0 个。
- **要新增的函数**：0 个。
- **要改的位置**：仅 Notebook 调用点（[ipynb 第 662-664 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb#L662-L664)）一处。

**方案 B：新增一个自定义打分函数**

当 `weighted_mean_score` 的「乘以权重再求和」模型不够（比如你想做非线性聚合、或按 mask 比例动态调整），就在 `detector_mean.py` 里**新增**一个同签名函数：

```python
# 示例代码：新增函数，签名与 mean_score 完全一致
def my_weighted_score(g_values, mask):
    watermarking_depth = g_values.shape[-1]
    # 你的自定义聚合逻辑（示例：按位置在前 10% 的 token 加权更重）
    seq_len = g_values.shape[1]
    position_weights = jnp.ones(seq_len)           # 此处填你的逻辑
    weights = jnp.linspace(10, 1, watermarking_depth)
    weights *= watermarking_depth / jnp.sum(weights)
    weighted = g_values * jnp.expand_dims(weights, (0, 1))
    num_unmasked = jnp.sum(mask, axis=1)
    return jnp.sum(weighted * jnp.expand_dims(mask, 2), axis=(1, 2)) / (
        watermarking_depth * num_unmasked
    )
```

然后在调用点把 `detector_mean.mean_score(...)` 换成 `detector_mean.my_weighted_score(...)`。

- **要改的函数**：0 个（不修改任何现有函数）。
- **要新增的函数**：1 个 `my_weighted_score`，加在 `detector_mean.py` 内。
- **要改的位置**：Notebook 调用点一处。

### 5.4 交付清单与验证

| 步骤 | 动作 | 是否触动主流程 |
| --- | --- | --- |
| 1 | 选定权重方案（A 传参 / B 新函数） | 否 |
| 2 | 在 `detector_mean.py` 新增函数（仅方案 B） | 否（只新增，不改既有函数） |
| 3 | 把 Notebook 里 `mean_score` 调用换成新打分 | 否（检测侧调用点） |
| 4 | 对同一批 wm/uwm g 值算分，画分布对比 | —— |
| 5 | 选阈值（参考 u5-l2：按 token 长度与目标 FPR 校准） | —— |

**验证标准**：水印样本分数应明显高于非水印样本；自定义加权相比默认 `mean_score` 应在短文本上有更好的 wm/uwm 分离（更小的重叠面积）。若分离反而变差，说明权重假设错误，回到方案 A 调参。

**为什么这能「不改主流程」**：因为施加侧（`SynthIDLogitsProcessor` / `watermarked_call` / Mixin）只负责产出 g 值，检测侧的打分函数只是 g 值的**只读消费者**。你换消费者，永远不影响生产者——这是 SynthID Text 架构留给二次开发者最大的便利。

## 6. 本讲小结

- SynthID Text 有**三类扩展面**：水印配置（`SynthIDLogitsProcessor.__init__` 的 `keys`/`num_leaves`/`apply_top_k` 等旋钮）、免训练打分（`mean_score`/`weighted_mean_score` 的 `(g_values, mask) -> scores` 契约与 `weights` 参数）、贝叶斯训练超参（`train_best_detector` 的 `l2_weights` 网格等）。三者互不耦合。
- **稀疏 top_k** 是施加侧的性能核心：只在 top_k 个候选上算 g 值与更新得分，把单步成本从 \(O(V\cdot D)\) 降到 \(O(\text{top\_k}\cdot D)\)；代价是必须禁用 `__call__`、改写采样循环、并用 `torch.vmap(torch.take)` 把局部下标回映成词表 id。`apply_top_k=False` 是关掉稀疏化的逃生阀。
- 检测侧的性能瓶颈在别处：训练显存随 depth **平方**增长（`delta` 矩阵 \(D\times D\)），加大 keys 要权衡。
- 这个仓库**不适合直接生产化**：静态公开 keys（不可保密/隔离）、LCG 非密码学安全、API 漂移（README 的 `optimize_model` 不存在、Notebook 的 `score()` 双参签名过时）、甚至训练入口有自相矛盾的设备守卫。
- 生产化的正确路径是 **HuggingFace Transformers 官方 SynthID Text 实现**；贝叶斯检测器还需「一钥一训」的持续运维成本。
- 贯穿全手册的总原则再次被印证：**文档与源码冲突时，一律以源码为准**。

## 7. 下一步学习建议

本讲是手册的终点，也是真正使用的起点。建议按兴趣选择方向：

1. **想真的跑起来**：回到 u1-l4 的端到端 Notebook，用 GPT-2（任意配置即可）完整跑一遍「生成 → 重算 g 值 → Mean/Weighted Mean 打分」，再尝试第 5 节的自定义打分方案。
2. **想深入理论**：结合 u7-l1 的 `expected_mean_g_value` 与 u3-l3 的得分更新公式，自己推导 num_leaves=3 的期望 0.875，并用 u7-l2 的统计测试方法验证。
3. **想走向生产**：阅读 README 指向的 [HuggingFace Transformers 官方 SynthID Text 实现](https://huggingface.co/blog/synthid-text)，对比它与本参考实现在密钥管理、性能与 API 稳定性上的差异。
4. **想做二次开发**：以第 5 节为模板，尝试更高难度的扩展——例如实现一个论文里提到、但本仓库未提供的打分函数，或为一种新的因果语言模型写一个 `pass` 子类挂上 Mixin（参考 u4-l3）。

至此，你已从「SynthID Text 是什么」一路走到「怎么改、怎么取舍、何时止步」。祝阅读源码愉快。
