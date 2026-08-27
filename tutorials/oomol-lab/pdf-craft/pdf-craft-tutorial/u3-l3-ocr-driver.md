# OCR 驱动器：事件流、缓存与断点续跑

## 1. 本讲目标

上一讲（u3-l2）我们看完了 PDF 访问层：`PDFHandler` 负责打开文档、`PageRefContext` 负责逐页迭代、`PageRef.render` 负责把单页渲染成图像。本讲沿着数据流继续向下，进入整条提取链路里最「贵」的一环——OCR 驱动器 `OCR.recognize`。

学完本讲，你应该能够：

1. 说出 `OCREvent` 六种事件（`START` / `IGNORE` / `SKIP` / `RENDERED` / `COMPLETE` / `FAILED`）各自的产生时机与含义，并能写出一个小的事件记录器。
2. 讲清断点续跑的完整机制：`ocr/` 目录下 `page_N.xml`、`page_N.failed`、`done` 三类文件如何协作，让「中断后重跑只补缺失的页」成为零成本行为。
3. 理解 token 预算如何跨页累计与扣减、预算耗尽时如何中断，以及失败页为什么不会让整本书报废，而是产出兜底内容并被标记待重试。

本讲的所有结论都来自 `pdf_craft/pdf/ocr.py` 与 `pdf_craft/metering.py` 的真实源码。

## 2. 前置知识

本讲只需要几个通用的编程概念，先用一段话各自说清：

- **生成器（generator）**：一个带 `yield` 的函数。调用它不会立刻执行，而是返回一个「可以逐步取值」的对象；每取一次（`for` 循环每轮）函数就执行到下一个 `yield` 暂停。生成器是天然的「边干活边汇报」结构：干一步、报一步，消费方随时可以停止。
- **事件对象（event object）**：用一个 `dataclass` 把「发生了什么」（`kind`）和「附带的上下文」（页码、耗时、token 数、异常）打包在一起。观察者只看事件，不需要理解内部实现。
- **哨兵文件（sentinel file）/ 标记文件**：用「某个路径下是否存在一个小文件」来表达状态。比如 `done` 文件存在就代表「全书 OCR 已完成」。它比数据库轻，且天然持久化在磁盘上、重启后依然有效——这是断点续跑的基础。
- **token 预算**：大模型按 token 计费。OCR 服务每处理一页会消耗输入 token（喂进去的图像/文本）和输出 token（吐出来的识别结果）。给一次提取任务设一个总预算，花完即停，避免失控成本。

另外请回忆前几讲已经建立的事实，本讲直接承接不再重复：

- `on_ocr_event` 回调、`aborted` 回调、`max_ocr_tokens` 预算都声明在 `ExtractionOptions` 上（u2-l2），门面把它们逐字段转发给引擎，其中 `max_ocr_tokens` 改名为引擎侧的 `max_tokens`（u3-l1）。
- `PageRefContext` 是「打开一次、逐页迭代、统一关闭」的上下文管理器；`PageRef.render` 才真正渲染位图（u3-l2）。
- 引擎四步主流程（OCR 循环 → 目录分析 → 章节生成 → 元数据落盘）中，本讲只深入第一步的内部（u3-l1）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pdf_craft/pdf/ocr.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py) | **本讲主角**。`OCREventKind`/`OCREvent` 事件定义与 `OCR.recognize` 生成器：事件流、缓存判定、预算控制、失败兜底全在这一个文件里 |
| [pdf_craft/metering.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py) | 小而关键：`AbortedCheck` 类型、`check_aborted` 中止检查、`OCRTokensMetering` 计量数据类、`InterruptedKind` 枚举 |
| [pdf_craft/transform.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py) | 引擎侧事件**消费方**：累计计量、统计可用页、抛 `NoUsableOCRPagesError` |
| [pdf_craft/error.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py) | `PDFError`/`OCRError`/`NoUsableOCRPagesError` 定义与 `to_interrupted_error` 转换助手 |
| [pdf_craft/pdf/page_extractor.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py) | `image2page`：OCR 驱动器每页调用的识别入口，返回带 token 计量的 `Page`（细节留到 u3-l4） |
| [pdf_craft/pdf/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py) | `Page` 数据结构与 `encode`，决定 `page_N.xml` 里写什么 |
| [docs/zh-CN/TROUBLESHOOTING.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/zh-CN/TROUBLESHOOTING.md) | 官方排障指南，对事件类型与中断异常的说明可作为本讲的对照阅读 |

## 4. 核心概念与源码讲解

### 4.1 生成器事件流：recognize 如何逐页汇报

#### 4.1.1 概念说明

一次提取可能要对几百页逐页调用 OCR 服务，耗时以分钟甚至小时计。调用方（以及最终用户）必须能回答三个问题：

1. **现在跑到第几页了？**——进度可见。
2. **每一页花了多少时间、多少 token？**——成本可核算。
3. **有没有页失败了？失败在哪一步？**——问题可定位。

如果 `recognize` 是一个「吞进 PDF、吐出全部结果」的普通函数，这三个问题都回答不了。所以它被设计成**生成器**：每处理一页就 `yield` 出若干事件对象，把「干活」和「汇报」交织在同一条时间线上。引擎侧的 `for event in self._ocr.recognize(...)` 循环每收到一个事件，就转发给用户的 `on_ocr_event` 回调并累计计量——这就是 u2-l2 里那个只读观测钩子的全部实现原理。

用一个比喻：`recognize` 像一条流水线上的工人，每装完一个零件就朝窗外喊一声「第 N 页装完了，用时 X 毫秒」；你可以站在窗外拿本子记录（`on_ocr_event`），也可以随时拍玻璃叫停（`aborted`），但你的记录动作不会让流水线变慢或停顿。

#### 4.1.2 核心流程

`recognize` 对每一页最多产生三个事件，顺序固定：

```text
对 PDF 的每一页 ref：
  ├─ 检查 aborted() → 为真则抛 AbortError（整条流水线停止）
  ├─ yield START                    # 一定会发：即将处理第 N 页
  ├─ 若 N 不在 page_indexes 范围内：
  │    yield IGNORE                 # 该页被范围排除，直接跳到下一页
  ├─ 否则若缓存判定命中（见 4.2）：
  │    yield SKIP                   # 该页已有结果，不再调用 OCR
  └─ 否则（真正执行 OCR）：
       渲染图像
       yield RENDERED              # 图像已就绪，即将送入识别
       调用 image2page 识别
       yield COMPLETE 或 FAILED    # 识别成功 / 失败（附异常与 token 数）
```

归纳成一张速查表：

| 事件 | 产生时机 | 携带的关键字段 | 对计量/可用页的贡献 |
| --- | --- | --- | --- |
| `START` | 每页循环开头，无条件 | `page_index`、`total_pages` | 无 |
| `IGNORE` | 页码不在 `page_indexes` 内 | 耗时（几乎为 0） | 无（且会阻止 `done` 写入） |
| `SKIP` | 缓存命中（4.2 详述） | 耗时（几乎为 0） | **计入可用页**，token 为 0 |
| `RENDERED` | 页面渲染成图像之后 | 耗时 | 无 |
| `COMPLETE` | 识别成功 | 耗时、`input_tokens`、`output_tokens` | 计入可用页，累计 token |
| `FAILED` | 识别失败但被忽略策略放行 | 耗时、`error` 异常对象 | 计入失败页列表 |

#### 4.1.3 源码精读

先看事件类型的定义——六行枚举加一个数据类，就是整个「观测协议」：

- [pdf_craft/pdf/ocr.py:L22-L28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L22-L28) —— `OCREventKind` 枚举定义了六种事件；注意 `RENDERED` 的存在意味着「渲染」和「识别」是两个独立阶段，渲染失败时你只会看到 `START` 之后直接 `FAILED`，没有 `RENDERED`。
- [pdf_craft/pdf/ocr.py:L31-L39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L31-L39) —— `OCREvent` 数据类：`kind` 之外还有页码、总页数、耗时（毫秒）、输入/输出 token 数与可选的 `error`。`total_pages` 让回调方不必自己数页数就能算进度百分比。

再看每页循环里四个发事件的代码点（按执行顺序）：

- [pdf_craft/pdf/ocr.py:L107-L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L107-L114) —— 循环体第一件事是 `check_aborted(aborted)`，然后无条件 `yield START`。这就是 u2-l2 说的「中止检查点在每页 `START` 之前」的准确位置。
- [pdf_craft/pdf/ocr.py:L115-L124](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L115-L124) —— 页码范围过滤：`ref.page_index not in page_indexes` 就 `yield IGNORE` 并 `continue`。注意两点：判定发生在缓存判定**之前**（范围外的页即使有缓存也只会 `IGNORE`）；`did_ignore_any` 被置真，后面 `done` 标记就不会写（4.2 详述）。
- [pdf_craft/pdf/ocr.py:L150-L165](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L150-L165) —— 先 `ref.render(...)` 渲染（缺省 DPI=300，u3-l2 讲过按文件大小自动压 dpi 的逻辑），把图像尺寸记入 `_last_page_pixel_sizes`，然后 `yield RENDERED`。
- [pdf_craft/pdf/ocr.py:L211-L221](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L211-L221) —— 收尾事件用条件表达式二选一：没有错误发 `COMPLETE`，有错误发 `FAILED`（`error` 字段带上原始异常）。token 数取自识别结果 `page.input_tokens` / `page.output_tokens`。

最后看消费方如何「记账」：

- [pdf_craft/transform.py:L78-L101](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L78-L101) —— 引擎在 `for event in self._ocr.recognize(...)` 循环里做三件事：调用 `on_ocr_event(event)` 转发给用户回调；把事件的 token 累加进 `OCRTokensMetering`；按事件类型维护 `usable_pages` 与 `failed_page_indexes` 两个统计量。关键一行是 `if event.kind in (OCREventKind.COMPLETE, OCREventKind.SKIP): usable_pages += 1`——**SKIP 也算可用页**，因为缓存里的 `page_N.xml` 就是可用结果。

官方文档对这六种事件也有一份对照表，可作为速记：

- [docs/zh-CN/TROUBLESHOOTING.md:L130-L143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/zh-CN/TROUBLESHOOTING.md#L130-L143) —— 「用 OCR 事件定位具体页面」一节，逐条解释六种事件并强调「事件回调只用于观测，不会自动修复失败页面」。

#### 4.1.4 代码实践

**实践目标**：写一个事件记录器，把一次转换的完整事件流按时间顺序打印出来，直观看到每页的事件序列与 token 消耗。

**操作步骤**（以下脚本为示例代码，基于 `tests/test_craft.py` 中验证过的 API 组合方式编写）：

```python
# ocr_event_log.py（示例代码）
from collections import Counter
from pathlib import Path

from pdf_craft import PDFCraft, PDFOptions, ExtractionOptions, OCREvent
from pdf_craft.ocr_config import DeepSeekOCRVendorConfig

PDF = Path("small_book.pdf")  # 换成你本地一个几页的小 PDF

def on_event(event: OCREvent) -> None:
    print(
        f"page {event.page_index}/{event.total_pages}"
        f"  {event.kind.name:<9}"
        f"  {event.cost_time_ms:>6} ms"
        f"  in={event.input_tokens} out={event.output_tokens}"
        + (f"  error={event.error!r}" if event.error else "")
    )

counts: Counter[str] = Counter()
def counting(event: OCREvent) -> None:
    counts[event.kind.name] += 1
    on_event(event)

metering = PDFCraft(pdf=PDFOptions(
    ocr=DeepSeekOCRVendorConfig(
        base_url="https://你的OCR服务",   # 换成真实 endpoint
        api_key="你的key",
        model="你的模型名",
    ),
)).convert_pdf_to_markdown(
    PDF, Path("out.md"),
    package_path=Path("pkg"),            # 保留中间包，下一阶段还要用
    extraction=ExtractionOptions(on_ocr_event=counting),
)
print("事件统计:", dict(counts))
print("总计量:", metering)               # OCRTokensMetering(input_tokens=…, output_tokens=…)
```

**需要观察的现象**：

1. 每一页打印的序列应该是 `START → RENDERED → COMPLETE` 三个事件一组。
2. `COMPLETE` 事件的 token 之和应等于脚本结尾 `metering.input_tokens + metering.output_tokens`。
3. 事件统计里没有 `IGNORE`/`SKIP`/`FAILED`（首次全量运行、无范围过滤、无失败）。

**预期结果**：若 PDF 有 3 页，则 `START`、`RENDERED`、`COMPLETE` 各出现 3 次，共 9 行日志；`metering` 的两个 token 数大于 0。具体数值与 OCR 服务相关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：某页的日志只出现了 `START` 和 `FAILED`（没有 `RENDERED`），最可能失败在哪一步？如果出现了 `START`、`RENDERED`、`FAILED` 呢？

**答案**：`RENDERED` 在渲染成功后才发出。没有 `RENDERED` 说明 `ref.render(...)` 阶段就抛了 `PDFError`（如 Poppler 渲染失败，u3-l2 讲过底层异常会被包装为带页码的 `PDFError`）；有 `RENDERED` 再 `FAILED` 说明图像渲染成功、是 `image2page` 识别阶段抛了 `OCRError`。

**练习 2**：为什么引擎统计可用页时要把 `SKIP` 也算进去？

**答案**：`SKIP` 的语义是「`page_N.xml` 已存在且无失败标记」，即该页早已成功识别、结果在磁盘上。对后续的目录分析和章节生成而言，缓存结果与新识别结果没有区别，所以它是可用页；而 `IGNORE` 的页根本没有结果文件，不能算。

**练习 3**：`on_ocr_event` 回调里抛出一个异常，会发生什么？

**答案**：回调在引擎的消费循环里同步执行（[pdf_craft/transform.py:L95](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L95) `on_ocr_event(event)`），异常会沿生成器的消费循环向上传播，中断整个提取。所以回调应当只做轻量的记录动作，并自行捕获可能的异常。

### 4.2 断点续跑缓存：page_N.xml、.failed 与 done 三件套

#### 4.2.1 概念说明

OCR 一本几百页的书要花不少钱和时间。如果第 280 页时断网、或用户主动中止、或 token 预算耗尽，重跑时再从头识别前 279 页就是纯粹的浪费。pdf-craft 的解法非常朴素：**识别结果本来就以 XML 文件形式落盘，那么「文件是否存在」本身就是缓存**，不需要额外引入缓存数据库。

`ocr/` 目录下有三类文件共同构成缓存协议：

| 文件 | 含义 | 写入时机 | 删除时机 |
| --- | --- | --- | --- |
| `page_N.xml` | 第 N 页的识别结果（`Page` 结构序列化） | 每页识别结束**无条件**写入（含失败兜底页） | 用户手动删 |
| `page_N.failed` | 第 N 页上次识别失败的标记，内容是异常类名 | 识别失败且被忽略策略放行时 | 该页下次识别**成功**时自动删除 |
| `done` | 「全部页都成功处理完」的哨兵 | 全程无 `IGNORE` 且无 `FAILED` 时 `touch` 一次 | 不会自动删除 |

第三行的条件值得咀嚼：**只要有页被范围排除（`IGNORE`）或失败（`FAILED`），`done` 就不写**。为什么？因为 `done` 的语义是「这个包已经完整了，下次连 `page_N.xml` 都不用看」。而范围过滤只处理了部分页（用户下次可能扩大范围）、失败页还等着重试（下次应该重跑），这两种情况下「继续逐页检查缓存」才是正确行为。

#### 4.2.2 核心流程

把缓存判定整理成决策表（`recognize` 开头与每页循环各有一半）：

```text
recognize 启动时：
  若 ocr/done 存在 且 ocr/ 下没有任何 page_*.failed 文件：
      直接 return —— 不 yield 任何事件（连 START 都没有）
      # 注意：若 done 存在但同时有 .failed 文件，仍会逐页走缓存判定

对每一页 N：
  N 不在 page_indexes ──────────────→ IGNORE（不看缓存）
  page_N.xml 存在 且 page_N.failed 不存在 ─→ SKIP（缓存命中，不调 OCR）
  否则（无 xml，或有 xml 但 .failed 也在）──→ 重新识别该页
      识别成功 → 删除 page_N.failed，写 page_N.xml → COMPLETE
      识别失败 → 写 page_N.failed（内容为异常类名），仍写 page_N.xml（兜底内容）→ FAILED
```

由此可以推出三个实用的行为结论：

1. **完全成功的包重跑是「零事件」的**——`done` 短路让生成器直接结束，`on_ocr_event` 一次都不会被调用，返回的 `OCRTokensMetering` 全为 0。这是缓存生效最强的证据。
2. **失败页永远不会被 SKIP**——`.failed` 的存在使缓存判定失效，即使 `page_N.xml` 在（兜底内容），该页也会重新识别，成功后 `.failed` 被自动清掉。
3. **中断的包重跑是「增量」的**——已完成页 `SKIP`（token 为 0、零成本），只有缺失页真正调用 OCR。

#### 4.2.3 源码精读

- [pdf_craft/pdf/ocr.py:L91-L95](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L91-L95) —— `done` 短路：`done_path.exists() and not any(ocr_path.glob("page_*.failed"))` 时直接 `return`。`any(...)` 配合 glob 只要有任何一个失败标记就会让短路失效。
- [pdf_craft/pdf/ocr.py:L126-L137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L126-L137) —— 每页的缓存判定：`filename = f"page_{ref.page_index}.xml"` 与 `failure_path = ocr_path / f"page_{ref.page_index}.failed"` 两个路径先算好，`file_path.exists() and not failure_path.exists()` 命中即 `yield SKIP`。
- [pdf_craft/pdf/ocr.py:L196-L204](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L196-L204) —— 失败标记的写入与清除：失败时把 `type(recognized_error).__name__`（仅异常类名字符串，不含堆栈）写进 `.failed`；成功时 `failure_path.unlink(missing_ok=True)` 删掉可能残留的旧失败标记。紧接着**无论成败**都执行 `save_xml(encode(page), file_path)`——失败页写进去的是 4.3 节讲的兜底 `Page`。
- [pdf_craft/pdf/ocr.py:L229-L230](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L229-L230) —— `done` 的写入条件：`if not did_ignore_any and not did_fail_any: done_path.touch()`。对照 L117（`IGNORE` 时置 `did_ignore_any = True`）与 L197（失败时置 `did_fail_any = True`），即可验证 4.2.1 表格第三行的结论。

除了「结果缓存」，`ocr/` 目录里还有第四个文件 `page_pixel_sizes.json`——页面几何缓存，它与断点续跑关系密切：

- [pdf_craft/pdf/ocr.py:L85-L87](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L85-L87) —— `recognize` 一开始就从 `page_pixel_sizes.json` 加载历史几何到 `_last_page_pixel_sizes`；这样即使本轮全部 `SKIP`（一页都不渲染），几何数据也不会丢。
- [pdf_craft/pdf/ocr.py:L157](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L157) 与 [L205](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L205) —— 每页渲染后更新内存字典，每页落盘时整体写回 JSON。
- [pdf_craft/pdf/ocr.py:L274-L283](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L274-L283) —— 写回采用「先写 `page_pixel_sizes.json.tmp` 再 `replace`」的原子写法：进程若在写盘中途被杀，也不会留下半截 JSON；下轮 `_load_page_pixel_sizes`（L245-L272）读取时会校验格式，坏文件直接报 `ValueError` 而不是静默吞掉。
- [pdf_craft/transform.py:L116](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L116) —— 引擎收尾时用「提取前已有的几何 ∪ 本轮的几何」合并写入 `document.json`，保证部分重跑不会抹掉其他轮次的几何数据（u3-l1 讲过这个并集合并，此处即其数据来源）。

#### 4.2.4 代码实践

**实践目标**：用同一个 `package_path` 连续运行四次转换，观察 `done`、`page_N.xml` 两级缓存如何把第二次之后的运行成本压到接近零；再通过删除单个 `page_N.xml` 验证「只有该页重新 OCR」。

一个重要的预期修正：如果你以为「第二次运行会看到满屏 `SKIP` 事件」，实际会**一个事件都看不到**——因为首次全量成功后 `done` 已写入，生成器在 L94-L95 直接短路返回。`SKIP` 只有在 `done` 不存在时才会出现。本实践正是要把这两个层级都观察到。

**操作步骤**（示例代码，四个阶段合成一个脚本或分四次运行均可）：

```python
# resume_probe.py（示例代码）
from collections import Counter
from pathlib import Path

from pdf_craft import PDFCraft, PDFOptions, ExtractionOptions, OCREvent
from pdf_craft.ocr_config import DeepSeekOCRVendorConfig

PDF, PACKAGE = Path("small_book.pdf"), Path("pkg")
craft = PDFCraft(pdf=PDFOptions(ocr=DeepSeekOCRVendorConfig(
    base_url="https://你的OCR服务", api_key="你的key", model="你的模型名",
)))

def run(label: str) -> None:
    counts: Counter[str] = Counter()
    metering = craft.convert_pdf_to_markdown(
        PDF, Path(f"out-{label}.md"),
        package_path=PACKAGE,
        extraction=ExtractionOptions(on_ocr_event=lambda e: counts.update([e.kind.name])),
    )
    print(f"[{label}] 事件={dict(counts)}"
          f"  tokens={metering.input_tokens + metering.output_tokens}")

run("1-首次全量")                       # 阶段一
run("2-done短路")                        # 阶段二：什么都不删，直接再跑
(PACKAGE / "ocr" / "done").unlink()      # 阶段三：删除 done 标记
run("3-全SKIP")
(PACKAGE / "ocr" / "page_2.xml").unlink()  # 阶段四：只删第 2 页的结果
run("4-单页重跑")
```

**需要观察的现象与预期结果**（**待本地验证**，以下为按源码推演的预期）：

| 阶段 | 预期事件统计 | 预期 token 合计 | 解释 |
| --- | --- | --- | --- |
| 1 首次全量（3 页 PDF） | `START=3, RENDERED=3, COMPLETE=3` | > 0 | 全部页真正识别，`done` 写入 |
| 2 done 短路 | `{}`（空） | 0 | L94-L95 直接 return，零事件 |
| 3 全 SKIP | `START=3, SKIP=3` | 0 | `done` 没了，逐页检查缓存，3 页全命中 |
| 4 单页重跑 | `START=3, SKIP=2, RENDERED=1, COMPLETE=1` | ≈ 第 2 页的 token | 只有 `page_2.xml` 缺失，第 2 页重识别，另外两页 SKIP |

同时可以在每次运行后 `ls pkg/ocr/` 检查：阶段 4 结束后 `page_2.xml` 重新出现，`done` 再次写入（因为本轮既无 IGNORE 也无 FAILED）。

若手头暂时没有可用的 OCR 凭据，可以改做**源码阅读型实践**：通读 L91-L95、L126-L137、L196-L204、L229-L230 四个代码点，然后不运行代码、仅凭决策表推演「删除 `page_1.xml` 与 `page_3.xml` 后重跑、且第 3 页识别失败」的事件序列（答案见下面练习 3）。

#### 4.2.5 小练习与答案

**练习 1**：用 `page_indexes=range(1, 6)` 提取了前 5 页并成功完成，`ocr/` 目录里会有 `done` 文件吗？之后用 `page_indexes=range(1, 11)` 重跑会发生什么？

**答案**：不会有 `done`——前 5 页之外的页都走了 `IGNORE`，`did_ignore_any` 为真，L229 的条件不成立。重跑时前 5 页因 `page_N.xml` 存在且无 `.failed` 而全部 `SKIP`（零成本），第 6-10 页真正识别；这正是「先试前几页、满意后扩全量」的增量工作流背后的机制。

**练习 2**：某个包里同时存在 `page_4.xml` 和 `page_4.failed`，重跑时第 4 页会走哪条分支？如果这次识别成功，目录会有什么变化？

**答案**：走重新识别分支——L130 的缓存命中要求 `file_path.exists() and not failure_path.exists()`，`.failed` 的存在使条件不满足。识别成功后 L202 会删除 `page_4.failed`，L204 用新的识别结果覆盖 `page_4.xml`（旧文件里只是兜底内容）。

**练习 3**：3 页的包，删除 `page_1.xml` 与 `page_3.xml` 后重跑，且本次第 3 页识别失败（失败被忽略策略放行）。请写出完整事件序列与目录终态。

**答案**：`done` 不存在（上一步删除 xml 前提下它本来也可能不存在，无论如何逐页判定照常进行）。事件序列：第 1 页 `START→RENDERED→COMPLETE`；第 2 页 `START→SKIP`；第 3 页 `START→RENDERED→FAILED`。终态：`page_1.xml`（新结果）、`page_2.xml`（旧缓存）、`page_3.xml`（兜底内容）、`page_3.failed`（异常类名）、无 `done`（存在失败页）。

### 4.3 预算与失败处理：token 扣减、失败页兜底与空包保护

#### 4.3.1 概念说明

**预算**解决的是成本失控问题。`max_tokens`（用户侧叫 `max_ocr_tokens`）是「输入 + 输出 token」的总预算，`max_output_tokens`（用户侧叫 `max_ocr_output_tokens`）是只约束输出 token 的独立预算，两者可以同时生效。预算跨页**累计**：每页识别完，从剩余额度里扣掉该页消耗；进入下一页前若额度已耗尽，立即抛 `TokenLimitError` 中止——宁可停下来让用户决定，也不悄悄超支。

**失败处理**解决的是鲁棒性问题。一页渲染失败或识别失败，是否要报废整本书？pdf-craft 的策略分两层：

- 错误默认向上抛（快速失败）；但如果用户通过 `ignore_pdf_errors` / `ignore_ocr_errors` 说「这类错可以忽略」，该页就转为「兜底页」继续前进；
- 兜底页要么是「整页截图」版式（图像能渲染出来时），要么是一行占位文本；它照样写 `page_N.xml`、照样发 `FAILED` 事件、照样写 `.failed` 标记等待下次重试；
- 极端情况——所有请求的页都失败了——引擎最后会抛 `NoUsableOCRPagesError` 拒绝产出空包。

#### 4.3.2 核心流程

预算的数学模型很简单，设第 \( i \) 页消耗输入 \( a_i \)、输出 \( b_i \) 个 token：

\[ \text{remain\_tokens} = \text{max\_tokens} - \sum_{i \in \text{已识别页}} (a_i + b_i) \]

\[ \text{remain\_output\_tokens} = \text{max\_output\_tokens} - \sum_{i \in \text{已识别页}} b_i \]

每个新页开始 OCR 前检查（L141-L144）：

\[ \text{remain\_tokens} \le 0 \ \text{或} \ \text{remain\_output\_tokens} \le 0 \ \Rightarrow \ \text{raise TokenLimitError} \]

失败处理的路径：

```text
识别一页时抛出异常：
  ├─ PDFError（渲染/文档层）且 ignore_pdf_errors 判定可忽略 → 记为 recognized_error
  ├─ OCRError（识别层）且 ignore_ocr_errors 判定可忽略     → 记为 recognized_error
  ├─ 其他异常，或检查器返回 False                            → 原样上抛，提取终止
  可忽略时：
    page 为 None → _create_fallback_page 兜底
    写 page_N.failed + 写 page_N.xml（兜底内容） → yield FAILED
循环结束后（引擎侧）：
  若存在失败页 且 可用页数为 0 → 抛 NoUsableOCRPagesError(失败页码元组)
```

#### 4.3.3 源码精读

预算的三段式：

- [pdf_craft/pdf/ocr.py:L97-L98](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L97-L98) —— 两个剩余额度变量初始化为各自的预算上限；预算为 `None` 表示不设限。
- [pdf_craft/pdf/ocr.py:L139-L144](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L139-L144) —— 页级预检：进入识别分支（非 SKIP）后、渲染之前，任一剩余额度 ≤ 0 即抛 `TokenLimitError`。注意 `TokenLimitError` 是在这里从 `doc_page_extractor.extraction_context` 懒加载导入的——保持上游库懒加载的同时复用其异常类型。
- [pdf_craft/pdf/ocr.py:L166-L178](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L166-L178) 与 [pdf_craft/pdf/page_extractor.py:L195-L200](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L195-L200) —— 剩余额度（而非原始上限）作为 `max_tokens` 传给 `image2page`，再装进上游的 `ExtractionContext`，让页内识别也能感知「全书还剩多少额度」。
- [pdf_craft/pdf/ocr.py:L222-L227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L222-L227) —— 识别成功后的扣减：总预算同时扣输入与输出，输出预算只扣输出。

失败处理链：

- [pdf_craft/pdf/ocr.py:L179-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L179-L187) —— 两类异常分别交给 `_check_ignore_error` 判定。对照 [pdf_craft/pdf/ocr.py:L324-L328](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L324-L328)：检查器是布尔就取其值，是可调用对象就 `check(error)`——这正是 u2-l2 讲过的「布尔或谓词」双形态。判定不忽略则 `raise` 原样上抛。
- [pdf_craft/pdf/ocr.py:L189-L194](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L189-L194) 与 [L285-L318](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L285-L318) —— 兜底页的构造：图像渲染成功时把整页截图裁剪进资产库、生成一个 `ref="image"` 的版式（读者至少能看到原页图像）；连图像都没有时退化为 `ref="text"` 的占位文本 `[[Page N extraction failed due to PDF rendering error]]`。兜底页的 token 计量为 0。
- [pdf_craft/transform.py:L103-L104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L103-L104) 与 [pdf_craft/error.py:L19-L28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L19-L28) —— 空包保护：`failed_page_indexes and usable_pages == 0` 时抛 `NoUsableOCRPagesError`，异常对象携带全部失败页码，消息明确说明「所有请求页在忽略错误后都失败了」。

最后是 `metering.py` 里三个小而重要的定义：

- [pdf_craft/metering.py:L5-L12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L5-L12) —— `AbortedCheck = Callable[[], bool]` 与 `check_aborted`：回调返回真时抛上游的 `AbortError`（同样是懒加载导入）。它是「协作式中止」：驱动器只在每页开头这一处检查，长页识别内部由 `ExtractionContext` 继续检查（L177 把 `aborted` 一并传给了 `image2page`）。
- [pdf_craft/metering.py:L15-L18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L15-L18) —— `OCRTokensMetering` 就是两个 int 的数据类，作为 `convert_pdf_to_*` 的返回值（u1-l4 讲过）。
- [pdf_craft/metering.py:L21-L23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L21-L23) —— `InterruptedKind` 的两个成员 `ABORT` 与 `TOKEN_LIMIT_EXCEEDED` 对应两种中断来源。

关于中断异常的传播，官方文档有一句必须记住的说明：

- [docs/zh-CN/TROUBLESHOOTING.md:L147-L149](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/zh-CN/TROUBLESHOOTING.md#L147-L149) —— 中止抛 `AbortError`、预算耗尽抛 `TokenLimitError`，**它们不会自动转换成 pdf-craft 导出的 `InterruptedError`**，除非调用方显式使用 [pdf_craft/error.py:L57-L79](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L57-L79) 的 `to_interrupted_error` 助手（它会把这两种异常包成携带 `InterruptedKind` 与 `OCRTokensMetering` 的 `InterruptedError`）。

#### 4.3.4 代码实践

**实践目标**：用一个故意调小的 token 预算触发 `TokenLimitError`，观察「中断不破坏缓存」——已完成的页留在磁盘上，提高预算重跑时只需补齐剩余页。

**操作步骤**（示例代码）：

```python
# budget_probe.py（示例代码）
from pdf_craft import PDFCraft, PDFOptions, ExtractionOptions
from pdf_craft.error import to_interrupted_error
from pdf_craft.ocr_config import DeepSeekOCRVendorConfig

PDF, PACKAGE = Path("small_book.pdf"), Path("pkg_budget")
craft = PDFCraft(pdf=PDFOptions(ocr=DeepSeekOCRVendorConfig(
    base_url="https://你的OCR服务", api_key="你的key", model="你的模型名",
)))

try:
    craft.convert_pdf_to_markdown(
        PDF, Path("out.md"), package_path=PACKAGE,
        extraction=ExtractionOptions(max_ocr_tokens=100),  # 故意小到一两页就耗尽
    )
except Exception as error:
    print("原始异常类型:", type(error).__name__)
    translated = to_interrupted_error(error)
    if translated is not None:
        print("转换后:", translated.kind, translated.metering)
```

**需要观察的现象**：

1. 捕获到的原始异常类型是 `TokenLimitError`（来自上游 `doc_page_extractor`，不是 pdf-craft 自己的类）。
2. `to_interrupted_error` 应返回非 `None`，`kind` 为 `InterruptedKind.TOKEN_LIMIT_EXCEEDED`，`metering` 里是中断前已消耗的 token。
3. 检查 `pkg_budget/ocr/`：预算耗尽前已完成的页都有 `page_N.xml`，且**没有** `done` 文件。

**预期结果**：去掉 `max_ocr_tokens`（或调大）后用同一 `PACKAGE` 重跑，事件流应为「前几页 `SKIP` + 剩余页 `START→RENDERED→COMPLETE`」，总 token 消耗约等于预算耗尽那次 + 新识别页之和。具体在第几页耗尽取决于页面内容，**待本地验证**。

若暂无 OCR 凭据，可改做源码阅读型实践：在 L141-L144 处推演「预算恰好在本页 OCR 后扣到 0」与「扣到 -50」两种情形分别在哪一页抛出（答案：都在**下一页**进入识别分支时抛，预检在渲染之前，与剩余多少无关，只看是否 ≤ 0）。

#### 4.3.5 小练习与答案

**练习 1**：`max_tokens=1000`，前两页各消耗（输入+输出）300 与 450。第三页会开始识别吗？

**答案**：前两页扣完后 `remain_tokens = 1000 - 750 = 250 > 0`，预检通过，第三页**会**开始识别；且传给第三页的 `max_tokens` 是 250。若第三页消耗超过 250，页内识别会由上游 `ExtractionContext` 抛 `TokenLimitError`（或者第四页预检时抛），取决于具体实现路径——总之中断一定发生在预算实际耗尽处，不会超支太多。

**练习 2**：一页渲染成功但识别失败，且被 `ignore_ocr_errors=True` 放行。写出该页的事件序列、写盘文件与 `page.input_tokens` 的来源。

**答案**：事件 `START → RENDERED → FAILED`（无 `COMPLETE`）；写盘 `page_N.failed`（内容为异常类名）与 `page_N.xml`（兜底内容：整页截图版式，因为图像已渲染成功）；该页 `page` 是 `_create_fallback_page` 的产物，`input_tokens=0, output_tokens=0`，所以本页不扣预算。

**练习 3**：为什么 `NoUsableOCRPagesError` 的判定放在引擎（消费侧）而不是驱动器（生产侧）？

**答案**：判定条件是「存在失败页 **且** 可用页为 0」，其中「可用页」的计数规则（`COMPLETE` 与 `SKIP` 都算）是在引擎的消费循环里维护的（[pdf_craft/transform.py:L98-L104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L98-L104)）。驱动器只负责逐页报告，不掌握全局统计；把「全书级」的判断放在汇总方，职责更清晰——这也是生产者/消费者分层的一个典型例子。最近的提交 bbb2d20（"reject extraction with no usable OCR pages"）正是强化了这道保护。

## 5. 综合实践

**任务：打造一个「断点续跑观察器」**，把本讲三个模块串成一件事——像运维面板一样回答「这次运行哪些页真正花了钱」。

要求实现一个函数 `probe(pdf_path, package_path, runs=3)`：

1. 每轮运行都注册 `on_ocr_event` 回调，按事件类型计数，并单独记录每个 `COMPLETE`/`FAILED` 页的 `(page_index, cost_time_ms, input_tokens, output_tokens)` 明细。
2. 每轮运行后打印一行摘要：事件计数、总 token（用返回的 `OCRTokensMetering` 核对与明细求和是否一致）、`ocr/` 目录下 `page_*.xml`、`page_*.failed`、`done` 的文件个数。
3. 在第 1、2 轮之间不做任何事（观察 `done` 短路）；在第 2、3 轮之间删除 `done` 与任意一页的 `page_N.xml`（观察全 `SKIP` 加单页补跑）。
4. 附加题：再跑第 4 轮，用 `ExtractionOptions(max_ocr_tokens=...)` 给一个仅够一页的小预算，捕获异常并用 `to_interrupted_error` 打印 `kind` 与中断前的计量，然后恢复预算跑第 5 轮，验证失败/中断的页被补齐。

验收标准（按源码推演，**待本地验证**）：

- 第 2 轮：零事件、零 token、`done` 仍存在；
- 第 3 轮：`SKIP = 总页数 - 1`，恰好 1 个 `COMPLETE`，token 只有该页的量；
- 第 5 轮：全部页就位，`done` 重新出现，全程没有重复为同一页付过两次 OCR 钱（每页的 `COMPLETE` 明细在所有轮次里至多出现一次，第 3 轮那一页除外——它被你手动删过缓存）。

这个观察器本身不到 60 行，但它逼你同时使用事件流（模块一）、缓存判定（模块二）与预算/中断处理（模块三），做完后你对 `recognize` 的行为就有了可复现的实证把握。

## 6. 本讲小结

- `OCR.recognize` 是一个生成器，逐页产出六种 `OCREvent`（`START`/`IGNORE`/`SKIP`/`RENDERED`/`COMPLETE`/`FAILED`）；每页序列为 `START → (IGNORE | SKIP | (RENDERED → COMPLETE/FAILED))`，`RENDERED` 把渲染与识别分成两个可区分的失败阶段。
- 引擎侧消费循环把 `COMPLETE` 与 `SKIP` 都计为可用页、累计 token 进 `OCRTokensMetering`，并在「有失败页且可用页为 0」时抛 `NoUsableOCRPagesError` 拒绝空包。
- 断点续跑靠 `ocr/` 目录的三件套：`page_N.xml` 是结果缓存（存在且无失败标记即 `SKIP`）、`page_N.failed` 让失败页永远重试（成功后自动删除）、`done` 是全书完成哨兵（有 `IGNORE` 或 `FAILED` 就不写；存在且无失败标记时重跑零事件）。
- token 预算是双轨累计扣减（总预算扣输入+输出、输出预算只扣输出），页级预检在渲染前抛 `TokenLimitError`；剩余额度还会传入页内 `ExtractionContext`。
- 被忽略的失败页不报废整本书：构造兜底 `Page`（整页截图或占位文本）、照常落盘并标记 `.failed`，token 计量为 0。
- `aborted` 协作式中止在每页 `START` 前检查；`AbortError`/`TokenLimitError` 不会自动转为 `InterruptedError`，需要调用方显式用 `to_interrupted_error` 转换。

## 7. 下一步学习建议

本讲结束时，事件流里还留着一个黑盒：`RENDERED` 之后的 `self._extractor.image2page(...)` 到底怎么把一张位图变成带 `body_layouts`/`footnotes_layouts` 的 `Page`？这正是下一讲 **u3-l4《页面提取后端：doc-page-extractor 适配》**的主题——六种 OCR 配置到具体后端的 `isinstance` 分派、本地运行时缺失时的可操作安装提示、以及上游布局类型到 `PageLayout` 的归一化映射。

若想先横向扩展，可以：

- 对照阅读 [docs/zh-CN/TROUBLESHOOTING.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/zh-CN/TROUBLESHOOTING.md) 全文，其中「处理过程中被中断」「用 OCR 事件定位具体页面」两节与本讲互为印证；
- 回看 [pdf_craft/pdf/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py) 的 `encode`/`decode`，弄清 `page_N.xml` 里的 XML 长什么样——第 5 单元（章节生成）会大量消费这些文件；
- 用 `git log --oneline -- pdf_craft/pdf/ocr.py` 浏览该文件的演进史，留意 bbb2d20（空包保护）等提交，体会「事件流 + 缓存」这套机制是如何在真实问题驱动下逐步长出来的。
