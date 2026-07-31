# 书签大纲

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `build_outline()` 如何把 Typst 文档里的标题（`HeadingElem`）查询出来，并整理成 PDF 的「书签大纲（bookmark outline）」。
- 解释 `bookmarked` 与 `outlined` 两个标志的**回退关系**：默认情况下 PDF 书签跟随 Typst 大纲，何时才会分道扬镳。
- 理解 `OutlineNode::build_tree()` 如何把「扁平的标题列表」还原成一棵**层级树**，以及 `convert_node()` 如何递归地把这棵树翻译成 krilla 的 `OutlineNode`。
- 认识大纲条目的**目的地址**如何复用 u4-l14 讲过的 `pos_to_xyz()`，以及带编号标题的显示文本（`numbers`）是如何拼出来的。

本讲承接 u2-l6（页面导出与 `PageIndexConverter`）与 u4-l14（链接与目的地址）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**① PDF 书签大纲 ≠ Typst 的 `#outline()`。** Typst 源码里写的 `#outline()` 是排版在页面上的「目录」，是可见的图形内容；而 PDF 书签大纲（PDF 规范里的 *document outline / bookmarks*）是 PDF 阅读器左侧那棵可折叠的导航树，是**元数据**，不占版面。`typst-pdf` 的 `build_outline()` 生成的就是后者。

**② 书签是一棵「目的地址树」。** 每个书签条目由两样东西组成：一段**显示文本**（标题）和一个**目的地址（destination）**——「点一下跳到第几页的哪个坐标」。这和 u4-l14 讲的链接目的地本质相同，所以本讲会直接复用 `pos_to_xyz()`。

**③ Typst 的标题天然带层级（`level`）。** `= 一级`、`== 二级`、`=== 三级`……书签树的层级就直接来自这些 level，但代码里需要先把标题**拍平查询**出来，再根据 level **重建**成树。这个「拍平 → 重建」的过程是本讲的核心机制。

> 复习：`Smart<T>` 是 Typst 里表达「自动 / 手动」的三态容器，`Auto` 表示「用默认行为」，`Custom(v)` 表示「我明确指定了 v」。`Smart<bool>::get()` 返回 `Option<bool>`：`Auto → None`，`Custom(b) → Some(b)`。这在本讲的 `bookmarked` 回退逻辑里会用到。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-pdf/src/outline.rs` | **本讲主角**。只有约 85 行，定义 `build_outline()`、`convert_list()`、`convert_node()`，把 Typst 标题翻译成 krilla 书签大纲。 |
| `crates/typst-pdf/src/link.rs` | 提供 `pos_to_xyz()`（u4-l14 精读过），把一个 `PagedPosition` 变成 PDF 的 XYZ 目的地。大纲条目的跳转目标由它计算。 |
| `crates/typst-pdf/src/convert.rs` | 在 `convert()` 编排的收尾阶段调用 `document.set_outline(build_outline(&gc))`（第 90 行），把书签挂到 krilla 文档上。 |
| `crates/typst-library/src/model/heading.rs` | `HeadingElem` 的定义：`bookmarked`、`outlined`、`numbers`、`body` 等字段。 |
| `crates/typst-library/src/model/outline.rs` | 通用 `OutlineNode<T>::build_tree()`：把扁平 `(entry, level, include)` 列表变成层级树。注意这是 typst-**library** 里的通用工具，不仅服务于 PDF 书签。 |
| `crates/typst-library/src/layout/page.rs` | `PageRanges::includes_page()`：判断某页是否在导出范围内。 |

## 4. 核心概念与源码讲解

### 4.1 从标题查询到 PDF 大纲：`build_outline` 的三步

#### 4.1.1 概念说明

`build_outline()` 是 PDF 书签的**唯一入口**，它接收一个 `GlobalContext`（贯穿导出全程的状态容器，见 u2-l5），返回一棵 krilla 的 `KrillaOutline`。它的工作可以概括为三步：

1. **查询**：用 introspector 把文档里所有 `HeadingElem` 收集成一个扁平列表。
2. **筛选 + 重建**：对每个标题计算「是否纳入书签（`include`）」与「层级（`level`）」，再用 `OutlineNode::build_tree` 还原成层级树。
3. **翻译**：递归地把这棵 Typst 树翻译成 krilla 的 `OutlineNode` 树，组装成 `KrillaOutline`。

它**不**自己拼 PDF 字节，而是构造 krilla 对象，最终序列化由 krilla 完成（见 u1-l1 的「适配器层」心智模型）。

#### 4.1.2 核心流程

```
build_outline(gc)
  │
  ├─ ① introspector().query(HeadingElem)      → 拿到扁平的标题元素列表 elems
  │
  ├─ ② 对每个 elem 计算 (heading, level, include)
  │       level    = heading.resolve_level()
  │       include  = bookmarked标志 && visible(page_ranges 过滤)   ← 见 4.2
  │
  ├─ ③ OutlineNode::build_tree(flat)          → 把扁平列表还原成层级树 tree   ← 见 4.3
  │
  └─ ④ convert_list(&tree, gc)                → 递归翻译成 Vec<KrillaOutlineNode>  ← 见 4.4
          并逐个 outline.push_child(child)
       返回 KrillaOutline
```

#### 4.1.3 源码精读

入口函数整体（85 行里占了约 37 行）：

[crates/typst-pdf/src/outline.rs:11-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L11-L47) —— `build_outline` 主体：查询标题、计算三元组、建树、组装。

第一步「查询」用到了 introspector 的 `query`：

```rust
let elems = gc.document.introspector().query(&HeadingElem::ELEM.select());
```

这里 `HeadingElem::ELEM.select()` 构造一个「选中所有标题元素」的选择器。`introspector()` 是 Typst 内省系统（introspection）的入口，它能回答「文档里哪些元素满足某条件」「某元素排在第几页」等问题。注意：这里查询到的是**所有**标题，无论它是否被 `outlined: false` 排除——是否排除在下一步的 `include` 里决定。

> 同样的「查询全部标题」也出现在 `convert.rs` 的 `collect_named_destinations()` 里，用于收集带 label 的标题作为命名目的地址（u4-l14）。两处是平行的需求。

第三步之后，把树交给 `convert_list`，再把结果逐个 `push_child` 进一个全新的 `KrillaOutline`：

```rust
let mut outline = KrillaOutline::new();
for child in convert_list(&tree, gc) {
    outline.push_child(child);
}
outline
```

这棵 `KrillaOutline` 最终在 `convert()` 收尾时挂到文档上：

[crates/typst-pdf/src/convert.rs:90-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L90-L92) —— `document.set_outline(build_outline(&gc))`，与元数据、结构树同属收尾阶段的「文档级特性」设置。

#### 4.1.4 代码实践

**实践目标**：确认「书签来自 `HeadingElem` 查询」，并观察 `set_outline` 在整体编排里的位置。

**操作步骤**：

1. 打开 `src/outline.rs:12`，确认查询的选择器是 `HeadingElem::ELEM.select()`。
2. 打开 `src/convert.rs`，在第 86–92 行附近观察调用顺序：`convert_pages` → `attach_files` → `tags::resolve` → `set_outline` → `set_metadata` → `set_tag_tree` → `finish`。

**需要观察的现象**：`build_outline` 在 `convert_pages` **之后**才被调用——也就是说，必须先把页面都导出完毕，introspector 才能给出稳定的标题页码，书签的目的地址才可靠。

**预期结果**：你能用一句话说清「为什么书签生成必须排在页面导出之后」——因为目的地址依赖已确定的页号映射（`PageIndexConverter`，见 u2-l6）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `HeadingElem::ELEM.select()` 换成只选一级标题的选择器，书签树会变成什么样？

**参考答案**：扁平列表里只剩一级标题，`build_tree` 重建出的树只有一层、没有子节点；最终 PDF 书签也就是一串平级的一级标题条目，没有可折叠的层级。

**练习 2**：`build_outline` 为什么返回 `KrillaOutline` 而不是 `Vec<u8>`？

**参考答案**：因为 typst-pdf 是适配器层，只负责构造 krilla 的对象模型；真正的 PDF 字节序列化由 krilla 的 `finish()` 在 `convert()` 最后完成。

---

### 4.2 `bookmarked`/`outlined` 回退与 `page_ranges` 可见性过滤

#### 4.2.1 概念说明

「哪些标题进书签」由一个布尔量 `include` 决定，它由两部分相与：

\[
\text{include} = \text{bookmarked} \;\land\; \text{visible}
\]

- **bookmarked**：用户对「这个标题要不要进 PDF 书签」的意愿。Typst 给标题提供了**两个**相关字段：
  - `outlined: bool`（默认 `true`）：控制标题是否进 Typst 的 `#outline()` 目录。
  - `bookmarked: Smart<bool>`（默认 `Auto`）：**专门**控制是否进 PDF 书签。
  
  二者的关系是「**书签默认跟随目录**」：当 `bookmarked` 为 `Auto` 时，回退到 `outlined` 的值；只有用户显式写 `bookmarked: true/false` 时，PDF 书签才和 Typst 目录分道扬镳。

- **visible**：当用 `PdfOptions::page_ranges` 只导出部分页面时，过滤掉「目标页不在导出范围」的标题，避免书签指向一个根本不存在的页面。

#### 4.2.2 核心流程

```
对每个 heading:
  bookmarked = heading.bookmarked.get()                 // Smart<bool> → Option<bool>
                 .unwrap_or_else(|| heading.outlined.get())   // Auto 时回退到 outlined

  visible = page_ranges 为 None             → true（导出全部页，都可见）
          | page_ranges 为 Some(ranges)     → !ranges.includes_page(标题所在页)

  include = bookmarked && visible
  产出元组 (heading, level, include)
```

`bookmarked` 的回退真值表：

| 用户设置 | `bookmarked.get()` | 最终 `bookmarked` 值 | 含义 |
|----------|--------------------|----------------------|------|
| `bookmarked: auto`（默认）+ `outlined: true` | `None` → 回退 | `true` | 书签跟随目录，进书签 |
| `bookmarked: auto` + `outlined: false` | `None` → 回退 | `false` | 既不在目录也不在书签 |
| `bookmarked: true` | `Some(true)` | `true` | 即使 `outlined: false` 也进书签 |
| `bookmarked: false` | `Some(false)` | `false` | 即使在目录里也不进书签 |

#### 4.2.3 源码精读

`bookmarked` 的回退逻辑（注意源码里变量名拼写为 `boomarked`，多了一个 `o`，是源码里的一处笔误，但它就是「bookmarked 标志」）：

[crates/typst-pdf/src/outline.rs:20-23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L20-L23) —— `bookmarked.get().unwrap_or_else(|| outlined.get())`，`Auto` 回退到 `outlined`。

这两个字段的定义在 typst-library，默认值很关键：

[crates/typst-library/src/model/heading.rs:192-193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L192-L193) —— `outlined` 默认 `true`。

[crates/typst-library/src/model/heading.rs:214-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L214-L215) —— `bookmarked` 默认 `Smart::Auto`。

`visible` 的计算与 `include` 的合成：

[crates/typst-pdf/src/outline.rs:25-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L25-L34) —— `visible` 用 `is_none_or` + `!includes_page` 表达，`include = bookmarked && visible`。

阅读这段要特别留意两个 API 的语义：

- `Option::is_none_or(f)`：当 `page_ranges` 为 `None` 时直接返回 `true`（没有限制 → 全可见）；为 `Some(ranges)` 时返回 `f(ranges)`。
- `PageRanges::includes_page(page)`：判断该页**是否在导出范围内**（即在 `page_ranges` 列出的区间里），见 [crates/typst-library/src/layout/page.rs:776-781](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L776-L781)。

> **务必按字面理解这段表达式**：当设置了 `page_ranges` 时，`visible = !includes_page(标题所在页)`，也就是说从字面上看，`visible` 为真对应「标题所在页**不在**导出范围内」的情形。再加上 4.4 节 `convert_node` 里 `pos_to_xyz` 对未导出页会返回 `None` 而二次过滤，二者叠加后的**最终效果**请你在下面的实践里用具体例子跟踪验证，不要凭直觉下结论。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `bookmarked`/`outlined` 的回退关系，并跟踪 `page_ranges` 下 `visible` 的真实行为。

**操作步骤**：

1. 准备一个最小 Typst 文档（示例代码，不是项目原有文件）：

   ```typst
   #set heading(numbering: "1.")

   = 可见且在目录 <a>
   #heading(outlined: false, bookmarked: true)[只进书签] <b>
   #heading(outlined: true, bookmarked: false)[只在目录] <c>
   = 普通二级
   ```

2. 用 typst CLI 导出 PDF：`typst compile doc.typ`，然后在 PDF 阅读器左侧的「书签」面板观察 `<a>`、`<b>`、`<c>` 三个标题分别是否出现。

3. 再写一份多页文档（每个标题独占一页），用 `typst compile --page-range 1,3 doc.typ`（或通过 PDF 导出选项设置 `page_ranges`）只导出第 1、3 页，观察书签面板里还剩哪些条目。

**需要观察的现象**：

- 步骤 2：`<b>`（`outlined: false, bookmarked: true`）应该**出现**在书签里；`<c>`（`bookmarked: false`）应该**不出现**；`<a>` 跟随默认，出现。
- 步骤 3：跟踪「第 2 页的标题」在 `visible` 与 `convert_node` 的 `pos_to_xyz` 两道过滤下，最终是否出现在书签里。

**预期结果**：步骤 2 用于确认回退表；步骤 3 的具体结果**待本地验证**——请结合 4.2.3 的字面语义与 4.4 的二次过滤，把跟踪过程与实际输出对齐。

#### 4.2.5 小练习与答案

**练习 1**：用户写 `#heading(outlined: false)[X]`，没设 `bookmarked`。`X` 会进 PDF 书签吗？

**参考答案**：不会。`bookmarked` 默认 `Auto`，回退到 `outlined`，而 `outlined = false`，所以 `bookmarked` 最终为 `false`，`include` 为 `false`。

**练习 2**：为什么 Typst 要把 `bookmarked` 设计成 `Smart<bool>` 而不是普通 `bool`？

**参考答案**：因为需要区分三种状态——「跟随目录（Auto）」「强制进书签（true）」「强制不进书签（false）」。普通 `bool` 只有两种状态，无法表达「跟随」这一默认语义。

---

### 4.3 `build_tree`：扁平列表 → 层级树

#### 4.3.1 概念说明

第 ② 步得到的是一个**扁平**的三元组列表 `Vec<(heading, level, include)>`，顺序就是标题在文档里的出现顺序。但书签需要的是**嵌套**结构：二级标题是一级标题的子节点。`OutlineNode::build_tree()` 负责这次「扁平 → 树」的还原。

这个函数定义在 typst-**library** 里、是泛型的 `OutlineNode<T>::build_tree`，不仅 PDF 书签用，Typst 自己的 `#outline()` 目录也用它。`typst-pdf` 只是把 `T` 实例化成 `&Packed<HeadingElem>` 来复用它。

#### 4.3.2 核心流程

`build_tree` 的关键思想：用一个游标 `children` 始终指向「当前应该挂载的父节点的子列表」，然后逐个处理扁平项：

```
对每个 (entry, level, include):
  若 include == true:
      从根 tree 开始向下走：
        只要「上一个同级节点 level < 当前 level」
           （且没有更浅的被跳过祖先挡路），
        就进入它的 children，继续往下找
      把当前节点 push 到找到的 children 里
  若 include == false（被跳过）:
      记录「最近被跳过祖先的最浅 level」（last_skipped_level）
```

这里有个精妙的细节：**被跳过的标题（`include=false`）仍然会影响树形**。设想「一级(含) → 一级(跳) → 二级(跳) → 三级(含)」的情形——最后一个三级标题不能被错误地塞进第一个一级标题下面，因为中间那几个被跳过的标题其实是它的祖先。`last_skipped_level` 就是用来挡住这种「误挂」的。

#### 4.3.3 源码精读

`build_outline` 里的调用只有一行：

[crates/typst-pdf/src/outline.rs:39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L39) —— `let tree = OutlineNode::build_tree(flat);`。

真正的算法在 typst-library：

[crates/typst-library/src/model/outline.rs:338-394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L338-L394) —— `build_tree` 主体，含详细的「向下走 / 被跳过祖先」注释。

核心循环（节选关键部分）：

```rust
for (entry, level, include) in flat {
    if include {
        let mut children = &mut tree;
        // 向下走到合适的父节点
        while children.last().is_some_and(|last| {
            last_skipped_level.is_none_or(|l| last.level < l)
                && last.level < level
        }) {
            children = &mut children.last_mut().unwrap().children;
        }
        last_skipped_level = None;
        children.push(OutlineNode { entry, level, children: vec![] });
    } else if last_skipped_level.is_none_or(|l| level < l) {
        // 只记最浅的被跳过祖先
        last_skipped_level = Some(level);
    }
}
```

`OutlineNode` 本身是个朴素的三字段结构体：

[crates/typst-library/src/model/outline.rs:323-330](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L323-L330) —— `entry`（条目）、`level`（层级）、`children`（子节点）。

#### 4.3.4 代码实践

**实践目标**：用一个小例子手工模拟 `build_tree`，理解「被跳过祖先」的作用。

**操作步骤**：假设扁平列表是（`✓` 表示 `include=true`，`✗` 表示 `false`）：

```
L1 ✓   (A)
L1 ✗   (B, 被跳过)
L2 ✗   (C, 被跳过)
L3 ✓   (D)
```

1. 处理 A(L1,✓)：`tree` 为空，A 挂到根 → `[A]`。
2. 处理 B(L1,✗)：记 `last_skipped_level = 1`。
3. 处理 C(L2,✗)：`2 < 1` 为假，不更新（保留更浅的 1）。
4. 处理 D(L3,✓)：从根开始，`children.last()` 是 A（level 1）。while 条件里 `last_skipped_level.is_none_or(|l| last.level < l)` 即 `1 < 1` 为**假**，所以**不进入** A 的 children，D 直接挂到根，与 A 平级 → `[A, D]`。

**需要观察的现象**：D 没有被错误地塞进 A 下面，而是顶替了它被跳过的祖先 B 的「一级位置」。

**预期结果**：最终树是 `[A, D]` 两棵平级子树。如果去掉 `last_skipped_level` 这个挡路逻辑，D 会被错误地挂成 A 的孙节点。

#### 4.3.5 小练习与答案

**练习**：为什么 `build_tree` 要用泛型 `OutlineNode<T>`，而不是写死 `HeadingElem`？

**参考答案**：因为这个「扁平 → 树」的算法对任何「带 level 的可大纲元素」都通用——Typst 的 `#outline()` 目录、PDF 书签、未来其他可大纲元素都能复用同一份代码，`T` 只是「条目里装什么」的占位。

---

### 4.4 `convert_node`：标题文本拼接与目的地址、递归

#### 4.4.1 概念说明

`build_tree` 产出的是 Typst 侧的 `OutlineNode<&Packed<HeadingElem>>` 树。最后一步 `convert_list` / `convert_node` 把它**逐节点翻译**成 krilla 侧的 `KrillaOutlineNode`。每个节点的翻译要做三件事：

1. **算目的地址**：用 `pos_to_xyz()`（u4-l14）把标题位置变成 PDF 的 XYZ 目的地。
2. **拼显示文本**：如果有编号（`numbers`），把编号前缀拼到标题正文前面。
3. **递归子节点**：对 `children` 递归调用 `convert_list`。

#### 4.4.2 核心流程

```
convert_list(nodes):  对每个 node 调 convert_node，filter_map 掉返回 None 的
convert_node(node):
  pos = introspector.position(node.entry.location())   // 标题落在哪一页哪个点
  text = node.entry.body.plain_text()                  // 标题正文纯文本
  title = 有 numbers ? format!("{numbers} {text}") : text
  dest = pos_to_xyz(page_index_converter, pos)         // → Option<XyzDestination>
  if dest 是 None: 返回 None（这页没导出，丢弃）        // ← 第二道过滤
  else:
      node = KrillaOutlineNode::new(title, dest)
      对 node.children 递归 convert_list，逐个 push_child
      返回 Some(node)
```

#### 4.4.3 源码精读

`convert_list` 只是对 `convert_node` 的 `filter_map` 包装：

[crates/typst-pdf/src/outline.rs:49-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L49-L54) —— `filter_map` 自动丢弃 `convert_node` 返回 `None` 的节点。

`convert_node` 主体，重点看文本拼接、目的地址与递归三段：

[crates/typst-pdf/src/outline.rs:56-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L56-L84) —— 完整的节点翻译。

**带编号标题的文本拼接**（这是本讲实践任务的一部分）：

```rust
// Prepend the numbers to the title if they exist.
let text = node.entry.body.plain_text();
let title = match &node.entry.numbers {
    Some(num) => format!("{num} {text}"),
    None => text.to_string(),
};
```

[crates/typst-pdf/src/outline.rs:67-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L67-L72) —— `numbers` 非空时拼成 `"{编号} {正文}"`。

关于 `numbers` 字段：它在 `heading.rs` 里声明为 `pub numbers: EcoString`，但带有 `#[synthesized]` 属性。该属性会把字段**包装成 `Option<EcoString>`**（合成阶段 `self.numbers = Some(...)` 赋值，见 [heading.rs:277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L277)），所以这里能用 `Some(num)` / `None` 匹配。它的注释也点明了用途——「This field is internal and only used for creating PDF bookmarks」：导出阶段拿不到 `World`/`Engine`/`styles` 去实时解析计数器，所以在排版合成阶段就把编号算好存成纯文本，PDF 书签直接取用。字段声明见 [heading.rs:154-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L154-L156)。

于是，一个 `#set heading(numbering: "1."); = 引言` 的标题，`numbers` 在合成阶段被解析为类似 `"1."`，书签显示文本就是 `"1. 引言"`。注意中间有一个空格（`format!("{num} {text}")`）。

**目的地址与第二道过滤**：

```rust
if let Some(dest) = crate::link::pos_to_xyz(&gc.page_index_converter, pos) {
    let mut outline_node = KrillaOutlineNode::new(title, dest);
    for child in convert_list(&node.children, gc) {
        outline_node.push_child(child);
    }
    return Some(outline_node);
}
None
```

[crates/typst-pdf/src/outline.rs:74-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/outline.rs#L74-L83) —— `pos_to_xyz` 返回 `None`（页未导出）时整个节点被丢弃；否则建节点并递归挂子节点。

这里调用的 `pos_to_xyz` 就是 u4-l14 精读过的同一个函数：

[crates/typst-pdf/src/link.rs:201-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L201-L209) —— 用 `PageIndexConverter` 把文档页号重映射为 PDF 页号，并把 y 坐标减 10pt 做基线偏移；未导出的页返回 `None`。

> 这就是 4.2 节提到的「第二道过滤」：即便一个标题在 `build_outline` 的 `include` 里通过了，如果它的目标页被 `page_ranges` 排除，`pos_to_xyz` 仍会返回 `None`，`convert_node` 就丢弃它。两道过滤的**叠加效果**需要在实践中跟踪验证。

#### 4.4.4 代码实践

**实践目标**：搞清「带编号标题在书签里显示成什么」，并验证 `pos_to_xyz` 的二次过滤。

**操作步骤**：

1. 准备文档：

   ```typst
   #set heading(numbering: "1.a")
   = 引言        // 预期 numbers = "1."，书签显示 "1. 引言"
   == 背景       // 预期 numbers = "1.a"，书签显示 "1.a 背景"
   == 动机
   = 方法        // 预期 numbers = "2."
   ```

2. `typst compile doc.typ`，打开书签面板核对显示文本。
3. 对照 `outline.rs:67-72` 的 `format!("{num} {text}")`，确认编号与正文之间确实有一个空格。

**需要观察的现象**：书签条目的文本与 `numbers` 合成值完全一致（含前缀编号和空格）；层级（一级、二级）与 `build_tree` 重建的树形吻合。

**预期结果**：书签显示为可折叠树，根节点 `1. 引言` 下挂 `1.a 背景`、`1.a 动机`，平级还有 `2. 方法`。若 `numbers` 的具体字符串与预期不符，说明合成阶段的编号解析逻辑（在 typst-library）需要进一步查看——本讲不展开。

#### 4.4.5 小练习与答案

**练习 1**：一个标题的 `numbers` 为 `None`，书签显示文本是什么？

**参考答案**：只显示正文 `text.to_string()`，没有编号前缀。`numbers` 为 `None` 通常是因为标题未设置 `numbering`。

**练习 2**：`convert_node` 为什么返回 `Option<KrillaOutlineNode>` 而不是直接 `KrillaOutlineNode`？

**参考答案**：因为目标页可能未导出，`pos_to_xyz` 返回 `None`，此时无法构造有效目的地，只能丢弃该节点。`convert_list` 用 `filter_map` 把这些 `None` 过滤掉，保证书签里不出现死链接。

---

## 5. 综合实践

把本讲的三块知识（查询与 `include`、`build_tree`、`convert_node` 文本与目的地址）串起来，做一次端到端跟踪。

**任务**：给下面这份文档，手工预测 PDF 书签的**完整结构**（层级 + 每条显示文本），再用编译结果验证。

```typst
#set heading(numbering: "1.")

= 绪论
== 研究问题
#heading(outlined: false, bookmarked: true)[附录性补充]   // 想想它进不进书签
#heading(outlined: true, bookmarked: false)[草稿片段]     // 想想它进不进书签
= 方法
== 数据
```

**跟踪要点**：

1. 对 6 个标题分别算 `bookmarked`（注意两个特殊标题的回退）、`visible`（无 `page_ranges`，全为 `true`）、`include`。
2. 用 4.3 的 `build_tree` 规则，把通过 `include` 的标题还原成树。
3. 用 4.4 的文本拼接规则，写出每条的显示文本（带编号的形如 `"1. 绪论"`、`"1.a ..."`，注意 `numbering: "1."` 下二级编号的实际形式）。
4. 编译后在阅读器书签面板核对。

**预期结果**（待本地验证编号字符串细节）：

- `绪论` → 进书签（默认跟随 `outlined: true`），显示 `1. 绪论`。
- `研究问题` → 进书签，作为 `绪论` 子节点。
- `附录性补充`（`outlined: false, bookmarked: true`）→ **进**书签（`bookmarked` 显式为 `true`）。
- `草稿片段`（`bookmarked: false`）→ **不进**书签。
- `方法` → 进书签。
- `数据` → 进书签，作为 `方法` 子节点。

如果实际输出与预测不符，回到对应小节的源码行号核对——这正是「源码阅读型实践」的价值：用可观察的输出反向校准你对代码的理解。

## 6. 本讲小结

- `build_outline()` 是 PDF 书签的唯一入口，三步走：**查询全部标题 → 算 `include` 并用 `build_tree` 重建层级树 → 递归翻译成 krilla `OutlineNode`**。
- `include = bookmarked && visible`：`bookmarked` 默认 `Auto` 时**回退到 `outlined`**（书签跟随目录）；`visible` 由 `page_ranges` 决定，表达式为 `is_none_or(|r| !r.includes_page(page))`，须按字面理解。
- `OutlineNode::build_tree` 是 typst-library 里的**通用**「扁平 → 树」算法，用 `last_skipped_level` 处理「被跳过祖先」造成的误挂。
- `convert_node` 做三件事：用 `pos_to_xyz` 算目的地址（未导出页返回 `None` 即二次过滤）、把 `numbers` 前缀拼到正文前形成显示文本、递归挂子节点。
- 带编号标题的书签文本形如 `"{numbers} {正文}"`；`numbers` 是排版合成阶段预先算好的纯文本（`#[synthesized]` 包成 `Option`），因为导出阶段拿不到实时解析编号所需的环境。
- 书签生成排在 `convert_pages` 之后，因为目的地址依赖已确定的页号映射。

## 7. 下一步学习建议

- **本单元后续**：u4-l16 讲元数据与时间戳（`build_metadata`，与 `build_outline` 同属 `convert()` 收尾阶段的文档级特性），u4-l17 讲文件附件。可顺读这两讲，凑齐「文档级特性」全貌。
- **深入目的地址**：本讲和 u4-l14 都重度依赖 `pos_to_xyz` 与 `PageIndexConverter`（u2-l6）。如果对页号重映射或 10pt 基线偏移还有疑问，回看 u2-l6 的 `PageIndexConverter::new`。
- **专家层预告**：u5 单元会进入 tagged PDF 子系统。书签（outline）与 tagged PDF 是两套**独立**的 PDF 特性——书签是导航树，tagged PDF 是无障碍结构树；但二者都建立在「查询 introspection + 用 `pos_to_xyz` 算位置」的共同基座上，学完本讲再进 u5 会更顺。
