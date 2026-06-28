# pdf_obj：PDF 对象类型系统

## 1. 本讲目标

本讲是「PDF 对象模型深入」单元的第一篇。学完之后，你应该能够：

- 说清 `pdf_obj` 在内存中到底有哪几种类型、它们各自如何被判断和构造；
- 理解 MuPDF 一个相当反直觉的设计：**并非所有 `pdf_obj *` 都指向堆内存**，`null`、`true`、`false` 以及大量「常用名字」（如 `/MediaBox`、`/Pages`）其实是被编码成小整数指针的「单例」；
- 熟练使用 `pdf_dict_get` / `pdf_array_get` 这两条最基本的取值通路，并知道它们返回的是「借来的引用」；
- 掌握 `pdf_to_int` / `pdf_to_real` / `pdf_to_name` / `pdf_to_text_string` 这一类「静默类型转换」函数，以及 `pdf_dict_get_int` 等「取值 + 转换」一步到位的封装；
- 理解 `pdf_dict_get_inheritable` 沿 `/Parent` 链向上查找「可继承属性」的语义，并能用它正确读取一个页面的 `MediaBox`。

本讲只讲「对象本身」，**不**涉及 xref 间接对象如何从磁盘加载（那是 u7-l2 的主题），也**不**涉及内容流操作符如何被解释（那是 u7-l4）。我们假设你已经学过 u3-l1，知道 `fz_document`/`fz_page` 是「虚表 + 派生结构体」的手写多态。

## 2. 前置知识

- **PDF 对象的 8 种规范类型**。PDF 规范（ISO 32000）定义了八种基本对象：布尔（Boolean）、整数（Integer）、实数（Real）、字符串（String）、名字（Name）、数组（Array）、字典（Dictionary）、空（Null），外加由字典+流组成的「流（Stream）」。一个 PDF 文件本质就是一棵由这些对象组成的树。
- **名字（Name）** 写成 `/MediaBox` 这种以 `/` 开头的形式，常被用作字典的键（key）。
- **字典（Dictionary）** 是「名字 → 对象」的无序映射，类似 JSON 的对象；**数组（Array）** 是有序的对象列表。
- **间接对象（Indirect Object）**：PDF 里大部分对象不直接内联在引用处，而是写成 `12 0 obj ... endobj`，别处用 `12 0 R` 这样的「间接引用」指向它。MuPDF 在内存里用一个特殊的 `INDIRECT` 类型表示这种引用。
- **手写多态**（来自 u3-l1）：MuPDF 用「基类结构体作首成员 + 函数指针/类型标记」实现 C 语言里的继承。本讲的 `pdf_obj` 是另一个范例。

> 一个关键直觉：MuPDF 把 PDF 对象模型做成了**值语义 + 单例优化 + 借用引用**的混合体。理解这三点，本讲就通了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/pdf/object.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/object.h) | `pdf_obj` 的全部公共 API 声明：构造、判断、取值、字典/数组操作、可继承查询，以及 `PDF_NAME(...)` 宏与单例常量。 |
| [source/pdf/pdf-object.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c) | 上述 API 的实现，是本讲的主战场。`pdf_objkind` 枚举、派生结构体、`OBJ_IS_*` 宏、`pdf_dict_get`、`pdf_dict_get_inheritable` 都在这里。 |
| [source/pdf/pdf-parse.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c) | `pdf_to_rect` / `pdf_to_point` / `pdf_to_matrix` 等把「数字数组」还原成几何类型的辅助函数，本讲实践会用到 `pdf_to_rect`。 |
| [source/pdf/pdf-run.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c) | 渲染页面时读取 `MediaBox` 的真实代码，作为实践任务的「标准答案」参照。 |
| [include/mupdf/pdf/page.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/page.h) | `struct pdf_page`，其中 `obj` 字段就是页面对应的字典对象。 |

## 4. 核心概念与源码讲解

### 4.1 七种对象类型与「小整数指针」单例编码

#### 4.1.1 概念说明

`pdf_obj` 是一个**不透明指针**（`typedef struct pdf_obj pdf_obj;`），它统一代表「任意一种 PDF 对象」。在内存里，MuPDF 用一个字符型枚举 `pdf_objkind` 给「堆上对象」打类型标签：

```c
typedef enum pdf_objkind_e
{
    PDF_INT = 'i',
    PDF_REAL = 'f',
    PDF_STRING = 's',
    PDF_NAME = 'n',
    PDF_ARRAY = 'a',
    PDF_DICT = 'd',
    PDF_INDIRECT = 'r'
} pdf_objkind;
```

这就是本讲标题里的「七种类型」——七种**堆对象** kind。注意里面**没有** `PDF_NULL` 和 `PDF_BOOL`：空（null）和布尔（true/false）不单独占堆，而是用「单例」表示（见 4.1.3）。

下表把规范类型、MuPDF kind、判断函数、构造函数一一对应：

| 规范类型 | MuPDF kind / 表示 | 判断函数 | 典型构造函数 |
| --- | --- | --- | --- |
| Null | 单例 `PDF_NULL` | `pdf_is_null` | 直接用 `PDF_NULL` |
| Boolean | 单例 `PDF_TRUE`/`PDF_FALSE` | `pdf_is_bool` | 直接用 `PDF_TRUE`/`PDF_FALSE` |
| Integer | `PDF_INT` | `pdf_is_int` | `pdf_new_int` |
| Real | `PDF_REAL` | `pdf_is_real` | `pdf_new_real` |
| Number（整数或实数） | INT 或 REAL | `pdf_is_number` | — |
| String | `PDF_STRING` | `pdf_is_string` | `pdf_new_string` |
| Name | `PDF_NAME`（单例或堆） | `pdf_is_name` | `pdf_new_name` / `PDF_NAME(X)` |
| Array | `PDF_ARRAY` | `pdf_is_array` | `pdf_new_array` |
| Dictionary | `PDF_DICT` | `pdf_is_dict` | `pdf_new_dict` |
| 间接引用（MuPDF 内部） | `PDF_INDIRECT` | `pdf_is_indirect` | `pdf_new_indirect` |

> 小提示：MuPDF 还提供了 `pdf_is_stream`/`pdf_is_image_stream` 等判断「流对象」的函数，但「流」在对象模型层面是「一个字典 + 一段字节流」，并不是 `pdf_objkind` 里的独立 kind，故不在本讲的「七种」之列。

#### 4.1.2 核心流程

构造一个对象的流程很简单：分配对应派生结构体 → 把首成员 `super` 的 `refs` 置 1、`kind` 置为对应字符、`flags` 清零 → 填入数据 → 返回 `&obj->super`。读者用 `pdf_is_*` 判断类型，用 `pdf_to_*` 提取值。

判断与取值前有一个**不可见但至关重要**的步骤：`RESOLVE`。如果对象是间接引用（`INDIRECT`），先把它解引用成真实对象再继续。因此你拿到的 `pdf_obj *` 可能是「引用壳」，也可能是「实物」——大多数 API 都会替你透明解引用。

#### 4.1.3 源码精读

**基类与派生结构体（手写多态）**。`struct pdf_obj` 是所有对象的公共头部，只有引用计数、类型标记和标志位：

[struct pdf_obj 基类 — source/pdf/pdf-object.c:65-70](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L65-L70) —— 这三字段（`refs`/`kind`/`flags`）是每个堆对象都有的「身份证」。

每种具体类型都用「`pdf_obj super` 作首成员」的方式派生，下转型靠一组宏：

[派生结构体与下转型宏 — source/pdf/pdf-object.c:72-122](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L72-L122) 和 [cast 宏 NAME/NUM/STRING/DICT/ARRAY/REF — source/pdf/pdf-object.c:167-172](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L167-L172)。例如 `pdf_obj_num` 里 `union { int64_t i; float f; } u` 复用同一块内存来存整数或实数；`pdf_obj_dict` 用 `struct keyval *items` 存「键值对数组」；`pdf_obj_ref` 只存 `doc`/`num`/`gen`，是「指向 xref 中某对象的引用壳」。这与 u3-l1 的 `fz_document`/`pdf_page` 是同一套手写多态手法。

**最关键的反直觉点：单例编码。** 看 `pdf_new_name`：

[pdf_new_name：先二分查名字表，命中就返回单例小整数指针 — source/pdf/pdf-object.c:218-242](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L218-L242)。它在按字母序排好的 `PDF_NAME_LIST`（由 `name-table.h` 生成）里二分查找；**命中就 `return (pdf_obj*)(intptr_t)m;`**——返回的是「把下标 m 当成指针」的小整数地址，**完全不分配内存**。只有查不到（罕见自定义名字）才真正 `fz_malloc`。

那 `PDF_NAME(MediaBox)` 又是什么？看头文件里的宏：

[PDF_NAME 宏与单例常量 — include/mupdf/pdf/object.h:364-381](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/object.h#L364-L381)。`PDF_NAME(X)` 在编译期展开成一个枚举小整数指针，`PDF_NULL`/`PDF_TRUE`/`PDF_FALSE`/`PDF_LIMIT` 同理。`PDF_LIMIT` 是「最后一个名字的枚举值 + 1」。

于是产生了一条贯穿全篇的判别法则：

[OBJ_IS_* 与 RESOLVE 宏 — source/pdf/pdf-object.c:268-289](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L268-L289)。**每个 `OBJ_IS_*` 都先判断 `obj >= PDF_LIMIT`**：小整数指针（单例）一律 `< PDF_LIMIT`，真实堆地址一律 `>= PDF_LIMIT`。这样一刀切之后，只有堆对象才能安全读 `obj->kind`。例如 `OBJ_IS_NAME` 写成「`obj > PDF_FALSE && obj < PDF_LIMIT`（单例名字）**或** `obj >= PDF_LIMIT && obj->kind == PDF_NAME`（堆名字）」——名字既可能是单例也可能是堆对象。

`RESOLVE` 宏则把间接引用透明换成实物：`if (OBJ_IS_INDIRECT(obj)) obj = pdf_resolve_indirect_chain(ctx, obj);`（真正的解引用实现位于 `pdf-xref.c`，留待 u7-l2 讲）。

**判断函数**都是「先 RESOLVE 再 OBJ_IS_*」的一行包装：

[pdf_is_null / pdf_is_int / pdf_is_name … — source/pdf/pdf-object.c:295-347](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L295-L347)。

#### 4.1.4 代码实践

**实践目标**：亲手验证「单例 vs 堆对象」的内存差异，建立直觉。

**操作步骤**（源码阅读型 + 最小调用）：

1. 在 `pdf-object.c` 里找到 `pdf_new_int`、`pdf_new_name`、`pdf_new_indirect` 的实现（[174-266 行区间](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L174-L266)），观察它们都把 `super.refs = 1; super.kind = ...; super.flags = 0;` 设好。
2. 写一段最小测试程序（**示例代码**，非项目原有）：

```c
/* 示例代码：观察单例与堆对象的指针取值 */
fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
fz_register_document_handlers(ctx);

pdf_obj *singleton_name = PDF_NAME(MediaBox);   /* 小整数指针 */
pdf_obj *heap_int = pdf_new_int(ctx, 42);        /* 堆对象      */
pdf_obj *heap_name = pdf_new_name(ctx, "/MyCustomName"); /* 不在表里，堆对象 */

printf("MediaBox  ptr = %p\n", (void*)singleton_name); /* 远小于堆地址 */
printf("int 42    ptr = %p\n", (void*)heap_int);       /* 普通堆地址   */
printf("is_name(MediaBox)=%d  is_name(int)=%d\n",
       pdf_is_name(ctx, singleton_name), pdf_is_name(ctx, heap_int));

pdf_drop_obj(ctx, heap_int);
pdf_drop_obj(ctx, heap_name);
fz_drop_context(ctx);
```

3. 编译运行（参照 u1-l2 的构建方式，链接 `libmupdf`）。

**需要观察的现象**：`PDF_NAME(MediaBox)` 的指针值是一个极小的数（其实就是枚举下标），而 `pdf_new_int` 返回的是正常的堆地址；`pdf_drop_obj` 对单例是空操作（见下文 keep/drop），对堆对象才真正释放。

**预期结果**：`is_name(MediaBox)=1`、`is_name(int)=0`；打印出的两类指针数值量级差异巨大。**待本地验证**：具体指针数值因平台/编译而异，但「单例远小于堆地址」这一规律必然成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `struct pdf_obj` 里没有 `PDF_NULL` 和 `PDF_BOOL` 这两种 kind？

> **答案**：因为 `null`/`true`/`false` 在 MuPDF 里被编码成单例小整数指针（`PDF_NULL`/`PDF_TRUE`/`PDF_FALSE`），根本不分配堆对象，自然没有 `kind` 字段。判断它们靠「指针等于某个单例常量」，如 `OBJ_IS_NULL(obj) (obj == PDF_NULL)`。

**练习 2**：`pdf_drop_obj(ctx, PDF_NAME(MediaBox))` 会不会崩溃或造成问题？

> **答案**：不会，也不会释放任何内存。`pdf_drop_obj` 先判断 `obj >= PDF_LIMIT`，单例 `< PDF_LIMIT` 时直接 return（见 4.2.3 的 keep/drop 源码）。这正是单例编码带来的好处：常用名字零分配、零释放。

---

### 4.2 字典与数组访问

#### 4.2.1 概念说明

PDF 里最常用的两种容器是**数组**（有序）和**字典**（名字→对象）。MuPDF 把它们也实现成 `pdf_obj`，并提供两套对称的访问 API：

- 数组：`pdf_array_get(ctx, array, i)` 按下标取，`pdf_array_len` 取长度。
- 字典：`pdf_dict_get(ctx, dict, key)` 按 `pdf_obj` 名字键取，`pdf_dict_gets(ctx, dict, "MediaBox")` 按 C 字符串键取，`pdf_dict_getp(ctx, dict, "Root/Pages/Count")` 按「斜杠分隔路径」逐级取。

一个极其重要的语义约定：**这些 get 函数返回的是「借来的引用（borrowed reference）」**——它们不增加引用计数，调用方**不要** `pdf_drop_obj` 它，也不能在父容器被修改/销毁后继续使用它。如果你需要长期持有，必须自己 `pdf_keep_obj`。

#### 4.2.2 核心流程

- **数组取值**：先 `RESOLVE`（解引用），再 `OBJ_IS_ARRAY` 判断；下标越界或非数组都安静地返回 `NULL`。
- **字典取值**：先 `RESOLVE`，再 `OBJ_IS_DICT` 判断，且要求 key 必须是名字（`OBJ_IS_NAME`）；然后在内部键值对数组里查找。
- **字典查找的两种复杂度**：字典有一个 `PDF_FLAGS_SORTED` 标志。已排序时用二分查找，复杂度 \(O(\log n)\)；未排序时线性扫描，复杂度 \(O(n)\)。当一个字典在插入过程中增长到超过 100 项时，会被自动排序（见 `pdf_dict_get_put` 中的 `if (DICT(obj)->len > 100 ...) pdf_sort_dict(...)`）。

\[ \text{sorted?}\quad T_{\text{lookup}}=\begin{cases} O(\log n) & \text{已置 } \texttt{PDF\_FLAGS\_SORTED} \\ O(n) & \text{线性扫描} \end{cases} \]

#### 4.2.3 源码精读

**数组取值**：带边界检查、安静失败：

[pdf_array_get — source/pdf/pdf-object.c:881-890](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L881-L890)。注意它直接 `return ARRAY(obj)->items[i];`，没有任何 `keep`，所以返回值是借来的。

**字典按 C 字符串键取值**，底层是查找函数 `pdf_dict_finds`：

[pdf_dict_finds：已排序则二分、否则线性 — source/pdf/pdf-object.c:2300-2337](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L2300-L2337)。

外层封装：

[pdf_dict_gets — source/pdf/pdf-object.c:2392-2407](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L2392-L2407)。

**字典按 pdf_obj 名字键取值**，并对单例键做指针级优化：

[pdf_dict_get — source/pdf/pdf-object.c:2459-2477](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L2459-L2477)。这里有一个精妙点：当 `key < PDF_LIMIT`（即单例名字，如 `PDF_NAME(MediaBox)`）时走 `pdf_dict_find`，它**优先用指针相等比较**（`k == key`），只有遇到堆名字才退化成 `strcmp`——这就是 4.1 单例编码带来的性能红利：常用键的比较几乎零成本。

**keep/drop 与借用语义**：

[pdf_keep_obj / pdf_drop_obj — source/pdf/pdf-object.c:3122-3150](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L3122-L3150)。两者开头都判断 `obj >= PDF_LIMIT`：单例直接返回/无视，堆对象才动引用计数。`pdf_drop_obj` 在 `refs` 归零时按 `kind` 分派析构（数组逐项 drop、字典逐键值 drop、字符串额外释放 `text` 缓存）。所以「`get` 借来、`keep` 才持有、`drop` 释放」是铁律。

#### 4.2.4 代码实践

**实践目标**：用一个真实 PDF 的 Catalog 字典，对比三种字典取值方式的等价性。

**操作步骤**：

1. 阅读项目自身如何拿到 Catalog：[pdf-page.c:267-268](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-page.c#L267-L268) 中 `root = pdf_dict_get(ctx, pdf_trailer(ctx, doc), PDF_NAME(Root)); node = pdf_dict_get(ctx, root, PDF_NAME(Pages));`。
2. 写一段最小程序（**示例代码**），对同一份 PDF 打开后的 trailer，用三种方式取 `Root`：

```c
/* 示例代码 */
pdf_document *doc = pdf_open_document(ctx, "input.pdf");
pdf_obj *trailer = pdf_trailer(ctx, doc);

pdf_obj *root_a = pdf_dict_get(ctx, trailer, PDF_NAME(Root));        /* pdf_obj 键 */
pdf_obj *root_b = pdf_dict_gets(ctx, trailer, "Root");               /* C 字符串键 */
pdf_obj *root_c = pdf_dict_getp(ctx, trailer, "Root/Pages/Count");   /* 路径，应得到一个 int */

printf("root_a==root_b ? %d\n", root_a == root_b);            /* 期望 1，同一对象 */
printf("Pages count = %d\n", pdf_to_int(ctx, root_c));

/* 注意：root_a/root_b/root_c 都是借来的，不要 drop 它们 */
pdf_drop_document(ctx, doc);
```

**需要观察的现象**：`root_a == root_b`（两个指针完全相同，因为都是借用同一个值对象的指针）；`root_c` 经两级字典下钻后拿到页数整数。

**预期结果**：`root_a==root_b` 为 1；`Pages count` 等于该 PDF 的实际页数。**待本地验证**：需用一个真实的、页数已知的 PDF。

#### 4.2.5 小练习与答案

**练习 1**：`pdf_dict_get(ctx, dict, PDF_NAME(Foo))` 和 `pdf_dict_gets(ctx, dict, "Foo")` 在结果上等价吗？在内部实现上有何区别？

> **答案**：结果等价（都返回键 `/Foo` 对应的值，或 `NULL`）。区别在内部：前者传入单例名字键，走 `pdf_dict_find`，优先用指针相等比较；后者走 `pdf_dict_finds`，始终用 `strcmp`。对常用名字前者略快。

**练习 2**：下面代码哪里错了？

```c
pdf_obj *mb = pdf_dict_get(ctx, page, PDF_NAME(MediaBox));
/* ... 使用 mb ... */
pdf_drop_obj(ctx, mb);   /* 行 X */
```

> **答案**：`行 X` 错了。`pdf_dict_get` 返回借来的引用，没有增加引用计数，调用方不应 `drop` 它；这会导致引用计数失衡，后续释放 `page` 时可能 double-free 或提前析构。正确做法是**不 drop**；若需长期持有则先 `pdf_keep_obj(ctx, mb)`，之后再 drop。

---

### 4.3 类型化取值与可继承属性

#### 4.3.1 概念说明

拿到一个 `pdf_obj *` 后，你往往想要的是「它作为整数/实数/字符串/矩形」的**值**，而不是对象本身。MuPDF 提供：

- **静默类型转换族** `pdf_to_*`：`pdf_to_int`、`pdf_to_real`、`pdf_to_name`、`pdf_to_text_string`、`pdf_to_string`、`pdf_to_num`（取间接引用的对象号）等。它们的特点是「**安全、静默失败**」——类型不符时返回 0 或 `""`，**不抛异常**。源码注释原话是 `/* safe, silent failure, no error reporting on type mismatches */`。
- **取值+转换一步到位**：`pdf_dict_get_int`、`pdf_dict_get_rect`、`pdf_dict_get_text_string`、`pdf_array_get_real` 等，本质是 `pdf_to_X(ctx, pdf_dict_get(...))` 的薄封装。
- **可继承查询** `pdf_dict_get_inheritable`：PDF 的页面树里，`MediaBox`/`CropBox`/`Resources`/`Rotate` 等属性可以写在父级 `Pages` 节点上，由子页面**继承**。本函数沿 `/Parent` 链一路向上找，返回第一个命中。

#### 4.3.2 核心流程

**取值转换**：以 `pdf_to_int` 为例——先 `RESOLVE`，若是 `PDF_INT` 直接取 `NUM(obj)->u.i`，若是 `PDF_REAL` 则四舍五入 `floorf(f+0.5)`，否则返回 0。

**可继承查找**（`pdf_dict_get_inheritable`）伪代码：

```
node = start          # 从当前节点出发
slow  = start          # 用于环检测的慢指针
halfbeat = 11
while node != NULL:
    val = pdf_dict_get(node, key)
    if val != NULL: return val        # 命中即返回
    node = pdf_dict_get(node, /Parent) # 否则向上找父亲
    if node == slow:                   # 检测到环
        尝试修复一次页树；若仍环则抛错
    if --halfbeat == 0:                # 慢指针每两步走一步
        slow = pdf_dict_get(slow, /Parent); halfbeat = 2
return NULL                            # 一路到根都没命中
```

这是一个**改进版 Floyd 环检测算法**（快慢指针）：`node` 是快指针每轮都上移，`slow` 是慢指针延迟启动后每两轮上移一次，若两者相遇则存在环。页面树本不该有环，但损坏的 PDF 可能造出循环 `/Parent` 链，MuPDF 会先尝试 `pdf_repair_page_tree_parents` 修复一次，仍环才抛 `FZ_ERROR_FORMAT`。

#### 4.3.3 源码精读

**静默转换族**：

[pdf_to_int / pdf_to_real — source/pdf/pdf-object.c:371-417](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L371-L417)，注意 `pdf_to_int` 对 real 做 `floorf(f+0.5)` 四舍五入、对单例（`obj < PDF_LIMIT`）直接返回 0。

[pdf_to_name / pdf_to_text_string — source/pdf/pdf-object.c:431-481](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L431-L481)。`pdf_to_name` 对单例名字查 `PDF_NAME_LIST`、对堆名字读 `NAME(obj)->n`，其余返回 `""`；`pdf_to_text_string` 会把 PDF 字符串（可能是 UTF-16BE 或 PDFDocEncoding）惰性解码成 UTF-8 缓存到 `STRING(obj)->text`。

**取值+转换封装**：

[类型化字典取值 pdf_dict_get_int / pdf_dict_get_rect / pdf_dict_get_text_string … — source/pdf/pdf-object.c:4077-4133](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L4077-L4133)。可见它们都是一行：`return pdf_to_X(ctx, pdf_dict_get(ctx, dict, key));`。

**数组 → 矩形**：`MediaBox` 在 PDF 里写作 `/MediaBox [0 0 612 792]` 这样的四元数字数组，需要还原成 `fz_rect`：

[pdf_to_rect — source/pdf/pdf-parse.c:35-53](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L35-L53)。它取数组前 4 个元素为 `a,b,c,d`，并用 `fz_min/fz_max` 规整成 `x0<x1, y0<y1` 的规范矩形，非数组则返回 `fz_empty_rect`。

**可继承查找**（本模块核心）：

[pdf_dict_get_inheritable：沿 /Parent 链查找 + Floyd 环检测 — source/pdf/pdf-object.c:3787-3824](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L3787-L3824)。配套的类型化封装 `pdf_dict_get_inheritable_rect` 在 [source/pdf/pdf-object.c:4175-4178](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L4175-L4178)，正是 `pdf_to_rect(ctx, pdf_dict_get_inheritable(ctx, dict, key))`。

**「标准答案」参照**——项目自己在渲染页面时就是这样读 `MediaBox` 的：

[pdf_page_mediabox — source/pdf/pdf-run.c:99-102](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L99-L102) 用 `pdf_dict_get_inheritable_rect(ctx, page->obj, PDF_NAME(MediaBox))`，而 `page->obj` 就是页面对应的字典对象（见 [struct pdf_page — include/mupdf/pdf/page.h:319-331](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/page.h#L319-L331) 中的 `pdf_obj *obj;`）。这说明：**读页面尺寸必须用可继承版本**，因为很多 PDF 把 `MediaBox` 只写在根 `Pages` 节点上。

#### 4.3.4 代码实践

**实践目标**：复刻 MuPDF 自身读取 `MediaBox` 的做法，亲手把一个页面的 `MediaBox` 数组解析成 `fz_rect`，并体会「可继承」的必要性。

**操作步骤**：

1. 加载文档与某页，拿到该页的字典对象 `page->obj`：

```c
/* 示例代码 */
fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
fz_register_document_handlers(ctx);
fz_try(ctx) {
    pdf_document *doc = pdf_open_document(ctx, "input.pdf");
    pdf_page *page = pdf_load_page(ctx, doc, 0);   /* 第 0 页（内部页码从 0 起） */

    /* 关键：用可继承版本读 MediaBox（与 pdf_run.c 一致） */
    fz_rect mb = pdf_dict_get_inheritable_rect(ctx, page->obj, PDF_NAME(MediaBox));
    printf("MediaBox = [%.0f %.0f %.0f %.0f]\n", mb.x0, mb.y0, mb.x1, mb.y1);

    /* 对比：若该页自身没写 MediaBox，下面这句会得到空矩形 */
    fz_rect mb_local = pdf_dict_get_rect(ctx, page->obj, PDF_NAME(MediaBox));
    printf("local (non-inheritable) empty? %d\n",
           fz_is_empty_rect(ctx, mb_local));

    /* 也可手动拆解数组，体会 pdf_to_rect 的等价做法 */
    pdf_obj *arr = pdf_dict_get_inheritable(ctx, page->obj, PDF_NAME(MediaBox));
    if (pdf_is_array(ctx, arr))
        printf("array len=%d, [0]=%g [3]=%g\n",
               pdf_array_len(ctx, arr),
               pdf_array_get_real(ctx, arr, 0),
               pdf_array_get_real(ctx, arr, 3));

    fz_drop_page(ctx, (fz_page*)page);
    pdf_drop_document(ctx, doc);
}
fz_catch(ctx)
    fz_report_error(ctx);
fz_drop_context(ctx);
```

2. 选一个**把 `MediaBox` 只写在根 `Pages` 节点**的 PDF（许多生成器如此），分别用可继承版与非可继承版读取第 0 页。

**需要观察的现象**：可继承版总能拿到正确的页面尺寸；非可继承版在该页自身没写 `MediaBox` 时返回空矩形（`fz_is_empty_rect` 为真）。手动拆数组得到的 `[0]`、`[3]` 与 `mb.x0`、`mb.y1` 数值一致。

**预期结果**：`MediaBox` 通常是 `[0 0 612 792]`（US Letter，单位为 1/72 英寸）或 `[0 0 595 842]`（A4）。**待本地验证**：具体数值取决于所用 PDF。

#### 4.3.5 小练习与答案

**练习 1**：为什么读取页面 `MediaBox` 应该用 `pdf_dict_get_inheritable_rect` 而不是 `pdf_dict_get_rect`？

> **答案**：因为 PDF 规范允许 `MediaBox` 等「可继承属性」写在父级 `Pages` 节点上由子页面继承。若某页自身未写 `/MediaBox`，`pdf_dict_get_rect` 只看本页字典会得到空矩形；`pdf_dict_get_inheritable_rect` 会沿 `/Parent` 链向上找到真正定义它的祖先节点。这正是 `pdf_run.c` 用前者的原因。

**练习 2**：`pdf_to_int(ctx, PDF_NAME(MediaBox))` 会抛异常吗？返回什么？

> **答案**：不抛异常。`pdf_to_*` 系列的设计就是「静默失败」——`PDF_NAME(MediaBox)` 是单例名字（`< PDF_LIMIT`），`pdf_to_int` 直接返回 0。这正是它与「抛异常的严格取值」的关键区别：它把类型不匹配当成「没有值」而非错误。

**练习 3**：`pdf_dict_get_inheritable` 是怎么防止损坏 PDF 里 `/Parent` 循环导致死循环的？

> **答案**：用改进版 Floyd 环检测——快指针 `node` 每轮上移一个 `/Parent`，慢指针 `slow` 延迟启动后每两轮上移一次；若 `node == slow` 即判定有环。首次发现环会调用 `pdf_repair_page_tree_parents` 尝试修复并重试一次，仍环才抛 `FZ_ERROR_FORMAT`，避免无限递归/死循环。

## 5. 综合实践

把三个模块串起来，完成一次「对象树巡游」：

> 任务：打开一个 PDF，从 trailer 出发，**全程只用对象模型 API**，依次完成——
>
> 1. 取出 Catalog：`pdf_dict_get(ctx, pdf_trailer(ctx, doc), PDF_NAME(Root))`；
> 2. 从 Catalog 取出 `Pages` 根节点，并打印其 `/Count`（用 `pdf_dict_get_int`）；
> 3. 取第 0 页的页面对象（用 `pdf_load_page` 拿 `pdf_page`，再读 `page->obj`），打印其 `/Type` 名字（应为 `Page`）；
> 4. 用 `pdf_dict_get_inheritable_rect` 读取该页 `MediaBox`，并换算成英寸（除以 72）打印页面物理尺寸；
> 5. 额外挑战：用 `pdf_dict_getp(ctx, pdf_trailer(ctx, doc), "Root/Pages/Count")` 一行完成第 2 步，验证路径式取值。

**验收标准**：

- 全程没有对 get 的返回值调用 `drop`（体现「借用引用」理解）；
- 能正确解释为什么第 4 步必须用可继承版本；
- 能说出 `PDF_NAME(Root)` 与字符串 `"Root"` 两种键在内部查找路径上的差异。

**提示**：可对照 [pdf-page.c:267-268](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-page.c#L267-L268) 与 [pdf-run.c:99-102](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-run.c#L99-L102) 这两处项目自身的真实用法，确保你的写法与之一致。

## 6. 本讲小结

- `pdf_obj` 用一个 `pdf_objkind` 枚举为**堆对象**定义了 7 种 kind（int/real/string/name/array/dict/indirect），而 null 与 boolean 不占堆，以单例小整数指针表示。
- **单例编码**是贯穿全篇的关键：`PDF_NULL`/`PDF_TRUE`/`PDF_FALSE` 及所有常用名字（`PDF_NAME(X)`）是 `< PDF_LIMIT` 的小整数指针，真实堆对象 `>= PDF_LIMIT`；因此每个类型判断都先看 `obj >= PDF_LIMIT` 再读 `kind`，对单例名字的比较可退化为指针相等。
- 构造、判断、取值前都会经过 `RESOLVE`，把间接引用透明换成实物；这是「你拿到的可能是引用壳」这一直觉的来源。
- `pdf_array_get` / `pdf_dict_get` / `pdf_dict_gets` / `pdf_dict_getp` 返回**借来的引用**，不可 drop；字典查找在已排序时为 \(O(\log n)\) 二分、否则 \(O(n)\) 线性。
- `pdf_to_*` 是**静默失败**的类型转换（类型不符返回 0/`""`，不抛异常）；`pdf_dict_get_int` 等是「取值+转换」的一行封装。
- `pdf_dict_get_inheritable` 沿 `/Parent` 链查找可继承属性（如 `MediaBox`），用改进版 Floyd 环检测防止损坏 PDF 的循环，是读取页面尺寸等属性的正确方式。

## 7. 下一步学习建议

- **u7-l2（xref 与间接对象）**：本讲反复出现的 `RESOLVE` / `pdf_resolve_indirect_chain` 到底怎么把 `PDF_INDIRECT` 引用壳换成磁盘上的真实对象？答案在 `pdf-xref.c` 的交叉引用表与对象缓存里。这是自然的下一站。
- **u7-l3（词法、解析与写入）**：本讲的 `pdf_obj` 树是怎么从一份 PDF 文件的字节流「长」出来的？去看 `pdf-lex.c` 的词法切词与 `pdf-parse.c` 的语法解析（`pdf_to_rect` 所在文件）。
- **延伸阅读**：浏览 [include/mupdf/pdf/object.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/object.h) 里尚未讲到的「写」侧 API（`pdf_dict_put_*`、`pdf_array_push_*`）以及 `pdf_mark_obj`/`pdf_cycle` 这类遍历对象树时防环的辅助函数，它们在 u7-l4 解释内容流、以及 u10 操作表单/标注时会大量出现。
