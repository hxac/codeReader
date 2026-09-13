# 基准评测与性能分析：读懂 README 里的每一条成绩

## 1. 本讲目标

读完本讲，你应该能够：

1. 把 README 里散落在 Introduction 与两张性能图中的所有基准（MMMU、MathVista、MathVision、InfoVQA、ScreenSpot-Pro、LongVideoBench、OSWorld 等），按「通用理解 / 推理 / 长上下文 / Agent 定位（含高分辨率感知）」归类成结构化表格。
2. 解读 Thinking-2506 相对旧版的提升幅度：看懂 `56.9 on MathVision (+20.1)` 这类「绝对分数 + 官方差值」的写法，会用相对提升公式核算，并识别差值与两端分数可能存在的口径出入。
3. 产出一份数据支撑的「任务类型 → 变体选择 → 参数设置」选型决策清单，把榜单数据转化为工程决策。

本讲是第 4 单元的第二讲。上一讲（u4-l1）精读了技术报告，得到一个关键提醒：报告只覆盖初代模型，2506 版成绩不在其中，引用数字必须对齐评测口径。本讲就把这个「口径对齐」展开成一套完整的读榜方法论。

## 2. 前置知识

### 2.1 什么是基准评测（Benchmark）

基准评测是用一套**固定题目 + 固定判分规则**来给模型打分的标准化考试。README 里出现的每个基准名（如 MMMU、MathVista）都对应一个公开数据集，模型答对的比例就是分数。例如 `64.5 on LongVideoBench` 表示在 LongVideoBench 的题目集上得到 64.5 分（通常是准确率或官方综合口径的分值）。

读榜时要带三个问题：

- **考什么**：每个基准只考察一种能力切片，没有「全科万能分」。
- **跟谁比**：单独一个分数没有意义，必须有对比对象（GPT-4o-mini？70B 级开源模型？上一版自己？）。
- **什么口径**：同一基准在不同设置（是否允许拒绝作答、是否使用工具、提示词格式）下分数会不同。

### 2.2 分数的三种对比方式

| 对比方式 | 含义 | README 中的例子 |
| :--- | :--- | :--- |
| 绝对分数 | 在某基准上的得分 | `83.2 on InfoVQA` |
| 官方差值 | 相对某个基线的提升量，写在括号里 | `56.9 on MathVision (+20.1)` |
| 定性表述 | 不给数字，只说相对位置 | `comparable to flagship models`（OSWorld） |

绝对提升（差值）与相对提升是两回事。相对提升的计算公式为：

\[ \text{相对提升} = \frac{s_{\text{new}} - s_{\text{old}}}{s_{\text{old}}} \times 100\% \]

例如 MathVision 从 36.8 提升到 56.9，绝对提升 \(56.9 - 36.8 = 20.1\)，相对提升 \(20.1 \div 36.8 \approx 54.6\%\)——同一份进步，两种说法观感差别很大，引用时要写清是哪一种。

### 2.3 本讲要用的已学知识

- **变体三件套**（u1-l1）：Instruct、Thinking（旧版，已废弃）、Thinking-2506 规格完全相同（16B 总参 / 约 2.8B 激活 / 128K 上下文），差异全部来自后训练。
- **架构因果链**（u3-l1）：MoonViT 原生分辨率编码，视觉 token 数约为「像素数 ÷ 784」——这就是「分辨率提升 4 倍为什么依赖 128K 长上下文」的物理原因。
- **参数换算**（u2-l4）：视觉 token ≈ 像素数 ÷ 196 ÷ 4，3.2M 像素（1792×1792）的图大约消耗 4096 个视觉 token，全部塞进 128K 的总预算。

## 3. 本讲源码地图

本仓库是发布型仓库（u1-l3），「源码」就是文档与图表本身：

| 文件 | 作用 |
| :--- | :--- |
| [README.md](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md) | 本讲主战场：Introduction（L15-L33）给出绝大多数基准分数与官方口径；Model Variants（L51-L68）给出选型建议与温度参数；Performance（L76-L93）嵌入两张性能图 |
| [figures/instruct_perf.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/instruct_perf.png) | Instruct 变体与 10B 级稠密 VLM、DeepSeek-VL2 (A4.5B) 的多基准横评图 |
| [figures/thinking_perf.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f9c29f125fda/figures/thinking_perf.png) | MathVision 单基准上 Thinking（2504 版）与 30B/70B 级前沿开源 VLM 的对比图 |

> 说明：本讲引用的分数绝大多数来自 README **文本原文**（可精确核对行号）；两张图中逐柱的数值，凡未能从文本确认的一律标注「待本地核对」，请在实践环节亲自打开图片填写。

## 4. 核心概念与源码讲解

### 4.1 基准数据归类：把 README 的成绩单变成结构化知识

#### 4.1.1 概念说明

README 的 Introduction 把十几条成绩**按叙事顺序**散写在四段话里：先讲通用能力与 Agent（L17-L21），再讲长上下文与高分辨率（L23），然后是 Thinking 变体（L25），最后是 2506 升级要点（L28-L33）。叙事顺序适合宣传，不适合检索。

归类解决两个问题：

1. **检索**：拿到一个任务（比如「识别 4K 屏幕截图里的按钮位置」），能立刻反查「哪个基准考察这个能力、Kimi-VL 得了多少分」。
2. **去重**：同一基准（如 ScreenSpot-Pro）可能出现在多段话里且分数不同（初代 34.5、2506 版 52.8），不归类就容易被当成两个基准或看串版本。

本讲采用四类框架：**通用理解与感知 / 多模态推理 / 长上下文 / Agent 定位与高分辨率感知**，另加一个补充类「视频理解」。注意有些基准天然跨类（ScreenSpot-Pro 既是高分辨率感知也是 GUI grounding；MMMU 既考学科知识也考推理），归类以「主要考察点」为准，跨类要备注。

#### 4.1.2 核心流程

归类方法论五步：

```text
① 扫源：通读 README L15-L33、L76-L93，把所有「基准名 + 数字」逐条摘出
② 定版本：给每条分数标注属于哪个变体（Instruct / Thinking 2504 / Thinking-2506）
③ 定类别：按四类框架给基准贴标签，跨类基准记两处
④ 记对比对象：这条分数是跟 GPT-4o-mini 比、跟 70B 开源模型比、还是跟上版自己比
⑤ 记口径：括号里的说明（without extra tools / full set with refusal 等）原样抄录
```

其中第④步最容易被忽略，却是「读懂」与「没读懂」的分界线——没有对比对象的分数等于没有信息。

#### 4.1.3 源码精读

**第一段：通用能力与 Agent（L17-L21）。**

[README.md:L17-L21](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L17-L21) 这段话给出两条关键信息：一是 Kimi-VL 在 OSWorld 这类多轮 Agent 交互任务上「达到与旗舰模型相当的最先进结果」（定性，无绝对分数，OSWorld 绝对分待在图中或 HF 模型卡核对）；二是 Instruct 变体的对比对象被明确框定为 GPT-4o-mini、Qwen2.5-VL-7B、Gemma-3-12B-IT 三个同级竞品，并在若干专项上超过 GPT-4o——**注意这里对比对象从「同级」切换到了「上级」，这正是需要逐句记对比对象的原因**。

**第二段：长上下文与高分辨率（L23）。**

[README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23) 一次性给出四个绝对分数，是长上下文与感知两条产品线的核心证据：

- 长上下文（依托 128K 窗口）：LongVideoBench **64.5**、MMLongBench-Doc **35.1**
- 高分辨率感知（依托 MoonViT 原生分辨率）：InfoVQA **83.2**、ScreenSpot-Pro **34.5**

本段还出现了重要术语 **pareto frontiers（帕累托前沿）**：指「能力-成本」权衡曲线上的最优边界——没有别的模型能同时做到「比它便宜」且「比它强」。README 声称 Kimi-VL 在长上下文与清晰感知两个维度推进了这条前沿。

**第三段：初代 Thinking（L25）。**

[README.md:L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25) 给出初代 Kimi-VL-Thinking（2504 版）三个推理基准分数：MMMU **61.7**、MathVision **36.8**、MathVista **71.3**，并强调是在「仅 2.8B 激活参数」的紧凑规格下取得的——分数与效率要成对阅读。

**第四段：2506 版四个升级要点（L28-L33）。**

[README.md:L28-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L28-L33) 是本讲第二模块的主原料（详见 4.2），这里先摘出其中的**新增基准**（初代段落没出现过的）：MMMU-Pro、MMBench-EN-v1.1、MMStar、RealWorldQA、MMVet、VideoMMMU、Video-MME、V* Benchmark、OSWorld-G。

**汇总归类表**（分数均为 README 文本原文；「出处」列可直接点击核对）：

| 类别 | 基准 | 考察点（一句话） | 变体与分数 | 出处 |
| :--- | :--- | :--- | :--- | :--- |
| 通用理解与感知 | MMMU | 大学水平多学科图文理解与推理 | Thinking 2504：61.7；2506：64.0 | [L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25)、[L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29) |
| 通用理解与感知 | MMMU-Pro | MMMU 的加难版 | 2506：46.3 (+3.2) | [L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29) |
| 通用理解与感知 | MMBench-EN-v1.1 / MMStar / RealWorldQA / MMVet | 感知综合、去捷径题、真实世界问答、开放式综合 | 2506：84.4 / 70.4 / 70.0 / 78.4（对比对象是 Instruct） | [L30](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L30) |
| 多模态推理 | MathVista | 视觉语境数学推理 | Thinking 2504：71.3；2506：80.1 (+8.4) | [L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25)、[L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29) |
| 多模态推理 | MathVision | 带插图的数学竞赛题 | Thinking 2504：36.8；2506：56.9 (+20.1) | [L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25)、[L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29) |
| 长上下文 | LongVideoBench | 长视频理解 | Instruct：64.5 | [L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23) |
| 长上下文 | MMLongBench-Doc | 长文档（多页）理解 | Instruct：35.1 | [L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23) |
| Agent 定位与高分辨率感知 | InfoVQA | 高分辨率信息图问答 | Instruct：83.2 | [L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23) |
| Agent 定位与高分辨率感知 | ScreenSpot-Pro | 高分辨率专业软件界面元素定位（GUI grounding） | Instruct 初代：34.5；2506：52.8 | [L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23)、[L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32) |
| Agent 定位与高分辨率感知 | V* Benchmark | 大图中的小细节定位（口径：without extra tools，不借助外部裁剪工具） | 2506：83.2 | [L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32) |
| Agent 定位 | OSWorld | 真实操作系统环境的多轮 Agent 任务 | 定性：与旗舰模型相当（绝对分待本地核对） | [L18](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L18) |
| Agent 定位 | OSWorld-G | OSWorld 的 grounding 变体（口径：full set with refusal，全任务集且允许拒绝执行） | 2506：52.5 | [L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32) |
| 视频理解（补充类） | VideoMMMU | 大学水平长视频理解 | 2506：65.2（开源模型新 SOTA） | [L31](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L31) |
| 视频理解（补充类） | Video-MME | 综合视频理解 | 2506：71.9 | [L31](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L31) |

**两张性能图的官方定位（L76-L93）。**

[README.md:L83-L87](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L83-L87) 声明 [figures/instruct_perf.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/instruct_perf.png) 的对比口径是「与现有 10B 级稠密 VLM 及 DeepSeek-VL2 (A4.5B) 的简要横评」，任务谱系见 [L81](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L81)：细粒度感知、数学、大学水平题目、OCR、Agent，输入形态覆盖单图、多图、视频、长文档。图中各模型在每个基准上的具体柱值**待本地核对**（本讲综合实践会带你逐柱填写）。

[README.md:L89-L93](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L89-L93) 声明 [figures/thinking_perf.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/thinking_perf.png) 的对比口径是「Kimi-VL-A3B-Thinking（2504 版）在 MathVision 上能与 30B/70B 级前沿开源 VLM 匹敌」——这是一条**跨量级对比**：用 2.8B 激活参数对标 30B/70B 总参数模型，是初代 Thinking 最强的宣传点，图中各模型分数同样待本地核对。

另外 [README.md:L78-L79](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L78-L79) 指出 Thinking-2506 的完整成绩表在 HuggingFace 模型卡（`#2-performance` 小节），README 只列了摘要——这是追完整口径的官方入口。

#### 4.1.4 代码实践

**实践：制作四类基准速查卡**

1. **实践目标**：把 4.1.3 的汇总表压缩成一张你自己手写（或用 Markdown 重排）的「四类 + 视频」速查卡，每类只留「基准 → 一句话考察点 → 最高分属于哪个变体」。
2. **操作步骤**：
   - 打开 [README.md:L15-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15-L33)，逐句把「基准名 + 数字」抄进速查卡；
   - 本地打开 `figures/instruct_perf.png` 与 `figures/thinking_perf.png`，把图中出现、但文本没给的基准（如 OCR 类基准）补进对应类别，分数填图中数值；
   - 给每条分数标注版本标签（Instruct / Thinking-2504 / Thinking-2506）。
3. **需要观察的现象**：同一名基准（ScreenSpot-Pro）在 L23 与 L32 出现了两个相差很大的分数（34.5 与 52.8）；Instruct 段（L23）的四个分数没有任何一个属于推理类基准。
4. **预期结果**：得到一张约 15 行的速查卡，能支撑「任意报一个任务关键词，10 秒内说出考察基准与对应分数」。图中数值部分**待本地验证**（以你打开图片实际读到的为准）。

#### 4.1.5 小练习与答案

**练习 1**：ScreenSpot-Pro 应该归入哪一类？为什么它容易看串版本？

<details><summary>参考答案</summary>

主要归入「Agent 定位与高分辨率感知」：它考察在高分辨率专业软件截图上定位 UI 元素的能力，既是感知题也是 GUI grounding（Agent 的基础动作）。它同时出现在 L23（初代 Instruct，34.5）与 L32（2506 版，52.8），两处分属不同变体，若不标注版本就会把 52.8 误记到 Instruct 头上。
</details>

**练习 2**：L23 声称的两个「pareto frontiers」维度分别是什么？各自的核心证据基准是哪两个？

<details><summary>参考答案</summary>

长上下文处理与清晰感知。长上下文：LongVideoBench 64.5、MMLongBench-Doc 35.1（依托 128K 窗口）；清晰感知：InfoVQA 83.2、ScreenSpot-Pro 34.5（依托 MoonViT 原生分辨率）。帕累托前沿的含义是：在同等推理成本下没有模型同时比它更强。
</details>

**练习 3**：为什么说 `64.5 on LongVideoBench` 单独拿出来「信息量不全」？补什么才算完整？

<details><summary>参考答案</summary>

因为缺少对比对象与口径。完整引用应类似：「Instruct 变体在 LongVideoBench 上 64.5，对比对象为 10B 级稠密 VLM 与 DeepSeek-VL2 (A4.5B)（横评见图 instruct_perf.png），输入为长视频，依托 128K 上下文」。对比对象、变体版本、输入形态三者缺一不可。
</details>

### 4.2 版本间提升解读：2506 相对旧版改在哪、为什么

#### 4.2.1 概念说明

[README.md:L28-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L28-L33) 用四个要点概括 2506 版升级，每条都是「能力主张 + 基准证据」的结构：

1. **Thinks Smarter while Consuming Less Tokens**（L29）：推理分数上涨的同时平均思维链长度缩短 20%——**分数与成本同向优化**，这在长思维链模型里并不常见（常见病是 overthinking：分数涨、token 爆炸）。
2. **Sees Clearer with Thinking**（L30）：旧版 Thinking 偏科（只会思考、感知变弱），2506 在感知类基准上追平甚至超过初代非思考版 Instruct——**从「偏科生」变「全科生」**。
3. **Extends to Video**（L31）：新增视频推理与理解能力，VideoMMMU 65.2 是开源模型新 SOTA。
4. **Extends to Higher Resolution**（L32）：单图上限提到 3.2M 像素（1792×1792），是初代 4 倍，直接推高高分辨率感知与 OS-agent grounding 成绩。

解读版本提升时要区分三种进步类型：**能力升级**（会做以前不会的事，如视频）、**规格升级**（能吃以前吃不了的输入，如分辨率）、**效率升级**（同样的钱办更好的事，如思维链 -20%）。2506 三者皆有，但归因不同——grounding 分数的暴涨主要归因于规格升级（分辨率 4×），推理分数的上涨主要归因于训练方法升级。

#### 4.2.2 核心流程

构建版本对照矩阵的流程：

```text
① 取两端分数：初代值（L23/L25）与 2506 值（L29-L32）
② 核算差值：s_new - s_old，与官方括号差值比对
③ 核算相对提升：(s_new - s_old) / s_old × 100%
④ 归因：把每条提升挂到四个升级要点之一
⑤ 标注口径：凡是官方括号与两端分数差不吻合的，标记「基线口径待确认」
```

第②步不是吹毛求疵——它训练的是对「评测口径」的敏感度：官方差值的基线可能是**重新评测后的分数**，与首发公告（L25）的数字有小幅出入。

#### 4.2.3 源码精读

**推理基准的三连涨（L29）。**

[README.md:L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29) 原文：`56.9 on MathVision (+20.1), 80.1 on MathVista (+8.4), 46.3 on MMMU-Pro (+3.2), 64.0 on MMMU (+2.1)`，并注明 `in average reducing 20% thinking length`。与 L25 初代分数对照：

| 基准 | 初代 Thinking (2504, L25) | Thinking-2506 (L29) | 官方差值 | 两端分数差 | 相对提升 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| MathVision | 36.8 | 56.9 | +20.1 | +20.1 ✓ | \(20.1/36.8 \approx 54.6\%\) |
| MathVista | 71.3 | 80.1 | +8.4 | +8.8（差 0.4） | \(8.8/71.3 \approx 12.3\%\) |
| MMMU | 61.7 | 64.0 | +2.1 | +2.3（差 0.2） | \(2.3/61.7 \approx 3.7\%\) |
| MMMU-Pro | 待本地核对 | 46.3 | +3.2 | —（基线 46.3-3.2=43.1 待核对） | — |

注意 MathVista 与 MMMU 两行：**官方差值与「L25 首发分数直接相减」对不上**。合理解释是官方差值的基线来自重测（例如 MathVista 基线约 71.7、MMMU 基线约 61.9），与首发公告值略有出入。这不是错误，而是评测口径问题——引用时应写「官方口径 +8.4」而非自己重算的 +8.8，并注明两种口径。MathVision 一行则完全吻合（36.8 + 20.1 = 56.9）。

**感知追平 Instruct（L30）。**

[README.md:L30](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L30) 给出 2506 在四个感知基准上的分数（MMBench-EN-v1.1 84.4、MMStar 70.4、RealWorldQA 70.0、MMVet 78.4），并明确对比对象是 `the original non-thinking version (Kimi-VL-A3B-Instruct)`。这句话的工程含义重大：**部署 2506 不再需要「感知走 Instruct、推理走 Thinking」的双模型架构**，一个模型覆盖两类负载。这四个基准在初代 Instruct 上的具体分数需到 instruct_perf.png 或 HF 模型卡**待本地核对**，核对后即可算出「追平还是反超」。

**规格升级 → grounding 暴涨的因果链（L32）。**

[README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32) 是全 README 最漂亮的因果论证：分辨率上限 4×（3.2M 像素 / 1792×1792）→ 高分辨率感知与 OS-agent grounding 基准「non-trivial improvements」→ V* 83.2（口径：without extra tools）、ScreenSpot-Pro 52.8、OSWorld-G 52.5（口径：full set with refusal）。

其中 ScreenSpot-Pro 从初代 34.5（L23）涨到 52.8，绝对提升 \(52.8 - 34.5 = 18.3\)，相对提升 \(18.3 \div 34.5 \approx 53.0\%\)——涨幅与 MathVision（+54.6%）同量级，但驱动力完全不同：MathVision 靠训练方法，ScreenSpot-Pro 靠输入规格。用 u3-l1 的换算验证其可行性：3.2M 像素 ÷ 784 ≈ 4096 视觉 token，只占 128K 预算的约 3%，规格放得开。

**视频能力新增（L31）。**

[README.md:L31](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L31)：VideoMMMU 65.2 为开源模型新 SOTA，Video-MME 71.9 保持综合水准。初代两段（L23/L25）完全没有视频推理基准，属于「能力升级」。

#### 4.2.4 代码实践

**实践：写脚本核算官方差值口径**

1. **实践目标**：用一段纯本地 Python 脚本（无需 GPU、无需模型）自动核对「官方括号差值」与「两端分数直接相减」是否一致，体会评测口径的细节。
2. **操作步骤**：

   示例代码（非项目原有代码，数据取自 README L25/L29）：

   ```python
   baseline = {"MathVision": 36.8, "MathVista": 71.3, "MMMU": 61.7}   # 初代 Thinking, README L25
   new      = {"MathVision": 56.9, "MathVista": 80.1, "MMMU": 64.0}   # Thinking-2506, README L29
   official = {"MathVision": 20.1,  "MathVista": 8.4,  "MMMU": 2.1}   # 官方括号差值, README L29

   for k in new:
       calc = round(new[k] - baseline[k], 1)
       rel  = (new[k] - baseline[k]) / baseline[k] * 100
       flag = "一致" if abs(calc - official[k]) < 0.05 else f"不一致(差{abs(calc-official[k]):.1f})"
       print(f"{k}: 官方{official[k]:+.1f} | 直算{calc:+.1f} | {flag} | 相对提升{rel:.1f}%")
   ```

3. **需要观察的现象**：MathVision 一行输出「一致」；MathVista 与 MMMU 两行输出「不一致」，缺口分别为 0.4 与 0.2。
4. **预期结果**：三行核算结果与 4.2.3 表格一致，从而得出结论——官方差值的基线是重测口径，与 L25 首发值存在小幅出入；引用时应注明「官方口径」。运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：MathVision 绝对提升 20.1、相对提升约 54.6%；ScreenSpot-Pro 绝对提升 18.3、相对提升约 53.0%。两者涨幅相近，驱动力分别是什么？

<details><summary>参考答案</summary>

MathVision（36.8 → 56.9）主要靠训练方法升级（长思维链 SFT + RL 的迭代，推理能力本身变强）；ScreenSpot-Pro（34.5 → 52.8）主要靠输入规格升级（单图上限从约 0.8M 提到 3.2M 像素，模型终于「看得清」高分辨率界面细节）。前者是能力升级，后者是规格升级——归因不同，决定了你在自己任务上能复现哪种收益：任务受限于「想不出」吃前者红利，受限于「看不清」吃后者红利。
</details>

**练习 2**：为什么「平均思维链长度缩短 20%」要跟「推理分数上涨」放在同一句话里讲（L29）？

<details><summary>参考答案</summary>

因为推理分数的提升可以用「更长的思维链」硬堆出来（overthinking），代价是推理成本与延迟同比上涨。2506 的主张是分数涨的同时 token 反而少 20%，即帕累托改进——能力与成本同时改善。工程上这直接体现为：给 2506 配 max_new_tokens=32768 的预算，实际平均消耗比旧版低约两成（u2-l2 已学：预算不足会截断思维链导致丢结论）。
</details>

**练习 3**：`52.5 on OSWorld-G (full set with refusal)` 括号里的口径说明为什么重要？如果去掉它会怎样？

<details><summary>参考答案</summary>

「full set with refusal」表明这是在完整任务集上、且允许模型拒绝执行动作的评测口径。Agent 基准里「允许拒绝」与「强制执行」的分数不可直接比较：允许拒绝时模型可以通过放弃高风险操作来提高成功率。去掉口径说明，读者可能拿 52.5 与其他模型「不允许拒绝」口径的分数直接对比，得出错误结论。同理 V* 的「without extra tools」表明不借助裁剪/缩放等外部工具链。
</details>

### 4.3 选型决策清单：从榜单到工程决策

#### 4.3.1 概念说明

榜单的终点不是「知道分数」，而是**做决策**。选型 = 三个映射的串联：

\[ \text{任务类型} \xrightarrow{\text{基准证据}} \text{变体选择} \xrightarrow{\text{官方推荐}} \text{参数设置} \xrightarrow{\text{规格预算}} \text{部署旋钮} \]

README 本身就是按这个逻辑组织的：[L53](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L53) 按用途推荐变体（通用感知/OCR/长视频/长文档/视频感知/OS-agent → Instruct 高效推理；2506 → 感知同样强且推理更好），[L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68) 给温度推荐（Thinking 0.8 / Instruct 0.2），部署注释 [L256-L257](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L256-L257) 给长上下文与多图的旋钮建议。本模块把它们拼成一张可执行的清单。

一个由 4.2 推出的重要修正：L30 证明 2506 感知已追平 Instruct，所以「混合负载」与「不确定负载」的默认答案从「Instruct 起步」变成了「2506 一步到位」；Instruct 的护城河只剩**纯感知场景的推理成本**——它不需要先产出长思维链。

#### 4.3.2 核心流程

决策树（伪代码）：

```text
if 任务含多步推理 / 数学 / 复杂问答:
    变体 = Thinking-2506; Temperature = 0.8; max_new_tokens = 32768
elif 任务是纯感知(OCR/描述/分类) 且成本敏感:
    变体 = Instruct; Temperature = 0.2; max_new_tokens ≈ 512
else(负载混合或不确定):
    变体 = Thinking-2506（感知已追平 Instruct，见 L30）

部署旋钮(以 vLLM 为例, 见 u3-l3):
    输入总 token 超 32K → --max-model-len 131072（长文档/长视频）
    单请求图片 > 64 张 → --limit-mm-per-prompt image=256 或 512
    高分辨率大图(≤3.2M 像素, 仅 2506) → 确认 --max-model-len 能容纳约 4096 视觉 token
旧版 Thinking(deprecated, L61): 任何场景都不选
```

#### 4.3.3 源码精读

**变体推荐段（L53）。**

[README.md:L53](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L53) 官方分工：通用多模态感知理解、OCR、长视频与长文档、视频感知、OS-agent 用途推荐 Instruct「高效推理」；2506 在同样具备上述能力的同时推理更强。注意「efficient inference」是关键词——选 Instruct 的理由从来不是能力上限，而是成本。

**变体规格表与温度推荐（L57-L68）。**

[README.md:L57-L61](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L57-L61) 的规格表再次确认三个变体 16B / 3B / 128K 完全相同、旧版已标 deprecated；[README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68) 给出温度推荐：Thinking 0.8（推理任务要保留采样多样性，给思维链「探索」空间，呼应 u2-l4 讲过的温度原理）、Instruct 0.2（感知任务要输出稳定）。

**参数与代码互证（L146 / L193 / L256-L263）。**

温度推荐不是空话，官方示例代码直接落实：Instruct 示例用 `max_new_tokens=512`（[README.md:L146](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L146)），Thinking-2506 示例用 `max_new_tokens=32768, temperature=0.8`（[README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193)）——两套生成预算相差 64 倍，正是 4.2「成本同向优化」要在部署层兑现的地方。部署侧 [README.md:L256-L263](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L256-L263) 默认 `--max-model-len 32768 --limit-mm-per-prompt image=64`，注释建议长上下文提到 131072、多图提到 256 或 512。

**选型决策清单（成品表）：**

| 任务类型 | 证据基准（本讲 4.1 表） | 首选变体 | Temperature | 生成预算 | 部署提示 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| OCR / 长文档 / 长视频理解 | InfoVQA 83.2；MMLongBench-Doc 35.1；LongVideoBench 64.5；Video-MME 71.9 | Instruct（成本优先）或 2506 | 0.2 | ≈512 | `--max-model-len 131072` |
| 数学 / 多步推理问答 | MathVista 80.1；MathVision 56.9；MMMU-Pro 46.3 | Thinking-2506 | 0.8 | 32768 | KV cache 随长思维链增长 |
| 感知 + 推理混合负载 | MMVet 78.4 + MathVista 80.1 | Thinking-2506（感知已追平，L30） | 0.8 | 32768 | 一个模型替代双模型架构 |
| 高分辨率细节问答 | V* 83.2；InfoVQA 83.2 | Thinking-2506 | 0.8 | 32768 | 单图 ≤3.2M 像素（1792×1792），约 4096 视觉 token |
| GUI / OS-Agent 定位 | ScreenSpot-Pro 52.8；OSWorld-G 52.5；OSWorld（定性比旗舰） | Thinking-2506 | 0.8 | 32768 | 高分辨率 + 多图旋钮一起调 |
| 大学水平视频课程理解 | VideoMMMU 65.2（开源 SOTA） | Thinking-2506 | 0.8 | 32768 | 长上下文旋钮 `--max-model-len 131072` |

#### 4.3.4 代码实践

**实践：三个场景选型演练**

1. **实践目标**：用 4.3.3 的清单表为三个假想业务场景做选型，并写出「场景 → 基准证据 → 变体 → 参数 → 部署旋钮」的完整决策链。
2. **操作步骤**（纯纸面实践，无需运行环境）：
   - 场景 A：批量识别一万张电商详情页长图中的价格与规格文字；
   - 场景 B：智能助手看用户的手机截图（1080P）回答「把这个按钮点下去会发生什么」；
   - 场景 C：分析一份 200 页的 PDF 财报并计算某个财务指标的变化趋势。
   - 对每个场景：先在 4.1 归类表中找到最接近的基准与分数 → 查 4.3.3 清单确定变体与参数 → 确认部署旋钮（要不要 131072？要不要提 image 上限？）。
3. **需要观察的现象**：场景 A 与 C 同属「感知/长文档」，但 C 需要「计算变化趋势」——一步之差把任务从感知类推向推理类，变体结论可能完全不同；场景 B 的 1080P 截图（约 2M 像素）只有 2506 的 3.2M 上限能整图吃下。
4. **预期结果**：三条完整决策链。参考方向（供核对）：A → Instruct / 0.2 / 512（纯 OCR，成本敏感，注意一万张的吞吐要上 vLLM 批量）；B → 2506 / 0.8 / 32768（GUI 理解 + 后果推理，近似 OSWorld-G 场景）；C → 2506 / 0.8 / 32768（长文档感知 + 数学推理混合，`--max-model-len 131072`）。具体结论以你自己按清单推演为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Thinking 模型推荐 Temperature=0.8 而 Instruct 只有 0.2？从两类任务对「输出分布」的不同要求解释。

<details><summary>参考答案</summary>

推理任务需要思维链探索不同解题路径，较高的温度让采样分布更平坦、保留多样性，避免思路过早收敛到错误方向；感知任务（OCR、描述、分类）答案客观唯一，需要尖锐的分布保证输出稳定可复现，所以低温 0.2。这也是 u2-l4 学过的「温度缩放 softmax」在两类任务上的最佳实践分化。注意（L67-L68 原文口径）温度推荐只区分 Thinking / Instruct 两个系列，与具体版本无关。
</details>

**练习 2**：老板说「我们只用 Instruct 省成本，但遇到数学题就多生成几遍取多数票」。结合 L29-L30 评价这个方案。

<details><summary>参考答案</summary>

不划算。初代 Instruct 在推理类基准上没有官方分数背书（L23 的四个分数全是感知/长上下文类），多数投票救不了能力缺口；而 2506 在推理大涨（MathVista 80.1）的同时感知已追平 Instruct（L30：MMBench 84.4 等四项对齐），且平均思维链缩短 20%（L29）。正确姿势是纯感知批量任务用 Instruct，凡含推理的负载直接 2506——两个模型的差别是「要不要为思维链付 token」，而不是能力梯度。
</details>

**练习 3**：为什么 3.2M 像素的分辨率升级（L32）必须建立在 128K 上下文（L23）之上？用本讲学过的换算说明。

<details><summary>参考答案</summary>

MoonViT 原生分辨率编码下视觉 token 数 ≈ 像素数 ÷ 784（14×14 patch 再经 2×2 pixel shuffle，u3-l1）。3.2M ÷ 784 ≈ 4096 视觉 token/图，多图场景（如 --limit-mm-per-prompt image=64）下视觉 token 轻松达到数十万量级，必须有大上下文窗口承载，否则高分辨率输入在编码阶段就会被截断（truncation，u2-l3）。分辨率规格（看得清）与上下文规格（装得下）是同一能力的两个必要条件。
</details>

## 5. 综合实践

**主任务：产出完整的基准分析表 + 200 字选型结论**（本讲规格书指定的实践，贯穿三个模块的全部方法）。

1. **实践目标**：把 README Introduction（L15-L33）与两张性能图（instruct_perf.png、thinking_perf.png）中出现的**所有**基准，整理成一张含分数与对比对象的分析表，并给出选型结论。
2. **操作步骤**：
   - 按 4.1.2 的五步法扫源：文本部分直接从 [README.md:L15-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15-L33) 摘录；
   - 本地打开 [figures/instruct_perf.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/instruct_perf.png)（对比口径见 [L83](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L83)）与 [figures/thinking_perf.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/thinking_perf.png)（对比口径见 [L89](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L89)），把图中独有基准（如 OCR 类）与各对比模型的柱值填入表格，图中数值以实际读到的为准；
   - 每行记录五要素：**基准 | 类别 | Kimi-VL 哪个变体多少分 | 对比对象及其分数 | 口径备注**；
   - 用 4.2 的差值核算方法抽查至少三条 2506 提升数据；
   - 写 200 字结论：「什么任务该选哪个变体、用什么参数」。

   建议表格模板：

   ```text
   | 基准 | 类别 | 变体与分数 | 对比对象与分数 | 口径备注 |
   |------|------|-----------|---------------|---------|
   | 例: MathVision | 推理 | Thinking-2506: 56.9 (+20.1) | 30B/70B 开源 VLM(具体模型与分数见图 thinking_perf.png) | 差值基线为重测口径 |
   | ...  |      |           |               |         |
   ```

3. **需要观察的现象**：文本给出的分数约 20 条，图还会补出若干文本未提的基准；thinking_perf.png 中 2.8B 激活的 Thinking 与 30B/70B 模型的柱高对比；instruct_perf.png 中 Kimi-VL 在哪些基准领先、哪些落后于同级竞品。
4. **预期结果**：一张 20 行以上的完整分析表 + 一段有数据支撑的结论（结论方向可对照 4.3.3 清单自查：纯感知与成本敏感 → Instruct / 0.2 / 512；推理、混合、高分辨率、Agent、视频理解 → Thinking-2506 / 0.8 / 32768）。图中柱值部分**待本地验证**。

**进阶选做**：到 [README.md:L79](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L79) 指引的 HuggingFace 模型卡 `#2-performance` 小节，核对 2506 完整成绩与本讲 4.1 表的差异，把你表中「待本地核对」的项补齐。

## 6. 本讲小结

- **基准归类**：README 的成绩散落在 L15-L33 四段叙事里，按「通用理解 / 推理 / 长上下文 / Agent 定位与高分辨率感知 + 视频补充」归类并记录「分数 + 变体 + 对比对象 + 口径」四要素，才能从宣传文变成可检索的知识。
- **读图先读口径**：instruct_perf.png 是与 10B 级稠密 VLM 及 DeepSeek-VL2 (A4.5B) 的横评（L83），thinking_perf.png 是 MathVision 上 2.8B 激活对 30B/70B 开源模型的跨量级对比（L89）——对比框架比柱子本身更重要。
- **版本提升三类型**：2506 的升级 = 能力升级（视频：VideoMMMU 65.2 开源 SOTA）+ 规格升级（分辨率 4× 至 3.2M 像素：ScreenSpot-Pro 34.5 → 52.8）+ 效率升级（思维链 -20% 且 MathVision +20.1）；归因不同，你在自己任务上能复现的收益也不同。
- **口径敏感度**：官方差值（如 MathVista +8.4）与首发分数直算（+8.8）存在小出入，说明差值基线是重测口径；OSWorld-G 的「with refusal」、V* 的「without extra tools」这类括号说明必须随分数一起引用。
- **选型即映射**：任务类型 →（基准证据）→ 变体 →（官方推荐）→ Temperature（Instruct 0.2 / Thinking 0.8）与生成预算（512 / 32768）→（规格预算）→ 部署旋钮（max-model-len 131072、limit-mm-per-prompt 256/512）。
- **2506 消灭了双模型架构**：感知四项追平 Instruct（L30）意味着混合负载可单模型落地，Instruct 的定位收窄为「纯感知场景的成本优化」。

## 7. 下一步学习建议

下一讲（u4-l3）是全手册收官的综合实战：把本讲的选型决策、u2 的推理链路、u3 的服务部署拼成一个端到端的多模态问答小应用——输入图片与问题，同时支持「本地 Transformers」与「vLLM 服务」两种后端，并输出结构化的 answer/thinking 字段。建议动手前重读本讲 4.3.3 的清单表与 [README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193) 的生成参数；若想继续深挖评测口径，可顺着 [README.md:L79](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L79) 的指引阅读 HuggingFace 模型卡的完整 performance 小节，并对照各基准的公开论文核对考察点描述。
