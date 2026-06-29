# experimental 扩展点：自定义命令与环境

## 1. 本讲目标

本讲是 [u5 高级配置](u5-l1-diagnostics-chktex.md) 的最后一篇，聚焦 `texlab.experimental.*` 这组「**可扩展点**」。学完本讲，你应该能够：

- 说出 `experimental.*` 这组配置存在的意义：让 texlab 认识你自己定义的命令与环境（自定义的引用命令、标签命令、定理环境、verbatim 环境等），而不是只认 LaTeX 内置的那一套。
- 用 `mathEnvironments` / `enumEnvironments` / `verbatimEnvironments` 扩展三类环境的内置名单，并理解 `verbatimEnvironments` 能用来**抑制诊断**。
- 用 `citationCommands` / `labelDefinitionCommands` / `labelReferenceCommands` / `labelReferenceRangeCommands` / `glossaryReferenceCommands` 扩展五类命令，并牢记「**命令不带前导 `\`**」这一统一约定。
- 用 `labelDefinitionPrefixes` / `labelReferencePrefixes` 把「自定义命令 + 前缀」关联起来，让 `\newcommand{\thm}[1]{\label{thm:#1}}` 这类包装命令产生的带前缀标签被 texlab 正确识别，从而可被补全与引用。

本讲依赖 [u2-l1 配置总览](u2-l1-config-overview.md) 建立的「配置归客户端、按 `texlab.*` 命名空间组织、用三要素（Type / Default value / Placeholders）阅读」这套通用语言，也依赖 [u1-l2 项目识别与根目录检测](u1-l2-project-detection.md) 里「依赖树 / Discovery」的概念（`followPackageLinks` 作用的正是这棵依赖树）。它与 [u5-l2 符号、补全、悬停与 Inlay Hints](u5-l2-symbols-completion-hover.md) 相互呼应：本讲教 texlab「**认识哪些新结构**」，而 u5-l2 教 texlab「**如何把这些结构呈现出来**」。

## 2. 前置知识

进入配置前，先用几句话把本讲会用到的 LaTeX 概念说清楚（不熟悉 LaTeX 的读者只需记黑体）：

- **环境（environment）**：由 `\begin{name} … \end{name}` 包裹的代码块，`name` 是**环境名**（不带反斜杠）。例如 `\begin{equation} … \end{equation}` 的环境名是 `equation`。
  - **数学环境（math environment）**：里面是数学公式，如 `equation`、`align*`。
  - **枚举环境（enumeration environment）**：里面是列表项，如 `itemize`、`enumerate`。
  - **逐字环境（verbatim environment）**：里面的内容**原样输出、不当作 LaTeX 解析**，如 `verbatim`、`lstlisting`、`minted`。texlab 默认也「放过」这类环境——不对它做 LaTeX 语法分析，因此也不会在里面报诊断。
- **命令（command）**：以反斜杠开头的指令，如 `\cite`、`\label`、`\ref`、`\gls`、`\crefrange`。注意：**当你在 `experimental.*` 里登记一个命令时，要写它的「裸名」**（如 `foo`），**不带前导 `\`**。这是本讲最容易踩的坑，下面会反复强调。
- **标签（label）与引用（reference）**：`\label{thm:foo}` 给某个位置打一个名为 `thm:foo` 的标签，`\ref{thm:foo}` 引用它。`thm:` 这种就是**前缀**，用来区分不同类型的标签（定理用 `thm:`、章节用 `sec:`、公式用 `eq:`）。本讲的「前缀关联」正是为了让 texlab 看懂自定义命令产生的「带前缀标签」。
- **术语（glossary）**：`\gls{term}` 引用一个术语表条目（glossaries 宏包提供），与 `\cite` 引用文献是平行关系。
- **跨范围引用（reference range）**：`\crefrange{a}{b}`（cleveref 宏包）表示「引用从 a 到 b 的范围」，与普通 `\ref` 不同，texlab 需要单独识别。

最后回顾一条来自 u2-l1 的总则：本讲所有配置项的 **Placeholders** 都是「无」——`%f`/`%p`/`%l` 只出现在 `build.args` 与 `forwardSearch.args`，**扩展点不调用外部命令**，因此不涉及占位符。它们改变的只是 texlab 内部的「结构识别名单」。

## 3. 本讲源码地图

本仓库是纯文档 wiki，本讲的「源码」就是 `Configuration.md` 末尾的 `experimental` 小节，共 11 个配置项（集中在文件尾部 L423–L550）。为方便查阅，先列一张总表：

| 配置项 | 类型 | 默认值 | 扩展对象 | 是否带「无 `\`」约定 |
|---|---|---|---|---|
| `texlab.experimental.followPackageLinks` | `boolean` | `false` | 依赖图解析 | —（非名单类） |
| `texlab.experimental.mathEnvironments` | `string[]` | `[]` | 数学环境 | 否（写裸环境名） |
| `texlab.experimental.enumEnvironments` | `string[]` | `[]` | 枚举环境 | 否（写裸环境名） |
| `texlab.experimental.verbatimEnvironments` | `string[]` | `[]` | 逐字环境（可抑制诊断） | 否（写裸环境名） |
| `texlab.experimental.citationCommands` | `string[]` | `[]` | 引用命令（`\cite` 类） | **是** |
| `texlab.experimental.labelDefinitionCommands` | `string[]` | `[]` | 标签定义命令（`\label` 类） | **是** |
| `texlab.experimental.labelReferenceCommands` | `string[]` | `[]` | 标签引用命令（`\ref` 类） | **是** |
| `texlab.experimental.labelReferenceRangeCommands` | `string[]` | `[]` | 范围引用命令（`\crefrange` 类） | **是** |
| `texlab.experimental.labelDefinitionPrefixes` | `(string, string)[]` | `[]` | 命令↔前缀 关联（定义侧） | 部分（命令名无 `\`） |
| `texlab.experimental.labelReferencePrefixes` | `(string, string)[]` | `[]` | 命令↔前缀 关联（引用侧） | 部分（命令名无 `\`） |
| `texlab.experimental.glossaryReferenceCommands` | `string[]` | `[]` | 术语引用命令（`\gls` 类） | **是** |

> 注意：除 `followPackageLinks` 是布尔开关外，其余 10 项都是「名单」，默认全是空数组 `[]`。**默认空数组意味着「只使用 texlab 内置的名单」**——你不配置，texlab 就只认识它出厂认得的那批命令/环境；你往里追加，是在内置名单之上**做并集**，而不是替换。

本讲把这张表按职责拆成三个最小模块来讲：4.1 讲三类**环境**扩展，4.2 讲五类**命令**扩展，4.3 讲把命令与标签前缀**关联**起来的两对配置。`followPackageLinks` 是其中唯一的「非名单」项，放在 4.1 的概述里一并说明（它作用的对象是 u1-l2 的依赖树）。

## 4. 核心概念与源码讲解

### 4.1 自定义环境扩展：mathEnvironments / enumEnvironments / verbatimEnvironments

#### 4.1.1 概念说明

texlab 内部维护着几张「环境分类名单」：哪些环境算**数学环境**、哪些算**枚举环境**、哪些算**逐字环境**。这三类名单之所以重要，是因为它们决定了 texlab 在解析到 `\begin{…}` 时该**用什么模式**处理里面的内容：

- 标记为**数学环境**的，里面的内容按数学公式处理（例如把 `$...$`/`\(...\)` 的语义、公式编号、`\ref` 到公式的跳转都启用）。
- 标记为**枚举环境**的，里面的 `\item` 会被识别为列表项结构。
- 标记为**逐字环境**的，里面的内容**不被当作 LaTeX 解析**，因此 texlab **不会在里面上报诊断**。

LaTeX 的生态非常庞大，texlab 不可能预知你用的每一个宏包定义的环境。比如你可能用 `tcolorbox` 定义了一个 `mybox` 环境、用 `minted` 包了一个自定义的 `pycode` 环境。`experimental.*Environments` 这三项就是让你**把自己的环境名追加进对应名单**，使 texlab 用正确的模式对待它们。

与之并列的还有 `followPackageLinks`：它虽然不属名单，但同样是「让 texlab 看到更多结构」的扩展点——开启后，**自定义宏包（custom packages）的依赖也会被解析并纳入依赖图**（即 u1-l2 讲过的 Discovery 依赖树）。它默认关闭，因为解析宏包依赖有性能成本。

#### 4.1.2 核心流程

三项环境扩展的运作方式完全一致，可以概括为一句话：**把你的环境名并入内置名单，解析时按名单分类**。用伪代码描述 texlab 在解析到一个 `\begin{X}` 时的判定（示例代码，仅说明语义）：

```text
function classifyEnvironment(name):
    mathSet    = builtin.mathEnvironments    ∪ config.mathEnvironments     # 并集
    enumSet    = builtin.enumEnvironments    ∪ config.enumEnvironments
    verbatimSet= builtin.verbatimEnvironments∪ config.verbatimEnvironments

    if name in verbatimSet: return VERBATIM   # 不做 LaTeX 解析，不上报诊断
    if name in mathSet:     return MATH       # 按数学公式处理
    if name in enumSet:     return ENUM       # 识别 \item 列表结构
    return NORMAL                             # 普通 LaTeX 环境
```

关键点有三：

1. **并集而非替换**：你配置的是「追加项」，texlab 内置的 `equation`、`itemize`、`verbatim` 等依然有效，不会被你的名单覆盖掉。
2. **逐字环境能抑制诊断**：这是 `verbatimEnvironments` 最实用的功能——当你有一个内嵌代码、伪代码或非 LaTeX 内容的环境（如 `minted`、`lstlisting`，或自定义的 `pycode`），texlab 默认会尝试按 LaTeX 解析它并报一堆「语法错误」。把它登记为 verbatim 后，texlab 就放过它，诊断随之消失。这和 [u5-l1](u5-l1-diagnostics-chktex.md) 里用 `ignoredPatterns` 屏蔽诊断是不同层面的手段：`ignoredPatterns` 是「诊断已经产生、推送前过滤」，而 `verbatimEnvironments` 是「从源头让 texlab 不分析这段内容」。
3. **环境名不带反斜杠**：环境名天然没有反斜杠（`\begin{mybox}` 里 `mybox` 就是裸名），所以这三项直接写 `mybox` 即可，无需关心「去 `\`」问题。

至于 `followPackageLinks`，它的判定更简单（示例代码）：

```text
function discoverProject():
    ... 沿用 u1-l2 的 Discovery 算法 ...
    if config.followPackageLinks == true:
        对 \usepackage / \RequirePackage 引入的自定义宏包，
        递归解析其内容并并入依赖图   # 默认 false：不进入宏包内部
```

#### 4.1.3 源码精读

先看数学环境扩展 [Configuration.md:L433-L439](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L433-L439)，它允许追加被视作数学环境（如 `align*`、`equation`）的环境名单，类型 `string[]`，默认 `[]`：

> Allows extending the list of environments which the server considers as math environments (for example `align*` or `equation`).

枚举环境扩展 [Configuration.md:L443-L449](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L443-L449) 完全对称，追加枚举环境（如 `enumerate`、`itemize`）：

> Allows extending the list of environments which the server considers as enumeration environments (for example `enumerate` or `itemize`).

逐字环境扩展 [Configuration.md:L453-L460](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L453-L460) 多了一句关键用途说明——**可用于抑制不含 LaTeX 代码的环境里的诊断**：

> Allows extending the list of environments which the server considers as verbatim environments (for example `minted` or `lstlisting`). **This can be used to suppress diagnostics from environments that do not contain LaTeX code.**

最后看非名单项 `followPackageLinks` [Configuration.md:L423-L429](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L423-L429)：置 `true` 后，自定义宏包的依赖会被解析并纳入依赖图，默认 `false`。它作用的对象正是 [u1-l2](u1-l2-project-detection.md) 里由 `\input`/`\import` 构建的那棵依赖树——开启后这棵树会把宏包内部也展开进去：

> If set to `true`, dependencies of custom packages are resolved and included in the dependency graph.

> 性能提示：`followPackageLinks` 默认关闭是有原因的。解析宏包依赖会显著增大 Discovery 的工作量（宏包可能又依赖别的宏包，递归展开），与 [u2-l2](u2-l2-build-config.md) 里 `build.useFileList` 默认关闭同理——都是「更全面但有性能代价」的开关。

#### 4.1.4 代码实践

**实践目标**：用一个自定义逐字环境 `pycode` 体验「从源头抑制诊断」。

**操作步骤**：

1. 准备一个测试文件 `demo.tex`（**示例代码**），故意在 `pycode` 环境里写一段不是 LaTeX 的内容（看起来像语法错误）：

   ```latex
   \documentclass{article}
   \newenvironment{pycode}{}{}
   \begin{document}
   \begin{pycode}
   def f(x): return x + @#$       % 这不是合法 LaTeX，也不是合法 Python，仅为制造“脏内容”
   \end{pycode}
   \end{document}
   ```

2. 先**不**配置 `verbatimEnvironments`，在支持 texlab 的编辑器里保存，观察诊断面板。
3. 在 texlab 配置里追加（**示例配置**）：

   ```json
   { "texlab.experimental.verbatimEnvironments": ["pycode"] }
   ```

4. 重新保存，再次观察诊断面板。

**需要观察的现象**：

- 第 2 步：texlab 试图按 LaTeX 解析 `pycode` 内部，对 `@#$` 等内容报出若干诊断（如「未定义命令」「非法字符」之类）。
- 第 4 步：把 `pycode` 登记为逐字环境后，texlab 不再分析其内部，上述诊断**消失**。

**预期结果**：登记为 verbatim 后，环境内部的诊断被抑制。具体诊断条目与措辞**待本地验证**（以实际 texlab 版本与编辑器为准）。

#### 4.1.5 小练习与答案

**练习 1**：你已经在用 `align*` 写公式，还需要把它加进 `mathEnvironments` 吗？
**答案**：不需要。`align*` 是 texlab 内置数学环境名单里已经有的（wiki 把它作为「for example」举的例子）。`mathEnvironments` 只用于追加内置名单里**没有**的环境；配置项是做并集，不是替换。

**练习 2**：`verbatimEnvironments` 与 [u5-l1](u5-l1-diagnostics-chktex.md) 的 `diagnostics.ignoredPatterns` 都能让某段内容不报诊断，二者有何本质区别？
**答案**：`verbatimEnvironments` 是**源头抑制**——让 texlab 根本不把该环境当作 LaTeX 解析；`ignoredPatterns` 是**推送前过滤**——诊断已经产生，在发给客户端前按正则剔除。前者作用于「整个环境的解析方式」，后者作用于「单条诊断的文本匹配」。

---

### 4.2 自定义命令扩展：citationCommands / labelDefinitionCommands / labelReferenceCommands / labelReferenceRangeCommands / glossaryReferenceCommands

#### 4.2.1 概念说明

和环境名单一样，texlab 内部也维护着「**命令名单**」：哪些命令算引用命令、哪些算标签定义命令、哪些算术语命令……这些名单决定了 texlab 能否把「某个命令的参数」理解成「一个标签 / 一篇文献 / 一个术语」，进而提供跳转、补全、引用检查等功能。

LaTeX 宏包众多，常会引入与内置命令**同类但不同名**的命令：

| 内置代表命令 | 同类的常见宏包命令 | 对应扩展项 |
|---|---|---|
| `\cite` | `\parencite`、`\footcite`、`\autocite`（biblatex） | `citationCommands` |
| `\label` | `\newcommand{\thm}[1]{\label{thm:#1}}` 等包装命令 | `labelDefinitionCommands` |
| `\ref` | `\eqref`、`\autoref`、`\cref`（cleveref） | `labelReferenceCommands` |
| `\crefrange` | `\crefrange`（cleveref 的范围引用） | `labelReferenceRangeCommands` |
| `\gls` | `\Gls`、`\glspl`（glossaries） | `glossaryReferenceCommands` |

`experimental.*Commands` 这五项就是让你把自己的命令追加进对应名单。

这里有一条**贯穿五项的铁律**：登记命令时要写它的**裸名**，**不带前导 `\`**。也就是说，要让 texlab 认识 `\mycite`，配置里要写 `"mycite"`，而不是 `"\mycite"`。这是本模块最核心的约定，wiki 在每一项后面都用 _Hint_ 反复强调。

#### 4.2.2 核心流程

五项命令扩展的运作方式也一致：**把裸名并入内置名单，解析时把命令参数按其语义处理**。伪代码（示例代码）：

```text
function classifyCommand(name):                # name 已去掉前导 '\'
    if name in (builtin.citationCommands            ∪ config.citationCommands):
        return CITATION              # 参数视为“文献键”，提供 \cite 补全/跳转
    if name in (builtin.labelDefinitionCommands    ∪ config.labelDefinitionCommands):
        return LABEL_DEF             # 参数视为“定义的标签”，加入标签集合
    if name in (builtin.labelReferenceCommands     ∪ config.labelReferenceCommands):
        return LABEL_REF             # 参数视为“被引用的标签”，检查是否已定义
    if name in (builtin.labelReferenceRangeCommands ∪ config.labelReferenceRangeCommands):
        return LABEL_REF_RANGE       # 两个参数 a,b 视为“a 到 b 的范围引用”
    if name in (builtin.glossaryReferenceCommands  ∪ config.glossaryReferenceCommands):
        return GLOSSARY              # 参数视为“术语键”
    ...
```

两条要点：

1. **同样做并集**：你的名单是追加项，内置的 `\cite`/`\label`/`\ref`/`\gls` 等依然有效。
2. **去 `\` 是硬性要求**：如果你不小心写成 `"\mycite"`，texlab 会把它当作字面字符串 `\mycite` 去匹配，而解析器提取出来的命令名是 `mycite`（不含 `\`），于是**永远匹配不上**——功能静默失效。这是「配了却没反应」时最该先排查的点。

注意：`\crefrange` 之所以单列一项 `labelReferenceRangeCommands`，是因为它带**两个**参数（起、止），语义不同于单参数的 `\ref`；texlab 需要单独知道「这两个参数都是标签」。

#### 4.2.3 源码精读

引用命令扩展 [Configuration.md:L464-L472](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L464-L472)，以 `\cite` 为例，并给出「无 `\`」约定：

> Allows extending the list of commands which the server considers as citation commands (for example `\cite`).
> _Hint:_ Additional commands need to be written **without** a leading `\` (e. g. `foo` instead of `\foo`).

标签定义命令扩展 [Configuration.md:L476-L484](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L476-L484)，扩展 `\label` 类命令（同样无 `\`）：

> Allows extending the list of `\label`-like commands.

标签引用命令扩展 [Configuration.md:L488-L496](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L488-L496)，扩展 `\ref` 类命令：

> Allows extending the list of `\ref`-like commands.

范围引用命令扩展 [Configuration.md:L500-L508](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L500-L508)，扩展 `\crefrange` 类命令（注意它带两个标签参数）：

> Allows extending the list of `\crefrange`-like commands.

术语引用命令扩展 [Configuration.md:L542-L550](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L542-L550)，扩展 `\gls` 类命令：

> Allows extending the list of `\gls`-like commands.

读这五节时，请抓住两个共同点：其一，类型都是 `string[]`、默认 `[]`；其二，每一项的 _Hint_ 都在重复「**without a leading `\`**」。这条约定如此重要，以至于 wiki 用斜体强调了一遍又一遍。

> 补充：标签类命令登记后，产生的标签在补全与符号面板里以何种图标呈现？查 [LSP-Internals.md 的枚举映射表](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L116-L129)（LSP-Internals.md:116-129）可知，**Label** 结构被映射为 `CompletionItemKind = Constructor(4)`、`SymbolKind = Constructor(9)`（见第 128 行）。也就是说，无论标签是 `\label` 还是你的自定义命令产生的，它在编辑器里都以「Constructor」类图标出现。

#### 4.2.4 代码实践

**实践目标**：让 biblatex 的 `\parencite` 被 texlab 当作引用命令，从而获得与 `\cite` 相同的文献补全。

**操作步骤**：

1. 准备 `main.tex` 与 `refs.bib`（**示例代码**）：

   ```latex
   \documentclass{article}
   \usepackage[backend=biber]{biblatex}
   \addbibresource{refs.bib}
   \begin{document}
   See \parencite{knuth1984}.        % biblatex 风格的引用
   \end{document}
   ```

   ```bibtex
   @article{knuth1984,
     author = {Donald E. Knuth},
     title  = {Literate Programming},
     year   = {1984}
   }
   ```

2. 在 texlab 配置里登记（**示例配置**）：

   ```json
   { "texlab.experimental.citationCommands": ["parencite"] }
   ```

   注意写的是 `parencite`，**不带 `\`**。

3. 在编辑器里把光标放到 `\parencite{` 之后，触发补全，观察是否出现 `knuth1984` 候选。
4. 故意写错成 `"\parencite"`（带 `\`），重复第 3 步，对比补全是否失效。

**需要观察的现象**：

- 第 3 步：补全列表里出现 `knuth1984`，说明 texlab 已把 `\parencite` 视为引用命令、把其参数当文献键处理。
- 第 4 步：写成 `"\parencite"` 后补全不再出现候选——印证「带 `\` 则匹配失败」。

**预期结果**：正确登记后 `\parencite` 获得文献补全；带 `\` 的错误写法静默失效。候选是否出现**待本地验证**（需编辑器/客户端已正确把配置透传给 texlab）。

#### 4.2.5 小练习与答案

**练习 1**：用户配置了 `"texlab.experimental.labelReferenceCommands": ["\eqref"]`，结果 `\eqref{eq:foo}` 仍然无法补全标签，为什么？
**答案**：因为带了前导 `\`。wiki 要求写裸名，应改为 `["eqref"]`。带 `\` 的字符串永远匹配不上解析器提取出来的命令名 `eqref`。

**练习 2**：`\crefrange` 为什么不放进 `labelReferenceCommands`，而要单独有 `labelReferenceRangeCommands`？
**答案**：`\crefrange{a}{b}` 带**两个**标签参数（起、止），语义是「从 a 到 b 的范围引用」，与单参数的 `\ref` 不同。texlab 需要专门的名单来知道「这两个参数都是标签」，因此单独成项。

**练习 3**：登记命令时，配置项与内置名单是什么关系？
**答案**：是**并集**。你的名单是追加项，内置的 `\cite`/`\label`/`\ref`/`\gls` 等依然有效，不会被覆盖。默认空数组 `[]` 表示「只用内置名单」。

---

### 4.3 前缀关联（Prefixes）：让自定义命令产生的带前缀标签被识别

#### 4.3.1 概念说明

4.2 解决了「让 texlab 认识新命令」。但有一种常见写法会让 4.2 的能力**不够用**，那就是**包装命令**。考虑这样的定理环境快捷命令（wiki 原例）：

```latex
\newcommand{\thm}[1]{\label{thm:#1}}
% ... 后文 ...
\thm{foo}
```

这里 `\thm` 是用户自定义的命令，它内部调用 `\label{thm:foo}`——也就是说，写 `\thm{foo}` 等价于打了一个名为 `thm:foo` 的标签。`thm:` 就是这个标签的**前缀**，用来标识「这是一条定理标签」。

问题来了：texlab 怎么知道「`\thm{foo}` 实际定义了标签 `thm:foo`」？它**无法**从 `\newcommand` 的定义里自动推理这一点（宏展开是 LaTeX 的事，不归语言服务器静态分析）。如果你只把 `thm` 加进 `labelDefinitionCommands`（4.2 的做法），texlab 会认为 `\thm{foo}` 定义了一个名为 `foo` 的标签（把参数原样当标签名），而**不是** `thm:foo`——于是后文 `\ref{thm:foo}` 会因为找不到对应定义而被报为「未定义引用」。

`labelDefinitionPrefixes` 就是为解决这个问题而设：它把「**命令名 ↔ 前缀**」显式关联起来，告诉 texlab「当 `\thm` 出现时，把它参数生成的标签前面拼上 `thm:`」。`labelReferencePrefixes` 是引用侧的对应物（当你也用自定义命令去**引用**带前缀标签时）。

#### 4.3.2 核心流程

前缀关联本质上是一道**字符串拼接**规则。设你登记了一个前缀对 `(cmd, prefix)`，当 texlab 在源码里遇到命令 `cmd` 且其参数为 `arg` 时，它合成的标签为：

\[
\text{label} \;=\; \text{prefix} \;\oplus\; \text{arg}
\]

其中 \(\oplus\) 表示字符串拼接。对 wiki 的 `\thm` 例子：

\[
\text{label} \;=\; \texttt{"thm:"} \;\oplus\; \texttt{"foo"} \;=\; \texttt{"thm:foo"}
\]

完整的「让 `thm:foo` 被识别」需要**两步配置同时到位**（缺一不可）：

```text
function recognizeThmLabel():
    # 第一步：把 thm 登记为“标签定义命令”（4.2 的能力）
    config.labelDefinitionCommands ∋ "thm"

    # 第二步：登记 (thm, "thm:") 前缀对（本模块的能力）
    config.labelDefinitionPrefixes ∋ ("thm", "thm:")

    # 解析 \thm{foo} 时：
    label = "thm:" ⊕ "foo" = "thm:foo"      # 于是 thm:foo 被加入标签集合
```

为什么两步缺一不可：

- **只做第一步**（只登记命令、不登记前缀）：texlab 把 `\thm{foo}` 的参数原样当标签名，得到 `foo` 而非 `thm:foo`，标签集合里没有 `thm:foo`。
- **只做第二步**（只登记前缀对、不登记命令）：texlab 根本不认为 `\thm` 是标签定义命令，前缀对无从触发。

只有两者都配置，texlab 才会把 `\thm{foo}` 正确解析为标签 `thm:foo`。此后，你在别处写 `\ref{thm:foo}`（或带前缀的自定义引用命令）时，texlab 就能匹配上、不再报未定义引用，并在补全里给出 `thm:foo` 候选。

`labelReferencePrefixes` 是引用侧的对称配置：当你用一个**自定义命令**去引用带前缀标签时（例如 `\newcommand{\thmref}[1]{\ref{thm:#1}}`，写 `\thmref{foo}` 等价于 `\ref{thm:foo}`），用它把 `(thmref, "thm:")` 关联起来，texlab 才知道 `\thmref{foo}` 引用的是 `thm:foo`。

> 配置形态：两项的类型都是 `(string, string)[]`——即「**二元组数组**」，每个元素是一对 `[命令名, 前缀]`。例如 `[["thm", "thm:"], ["lem", "lem:"]]` 表示同时关联 `\thm→thm:` 与 `\lem→lem:` 两个命令。命令名同样**不带前导 `\`**。

#### 4.3.3 源码精读

`labelDefinitionPrefixes` 见 [Configuration.md:L512-L527](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L512-L527)，wiki 直接用 `\thm` 这个例子说明它的用法，并把两步配置的关系讲得非常清楚：

> Allows associating a label definition command with a custom prefix. Consider,
> ```tex
> \newcommand{\thm}[1]{\label{thm:#1}}
> \thm{foo}
> ```
> Then setting `texlab.experimental.labelDefinitionPrefixes` to `[["thm", "thm:"]]` **and adding "thm" to `texlab.experimental.labelDefinitionCommands`** will make the server recognize the `thm:foo` label.

请特别注意这句里的 **and**（粗体为本讲所加）——它明确点出「两步都要做」。类型为 `(string, string)[]`，默认 `[]`。

`labelReferencePrefixes` 见 [Configuration.md:L531-L538](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L531-L538)，是引用侧的对称项，wiki 让你回看 `labelDefinitionPrefixes` 的例子即可理解：

> Allows associating a label reference command with a custom prefix. See `texlab.experimental.labelDefinitionPrefixes` for more details.

读这两节要建立的认知是：**前缀关联是建立在命令登记之上的「增强」**——它本身不声明「这是标签命令」，只是给已经登记的标签命令附加「拼前缀」的规则。因此它总是与 `labelDefinitionCommands` / `labelReferenceCommands` 配套使用。

#### 4.3.4 代码实践

**实践目标**：复刻 wiki 的 `\thm` 例子，让 texlab 识别 `thm:foo` 标签，并用补全/引用验证它可被引用。

**操作步骤**：

1. 准备 `main.tex`（**示例代码**），定义 `\thm` 命令并在后文引用它产生的标签：

   ```latex
   \documentclass{article}
   \newcommand{\thm}[1]{\label{thm:#1}}
   \begin{document}
   \begin{enumerate}
     \item Pythagoras. \thm{foo}     % 等价于 \label{thm:foo}
   \end{enumerate}
   See Theorem~\ref{thm:foo}.        % 引用 thm:foo
   \end{document}
   ```

2. 写出**最小 texlab 配置**（**示例配置**），两步缺一不可：

   ```json
   {
     "texlab.experimental.labelDefinitionCommands": ["thm"],
     "texlab.experimental.labelDefinitionPrefixes": [["thm", "thm:"]]
   }
   ```

3. 在编辑器里把光标放到 `\ref{` 之后，触发补全，观察是否出现 `thm:foo` 候选。
4. （对照实验）删掉 `labelDefinitionPrefixes` 那一行（只保留 `labelDefinitionCommands`），重复第 3 步，观察候选变成什么。

**需要观察的现象**：

- 第 3 步：补全列表里出现 `thm:foo`，且 `\ref{thm:foo}` 不被报为「未定义引用」——说明 `\thm{foo}` 被正确解析为标签 `thm:foo`。
- 第 4 步：去掉前缀对后，texlab 把 `\thm{foo}` 的参数原样当标签名，候选里出现的是 `foo`（而非 `thm:foo`），而 `\ref{thm:foo}` 会因找不到定义被报未定义引用。

**预期结果**：两步配置都到位时，`thm:foo` 标签可被补全与引用；缺少前缀对时退化为 `foo`。具体补全候选与诊断措辞**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：你同时把 `thm` 加进了 `labelDefinitionCommands`，并把 `["thm","thm:"]` 加进了 `labelDefinitionPrefixes`，但 `\ref{thm:foo}` 仍被报未定义引用。可能的错误写法有哪些？
**答案**：常见有三种误写：① 命令名带了 `\`（写成 `"thm"` 才对，写成 `"\thm"` 则匹配不上）；② 只配了前缀对、忘了把 `thm` 加进 `labelDefinitionCommands`（wiki 用「and」强调两步都要做）；③ 前缀写错（如写成 `"thm"` 漏了冒号，应为 `"thm:"`，否则拼出来是 `thmfoo` 而非 `thm:foo`）。

**练习 2**：`labelDefinitionPrefixes` 的类型 `(string, string)[]` 是什么意思？举一个同时关联两个命令的配置。
**答案**：表示「二元组数组」，每个元素是一对 `[命令名, 前缀]`。同时关联 `\thm→thm:` 和 `\lem→lem:` 写作：`"texlab.experimental.labelDefinitionPrefixes": [["thm", "thm:"], ["lem", "lem:"]]`，并且还要把 `"thm"`、`"lem"` 都加进 `labelDefinitionCommands`。

**练习 3**：为什么需要单独的 `labelReferencePrefixes`，而不能复用 `labelDefinitionPrefixes`？
**答案**：定义侧和引用侧是**不同的命令**。`\thm{foo}` 定义标签，`\thmref{foo}`（如果你这么包装）引用标签，二者是两个不同的命令名，需要各自登记命令 + 各自登记前缀对。`labelDefinitionPrefixes` 管定义命令，`labelReferencePrefixes` 管引用命令，各管一侧。

---

## 5. 综合实践

把本讲三个模块串起来，为一个「带自定义定理命令、自定义引用命令、自定义逐字代码块」的小工程做一次完整的扩展点配置。

**任务**：准备如下 `main.tex`（**示例代码**），它同时用到了本讲三个模块的能力：

```latex
\documentclass{article}
\usepackage[backend=biber]{biblatex}
\addbibresource{refs.bib}

% 自定义命令（涉及模块 4.2、4.3）
\newcommand{\thm}[1]{\label{thm:#1}}          % 定义带前缀的定理标签
\newcommand{\mycite}[2][]{\cite{#2}}          % 自定义引用命令（包装 \cite）

\newenvironment{pycode}{}{}                    % 自定义逐字环境（涉及模块 4.1）

\begin{document}
\section{Proof}\label{sec:proof}
\thm{main}                                     % 等价于 \label{thm:main}
See Theorem~\ref{thm:main} and \mycite{knuth1984}.

\begin{pycode}
def f(x): return x + @#$                       % 非 LaTeX 内容
\end{pycode}
\end{document}
```

要求你写出一份 texlab 配置，达成三件事：

1. **模块 4.1**：把 `pycode` 登记为逐字环境，使其中那段「脏内容」不产生诊断。
2. **模块 4.2**：把 `mycite` 登记为引用命令，使 `\mycite{knuth1984}` 能获得文献补全。
3. **模块 4.3**：让 `\thm{main}` 被正确解析为标签 `thm:main`，使 `\ref{thm:main}` 不报未定义引用、并在补全里出现。

参考配置（**示例配置**）：

```json
{
  "texlab.experimental.verbatimEnvironments": ["pycode"],
  "texlab.experimental.citationCommands": ["mycite"],
  "texlab.experimental.labelDefinitionCommands": ["thm"],
  "texlab.experimental.labelDefinitionPrefixes": [["thm", "thm:"]]
}
```

**验收点**：

- `pycode` 内部不再有诊断（逐字环境抑制）。
- 在 `\mycite{` 后触发补全，出现 `knuth1984`（自定义引用命令生效）。
- 在 `\ref{` 后触发补全，出现 `thm:main`（前缀关联生效）；`\ref{thm:main}` 不被报未定义引用。
- 能用一句话说清三个模块各自的「登记对象」：4.1 登记环境名、4.2 登记命令裸名（无 `\`）、4.3 在命令登记之上追加「命令↔前缀」二元组。

> 进阶：再为引用侧也做一遍包装——定义 `\newcommand{\thmref}[1]{\ref{thm:#1}}`，用 `labelReferenceCommands` 登记 `thmref`、用 `labelReferencePrefixes` 登记 `["thmref","thm:"]`，验证 `\thmref{main}` 同样能正确解析为引用 `thm:main`。

## 6. 本讲小结

- `texlab.experimental.*` 是一组**可扩展点**，让 texlab 在内置名单之上认识你自定义的环境与命令；除 `followPackageLinks` 外，各项都是「名单」，默认空数组 `[]`，配置是做**并集**而非替换。
- **自定义环境扩展**（`mathEnvironments` / `enumEnvironments` / `verbatimEnvironments`）：追加三类环境名单；其中 `verbatimEnvironments` 能从源头让 texlab 不分析某环境内部，从而**抑制诊断**。`followPackageLinks`（默认 `false`）则开启对自定义宏包依赖的解析、把它纳入 u1-l2 的依赖图。
- **自定义命令扩展**（`citationCommands` / `labelDefinitionCommands` / `labelReferenceCommands` / `labelReferenceRangeCommands` / `glossaryReferenceCommands`）：追加五类命令名单；铁律是**命令写裸名、不带前导 `\`**，带 `\` 会静默失效。
- **前缀关联**（`labelDefinitionPrefixes` / `labelReferencePrefixes`）：类型 `(string, string)[]`，把「命令名 ↔ 标签前缀」关联起来；它必须与对应的 `*Commands` **配套**（两步缺一不可），才能让 `\newcommand{\thm}[1]{\label{thm:#1}}` 这类包装命令产生的 `thm:foo` 标签被正确识别，本质是一道 `prefix ⊕ arg` 的字符串拼接。
- 这些扩展点都不使用 `%f`/`%p`/`%l` 占位符（它们不调用外部命令，只改 texlab 内部的结构识别名单），与 u5-l2 的「呈现配置」正交：本讲决定 texlab「认识哪些结构」，u5-l2 决定「认识到的结构怎么显示」。

## 7. 下一步学习建议

- 本讲是 [u5 高级配置](u5-l1-diagnostics-chktex.md) 的收尾。如果你是按顺序读下来的，现在已读完整套手册的「配置」主线。建议回头把 u5 的四篇（u5-l1 诊断、u5-l2 符号/补全/悬停、u5-l3 格式化、本讲扩展点）对照一遍，体会它们分别对应 texlab 的哪个子系统。
- 本讲的「命令登记 + 前缀关联」与 [u4-l2 workspace 命令](u4-l2-workspace-commands.md) 里的 `showDependencyGraph` 可以结合使用：用扩展点让 texlab 认识更多 `\input`/自定义结构后，调用 `texlab.showDependencyGraph` 渲染 DOT 图，能直观看到你的扩展是否被纳入了依赖树。
- 若你要为**新编辑器写 texlab 客户端**，可回看 [u4-l1 自定义 LSP 消息](u4-l1-custom-lsp-messages.md) 与 [u4-l3 枚举映射](u4-l3-enum-mapping.md)：本讲让 texlab 识别出的标签，最终会以 `Label → Constructor(4)/(9)` 的映射出现在补全与符号面板里——扩展点（认识结构）与枚举映射（呈现结构）是同一条链路的两个环节。
- texlab 的实际解析逻辑在 texlab 主仓库（Rust 实现）而非本 wiki。想确认某项扩展的精确行为（例如前缀拼接是否对多参数命令也生效）的读者，建议在主仓库源码中检索对应的配置项名称做交叉验证。
