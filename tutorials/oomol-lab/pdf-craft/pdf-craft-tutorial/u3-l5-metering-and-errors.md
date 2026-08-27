# 计量、错误与中断控制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `OCRTokensMetering` 计量对象在引擎里如何逐事件累计、又沿哪条返回链路交还调用方。
2. 区分五类错误/检查器：`PDFError`、`OCRError`、`NoUsableOCRPagesError`、`InterruptedError` 与 `IgnorePDFErrorsChecker`/`IgnoreOCRErrorsChecker`，并写出「布尔或谓词」两种忽略策略。
3. 准确说出 `NoUsableOCRPagesError` 的触发条件（有失败页 **且** 可用页为零）以及它携带的 `failed_page_indexes` 信息。
4. 解释 `AbortedCheck` 协作式中断的工作原理：回调在哪些检查点被轮询、抛出的是什么异常、为什么不会自动变成 `InterruptedError`。

本讲是「PDF 提取主链路」单元的收尾讲。前三讲（u3-l1 到 u3-l4）讲完了「正常情况下数据怎么流」，本讲专门回答「不正常情况下会发生什么」——这也是把 pdf-craft 用进生产环境（长跑任务、配额控制、脏 PDF 输入）前必须掌握的一讲。

## 2. 前置知识

- **计量（metering）**：OCR 服务通常按 token 计费。pdf-craft 在转换过程中逐页记录输入/输出 token 数并汇总返回，让你能在跑完整本书之前估算成本。
- **谓词（predicate）**：一个接受参数、返回布尔值的函数，例如 `lambda e: e.page_index > 3`。「布尔或谓词」类型的参数意味着你既可以传 `True`/`False` 一刀切，也可以传一个函数按具体情况决定。
- **协作式中断（cooperative cancellation）**：与「强杀线程」不同，协作式中断由任务自己在合适的时机主动询问「要停吗」。pdf-craft 的做法是周期性调用一个用户提供的回调函数，回调返回真值就抛异常退出。它不能立即打断正在进行的单页 OCR，只能在各检查点生效。
- **懒加载（lazy import）**：把 `import` 语句写进函数体而非模块顶部，使模块导入时不触发依赖加载。pdf-craft 之所以自定义一套错误类型而不直接重导出上游 `doc-page-extractor` 的异常，正是为了保住懒加载（见 4.2.3 的源码注释）。
- **检查点（checkpoint）**：代码中调用中断检查的位置。检查点越密，中断响应越及时，但也越频繁地执行回调。

前置讲义衔接：u3-l3 已经介绍过 OCR 事件流的六种事件（`START`/`IGNORE`/`SKIP`/`RENDERED`/`COMPLETE`/`FAILED`）、`page_N.failed` 失败标记与 token 预算双轨扣减；u2-l2 介绍过 `ExtractionOptions` 的 `ignore_pdf_errors`/`ignore_ocr_errors`/`aborted` 三个字段。本讲深入这三样东西的**类型定义与执行语义**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pdf_craft/metering.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py) | 全库最小的模块之一：`AbortedCheck` 类型、`check_aborted` 检查函数、`OCRTokensMetering` 计量数据类、`InterruptedKind` 枚举 |
| [pdf_craft/error.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py) | `PDFError`/`OCRError`/`NoUsableOCRPagesError`/`InterruptedError` 四个异常类、两个忽略检查器类型别名、`to_interrupted_error` 转换助手 |
| [pdf_craft/transform.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py) | `PDFExtractionEngine`：计量累计循环、可用页统计、`NoUsableOCRPagesError` 的唯一抛出点 |
| [pdf_craft/pdf/ocr.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py) | OCR 驱动器：`OCREvent` 事件定义、错误忽略的执行点（`_check_ignore_error`）、每页的中断检查点 |
| [pdf_craft/pdf/page_extractor.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py) | 两阶段识别内部的中断检查点、把上游异常包装成 `OCRError`（带 `step_index`）的位置 |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py) | 门面层：`ExtractionOptions` 中的错误/中断入口、计量的返回路径 |
| [pdf_craft/extractor/pdf/extractor.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py) | `PDFExtractor.extract_with_metering`：全部引擎参数的默认值表、`(package, metering)` 元组的返回处 |
| [tests/test_composable_boundaries.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py) | 本讲实践任务的权威范例：用一个假 OCR 驱动真实引擎、离线复现 `NoUsableOCRPagesError` 与失败页重试 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**计量数据类**、**错误类型**、**中断检查**。它们恰好对应两个小文件 `metering.py`（24 行）与 `error.py`（79 行），加上 `transform.py` 里的消费循环。

### 4.1 计量数据类：OCRTokensMetering

#### 4.1.1 概念说明

`OCRTokensMetering` 只有两个字段：`input_tokens` 与 `output_tokens`。它回答的问题是：**这次转换总共消耗了多少 OCR token？**

为什么需要它？vendor OCR（远程服务）按 token 收费，一本书几百页，跑完才知道花费就太晚了。pdf-craft 把每页的 token 消耗挂在 OCR 事件上，引擎边跑边加总，最后把结果交还调用方——你可以先转换几页、乘以总页数来估算全书成本，再决定是否继续。

注意它是一个**可变的普通 dataclass**（没有 `frozen=True`）：引擎就是在原对象上做 `+=` 累计的。这与上一单元那些不可变配置类（`frozen=True`）形成对比——配置是「一次写好、处处只读」，计量是「逐页累加的工作台账」。

#### 4.1.2 核心流程

计量的一生：

```text
每页 OCR 完成
  └─ OCR.recognize 产出事件（input_tokens/output_tokens 挂在终态事件上）
       └─ PDFExtractionEngine._extract_from_pdf 消费循环：
            metering.input_tokens  += event.input_tokens
            metering.output_tokens += event.output_tokens
       └─ 循环结束，metering 作为 _extract_from_pdf 的第 5 个返回值返回
            └─ PDFExtractor.extract_with_metering 返回 (package, metering)
                 └─ 门面层两条出口：
                      · extract_pdf_with_metering → 返回元组（拿得到计量）
                      · convert_pdf_to_markdown / convert_pdf_to_epub → 直接返回 metering
                      · extract_pdf → 丢弃计量（`package, _ = ...`）
```

一个关键事实：**只有 `COMPLETE` 事件携带非零 token**。查 `OCREvent` 的构造点可知：

- `START`/`IGNORE`/`SKIP`：不传 token 字段，默认 0（缓存命中不产生新消耗）；
- `RENDERED`：显式传 `input_tokens=0, output_tokens=0`（渲染位图不花 token）；
- `COMPLETE`：携带真实页面识别的 `page.input_tokens` / `page.output_tokens`；
- `FAILED`：携带的是兜底 Page 的 0/0（见 4.2.2，被忽略的失败页用兜底内容落盘，计量为 0）。

所以计量实际上等于「所有成功识别页面的 token 之和」。

#### 4.1.3 源码精读

**定义**——[pdf_craft/metering.py:L15-L18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L15-L18)：`OCRTokensMetering` 就是一个两整数字段的 dataclass，没有 `frozen`，允许引擎原地累计。

**事件载体**——[pdf_craft/pdf/ocr.py:L31-L39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L31-L39)：`OCREvent` 的字段里 `input_tokens: int = 0`、`output_tokens: int = 0` 默认为零，只有终态事件才会填入真实值。

**累计循环**——[pdf_craft/transform.py:L73-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L73-L75)：引擎在进入 OCR 循环前初始化三样东西——归零的 `metering`、`usable_pages = 0`（可用页计数）与 `failed_page_indexes: list[int] = []`（失败页清单）。这三者正是本讲三个模块的共享状态。

[pdf_craft/transform.py:L95-L101](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L95-L101)：循环体先 `on_ocr_event(event)` 转发回调，再对**每一个**事件累加 token；同时 `COMPLETE`/`SKIP` 让 `usable_pages += 1`，`FAILED` 把页号追加进 `failed_page_indexes`。注意 `SKIP`（缓存命中）也算「可用页」——它上次的成果还在磁盘上。

[pdf_craft/transform.py:L117-L121](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L117-L121)：写完 `document.json` 元数据后，`metering` 作为五元组的最后一项返回。

**返回链路**——[pdf_craft/extractor/pdf/extractor.py:L14-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py#L14-L36)：`extract_with_metering` 解包五元组 `_, _, _, _, metering = ...`，返回 `(package, metering)`；同时这份默认值字典也是引擎全部参数的权威清单（`ignore_pdf_errors: False`、`aborted: lambda: False` 等）。

[pdf_craft/craft.py:L91-L110](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L91-L110)：门面的 `extract_pdf_with_metering` 原样返回这个元组；而 [pdf_craft/craft.py:L84-L89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L84-L89) 的 `extract_pdf` 用 `package, _ = ...` 把计量丢弃——**想看计量就别用这个方法**。[pdf_craft/craft.py:L179-L190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190) 的一步式 `convert_pdf_to_markdown` 则把 `metering` 作为返回值交出（`convert_pdf_to_epub` 同理）。

#### 4.1.4 代码实践

**实践目标**：不改一行库代码，用「假事件流」复刻引擎的计量累计逻辑，验证你理解的加总规则（只有 COMPLETE 携带非零 token）。

**操作步骤**（以下为示例代码，可直接保存为 `metering_replay.py` 运行，无需 OCR 凭据）：

```python
# 示例代码：离线复刻 transform.py 的计量累计循环
from pdf_craft.pdf.ocr import OCREvent, OCREventKind
from pdf_craft.metering import OCRTokensMetering

# 模拟一本 4 页书的典型事件流（页 2 命中缓存，页 4 被忽略策略放过）
events = [
    OCREvent(OCREventKind.START,    page_index=1, total_pages=4),
    OCREvent(OCREventKind.COMPLETE, page_index=1, total_pages=4,
             input_tokens=1000, output_tokens=200),
    OCREvent(OCREventKind.SKIP,     page_index=2, total_pages=4),
    OCREvent(OCREventKind.COMPLETE, page_index=3, total_pages=4,
             input_tokens=900,  output_tokens=180),
    OCREvent(OCREventKind.FAILED,   page_index=4, total_pages=4),  # 兜底页，token 为 0
]

metering = OCRTokensMetering(input_tokens=0, output_tokens=0)
usable_pages = 0
failed_page_indexes: list[int] = []
for event in events:                      # 对照 transform.py L95-L101
    metering.input_tokens += event.input_tokens
    metering.output_tokens += event.output_tokens
    if event.kind in (OCREventKind.COMPLETE, OCREventKind.SKIP):
        usable_pages += 1
    elif event.kind == OCREventKind.FAILED:
        failed_page_indexes.append(event.page_index)

print(metering, usable_pages, failed_page_indexes)
```

**需要观察的现象**：输出应为 `OCRTokensMetering(input_tokens=1900, output_tokens=380) 3 [4]`——`SKIP` 计入可用页但不加 token，`FAILED` 进失败清单且 token 为 0。

**预期结果**：与你将来真实运行 `convert_pdf_to_markdown` 拿到的计量口径一致——它就是所有 `COMPLETE` 页的 token 和。真实运行验证（需 OCR 凭据，待本地验证）：调用 `extract_pdf_with_metering` 时注册 `on_ocr_event` 自己也累加一份，最后与返回的 `metering` 比对，两者应相等。

#### 4.1.5 小练习与答案

**练习 1**：第二次复用同一 `package_path` 跑同一本 PDF，返回的计量是多少？为什么？

答案：`OCRTokensMetering(input_tokens=0, output_tokens=0)`。全书命中 `page_N.xml` 缓存，循环里只有 `SKIP` 事件，而 `SKIP` 事件不携带 token（`ocr.py` 构造时省略了这两个字段，默认 0）。

**练习 2**：想同时拿到 `DocumentPackage` 和计量，该调用门面的哪个方法？`extract_pdf` 为什么不行？

答案：调用 `extract_pdf_with_metering`（`craft.py` L91-L110），它返回 `tuple[DocumentPackage, OCRTokensMetering]`。`extract_pdf` 内部就是调它之后用 `package, _ = ...` 把计量丢掉了（`craft.py` L88），属于「不关心成本的便捷封装」。

### 4.2 错误类型：从页级异常到 NoUsableOCRPagesError

#### 4.2.1 概念说明

提取一本扫描书要经历几百次页面渲染与识别，任何一页都可能出问题：PDF 文件损坏（渲染失败）、vendor 服务限流（识别失败）、某一页是纯图片（识别质量差）。pdf-craft 把「单页失败」和「全书失败」分成两个层级：

- **页级错误**：`PDFError`（渲染/读取 PDF 时出错，来自 u3-l2 讲的 handler 层）与 `OCRError`（识别阶段出错，来自 u3-l4 讲的 `PageExtractorNode`）。它们都带 `page_index`，`OCRError` 还带 `step_index`（两阶段识别中的第几阶段，正文为 1、脚注复查为 2）。
- **全书级保护**：`NoUsableOCRPagesError`——当所有请求的页都失败时，引擎拒绝产出空包。

在两层之间是**错误忽略策略**：`ignore_pdf_errors` 与 `ignore_ocr_errors` 各自接受「布尔或谓词」。谓词让你做精细控制，例如「只在第 3 到 10 页忽略 OCR 错误」或「只忽略限流类错误」。

被忽略的错误不会凭空消失：该页会产出**兜底 Page**（整页截图或占位文本）、写入 `page_N.failed` 标记、发出 `FAILED` 事件，然后继续下一页。这样断点续跑时（u3-l3）失败页会被自动重试。

#### 4.2.2 核心流程

单页错误的三种结局：

```text
OCR 循环中抛出 PDFError / OCRError
  │
  ├─ checker 为 False 或 谓词返回 False → 原异常向上抛，提取终止
  │    （调用方拿到的是原始的 PDFError/OCRError）
  │
  └─ checker 为 True 或 谓词返回 True  → 「被忽略」：
       1. recognized_error = error（记下）
       2. page 为 None → 生成兜底 Page（有位图→整页截图布局；无位图→占位文本）
       3. 写 page_N.failed 标记（内容是异常类名）
       4. 兜底 Page 照常 save_xml 落盘 page_N.xml
       5. yield FAILED 事件（携带 error 与 0/0 token）
       6. 继续下一页
```

循环结束后的全书判断（`transform.py` L103-L104）：

\[ \text{抛出 } NoUsableOCRPagesError \iff |F| \ge 1 \;\wedge\; U = 0 \]

其中 \( F \) 是 `failed_page_indexes`（FAILED 页集合），\( U \) 是可用页数（COMPLETE + SKIP 计数）。换句话说：

- 只要还有**任意一页**成功（或缓存命中），失败页就会被容忍，转换继续；
- **全部**请求的页都失败（且错误被忽略放行到了循环结束），才抛 `NoUsableOCRPagesError`，并把失败页号元组放进异常。

注意一个细节：若错误**没有**被忽略，原始异常早在循环里就抛出去了，根本轮不到这个判断——所以 `NoUsableOCRPagesError` 的语义是「所有页都在忽略策略的放行下失败了，我拒绝生成一本空书」。

#### 4.2.3 源码精读

**页级异常**——[pdf_craft/error.py:L6-L16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L6-L16)：`PDFError` 的 `page_index` 可为 `None`（有些 PDF 操作不针对具体页），`OCRError` 则强制携带 `page_index` 与 `step_index`。紧跟其后的 [error.py:L31-L32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L31-L32) 提供了 `is_inline_error` 帮助函数，一次判断两种页级错误。

**检查器类型**——[pdf_craft/error.py:L35-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L35-L36)：`IgnorePDFErrorsChecker = bool | Callable[[PDFError], bool]`，OCR 版同理。这就是「布尔或谓词」的类型表达。

**忽略的执行点**——[pdf_craft/pdf/ocr.py:L179-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L179-L187)：`except PDFError` 与 `except OCRError` 两个分支结构完全对称——先问 `_check_ignore_error`，不肯忽略就 `raise` 重抛原异常，肯忽略就存进 `recognized_error` 留待后续处理。

[pdf_craft/pdf/ocr.py:L321-L328](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L321-L328)：`_check_ignore_error` 用 `TypeVar` 同时服务两种异常类型——传布尔直接返回布尔，传可调用就调用它。整个「布尔或谓词」的分支逻辑只有这 5 行。

**兜底页与失败标记**——[pdf_craft/pdf/ocr.py:L189-L204](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L189-L204)：被忽略的失败页生成兜底 Page、写 `page_N.failed`（内容为异常类名）、照常落盘 `page_N.xml`；成功页则删除遗留的失败标记。兜底页的构造在 [ocr.py:L285-L318](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L285-L318)：渲染成功但识别失败时用整页截图布局，连渲染都失败时用占位文本——且 `input_tokens=0, output_tokens=0`，这就是 FAILED 事件计量为零的原因。

**OCRError 的出生地**——[pdf_craft/pdf/page_extractor.py:L209-L223](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L209-L223)：两阶段识别循环里，`AbortError` 与 `TokenLimitError` 原样重抛（它们是中断信号，不是页级错误，见 4.3），其余一切上游异常都被包装成带 `page_index` 与 `step_index` 的 `OCRError`——`step_index` 从 1 起，开了 `includes_footnotes` 时脚注复查是第 2 阶段。

**全书保护**——[pdf_craft/transform.py:L103-L104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L103-L104)：`if failed_page_indexes and usable_pages == 0: raise NoUsableOCRPagesError(tuple(failed_page_indexes))`——正是上一节公式的直译。异常定义在 [error.py:L19-L28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L19-L28)，`failed_page_indexes` 作为公开属性保存，错误信息里也把失败页号列了出来。

**官方测试范例**——[tests/test_composable_boundaries.py:L53-L63](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L53-L63) 定义了一个假 OCR `_AllPagesFailOCR`：两页全部 yield `FAILED` 事件。[L112-L141](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L112-L141) 用 `object.__new__(PDFExtractionEngine)` 绕过构造器、把假 OCR 塞给 `_ocr` 属性，然后断言三件事：抛出 `NoUsableOCRPagesError`、`failed_page_indexes == (1, 2)`、`analyse_toc` 未被调用（保护生效，目录分析不会在空数据上白跑）。这是**离线复现本讲全部行为的钥匙**，4.2.4 的实践就基于它。

**InterruptedError 与懒加载**——[pdf_craft/error.py:L39-L54](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L39-L54)：`InterruptedError` 携带 `kind`（`InterruptedKind.ABORT` 或 `TOKEN_LIMIT_EXCEEDED`）与 `metering`（中断前已消耗的 token）。L39 的中文注释解释了它存在的原因：**不可直接用 doc-page-extractor 的 Error，该库的一切都是懒加载，若暴露，则无法懒加载**。另外注意它遮蔽了 Python 内置的 `InterruptedError`——从 `pdf_craft` 导入的这个是库自定义类，与内置异常没有继承关系。

[pdf_craft/error.py:L57-L79](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L57-L79)：`to_interrupted_error` 是**给调用方用的**显式转换助手——函数体内才 import 上游异常（保持懒加载），识别 `AbortError`→`ABORT`、`TokenLimitError`→`TOKEN_LIMIT_EXCEEDED`，并从异常对象读取 `input_tokens`/`output_tokens` 组装计量；不认识就返回 `None`。官方文档（`docs/zh-CN/TROUBLESHOOTING.md`）明确说明：这两种上游异常**不会自动**转换成 `InterruptedError`，需要调用方显式使用这个 helper。

#### 4.2.4 代码实践

**实践目标**：用谓词版 `ignore_ocr_errors` 体验「布尔或谓词」的完整决策表——同一份假事件流，三种策略得到三种不同结局。（本实践补全了大纲中 `ignore_ocr_errors=lambda e: ...` 的任务。）

**操作步骤**（示例代码，保存为 `error_policy_lab.py`，无需 OCR 凭据，思路完全照搬官方测试）：

```python
# 示例代码：离线演练错误忽略策略
import tempfile
from pathlib import Path
from unittest.mock import patch
from typing import cast

from pdf_craft.error import NoUsableOCRPagesError, OCRError
from pdf_craft.pdf.ocr import OCR, OCREvent, OCREventKind
from pdf_craft.transform import PDFExtractionEngine


class _FlakyOCR:
    """页 1 成功；页 2 报 rate limited；页 3 报 vendor rejected。"""
    last_page_pixel_sizes: dict = {}

    def recognize(self, **kwargs):
        yield OCREvent(OCREventKind.COMPLETE, 1, 3,
                       input_tokens=500, output_tokens=80)
        yield OCREvent(OCREventKind.FAILED, 2, 3,
                       error=OCRError("rate limited", 2, 1))
        yield OCREvent(OCREventKind.FAILED, 3, 3,
                       error=OCRError("vendor rejected", 3, 1))


def run(ignore_ocr_errors):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "package").mkdir()
        engine = object.__new__(PDFExtractionEngine)
        engine._ocr = cast(OCR, _FlakyOCR())
        events = []
        with patch("pdf_craft.transform.analyse_toc"), \
             patch("pdf_craft.transform.generate_chapter_files"):
            try:
                result = engine.extract_package(
                    pdf_path=root / "input.pdf",
                    analysing_path=root / "package",
                    ocr_size="gundam", dpi=None,
                    max_page_image_file_size=None,
                    includes_cover=False, includes_footnotes=False,
                    ignore_pdf_errors=False,
                    ignore_ocr_errors=ignore_ocr_errors,   # ← 唯一变量
                    generate_plot=False, toc_llm=None, toc_assumed=False,
                    aborted=lambda: False,
                    max_tokens=None, max_output_tokens=None,
                    on_ocr_event=events.append,
                )
                return ("完成", result[-1], [e.kind.name for e in events])
            except Exception as error:
                return ("抛出", type(error).__name__, str(error))


print(run(False))                                       # 策略 A：不忽略
print(run(lambda e: "rate limited" in str(e)))          # 策略 B：谓词只放行限流
print(run(True))                                        # 策略 C：全部忽略
```

**需要观察的现象**：

- 策略 A（`False`）：页 2 的 `OCRError` 不被放行，直接从 `recognize` 重抛——输出 `('抛出', 'OCRError', 'rate limited')`。
- 策略 B（谓词）：页 2 被放行（消息含 "rate limited"），但页 3 的 "vendor rejected" 不匹配 → 同样抛 `OCRError`，只是这次是页 3。
- 策略 C（`True`）：两页失败都被放行，`usable_pages = 1 > 0` → 不触发 `NoUsableOCRPagesError`，跑完全程，事件序列含 `COMPLETE, FAILED, FAILED`。

**预期结果**：三种策略分别对应 4.2.2 决策表的「立即终止 / 谓词选择性终止 / 容忍失败继续」。若把 `_FlakyOCR` 改成**全部页 FAILED**（即官方测试的 `_AllPagesFailOCR`），策略 C 将转而抛出 `NoUsableOCRPagesError`，且 `failed_page_indexes == (1, 2, 3)`——这正是 `tests/test_composable_boundaries.py` L112-L141 断言的行为。以上结论可离线验证（只需 `pip install pdf-craft`，无需任何凭据）；真实脏 PDF 上的表现待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：一本 10 页书，页 1-9 成功、页 10 失败且被 `ignore_ocr_errors=True` 放行。会抛 `NoUsableOCRPagesError` 吗？页 10 在产物里是什么样子？

答案：不会抛。触发条件是「有失败页 **且** 可用页为 0」，这里可用页为 9。页 10 会以兜底 Page 落盘成 `ocr/page_10.xml`（内容通常是整页截图布局），同时留下 `page_10.failed` 标记并发出 `FAILED` 事件——下次重跑时仅这一页会重新 OCR。

**练习 2**：写一个只忽略「第二阶段失败」的检查器。`step_index` 什么时候会是 2？

答案：`ignore_ocr_errors=lambda e: e.step_index == 2`。只有 `ExtractionOptions(includes_footnotes=True)` 时识别才分两阶段（`page_extractor.py` L205：`stages=2 if includes_footnotes else 1`），第一阶段识别正文、第二阶段复查脚注；`step_index == 2` 表示失败发生在脚注阶段——即使脚注丢了，正文还在，忽略它常常是合理的取舍。

**练习 3**：为什么 pdf-craft 要自定义 `InterruptedError`，而不是直接 `from doc_page_extractor import ...` 重导出上游异常？

答案：见 [error.py:L39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L39) 的注释——上游库的一切都是懒加载的，若在 `pdf_craft.error` 模块顶层暴露上游异常类，导入 `pdf_craft` 就会连带触发上游导入，破坏懒加载设计（EPUB-only 用户不该被迫加载 OCR 栈）。所以库内定义自己的异常容器，转换工作交给运行时才 import 上游的 `to_interrupted_error`。

### 4.3 中断检查：AbortedCheck 与 check_aborted

#### 4.3.1 概念说明

`AbortedCheck = Callable[[], bool]`——一个无参、返回布尔值的回调。你在 `ExtractionOptions(aborted=...)` 传入它，pdf-craft 在提取与渲染的各个检查点轮询：一旦返回真值就抛 `AbortError` 终止。

为什么需要它？一本几百页的书可能要跑几个小时。用户可能想点「取消」按钮、或者你的服务收到了关机信号。协作式中断让长期任务能**优雅地**停下来：已完成的页都在磁盘缓存里（u3-l3 的断点续跑），下次从断点继续，一分钱 token 都不浪费。

三个要点：

1. **协作式**：回调只是被周期性轮询，不能打断正在进行的那一次 OCR 请求。检查点之间最多浪费一页的工作量。
2. **抛的是上游 `AbortError`**：`check_aborted` 抛出的异常来自 `doc_page_extractor.extraction_context`，不是 4.2 讲的任何 pdf-craft 异常。
3. **不会自动包装**：门面不会替你把它转成 `InterruptedError`；要带 `kind` 与 `metering` 的友好版本，调用方需自行用 `to_interrupted_error` 转换（这是 u2-l2 与 u3-l3 都强调过的易错点，本讲给出机制层面的解释）。

另一个容易忽略的事实：**同一个 `aborted` 回调贯穿提取与渲染两个阶段**。门面在一步式方法里把它继续传给 `render_markdown`/`render_epub`（`craft.py` L188-L189、L207-L209），渲染器内部同样逐章调用 `check_aborted`——所以「取消」按钮在整个 convert 生命周期内都有效。

#### 4.3.2 核心流程

```text
调用方构造 ExtractionOptions(aborted=my_check)
  │  （默认 lambda: False，即永不中止）
  ├─ 提取阶段检查点：
  │    · OCR 循环：每页 START 之前           (ocr.py L107-L108)
  │    · 两阶段识别：阶段之间、绘图之后       (page_extractor.py L242, L249)
  │    · 上游 ExtractionContext 也持有该回调  (page_extractor.py L195-L200)
  │         ——长识别内部由上游库自行轮询
  ├─ 渲染阶段检查点：
  │    · Markdown：每章开头、脚注汇总之前     (markdown/render/render.py L46-L47, L64)
  │    · EPUB：render.py 同样调用 check_aborted
  │
  └─ 任一检查点发现回调返回真值：
       check_aborted 抛 AbortError（上游异常，懒加载 import）
       → 沿调用栈穿透 OCR.recognize / engine / 门面，直达调用方
       → 调用方捕获后可自愿调用 to_interrupted_error(error)
         得到 InterruptedError(kind=ABORT, metering=中断前计量)
```

由于每个已完成页都已落盘，中断后重跑同一 `package_path` 会从 `page_N.xml` 缓存继续——中断的代价被压到最小。

#### 4.3.3 源码精读

**类型与检查函数**——[pdf_craft/metering.py:L5-L12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L5-L12)：`AbortedCheck` 一行类型别名；`check_aborted` 先调回调，为真时才 `from doc_page_extractor.extraction_context import AbortError` 并抛出——import 写在 if 里面就是 4.2.3 说过的懒加载模式。顺带一提，`doc-page-extractor` 本身是标准安装的**基础依赖**（[pyproject.toml:L33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pyproject.toml#L33)），可选的 `[local]` extra 只是它的本地 GPU 运行时，所以这个 import 在标准安装下总是可用，懒加载解决的是导入开销而非缺失问题。

**每页检查点**——[pdf_craft/pdf/ocr.py:L107-L108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L107-L108)：`for ref in refs:` 循环的第一条语句就是 `check_aborted(aborted)`，位于该页 `START` 事件之前——这是最外层、成本最低的检查点，保证「下一页开始前」必有机会退出。

**识别内部检查点**——[pdf_craft/pdf/page_extractor.py:L194-L200](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L194-L200)：`aborted` 被装进 `ExtractionContext(check_aborted=aborted, ...)` 传给上游，长耗时的识别内部由上游库自行轮询。[L242 与 L249](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L242-L249)：pdf-craft 自己在两个识别阶段之间、以及每阶段画完调试图之后各补一次检查——两阶段识别（正文/脚注）一页内就有多个退出机会。

**渲染阶段检查点**——[pdf_craft/markdown/render/render.py:L44-L64](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/render.py#L44-L64)：写 Markdown 文件的章节循环每章开头调一次 `check_aborted(aborted)`（L47），脚注汇总前再调一次（L64）。结合 [craft.py:L186-L189](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L186-L189)（`convert_pdf_to_markdown` 把 `extraction.aborted` 继续传给 `render_markdown`）可以看到：**一个回调管全程**。

**默认值**——[pdf_craft/craft.py:L63-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L63-L65)：`ExtractionOptions` 里 `aborted: AbortedCheck = lambda: False`——默认永不中止；同一段还能看到 `ignore_pdf_errors`/`ignore_ocr_errors` 的默认值都是 `False`。[extractor.py:L16-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py#L16-L26) 的引擎层默认值与之呼应。

**官方文档佐证**——[docs/en/TROUBLESHOOTING.md:L62-L66](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/TROUBLESHOOTING.md#L62-L66)：「Find the page that failed」一节明确区分了两种中断——`aborted` 回调请求取消，与 token 预算是另一类中断条件；诊断时应捕获**实际异常类型**和最近的 OCR 事件。

#### 4.3.4 代码实践

**实践目标**：分两步验证中断机制——先直接调用 `check_aborted` 观察它抛什么，再在假 OCR 里复刻「页间检查」并体验 `to_interrupted_error` 的显式转换。

**操作步骤**（示例代码，保存为 `abort_lab.py`，无需凭据）：

```python
# 示例代码：第一步——直接观察 check_aborted 的行为
from pdf_craft.metering import check_aborted

print(check_aborted(lambda: False))   # 回调返回 False
try:
    check_aborted(lambda: True)       # 回调返回 True
except Exception as error:
    print(type(error).__module__, type(error).__name__)
```

```python
# 示例代码：第二步——假 OCR + 页间检查 + 显式转换
from pdf_craft.error import to_interrupted_error
from pdf_craft.metering import check_aborted
from pdf_craft.pdf.ocr import OCREvent, OCREventKind


class _AbortAfterFirstPage:
    last_page_pixel_sizes: dict = {}

    def recognize(self, *, aborted, **kwargs):
        for page_index in (1, 2, 3):
            check_aborted(aborted)          # 复刻 ocr.py L107-L108
            yield OCREvent(OCREventKind.COMPLETE, page_index, 3,
                           input_tokens=100, output_tokens=20)


state = {"done": 0}
def aborted():                              # 第 1 页完成后请求中止
    return state["done"] >= 1

try:
    for event in _AbortAfterFirstPage().recognize(aborted=aborted):
        print("事件:", event.kind.name, "页:", event.page_index)
        state["done"] += 1
except Exception as error:
    print("捕获:", type(error).__name__)
    translated = to_interrupted_error(error)
    if translated is not None:
        print("转换后:", translated.kind, translated.metering)
```

**需要观察的现象**：

- 第一步：第一次调用安静返回 `None`；第二次抛出的异常类型打印为 `doc_page_extractor.extraction_context.AbortError`——**不是** `pdf_craft.error` 里的任何类，也**不是**内置 `InterruptedError`。
- 第二步：打印一条 `事件: COMPLETE 页: 1` 后即中断；`捕获: AbortError`；`to_interrupted_error` 返回的对象 `kind` 为 `InterruptedKind.ABORT`，`metering` 来源于异常对象携带的 `input_tokens`/`output_tokens` 属性。

**预期结果**：验证三件事——① 检查点在「下一页开始前」生效（页 2 的 COMPLETE 不会出现）；② 裸异常是上游 `AbortError`，只捕获 `pdf_craft.InterruptedError` 的代码接不住它；③ 显式转换后才能拿到结构化的 `kind` 与 `metering`。`metering` 的具体数值取决于上游异常构造时是否携带计数（`AbortError` 常为 0/0，具体默认值待确认，可打印观察）。以上均可离线验证。

#### 4.3.5 小练习与答案

**练习 1**：`aborted` 回调在单页 OCR 请求进行中返回了 `True`，会立刻中断吗？

答案：不会。这是协作式中断：回调只在检查点被轮询（每页 START 前、识别阶段间、渲染每章前等）。正在进行的单页识别会做完，到下一个检查点才抛 `AbortError`。代价被限制在「最多一页」的工作量，而这一页的成果也会照常落盘缓存。

**练习 2**：调用方写了 `except pdf_craft.InterruptedError:`，但用户点取消后程序还是崩了。最可能的原因是什么？

答案：`check_aborted` 抛的是上游 `doc_page_extractor.extraction_context.AbortError`，pdf-craft **不会自动**把它包装成 `InterruptedError`（`to_interrupted_error` 是给调用方用的显式助手，库内部与门面层都没有调用它）。正确写法是先 `except Exception as error:` 捕获原始异常，再 `translated = to_interrupted_error(error)`，非 `None` 时按 `translated.kind` 处理。

**练习 3**：数一数：一次开了脚注的两阶段识别、随后渲染 Markdown 的转换中，`aborted` 回调可能在哪几类位置被调用？

答案：至少五类——① OCR 循环每页 START 前（`ocr.py` L108）；② 两阶段识别的 `ExtractionContext` 内部（上游轮询，`page_extractor.py` L195-L200）；③ 阶段之间与绘图之后（`page_extractor.py` L242、L249）；④ Markdown 渲染每章开头（`render.py` L47）；⑤ 脚注汇总前（`render.py` L64）。EPUB 渲染路径（`renderer/epub/render.py`）同样接入了 `check_aborted`。

## 5. 综合实践

**任务：写一个「错误与中断策略离线演练场」单元测试。**

把 4.2.4 与 4.3.4 的脚本整合成一个正式的 `unittest` 测试类，放在仓库外自建目录（不要改动仓库源码或测试），覆盖四个用例：

1. **全部失败 + 全部忽略 → 保护触发**：仿照 [tests/test_composable_boundaries.py:L53-L63](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L53-L63) 的 `_AllPagesFailOCR`，断言 `NoUsableOCRPagesError` 抛出、`failed_page_indexes == (1, 2)`，并用 `mock.patch` 验证 `pdf_craft.transform.analyse_toc` 未被调用。
2. **部分失败 + 谓词忽略 → 容忍继续**：COMPLETE + FAILED 混合流 + `ignore_ocr_errors=True`，断言不抛异常、返回的计量只含成功页 token、事件序列以 FAILED 结尾。
3. **谓词不匹配 → 原异常穿透**：`ignore_ocr_errors=lambda e: "rate limited" in str(e)`，失败消息是别的，断言原始 `OCRError` 从 `engine.extract_package` 冒出来。
4. **页间中止 → AbortError 可转换**：在假 OCR 的 `recognize` 里复刻 `check_aborted(aborted)`，第 1 页后中止，断言捕获的是上游 `AbortError`，且 `to_interrupted_error(error)` 返回 `kind == InterruptedKind.ABORT`。

**验收标准**：`python -m unittest your_test_file.py` 四个用例全绿。全部离线可验证——这正是官方测试示范的做法：用假 OCR 驱动**真实引擎**，把不可控的远程服务变成确定性的本地事件流。以后你想为任何 pdf-craft 行为写回归测试，这个模式都适用。

## 6. 本讲小结

- `OCRTokensMetering(input_tokens, output_tokens)` 是引擎在事件循环里逐事件 `+=` 出来的工作台账；只有 `COMPLETE` 事件携带非零 token，`SKIP`/`RENDERED`/`FAILED` 均为 0，计量即成功页 token 之和。
- 计量返回链路：`_extract_from_pdf` 五元组末位 → `extract_with_metering` 的 `(package, metering)` → `convert_pdf_to_markdown/epub` 直接返回；`extract_pdf` 会丢弃它。
- 错误分两级：页级 `PDFError`/`OCRError`（带 `page_index`，`OCRError` 另带阶段号 `step_index`），全书级 `NoUsableOCRPagesError`——触发条件是 \( |F| \ge 1 \wedge U = 0 \)，异常携带 `failed_page_indexes` 元组。
- `ignore_pdf_errors`/`ignore_ocr_errors` 接受布尔或谓词；被放行的失败页产出兜底 Page + `page_N.failed` 标记 + `FAILED` 事件并继续，下次运行自动重试。
- `AbortedCheck` 是无参布尔回调，`check_aborted` 在每页 START 前、识别阶段间、渲染每章前等检查点轮询，为真即抛上游 `AbortError`——协作式，不能打断单页识别进行中的请求。
- `AbortError`/`TokenLimitError` 不会自动变成 `InterruptedError`；调用方需显式用 `to_interrupted_error` 转换才能拿到 `kind` 与中断前计量。pdf-craft 自定义这套异常容器是为了保住对上游 `doc-page-extractor` 的懒加载。

## 7. 下一步学习建议

至此「PDF 提取主链路」单元完结：你已经吃透了从门面到 OCR 后端的完整调用链，以及这条链路上的计量、容错与中断机制。接下来按数据流方向有两个选择：

- **主线推荐**：进入单元 4「目录分析」，从 [u4-l1 目录页定位](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_pages.py) 开始——OCR 产出的 `ocr/page_N.xml` 正是目录分析的输入，`NoUsableOCRPagesError` 保护的就是这道工序前的数据完整性。
- **并发兴趣路线**：若你对本讲的「事件流 + 回调」风格意犹未尽，可以先跳到单元 8 的 u8-l1（LLM 运行时）与 u8-l2（修复循环），那里把「重试与错误策略」做得更系统化。
- 顺手阅读：[docs/en/TROUBLESHOOTING.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/TROUBLESHOOTING.md) 的「Find the page that failed」小节是本讲内容的官方对照版，两相对照能检验你的理解。
