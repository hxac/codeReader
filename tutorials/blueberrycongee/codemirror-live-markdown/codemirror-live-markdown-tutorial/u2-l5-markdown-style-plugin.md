# markdownStylePlugin：为内容节点加样式

## 1. 本讲目标

学完本讲后，你应该能够：

1. 看懂 `styleMap` 这张「语法节点名 → CSS 类」的映射表，并知道它只覆盖**内容节点**（如 `StrongEmphasis`、`ATXHeading1`），而不覆盖标记符号节点。
2. 区分 `Decoration.mark`（给一段文字加类）和 `Decoration.line`（给整行加类）两种装饰，并理解为什么标题要**同时**使用这两种。
3. 说清 `markdownStylePlugin` 与上一讲的 `livePreviewPlugin` 是如何**职责分离**的：一个管「内容长什么样」，一个管「标记符号藏不藏」。
4. 理解为什么 `markdownStylePlugin` **完全不需要** `shouldShowSource`、`mouseSelectingField` 这些与光标/拖拽相关的状态——它的样式永远存在。

## 2. 前置知识

本讲建立在前面几讲之上，先快速回顾三个概念：

- **ViewPlugin**（见 u2-l1）：一种随编辑器视图生命周期运行、负责产出「装饰（Decoration）」的扩展。`markdownStylePlugin` 就是一个 ViewPlugin。
- **Decoration 的两种类型**（见 u2-l1）：
  - `Decoration.mark({ class })`：给 `[from, to)` 区间内的文字套一个 CSS 类，可以多层嵌套。
  - `Decoration.line({ class })`：给整行套一个 CSS 类，定位时只需要行内任意一个位置。
- **syntaxTree.iterate**（见 u2-l4）：遍历 Lezer 解析出的 Markdown 语法树，按 `node.name` 过滤我们感兴趣的节点。
- **标记节点 vs 内容节点**（见 u2-l4）：`EmphasisMark`（`*`）、`HeaderMark`（`#`）这类是**标记符号**；而 `StrongEmphasis`、`ATXHeading1` 这类是包含实际文本的**内容容器**。本讲只处理后者。

> 一句话提醒：`livePreviewPlugin` 处理「标记符号」，`markdownStylePlugin` 处理「内容容器」。两者合作的产物，就是你最终看到的 Live Preview 效果。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/plugins/markdownStyle.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts) | 本讲主角：遍历语法树，为内容节点套样式类。 |
| [src/plugins/livePreview.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts) | 对照组：上一讲的标记隐藏插件，用来讲清「职责分离」。 |
| [src/theme/default.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts) | 样式落地点：`cm-header-1`、`cm-strong` 等类在这里被真正赋予字号、颜色。 |
| [src/plugins/__tests__/markdownStyle.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/markdownStyle.test.ts) | 测试：演示如何构造 `EditorView` 并断言「装饰里包含某个 CSS 类」。 |
| [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) | 桶文件，`markdownStylePlugin` 在此被导出（第 18 行）。 |

---

## 4. 核心概念与源码讲解

### 4.1 styleMap：把语法节点名映射成 CSS 类

#### 4.1.1 概念说明

Lezer 在解析 Markdown 时，会把文档拆成一棵语法树，每个节点都有一个名字（`node.name`），例如：

```text
# Hello
```

会被解析出名为 `ATXHeading1` 的节点，覆盖整行 `# Hello`，它下面还有 `HeaderMark`（`#`）和文本子节点。

`markdownStylePlugin` 的核心思路极其朴素：**准备一张查找表 `styleMap`，把节点名映射到一个 CSS 类名；遍历树时，命中表的节点就给它套上对应的类。**

```
节点名 ATXHeading1  ──查表──▶  cm-header-1  ──套类──▶  文字变大变粗
节点名 StrongEmphasis ──查表──▶  cm-strong   ──套类──▶  文字加粗
节点名 Emphasis     ──查表──▶  cm-emphasis  ──套类──▶  文字斜体
```

这张表只包含**内容节点**，不包含 `EmphasisMark`、`HeaderMark` 这类标记符号——后者归 `livePreviewPlugin` 管。

#### 4.1.2 核心流程

`build(view)` 的执行过程可以概括为：

1. 准备一个空数组 `decorations` 收集结果。
2. 定义局部查找表 `styleMap`。
3. 用 `syntaxTree(view.state).iterate({ enter })` 遍历整棵语法树。
4. 在 `enter` 回调里：
   - 用 `styleMap[node.name]` 查表；查不到（`undefined`）就直接 `return` 跳过。
   - 若该节点落在代码块内部，也 `return` 跳过（代码块有独立的渲染逻辑）。
   - 否则 push 一条 `Decoration.mark({ class: cls }).range(node.from, node.to)`。
   - 若是标题节点，额外 push 一条 `Decoration.line`（见 4.2）。
5. 用 `Decoration.set(decorations, true)` 把数组打包成一个 `DecorationSet` 返回。

关键点：**判定逻辑是「查表 + 跳过」，没有任何与光标、选区、拖拽相关的判断。** 这一点决定了它为何不需要 `shouldShowSource`（见 4.3）。

#### 4.1.3 源码精读

先看主角插件的整体骨架：它是一个 `ViewPlugin.fromClass`，构造时 `build` 一次，之后靠 `update` 决定何时重建。

[markdownStylePlugin 整体结构：src/plugins/markdownStyle.ts:39-100](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L39-L100) — 一个 ViewPlugin 类，构造函数调用 `build`，并把 `decorations` 作为字段对外暴露。

> 注意它的 `update` 判定与上一讲 `livePreviewPlugin` 不同（详见 4.3）。

接着是本模块的核心——`styleMap` 这张表：

[styleMap 查找表：src/plugins/markdownStyle.ts:57-69](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L57-L69) — 把 6 级标题、加粗、斜体、删除线、行内代码、链接映射到对应的 CSS 类。

```ts
const styleMap: Record<string, string> = {
  ATXHeading1: 'cm-header-1',
  ATXHeading2: 'cm-header-2',
  // ... ATXHeading3~6 同理
  StrongEmphasis: 'cm-strong',
  Emphasis: 'cm-emphasis',
  Strikethrough: 'cm-strikethrough',
  InlineCode: 'cm-code',
  Link: 'cm-link',
};
```

遍历树并查表、套类的逻辑：

[遍历 + 查表 + 套类：src/plugins/markdownStyle.ts:71-92](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L71-L92) — `enter` 回调里先用 `styleMap[node.name]` 查表，查不到就跳过；落在代码块内也跳过；命中则 push 一条 `Decoration.mark`。

```ts
syntaxTree(view.state).iterate({
  enter: (node) => {
    const cls = styleMap[node.name];
    if (!cls) return;                       // ① 查表失败，跳过

    if (isInsideSkippedParent(node)) {      // ② 在代码块内，跳过
      return;
    }

    decorations.push(
      Decoration.mark({ class: cls }).range(node.from, node.to)  // ③ 套类
    );

    if (node.name.startsWith('ATXHeading')) {                     // ④ 标题额外加 line，见 4.2
      decorations.push(
        Decoration.line({ class: 'cm-heading-line' }).range(node.from)
      );
    }
  },
});
```

这里有一个重要细节：`StrongEmphasis` 这类**内容节点覆盖的是整个 `**粗体**` 区间，包括两边的 `*` 标记**。也就是说，`cm-strong` 类会施加在「标记 + 文本」整体上。但因为 `livePreviewPlugin` 同时把 `*` 标记隐藏（动画收窄到 0 宽度），最终视觉上你只会看到被加粗的文本，看不到 `*`。这就是两个插件协作的关键。

> 观察（非缺陷）：`styleMap` 定义在 `build()` 函数内部，意味着每次重建装饰都会重新创建这个小对象字面量。因为表很小、开销可忽略，作者选择了「就近可读」而非「提到模块顶层」。你可以在练习里思考把它提到顶层是否更合适。

#### 4.1.4 代码实践

**目标**：亲手验证 `styleMap` 的「查表命中」行为，理解「只有表里的节点才会被套类」。

**操作步骤**：

1. 打开测试文件 [src/plugins/__tests__/markdownStyle.test.ts:10-18](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/markdownStyle.test.ts#L10-L18)，读懂 `createTestView(doc)` 如何用 `markdown()` + `markdownStylePlugin` 构造一个编辑器。
2. 在 `src/plugins/__tests__/markdownStyle.test.ts` 末尾追加一个临时用例（仅用于本地观察，勿提交）：

   ```ts
   it('本地观察：哪些节点被套了类', () => {
     const view = createTestView('**粗体** 和 `代码`');
     const plugin = view.plugin(markdownStylePlugin);
     plugin?.decorations.between(0, view.state.doc.length, (from, to, deco) => {
       console.log((deco.spec as { class?: string }).class, from, to,
         JSON.stringify(view.state.doc.sliceString(from, to)));
     });
     view.destroy();
   });
   ```

3. 运行 `npm test -- markdownStyle`。

**需要观察的现象**：控制台打印出命中的类名、区间和对应原文。

**预期结果**：你应该看到 `cm-strong` 覆盖了**整个** `**粗体**`（含星号），`cm-code` 覆盖了整个 `` `代码` ``（含反引号）。普通文字「和」不会有任何装饰。

> 若运行环境无控制台输出，结论为「待本地验证」，但你可以通过阅读测试断言 [src/plugins/__tests__/markdownStyle.test.ts:112-114](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/markdownStyle.test.ts#L112-L114)（断言 `**bold text**` 命中 `cm-strong`）间接确认。

#### 4.1.5 小练习与答案

**练习 1**：`styleMap` 里没有 `EmphasisMark`。如果你误把它加进去（`EmphasisMark: 'cm-emphasis'`），会发生什么？

**参考答案**：那么 `*` 这个标记符号本身也会被套上斜体类。但更关键的是，它会与 `livePreviewPlugin` 给同一个 `*` 区间套的 `cm-formatting-inline`（隐藏动画）类叠加。由于装饰可以嵌套，两个 mark 会同时作用——视觉上可能看不出大问题，但这违背了职责分离，属于重复/错误处理。

**练习 2**：为什么 `styleMap` 不需要包含 `Paragraph`、`Document` 这类节点？

**参考答案**：这些是结构性的「容器」节点，没有对应的视觉样式需求。`styleMap` 只收录「需要改变外观」的内容节点。容器节点虽然存在于语法树，但查表返回 `undefined` 后会被第 ① 步直接跳过。

---

### 4.2 Decoration.mark 与 Decoration.line：内联样式与整行样式

#### 4.2.1 概念说明

CodeMirror 6 的 `Decoration` 有多种类型，本插件用到两种：

- **`Decoration.mark({ class })`**：给 `[from, to)` 区间内的**文字**套类。它是「内联」的，只影响被覆盖的字符，可以多层嵌套（比如标题里的粗体）。
- **`Decoration.line({ class })`**：给**整行**套类。它只需要一个定位点（行内任意位置），CodeMirror 会自动把它扩展到整行。适合做「这一行有特殊背景/间距」这类效果。

举个直观例子，对于 `# 标题`，我们希望：

1. 文字本身变大变粗 → 用 `Decoration.mark` 套 `cm-header-1`（这是内联文字样式）。
2. 整行可以有独立的间距或底边 → 用 `Decoration.line` 套 `cm-heading-line`（这是行级样式）。

两者**不冲突**，而是叠加：mark 管文字，line 管行。

#### 4.2.2 核心流程

标题节点的处理流程：

```
遇到 ATXHeading1~6
   ├─ push Decoration.mark({ class: 'cm-header-N' }).range(from, to)   ← 文字样式
   └─ push Decoration.line({ class: 'cm-heading-line' }).range(from)   ← 整行样式（只需一个定位点）
```

非标题节点只走第一条（mark），不走 line。

#### 4.2.3 源码精读

标题的双装饰在这里：

[标题：同时加 mark 和 line：src/plugins/markdownStyle.ts:81-90](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L81-L90) — 先用 `node.name.startsWith('ATXHeading')` 判断是否标题，是则额外追加一条 `Decoration.line`。

```ts
decorations.push(
  Decoration.mark({ class: cls }).range(node.from, node.to)   // mark 需要两个位置 from, to
);

if (node.name.startsWith('ATXHeading')) {
  decorations.push(
    Decoration.line({ class: 'cm-heading-line' }).range(node.from)  // line 只需一个位置
  );
}
```

注意参数差异：`mark` 的 `range` 要 `(from, to)` 两个参数；`line` 的 `range` 只给一个位置（`node.from`），CodeMirror 据此定位到它所在的行并对整行生效。

这些类最终在主题文件里被赋予真实样式。例如 `cm-header-1` 的字号定义：

[cm-header-1 样式定义：src/theme/default.ts:89-94](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L89-L94) — 设置 `fontSize: 2em`、`fontWeight: 700`、以及基于 CSS 变量的颜色。

```ts
'.cm-header-1': {
  fontSize: '2em',
  fontWeight: '700',
  lineHeight: '1.3',
  color: 'hsl(var(--md-heading, var(--foreground, 220 9% 9%)))',
},
```

而 `cm-strong` 的加粗：

[cm-strong 样式定义：src/theme/default.ts:113-116](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L113-L116) — `fontWeight: 700` 实现加粗效果。

```ts
'.cm-strong': {
  fontWeight: '700',
  color: 'hsl(var(--md-bold, var(--foreground, 220 9% 9%)))',
},
```

> 可以看出三者的接力关系：**语法树节点名 →（styleMap）→ CSS 类名 →（editorTheme）→ 真实样式**。`markdownStylePlugin` 只负责中间这一跳。

#### 4.2.4 代码实践

**目标**：通过「删掉其中一种装饰」来直观感受 mark 与 line 的分工。

**操作步骤**：

1. 复制 `src/plugins/markdownStyle.ts` 到一个临时文件（**不要改动源码**），或直接在本地分支实验。
2. 临时注释掉标题的 `Decoration.mark` 那一行（只保留 `Decoration.line`）。
3. 在 demo 中输入 `# 标题`。

**需要观察的现象**：标题文字的字号、字重变化。

**预期结果**：注释掉 `cm-header-1` 这个 mark 后，标题文字会**退回普通字号**（因为 `cm-heading-line` 这个 line 类在主题里只定义了行级样式，没有定义 `fontSize`）。这说明「字号变大」来自 mark，而非 line。

> 反过来，若只保留 mark、注释 line，文字仍会变大，只是失去了任何行级样式（本项目当前 `cm-heading-line` 未在 default.ts 中定义具体规则，所以这一项的视觉差异可能很小——属于「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接用 `Decoration.line` 给标题套 `cm-header-1`，而要额外用 mark？

**参考答案**：`Decoration.line` 的类作用在「行的容器」上，CSS 中 `font-size` 等继承虽然也能影响文字，但更精确、可嵌套的做法是用 mark 直接作用在文字区间。更重要的是，标题行内可能还有粗体、链接等嵌套内容，mark 能保证 `cm-header-1` 精确覆盖标题文字范围，而 line 只适合做行级的背景/间距等「环境」样式。

**练习 2**：`Decoration.line().range(node.from)` 只传了一个位置。如果文档很长，CodeMirror 怎么知道要装饰「整行」？

**参考答案**：`Decoration.line` 的语义就是「装饰定位点所在的那一整行」。CodeMirror 内部会根据这个位置算出所属行，并把类应用到该行的 DOM 容器（`.cm-line`）上，与区间长度无关。这是 line 装饰与 mark 装饰的根本区别。

---

### 4.3 职责分离：与 livePreviewPlugin 的分工

#### 4.3.1 概念说明

本模块回答一个核心问题：**为什么要把「加样式」和「隐藏标记」拆成两个插件？**

答案是**关注点分离（Separation of Concerns）**：

- `markdownStylePlugin` 负责**静态外观**：粗体永远是粗体、一级标题永远是大号字。这些样式**与光标位置无关**，只要文档结构不变，样式就不变。
- `livePreviewPlugin` 负责**动态显隐**：`*`、`#`、`` ` `` 这些标记符号，光标不在时藏起来（呈现渲染效果），光标进入时露出来（方便编辑）。这**强依赖光标/选区**。

正因为两者关注点不同，它们的实现差异巨大：`markdownStylePlugin` 不需要读光标、不需要 `shouldShowSource`、不需要 `mouseSelectingField`，因此也更简单、更高效。

#### 4.3.2 核心流程

下面这张对比表是本模块的精华：

| 维度 | markdownStylePlugin | livePreviewPlugin |
|---|---|---|
| 处理的节点 | 内容节点（`ATXHeading1-6`、`StrongEmphasis`、`Emphasis`、`Strikethrough`、`InlineCode`、`Link`） | 标记节点（`EmphasisMark`、`StrikethroughMark`、`CodeMark`、`HeaderMark`、`ListMark`、`QuoteMark`） |
| 职责 | 给内容套样式类（加粗/斜体/字号/颜色） | 控制标记符号的显隐动画 |
| 是否依赖光标/选区 | 否 | 是（用 `shouldShowSource` 判断） |
| 是否依赖拖拽状态 | 否 | 是（读 `mouseSelectingField`，拖拽时强制不显示源码以防闪烁） |
| `update` 重建条件 | `docChanged \|\| viewportChanged` | `checkUpdateAction(update) === 'rebuild'`（含选区变化） |
| 使用的装饰 | `Decoration.mark` + `Decoration.line` | `Decoration.mark`（按行级/行内分两种 CSS 类） |

#### 4.3.3 源码精读

先看两者 `update` 的差异，这是「依赖什么」最直接的体现。

`markdownStylePlugin` 的 update——只看文档和视口：

[markdownStyle 的 update：src/plugins/markdownStyle.ts:47-51](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L47-L51) — 仅在 `docChanged` 或 `viewportChanged` 时重建。

```ts
update(update: ViewUpdate) {
  if (update.docChanged || update.viewportChanged) {
    this.decorations = this.build(update.view);
  }
}
```

对比 `livePreviewPlugin` 的 update——还要看选区和拖拽：

[livePreview 的 update：src/plugins/livePreview.ts:55-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L55-L59) — 用 `checkUpdateAction` 判定，选区变化也会触发 rebuild。

```ts
update(update: ViewUpdate) {
  if (checkUpdateAction(update) === 'rebuild') {
    this.decorations = this.build(update.view);
  }
}
```

为什么 `markdownStylePlugin` 不把选区变化纳入重建？因为**移动光标不会改变「这段文字是不是粗体」**。加粗只取决于文档内容，与光标无关。所以它刻意忽略了 `update.selectionSet`，避免每次移动光标都重新遍历整棵语法树——这是一个实实在在的性能优化。

再看两者遍历树时「关心哪些节点」的差异。

`livePreviewPlugin` 关心的是标记符号名单：

[livePreview 的 markTypes：src/plugins/livePreview.ts:81-88](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L81-L88) — 只有名字在 `markTypes` 里的节点才处理。

```ts
const markTypes = [
  'EmphasisMark', 'StrikethroughMark', 'CodeMark',
  'HeaderMark', 'ListMark', 'QuoteMark',
];
```

并且它根据 `shouldShowSource` 的返回值切换 CSS 类（显/隐）：

[livePreview 用 shouldShowSource 决定显隐：src/plugins/livePreview.ts:128](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L128) — 行内标记是否可见，完全由光标是否触碰区间决定。

```ts
const isTouched = shouldShowSource(state, node.from, node.to);
```

而 `markdownStylePlugin` 的 `build` 里**没有任何 `shouldShowSource`、没有任何 `state.field(mouseSelectingField, ...)`、没有任何 `state.selection`**——你可以回到 [4.1.3 的遍历代码](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L71-L92) 自行确认这一点。这正是「职责分离」最干净的证据：**静态样式不需要动态状态**。

> 两个插件还共享了一个小工具 `isInsideSkippedParent`（跳过代码块），见 [src/plugins/markdownStyle.ts:19-30](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L19-L30)。代码块内部的文本由 `codeBlockField` 等独立处理，所以两个插件都主动跳过它。

#### 4.3.4 代码实践

**目标**：通过对比实验，确认「移动光标」对两个插件的影响不同。

**操作步骤**：

1. 参考 [src/plugins/__tests__/markdownStyle.test.ts:23-33](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/markdownStyle.test.ts#L23-L33) 的 `hasDecorationClass` 辅助函数，构造一个含 `**粗体**` 的编辑器。
2. 记录 `markdownStylePlugin` 当前 `decorations.size`。
3. 用 `view.dispatch({ selection })` 把光标移到文档末尾（不改变文本）。
4. 再次读取 `decorations.size`。

**需要观察的现象**：移动光标前后，`markdownStylePlugin` 的装饰数量是否变化。

**预期结果**：**不变**。因为 `update` 只在 `docChanged || viewportChanged` 时重建，纯选区变化不会触发它。这与 `livePreviewPlugin`（光标进入 `**` 时标记会从隐藏变可见）形成鲜明对比。

#### 4.3.5 小练习与答案

**练习 1**：如果让 `markdownStylePlugin` 也依赖 `shouldShowSource`（光标不在时连 `cm-strong` 都不加），会怎样？

**参考答案**：那样光标移开时粗体文字会「退回普通字」，进入时才变粗——这违背了 Live Preview 的初衷。Live Preview 要求是「渲染结果常驻、仅标记符号随光标显隐」。所以把动态显隐限制在标记符号（`livePreviewPlugin`），让内容样式（`markdownStylePlugin`）保持静态，才是正确分工。

**练习 2**：从性能角度，为什么 `markdownStylePlugin` 故意不在 `update` 里响应 `selectionSet`？

**参考答案**：因为响应选区变化意味着每次移动光标都要重新遍历整棵语法树并重建所有装饰，而样式结果根本不会因此改变。忽略 `selectionSet` 让该插件只在「文档结构真正变化」时才工作，大幅减少了无谓计算。

---

## 5. 综合实践

**任务**：为 `markdownStylePlugin` 新增一个样式映射，并解释它为何不需要 `shouldShowSource`。

本任务分两步，第二步是关键。

### 第一步：新增一个 `cm-task` 样式映射

题目建议为「任务列表项 `- [ ]`」套一个 `cm-task` 类。但要注意一个**真实情况**：本项目的 demo 用的是 `markdown({ base: markdownLanguage, extensions: [Table] })`（见 [demo/main.ts:328](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L328) 附近），**只启用了 Table 扩展，没有启用 GFM 的任务列表扩展**。因此在当前 demo 配置下，`- [ ]` 只会被解析成普通列表项，**不会**产生名为 `Task` 的节点。

所以请按下面稳妥的流程操作：

1. **先发现真实的节点名**。在测试或 demo 里临时插入一段树遍历，打印 `- [ ] todo` 这一行产生的所有 `node.name`：

   ```ts
   // 示例代码（仅用于调试，非项目原有代码）
   syntaxTree(view.state).iterate({
     enter: (n) => console.log(n.name, n.from, n.to),
   });
   ```

2. **若要得到 Task 节点**：需要先在 `markdown()` 配置里启用 GFM 任务扩展（例如改用 `gfm()` 或显式加入任务列表扩展）。启用后再次打印树，确认任务标记对应的节点名（GFM 下通常为 `Task` / `TaskMarker`，**具体名称以本地打印结果为准，待本地确认**）。

3. **在 `styleMap` 里追加一行**（把 `XXX` 换成第 1、2 步确认到的节点名）：

   ```ts
   // 修改 src/plugins/markdownStyle.ts 的 styleMap（示例代码）
   const styleMap: Record<string, string> = {
     // ... 原有映射
     XXX: 'cm-task',   // 用本地确认到的真实节点名替换 XXX
   };
   ```

4. **在 `src/theme/default.ts` 里给 `.cm-task` 加样式**，例如：

   ```ts
   // 示例代码
   '.cm-task': { color: 'hsl(var(--muted-foreground, 220 9% 46%))' },
   ```

5. 运行 `npm run dev`（demo 目录）或 `npm test`，确认 `- [ ] todo` 的文本被套上了 `cm-task`。

> 若暂时无法启用 GFM 任务扩展，可以用一个**一定存在**的节点完成同样的练习：例如给 `Blockquote`（引用块）追加 `Blockquote: 'cm-quote'` 映射，流程完全一致，只是改了节点名。

### 第二步（关键）：说明为什么不需要 `shouldShowSource`

请用一段话回答：**为什么 `markdownStylePlugin` 完全不依赖 `shouldShowSource`、`mouseSelectingField` 和选区状态？**

参考要点（自己组织语言）：

- `shouldShowSource` 决定的是「标记符号显隐」，属于 `livePreviewPlugin` 的职责。
- `markdownStylePlugin` 只决定「内容长什么样」（粗体、字号、颜色），这些是**静态**的，只取决于文档结构，与光标位置无关。
- 因此它的 `update` 只在 `docChanged || viewportChanged` 时重建，移动光标不触发；它不读 `shouldShowSource`，也不读拖拽状态。这种「静态样式」与「动态显隐」的拆分，让样式插件更简单、更高效。

---

## 6. 本讲小结

- `styleMap` 是一张「语法节点名 → CSS 类」的查找表，`build` 遍历语法树时用它给命中的**内容节点**套类；查不到或落在代码块内就跳过。
- `markdownStylePlugin` 处理内容节点（`ATXHeading1-6`、`StrongEmphasis`、`Emphasis`、`Strikethrough`、`InlineCode`、`Link`），而 `livePreviewPlugin` 处理标记符号节点（`EmphasisMark`、`HeaderMark` 等），二者职责分离。
- 标题同时使用 `Decoration.mark`（`cm-header-N`，管文字字号/字重）和 `Decoration.line`（`cm-heading-line`，管整行）两种装饰。
- 样式接力链：**节点名 →（styleMap）→ CSS 类 →（editorTheme）→ 真实样式**。
- `markdownStylePlugin` 的 `update` 只在 `docChanged || viewportChanged` 时重建，**不响应选区变化**，因此不需要 `shouldShowSource` / `mouseSelectingField`——这是它比 `livePreviewPlugin` 更简单、更高效的根因。

## 7. 下一步学习建议

到这里，你已经掌握了 Live Preview 的「两种基础插件」：`livePreviewPlugin`（标记显隐）和 `markdownStylePlugin`（内容样式）。接下来建议：

1. 进入**第三单元 Widget 渲染机制**，学习 `WidgetType` + `Decoration.replace` 这种「把原文替换成真实 DOM」的渲染策略，以 `mathWidget` 为范例。
2. 在阅读后续特性插件（math/table/code/image/link）时，注意它们是「replace+Widget 渲染派」还是「mark+CSS 样式派」，与本讲的两类基础插件对照理解。
3. 想加深对本讲的理解，可以回头读 [src/plugins/__tests__/markdownStyle.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/markdownStyle.test.ts) 中的「嵌套样式」用例（粗体在标题内），体会 mark 装饰的嵌套叠加。
