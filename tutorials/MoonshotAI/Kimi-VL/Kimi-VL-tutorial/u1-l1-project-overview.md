# 项目总览：Kimi-VL 是什么，这个仓库里有什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 Kimi-VL 的定位：**月之暗面（Moonshot AI）开源的高效 MoE 视觉语言模型**，总参数 16B、语言解码器仅激活约 2.8B、支持 128K 上下文。
2. 区分三个模型变体——`Kimi-VL-A3B-Instruct`、`Kimi-VL-A3B-Thinking`（已废弃）与 `Kimi-VL-A3B-Thinking-2506`——各自的适用场景与推荐参数。
3. 理解本 GitHub 仓库的特殊形态：它是**模型发布与文档仓库**，不含模型实现代码；真正的实现代码托管在 HuggingFace 模型仓库中，通过 `trust_remote_code=True` 在运行时加载。

## 2. 前置知识

本讲面向零基础读者，你只需要对以下概念有朴素的认识即可（不熟悉也没关系，下面用大白话解释）：

- **大语言模型（LLM）**：输入一段文字，逐个 token（词元）预测下一个 token 的神经网络，比如 ChatGPT 背后的模型。
- **视觉语言模型（VLM）**：在 LLM 基础上"长出眼睛"——把图片转换成模型能理解的向量，再和文字一起处理。你可以向它提问"这张图里有什么"。
- **MoE（Mixture-of-Experts，混合专家）**：一种"分诊"机制。模型内部有很多个"专家"网络，每来一个 token 只有一部分专家被激活参与计算。好处是：模型总容量可以做得很大，但每次推理的实际计算量很小。
- **参数量（Params）**：模型中可学习权重（数字）的总个数。16B = 160 亿。参数越多通常能力越强，但显存和计算开销也越大。
- **上下文长度（Context Length）**：模型一次能"看到"的 token 总量上限。128K 意味着可以一次性输入约十几万字（或等价的图像 token），适合长文档、长视频理解。
- **GitHub 仓库 vs HuggingFace 模型仓库**：GitHub 存放代码和文档；HuggingFace 除了文档，还存放模型权重文件（几个到上百 GB 的 `.safetensors`）以及配套的 Python 代码。

## 3. 本讲源码地图

本讲涉及的文件都位于仓库根目录（本仓库没有 `src/`、`lib/` 等代码目录，这本身就是它的重要特征，见 4.3 节）：

| 文件 / 目录 | 作用 |
| :--- | :--- |
| [README.md](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md) | 项目主文档：模型定位、变体对照表、推理/微调/部署全部示例代码，是本讲和后续多讲的核心阅读材料 |
| [figures/arch.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/arch.png) | 官方架构图：MoE 语言模型 + MoonViT 视觉编码器 + MLP 投影层 |
| [figures/demo.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/demo.png) | README 推理示例使用的测试图片（一张穹顶建筑照片），后续实践会反复用到 |
| [LICENSE](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/LICENSE) | MIT 许可证，允许自由使用、修改和再分发 |
| [requirements.txt](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt) | 运行 README 示例所需的 Python 依赖清单（仅 8 项） |
| [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) | 官方技术报告，深入原理时的第一手资料（进阶单元会精读） |

## 4. 核心概念与源码讲解

### 4.1 项目背景与模型定位

#### 4.1.1 概念说明

Kimi-VL 是月之暗面开源的**视觉语言模型**，它的核心卖点是"**大容量、小激活**"：

- **16B 总参数，约 2.8B 激活参数**：模型里存着 160 亿个权重，但处理每个 token 时只动用其中约 28 亿个（都在语言解码器里）。这就是 MoE 架构带来的稀疏激活——能力按 16B 的容量积累，计算按 2.8B 的开销付费。
- **三大能力**：多模态推理（看图做数学题、OCR、多图理解）、长上下文理解（128K 窗口，长视频/长文档）、Agent 能力（操作系统级交互，如 OSWorld 基准）。
- **原生分辨率视觉编码**：视觉编码器 MoonViT 不把图片强行缩放到固定尺寸，而是按原始分辨率切分处理，因此能看清超高分辨率图片的细节，同时普通图片不浪费算力。

激活比例可以用一个简单的式子直观感受：

\[ \text{激活比例} = \frac{\text{激活参数}}{\text{总参数}} \approx \frac{2.8\text{B}}{16\text{B}} \approx 17.5\% \]

也就是说，每次推理大约只"点亮"了模型的六分之一的参数。

> **关于 2.8B 与 3B 两个口径**：README 正文说语言解码器"activating only **2.8B** parameters"，而变体表格里写的是 **3B**。两者并不矛盾——2.8B 是正文的精确值（仅指语言解码器），表格取整为 3B，模型名中的 "A3B" 就是 **A**pproximately 3B（约 3B）的意思。读文档时注意记录每个数字的出处，这是源码阅读的基本功。

#### 4.1.2 核心流程

Kimi-VL 处理一次"看图问答"的高层数据流（细节留到进阶单元）：

```
用户输入：一张图片 + 一段文字提问
        │
        ▼
┌──────────────────────┐
│ MoonViT 视觉编码器    │  按原生分辨率把图片切成 patch，
│ （原生分辨率）        │  编码成一串视觉特征向量
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ MLP 投影层           │  把视觉特征"翻译"成语言模型
│                      │  能理解的向量空间（对齐）
└──────────┬───────────┘
           ▼  视觉 token 与文字 token 拼接
┌──────────────────────┐
│ MoE 语言解码器        │  16B 总参数，每 token 仅激活
│ （128K 上下文）       │  约 2.8B；逐 token 生成回答
└──────────┬───────────┘
           ▼
输出：文字回答
```

#### 4.1.3 源码精读

**① 定位句——README 开篇第一句话**

[README.md:L15](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15) 用一句话给出官方定位：高效开源的 MoE 视觉语言模型，提供多模态推理、长上下文理解和强 Agent 能力，语言解码器仅激活 2.8B 参数（Kimi-VL-A3B）。这就是我们上面 4.1.1 全部结论的原文出处。

**② 长上下文与原生分辨率的实测成绩**

[README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23) 说明了两个关键设计及对应基准分数：128K 扩展上下文窗口（LongVideoBench 64.5、MMLongBench-Doc 35.1），以及 MoonViT 原生分辨率编码带来的高清感知能力（InfoVQA 83.2、ScreenSpot-Pro 34.5），同时普通图片还能保持较低计算成本。"高分不掉算力"是原生分辨率方案的核心动机。

**③ Thinking 变体的由来**

[README.md:L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25) 介绍了长思维链变体 Kimi-VL-Thinking：通过长链式思考（CoT）监督微调（SFT）和强化学习（RL）训练得来，在 MMMU 61.7、MathVision 36.8、MathVista 71.3 的水平上仍保持 2.8B 激活参数。这解释了"会思考"能力不是天生的，而是后训练出来的。

**④ 架构三件套**

[README.md:L37-L43](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L37-L43) 是 README 的 Architecture 章节，明确架构由三部分组成：MoE 语言模型、原生分辨率视觉编码器 MoonViT、MLP 投影层，并嵌入了官方架构图 [figures/arch.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/arch.png)。建议你现在就打开这张图对照 4.1.2 的流程图看一遍——图中从左到右正是"图像 → 视觉编码 → 投影 → 语言解码"的走向（具体模块参数以图和 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) 技术报告为准，本讲不展开）。

**⑤ 开源协议**

[LICENSE:L1-L2](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/LICENSE#L1-L2) 表明本仓库采用 MIT 许可证，版权归 Moonshot AI 所有。MIT 是最宽松的开源协议之一，你可以自由地使用、修改、再分发（包括商用），只需保留版权声明。这为后续把 Kimi-VL 集成进自己的产品扫清了法律顾虑。

#### 4.1.4 代码实践

**实践：从 README 提取模型定位事实卡（源码阅读型，无需 GPU）**

1. **实践目标**：训练"从官方文档精确提取关键事实并注明出处"的能力，这是阅读一切模型仓库的第一步。
2. **操作步骤**：
   - 打开 [README.md:L13-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L13-L33)（Introduction 章节），通读一遍。
   - 新建一个笔记文件，抄录下面事实卡模板，把空格填上（内容全部来自这三段原文）：

     | 事实 | 数值 / 结论 | 出处行号 |
     | :--- | :--- | :--- |
     | 模型类型 | 开源 ___ 视觉语言模型 | L15 |
     | 语言解码器激活参数 | ___ | L15 |
     | 上下文窗口 | ___ | L23 |
     | 视觉编码器名称及特点 | ___ | L23 |
     | Thinking 变体的两种后训练手段 | ___ 和 ___ | L25 |
     | 2506 版思维链平均长度变化 | 减少 ___ | L29 |

   - 用 `grep -n "2.8B" README.md`（在仓库根目录执行）核对每个数字的行号是否与你填的一致。
3. **需要观察的现象**：`grep -n` 输出的行号应与事实卡中的出处列一致；特别注意 2.8B 只出现在 Introduction 正文，而变体表（第 4.2 节）用的是 3B。
4. **预期结果**：完成一张六行的事实卡，每个数字都能在 README 中指认出确切行号。（本实践为纯阅读任务，结论可直接与原文核对，无需本地运行环境。）

#### 4.1.5 小练习与答案

**练习 1**：为什么说 Kimi-VL 是"高效"的视觉语言模型？请用总参数、激活参数两个数字说明。

**参考答案**：总参数 16B 而每个 token 仅激活约 2.8B（语言解码器），激活占比约 17.5%。MoE 稀疏激活让模型容量与推理开销解耦——能力按 16B 积累，计算按约 3B 付费，因此在与 GPT-4o-mini、Qwen2.5-VL-7B 等同量级模型的对比中保持了竞争力（README L15、L21）。

**练习 2**：`Kimi-VL-A3B` 中的 "A3B" 是什么意思？

**参考答案**：A = Approximately（大约），3B 指语言解码器激活参数约 30 亿（精确值 2.8B，见 README L15；变体表格中取整写作 3B）。命名强调的是"推理时实际动用"的参数规模，而非 16B 的总参数。

**练习 3**：MoonViT 的"原生分辨率"编码解决了什么问题？

**参考答案**：传统 VLM 常把输入图片统一缩放到固定分辨率（如 448×448），高分辨率大图会丢失细节、小图则浪费算力。MoonViT 按图片原始分辨率切 patch 编码，因此既能看清超高分辨率输入（InfoVQA 83.2、ScreenSpot-Pro 34.5，README L23），又能在普通图片上保持较低计算成本。

### 4.2 模型变体一览

#### 4.2.1 概念说明

Kimi-VL 家族目前有三个公开变体，全部基于同一套 16B 总参数 / 3B 激活 / 128K 上下文的底座，区别在于**后训练方式**带来的行为差异：

| 变体 | 状态 | 一句话定位 |
| :--- | :--- | :--- |
| `Kimi-VL-A3B-Instruct` | 在用 | 通用指令模型：多模态感知理解、OCR、长视频长文档、OS-Agent 的高效首选 |
| `Kimi-VL-A3B-Thinking-2506` | 在用（2025-06-21 发布） | 长思维链模型：推理更强，同时感知理解能力不输 Instruct 版 |
| `Kimi-VL-A3B-Thinking` | **已废弃（deprecated）** | 旧版思维链模型，被 2506 版取代，不要再选用 |

2506 版相对旧版 Thinking 的四项官方改进（README L28-L33）：

1. **想得更聪明、花得更少**：MathVision 56.9（+20.1）、MathVista 80.1（+8.4）、MMMU-Pro 46.3（+3.2）、MMMU 64.0（+2.1），且平均思维链长度缩短 20%。
2. **带着思考看得更清**：在 MMBench-EN-v1.1、MMStar、RealWorldQA、MMVet 等通用感知基准上追平甚至超过 Instruct 版。
3. **扩展到视频场景**：VideoMMMU 65.2（开源新 SOTA）、Video-MME 71.9。
4. **支持更高分辨率**：单图最高 3.2M 总像素（1792×1792），是原版的 4 倍；V* Benchmark 83.2、ScreenSpot-Pro 52.8、OSWorld-G 52.5。

采样参数的官方推荐（区别对待两类变体）：**Thinking 类模型 `Temperature = 0.8`，Instruct 类模型 `Temperature = 0.2`**。直觉上：思维链模型需要更高温度保持探索的多样性，指令模型需要低温保证输出的稳定确定。

#### 4.2.2 核心流程

面对一个实际任务时的变体选择流程：

```
你的任务是什么类型？
│
├─ 通用看图理解 / OCR / 长视频 / 长文档 / OS-Agent 操作
│   ├─ 追求推理速度与效率 ──────────→ Instruct（temperature=0.2）
│   └─ 同时想要较强推理能力 ────────→ Thinking-2506（temperature=0.8）
│
├─ 数学 / 逻辑 / 多步推理类问题
│   └──────────────────────────────→ Thinking-2506（temperature=0.8）
│
└─ 在任何场景下 ──────────────────→ ✗ 不要用旧版 Thinking（已废弃）
```

#### 4.2.3 源码精读

**① 变体对照表——本仓库最核心的一张表**

[README.md:L57-L61](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L57-L61) 用表格列出三个变体的总参数（均为 16B）、激活参数（均为 3B）、上下文长度（均为 128K）和 HuggingFace 下载链接，其中第三行明确标注 `Kimi-VL-A3B-Thinking (deprecated)`。注意：**三个变体的规格完全相同**，差异只来自后训练，所以选型的依据是"行为"而不是"参数规模"。

**② 官方选型建议**

[README.md:L53](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L53) 给出官方推荐：常规多模态感知理解、OCR、长视频长文档、视频感知、OS-Agent 场景推荐 `Kimi-VL-A3B-Instruct` 做高效推理；新的 `Kimi-VL-A3B-Thinking-2506` 在保持同等感知能力的同时推理更强。这正是 4.2.2 选择流程的原文依据。

**③ Temperature 推荐值**

[README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68) 以 Note 形式给出两条采样参数建议：Thinking 模型用 `Temperature = 0.8`，Instruct 模型用 `Temperature = 0.2`。后续跑推理示例时（第 2 单元）会看到代码里正是这么传的。

**④ 版本时间线**

[README.md:L47-L49](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L47-L49) 的 News 章节记录了三个关键时间点：2025-06-21 发布 Thinking-2506；2025-04-15 vLLM 官方支持 Kimi-VL 部署；2025-04-14 LLaMA-Factory 支持微调。由此能看出仓库的维护节奏：模型发布在先，生态支持（推理框架、微调框架）紧随其后。

#### 4.2.4 代码实践

**实践：对比两个变体的官方推理代码差异（源码阅读型）**

1. **实践目标**：从 README 的两段官方示例代码中，亲眼看到"变体差异"如何落到具体参数上。
2. **操作步骤**：
   - 阅读 Instruct 示例 [README.md:L113-L154](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L113-L154) 与 Thinking-2506 示例 [README.md:L156-L201](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L156-L201)。
   - 重点比对 `model.generate(...)` 这一行：Instruct 版（L146）是 `max_new_tokens=512`；Thinking-2506 版（L193）是 `max_new_tokens=32768, temperature=0.8`。
   - 再比对输入图片：Instruct 版单图（L139-L140），Thinking-2506 版是两张图组成的列表（L181-L182）。
   - 在仓库根目录执行 `grep -n "max_new_tokens\|temperature" README.md`，核对上面每一处行号。
3. **需要观察的现象**：grep 会显示 `max_new_tokens` 恰好出现两次，且 Thinking 版的生成预算（32768）是 Instruct 版（512）的 64 倍；`temperature` 只在 Thinking 版显式传入。
4. **预期结果**：得出结论——思维链模型需要为"长思考"预留大得多的生成预算，这与 4.2.1 中"2506 版减少 20% 思维链长度仍是改进项"的背景相呼应。（本实践为纯阅读任务，可直接与原文核对。）

#### 4.2.5 小练习与答案

**练习 1**：三个变体的总参数、激活参数、上下文长度分别是多少？为什么规格完全相同却要分出三个变体？

**参考答案**：均为总参数 16B、激活参数 3B、上下文 128K（README L57-L61）。规格相同是因为三者共享同一底座，差异全部来自后训练：Instruct 走指令对齐，Thinking/2506 走长思维链 SFT + RL（README L25）。变体区分的是"行为模式"（直接回答 vs 先长思考再回答），不是模型规模。

**练习 2**：旧版 `Kimi-VL-A3B-Thinking` 为什么被废弃？新版本在哪四个方面改进了？

**参考答案**：被 2506 版全面取代（表格标注 deprecated）。四方面改进（README L28-L33）：① 推理精度更高且思维链平均短 20%；② 感知理解追平/超过 Instruct 版，不再是"偏科生"；③ 扩展到视频推理（VideoMMMU 65.2 开源 SOTA）；④ 单图分辨率上限提升 4 倍到 3.2M 像素（1792×1792）。

**练习 3**：你要做两个应用——(a) 商品图 OCR 提取文字；(b) 看几何图解题。各选哪个变体、什么 Temperature？

**参考答案**：(a) 选 `Kimi-VL-A3B-Instruct`，Temperature = 0.2——OCR 属于通用感知任务，低温输出稳定；(b) 选 `Kimi-VL-A3B-Thinking-2506`，Temperature = 0.8——多步推理任务需要思维链模型，官方对 Thinking 类的推荐温度就是 0.8（README L53、L65-L68）。

### 4.3 仓库形态与代码位置

#### 4.3.1 概念说明

这是本讲最容易被初学者忽略、却最重要的一个认知：**本仓库是一个"模型发布仓库"，不是传统意义上的"代码仓库"**。

在仓库根目录执行 `ls`，你只会看到：

```
Kimi-VL.pdf        # 技术报告（约 11MB）
LICENSE            # MIT 许可证
README.md          # 主文档
figures/           # 架构图 + 示例图片 + 性能对比图（7 张）
requirements.txt   # 8 行依赖清单
Kimi-VL-tutorial/  # 你正在阅读的讲义目录（本教程生成）
```

没有 `src/`、没有 `setup.py`、没有任何 `.py` 源码文件。那么模型的实现代码（MoonViT 怎么编码、MoE 怎么路由）在哪里？

答案是：**在 HuggingFace 模型仓库里**，例如 [moonshotai/Kimi-VL-A3B-Instruct](https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct)。HuggingFace 的模型仓库除了权重文件（`.safetensors`），还可以携带自定义 Python 代码。当 `from_pretrained()` 传入 `trust_remote_code=True` 时，transformers 库会：

1. 读取模型仓库中的配置文件，发现该模型没有对应的内置实现；
2. 从模型仓库**下载自定义的建模代码**（若干 `.py` 文件）；
3. 动态加载这些代码，用它们来实例化模型和处理器。

所以 README 中所有示例代码都带着 `trust_remote_code=True`——没有它，transformers 不知道 Kimi-VL 的网络结构该怎么搭。这也是一种提醒：`trust_remote_code` 意味着"信任并执行远端仓库的代码"，只应对可信来源（如此处的官方 moonshotai 账号）开启。

#### 4.3.2 核心流程

从 GitHub 仓库到一次成功推理的完整链路：

```
本 GitHub 仓库                     HuggingFace 模型仓库
┌─────────────────────┐          ┌─────────────────────────────┐
│ README.md           │          │ 权重文件 *.safetensors       │
│  └─ 示例代码         │──下载──→ │ 模型代码 *.py（自定义实现）   │
│ requirements.txt    │          │ 配置 config.json、tokenizer  │
│  └─ 依赖清单         │          │ chat 模板等                  │
└─────────────────────┘          └─────────────────────────────┘
        │                                   │
        ▼                                   ▼
   pip install 依赖              trust_remote_code=True 动态加载
        └──────────────────┬────────────────┘
                           ▼
                 AutoModelForCausalLM / AutoProcessor
                           ▼
                    本地/服务端推理
```

要点：GitHub 仓库提供"使用说明书 + 依赖清单"，HuggingFace 仓库提供"权重 + 实现"，两者缺一不可。

#### 4.3.3 源码精读

**① trust_remote_code 的真实出现位置**

[README.md:L121-L126](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L121-L126) 是 Instruct 示例的模型加载代码：`AutoModelForCausalLM.from_pretrained(model_path, torch_dtype="auto", device_map="auto", trust_remote_code=True)`。由于 transformers 内置模型清单里没有 Kimi-VL，这里的 `trust_remote_code=True` 不可省略——正是它触发了从 HuggingFace 仓库下载自定义建模代码的过程。

[README.md:L137](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L137) 表明加载处理器（processor）同样需要 `trust_remote_code=True`：`AutoProcessor.from_pretrained(model_path, trust_remote_code=True)`。处理器负责把图片和文字组装成模型输入张量，其实现同样来自远端代码。

[README.md:L226-L231](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L226-L231) 显示 vLLM 部署路径也不例外：`LLM(model_path, trust_remote_code=True)` 和 `AutoProcessor.from_pretrained(model_path, trust_remote_code=True)`。无论用哪种推理引擎，"实现代码在远端"这一事实不变。

**② 极简依赖清单侧面印证仓库形态**

[requirements.txt:L1-L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L1-L8) 全部内容只有 8 项：`torch==2.5.1`、`torchvision==0.20.1`、`transformers==4.51.3`、`pillow`、`tiktoken`、`accelerate`、`blobfile`、`openai`。没有任何本项目自有的包——因为要运行的代码全部来自 transformers 生态和 HuggingFace 远端，这个仓库本身不发布 Python 包。依赖解读和安装实战放在下一讲（u1-l2）。

**③ 仓库自带的图像资产**

[figures/](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures) 目录下共 7 张图片：`arch.png`（架构图）、`demo.png`（单图示例，穹顶建筑）、`demo1.png` 与 `demo2.png`（多图示例，手稿推断）、`instruct_perf.png` 与 `thinking_perf.png`（性能对比图）、`logo.png`。README 的推理示例直接以 `./figures/demo.png` 等相对路径引用它们（L139、L181），因此克隆本仓库后即可原样运行示例，无需另找测试图片。

**④ 生态入口预告**

[README.md:L205-L207](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L205-L207)（微调）与 [README.md:L211-L264](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L211-L264)（部署）说明：微调由外部框架 LLaMA-Factory 支持，部署由 vLLM 支持——这些能力同样不在本仓库内实现，仓库只负责给出对接方式。第 3 单元会逐一展开。

#### 4.3.4 代码实践

**实践：亲手验证"代码不在本仓库"（源码阅读型 + 网页验证）**

1. **实践目标**：通过三个独立证据确信"本仓库不含模型实现代码，实现位于 HuggingFace"，并找到远端代码的具体文件。
2. **操作步骤**：
   - 在仓库根目录执行 `ls`，确认根目录只有 README.md、Kimi-VL.pdf、LICENSE、requirements.txt、figures/（以及本教程目录）。
   - 执行 `find . -name "*.py" -not -path "./.git/*"`，确认整个仓库没有一个 Python 源文件。
   - 执行 `grep -n "trust_remote_code" README.md`，数一数有效调用（不含注释）出现几处、分别在哪些行。
   - 打开浏览器访问 HuggingFace 模型页 <https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct>，进入 **Files and versions** 标签页，浏览文件列表。
3. **需要观察的现象**：
   - `find` 命令无任何 `.py` 输出；
   - `grep` 显示 `trust_remote_code` 出现在 L125、L133（注释）、L137、L168、L176（注释）、L179、L228、L231——去掉两处注释后共 **6 处有效调用**，覆盖模型加载、处理器加载和 vLLM 三条路径；
   - HuggingFace 页面的文件列表中除了权重和配置，还有若干 `.py` 文件——它们就是 `trust_remote_code=True` 实际下载执行的代码。
4. **预期结果**：把模型页上看到的 `.py` 文件名抄录下来（通常包含模型配置、建模、处理器实现等几类；具体文件名以页面实际显示为准，待本地验证）。对照 4.3.1 的机制描述，你应该能回答"transformers 是从哪里知道 Kimi-VL 网络结构的"。

#### 4.3.5 小练习与答案

**练习 1**：为什么本仓库没有一个 `.py` 文件，README 里的示例代码却可以运行？

**参考答案**：示例代码只依赖 transformers / vLLM 等第三方库，而 Kimi-VL 模型本身的实现代码和权重存放在 HuggingFace 模型仓库中，通过 `trust_remote_code=True` 在 `from_pretrained()` 时动态下载加载（README L125、L137、L228、L231）。GitHub 仓库的角色是"使用说明书"。

**练习 2**：如果把示例中的 `trust_remote_code=True` 去掉，预计会发生什么？

**参考答案**：transformers 在内置模型清单中找不到 Kimi-VL 的注册实现，无法根据 `config.json` 实例化模型，会抛出要求显式信任远端代码的错误（或提示该架构需要 `trust_remote_code`），加载失败。（具体报错文案与 transformers 版本有关，待本地验证。）

**练习 3**：`trust_remote_code=True` 有什么安全上的含义？什么时候可以放心开启？

**参考答案**：它意味着"下载并执行模型仓库里的任意 Python 代码"，属于代码执行授权。只应在模型来源可信时开启——例如本仓库示例对应的官方 `moonshotai` 账号发布的模型；对来源不明的第三方模型随意开启相当于在本地执行陌生人代码。

## 5. 综合实践

**任务：制作「Kimi-VL 变体规格卡」并定位远端实现代码**

把本讲三个模块的产出合并成一份可长期查阅的文档（建议存为 `Kimi-VL-tutorial/my-notes/variants.md`，与讲义放在一起）：

1. **变体规格表**：通读 [README.md:L51-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L51-L68) 后，填写下表（参考答案已给出，请先独立填写再核对）：

   | 模型 | 总参数量 | 激活参数量 | 上下文长度 | 推荐 Temperature |
   | :--- | :---: | :---: | :---: | :---: |
   | Kimi-VL-A3B-Thinking-2506 | 16B | 3B（正文精确值：语言解码器 2.8B） | 128K | 0.8 |
   | Kimi-VL-A3B-Instruct | 16B | 3B（同上） | 128K | 0.2 |
   | Kimi-VL-A3B-Thinking（已废弃） | 16B | 3B（同上） | 128K | 0.8（Thinking 类） |

   注意表中最右列的依据是 [README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68) 的 Note（原文只分 Thinking / Instruct 两类，废弃变体属 Thinking 类）。

2. **远端代码定位**：按 4.3.4 的步骤打开 HuggingFace 模型页 `moonshotai/Kimi-VL-A3B-Instruct` 的 Files and versions 标签，把所有 `.py` 文件名抄进规格卡，并在每个文件名后注明你猜测的职责（配置 / 建模 / 处理器……）。具体文件名以页面实际显示为准（待本地验证）。
3. **自检问题**（写在规格卡末尾）：如果明天要在无 GPU 的笔记本上向同事介绍 Kimi-VL，你只带这一份规格卡够不够？还缺哪些信息？（提示：想想 4.1 的三大能力、4.2 的选型流程。）

**预期结果**：一张三行规格卡 + 一份 `.py` 文件清单 + 一段自检反思。此后每读一个新的模型仓库，你都可以复用"事实卡 → 规格表 → 代码定位"这套三步法。

## 6. 本讲小结

- **Kimi-VL 是月之暗面开源的 MoE 视觉语言模型**：16B 总参数、语言解码器仅激活约 2.8B（A3B ≈ 3B）、128K 上下文，主打多模态推理、长上下文和 Agent 三大能力。
- **架构三件套**：MoonViT 原生分辨率视觉编码器 → MLP 投影层 → MoE 语言解码器，高层流程图与 [figures/arch.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/arch.png) 对应。
- **三个变体规格相同、行为不同**：Instruct（temperature=0.2）面向通用感知与效率，Thinking-2506（temperature=0.8）面向深度推理且感知不缩水，旧版 Thinking 已废弃。
- **本仓库是发布型仓库**：只有文档、技术报告、图片和 8 行依赖清单，没有任何 `.py` 源码；模型实现与权重在 HuggingFace，靠 `trust_remote_code=True`（README 中共 6 处有效调用）动态加载。
- **学习方法论**：读模型仓库先做"事实卡 → 规格表 → 代码定位"，每个数字标注出处行号，遇到口径不一致（2.8B vs 3B）记录而不是忽略。

## 7. 下一步学习建议

- **下一讲（u1-l2）环境搭建**：动手创建 conda 环境并安装 [requirements.txt](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt) 中的依赖，搞清 torch / transformers / accelerate 各自在推理链路中的角色。
- **u1-l3 仓库结构与资产**：更系统地建立仓库文件地图，并初探 [Kimi-VL.pdf](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/Kimi-VL.pdf) 技术报告的章节结构，为进阶精读做索引。
- **提前翻看**：[README.md:L96-L201](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L96-L201) 的两段推理示例代码——第 2 单元将逐行拆解它们，如果你已能看懂七八成，说明本讲目标达成了。
