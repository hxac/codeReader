# Cookbooks 能力地图与提示词模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 把 `cookbooks/` 目录下的能力笔记本按「识别 / OCR 与文档 / 定位与空间（含视频）/ Agent 与编码」四族归类，并说出每族的代表笔记本与输出格式约定。
2. 从任何一本 cookbook 中拆出三层结构：**任务指令 → 输出格式协议 → 坐标与像素预算约定**，并能复用到自己的业务提示词里。
3. 看懂 cookbook 的通用代码骨架（可视化工具 + 本地推理函数 + API 推理函数 + 用例单元格），知道哪些代码可以直接抄进自己的项目。
4. 跑通 `ocr.ipynb` 与 `video_understanding.ipynb` 的核心单元格，并产出一份「系统提示 vs 用户提示」对照表。
5. 面对一个实际业务需求（票据录入、货架盘点、视频剪辑定位……），能快速选出该参考哪几本 cookbook、抄哪些提示词模板。

## 2. 前置知识

本讲假设你已完成 u1-l3（第一次推理），知道下面几件事。忘了的话先回去翻一眼：

- **messages 结构**：`content` 是一个列表，可混排 `{"type": "text", ...}`、`{"type": "image", ...}`、`{"type": "video", ...}` 元素，顺序即模型接收顺序。
- **推理三步**：`apply_chat_template` 渲染 → `model.generate` 自回归解码 → 逐样本裁剪 `out_ids[len(in_ids):]` 后 `batch_decode`。
- **视觉 token 与像素预算**：一张 H×W 的图被压缩为 \((H/32) \times (W/32)\) 个视觉 token；所以像素预算的真正单位是 token：\(\text{pixels} = \text{token 数} \times 32 \times 32\)。cookbook 里到处出现的 `min_pixels=512*32*32`，含义就是「这张图至少给 512 个视觉 token」。
- **视频时间维 2× 压缩**：视频 token 数 ≈ 帧数 ÷ 2 × 单帧 token 数；控制总预算用 `total_pixels`。
- **相对坐标**：Qwen3-VL 的 grounding 输出默认是 0～1000 的相对坐标（区别于 Qwen2.5-VL 的绝对像素坐标），画框时按 `x / 1000 * 图宽` 还原。

两个本讲新术语：

- **cookbook（食谱书）**：官方为每项能力写的一个 Jupyter 笔记本，本质是「提示词样例 + 可运行代码骨架」的组合。它不是库、不是 benchmark，而是**提示词工程的标准答案集**。
- **魔法提示（magic prompt）**：个别任务用极短的触发词（如 `qwenvl html`）让模型切换到专门训练过的输出模式，详见 4.2.3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md:L109-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L109-L126) | 官方 Cookbooks 章节与 12 本笔记本的功能表（本讲能力地图的权威来源） |
| [cookbooks/ocr.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb) | OCR 与关键信息提取（本讲精读对象一） |
| [cookbooks/video_understanding.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb) | 视频理解与时空定位（本讲精读对象二） |
| cookbooks/document_parsing.ipynb | Qwen 专属 HTML/Markdown 文档解析格式 |
| cookbooks/long_document_understanding.ipynb | 数百页 PDF 长文档理解 |
| cookbooks/german_document_ocr.ipynb | 德语文档 OCR（README 表未收录的补充笔记本） |
| cookbooks/omni_recognition.ipynb | 万物识别（名人/动漫/商品/动植物） |
| cookbooks/2d_grounding.ipynb | 2D 框/点定位（相对坐标 + 多目标） |
| cookbooks/3d_grounding.ipynb | 3D 边界框定位（相机坐标系 9 元组） |
| cookbooks/spatial_understanding.ipynb | 空间关系 / 可供性 / 动作规划推理 |
| cookbooks/mobile_agent.ipynb、cookbooks/computer_use.ipynb | GUI Agent（手机/电脑操控） |
| cookbooks/think_with_images.ipynb | qwen-agent 缩放/搜索工具调用 |
| cookbooks/mmcode.ipynb | 图生 HTML、图生代码、多模态编程挑战 |
| [cookbooks/utils/agent_function_call.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py) | `MobileUse`/`ComputerUse` 工具定义（下一讲 u7-l2 精读） |

> **行号说明**：`.ipynb` 是 JSON 文件，本文给出的 `#L` 行号指向该 JSON 源文件的行。在 GitHub 上打开笔记本后切换到 "Code" 视图（raw）即可对照；每段引用旁边都附有中文说明与原始提示词文本，可脱离行号独立阅读。

## 4. 核心概念与源码讲解

### 4.1 能力分类地图

#### 4.1.1 概念说明

`cookbooks/` 目录是官方能力的「 showroom」：每个笔记本演示一项能力，配素材图、可视化代码与提示词。理解它的正确姿势不是逐本读完，而是先建立**分类地图**——同一族笔记本共享几乎相同的代码骨架与提示词模式，读通一本等于读通一族。

先核对一个基本事实：[README.md:L113-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L113-L126) 的表格收录了 **12 本** cookbook；而 `cookbooks/` 目录下实际有 **13 个** `.ipynb`——多出的 `german_document_ocr.ipynb` 未列入 README 表格。清点文件时不要被这个差异迷惑。

把 12 本按输出产物归类，可归并为四大族：

| 能力族 | 笔记本 | 输出产物 | 关键输出协议 |
| --- | --- | --- | --- |
| **识别** | omni_recognition | 实体名称（可带框） | 自由文本 / `bbox_2d`+双语 `label` JSON |
| **OCR 与文档解析** | ocr、document_parsing、long_document_understanding（+german_document_ocr） | 纯文本 / 结构化键值 / Qwen HTML / Qwen Markdown | `bbox_2d`+`text_content` JSON、`qwenvl html/markdown` 魔法提示 |
| **定位与空间（含视频）** | 2d_grounding、3d_grounding、spatial_understanding、video_understanding | 坐标（框/点/3D 框/时间戳） | `bbox_2d`/`point_2d`/`bbox_3d`/`time` JSON，0～1000 相对坐标 |
| **Agent 与编码** | mobile_agent、computer_use、think_with_images、mmcode | 工具调用 / 代码 | `<tool_call>` JSON、markdown 代码块 |

这个归类与 [README.md:L24-L40](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L24-L40) 列出的八大 Key Enhancements 一一呼应：Visual Agent ↔ Agent 族、Visual Coding Boost ↔ mmcode、Advanced Spatial Perception ↔ grounding 族、Long Context & Video ↔ 长文档与视频、Upgraded Visual Recognition ↔ 识别族、Expanded OCR ↔ OCR 族。**cookbooks 就是 README 能力清单的可执行版本。**

#### 4.1.2 核心流程

翻开任何一本 cookbook，单元格顺序都遵循同一个五段骨架：

```text
① Setup        ：pip install transformers / qwen-vl-utils / openai（Agent 本另装 qwen-agent）
② 可视化工具   ：parse_json（剥 ```json 围栏）+ plot_*（坐标 /1000 还原画框）
③ 推理函数双轨 ：inference()          —— 本地 HF 模型加载 + generate
                inference_with_api() —— OpenAI 兼容客户端 + DashScope base_url + base64 图
④ 用例单元格   ：image_path + prompt = "..." + 调用 + display 可视化
⑤ （可选）评测 ：mmcode 下载 MMCode 测试集跑分
```

而所有用例的提示词都服从同一条**三段式模式**：

```text
任务动词（Read / Spotting / Locate / Identify / Extract / Convert / Find）
+ 作用范围（all the text / every instance / the top right brown cake）
+ 输出格式（in JSON format as [{'bbox_2d': [x1, y1, x2, y2], 'text_content': 'text'}, ...]）
```

配套的四条全局约定：

1. **JSON schema 直接内嵌在提示里，键名即协议**：`bbox_2d`、`point_2d`、`bbox_3d`、`text_content`、`label`、`time`。下游代码按这些键名解析。
2. **坐标口径**：默认 0～1000 相对坐标；个别文档写 0～999（见 4.2.2 的辨析）。
3. **系统提示多数留空**：绝大多数用例注释掉了 system prompt（默认 "You are a helpful assistant."），只有重任务（Agent 循环、竞赛编程）才写实质性系统提示。
4. **像素预算按任务定档**（32 口径，括号内为换算的视觉 token 数）：
   - OCR/文本定位：`512*32*32 ~ 2048*32*32`（512～2048 token/图）
   - grounding 默认：`64*32*32 ~ 9800*32*32`（64～9800 token/图）
   - 长文档每页：`512*32*32 ~ 730*32*32`（512～730 token/页，控制总长）
   - 视频：`total_pixels=20480*32*32`（全局总预算约 20480 token，另有 `max_frames=2048`）

#### 4.1.3 源码精读

**（a）README 能力表。** [README.md:L113-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L113-L126) 逐行列出 12 本笔记本的中性英文描述，每行末尾附 Colab 徽章——官方的意图就是「点开即跑」。这张表是能力地图的权威索引，比目录名更可信（目录里多了 german_document_ocr）。

**（b）骨架实证：ocr.ipynb 的前三段。** 以 OCR 本为例验证五段骨架：

- 可视化工具：[ocr.ipynb:L106-L155](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L106-L155) 定义 `plot_text_bounding_boxes`——把模型返回的 `bbox_2d` 相对坐标除以 999 还原到像素并画框；[ocr.ipynb:L157-L166](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L157-L166) 定义 `parse_json`——剥掉模型输出里可能包裹的 ` ```json ` markdown 围栏。这两个函数几乎是所有 cookbook 的标配。
- 本地推理：[ocr.ipynb:L168-L202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L168-L202) 的 `inference()` 组装 messages、`apply_chat_template(tokenize=False)`、`processor(text=..., images=[image], ...)`、`generate`、裁剪、解码——正是 u1-l3 的调用链装进函数。
- API 推理：[ocr.ipynb:L204-L250](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L204-L250) 的 `inference_with_api()` 把图转 base64 塞进 `image_url`，经 OpenAI 兼容客户端发往 DashScope，默认 `model_id="qwen3-vl-235b-a22b-instruct"`，并把 `min_pixels`/`max_pixels` 直接挂在图像元素上。

**（c）一个需要留意的代际混用。** [ocr.ipynb:L279-L285](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L279-L285) 本地演示加载的 checkpoint 是 `Qwen/Qwen2.5-VL-7B-Instruct`（用 `Qwen2_5_VLForConditionalGeneration`），而 API 路径才是 Qwen3-VL。也就是说：**cookbook 的提示词与解析代码是 Qwen3-VL 口径，本地演示模型却停留在上一代**。自己复现时建议把本地 checkpoint 换成 `Qwen/Qwen3-VL-2B-Instruct` 并改用 `AutoModelForImageTextToText`（README Quickstart 的写法）。类似的混用还出现在 video 本（本地模型路径是占位符 `"***"`，见 [video_understanding.ipynb:L56-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L56-L66)），而 long_document 本地路径是正宗的 `Qwen/Qwen3-VL-4B-Instruct`（[long_document_understanding.ipynb:L68-L78](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/long_document_understanding.ipynb#L68-L78)）。读 cookbook 时先看它到底加载了哪个模型。

#### 4.1.4 代码实践（零 GPU，目录清点 + 建图）

1. **实践目标**：亲手建立 12→4 的能力映射表，并验证「README 表 12 本 vs 目录 13 个 ipynb」的差异。
2. **操作步骤**：
   - 在仓库根目录执行 `ls cookbooks/*.ipynb`，记录文件清单；
   - 打开 [README.md:L113-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L113-L126)，把表格里的 12 个链接与目录文件逐一比对；
   - 按 4.1.1 的表格把每个文件归入四族，写进自己的笔记。
3. **需要观察的现象**：目录中有 `german_document_ocr.ipynb`，但 README 表格里没有它；`utils/` 与 `assets/` 两个子目录分别存放工具代码与素材图。
4. **预期结果**：得到一张 13 行的映射表（12 本在表 + 1 本不在表），每行标注能力族、输出协议、是否需要 GPU。本实践为纯源码阅读，无需运行模型，结果可直接核对。

#### 4.1.5 小练习与答案

**练习 1**：同事说「cookbook 就是官方 SDK 吧？」请用一句话纠正。
**答案**：cookbook 不是 SDK，它是提示词样例库 + 可抄的代码骨架——本仓库唯一可 pip 安装的库是 `qwen-vl-utils`（u2-l1），cookbook 里的代码需要你自己复制进项目。

**练习 2**：`min_pixels=512*32*32` 对应多少视觉 token？
**答案**：512 个。由 \(\text{tokens} = \text{pixels} / 32^2 = 512 \times 32^2 / 32^2 = 512\)。

**练习 3**：为什么 long_document 笔记本把每页的 `max_pixels` 压到 `730*32*32` 这么低？
**答案**：长文档有几十到上百页，总 token 数 ≈ 页数 × 每页 token 数；每页限到 730 token，23 页也才约 1.7 万 token，才能整体塞进 256K 上下文并控制显存与延迟（对应 [long_document_understanding.ipynb:L245-L323](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/long_document_understanding.ipynb#L245-L323) 中 `inference` 的默认参数）。

---

### 4.2 OCR 与文档解析类

#### 4.2.1 概念说明

这一族回答「**把图里的文字/结构变成机器可用的数据**」，按结构化程度递进分四档：

1. **全文转写**（ocr 前两节）：图 → 纯文本，多语言（官方称支持 32 种语言，低光/模糊/倾斜鲁棒）。
2. **文本定位（text spotting）**（ocr 第三节）：图 → `[{bbox_2d, text_content}]`，既要知道写了什么，还要知道写在哪。
3. **关键信息提取（KIE）**（ocr 第四节）：图 → 按给定 schema 的键值 JSON，发票/证件录入的核心能力。
4. **整页结构解析**（document_parsing、long_document_understanding、german_document_ocr）：图/PDF → 带版面坐标的 HTML/Markdown，或数百页级语义问答。

结构化程度越高，提示词里**输出格式的权重**越大——这正是本族提示词模式的看点。

#### 4.2.2 核心流程

OCR 本的四个用例构成一条「约束递增」的提示词阶梯：

```text
用例 1  "Read all the text in the image."                       → 自由文本
用例 2  "Please output only the text content ... without
         any additional descriptions or formatting."            → 纯文本（禁止废话）
用例 3  "Spotting all the text ... output in JSON format as
         [{'bbox_2d': [x1, y1, x2, y2], 'text_content': 'text'}]" → 定位 JSON
用例 4  "Extract the key-value information in the format:
         {\"company\": \"\", \"date\": \"\", ...}"                → 给定 schema 的键值 JSON
```

下游处理链固定为：模型输出（可能裹着 ` ```json `）→ `parse_json` 剥围栏 → `ast.literal_eval`/`json.loads` → 坐标 `÷999`（该本口径）还原画框。

一个值得辨析的细节：ocr 笔记本的 markdown 说明写「Qwen3-VL 的文本定位坐标是 0～999 的相对坐标」，其画框函数 [ocr.ipynb:L135-L139](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L135-L139) 也确实除以 999；而 2d_grounding、video_understanding 等本的官方口径是 0～1000。两者相差不超过 0.1%，工程上都成立，但**解析与画框必须用同一口径**，混用会引入系统性偏移。以 README 与多数 notebook 的 0～1000 为准即可。

#### 4.2.3 源码精读

**（a）四个用例的原始提示词。**

- 全页英文 OCR：[ocr.ipynb:L337-L345](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L337-L345)——`prompt = "Read all the text in the image."`，素材是 `./assets/ocr/ocr_example2.jpg`。
- 多语言纯文本：[ocr.ipynb:L406-L414](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L406-L414)——`"Please output only the text content from the image without any additional descriptions or formatting."`；中文版在 [ocr.ipynb:L465-L473](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L465-L473)——`"请识别出图中所有的文字。"`。注意「不加任何描述和格式」这个否定式约束，是压制模型输出「以下是识别结果：」这类废话的关键。
- 行级文本定位：[ocr.ipynb:L590-L612](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L590-L612)——`"Spotting all the text in the image with line-level, and output in JSON format as [{'bbox_2d': [x1, y1, x2, y2], 'text_content': 'text'}, ...]."`；把 `line-level` 换成 `word-level`（[ocr.ipynb:L673-L695](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L673-L695)）即得词级粒度——**粒度是提示词里的一个词**。该用例同时把预算提到 `min_pixels=512*32*32, max_pixels=2048*32*32`，因为小字识别需要更多视觉 token。
- 关键信息提取：英文 schema 版 [ocr.ipynb:L759-L775](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L759-L775)——`"Extract the key-value information in the format:{\"company\": \"\", \"date\": \"\", \"address\": \"\", \"total\": \"\"}"`；中文发票版 [ocr.ipynb:L828-L845](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L828-L845)——直接把九个字段名（发票代码、发票号码、到站、燃油费、票价……）列进提示并要求 json 输出。**schema 即提示**，字段清单抄进去就能用。

**（b）document_parsing 的魔法提示。** [document_parsing.ipynb:L586-L596](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/document_parsing.ipynb#L586-L596) 里整条提示只有两个词：`prompt = "qwenvl html"`；[document_parsing.ipynb:L854-L864](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/document_parsing.ipynb#L854-L864) 则是 `prompt = "qwenvl markdown"`。这是专门训练过的输出模式开关：

- **Qwen HTML 格式**：每个元素带 `data-bbox="x1 y1 x2 y2"` 属性（0～1000 相对坐标），可精确重建版面。解析见 [document_parsing.ipynb:L216-L274](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/document_parsing.ipynb#L216-L274) 的 `draw_bbox_html`——用 BeautifulSoup 找出所有 `data-bbox` 元素画框。
- **Qwen Markdown 格式**：表格转 LaTeX 且表格前标注坐标注释、图片替换为坐标占位符。解析见 [document_parsing.ipynb:L276-L308](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/document_parsing.ipynb#L276-L308) 的 `draw_bbox_markdown`——正则抓 `<!-- Image/Table (x1, y1, x2, y2) -->` 注释。
- [document_parsing.ipynb:L310-L400](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/document_parsing.ipynb#L310-L400) 的 `clean_and_format_html` 演示了反向清洗：删 `data-bbox`/`data-polygon`、把 `formula.machine_printed`/`formula.handwritten` 类名归并为 `formula`，得到可正常渲染的普通 HTML。

**（c）长文档 = 多图输入。** [long_document_understanding.ipynb:L140-L243](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/long_document_understanding.ipynb#L140-L243) 的 `get_pdf_images` 用 `pdf2image` 按 144 DPI 把 PDF 逐页转图、最长边压到 1500 像素、按 PDF 路径 MD5 缓存；随后 [L245-L323](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/long_document_understanding.ipynb#L245-L323) 的 `inference` 把每一页作为一个 `image_url` 元素（各带 `min_pixels=512*32*32, max_pixels=730*32*32`）追加进同一条 content，最后放一句 prompt（如 `"Please summarize the key contributions of this paper based on its abstract and introduction."`）。**长文档理解的全部秘密就是「PDF → 页图序列 → 多图 messages」**，没有别的魔法。

**（d）german_document_ocr：垂直领域模板。** 该本把发票、表单、证件、整页四类文档各写成一条带德语字段名 schema 的提示，运行时按文档类型取用（[german_document_ocr.ipynb:L448-L456](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/german_document_ocr.ipynb#L448-L456) 的 `prompt = prompts.get(document_type, ...)`），并提供批量处理函数（[L432 起](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/german_document_ocr.ipynb#L432-L447)）与 GDPR 合规提示。它是「KIE 提示词工程 + 领域字段表」的直接范本。

#### 4.2.4 代码实践（跑通 ocr.ipynb 的文本定位单元格）

1. **实践目标**：亲眼看一次「提示词 → bbox_2d JSON → 画框」的完整闭环。
2. **操作步骤**：
   - 准备环境：`pip install "transformers>=4.57.0" qwen-vl-utils openai pillow`；
   - 方式 A（无 GPU/API Key，推荐先做）：在 Colab 打开 README 表格里 ocr 行的徽章链接，只执行 Setup、工具函数与「3. Text Spotting」单元格，填写 `DASHSCOPE_API_KEY`；
   - 方式 B（本地 GPU）：把 [ocr.ipynb:L279-L285](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L279-L285) 的 checkpoint 换成 `Qwen/Qwen3-VL-2B-Instruct`，并取消用例单元格里 `inference(...)` 的注释；
   - 把返回文本手动粘给 `parse_json` 与 `plot_text_bounding_boxes` 复现画框。
3. **需要观察的现象**：模型返回的是形如 `[{'bbox_2d': [x1, y1, x2, y2], 'text_content': '...'}, ...]` 的 JSON（可能被 ` ```json ` 包裹）；坐标数值全部落在 0～999 区间；`line-level` 与 `word-level` 两次运行的框数量明显不同（行数 << 词数）。
4. **预期结果**：每行/每词文本都有一个框，框内文字与 `text_content` 一一对应。若在本地无 GPU 环境运行方式 B 会因显存不足失败——本实践依赖外部 API 或本地 GPU，具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：要把「识别整页文字」升级为「只提取发票代码与发票号码」，提示词该怎么改？
**答案**：从自由转写改为给定 schema 的 KIE 模式，参照 [ocr.ipynb:L828-L845](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L828-L845)：列出字段名清单并声明「按照json格式输出」。

**练习 2**：`qwenvl html` 输出里，某个 `<div data-bbox="100 200 300 400">` 在 800×600 的图上对应哪个像素框？
**答案**：坐标是 0～1000 相对制，x1=100/1000×800=80，y1=200/1000×600=120，x2=240，y2=240，即左上角 (80,120)、右下角 (240,240) 的矩形。

**练习 3**：为什么 text spotting 用例要把 `max_pixels` 从默认提到 `2048*32*32`？
**答案**：小字号文本需要更高的有效分辨率才有足够视觉 token 刻画笔画细节；预算换算为单图至多 2048 个视觉 token（[ocr.ipynb:L604-L612](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L604-L612) 中显式设置）。

---

### 4.3 识别与 grounding 类（含视频时空定位）

#### 4.3.1 概念说明

这一族回答「**这是什么 / 在哪里 / 什么关系**」，是 Qwen3-VL 空间感知能力的主体：

- **omni_recognition**：万物识别（名人、动漫角色、商品、地标、动植物），可顺带输出坐标——「识别」与「定位」在此合流。
- **2d_grounding**：2D 框/点定位。Qwen3-VL 两项关键升级：坐标从 Qwen2.5-VL 的绝对像素制改为 **0～1000 相对制**（不用再算 resized_w），以及**多目标 grounding** 增强。
- **3d_grounding**：输出相机坐标系下的 3D 边界框（9 元组），面向具身智能与自动驾驶。
- **spatial_understanding**：不做定位输出，而是空间**关系推理**——上下左右、遮挡、可供性（affordance，物体可供使用的属性）、动作规划。
- **video_understanding**：把识别与定位推广到时间轴——视频 OCR、长视频理解、事件时间定位、时空联合定位（VidSTG）。

本族的提示词模式在 4.1 的三段式之上多了一条：**坐标类型与粒度由提示词选择**（bbox 还是 point、行级还是词级、2D 还是 3D、单目标还是多目标）。

#### 4.3.2 核心流程

**2D 定位提示词的三种形态**（作用范围递增）：

```text
① 类别清单式（多目标检测）：
   locate every instance that belongs to the following categories:
   "plate/dish, scallop, wine bottle, ..." . Report bbox coordinates in JSON format.
② 描述指代式（单目标）：
   Locate the top right brown cake, output its bbox coordinates using JSON format.
③ 附加键扩展式（定位 + 属性）：
   ... report bbox coordinates, vehicle type and vehicle color in JSON format
   like this: {"bbox_2d": [...], "label": "vehicle", "type": "...", "color": "..."}
```

**视频链路**（video 本，三种输入形态共享一条推理函数）：

```text
构造 messages（video 元素携带 total_pixels/min_pixels/max_frames/sample_fps）
→ process_vision_info(image_patch_size=16, return_video_kwargs=True,
                      return_video_metadata=True)     # u2 系列讲的预处理库在此上岗
→ zip(*video_inputs) 拆出 (帧张量, video_metadata) 二元组
→ processor(..., videos=..., video_metadata=..., **video_kwargs, do_resize=False)
→ generate → 裁剪 → 解码 → parse_json → bbox ÷1000 画框
```

这正是 u2-l4 讲过的「时间戳元数据」机制在应用侧的落点：metadata 里携带的 fps/帧号让模型能把「第 i 帧」换算成「第 x 秒」，从而支撑秒级事件定位。

#### 4.3.3 源码精读

**（a）坐标制式声明。** [2d_grounding.ipynb:L8-L15](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L8-L15) 开篇即写明两项升级：坐标改为 0～1000 相对制（不再需要计算 resized_w）、多目标 grounding 增强。画框还原在 [2d_grounding.ipynb:L124-L211](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L124-L211) 的 `plot_bounding_boxes`（`int(bbox["bbox_2d"][0] / 1000 * width)`），点定位版本 [L213-L241](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L213-L241) 的 `plot_points`。

**（b）三种形态的原始提示词。** 类别清单式见 [2d_grounding.ipynb:L482-L500](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L482-L500)（餐桌多目标、人群、4K 无人机视角三个例子共用同一句式；注释明确「负类别也支持，生成时会被跳过」——清单里故意放不存在的类是合法用法）；附加键扩展式见 [L869-L885](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L869-L885)（车辆 + 类型 + 颜色），点定位同样支持扩展（足球场球员 + 角色 + 球衣颜色）；描述指代式见 [L1098-L1112](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L1098-L1112)（`"Locate the top right brown cake, ..."`，中英双版并列）。点定位还有一种 XML 输出格式：[L1162-L1180](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L1162-L1180)——`"point to the rolling pin ... output its coordinates in XML format <points x y>object</points>"`，兼容 Qwen2.5-VL 时代的老格式。

**（c）已知边界（官方自曝）。** [2d_grounding.ipynb:L1364-L1373](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L1364-L1373) 列出两条限制：单类别实例数显著超过 40～50 或物体密集堆叠时模型可能陷入无限生成循环；相邻密集物体偶尔被合并成一个框，可通过细化提示缓解。做工程选型时必须把这两条写进风险清单（推理侧要配 `max_new_tokens` 上限与循环检测兜底）。

**（d）万物识别的极简提示。** [omni_recognition.ipynb:L222-L230](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/omni_recognition.ipynb#L222-L230) 识别名人只要一句 `"Who is this"`；识别+定位组合见 [L298-L312](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/omni_recognition.ipynb#L298-L312)——`"Identify food in the image and return their bounding box and Chinese and English name in JSON format."`，以及 [L417-L430](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/omni_recognition.ipynb#L417-L430) 的动漫角色双语命名。「中文和 English name」这种双语要求可以直接写进提示。

**（e）3D 定位协议。** [3d_grounding.ipynb:L205-L223](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L205-L223) 定义 3D 框为 9 元组 `[x_center, y_center, z_center, x_size, y_size, z_size, roll, pitch, yaw]`——中心与尺寸以米计、姿态角以弧度计，全部在**相机坐标系**下。因此 3D 定位强依赖相机内参（fx、fy、cx、cy）；没有真实内参时 [L272-L322](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L272-L322) 提供 `generate_camera_params` 按 60° 视场角生成伪参数用于演示。提示词示例（类别式，见 [L323 起的用例区](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L323-L380)）：`"Find all cars in this image. ... The output format required is JSON: [{\"bbox_3d\":[x_center, ...],\"label\":\"category\"}]."`——与 2D 完全同构，只是键名换成 `bbox_3d`、schema 换成 9 元组。可视化时 8 个角点经 roll/pitch/yaw 旋转后按针孔模型投影回像素平面，连 12 条棱。

**（f）空间关系推理。** spatial_understanding 本不做坐标输出，用选择题与开放问题考察关系判断（[L267](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/spatial_understanding.ipynb#L267)：`"Which object, in relation to your current position, holds the farthest placement..."` 带 A/B/C/D 选项）、可供性（[L405](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/spatial_understanding.ipynb#L405)：`"Can the speaker fit behind the guitar?"`）、动作规划（[L473](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/spatial_understanding.ipynb#L473)：`"What color arrow should the robot follow to move the apple in between the green can and the orange? Choices: ..."`）。**选择题格式是压稳空间推理输出的实用技巧。**

**（g）视频三种输入形态与两条定位提示。** 推理函数 [video_understanding.ipynb:L170-L245](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L170-L245) 的签名自带全部预算旋钮：`total_pixels=20480*32*32, min_pixels=64*32*32, max_frames=2048, sample_fps=2`（注意其 docstring 写 min 默认 `16*32*32`，与签名 `64*32*32` 不一致——**以签名为准**，这也是读 cookbook 要留心的典型疏漏）。视频元素直接写进 messages；帧序列则用 decord 均匀采样并缓存（[L105-L167](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L105-L167) 的 `get_video_frames`/`create_image_grid`）。两条标志性提示：

- 事件时间定位（[L437-L450](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L437-L450)）：`"Localize a series of activity events in the video, output the start and end timestamp for each event, and describe each event with sentences. Provide the result in json format with 'mm:ss.ff' format for time depiction."` 该单元格保存的真实 API 返回显示模型输出了 `{"00:23.00 - 00:48.00": "trim the fat off a piece of beef.", ...}` 的事件表——一次调用消耗 `input_tokens 64572`，其中 `video_tokens 64514`。**视频 token 是输入大头，务必用 total_pixels 管住预算。**
- 时空联合定位（[L585-L690](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L585-L690)）：输入是**交错的「时间戳文本 + 帧图像」对**——`{"type": "text", "text": "<0.0 seconds>"}` 与 `image_url` 交替排列 8 对，再接 VidSTG 查询：`"Given the query \"...\", for each frame, detect and localize the visual content ... Output Format: [{\"time\": 1.0, \"bbox_2d\": [x_min, y_min, x_max, y_max], \"label\": \"\"}, ...]."`。这是「文本-时间戳对齐」架构更新（u1-l1）最直白的应用形态：时间戳以普通文本进 prompt，模型按帧输出 `time` + `bbox_2d`。下游解析在 [L715-L800](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L715-L800)：按结果里的 `time` 字符串去匹配 `"<x seconds>"` 文本找到对应帧，bbox 除以 1000 还原画框。

#### 4.3.4 代码实践（构造交错时间戳输入，无 GPU 也能做）

1. **实践目标**：亲手构造 VidSTG 式「时间戳-图像」交错 messages，看清它渲染成什么样。
2. **操作步骤**（以下为**示例代码**，仿照 [video_understanding.ipynb:L585-L690](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L585-L690) 编写）：

   ```python
   from transformers import AutoProcessor
   processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")

   content = []
   for t in range(3):                       # 3 帧，每秒 1 帧
       content.append({"type": "text", "text": f"<{t}.0 seconds>"})
       content.append({"type": "image", "image": f"file:///tmp/frame_{t}.jpg"})
   content.append({"type": "text", "text":
       'Given the query "a person is drinking", for each frame, detect and localize '
       'the visual content in JSON format. Output Format: '
       '[{"time": 1.0, "bbox_2d": [x_min, y_min, x_max, y_max], "label": ""}, ...].'})

   text = processor.apply_chat_template(
       [{"role": "user", "content": content}], tokenize=False, add_generation_prompt=True)
   print(text)
   ```

   只需准备 3 张任意 jpg，无需加载模型权重（processor 很小）。
3. **需要观察的现象**：渲染出的提示串里，每个 `<t.0 seconds>` 都是普通文本，其后紧跟 `<|vision_start|><|image_pad|><|vision_end|>` 视觉块；时间信息完全由文本携带，而非隐藏在位置编码里。
4. **预期结果**：打印出的字符串呈现「文本时间戳 → 视觉块 → 文本时间戳 → 视觉块 → 查询」的交错排布。这与 u5-l4 讲的「Qwen3-VL 每帧一个独立视觉块、t_index 恒 0、时间走文本时间戳」完全互证。processor 下载需联网，具体渲染串**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`bbox_2d: [120, 300, 880, 650]` 在 1920×1080 图上是什么区域？
**答案**：相对 0～1000 制。x：120/1000×1920=230 到 880/1000×1920≈1690；y：300/1000×1080=324 到 650/1000×1080=702。即图中偏右的一片矩形区域。

**练习 2**：货架盘点要用 2d_grounding 一次框出 200 个同类商品，直接照抄类别清单式提示安全吗？
**答案**：不安全。[2d_grounding.ipynb:L1364-L1373](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L1364-L1373) 明示单类别超过 40～50 个实例可能触发无限生成；应分块拍摄/切块推理，并设置 `max_new_tokens` 上限。

**练习 3**：为什么 3d_grounding 没有相机内参时宁可生成 fov=60° 的伪参数，也不直接输出 2D 框凑合？
**答案**：因为 `bbox_3d` 是相机坐标系下的物理量（米/弧度），可视化时必须经内参做针孔投影才能落回像素（[3d_grounding.ipynb:L272-L322](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L272-L322)）；伪参数只影响投影显示的准确性，任务协议仍是 3D。

---

### 4.4 Agent 与编码类

#### 4.4.1 概念说明

这一族的输出不再是文本/坐标，而是**可执行的动作**：

- **mobile_agent / computer_use**：GUI Agent。输入屏幕截图 + 指令，输出结构化工具调用（点哪个坐标、输什么字、滚多少）。工具协议由 `cookbooks/utils/agent_function_call.py` 中的 `MobileUse`（[L7](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L7)）与 `ComputerUse`（[L147](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L147)）两个类定义——它们是下一讲 u7-l2 的主角，本讲只看调用侧。
- **think_with_images**：用 qwen-agent 框架给模型挂上 `image_zoom_in_tool`（放大看细节）与 `image_search`（搜图验证）两个内置工具，让模型在「看→查→想」循环里推理。
- **mmcode**：图生代码——截图转 HTML、图表转 matplotlib、以及需要看图才能做的编程题（MMCode 基准）。

本族提示词模式的两个新特征：**系统提示开始承担结构性角色**（定义循环流程或专家人格），以及**输出是带 schema 的工具调用 JSON 或必须可解析的代码块**。

#### 4.4.2 核心流程

**GUI 工具调用闭环（computer_use 本为例）：**

```text
定义工具实例 ComputerUse(cfg={"display_width_px": 1000, "display_height_px": 1000})
→ NousFnCallPrompt().preprocess_fncall_messages(...)   # 把工具 schema 渲染进 system 消息
→ user 消息 = 截图(image_url) + 自然语言指令
→ 模型输出 <tool_call>\n{"name": ..., "arguments": {"action": ..., "coordinate": [x, y]}}\n</tool_call>
→ 字符串切分 + json.loads 解析出动作
→ coordinate 为 0～1000 相对坐标，÷1000 × resized_width 还原成屏幕像素 → 画点/执行
```

**think_with_images 的推理循环：** 系统提示规定「先仔细看 → 用工具查 → 复查发现 → 循环直到完成 → 综合作答」，agent.run 驱动多轮工具调用。

**mmcode 的代码闭环：** 提示要求把代码放进 ` ```python ` 块 → `extract_last_code_block` 正则取最后一个代码块 → （可选）执行/评测。

#### 4.4.3 源码精读

**（a）computer_use 的调用侧。** [computer_use.ipynb:L113-L119](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L113-L119) 同时导入 qwen-agent 的 `NousFnCallPrompt` 与本仓库的 `ComputerUse` 工具类；[L125-L215](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L125-L215) 的 `perform_gui_grounding_with_api` 完整实现闭环：L157 实例化工具、L162 用 `preprocess_fncall_messages` 把工具 schema 注入 system 消息（这才是真正的「系统提示」——由工具协议自动生成，而非手写）、L207 把返回的相对坐标 `coordinate[0]/1000*resized_width` 换算成像素画点。本地版函数在 [L376 起](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L376-L455)，其 `smart_resize` 用 `factor=patch_size*merge_size` 动态读取 processor 参数，比 API 版硬编码 32 更稳健。指令示例：`user_query = 'open the third issue'`。

**（b）mobile_agent。** 该笔记本（[mobile_agent.ipynb:L1](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/mobile_agent.ipynb#L1)，单行压缩 JSON）从同一工具文件导入 `MobileUse`，指令形如 `instruction = "Search for Musk in X and go to his homepage to open the fi..."`，并把任务进度拼进 user_query（`"The user query: {instruction}.\nTask progress (You ha..."`）——**把历史进度显式写进提示**是多步 Agent 维持状态的手法。中英文 App 各有一组示例。

**（c）think_with_images 的结构化系统提示。** [think_with_images.ipynb:L89-L99](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/think_with_images.ipynb#L89-L99) 的 `analysis_prompt` 用 markdown 列表规定四步循环（First, look closely → Next, find information → Then, review the findings → 循环 → 综合作答），这是 cookbook 里少见的重系统提示；[L101-L110](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/think_with_images.ipynb#L101-L110) 挂上 `tools = ['image_zoom_in_tool']` 创建 `Assistant`。三处细节值得抄：

- 其 `generate_cfg`（top_p 0.8、top_k 20、temperature 0.7、presence_penalty 1.5）与 [README.md:L1143-L1153](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1143-L1153) 的 Instruct 官方采样超参逐项一致——cookbook 连超参都是官方口径；
- [L43-L47](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/think_with_images.ipynb#L43-L47) 注明 `image_zoom_in_tool` 的 `bbox_2d` 同样是 0～1000 相对坐标——坐标协议全族统一；
- 第二个示例（[L307-L310](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/think_with_images.ipynb#L307-L310)）把 `image_zoom_in_tool` 与 `image_search` 组成多工具列表，演示工具组合。

**（d）mmcode 的三个用例。** 图生 HTML：[mmcode.ipynb:L432-L478](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/mmcode.ipynb#L432-L478) 的 `sketch_to_html`，提示为 `"Analyze this screenshot and convert it to clean, functional and modern HTML code."`（L451），temperature=0.8 鼓励生成多样代码，随后用 playwright 给生成页面截图形成**视觉回环验证**。图生图表代码：[L651-L700](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/mmcode.ipynb#L651-L700) 的 `chart_to_matplotlib`，提示 `"Convert this chart image to Python matplotlib code that can reproduce the chart."`（L663），执行前设 `ALLOW_DANGEROUS_CODE` 安全开关。多模态编程挑战：[L1072-L1160](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/mmcode.ipynb#L1072-L1160) 的 `solve_mmcode_problem` 使用专家人格系统提示（L1122：`"You are a professional programming contester trying to solve algorithmic problems. The problems come with a description and some images, and you should write a Python solution."`）、temperature=0.1 压稳输出，并把题目文本按 `![image](...)` 标记切开、与图片**交错**排列成消息（与 video 本的时间戳交错同构）。笔记本末尾记录 235B 模型在 MMCode 测试集准确率约 37.6%。

#### 4.4.4 代码实践（解析一次工具调用输出）

1. **实践目标**：写出 GUI Agent 输出的解析代码，理解 `<tool_call>` 协议与坐标换算。
2. **操作步骤**（**示例代码**，仿照 [computer_use.ipynb:L205-L210](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L205-L210)）：

   ```python
   import json

   output_text = ("Some reasoning...\n<tool_call>\n"
                  '{"name": "computer_use", "arguments": '
                  '{"action": "click", "coordinate": [420, 310]}}'
                  "\n</tool_call>")

   action = json.loads(output_text.split('<tool_call>\n')[1].split('\n</tool_call>')[0])
   x_rel, y_rel = action['arguments']['coordinate']
   resized_width, resized_height = 1000, 1000      # smart_resize 后的尺寸
   x_abs = x_rel / 1000 * resized_width
   y_abs = y_rel / 1000 * resized_height
   print(action['name'], action['arguments']['action'], (x_abs, y_abs))
   ```

   纯本地运行，无需模型。
3. **需要观察的现象**：解析出的 `action` 是字典；相对坐标 (420, 310) 在 1000×1000 的显示坐标系里仍是 (420.0, 310.0)——只有当 smart_resize 后尺寸 ≠ 1000 时换算才产生实际缩放。
4. **预期结果**：打印 `computer_use click (420.0, 310.0)`。可把 `resized_width/height` 改成 1280×720 再观察换算差异。

#### 4.4.5 小练习与答案

**练习 1**：GUI Agent 的「系统提示」主要来自哪里？
**答案**：不是手写的，而是 `NousFnCallPrompt().preprocess_fncall_messages` 依据工具的 `function` schema 自动渲染进 system 消息（[computer_use.ipynb:L162](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L162)）。

**练习 2**：mmcode 里为什么图生 HTML 用 temperature=0.8、解编程题却用 0.1？
**答案**：前者是创造性生成，多样性能带来更好的页面设计；后者要求确定性推理与严格格式，低温压稳输出（[mmcode.ipynb:L451 与 L1072 起的单元格](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/mmcode.ipynb#L1072-L1160)）。

**练习 3**：think_with_images 的采样参数能否直接照搬到生产？
**答案**：参数本身是 README 官方 Instruct 口径（top_p 0.8 / top_k 20 / temperature 0.7 / presence_penalty 1.5），可照搬；但要注意它面向 Instruct 模型，Thinking 模型的官方口径不同（presence_penalty=0.0 等），切换模型必须换参数（承接 u6-l3）。

---

## 5. 综合实践

**任务**：跑通 `ocr.ipynb` 与 `video_understanding.ipynb` 的核心单元格，产出一份「系统提示 vs 用户提示」对照表，并完成一次能力选型。这是本讲的收官实践，把四个模块的知识串起来。

**步骤一：跑通 ocr.ipynb 核心（对应 4.2）**

1. 在 Colab（README 表格徽章）或本地打开 `cookbooks/ocr.ipynb`；
2. 执行 Setup、工具函数、模型加载三个单元格（本地建议把 checkpoint 换成 `Qwen/Qwen3-VL-2B-Instruct`）；
3. 执行「1. Full-page OCR」与「3. Text Spotting」两个用例，记录每次的 prompt 与返回。

**步骤二：跑通 video_understanding.ipynb 核心（对应 4.3）**

1. 执行 Setup（把 `model_path = "***"` 换成 `Qwen/Qwen3-VL-2B-Instruct` 或走 API 路径）与 `get_video_frames`、`inference` 单元格；
2. 执行「1. Using Video URL - Local Inference」；
3. 执行 4.3.4 的交错时间戳构造脚本，观察渲染串。

**步骤三：产出提示对照表（交付物）**

按下面模板整理（已给出从源码读出的参考答案，跑通后用自己的运行结果补全最后一列）：

| 维度 | ocr.ipynb | video_understanding.ipynb |
| --- | --- | --- |
| 系统提示 | 被注释掉，默认 `"You are a helpful assistant."`（[L168-L175](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L168-L175)） | 无显式系统提示，user 单轮 |
| 用户提示模板（转写） | `"Read all the text in the image."`（L339） | `"请用表格总结一下视频中的商品特点"`（L381） |
| 用户提示模板（定位） | `"Spotting all the text ... output in JSON format as [{'bbox_2d': ..., 'text_content': ...}]"`（L592） | 事件定位 `"Localize a series of activity events ... 'mm:ss.ff' format"`（L439）；时空定位 `"... Output Format: [{'time': ..., 'bbox_2d': ..., 'label': ...}]"`（L681） |
| 输出格式协议 | `bbox_2d` + `text_content` JSON | `time` + `bbox_2d` JSON / `mm:ss.ff` 事件表 |
| 坐标口径 | 0～999（本本画框函数除以 999） | 0～1000（画框除以 1000） |
| 视觉预算 | `512*32*32 ~ 2048*32*32`（ spotting 用例） | `total_pixels=20480*32*32, min_pixels=64*32*32, max_frames=2048, sample_fps=2` |
| 输入构造要点 | 单图 base64 / 本地 PIL | `process_vision_info(image_patch_size=16, return_video_metadata=True)` + `do_resize=False` |
| 你的运行结果 | （待填写） | （待填写） |

**步骤四：能力选型演练**

给定三个需求，写出「参考哪本 cookbook + 抄哪条提示 + 用什么输出协议」：

1. 超市小票归档（要金额、日期字段）→ 参考 ocr 的 KIE 用例（[L759-L775](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb#L759-L775)），自定义字段 schema，JSON 输出；
2. 仓库监控里定位搬运的叉车并输出 3D 框 → 参考 3d_grounding（[L205-L223](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L205-L223)），需准备相机内参；
3. 从产品演示视频自动切片（按事件起止时间）→ 参考 video_understanding 的事件定位提示（[L437-L450](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/video_understanding.ipynb#L437-L450)），`mm:ss.ff` 事件表输出。

**验收标准**：对照表 8 行填满、选型三条各含「cookbook + 提示 + 协议」三要素。若本地无 GPU 且无 API Key，步骤一/二可用「阅读单元格 + 手工构造 messages」替代（如 4.3.4 的脚本），并在表中如实标注「待本地验证」。

## 6. 本讲小结

- cookbooks 目录有 13 个笔记本（README 表收录 12，german_document_ocr 未列入），可归并为**识别 / OCR 与文档 / 定位与空间（含视频）/ Agent 与编码**四族，同族共享代码骨架与提示词模式。
- 所有提示词服从**三段式**：任务动词 + 作用范围 + 输出格式；JSON schema 直接内嵌提示，键名（`bbox_2d`/`point_2d`/`bbox_3d`/`text_content`/`label`/`time`）即下游解析协议。
- 坐标统一走 **0～1000 相对制**（ocr 本的画框函数用 999，属个别实现差异）；像素预算统一 32 口径且按任务定档：OCR 高预算、长文档每页低预算、视频用 `total_pixels` 总预算。
- 特殊模式：`qwenvl html`/`qwenvl markdown` 两个魔法提示切换专属文档解析格式；交错「时间戳文本 + 帧」输入支撑秒级时空定位；GUI Agent 的系统提示由工具 schema 自动生成，输出为 `<tool_call>` JSON。
- 系统提示多数留空，重任务（Agent 循环、竞赛编程）才写结构性系统提示，且采样超参与 README 官方 Instruct 口径一致。
- cookbook 存在代际混用（ocr 本地演示仍加载 Qwen2.5-VL-7B、video 本模型路径为占位符），复现时需自行替换 checkpoint。

## 7. 下一步学习建议

本讲从「用」的角度看了 Agent 类笔记本的调用侧；下一讲 **u7-l2《GUI Agent 与工具调用：mobile_use 工具实现》** 将深入 [cookbooks/utils/agent_function_call.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L7)，精读 `MobileUse`/`ComputerUse` 如何用 `description` 文案、`action` 枚举与 `coordinate` 参数 schema 引导模型正确生成工具调用，并动手实现一个自定义工具。

若你想先横向扩展，建议按这条线读源码：2d/3d grounding 的画框函数（坐标还原）→ mmcode 的 `extract_last_code_block`（代码块解析）→ u7-l3《2D/3D Grounding 实践与坐标格式体系》（会把本讲的坐标约定接到训练数据制作：`qwen-vl-finetune/tools/process_bbox.ipynb`）。
