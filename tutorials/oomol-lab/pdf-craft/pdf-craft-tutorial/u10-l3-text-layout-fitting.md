# u10-l3 BoxTextLayout：字号自适应与排版

## 1. 本讲目标

上一讲（u10-l2）我们看清了 `PDFPatcher` 的「白块覆盖 + 文字叠层」整页重建方案：原页栅格化作底图、白色矩形盖住旧文字、坐标系从图像像素换到 PDF 点。但有一个问题被刻意跳过了——**译文文字究竟以多大字号、怎样排进那个白色矩形**。文字排大了会溢出边框压到邻块，排小了又看不清；中文没有空格，换行规则和英文完全不同；万一译文比原文长很多（中译英反向、或德文译中文），干脆放不下怎么办？

本讲深入这最后一层，读完你应当能够：

1. 说清 `BoxTextLayout.fit` 的契约：**不截断、不溢出**，返回「能完整放下的最大字号」。
2. 理解字号自适应的策略：先在最小字号上做可行性预检，再在四分之一磅步进的字号网格上二分搜索。
3. 掌握 `PatchTextOptions` 八个字段各自的排版含义，尤其是默认中文字体 `STSong-Light` 的注册机制与拉丁基础字体的中文拒绝逻辑。
4. 理解 `reading_order` 对同一页多个文本块的真实影响：它决定绘制顺序（叠放层级）而非几何位置。

## 2. 前置知识

本讲假设你已读过 u10-l1（`PDFReplacement` 的 `bbox` 来自 OCR 检测框、`page_pixel_size` 来自 `document.json` 页几何）与 u10-l2（`PDFPatcher` 的叠层回写与预检机制）。在此之上，补充三个 reportlab 领域的概念：

- **Paragraph（段落）**：reportlab 的排版构件（术语叫 flowable）。给它一个宽度和样式，它自己完成断行，然后可以用 `drawOn(canvas, x, y)` 画到画布上——注意 `y` 是段落**左下角**的纵坐标。
- **wrap 与自然尺寸**：`paragraph.wrap(width, height)` 让段落按给定宽度断行，返回 `(实际宽度, 实际高度)`，即这段文字的「自然尺寸」。判断「放不放得下」就是拿自然尺寸和可用空间比大小。
- **leading（行距）**：排版术语，指相邻两行基线之间的距离。pdf-craft 里 `leading = font_size × line_height`，所以 `line_height=1.2` 意味着行高是字号的 1.2 倍。
- **CID 字体**：一种按「字符编号」组织的大字符集字体方案（如中日韩）。reportlab 的 `UnicodeCIDFont("STSong-Light")` 只在 PDF 里写入字体引用，**不需要随包携带字体文件**，由阅读器内置的字形渲染——这是默认选项能开箱画中文的原因。

还有一个量纲要明确：字号搜索发生在**四分之一磅**（0.25 pt）的网格上，因为 PDF 的长度单位是磅（point，1 pt ≈ 0.353 mm），0.25 pt 已远小于人眼可分辨的字号差异。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/pipeline/pdf/text_layout.py` | 本讲主角：`PatchTextOptions` 排版选项、`FittedParagraph` 排版结果、`BoxTextLayout` 盒式排版器 |
| `pdf_craft/pipeline/pdf/patcher.py` | 消费者：`PDFPatcher` 在预检中调 `fit`、在绘制中用 `FittedParagraph` 落笔，`reading_order` 排序也在这里 |
| `pdf_craft/pipeline/pdf/pipeline.py` | `PDFTranslationPipeline` 构造 `PDFPatcher` 的地方，也是自定义选项的注入缝 |
| `pdf_craft/craft.py` | 门面 `patch_pdf_with_package`：注意它**不暴露**排版参数，永远用默认选项 |
| `pdf_craft/__init__.py` | 公开 API 边界：`PatchTextOptions` 等从包顶层可导入 |
| `tests/test_pdf_text_layout.py` | `BoxTextLayout` 的单元测试：换行、归一化、最大字号、放不下抛错、字体校验 |
| `tests/test_pdf_patcher.py` | 回写集成测试：预检失败不留半成品、`overflow="skip"` 记录原因 |

## 4. 核心概念与源码讲解

### 4.1 盒式排版：把一段文字完整放进一个矩形

#### 4.1.1 概念说明

所谓盒式排版，就是**把一个固定的矩形（白色覆盖块）当成排版容器，把一段连续文本排进去，并保证一个硬约束：每个字符都必须落在容器内**——既不截断尾巴，也不溢出边界压到邻居。这个约束比「好看」优先：u10-l2 讲过，预检（preflight）要求任何一个替换项排不下就让整个 patch 失败，而「排不下」的判定权就在 `BoxTextLayout.fit` 手里。

`fit` 的输入输出非常干净：进是 `(文本, 容器宽, 容器高)`（单位磅，即 `patcher.py` 里换算好的 PDF 点坐标盒子），出是一个 `FittedParagraph`——已经断好行的 reportlab 段落、选定的字号、以及自然的宽高。三个前置加工值得注意：

1. **空白归一化**：`" ".join(text.split())` 把换行、连续空格全部压成单个空格。OCR/译文里的换行是「源文版式的残渣」而非语义，目标版式由白块形状决定，不该继承。
2. **内边距**：可用空间不是盒子本身，而是盒子四边各缩进 `horizontal_padding` / `vertical_padding` 之后剩下的内矩形——文字不贴白块边缘，视觉上有呼吸感。
3. **诚实测量**：测自然高度时给 `wrap` 传一个一百万磅高的巨型框架，而不是真实高度。这是为了让 reportlab 返回段落的**完整自然高度**——如果传真实的小高度，Platypus 会以为框架装不下要「拆分段落」，可能把尾部内容藏起来，导致「看似放下、实则丢字」的假阳性。

#### 4.1.2 核心流程

`fit` 的整体流程（字号搜索部分详见 4.2）：

```text
fit(text, width, height):
    normalized = 把 text 的所有空白压成单空格
    若 normalized 为空            -> ValueError（替换文本不能为空）
    available = (width - 2×h_pad, height - 2×v_pad)
    若 available 任一边 <= 0      -> ValueError（内边距吃光了盒子）
    _ensure_font(normalized)      # 字体可用性与中文能力检查（见 4.3）
    在 min_font_size 上做可行性预检  # 放不下直接抛错（见 4.2）
    在字号网格上二分搜索最大可放下字号 s
    return FittedParagraph(按 s 断好行的段落, s, 自然宽, 自然高)
```

拿到 `FittedParagraph` 后，`PDFPatcher` 如何落笔：`_draw_text` 以「盒子左上角减去垂直内边距」为段落的**顶边**，再向下悬垂 `fitted.height`，最后才减出 `drawOn` 需要的左下角纵坐标——即文字**顶对齐**地挂在白块上沿：

\[ y_{\text{段落左下}} = y_{\text{盒底}} + h_{\text{盒}} - v_{\text{pad}} - h_{\text{段落}} \]

至于**同一页多个文本块的排布**：每个块的位置完全由它自己的 `bbox` 决定，`reading_order` 不改几何。它的作用发生在绘制阶段——`patch` 先把每页的替换项按 `reading_order` 排序，然后跑两轮循环：第一轮画完**所有**白块，第二轮按顺序画**所有**文字。因此当两个白块重叠时，层级是确定的（文字永远在白块之上）；当两个文字区重叠时，阅读顺序靠后的文字画在最上层。排序同时保证输出与替换项的传入顺序无关——管线是按章节逐个 append 替换项的，同一页的块可能来自不同章节，排序把它们还原成页面的自然阅读序列。

#### 4.1.3 源码精读

fit 的入口与前置加工——空白归一化、空文本拒绝、内边距收缩、盒子过小拒绝：

[pdf_craft/pipeline/pdf/text_layout.py:42-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L42-L50)

这段是 `fit` 的前半：`normalized` 压平空白；`available_width/height` 扣掉两侧内边距；非正就抛 `ValueError`（比如一个 2pt 高的碎 bbox 会被 1pt×2 的内边距吃光）。

诚实测量——给 `wrap` 传巨型高度以获得完整自然尺寸：

[pdf_craft/pipeline/pdf/text_layout.py:100-104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L100-L104)

`_natural_size` 是静态方法，注释写明了动机：太矮的框架会让 Platypus 拆分段落、藏起尾部内容，一百万磅的高度迫使它报告真实所需高度。

段落构造——样式、CJK 断词、XML 转义：

[pdf_craft/pipeline/pdf/text_layout.py:79-98](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L79-L98)

`_paragraph` 把选项翻译成 reportlab 样式：四个对齐字符串映射为 `TA_*` 常量；`leading = font_size × line_height` 把行距绑定到字号；`wordWrap="CJK"` 允许在**任意两个汉字之间**断行（英文只能按空格断）——这是中文排得进窄盒的关键；`escape(text)` 转义 XML 特殊字符，防止译文里的 `<`、`&` 被 reportlab 的迷你标记语言误解析。注意所有 reportlab 导入都在函数体内——与 u10-l2 讲过的懒加载策略一致，不做 PDF 回写的用户不付 import 成本。

顶对齐落笔——`_draw_text` 的坐标换算：

[pdf_craft/pipeline/pdf/patcher.py:168-175](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L168-L175)

`y + box_height - vertical_padding - fitted.height` 正是上面的顶对齐公式：先用盒底加盒高走到盒顶，减去垂直内边距得到段落顶边，再减段落自身高度得到 `drawOn` 要求的左下角。

阅读顺序排序与两轮绘制：

[pdf_craft/pipeline/pdf/patcher.py:83-84](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L83-L84)（按 `reading_order` 对每页替换项排序）

[pdf_craft/pipeline/pdf/patcher.py:117-120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L117-L120)（第一轮全部白块、第二轮全部文字）

`reading_order` 的来源在管线收集阶段——每个块的页内序号 `block.order` 被直接当作阅读顺序：

[pdf_craft/pipeline/pdf/pipeline.py:91-94](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L91-L94)

排好序后，`render_dpi` 取该页第一个替换项声明的 dpi 来栅格化底图（[pdf_craft/pipeline/pdf/patcher.py:114-116](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L114-L116)）。

单元测试对换行与归一化行为的锚定：

[tests/test_pdf_text_layout.py:7-19](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_text_layout.py#L7-L19)（无空格中文自动多行、断言宽高 ≤ 盒宽-2 / 盒高-2；空白归一化后英文单词保持完整）

[tests/test_pdf_patcher.py:57-78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py#L57-L78)（三倍长中文译文多行排入 100×100 盒、成品 PDF 文本层可提取到译文开头）

#### 4.1.4 代码实践

**实践目标**：亲手调用 `fit`，验证「内边距收缩」与「空白归一化」两个行为，并读懂 `FittedParagraph` 三个字段。

**操作步骤**（示例代码）：

```python
# box_probe.py
from pdf_craft.pipeline.pdf import BoxTextLayout, PatchTextOptions

layout = BoxTextLayout(PatchTextOptions())  # 全默认：pad=1/1，字号 4~12

fitted = layout.fit("这是一段需要排入边框的中文译文。", 120, 40)
print("font_size:", fitted.font_size)
print("size:", fitted.width, "x", fitted.height)
print("lines:", len(fitted.paragraph.blPara.lines))

# 空白归一化观察：换行与连续空格被压平，英文单词内部不受影响
mixed = layout.fit("First paragraph.\n\nSecond   paragraph.", 120, 40)
print("normalized:", mixed.paragraph.text)
```

**需要观察的现象**：

- `fitted.width ≤ 118`、`fitted.height ≤ 38`——正是盒宽 120 减 2×1、盒高 40 减 2×1，内边距真实生效。
- 中文一行放不下时 `lines > 1`，说明 `wordWrap="CJK"` 在无空格文本中生效。
- `normalized` 输出形如 `First paragraph. Second paragraph.`（单个空格连接）。

**预期结果**：字号落在 4~12 之间且为 0.25 的整数倍；归一化文本与上述一致。具体字号数值**待本地验证**（依赖 reportlab 的实测字形宽度）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_natural_size` 要传一百万磅的高度，而不是直接传盒子的高度？

**答案**：`Paragraph.wrap(宽, 高)` 在高度不足时会按 Platypus 的分帧语义处理——可能拆分段落、只报告放得下的部分高度，导致尾部文字被「藏」起来，`fit` 误判为放得下、成品 PDF 丢字。传一个远大于任何真实需求的高度，`wrap` 别无选择只能报告完整自然高度，「放得下」的判定才是诚实的。

**练习 2**：`reading_order` 会改变某个文本块画在页面上的位置吗？它到底影响什么？

**答案**：不会。每个块的位置完全由其 `bbox` 经 `_box_in_points` 换算决定。`reading_order` 影响的是**绘制顺序**：每页替换项先按它排序，再执行「先全部白块、后全部文字」两轮循环。后果有二：文字永远压在白块之上（层级确定）；两个文字区重叠时阅读顺序靠后的画在上层。排序还让输出与替换项传入顺序无关，因为管线收集时同一页的块可能来自不同章节。

### 4.2 自适应字号：四分之一磅网格上的二分搜索

#### 4.2.1 概念说明

「自适应字号」要解的问题是：译文长度不可控（翻译本来就会长短不一），但白块大小由原文 bbox 锁死。策略是把字号当成**唯一的自由度**——在 `[min_font_size, max_font_size]` 区间里找**能完整放下的最大字号**。字号越大越易读，所以取最大而非最小；但必须先保证「放得下」这个硬约束，所以有下限保底、上限封顶。

这个搜索有一个天然的算法结构：定义谓词 \( P(s) \) = 「字号 \( s \) 下文本的自然尺寸不超过可用空间」。在固定宽度下，字号变小字变少行变矮，\( P \) 单调（\( P(s) \) 为真则更小的 \( s' \) 也为真）——这正是二分搜索的适用条件。而字号不必在连续实数上搜：reportlab 的字号是浮点数，但 0.25 pt 以下的差异没有视觉意义，所以 pdf-craft 把搜索离散化到**四分之一磅网格**上：

\[ s \in \left\{ \frac{k}{4} \,\middle|\, k \in \{4f_{\min},\ \dots,\ 4f_{\max}\} \right\}, \quad N = 4(f_{\max} - f_{\min}) + 1 \text{ 个候选} \]

默认区间 \( f_{\min}=4,\ f_{\max}=12 \) 时 \( N = 33 \)，二分搜索只需 \( \lceil \log_2 33 \rceil = 6 \) 次测量（外加 1 次最小字号预检），每个替换项最多构造 7 个 Paragraph——对整本书成千上万个替换项也依然廉价。

放不下时的策略由 `overflow` 字段决定，这是**排版层的失败策略**与 u10-l2 讲过的**预检机制**的接合点：`"error"`（默认）让 `ValueError` 一路上抛、整个 patch 失败且不留半成品；`"skip"` 把该块记入 `skipped_replacements`（不画白块、保留原文底图），其余照常。

#### 4.2.2 核心流程

```text
fit 的字号部分:
    # 第一步：可行性预检
    minimum = 按 min_font_size 构造段落
    (w, h) = natural_size(minimum, available_width)
    若 w > available_width 或 h > available_height:
        raise ValueError("cannot fit bbox at minimum font size ...")

    # 第二步：四分之一磅网格上的二分搜索
    low, high = round(4×min), round(4×max)
    best = None
    while low <= high:
        middle = (low + high) // 2
        s = middle / 4
        若 natural_size(按 s 构造的段落) 都不超过可用空间:
            best = 记录 (段落, s, 自然宽高)     # s 可行，尝试更大
            low = middle + 1
        否则:
            high = middle - 1                   # s 太大，收缩
    return best
```

补一个关键细节：**预检先于搜索**。如果连最小字号都放不下，二分搜索一次都不会成功，不如直接用带尺寸信息的错误消息快败——消息里包含 `required WxH` 与 `available WxH`，用户能立刻判断是译文太长还是 bbox 太小。

#### 4.2.3 源码精读

最小字号预检——放不下即抛带尺寸详情的错误：

[pdf_craft/pipeline/pdf/text_layout.py:53-60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L53-L60)

四分之一磅网格与二分搜索主体：

[pdf_craft/pipeline/pdf/text_layout.py:62-77](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L62-L77)

`low`/`high` 是字号乘 4 取整后的整数界，`font_size = middle / 4` 还原；可行则 `low = middle + 1`（往大搜），不可行则 `high = middle - 1`（往小收）；循环结束时 `best` 持有最大可行字号。L75-76 的防御性抛错在正常路径下不可达（预检已保证最小字号可行），防御的是未来改动。

排版层失败策略接入预检——`overflow` 的两个分支：

[pdf_craft/pipeline/pdf/patcher.py:86-102](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L86-L102)

u10-l2 已精读过这段的「先全部排版再落盘」意义，本讲的视角是：`except ValueError` 捕获的正是 `fit` 抛出的「放不下」；`"skip"` 分支把它转成 `PDFSkippedReplacement(页码, bbox, 原因)`，`"error"` 分支补上页码与 bbox 上下文后重抛。

跳过记录的落定时点与原子写收尾：

[pdf_craft/pipeline/pdf/patcher.py:127-132](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L127-L132)

`skipped_replacements` 在 `patch` 全部成功后才挂到实例上——失败的 patch 不留半成品文件，也不留半成品记录。同目录临时文件加 `replace` 的原子落盘在 u10-l2 已讲，此处呼应。

测试对「最大字号」与「放不下抛错」的锚定：

[tests/test_pdf_text_layout.py:27-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_text_layout.py#L27-L36)（300×100 盒放 "short text" 恰好命中 max=16；最小字号锁 8 时百倍长文本抛 "cannot fit bbox"）

[tests/test_pdf_patcher.py:80-96](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py#L80-L96)（预检失败抛 `page 1, bbox ...` 且目标文件不存在）

[tests/test_pdf_patcher.py:98-115](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py#L98-L115)（`overflow="skip"` 时 `skipped_replacements` 记录 "cannot fit bbox" 原因）

#### 4.2.4 代码实践

**实践目标**：观察字号随盒子高度的「台阶式」变化，验证 0.25 pt 步进与最小字号预检的报错信息。

**操作步骤**（示例代码）：

```python
# size_ladder.py
from pdf_craft.pipeline.pdf import BoxTextLayout, PatchTextOptions

layout = BoxTextLayout(PatchTextOptions(max_font_size=12, min_font_size=4))
text = "字号自适应的观察样例，盒子越小字号越小。"

for height in (60, 40, 28, 20, 14):
    try:
        fitted = layout.fit(text, 100, height)
        print(f"height={height:>3} -> font_size={fitted.font_size}, lines={len(fitted.paragraph.blPara.lines)}")
    except ValueError as error:
        print(f"height={height:>3} -> ValueError: {error}")
```

**需要观察的现象**：

- 随高度减小，`font_size` 逐级下降，且每个值都是 0.25 的整数倍（如 10.5、9.25）。
- 高度足够大时 `font_size` 封顶在 12（与测试 L27-31 中封顶在 max=16 同理）。
- 某个高度以下抛出 `ValueError`，消息形如 `replacement text cannot fit bbox at minimum font size 4.0: required ... available ...`，附带 required/available 两组尺寸。

**预期结果**：台阶单调下降直至报错；精确的临界高度**待本地验证**。

**延伸**（可选）：把 `PatchTextOptions` 换成 `overflow="skip"` 并走 `PDFPatcher.patch`（见第 5 节综合实践），观察同一溢出从「抛错」变为「记入 `skipped_replacements`」。

#### 4.2.5 小练习与答案

**练习 1**：默认选项下，`fit` 对一个替换项最多构造多少个 Paragraph？为什么可以放心这么算？

**答案**：最多 7 个：1 个最小字号预检 + 至多 6 次二分测量（候选 \( 4×(12-4)+1=33 \) 个，\( \lceil \log_2 33 \rceil = 6 \)）。能这么算的前提是「字号越小越放得下」的单调性——它使二分搜索正确，也使预检一次即可排除整段区间。整本书数千替换项也只是数万次内存中的段落测量，无网络、无磁盘，成本可忽略。

**练习 2**：把 `min_font_size` 从 4 改成 8，对「能放下的文本」与「选中的字号」各有什么影响？

**答案**：对本来就能放下的文本，选中字号**可能不变也可能变大**——搜索永远取「能放下的最大字号」，下限只影响可行性边界，不影响「往大搜」的方向；真正变化的是原本需要 4~7.75 pt 才放得下的长译文现在会直接抛 `ValueError`（或被 `overflow="skip"` 记为跳过）。换句话说：`min_font_size` 是可读性底线——宁可失败也不排出小于底线的蚂蚁字。

**练习 3**：为什么预检的 `ValueError` 消息里要带 `required` 与 `available` 两组尺寸？

**答案**：因为「放不下」的原因有两种——译文异常长，或 bbox 异常小（OCR 碎块）。带上两组尺寸让调用者一眼分辨：required 远超 available 是译文问题（该检查翻译步骤）；available 本身极小是几何问题（该检查包的页几何元数据）。这符合项目一贯的「快败且可诊断」风格（对比 u10-l1 门面 `_validate_package_for_pdf` 的预检快败）。

### 4.3 字体选项：PatchTextOptions 与中文渲染

#### 4.3.1 概念说明

`PatchTextOptions` 是排版层的全部旋钮，一个冻结 dataclass、八个字段：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `font_name` | `"STSong-Light"` | 字体名。默认是 Adobe 华文宋体的 CID 引用 |
| `max_font_size` | `12.0` | 字号搜索上限（pt），也是「短文本的理想字号」 |
| `min_font_size` | `4.0` | 字号搜索下限（pt），可读性底线 |
| `line_height` | `1.2` | 行距系数，`leading = font_size × line_height` |
| `horizontal_padding` | `1.0` | 白块内左右内边距（pt），两侧各扣一次 |
| `vertical_padding` | `1.0` | 白块内上下内边距（pt） |
| `alignment` | `"left"` | 对齐：`left` / `center` / `right` / `justify` |
| `overflow` | `"error"` | 放不下时抛错或跳过：`"error"` / `"skip"` |

中文渲染的核心是**字体供应链**。默认 `STSong-Light` 走 CID 机制：reportlab 只在 PDF 里登记字体引用，不嵌字体文件，阅读器自带字形——零资产、开箱即画中文，代价是成品外观依赖阅读器实现。而 PDF 规范内建的 14 个标准字体（Courier/Helvetica/Times 四族）只有拉丁字形，`ord(字符) > 255` 的文本（中文、日文、带变音符的文字等）交给它们会得到空白或乱码——所以 `fit` 对这种组合**主动拒绝**，而不是画出一份看似成功实则无法阅读的 PDF。这是「宁可不画，不可画错」在字体层的体现。

选项的注入路径有一条**历史缝**：`PDFPatcher` 构造函数还接受旧的 `font_name` / `font_size` 位置参数（翻译为「最大字号」而非「固定字号」，且下限随之压低），与新的 `options` 参数互斥。更要注意的是：**门面 `PDFCraft.patch_pdf_with_package` 不接受任何排版参数**，内部永远构造默认选项的管线；想自定义排版，必须自己构造 `PDFPatcher(options=...)` 直接调用 `patch`，或把它注入 `PDFTranslationPipeline(patcher=...)`。

#### 4.3.2 核心流程

`_ensure_font` 的三道关卡（在每次 `fit` 开头执行）：

```text
_ensure_font(text):
    若 font_name == "STSong-Light" 且未注册:
        registerFont(UnicodeCIDFont("STSong-Light"))   # 惰性注册 CID 字体
    若 pdfmetrics 中查不到 font_name:
        raise ValueError("PDF patch font is unavailable: ...")
    若 text 含码位 > 255 的字符 且 font_name 属于 14 个拉丁标准字体:
        raise ValueError("... cannot reliably draw non-Latin replacement text")
```

构造期还有一道选项校验 `_validate_options`：字号必须为正且 `max ≥ min`、`line_height > 0`、两个内边距非负、对齐与溢出策略必须是合法字面量——非法配置在构造 `BoxTextLayout` 时就快败，而不是等到第一次 `fit` 才炸在半路上。

#### 4.3.3 源码精读

`PatchTextOptions` 全部字段与默认值：

[pdf_craft/pipeline/pdf/text_layout.py:11-22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L11-L22)

`_ensure_font`——CID 惰性注册、字体不存在拒绝、拉丁字体画非拉丁文本拒绝：

[pdf_craft/pipeline/pdf/text_layout.py:106-126](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L106-L126)

三段逻辑对应上面流程的三道关卡。注意第一段只对 `STSong-Light` 特判注册——它是唯一「reportlab 知道怎么建但需要显式注册」的字体；其余字体名要么是 14 个内建标准字体（天然可用），要么是调用方自己注册过的（否则第二关拒绝）。第三关的字形能力检查用 `ord(character) > 255` 判「非拉丁」，列出的十二个字体名即 Courier/Helvetica/Times 三族×四变体。

构造期选项校验：

[pdf_craft/pipeline/pdf/text_layout.py:128-139](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L128-L139)

`PDFPatcher` 构造函数——`options` 与旧参数互斥、旧 `font_size` 翻译为上限：

[pdf_craft/pipeline/pdf/patcher.py:38-67](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L38-L67)

两个要点：L51-52 同时传两套参数抛 `ValueError`；L53-62 的兼容翻译里 `max_font_size=font_size`，且 `min_font_size = min(4.0, font_size)`——传旧的 `font_size=2` 会同时把上限和下限都设为 2。L64 持有本讲主角 `BoxTextLayout` 实例，`fit` 的全部调用都经它发出。

字体现测的测试锚定：

[tests/test_pdf_text_layout.py:38-45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_text_layout.py#L38-L45)（默认字体画中文成功且字号 12；Helvetica 画中文抛 "cannot reliably draw"；不存在的字体抛 "font is unavailable"）

导出边界与门面的「默认值陷阱」：

[pdf_craft/__init__.py:14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L14)（`PatchTextOptions` 等从包顶层导出，用户无需深入子模块）

[pdf_craft/pipeline/pdf/pipeline.py:18-21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L18-L21)（`PDFTranslationPipeline` 的 `patcher` 参数是自定义排版的注入缝：缺省 `PDFPatcher(pdf_handler=...)` 即默认选项）

[pdf_craft/craft.py:160-172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L160-L172)（门面 `patch_pdf_with_package` 不接受排版参数，内部构造的管线永远用默认 `PatchTextOptions`）

#### 4.3.4 代码实践

**实践目标**：亲手触发三道字体现卡与构造期校验，弄清「哪些配置在构造时报错、哪些在 fit 时报错」。

**操作步骤**（示例代码）：

```python
# font_gates.py
from pdf_craft import PatchTextOptions, PDFPatcher  # 顶层即可导入
from pdf_craft.pipeline.pdf import BoxTextLayout

# 关卡 1：默认 CID 字体画中文 —— 成功
print(BoxTextLayout().fit("中文", 100, 100).font_size)

# 关卡 2：拉丁标准字体画中文 —— fit 时拒绝
try:
    BoxTextLayout(PatchTextOptions(font_name="Helvetica")).fit("中文", 100, 100)
except ValueError as error:
    print("gate2:", error)

# 关卡 3：不存在的字体 —— fit 时拒绝
try:
    BoxTextLayout(PatchTextOptions(font_name="not-a-font")).fit("text", 100, 100)
except ValueError as error:
    print("gate3:", error)

# 构造期校验：非法字号关系 —— BoxTextLayout 建立时即抛错
try:
    BoxTextLayout(PatchTextOptions(min_font_size=0))
except ValueError as error:
    print("validate:", error)

# 兼容缝：options 与旧参数互斥 —— PDFPatcher 构造时抛错
try:
    PDFPatcher(font_size=12, options=PatchTextOptions())
except ValueError as error:
    print("compat:", error)
```

**需要观察的现象**：后四条各打印一条 `ValueError` 消息，分别含 `cannot reliably draw`、`font is unavailable`、`font sizes must be positive...`、`pass either options or legacy...`；第一条正常打印字号 12。

**预期结果**：与 [tests/test_pdf_text_layout.py:38-45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_text_layout.py#L38-L45) 的断言一致；互斥与校验消息**待本地验证**（消息原文以 [pdf_craft/pipeline/pdf/patcher.py:51-52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/patcher.py#L51-L52) 与 [pdf_craft/pipeline/pdf/text_layout.py:128-139](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/text_layout.py#L128-L139) 为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把默认字体做成内嵌的 TTF/OTF，而用 `STSong-Light` 这种 CID 引用？

**答案**：CID 引用不需要随库或随 PDF 携带字体文件——reportlab 只登记引用，字形由 PDF 阅读器内置提供。对一个 PyPI 库来说这意味着零字体资产、零许可负担、包体不变，中文却开箱可用。代价是渲染细节因阅读器而异；需要统一视觉的项目可以自己注册字体后把 `font_name` 指过去（此时第二道关卡只检查「已注册」）。

**练习 2**：`PDFCraft().patch_pdf_with_package(...)` 想把行距从 1.2 调到 1.5，可行吗？该怎么做？

**答案**：直接走门面不可行——[pdf_craft/craft.py:160-172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L160-L172) 的门面不接收排版参数，内部构造的 `PDFTranslationPipeline` 缺省使用默认 `PDFPatcher`。可行做法是绕过门面组装：自己构造 `PDFPatcher(options=PatchTextOptions(line_height=1.5))`，注入 `PDFTranslationPipeline(pdf_handler=..., patcher=...)` 后调用其 `patch`（替换文本来自包，无需再传 transformer）。

**练习 3**：`alignment="justify"`（两端对齐）对中文换行意味着什么？结合 `wordWrap="CJK"` 说明。

**答案**：`wordWrap="CJK"` 允许在任意两个汉字间断行，所以中文文本几乎总能凑出「正好填满一行」的断点；配合 `justify` 时 reportlab 会拉伸字间距使两端齐平——这正是传统中文书籍的排版风格。而默认 `left` 下，断行位置同样自由，但行尾可能留白。注意无论哪种对齐，字号搜索的可行性判定不受影响（自然尺寸测量与对齐方式无关）。

## 5. 综合实践

把本讲三个模块串成一个完整实验：**同一替换项、三套 `PatchTextOptions`，各生成一份回写 PDF 对比；再为「文本恰好放不下时抛错」补一个边界测试**。

**实践目标**：

1. 体会 `min_font_size` 与 `overflow` 对成品的影响（三份 PDF 三种命运：小字排入 / 正常排入 / 跳过留原文）。
2. 验证换字体对中文的拒绝在真实 patch 链路上同样生效。
3. 按仓库测试风格补一个边界用例。

**操作步骤**：

第一步，准备一份本地源 PDF 并跑三套选项（示例代码，参照 [tests/test_pdf_patcher.py:13-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_patcher.py#L13-L33) 的造源手法，无需 OCR/LLM）：

```python
# patch_lab.py
from pathlib import Path
from reportlab.pdfgen import canvas
from pdf_craft import PatchTextOptions, PDFPatcher, PDFReplacement

root = Path("patch-lab"); root.mkdir(exist_ok=True)
source = root / "source.pdf"
doc = canvas.Canvas(str(source), pagesize=(200, 200))
doc.setFont("Helvetica", 12)
doc.drawString(20, 160, "Original")
doc.save()

# 600x600 像素坐标系里的边框 (60,60)-(360,360)，映射到 200x200pt 页面即 100x100pt 的盒子
long_text = "这是一段没有空格的中文译文，它应该在方框内自动换行。" * 40

cases = {
    # 默认：下限 4pt。超长译文若 4pt 仍放不下 -> overflow=error 直接抛错
    "default-error": (PatchTextOptions(), long_text),
    # 放低下限到 2pt：同样的盒子大概率能以极小字号排入
    "small-min": (PatchTextOptions(min_font_size=2.0), long_text),
    # 保留默认下限但允许跳过：该块不画白块、保留原文，其余正常
    "skip": (PatchTextOptions(overflow="skip"), long_text),
}
for name, (options, text) in cases.items():
    target = root / f"{name}.pdf"
    patcher = PDFPatcher(options=options)
    try:
        patcher.patch(source, target, [PDFReplacement(1, (60, 60, 360, 360), text, (600, 600))])
        print(name, "->", target, "skipped:", len(patcher.skipped_replacements))
    except ValueError as error:
        print(name, "-> ValueError:", error)
```

第二步，打开 `patch-lab/` 下的成品对比（PDF 阅读器或 `pdftotext`）：`small-min.pdf` 中该区域是一团极小但完整的多行中文；`skip.pdf` 中该区域保留着底图原文（白块未画）；`default-error` 则没有产物文件。

第三步，把上一步换成 `PatchTextOptions(font_name="Helvetica")` 配中文文本重跑，确认在 patch 链路上抛出 `cannot reliably draw non-Latin replacement text`。

第四步，补边界测试。仓库已有「明显放不下抛错」（[tests/test_pdf_text_layout.py:33-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_pdf_text_layout.py#L33-L36) 用百倍长文本），缺「恰好差一点放不下」的精确边界用例。新建独立测试文件（示例代码，不改动仓库现有测试）：

```python
# test_fit_boundary.py —— 用 python -m unittest test_fit_boundary 运行
import unittest
from pdf_craft.pipeline.pdf import BoxTextLayout, PatchTextOptions


class TestFitBoundary(unittest.TestCase):
    def test_just_overflowing_height_raises_instead_of_truncating(self):
        # 锁定字号（min == max == 10）：先量出自然高度，再把盒子削去一点，
        # 「恰好放不下」必须精确抛错，而不是缩小、截断或静默丢字。
        layout = BoxTextLayout(PatchTextOptions(max_font_size=10, min_font_size=10))
        text = "一段刚好放不下的中文译文。"
        fitted = layout.fit(text, 300, 100)          # 宽度不变 -> 断行不变
        with self.assertRaisesRegex(ValueError, "cannot fit bbox"):
            layout.fit(text, 300, fitted.height - 0.5)
```

**需要观察的现象**：第一步三种命运各不相同；第四步的测试通过（第一次 `fit` 成功，第二次高度减 0.5 pt 后，可用高度比自然高度还小 2.5 pt，触发最小字号预检抛错）。

**预期结果**：`small-min` 是否一定能在 2pt 排入 40 倍长文本、字号台阶的具体数值**待本地验证**（取决于 reportlab 实测字形宽度）；`skip` 记录一条 `skipped_replacements`、`Helvetica`+中文抛错、边界测试通过这三点由源码与既有测试可直接推定。

## 6. 本讲小结

- **盒式排版**：`BoxTextLayout.fit` 以「不截断、不溢出」为硬约束，把白块减去内边距得到可用矩形，空白归一化后交给 reportlab 断行；`wrap(width, 1_000_000)` 的巨型高度换来了诚实的自然高度测量。
- **自适应字号**：先在 `min_font_size` 上做可行性预检（失败即抛带 required/available 尺寸的错误），再在四分之一磅网格上二分搜索最大可放下字号——默认区间只需约 7 次段落测量；`overflow` 决定放不下时「整体失败」还是「记入 `skipped_replacements` 跳过」。
- **字体选项**：`PatchTextOptions` 八个字段覆盖字体、字号界、行距、内边距、对齐与溢出策略；默认 `STSong-Light` 走 CID 引用零资产画中文，14 个拉丁标准字体遇非拉丁文本被主动拒绝；选项经 `PDFPatcher(options=...)` 注入，门面路径固定用默认值。
- **阅读顺序**：`reading_order` 不改几何位置，只决定每页「先全部白块、后按序全部文字」的绘制顺序——层级确定、输出与传入顺序无关。
- 落笔是顶对齐的：段落顶边贴白块上沿减垂直内边距，`drawOn` 的纵坐标由盒高反推。

## 7. 下一步学习建议

PDF 翻译与回写单元到此完整走通：u10-l1 讲了替换项从哪来（包几何 + 检测框），u10-l2 讲了怎么画回页面（叠层 + 预检），本讲讲了文字怎么排进白块。接下来建议：

1. 进入 u11-l1，学习 `pdf_craft_tool` 的 `package patch-pdf` 子命令与 `pdf-patch` 冒烟路线（[pdf_craft_tool/cli.py:90-97](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L90-L97)、[pdf_craft_tool/smoke/checks.py:37-57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/checks.py#L37-L57) 的几何预检检查），把本讲的排版知识接到真实批量流程里。
2. 若想深挖排版细节，阅读 reportlab 用户手册中 Paragraph/ParagraphStyle 与 CJK wordWrap 的章节，对照 `_paragraph` 的样式构造逐项理解。
3. 为手册收官做准备：u12-l1 的综合实战会要求你把转换器协议、LLM 基础设施与本单元的回写管线组合成完整的自定义流程，届时可回顾 u10 三讲梳理「包 → 替换列表 → 叠层 → 排版」的完整数据流。
