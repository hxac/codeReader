# 测试套件如何验证水印正确性

## 1. 本讲目标

本讲是专家层「理论、测试与扩展实践」单元的第二篇，回答一个关键问题：

> SynthID Text 是一个统计水印系统——它的「正确」不能用一个确定的输入→输出对来定义，而要靠**统计性质**来保证。那么仓库里的测试套件，究竟用什么手段去验证这些统计性质？

读完本讲，你应当能够：

1. 理解如何用「大样本 + 容差断言」验证 g 值的**均匀性（无偏性）**，并能算出 `batch_size` 为什么必须取得很大。
2. 区分两类「分布」测试：**分布收敛性**（水印不扭曲 token 频率）与**理论期望**（水印确实制造了预期偏置），并理解二者为何看似矛盾却同时成立。
3. 掌握如何用 `mock.patch.object` 配合 `side_effect` 既能**计数**又能**真实执行**，从而验证重写的 `_sample` 采样循环确实调用了 `watermarked_call`。
4. 看懂「形状测试」这一最朴素却最有效的防线，理解 g 值、掩码等张量的输出形状契约。

本讲只读、不改源码，全部是「阅读测试理解行为」的实践。

## 2. 前置知识

在进入测试细节前，你需要先建立下面几条来自前置讲义的认知（本讲直接承接，不再重复推导）：

- **g 值**：一个 ngram + 一把水印密钥经哈希取出的一颗二进制比特，形状 `[batch, seq, depth]`，值域 0/1。非水印文本的 g 值近似一颗**无偏硬币**（均值 ≈ 0.5）；水印文本因施加阶段把概率推向 g=1，均值升到约 0.75（`num_leaves=2`）或 0.875（`num_leaves=3`）。参见 [[u2-l3]]、[[u5-l2]]。
- **`watermarked_call`**：水印施加的带状态主入口，分 5 步完成 top_k → 滑动上下文 → 算 ngram keys → 取 g 值 → 修改 scores，返回稀疏三元组。标准 `__call__` 被禁用。参见 [[u3-l2]]。
- **`_sample` 与 `watermarked_call`**：HuggingFace 的采样循环被整体改写，在 `do_sample` 分支里调用 `watermarked_call`，再用 `torch.vmap(torch.take)` 把局部下标回映为稠密 token id。参见 [[u4-l2]]。
- **`expected_mean_g_value`**：在「均匀 LM 分布」假设下，单层锦标赛水印的理论 g 值期望——`num_leaves=2` 为 \(0.5+0.25(1-1/V)\)，`num_leaves=3` 为 \(7/8-3/(8V)\)。参见 [[u7-l1]]。

此外，理解本讲需要一点点**统计直觉**：

- **大数定律（LLN）**：把一颗无偏硬币掷很多次，正面的**频率**会收敛到真实概率 0.5；样本越多，样本均值离 0.5 越近。
- **中心极限定理（CLT）**：样本均值的标准差（标准误）约为

\[ \text{SE} = \sqrt{\frac{p(1-p)}{N}} \]

对无偏硬币 \(p=0.5\)，即 \(\text{SE}=0.5/\sqrt{N}\)。这一条正是本讲解释「为什么 `batch_size` 要很大」的钥匙。

- **容差断言** `assertAlmostEqual(a, b, delta=d)`：不要求 `a == b`，只要求 \(|a-b|\le d\)。统计测试必须用容差，因为随机量的样本均值永远只是「接近」而非「等于」理论值。

> 全手册原则提醒：文档与源码冲突时以源码为准。本讲所有行号、函数名均取自当前 HEAD 的真实源码。

## 3. 本讲源码地图

本讲涉及三个文件，全部位于 `src/synthid_text/` 下：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `logits_processing_test.py` | 水印内核的**正确性 + 形状**测试（pytest 自动发现） | g 值均匀性、分布收敛、理论期望匹配、各张量形状契约 |
| `synthid_mixin_test.py` | HuggingFace 集成的**集成性**测试 | 用 mock 验证 `_sample` 调用了 `watermarked_call` |
| `torch_testing.py` | 测试公共工具 | `torch_device()` 选 cuda 或 cpu，让测试设备无关 |

被测的「产品代码」主要来自：

- `logits_processing.py`：`compute_g_values`、`get_gvals`、`compute_ngram_keys`、`_compute_keys`、`compute_context_repetition_mask`、`compute_eos_token_mask`、`watermarked_call`、`update_scores`、`update_scores_distortionary`。
- `synthid_mixin.py`：`_sample`、`_get_logits_warper`。
- `g_value_expectations.py`：`expected_mean_g_value`。

> 注意目录约定（来自 [[u1-l3]]）：测试文件以 `_test.py` 结尾、与被测模块同目录，由 pytest 自动收集；`torch_testing.py` 因为被测试代码 `import` 而位于包内（否则会随产品代码被打包，这是有意为之的副作用）。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：g 值均匀性/形状测试、分布收敛与理论期望测试、mixin mock 测试。

### 4.1 g 值均匀性与形状测试

#### 4.1.1 概念说明

统计水印的正确性，最底层的一条保证是：

> **对于一段与水印密钥无关的随机文本，g 值必须是一颗无偏硬币——每个 g 值取 0 或 1 的概率都接近 0.5。**

如果这条不成立（比如 g 值系统性地偏向 1），那么连**非水印文本**的 g 值均值都会偏离 0.5，检测器就会把没水印的文本也判成有水印——这是灾难性的假阳性。反之，只有当「非水印 → 0.5、水印 → 0.75」这个对比成立时，打分函数（`mean_score` 等）才有区分力（参见 [[u5-l2]]）。

「均匀性测试」就是用大样本随机 ngram 去验证这条性质。而「形状测试」则更朴素：直接断言每个公开函数的输出张量形状符合契约（例如 g 值序列维比输入短 `ngram_len-1`）。形状测试虽然简单，却是最有效的「重构回归防线」——一旦有人改坏了 `unfold` 滑窗或 vmap 维度，形状立刻对不上。

#### 4.1.2 核心流程

均匀性测试的套路可以抽象成一个伪代码模板：

```text
1. 用固定随机种子生成一大批随机 ngram（与密钥无关的「普通文本」）
2. 实例化 SynthIDLogitsProcessor（任意 keys 即可）
3. 调 compute_g_values(ngrams) 得到 g 值 [batch, seq, depth]
4. 对所有样本求均值
5. assertAlmostEqual(mean, 0.5, delta=容差)
```

关键设计点：

- **大样本**：`batch_size` 取得极大（如 100000），让样本均值的随机波动远小于容差，避免「假性失败」。
- **固定种子**：`torch.manual_seed(0)` 让测试可复现；同时用 `assertAlmostEqual` 而非 `assertEqual` 容纳不可消除的随机残差。
- **`delta` 反映对「接近程度」的要求**：样本越多、越均匀，`delta` 可以取得越小。

形状测试的套路则统一为「构造 → 调用 → `assertEqual(shape, 期望)`」，由一个公共的 `set_up_logits_processor` 辅助函数构造处理器与随机序列。

#### 4.1.3 源码精读

**均匀性测试主例**（本讲实践任务的主角）：

[src/synthid_text/logits_processing_test.py:140-164](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L140-L164) —— `test_g_value_uniformity_for_random_ngrams`：生成 `batch_size=100000` 条随机 ngram，计算 g 值后断言均值 ≈ 0.5（`delta=0.01`）。注意这里**没有走 `watermarked_call`**，只是直接对随机 token 算 g 值，验证的是「哈希取位」本身的无偏性。

```python
batch_size = 100000
torch.manual_seed(0)
ngrams = torch.randint(low=0, high=vocab_size, size=(batch_size, ngram_len), device=device)
...
g_values = logits_processor.compute_g_values(ngrams)
g_values_mean = torch.mean(torch.mean(g_values.float(), dim=0))
self.assertAlmostEqual(g_values_mean, 0.5, delta=0.01)
```

注意 `g_values_mean` 是「先沿 dim=0（batch）求均值、再对所有元素求均值」，等价于把 `[batch, 1, num_layers]` 里所有比特摊平求均值。

**更严格的跨词表均匀性测试**：

[src/synthid_text/logits_processing_test.py:178-208](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L178-L208) —— `test_g_values_uniformity_across_vocab_size`：它不只随机生成「上下文」，还把**整个词表的每个 token**都作为候选续接，再用 `_compute_keys` + `get_gvals` 算 g 值，断言均值 ≈ 0.5（`delta=0.001`，比上一个严 10 倍）。因为它枚举了全部词表，g 值在词表维上被「强制摊平」得更均匀，容差才能压到 0.001。

这里直接调了内部函数 `_compute_keys`（生成侧用的、可累积哈希的那个）：

[src/synthid_text/logits_processing.py:403-448](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L403-L448) —— `_compute_keys`：把 n-1 gram 上下文与每个候选 token、每个深度层做可累积哈希，返回 `[batch, num_indices, depth]` 的 ngram keys。

[src/synthid_text/logits_processing.py:328-356](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L328-L356) —— `get_gvals`：对 ngram keys 做 12 轮「再哈希 + 右移」充分混淆，最后取第 30 比特 `% 2` 得到 0/1。均匀性测试验证的就是这一步取出的比特无偏。

**形状测试一族**（全部由 `set_up_logits_processor` 构造，见 [src/synthid_text/logits_processing_test.py:321-347](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L321-L347)）：

- [src/synthid_text/logits_processing_test.py:349-362](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L349-L362) —— `test_compute_g_values_shape`：断言 g 值形状为 `(batch, sequence_len-(ngram_len-1), num_layers)`，正是 [[u2-l3]] 讲过的「序列维 = 输入长度 − (ngram_len−1)」。
- [src/synthid_text/logits_processing_test.py:364-377](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L364-L377) —— `test_compute_context_repetition_mask_shape`：重复掩码形状 `(batch, sequence_len-(ngram_len-1))`，与 g 值序列维对齐。
- [src/synthid_text/logits_processing_test.py:379-392](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L379-L392) —— `test_compute_eos_token_mask_shape`：eos 掩码形状 `(batch, sequence_len)`（注意是全长 N，不是 L，参见 [[u5-l1]]）。
- [src/synthid_text/logits_processing_test.py:394-414](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L394-L414) —— `test_compute_ngram_keys_shape`：ngram keys 形状 `(batch, num_ngrams, num_layers)`。
- [src/synthid_text/logits_processing_test.py:416-434](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L416-L434) —— `test_watermarked_call_shape`：`watermarked_call` 返回的三元组形状都应是 `(batch, top_k)`（稀疏！）。

**设备无关工具**：

[src/synthid_text/torch_testing.py:21-26](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/torch_testing.py#L21-L26) —— `torch_device()`：有 GPU 返回 `cuda:0`，否则 `cpu`。所有正确性测试都通过它取设备，CI 与本地都能跑。

#### 4.1.4 代码实践

> 实践目标：精读 `test_g_value_uniformity_for_random_ngrams`，解释它如何用 `assertAlmostEqual(..., 0.5, delta=0.01)` 验证 g 值无偏性，并算清楚 `batch_size` 为什么必须很大。

**操作步骤（源码阅读 + 手算，无需运行）**：

1. 打开 [src/synthid_text/logits_processing_test.py:140-164](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L140-L164)，确认它**没有**调用 `watermarked_call`，而是直接对随机 ngram 调 `compute_g_values`。这说明它测的是「哈希取位」本身，与水印施加无关。
2. 计算样本量：`ngrams` 形状 `[100000, ngram_len]`，`compute_g_values` 返回 `[100000, 1, num_layers]`，所以总比特数 \(N = 100000 \times \text{num\_layers}\)。
3. 套用标准误公式（无偏硬币 \(p=0.5\)）：

\[ \text{SE} = \frac{0.5}{\sqrt{N}} \approx \frac{0.5}{\sqrt{100000}} \approx 0.00158 \]

4. 比较 `delta=0.01` 与 SE：\(0.01 / 0.00158 \approx 6.3\)，即容差约为 **6.3 个标准误**。

**需要观察的现象 / 预期结论**：

- 容差是 SE 的 6 倍多，按中心极限定理，样本均值落在这个区间外的概率极小（约 \(2 \times 10^{-10}\)），所以测试几乎不会因随机波动而「假性失败」——这正是 `batch_size` 必须大的根本原因。
- **反推**：若把 `batch_size` 降到 100，则 \(\text{SE}=0.5/\sqrt{100}=0.05\)，远大于 `delta=0.01`，样本均值会经常偏离 0.5 超过 0.01，测试就会**不稳定地失败**（flaky）。大样本是把统计噪声压到容差以内的唯一办法。
- 注意 `keys` 是随机生成的（`np.random.randint`），但只要密钥不与输入 token 相关，g 值就应当无偏——这恰好验证了「换 keys 不破坏无偏性」。

> 如需本地验证：`pip install -e .[test]` 后运行 `pytest src/synthid_text/logits_processing_test.py -k uniformity -v`，应全部通过；若改小 `batch_size` 重跑，会观察到偶发失败。未实际运行则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`test_g_value_uniformity_for_random_ngrams` 用 `delta=0.01`，而 `test_g_values_uniformity_across_vocab_size` 用 `delta=0.001`。为什么后者可以严格 10 倍？

> **参考答案**：后者把**整个词表的每个 token**都枚举为候选续接（`torch.arange(vocab_size)`），g 值在词表维上被强制摊平，样本量是 `batch × vocab_size × num_layers`（如 1000×1000×num_layers），远大于前者的 `100000×1×num_layers`；样本越多 SE 越小，容差就能压得更紧。

**练习 2**：如果把 `torch.manual_seed(0)` 删掉，测试还能通过吗？删掉它是好是坏？

> **参考答案**：大概率仍能通过，因为大样本下均值几乎总落在容差内。但删掉种子会让测试**不可复现**——一旦偶发失败，无法稳定重现定位。固定种子 + 容差断言是统计测试的最佳实践：种子保证可复现，容差容纳不可消除的残差。

### 4.2 分布收敛与理论期望测试

#### 4.2.1 概念说明

均匀性测试回答了「非水印文本 g 值无偏」。但水印系统还有两条更微妙的性质需要验证，它们看起来**互相矛盾**，必须同时成立：

1. **分布收敛性（distributional convergence）**：水印**不应扭曲 token 的边缘频率**。也就是说，把水印分布「在密钥上取平均」后，每个 token 出现的概率应当回到原始 LM 分布。这是水印「难以被察觉」的根源——光看 token 频率分布，分不出水印与非水印文本。
2. **理论期望偏置（theoretical bias）**：水印**必须**在 g 值层面制造出可预测的偏置。即对一段**真正经过 `watermarked_call` 施加水印、并按水印分布采样**得到的文本，其 g 值均值应当**精确**等于 `expected_mean_g_value` 给出的理论值（0.75 / 0.875 附近）。

一句话区分：「分布收敛」说的是**token 维度**不偏（对外不可见），「理论期望」说的是 **g 值维度**有偏（对内可检测）。二者同时成立，水印才既隐蔽又可测。

#### 4.2.2 核心流程

**分布收敛测试**（`test_distributional_convergence`）的思路：

```text
for 多把随机 keys（num_keys=1000）:
    用这把 key 实例化处理器
    对随机 ngram 施加水印（watermarked_call），得 updated_scores
    把 updated_scores 的 softmax 累加起来
把累加结果在 (keys × batch) 上求平均
断言：每个 token 的平均概率 ≈ 输入分布（这里是均匀 0.5）
```

直觉：单把 key 会把概率推向某些 token；但 g 值在 key 上是随机的，换一把 key 推的方向就不同；把很多把 key 的结果平均，「推」的效应相互抵消，边缘分布回到原始。输入 scores 全 1（均匀），所以平均后每个 token 概率回到 0.5。

**理论期望测试**（`test_bias_from_logits_processor` + 辅助函数 `does_mean_g_value_matches_theoretical`）的思路：

```text
构造「均匀 LM」（scores 全 1）
真正走一遍 watermarked_call 施加水印
按水印后的分布采样出 next token
把 (context, next_token) 拼成 ngram，重算 g 值
断言：g 值均值 ≈ expected_mean_g_value(vocab_size, num_leaves)
```

这里的关键是「采样后再重算 g 值」——只有真正按水印分布采样出的 token，其 g 值才会呈现水印偏置；如果直接对随机 token 算，就回到 4.1 的 0.5 了。

#### 4.2.3 源码精读

**分布收敛测试**：

[src/synthid_text/logits_processing_test.py:210-254](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L210-L254) —— `test_distributional_convergence`：`vocab_size=2`、`batch_size=1500`、`num_keys=1000` 次循环，每次换一把随机 key 施加水印并累加 softmax，最后断言每个 token 的平均概率 ≈ 0.5（`delta=0.002`）。注意它用了 `apply_top_k=False`（因为 vocab 只有 2，top_k 截断无意义）。

```python
for _ in tqdm.tqdm(range(num_keys)):
    ...  # 每次随机 key
    updated_scores, _, _ = logits_processor.watermarked_call(ngrams, scores)
    updated_softmaxes += torch.nn.functional.softmax(updated_scores, dim=1).cpu().numpy()
updated_softmaxes = np.mean(updated_softmaxes, axis=0) / num_keys
for softmax in updated_softmaxes:
    self.assertAlmostEqual(softmax, 0.5, delta=0.002)
```

**理论期望测试**（参数化，覆盖 num_leaves=2 与 3）：

[src/synthid_text/logits_processing_test.py:295-316](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L295-L316) —— `test_bias_from_logits_processor`：调用辅助函数，断言 `passes` 为真。

[src/synthid_text/logits_processing_test.py:29-121](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L29-L121) —— `does_mean_g_value_matches_theoretical`（模块级辅助函数）：它的精髓在第 88–119 行——真正调 `watermarked_call`、`softmax`+`multinomial` 采样、`torch.vmap(torch.take)` 回映稠密 token（与 `_sample` 里的回映手法完全一致）、拼成 ngram 重算 g 值，再用 `torch.isclose(..., atol=atol)` 与理论值对拍：

```python
expected_mean_g_value = g_value_expectations.expected_mean_g_value(
    vocab_size=vocab_size, num_leaves=num_leaves)
is_close = torch.all(torch.isclose(mean_g_values,
    torch.tensor(expected_mean_g_value, ...), atol=atol, rtol=0))
```

被对拍的理论值来自：

[src/synthid_text/g_value_expectations.py:37-44](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L37-L44) —— `expected_mean_g_value` 的两个分支：`num_leaves=2` 返回 \(0.5+0.25(1-1/V)\)；`num_leaves=3` 返回 \(7/8-3/(8V)\)。手算几例验证直觉：

- \(V=1000,\,N=2\)：\(0.5+0.25\times0.999=0.74975\)
- \(V=2,\,N=2\)：\(0.5+0.25\times0.5=0.625\)
- \(V=100,\,N=3\)：\(0.875-0.00375=0.87125\)

可以看到词表越小、`num_leaves` 越大，有限词表的修正项越显著——这正是测试要逐组合（`vocab_size`、`num_leaves`）参数化的原因。

#### 4.2.4 代码实践

> 实践目标：用「同一段均匀 scores」走两条路——直接算 g 值 vs. 先水印再采样算 g 值——观察均值从 0.5 跳到 0.75，亲手印证「均匀性」与「理论期望」的分野。

**操作步骤（源码阅读型实践，无需运行模型）**：

1. 阅读 `does_mean_g_value_matches_theoretical` 的 [第 77–104 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L77-L104)，确认它做了三件事：scores 全 1（均匀 LM）→ `watermarked_call` 施加 → `multinomial` 采样 → 回映 → 重算 g 值。
2. 对比 4.1 的均匀性测试：同样是「均匀 scores + 随机 ngram」，但 4.1 **直接**算 g 值（绕过水印），这里**先水印再采样**算 g 值。
3. 手算预期：取 `vocab_size=1000`、`num_leaves=2`，理论均值 0.74975；而 4.1 同设置下是 0.5。

**需要观察的现象 / 预期结果**：

| 路径 | 是否经 `watermarked_call` | g 值均值预期 |
| --- | --- | --- |
| 均匀性测试（4.1） | 否 | ≈ 0.50 |
| 理论期望测试（4.2） | 是（采样后重算） | ≈ 0.75（N=2）/ 0.875（N=3） |

这张表就是本模块的核心结论：**绕过水印 → 0.5；经过水印 → 理论偏置值**。检测器（[[u5-l2]] 的 `mean_score`）正是靠这个差来判别。

> 待本地验证：可在 REPL 里复制 `does_mean_g_value_matches_theoretical` 的核心步骤，打印 `mean_g_values` 与 `expected_mean_g_value` 对比。

#### 4.2.5 小练习与答案

**练习 1**：`test_distributional_convergence` 的循环里每次都换一把随机 key。如果改成「只用一把固定 key」，断言 `softmax ≈ 0.5` 还会成立吗？为什么？

> **参考答案**：不会稳定成立。单把 key 会让某些 token 系统性获得更高 g 值从而被 `update_scores` 推高，边缘分布偏离均匀。只有「在 key 上取平均」让随机推力相互抵消，边缘分布才回到均匀——这正是「分布收敛」需要对多把 key 求平均的原因。

**练习 2**：`does_mean_g_value_matches_theoretical` 为什么要先 `multinomial` 采样再重算 g 值，而不是直接对 `top_k` 候选取平均？

> **参考答案**：理论期望 \(0.75\) 描述的是「**被实际生成出来的 token**」的 g 值均值，而生成分布正是水印后的概率分布。必须按水印分布采样出 token，再算它的 g 值，才能复现论文里的期望；直接对候选取平均得到的是「候选层」的统计量，不是「生成文本」的统计量，对不上理论值。

**练习 3**：`test_bias_from_logits_processor` 为什么用 `keys=[1]`（单个 key、depth=1），而不是默认的 30 层？

> **参考答案**：理论公式 `expected_mean_g_value` 是「单层锦标赛」的闭式解（论文 Corollary 27 / Theorem 25 的单层情形）。用 `num_layers=1` 让实测与单层理论严格对齐；多层水印的均值是各层独立同分布的平均，仍趋近同一期望，但单层最干净地对拍公式。

### 4.3 Mixin mock 测试：验证采样循环真的集成了水印

#### 4.3.1 概念说明

前两个模块测的是「水印内核」`SynthIDLogitsProcessor` 本身。但 SynthID 真正的使用方式是 `model.generate(...)`（参见 [[u4-l2]]、[[u4-l3]]），水印要靠重写的 `_sample` 采样循环去调用 `watermarked_call` 才能生效。

这就带来一个测试难题：

> `_sample` 是从 HuggingFace `transformers` 里**整体复制改写**的一大段代码（数百行）。怎么验证「这段改写确实在某处调用了 `watermarked_call`」，而不用真的加载一个大模型去跑 `generate`？

答案是 **mock（打桩）**：用 `mock.patch.object` 把 `watermarked_call` 替换成一个「假对象」，运行一次 `_sample`，然后检查这个假对象「被调用了几次」。如果调用次数 ≥ 1，就证明采样循环确实把水印挂进去了。

这里有一个精妙的设计：mock 通常会**屏蔽**真实行为（替换成什么都不做的假对象），但本测试用 `side_effect = old_watermarked_call` 让 mock 在记录调用的同时**仍然执行真实逻辑**——既计数、又不破坏运行。

#### 4.3.2 核心流程

```text
1. 备份真实的 watermarked_call（old_watermarked_call）
2. with mock.patch.object(processor 类, "watermarked_call", autospec=True) as mock_obj:
       mock_obj.side_effect = old_watermarked_call   # 既计数又真实执行
3. 构造一个极简的 MockSynthIDModel（只继承 Mixin，__call__ 返回固定形状 logits）
4. _get_logits_warper(...) 得到水印 warper
5. 调用 _sample(...) 跑一轮采样
6. assertEqual(mock_obj.call_count, 1)   # 水印确实被调用了一次
```

关键点：

- **`autospec=True`**：让 mock 的签名与原函数一致，防止测试代码传错参数却悄悄通过。
- **`side_effect = old_watermarked_call`**：把真实函数设为「副作用」，每次被调用先记账、再执行真实逻辑，这样 `_sample` 内部依赖 `watermarked_call` 返回值（三元组）的后续代码也能正常跑。
- **`call_count == 1`**：测试里 `stopping_criteria` 第一轮就返回 `True`（`lambda *_: True`），所以循环只跑一轮，恰好调用一次。
- **极简 Mock 模型**：`MockSynthIDModel` 只继承 Mixin，`__call__` 返回固定形状的 logits（`[batch, 5, 7]`），完全不需要真实权重——既快又隔离。

#### 4.3.3 源码精读

**极简 Mock 模型**：

[src/synthid_text/synthid_mixin_test.py:34-58](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin_test.py#L34-L58) —— `MockSynthIDModel`：继承 `SynthIDSparseTopKMixin`，`__call__` 返回固定 logits 的 `ModelOutput`，`prepare_inputs_for_generation` 只做最小透传。它把「模型」简化到刚好够触发 `_sample` 的程度。

**mock 测试本体**：

[src/synthid_text/synthid_mixin_test.py:63-94](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin_test.py#L63-L94) —— `test_sampling_from_mixin_includes_watermarking`：

```python
old_watermarked_call = logits_processing.SynthIDLogitsProcessor.watermarked_call
with mock.patch.object(
    logits_processing.SynthIDLogitsProcessor, "watermarked_call", autospec=True,
) as mock_watermarked_call:
    mock_watermarked_call.side_effect = old_watermarked_call   # 既计数又真实执行
    ...
    synthid_model._sample(input_ids=torch.ones(3, 11, dtype=torch.long), ...)
    self.assertEqual(mock_watermarked_call.call_count, 1)
```

`generation_config` 里设了 `top_k=5, temperature=0.5, do_sample=True`（这些都是 Mixin 与 processor 的合法性区间，参见 [[u4-l1]]）。`stopping_criteria` 用 `lambda *_: True` 让循环一轮即停，把 `call_count` 钉死在 1，断言才精确。

被验证的「产品代码」是被改写的 `_sample`（[src/synthid_text/synthid_mixin.py:129](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L129)）以及构造唯一水印 warper 的 `_get_logits_warper`（[src/synthid_text/synthid_mixin.py:85](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L85)）。如果有人重构 `_sample` 时误删了对 `watermarked_call` 的调用，这个测试会立刻失败——`call_count` 变成 0。

#### 4.3.4 代码实践

> 实践目标：理解 mock 的「计数 + 真实执行」双角色，并设计一个思想实验——如果把 `side_effect` 那一行删掉会怎样。

**操作步骤（源码阅读型实践）**：

1. 阅读 [src/synthid_text/synthid_mixin_test.py:63-94](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin_test.py#L63-L94)，圈出三处：备份真实函数（第 64–66 行）、`mock.patch.object` + `side_effect`（第 67–72 行）、`assertEqual(..., 1)`（第 94 行）。
2. 思考：`_sample` 内部拿到 `watermarked_call` 的返回值后，还要做 `torch.vmap(torch.take)` 回映（参见 [[u4-l2]]）。如果 mock 不设 `side_effect`，`watermarked_call` 会返回一个 `MagicMock` 对象而非三元组，后续解包与张量运算就会崩。

**需要观察的现象 / 预期结论**：

- **保留 `side_effect`**（现状）：`call_count == 1`，`_sample` 正常跑完，测试通过。证明采样循环集成了水印。
- **删掉 `side_effect`**（思想实验）：`watermarked_call` 返回 mock 对象，`_sample` 在解包三元组或回映 token 时抛 `TypeError`/`AttributeError`，测试**报错失败**——但失败点在「mock 返回值无法用」而非「没调用」，这正是设 `side_effect` 的工程价值：让 mock 透明地转发真实逻辑。

**预期结果**：本测试以极低成本（无需真实模型权重、无需 GPU）锁定了「`_sample` 必调 `watermarked_call`」这一集成契约，是防止采样循环重构回归的关键护栏。

> 待本地验证：`pip install -e .[test]` 后 `pytest src/synthid_text/synthid_mixin_test.py -v`。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `autospec=True` 而不是普通 `mock.patch.object`？

> **参考答案**：`autospec=True` 让 mock **继承原函数的签名**（参数名、参数个数）。这样如果未来 `_sample` 调用 `watermarked_call` 时传错了参数（比如漏了 `scores`），mock 会立即报错，而不是静默吞掉。它把「接口契约」也纳入了测试。

**练习 2**：测试断言 `call_count == 1` 而非 `>= 1`。为什么能精确等于 1？

> **参考答案**：`stopping_criteria` 设为 `lambda *_: True`，`_sample` 循环第一轮就被判定「该停止」，因此只执行一轮、只调用一次 `watermarked_call`。这是把循环轮次钉死在 1 的有意设计，让断言可以精确而非模糊。

**练习 3**：这个测试只验证「调用了」，没验证「调用结果正确」。这样够吗？它的定位是什么？

> **参考答案**：它够用，因为「调用结果正确」已经由 4.1、4.2 对 `watermarked_call`/`compute_g_values` 的直接测试覆盖了。本测试的定位是**集成契约**：确认「被复制改写的数百行 `_sample`」没有在重构中丢失对水印函数的调用。它是「有没有挂上」的护栏，不是「挂得对不对」的检验——后者属于内核测试的职责。这种「分层测试、各司其职」是测试套件设计的关键思想。

## 5. 综合实践

把三个模块串起来，设计一个「给测试套件画地图」的综合任务：

**任务**：通读 `logits_processing_test.py` 与 `synthid_mixin_test.py`，填写下表（不运行，仅靠阅读源码）。

| 测试名 | 验证的性质 | 是否经 `watermarked_call` | 关键断言 | 容差/次数 |
| --- | --- | --- | --- | --- |
| `test_g_value_uniformity_for_random_ngrams` | g 值无偏 | 否 | `≈ 0.5` | `delta=0.01` |
| `test_g_values_uniformity_across_vocab_size` | ? | ? | ? | ? |
| `test_distributional_convergence` | ? | ? | ? | ? |
| `test_bias_from_logits_processor` | ? | ? | ? | ? |
| `test_compute_g_values_shape` | ? | 否 | ? | — |
| `test_sampling_from_mixin_includes_watermarking` | ? | 是（被 mock） | ? | ? |

**要求**：

1. 补全表格，每一行写出「验证的性质」（均匀性 / 分布收敛 / 理论期望 / 形状契约 / 集成性）。
2. 在表外用一段话回答：哪几个测试「绕过水印」、哪几个「经过水印」？这对理解「0.5 vs 0.75」的分野有什么帮助？
3. 选一个均匀性测试，用标准误公式 \(0.5/\sqrt{N}\) 估算它的 `delta` 是否留了足够裕度。

**参考结论**：`uniformity_for_random_ngrams` 与 `uniformity_across_vocab_size` 都是「绕过水印、直接算 g 值」，验证无偏（≈0.5）；`distributional_convergence` 与 `bias_from_logits_processor` 都「经过 `watermarked_call`」，但前者验证 token 维度不偏（≈0.5 的概率）、后者验证 g 值维度有偏（≈0.75）；形状测试只查契约；mixin 测试查集成。把「绕过 vs 经过」分清，就抓住了 0.5 与 0.75 之所以同时成立的全部秘密。

## 6. 本讲小结

- SynthID Text 的「正确性」是**统计性**的，必须用「大样本 + 容差断言」验证，而不是确定性输入输出对比。
- **g 值均匀性测试**：对与密钥无关的随机文本直接算 g 值，断言均值 ≈ 0.5；`batch_size` 必须大，是为了把样本均值的随机波动（\(0.5/\sqrt{N}\)）压到容差以内，避免 flaky。
- **形状测试**最朴素却最有效：直接锁定 g 值、掩码、ngram keys、`watermarked_call` 返回值的张量形状契约，是重构回归的第一道防线。
- **分布收敛性测试**验证水印不扭曲 token 边缘频率（在 key 上取平均后回到原始分布）；**理论期望测试**验证水印确实制造了 g 值偏置（均值精确匹配 `expected_mean_g_value`）。二者看似矛盾却同时成立：token 维度不偏、g 值维度有偏。
- **mixin mock 测试**用 `mock.patch.object(..., autospec=True)` + `side_effect = 真实函数` 的组合，让 mock 既计数又真实执行，以极低成本锁定「`_sample` 必调 `watermarked_call`」这一集成契约。
- 测试套件遵循**分层各司其职**：内核测试（4.1/4.2）管「算得对不对」，集成测试（4.3）管「挂没挂上」。

## 7. 下一步学习建议

- **下一讲（u7-l3）**：把视角从「验证」转到「扩展」，讲清项目的扩展点（自定义 keys、`num_leaves`、distortionary 变体、自定义打分）、稀疏 top_k 的性能取舍，以及参考实现的局限与生产化路径。
- **延伸阅读**：对照本讲提到的源码再读一遍 [[u3-l2]]（`watermarked_call` 5 步主流程）、[[u5-l2]]（`mean_score` 如何利用 0.5 vs 0.75 的差判别），你会更清楚每个测试到底在守护哪一条产品逻辑。
- **动手建议**：尝试为本项目新增一个测试——例如验证 `weighted_mean_score` 的权重全为 1 时退化等于 `mean_score`，借此把本讲学到的「形状契约 + 容差断言」套路用一次。
