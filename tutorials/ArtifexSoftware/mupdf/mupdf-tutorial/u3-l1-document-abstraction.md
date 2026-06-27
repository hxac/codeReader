# fz_document 与 fz_page 抽象

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `fz_document` 与 `fz_page` 这两个结构体是如何用「函数指针表（虚表）+ 派生结构体」的方式，把 PDF、XPS、EPUB 等十几种格式统一成同一套 API 的。
- 画出从 `fz_open_document` 到最终拿到一个可用 `fz_page` 的完整调用链：识别 handler → `handler->open` → `fz_count_pages` → `fz_load_page` → `fz_bound_page` → `fz_run_page`。
- 解释 `fz_page` 为什么必须持有一个对父文档（`fz_document`）的引用，以及 `fz_drop_page` 为什么在内部会去调用 `fz_drop_document`。
- 区分「定页文档」（如 PDF，页数与边界固定）与「回流文档」（如 EPUB，页数要等排版后才确定），并知道 `fz_count_pages` 内部为什么要先触发 `fz_ensure_layout`。

本讲承接 [u1-l5](u1-l5-first-render.md)（你已经跑通了 example.c 的「创建 context → 注册 handler → 打开文档 → 渲染 pixmap」最小链路）和 [u2-l3](u2-l3-exceptions.md)（你已经理解 `fz_try` / `fz_var` 的异常模型）。本讲把焦点从「渲染一页」前移到「文档与页面这两个抽象本身」，回答：为什么同一行 `fz_count_pages(ctx, doc)` 能同时适用于 PDF 和 EPUB。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，MuPDF 用「C 的手写多态」模拟面向对象。** C 语言没有类、没有继承、没有虚函数。MuPDF 的做法是经典的「结构体首成员 + 函数指针表」：

- 定义一个**基类结构体** `struct fz_document`，它的成员几乎全是函数指针（`count_pages`、`load_page`、`drop_document`……）。
- 每种格式定义自己的派生结构体，例如 PDF 侧写成 `typedef struct { fz_document base; /* PDF 专属字段 */ } pdf_document;`。因为 `base` 是首成员，`pdf_document*` 可以安全地当作 `fz_document*` 使用。
- 派生结构体在创建时，把自己的具体函数填进 `base` 的函数指针表。于是调用 `doc->count_pages(ctx, doc)` 时，实际执行的是该格式自己实现的计数函数。

这套手法在 C 代码库里非常常见（Linux 内核的 `struct file_operations`、GObject 都是类似思路），理解了它，你就理解了 MuPDF 整个「一次编写、多格式通用」的根基。

**第二，「打开」分两步：先认格式，再交给格式专用代码。** `fz_open_document(ctx, "a.epub")` 自己并不认识 EPUB。它先把文件名（扩展名）和文件内容拿去「打分」，让所有已注册的 handler 各自报告「我有多确定这是我能处理的格式」，得分最高的 handler 胜出；然后 MuPDF 调用那个 handler 的 `open` 回调，由它（比如 EPUB 的 handler）真正去构造并返回一个 `fz_document*`。所以 `fz_open_document` 本质是一个**调度器**，真正的解析工作在各格式专用层里。

**第三，页码有两套：面向用户的「线性页号」和内部的「章 + 页」二元位置。** 你平时调用的 `fz_load_page(ctx, doc, 3)` 用的是线性页号（0 起算）。但有些格式（EPUB、FB2 等电子书）天然是「多章节」结构，MuPDF 内部用一个 `fz_location { chapter, page }` 二元组来定位一页。`fz_load_page` 的第一件事就是把线性页号换算成 `fz_location`，再交给 `fz_load_chapter_page`。这是后面「为什么对某些格式按页号加载更慢」的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `include/mupdf/fitz/document.h` | 文档/页面抽象的**全部公开契约**：声明 `fz_open_document` / `fz_count_pages` / `fz_load_page` / `fz_bound_page` / `fz_run_page` 等接口；定义 `struct fz_page`、`struct fz_document`、`struct fz_document_handler` 三个关键结构体，以及一整套函数指针类型（虚表回调签名） |
| `source/fitz/document.c` | 上述接口的**通用实现**：文档/页面的生命周期（keep/drop）、打开与格式识别的调度逻辑、页号↔位置的换算、页面缓存（open 链表），以及 `fz_run_page` 如何把页面内容驱动到 device |
| `include/mupdf/fitz/types.h` | 定义 `fz_location { int chapter; int page; }`，即内部的「章 + 页」定位类型 |
| `include/mupdf/fitz/geometry.h` | 定义 `fz_rect { float x0, y0, x1, y1; }`，即 `fz_bound_page` 返回的页面边界矩形 |
| `docs/examples/example.c` | 官方示例，展示了 `fz_open_document` → `fz_count_pages` → 渲染的标准用法，本讲的实践代码以它为蓝本 |

## 4. 核心概念与源码讲解

### 4.1 fz_document：格式无关的文档接口

#### 4.1.1 概念说明

`fz_document` 是 MuPDF 对「一份文档」的统一抽象。无论底层是 PDF、XPS 还是 EPUB，应用代码拿到的都是一个 `fz_document *doc`，然后用同一套函数（`fz_count_pages`、`fz_load_page`、`fz_lookup_metadata`……）去操作它。

它的实现方式就是上一节说的「函数指针表 + 派生结构体」。`struct fz_document` 的字段几乎全是回调函数指针，每一个指针对应一个「文档级能力」：

- 计数与加载：`count_chapters`、`count_pages`、`load_page`
- 安全：`needs_password`、`authenticate_password`、`has_permission`
- 导航：`resolve_link_dest`、`make_bookmark`、`lookup_bookmark`
- 元数据：`lookup_metadata`、`set_metadata`
- 生命周期：`drop_document`（引用计数归零时析构）
- 排版：`style`、`layout`、`is_reflowable`（仅回流格式需要）

格式专用层（如 `source/pdf/`）在创建文档时把这些指针填成自己的实现，留空的指针表示「本格式不支持该能力」。`document.c` 里每个公开函数（如 `fz_count_pages`）的套路都是：**先做通用预处理，再判断对应回调是否为 NULL，非空就转发给具体格式**。这样，通用层永远不需要 `if (是PDF) … else if (是XPS) …`，而是统一走函数指针。

#### 4.1.2 核心流程

一个格式如何「变成」一个 `fz_document`：

```
派生结构体定义          pdf_document { fz_document base; ...PDF字段... }
        │
        ▼
分配并初始化基类        fz_new_derived_document(ctx, pdf_document)
                        └─ fz_new_document_of_size(ctx, sizeof(pdf_document))
                           └─ refs = 1; 设置默认排版参数
        │
        ▼
填充虚表                pdf_xxx_init(): doc->base.count_pages = pdf_count_pages;
                                   doc->base.load_page   = pdf_load_page;
                                   ...（其余回调）...
        │
        ▼
以基类指针返回          return (fz_document *)doc;
                        ↑ 应用层只看到 fz_document*，多态发生在这里
```

文档的生命周期由引用计数管理（回顾 [u2-l2](u2-l2-memory-refcount.md) 的 keep/drop 铁律）：`fz_new_*` 创建时 `refs=1`；`fz_keep_document` 自增；`fz_drop_document` 自减，归零时调用格式自己的 `drop_document` 析构、再 `fz_free` 自身。注意 `fz_drop_document` 不抛异常，可以安全地放在 `fz_always` / `fz_catch` 里。

#### 4.1.3 源码精读

先看 `struct fz_document` 的全貌——它就是一张函数指针表，加上少量通用状态（排版参数、`is_reflowable` 标志、打开页面链表 `open`）：

[include/mupdf/fitz/document.h:1079-1125](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1079-L1125) —— 定义 `struct fz_document`，前几十行全是回调指针（`drop_document`、`count_pages`、`load_page` 等），末尾 `is_reflowable`、排版参数和 `open` 页面链表是通用层维护的状态。

其中 `load_page` 这个回调的签名是这样的（注意它接收的是「章 + 页」二元位置，而不是线性页号）：

[include/mupdf/fitz/document.h:213-219](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L213-L219) —— `fz_document_count_pages_fn` 与 `fz_document_load_page_fn` 的类型定义，`count_pages` 按「章」计数，`load_page` 按「章 + 页」加载。

创建文档的通用入口是 `fz_new_document_of_size`，派生宏 `fz_new_derived_document` 最终都走到这里：

[source/fitz/document.c:605-624](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L605-L624) —— 分配指定大小的内存（派生结构体的大小）、`refs=1`、赋一个唯一 `id`，并写入默认排版参数（A5 大小）。注意这里**没有**填任何函数指针——那是由各格式的 init 函数随后完成的。

引用计数的 keep/drop 则是 `fz_keep_imp` / `fz_drop_imp` 的薄包装（回顾 [u2-l2](u2-l2-memory-refcount.md)）：

[source/fitz/document.c:626-645](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L626-L645) —— `fz_keep_document` 自增 `refs`；`fz_drop_document` 在 `refs` 归零时回收失效页面、警告「还有页面没关」、调用格式的 `drop_document` 析构、释放 `user_css`、最后 `fz_free(doc)`。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手验证「通用层靠函数指针转发」这一结论。

1. **实践目标**：确认 `document.c` 里的公开函数都是「判空 + 转发」的模式。
2. **操作步骤**：
   - 打开 `source/fitz/document.c`，定位 `fz_needs_password`、`fz_authenticate_password`、`fz_has_permission`、`fz_lookup_metadata` 这几个函数。
   - 观察它们的共同骨架：`if (doc && doc-><回调>) return doc-><回调>(ctx, doc, ...);`，回调为 NULL 时返回一个「安全默认值」（例如 `fz_needs_password` 返回 `0` 表示不需要密码，`fz_has_permission` 返回 `1` 表示默认放行）。
3. **需要观察的现象**：每个函数体都很短，核心只有一行「转发到函数指针」。
4. **预期结果**：你会发现整份 `document.c` 几乎所有公开函数都遵循同一套模板——这就是「基类不写业务，只做调度」的直接证据。
5. 待本地验证：具体返回的安全默认值，请以你本地这份 `document.c` 为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `struct fz_document` 的字段几乎全是函数指针，而几乎没有「数据」？这样做的好处是什么？

> **参考答案**：因为「文档的具体内容」（PDF 的对象树、EPUB 的 HTML、XPS 的 FixedDocument）千差万别，无法用一组通用字段装下。把数据藏在派生结构体里、只通过函数指针暴露行为，通用层就无需知道每种格式的内部表示——新增一种格式时，通用层一行都不用改。这就是「接口与实现分离」。

**练习 2**：如果一个格式没有实现 `lookup_metadata` 回调（指针为 NULL），调用 `fz_lookup_metadata` 会发生什么？

> **参考答案**：不会崩溃。看 [source/fitz/document.c:953-961](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L953-L961)：函数会先给 `buf` 写入终止符，再判断 `doc->lookup_metadata` 是否为 NULL，为 NULL 时直接返回 `-1`（表示「不识别/未找到」），调用方据此知道该格式不支持元数据查询。

---

### 4.2 fz_page：单页的边界与运行入口

#### 4.2.1 概念说明

`fz_page` 是「一页」的抽象，是渲染的最小工作单元。它和 `fz_document` 用的是同一套「函数指针表 + 派生」手法：`struct fz_page` 里同样有一组回调，分别对应「页级能力」：

- `bound_page`：返回这一页的边界矩形 `fz_rect`（72 dpi 下的页面尺寸，单位是 point，1 point = 1/72 inch）。
- `run_page_contents` / `run_page_annots` / `run_page_widgets`：把页面内容 / 标注 / 表单控件「跑」到一个 `fz_device` 上（device 是绘图指令的接收者，详见 [u4-l1](u4-l1-device-model.md)）。
- `load_links`：加载这页上的超链接。
- `drop_page`：引用计数归零时析构这一页。

这里有一个关键设计：**每个 `fz_page` 都持有一个对父文档的引用**（`page->doc` 字段）。因为「运行一页」需要访问文档里的字体、图片、颜色空间等共享资源，页面不能比文档活得更久。所以创建页面时会 `fz_keep_document`，销毁页面时会 `fz_drop_document`——这一对 keep/drop 保证了「只要还有页面存在，文档就不会被回收」。

#### 4.2.2 核心流程

把一页从「加载」到「渲染」再到「释放」串起来：

```
fz_load_page(ctx, doc, n)          用线性页号换算成 (chapter,page)
        │
        ▼
fz_load_chapter_page(...)          查页面缓存(open链表)：命中则 keep 并返回
        │                           未命中则调 doc->load_page 创建新页
        ▼
得到 fz_page *page (refs=1, page->doc 已 keep)
        │
        ├── fz_bound_page(ctx, page)     → 返回 fz_rect（默认取 CropBox）
        │
        ├── fz_run_page(ctx, page, dev, ctm, cookie)
        │       └─ 依次调用 run_page_contents / run_page_annots / run_page_widgets
        │          把页面指令按 ctm 变换后送到 device
        │
        ▼
fz_drop_page(ctx, page)            refs-- ；归零时 drop_page 析构 + fz_drop_document(doc)
```

注意 `fz_run_page` 是个「三合一」便捷函数：它依次运行页面正文内容、标注（annotations）、表单控件（widgets）。如果你只想要正文、不要标注，可以直接用更细粒度的 `fz_run_page_contents`。参数 `ctm` 是变换矩阵（回顾 [u1-l5](u1-l5-first-render.md) 的缩放/旋转），`cookie` 用于多线程下报告进度和中断渲染（单线程传 `NULL`）。

#### 4.2.3 源码精读

先看 `struct fz_page` 的结构——同样是一张函数指针表，外加 `chapter` / `number` 定位信息和一条用于缓存的链表指针：

[include/mupdf/fitz/document.h:1043-1071](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1043-L1071) —— 定义 `struct fz_page`。注意 `doc` 字段注释写明「Guaranteed non-NULL」且是「kept reference」；`prev` / `next` 把当前打开的页面串成链表，由通用层维护。

页面回调的签名，以「取边界」和「运行内容」为例：

[include/mupdf/fitz/document.h:287-294](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L287-L294) —— `fz_page_bound_page_fn` 返回 `fz_rect`、接收一个 `fz_box_type`（MediaBox/CropBox/…）；`fz_page_run_page_fn` 把页面内容按 `transform` 跑到 `dev`，并用 `cookie` 通信。

创建页面时的通用入口，注意它在内部 `fz_keep_document`——这就是「页面持有文档引用」的落点：

[source/fitz/document.c:1149-1156](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1149-L1156) —— `fz_new_page_of_size` 分配页面、`refs=1`，并 `page->doc = fz_keep_document(ctx, doc)`。派生宏 `fz_new_derived_page` 走到这里。

`fz_bound_page` 的默认行为是取 CropBox：

[source/fitz/document.c:1053-1059](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1053-L1059) —— 转发到 `page->bound_page(ctx, page, FZ_CROP_BOX)`，回调为空时返回 `fz_empty_rect`。想取其它盒子可用 `fz_bound_page_box`。

`fz_run_page` 的「三合一」实现，非常简短：

[source/fitz/document.c:1141-1147](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1141-L1147) —— 依次调用 `fz_run_page_contents`、`fz_run_page_annots`、`fz_run_page_widgets`。每个子函数内部都用 `fz_try` / `fz_catch` 包裹，捕获 `FZ_ERROR_ABORT`（用户中断）以外的异常（见 [source/fitz/document.c:1087-1103](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1087-L1103)）。

最后看 `fz_drop_page`——重点在末尾那次 `fz_drop_document`，它与创建时的 `fz_keep_document` 严格配对：

[source/fitz/document.c:1164-1187](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1164-L1187) —— `refs` 归零时：调格式的 `drop_page` 析构、把页面标记为「dead」（`doc=NULL`），再 `fz_drop_document(ctx, doc)`。`in_doc` 为假的页面（从没进过缓存链表）会被立即 `fz_free`。

#### 4.2.4 代码实践

这是一个**半阅读半动手实践**，理解 `fz_bound_page` 返回的矩形含义。

1. **实践目标**：弄清「页面边界矩形」在 PDF 和 EPUB 上的差别。
2. **操作步骤**：
   - 阅读本讲 4.3 的完整示例程序，找到 `r = fz_bound_page(ctx, page);` 一行。
   - 查阅 `include/mupdf/fitz/geometry.h` 里 `fz_rect` 的定义：`{ float x0, y0, x1, y1; }`（[include/mupdf/fitz/geometry.h:230-234](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L230-L234)）。`x1-x0` 是页面宽度、`y1-y0` 是高度，单位 point。
3. **需要观察的现象**：对一份 A4 的 PDF，矩形宽度应约为 595、高度约为 842（A4 = 210mm×297mm ≈ 595×842 point）；对一份用默认 A5 排版的 EPUB，矩形应约为 420×595。
4. **预期结果**：PDF 的边界由文件里的 MediaBox/CropBox 决定（固定）；EPUB 的边界由排版尺寸决定（可变，默认 A5）。
5. 待本地验证：以你本地文档的实际数值为准。

#### 4.2.5 小练习与答案

**练习 1**：如果你 `fz_load_page` 拿到一个 `page` 后，既不 `fz_drop_page` 也不 `fz_drop_document`，会发生什么？如果只 `fz_drop_document` 不 `fz_drop_page` 呢？

> **参考答案**：两者都会内存泄漏。因为创建页面时 `fz_keep_document` 让文档的 `refs` 增加了 1，所以必须先 `fz_drop_page`（它内部会 `fz_drop_document`，把那一次 keep 抵消掉），再 `fz_drop_document`（抵消 `fz_open_document` 时的初始 refs）。只 drop 文档不 drop 页面时，文档的 `refs` 不会归零（页面还持有一次引用），于是文档和页面都泄漏；`fz_drop_document` 里还有一句 `fz_warn(ctx, "There are still open pages in the document!")` 会报警。

**练习 2**：`fz_run_page` 和 `fz_run_page_contents` 有什么区别？什么时候该用后者？

> **参考答案**：`fz_run_page` = `fz_run_page_contents` + `fz_run_page_annots` + `fz_run_page_widgets`，即它额外还画了标注和表单控件。当你只关心页面正文（例如做文本提取、或导出干净的矢量图而不想要批注覆盖）时，用 `fz_run_page_contents` 更合适。

---

### 4.3 打开、计数与加载的统一流程

#### 4.3.1 概念说明

前面两节讲了「文档」和「页面」两个抽象的静态结构。本节把它们串成动态流程，并解释三个容易踩坑的点：**格式识别打分**、**章 + 页的二元模型**、**回流文档的延迟排版**。

**格式识别不是「if-else 判断扩展名」，而是「让所有 handler 打分，取最高分」。** 每个已注册的 `fz_document_handler` 都能对给定的文件名/扩展名/mimetype 和文件内容给出一个 0~100 的「把握度」分数。MuPDF 综合两类分数：扩展名/mimetype 命中给满分基础分，内容识别（读文件头几个字节）给内容分；两者都命中时用 `100 + score` 加权，确保「内容也认可」的 handler 优先于「只靠扩展名猜」的 handler。这就是为什么 `fz_open_document` 能在扩展名缺失或错误时依然猜对格式。

**线性页号 vs 章+页位置。** 你调 `fz_load_page(ctx, doc, n)` 用的是「全文第 n 页」（0 起）。但内部用 `fz_location { chapter, page }` 定位——`chapter` 是第几章、`page` 是该章内的第几页。对单章文档（PDF、XPS）而言 chapter 恒为 0，二者等价；但对多章节电子书（EPUB 每个章节是独立 XHTML，页数要排版后才知道），按线性页号加载需要从头累加每章页数才能定位，效率低于直接给 `fz_load_chapter_page` 传 (chapter, page)。头文件注释也明确写了「This may be much less efficient than loading by location for some document types」。

**回流文档要先排版才能数页。** PDF 的页数写在文件里，打开即知；但 EPUB/HTML 这类「回流（reflowable）」文档，页数取决于你用多大的页面、多大的字号去排版——同一个 EPUB，A5 排版可能是 80 页，A4 排版可能只有 50 页。因此 `fz_count_pages` 内部会先调用 `fz_ensure_layout`，若排版参数有变动（`did_layout == FZ_LAYOUT_NEEDS_UPDATE`）就触发 `doc->layout`，用默认的 A5 尺寸（`FZ_DEFAULT_LAYOUT_W/H/EM`）排一遍，之后才能数出页数。定页文档没有 `layout` 回调，这一步是空操作。

#### 4.3.2 核心流程

完整的「打开 → 计数 → 加载首页」流程，标注了每一步对应的源码位置：

```
fz_open_document(ctx, filename)                 [document.c:569]
  └─ fz_open_accelerated_document               [document.c:504]
       ├─ do_recognize_document_content         按「内容分 + 扩展名分」选出 handler
       └─ handler->open(ctx, handler, stream…)  由胜出的格式真正构造 fz_document
                                                   返回 doc (refs=1)

fz_count_pages(ctx, doc)                        [document.c:845]
  └─ fz_count_chapters                          [document.c:827]
       └─ fz_ensure_layout                      [document.c:647]  ← 回流文档在此排版
  └─ for 每一章: fz_count_chapter_pages          [document.c:836]  累加得到总页数

fz_load_page(ctx, doc, number)                  [document.c:855]
  └─ 把 number 换算成 (chapter, page)
  └─ fz_load_chapter_page(ctx, doc, chapter, page)   [document.c:1000]
       ├─ fz_ensure_layout
       ├─ fz_reap_dead_pages                    顺手回收已 drop 的页面结构
       ├─ 遍历 doc->open 链表: 命中则 fz_keep_page 直接返回（缓存命中）
       └─ 未命中: doc->load_page(...) 新建页面，挂到 open 链表头部
```

页面缓存（`doc->open` 链表）是个值得注意的细节：同一个 (chapter, page) 第二次 `fz_load_chapter_page` 时会直接在链表里找到、`fz_keep_page` 后返回，不必重新解析。这也是为什么页面要维护 `prev`/`next` 指针、以及为什么 `fz_drop_page` 不立即 `fz_free` 而是先标记「dead」再交给后续的 `fz_reap_dead_pages` 清理（线程安全的延迟回收）。

#### 4.3.3 源码精读

公开入口 `fz_open_document` 只有一行，真正的活儿都在 `fz_open_accelerated_document` 里：

[source/fitz/document.c:569-573](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L569-L573) —— `fz_open_document` 转调 `fz_open_accelerated_document(ctx, filename, NULL)`（accel=NULL 表示不带加速文件）。

`fz_open_accelerated_document` 负责「打开文件流 + 选 handler + 调 handler->open」：

[source/fitz/document.c:504-567](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L504-L567) —— 目录走 `fz_open_directory`；普通文件先 `do_recognize_document_content` 选出 handler，再 `fz_open_file` 打开流、按需打开目录上下文（`wants_dir`），最后 `handler->open(...)` 构造文档。整个调用包在 `fz_try` / `fz_always` / `fz_catch` 里，并在 `fz_always` 中释放流/目录/状态——这正是 [u2-l3](u2-l3-exceptions.md) 讲过的 `fz_var` + `fz_always` 标准清理范式。

handler 的「打分识别」核心逻辑（`do_recognize_document_stream_and_dir_content`）：

[source/fitz/document.c:222-379](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L222-L379) —— 遍历所有已注册 handler，分别算「内容分」（`recognize_content`，读流头）和「magic 分」（扩展名/mimetype 命中记 100）。关键加权在 [source/fitz/document.c:329-334](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L329-L334)：两者都命中用 `100 + score`，只命中扩展名用弱分 `1`，最终取最高分 handler。

计数与排版的衔接：

[source/fitz/document.c:845-853](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L845-L853) —— `fz_count_pages` 先 `fz_count_chapters`（内部触发 `fz_ensure_layout`），再循环各章 `fz_count_chapter_pages` 累加。回流文档的排版就发生在 `fz_ensure_layout` 里：

[source/fitz/document.c:647-667](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L647-L667) —— 若 `doc->layout` 存在且 `did_layout == FZ_LAYOUT_NEEDS_UPDATE`，就调用 `doc->layout(ctx, doc)` 完成排版。定页文档没有 `layout` 回调，整个函数对它是空操作。

按页号加载与换算：

[source/fitz/document.c:855-868](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L855-L868) —— `fz_load_page` 逐章累加页数，找到 number 落在哪一章，再调 `fz_load_chapter_page(ctx, doc, chapter, number-start)`；超出范围抛 `FZ_ERROR_ARGUMENT`（注意错误信息里是 `number+1`，即用户视角的 1 起页号）。

真正的页面加载器（含缓存）：

[source/fitz/document.c:1000-1043](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1000-L1043) —— `fz_load_chapter_page`：先 `fz_ensure_layout`、`fz_reap_dead_pages`，再遍历 `doc->open` 链表找 (chapter, number) 命中的页面（命中则 `fz_keep_page` 返回）；未命中则 `doc->load_page` 新建、记录 chapter/number、插入链表头部。`incomplete`（渐进加载未完成）的页面不进链表、也不缓存。

#### 4.3.4 代码实践

这是一个**完整可运行实践**，对应本讲的核心任务：用同一套 API 分别打开一个 PDF 和一个 EPUB，打印页数与首页边界矩形。

1. **实践目标**：亲身体会「同一套 `fz_open_document` / `fz_count_pages` / `fz_load_page` / `fz_bound_page` 作用于不同格式」，并观察回流文档（EPUB）与定页文档（PDF）的边界差异。
2. **操作步骤**：

   把下面这段**示例代码**保存为 `docinfo.c`（它以 [docs/examples/example.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c) 为蓝本，去掉了渲染部分，只做文档/页面信息查询）：

   ```c
   /* docinfo.c —— 打开文档、打印页数与首页边界（示例代码） */
   #include <mupdf/fitz.h>
   #include <stdio.h>
   #include <stdlib.h>

   static void info(fz_context *ctx, const char *path)
   {
       fz_document *doc = NULL;
       fz_page *page = NULL;
       int n;
       fz_rect r;

       /* 这些变量会在 fz_try 内赋值、在 fz_always 内读取，
          按 u2-l3 的要求必须先 fz_var，防止 longjmp 后丢值。 */
       fz_var(doc);
       fz_var(page);

       fz_try(ctx)
       {
           doc = fz_open_document(ctx, path);
           n = fz_count_pages(ctx, doc);
           printf("%-20s pages=%-4d  ", path, n);

           page = fz_load_page(ctx, doc, 0);   /* 线性页号 0 = 第 1 页 */
           r = fz_bound_page(ctx, page);
           printf("page1 size = %.1f x %.1f pt  (rect [%.1f %.1f %.1f %.1f])\n",
                  r.x1 - r.x0, r.y1 - r.y0, r.x0, r.y0, r.x1, r.y1);
       }
       fz_always(ctx)
       {
           /* 释放顺序：先 page（内部会 drop doc 一次），再 doc，最后 ctx。 */
           fz_drop_page(ctx, page);
           fz_drop_document(ctx, doc);
       }
       fz_catch(ctx)
       {
           fz_report_error(ctx);
           fprintf(stderr, "  (failed to read %s)\n", path);
       }
   }

   int main(int argc, char **argv)
   {
       fz_context *ctx;
       int i;

       if (argc < 2)
       {
           fprintf(stderr, "usage: docinfo file [file ...]\n");
           return EXIT_FAILURE;
       }

       ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
       if (!ctx)
       {
           fprintf(stderr, "cannot create context\n");
           return EXIT_FAILURE;
       }

       fz_try(ctx)
           fz_register_document_handlers(ctx);
       fz_catch(ctx)
       {
           fz_report_error(ctx);
           fz_drop_context(ctx);
           return EXIT_FAILURE;
       }

       for (i = 1; i < argc; i++)
           info(ctx, argv[i]);

       fz_drop_context(ctx);
       return 0;
   }
   ```

   编译方式参照 example.c 头部注释。在**已安装**环境下：

   ```bash
   gcc -I/usr/local/include -o docinfo docinfo.c \
       /usr/local/lib/libmupdf.a /usr/local/lib/libmupdf-third.a -lm
   ```

   在**源码树**下（先 `make` 出库），则链接 `build/release/libmupdf.a` 与 `build/release/libmupdf-third.a`（路径随 `build=release/debug` 而变）。

   准备两份测试文档：任意一份 PDF，以及任意一份 EPUB（若无 EPUB，可临时改用一个 XPS/CBZ 文件代替，同样能验证「同一套 API 适用于不同格式」；注意当前构建需启用对应格式，HTML/EPUB 默认启用，详见 [u1-l2](u1-l2-build-system.md)）。

   运行：

   ```bash
   ./docinfo a4.pdf book.epub
   ```

3. **需要观察的现象**：
   - 两份不同格式的文档，用的是**完全相同**的 `fz_open_document` / `fz_count_pages` / `fz_load_page` / `fz_bound_page` 调用，没有任何 `if (是PDF)` 分支。
   - PDF 的 `page1 size` 对应它的 MediaBox/CropBox（A4 约 595×842）。
   - EPUB 的 `page1 size` 约为 420×595（默认 A5 排版）。
4. **预期结果**：PDF 边界由文件决定、固定不变；EPUB 边界由默认排版尺寸决定。两份文档都能正确打印页数和首页矩形。
5. 待本地验证：页数与矩形的**具体数值**取决于你本地的文档，请以实际输出为准。EPUB 的页数尤其会因排版尺寸不同而变化——你可以在 `fz_count_pages` 之前加一句 `fz_layout_document(ctx, doc, 612, 792, 12)`（US Letter 尺寸、12pt 字号），重新运行，观察 EPUB 页数变化，体会「回流」的含义。

#### 4.3.5 小练习与答案

**练习 1**：为什么对一份多章节 EPUB，用 `fz_load_page(ctx, doc, 70)` 去加载第 71 页，可能比加载首页更慢？

> **参考答案**：`fz_load_page`（[source/fitz/document.c:855-868](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L855-L868)）要把线性页号 70 换算成 (chapter, page)，方法是「从第 0 章开始，逐章 `fz_count_chapter_pages` 累加」，直到累加和超过 70。对多章节电子书，这意味着要逐章排版并计数，开销随页号增大而增加。若直接用 `fz_load_chapter_page` 传 (chapter, page)，可跳过这个累加过程。

**练习 2**：`fz_open_document("a.epub")` 时，EPUB 是怎么「赢」过其它 handler 的？如果文件扩展名被故意改成 `.bin`，它还能打开吗？

> **参考答案**：识别靠「内容分 + 扩展名分」综合。`.epub` 扩展名命中 EPUB handler 的 extensions 列表得满分；同时 EPUB 本质是 zip，内容识别（读文件头）也能给出正分，于是总分 `100 + score` 最高。如果改成 `.bin`，扩展名分失效，但 EPUB 的 `recognize_content` 仍会读内容（zip 头、 mimetype）给分——只要这个内容分仍是所有 handler 里最高的，它就能被识别并打开（这正是「内容也认可」优先于「只靠扩展名」的设计意图，见 [source/fitz/document.c:329-334](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L329-L334)）。

**练习 3**：`fz_count_pages` 对 PDF 似乎「秒回」，对大 EPUB 却可能明显耗时，为什么？

> **参考答案**：PDF 的页数写死在文件的 Pages 树里，`count_pages` 回调直接读出来；而 EPUB 是回流文档，`fz_count_pages` 会经 `fz_count_chapters` → `fz_ensure_layout` 触发完整排版（解析所有章节 XHTML、按当前页面尺寸分页），排版完成后才能数出页数（见 [source/fitz/document.c:647-667](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L647-L667) 与 [source/fitz/document.c:845-853](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L845-L853)）。文档越大、排版越重，计数就越慢。

---

## 5. 综合实践

把本讲三个模块串起来，写一个迷你的 **`docinfo` 文档信息工具**：它接受任意多个文档路径，对每份文档打印格式（`fz_lookup_metadata` 取 `format` 键）、是否加密（`fz_needs_password`）、总页数（`fz_count_pages`）、章节数（`fz_count_chapters`）、是否回流（`fz_is_document_reflowable`），以及**前 3 页**各自的边界尺寸。

要求：

1. 复用 4.3.4 的程序骨架与异常清理范式（`fz_var` + `fz_try` / `fz_always` / `fz_catch`，释放顺序 page → document → context）。
2. 用 `fz_lookup_metadata(ctx, doc, FZ_META_FORMAT, buf, sizeof buf)` 取格式字符串（键常量定义见 [include/mupdf/fitz/document.h:974-985](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L974-L985)）；它返回 `-1` 表示该格式不支持，要据此处理。
3. 在循环里依次 `fz_load_page` 取前 3 页（注意别超过 `fz_count_pages`），`fz_bound_page` 后打印尺寸；**每页用完立刻 `fz_drop_page`**，避免同时持有多个页面引用。
4. 用一份 PDF 和一份 EPUB 各跑一次，对照输出：体会「同一套代码、两种格式」，并解释为什么 EPUB 的「章节数」通常大于 1、而 PDF 通常为 1。

这个任务综合了「文档接口（4.1）」「页面边界（4.2）」「打开/计数/加载流程（4.3）」三部分，并要求你正确处理引用计数与异常——这正是后续所有 MuPDF 编程的通用骨架。

## 6. 本讲小结

- `fz_document` 与 `fz_page` 都用「函数指针表 + 派生结构体」实现 C 多态：基类只存回调指针，格式专用层派生后填表，通用层靠函数指针转发，从而一份应用代码处理十几种格式。
- `fz_open_document` 是调度器：先用「内容分 + 扩展名分」打分选出 handler（`do_recognize_document_stream_and_dir_content`），再调 `handler->open` 让该格式真正构造 `fz_document`。
- 页码有两套：面向用户的线性页号（`fz_load_page`）与内部的「章 + 页」`fz_location`（`fz_load_chapter_page`）；多章节格式按页号加载需逐章累加，效率较低。
- 回流文档（EPUB 等）的页数不固定：`fz_count_pages` 会经 `fz_ensure_layout` 触发排版后才能数页；定页文档（PDF）无此开销。
- 每个 `fz_page` 在创建时 `fz_keep_document`、销毁时 `fz_drop_document`，保证页面存活期间文档不会被回收；释放顺序必须是 page → document → context。
- `fz_run_page` = 正文 + 标注 + 表单控件三者之和，只想取正文可用 `fz_run_page_contents`；`fz_bound_page` 默认取 CropBox。

## 7. 下一步学习建议

- 想深入「格式识别与 handler 注册」的细节（extensions/mimetypes 列表、`recognize` 与 `recognize_content` 的区别、14 个 handler 在哪里集中注册），请继续学 [u3-l2 文档处理器与格式识别](u3-l2-document-handlers.md)。
- 想搞清楚 `fz_bound_page` 返回的矩形如何配合 `fz_matrix`（缩放/旋转）变成渲染用的 ctm，以及 `fz_rect` 与 `fz_irect` 的换算，请学 [u3-l3 坐标、矩阵与页面几何](u3-l3-geometry-matrix.md)。
- 想了解加密文档的 `fz_needs_password` / `fz_authenticate_password` 鉴权链路与 `fz_lookup_metadata` 的全部键，请学 [u3-l4 密码、加密与文档元数据](u3-l4-password-metadata.md)。
- 当你准备好把 `fz_run_page` 真正「画出东西」时，下一单元 [u4 设备模型与渲染管线](u4-l1-device-model.md) 会拆解 `fz_device` 这个绘图指令接收者，以及从页面到像素的完整光栅化链路。
