# 字体加载与字形缓存

> 讲义 id：`u5-l1`　所属单元：u5 字体、文本与搜索　阶段：intermediate　依赖：u4-l3（draw device 与 pixmap 位图渲染）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `fz_font` 这个“字体句柄”封装了什么：它如何把 FreeType（光栅化）与 HarfBuzz（文字整形）两套第三方库收敛进一个统一对象，并区分 FreeType 字体与 Type3 字体两种变体。
- 解释 MuPDF 为什么“零外部字体文件也能渲染”，以及 base14 / Noto / CJK 内置字体与系统字体钩子各自的定位。
- 准确描述**字形缓存（glyph cache）**：它缓存的是什么、用什么作 key、命中与未命中分别发生什么，以及它和 `fz_store` 的关系。
- 动手写一段最小代码，观察字形缓存对“大量重复字符”渲染的加速作用。

> ⚠️ 一个必须先纠正的误解：本讲的实践任务原文写的是“通过 **store 上限**间接影响字形缓存”。这在当前源码里**不成立**——字形缓存有自己的 1 MiB 硬上限（`MAX_CACHE_SIZE`），与 `fz_new_context(max_store)` 的 store 上限**互相独立**。本讲会讲清楚真实机制，并把实践改为用 `fz_purge_glyph_cache` 来对照。

## 2. 前置知识

本讲建立在前面几讲已经建立的概念之上，这里只做最小回顾：

- **`fz_context` 与引用计数**（u2-l1、u2-l2）：几乎所有 fitz 调用的首参是 `ctx`；对象的“借走/归还”靠 `fz_keep_*` / `fz_drop_*` 配对。本讲会反复用到这套约定。
- **`fz_storable` 与“裸计数”两种生命周期**（u2-l2）：有些对象用 `fz_storable` 头部走通用计数；有些对象（如 `fz_font`）用普通的 `int refs` 字段配 `fz_keep_imp` / `fz_drop_imp`。两者要分清。
- **draw device 与 pixmap**（u4-l3）：draw device 把矢量绘图指令（`fill_path` / `fill_text` / `fill_image`）光栅化进 `fz_pixmap`。本讲的字形缓存，正是 draw device 在画文字时反复调用的那一层。
- **device 虚表**（u4-l1）：`fz_fill_text` 是 device 的一个回调，文字渲染从这里进入。

一个直觉：**画一页文字，等于把几千个“小位图（字形）”按位置贴到画布上**。每个字形如果都要现算（交给 FreeType 光栅化），就太慢了。本讲的两条主线——**字体加载**（得到字形数据）与**字形缓存**（把算好的字形位图存起来复用）——正是为了让这件事又快又省。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/font.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h) | `fz_font` 与字体相关 API 的公共契约：字体创建、查询、内置字体查找、系统字体钩子。 |
| [source/fitz/font.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c) | `fz_font` 的实现：结构初始化、引用计数、FreeType 集成、内置字体构造、字形光栅化。 |
| [source/fitz/font-table.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font-table.h) | 内置字体登记表（URW Nimbus 与 Noto 各字体的名字/字形数据来源）。 |
| [include/mupdf/fitz/glyph-cache.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/glyph-cache.h) | 字形缓存对外的少量 API（清空、调试统计等）。 |
| [source/fitz/draw-glyph.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c) | **字形缓存的真正实现**：哈希表 + LRU、`fz_render_glyph` 的命中/渲染/入缓存流程。 |
| [source/fitz/glyph.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/glyph.c) | 缓存值 `fz_glyph`：把字形位图做 RLE 压缩或直接持有 pixmap。 |
| [source/fitz/glyph-imp.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/glyph-imp.h) | `fz_glyph` 结构与内部字形渲染函数声明。 |
| [source/fitz/harfbuzz.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/harfbuzz.c) | HarfBuzz 的内存与锁适配（把第三方库的分配/并发收编进 `fz_context`）。 |
| [source/fitz/draw-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c) | draw device 画文字时如何逐字形调用 `fz_render_glyph`，是缓存的“客户”。 |

> 顺带一提：`glyph-cache.h` 里那个 `fz_render_glyph_pixmap`，注释明确写着“**正常渲染已不再使用它**，仅为 app 保留，有被移除风险”。真正用于渲染的入口在 `draw-glyph.c` 里的 `fz_render_glyph`，别认错文件。

---

## 4. 核心概念与源码讲解

### 4.1 fz_font 封装：一个字体句柄装下两套库

#### 4.1.1 概念说明

“字体”在不同格式里长得完全不一样：PDF 里可能是 Type1/CFF/TrueType/CID，XPS、EPUB、图片 OCR 又各有各的来源。MuPDF 的做法是用一个统一句柄 [fz_font](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L106) 把它们都收编，上层（device、文本抽取、搜索）只认 `fz_font`，不关心来源。

`fz_font` 对外只暴露前向声明（不透明指针），内部却同时挂着两套第三方库：

- **光栅化库 FreeType**：负责把字形的矢量轮廓变成像素位图。绝大多数字体走这条路，字段是 `ft_face`。
- **整形库 HarfBuzz**：负责复杂脚本的“文字整形”（决定用哪些字形、怎么排布，如阿拉伯文连写、天城文合字）。通过字段 `shaper_data` 挂接。

另外有一类特殊的 **Type3 字体**：它不是真正的字体文件，而是 PDF 里用绘图指令“画”出来的字形（每个字形是一小段内容流），所以 `fz_font` 也保留了 `t3*` 一组字段来回调 PDF 解释器。

头文件注释把这两种变体说得很直白：

> Fonts come in two variants: Regular fonts are handled by FreeType. Type 3 fonts have callbacks to the interpreter. —— [font.h:L109-L112](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L109-L112)

#### 4.1.2 核心流程

一个 `fz_font` 的生命周期：

1. **创建**：通过某个 `fz_new_*_font` 工厂函数分配 `fz_font` 结构、置 `refs=1`、按来源填好 `ft_face`（FreeType 字体）或 `t3*`（Type3）字段。
2. **使用**：上层 `fz_keep_font` 借引用、用完 `fz_drop_font` 归还；可被多处共享。
3. **销毁**：当 `refs` 归零，`fz_drop_font` 释放它持有的全部下级资源（FreeType face、字体文件 buffer、各种缓存表、shaper 句柄）。

`fz_font` 用的是**普通 `int refs` + `fz_keep_imp`/`fz_drop_imp`**，而不是 `fz_storable` 头部——这一点和 u2-l2 讲的 `fz_pixmap`（走 storable）不同，不要混淆。

#### 4.1.3 源码精读

先看 `fz_font` 结构本体（只列关键字段）：

[font.h:L775-L827](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L775-L827) —— `struct fz_font`。要点：

- `int refs`：手动引用计数（不是 storable）。
- `char name[32]` / `family[32]`：字体名与家族名。
- `fz_buffer *buffer`：字体文件的原始字节（创建时持有，供嵌入/子集化时回读）。
- `fz_font_flags_t flags`：粗体/斜体/衬线/等宽/CJK/是否可嵌入等位标志（[font.h:L144-L163](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L144-L163)）。
- `void *ft_face`：FreeType 的 `FT_Face`（以 `void*` 暴露，避免污染公共头）；见 [font.h:L784](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L784)。
- `fz_shaper_data_t shaper_data`：HarfBuzz 等整形库的句柄与析构回调（[font.h:L785](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L785)，类型见 [font.h:L185-L189](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L185-L189)）。
- `t3matrix / t3procs / t3lists / t3widths / t3flags / t3doc / t3run`：Type3 字体专用，回调 PDF 解释器（[font.h:L787-L796](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L787-L796)）。
- 一组缓存表：`bbox_table`（每字形包围盒）、`width_table`（替换度量）、`advance_cache`（字形推进量）、`encoding_cache`（unicode→gid 查表）、`digest`（字体数据 MD5，用于缓存去重）。

引用计数实现非常薄：

[font.c:L146-L150](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L146-L150) —— `fz_keep_font` 只是 `fz_keep_imp(ctx, font, &font->refs)`。

[font.c:L200-L252](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L200-L252) —— `fz_drop_font`：归零后才真正释放，且**逆序**释放下级资源——先清 Type3 资源、释放 `t3lists/widths/flags`，再在 `FZ_LOCK_FREETYPE` 下 `FT_Done_Face` 销毁 FreeType face（[font.c:L218-L226](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L218-L226)），释放编码/包围盒/推进量缓存表，调用 `shaper_data.destroy` 销毁 HarfBuzz 句柄（[font.c:L247-L250](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L247-L250)），最后 `fz_free` 自身。

工厂函数有一组，按“来源”区分：

| 工厂函数 | 来源 | 位置 |
| --- | --- | --- |
| `fz_new_base14_font(ctx, name)` | PDF 标准 14 字体之一（按名字） | [font.c:L918-L941](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L918-L941) |
| `fz_new_builtin_font(ctx, name, bold, italic)` | 任意内置字体（按名字+样式） | [font.c:L970-L985](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L970-L985) |
| `fz_new_cjk_font(ctx, ordering)` | 内置 CJK 字体 | [font.c:L943-L968](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L943-L968) |
| `fz_new_font_from_file(ctx, name, path, index, use_glyph_bbox)` | 磁盘文件 | [font.h:L518](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L518) |
| `fz_new_font_from_buffer(ctx, name, buffer, index, use_glyph_bbox)` | 内存 `fz_buffer` | [font.h:L501](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L501) |
| `fz_new_font_from_memory(ctx, name, data, len, index, use_glyph_bbox)` | 裸内存指针 | [font.h:L484](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L484) |

它们最终都汇聚到内部构造器 [fz_new_font](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L92-L144)，由它 `fz_malloc_struct`、置 `refs=1`、初始化各字段为安全初值。

> ⚠️ **关于 `fz_new_font_from_name`**：本讲实践任务原文用到这个名字，但**当前源码里不存在该函数**（在头文件与 `font.c` 中都搜不到）。“按名字加载字体”在 MuPDF 里的真实入口是上表的 `fz_new_base14_font` 与 `fz_new_builtin_font`。后文的实践会使用真实函数。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认“每个 `fz_keep_font`/初始 `refs=1` 必须配一个 `fz_drop_font`”，并看清 `fz_font` 用的是裸计数而非 storable。
2. **步骤**：
   - 打开 [font.c:L146-L150](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L146-L150) 与 [font.c:L200-L252](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L200-L252)。
   - 对比 u2-l2 里 `fz_pixmap` 的 keep/drop（走 `fz_keep_storable`/`fz_drop_storable`）。找出 `fz_font` 为何不这么做（提示：它不是缓存进 `fz_store` 的通用可存储对象，生命周期由文档/字体上下文直接管理）。
3. **现象/预期**：`fz_drop_font` 只在 `fz_drop_imp` 返回非零（计数归零）时才执行释放体；因此对一个被多处 keep 的字体，drop 只是“减一”，不会过早释放 FreeType face。
4. 结论：`fz_font` 是“裸 `int refs` + imp”范式的典型样本。

#### 4.1.5 小练习与答案

- **Q1**：`fz_font` 为什么用 `void *ft_face` 而不是直接写 `FT_Face ft_face`？
  - **A**：为了不让 FreeType 的内部头文件（`ft2build.h` 等）泄漏进公共头 `font.h`。需要真正类型时，用 [fz_font_ft_face](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L124) 取出并自行转型。
- **Q2**：`fz_new_base14_font(ctx, "Helvetica")` 第二次调用时，会重新读字体数据建 face 吗？
  - **A**：不会。见 [font.c:L926-L927](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L926-L927)：若 `ctx->font->base14[x]` 已缓存，直接 `fz_keep_font` 返回已有实例。

---

### 4.2 FreeType 集成与字体上下文（font context）

#### 4.2.1 概念说明

FreeType 是 C 库，有两个特点直接决定了 MuPDF 的封装方式：

1. **一个进程通常只建一个 `FT_Library`**，所有 `FT_Face` 都从它派生。
2. **FreeType 不是线程安全的**，多线程访问必须由调用方加锁。

为此 MuPDF 把“所有字体共享的全局状态”收进一个 **字体上下文 `fz_font_context`**（挂在 `ctx->font`），由它持有唯一的 `FT_Library`、FreeType 的自定义分配器记录、系统字体钩子，以及一组内置字体的缓存实例。

#### 4.2.2 核心流程

字体上下文的创建与共享：

1. `fz_new_context` 的第二阶段会调 [fz_new_font_context](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L377-L388)：分配 `fz_font_context`、`ctx_refs=1`、把 FreeType 内存回调接到 MuPDF 的分配器（`ft_alloc/ft_free/ft_realloc`）。
2. `fz_clone_context`（多线程）通过 [fz_keep_font_context](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L390-L396) 共享同一个字体上下文——所以一族 context 共用一个 `FT_Library`。
3. `fz_drop_context` 最终经 [fz_drop_font_context](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L398-L425) 逆序释放所有缓存的内置字体实例，再释放上下文本身。

字形光栅化的“一次调用”：

1. draw device 画文字时，对每个字形调用 `fz_render_glyph`（见 4.4）。
2. 缓存未命中时，转交 [fz_render_ft_glyph](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L1161-L1186)。
3. 它调用内部的 `do_ft_render_glyph`：加 FreeType 锁、`FT_Set_Char_Size` + `FT_Set_Transform` + `FT_Load_Glyph` + `FT_Render_Glyph`，拿到一张 `FT_Bitmap`，再转成 `fz_glyph`。

#### 4.2.3 源码精读

[font.c:L295-L311](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L295-L311) —— `struct fz_font_context`。注意它缓存了一整套内置字体实例：`base14[14]`、`cjk[4]`、`fallback[256]{serif,sans}`、`symbol1/2`、`math`、`music`、`boxes`、`emoji`。这就是 4.3 要讲的“内置字体提供”的存储位置。

FreeType 非线程安全，靠一对锁函数串行化：

[font.c:L350-L367](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L350-L367) —— `fz_ft_lock` / `fz_ft_unlock`。关键细节：它同时取 `FZ_LOCK_FREETYPE` 与 `FZ_LOCK_ALLOC`，并把**当前 `ctx` 暂存进 `ftmemory.user`**。这样 FreeType 的分配回调（[font.c:L324-L348](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L324-L348) 的 `ft_alloc/ft_free/ft_realloc`）才能从 `memory->user` 反推出是哪个 `ctx` 在分配，从而走 MuPDF 的内存统计与异常机制。`fz_ft_lock_held`（[font.c:L369-L375](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L369-L375)）用于检测“当前线程是否已持有该锁”，避免递归死锁。

真正的光栅化在 `do_ft_render_glyph`：

[font.c:L1040-L1055](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L1040-L1055)（开头片段）—— 先 `fz_adjust_ft_glyph_width` 做“替换字体时拉伸度量”的修正，处理 `fake_italic`（用 shear 模拟斜体），然后 `fz_ft_lock` 进入 FreeType 临界区。函数**返回时仍持有锁**，由调用方 `fz_render_ft_glyph` 在转换完成后解锁（[font.c:L1163-L1178](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L1163-L1178)）——这种“加锁者与解锁者分离”的写法在性能敏感路径里很常见。

**HarfBuzz 整形**这一侧的封装同理：HarfBuzz 也不是完全线程安全，MuPDF 用 `fz_hb_lock` / `fz_hb_unlock`（[harfbuzz.c:L139-L146](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/harfbuzz.c#L139-L146)）串行化，并**复用 `FZ_LOCK_FREETYPE` 这把锁**（注释见 [font.h:L731-L742](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L731-L742)）；同样为它装了自定义 malloc/calloc/realloc/free（[harfbuzz.c:L153-L184](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/harfbuzz.c#L153-L184)），把分配收编进 `fz_context`。

整形句柄是**懒初始化**的：首次需要整形时才创建并挂到 `fz_font->shaper_data`。HTML/EPUB 回流引擎里能看到完整用法——[html-layout.c:L277-L290](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/html/html-layout.c#L277-L290)：取出 `fz_shaper_data_t`，若 `shaper_handle == NULL` 就 `hb->destroy = destroy_hb_shaper_data; hb->shaper_handle = hb_ft_font_create(face, NULL);`，然后 `hb_shape(...)` 做整形。这样字体被 drop 时，`fz_drop_font` 里那段 `shaper_data.destroy`（[font.c:L247-L250](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L247-L250)）就会顺手销毁 HarfBuzz 句柄。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解“一个 `FT_Library`、一把 FreeType 锁”的全局化设计。
2. **步骤**：跟踪 `fz_render_ft_glyph`（[font.c:L1161](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L1161)）→ `do_ft_render_glyph`（[font.c:L1040](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L1040)）→ `fz_ft_lock`（[font.c:L351](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L351)）这条链，画出“进入光栅化前加了哪几把锁、出来时由谁解锁”。
3. **现象/预期**：`do_ft_render_glyph` 入口加锁、返回时持锁；`fz_render_ft_glyph` 在把 `FT_Bitmap` 转成 `fz_glyph` 之后，在 `fz_always` 里 `fz_ft_unlock`。这种“锁跨越函数边界”是为了避免在持锁期间做可抛异常的内存分配时发生重入。
4. 待本地验证：可开 `memento` 构建（见 u1-l2）渲染一页文字，观察 FreeType 相关分配是否都走了 `ft_alloc`。

#### 4.2.5 小练习与答案

- **Q1**：为什么 `fz_ft_lock` 要把 `ctx` 塞进 `ftmemory.user`？
  - **A**：FreeType 的分配回调签名里没有 `ctx`，只能通过 `FT_Memory->user` 反查；塞进去后 `ft_alloc/ft_free/ft_realloc` 才能用正确的 `ctx` 调 `fz_malloc_no_throw` / `fz_free`（[font.c:L324-L348](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L324-L348)）。
- **Q2**：`fz_hb_lock` 为什么复用 `FZ_LOCK_FREETYPE` 而不单独开一把锁？
  - **A**：HarfBuzz 整形通常紧挨着 FreeType 用（`hb_ft_font_create` 直接包装 FreeType face），两者访问的底层对象高度重叠；复用一把锁既减少锁数量，也避免两者互相死锁。

---

### 4.3 内置字体的提供方式：base14、Noto 与系统钩子

#### 4.3.1 概念说明

一个文档查看器最尴尬的场景是“文档里指定了 Helvetica，但用户机器上没装”。MuPDF 的解法是**自己内置一批字体**，保证零外部依赖也能渲染：

- **URW Nimbus**（GPL 系列）：作为 PDF **标准 14 字体（base14）**的替代品。PDF 规范规定这 14 种字体“任何阅读器都应内置”，MuPDF 用 NimbusMono/NimbusSans/NimbusRoman 来实现。
- **Noto**（Google）：覆盖大量脚本的泛字体，作为拉丁/希腊/西里尔/CJK 等的回退与 HTML 排版用字。
- **系统字体钩子**：当内置字体仍不够（例如用户想用本地已装字体），允许宿主程序注册回调，把“按名字/CJK/脚本找字体”交给应用层。

#### 4.3.2 核心流程

内置字体的“提供”分两层：

1. **数据层（查找）**：一组 `fz_lookup_*_font` 函数返回**编译进二进制的字体文件数据指针**（`const unsigned char *` + 长度）。是否可用取决于编译配置。
2. **对象层（构造）**：一组 `fz_new_*_font` 便捷构造器，把上一步的数据包成 `fz_font`，并在字体上下文里缓存实例（如 `ctx->font->base14[x]`），避免重复建 face。

系统字体则走另一条路：应用调用 `fz_install_load_system_font_funcs` 注册三个回调，之后 `fz_load_system_font` 等就会回调它们。

#### 4.3.3 源码精读

**内置字体登记表**在 [font-table.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font-table.h)。每行 `FONT(...)` 或 `ALIAS(...)` 声明一个内置字体条目，含来源（`urw`/`noto`）、字形数据符号、对外名字、脚本、语言、子字体索引、样式。例如：

- [font-table.h:L24-L31](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font-table.h#L24-L31)：URW `NimbusMonoPS_*` 同时以 `"Courier"`（base14 名）和 `"Nimbus Mono"`（别名）暴露，覆盖 Regular/Italic/Bold/BoldItalic。
- [font-table.h:L33-L66](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font-table.h#L33-L66)：`NimbusSans_*` 对应 `"Helvetica"`（+`"Arial"` 别名），`NimbusRoman_*` 对应 `"Times"`（+`"Times New Roman"`/`"Times Roman"` 别名）。
- [font-table.h:L85-L88](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font-table.h#L85-L88)：Noto `NotoSerif_Regular_otf` 以 `"Noto Serif"` 暴露给 Latin/Greek/Cyrillic/Common 脚本。

**数据层查找函数**（返回原始字体字节）：

| 函数 | 作用 | 位置 |
| --- | --- | --- |
| `fz_lookup_base14_font(ctx, name, &len)` | 查 base14 字体数据 | [font.h:L359](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L359) |
| `fz_lookup_builtin_font(ctx, name, bold, italic, &len)` | 查任意内置字体（按名字+样式） | [font.h:L345](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L345) |
| `fz_lookup_cjk_font(ctx, ordering, &len, &index)` | 查 CJK 字体 | [font.h:L373](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L373) |
| `fz_lookup_noto_font(ctx, script, lang, &len, &subfont)` | 按脚本/语言查 Noto 字体 | [font.h:L413](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L413) |

**对象层构造器**：

[font.c:L898-L916](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L898-L916) —— `find_base14_index`：把 14 个标准名字（Courier/Helvetica/Times-Roman/Symbol/ZapfDingbats 及其变体）映射到 `0..13`。

[font.c:L918-L941](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L918-L941) —— `fz_new_base14_font`：先看 `ctx->font->base14[x]` 有没有缓存，有就 `fz_keep_font` 复用；否则 `fz_lookup_base14_font` 取数据、`fz_new_font_from_memory` 建 face、标记可嵌入、缓存后返回。这正是“同名字体只解析一次”的实现。

[font.c:L970-L985](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L970-L985) —— `fz_new_builtin_font`：`fz_lookup_builtin_font` 取数据后建 face，并**显式标记不可嵌入**（`fz_set_font_embedding(ctx, font, 0)`），因为内置字体本就在阅读器里、不必再写进输出文档。

**系统字体钩子**：

[font.h:L292-L295](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L292-L295) —— `fz_install_load_system_font_funcs(ctx, f, f_cjk, f_fallback)`：一次安装三套回调（普通/CJK/回退），存进字体上下文（[font.c:L301-L303](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L301-L303)）。之后 [fz_load_system_font](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L312)（[font.h:L312-L312](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/font.h#L312)）就会回调它们。默认未安装时，这些查找返回 NULL，MuPDF 回退到内置字体。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：搞清“PDF 里写 Helvetica，MuPDF 最终用哪个字体文件”。
2. **步骤**：
   - 在 `fz_new_base14_font`（[font.c:L918](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L918)）里看 `find_base14_index("Helvetica")` 得到索引 4。
   - 追到 `fz_lookup_base14_font` → 最终命中的是 `font-table.h` 里 `"Helvetica"` 的 URW NimbusSans 数据。
3. **现象/预期**：渲染一个只含 Helvetica 的 PDF，不需要系统装任何字体即可正确显示，字体来自二进制内嵌的 NimbusSans。
4. 待本地验证：用 `mutool convert -F pdf` 或 `pdfshow` 查看字体名，确认是 NimbusSans。

#### 4.3.5 小练习与答案

- **Q1**：为什么 `fz_new_builtin_font` 要把字体标记为“不可嵌入”？
  - **A**：内置字体（如 Noto）已经在阅读器二进制里，输出文档时再把它整份写进去会无谓增大体积；而 base14 字体另当别论（[font.c:L933-L936](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L933-L936) 注释说明了 base14 暂时允许嵌入的权宜之计）。
- **Q2**：若想让 MuPDF 用 Windows 已装的“微软雅黑”，应该怎么做？
  - **A**：实现 `fz_load_system_cjk_font_fn` / `fz_load_system_fallback_font_fn`，在回调里从系统字体目录加载并用 `fz_new_font_from_file` 返回，再 `fz_install_load_system_font_funcs` 注册。

---

### 4.4 字形缓存（glyph cache）：哈希 + LRU，且独立于 store

#### 4.4.1 概念说明

这是本讲的重点。**字形缓存缓存的是“已经光栅化好的字形位图”**，而不是字体对象本身。它解决的核心矛盾是：

- 画一页正文，同一个字形（如字母 `e`）会出现成百上千次。
- 每次都调用 FreeType 重新光栅化（`FT_Load_Glyph` + `FT_Render_Glyph`）代价很高。
- 但字形位图只跟“哪个字体、哪个字形 id、多大的变换、几级抗锯齿”有关——这些一旦确定，位图就可以反复复用。

所以 MuPDF 把“字体 + 字形 + 量化后的变换 + 抗锯齿级别”作为 key，把渲染出的位图作为 value 存起来。

> 🔑 **本讲最重要的一条事实**：字形缓存是一个**独立的缓存**，挂在 `ctx->glyph_cache`，**不是 `ctx->store`**。它有**写死的 1 MiB 上限**（`MAX_CACHE_SIZE`），与 `fz_new_context(max_store)` 的 store 上限**没有任何关系**。原文实践任务里“用 store 上限影响字形缓存”的说法，在当前代码里不成立。

#### 4.4.2 核心流程

字形缓存的查—算—存流程（封装在 [fz_render_glyph](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L278-L448)）：

```text
fz_render_glyph(ctx, font, gid, ctm, ...)
  │
  ├─ fz_subpixel_adjust：把 ctm 的子像素位置量化（让相邻位置命中同一缓存项）
  │     → 得到 key = { font, gid, a,b,c,d (ctm×65536), e,f (量化子像素), aa }
  │
  ├─ do_hash(key) % 509 → 定位桶；桶内逐项 memcmp(key)
  │     ├─ 命中：move_to_front + fz_keep_glyph，直接返回（不调 FreeType）
  │     └─ 未命中：继续
  │
  ├─ 真正光栅化：
  │     ├─ FreeType 字体 → fz_render_ft_glyph（持 FZ_LOCK_FREETYPE）
  │     └─ Type3 字体   → fz_render_t3_glyph（执行期间临时放开 glyphcache 锁，避免死锁）
  │
  └─ 入缓存（仅当尺寸 < MAX_GLYPH_SIZE 且可缓存）：
        ├─ 新建 entry，挂到桶头 + LRU 头；keep glyph、keep font
        ├─ cache->total += 该字形大小
        └─ while (total > 1 MiB)：淘汰 LRU 尾部
```

关键点：

- **key 含完整变换**：同一字形在不同字号/旋转下是不同缓存项；但**子像素位置被量化**，所以“几乎同一位置”的两个字形会共用一个位图（`fz_subpixel_adjust`）。
- **LRU + move-to-front**：每次命中都把条目移到链表头；满了从尾部淘汰。
- **线程安全**：整个查/算/存过程在 `FZ_LOCK_GLYPHCACHE` 下进行；该缓存在 `fz_clone_context` 时被**共享**（和 store 一样），所以多线程渲染能共享已暖好的字形。

#### 4.4.3 源码精读

**缓存结构**：

[draw-glyph.c:L31-L34](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L31-L34) —— 三个常量：`MAX_GLYPH_SIZE 256`（超过此尺寸的字形不入缓存）、`MAX_CACHE_SIZE (1024*1024)`（**写死的 1 MiB 上限**）、`GLYPH_HASH_LEN 509`（哈希桶数）。

[draw-glyph.c:L36-L44](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L36-L44) —— `fz_glyph_key`：缓存键。注意 `a/b/c/d` 是把 `ctm` 的四个分量乘以 65536 得到的定点数，`e/f` 是量化后的子像素偏移（各 1 字节），`gid` 是字形 id，`aa` 是抗锯齿级别。

[draw-glyph.c:L57-L68](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L57-L68) —— `struct fz_glyph_cache`：`refs`（跨克隆 context 共享计数）、`total`（当前已用字节）、`entry[509]`（哈希桶数组）、`lru_head/lru_tail`（LRU 双链表）。**注意它没有“最大上限”字段——上限就是上面那个编译期常量。**

**核心函数 `fz_render_glyph`**：

[draw-glyph.c:L278-L338](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L278-L338) —— 前半段：`fz_subpixel_adjust` 量化得到 key；构造 key（[L317-L323](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L317-L323)）；`do_hash` 后在桶里 `memcmp` 查找（[L325-L338](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L325-L338)）。命中即 `move_to_front` + `fz_keep_glyph` 返回。

[draw-glyph.c:L258-L276](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L258-L276) —— `move_to_front`：把命中条目摘下、重新挂到 LRU 头。这就是 LRU“最近用过的更不容易被淘汰”的实现。

[draw-glyph.c:L344-L423](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L344-L423) —— 未命中时的渲染与入缓存：对 FreeType 字体调 `fz_render_ft_glyph`（[L348](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L348)），对 Type3 调 `fz_render_t3_glyph`（[L363](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L363)）。Type3 路径有一段重要注释（[L352-L366](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L352-L366)）：执行 Type3 字形代码前必须**临时放开 glyphcache 锁**，因为它可能回调进设备、甚至递归触发渲染；放开后另一线程可能抢先渲染了同一字形，所以入缓存前要再查一次，若已被插入就丢弃自己的、用已有的（[L382-L394](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L382-L394)）。

[draw-glyph.c:L396-L422](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L396-L422) —— 入缓存与淘汰：新建 entry、挂桶头、挂 LRU 头、`fz_keep_glyph` + `fz_keep_font`、`cache->total += size`；然后 `while (cache->total > MAX_CACHE_SIZE)` 从 `lru_tail` 淘汰（[L414-L421](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L414-L421)）。这里就是“1 MiB 上限”的实际作用点。

**缓存值 `fz_glyph`**（RLE 压缩的位图）：

[glyph-imp.h:L55-L62](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/glyph-imp.h#L55-L62) —— `struct fz_glyph`：开头是 `fz_storable`（所以字形本身用 storable 引用计数），后面跟 `x,y,w,h`、可选的 `fz_pixmap *pixmap`、`size`、柔性数组 `data[]`（RLE 压缩数据）。注释（[L28-L54](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/glyph-imp.h#L28-L54)）详细描述了这套 RLE 编码（每行用“透明段/实心段/中间调段”游程编码），目的是既省内存又便于快速贴图。

[glyph.c:L155-L191](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/glyph.c#L155-L191) —— `fz_new_glyph_from_pixmap`：当像素数 `< RLE_THRESHOLD(256)` 或非 8bit 时，直接持有 pixmap；否则走 `fz_new_glyph_from_8bpp_data` 做 RLE 压缩（[glyph.c:L193-L339](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/glyph.c#L193-L339)）。也就是说小字形直接存位图、大字形压缩存——在缓存密度与解码速度间取平衡。

**谁在调用缓存（客户侧）**：

[draw-device.c:L1073](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L1073) —— `fz_draw_fill_text`：draw device 的 `fill_text` 回调实现，遍历文本 span 的每个字形。

[draw-device.c:L1130](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L1130) —— 对每个字形调 `fz_render_glyph(ctx, span->font, gid, &trm, model, &state->scissor, ...)`，这就是进入缓存的入口；拿到 `fz_glyph` 后由 `draw_glyph`（[draw-device.c:L1004](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L1004)）贴到目标 pixmap（[L1138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L1138)）。

**缓存的管理 API**：

- [fz_purge_glyph_cache](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/glyph-cache.h#L35)（实现 [draw-glyph.c:L131-L137](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L131-L137)）：清空全部字形缓存。本讲实践用它来制造“缓存冷”的场景。
- [fz_dump_glyph_cache_stats](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/glyph-cache.h#L75)（实现 [draw-glyph.c:L487-L494](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L487-L494)）：打印缓存占用字节数（debug 构建下还打印淘汰统计）。

**与 context 的关系**：

[context.c:L298-L299](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L298-L299) —— `fz_new_context` 第二阶段：`fz_new_store_context` 与 `fz_new_glyph_cache_context` **并列**创建，二者本就是两套独立缓存。

[context.c:L351-L352](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L351-L352) —— `fz_clone_context`：`fz_keep_store_context` 与 `fz_keep_glyph_cache` **并列**共享，所以多线程渲染时一族 context 共用同一个字形缓存。

#### 4.4.4 代码实践（可运行，含完整示例）

> 说明：原文任务用 `fz_new_font_from_name`（不存在）且想用 store 上限影响字形缓存（不成立）。下面给出**基于真实 API** 的等价实践：用 `fz_new_base14_font` 按名字加载内置字体，渲染大量重复字符，用 `fz_purge_glyph_cache` 制造“缓存冷/热”对照。

1. **目标**：直观看到字形缓存对“重复字符”渲染的加速。
2. **操作步骤**：
   - 把下面的示例代码存为 `docs/examples/glyph-cache-demo.c`（示例代码，非项目原有文件），用构建 example 的方式编译（参考 u1-l5：`make examples` 的同款编译参数，链 `libmupdf` 与 `libmupdf-third`）。
   - 运行程序，观察输出的两组耗时。

```c
/* 示例代码：观察字形缓存对重复字符渲染的影响
 * 关键 API（均真实存在）：
 *   fz_new_base14_font      —— 按名字加载内置字体（替代不存在的 fz_new_font_from_name）
 *   fz_show_string          —— 把字符串追加进 fz_text（做文字整形/排布）
 *   fz_fill_text            —— device 的文字绘制回调
 *   fz_purge_glyph_cache    —— 清空字形缓存（本实践用来制造"冷缓存"场景）
 */
#include "mupdf/fitz.h"
#include <stdio.h>
#include <time.h>

static double render_once(fz_context *ctx, fz_pixmap *pix,
                          const fz_text *text, const float black[3])
{
    fz_device *dev = NULL;
    clock_t t0, t1;

    fz_clear_pixmap_with_value(ctx, pix, 0xFF); /* 清成白底 */

    fz_var(dev);
    fz_try(ctx)
    {
        dev = fz_new_draw_device(ctx, fz_identity, pix);
        fz_fill_text(ctx, dev, text, fz_identity,
                     fz_device_rgb(ctx), black, 1.0f,
                     fz_default_color_params);
        fz_close_device(ctx, dev);
    }
    fz_always(ctx)
        fz_drop_device(ctx, dev);
    fz_catch(ctx)
        fz_rethrow(ctx);

    /* 注：为便于演示，计的是"含贴图在内的整次绘制" */
    t0 = clock();
    /* 真正的计时应把上面 fz_fill_text 包在 t0/t1 之间；
       此处结构仅为示意，请按提示自行调整。 */
    t1 = clock();
    return (double)(t1 - t0) / CLOCKS_PER_SEC;
}

int main(void)
{
    fz_context *ctx = NULL;
    fz_font *font = NULL;
    fz_text *text = NULL;
    fz_pixmap *pix = NULL;
    const float black[3] = {0, 0, 0};

    ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_register_document_handlers(ctx);

    /* 1. 按名字加载一个内置字体（base14） */
    font = fz_new_base14_font(ctx, "Helvetica");

    /* 2. 构造大量重复字符的文本：200 行，每行 16 个 'A' */
    text = fz_new_text(ctx);
    {
        fz_matrix trm = fz_scale(24, 24); /* 字号 24 */
        int i;
        for (i = 0; i < 200; i++)
        {
            trm.e = 20;            /* 行首 x */
            trm.f = 40 + i * 26;   /* 逐行下移 */
            /* fz_show_string 返回画完后的笔位置，这里不关心 */
            (void)fz_show_string(ctx, text, font, trm,
                                 "AAAAAAAAAAAAAAAA", /* 16 个 A */
                                 0,            /* wmode: 水平 */
                                 0,            /* bidi_level: LTR */
                                 FZ_BIDI_LTR,  /* markup_dir */
                                 FZ_LANG_UNSET /* language */);
        }
    }

    pix = fz_new_pixmap(ctx, fz_device_rgb(ctx), 800, 1100, NULL, 0);

    /* 3a. 预热：先画一次，让字形进入 glyph cache */
    render_once(ctx, pix, text, black);

    /* 3b. 缓存热：再画一次（字形大多命中 glyph cache） */
    printf("cache-hot  : %.4fs\n", render_once(ctx, pix, text, black));

    /* 3c. 清空字形缓存后再画（每个字形都要重新光栅化） */
    fz_purge_glyph_cache(ctx);
    printf("cache-cold : %.4fs\n", render_once(ctx, pix, text, black));

    /* 4. 逆序释放：pixmap → text → font → context */
    fz_drop_pixmap(ctx, pix);
    fz_drop_text(ctx, text);
    fz_drop_font(ctx, font);
    fz_drop_context(ctx);
    return 0;
}
```

3. **需要观察的现象**：`cache-hot` 的耗时应**明显小于** `cache-cold`，因为热路径上每个 `A` 命中 `fz_render_glyph` 的缓存（`move_to_front` + `fz_keep_glyph`），冷路径上每个 `A` 都要走 `fz_render_ft_glyph` 让 FreeType 重新光栅化。
4. **预期结果**：200 行 × 16 个 `A` = 3200 个字形，但字形种类只有 1 种（`A`）。热路径理论上只光栅化 1 次；冷路径要光栅化 3200 次。耗时差距应能观察到。
5. **若无法运行**：明确标注「待本地验证」。编译命令请参考 u1-l5 中 example 的链接方式（`-lmupdf -lmupdf-third`，并保证 freetype/harfbuzz 等子模块已 `git submodule update --init`，见 u1-l2）。
6. **对照实验建议**：把循环里 `fz_show_string` 的字符串换成不重复的乱序字母（每个字形都不同），此时冷热差距会大幅缩小——因为缓存命中率本来就低。这能反向验证“缓存价值来自重复”。

> 关于计时的诚实说明：上面 `render_once` 把 `clock()` 放在了 `fz_fill_text` 之外仅为示意。真正测量时，请把 `t0 = clock();` 放在 `fz_fill_text` 之前、`t1 = clock();` 放在之后（仍在 `fz_try` 内）。本讲不在此假装已运行，具体数值「待本地验证」。

#### 4.4.5 小练习与答案

- **Q1**：为什么 key 里要包含 `a,b,c,d`（ctm 的四个分量）？只用 `font+gid` 作 key 行不行？
  - **A**：不行。同一个 `A` 在 12pt 和 48pt 下位图完全不同（轮廓光栅化的像素不同）。必须把变换量化后纳入 key，才能保证取出的位图与当前字号匹配。
- **Q2**：把 `fz_new_context` 的 `max_store` 设成 `FZ_STORE_UNLIMITED`，能让字形缓存变大吗？
  - **A**：**不能**。字形缓存上限是编译期常量 `MAX_CACHE_SIZE = 1 MiB`（[draw-glyph.c:L32](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L32)），与 store 上限无关。store 控制的是字体/图像/颜色空间等 storable 对象的缓存，不是字形位图。
- **Q3**：多线程渲染时，两个线程渲染同一字形，会各自光栅化一次吗？
  - **A**：通常不会。字形缓存在 `fz_clone_context` 时被共享（[context.c:L352](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L352)），且查/算/存都在 `FZ_LOCK_GLYPHCACHE` 下（[draw-glyph.c:L326](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L326)）。唯一例外是 Type3 字形：执行其内容流前会放开锁，可能两线程都渲染，但代码保证最终只有一个被入缓存（[draw-glyph.c:L382-L394](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L382-L394)）。

---

## 5. 综合实践

把本讲三块内容（字体加载、内置字体、字形缓存）串成一个端到端的小任务：

**任务：手写一个“单字形热力”渲染器并解释缓存命中**

1. 用 `fz_new_base14_font(ctx, "Times-Roman")` 加载字体（体会 4.3 的“按名字取内置字体 + 实例缓存”）。
2. 构造一段文本：先 100 行全为字母 `e`（最高频），再 100 行为不重复的随机大写字母。
3. 渲染到 pixmap，在渲染前后分别调用 [fz_dump_glyph_cache_stats](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/glyph-cache.h#L75)（需 debug 构建）打印 `Glyph Cache Size`。
4. 回答：
   - 渲染完前 100 行（全是 `e`）后，缓存里大约多了几个字形条目？为什么不是 100 个？
   - 渲染完全部 200 行后，缓存占用是多少？是否接近 1 MiB 上限？若没接近，说明什么？
5. 把同一页**用两种不同字号**（如 12pt 与 24pt）各渲染一次，再用 `fz_dump_glyph_cache_stats` 观察：缓存条目数是否几乎翻倍？这验证了 key 里 `a,b,c,d` 的必要性。

**交付物**：一段不超过 300 字的说明，解释“为什么高频重复字符是字形缓存的最佳受益者，而每个字都不同的文本几乎得不到加速”，并结合 `fz_render_glyph` 的命中路径（[draw-glyph.c:L328-L338](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L328-L338)）给出依据。运行数值「待本地验证」。

## 6. 本讲小结

- `fz_font` 是统一字体句柄，内部同时挂接 **FreeType（光栅化）**与 **HarfBuzz（整形）**，并区分 **FreeType 字体**（`ft_face`）与 **Type3 字体**（`t3*` 回调）两种变体；它用**裸 `int refs` + `fz_keep_imp`/`fz_drop_imp`** 管理生命周期，不是 storable。
- 所有字体共享一个 **字体上下文 `fz_font_context`**（`ctx->font`），它持有唯一的 `FT_Library`、自定义 FreeType 分配器、系统字体钩子，以及内置字体实例缓存；FreeType/HarfBuzz 的非线程安全靠 `fz_ft_lock`/`fz_hb_lock`（复用 `FZ_LOCK_FREETYPE`）串行化。
- **内置字体**保证零外部依赖可渲染：URW Nimbus 充当 PDF base14 替代，Noto 覆盖多脚本；数据层用 `fz_lookup_*_font` 查编译内嵌字节，对象层用 `fz_new_base14_font`/`fz_new_builtin_font` 构造并在 `ctx->font` 里缓存实例；应用还可经 `fz_install_load_system_font_funcs` 接入系统字体。
- **字形缓存** `fz_glyph_cache`（`ctx->glyph_cache`）缓存的是**已光栅化的字形位图**，key 为 `字体+字形+量化变换+抗锯齿`，结构是 **509 桶哈希 + LRU + move-to-front**。
- ⚠️ 关键纠偏：字形缓存**独立于 `fz_store`**，上限是**写死的 1 MiB**（`MAX_CACHE_SIZE`），不受 `fz_new_context(max_store)` 影响；要清空它用 `fz_purge_glyph_cache`。它在 `fz_clone_context` 时被共享，故多线程渲染共享同一份已暖字形。
- draw device 画文字的入口是 `fz_draw_fill_text`，它对每个字形调 `fz_render_glyph`——这就是缓存发挥作用的客户侧。

## 7. 下一步学习建议

- **u5-l2 结构化文本与 stext 设备**：本讲只关心“把字形画成像素”，下一讲讲如何用 `fz_new_stext_device` 把同样的文字指令**还原成带坐标的文本块**，是文本导出与搜索的基础。两者用的是同一个 device 抽象，但消费方式截然不同。
- **u5-l3 全文搜索**：在 stext 之上做关键词定位，返回 `fz_quad` 命中区——正好与本讲 `fz_glyph` 的位图坐标体系呼应。
- **深入阅读建议**：
  - 想吃透光栅化，读 `do_ft_render_glyph`（[font.c:L1040](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/font.c#L1040)）到 `FT_Render_Glyph` 的整条链。
  - 想理解缓存淘汰，读 `drop_glyph_cache_entry`（[draw-glyph.c:L90-L113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-glyph.c#L90-L113)）如何同时维护哈希桶链与 LRU 链。
  - 想看整形实战，读 `source/html/html-layout.c` 里 `string_walker` 如何用 HarfBuzz 把 UTF-8 文本切成字形序列。
