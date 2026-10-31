# 第 1 讲：项目总览——Qwen3-VL 是什么、模型家族与架构更新

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一两句话向别人解释清楚 **Qwen3-VL 是什么**、它在本学习手册涉及的仓库中处于什么位置。
2. 说出 **Qwen3-VL 的模型家族构成与命名规则**：Dense 与 MoE 两种架构、Instruct 与 Thinking 两种版本，以及 `Qwen3-VL-30B-A3B` 这类名字里每个字段的含义。
3. 列举 README 中宣告的 **八大核心能力**，并知道每项能力对应仓库里的哪个 cookbook 示例。
4. 解释 **三大架构更新**——Interleaved-MRoPE、DeepStack、文本-时间戳对齐——各自解决什么问题。
5. 理解一个贯穿全手册的关键事实：**本仓库只包含工程代码（工具库、微调框架、评测脚本、示例），模型结构的实现代码位于 `transformers` 库中**，权重托管在 Hugging Face / ModelScope 上。

本讲是整个学习手册的第一讲，不要求你写任何训练代码，重点是建立正确的「项目心智地图」。

## 2. 前置知识

本讲默认你刚开始接触多模态大模型，以下概念会用通俗语言解释清楚：

- **大语言模型（LLM）**：一个基于 Transformer 结构、通过预测下一个 token 来生成文本的神经网络。你输入一段文字（prompt），它输出一段文字（response）。
- **多模态大模型（VLM，Vision-Language Model）**：在 LLM 基础上加一个视觉编码器（通常是 ViT，Vision Transformer），把图片/视频转换成 LLM 能理解的向量序列，从而实现「看图说话」。Qwen3-VL 就是一个 VLM。
- **Token（词元）**：模型处理文本的最小单位。对 Qwen3-VL 来说，一张图也会被「压缩」成若干视觉 token，和文本 token 拼在一起送入 LLM。
- **Dense（稠密）模型**：所有参数在每次前向计算中都被激活使用。参数越多效果通常越好，但推理成本也线性上涨。
- **MoE（Mixture of Experts，混合专家）模型**：模型里有很多「专家」子网络，每个 token 只路由到其中少数几个专家。**总参数量可以很大，但每次实际激活的参数量很小**，兼顾容量与速度。
- **RoPE（旋转位置编码）**：让 Transformer 知道「token 在序列中的位置」的经典方法，通过把向量按不同频率旋转来编码位置。本讲只需要知道：**多模态模型需要一种更复杂的位置编码来同时表达「时间、画面高度、画面宽度」三种位置**，这就是后面要讲的 MRoPE。
- **transformers 库**：Hugging Face 的模型库。`pip install transformers` 之后，模型的结构代码（`Qwen3VLModel` 等类的 Python 实现）就来自这个库，而不是来自本仓库——这是本讲要反复强调的事实。

## 3. 本讲源码地图

本讲的「源码」主角是 `README.md`——它是这个仓库信息密度最高的文件，项目定位、模型家族、架构更新、快速上手代码全部在里面。同时我们会扫一眼仓库的顶层目录，为下一讲的仓库结构精读做铺垫。

| 文件/目录 | 作用 | 本讲关注点 |
| --------- | ---- | ---------- |
| `README.md` | 项目官方说明：介绍、模型发布记录、性能、cookbook 清单、快速上手、部署、评测复现设置 | 项目定位（L16-L21）、能力清单（L24-L40）、架构更新（L43-L54）、模型发布（L61-L66）、Quickstart（L128-L194）、Instruct/Thinking 采样超参（L1143-L1164） |
| `qwen-vl-utils/` | 视觉输入预处理工具库（Python 包） | 仅确认其存在与定位，详细源码在单元二精读 |
| `qwen-vl-finetune/` | SFT 微调框架 | 仅确认其存在，其中 `qwenvl/data/rope2d.py` 是理解 MRoPE 的仓库内入口（单元五精读） |
| `evaluation/` | 各基准评测脚本 | 仅确认其存在（单元六精读） |
| `cookbooks/` | 12+ 个能力示例笔记本 | 与能力清单对照 |
| `web_demo_mm.py` | Gradio 网页演示入口 | 仅确认其存在（第 4 讲精读） |

> 说明：目录职责的详细拆解是下一讲（u1-l2 仓库结构）的主题，本讲只需记住「模型本体不在这个仓库里」。

## 4. 核心概念与源码讲解

### 4.1 模块一：项目定位与模型家族

#### 4.1.1 概念说明

**Qwen3-VL 是什么？**

用 README 原话说，它是「Qwen 系列迄今最强大的视觉语言模型」。通俗地说：它是阿里通义千问团队的第三代「看得见」的大模型，你给它图片、视频加文字提问，它输出文字回答，还可以操作 GUI（图形界面）当 Agent 用。

**它以两种「版本维度」发布：**

1. **架构维度：Dense 与 MoE**
   - Dense：一个数字，如 `Qwen3-VL-2B`，表示总参数量约 20 亿，全部激活。
   - MoE：两个数字，如 `Qwen3-VL-30B-A3B`，总参数约 300 亿，每个 token 只激活约 30 亿（`A` = Activated，激活参数量）。
2. **行为维度：Instruct 与 Thinking**
   - Instruct：指令模型，直接给出回答。
   - Thinking：推理增强模型，回答前先在内部「思考」（生成一段推理过程），适合数学、逻辑等难题。

**一个非常重要的工程事实**：这个 GitHub 仓库里**没有模型结构代码**。模型类（`Qwen3VLForConditionalGeneration` 等）实现在 `transformers` 库中，需要 `transformers >= 4.57.0`；权重文件托管在 Hugging Face 与 ModelScope。本仓库提供的是围绕模型的**工程代码**：输入预处理工具、微调框架、评测脚本、能力示例、网页演示。

#### 4.1.2 核心流程

**命名规则解析流程**（拿到任意一个模型名，按下面步骤拆解）：

```text
输入: Qwen3-VL-30B-A3B-Thinking
  │
  ├─ "Qwen3-VL"      → 第 3 代视觉语言模型
  ├─ "30B"           → 总参数量 30 Billion → 含两个数字 → MoE 架构
  ├─ "A3B"           → 每 token 激活约 3 Billion 参数
  └─ "Thinking"      → 推理增强版本（若为 "Instruct" 则为直答版本）

输出: MoE / Thinking / 边缘到云端中的中档规格
```

**模型家族全景**（依据 README News 一节的发布记录）：

| 发布日期 | 模型 | 架构 | 版本 |
| -------- | ---- | ---- | ---- |
| 2025.09.23 | Qwen3-VL-235B-A22B | MoE（激活 22B） | Instruct / Thinking |
| 2025.10.04 | Qwen3-VL-30B-A3B | MoE（激活 3B） | Instruct / Thinking |
| 2025.10.15 | Qwen3-VL-4B、Qwen3-VL-8B | Dense | Instruct / Thinking |
| 2025.10.21 | Qwen3-VL-2B、Qwen3-VL-32B | Dense | Instruct / Thinking |

另外，2025.10.04 起还提供了 **FP8 量化版本**（发布于 Hugging Face collection），Quickstart 的 vLLM 示例里用的就是 `Qwen3-VL-235B-A22B-Instruct-FP8`。

#### 4.1.3 源码精读

**① 项目定位句**：[README.md:L16-L21](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L16-L21)

> 这一段是整个 README 的「电梯演讲」：说明 Qwen3-VL 是 Qwen 系列迄今最强的 VLM，在文本理解与生成、视觉感知与推理、上下文长度、空间与视频动态理解、Agent 交互五个方面全面升级。最关键的是第 21 行这句：

> *Available in Dense and MoE architectures that scale from edge to cloud, with Instruct and reasoning‑enhanced Thinking editions for flexible, on‑demand deployment.*

翻译：提供从边缘设备到数据中心的 Dense 与 MoE 两种架构，每种规格都有 Instruct 与 Thinking 两个版本按需选用。这句话就是我们 4.1.1 中「两个版本维度」结论的直接出处。

**② 模型发布记录（News）**：[README.md:L61-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L61-L66)

> News 一节按时间倒序记录了 Qwen3-VL 各规格模型的发布：L66 是 235B-A22B（旗舰 MoE），L65 是 30B-A3B（轻量 MoE，同时宣布 FP8 版本），L64 是 4B/8B（Dense），L63 是 2B/32B（Dense）。每一行都同时给出 Instruct 与 Thinking 两个 Hugging Face 链接——这就是我们整理上面「模型家族全景表」的依据。L62 提到 2025.11.27 发布了技术论文（arXiv:2511.21631），想深入原理可读。

**③ 模型代码在 transformers 中的证据**：[README.md:L132-L135](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L132-L135)

> Quickstart 开头第一句就是：

```
# The Qwen3-VL model requires transformers >= 4.57.0
pip install "transformers>=4.57.0"
```

> 只需装 `transformers`（加上 `torch`/`accelerate`）就能加载并推理 Qwen3-VL，不需要 clone 本仓库——这从侧面证明了「模型结构代码在 transformers 里」。本仓库的 `pip install -e ./qwen-vl-utils` 等命令只服务于输入预处理、微调、评测等工程环节。

#### 4.1.4 代码实践

**实践目标**：把「命名规则解析流程」变成一个可运行的小脚本，加深对模型家族的记忆。

**操作步骤**（示例代码，非仓库原有代码）：

1. 新建 `parse_name.py`，粘贴以下内容：

```python
# 示例代码：解析 Qwen3-VL 模型名的字段
import re

def parse_qwen3vl_name(name: str) -> dict:
    m = re.match(r"Qwen3-VL-(\d+B)(?:-A(\d+B))?-(Instruct|Thinking)", name)
    total, activated, edition = m.groups()
    return {
        "name": name,
        "arch": "MoE" if activated else "Dense",
        "total_params": total,
        "activated_params": activated or total,  # Dense 模型激活量 = 总量
        "edition": edition,
    }

for n in [
    "Qwen3-VL-2B-Instruct",
    "Qwen3-VL-235B-A22B-Thinking",
    "Qwen3-VL-30B-A3B-Instruct",
]:
    print(parse_qwen3vl_name(n))
```

2. 运行 `python parse_qwen3.py`。
3. 把输出与 4.1.2 的全景表逐行核对。

**需要观察的现象**：`Qwen3-VL-2B-Instruct` 被判为 Dense（无 `A` 字段），两个 `A` 系模型被判为 MoE，且激活参数量与名字中的 `A` 后数字一致。

**预期结果**：三行字典输出，`arch` 字段分别为 `Dense`、`MoE`、`MoE`。该脚本只依赖标准库 `re`，逻辑确定，可直接验证。

#### 4.1.5 小练习与答案

**练习 1**：`Qwen3-VL-235B-A22B-Instruct` 中的 `A22B` 是什么意思？它和「总参数 235B」有什么关系？

**参考答案**：`A22B` 表示每个 token 前向计算时**激活**的参数量约 220 亿（A = Activated）；235B 是模型总参数量。这是 MoE 架构：模型容量按 235B 计，但推理计算成本接近一个 22B 的 Dense 模型。

**练习 2**：如果你想在自己笔记本电脑的 CPU/小显存 GPU 上跑一个 Qwen3-VL，应该选家族里哪个模型？为什么？

**参考答案**：选 `Qwen3-VL-2B-Instruct`。它是家族中总参数量最小的 Dense 模型（约 2B），bf16 下仅权重就需约 \( 2 \times 10^9 \times 2 \text{字节} = 4\text{GB} \) 量级内存/显存（另计激活与框架开销），是入门实验的最低门槛；Instruct 版本直接输出答案，适合快速验证环境。

**练习 3**：判断对错：「要阅读 Qwen3-VL 模型前向传播的源码，应该 clone 本仓库。」——请说明理由。

**参考答案**：错。本仓库只有工程代码（qwen-vl-utils、微调、评测、示例），模型结构实现位于 `transformers` 库（>= 4.57.0）中；阅读模型源码应去看 `transformers` 仓库中对应的 modeling 文件。本仓库中最接近「模型内部机制」的源码是微调框架里的 `qwen-vl-finetune/qwenvl/data/rope2d.py`（MRoPE 位置编码的重建实现，单元五精读）。

### 4.2 模块二：核心能力清单

#### 4.2.1 概念说明

README 的 Key Enhancements 一节用 8 个条目概括了 Qwen3-VL 相对上一代的能力跃升。理解这份清单的价值在于：**它就是整本学习手册的「课程目录」**——每项能力在 `cookbooks/` 目录里几乎都有对应的可运行示例，在 `evaluation/` 目录里都有对应的评测基准。

八项能力速览（对照原文顺序）：

| # | 能力 | 一句话人话版 |
| - | ---- | ------------ |
| 1 | Visual Agent | 看着手机/电脑屏幕，认出按钮是什么、然后点按操作完成任务 |
| 2 | Visual Coding Boost | 看图/看视频直接生成 Draw.io、HTML、CSS、JS 代码 |
| 3 | Advanced Spatial Perception | 判断物体位置、视角、遮挡；更强的 2D 定位，新增 3D 定位 |
| 4 | Long Context & Video Understanding | 原生 256K 上下文可扩到 1M；能读整本书、看数小时视频并做秒级索引 |
| 5 | Enhanced Multimodal Reasoning | STEM/数学推理更强，回答有因果分析、讲证据 |
| 6 | Upgraded Visual Recognition | 名人、动漫、商品、地标、动植物……「什么都认识」 |
| 7 | Expanded OCR | 支持 32 种语言（上一代 10 种），低光/模糊/倾斜也稳，长文档结构解析更好 |
| 8 | Text Understanding on par with pure LLMs | 纯文本能力对标同代纯文本 LLM，图文融合「无损」 |

#### 4.2.2 核心流程

「能力 → 示例 → 评测」的对应流程：

```text
README 能力条目 (L24-L40)
   │  对应
   ▼
cookbooks/ 目录的示例笔记本 (README L113-L126 的表格)
   │  例如 Visual Agent → mobile_agent.ipynb / computer_use.ipynb
   │  例如 Expanded OCR → ocr.ipynb
   ▼
evaluation/ 目录的评测脚本（单元六精读）
   例如长视频理解 → VideoMME
```

其中两个数字值得记下（后面单元会反复出现）：

- **256K → 1M**：原生上下文 262144 token，通过 YaRN 技巧可扩到 1000000（详见 4.3 与第 3 单元第 4 讲）。
- **32 倍空间压缩**：一张 \( H \times W \) 的图，进入模型的视觉 token 数约为

\[ n_{\text{visual}} = \frac{H}{32} \times \frac{W}{32} \]

  这意味着一张 \( 1024 \times 1024 \) 的图 ≈ 1024 个视觉 token——这是理解后续「像素预算」一切参数（`min_pixels`/`max_pixels`）的钥匙。

#### 4.2.3 源码精读

**① 八项能力原文**：[README.md:L24-L40](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L24-L40)

> Key Enhancements 清单原文。8 个条目依次是 Visual Agent（L26）、Visual Coding Boost（L28）、Advanced Spatial Perception（L30）、Long Context & Video Understanding（L32，其中明确写了 *Native 256K context, expandable to 1M ... second-level indexing*）、Enhanced Multimodal Reasoning（L34）、Upgraded Visual Recognition（L36）、Expanded OCR（L38，写明 *Supports 32 languages (up from 10)*）、Text Understanding on par with pure LLMs（L40）。本讲表格即由此整理。

**② cookbook 能力对照表**：[README.md:L109-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L109-L126)

> README 用一张表列出 12 个 cookbook：omni_recognition、document_parsing、2d_grounding、ocr、video_understanding、mobile_agent、computer_use、3d_grounding、think_with_images、mmcode、long_document_understanding、spatial_understanding。每个条目都带 Colab 入口。把这张表与 ① 的能力清单并排看，会发现几乎一一对应：Visual Agent ↔ mobile_agent/computer_use，Spatial Perception ↔ 2d_grounding/3d_grounding，OCR ↔ ocr，Visual Coding ↔ mmcode……

**③ 32 倍压缩的直接证据**：[README.md:L348-L356](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L348-L356)

> Pixel Control 一节的注释写着 *since the compression ratio is 32 for Qwen3-VL, we can set the number of visual tokens of a single image to 256-1280 (32× spatial compression)*，并把 `processor.image_processor.size` 设成 `1280*32*32` / `256*32*32` 来控制单图视觉 token 数在 256~1280 之间。L354 还指出视频是 *32× spatial + 2× temporal compression*。这段代码本讲只需看懂结论，参数细节留到单元二/三。

#### 4.2.4 代码实践

**实践目标**：建立「能力 ↔ cookbook」的条件反射，并亲手验证 32 倍压缩公式。

**操作步骤**：

1. 打开 [README.md:L113-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L113-L126) 的 cookbook 表格，本地 `ls cookbooks/` 对照，确认 12 个笔记本文件真实存在（目录里还有表格未列出的 `german_document_ocr.ipynb` 等新示例）。
2. 画一张两列对照表：左列抄 8 项能力，右列填你认为最匹配的 cookbook 文件名。
3. 用 Python 口算验证压缩公式（示例代码）：

```python
# 示例代码：估算视觉 token 数
def visual_tokens(h: int, w: int, patch: int = 32) -> int:
    return (h // patch) * (w // patch)

for h, w in [(1024, 1024), (1280, 720), (640, 480)]:
    print(f"{h}x{w} -> {visual_tokens(h, w)} visual tokens")
```

**需要观察的现象**：cookbook 表格的 12 个条目能否全部被 8 项能力「认领」；三张图的 token 数是否符合 \( \frac{H}{32}\times\frac{W}{32} \)。

**预期结果**：`1024x1024 -> 1024`、`1280x720 -> 40*22=880`、`640x480 -> 20*15=300`（整除向下取整）。纯算术，可直接验证。

#### 4.2.5 小练习与答案

**练习 1**：`max_pixels = 1280*32*32`（即 1,310,720 像素）限制下，一张图最多产生多少视觉 token？

**参考答案**：\( \frac{1310720}{32 \times 32} = 1280 \) 个。`max_pixels` 限制的是像素总数 \( H \times W \)，除以压缩率 \( 32^2 = 1024 \) 就是视觉 token 上限。

**练习 2**：哪两项能力是 Qwen3-VL 明确标注「上一代没有或明显更弱」的升级点？

**参考答案**：① OCR：支持语言从 10 种扩到 32 种（L38 明确写 *up from 10*）；② 空间感知：*enables 3D grounding*（L30），3D 定位是本代新强调的能力（配套 cookbook `3d_grounding.ipynb`）。

**练习 3**：如果你想验证「数小时长视频理解 + 秒级索引」能力，应该去仓库哪两个地方找材料？

**参考答案**：能力示例去 `cookbooks/video_understanding.ipynb`（含长视频理解与 video grounding）；量化评测去 `evaluation/VideoMME/`（单元六第 3 讲会精读其推理与评测脚本）。

### 4.3 模块三：三大架构更新

#### 4.3.1 概念说明

README 的 Model Architecture Updates 一节只列了三行，但它们是 Qwen3-VL 区别于 Qwen2.5-VL 的核心，也是整本手册里多次回访的主线（rope2d.py 源码精读、视频 metadata 机制、YaRN factor 取值都源于此）。

**① Interleaved-MRoPE（交错式多模态旋转位置编码）**

先从问题说起：文本是一维序列，用普通 RoPE 编码位置就够了；但视频 token 天然带三个坐标——**第几帧（t）、画面第几行（h）、第几列（w）**。MRoPE（Multimodal RoPE）把 RoPE 的维度分成三段，分别用 t/h/w 三组位置 id 去旋转。「Interleaved（交错）」指的是 Qwen3-VL 改变了三段维度的分配方式——从「连续分块」改为「交错排列」，让时间、宽度、高度三个维度都能获得**全频段**的频率覆盖。低频分量对应长程位置感知，全频段分配显著提升长视频推理。

**② DeepStack（深层堆叠特征注入）**

传统 VLM 通常只把 ViT **最后一层**的输出投影后喂给 LLM，细节信息在深层已被压缩损耗。DeepStack 把 ViT **多个层级**的特征融合注入 LLM，保住细粒度细节、锐化图文对齐——这对 OCR 小字、UI 小图标这类「细节决定成败」的场景尤其重要。

**③ 文本-时间戳对齐（Text–Timestamp Alignment）**

上一代 Qwen2.5-VL 用 T-RoPE：把「绝对时间戳」编进位置 id 的 t 分量来实现视频定位。Qwen3-VL 走了另一条路——**把时间戳信息直接对齐到文本/帧序列上**，实现精确到秒级的事件定位（*second-level indexing*）。工程上的配套是 `qwen-vl-utils` 新增的 `return_video_metadata`：预处理阶段返回每个视频的元数据（fps、采样帧下标、总帧数），供处理器据此构造带时间信息的输入。

#### 4.3.2 核心流程

**MRoPE 维度分配的直觉**（以 Qwen3-VL 默认 `mrope_section = [24, 20, 20]` 为例）：

- RoPE 作用于每个注意力头的维度（Qwen3 系列为 64 维），三段之和 64：24 维编码时间 t、20 维编码高度 h、20 维编码宽度 w。
- 每一段内部再按频率从高到低展开。第 \(k\) 维的旋转角频率为

\[ \theta_k = b^{-2k/d}, \quad b=10000,\ d=64 \]

  低频维度（\(k\) 大）变化缓慢、负责长程相对位置；高频维度变化快、负责局部精细位置。
- 「Interleaved」的关键收益：无论 t/h/w 哪一段，都能享用到完整的高频→低频频谱，而不是某一段只剩高频、某一段只剩低频。这正是 README 说 *Full-frequency allocation over time, width, and height* 的含义。

**时间戳对齐的数据流（工程视角，细节在单元二第 4 讲）**：

```text
视频文件/帧列表
  → qwen-vl-utils 采样 (fps, 帧下标)
  → return_video_metadata=True 返回 (帧张量, {fps, frames_indices, total_num_frames})
  → processor 据此为视觉 token 构造时间信息
  → 模型可回答"第几秒发生了什么 / 事件持续多久"
```

**三个更新各解决什么**（速记）：

| 更新 | 针对模态 | 解决的痛点 |
| ---- | -------- | ---------- |
| Interleaved-MRoPE | 视频（长时序） | 长视频位置编码分辨率不足 |
| DeepStack | 图像（细节） | 只用 ViT 末层特征导致细粒度信息丢失 |
| 文本-时间戳对齐 | 视频（定位） | T-RoPE 绝对时间编码下的事件定位不准 |

#### 4.3.3 源码精读

**① 三大更新原文**：[README.md:L43-L54](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L43-L54)

> Model Architecture Updates 一节（L45-L47 是架构示意图链接，L50/L52/L54 是三条更新）：
>
> - L50 *Interleaved-MRoPE: Full‑frequency allocation over time, width, and height via robust positional embeddings, enhancing long‑horizon video reasoning.*——「在时间/宽/高三个维度做全频段分配」印证了 4.3.2 的频率分析。
> - L52 *DeepStack: Fuses multi‑level ViT features to capture fine‑grained details and sharpen image–text alignment.*——「multi-level ViT features」即多层特征融合。
> - L54 *Text–Timestamp Alignment: Moves beyond T‑RoPE to precise, timestamp‑grounded event localization for stronger video temporal modeling.*——明确写了「超越 T-RoPE」。

**② Interleaved-MRoPE 在配置文件中的样子**：[README.md:L770-L787](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L770-L787)

> Processing Long Texts 一节给出的 YaRN 扩展配置片段中可以直接看到 MRoPE 的字段：

```json
"rope_scaling": {
    "rope_type": "yarn",
    "mrope_section": [24, 20, 20],
    "mrope_interleaved": true,
    "factor": 3.0,
    "original_max_position_embeddings": 262144
}
```

> `mrope_section: [24,20,20]` 就是 t/h/w 三段的维度划分（与 4.3.2 的直觉推导一致）；`mrope_interleaved: true` 显式打开交错分配；`original_max_position_embeddings: 262144` 即原生 256K 上下文。

**③ Interleaved-MRoPE 带来的 YaRN factor 取值变化**：[README.md:L795-L795](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L795)

> 原文提醒：*Because Interleaved-MRoPE's position IDs grow more slowly than vanilla RoPE, use a smaller scaling factor. For example, to support 1M context with 256K context length, set factor=2 or 3 — not 4.*（因为 Interleaved-MRoPE 的位置 id 增长比普通 RoPE 慢，扩展 1M 上下文时 factor 取 2 或 3 而不是理论值 4。）算一笔账：\( 1000000 / 262144 \approx 3.8 \)，普通 RoPE 会取 4；由于 MRoPE 的多段位置 id 交替使用，等效序列长度增长更慢，所以取 2~3 就够了。这是「架构更新直接影响部署参数」的一个漂亮例子。

**④ 时间戳对齐的工程配套**：[README.md:L422-L434](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L422-L434)

> New qwen-vl-utils Usage 一节列出新版工具库的两个变化：`image_patch_size`（Qwen2.5-VL 为 14，Qwen3-VL 为 16）与 `return_video_metadata`（*Due to the new video processor, if True, each video returns as (video_tensor, video_metadata)*，仅 Qwen3-VL 可用）。后者正是文本-时间戳对齐在预处理层的落地：元数据携带采样信息进入 processor。深度精读安排在单元二第 4 讲。

#### 4.3.4 代码实践

**实践目标**：亲手「摸到」MRoPE 的三段维度划分，并对 factor 取值建立数感。

**操作步骤**：

1. 阅读配置片段 [README.md:L770-L787](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L770-L787)，抄下 `mrope_section` 的三个数。
2. 运行下面的小脚本（示例代码），模拟「连续分块」与「交错分配」两种维度排列：

```python
# 示例代码：对比 MRoPE 维度的两种排列方式
section = [24, 20, 20]           # t, h, w 三段大小，来自 config 的 mrope_section
dims = list(range(64))           # 一个注意力头的 64 个旋转维度下标

# 连续分块（Qwen2-VL / Qwen2.5-VL 风格）：[ttt...hhh...www...]
blocked = ["t"] * section[0] + ["h"] * section[1] + ["w"] * section[2]
# 交错分配（Qwen3-VL，mrope_interleaved=true）：t,h,w,t,h,w,...
interleaved = ["thw"[i % 3] for i in dims]

for name, layout in [("blocked", blocked), ("interleaved", interleaved)]:
    # 维度下标越大频率越低（长程感知），统计每段拿到的低频维度数
    low_freq = layout[64 // 2:]
    print(name, {c: low_freq.count(c) for c in "thw"})
```

3. 观察两种排列下，低频（长程）维度在 t/h/w 上的分布差异。
4. 口算验证 factor：\( 1000000 / 262144 \approx 3.81 \)，再对照 L795 的「取 2 或 3，不取 4」。

**需要观察的现象**：`blocked` 排列里 t 段（24 维）会占据不成比例的低频维度，w 段可能完全落在高频区；`interleaved` 排列里三个维度近似均分高低频。

**预期结果**：`blocked` 输出中 t/h/w 的低频维度数是 `t:0, h:12, w:20`——时间维度 t 的 24 个维度全部落在前 24 个高频区，一个低频维度都拿不到，这正是连续分块在长视频上的短板；`interleaved` 输出为 `t:11, h:10, w:11`，三段近似均分全频段。注意：这里的交错示意是帮助理解的简化模型，transformers 内部的精确分配方式以官方代码为准（待本地验证具体分配细节）。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 Interleaved-MRoPE 对「长视频」特别重要？

**参考答案**：长视频意味着时间维度的位置跨度极大，位置编码必须有足够低频（长波长）的分量才能区分「相隔很远」的两帧。连续分块分配时某些维度可能分不到足够的低频分量，长程位置就会变得难以区分；交错分配保证 t/h/w 三段都能覆盖全频段，因此长时序视频推理更稳（README L50 原文：*enhancing long-horizon video reasoning*）。

**练习 2**：DeepStack 与「只取 ViT 最后一层特征」的做法相比，直观优势是什么？对哪类任务收益最大？

**参考答案**：ViT 深层特征语义强但空间细节已被抽象掉，浅/中层保留更多边缘、纹理、小字等细粒度信息。DeepStack 融合多层特征注入 LLM，相当于把「语义」和「细节」一起交给语言模型。收益最大的是 OCR 小字、UI 小图标识别、文档版面解析这类细节敏感任务（对应 cookbook `ocr.ipynb`、`document_parsing.ipynb`）。

**练习 3**：把 256K 上下文扩到 1M，为什么 README 建议YaRN factor 取 2~3 而不是理论的 4？

**参考答案**：因为 Interleaved-MRoPE 下模型实际使用的位置 id 增长比普通 RoPE 慢（三段维度交替推进），等效位置范围比序列长度小；理论比 \( 10^6/262144 \approx 3.8 \) 会高估所需缩放，故取 2 或 3（README L795 明确说明）。

### 4.4 模块四：Instruct 与 Thinking 版本差异

#### 4.4.1 概念说明

同一规格的 Qwen3-VL 总是成对发布两个版本：

- **Instruct（指令版）**：你提问，它直接回答。延迟低、输出短、确定性相对强，适合 OCR、识别、Agent 操作这类「要答案不要过程」的任务。
- **Thinking（思考版）**：回答前先生成一段内部推理（thinking 过程），再给出最终答案。数学、逻辑、复杂视觉推理上更强，代价是输出 token 更多、延迟更高。

注意两点：

1. 两个版本是**两个不同的checkpoint**（不同的 Hugging Face 仓库），不是同一个模型加开关。
2. 选用不同版本时，**采样超参（temperature/top_p 等）也应不同**——README 的 Evaluation Reproduction 一节分别给出了两组官方推荐值，这也是复现官方评测分数的必要条件。

#### 4.4.2 核心流程

「选版本 → 配超参」的决策流程：

```text
任务是什么？
 ├─ 识别 / OCR / Agent 操作 / 简单 VQA ──→ Instruct
 │     采样: temperature=0.7, top_p=0.8, top_k=20,
 │            presence_penalty=1.5, out_seq_length=32768
 └─ 数学 / 逻辑推理 / 复杂多步视觉推理 ──→ Thinking
       采样: temperature=0.6, top_p=0.95, top_k=20,
              presence_penalty=0.0, out_seq_length=40960
```

两个版本的差异不只是「想不想」，还有**输出预算**：Thinking 版官方 `out_seq_length` 是 40960（比 Instruct 的 32768 更长），因为思考过程本身要消耗大量 token——部署时上下文窗口和显存的规划都要按这个来。

#### 4.4.3 源码精读

**① 双版本并存的直接证据**：[README.md:L63-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L63-L66)

> News 里每一行发布记录都是成对给出两个链接，例如 L63：*We have released the **Qwen3-VL-2B** ([Instruct](...)/[Thinking](...)) and **Qwen3-VL-32B** ([Instruct](...)/[Thinking](...))*。2B、4B、8B、32B、30B-A3B、235B-A22B 六个规格全部如此——「同规格双版本」是家族的发布惯例。

**② 官方评测采样超参（Instruct）**：[README.md:L1143-L1153](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1143-L1153)

> Evaluation Reproduction → Generation Hyperparameters → Instruct models 一节：`seed=3407, top_p=0.8, top_k=20, temperature=0.7, repetition_penalty=1.0, presence_penalty=1.5, out_seq_length=32768`。值得注意的是 `presence_penalty=1.5` 是个不小的值——对识别/描述类任务可以抑制重复罗列。

**③ 官方评测采样超参（Thinking）**：[README.md:L1154-L1164](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1154-L1164)

> Thinking models 一节：`seed=1234, top_p=0.95, top_k=20, temperature=0.6, repetition_penalty=1.0, presence_penalty=0.0, out_seq_length=40960`。对比 ②：top_p 更高（0.95 vs 0.8，思考需要更大的候选词多样性）、presence_penalty 归零（思考过程允许「重复提及」同一实体，不应惩罚）、输出预算更大（40960 vs 32768）。

**④ 与 Thinking 相关的能力示例**：[README.md:L123-L123](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L123-L123)

> cookbook 表格中的 *Thinking with Images*（`think_with_images.ipynb`）：借助 `image_zoom_in_tool` 与 `search_tool`，让模型在思考过程中主动「放大看细节」——展示了 Thinking 版本「边想边看」的进阶玩法（单元七第 1 讲实践）。

#### 4.4.4 代码实践

**实践目标**：把两组官方采样超参整理成可直接导入的配置，并理解每个参数的作用方向。

**操作步骤**：

1. 对照 [README.md:L1143-L1164](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1143-L1164)，新建 `sampling_presets.py`（示例代码）：

```python
# 示例代码：Qwen3-VL 官方评测采样预设（数值抄自 README L1143-L1164）
SAMPLING_PRESETS = {
    "instruct": dict(seed=3407, top_p=0.8,  top_k=20,
                     temperature=0.7, repetition_penalty=1.0,
                     presence_penalty=1.5, out_seq_length=32768),
    "thinking": dict(seed=1234, top_p=0.95, top_k=20,
                     temperature=0.6, repetition_penalty=1.0,
                     presence_penalty=0.0, out_seq_length=40960),
}

for name, p in SAMPLING_PRESETS.items():
    print(f"{name:>9}: " + ", ".join(f"{k}={v}" for k, v in p.items()))
```

2. 运行脚本，逐项对照原文检查数字是否抄对。
3. 在笔记里用一句话回答：为什么 Thinking 的 `presence_penalty` 是 0 而 Instruct 是 1.5？

**需要观察的现象**：两组参数共 5 处不同（seed、top_p、temperature、presence_penalty、out_seq_length），2 处相同（top_k、repetition_penalty）。

**预期结果**：脚本打印两行预设；差异分析能对上 4.4.2 的流程图。参数本身的「效果差异」（例如 presence_penalty 对输出风格的影响）属于运行时行为，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：Instruct 与 Thinking 是同一个模型的不同提示模式吗？

**参考答案**：不是。它们是**两个独立训练、独立发布的 checkpoint**（Hugging Face 上是两个不同的模型仓库，如 `Qwen/Qwen3-VL-2B-Instruct` 与 `Qwen/Qwen3-VL-2B-Thinking`），README News 中每个规格都成对给出两个链接。

**练习 2**：你要构建一个「拍照识别发票并抽取字段」的生产服务，选 Instruct 还是 Thinking？采样上有什么注意点？

**参考答案**：选 Instruct。信息抽取任务要的是快而稳的确定性输出，不需要长推理；按官方预设用 `temperature=0.7, top_p=0.8, top_k=20`，可保留 `presence_penalty=1.5` 抑制字段罗列时的重复；若追求更强确定性，生产上也常进一步调低温度（属于工程取舍，偏离官方评测预设时需自行评估）。

**练习 3**：部署 Thinking 版时，为什么 `out_seq_length` 要给到 40960？这会影响哪方面的资源规划？

**参考答案**：因为思考过程与最终回答加起来 token 量远大于直答式输出，官方评测预算给到 40960。它直接影响：KV cache 显存占用、单请求延迟与吞吐估算、以及 vLLM 部署时 `--max-model-len` 等参数的设定。

## 5. 综合实践

**任务：跑通你的第一个 Qwen3-VL 对话，并建立贯穿全手册的「实验笔记」习惯。**

1. **实践目标**：安装 `transformers>=4.57.0`，用 `Qwen/Qwen3-VL-2B-Instruct` 跑通 README Quickstart 的最小图像对话示例；把模型规格、环境版本与生成文本沉淀到 `notes.md`，作为后续所有讲义实验的记录模板。

2. **操作步骤**：

   a. 准备环境（示例命令）：

   ```bash
   conda create -n qwen3vl python=3.10 -y && conda activate qwen3vl
   pip install "transformers>=4.57.0" accelerate torch
   python -c "import transformers; print(transformers.__version__)"   # 应 >= 4.57.0
   ```

   b. 新建 `infer_first.py`。以下代码改编自 [README.md:L144-L194](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L144-L194) 的 Quickstart，仅把模型从 235B 换成 2B（示例代码）：

   ```python
   from transformers import AutoModelForImageTextToText, AutoProcessor

   MODEL_ID = "Qwen/Qwen3-VL-2B-Instruct"   # 家族中最小的 Dense 模型，适合入门
   model = AutoModelForImageTextToText.from_pretrained(
       MODEL_ID, dtype="auto", device_map="auto"
   )
   processor = AutoProcessor.from_pretrained(MODEL_ID)

   messages = [
       {
           "role": "user",
           "content": [
               {"type": "image",
                "image": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg"},
               {"type": "text", "text": "Describe this image."},
           ],
       }
   ]

   inputs = processor.apply_chat_template(
       messages, tokenize=True, add_generation_prompt=True,
       return_dict=True, return_tensors="pt",
   )
   inputs = inputs.to(model.device)

   generated_ids = model.generate(**inputs, max_new_tokens=128)
   generated_ids_trimmed = [
       out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
   ]
   output_text = processor.batch_decode(
       generated_ids_trimmed, skip_special_tokens=True,
       clean_up_tokenization_spaces=False,
   )
   print(output_text[0])
   ```

   c. 运行 `python infer_first.py`（首次运行会从 Hugging Face 下载约几 GB 权重；国内网络可改用 ModelScope 的 `snapshot_download`）。

   d. 新建 `notes.md`，至少记录：模型 ID 与版本（Instruct）、`transformers` 版本、`torch` 版本与设备（CPU/GPU）、实际 `dtype`、完整生成文本、`len(inputs.input_ids)`（输入 token 数）、遇到的问题与解决方式。

3. **需要观察的现象**：
   - 模型对示例图（一位女士和狗的海滩照）输出一段英文描述；
   - 终端首次运行出现权重下载进度条；
   - `generated_ids` 比 `inputs.input_ids` 长，裁剪逻辑 `out_ids[len(in_ids):]` 去掉了输入部分。

4. **预期结果**：打印一段连贯的图像描述（示例图内容为海滩上人与狗，描述应包含相应元素）；`notes.md` 成为你的环境基线记录。若在无 GPU 的机器上运行，`device_map="auto"` 会自动落到 CPU，速度慢但可完成。生成内容的具体措辞取决于采样与版本，待本地验证。

5. **可选延伸**：把模型 ID 换成 `Qwen/Qwen3-VL-2B-Thinking` 再跑一次，观察输出前是否出现思考过程，并对比两版的输出长度与风格（对应 4.4 的超参差异）。

## 6. 本讲小结

- **Qwen3-VL 是 Qwen 系列第三代视觉语言模型**，按「架构 × 版本」两个维度发布：Dense（2B/4B/8B/32B）与 MoE（30B-A3B、235B-A22B），每个规格都有 Instruct 与 Thinking 两个独立 checkpoint。
- **本仓库只包含工程代码**（qwen-vl-utils 预处理、qwen-vl-finetune 微调、evaluation 评测、cookbooks 示例、web_demo_mm.py 演示）；模型结构实现位于 `transformers >= 4.57.0`，权重在 Hugging Face / ModelScope。
- **八大核心能力**（Agent、视觉编码、空间感知、长上下文视频、多模态推理、识别、OCR、纯文本）与 cookbooks 目录的 12 个示例笔记本基本一一对应；图像按 \( \frac{H}{32} \times \frac{W}{32} \) 压缩为视觉 token。
- **三大架构更新**：Interleaved-MRoPE（t/h/w 三段维度交错、全频段分配，改善长视频位置编码）、DeepStack（多层 ViT 特征融合注入，保细节）、文本-时间戳对齐（超越 T-RoPE，配合 `return_video_metadata` 实现秒级事件定位）。
- **Interleaved-MRoPE 直接影响部署参数**：位置 id 增长变慢，256K→1M 的 YaRN factor 取 2~3 而非 4。
- **Instruct 与 Thinking 的差异要落到采样超参上**：官方评测预设分别是 `temperature=0.7/top_p=0.8/presence_penalty=1.5/out_seq_length=32768` 与 `temperature=0.6/top_p=0.95/presence_penalty=0/out_seq_length=40960`。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：仓库结构与四大子项目地图——逐一打开 `qwen-vl-utils/`、`qwen-vl-finetune/`、`evaluation/`、`cookbooks/` 的 README 与入口文件，画出你自己的仓库地图。
- **第 3 讲（u1-l3）**：第一次推理的多图与视频变体，深入 `messages` 结构与 `apply_chat_template` 调用链。
- **如果想提前触碰「模型内部」**：直接跳去浏览 `qwen-vl-finetune/qwenvl/data/rope2d.py`，找一找 `get_rope_index_3` 函数——那里有本仓库中最接近 Interleaved-MRoPE 实现的源码（正式精读在单元五第 4 讲）。
- **如果想读原理**：README L62 链接的 Qwen3-VL 技术报告（arXiv:2511.21631）对三大架构更新有完整论述，可与本讲 4.3 节互相印证。
