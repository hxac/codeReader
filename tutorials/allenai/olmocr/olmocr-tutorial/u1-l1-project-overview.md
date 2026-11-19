# 项目总览：olmOCR 是什么，解决什么问题

## 1. 本讲目标

本讲是整本学习手册的第一讲，不要求你写过一行 olmOCR 代码。读完本讲，你应该能够：

1. 用自己的话说出 olmOCR 的定位：一个基于 **7B 参数视觉语言模型（VLM）** 的文档转换工具箱，把 PDF、PNG、JPEG 文档变成干净、可读的 Markdown/纯文本。
2. 理解它的两种输出形态：**Dolma 格式**（面向大规模语言模型预训练的 JSON 文档）与 **Markdown 文件**（面向人类阅读）。
3. 知道 **olmOCR-Bench** 是什么：项目自带的、包含 7000+ 测试用例的 OCR 评测基准，以及它为什么是理解这个项目的钥匙。
4. 学会阅读 README 中的 benchmark 结果表，能找出 olmOCR 最强与最弱的子项并解释原因。
5. 通过 README 的 News 段落和 CHANGELOG.md，梳理项目从 v0.1 到 v0.4 的演进脉络。

## 2. 前置知识

本讲几乎不需要编程基础，但下面几个术语会反复出现，先通俗地解释一遍：

- **OCR（Optical Character Recognition，光学字符识别）**：把图片里的文字「读」出来变成计算机可编辑文本的技术。传统 OCR（如 tesseract）把页面切成小图块逐个认字；而 olmOCR 属于新一代做法——把整页文档当成一张图片，直接交给一个能「看图」的大模型去理解。
- **VLM（Vision Language Model，视觉语言模型）**：同时接受图像和文本输入的大语言模型。olmOCR 底座是 Qwen2.5-VL 系列的 7B（70 亿参数）模型，微调而来。「7B」决定了它必须跑在 GPU 上（约 12GB 显存起步）。
- **Markdown**：用纯文本标记表达格式的轻量语法，比如 `**粗体**`、表格、公式。olmOCR 的输出目标之一就是把 PDF 版面翻译成 Markdown。
- **Dolma**：AI2 的开源预训练语料库格式。一个 Dolma 文档是一个 JSON 对象，除了正文文本，还带元数据（metadata）和属性（attributes）。olmOCR 把每个 PDF 转成一个 Dolma 文档，方便直接喂给语言模型预训练流水线。第 2 单元第 8 讲会精读它的生成代码。
- **Benchmark（基准测试）**：用一套固定的题目和评分规则给不同系统打分，便于横向比较。「±1.1」这类数字是统计置信区间，表示真实水平的可能波动范围。
- **SFT / GRPO**：两种训练技术。SFT（Supervised Fine-Tuning，监督微调）是「给标准答案让模型模仿」；GRPO 是一种强化学习方法，olmOCR-2 用它把「单元测试通过率」当作奖励信号来训练模型。第 6 单元会专门讲。

## 3. 本讲源码地图

本讲是文档精读型的一讲，涉及的「源码」是两份 Markdown 文档，它们是了解项目的最短路径：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `README.md` | 项目主页说明：功能定位、News、Benchmark 表、安装与用法、代码地图 | 功能特性段落、Benchmark 表、News 段落、Code overview |
| `CHANGELOG.md` | 全部版本号与发布日期的清单（Keep a Changelog 格式） | 版本演进脉络、与 News 段落的互补关系 |

后续讲义会逐步进入真实 Python 源码（如 `olmocr/pipeline.py`、`olmocr/work_queue.py`），本讲先把「项目是什么」这个问题回答清楚。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**README 阅读与功能定位**、**benchmark 结果表解读**、**版本演进史**。

### 4.1 README 阅读与功能定位

#### 4.1.1 概念说明

打开任何开源项目的第一件事都是读 README。olmOCR 的 README 第一句话就把项目定位说得非常清楚：

> "A toolkit for converting PDFs and other image-based document formats into clean, readable, plain text format."
> （一个把 PDF 及其他基于图像的文档格式转换成干净、可读、纯文本格式的工具箱。）

注意两个关键词：

- **toolkit（工具箱）**：olmOCR 不只是一个模型权重，也不只是一个命令行工具，而是一整套流水线——渲染 PDF、构造提示词、调用推理服务、重试纠错、输出 Dolma/Markdown，外加训练代码和评测基准。
- **clean, readable（干净、可读）**：它追求的不是逐字符照抄，而是「读懂版面后的重写」——去掉页眉页脚、恢复自然阅读顺序、用 Markdown 还原表格和公式。

这正是它与传统 OCR 的本质区别：传统 OCR 输出的是「字符坐标 + 文本碎片」，olmOCR 输出的是「理解了版面语义的文档」。

#### 4.1.2 核心流程

从用户视角看，olmOCR 的使用流程极简：

```text
输入（PDF / PNG / JPEG，本地或 S3）
        │
        ▼
olmocr ./localworkspace --markdown --pdfs 你的文件
        │
        ▼
workspace 目录产出两种结果：
  ├── results/*.jsonl        ← Dolma 格式文档（机器友好）
  └── markdown/*.md          ← Markdown 文件（人类友好）
```

背后实际发生的事情（第 2 单元会沿源码逐站精读）是：渲染 PDF 页面为图片 → 构造提示词 → 发给 vLLM 推理服务 → 解析模型输出的结构化 front matter → 失败重试与旋转纠错 → 拼装 Dolma 文档落盘。

#### 4.1.3 源码精读

**项目定位与功能特性清单**，见 [README.md:L26-L37](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L26-L37)。这段列出了六条特性，逐条翻译并解释：

1. 把 PDF、PNG、JPEG 文档转换成干净的 Markdown —— 输入输出形态。
2. 支持公式、表格、手写体和复杂格式 —— 难点场景覆盖。
3. 自动去除页眉页脚（headers and footers）—— 预处理式的「版面理解」能力。
4. 即使存在插图、多栏布局和内嵌文本框，也按自然阅读顺序输出 —— 阅读顺序还原，这是传统 OCR 最容易出错的地方。
5. 高效：每百万页转换成本低于 200 美元 —— 面向百万级文档的工程指标。
6. 基于 7B 参数 VLM，因此需要 GPU —— 技术底座与硬件门槛。

**两种输出形态**，见 [README.md:L292-L308](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L292-L308)。使用 `--markdown` 标志后，`./localworkspace/` 工作区会同时包含 Dolma 文件和 Markdown 文件；用 `cat localworkspace/markdown/olmocr-sample.md` 可以直接查看转换结果。Dolma 是默认必有产物，Markdown 是可选附加产物。

**可复用代码组件清单（Code overview）**，见 [README.md:L493-L502](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L493-L502)。作者自己列出了仓库里「对你自己的项目也可能有用」的部分，这实际上是官方给出的模块地图，正好对应我们后续单元的安排：

| README 提到的组件 | 文件 | 对应本手册 |
| --- | --- | --- |
| 用 GPT-4o 构造高质量自然文本的提示词策略 | `olmocr/data/buildsilver.py` | 第 7 单元第 1 讲 |
| 按语言与 SEO 垃圾过滤 | `olmocr/filter/filter.py` | 第 4 单元第 2 讲 |
| Qwen2.5-VL 的 SFT 微调代码 | `olmocr/train/train.py` | 第 6 单元第 2 讲 |
| GRPO 强化学习训练器 | `olmocr/train/grpo_train.py` | 第 6 单元第 3 讲 |
| 合成数据生成 | `olmocr/synth/mine_html_templates.py` | 第 7 单元第 2 讲 |
| 用 vLLM 批量处理百万 PDF 的流水线 | `olmocr/pipeline.py` | 第 2、3 单元 |
| 查看由 PDF 生成的 Dolma 文档 | `olmocr/viewer/dolmaviewer.py` | 第 7 单元第 3 讲 |

**两篇技术报告**的链接在 [README.md:L12-L17](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L12-L17)：v1 对应 arXiv 2502.18443，v2 对应 arXiv 2510.19817（标题 *olmOCR 2: Unit Test Rewards for Document OCR*，即「用单元测试奖励做文档 OCR」，见文末引用文献 [README.md:L540-L551](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L540-L551)）。

#### 4.1.4 代码实践

**实践：像维护者一样读 README**

1. **实践目标**：不运行任何代码，仅通过 README 建立对项目的准确认知，并验证自己能否找到关键信息的位置。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`，从第一行读到 Installation 一节之前（约前 190 行）。
   - 用纸笔回答三个问题：① olmOCR 的输入格式有哪几种？② 输出有哪两种形态，分别存在 workspace 的哪里？③ 项目基于多大的模型，硬件要求是什么？
   - 再跳到 `README.md` 的 Code overview 一节，数一数官方列出了几个可复用组件。
3. **需要观察的现象**：你会发现自己不需要懂 Python 也能回答上述问题——README 的信息密度很高，且与源码结构一一对应。
4. **预期结果**：① PDF、PNG、JPEG 三种输入；② Dolma 文件（workspace 的 results 输出）与 Markdown 文件（`--markdown` 标志生成的 `markdown/` 目录）；③ 7B 参数 VLM，需要至少 12GB 显存的 NVIDIA GPU、30GB 磁盘。Code overview 列出 7 个可复用组件。

#### 4.1.5 小练习与答案

**练习 1**：olmOCR 说自己 "less than $200 USD per million pages converted"，这个数字和它基于 7B 模型有什么关系？

**参考答案**：7B 是相对小的模型规模（相比几十亿上百亿的通用多模态模型），在单张 GPU 上用 vLLM 做 FP8 量化推理的吞吐足够高，摊到每页的成本才能压到美元级以下。如果换成大得多的模型，同样硬件每秒能处理的页面数会显著下降，单位成本上升。

**练习 2**：传统 OCR 工具（如 tesseract）的输出通常是「文本行 + 坐标框」，而 olmOCR 输出 Markdown。这对下游使用方意味着什么？

**参考答案**：下游（比如语言模型预训练、检索、问答）拿到的是带结构语义的文档——标题层级、表格、公式、阅读顺序都已经被还原，可以直接使用；而传统 OCR 的输出需要再写大量后处理规则去重排文本、拼接分栏、剔除页眉页脚。olmOCR 把这些「脏活」从下游挪到了模型内部。

### 4.2 benchmark 结果表解读

#### 4.2.1 概念说明

**olmOCR-Bench** 是项目自带的评测基准，README 对它的介绍见 [README.md:L50-L53](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L50-L53)：覆盖 **1400 份文档、超过 7000 个测试用例**，用来衡量 OCR 系统的表现。

它的特别之处在于设计哲学：**像单元测试一样评测 OCR**。每个测试用例不是「两段文本算相似度」，而是一条条机器可校验的断言，例如「输出中必须出现这个表格的完整结构」「公式渲染后与标准答案等价」「第 3 段必须出现在第 5 段之前」。八种测试类型（baseline、present、absent、order、table、math、format、footnote）的源码将在第 5 单元精读。

benchmark 表的八个子项对应八类页面难度：

| 子项 | 考察内容 |
| --- | --- |
| Base | 基础文本忠实转写 |
| ArXiv | 学术论文（公式、引用、双栏） |
| Old scans math | 老扫描件上的数学公式 |
| Tables | 表格结构还原 |
| Old scans | 老扫描件（低质量、噪点、退化） |
| Headers & footers | 页眉页脚的识别与剔除 |
| Multi column | 多栏阅读顺序 |
| Long tiny text | 长而极小的文字 |

#### 4.2.2 核心流程

读表的正确姿势分三步：

1. **先看 Overall（总分）**：了解综合水平排序。
2. **再看子项**：找出每个系统「擅长什么、怕什么」——总分接近的系统，子项分布可能截然不同。
3. **最后看置信区间**：表中的 ±X 是 bootstrap 置信区间（第 5 单元第 2 讲会讲它的计算源码）。直觉理解：真实水平大概率落在「得分 ± X」范围内。若两个系统的总分差 \(\Delta\) 小于区间宽度，就不能断言谁真的更强。

例如 Chandra OCR 总分 83.1±0.9，olmOCR v0.4.0 总分 82.4±1.1：

\[
\Delta = 83.1 - 82.4 = 0.7, \qquad \text{Chandra 区间} = [82.2, 84.0], \quad \text{olmOCR 区间} = [81.3, 83.5]
\]

两个区间大量重叠，说明在统计意义上这 0.7 分的差距不足以分出高下。

#### 4.2.3 源码精读

完整的 benchmark 结果表见 [README.md:L55-L183](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L55-L183)。表中对比了 9 个系统（Mistral OCR API、Marker、MinerU、DeepSeek-OCR、Nanonets-OCR2-3B、PaddleOCR-VL、Infinity-Parser、Chandra OCR 与 olmOCR v0.4.0 本身）。

**olmOCR v0.4.0 这一行的数据**，见 [README.md:L170-L181](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L170-L181)：

| 子项 | 得分 | 点评 |
| --- | --- | --- |
| Base | **99.7** | 全表最强子项，基础转写几乎满分 |
| Headers & footers | 96.1 | 版面理解类强项，远超 Nanonets 的 32.1 |
| Tables | 84.9 | 表格还原位居第一梯队（Chandra 88.0、Nanonets 86.8 略高） |
| Multi column | 83.7 | 多栏阅读顺序表现稳定 |
| ArXiv | 83.0 | 学术文档处于第一梯队（Paddle 85.7、Marker 83.8 略高） |
| Old scans math | 82.3 | 老扫描公式大幅领先 Mistral（67.5）与 Marker（66.8） |
| Long tiny text | 81.9 | 长微小文字中规中矩（Nanonets 93.0 更高） |
| **Old scans** | **47.7** | **全表最弱子项**，也是所有参赛系统的共同短板 |
| **Overall** | **82.4±1.1** | 总分第一梯队（Chandra 83.1±0.9、Infinity-Parser 82.5） |

几个值得深挖的观察：

- **最弱项 Old scans（47.7）**：看整列数据——Mistral 29.3、Marker 33.5、MinerU 33.7、DeepSeek-OCR 33.3、Nanonets 40.9、PaddleOCR-VL 37.8、Infinity-Parser 47.9、Chandra 50.4（见 [README.md:L71-L166](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L71-L166)）。**没有一个系统超过 51 分**，说明老扫描件（低分辨率、纸张噪点、字体退化）是整个行业的未解难题，而不是 olmOCR 独有的缺陷。原因可以推测：渲染成图片后信息本身已经物理缺失，模型「看不清」而非「读不懂」。
- **最强项 Base（99.7）**：基础文本转写对 7B VLM 来说已是接近饱和的能力，各系统都在 93 以上。
- **对比 Mistral OCR API（总分 72.0）与 Marker（76.1）**：olmOCR 领先 6~10 分，优势集中在 Old scans math（82.3 vs 67.5/66.8）、Old scans（47.7 vs 29.3/33.5）等困难子项；但在 ArXiv（83.0 vs 77.2/83.8）上 Marker 反而略高。说明 olmOCR 的优势场景是「困难版面 + 低质量扫描」，而不是压倒性全胜。
- 表中部分系统名带 `*`（MinerU、PaddleOCR-VL、Infinity-Parser、Chandra），README 未附图例说明其确切含义（待确认；推测与测试方式或提交方有关，阅读时不宜过度解读）。

#### 4.2.4 代码实践

**实践：从 benchmark 表中找出 olmOCR 的最强与最弱子项，并写出理由**

1. **实践目标**：训练「读表 → 提结论 → 给解释」的能力，为后面读评测源码建立直觉。
2. **操作步骤**：
   - 对照 [README.md:L55-L183](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L55-L183) 的表格，在纸上抄下 olmOCR v0.4.0 行的全部 9 个数字。
   - 圈出最小值和最大值（除 Overall 外）。
   - 对最弱子项，纵向看这一列其他 8 个系统的得分，回答：「这是 olmOCR 独有的弱点，还是全行业难题？」
   - 对最强子项，找出全表在该列最接近 olmOCR 的两个系统。
3. **需要观察的现象**：最弱项 Old scans 那一列所有系统都不及格（全部低于 51）；最强项 Base 那一列所有系统都在 93 以上。
4. **预期结果**：最弱 Old scans 47.7（全行业短板，信息在扫描源头已丢失，模型无能为力）；最强 Base 99.7（成熟能力，接近天花板）。由此得出使用建议：数字原生的现代 PDF 是 olmOCR 的舒适区，老扫描件需要额外预处理（如超分辨率）或人工校对。

#### 4.2.5 小练习与答案

**练习 1**：Mistral OCR API 的 Base 得分 99.4 与 olmOCR 的 99.7 几乎相同，但总分差了 10 分以上。这说明了什么？

**参考答案**：总分差距不来自基础转写能力，而来自困难子项——Mistral 在 Old scans（29.3 vs 47.7）、Old scans math（67.5 vs 82.3）、Tables（60.6 vs 84.9）上落后明显。选型时不能只看「能不能认字」，要看你的文档落在哪些子项上。

**练习 2**：Nanonets-OCR2-3B 的 Headers & footers 只有 32.1，而 olmOCR 是 96.1。结合该子项考察的内容，推测 Nanonets 可能出了什么问题？

**参考答案**：Headers & footers 考察「识别并剔除页眉页脚」。32.1 的低分说明 Nanonets 很可能把页眉页脚（页码、期刊名、重复标题）混进了正文输出，或错误地删掉了正文内容——这是「保留该删的、删掉该留的」两类错误的体现，暴露的是版面语义理解不足而非字符识别不足。

**练习 3**：表中 olmOCR 总分 82.4±1.1，Infinity-Parser 82.5±?（置信区间未知）。为什么「?」会削弱这两个系统之间的可比性？

**参考答案**：没有置信区间就无法判断 0.1 分的差距是真实差异还是采样噪声。±1.1 意味着 olmOCR 的真实得分可能在 81.3~83.5 之间浮动；Infinity-Parser 若区间宽度类似，两者几乎必然统计不可区分。做严谨对比时，缺失区间的分数只能作参考。

### 4.3 版本演进史（CHANGELOG / News）

#### 4.3.1 概念说明 olmOCR 有两份「历史记录」，用途不同：

- **README 的 News 段落**：维护者手工挑选的**里程碑视图**，只记大事——新模型发布、重大架构切换、新子系统上线。
- **`CHANGELOG.md`**：**完整版本视图**，遵循 Keep a Changelog 格式与语义化版本（SemVer），列出每一个发布版本和日期，见 [CHANGELOG.md:L1-L6](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/CHANGELOG.md#L1-L6)。

两者互补：想知道「项目经历了哪几个时代」看 News；想知道「现在最新版是多少、某个修复进了哪个版本」看 CHANGELOG。

#### 4.3.2 核心流程

把 News 中的 9 条记录按主题归并，可以画出项目的三代演进主线：

```text
v0.1.x 时代（2025.02 - 2025.06）「v1：跑起来」
  ├─ v0.1.58 (02-25)  初始公开发布 + 在线 demo
  ├─ v0.1.60 (03-17)  改进采样温度选择 → 性能提升
  ├─ v0.1.68 (05-19)  ★ olmOCR-Bench 发布（首发 77.4 分）
  ├─ v0.1.70 (05-23)  官方 Docker 镜像支持
  └─ v0.1.75 (06-17)  ★ 推理后端从 sglang 切换到 vllm

v0.2.x - v0.3.x 时代（2025.07 - 2025.10）「工程化 + 模型迭代」
  ├─ v0.2.0 (07-23)  训练代码重构（trainer 开源）
  ├─ v0.2.1 (07-24)  新模型 0725-FP8：bench +3 分、默认 FP8、重试更少
  └─ v0.3.0 (08-13)  新模型 0825-FP8：修复自动旋转检测、空白页幻觉

v0.4.x 时代（2025.10 至今）「olmOCR-2：合成数据 + RL」
  └─ v0.4.0 (10-21)  ★ olmOCR-2-7B-1025-FP8：
      用合成数据 + RL 训练（GRPO），bench 再 +4 分
```

三条暗线值得注意：

1. **推理后端的迁移**（sglang → vllm）：为了吞吐和稳定性更换整个推理引擎，第 3 单元第 3 讲会看到现在的 vLLM 服务管理代码。
2. **评测驱动开发**：v0.1.68 发布 bench 之后，每一次模型迭代（v0.2.1、v0.4.0）的 News 都用 bench 分数量化改进幅度（+3、+4 分）。评测基准成了项目的「单元测试」。
3. **从 SFT 到 RL**：v0.2.0 先开源训练器（SFT 路线），v0.4.0 引入 RL 训练——这正是 v2 论文标题「Unit Test Rewards」的由来：把 olmOCR-Bench 的单元测试直接搬进强化学习的奖励函数。

#### 4.3.3 源码精读

**News 段落全文**，见 [README.md:L39-L48](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L39-L48)。9 条记录覆盖 2025-02-25 至 2025-10-21，其中三条直接与新模型发布挂钩并给出 HuggingFace 链接（olmOCR-2-7B-1025-FP8、olmOCR-7B-0825-FP8、olmOCR-7B-0725-FP8）。

**CHANGELOG 的格式声明与版本节奏**，见 [CHANGELOG.md:L1-L6](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/CHANGELOG.md#L1-L6)。版本清单从 [CHANGELOG.md:L10](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/CHANGELOG.md#L10) 的 v0.4.27（2026-03-12，当前清单中最新）一直列到 [CHANGELOG.md:L112-L116](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/CHANGELOG.md#L112-L116) 的 v0.1.53。两个观察：

- **粒度差异**：News 只写到 v0.4.0，而 CHANGELOG 记录了 v0.4.0 之后的十几个补丁版本（v0.4.1 ~ v0.4.27），说明 v0.4 是一个长期维护的大版本，日常修复持续发生。
- **绝大多数版本条目没有描述文字**，唯一带说明的是 v0.1.53（"Fixed git checks"、"Added gemini and claude runners and a viewer"）。所以想了解每个版本的实质变化，应该结合 `git log` 而不是只看 CHANGELOG——这也是本手册把 CHANGELOG 当「版本索引」而非「变更说明」用的原因。

#### 4.3.4 代码实践

**实践：对照两份历史记录画一条时间线**

1. **实践目标**：体会 News（里程碑）与 CHANGELOG（全版本）两种粒度的差异，亲手整理出项目演进骨架。
2. **操作步骤**：
   - 打开 [README.md:L39-L48](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L39-L48) 的 News 与 [CHANGELOG.md:L8-L116](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/CHANGELOG.md#L8-L116) 的版本清单。
   - 在纸上画一条时间轴，把 News 的 9 个里程碑标在轴上。
   - 统计：CHANGELOG 中 v0.1.x、v0.2.x、v0.3.x、v0.4.x 各有多少个版本？哪个大版本存活时间最长、补丁最多？
   - 挑一个你关心的版本（如 v0.1.75 sglang→vllm 迁移），在仓库根目录运行只读命令 `git log --oneline --since=2025-06-01 --until=2025-06-30` 查看当时的提交记录（可选）。
3. **需要观察的现象**：v0.1.x 的小版本数量远多于其他大版本（从 0.1.53 一路排到 0.1.76）；v0.4.0 之后补丁版本延续了数月。
4. **预期结果**：v0.1.x 约 20+ 个版本，是迭代最频繁的「打地基」阶段；v0.4.x 从 2025 年 10 月延续到 2026 年初，是当前稳定大版本。时间轴应呈现「密集试错（v0.1）→ 架构稳定（v0.2/v0.3）→ 长期维护（v0.4）」的形态。

#### 4.3.5 小练习与答案

**练习 1**：为什么 v0.1.75 从 sglang 迁移到 vLLM 这件事值得写进 News，而大量普通 bug 修复只留在 CHANGELOG（甚至不留文字说明）？

**参考答案**：News 的筛选标准是「对用户和项目方向有重大影响」。推理后端决定了部署方式、性能特征和依赖栈，属于架构级变更；普通修复不改变使用方式。这也提示读者：读开源项目史时，News 段落是最快理解「项目经历了哪几个关键转折」的入口。

**练习 2**：v0.4.0 的 News 说 "boosts olmOCR-bench score by ~4 points using synthetic data and introduces RL training"。结合 benchmark 表，这次迭代把总分从大约多少提升到了多少？

**参考答案**：v0.4.0 总分 82.4±1.1，提升约 4 分意味着上一代大约在 78 分上下。再往前，v0.2.1 曾带来 +3 分，v0.1.68 首发 77.4——把这些数字串起来，正好是「评测驱动迭代」的证据链：77.4 → ~78 → 82.4。

**练习 3**：CHANGELOG.md 头部声明遵循 SemVer（语义化版本）。观察 v0.2.0 → v0.2.1 → v0.3.0 的变化内容，项目把「新模型发布」标为 minor 版本（0.X.0）还是 patch 版本（0.X.Y）？

**参考答案**：新模型发布标为 minor 版本：v0.2.1（0725 模型）虽写作 patch 形式，但 v0.3.0（0825 模型）和 v0.4.0（olmOCR-2）都是 minor。更细看 v0.2.1 也换了模型却只升 patch——说明项目的版本号实践并不严格贴合 SemVer 语义，读史时以 News 内容为准更可靠。

## 5. 综合实践

**任务：写一份 200 字的「选型备忘录」**

把本讲三个模块串成一份给同事看的选型建议，要求包含以下三部分：

1. **对比总结（≤200 字）**：基于 [README.md:L55-L183](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L55-L183) 的 benchmark 表，说明 olmOCR 相比 Mistral OCR API、Marker 等方案的优势场景。提示：从 OlmOCR 领先最多的子项（Old scans math、Old scans、Tables）和它需要的运行条件（GPU）两方面写。
2. **最强/最弱子项分析**：给出 olmOCR 最强（Base 99.7）与最弱（Old scans 47.7）的子项，并各用 2~3 句解释原因。要求像 4.2.4 的实践那样，用「纵向对比全列数据」支撑你的解释——最弱项必须是全行业短板的证据，最强项要说明为什么接近天花板。
3. **版本建议**：如果同事要部署生产环境，结合 4.3 的演进史说明应该选哪个大版本线（提示：v0.4.x 是当前长期维护线，CHANGELOG 显示其补丁持续到 2026 年初），以及为什么不该参考 News 之外的旧 benchmark 数字（旧版本分数低 4~10 分，见练习 2 的分数链）。

**验收标准**：备忘录中的每一个数字都能在 README 的表格或 News 段落中找到出处；没有凭印象写的结论。

## 6. 本讲小结

- olmOCR 是 AI2 开源的**文档转换工具箱**：基于 7B 视觉语言模型（Qwen2.5-VL 系微调），把 PDF/PNG/JPEG 转成干净的 Markdown 与 Dolma 格式纯文本，每百万页成本低于 200 美元。
- 它与传统 OCR 的本质区别是**版面语义理解**：自动去页眉页脚、还原多栏自然阅读顺序、用 Markdown 还原表格与公式——这些由模型「看图重写」完成，而非下游后处理规则。
- **olmOCR-Bench**（1400 文档、7000+ 测试用例）采用「像单元测试一样评测」的设计，既是选型依据，也是项目自身迭代和 RL 训练的标尺。
- benchmark 表解读：olmOCR v0.4.0 总分 82.4±1.1 处第一梯队；最强子项 Base 99.7（接近天花板），最弱子项 Old scans 47.7（全行业未解难题）；对比 Mistral/Marker 的优势集中在困难版面与低质量扫描场景。
- 项目演进三代：v0.1.x 跑起来（含 sglang→vLLM 迁移与 bench 发布）→ v0.2/v0.3 工程化与模型迭代 → v0.4.x 的 olmOCR-2 用合成数据 + GRPO 强化学习再涨 4 分。
- README 的 **Code overview** 一节就是官方模块地图，本手册后续单元与之一一对应。

## 7. 下一步学习建议

下一讲（**u1-l2 环境搭建**）将动手安装 olmocr：区分 `olmocr` / `olmocr[gpu]` / `olmocr[beaker]` / `olmocr[bench]` 四种安装选项，理解 poppler 等系统依赖的作用，并运行 `python -m olmocr.check` 做环境自检。

在进入下一讲之前，建议先浏览这样两处源码建立印象（不要求读懂）：

- [pyproject.toml](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml) —— 看看 `[project.optional-dependencies]` 里四个 extra 各自装什么。
- [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) 的文件开头 —— 这就是第 2 单元要精读的「项目心脏」，先混个眼熟。

如果你对 benchmark 的「单元测试式」设计感到好奇，可以提前翻看 [olmocr/bench/README.md](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/README.md)，那是第 5 单元的主战场。
