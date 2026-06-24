# 扫描文档检测：DetectScannedFile

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 BabelDOC 是**怎么判断**一份 PDF 是「扫描件」的，并能解释为什么用 SSIM（结构相似度）而非简单的像素差。
- 区分两个不同层级的阈值：**单页** SSIM 阈值（`> 0.95`）与**文档级**扫描比例阈值（`> 80%`）。
- 读懂 `DetectScannedFile` 阶段的早退（early-exit）循环，知道为什么不需要逐页全部检测完就能下结论。
- 解释 `--auto-enable-ocr-workaround` 与 `ocr_workaround`、`skip_scanned_detection` 这三个开关在**两个阶段**（`TranslationConfig` 初始化期、`DetectScannedFile` 运行期）里的改写关系。
- 理解扫描检测一旦命中，会怎样向后联动改写翻译流水线（关富文本、清渲染顺序、关非公式线移除）。

## 2. 前置知识

### 2.1 什么是「扫描件 PDF」

PDF 有两种生成方式：

- **原生（born-digital）PDF**：文字、矢量图由排版软件（LaTeX、Word）直接写成 PDF 的文本对象（text object），可以被选中、复制。BabelDOC 的解析前端（见 u4 单元）能从内容流里把这些文本对象解析成带坐标的 `PdfCharacter`。
- **扫描件（scanned PDF）**：纸质文档经扫描仪拍成一张大位图，整页其实只有一张图片，文字是「图片里的像素」，没有可解析的文本对象。

对翻译流水线而言，二者区别巨大：扫描件没有真实文本可供解析，强行翻译只会得到一张「空白」的 IL。所以流水线必须在最早期就识别出扫描件，要么**报错中止**，要么**切换到 OCR workaround 模式**（把整页当图片处理）。这就是 `DetectScannedFile` 阶段存在的意义。

### 2.2 SSIM（结构相似度）是什么

比较两张图片「像不像」，最朴素的做法是逐像素相减求差值。但像素差对光照、平移极其敏感，两张「内容相同但亮度略不同」的图也会得到很大的差值。

SSIM（Structural Similarity Index Measure）换了个思路：它不再只看像素值，而是从**亮度（luminance）、对比度（contrast）、结构（structure）**三个维度综合衡量两幅图的相似程度。对一个局部窗口，其简化形式为：

\[
\mathrm{SSIM}(x,y)=\frac{(2\mu_x\mu_y+C_1)(2\sigma_{xy}+C_2)}{(\mu_x^2+\mu_y^2+C_1)(\sigma_x^2+\sigma_y^2+C_2)}
\]

其中 \(\mu_x,\mu_y\) 是窗口均值，\(\sigma_x,\sigma_y\) 是标准差，\(\sigma_{xy}\) 是协方差，\(C_1,C_2\) 是防除零的小常数。SSIM 取值范围为 \([-1,1]\)，越接近 1 表示两图越相似。BabelDOC 直接复用 `skimage.metrics.structural_similarity` 实现，不自己造轮子。

> **一句话直觉**：SSIM 衡量「把文字抠掉之后，这页看起来还像不像原来那页」。越像，说明「文字」本就不构成可见画面，也就是扫描件。

### 2.3 与前置讲义的衔接

本讲位于 **midend（中端）流水线的第一个 stage**（见 u2-l1、u2-l2）。`DetectScannedFile` 在 `TRANSLATE_STAGES` 中排在最前，权重 `2.45`，但只在 `skip_scanned_detection=False` 时才真正运行。它操作的对象是已经由 frontend 解析好的 IL `Document`（见 u3-l1），并对 `TranslationConfig`（见 u1-l4）做运行期的字段改写。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`babeldoc/format/pdf/document_il/midend/detect_scanned_file.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py) | 本讲主角。`DetectScannedFile` 类：逐页用 SSIM 判定、文档级汇总、命中后改写配置或抛错。 |
| [`babeldoc/format/pdf/high_level.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 把 `DetectScannedFile` 注册进 `TRANSLATE_STAGES`，并在 `_do_translate_single` 里显式调用；分片翻译时仅第 0 片做检测；跨片传播 OCR 决定。 |
| [`babeldoc/format/pdf/translation_config.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | `TranslationConfig.__init__` 在「初始化期」对 `ocr_workaround` / `skip_scanned_detection` 等做第一轮改写。 |
| [`babeldoc/format/pdf/document_il/backend/pdf_creater.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py) | `PDFCreater.update_page_content_stream(..., skip_char=True)`：重建内容流时跳过字符渲染单元，实现「移除文字」。 |

## 4. 核心概念与源码讲解

### 4.1 单页扫描判定：移除文字前后的 SSIM 对比

#### 4.1.1 概念说明

判断「某一页是不是扫描件」，最直接的办法是：**把这一页里的文字抠掉，看页面画面变不变**。

- 如果是**原生 PDF**：文字是可见的矢量笔画，抠掉之后画面会有明显空洞 → 两张图差异大 → SSIM 低 → **不是扫描件**。
- 如果是**扫描件**：文字其实是图片里的像素，解析器几乎抠不出有效的文字对象，抠掉前后画面几乎不变 → SSIM 高 → **是扫描件**。

于是单页判定的阈值就定在：SSIM `> 0.95` 时认定该页为扫描页。注意这是**单页**阈值，不是 80%（80% 是文档级阈值，见 4.2）。

#### 4.1.2 核心流程

`detect_page_is_scanned` 对单页的处理流程（伪代码）：

```text
渲染原页 → 得到位图 A（含文字）
用 PDFCreater 重建该页内容流，skip_char=True（剥掉所有字符渲染单元）
渲染改写后的页 → 得到位图 B（不含文字）
A、B 转灰度
similarity = SSIM(A_gray, B_gray)
返回 similarity > 0.95   # True = 该页是扫描页
```

关键点：`before` 与 `after` 都是从**同一个 pymupdf 文档**渲染出来的像素图，差异**唯一来源**是中间那次 `skip_char=True` 的内容流重建。

#### 4.1.3 源码精读

单页判定的完整实现：

[detect_scanned_file.py:L151-L174](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L151-L174) —— 取页像素图、转 numpy 数组、转置通道（`[:, :, ::-1]` 把 RGB 调成 BGR/OpenCV 风格）、调 `structural_similarity`、最后 `return similarity > 0.95`。

其中两段像素图转换值得注意：

```python
before_page_image = pdf[page.page_number].get_pixmap()
before_page_image = np.frombuffer(before_page_image.samples, np.uint8).reshape(
    before_page_image.height, before_page_image.width, 3,
)[:, :, ::-1]
```

`pymupdf` 的 `get_pixmap().samples` 是一维字节流，`.reshape((h, w, 3))` 还原成三通道图，`[:, :, ::-1]` 反转通道顺序。最终两图都再 `cv2.cvtColor(..., COLOR_RGB2GRAY)` 转灰度后才算 SSIM——灰度比较既快又去掉了颜色干扰。

「移除文字」这一步靠的是 `PDFCreater.update_page_content_stream` 的最后一个参数 `skip_char`：

[detect_scanned_file.py:L161-L163](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L161-L163) —— 以 `skip_char=True` 调用，重建该页内容流。

[pdf_creater.py:L1699-L1704](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1699-L1704) —— 当 `skip_char=True` 时，过滤掉所有 `CharacterRenderUnit`，只保留曲线、图片、矩形等非字符单元。于是重新写回的内容流里不再有可见文字。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解「移除文字」到底移除了什么。
2. **步骤**：
   - 打开 `pdf_creater.py`，定位 `update_page_content_stream`（L1629 起）与 `create_render_units_for_page`，弄清 `CharacterRenderUnit` 都包含哪些对象。
   - 思考：对一个原生 PDF 页，剥掉 `CharacterRenderUnit` 后还会剩下什么？对一个纯扫描页（一整张大图），剥掉前后差别有多大？
3. **需要观察的现象**：你会发现在扫描页里，文字解析得到的 `PdfCharacter` 要么很少、要么本身就不在可见位图上，所以剥掉它们对渲染结果几乎无影响——这正是 SSIM 会接近 1 的原因。
4. **预期结果**：原生页 SSIM 显著低于 0.95，扫描页 SSIM 接近 1.0。
5. 若想亲自跑数值：在 `--debug` 模式下翻译一份已知扫描 PDF，可在 `detect_page_is_scanned` 内临时加一行日志打印 `similarity`（**示例代码，非项目原有**）。数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用「逐像素差的绝对值之和」而要用 SSIM？

> **答**：扫描件里常有轻微的栅格化抖动、抗锯齿差异，逐像素差对这些噪声极其敏感，容易把「几乎相同」误判为「差异巨大」；SSIM 从亮度/对比度/结构三维度比较，对这类噪声鲁棒得多，更贴近人眼对「像不像」的判断。

**练习 2**：单页判定阈值是 `similarity > 0.95`。如果把它调低到 `> 0.5`，会带来什么后果？

> **答**：会有更多页被误判为扫描页，进而推高文档级 `scanned` 计数，容易让正常 PDF 也越过 80% 阈值被当作扫描件处理（轻则触发不必要的 OCR workaround，重则抛 `ScannedPDFError` 中止翻译）。

---

### 4.2 文档级判定与早退：80% 扫描比例阈值

#### 4.2.1 概念说明

单页判定只回答「这页是不是扫描页」。但我们要决定的是**整份文档**怎么处理，需要的是「这份 PDF 是不是扫描件」的结论。

BabelDOC 的策略是：**当扫描页数超过总待译页数的 80%，就判定整份为扫描件**。同时，为了不让大文档逐页全部跑一遍昂贵的 SSIM（每页都要渲染两次 + 算 SSIM），实现了一个**早退（early-exit）循环**：一旦计数足以作出结论，就停止继续检测。

#### 4.2.2 核心流程

设待译总页数为 `total`，定义：

\[
\text{threshold} = \max(0.8 \times \text{total},\; 1)
\]
\[
\text{non\_scanned\_threshold} = \text{total} - \text{threshold} = 0.2 \times \text{total}
\]

循环逻辑（伪代码）：

```text
for 每一待译页 page:
    if scanned < threshold 且 non_scanned < non_scanned_threshold:
        # 结论未定，继续实际检测
        is_scanned = detect_page_is_scanned(page)
        scanned 或 non_scanned 自增 1
    else:
        # 结论已定，剩下页直接跳过（记为 non_scanned 占位）
        non_scanned += 1
    progress.advance(1)

最终：if scanned >= threshold  → 文档判定为扫描件
```

早退的两种触发条件：

- `scanned >= threshold`：已经攒够 80% 的扫描页，文档必然是扫描件，剩余页无需再测。
- `non_scanned >= non_scanned_threshold`：已经攒够 20% 的非扫描页，文档**不可能**达到 80% 扫描，必然不是扫描件，剩余页无需再测。

注意「提前停止」时，剩余页会被记为 `non_scanned`（占位），但这不影响结论——因为此时结论已由计数边界锁定。

#### 4.2.3 源码精读

阈值计算与空集保护：

[detect_scanned_file.py:L94-L107](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L94-L107) —— 先用 `should_translate_page` 过滤出真正待译的页；若为空直接 `return`；否则算出 `threshold = 0.8 * total`（再 `max(threshold, 1)` 保证至少为 1）与 `non_scanned_threshold = total - threshold`。

早退循环本体：

[detect_scanned_file.py:L108-L123](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L108-L123) —— `with self.translation_config.progress_monitor.stage_start(...)` 起一个进度上下文；循环条件 `scanned < threshold and non_scanned < non_scanned_threshold` 决定是否真的调用 `detect_page_is_scanned`；无论是否真检测，都 `progress.advance(1)`，保证进度条仍按页推进。

文档级最终判定：

[detect_scanned_file.py:L125-L142](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L125-L142) —— `if scanned >= threshold` 即文档判定为扫描件，随后分两种命运（见 4.3）；否则什么都不做（正常文档，放行）。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：验证早退逻辑对极端页数的行为。
2. **步骤**：
   - 假设 `total = 10`。手算：`threshold = max(0.8×10, 1) = 8`，`non_scanned_threshold = 10 - 8 = 2`。
   - 模拟「前 8 页全是扫描页」：第 8 页检测完后 `scanned = 8`，`scanned < threshold` 变假 → 第 9、10 页直接跳过检测。
   - 再模拟「前 2 页全非扫描页」：第 2 页检测完后 `non_scanned = 2`，`non_scanned < non_scanned_threshold` 变假 → 第 3 页起直接跳过。
3. **需要观察的现象**：两种情形下都只实际跑了 SSIM 2 次或 8 次，而非全部 10 次。
4. **预期结果**：早退使大文档的最坏检测开销被压缩到约 `min(threshold, non_scanned_threshold)` 量级。
5. 手算结论可直接验证，无需运行；具体页数「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`threshold = max(threshold, 1)` 里的 `max(..., 1)` 是为哪种边界情况准备的？

> **答**：当 `total = 1`（只翻译一页）时，`0.8 × 1 = 0.8`，取整后若为 0 则永远不会触发「扫描件」结论。`max(..., 1)` 保证单页文档只要这一页是扫描页就会被判为扫描件。

**练习 2**：为什么早退时剩余页要记为 `non_scanned += 1` 而非 `scanned += 1`？

> **答**：早退发生意味着结论已定。若因 `scanned >= threshold` 早退，无论剩余页记成什么，`scanned >= threshold` 仍成立，结论不变；若因 `non_scanned >= non_scanned_threshold` 早退，则文档已不可能达到 80% 扫描，结论是「非扫描件」，剩余页记为 `non_scanned` 与该结论一致。记成 `scanned` 反而可能把一个已判定的非扫描件错误推过阈值。

---

### 4.3 OCR workaround 触发：命中后的副作用改写

#### 4.3.1 概念说明

当文档被判定为扫描件（`scanned >= threshold`），`process` 会根据 `auto_enable_ocr_workaround` 走两条截然不同的路：

- **开启** `auto_enable_ocr_workaround`：不报错，而是**自动打开 OCR workaround 模式**，并连带改写一批影响后续流水线的字段，让翻译「凑合」进行下去（把整页当图片填白底叠文字）。
- **未开启**：直接抛 `ScannedPDFError` 中止翻译，提示用户检查输入。

OCR workaround 模式的假设是：扫描页**背景纯白、文字纯黑**。在该模式下，BabelDOC 会用白色填底、把检测/翻译后的内容叠上去。正因如此，命中后必须关闭一批「依赖真实矢量文字」的处理：富文本翻译、非公式线移除、字符渲染顺序等。

#### 4.3.2 核心流程

命中扫描件后，若 `auto_enable_ocr_workaround=True`（伪代码）：

```text
记录 shared_context_cross_split_part.auto_enabled_ocr_workaround = True   # 跨分片广播
ocr_workaround = True
skip_scanned_detection = True          # 后续不再重复检测
disable_rich_text_translate = True     # 关富文本
clean_render_order_for_chars(docs)     # 清空字符渲染顺序、把字符颜色刷成 BLACK
remove_non_formula_lines = False       # 保留所有线（非公式线不再被移除）
```

若 `auto_enable_ocr_workaround=False`：

```text
raise ScannedPDFError("Scanned PDF detected.")
```

`clean_render_order_for_chars` 的作用：把每个 `PdfCharacter` 的 `render_order` 置 `None`，并把非调试字符的 `graphic_state` 强制设为 `BLACK`——即放弃原有渲染顺序与配色，统一按「黑字白底」重画。

#### 4.3.3 源码精读

命中分支与副作用改写：

[detect_scanned_file.py:L125-L142](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L125-L142) —— `if scanned >= threshold` 后再按 `auto_enable_ocr_workaround` 二分；开启分支连续改写 6 个字段/状态，关闭分支抛 `ScannedPDFError`。

清渲染顺序的实现：

[detect_scanned_file.py:L144-L149](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L144-L149) —— 遍历每页每个 `pdf_character`，`render_order = None`；非 `debug_info` 字符 `graphic_state = BLACK`。

涉及的异常类型来自 BabelDOC 异常体系（见 u8-l5）：

[detect_scanned_file.py:L9](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L9) —— `from ...BabelDOCException import ScannedPDFError`。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解 OCR workaround 模式为何要连带关闭一系列处理。
2. **步骤**：对照下面这张「命中后改写字段 → 影响的下游 stage」映射表，逐项在源码里找到下游消费点。

   | 改写字段 | 值 | 影响的下游行为 |
   | --- | --- | --- |
   | `ocr_workaround` | `True` | 后端按白底黑字填充扫描页 |
   | `skip_scanned_detection` | `True` | 分片翻译时后续分片跳过本阶段 |
   | `disable_rich_text_translate` | `True` | ILTranslator 不做富文本占位符（见 u6-l2） |
   | `remove_non_formula_lines` | `False` | StylesAndFormulas 不移除非公式线（见 u5-l4） |
   | `clean_render_order_for_chars` | 执行 | 丢弃原渲染顺序与配色 |
   | `auto_enabled_ocr_workaround` | `True` | 跨分片广播给其它片（见 4.4） |

3. **需要观察的现象**：这些字段并非孤立，它们共同把流水线从「矢量文字翻译」切换到「整页图像 OCR 风格翻译」。
4. **预期结果**：能说出每个改写至少影响一个下游 stage。
5. 实际运行触发需准备一份扫描 PDF，结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么命中后要把 `remove_non_formula_lines` 设为 `False`？

> **答**：OCR workaround 假设背景纯白、文字纯黑，扫描页里很多「线」其实是表格/版面的真实边线或文字笔画的一部分，贸然当作「非公式线」移除会破坏版面；关闭移除更安全。

**练习 2**：若用户既没开 `auto_enable_ocr_workaround`、也没开 `ocr_workaround`，翻译一份扫描 PDF 会发生什么？

> **答**：`process` 在 `scanned >= threshold` 后走 `else` 分支，抛出 `ScannedPDFError("Scanned PDF detected.")`，翻译中止，提示用户检查输入 PDF。

---

### 4.4 与配置项的交互：两阶段改写 ocr_workaround / skip_scanned_detection

#### 4.4.1 概念说明

本讲最容易混淆的就是三个开关之间的相互改写。README 把它单独列为「Important Interaction Note」。关键认知：**改写发生在两个阶段**，且这两个阶段对字段的「压服方向」相反。

- **阶段一：初始化期（`TranslationConfig.__init__`）**。在构造配置对象时，根据用户传入的开关做一轮「预处理改写」。
- **阶段二：运行期（`DetectScannedFile.process`）**。在实际检测出扫描件后，再做一轮「结果驱动改写」。

README 的核心交互规则（中文转述）：

1. 开启 `--auto-enable-ocr-workaround` 后，初始化期会**强制**把 `ocr_workaround` 与 `skip_scanned_detection` 都设为 `False`，**无视**用户同时传入的 `--ocr-workaround` 或 `--skip-scanned-detection`。
2. 进入 `DetectScannedFile` 阶段后，若检测为重度扫描（>80% 扫描页）且 `auto_enable_ocr_workaround=True`，才**反过来**把 `ocr_workaround` 与 `skip_scanned_detection` 都设为 `True`。
3. 若未检测为重度扫描，则阶段一强制出来的 `False` 值继续生效（除非被其它逻辑改写）。

#### 4.4.2 核心流程

用一张时序图式的描述把两阶段串起来：

```text
用户传入开关
   │
   ▼
[阶段一] TranslationConfig.__init__
   ├─ if ocr_workaround:                       # 用户直接开了 --ocr-workaround
   │     skip_scanned_detection = True          #   → 跳过检测、关富文本、不移除非公式线
   │     disable_rich_text_translate = True
   │     （后续 remove_non_formula_lines = False）
   │
   └─ if auto_enable_ocr_workaround:           # 用户开了 --auto-enable-ocr-workaround
         ocr_workaround = False                 #   → 强制压回 False（即使同时开了 --ocr-workaround）
         skip_scanned_detection = False         #   → 强制压回 False，保证检测一定会跑
   │
   ▼
[阶段二] DetectScannedFile.process（仅当 skip_scanned_detection=False 才进入）
   └─ if scanned >= threshold 且 auto_enable_ocr_workaround:
         ocr_workaround = True                  #   → 检测命中后才真正打开
         skip_scanned_detection = True          #   → 后续分片不再重复检测
         ...（4.3 的其它副作用）
```

要点：`auto_enable_ocr_workaround` 的语义是「**把是否启用 OCR 的决定权交给检测器**」。所以它在初始化期先**压制**手动设置（让检测有机会跑），在运行期命中后再**启用**。

#### 4.4.3 源码精读

**阶段一·`ocr_workaround` 的连带改写**：

[translation_config.py:L273-L278](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L273-L278) —— `self.ocr_workaround = ocr_workaround`；若为真则连带 `skip_scanned_detection = True`、`disable_rich_text_translate = True`。

[translation_config.py:L380-L381](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L380-L381) —— 若 `ocr_workaround` 为真，`remove_non_formula_lines = False`。

**阶段一·`auto_enable_ocr_workaround` 的压制改写**：

[translation_config.py:L336](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L336) 与 [translation_config.py:L343-L345](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L343-L345) —— 存入 `self.auto_enable_ocr_workaround`；若为真则强制 `ocr_workaround = False`、`skip_scanned_detection = False`。注意这段在 `__init__` 中位于 `ocr_workaround` 连带改写**之后**，因此能覆盖前面的值——这正是 README 所说「无视同时设置的 `--ocr-workaround`」的实现原因。

**阶段二·命中后的启用改写**：

[detect_scanned_file.py:L125-L142](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L125-L142) —— 运行期把 `ocr_workaround`、`skip_scanned_detection` 等设为 `True`（见 4.3）。

**阶段二的跨分片传播**：分片翻译时只有第 0 片做检测，但 OCR 决定必须传给后续片。第 0 片命中后会把决定写入共享上下文 `shared_context_cross_split_part.auto_enabled_ocr_workaround`，后续每片在 `_do_translate_single` 开头读取它：

[high_level.py:L843-L845](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L843-L845) —— 若共享上下文标记了 `auto_enabled_ocr_workaround`，则在本片也设 `ocr_workaround = True`、`skip_scanned_detection = True`。

[high_level.py:L600-L602](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L600-L602) —— 分片时 `if i > 0: part_config.skip_scanned_detection = True`，即只有第 0 片跑检测。

**stage 的注册与剔除**：

[high_level.py:L62](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L62) —— `TRANSLATE_STAGES` 中 `(DetectScannedFile.stage_name, 2.45)`，权重 2.45。

[high_level.py:L288-L289](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L288-L289) —— `get_translation_stage` 在 `skip_scanned_detection=True` 时把本 stage 从节目单剔除。

[high_level.py:L938-L944](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L938-L944) —— `_do_translate_single` 里对检测的**显式调用**：`skip_scanned_detection` 为真则跳过，否则 `DetectScannedFile(config).process(docs, temp_pdf_path, mediabox_data)`。

> 旁注：`detect_scanned_file.py` 里还有一个 [`fast_check`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L68-L82) 方法，用正则扫内容流里的标记内容（`/Artifact`、`/P … BDC`）与文本渲染模式（`3 Tr`）做一次「快速预筛」。但它在生产链路里的唯一调用处目前是**被注释掉的**（[high_level.py:L884-L898](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L884-L898)），所以当前真正生效的只有逐页 SSIM 这条路径。阅读时请注意区分「定义存在」与「已被启用」。同理，文件内的 `_save_debug_box_to_page`（[L25-L66](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L25-L66)）是一个调试辅助方法，当前 `process` 并未调用它。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：用真实代码验证 README 的「Important Interaction Note」。
2. **步骤**：
   - 读 README 第 438–446 行的交互说明（英文原文）。
   - 对照本节给出的两组源码行号（阶段一 `translation_config.py:273-278 / 343-345`，阶段二 `detect_scanned_file.py:125-142`），亲手在 `__init__` 里追踪字段赋值顺序：先 `ocr_workaround` 连带改写（L276-278），后 `auto_enable_ocr_workaround` 压制改写（L343-345），确认后者覆盖前者。
   - 模拟三种用户输入，填写下表：

     | 用户传入 | 阶段一后 `ocr_workaround` | 阶段一后 `skip_scanned_detection` | 是否进入阶段二 | 命中扫描件后 |
     | --- | --- | --- | --- | --- |
     | 只开 `--ocr-workaround` | True | True | 否（已跳过） | — |
     | 只开 `--auto-enable-ocr-workaround` | False | False | 是 | 改为 True / True |
     | 同时开两者 | False（被压制） | False（被压制） | 是 | 命中才改 True |

3. **需要观察的现象**：第二、三行体现了「阶段一压制、阶段二启用」的相反方向；第一行则直接走捷径，根本不检测。
4. **预期结果**：你的表格应与 README 描述一致——`--auto-enable-ocr-workaround` 会无视同时设置的 `--ocr-workaround`，先压制再按检测结果决定。
5. 若想实跑：用 TOML/CLI 分别设置上述三组开关翻译同一份扫描 PDF，观察日志中「Turning on OCR workaround」是否出现。运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `auto_enable_ocr_workaround` 的压制改写（L343-345）必须放在 `ocr_workaround` 的连带改写（L276-278）**之后**？

> **答**：Python 按顺序执行赋值。若压制改写在前、连带改写在后，则用户开了 `--ocr-workaround` 时连带改写会把 `skip_scanned_detection` 重新设回 `True`，检测又被跳过，`auto_enable_ocr_workaround` 的「交给检测器决定」语义就失效了。放在后面才能保证压制生效。

**练习 2**：分片翻译时，第 0 片检测命中 OCR workaround，第 3 片是怎么知道也要开 OCR 的？

> **答**：第 0 片把决定写入跨片共享对象 `shared_context_cross_split_part.auto_enabled_ocr_workaround = True`；后续每片在 `_do_translate_single` 开头（high_level.py:843-845）检查该标记，若为真则在本片也设 `ocr_workaround=True`、`skip_scanned_detection=True`。同时分片逻辑本身就令 `i>0` 的片 `skip_scanned_detection=True`（high_level.py:600-602），避免重复检测。

## 5. 综合实践

**任务**：把本讲四个模块串起来，写一份「扫描检测行为说明卡」。

1. 准备一份**已知为扫描件**的 PDF（例如用手机扫描的文档）和一份**原生** PDF（例如 LaTeX 导出的论文）。
2. 分别用两种配置翻译：
   - 配置 A：`--auto-enable-ocr-workaround`
   - 配置 B：什么都不加（保持默认）
3. 对照源码回答：
   - 配置 A 下，阶段一（`TranslationConfig.__init__`）把 `ocr_workaround`、`skip_scanned_detection` 设成了什么？为什么？
   - 扫描件在配置 A 下：阶段二（`DetectScannedFile.process`）检测命中后改写了哪些字段？翻译是否继续？
   - 扫描件在配置 B 下：发生了什么异常？异常类名是什么？在哪一行抛出？
   - 原生 PDF 在两种配置下：`scanned` 计数是否达到 `threshold`？为什么阶段二什么都不改写就放行了？
4. 在 `--debug` 模式下，找到工作目录中的 `detect_scanned_file.json`（由 [high_level.py:L946-L949](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L946-L949) 落盘），对比扫描件命中前后 IL 中 `pdf_character` 的 `render_order` 与 `graphic_state` 变化（对应 `clean_render_order_for_chars`）。
5. **预期结果**：你能不看源码，用自己的话向别人讲清「为什么 `--auto-enable-ocr-workaround` 会无视 `--ocr-workaround`，以及检测命中后整条流水线是怎么切换到 OCR 模式的」。

> 说明：本实践需要真实 PDF 与可用的翻译环境，部分观察结果标注「待本地验证」。即便无法运行，仅完成第 3 步的源码对照也已达成本讲学习目标。

## 6. 本讲小结

- `DetectScannedFile` 是 midend 第一个 stage，用**移除文字前后的 SSIM 对比**判断单页是否为扫描页，单页阈值是 `similarity > 0.95`。
- 文档级判定用 **80% 扫描比例阈值**（`scanned >= 0.8 * total`），并通过早退循环避免逐页全部跑昂贵的 SSIM。
- 命中扫描件后，若开了 `auto_enable_ocr_workaround` 则切换到 OCR workaround 模式（白底黑字），连带改写 `ocr_workaround`/`skip_scanned_detection`/`disable_rich_text_translate`/`remove_non_formula_lines` 并清字符渲染顺序；否则抛 `ScannedPDFError` 中止。
- 三个开关的改写分**两阶段**：初始化期 `TranslationConfig.__init__` 先按 `ocr_workaround` 连带改写、再被 `auto_enable_ocr_workaround` **压制**回 `False`；运行期 `DetectScannedFile.process` 检测命中后才**启用**为 `True`。
- 分片翻译时仅第 0 片做检测，OCR 决定经 `shared_context_cross_split_part` 跨片广播给后续片。
- `fast_check` 与 `_save_debug_box_to_page` 虽在文件中定义，但当前生产链路未被启用/调用，阅读时注意区分。

## 7. 下一步学习建议

- **下一讲 u5-l2（版面分析）**：扫描检测放行后，流水线进入 `LayoutParser`，用 DocLayout-YOLO 识别页面区域。建议接着阅读 `babeldoc/format/pdf/document_il/midend/layout_parser.py`。
- **横向阅读 u5-l4（公式与样式）**：本讲提到的 `remove_non_formula_lines`、`disable_rich_text_translate` 的下游消费方都在 StylesAndFormulas 与 ILTranslator，对照阅读能加深对「OCR 模式副作用」的理解。
- **回顾 u2-l2**：把 `DetectScannedFile` 放回 `_do_translate_single` 的完整阶段序列里，确认它在解析之后、版面分析之前的位置。
- **延伸 u8-l5（异常体系）**：`ScannedPDFError` 是 BabelDOC 异常体系的一员，若对错误处理感兴趣可进一步阅读 `babeldoc/babeldoc_exception/`。
