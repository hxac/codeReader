# fz_device：显示设备抽象

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `fz_device` 是什么：它是一个「绘图指令的接收者」，是页面内容与具体后端（位图、文本、调试、列表……）之间的抽象边界。
- 读懂 `struct fz_device` 的虚表（vtable）结构，知道它由哪些函数指针回调组成。
- 掌握 `fill_path`、`fill_text`、`clip_path`、`fill_image`、`fill_shade`、`pop_clip` 等核心回调的语义，并能判断「画一个红色矩形」时哪些回调会被触发。
- 理解「为什么必须用 `fz_fill_path(ctx, dev, ...)` 而不是 `dev->fill_path(...)` 直接调用」——即包装函数的判空、异常隔离与「出错自禁用」机制。
- 认识 `fz_run_page` 如何把一页内容（正文 + 标注 + 表单控件）依次「驱动」到一个 device 上，从而把抽象的 device 真正跑起来。

本讲是第四单元「设备模型与渲染管线」的第一篇，承接 [u3-l1 文档与页面抽象](u3-l1-document-abstraction.md)，并为后续的显示列表（u4-l2）、draw device 位图渲染（u4-l3）、mudraw 管线（u4-l4）打地基。

## 2. 前置知识

阅读本讲前，建议你已经了解（来自前置讲义）：

- **context（`fz_context`）**：MuPDF 的全局状态容器，几乎所有 fitz 函数的第一个参数（见 u2-l1）。
- **document / page 抽象**：`fz_document` 与 `fz_page` 用「函数指针虚表 + 派生结构体」把多种文档格式统一成一套 API（见 u3-l1）。本讲的 device 用的是同一套手写多态手法。
- **ctm（当前变换矩阵）**：把页面 72dpi 用户坐标映射到设备像素的仿射矩阵（见 u3-l3）。device 的几乎每个回调都会收到一个 `ctm` 参数。
- **keep / drop 引用计数**：对象的生与死靠引用计数管理（见 u2-l2）。device 同样遵守 `fz_keep_device` / `fz_drop_device` 规则。

本讲会反复出现一个比喻：**device 像一个「访客（visitor）」**。文档解释器是「生产者」，负责把页面内容流翻译成一连串绘图调用；device 是「消费者」，决定这串调用到底用来干什么——画到像素缓冲、抽取成文本、打印调试日志，还是存进列表稍后回放。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `include/mupdf/fitz/device.h` | device 的公共头：定义 `struct fz_device` 虚表、所有 `fz_*` 包装函数声明、device 生命周期与各种 device 的构造函数声明。是本讲的核心。 |
| `source/fitz/device.c` | device 框架的实现：构造、引用计数、close/drop、容器栈（clip/group 栈）、所有 `fz_fill_path` 等包装函数、passthrough 设备。 |
| `include/mupdf/fitz/document.h` | 声明 `fz_run_page` / `fz_run_page_contents` / `fz_run_page_annots` / `fz_run_page_widgets`，即「驱动」device 的入口。 |
| `source/fitz/document.c` | 上述四个 run 函数的实现：把页面内容经 page 虚表回调喂给 device。 |
| `source/fitz/trace-device.c` | 一个具体 device 的范例：trace device。它的构造函数展示了「如何填充虚表」。本讲用它佐证「派生结构体 + 填表」的写法。 |
| `source/tools/mutrace.c` | `mutool trace` 子命令：用 trace device 把一页所有 device 调用以 XML 打印出来。是本讲最重要的动手实践工具。 |

定位口诀（来自 u1-l3）：**找抽象看 `include/`、找实现看 `source/`**。device 的「契约」在头文件，各后端的「填表」散落在 `source/fitz/*-device.c`。

## 4. 核心概念与源码讲解

### 4.1 device 虚表结构

#### 4.1.1 概念说明

PDF、XPS、EPUB 这些格式千差万别，但它们画到屏幕上做的事情其实就那几类：填一个路径、描一条边、画一段文字、贴一张图、做透明度编组、做裁剪……

MuPDF 把这些「基本绘图动作」抽象成一组回调，集中放在一个结构体 `struct fz_device` 里。这个结构体本质上是一张**函数指针表（虚表，vtable）**：

- **格式解释器（生产者）**：比如 `source/pdf/pdf-op-run.c`，负责读懂页面内容流，然后调用 `fz_fill_path(...)`、`fz_fill_text(...)` 等「发出指令」。
- **设备（消费者）**：每个具体 device（draw / trace / list / stext / bbox …）在构造时把自己的函数填进虚表，决定这些指令最终被如何处理。

这样设计的好处是**生产者和消费者解耦**：解释器只管「按统一接口喊一声」，至于这一声是变成像素、文本、日志还是存进列表，完全由挂在末端的 device 决定。同一份解释代码，换个 device 就能换一种用途，互不相识。

device.h 顶部的注释把现有 device 一语道破：

> The draw device will render them. The list device stores them in a list to play back later. The text device performs text extraction and searching. The bbox device calculates the bounding box for the page.

对应代码：[device.h:35-45](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L35-L45)

#### 4.1.2 核心流程

一个 device 的生命周期可以归纳为四步：

1. **创建**：调用某个 `fz_new_*_device(...)`（如 `fz_new_draw_device`、`fz_new_trace_device`）。底层都走 `fz_new_device_of_size` 分配内存并把 `refs` 置 1，然后由具体 device 填充自己关心的回调指针。
2. **驱动**：把 device 交给 `fz_run_page(ctx, page, dev, ctm, cookie)`。解释器开始读页面内容，逐条发出 `fz_fill_path`、`fz_clip_path`、`fz_pop_clip`……这些调用最终落到 device 的回调里。
3. **关闭**：调用 `fz_close_device(ctx, dev)`。它会触发 `dev->close_device`，把缓冲区里的输出冲刷干净。**关闭不是可选的**——某些 device（如输出到文件的 device）若不 close 就会得到残缺结果。
4. **释放**：调用 `fz_drop_device(ctx, dev)`，引用计数减一；归零时执行 `dev->drop_device` 并释放内存。

需要特别记牢的一对关系是 **close 与 drop 的分工**：

- `close` 表示「输入结束，请冲刷输出」，**可能抛异常**，且 **drop 时不会自动调用 close**。
- 如果你在没 close 的情况下直接 drop 到零，框架会打一句警告 `dropping unclosed device`（见下文源码），提醒你可能丢了输出。

#### 4.1.3 源码精读

先看虚表本体。`struct fz_device` 的前几个字段是公共状态，后面是一大排函数指针：

[device.h:289-313](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L289-L313) —— device 的「公共字段 + 路径/文字/图像回调」开头部分。关键字段含义：

```c
struct fz_device
{
    int refs;          // 引用计数
    int hints;         // 提示位：如 FZ_NO_CACHE、FZ_NO_TILING
    int flags;         // 渲染标志位

    void (*close_device)(fz_context *, fz_device *);   // 关闭：冲刷输出
    void (*drop_device)(fz_context *, fz_device *);    // 析构：释放资源

    void (*fill_path)(...);        // 填充路径（实心图形）
    void (*stroke_path)(...);      // 描边路径（轮廓）
    void (*clip_path)(...);        // 建立裁剪路径
    void (*clip_stroke_path)(...); // 以描边路径建立裁剪

    void (*fill_text)(...);        // 填充文字
    void (*stroke_text)(...);      // 描边文字
    void (*clip_text)(...);        // 以文字建立裁剪
    void (*clip_stroke_text)(...);
    void (*ignore_text)(...);      // 占位文字（不计入可见内容，用于文本提取时定位）

    void (*fill_shade)(...);       // 渐变/着色
    void (*fill_image)(...);       // 贴图
    void (*fill_image_mask)(...);  // 用图像做蒙版上色
    void (*clip_image_mask)(...);  // 以图像蒙版建立裁剪
    ...
};
```

回调按职能大致分组：**路径类**（fill/stroke/clip_path）、**文字类**（fill/stroke/clip_text、ignore_text）、**图像与着色类**（fill_image、fill_shade、fill_image_mask、clip_image_mask）、**容器类**（clip→pop_clip、begin/end_mask、begin/end_group、begin/end_tile）、**元信息类**（begin/end_layer、begin/end_structure、begin/end_metatext）。

虚表后半部分是「容器栈」与 `passthrough` 指针（见 [device.h:336-343](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L336-L343)），4.2 节会用到。

头文件里有一段**铁律式注释**，决定了我们该如何使用虚表：

[device.h:164-171](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L164-L171) —— 它明确说：device 结构体公开是为了让人能在 fitz 之外实现 device，但**调用回调时必须用 `fz_fill_path(ctx, dev, ...)`，绝不能直接 `dev->fill_path(...)`**。原因就是包装函数做了三件直接调用做不到的事（判空、异常隔离、出错自禁用），4.2 节展开。

再看构造与生命周期的实现：

[device.c:27-33](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L27-L33) —— `fz_new_device_of_size`：用 `fz_calloc` 清零分配（所以所有回调指针初始都是 `NULL`），再把 `refs` 置 1。具体 device 随后只填自己关心的回调，其余保持 `NULL`（表示「我不处理这种指令」）。

[device.c:69-84](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L69-L84) —— `fz_close_device`：在 `fz_try` 里调 `dev->close_device`，在 `fz_always` 里调 `fz_disable_device`（把所有回调清成 NULL），`fz_catch` 里 rethrow。所以**关闭后的 device 不再响应任何绘图调用**。

[device.c:92-104](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L92-L104) —— `fz_drop_device`：引用计数归零时，如果 `close_device` 还没被清掉（说明没 close 过），就 `fz_warn(ctx, "dropping unclosed device")`；再调 `drop_device` 释放资源，最后释放 `container` 栈和 device 自身。

#### 4.1.4 代码实践

**实践目标**：亲手验证「创建 device → 驱动 → 关闭 → 释放」的生命周期，尤其是体验 **close 与 drop 的区别**以及「未关闭」警告。

**操作步骤（源码阅读型 + 可选运行型）**：

1. 打开 [device.c:92-104](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L92-L104)，确认「未关闭就 drop 会触发警告」这一判断来自第 97-98 行的 `if (dev->close_device) fz_warn(...)`。注意：`fz_close_device` 在关闭后会把 `close_device` 置 NULL（见 [device.c:36-39](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L36-L39) 的 `fz_disable_device`），所以「已关闭」就不会警告。
2. （可选，需先按 u1-l2 编译出 `mutool`）用 trace device 做一次「正确」的生命周期实验。`mutrace.c` 的主循环已经示范了完整套路：创建 device → `fz_run_page` 驱动 → 释放。参考 [mutrace.c:76-85](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c#L76-L85)。

**需要观察的现象**：

- 阅读 `fz_close_device` 时，注意它即使 `close_device` 为 NULL 也照常执行 `fz_disable_device`——也就是说对一个「什么回调都没填」的空白 device 调 close 是安全的。
- 如果你能跑 mutool：构造一个会向文件输出的 device（后续 draw/list 讲义涉及），故意只 `fz_drop_device` 不 `fz_close_device`，观察 stderr 是否出现 `dropping unclosed device` 警告。

**预期结果**：生命周期四步顺序固定——**new → run → close → drop**，drop 必须最后；跳过 close 会得到残缺输出加一条警告。具体运行现象「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `struct fz_device` 把所有回调都设计成可空的函数指针，而不是必须实现的一组接口？

**参考答案**：不同后端只关心部分指令。例如 bbox device 只想知道「画了什么、画在哪」，对 `fill_image` 的细节不感兴趣；trace device 想记录一切但并不真正渲染。可空的回调让每个 device 只实现自己关心的那部分，其余保持 NULL，由包装函数判空跳过。这是一种「按需实现」的轻量接口约定。

**练习 2**：`fz_close_device` 之后还能再 `fz_fill_path` 吗？

**参考答案**：不能产生效果。`fz_close_device` 的 `fz_always` 分支会调 `fz_disable_device`，把所有回调（含 `fill_path`）清成 NULL。之后包装函数 `fz_fill_path` 里的判空 `if (dev && dev->fill_path)` 不成立，调用被静默忽略（见 4.2 节）。

---

### 4.2 核心绘图回调

#### 4.2.1 概念说明

虚表里有几十个回调，但日常渲染最常碰到的是这几类**核心绘图回调**：

- **路径类**：`fill_path`（填充）、`stroke_path`（描边）。一个矩形、一条曲线、一个任意形状，最终都变成一个 `fz_path` 传进来。
- **文字类**：`fill_text` / `stroke_text`。`fz_text` 是一组带位置的字形。
- **图像与着色类**：`fill_image`（贴一张位图）、`fill_shade`（渐变/着色模式）、`fill_image_mask`（用单色蒙版上色）。
- **裁剪类（容器起点）**：`clip_path` / `clip_text` / `clip_image_mask`，它们建立一块裁剪区域，之后的绘图都被限制在里面，直到 `pop_clip` 收尾。
- **透明度编组**：`begin_group` / `end_group`，对应 PDF 的透明度组（isolated/knockout/blendmode/alpha）。

每个回调都带一个 `fz_matrix ctm` 参数——这是把「用户空间坐标」变换到「设备空间」的矩阵（详见 u3-l3）。device 拿到路径/文字后，要用 `ctm` 把坐标变换过来，才能正确落到像素或输出上。

#### 4.2.2 核心流程

每个 `fz_*` 包装函数都遵循同一种「**判空 → 转发 → 出错自禁用**」三段式。以 `fz_fill_path` 为例：

```
fz_fill_path(ctx, dev, path, even_odd, ctm, cs, color, alpha, cp):
    如果 dev 非空 且 dev->fill_path 非空:
        fz_try:
            dev->fill_path(ctx, dev, path, even_odd, ctm, cs, color, alpha, cp)
        fz_catch:
            fz_disable_device(ctx, dev)   # 把全部回调清成 NULL
            fz_rethrow(ctx)               # 把异常继续向上抛
```

这里有三个关键设计：

1. **判空**：`dev` 可以为 NULL（表示「不要任何后端」），回调也可以为 NULL（表示「本 device 不处理这种指令」）。两种情况都安全跳过。
2. **异常隔离**：回调内部可能 `fz_throw`（比如 draw device 渲染时 OOM）。包装函数用 `fz_try/fz_catch`（见 u2-l3）接住。
3. **出错自禁用**：一旦某次回调抛异常，立刻 `fz_disable_device` 把整张虚表清空。这样**后续的绘图调用都会被静默跳过**，避免在一个已经处于错误状态的 device 上继续画，导致半残的、可能崩溃的结果。这是 MuPDF 容错渲染的关键一招。

裁剪类回调（`clip_path`、`clip_text`、`clip_image_mask`、`begin_mask`、`begin_group`、`begin_tile`）还多干一件事：**把一块裁剪矩形压入「容器栈」**，并把它与上层 scissor 取交集，作为新的「当前可见区域」。对应的收尾回调（`pop_clip`、`end_mask`、`end_group`、`end_tile`）则弹出栈顶。这对配对必须严格平衡，否则触发 `device calls unbalanced` 错误。

容器栈里维护的「当前 scissor」是逐层取交的：

\[ \text{scissor}_n = \text{rect}_n \cap \text{scissor}_{n-1}, \quad \text{scissor}_0 = \text{rect}_0 \]

即任意一层的可见区域 = 本层矩形 ∩ 所有祖先层矩形的交集。这样 device 在内部做裁剪/光栅化时，可以用这个最小包围盒来限制工作量。

#### 4.2.3 源码精读

**包装函数的三段式**（以 `fz_fill_path` 为典型）：

[device.c:166-180](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L166-L180) —— 判空 `dev && dev->fill_path`，在 `fz_try` 里转发，在 `fz_catch` 里 `fz_disable_device` 后 `fz_rethrow`。这正是「出错自禁用」的源头。

**裁剪类回调如何维护容器栈**（以 `fz_clip_path` 为例）：

[device.c:198-220](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L198-L220) —— 注意它**先算包围盒、压栈，再转发回调**：

```c
bbox = fz_bound_path(ctx, path, NULL, ctm);   // 路径在设备空间的包围盒
bbox = fz_intersect_rect(bbox, scissor);      // 与传入 scissor 取交
push_clip_stack(ctx, dev, bbox, ...);         // 压入容器栈
if (dev->clip_path) { fz_try dev->clip_path(...) ... }
```

也就是说，**容器栈的维护由框架（device.c）统一负责**，具体 device 的 `clip_path` 回调不必自己管栈。这把「通用记账」与「后端具体行为」分离开。

**容器栈的实现**：

[device.c:122-142](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L122-L142) —— `push_clip_stack`：容量不够时按 2 倍扩容（首倍为 4），栈空时直接置 `scissor=rect`，否则取「上一层 scissor 与本层 rect 的交集」。这就是上面公式的代码落地。

[device.c:144-153](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L144-L153) —— `pop_clip_stack`：如果栈空或栈顶类型不匹配（比如 `begin_group` 却用 `pop_clip` 收尾），就 `fz_disable_device` 并抛 `FZ_ERROR_ARGUMENT`，错误信息 `"device calls unbalanced"`。

**查询当前 scissor**：

[device.c:696-702](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L696-L702) —— `fz_device_current_scissor`：返回栈顶 scissor；栈空则返回 `fz_infinite_rect`（不裁剪）。

**一个有趣的「前置校验」**：`fz_fill_image` 在转发前会先检查图像是否带颜色空间：

[device.c:376-391](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L376-L391) —— 如果 `image->colorspace == NULL`（即单色蒙版图），直接 `fz_throw("argument to fill image must be a color image")`。因为蒙版图应当走 `fill_image_mask` 而不是 `fill_image`。这类前置校验把「用错回调」的错误挡在最外层。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：阅读 `struct fz_device` 的函数指针表，列出至少 6 个核心绘图回调，并解释「画一个红色矩形」时哪些会被调用、哪些不会。

**操作步骤**：

1. 打开 [device.h:298-314](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L298-L314)，把路径类、文字类、图像类回调逐个看一遍，对照每个的参数列表（注意都带 `fz_matrix ctm`）。
2. 在脑中模拟「一页内容流只画一个红色实心矩形」时，解释器会发出哪些 device 调用。注意：纯填充矩形走的是 **`fill_path`**——解释器把矩形构造为一条 `fz_path`，配上 DeviceRGB 颜色空间和 `[1,0,0]` 颜色，调用 `fz_fill_path`。（`re`/`f` 等 PDF 操作符到回调的精确映射在 u7-l4 讲。）

**参考答案（至少 6 个回调 + 是否触发）**：

| 回调 | 作用 | 画一个红色实心矩形时会触发吗？ |
| --- | --- | --- |
| `fill_path` | 用颜色填充一条路径 | **会**。矩形以路径形式被填充，颜色 [1,0,0]、DeviceRGB。 |
| `stroke_path` | 描边一条路径 | 否（除非矩形是「轮廓」而非「填充」）。 |
| `clip_path` | 建立裁剪路径 | 通常不会，除非矩形外层还有裁剪；若触发，稍后必有 `pop_clip` 配对。 |
| `pop_clip` | 结束一处裁剪 | 仅当之前有 `clip_*` 时才触发，与裁剪一一配对。 |
| `fill_text` | 填充文字 | 不会（页面没有文字）。 |
| `fill_image` | 贴一张彩色位图 | 不会（矩形不是图片）；而且若误把蒙版图传进来会被 [device.c:379-380](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L379-L380) 拒绝。 |
| `fill_shade` | 渐变/着色 | 不会（纯色矩形不是渐变）。 |
| `begin_group` / `end_group` | 透明度编组 | 视页面而定：若该页有透明度组，解释器会先 `begin_group` 再画矩形再 `end_group`。 |

**核心结论**：一个**纯实心红矩形**在干净页面上通常只产生一次 `fill_path`（外加可能的 `begin/end_group` 包裹）。其余回调描述的是「相邻或相关」的绘图情形。理解这点，就理解了「device 的每个回调对应一类基本绘图动作」。

**预期结果**：你能用自己的话回答「红矩形→`fill_path`」，并能解释 `stroke_path`/`fill_image`/`fill_text` 在什么相邻场景下才会出现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MuPDF 规定「必须用 `fz_fill_path(ctx, dev, ...)` 而不能直接 `dev->fill_path(...)`」？

**参考答案**：包装函数做了三件直接调用做不到的事：① 判空（`dev` 或回调为 NULL 时安全跳过）；② 异常隔离（用 `fz_try/fz_catch` 接住回调内的 `fz_throw`）；③ 出错自禁用（捕获异常后调 `fz_disable_device`，把虚表清空，避免在错误状态下继续画）。直接 `dev->fill_path(...)` 会绕过这一切，回调一旦抛异常就会一路 longjmp 到无法预期的位置，且 device 仍处于「半活」状态。

**练习 2**：如果一个页面的内容流只写了 `begin_group` 却忘了 `end_group`，渲染时会怎样？

**参考答案**：容器栈会失配。当后续某个收尾调用（如 `pop_clip`/`end_group`）执行 `pop_clip_stack` 时，发现栈顶类型与期望不符（见 [device.c:147-151](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L147-L151)），就会 `fz_disable_device` 并抛 `FZ_ERROR_ARGUMENT`（"device calls unbalanced"）。device 随即自禁用，剩余绘图被跳过，异常向上传播由调用方处理。

---

### 4.3 fz_run_page 驱动

#### 4.3.1 概念说明

有了 device 这个「消费者」，还需要一个「生产者」来真正发出指令——这就是 `fz_run_page`。它承接 u3-l1 的 page 抽象：每个 `fz_page` 在虚表里挂着 `run_page_contents`、`run_page_annots`、`run_page_widgets` 三个回调，分别负责解释「页面正文内容」「标注」「表单控件」。

`fz_run_page(ctx, page, dev, ctm, cookie)` 的职责就是把这三部分**依次**经同一个 device 「跑」一遍。换句话说，它把抽象的 device 真正「驱动」起来：

```
fz_run_page(page, dev, ctm, cookie):
    fz_run_page_contents(page, dev, ctm, cookie)   # 正文（图片、文字、矢量）
    fz_run_page_annots   (page, dev, ctm, cookie)   # 标注（高亮、注释外观…）
    fz_run_page_widgets  (page, dev, ctm, cookie)   # 表单控件（按钮、输入框…）
```

三段共用同一个 device 和同一个 ctm，所以它们叠加在一起构成你最终看到的整页画面。这也解释了为什么 u3-l1 把 `fz_run_page` 概括为「正文 + 标注 + 表单控件三合一」。

`cookie` 参数（定义在 device.h）是「应用 ↔ 库」的双向通信通道：应用可置 `cookie->abort=1` 中断长任务，库则回填 `progress`/`progress_max`/`errors`/`incomplete`。单线程场景传 `NULL` 即可。

#### 4.3.2 核心流程

完整的一次「驱动」调用链：

1. 应用：`dev = fz_new_*_device(...)` 创建设备。
2. 应用：`fz_run_page(ctx, page, dev, ctm, cookie)`。
3. `fz_run_page` 依次调用 `fz_run_page_contents` / `_annots` / `_widgets`。
4. 每个子函数判空后，转发到 `page->run_page_contents(...)` 等 page 虚表回调。
5. page 回调（由各格式专用层实现，如 PDF 的 `pdf-op-run.c`）开始解释内容流，逐条发出 `fz_fill_path`、`fz_fill_text`……
6. 这些 `fz_*` 包装函数把指令转发到 `dev->fill_path` 等真正后端。
7. 若解释中途抛 `FZ_ERROR_ABORT`（如 cookie 请求中断），run 函数会清掉 `close_device`（避免「未关闭」警告）并吞掉该错误优雅返回；其它错误则继续上抛。

第 7 点值得注意：`fz_run_page_contents` 的 catch 里有一句 `dev->close_device = NULL; /* aborted run, don't warn about unclosed device */`，因为中断是「预期内」的结束，不该被当成「忘了关闭」。

#### 4.3.3 源码精读

**`fz_run_page` 的「三合一」实现**（位于 document.c，注意不是 device.c）：

[document.c:1141-1147](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1141-L1147) —— 它就是连续三次调用：

```c
void fz_run_page(fz_context *ctx, fz_page *page, fz_device *dev,
                 fz_matrix transform, fz_cookie *cookie)
{
    fz_run_page_contents(ctx, page, dev, transform, cookie);
    fz_run_page_annots(ctx, page, dev, transform, cookie);
    fz_run_page_widgets(ctx, page, dev, transform, cookie);
}
```

**子函数的「判空 + 转发 + 中断优雅处理」**：

[document.c:1087-1103](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1087-L1103) —— `fz_run_page_contents`：判 `page && page->run_page_contents` 后在 `fz_try` 里转发；catch 中先把 `dev->close_device=NULL`（标记为「中断而非未关闭」），再用 `fz_rethrow_unless(ctx, FZ_ERROR_ABORT)` 仅放行中断错误、其余 rethrow，最后 `fz_ignore_error` 吞掉中断错误。`fz_run_page_annots` / `fz_run_page_widgets` 结构完全相同（[document.c:1105-1139](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L1105-L1139)）。

**头文件对参数的权威说明**：

[document.h:855-868](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L855-L868) —— 明确 `transform` 可含缩放/旋转（见 u3-l3），`cookie` 用于多线程进度与中断，单线程传 NULL。

**一个真实「驱动」范例（trace device）**：

[mutrace.c:76-85](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c#L76-L85) —— `mutool trace` 的核心：`dev = fz_new_trace_device(ctx, fz_stdout(ctx))`，然后 `fz_run_page(ctx, page, dev, fz_identity, NULL)`。这正是一条最短、最典型的「创建 device → fz_run_page 驱动」链路，identity 表示不缩放。

**trace device 是如何「填充虚表」的**（佐证派生结构体手法）：

[trace-device.c:664-692](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L664-L692) —— 构造函数用 `fz_new_derived_device(ctx, fz_trace_device)`（派生结构体以 `fz_device super` 作首成员，复用 u3-l1 的多态手法），然后逐个赋值 `dev->super.fill_path = fz_trace_fill_path;`、`dev->super.clip_path = fz_trace_clip_path;` ……

[trace-device.c:179-197](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L179-L197) —— `fz_trace_fill_path` 的实现：把这次调用写成一段 `<fill_path winding="..." > ... </fill_path>` 的 XML 打印出来。所以 trace device 收到的每次 `fill_path`，都会变成一行可读日志。

#### 4.3.4 代码实践（可运行）

**实践目标**：用 `mutool trace` 把一页内容「驱动」到 trace device，亲眼看到 device 调用流，并把它和虚表回调一一对应。

**操作步骤**：

1. 确认 `mutool trace` 存在：它在 mutool 的分发表里注册为常驻命令（见 u1-l4），入口是 [mutrace.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c) 的 `mutrace_main`。用法见 [mutrace.c:29-35](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c#L29-L35)：
   ```
   usage: mutool trace [options] file [pages]
       -p -    password
       -b -    use named page box (MediaBox, CropBox, ...)
   ```
2. 按 u1-l2 编译出 `mutool`。
3. 找一个简单 PDF（最好是一页只画了几个矩形和一行文字的）。运行：
   ```bash
   ./build/release/mutool trace 你的文件.pdf 1
   ```
4. 阅读输出：每个 XML 元素（`<fill_path>`、`<fill_text>`、`<clip_path>`/`</...>` 对应 `pop_clip`、`<fill_image>`、`<begin_group>`/`<end_group>` 等）都对应一次 device 回调。

**需要观察的现象**：

- 输出是一段以 `<page>` 包裹的 XML 流，里面是 trace device 在 `fz_run_page` 驱动过程中收到的每一次回调（参考 [trace-device.c:179-197](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L179-L197) 的打印格式）。
- 注意「容器类」标签是成对出现的：`<clip_path>` 与后续的 `</clip>`（pop_clip）、`<begin_group>` 与 `<end_group>`——这正是 4.2 节容器栈的「压栈/弹栈」在输出上的投影。
- 如果页面只有正文，你会看到 `run_page_contents` 产生的调用；标注和表单控件如果有，会在正文之后追加。

**预期结果**：你能把 `mutool trace` 的每一行 XML 对应到一个虚表回调，并验证「红矩形→`<fill_path>`」的判断。由于输出依赖具体文档，「待本地验证」具体行数。

#### 4.3.5 小练习与答案

**练习 1**：`fz_run_page` 内部三个子调用的顺序能否调换？为什么是这个顺序？

**参考答案**：顺序是 contents → annots → widgets，且不可随意调换：正文是底层内容，标注叠在正文之上，表单控件又叠在最上层（用户要能点到）。由于三段共用同一个 device 和 ctm，后画的会覆盖先画的，因此必须按「从底到顶」的 z 序绘制，才能得到正确的视觉叠加。

**练习 2**：`fz_run_page` 的 `cookie` 参数有什么用？单线程程序该怎么传？

**参考答案**：cookie 是双向通信通道：应用可置 `cookie->abort=1` 中断渲染，库回填 `progress`/`progress_max`/`errors`/`incomplete`（定义见 [device.h:498-505](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L498-L505)）。单线程、不需要进度/中断的场景直接传 `NULL`（如 mutrace.c 的调用）。多线程渲染时（见 u9-l2）才需要真实 cookie。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来——用 `mutool trace` 观察 device 调用流，反向「解码」一页内容，并据此设计一个最小的自定义 device 骨架。

**步骤**：

1. **准备一个最小测试页**：用任意工具生成一个只含「一个红色实心矩形 + 一行黑色文字」的单页 PDF（命名如 `red.pdf`）。
2. **跑 trace**：`./build/release/mutool trace red.pdf 1 > trace.xml`，把输出重定向到文件。
3. **解码对应关系**：在 `trace.xml` 里找到对应矩形的那段（应为 `<fill_path ...>`，颜色属性里能看到 RGB≈1,0,0），以及对应文字的那段（`<fill_text ...>`）。为每个 XML 标签写一行：「它对应 `struct fz_device` 的哪个回调（如 `fill_path`）、是 4.2 节哪一类（路径/文字/容器）、由 `fz_run_page` 的哪一段（contents/annots/widgets）产生」。
4. **核对你的判断**：用 4.2.4 的表格对照，确认「红矩形→`fill_path`」「文字→`fill_text`」，并指出你**没有**看到的回调（如 `fill_image`、`fill_shade`）以及原因。
5. **设计自定义 device 骨架（画在纸上或写伪代码即可，不必编译）**：参照 [trace-device.c:664-692](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L664-L692) 的构造函数，写出一个「统计 device」的骨架——派生结构体里放两个计数器 `n_path`、`n_text`，在 `fill_path` 回调里 `n_path++`、在 `fill_text` 回调里 `n_text++`，其余回调留空。说明它的构造函数该如何 `fz_new_derived_device` 并只填这两个回调，以及如何用 `fz_run_page` 驱动它来统计一页的路径数与字符数。（这个骨架的完整实现是 u10-l2「自定义 device」的实践任务，这里只需设计。）

**预期收获**：你会切身理解「device = 一组可按需实现的回调；`fz_run_page` 把页面内容驱动成这串回调；换个 device 就换个用途」。这正是 MuPDF 能用一套代码同时做渲染、文本提取、调试追踪的根本原因。

## 6. 本讲小结

- **device 是绘图指令的「访客/消费者」**：`struct fz_device` 是一张函数指针虚表，格式解释器是「生产者」，device 决定指令最终变成像素、文本、日志还是待回放的列表。
- **必须用包装函数调用**：`fz_fill_path(ctx, dev, ...)` 而非 `dev->fill_path(...)`。包装函数做三件事——判空、异常隔离、**出错自禁用**（`fz_disable_device` 清空整张虚表）。
- **回调按职能分组**：路径（fill/stroke/clip_path）、文字（fill/stroke/clip_text、ignore_text）、图像着色（fill_image/fill_shade/fill_image_mask）、容器（clip→pop_clip、mask、group、tile）、元信息（layer/structure/metatext）。
- **容器栈由框架统一维护**：`clip_path`/`begin_group` 等先把「与祖先 scissor 取交」的矩形压栈，再转发；配对必须平衡，否则 `device calls unbalanced` 并自禁用。
- **生命周期四步固定**：`fz_new_*_device` → `fz_run_page` 驱动 → `fz_close_device`（冲刷输出）→ `fz_drop_device`（释放）。跳过 close 会得到残缺输出加 `dropping unclosed device` 警告。
- **`fz_run_page` 三合一**：依次把「正文 + 标注 + 表单控件」经同一 device、同一 ctm 跑一遍，叠加成整页；cookie 提供多线程进度与中断，单线程传 NULL。

## 7. 下一步学习建议

- **u4-l2 显示列表**：device 的「录制」后端——`fz_new_list_device` 把调用存进列表，`fz_run_display_list` 再回放。学完你会更懂「一次录制、多次回放」的价值，并理解 `passthrough` device 的组合用法。
- **u4-l3 draw device 与 pixmap**：device 的「渲染」后端——`fz_new_draw_device` 如何把回调真正光栅化进 `fz_pixmap` 像素。这是把本讲的抽象回调落到具体像素的关键一跃。
- **u4-l4 mudraw 渲染管线**：以 `mudraw.c` 为主线，把 context、document、display-list、draw device、stext 串成工程级管线。
- **u10-l2 自定义 device**：本讲综合实践里「统计 device」的完整实现，以 `trace-device.c` 与 `list-device.c` 为范例。
- **u7-l4 资源与内容流解释**：本讲一直说的「解释器发出回调」的生产者一侧——`pdf-op-run.c` 如何把 PDF 操作符（`re`/`f`/`Tj` 等）映射到 `fill_path`/`fill_text` 等回调。
