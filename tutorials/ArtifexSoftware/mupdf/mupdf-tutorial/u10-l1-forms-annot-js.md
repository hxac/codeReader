# 表单、标注与 JavaScript 交互

## 1. 本讲目标

本讲聚焦 PDF 文档的「交互能力」。学完后你应该能够：

- 理解 AcroForm 表单字段在 PDF 对象树里是如何表示的，并能遍历、识别、读取、填写一个字段。
- 认识标注（annotation）的创建、修改与「延迟外观重生成」机制，理解「表单字段本质上是一种标注」。
- 厘清 MuPDF 中「两套 JavaScript 引擎」的区别：PDF 内嵌的 Acrobat 风格 JS（`pdf-js.c`）与脚本化 MuPDF 库的 `mutool run`（`murun.c`），以及它们如何经 `pdf_enable_js` 衔接。

本讲是高级单元「交互、扩展与二次开发」的首篇，前置知识是 [u7-l1](u7-l1-pdf-object-model.md) 的 `pdf_obj` 对象模型——表单与标注的全部数据都住在 `pdf_obj` 字典/数组里。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是 AcroForm。** PDF 不只是「带文字的纸」。它可以承载可填写的表单——文本框、复选框、单选钮、下拉框、列表框、签名框。这套机制叫 AcroForm（Acrobat Form）。表单的数据组织在一棵「字段树」里，挂在文档根目录的 `/Root/AcroForm/Fields` 数组下。字段可以分层（父字段 `/Kids` 下挂子字段），子字段共享父字段的部分属性（这就是 u7-l1 讲过的「可继承属性」）。

**什么是标注。** 标注（annotation）是「贴」在页面上的交互对象：高亮、便签、线条、图章、链接、墨迹……也包括表单控件。每个标注是页面 `/Annots` 数组里的一个字典，有 `/Rect`（位置）、`/Subtype`（类型）、`/AP`（外观流）等关键键。**关键洞见：表单字段就是 `/Subtype` 为 `/Widget` 的标注。** 所以「表单」是「标注」的一个子集——这也是为什么 `pdf_widget_*` 函数大多以 `pdf_annot *` 为参数。

**什么是 PDF JavaScript。** PDF 规范允许把 JavaScript 代码嵌进文档，由阅读器在特定事件（打开页面、点击按钮、字段值变化）时执行，用来做校验、自动计算、弹窗等。MuPDF 用第三方库 mujs 来解释执行这些脚本。注意：MuPDF 里存在**两个**独立的 mujs 引擎实例，用途完全不同，本讲会重点区分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/pdf/form.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/form.h) | 表单字段公共 API：控件类型枚举、字段标志位、字段读写与事件回调声明 |
| [include/mupdf/pdf/annot.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/annot.h) | 标注公共 API：标注类型枚举、属性读写、创建/删除/更新外观声明 |
| [source/pdf/pdf-form.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c) | 表单核心实现：字段类型判定、值读写、按名查找、重算/重置、JS 事件分发 |
| [source/pdf/pdf-annot.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c) | 标注实现：创建、属性存取、脏标记与延迟外观重生成调度 |
| [source/pdf/pdf-appearance.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-appearance.c) | 外观流（`/AP`）的合成器：把字段/标注的属性渲染成一段可绘制的内容流 |
| [include/mupdf/pdf/javascript.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/javascript.h) | PDF 内嵌 JS 引擎的公共 API（enable/disable/execute/event） |
| [source/pdf/pdf-js.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c) | PDF 内嵌 JS 实现：mujs 桥接、Acrobat 风格 DOM（app/console/util/Doc/Field/event）、事件钩子 |
| [source/tools/murun.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/murun.c) | `mutool run` 实现：独立的 mujs 解释器，把整个 MuPDF C API 暴露给 JS |

## 4. 核心概念与源码讲解

### 4.1 表单字段处理

#### 4.1.1 概念说明

AcroForm 用一棵「字段树」组织表单。每个字段是一个 `pdf_obj` 字典，核心键有：

- `/T`：字段的部分名（partial name），字段的全名是把从根到本节点的 `/T` 用 `.` 拼起来（如 `address.zip`）。
- `/FT`：字段类型，取 `/Btn`（按钮）、`/Tx`（文本）、`/Ch`（选择）、`/Sig`（签名）四种。可继承。
- `/Ff`：字段标志位（field flags），整型位掩码。可继承。
- `/V`：字段当前值（value）。可继承。
- `/DV`：字段默认值（default value），重置时回到它。
- `/Kids`：子字段数组，构成层级。
- `/Parent`：父字段指针。
- `/TU`：供用户看的替代名（tooltip/label）。

注意 `/FT` 和用户感知的「控件种类」不完全一一对应：一个 `/Btn` 字段，依据 `/Ff` 标志位的不同，可能是按钮、复选框或单选钮。MuPDF 用枚举 `enum pdf_widget_type` 给出更细的、面向用户的八种类型：

[include/mupdf/pdf/form.h:29-40](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/form.h#L29-L40) 定义了 `UNKNOWN/BUTTON/CHECKBOX/COMBOBOX/LISTBOX/RADIOBUTTON/SIGNATURE/TEXT` 八种控件类型。

#### 4.1.2 核心流程

遍历并读取一个文档所有字段的典型流程：

```
fz_open_document(ctx, "form.pdf")          # 拿到 pdf_document
for 每一页 page:
    widget = pdf_first_widget(ctx, page)   # 页面有独立的 widget 链表
    while widget:
        type  = pdf_widget_type(ctx, widget)        # 推断控件类型
        name  = pdf_load_field_name(ctx, widget->obj)  # 拼全名
        value = pdf_field_value(ctx, widget->obj)   # 读 /V
        widget = pdf_next_widget(ctx, widget)
```

写一个字段的值则经「类型分发 + 校验 + 标脏」三步：

```
pdf_set_field_value(ctx, doc, field, "hello", ignore_trigger_events)
  ├─ 按字段类型分发：
  │    文本/下拉/列表 → set_validated_field_value（可能触发 Validate JS）
  │    复选/单选      → set_checkbox_value（改 /AS 外观状态）
  ├─ 若不忽略触发事件：doc->recalculate = 1（稍后重算）
  └─ 标脏 → 外观流延迟重生成
```

「延迟」是关键：写值不会立刻重画外观，只是设脏标记；真正的 `/AP` 重生成发生在稍后的 `pdf_update_annot` / `pdf_update_page`。这避免了连续修改时的重复绘制。

#### 4.1.3 源码精读

**字段类型推断**——`pdf_field_type` 把 PDF 的四种 `/FT` 细分成八种控件类型，按钮和选择类还需结合 `/Ff` 标志位二次判定：

[source/pdf/pdf-form.c:68-94](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L68-L94) 读取可继承的 `/FT` 与 `/Ff`，`/Btn` 据 `PDF_BTN_FIELD_IS_PUSHBUTTON`/`IS_RADIO` 拆成按钮/单选/复选，`/Ch` 据 `IS_COMBO` 拆成下拉/列表。

配套的 `pdf_field_type_string` 把枚举翻成 `"text"`、`"checkbox"` 等字符串（[pdf-form.c:96-109](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L96-L109)），这正是 JS 里 `Field.type` 的取值来源。

**字段值读取**——`pdf_field_value` 处理值的多种形态：

[source/pdf/pdf-form.c:43-61](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L43-L61) 经 `pdf_dict_get_inheritable` 取 `/V`：若是 name（复选/单选的选中态）取其名；若是流对象则转成 UTF8 文本串并回写；否则取文本串。这里复用了 u7-l1 讲过的「可继承字典访问」。

**控件遍历**——页面维护两条独立链表：普通标注 `page->annots` 与表单控件 `page->widgets`。`pdf_first_widget`/`pdf_next_widget` 直接走链表头/next 指针：

[source/pdf/pdf-form.c:673-681](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L673-L681) 遍历 `page->widgets`。`pdf_widget_type`（[pdf-form.c:683-701](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L683-L701)）先确认 `/Subtype == /Widget`，再把 `widget->obj` 当字段交给 `pdf_field_type`。注意它包在 `pdf_annot_push_local_xref`/`pop_local_xref` 之间——读写控件属性可能要临时往文档里写新对象（如生成外观），local xref 让这些写入可被回滚。

**字段全名**——`pdf_load_field_name` 递归爬 `/Parent` 链，把各层 `/T` 用 `.` 拼接，并做环检测防止损坏 PDF 递归：

[source/pdf/pdf-form.c:862-907](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L862-L907) 自底向上累加每层部分名长度，最终 `strcat` 出 `"a.b.c"` 形式的全名。

**按名查找**——`pdf_lookup_field` 接收点分全名，在 `/Root/AcroForm/Fields` 树里逐段匹配：

[source/pdf/pdf-form.c:164-205](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L164-L205) `lookup_field_sub` 取每一段（按 `.` 切分）与字段 `/T` 比对，命中则深入 `/Kids` 继续，串耗尽即命中目标字段。这是 JS `getField("a.b")` 的底层实现。

**写值分发**——`pdf_set_field_value` 是填写字段的主入口：

[source/pdf/pdf-form.c:751-778](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L751-L778) 按类型分发：文本/下拉/列表走 `set_validated_field_value`（先 `pdf_field_event_validate` 跑校验，不过则返回 0 拒绝写入），复选/单选走 `set_checkbox_value`（改 `/AS` 选择外观态）。若不忽略触发事件，置 `doc->recalculate=1`。

**字段标志位**——`/Ff` 的每一位在 [form.h:121-151](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/form.h#L121-L151) 定义，分通用、文本、按钮、选择四组，例如 `PDF_TX_FIELD_IS_PASSWORD`（密码框）、`PDF_BTN_FIELD_IS_RADIO`（单选）、`PDF_CH_FIELD_IS_COMBO`（下拉）。

**表单重算与重置**——`pdf_calculate_form` 遍历 `/Root/AcroForm/CO`（calculation order，计算顺序数组），对每个字段跑 Calculate 事件；`pdf_field_reset` 把 `/V` 复位到 `/DV`：

[source/pdf/pdf-form.c:479-498](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L479-L498) 重算；[pdf-form.c:234-288](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L234-L288) 重置逻辑（复位值并更新复选框 `/AS` 外观态）。

#### 4.1.4 代码实践

**实践目标**：遍历一个含 AcroForm 的 PDF，打印每个字段的全名、类型、当前值；然后给第一个文本框填一个值并增量保存。

**操作步骤**：

1. 先确认环境编译了 PDF 支持。MuPDF 仓库自带测试 PDF，例如 `tests/` 下；若手头没有表单 PDF，可用 `mutool create` 造一个，或在仓库示例中寻找。下面是一段**示例代码**（非项目原有文件），演示最小调用链，建议存为 `dump_form.c`：

```c
/* 示例代码：编译时需链接 libmupdf，例如
 * gcc dump_form.c -lmupdf -lmupdf-third -lm -o dump_form */
#include "mupdf/fitz.h"
#include "mupdf/pdf.h"

int main(int argc, char **argv)
{
    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_register_document_handlers(ctx);

    fz_try(ctx) {
        fz_document *doc = fz_open_document(ctx, argv[1]);
        int i, n = fz_count_pages(ctx, doc);
        for (i = 0; i < n; i++) {
            fz_page *page = fz_load_page(ctx, doc, i);
            pdf_page *ppage = pdf_page_from_fz_page(ctx, page);
            pdf_annot *w;
            for (w = pdf_first_widget(ctx, ppage); w; w = pdf_next_widget(ctx, w)) {
                char *name = pdf_load_field_name(ctx, pdf_annot_obj(ctx, w));
                printf("page %d: [%s] type=%s value=%s\n", i,
                       name,
                       pdf_field_type_string(ctx, pdf_annot_obj(ctx, w)),
                       pdf_field_value(ctx, pdf_annot_obj(ctx, w)));
                fz_free(ctx, name);
            }
            fz_drop_page(ctx, page);
        }
        fz_drop_document(ctx, doc);
    }
    fz_catch(ctx)
        fz_report_error(ctx);
    fz_drop_context(ctx);
    return 0;
}
```

2. 在上述循环里，对第一个文本框调用 `pdf_set_field_value` 填值（注意第二个参数是 `pdf_document *`）：

```c
/* 示例代码：填值后必须 update + save 才会落盘 */
pdf_set_field_value(ctx, (pdf_document *)doc, field_obj, "hello", 0);
pdf_update_page(ctx, ppage);          /* 触发延迟的外观重生成 */
pdf_save_document(ctx, (pdf_document *)doc, "out.pdf", NULL); /* 增量保存 */
```

**需要观察的现象**：打印出的字段名应是点分全名（如 `person.name`）；类型字符串为 `text`/`checkbox` 等；填写后再用 `mutool show out.pdf trailer Root/AcroForm/Fields` 能看到对应字段的 `/V` 已变化。

**预期结果**：程序输出每个字段一行；保存后的 `out.pdf` 在任意 PDF 阅读器里打开，该文本框显示 `hello`。若手头确实没有表单 PDF，可跳过运行，转做下面的「源码阅读型实践」：在 `pdf_set_field_value`（[pdf-form.c:751](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L751)）处跟踪一个文本框的写入路径，记下它经过了哪几个函数、最终改了字典里的哪个键（答案：`/V`）。

> 说明：规格中提到的 `pdf_set_form_field_value` 是泛指的「之类接口」；MuPDF 实际暴露的公共函数名是 `pdf_set_field_value`（[form.h:178](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/form.h#L178)），还有更细粒度的 `pdf_set_text_field_value` / `pdf_set_choice_field_value`（[form.h:187-188](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/form.h#L187-L188)）。

#### 4.1.5 小练习与答案

**练习 1**：一个 `/FT` 为 `/Btn` 的字段，分别满足什么条件会被判定为 `BUTTON`、`CHECKBOX`、`RADIOBUTTON`？

**参考答案**：看 `/Ff` 标志位（[pdf-form.c:72-80](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L72-L80)）。置 `PDF_BTN_FIELD_IS_PUSHBUTTON` → `BUTTON`；否则置 `PDF_BTN_FIELD_IS_RADIO` → `RADIOBUTTON`；两者都不置 → `CHECKBOX`。

**练习 2**：为什么遍历字段要用 `pdf_first_widget` 而不是 `pdf_first_annot`？

**参考答案**：页面维护两条独立链表（`page->annots` 与 `page->widgets`）。普通标注（高亮、便签等）在 annots 链表里，表单控件在 widgets 链表里（`pdf_create_annot_raw` 按 `/Subtype==/Widget` 决定挂哪条，见 [pdf-annot.c:691-699](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c#L691-L699)）。遍历表单应用 widgets 链表；若想列出全部交互对象则两条都要走。

---

### 4.2 标注管理

#### 4.2.1 概念说明

标注（annotation）是页面 `/Annots` 数组中的字典对象，描述「贴」在页面上的交互内容。MuPDF 在 [annot.h:34-65](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/annot.h#L34-L65) 定义了约 28 种 `enum pdf_annot_type`，从 `TEXT`（便签）、`LINK`、`HIGHLIGHT`、`STAMP` 到 `WIDGET`（表单控件）、`REDACT`（消隐）等。

标注字典的关键键：

- `/Rect`：标注在设计坐标系下的矩形位置。
- `/Subtype`：标注类型名（`/Highlight`、`/Widget` 等）。
- `/AP`：外观流（appearance stream），一个 `/N`（normal）/`/R`（rollover）/`/D`（down）子字典，每个态指向一段内容流。**标注长什么样，由 `/AP` 决定。**
- `/F`：标志位（可见/隐藏/可打印/只读等），见 [annot.h:80-92](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/annot.h#L80-L92)。
- `/C`、`/IC`：颜色 / 内部颜色。
- `/Contents`：文字内容（便签正文）。
- `/BS`：边框样式（实线/虚线/斜面等）。

MuPDF 提供数百个 `pdf_annot_*` / `pdf_set_annot_*` 函数读写这些属性——它们不是直接改字典就完事，而是会顺手标脏，以便稍后重生成外观。

#### 4.2.2 核心流程

创建一个标注并让它可见的流程：

```
pdf_create_annot(ctx, page, PDF_ANNOT_HIGHLIGHT)
  ├─ pdf_create_annot_raw: 新建 dict，设 /Type /Annot、/Subtype，分配对象号，
  │                       追加到 page->obj 的 /Annots，挂入 page->annots 链表
  ├─ 按类型填默认值：/Rect、/C、popup 等
  └─ 标脏 → needs_new_ap = 1, doc->resynth_required = 1

（修改属性）pdf_set_annot_color / pdf_set_annot_rect / ...
  └─ 内部都调 pdf_dirty_annot → 标脏

（渲染前）pdf_update_annot(ctx, annot) 或 pdf_update_page(ctx, page)
  └─ 检测到 resynth_required → update_all_appearances 合成 /AP

（绘制）pdf_run_annot(ctx, annot, dev, ctm, cookie)
  └─ 把标注外观流跑到 device 上
```

核心设计是「属性改动只标脏，外观延迟合成」。这样一次交互（用户连续拖动、改色）只在最后重画一次 `/AP`，而不是每次属性变化都重画。

#### 4.2.3 源码精读

**创建标注的底层**——`pdf_create_annot_raw` 构造标注字典并挂到页面：

[source/pdf/pdf-annot.c:641-700](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c#L641-L700) 建 dict，写 `/Type /Annot` 与 `/Subtype`（由 `pdf_string_from_annot_type` 翻译），分配对象号并 `pdf_array_push` 进页面 `/Annots`，最后按类型挂进 `page->widget_tailp` 或 `page->annot_tailp`。

`pdf_create_annot`（[pdf-annot.c:951](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c#L951)）在 raw 之上，按类型给一组合理默认（例如便签给 16×16 图标矩形 + 黄色 + popup；图章给红色 + "Draft" 图标名；自由文本给 Helv 12 号字）。

**脏标记机制**——这是理解「延迟外观重生成」的钥匙：

[source/pdf/pdf-annot.c:454-472](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c#L454-L472) `pdf_annot_request_resynthesis` 置 `annot->needs_new_ap = 1` 并设 `doc->resynth_required = 1`；`pdf_annot_request_synthesis` 只在没有 `/AP` 时才请求合成。所有 `pdf_set_annot_*` 最终都经 `pdf_dirty_annot`（[pdf-annot.c:503-507](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c#L503-L507)）走到这里。

**外观合成入口**——`pdf_update_annot` 只是把脏标记「结算」成一次实际合成：

[source/pdf/pdf-appearance.c:3910-3923](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-appearance.c#L3910-L3923) 若 `doc->resynth_required`，调 `update_all_appearances` 重画本页所有脏标注的 `/AP`，然后返回「是否有变化」供上层决定是否重渲染。

**页面级更新**——`pdf_update_page` 先按需重算表单，再更新本页所有 annots 与 widgets，返回是否变化（[pdf-form.c:625-660](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L625-L660)）。它的注释明确指出：一次事件可能影响多页，应用应遍历当前打开的所有页面调 `pdf_update_annot`，且必须用「上次渲染时用的同一个 `pdf_annot` 对象」，否则无法可靠报告变化。

#### 4.2.4 代码实践

**实践目标**：在一个 PDF 上新建一个高亮标注，设置矩形与颜色，渲染验证其可见。

**操作步骤**：

1. 阅读现有工具 `mutool` 是否提供标注操作（例如 `mutool merge`/`clean` 不直接建标注）。下面是**示例代码**骨架，演示创建流程：

```c
/* 示例代码 */
pdf_annot *hl = pdf_create_annot(ctx, ppage, PDF_ANNOT_HIGHLIGHT);
pdf_set_annot_rect(ctx, hl, fz_make_rect(50, 700, 250, 720)); /* 文档坐标系 */
pdf_set_annot_color(ctx, hl, 3, (float[]){1, 1, 0});          /* 黄色 RGB */
pdf_set_annot_contents(ctx, hl, "my highlight");
pdf_update_page(ctx, ppage);                                  /* 结算外观 */
pdf_save_document(ctx, pdf, "out.pdf", NULL);
```

2. 用 `mutool draw -o out.png out.pdf 1` 渲染第一页为 PNG，肉眼检查黄色高亮条是否出现。

**需要观察的现象**：保存后的 PDF 用 `mutool show out.pdf 1 Annots` 能看到新标注对象，其 `/Subtype /Highlight`、`/AP /N` 指向一段新生成的内容流。

**预期结果**：PNG 上出现指定位置的黄色半透明高亮。若运行失败或无 `libmupdf`，转「源码阅读型实践」：在 [pdf-annot.c:454](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c#L454) 的 `pdf_annot_request_resynthesis` 设断点或加日志，连续调用两个 `pdf_set_annot_*`，观察 `needs_new_ap` 被置位但 `/AP` 直到 `pdf_update_annot` 才真正改写——这印证了「延迟合成」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pdf_set_annot_color` 不直接改 `/C` 就结束，还要标脏？

**参考答案**：`/C` 只描述「想要的颜色」，标注实际显示由 `/AP` 外观流决定。改了 `/C` 不重画 `/AP`，视觉上不会变。标脏让后续 `pdf_update_annot` 调 `update_all_appearances` 重新合成一段反映新颜色的 `/AP`。

**练习 2**：`pdf_create_annot` 与 `pdf_create_annot_raw` 的区别是什么？

**参考答案**：`raw` 只建字典、挂链表、设 `/Type`/`/Subtype`，是个空壳；`pdf_create_annot` 在 raw 基础上按类型填入一组「合理默认」（矩形、颜色、边框、字体等），免去调用者手工设基础属性。

---

### 4.3 JavaScript 执行

#### 4.3.1 概念说明

MuPDF 里有两套独立的 mujs JavaScript 引擎，这是本讲最容易混淆、也最需要厘清的点：

1. **PDF 内嵌 JS（pdf-js.c）**：这是「Acrobat 兼容」的脚本引擎，挂在某个 `pdf_document` 上。它实现了 Acrobat JS DOM 的一个子集——`app.alert`、`app.launchURL`、`console.println`、`util.printf`、`Doc.getField`、`Doc.resetForm`、`Field.value`、全局 `event` 对象等。它的作用是执行**文档自带的脚本**（文档级脚本、字段事件脚本），用来做校验、自动计算、弹窗。它由 `pdf_enable_js(ctx, doc)` 显式开启，由 `FZ_ENABLE_JS` 宏在编译期门控。

2. **脚本化 MuPDF（murun.c = `mutool run`）**：这是**另一个**全新的 mujs 状态，作用完全不同——它把整个 MuPDF C 库（Document/Page/Pixmap/Buffer/PDFDocument/PDFObject/Archive……）包装成 JS 对象，让你**用 JavaScript 脚本驱动 MuPDF**（批量处理、自动化测试、交互式探索）。它本身和具体某个 PDF 无关。

两者的桥梁是：`mutool run` 脚本里可以调用 `PDFDocument.enableJS()`，这会转发到 `pdf_enable_js`，从而在脚本宿主之外再为某个 PDF 文档**开启内嵌 JS 引擎**。两者共享 mujs 库，但是两个独立状态、两套不同 API。

#### 4.3.2 核心流程

PDF 内嵌 JS 的生命周期：

```
pdf_enable_js(ctx, doc)
  ├─ pdf_new_js: js_newstate 建状态，pdf_js_alloc 把内存接到 fz_context
  ├─ declare_dom: 注册 app/console/util/Doc/Field/event 等全局对象
  ├─ preload_helpers: 加载内置 util.js.h 辅助函数
  └─ pdf_js_load_document_level: 执行 /Root/Names/JavaScript 里的文档级脚本

触发事件（由 pdf-form.c 的事件函数发起）：
  pdf_annot_event_up / pdf_field_event_validate / pdf_field_event_calculate ...
    └─ pdf_execute_action_chain → pdf_execute_js_action
         ├─ pdf_js_event_init: 把 target/value/willCommit 灌进全局 event
         ├─ pdf_js_execute(doc->js, name, code, NULL):
         │     js_ploadstring + js_setlimit(10M 指令/100MB 内存) + js_pcall
         └─ pdf_js_event_result: 读回 event.rc（脚本是否接受）
```

`mutool run` 的生命周期则独立：

```
mutool run script.js   （mutool.c 分发表里 "run" 项，受 FZ_ENABLE_JS 门控）
  └─ murun_main: js_newstate 建全新状态，注册 mupdf 模块（Document/Buffer/…）
         执行用户脚本；脚本内可 PDFDocument.enableJS() 开启内嵌引擎
```

#### 4.3.3 源码精读

**资源限额**——为防止恶意/损坏脚本耗尽 CPU 与内存，pdf-js.c 写死了两条上限：

[source/pdf/pdf-js.c:28-30](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L28-L30) `PDF_JS_LIMIT_RUNTIME`（一千万条指令）与 `PDF_JS_LIMIT_MEMORY`（100 MiB）。

**引擎句柄**——`struct pdf_js` 把 mujs 状态 `js_State *imp`、所属 `ctx`、`doc` 和一个可选 console 绑在一起：

[source/pdf/pdf-js.c:37-44](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L37-L44) 引擎句柄。`pdf_new_js`（[pdf-js.c:1056-1088](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L1056-L1088)）用它初始化：`js_newstate(pdf_js_alloc, ctx, 0)` 建状态、`js_setcontext` 把 `pdf_js*` 挂进去以便回调取回、`declare_dom` 注册 DOM、`preload_helpers` 装辅助脚本。

**Acrobat 风格 DOM**——`declare_dom` 是「JS 能调什么」的全景图：

[source/pdf/pdf-js.c:896-988](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L896-L988) 依次创建：`global`、`event`、`util`（含 `util.printf`）、`app`（含 `app.alert`/`execMenuItem`/`launchURL`、`app.platform`）、`Field` 原型（`value`/`type`/`borderStyle`/`textColor`/`fillColor`/`display`/`name` 属性与 `buttonSetCaption` 方法，存进 registry 供 `getField` 实例化）、`console`（`println`/`clear`/`show`/`hide`），最后把一整套 `Doc` 方法挂到全局对象上（`numPages`、`author`/`title`/…、`getField`、`resetForm`、`calculateNow`、`print`、`mailDoc`）。

举例：`Doc.getField` 的实现——它从 `/Root/AcroForm/Fields` 取出字段树，调 `pdf_lookup_field` 按名查找，命中则用 registry 里的 `Field` 原型包成一个 JS 对象返回：

[source/pdf/pdf-js.c:393-418](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L393-L418) `doc_getField`。它把 `pdf_obj *field` 以 `pdf_keep_obj` 增引用后存为 mujs userdata，析构时由 `field_finalize`（[pdf-js.c:176-180](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L176-L180)）配对 `pdf_drop_obj`——这正是 u2-l2 讲过的引用计数与 C 多态的结合。

**执行与限额**——`pdf_js_execute` 是真正跑脚本的地方：

[source/pdf/pdf-js.c:1251-1291](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L1251-L1291) `js_ploadstring` 编译、`js_setlimit` 套上 CPU/内存上限、`js_pcall` 受保护调用。出错时不抛 C 异常，而是把 JS 错误信息打到 `fz_stddbg`（`js: ...`）或回填到 `result`。所有执行包在 `pdf_begin_implicit_operation`/`pdf_end_operation` 之间，使其可被撤销/计入操作历史。

**开启与门控**——`pdf_enable_js` 懒加载：

[source/pdf/pdf-js.c:1310-1323](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L1310-L1323) 仅在 `doc->js` 为空时建引擎并立刻执行文档级脚本（`pdf_js_load_document_level`，[pdf-js.c:1090-1130](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L1090-L1130) 从 `/Root/Names/JavaScript` 名称树加载）。

**编译期门控的退化**——当 `FZ_ENABLE_JS` 关闭时，整个 pdf-js.c 退化成一堆空实现：

[source/pdf/pdf-js.c:1330-1347](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L1330-L1347) `enable/disable` 变空操作，`pdf_js_supported` 返回 0，所有 `pdf_js_event_result*` 返回 1（成功）。含义：表单照常填写，但校验/格式/计算等 JS 事件被静默跳过。

**事件触发链**——pdf-form.c 的事件函数读 action 字典并送进引擎：

[source/pdf/pdf-form.c:2037-2065](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L2037-L2065) `pdf_execute_js_action` 把 action 的 `/JS`（可能是字符串或流）经 `pdf_load_stream_or_string_as_utf8` 取出代码，交 `pdf_js_execute` 执行。[pdf-form.c:2083-2111](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L2083-L2111) `pdf_execute_action_chain` 处理 action 为数组或带 `/Next` 链的情况，并用 `pdf_cycle` 做环检测。

典型事件路径（字段校验）：

[source/pdf/pdf-form.c:2283-2299](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L2283-L2299) `pdf_field_event_validate` 取字段 `/AA/V/JS`（Additional Actions → Validate → JS），先用 `pdf_js_event_init` 设好 `event`，执行脚本，再用 `pdf_js_event_result_validate` 读回 `event.rc` 与可能改写的 `event.value`。同理 Calculate 事件读 `/AA/C/JS`（[pdf-form.c:2301-2332](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L2301-L2332)）、Keystroke 读 `/AA/K/JS`、Format 读 `/AA/F/JS`、鼠标进入读 `/AA/E`、抬起读 `/A` 或 `/AA/U`（见 [pdf-form.c:2165-2230](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L2165-L2230)）。

**`mutool run` 与内嵌引擎的衔接**——这是两套引擎的关系落点：

[source/tools/mutool.c:64-65](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L64-L65) `"run"` 子命令受 `#if FZ_ENABLE_JS` 门控——这正是 u1-l4 讲过的「条件编译裁剪分发表」。[source/tools/murun.c:12326-12350](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/murun.c#L12326-L12350) `murun_main` 建一个**全新** `js_newstate`，与 pdf-js.c 的状态无关。murun 把 MuPDF C API 绑成 JS（如 `Document.countPages`、`PDFDocument.enableJS`）：

[source/tools/murun.c:8325-8333](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/murun.c#L8325-L8333) `ffi_PDFDocument_enableJS` 直接调 `pdf_enable_js`——这就是「宿主脚本开启文档内嵌引擎」的衔接点。两个引擎共享 mujs 库代码，但是两个独立 `js_State`，互不干扰。

#### 4.3.4 代码实践

**实践目标**：用 `mutool run` 跑一段 JS 脚本，打开一个 PDF、开启内嵌 JS、读取并打印某个表单字段的值。

**操作步骤**：

1. 确认 `mutool` 已编译且 JS 未被裁掉（`mujs=yes`，见 u1-l2）。运行 `./build/debug/mutool run` 应进入交互式 JS 提示符或报用法。

2. 写一段**示例脚本** `readform.js`（非项目原有文件）：

```javascript
// 示例脚本：用 mutool run 驱动 MuPDF 读取表单
var doc = new Document("form.pdf");        // murun 暴露的 Document 对象
if (doc.isPDF()) {
    var pdf = doc;                          // PDFDocument 提供 enableJS
    if (pdf.isJSSupported === undefined || true) {
        try { pdf.enableJS(); } catch (e) { print("js not enabled:", e); }
    }
    var n = doc.countPages();
    print("pages:", n);
}
```

   执行：`./build/debug/mutool run readform.js`

3. （备选）若想直接验证内嵌 Acrobat JS 的 `getField`，可在含文档级脚本的 PDF 上调用 `pdf_enable_js` 后观察 `fz_stddbg` 输出（脚本里的 `console.println` 会写到调试输出）。

**需要观察的现象**：脚本能 `new Document(...)` 并 `countPages()`；若 PDF 含可执行文档级 JS，`enableJS()` 后其 `app.alert`/`console.println` 会在调试输出产生 `js: ...` 行。

**预期结果**：打印出页数；调试输出里出现文档自带脚本的副作用。若手头无 `mutool run` 或无含 JS 的 PDF，转「源码阅读型实践」：对照 [pdf-js.c:896-988](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L896-L988) 的 `declare_dom`，列出 `app` 对象暴露的全部方法，并指出 `app.alert` 最终经哪个 fitz 事件回调上行（答案：`pdf_event_issue_alert`，由 `app_alert` 在 [pdf-js.c:78-153](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L78-L153) 调用）。

#### 4.3.5 小练习与答案

**练习 1**：一个 PDF 字段配了 Validate 脚本（`/AA/V/JS`）。若编译 MuPDF 时 `mujs=no`（`FZ_ENABLE_JS=0`），往这个字段写值会发生什么？

**参考答案**：`pdf_enable_js` 是空操作，`doc->js` 始终为 NULL，于是 `pdf_field_event_validate`（[pdf-form.c:2283](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L2283)）中 `if (js)` 不成立，直接 `return 1`（默认通过），值照常写入。校验脚本被静默跳过——表单可用，但自定义校验失效。

**练习 2**：`mutool run` 的 mujs 状态和 `pdf_enable_js` 创建的 mujs 状态是同一个吗？为什么这样设计？

**参考答案**：不是同一个。`murun_main` 在 [murun.c:12341](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/murun.c#L12341) `js_newstate` 建宿主状态（绑 MuPDF C API）；`pdf_new_js` 在 [pdf-js.c:1066](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-js.c#L1066) 为每个文档建 Acrobat 风格状态。分离是因为职责不同：宿主脚本控制整个库，内嵌脚本只能操作所属文档且必须受资源限额约束（10M 指令/100MB），把不可信的文档脚本关在独立、受限的状态里更安全。

---

## 5. 综合实践

把三个最小模块串起来，完成一个「自动填写并触发计算」的端到端任务。

**场景**：假设有一个订单 PDF，含字段 `qty`（数量，文本）、`price`（单价，文本）、`total`（合计，文本，配了 Calculate 脚本 `qty * price`）。

**任务**：

1. 用 4.1.4 的遍历程序，打印出这三个字段，确认它们都是 `text` 类型、全名正确。
2. 用 `pdf_set_field_value` 分别给 `qty` 填 `3`、给 `price` 填 `9.5`。
3. 调 `pdf_enable_js(ctx, doc)` 开启内嵌引擎，再调 `pdf_calculate_form(ctx, doc)`（[pdf-form.c:479](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-form.c#L479)）触发重算，然后读 `total` 的值——预期为 `28.5`。
4. 调 `pdf_update_page` 结算外观，`pdf_save_document` 增量保存，用 `mutool draw` 渲染验证三个框都显示了新值。

**思考点**：

- 若跳过第 3 步的 `pdf_enable_js`，`total` 会是多少？（答：不变，因为 Calculate 事件需要 `doc->js` 非空才会执行。）
- 若把 `pdf_set_field_value` 的最后参数 `ignore_trigger_events` 设为 1，会有什么不同？（答：不会置 `doc->recalculate=1`，且跳过 Validate 校验；填写更快但失去校验保护。）
- 若希望用脚本而非 C 完成，整个流程如何用 `mutool run` 表达？（提示：`new Document` → `enableJS` → `getField("qty").value = "3"` → `calculateNow()`。）

> 若手头没有这样的 PDF，可用任意支持表单的工具造一个，或退化为纯源码跟踪：在 `pdf_set_field_value` → `set_validated_field_value` → `pdf_field_event_validate` → `pdf_js_event_init` + `pdf_execute_js_action` + `pdf_js_event_result_validate` 这条链上逐函数阅读，画成时序图，标注每一步读/写了 `pdf_obj` 的哪个键、`event` 全局的哪个属性。

## 6. 本讲小结

- **表单即字段树**：AcroForm 字段住在 `/Root/AcroForm/Fields` 字典树里，`/FT`+`/Ff` 决定八种控件类型，`/V` 存值、`/DV` 存默认值，`/T` 拼出点分全名；`pdf_set_field_value` 按类型分发（文本类走校验、复选类改 `/AS`）并标脏。
- **字段即标注**：表单控件是 `/Subtype /Widget` 的标注，挂在其专属的 `page->widgets` 链表上（区别于普通 `page->annots`）。
- **延迟外观合成**：改属性只标脏（`needs_new_ap`/`resynth_required`），真正的 `/AP` 外观流在 `pdf_update_annot`/`pdf_update_page` 时才由 `pdf-appearance.c` 合成，避免连续修改重复重画。
- **两套 JS 引擎**：pdf-js.c 是文档内嵌的 Acrobat 风格引擎（`pdf_enable_js` 开启，受 `FZ_ENABLE_JS` 门控，有 CPU/内存限额），murun.c 是脚本化整个 MuPDF 库的 `mutool run` 宿主引擎；二者经 `PDFDocument.enableJS()` 衔接。
- **事件驱动执行**：字段/标注的 `/AA/*/JS` 与文档/页面的 action 链由 `pdf_execute_action_chain` 读取，经 `pdf_js_event_init`→`pdf_js_execute`→`pdf_js_event_result` 三段式与 JS 交互；关掉 JS 时这些事件被静默跳过，表单仍可用。

## 7. 下一步学习建议

- [u10-l2](u10-l2-custom-device.md) 实现自定义 device：表单与标注的外观最终都要经 device 绘制，理解 device 虚表后可以自定义「只统计不绘制」的分析设备来审计表单。
- 阅读 [source/pdf/pdf-appearance.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-appearance.c) 的 `update_all_appearances` 与各 `fz_*.pdf_ap_*.c` 合成器，理解 `/AP` 内容流是如何按标注类型逐类生成的（这是本讲「延迟合成」的下半场）。
- 深入 [source/pdf/pdf-annot.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-annot.c) 的红action（`pdf_apply_redaction`）与签名（form.h 的 `pdf_pkcs7_signer`/`pdf_sign_signature`），这两者是把「交互」用到极致的进阶场景。
- 若对脚本化感兴趣，通读 [source/tools/murun.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/murun.c) 的 FFI 绑定表，看 MuPDF 如何把 C API 系统性地映射成 JS 对象——这是 [u10-l3](u10-l3-extending-handlers.md) 扩展点的另一种实践范例。
