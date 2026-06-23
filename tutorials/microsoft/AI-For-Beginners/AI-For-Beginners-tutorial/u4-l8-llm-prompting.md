# 大语言模型与提示编程

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 **GPT（Generative Pre-Trained Transformer）** 到底是什么，它和上一讲的 **BERT** 在结构、预训练任务、用法上的根本区别。
- 用「**自回归解码**」解释 GPT 是如何一个词一个词地把文本「写」出来的，并能用**条件概率**与**困惑度**衡量生成质量。
- 理解**缩放规律（scaling law）** 的直觉：为什么参数量、数据量越大，模型就越「能干」，乃至涌现出**少样本学习**能力。
- 掌握**提示工程（Prompt Engineering）** 的基本套路，并能写出 **zero-shot / one-shot / few-shot** 三种提示。
- 亲手运行课程提供的 `GPT-PyTorch.ipynb`，体验一个小规模 GPT 的生成，并设计 3 个少样本提示完成一个分类任务。

> 本讲是 NLP 单元的第 8 课，也是整个 NLP 链路的「现代收尾」：从词袋、词嵌入、RNN，到 Transformer/BERT，再到本讲的生成式大模型，把 NLP 的范式迁移讲完整。

## 2. 前置知识

本讲默认你已经学过以下讲义（否则建议先补）：

- **u4-l3 语言模型**：语言模型给文本打概率、预测下一个词；**自监督学习（self-supervised learning）**——「遮词自造标签」在海量无标注文本上训练。GPT 的预训练目标正是它的神经化、规模化版本。
- **u4-l5 生成式循环网络**：**自回归生成**（用上一步输出当下一步输入）、**温度采样** \(\tau\)（控制生成多样性）这两个概念本讲会直接复用，只是把 RNN 换成了 Transformer。
- **u4-l6 Transformer 与 BERT**：自注意力、多头注意力、Transformer 的编码器/解码器结构；**BERT** 用编码器做**掩码语言模型（MLM）**的预训练-微调范式。本讲的 GPT 是 Transformer 的另一半——**解码器**。

一个一句话的对照表，帮你建立坐标系：

| 维度 | BERT（上讲） | GPT（本讲） |
| -- | -- | -- |
| Transformer 部分 | 编码器栈（encoder） | 解码器栈（decoder） |
| 注意力方向 | **双向**（每个词能看到左右两侧） | **单向/因果**（只看左侧已生成词） |
| 预训练任务 | 掩码语言模型（挖空填词） | 自回归：预测**下一个**词 |
| 擅长 | **理解**类任务（分类、NER） | **生成**类任务（写文、对话） |
| 下游用法 | 几乎都要微调 | 可「不微调、只靠提示」 |

## 3. 本讲源码地图

本讲只涉及一个课程目录 `lessons/5-NLP/20-LangModels/`，它只有两个文件：

| 文件 | 作用 |
| -- | -- |
| `lessons/5-NLP/20-LangModels/README.md` | 课程讲义正文：讲清楚 GPT 是什么、文本生成与困惑度、GPT 家族（GPT-2/3/4）、提示工程，并给出 zero/few-shot 的结论。 |
| `lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb` | 可执行 Notebook：用 Hugging Face `transformers` 加载 `openai-gpt`（即 GPT-1），演示**文本生成、提示工程、采样策略**三大玩法。 |

> 注意：本课**没有** `lab/` 或 `assignment.md`，代码实践以「跑 Notebook + 自己设计提示」为主。Notebook 引用的是 `openai-gpt`（2018 年的初代 GPT，约 1.1 亿参数），**不是** README 里大书特书的 GPT-2/3/4。课程这样选是为了让学生能在本地跑起来；但正因为模型小，它的少样本能力很弱——这本身就是一个绝佳的教学观察点（见 4.3）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，恰好对应学习目标：**① 自回归大模型**（GPT 怎么生成）、**② 提示工程**（怎么用文字驱动它）、**③ 少样本任务**（规模为什么重要）。

---

### 4.1 自回归大模型：GPT 如何一个词一个词地写

#### 4.1.1 概念说明

在前面的课程里，我们解决任何任务都要**拿标注数据训练一个网络**。BERT 把流程改成「先在海量文本上自监督预训练，再用少量标注数据微调」。而 README 在开篇点出了本讲的核心立论：

> 大语言模型甚至**完全不需要任何领域训练**，就能解决很多任务。能这样做的模型家族叫做 **GPT：Generative Pre-Trained Transformer（生成式预训练 Transformer）**。

参见 [lessons/5-NLP/20-LangModels/README.md:L3](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L3)，这里给出了 GPT 全称与「无需下游训练即可解题」的关键性质。

GPT 的名字拆开看就是它的三件套：

- **Generative（生成式）**：它的工作方式是**生成**文本——给定开头，续写下去。README 引用 GPT-2 论文的核心思想：「**理解文本本质上就等于能够产出文本**」，因为能正确预测下一个词，意味着你已经「懂」了语言的规律与世界知识。参见 [README.md:L9](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L9)。
- **Pre-Trained（预训练）**：在覆盖人类知识的海量文本上**预训练**一遍（注意是「预」，即针对具体任务**之前**的大规模训练），模型因此变得博学。
- **Transformer**：用 Transformer 的**解码器**（decoder，见 u4-l6）作为骨架，靠**因果自注意力**保证生成时只能「看到」左侧已写出的词，不能偷看未来。

#### 4.1.2 核心流程

GPT 的生成是典型的**自回归（autoregressive）**过程：每一步用已有词预测下一个词，再把新词拼回去循环——这正是 u4-l5 讲过的自回归生成，只是骨干从 RNN 换成了 Transformer。

**① 预训练阶段（一次性、自监督）：**

在巨量无标注文本上，反复做一件事：给定前 \(i-1\) 个词，预测第 \(i\) 个词。用链式法则，一整句话的概率被分解成每一步条件概率的乘积（语言模型的标准因子化，呼应 u4-l3）：

\[
P(w_1, \dots, w_N) = \prod_{i=1}^{N} P(w_i \mid w_1, \dots, w_{i-1})
\]

其中每个条件概率由 Transformer 解码器给出。注意它和「词在语料里的出现频率」不同——GPT 给的是**条件概率**，即「在前面这些词已经出现的前提下，下一个词最可能是什么」：

\[
P(w_N \mid w_{n-1}, \dots, w_0)
\]

参见 [README.md:L13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L13)。

**② 推理/生成阶段（自回归循环）：**

```text
输入：提示 prompt = 已有词序列 w_1..w_k
循环直到达到长度上限：
    1. 模型算出下一词的概率分布 P(w_{k+1} | w_1..w_k)
    2. 按某种【采样策略】挑出 w_{k+1}
    3. 把 w_{k+1} 拼回输入：k ← k+1
输出：续写完成的整段文本
```

**③ 质量评估——困惑度（Perplexity）：**

生成模型好不好，可以用一个**不需要任务专用数据集**的内在指标来衡量，叫**困惑度（perplexity, PPL）**。它的直觉是：模型对「像真话」的句子应给出高概率、对「不像话」的句子（例如 README 举的反例 *Can it does what?*）应给出低概率。把模型喂真实句子时，我们希望概率高、困惑度低。数学上它是测试集概率的「归一化倒数」的 \(N\) 次方根：

\[
\mathrm{Perplexity}(W) = \sqrt[N]{\frac{1}{P(W_1, \dots, W_N)}}
\]

等价地用交叉熵表示：

\[
\mathrm{PPL}(W) = \exp\!\left(-\frac{1}{N}\sum_{i=1}^{N} \log P(w_i \mid w_{<i})\right)
\]

直观理解：困惑度 ≈ 「模型在每个位置平均在多少个词之间犹豫」。PPL 越低越好（理想值接近 1）。定义见 [README.md:L17-L20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L17-L20)。

**④ 采样策略（决定生成形态的旋钮）：**

第 ② 步里「挑出下一个词」有多种做法，这正是 Notebook 后半部分在演示的内容。

| 策略 | 关键参数 | 特点 |
| -- | -- | -- |
| 贪心 greedy | 默认 | 每步取概率最高的词；确定、但容易复读陷入循环 |
| 束搜 beam search | `num_beams`、`no_repeat_ngram_size` | 同时保留多条候选序列，最后选总分最高者；整体更连贯 |
| 随机采样 sampling | `do_sample=True`、`temperature` | 按概率分布抽样，结果多样、更自然 |
| Top-K | `top_k` | 只在概率最高的 K 个词里采样，过滤掉低概率「怪词」 |
| Top-P（核采样 nucleus） | `top_p` | 只在「累计概率 ≥ p」的最小词集里采样，自适应词数 |

其中**温度** \(\tau\) 通过下式调节分布的尖锐程度（与 u4-l5 完全一致）：

\[
P(i) = \frac{\exp(z_i / \tau)}{\sum_j \exp(z_j / \tau)}
\]

\(\tau \to 0\) 退化为贪心（最确定），\(\tau \to \infty\) 趋近均匀分布（最随机）。

#### 4.1.3 源码精读

Notebook 一上来就用 Hugging Face `transformers` 的 `pipeline` 把 GPT 装好、生成第一段文本：

```python
from transformers import pipeline

model_name = 'openai-gpt'           # 初代 GPT（GPT-1）

generator = pipeline('text-generation', model=model_name)

generator("Hello! I am a neural network, and I want to say that",
          max_length=100, num_return_sequences=5)
```

参见 [lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb:L58-L64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L58-L64)。这段做了三件事：

1. `pipeline('text-generation', ...)` 把「分词器 + 模型 + 解码」打包成一条流水线，调用者只需喂文本。
2. `model_name = 'openai-gpt'` 指定用 GPT-1；首次运行会从 Hugging Face 下载约 479MB 的权重。
3. 一次调用返回 `num_return_sequences=5` 条续写，每条最长 `max_length=100` 个 token。

这段代码运行后，Notebook 里记录了 5 条续写，例如：

> "Hello! I am a neural network, and I want to say that **i apologize for not coming to you yourself, for not helping you…**"

可以看到，即便 GPT-1 很小，它在「自由续写故事」这种纯生成任务上已经能产出语法连贯的英文长句——这正是「理解≈产出」立论的直接证据。

**采样策略的源码对照：**

- **贪心（默认）**：Notebook 在「Text Sampling Strategies」一节先用默认贪心策略续写一段悬疑小说开头，见 [GPT-PyTorch.ipynb:L201-L202](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L201-L202)（`generator(prompt,max_length=100,num_return_sequences=5)`）。
- **束搜**：加上 `num_beams=10, no_repeat_ngram_size=2`，模型会探索 10 条候选路径并禁止 2-gram 重复，生成的句子明显更通顺、更少胡话，见 [GPT-PyTorch.ipynb:L233-L234](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L233-L234)。
- **随机采样 + 温度**：`do_sample=True, temperature=0.8` 开启按概率抽样，结果每次不同、更有「人味」，见 [GPT-PyTorch.ipynb:L261-L262](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L261-L262)。
- **Top-K / Top-P**：Notebook 在文字里给出了说明，鼓励你自行把 `top_k`、`top_p` 加进去实验，见 [GPT-PyTorch.ipynb:L270-L271](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L270-L271)。

> 关于 GPT 家族与规模：README 列出 GPT-2（约 15 亿参数）、GPT-3（约 1750 亿参数）、GPT-4（课程标注 100T 级，且接受图文多模态输入），见 [README.md:L30-L32](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L30-L32)。Notebook 实际用的 `openai-gpt` 比这些都早、都小，是「能跑起来的教学版」。

#### 4.1.4 代码实践

**实践目标：** 在本地跑通 Notebook 的第一个 cell，亲眼看到一个 GPT 续写文本。

**操作步骤：**

1. 确认已按 u1-l3 激活 `ai4beg` 环境，并启动 Jupyter，选对内核。
2. 打开 `lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb`，运行第二个 cell（`from transformers import pipeline ...`，对应 [L58-L64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L58-L64)）。
3. 首次运行会下载约 479MB 模型权重，请耐心等待。

**需要观察的现象：**

- 下载完成后，返回 5 条以 "Hello! I am a neural network, and I want to say that" 开头的续写。
- 续写内容**每次运行不同**（默认其实接近贪心，但权重加载等仍有不确定性），但都语法连贯。

**预期结果：** 你会得到 5 段各不相同、读起来像小说独白的英文段落（Notebook 里已记录了示例输出可作对照）。

**待本地验证：** 若网络无法访问 Hugging Face，可把 `model_name` 换成 `'distilgpt2'`（更小、更易下载）再试；行为类似，只是风格不同。

#### 4.1.5 小练习与答案

**练习 1：** GPT 和 BERT 都基于 Transformer，为什么 GPT 只能「从左到右」生成，而 BERT 可以「双向」理解？

> **参考答案：** GPT 用的是 Transformer **解码器**，其自注意力带**因果掩码（causal mask）**，每个位置只能看到自己和左侧的词，不能偷看右侧——这正是「预测下一个词」所需的单向性。BERT 用的是**编码器**，自注意力无掩码，每个词能同时看到左右两侧，适合做「挖空填词」的理解任务。

**练习 2：** 困惑度为 1 意味着什么？为什么实际中几乎不可能达到？

> **参考答案：** PPL=1 意味着模型对测试集每个位置都「100% 确定」地猜对下一个词（概率为 1）。真实语言充满歧义与创造性，下一个词几乎从不是唯一确定的，所以 PPL 恒大于 1；模型越好，PPL 越接近某个下限，但不会到 1。

**练习 3：** 同一个 prompt，分别用贪心和 `temperature=1.2` 采样各生成 5 次，输出会有什么不同？

> **参考答案：** 贪心几乎每次都给出相同（或高度相似）的输出，且容易陷入重复；高温采样每次输出都不同，语言更跳脱多样，但也更可能出现不通顺或跑题的句子。

---

### 4.2 提示工程：用文字驱动模型

#### 4.2.1 概念说明

既然 GPT 是「给开头、续下去」，那么**我们给的开头**就成了控制它的唯一旋钮——这个开头就叫**提示（prompt）**。README 把这件事正式命名为**提示工程（Prompt Engineering）**：

> Prompt 是 GPT 的输入/查询，你借此向模型下达任务指令。为了得到想要的输出，你需要挑选最有效的措辞、格式、短语甚至符号。

参见 [README.md:L37-L39](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L37-L39)。

提示工程之所以成立，根因是 GPT 的预训练目标就是「续写」。所以你只要把提示**写成一个未完成的、模型自然会接着往下写的样子**，它就会顺着补全——这叫**补全式提示（completion-style prompting）**。这与现代对话模型那种「请帮我……」的**指令式提示**同源，只是早期 GPT 更偏向补全。

关键直觉：**你不改模型权重，只改输入文字，就能改变输出任务**。这在 BERT 时代是做不到的（BERT 必须微调才能换任务）。

#### 4.2.2 核心流程

设计一个好提示，通常按这几步迭代：

```text
1. 明确任务：翻译？分类？摘要？续写？
2. 选提示风格：
   - 补全式：把提示写成「半句话 / 待填表」，让模型自然补全；
   - 指令式：直接用祈使句下达命令（大模型上更有效）。
3. 选格式与符号：冒号、箭头 =>、换行、示例对齐等，都是为了
   让模型「看出」你想要的输出结构。
4. 运行、看输出、改提示、再运行——迭代收敛。
5. 必要时叠加采样参数（见 4.1）控制随机性。
```

#### 4.2.3 源码精读

Notebook「Prompt Engineering」一节（[GPT-PyTorch.ipynb:L72](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L72) 起）给了几个**补全式提示**的例子，最能说明问题的是同义词那个：

```python
generator("Synonyms of a word cat:", max_length=20, num_return_sequences=5)
```

参见 [GPT-PyTorch.ipynb:L98](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L98)。

这里用冒号 `:` 结尾，等于在暗示模型「后面要开始列举同义词了」。Notebook 记录的输出有：

> "Synonyms of a word cat: **cat of the woods, cat of the hills, cat of**…"

可以看到模型确实「领会」了要列举，只是 GPT-1 太小，列举得并不准确（甚至把句子带偏）。这恰好说明：**提示工程的方向是对的，但效果受模型能力上限制约**——这也是 4.3 要讲的规模问题的伏笔。

另一个例子是电影推荐：

```python
generator("People who liked the movie The Matrix also liked ", max_length=40, num_return_sequences=5)
```

参见 [GPT-PyTorch.ipynb:L168](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L168)。这句提示模仿了推荐网站的句式，模型于是顺着「推荐」的语境续写。这些都是「用文字塑造任务」的范例。

#### 4.2.4 代码实践

**实践目标：** 体会「换措辞、换格式，输出任务就变」。

**操作步骤：**

1. 在 Notebook 里复制 [L98](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L98) 的同义词 cell，新开一个 cell。
2. 把提示分别改成下面三种，各跑一次，对比输出：
   - `"Synonyms of a word cat:"`（原版，冒号列举）
   - `"Cat is an animal. Another word for cat is"`（补全半句）
   - `"List three synonyms of cat:\n1."`（用换行 + 编号给结构）
3. 记录哪种提示最容易让模型产出**真正的同义词列表**。

**需要观察的现象：** 第三种（带编号结构）通常更能引导模型按列表格式输出，说明**格式符号本身就是提示的一部分**。

**预期结果 / 待本地验证：** GPT-1 能力有限，三种都不一定给出准确同义词；但格式提示在「输出结构」上的差异应是肉眼可见的。

#### 4.2.5 小练习与答案

**练习 1：** 为什么把提示写成「半句话」对早期 GPT 特别有效？

> **参考答案：** 早期 GPT 的预训练目标就是「续写文本」。把提示写成未完成的半句话，正好让「完成任务」与「模型最擅长的续写」对齐，模型会自然地把半句话补全成答案。写成完整问句反而可能让它继续「编故事」。

**练习 2：** 提示工程和「微调模型」相比，最大的优势与最大的代价分别是什么？

> **参考答案：** 优势是**零代码、零训练数据、即改即用**，换任务只需换文字，迭代极快。代价是**效果受模型能力上限限制**，且对提示措辞敏感（换个词结果可能大变）；当任务复杂或要求高准确率时，仍需微调或更大的模型。

---

### 4.3 少样本任务：zero-shot、one-shot、few-shot 与规模

#### 4.3.1 概念说明

提示工程的进阶玩法是**在提示里塞几个示范**，让模型「照葫芦画瓢」。按塞的示范数量分三档：

- **Zero-shot（零样本）**：只给指令，不给示范。例如 `"Translate English to French: cat"`。
- **One-shot（单样本）**：给 1 个示范。
- **Few-shot（少样本）**：给几个示范。例如：

```text
Translate English to French:
cat => chat, dog => chien, student =>
```

模型看到前两个「英文 => 法文」的示范后，会推断出「把英文词翻成法文」这个任务，并试着补全 `student =>` 后面的法文词。

README 在结论里点明了这件事的意义：

> 新的通用预训练语言模型不仅建模了语言结构，还包含了海量自然语言知识，因此能在 **zero-shot 或 few-shot** 设定下有效解决不少 NLP 任务。

参见 [README.md:L49-L51](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L49-L51)。

**为什么 few-shot 这么重要？** 因为它意味着**一个模型、不微调、靠提示就能做无数种任务**——这是从「每个任务训一个模型」到「一个通用模型适配所有任务」的范式跃迁，也是今天大模型时代的基石。

#### 4.3.2 核心流程

少样本提示的标准结构：

```text
[可选：一句任务说明]
示范1：输入A => 输出A
示范2：输入B => 输出B
示范3：输入C => 输出C
真实查询：输入X =>          ← 模型在这里续写出 输出X
```

要点：

1. **示范要对齐**：所有示范（含查询）用**同样的格式与分隔符**（如 ` => `、` -> `、换行），让模型容易归纳出「输入→输出」的模式。
2. **示范要有代表性**：覆盖任务里不同的类别/情形，避免模型只学到单一套路。
3. **数量适度**：通常 3–8 个示范即可；过多会挤占上下文、稀释信号。

**规模与缩放规律（scaling law）——本模块的关键洞察：**

少样本能力**不是免费的**，它高度依赖模型规模。README 引用的两篇里程碑论文正好画出这条曲线：

- **GPT-2 论文**《Language Models are Unsupervised Multitask Learners》（[README.md:L9](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L9)）：提出「语言模型是无监督多任务学习者」的设想。
- **GPT-3 论文**《Language Models are Few-Shot Learners》（[README.md:L30](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L30)）：用 1750 亿参数实证——**模型够大，few-shot 才真正好用**。

缩放规律的直觉是：模型质量（用交叉熵/困惑度衡量）随**参数量、数据量、算力**按**幂律（power law）**提升，即

\[
\text{Loss} \;\propto\; (\text{参数量 / 数据 / 算力})^{-\alpha}
\]

而**少样本这种「涌现」能力**，往往要等规模越过某个阈值后才明显出现——这正是本课 Notebook 用小 GPT-1 做 few-shot 会「翻车」的根本原因。

#### 4.3.3 源码精读

Notebook 里有两个典型的少样本提示，它们恰好展示了「小模型的少样本局限」：

**① 情感分类（few-shot）：**

```python
generator("I love when you say this -> Positive\n"
          "I have myself -> Negative\n"
          "This is awful for you to say this ->",
          max_length=40, num_return_sequences=5)
```

参见 [GPT-PyTorch.ipynb:L122](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L122)。前两行是两个示范（用 ` -> Positive/Negative` 标注情感），第三行是查询，期望模型续写出 `Negative`（因为 "awful" 是负面）。

但 Notebook 记录的实际输出是：

> "This is awful for you to say this -> **positive this is so horrible - > positive that your brother is gay - >**"

模型**没给出干净的 `Negative`**，反而开始胡乱续写。这正是 GPT-1 太小、few-shot 能力不足的体现——同样的提示放到 GPT-3/4 上，几乎必然正确输出 `Negative`。

**② 机器翻译（few-shot）：**

```python
generator("Translate English to French: cat => chat, dog => chien, student => ",
          top_k=50, max_length=30, num_return_sequences=3)
```

参见 [GPT-PyTorch.ipynb:L144](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L144)。这里用 ` => ` 作分隔符，给了 cat、dog 两个示范，查询 `student =>`，正确答案应是 `étudiant`。Notebook 记录的输出同样跑偏（如 `student => student`、或续写成无关句子）。

> **教学小结：** 这两个 cell 是本课最值得细看的——它们用失败案例告诉你：**少样本的「形」很容易写出来（格式对齐就行），但「效」要靠规模撑起来**。把同样的提示交给 GPT-3 以上规模的模型，效果会天差地别。这正是 README 反复强调 GPT 家族（[L30-L32](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/README.md#L30-L32)）的原因。

Notebook 最后还顺带提到：你也可以**用自己的数据微调（fine-tune）** 模型，在保留语言能力的同时调整其风格或专项能力，见 [GPT-PyTorch.ipynb:L281](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L281)——这与 u4-l6 讲的 BERT 微调、u3-l3 讲的迁移学习一脉相承。

#### 4.3.4 代码实践（本讲主任务）

**实践目标：** 为一个**简单分类任务**设计 3 个少样本提示，体会格式对齐与示范代表性的作用。

任务设定：**把一句话判为「天气 / 体育 / 科技」三类之一**（类似 u4-l1 讲过的 AG News 主题分类的简化版）。

**操作步骤：**

1. 在 Notebook 里新开一个 cell，沿用 `generator` 对象。
2. 设计 **3 个不同的少样本提示**，每个都做同一个三类分类任务，但策略不同：
   - **提示 A（2 个示范，` => ` 分隔）：**
     ```python
     generator("Classify the topic:\n"
               "It will rain tomorrow => Weather\n"
               "The team won the match => Sports\n"
               "Apple released a new chip =>",
               max_length=30, num_return_sequences=3)
     ```
   - **提示 B（4 个示范，覆盖每个类别，含一个科技类示范）：**
     ```python
     generator("Topic classifier. Examples:\n"
               "Heavy snow expected => Weather\n"
               "Goal in the last minute => Sports\n"
               "New GPU launched => Tech\n"
               "Sunny and warm => Weather\n"
               "Quarterback injured => Sports\n"
               "AI model beats humans =>",
               max_length=40, num_return_sequences=3)
     ```
   - **提示 C（指令式 + 换行编号，强制结构）：**
     ```python
     generator("Read each sentence, output one of {Weather, Sports, Tech}.\n"
               "1. Thunderstorm tonight -> Weather\n"
               "2. Slam dunk winner -> Sports\n"
               "3. Self-driving car demo ->",
               do_sample=True, temperature=0.3, max_length=40)
     ```
3. 逐个运行，记录每个提示下模型是否在查询位置给出**干净的三类标签**，还是跑偏成续写故事。

**需要观察的现象：**

- 提示 B（示范更全、每类都覆盖）通常比 A 更可能产出正确类别。
- 提示 C 加了 `temperature=0.3`（低温度，更确定），输出更稳定。
- 但因为用的是 GPT-1，三种都未必稳定正确——把"AI model beats humans"判成 `Tech` 都可能失败。

**预期结果 / 待本地验证：** 你大概率会看到模型「领会了要做分类」、但执行不准。**这正是本课的核心观察**——请把这一现象写进你的实验记录，并思考：若换成 GPT-2/GPT-3 规模，结果会怎样。

> 进阶可选：若本地能联网且显存够，把 `model_name` 换成 `'gpt2'` 或 `'distilgpt2'` 重复上述 3 个提示，对比 GPT-1 与 GPT-2 在 few-shot 上的差距，直观感受「规模→能力」。

#### 4.3.5 小练习与答案

**练习 1：** 用一句话区分 zero-shot、one-shot、few-shot。

> **参考答案：** 按提示里给出的「示范数量」区分：给 0 个示范、只下指令的是 zero-shot；给 1 个示范的是 one-shot；给少数几个示范的是 few-shot。三者都不更新模型权重，只改输入。

**练习 2：** 本课 Notebook 的 few-shot 翻译、情感分类大多失败，而 README 却说大模型能做 few-shot。这两者矛盾吗？为什么？

> **参考答案：** 不矛盾。Notebook 用的是 `openai-gpt`（GPT-1，约 1.1 亿参数），规模远低于 few-shot 能力涌现的阈值；README 说的是 GPT-3（1750 亿）及以上。这恰恰印证了缩放规律：**few-shot 的格式谁都能写，但效果要靠规模撑起来**。

**练习 3：** 设计 few-shot 提示时，为什么强调「示范要对齐、用统一格式」？

> **参考答案：** 模型是靠在示范间**归纳模式**来完成任务的。统一格式（同样的分隔符、同样的字段顺序、同样的换行）让「输入→输出」的模式最清晰，模型最容易把归纳出的规则套用到查询上；格式混乱会让模型无所适从，转而退回「随便续写」。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小项目：**用 GPT 做一个「不微调」的 AG News 风格新闻四分类器，并系统比较不同提示策略。**

任务：给一句新闻标题，判为 World / Sports / Business / Sci/Tech 四类之一（呼应 u4-l1 的 AG News 数据）。

要求你完成：

1. **搭环境**：按 u1-l3 启动 Jupyter，运行 [GPT-PyTorch.ipynb:L58-L64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/20-LangModels/GPT-PyTorch.ipynb#L58-L64) 加载好 `generator`。
2. **写 3 种提示**，对同一批 5 条测试标题分类：
   - (a) **zero-shot**：只给一句指令 + 一个待分类标题；
   - (b) **few-shot**：给每类各 1 个示范，共 4 个示范；
   - (c) **few-shot + 低温度**：在 (b) 基础上加 `do_sample=True, temperature=0.2`。
3. **记录与对比**：建一张表，统计三种策略在 5 条标题上的「是否给出干净类别标签」「是否分类正确」。
4. **反思**：写一段话，用**缩放规律**解释为什么 GPT-1 的结果不理想，并说明你预期 GPT-2/GPT-3 会如何改善。

**交付物：** 一份含提示代码、输出截图、对比表格与反思的 Markdown 笔记（可放在你自己的练习目录，不要改动课程源码）。

**预期结论（待本地验证）：** (c) 通常最稳定；(a) 最易跑偏；整体准确率受限于 GPT-1 规模。这个练习帮你把「自回归生成 → 提示工程 → 少样本 + 规模」整条逻辑链亲手走一遍。

---

## 6. 本讲小结

- **GPT = Generative Pre-Trained Transformer**：用 Transformer **解码器**做**自回归**生成，预训练目标是「预测下一个词」，是 BERT（编码器、理解）的生成式对照面。
- **自回归生成** = 用条件概率 \(P(w_N\mid w_{<N})\) 一步步续写；质量用**困惑度**衡量，PPL 越低越好。
- **采样策略**（贪心 / 束搜 / 温度采样 / Top-K / Top-P）是控制生成「确定 vs 多样」的旋钮，本讲与 u4-l5 的温度概念打通。
- **提示工程**的核心是「**不改权重、只改输入文字**就能换任务」；早期 GPT 偏好**补全式**提示。
- **zero / one / few-shot** 通过在提示里塞示范让模型「照葫芦画瓢」；few-shot 的**格式对齐**至关重要。
- **缩放规律**：少样本能力依赖规模——GPT-1（Notebook 所用）太小、few-shot 常翻车，GPT-3（1750 亿）才真正确立 few-shot 范式。这是本课最关键的 takeaway。

## 7. 下一步学习建议

- **横向对照**：回到 u4-l6 重读 BERT 的预训练-微调，与本讲的「预训练 + 提示」做一张大表，彻底厘清「理解 vs 生成」「微调 vs 提示」两条路线。
- **深入生成**：重读 u4-l5 的 RNN 文本生成与温度采样，体会「自回归」这一思想从 RNN 到 Transformer 的传承。
- **追规模**：本课 README 引用的两篇论文值得找来读——《Language Models are Unsupervised Multitask Learners》（GPT-2）和《Language Models are Few-Shot Learners》（GPT-3），理解缩放规律与少样本涌现的实证。
- **课程衔接**：本讲是 NLP 单元（u4）的最后一课，下一站进入 **u5 其他 AI 技术与伦理**——遗传算法、深度强化学习、多智能体、多模态（CLIP），以及贯穿全程的 **AI 伦理（u5-l5）**：当你能用大模型「不训练就解任务」时，公平、偏见、可追溯等责任问题只会更尖锐，值得提前建立意识。
