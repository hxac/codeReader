# 多格式输出后端

## 1. 本讲目标

在 u6-l1 中我们认识了 `fz_document_writer` 这个「反向 device」导出抽象，在 u6-l2 中我们用 `muconvert` 跑通了「读入任意格式 → 逐页 `begin_page`/`run_page`/`end_page` → 写出」的通用管线。本讲要回答一个更深入的问题：

**同样都是 document writer，为什么 SVG、PS、CBZ、docx、PDF 这几种输出后端，内部实现差异那么大？**

读完本讲，你应当能够：

1. 用一套「四类取向」框架，把 MuPDF 的所有输出后端归类，并说出每一类的本质取舍。
2. 看懂 `output-svg.c`、`output-ps.c`、`pdf-write.c` 这三个代表性后端的 `begin_page`/`end_page`/`close_writer` 实现，并指出它们各自的「内容记录方式」。
3. 理解一个反直觉但关键的事实：**「打印语言 PS」在 MuPDF 里其实是一个光栅（位图）后端**，而 **PDF 写回根本不走 device 回调翻译**，而是直接把内容录回 PDF 自己的对象模型。
4. 认识 PDF 写入器（`pdf-write.c`）作为「可被其他后端或工具复用的写回能力」的特殊地位。

---

## 2. 前置知识

本讲建立在以下已建立的概念之上（不再重复定义，只做必要衔接）：

- **document writer 虚表四回调**：`begin_page` / `end_page` / `close_writer` / `drop_writer`（见 u6-l1）。`begin_page` 返回一个 `fz_device *`，是「内容进入后端的唯一入口」。
- **派生手法**：派生结构体以 `super`（`fz_document_writer`）打头，用 `fz_new_derived_document_writer` 一次性填好四个回调指针（见 u6-l1）。
- **生命周期状态机**：`new → 反复 begin/run/end → close → drop`，其中 `close` 负责冲刷文档级收尾（如 trailer、总页数、对象树序列化），`drop` 只释放资源（见 u6-l1）。
- **draw device 与 pixmap**：draw device 是把矢量指令光栅化进 `fz_pixmap` 的位图后端（见 u4-l3）。
- **device 虚表**：`fill_path` / `fill_text` / `fill_image` / `clip_*` 等回调是所有后端记录内容的「通用中间语言」（见 u4-l1）。
- **fz_output**：一个面向字节流的输出抽象（文件、缓冲区、zip 条目都可以是一个 `fz_output`），所有后端最终都把字节写进某个 `fz_output`。

一个贯穿全讲的核心直觉：

> **document writer 只规定了「何时开始一页、何时结束一页、何时收尾」的骨架；至于「一页的内容到底用什么形式记录下来」，完全由 `begin_page` 返回的那个 device 决定。** 不同后端的差异，本质就是它们返回的 device 不同——有的是矢量翻译器，有的是光栅化器，有的是语义分析器，有的干脆是 PDF 对象收集器。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [source/fitz/writer.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c) | 分发中枢：`fz_new_document_writer` 按扩展名选后端；通用生命周期函数 `fz_begin_page`/`fz_end_page`/`fz_close_document_writer`/`fz_write_document` |
| [source/fitz/output-svg.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c) | **矢量输出** SVG 后端：每页一个 SVG 文件，包一个 svg device |
| [source/fitz/svg-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/svg-device.c) | SVG device 实现：把 device 回调翻译成 SVG 矢量原语 |
| [source/fitz/output-ps.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c) | **打印语言** PS 后端：光栅化后用 PostScript 图像算子编码，是 band-writer 的典型 |
| [source/fitz/output-cbz.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-cbz.c) | **光栅打包** CBZ 后端：光栅化后把每页 PNG 打进一个 zip（与 PS 同属光栅族，作对比） |
| [source/fitz/output-docx.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-docx.c) | **富文本** docx/odt 后端：借 `extract` 库做版面分析、语义重建（作对比） |
| [source/pdf/pdf-write.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c) | **PDF 写回**：document writer 包装 + `pdf_write_document` 对象树序列化 + 丰富写选项 |
| [include/mupdf/pdf/document.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/document.h) | `pdf_write_options` 结构与 `pdf_default_write_options` 定义 |

---

## 4. 核心概念与源码讲解

### 4.1 四类输出取向与分发总览

#### 4.1.1 概念说明

MuPDF 支持 SVG、PS、PWG、PCL、PCLM、CBZ、PNG/JPEG 等图片族、txt/html/xhtml/stext、docx/odt、PDF 等十几种输出格式。乍看眼花缭乱，但只要看 **`begin_page` 返回的是哪一种 device**，就能把它们归到四类取向里：

| 取向 | 代表后端 | `begin_page` 返回的 device | 记录的是什么 | 何时真正产出文件 |
| --- | --- | --- | --- | --- |
| **① 矢量重发** | SVG | svg device（矢量翻译器） | 把每条 device 指令重新翻译成另一种**矢量原语**（path/text/image 标签） | `end_page` 即写完一页 |
| **② 光栅化后编码** | PS / PWG / PCL / PCLM / CBZ / 图片族 | draw device（光栅化器） | 先把整页**光栅化成 pixmap**，再把像素**编码**进打印语言或图片容器 | `end_page`（单图）或 `close`（多页打包） |
| **③ 语义重建** | docx / odt / html / text | 自定义 docx device（喂 `extract` 库） | 抽取文字 span、路径、图片、结构标签，做**版面分析**重建段落/表格 | `close_writer`（`extract_write`） |
| **④ 对象树写回** | PDF | `pdf_page_write` 返回的 device | 把内容录成**原生 PDF 内容流**，累积进一棵新的 `pdf_document` 对象树 | `close_writer`（`pdf_write_document` 序列化整棵树） |

四类取向的本质区别是 **「保真度 vs. 可编辑性」的取舍**：

- 矢量重发（SVG）：**几何保真**，但文字字体可能不准（除非把文字也转成 path）。
- 光栅化后编码（PS/CBZ）：**像素级绝对保真**，但放大后会糊，且无法再编辑文字。
- 语义重建（docx）：**可编辑性最高**（得到真正的段落/表格），但几何版面是「分析」出来的，不保证像素一致。
- 对象树写回（PDF）：**无损往返**，因为目标格式就是源格式家族，device 录的就是 PDF 自己的内容流操作符。

#### 4.1.2 核心流程

所有后端都遵守同一个 document writer 骨架，差异只集中在三个回调里：

```text
fz_new_document_writer(path/format/options)   ← writer.c 按扩展名分发，选具体后端
        │
        │  对每一页 i：
        ├─ dev = fz_begin_page(wri, mediabox)   ← 【差异点 1】返回哪种 device？
        ├─ fz_run_page(doc, page, dev, ctm)      ← 通用：把页面内容驱动进 dev
        └─ fz_end_page(wri)                       ← 【差异点 2】如何收尾这一页？
        │
fz_close_document_writer(wri)                    ← 【差异点 3】是否需要文档级收尾？
fz_drop_document_writer(wri)                     ← 通用：释放资源
```

三条判断后端「重量」的经验法则：

1. **看 `begin_page` 返回的 device**：svg device = 矢量；draw device = 光栅；自定义 device = 语义；pdf device = 对象树。
2. **看 `close_writer` 是不是 NULL**：NULL（如 SVG）说明每页自包含、无文档级状态；非 NULL（如 PS/PDF/docx/CBZ）说明跨页累积了状态，需要在最后冲刷。
3. **看是否需要 `fz_layout_document`**：只有回流源（EPUB 等）才需要，与输出后端无关（见 u6-l2）。

#### 4.1.3 源码精读

**分发中枢** —— [source/fitz/writer.c:147-223](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L147-L223) 是一张「扩展名 → 构造函数」的查表。显式 `format` 优先于从 `path` 推断；找不到匹配扩展名则抛 `cannot detect document format`：

```c
// source/fitz/writer.c:147-223（节选关键分支）
fz_document_writer *
fz_new_document_writer(fz_context *ctx, const char *path,
                       const char *explicit_format, const char *options)
{
    const char *format = explicit_format;
    if (!format) format = strrchr(path, '.');   // 没给 format 就取路径后缀
    while (format) {
#if FZ_ENABLE_PDF
        if (is_extension(format, "pdf"))  return fz_new_pdf_writer(ctx, path, options);   // ④ 对象树
#endif
        if (is_extension(format, "cbz"))  return fz_new_cbz_writer(ctx, path, options);    // ② 光栅打包
        if (is_extension(format, "svg"))  return fz_new_svg_writer(ctx, path, options);    // ① 矢量
        if (is_extension(format, "ps"))   return fz_new_ps_writer(ctx, path, options);     // ② 光栅打印
        if (is_extension(format, "pwg"))  return fz_new_pwg_writer(ctx, path, options);    // ② 光栅打印
        ...
#if FZ_ENABLE_DOCX_OUTPUT
        if (is_extension(format, "docx")) return fz_new_docx_writer(ctx, path, options);   // ③ 语义
#endif
        ...
    }
    fz_throw(ctx, FZ_ERROR_ARGUMENT, "cannot detect document format");
}
```

注意三个细节：

- PDF 与 docx/odt 受 `FZ_ENABLE_*` 宏守卫可裁剪（见 [source/fitz/writer.c:159-162](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L159-L162) 与 [source/fitz/writer.c:213-216](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L213-L216)）；SVG/PS/CBZ 不受守卫，常驻。
- `is_extension` 会跳过前导 `.` 并大小写不敏感（[source/fitz/writer.c:130-137](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L130-L137)），所以 `.PDF` 与 `.pdf` 等价。
- 当 `format` 来自路径后缀但第一个后缀不匹配时，`prev_period` 会回退到再前一个 `.`（[source/fitz/writer.c:139-145](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L139-L145)），这就是为什么 `foo.stext.json` 这种双后缀也能识别。

**通用生命周期函数**也在此文件，它们对任何后端都适用，因为它们只调虚表：

- [fz_begin_page](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L332-L341)：把 `begin_page` 返回的 device 暂存到 `wri->dev`，并禁止「没 end 就再 begin」。
- [fz_end_page](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L343-L353)：取出 `wri->dev` 置 NULL，再调 `end_page`——这就是 u6-l1 强调的「dev 归 writer 托管，调用方不能自 drop」的实现原因。
- [fz_close_document_writer](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L309-L315)：调一次 `close_writer` 后**把它置 NULL**，保证 close 幂等、不会重复冲刷。
- [fz_write_document](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L355-L382)：便捷函数，对整篇文档循环 `load_page → begin_page → run_page(identity) → end_page`，是 muconvert 主循环的内核（见 u6-l2）。

#### 4.1.4 代码实践

**实践目标**：用同一段内容、同一个工具，分别产出四种取向的输出，直观感受它们的差异。

**操作步骤**：

1. 准备一个多页、含文字与图片的 PDF（仓库自带示例可用 `docs/examples`，或任意 PDF）。
2. 用 `mutool convert`（即 muconvert，见 u6-l2）分别导出四种格式（命令需本地可执行，路径以实际构建产物为准）：

   ```bash
   ./build/debug/mutool convert -o out.svg   input.pdf   # ① 矢量（每页一个 SVG）
   ./build/debug/mutool convert -o out.ps    input.pdf   # ② 光栅打印
   ./build/debug/mutool convert -o out.cbz   input.pdf   # ② 光栅打包（zip）
   ./build/debug/mutool convert -o out.docx  input.pdf   # ③ 富文本（需 FZ_ENABLE_DOCX_OUTPUT）
   ./build/debug/mutool convert -o out.pdf   input.pdf   # ④ 对象树写回
   ```

3. 观察产出：SVG 用文本编辑器打开能看到 `<path>`/`<text>` 标签；PS 里能看到 `image` 算子与 `FlateDecode`；CBZ 解压后是若干 PNG；docx 可用办公软件打开并编辑文字；PDF 仍是 PDF。

**需要观察的现象**：

- SVG 文件大小通常最小（矢量），PS/CBZ 因为光栅化而较大且与分辨率相关。
- docx 的文字可以被选中编辑，而 SVG（若 `text=path`）与 PS 的文字其实是图形，不可编辑。
- 转回 PDF 的文件最接近原文件（无损往返）。

**预期结果**：四类后端各按其取向产出；若 `FZ_ENABLE_DOCX_OUTPUT` 未开启，docx 命令会报 `DOCX writer not enabled`（见 [source/fitz/output-docx.c:882-892](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-docx.c#L882-L892)）。

> 若本地未编译或无示例文档，可改为**源码阅读型实践**：在 [writer.c 的分发表](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L147-L223) 里，把每个扩展名映射到上表四类取向，自制一张完整对照表（含 pcl/pclm/pwg/png/jpeg/txt/html 等）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SVG writer 的 `close_writer` 可以是 `NULL`，而 PS / PDF / docx / CBZ 都不行？

> **答案**：SVG 每页是一个自包含的 SVG 文件，`end_page` 里已经 `fz_close_device` + `fz_close_output` 把这一页写完了，没有任何跨页状态需要在文档末尾冲刷；而 PS 需在末尾写带总页数的 trailer、PDF 需序列化整棵对象树、docx 需调 `extract_write` 生成 docx 包、CBZ 需关闭 zip，这些都要在 `close_writer` 里完成。

**练习 2**：分发表里 `format` 既可能来自 `explicit_format`，也可能来自 `path` 后缀。代码是怎么处理「两者都没给」的？

> **答案**：若 `explicit_format` 为 NULL，则取 `strrchr(path, '.')` 作为后缀；若仍为 NULL（路径无扩展名且未显式指定），`while(format)` 不进入循环，直接抛 `cannot detect document format`（[writer.c:222](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/writer.c#L222)）。

---

### 4.2 矢量输出：SVG

#### 4.2.1 概念说明

SVG 后端是最「纯粹」的矢量重发：它不光栅化任何东西，而是把页面解释器发出的每一条 device 指令（画路径、画文字、画图片、裁剪、蒙版……）**逐条翻译成对应的 SVG 标签**。结果是几何上高度保真、文件通常很小、放大不糊。

它的两个工程特点值得注意：

1. **一页一文件**：SVG 是单页格式，无法在一个 SVG 里放多页。因此 `fz_svg_writer` 用 `count` 计页，每 `begin_page` 用 `fz_format_output_path` 把含 `%04d` 的路径模板展开成 `out-0001.svg`、`out-0002.svg`。
2. **每页自包含**：每页独立 open/close 一个 `fz_output`，所以 `close_writer` 为 NULL（见 4.1.5 练习 1）。

#### 4.2.2 核心流程

```text
fz_new_svg_writer(path,args)
  └─ 解析 svg device 选项(text=path|text, no-reuse-images, resolution)
  └─ path 模板默认 "out-%04d.svg"

对每页：
  svg_begin_page(mediabox)
    ├─ count++ ；按模板展开得到本页文件名
    ├─ fz_new_output_with_path(本页文件名)        ← 新开一个 fz_output
    └─ return fz_new_svg_device_with_options(out,w,h,&opts)   ← ① 矢量翻译器
  fz_run_page(... identity ...)                    ← svg device 把指令翻译成 SVG 标签写入 out
  svg_end_page(dev)
    ├─ fz_close_device(dev)   ← svg device 在 close 时把 <defs> 与主体拼成完整 SVG
    └─ fz_close_output(out)   ← 落盘
```

#### 4.2.3 源码精读

**writer 结构**——[source/fitz/output-svg.c:27-34](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L27-L34)：派生结构体只多了 `path`（模板）、`count`（页号）、`out`（当前页输出）、`opts`（SVG device 选项）。

**begin_page** —— [source/fitz/output-svg.c:45-68](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L45-L68)：核心是最后 `return fz_new_svg_device_with_options(...)`，这就是「矢量取向」的标志——返回一个矢量翻译 device，而不是 draw device。注意它有一条硬约束：若调用方用了 `fz_new_svg_writer_with_output`（即指定了单一 `out` 而非多文件模板），则在第二页直接抛 `cannot write multiple pages to a single SVG output`（[output-svg.c:63-65](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L63-L65)）。

**end_page** —— [source/fitz/output-svg.c:70-88](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L70-L88)：先 `fz_close_device` 再 `fz_close_output`，并在 `fz_always` 里 drop 二者、把 `wri->out` 置 NULL。`close_device` 对 svg device 至关重要——它负责把录制期累积在 `defs` 缓冲里的可复用 `<symbol>`（图片、字形）拼到主体之前，构成合法的完整 SVG。

**构造函数** —— [source/fitz/output-svg.c:98-113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L98-L113)：注意第四个参数（`close_writer`）传的是 `NULL`，印证「每页自包含」。

**svg device 的回调表** —— [source/fitz/svg-device.c:1455-1508](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/svg-device.c#L1455-L1508)：这张表说明「矢量重发」到底重发了什么——`fill_path`/`stroke_path`/`clip_path` → SVG `<path>`；`fill_text` → `<text>` 或 `<path>`（由 `text_as_text` 决定）；`fill_image` → base64 内嵌图片；`begin_group`/`end_group` → `<g>`；`begin_tile`/`end_tile` → `<pattern>`。两个缓冲 `defs`（放可复用定义）与 `main`（放主体）是 svg device 的特色：录制时图片先写进 `defs` 作 `<symbol>`，主体里用 `<use>` 引用，从而去重。

#### 4.2.4 代码实践

**实践目标**：体验 SVG 文字输出的两种取舍——`text=text`（标签可编辑但字体不准）与 `text=path`（几何精确但不可编辑）。

**操作步骤**：

```bash
# text=path（默认倾向，精确）
./build/debug/mutool draw -F svg -O text=path -o out_path.svg   input.pdf 1
# text=text（可编辑，字体近似）
./build/debug/mutool draw -F svg -O text=text -o out_text.svg   input.pdf 1
```

> 注：`mutool draw -F svg` 与 `mutool convert -o x.svg` 最终都走 `fz_new_svg_writer`（见 4.1.3）。`-O` 选项串经 `fz_parse_svg_device_options` 解析（[svg-device.c:1440-1453](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/svg-device.c#L1440-L1453)）。

**需要观察的现象**：用文本编辑器打开两个 SVG——`out_text.svg` 里能看到 `<text>...原文...</text>`，字体由查看器替换；`out_path.svg` 里文字变成了一串 `<path d="...">`，外观与原 PDF 一致但无法选中文字。

**预期结果**：这正是 4.1.1 所说的「矢量重发在文字上的两难」——要么保几何、要么保可编辑性，二者不可兼得（除非嵌入字体子集）。

> 若本地不可运行，改为阅读 [svg-device.c:1455-1508](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/svg-device.c#L1455-L1508) 的回调表，逐一写出每个回调对应的 SVG 标签。

#### 4.2.5 小练习与答案

**练习 1**：SVG 后端为什么必须「一页一文件」？代码在哪里强制这一点？

> **答案**：SVG 是单页格式。`svg_begin_page` 在检测到 `wri->out` 已存在（即走单输出路径）且不是第一页时，抛 `cannot write multiple pages to a single SVG output`（[output-svg.c:63-65](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L63-L65)）；走多文件路径时则用 `fz_format_output_path` 展开模板生成不同文件名（[output-svg.c:56-60](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L56-L60)）。

**练习 2**：svg device 为什么要有 `defs` 和 `main` 两个缓冲？

> **答案**：为了图片/字形的去重重用。录制时把可复用的图片以 `<symbol>` 写进 `defs`，主体 `main` 里用 `<use href="#id">` 引用；`close_device` 时再把 `defs` 拼到 `main` 之前输出完整 SVG。这由 `no-reuse-images` 选项控制（默认开启重用）。

---

### 4.3 打印语言：PS（及光栅打包 CBZ）

#### 4.3.1 概念说明

这是本讲最可能违反直觉的一点：**「打印语言 PS」在 MuPDF 里是一个光栅（位图）后端，不是矢量后端。**

PostScript 本是一门图灵完备的页面描述语言，完全能画矢量。但 MuPDF 的 PS writer 选择了最简单可靠的策略：**先把整页用 draw device 光栅化成一张 pixmap，再把这个位图包进 PostScript 的 `image` 算子里输出**。也就是说，输出的 PS 文件画的是「一张大图」，而不是「一堆矢量指令」。这样做的好处是无需把 device 回调逐一翻译成 PS 算子、字体处理零成本、输出可预测；代价是放大后糊、文件大。

PS 后端与 PWG/PCL/PCLM 同属「光栅化后编码」族——它们都用 draw device 光栅化，差别只在「把像素编码成哪种语言」：PS 用 `image` 算子 + FlateDecode，PWG/PCL/PCLM 用各自的打包格式。**CBZ 也属于这一族**：同样光栅化，只是把每页 PNG 打进一个 zip（Comic Book Zip），连「打印语言」都不算，纯粹是图片打包。

PS 写入器内部用的是 **band-writer**（条带写入器）抽象：它不是 document writer，而是一个更底层的「逐条带把像素流式写出去」的接口。document writer 在 `end_page` 里把整页 pixmap 喂给 band-writer。这条 band-writer 路径正是 u4-l4 讲过的 mudraw 分带渲染的同源机制。

#### 4.3.2 核心流程

```text
fz_new_ps_writer(path,options)
  ├─ 解析 draw 选项(resolution 等) → wri->draw
  ├─ fz_new_output_with_path(path)            ← 单一 .ps 文件，多页累积
  └─ fz_write_ps_file_header(out)             ← 写 %!PS-Adobe-3.0 头

对每页：
  ps_begin_page(mediabox)
    └─ return fz_new_draw_device_with_options(&draw, mediabox, &pixmap)   ← ② 光栅化器！
  fz_run_page(... ctm ...)                     ← draw device 把页面光栅化进 pixmap
  ps_end_page(dev)
    ├─ fz_close_device(dev)
    ├─ bw = fz_new_ps_band_writer(out)         ← 创建底层 band-writer
    ├─ fz_write_header(bw, w,h,n,...)          ← 写 %%Page、setcolorspace、image 字典
    ├─ fz_write_band(bw, stride, h, samples)   ← 像素经 zlib deflate 后写出
    └─ fz_close_band_writer(bw)                ← 写 showpage、%%PageTrailer

ps_close_writer
  ├─ fz_write_ps_file_trailer(out, count)      ← %%Trailer + %%Pages: N + %%EOF
  └─ fz_close_output(out)
```

PS 的「打印」本质还体现在分辨率换算上。pixmap 是按设备分辨率（如 300 dpi）光栅化的，但 PostScript 的页面尺寸以**点**（point，1/72 英寸）为单位。所以 header 里要把像素尺寸换算回点：

\[
w_{\text{points}} = \frac{w_{\text{px}} \times 72 + \tfrac{1}{2}\,\text{xres}}{\text{xres}}, \qquad
s_x = \frac{w_{\text{px}}}{w_{\text{points}}}
\]

其中 \(s_x\) 写进 `ImageMatrix`，让 PS 解释器把位图缩放回正确的物理尺寸。

#### 4.3.3 源码精读

**writer 结构** —— [source/fitz/output-ps.c:323-330](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L323-L330)：注意它持有 `fz_draw_options draw` 和 `fz_pixmap *pixmap`——这两个字段就是「光栅取向」的铁证（SVG writer 既没有 draw 选项也没有 pixmap）。

**begin_page** —— [source/fitz/output-ps.c:332-338](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L332-L338)：只有一行实质代码 `return fz_new_draw_device_with_options(ctx, &wri->draw, mediabox, &wri->pixmap)`——与 SVG 的 `fz_new_svg_device_with_options` 形成鲜明对照。draw device 把 pixmap 地址 `&wri->pixmap` 交给它填充，光栅化结果就存在 writer 的 pixmap 字段里。

**end_page** —— [source/fitz/output-ps.c:340-366](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L340-L366)：这是「光栅化后编码」的典型范式——`close_device` 之后，立刻 `fz_new_ps_band_writer` 创建 band-writer，再 `fz_write_header` / `fz_write_band` / `fz_close_band_writer` 三步把整页像素走一遍。最后在 `fz_always` 里 drop pixmap（`wri->pixmap = NULL`），为下一页清空。

**band-writer 的 header** —— [source/fitz/output-ps.c:70-141](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L70-L141)：这段写出了 `%%Page`、`%%PageBoundingBox`、`setpagedevice`、`/DataFile currentfile /FlateDecode filter`、按通道数 `setcolorspace`（Gray/RGB/CMYK），最后写一个 `image` 字典算子。注意它明确禁止专色（`s != 0` 抛错，[output-ps.c:88-89](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L88-L89)）和 alpha（[output-ps.c:91-92](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L91-L92)）——PS Level 2 图像不支持这两者。

**band 的压缩写出** —— [source/fitz/output-ps.c:216-307](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L216-L307)：`ps_write_band` 把像素按行重排进输入缓冲，再用 zlib `deflate` 压缩、流式写到 `out`。这正是 u8（流与压缩）会深入讲解的 Flate 编码，这里它是「打印语言」的内部实现细节。

**文件头/尾** —— [fz_write_ps_file_header](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L41-L62) 在构造 writer 时就写好 `%!PS-Adobe-3.0` 头；[fz_write_ps_file_trailer](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L64-L68) 在 `ps_close_writer` 里写 `%%Pages: N`——这个 N 只有全部页处理完才知道，这正是 PS 必须 `close_writer` 的根因。

**对照：CBZ（光栅打包）** —— [source/fitz/output-cbz.c:38-78](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-cbz.c#L38-L78)：`cbz_begin_page` 同样返回 draw device（[output-cbz.c:38-43](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-cbz.c#L38-L43)），证实它与 PS 同族；区别在 `cbz_end_page`（[output-cbz.c:45-71](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-cbz.c#L45-L71)）把 pixmap 编码成 PNG buffer 再 `fz_write_zip_entry` 打进 zip，`cbz_close_writer`（[output-cbz.c:73-78](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-cbz.c#L73-L78)）关闭 zip。换句话说：**PS 与 CBZ 共享「draw device 光栅化」这一半，只在「像素编码成什么」这一半分道**。

#### 4.3.4 代码实践

**实践目标**：验证 PS 输出确实是「一张位图」而非矢量，并对比它与 CBZ 同源、与 SVG 异构。

**操作步骤**：

```bash
# 同一页分别导出 PS 与 SVG
./build/debug/mutool draw -F ps  -r 150 -o out.ps  input.pdf 1
./build/debug/mutool draw -F svg -o out.svg input.pdf 1
```

**需要观察的现象**：

1. 用文本编辑器看 `out.ps`：能看到 `%%Page`、`/DeviceRGB setcolorspace`、一个 `image` 字典，以及一大段二进制（FlateDecode 压缩的像素）。找不到任何描述原文字形的矢量指令。
2. 改 `-r 300` 重新生成 PS，文件显著变大——证明 PS 大小随分辨率线性增长（位图特征）。而 SVG 文件大小与分辨率无关（矢量特征）。

**预期结果**：PS 文件大小 ∝ 分辨率²，SVG 文件大小与分辨率无关。这正是 4.3.1「PS 是光栅后端」的直接证据。

> 源码阅读型替代：对比 [output-ps.c:332-338](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L332-L338) 与 [output-svg.c:45-68](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L45-L68) 的 `begin_page` 返回值，一句话总结二者取向差异。

#### 4.3.5 小练习与答案

**练习 1**：PS writer 的 `begin_page` 返回 draw device，这对输出质量意味着什么？

> **答案**：意味着整页被光栅化为一张位图再编码进 PS。结果是放大后会出现马赛克（位图特征），文件大小正比于分辨率平方；但字体处理零成本、输出可预测、不依赖 PS 解释器的字体能力。这是「保真但不可编辑、且体积大」的取舍。

**练习 2**：PS 与 CBZ 都属于光栅族，它们在代码结构上最像的是哪一段？最不像的是哪一段？

> **答案**：最像 `begin_page`——两者都 `return fz_new_draw_device_with_options(...)` 光栅化（[output-ps.c:337](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c#L337) vs [output-cbz.c:42](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-cbz.c#L42)）。最不像 `end_page` 的编码方式——PS 用 `fz_new_ps_band_writer` 把像素经 FlateDecode 写成 PS `image` 算子，CBZ 用 `fz_new_buffer_from_pixmap_as_png` 把像素编码成 PNG 再 `fz_write_zip_entry` 打进 zip。

---

### 4.4 PDF 写回：pdf-write.c

#### 4.4.1 概念说明

PDF 写回是四类取向里最特殊的一种——**它根本不把 device 回调翻译成「另一种格式」，而是把内容录回 PDF 自己的对象模型**。

回想一下：SVG/PS/docx 都是把页面指令「翻译」成别的某种表示。而 PDF writer 的 `begin_page` 返回的是 `pdf_page_write` 产生的 device——这个 device 接收到 `fill_path`/`fill_text` 等回调时，**直接把它们翻译回 PDF 的内容流操作符**（如 `re`、`Tj`），累积进一棵新建的 `pdf_document` 对象树。也就是说，PDF writer 把 device 当成了「PDF 内容流录制器」，目标格式和源格式是同一种。

正因为如此，PDF 写回是**无损往返**：PDF → device → PDF，中间没有跨格式的信息损失。这也使 `pdf-write.c` 具备了超越「输出后端」的地位——它还是 `mutool clean`、`mutool merge`、增量保存、加密、垃圾回收、快照等所有「改写 PDF」操作的共同底座，是一个**可被其他工具复用的写回能力**。

PDF 写回的另一个重点是它有**所有后端里最丰富的写选项**（`pdf_write_options`）：压缩、垃圾回收、对象重编号、去重、加密、增量保存、清理内容流、对象流（objstms）等。

#### 4.4.2 核心流程

PDF writer 分两层：

```text
外层：document writer 包装（让 PDF 也能进 fz_new_document_writer 分发表）
fz_new_pdf_writer(path,options)
  ├─ pdf_parse_write_options(&opts, options)     ← 解析 -O 选项串
  ├─ wri->pdf = pdf_create_document(ctx)         ← 建一棵空 PDF 对象树
  └─ wri->out = fz_new_output_with_path(path)

对每页：
  pdf_writer_begin_page(mediabox)
    └─ return pdf_page_write(pdf, mediabox, &resources, &contents)   ← ④ PDF 内容流录制 device
  fz_run_page(... identity ...)
  pdf_writer_end_page(dev)
    ├─ fz_close_device(dev)
    ├─ obj = pdf_add_page(pdf, mediabox, 0, resources, contents)     ← 造一个 page 对象
    └─ pdf_insert_page(pdf, -1, obj)                                ← 追加到页树末尾

pdf_writer_close_writer
  └─ pdf_write_document(pdf, out, &opts)    ← 序列化整棵对象树（内层）
  └─ fz_close_output(out)

内层：pdf_write_document（对象树 → 字节流）
  ├─ prepare_for_save：清理/消毒内容流、生成标注外观（按选项）
  ├─ 遍历对象：垃圾回收、压缩流、重编号、（可选）加密
  └─ 写 xref + trailer
```

#### 4.4.3 源码精读

**writer 结构** —— [source/pdf/pdf-write.c:3146-3156](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L3146-L3156)：除了 `super`，它持有 `pdf_document *pdf`（正在累积的对象树）、`pdf_write_options opts`、`fz_output *out`、以及每页的 `resources` 与 `contents`。注意它**没有 pixmap、没有 draw 选项**——与 PS writer 形成对照，证明 PDF 不光栅化。

**begin_page** —— [source/pdf/pdf-write.c:3158-3164](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L3158-L3164)：`return pdf_page_write(ctx, wri->pdf, wri->mediabox, &wri->resources, &wri->contents)`。这是「对象树取向」的标志——返回的 device 把指令录成 PDF 内容流写进 `contents` buffer，资源（字体/图片）挂到 `resources`。

**end_page** —— [source/pdf/pdf-write.c:3166-3191](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L3166-L3191)：`pdf_add_page` 把 `mediabox + resources + contents` 打包成一个 page 对象，`pdf_insert_page(pdf, -1, obj)` 追加到页树末尾。`fz_always` 里 drop 这页临时资源——对象树里已持有引用。

**close_writer** —— [source/pdf/pdf-write.c:3193-3199](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L3193-L3199)：核心就一行 `pdf_write_document(ctx, wri->pdf, wri->out, &wri->opts)`——把累积的整棵对象树序列化成字节流。这是 PDF 写回与其他后端最大的不同：**真正的「写文件」被推迟到 close 这一刻一次性完成**（因为要重排对象编号、写 xref）。

**构造函数** —— [source/pdf/pdf-write.c:3211-3242](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L3211-L3242)：先 `pdf_parse_write_options` 解析选项串，再 `pdf_create_document` 建空树。

**写选项** —— [include/mupdf/pdf/document.h:767-791](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/document.h) 定义了 `pdf_write_options`，关键开关：`do_garbage`（1=gc/2=重编号/3=去重）、`do_compress`（1=zlib/2=brotli）、`do_compress_fonts`/`do_compress_images`、`do_clean`/`do_sanitize`（清理/消毒内容流）、`do_encrypt`（加密方法）、`do_incremental`（只写变化对象）、`do_use_objstms`（对象流）。这些选项由 [pdf_apply_write_options](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2047-L2113) 从 `fz_options` 字符串解析——正是 u6-l2 讲过的「选项串经 `fz_new_options` + `fz_lookup_option_*` 解析」范式。

**序列化入口** —— [pdf_write_document](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2889-L2933) 是内层核心：先做一系列合法性校验（增量保存不能用于修复过的文件、不能既增量又改加密、linearize 已不再支持等——见 [output 2890-2924](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2890-L2924)），再 `prepare_for_save`，最后 `do_pdf_save_document` 真正落盘。另一入口 [pdf_save_document](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2935-L3018) 面向文件名，会在保存前按 `do_appearance` 重新生成标注外观（[2973-2999](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2973-L2999)）。

> **复用地位**：`mutool clean` 内部就是调 `pdf_save_document`（带 `do_clean`/`do_garbage` 等选项）来重写 PDF——这正是 u7-l3 会讲到的「pdf-write.c 把对象树重新序列化」的实战入口。换言之，PDF 写回既是输出后端，也是 PDF 二次开发的写回底座。

#### 4.4.4 代码实践

**实践目标**：用 PDF 写回的选项做一次「清理 + 垃圾回收」，体会 `pdf-write.c` 作为「写回能力」被工具复用。

**操作步骤**：

```bash
# mutool clean 内部走 pdf_write_document（do_clean + do_garbage）
./build/debug/mutool clean -g -d input.pdf cleaned.pdf
#            -g = garbage collect(并重编号)   -d = decompress(便于观察)
ls -l input.pdf cleaned.pdf
```

> `-g`、`-d` 等命令行标志与 `pdf_write_options` 字段的对应关系见 [include/mupdf/pdf/document.h:795-798](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/document.h) 注释（`g: garbage collect`，`d: expand all` 等）。

**需要观察的现象**：清理后的 `cleaned.pdf` 通常比原文件小（去除了未引用对象、重编号紧凑）；若加了 `-d`，用 `mutool show cleaned.pdf` 看到的对象是未压缩的明文，便于阅读对象树。

**预期结果**：文件变小、对象被整理——这就是 `pdf-write.c` 在「清理」场景下的复用。这与本讲其他后端（SVG/PS/docx）「把页面翻译成别种格式」的定位截然不同：**PDF 写回是「把 PDF 对象树重新序列化为更干净的 PDF」**。

> 若本地不可运行，改为阅读 [pdf_writer_close_writer](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L3193-L3199)，确认它调用的是 `pdf_write_document`（序列化对象树），而非像 SVG/PS 那样在 end_page 逐页写文件。

#### 4.4.5 小练习与答案

**练习 1**：为什么 PDF writer 把「真正的写文件」放在 `close_writer`，而不是像 SVG 那样在 `end_page` 里写？

> **答案**：PDF 的对象编号要在整篇所有页都确定后才能重排，xref 表也要在知道所有对象偏移后才能写。`end_page` 只是往对象树里追加一个 page 对象（`pdf_insert_page`），并不落盘；只有 `close_writer` 调 `pdf_write_document` 时才一次性遍历整棵树、重编号、写 xref + trailer。SVG 每页自包含，无此跨页依赖，故可逐页写。

**练习 2**：`pdf_write_document` 对 `do_incremental` + `do_garbage` 的组合会怎样处理？为什么？

> **答案**：直接抛错 `Can't do incremental writes with garbage collection`（[pdf-write.c:2902-2903](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2902-L2903)）。因为增量保存只追加变化的对象、保持原对象编号不变，而垃圾回收需要重排对象编号、删除未引用对象——两者逻辑互斥。

---

## 5. 综合实践

**任务**：选定两个取向不同的后端（推荐 **SVG（矢量重发）** 与 **docx（语义重建）**），精读它们的 `begin_page`/`end_page` 实现，写一份「矢量型 vs 富文本型」取舍对比报告。

**步骤**：

1. **读 SVG 后端**：[output-svg.c:45-88](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-svg.c#L45-L88) 的 `svg_begin_page`/`svg_end_page`，以及 [svg-device.c:1455-1508](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/svg-device.c#L1455-L1508) 的回调表。确认：它 `begin_page` 返回 **svg device（矢量翻译器）**，`end_page` 关设备关输出即落盘，`close_writer` 为 **NULL**。
2. **读 docx 后端**：[output-docx.c:568-627](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-docx.c#L568-L627) 的 `writer_begin_page`/`writer_end_page`，以及 [output-docx.c:66-138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-docx.c#L66-L138) 的 `dev_text`，再看 [output-docx.c:662-711](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-docx.c#L662-L711) 的 `writer_close`。确认：它 `begin_page` 创建**自定义 docx device**（`fill_text`/`fill_path`/`fill_image`/`begin_structure` 喂给 `extract` 库），`end_page` 调 `extract_process` 做版面分析，真正的 docx 在 `close_writer` 里由 `extract_write` 一次性生成。
3. **产出对比表**（建议字段）：

   | 维度 | SVG（矢量型） | docx（富文本型） |
   | --- | --- | --- |
   | `begin_page` 返回 | svg device | 自定义 docx device |
   | 记录的内容 | 矢量原语（path/text/image 标签） | 文字 span + 坐标 + 结构标签（喂 extract） |
   | 文字保真 | 几何精确（`text=path`）或字体近似（`text=text`） | 字符准确，可编辑，版面靠分析 |
   | 何时产出文件 | `end_page` 逐页 | `close_writer` 一次性 |
   | `close_writer` | NULL | 非 NULL（`extract_write`） |
   | 核心取舍 | 保几何、失可编辑 | 保可编辑、失像素一致 |

4. **结论**：用一段话回答——「为什么同样接收 device 回调，SVG 选择逐条翻译原语，而 docx 选择把文字和结构喂给一个版面分析库？」（提示：目标用途不同——SVG 服务于「高保真展示」，docx 服务于「可编辑文档」。）

> 若本地可编译运行，额外用 `mutool convert` 把同一 PDF 分别导出为 SVG 与 docx，打开两份产物验证上表结论（SVG 文字不可编辑但版面一致；docx 文字可编辑）。

---

## 6. 本讲小结

- MuPDF 的输出后端可按「`begin_page` 返回哪种 device」归为四类取向：**矢量重发（SVG）**、**光栅化后编码（PS/PWG/PCL/CBZ/图片族）**、**语义重建（docx/odt）**、**对象树写回（PDF）**。
- **SVG** 每页一个文件、`begin_page` 返回 svg device 把指令翻译成矢量标签、`close_writer` 为 NULL；在文字上面临「保几何（`text=path`）还是保可编辑（`text=text`）」的两难。
- **PS 是光栅后端，不是矢量后端**：`begin_page` 返回 draw device 把整页光栅化成 pixmap，`end_page` 用 band-writer 把像素经 FlateDecode 编码进 PostScript `image` 算子；CBZ 与之同族（都光栅化），只在「像素编码成什么」上分道。
- **PDF 写回不翻译格式**：`begin_page` 返回 `pdf_page_write` 的 device，把内容录成**原生 PDF 内容流**累积进一棵 `pdf_document` 对象树，`close_writer` 调 `pdf_write_document` 一次性序列化；这是无损往返。
- `pdf-write.c` 不止是输出后端，更是 `mutool clean`/`merge`/增量保存/加密/快照等所有「改写 PDF」操作的**共同写回底座**，拥有最丰富的 `pdf_write_options`（压缩/垃圾回收/去重/加密/增量/清理等）。
- 判断任一后端「重量」的三条法则：看 `begin_page` 返回的 device、看 `close_writer` 是否为 NULL、看输出是否依赖分辨率。

---

## 7. 下一步学习建议

- **深入 PDF 对象模型**：本讲的 PDF 写回依赖 `pdf_page_write`/`pdf_add_page`/`pdf_insert_page`，要真正理解它「录成内容流」的过程，需先掌握 `pdf_obj` 七种类型与 xref 间接对象——这正是 **u7-l1（pdf_obj 对象类型系统）** 与 **u7-l2（xref 交叉引用表）** 的内容。
- **理解内容流如何被解释与生成**：`pdf_page_write` 的 device 把回调翻译成 PDF 操作符，而 **u7-l4（资源、页面与内容流解释）** 会从反方向讲 `pdf-op-run.c` 如何把操作符解释成 device 回调；二者对照阅读，能完整理解「device 回调 ↔ PDF 操作符」的双向映射。
- **流与压缩的底层**：PS 后端的 FlateDecode、PDF 写回的 `do_compress` 都建立在 filter 管线之上——**u8-l1（流与过滤管线）** 与 **u8-l2（压缩编解码器）** 会讲清 zlib/deflate 如何被封装成 filter。
- **扩展实践**：学完 u7 后，可尝试用 `pdf_save_document` 配合不同 `pdf_write_options` 写一个小工具，对一份 PDF 做「加密 + 垃圾回收 + 去重」，对比前后对象数量与文件大小，把本讲的 PDF 写回与 u7 的对象模型串起来。
