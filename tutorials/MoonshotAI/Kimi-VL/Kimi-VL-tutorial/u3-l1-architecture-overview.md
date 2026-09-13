# 架构解析：MoE 语言解码器 + MoonViT 视觉编码器 + MLP 投影层

## 1. 本讲目标

学完本讲，你应该能够：

1. **说出 Kimi-VL 的三段式结构**：MoonViT 视觉编码器 → MLP 投影层 → MoE 语言解码器，并描述图像与文本两条数据路径在哪里汇合。
2. **解释 MoonViT 的原生分辨率设计**：它如何摆脱「缩放到固定正方形再切块」的传统做法，直接吃任意分辨率的图，并理解这套设计在算力与清晰度上的双重收益。
3. **理解 MoE 稀疏激活**：为什么 Kimi-VL 总参数 16B，每个 token 却只激活约 2.8B（A3B 名字的由来），以及「专家 + 路由器」是如何做到这一点的。
4. **知道 128K 长上下文从哪来**：它不是训练第一天就有的，而是预训练末段专门「扩展」出来的。

本讲是第 3 单元的第一讲。前面两个单元你已经会「跑」模型了；从本讲开始，我们要打开机器盖子，看看跑起来的到底是什么。

## 2. 前置知识

本讲假设你已完成 u1-l3（仓库结构）与 u2 系列（推理实践）。在此基础上，补充几个读架构图必需的概念：

- **Token（词元）**：模型处理信息的最小单位。文本会被切分成 token；图像也会被转换成一串 token。对多模态模型来说，图像 token 和文本 token 最终会拼成**同一条序列**送进语言模型——这是理解本讲的关键。
- **ViT（Vision Transformer，视觉 Transformer）**：把图像切块（patch）、展平、当作「视觉单词」序列处理的经典架构。传统 ViT 要求输入是固定尺寸（如 224×224），这是 MoonViT 要推翻的约束。
- **Patch（图块）**：把图片按固定边长（Kimi-VL 中是 14×14 像素）切成的小方块，每个 patch 对应一个视觉 token。
- **MoE（Mixture-of-Experts，混合专家）**：把 Transformer 层里那个单一的前馈网络（FFN）替换成多份「专家」网络，配一个「路由器」按需挑选少数几个专家为当前 token 计算。**总参数可以很大，但每个 token 实际动用的参数很小**——这就是「稀疏激活」。
- **投影层（Projector）**：视觉编码器输出的特征向量和语言模型的词嵌入空间维度不同、语义也不同，需要一个「翻译官」把视觉特征映射到语言空间。Kimi-VL 用的是一个两层 MLP。
- **RoPE（旋转位置编码）**：一种把「位置」信息编进注意力计算的方法。本讲会在 128K 扩展处遇到它的一个参数 `rope_theta`（基础频率），先混个眼熟即可。
- **证据的三个来源**：本仓库是发布型仓库（u1-l3 已确认），不含模型实现代码。因此本讲的架构证据来自三处：① 本仓库 [README.md](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md) 的架构章节与推理代码；② 本仓库的技术报告 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf)（即 arXiv:2504.07491）第 2.1 节；③ HuggingFace 模型仓库的 `config.json`（外部资源，下文会明确标注）。凡是推算出来的结论，都会注明。

## 3. 本讲源码地图

| 文件 | 位置 | 在本讲中的作用 |
| :--- | :--- | :--- |
| `README.md` | 本仓库 | 架构官方宣言（§2 Architecture）、模型规格表（§4）、推理代码中两条输入通道的汇合证据（§6） |
| `figures/arch.png` | 本仓库 | 官方架构图，本讲的「地图」，综合实践要对着它手绘数据流 |
| `Kimi-VL.pdf` | 本仓库 | 技术报告 §2.1（Model Architecture）与 §2.3（长上下文激活），架构细节的第一手出处 |
| HuggingFace `moonshotai/Kimi-VL-A3B-Instruct` 的 `config.json` | [外部链接](https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct/raw/main/config.json) | 模型的真实结构数值（层数、专家数、patch 大小等），**注意这是外部资源，不是本仓库文件** |

提醒：u1-l3 已经确认，模型实现代码（`modeling_kimi_vl.py` 等）托管在 HuggingFace 模型仓库，靠 `trust_remote_code=True` 动态加载。本讲做「架构层」的解析，还不需要逐行读实现代码。

## 4. 核心概念与源码讲解

### 4.1 三大组件与数据流

#### 4.1.1 概念说明

README 的架构章节只有一句话，但这句话就是整个模型的骨架：

> The model adopts an MoE language model, a native-resolution visual encoder (MoonViT), and an MLP projector.

—— [README.md:L39](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L39)

翻译成结构图，Kimi-VL 由三段组成：

```text
图像输入 ──→ ① MoonViT 视觉编码器 ──→ ② MLP 投影层 ──┐
                                                      ├──→ ③ MoE 语言解码器 ──→ 文本输出
文本输入 ──→ (tokenizer 分词为 token) ────────────────┘
```

三个组件的分工：

1. **MoonViT（视觉编码器）**：把一张任意分辨率的图变成一串视觉特征向量。它决定了模型「看得清不清」。
2. **MLP 投影层（翻译官）**：把视觉特征对齐到语言模型的嵌入空间，让视觉 token「说语言模型听得懂的话」。
3. **MoE 语言解码器（大脑）**：接收「视觉 token + 文本 token」拼成的完整序列，自回归地生成回答。它决定了模型「想得深不深」，也是 16B/2.8B 参数账本的主战场。

**汇合点**：图像路径与文本路径在进入 MoE 解码器**之前**就合并了——视觉 token 序列会被插入到文本序列中图像占位符所在的位置（u2-l3 讲过：`messages` 里 `{"type": "image"}` 分片只是占位符，processor 会按视觉 token 数展开它），两者拼成一条统一的 token 序列后再喂给解码器。解码器本身并不区分「这是图像 token 还是文本 token」，它眼里只有一条序列。

#### 4.1.2 核心流程

一次多模态推理的完整数据流（伪代码）：

```text
输入: 图片 I（任意分辨率 W×H），问题文本 Q

# ── 图像路径 ──
I ──切 14×14 patch──→ patches（数量 ≈ (W/14)×(H/14)）
patches ──MoonViT(27 层 Transformer)──→ 视觉特征序列 F_v
F_v ──pixel shuffle 2×2 压缩──→ 特征数变为约 1/4
     ──两层 MLP 投影──→ 视觉 token 序列 T_v（维度已对齐语言嵌入空间）

# ── 文本路径 ──
Q + 聊天模板 ──tokenizer──→ 文本 token 序列 T_t（含图像占位符）

# ── 汇合 ──
T_t 中的图像占位符 ──被 T_v 展开/替换──→ 混合序列 T = [系统/用户文本 token, 视觉 token, ...]

# ── 解码 ──
T ──MoE 语言解码器（每 token 激活约 2.8B 参数）──→ 逐 token 生成回答
```

三条只在本讲先建立的直觉：

- **图像是「插入」文本的**：图片不是模型的第二个输入口，而是变成 token 嵌进同一条序列。
- **视觉 token 数量由图片分辨率决定**（4.2 节展开）：这是原生分辨率设计的直接后果。
- **解码器是「参数仓库 + 按需取用」**（4.3 节展开）：16B 参数躺在仓库里，每个 token 只取 2.8B 来算。

#### 4.1.3 源码精读

**证据一：README 架构章节与官方架构图。**

[README.md:L37-L43](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L37-L43) 是完整的 `## 2. Architecture` 章节——全节只有一句话加一张图 `figures/arch.png`：

```markdown
## 2. Architecture

The model adopts an MoE language model, a native-resolution visual encoder
(MoonViT), and an MLP projector, as illustrated in the following image.
```

这句话点名了三段式结构，[figures/arch.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/arch.png) 就是官方架构图（技术报告中的 Figure 3，报告给它的图注是「由允许原生分辨率图像的 MoonViT、一个 MLP 投影层和一个 MoE 语言解码器组成」）。请打开图对照本节的伪代码走一遍，图中左右两半正对应图像路径与语言解码路径。图中更细的标注（如解码器内部的注意力与专家结构）留到综合实践里由你自己核对补充。

**证据二：规格表确认三个组件的参数账。**

[README.md:L57-L62](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L57-L62) 的模型变体表列出：三个变体全部是 **16B 总参数 / 3B 激活参数 / 128K 上下文**——注意表格里的「3B」是营销口径的约数，README 引言处写的是精确些的 2.8B：

[README.md:L15](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15) 说明模型是「activating only **2.8B** parameters in its language decoder (Kimi-VL-A3B)」——即 **2.8B 指的是语言解码器部分**，视觉编码器另有约 0.4B（400M，见技术报告 §1 的「2.8B activated (16B total) parameters, paired with a 400M native-resolution MoonViT」以及报告 Table 3 中 Kimi-VL-A3B 一行的 `# Act. Params (LLM+VT) = 2.8B+0.4B`）。合起来约 3.2B 激活，这就是「A3B」名字的由来。

**证据三：推理代码里藏着数据流。**

你在 u2-l1 跑过的官方示例，其实就是三段式架构的「用户视角投影」。看 [README.md:L139-L146](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L139-L146)：

```python
image_path = "./figures/demo.png"
image = Image.open(image_path)
messages = [
    {"role": "user", "content": [{"type": "image", "image": image_path}, {"type": "text", "text": "What is the dome building in the picture? Think step by step."}]}
]
text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
inputs = processor(images=image, text=text, return_tensors="pt", padding=True, truncation=True).to(model.device)
generated_ids = model.generate(**inputs, max_new_tokens=512)
```

逐行对应到架构：

| 代码 | 对应架构环节 |
| :--- | :--- |
| `Image.open(image_path)` | 拿到**原始分辨率**的 PIL 图，没有任何 resize——原生分辨率从这里开始 |
| `messages` 里的 `{"type": "image", ...}` | 文本路径中的图像**占位符**（汇合的锚点） |
| `processor(images=image, text=text, ...)` | 一次调用同时走两条路径：内部调 MoonViT 的图像预处理产出 `pixel_values`，调 tokenizer 产出 `input_ids`（u2-l3 已验证） |
| `model.generate(**inputs, ...)` | 汇合后的混合序列进入 MoE 解码器，自回归生成 |

也就是说：**processor 是三段式前两段（编码 + 对齐）的入口包装，model.generate 是第三段的入口**。你 everyday 使用的两行代码，正好落在架构图的分界线两侧。

#### 4.1.4 代码实践

**实践：用 processor「称重」一张图——数一数图像变成了多少 token**

1. **实践目标**：不加载模型权重（无需 GPU），仅用 processor 亲眼看「一张图变成一串 token、且 token 数随分辨率变化」，验证图像路径与文本路径的汇合。
2. **操作步骤**（承接 u2-l3 的「processor-only」打法，以下为示例代码，需在 u1-l2 搭建的环境运行）：

```python
# 示例代码：仅加载 processor，不加载模型权重
from PIL import Image
from transformers import AutoProcessor

model_path = "moonshotai/Kimi-VL-A3B-Instruct"
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

image = Image.open("./figures/demo.png")
messages = [
    {"role": "user", "content": [
        {"type": "image", "image": "./figures/demo.png"},
        {"type": "text", "text": "What is this?"},
    ]}
]
text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
inputs = processor(images=image, text=text, return_tensors="pt", padding=True, truncation=True)

print("input_ids 形状:", inputs.input_ids.shape)      # 序列总长 = 文本 token + 视觉 token
print("pixel_values 形状:", inputs.pixel_values.shape)  # 图像路径的真实像素张量

# 再把同一张图缩小到一半，重复上面流程，对比 input_ids 长度变化
```

3. **需要观察的现象**：`input_ids` 的长度远大于那句英文问题本身的 token 数——多出来的就是视觉 token；把图片缩小后重复实验，`input_ids` 长度应明显变短。
4. **预期结果**：图片分辨率越高，混合序列越长（像素数与视觉 token 数近似成正比，u2-l4 已建立此结论，本实践是亲手验证）。若把 `{"type": "image"}` 分片删掉，流程应报错或行为异常，说明占位符是汇合的必要锚点。
5. **待本地验证**：具体数字取决于 `demo.png` 的真实尺寸与 tokenizer 版本，作者环境未运行，结果请以本地输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把图像特征和文本 token 分成两个输入口，分别喂给模型？
**参考答案**：因为 Transformer 解码器的核心机制（自回归生成 + 统一注意力）天然处理**单一序列**：任何 token 都能注意到序列中所有前文 token。把视觉 token 嵌入同一条序列，语言模型就能用同一套注意力机制「读到」图像内容，无需为多模态另造一套架构。代价是视觉 token 与文本 token 共享同一个 128K 上下文预算（u2-l4 讲过）。

**练习 2**：`processor(images=image, text=text)` 的两个参数分别对应数据流中的哪一段？
**参考答案**：`text` 对应文本路径（经聊天模板渲染后分词为 `input_ids`，其中含图像占位符）；`images` 对应图像路径的起点（产出 `pixel_values`，供 MoonViT 编码）。processor 内部完成两条路径的编码与占位符展开——即「汇合」发生在这里。

**练习 3**：README 表格写 3B 激活参数，引言又写 2.8B，矛盾吗？
**参考答案**：不矛盾。2.8B 指语言解码器的激活参数，另有约 0.4B 的 MoonViT 视觉编码器（技术报告 Table 3 标注为 2.8B+0.4B），合计约 3.2B，规格表取约数写成 3B；「A3B」即「Active 约 3B」。

### 4.2 MoonViT 原生分辨率设计

#### 4.2.1 概念说明

**问题**：传统视觉编码器（如早期 LLaVA 用的 CLIP-ViT）要求输入必须是固定尺寸正方形，比如 448×448。一张 1920×1080 的截图送进去，先要粗暴缩放/裁剪——结果要么小字糊掉，要么画面缺角。高分辨率文档、4K 屏幕截图这类任务（恰恰是 OCR 和 Agent 场景）最受伤。

一些模型用「切子图」补救：把大图切成多块、分别编码再拼接（如 LLaVA-OneVision）。缺点是流程复杂，且块与块之间的全局关系被打散。

**MoonViT 的答案：原生分辨率（native resolution）**——图是什么分辨率就按什么分辨率处理，不缩放、不切图。技术报告 §2.1 的原话是：这样设计「消除了复杂的子图切分与拼接操作的需要」。README 则给出了这种设计换来的成绩单：

> Its native-resolution vision encoder, MoonViT, further allows it to see and understand ultra-high-resolution visual inputs, achieving 83.2 on InfoVQA and 34.5 on ScreenSpot-Pro, while maintaining lower computational cost with common visual inputs and general tasks.

—— [README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23)

注意最后半句的巧妙之处：**低分辨率的小图不会被拉高浪费算力**。固定分辨率模型对一张 200×200 的小图也要填满 448×448 的格子；MoonViT 则是小图少算、大图多算，计算量与内容复杂度匹配。而 2506 版本把单图上限提到 3.2M 像素（1792×1792，初代 4 倍）：

> The new 2506 version supports 3.2 million total pixels in a single image (1792x1792), 4X compared to the original release.

—— [README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32)

#### 4.2.2 核心流程

MoonViT 让「任意分辨率」变得可行的三件技术武器（出处：技术报告 §2.1，均可在 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) 第 2.1 节查到）：

1. **Patch n' Pack（来自 NaViT 的打包法）**：图片切成 patch 后**展平成一维序列**，多张/多种分辨率的 patch 序列可以直接打包进同一个批次。由于变成了 1D 序列，MoonViT 可以直接复用语言模型那套**变长序列注意力**（FlashAttention 支持），训练吞吐不受分辨率参差的影响。这是「视觉编码器像语言模型一样算」的关键一步。
2. **双位置编码**：MoonViT 从 SigLIP-SO-400M 初始化并继续预训练。SigLIP 原本用**可学习的固定尺寸绝对位置嵌入**，MoonViT 将其插值保留（继承旧能力），但它在高分辨率下不够用，于是又叠加了**2D RoPE（沿高、宽两个方向的旋转位置编码）**来编码细粒度空间位置。两套位置编码配合展平打包，使同一批次内不同分辨率的图都能正确编码。
3. **像素汇总压缩（在投影层完成，见下）**：视觉特征在送入语言模型前做 2×2 空间下采样，把视觉 token 数压到约四分之一。

**算一笔 token 帐**（示例推算，依据是 HuggingFace 模型仓库 `config.json` 中的真实数值，见 4.2.3）：

一张 1792×1792 的图（2506 版上限）：

\[ N_{\text{patch}} = \frac{1792}{14} \times \frac{1792}{14} = 128 \times 128 = 16384 \]

经 2×2 pixel shuffle 压缩后：

\[ N_{\text{视觉 token}} = \frac{16384}{2 \times 2} = 4096 \]

也就是说，**一张上限分辨率的图约占 4096 个视觉 token**，它们全部计入 128K 共享上下文（131,072）。而一张 672×448 的普通图约为 \(48 \times 32 / 4 = 384\) 个 token——小图省算力、大图保清晰，账面立刻可见。这也解释了 u2-l4 遗留的问题：为什么分辨率提升必须依托长上下文才能兑现。

#### 4.2.3 源码精读

本仓库中 MoonViT 的「源码」以配置形式存在于 HuggingFace 模型仓库。以下 JSON 片段摘自 [moonshotai/Kimi-VL-A3B-Instruct 的 config.json](https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct/raw/main/config.json)（**外部资源，非本仓库文件；已实际访问核对**）的 `vision_config` 部分：

```json
"vision_config": {
    "model_type": "moonvit",
    "patch_size": 14,
    "num_attention_heads": 16,
    "num_hidden_layers": 27,
    "hidden_size": 1152,
    "intermediate_size": 4304,
    "init_pos_emb_height": 64,
    "init_pos_emb_width": 64,
    "merge_kernel_size": [2, 2],
    "torch_dtype": "bfloat16"
}
```

逐项对应本讲概念：

| 配置项 | 数值 | 含义对应 |
| :--- | :--- | :--- |
| `model_type` | `moonvit` | 视觉塔有自己的模型类型，与文本塔分离 |
| `patch_size` | 14 | 4.2.2 的 patch 切块边长，token 帐的「汇率」 |
| `num_hidden_layers` | 27 | MoonViT 共 27 层 Transformer |
| `hidden_size` / `intermediate_size` | 1152 / 4304 | 特征维度与 FFN 中间维度——这是 SO-400M 量级的典型配置 |
| `init_pos_emb_height/width` | 64 / 64 | 初始化时绝对位置嵌入对应 64×64 的 patch 网格（64×14=896，即 SigLIP-SO-400M 的 896×896 原始输入），印证「从 SigLIP 初始化 + 位置嵌入插值」的说法 |
| `merge_kernel_size` | [2, 2] | 4.2.2 的 2×2 空间合并（pixel shuffle），把视觉 token 压到 1/4 |

再对照仓库内的两处文字证据：

- 技术报告 §2.1「MoonViT: A Native-resolution Vision Encoder」：确认 NaViT packing、SigLIP-SO-400M 初始化与继续预训练、2D RoPE，并说明 MoonViT 约 400M 参数（报告 §1）。报告未公开 MoonViT 的逐层配置表，上表的 27 层 / 1152 维等数值以 `config.json` 为准——**这正体现了「发布型仓库 + 模型仓库配置」组合的读法：论文讲设计思想，config 给工程数值**。
- README 的推理示例中，图像从头到尾保持原始尺寸（u2-l1 精读过的 [README.md:L139-L145](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L139-L145) 没有 resize/crop 调用），原生分辨率一直延伸到 processor 内部。

#### 4.2.4 代码实践

**实践：三张不同宽高比的图，验证「原生分辨率不吃宽高比」**

1. **实践目标**：直观感受「原生分辨率 + Patch n' Pack」与传统固定分辨率编码的差异。
2. **操作步骤**（示例代码，接 4.1.4 的 processor 环境）：

```python
# 示例代码：构造三种极端宽高比的同图，对比视觉 token 数
from PIL import Image

for size in [(1792, 1792), (1792, 896), (896, 448)]:
    img = Image.new("RGB", size, color=(128, 128, 128))
    messages = [{"role": "user", "content": [
        {"type": "image", "image": "placeholder"},
        {"type": "text", "text": "hi"},
    ]}]
    text = processor.apply_chat_template(messages, add_generation_prompt=True)
    inputs = processor(images=img, text=text, return_tensors="pt")
    print(size, "→ input_ids 长度:", inputs.input_ids.shape[-1])
```

3. **需要观察的现象**：三个尺寸的 `input_ids` 长度不同，且比例大致对应像素数之比（16:8:2）；正方形与宽条形都不会被拉伸成正方形再编码。
4. **预期结果**：按 4.2.2 的公式推算，三者视觉 token 应分别约为 4096、2048、512（加上少量文本 token）。若某个尺寸被拒绝或长度与像素数不成比例，说明 processor 内部有额外的限制逻辑（如对超限图片的缩放兜底）。
5. **待本地验证**：processor 内部对超分辨率图是否有自动缩放兜底，本仓库文档未说明，请以本地实测为准。

#### 4.2.5 小练习与答案

**练习 1**：固定分辨率编码器处理一张 4K 截图（3840×2160）通常会发生什么？MoonViT 呢？
**参考答案**：固定分辨率编码器必须把 3840×2160 缩放/裁剪到如 448×448，小字直接糊掉，OCR 与 GUI 定位能力受损（ScreenSpot-Pro 就是 4K 专业屏幕定位基准，README 中原生分辨率设计直接对应其 34.5 分）；MoonViT 按原始分辨率切 patch，清晰度保留，代价是占用更多视觉 token 与算力。

**练习 2**：为什么 MoonViT 要「展平成 1D 序列」而不是保持 2D 网格做注意力？
**参考答案**：展平后视觉数据形态与语言 token 完全一致，可以复用语言模型生态里成熟的变长序列算子（如 FlashAttention 的变长注意力），任意分辨率的图能打包进同一批次高效训练；同时配合 2D RoPE，位置信息并不会因为展平而丢失。

**练习 3**：一张 896×896 的图大约产生多少视觉 token？
**参考答案**：\((896/14)^2 / 4 = 64^2 / 4 = 1024\) 个视觉 token（patch 数 4096，经 2×2 合并后为 1024）。

### 4.3 MoE 稀疏激活与效率

#### 4.3.1 概念说明

**问题**：模型能力大致随参数量增长，但每个 token 都要过完所有参数（稠密模型），推理成本随规模线性上涨。想变聪明，就必须变贵吗？

**MoE 的解法**：把 Transformer 层中「一个 FFN」换成「一个 FFN 池子」——N 个结构相同、权重各异的**专家**，再加一个**路由器（gate）**。每个 token 到来时，路由器打分、只选最匹配的 top-k 个专家计算，其余专家对这个 token 而言等于不存在。于是：

- **总参数** = 所有专家的参数之和（决定「知识容量」）；
- **激活参数** = 每 token 实际经过的参数（决定「计算成本」）。

两者解耦，这就是 16B 总参、2.8B 激活的机制基础。Kimi-VL 的语言解码器直接采用自家 Moonlight 系列的 MoE 语言模型（架构类似 DeepSeek-V3），README 引言明确它「activating only 2.8B parameters in its language decoder」（[README.md:L15](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15)），并以这个体量对标 GPT-4o-mini、Qwen2.5-VL-7B、Gemma-3-12B-IT（[README.md:L21](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L21)）。

效率收益是三段式里最硬的：推理时算力约按激活参数走，而模型容量按总参数走——**花 2.8B 的钱，用 16B 的脑子**。

#### 4.3.2 核心流程

一个 token 穿过一层 MoE 解码器（伪代码，依据 `config.json` 的 `text_config` 数值）：

```text
token 隐状态 h (维度 2048)
  ├─→ 注意力模块（16 头，MLA 压缩 KV，RoPE 位置编码）
  └─→ MoE 前馈层：
        路由器给 64 个路由专家打分（sigmoid 评分 + noaux_tc 选择法）
        选出 top-6 个路由专家 → 各自计算后加权求和（缩放系数 2.446）
        同时 2 个共享专家无条件全开（每个 token 都算）
        输出 = 共享专家输出 + 6 个路由专家加权输出
```

用公式表达稀疏激活的输出（示意形式）：

\[ y = \sum_{i \in \text{Top6}} g_i \cdot E_i(h) \cdot s \; + \; \sum_{j=1}^{2} S_j(h) \]

其中 \(g_i\) 是路由权重、\(s\) 是缩放因子、\(E_i\) 是被选中的路由专家、\(S_j\) 是共享专家。每个 token 实际计算的前馈参数 ≈ 6 个路由专家 + 2 个共享专家，而不是全部 64 个。

**每层参数的粗略账**（推算）：`moe_intermediate_size=1408`、`hidden_size=2048` 下，单个专家约 \(3 \times 2048 \times 1408 \approx 8.65\text{M}\) 参数（SwiGLU 结构三份矩阵），64 个路由专家约 554M/层；由于第 1 层是稠密层（见 `first_k_dense_replace=1`），26 个 MoE 层的专家部分合计约 14.4B——占 16B 总参的绝对大头；而每层实际激活的 8 个专家（6 路由 + 2 共享）只动用约 69M/层。这就是 16B 与 2.8B 之间差距的主要来源（注意力与嵌入等贡献其余部分）。

**128K 长上下文的由来**（技术报告 §2.3）：语言解码器并非生来 128K——Moonlight 预训练的中间检查点（已消化 5.2T 纯文本 token）只有 **8K** 上下文；Kimi-VL 在预训练最后的「长上下文激活阶段」用两个子阶段、各扩展 4 倍（8K→32K→128K），同时把 RoPE 的基础频率从 50,000 重置为 **800,000**（对应 `config.json` 里的 `rope_theta: 800000.0`），并以 25% 长数据 + 75% 短数据回放的比例训练，长数据包含长文本、长交错图文、长视频与长文档。README 中的 64.5 LongVideoBench 与 35.1 MMLongBench-Doc（[README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23)）就是这一阶段的回报。

#### 4.3.3 源码精读

**证据一：README 的能力声明与对比。**

[README.md:L15-L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15-L23)：从「only 2.8B parameters in its language decoder」到「effectively competes with cutting-edge efficient VLMs such as GPT-4o-mini, Qwen2.5-VL-7B, and Gemma-3-12B-IT」，再到 128K 上下文与 64.5/35.1 的长上下文成绩——这段引言就是 MoE 稀疏激活的「产品说明书」。

**证据二：`config.json` 的 `text_config`（外部资源，已实际访问核对）。**

摘自 [moonshotai/Kimi-VL-A3B-Instruct 的 config.json](https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct/raw/main/config.json)：

```json
"text_config": {
    "vocab_size": 163840,
    "max_position_embeddings": 131072,
    "hidden_size": 2048,
    "moe_intermediate_size": 1408,
    "num_hidden_layers": 27,
    "n_shared_experts": 2,
    "n_routed_experts": 64,
    "num_experts_per_tok": 6,
    "first_k_dense_replace": 1,
    "topk_method": "noaux_tc",
    "scoring_func": "sigmoid",
    "routed_scaling_factor": 2.446,
    "kv_lora_rank": 512,
    "qk_rope_head_dim": 64,
    "qk_nope_head_dim": 128,
    "v_head_dim": 128,
    "rope_theta": 800000.0,
    "num_attention_heads": 16,
    "hidden_act": "silu",
    "torch_dtype": "bfloat16"
}
```

关键项逐个对应：

| 配置项 | 数值 | 架构含义 |
| :--- | :--- | :--- |
| `n_routed_experts` | 64 | 每个解码层的路由专家池大小 |
| `num_experts_per_tok` | 6 | 每 token 只激活 6 个路由专家（top-6） |
| `n_shared_experts` | 2 | 2 个共享专家无条件参与，承载各 token 的共性知识 |
| `first_k_dense_replace` | 1 | 第 1 层用稠密 FFN，其余 26 层是 MoE 层 |
| `max_position_embeddings` | 131072 | 128K 上下文的最终落地：\(131072 = 128 \times 1024\) |
| `rope_theta` | 800000.0 | 长上下文激活阶段重置后的 RoPE 基础频率，与技术报告 §2.3 的「reset from 50,000 to 800,000」一字不差 |
| `kv_lora_rank` / `qk_*_head_dim` / `v_head_dim` | 512 / 64+128 / 128 | MLA（多头潜在注意力）的低秩压缩配置，DeepSeek-V3 风格的标志性设计，服务于长上下文的 KV 显存效率 |
| `vocab_size` | 163840 | 16 万词表，`media_placeholder_token_id: 163605` 也在这个词表里——它就是 4.1 节「图像占位符」的真身 |

**证据三：推理代码中的 128K 影子。**

README 的 vLLM 部署注释写着「If you need a longer context window, you can set `--max-model-len` and `--max-num-batched-tokens` to 131072」（[README.md:L256-L260](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L256-L260)），示例默认给 32768，而 `config.json` 的上限正是 131072——部署参数与模型规格在此对齐。

#### 4.3.4 代码实践

**实践：自己算一遍 MoE 的参数与激活账**

1. **实践目标**：亲手从 `config.json` 的数值推算「每层 64 选 6+2」的稀疏激活，把 16B/2.8B 从口号变成会算的数。
2. **操作步骤**：
   1. 打开 [config.json](https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct/raw/main/config.json)，记下 `hidden_size=2048`、`moe_intermediate_size=1408`、`n_routed_experts=64`、`num_experts_per_tok=6`、`n_shared_experts=2`、`num_hidden_layers=27`。
   2. 按单个专家（SwiGLU：门、上、下三份矩阵）估算参数：\(3 \times 2048 \times 1408 \approx 8.65\text{M}\)。
   3. 算三层账：单层全部路由专家 \(64 \times 8.65\text{M} \approx 554\text{M}\)；单层激活 \(8 \times 8.65\text{M} \approx 69\text{M}\)；26 个 MoE 层（第 1 层稠密）合计总/激活专家参数分别约 14.4B / 1.8B。
   4. 思考剩余缺口：16B−14.4B≈1.6B 总参与 2.8B−1.8B≈1B 激活来自哪里？（提示：`intermediate_size=11264` 的第 1 层稠密 FFN、注意力/MLA 投影矩阵、163,840×2048 的嵌入与输出头。）
3. **需要观察的现象**：你的估算与官方 16B/2.8B 同数量级但不完全相等——差异来自注意力参数、嵌入层、共享专家是否计入不同口径。
4. **预期结果**：能向别人解释清楚「64 选 6+2」如何把总参与激活参拉开约 5.7 倍的差距。
5. **待本地验证**：以上为手算推估，精确数值需加载权重逐张量统计（需要 GPU 与约 32GB+ 显存，可选做）。

#### 4.3.5 小练习与答案

**练习 1**：共享专家和路由专家有什么区别？为什么要两种并存？
**参考答案**：路由专家由路由器按 token 临时挑选（64 选 6），承载差异化知识；共享专家（2 个）对每个 token 无条件生效，承载所有 token 都需要的共性计算。没有共享专家时，共性能力也得靠路由专家重复学习，浪费容量并加重路由压力。

**练习 2**：Kimi-VL 的 128K 是模型一出生就有的吗？
**参考答案**：不是。语言解码器初始化自 Moonlight 预训练中间检查点时只有 8K 上下文；在预训练最后的「长上下文激活阶段」分两个子阶段各扩 4 倍（8K→32K→128K），同时把 RoPE 基础频率从 50,000 重置为 800,000（`config.json` 中 `rope_theta=800000.0` 可证），并用长短视频/文档/交错图文数据激活多模态长上下文能力。

**练习 3**：`first_k_dense_replace: 1` 是什么意思？为什么第一层可以不用 MoE？
**参考答案**：前 1 层解码层用普通稠密 FFN 替代 MoE 层。第 1 层紧邻嵌入层，token 表征还很浅、共性更强，用稠密层更简单稳定；MoE 的收益主要体现在中后层的大容量上（DeepSeek-V3 同款做法）。

## 5. 综合实践

**任务：亲手绘制并补全 Kimi-VL 架构数据流图**（本讲规格指定的综合实践，纸笔或画图软件均可）。

**步骤：**

1. **照图画骨架**：打开官方架构图 [figures/arch.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/arch.png)，对照 4.1.2 的伪代码，手绘一份数据流图。要求：
   - 画出图像路径（输入 → patch → MoonViT → pixel shuffle 合并 → MLP 投影）；
   - 画出文本路径（输入 → tokenizer）；
   - **用醒目颜色标出两条路径的汇合点**（视觉 token 展开/替换文本序列中的图像占位符，形成混合序列进入解码器）；
   - 在解码器内部画出「注意力 + MoE 前馈（路由器 + 专家池）」的重复堆叠。
2. **补全配置数值**：查阅技术报告 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) §2.1/§2.3 与 [config.json](https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct/raw/main/config.json)，把下列空格填到图上：
   - MoonViT：patch 大小 ____（14）、层数 ____（27）、隐藏维 ____（1152）、初始化来源 ____（SigLIP-SO-400M，约 400M 参数）、2D 位置编码方式 ____（插值绝对位置嵌入 + 2D RoPE）；
   - 投影层：结构 ____（两层 MLP，前置 2×2 pixel shuffle 空间压缩，对应 `merge_kernel_size [2,2]`）；
   - MoE 解码器：层数 ____（27，其中第 1 层稠密）、路由专家数 ____（64）、每 token 激活路由专家 ____（6）、共享专家 ____（2）、注意力配置 ____（16 头，MLA，`kv_lora_rank=512`）、上下文 ____（128K=131,072，`rope_theta=800,000`）。
3. **核对与标注**：报告中未明确写出的数值（如 MoonViT 的 27 层不在 PDF 正文里），在图注里标明「来源：模型仓库 config.json」；凡是推算（如 4.3.4 的参数账）标明「推算」。

**验收标准**：拿着这张图，你能不看书向别人讲清三件事——一张 1792×1792 的图如何变成约 4096 个 token、它们在哪里汇入文本序列、每个 token 为什么只激活约 2.8B 参数。

## 6. 本讲小结

- Kimi-VL 是三段式结构：**MoonViT（看）→ MLP 投影层（翻译）→ MoE 语言解码器（想）**；图像与文本在进入解码器前汇合成同一条 token 序列，汇合锚点是文本中的图像占位符（`media_placeholder_token_id: 163605`）。
- **MoonViT 原生分辨率**：不缩放、不切子图，按原始分辨率切 14×14 patch 并展平打包（NaViT 风格），配插值绝对位置嵌入 + 2D RoPE，复用语言模型的变长序列算力；小图省算力、大图保清晰（2506 版上限 3.2M 像素 ≈ 4096 视觉 token）。
- **MoE 稀疏激活**：64 个路由专家中每 token 只选 top-6，另有 2 个共享专家常开，第 1 层为稠密层；总参数 16B 与激活参数约 2.8B+0.4B（视觉）解耦，是「A3B」名称的由来。
- **128K 不是天生的**：从 Moonlight 8K 中间检查点出发，经两个 4 倍扩展子阶段激活而来，RoPE 基础频率同步从 50,000 重置为 800,000（`config.json` 的 `rope_theta` 可证）。
- **读架构的方法论**：本仓库 README 给骨架宣言，技术报告给设计思想，HuggingFace `config.json` 给工程数值——三者交叉验证，缺一不可。

## 7. 下一步学习建议

本讲搞清楚了「模型长什么样」，下一讲 **u3-l2「vLLM 离线批量推理」**将回到工程侧：当 16B 总参的模型要高吞吐服务时，vLLM 如何组织批处理与显存，`SamplingParams` 如何控制生成。建议顺序：

1. 先做本讲综合实践的手绘图（10~20 分钟），它是后面所有部署/微调讲义的心智底图。
2. 预习 [README.md:L215-L246](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L215-L246) 的 vLLM 离线推理示例，留意它仍然复用 `AutoProcessor`——你已经完全有能力读懂它的每一行。
3. 有余力的读者可提前翻阅技术报告 §2.5 的 4D 并行（DP/EP/PP/CP）一节，其中**专家并行（EP）**正是为 MoE 的专家分布设计的，是本讲 4.3 节的训练侧延伸，也为 u3-l4 微调讲义做铺垫。
