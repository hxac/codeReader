# document writer：导出抽象

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `fz_document_writer` 为什么被称为「反向 device」，它和第四单元的 `fz_device` 是怎样的一对镜像关系。
- 写出一个标准的多页写入循环：`fz_new_document_writer` → 反复 `fz_begin_page` / `fz_run_page` / `fz_end_page` → `fz_close_document_writer` → `fz_drop_document_writer`。
- 理解一个写入器派生结构体长什么样，以及 `fz_new_document_writer` 如何按扩展名（`.svg`、`.pdf`、`.text`……）把请求分发到具体格式（pdf / svg / text / docx / ps 等）。
- 能用这套抽象把任意格式输入文档逐页「翻译」成目标格式导出文件。

本讲是整本手册从「读文档」转向「写文档」的转折点。前面几讲都在讲如何**消费**绘图指令（draw device 画位图、stext device 抽文本），本讲开始讲如何**生产**一个新文档。

## 2. 前置知识

本讲建立在第四单元「设备模型」之上（依赖 u4-l1），请先确认你已经理解：

- **`fz_device`（设备）**：一个函数指针虚表，是绘图指令的「消费者」。页面内容流被解释后，会调用 `fill_path` / `fill_text` 等设备回调。回顾 u4-l1：device 把格式解释器（生产者）发出的统一指令分流到不同后端。
- **`fz_run_page(ctx, page, dev, ctm, cookie)`**：把一页的内容驱动到一个 device 上，`ctm` 是当前变换矩阵（u3-l3）。
- **`fz_rect` / 页面边界**：`fz_bound_page` 返回的页面矩形（u3-l1）。
- **引用计数 keep/drop**：每个 `new`/`begin` 都要配对一个 `drop`/`close`（u2-l2）。

如果你还没看过 u4-l1，强烈建议先读，因为本讲的「反向 device」概念完全建立在对 `fz_device` 的理解之上。

一个直觉性的比喻：如果说 `fz_device` 是「一个能听懂绘图指令的画板」，那么 `fz_document_writer` 就是「一台能管理多页画板、并最终把它们装订成一本新文档的印刷机」。印刷机内部仍然用画板（device）来接收每一页的内容，但它额外负责「每页的开头/结尾」「整本书的封面/封底」这类 device 管不了的框架性工作。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/mupdf/fitz/writer.h` | 写入器的公共 API 契约：虚表结构体、四个回调类型、所有格式构造函数声明、`begin/end_page`/`close`/`drop` 包装函数。 |
| `source/fitz/writer.c` | 写入器的通用实现：派生结构体分配器、格式分发（按扩展名选具体写入器）、生命周期包装函数、便捷函数 `fz_write_document`。 |
| `source/fitz/output-svg.c` | **格式派生范例 1**：SVG 写入器。派生结构体 + 自定义 `begin/end_page`，展示「writer 包一层 svg device」的最小骨架。 |
| `source/fitz/stext-output.c` | **格式派生范例 2**：文本写入器（text/html/xhtml/stext.xml/stext.json 五合一）。展示「writer 包一层 stext device，在 `end_page` 里导出成不同文本格式」。 |
| `source/tools/muconvert.c` | **使用范例**：`mutool convert` 的实现，是「打开源文档 → 逐页 begin/run/end → close」的标准工程写法。 |

定位口诀（承接 u1-l3）：找 API 看 `include/`，找通用机制看 `source/fitz/writer.c`，找某种格式的具体写法看 `source/fitz/output-<格式>.c`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **写入器抽象**——什么是 document writer，为什么它是「反向 device」。
2. **多页写入循环**——`begin_page`/`run_page`/`end_page`/`close` 的状态机与生命周期。
3. **格式派生与分发**——如何派生一个具体格式写入器，`fz_new_document_writer` 如何按扩展名路由。

### 4.1 写入器抽象：document writer 是「反向 device」

#### 4.1.1 概念说明

先回忆第四单元的渲染链路（**读方向**）：

```
源文档 → 解释器（解释页面内容流） → 调用 device 回调 → draw device 把指令光栅化进 pixmap
```

这里 `device` 是绘图指令的**消费者**，它只关心「这一页要画什么」，不关心「这一页在文件里怎么组织」、也管不了「整个文件的头尾」。

但当你想**导出**一个新文档（比如把 PDF 转成 SVG、把 EPUB 转成 text、把任意文档转成新 PDF）时，你面临的是**写方向**的需求：

- 每一页都需要一个「画板」来接收内容（这正是 device 能干的活）。
- 但每页开头/结尾要写格式特有的页框（SVG 的 `<svg>` 根元素、PDF 的页对象、PS 的 `showpage`）。
- 整个文件还要写头尾（PDF 的 `%PDF-1.7` 头与 `%%EOF` 尾、HTML 的 `<!DOCTYPE>` 与 `</html>`、PS 的 `%!PS` 头）。

`fz_document_writer` 就是在 device 之上**再加一层「多页 + 文档」框架**的抽象。它的关键设计是：

> **`begin_page` 回调返回一个 `fz_device *`。** 你拿到这个 device 后，用 `fz_run_page` 把页面内容灌进去——和渲染时完全一样的用法。`end_page` 再把这个 device 收回去，做该格式的页收尾。

这就是「反向 device」的含义：device 的接口在读写两个方向上完全复用，writer 只是在「写方向」上替你管理了 device 的创建、页框、文档头尾。换句话说：

- **device = 单页内容的抽象**（消费指令）。
- **writer = 多页/多文档的抽象 = device + 页框协议 + 文档头尾协议**。

这种「同一套 device 接口双向复用」是 MuPDF 能用一份代码既渲染又导出的根本原因。

#### 4.1.2 核心流程

一个写入器的完整生命周期是一个严格的状态机：

```
new_writer ─┐
            ▼
      ┌─── begin_page(mediabox) ──► 返回一个临时 dev
      │           │
      │           ▼
      │      run_page(page, dev, ctm)   （把源页内容灌进 dev）
      │           │
      │           ▼
      └──── end_page()                 （writer 关闭并 drop 掉 dev，写页框）
                  │
                  ▼
            (重复 N 页)
                  │
                  ▼
            close_writer()              （写文档头尾，文件至此完整可读）
                  │
                  ▼
            drop_writer()               （释放 writer 自身）
```

四条铁律：

1. **配对**：`begin_page` 和 `end_page` 必须严格配对；`close_writer` 在所有页写完后调用一次；`drop_writer` 总是最后调用（且必须调用）。
2. **device 归 writer 管**：`begin_page` 返回的 device 是**借出**的，你不能自己 `drop` 它——`end_page` 会替你收回去（见 4.2 的源码）。
3. **close 后文件才完整**：`close_writer` 负责写文件尾（如 `%%EOF`），没调用 close 就 drop，文件可能是不完整的。
4. **顺序逆序**：和 u1-l5 的资源释放铁律一致——先释放页面内容、再 close/drop writer、最后才 drop context。

#### 4.1.3 源码精读

先看写入器的「底座」结构体，它就四个函数指针加一个 device 指针：

[include/mupdf/fitz/writer.h:L224-L231](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L224-L231) 定义了基类 `struct fz_document_writer`：`begin_page` / `end_page` / `close_writer` / `drop_writer` 四个回调，外加一个 `dev` 字段（当前页借出的设备）。

四个回调的签名在同文件里声明，其中最关键的是 `begin_page` 返回一个 device：

[include/mupdf/fitz/writer.h:L42-L50](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L42-L50) 声明 `begin_page` 接收页面尺寸 `mediabox` 并 **返回一个 `fz_device *`**，`end_page` 接收并处理这个 dev。这正是「writer 借出 device」契约的来源。

派生某个格式写入器时，用宏 `fz_new_derived_document_writer` 一步分配带额外字段的派生结构体并把四个回调填进基类：

[include/mupdf/fitz/writer.h:L72-L73](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/writer.h#L72-L73) 这个宏展开后调用 `fz_new_document_writer_of_size`，传入派生类型的大小和四个回调，返回带正确虚表的指针（这是 C 语言手写「结构体继承」的标准手法，与 u3-l1 的 document 派生同构）。

底座分配器在源码里非常简短：

[source/fitz/writer.c:L27-L38](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L27-L38) `fz_new_document_writer_of_size` 用 `fz_calloc` 分配「大小为 `size`」的整块内存（多出来的字节被零初始化，即派生类型的额外字段），然后把四个回调填进基类字段。注意它**只填虚表，不碰 `dev`**——`dev` 由后续的 `begin_page` 设置。

#### 4.1.4 代码实践

**实践目标**：用源码阅读确认「writer 内部包了一个 device」。本练习不改任何源码，只读代码。

**操作步骤**：

1. 打开 SVG 写入器实现 `source/fitz/output-svg.c`，找到 `svg_begin_page`（下面会给链接）。
2. 观察它的返回值：它返回的是 `fz_new_svg_device_with_options(...)` 创建的 **svg device**——也就是说，SVG 写入器在每一页都临时造一个 svg device 给你用。
3. 再打开文本写入器 `source/fitz/stext-output.c` 的 `text_begin_page`，观察它返回 `fz_new_stext_device(...)`——文本写入器包的是 stext device（u5-l2 讲过）。

**需要观察的现象**：两种完全不同性质的输出（矢量 SVG vs 文本）背后，writer 的 `begin_page` 都只是「造一个 device 还给你」。这印证了「device 是单页内容抽象，writer 是多页框架抽象」的分工。

**预期结果**：你能用一句话描述：**「SVG writer 包 svg device，text writer 包 stext device，writer 的职责是在 device 之上加多页与文档框架。」**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `struct fz_document_writer` 的 `dev` 字段放在基类里，而不是放在各派生结构体里？

> **答案**：因为「当前页借出的 device」是所有格式写入器都需要的公共状态，生命周期由通用的 `fz_begin_page` / `fz_end_page` 包装函数统一管理（见 4.2）。放基类可以让通用层不依赖任何派生类型就能读写它。

**练习 2**：如果一种输出格式根本不需要 device（比如只想直接拷贝字节），它还需要实现 `fz_document_writer` 吗？

> **答案**：仍然可以。`begin_page` 回调里可以返回任意 device（甚至一个什么都不做的空 device），真正干活的是 `end_page`。writer 抽象的价值在于统一的「多页 + 文档」协议，device 只是它复用单页内容接口的手段，而非强制要求每个格式都重度使用 device。

---

### 4.2 多页写入循环：begin_page / run_page / end_page / close

#### 4.2.1 概念说明

上一节讲了 writer 的「底座」和状态机。本节看通用层如何用四个包装函数 `fz_begin_page` / `fz_end_page` / `fz_close_document_writer` / `fz_drop_document_writer` 把这套状态机落地。

核心点是：**通用包装函数做了判空、状态校验和 dev 的托管**，所以你调用 `fz_begin_page`/`fz_end_page` 时不必直接碰虚表，也**不应该自己去 drop 那个 dev**——包装函数会替你管。

特别地，`fz_begin_page` 做了一个重要的状态校验：

> 如果上一页的 `end_page` 还没调用（即 `wri->dev != NULL`），就抛异常 `"called begin page without ending the previous page"`。

这保证了「begin/end 严格配对」这条铁律被强制执行。

#### 4.2.2 核心流程

通用包装函数 `fz_begin_page` / `fz_end_page` 的协作（伪代码）：

```
fz_begin_page(ctx, wri, mediabox):
    if wri->dev != NULL:          # 上一页还没结束
        throw "called begin page without ending the previous page"
    wri->dev = wri->begin_page(ctx, wri, mediabox)   # 借出 dev，存进基类
    return wri->dev               # 调用方拿去 run_page

fz_end_page(ctx, wri):
    dev = wri->dev
    wri->dev = NULL               # 先清空，再回调，保证 begin 能再次进入
    wri->end_page(ctx, wri, dev)  # 派生实现负责 close+drop 这个 dev
```

注意 `end_page` 的顺序细节：**先把 `wri->dev` 置 NULL，再调回调**。这样即便回调内部出错，基类状态也是干净的；同时也意味着回调函数从参数 `dev` 里拿到设备指针，而不是从 `wri->dev` 里拿。

文档级收尾：

```
fz_close_document_writer:   # 调 close_writer 回调（写文件尾），然后置 NULL
fz_drop_document_writer:    # 若未 close 会 warn；drop 掉残留 dev；调 drop_writer；free
```

`fz_drop_document_writer` 里有一句关键警告：如果你没 `close` 就 `drop`，它会打 `"dropping unclosed document writer"` 警告——因为文件可能还没写完尾（比如缺 `%%EOF`），是个半成品。

#### 4.2.3 源码精读

`fz_begin_page` 的状态校验与 dev 托管：

[source/fitz/writer.c:L332-L341](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L332-L341) 判空后，若 `wri->dev` 非空就抛「上一页未结束」异常；否则调用派生的 `begin_page` 回调，把返回的 device 存进 `wri->dev` 并返回。

`fz_end_page` 的「先清空后回调」：

[source/fitz/writer.c:L343-L353](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L343-L353) 取出 `wri->dev`、先把 `wri->dev` 置 `NULL`，再调用派生的 `end_page(ctx, wri, dev)`。dev 的释放责任被移交给派生回调。

`fz_close_document_writer` 与 `fz_drop_document_writer`：

[source/fitz/writer.c:L309-L330](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L309-L330) close 调一次 `close_writer` 回调后把它置 NULL（防止重复 close）；drop 时若发现还没 close 会 `fz_warn`，并兜底 drop 掉残留的 `wri->dev`，再调 `drop_writer`、最后 `fz_free`。

**真值在于工程写法**。看 `muconvert.c`（`mutool convert` 的实现）里逐页写入的核心：

[source/tools/muconvert.c:L109-L111](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/muconvert.c#L109-L111) 这三行就是「多页写入循环」的精髓：`fz_begin_page(ctx, out, box)` 拿到 dev → `fz_run_page(ctx, page, dev, ctm, NULL)` 把源页内容灌进去 → `fz_end_page(ctx, out)` 收尾。注意它**从不直接操作 dev 的引用计数**——dev 完全由 writer 托管。

最后是便捷函数 `fz_write_document`，它把整个循环封装成一句：

[source/fitz/writer.c:L355-L382](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L355-L382) `fz_write_document(ctx, wri, doc)` 自动 `fz_count_pages` 后，对每一页执行 `fz_load_page` → `fz_begin_page(bound_page)` → `fz_run_page(identity)` → `fz_drop_page` → `fz_end_page`。如果你不需要自定义 ctm 或页面裁剪，用它能一行导出整篇文档。

#### 4.2.4 代码实践

**实践目标**：理解 `fz_begin_page` 的状态校验如何强制 begin/end 配对。

**操作步骤**：

1. 阅读上面的 [writer.c:L332-L341](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L332-L341)。
2. 在脑海中（或在本讲 4.3 的示例程序基础上）模拟这样一个错误：连续调用两次 `fz_begin_page` 而中间不调 `fz_end_page`。

**需要观察的现象**：第二次 `fz_begin_page` 会因为 `wri->dev != NULL` 命中 `fz_throw(ctx, FZ_ERROR_ARGUMENT, "called begin page without ending the previous page")`。

**预期结果**：异常被抛出，说明通用层用状态机强制了「一页未结、不开新页」。待本地验证：在你自己的程序里删掉 `fz_end_page` 跑一遍，应能在 stderr 看到这条错误（配合 u2-l3 的 `fz_report_error`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fz_end_page` 要「先把 `wri->dev` 置 NULL，再调回调」，而不是反过来？

> **答案**：两个原因。其一，置 NULL 后即便回调抛异常，基类的 `dev` 状态也是干净的，下次 `begin_page` 不会被误判为「上一页未结束」。其二，把 dev 作为参数显式传给回调，让回调从参数取值而非从基类成员取值，语义更清晰、更不易出错。

**练习 2**：`fz_drop_document_writer` 里为什么要在 `drop_writer` 之前先 `fz_drop_device(ctx, wri->dev)`？

> **答案**：这是兜底。正常流程下 `end_page` 已经 drop 了 dev、`wri->dev` 是 NULL；但如果用户中途异常或忘了 `end_page`，`wri->dev` 可能还指向一个活着的 device。drop 时统一兜底释放，避免内存泄漏。这正是「drop 总是安全的最终回收点」的设计。

---

### 4.3 格式派生与分发：fz_new_document_writer 的格式路由

#### 4.3.1 概念说明

前两节讲了 writer 的抽象骨架。本节回答两个问题：

1. **派生**：一个具体格式（如 SVG）的写入器是怎么写出来的？
2. **分发**：用户调 `fz_new_document_writer(ctx, "out.svg", NULL, NULL)` 时，库怎么知道要造一个 SVG 写入器？

**派生**的套路是固定的（C 手写继承，和 u3-l1 的 document 完全同构）：

```c
typedef struct {
    fz_document_writer super;   // 基类放第一个成员 = 「继承」
    char *path;                 // 派生字段：输出路径模板
    int count;                  // 派生字段：当前页号
    fz_output *out;             // 派生字段：底层字节输出
    fz_svg_device_options opts; // 派生字段：格式选项
} fz_svg_writer;
```

派生类型只多存「这个格式特有的状态」（路径、页计数、底层 `fz_output`、选项）。四个行为通过把 `svg_begin_page` / `svg_end_page` / `svg_drop_writer` 填进基类虚表来注入。

**分发**则是一张「扩展名 → 构造函数」的查表。`fz_new_document_writer` 拿到 `path` 和 `format`，按扩展名逐个 `is_extension` 匹配，命中就调对应格式的 `fz_new_<格式>_writer`。

#### 4.3.2 核心流程

格式分发有两个输入和一个回退策略：

```
fz_new_document_writer(ctx, path, explicit_format, options):
    format = explicit_format ? explicit_format : path 中最后一个 '.'
    while (format != NULL):
        if is_extension(format, "svg"): return fz_new_svg_writer(...)
        if is_extension(format, "pdf"): return fz_new_pdf_writer(...)
        if is_extension(format, "text") or "txt": return fz_new_text_writer(ctx, "text", ...)
        ...（其余十几种格式）
        # 若 format 来自扩展名探测，则回退到倒数第二个 '.' 再试
        format = prev_period(path, format)   # 处理 "a.tar.gz" 这种多扩展名
    throw "cannot detect document format"
```

两个要点：

1. **format 显式优先**：传了 `explicit_format`（如 `"svg"`）就直接用它；没传就从 `path` 的扩展名推断。
2. **多扩展名回退**：`prev_period` 会向左找上一个 `.`，所以 `foo.svg.gz` 这种能逐段尝试。但只要 `format` 是用户显式给的，就不回退（只试一次）。

`is_extension` 本身很宽容：允许带或不带前导 `.`，且大小写不敏感（`fz_strcasecmp`）。

#### 4.3.3 源码精读

派生范例 1——SVG 写入器的结构体与构造：

[source/fitz/output-svg.c:L27-L34](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L27-L34) `fz_svg_writer` 把 `fz_document_writer super` 放在首位实现继承，其余是 SVG 特有字段（path/count/out/opts）。

[source/fitz/output-svg.c:L98-L113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L98-L113) `fz_new_svg_writer` 用 `fz_new_derived_document_writer` 一步分配并填好 `svg_begin_page`/`svg_end_page`/`NULL`(close)/`svg_drop_writer` 四个回调（SVG 没有 close 回调，因为它每页一个文件，无文档级头尾），再解析选项、保存路径模板。

SVG 的 `begin_page`/`end_page` 最能体现「writer 包 device」：

[source/fitz/output-svg.c:L45-L68](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L45-L68) `svg_begin_page` 给每一页按路径模板（如 `out-%04d.svg`）格式化出一个文件名、打开 `fz_output`，然后返回一个 `fz_new_svg_device_with_options(...)` 创建的 **svg device**——这就是被「借出」给 `fz_run_page` 用的画板。

[source/fitz/output-svg.c:L70-L88](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L70-L88) `svg_end_page` 把这个 dev `fz_close_device` 冲刷、`fz_close_output` 落盘，然后在 `fz_always` 里 `fz_drop_device` + `fz_drop_output`——**dev 的释放责任在这里被回收**，呼应 4.2 的「dev 归 writer 管」。

派生范例 2——文本写入器（一个 writer 五种格式）：

[source/fitz/stext-output.c:L1351-L1359](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1351-L1359) `fz_text_writer` 同样以 `super` 起头，多了 `format`（text/html/xhtml/stext.xml/stext.json 五选一）、`page`（暂存的 stext 页）、`out` 等字段。

[source/fitz/stext-output.c:L1361-L1377](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1361-L1377) `text_begin_page` 造一个 `fz_stext_page`，返回一个 `fz_new_stext_device(...)`——文本写入器包的是 stext device（u5-l2）。

[source/fitz/stext-output.c:L1379-L1420](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1379-L1420) `text_end_page` 在 close 掉 dev 后，根据 `wri->format` 用不同的 `fz_print_stext_page_as_*` 把这页 stext 导出成 text/html/xhtml/xml/json 之一。**这就是「同一种 device 树，五种导出 walker」在 writer 层的体现**（承接 u5-l2）。

[source/fitz/stext-output.c:L1422-L1442](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1422-L1442) `text_close_writer` 写各格式的文档尾（HTML 的 `</html>`、JSON 的 `]` 等），呼应「close 负责文档级头尾」。

分发中枢——`fz_new_document_writer` 的扩展名查表：

[source/fitz/writer.c:L147-L223](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L147-L223) 按 `is_extension` 逐个匹配扩展名，命中即返回对应格式的构造函数；`.svg` 走 `fz_new_svg_writer`、`.pdf` 走 `fz_new_pdf_writer`、`.txt`/`.text` 走 `fz_new_text_writer(ctx,"text",...)` 等。注意 `.pdf`、`.ocr`、`.odt`、`.docx` 被各自的 `#if FZ_ENABLE_*` 包裹，可编译期裁剪（见下方说明）；全部不匹配则 `fz_throw(ctx, FZ_ERROR_ARGUMENT, "cannot detect document format")`（[第 222 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L222)）。

关于条件编译裁剪（承接 u1-l2 的 `FZ_ENABLE_*` 机制）：PDF/OCR/ODT/DOCX 这四类输出的表项受宏守卫：

[include/mupdf/fitz/config.h:L244-L254](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L244-L254) `FZ_ENABLE_OCR_OUTPUT` / `FZ_ENABLE_ODT_OUTPUT` / `FZ_ENABLE_DOCX_OUTPUT` 默认为 1，可在编译期置 0 以从分发表里删除对应格式（连同其第三方依赖）。这和 u1-l4 里 mutool 子命令的裁剪是同一套机制。SVG/CBZ/text/PS 等常驻格式则无守卫，始终可用。

#### 4.3.4 代码实践

**实践目标**：用 `fz_new_document_writer` 以 SVG 格式打开输出，把一个输入 PDF 的前 3 页导出为 SVG 文件。这是本讲的主实践。

**操作步骤**：

1. 准备一个测试 PDF（例如仓库自带的 `docs/examples/` 下任意素材，或你手头的任意 PDF）。
2. 编写下面的示例程序 `svgexport.c`（**示例代码**，仿照 `muconvert.c` 与 `example.c` 的写法）：

```c
/* 示例代码：把输入 PDF 的前 3 页导出为 SVG */
#include "mupdf/fitz.h"

int main(int argc, char **argv)
{
    fz_context *ctx;
    fz_document *doc = NULL;
    fz_document_writer *wri = NULL;
    fz_page *page = NULL;
    int i, npages, maxpages;

    if (argc < 3) {
        fprintf(stderr, "usage: %s input.pdf out-%04d.svg\n", argv[0]);
        return 1;
    }

    /* 标准三步（承接 u1-l5）：建 ctx → 注册 handler → 开文档 */
    ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    if (!ctx) { fprintf(stderr, "cannot create ctx\n"); return 1; }

    fz_var(doc); fz_var(page); fz_var(wri);  /* 异常安全，承接 u2-l3 */

    fz_try(ctx) {
        fz_register_document_handlers(ctx);
        doc = fz_open_document(ctx, argv[1]);

        /* 创建 SVG 写入器：路径模板含 %04d，每页一个文件 */
        wri = fz_new_document_writer(ctx, argv[2], NULL, NULL);

        npages = fz_count_pages(ctx, doc);
        maxpages = npages < 3 ? npages : 3;   /* 最多导出前 3 页 */

        for (i = 0; i < maxpages; i++) {
            page = fz_load_page(ctx, doc, i);
            /* 多页写入循环三件套：begin 借出 dev → run 灌内容 → end 回收 dev */
            fz_device *dev = fz_begin_page(ctx, wri, fz_bound_page(ctx, page));
            fz_run_page(ctx, page, dev, fz_identity, NULL);
            fz_end_page(ctx, wri);
            fz_drop_page(ctx, page);
            page = NULL;
        }
        fz_close_document_writer(ctx, wri);  /* 写文档尾，文件至此完整 */
    }
    fz_always(ctx) {
        fz_drop_page(ctx, page);
        fz_drop_document_writer(ctx, wri);   /* 总是 drop，安全回收 */
        fz_drop_document(ctx, doc);
    }
    fz_catch(ctx) {
        fz_report_error(ctx);
        fz_drop_context(ctx);
        return 1;
    }
    fz_drop_context(ctx);
    return 0;
}
```

3. 编译（承接 u1-l2，链接 libmupdf）：

```bash
cc svgexport.c -Iinclude -Lbuild/release -lmupdf -lmupdf-third -lm -o svgexport
```

4. 运行：

```bash
./svgexport input.pdf "page-%04d.svg"
```

**需要观察的现象**：由于 SVG 写入器按路径模板逐页开文件（见 `svg_begin_page` 的 `fz_format_output_path`），你会得到 `page-0001.svg`、`page-0002.svg`、`page-0003.svg` 三个独立 SVG 文件，每个文件包含一页内容。打开任一 SVG 应能看到对应页的矢量内容。

**预期结果**：生成 3 个 SVG 文件；如果输入 PDF 不足 3 页，则按实际页数生成（程序里 `maxpages` 已处理）。验证：用 `mutool convert -o page-%04d.svg input.pdf` 对照命令行产物（它走的正是同样的 `fz_new_document_writer` + 循环）。待本地验证具体渲染内容。

**关于 dev 不要重复 drop**：程序里我们**从不**对 `dev` 调 `fz_drop_device`——它由 `fz_end_page` 内部回收（见 4.2.3 的 `svg_end_page`）。如果你误加 `fz_drop_device(dev)`，会触发二次释放。

#### 4.3.5 小练习与答案

**练习 1**：把上面程序的输出格式从 SVG 改成纯文本（text），需要改哪些地方？

> **答案**：几乎只改输出路径/格式：把 `fz_new_document_writer(ctx, "page-%04d.svg", NULL, NULL)` 改成 `fz_new_document_writer(ctx, "out.txt", NULL, NULL)`（或显式 `fz_new_text_writer(ctx, "text", "out.txt", NULL)`）。由于 text 写入器把多页合并写进同一个 `out`，不再需要 `%04d` 模板，你会得到一个含全部 3 页文本的 `out.txt`。这正体现了 writer 抽象的好处：**调用方代码不变，只换一个格式串**。

**练习 2**：为什么 SVG 写入器的 `close_writer` 回调是 `NULL`，而 text 写入器却实现了 `text_close_writer`？

> **答案**：SVG 写入器**每页一个文件**，每页的头尾在 `svg_begin_page`/`svg_end_page` 里就写完了，没有跨页的文档级头尾，所以不需要 close。text 写入器（尤其 html/xhtml/json/xml）把多页合并写进同一个文件，需要文档级的头（`<!DOCTYPE>`/`[`/`<document>`）和尾（`</html>`/`]`），这些尾必须在所有页写完后由 `close_writer` 写出。这印证了 4.1.1 里「close 负责文档级框架」的分工。

**练习 3**：`fz_new_document_writer(ctx, "out.svg", "pdf", NULL)` 同时给了路径扩展名和显式 format，会导出哪种格式？

> **答案**：导出 **PDF**。因为 [writer.c:L150-L152](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L150-L152) 里 `format = explicit_format` 优先，`path` 只决定写盘文件名，不参与格式判断（且显式 format 不走多扩展名回退，只试一次）。结果是写出一个内容为 PDF、却名为 `out.svg` 的文件——通常是你不想要的，所以一般让二者一致。

## 5. 综合实践

**任务**：写一个「格式中转器」`xport.c`，它能把任意输入格式（PDF/XPS/EPUB……）的指定页面范围导出成用户选择的输出格式，借此把本讲三个模块串起来。

要求：

1. 命令行：`xport <input> <output> [page-range]`，例如 `xport in.epub out.svg 1-3`。
2. 用 `fz_new_document_writer(ctx, output, NULL, NULL)` 让**输出格式由 `output` 的扩展名决定**（承接 4.3 的分发机制）。
3. 用 `fz_parse_page_range`（参考 `muconvert.c` 的 `runrange` 写法）解析页范围，循环 `fz_begin_page`/`fz_run_page`/`fz_end_page`（承接 4.2 的多页循环）。
4. 正确处理 `fz_try/fz_catch`、`fz_var`、以及 page→writer→document→context 的释放顺序（承接 u2-l3、u1-l5）。
5. 跑两组对照实验并记录观察：
   - `xport in.pdf out-%04d.svg 1-5`：得到每页一个 SVG（验证 SVG 写入器每页一文件）。
   - `xport in.pdf out.txt 1-5`：得到一个合并的文本文件（验证 text 写入器多页合一，且需要 `close_writer` 写尾）。

**进阶**：把 ctm 从 `fz_identity` 改成 `fz_scale(2, 2)`（承接 u3-l3），观察 SVG 输出的尺寸/坐标变化；并解释为什么对 text 输出改 ctm 几乎没有可见影响（提示：text 写入器只关心字形与坐标的语义，不关心像素）。

**预期结果**：你能用一句话回答「为什么换一个输出扩展名，调用方代码几乎不用改」——这正是 document writer 抽象的核心价值：把「多页写入协议」与「具体格式」解耦。

## 6. 本讲小结

- `fz_document_writer` 是「写方向」的抽象，本质是 **device + 多页框架 + 文档头尾**；它的 `begin_page` 回调**返回一个 `fz_device *`**，所以被称为「反向 device」——device 接口在读写两个方向上被完全复用。
- 完整生命周期是严格状态机：`new` → 反复 `begin_page`/`run_page`/`end_page` → `close_writer` → `drop_writer`；通用包装函数 `fz_begin_page`/`fz_end_page` 做判空、配对校验（「上一页未结束」异常）并托管 dev。
- **dev 归 writer 管**：`begin_page` 借出、`end_page` 回收，调用方**绝不能**自己 `fz_drop_device` 那个 dev。
- `fz_close_document_writer` 写文档级尾（`%%EOF`、`</html>` 等），不 close 就 drop 会触发 `dropping unclosed document writer` 警告；便捷函数 `fz_write_document` 用 `fz_identity` 一行导出整篇。
- 格式派生是 C 手写继承：派生结构体以 `fz_document_writer super` 起头，用 `fz_new_derived_document_writer` 一步分配并填虚表；SVG writer 包 svg device，text writer 包 stext device。
- 分发中枢 `fz_new_document_writer` 是「扩展名 → 构造函数」查表，显式 format 优先、否则从 path 推断，可处理多扩展名回退；PDF/OCR/ODT/DOCX 受 `FZ_ENABLE_*` 编译期守卫可裁剪。

## 7. 下一步学习建议

- **本单元后续**：u6-l2「muconvert：文档格式转换」会带你完整读 `muconvert.c`，把本讲的 writer 抽象与第三单元的 document 抽象拼成「读入任意格式 → 逐页 run_page → 写入目标格式」的转换范式。建议紧接着学。
- **u6-l3「多格式输出后端」**：对比 SVG/PS/docx/CBZ/PDF 各类后端 `begin_page`/`end_page` 的实现差异，理解「矢量型 / 打印语言型 / 光栅型 / 富文本型」四类输出后端的不同取舍，深化你对 4.3「格式派生」的理解。
- **想自己加一种输出格式**：等学完 u6-l3 与 u10-l3 后，可以参照 `output-svg.c` 的骨架（派生结构体 + 四个回调 + 在 `fz_new_document_writer` 分发表里加一行扩展名匹配）实现一个自定义 writer。
- **配套阅读**：`include/mupdf/fitz/writer.h` 的注释是本讲最权威的 API 文档；`source/tools/muconvert.c` 是最干净的工程范例，值得逐行通读。
