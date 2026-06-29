# 枚举映射：LaTeX/BibTeX 结构与 LSP 符号种类

## 1. 本讲目标

本讲紧接 u4-l1（自定义 LSP 消息），继续阅读 `LSP-Internals.md`，但方向相反：u4-l1 讲 texlab 如何**扩展** LSP（新增自定义请求），本讲讲 texlab 如何**复用** LSP 的标准枚举，把自己的 LaTeX/BibTeX 结构「翻译」给编辑器。

学完后你应当能够：

1. 说清 LSP 里 `CompletionItemKind` 与 `SymbolKind` 两个枚举各自管什么、出现在哪两个请求里。
2. 会查 texlab 的映射表，对任意一种 LaTeX/BibTeX 结构（命令、环境、节、方程、标签、BibTeX 条目等）说出它在补全列表和符号面板里分别以哪种图标/种类呈现。
3. 解释为什么同一行里两个枚举的**数值经常不同**（例如 Command 在补全列是 `Function (3)`，在符号列却是 `Function (12)`）。
4. 解释为什么六类 BibTeX 条目（Article/Book/Thesis 等）被特意映射到**互不相同**的枚举值，这背后的设计意图是什么。

---

## 2. 前置知识

### 2.1 回顾：LSP 客户端-服务器模型

在 u4-l1 里我们已经建立：texlab 是一个 LSP 服务器，编辑器是 LSP 客户端，两者用 JSON-RPC 交换标准化的请求与响应。texlab 给编辑器的所有「智能」能力（补全、跳转、符号面板、诊断……）都是通过一条条标准 LSP 请求实现的。

### 2.2 什么是「枚举（enum）」和「kind」

LSP 规范里有很多结构体，其中不少带一个 `kind` 字段，值是一个**整数**。这个整数来自一个预先定义好的**枚举（enum）**：每个整数对应一个有名字的种类，例如 `Function`、`Class`、`File`。

`kind` 的唯一作用是告诉客户端「这一项是什么类型的东西」，好让客户端为它渲染**合适的图标和分类**。比如同样是一行补全候选，标记成 `Function` 的会画一个 `ƒ` 图标，标记成 `File` 的会画一个文件图标。texlab 本身不画图标——画图标是编辑器的事；texlab 只负责把 `kind` 数值填对。

### 2.3 本讲涉及的两个枚举与两个请求

| 枚举 | 出现在哪个 LSP 数据类型 | 由哪个请求触发 | 决定了编辑器里什么 |
| --- | --- | --- | --- |
| `CompletionItemKind` | `CompletionItem.kind` | `textDocument/completion`（补全） | 补全弹出列表里每一项的图标 |
| `SymbolKind` | `SymbolInformation.kind` / `DocumentSymbol.kind` | `textDocument/documentSymbol`（文档符号） | 大纲/符号面板、面包屑里每一项的图标 |

关键点：**这是两个互相独立的枚举**，由两个互相独立的请求产生。所以 texlab 维护了两列映射。

> 名词解释：
> - **补全（completion）**：你敲到一半时弹出的候选列表。
> - **文档符号（document symbol）**：编辑器侧边栏的「大纲/Outline」面板，列出文档里的章节、公式、标签等结构。
> - **面包屑（breadcrumbs）**：编辑器顶部显示当前光标所在结构层级的小条。

---

## 3. 本讲源码地图

本讲只读一个文件，但只读它的下半部分。

| 文件 | 作用 |
| --- | --- |
| `LSP-Internals.md` | 上半部分「Custom Messages」（u4-l1 已讲）讲自定义请求；下半部分「Enum Mapping」就是本讲的全部内容——一张把 LaTeX/BibTeX 结构映射到两个 LSP 枚举的表。 |

本讲不涉及 `Configuration.md`，但会和 u5-l2（符号/补全/悬停配置）呼应：本讲讲「这些结构**以什么图标**出现」，u5-l2 讲「哪些结构**允许/禁止**出现、补全如何匹配」。

---

## 4. 核心概念与源码讲解

### 4.1 CompletionItemKind 枚举

#### 4.1.1 概念说明

`CompletionItemKind` 是 LSP 规范为补全候选定义的枚举，共 25 个值（1=`Text` 到 25=`TypeParameter`）。当 texlab 回答 `textDocument/completion` 请求时，它返回一个 `CompletionItem` 数组，每个 `CompletionItem` 带一个 `kind` 整数。编辑器拿到这个整数，映射成图标画在补全列表里。

它解决的问题是：LaTeX 里能补全的东西五花八门——命令（`\section`）、环境名（`equation`）、标签（`sec:intro`）、文献条目键（`knuth1984`）、颜色名……编辑器需要一种统一的方式知道「这条候选是什么」，才能画对图标、做对排序。`CompletionItemKind` 就是这个统一的「打标签」机制。

#### 4.1.2 核心流程

补全候选从 texlab 流向编辑器的过程：

1. 用户在编辑器里敲键（例如输入 `\beg` 或 `\cite{`），编辑器发起 `textDocument/completion` 请求。
2. texlab 收集候选的 LaTeX/BibTeX 结构。
3. 对每个候选，texlab 按映射表查到对应的 `CompletionItemKind` 数值，填进 `CompletionItem.kind`。
4. 编辑器收到列表，把每个 `kind` 数值映射成图标，渲染补全弹窗。

伪代码：

```
// texlab 内部（示意）
for candidate in collect_candidates(document, position):
    kind = COMPLETION_KIND_TABLE[type_of(candidate)]   # 例如 Label -> 4
    items.append(CompletionItem{ label: candidate.name, kind: kind })
return items
```

#### 4.1.3 源码精读

映射表的开头说明了它的用途——把 LaTeX/BibTeX 结构翻译成两个 LSP 枚举：

[LSP-Internals.md:111-114](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L111-L114) —— 这是「Enum Mapping」小节的标题与一句话说明：下表描述 LaTeX/BibTeX 结构到 `CompletionItemKind` 和 `SymbolKind` 的映射。

表的第二列就是 `CompletionItemKind`。挑几行有代表性的看：

[LSP-Internals.md:118](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L118) —— `Command` → `Function (3)`。LaTeX 命令（如 `\section`、`\textbf`）像函数：带参数、有「调用」语义，所以补全时画函数图标。

[LSP-Internals.md:128](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L128) —— `Label` → `Constructor (4)`。你在 `\ref{` 里补全标签时，每个标签候选用 `Constructor` 图标。

[LSP-Internals.md:121](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L121) —— `Snippet` → `Snippet (15)`，且**只有** `CompletionItemKind` 列有值。snippet 是补全时展开的代码模板，自然会出现在补全列表里。

注意几个「只有补全列、没有符号列」的行：`Snippet`、`Color`、`Color Model`（[L121](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L121)、[L133](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L133)、[L134](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L134)）。它们只会在补全里出现，不会进符号面板——这是两个枚举独立的直接体现。

#### 4.1.4 代码实践

**目标**：亲手验证补全列表的图标确实由 `CompletionItemKind` 决定。

**步骤**：

1. 在支持 texlab 的编辑器（如 VS Code + LaTeX Workshop、Neovim）里打开任意 `.tex` 文件。
2. 输入 `\beg`，在弹出的补全列表里找到 `equation` 环境。
3. 输入 `\cite{`，观察弹出的文献键候选。

**需要观察的现象**：

- `equation` 这类环境名候选，图标应对应 `Enum (13)`（见 [L122](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L122)）。
- `\section` 这类命令候选，图标应对应 `Function (3)`（见 [L118](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L118)）。

**预期结果**：补全项的图标与映射表第二列一致。具体图标样式因编辑器而异（不同主题画法不同），但**种类语义**一致。若你的编辑器不显示图标，属客户端渲染问题，不影响 texlab 已正确返回 `kind`。

**注意**：图标的具体外观「待本地验证」（取决于编辑器主题），但 `kind` 数值是 texlab 按 wiki 表固定的。

#### 4.1.5 小练习与答案

**练习 1**：用户输入 `\cite{kn`，补全候选里出现一条 BibTeX 文章条目（`@article`）。根据映射表，它的 `CompletionItemKind` 是哪个？数值是多少？

**答案**：`Event (23)`，见 [LSP-Internals.md:138](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L138)。

**练习 2**：为什么 `Snippet` 行只有 `CompletionItemKind`、没有 `SymbolKind`？

**答案**：snippet 是补全时展开的模板，只在补全列表里有意义；它不是文档里可导航的结构，不会出现在符号面板，所以不需要 `SymbolKind`。

---

### 4.2 SymbolKind 枚举

#### 4.2.1 概念说明

`SymbolKind` 是 LSP 规范为**文档符号**定义的另一个枚举，共 26 个值（1=`File` 到 26=`TypeParameter`）。当 texlab 回答 `textDocument/documentSymbol` 请求时，它返回文档里的结构元素（章节、公式、标签、定理……），每个带一个 `kind` 整数，编辑器据此在大纲面板里画图标。

它和 `CompletionItemKind` 解决的问题类似（「这是什么类型的东西」），但服务的场景不同：一个服务补全弹窗，一个服务大纲面板。**两者是规范里两个独立定义的枚举，数值互不相关**——这是本讲最容易踩的坑，下一节细讲。

#### 4.2.2 核心流程

文档符号从 texlab 流向编辑器的过程：

1. 用户打开 `.tex` 文件（或编辑器主动请求大纲），编辑器发起 `textDocument/documentSymbol` 请求。
2. texlab 解析文档，提取结构元素（section、equation、label、theorem 等）。
3. 对每个元素，texlab 按映射表查到对应的 `SymbolKind` 数值，填进 `SymbolInformation.kind`（或 `DocumentSymbol.kind`）。
4. 编辑器把每个 `kind` 映射成图标，渲染大纲面板与面包屑。

伪代码：

```
// texlab 内部（示意）
for element in parse_document(document):
    kind = SYMBOL_KIND_TABLE[type_of(element)]   # 例如 Equation -> 14
    symbols.append(SymbolInformation{ name: element.name, kind: kind, location: ... })
return symbols
```

#### 4.2.3 源码精读

表的第三列就是 `SymbolKind`。看几行：

[LSP-Internals.md:123](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L123) —— `Section` → `Module (2)`。`\section`/`\subsection` 是文档的结构容器，像模块/命名空间，所以大纲里用 `Module` 图标，并形成可折叠的层级。

[LSP-Internals.md:126](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L126) —— `Equation` → `Constant (14)`。带编号的公式是稳定、可引用的对象，像常量。

[LSP-Internals.md:120](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L120) —— `Command Definition` → `Key (20)`，且**只有** `SymbolKind` 列有值。`\newcommand`/`\DeclarePairedDelimiter` 这类定义是文档里的可导航地标（你想跳到某命令的定义处），所以出现在符号面板；但「定义本身」不是补全候选（被定义的命令会以 `Command → Function` 出现在补全里，那是另一行）。

**关键提醒：同名枚举值，数值不同。** 对照 [L118](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L118)：Command 在补全列是 `Function (3)`，在符号列是 `Function (12)`。都叫 `Function`，但一个是 `CompletionItemKind.Function=3`，一个是 `SymbolKind.Function=12`。表中特意把数值写在名字后面，就是为了避免读者把两列的数字混用。如果你在写 texlab 客户端、需要按数值判断类型，**必须区分这一数值来自哪个枚举**。

#### 4.2.4 代码实践

**目标**：验证大纲面板的图标由 `SymbolKind` 决定，且与补全列数值不同。

**步骤**：

1. 准备一份含多级标题与公式的 `.tex`（示例代码见下方 4.3.4）。
2. 在编辑器里打开该文件，唤出大纲/Outline 面板。
3. 对照映射表第三列，逐项核对图标。

**需要观察的现象**：

- `\section{...}` 在大纲里以 `Module (2)` 呈现，并形成可折叠树。
- `\label{...}` 以 `Constructor (9)` 呈现。
- `\begin{equation}` 块以 `Constant (14)` 呈现。

**预期结果**：大纲图标种类与映射表第三列一致。哪些元素会被纳入大纲、是否递归，受 `texlab.symbols.*` 配置影响（详见 u5-l2）；本讲只确认**已纳入的元素**其 `kind` 符合表格。

**注意**：texlab 具体把哪些结构上报进大纲，部分行为「待本地验证」（与配置有关）；但只要某结构出现，其 `SymbolKind` 一定符合 [L116-144](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L116-L144) 的表。

#### 4.2.5 小练习与答案

**练习 1**：`\newcommand{\foo}{...}` 会在符号面板里以哪种 `SymbolKind` 呈现？数值是多少？

**答案**：`Command Definition` → `Key (20)`，见 [LSP-Internals.md:120](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L120)。

**练习 2**：同样叫 `Function`，为什么 Command 在 `CompletionItemKind` 列是 3、在 `SymbolKind` 列却是 12？

**答案**：因为 `CompletionItemKind` 和 `SymbolKind` 是 LSP 规范里**两个独立定义的枚举**，各自的数值表不同；名字都叫 `Function` 只是规范命名上的巧合，两列数值没有换算关系。

---

### 4.3 结构→种类映射表

#### 4.3.1 概念说明

前两节分别看了两列。本节把整张表作为一个整体来读，回答三个问题：

1. 为什么需要这张表？——因为 LaTeX/BibTeX 的结构（定理、方程、文献条目……）在通用编程语言里**没有对应物**，LSP 枚举里没有 `Theorem`、`Equation`、`BibEntry`。texlab 必须为每种结构**挑一个最贴近的** LSP 枚举值。
2. 表有哪些「形态」？——有些结构两列都有、有些只有补全列、有些只有符号列。
3. BibTeX 条目为什么按类型拆成六种不同枚举？——这是本表最有设计感的一处。

#### 4.3.2 核心流程：如何读这张表

读表的三步法：

1. **定位行**：先在第一列找到你关心的 LaTeX/BibTeX 结构。
2. **查两列**：第二列是它在**补全列表**里的图标，第三列是它在**符号面板**里的图标。
3. **认空格**：某列为空，表示该结构不会出现在那个场景。空格不是「未定义」，而是「明确不出现」。

整张表按这三种形态分组：

| 形态 | 含义 | 例子 |
| --- | --- | --- |
| 两列都有 | 既会被补全、也会进符号面板 | Command、Section、Equation、Label |
| 只有补全列 | 只在补全弹窗出现，不进大纲 | Snippet、Color、Color Model |
| 只有符号列 | 只进大纲，不作为补全候选 | Command Definition |

#### 4.3.3 源码精读：整表与设计意图

完整映射表如下（直接引自 wiki，[LSP-Internals.md:116-144](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L116-L144)）：

| LaTeX / BibTeX 结构 | CompletionItemKind | SymbolKind |
| --- | --- | --- |
| Command | `Function` (3) | `Function` (12) |
| Command Argument | `Value` (12) | `Number` (16) |
| Command Definition | — | `Key` (20) |
| Snippet | `Snippet` (15) | — |
| Environment | `Enum` (13) | `Enum` (10) |
| Section | `Module` (9) | `Module` (2) |
| Float | `Method` (2) | `Method` (6) |
| Theorem | `Variable` (6) | `Variable` (13) |
| Equation | `Constant` (21) | `Constant` (14) |
| Enumeration Item | `EnumMember` (20) | `EnumMember` (22) |
| Label | `Constructor` (4) | `Constructor` (9) |
| Folder | `Folder` (19) | `Namespace` (3) |
| File | `File` (17) | `File` (1) |
| PGF Library | `Property` (10) | `Property` (7) |
| TikZ Library | `Property` (10) | `Property` (7) |
| Color | `Color` (16) | — |
| Color Model | `Color` (16) | — |
| Package | `Class` (7) | `Class` (5) |
| Class | `Class` (7) | `Class` (5) |
| BibTeX Entry (Misc) | `Interface` (8) | `Interface` (11) |
| BibTeX Entry (Article) | `Event` (23) | `Event` (24) |
| BibTeX Entry (Book) | `Struct` (22) | `Struct` (23) |
| BibTeX Entry (Collection) | `TypeParameter` (25) | `TypeParameter` (26) |
| BibTeX Entry (Part) | `Operator` (24) | `Operator` (25) |
| BibTeX Entry (Thesis) | `Unit` (11) | `Object` (19) |
| BibTeX String | `Text` (1) | `String` (15) |
| BibTeX Field | `Field` (5) | `Field` (8) |

逐组理解设计意图：

**LaTeX 文档结构组**（[L118-128](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L118-L128)）：texlab 把章节、公式、定理、标签等映射成语义最接近的通用枚举——`Section→Module`（容器）、`Equation→Constant`（稳定可引用）、`Theorem→Variable`（命名的引用对象）、`Label→Constructor`（被 `\ref` 构造引用的目标）、`Enumeration Item→EnumMember`（`enumerate` 里的条目，天然的「枚举成员」）。这些是「语义就近」的取舍。

**资源组**（[L129-136](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L129-L136)）：`File`、`Folder` 直接对应；`Package`/`Class` 都映射到 `Class`——LaTeX 的包和文档类本质是「类库」，符号面板里用同一种图标。

**BibTeX 条目组**（[L137-144](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L137-L144)）：这是全表设计意图最明显的一处。LSP 枚举里**根本没有「文献类型」这种概念**，于是 texlab 把六类常见 BibTeX 条目分别映射到**六个互不相同**的枚举值：

| BibTeX 条目类型 | CompletionItemKind | SymbolKind |
| --- | --- | --- |
| Misc | `Interface` (8) | `Interface` (11) |
| Article | `Event` (23) | `Event` (24) |
| Book | `Struct` (22) | `Struct` (23) |
| Collection | `TypeParameter` (25) | `TypeParameter` (26) |
| Part | `Operator` (24) | `Operator` (25) |
| Thesis | `Unit` (11) | `Object` (19) |

设计意图不是「语义匹配」（`Article` 和 `Event` 没什么语义关联），而是**借用互不相同的枚举值来制造视觉区分**：在一篇文档引用了几十条文献时，编辑器能给文章、书、学位论文画不同图标，让你一眼分辨条目类型。这是「枚举不够用就挪用」的典型手法，前提是被挪用的值（`Event`、`Struct`、`Operator` 等）在 LaTeX 场景里没有其他结构占用，不会撞图标。

其余两项：`BibTeX String`（`@string` 缩写定义）→ `Text`/`String`；`BibTeX Field`（如 `author`、`title` 字段名）→ `Field`，这是少有的**语义完全对上**的映射。

#### 4.3.4 代码实践（本讲主实践）

**目标**：用一份覆盖多种结构的文档，把整张映射表在真实编辑器里走一遍，逐项核对 `CompletionItemKind` 与 `SymbolKind`。

**步骤**：

1. 新建 `main.tex`（示例代码，非项目原有代码）：

```latex
\documentclass{article}
\usepackage{amsmath}

\newcommand{\hello}{Hello, world!}

\begin{document}
\section{Introduction}
\label{sec:intro}

\ref{sec:intro} and cite \cite{knuth1984}.

\begin{equation}
  \label{eq:euler}
  e^{i\pi} + 1 = 0
\end{equation}

\end{document}
```

2. 新建 `refs.bib`（示例代码）：

```bibtex
@article{knuth1984,
  author = {Donald E. Knuth},
  title  = {The TeXbook},
  year   = {1984}
}
```

3. 在支持 texlab 的编辑器里打开这两个文件。
4. **查补全列**：分别输入 `\sect`（命令）、`\begin{`（环境）、`\ref{`（标签）、`\cite{`（文献键），观察每个弹窗里候选的图标。
5. **查符号列**：打开 `main.tex` 与 `refs.bib` 的大纲面板，观察各结构图标。

**需要观察并对照映射表确认的现象**（预测值来自 [L116-144](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L116-L144)）：

| 你看到的结构 | 出现场景 | 预测种类（名称 = 数值） |
| --- | --- | --- |
| `\section` 候选 | 补全 | `Function` (3) |
| `\section{Introduction}` | 大纲 | `Module` (2) |
| `equation` 环境名候选 | 补全 | `Enum` (13) |
| `\begin{equation}` 块 | 大纲 | `Constant` (14) |
| `sec:intro` / `eq:euler` 标签候选（在 `\ref{` 里） | 补全 | `Constructor` (4) |
| 标签在大纲里 | 大纲 | `Constructor` (9) |
| `\hello` 的定义 `\newcommand` | 大纲 | `Key` (20) |
| `knuth1984` 候选（在 `\cite{` 里） | 补全 | `Event` (23) |
| `knuth1984` 条目（`refs.bib` 大纲里） | 大纲 | `Event` (24) |

**预期结果**：上表中每一项的图标种类都与映射表吻合。注意 `Function` 在补全列是 3、在符号列是 12——数值不同但都叫 `Function`。

**截图标注**：对补全弹窗与大纲面板各截一张图，按上表给每个图标标上对应的枚举名与数值。

**注意**：图标具体外观、大纲是否纳入标签/公式等「待本地验证」（受编辑器主题与 `texlab.symbols.*` 配置影响）；但凡出现的项，其 `kind` 一定符合映射表。

#### 4.3.5 小练习与答案

**练习 1**：把六类 BibTeX 条目设计成各不相同的枚举值，设计意图是什么？

**答案**：LSP 没有「文献类型」概念，texlab 借用六个互不相同的枚举值（`Interface`/`Event`/`Struct`/`TypeParameter`/`Operator`/`Unit`-`Object`），让 Article、Book、Thesis 等不同类型文献在补全与大纲里显示不同图标，便于一眼区分。语义并非重点，视觉区分才是。

**练习 2**：`Color` 与 `Color Model` 为什么只有 `CompletionItemKind`、没有 `SymbolKind`？

**答案**：颜色名/颜色模型只在补全时出现（用户输入颜色时弹出），它们不是文档的结构性组成部分，不会出现在大纲面板，所以没有 `SymbolKind`。

**练习 3**：你在写一个 texlab 客户端，收到一个补全项 `kind=20`。它可能是什么结构？如果是在大纲里收到 `kind=20` 呢？

**答案**：补全列 `kind=20` 是 `EnumMember`，对应 `Enumeration Item`（[L127](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L127)）；大纲列 `kind=20` 是 `Key`，对应 `Command Definition`（[L120](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L120)）。同一个数值 20 在两个枚举里含义完全不同——这正说明必须先确定 `kind` 来自哪个枚举，再查表。

---

## 5. 综合实践

把本讲三张表串起来，做一次「枚举侦探」：

1. 用 4.3.4 的 `main.tex` + `refs.bib`，再补一个定理环境与一个 `figure` 浮动体：

```latex
\newtheorem{thm}{Theorem}
\begin{thm}\label{thm:pyth} ...\end{thm}
\begin{figure}\centering\fbox{pic}\caption{a figure}\label{fig:one}\end{figure}
```

2. 打开大纲面板，给 `Theorem`、`Float`、`Equation`、`Section`、`Label`、`Command Definition` 各自标注其 `SymbolKind` 名称与数值（对照 [L120-128](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L120-L128)）。
3. 在 `.bib` 里再加一条 `@book` 和一条 `@phdthesis`（归入 Thesis 类），打开 `refs.bib` 大纲，确认 Article/Book/Thesis 三类条目图标互不相同（对照 [L138-142](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L138-L142)）。
4. 写一段总结：举出一个「同名不同值」（如 `Function` 3 vs 12）和一个「同名同值不同列」的例子，说明为什么写客户端时不能跨枚举混用数值。

**预期**：你能不看 wiki，凭图标反推出 texlab 给每项填的 `kind`，并解释每一处映射的设计理由。

---

## 6. 本讲小结

- texlab 用 LSP 的两个**标准**枚举给自己的 LaTeX/BibTeX 结构打标签：`CompletionItemKind` 管补全弹窗图标，`SymbolKind` 管大纲面板图标。
- 两个枚举由两个独立请求（`textDocument/completion` 与 `textDocument/documentSymbol`）产生，是规范里**各自独立**定义的，数值互不相关——同名 `Function` 在补全列是 3、在符号列是 12。
- 完整映射表见 [LSP-Internals.md:116-144](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L116-L144)；并非每个结构都两列俱全——`Snippet`/`Color` 只有补全列，`Command Definition` 只有符号列。
- LaTeX 结构多为「语义就近」映射（`Section→Module`、`Equation→Constant`、`Label→Constructor`）。
- 六类 BibTeX 条目被特意映射到**互不相同**的枚举值，目的是在没有「文献类型」枚举的前提下，靠挪用不同值制造视觉区分。
- 写 texlab 客户端时，判断 `kind` 必须先确认它来自哪个枚举，再按对应列查表。

---

## 7. 下一步学习建议

本讲回答了「这些结构以**什么图标**呈现」。接下来推荐进入 u5（高级配置与可扩展性），尤其是：

- **u5-l2 符号、补全、悬停与 Inlay Hints**：讲 `texlab.symbols.allowedPatterns`/`ignoredPatterns`/`customEnvironments`（决定哪些结构**进不进**大纲）、`completion.matcher`（补全如何匹配）、`inlayHints.*`。它和本讲是「内容控制」与「图标种类」的两面——本讲学完图标规则后，正好去学如何过滤这些图标背后的结构。
- **u5-l4 experimental 扩展点**：讲 `labelDefinitionCommands`/`labelReferencePrefixes` 等，能让你**新增**自定义命令产生的标签——这些新标签会沿用本讲的 `Label → Constructor` 映射。把 u5-l4 与本讲合看，就能完整理解「自定义结构如何获得标准图标」。

继续阅读源码时，建议把 [LSP-Internals.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md) 的「Custom Messages」与「Enum Mapping」两部分对照看：前者是 texlab 给 LSP **加**的东西，后者是 texlab **用** LSP 已有东西的方式，两者合起来就是 texlab 与 LSP 的完整接口面。
