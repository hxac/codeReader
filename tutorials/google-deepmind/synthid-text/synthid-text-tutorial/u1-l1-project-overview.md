# 讲义标题：项目概览与定位——SynthID Text 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应当能够：

1. 用一句话说清楚 SynthID Text 解决的是什么问题，以及它和大语言模型（LLM）的关系。
2. 把整个系统拆成「水印施加（watermarking）」和「水印检测（detection）」两个阶段，并说清二者的分工。
3. 知道这个仓库是「参考实现（reference implementation）」而非生产实现，理解它「能做什么、不能做什么」。
4. 看懂项目最关键的两个文件——`README.md` 和 `pyproject.toml`——并以此建立对后续源码的预期。

本讲不涉及复杂源码细节，目的是让你「先有全局地图，再进具体街道」。后续每一讲都会在各自的主题里深入源码。

## 2. 前置知识

本讲面向零基础读者，只需要以下几个概念即可：

- **大语言模型（LLM）**：一类能根据上文预测并生成文本的 AI 模型，例如 GPT 系列、Gemma。它们生成的文字和人类写的文字越来越难以区分。
- **文本生成（text generation）**：模型一次输出一个「token」（可以理解为字或词的片段），逐步拼成一整句话。
- **水印（watermark）**：借用纸张钞票里「水印」的概念——一种嵌入内容、肉眼不易察觉但可被专门方法检测到的隐藏标记。SynthID Text 把这个想法搬到文本生成里。
- **PyTorch 与 JAX**：两个主流深度学习框架。本项目中「施加水印」用 PyTorch，「检测水印」用 JAX/Flax，二者并存（后面的源码地图会再解释原因）。

不需要你已经读过论文或写过水印代码。本讲会从「这个仓库到底是个什么东西」开始讲起。

## 3. 本讲源码地图

本讲只聚焦两个「非代码」但最重要的文件，它们决定了项目定位：

| 文件 | 作用 | 本讲用来看什么 |
| --- | --- | --- |
| `README.md` | 项目说明书 | 项目能做什么、支持哪些模型、有哪些免责声明 |
| `pyproject.toml` | Python 打包与依赖配置 | 项目名、版本、依赖了哪些框架、有哪些可选依赖分组 |

这两个文件是「项目的门面」。在阅读任何 `.py` 源码之前，先读懂它们，能避免你一上来就迷失在细节里。

> 说明：本仓库真正的源码位于 `src/synthid_text/` 目录下，共 9 个文件。本讲只是「定位」，不会深入它们；后续讲义会逐个拆解。第 3 讲（`u1-l3`）会专门给出完整源码地图。

## 4. 核心概念与源码讲解

本讲包含三个最小模块：

- 4.1 项目定位与论文背景
- 4.2 水印施加 vs 检测
- 4.3 参考实现的边界与免责声明

### 4.1 项目定位与论文背景

#### 4.1.1 概念说明

随着大语言模型（LLM）能力增强，一个现实问题浮现：**如何辨认一段文字究竟是 AI 生成的，还是人写的？** 这关系到防范滥用（例如大规模生成虚假信息）。

SynthID Text 给出的思路不是「事后去猜这段文字像不像 AI 写的」（这类方法容易被攻击、也不够可靠），而是在**模型生成文字的那一刻，就把一个隐藏的统计信号悄悄嵌进去**。只要生成时嵌了信号，事后就可以用对应的检测器把信号「读」出来，从而判定「这段文字大概率来自带水印的模型」。

这套方法来自 DeepMind 发表在顶级期刊《Nature》上的论文。本仓库正是这篇论文的**参考实现（reference implementation）**——用代码把论文里的方法复现出来，供研究者和开发者学习、复现与二次实验。

#### 4.1.2 核心流程

从最宏观的视角，SynthID Text 把「识别 AI 生成文本」这件事拆成两步：

1. **生成时嵌入**：模型在生成每个 token 时，对候选词的概率分布做一点点「不易察觉的偏置」，使得最终文本在统计上带有可识别的痕迹。
2. **检测时读取**：拿到一段待判定的文本，重新计算那些隐藏痕迹（项目里叫「g 值」，后续讲义会专门讲），再用打分函数给出一个介于 0 和 1 之间的分数。

用伪流程表示：

```
原始文本提示 ──▶ [带水印的 LLM 生成] ──▶ 含水印的文本
                                          │
                                          ▼
                              [检测器读取 g 值并打分]
                                          │
                                          ▼
                            一个 [0,1] 的分数：越接近 1，越可能是「带水印」
```

注意：水印是**在生成阶段**注入的；如果一段文本本来就不是用这个带水印模型生成的，那它身上就没有这个信号，检测器会给出较低的分数。

#### 4.1.3 源码精读

打开 `README.md`，第一段就点明了项目身份：

> 该仓库提供了 SynthID Text 水印与检测能力的参考实现，用于配合发表在《Nature》上的研究论文。它不适用于生产环境。

参见 [README.md:L3-L8](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L3-L8)。这段话交代了三件事：

1. 它是「参考实现」（不是产品）。
2. 它对应一篇《Nature》论文。
3. 核心库发布在 PyPI 上，便于在 Notebook 示例里安装，并演示如何配合 **Gemma** 和 **GPT-2** 两个模型使用。

论文的完整出处写在 README 的「Citing this work」一节，关键信息如下（论文标题、期刊、卷期页码）：

> 标题：用于识别大语言模型输出的可扩展水印技术
> 期刊：Nature, 2024 年 10 月, 第 634 卷, 第 8035 期, 第 818–823 页

参见 [README.md:L284-L295](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L284-L295)。如果你打算读原论文来对照源码，这是它的引用信息；论文标题里的「Scalable（可扩展）」点明了方法的设计目标：即使生成量极大，也能稳定嵌入和检测。

#### 4.1.4 代码实践

这是一个**阅读型实践**（本讲不要求运行代码，重在建立认知）。

1. **实践目标**：能用自己的话复述「SynthID Text 解决什么问题」。
2. **操作步骤**：
   - 打开本仓库的 [README.md:L1-L8](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L1-L8)。
   - 同时打开论文引用 [README.md:L284-L295](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L284-L295)。
3. **需要观察的现象**：注意 README 是如何用一句话同时交代「是什么（参考实现）」「对应什么（Nature 论文）」「能配合谁用（Gemma / GPT-2）」三件事的。
4. **预期结果**：你能写出一句不依赖原文照搬的话，例如「SynthID Text 是 DeepMind 在 Nature 论文里提出的、用于识别 LLM 生成文本的水印方法的参考实现」。
5. 如果对论文背景仍有疑问，标注「待本地确认论文细节」即可，不影响后续学习。

#### 4.1.5 小练习与答案

**练习 1**：SynthID Text 的核心思路是「事后判断文本像不像 AI 写的」，对吗？为什么？

> **参考答案**：不对。它的核心思路是「在生成阶段就嵌入隐藏统计信号，检测阶段再读取」，而不是事后靠风格猜测。这使得判定更可靠、也更难被绕过。

**练习 2**：本仓库对应的是一篇什么级别的论文？在哪里能找到它的引用信息？

> **参考答案**：对应 DeepMind 发表在《Nature》（2024 年）上的论文，引用信息在 README 的「Citing this work」一节，见 [README.md:L284-L295](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L284-L295)。

---

### 4.2 水印施加 vs 检测

#### 4.2.1 概念说明

整个系统在工程上被清晰地分成**两个独立阶段**，理解它们的区别是读懂本项目结构的关键：

- **水印施加（watermarking / applying）**：发生在「生成」环节。借助一个特殊的「logits 处理器（logits processor）」，在模型输出每个 token 之前，悄悄调整候选词的得分，让最终文本带上水印。
- **水印检测（detection）**：发生在「判定」环节。拿到一段（可能是、也可能不是模型生成的）文本，重新计算水印痕迹并打分。

这两个阶段还有一个容易被忽略的工程特点：**它们用的深度学习框架不同**。施加侧用 **PyTorch**（因为要和 HuggingFace Transformers 的生成流程结合），检测侧用 **JAX / Flax**（贝叶斯检测器的训练与推理用 JAX 实现）。这也是为什么本项目的依赖里同时出现了 `torch` 和 `jax`。

检测侧还进一步分成两条路线：

- **Weighted Mean 打分器**：不需要训练，直接对 g 值做加权平均。
- **Bayesian（贝叶斯）检测器**：功能更强，但**必须先用带水印/不带水印的样本训练**才能使用。

#### 4.2.2 核心流程

把「施加」和「检测」拼起来，端到端流程如下：

```
┌──────────── 施加侧（PyTorch）────────────┐         ┌──────────── 检测侧（JAX）────────────┐
│                                          │         │                                       │
│  HuggingFace 模型 (Gemma / GPT-2)        │         │  待判定文本                            │
│        + SynthID Mixin (注入水印)         │         │        │                              │
│        │                                 │         │        ▼                              │
│        ▼                                 │ ─────▶ │  重算 g 值 + 构造掩码 (mask)            │
│  带水印的生成文本                          │         │        │                              │
│                                          │         │   ┌────┴────┐                         │
└──────────────────────────────────────────┘         │   ▼         ▼                         │
                                                     │ Weighted   Bayesian                   │
                                                     │ Mean       (需训练)                    │
                                                     │ (免训练)      │                         │
                                                     │   └────┬────┘                         │
                                                     │        ▼                              │
                                                     │   分数 score ∈ [0,1]                   │
                                                     └───────────────────────────────────────┘
```

输出的分数含义在 README 里有明确说明：分数介于 0 和 1 之间，**越接近 1，越倾向于判定「这段文本是用该水印配置生成的」**。

用概率记号表述打分输出：

\[
\text{score} \in [0, 1], \qquad \text{score 越大} \Rightarrow P(\text{带水印}\mid \text{文本}) \text{ 越高}
\]

注意这只是一个**标量分数**，是否最终判定为「带水印」，还需要使用者自己选定一个**阈值（threshold）**——这个阈值要根据可接受的「假阳率（false positive rate）」来定。

#### 4.2.3 源码精读

README 的「Installation and usage」一节，用一个两步列表把「施加 + 检测」讲得非常清楚：

参见 [README.md:L14-L27](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L14-L27)。这段内容包含两步：

1. **第一步（施加）**：用一个 mixin（混入类）扩展 HuggingFace 的 `GemmaForCausalLM` 和 `GPT2LMHeadModel`，在 PyTorch 中为生成内容启用水印。
2. **第二步（检测）**：检测水印——既可以用**无需训练**的 Weighted Mean 检测器，也可以用**更强大但需训练**的 Bayesian 检测器。

其中水印配置的数据结构由一个 `TypedDict` 描述，最关键的字段是 `keys`——一串唯一整数，`len(keys)` 决定了水印的「层数（深度）」：

参见 [README.md:L111-L117](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L111-L117)。这段代码说明：施加水印需要的输入是一个配置对象，里面包含 ngram 长度、密钥序列、采样表大小/种子、上下文历史大小、运行设备等字段。（这些字段的具体含义会在第 2 单元 `u2-l1` 详讲，本讲只需知道「水印由一个配置驱动」。）

README 还列出了支持的两类模型及其推荐运行硬件：

参见 [README.md:L33-L36](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L33-L36)。可见仓库官方演示的两类模型是：

- **Gemma**（v1.0 2B IT 与 7B IT），需要 GPU（16GB / 32GB）。
- **GPT-2**，任意运行环境均可，高内存 CPU 或任意 GPU 更快。

> 提示：虽然代码层面 mixin 还可以挂到别的模型上，但 README 明确「演示」并保证端到端跑通的就是 **Gemma** 和 **GPT-2** 这两类。

至于检测侧两种打分器的分工，README 在「Detecting a watermark」一节说明：仓库包含论文中描述的 Mean、Weighted Mean 与 Bayesian 三类打分函数；贝叶斯检测器必须**先针对每个唯一水印密钥训练**后才能使用。参见 [README.md:L176-L185](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L176-L185)。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**。

1. **实践目标**：能区分 README 里的「施加」代码和「检测」代码，并指出它们分别属于哪个框架。
2. **操作步骤**：
   - 阅读「Applying a watermark」代码示例 [README.md:L129-L172](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L129-L172)，注意它 `import torch`、用 `model.generate(...)`。
   - 阅读「Detecting a watermark」代码示例 [README.md:L222-L255](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L222-L255)，注意它 `import jax.numpy as jnp`、最后调用 `detector.score(...)`。
3. **需要观察的现象**：施加示例顶部导入的是 `torch` / `transformers`；检测示例顶部导入的是 `jax.numpy`。这印证了「施加用 PyTorch、检测用 JAX」。
4. **预期结果**：你能指出「施加」对应 `model.generate(...)` 调用，「检测」对应 `detector.score(g_values, combined_mask)` 调用，且二者分属不同框架。
5. 本步骤不要求实际运行模型；若想真正跑通，需要先完成第 2 讲（`u1-l2`）的环境搭建。

#### 4.2.5 小练习与答案

**练习 1**：施加水印和检测水印，分别用的是哪个深度学习框架？为什么本项目要同时装这两个框架？

> **参考答案**：施加用 PyTorch（为了接入 HuggingFace Transformers 的生成流程），检测用 JAX/Flax（贝叶斯检测器的训练与推理在 JAX 上实现）。因此 `pyproject.toml` 的依赖里同时有 `torch` 和 `jax`，安装本项目会把两个框架都装上。

**练习 2**：检测侧有哪两种打分路线？它们的最大区别是什么？

> **参考答案**：Weighted Mean 打分器（免训练）和 Bayesian 检测器（需训练）。最大区别在于是否需要先用带水印/不带水印的样本训练检测器；贝叶斯检测器还必须**针对每个唯一水印密钥**单独训练。

**练习 3**：README 官方演示（并保证端到端跑通）的是哪两类模型？

> **参考答案**：Gemma（2B IT / 7B IT）和 GPT-2，参见 [README.md:L33-L36](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L33-L36)。

---

### 4.3 参考实现的边界与免责声明

#### 4.3.1 概念说明

读懂一个项目，不仅要看它「能做什么」，更要看它**自我声明「不能做什么」**。SynthID Text 在 README 里反复强调：这是一个**参考实现（reference implementation）**，目的是**研究复现（research reproducibility）**，**不是生产实现**。

「参考实现」意味着：

- 它的代码以「清晰、可对照论文」为优先目标，而不是「高性能、高鲁棒、可直接上线」。
- 它的某些设计是「为了演示而简化」的，例如水印配置是**静态写死的**，不适合生产。
- 它不提供任何**密码学安全保证**。
- 如果你要在生产环境用 SynthID Text，应该转向 HuggingFace Transformers 里的**官方实现**。

这些声明不是「客套话」，而是直接影响你如何使用这个仓库：**用它学习、复现、做实验是合适的；把它直接塞进线上系统则不合适。**

#### 4.3.2 核心流程

README 用两段「NOTE（注意）」划出了边界：

```
NOTE 1（用途边界）：本实现仅供参考与研究复现；
        Gemma/Mistral 在不同实现间存在细微差异，检测结果与论文会有小幅波动；
        本仓库引入的子类不设计用于生产系统；
        生产环境请用 HuggingFace Transformers 的官方实现。

NOTE 2（安全边界）：计算 g 值所用的 accumulate_hash() 函数
        不提供任何密码学安全保证。
```

此外，作为一个**研究参考仓库**，你甚至会看到 README 里出现**指向不存在文件**的链接——这恰恰是「参考实现」的现实一面：文档未必和代码完全同步，遇到不一致时**以真实源码为准**。本讲末尾的实践会带你亲眼看一处这样的不一致。

#### 4.3.3 源码精读

**边界声明一：用途与生产化。** 见 [README.md:L38-L45](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L38-L45)。关键句是：

> 本实现仅供参考与研究复现之用……本仓库引入的子类不设计用于生产系统。生产就绪的实现请参见 HuggingFace Transformers 的官方 SynthID Text 实现。

**边界声明二：哈希非密码学安全。** 见 [README.md:L47-L49](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L47-L49)。关键句是：

> 计算本参考实现中 g 值所用的 `synthid_text.hashing_function.accumulate_hash()` 函数，不提供任何密码学安全保证。

**静态配置的局限。** README 在「Applying a watermark」里特别提醒：本库提供的 mixin 使用的是**静态水印配置**，因此不适合生产使用。见 [README.md:L122-L127](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L122-L127)。（「静态配置」的含义会在 `u2-l1` 详讲。）

**框架并存的事实，记录在打包配置里。** 打开 `pyproject.toml`，可以看到核心依赖同时列出了 PyTorch 与 JAX：

参见 [pyproject.toml:L15-L26](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L15-L26)。这段依赖列表印证了 4.2 节的结论——既有 `torch==2.4.0`、`transformers==4.43.3`（施加侧），也有 `flax`、`jax[cuda]`、`optax`（检测侧）。

项目自身的元信息也体现了「参考实现」的定位：

参见 [pyproject.toml:L5-L14](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L5-L14)。可以看到：

- `name = "synthid-text"`，`version = "0.2.1"`。
- `description = "SynthID Text: Identifying AI-generated text content"`（一句话定位：识别 AI 生成的文本内容）。
- `requires-python = ">=3.9"`。

而 `pyproject.toml` 还定义了三组**可选依赖（optional-dependencies）**，对应「本地跑 Notebook」和「跑测试」两种场景：

参见 [pyproject.toml:L54-L72](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L54-L72)。其中：

- `notebook` / `notebook-local`：跑 Notebook 示例所需的额外包（datasets、tensorflow、notebook 等）。
- `test`：跑测试套件所需的包（pytest、mock、absl-py）。

**一处真实的「文档与代码不一致」。** README 的链接表里把训练器指向 `./src/synthid_text/train_detector_bayesian.py`（见 [README.md:L330-L333](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L330-L333)），检测示例里也写了 `train_detector_bayesian.optimize_model(...)`（见 [README.md:L206-L213](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L206-L213)）。但**仓库里并不存在 `train_detector_bayesian.py` 这个文件**——实际的贝叶斯检测器与训练逻辑都在 `src/synthid_text/detector_bayesian.py` 中（训练入口是其中的 `BayesianDetector.train_best_detector` 等方法）。这正是「参考实现」的典型现象：**遇到 README 与源码冲突时，以源码为准。** 本手册后续所有讲义都遵循这一原则。

> 说明：本段结论基于对仓库实际文件的核对——`src/synthid_text/` 下只有 9 个文件，其中并没有 `train_detector_bayesian.py`。第 6 单元（`u6`）会专门讲贝叶斯检测器真正的训练入口。

#### 4.3.4 代码实践

这是一个**核对型实践**，用来亲身体验「参考实现的文档可能与代码不一致」。

1. **实践目标**：验证 README 提到的某个文件/接口，是否真的存在于源码中。
2. **操作步骤**：
   - 读 README 链接 [README.md:L330-L333](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L330-L333)，记下它声称训练器位于 `train_detector_bayesian.py`。
   - 在仓库根目录执行 `ls src/synthid_text/`，查看实际有哪些文件。
3. **需要观察的现象**：你会发现 `src/synthid_text/` 目录下**没有** `train_detector_bayesian.py`，但**有** `detector_bayesian.py`。
4. **预期结果**：确认「README 文档与真实源码存在出入」，并得出结论——学习本项目时要以 `detector_bayesian.py` 为准。
5. 如果你不在本地、无法执行 `ls`，可改用代码托管网页直接浏览 `src/synthid_text/` 目录，同样能得出结论；若仍无法确认，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：README 在哪两处 NOTE 里划出了「参考实现」的边界？分别讲了什么？

> **参考答案**：第一处 [README.md:L38-L45](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L38-L45) 讲「用途边界」——仅供参考与研究复现，子类不用于生产，生产请用 HF Transformers 官方实现；第二处 [README.md:L47-L49](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L47-L49) 讲「安全边界」——`accumulate_hash()` 不提供密码学安全保证。

**练习 2**：为什么说本项目「静态水印配置不适合生产」？这条提醒写在哪里？

> **参考答案**：因为 mixin 使用的是写死的静态配置，无法在生产中灵活、安全地轮换水印参数，故不适合生产。提醒见 [README.md:L122-L127](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L122-L127)。（静态配置的具体含义在 `u2-l1` 详讲。）

**练习 3**：README 引用的 `train_detector_bayesian.py` 是否存在于源码中？应以哪个文件为准？

> **参考答案**：不存在。`src/synthid_text/` 下并没有该文件，实际的贝叶斯检测器及其训练逻辑都在 `detector_bayesian.py` 中。学习时应以 `detector_bayesian.py` 为准。

## 5. 综合实践

本讲的综合实践，是把三个模块串起来，做一次「读懂项目门面」的小结任务。

**任务**：阅读 `README.md` 全文与 `pyproject.toml`，用自己的话完成下面三件事：

1. **能做什么 / 不能做什么**：写一段**不超过 100 字**的中文说明，回答「这个仓库能做什么、不能做什么」。要求同时覆盖「它是参考实现、不是生产实现」「用于识别 LLM 生成文本」这两点。
2. **支持的两类模型**：列出仓库官方演示并保证端到端跑通的两类模型（答案见 [README.md:L33-L36](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L33-L36)）。
3. **两大阶段与框架**：用一句话说明「施加」和「检测」分别用什么框架，并指出检测侧有哪两条打分路线。

**参考作答要点（先自己写，再对照）**：

- 能做：提供 SynthID Text（Nature 论文）文本水印「施加 + 检测」的参考实现，配合 Gemma / GPT-2 使用，可用于学习、复现、实验。不能做：不保证密码学安全、配置是静态的、子类不用于生产；生产环境应改用 HuggingFace Transformers 的官方实现。
- 两类模型：**Gemma**（2B IT / 7B IT）与 **GPT-2**。
- 两阶段：施加用 **PyTorch**（接入 HF Transformers 生成），检测用 **JAX/Flax**；检测侧有 **Weighted Mean（免训练）** 与 **Bayesian（需训练）** 两条路线。

完成本任务后，你就具备了阅读后续源码讲义所需的「全局认知」。

## 6. 本讲小结

- SynthID Text 是 DeepMind 在《Nature》论文中提出的、用于**识别 LLM 生成文本**的水印方法的**参考实现**，不是生产实现。
- 它的核心思路是「**生成时嵌入**隐藏统计信号，检测时再读取」，而非事后靠风格猜测。
- 系统分为**水印施加**与**水印检测**两个阶段：施加用 **PyTorch**（挂到 HuggingFace 模型上），检测用 **JAX/Flax**。
- 检测侧有两条路线：**Weighted Mean（免训练）** 和 **Bayesian（需训练，且每个水印密钥要单独训练）**。
- 仓库官方演示的两类模型是 **Gemma** 与 **GPT-2**。
- 重要的边界声明：仅供研究复现、`accumulate_hash()` 非密码学安全、配置为静态不适合生产；生产请用 HF Transformers 官方实现。遇到文档与源码不一致（如 `train_detector_bayesian.py` 并不存在）时，**以源码为准**。

## 7. 下一步学习建议

本讲只建立了「全局认知」，还没有真正跑过代码、也没看 `.py` 源码。建议按以下顺序继续：

1. **第 2 讲 `u1-l2`（运行与环境）**：学习如何安装依赖、运行 Notebook 与 `pytest` 测试套件，亲手把环境跑起来。
2. **第 3 讲 `u1-l3`（目录结构与源码地图）**：拿到 `src/synthid_text/` 下 9 个文件的完整地图，知道每个文件负责什么。
3. **第 4 讲 `u1-l4`（端到端流程总览）**：跟着 Notebook 走一遍「施加→生成→检测」的完整流程，建立数据流直觉。

之后再进入第 2 单元，学习贯穿全项目的核心数据结构——**g 值**与水印配置。本讲提到的「静态配置」「g 值」「accumulate_hash」等概念，都会在后续讲义中逐一展开。
