# PDF 翻译管线：从包到替换列表

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `PDFTranslationPipeline.translate` 与 `patch` 两个入口各自的输入约定与适用场景，以及门面 `PDFCraft.translate_pdf` 实际走的是哪条路。
2. 逐字段说清 `PDFReplacement` 中 `bbox` 与 `page_pixel_size` 的数据来源（OCR 检测框与 `document.json` 的页面几何）。
3. 理解 `_to_patch_text` 为什么在序列化章节内容时保留公式定界符、脚注引用标记，只剥掉 HTML 外壳。
4. 理解当包内页几何缺失时，管线如何用 PDF handler 现场渲染兜底，以及门面为什么用预检把这种情况提前拦下。

## 2. 前置知识

本讲是专家层「PDF 翻译与回写」单元的第一讲，建立在前几单元的结论之上，先快速回顾：

- **DocumentPackage（u6-l1）**：提取器产出的中间包，`document.json` 中的 `page_pixel_sizes` 记录每页 OCR 位图的像素尺寸，`bbox_coordinate_space` 声明所有 bbox 处于「OCR 位图像素坐标系」。
- **BlockLayout（u5-l1）**：章节里最小的文本块，携带 `page_index`（所在页，从 1 起数）、`order`（页内阅读序号）、`det`（OCR 检测框，四元组 `(left, top, right, bottom)`，像素坐标）与 `content`（字符串、行内公式、脚注引用、HTML 容器混排的列表）。
- **布局归一化（u3-l4）**：OCR 布局类型经 `_LAYOUT_KIND_TO_REF` 归一化后，`title` 被映射为 `sub_title`——所以本讲会看到管线按 `{"text", "sub_title"}` 筛选段落。
- **转换器协议（u7-l1）**：`ChapterTransformer` 按「章进章出」改写 `Chapter` 对象；`ChapterPackageTransformer` 以「复制—改写」方式产出新包；`SubmitKind` 有 REPLACE / APPEND_TEXT / APPEND_BLOCK 三种译文落地方式。

还需要两个本讲新引入的坐标概念：

- **两套坐标系**：OCR 世界用「图像像素坐标」（原点在左上，y 向下增长）；PDF 世界用「点（point）坐标」（原点在左下，y 向上增长，1 点 = 1/72 英寸）。回写时必须做一次换算与 y 轴翻转。
- **叠层（overlay）回写**：PDF 回写不是改原文字对象，而是「整页渲染成背景图 + 白块盖住原文字区域 + 在白块上画译文」。每个待替换区域抽象成一条 `PDFReplacement`（替换项），本讲讲的就是这些替换项如何被收集出来；真正的绘制留给下一讲的 `PDFPatcher`。

像素与点之间的换算依托 dpi（每英寸像素数）：

\[
\text{点数} = \frac{\text{像素数}}{\text{dpi}} \times 72
\]

但管线实际不用这条公式，而是用「页面总宽比」直接缩放（见 4.3.3），这样即使包内 dpi 与真实渲染 dpi 有出入，也能保证替换框铺满正确的页面区域。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pdf_craft/pipeline/pdf/pipeline.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py) | 本讲主角：`PDFTranslationPipeline`，遍历章节块并收集 `PDFReplacement` 列表 |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py) | 门面：`translate_pdf` / `patch_pdf_with_package` / `_translate_for_pdf` / `_TextChapterTransformer` / `_validate_package_for_pdf` |
| [pdf_craft/pipeline/pdf/patcher.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py) | `PDFReplacement` 数据定义与坐标换算（绘制细节属下一讲） |
| [pdf_craft/extractor/chapter/chapter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py) | `ParagraphLayout` / `BlockLayout` 等数据来源 |
| [pdf_craft/extractor/chapter/reader.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reader.py) | `create_chapters_reader`：流式读取 `chapter_head.xml` 与 `chapter_N.xml` |
| [pdf_craft/expression.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/expression.py) | `to_markdown_string`：公式按类型包回 Markdown 定界符 |
| [pdf_craft/document/package.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/document/package.py) | `page_pixel_sizes()`：从 `document.json` 读页面几何 |

## 4. 核心概念与源码讲解

### 4.1 替换收集：translate 与 patch 两个入口

#### 4.1.1 概念说明

`PDFTranslationPipeline` 只解决一个问题：**把一个 DocumentPackage 变成一列 `PDFReplacement`**，然后交给 patcher 绘制。它提供两个入口，区别只在「替换文本从哪来」：

- `translate(pdf_path, target_path, package, transformer)`：包里是**原文**，由调用者提供转换器（一个 `Callable[[str], str]` 文本回调，或一个 `ChapterTransformer`），管线边遍历边转换、边收集。
- `patch(pdf_path, target_path, package)`：包里已经是**最终文本**（翻译在别处完成，比如你用 `translate_package` 或手工改过 `chapter_N.xml`），管线不做任何转换，把每个文本块原样收集回写。

官方文档对后者的一句话说明：「It runs neither OCR nor an LLM; it uses the translated package's page geometry to patch the source PDF.」（[docs/en/PDF_TRANSLATION.md:140](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/PDF_TRANSLATION.md#L140)）

#### 4.1.2 核心流程

两个入口共享同一条收集流水线，差别只在「转换器是谁」：

```text
translate 入口：
  加载/校验包 → 打开 PDF 文档(可选) → 读页几何表 pages
  → 逐章迭代(chapter_head 优先，其后 chapter_N)
      structured = not callable(transformer)
      structured? → transformed = transformer.transform(chapter)，回调 = 恒等
      否则       → transformed = chapter 原样，      回调 = transformer 本身
      → _collect_chapter(transformed, 回调, ...)
  → 关闭文档 → patcher.patch(pdf, target, replacements)

patch 入口：
  同上，但回调恒等于 lambda text: text，且 structured=True
  （即：不做转换，收集全部文本块）
```

门面 `PDFCraft.translate_pdf` 的真实路径值得特别注意——**它最终走的是 patch 入口**：

```text
PDFCraft.translate_pdf(source, package, output, transformer, steps)
  1. 预检 steps：SubmitKind.APPEND_BLOCK → 直接 ValueError
  2. TemporaryDirectory 中：
     a. callable? → 包成 _TextChapterTransformer
     b. translate_package → ChapterPackageTransformer 复制-改写
        → 临时目录 translated/ 下的"翻译后新包"
     c. _apply_steps 链式执行附加步骤
  3. patch_pdf_with_package(source, 翻译后新包, output)
     → _validate_package_for_pdf 预检
     → PDFTranslationPipeline(...).patch(...)   ← patch 入口！
     → patcher 绘制落盘
```

也就是说：门面把「翻译」与「回写」拆成两个阶段，翻译阶段的产物是一个完整的包（会被 `ChapterPackageTransformer` 复制-改写），回写阶段用 patch 入口无转换地收集。而管线自己的 `translate` 方法是为「不想先落盘翻译包、想边走边转」的直接调用者准备的捷径。

#### 4.1.3 源码精读

先看管线构造函数与 `translate`：

[pdf_craft/pipeline/pdf/pipeline.py:L18-L21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L18-L21)——构造时可注入 `pdf_handler`（页几何兜底要用）、`patcher`（可替换为测试替身）与 `dpi`（默认 300）：

```python
def __init__(self, pdf_handler: PDFHandler | None = None, patcher: PDFPatcher | None = None, dpi: int = 300) -> None:
    self.pdf_handler = pdf_handler
    self.patcher = patcher or PDFPatcher(pdf_handler=pdf_handler)
    self.dpi = dpi
```

[pdf_craft/pipeline/pdf/pipeline.py:L23-L45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L23-L45)——`translate` 的完整主体：接受 `DocumentPackage | Path`（路径会经 `DocumentPackage.from_path` 加载）与联合类型转换器；`try/finally` 保证 PDF 文档句柄一定被关闭：

```python
def translate(self, pdf_path, target_path, package, transformer) -> None:
    package = package if isinstance(package, DocumentPackage) else DocumentPackage.from_path(package)
    package.validate()
    document = self.pdf_handler.open(pdf_path) if self.pdf_handler else None
    replacements: list[PDFReplacement] = []
    try:
        pages = package.page_pixel_sizes()
        reader = create_chapters_reader(package.chapters_path)
        for chapter in reader():
            structured = not callable(transformer)
            transformed = transformer.transform(chapter) if structured else chapter
            callback = transformer if callable(transformer) else (lambda text: text)
            self._collect_chapter(transformed, callback, document, pages, replacements, structured)
    finally:
        if document:
            document.close()
    self.patcher.patch(pdf_path, target_path, replacements)
```

注意 [L38-L40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L38-L40) 这三行的分派逻辑，它是用 `callable()` 做鸭子类型判断：

- `ChapterTransformer` 实例只有 `.transform` 方法、不可调用 → `structured=True` → 先 `transform(chapter)` 得到改写后的章，回调退化为恒等函数；
- 普通 `Callable[[str], str]` 函数 → `structured=False` → 章原样传入，函数本身作为文本回调。

`structured` 标志随后控制收集时的跳过条件（见 4.1.3 末尾），带来一个对用户可见的行为差异：

- **callable 模式**：某块的译文与原文相同（没翻译或字典没命中）→ 该块**不进**替换列表 → 原 PDF 上那段字保持原样，不会被白块覆盖。
- **structured / patch 模式**：所有非空 `text` / `sub_title` 块**都进**替换列表 → 整页文字全部被白块覆盖后重绘，即使内容没变。

[pdf_craft/pipeline/pdf/pipeline.py:L47-L73](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L47-L73)——`patch` 入口与 `translate` 结构逐行同构，唯一区别是转换器被硬编码为恒等回调且 `structured=True`，docstring 明确说明「包本身就是替换文本的来源，因此不涉及 OCR 与 LLM 转换器」：

```python
def patch(self, pdf_path, target_path, package) -> None:
    """Write the text already present in ``package`` back to ``pdf_path``. ..."""
    ...
    for chapter in reader():
        self._collect_chapter(
            chapter, lambda text: text, document, pages, replacements, structured=True,
        )
    ...
```

再看门面侧。[pdf_craft/craft.py:L147-L158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L147-L158)——`translate_pdf` 先拒绝 `APPEND_BLOCK`（固定版式页面无法安全追加块级内容），再在临时目录中完成「翻译成新包 → 回写」两段：

```python
def translate_pdf(self, source, package, output, transformer, *, steps=()) -> None:
    for step in steps:
        mode = _step_mode(step, self._as_package_transformer(step))
        if mode == SubmitKind.APPEND_BLOCK:
            raise ValueError("PDF output does not support APPEND_BLOCK")
    with TemporaryDirectory(prefix="pdf-craft-translated-package-") as directory:
        translated = self._translate_for_pdf(package, Path(directory), transformer, steps)
        self.patch_pdf_with_package(source, translated, output)
```

这一拒绝行为被三个测试固化（[tests/test_craft.py:L130-L160](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L130-L160)）：无论 `APPEND_BLOCK` 来自 `TranslationStep`、`ChapterPackageTransformer` 还是自定义包级转换器，都会在**任何文件操作之前**抛 `ValueError`——快败优于半途而废。

[pdf_craft/craft.py:L223-L236](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L223-L236)——`_translate_for_pdf` 把文本回调适配成章级转换器，再走 u7-l1 讲过的 `translate_package`（内部是 `ChapterPackageTransformer` 的复制-改写）：

```python
def _translate_for_pdf(self, package, output_root, transformer, steps) -> DocumentPackage:
    current = package
    if callable(transformer):
        transformer = _TextChapterTransformer(transformer)
    current = self.translate_package(current, output_root / "translated", transformer)
    if steps:
        current = self._apply_steps(current, steps)
    return current
```

[pdf_craft/craft.py:L301-L316](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L301-L316)——`_TextChapterTransformer`：把整块内容摊平成纯文本交给回调，若返回值有变化，就把 `block.content` 整个替换为单一字符串（块内结构被丢弃——这就是文档说它是 replace-only 适配器的原因）：

```python
class _TextChapterTransformer:
    """Adapt the legacy block-text callback to the package transformer shape."""

    def __init__(self, callback: Callable[[str], str]) -> None:
        self._callback = callback

    def transform(self, chapter: Chapter) -> Chapter:
        for layout in chapter.layouts:
            if not isinstance(layout, ParagraphLayout):
                continue
            for block in layout.blocks:
                text = _to_patch_text(block.content)
                translated = self._callback(text)
                if translated != text:
                    block.content = [translated]
        return chapter
```

注意这个类没有 `__call__`，所以在管线的 `callable()` 判断下天然落入 `structured=True` 分支——门面路径由此统一走「先改章、再恒等收集」的路径。

[pdf_craft/craft.py:L160-L172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L160-L172)——`patch_pdf_with_package` 是回写阶段的公开入口：加载（或复用）包、`validate()`、`_validate_package_for_pdf` 预检，然后构造管线并调用 **patch** 方法。管线从 `PDFOptions.pdf_handler` 取 handler（可能为 `None`）：

```python
def patch_pdf_with_package(self, source, package, output) -> None:
    package = package if isinstance(package, DocumentPackage) else DocumentPackage.from_path(Path(package))
    package.validate()
    _validate_package_for_pdf(Path(source), package)
    PDFTranslationPipeline(
        pdf_handler=self._pdf.pdf_handler if self._pdf else None
    ).patch(Path(source), Path(output), package)
```

`patch_pdf_with_package` 与管线的委托关系由 [tests/test_craft.py:L61-L71](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L61-L71) 用 mock 固化：校验函数与 `PDFTranslationPipeline.patch` 各被调用恰好一次。

最后看收集循环本体 [pdf_craft/pipeline/pdf/pipeline.py:L75-L94](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L75-L94)——`_collect_chapter` 是两个入口共用的核心，四段逻辑：筛选布局 → 序列化原文 → 转换与跳过判定 → 补页几何并落替换项：

```python
def _collect_chapter(self, chapter, transformer, document, pages, replacements, structured=False) -> None:
    for layout in chapter.layouts:
        if not isinstance(layout, ParagraphLayout) or layout.ref not in {"text", "sub_title"}:
            continue
        for block in layout.blocks:
            source = _to_patch_text(block.content).strip()
            if not source:
                continue
            translated = transformer(source)
            if not translated or (translated == source and not structured):
                continue
            if block.page_index not in pages:
                if document is None:
                    raise ValueError("PDF handler is required to resolve page dimensions")
                image = document.render_page(block.page_index, self.dpi)
                pages[block.page_index] = image.size
            replacements.append(PDFReplacement(
                block.page_index, block.det, translated, pages[block.page_index], self.dpi,
                reading_order=block.order,
            ))
```

第一行的筛选条件（[L77](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L77)）说明回写的覆盖范围：只有 `ref` 为 `text`（正文段落）或 `sub_title`（标题段落，u3-l4 中 `title` 被归一化而来）的 `ParagraphLayout` 参与替换；`AssetLayout`（图片、表格、公式资源块）与其他 ref 的段落被整体跳过。这与官方文档的说明一致：「It replaces text and subtitle layouts only; tables and images are not translated in place.」（[docs/en/PDF_TRANSLATION.md:L152](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/PDF_TRANSLATION.md#L152)）

#### 4.1.4 代码实践：不画一笔，截获替换列表

`patcher` 是构造函数可注入的，我们塞一个「只记录、不绘制」的替身，就能在完全不依赖 `reportlab`/`pypdf` 的情况下观察管线产出。这个实践需要一个已存在的提取包（可以用 u1-l2 首次转换时用 `package_path` 保留的那个）。

1. **实践目标**：验证 `patch` 入口产出替换列表的过滤规则与字段来源。
2. **操作步骤**：运行以下脚本（示例代码，路径换成你自己的包位置）：

   ```python
   from pathlib import Path
   from pdf_craft.document import DocumentPackage
   from pdf_craft.pipeline.pdf.pipeline import PDFTranslationPipeline

   class RecordingPatcher:
       """替身 patcher：不绘制，只截获替换列表。"""
       def __init__(self):
           self.replacements = []
       def patch(self, source_path, target_path, replacements):
           self.replacements = list(replacements)

   package = DocumentPackage.from_path(Path("work/book-package"))
   recorder = RecordingPatcher()
   pipeline = PDFTranslationPipeline(patcher=recorder)
   pipeline.patch(Path("book.pdf"), Path("ignored.pdf"), package)

   print("替换项总数:", len(recorder.replacements))
   for r in recorder.replacements[:5]:
       print(f"page={r.page_index} bbox={r.bbox} size={r.page_pixel_size} "
             f"dpi={r.dpi} order={r.reading_order} text={r.text[:30]!r}")
   ```

3. **需要观察的现象**：替换项数量与包内 `chapters/chapter_*.xml` 中 `text`/`sub_title` 段落里非空块的数量一致；`bbox` 与章节 XML 中对应块的 `det` 属性一致；`page_pixel_size` 与 `document.json` 中该页的 `page_pixel_sizes` 条目一致。
4. **预期结果**：能打印出每条替换项的六元组字段；由于走的是 `patch` 入口，`text` 就是包内已有文本（未翻译时即原文）。
5. 若本地暂无提取包，此脚本无法运行——「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PDFCraft.translate_pdf` 最终调用的是管线的 `patch` 方法而不是 `translate` 方法？

**答案**：门面在 `_translate_for_pdf` 中已经用 `ChapterPackageTransformer` 把翻译结果落成临时目录下的新包（翻译阶段完成），回写阶段的包内文本就是最终文本，无需再转换；`patch` 入口恰好是「零转换、纯收集」的入口。这也让 `translate_pdf` 与「先 `translate_package`、后 `patch_pdf_with_package`」的积木式组合共享同一条回写代码路径。

**练习 2**：如果用户传入的 `ChapterTransformer` 恰好定义了 `__call__` 方法，管线会发生什么？

**答案**：`callable(transformer)` 返回 `True`，`structured=False`，管线不会调用它的 `.transform(chapter)`，而是把它当作 `Callable[[str], str]` 文本回调逐块调用——与用户意图相反。这是 `callable()` 鸭子类型判断的固有歧义，规避方式是不要给章级转换器加 `__call__`，或直接使用门面（门面内部用 `_TextChapterTransformer` 包装，无此歧义）。

**练习 3**：callable 模式下某段文本的译文与原文完全相同，输出 PDF 上那段字会怎样？

**答案**：`_collect_chapter` 中 `translated == source and not structured` 成立，该块被 `continue` 跳过、不进替换列表；patcher 不会为它画白块，原 PDF 上那段文字原样保留。而 structured / patch 模式下同样的块会被收集，原文被白块覆盖后按原框重绘。

### 4.2 文本序列化：_to_patch_text 为什么保留公式定界符与引用标记

#### 4.2.1 概念说明

块的 `content` 是异构列表：普通字符串、`InlineExpression`（行内公式）、`Reference`（脚注引用）、`HTMLTag`（白名单 HTML 容器）混居。patcher 只会「画文本」，不认识任何结构，所以收集前必须把 `content` 摊平成一个字符串。摊平策略的核心取舍是：

- **公式保留定界符**：`InlineExpression` 的 `content` 是裸 LaTeX（如 `E = mc^2`）。定界符（`$...$`、`\(...\)` 等）告诉读者「这是公式」，剥掉后读者看到的是一串裸符号；所以序列化时要把定界符包回去。
- **引用保留打印标记**：`Reference` 在正文里的形态就是脚注编号（如 `1`），序列化取 `str(item.mark)`，读者在译文中仍能看到「此处有个脚注标记」。
- **HTML 只剥外壳**：`HTMLTag` 是排版容器（`<b>`、`<i>` 之类），白名单过滤在 u6-l2 已经完成，容器本身对「画文本」没有意义，递归保留子内容即可。

docstring 把这条原则总结为「不静默丢弃节点」（without silently dropping nodes）：任何无法归类的成员直接抛 `TypeError`，宁可失败也不悄悄丢内容。

#### 4.2.2 核心流程

```text
_to_patch_text(items):
  对每个 item 分派：
    str              → 原样追加
    InlineExpression → to_markdown_string(kind, content)  # 包回定界符
    Reference        → str(item.mark)                      # 脚注编号
    HTMLTag          → 递归 _to_patch_text(item.children)  # 剥壳保子
    其他             → TypeError（拒绝静默丢弃）
  返回 "".join(parts)
```

注意 `HTMLTag` 分支只递归 `children`、不输出标签本身——与 u6-l2 的 `flatten` 同构，但多了对公式与引用的显式处理。

#### 4.2.3 源码精读

[pdf_craft/pipeline/pdf/pipeline.py:L97-L115](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L97-L115)——序列化器本体，docstring 明确写出三保留原则：

```python
def _to_patch_text(items) -> str:
    """Serialize structured Chapter content without silently dropping nodes.

    The patcher can only draw text, so formulas retain their Markdown delimiters,
    references retain their printed mark, and HTML wrappers retain their children.
    """
    parts: list[str] = []
    for item in items:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, InlineExpression):
            parts.append(to_markdown_string(item.kind, item.content))
        elif isinstance(item, Reference):
            parts.append(str(item.mark))
        elif isinstance(item, HTMLTag):
            parts.append(_to_patch_text(item.children))
        else:
            raise TypeError(f"unsupported chapter content for PDF patching: {type(item).__name__}")
    return "".join(parts)
```

这个函数不只是管线私用——门面在 [pdf_craft/craft.py:L25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L25) 显式导入它（`from .pipeline.pdf.pipeline import _to_patch_text`），供 `_TextChapterTransformer` 在把块交给文本回调前摊平内容。也就是说，**你写的 `Callable[[str], str]` 收到的字符串就是 `_to_patch_text` 的输出**——公式带着 `$` 定界符、脚注位置带着编号，翻译时需要原样保留这些记号。

[pdf_craft/expression.py:L51-L60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py#L51-L60)——`to_markdown_string` 按 `ExpressionKind` 的四种定界风格包装：

```python
def to_markdown_string(kind: ExpressionKind, content: str) -> str:
    if kind == ExpressionKind.INLINE_DOLLAR:
        return "$" + content + "$"
    elif kind == ExpressionKind.DISPLAY_DOUBLE_DOLLAR:
        return "$$" + content + "$$"
    elif kind == ExpressionKind.INLINE_PAREN:
        return "\\(" + content + "\\)"
    elif kind == ExpressionKind.DISPLAY_BRACKET:
        return "\\[" + content + "\\]"
```

为什么四种风格要原样区分、而不是统一成一种？因为定界风格是 OCR 阶段从原文里识别出来的事实（行内美元式、行间双美元式……），序列化的目标是忠实还原「这块内容在原书里的呈现方式」，交给文本翻译器后也便于用正则识别「这段不要翻译」。

数据来源一侧，[pdf_craft/extractor/chapter/chapter.py:L60-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L60-L65) 是 `BlockLayout` 的四个字段——它们与 `PDFReplacement` 的字段一一对应，是理解「替换项从何而来」的钥匙：

```python
@dataclass
class BlockLayout:
    page_index: int
    order: int
    det: tuple[int, int, int, int]
    content: list[str | BlockMember | HTMLTag[BlockMember]]
```

对照 [pdf_craft/pipeline/pdf/patcher.py:L11-L18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L11-L18) 的 `PDFReplacement`：

```python
@dataclass(frozen=True)
class PDFReplacement:
    page_index: int
    bbox: tuple[int, int, int, int]
    text: str
    page_pixel_size: tuple[int, int]
    dpi: int = 300
    reading_order: int = 0
```

对应关系整理成表：

| PDFReplacement 字段 | 来源 | 说明 |
|---|---|---|
| `page_index` | `block.page_index` | 块所在页，1 起始 |
| `bbox` | `block.det` | OCR 检测框，OCR 位图像素坐标 |
| `text` | `transformer(source)` 或包内文本 | `source` 即 `_to_patch_text(block.content).strip()` |
| `page_pixel_size` | `pages[block.page_index]` | `document.json` 页几何，或兜底渲染的 `image.size` |
| `dpi` | 管线的 `self.dpi` | 缺省 300，兼作兜底渲染与背景图分辨率 |
| `reading_order` | `block.order` | 页内阅读序号，patcher 按它排序绘制 |

#### 4.2.4 代码实践：离线观察摊平规则

不需要任何 PDF 或包，直接构造异构 content 列表调用序列化器。

1. **实践目标**：亲眼验证四类成员各自的序列化产物。
2. **操作步骤**（示例代码）：

   ```python
   from pdf_craft.pipeline.pdf.pipeline import _to_patch_text
   from pdf_craft.extractor.chapter.chapter import InlineExpression, Reference
   from pdf_craft.expression import ExpressionKind
   from pdf_craft.markdown.paragraph import HTMLTag
   from pdf_craft.markdown.paragraph.tags import HTML_B

   items = [
       "The value is ",
       InlineExpression(kind=ExpressionKind.INLINE_DOLLAR, content="E = mc^2"),
       " (see note ",
       Reference(page_index=3, order=1, mark="1", layouts=[]),
       ") and ",
       HTMLTag(definition=HTML_B, attributes=[], children=["bold text"]),
   ]
   print(_to_patch_text(items))

   # 反例：不支持的营养类型
   try:
       _to_patch_text([42])
   except TypeError as e:
       print("TypeError:", e)
   ```

3. **需要观察的现象**：公式被包回 `$...$`；引用变成编号字符 `1`；`HTMLTag` 只留下子文本、没有 `<b>` 标签；整数成员触发 `TypeError`。
4. **预期结果**：输出形如 `The value is $E = mc^2$ (see note 1) and bold text`，随后打印 `TypeError: unsupported chapter content for PDF patching: int`。此脚本可直接本地运行验证。
5. `HTML_B` 是 [pdf_craft/markdown/paragraph/tags.py:L288](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L288) 导出的真实标签定义，用它构造的 `HTMLTag` 与章节 XML 解码出的对象同构。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Reference` 序列化为 `str(item.mark)` 而不是把脚注全文（`layouts`）也画进正文框？

**答案**：`Reference.mark` 是它在正文中的打印形态（脚注编号），`layouts` 里才是脚注全文。替换发生在正文检测框 `block.det` 内，空间只够放编号；把全文塞进去会溢出框外。脚注区本身的回写不在本管线覆盖范围内（`_collect_chapter` 只收集 `text`/`sub_title` 段落）。

**练习 2**：你的文本翻译回调收到 `"质量守恒 $E=mc^2$ 见注 3"` 这样的字符串，回调应该怎么处理 `$...$` 与末尾编号？

**答案**：应原样保留。`$E=mc^2$` 是 `_to_patch_text` 包回的公式定界符，译掉它会破坏公式；`3` 是 `Reference` 的打印标记，改掉它会让译文与脚注对不上。实践中常用正则把公式片段（`\$[^$]*\$` 与 `\\(...\\)`、`\\[...\\]`）切出来只译其余部分。

**练习 3**：`_to_patch_text` 对未知类型抛 `TypeError`，而 u6-l2 的 Markdown 解析器对解析失败「一律退回字面文本、永不抛错」。为什么两处策略不同？

**答案**：场景不同。Markdown 解析器处理的是不可信的 OCR 外部输入，抛错会让整条转换链瘫痪，降级更安全；`_to_patch_text` 处理的是库内已类型化的内存对象，出现未知类型意味着上游数据模型变了（版本不兼容或自定义转换器塞入了新类型），静默丢弃会造成内容悄悄缺失，快败更能暴露问题。

### 4.3 页几何兜底：page_pixel_sizes 与 PDF handler 渲染

#### 4.3.1 概念说明

`PDFReplacement` 同时携带 `bbox`（像素坐标的框）和 `page_pixel_size`（该页位图的总尺寸）。后者看似冗余——知道框不知道页大小不行吗？——其实是坐标换算的必需品：patcher 需要把像素框换算成 PDF 点坐标，换算比例是「页面总宽（点）÷ 页面总宽（像素）」，没有 `page_pixel_size` 就算不出比例（见 4.3.3 的 `_box_in_points`）。

页几何的**第一来源**是 `document.json`：提取阶段第四步 `write_metadata` 把每页 OCR 位图尺寸写进 `page_pixel_sizes`（u6-l1 讲过）。但存在边界情况：包是手工拼的、`document.json` 缺失或漏了某页。管线的兜底策略是：**缺哪页，就用 PDF handler 现场渲染哪页，取位图的 `image.size` 当页几何**。渲染需要 `pdf_handler`；若调用者连 handler 都没给，就只能抛 `ValueError`。

而门面路径在这之前还有一道**预检** `_validate_package_for_pdf`：页几何缺失直接拒绝，根本不给兜底机会。「预检优先于兜底」是分层设计——门面对普通用户快速给出可修复的明确报错；兜底留给直接操作管线、明确知道自己为什么缺几何的高级调用者。

#### 4.3.2 核心流程

```text
_collect_chapter 对每个待收集块：
  if block.page_index in pages:        # document.json 已有该页几何
      直接使用
  else:                                 # 缺几何 → 兜底
      if document is None:              # 没注入 pdf_handler
          raise ValueError("PDF handler is required to resolve page dimensions")
      image = document.render_page(block.page_index, self.dpi)   # 现场渲染
      pages[block.page_index] = image.size                        # 记入本页表（内存态，不回写 document.json）
  追加 PDFReplacement(..., pages[block.page_index], self.dpi, ...)

门面预检 _validate_package_for_pdf（在兜底之前执行）：
  1. pypdf 可导入？否则 RuntimeError
  2. page_pixel_sizes 为空 → ValueError（缺页几何元数据）
  3. 页码超出源 PDF 页数 → ValueError
  4. 章节页不在 page_pixel_sizes 中 → ValueError（missing）
```

补进 `pages` 的兜底几何只存在于本次运行的内存字典中，不会写回 `document.json`——兜底是运行时补丁，不是持久化修复。

#### 4.3.3 源码精读

[pdf_craft/pipeline/pdf/pipeline.py:L86-L90](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L86-L90)——兜底分支本体，位于追加替换项之前：

```python
if block.page_index not in pages:
    if document is None:
        raise ValueError("PDF handler is required to resolve page dimensions")
    image = document.render_page(block.page_index, self.dpi)
    pages[block.page_index] = image.size
```

`document` 来自入口处的 `self.pdf_handler.open(pdf_path) if self.pdf_handler else None`（[L32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L32) / [L61](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L61)），`render_page` 是 u3-l2 讲过的 `PDFDocument` 协议方法。注意渲染用 `self.dpi`（管线构造参数，默认 300），且同一页只渲染一次（结果记进 `pages` 后后续块直接命中）。

[pdf_craft/document/package.py:L52-L57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L52-L57)——页几何的第一来源：

```python
def page_pixel_sizes(self) -> dict[int, tuple[int, int]]:
    """Return OCR canvas sizes recorded by the Extractor, without OCR cache."""
    if self.metadata_path is None or not self.metadata_path.exists():
        return {}
    payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
    return self._parse_page_pixel_sizes(payload)
```

`document.json` 不存在时返回空字典——此时所有页都走兜底分支。

[pdf_craft/craft.py:L319-L347](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L319-L347)——门面预检。docstring 点明意图：「包几何无法匹配 PDF 时，在 patching 之前失败」。四道关卡中第三、四道直接对应兜底场景：

```python
def _validate_package_for_pdf(source: Path, package: DocumentPackage) -> None:
    """Fail before patching when package geometry cannot match the PDF."""
    try:
        import pypdf
    except ImportError as error:
        raise RuntimeError("PDF patching requires the optional 'pypdf' dependency") from error
    page_sizes = package.page_pixel_sizes()
    if not page_sizes:
        raise ValueError("DocumentPackage is missing page geometry metadata required for PDF patching")
    ...
    missing = sorted(page for page in chapter_pages if page not in page_sizes)
    if missing:
        raise ValueError(
            "DocumentPackage is missing page geometry for chapter pages: "
            f"{missing}"
        )
```

（中间省略的部分用 `pypdf.PdfReader` 数出源 PDF 页数，把「页码超界」也拦下。）由于预检要求章节每一页都有几何，**门面路径下 4.3.2 的兜底分支实际不可达**——缺几何会在 `patch_pdf_with_package` 里先抛错。兜底真正服务的是绕过门面、直接构造 `PDFTranslationPipeline(pdf_handler=...)` 的调用者。

页几何的最终去向是坐标换算。[pdf_craft/pipeline/pdf/patcher.py:L153-L161](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L153-L161)——`_box_in_points` 用页面总宽比把像素框换算成点坐标，并翻转 y 轴（图像原点在左上、PDF 原点在左下）：

```python
@staticmethod
def _box_in_points(replacement: PDFReplacement, width: float, height: float) -> tuple[float, float, float, float]:
    pixel_width, pixel_height = replacement.page_pixel_size
    left, top, right, bottom = replacement.bbox
    scale_x = width / pixel_width
    scale_y = height / pixel_height
    x = left * scale_x
    y = height - bottom * scale_y
    return x, y, (right - left) * scale_x, (bottom - top) * scale_y
```

换算写成公式（\(W\)、\(H\) 为页面点尺寸，\(w_p\)、\(h_p\) 为位图像素尺寸）：

\[
x = \text{left} \cdot \frac{W}{w_p}, \qquad
y = H - \text{bottom} \cdot \frac{H}{h_p}
\]

用「总宽比」而不是 \(\frac{72}{\text{dpi}}\) 的好处是自洽：即便包内记录的 `dpi` 与位图实际渲染参数有出入，只要 `bbox` 与 `page_pixel_size` 出自同一次渲染（它们确实出自同一次 OCR 渲染），框就落在正确的相对位置上。此外 patcher 还会用替换项的 `dpi` 渲染背景页图（[patcher.py:L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L114) `render_dpi = page_layouts[0][0].dpi if page_layouts else self.dpi`），并按 `reading_order` 排序同页替换项（[L83-L84](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L83-L84)）——这两处的细节留待下一讲展开。

最后，`PDFReplacement` 在进入绘制前还要过 [patcher.py:L134-L147](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L134-L147) 的 `validate`：页码为正、不超总页数、bbox 四元组合法且**不超出 `page_pixel_size`**、文本非空、页尺寸为正。这组断言反过来解释了收集端为什么必须保证 `bbox` 与 `page_pixel_size` 出自同一坐标系——两者一旦错位，这里会立即暴露。

#### 4.3.4 代码实践：制造缺几何，观察兜底与预检

1. **实践目标**：分别触发「兜底渲染」与「无 handler 报错」，并验证门面预检会先一步拦截。
2. **操作步骤**：
   - 复制一个现成提取包：`cp -r work/book-package work/book-package-no-geo`；
   - 用文本编辑器打开 `work/book-package-no-geo/document.json`，从 `page_pixel_sizes` 里删掉某一页（比如键 `"5"`）的条目；
   - 直接用管线（绕过门面）跑 4.1.4 的脚本但换个包路径，且分别加与不加 `pdf_handler`：

     ```python
     from pdf_craft.pdf.handler import DefaultPDFHandler
     # 带 handler：缺的那页会现场渲染兜底
     pipeline = PDFTranslationPipeline(patcher=recorder, pdf_handler=DefaultPDFHandler())
     # 不带 handler：
     pipeline = PDFTranslationPipeline(patcher=recorder)
     ```

   - 再换门面入口试同一个缺几何的包：`PDFCraft().patch_pdf_with_package("book.pdf", "work/book-package-no-geo", "out.pdf")`。
3. **需要观察的现象**：
   - 管线 + handler：脚本报错消失，`recorder.replacements` 里第 5 页各块的 `page_pixel_size` 来自现场渲染的位图尺寸（可能与 `document.json` 里其他页的尺寸略有出入，取决于同一 dpi 下的渲染一致性）；
   - 管线无 handler：抛 `ValueError: PDF handler is required to resolve page dimensions`；
   - 门面入口：抛 `ValueError: DocumentPackage is missing page geometry for chapter pages: [5]`，**不会**走到兜底。
4. **预期结果**：三种情形分别命中 4.3.3 的三段源码（兜底分支、兜底分支的报错行、`_validate_package_for_pdf` 的 missing 检查）。
5. 需要真实的 PDF 与提取包才能运行——「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：兜底渲染得到的页几何会写回 `document.json` 吗？这个设计有什么含义？

**答案**：不会。`pages` 是 `_collect_chapter` 调用链里的局部字典，兜底结果只活在本次运行。含义是：兜底是「运行时补丁」而非「持久化修复」，下次运行同一包还会再渲染一次；要永久修复应补全 `document.json` 的 `page_pixel_sizes`。

**练习 2**：`_box_in_points` 用 `height - bottom * scale_y` 计算 y，而不是 `top * scale_y`，为什么？

**答案**：两套坐标系 y 轴方向相反——图像原点在左上、y 向下；PDF 原点在左下、y 向上。替换框在 PDF 中的「底边」对应图像中的 `bottom`；把图像 bottom 换算成点尺度后从页高里减去，得到 PDF 坐标系下框左下角的 y。`reportlab` 的文本绘制锚点也在左下角坐标系中，这样计算可以直接使用。

**练习 3**：既然有兜底，门面为什么还要 `_validate_package_for_pdf` 这道预检？

**答案**：兜底要求注入 `pdf_handler`，且逐页现场渲染开销不小；更重要的是缺几何往往意味着「包与 PDF 不匹配」（比如包来自另一份文档），继续跑会产出页码错乱的 PDF。预检用四道明确的 `ValueError` 把问题挡在文件操作之前，符合本仓库一贯的「快败优于半途而废」（与 `translate_pdf` 预拒 `APPEND_BLOCK`、patcher 预检排版是同一哲学）。

## 5. 综合实践

把本讲三个模块串起来：提取 → 字典翻译 → 回写，并对照源码说清每个 `PDFReplacement` 的字段来源。

**任务**：对一个英文小 PDF（可用 `tests/assets/` 下的资产，或任意几页的英文文档）完成「翻译 PDF」全流程，翻译器用硬编码字典。

1. **准备**：安装 pdf-craft 与 Poppler（u1-l2），准备 DeepSeek OCR vendor 凭据。
2. **编写脚本**（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft import PDFCraft, PDFOptions, ExtractionOptions
   from pdf_craft.ocr_config import DeepSeekOCRVendorConfig

   DICT = {
       "introduction": "引言",
       "conclusion": "结论",
       "the": "这", "and": "和", "system": "系统",
   }

   def translate_text(text: str) -> str:
       # 教学用简化字典：整段命中才翻译，演示 callable 入口的行为
       return DICT.get(text.strip().lower(), text)

   craft = PDFCraft(pdf=PDFOptions(
       ocr=DeepSeekOCRVendorConfig(
           base_url="https://api.deepseek.com/v1",  # 以你的 OCR 服务为准
           api_key="sk-...",
           model="...",
       ),
   ))
   package = craft.extract_pdf(
       "book.pdf", "work/book-package",
       options=ExtractionOptions(includes_footnotes=True),
   )
   craft.translate_pdf("book.pdf", package, "book.zh.pdf", translate_text)
   ```

3. **观察产物**：打开 `book.zh.pdf`——只有整段文本恰好是字典键（如纯 "Introduction" 标题块）的区域被替换成中文并画上白块；其余未命中的块保持原样。这印证了 4.1 讲的 callable 模式跳过规则。
4. **对照源码回答字段来源**（本讲核心问题）：
   - `bbox` ← `block.det`：OCR 阶段记录的检测框，在 [pipeline.py:L91-L94](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L91-L94) 装配进替换项；
   - `page_pixel_size` ← `pages[block.page_index]`：优先来自 `document.json` 的 `page_pixel_sizes`（[package.py:L52-L57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L52-L57)），缺页时由 `document.render_page(...).size` 兜底（[pipeline.py:L86-L90](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L86-L90)）；
   - 两者最终被 [patcher.py:L153-L161](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L153-L161) 的 `_box_in_points` 换算为 PDF 点坐标。
5. **加一步验证**（可选）：把 4.1.4 的 `RecordingPatcher` 思路搬过来——用 `craft.translate_pdf` 之前先手工跑一遍 `PDFTranslationPipeline.translate("book.pdf", "ignored.pdf", package, translate_text)`（替身 patcher 注入），确认同一字典下 `translate` 入口与门面路径收集到相同的替换集合。
6. 若本地没有 OCR 凭据或英文 PDF，步骤 2-5 无法运行——「待本地验证」；此时可退而执行 4.2.4 的离线序列化实践（零依赖）。

## 6. 本讲小结

- `PDFTranslationPipeline` 的职责是把 DocumentPackage 变成一列 `PDFReplacement`：`translate` 入口自带转换器（callable 或 `ChapterTransformer`，用 `callable()` 分派），`patch` 入口零转换、纯收集包内已有文本。
- 门面 `PDFCraft.translate_pdf` 实际走「`_TextChapterTransformer` 包装 → `ChapterPackageTransformer` 复制-改写出翻译包 → `patch_pdf_with_package` 走 patch 入口」的两段式路径，并在任何文件操作前拒绝 `APPEND_BLOCK`。
- 收集只覆盖 `ref` 为 `text` / `sub_title` 的段落块；callable 模式下译文与原文相同的块被跳过（原文保留），structured / patch 模式下全部非空块都被重绘。
- `_to_patch_text` 摊平块内容时三保留一剥壳：公式保留 Markdown 定界符（`to_markdown_string` 四种风格）、脚注引用保留打印编号、HTML 剥壳递归保子，未知类型抛 `TypeError` 拒绝静默丢弃。
- `PDFReplacement.bbox` 来自 OCR 检测框 `block.det`，`page_pixel_size` 优先来自 `document.json` 的 `page_pixel_sizes`，缺页时用 PDF handler 按管线 `dpi`（默认 300）现场渲染兜底；门面的 `_validate_package_for_pdf` 预检让普通用户在兜底之前就拿到明确报错。
- 像素坐标到 PDF 点坐标的换算在 patcher 的 `_box_in_points` 用「页面总宽比 + y 轴翻转」完成，`bbox` 与 `page_pixel_size` 必须出自同一次渲染才能对位。

## 7. 下一步学习建议

替换列表收集完之后，剩下的问题全是「怎么画」：下一讲 **u10-l2 PDFPatcher：pypdf + reportlab 叠层回写** 将精读 [pdf_craft/pipeline/pdf/patcher.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py) 的 `patch` 方法——pypdf 保留页面、reportlab 画白块与透明文字叠层、preflight「先全部排版再落盘」、溢出替换进入 `skipped_replacements`。再往后的 u10-l3 讲 `BoxTextLayout` 如何在框内自适应字号。如果你对「替换项从哪来」还想看更多真实调用样例，推荐回看 [tests/test_pdf_patcher.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py) 中构造 `PDFReplacement` 的方式，它展示了各字段的最小合法取值。
