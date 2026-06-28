# 资源、页面与内容流解释

## 1. 本讲目标

本讲是「PDF 对象模型深入」单元的最后一讲，要把前几讲孤立的知识点拼成一条完整的渲染链路：从磁盘上的 PDF 页面对象，到页面引用的资源字典，再到那条被逐操作符解释的内容流，最终落地为 `fz_device` 上的绘图调用。

读完本讲你应该能够：

- 说清页面资源字典（`/Resources`）的组织方式，以及「名字 → 真实对象」的解析过程。
- 描述 `pdf_page` 这一 fitz 页面派生结构承载了什么，`pdf_run_page_contents` 在解释内容流之前做了哪些准备工作。
- 用自己的话讲清 PDF 内容流操作符（如 `m`/`l`/`re`/`f`/`BT`/`Tf`/`Tj`/`ET`）是如何经过「词法分派 → processor 回调 → run processor 翻译」三步，最终变成 `fz_fill_path`、`fz_fill_text` 等 device 调用的。

本讲承接 [u4-l1 fz_device：显示设备抽象](u4-l1-device-model.md)（device 是绘图指令的消费者）与 [u7-l1 pdf_obj：PDF 对象类型系统](u7-l1-pdf-object-model.md)（如何读取字典/数组/间接对象）。

## 2. 前置知识

阅读本讲前，请确认你已经理解下面几个概念（前几讲已建立）：

- **device 是访客/消费者**：PDF 解释器（生产者）发出统一的绘图指令（填路径、画文字、贴图），device（消费者）把它分流到位图、文本、显示列表等后端。
- **pdf_obj 借用引用**：`pdf_dict_get` 等返回的是「不增引用计数、不可 drop」的借用指针。
- **CTM（当前变换矩阵）**：把 72 dpi 用户空间映射到设备像素的 `fz_matrix`。
- **图形状态栈**：PDF 用 `q`/`Q` 压栈/弹栈保存恢复图形状态，内容流里大量状态是「当前值」。

本讲还会引入一个新抽象 **processor（操作符处理器）**，它是 device 之上的一层，专门用来消费 PDF 操作符流。理解「device 接收的是几何指令，processor 接收的是 PDF 操作符」这一层差，是本讲的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/pdf/page.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/page.h) | 声明 `pdf_page` 结构与 `pdf_run_page`、`pdf_page_resources`、`pdf_page_contents` 等接口。 |
| [include/mupdf/pdf/interpret.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h) | 声明 `pdf_processor` 操作符虚表、资源栈 `pdf_resource_stack`、`pdf_lookup_resource`、`pdf_process_contents`。 |
| [include/mupdf/pdf/resource.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/resource.h) | 声明字体/颜色空间/图案/图像等资源的加载与去重接口。 |
| [source/pdf/pdf-page.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-page.c) | 实现 `pdf_page_resources`/`pdf_page_contents` 等页面对象访问器与页面树管理。 |
| [source/pdf/pdf-run.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c) | 实现 `pdf_run_page` / `pdf_run_page_contents`，把页面装配成一条「建 processor → 跑内容流」的调用。 |
| [source/pdf/pdf-interpret.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c) | 内容流的词法分派循环：`pdf_lex` 取 token → `pdf_process_keyword` 大 switch → 调 `proc->op_X`。 |
| [source/pdf/pdf-resources.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-resources.c) | **写侧**的资源去重哈希表（字体/颜色空间/图像），供 pdfwrite 避免重复添加。 |
| [source/pdf/pdf-op-run.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c) | `pdf_processor` 的「渲染派生」实现：把每个操作符翻译成 device 调用。本讲的核心。 |

> 一个容易踩坑的点：`pdf-resources.c` 名字里有 "resources"，但它实现的是**写入 PDF 时**的资源去重（哈希表按内容指纹查重），而不是渲染时「按名字查资源」的运行时查找。运行时查找是 `pdf_lookup_resource`，定义在 `pdf-interpret.c`。本讲第 4.1 节会讲清两者分工。

---

## 4. 核心概念与源码讲解

### 4.1 资源字典

#### 4.1.1 概念说明

PDF 的页面内容流里，所有「重资产」都**只用一个名字引用**，真正的定义放在别处。比如下面这段内容流：

```
/F1 12 Tf          % 用名字 F1 选 12 号字体
1 0 0 rg           % 设填充色为红
100 700 Td         % 文本定位
(Hello) Tj         % 显示文字
```

这里的 `/F1` 只是一个名字，字体本体不在内容流里。这些名字到真实对象的映射表，就是页面的**资源字典** `/Resources`。它是一个字典的字典，按类型分桶：

| 子字典键 | 存放内容 | 内容流里典型的引用操作符 |
| --- | --- | --- |
| `/Font` | 字体对象 | `Tf` |
| `/XObject` | 图片（Image）与表单（Form） | `Do` |
| `/ColorSpace` | 颜色空间定义 | `CS`/`cs` |
| `/ExtGState` | 扩展图形状态（透明度、混合模式等） | `gs` |
| `/Pattern` | 图案 | 用 `Pattern` 名做 `SC`/`sc` |
| `/Shading` | 渐变着色 | `sh` |
| `/Properties` | 标记内容属性表 | `BDC`/`DP` |

这种「内容流只放名字、定义放资源字典」的设计有两点好处：

1. **复用**：同一张图、同一个字体可以在一页里被引用多次，文件里只存一份。
2. **隔离**：内容流可以被压缩、被流式处理，而资源对象可以按需懒加载（见 [u7-l2](u7-l2-pdf-xref.md) 的 xref 间接对象解析）。

#### 4.1.2 核心流程

资源解析的关键是**「资源栈」**。为什么需要栈？因为 Form XObject 自带 `/Resources`：当内容流里遇到 `/Fm1 Do` 进入一个表单时，表单内部用的 `/F1` 可能是**表单自己的**字体，也可能是**页面的**字体。PDF 规定：先查内层（表单），查不到再查外层（页面）。于是 MuPDF 维护一个 `pdf_resource_stack`，进入表单时压栈、退出时弹栈。

```
运行时资源查找流程：

  pdf_lookup_resource(stack, type=Font, name="F1")
        │
        ▼
   遍历 stack（从栈顶→栈底，即从最内层→最外层）
        │
        ├─ 取当前层 resources["Font"] 子字典
        ├─ 在子字典里查 "F1" → 命中则返回（借用引用）
        └─ 未命中 → 转到 stack->next 下一层
        │
        ▼
   全部未命中 → 返回 NULL（调用方通常抛 FZ_ERROR_SYNTAX）
```

注意查找返回的是**借用引用**（不 keep、不可 drop），与 [u7-l1](u7-l1-pdf-object-model.md) 讲的 `pdf_dict_get` 语义一致。

#### 4.1.3 源码精读

资源栈的节点定义极其简洁，就是一个带 `next` 的链表节点，挂着一个资源字典：

[include/mupdf/pdf/interpret.h:43-47](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L43-L47) —— `pdf_resource_stack` 就是 `next` + `resources`，整条链代表「页面 → 表单 → 嵌套表单」的资源嵌套。

运行时查找的实现只有十几行，核心是一个 `while (stack)` 循环逐层下沉：

[source/pdf/pdf-interpret.c:33-49](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L33-L49) —— 先取本层 `resources[type]` 子字典，再在其中按名字找；命中即返回，否则 `stack = stack->next` 转下一层。这就是 4.1.2 那张流程图的代码形态。

调用点都是「按需」的，散落在 `pdf_process_keyword` 的各个分支里。例如遇到 `Do` 操作符要画一个 XObject 时：

[source/pdf/pdf-interpret.c:1046-1052](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1046-L1052) —— `pdf_process_Do` 调 `pdf_lookup_resource(stack, XObject, name)`，找不到就抛 `cannot find XObject resource`。设置颜色空间（`CS`/`cs`）、字体（`Tf`）、扩展图形状态（`gs`）、图案、着色也都是同样的模式。

页面级 `/Resources` 本身的获取用 `pdf_dict_get_inheritable`，因为 `Resources` 是**可继承的页面树属性**——父页面节点可以声明一份共享资源，子页面省略即继承：

[source/pdf/pdf-page.c:723-726](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-page.c#L723-L726) —— `pdf_page_resources` 沿 `/Parent` 链查找 `Resources`；对比之下 `/Contents`（内容流本体）不可继承，直接 `pdf_dict_get`（[pdf-page.c:728-731](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-page.c#L728-L731)）。这个差别正是 [u7-l1](u7-l1-pdf-object-model.md) 讲的 `pdf_dict_get_inheritable` 的真实用法。

至于 `pdf-resources.c`，它的职责是**写侧去重**：当 pdfwrite 把一个 `fz_font`/`fz_image`/`fz_colorspace` 写进 PDF 时，先按内容指纹（digest）查哈希表，已存在就复用已有的 `pdf_obj`，避免写出重复资源：

[source/pdf/pdf-resources.c:40-59](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-resources.c#L40-L59) —— `pdf_find_font_resource` 用 `fz_font_digest` 算指纹作 key，在 `doc->resources.fonts` 哈希表里查；图片/颜色空间的 `find_*` 同构（[pdf-resources.c:80-111](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-resources.c#L80-L111)）。读写两端各有一套资源机制，别混淆。

#### 4.1.4 代码实践

**实践目标**：直观看到资源字典的「分桶」结构，并验证名字解析。

**操作步骤**：

1. 准备任意一个含文字的 PDF（记为 `a.pdf`），确保已编译出 `mutool`（见 [u1-l2](u1-l2-build-system.md)）。
2. 用 `show` 子命令把第一页的 `/Resources` 字典打印出来：
   ```bash
   mutool show a.pdf trailer/Root/Pages/Kids/0/Resources
   ```
   （`show` 子命令在 [source/tools/mutool.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c) 的 `tools[]` 分发表里登记。）
3. 在输出里找到 `/Font` 子字典，记下其中一个字体名字（如 `/F1`）及其指向的间接对象号。
4. 打开 [source/pdf/pdf-interpret.c:33-49](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L33-L49)，对照 `pdf_lookup_resource` 的循环，用纸笔模拟：当内容流出现 `/F1 12 Tf` 时，`type`=`/Font`、`name`=`"F1"`，循环如何取到步骤 3 看到的那个对象。

**需要观察的现象**：`/Resources` 下应能看到 `Font`、`XObject`、`ColorSpace` 等若干子字典；字体名字与对象号一一对应。

**预期结果**：你能把内容流里的 `/F1`、步骤 2 输出的 `/Font/F1`、以及 `pdf_lookup_resource` 的循环逻辑三者对上号。

> 若手头没有 PDF，可先 `mutool create -o a.pdf` 生成一个空文档，或用 `docs/examples/pdf-create.c` 的思路造一个最小 PDF。本步骤也可纯做源码阅读，不强制运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pdf_page_resources` 用 `pdf_dict_get_inheritable`，而 `pdf_page_contents` 用普通的 `pdf_dict_get`？

**参考答案**：`Resources` 在 PDF 规范里是可继承的页面树属性，父节点可以放一份共享资源供所有子页面用，所以要沿 `/Parent` 链向上找；`Contents`（内容流）不是可继承属性，每页必须自己持有，直接取本页字典即可。

**练习 2**：内容流里出现 `/Im1 Do`，但页面 `/Resources/XObject` 里没有 `Im1`，会发生什么？

**参考答案**：`pdf_process_Do` 调 `pdf_lookup_resource` 在整条资源栈上都找不到，返回 NULL，于是抛 `FZ_ERROR_SYNTAX`（`cannot find XObject resource 'Im1'`）。由于资源栈会一直遍历到页面层，所以即便 `Im1` 定义在父页面资源里也能被继承命中。

---

### 4.2 页面对象

#### 4.2.1 概念说明

`pdf_page` 是 fitz 通用 `fz_page`（见 [u3-l1](u3-l1-document-abstraction.md)）在 PDF 格式上的派生结构。它「既是 fitz 页面，又握着原始 PDF 页面对象」。有了它，通用层（`fz_run_page` 等）只需调用一套格式无关的虚表，PDF 专用层在背后翻译。

一个 PDF 页面字典长这样（关键字段）：

```
<< /Type /Page
   /Parent 2 0 R          % 页面树父节点
   /MediaBox [0 0 595 842]% 物理介质大小（PDF 坐标，原点左下）
   /CropBox [0 0 595 842] % 可见裁剪框
   /Resources << ... >>    % 资源字典（见 4.1）
   /Contents 5 0 R         % 内容流（操作符序列）
   /Rotate 0               % 旋转
   /Annots [ ... ]         % 标注
>>
```

注意 PDF 的坐标系原点在**左下角**、y 轴向上，而 fitz 的坐标系原点在**左上角**、y 轴向下。页面渲染前必须做一次坐标变换把两者对齐。

#### 4.2.2 核心流程

把「页面对象」变成「device 上的画面」要经过四步准备，然后才进入内容流解释：

```
pdf_run_page(page, dev, ctm)
        │  = 正文 + 标注 + 表单控件 三合一
        ▼
pdf_run_page_contents_with_usage_imp(doc, page, dev, ctm)
        │
        ├─ 1. pdf_load_default_colorspaces   处理 /DefaultRGB 等默认色空间重映射
        ├─ 2. pdf_page_transform             算 page_ctm（PDF坐标→fitz坐标）
        │      ctm = concat(page_ctm, 用户ctm)   叠加用户的缩放/旋转
        ├─ 3. 取 resources = pdf_page_resources(page)
        │      取 contents = pdf_page_contents(page)
        ├─ 4. 若 CropBox 比 MediaBox 小 → fz_clip_path 把内容裁到裁剪框
        ├─ 5. proc = pdf_new_run_processor(dev, ctm, ...)
        │      pdf_process_contents(proc, doc, resources, contents)
        │      pdf_close_processor(proc)
        └─ （弹出裁剪、结束透明组）
```

关键点：**用户的缩放/旋转矩阵是和「PDF→fitz 坐标变换」相乘后一起传给 processor 的**，内容流里的坐标全程是 PDF 用户空间，坐标变换在 device 端统一完成。

#### 4.2.3 源码精读

`pdf_page` 结构以 `fz_page super` 起头（C 手写继承），随后是 PDF 专有字段：

[include/mupdf/pdf/page.h:319-331](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/page.h#L319-L331) —— `obj` 持有原始 PDF 页面字典，`transparency`/`overprint` 缓存透明性判定，`annots`/`widgets` 缓存标注与表单控件。这些缓存让「渲染正文」「渲染标注」「渲染控件」可以分别进行而又共享同一份页面对象。

页面渲染的核心编排函数是 `pdf_run_page_contents_with_usage_imp`，下面是它的关键骨架（已删去透明组等细节）：

[source/pdf/pdf-run.c:152-190](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L152-L190) —— 注意它先 `pdf_page_transform` 把 page_ctm 算出来并 `fz_concat(page_ctm, ctm)` 叠加用户矩阵，再取出 `resources` 与 `contents`，必要时用 `fz_clip_path` 把内容裁到 CropBox，最后才 `pdf_new_run_processor` + `pdf_process_contents` 驱动解释。这正是 4.2.2 流程图的代码实现。

外层的 `pdf_run_page` 只是「三合一」封装：

[source/pdf/pdf-run.c:390-419](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L390-L419) —— `pdf_run_page` 依次跑 `pdf_run_page_contents_with_usage_imp`、`pdf_run_page_annots_with_usage_imp`、`pdf_run_page_widgets_with_usage_imp`，与 [u3-l1](u3-l1-document-abstraction.md) 讲的「正文+标注+表单控件三合一」对应。这也解释了为什么 `pdf_run_page_contents`（只跑正文）会单独存在——有时你只想提取/转换正文，不想要标注外观。

#### 4.2.4 代码实践

**实践目标**：理解「解释内容流之前」的准备工作到底有哪几步。

**操作步骤**：

1. 打开 [source/pdf/pdf-run.c:104-190](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L104-L190)。
2. 用笔在纸上列出 `pdf_run_page_contents_with_usage_imp` 在调用 `pdf_process_contents` **之前**做的全部事情。
3. 对每一项写一句话说明「为什么这一步必须先做」。例如「裁剪到 CropBox」是因为 PDF 允许 MediaBox 比 CropBox 大（留出印刷出血标记），不裁就会把出血也画出来。

**需要观察的现象**：你会发现有 5 类准备工作（默认色空间、坐标变换、取资源、取内容、裁剪/透明组）。

**预期结果**：能复述「内容流解释不是裸跑，而是先搭好坐标、色空间、裁剪舞台」。这一步纯源码阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`pdf_run_page` 和 `pdf_run_page_contents` 有什么区别？什么时候该用后者？

**参考答案**：前者把页面正文、标注外观、表单控件外观三部分依次画到 device，得到「和肉眼看到一致」的完整画面；后者只跑 `/Contents` 内容流，不含标注与控件。做文本提取、内容流改写（`mutool clean`）、或只关心矢量正文时，用 `pdf_run_page_contents` 更合适，避免标注外观干扰。

**练习 2**：为什么用户传入的缩放矩阵要和 `page_ctm` 相乘，而不是直接覆盖？

**参考答案**：`page_ctm` 承担的是「PDF 坐标系（左下原点）→ fitz 坐标系（左上原点）」的格式转换，外加页面的 `/Rotate`。用户的缩放/旋转是叠加在这之上的额外变换。若直接覆盖会丢掉坐标翻转和页面旋转，导致画面上下颠倒、旋转角度丢失。相乘（`fz_concat(page_ctm, ctm)`）才能把两者正确叠加。

---

### 4.3 内容流解释

#### 4.3.1 概念说明

内容流是一条**操作符序列**（注意是序列，不是对象树）。它有点像 PostScript 的精简版：先压一些数字到操作数栈，再跟一个操作符关键字消费它们。例如画一个红色填充矩形：

```
1 0 0 rg           % 压 1,0,0，rg 把填充色设为红
100 700 50 30 re   % 压 100,700,50,30，re 画矩形
f                  % f 填充当前路径
```

MuPDF 把「解释内容流」拆成了**三层**，这是本讲最重要的架构认知：

1. **词法分派层**（`pdf-interpret.c`）：`pdf_lex` 逐个切 token；数字压栈，关键字交给 `pdf_process_keyword` 的大 `switch`。
2. **processor 抽象层**（`pdf_processor`，`interpret.h`）：一张约 90 个 `op_X` 函数指针的虚表。它是「PDF 操作符的访客」。
3. **run processor 派生层**（`pdf-op-run.c`）：processor 的一种具体实现，把每个操作符翻译成 device 调用。

为什么要中间夹一层 processor？因为同一份内容流可能要被**多种方式处理**：渲染到屏幕（run processor）、原样录制到 buffer（buffer processor）、改色后重写（color filter）、规范化后清理（sanitize filter）……这些都实现同一张 `pdf_processor` 虚表，词法分派层完全不关心后端是谁。这与 device 的「访客模式」是同一个设计思想，只是抽象层级更高——device 接收几何指令，processor 接收 PDF 操作符。

#### 4.3.2 核心流程

以「画一个填充矩形」和「显示一行文字」为例，跟踪整条链路：

```
=== 画矩形：100 700 50 30 re  f ===

词法层 pdf_process_stream：
    读到 100/700/50/30 → 压入 csi->stack
    读到 "re" → pdf_process_keyword:
        case B('r','e'): proc->op_re(ctx, proc, s[0],s[1],s[2],s[3]);

run processor pdf_run_re：
    fz_rectto(ctx, pr->path, x, y, x+w, y+h)   % 往当前路径 pr->path 追加一个矩形

    读到 "f" → pdf_process_keyword:
        case A('f'): proc->op_f(...) → pdf_run_F → pdf_show_path(doclose=0, dofill=1, ...)

pdf_show_path（路径转 device 调用的桥）：
    根据 gstate->fill.kind 分派：
        PDF_MAT_COLOR → fz_fill_path(ctx, dev, path, even_odd, ctm,
                                     fill.colorspace, fill.v, fill.alpha, ...)
    （PATTERN/SHADE 则 clip + 递归画图案/着色）
```

```
=== 显示文字：BT  /F1 12 Tf  100 700 Td  (Hello) Tj  ET ===

BT → pdf_run_BT: 重置文本矩阵 tos.tm = tos.tlm = identity
/F1 12 Tf → pdf_process_keyword 的 Tf 分支:
        fontobj = pdf_lookup_resource(stack, Font, "F1")   % 4.1 的资源查找
        font = pdf_try_load_font(fontobj)                   % 加载字体
        proc->op_Tf(name, font, size) → pdf_run_Tf: 存入 gstate->text
100 700 Td → pdf_run_Td: 平移文本矩阵
(Hello) Tj → pdf_run_Tj → pdf_show_string → 逐字符:
        pdf_show_char(cid):
            gid = pdf_tos_make_trm(...)        % 算字形渲染矩阵 trm
            fz_show_glyph_aux(tos.text, font, trm, gid, ...)  % 把字形累积进 tos.text
ET → pdf_run_ET → pdf_flush_text_imp:
        根据 text_mode（0=填充,1=描边,...）与 fill.kind:
            PDF_MAT_COLOR → fz_fill_text(ctx, dev, text, ctm,
                                         fill.colorspace, fill.v, fill.alpha, ...)
```

两条链的共同结构是：**操作符只负责「更新状态 / 累积几何」，真正发 device 调用的时机被推迟到「路径填充」或「文本对象结束」**。路径在 `re`/`m`/`l` 时只往 `pr->path` 追加点，直到 `f`/`S`/`n` 才触发 `pdf_show_path`；文字在 `Tj` 时只往 `tos.text` 累积字形，直到 `ET` 才 `fz_fill_text`。这种「累积—触发」的两阶段是理解 PDF 渲染的关键。

#### 4.3.3 源码精读

**词法分派层**——`pdf_process_keyword` 是一个巨大的 `switch`，每个 `case` 对应一个操作符关键字，调用虚表上对应的 `op_X`：

[source/pdf/pdf-interpret.c:1309-1372](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1309-L1372) —— 路径构造操作符 `m`/`l`/`c`/`v`/`y`/`h`/`re` 都从操作数栈 `s` 取参数，转调 `proc->op_re` 等；其中 `case B('r','e')` 正是矩形操作符的分派点。注意每个调用前都有 `if (proc->op_re)` 判空——这让 filter 派生可以只覆盖部分操作符。

而驱动这一切的词法循环在 `pdf_process_stream`：

[source/pdf/pdf-interpret.c:1527-1567](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1527-L1567) —— `do { tok = pdf_lex(ctx, stm, buf); ... }` 不断取 token；整数/浮点压入 `csi->stack`，名字记入 `csi->name`，字符串记入 `csi->string`，遇到 `PDF_TOK_KEYWORD` 就调 `pdf_process_keyword` 并 `pdf_clear_stack`。文本数组（`TJ` 的 `[...]`）还有特殊处理。

外层的 `pdf_process_contents` 负责把资源压栈、开流、调 `pdf_process_raw_contents`：

[source/pdf/pdf-interpret.c:1808-1849](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1808-L1849) —— `pdf_process_raw_contents` 建一个栈上的 `pdf_csi`（解释器状态，含操作数栈、名字/字符串缓冲），逐个内容流对象（页面 `/Contents` 可以是数组，多段拼接）跑 `pdf_process_stream`，最后 `pdf_process_end` 把未闭合的 `q` 状态补齐弹出。

**processor 虚表**——`pdf_processor` 是一张庞大的函数指针表，按操作符分组（图形状态、路径构造、路径绘制、裁剪、文本、颜色、图像/xobject、标记内容）：

[include/mupdf/pdf/interpret.h:103-151](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L103-L151) —— 注意 `op_re`（[L110](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L110)）、`op_Tf`（[L137](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L137)）、`op_TJ`（[L148](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L148)）、`op_Tj`（[L149](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L149)）。词法层只认这张表，不认具体实现，因此 run/buffer/output/filter 四类 processor 能互换。

**run processor 派生层**——`pdf_run_processor` 以 `pdf_processor super` 起头，额外持有 `dev`、`path`（当前路径）、`tos`（文本对象状态）、`gstate`（图形状态栈）：

[source/pdf/pdf-op-run.c:113-168](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L113-L168) —— `pr->path` 是「正在累积的当前路径」，`pr->tos` 是「正在累积的当前文本」，`pr->gstate/gtop` 是图形状态栈（`q`/`Q` 压弹）。这三个字段正是 4.3.2「累积—触发」模型的载体。

构造时把虚表每个槽位接到对应的 `pdf_run_X`：

[source/pdf/pdf-op-run.c:3397-3442](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L3397-L3442) —— `proc->super.op_re = pdf_run_re`、`op_F = pdf_run_F`、`op_Tf = pdf_run_Tf`、`op_Tj = pdf_run_Tj`、`op_TJ = pdf_run_TJ`，一一对应。

**矩形操作符**——`re` 把矩形追加到当前路径，仅此而已（不绘制）：

[source/pdf/pdf-op-run.c:2839-2843](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2839-L2843) —— `pdf_run_re` 调 `fz_rectto(ctx, pr->path, x, y, x+w, y+h)`；同区的 `m`/`l`（[2804-2814](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2804-L2814)）分别调 `fz_moveto`/`fz_lineto`。它们都在「累积路径」。

**路径填充桥**——`f` 操作符触发 `pdf_show_path`，它根据填充材质类型分派到 `fz_fill_path`：

[source/pdf/pdf-op-run.c:2859-2865](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2859-L2865) 与 [source/pdf/pdf-op-run.c:1018-1024](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1018-L1024) —— `pdf_run_F`/`pdf_run_f` 都以 `dofill=1` 调 `pdf_show_path`；后者在 `PDF_MAT_COLOR` 分支调 `fz_fill_path(ctx, pr->dev, path, even_odd, gstate->ctm, fill.colorspace, fill.v, fill.alpha, ...)`。`f` 与 `F` 在 PDF 规范里语义相同（都是 nonzero 填充），所以这里实现也完全一致；描边走 `fz_stroke_path`（[L1057](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1057)）。这就是「矩形 → device 填充路径」链路的最后一跳。

**文字操作符**——`Tf` 选字体、`Tj` 显示字符串、`BT`/`ET` 定界文本对象：

[source/pdf/pdf-op-run.c:2925-2935](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2925-L2935) —— `pdf_run_BT` 重置文本矩阵；`pdf_run_ET` 调 `pdf_flush_text` 把累积的字形真正发到 device。

[source/pdf/pdf-op-run.c:2974-2979](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2974-L2979) —— `pdf_run_Tf` 把加载好的字体存进 `gstate->text.font`。注意 `pdf_run_Tf` 收到的已经是**加载完成的 `pdf_font_desc *`**，字体加载（含资源查找）发生在词法层的 `Tf` 分支（[pdf-interpret.c:1408-1414](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1408-L1414)）。

[source/pdf/pdf-op-run.c:3034-3038](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L3034-L3038) —— `pdf_run_Tj` 转调 `pdf_show_string`，后者逐字符调 `pdf_show_char`：

[source/pdf/pdf-op-run.c:1439-1439](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1439-L1439) —— `pdf_show_char` 调 `fz_show_glyph_aux(ctx, pr->tos.text, ...)` 把每个字形（含其渲染矩阵 `trm`）累积进 `pr->tos.text`，**此时仍未发 device 调用**。

直到 `ET`，`pdf_flush_text_imp` 才把累积的文本一次性发出：

[source/pdf/pdf-op-run.c:1195-1203](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1195-L1203) —— `pdf_flush_text_imp` 按 `text_mode`（0=填充、1=描边、3=不可见……）和 `fill.kind` 分派，`PDF_MAT_COLOR` + 填充模式即调 `fz_fill_text(ctx, pr->dev, text, gstate->ctm, fill.colorspace, fill.v, fill.alpha, ...)`。这就是「文字 → device 填充文本」链路的最后一跳。

把 4.3.2 的两段伪代码与上面这些真实行号一一对照，你就完整掌握了「PDF 操作符 → device 调用」的翻译过程。

#### 4.3.4 代码实践

**实践目标**（即本讲指定的实践任务）：在 `pdf-op-run.c` 中定位处理矩形（`re`）与显示文本（`Tj`）操作符的函数，并写一段说明：一个「画矩形 + 输出文字」的 PDF 指令是如何被解释为 device 的 `fill_path`/`fill_text` 调用的。

**操作步骤**：

1. 在 [source/pdf/pdf-op-run.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c) 中定位：
   - 矩形操作符函数 `pdf_run_re`（[L2839](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2839)），它调 `fz_rectto` 累积路径。
   - 路径填充桥 `pdf_show_path`（[L957](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L957)），其 `fz_fill_path` 在 [L1022](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1022)。
   - 文本操作符函数 `pdf_run_Tj`（[L3034](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L3034)）→ `pdf_show_string` → `pdf_show_char`（[L1329](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1329)），字形累积在 [L1439](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1439)。
   - 文本刷新桥 `pdf_flush_text_imp`（[L1115](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1115)），其 `fz_fill_text` 在 [L1201](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1201)。
2. 写下这样一段说明（示例文字，你可以扩充）：

   > 给定内容流片段 `1 0 0 rg 100 700 50 30 re f BT /F1 12 Tf (Hi) Tj ET`：词法层把 `1 0 0` 压栈后由 `rg` 设填充色为红；`100 700 50 30` 压栈后由 `re` 调 `pdf_run_re` → `fz_rectto` 把矩形追加进 `pr->path`；随后 `f` 调 `pdf_run_F` → `pdf_show_path(dofill=1)`，在 `fill.kind == PDF_MAT_COLOR` 分支发出 `fz_fill_path(dev, path, …)`，红色矩形由此落地。文本部分：`BT` 重置文本矩阵，`Tf` 经词法层 `pdf_lookup_resource` 查到 `/F1` 字体并加载后存入 `gstate->text`；`(Hi)` 由 `Tj` → `pdf_show_string` 逐字符调 `pdf_show_char`，每个字形经 `pdf_tos_make_trm` 算出渲染矩阵后用 `fz_show_glyph_aux` 累积进 `pr->tos.text`；直到 `ET` 调 `pdf_flush_text_imp`，在填充模式下发出 `fz_fill_text(dev, text, …)`。两条链都是「操作符累积、特定时机触发 device 调用」。

3. （可选，验证用）若已编译 `mutool`，用 `trace` 子命令把上述 device 调用打印出来对照：
   ```bash
   mutool trace a.pdf 1
   ```
   `trace` 走的就是 trace device，你能看到形如 `fill_path`、`fill_text` 的调用序列，与本讲描述的触发点一一对应。

**需要观察的现象**：`trace` 输出里，矩形对应一次 `fill_path`，文字对应一次 `fill_text`（在 `ET` 处），顺序与内容流一致。

**预期结果**：你能把源码里的函数调用链和 `mutool trace` 观察到的 device 调用对上号，确认「累积—触发」模型。若未编译或无 PDF，纯源码阅读同样成立。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `re`/`m`/`l` 这些路径构造操作符**不直接**调 device？为什么要等到 `f`/`S`/`n`？

**参考答案**：因为 PDF 把「构造路径」和「绘制路径」分成两阶段。`re` 只定义了一段几何，是否填充、描边、裁剪取决于后续的绘制操作符（`f`/`S`/`W`/`n`），甚至同一路径可以既填又描（`B`）。如果构造时就调 device，就无法支持「先构造、后决定如何画」的语义。所以路径先累积在 `pr->path`，由 `pdf_show_path` 根据绘制操作符的意图一次性发出对应的 device 调用。

**练习 2**：`Tj` 操作符为什么也不直接调 `fz_fill_text`，而要等到 `ET`？

**参考答案**：两个原因。其一，PDF 文本对象（`BT`…`ET`）内可以有多个 `Tj`/`TJ`，它们共享同一套文本状态（字体、字距、文本矩阵），逐字累积到 `tos.text` 更高效，也便于在 `ET` 时统一处理渲染模式（填充/描边/裁剪）与透明组。其二，文本渲染模式可能在 `BT`/`ET` 内变化，`pdf_show_char` 会在模式变化时先 flush 已累积的部分（见 `pdf_show_char` 开头对 `render` 变化的判断），所以刷新时机既可能是 `ET`，也可能是模式切换点。

**练习 3**：`pdf_processor` 这层抽象除了 run processor，还可能有哪些派生？各自用途是什么？

**参考答案**：至少有四类。① buffer processor：把操作符流原样录制进 `fz_buffer`（用于复制内容流）。② output processor：把操作符流写到 `fz_output`（序列化）。③ color filter：拦截颜色/图像操作符做改色后转发给下游 processor。④ sanitize filter：补齐失衡的 `q`/`Q`、去重冗余颜色算子，输出「干净等价」的操作符流（`mutool clean` 走的就是这类 filter 链）。它们都实现同一张 `pdf_processor` 虚表，可以首尾相接组成 filter 链。

---

## 5. 综合实践

**任务**：用一张完整的调用链图，把本讲三个模块串起来，并对照真实工具验证。

**步骤**：

1. 选一个含「一个填充矩形 + 一行文字」的 PDF（或用 `mutool create` / `docs/examples/pdf-create.c` 造一个最小 PDF）。
2. 在纸上画出从 `pdf_run_page` 到 device 调用的**全链路**，至少包含以下节点，并标注每步所在的文件与关键行号：
   - `pdf_run_page`（[pdf-run.c:417](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L417)）
   - `pdf_run_page_contents_with_usage_imp`（[pdf-run.c:104](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L104)）—— 标出「坐标变换 / 取资源 / 取内容 / 裁剪」四项准备
   - `pdf_new_run_processor` + `pdf_process_contents`（[pdf-run.c:186-187](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L186-L187)）
   - `pdf_process_stream` → `pdf_lex` → `pdf_process_keyword`（[pdf-interpret.c:1527](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1527)、[1309](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1309)）
   - 矩形支线：`op_re` → `pdf_run_re` → `fz_rectto`（[pdf-op-run.c:2839](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2839)）→ `op_f` → `pdf_show_path` → `fz_fill_path`（[L1022](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1022)）
   - 文字支线：`op_Tf`（含 `pdf_lookup_resource` 查字体）→ `op_Tj` → `pdf_show_char` → `fz_show_glyph_aux`（[L1439](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1439)）→ `op_ET` → `pdf_flush_text_imp` → `fz_fill_text`（[L1201](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1201)）
3. 在图上用三种颜色分别标出**资源字典**（4.1）、**页面对象**（4.2）、**内容流解释**（4.3）三个模块覆盖的区段。
4. 用 `mutool trace a.pdf 1` 跑一遍，把你画图里最终的 `fill_path`/`fill_text` 节点与 trace 输出对照确认。

**验收标准**：图能自洽地把「PDF 字节」一路讲到「device 函数名」，且每个箭头都能在源码里指到具体行号。

## 6. 本讲小结

- 页面 `/Resources` 字典按类型分桶（Font/XObject/ColorSpace/ExtGState/Pattern/Shading/Properties），内容流只放名字；运行时由 `pdf_lookup_resource` 沿资源栈逐层解析，返回借用引用。`pdf_page_resources` 用 `pdf_dict_get_inheritable` 是因为 `Resources` 可沿页面树继承。
- `pdf-resources.c` 名字像运行时查找，实则是**写侧**的资源去重哈希表（按内容指纹复用 `pdf_obj`）；运行时查找在 `pdf-interpret.c` 的 `pdf_lookup_resource`。读写两端各有一套，别混淆。
- `pdf_page` 是 `fz_page` 的 PDF 派生，`pdf_run_page` = 正文 + 标注 + 控件三合一；`pdf_run_page_contents_with_usage_imp` 在解释内容流前先做「默认色空间、PDF→fitz 坐标变换、取资源、取内容、CropBox 裁剪」五项准备。
- 内容流解释是**三层架构**：词法分派层（`pdf_lex` + `pdf_process_keyword` 大 switch）→ processor 虚表层（`pdf_processor`，约 90 个 `op_X`）→ run processor 派生层（把操作符翻译成 device 调用）。中间夹 processor 是为了同一份内容流可被渲染/录制/过滤等多种处理。
- 路径与文本都遵循「**累积—触发**」两阶段：路径在 `re`/`m`/`l` 时累积进 `pr->path`，到 `f`/`S`/`n` 才 `pdf_show_path` 发出 `fz_fill_path`/`fz_stroke_path`；文字在 `Tj` 时累积进 `pr->tos.text`，到 `ET` 才 `pdf_flush_text_imp` 发出 `fz_fill_text`。

## 7. 下一步学习建议

- 学完本讲，PDF 的「读入 → 渲染」主链路已经完整。建议接着学 **u8 流、过滤与压缩**：内容流和资源对象本身常常是被 `FlateDecode`/`DCT` 等压缩的，`fz_stream` 过滤管线正是 `pdf_process_stream` 读取字节之前的底层。
- 若对「改写内容流」感兴趣，可直接读 `pdf_new_sanitize_filter`/`pdf_new_color_filter`（`interpret.h` 已声明），它们是 processor 抽象在写入/清理场景的应用，配合 [u7-l3](u7-l3-pdf-lex-parse-write.md) 的 `pdf-write.c` 形成「读 → 过滤 → 写」往返。
- 想深入文本定位细节（字形渲染矩阵、字距、双向）的读者，可精读 `pdf_tos_make_trm`（[interpret.h:527](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L527) 的声明）与 `source/pdf/pdf-text-state.c`（`pdf_text_object_state` 的实现）。
- XObject Form 的递归解释（进入表单时压资源栈、`pdf_run_xobject` 的循环检测）是本讲资源栈与 processor 的进阶应用，可在 [pdf-op-run.c:2403](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2403) 的 `pdf_run_xobject` 处继续追。
