# 文档处理器与格式识别

## 1. 本讲目标

上一讲（u3-l1）我们看清了 `fz_document` / `fz_page` 的「派生结构体 + 函数指针表」多态：通用层只存回调指针，格式专用层填表，通用层用「判空 + 转发」调度。但有一个关键问题被刻意留到了本讲：

> `fz_open_document(ctx, "a.pdf")` 调用时，MuPDF 是怎么知道这个文件该交给 PDF 的代码、而不是 XPS 或 EPUB 的代码去处理的？

本讲就回答这个问题。学完后你应当能够：

- 说出 `fz_register_document_handlers` 到底登记了哪些格式处理器，以及它们是如何被条件编译裁剪的。
- 读懂 `struct fz_document_handler` 的每一个字段，理解 `recognize`、`open`、`recognize_content` 三组回调各管什么。
- 说清「按扩展名 / mimetype 识别」与「按文件内容（魔数）识别」这两条打分路径，以及它们如何被组合成一个最终得分来选出唯一的 handler。
- 自己写代码只注册某一种 handler，并预测给定文件能否被识别。

---

## 2. 前置知识

本讲默认你已经掌握 u3-l1 的核心结论：

- `fz_document` 是格式无关的统一文档抽象；打开它用 `fz_open_document`。
- MuPDF 是双层架构：fitz 通用层定义抽象，格式专用层（PDF/XPS/HTML…）各自把本格式翻译成 fitz 抽象。
- 几乎所有 fitz 调用的第一个参数都是 `fz_context *ctx`。

此外需要两个 C 语言常识：

- **函数指针表（手写虚表）**：C 没有面向对象，但可以把一组函数指针放进结构体里，谁要支持某接口就填上自己的函数地址，调用方通过结构体里的指针间接调用——这等价于其他语言里的「接口 + 实现」。
- **魔数（magic number）**：很多文件格式在文件头放几个固定字节作「签名」，例如 PDF 文件几乎都以 `%PDF-` 开头。读到这几个字节就能高概率判断格式，不必解析整个文件。

一个直觉类比：MuPDF 维护着一张「**格式快递分拣表**」。你递给它一个文件，它不是靠猜，而是把文件同时拿给表里每一种格式问一句「这是你的菜吗？打分 0 到 100」，最后选得分最高的那种去真正打开。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `source/fitz/document-all.c` | 处理器注册表：`fz_register_document_handlers` 把所有内置 handler 一次性登记进 context。 |
| `include/mupdf/fitz/document.h` | 定义 `struct fz_document_handler` 与识别相关的函数原型（`fz_recognize_document` 等）。 |
| `source/fitz/document.c` | 注册、识别、打开的核心实现：打分循环、magic/content 两条路径、`fz_open_document` 的总入口。 |
| `source/pdf/pdf-xref.c` | PDF handler 的真实填表实例：`pdf_document_handler`、扩展名/mimetype 表、内容识别函数。 |

---

## 4. 核心概念与源码讲解

### 4.1 处理器注册表：document-all.c 与 fz_register_document_handler

#### 4.1.1 概念说明

MuPDF 支持十几种输入格式，但你的程序调用 `fz_open_document` 时并不会去遍历所有 `.c` 文件——它只会查找一张「**已注册处理器列表**」。这张列表存在 `fz_context` 里（字段名 `ctx->handler`）。

往这张列表里添加一项的动作叫「注册」，由两个函数承担：

- `fz_register_document_handler(ctx, 单个handler)`：注册**一个** handler。这是唯一的注册原语。
- `fz_register_document_handlers(ctx)`（注意末尾多了一个 `s`）：注册**全部**内置 handler。它内部就是一连串调用上面那个单数版本。

为什么要有「全部」版本？因为绝大多数程序都希望「能开什么就开什么」，逐个手写注册太繁琐，于是 MuPDF 提供了一个一键全注册的便捷函数。

#### 4.1.2 核心流程

注册表在内存里其实就是一个简单的**指针数组**（容量上限 32）：

```
fz_document_handler_context
├── count                       // 已注册数量
└── handler[0..count-1]         // 指向各 handler 结构体的指针
```

注册流程：

1. `fz_register_document_handler` 检查传入的 handler 是否已经在数组里（按指针去重，防止重复注册）。
2. 若不在，且数组未满（`count < 32`），就追加到末尾，`count++`。
3. 数组满则抛出 `FZ_ERROR_LIMIT` 异常。

`fz_register_document_handlers` 则依次把 PDF、XPS、SVG、CBZ、IMG、FB2、HTML、XHTML、Markdown、MOBI、TXT、Office、EPUB、GZ 共 14 个内置 handler 注册进去，每个都被 `#if FZ_ENABLE_*` 包裹——编译期没启用的格式，对应的注册行根本不会被编译进来。

#### 4.1.3 源码精读

先看「全部注册」的便捷函数，注意每个 `fz_register_document_handler` 都被条件编译宏保护：

[document-all.c:40-80](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L40-L80) — `fz_register_document_handlers`：逐个注册 14 种格式 handler，每个都包在 `#if FZ_ENABLE_*` 内。

注意三个细节：

- 文件顶部用 `extern` 声明了 14 个 handler 全局变量（[document-all.c:25-38](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L25-L38)），它们的真正定义散落在各自的格式源码里（PDF 的在 `pdf-xref.c`，我们 4.2 会看到）。
- `md_document_handler`（Markdown）嵌套在 `#if FZ_ENABLE_HTML` 里（[document-all.c:60-66](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L60-L66)），因为 Markdown 复用 HTML 排版引擎，HTML 没启用时 Markdown 也无从工作。
- `gz_document_handler` **没有**任何 `#if` 包裹（[document-all.c:79](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L79)），它处理的是「外层 gzip 压缩包」，任何格式都可能被 gz 包一层，因此始终注册。

再看真正的注册原语，它做去重和容量检查：

[document.c:194-214](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L194-L214) — `fz_register_document_handler`：按指针去重后追加到数组末尾，超过 32 个抛异常。

去重的意义：同一个 handler 重复注册（比如用户既调了 `fz_register_document_handlers` 又手动注册了 PDF）不会产生重复项，识别时也就不会重复打分。

#### 4.1.4 代码实践

**实践目标**：亲手追踪 PDF 处理器的注册路径，确认「全注册」与「单注册」的关系。

**操作步骤**：

1. 打开 `source/fitz/document-all.c`，找到 `fz_register_document_handlers` 函数。
2. 定位其中 PDF 那一行：`fz_register_document_handler(ctx, &pdf_document_handler);`（被 `#if FZ_ENABLE_PDF` 保护）。
3. 全局搜索 `pdf_document_handler` 的**定义**（不是声明），你会找到它在 `source/pdf/pdf-xref.c` 中。
4. 阅读该定义，对照本讲 4.2 节理解它的每个字段。

**需要观察的现象**：`document-all.c` 里只有 `extern` 声明（告诉编译器「这个符号在别处定义」），真正的填表代码在格式自己的源文件里。这种「声明集中、定义分散」的布局，正是 MuPDF 双层架构的体现——通用层只引用符号，不关心实现。

**预期结果**：能在 `pdf-xref.c` 中找到 `fz_document_handler pdf_document_handler = { ... };`，其字段顺序与 `struct fz_document_handler` 一一对应（见 4.2.3）。

---

### 4.2 handler 回调集合：fz_document_handler 结构

#### 4.2.1 概念说明

每种格式要被 MuPDF 接纳，必须填好一张「**应聘登记表**」——也就是 `struct fz_document_handler`。这张表告诉通用层两件事：

1. **我认不认识这种文件**（识别回调）。
2. **认得的话，怎么把它打开**（打开回调）。

除此之外，还要附上「我能处理的扩展名和 mimetype 清单」，以及两个开关 `wants_dir` / `wants_file`，声明这种格式打开时是否需要一个目录上下文或一个真实磁盘文件。

#### 4.2.2 核心流程

一个 handler 的生命周期：

```
注册时：把填好的 handler 结构体指针塞进 ctx->handler 数组
   │
打开文档时：
   ├─ 调 recognize(magic)         → 给扩展名/mimetype 打分
   ├─ 调 recognize_content(stream)→ 给文件内容打分
   ├─ 综合得分，选出最佳 handler
   └─ 调 best_handler->open(...)  → 真正构造 fz_document 返回
   │
程序退出时：
   └─ 调 fin(handler)（若有）→ 释放 handler 持有的资源
```

识别（recognize）和打开（open）是分离的两个回调，这一点很关键：MuPDF 会先用识别回调把所有 handler 问一遍、挑出唯一赢家，然后**只**对赢家调一次 open。这样即便注册了 14 种格式，每次打开也只会真正初始化一种。

#### 4.2.3 源码精读

先看结构体定义本身，共 8 个字段：

[document.h:1127-1138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1127-L1138) — `struct fz_document_handler`：handler 的「应聘登记表」。

逐字段含义：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `recognize` | `fz_document_recognize_fn *` | 按 magic 字符串（扩展名/mimetype）打分，返回 0~100。 |
| `open` | `fz_document_open_fn *` | 真正打开文档，返回 `fz_document *`。 |
| `extensions` | `const char **` | 支持的扩展名数组（如 `"pdf"`），以 `NULL` 结尾。 |
| `mimetypes` | `const char **` | 支持的 mimetype 数组（如 `"application/pdf"`），以 `NULL` 结尾。 |
| `recognize_content` | `fz_document_recognize_content_fn *` | 按文件**内容**（魔数）打分，返回 0~100。 |
| `wants_dir` | `int` | 打开时是否需要目录上下文（HTML 等关联资源时用）。 |
| `wants_file` | `int` | 是否必须是一个真实磁盘文件（而非纯内存流）。 |
| `fin` | `fz_document_handler_fin_fn *` | 程序退出时调用的收尾函数，可空。 |

再看回调函数的签名约定，理解识别回调返回的是「置信度分数」：

[document.h:385](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L385) — `fz_document_recognize_fn`：注释明确「返回 0（不认）到 100（完全确定）之间的数」。

[document.h:412](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L412) — `fz_document_recognize_content_fn`：内容识别，多出 `recognize_state` / `free_recognize_state` 两个输出参数，用于把识别阶段已解析的中间结果（如 zip 目录）直接传给 open，避免重复解析。

[document.h:370](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L370) — `fz_document_open_fn`：真正打开文档的回调签名，参数包括 stream、accel（加速数据）、dir（目录上下文）、以及来自识别阶段的 `recognize_state`。

现在看 PDF 是怎么填这张表的，它用的是**位置初始化**（按字段顺序依次填）：

[pdf-xref.c:3846-3853](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3846-L3853) — `pdf_document_handler`：PDF 格式的 handler 填表，5 个位置分别对应 `recognize=NULL / open / extensions / mimetypes / recognize_content`。

对照结构体字段顺序可以读出：

- `recognize = NULL`：PDF **不**实现按 magic 字符串的自定义打分，完全依赖扩展名/mimetype 表和内容识别。
- `open = open_document`：真正打开函数。
- 后三项是 `extensions`、`mimetypes`、`recognize_content`。

PDF 的扩展名与 mimetype 表（注意都是以 `NULL` 结尾的字符串数组）：

[pdf-xref.c:3783-3797](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3783-L3797) — `pdf_extensions` 与 `pdf_mimetypes`：PDF 认领 `.pdf .fdf .pclm .ai` 四种扩展名，以及两种 mimetype。

PDF 的打开回调极其简短，把活儿全转给了已有的流式打开函数：

[pdf-xref.c:3838-3844](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3838-L3844) — `open_document`：handler 的 open 回调只是对 `pdf_open_document_with_stream` 的薄封装。

这正是 u3-l1 讲过的「派生层把活儿翻译成通用层能懂的形式」——handler 的 open 回调是格式专用层与通用层之间的**适配胶水**。

#### 4.2.4 代码实践

**实践目标**：对比 PDF 与另一种格式（图片 IMG）的 handler 填表，体会「同构」。

**操作步骤**：

1. 在 `source/pdf/pdf-xref.c` 看 `pdf_document_handler`（上面已引用）。
2. 打开 `source/cbz/muimg.c`，找到 `img_document_handler` 的定义。

**需要观察的现象**：两个 handler 的结构完全同构——都是同样顺序的 5 个字段，只是填了各自的 `open` 函数、扩展名表、mimetype 表和 `recognize_content` 函数。

**预期结果**：你会得出一个重要结论：**新增一种输入格式 = 写一个这样的结构体 + 实现它的回调 + 在 document-all.c 注册一行**（这正是 u10-l3「扩展 handler」的主题）。分发与识别逻辑完全不用改。

---

### 4.3 magic 与内容识别：两条打分路径

#### 4.3.1 概念说明

识别一个文件该用哪种格式，有两条互补的线索：

- **magic（名字）线索**：看文件名扩展名（`.pdf`）或 mimetype（`application/pdf`）。优点是快，缺点是不可靠——文件完全可以被改错扩展名。
- **content（内容）线索**：读文件开头几个字节，看魔数签名（PDF 的 `%PDF-`）。优点是准，缺点是要读盘、且有的格式没有明显签名。

MuPDF **同时**用这两条线索，把它们各自换算成一个分数，再用一套组合规则得出总分，选总分最高的 handler。这套打分机制的核心代码在 `do_recognize_document_stream_and_dir_content`。

#### 4.3.2 核心流程

打分循环对每一个已注册 handler 计算两个分：

```
对每个 handler[i]:
    score       = recognize_content(stream)    // 内容分，0~100（若有流且 handler 实现）
    magic_score = recognize(magic)             // 自定义 magic 分（若有）
    若 magic 命中 mimetype 表  → magic_score = 100
    若 magic 的扩展名命中表    → magic_score = 100

    组合规则：
    若 score>0 且 magic_score>0 : score = 100 + score   // 双重确认，最强
    否则若 magic_score>0        : score = 1             // 仅名字命中，弱信任
    （否则 score 保持内容分，可能为 0）

    记录最高 score 的 handler 为 best
```

组合规则的设计意图很巧妙：

- **双重确认（100+score）最强**：名字和内容都对得上，几乎不会错。
- **仅名字命中（score=1）很弱**：扩展名说它是 PDF，但内容不像——这种「弱信任」只能压过「完全零分」的 handler，**压不过**任何真正能解析内容的 handler。这就防止了「文件改错扩展名导致误打开」。
- **仅内容命中**：保留 `recognize_content` 返回的 0~100 原始分，通常足以当选。

最后如果所有 handler 的最高分仍是 0（`best_i < 0`），返回 `NULL`，调用方据此抛出 `cannot find document handler` 异常。

#### 4.3.3 源码精读

先看 PDF 的内容识别函数，它就是经典的「在文件头扫魔数」：

[pdf-xref.c:3799-3836](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3799-L3836) — `pdf_recognize_doc_content`：在文件前 4096 字节内寻找 `%PDF-` 或 `%FDF-` 签名，找到返回 100，否则返回 0。

几个值得注意的点：

- 容许签名不在第 0 字节：循环逐字节推进，`pos` 记录已匹配长度，匹配失败时用 `pos = (c == match[0])` 重置但不丢弃当前字节（标准字符串匹配的状态机写法）。
- 只扫前 `4096+5` 字节就放弃，避免对大文件无谓读取。
- `stream == NULL` 时直接返回 0——纯目录型文档（如解压后的 EPUB 目录）PDF 不认。

现在看打分主循环。这是本讲最核心的一段代码，请结合 4.3.2 的流程图逐段读：

[document.c:279-346](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L279-L346) — 打分循环主体：对每个 handler 先算内容分，再算 magic 分，按组合规则合并，记录最佳。

其中内容分计算（注意对 EPUB/XPS/DOCX 解 zip 失败时的容错）：

[document.c:288-303](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L288-L303) — 调 `recognize_content` 取内容分，捕获 `FZ_ERROR_FORMAT`（如 zip 损坏）并降级为 0 分。

magic 分计算（三道判断：自定义 recognize → mimetype → 扩展名）：

[document.c:305-324](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L305-L324) — magic 分：先调 handler 自定义 `recognize`，再匹配 mimetype 表，最后匹配扩展名表，命中任一即置 100。

组合规则（双重确认 vs 弱信任）：

[document.c:326-334](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L326-L334) — 组合得分：`score>0 && magic_score>0` 给 `100+score`；仅 `magic_score>0` 给 `1`。

如果最终没有 handler 得分大于 0，返回 `NULL`：

[document.c:359-364](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L359-L364) — `best_i < 0` 时返回 NULL，调用方据此报「找不到 handler」。

再上看一层，`fz_open_accelerated_document_with_stream_and_dir` 把识别和打开串起来：识别失败就抛 `FZ_ERROR_UNSUPPORTED`，识别成功就调赢家的 `open`：

[document.c:438-470](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L438-L470) — 识别 → open 的串联：`handler->open(...)` 前先判 NULL 抛异常。

而最常用的 `fz_open_document(ctx, filename)` 走的是文件路径版，它先把文件名打开成 stream，再进入同一条识别链：

[document.c:504-567](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L504-L567) — `fz_open_accelerated_document`：按文件名打开 stream（或目录），识别后调 open。

这里有个**「要不要落地成磁盘文件」**的细节：如果任意一个已注册 handler 的 `wants_file` 为真，而传入的是纯内存流，MuPDF 会先把流写到临时文件再识别，以满足这类 handler 的要求：

[document.c:255-273](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L255-L273) — 若有 handler 需要 file，则用 `fz_file_backed_stream` 把内存流落地成临时文件。

最后，区分两条公开的识别入口：

- `fz_recognize_document(ctx, magic)` —— **只看名字**，不读文件内容（stream 传 NULL），适合「我只想知道这个扩展名归谁管」：

[document.c:428-432](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L428-L432) — `fz_recognize_document`：stream 与 dir 都传 NULL 的纯 magic 识别。

- `fz_recognize_document_content(ctx, filename)` —— **打开文件读内容**做完整识别（[document.c:423-426](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L423-L426)）。

`fz_open_document` 实际走的是后者（内容识别），所以即便扩展名错了，只要内容签名在，仍能正确打开。

#### 4.3.4 代码实践

**实践目标**：编写一个只注册 PDF handler 的程序，用一个 XPS 文件去试 `fz_open_document`，观察识别失败的行为（这正是讲义规格指定的实践任务）。

**操作步骤**（示例代码，基于 u1-l5 跑通过的最小渲染骨架改写）：

```c
/* 示例代码：只注册 PDF，然后用它打开一个 XPS 文件 */
#include "mupdf/fitz.h"

/* PDF handler 的定义在 pdf-xref.c，这里只需 extern 声明 */
extern fz_document_handler pdf_document_handler;

int main(int argc, char **argv)
{
    fz_context *ctx;
    fz_document *doc = NULL;

    ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    if (!ctx) { fprintf(stderr, "ctx failed\n"); return 1; }

    /* 关键：只注册 PDF 一种 handler，不调用 fz_register_document_handlers */
    fz_register_document_handler(ctx, &pdf_document_handler);

    fz_var(doc);
    fz_try(ctx)
    {
        /* argv[1] 传一个 .xps 文件路径 */
        doc = fz_open_document(ctx, argv[1]);
        printf("pages = %d\n", fz_count_pages(ctx, doc));
    }
    fz_catch(ctx)
    {
        fz_report_error(ctx);
        printf("打开失败：识别不到能处理该文件的 handler\n");
    }

    fz_drop_document(ctx, doc);
    fz_drop_context(ctx);
    return 0;
}
```

编译（参考 u1-l2 的构建方式，链上 `libmupdf` 与 `libmupdf-third`）后运行：

```bash
./myprog some_document.xps
```

**需要观察的现象**：

- 即便文件扩展名是 `.xps`，由于只注册了 PDF handler，打分循环里只有 PDF 一家参与。
- PDF 的内容识别 `pdf_recognize_doc_content` 在 XPS 文件里找不到 `%PDF-`，返回 0；magic 分里 `.xps` 不在 PDF 的扩展名表（`pdf/fdf/pclm/ai`）也不在 mimetype 表，也是 0。
- 最终 `best_i < 0`，`fz_open_document` 抛出 `FZ_ERROR_UNSUPPORTED: cannot find document handler for file: ...`，被你的 `fz_catch` 捕获并打印。

**预期结果**：程序输出错误信息并打印「打开失败」，不会崩溃。这验证了「识别失败是可恢复的异常」而非进程退出。

> 待本地验证：上述命令的实际错误文本措辞以本地编译运行的输出为准（不同版本措辞可能略有差异）。

进一步实验：把上面 `fz_register_document_handler(ctx, &pdf_document_handler)` 这一行换成 `fz_register_document_handlers(ctx)`（全注册），再用同一个 XPS 文件运行，应当能成功打开并打印页数——对比两种结果，体会注册表内容对识别结果的决定性影响。

#### 4.3.5 小练习与答案

**练习 1**：一个文件名是 `report.pdf`，但内容其实是一个 EPUB（zip 包）。在「全注册」的 context 下，MuPDF 会用哪种 handler 打开它？为什么？

**参考答案**：会用 EPUB handler 打开。因为 EPUB 的 `recognize_content` 识别出 zip 内容得正值 `score>0`，而 `.pdf` 扩展名让 PDF 的 `magic_score=100`、但 PDF 的内容识别在 zip 里找不到 `%PDF-`，`score=0`。根据组合规则，PDF 的最终分只有 `magic_score` 带来的弱信任分 `1`，而 EPUB 的内容分（≤100）远高于 1，所以 EPUB 当选。这正是「内容压过名字」的设计意图。

**练习 2**：为什么 `gz_document_handler` 在 `document-all.c` 里没有任何 `#if FZ_ENABLE_*` 包裹、始终注册？

**参考答案**：gz 处理的是「外层 gzip 压缩」这一层包装，理论上任何格式（PDF/XPS/EPUB…）都可能被 gzip 压一层。把它单独作为一种 handler 注册后，识别到 gz 会先解压，再把内部流交回识别链做二次识别。它不属于某一具体格式，因此不受任何格式开关控制，始终存在。

**练习 3**：`fz_recognize_document(ctx, "application/pdf")` 与 `fz_recognize_document(ctx, "x.pdf")` 各自依据什么命中 PDF handler？

**参考答案**：前者走 mimetype 表匹配——`"application/pdf"` 命中 `pdf_mimetypes`，`magic_score=100`；后者先由 `strrchr(magic, '.')` 取出扩展名 `pdf`，再命中 `pdf_extensions`，同样 `magic_score=100`。两者都不读文件内容，是纯名字识别。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**handler 侦探**」小任务：

1. **读注册表**：在 `source/fitz/document-all.c` 中数出当前构建实际注册了哪些 handler（结合各 `#if FZ_ENABLE_*`）。如果你本地用的是默认全功能构建，应当得到 14 项左右。
2. **画打分表**：仿照 4.3.2，手画一张表格，列出 PDF、IMG、GZ 三种 handler 对以下三种输入各自的 `score`（内容分）和 `magic_score`（名字分）以及最终组合分：
   - 输入 A：`photo.jpg`（真实 JPEG）
   - 输入 B：`doc.pdf`（真实 PDF）
   - 输入 C：`doc.pdf.gz`（被 gzip 压过的 PDF）
   对每种输入，预测哪个 handler 当选。
3. **用代码验证**：写一个小程序，全注册 handler 后，对上面三种文件分别调用 `fz_recognize_document_content(ctx, filename)`，打印返回的 handler 指针（可对比 `&pdf_document_handler` 等地址判断是哪一种），核对你的预测。

> 提示：对于输入 C，注意 gz handler 会先当选、解压后内部流会再走一次识别链最终落到 PDF——这是「分层识别」。若一次调用看不到中间过程，可结合 `pdf_recognize_doc_content` 与 gz 的内容识别函数源码推理。

> 待本地验证：步骤 3 中 handler 地址比较的具体写法与打印格式，请以本地编译运行结果为准。

---

## 6. 本讲小结

- `fz_register_document_handlers` 是「一键全注册」便捷函数，内部逐个调用原语 `fz_register_document_handler`，每个都被 `#if FZ_ENABLE_*` 条件编译保护；`gz` 因处理外层压缩而始终注册。
- 注册表本质是 `ctx->handler->handler[]` 指针数组（上限 32），注册时按指针去重。
- 每种格式通过 `struct fz_document_handler` 这张「登记表」接入：核心是 `recognize`（按名字）、`recognize_content`（按内容）、`open`（打开）三组回调，外加扩展名/mimetype 表与 `wants_dir`/`wants_file` 开关。
- PDF handler 用位置初始化填表：`recognize=NULL`、`open=open_document`、扩展名 `pdf/fdf/pclm/ai`、内容识别扫 `%PDF-`/`%FDF-` 魔数。
- 识别走两条打分路径：内容分 `score` 与名字分 `magic_score`；两者都正给 `100+score`（最强），仅名字命中给 `1`（弱信任，压不过任何内容命中），全 0 则返回 NULL 抛 `FZ_ERROR_UNSUPPORTED`。
- `fz_open_document` 实际走「内容识别」（先打开文件成 stream），所以扩展名错了也能靠魔数纠正；`fz_recognize_document` 则是纯名字识别。

---

## 7. 下一步学习建议

本讲解决了「文件 → handler」的识别问题，handler 选定后下一步就是「几何」。建议：

- 继续学习 **u3-l3「坐标、矩阵与页面几何」**：在打开文档之后、真正渲染之前，理解 `fz_matrix` 仿射变换如何把 72 dpi 的页面坐标映射到设备像素，这是渲染链路的数学基础。
- 若你对「handler 如何驱动 device」更感兴趣，可以先跳到 **u4-l1「fz_device：显示设备抽象」**，看 `handler->open` 产出的 `fz_document` 之后，内容是如何被 `fz_run_page` 翻译成绘图指令的。
- 想了解密码与元数据读取，可看 **u3-l4「密码、加密与文档元数据」**，它承接本讲的 `fz_open_document`，讲解打开后的鉴权与元数据查询。
- 若你已迫不及待想做扩展开发，**u10-l3「扩展：新增格式与输出 handler」** 会把本讲的「填表 + 注册」模式总结成新增输入格式的完整步骤。
