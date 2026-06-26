# 仓库阅读地图：章节约定与代码汇总机制

> 本讲属于第 1 单元「项目概览与环境搭建」，承接 [u1-l1 项目定位与整体结构](u1-l1-project-overview.md) 和 [u1-l2 环境搭建与运行第一个模型](u1-l2-environment-setup.md)。
> u1-l1 告诉你「项目是什么、目录长什么样」，u1-l2 让你「跑出第一段生成」。
> 本讲要回答一个更实用的问题：**面对这么多章节目录和文件，我打开一个章节时到底该看哪个文件？后一章是怎么复用前几章代码的？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出每一章 `01_main-chapter-code` 目录里的固定约定：主 notebook、`previous_chapters.py`、summary `.py`、`exercise-solutions.ipynb` 各自扮演什么角色。
2. 理解 `previous_chapters.py` 这个「代码汇总器」是如何把前几章的关键类层层累积、被当前章节 notebook 复用的。
3. 区分两种代码复用模式：**完全自包含的 summary 脚本**（如 `gpt.py`）和**依赖 `previous_chapters.py` 的训练脚本**（如 `gpt_train.py`）。
4. 知道当本地找不到 `previous_chapters.py` 时，可以用官方 PyPI 包 `llms_from_scratch` 作为替代导入来源。

掌握这四点后，你阅读后续任何一章（ch05~ch07、附录）时都不会在文件堆里迷路。

## 2. 前置知识

- **模块（module）与 `import`**：Python 里一个 `.py` 文件就是一个模块。同目录下另一个文件用 `from previous_chapters import GPTModel` 就能拿到这个模块里定义的类。本讲大量依赖这一点。
- **类（class）与函数（def）**：只需知道 `class X(nn.Module)` 定义一个神经网络层、`def f(...)` 定义一个普通函数即可。
- **Jupyter Notebook（`.ipynb`）**：一种可以「一格一格」运行的代码文档，正文里的讲解、代码、图表交织在一起。本书正文的代码是在 notebook 里一行行演进写出来的。
- **从零（from scratch）**：u1-l1 已说明，本书不用 `transformers` 等高层库，所有组件都手写。正因为「都手写」，所以代码量大，才需要一套约定把它们组织起来——这正是本讲的主题。

如果你对上面这些还不够熟，建议先补一下 [u1-l1 项目定位与整体结构](u1-l1-project-overview.md)。

## 3. 本讲源码地图

本讲只读三类「说明性」文件，它们一起构成了仓库的阅读地图：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md) | 顶层目录的总目录表，标注每章的「主 notebook + summary 文件」。 |
| [ch04/01_main-chapter-code/README.md](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/README.md) | 第 4 章目录的「使用说明」，解释 notebook / previous_chapters / gpt.py 各自用途。 |
| [ch05/01_main-chapter-code/README.md](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/README.md) | 第 5 章目录的「使用说明」，并体现 previous_chapters.py 累积了更多内容。 |

为了说明「汇总机制」，本讲还会顺带精读这两个脚本与两个汇总模块（它们都真实存在）：

| 文件 | 作用 |
|------|------|
| [ch04/01_main-chapter-code/gpt.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py) | **完全自包含**的 summary 脚本，把第 2~4 章代码全部内联。 |
| [ch05/01_main-chapter-code/gpt_train.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py) | 第 5 章训练脚本，**通过 import 复用** `previous_chapters.py`。 |
| [ch04/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py) | 第 4 章用到的「前序章节汇总器」（含第 2、3 章关键类）。 |
| [ch05/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py) | 第 5 章用到的「前序章节汇总器」（含第 2、3、4 章关键类）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 章节目录约定**、**4.2 previous_chapters.py 汇总机制**、**4.3 notebook 与 summary 文件的关系（两种复用模式）**。

### 4.1 章节目录约定

#### 4.1.1 概念说明

一本书有 7 个主章（ch02~ch07）加若干附录（appendix-A/D/E）。如果把所有代码堆在一起会乱成一锅粥，所以作者给每一章设计了一套**固定目录结构**。你只要记住这套约定，打开任何一章都能立刻定位到想看的内容。

每一章的核心代码都放在 `chXX/01_main-chapter-code/` 这个目录里（注意这个固定名字 `01_main-chapter-code`，意思是「正文主体代码」）。这个目录里通常会反复出现这几类文件：

1. **主 notebook `chXX.ipynb`**：正文逐行演进写出来的代码，是最适合「跟着学」的入口。
2. **`previous_chapters.py`**：把「前几章需要复用的成品代码」汇总成一个模块，供当前章 notebook `import`。
3. **summary `.py` 脚本**（如 `gpt.py`、`gpt_train.py`）：把「本章学完后得到的结果代码」打包成可直接运行的脚本，方便你快速跑通或复制使用。
4. **`exercise-solutions.ipynb`**：本章课后练习的答案。
5. **`tests.py`**（部分章有）：对本章代码的单元测试。

> 额外提示：章节目录里 `01_`、`02_`、`03_` 这种数字前缀，表示同一主题下的「子专题」，序号越大越是补充/进阶内容（例如 `ch04/03_kv-cache` 是 KV 缓存的补充专题）。主章正文永远在 `01_main-chapter-code`。

#### 4.1.2 核心流程

阅读某一章时，按下面的优先级挑文件：

```text
想「跟着教材一步步学」        → 打开 chXX.ipynb（主 notebook）
想「直接跑出本章最终结果」    → 打开 summary .py（如 gpt_train.py）用 python 运行
想「只看课后题答案」          → 打开 exercise-solutions.ipynb
想「看额外补充专题」          → 进入同章的 02_/03_/... 目录
```

这套约定不是凭空说的，顶层 [README.md:64-73](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L64-L73) 的总目录表就用一张表把这些文件列了出来，并在 summary 文件后面标注了 `(summary)` 字样，这就是本节约定的「官方出处」。

#### 4.1.3 源码精读

先看顶层 README 的目录表，它列出了每一章的「快速入口」文件：

[README.md:64-73](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L64-L73) ——这张表把每章分成三列：正文主 notebook、带 `(summary)` 标记的汇总 `.py`/`.ipynb`、`exercise-solutions.ipynb`，以及最后一列 `./chXX` 指向整章补充材料。读这张表就能定位任何章节的入口。

以第 4 章为例，它自己的 [ch04/01_main-chapter-code/README.md:5-10](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/README.md#L5-L10) 用三句话讲清了目录里三类文件的角色：

- `ch04.ipynb` 包含「正文中出现的全部代码」；
- `previous_chapters.py` 是「包含上一章 `MultiHeadAttention` 模块的 Python 模块，本章 notebook 会 import 它」；
- `gpt.py` 是「一个独立（standalone）的 Python 脚本，内含截至目前实现的全部代码，包括本章编写的 GPT 模型」。

第 5 章的说明结构一模一样，只是内容更多一点。见 [ch05/01_main-chapter-code/README.md:5-13](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/README.md#L5-L13)：

- 它的 `previous_chapters.py` 同时包含 `MultiHeadAttention` **和** `GPTModel`（说明累积范围变大了，见 4.2）；
- 它有两个 summary 脚本：`gpt_train.py`（训练用）和 `gpt_generate.py`（加载 OpenAI 权重生成用）。

#### 4.1.4 代码实践

**实践目标**：用 README 的总目录表，亲手把第 2~7 章的入口文件梳理成一张清单。

**操作步骤**：

1. 打开 [README.md:64-73](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L64-L73)。
2. 为每一行（章）记录三件事：主 notebook 文件名、哪些是 `(summary)` 文件、有没有额外的 `gpt_download.py`/`ollama_evaluate.py` 之类辅助脚本。

**需要观察的现象**：

- 第 2 章的 summary 是 `dataloader.ipynb`（notebook 形式），而第 4、5 章的 summary 是 `.py`（脚本形式）——summary 文件**不一定是 `.py`**。
- 只有第 5 章出现了 `gpt_download.py`（下载权重），只有第 7 章出现了 `ollama_evaluate.py`（模型评估）。这说明辅助脚本是「按需出现」的，不是每章都有。

**预期结果**：你得到一张 6 行的表，每行都能对上「主 notebook + summary + 练习答案」三类。能准确说出 ch05 比 ch04 多了哪个 summary 文件（`gpt_generate.py`）。

#### 4.1.5 小练习与答案

**练习 1**：某读者只想「看到一段训练好的 GPT 生成连贯文本」，他应该优先打开第 5 章目录里的哪个文件？

> **参考答案**：`gpt_generate.py`。根据 [ch05/01_main-chapter-code/README.md:13](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/README.md#L13)，它的用途正是「加载并使用 OpenAI 预训练权重」。`gpt_train.py` 是从头训练用的，不适合「快速看到结果」。

**练习 2**：`01_main-chapter-code` 这个目录名里的 `01_` 前缀有什么含义？

> **参考答案**：它是「子专题序号」。`01_` 表示正文主体代码；同章下还会有 `02_`、`03_` 等更靠后的目录，通常是补充/进阶专题（如 `ch04/03_kv-cache`）。阅读正文时永远先找 `01_main-chapter-code`。

### 4.2 previous_chapters.py 汇总机制

#### 4.2.1 概念说明

本书是「自底向上」讲的：第 2 章写数据处理，第 3 章写注意力，第 4 章用前两章的成果组装 GPT，第 5 章再训练这个 GPT……每一章的 notebook 都会用到前面章节已经写好的成品代码。

问题是：第 5 章的 notebook 怎么拿到第 4 章的 `GPTModel`、第 3 章的 `MultiHeadAttention`、第 2 章的 `create_dataloader_v1`？

仓库的解决方案是：**给每一章配一个 `previous_chapters.py`**，它是一个「精选汇总器」——只把「后续章节会复用的成品类/函数」从前面几章摘出来，集中放进一个 `.py` 模块。当前章节的 notebook 只需 `from previous_chapters import GPTModel` 就能直接用，而不必把旧代码重新粘一遍。

关键特点：

- 它**只放成品、不放探索过程**。notebook 里那些「试错/演示」的中间代码不会进 `previous_chapters.py`，只有稳定的类和函数才会。
- 它是**逐章累积**的：ch05 的 `previous_chapters.py` ≈ ch04 的 `previous_chapters.py` + ch04 本章新增的 `GPTModel` 等成品。

#### 4.2.2 核心流程

汇总器的「累积」过程可以这样理解（伪代码）：

```text
# ch04/previous_chapters.py（第 4 章要复用的旧代码）
包含 ← 第2章成品：GPTDatasetV1, create_dataloader_v1
包含 ← 第3章成品：MultiHeadAttention

# ch04.ipynb（第 4 章正文）
from previous_chapters import MultiHeadAttention   # 复用旧代码
...在本章新写：LayerNorm, GELU, FeedForward, TransformerBlock, GPTModel...

# ch05/previous_chapters.py（第 5 章要复用的旧代码）
包含 ← 第2章成品：GPTDatasetV1, create_dataloader_v1
包含 ← 第3章成品：MultiHeadAttention
包含 ← 第4章成品：LayerNorm, GELU, FeedForward, TransformerBlock, GPTModel, generate_text_simple   ← 累积进来了！

# ch05.ipynb（第 5 章正文）
from previous_chapters import GPTModel             # 直接拿到拼好的模型
```

也就是说，**`previous_chapters.py` 像一个滚动更新的「组件库」**：每过一章就把它新产出的成品并进去，供更后面的章节直接 import。

#### 4.2.3 源码精读

第 4 章的汇总器只有 102 行，内容很少——因为它只需复用到第 3 章为止。确认一下它包含哪些成品：

[ch04/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py) ——

- [previous_chapters.py:12](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L12) 定义 `class GPTDatasetV1`（第 2 章的数据集）；
- [previous_chapters.py:34](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L34) 定义 `def create_dataloader_v1`（第 2 章的 DataLoader 构造器）；
- [previous_chapters.py:49](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L49) 定义 `class MultiHeadAttention`（第 3 章的多头注意力）。

第 5 章的汇总器膨胀到 279 行，因为第 4 章「组装 GPT」产出了大量成品，全都被累积进来：

[ch05/01_main-chapter-code/previous_chapters.py:6-8](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L6-L8) ——文件头注释明确写着「This file collects all the relevant code that we covered thus far throughout Chapters 2-4」，一句话点明了「汇总第 2~4 章」的定位。

它的内容（按行号）正好覆盖了「数据 + 注意力 + GPT 模型」全链路成品：

| 成品 | 来源章 | 行号 |
|------|--------|------|
| `class GPTDatasetV1` | 第 2 章 | [L20](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L20) |
| `def create_dataloader_v1` | 第 2 章 | [L42](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L42) |
| `class MultiHeadAttention` | 第 3 章 | [L60](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L60) |
| `class LayerNorm` | 第 4 章 | [L119](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L119) |
| `class GELU` | 第 4 章 | [L133](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L133) |
| `class FeedForward` | 第 4 章 | [L144](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L144) |
| `class TransformerBlock` | 第 4 章 | [L157](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L157) |
| `class GPTModel` | 第 4 章 | [L190](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L190) |
| `def generate_text_simple` | 第 4 章 | [L215](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L215) |

对比两个汇总器就能直观看到「累积」：ch04 版本止步于 `MultiHeadAttention`（L49），ch05 版本在此基础上又接上了第 4 章的 `LayerNorm`~`generate_text_simple` 一整套（L60 之后）。

那么 notebook 是怎么用它的？第 5 章 notebook 里就有这样的 import：

[ch05.ipynb:151-154](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L151-L154) ——`from previous_chapters import GPTModel`，紧跟着注释说明：如果本地没有 `previous_chapters.py`，可以改用 PyPI 包 `llms_from_scratch`（见 4.2.5）。同样的 import 在该 notebook 的 [第 220 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L220)（导入 `generate_text_simple`）和 [第 954 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L954)（导入 `create_dataloader_v1`）反复出现。

> 备用来源：仓库根目录的 [`pkg/`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/pkg/llms_from_scratch) 目录就是把这套汇总代码打包成了可发布的 PyPI 包 `llms_from_scratch`，里面有 `ch02.py`~`ch07.py` 对应各章。当本地 `previous_chapters.py` 缺失时，notebook 注释里给出的替代写法 `from llms_from_scratch.ch04 import generate_text_simple` 就是走这个包。这是 `previous_chapters.py` 机制的「云端版」。

#### 4.2.4 代码实践

**实践目标**：亲眼看一次「汇总器在累积」。

**操作步骤**：

1. 打开 [ch04/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py)（102 行），数一下里面定义了几个 `class`/`def`。
2. 再打开 [ch05/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py)（279 行），同样数一下。
3. 对比两者：ch05 版本比 ch04 版本多出了哪几个类？这些多出来的类来自哪一章？

**需要观察的现象**：

- ch04 版只有 3 个顶层定义（`GPTDatasetV1`、`create_dataloader_v1`、`MultiHeadAttention`）。
- ch05 版有 9 个顶层定义，比 ch04 多出了 `LayerNorm`、`GELU`、`FeedForward`、`TransformerBlock`、`GPTModel`、`generate_text_simple` 这 6 个，它们全是第 4 章「组装 GPT」时新增的成品。

**预期结果**：你能用一句话总结——「每章的 `previous_chapters.py` = 前一章的 `previous_chapters.py` + 前一章正文新产出的成品」。这正是后续章节能层层复用的根本原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 notebook 要从 `previous_chapters.py` 导入，而不是直接 `import` 上一章目录里的 `ch04.ipynb`？

> **参考答案**：因为 notebook 是「探索过程」的载体，里面有大量中间演示、试错代码，不适合被当模块复用；而 `previous_chapters.py` 是「精选的成品集合」，干净、稳定、可直接 import。把成品抽到一个 `.py` 模块，既让 notebook 保持可读，又让复用变得简单。

**练习 2**：如果本地完全没有 `previous_chapters.py` 这个文件，notebook 还能跑吗？

> **参考答案**：能。如 [ch05.ipynb:151-154](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L151-L154) 的注释所示，可以改用官方 PyPI 包 `llms_from_scratch`（对应仓库的 `pkg/` 目录），写成 `from llms_from_scratch.ch04 import ...`。这是同一个汇总机制的发布版。

### 4.3 notebook 与 summary 文件的关系（两种复用模式）

#### 4.3.1 概念说明

4.2 讲的是「notebook 复用旧代码」的模式（通过 import `previous_chapters`）。本节讲另一个容易混淆的点：**summary `.py` 脚本和 notebook 是什么关系？不同的 summary 脚本复用代码的方式一样吗？**

答案是：summary `.py` 是「本章学完后、把成果代码整理成一个可独立运行的脚本」，相当于本章的「速查版」。但**两个看起来很像的 summary 脚本，复用旧代码的方式可能完全不同**：

- **模式 A：完全自包含**。把所有需要的旧代码**原样内联**进同一个 `.py`，不依赖任何其他文件，复制走就能跑。代表是 [ch04 的 `gpt.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py)。
- **模式 B：依赖 `previous_chapters.py`**。脚本只写「本章新增的逻辑」，旧代码通过 `from previous_chapters import ...` 拿，运行时必须和 `previous_chapters.py` 放在同一目录。代表是 [ch05 的 `gpt_train.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py)。

理解这个区别非常重要：它会决定你「复制一个脚本到别处」时，需不需要顺带带上别的文件。

#### 4.3.2 核心流程

两种模式的依赖关系对比：

```text
模式 A（自包含，ch04/gpt.py）
┌─────────────────────────────┐
│  gpt.py                     │
│   └─ 内联：第2章数据         │   ← 全部写在本文件里
│   └─ 内联：第3章多头注意力   │
│   └─ 内联：第4章 GPTModel    │
│   └─ main()                 │
└─────────────────────────────┘
  （单独一个文件即可 python gpt.py 运行）

模式 B（依赖模块，ch05/gpt_train.py）
┌──────────────────┐      import       ┌───────────────────────┐
│  gpt_train.py    │  ───────────────▶ │  previous_chapters.py │
│   └─ 训练循环    │                    │   └─ GPTModel 等       │
│   └─ main()      │                    └───────────────────────┘
└──────────────────┘           （两个文件必须在同一目录）
```

注意：两种模式的「最终行为」是一致的（都能训练/生成），区别只在**代码是怎么组织的、运行时依赖哪些文件**。

#### 4.3.3 源码精读

先看模式 A 的 `ch04/gpt.py`。它的文件头开门见山说明自己是「汇总至今所有代码、可独立运行」：

[gpt.py:1-3](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L1-L3) ——「This file collects all the relevant code that we covered thus far throughout Chapters 2-4. This file can be run as a standalone script.」

它内部用注释把各章代码分段，全部内联，**没有任何 `from previous_chapters import`**：

- [gpt.py:11](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L11) `# Chapter 2` 段，内联了数据相关代码；
- [gpt.py:55](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L55) `class MultiHeadAttention`（第 3 章注意力，内联）；
- [gpt.py:185](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L185) `class GPTModel`（第 4 章模型，内联）；
- [gpt.py:236](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L236) `def main()`，[gpt.py:276](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L276) `if __name__ == "__main__":`（这就是 u1-l2 让你跑的那个入口）。

再看模式 B 的 `ch05/gpt_train.py`，开头先导标准库，然后**从本地模块 import** 三件复用成品：

[gpt_train.py:7-12](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L7-L12) ——

```python
# Import from local files
from previous_chapters import GPTModel, create_dataloader_v1, generate_text_simple
```

这一行就是模式 B 的标志：`GPTModel`、`create_dataloader_v1`、`generate_text_simple` 都来自同目录的 `previous_chapters.py`，本文件只负责写「训练循环」这种**第 5 章新增的逻辑**。它的入口也是标准的 `def main(...)`（[gpt_train.py:131](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L131)）配 `if __name__ == "__main__":`（[gpt_train.py:205](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L205)）。

> 为什么两章选了不同模式？这是一种务实的取舍：第 4 章末尾用 `gpt.py` 给读者一个「一键跑通完整模型」的自包含脚本，体验最顺畅；而第 5 章及以后代码量更大，全部内联会让脚本又长又难维护，所以改用「import 复用」来减少重复。你阅读时只要会判断「这个脚本属于哪种模式」即可，不必纠结哪种「更对」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对比 `ch04/gpt.py` 与 `ch05/gpt_train.py` 的 `import` 区，亲手画出「ch05 如何复用前序章节代码」的依赖图。这是本讲的核心实践任务。

**操作步骤**：

1. 打开 [ch04/01_main-chapter-code/gpt.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py)，只看开头 1~10 行的 import 区。确认它只 `import` 了标准库（`tiktoken`、`torch`、`torch.nn`、`Dataset/DataLoader`），**没有** `from previous_chapters import`。
2. 打开 [ch05/01_main-chapter-code/gpt_train.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py)，看 [第 7~12 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L7-L12) 的 import 区。记录下它从 `previous_chapters` 拿到的三个名字：`GPTModel`、`create_dataloader_v1`、`generate_text_simple`。
3. 追溯这三个名字的真实定义位置：它们都在 [ch05/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py) 里（见 4.2.3 的行号表）。
4. 把这条链画成依赖图（见下方）。

**需要观察的现象 / 依赖图**：

```text
gpt_train.py (第5章：训练循环)
  │
  ├── from previous_chapters import GPTModel
  │        └── previous_chapters.py:190  class GPTModel      ← 原始出处：第4章
  │
  ├── from previous_chapters import create_dataloader_v1
  │        └── previous_chapters.py:42   def create_dataloader_v1 ← 原始出处：第2章
  │
  └── from previous_chapters import generate_text_simple
           └── previous_chapters.py:215  def generate_text_simple ← 原始出处：第4章
```

**预期结果**：你能解释清楚——`gpt_train.py` 本身只写了第 5 章新增的训练/损失/采样逻辑，模型本体和数据处理都靠 `previous_chapters.py`（一个把第 2~4 章成品累积起来的模块）提供。而 `ch04/gpt.py` 走的是另一条路：把第 2~4 章代码全部内联，不依赖任何同目录模块。两个文件都是 summary 脚本，但复用旧代码的方式截然不同。

> 「待本地验证」部分：如果你本地想实跑 `gpt_train.py`，需要保证 `previous_chapters.py` 与它在同一目录（默认就在 `ch05/01_main-chapter-code/`，所以无需额外操作）；若把 `gpt_train.py` 单独复制到别处运行则会因找不到 `previous_chapters` 而报 `ModuleNotFoundError`，这正是模式 B 的特征，可在本地亲自验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `ch04/gpt.py` 单独复制到一个空文件夹里运行，能跑通吗？把 `ch05/gpt_train.py` 单独复制到空文件夹呢？

> **参考答案**：`gpt.py` 能跑（模式 A，完全自包含）；`gpt_train.py` 不能（模式 B，它在 [第 12 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L12) `from previous_chapters import ...`，离开同目录的 `previous_chapters.py` 就会报 `ModuleNotFoundError`）。判断依据就是开头有没有 `from previous_chapters import`。

**练习 2**：summary `.py` 脚本和主 notebook `chXX.ipynb` 的内容是什么关系？

> **参考答案**：summary `.py` 是 notebook 里「最终成果代码」的整理版（去掉演示、试错，保留能跑通主流程的代码），相当于本章的「速查/速跑版」。notebook 适合逐步学习，summary `.py` 适合快速运行或复用。两者最终实现的是同一套逻辑。

## 5. 综合实践

**任务**：为第 5 章目录写一份「文件导览」，把本讲三个模块串起来用一次。

要求你打开 [ch05/01_main-chapter-code/](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) 这个目录，对其中每个文件，用一两句话回答：

1. 它属于哪一类（主 notebook / previous_chapters 汇总器 / summary 脚本 / 练习答案 / 辅助工具）？
2. 它复用旧代码的方式（是内联自包含，还是 `import previous_chapters`，还是被别人 import）？
3. 一个初学者第一次学第 5 章，应该最先打开哪个？想直接跑训练又该打开哪个？

参考结论（可对照）：

- `ch05.ipynb`：主 notebook，正文入口，`import previous_chapters`（模式 B 的使用者）。**第一次学就开它**。
- `previous_chapters.py`：汇总器，被 notebook 和 `gpt_train.py` 共同 import，内部累积了第 2~4 章成品。
- `gpt_train.py`：summary 脚本（训练版），`import previous_chapters`（模式 B）。**想直接跑训练就开它**（注意它依赖同目录的 `previous_chapters.py`）。
- `gpt_generate.py`：summary 脚本（加载权重生成版）。
- `gpt_download.py`：辅助工具（下载 OpenAI GPT-2 权重），被 `gpt_generate.py` 等调用。
- `exercise-solutions.ipynb`：课后练习答案。

完成这个导览后，你就真正掌握了本仓库的「阅读地图」，后面读任何章节都不会迷路。

## 6. 本讲小结

- 每一章的核心代码都在 `chXX/01_main-chapter-code/` 下，固定包含主 notebook `chXX.ipynb`、汇总器 `previous_chapters.py`、summary `.py` 脚本、`exercise-solutions.ipynb`；顶层 [README](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L64-L73) 的目录表是这套约定的官方出处。
- `previous_chapters.py` 是「精选成品汇总器」，只放后续会复用的稳定类/函数，并随章节推进逐层累积（ch04 版 102 行止步于注意力，ch05 版 279 行追加了整套 GPT 模型）。
- notebook 通过 `from previous_chapters import ...` 复用旧代码；当本地缺这个文件时，可改用 PyPI 包 `llms_from_scratch`（对应仓库 `pkg/` 目录）。
- summary `.py` 脚本有两种复用模式：**完全自包含**（如 `ch04/gpt.py`，内联全部旧代码）与**依赖模块**（如 `ch05/gpt_train.py`，`import previous_chapters`），区别就在开头有没有 `from previous_chapters import`。
- 判断一个脚本属于哪种模式，决定你「复制它到别处运行」时需不需要带上 `previous_chapters.py`。

## 7. 下一步学习建议

- **进入第 2 单元（u2）**：本讲建立了「阅读地图」，下一讲 [u2-l1 分词与词表构建](u2-l1-tokenization-vocabulary.md) 将正式进入代码内容，从「文本如何变成数字」开始。你会用到本讲提到的 `ch02.ipynb` 主 notebook。
- **若想提前感受训练流程**：可以跳读 `ch05/gpt_train.py` 的 `main()`（[gpt_train.py:131](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L131)），先对「训练一个 GPT 大致分几步」有个直觉，再回头按章节顺序扎实学。
- **继续源码阅读**：建议浏览各章的 `previous_chapters.py`，把它们当成「本书所有核心组件的索引」——日后想快速回忆某个类（如 `MultiHeadAttention`、`GPTModel`）的实现，直接去对应章节的汇总器里查行号即可。
