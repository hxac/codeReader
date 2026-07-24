# 端到端流程总览：施加→生成→检测

> 本讲是单元一（认识 SynthID Text）的最后一讲。前几讲我们分别建立了「项目是什么」（[u1-l1](u1-l1-project-overview.md)）和「源码长什么样、文件怎么归类」（[u1-l3](u1-l3-repo-structure.md)）的认知。本讲把这两块拼起来：沿着官方 Notebook 的真实代码，把整条「输入一段提示 → 生成带水印文本 → 给出一个 [0,1] 检测分数」的数据流走一遍。
>
> 本讲只讲**全局直觉**，不深挖每个函数的内部实现——那些留给后续单元（u2~u7）。读完本讲你应该知道：流程一共有几步、每一步对应哪个 API、这些 API 分别在哪个文件里。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清「从一段 prompt 到得到检测分数」的完整数据流，并指出每一步对应哪个类或函数。
2. 识别流程中三类关键 API——**水印模型类**、**logits processor**、**打分函数**——并知道它们分别定义在哪个源码文件。
3. 理解 SynthID Text 最核心的节奏：**生成时埋水印、检测时重算 g 值**，并明白为什么检测阶段不依赖生成时的内部状态。
4. 建立「先整体后细节」的学习预期，知道本讲提到的每个点在后续哪一讲会被展开。

## 2. 前置知识

本讲会用到的几个基础概念（如果你已经熟悉语言模型生成，可以跳过）：

- **logits 与下一个 token 采样**：语言模型每一步输出一个覆盖整个词表的分数向量（logits），再从中「采样」出下一个 token。`temperature`（越大越随机）、`top_k`（只在小范围高分 token 里挑）、`top_p` 是控制采样随机性的常见旋钮。
- **水印不是事后打标记**：SynthID 的做法是在**采样之前**对 logits 做一点点不易察觉的偏置，让生成的文本带上统计信号。对使用者来说，调用 `model.generate(...)` 的方式和普通模型**几乎完全一样**。
- **g 值（贯穿全项目的核心数据）**：把若干连续 token（ngram）和水印密钥一起哈希，得到的「二进制指纹」。生成时用它来决定偏置方向，检测时用它来还原统计信号。本讲只把它当成「一个 0/1 的指纹数组」即可，详细的哈希与取位逻辑在 [u2-l3](u2-l3-g-values.md)。
- **掩码（mask）**：一个 0/1 数组，用来标记「哪些位置的 g 值是有效的、应当参与打分」。例如遇到结束符 `<eos>` 之后的内容就该被忽略。

承接 [u1-l1](u1-l1-project-overview.md)：系统分**水印施加**（PyTorch）与**水印检测**（JAX/Flax）两阶段；承接 [u1-l3](u1-l3-repo-structure.md)：两侧通过 **g 值**衔接，`logits_processing.py` 同时承担「生成时用」和「检测时重算」两份职责。本讲就是要把这条衔接线用真实代码连起来。

## 3. 本讲源码地图

本讲围绕「主线 Notebook」展开，并把它调用的 API 回指到真实源码文件：

| 文件 | 在本讲中的角色 |
| --- | --- |
| `notebooks/synthid_text_huggingface_integration.ipynb` | **主线**：自包含的端到端示例，按 Setup / Applying / Detecting 三节组织 |
| `README.md` | 安装方式与「How it works」三段式说明（含可直接复制的最小代码） |
| `src/synthid_text/synthid_mixin.py` | 水印模型类（`SynthIDGPT2LMHeadModel` 等）+ 静态默认配置 |
| `src/synthid_text/logits_processing.py` | 水印处理器 `SynthIDLogitsProcessor`，以及检测侧的 g 值与掩码计算函数 |
| `src/synthid_text/detector_mean.py` | 免训练的 Mean / Weighted Mean 打分 |
| `src/synthid_text/detector_bayesian.py` | 贝叶斯打分及其训练入口 |

> 提示：Notebook 里出现的 API 名字，我们都会在「源码精读」里给出指向 `.py` 定义的永久链接。Notebook 本身按单元格（cell）组织，下文用「cell-N」指代第 N 个单元格。

## 4. 核心概念与源码讲解

Notebook 的开头把全流程分成三大节：**1. Setup**、**2. Applying a watermark**、**3. Detecting a watermark**（见 [notebooks/synthid_text_huggingface_integration.ipynb](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb) 的 cell-0）。本讲按三个最小模块拆解：**水印化生成**、**g 值与掩码重算**、**打分输出**。

### 4.1 水印化生成

#### 4.1.1 概念说明

「水印化生成」要解决的问题是：**怎么让模型在生成文本的同时、悄悄把水印埋进去，又不改变用户的使用习惯？**

SynthID 的答案是用一个 **logits processor**（分数处理器）。在模型每一步算出 logits 之后、真正采样下一个 token 之前，这个处理器会根据「当前的 ngram 上下文 + 水印密钥」算出一个 g 值，再用 g 值对候选 token 的分数做一点点偏置。对调用方而言，模型类、`tokenizer`、`model.generate(...)` 的用法都和 HuggingFace 原版一模一样，唯一区别是加载的类名换成了带 `SynthID` 前缀的子类。

#### 4.1.2 核心流程

对应 Notebook 的 Setup（cell-5、cell-6）与 Applying（cell-9）：

```text
1. 取默认水印配置
     CONFIG = synthid_mixin.DEFAULT_WATERMARKING_CONFIG        # cell-5
2. 用 transformers 标准方式准备 tokenizer 与输入               # cell-6
3. 用 SynthID 子类加载模型（关键：多重继承 Mixin）            # cell-7 的 load_model / cell-9
4. model.generate(do_sample=True, temperature=, top_k=, ...)  # cell-9
        └─ 内部进入 Mixin 重写过的采样循环 _sample
              └─ 每一步调用 logits processor 的 watermarked_call 改写 scores
                  └─ 在被偏置过的分数上采样出下一个 token
```

要点：水印是在 `generate` 内部、逐 token 自动施加的；调用方感知不到中间的处理器。

#### 4.1.3 源码精读

**① 默认配置**。Notebook cell-5 用的就是一个写死的静态配置，对应源码中的 `DEFAULT_WATERMARKING_CONFIG`：

- [src/synthid_text/synthid_mixin.py:27-67](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L27-L67) —— 仓库内置的静态默认配置。注意第 28 行的注释：`ngram_len=5` 对应论文里 `H=4` 的上下文窗口；`keys` 是一串互不相同的整数，`len(keys)` 决定水印的层数（深度）。（详见 [u2-l1](u2-l1-watermarking-config.md)。）

**② 水印模型子类**。cell-7 的 `load_model` 根据是否开启水印，选择带不带 `SynthID` 前缀的类：

- [src/synthid_text/synthid_mixin.py:396-405](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L396-L405) —— `SynthIDGPT2LMHeadModel` 与 `SynthIDGemmaForCausalLM` 的全部定义。它们都只是「`SynthIDSparseTopKMixin` + transformers 原模型」的多重继承，类体为空（`pass`）。这正是「API 与原模型一致、但自带水印」的实现方式。（详见 [u4-l3](u4-l3-model-subclasses.md)。）

**③ logits processor 的构造**。cell-6 把配置展开后实例化处理器：

- [src/synthid_text/logits_processing.py:135-160](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L135-L160) —— `SynthIDLogitsProcessor.__init__` 的签名。它接收 `ngram_len`、`keys`、`context_history_size`、`temperature`、`top_k` 等参数，并把 `keys` 哈希成一个不可预测的初始向量（IV）。

**④ 真正的水印入口**。这里有一个**容易踩坑**的点：transformers 标准 logits processor 用的是 `__call__`，但本项目的 `__call__` 被故意设成抛异常：

- [src/synthid_text/logits_processing.py:214-221](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L214-L221) —— `__call__` 直接 `raise NotImplementedError`。原因是 SynthID 需要一个**带状态**、且要返回「top_k 索引映射」的非标准接口，所以真正的入口是 `watermarked_call`。
- [src/synthid_text/logits_processing.py:223-326](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L223-L326) —— `watermarked_call` 是水印施加的主流程，源码里清晰地标注了 5 步：①温度缩放 + 取 top_k（L245-L246）→ ②滑动上下文、计算 ngram keys（L289）→ ③取哈希低位得到 g 值（L295）→ ④用 g 值修正 scores（L299-L304）→ ⑤检测重复上下文并按需跳过水印（L307-L325）。本讲只需记住「它在采样前改写了 scores」；5 步细节留给 [u3-l2](u3-l2-watermarked-call.md)。

#### 4.1.4 代码实践

**实践目标**：在不实际运行的前提下，写出「用 SynthID 版 GPT-2 生成水印文本」的最小代码片段，并标出它与普通 `GPT2LMHeadModel` 的唯一差异点。

**操作步骤**：

1. 参考 Notebook cell-6、cell-9 与 README「Applying a watermark」一节。
2. 写出：导入 → 加载 tokenizer → 用 `SynthIDGPT2LMHeadModel.from_pretrained(...)` 加载模型 → `model.generate(do_sample=True, ...)`。
3. 圈出与普通 GPT-2 用法的唯一区别。

**预期结果**（示例答案，供对照）：

```python
import transformers, torch
from synthid_text import synthid_mixin

tokenizer = transformers.AutoTokenizer.from_pretrained("gpt2")
# 唯一差异：类名从 transformers.GPT2LMHeadModel 换成 SynthID 版子类
model = synthid_mixin.SynthIDGPT2LMHeadModel.from_pretrained("gpt2", device_map="auto")
inputs = tokenizer("I enjoy walking with my cute dog", return_tensors="pt")
outputs = model.generate(**inputs, do_sample=True, max_length=1024, temperature=0.5, top_k=40)
```

差异点只有一处：**模型加载用的类名**。`generate` 的调用方式、参数、返回值与原版完全一致。其余配置（`keys` 等）走的是类内部写死的静态默认配置，不需要你显式传入。

> 说明：以上为「源码阅读型实践」，无需运行；若要真正生成文本，可参照 README 用虚拟环境安装 `.[notebook-local]` 后在 Notebook 中执行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SynthIDGPT2LMHeadModel` 的类体是空的（只有 `pass`），却能让生成的文本带上水印？

> **答案**：因为它通过多重继承了 `SynthIDSparseTopKMixin`，后者覆盖了 transformers 的采样循环（`_sample`）和 logits warper 构造逻辑，把 `SynthIDLogitsProcessor` 注入进去。水印能力来自 Mixin，子类本身不需要再写任何代码。

**练习 2**：`SynthIDLogitsProcessor.__call__` 会被调用吗？

> **答案**：不会。它在 [logits_processing.py:214-221](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L214-L221) 中被实现为直接抛 `NotImplementedError`；真正生效的是 `watermarked_call`，由 Mixin 的采样循环显式调用。

### 4.2 g 值与掩码重算

#### 4.2.1 概念说明

检测阶段通常在**离线**进行：你拿到一段文本，既没有生成时的内部状态，也不应该相信「这段文本自己声称被水印过」。因此 SynthID 的检测思路是——**只看输出 token 序列本身，结合水印密钥，重新把 g 值算出来**，再统计这些 g 值是否符合「被水印过」应有的分布。

但并不是序列里每个位置都该参与统计：

- 遇到结束符 `<eos>` 之后的内容是填充/无效的，要截掉 → 由 **eos 掩码**处理。
- 出现「重复上下文」的位置在生成时本来就没有被水印（见 `watermarked_call` 第 5 步），检测时也不该算进去 → 由 **上下文重复掩码**处理。

把两者相乘就得到最终决定「哪些 g 值有效」的 `combined_mask`。

#### 4.2.2 核心流程

对应 Notebook cell-18 的 `generate_responses` 函数：

```text
输入：outputs[:, inputs_len:]           # 只取「新生成」的那部分 token（去掉 prompt）
  │
  ├─ compute_eos_token_mask(outputs, eos_token_id)
  │      → 在第一个 <eos> 处截断；再切掉前 ngram_len-1 个位置以与 g 值对齐
  │      → 形状 [batch, output_len - (ngram_len-1)]
  │
  ├─ compute_context_repetition_mask(outputs)
  │      → 标记重复上下文位置为 0
  │      → 形状 [batch, output_len - (ngram_len-1)]
  │
  ├─ combined_mask = context_repetition_mask * eos_token_mask
  │
  └─ compute_g_values(outputs)
         → 形状 [batch, output_len - (ngram_len-1), depth]
```

三条掩码/g 值的「序列维」长度都对齐到 `output_len - (ngram_len - 1)`，这是后续打分能直接相乘的前提。

#### 4.2.3 源码精读

**① 重算 g 值**。检测时和生成时用的是**同一个**函数族，只是入口换成面向整条序列的 `compute_g_values`：

- [src/synthid_text/logits_processing.py:458-473](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L458-L473) —— `compute_g_values`。它先用 `unfold` 把整条 token 序列切成一个个长度为 `ngram_len` 的滑动窗口，再算 ngram keys，最后取哈希低位得到 g 值。返回形状为 `[batch, output_len - (ngram_len - 1), depth]`——这也解释了为什么序列长度会「缩水」`ngram_len - 1`：因为最前面凑不齐一个完整 ngram。（详见 [u2-l3](u2-l3-g-values.md)。）

**② eos 掩码**：

- [src/synthid_text/logits_processing.py:527-552](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L527-L552) —— `compute_eos_token_mask`。逐行找到第一个 `<eos>`，把它及其之后的位置全部置 0。返回形状 `[batch, output_len]`，所以 Notebook 还要做 `[:, ngram_len-1:]` 的切片来与 g 值对齐。

**③ 上下文重复掩码**：

- [src/synthid_text/logits_processing.py:475-525](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L475-L525) —— `compute_context_repetition_mask`。它重建一个 `SynthIDState`，逐个上下文哈希并查重，凡是「之前见过的上下文」就标 0。返回形状 `[batch, output_len - (ngram_len - 1)]`。（详见 [u3-l4](u3-l4-context-repetition.md)。）

**④ Notebook 的组合**。cell-18 把上面三者组合，返回 `(g_values, combined_mask)` 给后续打分使用。

#### 4.2.4 代码实践

**实践目标**：理解三个张量的形状与对齐关系。

**操作步骤**：

1. 假设 `ngram_len=5`，某条输出序列去掉 prompt 后长度为 `output_len=12`，且第 9 个位置是 `<eos>`。
2. 写出 `compute_g_values`、`compute_eos_token_mask`（切片后）、`compute_context_repetition_mask` 三者的序列维长度。
3. 画一张表，标出 `combined_mask` 里哪些位置为 0、为什么。

**预期结果**：

- `compute_g_values` 序列维长度 = `12 - (5-1) = 8`。
- `compute_eos_token_mask` 原始长度 12，切片 `[:, 4:]` 后长度也是 8。
- `compute_context_repetition_mask` 序列维长度也是 8。
- 三者对齐到长度 8，可直接逐元素相乘。`combined_mask` 中第 9 个 token 及之后对应的位置为 0（被 eos 截断）；若某上下文重复，则该位置也为 0。

> 说明：以上为「源码阅读 + 手算型实践」，无需运行；具体取值取决于你设的序列内容，故标注「待本地验证」的是「重复上下文出现的确切位置」，而形状关系由源码 docstring 可直接推出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `compute_g_values` 输出的序列长度比输入少了 `ngram_len - 1`？

> **答案**：因为 g 值是按长度为 `ngram_len` 的滑动窗口计算的（`unfold(size=ngram_len, step=1)`）。一条长 `L` 的序列能切出 `L - ngram_len + 1` 个窗口，所以少了 `ngram_len - 1`。

**练习 2**：`combined_mask = context_repetition_mask * eos_token_mask` 中，两个掩码的形状必须满足什么条件？

> **答案**：两者的序列维长度必须相等（都是 `output_len - (ngram_len - 1)`），否则无法逐元素相乘。这正是 Notebook 要对 `eos_token_mask` 做 `[:, ngram_len-1:]` 切片的原因。

### 4.3 打分输出

#### 4.3.1 概念说明

拿到对齐好的 `(g_values, combined_mask)` 之后，最后一步是把它压缩成一个 `[0, 1]` 的**每条样本分数**，分数越接近 1 越像「被这个配置水印过」。Notebook 在 cell-16 给出两条路线：

1. **Mean / Weighted Mean**：免训练，直接对 g 值做（加权）平均。快，但判别力较弱。
2. **Bayesian**：更强，但需要**针对每一个水印密钥单独训练**一个检测器。

两条路线的输入输出形状一致：`g_values [batch, seq, depth]` + `mask [batch, seq]` → 分数 `[batch]`。

#### 4.3.2 核心流程

**Mean 路线**（Notebook cell-20）：

```text
wm_mean_scores = detector_mean.mean_score(wm_g_values, wm_mask)
```

**Bayesian 路线**（Notebook cell-22 ~ cell-25）：

```text
1. 生成一批水印样本 wm_outputs（cell-22）与一批非水印样本 tokenized_uwm_outputs（cell-23，取自 wikipedia）
2. BayesianDetector.train_best_detector(wm, uwm, logits_processor, tokenizer, ...) → (detector, loss)   # cell-24
3. detector.score(...) → [0,1] 分数                                                                       # cell-25
```

#### 4.3.3 源码精读

**① Mean 打分公式**：

- [src/synthid_text/detector_mean.py:22-41](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L22-L41) —— `mean_score`。把掩码为 1 的 g 值求和，再除以「层数 × 未掩码位置数」。写成公式（\(D\) 为层数，\(m_i\) 为掩码）：

  \[ \text{mean\_score} = \frac{\sum_{i}\sum_{d} g_{i,d}\, m_i}{D \cdot \sum_{i} m_i} \]

**② Weighted Mean 打分**：

- [src/synthid_text/detector_mean.py:44-77](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L44-L77) —— `weighted_mean_score`。默认对深度方向施加从 10 线性递减到 1 的权重（L66），再归一化、加权求和。直观理解：越靠前的层（与 `keys[0]` 相关）权重越高。公式为（\(w_d\) 为归一化后权重）：

  \[ \text{wm\_score} = \frac{\sum_{i}\sum_{d} g_{i,d}\, w_d\, m_i}{D \cdot \sum_{i} m_i} \]

  （详见 [u5-l2](u5-l2-mean-scoring.md)。）

**③ Bayesian 训练入口与打分**：

- [src/synthid_text/detector_bayesian.py:986-1002](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L986-L1002) —— `BayesianDetector.train_best_detector` 类方法。接收水印/非水印的 token 序列、`logits_processor`、`tokenizer`、`torch_device` 等参数，内部完成数据处理、训练与超参搜索，返回 `(BayesianDetector, loss)`。**注意**：这是当前源码中**真实存在**的训练入口（README 里写的 `train_detector_bayesian.optimize_model` 在本仓库中并不存在——这是文档与源码不一致的一处，以源码为准，详见 [u6-l3](u6-l3-bayesian-data-and-api.md)）。
- [src/synthid_text/detector_bayesian.py:724-734](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L724-L734) —— `BayesianDetector.score`。它返回 `[0, 1]` 区间的每条样本分数（docstring：0 表示未水印，1 表示水印）。

> ⚠️ **一个需要如实说明的细节**：当前源码里 `BayesianDetector.score(outputs)` 接收的是**原始 token 序列**，并在内部（L735-L753）自行重算 eos 掩码、上下文重复掩码、`combined_mask` 和 g 值；而 Notebook cell-25 与 README 示例则演示了「先把 `g_values`、`combined_mask` 算好再传入」的写法。这两者在调用签名上存在差异，**本讲不下定论**，精确的调用约定与内部流程留给 [u6-l1](u6-l1-bayesian-principle.md) 与 [u6-l3](u6-l3-bayesian-data-and-api.md) 统一梳理（遵循本项目一贯原则：文档与源码冲突时以源码为准）。

#### 4.3.4 代码实践

**实践目标**：体会「水印样本分数偏高、非水印样本分数偏低」这一判别逻辑。

**操作步骤**：

1. 阅读 Notebook cell-20，它对同一批 `wm_g_values` / `uwm_g_values` 分别调用 `mean_score` 与 `weighted_mean_score`。
2. 写出这 4 个分数变量的来源（函数名 + 输入）。
3. 思考：如果只给你一个阈值，你会怎么把分数二值化成「水印 / 非水印」？

**预期结果**：

- `wm_mean_scores = detector_mean.mean_score(wm_g_values, wm_mask)`
- `uwm_mean_scores = detector_mean.mean_score(uwm_g_values, uwm_mask)`
- `wm_weighted_mean_scores = detector_mean.weighted_mean_score(wm_g_values, wm_mask)`
- `uwm_weighted_mean_scores = detector_mean.weighted_mean_score(uwm_g_values, uwm_mask)`

判别方式：选定一个阈值 `τ`，分数 `≥ τ` 判为水印。阈值并非固定值，需根据你期望的假阳率（false positive rate）和样本 token 长度来经验性或理论性地确定（README 在 Weighted Mean 一节特别强调了这一点）。

> 说明：本实践为「源码阅读型」，无需运行；若想观察真实分数分布，可降低 Notebook cell-17 的样本规模后本地运行（标注「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：`mean_score` 和 `weighted_mean_score` 的输入输出形状分别是什么？

> **答案**：输入 `g_values` 形状 `[batch, seq_len, depth]`、`mask` 形状 `[batch, seq_len]`；输出分数形状 `[batch]`。

**练习 2**：为什么贝叶斯检测器需要「针对每个水印密钥单独训练」？

> **答案**：因为贝叶斯检测器学的是一个**特定水印配置**下 g 值的似然模型；换了 `keys`，g 值的分布就完全不同，旧检测器不再适用。而 Mean/Weighted Mean 不依赖训练，只需同样的 `keys` 重算 g 值即可直接打分。（详见 [u6-l1](u6-l1-bayesian-principle.md)。）

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全局数据流」小任务：

**任务**：对照 Notebook，列出端到端流程的 **5 个关键步骤**，每步写出对应的**函数或类名**（无需运行），并画出一张数据流图，标注每一步的张量大致形状与所属文件。

**参考作答框架**（请自己先填，再对照）：

| 步骤 | 做什么 | 关键 API | 文件 |
| --- | --- | --- | --- |
| 1 | 取水印配置 | `DEFAULT_WATERMARKING_CONFIG` | `synthid_mixin.py` |
| 2 | 加载带水印的模型 | `SynthIDGPT2LMHeadModel` / `SynthIDGemmaForCausalLM` | `synthid_mixin.py` |
| 3 | 生成（内部逐 token 施水印） | `model.generate(...)` → `SynthIDLogitsProcessor.watermarked_call` | `synthid_mixin.py` / `logits_processing.py` |
| 4 | 重算 g 值与掩码 | `compute_g_values`、`compute_eos_token_mask`、`compute_context_repetition_mask` | `logits_processing.py` |
| 5 | 打分输出 [0,1] | `mean_score` / `weighted_mean_score`，或 `BayesianDetector.score` | `detector_mean.py` / `detector_bayesian.py` |

**数据流图**（伪图）：

```text
prompt
  │  tokenizer
  ▼
input_ids [batch, inputs_len]
  │  SynthID 模型 .generate()  ── 内部 watermarked_call 逐 token 偏置 scores
  ▼
outputs [batch, inputs_len + output_len]   ── 取 [:, inputs_len:]
  │  compute_eos_token_mask / compute_context_repetition_mask / compute_g_values
  ▼
g_values [batch, seq, depth] + combined_mask [batch, seq]
  │  mean_score / weighted_mean_score / BayesianDetector.score
  ▼
scores [batch]  ∈ [0,1]   ── 阈值 τ 判定 ──▶ 水印 / 非水印
```

把这张图保留下来——后续每一讲，本质上都是在放大这张图里的某一个箭头。

## 6. 本讲小结

- SynthID Text 的端到端流程分为三段：**配置与生成（施加）**、**重算 g 值与掩码**、**打分输出**，分别对应 Notebook 的 Applying / 中间衔接 / Detecting。
- 水印是在 `model.generate(...)` 内部、由 `SynthIDLogitsProcessor.watermarked_call`（不是 `__call__`）逐 token 自动施加的；对调用方而言，只需把模型类换成 `SynthID` 前缀的子类。
- 检测**不信任**生成时状态，而是只用输出 token 序列 + 密钥**重新计算** `compute_g_values`，并用 `eos_token_mask`、`context_repetition_mask` 相乘得到 `combined_mask`，决定哪些位置参与统计。
- 打分有两条路线：免训练的 `mean_score` / `weighted_mean_score`，与需按密钥训练的 `BayesianDetector`（真实训练入口是 `train_best_detector` 类方法，而非 README 里的 `optimize_model`）。
- 两侧（PyTorch 施加 / JAX 检测）通过 **g 值**这一公共数据结构衔接——这正是 [u1-l3](u1-l3-repo-structure.md) 所说「框架即分水岭」的具体体现。

## 7. 下一步学习建议

本讲是「地图」，接下来该进入「地形」了。建议按以下顺序深入：

1. **先打底**：进入单元二，依次学 [u2-l1 水印配置](u2-l1-watermarking-config.md)、[u2-l2 哈希函数](u2-l2-hashing-function.md)、[u2-l3 g 值](u2-l3-g-values.md)——把本讲反复出现的「配置 + 哈希 + g 值」这条公共概念链彻底搞懂。
2. **再展开本讲的三条箭头**：
   - 想搞懂「生成」那一步的 5 步细节 → [u3 水印施加机制](u3-l1-processor-init-and-state.md)。
   - 想搞懂「掩码」怎么来的 → [u5-l1 检测掩码体系](u5-l1-detection-masks.md)。
   - 想搞懂贝叶斯打分到底怎么算、怎么训练 → [u6 贝叶斯检测器](u6-l1-bayesian-principle.md)。
3. **动手前的准备**：如果你想真正跑 Notebook，先回到 [u1-l2 运行与环境](u1-l2-setup-and-run.md) 把虚拟环境与可选依赖装好。
