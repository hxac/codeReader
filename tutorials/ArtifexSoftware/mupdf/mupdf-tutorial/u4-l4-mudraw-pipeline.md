# mudraw：渲染管线的集大成者

## 1. 本讲目标

前几讲我们把 MuPDF 的渲染拆成了零散的积木：`fz_context`（u2-l1）、`fz_document`/`fz_page`（u3-l1）、`fz_device`（u4-l1）、`fz_display_list`（u4-l2）、`fz_pixmap` 与 draw device（u4-l3）。本讲要做的，是把这些积木按工程级方式拼成一条完整的管线。

主线只有一个：命令行工具 `mudraw`（`source/tools/mudraw.c`）。它是 MuPDF 渲染能力的「集大成者」——你能想到的渲染需求（多分辨率、分带、多线程、颜色管理、文本抽取、格式转换），它几乎都有现成实现。官方示例 `example.c` 只用了几十行就跑通了最小渲染，而 mudraw 用两千多行把同一条链路做到了生产级。

学完本讲你应该能够：

- 说出 mudraw 从「命令行参数」到「输出文件」的完整主循环结构，以及它如何复用你前几讲学过的每一块积木。
- 解释**显示列表（display list）**在 mudraw 中的双重作用：既是页面内容的缓存，也是多线程并行渲染的前提；并能说明 `fz_cookie` 如何用于进度监控、错误计数与中断。
- 在 mudraw 源码中定位**多分辨率渲染、proof 色空间、分带（banding）、多线程 worker、后台打印（bgprint）**等进阶选项的接入点，理解它们各自解决什么工程问题。

本讲是「设备模型与渲染管线」单元（u4）的收尾，也是从「会用 API」迈向「能读懂真实工程代码」的转折点。

## 2. 前置知识

本讲默认你已经掌握以下概念（若不熟请先回看对应讲义）：

- **context / 异常**（u2-l1、u2-l3）：几乎所有 fitz 函数的第一参数是 `fz_context`，所有可能失败的调用都要包在 `fz_try/fz_catch` 里。mudraw 的主循环正是层层嵌套的 `fz_try/fz_catch`。
- **document / page 抽象**（u3-l1）：`fz_open_document` → `fz_count_pages` → `fz_load_page` → `fz_bound_page` → `fz_run_page` 是统一的页面处理链路，格式无关。
- **device 虚表**（u4-l1）：`fz_device` 是绘图指令的「消费者」，不同后端（draw / list / stext / trace / svg / pdf）填不同的虚表。`fz_run_page(ctx, page, dev, ctm, cookie)` 负责把页面内容驱动到某个 device。
- **显示列表**（u4-l2）：用 list device **录制**页面指令成 `fz_display_list`，再用 `fz_run_display_list` **回放**到任意 device，可一次录制、多次/多缩放回放。
- **draw device 与 pixmap**（u4-l3）：`fz_new_draw_device` 把矢量指令光栅化进 `fz_pixmap`；draw device 收 `fz_identity`，缩放交给回放时的 ctm。
- **几何 / ctm**（u3-l3）：`fz_pre_scale(fz_rotate(r), s, s)` 这种「先旋转再缩放」的矩阵组合，正是 mudraw 计算 ctm 的方式。

一个直觉：mudraw 与 `example.c` 跑的是**同一条本质链路**（打开→算矩阵→渲染→输出），区别只在于 mudraw 把「打开」「算矩阵」「渲染」「输出」每一步都做成了可配置、可并行、可分带的工业版本。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [source/tools/mudraw.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c) | 本讲主角。包含选项解析、主循环、`drawrange`→`drawpage`→`dodrawpage`→`drawband` 的四层调用链，以及 worker 线程与 bgprint 后台打印。 |
| [docs/examples/example.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c) | 官方最小渲染示例。作为「极简版管线」与 mudraw 对照，帮助你看清哪些是「本质」，哪些是 mudraw 额外加的工程能力。 |
| [include/mupdf/fitz/device.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h) | `fz_cookie` 结构体定义，本讲用它讲解进度监控与中断。 |

> 阅读建议：本讲涉及的函数较大（`dodrawpage` 一个函数就有近 640 行，因为里面是按 `output_format` 的大 `if/else` 分支）。建议先抓住四层调用链的骨架（4.1），再看显示列表与 cookie（4.2），最后挑一两个进阶选项深入（4.3），不要逐行通读。

---

## 4. 核心概念与源码讲解

### 4.1 选项解析与主循环

#### 4.1.1 概念说明

任何一个「集大成」工具的第一道难关都是**如何把命令行字符串变成程序内部状态**。mudraw 的做法非常典型：

1. 用一个 `fz_getopt` 循环把 `-r`、`-R`、`-o`、`-F` 等选项解析成一堆**全局变量**（如 `resolution`、`rotation`、`output`、`output_format`）。
2. 在创建 context 之前，先做**选项一致性校验**（例如「多线程必须搭配显示列表和分带」），避免运行到一半才崩溃。
3. 创建 context、注册 handler、逐个打开输入文件、对每个文件调用 `drawrange`。

这里有一个贯穿本讲的设计哲学值得记住：**mudraw 把选项解析与渲染执行彻底分离**。全局变量层是「配置」，四层调用链（`drawrange`→`drawpage`→`dodrawpage`→`drawband`）是「执行」。执行链的每一层只读配置、不动配置。这种分层让你可以单独阅读某一层而不被选项细节淹没。

#### 4.1.2 核心流程

mudraw 的整体结构可以用下面这张「从顶到底」的流程图概括：

```
mudraw_main（入口）
 │
 ├─ ① fz_getopt 解析选项 → 写入全局变量（resolution/rotation/output_format...）
 │
 ├─ ② 选项一致性校验（多线程⇒列表+分带；后台打印⇒列表）
 │
 ├─ ③ fz_new_context(alloc, locks, max_store)   ← 与 example.c 同样三步，但多了锁与可选 trace 分配器
 │
 ├─ ④ 确定 output_format（从 -F 或输出文件名后缀，查 suffix_table）
 ├─ ⑤ 校验 format ↔ colorspace 兼容性（format_cs_table），解析 colorspace/alpha
 │
 └─ ⑥ fz_register_document_handlers(ctx)
     │
     └─ 逐个输入文件 while (fz_optind < argc):
         │
         ├─ fz_open_accelerated_document  （密码鉴权 fz_needs_password/fz_authenticate_password）
         ├─ fz_style_document / fz_layout_document / fz_count_pages
         ├─ layer 配置（PDF 图层）
         │
         └─ drawrange(ctx, doc, "1-N" 或用户页码范围)
             │
             └─ 对范围内每一页 → drawpage(ctx, doc, page)
                 │
                 ├─ fz_load_page
                 ├─ （若 uselist）录制显示列表：fz_new_display_list + fz_new_list_device + fz_run_page
                 └─ dodrawpage(ctx, page, list, ...)
                     │
                     └─ 按 output_format 分支：
                         ├─ stext 系列 → fz_new_stext_device
                         ├─ OUT_PDF   → pdf_page_write device
                         ├─ OUT_SVG   → fz_new_svg_device
                         └─ 光栅输出（png/ppm/ps...）→ drawband(...)
                              │
                              └─ fz_new_draw_device_with_proof + fz_run_display_list/fz_run_page
```

注意这条链路的两个特点：

- **复用前几讲的积木**：`fz_new_context`、`fz_register_document_handlers`、`fz_load_page`、`fz_run_page`、`fz_new_draw_device` 全都来自前面讲过的 API。
- **`drawrange`→`drawpage`→`dodrawpage`→`drawband` 是逐层「收窄职责」**：`drawrange` 管页码范围，`drawpage` 管单页的录制与资源生命周期，`dodrawpage` 管按格式分派到不同 device，`drawband` 管单个光栅条带的实际光栅化。

#### 4.1.3 源码精读

**入口函数与选项解析。** mudraw 的入口在 `mudraw_main`（独立编译时为 `main`）：

[source/tools/mudraw.c:2132-2134](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2132-L2134) —— 这是 mudraw 的入口函数签名，编译进 mutool 时名为 `mudraw_main`，供 u1-l4 讲过的 `tools[]` 分发表调用。

选项解析的核心是一个标准 `fz_getopt` 循环，把每个选项写入对应全局变量：

[source/tools/mudraw.c:2149-2167](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2149-L2167) —— 例如 `-r` 写入 `resolution` 并置 `res_specified=1`，`-R` 写入 `rotation`，`-o` 写入 `output`，`-B` 写入 `band_height`。这些都是本文件顶部的静态全局变量。

**选项一致性校验**（在创建 context 之前），体现「多线程依赖显示列表+分带」这一关键约束：

[source/tools/mudraw.c:2294-2313](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2294-L2313) —— 这段明确报错：多线程必须开启显示列表（`uselist`），且必须有 `band_height`；否则「多线程无意义」。原因在 4.2 会讲：多线程是按「条带」切分显示列表并行回放的，没有列表和条带就没法并行。

**创建 context**，比 example.c 多了「锁」和「可选 trace 分配器」：

[source/tools/mudraw.c:2349-2354](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2349-L2354) —— `fz_new_context(alloc_ctx, locks, max_store)`。对照 [docs/examples/example.c:51](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L51) 的 `fz_new_context(NULL, NULL, FZ_STORE_UNLIMITED)`：example 传 `NULL` 锁（单线程不需要），mudraw 传 `locks`（多线程必须真实锁，见 u2-l1 的 clone_context 要求）。

**确定输出格式**，体现「后缀驱动」与「格式-色空间兼容矩阵」两个查表过程：

[source/tools/mudraw.c:2401-2423](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2401-L2423) —— 若给了 `-F`，就用格式名查 `suffix_table`（[source/tools/mudraw.c:112-159](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L112-L159)）；否则用输出文件名后缀匹配。`suffix_table` 把 `.png`/`.ppm`/`.svg`/`.pdf` 等后缀映射到 `OUT_PNG` 等枚举。

[source/tools/mudraw.c:2481-2502](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2481-L2502) —— 用 `format_cs_table` 校验「这个输出格式允许哪些色空间」。例如 `OUT_PPM` 只允许灰度或 RGB（见 [source/tools/mudraw.c:198](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L198)），这是 PPM 格式本身的限制。

**逐文件主循环**，串起 handler 注册、打开、布局、页码范围：

[source/tools/mudraw.c:2636-2645](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2636-L2645) —— `fz_register_document_handlers(ctx)` 之后，对非「每页一文件」的格式先写文件级头（`file_level_headers`）。注意 handler 注册发生在 `fz_try` 内部，且只注册一次，对所有输入文件复用——这正呼应 u3-l2「注册表挂进 ctx」。

[source/tools/mudraw.c:2687-2705](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2687-L2705) —— 用 `fz_open_accelerated_document` 打开（带可选加速器文件），随后 `fz_needs_password`/`fz_authenticate_password` 做密码鉴权。这与 u3-l4 讲的鉴权两步流程完全一致。

[source/tools/mudraw.c:2737-2739](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2737-L2739) —— `fz_style_document`（应用 CSS）、`fz_layout_document`（排版，回流文档如 EPUB 必需）、`fz_count_pages`（强制数页）。这是「定页文档 vs 回流文档」差异在 mudraw 中的体现（见 u3-l1）。

**页码范围分派**：

[source/tools/mudraw.c:2761-2764](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2761-L2764) —— 若命令行没有给页码范围，默认 `"1-N"`（全部页）；否则用用户给的范围串。

**`drawrange`——页码范围解析**：

[source/tools/mudraw.c:1609-1622](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1609-L1622) —— `fz_parse_page_range` 逐段解析 `"1-3,5,7-9"` 这样的范围串，对每个页号调用 `drawpage`，并用 `fz_try/fz_catch` 包住单页。注意：当 `ignore_errors`（`-i`）开启时，单页出错只 `fz_report_error` 后跳过，不让整个文档失败——这是「批处理」工具的常见容错策略。

#### 4.1.4 代码实践

**实践目标**：用 mudraw 自身验证「选项→全局变量→执行」的分层，并对比 example.c 看清两者的同构性。

**操作步骤**：

1. 先确保已按 u1-l2 编译出 `./build/debug/mutool`（或 `./build/debug/mutool draw`）。若没有，运行 `make build=debug`。
2. 准备一个示例 PDF（仓库自带测试文件可用，或任意 PDF）。
3. 对照阅读 [source/tools/mudraw.c:2149](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2149) 的 `fz_getopt` 选项串，对照 `usage()`（[source/tools/mudraw.c:426-523](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L426-L523)）确认每个字母含义。
4. 运行下面命令渲染第 1 页为 PNG（150 dpi）：
   ```bash
   ./build/debug/mutool draw -o page1.png -r 150 docs/examples/example.c.pdf 1
   # 若手头没有现成 PDF，任意 PDF 均可替换
   ```
   > 若没有 PDF 测试文件，可用 `mutool create` 生成一个最小 PDF，或使用系统已有的任意 PDF。
5. 同时阅读 [docs/examples/example.c:100-107](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L100-L107)：example.c 用 `fz_scale(zoom/100, zoom/100)` + `fz_pre_rotate` 算 ctm，再 `fz_new_pixmap_from_page_number` 一步出图。

**需要观察的现象**：

- mudraw 的 `-r 150` 让你无需改代码就改变分辨率；而 example.c 必须改命令行的 `zoom` 参数（它其实把 zoom 当命令行参数传进了 `fz_scale`）。两者本质相同，mudraw 只是把「zoom/rotate 落到全局变量再传给同样的矩阵函数」。
- mudraw 渲染多页时，文件级头（如 PS 的 `%!PS`、JSON 的 `{`）只写一次，这是 `file_level_headers` 的作用。

**预期结果**：得到一张 `page1.png`，分辨率是 150 dpi 下第 1 页的光栅图。若想验证「选项一致性校验」确实生效，可故意触发错误：`./build/debug/mutool draw -T 4 -o x.png some.pdf`（多线程却不分带），应看到报错 `multiple threads without banding is pointless`。

> 待本地验证：具体 PDF 文件名与生成的 PNG 像素尺寸取决于你使用的文档。

#### 4.1.5 小练习与答案

**练习 1**：mudraw 把选项存进全局变量而非逐层传参，这样做的好处和坏处各是什么？

> **答案**：好处是「配置层」与「执行层」解耦，执行链函数签名简洁（如 `drawband` 不必带十几个配置参数），新增选项只需加一个全局变量和一个 `case`。坏处是函数不再纯粹（依赖隐式全局状态），不利于并发复用同一进程渲染多组配置——这也是为什么 mudraw 是「一次性进程」工具，而不是可嵌入库。

**练习 2**：为什么 `fz_register_document_handlers` 在主循环**外**只调用一次，而 `fz_open_accelerated_document` 在主循环**内**每个文件都调用？

> **答案**：handler 注册是把「格式处理器」挂进 context（属配置，见 u3-l2），一次注册即可对所有文件复用；而打开文档是创建一个具体的 `fz_document` 对象实例，每个文件要一个独立实例并在处理后 drop（见 `drawrange` 末尾 `fz_drop_document`）。

---

### 4.2 display-list + cookie：录制、回放与监控

#### 4.2.1 概念说明

如果只能记 mudraw 的一句话，应该是：**mudraw 默认先把每页录制进显示列表，再回放**。这不是浪费——它带来三个工程收益：

1. **多分辨率回放**：列表录制一次，可以用不同 ctm 回放出不同分辨率的位图（呼应 u4-l2「一次录制多次渲染」）。
2. **并行渲染的前提**：列表是「已解释好、自包含」的指令流，可以被多个线程安全地并发回放——这是 4.1 校验「多线程⇒列表」的根因。
3. **生产/消费解耦**：录制（解释页面内容）和回放（光栅化）可以分到不同线程，实现「边解释下一页边渲染上一页」（bgprint）。

与显示列表搭档的是 **`fz_cookie`**——一个渲染「进度/中断」回调结构。它让调用方能在渲染过程中观察进度、统计错误、甚至随时中止。在 mudraw 里，cookie 被用来统计每页的错误数（`-i` 模式下据此决定是否标记 `errored`）。

#### 4.2.2 核心流程

单页「录制 + 回放 + 监控」的流程（`drawpage` 函数内）：

```
drawpage(ctx, doc, pagenum):
  cookie = {0}                      ← 每页一个新 cookie
  page = fz_load_page(ctx, doc, pagenum-1)

  if uselist（默认开，-D 关闭）:
      list = fz_new_display_list(ctx, bound_page_box)
      dev   = fz_new_list_device(ctx, list)
      fz_run_page(ctx, page, dev, fz_identity, &cookie)   ← 用 identity 录制！缩放留给回放
      fz_close_device(ctx, dev)
      （此时 cookie.errors 已统计了"解释阶段"的错误）

  dodrawpage(ctx, page, list, &cookie, ...)
      └─ drawband / stext / svg / pdf 分支
          └─ fz_run_display_list(ctx, list, dev, ctm, tbounds, &cookie)  ← 回放时再带入真实 ctm
             或 fz_run_page(ctx, page, dev, ctm, &cookie)   ← uselist=0 时直接跑

  if cookie.errors: errored = 1     ← 错误数汇总决定退出码
```

关键点：**录制时传 `fz_identity`，回放时才传真实 ctm**。这正是 u4-l2 强调的工程约定「draw device 收 identity，缩放纵 ctm 交给 `run_display_list`」。这样同一份列表可以 100%、200%、400% 任意回放。

`fz_cookie` 的字段（用于进度监控与中断）：

[include/mupdf/fitz/device.h:498-505](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L498-L505) —— `abort`（调用方置 1 可中止渲染）、`progress`/`progress_max`（当前进度与上界，`-1` 表示未知上界）、`errors`（错误计数）、`incomplete`（遇到 `TRYLATER` 异步错误时置位）。

#### 4.2.3 源码精读

**每页 cookie 与显示列表录制**，这是 `drawpage` 的核心片段：

[source/tools/mudraw.c:1418-1425](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1418-L1425) —— `drawpage` 函数开头声明 `fz_cookie cookie = { 0 }`，每页都从零开始。注意 `list` 和 `dev` 都先置 `NULL` 并用 `fz_var` 保护（u2-l3 讲过 setjmp/longjmp 会丢失非 volatile 变量，故跨 `fz_try` 的局部变量必须 `fz_var`）。

[source/tools/mudraw.c:1452](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1452) —— `fz_load_page(ctx, doc, pagenum - 1)`。再次提醒：用户页码从 1 起，内部从 0 起（u1-l5、u3-l1 都强调过）。

**显示列表录制三步**（仅当 `uselist`，默认 1；`-D` 关闭）：

[source/tools/mudraw.c:1483-1490](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1483-L1490) —— 这四行就是 u4-l2 讲的「建列表→建 list device→`fz_run_page` 录制→close」。注意第 1489 行传给 `fz_run_page` 的 ctm 是 `fz_identity`，缩放被刻意推迟到回放阶段。

**回放进 dodrawpage**（单线程路径）：

[source/tools/mudraw.c:1560-1569](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1560-L1569) —— 单线程时直接 `dodrawpage(ctx, page, list, pagenum, &cookie, ...)`，把 `list`（可能为 NULL）和 `cookie` 一起传下去。`bgprint.active` 时则改把 `page/list/cookie` 移交给后台线程（见 4.3）。

**dodrawpage 内的回放**——以光栅分支为例，`drawband` 是真正回放的地方：

[source/tools/mudraw.c:676-679](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L676-L679) —— 这两行是整条管线的「心脏」：**有列表就 `fz_run_display_list`，没列表就 `fz_run_page`**。`uselist=0`（`-D`）时走后者，直接解释+光栅化一步到位，但失去多分辨率/并行能力。这就是为什么校验段要求「多线程必须 `uselist`」。

**cookie 错误汇总**：

[source/tools/mudraw.c:1403-1404](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1403-L1404) —— 渲染结束后检查 `cookie->errors`，非零则置全局 `errored = 1`，最终决定进程退出码（[source/tools/mudraw.c:2916](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2916) `return (errored != 0)`）。这是 cookie 在 mudraw 中的实际用途：累积统计，而非实时进度条。

> 补充：mudraw 并未用 `cookie.progress` 实时打印进度条，它把 cookie 主要当作「错误累加器」。`abort`/`progress` 的典型用法是在 GUI viewer（如 platform/gl）里由主线程监控。本讲了解 cookie 字段语义即可，实时监控的例子在后续 viewer 相关讲义。

#### 4.2.4 代码实践

**实践目标**：动手验证「录制用 identity、回放带 ctm」的多分辨率价值，并观察 cookie.errors 的累加效果。

**操作步骤（命令行对比）**：

1. 用同一份 PDF，分别以 72、144、288 dpi 渲染第 1 页：
   ```bash
   ./build/debug/mutool draw -o p-72.png  -r 72  some.pdf 1
   ./build/debug/mutool draw -o p-144.png -r 144 some.pdf 1
   ./build/debug/mutool draw -o p-288.png -r 288 some.pdf 1
   ```
2. 用 `-D` 关闭显示列表再渲染一次，对比：
   ```bash
   ./build/debug/mutool draw -D -o p-nolist-144.png -r 144 some.pdf 1
   ```
3. 给一张故意有损坏对象的 PDF（或用 `-i` 忽略错误），观察 cookie.errors 的影响：
   ```bash
   ./build/debug/mutool draw -i -o out.png some.pdf ; echo "exit=$?"
   ```

**需要观察的现象**：

- 三张不同 dpi 的图内容一致、尺寸递增——因为同一份显示列表用了三个不同 ctm 回放（若 mudraw 每次都重新解释页面，结果相同但更慢）。
- `-D` 版本与 `-r 144` 版本像素一致，证明「关列表」只是少了缓存，不影响正确性。

**预期结果**：

- `p-72/144/288.png` 的宽高比约为 1:2:4。
- 故意制造错误时，`-i` 让进程继续并 `exit=1`（errored）；不加 `-i` 则提前抛出。

**源码阅读型实践（必做）**：在 mudraw.c 中精确定位以下两处，亲手画出调用流程图：
- `fz_new_display_list` 调用点：[source/tools/mudraw.c:1485](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1485)（录制）。
- `fz_run_page` 在录制阶段：[source/tools/mudraw.c:1489](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1489)（传 identity）。
- `fz_run_display_list` 在回放阶段：[source/tools/mudraw.c:677](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L677)（传真实 ctm，在 `drawband` 内）。
- 备用回放路径 `fz_run_page`：[source/tools/mudraw.c:679](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L679)（`uselist=0` 时）。

> 待本地验证：渲染耗时与是否开列表的关系，取决于页面复杂度；简单页面差异不大，复杂矢量页面（大量路径/文字）差异明显。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fz_run_page` 在录制阶段传 `fz_identity`，而 `fz_run_display_list` 在回放阶段传真实 ctm？如果录制时就传缩放 ctm 会怎样？

> **答案**：录制时用 identity，让列表里存的是「页面原始坐标」的指令，与分辨率无关；这样同一份列表可以用任意 ctm 回放出任意分辨率。若录制时就乘上缩放 ctm，列表里的坐标就被「烤死」成某个分辨率，再想换分辨率就得重新录制，失去缓存复用价值，也无法多线程并行回放不同分辨率。

**练习 2**：`-D`（关显示列表）会让 `fz_run_display_list` 这条路径完全走不到吗？

> **答案**：在光栅分支会改走 `fz_run_page`（[source/tools/mudraw.c:679](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L679)）。`drawband` 里 `if (list) ... else fz_run_page(...)` 的二选一就是这个机制。代价是失去缓存与并行能力，因此 `-D` 与 `-T`（多线程）互斥（见 4.1 校验）。

**练习 3**：`fz_cookie` 里 mudraw 实际用到了哪个字段？为什么没用 `progress` 做进度条？

> **答案**：mudraw 实际只用 `errors`（[source/tools/mudraw.c:1403](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1403)）做错误累加。`progress`/`abort` 主要面向交互式 GUI（主线程实时监控、用户点取消），命令行批处理工具一次性渲染完即可，没有「实时反馈」的对象，所以不必用。

---

### 4.3 进阶渲染选项：把管线变成工业级

#### 4.3.1 概念说明

mudraw 之所以叫「集大成者」，是因为它在「打开→渲染→输出」的基本链路上叠加了大量工程选项。本节把它们归类，并指出各自的源码接入点。理解这些接入点，你就掌握了「在哪里改 mudraw（或写自己的渲染程序）来实现特定需求」。

进阶选项大致分四类：

| 类别 | 选项 | 解决的工程问题 |
|------|------|----------------|
| **颜色管理** | `-c`/`-e`/`-N`/`-I`/`-G`、output intent | 不同输出设备需要不同色空间；proof 模拟印刷效果 |
| **分辨率与几何** | `-r`/`-w`/`-h`/`-f`/`-R`/`-b` | 多分辨率输出、按目标尺寸缩放、选不同页面框 |
| **内存与并行** | `-B`/`-T`/`-P`/`-L`/`-m` | 大图内存峰值、多核加速、解释与渲染并行 |
| **内容控制** | `-K`/`-KK`/`-A`/layers | 只出文字/只出图形、抗锯齿精度、PDF 图层 |

本节挑三类最重要的深入：**多分辨率与几何缩放**、**proof 色空间**、**分带与多线程/bgprint**。

#### 4.3.2 核心流程

**多分辨率与几何缩放**（光栅分支内）：mudraw 先按 dpi 算基础 ctm，再叠加 `width/height/fit` 的二次缩放：

```
zoom = resolution / 72
ctm  = fz_pre_scale(fz_rotate(rotation), zoom, zoom)     ← 与 example.c 的 scale+rotate 同构
tbounds = transform_rect(mediabox, ctm)
if (w 或 h 需要强制):                                      ← -w/-h
    scalex = w / tbounds宽; scaley = h / tbounds高
    （fit 模式各自独立缩放；否则取较小者保比例）
    ctm = concat(ctm, scale_mat)                          ← 二次缩放叠加
ibounds = round_rect(tbounds)                             ← 浮点→整数像素网格（u3-l3）
```

**分带与多线程**：当 `band_height` 非零，整页被切成多条水平条带，主线程分配条带、worker 线程并行回放：

```
bands = ceil(总高 / band_height)
每条 band 由一个 worker 线程：
    worker_thread → drawband(ctx, NULL, list, ctm, tbounds, &cookie, band*band_height, pix, &bit)
主线程收集每条带 → fz_write_band 写入 bander（PNG/PNM/PS...）
```

**bgprint（后台打印）**：把「页面解释（录制列表）」与「渲染输出」分到两个线程，主线程解释第 N+1 页时，bgprint 线程在渲染第 N 页：

```
主线程 drawpage:  录制 list → 把 list/page 移交给 bgprint → 立即去处理下一页
bgprint_worker:   收到信号 → dodrawpage(...) 回放输出 → 发回完成信号
```

#### 4.3.3 源码精读

**基础 ctm 与二次缩放**：

[source/tools/mudraw.c:1047-1051](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1047-L1051) —— `zoom = resolution/72; ctm = fz_pre_scale(fz_rotate(rotation), zoom, zoom)`。对比 [docs/examples/example.c:102-103](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L102-L103) 的 `fz_scale` + `fz_pre_rotate`：两者都是「先旋转后缩放」（u3-l3 讲过 `pre_`=左乘=先发生），只是 mudraw 写成 `pre_scale(rotate)` 而 example 写成 `pre_rotate(scale)`，等价。

[source/tools/mudraw.c:1071-1101](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1071-L1101) —— `-w`/`-h`/`-f` 的二次缩放：算出 `scalex/scaley`，`fit` 时各自独立（拉伸到指定尺寸），否则取较小者保持比例，最后 `ctm = fz_concat(ctm, scale_mat)` 叠加。这是 example.c 没有的能力。

**proof 色空间（软打样）**：

[source/tools/mudraw.c:2361-2366](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2361-L2366) —— `-e` 指定一个 ICC profile 作为 `proof_cs`，渲染时用它模拟「在目标设备上的最终颜色」。

[source/tools/mudraw.c:669](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L669) —— `fz_new_draw_device_with_proof(ctx, fz_identity, pix, proof_cs)`。注意它用的是「with_proof」变体，把 proof 色空间传给 draw device，由 device 内部做颜色转换（u4-l3 讲过 draw device 自动把内容颜色转换到目标空间）。

**像素后处理（反色/伽马）**，发生在 drawband 渲染完 pixmap 之后：

[source/tools/mudraw.c:684-687](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L684-L687) —— `-I` 反色、`-G` 伽马校正，直接在 `fz_pixmap` 上操作。这是「先光栅化成 pixmap，再对像素做处理」的典型模式（u4-l3 的 pixmap 内存布局在此派上用场）。

**kill switch（只出文字/只出图形）**——一个巧妙的 device 后处理技巧：

[source/tools/mudraw.c:632-650](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L632-L650) —— `-K`（一次）把 device 的 `fill_text/stroke_text/ignore_text` 置 NULL（不出文字），`-KK`（两次）把 `fill_path/stroke_path/fill_image` 置 NULL（不出图形）。结合 u4-l1 讲过的「包装函数对 NULL 回调判空、出错自禁用」：置 NULL 等于让对应指令被静默跳过。这是在不改解释器的前提下「过滤某类绘图指令」的最简手段。

**分带切分**：

[source/tools/mudraw.c:1112-1122](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1112-L1122) —— `band_height` 非零时计算 `bands = ceil(总高/band_height)`，并把渲染高度限制为单条带。分带的核心价值是**降低内存峰值**：不必一次性分配整页大 pixmap，而是一条带一条带地渲染并流式写入（u9-l3 会深入讲分带与内存）。

**多线程 worker 池**：

[source/tools/mudraw.c:1124-1167](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1124-L1167) —— `fz_clone_context(ctx)` 为每个 worker 克隆上下文（u2-l1 讲过 clone 共享 store、独有异常栈），并创建信号量驱动的 worker 线程。每个 worker 各自 `fz_new_pixmap_with_bbox` 创建自己那条带的 pixmap。

[source/tools/mudraw.c:1782-1797](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1782-L1797) —— `worker_thread` 循环：等 `start` 信号 → 调 `drawband(me->ctx, NULL, me->list, ...)` 回放自己那条带 → 发 `stop` 信号。注意第一个参数 `NULL`：worker 永远从 `list` 回放（这正是「多线程必须 uselist」的原因——线程安全地共享只读列表）。

**bgprint 后台打印**（页级生产/消费并行）：

[source/tools/mudraw.c:1514-1559](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1514-L1559) —— 主线程把 `page/list/seps` 移交给 `bgprint` 结构，触发 `bgprint.start` 信号后**立即返回继续下一页**。

[source/tools/mudraw.c:1812-1835](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1812-L1835) —— `bgprint_worker` 收信号后调 `dodrawpage` 回放输出，完成后发 `stop`。主线程在处理下一页前用 `bgprint_flush()` 等待上一页渲染完（[source/tools/mudraw.c:1407-1416](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1407-L1416)）。这正是 `-s t`（showtime）输出里「interpretation / rendering」分开计时的来源（[source/tools/mudraw.c:1369](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1369)）。

**lowmemory 模式**，一个跨多处的统一开关：

[source/tools/mudraw.c:2346-2347](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2346-L2347) —— `-L` 把 store 上限压到 1（基本禁用缓存）。运行时还在 device 上设 `FZ_NO_CACHE` 提示（[source/tools/mudraw.c:671-672](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L671-L672)），并在每页后 `fz_empty_store`（[source/tools/mudraw.c:1395-1396](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1395-L1396)）。这是「用速度换内存」的极端档，与 u9-l1 的 store 缓存直接相关。

#### 4.3.4 代码实践

**实践目标**：亲手触发分带 + 多线程，对比单线程，理解并行回放显示列表的加速来源。

**操作步骤**：

1. 选一个页数较多或页面较复杂的 PDF（页面越大、矢量越复杂，加速越明显）。
2. 单线程整页渲染并计时：
   ```bash
   time ./build/debug/mutool draw -o /dev/null -r 200 some.pdf
   ```
3. 分带 + 4 线程渲染并计时（注意必须同时给 `-B` 和 `-T`）：
   ```bash
   time ./build/debug/mutool draw -o /dev/null -r 200 -B 256 -T 4 some.pdf
   ```
4. 开启 showtime 看每页计时与最快/最慢页：
   ```bash
   ./build/debug/mutool draw -o /dev/null -s t -r 200 some.pdf
   ```
5. （可选）开 bgprint 看解释/渲染分开计时：
   ```bash
   ./build/debug/mutool draw -o /dev/null -s t -P -r 200 -B 256 -T 2 some.pdf
   ```

**需要观察的现象**：

- 不带 `-B`/`-T` 时为单线程整页；加了之后 worker 线程并行回放各条带，墙钟时间应下降（页面越大越明显）。
- 故意只给 `-T` 不给 `-B`，应被 4.1 的校验拦截：`multiple threads without banding is pointless`。
- `-s t` 会打印每页耗时，以及最快/最慢页统计。

**预期结果**：多线程版总耗时低于单线程版（视 CPU 核数与页面复杂度，加速比通常在 1.5×～3×）。若页面很简单，加速不明显甚至因线程开销略慢——这是正常的。

**源码阅读型实践（定位接入点）**：在 mudraw.c 中找到以下「插入新选项」的典型位置，体会工程代码的可扩展点：
- 加新输出格式：`suffix_table`（[source/tools/mudraw.c:112](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L112)）+ `dodrawpage` 内新增一个 `else if` 分支。
- 加新像素后处理：`drawband` 的 `fz_close_device` 之后（[source/tools/mudraw.c:683-687](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L683-L687)）。
- 加新的 device 提示：`drawband` 创建 device 之后（[source/tools/mudraw.c:670-675](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L670-L675)）。

> 待本地验证：实际加速比取决于机器核数与 PDF 内容；简单页面可能看不出差异。

#### 4.3.5 小练习与答案

**练习 1**：`-K`（kill text）的实现为什么是「把 device 回调置 NULL」而不是「在解释器里跳过文字操作符」？

> **答案**：置 NULL 利用了 u4-l1 讲的机制——device 包装函数对 NULL 回调判空跳过，且这发生在 device 抽象层，**与格式无关**（PDF/XPS/EPUB 都生效）。若在解释器里改，得对每种格式分别改，破坏了 device 的格式无关性。这是「在抽象边界做过滤」优于「在实现内部做过滤」的典型例子。

**练习 2**：worker 线程调 `drawband` 时第一个 `page` 参数为什么传 `NULL`？

> **答案**：见 [source/tools/mudraw.c:1797](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1797)。worker 只从共享的只读 `list` 回放（`drawband` 内 `if (list) fz_run_display_list`），不接触原始 `page`。传 NULL 既表明「不需要 page」，也避免了多线程访问同一 `page` 对象的竞争——列表是线程安全的只读快照，page 不是。

**练习 3**：bgprint（`-P`）与多线程（`-T`）都用了线程，它们的并行粒度有何不同？

> **答案**：`-T` 是**条带级**并行——同一页内多个 worker 并行回放不同水平条带；`-P` 是**页级**流水线并行——主线程解释（录制）第 N+1 页的列表时，bgprint 线程渲染（回放）第 N 页。两者可叠加（`-P` + `-T`），前者榨干单页的多核，后者抹平「解释」与「渲染」两阶段之间的等待。

---

## 5. 综合实践

**任务**：把本讲的三条主线（主循环结构、显示列表+cookie、进阶选项）串起来，亲手绘制一张「mudraw 完整渲染管线」流程图，并标注每个环节对应的源码行。

**步骤**：

1. **画骨架**：在纸上或文档里画出下面六个阶段，每阶段写一行 mudraw.c 的关键函数与行号：
   - 选项解析 → `fz_getopt`（[2149](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2149)）
   - context 创建 → `fz_new_context`（[2349](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2349)）
   - 打开与布局 → `fz_open_accelerated_document`（[2687](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2687)）/`fz_layout_document`（[2738](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2738)）
   - 显示列表录制 → `fz_new_display_list`+`fz_run_page`（[1485-1489](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1485-L1489)）
   - 回放分派 → `dodrawpage`（[1568](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1568)）
   - 光栅化单带 → `drawband`→`fz_new_draw_device_with_proof`+`fz_run_display_list`（[669-679](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L669-L679)）

2. **标注分支**：在「回放分派」节点上，画出按 `output_format` 分出的至少三条支路（stext / PDF / 光栅），各指向对应的 device 创建函数。

3. **标注并行**：在「光栅化」节点上，画出两条路径——单线程 `drawband` 与多 worker 并行回放（`worker_thread`→`drawband`），并注明 bgprint 是页级流水线。

4. **运行验证**：用一条命令同时体现「显示列表 + 多分辨率 + 多线程 + 计时」：
   ```bash
   ./build/debug/mutool draw -o out-%d.png -r 200 -B 256 -T 4 -s t some.pdf
   ```
   对照你画的图，确认每个 `-` 选项落在图中的哪个环节。

**验收标准**：你能指着图上的任一环节，说出「这里调用了哪个 fitz API、传了什么 ctm、cookie 起什么作用」。能脱口而出「录制用 identity、回放带 ctm」，就算通关。

> 待本地验证：流程图是手工产物；命令的实际输出（页数、耗时）取决于测试 PDF。

## 6. 本讲小结

- mudraw 与 `example.c` 跑的是**同一条本质链路**（context→打开→算 ctm→渲染→输出），区别在于 mudraw 用「全局变量配置层 + 四层执行链（`drawrange`→`drawpage`→`dodrawpage`→`drawband`）」把每一步都做成了可配置、可并行、可分带的工业版本。
- **显示列表是 mudraw 的默认路径**（`-D` 关闭）：录制时用 `fz_identity`（[1489](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1489)），回放时才带真实 ctm（[677](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L677)），从而支持多分辨率复用、多线程并行回放、bgprint 流水线。这也是「多线程必须 uselist」校验的根因。
- **`fz_cookie`** 在 mudraw 中主要当「错误累加器」用（`errors` 字段，[1403](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1403)）；`abort`/`progress` 字段面向 GUI 的实时监控与中断。
- 进阶选项各有接入点：几何缩放在 [1047-1101](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1047-L1101)、proof 色空间在 [669](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L669)、kill switch 在 [632-650](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L632-L650)、分带在 [1112-1122](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1112-L1122)、worker 并行在 [1782-1797](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1782-L1797)、bgprint 页级流水线在 [1812-1835](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1812-L1835)。
- mudraw 把「选项解析」与「渲染执行」分层、把「录制」与「回放」分离、把「解释」与「渲染」并行——这三组分离是它从几十行示例扩展到两千行工程代码仍可读的关键。

## 7. 下一步学习建议

- **进入文本与搜索（u5）**：本讲的 `dodrawpage` 里 `OUT_TEXT/HTML/STEXT` 分支（[840-949](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L840-L949)）已经用到了 stext device。下一单元 u5-l2 会专门讲结构化文本抽取，u5-l3 讲基于 stext 的全文搜索（`mugrep`）。
- **进入导出与转换（u6）**：`OUT_PDF` 分支（[951-992](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L951-L992)）和 `OUT_SVG` 分支（[994-1032](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L994-L1032)）是「document writer」抽象的实例。u6 会系统讲导出与格式转换（`muconvert`）。
- **深入性能与并发（u9）**：本讲的分带、多线程、bgprint、store 缓存（lowmemory）只是入门。u9-l1 讲 store 缓存机制，u9-l2 讲多线程渲染（基于 `docs/examples/multi-threaded.c`），u9-l3 讲分带渲染的内存峰值控制。
- **建议同步阅读**：`docs/examples/multi-threaded.c`（多线程渲染的最小范例，比 mudraw 的 worker 池更易读），对照本讲的 [1124-1167](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1124-L1167) 理解生产级与教学级代码的差异。
