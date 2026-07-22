# DeepSeek MLA 与模型优化资料

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **Multi-Head Latent Attention（MLA，多头潜在注意力）** 到底解决了什么问题，以及它和标准多头注意力（MHA）、多查询注意力（MQA）、分组查询注意力（GQA）的区别。
- 解释 SGLang 在 **v0.3 版本** 中针对 DeepSeek 模型取得的「**7× MLA 提速**」的含义，以及它为什么重要。
- 在本仓库中 **定位全部 MLA 相关资料**，并理解它们为什么分散在 meetup、biweekly、Hyperbolic 线下聚会等多个活动中，构成一个「资料簇」。
- 学会对比两份标题相近、侧重不同的 MLA 幻灯片，提炼出一份可复用的学习要点清单。

> 本讲承接 [u2-l2 调度器与性能优化资料](u2-l2-scheduler-performance.md) 提出的「**资料簇**」阅读法：同一个主题往往横跨多次活动、多个 README 区段，必须当成一组整体阅读。本讲就把这套方法套用到 MLA 这个主题上。

## 2. 前置知识

本讲涉及的几个概念全部来自公开的注意力机制背景知识（来自 DeepSeek 系列论文与通用 LLM 推理常识，**不是本仓库的内容**）。本仓库只收录资料、不含运行时代码，因此下面是帮你读懂幻灯片的「背景补丁」。

### 2.1 KV Cache：自回归推理的内存大头

大模型生成文本时是「一个 token 一个 token」往后写的。每生成一个新 token，都要让它「看见」之前所有 token。为了避免对历史 token 重复计算，推理引擎会把每一层、每一个 token 的 **Key（键）** 和 **Value（值）** 缓存下来，这就是 **KV Cache**。

对于标准的多头注意力（MHA），每个 token 在每一层要缓存的向量规模约为：

\[
\text{KV size}_{\text{MHA}} = 2 \cdot n_h \cdot d_h
\]

其中 \(n_h\) 是注意力头数，\(d_h\) 是每个头的维度，因子 2 来自 K 和 V 两份。当模型很大、上下文很长时，KV Cache 会变成最吃显存的部件，直接限制了能同时服务的请求数（batch size）和最大上下文长度。

### 2.2 缩小 KV Cache 的三条路线

业界主要有三种「给 KV Cache 瘦身」的思路：

| 方案 | 做法 | 代价 |
|------|------|------|
| **MQA**（Multi-Query） | 所有头共享同一份 K、V | 瘦身最狠，但质量下降明显 |
| **GQA**（Grouped-Query） | 把头分组，组内共享 K、V | 折中方案，介于 MHA 与 MQA 之间 |
| **MLA**（Multi-head Latent） | 把 K、V **低秩压缩**成一个潜在向量，只缓存它 | 工程更复杂，但能兼顾质量与瘦身 |

MLA 是 DeepSeek 提出的第四条路：它不像 MQA/GQA 那样「砍头」，而是「压缩」。本讲后续会展开。

### 2.3 RoPE：位置编码的伏笔

**RoPE（旋转位置编码，Rotary Position Embedding）** 是把「位置信息」以旋转角度的方式乘进 Q 和 K 的技术。先记住一个结论：**RoPE 是逐维度相乘的，它会干扰 MLA 的低秩压缩**——这恰恰是 MLA 设计里最巧也最绕的一环，第 4.1 节会用到。

---

## 3. 本讲源码地图

本仓库不含运行时源码，所谓「源码」就是导航索引 `README.md` 与仓库内的资料文件。本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | 资料导航索引，本讲要反复回到它的 Slides / Blog / Videos 三个区段定位 MLA 条目 |
| [slides/lmsys_1st_meetup_deepseek_mla.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/lmsys_1st_meetup_deepseek_mla.pdf) | 第一场 LMSYS 线上 meetup 的「SGLang DeepSeek MLA」幻灯片（仓库内资产） |
| [slides/sglang_deepseek_model_optimizations.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_deepseek_model_optimizations.pdf) | Hyperbolic 线下聚会的「SGLang DeepSeek Model Optimizations」幻灯片（仓库内资产） |
| [blogs/Efficient LLM Deployment and Serving.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md) | 仓库内唯一一篇长博客，第一场 meetup 的回顾，正文里提到了 MLA |

> 提醒：两份 PDF 是二进制幻灯片，本讲无法逐页展示其内部文字。凡是涉及「幻灯片第几页讲了什么」的细节，都需要你本地打开 PDF 确认，本讲会标注「待本地验证」。本讲能 100% 核实的是 README 与博客里的文字条目。

---

## 4. 核心概念与源码讲解

### 4.1 DeepSeek MLA 原理：用低秩压缩缩小 KV Cache

#### 4.1.1 概念说明

**MLA（Multi-Head Latent Attention，多头潜在注意力）** 是 DeepSeek-V2 提出的一种注意力机制，目标是：**在几乎不损失精度的前提下，把 KV Cache 压到非常小**。

它的核心思想可以一句话概括：**不要缓存完整的 K 和 V，而是缓存它们的一个「低秩潜影（latent）」，要用的时候再还原出来。**

打个比方：MHA 相当于把每张照片的原件都存进仓库（占地方）；MQA/GQA 相当于让所有人共用同一张照片（省地方但失真）；MLA 则相当于只存每张照片的「压缩包」，谁要看再解压——既省地方，又比共用一张更接近原件。

这也是为什么本仓库的 meetup 回顾博客会用这样的措辞描述它：

> MLA technology has been instrumental in increasing the precision of the model.
> （MLA 技术在提升模型精度方面发挥了关键作用。）

这段话强调的正是 MLA 相对 MQA/GQA 的「**保精度**」优势。

#### 4.1.2 核心流程

MLA 的推理流程可以分为「压缩 → 缓存 → 还原」三步，再叠加一个处理 RoPE 的「解耦」小技巧。

**第 1 步：下投影，得到潜在向量。**
对当前 token 的隐状态 \(h_t\)（维度 \(d_{\text{model}}\)），用一个下投影矩阵压成一个低维的潜在向量 \(c_t\)（维度 \(d_c\)，远小于 \(n_h \cdot d_h\)）：

\[
c_t = W^{\downarrow} h_t, \qquad d_c \ll n_h \cdot d_h
\]

**第 2 步：只缓存潜在向量。**
KV Cache 里只存 \(c_t\)，于是单个 token、单层的缓存规模从 MHA 的 \(2 n_h d_h\) 降到了大约 \(d_c\)：

\[
\text{KV size}_{\text{MLA}} \approx d_c
\]

压缩比大致是：

\[
r = \frac{2\, n_h\, d_h}{d_c}
\]

> 以 DeepSeek-V2 论文公开的参考量级为例（\(n_h=128,\ d_h=128,\ d_c=512\)），理论上 \(r \approx 64\times\)。**这些具体数值来自 DeepSeek 论文、不属于本仓库**，精确数字请以论文与幻灯片为准（待确认）。

**第 3 步：上投影，还原出每个头的 K、V。**
算注意力时，再用上投影矩阵把 \(c_t\) 还原成各头的 K 和 V：

\[
k_t = W^{\uparrow}_K\, c_t, \qquad v_t = W^{\uparrow}_V\, c_t
\]

**第 4 步：解耦 RoPE（decoupled RoPE）。**
这里就用到第 2.3 节埋的伏笔了。RoPE 要乘在 K 上，但它和「先缓存 \(c_t\)、再上投影」的流程合不到一起（矩阵乘法顺序不可交换）。MLA 的解法是把 K 拆成两部分：

- **内容部分** \(k^C_t\)：来自潜在向量，可被吸收进 Q 的计算，走低秩路径；
- **位置部分** \(k^R_t\)：单独携带 RoPE 位置信息，维度很小，单独缓存。

于是实际缓存规模变成：

\[
\text{KV size}_{\text{MLA}} = d_c + d^R_h
\]

其中 \(d^R_h\) 是那条额外的 RoPE 维度。这样既保住了 RoPE 的位置编码能力，又把主体内容压到了低秩潜影里。

把上面四步串起来，就是 MLA 的执行流程：

```
隐状态 h_t
   │  下投影 W↓
   ▼
潜在向量 c_t  ──► 存入 KV Cache（只存它 + 一小段 RoPE 键）
   │  上投影 W↑
   ▼
还原各头 K、V ──► 参与注意力计算
```

#### 4.1.3 源码精读

本仓库没有 MLA 的实现代码（运行时代码在主仓库 `sgl-project/sglang`），但仓库里有两处关于 MLA 的「文字证据」可以精读。

**证据一：meetup 回顾博客对 MLA 的定性描述。**

博客在介绍 SGLang 的关键发展时，把 MLA 列为提升精度与速度的手段之一：

[blogs/Efficient LLM Deployment and Serving.md:21](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L21) —— 用一句话点明 MLA 的价值：提升精度、兼顾速度与准确性。

紧接着的一段进一步说明 MLA 被整合进 SGLang 之后的效果：

[blogs/Efficient LLM Deployment and Serving.md:23](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L23) —— 说明 MLA 不仅加速解码，还提升了整体准确率与效率。

注意：博客这里是**面向大众的回顾性表述**（「聚焦相关数据、忽略无关信息」是一种通俗解释），它没有给出第 4.1.2 节那样的低秩压缩数学细节。真正的原理性内容要去幻灯片与论文里找。

**证据二：v0.3 发布博客把 MLA 提速量化为「7×」。**

README 的 Blog 区段收录了 v0.3 的发布博客，标题里直接把 MLA 的提速写成了卖点：

[README.md:120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120) —— 标题「SGLang v0.3 Release: **7x Faster DeepSeek MLA**, 1.5x Faster torch.compile, …」，这是仓库里对 MLA 优化效果最具体的一句量化。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**——用 `grep` 在 README 里把所有 MLA 痕迹一次性捞出来，体会「资料簇」的分布。

1. **实践目标**：用一条命令列出 README 中所有提到 MLA 的行，确认 MLA 资料确实散落在多个区段。
2. **操作步骤**：在仓库根目录执行（大小写不敏感）：

   ```bash
   grep -ni 'mla' README.md
   ```

3. **需要观察的现象**：输出会包含 Slides 区段的两条幻灯片条目、Biweekly 区段的一条外链、Blog 区段的 v0.3 标题等多行，且它们的日期各不相同。
4. **预期结果**：你会看到至少 4 条匹配，分布在 Slides、Biweekly、Blog 等不同区段——这正是 MLA 构成「资料簇」的直接证据。（命令本身可在本地验证；具体匹配行数以你本地仓库为准。）
5. 如果想进一步定位 DeepSeek 相关资料，可把关键词换成 `deepseek`。

> 注意：本仓库没有可运行的脚本或 `Makefile`，因此本讲的「代码实践」以源码阅读、命令检索、PDF 对比为主，不涉及运行程序。

#### 4.1.5 小练习与答案

**练习 1**：MQA/GQA 和 MLA 缩小 KV Cache 的思路有什么本质不同？

> **参考答案**：MQA/GQA 是「**减少 K/V 的份数**」（让多个头共享同一份或同一组 K/V），是「砍头」；MLA 是「**压缩 K/V 的维度**」（把 K/V 低秩压成一个潜在向量，要用再还原），份数不变但每份更小。前者牺牲表达力换空间，后者尽量两头都要。

**练习 2**：为什么 MLA 要额外搞一个「解耦 RoPE」？

> **参考答案**：因为 RoPE 是对 K 逐维度做旋转相乘的，它无法被干净地吸收进「先存潜在向量、再上投影」的低秩流程里。MLA 于是把 K 拆成内容部分（走低秩、可吸收）和位置部分（单独携带 RoPE、单独缓存一小段），从而既保留了 RoPE 的位置编码，又保住了主体压缩收益。

---

### 4.2 DeepSeek 模型优化：v0.3 的 7× MLA 提速

#### 4.2.1 概念说明

理解了 MLA 原理后，下一个问题是：**SGLang 把 MLA 跑得多快？** 仓库给出的答案是 v0.3 的「**7× Faster DeepSeek MLA**」。

这里要分清两件事：

- **MLA 是模型结构层面的设计**（DeepSeek 模型自带，SGLang 不能改模型结构）。
- **「7× 提速」是推理引擎层面的工程优化**（SGLang 把 MLA 的注意力计算、KV Cache 管理做得更快了，从而相比自己之前的版本快了约 7 倍）。

换句话说，SGLang 不是「发明了 MLA」，而是「**把带 MLA 的 DeepSeek 模型服务得更快**」。这正是「模型优化（Model Optimizations）」这个词的含义——优化的是对模型的服务，而不是改模型本身。

#### 4.2.2 核心流程

把 v0.3 的 MLA 提速放到时间线里看，能看出它是一次「里程碑 → 公开讲解」的节奏：

```
2024-09-04  v0.3 发布博客：宣布 7x Faster DeepSeek MLA（里程碑）
     │
2024-09-21  Biweekly 开发者例会：同主题「SGLang DeepSeek MLA」外链幻灯片（讲解）
     │
2024-10-16  第一场 LMSYS meetup：「SGLang DeepSeek MLA」本地幻灯片 + 回顾博客（对外布道）
     │
2025-01-15  Hyperbolic 线下聚会：「SGLang DeepSeek Model Optimizations」（范围更广的工程总结）
```

这条线说明：**一个里程碑发布之后，团队会用 biweekly、meetup、线下聚会反复、多角度地讲解它**。阅读时应当把这些资料当成同一条故事线的前后章节。

#### 4.2.3 源码精读

**v0.3 的里程碑在 README 的 Announcement 与 Blog 两处都能追到。**

Announcement 区段把 v0.3 列为 2024 下半年的三大发布之一：

[README.md:24](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L24) —— 列出 v0.2 / v0.3 / v0.4 三大版本，并把每个版本都链接到对应的 LMSYS 博客。

Blog 区段则给出了 v0.3 的完整标题，MLA 提速被写成显式卖点：

[README.md:120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120) —— 标题里同时出现「7x Faster DeepSeek MLA」「1.5x Faster torch.compile」「Multi-Image/Video LLaVA-OneVision」三个卖点，说明 v0.3 是一次综合性发布，MLA 只是其中一项（ albeit 最亮眼的一项）。

把这两处合起来读，就完成了 [u2-l1](u2-l1-version-evolution.md) 讲过的「**追根溯源**」动作：从一句公告（L24）跳到 Blog 区段同名博客（L120），再把博客标题当成「核心卖点清单」来读。

#### 4.2.4 代码实践

这是一个**配置/参数观察型实践**——通过拆解博客标题来理解一次版本发布到底卖的是什么。

1. **实践目标**：把 v0.3 博客标题拆成一张「卖点清单」，并区分哪些与 MLA 直接相关、哪些无关。
2. **操作步骤**：
   - 打开 [README.md:120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120) 那一行。
   - 把标题里的三个卖点分别抄下来：① 7x Faster DeepSeek MLA；② 1.5x Faster torch.compile；③ Multi-Image/Video LLaVA-OneVision。
   - 给每个卖点标注：它优化的是「注意力/MLA」「编译加速」还是「多模态能力」。
3. **需要观察的现象**：三个卖点分属完全不同的优化维度。
4. **预期结果**：你会发现 MLA 提速只是 v0.3 的三分之一，但它是最容易被单独拎出来讲的一项——这也解释了为什么后续 meetup 会专门做一场 MLA 幻灯片。
5. 想看更细的优化手段，可点开该行的外链博客（lmsys.org）继续阅读。

#### 4.2.5 小练习与答案

**练习 1**：「7× Faster DeepSeek MLA」里的 7× 是和谁比？

> **参考答案**：从标题语境看，这是 SGLang v0.3 相对**自己更早版本**在 DeepSeek MLA 上的提速，而不是和别的引擎比、也不是 MLA 相对 MHA 的理论压缩比。比较口径是「同一引擎的前后版本」，这一点在读资料时要心里有数（精确对照基准以博客正文为准，待确认）。

**练习 2**：为什么说 SGLang 做的是「模型优化」而不是「改模型」？

> **参考答案**：MLA 是 DeepSeek 模型自带的结构，SGLang 作为推理引擎不会去改模型权重或结构；它优化的是 attention kernel、KV Cache 调度、算子融合等**服务侧**环节，让同一个模型跑得更快。所以叫 Model Optimizations（对模型的服务优化）而非 model modifications。

---

### 4.3 MLA 资料的多次出现：meetup 与 biweekly 构成的资料簇

#### 4.3.1 概念说明

承接 [u2-l2](u2-l2-scheduler-performance.md) 的「资料簇」阅读法，MLA 是本仓库里**最典型的资料簇之一**：同一个主题，在 README 的 Slides、Biweekly、Blog、Videos 多个区段反复出现，且分属不同活动。

学会识别这种簇，能帮你避免两个坑：

- **重复读**：把两份内容高度重叠的 MLA 幻灯片当成两件不同的事，浪费时间。
- **漏读**：只看到 meetup 那一份，错过 biweekly 和 v0.3 博客里的补充信息。

#### 4.3.2 核心流程

识别一个资料簇的通用流程（在 u2-l2 的方法上细化到 MLA）：

1. **关键词检索**：用 `grep -ni 'mla' README.md` 把所有含 MLA 的行捞出来。
2. **按日期排序**：把命中的条目按日期排成时间线（见 4.2.2 的那条线）。
3. **判断归属**：每条看它在哪个 `###` 子区段下（meetup / biweekly / 线下聚会）、是内链（`slides/...`）还是外链（`https://...`）。
4. **配对录像**：对每条幻灯片，去 Videos 区段按「同日期、同名」找配套 YouTube 录像（u2-l2 讲过的方法）。
5. **整体阅读**：把这一组资料当成「原理 → 里程碑 → 工程 → 回顾」的一个完整故事来读。

#### 4.3.3 源码精读

MLA 资料簇在本仓库里至少有四个入口，逐一精读它们在 README 里的登记行：

**入口 ①：第一场 LMSYS meetup 的 MLA 幻灯片（仓库内 PDF，本讲主角之一）。**

[README.md:80](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L80) —— `[2024-10-16] [SGLang DeepSeek MLA]`，登记在「The first LMSYS online meetup」子区段下，链接是 `slides/...` 相对路径，说明是**仓库内资产**，可直接打开。

**入口 ②：Biweekly 的同标题 MLA 幻灯片（外链 Google Slides）。**

[README.md:106](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L106) —— `[2024-09-21] [SGLang DeepSeek MLA]`，注意它的标题**和入口①一模一样**，但日期更早、且链接是 `https://docs.google.com/...`，说明这是一份**外部 Google 幻灯片**。这正是「资料簇」的典型特征：同名资料、不同活动、不同载体。

**入口 ③：Hyperbolic 线下聚会的模型优化幻灯片（仓库内 PDF，本讲另一主角）。**

[README.md:64](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L64) —— `[2025-01-15] [SGLang DeepSeek Model Optimizations]`，登记在「Hyperbolic in-person meetup」子区段下，标题比前两个更宽（「Model Optimizations」而非纯「MLA」），暗示它的**覆盖面更广**，MLA 只是其中一节。

**入口 ④：与幻灯片配对的 YouTube 录像（部分有，部分没有）。**

meetup 那场有配套录像：

[README.md:158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158) —— `[2024-10-16] [The First SGLang Online Meetup]`，与入口①同日。

biweekly 那场也有配套开发者例会录像：

[README.md:182](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L182) —— `[2024-09-21] [SGLang Developer Sync 20240921]`，与入口②同日。

> **一个重要提醒**：并不是每份幻灯片都有配套录像。Hyperbolic 线下聚会（2025-01-15，入口③）在 README 的 Videos 区段里**没有**同日期条目。所以 u2-l2 教的「按日期配对录像」法要灵活用——配得上就看，配不上就以 PDF 为准。

#### 4.3.4 代码实践（本讲主实践）

这是本讲规格指定的实践：**对比两份 MLA 幻灯片，指出各自侧重点，并写一份 MLA 学习要点清单。**

1. **实践目标**：通过对比，体会「同主题、不同活动、不同侧重」的资料如何互补。
2. **操作步骤**：
   - 打开两份仓库内 PDF：
     - A = [slides/lmsys_1st_meetup_deepseek_mla.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/lmsys_1st_meetup_deepseek_mla.pdf)（2024-10-16，标题「SGLang DeepSeek MLA」）
     - B = [slides/sglang_deepseek_model_optimizations.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_deepseek_model_optimizations.pdf)（2025-01-15，标题「SGLang DeepSeek Model Optimizations」）
   - 按下表逐项对比，把你的观察填进第三列（**待本地验证**——本讲无法读取 PDF 内页，以下「假设侧重」仅根据标题/日期/活动推断，需你打开 PDF 核实）：

     | 对比维度 | A：meetup MLA | B：模型优化 | 你的观察 |
     |---------|---------------|------------|----------|
     | 标题范围 | 仅 MLA | 更宽（Model Optimizations） | 待本地验证 |
     | 活动性质 | 线上社区首场 meetup | 线下企业聚会 | — |
     | 是否含 7× 提速数字 | 待本地验证 | 待本地验证 | 待本地验证 |
     | 是否含 MLA 之外的优化（如 FP8、torch.compile） | 待本地验证 | 待本地验证 | 待本地验证 |
     | 推断侧重 | MLA 原理与单一亮点 | 多项工程优化的汇总 | — |

   - 基于对比，写一份「MLA 学习要点清单」（至少 5 条），每条注明出自 A、B、还是第 4.1 节的原理。
3. **需要观察的现象**：两份 PDF 的目录/章节标题应当能反映「纯 MLA」与「多项优化」的范围差异；B 里 MLA 可能只是其中一个章节。
4. **预期结果**：你能用一句话区分两份资料——例如「A 聚焦讲清 MLA 是什么、为什么快；B 把 MLA 放进一整套 DeepSeek 优化方案里讲」。具体措辞以你读到的内容为准。
5. **如果无法本地打开 PDF**：可退而用入口②的外链 Google 幻灯片与入口①的回顾博客做替代对比，并明确标注「未读取 PDF 内页，结论待验证」。

#### 4.3.5 小练习与答案

**练习 1**：README 里有两份标题都叫「SGLang DeepSeek MLA」的资料，分别在哪？为什么不能只看其中一份？

> **参考答案**：一份在 Slides 区段的 meetup 子段下（[L80](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L80)，2024-10-16，仓库内 PDF）；另一份在 Biweekly 子段下（[L106](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L106)，2024-09-21，外链 Google Slides）。两者活动性质、日期、载体都不同，内容侧重点很可能不同，只看一份会漏掉另一份的补充视角，所以应当成资料簇整体读。

**练习 2**：如何判断一份 MLA 资料有没有配套录像？

> **参考答案**：拿幻灯片的日期（如 2024-10-16）去 README 的 Videos 区段找同日期条目。能找到（如 L158 的 First Meetup 录像）就先听讲解再看 PDF；找不到（如 2025-01-15 的 Hyperbolic 场就没有）就以 PDF 为准，不要假设一定有录像。

**练习 3**：v0.3 的「7×」和入口③的「Model Optimizations」幻灯片是什么关系？

> **参考答案**：7× 是 v0.3（2024-09-04）发布的量化里程碑；入口③（2025-01-15）是约 4 个月后的线下聚会幻灯片，标题更宽。可以推断后者是对包括 MLA 在内的多项 DeepSeek 优化的**阶段性工程总结**，7× 很可能是它引用的一个关键数字（具体是否引用，待本地打开 PDF 确认）。

---

## 5. 综合实践

把本讲三个模块串起来，产出一份「**MLA 学习地图**」。

任务：用一张表，把本仓库内所有 MLA 相关资料（含内链与外链）按下面的维度整理，并附上每份资料**最适合用来学什么**：

| 资料日期 | 资料名称 | 载体（内链/外链） | README 位置（行号） | 适合用来学什么 |
|---------|---------|------------------|---------------------|----------------|
| 2024-09-04 | v0.3 发布博客（7× MLA） | 外链 | L120 | ？ |
| 2024-09-21 | SGLang DeepSeek MLA（biweekly） | 外链 | L106 | ？ |
| 2024-10-16 | SGLang DeepSeek MLA（meetup PDF） | 内链 | L80 | ？ |
| 2024-10-16 | meetup 回顾博客 | 内链 | 见 blogs/ | ？ |
| 2025-01-15 | SGLang DeepSeek Model Optimizations | 内链 | L64 | ？ |

要求：

1. 补全「适合用来学什么」一列——把每份资料映射到本讲的三个模块（原理 / 7× 提速 / 资料簇）之一或多个。
2. 在表下方用 3 句话写出你的**推荐阅读顺序**，并说明理由（提示：可按「原理 → 里程碑 → 工程总结 → 回顾」排）。
3. 标注哪些资料你**实际读过**、哪些只看了 README 条目（保持诚实，便于后续补读）。

完成后，你就拥有了一张可复用的 MLA 学习路线图，它也是 [u4-l2 设计个人 SGLang 学习路线](u4-l2-study-plan.md) 的直接素材。

---

## 6. 本讲小结

- **MLA 用低秩压缩缩小 KV Cache**：把 K/V 压成潜在向量 \(c_t\) 再缓存，需要时上投影还原，配合「解耦 RoPE」处理位置编码，做到既省显存又保精度。
- **v0.3 的「7× Faster DeepSeek MLA」是引擎侧工程优化**，不是模型结构改动，也不是 MLA 相对 MHA 的理论压缩比；比较口径是 SGLang 的前后版本。
- **MLA 是一个典型的「资料簇」**：同标题资料分别出现在 meetup（仓库内 PDF）、biweekly（外链 Google Slides）、Hyperbolic 线下聚会（仓库内 PDF）多处，外加 v0.3 博客与 meetup 回顾博客。
- **配对录像要灵活**：meetup 与 biweekly 都有同日期 YouTube 录像，但 Hyperbolic 线下场在 Videos 区段没有对应条目。
- **读 MLA 资料的正确姿势**：先 grep 出全部条目，按日期排成时间线，再按「原理 → 里程碑 → 工程 → 回顾」整体阅读，而不是单看一份。
- **本仓库只聚合资料、不含运行时代码**：MLA 的真正实现代码在主仓库 `sgl-project/sglang`，原理细节在 DeepSeek 论文，本仓库提供的是「路标」。

## 7. 下一步学习建议

- 下一讲 [u2-l5 大规模部署：EP 与 PD 分离资料](u2-l5-large-scale-ep.md) 会从「单模型优化」升级到「大规模部署」，把 DeepSeek 的 MLA 优化放进 96×H100、EP + PD 分离的生产场景里看，你会更清楚 MLA 提速在端到端成本里的位置。
- 若想补 MLA 的原理数学，建议结合 README 指向的 **v0.3 LMSYS 博客外链**（[L120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120)）与 DeepSeek 原始论文一起读，本仓库的 PDF 幻灯片适合作为「图文导读」。
- 想看 MLA 与硬件结合的资料，可提前跳读 [u3-l2 硬件适配与量化资料](u3-l2-quantization-hardware.md)，了解 DeepSeek 在 AMD MI300X 上的量化与 kernel 优化。
- 复习时可回看 [u2-l2 调度器与性能优化资料](u2-l2-scheduler-performance.md) 的「资料簇」方法论——本讲正是它的应用范例。
