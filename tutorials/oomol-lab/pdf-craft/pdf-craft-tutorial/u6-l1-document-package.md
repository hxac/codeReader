# DocumentPackage：中间产物契约

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `DocumentPackage` 的五个字段与它约定的目录布局（`chapters/`、`assets/`、`toc.xml`、`cover.png`、`document.json`）。
- 读懂 `document.json` 的每一项内容，特别是 `schema`、`bbox_coordinate_space`、`page_pixel_sizes` 的含义与用途。
- 解释 `validate()` 与 `_parse_page_pixel_sizes()` 的校验规则，以及「EPUB 渲染必须有 toc.xml、PDF 回写必须有页几何」这两条约束分别在哪里被强制。
- 理解为什么 pdf-craft 要在提取器与渲染器之间放一个「磁盘上的目录」作为契约，而不是在内存里传对象。

本讲是第 6 单元「文档包与渲染」的第一讲，向上承接 u3-l1（提取主链路四步中的最后一步 `write_metadata` 就是本讲的主角），向下为 Markdown/EPUB 渲染器（u6-l3、u6-l4）与 PDF 回写管线（u10）铺路。

## 2. 前置知识

阅读本讲前，你需要了解几个前置概念（在前面各讲已建立，这里简要回顾）：

- **中间包（package）**：u1-l4 讲过，`extract_pdf` 的产物不是一个内存对象，而是一个磁盘目录。不传 `package_path` 时它落在临时目录、用完即删；传了就持久化。这个目录的「格式定义」就是本讲的 `DocumentPackage`。
- **OCR 检测框（det/bbox）**：u5-l1 讲过，章节 XML 里每个文本块 `BlockLayout` 都带着 `page_index` 与四元组坐标 `det`。这个坐标是 **OCR 渲染位图像素坐标系** 下的值——即先把 PDF 页面用 Poppler 渲染成一张位图，OCR 在位图上框出文字位置。
- **PDF 点坐标系**：PDF 页面本身的度量单位是「点」（point，1 英寸 = 72 点）。回写 PDF 时要在正确的位置画白块，就必须把「位图像素坐标」换算成「PDF 点坐标」。
- **契约（contract）**：软件里指生产方与消费方共同遵守的稳定接口。这里生产方是提取器，消费方是渲染器/翻译管线，契约是一个约定好布局的目录加上 JSON 元数据。

一个关键直觉：**`DocumentPackage` 类本身几乎不存数据，它只存路径**。真正的数据（章节 XML、图片资源、目录树、元数据）都在磁盘上，类只是把它们组织起来并提供读写与校验方法。这是一个「目录即数据格式」的设计。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/document/package.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py) | `DocumentPackage` 数据类：目录约定、`validate` 校验、`write_metadata` 写入、`page_pixel_sizes` 读取。本讲主战场，全文件仅 93 行。 |
| [pdf_craft/document/\_\_init\_\_.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/__init__.py) | 包出口：导出 `DocumentPackage`、`SourceLocation`、`source_location`。 |
| [pdf_craft/document/source.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/source.py) | `SourceLocation`：描述一个文档块在渲染 PDF 页上的位置（页码、bbox、序号），是随公开 API 导出的溯源辅助类型。 |
| [pdf_craft/transform.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py) | 提取引擎内部模块，L116-120 调用 `write_metadata`，是 `document.json` 的**生产端**。 |
| [pdf_craft/extractor/pdf/extractor.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py) | 公开提取边界 `PDFExtractor`，提取完成后从磁盘重建并校验 `DocumentPackage`。 |
| [pdf_craft/renderer/epub/renderer.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/renderer.py) | EPUB 渲染器，以 `require_toc=True` 校验包。 |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py) | 门面类，`_validate_package_for_pdf` 在 PDF 回写前检查页几何元数据是否齐全。 |
| [pdf_craft/pipeline/pdf/pipeline.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py) | PDF 翻译管线，`page_pixel_sizes()` 的主要**消费端**。 |
| [tests/test_composable_boundaries.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py) | 边界测试：展示了如何手工搭建最小合法包（无需 OCR），是本讲实践的样板。 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**包结构**、**元数据 schema**、**校验逻辑**。

### 4.1 包结构

#### 4.1.1 概念说明

提取一份 PDF 之后，pdf-craft 在磁盘上留下一个这样的目录：

```
<package_path>/
├── chapters/          # chapter_head.xml、chapter_1.xml、chapter_2.xml ...（u5-l3 的产物）
├── assets/            # 图片、表格截图、公式图片，按内容哈希命名（u3-l4 的产物）
├── toc.xml            # 目录树（u4-l3 的产物，可能不存在）
├── cover.png          # 封面截图（仅 includes_cover=True 时，可能不存在）
├── document.json      # 元数据：schema 版本、dpi、每页位图尺寸（引擎第四步写入）
└── ocr/               # OCR 缓存 page_N.xml —— 注意：不属于 DocumentPackage 契约！
```

要点：

- `chapters/` 与 `assets/` 是**必备**的；`toc.xml`、`cover.png`、`document.json` 是**可选**的。
- `ocr/` 目录（u3-l3 讲过的断点续跑缓存）虽然和包住在一起，但 `DocumentPackage` 类**不管它**——它属于提取器的内部缓存，不属于提取器与渲染器之间的契约。`page_pixel_sizes()` 的文档字符串专门强调了这一点（见 4.2.3）。
- 为什么用「目录 + 约定」而不是一个压缩包或数据库？因为各文件可以独立被缓存短路（`toc.xml` 存在即跳过目录分析、`page_N.xml` 存在即跳过该页 OCR），也可以独立被手工检查和编辑（u4-l3 实践过手改 `toc.xml`）。目录布局天然支持「部分更新」。

#### 4.1.2 核心流程

`DocumentPackage` 从路径构建的过程：

```
给定包根目录 path
  ├── chapters_path = path / "chapters"
  ├── assets_path  = path / "assets"
  ├── toc_path     = path / "toc.xml"
  ├── cover_path   = path / "cover.png"（仅当文件已存在，否则 None）
  └── metadata_path = path / "document.json"
```

#### 4.1.3 源码精读

先看数据类定义与 `from_path`：

[package.py:7-26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L7-L26) 定义了 `DocumentPackage` 数据类：前两个字段 `chapters_path`、`assets_path` 必填，后三个 `toc_path`、`cover_path`、`metadata_path` 默认 `None`；类文档字符串只有一句——「由提取器产出的、稳定的渲染器输入」。`from_path` 类方法按固定命名拼出五个路径，其中 `cover.png` 只有已存在才赋值（第 24 行的三元表达式），其余路径不做存在性检查——**存在性检查是 `validate` 的职责，构建阶段保持宽容**。

两个便捷谓词：

[package.py:88-92](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L88-L92) 提供 `has_toc()` 与 `has_cover()`：路径非空且文件存在才返回真。EPUB 渲染器正是靠前者判断能不能建目录树。

同包还导出一个溯源类型：

[source.py:4-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/source.py#L4-L14) 定义 `SourceLocation`（页码、bbox、序号）与构造函数 `source_location`，用于描述一个文档块在渲染 PDF 页上的位置。它随 [document/\_\_init\_\_.py:1-4](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/__init__.py#L1-L4) 一并导出为公开 API，服务于需要块级溯源的下游代码。

最后看生产端如何造出一个真实包：

[transform.py:66-72](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L66-L72) 提取引擎把 `analysing_path` 按同样的命名约定拆出 `assets`、`ocr`、`chapters`、`toc.xml`、`cover.png` 各路径——注意引擎与 `DocumentPackage` 之间没有共享常量，目录名是**靠约定对齐**的，这也是「契约」一词的字面含义。

#### 4.1.4 代码实践

**实践目标**：不花一分钱 OCR token，手工搭出一个「结构合法」的最小包，直观感受目录约定。

**操作步骤**（示例代码，仿照 [tests/test_composable_boundaries.py:201-204](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L201-L204) 的写法）：

```python
# build_min_package.py（示例代码）
from pathlib import Path
from pdf_craft.document import DocumentPackage

root = Path("my_package")
root.mkdir(exist_ok=True)
package = DocumentPackage.from_path(root)
package.chapters_path.mkdir(parents=True, exist_ok=True)  # chapters/
package.assets_path.mkdir()                               # assets/
package.write_metadata(dpi=300, page_pixel_sizes={1: (100, 100)})
print(package)
```

**需要观察的现象**：目录下出现 `chapters/`、`assets/`、`document.json` 三个成员；`print` 出的五个路径里 `cover_path` 为 `None`（因为 `cover.png` 不存在）。

**预期结果**：`tree my_package` 可见三个成员；随后 `DocumentPackage.from_path(root).validate()` 通过（不抛异常）。

**待本地验证**：本实践未在编写讲义时实际运行，请读者自行执行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cover_path` 在 `from_path` 里做了存在性判断，而 `toc_path` 没有？

**答案**：封面纯粹是「渲染时的可选装饰」，不存在就当没有，提前置 `None` 可以让 `has_cover()` 直接判路径为空，省一次文件系统访问；而 `toc.xml` 的存在性语义重要得多——它既是「EPUB 渲染的硬性前提」（`require_toc`），又是「目录分析缓存短路」的信号（u4-l3），需要每次实时检查文件是否真的存在，所以保留路径、由 `has_toc()` 每次落盘判断。

**练习 2**：`ocr/` 目录明明和包放在一起，为什么不进 `DocumentPackage` 的字段？

**答案**：`DocumentPackage` 是提取器与渲染器之间的**公开契约**，而 `ocr/` 是提取器的**私有缓存**。渲染器只消费 `chapters/`、`assets/`、`toc.xml`；把缓存塞进契约会暴露实现细节，一旦缓存格式调整（比如 u3-l3 提到的 `page_pixel_sizes.json` 就是后来加进 `ocr/` 的）就会破坏契约稳定性。

### 4.2 元数据 schema

#### 4.2.1 概念说明

`document.json` 是包的「说明书」，回答四个问题：

1. 这包数据是什么格式版本？（`schema`）
2. 章节里的 bbox 坐标是什么坐标系？（`bbox_coordinate_space`）
3. 页码从几开始数？（`page_index_base`）
4. 每页渲染位图多大、用什么 dpi 渲的？（`dpi` + `page_pixel_sizes`）

后三项合起来解决一个要命的问题：**章节 XML 里的 `det` 坐标是 OCR 位图像素值，而 PDF 页面是点坐标，两者换算需要「位图尺寸 + dpi」这两个外部信息**。没有 `document.json`，块的坐标就只是一堆悬空的数字。

#### 4.2.2 核心流程

`document.json` 的完整生命周期：

```
提取引擎第四步 write_metadata
  ├── 读旧包已有几何（断点续跑时保留历史页）
  ├── 合并本次 OCR 会话记录的 last_page_pixel_sizes
  └── 写 document.json（schema=1 声明格式版本）
            │
            ▼
消费端读取 page_pixel_sizes()
  ├── PDF 翻译管线：像素 bbox ÷ dpi × 72 → PDF 点坐标，定位白块
  └── 门面预检：几何缺失 / 页码越界 → 拒绝回写
```

坐标换算的数学关系：

\[ \text{PDF 点坐标} = \frac{\text{OCR 像素坐标}}{\text{dpi}} \times 72 \]

例如 dpi=300 时，某块检测框宽 900 像素，对应 \( 900 / 300 \times 72 = 216 \) 点，即 3 英寸宽。每页位图尺寸（`page_pixel_sizes`）则提供了该页坐标系的「上界」，也是换算后与实际 PDF 页面比对、判断替换文本是否溢出的基准。

#### 4.2.3 源码精读

写入端：

[package.py:42-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L42-L50) 是 `write_metadata`：若 `metadata_path` 为空则默认写到包根的 `document.json`；payload 固定五个键——`schema: 1`（格式版本号）、`bbox_coordinate_space: "ocr_pixels"`（声明 bbox 的坐标系）、`page_index_base: 1`（页码 1 起数，与 u3-l2 的全局约定一致）、`dpi`、`page_pixel_sizes`（键转字符串、尺寸转列表以适配 JSON）。注意 `page_pixel_sizes` 的键在 JSON 里是字符串（JSON 的键只能是字符串），读回来时再转回 `int`。

生产端的合并逻辑：

[transform.py:116-120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L116-L120) 引擎用字典合并运算符 `|` 把「旧包已有几何」（第 76 行读入的 `existing_page_pixel_sizes`，见 [transform.py:76](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L76)）与「本次 OCR 会话新记录的几何」合并——右侧优先。这保证了断点续跑时，缓存命中（SKIP）的页面几何不丢失，因为那几页本次没有重新渲染。`dpi` 缺省按 300 记录。

读取端：

[package.py:52-57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L52-L57) 是 `page_pixel_sizes()`：文档字符串明确写着「返回提取器记录的 OCR 画布尺寸，**不触碰 OCR 缓存**」——即只读 `document.json`，不去 `ocr/` 目录翻 `page_pixel_sizes.json`。元数据不存在时安静地返回空字典，不报错（是否允许为空由调用方决定，见 4.3）。

严格的解析器：

[package.py:59-86](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L59-L86) 是 `_parse_page_pixel_sizes` 静态方法，逐项校验：整体必须是映射；每个键必须能转成 **≥1 的整数**（第 67-71 行，页码从 1 起数的约定在这里被强制）；每个值必须是**恰好两个正整数**的列表或元组（第 72-84 行）——连 `bool` 都被显式排除（第 77 行 `not isinstance(value, bool)`，因为 Python 里 `bool` 是 `int` 的子类，`True` 会被当成 1 混过去）。任何一项不合法都抛带具体信息的 `ValueError`。

消费端一：PDF 翻译管线。

[pipeline.py:35-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L35-L36) 在 `translate` 中先取 `pages = package.page_pixel_sizes()` 再遍历章节，把每页尺寸传给替换收集器；`patch` 方法同理（[pipeline.py:64-65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L64-L65)）。

[pipeline.py:86-90](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py#L86-L90) 是兜底路径：某块的页码不在 `pages` 里（比如包是用旧版本库生成的，没有 `document.json`），且注入了 `pdf_handler`，就现场把该页按 300 dpi 渲一张图，取其尺寸补进 `pages`；没有 handler 就抛 `ValueError`。这就是 u10-l1 会讲的「页几何兜底」。

消费端二：门面预检。

[craft.py:325-327](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L325-L327) `_validate_package_for_pdf` 在回写前检查：`page_pixel_sizes()` 为空则直接抛 `ValueError("DocumentPackage is missing page geometry metadata required for PDF patching")`——宁可拒绝工作，也不把白块画到错误位置。后续第 336-347 行还会检查页码是否超出 PDF 实际页数、章节引用的页是否缺几何。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `document.json` 的内容，并验证「页码必须 ≥1、尺寸必须是两个正整数」的校验。

**操作步骤**（示例代码）：

```python
# inspect_metadata.py（示例代码）
import json
from pathlib import Path
from pdf_craft.document import DocumentPackage

root = Path("my_package")  # 4.1.4 实践搭的包，或真实提取产物
print(json.dumps(json.loads((root / "document.json").read_text()), indent=2))
print(DocumentPackage.from_path(root).page_pixel_sizes())

# 手工破坏 schema，观察解析器报错
data = json.loads((root / "document.json").read_text())
data["page_pixel_sizes"]["0"] = [100, 100]   # 页码 0 非法
(root / "document.json").write_text(json.dumps(data))
DocumentPackage.from_path(root).validate()
```

**需要观察的现象**：第一次打印可见五个键；`page_pixel_sizes()` 把 JSON 字符串键还原成 `{1: (100, 100)}`；把页码改成 0 后，`validate()` 抛出 `ValueError: page_pixel_sizes page indexes must be positive`。

**预期结果**：与 [package.py:70-71](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L70-L71) 的错误信息一致。可再试 `[100, 100, 100]`（长度不是 2）与 `[100, true]`（布尔混入），分别命中第 74-84 行两条信息。

**待本地验证**：请读者自行运行确认。

#### 4.2.5 小练习与答案

**练习 1**：断点续跑时，第二次运行只重新 OCR 了第 3 页，为什么第 1、2 页的几何不会丢？

**答案**：`transform.py:76` 在 OCR 循环前先从旧包读出 `existing_page_pixel_sizes`，第 116 行用 `|` 与本次会话的 `self._ocr.last_page_pixel_sizes` 合并。缓存命中（SKIP）的页本次不渲染、不在 `last_page_pixel_sizes` 里，但旧值被保留；第 3 页是重新渲染的，新值在右侧、覆盖旧值。

**练习 2**：把 `schema` 字段改成 2 再 `validate()`，会发生什么？这个设计的意义是什么？

**答案**：抛 `ValueError("unsupported document package schema")`（[package.py:37-38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L37-L38)）。意义是「版本门卫」：将来若元数据格式不兼容地变更（如新增必填字段、改坐标系约定），老库读新包能立刻、明确地失败，而不是拿着错误假设默默算出错位的坐标。目前唯一合法值是 1。

### 4.3 校验逻辑

#### 4.3.1 概念说明

`validate()` 是包的「入场安检」，规则一共四条，按严格程度递增：

1. `chapters/` 目录必须存在；
2. `assets/` 目录必须存在（**即使没提取出任何图片**——空目录也算合法）；
3. 若调用方要求 `require_toc=True`，`toc.xml` 必须存在；
4. 若 `document.json` 存在，其 `schema` 必须是 1，且 `page_pixel_sizes` 必须能通过严格解析；**不存在则跳过**（元数据是可选的）。

校验的调用时机体现了「谁需要什么，谁强制什么」的原则：提取器出口统一做基础校验；EPUB 渲染器追加 toc 要求；PDF 回写追加几何要求。`DocumentPackage` 自己不替所有调用方把关。

#### 4.3.2 核心流程

```
validate(require_toc=False)
  ├── chapters 目录缺失？→ ValueError: missing chapters directory
  ├── assets 目录缺失？  → ValueError: missing assets directory
  ├── require_toc 且无 toc.xml？→ ValueError: ... missing toc.xml
  ├── document.json 存在？
  │     ├── schema ≠ 1 → ValueError: unsupported ... schema
  │     └── 解析 page_pixel_sizes（4.2.3 的六道检查）
  └── 返回 self（支持链式调用 from_path(p).validate()）
```

调用方分布：

| 调用方 | 位置 | require_toc | 说明 |
| --- | --- | --- | --- |
| `PDFExtractor.extract_with_metering` | extractor.py:33-35 | 否 | 提取出口的统一安检 |
| `EpubRenderer.render` | renderer.py:13 | **是** | 无目录树建不了 EPUB spine |
| `PDFTranslationPipeline.translate/patch` | pipeline.py:31 / 60 | 否 | 几何要求另行由门面预检把关 |

#### 4.3.3 源码精读

校验本体：

[package.py:28-40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L28-L40) 即 `validate` 全文：两个目录检查在前（错误信息带完整路径，方便定位），`require_toc` 检查居中，元数据检查垫后且以「文件存在」为前提。返回 `self` 支持链式写法。注意它**不检查** `chapters/` 里有没有 XML 文件、`assets/` 里有没有资源——只查骨架，内容留给下游按需处理。

提取器出口的强制校验：

[extractor.py:31-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py#L31-L36) 提取完成后，先显式 `mkdir` 确保 `assets/` 存在（注释说明：**即使所选页面没有提取出任何图片，DocumentPackage 也永远拥有 assets 目录**），再 `from_path` 重建并 `validate`。这解释了为什么第 2 条规则可以严格要求 assets 存在——生产端兜底保证了它。

EPUB 渲染器的强化校验：

[renderer.py:13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/renderer.py#L13) `EpubRenderer.render` 第一行就是 `package.validate(require_toc=True)`：EPUB 是「书」格式，必须由目录树构建 spine（阅读顺序）与 NCX/nav 目录（u6-l4 详述），没有 `toc.xml` 的包只能渲染 Markdown，不能渲染 EPUB。

为什么 PDF 回写必须有页几何（本讲实践任务要求解释的问题）：

回写 PDF 的方案是「白块盖住原文 + 透明叠层写译文」（u10-l2 详述）。白块画在哪、画多大，完全由块检测框决定，而检测框是 OCR 位图像素坐标。要把它落到 PDF 点坐标系，必须知道渲染 dpi（像素→英寸）与目标 PDF 每页的实际点尺寸（英寸→点，并核对该页位图与 PDF 页是否同一页）。`page_pixel_sizes` 提供了每页位图的宽高——既参与换算，也是「这页几何是否可信」的登记表。若几何缺失，白块位置就无从算起；pdf-craft 的选择是在 [craft.py:326-327](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L326-L327) 预检阶段直接拒绝，或如 4.2.3 所述在注入 `pdf_handler` 时现场渲染兜底，绝不带病作画。

#### 4.3.4 代码实践

**实践目标**：完成本讲规定的三步实践——加载校验、破坏 assets 验证报错、读取页几何并解释其回写用途。

**操作步骤**（示例代码）：

```python
# practice_u6l1.py（示例代码）
import shutil
from pathlib import Path
from pdf_craft.document import DocumentPackage

root = Path("my_package")          # 4.1.4 搭的最小包，或真实提取产物
package = DocumentPackage.from_path(root).validate()   # 第 1 步：加载并校验
print("validate 通过:", package)

shutil.rmtree(root / "assets")                          # 第 2 步：删掉 assets/
try:
    DocumentPackage.from_path(root).validate()
except ValueError as error:
    print("报错信息:", error)

# 第 3 步：恢复后读取页几何
(root / "assets").mkdir()
sizes = DocumentPackage.from_path(root).page_pixel_sizes()
print("页几何:", sizes)
for page_index, (width, height) in sorted(sizes.items()):
    points = (width / 300 * 72, height / 300 * 72)     # dpi=300 时的 PDF 点尺寸
    print(f"第 {page_index} 页位图 {width}x{height} px ≈ {points[0]:.0f}x{points[1]:.0f} pt")
```

**需要观察的现象**：第 1 步不抛异常；第 2 步捕获的报错为 `missing assets directory: my_package/assets`（带完整路径）；第 3 步打印出每页位图像素尺寸与换算后的点尺寸。

**预期结果**：与 [package.py:31-32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L31-L32) 的实现一致；若使用真实提取产物，页几何的键应是从 1 连续递增的页码。

**待本地验证**：本实践依赖读者本地环境，请运行后核对输出。

#### 4.3.5 小练习与答案

**练习 1**：一个包只有 `chapters/` 而没有 `assets/`，且其中确实没有任何图片。`validate()` 该通过吗？为什么源码仍然要求目录存在？

**答案**：不该通过，会抛 `missing assets directory`。目录存在性是**结构契约**，内容是另一回事：渲染器遍历 `assets_path` 时不必先特判「没有资源目录」这种历史情况，代码路径更简单；且提取端 [extractor.py:33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py#L33) 已保证永远创建该目录，空目录是零成本的统一性。

**练习 2**：`MarkdownRenderer` 没有像 EPUB 渲染器那样 `require_toc=True`，这说明两种渲染器对包的依赖差在哪？

**答案**：Markdown 是「扁平文件流」，按 `chapters/` 顺序逐章输出即可，没有目录树也能渲染（只是没有导航结构）；EPUB 是「书容器」，spine、nav/ncx 都要从目录树生成，`toc.xml` 是硬依赖。同一份契约、两种消费强度，正好演示了「可选字段 + 按需强制」设计的价值。

**练习 3**：如果 `document.json` 整个不存在，`validate()` 与 `page_pixel_sizes()` 分别表现如何？

**答案**：`validate()` 通过（第 35 行以 `metadata_path.exists()` 为前提，跳过元数据检查）；`page_pixel_sizes()` 返回空字典 `{}`（[package.py:54-55](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L54-L55)）。此时 Markdown/EPUB 渲染不受影响，但 PDF 回写会被 [craft.py:326-327](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L326-L327) 的预检拦下——几何只对回写这一条链路是必需品。

## 5. 综合实践

**任务：给你的包写一份「体检报告」脚本。**

综合本讲三个模块，写一个 `package_doctor.py`（示例代码），对任意一个包目录输出体检报告：

1. 用 `DocumentPackage.from_path` 构建（模块一：包结构）。
2. 逐项报告五个成员的存在状态：`chapters/`、`assets/`、`toc.xml`（`has_toc()`）、`cover.png`（`has_cover()`）、`document.json`，并列出 `chapters/` 里 XML 文件的数量。
3. 分别尝试 `validate()` 与 `validate(require_toc=True)`，报告各自的通过/失败原因（模块三：校验逻辑）。
4. 若 `document.json` 存在，读取 `page_pixel_sizes()`，报告：页数、页码范围、每页尺寸是否一致；用 `dpi` 字段把每页换算成 PDF 点尺寸，并按 4.2.2 的公式抽一页验证（模块二：元数据 schema）。
5. 结尾给出结论：这个包能渲染 Markdown 吗？能渲染 EPUB 吗？能回写 PDF 吗？（三条判据分别对应：基础校验、`require_toc`、页几何非空。）

用一个真实提取产物（`package_path` 保留的那种，见 u1-l2）和一个手工搭的空壳包分别跑一遍，对比报告差异。全部结论应能从源码 [package.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py) 中找到依据。**待本地验证**。

## 6. 本讲小结

- `DocumentPackage` 只存路径不存数据：五个字段（`chapters_path`、`assets_path` 必填，`toc_path`、`cover_path`、`metadata_path` 可选），`from_path` 按固定命名约定把包根目录映射成结构化句柄。
- `document.json` 是包的说明书：`schema` 版本门卫、`bbox_coordinate_space: "ocr_pixels"` 与 `page_index_base: 1` 声明坐标约定、`dpi` + `page_pixel_sizes` 提供像素坐标 → PDF 点坐标的换算桥梁。
- 页几何由引擎在第四步 `write_metadata` 落盘：断点续跑时旧值与新渲染页用字典合并 `|` 取并集，SKIP 页的几何不丢失。
- `validate()` 是分层强化的安检：基础两条（chapters/assets 目录）人人要过，`require_toc=True` 由 EPUB 渲染器追加，页几何非空由 PDF 回写预检追加——谁需要什么，谁强制什么。
- `_parse_page_pixel_sizes` 的校验极为严格：页码 ≥1、尺寸恰两个正整数、连 `bool` 都排除——因为坐标错了不会崩，只会悄悄把白块画歪。
- 包是提取器与渲染器之间的**中立契约**：`ocr/` 缓存不属于契约；正因契约稳定，才能支撑「二次渲染零 OCR 成本」「手改 toc.xml 重切章节」「转换步骤链式派生新包」这些上层玩法。

## 7. 下一步学习建议

包的契约已经就位，下一讲进入消费它的第一条链路：**u6-l2「Markdown 段落模型与 HTML 安全过滤」**将先讲 `pdf_craft/markdown/paragraph/` 如何解析与过滤 HTML 标签（这是渲染 Markdown 的前置技能），随后 u6-l3 讲 `MarkdownRenderer` 如何遍历包的 `chapters/` 产出 Markdown 文件。

建议提前阅读的源码：

- [pdf_craft/renderer/markdown/renderer.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/markdown/renderer.py)——看渲染器如何只依赖 `DocumentPackage` 的字段工作。
- [tests/test_composable_boundaries.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py)——大量「手工搭最小包」的样板，是理解契约边界的最好教材。

若你对「页几何如何被回写消费」更感兴趣，可以直接跳到 u10-l1 预习 [pdf_craft/pipeline/pdf/pipeline.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/pdf/pipeline.py)，再回到主线。
