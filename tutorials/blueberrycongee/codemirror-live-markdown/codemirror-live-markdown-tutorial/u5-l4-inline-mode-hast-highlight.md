# inline 模式：保留 contentDOM 的可编辑高亮

## 1. 本讲目标

本讲是代码块子系统（专家级单元）的收尾篇。前两讲我们分别讲了三种交互模式的决策层（u5-l2）和 `auto`/`toggle` 模式的渲染 Widget（u5-l3）。本讲专攻第三种、也是最特殊的一种——`inline` 模式。

学完本讲，你应当能够：

- 说清 `inline` 模式与 `auto`/`toggle` 模式在「代码文本到底存放在哪里」上的根本区别。
- 读懂 `hastToMarkDecorations` 如何把 lowlight 的 HAST 树递归转成 `Decoration.mark` 区间，并理解其中的偏移量追踪。
- 理解为什么 `inline` 模式需要额外挂载 `codeBlockSelectionPlugin` 来处理选区高亮，以及它如何绕过 `drawSelection` 的层级问题。
- 建立「innerHTML 渲染路线」与「HAST→mark 装饰路线」的两条高亮链路对照。

## 2. 前置知识

本讲默认你已掌握前三讲建立的概念，这里只做最简承接，不展开：

- **u5-l1**：lowlight 把源码解析成 **HAST 树**（节点类型 `root`/`element`/`text`，`element` 带 `properties.className`）；`highlightCode` 返回 HTML 字符串，`highlightCodeHast` 返回原始 HAST 树——本讲正是后者的用武之地。
- **u5-l2**：三种 `interaction` 模式中，`toggle`/`inline` 都由显式按钮（`setCodeBlockSourceMode` Effect + `codeBlockSourceModeField`）驱动源码态，**不响应光标选区**；只有 `auto` 跟光标走。`inline` 的装饰由 `buildCodeBlockInlineDecorations` 专门构建，与 `auto`/`toggle` 共用的 `buildCodeBlockDecorations` 是两条独立分支。
- **u5-l3**：`auto`/`toggle` 模式用 `CodeBlockWidget.toDOM` 通过 `highlightCode` 得到 HTML，再 `codeEl.innerHTML` 写入 Widget 自有 DOM——代码文本是**渲染副本**。
- **CodeMirror 基础**（u2-l1/u3-l1）：`Decoration.mark` 给真实文本叠加 CSS 类但不替换文本；`Decoration.replace({widget, block})` 整块替换原文。`contentDOM` 是 CodeMirror 持有真实可编辑文档文本的容器。

一个贯穿本讲的关键直觉：**`auto`/`toggle` 把代码「拍成一张高亮截图」（Widget DOM 里的副本），`inline` 则把高亮当成「蒙在真实文本上的彩色滤镜」（mark 装饰）**。截图好看但不能直接改，滤镜之上你可以随手打字。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| `src/plugins/codeBlock.ts` | `buildCodeBlockInlineDecorations`（inline 装饰策略）、`hastToMarkDecorations`（HAST→mark）、`CodeBlockHeaderWidget`/`CodeBlockFooterWidget`（围栏行 Widget）、`codeBlockSelectionPlugin`（选区高亮）、`codeBlockField` 出口处按模式挂载扩展 |
| `src/utils/codeHighlight.ts` | `highlightCodeHast`（返回原始 HAST 树）与 `highlightCode`（返回 HTML，用于对照） |
| `src/widgets/codeBlockWidget.ts` | `CodeBlockWidget.toDOM` 的 `innerHTML` 路线（auto/toggle），作为对照 |
| `demo/main.ts` | `codeBlockField({ interaction: 'inline' })` 的真实接线与示例文档 |

## 4. 核心概念与源码讲解

### 4.1 inline 装饰策略：只换围栏，留下正文

#### 4.1.1 概念说明

回忆 u5-l3：`auto`/`toggle` 模式下，渲染态用一条 `Decoration.replace({ widget, block: true }).range(node.from, node.to)` 把**整个 `FencedCode` 节点**（开头围栏 + 代码正文 + 结尾围栏）整体替换成一个 Widget。代码正文只是 Widget DOM 里通过 `innerHTML` 写进去的**副本**，光标无法落进去直接编辑——你要么点击让光标跳到源码（`auto`），要么点 MD 按钮切到源码态（`toggle`）。

`inline` 模式换了个思路：它**只替换两行围栏**（开头的 ` ```js ` 和结尾的 ` ``` `），把中间的代码正文原封不动地留在 `contentDOM` 里作为真实可编辑文本。于是你可以像编辑普通段落一样，直接点进代码里打字，CodeMirror 原生处理光标、输入、撤销；语法高亮则作为 `Decoration.mark` 蒙在这些真实文本之上，随你输入实时刷新。

这正是源码文件头部注释对三种模式的概括：

- `auto`：默认渲染 Widget，光标进入时切源码。
- `toggle`：默认渲染 Widget，需点按钮切源码。
- `inline`：**只替换围栏行，代码正文留在 contentDOM 以便原生编辑**。

#### 4.1.2 核心流程

`inline` 模式渲染态（`showSource === false`）对每个 `FencedCode` 节点产出**四类装饰**，分工明确：

```
FencedCode 节点
├─ 开头围栏行 `````lang`````  → replace + CodeBlockHeaderWidget (block, inclusiveEnd)
├─ 正文第 1 行               → Decoration.line(cm-codeblock-content)
│      └─ + 若干 Decoration.mark(hljs-*)  ← 来自 HAST
├─ 正文第 2 行               → Decoration.line(cm-codeblock-content)
│      └─ + 若干 Decoration.mark(hljs-*)
├─ ...                       → 同上
└─ 结尾围栏行 ````````        → replace + CodeBlockFooterWidget (block, inclusiveStart)
```

源码态（`showSource === true`，由 MD 按钮触发）则与 `toggle` 的源码态一致：插入一个切换按钮 Widget + 给每一行加 `cm-codeblock-source` 类。

注意 `inline` 模式判定 `showSource` 用的是按钮驱动的 `isCodeBlockInSourceMode(sourceRanges, ...)`，**完全不调用 `shouldShowSource`、也不读 `mouseSelectingField`**——这印证了 u5-l2 的结论：inline 的显隐与光标、拖拽无关。

#### 4.1.3 源码精读

整段渲染态构建逻辑在 `buildCodeBlockInlineDecorations` 的 `else` 分支里：

[源码：buildCodeBlockInlineDecorations 渲染态四步装饰](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L347-L407)

四个步骤逐一对照：

1. **替换开头围栏行**（L356–L369）：构造 `CodeBlockHeaderWidget`，用 `Decoration.replace({ widget, block: true, inclusiveEnd: true }).range(openFenceLine.from, openFenceLine.to)` 只替换开头那一行。`inclusiveEnd: true` 的作用是禁止光标落在「header 与第一行正文」之间的缝隙，避免出现一个无法定位的空位置。
2. **正文行的行级样式**（L371–L381）：遍历开头围栏行号与结尾围栏行号之间的每一行，各 push 一条 `Decoration.line({ class: 'cm-codeblock-content' }).range(line.from)`。注意 `Decoration.line` 只需要一个定位点（`line.from`），不带 `from/to` 区间。
3. **高亮 mark 装饰**（L383–L394）：调用 `highlightCodeHast(code, language)` 得到 HAST 树，再经 `hastToMarkDecorations(hast, codeText.from)` 转成 mark 区间，逐条做边界校验 `mark.from >= 0 && mark.to <= state.doc.length` 后推入。这一步是本讲 4.2 的主角。
4. **替换结尾围栏行**（L399–L406）：构造 `CodeBlockFooterWidget`，用 `Decoration.replace({ widget, block: true, inclusiveStart: true })`，`inclusiveStart: true` 同理封住「最后一行正文与 footer」之间的缝隙。

围栏行的两个 Widget 都很轻量。`CodeBlockHeaderWidget` 渲染语言徽标 + Copy 按钮 + MD 按钮：

[源码：CodeBlockHeaderWidget 的 toDOM 与 eq](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L127-L218)

两个细节值得留意：其一，`eq`（L138–L146）比较 `language/code/from/to/showCopyButton`——只要代码正文没变就复用旧 DOM；其二，`ignoreEvent` 恒返回 `true`（L215–L217），意味着 CodeMirror 对 header 内的所有事件都不插手，完全交给 Widget 自己的按钮监听器处理（这与 `MathWidget` 固定返回 `false` 正好相反，因为 header 上有需要响应的按钮）。MD 按钮点击会派发 `setCodeBlockSourceMode.of({ from, to, showSource: true })` 切到源码态（L200–L209）。

`CodeBlockFooterWidget` 更简单，`eq` 恒返回 `true`（纯静态视觉元素）：

[源码：CodeBlockFooterWidget](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L224-L238)

最后看更新策略。`createCodeBlockField` 的 `update` 里，`inline`（以及 `toggle`）在文档未变、配置未重置、未收到 `setCodeBlockSourceMode` 时，会因 `options.interaction !== 'auto'` 直接返回旧装饰，**不响应选区变化**：

[源码：inline/toggle 不因选区变化重建](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L528-L541)

但请注意：虽然装饰集合本身不因选区重建，你输入的每一个字符（`docChanged`）都会触发重建——这正是 demo 所宣称「语法高亮随输入实时刷新」的由来：每次输入后重跑 `highlightCodeHast` + `hastToMarkDecorations`，把新的 mark 蒙在新文本上。

#### 4.1.4 代码实践：在 demo 中观察 inline 与 auto 的编辑差异

1. **实践目标**：直观感受「代码正文是真实可编辑文本」与「代码正文是渲染副本」的区别。
2. **操作步骤**：
   - 进入 `demo/` 目录运行 `npm run dev`，浏览器打开页面（参考 u1-l3）。
   - 找到标注 **Code Block Inline Editing** 的编辑器（对应 `demo/main.ts` 中 `codeBlockField({ interaction: 'inline', copyButton: true })`）。
   - 直接用鼠标点击代码 `function sum(a, b) {` 内部的某个字符，观察光标是否立刻落在该字符上，并尝试输入字符。
   - 再切换到 **Code Block Auto Preview** 编辑器（`codeBlockField()`，auto 模式），在渲染态下点击代码内部，观察行为差异；然后移动光标进入代码块，观察切到源码态的过程。
3. **需要观察的现象**：
   - inline 编辑器：点击即定位、即输入，高亮随输入实时变化，无需「先进入再编辑」。
   - auto 编辑器：渲染态下点击会跳转光标到源码位置（u5-l3 讲过的 Canvas 定位），需光标进入后才显示原始文本可编辑。
4. **预期结果**：inline 模式下你感受到的是「在一个带语法高亮的普通文本区里打字」；auto 模式下则是「在一堵高亮墙与源码之间来回切换」。
5. 现象背后的代码依据见 4.1.3 的四步装饰——inline 没有用任何 Widget 覆盖正文行，所以点击穿透到 `contentDOM` 的真实文本。

#### 4.1.5 小练习与答案

**练习 1**：`buildCodeBlockInlineDecorations` 里替换开头围栏用了 `inclusiveEnd: true`，替换结尾围栏用了 `inclusiveStart: true`。如果两者都省略，会出现什么体验问题？

**参考答案**：省略后，光标有可能停在「header Widget 与第一行正文」或「最后一行正文与 footer Widget」之间的空隙位置——这是一个既不属于 Widget、又不落在可见正文上的「幽灵位」，用户会看到光标消失或落在一个无法编辑的空当。`inclusiveEnd`/`inclusiveStart` 把这两个缝隙封死，强制光标进入正文行。

**练习 2**：为什么 `CodeBlockHeaderWidget.ignoreEvent()` 返回 `true`，而 u3-l1 里 `MathWidget.ignoreEvent()` 返回 `false`？

**参考答案**：header 内部有 Copy、MD 等需要自身处理的按钮，返回 `true` 让 CodeMirror 完全不接管这些事件，避免 CodeMirror 的默认编辑行为干扰按钮点击；而公式 Widget 希望点击后能进入编辑（把焦点交还编辑器），所以返回 `false`。`ignoreEvent` 的取值取决于「Widget 是否想自己吞掉事件」。

---

### 4.2 HAST → mark 装饰：把语法树变成彩色滤镜

#### 4.2.1 概念说明

u5-l1 讲过，`highlightCode` 与 `highlightCodeHast` 是同源的两个函数：底层都调 `lowlightInstance.highlight(lang, code)`，区别在于前者把 HAST 树经 `hastToHtml` 拍成 HTML 字符串（服务 `innerHTML`），后者**直接返回原始 HAST 树**。

为什么 `inline` 模式必须走 HAST、不能用 HTML？因为 `inline` 的代码正文是 `contentDOM` 里的**真实文本**，不能用 `innerHTML` 覆盖（那会把它变成 Widget 自有节点、失去可编辑性）。CodeMirror 提供「不替换文本、只叠加样式」的 `Decoration.mark`——我们只需知道「文档里的哪一段字符该涂哪个 CSS 类」。HAST 树恰好携带了「这段字符属于什么语法类」的信息，只需把它翻译成 mark 区间即可。

#### 4.2.2 核心流程

`hastToMarkDecorations(root, basePos)` 的任务：给定一棵 HAST 树和正文在文档中的起始位置 `basePos`（即 `codeText.from`），按字符顺序遍历，为每个带类名的文本片段产出一条 `Decoration.mark`。

关键在于维护一个**字符偏移量 `offset`**，它记录「已经走过多少个字符」。遍历规则：

- 遇到 `text` 节点（真正的字符）：若当前有累计类名 `cls` 且文本长度 `len > 0`，产出一条覆盖 \([basePos+offset,\ basePos+offset+len]\) 的 mark；然后偏移量前进：

\[
offset \leftarrow offset + len
\]

- 遇到 `element`/`root` 节点（结构容器，自身不含字符）：计算本节点的类名 `nodeClass`，取 `effectiveClass = nodeClass || cls`（本节点没类名就继承父节点的类名），然后递归处理所有子节点；**不推进 `offset`**，因为容器节点本身不占字符。

这样，类名会沿着树「向下继承」：例如外层 `hljs` 包着内层 `hljs-keyword` 包着文本 `"function"`，最终 `"function"` 这段字符拿到 `hljs-keyword`（内层覆盖外层）。这些 `.hljs-*` 类正是 `editorTheme` 里定义好的配色（见 `src/theme/default.ts`），所以视觉结果与 `auto` 模式的 `innerHTML` 高亮**完全一致**——两条路线共用同一套 CSS。

#### 4.2.3 源码精读

转换函数本体：

[源码：hastToMarkDecorations 递归遍历 HAST](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L254-L286)

几个要点对照阅读：

- `offset` 只在 `text` 分支里被推进（L272 `offset += len`），`element`/`root` 分支不推进——这与「容器不占字符」一致。
- 类名继承在 L274–L275：`const nodeClass = node.properties?.className?.join(' '); const effectiveClass = nodeClass || cls;`，子节点用 `effectiveClass` 作为它自己的 `cls` 继续递归（L278）。
- mark 区间的起止用 `basePos + offset` 与 `basePos + offset + len`（L267–L268），把 HAST 的相对字符位置映射到文档绝对位置。

HAST 树本身由 `highlightCodeHast` 产出：

[源码：highlightCodeHast 返回原始 HAST 树](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L250-L265)

注意它的优雅降级：空代码、lowlight 不可用（`initLowlightSync()` 返回 `false`）、语言未注册、或 `highlight` 抛错时，都返回 `null`（L251–L264）。于是调用方 `buildCodeBlockInlineDecorations` 里 `if (hast)` 守卫（L386）自然跳过高亮——代码正文仍以纯文本可编辑，只是没有颜色，不会崩库。

作为对照，`auto`/`toggle` 模式走的 `highlightCode` 把同一棵 HAST 树经 `hastToHtml` 拍平成 HTML 字符串：

[源码：highlightCode 返回 HTML（对照路线）](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L167-L222)

最终这段 HTML 被 `CodeBlockWidget.toDOM` 写进 Widget 自有的 `<code>` 元素：

[源码：auto/toggle 用 innerHTML 写入高亮副本](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L234-L270)

这就是「innerHTML 路线」与「HAST→mark 路线」的分叉点：同一个 lowlight 结果，前者变成 Widget 里的静态 HTML 副本，后者变成蒙在真实文本上的 mark 区间。

#### 4.2.4 代码实践：手工推演一次 HAST→mark 的偏移量

1. **实践目标**：通过跟踪 `offset` 的变化，验证 mark 区间与文档位置的对齐。
2. **操作步骤**（源码阅读型）：
   - 设想代码正文为 `a b`（长度 3），`codeText.from = 100`，假设 lowlight 产出 HAST 形如：`root → [element(hljs-keyword, [text("a")]), text(" b")]`（即 `a` 是关键字，` b` 是普通文本）。
   - 按函数逻辑手工推演 `walk` 调用序列与 `offset` 变化：
     - `walk(root, undefined)`：root 无类名，`effectiveClass = undefined`，递归子节点。
     - `walk(element hljs-keyword, undefined)`：`nodeClass = "hljs-keyword"`，`effectiveClass = "hljs-keyword"`，递归其子节点。
     - `walk(text "a", "hljs-keyword")`：`len=1`，产出 `mark(100, 101, class="hljs-keyword")`，`offset = 1`。
     - `walk(text " b", undefined)`：`cls` 为空，不产出 mark，`offset = 1 + 2 = 3`。
3. **需要观察的现象**：只有带类名的文本片段会产出 mark；普通文本只推进 offset 不涂色。
4. **预期结果**：最终得到一条 `Decoration.mark({ class: 'hljs-keyword' }).range(100, 101)`，即文档位置 100–101 的 `a` 被涂成关键字色，其余文本不着色。`basePos + offset` 始终等于「下一个待处理字符的文档位置」。
5. 这个推演说明了一个不变式：遍历结束时 `offset` 应等于代码正文总长度——否则 mark 区间会与真实文本错位。

#### 4.2.5 小练习与答案

**练习 1**：如果某段代码在 HAST 里被嵌套成 `element(hljs) → element(hljs-string) → text("x")`，文本 `"x"` 最终会拿到哪个类？若把内层 `hljs-string` 换成没有 `className` 的 `element` 呢？

**参考答案**：第一种情况，`walk` 进入内层时 `nodeClass = "hljs-string"` 非空，`effectiveClass = "hljs-string"`，`"x"` 拿到 `hljs-string`（内层覆盖外层）。第二种情况，内层无 `className`，`effectiveClass = nodeClass || cls` 回退到父节点的 `hljs`，`"x"` 拿到外层继承下来的 `hljs`。这就是「向下继承、内层优先」的规则。

**练习 2**：`hastToMarkDecorations` 为何要用 `offset` 手动追踪位置，而不能直接用 HAST 节点上的 `from/to` 之类字段？

**参考答案**：因为 lowlight 返回的 HAST 树**只描述结构、不携带源码位置**（不同于 Lezer 语法树节点带 `from/to`）。HAST 节点里只有 `value`（文本内容）和 `children`，要把它映射回文档位置，只能按字符顺序累加长度。所以必须手动维护 `offset`，并以 `basePos`（正文起始）为基准换算。

---

### 4.3 codeBlockSelectionPlugin：让选区在代码块里重新可见

#### 4.3.1 概念说明

`inline` 模式把代码正文留在 `contentDOM`，于是你会自然地用鼠标在代码里拖选文本。但这里藏着一个渲染坑：CodeMirror 的 `drawSelection()` 把原生选区层画在 `.cm-content` **背后**（`z-index: -1`），而 `inline` 的代码正文行有不透明背景（源码态的 `cm-codeblock-source`、以及正文行的底色）；同时 `drawSelection` 还用高优先级 CSS 隐藏了浏览器的原生 `::selection`。两层叠加的结果是——**代码块内的选区高亮会被不透明背景盖住，几乎看不见**。

`codeBlockSelectionPlugin` 就是为解决这个问题而生的：它是一个独立的 `ViewPlugin`，专门给「落在代码块正文范围内」的选区追加 `Decoration.mark({ class: 'cm-codeblock-sel' })`。这些 mark 渲染在 `.cm-line` **内部**（在不透明背景之上），于是选区重新可见，且在 Chromium / WebKit 等不同引擎下表现一致。

源码文件里这段注释把这个动机说得非常清楚：

> drawSelection() renders its selection layer behind .cm-content (z-index:-1). Inline code-block lines have an opaque background that covers this layer... this ViewPlugin adds Decoration.mark spans for selected ranges that overlap code-block content. These spans render INSIDE the .cm-line (above the opaque background), so selection is always visible.

#### 4.3.2 核心流程

`codeBlockSelectionPlugin` 作为一个 `ViewPlugin.fromClass`，维护一份 `decorations`，其 `build` 逻辑：

1. 取主选区 `sel = view.state.selection.main`；若为空选区（`sel.empty`）直接返回 `Decoration.none`。
2. 在选区范围 `[sel.from, sel.to]` 内用 `syntaxTree.iterate` 找 `FencedCode` 节点。
3. 跳过特殊语言（`SKIP_LANGUAGES`，目前含 `math`）。
4. 计算代码块**正文范围**：`contentFrom = openFenceLine.to + 1`（开头围栏行末之后），`contentTo = lastLine.from - 1`（结尾围栏行首之前）。
5. 把正文范围与选区求交：`from = max(sel.from, contentFrom)`，`to = min(sel.to, contentTo)`；若 `from < to`，产出一条 `selMark.range(from, to)`。

更新策略与 `checkUpdateAction` 同构：`docChanged`/`viewportChanged` 重建；选区变化时，若正在拖拽则跳过（交给原生/drawSelection 提供临时反馈），拖拽结束时重建以让选区 mark 追上来。

#### 4.3.3 源码精读

插件类与更新逻辑：

[源码：codeBlockSelectionPlugin 的 update 与 build](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L607-L675)

`build` 中正文范围计算与选区求交是核心（L654–L667）：注意它刻意排除了围栏行（用 `openFenceLine.to + 1` 与 `lastLine.from - 1`），因为围栏行已被 header/footer Widget 替换，不在可选中的真实文本里。

`selMark` 与配套主题：

[源码：selMark 与 codeBlockSelectionTheme](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L605-L605)

[源码：.cm-codeblock-sel 的背景色（带 CSS 变量 fallback）](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L677-L681)

背景色用 `var(--cm-codeblock-selection, rgba(128, 188, 254, 0.4))`——消费者可覆盖 `--cm-codeblock-selection` 自定义，否则用半透明蓝 fallback。

最后，这个插件**只在 `inline` 模式下挂载**：

[源码：codeBlockField 仅在 inline 模式追加选区插件与主题](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L683-L693)

原因很自然：`auto`/`toggle` 的正文在渲染态被 Widget 整块覆盖、本来也不在 `contentDOM` 里直接选中，不存在「选区被背景盖住」的问题；只有 `inline` 把正文留在 `contentDOM` 且带不透明背景，才需要这个补救插件。

> 补充说明（待本地验证）：`cm-codeblock-content`、`cm-codeblock-header`、`cm-codeblock-footer` 这三个类名在 `src/theme/default.ts` 中**没有**对应的样式定义（仓库内全文检索仅命中 `codeBlock.ts` 内的赋值处）。也就是说，inline 模式的正文行底色、header/footer 外观是否「不透明」取决于消费者自行覆盖这些类，或依赖 CodeMirror 默认行样式。本讲关于「不透明背景盖住选区」的论述，是依据源码注释与 `cm-codeblock-source`（主题中有 `backgroundColor`）给出的；在默认主题下精确的视觉表现建议在 demo 中实地确认。

#### 4.3.4 代码实践：观察选区高亮的重建时机

1. **实践目标**：理解 `codeBlockSelectionPlugin` 的更新策略——拖拽中不重建、拖拽结束才追上。
2. **操作步骤**：
   - 在 demo 的 inline 编辑器里，用鼠标在一个代码块正文内拖选多行代码。
   - 在拖拽**进行中**观察选区颜色；松开鼠标后（拖拽结束）再观察。
3. **需要观察的现象**：
   - 拖拽过程中，选区反馈可能由浏览器原生/drawSelection 临时提供（注释里说「native/drawSelection handles transient feedback」）。
   - 松开后，`codeBlockSelectionPlugin` 重建，`cm-codeblock-sel` 的 mark 覆盖到选中范围，选区以稳定的半透明蓝显示。
4. **预期结果**：拖拽中与松开后的选区视觉略有差异，松开后由 `cm-codeblock-sel` 接管。这对应 `update` 中 `wasDragging && !isDragging` 分支（L624–L627）触发的重建。
5. 如果在默认主题下看不出明显差异，可临时在浏览器 DevTools 给 `.cm-codeblock-sel` 加一个显眼的 `background: red`，重复上述操作以放大现象（这是调试手段，非源码修改）。

#### 4.3.5 小练习与答案

**练习 1**：`codeBlockSelectionPlugin` 为什么用 `ViewPlugin` 而不是 `StateField`？

**参考答案**：因为它产出的是**行内 mark 装饰**（`Decoration.mark`），不改变行的垂直结构，属于行内装饰。行内装饰不要求在视口测量前确定，因此可以用随视口重算的 `ViewPlugin`（参见 u2-l1 关于「块级装饰必须用 StateField」的对照）。这与同样产出装饰的 `codeBlockField`（含块级 replace，必须用 StateField）形成对比。

**练习 2**：`build` 里为什么要用 `Math.max(sel.from, contentFrom)` / `Math.min(sel.to, contentTo)` 做一次裁剪，而不是直接用 `selMark.range(sel.from, sel.to)`？

**参考答案**：因为选区可能跨越多段——例如从代码块外的正文一直拖到块内。直接用 `sel.from..sel.to` 会把 mark 蒙到围栏行甚至块外文本上，而围栏行已被 Widget 替换、不存在可选中的真实文本。裁剪到 `[contentFrom, contentTo]` 才能保证 mark 只落在真实可编辑的正文范围内。

---

## 5. 综合实践：inline 与 auto 两种高亮路线全景对照

把本讲三个最小模块串起来，完成下面这张对照表（先自己填，再对照参考答案）。设想同一份代码块：

````markdown
```javascript
const x = 1;
```
````

| 对照维度 | `auto` 模式（`codeBlockField()`） | `inline` 模式（`codeBlockField({ interaction: 'inline' })`） |
| --- | --- | --- |
| 渲染态装饰范围 |  |  |
| 代码正文存放位置 |  |  |
| 高亮实现路径 |  |  |
| 高亮函数 |  |  |
| 渲染态可直接编辑？ |  |  |
| 源码态由谁触发 |  |  |
| 是否调用 `shouldShowSource` |  |  |
| 选区高亮依赖 |  |  |

**参考答案**：

| 对照维度 | `auto` | `inline` |
| --- | --- | --- |
| 渲染态装饰范围 | 整个 `FencedCode` 一条 `replace` | 围栏行 2 条 `replace` + 正文行若干 `line` + 高亮 `mark` |
| 代码正文存放位置 | Widget 自有 DOM（副本） | `contentDOM`（真实文档文本） |
| 高亮实现路径 | `highlightCode` → HTML → `innerHTML` | `highlightCodeHast` → HAST → `hastToMarkDecorations` → `Decoration.mark` |
| 高亮函数 | `highlightCode`（返回 HTML） | `highlightCodeHast`（返回 HAST 树） |
| 渲染态可直接编辑？ | 否（需光标进入切源码态） | 是（点击即编辑，高亮实时刷新） |
| 源码态由谁触发 | 光标进入（`shouldShowSource`） | MD 按钮（`setCodeBlockSourceMode`） |
| 是否调用 `shouldShowSource` | 是 | 否（用 `isCodeBlockInSourceMode`） |
| 选区高亮依赖 | 原生 / `drawSelection` | 额外 `codeBlockSelectionPlugin` |

**优劣小结**：

- `auto` 模式：渲染保真度高（整块 Widget 可自由排版、点按定位精准），但渲染态不可直接编辑，需在「墙」与「源码」间切换；适合以阅读为主、偶尔编辑的场景。
- `inline` 模式：编辑体验最接近原生文本（点进去就改、高亮随输入刷新），但实现更复杂——要维护 HAST→mark 的位置对齐、要单独处理选区可见性、围栏行用两个 Widget 分别替换；适合以编写代码为主、希望「所见即所编」的场景。

完成对照后，建议在 demo 里同时打开两个编辑器，输入同一段多行代码，亲手验证「直接编辑」与「进入再编辑」的手感差异。

## 6. 本讲小结

- `inline` 模式的核心策略是**只替换两行围栏**（`CodeBlockHeaderWidget` + `CodeBlockFooterWidget`），把代码正文留在 `contentDOM` 作为真实可编辑文本，辅以正文行的 `cm-codeblock-content` 行级装饰。
- 高亮走 **HAST→mark 路线**：`highlightCodeHast` 返回原始 HAST 树，`hastToMarkDecorations` 递归遍历、用 `offset` 追踪字符位置，把每个带类名的文本片段变成 `Decoration.mark`，最终复用同一套 `.hljs-*` 主题样式。
- `hastToMarkDecorations` 的两条规则：偏移量只在 `text` 节点推进；类名「向下继承、内层优先」（`effectiveClass = nodeClass || cls`）。
- `inline` 不调用 `shouldShowSource`、不响应选区变化，源码态完全由 `setCodeBlockSourceMode` 按钮驱动；但每次 `docChanged` 都会重建，从而实现「随输入实时刷新高亮」。
- 由于 `drawSelection` 的选区层在不透明背景之后，`inline` 额外挂载 `codeBlockSelectionPlugin`（仅 inline 模式），用 `cm-codeblock-sel` mark 把选区画在 `.cm-line` 内部，保证跨浏览器可见。
- 两条高亮路线的本质分叉：`auto`/`toggle` 用 `highlightCode`+`innerHTML` 把代码拍成 Widget 内的静态副本；`inline` 用 `highlightCodeHast`+`Decoration.mark` 把高亮当成真实文本上的彩色滤镜。

## 7. 下一步学习建议

至此，代码块子系统（u5-l1 → u5-l4）已完整讲完：高亮引擎、三模式决策、auto/toggle Widget、inline 的 HAST→mark 与选区插件。接下来建议：

- 进入 **u6 图片与链接** 单元，对比另一类「渲染态用 replace+Widget、编辑态回退源码」的实现（`imageField`、`linkPlugin`），体会 `shouldShowSource` 驱动与按钮驱动的差异。
- 若对装饰策略取舍感兴趣，可提前跳读 **u7-l3 整体架构与设计取舍**，把本讲的「innerHTML 路线 vs HAST→mark 路线」放回全库的 mark+CSS 与 replace+Widget 两条主线中统合理解。
- 想动手实验的话，可尝试 **u7-l4 二次开发** 的自定义插件任务，把本讲的 `hastToMarkDecorations` 递归模式迁移到自定义语法的高亮上。
