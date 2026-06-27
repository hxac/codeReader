# 结构化文本与 stext 设备

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚什么是「结构化文本（structured text，简称 stext）」，以及它为什么是 PDF/XPS 等格式「文本提取、全文搜索、格式转换」的共同底座。
- 理解 `fz_stext_device` 是如何作为「消费者」拦截页面绘制指令、用几何启发式把零散字形重新拼装成「段落 → 行 → 字」的带坐标结构的。
- 掌握 `fz_new_stext_page` / `fz_new_stext_device` / `fz_run_page` / `fz_print_stext_page_as_*` 这套标准「抽取 → 导出」流程。
- 认识 stext 支持的多种导出格式（text / html / xhtml / xml / json），并理解它们只是同一棵「block 树」的不同遍历器。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们分别来自前置讲义）：

- **设备模型（device）**：`fz_device` 是一张函数指针虚表，扮演「访客/消费者」；页面解释器（生产者）发出统一的绘图指令（`fill_path` / `fill_text` / `clip` 等），device 决定如何处理这些指令。draw device 把指令光栅化成像素（[u4-l3](u4-l3-draw-device-pixmap.md)），list device 把指令录制成显示列表（[u4-l2](u4-l2-display-list.md)）。本讲的 stext device 是第三种 device——它把指令「还原成带坐标的文字」。
- **`fz_run_page` 驱动**：`fz_run_page(ctx, page, dev, ctm, cookie)` 会把一页内容（正文 + 标注 + 表单）依次喂给 device `dev`，所有 device 都共用这一条驱动路径（[u4-l1](u4-l1-device-model.md)）。
- **坐标与几何**：`fz_matrix` 仿射变换、`fz_rect` 浮点矩形、`fz_point` 点、`fz_quad` 非轴对齐四边形（[u3-l3](u3-l3-geometry-matrix.md)）。本讲会频繁用到「字形的原点 `origin`」和「字形包围四边形 `quad`」。
- **引用计数**：`fz_keep_*` / `fz_drop_*` 的配对规则（[u2-l2](u2-l2-memory-refcount.md)）。

一个直觉性的问题先放在脑子里：**PDF 里根本没有「一行字」这种东西。** PDF 内容流里写的只是「在坐标 \((e,f)\) 处放置字形 65、在 \((e+adv,f)\) 处放置字形 66……」。要得到读者能选中的「一行文字」，必须有人根据坐标把字形重新拼成「同一基线上的字属于同一行、行与行构成段落、字与字之间的大间隙要补一个空格」。这个人就是 stext device。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/mupdf/fitz/structured-text.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h) | stext 的全部公共 API 与数据结构：`fz_stext_page` / `fz_stext_block` / `fz_stext_line` / `fz_stext_char`、`FZ_STEXT_*` 选项位、以及各类 `fz_print_stext_page_as_*` 导出函数。 |
| [source/fitz/stext-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c) | stext **设备**的实现：构造 device、拦截 `fill_text` 等回调、用几何启发式把字形拼成行/段、`close` 时收尾（补 bbox、bidi 重排、分段/查表）。 |
| [source/fitz/stext-output.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c) | stext **导出**的实现：把 block 树序列化成 text / html / xhtml / xml / json 五种格式，以及作为 `fz_document_writer`（文本写入器）的多页写回封装。 |
| [source/tools/mudraw.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c) | `mutool draw` 的实现，是 stext「抽取 + 多格式导出」最权威的工程级示例，第 877–928 行是本讲的范本。 |

一句话定位：**头文件是契约，`stext-device.c` 负责「造」（抽取），`stext-output.c` 负责「卖」（导出）。**

## 4. 核心概念与源码讲解

### 4.1 stext 设备抽取

#### 4.1.1 概念说明

stext device 是 `fz_device` 的派生实现（参见 [source/fitz/stext-device.c:114-165](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L114-L165)，`fz_stext_device` 结构体首成员就是 `fz_device super`，这是 u4-l2 介绍过的「C 多态」手法）。它和 draw device 接收的是**同一批指令**，但处理方式截然不同：

- draw device：把 `fill_path` / `fill_text` 光栅化成像素。
- **stext device：只关心文本指令**，把每个字形的「字符码 + 落点坐标 + 字体 + 字号 + 颜色」记录下来，再用几何关系把它们组织成「行」和「段落」。

为什么需要它？因为文档里的文本有三种典型需求是「光栅化」给不了的：

1. **复制 / 提取**：用户选中一段文字复制出来，需要的是 Unicode 文本和阅读顺序，而不是一张位图。
2. **搜索**：搜一个关键词，需要知道它出现在第几页、哪个坐标，以便高亮。
3. **格式转换**：把 PDF 转成 HTML / 纯文本 / JSON，需要文字的结构与位置。

这三件事都建立在同一份数据上——即 stext device 抽取出的「带坐标的结构化文本树」。所以 stext 是搜索（u5-l3）、文本导出（u6 单元）的共同底座。

关键术语：

- **stext device**：抽取设备，消费者，把绘制指令还原成文字结构。
- **`fz_stext_page`**：抽取的产物，一棵以 block 为节点的（可能是树形的）结构，见 4.2。
- **几何启发式（heuristic）**：因为 PDF 不直接告诉你「这几个字是一行」，device 必须靠字形坐标的间距与基线偏移来推断行/段边界。
- **阅读顺序（reading order）**：抽取出的文字顺序取自源文件中字形的绘制顺序，**因此不保证与人类视觉阅读顺序一致**（头文件 [include/mupdf/fitz/structured-text.h:866-867](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L866-L867) 的注释明确说明了这一点）。

#### 4.1.2 核心流程

完整的「抽取」管线（以 mudraw.c 为权威范本）是五步，前四步构造、第五步导出：

```text
fz_new_stext_page(ctx, mediabox)        // 1. 建一个空的 stext 页（含内存池）
        │
fz_new_stext_device(ctx, page, &opts)   // 2. 建 device，把虚表指向该 page
        │
fz_run_page(ctx, page, dev, ctm, cookie) // 3. 驱动页面内容：每个字形触发
        │                                  fill_text → fz_stext_extract → fz_add_stext_char
        │                                  （内部决定：续接当前行 / 开新行 / 开新段 / 补空格）
        │
fz_close_device(ctx, dev)               // 4. 收尾：补 bbox、bidi 重排、
        │                                  按 opts 跑 segment/paragraph-break/table-hunt
        │
fz_print_stext_page_as_*(ctx, out, ...) // 5. 导出成 text/html/xhtml/xml/json
        │
fz_drop_device + fz_drop_stext_page     // 释放（pool 一次性归还，无需逐节点释放）
```

进入 device 后，单个字形的最关键决策发生在 `fz_add_stext_char_imp`：它把「当前字形落点 `p`」与「上一字形结束点 `pen`」的位移，**分解到基线方向（沿字行）与垂直基线方向（跨字行）两个分量**，再按下表判定。设 `size` 为字号，两个归一化分量定义为：

\[
\text{spacing} = \frac{\hat n \cdot \Delta}{\text{size}} \quad(\text{沿基线方向的位移}),\qquad
\text{base\_offset} = \frac{\hat n_\perp \cdot \Delta}{\text{size}} \quad(\text{垂直基线方向的位移})
\]

其中 \(\Delta = p - \text{pen}\)，\(\hat n\) 为基线单位方向向量。判定阈值（[source/fitz/stext-device.c:85-89](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L85-L89)）：

| `|base_offset|` 范围 | `spacing` 情况 | 决策 |
|---|---|---|
| `< 0.8`（`BASE_MAX_DIST`） | 很小 | 续接当前行 |
| `< 0.8` | 正向较大（`0.15~0.8`） | 续接当前行，**补一个空格** |
| `< 0.8` | 负向小（重叠） | 续接当前行（容错重叠字） |
| `0.8 ~ 1.5`（`PARAGRAPH_DIST`） | — | **开新行**，同段落 |
| `> 1.5` | — | **开新段落** |

直觉：基线偏移小于一个字号的大概率是同一行；一到一点五个字号是换行不换段；超过一点五个字号就是另起一段。字间距大于 `0.15` 但小于 `0.8` 个字号时，原文里通常没有空格字符，device 会**主动补一个空格**（这正是「复制出来的 PDF 文本词与词之间有空格」的由来）。

#### 4.1.3 源码精读

**① device 的虚表装配** —— `fz_new_stext_device_for_page`：

参见 [source/fitz/stext-device.c:2977-3010](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L2977-L3010)。这段代码装配虚表，揭示了 stext device「默认只听文本」的本质：

```c
dev->super.fill_text = fz_stext_fill_text;       // 填充文本（最常见）
dev->super.stroke_text = fz_stext_stroke_text;   // 描边文本
dev->super.clip_text = fz_stext_clip_text;       // 裁剪文本
dev->super.clip_stroke_text = fz_stext_clip_stroke_text;
dev->super.ignore_text = fz_stext_ignore_text;   // 不可见文本（仍要占位）
dev->super.begin_metatext = fz_stext_begin_metatext;
dev->super.end_metatext = fz_stext_end_metatext;
dev->super.fill_shade = fz_stext_fill_shade;
dev->super.fill_image = fz_stext_fill_image;     // 注意：图像回调总被装配
dev->super.fill_image_mask = fz_stext_fill_image_mask;
/* 仅在显式开启时才装配路径回调 */
if (opts->flags & FZ_STEXT_COLLECT_STRUCTURE) { dev->super.begin_structure=...; }
if (opts->flags & (FZ_STEXT_COLLECT_VECTORS | FZ_STEXT_COLLECT_STYLES))
    { dev->super.fill_path=...; dev->super.stroke_path=...; }
```

两个要点：

- **`fill_path` / `stroke_path` 默认不装配**，意味着矢量绘图指令（画线、画矩形）默认被 stext 完全忽略——它只关心字。只有当你开启 `FZ_STEXT_COLLECT_VECTORS`（收集矢量 bbox）或 `FZ_STEXT_COLLECT_STYLES`（检测下划线/删除线等样式）时才装配。
- **`fill_image` 总被装配**，看似矛盾（默认 `FZ_STEXT_PRESERVE_IMAGES` 关闭，图像被丢弃），原因是它需要图像的 bbox 来为 `ActualText`（PDF 的「实际文本」替换）定位——见 [source/fitz/stext-device.c:3023-3026](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L3023-L3026) 的注释与 `FZ_DONT_DECODE_IMAGES` 提示。

**② 文本回调的入口** —— `fz_stext_fill_text`：

参见 [source/fitz/stext-device.c:1423-1436](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L1423-L1436)。device 接到一条 `fill_text` 指令后：先把颜色转成 `argb`，再遍历文本的每个 span，交给 `fz_stext_extract` → `do_extract` 逐字形处理，最后 `keep` 住这条 `fz_text` 以便去重（同一条 text 被 fill+clip 两次调用时只抽一次，除非开了 `COLLECT_STYLES`）。

**③ 几何启发式核心** —— `fz_add_stext_char_imp`：

参见 [source/fitz/stext-device.c:727-1010](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L727-L1010)。这是整个 stext 最复杂、也最值得读的函数。它做了这几件事：

1. 由变换矩阵 `trm` 算出基线方向 `dir`、字号 `size`、字形起点 `p` 与终点 `q`（行模式 `p` 为左下、`q` 为右下）。
2. 与上一字形比，按 4.1.2 的阈值表决定 `new_line` / `new_para` / `add_space`。
3. 若 `new_para` 或当前无 block，则 `add_text_block_to_page` 新开一个文本块（段落）。
4. 若 `new_line`，则 `add_line_to_block` 新开一行。
5. 若需要补空格，先 `add_char_to_line(...' '...)`，再 `add_char_to_line(...c...)` 把本字加入。
6. 更新 `dev->pen = q`、`dev->lastchar = c` 等「上一字」状态。

其中 RTL（从右到左，如阿拉伯文）还有一套额外逻辑：用 `bidi` 标记区分逻辑序与视觉序，`bidi==3` 标记「视觉序需重排」，留待 `close` 时由 `reverse_bidi_line` 处理。

**④ 收尾** —— `fz_stext_close_device`：

参见 [source/fitz/stext-device.c:1996-2023](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L1996-L2023)。`close` 不是可选项（u4-l1 讲过 device 生命周期四步 new→run→close→drop）。它做四件事：

1. `fixup_bboxes_and_bidi`：自底向上补齐每个 line/block 的 `bbox`（由字的 `quad` 取并集），并对标记为视觉序的行做 bidi 重排。
2. 若开了 `COLLECT_STYLES`，用收集到的「细矩形」推测删除线/下划线（`check_rects_for_strikeout`）。
3. 按选项跑 `fz_segment_stext_page`（页面分段）、`fz_paragraph_break`（段落断行）、`fz_table_hunt`（表格识别）。

**⑤ 工程范本** —— mudraw.c 的使用：

参见 [source/tools/mudraw.c:877-928](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L877-L928)。`mutool draw` 把 `text/html/xhtml/stext/stext.json` 等输出格式统一走 stext 路径，正是 4.1.2 五步流程的落地：先按格式挑选 `FZ_STEXT_*` 标志位（HTML 要 `PRESERVE_IMAGES`，非纯文本要 `ACCURATE_BBOXES` + `COLLECT_STYLES`，JSON 要 `PRESERVE_SPANS`），再 `fz_new_stext_page` → `fz_new_stext_device` → `fz_run_page` → `fz_close_device`，最后按格式分支调用对应的 `fz_print_stext_page_as_*`。

#### 4.1.4 代码实践

**实践目标**：理解「绘制指令 → 字形 → 行/段」的完整调用链，亲眼看到 stext device 如何被 `fz_run_page` 驱动。

**操作步骤**（源码阅读型实践）：

1. 打开 [source/fitz/stext-device.c:1423](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L1423) 的 `fz_stext_fill_text`，确认它遍历 `text->head` 链表上的每个 span。
2. 跟进 `fz_stext_extract`（[stext-device.c:1393](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L1393)），看它如何分流到 `do_extract`（普通）或 `do_extract_within_actualtext`（处于 ActualText 中）。
3. 跟进 `fz_add_stext_char_imp`（[stext-device.c:727](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L727)），在 `spacing` / `base_offset` 计算处（约第 856–857 行）设断点或加一行 `printf`。
4. 打开 [source/tools/mudraw.c:898](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L898) 的 `fz_run_page(ctx, page, dev, ctm, cookie)`，确认这就是触发上面整条链路的「发动机」。

**需要观察的现象**：渲染一个含两段文字、段间留白的 PDF 时，每个字形都会进入 `fz_add_stext_char_imp`；当笔从第一段末尾跳到第二段开头时，`base_offset` 会大于 `PARAGRAPH_DIST(1.5)`，从而触发 `new_para=1`、新建一个 block。

**预期结果**：你能在脑中画出「`fz_run_page` → `fill_text` → `extract` → `add_stext_char_imp` → `add_char_to_line`」这条调用链，并理解「段落/行/空格」全是 device 在运行时根据坐标算出来的，而不是 PDF 文件里写好的。

> 说明：本实践为源码阅读型，未执行编译运行；如需运行验证，可在 `fz_add_stext_char_imp` 顶部临时加 `printf("%g,%g '%c' spacing=%g base=%g\n", p.x, p.y, c, spacing, base_offset);`（注：源码中原已有一行被注释的类似 printf，见 [stext-device.c:793](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L793)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 stext device 默认不装配 `fill_path` / `stroke_path`，却总要装配 `fill_image`？

> **答案**：默认目标只是抽取文字，矢量路径对「文字内容」无贡献，故省略以提速；但图像的 bbox 是 `ActualText` 替换文本定位的依据（ActualText 可能包住一张图来提供替代文字），所以图像回调必须保留以更新 bounds。只有显式开启 `COLLECT_VECTORS` 或 `COLLECT_STYLES` 时才需要路径回调。

**练习 2**：若一份 PDF 的两段文字之间垂直间距刚好是 1.2 倍字号，device 会判定为「换行」还是「换段」？

> **答案**：换行（同段）。因为 `|base_offset|=1.2` 落在 `0.8 < |base_offset| <= 1.5`（`BASE_MAX_DIST` 到 `PARAGRAPH_DIST`）区间，触发 `new_line=1` 但 `new_para=0`，即在当前 block 内新开一行。

**练习 3**：`fz_stext_close_device` 为什么必须调用，不能直接 `fz_drop_device`？

> **答案**：`close` 负责收尾计算（补 bbox、bidi 重排、分段/查表），跳过它会导致导出的 bbox 全是空矩形、RTL 文字顺序错乱；按 u4-l1 的 device 生命周期约定，跳过 close 还会触发 `dropping unclosed device` 警告。

---

### 4.2 fz_stext_page 结构

#### 4.2.1 概念说明

stext device 抽取出的产物是 `fz_stext_page`，它是一棵「以 block 为节点」的结构。核心数据层次是：

```text
fz_stext_page  （整页，持有一个内存池 pool 与 mediabox）
   │
   └─ first_block ──> fz_stext_block ──> fz_stext_block ──> ...   （兄弟链表）
                          │
            ┌─────────────┼──────────────┬─────────────┐
        type=TEXT      type=IMAGE     type=STRUCT     type=VECTOR/GRID
            │              │              │
   first_line→line→line  transform+image  down→fz_stext_struct
            │                                └─ first_block→... （递归成树）
      first_char→char→char
```

引入的关键术语：

- **block（块）**：页面内容的顶层单元，有五种类型（见下方枚举）。最常见的 `TEXT` 块通常对应「一个段落」。
- **line（行）**：共享同一基线的一串字。
- **char（字）**：单个 Unicode 字符，附带原点 `origin`、包围四边形 `quad`、字号 `size`、字体 `font`、颜色 `argb`、样式 `flags`。
- **内存池（`fz_pool`）**：所有 block / line / char 节点都从同一个池分配，释放时只需 `fz_drop_pool` 一次性归还，**无需逐节点 free**。这是 stext 能高效承载数万字页面的关键。
- **STRUCT 块与树形结构**：当开启 `FZ_STEXT_COLLECT_STRUCTURE` 时，PDF 的结构树（如「章/节/段落」标记）会以 `STRUCT` 块的形式嵌入，使原本的线性 block 链表变成一棵树，遍历时需用深度优先（头文件 [include/mupdf/fitz/structured-text.h:223-326](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L223-L326) 有详细图解）。

一个容易混淆的点：结构体名叫 `fz_stext_page`，但头文件注释（[structured-text.h:340-342](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L340-L342)）坦言它「其实应该叫 `fz_stext_document`，因为一个实例可以累积多页内容」——它的 `mediabox` 字段是各页 mediabox 的**并集**。不过典型用法仍是「一页一个 stext page」。

#### 4.2.2 核心流程

`fz_stext_page` 的生命周期与节点构造：

```text
fz_new_stext_page(ctx, mediabox)
   │  新建 pool → 从池中分配 page → refs=1 → 建 id_list 数组
   │
（device 运行期间，按需调用：）
   add_block_to_page   → 新建一个 block，链入末尾（或当前 struct 的末尾）
   add_line_to_block   → 在 TEXT block 末尾加一行
   add_char_to_line    → 在行末尾加一个 char，并算出 quad
   add_image_block_to_page → 新建 IMAGE block，keep 住 image，bbox=transform(unit_rect)
   │
fz_keep_stext_page / fz_drop_stext_page   → 引用计数；drop 到 0 时
   │   先 drop_run（释放下级 font/image 引用），再 drop_pool（一次性归还全部节点内存）
```

注意 `drop_run`（[source/fitz/stext-device.c:258-283](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L258-L283)）的存在：池只管「节点结构体」的内存，但 char 里的 `font`、image block 里的 `image` 是池外对象（带独立引用计数），必须在 drop 池之前先 `fz_drop_font` / `fz_drop_image`，否则会泄漏字体与图像。

#### 4.2.3 源码精读

**① 顶层结构** —— `fz_stext_page`：

参见 [include/mupdf/fitz/structured-text.h:343-359](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L343-L359)。要点：`refs` 引用计数、`pool` 内存池、`mediabox` 并集矩形、`first_block` 链头；`last_block` / `last_struct` / `id_list` 是「构造期」专用字段，注释明确警告外部代码不要读取它们。

**② block 与五种类型**：

参见 [include/mupdf/fitz/structured-text.h:371-378](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L371-L378)（类型枚举）与 [include/mupdf/fitz/structured-text.h:439-452](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L439-L452)（block 结构体）。`fz_stext_block` 用一个 `union u` 按 `type` 复用内存：

| type | union 成员 | 含义 |
|------|-----------|------|
| `TEXT` (0) | `u.t` | `first_line`/`last_line` 文本行链表 + `flags`（对齐方式） |
| `IMAGE` (1) | `u.i` | `transform` 矩阵 + `image` 指针 |
| `STRUCT` (2) | `u.s` | `down` 指向 `fz_stext_struct`（结构子树）+ `index` |
| `VECTOR` (3) | `u.v` | 矢量 bbox 的 `flags`（描边/矩形/连续）+ `argb` 颜色 |
| `GRID` (4) | `u.b` | 表格网格的 x/y 坐标与单元格信息 |

**③ line 与 char**：

参见 [include/mupdf/fitz/structured-text.h:462-470](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L462-L470)（line）与 [include/mupdf/fitz/structured-text.h:476-487](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L476-L487)（char）。`fz_stext_char` 是整个结构信息密度最高的节点，记录了导出各种格式所需的全部信息：

```c
struct fz_stext_char {
    int c;            /* Unicode 码点 */
    uint16_t bidi;    /* 偶=LTR，奇=RTL */
    uint16_t flags;   /* 删除线/下划线/粗体/裁剪... 见 489-502 行枚举 */
    uint32_t argb;    /* sRGB 颜色（alpha 占高 8 位） */
    fz_point origin;  /* 字形基线起点 */
    fz_quad quad;     /* 字形包围四边形（搜索高亮用） */
    float size;       /* 字号 */
    fz_font *font;    /* 字体（被 keep，drop_run 时释放） */
    fz_stext_char *next;
};
```

**④ `fz_new_stext_page` 的池式分配**：

参见 [source/fitz/stext-device.c:235-256](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L235-L256)。先 `fz_new_pool` 建池，再 `fz_pool_alloc` 从池里分配 `page` 结构体本身；失败时 `fz_drop_pool` 回滚。整个 stext 体系的所有节点都走 `fz_pool_alloc`，没有一次 `malloc`。

**⑤ 字形 quad 的计算** —— `add_char_to_line`：

参见 [source/fitz/stext-device.c:440-515](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L440-L515)。这个函数把一个字的「原点 `p`、终点 `q`、ascender/descender 向量 `a`/`d`」组合成四个角点，填入 `ch->quad`：

```c
ch->quad.ll = (p.x + d.x, p.y + d.y);   /* 左下 */
ch->quad.ul = (p.x + a.x, p.y + a.y);   /* 左上 */
ch->quad.lr = (q.x + d.x, q.y + d.y);   /* 右下 */
ch->quad.ur = (q.x + a.x, q.y + a.y);   /* 右上 */
```

这个 `quad`（非轴对齐四边形，u3-l3 已介绍）正是后续搜索命中、选中高亮的几何依据——`fz_search_stext_page` 返回的命中就是一组 `fz_quad`。

#### 4.2.4 代码实践

**实践目标**：动手遍历一棵 `fz_stext_page`，验证对节点层次的理解。

**操作步骤**（编程型实践，基于 example.c 的骨架改写）：

1. 参考 [u1-l5](u1-l5-first-render.md) 的 example.c 跑通「创建 context → 注册 handler → 打开文档 → 取页」。
2. 在取到 `fz_page *page` 后，按 4.1.2 的五步流程替换掉原本的 pixmap 渲染：

   ```c
   /* 示例代码：手动遍历 stext 树并统计字数（仿照 stext-output.c 的遍历写法） */
   fz_rect mb = fz_bound_page(ctx, page);
   fz_stext_page *text = fz_new_stext_page(ctx, mb);
   fz_device *dev = fz_new_stext_device(ctx, text, NULL);  /* NULL = 默认选项 */
   fz_run_page(ctx, page, dev, fz_identity, NULL);
   fz_close_device(ctx, dev);
   fz_drop_device(ctx, dev);

   int n = 0;
   for (fz_stext_block *b = text->first_block; b; b = b->next)
       if (b->type == FZ_STEXT_BLOCK_TEXT)
           for (fz_stext_line *l = b->u.t.first_line; l; l = l->next)
               for (fz_stext_char *c = l->first_char; c; c = c->next)
                   n++;
   printf("本页共 %d 个字形\n", n);

   fz_drop_stext_page(ctx, text);
   ```

3. 编译运行（`make examples` 或自行链接 `libmupdf`）。

**需要观察的现象**：统计出的字形数应与该页可见文字数量接近；若文档含 ActualText 替换，字形数可能少于肉眼所见（被替换的部分按替换文本计数）。

**预期结果**：程序输出形如「本页共 1234 个字形」。> 待本地验证：确切数字取决于你选用的 PDF。

> 提示：传给 `fz_new_stext_device` 的 `NULL` 会走默认选项，等价于 `fz_init_stext_options` 设置的 `FZ_STEXT_CLIP | scale=1`（见 [stext-device.c:2065-2071](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L2065-L2071)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fz_stext_page` 的所有节点都用 `fz_pool_alloc` 而不是 `fz_malloc`？

> **答案**：一页可能有成千上万个字形，逐个 `malloc`/`free` 既慢又易泄漏、易碎片化。池式分配把所有节点放在一块连续 arena 里，构造时只是 bump pointer，释放时一次 `fz_drop_pool` 全部归还，兼顾速度与简单性。

**练习 2**：`fz_drop_stext_page` 为什么不能只 drop 池，还要先 `drop_run`？

> **答案**：池只持有「节点结构体」的内存；但 `char->font` 与 image block 的 `image` 是池外对象，有自己的引用计数（u2-l2 的 keep/drop 体系）。若直接 drop 池，这些 font/image 引用就永远无法归零，造成泄漏。`drop_run` 在 drop 池之前先沿树遍历释放这些外部引用。

**练习 3**：`fz_stext_char.quad` 与 `fz_stext_char.origin` 各有什么用途？

> **答案**：`origin` 是字形基线起点，用于计算行/段的几何关系（spacing、base_offset）和文本布局；`quad` 是字形四个角点的非轴对齐四边形，是搜索命中区域、GUI 选中高亮、`fz_highlight_selection` 的几何来源——它能在旋转/倾斜的文本上仍然准确框住字形。

---

### 4.3 多格式文本导出

#### 4.3.1 概念说明

一旦拿到 `fz_stext_page`，导出就是「用不同的方式遍历同一棵 block 树」。`stext-output.c` 提供了五种导出器，本质都是「walker」：

| 格式 | 入口函数 | 特点 | 典型用途 |
|------|---------|------|---------|
| 纯文本 | `fz_print_stext_page_as_text` | 只输出 Unicode，行末 `\n`，段末空行 | 复制粘贴、语料提取 |
| HTML（视觉） | `fz_print_stext_page_as_html` | 用绝对定位 `top/left`（pt）+ `<span>` 字体样式**还原视觉版面** | 在浏览器里看到与原 PDF 相似的排版 |
| XHTML（语义） | `fz_print_stext_page_as_xhtml` | 按字号映射 `<h1>/<h2>/<p>`，支持表格 `<table>`，**重排为流式** | reflow、语义化处理 |
| XML（详尽） | `fz_print_stext_page_as_xml` | 每个 block/line/char 都带 `bbox`/`quad`/`bidi`/`color` 属性 | 调试、精确坐标分析、机器后处理 |
| JSON | `fz_print_stext_page_as_json` | 结构化键值对，坐标可按 `scale` 缩放 | 程序间数据交换 |

关键认知：**HTML 与 XHTML 是两种取向相反的导出**。HTML（`as_html`）追求「看起来像原 PDF」——用 CSS 绝对定位把每行文字钉在它原来的坐标上；XHTML（`as_xhtml`）追求「读起来像正常网页」——丢弃精确坐标，按字号推断标题层级（`tag_from_font_size`：≥20pt→h1，≥15pt→h2，≥12pt→h3，否则 p），重排成流式文档。

另一个要点：这五种导出器 + 多页写回，被统一封装成 `fz_document_writer`（文本写入器 `fz_text_writer`），由 `fz_new_text_writer(ctx, format, path, options)` 创建。这就是 u6-l1 将要讲的「document writer」抽象的一个具体后端——stext 既是「读」的产物，也成了「写」的来源。

#### 4.3.2 核心流程

每种导出器都是「深度优先遍历 block 链」，差别只在于「遇到每种节点输出什么」：

```text
print_*(ctx, out, page, ...)
   └─ 遍历 first_block 链
        ├─ TEXT 块   → 遍历 first_line → 遍历 first_char
        │              （text: 拼 Unicode；html: <p style="top;left">+<span>；
        │                xml: <line bbox text="..."/> + <char quad="..."/>）
        ├─ IMAGE 块  → （text: 跳过；html: <img> data-uri；xml: <image bbox/>）
        ├─ STRUCT 块 → 递归进入 down->first_block（树形遍历）
        ├─ VECTOR 块 → （text: 跳过；xml: <vector bbox .../>）
        └─ GRID 块   → （表格网格；xhtml 转 <table>）
```

注意纯文本导出 `do_as_text` 对 `FZ_STEXT_LINE_FLAGS_JOINED` 的特殊处理（[stext-output.c:1313-1317](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1313-L1317)）：当一行被标记为「与下行相连」（dehyphenate 选项把行末连字符当成软连字符），导出时会跳过行末连字符且不换行，把两行拼成一词。

#### 4.3.3 源码精读

**① 纯文本导出** —— `do_as_text` / `fz_print_stext_page_as_text`：

参见 [source/fitz/stext-output.c:1294-1339](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1294-L1339)。逻辑极简：遍历 block，对 TEXT 块遍历 line，对 line 遍历 char，用 `fz_runetochar` 把 Unicode 码点编码成 UTF-8 字节写出，行末 `\n`，block 末尾再补一个 `\n`（段间空行）；遇到 STRUCT 块则递归。这是理解「stext 树遍历」最干净的范本。

**② HTML 视觉导出** —— `fz_print_stext_block_as_html`：

参见 [source/fitz/stext-output.c:291-353](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L291-L353)。每行输出一个 `<p style="top:%.1fpt;left:%.1fpt;line-height:%.1fpt">`，把行钉在原坐标；当字的 `font`/`size`/`argb`/上下标发生变化时，包一层 `<span style="font-family:...;font-size:...;color:...">`，并按字体属性加 `<b>`/`<i>`/`<tt>`/`<sup>`。特殊字符做 HTML 实体转义（`<`→`&lt;` 等）。页面外层包一个 `<div id="pageN" style="width;height">`（[stext-output.c:455-466](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L455-L466)），配合 header 里的 CSS（`p{position:absolute;white-space:pre}`）实现视觉还原。

**③ JSON 导出** —— `as_json` / `fz_print_stext_page_as_json`：

参见 [source/fitz/stext-output.c:1121-1290](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1121-L1290)。每个 TEXT 块输出 `{type:"text", bbox:{...}, lines:[...]}`，每行带 `wmode`/`bbox`/`flags`/`font`（含 family/weight/style）/`text`。坐标乘以传入的 `scale` 取整。注意 JSON 导出要求 `FZ_STEXT_PRESERVE_SPANS`（[stext-output.c:1480-1482](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1480-L1482)），这样每行首字就携带整行样式。

**④ XML 详尽导出** —— `as_xml`：

参见 [source/fitz/stext-output.c:908-1093](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L908-L1093)。这是信息最全的导出：每个 char 都带 `quad`（8 个坐标）、`x`/`y`、`bidi`、`color`、`alpha`、`flags`；每行还附带一个便于检索的 `text="..."` 属性（以及非法 XML 字符时的 `hextext` 兜底）。`mutool draw -F stext` 走的就是这条路径（[mudraw.c:907](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L907)）。

**⑤ 文档写入器封装** —— `fz_text_writer`：

参见 [source/fitz/stext-output.c:1361-1518](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c#L1361-L1518)。`text_begin_page` 建新 stext page 并返回 stext device；`text_end_page` 关闭 device 后按 `format` 分支调用对应的 `fz_print_stext_page_as_*`。这是 stext 接入 u6-l1「document writer」多页写回循环的入口，也是 `mutool convert` 把任意格式转 text/html 的底层实现。

#### 4.3.4 代码实践

**实践目标**：亲手对比同一段文字的「纯文本」与「HTML」导出，直观感受「walker 不同、输出取向不同」。

**操作步骤**（命令行型实践，最快路径）：

1. 按 [u1-l2](u1-l2-build-system.md) 编译出 `mutool`（`make` 后位于 `build/release/mutool`）。
2. 准备任意一个 PDF（记为 `in.pdf`），对同一页分别导出纯文本与 HTML：

   ```bash
   ./build/release/mutool draw -F text -o out.txt   in.pdf 1
   ./build/release/mutool draw -F html  -o out.html in.pdf 1
   ```

3. 打开 `out.txt`：你看到的是按阅读顺序排好的纯文字，段间空行，**丢失了所有坐标与字体信息**。
4. 用浏览器打开 `out.html`：你会看到文字被钉在与原 PDF 相近的位置上，`<p>` 带 `top/left` 绝对定位，`<span>` 带字体/字号/颜色——**保留了版面**。
5. 查看页面源码，定位到某个 `<p style="top:..;left:..">`，对照本讲 4.3.3 ② 的 `fz_print_stext_block_as_html`，理解每个属性是哪行代码写出的。

**需要观察的现象**：

- `out.txt` 里词与词之间有空格——这些空格大部分是 stext device 在 `fz_add_stext_char_imp` 里按 `spacing` 阈值**主动补的**（4.1.2），并非 PDF 原文所有。
- `out.html` 里若某行字号明显大于正文，它仍只是 `<p>`（HTML 模式不区分标题），而如果改用 `-F xhtml` 导出，同样的大字号会被映射成 `<h1>`/`<h2>`（4.3.1 的「两种取向」）。

**预期结果**：两份输出内容一致，但结构截然不同——`out.txt` 是扁平字符串，`out.html` 是带坐标与样式的定位文档。> 待本地验证：具体内容随 `in.pdf` 而异。

#### 4.3.5 小练习与答案

**练习 1**：同样一份 PDF，`-F text` 与 `-F html` 的输出文字内容是否相同？坐标信息呢？

> **答案**：文字内容（Unicode 序列）基本相同，都来自同一棵 stext 树；但 `text` 丢弃了所有坐标/字体/颜色信息（只保留换行），`html` 把每行的 `top/left` 和每个 span 的 `font-family/font-size/color` 都写进了 style 属性。

**练习 2**：为什么 `-F html` 能在浏览器里还原原 PDF 的版面，而 `-F xhtml` 不能？

> **答案**：HTML 模式用 CSS 绝对定位（`position:absolute` + `top/left` pt 值）把每行钉在原坐标，追求「视觉相似」；XHTML 模式丢弃精确坐标、按字号推断标题层级并重排成流式，追求「语义/可重排」。两者取向相反（4.3.1）。

**练习 3**：若要把 stext 接入「逐页写回」的多页转换流程（如 PDF→多页 HTML），应该用哪组 API？

> **答案**：用 `fz_new_text_writer(ctx, "html", path, options)` 创建一个 `fz_document_writer`，再按 u6-l1 的 `begin_page`/`run_page`/`end_page`/`close` 循环写回；它内部正是 4.3.3 ⑤ 的 `fz_text_writer`，每页自动建 stext page、跑 device、调 `fz_print_stext_page_as_html`。

## 5. 综合实践

把 4.1–4.3 串起来，完成一个「双格式对比抽取器」。

**任务**：写一个最小 C 程序，打开一个 PDF 的指定页，用 stext device 抽取文本，然后**分别**用 `fz_print_stext_page_as_text` 与 `fz_print_stext_page_as_html` 输出到两个文件，最后打印该页的「block 数 / line 数 / char 数」统计。

**骨架（示例代码，基于本讲已验证的 API）**：

```c
/* 示例代码：stext 双格式抽取器，链接 libmupdf 后运行 */
#include "mupdf/fitz.h"

int main(int argc, char **argv)
{
    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_register_document_handlers(ctx);

    fz_try(ctx) {
        fz_document *doc = fz_open_document(ctx, argv[1]);
        fz_page *page = fz_load_page(ctx, doc, atoi(argv[2]) - 1); /* 用户页码从 1 起 */
        fz_rect mb = fz_bound_page(ctx, page);

        fz_stext_page *text = fz_new_stext_page(ctx, mb);
        fz_device *dev = fz_new_stext_device(ctx, text, NULL);
        fz_run_page(ctx, page, dev, fz_identity, NULL);
        fz_close_device(ctx, dev);
        fz_drop_device(ctx, dev);

        /* 统计 */
        int nb = 0, nl = 0, nc = 0;
        for (fz_stext_block *b = text->first_block; b; b = b->next) {
            nb++;
            if (b->type != FZ_STEXT_BLOCK_TEXT) continue;
            for (fz_stext_line *l = b->u.t.first_line; l; l = l->next) {
                nl++;
                for (fz_stext_char *c = l->first_char; c; c = c->next) nc++;
            }
        }
        printf("blocks=%d lines=%d chars=%d\n", nb, nl, nc);

        /* 双格式导出 */
        fz_output *ot = fz_new_output_with_path(ctx, "out.txt", 0);
        fz_print_stext_page_as_text(ctx, ot, text);
        fz_close_output(ctx, ot); fz_drop_output(ctx, ot);

        fz_output *oh = fz_new_output_with_path(ctx, "out.html", 0);
        fz_print_stext_header_as_html(ctx, oh);
        fz_print_stext_page_as_html(ctx, oh, text, 1);
        fz_print_stext_trailer_as_html(ctx, oh);
        fz_close_output(ctx, oh); fz_drop_output(ctx, oh);

        fz_drop_stext_page(ctx, text);
        fz_drop_page(ctx, page);
        fz_drop_document(ctx, doc);
    }
    fz_catch(ctx)
        fz_report_error(ctx);

    fz_drop_context(ctx);
    return 0;
}
```

**验收要点**：

1. 程序能正确编译运行，生成 `out.txt` 与 `out.html`（资源释放顺序：text → page → doc，context 最后 drop，符合 u2-l1 的铁律）。
2. `out.txt` 与 `out.html` 文字内容一致，但 HTML 多出坐标与字体样式。
3. 能解释统计数字的来源：`nb` 是 block 链长度（含可能的 IMAGE/VECTOR 块），`nl`/`nc` 只统计 TEXT 块内的行与字。
4. （进阶）把 `fz_new_stext_device` 的第三个参数从 `NULL` 换成一个带 `FZ_STEXT_PRESERVE_IMAGES` 的 `fz_stext_options`，观察 IMAGE 块是否出现在统计与 HTML 输出中。

> 待本地验证：编译方式依平台而异，可参照 `docs/examples/example.c` 的 Makefile 规则或直接 `make examples`。

## 6. 本讲小结

- **stext device 是「文本消费者」**：作为 `fz_device` 的派生实现，它拦截 `fill_text` 等文本回调，把零散字形拼装成带坐标的「段落 → 行 → 字」结构，是文本提取、搜索、格式转换的共同底座。
- **行/段/空格全靠几何启发式算出**：`fz_add_stext_char_imp` 把字形位移分解为沿基线的 `spacing` 与垂直基线的 `base_offset`，按 `0.8`/`1.5` 两个阈值判定续行/换行/换段，并在字间距大时主动补空格。
- **标准抽取五步**：`fz_new_stext_page` → `fz_new_stext_device` → `fz_run_page` → `fz_close_device` → `fz_print_stext_page_as_*`，`close` 负责补 bbox、bidi 重排与可选的分段/查表。
- **`fz_stext_page` 是池式 block 树**：所有节点从同一个 `fz_pool` 分配、一次释放；节点分 TEXT/IMAGE/STRUCT/VECTOR/GRID 五类，开启结构收集时退化为深度优先遍历的树。
- **五种导出 = 五种 walker**：text 扁平、html 视觉定位、xhtml 语义重排、xml 详尽带坐标、json 结构化；它们遍历同一棵树，取向不同，并经 `fz_text_writer` 封装为 document writer 后端。
- **核心约束**：device 必须先 `close` 再 `drop`；stext page 的释放要先 `drop_run` 释放池外 font/image 引用，再 drop 池；资源释放顺序为 text → page → document → context。

## 7. 下一步学习建议

- **[u5-l3 全文搜索：mugrep 与 stext 搜索](u5-l3-text-search.md)**：本讲建立的 `fz_stext_char.quad` 正是搜索命中的几何依据，下一讲将讲 `fz_search_stext_page` 如何在 stext 上定位关键词并返回 `fz_quad`。
- **[u6-l1 document writer：导出抽象](u6-l1-document-writer.md)**：本讲 4.3 提到的 `fz_text_writer` 是 document writer 的一个具体后端，u6 单元会系统讲解「反向 device」的多页写回抽象。
- **继续阅读源码**：若对启发式细节感兴趣，精读 [source/fitz/stext-device.c:727-1010](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c#L727-L1010) 的 `fz_add_stext_char_imp`（含 RTL/bidi/连字符处理），以及 [source/fitz/stext-output.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-output.c) 的 `fz_print_stext_table_as_xhtml`（看 stext 如何表达表格）。
