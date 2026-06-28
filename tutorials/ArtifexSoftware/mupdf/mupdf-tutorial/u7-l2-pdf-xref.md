# xref 交叉引用表与间接对象解析

## 1. 本讲目标

在上一篇（u7-l1）里，我们已经认识了 `pdf_obj` 七种对象类型，并提到「`PDF_INDIRECT` 只是一个『引用壳』，真正的解引用实现位于 xref」。本讲就把这句话彻底讲透。读完本讲，你应当能够：

1. 说清楚 PDF 的 **xref（cross-reference，交叉引用）表** 是什么、它在磁盘上长什么样、在内存里又被组织成什么数据结构。
2. 解释 **`pdf_resolve_indirect`** 是如何把一个间接引用「透明地」变成真实对象的，以及它如何防止「A 引用 B、B 引用 A」这种损坏 PDF 造成的死循环。
3. 理解 **对象缓存与懒加载** 的设计：对象为何只在第一次被用到时才读盘，之后命中 `entry->obj` 缓存就不再做磁盘 I/O；以及对象流（object stream）一次解压会顺带把多个对象一起缓存进来的批量加载行为。

本讲承接 u7-l1 的对象模型，是后续 u7-l3（词法/解析/写入）、u7-l4（内容流解释）的底层基础——要读懂任何一篇 PDF，都得先会查 xref 这张「目录」。

## 2. 前置知识

在进入源码前，先用大白话建立三点直觉。

**第一，PDF 是一堆「带编号的对象」散落在文件里。** 一个 PDF 文件本质上不是 XML 那种树状结构，而是一堆被编号的独立小对象（例如 `3 0 obj << /Type /Page >> endobj`），它们可以按任意顺序、甚至带空隙地分布在文件的任何字节位置。每个对象用 `(编号, 生成号)` 这一对数来唯一标识，写作 `3 0 R`（读作「3 0 的引用」）。

**第二，要在这堆对象里定位某一个，就需要一张「目录」。** 这张目录就是 xref 表。它的每一行本质上就是一句话：「编号为 N 的对象，在文件的第 K 个字节处」。没有 xref，要找一个对象就得把整个文件从头扫一遍——对动辄几十 MB 的 PDF 来说完全不可接受。

**第三，「间接引用」是 PDF 节省空间的技巧。** 当一个字典里写着 `/Parent 3 0 R` 时，`3 0 R` 并不是对象 3 本身，而是一个「指针」。读到这里时，MuPDF 必须顺着这个指针去查 xref、把对象 3 真正加载进来。这个「顺着指针取真身」的动作，就是本讲的核心：**解引用（resolve indirect）**。

> 关联记忆：u7-l1 讲过的 `RESOLVE` 宏就是触发解引用的开关；u3-l1 讲过的「虚表 + 判空转发」是 MuPDF 通用层的一贯手法，本讲的 `pdf_resolve_indirect → pdf_cache_object` 也是同一种「薄入口 + 真正干活的核心函数」的分层。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `include/mupdf/pdf/xref.h` | xref 子系统的公开头文件 | 三个核心结构体 `pdf_xref_entry` / `pdf_xref_subsec` / `pdf_xref`，以及 `pdf_cache_object`、`pdf_resolve_indirect` 的声明 |
| `source/pdf/pdf-xref.c` | xref 的全部实现（约 5500 行） | 表的加载、对象缓存、解引用、修复、对象流解包 |
| `source/pdf/pdf-parse.c` | PDF 词法/语法解析 | `pdf_parse_ind_obj`：从字节流解析出 `N G obj ... endobj` |
| `source/pdf/pdf-object.c` | `pdf_obj` 类型系统 | `RESOLVE` 宏与 `pdf_is_indirect`，承接 u7-l1 |
| `source/tools/pdfshow.c` | `mutool show` 调试工具 | `showxref`：把内存里的 xref 表打印出来的现成入口 |
| `include/mupdf/pdf/document.h` | `pdf_document` 结构 | 与 xref 相关的字段：`xref_sections`、`xref_index`、`num_xref_sections` 等 |

## 4. 核心概念与源码讲解

### 4.1 xref 交叉引用表：把对象编号映射到磁盘位置

#### 4.1.1 概念说明

xref 表解决的问题是：「给我一个对象编号 N，告诉我它在哪里」。它的形态有两类，都来自 PDF 规范：

- **老式 xref 表（PDF 1.4 及以前）**：一段纯文本，以 `xref` 关键字开头，后面跟若干「子段（subsection）」，每个子段形如 `start length`，再跟 `length` 行、每行 20 字节的 `ofs gen type` 三列。其中 `type` 是单个字母：`n` 表示 in use（该行 `ofs` 就是对象的文件偏移），`f` 表示 free（空闲，可被复用）。表后面跟一个 `trailer` 字典，里面有 `/Size`（对象总数）、`/Root`（Catalog 引用）等关键信息。
- **新式 xref 流（PDF 1.5+）**：把整张表压进一个**流对象**里，用二进制编码（每个条目按 `w0 w1 w2` 三个宽度编码 type/ofs/gen），trailer 就是这个流对象自身的字典。它还能让对象「住在」压缩流里（即对象流 object stream），大幅减小文件体积。

无论哪种磁盘形态，MuPDF 读进来后都统一存成同一种内存结构。

#### 4.1.2 核心流程

加载一张 xref 表的链路是「从文件尾部往前找」，步骤如下：

1. **找 `startxref`**：跳到文件末尾，往前搜 `startxref` 关键字，读出它后面那个数字——那是主 xref 表在文件中的字节偏移。
2. **读 xref**：跳到该偏移，判断是老式表（`xref` 关键字）还是新式流（流对象），分别走 `pdf_read_old_xref` 或 `pdf_read_new_xref`。
3. **回溯历史版本**：如果该 xref 的 trailer 里有 `/Prev`（增量更新场景），则递归去读更早的 xref 段，直到没有 `/Prev` 为止。每次保存都会追加一段新 xref + 新 trailer，所以一个文件在内存里是**多段叠加**的：新段（数组下标小）覆盖旧段（下标大）。
4. **统一成 `pdf_xref_entry` 数组**：每一段是一个 `pdf_xref`，内部用若干 `pdf_xref_subsec`（连续区间）存放条目。

读对象时，则反过来：**由编号 → 查条目 → 拿到 `ofs` 或「对象流号 + 流内索引」**。

#### 4.1.3 源码精读

先看承载一切的三层结构。最内层是单条记录 `pdf_xref_entry`：

[include/mupdf/pdf/xref.h:65-75](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/xref.h#L65-L75) —— 这是 xref 表的一行，也是本讲最重要的结构。注意几个关键字段的含义：

- `type`：条目状态。`0`=未设置，`'f'`=free 空闲，`'n'`=in use（普通对象），`'o'`=objstm（住在对象流里）。这个字符直接决定了后续如何加载对象。
- `ofs`：双重含义。`type=='n'` 时是对象的文件偏移；`type=='o'` 时是「所在对象流的编号」。
- `gen`：生成号；但 `type=='o'` 时被复用为「在对象流内部的索引」。
- `num`：原始对象编号，重编号后仍用于解密。
- `obj`：**缓存槽**。这是「懒加载 + 缓存」的关键——首次加载后对象就存在这里。

中间层是连续区间 `pdf_xref_subsec`：

[include/mupdf/pdf/xref.h:77-83](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/xref.h#L77-L83) —— 一段编号连续 `[start, start+len)` 的条目表。PDF 在磁盘上用子段来紧凑表示「对象 0~10 是一拨、100~105 是另一拨」这种稀疏分布，避免为中间不存在的对象留空。

最外层是一「版」文件 `struct pdf_xref`：

[include/mupdf/pdf/xref.h:85-95](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/xref.h#L85-L95) —— 一个文件版本：它有一组子段、一个 `trailer` 字典、以及签名相关字段。`doc->xref_sections` 是这种结构的数组，下标从小到大对应从新到旧的版本。

`pdf_document` 里和 xref 相关的字段集中在以下区域：

[include/mupdf/pdf/document.h:478-493](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/document.h#L478-L493) —— 注意 `num_xref_sections`（有多少个叠加版本）、`xref_sections`（版本数组指针）、`xref_index`（下面 4.3 要讲的查找加速表）、`xref_base`（访问历史版本时用的基线下标）、`repair_attempted`（修复标记）。

加载的第一步是定位 `startxref`：

[source/pdf/pdf-xref.c:1010-1049](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L1010-L1049) —— `pdf_read_start_xref` 先 `fz_seek` 到文件末尾拿到 `file_size`，然后只读末尾 1024 字节，从后往前匹配 `"startxref"`，再把后面那段数字解析进 `doc->startxref`。之所以只读末尾一小段，是因为 `startxref` 按规范必须紧贴文件尾部。

老式表的解析逻辑：

[source/pdf/pdf-xref.c:1306-1413](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L1306-L1413) —— `pdf_read_old_xref` 先用 `pdf_xref_size_from_old_trailer` 拿到 `/Size`（对象总数），再跳过 `xref` 关键字，循环读每个子段。关键在第 1355~1397 行：**每个条目固定读 20 字节**，按「跳过空白 → 读 ofs 数字 → 读 gen 数字 → 读 type 字母」填进 `pdf_xref_entry`。注意第 1388~1390 行：合法 type 只允许 `'f'`/`'n'`/`'o'`，否则报格式错。第 1351~1399 行的 `carried` 逻辑是为了兼容那些把每行写成 19 字节（而非规范的 20 字节）的损坏 PDF（比如某些 PCLm 驱动生成的文件）。

新式流的逐条解析：

[source/pdf/pdf-xref.c:1416-1452](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L1416-L1452) —— `pdf_read_new_xref_section` 按 `w0/w1/w2` 三个位宽，分别把 type/ofs/gen 当作大端整数读出来（`a = (a << 8) + byte`）。第 1443~1448 行做类型映射：`t==0→'f'`、`t==1→'n'`、`t==2→'o'`，和老式表最终落到同一套字符编码上——这就是「两种磁盘格式，一种内存结构」的体现。

读取单条目录的统一入口是 `pdf_get_xref_entry`：

[source/pdf/pdf-xref.c:336-337](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L336-L337) 与 [source/pdf/pdf-xref.c:379-403](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L379-L403) —— `pdf_get_xref_entry_aux` 的核心是从 `doc->xref_index[i]`（上次找到 i 的那个版本下标）开始，**从新到旧**遍历 `xref_sections`，返回第一个 `type` 非空的条目。这正体现了「新版本覆盖旧版本」的语义：增量更新后，同编号对象取最新版本。条目在子段表里的地址是一个简单算式：

\[ \text{entry 指针} = \text{sub->table} + (\text{i} - \text{sub->start}) \]

对应代码里的 `&sub->table[i - sub->start]`（见第 392 行）。

#### 4.1.4 代码实践

**实践目标**：用现成的调试工具把一个真实 PDF 的 xref 表「看」出来，亲手验证「编号 → ofs/gen/type」的映射。

**操作步骤**：

1. 先按 u1-l2 编译出 `mutool`（`make` 后位于 `build/release/mutool`）。
2. 找任意一个 PDF 文件（记为 `a.pdf`），执行：

   ```bash
   ./build/release/mutool show a.pdf xref
   ```

3. 再执行以下两条命令对照：

   ```bash
   ./build/release/mutool show a.pdf trailer
   # 想看某个具体对象，用「编号 0 R」形式，例如对象 2：
   ./build/release/mutool show a.pdf 2 0 R
   ```

**需要观察的现象**：

- `mutool show ... xref` 的输出形如 `00003: 0000000123 00000 n`，三列分别是 `编号: ofs gen type`（见工具源码 [source/tools/pdfshow.c:73-87](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/pdfshow.c#L73-L87)）。
- 记下 `type` 列里 `n`（普通）、`o`（对象流）、`f`（free）各有多少个。如果该 PDF 用了对象流压缩，你会看到大量 `o` 且 `ofs` 列是同一个对象流编号。
- `trailer` 输出里有 `/Size`、`/Root`，`/Root` 的值形如 `3 0 R`，正是一个间接引用。

**预期结果**：你会直观看到「xref 就是一张把编号映射到偏移的表」，并且 `trailer` 里的 `/Root 3 0 R` 就是通过这张表去定位 Catalog 对象的。

**如果无法编译运行**：阅读 [source/tools/pdfshow.c:73-87](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/pdfshow.c#L73-L87) 的 `showxref`，它在内存里遍历 `pdf_xref_len` 个条目并打印 `ofs/gen/type`——即「待本地验证」上述现象。

#### 4.1.5 小练习与答案

**练习 1**：xref 条目的 `type` 字段有 `'f'`/`'n'`/`'o'` 三种取值，分别表示什么？`ofs` 字段在 `'n'` 和 `'o'` 两种情况下含义有何不同？

> **答案**：`'f'`=free 空闲对象（可被新对象复用其编号），`'n'`=in use 普通对象，`'o'`=objstm 住在对象流里。`type=='n'` 时 `ofs` 是对象在文件中的字节偏移；`type=='o'` 时 `ofs` 是「所在对象流的编号」，而它在流内的偏移由 `gen` 复用为索引来定位。

**练习 2**：为什么 `pdf_xref_subsec` 要设计成「一段连续区间」的链表，而不是一个从 0 到 Size 的扁平大数组？

> **答案**：磁盘上的 xref 子段天然是稀疏、分段的（如对象 0~5 和 100~120），按子段保留可以忠实、紧凑地表示这种分布，避免为不存在的编号预留空位。需要时（见 4.3 的 `ensure_solid_xref`）才把它们「固化」合并成一个覆盖全范围的扁平表。

### 4.2 pdf_resolve_indirect：间接引用的透明解引用

#### 4.2.1 概念说明

在 u7-l1 里，`pdf_obj` 有一个 `PDF_INDIRECT` kind——它本身不是任何实际数据，只记录「我指向某个文档里的第 N 号对象」。绝大多数取值函数（`pdf_dict_get`、`pdf_to_int` 等）在动手前都会先调用 `RESOLVE` 宏，把这种引用壳「剥开」露出真身。对调用方而言，这个过程是**透明**的：你通常不必关心拿到的是引用还是实物，写代码时几乎看不到「解引用」这一步。

真正干活的核心函数是 `pdf_resolve_indirect`，它在 `pdf-object.c` 里被两个地方调用：一是 `RESOLVE` 宏，二是 `pdf_print_obj`（打印对象时也需先解引用）。

#### 4.2.2 核心流程

解引用一条引用的逻辑可以概括为：

```
pdf_resolve_indirect(ref):
    若 ref 不是 indirect  →  直接返回 ref（已经是真身）
    若 ref 是 indirect：
        doc ← ref 里记录的文档指针
        num ← ref 里记录的编号
        边界检查（0 < num < xref_len）
        entry ← pdf_cache_object(doc, num)   # 真正读盘/取缓存在这里
        返回 entry->obj
```

为防止损坏 PDF 出现 `A→B→A` 这种间接引用成环，还有一层带「安全计数」的 `pdf_resolve_indirect_chain`，它会反复解引用直到结果不再是 indirect，或达到上限。

#### 4.2.3 源码精读

先看 u7-l1 留下的入口——`RESOLVE` 宏：

[source/pdf/pdf-object.c:286-293](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L286-L293) —— `RESOLVE(obj)` 用 `OBJ_IS_INDIRECT` 判断（先比 `PDF_LIMIT` 单例阈值，再读 `kind`），若是引用就调用 `pdf_resolve_indirect_chain`。注意它被展开进各个 `pdf_dict_get_*` / `pdf_to_*` 函数的开头，所以「解引用」对调用方是完全隐形的。

公开声明与实现：

[include/mupdf/pdf/xref.h:117-118](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/xref.h#L117-L118) —— `pdf_resolve_indirect` 与 `pdf_resolve_indirect_chain` 的声明。注释提示「它可能触发 xref 的重整（solidify）/修复，调用后手中持有的 `pdf_xref_entry` 指针都应视为失效」。

[source/pdf/pdf-xref.c:2689-2722](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2689-L2722) —— `pdf_resolve_indirect` 的实现。要点：

1. 第 2692 行先用 `pdf_is_indirect` 判断；非引用直接原样返回（第 2721 行）。
2. 第 2694~2695 行从引用里取出 `doc` 和 `num`——这正是 u7-l1 里说的「引用壳里存着文档指针 + 编号」。
3. 第 2704~2714 行用 `fz_try/fz_catch` 包住真正的加载 `pdf_cache_object`。这里有个**重要的容错策略**：如果加载抛出的是 `FZ_ERROR_TRYLATER`（线性加载还没读到该对象）、`FZ_ERROR_SYSTEM`、`FZ_ERROR_REPAIRED` 这三种，会原样向上抛；其余错误则**降级为警告并返回 NULL**，而不是让整个文档渲染崩溃。这体现了 MuPDF「尽量容错、能渲染就渲染」的工程取向。
4. 第 2716~2717 行：成功后返回 `entry->obj`，**但不增引用计数**——这是「借用引用」，与 u7-l1 的 `pdf_dict_get` 一致，调用方不可 `drop` 它。

防成环版本：

[source/pdf/pdf-xref.c:2724-2741](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2724-L2741) —— `pdf_resolve_indirect_chain` 用 `sanity = 10` 作计数，每次 `pdf_resolve_indirect` 后若结果仍是 indirect 就继续，直到不再是引用或计数耗尽（第 2731~2735 行打印警告并返回 NULL）。这把「多层间接引用」和「引用成环」都兜住了。

`pdf_load_object` 则是「需要持有引用」的兄弟函数：

[source/pdf/pdf-xref.c:2677-2687](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2677-L2687) —— 它和 `pdf_resolve_indirect` 一样调用 `pdf_cache_object`，但返回前多了一步 `pdf_keep_obj`（自增引用计数）。所以二者的区别就是**借用 vs 持有**：`resolve_indirect` 返回的对象不能 drop，`load_object` 返回的对象必须配对一个 `pdf_drop_obj`。

#### 4.2.4 代码实践

**实践目标**：跟踪「拿到 `/Root` 引用后，它如何被透明地变成 Catalog 对象」这一条最常见链路。

**操作步骤**：

1. 阅读式跟踪。从 `source/pdf/pdf-object.c` 第 286 行的 `RESOLVE` 宏出发，对照下面的调用链：

   ```
   pdf_dict_get(ctx, trailer, PDF_NAME(Root))   # 取 trailer 的 /Root
     → 开头展开 RESOLVE(返回值)               # 但 /Root 的值本身是 "3 0 R"
     → pdf_resolve_indirect_chain
     → pdf_resolve_indirect  (pdf-xref.c:2690)
     → pdf_cache_object      (pdf-xref.c:2548)   # 这里才真正去查 xref / 读盘
   ```

2. 命令验证。对一个真实 PDF 执行：

   ```bash
   ./build/release/mutool show a.pdf trailer
   # 假设 trailer 里 /Root 3 0 R，那么下面两条都应能打印出 Catalog 字典：
   ./build/release/mutool show a.pdf trailer/Root
   ./build/release/mutool show a.pdf 3 0 R
   ```

**需要观察的现象**：`trailer/Root` 这种「路径式」写法之所以能直接给出 Catalog 字典内容，正是因为内部对 `3 0 R` 这个引用做了透明解引用。

**预期结果**：你会确认「引用 `3 0 R` 不是实物，必须经 resolve 才变成真实字典」，并且这个过程对上层调用完全隐藏。

**待本地验证**：若手头没有 `mutool`，仅靠阅读上述三处源码即可完成本实践。

#### 4.2.5 小练习与答案

**练习 1**：`pdf_resolve_indirect` 返回的对象，调用方应不应该 `pdf_drop_obj` 它？为什么？

> **答案**：不应该。它在第 2717 行直接返回 `entry->obj`，没有调用 `pdf_keep_obj`，属于「借用引用」——对象的生命周期由 xref 缓存管理。需要长期持有时应改用 `pdf_load_object`（它做了 keep），那样才必须配对 drop。这与 u7-l1 讲的 `pdf_dict_get` 借用语义完全一致。

**练习 2**：为什么要单独搞一个 `pdf_resolve_indirect_chain`，而不是让 `pdf_resolve_indirect` 自己循环？

> **答案**：两点。其一，绝大多数引用只解一层就到真身，`pdf_resolve_indirect` 的单次实现足够轻量；其二，`_chain` 版本用 `sanity=10` 计数专门防御损坏 PDF 的「引用成环」（A→B→A），既避免无限递归/死循环，又能在异常时给出明确警告。把「正常单跳」和「成环兜底」分开，职责更清晰。

### 4.3 pdf_cache_object：对象缓存与按需懒加载

#### 4.3.1 概念说明

`pdf_cache_object` 是整个 xref 子系统的心脏，它同时承担三件事：**懒加载**（按需读盘）、**缓存**（读过的存进 `entry->obj`）、**容错修复**（解析失败时尝试重建 xref）。

懒加载的意义：一个 PDF 可能有成千上万个对象，但渲染某一页通常只用到其中几十个。MuPDF 不会在打开文档时把所有对象都读进来，而是**谁被第一次用到、谁才被读盘解析**。缓存的意义则更大：渲染一页时，同一个字体或图片资源可能被引用几十次，若每次都重新读盘解析，性能不可接受。所以「读一次、存进 `entry->obj`、以后直接命中」是性能关键。

对象流（object stream）带来一个额外的批量行为：当某个 `type=='o'` 的对象被请求时，MuPDF 会把**整个对象流解压**，并把流里**所有**对象一次性都缓存进各自条目——相当于顺带做了预读。

#### 4.3.2 核心流程

`pdf_cache_object(doc, num)` 的主干是一个「按 type 分派」的状态机：

```
pdf_cache_object(num):
    边界检查；x ← pdf_get_xref_entry(num)
    若 x->obj != NULL  →  直接返回 x          # ★缓存命中，零磁盘 I/O
    否则按 x->type 分派：
        'f' (free)        →  x->obj = PDF_NULL
        'n' (in use)      →  seek(bias + ofs)
                             x->obj = pdf_parse_ind_obj(...)   # 读盘解析
                             若 doc->crypt  →  pdf_crypt_obj(...)  # 解密
        'o' (objstm)      →  pdf_load_obj_stm(...)             # 解压对象流，批量缓存
    解析/编号不符 → 尝试一次 pdf_repair_xref 后重试
    pdf_set_obj_parent(x->obj, num)
    返回 x
```

其中**第一行的 `x->obj != NULL` 判断就是缓存命中**——只要之前加载过，立刻返回，完全不碰磁盘。这是「重复解引用不会重复读盘」的根本原因。

#### 4.3.3 源码精读

入口与缓存命中：

[source/pdf/pdf-xref.c:2547-2567](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2547-L2567) —— `pdf_cache_object` 先做边界检查，取条目 `x`，第 2566~2567 行就是**缓存命中快路径**：`if (x->obj != NULL) return x;`。这两行是本讲「缓存」二字的全部落点。

`'f'`/`'n'` 两种普通对象的处理：

[source/pdf/pdf-xref.c:2569-2625](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2569-L2625) —— 对 `'n'` 类型（第 2573 行起）：先 `fz_seek` 到 `doc->bias + x->ofs`（`bias` 是线性化/流式读取场景的基地址偏移，普通文件为 0），再调 `pdf_parse_ind_obj` 从字节流解析出 `N G obj ...`。解析函数在 [source/pdf/pdf-parse.c:932-936](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L932-L936)，它返回对象本体并顺带给出流偏移 `stm_ofs`。解析成功后，若文档有加密（`doc->crypt`），第 2623~2624 行调 `pdf_crypt_obj` **就地解密**——注意用的是 `x->num`（原始编号）而非重编号后的值，因为 PDF 的加密密钥与对象编号绑定。

容错与修复（第 2592~2621 行）是个重要细节：解析回来的对象编号 `rnum` 应当等于请求的 `num`，若不等说明 xref 表与实际对象错位，MuPDF 会把该条目改记为 free 并触发**一次** `pdf_repair_xref`（用 `doc->repair_attempted` 防止无限修复），修完 `goto object_updated` 重试。这就是「损坏 PDF 也能打开」的内部机制。

对象流的批量加载：

[source/pdf/pdf-xref.c:2626-2659](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2626-L2659) —— 对 `'o'` 类型：调用 `pdf_load_obj_stm(ctx, doc, x->ofs, ..., num)`，其中 `x->ofs` 是对象流的编号。注意第 2632 行一个小技巧：进入递归前把 `orig_x->type` 临时从 `'o'` 改成大写 `'O'`，标记「正在递归加载」，加载完再在第 2642~2643 行改回。这是为了防止「对象流加载过程中又来请求它自己」造成无限递归。

对象流内部的解包逻辑：

[source/pdf/pdf-xref.c:2097-2248](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2097-L2248) —— `pdf_load_obj_stm` 先把对象流编号本身 `pdf_cache_object` 出来（第 2130 行），再用 `pdf_open_stream_number` 打开其压缩流；接着读它字典里的 `/N`（对象个数）和 `/First`（首个对象在流内的偏移），按「编号 + 流内偏移」成对读出索引表（第 2172~2188 行）；最后逐个 `pdf_parse_stm_obj` 解析出每个对象，并**把它们各自塞进对应编号的 xref 条目**（第 2209~2243 行）。**关键点**：这里不是只取出目标 `target`，而是把流里**所有**对象都缓存进各自的 `entry->obj`——一次解压，全家受益。这解释了为什么访问对象流里任何一个对象，后续访问同流的其他对象都会命中缓存。

查找加速表 `xref_index`：

[source/pdf/pdf-xref.c:337-403](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L337-L403) —— `pdf_get_xref_entry_aux` 每次查找前先查 `doc->xref_index[i]`（第 346~347 行），它缓存了「编号 i 上次出现在第几个 xref 段」，从而把「从第 0 段线性扫」优化成「从上次命中的段开始扫」。命中后第 397~398 行还会回写更新它（仅当 `xref_base==0` 时，避免访问历史版本时污染）。这是一个典型的「缓存上次结果以加速重复查找」的小优化。

#### 4.3.4 代码实践

**实践目标**：亲手验证「同一个对象被解引用两次，第二次不会重复读盘」——这是缓存最直观的价值。

**方法 A（源码阅读型，无需编译）**：

1. 在 [source/pdf/pdf-xref.c:2547-2567](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L2547-L2567) 的 `pdf_cache_object` 里，确认快路径只有两行 `if (x->obj != NULL) return x;`。
2. 推理：第一次 `pdf_resolve_indirect(Catalog)` 时 `x->obj` 为 NULL，走 `'n'` 分支读盘解析，结果写进 `x->obj`；第二次 `pdf_resolve_indirect(Catalog)` 时 `x->obj` 已非空，**直接返回**，根本不会执行到 `fz_seek` / `pdf_parse_ind_obj`。
3. 结论：两次解引用只有第一次产生磁盘 I/O，第二次纯内存命中。这正是 `pdf_dict_get_inheritable` 沿 `/Parent` 链反复取值时性能仍然可控的原因。

**方法 B（编译型，可选）**：写一个最小程序，对同一引用调用两次 `pdf_resolve_indirect`，并在两次之间打印提示。骨架代码如下（**示例代码**，非项目原有文件）：

```c
/* 示例代码：演示两次解引用 Catalog，观察缓存 */
#include <mupdf/fitz.h>
#include <mupdf/pdf.h>

int main(int argc, char **argv)
{
    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_register_document_handlers(ctx);
    pdf_document *doc = pdf_open_document(ctx, argv[1]);     /* 假设 argv[1] 是 PDF */

    fz_try(ctx)
    {
        pdf_obj *root_ref = pdf_dict_get(ctx, pdf_trailer(ctx, doc), PDF_NAME(Root));
        /* 第一次：触发读盘 + 缓存 */
        pdf_obj *root1 = pdf_resolve_indirect(ctx, root_ref);
        fz_warn(ctx, "first resolve done, obj=%d 0 R", pdf_to_num(ctx, root_ref));
        /* 第二次：应命中 entry->obj，不再读盘 */
        pdf_obj *root2 = pdf_resolve_indirect(ctx, root_ref);
        fz_warn(ctx, "second resolve returned same ptr=%d", root1 == root2);
    }
    fz_catch(ctx)
        fz_report_error(ctx);

    fz_drop_document(ctx, doc);
    fz_drop_context(ctx);
    return 0;
}
```

**需要观察的现象**：方法 B 中 `root1 == root2` 应为真（两者是同一个 `entry->obj` 指针）。若想「看见」第二次没有读盘，可在 `pdf_cache_object` 的 `'n'` 分支（第 2575 行 `fz_seek` 前）临时加一行 `fz_warn(ctx, "DEBUG: read object %d from disk", num);`，重新编译后运行，应只看到 Catalog 编号被打印**一次**。

**预期结果**：第一次解引用打印一次「读盘」调试信息，第二次完全没有——从而用代码行为证明缓存生效。

> 注意：按 u1-l2、u2-l1，编译需链接 `libmupdf` 与 `libmupdf-third`，并保证头文件路径包含 `include/`。若环境不便编译，方法 A 已足以完成本实践（待本地验证方法 B 的运行现象）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MuPDF 在打开文档时**不**把所有对象一次性读进内存，而是做成懒加载？这种设计在什么场景下特别有价值？

> **答案**：因为一个 PDF 可能有成千上万对象，而单次操作（如渲染一页）通常只用到其中一小部分。一次性全读会带来巨大的内存与启动延迟，且大多数对象可能根本用不上。懒加载让「打开文档」几乎瞬时，内存占用随实际访问量增长。这在只渲染指定页、或仅提取文本/搜索关键词（mugrep）等场景下尤其有价值。

**练习 2**：访问对象流（`type=='o'`）里的某一个对象时，会发生什么「顺带」行为？这个行为带来什么好处和潜在代价？

> **答案**：`pdf_load_obj_stm` 会把**整个对象流解压**，并把流里**所有**对象都缓存进各自编号的 xref 条目（见第 2209~2248 行），而不只是取走目标对象。好处是「预读」——同流的其它对象后续访问直接命中缓存，无需再次解压；潜在代价是首次访问会一次性占用较多内存与 CPU（解压整个流），哪怕你只想要其中一个小对象。

**练习 3**：`pdf_cache_object` 的头文件注释（[include/mupdf/pdf/xref.h:97-104](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/xref.h#L97-L104)）说「调用后手中持有的 `pdf_xref_entry` 指针都应视为失效」。为什么？

> **答案**：因为它可能在解析失败时触发 `pdf_repair_xref`，或在查找时触发 `ensure_solid_xref`（把多段子段合并成一张扁平表），这些操作会**重新分配并搬移** `pdf_xref_entry` 数组。于是调用前你手里握着的旧指针可能指向已释放的旧内存。安全做法是只使用本次调用**返回**的那一个指针，其余的要在调用后重新获取。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从文件到对象」的完整追踪。任选一个真实 PDF（`a.pdf`），按顺序回答：

1. **定位入口**：`startxref` 指向哪里？（用 `mutool show a.pdf xref` 配合十六进制工具，或直接阅读 `pdf_read_start_xref` 理解它如何从文件尾部找到该偏移。）
2. **读表**：用 `mutool show a.pdf xref` 列出条目，找出 Catalog（即 `trailer` 里 `/Root` 指向的编号）。观察它的 `type` 是 `n` 还是 `o`，并据此判断它会走 4.3 的哪一条加载分支。
3. **解引用**：用 `mutool show a.pdf trailer/Root` 拿到 Catalog 字典，再 `mutool show a.pdf trailer/Root/Pages` 拿到 Pages 树根。体会每一步 `/Xxxx` 路径背后都隐含了一次 `pdf_resolve_indirect`。
4. **验证缓存**：基于 4.3.4 方法 A 的推理，说明「第 3 步里如果 Catalog 被多个路径反复引用，只有第一次真正读盘」。若条件允许，按方法 B 给 `pdf_cache_object` 加调试输出并编译运行，统计 Catalog 实际被读盘的次数。
5. **画出数据流**：用一张图把下列结构连起来——`pdf_document.xref_sections[]` → `pdf_xref.subsec` → `pdf_xref_entry`（含 `type/ofs/obj`）→ 经 `pdf_get_xref_entry` 查找 → `pdf_cache_object` 加载/命中 → `pdf_resolve_indirect` 返回 `entry->obj`。

完成本任务后，你应当能把「编号 → 偏移 → 读盘 → 缓存 → 解引用」这一整条链路讲给别人听。

## 6. 本讲小结

- xref 表是 PDF 的「目录」：把每个对象编号映射到磁盘位置（普通对象映射到文件偏移；对象流里的对象映射到「对象流编号 + 流内索引」）。它有老式文本表和新式二进制流两种磁盘形态，但读进内存后统一成 `pdf_xref_entry` 结构。
- 内存里 xref 是**分层叠加**的：`doc->xref_sections` 是一个版本数组，新版本（下标小）覆盖旧版本，支持增量更新；每段内部用若干 `pdf_xref_subsec` 连续区间紧凑存放。
- `pdf_resolve_indirect` 把间接引用「透明地」变成真实对象：它从引用壳取出 `doc` + `num`，调用 `pdf_cache_object` 拿到 `entry->obj` 返回；返回的是**借用引用**（不 keep、不可 drop），需持有则用 `pdf_load_object`。
- `pdf_resolve_indirect_chain` 用 `sanity=10` 计数防御「引用成环」，避免损坏 PDF 导致死循环。
- `pdf_cache_object` 是引擎，集懒加载、缓存、修复于一身：**`entry->obj != NULL` 即缓存命中、零磁盘 I/O**；`'n'` 类型 seek+解析+解密，`'o'` 类型解压整个对象流并批量缓存其中所有对象；解析编号错位时触发一次性 `pdf_repair_xref`。
- 缓存使「重复解引用不重复读盘」成立，是渲染性能的基础；但 `pdf_cache_object` 可能重整/修复 xref 而搬移条目内存，故调用后旧 `pdf_xref_entry` 指针一律视为失效。

## 7. 下一步学习建议

下一篇 **u7-l3「词法分析、解析与文件写入」** 会从「字节流」这一端补完本讲的另一半：`pdf_cache_object` 里调用的 `pdf_parse_ind_obj` 是怎么把字节流切成 token、再组装成 `pdf_obj` 的（`pdf-lex.c`、`pdf-parse.c`），以及 `pdf-write.c` 如何把整棵对象树连同 xref 重新序列化回合法 PDF。建议阅读顺序：

1. 先读 `source/pdf/pdf-lex.c` 的 `pdf_lex`，理解 token 的切分（与本讲 `pdf_read_old_xref` 里手动解析 xref 文本行形成对照）。
2. 再读 `source/pdf/pdf-parse.c` 的 `pdf_parse_ind_obj_or_newobj`（[pdf-parse.c:783](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L783)），看它如何吐出本讲反复用到的「对象 + 流偏移」。
3. 最后回到本讲的 `pdf_cache_object`，确认二者衔接无误，再进入 `pdf-write.c` 了解「写出 xref」的反向过程。
