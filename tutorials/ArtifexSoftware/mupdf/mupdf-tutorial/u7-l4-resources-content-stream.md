# 资源、页面与内容流解释

> 本讲承接 u4-l1（`fz_device` 显示设备抽象）与 u7-l1（`pdf_obj` 对象类型系统），把这两块拼到一起：一个 PDF 页面到底是怎么「变成」device 上的一串 `fill_path` / `fill_text` 调用的。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清 PDF 页面对象里 `/Resources`、`/Contents` 这两个键分别承载什么、如何被取出来；
- 解释「资源字典栈」的概念，理解嵌套 Form（XObject）为何能引用自己页面里没有的字体；
- 描述 `pdf_processor` 这一「访问者（visitor）」虚表，以及 `pdf_run_processor` 如何把 PDF 操作符翻译成 device 回调；
- 手动追踪一条具体的指令链：`re f`（画矩形并填充）如何走到 `fz_fill_path`，`Tj`（显示文字）如何走到 `fz_fill_text`。

## 2. 前置知识

在进入源码前，先用三个比喻建立直觉。这能帮你把后面枯燥的函数指针表对应到熟悉的画面。

**(1) 页面 = 数据 + 程序。** 一个 PDF 页面对象本质是一个字典（`pdf_obj`），里面有两样关键东西：

- `/Resources`：这一页用到的「军火库」——字体、图片、颜色空间、扩展状态等都登记在这里，按名字取用（类似 Python 里的 `dict`）。
- `/Contents`：一段或几段压缩过的**指令流**，是一段「小程序」。这段程序里只有 `re f Tj` 这样的简短操作符和数字，它本身不含字体定义，而是**按名字去 `/Resources` 里查**。

所以渲染一页，本质是「解释一段程序，程序里每次用到资源就去军火库查名」。

**(2) `pdf_processor` 是一个「访问者」。** 回忆 u4-l1：device 是绘图指令的**消费者**。而 PDF 的内容流是一串**操作符**，需要一个**解释器**把它们拆解成绘图指令。MuPDF 把「如何响应每一个 PDF 操作符」抽象成一张函数指针表 `pdf_processor`——每遇到 `re` 就调 `op_re`、遇到 `Tj` 就调 `op_Tj`。这正是面向对象里**访问者模式（visitor pattern）**在 C 里的手写实现：操作符是「访问事件」，`op_*` 是「visit 方法」。

**(3) 解释器有多个实现，渲染只是其中之一。** `pdf_processor` 这张表有多种实现：把操作符**翻译成 device 调用**的叫 `pdf_run_processor`（本讲主角，渲染用）；把操作符**原样收集进 buffer** 的叫 buffer/output processor（写回 PDF 用）；把操作符**过滤改写后转给下游**的叫各种 filter processor（`mutool clean`/sanitize 用）。本讲聚焦第一种。

> 一句话总结架构：`页面对象(/Resources + /Contents)` → 解释器(`pdf_processor`) → 绘图指令(`fz_device`)。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/mupdf/pdf/page.h` | `pdf_page` 结构体声明，以及 `pdf_page_resources` / `pdf_page_contents` / `pdf_run_page` 等接口 |
| `include/mupdf/pdf/interpret.h` | `pdf_processor` 访问者虚表、`pdf_resource_stack`、解释状态 `pdf_csi` 的定义 |
| `source/pdf/pdf-page.c` | `pdf_page_resources` / `pdf_page_contents` 的实现（从页面对象取字典） |
| `source/pdf/pdf-interpret.c` | 解释循环核心：词法→分发→`pdf_lookup_resource` 查资源 |
| `source/pdf/pdf-op-run.c` | `pdf_run_processor`：把每个操作符翻译成 device 调用的具体实现（本讲最厚的文件） |
| `source/pdf/pdf-run.c` | 顶层入口 `pdf_run_page_contents`，串联「取资源→建 processor→解释」 |
| `source/pdf/pdf-resources.c` | **写侧**资源去重哈希表（注意：与本讲「读侧」资源字典是两个概念，见 4.1.4） |
| `source/fitz/trace-device.c` | trace device，把 device 调用打印成 `<fill_path>`/`<fill_text>` 标签，是本讲实践的观测工具 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**资源字典** → **页面对象** → **内容流解释**。

### 4.1 资源字典：页面的「军火库」

#### 4.1.1 概念说明

PDF 的资源字典（`/Resources`）是页面、Form XObject、Pattern 等内容流的共享依赖库。它本身是一个 `pdf_obj` 字典，标准子键包括：

| 子键 | 内容 | 典型用法 |
|------|------|----------|
| `/Font` | 字体字典（名字 → 字体对象引用） | `Tf` 操作符按名字选字体 |
| `/XObject` | 外部对象（Form / Image） | `Do` 操作符按名字引用 |
| `/ColorSpace` | 命名颜色空间 | `cs`/`CS` 操作符 |
| `/ExtGState` | 扩展图形状态 | `gs` 操作符（透明度、混合模式） |
| `/Pattern` | 图案 | `sc`/`SC` 配 `Pattern` |
| `/Shading` | 渐变着色 | `sh` 操作符 |
| `/Properties` | 可选内容组（OCG）等属性 | marked content 引用 |

关键点：内容流里写的是**名字**（如 `/F1`），名字的真正定义在 `/Resources` 的某个子字典里。解释器的工作就是「拿名字去查字典」。

#### 4.1.2 核心流程

资源查找分两步：

1. 取出本层的 `Resources` 字典（页面层、或当前 Form 层）；
2. 按子键类型（`/Font` 等）+ 名字查找，返回**借用引用**（borrowed，不增计数、不可 drop，见 u7-l1）。

由于 Form 可以嵌套，查找时维护一个**资源栈**：内层 Form 的资源字典压栈，查找时从栈顶（最内层）往栈底（页面层）逐层找，**内层优先、命中即返回**。这就像词法作用域：内层名字遮蔽外层同名名字，找不到才向上回溯。

```
查找 "F1":
  栈顶 Form 的 Resources/Font  ──miss──▶  页面 Resources/Font  ──hit──▶ 返回字体对象
```

#### 4.1.3 源码精读

**取页面的资源字典**——`pdf_page_resources` 沿 `/Parent` 链用可继承查找（`pdf_dict_get_inheritable`），因为 `/Resources` 可写在页面树父节点上让子页共享：

```c
pdf_obj *
pdf_page_resources(fz_context *ctx, pdf_page *page)
{
	return pdf_dict_get_inheritable(ctx, page->obj, PDF_NAME(Resources));
}
```

[include/mupdf/pdf/page.h:138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/page.h#L138) 是该函数的公共声明；实现见 [source/pdf/pdf-page.c:724-727](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-page.c#L724-L727)，它取出本页（可继承）的 `/Resources`。注意它返回的是借用指针。

相比之下 `/Contents` 不需要继承，直接从页面对象取（[source/pdf/pdf-page.c:730-733](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-page.c#L730-L733)）：

```c
pdf_obj *
pdf_page_contents(fz_context *ctx, pdf_page *page)
{
	return pdf_dict_get(ctx, page->obj, PDF_NAME(Contents));
}
```

**沿资源栈按名字查找**——这是解释时真正调用的函数。`stack` 是一条单链表，每个节点持有某一层的 `Resources` 字典：

```c
pdf_obj *
pdf_lookup_resource(fz_context *ctx, pdf_resource_stack *stack, pdf_obj *type, const char *name)
{
	pdf_obj *sub, *obj;
	while (stack)
	{
		sub = pdf_dict_get(ctx, stack->resources, type);   /* 如 /Font */
		if (sub)
		{
			obj = pdf_dict_gets(ctx, sub, name);            /* 如 F1 */
			if (obj)
				return obj;
		}
		stack = stack->next;                               /* 内层没找到，向上层回溯 */
	}
	return NULL;
}
```

[source/pdf/pdf-interpret.c:32-48](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L32-L48)。`type` 是 `/Font`、`/XObject` 等名字单例（见 u7-l1 的 `PDF_NAME(...)` 宏），`name` 是内容流里写的资源名。这段代码完整表达了「内层优先、向上回溯」的词法作用域语义。

资源栈节点的结构本身很朴素（[include/mupdf/pdf/interpret.h:43-47](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L43-L47)）：

```c
struct pdf_resource_stack
{
	pdf_resource_stack *next;
	pdf_obj *resources;
};
```

#### 4.1.4 重要的概念辨析：两种「resources」

源码清单里有一个 `pdf-resources.c`，但它实现的东西**和上面讲的资源字典不是一回事**，初学者极易混淆，必须分清：

| | 读侧：资源字典（本讲主角） | 写侧：资源去重表（`pdf-resources.c`） |
|---|---|---|
| 数据形态 | PDF 文件里的 `/Resources` 字典（`pdf_obj`） | 内存哈希表 `doc->resources.fonts` / `.colorspaces` / `.images` |
| 谁用 | 解释器**读** PDF 时按名查 | pdfwrite **写** PDF 时避免重复添加同一字体/图片 |
| key | 名字字符串（`F1`） | 内容摘要（`fz_font_digest` 的 MD5） |

看 [source/pdf/pdf-resources.c:40-58](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-resources.c#L40-L58) 的 `pdf_find_font_resource`：它对字体算 `fz_font_digest` 得到 16 字节摘要当 key，去哈希表查——这显然是「按内容去重」而非「按名字查」。它的用途是 pdfwrite 在往文档里塞字体前，先看看是否已经塞过等价字体。

> 记住：**渲染（读）走 `pdf_lookup_resource` + `/Resources` 字典；导出（写）走 `pdf_find_*_resource` + 哈希表。** 本讲只关心前者，列出 `pdf-resources.c` 是为了让你在翻代码时不被它误导。

### 4.2 页面对象：从 page obj 到 contents/res

#### 4.2.1 概念说明

`pdf_page` 是 PDF 页面在内存中的表示，它是 u3-l1 里通用层 `fz_page` 的**派生结构体**（C 手写继承：`super` 放在首位）。除了通用层需要的虚表指针，它额外持有指向原始 `pdf_obj` 页面对象的指针，以及一些缓存字段。

页面对象字典（`page->obj` 指向它）的标准键：

- `/Type /Page`、`/Parent`：标识与页面树归属；
- `/MediaBox`、`/CropBox`：页面尺寸与裁剪区（见 u3-l3）；
- `/Resources`：上一节讲的军火库；
- `/Contents`：内容流（一段或一个数组）；
- `/Annots`、`/Group`、`/Rotate` 等：标注、透明组、旋转。

#### 4.2.2 核心流程

渲染一页的「装配」发生在 `pdf_run_page_contents_with_usage_imp`，它做了三件事再交给解释器：

1. **算页面坐标变换**：调用 `pdf_page_transform` 得到把 PDF 坐标系映射到 fitz 坐标系的 `page_ctm`，再与调用者传入的 `ctm`（缩放/旋转）相乘；
2. **取资源与内容**：`pdf_page_resources` + `pdf_page_contents`；
3. **建解释器并驱动**：`pdf_new_run_processor` 创建 `pdf_run_processor`，`pdf_process_contents` 把资源压栈并解释内容流，最后 `pdf_close_processor` 收尾。

中间还有两处「围栏」：若页面带透明，用 `fz_begin_group`/`fz_end_group` 包住整页；若 CropBox 比 MediaBox 小，先 `fz_clip_path` 裁剪。

#### 4.2.3 源码精读

`pdf_page` 结构体定义（[include/mupdf/pdf/page.h:319-331](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/page.h#L319-L331)）：

```c
struct pdf_page
{
	fz_page super;          /* 通用层基类，放首位实现继承 */
	pdf_document *doc;      /* type alias for super.doc */
	pdf_obj *obj;           /* 指向原始页面对象字典 */

	int transparency;
	int overprint;

	fz_link *links;
	pdf_annot *annots, **annot_tailp;
	pdf_annot *widgets, **widget_tailp;
};
```

`obj` 字段是关键——所有「按字典键取值」的操作都从它出发。

装配函数的核心段（[source/pdf/pdf-run.c:133-188](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L133-L188)），节选：

```c
pdf_page_transform(ctx, page, &fitzbox, &page_ctm);
ctm = fz_concat(page_ctm, ctm);                 /* 叠加页面变换与外部变换 */

resources = pdf_page_resources(ctx, page);      /* 取 /Resources */
contents  = pdf_page_contents(ctx, page);       /* 取 /Contents */

/* ...CropBox 裁剪、透明 group 包裹... */

proc = pdf_new_run_processor(ctx, page->doc, dev, ctm, struct_parent_num,
                             usage, NULL, default_cs, cookie, NULL, NULL);
pdf_process_contents(ctx, proc, doc, resources, contents, cookie, NULL);
pdf_close_processor(ctx, proc);
```

这段代码是「页面对象 → 解释器」的衔接点。注意 `pdf_new_run_processor` 把 `dev` 和最终 `ctm` 都塞进 processor，解释时所有 device 调用都用这个合并后的 `ctm`——这就是为什么 u3-l3 讲的变换矩阵最终能落到像素上。

#### 4.2.4 代码实践

阅读 [source/pdf/pdf-run.c:179-193](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L179-L193) 的 CropBox 裁剪逻辑：

1. **实践目标**：理解为何要「先裁剪再解释、解释后再 pop_clip」。
2. **操作步骤**：找到 `if (cropbox.x0 > mediabox.x0 || ...)` 这段，它用 `fz_rectto` 构造一个 CropBox 矩形路径，调 `fz_clip_path` 建立**裁剪围栏**，再在解释完成后 `fz_pop_clip` 撤销。
3. **需要观察的现象**：device 的 clip 是有进有出的「容器」操作（见 u4-l1 的容器栈），`fz_clip_path` 与 `fz_pop_clip` 必须配对。
4. **预期结果**：你能解释「为什么内容流里画到 MediaBox 外、CropBox 内的东西不会显示」——因为内容流本身画到了更大的 MediaBox，但装配函数在 device 上先立了一道 CropBox 裁剪墙。

### 4.3 内容流解释：从操作符到 device 回调

#### 4.3.1 概念说明

内容流是一串 token：数字、名字、字符串和操作符关键字（如 `re`、`f`、`Tj`）。解释它需要：

1. **词法**：把字节流切成 token（`pdf_lex`，见 u7-l3）；
2. **状态机**：维护一个操作数栈 `csi->stack[32]`（数字先入栈）、当前名字 `csi->name`、当前字符串 `csi->string`；
3. **关键字分发**：遇到操作符关键字时，把栈上的操作数弹给对应的 `op_*` 回调。

`pdf_processor` 这张虚表（[include/mupdf/pdf/interpret.h:56-215](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L56-L215)）几乎为 PDF 规范里的每个操作符都准备了一个函数指针：路径构造 `op_m/l/c/v/y/h/re`、路径描绘 `op_S/f/B/n`、裁剪 `op_W`、文字 `op_BT/Tf/Tj/TJ`、颜色 `op_rg/k/g`、xobject `op_Do_form/Do_image`……每个指针可以为 NULL（表示该 processor 实现不关心这个操作符）。

#### 4.3.2 核心流程

完整解释循环（`pdf_process_raw_contents`）：

```
pdf_process_raw_contents
  ├─ pdf_open_contents_stream   打开（可能是解压后的）内容流
  ├─ pdf_process_stream          主循环：
  │     do {
  │       tok = pdf_lex(...)        切一个 token
  │       数字/名字/字符串 → 压栈或存 name/string
  │       关键字 → pdf_process_keyword(...)
  │                   └─ 把操作数弹给 proc->op_XXX(ctx, proc, ...)
  │     } while (tok != EOF)
  └─ pdf_process_end              收尾
```

关键字分发用一个巧妙的技巧：把操作符字符串的前 1~3 个字符**打包成一个 32 位整数 key**，再用 `switch` 跳转，避免大量 `strcmp`：

```c
#define A(a) (a)
#define B(a,b) (a | b << 8)
#define C(a,b,c) (a | b << 8 | c << 16)
```

[source/pdf/pdf-interpret.c:1304-1306](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1304-L1306)。于是 `B('r','e')` 对应矩形操作符 `re`，`B('T','j')` 对应 `Tj`。三个宏分别处理 1/2/3 字符操作符；超过 3 个字符的（如 `BDC`）会走特殊处理。

#### 4.3.3 源码精读

**解释主循环与词法**——[source/pdf/pdf-interpret.c:1549-1566](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1549-L1566) 是核心 do-while，每个 token 都先查 `cookie->abort` 支持中断，再 `pdf_lex` 取 token：

```c
do
{
    /* Check the cookie */
    if (cookie) {
        if (cookie->abort) { tok = PDF_TOK_EOF; break; }
        cookie->progress++;
    }
    tok = pdf_lex(ctx, stm, buf);   /* 切一个 token */
    /* ... 数字/字符串压栈，关键字调 pdf_process_keyword ... */
}
```

**`pdf_process_contents` 的压栈/弹栈框架**（[source/pdf/pdf-interpret.c:1844-1863](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1844-L1863)）：

```c
pdf_process_contents(ctx, proc, doc, in_res, stmobj, cookie, out_res)
{
	if (in_res)
		pdf_processor_push_resources(ctx, proc, in_res);   /* 把本层 Resources 压资源栈 */
	fz_try(ctx)
		pdf_process_raw_contents(ctx, proc, doc, stmobj, cookie);
	fz_always(ctx)
	{
		if (in_res) {
			pdf_obj *ret_res = pdf_processor_pop_resources(ctx, proc);  /* 弹栈 */
			/* ... */
		}
	}
	pdf_catch(ctx) pdf_rethrow(ctx);
}
```

这就是 4.1.2 里「资源栈压栈/弹栈」的发生地。每解释一段内容流（页面层、或某个嵌套 Form），先把它的 Resources 压栈，解释完弹栈——天然实现了词法作用域。

**矩形操作符 `re`**——分发后调 `proc->op_re`（[source/pdf/pdf-interpret.c:1373](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1373)）：

```c
case B('r','e'): if (proc->op_re) proc->op_re(ctx, proc, s[0], s[1], s[2], s[3]); break;
```

`pdf_run_processor` 的实现 `pdf_run_re`（[source/pdf/pdf-op-run.c:2839-2843](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2839-L2843)）只做一件事：往当前路径里追加一个矩形：

```c
static void pdf_run_re(fz_context *ctx, pdf_processor *proc, float x, float y, float w, float h)
{
	pdf_run_processor *pr = (pdf_run_processor *)proc;
	fz_rectto(ctx, pr->path, x, y, x+w, y+h);   /* 构造路径，但还不画 */
}
```

注意 `re` **只构造路径，不输出任何 device 调用**——它把矩形累积在 `pr->path` 里。真正画出来要等描绘操作符。

**填充操作符 `f`**——`pdf_run_f`（[source/pdf/pdf-op-run.c:2865-2869](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2865-L2869)）把四个布尔参数传给统一的 `pdf_show_path`：

```c
static void pdf_run_f(fz_context *ctx, pdf_processor *proc)
{
	pdf_run_processor *pr = (pdf_run_processor *)proc;
	pdf_show_path(ctx, pr, 0, 1, 0, 0);   /* doclose=0, dofill=1, dostroke=0, even_odd=0 */
}
```

`pdf_show_path` 是路径描绘的总枢纽（[source/pdf/pdf-op-run.c:956-1048](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L956-L1048)）。在 `dofill` 分支里，根据当前填充材质（纯色 / 图案 / 着色）走不同路径，纯色时终于发出了我们追踪的目标 device 调用：

```c
case PDF_MAT_COLOR:
	fz_fill_path(ctx, pr->dev, path, even_odd, gstate->ctm,
		gstate->fill.colorspace, gstate->fill.v, gstate->fill.alpha, gstate->fill.color_params);
	break;
```

[source/pdf/pdf-op-run.c:1021-1024](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1021-L1024)。这就是 `re f` → `fz_fill_path` 的终点。`fz_fill_path` 是 u4-l1 讲过的 device 包装函数，它内部会判空、异常隔离，最终调用具体 device（draw device 光栅化、trace device 打印标签等）的虚表。

> 完整矩形链：`re`（[op-run:2839](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2839) 构造路径）→ `f`（[op-run:2865](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2865)）→ `pdf_show_path`（[op-run:956](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L956)）→ `fz_fill_path`（[op-run:1022](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1022)）。

**文字操作符 `Tj`**——分发见 [source/pdf/pdf-interpret.c:1430-1438](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1430-L1438)，把字符串交给 `op_Tj`：

```c
case B('T','j'):
	if (proc->op_Tj) {
		if (csi->string_len > 0)
			proc->op_Tj(ctx, proc, csi->string, csi->string_len);
		else
			proc->op_Tj(ctx, proc, pdf_to_str_buf(ctx, csi->obj), pdf_to_str_len(ctx, csi->obj));
	}
	break;
```

`pdf_run_Tj` 把字符串转给 `pdf_show_string`（[source/pdf/pdf-op-run.c:3034-3038](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L3034-L3038)）。注意字符串里的字节不是 Unicode，而是**字符代码（character code）**，要靠当前字体的编码表才能映射到字形（这就是 `Tj` 之前必须先 `Tf` 选字体的原因）。

文字链比矩形链长，因为要把字节串**逐字符**解码成字形、累积进一个 `fz_text` 对象，再一次性 flush：

```
Tj → pdf_run_Tj (3034)
   → pdf_show_string (1593) → show_string → 逐字节解码出 cid
   → pdf_show_char (1329)        每个字符：pdf_tos_make_trm 算字形矩阵，追加进 tos.text
   → pdf_flush_text → pdf_flush_text_imp (1115)   rendermode=0 时 dofill=1
   → fz_fill_text (1201)         把累积的 fz_text 交给 device
```

`pdf_flush_text_imp` 根据**文字渲染模式（text_mode / Tr）**决定填充/描边/裁剪/不可见（[source/pdf/pdf-op-run.c:1140-1150](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1140-L1150)）：模式 0 是填充（最常见的可见文字）。在 dofill 分支，纯色材质时发出：

```c
case PDF_MAT_COLOR:
	fz_fill_text(ctx, pr->dev, text, gstate->ctm,
		gstate->fill.colorspace, gstate->fill.v, gstate->fill.alpha, gstate->fill.color_params);
	break;
```

[source/pdf/pdf-op-run.c:1200-1203](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1200-L1203)。这就是 `Tj` → `fz_fill_text` 的终点。

> 完整文字链：`Tj`（[op-run:3034](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L3034)）→ `pdf_show_string`（[op-run:1593](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1593)）→ `pdf_show_char`（[op-run:1329](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1329)）→ `pdf_flush_text_imp`（[op-run:1115](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1115)）→ `fz_fill_text`（[op-run:1201](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1201)）。

**字体选择 `Tf`**——`Tj` 之前必先 `Tf`。它的分发会**先查资源**再调回调（[source/pdf/pdf-interpret.c:1403-1420](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1403-L1420)）：

```c
case B('T','f'):
	if (proc->op_Tf) {
		pdf_obj *fontobj;
		fontobj = pdf_lookup_resource(ctx, proc->rstack, PDF_NAME(Font), csi->name);  /* 查 /Resources/Font/<name> */
		if (pdf_is_dict(ctx, fontobj))
			font = pdf_try_load_font(ctx, csi->doc, proc->rstack, fontobj, csi->cookie);
		else
			font = pdf_load_hail_mary_font(ctx, csi->doc);   /* 查不到字体时兜底 */
		/* ... proc->op_Tf(ctx, proc, csi->name, font, s[0]); ... */
	}
	break;
```

这正是 4.1 节资源查找的真实调用点：`csi->name` 是内容流里的字体名（如 `F1`），`pdf_lookup_resource` 沿资源栈查到字体对象字典，再由 `pdf_try_load_font` 加载成 `pdf_font_desc`。`pdf_run_Tf`（[source/pdf/pdf-op-run.c:2974-2981](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2974-L2981)）把它存进图形状态的 `text.font`。

**XObject `Do` 的递归**——`Do` 引用 Form 时，会带着 Form 自己的 Resources 递归进 `pdf_process_contents`，从而把 Form 的资源字典压栈（[source/pdf/pdf-op-run.c:2504-2530](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2504-L2530)）：

```c
resources = pdf_xobject_resources(ctx, xobj);
if (!resources)
	resources = page_resources;                  /* Form 没自带资源就回退到页面资源 */
/* ... */
pdf_process_contents(ctx, (pdf_processor*)pr, doc, resources, xobj, pr->cookie, NULL);
```

配合 4.1 的 `pdf_process_contents` 压栈逻辑，这就解释了「嵌套 Form 如何看到自己页面里没有的字体」——它的 Resources 在解释期间被压在栈顶。

#### 4.3.4 代码实践

本讲的主实践（详见第 5 节）要求你定位 `re` 与 `Tj` 两条链并写成说明。这里先给一个**可运行的观测手段**，让你眼见为实：

`mutool trace` 会用 trace device 把每一条 device 调用打印成 XML 标签。trace device 的 `fz_trace_fill_path` 输出 `<fill_path ...>`（[source/fitz/trace-device.c:179-196](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L179-L196)），`fz_trace_fill_text` 输出 `<fill_text ...>`（[source/fitz/trace-device.c:277-283](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L277-L283)）：

```c
fz_write_printf(ctx, out, "<fill_path");
if (even_odd)
	fz_write_printf(ctx, out, " winding=\"eofill\"");
else
	fz_write_printf(ctx, out, " winding=\"nonzero\"");
/* ... 颜色、ctm、路径坐标 ... */
fz_write_printf(ctx, out, "</fill_path>\n");
```

这意味着：**你可以在内容流里看到 `re f`，在 trace 输出里直接看到对应的 `<fill_path>`**，从而把「操作符」和「device 调用」逐条对上号。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `re` 操作符本身不产生任何 device 调用？如果内容流里只有 `100 100 50 50 re` 而没有任何描绘操作符，会发生什么？

**答案**：`re` 只是把矩形追加进 `pr->path`（[op-run:2842](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2842)），路径是「累积的草稿」。只有遇到描绘操作符（`f`/`S`/`B`/`n` 等）才在 `pdf_show_path` 里真正输出 `fz_fill_path`/`fz_stroke_path`。若只有 `re` 没有描绘符，这条路径会被丢弃（下一个路径操作会 `fz_new_path` 新建，旧的无人引用），页面上看不到任何矩形——这与 PDF 规范一致：路径必须被描绘才可见。

**练习 2**：`Tj` 的字符串字节是「字符」还是「字形」？为什么需要 `Tf` 先选字体？

**答案**：是**字符代码（character code）**，不是 Unicode 也不是字形索引。`pdf_show_char`（[op-run:1329](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1329)）需要 `gstate->text.font`（由 `Tf` 设置）的编码表，才能把字节解码成字形 gid（`pdf_tos_make_trm` 内部完成）。没有 `Tf`，`pdf_show_string` 会因 `fontdesc == NULL` 直接 `fz_warn` 返回、不画任何字（[op-run:1598-1602](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1598-L1602)）。

**练习 3**：关键字分发为什么用 `B('r','e')` 这种打包整数而不用 `strcmp("re", word)`？

**答案**：性能。内容流里关键字极多，逐个 `strcmp` 是 O(n×m) 的字符串比较。把 1~3 字符的关键字打包进一个 32 位整数（[interpret:1304-1306](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1304-L1306)），编译器可以把 `switch` 优化成跳转表，比较退化为整数相等判断，开销远低于字符串比较。

## 5. 综合实践

**实践任务**：把「画矩形 + 输出文字」这条最典型的指令链完整走通——既在源码里追踪函数调用，又用 `mutool` 工具亲眼看到对应的 device 调用，最后写一段说明。

### 步骤 1：准备一个最小 PDF

找一份含有一段矩形和一行文字的 PDF（任何排版软件导出的简单文档都行）。假设文件名为 `sample.pdf`。

### 步骤 2：查看解压后的内容流与资源

用 `mutool show` 看第 1 页的页面对象、内容流和资源（命令行的精确子命令语法请**待本地验证**，常见形式如下）：

```bash
mutool show sample.pdf 1            # 打印第 1 页对象，含 /Resources、/Contents
mutool show sample.pdf 1/Contents   # 解压并打印内容流，应能看到 re f / BT Tf Tj ET
mutool show sample.pdf 1/Resources  # 打印资源字典，应能看到 /Font /F1 ...
```

在内容流里，典型片段长这样（PDF 操作符都是后缀的）：

```
1 0 0 rg            % 设填充色为红
100 700 200 50 re   % 构造矩形 (100,700) 宽200 高50
f                   % 填充
BT                  % 开始文字对象
/F1 24 Tf           % 选字体 F1，字号 24
100 600 Td          % 文字定位
(Hello) Tj          % 显示文字 Hello
ET
```

### 步骤 3：用 trace device 观测 device 调用

```bash
mutool trace -o trace.xml sample.pdf 1
```

在 `trace.xml` 里寻找 `<fill_path ...>` 与 `<fill_text ...>`。对照内容流，你会看到：

- 内容流的 `re f` 对应一个 `<fill_path winding="nonzero" ...>`（trace device 由 `fz_trace_fill_path` 输出，[trace-device.c:185](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L185)）；
- 内容流的 `(Hello) Tj` 对应一个 `<fill_text ...>`（由 `fz_trace_fill_text` 输出，[trace-device.c:283](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L283)），里面还会展开每个字形的坐标。

> 若工具选项或输出格式与上述不符，以本地实际输出为准（**待本地验证**）。核心是：trace 能让你把内容流操作符与 device 调用一一对应。

### 步骤 4：在源码里定位并写出说明

按下面两条链，对照真实行号阅读 `source/pdf/pdf-op-run.c`，写一段说明文字。

**矩形链**（`re f` → `fz_fill_path`）：

1. `re` 在分发后调 `pdf_run_re`（[op-run.c:2839](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2839)），用 `fz_rectto` 把矩形累积进 `pr->path`；
2. `f` 调 `pdf_run_f`（[op-run.c:2865](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2865)），转交 `pdf_show_path`；
3. `pdf_show_path`（[op-run.c:956](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L956)）在纯色填充分支发出 `fz_fill_path`（[op-run.c:1022](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1022)），把路径、奇偶规则、ctm、颜色一并交给 device。

**文字链**（`Tj` → `fz_fill_text`）：

1. `Tf` 先经分发查资源（[interpret.c:1408](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1408) `pdf_lookup_resource(..., Font, ...)`），加载字体后由 `pdf_run_Tf`（[op-run.c:2974](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L2974)）存入图形状态；
2. `Tj` 经分发（[interpret.c:1430](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-interpret.c#L1430)）调 `pdf_run_Tj`（[op-run.c:3034](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L3034)）→ `pdf_show_string`（[op-run.c:1593](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1593)）；
3. 字符串被逐字符解码，`pdf_show_char`（[op-run.c:1329](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1329)）把每个字形的矩阵累积进 `fz_text`；
4. 文字对象结束时 `pdf_flush_text_imp`（[op-run.c:1115](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1115)）按渲染模式（默认 0=填充）发出 `fz_fill_text`（[op-run.c:1201](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-op-run.c#L1201)）。

### 需要观察的现象与预期结果

- trace 输出里，每个 `re f` 对应恰好一个 `<fill_path>`；每个文字对象（`BT…ET`）里的 `Tj` 对应一个 `<fill_text>`，其内含若干字形。
- 改变内容流里的填充色操作符（如把 `1 0 0 rg` 换成 `0 0 1 rg`），`<fill_path>` 的颜色属性会跟着变，证明颜色经图形状态 `gstate->fill.v` 流入了 device 调用。
- 若删掉 `Tf` 行再 trace，`<fill_text>` 会消失（`pdf_show_string` 因无字体而警告返回），印证字体是文字链的必要前提。

### 写出最终说明

把上面两条链用自己的话串成一段：**一个 PDF 中「画矩形 + 输出文字」的指令，先由 `pdf_run_re` 把矩形累积成路径、`pdf_run_f` 经 `pdf_show_path` 触发 `fz_fill_path`；文字则先经 `Tf` 查字体资源、`pdf_run_Tj` 逐字符累积进 `fz_text`、最后由 `pdf_flush_text_imp` 触发 `fz_fill_text`。两条链都在图形状态 `gstate`（颜色、ctm、alpha）的伴奏下，把 PDF 操作符翻译成了 device 回调。**

## 6. 本讲小结

- PDF 页面 = `/Resources`（军火库）+ `/Contents`（指令程序）；`pdf_page_resources` 用可继承查找取资源，`pdf_page_contents` 取内容流。
- 资源查找靠**资源栈**：`pdf_lookup_resource` 从栈顶（最内层 Form）向栈底（页面层）逐层按「子键 + 名字」查找，内层优先、命中即返，形同词法作用域；压栈/弹栈发生在 `pdf_process_contents`。
- **两种 resources 要分清**：读侧是 `/Resources` 字典（按名查，本讲主角），写侧是 `pdf-resources.c` 的去重哈希表（按内容摘要，pdfwrite 用）。
- `pdf_processor` 是一张访问者虚表，几乎每个 PDF 操作符对应一个 `op_*`；`pdf_run_processor` 是把操作符翻译成 device 调用的实现。
- 关键字分发用 `A/B/C` 宏把操作符前 1~3 字符打包成整数做 `switch`，避免逐个 `strcmp`。
- `re` 只构造路径不输出；`f` 经 `pdf_show_path` 发出 `fz_fill_path`；`Tj` 经 `pdf_show_string`→`pdf_show_char` 累积 `fz_text`，再经 `pdf_flush_text_imp` 发出 `fz_fill_text`。
- `mutool trace` 能把 device 调用打印成 `<fill_path>`/`<fill_text>`，是把「内容流操作符」与「device 调用」逐条对账的利器。

## 7. 下一步学习建议

- **进入写侧**：本讲只讲了「读 + 解释」。u6-l1/u6-l3 的 document writer 与 `pdf-write.c` 讲的是「把 device 调用录回 PDF 对象树」，正好是本讲的逆过程，建议对照阅读，体会 `pdf_page_write` device 如何把回调累积成内容流。
- **读 filter processor**：`mutool clean` 走的是 `pdf_new_sanitize_filter` 等 filter processor（[interpret.h:413](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/interpret.h#L413) 附近）。它们同样实现 `pdf_processor`，但不画图、而是改写操作符后转给下游——理解它们能让你看清 `pdf_processor` 抽象的复用价值。
- **追一条复杂指令**：挑一个带透明度（`gs` + `ExtGState` 的 `/ca`）或带图案填充的 PDF，追踪 `pdf_show_path` 里 `PDF_MAT_PATTERN`/`PDF_MAT_SHADE` 分支如何递归进 `pdf_show_pattern`，体会材质种类如何让同一条 `f` 走出不同的 device 调用序列。
- **字体解码细节**：`pdf_show_char` 里 `pdf_tos_make_trm` 与字体编码表的交互，是 u5-l1（字体与字形缓存）的延伸，想做 OCR/文本抽取的读者值得深读。
