# 数据处理与端到端检测 API

> 单元六 · 第 3 讲（u6-l3）。承接 [u6-l2 训练循环](u6-l2-bayesian-training-loop.md)：上一讲我们弄清了「参数从哪来、怎么更新、怎么挑最优 epoch」，本讲回答最后两个问题——**喂给训练器的数据长什么样、怎么造出来**，以及**训练好的检测器怎么用一行 API 给真实文本打分**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `process_outputs_for_training` / `process_raw_model_outputs` 如何把「长度参差不齐的生成 token」加工成「形状统一、可送进 JAX 训练器的 g 值 / 掩码 / 标签」三件套。
- 解释为什么正负样本要用不同的截断长度、为什么 g 值与掩码要做「左侧填充」对齐。
- 理解 `train_best_detector` 如何在 `l2_weights` 网格上做超参搜索、用 CV loss 选出最优检测器。
- 画出 `train_best_detector → score` 的完整端到端调用链，并写出它的调用签名。
- 辨析 README / Notebook 与当前源码的三处不一致，牢记「文档与源码冲突时以源码为准」。

## 2. 前置知识

本讲默认你已经掌握（若生疏请先回看对应讲义）：

- **g 值与深度**（[u2-l3](u2-l3-g-values.md)）：形状 `[batch, seq, depth]`，`depth = len(keys)`，默认 30；每颗 g 值需要一个完整 ngram，故序列维比输入短 `ngram_len - 1`。
- **检测掩码体系**（[u5-l1](u5-l1-detection-masks.md)）：`eos_token_mask` 屏蔽 EOS 之后的填充噪声，`context_repetition_mask` 屏蔽重复上下文，二者相乘得 `combined_mask`；`L = N - (ngram_len - 1)` 是 g 值序列维。
- **贝叶斯检测原理**（[u6-l1](u6-l1-bayesian-principle.md)）：后验 \(P(w\mid g)\) 由两个似然模型 + 先验经贝叶斯公式算出；输入是 g 值 + 掩码。
- **训练循环**（[u6-l2](u6-l2-bayesian-training-loop.md)）：`train` 函数做 minibatch 更新，用 `argmin(val_loss)` 选最优 epoch 写回参数。
- **JAX/Flax 基本概念**：`nn.Module` 用 `apply(params, ...)` 执行；`params` 是一棵 PyTree；`detector.score()` 内部就是对 `apply` 的封装。

一个贯穿全讲的直觉：**贝叶斯检测器是「按密钥定制」的可学习模型**。它不是通用分类器，而是为「这一把 keys 产生的水印」单独训练的。所以训练数据必须是「用同一把 keys 施加水印生成的正样本」+「未水印的负样本」，再加工成统一形状。

## 3. 本讲源码地图

本讲只涉及两个文件（外加 README 作对照）：

| 文件 | 角色 |
|------|------|
| [src/synthid_text/detector_bayesian.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py) | 全部数据加工函数、`BayesianDetector` 包装类、两个 `train_best_detector*` 类方法都在这里 |
| [notebooks/synthid_text_huggingface_integration.ipynb](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb) | 端到端演示：生成正负样本 → 训练 → 打分（cell-22 ~ cell-25） |
| [README.md](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md) | 对照用：其「Detecting a watermark」段已与当前源码脱节，是本讲辨析练习的靶子 |

`detector_bayesian.py` 内部与本讲相关的函数分布（行号）：

| 行号 | 符号 | 作用 |
|------|------|------|
| 37–62 | `pad_to_len` | 把数组填充 / 截断到定长，左填充或右填充可选 |
| 65–85 | `filter_and_truncate` | 按目标长度截断输出，并丢弃有效长度不足的样本 |
| 88–191 | `process_outputs_for_training` | 单个数据分片的核心加工：算掩码、算 g 值、对齐 |
| 443–450 | `BayesianDetectorModule.score` | **模块级**打分：输入 `(g_values, mask)` |
| 711–722 | `BayesianDetector.__init__` | 包装类构造：用 `len(keys)` 当 depth |
| 724–758 | `BayesianDetector.score` | **包装类级**打分：输入只要 token 序列 |
| 760–938 | `process_raw_model_outputs` | 顶层编排：划分 train/CV、加工、打标、shuffle |
| 940–983 | `train_best_detector_given_g_values` | 已有 g 值时的 `l2_weights` 网格搜索 |
| 985–1068 | `train_best_detector` | 最外层入口：从原始 token 一路走到训练好的检测器 |

---

## 4. 核心概念与源码讲解

### 4.1 `process_raw_model_outputs`：把原始 token 变成训练张量

#### 4.1.1 概念说明

训练循环（[u6-l2](u6-l2-bayesian-training-loop.md) 的 `train`）只认一种输入：形状严格统一的

- `g_values: [num, seq_len, depth]`
- `mask:      [num, seq_len]`
- `labels:    [num]`（1=水印，0=非水印）

但真实世界里，模型生成的 token 序列**长度参差不齐**——有的生成 50 个 token 就遇到 EOS，有的撑满 1024。而且正负样本来源不同：正样本是「带水印模型生成」的，负样本在本仓库里取自**维基百科真实文本**（见 Notebook cell-23，用 `wikipedia/20230601.en`）。两者长度分布、词表用法都不一样。

所以需要一道「数据加工流水线」完成：**截断 → 填充到等长 → 重算 g 值与掩码 → 对齐 → 打标签 → 划分 train/CV → 打乱**。这就是 `process_raw_model_outputs` 与它调用的 `process_outputs_for_training` 的职责。

#### 4.1.2 核心流程

顶层 `process_raw_model_outputs` 的编排逻辑（伪代码）：

```
输入: tokenized_wm_outputs（正样本 token 列表）, tokenized_uwm_outputs（负样本 token 列表）

1. 用 sklearn 的 train_test_split 把正、负样本各自切成 train / CV 两份（默认 test_size=0.3）
2. 对 {wm_train, wm_cv, uwm_train, uwm_cv} 四份分别调 process_outputs_for_training:
     - 正样本用 pos_truncation_length（默认 200）截断
     - 负样本 train 用 neg_truncation_length（默认 100）截断
     - 负样本 CV 也用 pos_truncation_length（见 4.1.3 的说明）
3. 每份返回 (masks, g_values) 列表 → torch.cat 拼成大张量
4. 给正样本打 ones 标签、负样本打 zeros 标签
5. 把正负在 dim=0 拼接 → 转 .cpu().numpy() → jnp.squeeze
6. 释放 GPU 显存（del + gc + empty_cache）
7. 对 train / CV 各自随机 shuffle（打乱正负顺序）
8. 返回六元组 (train_g_values, train_masks, train_labels,
              cv_g_values,   cv_masks,   cv_labels)
```

注意第 5 步的**框架切换**：g 值与掩码的**计算**用 PyTorch（复用 `logits_processor`，它在水印侧用 torch），而**训练**用 JAX。两者之间靠 `.cpu().numpy()` 桥接——这正是 [u1-l3](u1-l3-repo-structure.md) 所说「`detector_bayesian.py` 里出现 torch 仅用于预处理施加侧张量」的体现。

#### 4.1.3 源码精读

**① 单分片加工 `process_outputs_for_training`** ——这是本讲最值得逐行读的函数：

[detector_bayesian.py:88-191](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L88-L191) 对每个 batch 做如下处理（关键行）：

```python
# (1) 先算一次 eos_mask，用于按长度过滤
eos_token_mask = logits_processor.compute_eos_token_mask(outputs, eos_token_id=tokenizer.eos_token_id)
# (2) 按正/负、train/CV 选择截断长度
outputs = filter_and_truncate(outputs, pos_or_neg_truncation_length, eos_token_mask)
# (3) 右填充到 max_length（用 eos token 填）
outputs = pad_to_len(outputs, max_length, left_pad=False, eos_token=tokenizer.eos_token_id, ...)
# (4) 在等长 outputs 上重算 eos_mask，再算 context_repetition_mask
eos_token_mask = logits_processor.compute_eos_token_mask(outputs, ...)
context_repetition_mask = logits_processor.compute_context_repetition_mask(outputs)   # 长度 = max_length-(ngram_len-1)
# (5) 把 context_repetition_mask 左填充到 max_length
context_repetition_mask = pad_to_len(context_repetition_mask, max_length, left_pad=True, eos_token=0, ...)
combined_mask = context_repetition_mask * eos_token_mask
# (6) 算 g 值，再左填充到 max_length
g_values = logits_processor.compute_g_values(outputs)   # [B, max_length-(ngram_len-1), depth]
g_values = pad_to_len(g_values, max_length, left_pad=True, eos_token=0, ...)
```

[detector_bayesian.py:130-139](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L130-L139) 里的截断长度选择有个容易看漏的细节：

```python
if is_pos or is_cv:
    outputs = filter_and_truncate(outputs, pos_truncation_length, eos_token_mask)
elif not is_pos and not is_cv:
    outputs = filter_and_truncate(outputs, neg_truncation_length, eos_token_mask)
```

也就是说，**负样本的 CV 份（`is_pos=False, is_cv=True`）也走 `pos_truncation_length`**，只有「负样本 train」用 `neg_truncation_length`。源码注释解释：「We also filter for length when CV negatives are processed」——CV 集统一用正样本长度，使验证集长度分布与正样本可比，让 CV loss 更能反映真实检测场景。

**② 为什么 g 值和 `context_repetition_mask` 要「左填充」？** 这是本讲最精巧的对齐技巧，值得单独讲清楚。

回顾维度关系：`outputs` 是 `[B, max_length]`，而 `g_values` 与 `context_repetition_mask` 的天然长度是 `max_length - (ngram_len - 1)`（因为每颗 g 值需要一个完整 ngram，见 [u2-l3](u2-l3-g-values.md)）。两者长度不等，无法直接和 `eos_token_mask`（`[B, max_length]`）相乘。

仓库选了对齐策略：**把短的序列在左侧补 0，撑到 `max_length`**（[detector_bayesian.py:166-172](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L166-L172) 与 [detector_bayesian.py:183-185](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L183-L185)）。这样 `g_values[t]` 恰好对齐到 token 位置 `t + (ngram_len - 1)`，而左侧 `[0, ngram_len-1)` 这几个补出来的位置在 `combined_mask` 里是 0，天然被屏蔽（mask=0 即丢弃）。一图理解：

```
token 位置:  0   1   2   3   4      (ngram_len=3 时, max_length=5)
outputs:    t0  t1  t2  t3  t4
eos_mask:    1   1   1   0   0      (假设 t3 是 EOS)
g_values:        g0  g1  g2         (天然长度 = 5-2 = 3)
left-pad后:   0   0  g0  g1  g2      ← 撑到 5, 左侧补 0
combined:    0   0  g0  g1  0        ← 与 eos_mask 逐位相乘
```

对比一下：在「手动流程」（README 与 Notebook cell-18、以及本讲的 `BayesianDetector.score`）里，对齐走的是**另一条等价路径**——不动 g 值，而是把 `eos_token_mask` 左切 `[:, ngram_len-1:]` 压到 g 值长度。两种策略殊途同归：一个是「右移 g 值 + 左填 0」，一个是「左切 eos 掩码」。明白这点，你就不会在两段代码里看到不同长度时犯迷糊。

**③ 填充 / 截断工具 `pad_to_len` 与 `filter_and_truncate`**：

[detector_bayesian.py:37-62](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L37-L62) `pad_to_len`：短了就在左或右补指定 `eos_token`（掩码/g 值用 0，token 序列用真 eos id）；长了就直接 `arr[:, :target_len]` 截断。

[detector_bayesian.py:65-85](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L65-L85) `filter_and_truncate`：先截断到 `truncation_length`，再用 `torch.sum(eos_mask, dim=1) >= truncation_length` 这条布尔掩码**整条丢弃**有效长度不足的样本——即「这条文本太短，连截断长度都凑不齐，就不要了」。这就是 [detector_bayesian.py:142-143](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L142-L143) `if outputs.shape[0] == 0: continue` 的由来。

**④ 顶层编排 `process_raw_model_outputs`**：

[detector_bayesian.py:760-938](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L760-L938)。其中 [detector_bayesian.py:863-894](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L863-L894) 完成打标签与正负拼接（`wm` 标 `ones`、`uwm` 标 `zeros`，再 `torch.cat`），[detector_bayesian.py:896-906](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L896-L906) 主动释放显存，[detector_bayesian.py:917-929](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L917-L929) 用 `np.random.shuffle` 打乱 train 与 CV。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一条样本在加工流水线里的形状变化。

**操作步骤**（源码阅读型，无需运行模型）：

1. 假设一条水印输出 `outputs` 形状 `[1, 1024]`，配置 `ngram_len=5`、`max_padded_length=2300`、`pos_truncation_length=200`。
2. 跟着 `process_outputs_for_training` 走，在纸上逐步写出每一步的张量形状：
   - `filter_and_truncate` 后 `outputs` 形状？（答：`[1, 200]`，假设长度足够未被丢弃）
   - `pad_to_len(left_pad=False)` 后？（答：`[1, 2300]`，右侧补 eos）
   - 重算后的 `eos_token_mask` 形状？（答：`[1, 2300]`）
   - `context_repetition_mask` 原始形状？（答：`[1, 2296]`，因 `2300-(5-1)=2296`）
   - `context_repetition_mask` 经 `pad_to_len(left_pad=True)` 后？（答：`[1, 2300]`）
   - `combined_mask` 形状？（答：`[1, 2300]`）
   - `g_values` 原始形状？（答：`[1, 2296, 30]`，depth=30）
   - `g_values` 左填充后？（答：`[1, 2300, 30]`）

**需要观察的现象**：`combined_mask` 与 `g_values` 最终在序列维（2300）上完全对齐，左侧前 4 个位置是补出来的 0（被屏蔽）。

**预期结果**：写出一张「形状变迁表」，确认 `mask` 与 `g_values` 最后一维之前的长度一致，可安全相乘。

> 若你想真跑：可构造一个 `[1, 50]` 的随机 `torch.long` 张量当作 outputs，实例化 `SynthIDLogitsProcessor` 后直接调用 `process_outputs_for_training`，打印每步 `.shape` 验证。运行结果「待本地验证」（取决于本地是否装好 torch/jax 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么正样本用 `pos_truncation_length=200`、负样本 train 用 `neg_truncation_length=100`，而不是同一个值？

> **参考答案**：正负样本来自不同分布（水印生成文本 vs 维基百科），长度分布不同；分别截断可以让两类样本在「各自典型长度」上被刻画，避免一方被过度截断或过度填充。同时负样本截更短（100）能制造「更接近边界」的难例，提升检测器判别力。

**练习 2**：若一条负样本有效长度只有 60，而 `neg_truncation_length=100`，它会被怎样处理？

> **参考答案**：`filter_and_truncate` 里 `torch.sum(eos_mask, dim=1) >= 100` 对它为 False，于是 `truncation_mask` 把它整条滤掉；接着 `if outputs.shape[0] == 0: continue` 跳过该 batch（若该 batch 只剩它）。即「太短的样本直接丢弃，不参与训练」。

---

### 4.2 `train_best_detector`：l2_weights 网格搜索

#### 4.2.1 概念说明

[u6-l2](u6-l2-bayesian-training-loop.md) 讲过：`train` 内部用 `argmin(val_loss)` 选**最优 epoch**（时间维度上的早停）。但还有另一个正交的超参——**L2 正则权重 `l2_weight`**（只罚 `delta`，见 [u6-l2](u6-l2-bayesian-training-loop.md)）。`l2_weight` 太小会过拟合，太大会欠拟合，事先不知道哪个值最好。

于是最朴素可靠的办法就是**网格搜索**：取一组候选 `l2_weights`，每个都从头训一个检测器，比一比谁的 CV loss 最低，留下冠军。这就是 `train_best_detector_given_g_values` 与 `train_best_detector` 的职责——「双重选择」：先在 epoch 维选最优，再在 l2 维选最优。

#### 4.2.2 核心流程

两个类方法是一层套一层的关系：

```
train_best_detector (最外层, 接原始 token)
  ├─ 守卫: 检查 torch_device
  ├─ process_raw_model_outputs → 得到六元组 (train_g/mask/label, cv_g/mask/label)
  └─ train_best_detector_given_g_values (接已算好的 g 值)
       └─ for l2_weight in l2_weights:        # 默认 np.logspace(-3, -2, num=4) → 4 个候选
            ├─ 新建一个空的 BayesianDetectorModule
            ├─ train(...) → 得到 min_val_loss
            └─ if min_val_loss < lowest_loss: 记录 best_detector
       return BayesianDetector(logits_processor, tokenizer, best_detector.params), lowest_loss
```

注意循环里**每个 `l2_weight` 都新建一个全新的 `BayesianDetectorModule`**（[detector_bayesian.py:962-964](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L962-L964)），彼此独立训练，不存在参数继承——这是公平比较的前提。最终 [detector_bayesian.py:983](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L983) 用冠军模块的 `params` 构造一个 `BayesianDetector` 包装类实例返回。

#### 4.2.3 源码精读

**① 已有 g 值的网格搜索 `train_best_detector_given_g_values`**：

[detector_bayesian.py:940-983](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L940-L983)。默认超参（[detector_bayesian.py:952-955](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L952-L955)）：`n_epochs=50`、`learning_rate=2.1e-2`、`l2_weights=np.logspace(-3, -2, num=4)`。`np.logspace(-3, -2, num=4)` 生成 4 个对数均匀分布在 \([10^{-3}, 10^{-2}]\) 的值（约 `0.001, 0.00215, 0.00464, 0.01`）。

循环体（[detector_bayesian.py:961-982](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L961-L982)）每个候选调用 [u6-l2](u6-l2-bayesian-training-loop.md) 的 `train`，拿到该 l2 下的 `min_val_loss`，用 `if min_val_loss < lowest_loss` 留下冠军。

**② 最外层入口 `train_best_detector`**：

[detector_bayesian.py:985-1068](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L985-L1068)。它的文档串（[detector_bayesian.py:1003-1008](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L1003-L1008)）坦言这些超参都建议按自己的数据再调一遍。

⚠️ **一处源码矛盾，请务必留意**。[detector_bayesian.py:1031-1035](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L1031-L1035) 的设备守卫：

```python
if torch_device.type in ("cuda", "tpu"):
    raise ValueError(
        "We have found the training unstable on CPUs; we are working on"
        " a fix. Use GPU or TPU for training."
    )
```

**分支条件**是「设备为 cuda/tpu 时抛错」，但**异常文本**却说「CPU 上不稳定，请改用 GPU/TPU」——文字与逻辑正好相反。以实际分支为准：这段代码**实际只允许在 CPU 上训练**，传入 GPU/TPU 会被拒绝。这是源码里一处明显的笔误（错误信息写反），也意味着 Notebook cell-24 里 `torch_device=DEVICE`（在 Colab 上通常是 `cuda:0`）直接跑会在这一行抛 `ValueError`——这是 Notebook 与当前源码版本脱节的又一证据。**遇到这类矛盾，一律以源码实际分支行为为准。**

**③ 真实调用签名**（这是本讲最该记住的一段）。正确的训练入口是**类方法** `BayesianDetector.train_best_detector`：

```python
detector, loss = detector_bayesian.BayesianDetector.train_best_detector(
    tokenized_wm_outputs=wm_outputs,        # 水印正样本: Sequence[np.ndarray] 或 np.ndarray
    tokenized_uwm_outputs=tokenized_uwm_outputs,  # 非水印负样本
    logits_processor=logits_processor,       # 施加水印时用的同一个 processor(决定 keys/depth)
    tokenizer=tokenizer,                     # 提供 eos_token_id
    torch_device=torch_device,               # 注意上面的设备守卫
    test_size=0.3,                           # train/CV 划分比例
    pos_truncation_length=200,
    neg_truncation_length=100,
    max_padded_length=2300,
    n_epochs=50,
    learning_rate=2.1e-2,
    l2_weights=np.logspace(-3, -2, num=4),
    verbose=False,
)
# 返回: (BayesianDetector 实例, 最优 CV loss)
```

Notebook cell-24（[第 812 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb)）用的正是这个方法（它覆盖了 `max_padded_length=1000`、`pos/neg_truncation_length=200`、`learning_rate=3e-3`、`n_epochs=100`、`l2_weights=np.zeros((1,))`）。

如果你已经手动算好了 g 值（比如想复用 [u5-l1](u5-l1-detection-masks.md) 的产物），可以跳过数据加工，直接调更内层的 [detector_bayesian.py:940](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L940) `train_best_detector_given_g_values`，它的签名只接收六元组 g 值/掩码/标签。

#### 4.2.4 代码实践

**实践目标**：辨析 README 与源码的训练入口差异，写出正确签名。（这是本讲指定的核心实践。）

**操作步骤**：

1. 打开 [README.md 的 "Detecting a watermark" 段](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md)，找到这段示例代码：

   ```python
   from synthid_text import train_detector_bayesian
   ...
   detector, loss = train_detector_bayesian.optimize_model(...)
   ```

2. 在仓库根目录用 `ls src/synthid_text/` 列出文件（或回看 [u1-l3](u1-l3-repo-structure.md) 的源码地图），确认**是否存在 `train_detector_bayesian.py`**。
3. 在 `detector_bayesian.py` 里 `grep` `optimize_model`，确认它**是否真的存在**。
4. 对照 Notebook cell-24，确认真实入口是哪个类的哪个方法。

**需要观察的现象 / 预期结果**：

- `src/synthid_text/` 下**没有** `train_detector_bayesian.py`，只有 `detector_bayesian.py`。
- `grep optimize_model` 在整个 `src/` 下**零命中**——该方法在当前代码中不存在。
- README 的 `[synthid-detector-trainer]` 链接指向 `./src/synthid_text/train_detector_bayesian.py`，这是一个**失效链接**（目标文件不存在）。
- 正确入口是 `detector_bayesian.BayesianDetector.train_best_detector(...)`（类方法，非模块级函数），签名见上文 ③。

> 这是全手册反复强调的原则的活样本：**文档（README）与源码冲突时，以源码为准**。README 描述的是较早版本的 API，当前版本已把训练逻辑收敛进 `BayesianDetector` 类方法。

#### 4.2.5 小练习与答案

**练习 1**：为什么网格搜索循环里要「每个 l2_weight 都新建一个 `BayesianDetectorModule`」，而不是共用一个模块反复训练？

> **参考答案**：为了保证每个候选 l2 在**完全相同、彼此独立**的起点上训练，这样 CV loss 的比较才公平。若共用一个模块，后一个 l2 会继承前一个训出的参数，比较结果被污染。

**练习 2**：默认 `l2_weights=np.logspace(-3, -2, num=4)` 会训出几个检测器？最终返回的是其中哪一个？

> **参考答案**：训出 4 个。返回 CV loss（`min_val_loss`）最低的那一个对应的参数，封装成 `BayesianDetector` 返回，同时返回这个最低 loss。注意每个候选内部本身已经过 `train` 选了最优 epoch，所以是「4 个候选 × 各自最优 epoch」中的全局最优。

---

### 4.3 `BayesianDetector.score`：端到端打分

#### 4.3.1 概念说明

训练拿到 `detector` 后，最终目的是**给一段新文本打分**，输出一个 \([0,1]\) 的数——越接近 1 越可能是「用这把 keys 生成的水印文本」。

这里有一个容易踩坑的点：项目里有**两个名字都叫 `score` 的方法**，签名完全不同：

| 方法 | 位置 | 输入 | 适合谁调 |
|------|------|------|----------|
| `BayesianDetectorModule.score` | [行 443](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L443-L450) | `(g_values, mask)` 已预处理好 | 内部 / 高级用法 |
| `BayesianDetector.score` | [行 724](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L724-L758) | `(outputs,)` 只要 token 序列 | **推荐的端到端入口** |

包装类 `BayesianDetector.score` 是「最省事」的入口：你不用自己算 g 值和掩码，把生成文本的 token id 喂进去就行——它内部自己重算。这正呼应 [u1-l4](u1-l4-end-to-end-pipeline.md) 的节奏：「检测阶段不信任生成时状态，只用输出 token 序列加密钥重算」。

#### 4.3.2 核心流程

`BayesianDetector.score(outputs)` 内部做了和 [u5-l1](u5-l1-detection-masks.md) 手动流程一模一样的事，只是封装成了一步：

```
score(outputs[B, output_len])
  ├─ eos_token_mask = compute_eos_token_mask(outputs)[:, ngram_len-1:]   # 左切到 L 维
  ├─ context_repetition_mask = compute_context_repetition_mask(outputs)  # 天然 L 维
  ├─ combined_mask = context_repetition_mask * eos_token_mask            # [B, L]
  ├─ g_values = compute_g_values(outputs)                                # [B, L, depth]
  └─ return detector_module.score(g_values.numpy(), combined_mask.numpy())
            └─ module.apply(params, g_values, mask, method=__call__) → P(w|g)  [B]
```

这里 \(L = \text{output\_len} - (\text{ngram\_len} - 1)\)。注意它走的是「左切 eos 掩码」的对齐策略（与 4.1 里训练流水线的「左填 g 值」策略等价，见 4.1.3 ②的对比）。

#### 4.3.3 源码精读

**① 包装类构造 `BayesianDetector.__init__`**：

[detector_bayesian.py:711-722](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L711-L722)。关键一行：`BayesianDetectorModule(watermarking_depth=len(logits_processor.keys), params=params)`——**depth 直接由 keys 数量决定**，所以训练用的 processor 和打分用的必须是同一把 keys，否则 depth 对不上、参数形状不匹配。

**② 端到端打分 `BayesianDetector.score`**：

[detector_bayesian.py:724-758](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L724-L758)。注意它的形参只有 `outputs: jnp.ndarray`（[行 724](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L724)），内部用 `self.logits_processor` 与 `self.tokenizer` 重算掩码与 g 值，最后 [行 756-757](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L756-L757) 调底层模块的 `score`，并 `.cpu().numpy()` 完成 torch→jax 的桥接。

**③ 底层模块打分 `BayesianDetectorModule.score`**：

[detector_bayesian.py:443-450](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L443-L450)。

```python
def score(self, g_values, mask):
    if self.params is None:
        raise ValueError("params must be set before calling score")
    return self.apply(self.params, g_values, mask, method=self.__call__)
```

它先断言参数已就位，再用 Flax 的 `apply(params, ...)` 执行 `__call__`——后者就是 [u6-l1](u6-l1-bayesian-principle.md) 讲的 `_compute_posterior`，输出后验 \(P(w\mid g)\)，形状 `[batch]`。

⚠️ **又一处文档/调用方与源码的不一致**。README 与 Notebook cell-25 都这样调：

```python
bayesian_detector.score(g_values.cpu().numpy(), combined_mask.cpu().numpy())   # 双参
```

但当前源码的 `BayesianDetector.score(self, outputs)` 是**单参**的（只收 token 序列）。这种「双参」调用其实对应的是底层 `BayesianDetectorModule.score(g_values, mask)`，而不是包装类。也就是说，Notebook cell-25 的写法对当前版本的 `BayesianDetector` 实例**会因参数个数不符而报错**。以源码为准：对 `BayesianDetector` 实例应调 `detector.score(outputs)`（单参，传 token id）。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读型」方式，确认两种 `score` 的区别与正确用法。

**操作步骤**：

1. 打开 `detector_bayesian.py`，分别读 [行 443-450](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L443-L450)（`BayesianDetectorModule.score`）和 [行 724-758](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L724-L758)（`BayesianDetector.score`），数一数各自形参个数。
2. 对照 Notebook cell-25 的 `bayesian_detector.score(wm_g_values..., wm_mask...)`，判断它传了几个参数、匹配哪一个 `score`。
3. 写出「以源码为准」的正确端到端调用片段。

**需要观察的现象 / 预期结果**：

- `BayesianDetectorModule.score` 形参 2 个（`g_values, mask`）；`BayesianDetector.score` 形参 1 个（`outputs`）。
- Notebook cell-25 传 2 个参数，匹配的是**模块级** `score`，而非包装类——与当前 `BayesianDetector` 实例签名不符。
- 正确写法（源码为准）：

  ```python
  # detector 是 BayesianDetector.train_best_detector(...) 的返回值
  scores = detector.score(outputs)   # outputs: [batch, output_len] 的 token id
  # 返回 scores: [batch]，值域 [0,1]，越大越像水印
  ```

  若你已自行算好 g 值与掩码，则应改用底层模块：`detector.detector_module.score(g_values, mask)`。

> 这两处 `score` 不一致与 4.2 的 `optimize_model` 失效、设备守卫笔误一起，构成了 README/Notebook 与当前源码版本的三处脱节。它们都是「以源码为准」原则的活样本——读参考实现时，永远拿源码当 ground truth。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BayesianDetector.__init__` 要用 `len(logits_processor.keys)` 作为 `watermarking_depth`，而不是写死 30？

> **参考答案**：因为贝叶斯检测器是「按密钥定制」的，depth 必须与训练时那把 keys 的层数严格一致，否则似然模型里 `beta`、`delta` 的形状对不上、参数无法加载。从 `logits_processor.keys` 取长度，能保证训练与打分用同一份配置。

**练习 2**：`BayesianDetector.score` 内部为什么要把 torch 张量 `.cpu().numpy()` 再传给 `detector_module.score`？

> **参考答案**：掩码与 g 值用 `logits_processor`（PyTorch）算出，是 torch 张量；而检测模块是 JAX/Flax 的，`apply` 只吃 jax/numpy 数组。`.cpu().numpy()` 是跨越「水印侧 torch → 检测侧 jax」这道框架分水岭的桥（见 [u1-l3](u1-l3-repo-structure.md)）。

---

## 5. 综合实践

把本讲三个模块串成一条完整的「数据 → 训练 → 打分」链。考虑到完整训练需要上规模数据与模型生成（Notebook 里用了约 1 万条样本），这里给一个**轻量可执行 + 源码阅读混合**的任务。

**任务**：用随机数据走通「`train_best_detector_given_g_values` → `score`」的调用链，验证形状与返回值。

**操作步骤**：

1. **造合成数据**（示例代码，非项目原有代码）：

   ```python
   import numpy as np, torch, jax.numpy as jnp
   from synthid_text import logits_processing, detector_bayesian
   import transformers

   # 假装一把 keys（层数 depth=8，调小以便快速跑）
   CONFIG = dict(ngram_len=5, keys=[1,2,3,4,5,6,7,8], context_history_size=2,
                 sampling_table_size=0, sampling_table_seed=0, device=torch.device('cpu'))
   lp = logits_processing.SynthIDLogitsProcessor(**CONFIG, top_k=40, temperature=0.5)
   depth = len(CONFIG['keys'])

   # 合成正/负 g 值：正样本偏向 1（模拟水印），负样本近似无偏（0.5）
   N, L = 200, 60
   rng = np.random.default_rng(0)
   wm_g  = (rng.random((N, L, depth)) < 0.75).astype('float32')
   uwm_g  = (rng.random((N, L, depth)) < 0.50).astype('float32')
   train_g = jnp.asarray(np.concatenate([wm_g[:120], uwm_g[:120]]))
   cv_g    = jnp.asarray(np.concatenate([wm_g[120:], uwm_g[120:]]))
   train_label = jnp.asarray(np.concatenate([np.ones(120), np.zeros(120)]).astype('float32'))
   cv_label    = jnp.asarray(np.concatenate([np.ones(80),  np.zeros(80)]).astype('float32'))
   train_mask  = jnp.ones((240, L))
   cv_mask     = jnp.ones((160, L))
   ```

2. **训练**（用合成数据上的网格搜索；`l2_weights` 缩小到 2 个以加快速度）：

   ```python
   tokenizer = transformers.AutoTokenizer.from_pretrained('gpt2')
   detector, loss = detector_bayesian.BayesianDetector.train_best_detector_given_g_values(
       train_g_values=train_g, train_masks=train_mask, train_labels=train_label,
       cv_g_values=cv_g, cv_masks=cv_mask, cv_labels=cv_label,
       logits_processor=lp, tokenizer=tokenizer,
       n_epochs=20, l2_weights=np.array([0.0, 1e-3]), verbose=False,
   )
   ```

3. **打分**（注意：以源码为准，`BayesianDetector.score` 单参收 token 序列；这里为了用合成 g 值，改走底层模块）：

   ```python
   scores = detector.detector_module.score(cv_g[:5], cv_mask[:5])
   print(scores)   # 形状 [5]，正样本应偏高
   ```

**需要观察的现象**：训练打印 `Best val Epoch ...`；`scores` 中前 5 条（水印正样本）的平均分应明显高于 0.5（因为合成时把正样本均值设为 0.75）。

**预期结果**：得到一个形状正确的 `[batch]` 分数数组，水印样本分数偏高。完整跑通依赖本地 torch/jax 环境，具体数值「待本地验证」。

> 这个综合实践同时检验了三个知识点：合成数据要满足「正样本 g 均值偏高、负样本近似 0.5」（呼应 [u5-l2](u5-l2-mean-scoring.md) 的统计直觉）、`train_best_detector_given_g_values` 的签名与网格搜索、以及两个 `score` 的区别。

## 6. 本讲小结

- **数据加工**是贝叶斯检测器能用起来的前提：`process_raw_model_outputs` 把参差不齐的 token 序列截断、右填 eos 到 `max_padded_length`，再重算 g 值与掩码，并把 g 值/`context_repetition_mask` **左填充**对齐到同一长度，最后打标签、划分 train/CV、shuffle，产出六元组。
- **截断策略不对称**：正样本用 `pos_truncation_length`，负样本 train 用 `neg_truncation_length`，负样本 CV 也用正样本长度；太短的样本被 `filter_and_truncate` 整条丢弃。
- **框架分水岭**贯穿加工：g 值/掩码用 torch 算，训练用 jax，靠 `.cpu().numpy()` 桥接。
- **双重选择**：`train_best_detector` 在 `l2_weights` 网格上搜，每个候选内部再由 `train` 选最优 epoch，留下 CV loss 全局最低的检测器。
- **端到端入口**是类方法 `BayesianDetector.train_best_detector(...)`，打分用 `detector.score(outputs)`（单参，传 token 序列）；两个 `score`（模块级双参 vs 包装类单参）不可混用。
- **以源码为准**：README 的 `train_detector_bayesian.optimize_model` 与文件链接已失效、Notebook 的双参 `score` 与当前包装类签名不符、设备守卫文本与分支相反——三处脱节都以源码实际行为为准。

## 7. 下一步学习建议

本讲是单元六（贝叶斯检测器）的收尾，你已经走通了「原理 → 训练 → 数据与 API」的完整链路。接下来进入**单元七（理论、测试与扩展）**：

- [u7-l1 理论期望值](u7-l1-theoretical-expectations.md)：用 `expected_mean_g_value` 算「均匀分布下 num_leaves=2/3 的理论 g 均值」，校验本讲综合实践里「正样本≈0.75」这个数字的来历。
- [u7-l2 测试套件](u7-l2-test-suite.md)：看 `logits_processing_test.py` 如何用统计测试验证 g 值无偏、分布收敛，理解本讲数据加工的正确性是如何被自动化保证的。
- [u7-l3 性能与扩展](u7-l3-extensions.md)：把 Mean 打分换成自定义加权打分、把 `train_best_detector` 的超参搜索换成贝叶斯优化等二次开发方向。

如果想再巩固本讲，建议回头读一遍 `detector_bayesian.py` 第 760–1068 行，把 `process_raw_model_outputs → train_best_detector_given_g_values → train → score` 这条调用链在脑子里完整跑一遍。
