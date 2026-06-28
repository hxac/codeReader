# 扩展：新增格式与输出 handler

## 1. 本讲目标

本讲是整个学习手册的收尾篇，回答一个工程问题：**当 MuPDF 自带的格式不够用时，如何把一种新格式接进这套框架？**

学完本讲，你应当能够：

- 说出 MuPDF 的**两类主要扩展点**——输入侧的 `fz_document_handler` 与输出侧的 `fz_document_writer`，并理解二者为何是镜像关系。
- 列出实现一个最简自定义文档 handler 必须填写的回调字段，以及把它们挂进框架的「一行注册」。
- 说出派生一个自定义 document writer 的标准骨架，以及让它被 `fz_new_document_writer` 按扩展名自动路由的接入方式。
- 理解 `FZ_ENABLE_*` 条件编译宏在编译期裁剪扩展时的作用，以及为何输入与输出两侧的裁剪是分开的。

## 2. 前置知识

本讲是 advanced 层的总结篇，默认你已经读过以下讲义（这里只承接、不重复）：

- **u3-l2 文档处理器与格式识别**：你已经知道 `fz_register_document_handlers` 把各格式 handler 挂进 `ctx->handler->handler[]` 数组，`fz_open_document` 通过「内容分 + magic 分」打分选出 handler，再调 `handler->open` 构造文档。本讲只关心**如何往这张表里加新条目**。
- **u6-l1 document writer：导出抽象**：你已经知道 document writer 是「反向 device」，`begin_page` 返回一个 `fz_device *`，使读写两方向复用同一套 device 接口；并知道 `fz_new_document_writer` 按「扩展名 → 构造函数」查表。本讲只关心**如何加一条新的查表分支**。
- **u10-l2 实现自定义 device**：你已经掌握了「派生结构体以 `super` 起头 + `fz_new_derived_device` 填虚表」的 C 手写继承手法。本讲的 writer 扩展用的是**完全相同的手法**，只是把 `fz_new_derived_device` 换成 `fz_new_derived_document_writer`。

一句话概括两类扩展点的对称性：

| 维度 | 输入侧（读） | 输出侧（写） |
|---|---|---|
| 扩展单元 | `fz_document_handler` | `fz_document_writer` |
| 派生手法 | 全局静态结构体 + 回调 | `fz_new_derived_document_writer` 分配 |
| 路由中枢 | `document-all.c` 注册表 | `writer.c` 的 `fz_new_document_writer` 查表 |
| 关键回调 | `open` / `recognize` | `begin_page` / `end_page` / `close_writer` / `drop_writer` |
| 裁剪宏 | `FZ_ENABLE_<格式>` | `FZ_ENABLE_<格式>_OUTPUT`（部分格式） |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/mupdf/fitz/document.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h) | 声明 `struct fz_document_handler` 及其回调类型签名 |
| [source/fitz/document-all.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c) | 输入侧注册表：`fz_register_document_handlers` 逐个挂 handler |
| [source/fitz/document.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c) | `fz_register_document_handler` 的实现与打分/打开调度 |
| [source/html/txt.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/txt.c) | **最简输入 handler 范例**：`txt_document_handler` |
| [source/cbz/muimg.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/cbz/muimg.c) | 含内容嗅探的输入 handler 范例：`img_document_handler` |
| [include/mupdf/fitz/writer.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h) | 声明 `struct fz_document_writer`、回调类型与构造函数 |
| [source/fitz/writer.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c) | 输出侧路由中枢 `fz_new_document_writer` 查表 |
| [source/fitz/output-svg.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c) | **输出 writer 派生范例**：`fz_new_svg_writer` |
| [include/mupdf/fitz/config.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h) | `FZ_ENABLE_*` 默认值与依赖约束 |

---

## 4. 核心概念与源码讲解

### 4.1 输入格式 handler 扩展

#### 4.1.1 概念说明

回顾 u3-l2：MuPDF 之所以能用「同一套 `fz_document` API 处理 PDF/XPS/EPUB/图片等十余种格式」，是因为每种格式都向框架登记了一个薄适配器——`fz_document_handler`。它本身**不解析格式**，只回答两个问题：

1. **「这个文件是不是归我管？」**（识别）
2. **「如果是，请给我一个 `fz_document` 对象。」**（打开）

真正的格式解析发生在 handler 返回的派生 `fz_document` 内部（如 PDF 的 xref、EPUB 的 HTML 排版）。因此 handler 是通用层（fitz）与格式专用层（pdf/xps/html…）之间的**唯一接缝**。新增一种输入格式，本质上就是补一个 handler 并登记它。

#### 4.1.2 核心流程

新增输入 handler 的完整流程是「定义 → 实现 → 声明 → 注册」四步：

```text
1. 定义一个全局静态 fz_document_handler myfmt_document_handler;
       │  字段顺序固定：recognize, open, extensions, mimetypes,
       │               recognize_content, wants_dir, wants_file, fin
2. 实现 open 回调（必需）—— 返回一个派生自 fz_document 的对象
3.（可选）实现 recognize / recognize_content 做更聪明的识别
4. 在 document-all.c 里 extern 声明 + fz_register_document_handler(ctx, &myfmt_document_handler)
```

运行期的调度（已在 u3-l2 讲过，这里只定位代码点）：

```text
fz_open_document(filename)
   └─ 遍历 ctx->handler->handler[]
        ├─ handler->recognize_content(...)   // 内容嗅探分（可选）
        ├─ handler->recognize(...)           // magic 分（可选）
        └─ extensions[] / mimetypes[] 数组匹配 // 兜底分 100
   └─ 选最高分 handler
        └─ handler->open(ctx, handler, stream, accel, dir, state)  // 真正构造文档
```

#### 4.1.3 源码精读

**① handler 的结构定义**——只有 8 个字段，其中只有 `open` 是真正必需的：

[include/mupdf/fitz/document.h:1127-1138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1127-L1138) —— 这是「注册时由 handler 自己初始化」的虚表。各字段的语义与签名如下（回调类型都在同一个头文件里声明）：

| 字段 | 类型 | 作用 | 是否必需 |
|---|---|---|---|
| `recognize` | `fz_document_recognize_fn` | 按 magic（通常是文件名/mime）打分 | 可选（见 ③） |
| `open` | `fz_document_open_fn` | 打开流并返回 `fz_document *` | **必需** |
| `extensions` | `const char **` | 该格式认领的扩展名数组（`{"txt","text","log",NULL}`） | 强烈建议 |
| `mimetypes` | `const char **` | 该格式认领的 mime 数组 | 可选 |
| `recognize_content` | `fz_document_recognize_content_fn` | 读文件头做内容嗅探打分 | 可选 |
| `wants_dir` | `int` | 打开时是否要把所在目录一并交给 handler（多文件格式如 CBZ/EPUB 需要） | 可选，默认 0 |
| `wants_file` | `int` | 是否需要原始文件名 | 可选 |
| `fin` | `fz_document_handler_fin_fn` | 关闭时收尾 | 可选 |

`open` 与 `recognize` 的签名见 [include/mupdf/fitz/document.h:370](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L370) 与 [include/mupdf/fitz/document.h:385](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L385)：

```c
typedef fz_document *(fz_document_open_fn)(fz_context *ctx,
        const fz_document_handler *handler, fz_stream *stream,
        fz_stream *accel, fz_archive *dir, void *recognize_state);

typedef int (fz_document_recognize_fn)(fz_context *ctx,
        const fz_document_handler *handler, const char *magic);
```

注意 `open` 拿到的是已经抽象好的 `fz_stream`（不是裸文件句柄）与可选的 `dir`（目录归档）——这保证你的 handler 既能读磁盘文件，也能读内存流、管道、ZIP 内成员，复用了 u8-l1 的流管线。

**② 最简范例：txt handler**——整个 handler 只填了 4 个槽，其余靠 C 的「部分初始化 = 剩余成员零初始化」自动置 NULL/0：

[source/html/txt.c:251-257](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/txt.c#L251-L257) —— `recognize=NULL`，`recognize_content`、`wants_dir`、`wants_file`、`fin` 全部零初始化：

```c
fz_document_handler txt_document_handler =
{
    NULL,                 // recognize
    txt_open_document,    // open
    txt_extensions,       // extensions  = {"txt","text","log",NULL}
    txt_mimetypes         // mimetypes   = {"text/plain",NULL}
};
```

这就是「最小可工作 handler」的全部。配套的 `open` 回调 [source/html/txt.c:231-235](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/txt.c#L231-L235) 把纯文本包成 HTML 再交给 HTML 排版引擎，是「用现有能力拼新格式」的典型。

**③ 为何 `recognize` 可以为 NULL**——因为识别打分有兜底。看打分循环 [source/fitz/document.c:305-324](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L305-L324)：

```c
if (dc->handler[i]->recognize)
    magic_score = dc->handler[i]->recognize(ctx, dc->handler[i], magic);
// 即便 recognize 为 NULL，下面仍会用 extensions[]/mimetypes[] 兜底打 100 分
for (entry = &dc->handler[i]->mimetypes[0]; *entry; entry++)
    if (!fz_strcasecmp(magic, *entry)) { magic_score = 100; break; }
if (ext)
    for (entry = &dc->handler[i]->extensions[0]; *entry; entry++)
        if (!fz_strcasecmp(ext, *entry)) { magic_score = 100; break; }
```

也就是说：只要你的 `extensions[]` 数组里列了后缀，框架就能按扩展名认领你的格式，`recognize` 完全可以省略。txt handler 正是这么做的。`recognize_content` 则用于扩展名不可靠、要读文件头判定的格式（如图片）。

**④ 含内容嗅探的范例：img handler** [source/cbz/muimg.c:319-326](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/cbz/muimg.c#L319-L326) 填了第 5 个槽 `img_recognize_content`，让框架可以靠文件头（PNG/JPEG 魔数）而非扩展名识别：

```c
fz_document_handler img_document_handler =
{
    NULL,
    img_open_document,
    img_extensions,
    img_mimetypes,
    img_recognize_content   // 读文件头打分
};
```

**⑤ 注册：往表里加一行**——`fz_register_document_handler` 的实现 [source/fitz/document.c:194-214](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L194-L214) 做三件事：忽略空指针、**按指针去重**（同一 handler 注册两次是 no-op）、容量检查（上限 `FZ_DOCUMENT_HANDLER_MAX = 32`，见 [source/fitz/document.c:37](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L37)），最后 `dc->handler[dc->count++] = handler` 追加。

实际的「挂表」发生在 [source/fitz/document-all.c:40-80](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L40-L80) 的 `fz_register_document_handlers`。每加一种格式就是一对 `extern` 声明 + 一行注册调用（详见 4.3 节）。

> 关键结论：**新增输入格式的全部框架改动量 = 一个 `extern` + 一行 `fz_register_document_handler`**；剩下的工作全在「实现 `open` 回调、写出派生 `fz_document`」这个与框架无关的纯格式活里。

#### 4.1.4 代码实践

**实践目标**：动手设计（不必完整实现）一个最简自定义文档 handler 的骨架，并把它的注册过程对齐到 `document-all.c`。

**操作步骤（源码阅读 + 骨架设计）**：

1. 打开 [source/html/txt.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/txt.c) 与 [source/cbz/muimg.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/src)（注意 muimg 的派生 `img_document`/`img_page` 结构体在文件开头），作为「最简」与「带嗅探」两种模板。
2. 假设你要为一种叫 `.mft`（My Format）的自定义格式写 handler。在一张表里列出 `fz_document_handler` 的 8 个字段，对每个字段写出：填什么 / 可否为 NULL / 为什么。下文「小练习答案」给了参考。
3. 打开 [source/fitz/document-all.c:25-38](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L25-L38) 看 `extern` 声明风格，再对照 [source/fitz/document-all.c:40-80](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L40-L80) 的注册块，写出「为 `.mft` 加注册」需要新增的两行代码。

**预期结果**：你能说出最小 handler 至少要填 `open` + `extensions`，其余可留 NULL/0；并知道注册只需在 `document-all.c` 加 `extern fz_document_handler mft_document_handler;` 与 `fz_register_document_handler(ctx, &mft_document_handler);`。

**待本地验证**：若你想真正跑通，需自行实现 `mft_open_document`（返回一个派生 `fz_document`，至少实现 `count_pages`/`load_page`/`run_page`/`drop_document` 等回调——这正是 u3-l1 讲过的虚表），并把新 `.c` 文件加入 `Makelists` 的源码清单后重新 `make`。

#### 4.1.5 小练习与答案

**Q1**：`txt_document_handler` 把 `recognize` 设成了 NULL，为什么它仍能被按扩展名识别？

**答案**：因为识别打分有兜底。当 `handler->recognize` 为 NULL 时，框架不会跳过该 handler，而是继续用 `extensions[]` 和 `mimetypes[]` 两个数组匹配，命中即给 `magic_score = 100`（见 [source/fitz/document.c:309-324](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L309-L324)）。所以列了扩展名就足以认领。

**Q2**：`fz_register_document_handler` 为什么要先做「按指针去重」？

**答案**：因为 `fz_register_document_handlers` 可能被应用多次调用（例如先调框架默认注册、再补自己的），去重保证同一个 handler 全局结构体不会在 `handler[]` 数组里占两个槽，既省容量（上限 32）也避免打分时同一格式投两票（见 [source/fitz/document.c:206-208](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L206-L208)）。

---

### 4.2 输出格式 writer 扩展

#### 4.2.1 概念说明

输出侧是输入侧的镜像：输入靠 `fz_document_handler` 把「字节流 → `fz_document`」，输出靠 `fz_document_writer` 把「`fz_document` → 字节流」。回顾 u6-l1：document writer 在 `fz_device` 之上加了一层「多页 + 文档框架」，其 `begin_page` 回调返回一个 `fz_device *`，使读写两方向复用同一套 device 接口。

与输入 handler（一个静态全局结构体）不同，writer 是**每次创建时按需分配的派生对象**——因为一次转换就要 new 一个 writer、用完 drop。派生手法与 u10-l2 的自定义 device 完全同构，只是把基类从 `fz_device` 换成 `fz_document_writer`。

#### 4.2.2 核心流程

新增输出 writer 的「派生 → 路由」两步：

```text
1. 派生：写一个 fz_new_<fmt>_writer(ctx, path, options)
       └─ fz_new_derived_document_writer(ctx, myfmt_writer,
              myfmt_begin_page, myfmt_end_page, myfmt_close, myfmt_drop)
       └─ begin_page 内部返回一个 fz_device（自创或借用现成的 svg/draw/stext device）

2. 路由：在 writer.c 的 fz_new_document_writer 查表里加一条
       └─ if (is_extension(format, "mft")) return fz_new_myfmt_writer(ctx, path, options);
       └─ 同步在 fz_new_document_writer_with_output 里加一条 _with_output 版本
```

writer 的生命周期（u6-l1 已建立，此处定位代码）固定为：

```text
fz_new_<fmt>_writer  →  反复 (fz_begin_page → fz_run_page → fz_end_page)
                      →  fz_close_document_writer  →  fz_drop_document_writer
```

#### 4.2.3 源码精读

**① writer 基类与四个回调**——比 device 虚表小得多，只有 4 个函数指针 + 1 个借出的 `dev` 字段：

[include/mupdf/fitz/writer.h:224-231](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L224-L231) —— 基类结构体（public 以便派生，但「不要直接访问成员」）：

| 字段 | 作用 |
|---|---|
| `begin_page` | 开始一页，返回一个 `fz_device *` 供调用方 `fz_run_page` 写内容（见 [writer.h:42](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L42)） |
| `end_page` | 结束一页，收到的 `dev` 由 writer 托管（见 [writer.h:50](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L50)） |
| `close_writer` | 写文件级 trailer（如 PDF 的 xref/`%%EOF`），完成后文件即完整（[writer.h:59](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L59)） |
| `drop_writer` | 释放 writer 自身资源（[writer.h:70](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L70)） |
| `dev` | 基类借出字段：当前页正在用的 device，由 `fz_begin_page`/`fz_end_page` 维护 |

**② 派生宏**——与 `fz_new_derived_device`（u10-l2）是亲兄弟：

[include/mupdf/fitz/writer.h:72-73](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L72-L73) —— 传入派生类型、四个回调，自动算大小、清零并填虚表：

```c
#define fz_new_derived_document_writer(CTX,TYPE,BEGIN_PAGE,END_PAGE,CLOSE,DROP) \
    ((TYPE *)Memento_label(fz_new_document_writer_of_size(CTX,sizeof(TYPE),...),#TYPE))
```

底层的 `fz_new_document_writer_of_size` 见 [source/fitz/writer.c:27-38](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L27-L38)：`fz_calloc` 分配并清零整块（保证未填的派生字段为 0），再写四个函数指针。

**③ 真实派生范例：SVG writer**——这是写一个新 writer 最该照抄的模板：

[source/fitz/output-svg.c:99-113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L99-L113) —— 注意 `close` 传了 `NULL`（SVG 每页自包含、无需文件级 trailer，这正是 u6-l3 讲的「矢量重发」类后端特征）：

```c
fz_svg_writer *wri = fz_new_derived_document_writer(ctx, fz_svg_writer,
        svg_begin_page, svg_end_page, NULL, svg_drop_writer);
fz_try(ctx) {
    fz_parse_svg_device_options(ctx, &wri->opts, args);  // 解析 options 字符串
    wri->path = fz_strdup(ctx, path ? path : "out-%04d.svg");
}
fz_catch(ctx) { fz_free(ctx, wri); fz_rethrow(ctx); }
return (fz_document_writer*)wri;
```

这里的 `svg_begin_page` 内部会 `fz_new_svg_device(...)` 返回一个 SVG device，于是「写 SVG」本质上就是「把页面指令录成 SVG 标签」——`begin_page` 返回的 device 决定了输出取向（u6-l3 的四类后端由此而来）。

**④ 路由中枢：让 `fz_new_document_writer` 认识新扩展名**——这是把新 writer 接进框架的唯一改动点：

[source/fitz/writer.c:147-223](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L147-L223) —— 按「扩展名 → 构造函数」逐一 `is_extension` 比对，命中即返回对应 writer；全部不匹配抛 `cannot detect document format`。新增格式就是在这张表里加一条 `if`。注意它支持「显式 format 优先于 path 推断、找不到再退一档扩展名」的循环（[writer.c:217-220](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L217-L220)）。

> **输入 vs 输出扩展的对称对照**：输入侧「加 handler」= 在 `document-all.c` 注册表加一行 + 实现 `open`；输出侧「加 writer」= 在 `writer.c` 查表加一条 `if` + 实现 `fz_new_<fmt>_writer`。两侧都遵守「路由中枢只改一处，业务逻辑隔离在派生实现里」的开放-封闭原则。

#### 4.2.4 代码实践

**实践目标**：照 SVG writer 的模板，设计一个最简自定义 writer 的骨架，并定位它在 `writer.c` 的接入点。

**操作步骤（源码阅读 + 骨架设计）**：

1. 精读 [source/fitz/output-svg.c:99-113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L99-L113)，记下「派生结构体 → `fz_new_derived_document_writer` → 解析 options → 异常清理」四段式。
2. 假设你要新增 `.mfw`（My Format Writer）输出。写出 `fz_new_mfw_writer` 的骨架：派生结构体 `mfw_writer` 以 `fz_document_writer super` 起头，附带你需要的私有字段（如输出路径、options 解析结果）。
3. 决定你的 `begin_page` 返回什么 device：是借用现成的（`fz_new_draw_device` 光栅化、`fz_new_stext_device` 取文本、`fz_new_svg_device` 矢量），还是自创一个（参照 u10-l2）。这一步直接决定你属于 u6-l3 的哪一类后端。
4. 在 [source/fitz/writer.c:147-223](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L147-L223) 中找到 `is_extension(format, "svg")` 那一行，在其附近写出你新增的一行 `if (is_extension(format, "mfw")) return fz_new_mfw_writer(ctx, path, options);`，并同步在 [source/fitz/writer.c:225-292](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L225-L292) 的 `_with_output` 版本里加一条。

**预期结果**：你能复述「派生 + 路由」两步，并指出 `close` 在无文件级 trailer 时可传 `NULL`、`drop` 必须释放 begin_page 累积的 device 与私有资源。

**待本地验证**：真正编译需要在 `include/mupdf/fitz/writer.h` 声明 `fz_new_mfw_writer`、在 `Makelists` 加入新 `.c`，并实现 `mfw_begin_page/mfw_end_page/mfw_drop_writer` 三个回调后 `make`。

#### 4.2.5 小练习与答案

**Q1**：`fz_drop_document_writer` 发现 `close_writer` 还没调用时为什么会 `fz_warn`（[source/fitz/writer.c:323-324](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L323-L324)）？

**答案**：因为 `close_writer` 负责写文件级 trailer（如 PDF 的 xref 与 `%%EOF`），跳过它直接 drop 会让输出文件处于「不完整/不可用」状态。warn 是在提醒调用方：文件已生成但可能损坏。这与 writer.h 注释里「Calling drop without having previously called close may leave the file in an inconsistent state」一致。

**Q2**：为什么 SVG writer 的 `close` 回调可以传 `NULL`，而 PDF writer 不行？

**答案**：SVG 是「矢量重发」类后端，每页自包含（一个 `<svg>` 文档），`end_page` 时就能冲刷完该页，无需文件级收尾；而 PDF 是「对象树写回」，要等所有页累积成一棵 `pdf_document` 对象树后，在 `close_writer` 由 `pdf_write_document` 一次性序列化（写 xref/trailer/startxref）。所以 PDF 的 close 不可省略（参见 u6-l3）。

---

### 4.3 条件编译裁剪

#### 4.3.1 概念说明

新增的格式未必每个发行版都需要——嵌入式设备可能只要 PDF，不要 EPUB 的整套 HTML 引擎。MuPDF 用一组 `FZ_ENABLE_*` 宏在**编译期**裁剪扩展，被裁掉的格式从源码层面就不存在（handler 不注册、writer 查表分支被 `#if` 删除），既省二进制体积，也省对应的第三方依赖。

这是 u1-l2 讲过的「编译期功能裁剪」机制在扩展点上的具体落地：Makerules 把 `xps=no` 之类的 Make 变量翻成 `-DFZ_ENABLE_XPS=0`，进而控制本节看到的所有 `#if` 守卫。

#### 4.3.2 核心流程

裁剪的传导链：

```text
Make 变量（如 mujs=no / html=no）
   └─ Makerules 翻译成 -DFZ_ENABLE_<X>=0  编译宏
        └─ config.h：#ifndef FZ_ENABLE_<X> #define FZ_ENABLE_<X> 1（给默认 1）
             └─ document-all.c：#if FZ_ENABLE_<X> 包住 fz_register_document_handler(...)
             └─ writer.c：#if FZ_ENABLE_<X>_OUTPUT 包住对应 writer 查表分支
             └─ mutool.c 等：#if 包住对应子命令表项（见 u1-l4）
```

输入侧与输出侧的裁剪是**分开**的：`FZ_ENABLE_PDF` 控制能否**读** PDF，`FZ_ENABLE_OCR_OUTPUT`/`FZ_ENABLE_DOCX_OUTPUT`/`FZ_ENABLE_ODT_OUTPUT` 控制**写**这些格式。能读不一定能写，反之亦然。

#### 4.3.3 源码精读

**① config.h 给默认值**——所有 `FZ_ENABLE_*` 默认都是 1（全开），用户可用 `#define` 或编译宏覆盖：

[include/mupdf/fitz/config.h:196-266](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L196-L266) 是一串 `#ifndef ... #define ... 1 #endif`，例如：

```c
#ifndef FZ_ENABLE_PDF
#define FZ_ENABLE_PDF 1
#endif
...
#ifndef FZ_ENABLE_DOCX_OUTPUT
#define FZ_ENABLE_DOCX_OUTPUT 1
#endif
```

文件上方的注释块 [config.h:49-59](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L49-L59) 与 [config.h:75-77](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L75-L77) 则是「给用户抄」的覆盖示例（默认全注释掉）。

**② 输入侧守卫**——`fz_register_document_handlers` 里每条注册都被 `#if FZ_ENABLE_*` 包住：

[source/fitz/document-all.c:42-79](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L42-L79)，典型片段：

```c
#if FZ_ENABLE_PDF
    fz_register_document_handler(ctx, &pdf_document_handler);
#endif /* FZ_ENABLE_PDF */
...
#if FZ_ENABLE_HTML
    fz_register_document_handler(ctx, &html_document_handler);
    fz_register_document_handler(ctx, &xhtml_document_handler);
#if FZ_ENABLE_MD
    fz_register_document_handler(ctx, &md_document_handler);
#endif /* FZ_ENABLE_MD */
#endif /* FZ_ENABLE_HTML */
```

两个要点：

- **嵌套依赖**：`md`（Markdown）handler 复用 HTML 引擎，所以它被嵌套在 `#if FZ_ENABLE_HTML` 内部——关掉 HTML 必然连带关掉 MD。
- **永远注册**：[document-all.c:79](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L79) 的 `gz_document_handler` **没有** `#if` 守卫，因为它处理的是「外层 gzip 压缩」，与具体内层格式无关，必须始终可用。

config.h 用 `#error` 强制声明这种依赖：[config.h:274-293](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L274-L293) —— 例如 `#if FZ_ENABLE_HTML == 1` 且 HTML 引擎未启用就直接编译报错，防止「开了格式却没开它的引擎」这种不一致配置。

**③ 输出侧守卫**——`writer.c` 查表里只有部分格式被守卫：

[source/fitz/writer.c:155-216](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L155-L216)，只有依赖重资产或可选第三方库的输出格式才加守卫：

```c
#if FZ_ENABLE_OCR_OUTPUT
    if (is_extension(format, "ocr")) return fz_new_pdfocr_writer(ctx, path, options);
#endif
#if FZ_ENABLE_PDF
    if (is_extension(format, "pdf")) return fz_new_pdf_writer(ctx, path, options);
#endif
...
#if FZ_ENABLE_ODT_OUTPUT
    if (is_extension(format, "odt")) return fz_new_odt_writer(ctx, path, options);
#endif
#if FZ_ENABLE_DOCX_OUTPUT
    if (is_extension(format, "docx")) return fz_new_docx_writer(ctx, path, options);
#endif
```

其余（svg/png/ps/pwg/text…）无守卫，因为它们只用 fitz 内建能力（draw device、band-writer、stext device），没有可裁剪的外部依赖。

> 关键结论：**新增格式若依赖可选第三方库，应同时加 `FZ_ENABLE_*` 宏 + 在注册/路由处加 `#if` 守卫 + 在 config.h 给默认值与依赖 `#error`**；若只用 fitz 内建能力（如自创 device 做分析），则可不做裁剪、常驻编译。

#### 4.3.4 代码实践

**实践目标**：把「新增格式是否需要裁剪宏」这件事与 config.h、document-all.c、writer.c 三处对应起来。

**操作步骤（编译实验 + 源码核对）**：

1. 阅读 [include/mupdf/fitz/config.h:196-293](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L196-L293)，列出哪些 `FZ_ENABLE_*` 之间存在「关 A 必须关 B」的依赖关系。
2. 按 u1-l2 的方法尝试一次最小裁剪编译：`make build=release have_xps=no`（或对应 Makerules 变量），观察编译产物。
3. 用裁剪后的 `mutool` 打开一个 XPS 文件，验证「识别失败」——因为 `xps_document_handler` 根本没注册进表里（[document-all.c:45-47](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L45-L47) 被 `#if` 删掉了）。

**需要观察的现象**：裁剪前 `mutool draw file.xps` 正常渲染；裁剪后报「unsupported document format」之类（打分全为 0 时 `fz_open_document` 抛 `FZ_ERROR_UNSUPPORTED`，见 u3-l2）。

**预期结果**：你能解释「为何裁剪是从源头删除而非运行期开关」——被 `#if 0` 的代码不进二进制，连对应的第三方库（如关 EPUB 时连带不链 mujs/gumbo）也不会被链接。

**待本地验证**：具体能传哪些 `have_<x>=no` / `<x>=no` 变量，请以本地 `Makerules` 的实际变量名为准（变量名可能随版本调整）。

#### 4.3.5 小练习与答案

**Q1**：为什么 `gz_document_handler` 没有 `#if FZ_ENABLE_*` 守卫？

**答案**：因为 gzip 处理的是「文件外层压缩」，与内层是什么格式无关（一个 `report.pdf.gz` 解开后可能是 PDF 也可能是别的）。它是所有格式之上的通用壳层，必须始终注册，否则压缩文件永远打不开（[document-all.c:79](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L79)）。

**Q2**：`FZ_ENABLE_PDF` 和 `FZ_ENABLE_DOCX_OUTPUT` 控制的是同一件事吗？

**答案**：不是。前者控制能否**读入** PDF（输入 handler），后者控制能否**写出** docx（输出 writer）。输入与输出裁剪是分开的——你可以编译一个「能读 PDF 但不能写 docx」的精简版本，反之亦然。这正是 document-all.c 用 `FZ_ENABLE_<格式>`、writer.c 用 `FZ_ENABLE_<格式>_OUTPUT` 两套命名的原因。

---

## 5. 综合实践

把本讲三类知识串成一个完整的「新增 `.mft` 格式」方案设计（重在方案，不要求一次写完所有解析代码）：

1. **输入侧**（对齐 4.1）：画出你的 `mft_document_handler` 字段填表——`open` 填 `mft_open_document`、`extensions` 填 `{"mft", NULL}`；判断 `.mft` 是否有可靠的文件头魔数，决定要不要实现 `recognize_content`。若 `.mft` 是单文件，`wants_dir=0`；若是多文件容器，则 `wants_dir=1`。然后在 [document-all.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c) 加 `extern` + 一行注册。
2. **格式实现**（承接 u3-l1）：你的 `mft_open_document` 返回一个派生 `fz_document`，至少实现 `count_pages`/`load_page`/`run_page`（在 `run_page` 里把 `.mft` 内容翻译成 device 回调）/`drop_document`。若 `.mft` 本质是「文本+图片」，可像 txt handler 那样转交给 HTML 引擎，或像 img handler 那样包装。
3. **输出侧**（对齐 4.2）：如果还想「把任意文档转成 `.mft`」，照 [output-svg.c:99-113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L99-L113) 写 `fz_new_mfw_writer`，决定 `begin_page` 返回的 device（光栅化→draw device；取文本→stext device；自创→参考 u10-l2），并在 [writer.c:147-223](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L147-L223) 加一条路由。
4. **裁剪**（对齐 4.3）：若 `.mft` 解析依赖了可选第三方库，在 config.h 加 `FZ_ENABLE_MFT` 默认值 + 依赖 `#error`，并在两处注册/路由加 `#if` 守卫；若只用 fitz 内建能力，则不做裁剪。

**验收标准**：你能用一张图把「`.mft` 文件 → `fz_open_document` 打分选 handler → `handler->open` 返回派生 doc → `fz_run_page` 驱动 device」和「`fz_new_document_writer("out.mfw")` → writer.c 路由 → `begin_page` 返回 device → `fz_run_page` 写入 → `close` 收尾」两条链路画清楚，并标注每一处框架改动点。

## 6. 本讲小结

- MuPDF 有两类主要扩展点：输入侧 `fz_document_handler`（读）、输出侧 `fz_document_writer`（写），二者是镜像关系。
- 新增输入格式 = 实现一个 `fz_document_handler`（至少填 `open` + `extensions`）+ 在 `document-all.c` 加一个 `extern` 和一行 `fz_register_document_handler`。
- 新增输出格式 = 用 `fz_new_derived_document_writer` 派生一个 writer（与 u10-l2 自定义 device 同构）+ 在 `writer.c` 的 `fz_new_document_writer` 查表加一条 `if` 路由。
- `recognize` 可为 NULL：识别打分有 `extensions[]`/`mimetypes[]` 数组兜底（命中给 100 分），只有需要内容嗅探时才实现 `recognize_content`。
- `FZ_ENABLE_*` 在编译期裁剪：输入用 `FZ_ENABLE_<格式>`、输出用 `FZ_ENABLE_<格式>_OUTPUT`，二者独立；config.h 用 `#error` 强制依赖一致性（如关 HTML 引擎就不能开 EPUB）。
- 路由中枢（document-all.c 注册表、writer.c 查表）都遵循「只改一处、业务隔离在派生实现」的开放-封闭原则——这正是 MuPDF 能用一份框架代码扩展到十余种格式的架构根基。

## 7. 下一步学习建议

本讲是手册的终点，至此你已完整走过 MuPDF 的「宏观 → fitz 基石 → 文档抽象 → 渲染管线 → 文本搜索 → 导出转换 → PDF 对象模型 → 流与压缩 → 性能并发 → 交互扩展」全链路。继续深入的方向：

- **动手做一个真实扩展**：挑一个 MuPDF 尚不支持的小众格式（如某专有矢量格式或纯文本日志），按本讲的「输入 handler + 派生 document」骨架实现它，再补一个对应的输出 writer，跑通 `mutool convert` 往返。
- **研读一个完整的 handler-writer 对**：以 SVG 为例，对照 [source/svg/svg-doc.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/svg/svg-doc.c)（读）与 [source/fitz/output-svg.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c)（写），体会同一格式在读写两侧的对称设计。
- **回看架构主线**：重读 u3-l1（document/page 虚表）、u4-l1（device 虚表）、u6-l1（writer 虚表），你会发现这三张虚表是 MuPDF 解耦的同一套思想在不同抽象层的复用——理解了这套「手写多态 + 判空转发」，就理解了整个框架的扩展哲学。
- **关注许可证边界**：若你的扩展要商用，回顾 u1-l1 的双许可说明——基于 MuPDF 的衍生作品（含你的新 handler/writer）受 AGPL 约束，商业分发需购买授权。
