# 模型变体选择与采样参数：Temperature、上下文长度与分辨率规格

## 1. 本讲目标

前几讲我们已经能跑通 Kimi-VL 的单图问答（u2-l1）、多图与 Thinking 推理（u2-l2）、并拆解了输入组装机制（u2-l3）。本讲把视角从「怎么跑」提升到「怎么选、怎么配」——也就是工程选型问题。学完本讲，你应该能够：

1. 读懂 README 中的模型变体对比表，并根据任务类型（通用理解 / 深度推理 / Agent / 高分辨率 / 视频）选出合适的变体，说清楚旧版 Thinking 为什么被废弃。
2. 记住并理解官方推荐采样参数：Instruct 用 `temperature=0.2`、Thinking 用 `temperature=0.8`，以及两者 `max_new_tokens` 预算的巨大差异（512 vs 32768）；理解温度在数学上如何改变采样分布。
3. 理解 128K 上下文窗口与 2506 版 3.2M 像素（1792×1792）输入上限对实际任务的意义，并能对应到 vLLM 部署参数（`--max-model-len`、`--limit-mm-per-prompt`）上。

本讲的三个最小模块：**变体能力矩阵**、**采样参数建议**、**上下文与分辨率规格**。

## 2. 前置知识

本讲不再涉及新的代码链路，但需要用到前面几讲建立的几个概念，先用通俗语言复习一遍：

- **总参数 vs 激活参数（MoE 稀疏激活）**：Kimi-VL 总参数 16B，但语言解码器每个 token 只激活约 2.8B 参数（A3B 的含义就是 "about 3B"）。README 表格里写 3B、正文写 2.8B，是约数与精确数的两种口径。这一讲只需要记住：**三个变体的「身材」完全一样，差异全部来自后训练**。
- **temperature（温度）**：生成式模型每一步先输出整个词表上的原始得分（logit），再经 softmax 变成概率分布，从中采样下一个 token。温度 \( T \) 就是 softmax 里除在 logit 上的那个缩放系数：\( T \) 越小分布越「尖」（倾向选最高分），\( T \) 越大分布越「平」（采样更随机）。4.2 节会给出公式。
- **do_sample 与贪心解码**：在 transformers 的 `generate` 里，只有启用随机采样（`do_sample=True`）时 `temperature` 才参与计算；如果是贪心解码（每步取 argmax），传了 `temperature` 也会被忽略。这一点 u2-l1、u2-l2 都提过，本讲的实践脚本会显式写 `do_sample=True`。
- **上下文窗口（context window）**：模型一次请求能处理的最大 token 数，图像编码出的视觉 token 和文字 token **共享同一个预算**。Kimi-VL 是 128K = 131,072 个 token。
- **原生分辨率（native resolution）**：MoonViT 不把图片强行缩放到固定尺寸，而是保留原始分辨率与宽高比，因此**一张图占用的视觉 token 数与它的像素数近似成正比**（u2-l3 已实测过 input_ids 长度随分辨率变化）。这是理解「3.2M 像素上限」为什么和 128K 上下文绑在一起的关键。

如果以上任何一条让你觉得陌生，建议先回到 u1-l1（参数与激活）和 u2-l3（视觉 token 与输入长度）补一下。

## 3. 本讲源码地图

本仓库是发布型仓库（u1-l3 已确认），没有 Python 源码，本讲的「源码」就是 README.md 中的四段官方材料，外加一张实践用图：

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `README.md` 第 1 节 Introduction | L15-L33 | 能力声明：128K 上下文、MoonViT 原生分辨率、2506 版四项改进（含 3.2M 像素） |
| `README.md` 第 4 节 Model Variants | L51-L74 | 变体对比表（16B/3B/128K）、选型建议原文、**官方温度推荐（0.8 / 0.2）** |
| `README.md` 第 6 节 Example usage | L109-L201 | Instruct 与 Thinking-2506 两段推理示例，`generate` 参数是采样建议的「实证」 |
| `README.md` 第 8 节 Deployment | L255-L264 | `vllm serve` 参数：`--max-model-len`、`--limit-mm-per-prompt` 是上下文/图片数规格在部署层的映射 |
| `figures/demo.png` | — | 本讲综合实践使用的官方测试图 |

## 4. 核心概念与源码讲解

### 4.1 变体能力矩阵

#### 4.1.1 概念说明

Kimi-VL 家族目前有三个变体：`Kimi-VL-A3B-Instruct`（指令跟随版）、`Kimi-VL-A3B-Thinking-2506`（长思维链推理版，2025 年 6 月发布）和 `Kimi-VL-A3B-Thinking`（旧版思维链，**已废弃**）。

理解这张「能力矩阵」最重要的前提是：**三个变体的架构、参数量、上下文长度完全相同**，区别不是「大杯中杯小杯」，而是**后训练方式不同**造就的能力分工——Instruct 面向通用感知与理解，Thinking 系列经过长思维链（CoT）监督微调与强化学习（RL），面向深度推理。所以「选变体」选的是**能力与成本的权衡**，而不是模型大小。

#### 4.1.2 核心流程

拿到一个任务后，按下面的决策路径选变体：

```text
任务是什么类型？
├─ 通用多模态感知/理解、OCR、长视频、长文档、视频感知、OS-agent
│   └─ 追求低延迟、低 token 成本 ──→ Kimi-VL-A3B-Instruct
├─ 数学/科学推理、谜题求解、需要一步步推理的复杂任务
│   └─ 需要「先想后答」的长思维链 ──→ Kimi-VL-A3B-Thinking-2506
├─ 既要感知又要推理（混合负载）
│   └─ 官方声明 2506 感知能力已追平或超过 Instruct ──→ 默认选 Thinking-2506
└─ 旧版 Kimi-VL-A3B-Thinking
    └─ 已被 2506 全面超越并标记 deprecated ──→ 不要再新接入
```

「旧版为什么废弃」的依据是 2506 的四项官方改进，可以概括为「更聪明、更省、看得更清、看得更广」：

1. **更聪明且更省 token**：多模态推理基准全面上升，同时平均思维长度**减少 20%**；
2. **带思考的感知**：不再像旧版那样「专精推理」，通用感知理解追平甚至超过 Instruct；
3. **扩展到视频场景**：视频推理与理解基准提升；
4. **扩展到更高分辨率**：单图支持 3.2M 总像素（1792×1792），是初代发布的 4 倍（详见 4.3 节）。

#### 4.1.3 源码精读

**（1）变体对比表：三行规格完全一致，唯一差别是推荐度。**

[README.md:L57-L61](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L57-L61)

这段表格列出三个变体的总参数（均为 16B）、激活参数（均为 3B）、上下文长度（均为 128K）与下载链接；第三行在旧版 Thinking 后明确标注 `(deprecated)`。这张表是「能力矩阵」的定量骨架——它告诉你：**不存在参数量层面的选型，只有后训练能力层面的选型**。

**（2）官方选型建议原文。**

[README.md:L53](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L53)

README 在表格前给出明确分工：**通用多模态感知理解、OCR、长视频与长文档、视频感知、OS-agent 等场景推荐 Instruct 以获得高效推理**（efficient inference，指更快、生成 token 更少）；而 Thinking-2506 在**同样具备**这些感知能力的同时，推理能力更强。翻译成决策规则就是 4.1.2 的决策树：纯感知、成本敏感选 Instruct；要推理、或想一个模型通吃，选 2506。

**（3）2506 的四项改进数据。**

[README.md:L28-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L28-L33)

这四条 `<i>` 列表是 2506 的能力声明，逐条对应：

- [README.md:L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29)：推理精度 MathVision 56.9（**+20.1**）、MathVista 80.1（+8.4）、MMMU-Pro 46.3（+3.2）、MMMU 64.0（+2.1），同时平均**减少 20% 思维长度**——更准还要更省。
- [README.md:L30](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L30)：MMBench-EN-v1.1 84.4、MMStar 70.4、RealWorldQA 70.0、MMVet 78.4，声明**追平甚至超过 Instruct**——这直接支撑「默认选 2506」的决策。
- [README.md:L31](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L31)：视频场景 VideoMMMU 65.2、Video-MME 71.9。
- [README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32)：3.2M 总像素（1792×1792），为初代 4 倍，带动 V* 83.2、ScreenSpot-Pro 52.8、OSWorld-G 52.5。

**（4）旧版 Thinking 的原始成绩（废弃判断的「对照组」）。**

[README.md:L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25)

Introduction 里记录了旧版 Thinking 的成绩：MMMU 61.7、MathVision 36.8、MathVista 71.3。与 L29 的提升幅度对照：MathVision 36.8 + 20.1 = 56.9，**严格吻合**；MathVista 71.3 + 8.4 = 79.7 ≈ 80.1、MMMU 61.7 + 2.1 = 63.8 ≈ 64.0，略有出入（可能两次评测口径存在细微差异）。这个对照练习说明：阅读发布型仓库时要养成「正文声明 ↔ 数据自洽性」交叉验证的习惯，对不上号的数字保留怀疑、以待确认。

#### 4.1.4 代码实践

**实践目标**：把 README 的能力矩阵内化成自己的选型决策清单。

**操作步骤**：

1. 通读 [README.md:L15-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15-L33) 与 [README.md:L51-L74](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L51-L74)。
2. 为下面 5 个任务各选一个变体并写出一行理由（答案见 4.1.5）：
   - a. 给电商截图批量打标签（每张图一句话）；
   - b. 解一道几何竞赛题（图中含几何图形）；
   - c. 80 页 PDF 扫描件跨页问答总结；
   - d. 手机截屏上的 UI 元素定位（OS-agent grounding）；
   - e. 2023 年的训练代码想升级到最新变体，当前用的是 `Kimi-VL-A3B-Thinking`。
3. 打开 HuggingFace 模型页 [moonshotai/Kimi-VL-A3B-Thinking-2506](https://huggingface.co/moonshotai/Kimi-VL-A3B-Thinking-2506#2-performance)，把 README 第 5 节性能图（[README.md:L79](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L79) 指向的链接）里的 2506 成绩抄进你自己的对比表。

**需要观察的现象**：自己最初的选择与官方 L53 的推荐文字是否一致；不一致时是哪个能力维度判断错了。

**预期结果**：5 个任务中 a、c、d 的「成本敏感/纯感知」属性会导向 Instruct，b 导向 Thinking-2506，e 必须迁移（旧版 deprecated）。第 3 步的具体分数以模型卡页面实际内容为准，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：三个变体的总参数、激活参数、上下文长度有区别吗？那它们的差异从哪来？

**答案**：没有任何区别，均为 16B 总参数 / 3B 激活参数 / 128K 上下文（README L57-L61）。差异全部来自后训练：Thinking 系列经过长思维链 SFT 与 RL，获得了「先想后答」的推理能力。

**练习 2**：旧版 Thinking 为什么被废弃？用一句话概括。

**答案**：2506 在推理精度全面更高的同时平均思维长度还少 20%，感知能力追平 Instruct，并额外扩展了视频与高分辨率能力——旧版被全方位超越，没有保留价值。

**练习 3**：任务 c（80 页 PDF 问答）如果用 vLLM 服务部署，除了选变体还要注意什么规格参数？

**答案**：80 页是多图输入，默认的 `--limit-mm-per-prompt image=64` 可能不够，需要按 README L257 的注释调到 256 或 512；同时多图 + 长文档会占满 128K 上下文预算，可能还要按 L256 的注释放开 `--max-model-len`（见 4.3 节）。

### 4.2 采样参数建议

#### 4.2.1 概念说明

采样参数决定模型「从一个分布里怎么挑下一个 token」。README 对 Kimi-VL 只正式推荐了一个参数——温度：

- **Thinking 系列模型：`Temperature = 0.8`**
- **Instruct 系列模型：`Temperature = 0.2`**

直觉上：**感知求稳，推理求活**。Instruct 承担识别、OCR、描述这类「有标准答案」的任务，低温让输出集中、可复现；Thinking 模型要生成成千上万个 token 的推理链，适当高温带来多样性，避免长思维链陷入重复、僵化的推理路径。需要说明：README 只给了推荐值没有给理由，以上「求稳/求活」的解读是社区通行理解（推断），官方动机以其技术报告为准。

除温度外，README 示例里出现的另一个关键生成参数是 `max_new_tokens`：Instruct 示例只给 512，Thinking-2506 示例给到 32768——差 64 倍的生成预算，同样是变体分工的直接体现。

#### 4.2.2 核心流程

温度作用在 softmax 上。设某步生成的原始 logit 为 \( z_i \)（词表大小 \( V \)），温度 \( T > 0 \)：

\[ P(i) = \frac{\exp(z_i / T)}{\sum_{j=1}^{V} \exp(z_j / T)} \]

- \( T \to 0^+ \)：分布退化到 argmax（等价贪心解码）；
- \( T = 1 \)：不缩放，模型原始分布；
- \( T \) 越大：分布越平坦，低分 token 越有机会被采到。

用「前两名 logit 之差 \( \Delta z \)」看得更具体，两者的概率比为：

\[ \frac{P_1}{P_2} = \exp\!\left(\frac{\Delta z}{T}\right) \]

取 \( \Delta z = 1.0 \)：\( T = 0.2 \) 时概率比约为 \( e^{5} \approx 148:1 \)（几乎锁死第一名）；\( T = 0.8 \) 时约为 \( e^{1.25} \approx 3.5:1 \)（第二名仍有约两成的机会）。这就是 0.2 与 0.8 在数字上的实际分量。

采样在生成循环中的位置（伪代码）：

```text
for step in range(max_new_tokens):
    logits = model.forward(已生成的全部 token)          # 最后一步的词表得分
    probs  = softmax(logits / temperature)              # ← 温度在这里生效
    next   = sample(probs)   若 do_sample=False 则 next = argmax(logits)
    追加 next；若 next 是结束符则提前终止
```

注意两个工程要点（承接 u2-l1 / u2-l2 的结论）：

1. **`temperature` 只在随机采样开启时生效**。若走贪心解码，温度会被忽略；稳妥写法是显式 `do_sample=True`。模型仓库自带的 `generation_config` 可能已配置采样开关，具体内容**待确认**（不在本仓库内）。
2. **README 未推荐 `top_p`、`top_k` 等其它采样参数**，保持默认即可；不要凭空搬运其他模型的采样配置。

#### 4.2.3 源码精读

**（1）官方温度推荐原文。**

[README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68)

紧跟变体表格的 Note 是唯一正式的参数推荐：Thinking 模型 `Temperature = 0.8`，Instruct 模型 `Temperature = 0.2`。这两个数字是本讲的必背项，也是综合实践脚本的参数来源。

**（2）Instruct 示例：`max_new_tokens=512`，未传温度。**

[README.md:L146](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L146)

Instruct 的 `generate` 只限制了 512 个新 token——通用理解类回答短，预算小、成本低、延迟低。示例没有传 `temperature`，此时按 transformers 与模型自带配置的默认行为执行（示例本身即为贪心口径）；要落实官方推荐的 0.2，需要像综合实践脚本那样显式传 `temperature=0.2, do_sample=True`。

**（3）Thinking-2506 示例：`max_new_tokens=32768, temperature=0.8`。**

[README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193)

同一套 `generate` 调用，参数换成 32768 的生成预算和 0.8 的温度，正好是 L65-L68 推荐值的「示例实证」。32768 的一半（16384）已超过整个 128K 窗口的八分之一——长思维链模型就是按「输出可能非常长」来设计的，预算不足会在推理中途截断、丢掉最终结论（u2-l2 已分析过）。

**（4）温度之外的隐性参数：思维长度成本。**

[README.md:L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29)

2506 平均减少 20% 思维长度这条声明，本质是在说 Thinking 模型的** token 成本**：即便推荐了 32768 的上限，实际消耗仍决定于思维链平均长度。选变体时要把「每题多付多少 token」计入成本模型。

#### 4.2.4 代码实践

**实践目标**：用同一张图、同一个问题，直观感受 0.2 与 0.8 两种温度下输出稳定性的差异（需 GPU 环境；无 GPU 可改读 4.2.5 练习 3 的对照表）。

**操作步骤**：

1. 基于 [README.md:L115-L154](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L115-L154) 的 Instruct 示例（加载与输入构造部分照抄），把 `generate` 一行改为：

   ```python
   # 示例代码：在官方 Instruct 示例基础上修改 generate 参数
   generated_ids = model.generate(
       **inputs, max_new_tokens=256,
       do_sample=True, temperature=0.2,   # 之后改为 0.8 再跑三遍
   )
   ```

2. 分别在 `temperature=0.2` 与 `temperature=0.8` 下各运行 3 次（共 6 次），用 `diff` 或肉眼对比每次的回答文本。

**需要观察的现象**：0.2 的三次回答是否几乎逐字一致？0.8 的三次回答在用词、句式上是否出现可察觉的波动甚至不同的结论？

**预期结果**：低温组稳定性明显高于高温组（同一张穹顶建筑图，0.2 组答案应高度一致；0.8 组允许出现不同的描述顺序或表述）。具体差异程度**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么感知类任务（OCR、描述）适合低温，而长思维链推理反而要较高温？

**答案**：感知任务输出短、答案客观，低温（0.2）让分布集中在最优 token 上，输出稳定可复现；思维链动辄数千 token，过高确定性容易让模型在长路径上重复僵化，适当高温（0.8）保留探索多样性。这是官方推荐值的通行解释（推断口径）。

**练习 2**：README 的 Thinking 示例在 L193 只传了 `temperature=0.8` 没传 `do_sample`，直接照抄会有什么隐患？

**答案**：在 transformers 中若最终走的是贪心解码，`temperature` 不参与计算，实际效果与推荐配置不符。稳妥做法是显式加 `do_sample=True`（本讲综合实践脚本就是这么写的）。

**练习 3**：Instruct 示例 `max_new_tokens=512`，Thinking 示例给 32768，为什么差 64 倍？

**答案**：Instruct 直接作答，短输出即可；Thinking 要先生成长思维链再给结论，预算不足会截断在推理中途丢失最终答案。32768 是为「最坏情况」准备的生成预算，实际消耗由思维链长度决定（2506 平均已缩短 20%）。

### 4.3 上下文与分辨率规格

#### 4.3.1 概念说明

如果说变体选择是「选大脑」、采样参数是「调状态」，那么上下文与分辨率规格决定的是**一次请求能装进多少东西**：

- **128K 上下文窗口**：一次请求中（多张图的视觉 token + 提示文本 + 历史对话 + 将要生成的输出）总和不能超过 131,072 个 token。它是长视频、长文档任务的地基。
- **3.2M 总像素上限（1792×1792）**：2506 版单张图片最多支持约 3.2M（3,211,264）像素，是初代发布（约 0.8M，即 3.2M÷4）的 4 倍。由 [README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32) 声明。
- **两者强关联**：因为 MoonViT 是原生分辨率编码器（u2-l3 已确认 input_ids 长度与分辨率成正比），高分辨率图片会被编码成大量视觉 token，直接挤占 128K 预算。**分辨率上限必须放在一个足够大的上下文窗口里才有意义**——这就是为什么 4 倍像素提升与 128K 长上下文是配套能力。

#### 4.3.2 核心流程

一次多模态请求的「预算记账」：

```text
总预算：128K = 131,072 token
├─ 每张图 → MoonViT 按原生分辨率切 patch → N_i 个视觉 token（N_i ∝ 像素数）
├─ 图 × K 张 → Σ N_i 个视觉 token（受 --limit-mm-per-prompt 的 K 上限约束）
├─ 文本提示 + 对话历史 → M 个文本 token
└─ 生成输出 → 最多 max_new_tokens 个（Instruct 512 / Thinking 32768）
约束：Σ N_i + M + 生成 ≤ 131,072
```

两点补充说明：

1. **视觉 token 与像素的精确换算比例**（patch 大小、是否合并 token）取决于 MoonViT 的实现，本仓库无实现代码，**待确认**，留待 u4-l1 精读技术报告时补齐。本讲只需建立「像素↝token 线性、共享 128K 预算」的定性模型。
2. **部署层的规格映射**：vLLM 的 `--max-model-len` 是服务实际放开的窗口长度，`--limit-mm-per-prompt image=N` 是单条请求的图片数上限。KV cache 显存随窗口长度线性增长（通用背景知识，非本仓库声明），所以示例默认只开 32768，需要时再放大——这是「规格上限」与「部署成本」之间的旋钮。

#### 4.3.3 源码精读

**（1）128K 上下文与原生分辨率的能力声明。**

[README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23)

Introduction 同时给出两个数字的「成绩单」：128K 扩展上下文带来 LongVideoBench 64.5、MMLongBench-Doc 35.1；MoonViT 原生分辨率带来 InfoVQA 83.2、ScreenSpot-Pro 34.5，且普通分辨率输入下计算成本更低——注意「高分辨率看得清」与「常规图不浪费算力」是原生分辨率设计的一体两面。

**（2）3.2M 像素（1792×1792）与 4 倍提升。**

[README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32)

2506 支持单图 3.2M 总像素（1792×1792），相对初代 4 倍；由此带来高分辨率感知与 OS-agent 定位基准的提升：V* 83.2、ScreenSpot-Pro 52.8、OSWorld-G 52.5（对照 L23 里初代 ScreenSpot-Pro 34.5，提升 18.3 分，是「分辨率×上下文」配套升级收益的直接证据）。

**（3）vLLM 部署：窗口与图片数两个旋钮。**

[README.md:L256-L257](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L256-L257) 与 [README.md:L260](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L260)

启动命令前的两行注释是规格参数的「官方调节指南」：需要更长上下文就把 `--max-model-len` 与 `--max-num-batched-tokens` 设到 **131072**（即 128K，与 L57-L61 表格严格一致：131,072 = 128×1024）；需要更多输入图就把 `--limit-mm-per-prompt` 调到 `image=256` 或 `512`。实际命令（L260、L263）默认开的是 `--max-model-len 32768 --limit-mm-per-prompt image=64`——默认值偏保守，正是因为窗口越大 KV cache 显存越高。

**（4）生成预算也在窗口内。**

[README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193)

Thinking-2506 的 `max_new_tokens=32768` 占满 128K 窗口的四分之一。输入（视觉+文本）与输出共享窗口，多图高分辨率的输入越重，留给思维链的输出空间就越小——这是「上下文/分辨率/采样」三组规格在一条请求里最终汇合的地方。

#### 4.3.4 代码实践

**实践目标**：给「分辨率↝视觉 token↝上下文预算」建立量化手感。本实践**只需要 processor，不需要加载模型权重、不需要 GPU**（承接 u2-l3 的结论）。

**操作步骤**：

1. 运行下面脚本（示例代码，基于 u2-l3 的 processor 实践改写）：

   ```python
   from PIL import Image
   from transformers import AutoProcessor

   processor = AutoProcessor.from_pretrained(
       "moonshotai/Kimi-VL-A3B-Instruct", trust_remote_code=True
   )

   image = Image.open("./figures/demo.png")
   w, h = image.size
   print(f"原始尺寸: {w}x{h} = {w*h} 像素（2506 上限 3,211,264 ≈ 3.2M）")

   def encode(img):
       messages = [{"role": "user", "content": [
           {"type": "image", "image": "./figures/demo.png"},
           {"type": "text", "text": "hi"},
       ]}]
       text = processor.apply_chat_template(
           messages, add_generation_prompt=True, return_tensors="pt"
       )
       inputs = processor(images=img, text=text, return_tensors="pt")
       return inputs["input_ids"].shape[-1]

   n_full = encode(image)
   n_half = encode(image.resize((w // 2, h // 2)))   # 像素数降为 1/4
   print(f"整图编码 token 数: {n_full}；半尺度图 token 数: {n_half}")
   print(f"占 128K 窗口比例: 整图 {n_full/131072:.2%}，半图 {n_half/131072:.2%}")
   ```

2. 把图片再放大到 1792×1792（`image.resize((1792, 1792))`）编码一次，记录 token 数。
3. 重复第 2 步时改为一次传 4 张、16 张整图（仿照 u2-l2 的多图写法），看 token 数如何逼近 131,072。

**需要观察的现象**：像素降为 1/4 后 token 数下降多少（预期明显下降，因 MoonViT 存在 patch 切分与 token 合并策略，具体比例**待本地验证**）；放大到 3.2M 像素与多图叠加时，`input_ids` 长度向 131,072 逼近的速度。

**预期结果**：整图与半图的 token 数与像素数近似同向变化；3.2M 像素单图的 token 占比已不可忽略，叠加多图时会触发 `truncation=True` 的裁剪或超出窗口。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：1792×1792 是多少像素？这个上限和 128K 上下文是什么关系？

**答案**：\( 1792 \times 1792 = 3{,}211{,}264 \approx 3.2 \)M 像素。原生分辨率编码下像素越多视觉 token 越多，这些 token 与文本、生成输出共享 131,072 的窗口预算，所以 4 倍分辨率提升必须放在 128K 长上下文里才能兑现。

**练习 2**：vLLM 示例默认 `--max-model-len 32768`，业务确实需要 128K 时怎么改？

**答案**：按 README L256 的注释，把 `--max-model-len` 和 `--max-num-batched-tokens` 同时设为 131072。窗口放大意味着 KV cache 显存线性增长（通用原理），需要相应预留显存或提高 `--tensor-parallel-size`。

**练习 3**：`--limit-mm-per-prompt image=64` 控制什么？什么时候需要调大？

**答案**：控制单条 prompt 允许携带的最多图片数，防止视觉 token 挤爆上下文与显存。批量文档扫描（如 80 页 PDF）、长视频抽帧等场景需要按 L257 注释调到 256 或 512。

## 5. 综合实践

**任务**：对同一张 `figures/demo.png` 提出同一个推理型问题，让 Instruct（`temperature=0.2`）与 Thinking-2506（`temperature=0.8`）分别作答，把两份回答、生成 token 数与耗时整理成对比表，用自己的数据验证本讲的变体能力矩阵与采样建议。

**准备工作**：u1-l2 的 conda 环境与依赖；能装下单个模型权重的 GPU（bf16 下 16B 权重约 32GB，两模型**依次加载**而非同时驻留）；`figures/demo.png` 位于仓库根目录下。

**操作步骤**：

1. 将下面脚本保存为 `compare_variants.py`（示例代码，由 [README.md:L115-L154](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L115-L154) 与 [README.md:L158-L201](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L158-L201) 两段官方示例合并改写而来）：

   ```python
   import gc, time
   import torch
   from PIL import Image
   from transformers import AutoModelForCausalLM, AutoProcessor

   # 推理型问题：同一张图、同一个问题喂给两个变体
   QUESTION = ("图中的穹顶建筑最可能位于哪个国家？"
               "请结合建筑风格一步步推理，最后单独给出结论。")
   IMAGE_PATH = "./figures/demo.png"

   def run_once(model_path, temperature, max_new_tokens):
       model = AutoModelForCausalLM.from_pretrained(
           model_path, torch_dtype="auto", device_map="auto",
           trust_remote_code=True,
       )
       processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

       image = Image.open(IMAGE_PATH)
       messages = [{"role": "user", "content": [
           {"type": "image", "image": IMAGE_PATH},
           {"type": "text", "text": QUESTION},
       ]}]
       text = processor.apply_chat_template(
           messages, add_generation_prompt=True, return_tensors="pt"
       )
       inputs = processor(
           images=image, text=text, return_tensors="pt",
           padding=True, truncation=True,
       ).to(model.device)

       if torch.cuda.is_available():
           torch.cuda.synchronize()
       t0 = time.perf_counter()
       generated_ids = model.generate(
           **inputs,
           max_new_tokens=max_new_tokens,
           do_sample=True,          # 显式开启采样，温度才会生效（见 4.2）
           temperature=temperature,
       )
       if torch.cuda.is_available():
           torch.cuda.synchronize()
       elapsed = time.perf_counter() - t0

       trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, generated_ids)]
       n_new = trimmed[0].shape[-1]
       response = processor.batch_decode(
           trimmed, skip_special_tokens=True,
           clean_up_tokenization_spaces=False,
       )[0]

       del model, inputs, generated_ids   # 释放显存，给下一个模型腾位置
       gc.collect()
       if torch.cuda.is_available():
           torch.cuda.empty_cache()
       return response, n_new, elapsed

   if __name__ == "__main__":
       plan = [
           ("Instruct",      "moonshotai/Kimi-VL-A3B-Instruct",     0.2, 512),
           ("Thinking-2506", "moonshotai/Kimi-VL-A3B-Thinking-2506", 0.8, 32768),
       ]
       for name, path, temp, budget in plan:
           resp, n_tok, sec = run_once(path, temp, budget)
           print("=" * 60)
           print(f"{name}  temperature={temp}  max_new_tokens={budget}")
           print(f"生成 {n_tok} tokens，耗时 {sec:.1f}s（{n_tok/sec:.1f} tok/s）")
           print("-" * 60)
           print(resp[:2000])   # Thinking 输出很长，先打印前 2000 字
   ```

2. 运行 `python compare_variants.py`。若 Thinking 生成太久，可先把 32768 临时降为 4096 试跑（官方推荐预算仍是 32768，见 4.2.3）。
3. 把结果整理成对比表（模板如下）：

   | 变体 | temperature | 生成 token 数 | 耗时 | 输出结构 | 结论是否正确 |
   | --- | --- | --- | --- | --- | --- |
   | Instruct | 0.2 | 待填 | 待填 | 直接给答案 | 待填 |
   | Thinking-2506 | 0.8 | 待填 | 待填 | 思维链 + 最终结论 | 待填 |

4. 写 100–200 字结论：这次对比里哪个变体更适合该任务？token 成本差多少倍？

**需要观察的现象**：Instruct 是否直接给出简短答案；Thinking-2506 是否先输出长段推理再落到结论；两者 token 数与耗时的量级差距；用 u2-l2 的口径（`skip_special_tokens=False` 查看分界标记）切分 Thinking 输出的「思考」与「答案」两部分。

**预期结果**：Instruct 输出短（数百 token 内）、耗时短；Thinking-2506 输出长（数千 token 级）、耗时长，但对建筑风格的推理过程更完整。具体数值与答案质量**待本地验证**。

## 6. 本讲小结

- **变体能力矩阵**：三个变体规格完全相同（16B 总参 / 3B 激活 / 128K 上下文），差异全部来自后训练；Instruct 面向高效通用感知，Thinking-2506 推理更强且感知已追平 Instruct（默认可选），旧版 Thinking 被全面超越而废弃。
- **采样参数建议**：官方唯一正式推荐是 Thinking `temperature=0.8`、Instruct `temperature=0.2`（README L65-L68）；温度缩放 softmax 分布（\( P(i) \propto \exp(z_i/T) \)），且必须配合 `do_sample=True` 才生效；生成预算 Instruct 512 vs Thinking 32768，差 64 倍。
- **上下文与分辨率规格**：128K = 131,072 token 是视觉 token、文本与生成输出共享的总预算；2506 单图上限 3.2M 像素（1792×1792，初代 4 倍），靠 MoonViT 原生分辨率转化为视觉 token；部署层对应 `--max-model-len`（默认 32768，可开到 131072）与 `--limit-mm-per-prompt`（默认 image=64，可开到 256/512）两个旋钮。
- 阅读发布型仓库时要交叉验证数据自洽性（如 MathVision 36.8+20.1=56.9 严格吻合，个别基准略有出入），不确定处标注待确认。

## 7. 下一步学习建议

本讲是第 2 单元「上手推理」的收尾：你已经会跑推理、会组装输入、会选变体配参数。下一讲进入第 3 单元，建议顺序：

1. **u3-l1 架构解析**：本讲反复出现的「MoonViT 原生分辨率」「MoE 稀疏激活」「视觉 token」将落到架构层面——对照 `figures/arch.png` 与技术报告，弄清 MoonViT 的 patch 切分如何决定视觉 token 数（补上本讲 4.3 节标注的待确认项）。
2. **u3-l2 / u3-l3 vLLM 部署**：本讲 4.3 节的 `--max-model-len`、`--limit-mm-per-prompt` 只是从 README 读到的旋钮，接下来要在离线推理与 OpenAI 兼容服务中实际拧动它们。
3. 带着本讲综合实践的对比表去看 **u4-l2 基准评测分析**：你的小样本体验（Instruct 快而短、Thinking 慢而准）能否和 MathVision、MMMU 等官方基准数据对上，是很好的练手课题。
