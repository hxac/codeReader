# 精读技术报告：训练流程、MoonViT 与 Thinking 能力的来源

## 1. 本讲目标

学完本讲，你应该能够：

1. **带着问题读一篇技术报告**：掌握「架构 → 预训练 → 微调 → 评测」的导读主线，能在 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) 中快速定位任何一个关键结论的出处。
2. **讲清 MoonViT 的原生分辨率设计**：NaViT 式打包、双重位置编码、pixel shuffle，以及「为什么既省算力又看得清」的定量解释。
3. **复述四阶段预训练全景**：从 Moonlight 中间检查点（5.2T 纯文本、8K 上下文）出发，经 ViT 训练、联合预训练、冷却、长上下文激活，最终得到 128K 上下文的多模态底座——共 4.4T token 的账本。
4. **梳理 Thinking 变体的三段后训练路径**：联合 SFT → 长 CoT SFT → 强化学习（RL），理解长思维链能力不是天生的，而是被「激活 + 强化」出来的。
5. **建立报告结论与 README 产品声明的映射**：知道 61.7 / 36.8 / 71.3 这些数字出自报告哪张表，也知道 2506 版的 +20.1 之类数字**并不在**报告里。

本讲是第 4 单元（专家层）的第一讲。前三单元你已跑通推理、理解了架构与部署；本讲把 [figures/arch.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/arch.png) 背后的「为什么」一次性挖到底。

## 2. 前置知识

本讲假设你已完成 u3-l1（架构解析）——MoonViT/MoE/MLA/pixel shuffle 这些词在 u3-l1 已建立。在此基础上补充读报告必需的几个概念：

- **技术报告（Technical Report）**：模型团队发布的「设计说明书 + 训练配方 + 成绩单」。它的章节结构通常固定：引言 → 方法（Approach）→ 数据 → 评测 → 结论。读它的正确姿势不是从头到尾，而是**带着问题跳读**：想知道「看得清」就直奔架构节，想知道「128K 怎么来的」就直奔预训练节。
- **对比损失（Contrastive Loss）与 SigLIP**：让「匹配的图文对」表示相近、「不匹配的」表示远离的训练目标。SigLIP 是其变体，把 softmax 换成逐对的 sigmoid 损失，无需构造大对比矩阵。MoonViT 的「师父」SigLIP-SO-400M 就是用它训出来的（约 400M 参数，14×14 patch）。
- **CoCa（Contrastive Captioners）**：一种「对比 + 描述生成」双目标训练范式：图像编码器与文本编码器算对比损失，另用一个文本解码器以 next-token prediction（NTP）生成图片描述。MoonViT 的第一阶段训练照搬了这个思路。
- **拒绝采样（Rejection Sampling，RS）**：让模型对同一问题生成多个答案，用判分器（奖励模型或规则）只保留正确的那部分当训练数据。「采得多、拒得狠」= 数据质量过滤器。
- **强化学习三要素（最小版）**：策略 \(\pi_\theta\)（就是当前模型）、奖励 \(r\)（判分）、KL 正则（防止模型为拿奖励跑偏得太离谱）。本讲在模块 4.3 只需要这三个词。
- **镜像下降（Mirror Descent）**：一类「每步只在参考策略附近小步更新」的优化框架。Kimi k1.5 与本报告的 RL 算法都是它的在线变体——第 \(i\) 轮以 \(\pi_{\theta_i}\) 为参考，更新出 \(\pi_{\theta_{i+1}}\)，再滚动前移。
- **证据来源声明**：本仓库是发布型仓库（u1-l3 已确认），报告是 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf)（即 README 引用的 arXiv:2504.07491，见 [README.md:L6](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L6)）。PDF 是二进制文件，没有行号锚点，本讲用**章节号 + 图表号**定位；如果你的环境无法复制 PDF 文字，可对照 arXiv 的 HTML 版（`https://arxiv.org/html/2504.07491v1`）阅读，章节编号与 PDF 一致。文中凡属推算或外部资源的结论都会注明。

## 3. 本讲源码地图

| 文件 / 章节 | 位置 | 在本讲中的作用 |
| :--- | :--- | :--- |
| `Kimi-VL.pdf` §2.1 Model Architecture | 本仓库 | MoonViT / MLP 投影层 / MoE 语言模型的第一手描述（含 Figure 3 架构图） |
| `Kimi-VL.pdf` §2.2 Muon Optimizer、§2.5 Infrastructure | 本仓库 | 优化器与 4D 并行的训练侧栏知识 |
| `Kimi-VL.pdf` §2.3 Pre-Training Stages | 本仓库 | 四阶段预训练主叙事（Figure 4 流程图 + Table 1 阶段明细表），模块 4.2 主战场 |
| `Kimi-VL.pdf` §2.4 Post-Training Stages、§3.3 Reasoning Data | 本仓库 | SFT → 长 CoT SFT → RL 全路径（Figure 5 流程图），模块 4.3 主战场 |
| `Kimi-VL.pdf` §4 Evaluation、§5 Limitation | 本仓库 | Table 3/4 成绩单、Figure 13 测试时扩展曲线，建立报告 ↔ README 映射 |
| `README.md` §1 Introduction、§8 Deployment | 本仓库 | 产品层性能声明（模块 4.1/4.2/4.3 各有对照点）与 `--max-model-len` 部署旋钮 |
| `figures/arch.png` | 本仓库 | 三段式架构图（u3-l1 的地图，本讲追问其来历） |

读报告的「问题 → 章节」速查表（建议贴在书签里）：

| 你想弄明白的问题 | 去报告哪里找 |
| :--- | :--- |
| MoonViT 为什么能吃任意分辨率？ | §2.1「MoonViT: A Native-resolution Vision Encoder」小节 |
| 视觉特征怎么进语言模型？ | §2.1「MLP Projector」小节（pixel shuffle + 两层 MLP） |
| 语言模型是从零训的吗？ | §2.1「MoE Language Model」小节（Moonlight 中间检查点） |
| 128K 上下文怎么来的？ | §2.3「Joint Long-context Activation Stage」+ Table 2（NIAH） |
| Thinking 的长思维链怎么训出来的？ | §2.4「Long-CoT SFT」「Reinforcement Learning」+ §3.3 |
| 61.7 / 36.8 / 71.3 出自哪里？ | §4.2 + Table 4 + Figure 13 |

## 4. 核心概念与源码讲解

### 4.1 MoonViT 设计细节：原生分辨率如何兼得「省算力」与「看得清」

#### 4.1.1 概念说明

u3-l1 已经告诉你 MoonViT 是「原生分辨率视觉编码器」；本讲追问：**原生分辨率到底指什么，它解决了谁的痛点？**

传统 VLM 的视觉编码器继承自 SigLIP/CLIP 这类对比学习模型，输入必须是固定尺寸（如 384×384）。处理高清图时有两条老路，各有代价：

- **缩放（resize）**：把 4K 截图压到 384×384——小字直接糊掉，「看不见」。
- **切子图（sub-image splitting）**：把大图切成多张 384×384 分别编码再拼接（LLaVA-OneVision 的做法）——看得清了，但引入人为的切割缝隙，破坏全局布局，且流程复杂。

MoonViT 的答案是第三条路：**不固定尺寸，图多大就按多大处理**。这带来两个直接收益，正好对应 README 的两条性能声明：

> Its native-resolution vision encoder, MoonViT, further allows it to see and understand ultra-high-resolution visual inputs, achieving 83.2 on InfoVQA and 34.5 on ScreenSpot-Pro, while maintaining lower computational cost with common visual inputs and general tasks.

—— [README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23)（原生分辨率编码器让模型看得清超高分辨率输入——InfoVQA 83.2、ScreenSpot-Pro 34.5——同时对常见输入保持更低的计算成本）

「看得清」= 高分辨率图不被压缩降采样；「省算力」= 小图不会被强行放大到固定尺寸凑 token。计算量正比于实际 token 数，而不是固定网格。

#### 4.1.2 核心流程

MoonViT 的名字来自 **Moon**shot + **ViT**，报告称其约 400M 参数、从 SigLIP-SO-400M 继续预训练而来。它处理一张图的全流程：

```text
输入图像（任意 W×H）
   │  ① 切 patch：按 14×14 像素切块（u3-l1 从模型配置确认）
   │     → ⌈W/14⌉ × ⌈H/14⌉ 个 patch
   ▼  ② 展平 + 打包（NaViT 式 Patch n' Pack）
        patch 序列 → 拼成 1D 序列；不同图可同批混装
   │  ③ MoonViT 编码块
   │     位置信息 = 插值后的绝对位置嵌入（继承 SigLIP）
   │              + 2D RoPE（横竖两个维度，补高分辨率位置精度）
   ▼  ④ pixel shuffle：空间 2×2 下采样，通道维相应扩展
        → 视觉 token 数量 ÷4
   ▼  ⑤ 两层 MLP 投影 → 语言模型嵌入空间
```

三个设计点值得展开：

1. **NaViT 式打包（Patch n' Pack）**：不同分辨率的图各切成 patch、各自展平，然后**像语言模型拼不同长度的句子一样**把多条 patch 序列打包进同一批次。报告强调这让 MoonViT 能直接复用 LLM 的核心算子——特别是 FlashAttention 支持的**变长序列注意力**——训练吞吐不因分辨率参差而打折。
2. **双重位置编码**：SigLIP 原本用「可学习的固定尺寸绝对位置嵌入」，迁移到更高分辨率时只能插值，插值后的位置精度随分辨率升高而劣化。MoonViT 的补法是在高度、宽度两个维度再加 **2D RoPE**，两套位置信息协同工作，且都兼容「展平 + 打包」流程。
3. **pixel shuffle 压缩**：MoonViT 输出的特征先做空间 2×2 下采样（4 个相邻位置并成 1 个，通道维 ×4）再进 MLP 投影层——视觉 token 数砍到四分之一，语言侧的注意力开销同步砍掉四分之三。

视觉 token 数的估算公式（u3-l3 用过，这里给出完整推导）：

\[ N_{\text{token}} \approx \frac{\lceil W/14 \rceil \times \lceil H/14 \rceil}{4} \]

用 2506 版的单图上限验证这个公式：README 说 2506 版支持单图 3.2M 像素（1792×1792），代入得 \(\lceil 1792/14 \rceil^2 / 4 = 128 \times 128 / 4 = 4096\) 个视觉 token——与 u3-l1 得出的「上限图约 4096 token」严丝合缝，公式自洽。

> [README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32)（2506 版单图支持 3.2M 总像素 1792×1792，为初代 4 倍，带动 V* 83.2、ScreenSpot-Pro 52.8、OSWorld-G 52.5 等高分辨率基准提升）

#### 4.1.3 源码精读

**（a）报告 §2.1 对 MoonViT 动机的原话**（[Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) §2.1，对照 figures/arch.png 阅读）：

> We design MoonViT ... to natively process images at their varying resolutions, eliminating the need for complex sub-image splitting and splicing operations, as employed in LLaVA-OneVision.

（设计 MoonViT 原生处理各种分辨率的图，免去 LLaVA-OneVision 那类复杂的子图切分拼接。）

> We incorporate the packing method from NaViT, where images are divided into patches, flattened, and sequentially concatenated into 1D sequences. These preprocessing operations enable MoonViT to share the same core computation operators and optimization as a language model, such as the variable-length sequence attention mechanism supported by FlashAttention.

（采用 NaViT 的打包法：切 patch、展平、顺序拼成 1D 序列，使 MoonViT 能与语言模型共享同一套核心计算算子与优化，例如 FlashAttention 支持的变长序列注意力。）

**（b）双重位置编码的原文**（同节）：

> While we interpolate these original position embeddings to better preserve SigLIP's capabilities, these interpolated embeddings become increasingly inadequate as image resolution increases. To address this limitation, we incorporate 2D rotary positional embedding (RoPE) across the height and width dimensions.

（插值保留 SigLIP 能力，但分辨率升高后插值嵌入愈发不够用；为此在高度、宽度维度引入 2D RoPE。）

**（c）pixel shuffle 与投影层的原文**（§2.1「MLP Projector」小节）：

> We first use a pixel shuffle operation to compress the spatial dimension ... performing 2×2 downsampling in the spatial domain and correspondingly expanding the channel dimension. We then feed the pixel-shuffled features into a two-layer MLP.

（先用 pixel shuffle 压缩空间维度——空间域 2×2 下采样、通道维相应扩展——再送入两层 MLP。）

**（d）MoonViT 的训练起点**（§2.3「ViT Training Stages」小节）：

> MoonViT is trained on image-text pairs ... The training incorporates two objectives: a SigLIP loss and a cross-entropy loss for caption generation ... Following CoCa's approach, the final loss function is formulated as \(\mathcal{L} = \mathcal{L}_{siglip} + \lambda \mathcal{L}_{caption}\), where \(\lambda = 2\).

（MoonViT 在图文对上训练，双目标：SigLIP 损失 + 条件描述生成的交叉熵损失，按 CoCa 范式合成总损失，\(\lambda=2\)。）

\[ \mathcal{L} = \mathcal{L}_{siglip} + \lambda\, \mathcal{L}_{caption}, \qquad \lambda = 2 \]

两个细节见微知著：图像、文本两个编码器都从 SigLIP-SO-400M 初始化并配「渐进分辨率采样」策略（训练中逐步放大可用尺寸）；文本解码器则从一个极小的 decoder-only 语言模型初始化。报告还记录了一个现象级观察——**放大 OCR 数据量时，caption 损失出现涌现式下降**，说明解码器「顺便」学会了认字。

#### 4.1.4 代码实践：用一张表验证「token 数 ∝ 像素数」

本实践不需要下载模型权重，只用 PIL 读图片尺寸，验证原生分辨率的定量含义。

1. **实践目标**：亲手算出仓库三张示例图的视觉 token 数，直观感受「图多大、token 多少」，并验证 1792×1792 → 4096 token 的上限换算。
2. **操作步骤**：在 u1-l2 搭好的 kimi-vl 环境里（pillow 已在 requirements.txt 中）运行以下**示例代码**：

   ```python
   # 文件名建议：count_visual_tokens.py（示例代码，非仓库原有文件）
   import math
   from PIL import Image

   PATCH = 14     # patch 边长（像素），u3-l1 从模型配置确认
   SHRINK = 4     # 2×2 pixel shuffle：空间 4 并 1，token 数 ÷4

   def estimate(w, h):
       patches = math.ceil(h / PATCH) * math.ceil(w / PATCH)
       return patches, patches // SHRINK

   for p in ["figures/demo.png", "figures/demo1.png", "figures/demo2.png"]:
       img = Image.open(p)
       patches, tokens = estimate(img.width, img.height)
       print(f"{p}: {img.width}x{img.height} -> {patches} patches -> ~{tokens} 视觉 token")

   patches, tokens = estimate(1792, 1792)   # 2506 版单图上限
   print(f"1792x1792 -> {patches} patches -> ~{tokens} 视觉 token (2506 上限)")
   ```

3. **需要观察的现象**：三张图的 token 数各不相同且相差近一倍——这就是「原生分辨率」：没有固定网格把大家拉平。
4. **预期结果**（按公式与三张图的真实尺寸 1182×718 / 596×742 / 574×730 推算）：

   ```text
   figures/demo.png:  1182x718 -> 4420 patches -> ~1105 视觉 token
   figures/demo1.png:  596x742 -> 2279 patches -> ~569 视觉 token
   figures/demo2.png:  574x730 -> 2173 patches -> ~543 视觉 token
   1792x1792 -> 16384 patches -> ~4096 视觉 token (2506 上限)
   ```

   注意这是估算口径：真实 processor 可能对边长做对齐取整，结果可能有几个 token 的出入；精确值**待本地验证**（可在 u2-l3 的实践中对比 processor 输出的 `input_ids` 长度来核对）。
5. **想一想**：demo.png 的 token 数（1105）约为 demo2.png（543）的两倍，而它俩在模型眼里只是「两张不同的图」——没有子图切分、没有放大填充，算力随内容复杂度自然伸缩。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接把 SigLIP 的绝对位置嵌入插值到高分辨率用，而要加 2D RoPE？
**答案**：插值只是「拉伸」原有网格，分辨率越高、相邻位置的可区分度越差（报告原话：increasingly inadequate）。2D RoPE 把位置编进注意力计算且天然支持任意坐标，在高分辨率下仍能表达细粒度位置信息；两者协同，兼顾继承 SigLIP 能力与高分辨率精度。

**练习 2**：NaViT 式打包除了「支持任意分辨率」，还给工程上带来了什么好处？
**答案**：让 MoonViT 与 LLM 共享同一套核心算子和优化——尤其 FlashAttention 的变长序列注意力。不同分辨率的图可以混装同一批次训练，吞吐量不因分辨率参差而明显下降；也免去子图切分/拼接的复杂流水线。

**练习 3**：一张 1792×1792 的图约产生多少视觉 token？如果不用 pixel shuffle 会是多少？
**答案**：用了 pixel shuffle 约 4096 个（16384 个 patch ÷4）；不用则约 16384 个——语言侧注意力与 KV cache 的开销将放大四倍，128K 上下文很快被几张高清图吃满。pixel shuffle 是「看得清」与「装得下」之间的关键杠杆。

### 4.2 预训练与长上下文扩展：从 5.2T 纯文本到 128K 多模态

#### 4.2.1 概念说明

模块 4.1 回答了「眼睛怎么造」；本模块回答「大脑怎么养成」。Kimi-VL 的语言侧**不是从零训练**的——它直接继承了 Moonlight（月之暗面的 MoE 纯语言模型，架构类似 DeepSeek-V3）预训练到一半的检查点。这一「接力」策略的含义是：文本能力花大钱练一次，多模态能力在成熟底座上加练。

报告把检查点之后的预训练分为 **4 个阶段、共 4.4T token**：先独立练 ViT，再三个联合阶段（联合预训练、冷却、长上下文激活）。「联合（joint）」是关键词——**凡是更新语言模型的阶段都是图文混训**，纯文本数据始终在场，防止模型学了看图忘了说话。

#### 4.2.2 核心流程

```text
Moonlight 中间检查点（已吃 5.2T 纯文本 token，8K 上下文）
   │
   ├─ 阶段 A：ViT 独立训练（2.1T token）
   │    A1 CoCa 式训练 2T：L = L_siglip + 2·L_caption（§4.1.3(d)）
   │    A2 对齐训练 0.1T：只更新 MoonViT + MLP 投影层
   │       → 降低视觉嵌入进 LM 时的初始困惑度，给联合训练热身
   │
   ├─ 阶段 B：联合预训练（1.4T token）
   │    纯文本（与 Moonlight 同分布）+ 多模态数据
   │    开局纯语言 → 多模态占比逐步提高
   │
   ├─ 阶段 C：联合冷却（token 量见 Table 1，正文未单独给出）
   │    高质量语言/多模态数据；数学/知识/代码域用
   │    「合成 QA + 拒绝采样」提纯；QA 占比压低防过拟合
   │
   └─ 阶段 D：联合长上下文激活（两个子阶段，各扩 4 倍）
        8K ──×4──→ 32K ──×4──→ 128K
        RoPE 逆频率（rope_theta）：50,000 ──→ 800,000
        数据配方：25% 长数据（上采样）+ 75% 短数据回放
        长数据 = 长文本 + 长交错图文 + 长视频 + 长文档
        验收：文本/视频「大海捞针」（NIAH）全部通过（Table 2）
```

两个容易忽略的精妙处：

- **为什么 25% 长数据就够？** 长序列训练极贵，报告的探索结论是 1:3 的长短搭配既能学会长上下文，又不丢短上下文能力——纯长数据训练反而会让模型「偏科」。
- **为什么 RoPE 逆频率要重置为 800,000？** RoPE 的基础频率（theta）决定位置编码的「波长表」；上下文拉长 16 倍，波长表不重排的话远距离位置会混叠。u3-l1 已在模型配置里确认 `rope_theta = 800000`，本模块找到了它的出处：§2.3 长上下文激活阶段。

训练侧栏（§2.2、§2.5，了解即可）：全程使用**增强版 Muon 优化器**（加权重衰减、逐参数更新尺度调整，按 ZeRO-1 策略分布式实现）统一优化视觉编码器、投影层、语言模型全部参数；基础设施采用 **4D 并行**（数据 DP / 专家 EP / 流水线 PP / 上下文 CP）+ ZeRO1 + 选择性激活重计算，优化后训练吞吐比 7B 稠密 VLM 高约 60%。

#### 4.2.3 源码精读

**（a）接力起点的原文**（[Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) §2.1「MoE Language Model」小节）：

> The language model of Kimi-VL utilizes our Moonlight model ... with 2.8B activated parameters, 16B total parameters ... For our implementation, we initialize from an intermediate checkpoint in Moonlight's pre-training stage—one that has processed 5.2T tokens of pure text data and activated an 8192-token (8K) context length. We then continue pre-training it using a joint recipe of multimodal and text-only data totaling 2.3T tokens.

（语言模型用 Moonlight：2.8B 激活、16B 总参；从其预训练中间检查点初始化——该检查点已处理 5.2T 纯文本、激活 8K 上下文——随后以图文联合配方续训 2.3T token。）

**（b）四阶段总账的原文**（§2.3 开篇，对应 Figure 4 流程图与 Table 1 阶段明细表）：

> Kimi-VL's pre-training comprises a total of 4 stages consuming 4.4T tokens overall: first, standalone ViT training ... followed by three joint training stages (pre-training, cooldown, and long-context activation) that simultaneously enhance the model's language and multimodal capabilities.

（预训练共 4 阶段、总计 4.4T token：先是独立 ViT 训练，随后三个联合阶段——预训练、冷却、长上下文激活——同步增强语言与多模态能力。）

账本核对：2.1T（ViT）+ 2.3T（联合）= 4.4T ✓。冷却与长上下文激活两段各自的 token 量正文未单独给出，Table 1 有分阶段明细（数据构成、token 量、序列长度、可训练组件四列），**待本地翻表核对**。

**（c）长上下文激活的原文**（§2.3「Joint Long-context Activation Stage」小节）：

> We extend the context length of the model from 8192 (8K) to 131072 (128K), with the inverse frequency of its RoPE embeddings reset from 50,000 to 800,000. The joint long-context stage is conducted in two sub-stages, where each one extends the model's context length by four times. ... we filter and upsample the ratio of long data to 25% in each sub-stage, while using the remaining 75% tokens to replay shorter data.

（上下文从 8K 扩到 128K，RoPE 逆频率由 50,000 重置为 800,000；分两个子阶段各扩 4 倍；每个子阶段把长数据上采样到 25%，其余 75% 回放上一阶段的短数据。）

**（d）与 README 部署声明的映射**：README 说 128K 带来 LongVideoBench 64.5、MMLongBench-Doc 35.1（[README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23)），其训练侧出处就是上面的阶段 D；部署侧对应的旋钮是 `--max-model-len`——README 建议「需要更长上下文时提到 131072」（[README.md:L256-L257](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L256-L257)），这个 131072 正是阶段 D 终点的 128K（131,072 token）。u3-l3 讲过的「视觉 token 约为像素数÷196÷4」也在此闭环：阶段 A–D 造就的长上下文，就是阶段 D 之后模型能装下 4096 视觉 token 高清图的前提。

#### 4.2.4 代码实践：把 Table 1 抄成自己的「训练账本」

1. **实践目标**：用报告正文给出的数字重建四阶段训练表，并与模型配置交叉验证 RoPE 重置，训练流程从「一段叙述」变成「一张可查阅的账本」。
2. **操作步骤**：
   - 打开 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) §2.3，找到 Table 1（Overview of training stages）与 Figure 4，逐行抄录各阶段的数据构成、token 量、序列长度、可训练组件四列；
   - 用正文数字（2T / 0.1T / 1.4T / 合计 2.3T / 合计 4.4T）核对抄录结果，正文没有的格子标注「正文未给出，以 Table 1 为准」；
   - 打开 HuggingFace 模型仓库 `moonshotai/Kimi-VL-A3B-Instruct` 的 `config.json`（**外部资源**，非本仓库文件），搜索 `rope_theta`，确认其值为 `800000`，并在账本上给这一行标注出处「报告 §2.3 ↔ 模型配置」。
3. **需要观察的现象**：正文叙述与 Table 1 是否完全一致；`config.json` 里 `rope_theta` 的实际取值。
4. **预期结果**：账本呈现「ViT 独立（只动视觉侧）→ 三个联合阶段（视觉侧 + 语言侧一起动）」的结构；`rope_theta = 800000` 与报告 50,000 → 800,000 的重置结论吻合（此为外部资源核对，**待本地验证**）。
5. **附加一步（无 GPU 也可做）**：对照 [README.md:L260](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L260) 的 `vllm serve` 命令，把 `--max-model-len 32768` 改成 `131072`，写出「这个数字对应预训练哪个阶段的哪个终点」——答案：阶段 D 的终点 131,072 token。

#### 4.2.5 小练习与答案

**练习 1**：为什么对齐阶段（A2）只更新 MoonViT 和 MLP 投影层、冻结语言模型？
**答案**：目的只是让视觉嵌入「说语言模型听得懂的话」，显著降低视觉 token 进入语言模型时的初始困惑度。语言模型此时已是练好的底座，先不动它，可避免联合训练开局被乱码般的视觉嵌入拖坏文本能力，让阶段 B 平滑起步。

**练习 2**：冷却阶段为什么把 QA 数据占比压得很低？
**答案**：冷却期的 QA（含合成 QA）只为「激活」特定能力、帮助消化高质量数据，不是教模型学对话格式；占比过高会让模型过拟合 QA 模式（把所有输入都当问答题），反而损害通用能力。

**练习 3**：报告 §5（Limitation）承认，尽管有 128K 窗口，长上下文能力仍受什么制约？
**答案**：注意力层参数量只相当于 3B 级模型，面对超长序列、超高信息量场景时仍显不足；模型规模与推理能力未达理论上限也是报告自认的另外两条限制——这也是后续 2506 等版本继续演进的动机。

### 4.3 Thinking 版 SFT 与 RL 路径：长思维链能力从哪里来

#### 4.3.1 概念说明

u2-l2 你已经会用 Thinking 模型（`max_new_tokens=32768`、`temperature=0.8`），u2-l4 知道 Instruct 与 Thinking 规格相同、差异全部来自后训练。本模块打开「后训练」这只黑箱，看长思维链能力被**三段式**造出来：

1. **联合 SFT**：把底座模型变成会对话、会看图答题的 Instruct 模型；
2. **长 CoT SFT**：用一小批「精挑细选的长推理路径」给模型做思维链热身；
3. **强化学习（RL）**：在可判分的问题上大规模训练，让模型自己磨出「会计划、会检查、会回头」的推理策略。

报告用一个词概括这条路径的哲学：Thinking 的能力是**激活（activate）+ 强化（enhance）**出来的，不是凭空注入的——底座预训练时已经在阶段 B–D 见过海量推理素材，后训练只是把潜能拧开。

先澄清一个容易踩的坑（也是本讲「报告 ↔ README 映射」的第一课）：**报告写的是 2025 年 4 月发布的初代模型**。报告 §4.2 的 Kimi-VL-Thinking 对应 README 规格表里**已废弃**的旧版 Thinking（[README.md:L61](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L61)）；2506 版（2025-06-21 发布，见 [README.md:L47](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L47)）的训练细节**不在本报告内**，其成绩出自 README 与 HuggingFace 博客。

#### 4.3.2 核心流程

后训练三段式（对应报告 Figure 5）：

```text
底座（预训练产物）
   │
   ├─ ① 联合 SFT（§2.4 Joint SFT）
   │    数据：纯文本对话 + 多模态指令（§3.2，文本:图像 token ≈ 1:1）
   │    格式：ChatML；只监督答案与特殊 token，system/user 提示全部掩码
   │    日程：32K 序列 1 epoch → 128K 序列 1 epoch
   │    学习率：2e-5 → 2e-6，重升温到 1e-5 → 衰减到 1e-6
   │    训练对象：语言模型 + MLP 投影层 + 视觉编码器（全量）
   │    产物：Kimi-VL（Instruct）
   │
   ├─ ② 长 CoT SFT（§2.4 Long-CoT SFT）
   │    数据：小而精的热身集；提示工程生成 + 拒绝采样式校验（§3.3）
   │         采样器：Kimi k1.5（更强的私有长 CoT 模型）
   │    认知四要素：规划 planning / 评估 evaluation /
   │               反思 reflection / 探索 exploration
   │    轻量 SFT → 让模型「内化」这四种推理动作
   │
   └─ ③ 强化学习（§2.4 Reinforcement Learning）
        算法：在线策略镜像下降变体（承自 Kimi k1.5）
        目标：见下方公式 (1)
        奖励：0/1 判分奖励模型 + 长度惩罚（治 overthinking）
        采样：课程采样（按难度）+ 优先级采样（按单题成功率）
        产物：Kimi-VL-Thinking（推理时仍是普通自回归生成）
```

RL 目标函数（报告公式 1）：

\[
\max_{\theta}\; \mathbb{E}_{(x,y^{*})\sim\mathcal{D}}
\Big[\;\mathbb{E}_{(y,z)\sim\pi_{\theta}}\big[r(x,y,y^{*})\big]
\;-\;\tau\,\mathrm{KL}\big(\pi_{\theta}(x)\,\|\,\pi_{\theta_i}(x)\big)\Big]
\]

逐项拆解：\(x\) 是题目（可含图）、\(y^{*}\) 是标准答案；\((y,z)\) 是模型当前策略 \(\pi_\theta\) 采样出的「答案 + 思维链」；奖励模型 \(r(x,y,y^{*}) \in \{0,1\}\) 只判对错；KL 项以第 \(i\) 轮的参考策略 \(\pi_{\theta_i}\) 为锚，系数 \(\tau>0\) 控制每步不能跑太远；下一轮以更新后的模型为新锚点，滚动前进。

配套机制各有针对：**长度惩罚**治「想个没完」——报告明确称之为 overthinking（模型生成冗余推理链）；**课程采样**按难度标签排进度，**优先级采样**按单题历史成功率把算力砸在「最有教学价值」的题上（太简单和一直做不出都不划算）；数据侧（§3.3）先攒一批带标准答案的多步推理题（数学、领域 VQA），用 Kimi k1.5 采多条长推理轨迹，奖励模型 + 规则奖励双重过滤，错的思维链整条丢弃。

报告 §4.2 给出两组成绩，正好构成 README 声明的出处：

- **相对底座的提升**：MathVista +2.6、MMMU +4.7、MathVision +15.4（百分点）；终值 71.3 / 61.7 / 36.8。
- **测试时扩展（Figure 13）**：MathVision 上把最大思维 token 从 1k 放到 16k，准确率 18.7% → 36.8% 稳步上升；而 MathVista 在 4k 时已达 70.9%，16k 无显著增益——**不同任务的「有效思维深度」不同**，这就是 u2-l4 那句「生成预算 512 与 32768 相差 64 倍」背后的实验依据。

#### 4.3.3 源码精读

**（a）README 对 Thinking 能力来源的一句话宣言**（[README.md:L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25)）：

> Developed through long chain-of-thought (CoT) supervised fine-tuning (SFT) and reinforcement learning (RL), this model exhibits strong long-horizon reasoning capabilities. It achieves scores of 61.7 on MMMU, 36.8 on MathVision, and 71.3 on MathVista ...

（通过长思维链 SFT 与 RL 开发而来，具备长程推理能力；MMMU 61.7、MathVision 36.8、MathVista 71.3。）——三个数字的完整出处即报告 §4.2 与 Table 4（注意 Table 4 标注的评测口径：MathVista 用 mini、MMMU 用 val、MathVision 用 full）。

**（b）长 CoT SFT 的认知四要素原文**（[Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) §2.4「Long-CoT Supervised Fine-Tuning」小节）：

> The resulting warmup dataset is designed to encapsulate key cognitive processes ... such as planning, where the model systematically outlines steps before execution; evaluation, involving critical assessment of intermediate steps; reflection, enabling the model to reconsider and refine its approach; and exploration, encouraging consideration of alternative solutions.

（热身数据集刻意封装四种关键认知过程：规划——动手前系统列出步骤；评估——对中间步骤批判性审视；反思——重新考虑并修正路线；探索——考虑替代解法。）

**（c）RL 关键机制的原文**（同节「Reinforcement Learning」）：

> ... we adopt a variant of online policy mirror descent as our RL algorithm ... To enhance RL training efficiency, we implement a length-based reward to penalize excessively long responses, mitigating the overthinking problem ... we employ two sampling strategies including curriculum sampling and prioritized sampling, which leverage difficulty labels and per-instance success rates ...

（采用在线策略镜像下降变体做 RL；实施基于长度的奖励惩罚过长回答以缓解 overthinking；采用课程采样与优先级采样，利用难度标签与单实例成功率聚焦最有教学价值的样本。）

以及推理形态的重要结论（同节）：模型最终**保持标准自回归生成**——不需要在部署侧引入并行搜索等特殊规划算法；同时发展出错误检测、回溯、迭代精化等元推理能力，把「探索过的完整推理史」当作上下文利用。

**（d）README 声明与报告结论的映射表**（本讲的综合映射练习，数字口径务必看清）：

| README 声明 | 位置 | 报告出处 | 备注 |
| :--- | :--- | :--- | :--- |
| Thinking = 长 CoT SFT + RL；61.7 / 36.8 / 71.3 | [README.md:L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25) | §4.2、Table 4 | 完全一致 |
| Thinking 推荐 `Temperature = 0.8` | [README.md:L67](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L67) | 报告未直接给出 | 工程口径，报告只讲训练 |
| `max_new_tokens=32768` 生成预算 | [README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193) | Figure 13（16k 已饱和主流基准） | 32768 是安全余量口径 |
| 2506 版 MathVision 56.9（+20.1）等 | [README.md:L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29) | **无**（报告早于 2506 发布） | 出处是 README/HF 博客 |
| 初代 Thinking 已 deprecated | [README.md:L61](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L61) | 报告研究的正是这一版 | 名称指代要分清 |

一个批判性阅读的发现：README 的增量括号并非都与报告基线严丝合扣——MathVision 36.8 + 20.1 = 56.9 精确吻合，但 MathVista 71.3 + 8.4 = 79.7 ≠ 80.1、MMMU 61.7 + 2.1 = 63.8 ≠ 64.0。原因正是评测口径差异（Table 4 的 mini/val 子集与 2506 重新评测的口径不完全相同）。**读报告要养成的习惯：数字必查「表注」，口径不同不可直减。**

#### 4.3.4 代码实践：解剖一条真实思维链的「认知四要素」

1. **实践目标**：把报告 §2.4 的抽象概念（planning/evaluation/reflection/exploration）落到肉眼可见的生成文本上，验证「SFT 热身集封装的四种认知过程」确实体现在模型输出里。
2. **操作步骤**（两条路线任选，A 需 GPU，B 零算力）：
   - **路线 A（GPU）**：运行 u2-l2 讲过的 Thinking-2506 多图示例（对 `figures/demo1.png` 与 `figures/demo2.png` 问「这是谁的手稿、记录了什么」，提示语见 [README.md:L188](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L188)），拿到完整输出后打印存档；
   - **路线 B（零算力）**：直接读报告 §2.4 之后的 **Figure 6**（Manuscript reasoning visualization：模型逐步分析手稿、依据笔迹/内容/语言线索判定为爱因斯坦的手稿、内容与引力场方程相关的完整案例展示图）；
   - 两条路线共用最后一步：在输出文本中**用四种颜色/四种标记**分别标出 planning（「我先……再……」式列步骤）、evaluation（对中间结论的检验）、reflection（「等等，重新考虑……」式修正）、exploration（「另一种可能是……」式分支），统计各自出现的次数。
3. **需要观察的现象**：思维链不是线性流水，而是「计划 → 执行 → 检查 → 必要时回头」的循环结构；四种要素是否都出现。
4. **预期结果**：一条典型长思维链里四种要素多数可见（Figure 6 的爱因斯坦手稿案例即为官方示例）；路线 A 的具体输出与计数**待本地验证**。
5. **延伸（可选，GPU）**：把 `max_new_tokens` 依次设为 1024、4096、16384 重跑同一道数学题，记录每档的正确与否——亲手复现 Figure 13 的「测试时扩展」现象（难题受益于更长预算、简单题早饱和）。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：为什么长 CoT SFT 的数据集要「小而精」，而不是越大越好？
**答案**：它的定位是**热身（warmup）**——只负责让模型初步内化四种认知过程，真正的推理能力靠后续 RL 大规模磨出来。热身集经过提示工程生成 + 拒绝采样式校验（错路径整条丢弃），质量优先；盲目扩大反而容易让模型死记推理模板（过拟合套路），损害 RL 阶段的探索空间。

**练习 2**：RL 阶段的长度惩罚与 u2-l2 讲的「2506 版平均思维长度缩短 20%」有什么内在联系？
**答案**：长度惩罚在训练时直接压制冗余推理（overthinking），是「用更少 token 想得更对」这一能力的训练侧来源；2506 版「推理精度提升 + 平均思维长度降 20%」（[README.md:L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29)）正是这一方向持续迭代的产品侧表现。注意 2506 的训练细节不在本报告中。

**练习 3**：Figure 13 显示 MathVision 从 1k→16k 涨了约 18 个百分点，MathVista 却在 4k 就饱和。这对你实际调用 Thinking 模型设 `max_new_tokens` 有什么指导？
**答案**：按任务难度分级给预算——感知/常规题（偏 MathVista 型）4k 左右即饱和，给 32k 只会增加时延与费用；竞赛级难题（偏 MathVision 型）才值得 16k 以上预算。盲目拉满 `max_new_tokens=32768` 是拿成本换不到收益。

## 5. 综合实践

**任务：写一篇千字精读笔记 + 一张 Thinking 训练流程图**（本讲规格中指定的实践任务）。

1. **实践目标**：把本讲三个模块的知识压进你自己的语言，产出两件可长期复用的学习资产。
2. **操作步骤**：
   - **第一步（读）**：打开 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf)，按顺序精读 §2.1「MoonViT」小节、§2.3 全节、§2.4 全节；对照 [figures/arch.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/arch.png) 与报告 Figure 4、Figure 5 两张流程图。章节页码以你手上的 PDF 实际排版为准（不同版本页码有差异，**以章节号定位为准，页码待本地确认**）。
   - **第二步（写千字笔记）**：核心命题是「**原生分辨率编码为什么既省算力又看得清**」。必须覆盖：与 resize / 子图切分两条老路的对比；NaViT 打包如何让算力随 token 数伸缩；双重位置编码各管什么；pixel shuffle 的四倍压缩与「看得清 ↔ 装得下」的权衡；用你自己算的 demo.png（≈1105 token）与 1792×1792（≈4096 token）数字佐证。
   - **第三步（画流程图）**：整理「Thinking 模型从 SFT 到 RL 的训练流程图」，从预训练底座画到 Kimi-VL-Thinking，至少标注：联合 SFT 的两段序列长度（32K/128K）与掩码策略；长 CoT SFT 的认知四要素与数据来源（Kimi k1.5 + 拒绝采样）；RL 的算法（在线策略镜像下降变体）、0/1 奖励、KL 锚定、长度惩罚、双采样策略；以及「推理时仍是标准自回归」这个终点。
   - **第四步（批判性加分项）**：在报告中找一处「正文与表格数字不一致」并讨论以谁为准——例如 §4.1.6 正文写 MMLongBench-Doc 为 34.7%，而 Table 3 与 [README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23) 均为 35.1。结论：以表格为准（表格是评测脚本产出），但引用时要注明版本——这是读一切技术报告的通用素养。
3. **需要观察的现象**：写笔记时若发现哪个论证环节「抄得出原文却说不出自己的话」，说明该处理解有洞，回到对应小节重读。
4. **预期结果**：千字笔记能脱离报告独立讲清模块 4.1 的核心权衡；流程图能让人一眼看懂 Thinking 能力的三段式来源，且每个箭头都标了出处（章节号或图表号）。

## 6. 本讲小结

- **MoonViT = 原生分辨率 + NaViT 打包 + 双重位置编码 + pixel shuffle**：切 14×14 patch 后按图打包成变长 1D 序列，与 LLM 共用变长注意力算子；插值绝对嵌入继承 SigLIP 能力、2D RoPE 补高分辨率精度；视觉 token \(\approx \lceil W/14\rceil\lceil H/14\rceil/4\)——算力随图片大小伸缩，高清图不被压、小图不被撑。
- **预训练是「接力 + 四阶段」**：从 Moonlight 中间检查点（5.2T 纯文本、8K 上下文）出发，ViT 独立训练（CoCa 式双损失 \(\lambda=2\)，2T + 0.1T 对齐）→ 联合预训练（1.4T）→ 冷却（高质量 + 合成 QA 提纯）→ 长上下文激活（两子阶段各扩 4 倍，RoPE theta 50,000→800,000，25% 长数据 + 75% 回放），总计 4.4T token。
- **Thinking 的三段后训练**：联合 SFT（32K/128K 各一 epoch，只监督答案与特殊 token）→ 长 CoT SFT（小而精热身集，封装规划/评估/反思/探索四种认知过程）→ RL（在线策略镜像下降变体 + 0/1 奖励 + KL 锚定 + 长度惩罚 + 课程/优先级采样），推理时仍是标准自回归。
- **报告 ↔ README 要做口径对齐**：报告覆盖 2025-04 的初代模型（其 Thinking 即现已废弃的旧版）；2506 的成绩与训练细节不在报告中；引用数字必查表注（mini/val/full 子集口径）。
- **测试时扩展有任务差异**：MathVision 1k→16k 准确率 18.7%→36.8% 持续上升，MathVista 4k 即饱和——生成预算应按任务难度分级设置。

## 7. 下一步学习建议

- **下一讲 u4-l2（基准评测与性能分析）**将系统整理 README 出现的全部基准（MMMU、MathVista、MathVision、InfoVQA、ScreenSpot-Pro、OSWorld 等）——本讲你已经掌握了它们在报告中的位置（Table 3/4 与 §4.1 七个分域小节），下一讲把「读得懂单点」升级为「画得出全景、做得出选型」。
- **继续阅读源码**：报告 §3（Data Construction）本讲只顺带提及，值得单独精读——六类预训练数据（caption/interleaving/OCR/knowledge/agent/video）与 §3.2 指令数据、§3.3 推理数据的构造流水线，是理解「能力从数据来」的最好教材。
- **交叉验证习惯**：把本讲的报告结论与 HuggingFace 模型仓库的 `config.json`、`preprocessor_config.json` 逐项对照（`rope_theta`、`patch_size`、`max_pixels` 等），体会「论文 ↔ 配置 ↔ 代码」三层证据的互证方法——这也是未来读任何模型发布仓库的通用套路。
