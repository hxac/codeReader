# PDF 处理抽象：PDFHandler 与页面引用

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `PDFHandler` 与 `PDFDocument` 两个 Protocol 各自抽象了哪些能力，以及为什么提取引擎只依赖协议而不依赖具体 PDF 库。
2. 读懂 `DefaultPDFHandler` / `DefaultPDFDocument` 的默认实现：pypdf 负责「结构信息」（页数、元数据、页面尺寸），Poppler（经 pdf2image）负责「像素渲染」。
3. 理解 `PageRefContext` 如何以上下文管理器的形式「打开一次文档、逐页迭代、统一关闭」，以及 `PageRef.render` 如何按文件大小上限自动压低 dpi。
4. 动手写一个自定义 `PDFHandler` 并通过 `PDFOptions(pdf_handler=...)` 注入提取流程。

## 2. 前置知识

- **Protocol（协议类型）**：Python 3.8+ `typing.Protocol` 提供的「结构化类型」。一个类只要拥有协议要求的方法签名，就算实现了协议，**不需要显式继承**。这叫鸭子类型的静态化。加上 `@runtime_checkable` 装饰器后，还可以在运行时用 `isinstance(obj, SomeProtocol)` 做检查。它带来的好处是**依赖倒置**：高层模块（OCR 驱动器）依赖抽象接口，而不是依赖某个具体的 PDF 库。
- **上下文管理器**：实现了 `__enter__` / `__exit__` 的对象，配合 `with` 语句使用，保证资源（文件句柄、网络连接）无论是否抛异常都能被释放。
- **PDF 的三个度量单位**：
  - **点（point，pt）**：PDF 内部坐标单位，1 英寸 = 72 点，这也是源码里 `_POINTS_PER_INCH = 72.0` 的由来。
  - **英寸（inch）**：`PDFDocument.page_size` 返回的单位（宽、高）。
  - **dpi（dots per inch）**：渲染密度。三者换算关系：\( \text{width}_{px} = \text{width}_{inch} \times \text{dpi} \)，而 \( \text{width}_{inch} = \text{width}_{pt} / 72 \)。所以 A4 页（约 8.27 英寸宽）在 300 dpi 下渲染出约 2480 像素宽的图像。
- **pypdf 与 Poppler 的分工**：pypdf 是纯 Python 库，能读 PDF 的结构（页数、metadata、mediabox），但不做位图渲染；Poppler 是 C++ 写的系统级工具集（`pdfinfo`、`pdftoppm` 等），pdf2image 这个 Python 包只是调用它把页面转成图像。这就是为什么第一讲强调 Poppler 必须单独安装——**换任何 OCR 后端都绕不开它**。
- **页码约定**：本讲涉及的所有 `page_index` 参数都是 **1 起始**，与 `ExtractionOptions.page_indexes` 的约定一致。

承接上一讲（u3-l1）：提取引擎 `PDFExtractionEngine` 四步主流程的第一步「OCR 循环」里，引擎并不直接碰 PDF 文件，而是通过本讲的主角——`PDFHandler` ——拿到一个已打开的文档对象再逐页渲染。本讲就拆开这个「PDF 访问层」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/pdf/handler.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py) | 定义 `PDFDocument` / `PDFHandler` 两个 Protocol，以及基于 pypdf + pdf2image 的默认实现 |
| [pdf_craft/pdf/page_ref.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py) | `pdf_pages_count` 工具函数、`PageRefContext` 上下文管理器、`PageRef` 页面引用与 dpi 自适应 |
| [pdf_craft/pdf/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py) | `PDFDocumentMetadata` 等数据类型定义 |
| [pdf_craft/pdf/ocr.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py) | 消费侧：OCR 驱动器如何持有、懒加载并使用 pdf_handler（下一讲 u3-l3 的主角，本讲只看它与 handler 的交界） |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/craft.py) | `PDFOptions.pdf_handler` 字段及其向引擎的传递路径 |
| [docs/en/TROUBLESHOOTING.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/TROUBLESHOOTING.md) | 官方排错指南，其中「PDF and Poppler」小节与本层直接相关 |

## 4. 核心概念与源码讲解

### 4.1 协议抽象：PDFHandler 与 PDFDocument

#### 4.1.1 概念说明

pdf-craft 需要从 PDF 里获取的能力一共五项：**数页数、读元数据、量页面尺寸、把某页渲染成图像、关闭文档**。这五项被拆成两层协议：

- `PDFDocument` —— 「一本已打开的书」：对应一次打开的 PDF 文档实例。
- `PDFHandler` —— 「开书器」：无状态工厂，负责把磁盘上的路径变成 `PDFDocument`。

为什么要拆两层、而不是一个「PDF 工具类」静态函数？因为**文档是需要关闭的资源**。把「打开」与「使用」分离后，调用方可以用 `with` 语句明确生命周期：handler 随时可复用（一次提取中可能被打开多次，例如读元数据一次、OCR 循环一次），而 document 用完即关。

对用户的实际价值：如果你有一套自己的 PDF 处理基础设施（比如预先渲染好的页面图像服务、或者别的渲染引擎），只要实现这五个方法，就能整层替换掉 pypdf + Poppler，OCR、目录分析、章节生成的所有代码原样工作。

#### 4.1.2 核心流程

```text
调用方（OCR 驱动器）
    │ 只依赖 PDFHandler 协议
    ▼
pdf_handler.open(pdf_path) ──► PDFDocument（协议）
    │                            ├─ pages_count        数页数
    │                            ├─ metadata()          读元数据
    │                            ├─ page_size(i)        第 i 页尺寸（英寸）
    │                            ├─ render_page(i, dpi) 第 i 页 → PIL Image
    │                            └─ close()             释放资源
    ▼
具体实现由 PDFOptions.pdf_handler 注入；缺省时懒加载 DefaultPDFHandler
```

注入链路（自上而下）：

1. 用户构造 `PDFOptions(pdf_handler=my_handler)`。
2. `PDFCraft._pdf_engine()` 首次提取时懒加载引擎，把该字段传入 `PDFExtractionEngine`（[pdf_craft/craft.py:253-262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262)）。
3. 引擎转手交给 OCR 驱动器（[pdf_craft/transform.py:24-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L24-L34)）。
4. OCR 驱动器在需要时（读元数据、进入识别循环、PDF 回写）调用 `open()`。

注意 PDF 回写管线也复用同一个字段：`patch_pdf_with_package` 把 `self._pdf.pdf_handler` 传给 `PDFTranslationPipeline`（[pdf_craft/craft.py:170-172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L170-L172)），所以自定义 handler 对「提取」和「翻译回写 PDF」两条链路同时生效。

#### 4.1.3 源码精读

两个协议的定义在 handler.py 开头，非常紧凑：

[pdf_craft/pdf/handler.py:12-28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py#L12-L28) —— 这里用 `@runtime_checkable` + `Protocol` 定义了 `PDFDocument`（五个成员）与 `PDFHandler`（单个 `open` 方法）。`PDFDocument.pages_count` 是 property 而不是方法，暗示它应当是廉价的可缓存查询。

`PDFOptions` 是协议的注入入口：

[pdf_craft/craft.py:38-45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L38-L45) —— 冻结 dataclass，`pdf_handler: PDFHandler | None = None` 与 `ocr` 并列，都属于「长期基础设施」配置（对比 `ExtractionOptions` 管「单次运行」），这是第二讲讲过的配置分层在本文件里的体现。

OCR 驱动器对协议的消费与懒加载兜底：

[pdf_craft/pdf/ocr.py:42-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L42-L51) —— `OCR.__init__` 只是保存 `pdf_handler`，不打开任何文件。

[pdf_craft/pdf/ocr.py:236-243](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L236-L243) —— `_get_pdf_handler`：用户没传时才创建 `DefaultPDFHandler()`，用「双检锁」（先无锁判断，再在锁内二次判断）保证线程安全。这与 `PDFCraft` 门面的惰性初始化是同一风格：**不提取 PDF 的用户不付出任何 PDF 基础设施成本**。

协议类型是公开 API 的一部分：

[pdf_craft/__init__.py:37-47](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L37-L47) —— 顶层包导出了 `DefaultPDFDocument`、`DefaultPDFHandler`、`PDFDocument`、`PDFDocumentMetadata`、`PDFHandler`、`pdf_pages_count`，即「协议 + 默认实现」整体对外，用户写自定义 handler 时直接 `from pdf_craft import DefaultPDFHandler, PDFDocument, PDFHandler` 即可。

#### 4.1.4 代码实践：验证协议是结构化的

实践目标：直观感受「不需要继承也算实现协议」。

操作步骤（示例代码，非项目原有代码）：

```python
from pdf_craft import DefaultPDFHandler, PDFDocument, PDFHandler

# DefaultPDFHandler 没有显式继承 PDFHandler，试试 isinstance
print(isinstance(DefaultPDFHandler(), PDFHandler))

document = DefaultPDFHandler().open(__import__("pathlib").Path("tests/assets/space.pdf"))
print(isinstance(document, PDFDocument))
document.close()
```

需要观察的现象：两行都应打印 `True` —— 这就是 `@runtime_checkable` 的效果；而库内部其实在正常路径上根本不做这个检查，协议主要服务于类型标注与「约定文档」。

预期结果：`True` / `True`。（Poppler 未安装时 `open` 仍可成功，因为打开走的是 pypdf；只有 `render_page` 才需要 Poppler。）

#### 4.1.5 小练习与答案

**练习 1**：`PDFDocument` 协议有五个成员，哪个是 property、哪些是方法？为什么 `pages_count` 适合做成 property？

**答案**：`pages_count` 是 property，`metadata()`、`page_size()`、`render_page()`、`close()` 是方法。页数是文档的固有属性、查询廉价且结果不变（默认实现还会缓存它），适合以属性形式暴露；其余操作要么有参数（页码、dpi）、要么有副作用（渲染耗资源、关闭改变状态），用方法更贴切。

**练习 2**：如果把 `PDFHandler.open` 设计成模块级函数 `open_pdf(path)`，会损失什么能力？

**答案**：损失「带配置的复用」。`DefaultPDFHandler` 的构造参数 `poppler_path` 是 handler 实例持有的状态，工厂可以带着不同 Poppler 路径在多次 `open` 之间复用；模块级函数没有地方挂这种状态，只能靠全局变量或每次额外传参。协议对象还便于注入（`PDFOptions(pdf_handler=...)`）和替换。

### 4.2 默认实现：DefaultPDFHandler 与 DefaultPDFDocument

#### 4.2.1 概念说明

默认实现回答一个问题：**一本普通 PDF，这五项能力分别用什么技术实现？**

| 协议成员 | 实现技术 | 说明 |
| --- | --- | --- |
| `pages_count` | pypdf | 数 `reader.pages` 长度，结果缓存 |
| `metadata()` | pypdf | 读 `/Title`、`/Subject`、`/Author`、`/ModDate` |
| `page_size()` | pypdf | 读 `mediabox`（单位：点），除以 72 转成英寸 |
| `render_page()` | pdf2image → Poppler | 唯一需要系统依赖的成员 |
| `close()` | pypdf | 关闭底层文件流 |

一个容易踩的坑：**pypdf 能打开 ≠ Poppler 能渲染**。损坏的交叉引用表、加密、异常页面尺寸都可能让渲染阶段单独失败，所以 `render_page` 有自己的错误包装（见下文）。官方排错指南专门指出：Poppler 对本地与远程 OCR 都是必需的，换 OCR 后端解决不了 `Poppler not found in PATH`（[docs/en/TROUBLESHOOTING.md:19-27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/TROUBLESHOOTING.md#L19-L27)）。

#### 4.2.2 核心流程

`DefaultPDFHandler`（工厂）与 `DefaultPDFDocument`（文档）的生命周期：

```text
DefaultPDFHandler(poppler_path=None)     ← 一次构造，可长期复用
        │ open(pdf_path)
        ▼
DefaultPDFDocument(pdf_path, poppler_path)
        │ __init__: 惰性 import pypdf，创建 PdfReader
        │ pages_count 首次访问时缓存
        │ metadata()/page_size()/render_page() 按需调用
        ▼
close() 关闭 reader.stream
```

`render_page` 的错误处理优先级：

1. `PDFInfoNotInstalledError`（找不到 Poppler）→ 抛 `PDFError`，并按「指定了 poppler_path」与「依赖系统 PATH」两种情况给出不同的可操作提示。
2. 返回空图像列表 → 抛 `RuntimeError`（渲染失败）。
3. 图像非 RGB → 转成 RGB 再返回（OCR 模型期望三通道输入）。

#### 4.2.3 源码精读

工厂部分：

[pdf_craft/pdf/handler.py:31-41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py#L31-L41) —— `DefaultPDFHandler` 只存一个可选的 `poppler_path`；`open` 每次都新建一个 `DefaultPDFDocument`。注意它连 `PDFHandler` 协议都没有继承——结构化协议下这是合法的。

文档构造与页数缓存：

[pdf_craft/pdf/handler.py:44-60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py#L44-L60) —— `import pypdf` 放在 `__init__` **函数体内**而非模块顶部：只有真正打开 PDF 时才加载 pypdf，延续了整个代码库「按需导入重依赖」的纪律（与 `PDFCraft` 懒加载引擎同款）。`_pages_count` 初始为 `None`，首次访问 `pages_count` 才遍历 `reader.pages` 并缓存——这就是本讲标题里「缓存页面渲染」的第一层含义：**元信息只算一次**。

元数据解析：

[pdf_craft/pdf/handler.py:62-132](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py#L62-L132) —— `metadata()` 把 pypdf 的原始字典映射为 `PDFDocumentMetadata`（字段见 [pdf_craft/pdf/types.py:32-41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L32-L41)）。三个值得注意的细节：作者字段按 `;`、`,`、`&` 依次尝试切分（一本书常有多个作者挤在一个字符串里）；`/ModDate` 按 PDF 日期格式 `D:YYYYMMDDHHmmSS...` 手工解析，失败则退回当前 UTC 时间；`publisher`、`isbn` 等字段恒为空，因为 PDF 标准元数据里没有这些概念（EPUB 渲染时这些信息来自 `book_meta` 参数）。整个函数体被 `try/except` 包住，任何异常都转成 `PDFError`——**协议要求实现层不能把自家库的异常漏给上层**。

页面尺寸（点 → 英寸）：

[pdf_craft/pdf/handler.py:134-143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py#L134-L143) —— `page_size` 读 `page.mediabox` 的宽高（点），除以 `_POINTS_PER_INCH = 72.0` 得到英寸。注意 `self._reader.pages[page_index - 1]`：协议对外 1 起始，pypdf 内部 0 起始，减一转换。返回英寸而不是点，是为了让上层能直接做「英寸 × dpi = 像素」的换算。

渲染（本层唯一重操作）：

[pdf_craft/pdf/handler.py:145-179](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py#L145-L179) —— `render_page` 调 `pdf2image.convert_from_path`，用 `first_page=page_index, last_page=page_index` 只渲染单页（避免整本渲染）。`PDFInfoNotInstalledError` 被转换成带明确指引的 `PDFError`：指定了 `poppler_path` 就提示「指定路径下没找到」，否则提示「装 Poppler 或修 PATH」。最后强制转 RGB。

关闭：

[pdf_craft/pdf/handler.py:181-182](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/pdf/handler.py#L181-L182) —— `close` 只关闭 reader 的文件流；pypdf 的 `PdfReader` 持有打开的文件对象，不关会泄漏文件句柄（长批量任务里尤其重要）。

#### 4.2.4 代码实践：直接调用 render_page 对比不同 dpi

实践目标：亲手验证「英寸 × dpi = 像素」，并确认打开文档不需要 Poppler、渲染才需要。

操作步骤（示例代码）：

```python
from pathlib import Path
from pdf_craft import DefaultPDFHandler

pdf_path = Path("tests/assets/space.pdf")  # 仓库自带的小 PDF
document = DefaultPDFHandler().open(pdf_path)
try:
    print("页数:", document.pages_count)
    width_inch, height_inch = document.page_size(1)
    print(f"第 1 页尺寸: {width_inch:.2f} x {height_inch:.2f} 英寸")
    for dpi in (72, 150, 300):
        image = document.render_page(1, dpi)
        expected = (round(width_inch * dpi), round(height_inch * dpi))
        print(f"dpi={dpi:>3} -> 实际 {image.size}，按公式预期约 {expected}")
finally:
    document.close()
```

需要观察的现象：`page_size` 返回的英寸数值乘以 dpi 后，与 `image.size` 基本吻合（允许 1~2 像素的舍入误差）；dpi 翻倍，像素数约翻倍，宽高两个方向同时缩放。

预期结果：三条 dpi 记录的实际尺寸与公式预期一致；若 Poppler 未安装，会在 `render_page` 处抛出 `PDFError`，提示信息正是 handler.py:169 那句 "Poppler not found in PATH..."，而前面的页数与尺寸打印不受影响。具体像素数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`metadata()` 中 `/ModDate` 解析失败时为什么「静默退回当前时间」而不是抛异常？

**答案**：元数据是锦上添花的信息，不是提取的必要条件。上层 `_extract_book_meta` 对 `PDFError` 的处理也只是打印警告并返回 `None`（transform.py:137-139），随后书名回退为文件名主干。若因一个格式怪异的日期字段中断整本书的转换，代价与收益完全不成比例。

**练习 2**：`render_page` 里 `poppler_path=None` 与传入路径两种情况，错误提示为什么分开写？

**答案**：两种失败对应两种修法。`poppler_path=None` 意味着依赖系统 `PATH`，提示应引导用户安装 Poppler 或修正 PATH；指定了路径则说明用户已经尝试配置但路径写错了，提示应指向「检查该路径」。共享一句话会让其中一类用户按错误方向排查。这正是排错指南「Quick diagnosis」表格把 `Poppler not found in PATH` 列在首位的原因。

**练习 3**：为什么 `import pypdf` 写在 `__init__` 里、`from pdf2image import convert_from_path` 写在 `render_page` 里，而不是都放模块顶部？

**答案**：把重依赖的导入推迟到最后使用时刻，缩短无关路径上的导入时间与内存占用。只翻译现成 EPUB 的用户从头到尾不会执行到这两行；进一步地，读元数据、量页数只需要 pypdf，不需要 pdf2image，所以两个导入的时机也不同。

### 4.3 页面引用上下文：PageRefContext 与 PageRef

#### 4.3.1 概念说明

有了 handler 和 document，还差一个「遍历骨架」：提取循环需要的是「按页迭代 + 保证关闭 + 统一的页码与错误语义」。`page_ref.py` 用三个组件补齐：

- `pdf_pages_count(pdf_path, pdf_handler=None)`：数页数的一次性工具（打开→读→关闭）。
- `PageRefContext`：上下文管理器，`with` 进入时打开文档、退出时关闭；可迭代，产出 `PageRef` 序列。
- `PageRef`：一页的「引用」——只持有 `(document, page_index)`，**创建它不渲染**，调用 `render()` 才真正渲染。这是典型的惰性设计：OCR 循环里被 `page_indexes` 排除的页只付出一个轻量对象，不付出渲染成本。

`PageRef.render` 还承担 dpi 自适应：当调用方给了 `max_image_file_size`（页面图像文件大小上限）时，它会根据页面英寸尺寸反推「不超过该大小所能容忍的最大 dpi」，与传入 dpi 取小者。

#### 4.3.2 核心流程

`PageRefContext` 在 OCR 识别循环中的用法（消费侧代码）：

```text
with PageRefContext(pdf_path, pdf_handler) as refs:   # 打开一次文档
    pages_count = refs.pages_count
    for ref in refs:                                   # 逐页产出 PageRef（1 起始）
        # （OCR 循环在此做 page_indexes 过滤、缓存 SKIP、中止检查）
        image = ref.render(dpi=300 或用户指定, max_image_file_size=...)
        # 交给 PageExtractor 做 OCR
# with 退出 → document.close()
```

「缓存」在本层的三层含义（务必分清）：

1. **文档级**：整个识别循环只 `open`/`close` 一次，pypdf 只解析一遍 PDF 结构，所有页共享。
2. **元信息级**：`DefaultPDFDocument.pages_count` 只计算一次（见 4.2.3）。
3. **几何级**：OCR 驱动器把每页渲染出的 `image.size` 记入 `page_pixel_sizes.json`（[pdf_craft/pdf/ocr.py:151-157](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L151-L157)），断点续跑与 PDF 回写都从这里取页面坐标系。

而**页面位图本身不做内存缓存**——每次 `render` 都重新调用 Poppler。位图结果经 OCR 后以 `page_N.xml` 形式落盘缓存，那是下一讲（u3-l3）的主题。

dpi 自适应的推导：设页面宽高为 \( w_{inch}, h_{inch} \)，RGB 图像每像素 3 字节，PNG 压缩比保守估计 0.5，则文件大小近似

\[
S \approx (w_{inch} \cdot \text{dpi}) \times (h_{inch} \cdot \text{dpi}) \times 3 \times 0.5
\]

解出 dpi 上限：

\[
\text{dpi}_{max} = \sqrt{\frac{S}{w_{inch} \cdot h_{inch} \cdot 3 \times 0.5}}
\]

最终取 \( \text{dpi} = \min(\text{dpi}_{请求}, \text{dpi}_{max}) \)。

#### 4.3.3 源码精读

一次性工具函数：

[pdf_craft/pdf/page_ref.py:11-27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py#L11-L27) —— `pdf_pages_count` 是「open → 读页数 → finally close」的模板：`PDFError` 原样上抛，其他异常统一包装成 `PDFError("Failed to parse PDF document.")`；`finally` 保证关闭。handler 参数可选，缺省用默认实现。这个「except PDFError: raise / except Exception: 包装 / finally: 关闭」的三段式是本文件反复出现的错误处理范式。

上下文管理器本体：

[pdf_craft/pdf/page_ref.py:30-66](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py#L30-L66) —— `__enter__` 调用 `pdf_handler.open`，非 `PDFError` 异常包装后上抛；`__exit__` 关闭文档并把引用置空（可防御重复退出）。`__iter__` 是生成器：`for i in range(pages_count)` 逐个 `yield PageRef(document, page_index=i + 1)`——**1 起始页码在这里诞生**，并贯穿整个提取链路。注意它不做页码过滤：`PageRefContext` 总是迭代全部页，页选择（`IGNORE` 事件）由 OCR 循环里的 `page_indexes` 判断完成（[pdf_craft/pdf/ocr.py:115-124](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L115-L124)）。

页面引用与渲染：

[pdf_craft/pdf/page_ref.py:73-111](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py#L73-L111) —— `PageRef` 构造只存两个引用（零成本）；`render` 先处理大小上限：取 `document.page_size`，算 `max_dpi`，与请求 dpi 取小，再调 `document.render_page`。错误处理同样规范：`PDFError` 补上 `page_index` 再抛（上游报错就能定位到页），未知异常包装成带页码的 `PDFError`。

dpi 反推公式：

[pdf_craft/pdf/page_ref.py:113-121](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py#L113-L121) —— `_dpi_with_size` 就是上面的开平方公式，两个经验常数 `_PNG_COMPRESSION_RATIO = 0.5`、`_BYTES_PER_PIXEL = 3` 定义在 [pdf_craft/pdf/page_ref.py:69-70](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py#L69-L70)。由于是「保守估计」，算出的 dpi 偏小是安全方向——图像只会比上限更小，不会超。

#### 4.3.4 代码实践：观察「迭代不渲染、render 才渲染」

实践目标：验证 PageRef 的惰性——迭代整本 PDF 不触发任何渲染。

操作步骤（示例代码）：

```python
from pathlib import Path
from pdf_craft.pdf.handler import DefaultPDFHandler
from pdf_craft.pdf.page_ref import PageRefContext

with PageRefContext(
    pdf_path=Path("tests/assets/space.pdf"),
    pdf_handler=DefaultPDFHandler(),
) as refs:
    print("总页数:", refs.pages_count)
    refs_list = list(refs)          # 只产出轻量引用，不渲染
    print("产出 PageRef 数:", len(refs_list), "页码:",
          [r.page_index for r in refs_list])
    image = refs_list[0].render(dpi=100)   # 此刻才调用 Poppler
    print("第 1 页 100dpi 尺寸:", image.size)
# with 退出后文档已关闭
```

需要观察的现象：`list(refs)` 瞬间完成（没有 Poppler 调用）；只有 `render` 那一行有可感知耗时；页码列表从 1 开始。

预期结果：`产出 PageRef 数` 等于总页数，页码为 `[1, 2, ..., N]`；render 返回的尺寸约等于 `page_size × 100`。若在 `with` 块外调用 `refs_list[0].render(...)`，会在已关闭的 reader 上报错，从而验证生命周期约束。具体页数与尺寸**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`ExtractionOptions.page_indexes={1,2}` 时，`PageRefContext` 会少产出几页吗？

**答案**：不会。`PageRefContext.__iter__` 总是产出全部页的 `PageRef`；过滤发生在 OCR 循环里——`ref.page_index not in page_indexes` 时直接 `continue` 并发 `IGNORE` 事件。被排除的页只付出一个轻量 PageRef 对象，没有渲染成本。

**练习 2**：一本 6×9 英寸的书，`max_page_image_file_size` 设为 5 MB（5,242,880 字节）时，dpi 上限约是多少？

**答案**：代入公式 \[ \text{dpi}_{max} = \sqrt{\frac{5242880}{6 \times 9 \times 3 \times 0.5}} = \sqrt{\frac{5242880}{81}} \approx \sqrt{64727} \approx 254 \] 约 254 dpi。若用户请求 dpi=300，则实际按 254 渲染；若请求 150，则保持 150（`min` 取小者）。

**练习 3**：`PageRef.render` 里 `except PDFError as error: error.page_index = self._page_index` 这两行的价值是什么？

**答案**：底层 `PDFError` 可能不带页码（如 `metadata()` 抛的 `page_index=None`），而 `PageRef` 恰好知道自己代表哪一页。在这里补写页码后，上层无论记日志、触发 `ignore_pdf_errors` 谓词还是向用户报告，都能精确到页，不必再靠调用栈猜测。

## 5. 综合实践

**任务：写一个可观测的 LoggingPDFHandler，注入完整提取链路。**

这是本讲规格指定的综合实战，把三个模块串起来：协议抽象（模块 1）+ 默认实现（模块 2）+ 上下文生命周期（模块 3）。

第一步，实现包装式自定义 handler（示例代码）：

```python
# logging_handler.py
from pathlib import Path
from pdf_craft import DefaultPDFHandler, PDFDocument, PDFHandler


class LoggingPDFHandler:
    """包装 DefaultPDFHandler：每次打开文档时打印路径与页数。"""

    def __init__(self, poppler_path=None) -> None:
        self._inner = DefaultPDFHandler(poppler_path)

    def open(self, pdf_path: Path) -> PDFDocument:
        document = self._inner.open(pdf_path)
        print(f"[LoggingPDFHandler] open {pdf_path.name} -> {document.pages_count} pages")
        return document


if __name__ == "__main__":
    print(isinstance(LoggingPDFHandler(), PDFHandler))  # True：结构化协议
```

第二步，注入并跑一次提取（示例代码）：

```python
# run_extract.py
from pathlib import Path
from pdf_craft import DeepSeekOCRVendorConfig, ExtractionOptions, PDFOptions, PDFCraft
from logging_handler import LoggingPDFHandler

options = PDFOptions(
    ocr=DeepSeekOCRVendorConfig(
        base_url="...", api_key="...", model="..."   # 换成你的 OCR 服务凭据
    ),
    pdf_handler=LoggingPDFHandler(),
)
craft = PDFCraft(pdf=options)
craft.convert_pdf_to_markdown(
    source="tests/assets/space.pdf",
    output="out/space.md",
    package_path="out/package",                     # 保留中间产物便于检查
    extraction=ExtractionOptions(page_indexes={1}),  # 只处理第 1 页，省时省钱
)
```

第三步，观察并回答三个问题：

1. `[LoggingPDFHandler] open ...` 打印了几次？（提取链路里文档被打开不止一次：读元数据、进入识别循环各一次，可对照 4.1.2 的注入链路解释每一次打开分别服务谁。）
2. 把 `LoggingPDFHandler.__init__` 里的 `poppler_path` 指向一个不存在的目录，重跑后报错发生在哪一步、错误信息是哪一句？（对照 4.2.3 的 `render_page` 错误分支。）
3. 用 4.2.4 的脚本量出该 PDF 第 1 页的英寸尺寸，再手算 300 dpi 下的预期像素，与中间产物 `out/package/ocr/page_pixel_sizes.json` 里记录的数值对比，验证「英寸 × dpi = 像素」贯穿全链路。

预期结果：第 1 问能看到至少两次 open 日志（元数据 + 识别循环；具体次数**待本地验证**）；第 2 问应在渲染页面时抛 `PDFError`，信息为 "Poppler not found at specified path: ..."；第 3 问两侧数值应一致（允许舍入误差）。

## 6. 本讲小结

- `PDFHandler`（工厂）与 `PDFDocument`（文档）是两个 `@runtime_checkable` 协议，把「读结构」与「渲染像素」抽象成五个方法，OCR 引擎只依赖协议，具体实现经 `PDFOptions.pdf_handler` 注入，缺省时在 OCR 驱动器内用双检锁懒加载 `DefaultPDFHandler`。
- 默认实现分工明确：pypdf 负责页数、元数据（`/Title`、`/Author` 切分、`/ModDate` 手工解析）、页面尺寸（mediabox 点值 ÷ 72 转英寸）；pdf2image 调 Poppler 负责单页渲染并强制转 RGB，找不到 Poppler 时给出区分「指定路径错误」与「PATH 未配置」两种提示。
- 页码语义在本层定型：协议与 `PageRef` 全部 1 起始，进入 pypdf 时才减一。
- `PageRefContext` 是「打开一次、逐页迭代、统一关闭」的骨架，但不过滤页码；`PageRef` 是惰性引用，`render()` 才触发渲染，并按 `max_image_file_size` 用 \(\text{dpi}_{max}=\sqrt{S/(w\cdot h\cdot 3\cdot 0.5)}\) 自动压低 dpi。
- 本层的「缓存」指文档句柄、页数元信息与页面像素几何（`page_pixel_sizes.json`）；位图本身不缓存，其 OCR 结果以 `page_N.xml` 落盘缓存——那是下一讲的内容。
- 错误处理范式统一：底层异常一律包装为 `PDFError` 并尽量补上 `page_index`，使上层（`ignore_pdf_errors`、事件回调、日志）能精确到页。

## 7. 下一步学习建议

- 下一讲 **u3-l3「OCR 驱动器：事件流、缓存与断点续跑」**：本讲的 `PageRefContext` 正是 OCR 循环的迭代骨架，下一讲深入 `OCR.recognize` 生成器，看六种 `OCREvent`、`page_N.xml` 磁盘缓存与 `done` 标记如何协作实现断点续跑。
- 想先巩固本层，可重读 [pdf_craft/pdf/handler.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/handler.py) 与 [pdf_craft/pdf/page_ref.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_ref.py)，两文件合计不足 250 行，适合完整通读一遍。
- 关注「页面几何」去向的读者可以提前翻看 `DocumentPackage.write_metadata`（transform.py:117-120）与 PDF 回写管线（u10 单元），理解 `page_pixel_sizes` 为什么是提取与回写之间的坐标系契约。
- 遇到 `Poppler not found` 类问题时，按 [docs/en/TROUBLESHOOTING.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/TROUBLESHOOTING.md) 的「Quick diagnosis」表格逐层定位：先 PDF 读取与渲染（本层），再 OCR，再 LLM 翻译。
