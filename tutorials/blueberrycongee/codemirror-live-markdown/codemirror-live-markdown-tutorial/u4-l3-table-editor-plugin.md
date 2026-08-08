# tableEditorPlugin：可编辑表格与 source mode 切换

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `tableEditorPlugin` 与上一讲只读 `tableField`（u4-l2）的根本区别：可编辑表格的源码态由**显式按钮**触发，而非光标位置驱动。
- 读懂 `setTableSourceMode`（StateEffect）与 `tableSourceModeField`（StateField）这一「消息 + 状态」搭档，并理解它如何用一张区间表跟踪多张表的源码态。
- 掌握 `rangesOverlap` / `addRange` / `removeRange` 三个区间工具如何管理「哪些表处于源码态」。
- 看懂 `contenteditable` 单元格在「渲染态 / 编辑态」之间的双视图切换，以及 focus / blur / Enter 三个提交时机。
- 完整追踪一次单元格编辑的数据流：聚焦 → 输入 → 失焦 → `commitCell` → `serializeMarkdownTable` → `view.dispatch`，最终把改动写回 Markdown 文档。

## 2. 前置知识

本讲建立在以下已学内容之上（不再重复展开）：

- **u4-l1**：`parseMarkdownTable` 把 GFM 表格文本解析成 `TableData`（`headers` / `alignments` / `rows`），`serializeMarkdownTable` 把它反向序列化成规范表格文本。本讲会直接复用这两个纯函数。
- **u4-l2**：只读表格 `tableField` 用 `shouldShowSource` 决定「渲染态（replace + Widget）」还是「源码态（逐行 `cm-table-source`）」。本讲的核心就是**替换掉这个决策来源**。
- **u2-l1 / u3-l1**：StateField 的值会进入 EditorState 快照；带 `block: true` 的 replace 装饰会改变行高，所以表格这类块级装饰必须用 StateField；`WidgetType` 三件套 `eq` / `toDOM` / `ignoreEvent`。

补充两个本讲会用到的 CodeMirror 6 概念：

- **StateEffect（状态效果）**：一种「只携带消息、自己不存状态」的对象。它搭在 Transaction 的 `effects` 通道里传递。真正把效果落地的，是某个 StateField 在自己的 `update` 里识别它并改写自己的值。你可以把它理解成「给 StateField 发的一条指令」。
- **`view.dispatch` 的 `changes` 字段**：形如 `{ from, to, insert }`，表示把文档 `[from, to)` 区间替换为 `insert` 字符串。这是本讲把编辑写回文档的最终出口。

> 一句话区分：上一讲是「光标碰到表格就显示源码」，本讲是「表格始终渲染成可点击编辑的表格，只有按按钮才切到源码」。这两套机制互不依赖、可以二选一。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [src/plugins/tableEditorEffects.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditorEffects.ts) | 定义 `setTableSourceMode` StateEffect 及其载荷类型 | 消息定义 |
| [src/plugins/tableEditor.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts) | `tableSourceModeField`（跟踪源码态）、`tableEditorField`（产出装饰）、`tableEditorPlugin`（聚合入口）、三个区间工具 | 决策与编排中枢 |
| [src/widgets/tableEditorWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts) | `TableEditorWidget`（可编辑表格 DOM）、`TableSourceToggleWidget`（源码态切换按钮）、`createEditableCell`（contenteditable 单元格） | DOM 渲染与交互 |
| [src/utils/tableParser.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableParser.ts) | `parseMarkdownTable`、`TableData` | 解析（u4-l1） |
| [src/utils/tableSerializer.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableSerializer.ts) | `serializeMarkdownTable` | 写回序列化（u4-l1） |
| [src/plugins/__tests__/tableEditor.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/tableEditor.test.ts) | 三个用例：光标在表内仍渲染、Effect 切换源码态、编辑单元格写回 | 行为佐证与测试范本 |

## 4. 核心概念与源码讲解

### 4.1 setTableSourceMode 与 tableSourceModeField：显式跟踪源码态

#### 4.1.1 概念说明

上一讲的只读表格用 `shouldShowSource`（读光标/选区）决定显示哪一面。这种「光标进入就显示源码」的行为，对**可编辑表格**是个障碍：用户想点击单元格直接编辑，光标必然在表格里，若此时表格坍缩成源码文本，就没法点单元格了。

所以可编辑表格需要一套**与光标位置无关**的源码态开关：

- 默认始终渲染成可编辑表格。
- 用户点工具栏的「MD」按钮，才把某一张表切到源码态（显示原始 Markdown 文本）。
- 在源码态点「Table」按钮，再切回渲染态。

这套开关用「StateEffect（消息）+ StateField（状态）」实现：

- `setTableSourceMode` 是 **StateEffect**，载荷是 `{ from, to, showSource }`，表示「把 `[from, to]` 这张表设为/取消源码态」。它只是一条消息，不存数据。
- `tableSourceModeField` 是 **StateField**，它的值是一张「当前处于源码态的表格区间列表」。它在 `update` 里识别 `setTableSourceMode` 消息，据此增删区间。

#### 4.1.2 核心流程

```
按钮点击
   │  view.dispatch({ effects: setTableSourceMode.of({from, to, showSource}) })
   ▼
Transaction 携带 effect 进入 StateField.update
   │  tableSourceModeField.update 识别该 effect
   ▼
showSource ? addRange(ranges, {from,to})  ← 进入源码态
           : removeRange(ranges, {from,to}) ← 退出源码态
   │
   ▼  返回新的区间数组作为字段新值
tableEditorField 读到 effect，重建装饰 → 选渲染态或源码态分支
```

关键点：源码态信息**存在 StateField 的值里**（区间数组），而不是存成 widget 内部变量。这样它才能随 Transaction 流转、随文档变化自动平移（见 4.1.3）。

#### 4.1.3 源码精读

先看消息定义。载荷类型与 StateEffect 各只有一处：

[src/plugins/tableEditorEffects.ts:3-9](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditorEffects.ts#L3-L9) 定义了 `TableSourceModeToggle` 接口（`from` / `to` / `showSource`）和 `setTableSourceMode` 这个 StateEffect。注意它没有默认值，是一个「空定义」——它的全部意义就是当消息载体。

再看状态字段本身。`tableSourceModeField` 的值类型是 `TableSourceRange[]`（一组区间），`create` 返回空数组（初始没有任何表在源码态）：

[src/plugins/tableEditor.ts:48-68](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts#L48-L68) 是核心。它的 `update` 做两件事：

1. **先把所有已有区间随文档变化平移**：`tr.changes.mapPos(range.from, 1)` / `mapPos(range.to, -1)`。这样当表格上方插入文字导致表格整体后移时，记录的源码态区间也会跟着移动，不会错位。
2. **再处理本事务携带的 `setTableSourceMode` 效果**：对每条 effect，`showSource` 为真则 `addRange`，为假则 `removeRange`。注意 `from`/`to` 也经过 `mapPos` 映射，保证用的是「变化后」的坐标。

```ts
// 简化示意（非完整源码）
update(ranges, tr) {
  let next = ranges.map(r => ({
    from: tr.changes.mapPos(r.from, 1),
    to:   tr.changes.mapPos(r.to, -1),
  }));
  for (const effect of tr.effects) {
    if (!effect.is(setTableSourceMode)) continue;
    const { from, to, showSource } = effect.value;
    next = showSource ? addRange(next, mapped) : removeRange(next, mapped);
  }
  return next;
}
```

> 易错点：这里**没有**依赖 `mouseSelectingField`，也**没有**读选区。这正是它和只读 `tableField` 的分水岭——源码态完全由显式 effect 驱动。

#### 4.1.4 代码实践

**实践目标**：亲手验证「光标在表格内，表格依然保持渲染态」这一与只读版本相反的行为。

**操作步骤**：

1. 打开测试文件 [src/plugins/\_\_tests\_\_/tableEditor.test.ts:24-40](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/tableEditor.test.ts#L24-L40)，读懂 `createTestView(simpleTable, 5)`（光标定在第 5 个字符，即表格内部）。
2. 运行该用例：

   ```bash
   npm test -- tableEditor
   ```

3. 对照同一张表，若换成只读 `tableField`，光标在表内应进入源码态（见 u4-l2）。本插件则断言 `hasWidget === true` 且 `hasSourceClass === false`。

**需要观察的现象**：测试通过，且断言确认光标在表内时只出现 replace widget、不出现 `cm-table-source` 类。

**预期结果**：`should keep table widget rendered when cursor is inside table` 通过。

> 待本地验证：在你自己的环境跑一遍 `npm test -- tableEditor` 确认三项用例全绿。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tableSourceModeField.update` 要先用 `tr.changes.mapPos` 平移区间，再处理 effect？如果只做第二步会出什么问题？

**答案**：因为文档变化（如表格前插入文字）会让表格整体位移。若不平移，记录的源码态区间会指向旧位置，导致「明明切过源码态的表，改了几个字后状态丢失或错位」。`mapPos` 让区间随文本一起移动，保持附着在正确的表上。

**练习 2**：`tableSourceModeField` 的值为什么是「区间数组」而不是「单个布尔」？

**答案**：一个文档里可能有多张表，每张表都要独立记录是否处于源码态。布尔只能表达全局一个状态，无法区分多表；区间数组则能精确描述「第 X 到第 Y 这张表在源码态」。

---

### 4.2 区间管理：rangesOverlap / addRange / removeRange

#### 4.2.1 概念说明

`tableSourceModeField` 的值是区间数组，那么对它的增删查就需要三个纯函数：

- `rangesOverlap(a, b)`：判断两个区间是否相交（含端点）。
- `addRange`：加入一个新区间，若与已有区间相交则保持不变（避免重复）。
- `removeRange`：删除所有与目标区间相交的区间。

这三个函数都是**无副作用纯函数**，可独立测试，也是本讲「区间管理」最小模块的骨架。

#### 4.2.2 核心流程

相交判定用的是经典的闭区间重叠公式：

\[
\text{overlap}(a, b) \iff a.\text{from} \le b.\text{to} \;\land\; a.\text{to} \ge b.\text{from}
\]

这与上一讲 `shouldShowSource` 里判定「选区是否触碰目标区间」用的是**同一个公式**（回忆 `range.from <= to && range.to >= from`）。这是一个值得记住的模式：判断两个闭区间是否相交，就是「各自的左端都不超过对方的右端」。

- `addRange`：若 `ranges.some(r => rangesOverlap(r, next))` 为真（已有重叠），原样返回，不重复加；否则 `[...ranges, next]`。
- `removeRange`：`ranges.filter(r => !rangesOverlap(r, target))`，把所有相交的都滤掉。
- `isTableInSourceMode`：`ranges.some(r => r.from <= to && r.to >= from)`，查询某张表是否落在任一源码态区间内。

#### 4.2.3 源码精读

三个工具与一个查询函数集中在文件顶部：

[src/plugins/tableEditor.ts:25-46](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts#L25-L46) 定义了 `rangesOverlap`、`removeRange`、`addRange`、`isTableInSourceMode` 四个函数。注意：

- `addRange` 的「相交即不重复加」保证同一张表多次派发 `showSource: true` 不会产生重复区间。
- `removeRange` 的「相交即删」意味着它不是精确删某个区间，而是删掉所有与目标重叠的——这对「按表格范围取消源码态」足够用。

`isTableInSourceMode` 在装饰构建时被调用，决定每张表走哪条分支（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：脱离 CodeMirror，单独验证三个区间函数的行为，建立直觉。

**操作步骤**（源码阅读型）：

1. 阅读 [src/plugins/tableEditor.ts:25-38](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts#L25-L38)。
2. 在草稿纸上手算以下序列：
   - 初始 `ranges = []`。
   - `addRange([], {0, 20})` → 结果？
   - `addRange([{0,20}], {0, 20})` → 结果？（提示：相交，不重复加）
   - `addRange([{0,20}], {30, 40})` → 结果？
   - `removeRange([{0,20},{30,40}], {5, 10})` → 结果？（提示：删掉所有相交的）

**预期结果**：

- 第二步 → `[{0,20}]`
- 第三步 → `[{0,20}]`（不变）
- 第四步 → `[{0,20},{30,40}]`
- 第五步 → `[{30,40}]`（`{0,20}` 与 `{5,10}` 相交被删，`{30,40}` 不相交保留）

#### 4.2.5 小练习与答案

**练习 1**：若文档中有两张相邻表（区间分别是 `{0,20}` 和 `{21,40}`），对第二张派发 `showSource: true`，`addRange` 会把它误并进第一张吗？

**答案**：不会。`rangesOverlap({0,20}, {21,40})` 为 `0 <= 40 && 20 >= 21` → `20 >= 21` 为假，不相交。所以两张表各自独立记录，第二张会被独立加入，结果是 `[{0,20},{21,40}]`。

**练习 2**：`removeRange` 为什么用「删除所有相交区间」而不是「精确匹配 from/to 再删」？

**答案**：因为经过 `mapPos` 平移后，区间坐标会随文档变化而变动，调用方手上的 `{from, to}` 不一定与记录里的完全一致。用「相交即删」更鲁棒——只要大致覆盖到目标表格就能清掉它的源码态，容错性更好。

---

### 4.3 buildTableDecorations 与 tableEditorField：两条渲染分支

#### 4.3.1 概念说明

有了源码态字段，还需要一个 StateField 把它翻译成装饰。`tableEditorField`（`StateField<DecorationSet>`）就是这层翻译：它遍历语法树找 `Table` 节点，查 `isTableInSourceMode`，然后走两条分支之一：

- **渲染态**（不在源码态）：`Decoration.replace({ widget, block: true })` 套 `TableEditorWidget`，把表格原文替换成可编辑 DOM。
- **源码态**：在表格起始处插一个 `TableSourceToggleWidget`（「Table」按钮），并给表格每一行加 `cm-table-source` 行级类，让原始 Markdown 文本可见且带底色。

和上一讲只读版一样的「块级装饰必须用 StateField」理由仍然成立：`block: true` 的 replace 会改变行高，必须进 EditorState 才能参与视口测量。

#### 4.3.2 核心流程

```
tableEditorField.update(deco, tr)
   │  触发重建的条件：docChanged / reconfigured / 携带 setTableSourceMode effect
   │  （注意：不包含 selectionSet —— 这是与只读版的关键区别）
   ▼
buildTableDecorations(state)
   │  遍历 syntaxTree 找 'Table' 节点
   │  parseMarkdownTable 解析；解析失败(null)跳过
   │  isTableInSourceMode ?
   ▼
   ├─ 否 → Decoration.replace(block:true) + TableEditorWidget   （可编辑渲染态）
   └─ 是 → Decoration.widget(block:true) 放切换按钮
           + 每行 Decoration.line('cm-table-source')           （源码态）
```

#### 4.3.3 源码精读

[src/plugins/tableEditor.ts:70-109](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts#L70-L109) 是 `buildTableDecorations`。几个要点：

- 第 72 行 `state.field(tableSourceModeField)` **不带**第二参数 `false`——它假定该字段一定存在。这就是为什么 `tableEditorPlugin()` 必须把两个 field 一起打包（见 4.3 末尾）。
- 渲染态分支（第 85-90 行）：用 `createTableEditorWidget(tableData, node.from, node.to)`，把表格的源码区间 `from/to` 一并传给 widget——后者提交时要用它定位写回（见 4.4）。
- 源码态分支（第 93-104 行）：注意它用的是 `Decoration.widget`（零宽插入，第 95 行）**而非** `Decoration.replace`。也就是说源码态下表格原文**没有被替换掉**，只是额外插了一个「Table」按钮在表格开头，并给每行染色。这样用户能看到并直接编辑原始 Markdown 行。

再看 `tableEditorField` 本身的更新触发条件：

[src/plugins/tableEditor.ts:111-129](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts#L111-L129)。注意第 117-121 行的判定：

```ts
if (tr.docChanged || tr.reconfigured ||
    tr.effects.some(e => e.is(setTableSourceMode))) {
  return buildTableDecorations(tr.state);
}
return deco;
```

**这里没有 `tr.selectionSet`**——这是与只读 `tableField`（u4-l2 会在选区变化时重建）最大的不同。可编辑表格刻意**不响应光标移动**，所以你把光标移进表格、在表里点来点去，表格都不会坍缩。

最后是插件聚合入口：

[src/plugins/tableEditor.ts:131-135](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts#L131-L135)。`tableEditorPlugin()` 返回 `[tableSourceModeField, tableEditorField]`，并在末尾 re-export `setTableSourceMode` 供外部派发。两个 field 必须同时存在，否则 `buildTableDecorations` 读取 `tableSourceModeField` 时会抛错。

> 与 u4-l2 只读版的对照（最值得记住的一张表）：

| 维度 | `tableField`（u4-l2 只读） | `tableEditorField`（本讲可编辑） |
|------|--------------------------|--------------------------------|
| 源码态触发 | `shouldShowSource`（光标/选区触碰） | `setTableSourceMode` Effect（显式按钮） |
| 光标进入表格 | 切到源码态 | **保持渲染态**（可继续编辑） |
| 依赖 `mouseSelectingField` | 是（防拖拽闪烁） | 否 |
| 重建触发含 `selectionSet` | 是 | **否** |
| 单元格 | 只读渲染 | `contenteditable` 可编辑 |
| 编辑写回文档 | 否 | `serialize` → `dispatch` 写回 |

#### 4.3.4 代码实践

**实践目标**：通过测试验证「Effect 切换源码态」会同时产生切换按钮 widget 与逐行源码类。

**操作步骤**：

1. 阅读 [src/plugins/\_\_tests\_\_/tableEditor.test.ts:42-74](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/tableEditor.test.ts#L42-L74)。该用例先用 `syntaxTree.iterate` 找到 `Table` 节点的 `from/to`，再派发：

   ```ts
   view.dispatch({ effects: setTableSourceMode.of({ from, to, showSource: true }) });
   ```

2. 随后断言 `hasSourceClass === true`（出现 `cm-table-source`）且 `hasWidget === true`（出现切换按钮 widget）。

**需要观察的现象**：派发 effect 后，装饰里**同时**出现源码行类与切换按钮 widget——这正是源码态分支「按钮 + 逐行染色」的体现。

**预期结果**：`should toggle to source mode via effect` 通过。

> 思考题（不计入断言）：为什么切换到源码态后 `hasWidget` 仍是 `true`？因为切换按钮 `TableSourceToggleWidget` 本身就是一个 widget。

#### 4.3.5 小练习与答案

**练习 1**：源码态分支用 `Decoration.widget` 插按钮，为什么不用 `Decoration.replace`？

**答案**：源码态的目的是让用户**看到并直接编辑原始 Markdown 文本**。若用 `replace` 会把原文隐藏/替换掉，反而看不到源码。`widget` 是零宽插入，只额外加一个按钮，原文行保持原样，再用 `Decoration.line` 给每行染色即可。

**练习 2**：如果把 `tableEditorField.update` 的判定里**加上** `tr.selectionSet`，会产生什么用户体验问题？

**答案**：光标一旦移入表格就会触发重建，而重建走的是「查 `isTableInSourceMode`」——虽然不一定会切源码态，但频繁重建会打断 `contenteditable` 单元格的焦点与编辑连续性，且违背「可编辑表格不随光标坍缩」的设计初衷。

---

### 4.4 TableEditorWidget：contenteditable 单元格与编辑写回

#### 4.4.1 概念说明

这是本讲最关键的两个最小模块：**contenteditable 单元格提交** 与 **序列化写回文档**。

`TableEditorWidget.toDOM` 构建一张真正的 `<table>`，表头和单元格都由 `createEditableCell` 生成——它们是 `contenteditable="true"` 的 `<th>`/`<td>`，可以直接点击编辑。

每个单元格采用**双视图**设计：

- **休息态（未聚焦）**：`innerHTML = renderInlineMarkdown(value)`，显示渲染后的内联格式（`**粗**` 显示为粗体），原始文本存在 `dataset.raw` 里。
- **编辑态（聚焦后）**：`textContent = dataset.raw`，切回原始 Markdown 字符（看到字面的 `**粗**`），方便修改语法。

编辑结果通过 **focus / blur / Enter** 三个时机提交：聚焦切原始、失焦或按 Enter 时调用 `onCommit`，后者经 `commitHeader` / `commitCell` → `cloneTableData` → `serializeMarkdownTable` → `view.dispatch({ changes })` 把整张表重新序列化后写回文档。

#### 4.4.2 核心流程

一次单元格编辑的完整链路（这也是本讲的综合实践主线）：

```
用户点击 td（contenteditable）
   │  focus 事件
   ▼  editing=true；textContent = dataset.raw（显示原始文本）
用户输入（在 contenteditable 内打字，不产生 CodeMirror Transaction）
   │
   ▼
用户按 Enter 或点击别处
   ├─ keydown Enter → preventDefault → commitIfChanged → cell.blur()
   └─ blur 事件    → commitIfChanged（→ onCommit）→ editing=false
                    → innerHTML = renderInlineMarkdown(dataset.raw)（切回渲染态）
   │
   ▼  commitIfChanged：比较 textContent 与 dataset.raw
      ┌─ 相同：什么都不做（onCommit 不触发）
      └─ 不同：更新 dataset.raw，调用 onCommit(nextValue)
   │
   ▼  onCommit → commitCell(rowIndex, colIndex, nextValue)
      cloneTableData(this.data)（深拷贝，避免污染原数据）
      updated.rows[rowIndex][colIndex] = nextValue
      markdown = serializeMarkdownTable(updated)
      view.dispatch({ changes: { from: this.from, to: this.to, insert: markdown } })
   │
   ▼  文档变化 → tableEditorField.update 命中 docChanged → 重建装饰
      重新 parseMarkdownTable 解析新文档 → 生成新 TableEditorWidget
      CodeMirror 调用 newWidget.eq(oldWidget)：数据已变 → false → 重建 DOM
```

#### 4.4.3 源码精读

**（1）contenteditable 单元格与提交时机** —— 这是「contenteditable 单元格提交」最小模块的核心：

[src/widgets/tableEditorWidget.ts:19-64](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L19-L64) 是 `createEditableCell`。逐段看：

- 第 24-29 行：创建 `<th>`/`<td>`，设 `contentEditable = 'true'`、`spellcheck = false`，把原始值存进 `dataset.raw`，初始用 `renderInlineMarkdown(value)` 渲染内联格式。
- 第 33-39 行 `commitIfChanged`：读 `cell.textContent`，与 `dataset.raw` 比较；**只有不同才**更新 `dataset.raw` 并触发 `onCommit`。这避免了「聚焦又原样失焦」时产生无谓的写回。
- 第 41-46 行 focus：进入编辑态，`textContent = dataset.raw`，让你看到并改原始字符。
- 第 48-53 行 blur：`event.stopPropagation()`（阻止冒泡到 CodeMirror），`commitIfChanged()`，置 `editing=false`，再 `innerHTML = renderInlineMarkdown(...)` 切回渲染态。
- 第 55-61 行 keydown Enter：`preventDefault()`（阻止换行），`commitIfChanged()` 后 `cell.blur()`——所以 **Enter 即提交**。

> 注意 `blur` 里调了 `event.stopPropagation()`：因为单元格在 widget DOM 内，而 widget 的 `ignoreEvent` 返回 `true`（见下），这里再保险地阻止冒泡，确保 CodeMirror 不会把失焦误读成编辑器层面的动作。

**（2）提交回调与序列化写回** —— 这是「序列化写回文档」最小模块的核心：

[src/widgets/tableEditorWidget.ts:122-138](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L122-L138) 定义了 `commitHeader` 与 `commitCell`。两者结构相同：

```ts
const commitCell = (rowIndex, colIndex, nextValue) => {
  const updated = cloneTableData(this.data);        // 深拷贝
  updated.rows[rowIndex][colIndex] = nextValue;     // 改一个单元格
  const markdown = serializeMarkdownTable(updated); // 整表重新序列化
  view.dispatch({                                   // 写回文档
    changes: { from: this.from, to: this.to, insert: markdown },
  });
};
```

关键设计：**改一个单元格，重序列化整张表，替换整个表格区间**。`this.from` / `this.to` 是构造 widget 时传入的表格源码区间（来自 `buildTableDecorations` 的 `node.from` / `node.to`）。`cloneTableData`（[第 11-17 行](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L11-L17)）做深拷贝，避免修改 `this.data` 污染后续 `eq` 比较。

**（3）两个切换按钮**：

[src/widgets/tableEditorWidget.ts:100-115](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L100-L115) 是渲染态工具栏的「MD」按钮：点击后把光标 `selection.anchor` 设到表格起点，并派发 `setTableSourceMode.of({ from, to, showSource: true })` 切到源码态。

[src/widgets/tableEditorWidget.ts:184-206](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L184-L206) 是源码态的「Table」按钮（`TableSourceToggleWidget`）：点击派发 `showSource: false` 切回渲染态。两个按钮都调 `event.preventDefault()` + `stopPropagation()`。

**（4）eq 与 ignoreEvent**：

[src/widgets/tableEditorWidget.ts:75-91](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L75-L91) 的 `eq` 逐一比较表头数、行数、每个表头与对齐、每个单元格——全等才复用 DOM。它的意义在于：当光标移动等无关事务触发重建时，若表格数据没变，`eq` 返回 `true`，CodeMirror 复用旧 DOM，**正在编辑的 contenteditable 状态不会丢失**。而真正提交后数据变了，`eq` 返回 `false`，DOM 正确重建。

[src/widgets/tableEditorWidget.ts:174-176](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L174-L176) 与 [第 208-210 行](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L208-L210)：两个 widget 的 `ignoreEvent` 都返回 `true`，告诉 CodeMirror「widget 上的事件我来处理，你不要插手」。这对 contenteditable 至关重要——否则 CodeMirror 会把单元格里的键盘输入解释成编辑器命令，编辑就无法正常进行。

> 对比 u3-l1 的 `MathWidget`：它的 `ignoreEvent` 返回 `false`（放行点击让 CodeMirror 把光标放进公式编辑）。本讲恰恰相反返回 `true`——因为可编辑表格要自己接管单元格事件。`ignoreEvent` 的取值没有定论，取决于 widget 是否需要 CodeMirror 代为处理事件。

#### 4.4.4 代码实践

**实践目标**：在真实（jsdom）环境里完成一次单元格编辑，观察文档被序列化写回。

**操作步骤**：

1. 阅读 [src/plugins/\_\_tests\_\_/tableEditor.test.ts:76-96](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/tableEditor.test.ts#L76-L96)。该用例：
   - 用 `view.dom.querySelector('.cm-table-editor td')` 拿到第一个数据单元格。
   - 设 `cell.textContent = 'Bob'`，再 `dispatchEvent(new Event('blur'))` 模拟失焦。
   - 断言文档变成规整化后的表格（`Alice` → `Bob`，且分隔行被规整成 `| --- | --- |`）。
2. 运行：

   ```bash
   npm test -- tableEditor
   ```

**需要观察的现象**：失焦触发 `commitIfChanged` → `commitCell` → `serializeMarkdownTable` → `dispatch`，文档不仅 `Alice` 变 `Bob`，**整张表都被规整化**（首尾竖线、分隔行格式统一）。这印证了 u4-l1「一次 parse→serialize 往返会规整化表格」的结论。

**预期结果**：文档变为

```
| Name | Age |
| --- | --- |
| Bob | 25 |
```

测试 `should update markdown when editing a cell` 通过。

#### 4.4.5 小练习与答案

**练习 1**：`commitIfChanged` 为什么先比较 `textContent` 与 `dataset.raw`，相同就直接 return？

**答案**：用户经常「点进单元格又不改任何东西就移走焦点」。若每次失焦都无脑提交，会生成大量值相同的 `dispatch`，触发不必要的文档变化与装饰重建，浪费性能且可能打断其他编辑。先比较、不同才提交，是无谓写回的闸门。

**练习 2**：为什么 `commitCell` 要先 `cloneTableData` 再改，而不是直接 `this.data.rows[...][...] = nextValue`？

**答案**：`this.data` 是构造 widget 时传入的、用于 `eq` 比较的原始数据。若直接改它，会污染 `eq` 的判断依据，导致「数据其实变了但 eq 仍认为相等」的错误复用。深拷贝保证改的是副本，原始 `this.data` 不变。

**练习 3**：编辑一个单元格却替换了**整张表**的区间。这种「以表为单位写回」有什么好处和代价？

**答案**：好处是简单可靠——不必精确计算单个单元格在原文里的字符区间（考虑对齐、转义竖线后非常困难），整表重序列化一定一致；同时顺便规整化表格格式。代价是每次单元格编辑都重写整张表的文本，对超大表格有性能成本；且若用户手动写过特殊格式，重序列化会改写它。

---

## 5. 综合实践

**任务**：用一张表完整梳理「一次单元格编辑的数据流」，把本讲四个最小模块串起来。

请准备一张含一张表格的文档（可直接用测试里的 `simpleTable`），在草稿纸上按下面的步骤，**列出每一步触发的是哪个回调、产生的是 Effect 还是 changes、文档与装饰如何变化**：

| 步骤 | 用户动作 | 触发的回调 / 事件 | 产生的 Transaction 内容 | 对 State 的影响 | 最终文档 / 装饰变化 |
|------|----------|-------------------|------------------------|------------------|---------------------|
| 1 | 点击某 `td` | `createEditableCell` 的 `focus` | 无 dispatch | 无 | 单元格 `textContent` 切为 `dataset.raw`（显示原始文本） |
| 2 | 输入新内容 | contenteditable 内部 | 无（widget `ignoreEvent:true`，不进 CodeMirror） | 无 | 仅 DOM 文本变化 |
| 3 | 按 Enter | `keydown` Enter → `commitIfChanged` → `cell.blur()` | 见步骤 4 | — | — |
| 4 | （失焦）`blur` | `commitIfChanged` → `onCommit` → `commitCell` | `changes: {from,to,insert}` | `docChanged` | 文档整表被 `serializeMarkdownTable` 结果替换 |
| 5 | dispatch 后 | `tableEditorField.update` 命中 `docChanged` | — | 重建 `DecorationSet` | 重新解析 → 新 `TableEditorWidget`；`eq` 比较 → 数据已变 → 重建 DOM |
| 6 | （对照）点「MD」按钮 | `TableEditorWidget` 工具栏 click | `selection` + `effects: setTableSourceMode(showSource:true)` | `tableSourceModeField` `addRange` | 切到源码态：按钮变「Table」+ 逐行 `cm-table-source` |

**完成后请自查**：

1. 你能否解释步骤 2 里「为什么输入不产生 CodeMirror Transaction」？（提示：`ignoreEvent` 返回 `true`，且 contenteditable 在 widget DOM 内、不在 CodeMirror 的 contentDOM 里。）
2. 你能否指出步骤 4 的 `from`/`to` 来自哪里？（提示：`buildTableDecorations` 把 `node.from`/`node.to` 传给了 `createTableEditorWidget`。）
3. 若步骤 2 后用户直接点别的单元格（没按 Enter 也没失焦过第一个），会发生什么？（提示：焦点转移会让第一个单元格先 `blur`，从而触发提交。）

> 如果想把这张表跑成真实代码，可参照 [src/plugins/\_\_tests\_\_/tableEditor.test.ts:76-96](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/tableEditor.test.ts#L76-L96) 的写法，在 jsdom 里 dispatch `blur` 事件并打印 `view.state.doc.toString()` 观察每一步的文档状态。

## 6. 本讲小结

- `tableEditorPlugin` 与只读 `tableField` 的根本区别：源码态由**显式 `setTableSourceMode` Effect** 触发，而非光标位置；因此光标进入表格时表格**保持渲染态**，可继续编辑。
- `setTableSourceMode`（StateEffect）只当消息载体，`tableSourceModeField`（StateField）用一张**区间数组**存「哪些表处于源码态」，并在 `update` 里随文档变化 `mapPos` 平移区间、据 effect 增删。
- 三个区间工具 `rangesOverlap` / `addRange` / `removeRange` 用统一的闭区间相交公式 \(\,a.\text{from} \le b.\text{to} \land a.\text{to} \ge b.\text{from}\,\) 管理「源码态区间集合」。
- `tableEditorField` 仅在 `docChanged` / `reconfigured` / 收到 `setTableSourceMode` 时重建，**不含选区变化**；渲染态走 replace + `TableEditorWidget`，源码态走 widget 切换按钮 + 逐行 `cm-table-source`。
- `createEditableCell` 用 `contenteditable` 实现**双视图**：休息态显示 `renderInlineMarkdown` 渲染结果，聚焦切 `textContent` 显示原始文本；focus / blur / Enter 是三个提交时机。
- 编辑写回链路：`commitIfChanged`（值不同才提交）→ `commitCell` → `cloneTableData` → `serializeMarkdownTable`（整表重序列化并规整化）→ `view.dispatch({ changes })` 替换整张表格区间；`eq` 保证未变时不重建 DOM、`ignoreEvent:true` 让 widget 自管事件。

## 7. 下一步学习建议

- **横向对比可编辑代码块**：第五单元（u5）的 `codeBlockEditorPlugin`（toggle 模式）与本讲思路高度相似——都用 StateEffect + StateField 跟踪源码态、都用按钮切换、都有「整块替换写回」的机制。学完本讲后，建议直接对照 `src/plugins/codeBlock.ts` 与 `src/plugins/codeBlockEffects.ts`，找出它们共用的「显式切换」模式。
- **深入序列化的规整化效应**：重读 u4-l1 的 `serializeMarkdownTable`，思考为什么「以表为单位写回」必然导致格式规整化，以及在什么场景下这种规整化可能不被用户期望（例如用户刻意手写的特殊对齐）。
- **动手扩展**：尝试给 `TableEditorWidget` 加一个「新增行」按钮，要求复用本讲的 `cloneTableData` → `serializeMarkdownTable` → `view.dispatch` 写回链路，并在 [src/plugins/\_\_tests\_\_/tableEditor.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/tableEditor.test.ts) 里补一个对应用例。
- **架构回顾**：在学完第五单元后，回头读 u7-l3 的整体架构讲义，把本讲的「StateEffect 消息 + StateField 状态 + Widget 自管事件 + 整块写回」归纳为可编辑 Live Preview 元素的一类通用范式。
