# blockMathField 与 mathCache：块级公式与 KaTeX 缓存

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚**为什么块级公式必须用 `StateField`（`blockMathField`）而不能像行内公式那样用 `ViewPlugin`**——这是本讲最核心的一条结论。
- 看懂 `buildBlockMathDecorations` 如何借助 Lezer 语法树的 `FencedCode`→`CodeInfo`/`CodeText` 子节点，从 ` ```math ` 围栏块里取出语言标识和公式源码。
- 理解 `mathCache` 用一张 `Map` 缓存 KaTeX 渲染结果的键设计（`'inline:src'` / `'block:src'`），以及它和 `MathWidget.eq` 如何形成**两级优化**。

本讲是「行内公式（u3-l2）」的自然延续：行内公式走 `ViewPlugin` + `Decoration.replace`（不带 `block`），块级公式则走 `StateField` + `Decoration.replace({ block: true })`。两者「该显示渲染还是显示源码」的决策都交给同一个 `shouldShowSource`。

---

## 2. 前置知识

在进入源码前，确认你已经掌握下面这些（它们都在前置讲义里讲过）：

- **`shouldShowSource(state, from, to)`**（u2-l2）：纯函数，三步短路判定某区间显示源码（`true`）还是渲染结果（`false`）。本讲里它决定一个 ` ```math ` 块整体显示为渲染后的 KaTeX，还是退回逐行源码底色。
- **`mouseSelectingField`**（u2-l3）：记录「是否正在拖选」的 `StateField`。拖拽中所有公式都要退回稳定的源码态，避免 widget 抖动闪烁。
- **`checkUpdateAction(update)`**（u2-l3）：把 `ViewUpdate` 归纳为 `rebuild / skip / none` 三种动作。行内公式插件直接调用它；**块级公式不能直接调用它**——原因见第 4.1 节。
- **`WidgetType` 三件套 `eq / toDOM / ignoreEvent`**（u3-l1）：`eq` 决定能否复用旧 DOM，`toDOM` 造 DOM，`ignoreEvent` 决定事件是否交给 CodeMirror。
- **`Decoration.replace({ widget, block })`**（u3-l1）：带 `block: true` 的 replace 会整行替换并改变行高——这正是它必须进入 `EditorState` 的根因。

补充一句术语：本讲反复出现的「围栏块（fenced code block）」指 GFM 里用三个反引号包裹的代码块，例如 ` ```math\nx^2\n``` `。Lezer 的 Markdown 语法树把它解析成一个 `FencedCode` 节点，它有三个子节点——`CodeMark`（开/闭围栏 ` ``` `）、`CodeInfo`（语言标识，这里是 `math`）、`CodeText`（代码正文）。这是第 4.2 节的主角。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/plugins/math.ts` | 装饰编排者 | `buildBlockMathDecorations`（识别围栏块、提取源码、产出装饰）与 `blockMathField`（StateField 的 `create`/`update`） |
| `src/utils/mathCache.ts` | 纯函数 + 缓存 | `renderMath`（渲染并缓存）、`clearMathCache`（清缓存）、缓存键设计 |
| `src/widgets/mathWidget.ts` | DOM 渲染单元 | `MathWidget.eq`（与缓存的联动）、`toDOM`（调用 `renderMath`） |
| `src/core/pluginUpdateHelper.ts` | 更新决策 | `checkUpdateAction`——`blockMathField.update` 用 Transaction 语言复刻它的判定 |
| `src/plugins/__tests__/math.test.ts` | 测试 | `block math field` 测试组与缓存键测试，是理解行为的最快入口 |

---

## 4. 核心概念与源码讲解

### 4.1 blockMathField：为什么块级公式必须用 StateField

#### 4.1.1 概念说明

行内公式（u3-l2）和块级公式都是用 `Decoration.replace` 把 Markdown 原文替换成 `MathWidget` 生成的 DOM。两者的唯一区别是 replace 是否带 `block: true`：

- **行内公式**：`Decoration.replace({ widget })`，**不带** `block`。文本在一行之内被替换，不改变行的垂直结构 → 可以走 `ViewPlugin`。
- **块级公式**：`Decoration.replace({ widget, block: true })`，**带** `block`。整块（含开闭围栏多行）被替换成居中的 `div`，**改变了行高和文档的垂直布局**。

CodeMirror 6 有一条硬规则：**会改变行垂直结构的装饰（block decoration）必须通过 `StateField` 提供，而不能通过 `ViewPlugin` 提供。** 原因在于 block 装饰会影响视口里每行的高度，而视口测量依赖装饰结果；`ViewPlugin` 的更新发生在视图层、晚于状态计算，拿不到稳定的 block 装饰来参与布局。`StateField` 的值属于 `EditorState`，是状态快照的一部分，能在测量前就被算出来。

因此本库把块级公式放进 `blockMathField`（`StateField`），行内公式放进 `mathPlugin`（`ViewPlugin`）。文件头部注释明确写出了这条取舍：

> [src/plugins/math.ts:12-15](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L12-L15) —— 注释说明：行内用 `ViewPlugin`（不带 block 的 replace），块级用 `StateField`（block 装饰必须经 StateField 提供）。

#### 4.1.2 核心流程

`blockMathField` 是一个 `StateField.define<DecorationSet>`，它的值就是一整套块级公式装饰。生命周期如下：

1. **`create(state)`**：编辑器首次构造时，调用 `buildBlockMathDecorations(state)` 算出初始装饰集。
2. **`update(deco, tr)`**：每次收到 Transaction `tr`，用一套短路判定决定「重算」还是「原样保留（返回旧 `deco`）」。
3. **`provide`**：把字段的值作为 `EditorView.decorations` 暴露出去，CodeMirror 据此渲染。

关键在 `update`：它**没有调用 `checkUpdateAction`**，而是用 Transaction 的字段（`tr.docChanged`、`tr.selection` 等）手动复刻了同一套「结构性变化 → 拖拽结束 → 拖拽中 → 选区改变 → 无」的判定顺序。为什么要复刻而不是调用？因为 `checkUpdateAction` 的入参是 `ViewUpdate`（视图层产物），而 `StateField.update` 拿到的是 `Transaction`（状态层产物）——两者的可用字段不同，无法直接套用。这套「同一套更新语义、两种语言实现」的现象在 u2-l3 已经埋下伏笔，本讲是它的具体落地。

值得注意的一个**差异**：`checkUpdateAction` 还会检查 `viewportChanged`，而 `blockMathField.update` **不检查**视口变化。这是因为 StateField 的值是针对整篇文档算出来的状态，不随视口滚动而变；而 `ViewPlugin` 经常需要响应视口变化（u2-l1 提到 ViewPlugin 适合随视口重算）。这是一个可以观察到的真实区别。

#### 4.1.3 源码精读

`blockMathField` 的完整定义：

[src/plugins/math.ts:152-181](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L152-L181) —— `StateField.define<DecorationSet>` 的 `create` / `update` / `provide` 三件套。

重点拆 `update`（精简后）：

```ts
update(deco, tr) {
  if (tr.docChanged || tr.reconfigured) {
    return buildBlockMathDecorations(tr.state);          // ① 结构性变化：重算
  }

  const isDragging  = tr.state.field(mouseSelectingField, false);
  const wasDragging = tr.startState.field(mouseSelectingField, false);

  if (wasDragging && !isDragging) {                       // ② 拖拽刚结束：重算
    return buildBlockMathDecorations(tr.state);
  }
  if (isDragging) {                                       // ③ 拖拽中：冻结（返回旧 deco）
    return deco;
  }
  if (tr.selection) {                                     // ④ 选区改变：重算
    return buildBlockMathDecorations(tr.state);
  }
  return deco;                                            // ⑤ 都不满足：原样保留
}
```

把这五步和 [src/core/pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65) 的 `checkUpdateAction` 对照看：判定**顺序**和**意图**完全一致（结构性→拖拽结束→拖拽中→选区），只是把 `update.docChanged` 换成 `tr.docChanged`、把 `update.startState` 换成 `tr.startState`、把 `update.selectionSet` 换成 `tr.selection`（Transaction 携带了新选区即为真），并去掉了对 `viewportChanged` 的检查。

`provide: (f) => EditorView.decorations.from(f)`（第 180 行）是 StateField 把自己的 `DecorationSet` 喂给视图的标准写法——`f` 是这个字段的引用。

#### 4.1.4 代码实践

**实践目标**：验证 `blockMathField` 的 `update` 在「拖拽中」真的冻结装饰，而不是重算。

**操作步骤**（源码阅读型实践，配合测试文件）：

1. 打开 [src/plugins/__tests__/math.test.ts:167-254](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L167-L254) 的 `block math field` 测试组，看清 `createTestView` 如何装配扩展（`markdown()` + `mouseSelectingField` + `collapseOnSelectionFacet.of(true)` + `blockMathField`）。
2. 在 `should update on selection change` 用例（第 241 行起）后，**自己加一个用例**：先 `dispatch` 一个把 `mouseSelectingField` 置为 `true` 的 `setMouseSelecting.of(true)` 效果，再 `dispatch` 一次选区变化，断言此刻装饰集与拖拽前的快照是**同一个引用**（即没有被重算）。

**需要观察的现象**：拖拽中即使选区在变，`view.state.field(blockMathField)` 返回的对象引用不变（对应 ③ 分支 `return deco`）。

**预期结果**：用例通过即证明 ③ 分支生效——拖拽中装饰被冻结。若不确定 jsdom 下 `setMouseSelecting` 的派发细节，可标注「待本地验证」并在真实 demo 里用鼠标拖选穿过一个块级公式，观察它退回源码底色且不抖动。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `blockMathField` 的 `update` 第 ③ 分支（`if (isDragging) return deco;`）删掉，会出现什么现象？

> **答案**：拖拽选区时，每移动一下鼠标都会触发 `tr.selection`，于是走到 ④ 分支重算装饰，公式在「渲染 widget」和「源码底色」之间反复横跳，产生闪烁。这正是 ③ 分支存在的意义——和 u2-l3 的 `checkUpdateAction` 同理。

**练习 2**：为什么 `blockMathField.update` 不需要检查 `viewportChanged`，而 `checkUpdateAction` 需要？

> **答案**：StateField 的值是针对整篇文档语法树算出的状态、属于 `EditorState` 快照，不随视口滚动而改变；而 `ViewPlugin` 的 `update` 经常需要响应视口变化来补算进入视口的新节点。两者作用域不同，所以检查项也不同。

---

### 4.2 CodeInfo / CodeText：从围栏块提取语言与公式源码

#### 4.2.1 概念说明

无论是渲染还是退回源码，都得先回答两个问题：**这个围栏块是不是公式块？公式源码是什么？** 这两个答案分别藏在 `FencedCode` 节点的两个子节点里：

- **`CodeInfo`**：开围栏 ```` ``` ```` 后面的语言标识。对 ```` ```math ```` 来说，`CodeInfo` 的文本就是 `math`。
- **`CodeText`**：围栏之间的代码正文（不含围栏行本身）。对块级公式来说，它就是公式源码。

注意：**行内公式（u3-l2）没有这两个子节点**——行内公式是借 `InlineCode` 节点 + 文本前后缀判定的（`` `$...$` ``），不走围栏块结构。这是行内与块级在「识别方式」上的根本不同：行内靠**文本特征**，块级靠**语法树结构（语言标识）**。

#### 4.2.2 核心流程

`buildBlockMathDecorations(state)` 的流程：

1. `syntaxTree(state).iterate` 遍历语法树，只对 `node.name === 'FencedCode'` 的节点进入处理。
2. `node.node.getChild('CodeInfo')` 取语言子节点，`sliceString` 得到语言字符串；若等于 `'math'` 才继续。
3. `node.node.getChild('CodeText')` 取正文子节点，`sliceString` 后 `.trim()` 得到公式源码。
4. 用 `shouldShowSource(state, node.from, node.to)` 判定是否退回源码态（注意区间是**整个 FencedCode**，包括 ```` ```math ```` 和 ```` ``` ```` 两行）。
5. 渲染态：`Decoration.replace({ widget, block: true })`；源码态：对块内**每一行**加一条 `Decoration.line({ class: 'cm-math-source-block' })`。

#### 4.2.3 源码精读

识别 + 提取的核心几行（精简）：

[src/plugins/math.ts:104-114](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L104-L114) —— 从 `FencedCode` 取 `CodeInfo` 判语言、取 `CodeText` 取源码，并调用 `shouldShowSource`。

```ts
if (node.name === 'FencedCode') {
  const codeInfo = node.node.getChild('CodeInfo');
  if (codeInfo) {
    const lang = state.doc.sliceString(codeInfo.from, codeInfo.to);
    if (lang === 'math') {
      const codeText = node.node.getChild('CodeText');
      const source = codeText
        ? state.doc.sliceString(codeText.from, codeText.to).trim()
        : '';
      const isTouched = shouldShowSource(state, node.from, node.to);
      ...
```

两个要点：

- `iterate` 回调里的 `node` 是「迭代游标（cursor）」，能拿到 `from/to/name`，但要访问子节点得用 `node.node.getChild(...)` 取真正的 `SyntaxNode`。
- `codeText` 可能为 `null`（空块，如 ```` ```math\n``` ````），所以做了三元保护，空块时 `source = ''`。

拿到 `isTouched` 后分两条路：

[src/plugins/math.ts:116-136](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L116-L136) —— 渲染态用 `replace({ widget, block: true })`，源码态逐行 `line({ class: 'cm-math-source-block' })`。

```ts
if (!isTouched && !isDrag) {
  const widget = createMathWidget(source, true);          // 第二参 true = 块级
  decorations.push(
    Decoration.replace({ widget, block: true }).range(node.from, node.to)
  );
} else {
  for (let pos = node.from; pos <= node.to; ) {
    const line = state.doc.lineAt(pos);
    decorations.push(
      Decoration.line({ class: 'cm-math-source-block' }).range(line.from)
    );
    pos = line.to + 1;                                    // 跳到下一行起点
  }
}
```

三处细节值得留意：

1. `block: true` 就是 4.1 节那条「必须用 StateField」规则的**触发开关**——带上它，这个 replace 就成了 block 装饰。
2. 源码态用 `Decoration.line`（行级装饰），`range` 只能传**行起点** `line.from`；循环里 `pos = line.to + 1` 跳过换行符推进到下一行，从而给块内每一行（含开闭围栏行）都刷上底色。
3. `!isDrag` 是**第二道拖拽防护**：即便 `shouldShowSource` 内部已有拖拽短路，这里再判一次 `!isDrag`，确保拖拽中即便走到渲染态分支也稳妥退回源码。这与 u3-l2 行内公式的双层防护是同一套手法。

底色 `cm-math-source-block` 的样式定义在主题里：

[src/theme/default.ts:169-171](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L169-L171) —— 给块级公式源码态加一层淡绿色底，与行内源码态 `cm-math-source` 的样式区分。

#### 4.2.4 代码实践

**实践目标**：亲手从一段围栏块文本里复现 `CodeInfo`/`CodeText` 的提取逻辑，理解 `source` 究竟是什么。

**操作步骤**（源码阅读 + 心算型实践）：

1. 给定文档（`\n` 表示真实换行）：

   ```
   ```math
   \int_0^\infty e^{-x^2}\,dx
   ```
   ```

2. 对照上面的提取代码，写出：
   - `CodeInfo` 的文本 = `math`
   - `CodeText` 的文本 = `\int_0^\infty e^{-x^2}\,dx`（`trim()` 会去掉首尾换行/空白）
   - `source` = `\int_0^\infty e^{-x^2}\,dx`
   - `createMathWidget(source, true)` 的第二参 `isBlock = true`，决定 `MathWidget.toDOM` 生成 `<div class="cm-math-block">`（见 [src/widgets/mathWidget.ts:40-41](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L40-L41)）。

3. 再想象 `shouldShowSource` 用的是 `node.from..node.to`（整个围栏块），所以光标只要落在 ```` ```math ```` 这一行的任意位置，整个块就退回源码底色。

**需要观察的现象**：源码态下，块内**所有行**（含开闭围栏）都带 `cm-math-source-block` 底色；渲染态下，整个块被一个居中的 `div` 替换。

**预期结果**：渲染态的 KaTeX 输出大致是

\[
\int_0^\infty e^{-x^2}\,dx
\]

（由 KaTeX 渲染；具体外观「待本地验证」，取决于宿主加载的 KaTeX 版本。）

#### 4.2.5 小练习与答案

**练习 1**：如果用户写的是 ```` ```Math ````（大写 M）或 ```` ```latex ````，会发生什么？

> **答案**：`lang === 'math'` 是严格相等且区分大小写，```` ```Math ```` 不会被识别为公式块，会按普通代码块处理；```` ```latex ```` 同理不是 `math`，也不会被 `buildBlockMathDecorations` 渲染。这是项目当前的有意限制（不同于某些支持 `latex`/`tex` 别名的编辑器）。

**练习 2**：源码态循环里为什么是 `pos = line.to + 1` 而不是 `pos = line.to`？

> **答案**：`line.to` 指向行末换行符之前的位置；`+ 1` 才能跨过换行符到达下一行的起点。若写成 `line.to`，`lineAt(pos)` 仍返回当前行，循环就卡死在原地。

---

### 4.3 mathCache：用 Map 缓存 KaTeX 渲染结果

#### 4.3.1 概念说明

KaTeX 的 `renderToString` 是个**重操作**：它要把 LaTeX 源码解析成语法树、再生成带字体的 HTML。一篇文档里同一个公式可能反复出现，渲染态与源码态之间也会频繁切换。如果每次重建装饰都重新跑一遍 KaTeX，开销会很明显。

`mathCache` 的思路非常朴素：**渲染过的 HTML 字符串存进一张 `Map`，下次同样的输入直接取缓存。** 关键是「同样的输入」如何定义——这就引出了缓存键设计。

#### 4.3.2 核心流程

`renderMath(source, displayMode)` 的流程：

1. 拼缓存键：`key = ${displayMode ? 'block' : 'inline'}:${source}`。
2. `cache.has(key)` 命中 → 直接 `return cache.get(key)`，**不碰 KaTeX**。
3. 未命中 → 读取全局 `window.katex`，调用 `katex.renderToString(...)`，把结果 `cache.set(key, rendered)` 后返回。
4. 任何异常（如 KaTeX 未安装）→ 走 `catch`，返回错误 span，**且不写入缓存**。

注意第 4 步：**只有成功渲染的结果才进缓存；错误结果不缓存。** 这是一个有意的设计取舍，下面解释。

#### 4.3.3 源码精读

缓存表本身是一个模块级单例 `Map`：

[src/utils/mathCache.ts:11](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L11) —— `const cache = new Map<string, string>()`，进程内单例，跨编辑器实例共享。

渲染函数（精简）：

[src/utils/mathCache.ts:26-55](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L26-L55) —— 拼键、查缓存、调 KaTeX、写缓存、异常兜底。

```ts
export function renderMath(source: string, displayMode: boolean): string {
  const key = `${displayMode ? 'block' : 'inline'}:${source}`;

  if (cache.has(key)) {
    return cache.get(key)!;                                // 命中：直接返回，跳过 KaTeX
  }

  try {
    const katex = window.katex;                            // 运行时读取全局 KaTeX
    if (!katex) {
      throw new Error('KaTeX not found. Please install katex package.');
    }
    const rendered = katex.renderToString(source, {
      displayMode, throwOnError: false, errorColor: '#cc0000', strict: false,
    });
    cache.set(key, rendered);                              // 只缓存成功结果
    return rendered;
  } catch (error) {
    const errorMsg = error instanceof Error ? error.message : 'Unknown error';
    return `<span class="cm-math-error">[Math Error: ${errorMsg}]</span>`; // 不缓存
  }
}
```

四个要点：

1. **键设计**：`'inline:src'` 与 `'block:src'`。同一个源码串，行内和块级是**两条不同的缓存项**——因为 KaTeX 的 `displayMode` 不同会产生不同 HTML（块级居中、独占行；行内嵌入文本流）。这正是测试 `'should differentiate inline and block cache'`（[src/plugins/__tests__/math.test.ts:52-56](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L52-L56)）所验证的：`renderMath('x^2', false)` 与 `renderMath('x^2', true)` 不相等。

2. **KaTeX 的运行时绑定**：代码读的是 `window.katex`（第 36 行），**不是** `import`。注释虽写「Dynamic import」，但实际是「由宿主把 KaTeX 挂到全局 `window`」。这正是「KaTeX 是可选依赖、不打包进库、缺失时优雅降级」的实现方式（u1-l1/u1-l2 的可选依赖在这里落地）。测试里也正是用 `(global as any).window.katex = {...}` 来 mock 的（[src/plugins/__tests__/math.test.ts:14-25](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L14-L25)）。

3. **错误不缓存**：`throwOnError: false` 让 KaTeX 把语法错误渲染成红色错误 HTML（这类结果**会**被缓存，因为它走了正常返回路径）；而「KaTeX 未安装」等异常走 `catch`，返回的错误 span **不进缓存**。这样设计的好处是：若宿主稍后才加载 KaTeX，下次调用还能重新尝试，不会被一条「找不到 KaTeX」的错误结果永久卡住。

4. **与 `MathWidget.eq` 的两级优化**。`MathWidget.eq` 只比较 `source` 与 `isBlock`（[src/widgets/mathWidget.ts:32-34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L32-L34)）：

   ```ts
   eq(other: MathWidget): boolean {
     return other.source === this.source && other.isBlock === this.isBlock;
   }
   ```

   - **第一级（widget 级，CodeMirror 负责）**：当新装饰的 widget 与旧 widget `eq` 相等，CodeMirror 直接复用旧 DOM，**`toDOM` 根本不执行**，`renderMath` 连调用都不会发生。
   - **第二级（math 级，本缓存负责）**：当 widget 被重建（`eq` 失败），`toDOM` 会执行并调用 `renderMath`，此时缓存命中仍能跳过昂贵的 KaTeX 计算。

   为什么 widget 会被重建而绕过 `eq`？因为渲染态（`replace` widget）和源码态（`line` 装饰）是**不同类型**的装饰，光标在块级公式上进进出出时，每次从源码态切回渲染态都会造一个全新的 `MathWidget`，CodeMirror 在该位置找不到同类型旧 widget，只能 `toDOM` 重画。这时候**缓存**就是真正省下 KaTeX 开销的那一层。换句话说：`eq` 管「同一 widget 不变」，`mathCache` 管「widget 被重建时也能复用渲染结果」。

清缓存接口：

[src/utils/mathCache.ts:62-64](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L62-L64) —— `clearMathCache()` 一行 `cache.clear()`。供宿主在 KaTeX 升级或主题变化时手动清理，二者都从 `src/index.ts` 第 [19](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L19)、[37](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L37) 行统一导出。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用「插桩计数」直观对比 `clearMathCache()` 前后，`katex.renderToString` 被调用的次数差异，量化缓存命中带来的收益。

**操作步骤**：

1. 新建一个临时测试文件 `src/utils/__tests__/mathCache.bench.test.ts`（示例代码，仅供观察，不要提交），仿照 `math.test.ts` 的 mock 方式，但在 `renderToString` 外面包一层计数器：

   ```ts
   // 示例代码
   import { describe, it, expect, beforeEach } from 'vitest';
   import { renderMath, clearMathCache } from '../mathCache';

   describe('mathCache 命中对照', () => {
     let katexCalls = 0;

     beforeEach(() => {
       katexCalls = 0;
       // eslint-disable-next-line @typescript-eslint/no-explicit-any
       (global as any).window = {
         katex: {
           // eslint-disable-next-line @typescript-eslint/no-explicit-any
           renderToString: (src: string, _opts: any) => {
             katexCalls++;                       // 关键：每次真正调用 KaTeX 就 +1
             return `<span class="katex">${src}</span>`;
           },
         },
       };
       clearMathCache();                         // 每个用例从空缓存开始
     });

     it('同一公式重复渲染只命中 KaTeX 一次', () => {
       renderMath('E = mc^2', false);
       renderMath('E = mc^2', false);
       renderMath('E = mc^2', false);
       expect(katexCalls).toBe(1);              // 后两次走缓存
     });

     it('clearMathCache 后会重新调用 KaTeX', () => {
       renderMath('E = mc^2', false);           // katexCalls = 1
       expect(katexCalls).toBe(1);
       clearMathCache();                         // 清空缓存
       renderMath('E = mc^2', false);           // 缓存空 → 重新调 KaTeX
       expect(katexCalls).toBe(2);
     });

     it('行内与块级各算一次', () => {
       renderMath('x^2', false);                // inline:x^2 → 1
       renderMath('x^2', true);                 // block:x^2  → 2（不同键）
       renderMath('x^2', false);                // 命中 inline:x^2
       renderMath('x^2', true);                 // 命中 block:x^2
       expect(katexCalls).toBe(2);
     });
   });
   ```

2. 在项目根目录运行 `npm test -- mathCache.bench`。

**需要观察的现象**：三条断言全部通过——同一公式三次渲染 `katexCalls === 1`；清缓存后再渲染 `katexCalls === 2`；行内/块级同源各算一次。

**预期结果**：缓存命中时 `renderMath` 走的是「拼键 → `cache.has` 命中 → `return cache.get`」这条**极短路径**（[src/utils/mathCache.ts:27-31](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L27-L31)），完全不执行 KaTeX 解析与 HTML 生成。未命中时才走完整的 KaTeX 渲染（第 42-50 行）。在一份含大量重复公式的长文档里，光标反复进出公式造成的 widget 重建，因为缓存命中，KaTeX 调用次数趋近于「不同公式的种类数」，而非「渲染总次数」——这就是缓存收益的来源。测试通过情况可在本地运行后确认；若不想新增文件，也可直接阅读现有 `should cache rendered results`、`should differentiate inline and block cache`、`should clear cache` 三条用例（[src/plugins/__tests__/math.test.ts:46-64](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L46-L64)）获得等价认知。

#### 4.3.5 小练习与答案

**练习 1**：为什么缓存键要带上 `'inline:'` / `'block:'` 前缀，而不能只用 `source`？

> **答案**：KaTeX 的 `displayMode` 不同，同一个源码会生成不同 HTML（块级居中独占行、行内嵌入文本流）。若只用 `source` 作键，块级公式会错误命中行内公式的缓存，渲染出错误的排版。前缀正是为了区分这两种渲染模式。

**练习 2**：`renderMath` 的 `catch` 分支为什么不把错误结果写入缓存？

> **答案**：进入 `catch` 主要是 KaTeX 未安装（`throw new Error('KaTeX not found...')`）。若把「未找到 KaTeX」的错误结果缓存，宿主随后即便加载了 KaTeX，同名公式也会一直返回错误结果。不缓存错误，能保证「KaTeX 一旦可用就立即生效」。（注意：KaTeX 自身的语法错误因为 `throwOnError: false` 会被渲染成错误 HTML 并正常返回，这类结果会进缓存——这是另一条路径。）

**练习 3**：缓存收益和 `MathWidget.eq` 各自挡住了哪一层开销？

> **答案**：`eq` 在 CodeMirror 层挡住「`toDOM` 的执行」——widget 不变时连 DOM 都不重画；`mathCache` 在 widget 不得不重建时（如渲染态↔源码态切换）挡住「KaTeX 的重计算」——`toDOM` 虽执行，但 `renderMath` 命中缓存跳过解析。两者叠加，KaTeX 调用次数被压到很低。

---

## 5. 综合实践

把三个最小模块串起来，完成一个**完整调用链追踪**。

**任务**：给定文档（含一个块级公式，且光标在公式外）：

```
正文

```math
\frac{a}{b}
```

更多正文
```

请按下面的顺序，逐一指出**每一步发生在哪个文件、调用了什么、产生了什么**：

1. 编辑器构造时 `blockMathField.create(state)` 调用 `buildBlockMathDecorations(state)`（[src/plugins/math.ts:153-155](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L153-L155)）。
2. `iterate` 找到 `FencedCode` 节点，取 `CodeInfo` 判出 `lang === 'math'`，取 `CodeText` 得 `source = '\frac{a}{b}'`（第 104-113 行）。
3. 光标在公式外，`shouldShowSource` 返回 `false`，且非拖拽 → 走渲染态，`createMathWidget('\frac{a}{b}', true)` 产出 `MathWidget`（第 116-124 行）。
4. CodeMirror 渲染 widget 时调用 `MathWidget.toDOM`（[src/widgets/mathWidget.ts:39-53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L39-L53)），`toDOM` 调 `renderMath('\frac{a}{b}', true)`（第 44 行）。
5. `renderMath` 拼键 `'block:\frac{a}{b}'`，缓存未命中 → 调 `window.katex.renderToString` → 写缓存 → 返回 HTML（[src/utils/mathCache.ts:27-50](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L27-L50)）；`toDOM` 把 HTML 塞进 `<div class="cm-math-block">`。
6. 现在把光标移进围栏块：触发一次 `tr.selection` 事务，`blockMathField.update` 走第 ④ 分支重算（[src/plugins/math.ts:173-175](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L173-L175)）。此时 `shouldShowSource` 返回 `true` → 走源码态，逐行刷 `cm-math-source-block` 底色（第 125-136 行），widget 被移除。
7. 再把光标移出围栏块：又一次选区事务 → 重算 → `shouldShowSource` 又回 `false` → 重建 `MathWidget`。由于 `eq` 在该位置找不到旧 widget，`toDOM` 重跑，但 `renderMath` **命中缓存**，KaTeX 不再被调用。

**交付**：把上述 7 步画成一张时序图（谁调用谁、谁产出什么），并在第 6→7 步的切换处标注「widget 被销毁/重建，但缓存命中」。这张图能一次性体现「StateField 驱动 block 装饰 + CodeInfo/CodeText 提取源码 + shouldShowSource 切换 + mathCache 兜底性能」四件事如何协同。

---

## 6. 本讲小结

- **块级公式必须用 `StateField`**：`Decoration.replace({ block: true })` 会改变行高和垂直布局，而 block 装饰必须属于 `EditorState` 才能参与视口测量；`ViewPlugin` 给不了。这就是 `blockMathField` 存在的根因。
- **`blockMathField.update` 用 Transaction 语言复刻 `checkUpdateAction`**：判定顺序（结构性→拖拽结束→拖拽中→选区→无）一致，但因入参是 `Transaction` 而非 `ViewUpdate`，只能手动翻译，且省去了 `viewportChanged` 检查。
- **`CodeInfo`/`CodeText` 子节点**是识别围栏块的关键：`CodeInfo` 判语言（严格等于 `math`），`CodeText` 取公式源码（`trim()`）。行内公式没有这套结构，靠 `InlineCode` 的文本特征识别。
- **源码态逐行 `Decoration.line`**：用 `pos = line.to + 1` 推进，给块内每行（含围栏行）刷 `cm-math-source-block` 底色；`shouldShowSource` 用整个 `FencedCode` 区间判定，光标落在围栏行也会触发。
- **`mathCache` 用 `'inline:src'` / `'block:src'` 作键**：行内与块级同源算两条缓存项；只缓存成功结果，KaTeX 未安装等错误不缓存以便后续重试。
- **两级优化**：`MathWidget.eq` 在 widget 不变时挡住 `toDOM`；`mathCache` 在 widget 被重建（渲染↔源码切换）时挡住 KaTeX 重计算。

---

## 7. 下一步学习建议

- **横向对比其他 StateField 特性**：本讲之后，建议读 `src/plugins/image.ts`（u6-l1）和 `src/plugins/table.ts`（u4-l2）。它们同样是 `StateField` + `block` widget + `shouldShowSource` 双模式，但 image 引入了异步加载与状态机，table 引入了解析/序列化与可编辑单元格——你会看到同一个 StateField 骨架如何承载截然不同的特性。
- **专家级代码块（u5-l2/u5-l4）**：`codeBlockField` 是本套机制最复杂的变体，它也是 `StateField` 处理 `FencedCode`，但有 `auto/toggle/inline` 三种交互模式和 `SKIP_LANGUAGES`（恰好把 `math` 跳过去，避免和本讲的 `blockMathField` 抢同一节点）。读它之前先吃透本讲，会非常顺。
- **动手实验**：仿照第 4.3.4 节的插桩法，给 `renderMath` 加一个 `console.log` 打印缓存命中/未命中，在 demo 里输入几个重复公式，用光标反复进出，观察控制台——你会直观看到「KaTeX 只被调用 N 次（不同公式数），而非渲染总次数」。
