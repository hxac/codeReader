# tableField：只读 GFM 表格渲染

## 1. 本讲目标

上一讲（[u4-l1](u4-l1-table-parser-and-serializer.md)）我们写好了两个与 CodeMirror 无关的纯函数 `parseMarkdownTable` / `serializeMarkdownTable`，它们能把一段 GFM 表格文本变成结构化的 `TableData`，也能反过来序列化回去。本讲要把这层「地基」接进编辑器，让表格真正「渲染」出来。

具体地，学完本讲你应该能够：

- 看懂 `tableField`（一个 `StateField`）如何遍历语法树找到 `Table` 节点、调用解析器、再用 `shouldShowSource` 在「渲染态」与「源码态」之间二选一。
- 说出 `tableField.update` 为什么**手动内联**更新判定，而不像 `livePreviewPlugin` 那样调用 `checkUpdateAction`。
- 读懂 `TableWidget.toDOM` 如何从零搭出 `<thead>`/`<tbody>`，并把对齐写进单元格的 `style.textAlign`。
- 理解 `renderInlineMarkdown` 这个轻量工具如何在单元格内渲染粗体/斜体/行内代码，以及它为什么**先转义 HTML 再替换**。

本讲只覆盖**只读**渲染（光标在表格内时显示带底色的源码、可原样编辑，但不会把编辑内容结构化写回）。真正的「可编辑表格」是下一讲（[u4-l3](u4-l3-table-editor-plugin.md)）的 `tableEditorPlugin`，不要混淆。

## 2. 前置知识

本讲建立在三块已经讲过的认知之上，这里只做最简承接：

- **块级装饰必须用 `StateField`**（[u3-l1](u3-l1-widgettype-and-decoration-replace.md) / [u3-l3](u3-l3-block-math-and-cache.md)）：带 `block: true` 的 `Decoration.replace` 会改变行高，必须属于 `EditorState` 才能参与视口测量，所以不能用 `ViewPlugin`。表格是一整块，自然走这条路。
- **`shouldShowSource(state, from, to)` 是显示哪一面的唯一事实来源**（[u2-l2](u2-l2-should-show-source.md)）：返回 `true` 表示「显示源码」，返回 `false` 表示「显示渲染结果」。它内部已包含「拖拽中一律返回 `false`」的短路。
- **`parseMarkdownTable` 与 `TableData`**（[u4-l1](u4-l1-table-parser-and-serializer.md)）：`TableData` 有三个字段——`headers: string[]`、`alignments: ('left'|'center'|'right'|null)[]`、`rows: string[][]`。解析失败返回 `null`。

补充一个易混点：本讲的「源码态」用的是 `Decoration.line`（整行加背景类），而不是把整段表格包进一个 `Decoration.replace`。原因是源码态要让用户**逐行直接编辑原始 Markdown 文本**，所以只染色、不替换。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `src/plugins/table.ts` | 定义 `tableField`（StateField）与 `buildTableDecorations` | 决策中枢：遍历语法树 + 渲染/源码二选一 |
| `src/widgets/tableWidget.ts` | 定义 `TableWidget`（继承 `WidgetType`）与工厂 `createTableWidget` | 渲染态的 DOM 产出单元 |
| `src/utils/inlineMarkdown.ts` | 纯函数 `renderInlineMarkdown` | 单元格内联格式（粗体/斜体/代码等）渲染 |
| `src/utils/tableParser.ts` | `TableData` 类型与 `parseMarkdownTable` | 提供 `TableData` 结构（上一讲详讲） |
| `src/theme/default.ts` | 表格相关 CSS | 渲染态 `.cm-table-widget`、源码态 `.cm-table-source` 的外观 |

> 说明：`tableField` 与 `createTableWidget` 都在 [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) 中作为库的公开 API 导出。

---

## 4. 核心概念与源码讲解

### 4.1 tableField StateField

#### 4.1.1 概念说明

`tableField` 是一个 `StateField<DecorationSet>`，它的「值」就是一组装饰。它解决的问题是：

> 在 Live Preview 下，表格该显示成「漂亮的 HTML 表格」还是「带底色的原始 Markdown 源码」？以及，什么时候该重新计算？

答案是：

- **显示什么**：由 `shouldShowSource(state, node.from, node.to)` 决定——光标/选区触碰表格时显示源码，否则显示渲染结果。
- **何时重算**：由 `update` 里的判定决定——文档变了、拖拽结束了、选区变了，都要重算；拖拽中冻结。

为什么用 `StateField` 而不是 `ViewPlugin`？因为渲染态用的是 `Decoration.replace({ widget, block: true })`，这是**块级装饰**，会改变行的垂直高度，必须进入 `EditorState` 才能让 CodeMirror 正确测量视口（这一点 [u3-l1](u3-l1-widgettype-and-decoration-replace.md) 和 [u3-l3](u3-l3-block-math-and-cache.md) 已经反复强调过）。

#### 4.1.2 核心流程

`tableField` 由两块组成：一个负责「算」的 `buildTableDecorations(state)`，一个负责「什么时候重新算」的 `update`。

**算装饰的伪代码**（`buildTableDecorations`）：

```text
deco = []
isDrag = state 里读取 mouseSelectingField（拖拽态）

遍历语法树：
  若 node.name === 'Table':
    src      = 切出 node.from..node.to 的文本
    tableData = parseMarkdownTable(src)
    若 tableData 为 null（非法表格）→ 跳过，不加任何装饰

    isTouched = shouldShowSource(state, node.from, node.to)

    若 !isTouched && !isDrag：        # 渲染态
      push Decoration.replace({ widget: createTableWidget(tableData), block: true })
           .range(node.from, node.to)
    否则：                            # 源码态（光标在内 或 拖拽中）
      对表格覆盖的每一行：
        push Decoration.line({ class: 'cm-table-source' }).range(行起点)

return Decoration.set(deco 排序后, 允许相邻)
```

注意三个细节：

1. **非法表格直接跳过**：解析失败（`parseMarkdownTable` 返回 `null`，例如缺分隔行）时不加装饰，表格就以「纯文本」原样显示——既不渲染也不染色。
2. **渲染态覆盖整段、源码态逐行**：渲染态一条 `replace` 装饰吃掉 `node.from..node.to`；源码态按行循环，每行一条 `Decoration.line`。
3. **双层拖拽防护**：`shouldShowSource` 内部已有「拖拽中返回 `false`」的短路，`buildTableDecorations` 又在外层加了 `!isDrag`。二者叠加，确保拖拽期间表格稳定停留在渲染态、不抖动。

**`update` 的决策表**（手动内联，等价于 [u2-l3](u2-l3-facet-mouseSelecting-updatehelper.md) 的 `checkUpdateAction` 语义）：

| 触发条件 | 动作 | 理由 |
| --- | --- | --- |
| `tr.docChanged` 或 `tr.reconfigured` | 重建 | 文本/配置变了，结构可能变 |
| 拖拽刚结束（`wasDragging && !isDragging`） | 重建 | 冻结期结束，按当前选区重新判定渲染/源码 |
| 正在拖拽（`isDragging`） | 保持 `deco` 不变 | 冻结装饰，避免闪烁 |
| 选区改变（`tr.selection`） | 重建 | 光标进/出表格要切换两种态 |
| 其它 | 保持 `deco` 不变 | 无关更新不重算 |

判定顺序是「结构性变化 → 拖拽结束 → 拖拽中 → 选区 → 无」，与 `checkUpdateAction` 一致——拖拽判定排在选区判定之前，正是为了消除拖拽闪烁。

> 为什么不直接调用 `checkUpdateAction`？因为 `tableField` 是 `StateField`，而 `checkUpdateAction` 是为 `ViewPlugin.fromSchema` 式的 `update` 设计的便捷函数。这里选择把等价的判定逻辑**内联**进 `update`（与 [u3-l3](u3-l3-block-math-and-cache.md) 的 `blockMathField` 同样的做法）。这也是本库的一个模式：**行内特性的 `ViewPlugin` 复用 `checkUpdateAction`，块级特性的 `StateField` 手动内联等价判定**。

#### 4.1.3 源码精读

先看导入，确认本模块依赖了哪些「积木」（[src/plugins/table.ts:11-17](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/table.ts#L11-L17)）：`syntaxTree` 拿语法树，`shouldShowSource` 做决策，`mouseSelectingField` 读拖拽态，`parseMarkdownTable` 解析表格，`createTableWidget` 造渲染 widget。注意它**没有**导入 `checkUpdateAction`。

`buildTableDecorations` 的渲染/源码两个分支（[src/plugins/table.ts:22-58](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/table.ts#L22-L58)）：

```ts
syntaxTree(state).iterate({
  enter: (node) => {
    if (node.name === 'Table') {
      const tableSource = state.doc.sliceString(node.from, node.to);
      const tableData = parseMarkdownTable(tableSource);
      if (!tableData) return;                       // 非法表格：跳过

      const isTouched = shouldShowSource(state, node.from, node.to);

      if (!isTouched && !isDrag) {
        // 渲染态：整段替换成 widget（块级）
        const widget = createTableWidget(tableData);
        decorations.push(
          Decoration.replace({ widget, block: true }).range(node.from, node.to)
        );
      } else {
        // 源码态：逐行加背景类
        for (let pos = node.from; pos <= node.to; ) {
          const line = state.doc.lineAt(pos);
          decorations.push(
            Decoration.line({ class: 'cm-table-source' }).range(line.from)
          );
          pos = line.to + 1;
        }
      }
    }
  },
});
```

这里有个值得注意的小技巧——逐行循环的写法。`Decoration.line` 必须作用在「行起点」，所以用 `lineAt(pos)` 取行，推进到 `line.to + 1`（下一行起点），直到越过 `node.to`。注意循环条件是 `pos <= node.to` 而非 `<`，否则会漏掉表格的最后一行。

`tableField` 本体（[src/plugins/table.ts:65-98](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/table.ts#L65-L98)）：

```ts
export const tableField = StateField.define<DecorationSet>({
  create(state) { return buildTableDecorations(state); },
  update(deco, tr) {
    if (tr.docChanged || tr.reconfigured) return buildTableDecorations(tr.state);
    const isDragging  = tr.state.field(mouseSelectingField, false);
    const wasDragging = tr.startState.field(mouseSelectingField, false);
    if (wasDragging && !isDragging)  return buildTableDecorations(tr.state); // 拖拽结束
    if (isDragging)                  return deco;                            // 拖拽中：冻结
    if (tr.selection)                return buildTableDecorations(tr.state); // 选区变：重建
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});
```

`create` 与 `update` 都返回 `DecorationSet`，最后用 `provide: EditorView.decorations.from(f)` 把这个字段的值「翻译」成视图装饰。读取 `mouseSelectingField` 时带第二参数 `false`，表示「字段不存在就返回 `false`」——防御性降级（[u2-l3](u2-l3-facet-mouseSelecting-updatehelper.md) 已说明）。

#### 4.1.4 代码实践

实践目标：亲眼看到「光标进入表格 → 装饰从 `TableWidget` 切换为逐行 `cm-table-source`」。我们用现成的测试来观察这个切换。

操作步骤：

1. 打开 [src/plugins/\_\_tests\_\_/table.test.ts:93-109](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/table.test.ts#L93-L109)，它构造了一个光标位于表格内（位置 5）的编辑器，然后断言存在 `cm-table-source` 装饰。
2. 对照 [src/plugins/\_\_tests\_\_/table.test.ts:75-91](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/table.test.ts#L75-L91)，它把光标放在表格**之后**，断言存在 `widget` 装饰。
3. 运行这一个测试文件：`npx vitest run src/plugins/__tests__/table.test.ts`。

需要观察的现象：

- 光标在表格**外**（第二个用例）：`decos.between` 回调里能找到 `spec.widget`，说明走了渲染态分支。
- 光标在表格**内**（第一个用例）：能找到 `spec.class` 含 `cm-table-source`，说明走了源码态分支，且因为是逐行 `Decoration.line`，3 行表格至少有 3 条装饰。

预期结果：两个用例都通过。如果第一个用例失败（找不到 `cm-table-source`），通常意味着 `collapseOnSelectionFacet.of(true)` 没启用——`shouldShowSource` 第一道关卡会直接返回 `false`，导致即便光标在表格内也走渲染态。这正是 `createTestView` 里必须挂 `collapseOnSelectionFacet.of(true)` 的原因（见测试第 18 行）。

> 本地验证提示：本仓库测试跑在 jsdom 环境（[u1-l3](u1-l3-build-test-run-demo.md)），首次运行前需 `npm install`。

#### 4.1.5 小练习与答案

**练习 1**：如果删除 `buildTableDecorations` 里 `if (!tableData) return;` 这一行，对一个「没有分隔行的非法表格」会发生什么？

**答案**：`parseMarkdownTable` 返回 `null`，随后 `createTableWidget(null)` 会在 `toDOM` 里访问 `null.headers` 抛错。所以这行守卫是必需的——它让非法表格退化为「纯文本、不加装饰」。

**练习 2**：`tableField.update` 为什么把「拖拽结束（`wasDragging && !isDragging`）」单独列为一类、并且排在「选区改变」之前？

**答案**：拖拽过程中 `update` 走 `isDragging → return deco` 分支冻结了装饰。拖拽松手那一刻，`mouseSelectingField` 翻成 `false`，但同一个 transaction 未必携带 `tr.selection`（或携带了但要先于选区处理）。单独识别「拖拽结束」并立即重建，能保证松手后表格按最新光标位置正确切换；排在选区之前是为了不让拖拽尾部的选区抖动先触发一次错误的重建。

---

### 4.2 TableWidget DOM 构建

#### 4.2.1 概念说明

`TableWidget` 继承自 `WidgetType`（[u3-l1](u3-l1-widgettype-and-decoration-replace.md)），是渲染态下「代替整段表格文本」的那个 DOM 生产者。它做的事情很纯粹：

- `toDOM()`：用 `TableData` 从零搭出一个 `<div><table>`，含 `<thead>`（表头）和 `<tbody>`（数据行），并把每列对齐写进单元格的内联样式。
- `eq()`：判断两个 widget 是否「等价」，等价则 CodeMirror 复用旧 DOM、不重新 `toDOM`。
- `ignoreEvent()`：返回 `false`，表示「不要吃掉鼠标事件」——让点击穿透给 CodeMirror，从而把光标放进表格、触发选区变化、切换到源码态。

#### 4.2.2 核心流程

`toDOM` 的 DOM 结构：

```text
div.cm-table-widget
└─ table
   ├─ thead
   │  └─ tr
   │     └─ th × N        （N = 表头列数）
   │        innerHTML = renderInlineMarkdown(header)
   │        style.textAlign = alignments[i]   （仅当非 null）
   └─ tbody
      └─ tr × M            （M = 数据行数）
         └─ td × N
            innerHTML = renderInlineMarkdown(cell)
            style.textAlign = alignments[idx]  （仅当非 null）
```

关键点：

- **对齐是「按列」应用**：表头和数据行都用同一个 `alignments[idx]`，第 `idx` 列的对齐对 `th` 和 `td` 一致。
- **`null` 对齐不加样式**：`alignments[idx]` 为 `null` 时跳过 `style.textAlign`，沿用浏览器默认（左对齐）。这与 `TableData` 里 `null` 表示「未指定对齐」的语义一致（[u4-l1](u4-l1-table-parser-and-serializer.md)）。
- **内容来自 `renderInlineMarkdown`**：单元格文本先经它转成 HTML 字符串，再赋给 `innerHTML`。

#### 4.2.3 源码精读

类定义与构造（[src/widgets/tableWidget.ts:14-17](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableWidget.ts#L14-L17)）——用 `readonly` 参数属性直接把 `data` 存为字段：

```ts
export class TableWidget extends WidgetType {
  constructor(readonly data: TableData) { super(); }
```

`eq`（[src/widgets/tableWidget.ts:22-40](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableWidget.ts#L22-L40)）：逐一比较表头数、行数、每个表头与对齐、每个单元格。任意一处不同就返回 `false`。这正符合 [u3-l1](u3-l1-widgettype-and-decoration-replace.md) 的心法——「`eq` 比较的字段集合要等于决定 `toDOM` 输出的字段集合」：`toDOM` 只用 `data`，所以 `eq` 必须完整比较 `data`。

`toDOM` 的表头部分（[src/widgets/tableWidget.ts:52-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableWidget.ts#L52-L65)）：

```ts
const thead = document.createElement('thead');
const headerRow = document.createElement('tr');

this.data.headers.forEach((header, idx) => {
  const th = document.createElement('th');
  th.innerHTML = renderInlineMarkdown(header);
  if (this.data.alignments[idx]) {
    th.style.textAlign = this.data.alignments[idx]!;   // ! 非空断言：if 已保证非 null
  }
  headerRow.appendChild(th);
});
```

数据行部分（[src/widgets/tableWidget.ts:68-83](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableWidget.ts#L68-L83)）几乎同构，只是 `th` 换成 `td`、遍历 `rows`。两段合起来产出的 DOM 由 `.cm-table-widget` 等 CSS 类负责外观（[src/theme/default.ts:193-211](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L193-L211)）——注意主题里特意注释「不要加 margin」，因为 margin 会让 widget 高度与源码态不一致，导致 CodeMirror 的点击坐标计算偏移。

`ignoreEvent`（[src/widgets/tableWidget.ts:96-98](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableWidget.ts#L96-L98)）固定返回 `false`，与 `MathWidget` 一致（[u3-l1](u3-l1-widgettype-and-decoration-replace.md)）：让点击交给 CodeMirror，从而进入编辑。工厂函数 `createTableWidget` 见 [src/widgets/tableWidget.ts:104-106](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableWidget.ts#L104-L106)。

#### 4.2.4 代码实践

实践目标：验证 `TableWidget` 正确构建 `thead/tbody` 并应用对齐样式。

操作步骤：

1. 阅读 [src/widgets/\_\_tests\_\_/tableWidget.test.ts:61-74](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/tableWidget.test.ts#L61-L74)，它用一个 `alignments: ['left','center','right']` 的数据断言三个 `th` 和三个 `td` 的 `style.textAlign`。
2. 运行：`npx vitest run src/widgets/__tests__/tableWidget.test.ts`。

需要观察的现象：

- 每个 `th`/`td` 的 `style.textAlign` 与 `alignments` 对应位置一一相等。
- 对照 [src/widgets/\_\_tests\_\_/tableWidget.test.ts:86-93](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/tableWidget.test.ts#L86-L93)：相同数据 `eq` 返回 `true`、不同数据返回 `false`。

预期结果：全部通过。如果想亲手验证 `null` 对齐的行为，可在测试文件里临时加一个 `alignments: [null]` 的用例，断言对应 `th` 的 `style.textAlign` 为空字符串 `''`（即未设置）。

> 这是「源码阅读型实践」：不修改源码，只读测试、跑测试、据断言理解行为。

#### 4.2.5 小练习与答案

**练习 1**：`th.style.textAlign = this.data.alignments[idx]!` 里的 `!` 是什么意思？去掉会怎样？

**答案**：`!` 是 TypeScript 的非空断言，告诉编译器「这里不是 `null`/`undefined`」。因为 `alignments` 的元素类型是 `'left'|'center'|'right'|null`，直接赋给 `style.textAlign`（期望 `string`）类型不匹配。前面 `if (this.data.alignments[idx])` 已经排除了 `null`，所以断言是安全的。去掉 `!` 会导致类型检查报错（可运行 `npm run typecheck` 复现）。

**练习 2**：`TableWidget` 没有实现 `updateDOM`，只实现了 `eq`。当表格数据变化时会发生什么？

**答案**：CodeMirror 比较新旧 widget 的 `eq`。数据变了 → `eq` 返回 `false` → CodeMirror 丢弃旧 DOM、调用新 widget 的 `toDOM` 重建。对表格这种「整体重建成本可接受」的场景，只实现 `eq` 是最简且正确的做法。

---

### 4.3 renderInlineMarkdown

#### 4.3.1 概念说明

`renderInlineMarkdown` 是一个**纯函数**，输入单元格文本、输出 HTML 字符串。它是 `TableWidget` 用的轻量内联渲染器，不是完整的 Markdown 引擎——只覆盖表格单元格里常见的几种格式：粗体、斜体、删除线、高亮、行内代码。

> 为什么不复用 Lezer 语法树来渲染单元格？因为单元格内的文本是 `TableData` 里**已切分好的纯字符串**，不再属于语法树。对一个短字符串，用一个正则驱动的轻函数远比再走一遍解析树便宜。代价是它**不支持**链接、图片、行内公式等——这些在表格单元格里也不常见。

#### 4.3.2 核心流程

处理顺序很关键，遵循「先转义、再按优先级替换」：

```text
1. escapeHtml(text)        # 先把 & < > " 转义，杜绝 XSS
2. 抽出行内代码 → 暂存到 codeBlocks，原位换成占位符 %%CODE{i}%%
3. 替换 **text** / __text__ → <strong class="cm-strong">
4. 替换  *text*  / _text_   → <em class="cm-emphasis">
5. 替换 ~~text~~            → <del class="cm-strikethrough">
6. 替换 ==text==            → <mark class="cm-highlight">
7. 把占位符还原回 <code class="cm-code">…</code>
return HTML 字符串
```

两个设计要点：

- **行内代码优先级最高**：先把它「挖走」换成占位符，这样后续的粗体/斜体正则**不会**作用到代码内容上（比如 `` `**not bold**` `` 里的 `**` 不应被解析）。最后再把代码还原。这是经典的「保护区」技巧。
- **粗体在斜体之前**：`**text**` 必须先于 `*text*` 匹配，否则 `**` 会被当成两个单 `*`。同理 `__` 先于 `_`。

#### 4.3.3 源码精读

`escapeHtml`（[src/utils/inlineMarkdown.ts:5-11](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/inlineMarkdown.ts#L5-L11)）转义四类字符。注意 `&` 必须最先转义，否则会把后续插入的实体再次转错。

主体（[src/utils/inlineMarkdown.ts:20-49](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/inlineMarkdown.ts#L20-L49)）：

```ts
export function renderInlineMarkdown(text: string): string {
  let result = escapeHtml(text);

  // 行内代码（最高优先级——保护其内容不被后续替换）
  const codeBlocks: string[] = [];
  result = result.replace(/`([^`]+)`/g, (_match, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(`<code class="cm-code">${code}</code>`);
    return `%%CODE${idx}%%`;
  });

  result = result.replace(/\*\*(.+?)\*\*/g, '<strong class="cm-strong">$1</strong>');
  result = result.replace(/__(.+?)__/g, '<strong class="cm-strong">$1</strong>');
  result = result.replace(/\*(.+?)\*/g, '<em class="cm-emphasis">$1</em>');
  result = result.replace(/_(.+?)_/g, '<em class="cm-emphasis">$1</em>');
  result = result.replace(/~~(.+?)~~/g, '<del class="cm-strikethrough">$1</del>');
  result = result.replace(/==(.+?)==/g, '<mark class="cm-highlight">$1</mark>');

  // 还原代码块
  result = result.replace(/%%CODE(\d+)%%/g, (_match, idx) => codeBlocks[Number(idx)]);
  return result;
}
```

注意输出用的 CSS 类（`cm-strong`、`cm-emphasis`、`cm-code` 等）正是 [u2-l5](u2-l5-markdown-style-plugin.md) 里 `markdownStylePlugin` 给内容节点套的同名类——这样表格单元格里的粗体和正文里的粗体外观一致。

安全性：因为**先 `escapeHtml` 再替换**，输入 `<script>alert(1)</script>` 会先变成 `&lt;script&gt;…`，之后的正则匹配不到 `<script>`，所以 `innerHTML` 赋值是安全的。这一点有测试保障（[src/widgets/\_\_tests\_\_/tableWidget.test.ts:100-114](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/tableWidget.test.ts#L100-L114)、[src/utils/\_\_tests\_\_/inlineMarkdown.test.ts:51-61](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/inlineMarkdown.test.ts#L51-L61)）。

#### 4.3.4 代码实践

实践目标：验证「行内代码保护」与「处理顺序」两个关键行为。

操作步骤：

1. 阅读 [src/utils/\_\_tests\_\_/inlineMarkdown.test.ts:69-73](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/inlineMarkdown.test.ts#L69-L73)，输入 `` `**not bold**` ``，断言输出里 `**` 原样保留在 `<code>` 内。
2. 运行：`npx vitest run src/utils/__tests__/inlineMarkdown.test.ts`。

需要观察的现象：

- `` `**not bold**` `` → `<code class="cm-code">**not bold**</code>`，`**` 没有被当成粗体。
- `**bold** and *italic*`（[src/utils/\_\_tests\_\_/inlineMarkdown.test.ts:63-67](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/inlineMarkdown.test.ts#L63-L67)）→ 粗体和斜体各自正确，证明粗体先于斜体匹配。

预期结果：全部通过。

进阶（可选）：在测试文件里**临时**加一条用例，输入 `a_b_c`（单词下划线），观察它会被解析成斜体 `a<em>b</em>c`。这暴露了 `_` 斜体的一个已知边界——下划线词内分隔符会被误判。思考：为什么这个「缺陷」在表格单元格里通常可以接受？（提示：单元格内容短、且 `_` 较少用作变量名分隔。）

> 临时改动仅为本地观察，请勿提交；本讲要求不修改源码与正式测试。

#### 4.3.5 小练习与答案

**练习 1**：为什么行内代码要用「占位符暂存」而不是直接替换？

**答案**：为了让代码内容**豁免**后续的粗体/斜体等替换。如果直接替换成 `<code>…</code>`，那么代码里的 `**`、`_` 仍会被后续正则命中，破坏代码原样性。先换成不含特殊符号的占位符 `%%CODE{i}%%`，等所有替换结束再还原，就保证了代码内容分毫不差。

**练习 2**：`renderInlineMarkdown('hello world')` 返回什么？`renderInlineMarkdown('a & b')` 呢？

**答案**：前者返回 `'hello world'`（无格式，原样）；后者返回 `'a &amp; b'`——即使没有 Markdown 格式，`escapeHtml` 仍会转义 `&`。所以这个函数**总是**返回「安全 HTML」，调用方可以放心赋给 `innerHTML`。

---

## 5. 综合实践

把三个模块串起来，完成一次「端到端」追踪。

**任务**：给定下面这段文档（注意第二列表头是粗体、第三列右对齐）：

```markdown
| Name | **Role** | Score |
|------|----------|------:|
| Alice | *dev* | `99` |
| Bob   | ~~mgr~~ | 80 |
```

请按顺序回答并**用源码行号佐证**：

1. **解析**：`parseMarkdownTable` 会产出怎样的 `TableData`？写出 `headers`、`alignments`、`rows` 的值（提示：`alignments` 第三项是 `'right'`，前两项是 `null`）。
2. **渲染态 DOM**：光标在文档末尾时，`TableWidget.toDOM` 产出的表头第二列 `th` 的 `innerHTML` 是什么？第三列表头和数据行的 `style.textAlign` 各是什么？（提示：见 [src/widgets/tableWidget.ts:55-62](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableWidget.ts#L55-L62) 与 [src/utils/inlineMarkdown.ts:32](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/inlineMarkdown.ts#L32)）
3. **切换**：把光标移到表格第一行（比如位置 5），`tableField.update` 会走哪条分支重建？`buildTableDecorations` 这次会产出几条装饰、分别是什么类型？（提示：见 [src/plugins/table.ts:80-91](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/table.ts#L80-L91) 与 [src/plugins/table.ts:44-51](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/table.ts#L44-L51)）
4. **验证**：在 `src/plugins/__tests__/table.test.ts` 末尾仿照现有用例，**临时**加一个测试：构造上述文档、光标置于末尾，断言存在 widget 装饰；再 dispatch 选区到表格内，断言存在 `cm-table-source` 装饰。运行 `npx vitest run src/plugins/__tests__/table.test.ts` 确认通过（验证后删除该临时用例）。

**参考要点**：

- 第 2 问：表头第二列 `innerHTML` 为 `<strong class="cm-strong">Role</strong>`；第三列表头与两行数据单元格的 `style.textAlign` 均为 `'right'`。
- 第 3 问：选区改变命中 `if (tr.selection)` 分支重建；源码态对表格覆盖的每一行各产一条 `Decoration.line({ class: 'cm-table-source' })`，共 4 条（表头行、分隔行、两行数据），没有 widget 装饰。

## 6. 本讲小结

- `tableField` 是一个 `StateField<DecorationSet>`，渲染态用**块级** `Decoration.replace({widget, block:true})`——块级装饰必须进 `EditorState`，这正是用 `StateField` 而非 `ViewPlugin` 的根因。
- 显示哪一面由 `shouldShowSource` 决定：光标触碰表格 → 源码态（逐行 `Decoration.line` 加 `cm-table-source`）；否则渲染态（`TableWidget`）。
- `tableField.update` **手动内联**了「结构变化→拖拽结束→拖拽中→选区→无」的判定，等价于 `checkUpdateAction`，与 `blockMathField` 同属「块级 StateField 内联判定」模式。
- 非法表格（`parseMarkdownTable` 返回 `null`）直接跳过，退化为纯文本，不加任何装饰。
- `TableWidget.toDOM` 搭 `thead/tbody`，按列把对齐写进 `style.textAlign`（`null` 不写），`eq` 完整比较 `TableData` 以避免无谓重建，`ignoreEvent` 返回 `false` 让点击可进入编辑。
- `renderInlineMarkdown` 是单元格专用的轻量渲染器：**先 `escapeHtml` 再按优先级替换**，行内代码用占位符保护、最高优先级，输出安全 HTML 字符串。

## 7. 下一步学习建议

本讲的表格是**只读**的——光标进入时显示带底色的源码，你可以原样编辑文本，但编辑结果不会结构化同步回 `TableData`，也不会重新对齐。下一讲 [u4-l3 tableEditorPlugin：可编辑表格与 source mode 切换](u4-l3-table-editor-plugin.md) 解决的就是这个问题：

- 它用 `contenteditable` 的单元格让用户**逐格编辑**，失焦/回车时通过 `serializeMarkdownTable` 把改动 `view.dispatch` 写回文档（正好接上 [u4-l1](u4-l1-table-parser-and-serializer.md) 的序列化器）。
- 它引入 `setTableSourceMode` 这个 `StateEffect` 与 `tableSourceModeField`，按表格区间跟踪每张表独立的源码态。

建议在进入 u4-l3 前，先确保你能独立回答本讲「综合实践」的第 3 问——理解「装饰如何在两种态之间切换」是理解可编辑表格「何时提交」的前提。若对块级装饰为何必须用 `StateField` 还有疑虑，可回看 [u3-l3](u3-l3-block-math-and-cache.md) 的 `blockMathField`，它是同一模式的另一个范例。
