# tableParser 与 tableSerializer：表格的解析与序列化

## 1. 本讲目标

学完本讲，你应该能够：

- 说清一个 GFM 表格由哪三部分组成，以及解析时为什么必须校验它们。
- 读懂 `parseMarkdownTable` 如何把表格文本变成结构化的 `TableData`，并知道它在什么情况下返回 `null`。
- 读懂 `serializeMarkdownTable` 如何把 `TableData` 还原成 Markdown 文本，以及它对单元格做了哪些「安全化」处理。
- 理解两个最容易踩坑的细节：对齐分隔行（`:---` / `---:` / `:---:` / `---`）的解析与生成，以及转义管道符 `\|` 的占位替换技巧。
- 动手验证一次「不规整表格」经过 解析→序列化 往返（round-trip）后如何被规整化。

本讲只讲两个**纯函数工具**，不涉及 CodeMirror 的装饰、Widget 或 DOM。它们是整个表格子系统（后续 `u4-l2` 只读渲染、`u4-l3` 可编辑表格）的地基。

## 2. 前置知识

### 什么是 GFM 表格

GFM（GitHub Flavored Markdown）表格是用竖线 `|` 分隔的文本块，标准结构是**三要素**：

```markdown
| Name | Age |      ← 1. 表头行（header）
|------|-----|      ← 2. 分隔行（separator / delimiter）
| Alice | 25 |      ← 3. 数据行（data row），可以有 0 行或多行
```

- **分隔行**每个单元格只能由 `-` 组成（至少一个），两端可选地加 `:` 来表示对齐方式。
- 表头行的列数、分隔行的列数必须一致；数据行的列数可以不一致（后面会讲怎么处理）。

### 行首行尾的竖线是可选的

`| a | b |` 和 `a | b` 在很多解析器里都合法。本项目的解析器两种都接受。

### 转义管道符

单元格内容里如果需要一个真正的竖线字符，必须写成 `\|`，否则会被当成列分隔符。例如 `a \| b` 表示单元格内容是 `a | b`。

### 本讲的定位：纯函数，无 CodeMirror 依赖

`tableParser.ts` 和 `tableSerializer.ts` 都在 `src/utils/` 下，是**纯函数**：输入字符串/数据结构，输出数据结构/字符串，不碰 DOM、不碰编辑器状态。它们和 CodeMirror 的唯一接触点是**调用方**：

- `table.ts` 和 `tableEditor.ts` 用 `state.doc.sliceString(node.from, node.to)` 从语法树节点取出表格源码文本，再交给 `parseMarkdownTable`。这里用到的 `syntaxTree`、节点区间（`from`/`to`）的概念在 `u2-l1` 已讲过——本讲的输入字符串就来自那里。
- `tableEditorWidget.ts` 在用户编辑单元格后，用 `serializeMarkdownTable` 把改动还原成文本，再通过 `view.dispatch` 写回文档。

所以本讲可以脱离编辑器单独理解，先把「文本 ⇄ 数据结构」这条转换链彻底吃透。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`src/utils/tableParser.ts`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableParser.ts) | 定义 `TableData` 数据结构，导出 `parseMarkdownTable`：文本 → `TableData`（或 `null`）。内部还有三个私有助手：`parseRow`、`parseAlignment`、`isSeparatorRow`。 |
| [`src/utils/tableSerializer.ts`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableSerializer.ts) | 导出 `serializeMarkdownTable`：`TableData` → 文本。内部两个私有助手：`escapeCell`、`alignmentCell`。 |
| [`src/utils/__tests__/tableParser.test.ts`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/tableParser.test.ts) | 用 Vitest 写的解析器测试，覆盖了简单表格、对齐、空单元格、转义竖线、列数不匹配等边界。是理解解析行为最直接的资料。 |

> 注：序列化器目前**没有专门的测试文件**（`__tests__/` 下只有 `tableParser.test.ts`），这也正好是本讲综合实践要补的缺口。

数据流全景：

```
Markdown 文本 ──parseMarkdownTable──▶ TableData ──serializeMarkdownTable──▶ Markdown 文本
（取自语法树 Table 节点）   { headers,      }                       （dispatch 写回文档）
                          { alignments,   }
                          { rows          }
```

- **解析方向**（文本→数据）：[`src/plugins/table.ts:30`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/table.ts#L30)、[`src/plugins/tableEditor.ts:79`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/tableEditor.ts#L79) 调用 `parseMarkdownTable`。
- **序列化方向**（数据→文本）：[`src/widgets/tableEditorWidget.ts:125`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L125) 与 [`:134`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L134) 调用 `serializeMarkdownTable`，紧接着 `view.dispatch` 写回。

## 4. 核心概念与源码讲解

### 4.1 parseMarkdownTable：把表格文本解析成结构化数据

#### 4.1.1 概念说明

`parseMarkdownTable` 解决的问题是：编辑器里有一段表格文本，但文本只是「一坨字符串」，渲染或编辑时我们需要知道它有哪几列、每列怎么对齐、每行每个单元格是什么。解析器的工作就是把文本变成结构化的 `TableData`。

数据结构定义在 [`TableData` 接口 — src/utils/tableParser.ts:10-14](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableParser.ts#L10-L14)：

```ts
export interface TableData {
  headers: string[];                              // 表头单元格
  alignments: ('left' | 'center' | 'right' | null)[]; // 每列对齐方式
  rows: string[][];                               // 数据行，每行是单元格数组
}
```

三个字段正好对应 GFM 表格三要素中的信息：`headers` 来自表头行，`alignments` 来自分隔行，`rows` 来自数据行。注意 `alignments` 用 `null` 表示「未指定对齐」（即纯 `---`），这是和 `'left'` 不同的语义——`null` 通常渲染为默认（一般是左对齐），而 `'left'` 是**显式**左对齐。

#### 4.1.2 核心流程

`parseMarkdownTable` 的执行步骤（任何一步校验失败就返回 `null`）：

1. **切行**：按 `\n` 切分，丢弃空白行。
2. **至少两行**：少于两行（没有表头+分隔行）直接 `null`。
3. **解析表头**：第 1 行用 `parseRow` 切成单元格数组；若为空则 `null`。
4. **解析分隔行**：第 2 行用 `parseRow` 切成数组，用 `isSeparatorRow` 校验格式。
5. **列数匹配**：表头列数必须等于分隔行列数，否则 `null`。
6. **算对齐**：对分隔行每个单元格调用 `parseAlignment`。
7. **解析数据行**：从第 3 行起逐行 `parseRow`，并把每行**归一化到表头列数**（多则截断、少则补空串）。

列数归一化用一句话概括：设表头列数为 \(N\)，对每一数据行 \(r\)，输出第 \(i\) 列为 \(r'_i = r_i\)（当 \(i < |r|\)）否则 \(r'_i = \varepsilon\)（空串），且只保留 \(i < N\) 的列，超出部分丢弃。

其中 `parseRow` 是解析单行的核心，它要同时处理「可选的首尾竖线」和「转义竖线 `\|`」两个麻烦。它的策略是**占位替换**：

1. 先把 `\|` 替换成一个不可能在正常文本里出现的占位符 `\x00PIPE\x00`（两端裹着空字节 `\x00`）。
2. 再放心地按 `|` 切分。
3. 去掉首尾因可选竖线产生的空单元格。
4. 最后把占位符还原成 `|`，并对每个单元格 `trim()`。

#### 4.1.3 源码精读

先看入口函数 [`parseMarkdownTable` — src/utils/tableParser.ts:65-98](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableParser.ts#L65-L98)：

```ts
export function parseMarkdownTable(source: string): TableData | null {
  const lines = source.split('\n').filter((line) => line.trim() !== '');
  if (lines.length < 2) return null;

  const headerCells = parseRow(lines[0]);
  if (headerCells.length === 0) return null;

  const separatorCells = parseRow(lines[1]);
  if (!isSeparatorRow(separatorCells)) return null;

  if (headerCells.length !== separatorCells.length) return null;

  const alignments = separatorCells.map(parseAlignment);

  const rows: string[][] = [];
  for (let i = 2; i < lines.length; i++) {
    const rowCells = parseRow(lines[i]);
    // Pad or truncate to header column count
    const normalizedRow = headerCells.map((_, idx) => rowCells[idx] ?? '');
    rows.push(normalizedRow);
  }

  return { headers: headerCells, alignments, rows };
}
```

要点：

- 第 66 行 `filter` 丢弃空白行——这是防御性的，因为从文档切片得到的文本可能夹着空行。
- 第 89 行的 `headerCells.map((_, idx) => rowCells[idx] ?? '')` 就是归一化：以**表头列数**为模板遍历，数据行缺的列补 `''`，多的列因 `map` 只跑 `headerCells.length` 次被自然丢弃。
- 整个函数**只读**，没有副作用，可独立测试。

再看 [`parseRow` — src/utils/tableParser.ts:19-35](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableParser.ts#L19-L35)：

```ts
function parseRow(line: string): string[] {
  const placeholder = '\x00PIPE\x00';
  const escaped = line.replace(/\\\|/g, placeholder); // \| 先藏起来
  const cells = escaped.split('|');                    // 再按 | 切
  if (cells.length > 0 && cells[0].trim() === '') cells.shift(); // 去掉首部空
  if (cells.length > 0 && cells[cells.length - 1].trim() === '') cells.pop(); // 去掉尾部空
  return cells.map((cell) =>
    cell.replace(new RegExp(placeholder, 'g'), '|').trim() // 还原 | 并 trim
  );
}
```

为什么用占位符而不是直接「聪明地切分」？因为正则切分带转义的竖线很容易出错，而「先藏起来→简单切→再还原」是一个经典且稳妥的手法。注意只去掉**首尾**的空单元格——**中间**的空单元格（如 `| a | | c |` 里的空列）会被保留，这正是测试里「空单元格」用例期望的行为。

分隔行的校验在 [`isSeparatorRow` — src/utils/tableParser.ts:54-57](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableParser.ts#L54-L57)：

```ts
function isSeparatorRow(cells: string[]): boolean {
  if (cells.length === 0) return false;
  return cells.every((cell) => /^:?-+:?$/.test(cell.trim()));
}
```

正则 `^:?-+:?$` 的含义：可选冒号、一个或多个 `-`、可选冒号。即 `---`、`:---`、`---:`、`:---:` 都合法，但 `---x`、`abc` 不合法。

#### 4.1.4 代码实践

**实践目标**：理解列数不匹配时解析器如何归一化。

**操作步骤**（源码阅读型 + 可选运行验证）：

1. 打开测试文件 [`tableParser.test.ts:94-106`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/tableParser.test.ts#L94-L106)，阅读 `should handle mismatched column counts in rows` 用例。
2. 对照下面的表格，先**自己预测** `rows` 的输出，再看测试断言。

输入：

```markdown
| A | B | C |
|---|---|---|
| 1 | 2 |
| 1 | 2 | 3 | 4 |
```

**需要观察的现象**：

- 第 1 个数据行只有 2 列，少了第 3 列。
- 第 2 个数据行有 4 列，多了第 4 列。

**预期结果**（与测试断言一致）：

- `rows[0]` = `['1', '2', '']`（缺的列补空串）
- `rows[1]` = `['1', '2', '3']`（多的第 4 列 `'4'` 被丢弃）

如果想实际运行确认：在仓库根目录执行 `npm test`，Vitest 会跑通这个用例（测试环境见 `u1-l3`）。

#### 4.1.5 小练习与答案

**练习 1**：`parseMarkdownTable('| A |')` 返回什么？为什么？

> **答案**：返回 `null`。因为按 `\n` 切分后只有一行（`lines.length < 2`），不满足「至少两行」。

**练习 2**：输入 `| A | B |\n| x | y |`（分隔行不是合法分隔符），返回什么？哪一步拦截的？

> **答案**：返回 `null`。`lines[1]` 经 `parseRow` 得到 `['x', 'y']`，`isSeparatorRow` 用 `^:?-+:?$` 校验失败（`x` 不匹配），于是在第 76 行 `if (!isSeparatorRow(...))` 处返回 `null`。

**练习 3**：`| a | | c |`（中间有空单元格）解析后第二列是什么？

> **答案**：空串 `''`。`parseRow` 只去掉首尾空单元格，中间的空单元格保留并 `trim()` 成 `''`。这正是 `tableParser.test.ts:32-45` 用例的断言。

---

### 4.2 serializeMarkdownTable：把结构化数据还原成文本

#### 4.2.1 概念说明

`serializeMarkdownTable` 是解析器的逆操作：给定一个 `TableData`，输出一段合法的 Markdown 表格文本。它的典型用途在**可编辑表格**场景——用户在 `contenteditable` 单元格里改了内容后，需要把新内容写回文档，而文档里存的是 Markdown 文本，不是结构化数据，所以必须先序列化。

参考真实调用 [`tableEditorWidget.ts:131-137`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/tableEditorWidget.ts#L131-L137)（编辑某个单元格后写回）：

```ts
const commitCell = (rowIndex, colIndex, nextValue) => {
  const updated = cloneTableData(this.data);
  updated.rows[rowIndex][colIndex] = nextValue;
  const markdown = serializeMarkdownTable(updated);  // 数据→文本
  view.dispatch({                                     // 写回文档
    changes: { from: this.from, to: this.to, insert: markdown },
  });
};
```

#### 4.2.2 核心流程

`serializeMarkdownTable` 把表格拼成三段文本（表头行 / 分隔行 / 数据行），用 `\n` 连接：

1. **表头行**：`| h1 | h2 | ... |`，每个表头先 `escapeCell`。
2. **分隔行**：`| --- | :---: | ... |`，每列按 `alignmentCell` 生成对齐标记。
3. **数据行**：每行 `| c1 | c2 | ... |`，每个单元格 `escapeCell`。
4. 三段用 `\n` 拼接返回。

其中 `escapeCell` 对每个单元格做三件事：把 `|` 转义成 `\|`（保证不会破坏列结构）、把换行符替换成空格（单元格不能跨行）、`trim()`。

注意一个设计取舍：序列化器**总是**输出首尾竖线 `| ... |`，即使原表是 `a | b` 形式。也就是说，一次往返会让「无首尾竖线」的表变成「有首尾竖线」——这是一种**规整化**，不是 bug。

#### 4.2.3 源码精读

入口 [`serializeMarkdownTable` — src/utils/tableSerializer.ts:20-30](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableSerializer.ts#L20-L30)：

```ts
export function serializeMarkdownTable(data: TableData): string {
  const headerLine = `| ${data.headers.map(escapeCell).join(' | ')} |`;
  const separatorLine = `| ${data.alignments.map(alignmentCell).join(' | ')} |`;
  const rowLines = data.rows.map(
    (row) => `| ${row.map(escapeCell).join(' | ')} |`
  );
  return [headerLine, separatorLine, ...rowLines].join('\n');
}
```

三行各自用 `| ` 前缀、` | ` 分隔、` |` 后缀拼出来。分隔符统一是 `' | '`（竖线两侧各一空格），格式干净一致。

单元格转义 [`escapeCell` — src/utils/tableSerializer.ts:9-11](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableSerializer.ts#L9-L11)：

```ts
function escapeCell(value: string): string {
  return value.replace(/\|/g, '\\|').replace(/\r?\n/g, ' ').trim();
}
```

- `.replace(/\|/g, '\\|')`：把真正的竖线转义，保证它不会在序列化后被当成列分隔符。
- `.replace(/\r?\n/g, ' ')`：把单元格内的换行压成空格——Markdown 表格一个单元格必须在一行内。
- `.trim()`：去掉首尾空白。

这一步和解析器 `parseRow` 里的「`\|` → 占位 → 还原 `|`」恰好构成**对称的转义往返**：解析时 `\|` 被还原成内容里的 `|`，序列化时内容里的 `|` 又被转义回 `\|`。

#### 4.2.4 代码实践

**实践目标**：手算序列化输出，验证转义与格式。

**操作步骤**：给定如下 `TableData`（**示例数据**，非项目原有代码），先手写 `serializeMarkdownTable` 的输出，再对照答案。

```ts
const data = {
  headers: ['Expr', 'Result'],
  alignments: ['left', 'center'],
  rows: [['a | b', 'ok']],
};
```

**需要观察的现象**：

- 单元格 `'a | b'` 里含有真正的竖线，序列化时必须转义。
- 对齐 `'left'`/`'center'` 分别生成 `:---` / `:---:`。

**预期结果**：

```markdown
| Expr | Result |
| :--- | :---: |
| a \| b | ok |
```

注意第三行 `a \| b` 的竖线被转义——如果忘了转义，这行会被解析成 4 列，破坏表格。

#### 4.2.5 小练习与答案

**练习 1**：一个单元格内容是 `"hello\nworld"`（含换行），序列化后变成什么？

> **答案**：`hello world`。`escapeCell` 里 `.replace(/\r?\n/g, ' ')` 把换行替换成空格，再 `trim()`。

**练习 2**：为什么序列化器总是输出首尾竖线，而不是尽量还原原始写法？

> **答案**：因为 `TableData` 里**没有保存**「原文是否有首尾竖线」这个信息——解析阶段首尾空单元格已被丢弃。序列化器选择一个稳定、规范的格式（带首尾竖线）输出，保证结果总是合法且一致。这是一种刻意的规整化。

---

### 4.3 对齐与转义：两个核心难点的对称处理

#### 4.3.1 概念说明

对齐和转义是表格解析/序列化里最容易出错的两点，也是 `parseMarkdownTable` 与 `serializeMarkdownTable` 必须**严格对称**的地方。对称的含义是：解析得到的 `TableData`，再序列化回去，应当得到语义等价（且更规整）的表格。

**对齐**由分隔行决定。GFM 用冒号位置表达对齐：

| 分隔行写法 | 含义 | `parseAlignment` 返回 | `alignmentCell` 生成 |
|------------|------|----------------------|----------------------|
| `:---` | 左对齐 | `'left'` | `:---` |
| `---:` | 右对齐 | `'right'` | `---:` |
| `:---:` | 居中 | `'center'` | `:---:` |
| `---` | 未指定（默认） | `null` | `---` |

**转义**指单元格内容里的竖线。解析时 `\|` 是「内容的竖线」，序列化时内容的竖线要还原成 `\|`。两者必须配套，否则往返会丢数据。

#### 4.3.2 核心流程

**对齐解析**（[`parseAlignment` — src/utils/tableParser.ts:40-49](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableParser.ts#L40-L49)）只看分隔单元格的首尾字符：

```ts
function parseAlignment(cell: string): 'left' | 'center' | 'right' | null {
  const trimmed = cell.trim();
  const left = trimmed.startsWith(':');
  const right = trimmed.endsWith(':');
  if (left && right) return 'center';
  if (left) return 'left';
  if (right) return 'right';
  return null;
}
```

判定优先级是 `center > left > right > null`。它**不校验**中间是否全是 `-`——那个校验已由 `isSeparatorRow` 的 `^:?-+:?$` 在入口处做过了，所以这里能放心只看冒号。

**对齐生成**（[`alignmentCell` — src/utils/tableSerializer.ts:13-18](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/tableSerializer.ts#L13-L18)）是上表的逆映射：

```ts
function alignmentCell(alignment) {
  if (alignment === 'left') return ':---';
  if (alignment === 'right') return '---:';
  if (alignment === 'center') return ':---:';
  return '---';
}
```

注意：`null` 被映射成 `---`，与解析时 `---` → `null` 对称。但 `'left'` 与 `null` 在序列化输出上**不同**（`:---` vs `---`），保留了「显式左对齐」与「未指定」的语义区分。

#### 4.3.3 源码精读

把转义的两端放在一起对比，最容易看清对称性。

**解析端**还原转义（`parseRow` 第 32-34 行）：

```ts
return cells.map((cell) =>
  cell.replace(new RegExp(placeholder, 'g'), '|').trim()  // 占位 → |
);
```

**序列化端**施加转义（`escapeCell` 第 9-11 行）：

```ts
return value.replace(/\|/g, '\\|').replace(/\r?\n/g, ' ').trim();  // | → \|
```

两者互逆。测试 [`tableParser.test.ts:72-81`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/tableParser.test.ts#L72-L81) 验证了解析端：

```ts
const source = `| Expression | Result |
|------------|--------|
| a \\| b | true |`;
// ...
expect(result?.rows[0][0]).toBe('a | b');  // \| 被还原成内容里的 |
```

一个值得注意的**非对称**：解析端用 `isSeparatorRow` 的正则校验分隔行格式，但序列化端**不重新校验** `TableData` 的合法性——它假设传入的 `alignments` 都是合法值，直接用 `alignmentCell` 映射。也就是说，序列化器信任调用方，不做防御性校验。这在内部使用是安全的，因为 `TableData` 几乎总是由 `parseMarkdownTable` 产出或在其基础上克隆修改。

#### 4.3.4 代码实践

**实践目标**：构造转义 + 对齐的复合用例，验证往返语义。

**操作步骤**（源码阅读型）：

1. 阅读测试 [`tableParser.test.ts:21-30`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/tableParser.test.ts#L21-L30)（对齐用例）与 [`:72-81`](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/tableParser.test.ts#L72-L81)（转义用例）。
2. 对下面的输入，写出 `parseMarkdownTable` 的 `alignments` 与 `rows[0][0]`，并推断「再序列化」后的分隔行长什么样。

输入：

```markdown
| Left | C |
|:-----|:--:|
| x \| y | z |
```

**预期结果**：

- `alignments` = `['left', 'center']`
- `rows[0][0]` = `'x | y'`（`\|` 还原）
- 重新序列化后分隔行为 `| :--- | :---: |`（`-` 数量被规整成 3 个）。

**待本地验证**：`-` 的原始数量（5 个、2 个）在往返后是否一定会被规整成 3 个——根据 `alignmentCell` 的硬编码 `:---`/`:---:` 可以推断「是」，但可用第 5 节的综合实践脚本实测确认。

#### 4.3.5 小练习与答案

**练习 1**：分隔行 `|:--|--:|` 解析后 `alignments` 是什么？

> **答案**：`['left', 'right']`。第一格 `:--` 以 `:` 开头 → `'left'`；第二格 `--:` 以 `:` 结尾 → `'right'`。注意 `isSeparatorRow` 只要求至少一个 `-`，不要求固定数量，所以 `:--` 合法。

**练习 2**：如果 `alignmentCell` 接收到一个非法值（比如 `'top'`），会返回什么？这暴露了什么设计假设？

> **答案**：返回 `'---'`（走最后的 `return '---'` 分支，因为没有任何 `if` 命中）。这说明序列化器不校验对齐合法性，**信任**调用方只传 `'left'|'center'|'right'|null`。设计假设是 `TableData` 总由 `parseMarkdownTable` 或其克隆产物提供，类型已被 TypeScript 约束。

**练习 3**：为什么解析转义要用「占位符」而不是写一个能识别 `\|` 的复杂 split？

> **答案**：用占位符（先藏 `\|`、再简单 `split('|')`、再还原）把「识别转义」和「切分」两件事解耦，逻辑清晰、不易出错。直接写带转义感知的切分正则容易遗漏边界情况（如行尾转义、连续转义），可读性和正确性都更差。

---

## 5. 综合实践

**任务**：为一个「不规整表格」走完整的 解析→序列化 往返，验证序列化结果是否被规整化。我们顺手补上序列化器缺失的测试。

### 第 1 步：准备一个不规整输入

下面这个表格有三处不规整：无首尾竖线、某行单元格偏少、某行单元格偏多。

```markdown
A | B | C
---|---|---
1 | 2
1 | 2 | 3 | 4
```

### 第 2 步：手算 parse 的结果

- `headers` = `['A', 'B', 'C']`
- `alignments` = `[null, null, null]`
- `rows[0]` = `['1', '2', '']`（少一列，补空串）
- `rows[1]` = `['1', '2', '3']`（多一列 `'4'`，丢弃）

### 第 3 步：手算 serialize 的结果

按 `serializeMarkdownTable` 的格式拼接（总是带首尾竖线，分隔符 `' | '`）：

```markdown
| A | B | C |
| --- | --- | --- |
| 1 | 2 |  |
| 1 | 2 | 3 |
```

可以看到往返后：① 首尾竖线被补上；② 列数被统一成 3 列；③ `---` 数量被规整。**表格变得规整对齐了**——这就是往返的「规整化」效果。

### 第 4 步：写测试实测（可选运行）

仿照现有测试风格，在 `src/utils/__tests__/` 下新建一个文件（**不要修改任何源码或现有测试**），例如 `tableRoundTrip.test.ts`：

```ts
// 示例代码：新建测试文件，验证 parse → serialize 往返
import { describe, it, expect } from 'vitest';
import { parseMarkdownTable } from '../tableParser';
import { serializeMarkdownTable } from '../tableSerializer';

describe('parse → serialize 往返', () => {
  it('不规整表格被规整化', () => {
    const source = 'A | B | C\n---|---|---\n1 | 2\n1 | 2 | 3 | 4';
    const data = parseMarkdownTable(source)!;
    const out = serializeMarkdownTable(data);

    expect(data.headers).toEqual(['A', 'B', 'C']);
    expect(data.rows[0]).toEqual(['1', '2', '']);
    expect(data.rows[1]).toEqual(['1', '2', '3']);
    // 序列化后：首尾竖线补齐、列数统一、对齐标记规整
    expect(out).toBe(
      '| A | B | C |\n| --- | --- | --- |\n| 1 | 2 |  |\n| 1 | 2 | 3 |'
    );
  });
});
```

**操作步骤**：

1. 创建上述测试文件。
2. 在仓库根目录运行 `npm test`（测试环境与命令见 `u1-l3`）。
3. 观察该用例是否通过。

**预期结果**：用例通过，证明往返确实把不规整表格规整化了。

**如果无法本地运行**：明确标注「待本地验证」，但上面的断言是根据源码逻辑严格推导的，可作为参考答案。

> 提醒：本实践要求**新建**测试文件，不要改动 `src/utils/__tests__/tableParser.test.ts` 或任何源码——本讲义 worker 的规则是只写讲义目录的 Markdown，这里给出的测试代码是供你**学习时**在自己的工作副本里尝试，不属讲义生成范围。

## 6. 本讲小结

- `parseMarkdownTable` 是纯函数，把表格文本解析成 `TableData`（`headers` / `alignments` / `rows`），任何一步校验失败返回 `null`。
- 解析按「切行 → 至少两行 → 表头 → 分隔行格式 → 列数匹配 → 算对齐 → 归一化数据行」顺序校验；数据行被归一化到**表头列数**（少补空串、多则丢弃）。
- 转义竖线 `\|` 用「占位符替换」处理：解析时 `\|` → 占位 → 切分 → 还原 `|`；序列化时 `escapeCell` 把 `|` 转义回 `\|`，并把换行压成空格。
- 对齐由分隔行的冒号位置决定：`parseAlignment` 看 `startsWith(':')`/`endsWith(':')`，优先级 `center > left > right > null`；`alignmentCell` 是其逆映射，且区分 `'left'`（`:---`）与 `null`（`---`）。
- `serializeMarkdownTable` 总是输出带首尾竖线的规范格式，因此一次往返会**规整化**表格（补首尾竖线、统一列数、规整对齐标记）。
- 解析器对分隔行格式有校验（`isSeparatorRow`），但序列化器**不校验** `TableData` 合法性——它信任调用方，类型由 TypeScript 约束。

## 7. 下一步学习建议

本讲只完成了「文本 ⇄ 数据结构」的转换链，还没有把这些数据**渲染**出来。接下来的两讲会把它接进 CodeMirror：

- **`u4-l2 tableField：只读 GFM 表格渲染`**：看 `tableField`（StateField）如何遍历语法树找 `Table` 节点、调用本讲的 `parseMarkdownTable`，并结合 `shouldShowSource`（`u2-l2`）在 `TableWidget` 渲染与逐行源码态之间切换。建议先复习 `u3-l1` 的 `WidgetType`/`Decoration.replace`。
- **`u4-l3 tableEditorPlugin：可编辑表格与 source mode 切换`**：看可编辑表格如何用 `contenteditable` 单元格接收输入，再调用本讲的 `serializeMarkdownTable` 把改动 dispatch 写回文档——那正是 4.2.1 里 `commitCell` 的完整上下文。

建议阅读顺序：先 `u4-l2`（只读渲染，理解 `parseMarkdownTable` 的调用点与 `shouldShowSource` 切换），再 `u4-l3`（可编辑，理解 `serializeMarkdownTable` 的写回链路）。
