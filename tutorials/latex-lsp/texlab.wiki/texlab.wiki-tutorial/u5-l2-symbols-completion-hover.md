# 符号、补全、悬停与 Inlay Hints

## 1. 本讲目标

本讲聚焦 texlab 中「**编辑器内如何呈现 LaTeX 结构**」的一组配置。读完本讲，你应当能够：

- 用 `texlab.symbols.allowedPatterns` / `ignoredPatterns` 按正则过滤文档符号，并理解符号过滤的**递归**特性；用 `texlab.symbols.customEnvironments` 把自定义环境（如 `mybox`）纳入文档大纲。
- 区分 `texlab.completion.matcher` 的四种取值（`fuzzy` / `fuzzy-ignore-case` / `prefix` / `prefix-ignore-case`），并知道在补全列表里它们如何改变候选数量与大小写行为。
- 配置 `texlab.hover.symbols`（`none` / `glyph` / `image`）控制符号命令（如 `\epsilon`）的悬停展示；开关 `\label` / `\ref` 的 inlay hint 并设置截断长度。

本讲依赖 [u2-l1 配置总览](u2-l1-config-overview.md) 建立的「配置归客户端、按 `texlab.*` 命名空间组织、用三要素（Type / Default value / Placeholders）阅读」这套通用语言。同时与 [u4-l3 枚举映射](u4-l3-enum-mapping.md) 相呼应——本讲讨论的「符号 / 补全项以何种 Kind 呈现」正是由那张映射表决定的。

## 2. 前置知识

在进入配置前，先用一句话复习几个 LSP 概念（初学者可只记黑体部分）：

- **Document Symbols（文档符号）**：由标准方法 `textDocument/build` 之外的标准请求 `textDocument/documentSymbol` 产出，就是编辑器左侧的**大纲面板**（Outline）和顶部面包屑里看到的 `\section`、环境、方程等结构树。`texlab.symbols.*` 控制这棵树**呈现什么**。
- **Completion（补全）**：由标准方法 `textDocument/completion` 产出，即你输入 `\` 后弹出的候选列表。`texlab.completion.matcher` 控制服务端**如何按你输入的文本筛选候选**。
- **Hover（悬停）**：由标准方法 `textDocument/hover` 产出，光标停在某个命令上时弹出的小卡片。`texlab.hover.symbols` 只影响**符号类命令**（如 `\epsilon`）的展示方式。
- **Inlay Hints（内联提示）**：由标准方法 `textDocument/inlayHint` 产出，是直接在代码行内、用灰色小字显示的额外信息（例如在 `\label{...}` 旁边显示标签名）。`texlab.inlayHints.*` 控制其开关与截断。

一句话记忆：**这四组配置分别对应大纲、补全、悬停、内联提示四类编辑器内呈现，都不改变编译结果，只改变你在编辑器里「看到」的样子。**

## 3. 本讲源码地图

本讲涉及的文件只有两个，都是 wiki 文档页（本仓库本身是文档而非源码）：

| 文件 | 作用 |
| --- | --- |
| `Configuration.md` | texlab 全部配置项的权威清单。本讲引用其中的 `symbols.*`、`completion.matcher`、`hover.symbols`、`inlayHints.*` 几节。 |
| `LSP-Internals.md` | 协议层说明。本讲引用其中的「Enum Mapping」表，说明被纳入符号 / 补全的结构会以哪种 `SymbolKind` / `CompletionItemKind` 呈现。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **符号过滤与自定义环境**（`symbols.allowedPatterns` / `ignoredPatterns` / `customEnvironments`）
2. **补全匹配算法**（`completion.matcher`）
3. **悬停符号展示与 Inlay Hints**（`hover.symbols` 与 `inlayHints.*`）

### 4.1 符号（Document Symbols）的过滤与自定义环境

#### 4.1.1 概念说明

文档符号树是 texlab 解析 `.tex` 后产出的层级结构：一个 `\section` 里可能嵌套若干 `\subsection`，一个环境里可能嵌套方程、定理等。texlab 默认把常见的结构（节、环境、方程、标签、定理……）都纳入这棵树。

但在大型文档里，符号树可能非常臃肿——你可能只关心 `\section` 级别，想隐藏所有 `\label`；或者你用了自定义环境 `mybox`，希望它也像 `figure` 一样出现在大纲里。texlab 用三个配置项解决这两类需求：

- `texlab.symbols.allowedPatterns` / `ignoredPatterns`：用正则**过滤**已有符号；
- `texlab.symbols.customEnvironments`：**扩展**被识别为符号的环境名单。

这与 [u5-l1 诊断过滤](u5-l1-diagnostics-chktex.md) 里的 `diagnostics.allowedPatterns` / `ignoredPatterns` 是同一套「白名单 / 黑名单」思路，但用在符号上有一个关键差异：**符号过滤是递归的**。

#### 4.1.2 核心流程

符号过滤的逻辑可以概括为三步：

1. texlab 先把解析到的**完整符号树**构造出来（树节点带名字，如 `Section: Introduction`）。
2. 用 `allowedPatterns`（白名单）做**保留**过滤：某符号只有匹配至少一条白名单正则，才被保留。
3. 对保留下来的结果，再用 `ignoredPatterns`（黑名单）做**剔除**过滤：匹配任意一条黑名单正则的符号被移除。

顺序固定：**白名单先、黑名单后**（未通过白名单的符号根本到不了黑名单这一步）。

关键的「递归」体现在第 2、3 步对父子节点的处理方式上。用一个例子说明。假设符号树是：

```
Section: Introduction          (父)
   ├─ Equation: (1)            (子1)
   └─ Label: eq: gauss         (子2)
```

如果 `allowedPatterns` 里写了只匹配 `Equation` / `Label` 的规则，那么父节点 `Section` 因为不匹配会被移除——但**子节点 `Equation`、`Label` 仍然会被发送给客户端**，它们会被「提」到树的上一层，而不会随父节点一起被删掉。换句话说，过滤是「逐节点判定」而非「删父即删整棵子树」。这正是 texlab 文档里那句「nested symbols can still be sent … even though the parent node is removed」的含义。

> 对比：诊断（diagnostics）是扁平列表，没有父子关系，因此 `diagnostics.*Patterns` 不存在递归问题；只有符号过滤特意声明了递归语义。

#### 4.1.3 源码精读

先看白名单 [`texlab.symbols.allowedPatterns`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L231-L246)（Configuration.md:231-246），它明确写了「匹配至少一条才发送」以及**递归**特性：

```markdown
A list of regular expressions used to filter the list of reported document symbols.
If specified, only symbols that match _at least one_ of the specified patterns
are sent to the client.
Symbols are filtered recursively so nested symbols can still be sent to the client
even though the parent node is removed from the results.
```

黑名单 [`texlab.symbols.ignoredPatterns`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L250-L260)（Configuration.md:250-260）与之对称——「匹配零条才发送」，并回链到白名单项。二者叠加时的执行顺序（白名单先、黑名单后）在白名单项的 _Hint_ 段落里写明。

再看扩展项 [`texlab.symbols.customEnvironments`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L264-L285)（Configuration.md:264-285），它的类型是一个对象数组，wiki 给出了 TypeScript 接口：

```ts
interface SymbolEnvironmentOptions {
    name: string;          // 环境名，必填
    displayName?: string;  // 大纲里显示的名字，默认 title case
    label?: false;         // 见下方说明
}
```

- `name` 是 `\begin{name}` 里的环境名（**不带反斜杠**，写 `mybox` 而非 `\mybox`）。
- `displayName` 省略时默认用 **title case**（如 `mybox` → `Mybox`）。
- `label` 字段：wiki 注释说「If set, the server will try to match a label to environment and append its number」（设定后，服务器会尝试把标签匹配到该环境并追加编号），但其类型标注写的是 `false`，**类型与描述看起来存在出入**——该字段的确切取值与语义以本地实际行为为准（待本地验证）。

这些被纳入的自定义环境，在符号面板里会以哪种图标呈现？查 [LSP-Internals.md 的 Enum Mapping 表](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L116-L129)（LSP-Internals.md:116-129）可知，**Environment** 结构被映射为 `CompletionItemKind = Enum(13)`、`SymbolKind = Enum(10)`。也就是说，`customEnvironments` 加进去的环境，在大纲里和内置的 `figure`、`equation` 一样以「Enum」类图标出现。

#### 4.1.4 代码实践

**实践目标**：把一个自定义环境 `mybox` 加入文档符号面板，并观察默认 `displayName`。

**操作步骤**：

1. 准备一个测试文件 `demo.tex`（**示例代码**）：

   ```latex
   \documentclass{article}
   \newenvironment{mybox}[1]{\begin{center}#1\end{center}}{}
   \begin{document}
   \begin{mybox}{Hello}
   \section{Introduction}\label{sec:intro}
   \end{mybox}
   \end{document}
   ```

2. 在编辑器的 texlab 配置里加入（以 VS Code 风格 JSON 为例，**示例配置**）：

   ```json
   {
     "texlab.symbols.customEnvironments": [
       { "name": "mybox" }
     ]
   }
   ```

3. 重载窗口 / 重启语言服务器，让 texlab 重新查询配置（回顾 u2-l1：配置由客户端持有，服务器按需查询）。

4. 打开编辑器的 **Outline / 大纲** 面板。

**需要观察的现象**：大纲里是否出现 `mybox` 节点；其显示文字是否为 title case（`Mybox`）。

**预期结果**：`Mybox` 出现在大纲中（图标为 Enum 类）；它内部还嵌套着 `Introduction` 节点和 `sec:intro` 标签。

**待本地验证**：`label` 字段的实际效果、以及 `displayName` 在不同编辑器主题下的大小写呈现。

#### 4.1.5 小练习与答案

**练习 1**：如果 `allowedPatterns` 把某个父级 `\section` 过滤掉了，它内部的 `\label` 还会出现在大纲里吗？为什么？
**答案**：会。因为符号过滤是**递归的逐节点判定**——父节点被移除时，匹配规则的子节点会被提到上一层继续保留，而不是随父节点整体删除。

**练习 2**：`customEnvironments` 里某项只填了 `name`，没填 `displayName`，大纲里会显示什么？
**答案**：默认使用 **title case**，即把环境名首字母大写后显示（如 `mybox` → `Mybox`）。

**练习 3**：`symbols.allowedPatterns` 与 `diagnostics.allowedPatterns` 名称几乎一样，最大的语义差别是什么？
**答案**：符号版有**递归过滤**语义（父子节点独立判定），诊断版作用在扁平列表上、无父子关系。

### 4.2 补全匹配算法 completion.matcher

#### 4.2.1 概念说明

当你在 `.tex` 里输入 `\sec` 想补全 `\section` 时，texlab 会先在内部生成一大批候选（所有它知道的命令、环境、标签、被引文献……），然后**按你输入的文本做一次筛选**，只把命中的发回给编辑器。`texlab.completion.matcher` 决定的就是「用什么算法做这次筛选」。

这里有两种基本匹配思路：

- **prefix（前缀）**：候选必须**以你输入的文本开头**才算命中。要求严格、候选少、命中即高度相关。
- **fuzzy（模糊）**：你输入的字符只要**按顺序出现**在候选里即可（不必连续、不必从头开始）。要求宽松、候选多、容错好。

每种思路再叠加一个**大小写开关**，于是得到四个取值。

#### 4.2.2 核心流程

四种取值可以用一张表概括（用候选 `\textbf` / `\textit` / `\texttt` / `\emph` 做示意，**示例候选**）：

| matcher 取值 | 大小写 | 匹配规则 | 输入 `text` 命中 | 输入 `tt` 命中 |
| --- | --- | --- | --- | --- |
| `prefix` | 区分 | 必须从头连续匹配 | `\textbf` `\textit` `\texttt` | 无（都不以 `tt` 开头） |
| `prefix-ignore-case` | 不区分 | 同上但不区分大小写 | 同上（这里无差别） | 无 |
| `fuzzy` | 区分 | 按顺序出现即可（可不连续） | 上述三个 | `\textbf` `\textit` `\texttt`（都含 t…t） |
| `fuzzy-ignore-case` | 不区分 | 同上但不区分大小写 | 同上 | 同上 |

可以总结成一个判定流程：

```
用户输入文本 T
  ├── matcher 含 "prefix"  → 保留「以 T 开头」的候选
  └── matcher 含 "fuzzy"   → 保留「T 的字符按序出现在候选中」的候选
  └── matcher 含 "ignore-case" → 上述比较不区分大小写
```

直觉记忆：**`prefix` 严、`fuzzy` 宽；带 `-ignore-case` 就不挑大小写**。默认值是 `fuzzy-ignore-case`——最宽松的一种，目的是「随便敲几个字母就能找到」，适合不记得命令全名的场景。

> 待本地验证：wiki 只写了「filter out items that do not start with the search text」「fuzzy string matching」，并未规定筛选用的是带反斜杠的命令全名还是去掉反斜杠的标签。精确的匹配边界以本地编辑器实测为准。

#### 4.2.3 源码精读

[`texlab.completion.matcher`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L363-L374)（Configuration.md:363-374）原文即列出四个取值与默认值：

```markdown
- `fuzzy`: Fuzzy string matching (case sensitive)
- `fuzzy-ignore-case`: Fuzzy string matching (case insensitive)
- `prefix`: Filter out items that do not start with the search text (case sensitive)
- `prefix-ignore-case`: Filter out items that do not start with the search text (case insensitive)
**Default value:** `fuzzy-ignore-case`
```

注意它是扁平键 `texlab.completion.matcher`（一个 `string`），不是 `texlab.completion` 下的对象——这是 u2-l1 提过的「命名扁平化」现象（类似 `diagnosticsDelay`）。被筛选出来的候选项各自带一个 `CompletionItemKind`，其取值由 [Enum Mapping 表](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L116-L143)（LSP-Internals.md:116-143）决定（如 Command → `Function(3)`、Label → `Constructor(4)`、BibTeX Entry 按类型分到不同 Kind）。`matcher` 只管「留哪些」，不管「留下来的长什么样」。

#### 4.2.4 代码实践

**实践目标**：直观感受四种 matcher 在候选数量与大小写上的差异。

**操作步骤**：

1. 打开任意 `.tex` 文件，在正文中输入 `\` 触发补全。
2. 依次把 `texlab.completion.matcher` 改为下面四个值并重载（**示例配置**）：

   ```json
   { "texlab.completion.matcher": "prefix" }
   { "texlab.completion.matcher": "prefix-ignore-case" }
   { "texlab.completion.matcher": "fuzzy" }
   { "texlab.completion.matcher": "fuzzy-ignore-case" }
   ```

3. 每次改完后，分别输入小写测试串（如 `text`）和大写测试串（如 `TEXT`），观察弹出的候选列表。

**需要观察的现象**：
- 同样输入 `text` 时，`prefix` 与 `fuzzy` 哪个候选更多（预期 `fuzzy` 更多）。
- 输入大写 `TEXT` 时，带 `-ignore-case` 的两个是否仍能命中（预期命中），不带的是否命中数为零。

**预期结果**：`prefix` 比 `fuzzy` 严格、候选更少；`-ignore-case` 让大小写不再影响命中。

**待本地验证**：不同编辑器对 LSP 补全的客户端侧二次过滤可能叠加，最终候选数以本地为准。

#### 4.2.5 小练习与答案

**练习 1**：用户希望「必须从头连续匹配，但不区分大小写」，应选哪个取值？
**答案**：`prefix-ignore-case`。

**练习 2**：默认值是哪一个？为什么 texlab 选了最宽松的一种作为默认？
**答案**：默认 `fuzzy-ignore-case`。因为它容错最好——用户经常只记得命令的几个字母、又记不清大小写，模糊 + 忽略大小写能最大化「敲得到」的概率。

**练习 3**：把 matcher 从 `fuzzy-ignore-case` 改成 `prefix`，候选列表通常会变多还是变少？
**答案**：变少。前缀匹配比模糊匹配严格，能命中的候选更少。

### 4.3 悬停符号展示 hover.symbols 与 Inlay Hints

#### 4.3.1 概念说明

本模块管两类「行内/卡片式」的呈现：

- **悬停（hover）**：光标停在命令上弹出的卡片。texlab 对**符号类命令**（即代表数学符号的命令，如 `\epsilon`、`\alpha`）提供特殊展示——可以选择不显示、显示一个 unicode 字形、或显示一张符号图片。`texlab.hover.symbols` 控制这个选择。注意它**只影响符号类命令**，普通命令（如 `\section`）的悬停由其它逻辑负责。
- **Inlay Hints（内联提示）**：编辑器把元信息直接以灰色小字叠在代码行内。texlab 默认会对两类命令产生 inlay hint：`\label`（定义处）和 `\ref`（引用处）。`texlab.inlayHints.*` 控制其开关与文本截断长度。

#### 4.3.2 核心流程

`hover.symbols` 的取值与回退逻辑：

```
hover.symbols =
  ├── none   → 符号命令不显示任何悬停
  ├── glyph  → 有 unicode 字形就用字形；没有则……（无更多回退）
  └── image  → 有符号图片就用 markdown 图片预览；没有则回退到 glyph
```

要点：`image` 是默认值，且它会**自动回退**到 `glyph`——所以选 `image` 不会比 `glyph` 更差。`none` 则彻底关闭符号悬停。

`inlayHints.*` 三个开关：

| 配置项 | 类型 | 默认 | 作用 |
| --- | --- | --- | --- |
| `inlayHints.labelDefinitions` | `boolean` | `true` | 对 `\label` 类命令显示 inlay hint |
| `inlayHints.labelReferences` | `boolean` | `true` | 对 `\ref` 类命令显示 inlay hint |
| `inlayHints.maxLength` | `int \| null` | `null` | 截断 inlay hint 文本到指定长度；`null` 表示不截断 |

注意两个易踩的点：

1. **`\label` 与 `\ref` 的 inlay hint 默认都是开启的**（`true`）。所以如果你「突然看到行内有灰字」，多半就是它们；想关掉就把对应项设为 `false`。
2. `maxLength` 默认 `null` 表示**不截断**；设成正整数（如 `30`）才会把过长的标签文本截到该长度。设为 `0` 的行为未在 wiki 说明（待本地验证）。

#### 4.3.3 源码精读

[`texlab.hover.symbols`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L378-L388)（Configuration.md:378-388）列出三值与回退关系：

```markdown
- `none`: No hover is shown
- `glyph`: If available, the command is shown using a unicode character
- `image`: If available, a markdown image preview is returned. If not, the `glyph` method is tried next.
**Default value:** `image`
```

inlay hints 三项位于 [Configuration.md:393-419](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L393-L419)，分别是 [`labelDefinitions`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L393-L399)（对 `\label` 类命令）、[`labelReferences`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L403-L409)（对 `\ref` 类命令）、[`maxLength`](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L413-L419)（截断长度）。三者都在 `texlab.inlayHints.*` 命名空间下，前两个默认 `true`，第三个默认 `null`。

> 这里出现「`\label` 类 / `\ref` 类命令」的说法，是因为 texlab 允许你用 `texlab.experimental.labelDefinitionCommands` / `labelReferenceCommands` 把自定义命令也当作 `\label` / `\ref`（见 [u5-l4 experimental 扩展点](u5-l4-experimental-extensions.md)）。inlay hint 同样会作用于这些被扩展进来的命令，而不仅限于原生的 `\label` / `\ref`。

#### 4.3.4 代码实践

**实践目标**：验证 `\label` / `\ref` 的 inlay hint，以及 `\epsilon` 的三种悬停展示。

**操作步骤**：

1. 准备测试文件 `demo2.tex`（**示例代码**）：

   ```latex
   \documentclass{article}
   \begin{document}
   \section{Intro}\label{sec:intro}
   See \ref{sec:intro}. A symbol: $\epsilon$.
   \end{document}
   ```

2. inlay hint 两个开关默认就是 `true`，你也可以显式确认（**示例配置**）：

   ```json
   {
     "texlab.inlayHints.labelDefinitions": true,
     "texlab.inlayHints.labelReferences": true,
     "texlab.inlayHints.maxLength": 30
   }
   ```

3. 把鼠标悬停在 `$\epsilon$` 的 `\epsilon` 上，依次把 `texlab.hover.symbols` 设为 `none` → `glyph` → `image` 并重载。

**需要观察的现象**：
- `\label{sec:intro}` 行内是否出现灰色 inlay hint 文本；`\ref{sec:intro}` 行内是否显示解析后的标签信息。
- 悬停 `\epsilon` 时：`none` 无卡片；`glyph` 显示 unicode 字形（ε）；`image` 优先显示符号图片、无图则退回字形。

**预期结果**：两个 inlay hint 默认可见；`hover.symbols` 三值效果差异明显，`image` ≥ `glyph` > `none`。

**待本地验证**：`maxLength` 截断的具体计字方式（按字符数），以及 `hover.symbols=image` 在你的 texlab 版本里是否真有对应符号图片资源。

#### 4.3.5 小练习与答案

**练习 1**：`hover.symbols` 设为 `image`，但某个符号没有图片资源，会发生什么？
**答案**：会**回退到 `glyph`**（用 unicode 字形显示）。`image` 永远不会比 `glyph` 更差。

**练习 2**：`inlayHints.maxLength` 默认值是什么？含义是什么？
**答案**：默认 `null`，表示**不截断** inlay hint 文本。

**练习 3**：用户抱怨「我的 `.tex` 里突然多了一堆灰字」，最可能是哪个配置导致的？怎么关？
**答案**：最可能是 `inlayHints.labelDefinitions` / `labelReferences`（默认 `true`）产生的 `\label` / `\ref` inlay hint。把对应项设为 `false` 即可关闭。

## 5. 综合实践

把本讲三块内容串起来，为一个真实的小工程写一份「呈现层」配置。**目标**：让大纲只显示节与环境、让补全严格、让长标签的 inlay hint 不撑爆屏幕。

1. 准备一个含自定义环境 `mybox`、若干 `\section`、一个长名 `\label{very:long:label:name:that:overflows}` 与对应 `\ref` 的 `.tex`。
2. 编写配置（**示例配置**）：

   ```json
   {
     "texlab.symbols.customEnvironments": [
       { "name": "mybox", "displayName": "Box" }
     ],
     "texlab.symbols.ignoredPatterns": ["^Label:"],
     "texlab.completion.matcher": "prefix-ignore-case",
     "texlab.hover.symbols": "image",
     "texlab.inlayHints.labelDefinitions": true,
     "texlab.inlayHints.labelReferences": true,
     "texlab.inlayHints.maxLength": 20
   }
   ```

3. 逐项验证：
   - 大纲里出现 `Box`（自定义环境），且所有 `Label:` 节点被 `ignoredPatterns` 隐藏（注意：被隐藏的是 Label 节点本身，若有子节点仍会因递归保留——本例 Label 无子节点，故整体消失）。
   - 补全只保留以输入文本开头（不区分大小写）的候选。
   - 长 `\label` 的 inlay hint 被截到 20 个字符。
4. 记录每一项配置分别驱动了哪个 LSP 方法（`documentSymbol` / `completion` / `hover` / `inlayHint`）。

**待本地验证**：`ignoredPatterns` 的正则到底匹配的是符号的显示名（如 `Label: sec:intro`）还是纯类型前缀，需用本地大纲实测确认后再定稿正则。

## 6. 本讲小结

- `texlab.symbols.allowedPatterns` / `ignoredPatterns` 用正则过滤文档符号，**白名单先、黑名单后**，且过滤是**递归的**——父节点被移除时子节点仍可保留并上提。
- `texlab.symbols.customEnvironments` 可把自定义环境纳入大纲，类型为 `SymbolEnvironmentOptions[]`（`name` 必填、`displayName` 默认 title case）；被纳入的环境按 Enum 类（`SymbolKind=10`）呈现。
- `texlab.completion.matcher` 四取值 = {`fuzzy`, `prefix`} × {区分大小写, `-ignore-case`}；默认 `fuzzy-ignore-case`（最宽松）；`prefix` 要求从头连续、`fuzzy` 允许按序不连续。
- `texlab.hover.symbols` 仅影响符号类命令（如 `\epsilon`），取值 `none` / `glyph` / `image`，默认 `image` 且会**回退到 `glyph`**。
- `texlab.inlayHints.labelDefinitions` / `labelReferences` **默认均为 `true`**（`\label` / `\ref` 的内联提示默认开）；`maxLength` 默认 `null`（不截断）。
- 这四组配置只改变编辑器内**呈现**（大纲 / 补全 / 悬停 / 内联提示），不影响编译结果。

## 7. 下一步学习建议

- 想把自定义命令也纳入 `\label` / `\ref` 体系、从而让 inlay hint 与补全覆盖它们，请继续阅读 [u5-l4 experimental 扩展点：自定义命令与环境](u5-l4-experimental-extensions.md)。
- 想了解这些符号 / 补全项的图标为何如此分配，回顾 [u4-l3 枚举映射](u4-l3-enum-mapping.md) 的映射表。
- 想清理编译产物与查看依赖图（与本讲的「呈现」互补，偏「工程管理」），可阅读 [u4-l2 workspace/executeCommand 工作区命令](u4-l2-workspace-commands.md)。
