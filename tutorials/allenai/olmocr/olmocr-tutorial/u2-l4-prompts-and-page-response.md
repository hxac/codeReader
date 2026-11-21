# u2-l4 提示词体系与 PageResponse 数据模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `olmocr/prompts/prompts.py` 中提示词家族的成员与演进脉络：GPT-4o silver 数据提示词的四个版本 → 旧版微调提示词 → 当前推理使用的 `build_no_anchoring_v4_yaml_prompt`。
2. 逐句解释当前推理提示词的每一条指令，并说明它在 `build_page_query` 中如何与页面图片组装成一条多模态请求。
3. 解释 `PageResponse` 六个字段在流水线中各自扮演的角色：哪个驱动重试控制流、哪些进入 Dolma 属性、哪个是真正的文本载荷。
4. 理解 `rotation_correction` 的取值约束 `{0, 90, 180, 270}` 如何在数据类校验、JSON schema 的 `enum`、`--guided-decoding` 正则三处保持一致。
5. 区分 silver 数据提示词（给 GPT-4o 老师）与推理提示词（给微调后的 7B 学生模型）在设计上的本质差异。

## 2. 前置知识

本讲默认你已完成单元 1 和 u2-l1～u2-l3，知道 main() 的编排主线和「PDF 单页 → PNG → base64」的渲染流程。在此之上，补充几个新概念：

- **提示词（prompt）**：发给大语言模型的指令文本。olmOCR 是多模态模型，一条请求里既有文字指令又有图片，两者并列放在消息的 `content` 列表里。
- **silver 数据**：用更强的老师模型（GPT-4o）生成的「中等质量」标注文本，作为微调学生模型（Qwen2.5-VL 7B）的训练目标。这个「老师出题、学生模仿」的过程叫**蒸馏（distillation）**。gold 数据指人工精标数据，silver 数据便宜量大但质量略逊。
- **YAML front matter**：一段夹在 `---` 分隔线之间的键值对元数据，放在 Markdown 文件最顶部。olmOCR 借用它让模型在正文之外再输出若干结构化字段。
- **dataclass（数据类）**：Python 的 `@dataclass` 装饰器可以自动生成 `__init__` 等方法，用来定义「纯数据容器」。`frozen=True` 表示实例创建后不可修改（不可变对象）。
- **`__post_init__`**：dataclass 专用的钩子方法，在自动生成的 `__init__` 执行完之后立即调用，是做参数校验的标准位置。
- **结构化输出（structured output）**：强制模型输出符合某个 JSON Schema 或正则约束的文本，而不是自由发挥。OpenAI API 用 `response_format=json_schema`，vLLM 用 guided decoding（本项目中是一个正则）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [olmocr/prompts/prompts.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py) | 本讲主角：全部提示词构造函数、`PageResponse` 数据类、`openai_response_format_schema`，全文仅 184 行，无任何第三方依赖 |
| [olmocr/prompts/\_\_init\_\_.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/__init__.py) | 包的对外出口，只导出 7 个名字，决定了「哪些提示词是公共 API」 |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | 消费端：`build_page_query` 用提示词拼请求，`process_page` 读 `PageResponse` 字段做重试，`build_dolma_document` 把字段写进输出 |
| [olmocr/train/dataloader.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/dataloader.py) | 训练端复用同一个提示词函数，并用 `FrontMatterOutputFormat` 把 `PageResponse` 序列化成训练标签——「训练即推理」的一致性来源 |

## 4. 核心概念与源码讲解

### 4.1 提示词家族演进：silver 数据提示词与微调提示词

#### 4.1.1 概念说明

prompts.py 里躺着两代共 7 个构造函数，它们不是并列关系，而是一条**演进链**：

```
GPT-4o silver 提示词（老师用，带位置文本辅助）
    v1 → v2 → v2_simple → v3_simple（约束逐版收紧）
            ↓ 简化
旧版微调提示词 build_finetuning_prompt（学生用，仍带锚文本）
            ↓ 去掉锚文本
build_no_anchoring_yaml_prompt / v4（当前推理用，只看图）   ← 4.2 节精读
```

核心洞察：**给老师的提示词可以很长很啰嗦**，因为 GPT-4o 理解力强、还额外配了一份带坐标的原始文本（锚文本）当拐杖；**给学生（微调后的 7B 模型）的提示词则越来越短**——模型在训练中已经学会了任务本身，不再需要拐杖。这就是蒸馏带来的「提示词瘦身」。

#### 4.1.2 核心流程

silver 提示词的迭代逻辑（逐版对比）：

1. **v1**：图片 + 带坐标的锚文本，要求自然阅读、公式转 LaTeX、表格转 markdown、去页眉页脚、保留跨页句子、不许幻觉。
2. **v2**：数学定界符明确为 `\(` `\)` 和 `\[` `\]`；表格从 markdown 改为 HTML；新增图片标注语法 `![描述](page_startx_starty_width_height.png)`。
3. **v2_simple**：去掉锚文本参数，改为只传 `page_width` / `page_height`——图片标注所需的坐标参照系从「锚文本坐标」换成「页面尺寸」。
4. **v3_simple**：进一步禁止 Unicode 数学符号（∈ ∉ ⊂ 等），要求一律写 LaTeX；HTML 表格规则收紧（表头用 `<th>`、合理使用 rowspan/colspan、单元格内禁用 `<br>`、禁用 LaTeX 和 Markdown 表格）。

收紧的方向与 olmOCR-Bench 的校验器高度吻合：bench 的 `table_parsing.py` 解析的正是 HTML 表格，数学公式比较的正是 LaTeX——老师输出的格式越规范，训练数据和评测就越「机器可校验」。

#### 4.1.3 源码精读

v1 是整个家族的底稿，也是唯一被 `__init__.py` 导出的 silver 提示词：

- [olmocr/prompts/prompts.py:L7-L17](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L7-L17) — `build_openai_silver_data_prompt(base_text)`：接收锚文本参数，指令包括「像自然阅读一样返回纯文本」「公式转 LaTeX、表格转 markdown」「去页眉页脚但保留引用和脚注」「读手写」「跨页句子原样保留」「没有可读文本可输出 null」「不许幻觉」，最后把锚文本夹在 `RAW_TEXT_START` / `RAW_TEXT_END` 标记之间。

v3_simple 是演进终点，约束最细：

- [olmocr/prompts/prompts.py:L50-L63](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L50-L63) — `build_openai_silver_data_prompt_v3_simple(page_width, page_height)`：不再接收锚文本，只传页面宽高；数学与表格规则如上节所述，末尾附上 `Page width: ..., Page height: ...` 供图片标注定位使用。

旧版微调提示词刻意做减法，源码注释直接点明了这层关系：

- [olmocr/prompts/prompts.py:L145-L153](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L145-L153) — 注释写着「这是训练和运行微调模型用的基础提示词，从生成 silver 数据的提示词简化而来」；`build_finetuning_prompt(base_text)` 只剩三句话：任务描述、「不许幻觉」、夹入锚文本。

配套还有一个反向工具：

- [olmocr/prompts/prompts.py:L174-L183](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L174-L183) — `extract_raw_text(prompt)` 用正则 `RAW_TEXT_START\s*\n(.*?)\nRAW_TEXT_END`（开 `re.DOTALL` 让 `.` 匹配换行）从拼好的提示词里把锚文本抠回来，找不到就抛 `ValueError`。它服务于数据工具链的「往返操作」。

这些 silver 提示词的实际调用方主要在 bench runner（让 GPT-4o/Claude/Gemini 参加olmOCR-Bench 评测）和数据构建脚本，例如 [olmocr/data/buildsilver.py:L50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/buildsilver.py#L50) 用 v1 生成 silver 数据，[olmocr/data/build_openai_batch_from_olmocrmix.py:L98](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/build_openai_batch_from_olmocrmix.py#L98) 用 v3_simple 构造批量请求；而 [olmocr/prompts/\_\_init\_\_.py:L1-L9](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/__init__.py#L1-L9) 只导出 v1，v2/v3 系列属于模块内部 API，靠 `from olmocr.prompts.prompts import ...` 全路径引入。

#### 4.1.4 代码实践

**实践目标**：用肉眼+脚本确认「提示词逐版收紧」不是一句空话。

**操作步骤**（只需 `pip install olmocr` 的轻量环境，纯字符串操作）：

```python
# 示例代码：对比 silver 提示词 v2_simple 与 v3_simple
from olmocr.prompts.prompts import (
    build_openai_silver_data_prompt_v2_simple,
    build_openai_silver_data_prompt_v3_simple,
)

a = build_openai_silver_data_prompt_v2_simple(612, 792)
b = build_openai_silver_data_prompt_v3_simple(612, 792)
print(a)
print("=" * 40)
print(b)
```

**需要观察的现象**：两段文本结构几乎相同，但 v3_simple 明显更长——多出的部分全部是「禁止项」（禁 Unicode 数学符号、禁 `<br>`、禁 LaTeX/Markdown 表格）。

**预期结果**：输出末尾两行都是 `Page width: 612, Page height: 792`；v3_simple 中能找到 `\\( \\in \\)` 这样的转义示例而 v2_simple 没有。已按源码逐行核对，可直接验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 v2_simple 之后的 silver 提示词不再传 `base_text`，却要传 `page_width` 和 `page_height`？

**答案**：因为图片标注语法 `![...](page_startx_starty_width_height.png)` 需要一个坐标系参照。v1/v2 里锚文本自带每个文本块的位置坐标（原点在左下角），模型可以借此估计图片位置；去掉锚文本后，至少要告诉模型页面尺寸，标注坐标才有意义。

**练习 2**：`build_finetuning_prompt` 与 v1 silver 提示词相比删掉了哪些指令？为什么可以删？

**答案**：删掉了「公式转 LaTeX、表格转 markdown」「去页眉页脚保留引用脚注」「读手写」「保留跨页句子」「可输出 null」等全部格式细则，只留任务描述、防幻觉和锚文本。可以删是因为这些行为不再靠提示词约束，而是通过 SFT 训练直接写进了模型权重——微调的本质就是把「提示词里说的话」变成「模型的肌肉记忆」。

**练习 3**：`extract_raw_text` 的正则里为什么必须加 `re.DOTALL`？

**答案**：锚文本是多行文本，`.*?` 默认不匹配换行符；不加 `re.DOTALL` 时跨行的锚文本只能匹配到第一行，甚至匹配失败。开 DOTALL 后 `.*?` 以非贪婪模式跨越所有行，直到遇到 `RAW_TEXT_END` 为止。

### 4.2 当前推理提示词 build_no_anchoring_v4_yaml_prompt

#### 4.2.1 概念说明

这是流水线**现在每一页都在用**的提示词。名字里的三个关键词就是它的设计宣言：

- **no_anchoring**：不注入任何锚文本，模型只看页面图片——纯视觉 OCR。
- **v4**：第四代格式约定。与哥哥 `build_no_anchoring_yaml_prompt`（表格转 markdown）相比，v4 把表格改成 HTML，与 v3_simple 老师格式和 bench 校验器对齐。
- **yaml**：输出必须是带 YAML front matter 的 markdown，front matter 里填五个参数。

它是一个**无参函数**——不接收任何输入，返回固定字符串。提示词与输入数据彻底解耦，输入（页面图片）在消息的另一个 content 部件里。

#### 4.2.2 核心流程

提示词在请求生命周期中的位置：

```
build_page_query(pdf路径, 页码, 目标最长边, 旋转角)
    ├─ render_pdf_to_base64png(...)   ← u2-l3 讲过的渲染
    ├─ 若旋转角 ≠ 0：用 PIL 把图片转 90/180/270 度后重新编码
    └─ 组装请求 dict：
        messages[0].content = [
            {"type": "text",  "text": build_no_anchoring_v4_yaml_prompt()},  ← 指令
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},  ← 图片
        ]
        max_tokens = 8000, temperature 占位 0.0（稍后按重试档位覆盖）
```

模型返回一段 markdown，形如：

```
---
primary_language: en
is_rotation_valid: True
rotation_correction: 0
is_table: False
is_diagram: False
---
正文文本……
```

`try_single_page` 再用 `FrontMatterParser` 把它解析成 `PageResponse`（解析细节是下一讲 u2-l5 的主题）。

#### 4.2.3 源码精读

提示词本体只有三句话，逐句拆解：

- [olmocr/prompts/prompts.py:L164-L170](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L164-L170) — `build_no_anchoring_v4_yaml_prompt()`：
  - 第一句 `"Attached is one page of a document that you must process."`：任务定性——处理一页文档（注意是「一页」，页是流水线的最小工作单元）。
  - 第二句：自然阅读顺序返回纯文本表示，公式转 LaTeX，表格转 HTML；若有插图，用 `![描述](page_startx_starty_width_height.png)` 语法标注（顺带一提，源码字符串里写的是「LateX」，大写顺序如此，grep 源码时别写成 LaTeX 全小写去找）。
  - 第三句：输出为 markdown，顶部 front matter 填 `primary_language`、`is_rotation_valid`、`rotation_correction`、`is_table`、`is_diagram` 五个参数——这就是 `PageResponse` 的序列化契约（只点名五个，第六个字段 `natural_text` 就是 front matter 之后的正文本身）。

它在请求中的落位：

- [olmocr/pipeline.py:L133-L146](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L133-L146) — `build_page_query` 的返回值：`content` 列表第一项在 [L139](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L139) 插入这段提示词，第二项在 [L140](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L140) 插入 base64 图片；`max_tokens=8000`（[L107](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L107) 定义，[L144](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L144) 使用），`temperature` 先写死 0.0 占位（[L145](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L145)），实际值在 `try_single_page` 里按 `TEMPERATURE_BY_ATTEMPT` 档位覆盖（[L177](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L177)，u2-l6 详讲）。

可选的语法级约束：

- [olmocr/pipeline.py:L179-L182](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L179-L182) — 当命令行开启 `--guided-decoding` 时，请求会附带 `guided_regex`，用一条正则把 front matter 钉死为「语言是两个小写字母或 null、布尔是 True/False/true/false、旋转只能是 0/90/180/270」的形状，交给 vLLM 的 guided decoding 在解码层面强制执行。

「训练即推理」的关键证据：

- [olmocr/train/dataloader.py:L239-L245](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/dataloader.py#L239-L245) — 训练数据加载的 `NewYamlFinetuningPromptWithNoAnchoring` 步骤调用的是**同一个函数** `build_no_anchoring_v4_yaml_prompt()`；而 [olmocr/train/dataloader.py:L248-L266](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/dataloader.py#L248-L266) 的 `FrontMatterOutputFormat` 则把标注 `PageResponse` 序列化成 `---\n五个键值\n---\n正文` 的目标字符串。训练时见过、推理时复用，这就是模型默认就能吐出合法 front matter、连 guided decoding 都可开可不开的原因。

#### 4.2.4 代码实践

**实践目标**：把三句话提示词逐句注释，建立「每句话对应模型的一种行为约束」的直觉。

**操作步骤**：

```python
# 示例代码：打印并注释 v4 提示词
from olmocr.prompts import build_no_anchoring_v4_yaml_prompt

prompt = build_no_anchoring_v4_yaml_prompt()
print(prompt)

# 逐句拆解（在笔记中完成）：
# 句1 "Attached is one page of a document that you must process."
#     -> 定任务：单页文档处理
# 句2 "Just return the plain text representation ... Convert equations to LateX
#     and tables to HTML. If there are any figures or charts, label them with ..."
#     -> 定内容：自然阅读顺序 + 公式 LaTeX + 表格 HTML + 图片坐标标注
# 句3 "Return your output as markdown, with a front matter section on top
#     specifying values for the primary_language, is_rotation_valid,
#     rotation_correction, is_table, and is_diagram parameters."
#     -> 定格式：markdown + 顶部 YAML front matter 五参数
```

**需要观察的现象**：打印出的提示词很短（三句话），不含任何 `RAW_TEXT_START` 标记，也没有页面尺寸信息——与 4.1 节的 silver 提示词形成鲜明对比。

**预期结果**：总共约 5～6 行文本，末尾以 `...parameters.` 结束。可在轻量环境直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：`build_no_anchoring_v4_yaml_prompt()` 为什么不接收 `base_text` 参数？这对训练提出了什么要求？

**答案**：因为「no anchoring」设计下模型只依赖页面图片，无需传统 OCR 提供的文本拐杖。相应地，训练数据也必须按「图片进、markdown 出」的方式构造（dataloader 中的 `NewYamlFinetuningPromptWithNoAnchoring` + 页面渲染步骤），让模型在 SFT 中学会纯视觉转写。

**练习 2**：v4 与旧版 `build_no_anchoring_yaml_prompt`（[L156-L161](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L156-L161)）有三处差异，分别是什么？

**答案**：① 表格从 markdown 改为 HTML；② 新增图片标注指令（`![...](page_x_y_w_h.png)` 语法）；③ 任务描述从「PDF document」放宽为「document」（图片输入经 img2pdf 包装后同样走这条路）。两者输出格式约定（front matter 五参数）保持一致。

**练习 3**：请求里 `temperature` 为什么先写 0.0 再覆盖，而不是直接算好？

**答案**：`build_page_query` 只负责「渲染 + 组装」，不知道当前是第几次尝试；温度策略属于重试编排层（`try_single_page` 按 attempt 查 `TEMPERATURE_BY_ATTEMPT` 表）。占位 0.0 保证即便忘记覆盖，默认行为也是接近确定性的采样。

### 4.3 输出契约：PageResponse 数据类

#### 4.3.1 概念说明

`PageResponse` 是模型输出与流水线逻辑之间的**合同**。模型每页返回的 front matter + 正文，最终都被物化成一个 `PageResponse` 实例。六个字段按用途分三类：

| 字段 | 类型 | 类别 | 在流水线中的用途 |
| --- | --- | --- | --- |
| `is_rotation_valid` | `bool` | **控制流** | 为 False 时进入旋转纠错重试路径（[pipeline.py:L298](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L298)、[L308](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L308)） |
| `rotation_correction` | `int` ∈ {0,90,180,270} | **控制流** | 告诉流水线该顺时针转多少度再重试（[L299](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L299)、[L320](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L320)） |
| `natural_text` | `Optional[str]` | **载荷** | 真正的 OCR 文本：拼接进 Dolma 文档正文并记录页跨度（[L609-L617](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L609-L617)），`--markdown` 时直接落盘（[L765-L794](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L765-L794)） |
| `primary_language` | `Optional[str]` | **属性** | 写入 Dolma `attributes.primary_language` 列表（[L644](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L644)），下游可按语言筛选语料 |
| `is_table` | `bool` | **属性** | 写入 `attributes.is_table`（[L647](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L647)），标记该页主体是否为表格 |
| `is_diagram` | `bool` | **属性** | 写入 `attributes.is_diagram`（[L648](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L648)），标记该页主体是否为图表 |

注意提示词只要求 front matter 填**五个**参数——第六个字段 `natural_text` 不在 front matter 里，它就是 `---` 之后的正文，由解析器（下一讲）填入。

#### 4.3.2 核心流程

`PageResponse` 一旦构造成功，就保证内部数据合法，下游代码可以放心使用。校验发生在 `__post_init__`：

```
PageResponse(primary_language=..., is_rotation_valid=..., rotation_correction=...,
             is_table=..., is_diagram=..., natural_text=...)
    │ dataclass 自动 __init__ 赋值
    ▼
__post_init__:
    1) rotation_correction ∉ {0, 90, 180, 270} → ValueError
    2) 六个字段逐一 isinstance 类型检查 → TypeError
```

旋转纠错路径中的累计角度维护是一个小小的模运算：

\[ r_{\text{cum}} = (r_{\text{cum}} + c) \bmod 360, \quad c \in \{0, 90, 180, 270\} \]

即每次模型报告还需顺时针转 \( c \) 度时，流水线把它累加到已旋转的角度上再取模，作为下一轮 `build_page_query(image_rotation=...)` 的入参——所以旋转值被约束在四个合法值上不仅是「防呆」，还是这条模运算链能成立的前提。

#### 4.3.3 源码精读

- [olmocr/prompts/prompts.py:L66-L73](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L66-L73) — `@dataclass(frozen=True)` 修饰的 `PageResponse`，六个字段全部**没有默认值**：构造时必须一个不落地提供。`frozen=True` 使实例不可变——dataloader 中修改字段的方式是「新建实例」而非原地改（见 [olmocr/train/dataloader.py:L320-L321](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/dataloader.py#L320-L321) 的注释「Since PageResponse is frozen, we need to create a new instance」）。

- [olmocr/prompts/prompts.py:L75-L78](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L75-L78) — `__post_init__` 的第一道关卡：`rotation_correction` 不在 `{0, 90, 180, 270}` 集合中就抛 `ValueError("rotation_correction must be one of [0, 90, 180, 270].")`。这正是本讲实践任务要触发的异常。

- [olmocr/prompts/prompts.py:L80-L92](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L80-L92) — 第二道关卡：六个字段逐一 `isinstance` 检查，`primary_language` 和 `natural_text` 允许 `str` 或 `None`（`Optional` 的运行时体现），其余必须精确是 `bool` / `int`，违反抛 `TypeError`。注意顺序：合法性校验在前、类型校验在后——传 `rotation_correction=45.0`（float）会先命中 ValueError 分支（45.0 不在集合里），传 `rotation_correction=45`（int）才轮到值检查报错。

流水线中的消费点：

- [olmocr/pipeline.py:L94-L103](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L94-L103) — `PageResult` 数据类把 `response: PageResponse` 连同页码、token 数、`is_fallback`、`is_valid` 等流水线元信息打包，是 worker 层流转的单位。

- [olmocr/pipeline.py:L208-L212](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L208-L212) — `try_single_page` 拿到模型返回的 markdown 后，用 `FrontMatterParser(front_matter_class=PageResponse)` 解析成 `page_response`——非法的 rotation 值会在这里构造 `PageResponse` 时抛异常，被外层 `except Exception` 捕获记为本次尝试失败。

- [olmocr/pipeline.py:L296-L320](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L296-L320) — 旋转重试的起点：首次结果 `is_rotation_valid` 为 False 时，读 `rotation_correction` 更新累计角度（[L299](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L299)），随后带着旋转后的图片**串行**重试（[L311-L320](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L311-L320)），每次仍可能继续累积修正。

- [olmocr/pipeline.py:L233-L250](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L233-L250) — 兜底路径 `make_fallback_result` 用 `pdftotext` 提取的文本构造一个「朴素」的 `PageResponse`（语言为 None、旋转标记正常、非表格非图表）。关键设计：**即便最坏情况也要产出统一契约的对象**，下游 `build_dolma_document` 因此无需任何类型分支。

- [olmocr/pipeline.py:L602-L651](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L602-L651) — `build_dolma_document` 消费全部六字段：`natural_text` 拼正文并记录 `pdf_page_spans` 字符跨度（[L609-L617](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L609-L617)，None 视为空串），其余五个字段原样进入 `attributes`（[L644-L648](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L644-L648)）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手构造合法与非法的 `PageResponse`，观察校验器行为。

**操作步骤**：

```python
# 示例代码：PageResponse 校验实验（轻量环境即可运行）
from olmocr.prompts import PageResponse

# 1. 合法构造：六个字段全部提供
page = PageResponse(
    primary_language="en",
    is_rotation_valid=True,
    rotation_correction=0,
    is_table=False,
    is_diagram=False,
    natural_text="Hello, world!",
)
print(page)  # dataclass 自动生成的 repr

# 2. 非法 rotation：45 度不在允许集合
try:
    PageResponse(
        primary_language="en",
        is_rotation_valid=False,
        rotation_correction=45,   # ← 罪魁
        is_table=False,
        is_diagram=False,
        natural_text="rotated?",
    )
except ValueError as e:
    print("ValueError:", e)

# 3. 类型错误：布尔字段传字符串
try:
    PageResponse(
        primary_language="en",
        is_rotation_valid=True,
        rotation_correction=0,
        is_table="yes",           # ← 不是 bool
        is_diagram=False,
        natural_text=None,
    )
except TypeError as e:
    print("TypeError:", e)

# 4. frozen 实验：尝试修改字段
try:
    page.is_table = True
except Exception as e:
    print(type(e).__name__, ":", e)
```

**需要观察的现象**：

1. 第 1 步打印出完整 repr。
2. 第 2 步抛出 `ValueError: rotation_correction must be one of [0, 90, 180, 270].`。
3. 第 3 步抛出 `TypeError: is_table must be of type bool.`。
4. 第 4 步抛出 `FrozenInstanceError`（`dataclasses.FrozenInstanceError`，它是 `AttributeError` 的子类）。

**预期结果**：以上四种行为全部按源码 L75-L92 的校验顺序发生。已对照源码逐行核实，可直接运行验证。

#### 4.3.5 小练习与答案

**练习 1**：模型输出 `rotation_correction: 45` 会发生什么？这个异常最终被谁接住？

**答案**：`FrontMatterParser` 尝试用该字典构造 `PageResponse` 时，`__post_init__` 抛 `ValueError`。在 `try_single_page` 中，这个异常落进最外层的 `except Exception`（[pipeline.py:L228-L230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L228-L230)），被记为一次 warning 并返回 None，该页进入重试流程——坏输出被当成「可重试的失败」而非崩溃。

**练习 2**：为什么 `primary_language` 和 `natural_text` 允许为 `None`，而其余四个字段不允许？

**答案**：空白页可能「没有任何值得转写的文本」，此时语言与正文都不存在，但页面本身依然可以被描述（旋转有效、非表格、非图表）。让这两个字段可空，就避免了「空白页逼模型编造内容」的幻觉风险——这与 silver 提示词里「没有文本可输出 null」的指令一脉相承。

**练习 3**：`make_fallback_result` 为什么把 `is_rotation_valid` 硬编码为 True、`rotation_correction` 硬编码为 0？

**答案**：fallback 用 `pdftotext` 直接从 PDF 内部提取文本层，根本不做图像旋转，不存在「方向不对」的问题；构造一个「旋转正常」的 `PageResponse` 可以防止兜底结果再次触发旋转重试路径，让控制流在此终结。

### 4.4 机器可读契约：openai_response_format_schema

#### 4.4.1 概念说明

`PageResponse` 是给 **Python 代码**用的契约；`openai_response_format_schema()` 返回的 JSON Schema 是给 **OpenAI 兼容 API** 用的同一份契约。当 olmOCR 反过来调用 GPT-4o（生成 silver 数据、跑 bench、挖空白页）时，需要约束老师的输出格式，就把它塞进请求的 `response_format` 字段，让 API 在解码层面强制输出合规 JSON。

#### 4.4.2 核心流程

项目里实际存在**三种**结构化输出机制，各管一段：

| 机制 | 作用对象 | 强制手段 | 出处 |
| --- | --- | --- | --- |
| `openai_response_format_schema` | GPT-4o 等外部 API（老师） | `json_schema` + `strict: True`，API 保证输出匹配 schema | prompts.py L95-L142 |
| `guided_regex` | 本地 vLLM（学生，可选开关） | 正则约束解码（`--guided-decoding`） | pipeline.py L179-L182 |
| 微调即格式 | 微调后的学生模型（默认路径） | SFT 训练让 front matter 成为模型的固有行为 | dataloader.py L248-L266 |

默认推理路径靠第三种——零运行时开销；前两种是「不信任模型自律」时的保险丝。

#### 4.4.3 源码精读

- [olmocr/prompts/prompts.py:L95-L142](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L95-L142) — `openai_response_format_schema()` 返回 OpenAI 结构化输出格式的 dict。关键点：
  - [L111-L116](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L111-L116)：`rotation_correction` 用 `"enum": [0, 90, 180, 270]` + `"default": 0` 表达约束——与 `PageResponse.__post_init__` 的集合、`guided_regex` 的 `(?:0|90|180|270)` 三处一字不差。
  - [L103-L105](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L103-L105) 与 [L125-L128](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L125-L128)：`primary_language`、`natural_text` 的类型写成 `["string", "null"]`，对应 Python 的 `Optional[str]`；每个字段的 `description` 会展示给被约束的模型，本身就是提示词的一部分（例如 [L107-L110](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L107-L110) 教模型「判断旋转只看文字内容，别管图表旋转」）。
  - [L130-L138](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L130-L138)：`additionalProperties: False` + 六个字段全部列入 `required`——这是 OpenAI `strict` 模式的硬性要求：禁止额外键、禁止缺键。
  - [L140](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L140)：`"strict": True` 开启严格模式，API 保证第一个 token 起就逐 token 符合 schema。

调用方示例（均为「olmOCR 反向调用更强模型」的场景）：

- [olmocr/data/buildsilver.py:L59](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/buildsilver.py#L59) — 生成 silver 数据时把 schema 放进请求的 `response_format`，让 GPT-4o 直接输出可解析为 `PageResponse` 的 JSON（该文件 [L120](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/runners/run_chatgpt.py#L120) 风格的 `PageResponse(**data)` 反序列化在 bench runner 中同样出现）。
- [olmocr/bench/miners/mine_blank_pages_gpt.py:L108](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/miners/mine_blank_pages_gpt.py#L108) — 用 schema 约束 GPT-4o 判定空白页。

注意一个易混点：**本地 vLLM 推理路径并不使用这个 JSON schema**——v4 提示词要求的是 markdown + front matter（ YAML 而非 JSON），可选约束是 `guided_regex`。JSON schema 只服务于「调外部 OpenAI 兼容 API」的那些数据构建与 bench 场景。

#### 4.4.4 代码实践

**实践目标**：验证「三份契约描述的是同一个数据结构」。

**操作步骤**：

```python
# 示例代码：契约一致性检查
import dataclasses
import re
from olmocr.prompts import PageResponse, openai_response_format_schema

# 1. dataclass 字段名集合
dc_fields = {f.name for f in dataclasses.fields(PageResponse)}

# 2. schema properties 键集合
schema = openai_response_format_schema()
schema_fields = set(schema["json_schema"]["schema"]["properties"].keys())

# 3. pipeline guided_regex 中出现的字段名（手工摘录自 pipeline.py L181）
guided = "primary_language is_rotation_valid rotation_correction is_table is_diagram".split()

print("dataclass:", sorted(dc_fields))
print("schema   :", sorted(schema_fields))
print("diff     :", dc_fields ^ schema_fields)
print("schema required == properties:",
      set(schema["json_schema"]["schema"]["required"]) == schema_fields)
print("rotation enum:", schema["json_schema"]["schema"]["properties"]["rotation_correction"]["enum"])
```

**需要观察的现象**：`diff` 为空集；`required == properties` 为 True；enum 恰为 `[0, 90, 180, 270]`；`guided` 里的五个名字是 `dc_fields` 去掉 `natural_text` 后的子集。

**预期结果**：如上全部成立（已对照源码核实），说明六字段契约在 dataclass、JSON Schema、正则三处保持同步。

#### 4.4.5 小练习与答案

**练习 1**：schema 中 `is_rotation_valid` 的 description 为什么要写「只根据文字内容判断，不要考虑图表、表格、图形的旋转」？

**答案**：现实文档中横排表格、旋转的坐标轴标签很常见，若模型把「图表是横的」也算作页面方向错误，会触发不必要的旋转重试，反而毁掉本来正确的转写。这句 description 是在预防一类特定的误判，也再次说明 schema 的 description 本身就是给模型的指令。

**练习 2**：为什么 `strict: True` 模式下必须 `additionalProperties: False` 且所有字段进 `required`？

**答案**：这是 OpenAI 严格模式的接口规定——严格模式靠「完全确定的输出形状」来保证逐 token 合规；如果允许额外键或可省略键，模型在解码时就要做「要不要输出这个键」的选择，会产生不满足确定性的分支，所以 API 直接拒绝这种 schema。

**练习 3**：既然有 `guided_regex`，为什么默认推理不开启 `--guided-decoding`？

**答案**：因为模型经 SFT/GRPO 训练后已经稳定输出 front matter 格式（「微调即格式」），运行时语法约束是多余开销；且 guided decoding 会给 vLLM 增加编译语法、约束解码的计算成本。正则选项留作对付不自控模型（如直接拿基座 Qwen2.5-VL 跑）时的保险丝。

## 5. 综合实践

**任务：写一个「模拟单页请求」的只读脚本，把本讲三个模块串起来。**

要求你的脚本做到：

1. 打印 `build_no_anchoring_v4_yaml_prompt()`，并在注释里给每句话标注它约束的是「内容」还是「格式」。
2. 手工拼出一个「假想的模型输出」markdown 字符串（front matter 五行 + 若干正文），字段值自拟，但 `rotation_correction` 用合法值。
3. 参照 `FrontMatterOutputFormat`（[olmocr/train/dataloader.py:L248-L266](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/dataloader.py#L248-L266)）的序列化格式，写一个 10 行以内的迷你解析函数，把你的假想输出解析成 `PageResponse` 实例（提示：front matter 每行形如 `key: value`，布尔解析 `{"True": True, "False": False, "null": None}`，正文赋给 `natural_text`）。
4. 打印解析出的 `PageResponse`，确认 repr 中六个字段齐全。
5. 把假想输出里的 `rotation_correction` 改成 45 再解析一次，捕获并打印 `ValueError`。

**预期结果**：脚本输出一个合法 `PageResponse` 的 repr、一行 `ValueError: rotation_correction must be one of [0, 90, 180, 270].`，全程不需要 GPU 和网络（若第 3 步解析行为与预期不符，属「待本地验证」，可对照下一讲 u2-l5 的 `FrontMatterParser` 源码核对你的迷你实现差在哪）。这个练习同时是下一讲的完美预习。

## 6. 本讲小结

- prompts.py 的提示词是一条演进链：四个 GPT-4o silver 提示词（老师用，从带锚文本到 v3_simple 纯图+尺寸、约束逐版收紧）→ 简化的 `build_finetuning_prompt` → 当前推理用的 `build_no_anchoring_v4_yaml_prompt`（无参、纯视觉、三句话）。
- 当前推理提示词要求输出「markdown + 五参数 YAML front matter」；表格约定为 HTML、公式为 LaTeX、图片用坐标标注语法——这些格式与 bench 校验器和老师格式一一对齐。
- `PageResponse` 六字段三分法：`is_rotation_valid` + `rotation_correction` 驱动旋转重试控制流（累计角度做 mod 360），`natural_text` 是文本载荷（拼 Dolma 正文、写 markdown），`primary_language` / `is_table` / `is_diagram` 是 Dolma 属性。
- `__post_init__` 双关卡：非法旋转值抛 `ValueError`，字段类型不符抛 `TypeError`；`frozen=True` 强制「改字段 = 新建实例」。兜底路径 `make_fallback_result` 也产出统一契约，下游因此无需分支。
- 三种结构化输出机制各司其职：`openai_response_format_schema`（调外部 API 时用，`strict: True`）／`guided_regex`（vLLM 可选开关）／「微调即格式」（默认路径，零开销）——三处对 `rotation_correction` 的约束一字不差。

## 7. 下一步学习建议

下一讲 **u2-l5 front matter 解析**正好接住本讲埋下的线头：精读 [olmocr/train/front_matter.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/front_matter.py) 的 `FrontMatterParser`，看它如何从模型输出的 markdown 里提取 YAML、做类型转换、特判 `natural_text` 并组装成 `PageResponse`。之后 u2-l6 会回到 pipeline.py，把本讲的 `is_rotation_valid` / `rotation_correction` 放进完整的重试与温度阶梯编排中。若你想提前看清训练侧如何消费同一契约，可以浏览 `olmocr/train/dataloader.py` 中所有 `PageResponse(` 出现的位置。
