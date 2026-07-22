# 精读一篇 meetup 回顾博客

> 本讲对应的仓库内文件：`blogs/Efficient LLM Deployment and Serving.md`（仓库内唯一一篇完整长博客）。

## 1. 本讲目标

学完本讲，读者应该能够：

- 理解「meetup 回顾类资料」的组织结构：**一场活动 → 多个项目 → 各自要点 + 配图**。
- 从一篇长博客中，为每个项目提炼出至少 3 条可复述的要点。
- 说清博客正文与 `docs/figs/` 下配图的**一一对应关系**（哪段文字配哪张图）。
- 一句话概括 SGLang、XGrammar、FlashInfer、MLC-LLM 四个项目各自要解决的核心问题。

本讲是单元 3「深度阅读与硬件适配」的第一篇，承接 u1-l3 的「把 README 当导航地图」技能：我们已经会从 README 找到资料入口，现在要**真正打开一篇资料读懂它**。

## 2. 前置知识

在开始精读前，先用最朴素的语言建立几个直觉（细节会在后续讲义展开，本讲只需「知道是什么」）：

- **本仓库是 SGLang 官方学习资料聚合库**，不含任何运行时代码（见 u1-l1）。
- **README 是导航地图**：博客区段大多是外部 `lmsys.org` 链接，而本篇博客是仓库内**唯一一篇完整长文**（见 u1-l2）。
- **meetup（社区聚会）**：开发者围绕某个主题做的线上/线下分享会，通常一个项目讲一场，多场串成一次活动。
- 几个项目名词先混个眼熟：
  - **SGLang**：LLM/VLM 推理服务框架（serving framework），主打高吞吐、低延迟。
  - **RadixAttention**：用基数树（Radix Tree）记录并复用 KV Cache 的机制。
  - **MLA（Multi-Head Latent Attention，多头潜在注意力）**：把 KV Cache 压成低维潜在向量的注意力变体（原理详见 u2-l4）。
  - **受限解码 / XGrammar**：约束模型只输出合法 JSON 等结构的生成技术（详见 u2-l3）。
  - **attention 内核（kernel）**：高效计算注意力的底层算子；**FlashInfer** 就是这样一个算子库。
  - **跨平台部署**：把同一个模型跑在服务器、桌面、手机、嵌入式等多种硬件上；**MLC-LLM** 就是这样的引擎。

> 阅读提示：这篇博客是 2024 年 10 月 16 日「Efficient LLM Deployment and Serving Meetup」的**事后回顾**（review），不是会议纪要逐字稿，而是组织者对四场分享的二次提炼。所以正文里会出现「Lianmin Zheng 和 Liangsheng Yin 大幅推进了 CPU 与 GPU 的整合」这类**对演讲者贡献的总结性描述**，阅读时要区分「项目本身的能力」与「这次分享强调的亮点」。

## 3. 本讲源码地图

本讲只围绕一篇博客及其配图展开，涉及的全部文件如下：

| 文件 | 在本讲中的作用 |
| --- | --- |
| `blogs/Efficient LLM Deployment and Serving.md` | 精读对象，仓库内唯一一篇长博客（113 行） |
| `blogs/docs/figs/1016 meetup - SGLANG scheduler.png` | SGLang 段落的配图：CPU/GPU 协同调度 |
| `blogs/docs/figs/1016 meetup - Xgrammer benchmark.png` | XGrammar 段落的配图：解码加速基准 |
| `blogs/docs/figs/1016 meetup - flashinfer.png` | FlashInfer 段落的配图：attention 内核介绍 |
| `blogs/docs/figs/1016 meetup - MLC LLM.png` | MLC-LLM 段落的配图：跨平台部署概览 |
| `README.md` | 第 86 行给出本博客的索引条目，是找到它的「路标」 |

> 命名小提醒：配图文件名里的 `Xgrammer` 是「XGrammar」的拼写错误（少了一个 `a`、多了个 `e`），这是仓库既有文件的真实命名，引用时需原样保留。同理，博客正文里出现的「Jason decoding」实为「JSON decoding」的笔误。

## 4. 核心概念与源码讲解

博客的正文结构非常规整，可以看作一个「四宫格」：

```
博客正文
├── 引言：活动背景（哪些机构参与）          ← 第 6–8 行
├── 项目一：SGLang（CPU/GPU 协同 + MLA）   ← 第 10–36 行  + scheduler 配图
├── 项目二：XGrammar（结构化解码加速）      ← 第 38–58 行  + benchmark 配图
├── 项目三：FlashInfer（attention 内核）    ← 第 60–79 行  + flashinfer 配图
├── 项目四：MLC-LLM（跨平台部署）           ← 第 82–92 行  + MLC LLM 配图
└── 结语：LMSYS 介绍与延伸链接             ← 第 94–113 行
```

下面把这四个「最小模块」逐个拆开讲。

### 4.1 SGLang 的 CPU/GPU 协同与 MLA

#### 4.1.1 概念说明

SGLang 段落是全篇最长的一段，回答两个问题：

1. **SGLang 是什么？**——一个面向大语言模型（LLM）和视觉语言模型（VLM）的推理服务框架，由 LMSYS Org 开发。
2. **这次分享强调了它的哪两个亮点？**——一是 CPU 与 GPU 的协同优化（体现在调度器上），二是 MLA（多头潜在注意力）。

这两个亮点恰好对应后续讲义的两条主线：「CPU 开销隐藏 / 调度」对应 u2-l2，「MLA」对应 u2-l4。本讲只需抓住博客对它们的**通俗描述**。

#### 4.1.2 核心流程

博客把 SGLang 的提速来源归纳成一条链：

- **前端**提供编程原语（generation、parallelism control），让复杂程序好写。
- **运行时**用两个关键机制加速：**RadixAttention**（复用 KV Cache）+ **压缩有限状态机**（加速解码）。
- 在此之上，**CPU/GPU 协同**让两类处理器错峰工作：CPU 负责排班与控制流，GPU 负责大块计算，二者重叠以**把 CPU 开销藏进 GPU 计算时间里**，从而降低延迟。
- **MLA** 则从「精度 + KV Cache 体积」角度发力：把注意力要缓存的内容压成更紧凑的形式，既保精度又省显存。

直觉上可以这样理解 CPU/GPU 协同：

\[ \text{端到端延迟} \approx \max(\text{CPU 排班时间},\ \text{GPU 计算时间}) \]

当 CPU 排班能与 GPU 计算**重叠**时，整体延迟由较慢的一方决定，而不是两者相加——这正是「隐藏开销（overhead hiding）」的含义（工程细节见 u2-l2）。

#### 4.1.3 源码精读

博客开篇给 SGLang 下定义，并点出两个运行时加速机制：

> SGLang is a fast serving framework … its runtime accelerates execution through innovations like **RadixAttention for KV cache reuse** and **compressed finite state machines for faster decoding**.

对应 [blogs/Efficient LLM Deployment and Serving.md:L13-L14](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L13-L14)。这里还给出一个关键数字：相比其它 SOTA 推理系统，吞吐可达 **6.4×**；并提到已被 Databricks、Bytedance 采纳。

随后是本段的第一条要点「CPU and GPU Optimization」，**紧接着就插入 scheduler 配图**——这就是正文与配图的绑定关系：

对应 [blogs/Efficient LLM Deployment and Serving.md:L17-L19](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L17-L19)，其中第 19 行引用的图就是 [blogs/docs/figs/1016 meetup - SGLANG scheduler.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/docs/figs/1016%20meetup%20-%20SGLANG%20scheduler.png)。这段配图说明 CPU 与 GPU 如何协调、把延迟降到最低。

第二条要点是 MLA：

对应 [blogs/Efficient LLM Deployment and Serving.md:L21-L23](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L21-L23)。博客把 MLA 描述为「聚焦相关数据、忽略无关信息」，从而同时提升速度与精度。注意：这是面向大众的**比喻式说明**，MLA 的真实机制（下投影到潜在向量、解耦 RoPE）在 u2-l4 讲。

段落末尾还列出了三个未来方向（扩展到图像/音频、投机解码 speculative decoding、增强适应性），并配 `SGLANG future work.png` 图（第 34 行）。这些「future work」在后续版本里大多已落地，可作为对照阅读的线索。

#### 4.1.4 代码实践（源码阅读型）

本仓库无可运行代码，因此采用「源码阅读型实践」。

1. **实践目标**：验证「正文要点 ↔ 配图」的绑定关系。
2. **操作步骤**：
   - 打开博客 [L17-L23](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L17-L23)。
   - 注意第 17 行的「CPU and GPU Optimization」要点**正下方**（第 19 行）就是 scheduler 图的 Markdown 引用 `![scheduler](docs/figs/1016%20meetup%20-%20SGLANG%20scheduler.png)`。
   - 用只读命令确认这张图确实存在于仓库：
     ```
     git ls-files 'blogs/docs/figs/*SGLANG*'
     ```
3. **需要观察的现象**：每个 bullet 要点紧跟一张配图，图注（如 `![scheduler]`、`![benchmark]`）就是该图所解释的概念。
4. **预期结果**：`git ls-files` 应输出 `blogs/docs/figs/1016 meetup - SGLANG scheduler.png`（以及 future work 那张），证明正文引用的图都是仓库内真实资产。

#### 4.1.5 小练习与答案

**练习 1**：博客说 SGLang「相比其它 SOTA 推理系统吞吐可达多少倍」？
**答案**：6.4×（见第 14 行）。

**练习 2**：SGLang 段落里，scheduler 配图紧跟在哪条要点之后？这说明什么阅读规律？
**答案**：紧跟在「CPU and GPU Optimization」要点之后（第 17→19 行）。规律是：**配图是对其上方要点的图解**，读博客时应把「上文要点 + 紧随其图的图注」当作一个单元来理解。

---

### 4.2 XGrammar 的解码加速

#### 4.2.1 概念说明

XGrammar 段落讲的是**结构化生成**：让大模型在生成 JSON 等有固定格式的文本时，既能保证格式合法，又能比传统方法更快。博客原文标题写作「Elevating **Jason** Decoding」，这是「JSON Decoding」的笔误（详见 4.2.3 的阅读提醒）。

XGrammar 已被集成进 SGLang 和 MLC 两个框架，是一个独立的「结构化生成引擎」。

#### 4.2.2 核心流程

博客用三个递进的要点描述 XGrammar 的加速：

1. **更快的解码速度**：相比已有后端快 **3 到 5 倍**。实现手段是「CPU 开销管理 + Token mask cache（token 掩码缓存）」。
2. **基准测试表现**：端到端速度提升约 **30%**，即便在「常量字符串很少」的场景下也成立。
3. **文法引导生成（Grammar-Guided Generation）**：达到 SOTA 效率，让输出更精确。

要注意两组数字的口径不同：

| 数字 | 含义 | 比较对象 |
| --- | --- | --- |
| 3–5× | **解码**速度 | 已有后端 |
| 30% | **端到端**速度提升 | 整体流程 |

直觉：token mask cache 把「哪些 token 合法」的判断预先算好缓存，运行时只查表不重算，于是 CPU 开销被压低、能与 GPU 计算重叠——这与 4.1 的「overhead hiding」是同一套思路的不同应用（受限解码的完整原理见 u2-l3）。

#### 4.2.3 源码精读

XGrammar 段落开头就点明它已集成进两个框架，并给出第一条要点：

对应 [blogs/Efficient LLM Deployment and Serving.md:L40-L44](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L40-L44)，第 44 行引用的图是 [blogs/docs/figs/1016 meetup - Xgrammer benchmark.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/docs/figs/1016%20meetup%20-%20Xgrammer%20benchmark.png)。这张 benchmark 图直观展示「3–5× 解码加速」的对比柱状图。

> 阅读提醒：第 38、40 行的「Jason Decoding」是「JSON Decoding」的笔误；配图文件名 `Xgrammer benchmark` 里的 `Xgrammer` 同样是 `XGrammar` 的拼写错误。阅读二手资料时，识别并自行纠正这类笔误是一项基本能力。

第二条要点关于端到端 30% 提升：

对应 [blogs/Efficient LLM Deployment and Serving.md:L46-L50](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L46-L50)，第 50 行另配了一张 `Xgrammer optimization.png`（本讲未列为必读图，但同目录下可查）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：区分两组提速数字的口径。
2. **操作步骤**：
   - 阅读博客第 42 行与第 46 行。
   - 在自己的笔记里画一张表，把「3–5×」和「30%」分别归到「解码速度」与「端到端速度」两栏。
3. **需要观察的现象**：两个数字出现在**不同**的 bullet 里，分别配 benchmark 图与 optimization 图。
4. **预期结果**：能清楚说出「3–5× 指解码阶段相对其它后端，30% 指端到端整体」——避免日后读到 u2-l3 时把两套口径混淆。

#### 4.2.5 小练习与答案

**练习 1**：XGrammar 解码加速依赖哪两个手段？
**答案**：CPU 开销管理（overhead management）与 Token mask cache（token 掩码缓存）。

**练习 2**：「3–5×」和「30%」分别衡量什么？为什么不能直接相乘？
**答案**：前者是**解码速度**相对已有后端的倍数，后者是**端到端**整体提升的比例；二者衡量的是流水线的不同环节、比较对象也不同，不能相乘。

---

### 4.3 FlashInfer 的 attention 内核

#### 4.3.1 概念说明

FlashInfer 由 Zihao Ye 介绍，定位是**专门为 LLM 服务优化的 attention 内核库（kernel library）**。如果说 XGrammar 管「生成什么」，FlashInfer 就管「注意力这一步算得多快」——它把 attention 计算做成高性能底层算子，并支持 JIT（即时编译）。

#### 4.3.2 核心流程

博客给出两个要点：

1. **注意力算子优化**：在各种 **KV-Cache 存储格式**和应用场景下，提升 attention 算子的效率。
2. **灵活性与速度**：通过**用户自定义算子（user-defined functors）**支持定制化的 attention 变体，并持续为 LLM 服务中**动态输入**优化内核性能。

直觉：LLM 服务里请求长度、batch 大小一直在变（动态输入），固定编译的内核难以处处最优；FlashInfer 用 JIT 在运行时按需生成内核，兼顾灵活与高效。这一点与 4.1 的「RadixAttention 复用 KV Cache」是上下游关系——FlashInfer 负责把「带着 KV Cache 的 attention」这一步算得更快。

#### 4.3.3 源码精读

FlashInfer 段落开门见山点出讲者与定位：

对应 [blogs/Efficient LLM Deployment and Serving.md:L62-L68](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L62-L68)，第 68 行引用的图是 [blogs/docs/figs/1016 meetup - flashinfer.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/docs/figs/1016%20meetup%20-%20flashinfer.png)。这张图通常给出 FlashInfer 的定位与支持的 attention 变体概览。

随后博客还给出 FlashInfer 的 roadmap（路线图），并配第二张图 `flashinfer roadmap.png`（第 79 行）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：把 FlashInfer 的两个要点分别「挂」到对应文字行。
2. **操作步骤**：
   - 打开博客第 64、66 行。
   - 把「KV-Cache 存储格式优化」与「用户自定义算子」分别抄成两条笔记。
3. **需要观察的现象**：第 68 行的 flashinfer 配图紧跟在这两条要点之后，是对它们的图解。
4. **预期结果**：能复述「FlashInfer = 优化 attention 算子 + 支持自定义变体 + JIT 应对动态输入」。

#### 4.3.5 小练习与答案

**练习 1**：FlashInfer 是「框架」还是「内核库」？它优化的是哪一步？
**答案**：是 attention 内核库（kernel library），优化的是注意力（attention）这一步的计算，覆盖多种 KV-Cache 存储格式。

**练习 2**：FlashInfer 如何应对 LLM 服务中的「动态输入」？
**答案**：通过 JIT（即时编译）在运行时按需生成内核，并对动态输入持续优化内核性能。

---

### 4.4 MLC-LLM 的跨平台部署

#### 4.4.1 概念说明

MLC-LLM 由 Ruihang Lai 介绍，博客给它的比喻是「语言模型部署的瑞士军刀（Swiss Army knife）」。它的核心卖点是**通用部署（universal deployment）**：同一套方案，从服务器、桌面到手机、嵌入式设备都能跑，且保持低延迟、高吞吐。

#### 4.4.2 核心流程

MLC-LLM 的思路可以概括为「编译一次，到处部署」：

- 把模型经编译流程生成面向不同硬件后端的产物。
- 在**多种平台**（servers / desktops / mobile / embedded）上都能达到低延迟、高吞吐。
- 关键是**灵活性**：能无缝适配不同硬件环境，又不牺牲性能。

与前面三个项目的分工：

| 项目 | 主要负责 |
| --- | --- |
| SGLang | 服务框架（调度、KV 复用、整体编排） |
| XGrammar | 结构化生成（输出合法且快） |
| FlashInfer | attention 内核（把注意力算快） |
| MLC-LLM | 跨平台部署（把模型搬到各种硬件） |

四个项目恰好覆盖「**编排 → 生成 → 算子 → 落地硬件**」一条完整链路，这也是这次 meetup 把它们放在一起的原因。

#### 4.4.3 源码精读

MLC-LLM 段落点明讲者、比喻与目标平台：

对应 [blogs/Efficient LLM Deployment and Serving.md:L82-L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L82-L92)，其中第 92 行引用的图是 [blogs/docs/figs/1016 meetup - MLC LLM.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/docs/figs/1016%20meetup%20-%20MLC%20LLM.png)。第 87 行还另配了一张 `MLC LLM architecture.png` 展示其架构（本讲列为可选拓展图）。

博客强调 MLC-LLM「能无缝适配不同硬件环境而不牺牲性能」——这是「瑞士军刀」比喻的落脚点。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：用一张表把四个项目串成一条链路。
2. **操作步骤**：
   - 阅读博客第 82–92 行。
   - 回看 4.1–4.3，把 SGLang / XGrammar / FlashInfer / MLC-LLM 填进上面那张分工表。
3. **需要观察的现象**：四个项目在博客里是**并列的四个二级标题**（`###`），各自配图、各自讲一个层面。
4. **预期结果**：能说出 MLC-LLM 在链路中处于「落地到各种硬件」的最后一环。

#### 4.4.5 小练习与答案

**练习 1**：博客用什么比喻形容 MLC-LLM？它强调的核心能力是什么？
**答案**：「瑞士军刀（Swiss Army knife）」；核心能力是跨平台（服务器/桌面/手机/嵌入式）的通用部署，低延迟、高吞吐。

**练习 2**：为什么这次 meetup 把 MLC-LLM 和 SGLang、XGrammar、FlashInfer 放在一起？
**答案**：四者覆盖「编排 → 结构化生成 → attention 算子 → 跨平台落地」的完整部署链路，组合起来才是「高效的 LLM 部署与服务」全貌。

---

## 5. 综合实践

把全篇博客读完后，完成下面这个贯穿四个模块的小任务，产出一份可保存的学习笔记。

**任务：四项目要点 + 配图速查卡**

1. **实践目标**：把本讲四个最小模块的成果整合成一张可复用的速查卡，并验证正文与配图的绑定关系。
2. **操作步骤**：
   - 精读 [blogs/Efficient LLM Deployment and Serving.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md) 全文（共 113 行）。
   - 为 SGLang / XGrammar / FlashInfer / MLC-LLM **各写 3 条要点**。
   - 在每条要点后**标注它对应的配图文件名**（从 `docs/figs/` 里选）。
   - 用只读命令核对配图确实存在：
     ```
     git ls-files 'blogs/docs/figs/'
     ```
3. **需要观察的现象**：
   - 每个项目的关键 bullet 之后都紧跟一张 `![图注](docs/figs/...)` 引用。
   - `git ls-files` 列出的 9 张图里，能找到本讲用到的 4 张（scheduler、Xgrammer benchmark、flashinfer、MLC LLM）。
4. **预期结果**：得到一张类似下表的速查卡（示例前两行已填，请补全）：

   | 项目 | 要点（含关键数字） | 对应配图 |
   | --- | --- | --- |
   | SGLang | ① CPU/GPU 协同隐藏开销；② MLA 提精度省显存；③ 吞吐达 6.4× | `1016 meetup - SGLANG scheduler.png` |
   | XGrammar | ① 解码快 3–5×（token mask cache）；② 端到端 +30%；③ 文法引导生成 | `1016 meetup - Xgrammer benchmark.png` |
   | FlashInfer | ① …；② …；③ … | `1016 meetup - flashinfer.png` |
   | MLC-LLM | ① …；② …；③ … | `1016 meetup - MLC LLM.png` |

5. **若无法本地核对**：配图清单可改用本讲「源码地图」给出的文件名作为依据，并标注「待本地验证」。

> 进阶（可选）：博客结语（第 100–111 行）还介绍了 LMSYS 的其它项目（Vicuna、Chatbot Arena）并给出 SGLang 的文档/GitHub/Slack 入口。把这些入口整理成「延伸资源清单」，可为本单元后续 u4-l3《资料的边界与延伸资源》做准备。

## 6. 本讲小结

- 本仓库内**唯一一篇长博客**记录了 2024-10-16 的 meetup，按「引言 → 四个项目 → 结语」组织，结构规整、易于拆读。
- **SGLang**：服务框架，亮点是 CPU/GPU 协同（scheduler 图）与 MLA；吞吐可达 6.4×。
- **XGrammar**：结构化生成引擎，解码快 3–5×、端到端 +30%，靠 token mask cache 与 CPU 开销管理（benchmark 图）。
- **FlashInfer**：attention 内核库，优化多种 KV-Cache 格式、支持自定义变体与 JIT 应对动态输入（flashinfer 图）。
- **MLC-LLM**：「瑞士军刀」式跨平台部署，覆盖服务器到嵌入式（MLC LLM 图）。
- **核心阅读方法**：每个要点紧跟一张配图，应把「上文 bullet + 图注」当一个单元读；并注意识别「Jason/Xgrammer」这类笔误。

## 7. 下一步学习建议

- 想深入 **CPU/GPU 协同与调度**：读 u2-l2《调度器与性能优化资料》，配合仓库内 `slides/lmsys_1st_meetup_sglang.pdf`。
- 想深入 **XGrammar / 受限解码**：读 u2-l3《受限解码与结构化输出资料》，配合 `slides/lmsys_1st_meetup_xgrammar.pdf` 与 `slides/lmsys_1st_meetup_constrained_decoding.pdf`。
- 想深入 **MLA**：读 u2-l4《DeepSeek MLA 与模型优化资料》，配合 `slides/lmsys_1st_meetup_deepseek_mla.pdf`。
- 想看**同一场 meetup 的录像**：在 README 的 Videos 区段找到 `[2024-10-16] The First SGLang Online Meetup`（README 第 158 行），先听讲解再回看本博客，理解会更立体。
- 继续本单元：下一篇 u3-l2《硬件适配与量化资料》会把视角从「项目」转向「AMD 等具体硬件」。
