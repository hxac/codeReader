# 源码与文件身份：Source、FileId 与包路径

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `Source` 里装了什么（源文本 + 行元数据 + 语法树）、它为什么是「不可变快照」，以及 `Source::new`、`edit`、`find`、`range` 各自的职责。
- 独立完成「行/列 ↔ 字节区间」的双向换算，并验证往返一致。
- 画出 `Span` 的 64 位内存布局（16 位 FileId + 48 位编号），并解释 Typst 为什么用「树节点编号」而不是字节区间来标记源码位置。
- 区分 `Span` 与 `DiagSpan`、`SpanKind` 与 `DiagSpanKind` 的四种形态，理解「编号」与「区间」两种定位方式各自的适用场景。
- 解释 `FileId`（驻留后的 `RootedPath` = `VirtualRoot` + `VirtualPath`）如何为多文件项目与包系统提供统一、廉价的文件身份，并掌握 `FileId::new`、`FileId::unique`、测试专用的 `FileId::new_for_test` 三者的区别。

## 2. 前置知识

本讲会用到几个基础概念，先用通俗语言过一遍：

- **字节偏移（byte index）**：把文件看成一个字节数组，从 0 开始数到某个位置。Rust 的字符串是 UTF-8 编码，一个汉字占 3 个字节，所以「第 5 个字节」和「第 5 个字符」不是一回事。
- **行与列（line / column）**：人类习惯的坐标。Typst 内部全部 **从 0 开始计数**，只在展示给用户时才加 1；而且「列」按 **字符数**（Unicode 标量值）计算，不是字节数，也不是 UTF-16 码元数。
- **UTF-16 码元**：JavaScript、LSP 等生态用「UTF-16 码元个数」表示列。`Lines` 也支持这套换算，供编辑器场景使用。
- **`Arc` 与写时复制**：`Arc<T>` 是线程安全的引用计数智能指针，克隆它只是复制一个指针。`Arc::make_mut` 在引用计数为 1 时原地修改，否则先深拷贝再改——这就是「写时复制」。
- **驻留（interning）**：把相同的值只存一份，之后用一个小整数（句柄）代表它。比较、复制、哈希句柄都极快，代价是全局驻留表会永久占用内存。
- **诊断（diagnostic）**：编译器输出的错误与警告，需要附带「发生在源码哪个位置」的信息，这个位置信息就是 `DiagSpan`。
- **记忆化（comemo）**：Typst 编译器大量缓存函数结果，缓存的键里包含 `Source`、`Span` 等类型，因此这些类型必须「克隆廉价、哈希稳定」——本讲的很多设计都服务于此（详见 u5-l8）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-syntax/src/source.rs` | `Source`：一份源码的完整快照（文本 + 行表 + 语法树），以及查找与定位 API |
| `crates/typst-syntax/src/lines.rs` | `Lines` / `Line`：UTF-8 字节、UTF-16 码元、行/列三者之间的换算加速结构 |
| `crates/typst-syntax/src/span.rs` | `Span`、`DiagSpan`、`SpanNumber`、`SubRange`：源码位置的紧凑表示 |
| `crates/typst-syntax/src/path.rs` | `FileId`、`RootedPath`、`VirtualRoot`、`VirtualPath`：虚拟文件系统与全局驻留的文件身份 |
| `crates/typst-syntax/src/package.rs` | `PackageSpec`、`PackageVersion`、`PackageManifest`：包的标识、版本与清单解析 |
| `crates/typst-library/src/lib.rs` | （辅助）`WorldExt::range`：外界最常用的「Span → 字节区间」入口 |

这四个 syntax 文件构成一条自底向上的链：**路径与身份（path.rs + package.rs）→ 位置表示（span.rs）→ 源码快照（source.rs）**。

## 4. 核心概念与源码讲解

### 4.1 Source：一份源码的完整快照

#### 4.1.1 概念说明

编译一份文档，编译器首先需要回答三个问题：**文件里是什么文本？文本的哪个位置对应哪一行哪一列？文本解析成了什么结构？** `Source` 把这三样东西打包成一个不可变的值：

- `id: FileId` —— 这份源码属于哪个文件（4.3 节详述）；
- `root: SyntaxNode` —— 解析出的语法树根节点；
- `lines: Lines<String>` —— 文本及其行元数据，用于各种坐标换算。

为什么做成不可变快照？因为 Typst 编译器是高度记忆化的：同一个 `Source` 会在编译管线里被反复传递、克隆、当作缓存键。把它包在 `Arc<LazyHash<…>>` 里，克隆只是复制一次指针，哈希结果会被惰性缓存，于是「处处传值」变得几乎零成本。编辑不是原地改字符串，而是通过 `edit` 触发一次 **增量重解析**（u2-l5 专讲）。

一个值得注意的 API 变化：旧版本曾提供 `Source::detached(...)` 辅助构造函数，它已被移除（提交 `98a5a7e35` 即 "Replace `Source::detached` with `FileId::new_for_test`"）；现在 `Source::new` 的文本参数也放宽为 `impl Into<String>`。当前仓库测试中的惯用法是 `FileId::new_for_test("名字")` 加 `Source::new`。

#### 4.1.2 核心流程

`Source` 的生命周期可以概括为：

1. **创建**：`Source::new(id, text)` → `parse(&text)` 得到语法树 → `root.numberize(id, Span::FULL)` 给每个节点分配 `Span` 编号 → `Lines::new(text)` 建立行表 → 三者装入 `Arc<LazyHash<…>>`。
2. **查询**：
   - `text()` / `lines()` / `root()` / `id()`：只读访问三样家底；
   - `find(span)`：给一个 `Span`，在语法树里反查 `LinkedNode`（带父链的节点视图）；
   - `range(num, sub_range)`：给一个 `SpanNumber`，反查它对应的字节区间，可选地再取「子区间」。
3. **编辑**：`edit(replace, with)` → 写时复制内部结构 → 更新行表 → 对被替换范围做增量重解析，返回最终重解析的范围；`replace(new)` 则先做一次朴素的前/后缀 diff，把整体替换压缩成最小的一次 `edit`。

行列换算（由 `lines.rs` 提供）的对应关系：

- `byte_to_line` / `byte_to_column` / `byte_to_line_column`：字节 → 行、列；
- `line_column_to_byte`：行、列 → 字节；
- `byte_to_utf16` / `utf16_to_byte`：字节 ↔ UTF-16 码元（编辑器协议用）；
- `line_to_byte` / `line_to_range`：行 → 该行起始字节 / 该行完整区间。

#### 4.1.3 源码精读

**Source 的存储结构**——一个字段即一份家底，整体包在 `Arc<LazyHash>` 中以获得廉价克隆与惰性哈希：

- [crates/typst-syntax/src/source.rs:20-29](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L20-L29) —— `Source(Arc<LazyHash<SourceInner>>)`，内部 `SourceInner { id, root, lines }`。文档注释明确说明：所有行、列索引从 0 开始，仅面向用户展示时才加 1；该类型「克隆与哈希都很廉价」。

**构造函数**——解析 + 编号 + 建行表，三步一步到位：

- [crates/typst-syntax/src/source.rs:34-41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L34-L41) —— `Source::new(id, text: impl Into<String>)`：先 `parse(&text)`，再 `root.numberize(id, Span::FULL)` 为整棵树分配 span 编号，最后 `Lines::new(text)` 建行表。`with_root`（L44-46）则接受外部已经建好的语法树，跳过解析。
- [crates/typst-syntax/src/source.rs:154-158](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L154-L158) —— 仓库自带测试展示了当前的标准用法：`FileId::new_for_test("test_source_sub_ranges")` 构造身份，再 `Source::new(test_id, text)` 解析。

**查询 API**——从 span 反查节点与字节区间：

- [crates/typst-syntax/src/source.rs:109-114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L109-L114) —— `find(span)`：先检查 `span.id()` 是否指向本文件，再用 `LinkedNode::new(self.root()).find(span)` 沿树下行查找。找不到返回 `None`。
- [crates/typst-syntax/src/source.rs:121-134](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L121-L134) —— `range(num, sub_range)`：先用 `find_number(num)` 找到节点得到整体区间；若给了 `sub_range`，则用 `to_absolute(overall.start)` 把「相对子区间」平移成绝对区间，并断言不超过整体区间。`SpanNumber` 的字段是 `pub(crate)`，外部代码通常通过 `span.get()` 解包获得它（见 4.2），或干脆用 `WorldExt::range`。
- [crates/typst-library/src/lib.rs:149-159](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L149-L159) —— `WorldExt::range`：外部使用者的首选入口。它按 `DiagSpanKind` 分派——编号形态转给 `Source::range`，区间形态直接返回自带区间，分离形态返回 `None`。

**编辑模型**——写时复制 + 增量重解析：

- [crates/typst-syntax/src/source.rs:96-104](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L96-L104) —— `edit`：`Arc::make_mut` 保证写时复制；先让行表 `lines.edit` 更新文本与行号，再调 `reparse` 只重建受影响的子树，返回最终重解析的区间。
- [crates/typst-syntax/src/source.rs:76-88](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L76-L88) —— `replace`：用「公共前缀 + 公共后缀」的朴素 diff 找出把旧文本变成新文本的最小单次编辑，转交给 `edit`。

**行表与坐标换算**——`Lines` 的核心是每行记录两个起始偏移：

- [crates/typst-syntax/src/lines.rs:23-28](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L23-L28) —— `Line { byte_idx, utf16_idx }`：行首的 UTF-8 字节偏移与 UTF-16 码元偏移。有了每行起点，任意换算都只需在「一行内部」做小规模计算。
- [crates/typst-syntax/src/lines.rs:67-74](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L67-L74) —— `byte_to_line`：对行数组按 `byte_idx` 二分查找，找到包含该字节的行。列的定义见 [crates/typst-syntax/src/lines.rs:80-85](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L80-L85)：**列 = 该字节之前、同一行之内的字符个数**（`head.chars().count()`），按字符而非字节计数。
- [crates/typst-syntax/src/lines.rs:134-146](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L134-L146) —— `line_column_to_byte`：反方向换算，取该行区间后逐字符跳过 `column_idx` 个字符，返回落点字节。行列越界返回 `None`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Source` + `Lines` 的「行列 ↔ 字节」往返换算，并观察「列按字符计数」与「字节偏移」在中文文本上的差异。

**操作步骤**（本实践只需新建文件，不改任何现有源码）：

1. 在 `crates/typst-syntax/tests/` 下新建集成测试文件 `u2l1-lines.rs`（Cargo 会自动发现 `tests/` 目录下的测试）：

   ```rust
   use typst_syntax::{FileId, Source};

   #[test]
   fn line_column_roundtrip() {
       // 1. 构造文件身份并解析多行文本
       let id = FileId::new_for_test("u2l1_lines");
       let text = "= Title\n你好世界\nThe end";
       let source = Source::new(id, text);

       // 2. 用户视角「第 2 行第 3 列」= 内部 0 基的 (line 1, column 2)
       let start = source.lines().line_column_to_byte(1, 2).unwrap();
       let end = source.lines().line_column_to_byte(1, 3).unwrap();
       println!("字节区间: {start}..{end}");
       println!("对应文本: {:?}", &text[start..end]);

       // 3. 反向换算：字节区间端点 → (行, 列)，验证往返一致
       assert_eq!(source.lines().byte_to_line_column(start), Some((1, 2)));
       assert_eq!(source.lines().byte_to_line_column(end), Some((1, 3)));
   }
   ```

2. 在仓库根目录运行：

   ```bash
   cargo test -p typst-syntax --test u2l1-lines -- --nocapture
   ```

3. 完成后删除该文件即可。

**需要观察的现象**：`println!` 打印的字节区间与切片文本；`--nocapture` 让输出可见；断言全部通过。

**预期结果**：第一行 `= Title\n` 共 8 字节，第二行从字节 8 开始；`你`、`好` 各占 3 字节，所以第 2 行第 3 列（`世`）的字节区间是 `14..17`，打印出的文本是 `"世"`；两个 `assert_eq!` 通过，证明 (1,2)、(1,3) 与 14、17 互为逆映射。若把文本换成纯英文，区间会从「跳 3 字节」变成「跳 1 字节」，直观体现「列按字符、偏移按字节」。

（若你的本地环境无法运行 cargo，本实践结果标注为**待本地验证**，但断言依据的是 [lines.rs:134-146](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L134-L146) 与 [lines.rs:80-94](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L80-L94) 的实现逻辑。）

#### 4.1.5 小练习与答案

**练习 1**：`Source` 为什么把 `SourceInner` 包在 `Arc<LazyHash<…>>` 里，而不是直接存三个字段？

**答案**：`Source` 在编译管线中被大量克隆并作为记忆化缓存的键。`Arc` 让克隆只复制一次指针；`LazyHash` 让哈希只算一次并缓存。若直接存字段，每次克隆都要深拷贝语法树和行表，每次作为缓存键都要重新哈希，增量编译（u5-l8）会退化到接近全量重算。

**练习 2**：`Source::edit` 里 `Arc::make_mut` 起什么作用？什么时候会发生深拷贝？

**答案**：`make_mut` 实现写时复制——若 `SourceInner` 的引用计数为 1（没有其他 `Source` 克隆共享它），就地修改；若被多处共享，先深拷贝一份再改，保证其他克隆看到的旧快照不受影响。诊断信息、IDE 中常长期持有旧 `Source`，这个语义保证了它们不会读到「被改了一半」的状态。

**练习 3**：不用 `Source::range`，如何从一个 `Span` 拿到它的字节区间？

**答案**：调用 `source.find(span)` 得到 `Option<LinkedNode>`，再对 `LinkedNode` 调 `.range()`。`find` 先校验 `span.id()` 与 `source.id()` 一致再沿树查找（[source.rs:109-114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L109-L114)）；这也是 `LinkedNode` 相比裸 `SyntaxNode` 的价值——它记着从根到当前节点的路径，能直接给出区间。

### 4.2 Span 与 DiagSpan：给代码位置发身份证

#### 4.2.1 概念说明

编译器各阶段产出的东西（值、元素、诊断……）都需要记住「我来自源码的哪个位置」：报错要画波浪线，IDE 要跳转，布局要追溯来源。这个「位置」就是 `Span`。

最直观的做法是存字节区间，但 Typst 没这么做，而是给语法树的**每个节点发一个唯一编号**，`Span` 存的就是 `FileId + 编号`。原因写在 `span.rs` 的文档注释里：编辑文档时字节区间会整体后移（在开头插一个字符，后面所有区间都失效），而编号在增量编辑后**基本保持稳定**——插入点之后的节点编号不变。由于 `Span` 会参与记忆化缓存的键，编号的稳定性直接保住了缓存命中率。

编号还满足两条有序性，使「按编号找节点」可以沿树快速下钻：

- 父节点的编号一定小于任何子节点的编号；
- 兄弟节点的编号从左到右递增。

两者合并即：对兄弟 [A, B, C]，A 及其全部子孙的编号都小于 B，C 及其全部子孙的编号都大于 B。

但并非所有「源码位置」都来自 Typst 源文件。数据文件（如 JSON）解析出错时也要报位置，而 JSON 没有语法树可编号——这时只能退回存字节区间。于是位置表示分裂为两种形态：**编号**（树 attached、编辑稳定）与**区间**（无树可用、直接了当）。`Span`（8 字节）与诊断专用的 `DiagSpan`（16 字节）用一套位布局同时容纳这两种形态，通过 `SpanKind` / `DiagSpanKind` 枚举解包使用。

#### 4.2.2 核心流程

`Span` 的 64 位被切成两段——高 16 位是 `FileId`，低 48 位是「编号区」：

\[ \text{Span} = (\text{FileId} \ll 48) \,\big|\, \text{number} \]

编号区 \( \text{number} \) 再按取值划分成四段：

| number 取值 | 形态 | 含义 |
| --- | --- | --- |
| `1`（FileId 为 0） | Detached | 不指向任何文件（编译器内部产生的东西） |
| \( 2 \) ..= \( 2^{47}-1 \) | Number | 源文件语法树节点编号（47 位） |
| \( 2^{47} \) ..= \( 2^{47}+2^{46}-1 \) | 外部区间起点 | 外部文件（非 Typst 源码）诊断区间，起点存这里，终点存在 `DiagSpan` 的 `extra` 里（46 位，超限饱和） |
| \( 2^{47}+2^{46} \) ..= \( 2^{48}-1 \) | Range | 内部区间形态：两个 23 位数打包成 `start << 23 \| end`（用于「把文本当 Typst 语法解析」但文件本身未必是源文件的场景） |

`DiagSpan` 在 `Span` 之外多加 8 位字节 `extra`，按形态复用：外部区间形态时存终点；编号/内部区间形态时可选地打包一个 `SubRange`（相对该 span 整体区间的更小子区间，如只高亮单词中的几个字符），打包方式为 \((\text{start} \ll 32) \,\big|\, \text{end}\)。

span 的产生与消费流程：

1. `Source::new` 解析后调用 `numberize`，按先序遍历给节点发号（保证上述有序性）；
2. 节点携带的 `Span` 随求值、排版一路传播，贴在 `Content`、诊断等产物上；
3. 消费时 `span.get()` 解包成 `SpanKind`（或 `diag_span.get()` → `DiagSpanKind`），再配合 `Source::range` / `WorldExt::range` 还原字节区间。

#### 4.2.3 源码精读

**Span 本体与四种形态**——文档注释是全文件最重要的阅读材料：

- [crates/typst-syntax/src/span.rs:62-63](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L62-L63) —— `Span(NonZeroU64)`：8 字节紧凑存储；因为内部值恒非零，`Option<Span>` 无需额外空间（空值优化）。
- [crates/typst-syntax/src/span.rs:25-61](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L25-L61) —— 文档注释列举四种形态，并解释「编号 span」的动机：字节区间在插入文本后会漂移，编号基本稳定，因而能作为记忆化函数的输入而不伤缓存性能；同时给出父子、兄弟编号的有序性保证。
- [crates/typst-syntax/src/span.rs:76-83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L76-L83) —— `SpanKind`：`Detached` / `Number { id, num }` / `Range { id, range }` 三个变体（第四种「外部区间」只有通过 `DiagSpan` 才能完整解包）。

**位布局常量**——用一张常量表精确切分 64 位：

- [crates/typst-syntax/src/span.rs:92-110](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L92-L110) —— `NUMBER_BITS = 48`、`FILE_ID_SHIFT = 48`、`EXTERNAL_BASE = 2^47`、`RANGE_BASE = 2^47 + 2^46`、`RANGE_VALUE_BITS = 23` 等，与上面的分段表一一对应；[crates/typst-syntax/src/span.rs:87](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L87) 的 `FULL = 2..(1 << 47)` 即编号 span 的合法区间（起点 2，保留 1 给 Detached）。

**打包与解包**——一组小而清晰的位运算：

- [crates/typst-syntax/src/span.rs:145-150](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L145-L150) —— `pack`：把 FileId 左移 48 位再或上低 48 位；FileId 恒非零所以整体非零。
- [crates/typst-syntax/src/span.rs:177-187](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L177-L187) —— `get()`：先取 id（为 0 即 Detached），再取 number；若 `number - RANGE_BASE` 无溢出则为内部区间形态（拆出两个 23 位数），否则是编号形态。
- [crates/typst-syntax/src/span.rs:160-167](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L160-L167) —— `id()`：右移 48 位还原 FileId；Detached 的高位全 0，自然落入 `None` 分支。
- [crates/typst-syntax/src/span.rs:128-133](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L128-L133) —— `from_range`：内部区间形态的构造入口，start/end 各自饱和于 \( 2^{23}-1 \) 后打包（这是一段 `pub(crate)` 代码，外部通过 `get()` 对称解包验证）。

**DiagSpan 与 SubRange**——诊断专用的 16 字节扩展：

- [crates/typst-syntax/src/span.rs:226-240](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L226-L240) —— `DiagSpan { span, extra }` 与 `DiagSpanKind`：`Number` 变体多出可选 `sub_range`；这就是「为什么要有两种形态」的落地——Typst 源码用编号、外部文件用区间，诊断系统统一收口在这里。
- [crates/typst-syntax/src/span.rs:253-260](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L253-L260) —— `DiagSpan::from_range`：外部文件诊断区间的构造入口，起点塞进 span 的外部段、终点存进 `extra`，两者饱和于 \( 2^{46}-1 \)。
- [crates/typst-syntax/src/span.rs:294-325](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L294-L325) —— `DiagSpan::get`：三路解包的总调度——外部段还原 `start..extra`；编号段尝试从 `extra` 拆出 `SubRange`；内部区间段同样可叠加 `SubRange`（先 `to_absolute` 平移到绝对坐标）。
- [crates/typst-syntax/src/span.rs:349-369](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L349-L369) —— `SubRange`：相对某 span 整体区间的非空子区间，`new` 要求 `start < end`，超 \( 2^{32}-1 \) 饱和；`to_absolute(offset)`（L380-385）负责平移成绝对区间，正是 `Source::range` 用到的那个方法。

**两个顺手的设计**：

- [crates/typst-syntax/src/span.rs:207-212](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L207-L212) —— `Debug` 输出的是解包后的 `SpanKind`（`DiagSpan` 同理输出 `DiagSpanKind`），调试打印直接可读，而不是一串裸数字。
- [crates/typst-syntax/src/span.rs:398-405](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L398-L405) —— `Spanned<T>`：把「值 + 它的 span」打包成 ubiquitous 的小结构，求值阶段到处传递；`LooselySpanned` 变体让相等比较忽略 span。本讲只需认识它，细节留到 u3。

#### 4.2.4 代码实践

**实践目标**：验证 span 的三种常见行为——Detached 判定、编号有序性、`Debug` 可读输出。

**操作步骤**：同样在 `crates/typst-syntax/tests/` 下新建 `u2l1-spans.rs`：

```rust
use typst_syntax::{FileId, LinkedNode, Source, Span, SpanKind};

#[test]
fn span_kinds_and_ordering() {
    // 1. 分离形态：不指向任何文件
    assert!(Span::detached().is_detached());
    assert_eq!(Span::detached().id(), None);

    // 2. 编号形态：解析一段多节点源码
    let id = FileId::new_for_test("u2l1_spans");
    let source = Source::new(id, "= Hi\n*bold*\n#f(1)");
    let root = LinkedNode::new(source.root());

    // 3. 兄弟节点的编号应从左到右严格递增。
    //    SpanNumber 内部值外部拿不到，但同一文件的 span
    //    高 16 位相同，比较 into_raw 的整体值等价于比较编号。
    let raws: Vec<u64> =
        root.children().map(|c| c.span().into_raw().get()).collect();
    assert!(raws.windows(2).all(|w| w[0] < w[1]), "兄弟编号未递增: {raws:?}");

    // 4. Debug 输出的是解包后的 SpanKind，直接可读
    let SpanKind::Number { id: root_id, .. } = root.span().get() else {
        panic!("根节点应是编号形态");
    };
    assert_eq!(root_id, id);
    println!("root span = {:?}", root.span());
}
```

运行 `cargo test -p typst-syntax --test u2l1-spans -- --nocapture`，完成后删除。

**需要观察的现象**：断言全部通过；`println!` 打印形如 `Span(Number { id: RootedPath(...), num: SpanNumber(2) })` 的可读输出。

**预期结果**：根节点的直接子节点（标题、空段、强调、函数调用等 markup 子节点）的原始 span 值严格递增——这正是 `numberize` 先序发号 + 兄弟有序的直接体现；`root.span().get()` 解包成功且 `id` 与构造时的 `FileId` 相等。（严格递增的依据是 [span.rs:52-61](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L52-L61) 声明的有序性保证；具体打印值**待本地验证**。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Option<Span>` 和 `Span` 一样都只占 8 字节？

**答案**：`Span` 内部是 `NonZeroU64`，Rust 的空值优化让 `None` 可以复用「全零」这个非法表示，不需要额外的判别位。这是选择 `NonZero` 类型做内部表示的直接红利（[span.rs:21-23](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L21-L23)、[span.rs:62-63](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L62-L63)）。

**练习 2**：用户在文档**开头**插入一个字符。若 span 存字节区间，会发生什么？存编号呢？

**答案**：存区间时，插入点之后所有节点的区间起点、终点都要加上偏移量，全部缓存键随之失效，记忆化收益归零。存编号时，`Source::edit` 的增量重解析只重建受影响的局部子树并只给新节点发号，未受影响节点的编号原样保留，绝大多数缓存键仍然有效。这就是文档注释所说的「编号在编辑后基本稳定」（[span.rs:42-50](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L42-L50)）。

**练习 3**：什么场景会用到「外部区间」形态（`DiagSpan::from_range`）而不是编号形态？

**答案**：诊断目标不在 Typst 源文件里、因而没有语法树可编号的时候——例如加载的 JSON/YAML 数据文件解析出错。没有树就没有 `numberize` 发的号，只能直接存字节区间；起点放进 `Span` 的外部段（46 位）、终点放进 `DiagSpan::extra`（[span.rs:37-40](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L37-L40)、[span.rs:253-260](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L253-L260)）。

### 4.3 FileId、VirtualPath 与 PackageSpec：文件的身份系统

#### 4.3.1 概念说明

一份文档可以 `#import` 同项目的其他文件，也可以 `#import "@preview/cetz:0.3.0"` 引用包。编译器需要在统一的命名空间里指称「哪个文件的哪条路径」，还要做到：跨平台（Windows 反斜杠、盘符）、可复现（不受运行机器的绝对路径影响）、以及比较/复制/哈希极快（`Span` 里只给 FileId 留了 16 位，它必须是个小整数）。

Typst 的方案分三层：

- **`VirtualPath`**：项目或包**内部**的路径，如 `/src/lib.typ`。它被规范化为「以 `/` 开头的绝对路径」，段永远非空、永不是 `.` 或 `..`、永不包含正反斜杠——这些不变量在构造时就强制保证，使路径无法在词法上逃出根目录。
- **`VirtualRoot`**：路径所处的虚拟文件系统根，只有两个变体——`Project`（当前项目，对应编译时的根目录）或 `Package(PackageSpec)`（某个包的根）。`RootedPath = VirtualRoot + VirtualPath` 即「某根下的某路径」，完整定位一个文件。
- **`FileId`**：把 `RootedPath` 放进**全局驻留表**后得到的小整数句柄（`NonZeroU16`）。此后所有地方只传 2 字节的 `FileId`，需要时通过 `Deref` 取回 `&'static RootedPath`。

`PackageSpec` 则是包的身份牌：`@命名空间/名字:版本`，如 `@preview/cetz:0.3.0`。它既是 `VirtualRoot::Package` 的载荷，也由 `package.rs` 中的 `PackageManifest`（包清单 `typst.toml` 的解析结果）负责校验一致性。

#### 4.3.2 核心流程

`VirtualPath::new("a/./b/../c.typ")` 的规范化流程：

1. `components()` 按 `/` 切分并分类：开头的空串是 `Root`，`.` 与连续分隔符是 `Current`，`..` 是 `Parent`，其余尝试构造 `Segment`；
2. `Segments::normalize` 逐个消费：`Root` 清空重置（绝对路径）、`Current` 无操作、`Parent` 弹出上一段——**若已经没有段可弹，返回 `PathError::Escapes`**（路径将逃出根）、普通段追加；
3. 结果始终是形如 `/a/c.typ` 的规范化字符串（`EcoString` 存储）。

`FileId` 的驻留流程（`FileId::new`）：

1. 取全局 `INTERNER` 写锁；
2. 在 `to_id` 哈希表里查这个 `RootedPath`，命中则**复用**已有 id；
3. 未命中则 `Box::leak` 把路径泄漏成 `&'static`，分配 `len + 1` 作为新的 `NonZeroU16`，双向登记（`to_id` 插入、`from_id` 追加）；
4. 容量上限 \( 2^{16}-1 \)，超出直接 panic（"out of file ids"）。

三个构造函数的语义差异：

| 构造函数 | 是否查重复用 | 用途 |
| --- | --- | --- |
| `FileId::new(path)` | 是——同一路径永远同一个 id | 正常编译中标识真实文件 |
| `FileId::unique(path)` | 否——每次都新建，且不登记进 `to_id` | 「假」文件，如从 stdin 读入的内容：即使路径相同也不与 `new` 的结果撞号 |
| `FileId::new_for_test(name)` | 走 `new`（复用） | 测试专用：以 `name` 为路径造一个 Project 根的 id，替代已移除的 `Source::detached` |

`unique` 的路径仍然重要：它决定该文件内部的相对路径如何解析（stdin 内容里 `#import "x.typ"` 相对谁），只是「不参与去重」。

`PackageSpec` 的解析流程：`"@preview/cetz:0.3.0"` → `parse_namespace`（吃掉 `@`、读到 `/`、校验是合法标识符）→ `parse_name`（读到 `:`、同样校验）→ `parse_version`（严格三段 `major.minor.patch`，多一段报错）。`Display` 与之互逆，输出 `@{namespace}/{name}:{version}`。

#### 4.3.3 源码精读

**三层身份结构**：

- [crates/typst-syntax/src/path.rs:20-24](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L20-L24) —— `RootedPath { root: VirtualRoot, vpath: VirtualPath }`：「某虚拟根下的某路径」，`intern()` 方法（L33-35）把它变成 `FileId`。
- [crates/typst-syntax/src/path.rs:74-82](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L74-L82) —— `VirtualRoot`：`Project`（项目的规范根，即 `TYPST_ROOT` 界定的范围）或 `Package(PackageSpec)`。这个枚举就是「多文件项目 + 包系统」在类型层面的分界线。
- [crates/typst-syntax/src/path.rs:98-99](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L98-L99) —— `FileId(NonZeroU16)`：2 字节句柄，`Copy/Clone/Eq/Hash` 全部是整数级开销——这正是 `Span` 敢用 16 位存它的前提。

**全局驻留表**：

- [crates/typst-syntax/src/path.rs:85-93](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L85-L93) —— `INTERNER`：进程级 `RwLock<Interner>`，`to_id` 双向映射的哈希表方向，`from_id` 是「编号 → `&'static RootedPath`」的向量。
- [crates/typst-syntax/src/path.rs:105-129](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L105-L129) —— `FileId::new`：驻留全流程。注释直白承认内存策略——`Box::leak` 故意泄漏，但最多泄漏 \( 2^{16} \) 个路径「所以不是大事」；编号用 `u16::try_from(len + 1)` 分配，溢出即 panic。
- [crates/typst-syntax/src/path.rs:159-169](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L159-L169) —— `FileId::unique`：跳过 `to_id` 查找与登记，永远新建。文档注释强调：返回的 id 是该文件**唯一**的标识，即便之后用相同路径调 `new` 也不会复用它；典型用途是 stdin 这类「无路径可寻」的虚拟文件。
- [crates/typst-syntax/src/path.rs:174-177](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L174-L177) —— `FileId::new_for_test(name)`：把 `name` 当路径、`Project` 当根，走正常驻留。注释明确说明「正常代码请用 `unique`」——它是测试的便利品，也是 `Source::detached` 移除后的替代品（git 提交 `98a5a7e35`）。
- [crates/typst-syntax/src/path.rs:194-205](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L194-L205) —— `get()` 按 `from_id` 取回静态路径，`Deref` 让 `file_id.vpath()` 这类调用直接写——句柄用起来就像路径本身。

**VirtualPath 的规范化与不变量**：

- [crates/typst-syntax/src/path.rs:454-470](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L454-L470) —— `components()`：切分与分类；注意反斜杠段会在这里变成 `PathError::Backslash`（跨平台兼容性 hazards）。
- [crates/typst-syntax/src/path.rs:480-493](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L480-L493) —— `Segment::new`：段级不变量——拒绝空串、`.`、`..`、以及含 `/` 或 `\` 的内容，注释点明目的是「防止 realize 后路径逃出根」。
- [crates/typst-syntax/src/path.rs:611-628](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L611-L628) —— `push_component`：`Root` 重置、`Current` 忽略、`Parent` 弹段，**弹无可弹即 `PathError::Escapes`**——`/` 之上没有 `..`，路径在词法层面被锁死在根内。
- [crates/typst-syntax/src/path.rs:661-672](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L661-L672) —— `PathError::{Escapes, Backslash}`：两类构造期错误；注意文档提醒「符号链接仍可能逃逸」，词法安全不等于物理安全。

**虚拟与真实文件系统的桥**：

- [crates/typst-syntax/src/path.rs:232-251](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L232-L251) —— `virtualize(root_path, path)`：真实路径 → 虚拟路径的唯一入口，`strip_prefix` 不在根内即报 `Escapes`，全程不做任何文件系统操作——编译结果因此不依赖机器上的绝对路径（可复现性）。
- [crates/typst-syntax/src/path.rs:267-273](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L267-L273) —— `realize(root)`：反方向，虚拟路径拼上真实根得到 `PathBuf`；`World::source` / `World::file` 的实现者（如 typst-cli 的 `SystemWorld`）靠这对函数翻译两个世界。

**包的标识与清单**：

- [crates/typst-syntax/src/package.rs:225-233](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L225-L233) —— `PackageSpec { namespace, name, version }`：包的三要素。
- [crates/typst-syntax/src/package.rs:244-254](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L244-L254) —— `FromStr`：`@ns/name:version` 的三段解析入口，具体拆解在 [package.rs:314-351](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L314-L351)（命名空间与名字都必须是合法标识符）。
- [crates/typst-syntax/src/package.rs:262-266](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L262-L266) —— `Display`：输出 `@{namespace}/{name}:{version}`，与 `FromStr` 互逆；`Debug` 直接复用它，日志里看到的就是这个格式。
- [crates/typst-syntax/src/package.rs:354-362](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L354-L362) —— `PackageVersion { major, minor, patch }`：严格三段语义版本；[package.rs:432-455](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L432-L455) 的 `FromStr` 遇到第四段会报错。
- [crates/typst-syntax/src/package.rs:21-34](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L21-L34) —— `PackageManifest`：`typst.toml` 的解析模型，`package.entrypoint` 字段（[package.rs:105-106](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L105-L106)）就是一个 `VirtualPath`——身份系统在这里把「包」与「路径」缝合起来。
- [crates/typst-syntax/src/package.rs:157-183](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L157-L183) —— `PackageManifest::validate`：下载/加载包后校验清单名字、版本与请求的 `PackageSpec` 一致，并检查 `compiler` 字段声明的最低版本要求。

#### 4.3.4 代码实践

**实践目标**：亲手验证路径规范化、越界/反斜杠拒绝、驻留复用与 `unique` 语义、`PackageSpec` 往返。

**操作步骤**：新建 `crates/typst-syntax/tests/u2l1-paths.rs`：

```rust
use std::str::FromStr;

use typst_syntax::package::PackageSpec;
use typst_syntax::{FileId, PathError, RootedPath, VirtualPath, VirtualRoot};

#[test]
fn virtual_paths_and_ids() {
    // 1. 规范化：`.` 与 `..` 被消化，结果总是带前导斜杠的绝对路径
    assert_eq!(
        VirtualPath::new("a/./b/../c.typ").unwrap().get_with_slash(),
        "/a/c.typ"
    );

    // 2. 两类构造错误
    assert_eq!(VirtualPath::new("../x.typ"), Err(PathError::Escapes));
    assert_eq!(VirtualPath::new("a\\b.typ"), Err(PathError::Backslash));

    // 3. 驻留复用：同一路径两次 new 得到同一个 id
    let make = || {
        FileId::new(RootedPath::new(
            VirtualRoot::Project,
            VirtualPath::new("main.typ").unwrap(),
        ))
    };
    assert_eq!(make(), make());

    // 4. unique 不复用：同一路径两次 unique 得到不同 id
    let unique = || {
        FileId::unique(RootedPath::new(
            VirtualRoot::Project,
            VirtualPath::new("fake.typ").unwrap(),
        ))
    };
    assert_ne!(unique(), unique());

    // 5. 包标识解析与 Display 互逆
    let spec = PackageSpec::from_str("@preview/cetz:0.3.0").unwrap();
    assert_eq!(spec.namespace.as_str(), "preview");
    assert_eq!(spec.to_string(), "@preview/cetz:0.3.0");
    assert!(PackageSpec::from_str("preview/cetz:0.3.0").is_err()); // 缺 '@'
}
```

运行 `cargo test -p typst-syntax --test u2l1-paths`，完成后删除。

**需要观察的现象**：五个断言组全部通过；可自行追加 `println!("{:?}", make())` 观察 `FileId` 的 Debug 输出（即其 `RootedPath`）。

**预期结果**：第 3 组相等、第 4 组不等是最核心的观察点——同样路径「`new` 去重、`unique` 不去重」。断言依据分别是 [path.rs:113-115](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L113-L115)（命中即复用）与 [path.rs:159-169](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L159-L169)（无条件新建）；`PackageSpec` 断言依据 [package.rs:314-320](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/package.rs#L314-L320)（必须以 `@` 开头）。（**待本地验证**。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FileId` 要做成驻留句柄，而不是直接在 `Span` 里存路径字符串或 `RootedPath`？

**答案**：三个理由。其一，`Span` 只给文件身份留了 16 位（[span.rs:92-104](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L92-L104)），`RootedPath` 塞不进去，小整数正好。其二，`Span` 要当缓存键、被海量复制比较，字符串比较是 O(长度)，2 字节整数比较是一条指令。其三，驻留后 `Deref` 仍能取回 `&'static RootedPath`（[path.rs:199-205](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L199-L205)），信息一点不丢。

**练习 2**：`VirtualPath::new("hello/../../world.typ")` 返回什么？为什么这个设计对安全重要？

**答案**：返回 `Err(PathError::Escapes)`。规范化过程中两个 `..` 依次弹段，第一个弹掉 `hello`，第二个发现已到根 `/` 无段可弹，于是报错（[path.rs:617-624](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L617-L624)）。它保证任何虚拟路径被 `realize` 成真实路径时**在词法上**不可能越出项目/包根——否则一个恶意包就能用 `#import "../../secrets.typ"` 读到根外的文件。（符号链接造成的物理逃逸不在此防线内，见 [path.rs:663-667](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L663-L667) 的提醒。）

**练习 3**：`FileId::unique` 的文档说「返回的 id 是该文件唯一的标识」。既然不重复使用，为什么还要传入路径？

**答案**：因为路径不止用于身份去重，还决定**该文件内部的相对导入如何解析**。stdin 读入的内容里若出现 `#import "util.typ"`，要相对某个虚拟路径去解析；`unique` 传入的路径正是这个解析基准，只是它不进 `to_id` 表、不参与复用（[path.rs:139-143](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L139-L143)）。

## 5. 综合实践

**任务**：把本讲三个模块串成一条完整链路——「文本 → 语法树 → span → 字节区间 → 子区间」，即完整复刻并扩展仓库自带的 `test_source_sub_ranges` 测试。

新建 `crates/typst-syntax/tests/u2l1-full-circle.rs`：

```rust
use typst_syntax::{FileId, LinkedNode, Side, Source, SpanKind, SubRange};

#[test]
fn span_full_circle() {
    // 模块 3：FileId 身份
    let id = FileId::new_for_test("u2l1_full_circle");
    // 模块 1：Source 快照（解析 + 编号 + 行表一步完成）
    let text = "= head <label>";
    let source = Source::new(id, text);

    // 模块 2：从树上取一个叶子节点的 span
    let leaf = LinkedNode::new(source.root())
        .leaf_at(2, Side::After)
        .unwrap();
    let span = leaf.span();

    // span → 节点（find 反查）
    assert_eq!(source.find(span).unwrap().range(), leaf.range());

    // span → SpanNumber → 字节区间（整体 + 子区间）
    let SpanKind::Number { num, .. } = span.get() else {
        panic!("应为编号形态");
    };
    assert_eq!(&text[source.range(num, None).unwrap()], "head");
    assert_eq!(
        &text[source.range(num, Some(SubRange::new(1, 3).unwrap())).unwrap()],
        "ea"
    );
}
```

运行 `cargo test -p typst-syntax --test u2l1-full-circle`。完成后删除全部三个实践文件。

**检查点**：

1. `find` 的反查区间应与树上直接读到的 `leaf.range()` 完全一致；
2. `range(num, None)` 应取出 `"head"`（= 后面的标题文字），`range(num, Some(1..3 的子区间))` 应取出 `"ea"`——子区间坐标是**相对 span 自身**的；
3. 对照仓库原生测试 [source.rs:154-174](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L154-L174)，确认你的写法与官方一致。

**预期结果**：三个断言全部通过。`leaf_at(2, Side::After)` 取的是标题里第 2 个字节后的叶子，即 `head` 这个词——与官方测试同款调用（**待本地验证**）。

## 6. 本讲小结

- `Source` 是「文本 + 行表 + 语法树」的不可变快照，包在 `Arc<LazyHash>` 里让克隆与哈希近乎零成本，服务于记忆化编译；`edit` 走写时复制 + 增量重解析。
- 行/列换算由 `Lines` 承担：每行记录字节与 UTF-16 双起点，「列」按字符计数；行列与字节可双向换算且往返一致。
- `Span` 是 8 字节的紧凑位置身份证：高 16 位 FileId + 低 48 位编号区；编号区再切成「源文件节点编号 / 外部区间起点 / 内部打包区间」三段，`get()` 解包成 `SpanKind`。
- 用编号而非字节区间是为了**编辑稳定性**——插入文本不再使后续节点缓存键失效；编号的父子/兄弟有序性又让反查可以沿树快速下钻。
- `DiagSpan`（16 字节）是诊断专用扩展：用 `extra` 字段补上外部区间的终点或编号 span 的 `SubRange`，一套布局同时服务「有树的源码」和「无树的数据文件」。
- `FileId` 是驻留后的 `RootedPath`（`VirtualRoot` = Project 或 Package + 规范化的 `VirtualPath`）：2 字节句柄、全局复用；`unique` 造「假文件」、`new_for_test` 是测试专用（`Source::detached` 已移除）。
- `VirtualPath` 的构造期不变量（段非空、无 `.`/`..`、无反斜杠）+ `Parent` 弹无可弹即报错，共同把路径在词法层面锁死在根内，是多文件与包系统安全的地基。

## 7. 下一步学习建议

本讲解决的是「文本与身份」，但刻意绕开了语法树本身的构造。下一讲 **u2-l2「词法与类别：Lexer 与 SyntaxKind」** 进入 `Source::new` 里那句 `parse(&text)` 的内部：文本如何被切成一个个 token（`kind.rs` 的 `SyntaxKind` 分类体系与 `lexer.rs` 的手写状态机）。建议顺序：

1. 先读 `crates/typst-syntax/src/kind.rs`，只看枚举分组，感受「标记 / 字面量 / 关键字 / 错误」的分类；
2. 再带着本讲的问题读 `parser.rs` 的入口：`numberize` 是在解析完成后才给树发号的——发号逻辑在 `node.rs`，将在 u2-l3 展开；
3. 想提前验证理解，可以回到本讲综合实践的代码，把 `text` 换成含中文字符与 emoji 的文本，观察 `Lines` 对多字节字符的处理是否仍保持往返一致。
