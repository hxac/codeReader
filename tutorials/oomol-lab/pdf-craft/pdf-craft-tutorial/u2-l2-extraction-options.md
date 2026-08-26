# PDFOptions 与 ExtractionOptions 详解

## 1. 本讲目标

上一讲我们搞清楚了「用哪个 OCR 后端」，配置对象解决的是**基础设施**问题。本讲往下走一层，回答「**这一次**提取要怎么跑」：

1. 掌握 `ExtractionOptions` 的全部控制项：页面范围 `page_indexes`、OCR 尺寸 `ocr_size`、DPI、token 上限、封面/脚注开关等，并理解它们在源码中被逐层传递的路径。
2. 理解 `toc_assumed` 与 `toc_llm` 这两个「目录假设」参数如何改变目录分析的行为。
3. 掌握 `aborted`（协作式中止）与 `on_ocr_event`（逐页事件回调）两类回调的用途，以及 `ignore_pdf_errors` / `ignore_ocr_errors` 错误忽略策略。

学完本讲，你应该能对一个几百页的 PDF「先花最少的钱试跑 5 页」，在中途随时叫停，并清楚每一次调用到底发生了什么。

## 2. 前置知识

### 2.1 冻结 dataclass

Python 的 `@dataclass(frozen=True)` 生成一个「创建后不可修改」的类。pdf-craft 的所有配置类（上一讲的六种 OCR 配置、本讲的 `PDFOptions` / `ExtractionOptions`）都是冻结的：配置一旦构造完成就不会被库在运行途中偷偷改掉，这让「同一次运行的相同参数必然有相同行为」成为可能。想在两次运行之间换参数？新建一个 `ExtractionOptions` 即可，`PDFCraft` 实例本身可以复用。

### 2.2 `Container[int]`：只要支持 `in` 运算就行

`page_indexes` 的类型标注是 `Container[int] | None`。`Container` 是 Python 的一个抽象协议，任何支持 `x in y` 运算的对象都算：`set`、`tuple`、`list`、`range` 都可以。所以 `page_indexes=range(1, 6)` 和 `page_indexes={1, 2, 3, 4, 5}` 完全等价，而 `range` 不需要一次性展开所有数字，对大范围更省内存。

### 2.3 回调函数：把「钩子」交给调用方

回调（callback）就是「你把一个函数交给库，库在特定时机替你调用它」。本讲会遇到两个：

- `on_ocr_event: Callable[[OCREvent], None]` —— 每页事件发生时被调用，用于**观测**（打日志、记进度条）。
- `aborted: AbortedCheck`（即 `Callable[[], bool]`）—— 库在关键节点轮询它，返回 `True` 就中止任务，用于**控制**。

### 2.4 生成器事件流（预热）

`OCR.recognize` 是一个生成器函数，每处理完一步就 `yield` 出一个 `OCREvent`。提取引擎用 `for event in self._ocr.recognize(...)` 逐个消费。你注册的 `on_ocr_event` 回调正是被挂在这个消费循环里。生成器的细节是第 u3-l3 讲的主角，本讲只需要建立「**事件流**」这个画面。

### 2.5 页码从 1 开始

pdf-craft 的页码一律**从 1 计数**（one-based），没有第 0 页。这一点稍后有源码证据，也是 CLI 工具明确写了测试保障的约定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/craft.py` | 定义 `PDFOptions` 与 `ExtractionOptions` 两个冻结 dataclass，并在 `extract_pdf_with_metering` 中逐字段解包转发 |
| `pdf_craft/extractor/pdf/extractor.py` | 提取器边界层，把外部参数与内部默认值合并后交给引擎 |
| `pdf_craft/transform.py` | `PDFExtractionEngine._extract_from_pdf`：真正消费这些选项的地方（OCR 循环、目录分析、章节生成） |
| `pdf_craft/pdf/ocr.py` | `OCREvent` / `OCREventKind` 定义，`OCR.recognize` 生成器：事件产生、token 扣减、错误忽略、缓存判断都在这里 |
| `pdf_craft/metering.py` | `AbortedCheck` 类型、`check_aborted` 函数、`OCRTokensMetering` 计量、`InterruptedKind` 枚举 |
| `pdf_craft/error.py` | `IgnorePDFErrorsChecker` / `IgnoreOCRErrorsChecker` 类型、`InterruptedError` 与 `to_interrupted_error` 转换助手 |
| `pdf_craft/extractor/toc/analysing.py` | `analyse_toc`：消费 `toc_assumed` 与 `toc_llm` 的目录分析入口 |
| `pdf_craft/pdf/types.py` | `DeepSeekOCRSize` 字面量类型定义 |
| `pdf_craft/pdf/page_ref.py` | 页码从 1 计数的实现；按文件大小反推 DPI 上限的公式 |
| `docs/en/PDF_TRANSLATION.md` | 官方英文指南中 Extraction controls 一节 |
| `pdf_craft_tool/cli.py` | 仓库 CLI 如何组装一个真实的 `ExtractionOptions`（最佳参考范例） |

## 4. 核心概念与源码讲解

### 4.1 选项数据类：从字段到提取引擎的完整旅程

#### 4.1.1 概念说明

pdf-craft 把「配置」切成两刀：

- **`PDFOptions` —— 长期基础设施**。OCR 用哪个后端、PDF 用哪个处理器、模型缓存在哪。它跟着 `PDFCraft` 实例走，一次配置，多次提取共用。
- **`ExtractionOptions` —— 单次运行控制**。这次提哪几页、DPI 多少、token 预算多少、要不要封面脚注。它以 `extraction=` 参数的形式逐次传入。

为什么分开？因为「换一本书」和「换一种跑法」是两件事：同一个 `PDFCraft` 实例（同一套 OCR 凭据）完全可能第一次只跑前 5 页试效果，第二次全量跑。把变化频率不同的配置分层，是库设计中很常见的「稳定性分层」手法。

两个类都是 `frozen=True`，字段都带默认值，所以 `ExtractionOptions()` 裸构造完全合法，表示「全按默认来」。

#### 4.1.2 核心流程

`ExtractionOptions` 从构造到生效要经过四站：

```text
你的脚本
  │  craft.convert_pdf_to_markdown(..., extraction=ExtractionOptions(...))
  ▼
① PDFCraft.extract_pdf_with_metering()        craft.py
  │  把 options 的 15 个字段逐个解包成关键字参数
  ▼
② PDFExtractor.extract_with_metering()        extractor/pdf/extractor.py
  │  与内部默认值字典合并，补上 analysing_path 等引擎私有参数
  ▼
③ PDFExtractionEngine._extract_from_pdf()     transform.py
  │  组装 ocr/、assets/、chapters/、toc.xml 等包内路径
  ▼
④ OCR.recognize(...)                          pdf/ocr.py
     生成器逐页消费 page_indexes / dpi / token 上限 / 回调
```

注意一个细节：门面方法转发时做了**字段改名**——`ExtractionOptions.max_ocr_tokens` 对应引擎侧的 `max_tokens`，`max_ocr_output_tokens` 对应 `max_output_tokens`。公开命名带 `ocr_` 前缀是为了和翻译 LLM 的 token 概念区分（OCR 和翻译 LLM 是两套独立计费的东西，u1-l1 已建立这个认知）。

#### 4.1.3 源码精读

**两个选项类的定义**：

[pdf_craft/craft.py:L38-L45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L38-L45) 定义 `PDFOptions`，docstring 写明它是「只在提取 PDF 时才需要的长期基础设施」，四个字段（`ocr`、`pdf_handler`、`models_cache_path`、`local_only`）在上一讲已经通过 `ensure_ocr_config` 串联过，此处不再展开。

[pdf_craft/craft.py:L48-L66](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L48-L66) 定义 `ExtractionOptions`，全部 15 个字段如下表：

| 字段 | 类型 / 默认值 | 作用 |
| --- | --- | --- |
| `page_indexes` | `Container[int] \| None` = `None` | 只提取这些页（1 起始）；`None` 表示全部页 |
| `ocr_size` | `DeepSeekOCRSize` = `"gundam"` | DeepSeek 系 OCR 的模型档位 |
| `dpi` | `int \| None` = `None` | 页面渲染 DPI；`None` 时引擎按 300 处理 |
| `max_page_image_file_size` | `int \| None` = `None` | 单页图像文件大小上限（字节），超限自动降 DPI |
| `max_ocr_tokens` | `int \| None` = `None` | 整次提取的 OCR token 预算（输入+输出） |
| `max_ocr_output_tokens` | `int \| None` = `None` | 整次提取的 OCR 输出 token 预算 |
| `includes_cover` | `bool` = `False` | 是否把第 1 页图像存为 `cover.png` |
| `includes_footnotes` | `bool` = `False` | 是否识别并保留脚注区 |
| `generate_plot` | `bool` = `False` | 是否额外生成图表资源（`plots/` 目录） |
| `toc_assumed` | `bool` = `False` | 假设书中有目录页，启用目录页搜索（详见 4.3） |
| `toc_llm` | `LLM \| None` = `None` | 用 LLM 分析标题层级（详见 4.3） |
| `ignore_pdf_errors` | `bool \| 谓词` = `False` | PDF 渲染错误的忽略策略（详见 4.2） |
| `ignore_ocr_errors` | `bool \| 谓词` = `False` | OCR 错误的忽略策略（详见 4.2） |
| `aborted` | `Callable[[], bool]` = `lambda: False` | 协作式中止检查（详见 4.2） |
| `on_ocr_event` | `Callable[[OCREvent], None]` = `lambda _: None` | 逐页事件回调（详见 4.2） |

**第一站：门面解包转发**。[pdf_craft/craft.py:L91-L110](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L91-L110) 中 `extract_pdf_with_metering` 先把 `None` 归一成默认 `ExtractionOptions()`（L95），然后把 15 个字段一一展开传给 `extract_with_metering`。这里没有任何逻辑，纯粹是「公开命名 → 内部命名」的翻译层，例如 L101-L102 的 `max_tokens=options.max_ocr_tokens, max_output_tokens=options.max_ocr_output_tokens`。

**第二站：提取器补默认值**。[pdf_craft/extractor/pdf/extractor.py:L14-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py#L14-L36) 用一个 `defaults` 字典承接引擎的全部参数并 `defaults.update(kwargs)` 覆盖。这层存在的意义是：`PDFExtractor` 是公开的提取边界，允许绕过门面直接调用，所以它必须自己兜一套默认值。

**第三站：引擎消费**。[pdf_craft/transform.py:L46-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L46-L65) 是 `_extract_from_pdf` 的完整签名，能看到引擎视角的参数全集（含私有参数 `analysing_path`，即包目录）。真正消费 `page_indexes` 的是 [pdf_craft/transform.py:L93](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L93)：

```python
page_indexes=page_indexes if page_indexes is not None else range(1, 2**31),
```

`None` 被翻译成「从 1 到 2 的 31 次方」的巨大 range——实际上就是「每一页」。这也再次印证 `page_indexes` 为什么是 `Container` 而非 `Sequence`：它从不迭代，只做 `in` 判断。

**页码为什么从 1 开始**：[pdf_craft/pdf/page_ref.py:L60-L66](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py#L60-L66) 中 `PageRefContext.__iter__` 对 `range(pages_count)` 循环但 `page_index=i + 1`。仓库 CLI 的测试 `test_page_indexes_are_explicitly_one_based` 还专门断言传入 `"0,1"` 会报错。

**`ocr_size` 是什么**：[pdf_craft/pdf/types.py:L10](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L10) 定义 `DeepSeekOCRSize = Literal["tiny", "small", "base", "large", "gundam"]` 五档。注意官方文档 [docs/en/OCR_BACKENDS.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/OCR_BACKENDS.md#L33) 说明：档位约束与后端相关（Unlimited OCR local 只支持 `base`/`gundam`；DeepSeek OCR 2 local 验证过的是 `base`），且这类校验目前发生在仓库 CLI 层而非库层——库本身不做 `ocr_size` 与后端的组合校验。

**DPI 与图像大小的数学**：`dpi` 为 `None` 时渲染按 300 处理（见 [pdf_craft/pdf/ocr.py:L151-L155](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L151-L155) 的注释 `# DPI=300 for scanned page`）。若设置了 `max_page_image_file_size`，[pdf_craft/pdf/page_ref.py:L113-L121](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py#L113-L121) 会按「文件大小 ≈ 宽像素 × 高像素 × 每像素 3 字节 × PNG 压缩率 0.5」反推允许的最大 DPI：

\[ dpi_{max} = \sqrt{\frac{file\_size}{width\_inch \times height\_inch \times 3 \times 0.5}} \]

最终渲染 DPI 取 \( \min(dpi,\ dpi_{max}) \)。这个参数在「OCR 服务对图像体积有上限」时很有用。另外，落盘元数据时 [pdf_craft/transform.py:L116-L121](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L116-L121) 会把 `dpi`（或默认 300）连同每页像素尺寸一起写进 `document.json`——这是将来 PDF 回写排版所必需的几何信息。

#### 4.1.4 代码实践

**实践目标**：不跑 OCR，先纯本地验证「选项对象 → 引擎参数」的传递链，确认你理解的字段映射是对的。

**操作步骤**：

1. 写一个脚本，用 `dataclasses.fields` 打印 `ExtractionOptions` 的全部字段名与默认值（示例代码）：

```python
# 示例代码：检查 ExtractionOptions 的字段与默认值
from dataclasses import fields
from pdf_craft import ExtractionOptions

defaults = ExtractionOptions()  # 裸构造，全部走默认值
for field in fields(defaults):
    print(f"{field.name:28s} = {getattr(defaults, field.name)!r}")
```

2. 再看仓库自己的测试 [tests/test_craft.py:L182-L192](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L182-L192)：`test_extraction_options_reach_extractor_engine` 用一个假引擎捕获收到的关键字参数，断言 `page_indexes=(2, 4)` 与 `max_ocr_tokens=12` 原样（改名后）到达引擎。运行它：

```bash
python -m unittest tests.test_craft.TestPDFCraft.test_extraction_options_reach_extractor_engine -v
```

3. 顺手验证 `Container` 语义（示例代码）：

```python
# 示例代码：page_indexes 接受任何支持 in 运算的对象
options = ExtractionOptions(page_indexes=range(1, 6))
print(5 in options.page_indexes, 6 in options.page_indexes)  # True False
```

**需要观察的现象**：步骤 1 打印出 15 个字段及默认值；步骤 2 测试通过，证明门面只是逐字段转发。

**预期结果**：字段清单与 4.1.3 的表格逐行一致；`page_indexes` 默认为 `None`。测试运行结果为通过（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`ExtractionOptions(page_indexes=range(1, 6))` 和 `ExtractionOptions(page_indexes={1, 2, 3, 4, 5})` 在效果上有区别吗？什么时候该用哪个？

**答案**：效果完全相同，因为引擎只做 `in` 判断（`Container` 协议）。`range` 不用一次性物化所有整数，页号连续的大范围更省内存；`set` 适合跳页选取（如 `{1, 5, 9}`），且大集合下 `in` 判断是 O(1)。

**练习 2**：为什么 `max_ocr_tokens` 不直接叫 `max_tokens`？

**答案**：门面层刻意加 `ocr_` 前缀，与翻译用的 LLM token 区分——pdf-craft 中 OCR 与翻译 LLM 是两套独立配置、独立计费的基础设施（u1-l1 的核心认知）。引擎内部参数确实叫 `max_tokens`，改名发生在 `extract_pdf_with_metering` 的转发处（craft.py L101-L102）。

**练习 3**：`includes_cover=True` 时封面图从哪一页来？

**答案**：永远来自第 1 页。[pdf_craft/pdf/ocr.py:L172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L172) 中 `includes_raw_image=(ref.page_index == 1)` 只对第 1 页保留原始图像，[pdf_craft/pdf/ocr.py:L207-L209](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L207-L209) 在 `cover_path` 存在且页面有图像时把它存成 `cover.png`。所以「封面」本质上就是第 1 页的渲染图。

### 4.2 中断与回调：aborted、on_ocr_event 与错误忽略

#### 4.2.1 概念说明

长任务有两个永恒问题：「**进行到哪了**」和「**能不能停**」。pdf-craft 用两个回调分别回答：

- **`on_ocr_event` —— 观测钩子**。OCR 循环每发生一件事（开始一页、渲染完、识别完、跳过、失败、忽略）就产出一个 `OCREvent`，同步调用你的回调。它只读不改，适合做进度条、日志、成本核算。
- **`aborted` —— 控制钩子**。库在每个页面边界主动问一句「要停吗？」（调用你的函数），返回 `True` 就地抛异常终止。这叫**协作式中止**（cooperative cancellation）：库不会杀死线程，而是把检查点安排在自己方便停的安全位置，保证磁盘状态一致。

与之配套的还有**错误忽略策略** `ignore_pdf_errors` / `ignore_ocr_errors`：单页失败时是「立刻崩溃」还是「记下来继续跑」。三者共用一套事件与异常基础设施，所以放在一起讲。

#### 4.2.2 核心流程

OCR 主循环（简化伪代码）：

```text
for 每一页 page（页号从 1 到 pages_count）:
    ① check_aborted(aborted)          # 若返回 True → 抛 AbortError，全程终止
    ② 产出 START 事件
    ③ 若 page_index 不在 page_indexes:
         产出 IGNORE 事件；continue
    ④ 若 ocr/page_N.xml 已存在且无 failed 标记:
         产出 SKIP 事件（断点续跑缓存命中）
    ⑤ 否则:
         若 token 预算已耗尽 → 抛 TokenLimitError
         渲染页面（dpi）→ 产出 RENDERED 事件
         OCR 识别
           ├─ 成功 → 存 page_N.xml，产出 COMPLETE 事件（带 token 数）
           └─ PDFError/OCRError:
                若错误检查器说「忽略」→ 写 page_N.failed 标记，
                  生成兜底页面，产出 FAILED 事件（带 error）
                否则 → 异常上抛，全程终止
       每页结束后扣减剩余 token 预算
```

六种事件的语义（定义见 [pdf_craft/pdf/ocr.py:L22-L39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L22-L39)）：

| 事件 | 触发时机 | 关键字段 |
| --- | --- | --- |
| `START` | 每页循环开头 | `page_index`、`total_pages` |
| `IGNORE` | 该页不在 `page_indexes` 范围内，未识别 | `cost_time_ms` |
| `SKIP` | 该页已有缓存 `page_N.xml`，跳过识别 | `cost_time_ms` |
| `RENDERED` | 页面已渲染成图像、尚未 OCR | `cost_time_ms` |
| `COMPLETE` | 该页 OCR 成功 | `input_tokens`、`output_tokens`、`cost_time_ms` |
| `FAILED` | 该页失败但被忽略策略放过 | `error`（原始异常） |

中止与预算耗尽的表现形式是**异常**。根据官方排障文档 [docs/zh-CN/TROUBLESHOOTING.md:L145-L151](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/zh-CN/TROUBLESHOOTING.md#L145-L151)：主动中止抛出上游库的 `AbortError`，token 预算耗尽抛 `TokenLimitError`；这两个异常**不会**自动转换成 pdf-craft 导出的 `InterruptedError`，除非调用方显式使用 `to_interrupted_error` 助手。转换后可以拿到 `.kind`（`ABORT` 或 `TOKEN_LIMIT_EXCEEDED`）和 `.metering`（中断前已消耗的 token 数）。

#### 4.2.3 源码精读

**中止检查的定义**：[pdf_craft/metering.py:L5-L12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L5-L12) 把 `AbortedCheck` 定义为 `Callable[[], bool]`，`check_aborted` 在回调返回真时抛出上游的 `AbortError`。注意异常是**延迟导入**的（函数体内 import），这延续了 u1-l3 讲过的「上游 doc-page-extractor 一切懒加载」的边界约定。

**事件数据类**：[pdf_craft/pdf/ocr.py:L22-L39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L22-L39) 定义 `OCREventKind` 六值枚举与 `OCREvent` 数据类（`kind`、`page_index`、`total_pages`、`cost_time_ms`、`input_tokens`、`output_tokens`、`error`）。

**检查点位置**：[pdf_craft/pdf/ocr.py:L107-L124](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L107-L124) 是每页循环的开头三步——`check_aborted(aborted)` 在 `yield START` **之前**执行。也就是说：中止生效时，被中止的那一页连 `START` 事件都不会发出。同一段的 L115-L124 是 `IGNORE` 分支：不在范围内的页只记事件就 `continue`，不渲染、不识别、不花一分钱。

**缓存命中与预算守卫**：[pdf_craft/pdf/ocr.py:L130-L144](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L130-L144) 先判断 `page_N.xml` 存在且无失败标记则产出 `SKIP`；否则在动手前检查两个剩余预算（`remain_tokens` / `remain_output_tokens`），任一 `<= 0` 立即抛 `TokenLimitError`。预算的扣减在每页完成后：[pdf_craft/pdf/ocr.py:L222-L227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L222-L227)，`max_ocr_tokens` 扣「输入+输出」，`max_ocr_output_tokens` 只扣输出。

**错误忽略的两个分支**：[pdf_craft/pdf/ocr.py:L179-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L179-L187) 分别捕获 `PDFError`（渲染层）与 `OCRError`（识别层），交给 [pdf_craft/pdf/ocr.py:L324-L328](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L324-L328) 的 `_check_ignore_error`：检查器是 `bool` 就直接用，是函数就 `check(error)` 问你。被忽略的页在 [pdf_craft/pdf/ocr.py:L196-L204](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L196-L204) 写下 `page_N.failed` 标记并生成兜底页面（整页截图或一行占位文本），最终以 `FAILED` 事件收尾。检查器类型定义在 [pdf_craft/error.py:L35-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L35-L36)：`bool | Callable[[PDFError], bool]` 与 `bool | Callable[[OCRError], bool]`——谓词形态可以按页号、按错误消息精细决策。

**引擎侧的事件消费与「全部失败」保护**：[pdf_craft/transform.py:L78-L101](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L78-L101) 的 `for event in self._ocr.recognize(...)` 循环里：L95 调用你的 `on_ocr_event(event)`，L96-L97 把每个事件的 token 累加进 `OCRTokensMetering`（这就是一步式方法返回值的来源），L98-L101 统计「可用页」（`COMPLETE` + `SKIP`）与失败页。随后 [pdf_craft/transform.py:L103-L104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L103-L104) 抛出 `NoUsableOCRPagesError`：如果所有请求的页都失败了，即使你选择了「忽略错误」，提取也不会带着空结果继续——这是当前 HEAD 刚刚修复的保护（提交 `bbb2d20`）。对应测试见 [tests/test_composable_boundaries.py:L112-L141](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L112-L141)。

**中断异常的转换助手**：[pdf_craft/error.py:L57-L79](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/error.py#L57-L79) 的 `to_interrupted_error` 把上游的 `ExtractionAbortedError` 家族（含 `AbortError`、`TokenLimitError`）翻译成 pdf-craft 自己的 `InterruptedError`，保留中断种类和已消耗 token。一个小提醒：pdf-craft 的 `InterruptedError` 与 Python 内置同名异常（`OSError` 的子类）**没有任何关系**，导入时同名遮蔽是初学者容易踩的坑。该助手没有出现在包顶层导出里，需要 `from pdf_craft.error import to_interrupted_error`。

**aborted 会一路传到渲染**：一步式方法里，提取阶段的中止回调还会转交给渲染阶段——[pdf_craft/craft.py:L179-L190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190) 的 L189 把 `extraction.aborted` 传给 `render_markdown`。仓库测试 [tests/test_craft.py:L261-L270](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L261-L270) 专门验证了这条转发链，保证「一个回调管全程」。

**真实消费范例**：仓库 CLI 就是最标准的用法。[pdf_craft_tool/cli.py:L427-L446](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L427-L446) 把命令行参数组装成 `ExtractionOptions`（能看到 `page_indexes`、`dpi`、`toc_assumed`、`on_ocr_event=_print_ocr_event` 等全部身影），回调实现在 [pdf_craft_tool/cli.py:L563-L564](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L563-L564)：一行 `print(f"OCR {event.kind.name.lower()}: page {event.page_index}/{event.total_pages}")`。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：对一个多页 PDF 只转换第 1 到 5 页；用 `on_ocr_event` 打印每页事件类型与 token 消耗；再用 `aborted` 回调在第 3 页完成后人为中止，观察异常行为与磁盘上的半成品。

**操作步骤**：

1. 准备一个 20 页左右的 PDF（页数可先用 `pdfinfo your.pdf` 查看；仓库 `tests/assets/` 下有 `newton.pdf`、`double_column.pdf` 等资产可用，任选一个多页的）。
2. 写脚本 `practice_u2l2.py`（示例代码，OCR 配置换成你自己的）：

```python
# 示例代码：page_indexes + on_ocr_event + aborted 三合一实践
from pdf_craft import (
    DeepSeekOCRVendorConfig, ExtractionOptions, InterruptedKind,
    PDFCraft, PDFOptions,
)
from pdf_craft.error import to_interrupted_error

craft = PDFCraft(pdf=PDFOptions(
    ocr=DeepSeekOCRVendorConfig(
        base_url="https://example.com/v1",
        api_key="your-api-key",
        model="deepseek-ocr",
    ),
))

def on_ocr_event(event) -> None:
    print(f"[{event.kind.name:8s}] page {event.page_index}/{event.total_pages}"
          f"  tokens: in={event.input_tokens} out={event.output_tokens}"
          f"  {event.cost_time_ms}ms")

# —— 第一轮：只跑第 1~5 页，不中止 ——
metering = craft.convert_pdf_to_markdown(
    "book.pdf", "sample.md",
    package_path="work/package",   # 保留中间产物，供第二轮观察
    extraction=ExtractionOptions(
        page_indexes=range(1, 6),
        on_ocr_event=on_ocr_event,
    ),
)
print("成功：", metering)

# —— 第二轮：在第 3 页完成后中止 ——
stop = {"now": False}
def aborted() -> bool:
    return stop["now"]

def on_ocr_event_with_stop(event) -> None:
    on_ocr_event(event)
    if event.kind.name == "COMPLETE" and event.page_index >= 3:
        stop["now"] = True   # 下一页循环开头的 check_aborted 会触发

try:
    craft.convert_pdf_to_markdown(
        "book.pdf", "sample2.md",
        package_path="work/package",   # 故意复用同一个包目录
        extraction=ExtractionOptions(
            page_indexes=range(1, 6),
            on_ocr_event=on_ocr_event_with_stop,
            aborted=aborted,
        ),
    )
except Exception as error:
    translated = to_interrupted_error(error)
    if translated is not None:
        print("已中止：kind =", translated.kind,
              "已消耗 tokens =", translated.metering)
    else:
        raise
```

3. 检查 `work/package/ocr/` 目录：第一轮结束后应有 `page_1.xml` 到 `page_5.xml`；第二轮结束后再次列出该目录。

**需要观察的现象**：

- 第一轮：第 1~5 页各产生 `START`、`RENDERED`、`COMPLETE` 三个事件（`COMPLETE` 带 token 数）；第 6 页起的每一页只有 `START` + `IGNORE` 两个事件——这就是「不在范围内不花一分钱」的直观体现。
- 第二轮：第 1~3 页因第一轮已缓存，各产生 `START` + `SKIP` 两个事件（注意没有 `RENDERED`——渲染发生在缓存判断之后的 `else` 分支里，命中缓存的页根本不渲染）；随后第 4 页**连 `START` 都不会出现**（中止检查点在 `START` 之前），异常被捕获，打印出 `kind` 与已消耗 token。`SKIP` 事件的 token 为 0，说明缓存命中不产生费用。
- `to_interrupted_error` 若返回非 `None`，`translated.kind` 应为 `InterruptedKind.ABORT`。

**预期结果**：按源码逻辑（ocr.py L108 的检查点在 `START` 之前、L130-L137 的缓存跳过、error.py L66-L68 的 `AbortError` → `ABORT` 映射），上述现象应当成立；具体打印数值与事件耗时取决于你的 PDF 与 OCR 服务，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 pdf-craft 的中止是「协作式」的？如果 `aborted` 回调里写一个 `time.sleep(60)`，会发生什么？

**答案**：协作式指库在自选的安全点（每页循环开头）主动轮询回调，而不是被外部强杀。回调里的 `sleep(60)` 不会中止任务，反而让那一页的检查点阻塞 60 秒——回调是同步执行的，它只应做轻量的状态读取（如查一个标志位）。

**练习 2**：`ignore_ocr_errors=True` 跑完一次提取，结果里有些页变成了整页截图或占位文字。这些页在事件流里长什么样？如果所有请求的页都这样，会发生什么？

**答案**：每页产出 `FAILED` 事件（`error` 字段带原始异常），引擎生成兜底页面写 `page_N.xml`，并留下 `page_N.failed` 标记。若全部页失败（可用页数为 0），[pdf_craft/transform.py:L103-L104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L103-L104) 会抛 `NoUsableOCRPagesError`（携带失败页号元组），不会产出空包。官方文档也提醒：忽略错误跑完的结果仍需人工复核，跳过的页不是成功识别的页。

**练习 3**：`max_ocr_tokens=10000`，跑到第 7 页时预算刚好耗尽，第 8 页会发生什么？已经完成的前 7 页结果会丢吗（假设传了 `package_path`）？

**答案**：每页完成后预算扣减「输入+输出 token」（ocr.py L222-L224）；处理第 8 页前检查 `remain_tokens <= 0`，抛 `TokenLimitError`（L141-L142）。前 7 页的 `page_N.xml` 在各自完成时已落盘，不会丢；且 `done` 标记未写入，下次提高预算重跑时前 7 页会走 `SKIP` 直接复用。

### 4.3 目录假设：toc_assumed 与 toc_llm

#### 4.3.1 概念说明

提取链路的第二步是**目录分析**：从 OCR 结果推断出这本书的章节结构，写成 `toc.xml`，章节生成（第三步）按它切分章节。这一步有两个开关：

- **`toc_assumed`（默认 `False`）**：控制「要不要在书里**搜索目录页**」。很多书的前几页是「目录页」——密密麻麻列着「第 1 章 …… 12」这样的条目。`toc_assumed=True` 会启动一个标题匹配打分算法去找出这些页，并从目录条目推断层级；`False` 则跳过搜索，直接从**正文标题**的排版特征统计层级。
- **`toc_llm`（默认 `None`）**：可选地把一个 `pdf_craft.llm.LLM` 配置交给目录分析，用大模型判断标题层级。它是一条「增强路径」，失败时自动回退到统计方法。

两者的关系是正交的：`toc_assumed` 决定走「有目录页」还是「无目录页」分支，`toc_llm` 决定该分支里用 LLM 还是统计法。另外整个 `analyse_toc` 有一个**缓存短路**：包目录里已存在 `toc.xml` 时直接复用，完全不重算——想改变目录分析参数重新生效，得删掉旧的 `toc.xml`。

（这两个参数背后的算法细节——Aho-Corasick 匹配、统计层级推断、LLM 分析器——属于第 u4 单元的主角，本讲只讲「开关在哪、改变什么」。）

#### 4.3.2 核心流程

`analyse_toc` 的决策树：

```text
analyse_toc(pages_path, toc_path, toc_assumed, toc_llm)
  │
  ├─ toc.xml 已存在？
  │     └─ 是 → 直接读回缓存，返回（不重算）
  │
  ├─ toc_assumed=True？
  │     └─ 是 → find_toc_pages() 在 OCR 结果中搜索目录页
  │           ├─ 找到目录页：
  │           │     ├─ toc_llm 提供了？→ LLM 分析目录条目层级
  │           │     │     └─ LLMAnalysisError？打印警告，回退 ↓
  │           │     └─ 统计法 analyse_toc_levels()
  │           └─ 没找到 → 走「无目录页」分支 ↓
  │
  └─ （toc_assumed=False 或没找到目录页）
        ├─ toc_llm 提供了？→ LLM 分析正文标题层级
        │     └─ LLMAnalysisError？打印警告，回退 ↓
        └─ 统计法 analyse_title_levels()
```

最后把「(页码, 序号) → 层级」的扁平映射用栈算法组装成目录树写入 `toc.xml`。

#### 4.3.3 源码精读

**缓存短路**：[pdf_craft/extractor/toc/analysing.py:L25-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L25-L38) 的 `analyse_toc` 开头两行：`toc_path.exists()` 就 `decode_toc` 直接返回。这就是 u1-l4 讲过的「传 `package_path` 二次渲染零成本」在目录环节的具体机制。

**门面转发**：[pdf_craft/transform.py:L106-L111](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L106-L111) 里引擎把 `toc_llm` 与 `toc_assumed` 原样交给 `analyse_toc`。

**`toc_assumed` 的分支**：[pdf_craft/extractor/toc/analysing.py:L52-L66](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L52-L66) 在 `toc_assumed` 为真时调用 `find_toc_pages`，向它提供两个迭代器：全书的标题列表（`TITLE_TAGS` 布局的文本，去掉 Markdown 标题前缀）和每页的正文拼接文本——匹配打分算法用「正文标题是否出现在某页文本里」来判断该页是不是目录页。

**LLM 路径与回退**：[pdf_craft/extractor/toc/analysing.py:L71-L109](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L71-L109) 展示两条对称的路径：找到目录页时用 `analyse_toc_levels_by_llm`（L74-L84），没找到时用 `analyse_title_levels_by_llm`（L100-L102）；两处的 `except LLMAnalysisError` 都只 `print` 一句警告然后落到统计法（L90-L95、L108-L109），不抛异常。这是很典型的「**增强路径允许降级**」容错设计：LLM 是锦上添花，不该因为它挂了就毁掉整次提取。

**官方使用提醒**：中文排障文档 [docs/zh-CN/TROUBLESHOOTING.md:L121-L128](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/zh-CN/TROUBLESHOOTING.md#L121-L128) 指出：`toc_assumed=True` 而输入其实没有可用目录时，章节划分可能不符合预期；排查思路是先关掉 `toc_assumed` 或缩小 `page_indexes` 验证，再查 OCR 本身。

#### 4.3.4 代码实践

**实践目标**：亲手对比 `toc_assumed` 两个取值对 `toc.xml` 的影响，并验证缓存短路的存在。

**操作步骤**：

1. 选一本**带目录页**的书（`tests/assets/index.pdf` 从命名看可能含目录页；最稳妥的是你手头任何一本扫描书的前几十页）。对同一个 PDF 跑两次提取到**不同**的包目录（示例代码）：

```python
# 示例代码：对比 toc_assumed 两个取值
from pdf_craft import DeepSeekOCRVendorConfig, ExtractionOptions, PDFCraft, PDFOptions

craft = PDFCraft(pdf=PDFOptions(
    ocr=DeepSeekOCRVendorConfig(
        base_url="https://example.com/v1", api_key="your-api-key", model="deepseek-ocr",
    ),
))
for assumed in (False, True):
    craft.extract_pdf(
        "book.pdf", f"work/pkg-toc{int(assumed)}",
        ExtractionOptions(page_indexes=range(1, 21), toc_assumed=assumed),
    )
```

2. 用任意工具查看两个包里的 `toc.xml`：比较 `<toc>` 树的条目数量、层级深度，以及根节点的 `page_indexes` 属性（被判定为目录页的页号列表）。
3. 验证缓存短路：对 `work/pkg-toc1` **再跑一次**完全相同的 `extract_pdf`（参数不变），观察这次运行的速度与 OCR 事件（可加上 4.2 的 `on_ocr_event`）——是否几乎瞬时完成；然后删掉 `work/pkg-toc1/toc.xml` 再跑一次，观察目录分析是否重新执行。

**需要观察的现象**：`toc_assumed=True` 的包中，若书确有目录页，`toc.xml` 的根 `page_indexes` 应非空（记录了目录页页号），层级更接近书的真实章节结构；`False` 的包则从正文标题统计层级。缓存实验中，参数不变的重复运行应全部命中 `SKIP` 且 `toc.xml` 直接复用；删除 `toc.xml` 后仅目录分析与章节生成重跑，OCR 仍走缓存。

**预期结果**：上述行为由 analysing.py L31-L32 的短路逻辑和 ocr.py 的页级缓存共同保证。两个包的 `toc.xml` 具体差异取决于所选书籍，`tests/assets` 资产是否含目录页待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：改了 `toc_assumed` 但 `toc.xml` 没变化，最可能的原因是什么？

**答案**：包目录里已有旧的 `toc.xml`，`analyse_toc` 直接缓存返回（analysing.py L31-L32），新参数根本没被执行。删掉旧 `toc.xml`（或换一个新的 `package_path`）再跑。

**练习 2**：`toc_llm` 配置的 API 密钥是错的，提取会失败吗？

**答案**：不会整体失败。LLM 分析抛出的 `LLMAnalysisError` 被捕获后只打印一句「falling back to statistical method」警告，自动回退统计法（analysing.py L85-L88、L103-L106）。这是刻意的降级设计：LLM 是增强路径，不是关键路径。

**练习 3**：`toc_llm` 在 `toc_assumed=False` 时还有用吗？

**答案**：有。`toc_llm` 在两个分支里都可用：有目录页时执行 `analyse_toc_levels_by_llm`（分析目录条目层级），无目录页时执行 `analyse_title_levels_by_llm`（分析正文标题层级）。两个开关是正交的。

## 5. 综合实践

**任务：一次「成本可控」的分段试跑实验。** 把本讲三个模块串起来：先用最小成本试跑、全程可观测、随时可停、停了能续，最后用目录假设拿到合理章节结构。

背景设定：你要处理一本 300 页的扫描书，OCR 服务按 token 计费，直接全量跑风险太高。请写一个脚本 `costed_probe.py`：

1. **第一段（试探）**：`page_indexes=range(1, 6)` + `max_ocr_tokens=50000`（预算兜底）+ `on_ocr_event` 把每个事件写入 CSV（列：`kind, page_index, total_pages, input_tokens, output_tokens, cost_time_ms`），`package_path="work/probe"`。跑完用 CSV 汇总：实际输入/输出 token、每页平均耗时，据此估算全书的成本与时长。
2. **第二段（中止演练）**：复用同一个 `package_path` 再跑 `page_indexes=range(1, 31)`，注册 `aborted` 回调：当累计 `COMPLETE` 事件的页数达到 10 时返回 `True`。捕获异常并用 `to_interrupted_error` 打印 `kind` 与 `metering`。
3. **第三段（续跑验证）**：去掉 `aborted`，再跑一次 `page_indexes=range(1, 31)`。观察事件流：前 10 页左右应全部是 `SKIP`（零 token），只有剩余页真正识别——对照 CSV 证明「断点续跑不重复计费」。
4. **第四段（结构定稿）**：确认书籍带目录页后，删除 `work/probe/toc.xml`，以 `toc_assumed=True` 重新提取（OCR 依旧全走缓存），比较新旧 `toc.xml`，选定最终结构后再做全量转换。

**验收标准**：能回答以下问题——每页平均成本是多少？中止发生在哪一页（该页应有 `START` 之后、无 `COMPLETE` 的残缺记录吗？为什么没有？）？续跑时哪些事件类型消失了？`toc_assumed` 改变了 `toc.xml` 的什么？

（提示：第二段中止页不会有任何事件——检查点在 `START` 之前，见 4.2.3 的源码分析。所有具体数值待本地验证。）

## 6. 本讲小结

- pdf-craft 把配置切成两层：`PDFOptions` 管「长期基础设施」（OCR 后端、PDF 处理器），`ExtractionOptions` 管「单次运行控制」（15 个字段），两者都是冻结 dataclass；门面在 `extract_pdf_with_metering` 中逐字段解包转发，其中 `max_ocr_tokens → max_tokens` 有一次刻意的改名。
- `page_indexes` 是 `Container[int]`（支持 `in` 即可，`range`/`set` 均可），页码从 1 计数；`None` 表示全部页。`dpi` 缺省按 300 渲染，`max_page_image_file_size` 会按像素体积公式自动压低 DPI。
- OCR 循环是一个六种事件（`START`/`IGNORE`/`SKIP`/`RENDERED`/`COMPLETE`/`FAILED`）的生成器流；`on_ocr_event` 是只读观测钩子，`aborted` 是协作式控制钩子（检查点在每页 `START` 之前，中止抛上游 `AbortError`，可用 `to_interrupted_error` 转成带计量信息的 `InterruptedError`）。
- `max_ocr_tokens` / `max_ocr_output_tokens` 是跨页累计预算，耗尽抛 `TokenLimitError`；已完成页的 `page_N.xml` 不受影响，重跑时走 `SKIP` 缓存。
- `ignore_pdf_errors` / `ignore_ocr_errors` 接受布尔或谓词，被忽略的失败页产出 `FAILED` 事件并写 `page_N.failed` 标记；若所有请求页都失败，`NoUsableOCRPagesError` 会阻止产出空包。
- `toc_assumed` 决定是否搜索目录页，`toc_llm` 决定层级分析用 LLM 还是统计法（LLM 失败自动降级）；`toc.xml` 已存在时整个分析被缓存短路。

## 7. 下一步学习建议

本讲讲完了「提取的输入端」。下一讲 **u2-l3《LLM 配置与 token 编码》**补齐配置体系的最后一块：`toc_llm` 和翻译 LLM 的类型 `pdf_craft.llm.LLM` 怎么构造、`token_encoding` 与 tiktoken 的作用、缓存与日志目录如何帮助调试。

之后进入第 u3 单元，沿提取主链路自顶向下：**u3-l1《提取主链路：从门面到引擎》**会把本讲看到的 `OCR.recognize` 事件循环放进完整的四步流程（OCR 循环 → 目录分析 → 章节生成 → 元数据落盘）；**u3-l3《OCR 驱动器》**深入本讲反复出现的 `page_N.xml`、`done` 标记与断点续跑机制。建议在继续之前，先把 4.2.4 的主实践跑通——对事件流有手感之后再读生成器源码，效率会高很多。
