# 文档处理器与格式识别

## 1. 本讲目标

在上一讲（u3-l1）里，我们已经看到 `fz_open_document` 能用一个统一接口打开 PDF、EPUB、XPS 等截然不同的格式。但「它凭什么知道这个文件是 PDF、那个是 XPS？」这个问题我们刻意回避了。本讲就来回答它。

学完本讲，你应当能够：

- 说出 `fz_register_document_handlers` 到底注册了哪些格式处理器，以及它们是怎么被挂进 `fz_context` 的。
- 读懂 `struct fz_document_handler` 这张「应聘登记表」上的每一个字段（`recognize` / `open` / `extensions` / `mimetypes` / `recognize_content` / `wants_dir` / `wants_file` / `fin`）。
- 区分两条识别路径：**按扩展名/mime（magic）识别** 与 **按文件内容（content）嗅探**，并理解它们在打分系统里是如何叠加与互相竞争的。
- 动手验证：当你只注册 PDF 处理器、却喂给它一个 XPS 文件时，会发生什么。

## 2. 前置知识

阅读本讲前，请确保你已经掌握 u3-l1 的结论：

- `fz_document` / `fz_page` 是格式无关的抽象基类，靠「函数指针表（虚表）+ 派生结构体」实现手写多态。
- `fz_open_document(ctx, filename)` 是打开文档的总入口，它返回一个 `fz_document *`。

此外需要一点点 C 基础：

- **函数指针（function pointer）**：把一个函数的地址存进结构体字段，之后通过该字段「间接调用」它。这正是 MuPDF 实现多态的机制。
- **位置初始化（positional initialization）**：`struct Foo x = { a, b, c };` 按字段声明顺序依次赋值，省略的尾部字段会被置零。你会看到大量 handler 用这种方式构造。

几个本讲用到的术语：

- **magic**：在 MuPDF 里，magic 不是「魔数（文件头字节）」的意思，而是指**一个提示字符串**——通常是文件名（含扩展名，如 `report.pdf`）或 mime 类型（如 `application/pdf`）。代码注释里写得明白：`magic: Can be a filename extension (including initial period) or a mimetype.`（见 [include/mupdf/fitz/document.h:447-454](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L447-L454)）。
- **content（内容嗅探）**：真正打开文件、读取前若干字节，根据文件头判断类型（如 PDF 文件都以 `%PDF-` 开头）。
- **handler（处理器）**：一种格式对应的「适配器」，告诉通用层「我能识别这种格式、我能打开它」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [source/fitz/document-all.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c) | 处理器**注册表**：`fz_register_document_handlers` 在此把各格式 handler 逐个登记进 context。 |
| [include/mupdf/fitz/document.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h) | 定义 `struct fz_document_handler`，以及 `fz_register_document_handler`、`fz_recognize_document` 等公共 API。 |
| [source/fitz/document.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c) | 注册与识别的**实现**：打分算法 `do_recognize_document_stream_and_dir_content`、`fz_open_document` 的识别入口都在这里。 |
| [source/pdf/pdf-xref.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c) | PDF 处理器 `pdf_document_handler` 的定义，以及它如何嗅探 `%PDF-` 文件头。 |
| [source/html/epub-doc.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/epub-doc.c) | EPUB 处理器，是全代码库中**唯一**填了 `recognize`（magic 字符串识别）回调的 handler，便于对比两种识别路径。 |

---

## 4. 核心概念与源码讲解

### 4.1 处理器注册表：fz_register_document_handlers

#### 4.1.1 概念说明

每种格式在 MuPDF 里都有一个全局的 `fz_document_handler` 变量（如 `pdf_document_handler`、`xps_document_handler`）。但「存在」不等于「可用」——必须先把它们**登记**进 `fz_context`，通用层才知道有这些格式。这个登记动作的入口就是 `fz_register_document_handlers`。

可以把它理解成「开学注册」：每个格式（学生）本来在各自的源文件里定义好了（在家），但只有去教务处（context）报到、把名字写进名册，老师（`fz_open_document`）点名时才会叫到你。

#### 4.1.2 核心流程

注册流程非常直白：

1. `fz_new_context` 时，context 内部已经准备好一个空的 handler 容器（一个指针数组，见后文 4.1.3）。
2. 应用调用 `fz_register_document_handlers(ctx)`。
3. 该函数内部**逐个**调用 `fz_register_document_handler(ctx, &xxx_document_handler)`，把每种格式的处理器挂进去。
4. 之后 `fz_open_document` 在识别阶段，会遍历这个数组，给每个 handler 打分。

注意每条注册语句都被 `#if FZ_ENABLE_XXX` 包裹——这是 u1-l2 讲过的**编译期裁剪**：在 `Makerules` 里把 `pdf=no` 翻译成 `-DFZ_ENABLE_PDF=0`，对应的注册语句就从编译产物里消失。所以「实际注册了哪些格式」取决于你这版库是怎么编译的。

#### 4.1.3 源码精读

注册表的全部内容就在 [source/fitz/document-all.c:40-80](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L40-L80)：

```c
void fz_register_document_handlers(fz_context *ctx)
{
#if FZ_ENABLE_PDF
    fz_register_document_handler(ctx, &pdf_document_handler);
#endif /* FZ_ENABLE_PDF */
#if FZ_ENABLE_XPS
    fz_register_document_handler(ctx, &xps_document_handler);
#endif /* FZ_ENABLE_XPS */
    /* ... svg / cbz / img / fb2 ... */
#if FZ_ENABLE_HTML
    fz_register_document_handler(ctx, &html_document_handler);
    fz_register_document_handler(ctx, &xhtml_document_handler);
#if FZ_ENABLE_MD
    fz_register_document_handler(ctx, &md_document_handler);
#endif /* FZ_ENABLE_MD */
#endif /* FZ_ENABLE_HTML */
    /* ... mobi / txt / office / epub ... */
    fz_register_document_handler(ctx, &gz_document_handler);
}
```

几个关键观察：

- 文件顶部 [source/fitz/document-all.c:25-38](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L25-L38) 用 `extern` 声明了全部 14 个 handler 变量。这些变量的「本体」分散在各自格式的源文件里（PDF 在 `pdf-xref.c`、EPUB 在 `epub-doc.c`……）。
- PDF 的注册带 `FZ_ENABLE_PDF` 守卫（[第 42-44 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L42-L44)）。
- `md_document_handler`（Markdown）被**嵌套**在 `FZ_ENABLE_HTML` 里（[第 60-66 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L60-L66)）——因为 Markdown 复用 HTML 排版引擎，没有 HTML 就谈不上 MD。
- 唯独 `gz_document_handler`（[第 79 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L79)）**没有任何 `FZ_ENABLE_` 守卫**，永远注册。原因在 u1-l1 已说明：gz 处理的是外层 gzip 压缩，剥开之后还要再交给真正的格式 handler，属于基础设施。

那么 `fz_register_document_handler` 本身做了什么？看 [source/fitz/document.c:194-214](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L194-L214)：

```c
void fz_register_document_handler(fz_context *ctx, const fz_document_handler *handler)
{
    fz_document_handler_context *dc;
    int i;
    if (!handler) return;
    dc = ctx->handler;
    if (dc == NULL)
        fz_throw(ctx, FZ_ERROR_ARGUMENT, "Document handler list not found");
    for (i = 0; i < dc->count; i++)
        if (dc->handler[i] == handler)   /* 去重：同一个 handler 注册两次只算一次 */
            return;
    if (dc->count >= FZ_DOCUMENT_HANDLER_MAX)
        fz_throw(ctx, FZ_ERROR_LIMIT, "Too many document handlers");
    dc->handler[dc->count++] = handler;  /* 追加到数组末尾 */
}
```

它把 handler 指针追加进 `ctx->handler->handler[]` 数组。这个容器定义在 [source/fitz/document.c:149-154](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L149-L154)，是一个固定容量的指针数组：

```c
enum { FZ_DOCUMENT_HANDLER_MAX = 32 };  /* document.c:35-38 */

struct fz_document_handler_context
{
    int refs;
    int count;
    const fz_document_handler *handler[FZ_DOCUMENT_HANDLER_MAX];
};
```

两个细节值得记住：

- **按指针去重**：用 `dc->handler[i] == handler` 判断，所以即使你把同一个 `&pdf_document_handler` 注册两遍，数组里也只会有一个 PDF 项。
- **上限 32**：内置格式最多 14 个，离上限很远，这是为第三方扩展（u10-l3）留的余量。

#### 4.1.4 代码实践

**实践目标**：搞清楚「你这版库里实际注册了哪些 handler」，以及注册顺序。

**操作步骤**：

1. 打开 [source/fitz/document-all.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c)，从上到下数 `fz_register_document_handler` 调用的条数，记下顺序。
2. 打开 [include/mupdf/fitz/config.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h)（如存在），查看 `FZ_ENABLE_*` 的默认值。
3. 对照下表填写（示例答案已给出，前提是所有功能默认开启）：

| 顺序 | handler 变量 | 格式 | 守卫宏 |
| --- | --- | --- | --- |
| 1 | `pdf_document_handler` | PDF | `FZ_ENABLE_PDF` |
| 2 | `xps_document_handler` | XPS | `FZ_ENABLE_XPS` |
| 3 | `svg_document_handler` | SVG | `FZ_ENABLE_SVG` |
| 4 | `cbz_document_handler` | 漫画压缩包 | `FZ_ENABLE_CBZ` |
| 5 | `img_document_handler` | 单张图片 | `FZ_ENABLE_IMG` |
| … | … | … | … |
| 末 | `gz_document_handler` | gzip 外层 | 无（始终注册） |

**需要观察的现象**：注册顺序就是数组里的存放顺序，也是后面打分遍历的顺序（`for (i = 0; i < dc->count; i++)`）。

**预期结果**：在所有功能开启时，共注册 **14 个** handler（与 u1-l1 给出的格式清单一致）；`gz` 永远在列。

#### 4.1.5 小练习与答案

**练习 1**：如果把 MuPDF 编译成 `make build=release html=no`，`md_document_handler` 还会被注册吗？

> **答案**：不会。MD 嵌套在 `FZ_ENABLE_HTML` 块内（[document-all.c:60-66](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L60-L66)），`html=no` 同时抹掉了 HTML 与 MD。

**练习 2**：为什么 `gz_document_handler` 不需要 `FZ_ENABLE_GZ` 守卫？

> **答案**：gz 处理的是外层 gzip 压缩，解压后还要把内层交给真正的格式 handler，属于「跨格式」的基础能力，所以始终注册。

---

### 4.2 handler 回调集合：struct fz_document_handler

#### 4.2.1 概念说明

每种格式要被 MuPDF 接纳，必须填好一张「应聘登记表」——也就是 `struct fz_document_handler`。这张表告诉通用层两件事：

1. **我能识别这种格式吗？**（`recognize` / `extensions` / `mimetypes` / `recognize_content`）
2. **给我一个文件流，我能把它打开成 `fz_document` 吗？**（`open`）

外加两个能力声明：`wants_dir`（打开时是否需要「目录上下文」，比如 HTML 要能加载同目录的图片）、`wants_file`（是否必须是真实文件、不能是内存流），以及一个收尾钩子 `fin`。

#### 4.2.2 核心流程

handler 的工作分两个阶段，对应识别与打开：

```text
阶段 A：识别（recognize）——只读不打开，回答「这是不是我的格式？」
    ├── 路径 1：magic 字符串识别
    │     ├── recognize 回调（自定义打分，0~任意）
    │     ├── mimetypes 数组命中  → 100 分
    │     └── extensions 数组命中 → 100 分
    └── 路径 2：content 内容嗅探
          └── recognize_content 回调（读文件头字节，0~100 分）

阶段 B：打开（open）——确认是我的格式后，真正构造 fz_document
    └── open 回调：把 stream/accel/dir 交给格式专用层
```

通用层在识别阶段会把所有 handler 都问一遍、各自打分，最后挑分数最高那个进入阶段 B（详见 4.3）。

#### 4.2.3 源码精读

登记表的结构定义在 [include/mupdf/fitz/document.h:1127-1138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1127-L1138)：

```c
struct fz_document_handler
{
    /* These fields are initialised by the handler when it is registered. */
    fz_document_recognize_fn *recognize;          /* magic 字符串识别 */
    fz_document_open_fn *open;                    /* 真正打开文档 */
    const char **extensions;                       /* 支持的扩展名表，如 {"pdf","fdf",NULL} */
    const char **mimetypes;                        /* 支持的 mime 表，如 {"application/pdf",NULL} */
    fz_document_recognize_content_fn *recognize_content; /* 内容嗅探 */
    int wants_dir;                                 /* 打开时是否需要目录上下文 */
    int wants_file;                                /* 是否必须是真实文件 */
    fz_document_handler_fin_fn *fin;               /* 关闭时的收尾钩子 */
};
```

逐个看几个关键回调的签名：

- **`open`**（[document.h:349-370](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L349-L370)）：接收 `stream`（文档数据流）、`accel`（加速数据，可空）、`dir`（目录上下文）、`recognize_state`（识别阶段留下的状态），返回 `fz_document *`。失败时抛异常。
- **`recognize`**（[document.h:372-385](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L372-L385)）：只看 `magic` 字符串，返回 0（不认识）到 100（完全确定）的整数。
- **`recognize_content`**（[document.h:389-412](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L389-L412)）：真正读 `stream` 的字节，同样返回 0~100，还能通过 `recognize_state` 把识别时算出的中间结果直接传给 `open`，避免重复计算。

来看 PDF 这张登记表的真实填写（[source/pdf/pdf-xref.c:3846-3853](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3846-L3853)）：

```c
fz_document_handler pdf_document_handler =
{
    NULL,                      /* recognize：PDF 不用 magic 字符串识别 */
    open_document,             /* open */
    pdf_extensions,            /* extensions */
    pdf_mimetypes,             /* mimetypes */
    pdf_recognize_doc_content  /* recognize_content：嗅探 %PDF- */
};
```

PDF 的 `recognize` 字段是 `NULL`——它**只**靠扩展名/mime 表 + 内容嗅探来识别，不提供自定义的 magic 字符串打分。它的扩展名和 mime 表在 [pdf-xref.c:3783-3797](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3783-L3797)：

```c
static const char *pdf_extensions[] = { "pdf", "fdf", "pclm", "ai", NULL };
static const char *pdf_mimetypes[] = { "application/pdf", "application/PCLm", NULL };
```

注意位置初始化的妙处：PDF 这张表只填了 5 个字段，`wants_dir` / `wants_file` / `fin` 没写，C 会自动把它们补 0/NULL。对比之下，HTML handler 多填了一个 `1`（[source/html/html-doc.c:532-540](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/html-doc.c#L532-L540)）：

```c
fz_document_handler html_document_handler =
{
    NULL,
    htdoc_open_document,
    htdoc_extensions,
    htdoc_mimetypes,
    htdoc_recognize_html_content,
    1              /* ← 第 6 个字段 wants_dir=1：HTML 需要目录上下文来加载图片等 */
};
```

PDF 的 `open_document` 极其简短（[pdf-xref.c:3838-3844](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3838-L3844)），它把活儿转交给 PDF 专用层的 `pdf_open_document_with_stream`：

```c
static fz_document *
open_document(fz_context *ctx, const fz_document_handler *handler,
              fz_stream *file, fz_stream *accel, fz_archive *zip, void *state)
{
    if (file == NULL)
        return NULL;
    return (fz_document *)pdf_open_document_with_stream(ctx, file);
}
```

而 PDF 的内容嗅探 `pdf_recognize_doc_content`（[pdf-xref.c:3799-3836](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3799-L3836)）逻辑很朴素：在前 4096+5 字节里扫描 `%PDF-` 或 `%FDF-`，命中就返回 100：

```c
static int
pdf_recognize_doc_content(fz_context *ctx, const fz_document_handler *handler,
                          fz_stream *stream, fz_archive *dir,
                          void **state, fz_document_recognize_state_free_fn **free_state)
{
    const char *match = "%PDF-";
    const char *match2 = "%FDF-";
    int pos = 0, n = 4096+5, c;
    /* ... */
    do {
        c = fz_read_byte(ctx, stream);
        if (c == EOF) return 0;
        if (c == match[pos] || c == match2[pos]) {
            pos++;
            if (pos == 5) return 100;   /* 连续命中 5 个字符 = 确定是 PDF/FDF */
        } else {
            pos = (c == match[0]);      /* 失配后回退，但重新检查当前字节 */
        }
    } while (--n > 0);
    return 0;
}
```

> 这里用了一个朴素的字符串匹配（允许 magic 不在文件最开头，因为 PDF 规范允许文件头前面有少量垃圾）。`pos==5` 时返回满分 100，表示「内容上确定」。

作为对比，全代码库**只有 EPUB** 填了 `recognize` 字段（[source/html/epub-doc.c:1275-1282](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/epub-doc.c#L1275-L1282)）。它的 magic 识别函数会检查 magic 串里是否含 `META-INF/container.xml`（EPUB 的标志性内部路径），命中返回 200（[epub-doc.c:1218-1224](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/epub-doc.c#L1218-L1224)）：

```c
static int
epub_recognize(fz_context *doc, const fz_document_handler *handler, const char *magic)
{
    if (strstr(magic, "META-INF/container.xml") || strstr(magic, "META-INF\\container.xml"))
        return 200;
    return 0;
}
```

这个 200 分的意义在 4.3 节揭晓——它能让 EPUB 在与 HWPX（同样基于 zip + container.xml）竞争时仍被正确选中。

#### 4.2.4 代码实践

**实践目标**：把「登记表字段」与「真实 handler」一一对应起来。

**操作步骤**：

1. 在 [source/pdf/pdf-xref.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c) 中定位 `fz_document_handler pdf_document_handler = { ... };`（约第 3846 行）。
2. 对照 [struct fz_document_handler](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1127-L1138) 的字段顺序，填出 PDF 各字段对应的值。
3. 再找另一个 handler（如 [source/cbz/mucbz.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/cbz/mucbz.c) 末尾的 `cbz_document_handler` 或 [source/html/epub-doc.c:1275](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/epub-doc.c#L1275)），同样填表。

**需要观察的现象**：PDF 的 `recognize` 字段是 `NULL`，而 EPUB 的 `recognize` 字段是 `epub_recognize`——这是两种识别策略的差异。

**预期结果**：PDF 表 = `{ NULL, open_document, pdf_extensions, pdf_mimetypes, pdf_recognize_doc_content }`，字段顺序与 `struct fz_document_handler` 一一对应。

#### 4.2.5 小练习与答案

**练习 1**：PDF 的 `recognize` 字段为 `NULL`，那它靠什么识别 `.pdf` 文件？

> **答案**：靠 `extensions` 表（含 `"pdf"`）和 `recognize_content`（嗅探 `%PDF-` 文件头）。识别阶段会把扩展名命中算作 magic 满分 100，再把内容命中叠加（详见 4.3）。

**练习 2**：为什么 `open_document` 几乎不做事，只调用 `pdf_open_document_with_stream`？

> **答案**：handler 是通用层与格式专用层之间的**薄适配器**。它的职责只是把通用层的 `stream` 等参数转交出去，真正的解析逻辑在 PDF 专用层（`source/pdf/`）。这正是 u1-l1 所讲「双层架构」的体现。

---

### 4.3 magic 与内容识别：打分系统

#### 4.3.1 概念说明

到目前为止我们知道：识别有两个来源——magic（扩展名/mime/`recognize` 回调）和 content（`recognize_content` 嗅探文件头）。问题是，当多个 handler 都「觉得自己能处理」时，听谁的？

MuPDF 的答案是**打分制**：给每个 handler 算一个分数，取最高分。打分规则精心设计，让「内容嗅探」比「仅凭扩展名」更可信——毕竟扩展名可以随便改，文件头不会骗人。这套打分逻辑全部集中在 `do_recognize_document_stream_and_dir_content` 一个函数里。

#### 4.3.2 核心流程

打分函数对每个 handler 计算两个分量（[document.c:279-346](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L279-L346)）：

```text
对每个已注册 handler[i]：
    score       = recognize_content(stream)          # 内容嗅探，0~100
    magic_score = recognize(magic)                    # 自定义 magic 打分，0~任意
                或 mimetypes 命中(magic) → 100
                或 extensions 命中(ext) → 100

    合并规则：
        若 score>0 且 magic_score>0：  score = 100 + score   ← 最高档（双命中）
        否则若 magic_score>0：         score = 1             ← 仅 magic，刻意压低
        （score 仍为 0 且无 magic：    score = 0，不候选）

    若 score > best_score：记下该 handler 为当前最佳
最终返回 best_handler（若无任何 score>0，返回 NULL）
```

三条规则合起来表达了一个清晰的优先级：

- **内容 + magic 双命中**（`100 + score`）最可信，是最高档。
- **仅内容命中**（`score`，0~100）次之——文件头说了算，哪怕扩展名对不上。
- **仅 magic 命中**（`1`）最弱——刻意压成 1 分，是为了**不让一个仅凭扩展名的 handler 抢走真正能解析的 handler 的活儿**。

举个具体场景：你把一个 PDF 改名成 `report.xps`。XPS handler 的扩展名表里有 `"xps"`，所以 magic_score=100；但 XPS 的内容嗅探会发现这根本不是 zip 包，content score=0，于是 XPS 的总分被压成 1。而 PDF handler 虽然扩展名不匹配（magic_score=0），但 `pdf_recognize_doc_content` 嗅探到 `%PDF-`，content score=100，总分 100 > 1，PDF 胜出。**文件头击败了错误的扩展名。**

#### 4.3.3 源码精读

打分函数开头先从 magic 里切出扩展名（[document.c:242-248](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L242-L248)）：

```c
if (magic == NULL)
    magic = "";
ext = strrchr(magic, '.');   /* 找最后一个 '.' */
if (ext)
    ext = ext + 1;            /* 跳过 '.'，得到 "pdf" */
else
    ext = magic;              /* 没有 '.' 就拿整个 magic 当 ext */
```

接着是 content 分量（[document.c:288-303](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L288-L303)）：只有当 handler 提供了 `recognize_content`、且流可定位（`stream->seek != NULL`）时才嗅探；嗅探前先 `fz_seek` 回到文件头，确保每个 handler 都从头读起。

然后是 magic 分量（[document.c:306-324](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L306-L324)），三个来源取最大：

```c
if (dc->handler[i]->recognize)
    magic_score = dc->handler[i]->recognize(ctx, dc->handler[i], magic);

for (entry = &dc->handler[i]->mimetypes[0]; *entry; entry++)
    if (!fz_strcasecmp(magic, *entry)) { magic_score = 100; break; }

if (ext)
    for (entry = &dc->handler[i]->extensions[0]; *entry; entry++)
        if (!fz_strcasecmp(ext, *entry)) { magic_score = 100; break; }
```

最关键的合并规则在 [document.c:326-334](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L326-L334)，注释把意图说得很清楚：

```c
/* 若内容识别(至少部分)且 magic 也匹配，那肯定是它。用 100+score，
 * 以便在多个 handler 都支持同一 magic 时，让更擅长的一个胜出。*/
if (score > 0 && magic_score > 0)
    score = 100 + score;
/* 否则，若内容没识别出来，我们只弱相信 magic，但不让它盖过真正能处理的 handler。*/
else if (magic_score > 0)
    score = 1;
```

遍历结束后，如果没有 handler 拿到正分，就返回 `NULL`（[document.c:359-364](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L359-L364)）。

现在能解释 EPUB 的 `recognize` 为何返回 200 了：EPUB 与 HWPX 都是 zip 包、都含 `META-INF/container.xml`，光靠内容很难区分。EPUB 的 `recognize_content` 给 EPUB 打 74 分（[epub-doc.c:1253](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/epub-doc.c#L1253) 注释「比 HWPX 的 75 分少 1」），而当 magic 串里出现 `META-INF/container.xml`（即调用方明确指定）时，`epub_recognize` 给 200 分。一旦 magic 与内容双命中，`100 + score` 让 EPUB 牢牢压过 HWPX。

**两条公共入口**对应两个识别侧重点：

- `fz_recognize_document(ctx, magic)`（[document.h:447-454](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L447-L454)，实现 [document.c:428-432](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L428-L432)）：**只看 magic**，不读文件（传 `stream=NULL`），所以只有 magic 分量参与。
- `fz_recognize_document_content(ctx, filename)`（[document.h:456-463](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L456-L463)，实现 [document.c:395-426](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L395-L426)）：**打开文件做内容嗅探**，magic 与 content 都参与。

而 `fz_open_document`（我们最常用的入口）走的是**内容路径**。它最终调用 `do_recognize_document_content`（[document.c:534-536](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L534-L536)），如果识别失败（返回 NULL），就抛出一个明确的异常：

```c
handler = do_recognize_document_content(ctx, filename, &state, &free_state);
if (!handler)
    fz_throw(ctx, FZ_ERROR_UNSUPPORTED, "cannot find document handler for file: %s", filename);
```

这条 `FZ_ERROR_UNSUPPORTED` 分支正是本讲综合实践要触发的。

#### 4.3.4 代码实践

**实践目标**：用一个小程序，故意制造「识别失败」，亲眼看到打分系统的「否决」结果。

**操作步骤**：

1. 准备一个 XPS 文件（任选一个 `.xps` 或 `.oxps`；若手头没有，可从测试样本或网上取一个，或跳到「源码阅读型」替代任务）。
2. 编写下面的示例程序 `only_pdf.c`（**示例代码**，非项目原有文件）：

```c
/* 示例代码：只注册 PDF 处理器，然后用它去开一个 XPS */
#include "mupdf/fitz.h"

/* pdf_document_handler 没有出现在公共头里，需要自行 extern 声明，
 * 这正是 document-all.c / document.c 内部的做法。*/
extern fz_document_handler pdf_document_handler;

int main(int argc, char **argv)
{
    fz_context *ctx;
    fz_document *doc = NULL;

    if (argc < 2) { fprintf(stderr, "usage: %s <file>\n", argv[0]); return 1; }

    ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    if (!ctx) { fprintf(stderr, "cannot create context\n"); return 1; }

    /* 关键：只注册 PDF，不调用 fz_register_document_handlers */
    fz_register_document_handler(ctx, &pdf_document_handler);

    fz_var(doc);
    fz_try(ctx)
    {
        doc = fz_open_document(ctx, argv[1]);   /* 传入 .xps 文件 */
        printf("pages = %d\n", fz_count_pages(ctx, doc));
    }
    fz_always(ctx)
        fz_drop_document(ctx, doc);
    fz_catch(ctx)
    {
        fz_report_error(ctx);   /* 预期打印：cannot find document handler for file: xxx.xps */
    }

    fz_drop_context(ctx);
    return 0;
}
```

3. 编译并链接 mupdf（参照 u1-l2 的产物路径，大致形如）：

```bash
cc only_pdf.c -Iinclude -Lbuild/release -lmupdf -lmupdf-third -o only_pdf
```

4. 运行 `./only_pdf some.xps`。

**需要观察的现象**：程序不会崩溃，而是在 `fz_catch` 里打印出 `cannot find document handler for file: some.xps`（异常码 `FZ_ERROR_UNSUPPORTED`）。

**为什么会这样**（请对照 4.3.2 的打分规则解释）：

- 数组里只有一个 handler（PDF）。遍历到它时：
  - `recognize_content` = `pdf_recognize_doc_content`，读 XPS 的 zip 文件头（`PK\x03\x04`），找不到 `%PDF-`，返回 0 → `score=0`。
  - magic：`recognize` 为 NULL；`mimetypes` 不含 XPS 的 mime；扩展名 `"xps"` 不在 `pdf_extensions` 里 → `magic_score=0`。
- 合并后 `score=0`，不候选；`best_i` 保持 -1，函数返回 NULL。
- `fz_open_document` 见 NULL，抛 `FZ_ERROR_UNSUPPORTED`。

**预期结果**：捕到异常、打印错误后正常退出；若改用一个真正的 `.pdf` 文件，则能正常打印页数——证明「识别失败」纯粹是因为 XPS 不在已注册的 PDF handler 能力范围内。

> 如果本地暂无 XPS 样本或编译环境，可改为**源码阅读型实践**：在 [document.c:534-536](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L534-L536) 设断点或加日志，跟踪 `do_recognize_document_content` 对一个 `.xps` 文件的返回值，确认它返回 NULL 并触发 `fz_throw`。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把一个 PDF 改名成 `a.jpg`，`fz_open_document` 还能打开它吗？为什么？

> **答案**：能。`img` handler 的扩展名表含 `"jpg"`，magic_score=100，但它的 `recognize_content` 嗅探后会发现这不是图片，content score=0，总分被压成 1。而 PDF handler 虽扩展名不匹配，`pdf_recognize_doc_content` 嗅到 `%PDF-` 返回 100，总分 100 > 1，PDF 胜出。

**练习 2**：`fz_recognize_document(ctx, "report.pdf")` 和 `fz_recognize_document_content(ctx, "report.pdf")` 的结果可能不同吗？

> **答案**：可能。前者**只看 magic**（`stream=NULL`），PDF 仅凭扩展名命中得到 magic_score=100、合并后 score=1，返回 PDF handler。后者**还做内容嗅探**，若文件确实是 PDF，content 命中叠加成 `100+score`。两者都可能返回 PDF handler，但「确信度」与背后的打分过程不同；当文件内容与扩展名矛盾时，二者甚至可能返回不同 handler。

**练习 3**：为什么 `recognize_content` 在嗅探前要先 `fz_seek(ctx, stream, 0, SEEK_SET)`？

> **答案**：因为同一个流会被所有 handler 依次嗅探（[document.c:279-348](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L279-L348)）。每个 handler 读完后游标都挪到了不同位置，必须复位到文件头，下一个 handler 才能正确读到文件开头的 magic 字节。遍历结束后还有一次总的复位（[document.c:347-348](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L347-L348)），把流交还给后续的 `open` 调用。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「格式识别小侦探」任务：

1. **阅读注册表**（4.1）：在 [document-all.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c) 里数出你这版库注册了多少个 handler，列出它们的注册顺序。
2. **画登记表**（4.2）：任选 PDF、EPUB、HTML 三个 handler，对照 [struct fz_document_handler](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1127-L1138)，把每个字段填上对应的函数/表，标注哪些字段为空、为什么为空。
3. **追踪打分**（4.3）：写一个程序，正常调用 `fz_register_document_handlers(ctx)` 注册全部 handler，然后：
   - 用 `fz_recognize_document(ctx, "foo.pdf")` 与 `fz_recognize_document_content(ctx, "foo.pdf")` 分别识别同一个真实 PDF，比较二者是否返回同一个 handler（应都是 PDF）。
   - 再准备一个把 PDF 改名为 `foo.unknown` 的副本，用 `fz_open_document` 打开它，验证「内容嗅探击败错误扩展名」——应仍能成功打开。
4. **解释现象**：用 4.3.2 的三条打分规则，书面解释第 3 步两种情形各自的分数构成。

> 提示：第 3 步若需获取 handler 的可读名字，可打印 handler 的 `extensions[0]`（如 `"pdf"`）作为该 handler 的标识——`fz_document_handler` 结构体的字段是公开的（[document.h:1127-1138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1127-L1138)）。

## 6. 本讲小结

- **注册表**在 [document-all.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c)：`fz_register_document_handlers` 把各格式 handler 逐个挂进 `ctx->handler->handler[]` 数组（上限 32、按指针去重），每条注册受 `FZ_ENABLE_*` 编译期守卫，`gz` 始终注册。
- **登记表**是 [struct fz_document_handler](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1127-L1138)：8 个字段中，`recognize`/`extensions`/`mimetypes` 负责 magic 识别，`recognize_content` 负责内容嗅探，`open` 负责真正打开，`wants_dir`/`wants_file`/`fin` 是能力声明与收尾。PDF 只填了 5 个字段、`recognize=NULL`。
- **识别有两条路径**：magic（扩展名/mime/`recognize` 回调）与 content（读文件头的 `recognize_content`）。`fz_recognize_document` 只走 magic，`fz_recognize_document_content` 与 `fz_open_document` 还做内容嗅探。
- **打分系统**（[document.c:326-334](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L326-L334)）有三档：内容+magic 双命中 `100+score` 最高、仅内容命中 `0~100` 次之、仅 magic 命中刻意压成 `1`。结果是**文件头击败错误扩展名**。
- **识别失败**时（无任何 handler 拿到正分），`fz_open_document` 抛 `FZ_ERROR_UNSUPPORTED`：`cannot find document handler for file: ...`（[document.c:534-536](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L534-L536)）。
- **设计精髓**：handler 是通用层与格式专用层之间的薄适配器；识别用「数据驱动的打分遍历」而非 `if(是PDF)` 分支，新增格式只需填一张表 + 注册一行（这正是 u10-l3 扩展实践的根基）。

## 7. 下一步学习建议

- 本讲只讲了「识别与打开」，但 `open` 之后真正解析页面内容的链路还没展开。建议接着读 **u3-l3（坐标、矩阵与页面几何）** 与 **u3-l4（密码、加密与文档元数据）**，把文档打开后的常用操作补齐。
- 想了解 `open` 之后 PDF 专用层如何解析对象，可跳读 [source/pdf/pdf-xref.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c) 中 `pdf_open_document_with_stream` 的实现（u7-l2 会系统讲解 xref）。
- 对「如何新增一种格式」感兴趣的读者，可以把本讲的登记表与注册表记牢，它们是 **u10-l3（扩展：新增格式与输出 handler）** 的直接前置。
