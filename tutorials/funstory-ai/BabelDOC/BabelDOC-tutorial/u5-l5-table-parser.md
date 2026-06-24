# 表格解析：TableParser 与 OCR

## 1. 本讲目标

本讲讲解 midend 流水线中负责「表格」的阶段 `TableParser`（stage_name = `Parse Table`）。学完本讲你应该能够：

1. 说清 `TableParser` 在 `TRANSLATE_STAGES` 中的位置、它的输入输出，以及它与上一阶段 `LayoutParser`（u5-l2）的衔接关系。
2. 读懂 `TableParser.process` 的核心流程：筛选含表格的页面 → 调用表格模型 → 把检测结果从图像坐标映射回 IL 坐标 → 写回 `page.page_layout`。
3. 理解 `RapidOCRModel` / `YoloResult` / `YoloBox` 这套「检测模型接口」的契约，以及 `--translate-table-text` 这条 CLI 链路是如何把模型装配进 `TranslationConfig` 的。
4. **最重要的现实认知**：表格文本翻译是一项**已退役（retired）的实验性功能**。从 v0.6.0 起 `table_model` 被强制置 `None`，`Parse Table` 阶段在运行期被剔除，`TableParser.process` 实际上从不执行。本讲会带你把这条「代码还在、功能已停」的弃用链路彻底走通。

> 提示：本讲的特殊价值正在于第 4 点——它是一个「阶段如何被条件性启停」「功能如何优雅退役但保留脚手架」的鲜活范例。理解它，你就能看懂 BabelDOC 流水线里所有「条件阶段」的设计套路。

## 2. 前置知识

在进入本讲前，请确认你已建立以下认知（由前置讲义承接）：

- **三段式与 TRANSLATE_STAGES**（u2-l1）：midend 由一串 stage 顺序加工同一份 IL（`docs`），`TRANSLATE_STAGES` 是「节目单」，元组第二列是相对耗时权重，顺序即执行顺序。
- **版面分析 LayoutParser**（u5-l2）：`DocLayout-YOLO`（ONNX）识别页面区域，结果写入 `page.page_layout`，每个区域是一个 `PageLayout(box, id, conf, class_name)`；DocLayout-YOLO 的类别名（含 `table`、`title`、`figure` 等）来自 ONNX 元数据。本讲的 `TableParser` 正是消费 `class_name == "table"` 的那些区域。
- **坐标系的两次翻转**（u5-l2）：模型在「图像空间」（左上原点、y 向下）推理；IL/PDF 在「页面空间」（左下原点、y 向上）。在 72 DPI 下像素数 = 点数，故只需做 y 翻转 \(\,\text{pdf\_y}=h-\text{img\_y}\,\)，无需缩放。
- **TranslationConfig 是装配盘**（u1-l4）：所有 CLI/TOML 参数最终汇聚进 `TranslationConfig`，新参数追加在 `__init__` 末尾以保兼容，已废弃字段（如 `table_model`）会被强制清理并告警。
- **IL 数据模型**（u3-l1）：`Page` 下挂着 `page_layout`、`pdf_character`、`pdf_paragraph`、`pdf_rectangle` 等并列集合；`Box(x, y, x2, y2)` 是被大量复用的矩形支撑类型。

如果你对「stage 如何被剔除」还不熟，先回顾 u2-l1 中关于 `get_translation_stage` 的结论。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
| --- | --- |
| [babeldoc/format/pdf/document_il/midend/table_parser.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/table_parser.py) | `TableParser` 阶段本体：筛选表格页、调用模型、坐标映射、写回 `page_layout`、debug 可视化。 |
| [babeldoc/docvision/table_detection/rapidocr.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/table_detection/rapidocr.py) | `RapidOCRModel`：原 RapidOCR 表格文本检测器的封装，**现已退化为 no-op 兼容垫片**。 |
| [babeldoc/docvision/base_doclayout.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/base_doclayout.py) | `YoloResult`/`YoloBox` 检测结果容器、`DocLayoutModel` 抽象基类（定义 `handle_document` 接口契约）。 |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | `TRANSLATE_STAGES` 节目单、`get_translation_stage` 条件剔除、`_do_translate_single` 中对 `TableParser` 的调用与守卫。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | `TranslationConfig.__init__`：把传入的 `table_model` 强制置 `None` 并告警。 |
| [babeldoc/main.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | `--translate-table-text` CLI 参数定义，以及据此 `import RapidOCRModel` 并实例化的装配代码。 |
| [babeldoc/format/pdf/document_il/utils/mupdf_helper.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/mupdf_helper.py) | `get_no_rotation_img`：忽略页面旋转渲染像素图，供坐标翻转取 `h`、`w`。 |
| [docs/release-notes/v0.6.0.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/release-notes/v0.6.0.md) | 官方对「表格文本检测退役」的兼容性说明。 |
| [examples/table.xml](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/table.xml) | 一个 **DPML 示意格式**的表格样例（非 BabelDOC 实际输入），用于直观理解表格结构。 |

## 4. 核心概念与源码讲解

### 4.1 表格区域识别：从 LayoutParser 继承的 `class_name`

#### 4.1.1 概念说明

`TableParser` 不负责「发现页面里有没有表格」——那是上一阶段 `LayoutParser`（u5-l2）的活。`LayoutParser` 用 DocLayout-YOLO 把整页切成若干区域，每块打上一个类别名（`title`、`plain text`、`figure`、`table`……）。`TableParser` 接手后，只关心其中 `class_name == "table"` 的那几块区域，准备在这些区域**内部**进一步做细粒度检测（例如检测单元格 / 表格内的文字位置）。

这是一种典型的「**两趟版面（two-pass layout）**」设计：

- 第一趟（LayoutParser）：粗粒度，整页 → 区域（含 `table` 这一类）。
- 第二趟（TableParser）：细粒度，仅在 `table` 区域内 → 单元格 / 表格文字子区域。

两趟共用同一份 `page.page_layout` 列表和同一种 `PageLayout` 记录格式，第二趟只是往列表里 `extend` 更多框。这样下游阶段（`ParagraphFinder` 等）无需区分框来自哪一趟，统一按 `page_layout` 处理即可。

#### 4.1.2 核心流程

`TableParser.process` 开头先用一个双重循环筛出「含表格的页面」：

```text
for page in docs.page:
    for layout in page.page_layout:
        if layout.class_name == "table":
            have_table_pages[page.page_number] = page
```

要点：

1. 遍历每一页的全部 `page_layout` 区域。
2. 只要某页有**任意一块** `class_name == "table"`，就把整页收进 `have_table_pages`（按 `page_number` 去重）。
3. 没有表格的页直接跳过——这是 `TableParser` 相对廉价（权重仅 1.0）的原因之一。

#### 4.1.3 源码精读

`TableParser` 类声明与阶段名定义在：

[document_il/midend/table_parser.py:16-21](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/table_parser.py#L16-L21) —— `stage_name = "Parse Table"`，构造时把 `translation_config.table_model` 存为 `self.model`。注意它**直接信任** config 里的 `table_model`，自身不做任何「是否退役」的判断。

`TRANSLATE_STAGES` 中该阶段的位置与权重：

[high_level.py:60-75](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75) —— 节目单里 `Parse Table` 排在 `Parse Page Layout`（LayoutParser）之后、`Parse Paragraphs`（ParagraphFinder）之前，权重 `1.0`（远小于版面 14.03、翻译 46.96）。

筛选含表格页面的循环：

[document_il/midend/table_parser.py:119-123](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/table_parser.py#L119-L123) —— 用 `class_name == "table"` 作为唯一判据，体现「两趟版面」中第二趟对第一趟结果的依赖。

`PageLayout` 的字段结构（`TableParser` 产出的就是它）：

[document_il/il_version_1.py:314-341](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L314-L341) —— 含 `box`、`id`、`conf`、`class_name` 四个必填字段，与 LayoutParser 产出的区域**完全同构**。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「`table` 这个类别名确实来自 LayoutParser，而非 TableParser 自己定义」。
2. **操作步骤**：
   - 在 [document_il/midend/layout_parser.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py) 中找到写入 `class_name` 的位置，确认它取自 ONNX 模型的 `names` 元数据。
   - 在 [document_il/midend/table_parser.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/table_parser.py) 中确认 `class_name == "table"` 是字面量比较，没有任何额外定义。
3. **需要观察的现象**：两个阶段对 `class_name` 的处理是「一个写、一个读」，没有任何重复定义。
4. **预期结果**：你能用一句话说清「`table` 类别是 DocLayout-YOLO 模型给出的，TableParser 只是消费者」。
5. **待本地验证**：DocLayout-YOLO（DocStructBench）的完整类别清单由模型权重内置，可用 `OnnxModel.from_pretrained()` 加载后打印 `model.names` 核对（参见 u5-l2）。

#### 4.1.5 小练习与答案

**练习 1**：如果一页里同时有一个 `table` 区域和一个 `figure` 区域，`have_table_pages` 会收录这一页吗？为什么？
**参考答案**：会。判定条件是「存在任意一块 `class_name == "table"`」，与同页是否有其它类别无关；收录的是整页 `Page` 对象，后续会对整页做表格模型推理。

**练习 2**：为什么 `have_table_pages` 用 `dict`（按 `page_number` 索引）而不是 `list`？
**参考答案**：以 `page_number` 为键天然去重——同一页可能有多块 `table` 区域，但只需对该页推理一次；同时 `.values()` 给下游 `handle_document` 提供确定的遍历顺序。

---

### 4.2 单元格结构检测与坐标映射

#### 4.2.1 概念说明

筛出含表格的页面后，`TableParser` 要在这些页面里**进一步检测细粒度结构**（在原始设计里是「表格内的文字/单元格位置」）。它的做法是把页面交给一个「表格检测模型」，模型在**图像空间**输出一组检测框（每个框带类别、置信度、坐标），`TableParser` 再把这些框**从图像空间映射回 IL/PDF 空间**，包装成新的 `PageLayout` 追加进 `page.page_layout`。

坐标映射的数学与 LayoutParser（u5-l2）完全一致：72 DPI 下像素 = 点，只需 y 翻转；再外扩 1 像素容差并用 `np.clip` 钳到合法范围。

#### 4.2.2 核心流程

`process` 的主干（去掉 debug 与守卫后）如下：

```text
with progress_monitor.stage_start("Parse Table", len(have_table_pages)) as progress:
    for page, layouts in model.handle_document(pages, mupdf_doc, config, save_debug_image):
        page_layouts = []
        for box in layouts.boxes:                      # 遍历模型输出的每个检测框
            x0, y0, x1, y1 = box.xyxy                  # 图像空间坐标
            h, w = pixmap.height, pixmap.width         # 72DPI 像素图尺寸
            # 图像空间 -> IL 空间（y 翻转 + ±1 容差 + clip）
            x0,y0,x1,y1 = clip(x0-1), clip(h-y1-1), clip(x1+1), clip(h-y0+1)
            page_layouts.append(PageLayout(id, Box(x0,y0,x1,y1), conf, class_name))
        page.page_layout.extend(page_layouts)          # 追加进同一份版面列表
        progress.advance(1)
return docs
```

y 翻转的几何关系（图像空间 y 向下，IL 空间 y 向上）：

\[
\text{pdf\_y}_\text{bottom}=h-\text{img\_y}_1,\qquad
\text{pdf\_y}_\text{top}=h-\text{img\_y}_0
\]

其中 \(\text{img\_y}_0\) 是图像里框的上沿、\(\text{img\_y}_1\) 是下沿（\(\text{img\_y}_0<\text{img\_y}_1\)）。映射后，图像的上沿变成 IL 里较大的 `y2`，图像的下沿变成 IL 里较小的 `y`，正好对应 `Box(x, y, x2, y2)`「左下角 `(x,y)`、右上角 `(x2,y2)`」的约定。±1 的外扩是为了给检测框一点边缘余量，`np.clip(..., 0, w-1)` 防止越界。

#### 4.2.3 源码精读

完整的 `process` 方法（含进度上报与坐标映射）：

[document_il/midend/table_parser.py:116-166](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/table_parser.py#L116-L166) —— 进度阶段用 `stage_start(stage_name, total)` 上下文管理器包裹，逐页 `progress.advance(1)`。

坐标映射与 `PageLayout` 构造的关键四行：

[document_il/midend/table_parser.py:139-160](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/table_parser.py#L139-L160) —— 先 `get_no_rotation_img` 取像素图（忽略页面旋转），再用 `h - y - 1` / `h - y + 1` 做 y 翻转与容差；`Box(...)` 四个位置参数即 `(x, y, x2, y2)`；`id` 用本页局部计数器 `len(page_layouts)+1`（从 1 开始）；`conf`、`class_name` 直接取自模型输出的 `box.conf` 与 `layouts.names[box.cls]`。

`get_no_rotation_img` 的实现：

[utils/mupdf_helper.py:7-13](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/mupdf_helper.py#L7-L13) —— 临时把页面旋转置 0、渲染 72 DPI 像素图、再恢复原旋转，保证拿到的 `h/w` 与模型看到的「未旋转」图像一致。

`stage_start` 进度上下文：

[progress_monitor.py:110](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L110) —— 与其它 midend 阶段共用同一套进度协议（详见 u2-l3）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证 `TableParser` 与 `LayoutParser` 用的是**同一套坐标映射数学**。
2. **操作步骤**：
   - 打开 [document_il/midend/layout_parser.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py)，找到它做 y 翻转的代码段。
   - 与 [table_parser.py:143-148](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/table_parser.py#L143-L148) 逐项对照。
3. **需要观察的现象**：两处都是 `h - y` 形式翻转 + ±1 容差 + `np.clip`；LayoutParser 因可能用 150 DPI（RPC 版）多了一步 `scale`，而 TableParser 这里固定 72 DPI、无需 scale。
4. **预期结果**：你能指出两者坐标映射的「相同内核」与「DPI 差异带来的 scale 步骤有无」这一处不同。
5. **待本地验证**：若本地能跑 LayoutParser，可对比同一页 `table` 区域在两阶段产出的 `box` 是否近似重合。

#### 4.2.5 小练习与答案

**练习 1**：`id=len(page_layouts)+1` 用的是「本页本次新增」的局部计数，而不是全页唯一的 id。这会与 LayoutParser 已写入的 `page_layout` 里的 `id` 冲突吗？
**参考答案**：可能产生重复的 `id` 值（因为计数从 1 重新开始）。这暗示 `id` 在当前实现里并非严格的唯一键，下游若依赖 `id` 唯一性需注意；这也是该阶段「实验性」的一个侧面佐证。

**练习 2**：为什么用 `page.page_layout.extend(page_layouts)` 而不是新建一个专门的「表格单元格」集合？
**参考答案**：为了让下游（ParagraphFinder、Typesetting 等）无需感知「框来自哪一趟」——所有版面框都在同一个 `page_layout` 列表里，按 `class_name` 区分语义即可。这是「两趟版面」设计的关键简化。

---

### 4.3 RapidOCR 表格模型

#### 4.3.1 概念说明

`TableParser` 不自己跑深度学习推理，而是把工作委托给一个「表格检测模型」对象 `self.model`。这个对象需要满足一个接口契约：提供 `handle_document(pages, mupdf_doc, config, save_debug_image)` 方法，以生成器形式逐页 `yield (page, YoloResult)`。`YoloResult` 是「一页的检测结果」容器，内含若干 `YoloBox`（每个框 = 坐标 + 置信度 + 类别）。

历史上这个模型是 [RapidOCR](https://github.com/RapidAI/RapidOCR) 的表格检测分支，类别名为 `table_text`（即「表格内的文字」）。但在当前版本（v0.6.x），`RapidOCRModel` 已经被改写成一个 **no-op 兼容垫片**——它的所有方法都返回空结果，不再加载任何模型权重、不做任何真实检测。这是理解整条表格链路「已退役」的关键。

#### 4.3.2 核心流程

模型对象的接口契约（来自抽象基类 `DocLayoutModel`）：

```text
handle_document(pages, mupdf_doc, translate_config, save_debug_image)
    -> Generator[tuple[Page, YoloResult], None, None]
```

- 输入：待处理页面列表、已打开的 pymupdf 文档、翻译配置、debug 回调。
- 输出：逐页 yield 一个 `(page, YoloResult)` 元组。
- `YoloResult`：持有 `names`（类别索引→名称映射）与 `boxes`（`YoloBox` 列表，按 `conf` 降序）。
- `YoloBox`：`xyxy`（四个坐标）、`conf`（置信度）、`cls`（类别索引）。

当前 `RapidOCRModel` 的真实行为（no-op）：

```text
predict(...)        -> YoloResult(names={0:"table_text"}, boxes=[])   # 永远空
handle_document(...): for page in pages: yield (page, YoloResult(boxes=[]))
```

即：即便 `TableParser.process` 真的被调用，从 `RapidOCRModel` 拿到的也永远是「零个检测框」，`page_layouts` 恒为空，`page.page_layout.extend([])` 等于什么都没做。

#### 4.3.3 源码精读

检测结果容器 `YoloResult` / `YoloBox`：

[docvision/base_doclayout.py:12-37](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/base_doclayout.py#L12-L37) —— `YoloResult` 构造时会把 `boxes` 按 `conf` 降序排序；`YoloBox` 支持两种构造方式（原始 `data` 数组 或 显式 `xyxy/conf/cls`）。这两个类是 LayoutParser 与 TableParser **共用**的检测输出格式。

`DocLayoutModel` 抽象基类定义的 `handle_document` 契约：

[docvision/base_doclayout.py:40-68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/base_doclayout.py#L40-L68) —— 抽象 `stride` 属性与 `handle_document` 方法，任何「按页检测模型」都实现这套接口，可被 `TableParser`/`LayoutParser` 透明替换（这正是 u5-l2 里本地 ONNX 与 RPC 版面服务可互换的根基）。

`RapidOCRModel` 的 no-op 实现：

[docvision/table_detection/rapidocr.py:9-21](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/table_detection/rapidocr.py#L9-L21) —— 类文档串明确写着「Compatibility no-op for the retired RapidOCR table text detector」；`names = {0: "table_text"}`、`stride` 恒为 32、`predict` 永远返回空 `YoloResult`。

[docvision/table_detection/rapidocr.py:22-41](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/table_detection/rapidocr.py#L22-L41) —— `handle_document` 逐页 yield 空 `YoloResult`，仅在传了 `save_debug_image` 回调时写一张 1×1 的空白占位图。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：确认 `RapidOCRModel` 与 `OnnxModel`（LayoutParser 用的真实模型）实现的是**同一套接口**，因此可互换。
2. **操作步骤**：
   - 读 [docvision/table_detection/rapidocr.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/table_detection/rapidocr.py)，列出它实现的 `stride`、`predict`、`handle_document`。
   - 对照 [docvision/base_doclayout.py:40-68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/base_doclayout.py#L40-L68) 的抽象方法签名。
3. **需要观察的现象**：`RapidOCRModel` 没有继承 `DocLayoutModel`（不是正式子类），但「鸭子类型」上满足同一套方法签名。
4. **预期结果**：你能解释「为什么把一个真实的表格 ONNX 模型（只要实现 `handle_document`）塞进 `translation_config.table_model`，`TableParser` 就能即插即用」——这正是该设计为未来「重新激活」留的口子。
5. **待本地验证**：无（纯源码对照）。

#### 4.3.5 小练习与答案

**练习 1**：`RapidOCRModel.predict` 里有一行 `_ = image, imgsz, batch_size, kwargs`，这行的作用是什么？
**参考答案**：显式「消费」并丢弃这些未使用的参数，纯粹为了规避 linter 的「未使用参数」告警；同时也向读者明示「这些入参已不再影响任何行为」，是 no-op 实现的典型写法。

**练习 2**：既然 `RapidOCRModel` 已经是空操作，为什么仓库还保留这个类而不直接删掉？
**参考答案**：为了向后兼容——`main.py` 在 `--translate-table-text` 时仍会 `import RapidOCRModel` 并实例化它（见 4.4）。删掉类会破坏这条 import 与可能存在的下游脚本；保留 no-op 垫片让旧用法「不报错、但静默无效」，是更平滑的退役方式。

---

### 4.4 实验性表格文本翻译：CLI 链路与「优雅退役」

#### 4.4.1 概念说明

本模块是全讲的重点：把「`--translate-table-text` 这个开关」从 CLI 一路追到 `TableParser` 是否真的执行，你会看到一条**完整的弃用链路**。结论先行：

> 在 v0.6.x，无论你是否传 `--translate-table-text`，`TableParser.process` 都**不会被调用**，`Parse Table` 阶段都会被从流水线里剔除。

这条链路有四个环节，每一环都做了「拦截」：

1. **CLI 定义**：`--translate-table-text` 仍是合法参数（`store_true`，默认 False）。
2. **main 装配**：若开关为真，`import RapidOCRModel` 并实例化为 `table_model`，否则为 `None`，然后传入 `TranslationConfig`。
3. **Config 拦截**：`TranslationConfig.__init__` **无视**传入的 `table_model`，打一条 deprecation 警告，并强制 `self.table_model = None`。
4. **双重守卫**：`get_translation_stage` 因 `table_model` 为 `None` 把 `Parse Table` 从节目单剔除；`_do_translate_single` 又用 `if translation_config.table_model:` 守住，确保即便阶段没被剔除也不会调用 `process`。

这种「CLI 还在 → 中间装配还在 → 但在 Config 处被强制清零 → 运行期双重跳过」的设计，就是 BabelDOC 对实验性功能「保留入口、停用实现」的标准手法。

#### 4.4.2 核心流程

弃用链路的时序：

```text
用户: babeldoc --translate-table-text ...
  │
  ├─ main.py: args.translate_table_text == True
  │     └─ table_model = RapidOCRModel()            # no-op 对象
  │
  ├─ main.py: TranslationConfig(..., table_model=table_model, ...)
  │
  ├─ translation_config.__init__: if table_model is not None:
  │     └─ logger.warning("...deprecated and ignored...")
  │     └─ self.table_model = None                  # 强制清零！
  │
  ├─ get_translation_stage: if not translation_config.table_model:
  │     └─ should_remove.append("Parse Table")      # 从节目单剔除
  │
  └─ _do_translate_single: if translation_config.table_model:   # 恒为 False
        └─ TableParser(...).process(...)            # 永不执行
```

#### 4.4.3 源码精读

**环节 1 —— CLI 参数定义**：

[main.py:237-242](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L237-L242) —— `--translate-table-text`（`store_true`，默认 False，help 标注 `experimental`）。注意它仍出现在 `babeldoc --help` 里，是「入口尚存」的体现。

**环节 2 —— main 装配**：

[main.py:577-582](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L577-L582) —— 按开关懒加载 `RapidOCRModel`（注意是函数内 `import`，避免无关运行引入 RapidOCR 依赖），实例化后赋给局部变量 `table_model`。

[main.py:705](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L705) —— 把 `table_model=table_model` 作为关键字参数传入 `TranslationConfig` 构造。

**环节 3 —— Config 拦截（最关键）**：

[translation_config.py:326-331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L326-L331) —— `if table_model is not None:` 时打 warning（明确写「deprecated and ignored」「RapidOCR table-text detection has been retired」），随后**无条件** `self.table_model = None`。这正是 u1-l4 提到的「废弃字段会被强制清理」的实例。

**环节 4 —— 双重守卫**：

[high_level.py:286-287](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L286-L287) —— `get_translation_stage` 里 `if not translation_config.table_model: should_remove.append(TableParser.stage_name)`；由于 `table_model` 恒为 `None`，`Parse Table` 永远被剔除（进度条里看不到这一阶段）。

[high_level.py:963-970](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L963-L970) —— `_do_translate_single` 里 `if translation_config.table_model:` 才调用 `TableParser(...).process(...)` 并在 `--debug` 下写 `table_parser.json`；该 `if` 恒为假，故整段被跳过。

**官方退役说明**：

[docs/release-notes/v0.6.0.md:42-45](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/release-notes/v0.6.0.md#L42-L45) —— 明确记载「`table_model` 已废弃，传入会被忽略并告警；运行时不再加载 RapidOCR 表格文本检测资源」。

#### 4.4.4 代码实践（行为观察 + 源码追踪）

1. **实践目标**：亲眼确认「开了 `--translate-table-text` 也不会触发表格解析」，并能把弃用链路讲给别人听。
2. **操作步骤**：
   - 准备一个含表格的 PDF（仓库自带 [examples/ci/test.pdf](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/ci/test.pdf) 可用；注意 `examples/table.xml` 是 DPML 示意格式，**不是** BabelDOC 的输入，不能直接喂给 CLI）。
   - 运行（需 OpenAI 兼容服务）：
     ```bash
     babeldoc --openai --openai-api-key sk-xxx \
              --files examples/ci/test.pdf \
              --translate-table-text --debug
     ```
   - 在日志中搜索 `deprecated and ignored` 这条 warning。
   - 到工作目录（`--debug` 产出物所在目录）里找是否生成了 `table_parser.json` 与 `table-ocr-box-image/` 目录。
3. **需要观察的现象**：
   - 日志里出现 `TranslationConfig.table_model is deprecated and ignored; RapidOCR table-text detection has been retired.` 警告。
   - **没有** `table_parser.json` 文件，**没有** `table-ocr-box-image/` 目录。
   - 进度条里**看不到** `Parse Table` 阶段。
4. **预期结果**：三项现象共同证明「开关已被静默拦截、`TableParser` 未执行」。这恰好印证 4.4.1 的结论。
5. **若无法本地运行**（无 API key / 无网络）：改为纯源码追踪——依次打开 [main.py:577-582](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L577-L582)、[translation_config.py:326-331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L326-L331)、[high_level.py:286-287](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L286-L287)、[high_level.py:963-970](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L963-L970)，沿 4.4.2 的时序图复述一遍数据流，得出同样结论。

#### 4.4.5 小练习与答案

**练习 1**：如果未来要「重新激活」表格文本翻译，最少需要改哪几处？
**参考答案**：(1) 提供一个真实实现 `handle_document` 的表格检测模型（替换 no-op 的 `RapidOCRModel`）；(2) 取消 [translation_config.py:331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L331) 的 `self.table_model = None` 强制清零（改为尊重传入值）；只要 `table_model` 非 `None`，[high_level.py:286-287](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L286-L287) 与 [high_level.py:963](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L963) 的两道守卫就会自动放行——这正是该设计「保留脚手架」的价值。

**练习 2**：为什么 `main.py` 把 `from babeldoc.docvision.table_detection.rapidocr import RapidOCRModel` 写在 `if args.translate_table_text:` **里面**，而不是放在文件顶部？
**参考答案**：延迟导入（lazy import）。只有用户显式开启该开关时才 import，避免在普通翻译（绝大多数情况）里把 RapidOCR 及其重依赖拉进进程；同时也让「功能退役」对普通用户完全无感。

**练习 3**：`examples/table.xml` 能否用 `babeldoc --files examples/table.xml` 翻译？为什么？
**参考答案**：不能。它是 DPML 示意格式（`<wp:document>/<wp:page>/<wp:table>...`），仅用于直观展示表格结构，与 PDF 输入及 IL 的 XML 序列化格式都不同（详见 u3-l2 关于 `examples/*.xml` 的说明）。BabelDOC 的输入是 PDF。

## 5. 综合实践

**任务：绘制「`--translate-table-text` 的完整生命周期」一页纸时序图，并预测输出物。**

1. 画一条从用户命令到最终 PDF 的纵向时序线，标出下列节点及其所在文件/行号：
   - CLI 解析 `--translate-table-text`（[main.py:237-242](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L237-L242)）
   - main 实例化 `RapidOCRModel`（[main.py:577-582](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L577-L582)）
   - 传入 `TranslationConfig`（[main.py:705](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L705)）
   - Config 强制清零 + 告警（[translation_config.py:326-331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L326-L331)）
   - `get_translation_stage` 剔除 `Parse Table`（[high_level.py:286-287](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L286-L287)）
   - `_do_translate_single` 的守卫跳过（[high_level.py:963-970](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L963-L970)）
2. 在每个节点旁用一句话写出「该节点对 `table_model` 做了什么」。
3. 另起一栏，列出「如果该功能被重新激活，`TableParser.process` 会在含表格页里产生哪些 IL 变化」：新增带 `class_name`（如 `table_text`）的 `PageLayout` 区域、`--debug` 下生成 `table_parser.json` 与 `table-ocr-box-image/{page}.jpg`、绿色 `PdfRectangle` debug 叠加框。
4. 用一句话总结：这套设计在「保留入口」与「停用实现」之间是如何取得平衡的。

> 这个综合实践把本讲四个模块（区域识别、单元格检测、模型接口、CLI 弃用链路）串成一条线，做完你就真正掌握了 BabelDOC 对「实验性 / 条件性 stage」的工程处理范式。

## 6. 本讲小结

- `TableParser`（`Parse Table`，权重 1.0）排在 `LayoutParser` 之后、`ParagraphFinder` 之前，是「两趟版面」的第二趟：仅在 `class_name == "table"` 的页面里做细粒度检测。
- 它的 `process` 流程是：筛表格页 → 调 `model.handle_document` → 把检测框用「y 翻转 + ±1 容差 + clip」从图像空间映射回 IL 空间 → 包装成 `PageLayout` 追加进同一份 `page.page_layout`。
- 模型接口由 `YoloResult`/`YoloBox` 与 `DocLayoutModel.handle_document` 契约定义，任何满足该契约的检测模型都能即插即用。
- **核心现实**：表格文本翻译已退役。`RapidOCRModel` 是 no-op 垫片，`TranslationConfig` 把 `table_model` 强制置 `None` 并告警，`get_translation_stage` 剔除该阶段，`_do_translate_single` 再加一道守卫——`TableParser.process` 实际从不执行。
- 这条「CLI 还在 → 装配还在 → Config 处清零 → 运行期双重跳过」的链路，是 BabelDOC「保留入口、停用实现」的标准范式，也为未来重新激活留了干净的口子。

## 7. 下一步学习建议

- **横向对比其它条件阶段**：回头重读 [high_level.py:264-296](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L264-L296) 的 `get_translation_stage`，把 `DetectScannedFile`、`AutomaticTermExtractor`、`ILTranslator` 等阶段各自的「启用/禁用条件」列成表，体会同一套条件剔除机制的复用。
- **进入翻译机制**：表格阶段之后，流水线进入 `ILTranslator`（u6-l2）与术语抽取（u6-l4）。建议下一讲学习 **u6-l1 翻译器服务与缓存**，理解 `BaseTranslator`/`OpenAITranslator` 与 SQLite 缓存、漏桶限流。
- **若对版面识别意犹未尽**：可对照阅读 [docvision/doclayout.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py) 的真实 ONNX 推理实现（u5-l2），作为「一个真正在跑的检测模型」参照，与本讲的 no-op `RapidOCRModel` 形成对照。
- **关注后续版本**：表格文本翻译是否会被重新激活，可留意 `docs/release-notes/` 下后续版本的变更说明。
