# PDFPatcher：pypdf + reportlab 叠层回写

## 1. 本讲目标

上一讲（u10-l1）我们弄清了 `PDFTranslationPipeline` 如何把 `DocumentPackage` 变成一列 `PDFReplacement`——每项都带着「哪一页、原文字在图像像素坐标系里的哪个矩形、译文是什么」。本讲打开这条链路的最后一环：**这些替换项是如何真正画回 PDF 的**。

学完本讲你应该能够：

1. 说清「白块覆盖 + 文字叠层」回写方案的原理：为什么不能原地改 PDF 文字，而要整页重绘。
2. 解释预检（preflight）机制：为什么先为所有页完成排版尝试、确认全部放得下之后，才开始画第一块白斑、创建第一个目标文件。
3. 掌握溢出处理的两种策略（`error` 与 `skip`）与 `skipped_replacements` 跳过记录，理解「按 `reading_order` 排序替换项」对绘制顺序的意义。

## 2. 前置知识

### 2.1 为什么不能「原地替换」PDF 里的文字

PDF 是一种面向印刷的页面描述格式：一页的内容是操作符流（画线、贴图、在坐标 \((x, y)\) 处显示某串字形）。它**没有**「第 3 段」「这个句子」这样的结构概念。扫描版书籍更极端——整页本来就是一张位图，文字只存在于像素里。想精确删掉某句话再补一句译文，在 PDF 层面几乎无从下手。

工程上通行的绕法是**叠层（overlay）**：不改原有内容，而是在其上再画一层——先用不透明色块把旧文字盖住，再把新文字画在色块上。本讲的 `PDFPatcher` 就是这个思路的落地。

### 2.2 两套坐标系（承接 u10-l1）

- **图像像素坐标**：OCR 在渲染出的页面位图上工作，`PDFReplacement.bbox` 与 `page_pixel_size` 都在这个坐标系里，原点在**左上角**，y 轴向**下**增长。
- **PDF 用户空间坐标**：reportlab 画图用的坐标，单位是点（point，1/72 英寸），原点在**左下角**，y 轴向**上**增长；页面尺寸来自源 PDF 页的 `mediabox`。

回写必须做一次换算，包括一次 y 轴翻转——这是本讲源码里最值得盯着看的一行。

### 2.3 工具分工

- **pypdf**：纯 Python 的 PDF 读写库。本讲中负责读源 PDF（页数、每页 `mediabox` 尺寸）与写出目标文件。
- **reportlab**：PDF 生成库。`canvas` 提供画位图、画矩形、排版文字的低阶 API；`platypus.Paragraph` 提供自动换行的段落排版（下一讲 u10-l3 的主角）。
- 两者都是**懒加载**的可选依赖：`import` 写在 `patch()` 方法体内，缺失时抛出带指引的 `RuntimeError`。这与 u3-l4 见过的 doc-page-extractor 懒加载模式一脉相承——不做 PDF 回写的用户不必安装 reportlab。

### 2.4 原子写

「先写临时文件，全部成功后再一步改名替换正式文件」。中途任何失败都不会留下半成品。本讲会在预检机制里看到它的实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/pipeline/pdf/patcher.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py) | 本讲主角：`PDFReplacement`、`PDFSkippedReplacement`、`PDFPatcher`，全部回写逻辑 |
| [pdf_craft/pipeline/pdf/text_layout.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py) | `PatchTextOptions` 与 `BoxTextLayout.fit`：把文本适配进固定矩形，预检的排版引擎（细节留给 u10-l3） |
| [pdf_craft/pipeline/pdf/pipeline.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py) | 上游调用方：`PDFTranslationPipeline.patch` 收集替换项后委托 `PDFPatcher.patch` |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py) | 门面入口 `patch_pdf_with_package` 与 `translate_pdf` |
| [tests/test_pdf_patcher.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py) | 行为契约：页数保持、预检不留半成品、skip 记录原因 |
| [pdf_craft/extractor/chapter/chapter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py) | 章节 XML 编码（实践环节需要手工改 `<block>` 文本时用） |

## 4. 核心概念与源码讲解

### 4.1 叠层绘制

#### 4.1.1 概念说明

`PDFPatcher.patch()` 收到一列替换项后，对源 PDF 的**每一页**做一次「整页重建」：

1. 用 PDF handler（Poppler 渲染，见 u3-l2）把该页按指定 dpi 渲染成位图；
2. 在一个与原页同尺寸的 reportlab 画布上，把这张位图铺满整页作为**底图**；
3. 对该页的每个替换项，先在原文字位置画一个**不透明白色矩形**盖住旧内容；
4. 再在白块区域内排版绘制**译文**（reportlab 文本层，可被复制、可被 `extract_text()` 提取）；
5. 把这一页加入 pypdf 的 writer，最后统一写出。

理解这个方案的两个关键点：

- **视觉保真靠底图**。原页的一切——插图、表格、页眉、公式——都以位图形式原样保留；只有被白块盖住的区域被替换。代价是原页的矢量信息被栅格化，清晰度取决于渲染 dpi（默认 300）。
- **文本层是重建的**。原页若有文字层，栅格化后即消失（这正是测试断言 `assertNotIn("Original", page.extract_text())` 能通过的原因）；输出 PDF 里的可提取文字全部来自 reportlab 绘制的译文。换句话说，回写的同时完成了「扫描件 → 带文本层 PDF」的附带收益。

#### 4.1.2 核心流程

`patch()` 的整体流程可以概括为：

```text
输入: source_path, target_path, replacements

阶段 0  准备
  懒加载 pypdf / reportlab（缺失 → RuntimeError）
  reader = 读源 PDF

阶段 1  校验与分桶
  对每个 replacement 调 validate()（页码、bbox、文本、像素尺寸六项检查）
  按页码分桶；每页内部按 reading_order 排序

阶段 2  预检（见 4.2）
  对每个替换项做排版适配 BoxTextLayout.fit
  放不下 → 按 overflow 策略：skip 记录 / 抛 ValueError
  结果缓存进 layouts: {页码: [(替换项, 排版结果), ...]}

阶段 3  绘制
  打开源 PDF（PDF handler）
  for 每一页:
    建临时 overlay.pdf（尺寸 = 该页 mediabox）
    画底图（原页位图铺满）
    第一轮循环：画该页所有白块
    第二轮循环：画该页所有译文
    把 overlay 页加入 writer

阶段 4  落盘
  临时文件写出 → 原子替换 target_path
  记录 skipped_replacements
```

坐标换算是贯穿阶段 3 的基础。设某页 mediabox 宽高为 \( W, H \)（点），替换项声明的页面像素尺寸为 \( P_w, P_h \)，bbox 为 \( (l, t, r, b) \)（像素、左上原点），则白块在 PDF 用户空间的位置为：

\[
s_x = \frac{W}{P_w}, \qquad s_y = \frac{H}{P_h}
\]

\[
x = l \cdot s_x, \qquad y = H - b \cdot s_y, \qquad
\text{宽} = (r - l) \cdot s_x, \qquad \text{高} = (b - t) \cdot s_y
\]

其中 \( y = H - b \cdot s_y \) 就是 y 轴翻转：取 bbox 的**底边**（图像坐标里数值较大的 \( b \)）换算到 PDF 的**低处**。注意横纵缩放比例 \( s_x, s_y \) 是分别计算的——若 `page_pixel_size` 与 mediabox 宽高比略有出入，白块会按页面总宽比做非等比伸缩，仍能与位图上的原文字区域对齐（u10-l1 讲过 `page_pixel_sizes` 记录的正是渲染该页时的实际像素几何）。

#### 4.1.3 源码精读

先看两个数据类。`PDFReplacement` 在 u10-l1 已精读，这里只需记住六个字段中本讲直接消费的四个：`page_index`（分桶）、`bbox` + `page_pixel_size`（坐标换算）、`reading_order`（排序）：

[pdf_craft/pipeline/pdf/patcher.py:L11-L18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L11-L18) —— 冻结数据类 `PDFReplacement`：页码、像素坐标 bbox、译文、页面像素尺寸，以及带默认值的 `dpi=300` 与 `reading_order=0`。

构造函数负责装配排版引擎与 PDF handler，并兼容新旧两套字体参数：

[pdf_craft/pipeline/pdf/patcher.py:L38-L67](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L38-L67) —— `__init__`：若同时传 `options` 与旧的 `font_name`/`font_size` 则抛 `ValueError`（二者互斥）；只有旧参数时把它们折算进一个新 `PatchTextOptions`，且 `font_size` 的语义被明确为「最大适配字号」而非强制字号（docstring 原话：*A supplied font size is the maximum fitted size, not a forced size*）。随后创建 `BoxTextLayout`（排版引擎）、保存 `pdf_handler`（缺省 `DefaultPDFHandler`，即 Poppler 渲染）与 `dpi`，并把 `skipped_replacements` 初始化为空元组。

主方法开头是懒加载与分桶排序：

[pdf_craft/pipeline/pdf/patcher.py:L69-L84](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L69-L84) —— `patch()` 前半段：`import pypdf`、`from reportlab...` 全部写在方法体内，`ImportError` 被转成提示安装可选依赖 `reportlab` 的 `RuntimeError`；接着读源 PDF，对每个替换项执行 `validate()`（传入真实页数做越界检查），按 `page_index` 分桶，**每页内部按 `reading_order` 排序**。

关于这个排序：同一页的替换项若矩形互不重叠，绘制顺序本无影响；但一旦重叠（OCR 检测框贴得很近时并不罕见），先画的文字会被后画的白块盖住。按 `reading_order`（即 OCR 块的页内阅读序号，u5 系列讲过的 `BlockLayout.order`）排序后，绘制层级是确定的——阅读顺序靠后的文字绘制在上层，且同样输入总产出同样的 PDF，结果可复现。

绘制主循环是本模块的心脏：

[pdf_craft/pipeline/pdf/patcher.py:L104-L125](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L104-L125) —— 对 `reader.pages` 逐页（页码从 1 计数）处理：每页开一个 `TemporaryDirectory`，在其中创建 reportlab `canvas`，页面尺寸直接取该页 `mediabox` 的宽高；随后 `document.render_page(index, render_dpi)` 用 PDF handler 渲染原页位图并 `drawImage` 铺满整页（底图）；接着是**两轮循环**——第一轮 `for replacement, _ in page_layouts` 只画白块（`_draw_background`），第二轮 `for replacement, fitted in page_layouts` 只画文字（`_draw_text`）。两轮分离保证「先所有遮盖、后所有文字」，任何一个替换项的译文都不会被相邻替换项的白块压住。画完 `overlay.save()` 存成临时 overlay.pdf，用 pypdf 读回其第 0 页加入 `writer`。`finally` 里关闭 PDF handler 的文档句柄。

注意 `render_dpi` 的取值逻辑（L114）：该页有替换项时取**第一个替换项声明的 `dpi`**，否则用 patcher 构造时的 `self.dpi`。因为坐标换算只依赖比例（像素尺寸 ÷ mediabox），渲染 dpi 不影响白块定位，只决定底图的清晰度；跟随替换项的 dpi 是为了让底图分辨率与产生该 bbox 的那次 OCR 渲染保持一致。

三个私有方法完成几何与绘制：

[pdf_craft/pipeline/pdf/patcher.py:L153-L161](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L153-L161) —— `_box_in_points`：静态方法，把像素 bbox 换算成 PDF 点坐标，`y = height - bottom * scale_y` 就是 4.1.2 公式里的 y 轴翻转，返回 `(x, y, 宽, 高)`。

[pdf_craft/pipeline/pdf/patcher.py:L163-L166](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L163-L166) —— `_draw_background`：`setFillColorRGB(1, 1, 1)` 设纯白，`rect(x, y, w, h, stroke=0, fill=1)` 画**无描边、实心填充**的矩形，即遮盖旧文字的白块。

[pdf_craft/pipeline/pdf/patcher.py:L168-L175](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L168-L175) —— `_draw_text`：黑色（`setFillColorRGB(0, 0, 0)`）绘制预检阶段排版好的 `fitted.paragraph`（reportlab `Paragraph` 对象），`drawOn` 的落点是盒子**左上角向内收一个 padding、再向下留出段落高度**——即文本从白块顶部开始向下排。`fitted.height` 来自预检缓存的排版结果，绘制阶段不再做任何尺寸计算。

最后看它与上游的衔接（u10-l1 的收尾处）：

[pdf_craft/pipeline/pdf/pipeline.py:L47-L73](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L47-L73) —— `PDFTranslationPipeline.patch`：校验包、补齐缺失页几何、遍历章节收集替换项（`lambda text: text` 恒等转换，即「包里现在是什么就画什么」），最后一行 `self.patcher.patch(pdf_path, target_path, replacements)` 进入本讲的方法。

[pdf_craft/craft.py:L160-L172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L160-L172) —— 门面 `patch_pdf_with_package`：加载并校验包、执行 `_validate_package_for_pdf` 预检（缺页几何时在渲染兜底前快败，见 u10-l1），然后构造 `PDFTranslationPipeline` 走上面的 `patch` 入口。注意这里**没有向 `PDFPatcher` 传 `options`**——所以门面路径用的是默认 `PatchTextOptions()`，其 `overflow` 策略是 `"error"`（4.3 会看到这对实践任务的影响）。

#### 4.1.4 代码实践

**实践目标**：不依赖任何 OCR 服务，用手工构造的替换项跑通一次最小回写，亲眼确认「白块盖旧字、叠层写新字、页数不变」。

**操作步骤**（以下为示例代码，模仿官方测试 [tests/test_pdf_patcher.py:L13-L33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py#L13-L33) 的写法）：

```python
# mini_patch.py（示例代码）
import tempfile
from pathlib import Path

import pypdf
from reportlab.pdfgen import canvas

from pdf_craft import PDFPatcher, PDFReplacement

with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    source = root / "source.pdf"
    target = root / "target.pdf"

    # 1. 用 reportlab 造一个 200x200 点的单页 PDF，写一行 "Original"
    doc = canvas.Canvas(str(source), pagesize=(200, 200))
    doc.setFont("Helvetica", 12)
    doc.drawString(20, 160, "Original")
    doc.save()

    # 2. 声明一个替换项：第 1 页，像素坐标 bbox，译文 "Translated"
    #    假想该页是 600x600 像素渲染出来的（比例换算 200/600）
    replacement = PDFReplacement(
        page_index=1,
        bbox=(50, 25, 450, 100),   # 像素、左上原点
        text="Translated",
        page_pixel_size=(600, 600),
    )

    # 3. 回写
    patcher = PDFPatcher(font_size=12)
    patcher.patch(source, target, [replacement])

    # 4. 用 pypdf 验证
    reader = pypdf.PdfReader(str(target))
    print("页数:", len(reader.pages))                    # 期望 1
    text = reader.pages[0].extract_text()
    print("含译文:", "Translated" in text)                # 期望 True
    print("含原文:", "Original" in text)                  # 期望 False
```

运行：`python mini_patch.py`（需要 `pip install pypdf reportlab pdf-craft`，PDF 回写不涉及 OCR，无需凭据与 GPU；但 `DefaultPDFHandler` 渲染底图依赖 Poppler，请确认 `pdfinfo -v` 可用，参见 u1-l2）。

**需要观察的现象**：四行输出依次为 `1 / True / True / False`。最后一行尤其值得体会——源 PDF 里的 "Original" 是矢量文字层，输出里却提不出来了：它已被栅格化进底图并被白块盖住。

**预期结果**：回写成功，`target.pdf` 视觉上原文字位置变成白底黑字的 "Translated"，且文本层只有译文。若把 `bbox` 改成 `(4, 4, 2, 3)` 这类非法矩形（右 ≤ 左），`validate` 会当场抛 `ValueError`（对应测试 L35-41）。完整跑一遍无需 OCR 服务，可本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `patch()` 的绘制循环里，白块和文字要分成两轮 `for` 循环画，而不是每个替换项「画白块→画文字」一口气完成？

**答案**：假设替换项 A、B 矩形有重叠，逐项完成时 B 的白块会盖住刚画好的 A 的文字。两轮分离（先画完该页**所有**白块，再画**所有**文字）保证任何译文都不会被后续白块压住。代码见 [patcher.py:L117-L120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L117-L120)。

**练习 2**：`_box_in_points` 里为什么是 `y = height - bottom * scale_y`，而不是 `y = top * scale_y`？

**答案**：两套坐标系 y 轴方向相反——图像坐标原点在左上、y 向下；PDF 用户空间原点在左下、y 向上。bbox 的 `bottom` 是图像坐标里数值较大的底边，换算到 PDF 侧要先乘 `scale_y` 再被页高 `height` 反转，得到白块的**下边缘**。若直接用 `top * scale_y`，白块会跑到页面顶部甚至出界。

**练习 3**：输出 PDF 的底图 dpi 由什么决定？提高它会改变白块的位置吗？

**答案**：由该页第一个替换项的 `dpi`（无替换项时用 patcher 的 `self.dpi`）决定，见 [patcher.py:L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L114)。不会改变白块位置——坐标换算只依赖「像素尺寸 ÷ mediabox 尺寸」的比例，dpi 只影响底图清晰度与文件体积。

### 4.2 预检机制

#### 4.2.1 概念说明

「预检（preflight）」指：**在任何绘制发生之前，先对所有页的所有替换项完成排版适配**，确认每段译文都能放进自己的白块；任何一项放不下就整体失败（或按策略跳过），绝不产出残缺文件。

为什么值得单独设计一个阶段？考虑没有预检的坏情形：一本书 300 页，回写进行到第 280 页时遇到一段译文在最小字号下也塞不进原 bbox。此时如果就地抛错，磁盘上已经躺着半份 `target.pdf`——它**看起来是成功的**（能打开、绝大部分页正确），调用方极可能把它当成品用出去。这比干脆失败危险得多。源码注释把这条设计意图写得很直白：

> *Preflight all pages before drawing any white rectangles or creating a target file. A failed fit must not masquerade as a successful patch.*（画任何白块、创建任何目标文件之前先预检所有页；失败的适配不得伪装成成功的回写。）

排版适配本身由 `BoxTextLayout.fit` 完成（u10-l3 精读），本讲只需理解它的契约：给定文本与盒子的宽高（点），返回一个「**已被证明完整放得下**」的 `FittedParagraph`（含 reportlab `Paragraph` 对象与实际宽高）；在最小字号下仍放不下则抛 `ValueError`。预检阶段调它拿结果，绘制阶段只消费结果——`_draw_text` 里没有任何尺寸计算，画的就是预检验证过的那个段落。

配合预检的还有**原子写**：目标文件先写到同目录下的临时文件（`NamedTemporaryFile(delete=False)`），`writer.write` 全部成功后再 `replace` 成正式路径。这样即使写出阶段意外失败（磁盘满、进程被杀），也不会留下半份目标文件。

#### 4.2.2 核心流程

```text
预检阶段（绘制前）:
  layouts = {}
  for 页 in 源PDF所有页:                     # 注意：遍历的是全部页，不只含替换项的页
    for replacement in 该页替换项（已按 reading_order 排序）:
      try:
        fitted = BoxTextLayout.fit(文本, 盒宽, 盒高)   # 盒宽高由 _box_in_points 换算
      except ValueError:
        if overflow == "skip": 记入 skipped; continue   # 见 4.3
        else: 抛 ValueError（附页码与 bbox 上下文）
      layouts[页].append((replacement, fitted))

绘制阶段: 只遍历 layouts 中已验证的条目 → 任何 _draw_* 都不会遇到未适配文本

落盘阶段:
  写入 target_path.parent 下的临时 .pdf
  temporary_path.replace(target_path)        # 原子改名
```

`fit` 内部对「放不下」的判定值得先睹为快（细节下一讲展开）：它先用**最小字号**排一次版，若最小字号下段落的自然宽高仍超出可用区域，立即抛 `ValueError`（附所需的宽高数值）；否则在最小与最大字号之间做**二分查找**（步长 1/4 点），返回能完整放下的最大字号。也就是说，预检拦截的是「最小字号都放不下」的硬溢出。

#### 4.2.3 源码精读

[patcher.py:L86-L102](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L86-L102) —— 预检主段（原文注释即在此处）：外层 `for index, page in enumerate(reader.pages, 1)` 遍历**全部页**并读取每页 `mediabox` 宽高；内层对该页每个替换项调 `self._fit_replacement(replacement, width, height)`。`ValueError` 被捕获后分流：`overflow == "skip"` 时构造 `PDFSkippedReplacement` 记入 `skipped` 列表并 `continue`；否则重新抛出**带页码与 bbox 上下文**的 `ValueError`（`f"page {index}, bbox {replacement.bbox}: {error}"`），让调用方能定位到具体哪一页哪个矩形出了问题。成功者连同排版结果缓存进 `layouts` 字典。

[patcher.py:L149-L151](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L149-L151) —— `_fit_replacement`：先用 `_box_in_points` 把像素 bbox 换算成点坐标取盒宽高，再委托 `self._layout.fit(...)`。预检与绘制的全部排版都收敛在这一个入口。

[text_layout.py:L42-L60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L42-L60) —— `BoxTextLayout.fit` 的前半段：空白归一化（`" ".join(text.split())`）、扣掉双侧 padding 得可用宽高（非正即抛「bbox too small」）、注册字体，然后是关键的**最小字号整体检查**——用 `min_font_size` 排版并取自然尺寸（`_natural_size` 以 1,000,000 的高度调 `paragraph.wrap`，逼 Platypus 给出完整自然高度而非分段隐藏尾部），超出可用区域即抛带数值信息的 `ValueError`。这正是预检要拦的「硬溢出」。

[patcher.py:L127-L131](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L127-L131) —— 原子落盘：先 `mkdir` 目标父目录；`NamedTemporaryFile(dir=target_path.parent, suffix=".pdf", delete=False)` 在**目标同目录**创建临时文件（同目录保证 `replace` 是同一文件系统上的原子改名），`writer.write(output)` 写完关闭后 `temporary_path.replace(target_path)` 一步换名。至此预检的承诺兑现——走到这里的文件必然是完整的。

对应的行为契约在测试里：

[tests/test_pdf_patcher.py:L80-L96](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py#L80-L96) —— `test_preflight_failure_leaves_no_partial_target_file`：把字号钉死为 8（`max_font_size=8, min_font_size=8`，无处可缩），再塞进 `"too much text " * 100` 到一个 20x20 像素的小 bbox；断言抛出的 `ValueError` 信息以 `"page 1, bbox"` 开头（带定位上下文），且 **`target.pdf` 不存在**——预检失败连文件都不创建。

#### 4.2.4 代码实践

**实践目标**：亲手复现「预检失败不留半成品文件」。

**操作步骤**（示例代码，改写自上述测试）：

```python
# preflight_fail.py（示例代码）
import tempfile
from pathlib import Path

from reportlab.pdfgen import canvas

from pdf_craft import PDFPatcher, PDFReplacement, PatchTextOptions

with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    source = root / "source.pdf"
    target = root / "target.pdf"
    doc = canvas.Canvas(str(source), pagesize=(200, 200))
    doc.drawString(1, 1, "source")
    doc.save()

    # 字号钉死在 8pt：min == max，fit 没有缩放余地
    patcher = PDFPatcher(options=PatchTextOptions(max_font_size=8, min_font_size=8))
    try:
        patcher.patch(
            source, target,
            [PDFReplacement(1, (10, 10, 30, 30), "too much text " * 100, (200, 200))],
        )
        print("不应走到这里")
    except ValueError as e:
        print("捕获 ValueError:", str(e)[:60], "...")
    print("目标文件已创建:", target.exists())   # 期望 False
```

**需要观察的现象**：`ValueError` 信息形如 `page 1, bbox (10, 10, 30, 30): replacement text cannot fit bbox at minimum font size 8.0: required ...`（含所需与可用宽高）；最后一行打印 `False`。

**预期结果**：与测试断言一致——异常带页码与 bbox 定位，`target.pdf` 未被创建。可本地验证（无需 OCR）。

#### 4.2.5 小练习与答案

**练习 1**：预检循环遍历的是 `reader.pages` 的**全部页**，而不是只遍历有替换项的页。这样做有必要吗？

**答案**：对排版结果本身没必要（无替换项的页在内层循环里什么都不做）。真正的意义在于统一以「页码 → 替换项」的视图组织流程，并让 `_fit_replacement` 拿到**每页各自的 mediabox 尺寸**做换算——不同页尺寸可以不同（如横插页），逐页读宽高保证了换算正确。见 [patcher.py:L89-L92](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L89-L92)。

**练习 2**：`NamedTemporaryFile` 为什么要把 `dir` 设成 `target_path.parent`，而不是用系统默认临时目录 `/tmp`？

**答案**：`Path.replace` 的原子性只在**同一文件系统**内成立。临时文件与目标同目录，保证最后的改名是原子操作；若跨文件系统（如 `/tmp` 与目标分属不同挂载点），`replace` 退化为「复制 + 删除」，中途失败就会留下不完整的目标文件，原子写的承诺破产。

**练习 3**：预检把 `fitted`（排版结果）缓存进 `layouts` 后，绘制阶段为什么可以完全不做尺寸计算？

**答案**：`fit` 返回的 `FittedParagraph` 自带已验证的 `Paragraph` 对象与 `height`，`_draw_text` 只负责 `drawOn` 定位（见 [patcher.py:L168-L175](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L168-L175)）。预检与绘制共享同一结果，杜绝了「预检时排得下、绘制时排不下」的两态不一致。

### 4.3 跳过记录

#### 4.3.1 概念说明

预检遇到放不下的替换项时有两种处置策略，由 `PatchTextOptions.overflow` 控制：

| 策略 | 行为 | 适用场景 |
| --- | --- | --- |
| `"error"`（**默认**） | 抛 `ValueError`，整体失败，不产出文件 | 期望「要么全对、要么重来的确定性产物」；翻译质量优先，宁可不生成也不能缺段 |
| `"skip"` | 把该项记入 `skipped_replacements` 后跳过，其余照常回写 | 长文档容忍个别溢出：被跳过的块白块都不画，原文字（底图位图）原样保留，读者仍能看到原文 |

`skip` 的安全性在于「跳过是彻底的」：该替换项既不画白块也不画文字，页面那一块保持底图原样——不是留下一个刺眼的空白矩形。而每条跳过记录都是一个 `PDFSkippedReplacement(page_index, bbox, reason)`，`reason` 直接携带 `fit` 抛出的原始错误文本（含所需 vs 可用的宽高数值），调用方可以据此复盘：调大 `max/min_font_size` 区间、改写译文长度，或干脆接受该块保留原文。

一个容易踩的坑：`skipped_replacements` 是 **patcher 实例属性**，在 `patch()` 成功跑完后才被赋值（构造时是空元组）。所以要读取它，必须**持有同一个 patcher 实例**再调 `patch`——这也解释了为什么 `PDFTranslationPipeline` 允许从外部注入自定义 `patcher`（[pipeline.py:L18-L21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L18-L21)）。另需注意：走门面 `patch_pdf_with_package` 时内部新建的 pipeline/patcher 对调用方不可见，且默认策略是 `error`——想在真实包上观察 `skip`，必须自己组装 `PDFTranslationPipeline(patcher=PDFPatcher(options=PatchTextOptions(overflow="skip")))`。

#### 4.3.2 核心流程

```text
PatchTextOptions(overflow="skip")
        │
        ▼
预检捕获 fit 的 ValueError
        │
        ▼
skipped.append(PDFSkippedReplacement(页码, bbox, str(error)))   # 该项不进 layouts
        │
        ▼
绘制阶段：该页 layouts 中没有它 → 不画白块、不画文字 → 底图保留原文
        │
        ▼
patch() 收尾: self.skipped_replacements = tuple(skipped)        # 调用方可检查
```

#### 4.3.3 源码精读

[patcher.py:L21-L27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L21-L27) —— 冻结数据类 `PDFSkippedReplacement`，docstring 明确其定位：*An explicitly skipped overflow, retained for callers to inspect*（被显式跳过的溢出，保留给调用方检查）。三字段：页码、bbox、原因字符串。

[text_layout.py:L11-L22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L11-L22) —— `PatchTextOptions` 全貌：字体（默认 `STSong-Light`，reportlab 内置的中文 CID 字体）、字号区间（默认最大 12 最小 4）、行高、双侧 padding、对齐，以及最后一项 `overflow: Literal["error", "skip"] = "error"`——默认策略是**抛错**，跳过必须显式选择。这体现了库的默认倾向：不静默丢弃内容（与 u6-l3 表格渲染「宁丑勿丢数据」的哲学一致，只是方向相反——那里保数据、这里保完整性）。

[patcher.py:L96-L98](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L96-L98) —— 预检中的跳过分支：`if self.options.overflow == "skip"` 时以页码、bbox 与 `str(error)` 构造记录并 `continue`；注意记录用的是**循环变量 `index`**（当前页码）而非 `replacement.page_index`，二者在分桶后必然相等。

[patcher.py:L132](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L132) —— `patch()` 的最后一行：`self.skipped_replacements = tuple(skipped)`。只有走到这一行（即整个 patch 成功完成）跳过记录才可见；中途抛错时实例属性停留在旧值。

[tests/test_pdf_patcher.py:L98-L115](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py#L98-L115) —— `test_explicit_skip_records_overflow_reason`：与 4.2 的失败用例**完全相同的输入**（钉死 8pt + 超长文本 + 小 bbox），只把 `overflow` 换成 `"skip"`；断言 `patch` 正常返回、`len(patcher.skipped_replacements) == 1`、且 `reason` 含 `"cannot fit bbox"`。同一个溢出，两种策略两种结局——这两个测试合起来就是本讲「预检 + 策略」的完整行为规格。

#### 4.3.4 代码实践

这是本讲的主实践，对应任务：**用真实提取包回写 PDF；在包里制造一个超长文本块，观察它进入 `skipped_replacements` 而非被强行绘制；再用 pypdf 验证输出页数与源 PDF 一致。**

**实践目标**：在真实 `DocumentPackage` 上走通 `skip` 策略，并拿到可检查的跳过报告。

**操作步骤**：

第 1 步——准备提取包（需要 OCR 凭据，待本地验证；若你已按 u1-l2 配好 DeepSeek vendor OCR 则可直接运行）：

```python
# step1_extract.py（示例代码）
from pathlib import Path
from pdf_craft import PDFCraft, PDFOptions, ExtractionOptions
from pdf_craft.ocr_config import DeepSeekOCRVendorConfig

craft = PDFCraft()
craft.extract_pdf(
    Path("tests/assets/newton.pdf"),
    Path("run/package"),
    PDFOptions(ocr=DeepSeekOCRVendorConfig(
        base_url="...", api_key="...", model="...",
    )),
    ExtractionOptions(),
)
```

第 2 步——基线回写（可选）：用门面确认整条链路正常：

```python
# step2_baseline.py（示例代码）
from pathlib import Path
from pdf_craft import PDFCraft

PDFCraft().patch_pdf_with_package(
    Path("tests/assets/newton.pdf"), Path("run/package"), Path("run/baseline.pdf"),
)
```

第 3 步——在包里制造溢出：把某个正文 `<block>` 的文本拉长到远超原 bbox。章节 XML 的结构是 `<chapter><body><paragraph ref="text"><block page_index=".." order=".." det="..">文本</block>...`，块的纯文本就是 `<block>` 元素的 `.text`（编码逻辑见 [chapter.py:L399-L409](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L399-L409) 与 [markdown/paragraph/types.py:L52-L66](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L52-L66)）：

```python
# step3_stretch.py（示例代码）
import xml.etree.ElementTree as ET
from pathlib import Path

chapter_file = sorted(Path("run/package/chapters").glob("chapter_*.xml"))[0]
tree = ET.parse(chapter_file)
block = tree.getroot().find(".//paragraph[@ref='text']/block")
print("原文本:", (block.text or "")[:40], "... bbox det =", block.get("det"))
block.text = (block.text or "") * 30          # 拉长 30 倍，远超原区域
tree.write(chapter_file, encoding="utf-8", xml_declaration=True)
```

第 4 步——用 `skip` 策略回写并检查报告与页数：

```python
# step4_skip.py（示例代码）
from pathlib import Path

import pypdf

from pdf_craft import PDFPatcher, PatchTextOptions
from pdf_craft.pipeline.pdf import PDFTranslationPipeline

patcher = PDFPatcher(options=PatchTextOptions(overflow="skip"))
pipeline = PDFTranslationPipeline(patcher=patcher)
pipeline.patch(
    Path("tests/assets/newton.pdf"),
    Path("run/skipped.pdf"),
    Path("run/package"),
)

source_pages = len(pypdf.PdfReader("tests/assets/newton.pdf").pages)
target_pages = len(pypdf.PdfReader("run/skipped.pdf").pages)
print("源页数:", source_pages, "输出页数:", target_pages)          # 期望相等
print("跳过数量:", len(patcher.skipped_replacements))
for item in patcher.skipped_replacements:
    print(f"  page {item.page_index} bbox {item.bbox}: {item.reason[:70]}...")
```

**需要观察的现象**：

- 第 4 步正常完成、生成 `run/skipped.pdf`；`跳过数量 ≥ 1`，被跳过的正是第 3 步拉长的那个块（页码与 `det` 对得上），`reason` 含 `cannot fit bbox at minimum font size` 及所需/可用宽高。
- 打开 `run/skipped.pdf` 找到对应页：该块位置保留着**原文的位图**（没有白块、没有译文），其余替换项正常。
- 若把第 4 步中 `overflow` 改回默认（去掉 `options` 参数或用 `PatchTextOptions()`），同样的包会**直接抛 `ValueError`**——这就是门面 `patch_pdf_with_package` 的行为（它不传 `options`，见 [craft.py:L170-L172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L170-L172)）。
- 源页数与输出页数相等。

**预期结果**：跳过记录可检查、输出页数一致、被跳过块保留原文。第 1 步依赖 OCR 凭据，待本地验证；第 3、4 步的逻辑（XML 拉长 + skip 管线）不依赖网络，若你已有任意现成提取包即可直接从第 3 步开始。

#### 4.3.5 小练习与答案

**练习 1**：`overflow="skip"` 跳过一个替换项后，输出 PDF 里对应区域是什么样子？为什么这不是「留白」？

**答案**：保留底图上的原文字位图。因为跳过发生在预检阶段——该项从未进入 `layouts`，绘制循环的两轮（白块、文字）都遍历不到它，所以既不画白块也不画译文，那一块就是原页位图本身。见 [patcher.py:L92-L102](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L92-L102) 与 L113（`page_layouts` 只含 `layouts` 缓存的条目）。

**练习 2**：为什么默认策略是 `error` 而不是 `skip`？

**答案**：静默跳过意味着成品 PDF 里混着「没翻译的段落」，调用方若不主动检查 `skipped_replacements` 就会把残缺成品当完整翻译发布——这正是预检注释里说的 *masquerade as a successful patch*。默认抛错把选择权交回调用方：知道有溢出的人可以显式选 `skip` 并检查记录。这与库在其他环节「不静默丢弃内容」的取向一致（如 u10-l1 的 `_to_patch_text` 对未知类型抛 `TypeError`）。

**练习 3**：`skipped_replacements` 为什么不作为 `patch()` 的返回值，而要挂在实例上？

**答案**：`patch()` 的签名返回 `None`，主产物是 `target_path` 文件；跳过报告属于「顺带可检查的旁路信息」，挂实例上让关心的人按需读取（测试 L114 直接访问 `patcher.skipped_replacements`）。代价是调用方必须持有 patcher 实例——所以 `PDFTranslationPipeline` 开放了 `patcher` 注入口，门面内部自建的 patcher 则意味着门面路径实际只有默认 `error` 策略。设计取舍见 [patcher.py:L67](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L67) 与 [pipeline.py:L18-L21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L18-L21)。

## 5. 综合实践

把本讲三个模块串成一个「回写质量巡检」小任务：

1. **基线**：对 `tests/assets` 下任一 PDF 完成提取（或使用你已有的包），用 `PDFCraft().patch_pdf_with_package` 生成基线回写 PDF。
2. **对比实验**：仿照 4.3.4 第 3 步，写一个脚本对包里**每个** `ref="text"` 的 `<block>` 计算文本长度，找出最长的一个并打印其 `page_index`/`det`；先用默认 `error` 策略跑一次回写记录是否抛错，再切 `overflow="skip"` 跑一次。
3. **巡检报告**：遍历 `skipped_replacements`，输出一张「页码 → bbox → 所需宽高 vs 可用宽高」的表格（数值从 `reason` 字符串里解析，或直接整段打印）；对每条记录判断：是译文太长（该考虑 `PatchTextOptions` 放宽字号区间，即 u10-l3 的主题），还是原 bbox 本来就小（该考虑跳过是否可接受）。
4. **完整性校验**：用 pypdf 断言输出页数与源 PDF 一致、且每页 `extract_text()` 非空（确认文本层存在）。

预期产出：一份基线 PDF、一份带跳过的 PDF、一张跳过巡检表。这个过程正是给真实书籍做「翻译回写」时的量产前检查——先小样跑通 `error`，再决定长文档是否放宽为 `skip` 加人工巡检。

## 6. 本讲小结

- `PDFPatcher` 采用**整页重建**的叠层方案：PDF handler 按替换项的 dpi 渲染原页位图铺满画布，白块（`rect` 纯白填充）盖住旧文字，reportlab `Paragraph` 绘制译文文本层；pypdf 负责读源页尺寸/页数与写出，原页矢量内容被栅格化、原文字层消失，输出 PDF 的可提取文字全部是译文。
- 坐标换算在 `_box_in_points`：横纵各自按「页面总尺寸比例」缩放，y 轴经 \( y = H - b \cdot s_y \) 翻转；每页替换项按 `reading_order` 排序，白块与文字**两轮循环**分画，保证重叠区域层级确定、译文不被相邻白块压住。
- **预检机制**：绘制与建文件之前先对所有页的全部替换项跑 `BoxTextLayout.fit`，最小字号都放不下即失败（带页码与 bbox 定位），配合「目标同目录临时文件 + `replace` 原子改名」，失败的适配绝不伪装成成功的回写。
- **跳过记录**：`PatchTextOptions.overflow` 提供 `error`（默认，整体失败）与 `skip`（记入 `PDFSkippedReplacement(page_index, bbox, reason)`，该块不画白块不画文字、底图保留原文）两种策略；`skipped_replacements` 是 patcher 实例属性，须持有实例、`patch()` 成功后才可读；门面 `patch_pdf_with_package` 走默认 `error`，要 `skip` 须自组 `PDFTranslationPipeline(patcher=PDFPatcher(options=PatchTextOptions(overflow="skip")))`。

## 7. 下一步学习建议

下一讲 **u10-l3「BoxTextLayout：字号自适应与排版」** 将打开本讲反复借用的 `fit` 黑盒：最小字号检查、1/4 点步进的二分查找、CJK 断行（`wordWrap="CJK"`）、`STSong-Light` CID 字体的注册与「非拉丁文本配西文字体」的防错检查。建议先通读 [pdf_craft/pipeline/pdf/text_layout.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py)（全文不足 140 行）与 [tests/test_pdf_text_layout.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_text_layout.py)，带着一个问题读：为什么 `_natural_size` 要用一个高达 1,000,000 的帧高去调 `paragraph.wrap`？读完 u10-l3 你就能完整回答。此后可进入 u11 单元，看 `pdf_craft_tool` CLI 如何把本讲的管线封装成 `package patch-pdf` 子命令。
