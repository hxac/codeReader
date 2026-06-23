# 多模态网络：CLIP 与 VQGAN

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 **CLIP**（Contrastive Language–Image Pre-training，对比式语言-图像预训练）到底把图像和文本对齐到了「同一个空间」是什么意思。
2. 用自己的话讲明白 **对比学习**（contrastive learning）的训练目标：为什么是「拉近正对、推远负对」。
3. 写出 CLIP 的 **零样本分类**（zero-shot classification）和 **文本图像检索**（text-based image search）两段最简调用，并能解释 `logits_per_image` 与 `logits_per_text` 的方向区别。
4. 理解 **VQGAN+CLIP** 如何把 CLIP 当作「损失函数」反过来指导图像生成，并能把它与第 III 单元的 GAN、风格迁移、第 IV 单元的 Transformer 串起来。

本讲是全课程偏现代、偏前沿的一讲，但它并不需要你重新学新东西——它把前面已经学过的「向量空间 / 余弦相似度」「迁移学习 / 预训练」「GAN 与风格迁移」「Transformer」四块积木重新拼成一个新的能力：**让机器同时「看懂图」和「读懂字」**。

---

## 2. 前置知识

本讲依赖前面两讲的结论，建议先复习：

- **u3-l5 生成对抗网络 GAN 与风格迁移**：CLIP 不是 GAN，但本讲后半段的 VQGAN 是 GAN 的变体；而「用预训练网络当损失、对输入做梯度下降」的思路，和神经风格迁移几乎一模一样——风格迁移优化的是**像素**，VQGAN+CLIP 优化的是**隐向量**。
- **u4-l6 Transformer 与 BERT**：VQGAN 用「自回归 Transformer」生成一串「视觉 token」，这与 GPT 的解码器生成文本 token 是同一套机制。
- 此外还会用到两个更早的概念：**词嵌入 / 向量空间**（u4-l1、u4-l2）和**余弦相似度**（u4-l1 TF-IDF 一讲里用过），它们是理解「对齐到同一空间」的基础。

下面用最通俗的方式补两个本讲要用的小概念。

### 2.1 余弦相似度

两个向量 \(a\)、\(b\) 的余弦相似度定义为：

\[
\cos(a,b)=\frac{a\cdot b}{\lVert a\rVert\,\lVert b\rVert}
\]

它衡量的是两个向量「方向是否一致」，取值范围是 \([-1,1]\)：越接近 1 表示越相似，越接近 -1 表示越相反，接近 0 表示基本无关。它的好处是**只看方向、不看长度**，所以即便两张图被编码成「长短不同」的向量，只要方向接近，相似度就高。

### 2.2 什么叫「同一个向量空间」

在 u4-l2 里，我们把**词**映射成一个向量，得到「语义空间」（king−man+woman≈queen）。CLIP 的关键，是把**图像**和**文本**这两类原本完全不同的东西，映射到**同一个**高维向量空间里。一旦它们在同一个空间里，就可以用余弦相似度直接比较「这张图」和「这句话」有多像——这就是本讲的核心直觉。

---

## 3. 本讲源码地图

本讲只涉及一个小目录 `lessons/X-Extras/X1-MultiModal/`，两个关键文件：

| 文件 | 作用 |
| --- | --- |
| `lessons/X-Extras/X1-MultiModal/README.md` | 多模态网络的概览讲义：讲清 CLIP 的对比预训练思想、零样本分类 / 图像检索、VQGAN+CLIP 生成、DALL·E。本讲几乎所有原理都能在这里找到原文。 |
| `lessons/X-Extras/X1-MultiModal/Clip.ipynb` | CLIP 的实操 Notebook：安装 OpenAI CLIP、加载 ViT-B/32 模型、在牛津宠物猫数据上做零样本分类与文本图像检索。本讲的代码实践全部基于它。 |

> 提示：`X-Extras` 是课程主目录之外的「附加内容」单元（见 u1-l2 的目录地图），多模态 CLIP 被放在这里，正因为它**同时依赖**第 III 单元的视觉（CV）与第 IV 单元的语言（NLP），是这两条线的汇合点。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**图文对齐 → 对比学习 → 零样本检索 → VQGAN+CLIP 生成**，前三者构成 CLIP 本身，第四者是把 CLIP 当损失去生成图像。

---

### 4.1 图文对齐：把图像和文本投影到同一空间

#### 4.1.1 概念说明

CLIP 的核心目标，用 README 的一句话就能概括：

> The main idea of CLIP is to be able to compare text prompts with an image and determine how well the image corresponds to the prompt.
> （CLIP 的主旨，是能够把「一段文字描述」和「一张图」拿来比较，判断这张图在多大程度上符合这段描述。）

为什么这件事很难？因为图像和文本是两种异构数据：图像是像素数组，文本是词序列，它们本来活在「两个不同的世界」。CLIP 的做法是训练**两个编码器**：

- **图像编码器**（Image Encoder）：把一张图压成一个固定长度的向量 \(I\)。课程用的 CLIP 版本里，图像编码器是一个 **Vision Transformer（ViT）**，即把图像切成 patch 后用 Transformer 编码（这正好把 u4-l6 的 Transformer 用在了视觉上）。
- **文本编码器**（Text Encoder）：把一句话压成一个等长的向量 \(T\)。它本质上是个 Transformer 编码器，和 BERT（u4-l6）同源。

关键设计是：**这两个编码器输出的向量维度相同**（被「对齐」到同一个空间），于是可以直接算 \( \cos(I, T) \) 来衡量「这张图和这句话有多配」。

#### 4.1.2 核心流程

CLIP 推理时只做三步（注意它**不需要训练**，因为已经预训练好了）：

```
1. preprocess(图像) → 图像张量；clip.tokenize(句子) → 文本 token 张量
2. model.encode_image / encode_text → 得到向量 I 和 T（或直接 model(image, text) 得到相似度 logits）
3. 对 logits 做 softmax → 概率；argmax → 最匹配的那一项
```

这里出现一个新词 **零样本（zero-shot）**：因为它已经在海量「图+标注」上预训练过，做新任务时**完全不需要再训练**，所以叫「零样本」。这和 u3-l3 的迁移学习不同——迁移学习还要微调分类头，CLIP 连微调都省了。

#### 4.1.3 源码精读

加载模型这一段，Notebook 里写得非常克制，但信息量很大：

```python
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load("ViT-B/32", device=device)
```

参见 [lessons/X-Extras/X1-MultiModal/Clip.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/Clip.ipynb)（「加载 CLIP 模型」cell）。两点要理解：

1. `clip.load("ViT-B/32", ...)`：`ViT-B/32` 指定了**图像编码器**用 Vision Transformer Base、把图切成 32×32 的 patch；返回的 `model` 同时含图像编码器和文本编码器，`preprocess` 是与该模型配套的图像预处理（缩放、裁剪、归一化）。
2. `model(image, text)` 一次前向同时算出两个方向的相似度，下面 4.3 会细讲。

对应 README 对这一能力的总述，见 [README.md:17](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L17)：「一旦模型预训练完成，给它一批图和一批文本提示，它会返回一个概率张量」。

#### 4.1.4 代码实践

**实践目标**：亲手把一张图和一句话编码成向量，确认它们维度一致、并算出余弦相似度。

**操作步骤**（在 Notebook 第 4 个 cell 之后新建一个 cell）：

```python
# 示例代码：观察图文向量「同空间」
import torch.nn.functional as F

img = preprocess(Image.open("oxcats/Maine_Coon_1.jpg")).unsqueeze(0).to(device)
txt = clip.tokenize(["a fluffy cat"]).to(device)

with torch.no_grad():
    I = model.encode_image(img)   # 图像向量
    T = model.encode_text(txt)    # 文本向量

print("图像向量 shape:", I.shape)   # 期望 [1, 512]
print("文本向量 shape:", T.shape)   # 期望 [1, 512]，两者必须同维才能比较
sim = F.cosine_similarity(I, T).item()
print("余弦相似度:", round(sim, 4))
```

**需要观察的现象**：两个向量的第二维相同（ViT-B/32 是 512），这就是「同一空间」的体现；相似度是一个 \([-1,1]\) 之间的数。

**预期结果**：因为图确实是猫、句子也描述猫，相似度会是一个明显的正数。**待本地验证**：不同图片 / 句子的具体数值。

#### 4.1.5 小练习与答案

**练习 1**：如果把文本换成 `"a sports car"`，余弦相似度相比 `"a fluffy cat"` 应该变大还是变小？为什么？
**答**：应变小。因为图里是猫，与「跑车」语义不相关，方向更不一致，余弦相似度更低。

**练习 2**：`encode_image` 和 `encode_text` 输出的向量维度**必须相同**，这是 CLIP 能工作的前提。请说出这个「同维」在数学上的作用。
**答**：只有维度相同，才能计算点积 \(I\cdot T\) 和余弦相似度 \(\cos(I,T)\)，即才能在同一个空间里度量「图文距离」。

---

### 4.2 对比学习：拉近正对、推远负对

#### 4.2.1 概念说明

CLIP 是**怎么**学会把图文对齐到同一空间的？答案是 **对比学习（contrastive learning）**。它的思路朴素到一句话：

> 在一个批次里，让「本该配对」的图文（正对）相似度尽量高，让「本不该配对」的图文（负对）相似度尽量低。

这就是「对比」二字的含义——不告诉模型「这张图是什么类」，只告诉它「这张图配这句话、不配那 N-1 句话」。

#### 4.2.2 核心流程

设一个批次里有 \(N\) 对（图，文），编码后得到图像向量 \(I_1,\dots,I_N\) 和文本向量 \(T_1,\dots,T_N\)。先算一个 \(N\times N\) 的**相似度矩阵** \(S\)：

\[
S_{ij}=\cos(I_i,\,T_j)
\]

其中**对角线** \(S_{ii}\) 是正确配对的相似度（正对），**非对角线** \(S_{ij}(i\neq j)\) 是错误配对的相似度（负对）。CLIP 的损失（业界称 InfoNCE）相当于在这个矩阵的**每一行、每一列**各做一次「把对角线那个挑出来」的交叉熵：

\[
\mathcal{L}_{\text{image}\to\text{text}}=-\log\frac{\exp(S_{ii}/\tau)}{\sum_{j}\exp(S_{ij}/\tau)},\qquad
\mathcal{L}=\tfrac{1}{2}\bigl(\mathcal{L}_{\text{image}\to\text{text}}+\mathcal{L}_{\text{text}\to\text{image}}\bigr)
\]

这里 \(\tau\) 是温度系数（控制概率分布的尖锐程度）。直观上：最大化分子（正对相似度）、最小化分母（所有负对相似度），正好对应「拉近正对、推远负对」。注意一个精妙之处——**负样本不用专门造**，同一个 batch 里的其他 \(N-1\) 对天然就是负样本，batch 越大，负样本越多，CLIP 训练时用的 batch 高达数万。

#### 4.2.3 源码精读

README 把这段损失思想讲得很清楚（虽然没用公式）：

> For each batch, we take N pairs of (image, text) … Those representations are then matched together. The loss function is defined to maximize the cosine similarity between vectors corresponding to one pair (e.g. I_i and T_i), and minimize cosine similarity between all other pairs. That is the reason this approach is called **contrastive**.

见 [README.md:13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L13)：这一行正是 CLIP 对比损失的「人话版」，把上面那个公式翻译成了「最大化正对余弦相似度、最小化其余对相似度」。CLIP 的训练数据则是「从互联网抓取的图及其配文（caption）」，见同一段开头。

#### 4.2.4 代码实践

**实践目标**：在推理端复现「相似度矩阵」，亲眼看到对角线（正对）大、非对角线（负对）小。

**操作步骤**（新建 cell，示例代码）：

```python
# 示例代码：手工构造一个 3 图 × 3 文 的相似度矩阵
import os, torch.nn.functional as F

names = sorted(os.listdir("oxcats"))[:3]
imgs = torch.cat([preprocess(Image.open(os.path.join("oxcats",n))).unsqueeze(0) for n in names]).to(device)
texts = ["a cat","a dog","a car"]                                   # 注意：第 0 句才和图真正配对
toks = clip.tokenize(texts).to(device)
with torch.no_grad():
    fi = model.encode_image(imgs); ft = model.encode_text(toks)
    fi = F.normalize(fi, dim=-1); ft = F.normalize(ft, dim=-1)      # 归一化后点积即余弦相似度
    S = (fi @ ft.T)                                                 # 3×3 矩阵
print(np.array(S.cpu()))
```

**需要观察的现象**：理想情况下，矩阵的对角线（每个图与自己应配的描述）数值偏高；但因为这里 3 句话只有一句是「猫」，实际你会看到「图 vs a cat」那一列整体偏高。重点是把 `fi @ ft.T` 这个 \(N\times N\) 矩阵和 4.2.2 的公式 \(S_{ij}\) 对应起来。

**预期结果**：打印出一个 \(3\times3\) 的小数矩阵。**待本地验证**：具体数值取决于下载到的图片。

#### 4.2.5 小练习与答案

**练习 1**：为什么对比学习「不需要人工标注类别」？
**答**：因为监督信号来自「这张图原本配哪句话」（即抓取时的配对关系），正负样本由 batch 内的其他对自动充当，不需要额外打「猫/狗」标签。

**练习 2**：把 batch 从 16 扩大到 4096，对 CLIP 训练有什么好处？
**答**：batch 越大，每个正对能见到的负对越多，对比信号越强，学到的表示越好——这正是 CLIP 用超大 batch 的原因。

---

### 4.3 零样本检索：分类与图像搜索是一枚硬币的两面

#### 4.3.1 概念说明

预训练好的 CLIP 能做两件互为镜像的事，README 各用一段讲：

- **零样本图像分类**：给 1 张图 + 多句话（候选类别），问「这张图最像哪句话」。例如候选 `"a picture of a cat" / "a picture of a dog" / "a picture of a human"`，取概率最大的那项作为分类结果。见 [README.md:21](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L21)。
- **文本图像检索（智能图像搜索）**：反过来，给多张图 + 1 句话，问「这句话最像哪张图」。见 [README.md:27-L29](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L27-L29)。

两者本质都是「在一个集合上做 softmax 取 argmax」，区别只在于**哪个是 1、哪个是 N**：分类是 1 张图选 N 个类，检索是 1 句话选 N 张图。

#### 4.3.2 核心流程

Notebook 里 `model(image, text)` 一次返回**两个**方向的 logits：

```
logits_per_image : 形状 [num_images, num_texts]  → 逐图看，它在 N 句话上的分布（用于分类）
logits_per_text  : 形状 [num_texts, num_images]  → 逐句看，它在 N 张图上的分布（用于检索）
```

- 分类：`logits_per_image.softmax(dim=-1)` → 每张图在各类上的概率 → `argmax` 得类别。
- 检索：`logits_per_text.softmax(dim=-1)` → 每句话在各图上的概率 → `argmax` 得最匹配的图。

**注意 softmax 的方向**：分类沿「文本/类别」维 softmax，检索沿「图像」维 softmax。搞反了维度，结果就会错乱——这是本节最容易踩的坑。

#### 4.3.3 源码精读

**零样本分类** cell（拿一张缅因猫图，在「企鹅 / 熊 / 猫」三选一）：

```python
image = preprocess(Image.open("oxcats/Maine_Coon_1.jpg")).unsqueeze(0).to(device)
text = clip.tokenize(["a penguin", "a bear", "a cat"]).to(device)

with torch.no_grad():
    logits_per_image, logits_per_text = model(image, text)
    probs = logits_per_image.softmax(dim=-1).cpu().numpy()

print("Label probs:", probs)   # 输出: [[0. 0. 1.]]
```

参见 [Clip.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/Clip.ipynb)（「Zero-Shot Image Classification」cell）。`[0. 0. 1.]` 表示模型 100% 押第 3 个候选「a cat」——注意这里**完全没有任何训练**，全靠预训练知识。

**文本图像检索** cell（一堆猫图 + 「a very fat gray cat」，挑最像的那张）：

```python
cats_img = [ Image.open(os.path.join("oxcats",x)) for x in os.listdir("oxcats") ] 
cats = torch.cat([ preprocess(i).unsqueeze(0) for i in cats_img ]).to(device)
text = clip.tokenize(["a very fat gray cat"]).to(device)
with torch.no_grad():
    logits_per_image, logits_per_text = model(cats, text)
    res = logits_per_text.softmax(dim=-1).argmax().cpu().numpy()   # ← 注意用的是 logits_per_text
print("Img Index:", res)
plt.imshow(cats_img[res])
```

参见 [Clip.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/Clip.ipynb)（「Intelligent Image Search」cell）。关键点：这里查询是**文本**，要在**图集**里挑，所以用 `logits_per_text` 并沿 `dim=-1`（图像维）softmax 再 argmax，正好和分类那段反过来。

#### 4.3.4 代码实践

**实践目标**：复现并扩展两段调用，并**故意制造一个匹配错误的案例**来分析原因。

**操作步骤**：

1. 跑通原 Notebook 的两个 cell，确认分类得到 `[0. 0. 1.]`、检索得到一个图片索引。
2. **分类练习**：把候选文本改成更细粒度、更容易混淆的一组，例如：
   ```python
   text = clip.tokenize(["a photo of a Maine Coon cat",
                         "a photo of a Persian cat",
                         "a photo of a Siamese cat"]).to(device)
   ```
   观察 CLIP 是否还能区分猫的品种（多半会变差——CLIP 对细粒度子类不敏感）。
3. **检索练习**：把查询换成 `"a sleeping cat"` 或 `"a cat looking at camera"`，看挑出的图是否真的符合描述。
4. **错误分析**：记录哪些图被错配，结合 README 的零样本思想分析——是因为描述太抽象，还是图里特征不明显？

**需要观察的现象**：候选越「通用」（cat/dog/human），分类越准；候选越「细粒度」（具体品种、姿态），越容易出错。

**预期结果**：细粒度分类的概率分布会更「平」（不再是非 0 即 1），这说明模型不确定。**待本地验证**：取决于下载到的猫图实际内容。

#### 4.3.5 小练习与答案

**练习 1**：为什么分类用 `logits_per_image`、检索用 `logits_per_text`？能不能互换？
**答**：不能简单互换。分类是「1 张图 → N 个类别」，要在类别维做 softmax，对应 `logits_per_image` 的行；检索是「1 句话 → N 张图」，要在图像维做 softmax，对应 `logits_per_text` 的行。它们是同一个相似度矩阵的转置关系，softmax 的轴必须跟着「被选择的集合」走。

**练习 2**：CLIP 的「零样本分类」和 u3-l3 的「迁移学习微调」相比，省掉了哪一步？
**答**：省掉了**下游训练**（连分类头都不用训）。迁移学习还要在小数据集上微调，CLIP 直接用文本候选就能分类，代价是它更依赖「候选文本写得好不好」。

---

### 4.4 VQGAN+CLIP：用对比信号反向指导图像生成

#### 4.4.1 概念说明

CLIP 还能反过来**生成**图像。思路是：CLIP 已经会判断「这张图符不符合这句话」，那我们就让一张「初始随机图」不断变化，直到 CLIP 觉得它「最符合这句话」为止。这需要一个能生成图像的 **生成器**，课程选的是 **VQGAN**（Vector-Quantized GAN，向量量化 GAN）。

VQGAN 与第 III 单元普通 GAN（u3-l5）的区别，README 列了两点，见 [README.md:39-L41](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L39-L41)：

1. 用**自回归 Transformer**（u4-l6）生成一串「富含上下文的视觉部件」来拼出图像，而这些视觉部件本身由 **CNN**（u3-l2）学出来——也就是把 GAN 的生成器和 Transformer 两种技术嫁接。
2. 用**子图判别器**（sub-image discriminator）判断「图像的局部」真不真，而不是传统 GAN 的「整张图一刀切」。

#### 4.4.2 核心流程

但 VQGAN 单独有个毛病：它从任意隐向量生成的图可能**不连贯**。解决办法就是请 CLIP 来「导航」。整个生成循环见 README：

> To generate an image corresponding to a text prompt, we start with some random encoding vector that is passed through VQGAN to produce an image. Then CLIP is used to produce a loss function that shows how well the image corresponds to the text prompt. The goal then is to minimize this loss, using back propagation to adjust the input vector parameters.

翻译成流程：

```
随机隐向量 z  ──VQGAN──▶  候选图像  ──CLIP──▶  与文本提示的相似度（= -损失）
                                  ▲                          │
                                  └──── 反向传播调整 z ◀──────┘  （迭代）
```

这里有个非常漂亮的对照，建议和 u3-l5 风格迁移一起记：**两者都冻结网络、只优化输入**，差别只在「用什么网络当损失、优化什么输入」：

| | 风格迁移（u3-l5） | VQGAN+CLIP |
| --- | --- | --- |
| 当损失的网络 | 预训练 VGG | 预训练 CLIP |
| 被优化的对象 | 图像**像素** | VQGAN 的**隐向量** z |
| 目标 | 内容损失 + 风格损失 | 图文相似度（CLIP 损失） |

也就是说，CLIP 在这里扮演的角色，正是风格迁移里 VGG 的角色——一个**可微的「审美 / 语义判官」**。

> 课程还顺带提了 **DALL·E**（见 [README.md:60-L64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L60-L64)）：它是 GPT-3 的变体，把文本和图像都当作「同一串 token」来生成图，与 CLIP「对齐到同一空间」是不同的路线。了解即可。

#### 4.4.3 源码精读

VQGAN+CLIP 的生成原理见 [README.md:49](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L49)（上面已引用），README 同时给出了一个成熟实现库 **Pixray**，并展示了从文本提示生成的人像画（见 [README.md:51-L53](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L51-L53)）。本模块以**源码阅读**为主，因为完整跑 VQGAN+CLIP 需要较重资源，课程没有提供现成 Notebook。

#### 4.4.4 代码实践

**实践目标**：用「源码阅读 + 最小生成」理解 VQGAN+CLIP 的闭环；条件允许时用 Pixray 真正生成一张图。

**操作步骤**（源码阅读型，不依赖 GPU）：

1. 重读 [README.md:35-L51](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/X-Extras/X1-MultiModal/README.md#L35-L51)，画出「z → VQGAN → 图 → CLIP 损失 → 反传回 z」的循环图。
2. 对照上面的对照表，指出它和 u3-l5 神经风格迁移的「两个相同、一个不同」。
3.（可选，需联网与较好算力）安装 Pixray：`pip install pixray`，用一句提示生成图，例如在 Python 里：
   ```python
   # 示例代码：Pixray 最简调用（需安装 pixray，资源消耗较大）
   import pixray
   pixray.reset_settings()
   pixray.add_settings(prompts="a closeup watercolor portrait of a cat", iterations=100, size=(256,256))
   settings = pixray.apply_settings()
   pixray.do_init(settings)
   pixray.do_run(settings)
   ```
   **待本地验证**：是否能安装成功与生成时长，取决于本地 / 云端环境。

**需要观察的现象**：随迭代进行，生成图会从纯噪声逐步「贴合」文本提示的语义。

**预期结果**：若成功运行，得到一张与提示风格/内容大致相符的图；若环境不支持，完成第 1、2 步的阅读与画图即可。

#### 4.4.5 小练习与答案

**练习 1**：VQGAN+CLIP 里，**梯度更新的是谁**？是 VQGAN 的权重，还是隐向量 z？
**答**：更新的是**隐向量 z**（输入），VQGAN 和 CLIP 的权重都冻结不动。这与风格迁移优化像素、而非 VGG 权重是同一个道理。

**练习 2**：为什么 VQGAN 生成的图需要 CLIP「导航」？
**答**：因为 VQGAN 从任意 z 生成的图可能不连贯、不像任何东西；CLIP 提供了「这张图像不像这句话」的可微损失，能把 z 一步步拉向「符合文本提示」的连贯图像。

---

## 5. 综合实践

把本讲三块拼起来，完成一个**迷你图文检索 + 错误分析**任务：

1. **准备数据**：运行 Notebook，下载 `oxcats` 猫图集（见 Notebook「Let's also get a subset of cats images」cell）。
2. **零样本分类**：自选 3~5 张图，用一组候选描述（如 `"a cat"`, `"a dog"`, `"a fluffy animal"`, `"a car"`）做零样本分类，打印每张图的概率向量。
3. **文本检索**：自拟 3 个查询（如 `"a gray cat"`, `"a cat with stripes"`, `"a sleeping cat"`），对整批猫图做检索，保存挑中的图。
4. **错误分析（核心）**：找出分类或检索**出错或置信度很低**的案例，回答：
   - 是候选文本太抽象 / 太接近，还是图片特征不典型？
   - 如果改写候选文本（例如加 `"a photo of a ..."` 前缀，或换更具体的描述），结果是否改善？
5. **写成一句话结论**：CLIP 的零样本能力在什么场景下可靠、什么场景下会失效。

> 这一步对应本讲的实践任务：「运行 Clip Notebook，用 CLIP 对一组图片与若干文本描述做零样本匹配，并分析匹配错误的案例」。**不要**编造运行结果——若本地跑不通，把分析建立在「候选文本与图片语义的关系」上即可，并标注「待本地验证」。

---

## 6. 本讲小结

- **图文对齐**：CLIP 用图像编码器（ViT）和文本编码器（Transformer）把图和句子压成**同维向量**，落到同一空间，于是可用余弦相似度直接比较图文。
- **对比学习**：训练时在 batch 内最大化正对（图 i ↔ 文 i）相似度、最小化负对相似度，负样本由 batch 内其他对天然充当，故 batch 越大学得越好；这叫 InfoNCE 损失。
- **零样本**：CLIP 已预训练好，做新任务**不用再训练**。分类（1 图选 N 类）用 `logits_per_image`，检索（1 句选 N 图）用 `logits_per_text`，本质都是相似度矩阵上 softmax 取 argmax。
- **VQGAN+CLIP**：把 CLIP 当作「图文相似度损失」，对随机隐向量 z 做梯度下降，经 VQGAN 生成图——这与 u3-l5 风格迁移「冻结网络、优化输入」同构，只是把 VGG 换成 CLIP、把像素换成 z。
- **本讲位置**：CLIP 是第 III 单元（CV）与第 IV 单元（NLP）两条线的汇合点，它让「看图」和「读字」第一次能用同一套向量语言对话。

---

## 7. 下一步学习建议

- **想深入 CLIP 原理**：读 README 给出的原论文 *Learning Transferable Visual Models From Natural Language Supervision*（arXiv:2103.00020），重点看它的对称 InfoNCE 损失与超大 batch 训练。
- **想理解生成路线**：顺着 README 的 DALL·E 小节，对比「CLIP 对齐空间」与「DALL·E 把图文当同一串 token 生成」两条多模态路线的差异。
- **想动手扩展**：尝试把 CLIP 用在自己的图片库上，搭一个「用一句话搜图」的小 Demo；或安装 Pixray / 其他文生图库，体验 VQGAN+CLIP 的生成闭环。
- **回到课程主线**：本讲属于 `X-Extras` 附加单元，课程主干已基本结束；可继续阅读 `X-Extras` 下其它附加内容，或回到 u5-l5「AI 伦理与负责任的 AI」，思考这类强大的多模态 / 生成模型带来的伦理风险。
