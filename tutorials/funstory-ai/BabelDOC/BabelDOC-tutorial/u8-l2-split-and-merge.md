# 分片翻译与结果合并：SplitManager 与 ResultMerger

## 1. 本讲目标

本讲聚焦 BabelDOC 处理「超长 PDF」时的工程化能力——**分片翻译（split translation）**。

学完本讲，你应当能够：

- 说清 `--max-pages-per-part` 是如何从 CLI 参数一路变成流水线里的「分片动作」的。
- 读懂 `SplitManager` 与 `PageCountStrategy` 如何把一份多页 PDF 切成若干 `SplitPoint`。
- 理解 `do_translate` 中分片串行循环的结构：每片独立 `part_config`、独立临时 PDF、独立 `part_monitor`，但共享同一份 `SharedContextCrossSplitPart`。
- 解释「首片独占」设计：为什么只有第 0 片做扫描检测、只有第 0 片打水印，以及 OCR 决定如何跨片广播。
- 跟踪 `ResultMerger.merge_results` 如何把多片 PDF 拼回一份 mono/dual PDF，并导出全文档级别的术语表与耗时统计。

本讲依赖你已经掌握 u2-l2（翻译主流程编排 `do_translate` / `_do_translate_single`）与 u7-l3（`PDFCreater` 后端的字体子集化与保存）。本讲可以看作对 `do_translate` 中「分片分支」的专题展开。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要把一份 PDF 切开翻译？**

`_do_translate_single`（u2-l2）是一条「把整份 PDF 一次性吃进去」的流水线。对于一份几百页的论文集或书籍，这条流水线会同时面临三个压力：

1. **内存**：每一页都会在内存里维护一棵带坐标的 IL 对象树，并叠加版面、段落、字体等中间结果，页数越多占用越大。
2. **失败成本**：整份一次性翻译，如果在第 300 页崩了，前面 299 页的工作全部作废。
3. **进度可观测**：长任务需要分阶段上报进度，分片天然提供了一个「已完成的片数」这个粗粒度刻度。

分片翻译的思路很朴素：**把一份大 PDF 按页数切成若干小片，每片单独走一遍 `_do_translate_single`，最后把各片的结果 PDF 拼回去**。这是典型的「分而治之」工程手法。

**但是切片会带来两个必须解决的问题：**

- **片间一致性**：翻译不是逐页独立的。比如 u6-l4 的「自动术语抽取」希望对**整份文档**统一术语译法，u6-l3 的「标题上下文」要记住全文第一个标题。如果每片各自独立抽取术语，就会出现「第 1 片把 Transformer 译成变换器，第 2 片译成 Transformer」的不一致。因此必须有一个**跨片共享的容器**。
- **重复工作的剔除**：有些工作「整份只需要做一次」。扫描件检测（u5-l1）只需在第一片做一次、结果对全文档生效；水印（BabelDOC 的署名标记）只应该出现在最终 PDF 的第一片对应位置，不应每片都重复打。这就引出了「首片独占」的设计。

**三个关键词，贯穿本讲：**

| 关键词 | 含义 | 出现位置 |
|--------|------|----------|
| `SplitPoint` | 一个分片的页范围（`start_page` / `end_page`，闭区间） | `split_manager.py` |
| `part_config` | 每片的「浅拷贝配置」，承载该片专属的页范围、工作目录等 | `high_level.py` 分片循环 |
| `shared_context_cross_split_part` | 跨片共享的「全局记忆」容器，所有片指向**同一个对象** | `translation_config.py` |

理解了这三点，本讲剩余内容就是在源码里逐一验证它们。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 作用 |
|------|------|
| [`babeldoc/format/pdf/split_manager.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/split_manager.py) | 定义 `SplitPoint`、`BaseSplitStrategy`、`PageCountStrategy`、`SplitManager`——决定「在哪里切」。 |
| [`babeldoc/format/pdf/result_merger.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py) | 定义 `ResultMerger`——把各片 PDF 拼回一份。 |
| [`babeldoc/format/pdf/high_level.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | `do_translate` 的分片分支、`_do_translate_single` 的首片独占逻辑。 |
| [`babeldoc/format/pdf/translation_config.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | `SharedContextCrossSplitPart`（跨片共享容器）、`create_max_pages_per_part_split_strategy`、`get_part_working_dir` 等分片目录方法。 |
| [`babeldoc/main.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | `--max-pages-per-part` CLI 参数注册与转 `split_strategy`。 |
| [`babeldoc/progress_monitor.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py) | `create_part_monitor` 与分片进度偏移（`part_offset`）。 |

调用关系一句话概括：

```
main.py (--max-pages-per-part)
  └─> TranslationConfig.create_max_pages_per_part_split_strategy  造 PageCountStrategy
        └─> high_level.do_translate  检测到 split_strategy
              ├─> SplitManager.determine_split_points   算出 SplitPoint 列表
              ├─> for 每片: 浅拷贝 part_config + 切片 PDF + _do_translate_single
              └─> ResultMerger.merge_results  拼回 mono/dual PDF
```

## 4. 核心概念与源码讲解

### 4.1 切分点策略：PageCountStrategy 与 SplitManager

#### 4.1.1 概念说明

「在哪里切」本身是一个可以替换的策略（strategy）。BabelDOC 把它抽象成一个策略类层级：

- `BaseSplitStrategy`：抽象基类，只规定「给我一个 `config`，返回一个 `SplitPoint` 列表」。
- `PageCountStrategy`：目前唯一的实现——**按页数切**。

这样设计的目的是为将来留扩展口：比如可以写一个「按章节切」的策略（用 `SplitPoint.chapter_title`），或「按复杂度切」的策略（用 `SplitPoint.estimated_complexity`），而不必改动 `do_translate` 的循环。`SplitPoint` 里已经预留了 `estimated_complexity` 和 `chapter_title` 两个字段，只是 `PageCountStrategy` 暂时没有用到。

#### 4.1.2 核心流程

`PageCountStrategy.determine_split_points` 的逻辑非常直接：

1. 用 PyMuPDF 打开 PDF，读出总页数 `total_pages`。
2. 维护一个游标 `current_page` 从 0 开始。
3. 每次取 `[current_page, current_page + max_pages_per_part)` 这一段，右端用 `min(..., total_pages)` 钳住。
4. 造一个 `SplitPoint(start_page=current_page, end_page=end_page - 1)`——注意 `end_page` 字段是**闭区间右端**，所以减 1。
5. 游标跳到 `end_page`，重复直到覆盖全部页。

伪代码：

```
current = 0
while current < total_pages:
    end = min(current + max_pages_per_part, total_pages)
    切片 = (current, end - 1)   # 闭区间
    current = end
```

举例：一份 53 页的 PDF，`max_pages_per_part=20`，会切成 3 片：`(0,19)`、`(20,39)`、`(40,52)`。

#### 4.1.3 源码精读

`SplitPoint` 是一个简单的 `@dataclass`，两个核心字段加两个预留字段：

[split_manager.py:L7-L14](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/split_manager.py#L7-L14)

> 定义分片点：`start_page` / `end_page` 是页索引（从 0 开始的闭区间），`estimated_complexity` 与 `chapter_title` 当前未被 `PageCountStrategy` 使用，留作未来策略扩展。

`PageCountStrategy` 的全部切分逻辑，核心就是一个 `while` 循环按固定步长切：

[split_manager.py:L30-L49](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/split_manager.py#L30-L49)

> 注意第 44 行的注释 `# end_page is inclusive`——这是本模块最容易踩坑的地方：`end_page = end_page - 1` 把右端点转成闭区间，下游 `high_level.py` 里所有 `range(start_page, end_page + 1)` 都依赖这个约定。

`SplitManager` 本身只是一个薄壳，把策略对象持有起来并转发调用：

[split_manager.py:L52-L67](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/split_manager.py#L52-L67)

> `SplitManager.__init__` 直接从 `config.split_strategy` 取策略对象，`determine_split_points` 只是转发。`estimate_part_complexity` 是一个按页数估算的占位方法，目前流水线主链路并未调用它。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `PageCountStrategy` 的切分行为，理解闭区间约定。

**操作步骤**：

1. 在项目根目录启动一个 Python（确保已 `uv` 安装 BabelDOC，可 `uv run python`）。
2. 运行下面的「示例代码」（非项目原有代码）：

```python
# 示例代码：观察 PageCountStrategy 如何切分
from babeldoc.format.pdf.split_manager import PageCountStrategy
from babeldoc.format.pdf.translation_config import TranslationConfig

# 用 examples/ci/test.pdf 作为输入构造一个最小 config（其余字段仅占位）
class FakeCfg:  # PageCountStrategy 只用到 config.input_file
    def __init__(self, path):
        self.input_file = path

cfg = FakeCfg("examples/ci/test.pdf")
strategy = PageCountStrategy(max_pages_per_part=2)
for sp in strategy.determine_split_points(cfg):
    print(sp.start_page, sp.end_page)
```

3. 用 PyMuPDF 读出 `test.pdf` 的真实页数，对照输出验证：页数 N、步长 2 时，最后一片是 `(N-2 的偶数对齐, N-1)`。

**需要观察的现象**：每个 `SplitPoint` 的 `end_page - start_page + 1` 应当等于 `max_pages_per_part`（最后一片可能更小）；且第 i 片的 `start_page` 等于第 i-1 片的 `end_page + 1`，即片与片首尾相接、无重叠无遗漏。

**预期结果**：闭区间约定下，所有片连起来恰好覆盖 `[0, total_pages-1]`。如果你的输出出现页号重叠或跳号，说明对「闭区间右端 = end_page - 1」的理解有误。

**待本地验证**：`test.pdf` 的具体页数请以本地运行结果为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `max_pages_per_part` 设成一个大于总页数的值（比如 PDF 只有 5 页，设成 100），`determine_split_points` 返回什么？`do_translate` 会怎么处理？

**答案**：返回**只有一个元素**的列表 `[(0, 4)]`。在 `do_translate` 中会进入 `len(split_points) == 1` 分支，回退到普通的 `_do_translate_single`，等价于不分片。

**练习 2**：`SplitPoint.estimated_complexity` 字段当前是否影响切分？为什么还要保留它？

**答案**：不影响。`PageCountStrategy` 只按页数切，从不设置 `estimated_complexity`（用默认值 `1.0`）。保留它是为未来「按复杂度切」的策略预留接口，体现策略模式的可扩展性。

---

### 4.2 分片串行翻译：do_translate 的分片循环

#### 4.2.1 概念说明

切分点算出来之后，真正「按片翻译」的逻辑在 `high_level.do_translate` 里。本模块要讲清三件事：

1. **何时进入分片分支**：`config.split_strategy` 不为 `None` 时。
2. **每片如何隔离**：用 `copy.copy`（浅拷贝）造一个 `part_config`，给它独立的页范围、工作目录、输出目录、临时输入 PDF。
3. **每片如何串行**：`for i, split_point in enumerate(split_points)` 一个一个跑，结果收集进 `results: dict[int, TranslateResult]`，最后交给 `ResultMerger`。

「串行」是这里的重点——分片翻译**不是**把多片丢进线程池并发跑，而是**一片跑完再跑下一片**。这与翻译器内部用 `PriorityThreadPoolExecutor` 做的段级并发（u6-l2）是两层不同的并发：分片是外层串行，片内才是线程池并发。

#### 4.2.2 核心流程

`do_translate` 的顶层分支结构：

```
if not config.split_strategy:        # 没开分片
    result = _do_translate_single(pm, config)
else:
    split_points = SplitManager(config).determine_split_points(config)
    if not split_points:             # 策略没切出片 → 回退单文档
        result = _do_translate_single(pm, config)
    elif len(split_points) == 1:     # 只切出一片 → 回退单文档
        result = _do_translate_single(pm, config)
    else:                            # 真正的多片
        results = {}
        for i, sp in enumerate(split_points):
            part_config = 浅拷贝 + 改写
            切片 PDF 存成 input.part{i}.pdf
            part_monitor = pm.create_part_monitor(i, total)
            results[i] = _do_translate_single(part_monitor, part_config)
        result = ResultMerger(config).merge_results(results)
```

每片循环内部对 `part_config` 做的改写（按执行顺序）：

1. `part_config.skip_clean = True`（跳过清理，省时间）。
2. **重算页范围**：把「全局页号」换算成「片内页号」。比如全局第 25 页，在第 2 片（`start_page=20`）里就是片内第 6 页。
3. **首片独占 1**：`if i > 0: part_config.skip_scanned_detection = True`（详见 4.3）。
4. **换工作目录**：`get_part_working_dir(i)` / `get_part_output_dir(i)` 给每片一个独立的 `part_i` / `part_i_output` 目录。
5. **断言共享容器同一**：`part_config.shared_context_cross_split_part is translation_config.shared_context_cross_split_part`。
6. **切出物理 PDF**：用 PyMuPDF 的 `insert_pdf(from_page, to_page)` 把这一片页抽成 `input.part{i}.pdf`，作为 `part_config.input_file`。
7. **首片独占 2**：`if i > 0: part_config.watermark_output_mode = NoWatermark`（详见 4.3）。
8. 造 `part_monitor`，跑 `_do_translate_single`，结果存 `results[i]`。
9. `finally` 里 `cleanup_part_working_dir(i)` 清理该片的临时工作目录。

#### 4.2.3 源码精读

进入分片的总开关与三个回退分支：

[high_level.py:L546-L566](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L546-L566)

> `if not translation_config.split_strategy` 走单文档直翻；否则算切分点；切分点为空或只有 1 个都回退到 `_do_translate_single`——这是「优雅降级」，保证小文档不会因为开了分片而多绕一圈。注意第 560 行的日志 `f"Split points determined: {len(split_points)} parts"` 正是实践任务里要在日志里确认的「分片数量」。

每片页范围的重算逻辑——把全局页号映射成片内页号：

[high_level.py:L579-L598](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L579-L598)

> 关键是第 587 行 `page - split_point.start_page + 1`：`should_translate_page(page + 1)`（用户输入的页号从 1 开始）判断该全局页是否要翻译，命中则换算成片内页号，再写成 `page_ranges = [(x, x) for x in ...]`。这就是为什么分片翻译能正确配合 `--pages` 页面范围过滤——过滤发生在换算之前，按全局页号判定。

「首片独占」的第一个独占点——扫描检测只在第 0 片跑：

[high_level.py:L600-L615](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L600-L615)

> 第 601-602 行 `if i > 0: part_config.skip_scanned_detection = True`——第 0 片保留扫描检测，其余片一律跳过（理由见 4.3）。第 611-615 行的 `assert` 用 `id()` 强制保证 `part_config` 与原 `config` 指向**同一个** `shared_context_cross_split_part` 对象，这是跨片共享记忆的物理前提。

物理切片：用 PyMuPDF 把这一片页抽成独立的临时输入 PDF：

[high_level.py:L624-L645](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L624-L645)

> 第 624-640 行先新建一个空 `Document`，遍历该片页把原有 `Annots`（批注）清成 `null` 防止引用混乱，再 `insert_pdf(from_page=..., to_page=...)` 抽页，`safe_save` 成 `input.part{i}.pdf`。第 642-645 行的断言确保切出来的页数与 `SplitPoint` 承诺的页数严格一致。这样每片对 `_do_translate_single` 而言，就像在翻译一份独立的小 PDF。

「首片独占」的第二个独占点——水印只打在第 0 片，以及造 part_monitor、跑单片、合并：

[high_level.py:L647-L682](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L647-L682)

> 第 648-651 行把第 0 片之外的水印模式改成 `NoWatermark`（因为水印署名整份 PDF 只应出现一次，每片都打会重复）。第 654 行 `pm.create_part_monitor(i, len(split_points))` 给每片一个进度子监视器（见 4.4 的进度偏移）。第 680 行的 `logger.info("start merge results")` 正是实践任务要在日志里确认的合并起点。注意 `finally` 里 `cleanup_part_working_dir(i)` 保证即使某片失败也会清理临时目录。

#### 4.2.4 代码实践

**实践目标**：在 `--debug` 模式下，观察分片如何为每片创建独立的工作目录与临时输入 PDF。

**操作步骤**：

1. 准备一份**页数较多**的 PDF（`examples/ci/test.pdf` 可能页数不足，建议找一份 5 页以上的 PDF，记为 `big.pdf`）。
2. 用一个较小的 `max-pages-per-part` 强制分片，并开启 `--debug`：

```bash
babeldoc --openai --openai-api-key sk-xxx \
  --files big.pdf --max-pages-per-part 2 --debug \
  --working-dir ./work
```

3. 翻译过程中观察 `./work` 目录下是否出现 `part_0/`、`part_1/`、`part_2/` 等子目录，每个目录里是否都有 `input.part0.pdf`、`input.part1.pdf` 等切片后的输入 PDF。

**需要观察的现象**：每片对应一个独立的 `part_i` 工作目录；`input.part{i}.pdf` 的页数应当等于该片 `SplitPoint` 承诺的页数；`finally` 清理后这些 `part_i` 目录应被删除（除非 `--debug` 或异常保留）。

**预期结果**：分片数量 = ⌈总页数 / max-pages-per-part⌉，与日志 `Split points determined: N parts` 一致。

**待本地验证**：具体目录结构与日志输出请以本地运行为准；若无多页 PDF，可只阅读上述源码，在脑中走一遍 `for` 循环。

#### 4.2.5 小练习与答案

**练习 1**：为什么分片是**串行**而非**并行**？如果改成并行会有什么问题？

**答案**：因为翻译器内部已经有线程池段级并发（u6-l2），并且所有翻译器共享**同一个全局 QPS 漏桶**（u6-l1）。若再并行跑多片，总并发请求数会翻倍，轻易击穿 QPS 限流；同时 `SharedContextCrossSplitPart` 的某些字段（如自动术语表）在并行下需要更复杂的同步。串行把「片间」做成串行、「片内」交给线程池，是更稳健的折中。

**练习 2**：第 587 行的 `page - split_point.start_page + 1` 为什么最后要 `+ 1`？

**答案**：因为下游 `page_ranges` 与 `should_translate_page` 使用的页号约定是**从 1 开始**的用户页号。全局第 `start_page` 页（0 基）在片中应是片内第 1 页，所以 `page - start_page`（0 基片内号）再加 1 转成 1 基。

---

### 4.3 首片独占逻辑：扫描检测、水印与 OCR 决定的跨片广播

#### 4.3.1 概念说明

4.2 已经出现两处 `if i > 0:`——扫描检测和水印。本模块把它们和「OCR 决定的跨片广播」串起来，讲清**为什么有些事只能做一次，以及这一次的结果如何让其他片也知情**。

「首片独占」包含三件事：

| 独占项 | 第 0 片 | 第 i>0 片 | 机制 |
|--------|---------|-----------|------|
| 扫描件检测 | 运行 | `skip_scanned_detection=True` | `part_config` 改写 |
| 水印 | `Watermarked` | `NoWatermark` | `part_config` 改写 |
| OCR 决定 | 检测后设定 | 读取第 0 片的设定 | `shared_context_cross_split_part` 广播 |

前两项是「配置层面」的独占（每片的 `part_config` 不同），第三项是「**结果层面**的共享」——这是 `SharedContextCrossSplitPart` 最关键的用途之一。

#### 4.3.2 核心流程

**扫描检测的独占**：扫描件检测（u5-l1）是一个「整份文档级别」的判断——要么整份是扫描件、要么不是，没必要每片重测一遍（SSIM 比较很昂贵）。所以只在第 0 片跑；如果第 0 片判定为重度扫描件并开启了 `auto_enable_ocr_workaround`，会把决定写进 `shared_context_cross_split_part.auto_enabled_ocr_workaround = True`。

**OCR 决定的跨片广播**：当后续片进入 `_do_translate_single` 时，第一件事就是检查这个标志位：

```
def _do_translate_single(pm, config):
    if config.shared_context_cross_split_part.auto_enabled_ocr_workaround:
        config.ocr_workaround = True
        config.skip_scanned_detection = True
```

这样第 0 片的 OCR 决定就「传染」给了所有后续片，保证全文档一致地走 OCR workaround 模式（u5-l1）。这正是「shared_context 必须跨分片共享」的最直接理由：**如果不共享，第 0 片启用了 OCR workaround，第 1 片却不知道，会以普通模式翻译，导致同一份 PDF 前半部分 OCR 模式、后半部分普通模式，结果割裂**。

**水印的独占**：BabelDOC 的水印是「署名标记」（写在 PDF 元数据里，见 u2-l2 的 `add_metadata`）。整份合并后的 PDF 只应有一处水印，所以只有第 0 片保留 `Watermarked`，其余片设成 `NoWatermark`，合并后水印只出现一次。

#### 4.3.3 源码精读

`_do_translate_single` 开头的 OCR 决定读取——这就是「后续片吃第 0 片广播的结果」：

[high_level.py:L836-L845](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L836-L845)

> 第 843-845 行：每片一进来就读 `shared_context_cross_split_part.auto_enabled_ocr_workaround`，为 True 就把本片 `ocr_workaround`、`skip_scanned_detection` 都打开。注意读的是共享容器里的标志，不是本片 `part_config` 自己的字段——这就是广播。

扫描检测的广播源头——`DetectScannedFile` 把决定写进共享容器：

[detect_scanned_file.py:L131](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L131)

> 第 0 片的扫描检测一旦命中并允许自动 OCR workaround，就把 `shared_context_cross_split_part.auto_enabled_ocr_workaround = True`。由于所有片共享同一个对象（4.2 的 `assert` 保证），这个写入立刻对所有后续片可见。

`_do_translate_single` 中扫描检测是否运行的开关——由 `skip_scanned_detection` 控制：

[high_level.py:L937-L945](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L937-L945)

> 第 938 行 `if translation_config.skip_scanned_detection:` 决定是否真的跑 `DetectScannedFile`。第 0 片该字段为默认 False（会跑），第 i>0 片被 4.2 的循环改写成 True（不跑），从而实现「只在第 0 片检测」。

水印的独占改写——在 4.2 已引用，此处复述其位置与意图：

[high_level.py:L647-L651](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L647-L651)

> `if i > 0: part_config.watermark_output_mode = WatermarkOutputMode.NoWatermark`——非首片不打水印。合并后整份 PDF 只在首片对应位置有一处水印。

`SharedContextCrossSplitPart` 里承载这个广播标志的字段定义：

[translation_config.py:L34-L45](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L34-L45)

> 注意第 42 行 `self.auto_enabled_ocr_workaround = False` 是跨片共享的 OCR 决定；第 41 行 `raw_extracted_terms`（u6-l4 自动术语抽取的原始词条）也住在这里；第 44-45 行的字符/token 统计同样跨片累加。它们都是「必须跨分片共享」的全局量。

#### 4.3.4 代码实践

**实践目标**：理解「为什么 `shared_context_cross_split_part` 必须跨分片共享」——通过删除/破坏共享来推理后果。

**操作步骤（源码阅读型实践）**：

1. 打开 [high_level.py:L611-L615](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L611-L615)，确认那条 `assert` 要求 `part_config` 与原 `config` 共享同一对象。
2. 思考一个反事实场景：**假如** `part_config = copy.copy(translation_config)` 之后，`copy.copy` 把 `shared_context_cross_split_part` 也复制成了**独立的**新对象（实际不会，因为 `copy.copy` 是浅拷贝），会发生什么？
3. 写出三条后果（见下方「预期结果」）。

**需要观察的现象**：这是纯推理练习，不需要运行。重点在于理解「浅拷贝」在这里是**有意为之**的——正是浅拷贝让 `part_config` 复用了原 `config` 的 `shared_context_cross_split_part` 引用，从而实现跨片共享。

**预期结果**（若共享被破坏会出现的后果）：

1. **OCR 模式割裂**：第 0 片启用 OCR workaround 后，结果只存在第 0 片自己的容器里，第 1 片读到默认 False，以普通模式翻译——同一份 PDF 出现两种渲染模式。
2. **术语不一致**：各片各自抽取的 `raw_extracted_terms` 互不可见，`finalize_auto_extracted_glossary`（u6-l4 的多数投票）只能基于单片数据，无法对全文统一术语译法。
3. **统计不完整**：`valid_char_count_total` 等统计只反映单片，合并后 `do_translate` 末尾汇总的全文字符数会偏小。

因此 `shared_context_cross_split_part` **必须**跨分片共享，浅拷贝 + `assert` 是实现并守护这一点的两道保险。

#### 4.3.5 小练习与答案

**练习 1**：扫描检测「只在第 0 片做」是否可能导致漏判？比如一份 PDF 前 20 页是电子版、第 21 页起是扫描件，会怎样？

**答案**：会漏判第 21 页起的扫描部分。BabelDOC 的假设是「扫描属性在整份文档上是基本均匀的」（一份扫描件通常整份都是扫描件），所以用第 0 片代表全文档。这是工程上的近似——对「前电子后扫描」的混合文档，分片检测策略可能不理想。实践中若怀疑混合文档，可手动 `--ocr-workaround` 全程开启。

**练习 2**：水印为什么不能简单地「每片都打，合并后自然只有一处」？

**答案**：因为每片都会在自己的页面上渲染水印渲染单元，合并后每片的首页都会带上水印，导致水印重复出现 `N` 次（N=片数）。而且 BabelDOC 的水印还关联 PDF 元数据里的署名标记，重复写入会污染元数据。所以必须从源头让非首片不打。

---

### 4.4 结果合并：ResultMerger 把多片 PDF 拼回一个

#### 4.4.1 概念说明

各片翻译完，每片产出一个 `TranslateResult`（含 `mono_pdf_path` / `dual_pdf_path` 等路径）。`ResultMerger.merge_results` 负责把这些分散的 PDF 拼回**一份**最终 PDF，并做三件「全文档级别」的收尾：

1. **拼 PDF**：mono、dual、以及各自的 no_watermark 版本，分别按片序拼接。
2. **导出自动术语表**：如果开启了 `--save-auto-extracted-glossary`，把跨片汇总后的术语表写成 CSV（这是 `shared_context_cross_split_part` 共享的又一成果）。
3. **汇总统计**：把各片耗时 `total_seconds` 求和。

合并的关键技术细节：**拼接后要再做一次字体子集化与保存**。因为各片独立子集化过字体（u7-l3），直接拼接会产生重复或不连续的字体表，所以 `ResultMerger._merge_pdfs` 复用了 `PDFCreater.subset_fonts_in_subprocess` 和 `PDFCreater.save_pdf_with_timeout`（u7-l3 讲过的「子进程 + 超时 + 回退」健壮模式）对合并后的整份文档重新走一遍字体子集化与保存。

#### 4.4.2 核心流程

`merge_results(results: dict[int, TranslateResult | None])` 的流程：

```
results = 过滤掉 None 的片
sorted_results = 按 part index 排序
first_result = 第一片（取文件名模板）

对 mono PDF：
    if 任意片有 mono_pdf_path 且 not config.no_mono:
        merged_mono = _merge_pdfs([各片 mono], "xxx.mono.pdf")

对 dual PDF：同上（受 no_dual 控制）

对 no_watermark 版本（仅当与带水印版本不同时才单独合并）：
    合并 no_watermark mono / dual

对自动术语表（仅当 save_auto_extracted_glossary 且有汇总结果）：
    写 CSV（从 shared_context_cross_split_part.auto_extracted_glossary 取）

构造 merged_result，处理「水印/无水印版本互为缺省」的回填
total_seconds = 各片 total_seconds 之和
```

`_merge_pdfs(pdf_paths, output_name, tag)` 的流程：

```
merged_doc = 新建空 Document()
for path in pdf_paths（已按片序）:
    merged_doc.insert_pdf(Document(path))
merged_doc = PDFCreater.subset_fonts_in_subprocess(merged_doc, config, tag)  # 重新子集化
PDFCreater.save_pdf_with_timeout(merged_doc, output_path, config)            # 超时保护保存
return output_path
```

#### 4.4.3 源码精读

`merge_results` 的入口与文件名模板——`mono`/`dual` 与 `.debug`/`.no_watermark` 后缀的命名规则：

[result_merger.py:L26-L49](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py#L26-L49)

> 第 26-30 行用 `input_file` 的 stem 加语言与 `.mono.pdf`/`.dual.pdf` 组文件名；`--debug` 时插 `.debug` 后缀。第 40-42 行先过滤掉 `None` 片（如 `only_include_translated_page` 下无翻译页的片），再按 part index 排序，保证拼接顺序正确。

mono 与 dual 的拼接分支，以及「无水印版本仅在与带水印版本不同时才单独合并」的判断：

[result_merger.py:L88-L129](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py#L88-L129)

> 第 88-92 行 `any(r.dual_pdf_path != r.no_watermark_dual_pdf_path or r.mono_pdf_path != r.no_watermark_mono_pdf_path ...)` 判断「是否真的存在独立的去水印版本」——若带水印与去水印路径相同（如 `watermark_output_mode=no_watermark` 时两者本就一样），就不必重复合并，省一次昂贵的子集化。每个 `try/except` 都把单类合并失败的影响隔离，失败时该路径置 `None` 而不让整次合并崩掉。

自动术语表的导出——从共享容器取出跨片汇总结果写成 CSV：

[result_merger.py:L130-L144](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py#L130-L144)

> 第 133 行 `self.config.shared_context_cross_split_part.auto_extracted_glossary` 就是各片 `raw_extracted_terms` 经 `finalize_auto_extracted_glossary` 多数投票汇总后的产物（u6-l4）。能在这里拿到**全文档**统一的术语表，前提正是各片共享了同一个 `SharedContextCrossSplitPart`。

`_merge_pdfs`——拼接后重新字体子集化与超时保存：

[result_merger.py:L173-L194](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py#L173-L194)

> 第 183-185 行逐片 `insert_pdf` 拼接；第 187-189 行调 `PDFCreater.subset_fonts_in_subprocess(merged_doc, self.config, tag=tag)` 对**合并后**的整份文档重新做字体子集化（`tag` 用于子进程日志标识，如 `merged_mono`）；第 190-192 行 `save_pdf_with_timeout` 带超时保护地保存。这两步正是 u7-l3 讲过的 backend 健壮模式在合并阶段的复用。

水印/无水印版本的互为缺省回填与总耗时汇总：

[result_merger.py:L155-L171](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py#L155-L171)

> 第 155-163 行：若去水印 mono 为空则用带水印 mono 顶上、反之亦然（dual 同理），保证 `TranslateResult` 的四条路径都不会因为某种模式没产出而全部为空。第 166-169 行把各片 `total_seconds` 求和作为合并结果的耗时。

#### 4.4.4 代码实践

**实践目标**：在分片翻译的日志中确认「分片数量」「start merge results」「finish merge results」三个关键节点，并核对最终产物。

**操作步骤**：

1. 找一份多页 PDF（记为 `big.pdf`），用一个较小的步长分片翻译：

```bash
babeldoc --openai --openai-api-key sk-xxx \
  --files big.pdf --max-pages-per-part 3 \
  --save-auto-extracted-glossary
```

2. 在终端日志中依次确认：
   - `Split points determined: N parts`（N 为分片数）。
   - `start merge results`（进入合并）。
   - `finish merge results`（合并完成）。
3. 在输出目录检查产物：是否只有**一份** `big.zh.mono.pdf` 和 `big.zh.dual.pdf`（而非每片各一份），以及是否多出一个 `.glossary.csv`。

**需要观察的现象**：

- 日志里的分片数 N 与 `⌈总页数 / 3⌉` 一致。
- 合并前：各片在自己的 `part_i_output` 目录产出临时 PDF；合并后：只剩一份拼好的 mono/dual。
- 若开启 `--save-auto-extracted-glossary`，CSV 里的术语是**全文档**多数投票的结果（而非单片），页数越多、跨片共享效果越明显。

**预期结果**：最终 mono/dual PDF 的总页数等于各片页数之和（dual 视模式翻倍），水印只出现一次（仅首片对应位置），自动术语表为整份文档的统一译法。

**待本地验证**：具体日志文本与产物路径请以本地运行为准；若无多页 PDF 或 API key，可改为阅读 `do_translate`（527-701 行）与 `merge_results` 全文，在脑中跑一遍「切 3 片 → 各翻 → 拼回」的流程。

#### 4.4.5 小练习与答案

**练习 1**：合并时为什么要对拼接后的文档**重新**做一次字体子集化，而不是直接把各片已子集化的 PDF 拼起来就算完？

**答案**：各片是独立子集化的，各自只保留了本片用到的字形，字体表的内部编号也各自独立。直接拼接会导致合并文档里有多套不连续的字体表，可能引发字形冲突、PDF 阅读器渲染异常或文件体积膨胀。重新子集化相当于「把合并后的整份文档当成一份新文档，统一提取用到的字形」，得到干净连续的字体表。这就是 u7-l3 的 `subset_fonts_in_subprocess` 在合并阶段的第二次出场。

**练习 2**：`merge_results` 把每类合并（mono / dual / no_watermark）都包在独立的 `try/except` 里，这样设计的好处是什么？

**答案**：**故障隔离**。某一类合并失败（比如 dual 拼接时某片 PDF 损坏）只会让该类路径置 `None`，不会影响其他类（mono 仍能成功产出）。再配合 4.4.3 的「互为缺省回填」，最终 `TranslateResult` 总能尽量给出可用的产物，而不是整体抛错、前功尽弃。这是面向「长任务要尽量有产出」的健壮性设计。

---

## 5. 综合实践

把本讲四个模块串成一个端到端的小任务。

**任务**：对一份多页 PDF 进行分片翻译，并全程追踪「切分 → 串行翻译 → 首片独占 → 合并」四个阶段，最终回答三个问题。

**步骤**：

1. **准备**：找一份 8 页以上的 PDF（记为 `paper.pdf`）。设定 `--max-pages-per-part 3`，使它至少切成 3 片。开启 `--debug` 与 `--save-auto-extracted-glossary`，并指定 `--working-dir ./split_work` 方便观察：

```bash
babeldoc --openai --openai-api-key sk-xxx \
  --files paper.pdf --max-pages-per-part 3 \
  --debug --save-auto-extracted-glossary \
  --working-dir ./split_work
```

2. **追踪切分（模块 4.1）**：在日志里找到 `Split points determined: N parts`，手算 N 是否等于 `⌈总页数 / 3⌉`。

3. **追踪串行翻译（模块 4.2）**：观察进度条是否出现 `part_index` 推进（u2-l3 的 part 偏移），确认各片是**依次**完成的；检查 `./split_work` 下是否出现 `part_0` / `part_1` / `part_2` 目录及各自的 `input.partN.pdf`。

4. **追踪首片独占（模块 4.3）**：
   - 确认扫描检测日志（`start detect scanned file`）只在第 0 片出现。
   - 打开最终 dual PDF，确认水印（署名标记）只出现一次。
   - 用一句话解释：如果 `shared_context_cross_split_part` 没有跨片共享，自动术语表会有什么问题？

5. **追踪合并（模块 4.4）**：找到 `start merge results` 与 `finish merge results`；确认输出目录最终只有一份 `paper.zh.mono.pdf` 与 `paper.zh.dual.pdf`，以及一份 `paper.zh.glossary.csv`。

6. **回答三个问题**：
   - 这份 PDF 被切成了几片？每片的页范围（闭区间）分别是什么？
   - 为什么合并后还要再做一次字体子集化？
   - `shared_context_cross_split_part` 至少承载了哪三类必须跨片共享的信息？

**预期产出**：一份分片翻译后的单份 mono/dual PDF、一份全文档统一的术语表 CSV，以及你对上述三个问题的文字解答。

**待本地验证**：以上命令需本地具备多页 PDF 与可用的 OpenAI 兼容服务方可实际运行；若条件不具备，请以「源码阅读」方式完成第 2-6 步的追踪与问答。

## 6. 本讲小结

- **分片由 `--max-pages-per-part` 触发**：CLI 参数经 `main.py` 转成 `PageCountStrategy`，存入 `config.split_strategy`；`do_translate` 检测到它非空才进入分片分支，且切出 0 片或 1 片都会优雅降级回单文档。
- **`PageCountStrategy` 按页数切**：用 `while` 循环按固定步长产出 `SplitPoint`（闭区间 `end_page = end_page - 1`），`SplitManager` 只是转发策略的薄壳，预留了复杂度/章节字段供未来扩展。
- **分片串行**：`do_translate` 的 `for` 循环逐片跑，每片用 `copy.copy` 浅拷贝出独立 `part_config`（独立页范围、工作目录、切片 PDF、part_monitor），片内才用线程池并发；不是片间并行。
- **首片独占两件事**：扫描检测与水印都只在第 0 片做（`if i > 0:` 改写 `part_config`），因为它们都是「整份文档级别」只需一次的工作。
- **OCR 决定跨片广播**：第 0 片的扫描检测结果经 `shared_context_cross_split_part.auto_enabled_ocr_workaround` 广播给所有后续片，`_do_translate_single` 一进来就读取它统一 OCR 模式——这正是「共享容器必须跨片共享」的核心理由。
- **`ResultMerger` 拼回一份**：按片序拼接 mono/dual/no_watermark PDF，拼接后**重新字体子集化与超时保存**（复用 u7-l3 的 backend 健壮模式），再导出全文档术语表与汇总耗时；各类合并独立 `try/except` 实现故障隔离。

## 7. 下一步学习建议

本讲是 u8「工程化与扩展」单元的一环。建议按以下顺序继续：

- **横向对比异步事件流**：阅读 u8-l3（异步 API 内部实现与回调桥接），看分片翻译时 `part_monitor` 的 `part_offset` 是如何通过 `ProgressMonitor.calculate_current_progress` 算出整体进度的——本讲的 `pm.create_part_monitor` 在那里有完整的事件协议展开。
- **深入共享容器**：本讲反复出现的 `SharedContextCrossSplitPart` 是 u6-l4（自动术语抽取）与 u6-l5（术语表系统）的交汇点。建议回去重读 `finalize_auto_extracted_glossary` 的多数投票，体会「跨片共享 + 多数投票」如何保证大文档术语一致。
- **复用 backend 健壮模式**：本讲 `ResultMerger._merge_pdfs` 调用的 `subset_fonts_in_subprocess` / `save_pdf_with_timeout` 来自 u7-l3。建议结合 u7-l3 理解「子进程隔离 + 超时 + 回退」这套模式为何在合并阶段同样必要。
- **进阶扩展点**：若想自定义切分逻辑（如按章节切），可以参照 `BaseSplitStrategy` 写一个新策略类，注入 `config.split_strategy`——这是练习策略模式扩展的现成入口。
