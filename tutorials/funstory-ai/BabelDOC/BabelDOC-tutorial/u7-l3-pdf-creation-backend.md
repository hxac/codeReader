# PDF 生成后端：PDFCreater

## 1. 本讲目标

本讲是 BabelDOC 三段式流水线的**收官篇**：前面 u7-l1（排版）把译文贴着原版面重排回 IL，u7-l2（字体映射）为译文字符选好了能渲染的字体，本讲要回答最后一个问题——

> 这棵已经被翻译、重排、加好字体的 IL 对象树，**最终是怎么变回一个真正的 PDF 文件的？**

读完本讲，你应当能够：

1. 说清 `PDFCreater` 如何把 IL 中的字符 / 表单 / 矩形 / 曲线转成「渲染单元」，并按 `render_order` 排序后写成 PDF 内容流。
2. 理解「字体子集化（subset font）」为什么能让产物 PDF 变小，以及 `reproduce_cmap` 如何用 FreeType 复原 ToUnicode 映射、保证译文可被复制搜索。
3. 掌握 `save_pdf_with_timeout` 的「子进程 + 超时 + 回退」三段式保护模式。
4. 区分 mono（单语）与 dual（双语）PDF 的生成差异，特别是 `use-alternating-pages-dual`（交替页）与默认并排（side-by-side）两种双语布局。

本讲对应 midend 流水线最后的三个 stage：`Generate drawing instructions`、`Subset font`、`Save PDF`。

---

## 2. 前置知识

本讲假定你已掌握前置讲义的下列概念，这里只做一句话回顾，不展开：

- **IL（中间表示）**：一棵带坐标的对象树，根是 `Document`，页是 `Page`，其下并列挂着 `pdf_character` / `pdf_paragraph` / `pdf_curve` / `pdf_form` / `pdf_rectangle` 等集合（见 u3-l1）。
- **三段式架构**：frontend 把 PDF 解析成 IL，midend 原地加工 IL，backend 把加工后的 IL 渲染回 PDF（见 u2-l1）。`PDFCreater` 就是 backend。
- **PdfCharacter 的关键字段**：`char_unicode`（字形对应的 Unicode）、`pdf_character_id`（这个字在 PDF 字体里的**字形编码**，渲染时要用它而不是 Unicode）、`box`（坐标）、`pdf_style`（字号 / font_id / 颜色等）、`xobj_id`（这个字属于页面还是某个 XObject 表单，见 u4-l3）。
- **FontMapper**：为每个字符选出能渲染该字符的运行时字体，并把 ascent / descent / encoding_length 等度量写回 IL 的 `PdfFont`（见 u7-l2）。
- **PDF 内容流**：一串「操作数 + 操作符」的栈式绘画指令，比如 `BT /F1 12 Tf 1 0 0 1 100 700 Tm <0041> Tj ET` 表示「开始文本、选 F1 号字体 12 号、平移到 (100,700)、画编码为 0x0041 的字形、结束文本」（见 u4-l3）。
- **TRANSLATE_STAGES**：`high_level.py` 里的流水线「节目单」，本讲的三个 stage 就登记在其中。

如果你对「字形编码 `pdf_character_id` 与 Unicode `char_unicode` 的区别」还不清楚，建议先回看 u4-l3 的字形展开部分——这是理解本讲字符渲染的钥匙。

---

## 3. 本讲源码地图

本讲涉及两个核心源码文件：

| 文件 | 作用 |
| --- | --- |
| [babeldoc/format/pdf/document_il/backend/pdf_creater.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py) | backend 主体。定义渲染单元体系、`RenderContext`、字体子集化、`save_pdf_with_timeout`、`PDFCreater.write` 总编排。 |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 编排层。`TRANSLATE_STAGES` 登记本讲三个 stage；`only_parse_generate_pdf` 跳过翻译直达 backend；水印首页的生成与合并也在这里调用 `PDFCreater`。 |

此外还会旁征以下文件用于印证细节：

- `babeldoc/format/pdf/translation_config.py`：定义 `WatermarkOutputMode` 枚举与 `TranslateResult` 结果对象。
- `babeldoc/format/pdf/document_il/midend/detect_scanned_file.py`：复用 `update_page_content_stream(..., skip_char=True)` 做扫描件检测，是 backend 被 midend 反向调用的一个有趣案例（见 u5-l1）。

---

## 4. 核心概念与源码讲解

### 4.1 渲染单元：从 IL 对象到 PDF 绘画指令

#### 4.1.1 概念说明

`PDFCreater` 的核心任务，是把 IL 里那一堆「带坐标的对象」翻译成 PDF 能看懂的**内容流绘画指令**。但 IL 里有字符、段落、公式、曲线、矩形、表单……种类繁多，每种的画法都不一样。

为了不让 `write` 变成一坨巨大的 if-else，BabelDOC 用了**渲染单元（Render Unit）**这个抽象：

- 每一类可绘制对象，都包装成一个「渲染单元」。
- 所有渲染单元都实现同一个接口：给我一个字节流 `draw_op` 和一个上下文 `context`，我把自己「画」进去。
- 画之前，先按统一的「绘制顺序」排序，保证**底层的先画、上层的后画**（先画背景矩形，再画曲线，再画图，最后画文字，文字就不会被盖住）。

这是一个典型的**策略模式 + 模板方法**组合：策略模式把「怎么画」的差异下沉到各单元，模板方法把「收集 → 排序 → 逐个 render → 写回内容流」的骨架留在 `PDFCreater`。

#### 4.1.2 核心流程

```
对每一页 Page：
  1. 收集渲染单元（create_render_units_for_page）
       字符：page.pdf_character ＋ 段落展开后的字符（render_paragraph_to_char）
             → CharacterRenderUnit，默认 render_order=100
       表单：page.pdf_form ＋ 公式里的表单
             → FormRenderUnit，默认 render_order=50
       矩形：仅 OCR workaround 或 debug 时
             → RectangleRenderUnit，默认 render_order=10
       曲线：page.pdf_curve ＋ 公式里的曲线（仅 debug / passthrough_paint）
             → CurveRenderUnit，默认 render_order=20
  2. 排序（render_units_to_stream）
       按 (render_order, sub_render_order) 升序
       ↓ 于是绘制层次天然为：矩形(10) < 曲线(20) < 表单(50) < 字符(100)
  3. 逐个 render 到对应的 BitStream
       若单元带 xobj_id → 画进 xobj_draw_ops[xobj_id]（表单内部）
       否则              → 画进 page_op（页面主内容流）
  4. 把 page_op 写成一个新的内容流对象，挂到该页（set_contents）
```

#### 4.1.3 源码精读

渲染单元的抽象基类定义了三个东西：绘制顺序 `render_order` / `sub_render_order`、归属 `xobj_id`、抽象方法 `render`、以及排序键 `get_sort_key`：

[backend/pdf_creater.py:40-68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L40-L68) —— `RenderUnit` 抽象基类：`render_order` 为 `None` 时兜底成一个极大值（排到最后），排序键就是 `(render_order, sub_render_order)` 这个二元组。

最关键的是 `CharacterRenderUnit.render`，它把一个 `PdfCharacter` 翻译成一段 PDF 文本操作符：

[backend/pdf_creater.py:83-135](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L83-L135) —— 字符渲染。读这段代码要抓住四个要点：

1. **跳过空字符**：`char_unicode == "\n"` 或 `pdf_character_id is None`（哑字符）直接 return，不画。
2. **算字号**：调 `_font_size_for_pdf_tf`，对 Type3 字体要用 `inverse_type3_font_size_for_tf` 反算回 PDF 的 `Tf` 字号（因为 Type3 字体的「字号」语义和普通字体不同，见 u7-l2 字体度量）。
3. **拼定位矩阵 `Tm`**：横排字符用 `1 0 0 1 x y Tm`（平移到字符 box 左下角），纵排字符（`char.vertical`）用 `0 1 -1 0 x2 y Tm`（旋转 90°）。注意它画的是 `BT ... Tf ... Tm ... Tj ET` 这套标准 PDF 文本序列。
4. **写字形编码而非 Unicode**：`f"<{char.pdf_character_id:0{encoding_length * 2}x}>"`。这里 `encoding_length` 是该字体的编码宽度（字节数），`*2` 是因为一个字节用两个十六进制字符表示。最终 `Tj` 操作符画的是 `<字形编码的十六进制>`，**不是** `<Unicode>`——这正是 u4-l3 强调的：PDF 渲染靠字形编码，Unicode 只用于复制搜索。

`render_paragraph_to_char` 负责把段落「拍扁」成字符序列，是连接 u6 翻译产物与本讲渲染的桥梁：

[backend/pdf_creater.py:810-837](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L810-L837) —— 遍历段落的 `pdf_paragraph_composition`：若是普通字符就收下；若是公式就把公式内所有字符（`pdf_formula.pdf_character`）摊平收下。译文段落的字符就是经此进入渲染的。

四个单元的默认绘制顺序在收集函数里设定，注意矩形 / 曲线 / 表单都有各自的 gate（开关）：

[backend/pdf_creater.py:839-923](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L839-L923) —— `create_render_units_for_page`。注意矩形只在「OCR workaround 且填充背景」或「debug」时才生成单元；曲线只在 `passthrough_paint` 或 debug 时才生成——正常翻译产物里，背景图形走的是 u5-l4 那套「透传指令」而非这里重画。

排序与分流是 `render_units_to_stream` 的全部职责：

[backend/pdf_creater.py:925-944](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L925-L944) —— 先 `sorted(units, key=get_sort_key)`，再按 `xobj_id` 决定画进页面主流还是某个 XObject 的子流。一个字如果属于某个表单（`xobj_id` 非空），它的绘画指令必须落在那个表单的内容流里，坐标系才对。

#### 4.1.4 代码实践

**实践目标**：亲手看清「IL 字符 → PDF 文本操作符」的转换。

**操作步骤**：

1. 打开 [backend/pdf_creater.py:116-120](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L116-L120)，找到横排字符拼出的模板字符串 `f"BT /{font_id} {tf_font_size:f} Tf 1 0 0 1 {char.box.x:f} {char.box.y:f} Tm "`。
2. 假设一个字符：`font_id="F1"`、`tf_font_size=12`、`box.x=100`、`box.y=700`、`pdf_character_id=65`、字体 `encoding_length=2`。
3. 手工代入，预测 `render` 会往 `draw_op` 里追加的两段字节分别是什么。

**需要观察的现象**：你应该得到类似这样的完整指令序列：

```
q
BT /F1 12.000000 Tf 1 0 0 1 100.000000 700.000000 Tm
<0041> Tj ET Q
```

**预期结果**：`<0041>` 正是 `pdf_character_id=65` 按 `encoding_length*2=4` 位十六进制补零的结果（`65` 的十六进制是 `41`，补零成 `0041`）。注意它是**字形编码 0x0041**，恰好和 Unicode `A`(0x41) 数值相同纯属巧合——换个 CID 字体，这个编码就完全不是 Unicode 了。

> 待本地验证：以上为据源码推导的结果，未实际运行；可用 `--debug` 翻译后查看产物 PDF 解压出的内容流比对。

#### 4.1.5 小练习与答案

**练习 1**：为什么渲染单元要先按 `render_order` 排序，而不是按 IL 里出现的先后顺序直接画？

**参考答案**：因为 PDF 内容流是**后画的覆盖先画的**（画家算法）。要让文字始终在最上层不被背景图形遮挡，就必须保证背景（矩形、曲线）先画、图（表单）次之、文字最后画。`render_order` 默认值矩形(10) < 曲线(20) < 表单(50) < 字符(100) 正是实现这一层次。IL 里的存储顺序与视觉层次无关，不能直接用。

**练习 2**：一个 `char.xobj_id="X1"` 的字符，它的绘画指令会被写到哪里？为什么不能写到页面主流？

**参考答案**：会被写到 `xobj_draw_ops["X1"]`，即 XObject `X1` 的内容流里。因为该字符的 `box` 坐标是**相对于表单 `X1` 的局部坐标系**的，只有画在 `X1` 内部，经表单的变换矩阵映射后位置才正确；若画到页面主流，坐标会错位。

---

### 4.2 字体子集化与 ToUnicode CMap 复原

#### 4.2.1 概念说明

u7-l2 的 `FontMapper` 给译文挂上了完整的字体文件（比如一个几 MB 的中文字体）。但一篇论文可能只用到这个字体里的几百个字，把整个字体塞进 PDF 既浪费体积，也不专业。

**字体子集化（font subsetting）**就是：扫描 PDF 实际用了哪些字形，把字体文件裁剪到**只保留用到的字形**，大幅缩小产物体积。这是几乎所有专业 PDF 工具都会做的事。

但子集化有一个副作用：它可能破坏字体的 **ToUnicode CMap**（字形编码 → Unicode 的映射表）。ToUnicode 不影响**显示**（显示靠字形轮廓），但影响**复制、搜索、无障碍**——如果它坏了，读者从译文 PDF 里复制出来的就是乱码。所以 BabelDOC 在子集化之外，还专门用 `reproduce_cmap` 把自己加进去的字体的 ToUnicode **重新生成一遍**，保证译文可被正确复制。

> 术语澄清：
> - **字形（glyph）**：字体里一个具体的图形轮廓，有 `glyph id`（gid）。
> - **字形编码（character code）**：PDF 内容流里 `Tj` 画的那个十六进制码（即上节的 `pdf_character_id`）。
> - **ToUnicode CMap**：把「字形编码」映射到「Unicode」的表，专供复制 / 搜索用，与显示无关。

#### 4.2.2 核心流程

`reproduce_cmap` 对 BabelDOC 自己加入的每个字体，做「读旧映射 + 扫真字形 + 造新映射」三步：

```
reproduce_cmap(doc):
  遍历所有页，收集「BabelDOC 自带字体」(font 名 ∈ FONT_NAMES 且 .ttf)
  对每个这样的字体 reproduce_one_font(doc, xref):
    1. 读 ToUnicode 流 → parse_tounicode_cmap → cmap: {gid: unicode}
    2. 读 FontFile2（TrueType 字体字节）→ parse_truetype_data → used: 用到的 gid 列表
       （用 FreeType 逐字形加载，只保留 outline.contours 非空者）
    3. make_tounicode(cmap, used) → 只为「用到且能查到 Unicode」的字形生成新 CMap
    4. update_stream 写回 ToUnicode 流
```

子集化本身则更直接：

```
subset_fonts_in_subprocess(pdf):
  子进程内：pdf.subset_fonts(fallback=False) → save → os._exit(0)
```

#### 4.2.3 源码精读

用 FreeType 扫描 TrueType 字体，找出「真正有轮廓、被使用」的字形：

[backend/pdf_creater.py:476-483](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L476-L483) —— `parse_truetype_data`：用 `freetype.Face` 加载字体字节，遍历所有 `num_glyphs` 个字形，`load_glyph(i)` 后若 `outline.contours` 非空就认为它在用。这一步是「子集保留谁」的事实来源。

`reproduce_one_font` 把「旧 ToUnicode 映射」和「真用到的字形」拼成新 ToUnicode：

[backend/pdf_creater.py:524-536](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L524-L536) —— 关键四行：`ms`(ToUnicode 流) 经 `parse_tounicode_cmap` 得 `cmap`；`fs`(FontFile2 流) 经 `parse_truetype_data` 得 `used`；`make_tounicode(cmap, used)` 生成新文本；`update_stream` 写回。注意它只处理同时满足 `ToUnicode` 是 xref、`DescendantFonts` 是 array 的字体（即标准的 Type0/CID 字体）。

筛选「只处理 BabelDOC 自带字体」的守卫在 `reproduce_cmap`：

[backend/pdf_creater.py:539-552](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L539-L552) —— 条件 `font[3] in FONT_NAMES and ".ttf" in font[4]`：只复原 BabelDOC 自己加进去的字体，**不碰原文档原有字体**（它们的 ToUnicode 本来就是好的，乱动反而会破坏）。

`make_tounicode` 还做了一个细节优化——对 CJK 兼容汉字与康熙部首做 Unicode 正规化（`apply_normalization`，[backend/pdf_creater.py:427-437](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L427-L437)），保证复制出来的字是规范字形而非兼容异体。

> 说明：本讲聚焦 backend 的子集化与 ToUnicode 复原。`reproduce_cmap` 的实际调用发生在 `high_level.py` 落盘前的 `fix_cmap` 修正环节（见 u2-l2 提到的 PDF 修正链路），此处聚焦其实现原理。

#### 4.2.4 代码实践

**实践目标**：感受子集化对体积的影响，并验证译文可被复制。

**操作步骤**：

1. 用同一份 PDF 跑两次翻译：一次正常（默认），一次加 `--skip-clean`（跳过子集化与清理，见 u1-l4 配置）。
2. 对比两个 `.mono.pdf` 的文件大小。

**需要观察的现象**：正常模式下产物明显更小；`--skip-clean` 模式下体积更大（嵌入了完整字体）。

**预期结果**：用任何 PDF 阅读器打开**正常模式**的 mono PDF，框选译文文字复制，粘贴出来应当是正确的中文——这说明 `reproduce_cmap` 复原的 ToUnicode 生效了。

> 待本地验证：具体体积差与字体相关，请以本地实测为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reproduce_cmap` 只复原 `FONT_NAMES` 里的字体，而不对所有字体都复原一遍？

**参考答案**：原文档自带的字体，其 ToUnicode CMap 本来就是作者 / 排版软件生成好的、正确的。BabelDOC 只对自己**新嵌入**的字体（译文用的中 / 日 / 韩字体）做复原，因为这些字体是它自己挂上去的，子集化后映射可能不一致。对所有字体都重做既无必要，又有破坏原有正确映射的风险。

**练习 2**：`parse_truetype_data` 用「`outline.contours` 是否非空」判断字形是否在用。一个 `.notdef` 字形（码位未定义的占位字形）通常会被保留还是丢弃？为什么这无所谓？

**参考答案**：`.notdef` 一般没有可见轮廓（空或极简），`contours` 为空会被 `parse_truetype_data` 判为「未使用」而不保留。这无所谓，因为 `.notdef` 只在字体里找不到某字形时才显示，正常渲染不会命中它；子集化去掉它不影响任何实际字符的显示与复制。

---

### 4.3 子进程保护：save_pdf_with_timeout 与超时回退

#### 4.3.1 概念说明

PyMuPDF 的 `pdf.save(..., clean=True)` 会重算交叉引用表、压缩对象、清理冗余，能产出更干净更小的 PDF，但它**可能很慢**，极个别畸形 PDF 上甚至会**卡死或崩溃**。如果让它在主进程里跑，一旦卡住，整个翻译就挂了。

BabelDOC 的对策是一个反复出现的工程模式——**「子进程 + 超时 + 回退」**：

1. 把耗时不稳定的操作扔进一个**独立子进程**跑；
2. 主进程**轮询计时**，超过阈值就 `terminate` / `kill` 掉子进程；
3. 子进程失败或超时，就**回退**到一个更朴素但更稳的方案（如 `clean=False`），保证「最差也能出文件」。

这个模式在本讲出现了两次：字体子集化（`subset_fonts_in_subprocess`，超时 60s）和保存（`save_pdf_with_timeout`，超时 120s）。学会一个，另一个就是同构的。

子进程函数末尾都用 `os._exit(0/1)` 而不是 `return` 或正常的解释器退出——`os._exit` **不执行 atexit 钩子、不跑析构器、不刷新缓冲区**，能最大程度隔离子进程里的副作用（比如 PyMuPDF 的全局状态），避免污染父进程。

#### 4.3.2 核心流程

```
save_pdf_with_timeout(pdf, output_path, ..., timeout=120):
  把 pdf 存到临时输入文件 temp_input
  启子进程跑 _save_pdf_clean_process(temp_input → temp_output, clean=True)
  while 子进程还活着:
      若已超时:
          terminate → join(5) → 仍活着则 kill
          回退：pdf.save(output_path, clean=False)  # 主进程内，更便宜
          返回 False
      sleep 0.5
  子进程正常结束:
      若 exitcode==0 且 temp_output 非空:
          shutil.copy2(temp_output → output_path)，删除临时文件，返回 True
      否则:
          回退：pdf.save(output_path, clean=False)，返回 False
```

返回值 `True/False` 表示「是否用上了 clean=True 的高质量保存」，调用方可据此记录日志，但**无论如何 output_path 都会有文件**。

#### 4.3.3 源码精读

子进程入口 `_save_pdf_clean_process` 极简，关键是末尾 `os._exit`：

[backend/pdf_creater.py:574-609](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L574-L609) —— 成功 `os._exit(0)`、异常 `os._exit(1)`，把退出码当作「成功与否」的信号传回主进程。

主进程的超时轮询与三级回退在 `save_pdf_with_timeout`：

[backend/pdf_creater.py:1293-1433](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1293-L1433) —— 读这段重点看三个分支：(1) 超时分支（[L1352-L1388](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1352-L1388)）先 terminate 再 kill，然后 `clean=False` 回退；(2) 成功分支（[L1395-L1413](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1395-L1413)）拷贝临时产物、删临时文件；(3) 失败分支（[L1414-L1433](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1414-L1433)）同样 `clean=False` 回退，最后还有「最朴素 `pdf.save(output_path)`」兜底。注意 `finally` 里删除临时文件，避免工作目录残留。

同构的子集化保护：

[backend/pdf_creater.py:1219-1291](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1219-L1291) —— `subset_fonts_in_subprocess`，超时 60s。它与 `save_pdf_with_timeout` 唯一的不同是「回退策略」：子集化失败时没有更便宜的等价操作可做，于是直接返回**原始未子集化的 pdf**（`return original_pdf`）——产物会大一点，但保证能用。

#### 4.3.4 代码实践

**实践目标**：从源码层面确认「回退」一定有产物，不会因保存失败而无文件。

**操作步骤**：

1. 阅读 [backend/pdf_creater.py:1370-1386](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1370-L1386)。
2. 数一数：从超时发生到函数返回，`output_path` 上至少有几次「写文件」的尝试？

**需要观察的现象**：超时分支里先尝试 `pdf.save(output_path, clean=False)`，万一这一步又抛异常，`except` 里还有一次最朴素的 `pdf.save(output_path)`。

**预期结果**：在最坏路径上，`output_path` 仍然会被写入一个文件（哪怕体积更大、未清理）。这正是「健壮性优先于完美」的工程取舍。

> 待本地验证：可用一份故意损坏的 PDF 触发保存异常，观察日志中是否出现 `Falling back to save with clean=False` 与 `Error in fallback save`。

#### 4.3.5 小练习与答案

**练习 1**：为什么子进程函数用 `os._exit(0)` 而不是 `sys.exit(0)` 或直接 `return`？

**参考答案**：`sys.exit()` 会抛 `SystemExit`、触发 atexit 钩子和解释器清理，可能执行父进程注入的析构逻辑或刷新带副作用的缓冲；`return` 后子进程也可能继续跑 import 时注册的清理代码。`os._exit()` 立即终止进程、跳过一切清理，能彻底隔离子进程内的全局状态（如 PyMuPDF 的内部缓存），避免污染或拖慢。对「我只关心它成没成、用退出码表示」这种场景，`os._exit` 是最干净的选择。

**练习 2**：`save_pdf_with_timeout` 默认超时 120 秒，而 `subset_fonts_in_subprocess` 是 60 秒。两者失败后的回退有何不同？

**参考答案**：保存失败可回退到 `clean=False`（功能等价、只是产物没那么干净），所以最终能用高质量或低质量两种方式之一写出文件；子集化失败没有「便宜版子集化」可退，于是直接返回未子集化的原 pdf——产物体积更大但功能完整。两者都遵循「失败不致命，降级保产物」的原则。

---

### 4.4 mono / dual PDF 生成与 write 总编排

#### 4.4.1 概念说明

`PDFCreater.write` 是 backend 的**总编排函数**，它把前面三节的能力串成一条完整的「IL → 文件」流水线，并产出两种风格的 PDF：

- **mono PDF（单语）**：只含译文，贴着原版面。文件名形如 `xxx.zh.mono.pdf`。
- **dual PDF（双语）**：原文 + 译文并排或交替，方便对照阅读。文件名形如 `xxx.zh.dual.pdf`。

dual 又分两种布局：

| 布局 | 开关 | 效果 |
| --- | --- | --- |
| 并排（side-by-side，默认） | 默认 | 每页拆成左右两半，左原文右译文（`--dual-translate-first` 可反过来）。 |
| 交替页（alternating） | `--use-alternating-pages-dual` | 原文页 1、译文页 1、原文页 2、译文页 2……依次排列。 |

整条 `write` 用 `progress_monitor` 上报三个 stage（与 `TRANSLATE_STAGES` 对应）：先是逐页的 `Generate drawing instructions`（`PDFCreater.stage_name`），再是 `Subset font`（`SUBSET_FONT_STAGE_NAME`），最后是 `Save PDF`（`SAVE_PDF_STAGE_NAME`）。

> 关键认知：`write` 内部**并不真正画水印**。它产出的是 `.no_watermark` 后缀的「净版」PDF；水印首页由 `high_level.generate_first_page_with_watermark` 单独生成、再由 `merge_watermark_doc` 合并（见 u2-l2）。本讲聚焦 `write` 本身。

#### 4.4.2 核心流程

`write` 的主干（简化伪代码）：

```
write(config, check_font_exists=False):
  计算 mono_out_path（含 .debug / .no_watermark 后缀）
  pdf = 打开 original_pdf_path
  font_mapper.add_font(pdf, docs)                       # 把译文字体挂进 pdf
  【stage: Generate drawing instructions】逐页:
       update_page_content_stream(...) → 把 IL 画进 pdf 的每页
  【stage: Subset font】:
       若未 skip_clean → subset_fonts_in_subprocess(pdf)  # 4.2/4.3
  restore_media_box(pdf, mediabox_data)                  # 还原页面几何（见 u2-l2）
  若 only_include_translated_page → 删掉未翻译页
  【stage: Save PDF】:
       若未 no_mono → save_pdf_with_timeout → mono_out_path   # 单语
       若未 no_dual:
           打开 original_pdf 作对照底本
           若 use_alternating_pages_dual:
               dual = create_alternating_pages_dual_pdf(...)  # 交替
           否则:
               dual = create_side_by_side_dual_pdf(...)       # 并排
           save_pdf_with_timeout(dual → dual_out_path)        # 双语
       （可选）写出自动术语表 CSV
  return TranslateResult(mono_out_path, dual_out_path, glossary_path)
异常:
  若非重试 → 以 check_font_exists=True 重试一次（跳过缺字体的字符）
```

交替页模式的重排逻辑用到一个简单的下标公式。设原文有 \(n\) 页，`insert_file` 后文档顺序为 `[原文0..n-1, 译文0..n-1]`，要把第 \(i\) 页译文移到目标位置：

\[ \text{dest}_i = 2i + \mathbb{1}[\text{not dual\_translate\_first}] \]

即「译文优先」时译文放偶数位 \(2i\)，否则放奇数位 \(2i+1\)（原文在前）。

#### 4.4.3 源码精读

三个 stage 名常量与 `PDFCreater.stage_name`：

[backend/pdf_creater.py:34-35](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L34-L35) 与 [backend/pdf_creater.py:612-613](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L612-L613) —— 这三个名字正是 `high_level.TRANSLATE_STAGES` 末三行（[high_level.py:72-74](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L72-L74)）登记的 stage。

`write` 总编排：

[backend/pdf_creater.py:1443-1627](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1443-L1627) —— 重点看几处：

- 输出路径含 `.debug` / `.no_watermark` 后缀（[L1449-L1458](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1449-L1458)）：当 `watermark_output_mode != Watermarked` 时加 `.no_watermark`，呼应 4.4.1「write 产净版」。
- OCR workaround 时 `gc_level=4`（更激进的垃圾回收，[L1471-L1473](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1471-L1473)），否则 `gc_level=1`。
- 逐页绘制 stage（[L1461-L1469](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1461-L1469)）、子集化 stage（[L1474-L1483](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1474-L1483)）、保存 stage（[L1504-L1596](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1504-L1596)），三段与 `TRANSLATE_STAGES` 一一对应。
- 双语布局二选一（[L1558-L1576](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1558-L1576)）：`use_alternating_pages_dual` 决定走交替还是并排。
- 异常重试（[L1620-L1627](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1620-L1627)）：首次失败且 `check_font_exists=False` 时，以 `True` 重试——此时 `CharacterRenderUnit.render` 会跳过字体不存在的字符（见 4.1.3 的 `check_font_exists` 分支），牺牲个别字符换取整体能出 PDF。

两种 dual 布局的实现：

[backend/pdf_creater.py:1015-1097](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1015-L1097) —— `create_side_by_side_dual_pdf`：新建一个「宽度 = 原宽 + 译宽」的大页，用 `show_pdf_page` 把原文 / 译文分别贴到左右两个矩形，`dual_translate_first` 控制谁在左。

[backend/pdf_creater.py:1099-1127](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1099-L1127) —— `create_alternating_pages_dual_pdf`：把译文 `insert_file` 进原文档，再用 `move_page` 按上文公式重排成交替顺序。

`high_level.py` 里 `only_parse_generate_pdf` 的捷径：

[high_level.py:925-932](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L925-L932) —— 这就是本讲代码实践任务的靶子：跳过**所有** midend（扫描检测 / 版面 / 段落 / 公式 / 术语 / 翻译 / 排版），解析完 IL 直接 `PDFCreater(...).write(...)`，是「PDF → IL → PDF」的纯往返保真测试。注意它**仍然会跑** 4.4 里的三个 stage（绘制指令 / 子集化 / 保存）——捷径只省 midend，不省 backend。

正常翻译路径在排版后调用 `write`：

[high_level.py:1046-1047](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1046-L1047) —— `Typesetting` 之后立刻 `pdf_creater.write`，产出 mono/dual，随后 high_level 再合并水印首页。

`TranslateResult` 承载产物路径：

[translation_config.py:509-537](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L509-L537) —— `write` 返回的就是它：`mono_pdf_path` / `dual_pdf_path`，外加 `no_watermark_*` 别名（在未被水印合并前，二者指向同一文件）。

#### 4.4.4 代码实践（本讲主实践任务）

**实践目标**：运行 `babeldoc --only-parse-generate-pdf`，对照源码说清这条捷径走了哪条路、`SUBSET_FONT` 与 `SAVE_PDF` 两个 stage 分别做了什么。

**操作步骤**：

1. 准备一个示例 PDF（如仓库自带的 `examples/ci/test.pdf`，见 u1-l2）。
2. 运行（无需 `--openai` / key，因为不翻译）：

   ```bash
   babeldoc --only-parse-generate-pdf \
            --files examples/ci/test.pdf \
            --output ./out_only_parse
   ```

   > 待本地验证：以你本机安装的 BabelDOC 版本实际参数为准；若 `--output` 不支持，请按 `babeldoc --help` 指定的输出目录参数调整。

3. 观察终端进度条，留意是否依次出现 `Generate drawing instructions` → `Subset font` → `Save PDF` 三个 stage。
4. 打开产物目录，确认生成了 `.mono.pdf`（以及可能的 `.dual.pdf`）。

**需要观察的现象与对照源码**：

- **捷径在哪**：进度条里**不会**出现 `Detect Scanned File` / `Parse Page Layout` / `Parse Paragraphs` / `Translate Paragraphs` / `Typesetting` 等 midend stage——因为 [high_level.py:925-932](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L925-L932) 在解析完 IL 后直接跳到了 `PDFCreater.write`。
- **走的捷径是什么**：由于没跑 `ParagraphFinder` / `Typesetting`，IL 里没有成形的译文段落，`render_paragraph_to_char` 基本收不到译文字符；`write` 实际渲染的主要是 frontend 解析出的原文 `pdf_character`。产物 PDF 是「原文被解析再画回去」的往返结果——这是验证 frontend 解析保真度的利器。
- **`SUBSET_FONT` 做了什么**：对应 [pdf_creater.py:1474-1483](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1474-L1483) 的 `subset_fonts_in_subprocess`（4.2/4.3）。在 `only_parse` 模式下没注入新字体，子集化主要作用于原文已有字体；若加了 `--skip-clean`，这一步会被跳过。
- **`SAVE_PDF` 做了什么**：对应 [pdf_creater.py:1504-1596](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1504-L1596)，依次保存 mono 与 dual，每份都经 `save_pdf_with_timeout`（4.3）的子进程 + 超时 + 回退保护。

**预期结果**：产物 PDF 视觉上与原文高度一致（因为只是解析后重画），可用来评估 frontend 的解析精度；体积应比原文小（若未 `--skip-clean`，子集化生效）。

#### 4.4.5 小练习与答案

**练习 1**：`write` 第一次失败时，为什么会以 `check_font_exists=True` 重试一次？这一步牺牲了什么、保住了什么？

**参考答案**：首次失败常因某些字符的字体没正确挂进 PDF。`check_font_exists=True` 后，`CharacterRenderUnit.render`（[L101-L106](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L101-L106)）会跳过这些「字体不存在」的字符。它牺牲了个别字符的显示（这些字会缺失），保住了整份 PDF 能成功生成——「宁可少几个字，也不要整份失败」。这是 backend 的终极兜底。

**练习 2**：默认双语是「并排」，加 `--use-alternating-pages-dual` 变「交替」。从源码看，两者实现思路有何本质不同？

**参考答案**：并排（`create_side_by_side_dual_pdf`）是**新建大页 + 贴图**：每页物理上是「一张宽页」，用 `show_pdf_page` 把原文 / 译文当作图片贴到左右两半，原页面被重新排版成一张大画布。交替（`create_alternating_pages_dual_pdf`）是**重排已有页**：把译文整份 `insert_file` 进原文档，再用 `move_page` 调整页序，每页本身不变，只是顺序变成「原文、译文、原文、译文……」。前者改变页面几何，后者只改页面顺序。

**练习 3**：`only_parse_generate_pdf` 模式下，`write` 内部的 `SUBSET_FONT` 和 `SAVE_PDF` 两个 stage 是否仍然执行？为什么？

**参考答案**：仍然执行。`only_parse_generate_pdf` 的捷径在 `high_level` 层（[L925-L932](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L925-L932)），它跳过的是**midend 各阶段**，但最终仍调用同一个 `PDFCreater.write`；而 `SUBSET_FONT` 与 `SAVE_PDF` 是 `write` **内部**的固定环节（4.4.2 流程），无论从哪条路径进来都会跑。所以捷径只省翻译，不省 backend 的字体处理与落盘。

---

## 5. 综合实践

**任务：用 `--debug` 把整个 backend 的产物「剖开」，画出从 IL 到 PDF 的完整数据流。**

把前四节串起来，做一次带调试输出的翻译：

```bash
babeldoc --openai --openai-api-key <KEY> \
         --files examples/ci/test.pdf \
         --debug --output ./out_debug
```

> 待本地验证：请按你本机的服务配置与 `babeldoc --help` 调整参数；`examples/ci/test.pdf` 见 u1-l2。

完成后，结合本讲源码做以下分析（写成一份简短笔记）：

1. **IL 终态快照**：在 `--debug` 工作目录找到 `typsetting.json`（u7-l1 排版后的 IL 快照），挑一个段落，确认其 `pdf_paragraph_composition` 里既有普通字符、也有公式——它正是 `render_paragraph_to_char`（4.1.3）要拍扁的输入。
2. **渲染单元归类**：对照 4.1.2 的流程表，指出该页 IL 中的对象分别会被包装成哪类 `RenderUnit`、默认 `render_order` 是多少。
3. **内容流核对**：打开产物 `*.mono.pdf.decompressed.pdf`（`--debug` 时由 [pdf_creater.py:1511-1515](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1511-L1515) 输出的可读展开版），找到 `BT ... Tf ... Tm <XXXX> Tj ET` 序列，验证 `<XXXX>` 是字形编码而非 Unicode。
4. **子集化与保存**：对照 4.2、4.3，解释产物体积为何比原文小，并说明若某字体子集化超时，系统会如何回退。
5. **双语布局**：再用 `--use-alternating-pages-dual` 跑一次，对比 dual PDF 的页面结构差异（并排大页 vs 交替页）。

这个任务把「IL 对象 → 渲染单元 → 排序 → 内容流 → 子集化 → 落盘 → 双语布局」整条 backend 链路走了一遍，是检验你是否真正读懂本讲的最佳方式。

---

## 6. 本讲小结

- `PDFCreater` 是三段式流水线的 **backend**，把加工后的 IL 渲染回 PDF，对应 `TRANSLATE_STAGES` 末三个 stage：`Generate drawing instructions` / `Subset font` / `Save PDF`。
- **渲染单元**抽象（`CharacterRenderUnit` / `FormRenderUnit` / `RectangleRenderUnit` / `CurveRenderUnit`）把「怎么画」的差异下沉，`render_units_to_stream` 按 `(render_order, sub_render_order)` 排序保证「背景先画、文字后画」；字符渲染写的是**字形编码** `pdf_character_id` 而非 Unicode。
- **字体子集化**裁掉未用字形缩小体积；`reproduce_cmap` 用 FreeType 扫真字形、只为 BabelDOC 自带字体重生成 ToUnicode，保证译文可复制可搜索。
- **子进程 + 超时 + 回退**是两处共用的健壮性模式：`subset_fonts_in_subprocess`（60s，失败返回未子集化原 pdf）与 `save_pdf_with_timeout`（120s，失败回退 `clean=False`），子进程一律用 `os._exit` 隔离副作用。
- `write` 产出 **mono（单语）** 与 **dual（双语）** 两种 PDF；dual 默认**并排**（新建大页贴图），`--use-alternating-pages-dual` 切**交替页**（重排页序）；异常时以 `check_font_exists=True` 重试，宁可丢个别字符也要出文件。
- `only_parse_generate_pdf` 是 backend 的「纯往返测试」捷径：跳过全部 midend，解析完 IL 直接 `write`，但 backend 内部三阶段照跑。

---

## 7. 下一步学习建议

本讲讲完，BabelDOC 的核心三段式（解析 → 处理 → 渲染）主链路已全部覆盖。建议接下来：

1. **u8-l1（资源管理）**：本讲的字体子集化、`reproduce_cmap` 都依赖 `FONT_NAMES` 里的字体资源——去看看 `assets.py` 是如何下载、校验、离线打包这些字体的。
2. **u8-l2（分片翻译与结果合并）**：当 PDF 被切成多片分别翻译时，每片都会各自跑一遍 `PDFCreater.write`，再由 `ResultMerger` 合并。结合本讲的 mono/dual 生成逻辑，理解合并发生在哪一层。
3. **u8-l5（异常体系与健壮性）**：本讲的「子进程超时回退」「`check_font_exists` 重试」是 backend 的健壮性体现；去系统梳理 `BabelDOCException` 体系与 `high_level` 的 PDF 修正链路，把容错设计看全。
4. 若想再深入渲染细节，可阅读 `update_page_content_stream`（[pdf_creater.py:1629-1750](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1629-L1750)）里 XObject 表单子流的处理、以及 `_ensure_stream_extgstate_resources` 如何为透传指令补齐 ExtGState / Shading 资源引用。
