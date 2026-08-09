# livePreviewPlugin：用 CSS 动画隐藏标记

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「标记节点（mark node）」和「内容节点（content node）」的区别，以及为什么 Live Preview 只隐藏前者。
- 读懂 `livePreviewPlugin` 的整体结构：构造时构建一次装饰，更新时靠 `checkUpdateAction` 决定是否重建。
- 掌握用 `syntaxTree(state).iterate({ enter })` 按 `node.name` 过滤出感兴趣的语法树节点。
- 看懂 `isInsideSkippedParent` 如何沿父节点链向上查找，跳过代码块内部的标记；以及专门为数学公式做的 `CodeMark` 例外。
- 理解为什么行内/块级标记都用 `Decoration.mark` + CSS 类切换来实现平滑显隐，而不是用 `Decoration.replace`。

本讲是「Live Preview 核心机制」单元的关键一讲。上一讲（u2-l3）讲完了 `checkUpdateAction`，本讲就让它在真实插件里发挥作用；再上一讲（u2-l2）讲完了决策函数 `shouldShowSource`，本讲会看到 `livePreviewPlugin` 如何调用它。

## 2. 前置知识

在开始前，请确保你了解以下概念（都在前面几讲出现过，这里只做一句话回顾）：

- **语法树（syntaxTree）**：CodeMirror 6 用 Lezer 解析器把 Markdown 文本解析成一棵节点树，每个节点有名字（`node.name`）、起点（`node.from`）、终点（`node.to`）和父节点（`node.parent`）。插件靠遍历这棵树来定位要处理的片段。
- **Decoration（装饰）**：CodeMirror 6 让文本「显示得和原文不一样」的机制。本讲只用到其中一种：`Decoration.mark`，它给一段文本区间套上一个 CSS 类名，文本本身仍然留在 DOM 里、仍然可编辑。
- **ViewPlugin**：随视口/文档变化重新计算装饰的扩展。它持有一个 `DecorationSet`，每次 `update` 时决定是否重算。
- **shouldShowSource**：上一讲的决策函数，输入 `(state, from, to)`，返回 `true` 表示「这一段显示原始 Markdown（标记可见、可编辑）」，返回 `false` 表示「显示渲染后的效果（标记隐藏）」。
- **checkUpdateAction**：上一讲讲的更新助手，把一次 `ViewUpdate` 归纳成 `'rebuild' | 'skip' | 'none'` 三种动作。

一个贯穿全讲的直觉：**Live Preview 的「隐藏标记」不是把文字删掉，而是给标记字符套一个 CSS 类，让它「收缩/变透明」。** 把这层窗户纸戳破，本讲的代码就好读了。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/plugins/livePreview.ts` | 本讲主角。一个 `ViewPlugin`，遍历语法树找到所有标记节点，给它们套显隐 CSS 类。 |
| `src/core/shouldShowSource.ts` | 决策函数，判断某个标记区间当前该不该「显示源码」（即标记可见）。 |
| `src/core/pluginUpdateHelper.ts` | `checkUpdateAction`，统一判断 ViewPlugin 该 `rebuild` / `skip` / `none`。 |
| `src/core/mouseSelecting.ts` | `mouseSelectingField`，记录「是否正在拖拽选区」的 StateField。 |
| `src/theme/default.ts` | `editorTheme`，定义 `cm-formatting-inline` / `cm-formatting-block` 等 CSS 类，靠 `max-width` / `font-size` 过渡实现动画。 |
| `src/plugins/__tests__/livePreview.test.ts` | 测试，示范如何在 jsdom 里构造 EditorView 并检查插件产出的装饰。 |

---

## 4. 核心概念与源码讲解

### 4.1 插件整体结构：从 update 到 build

#### 4.1.1 概念说明

`livePreviewPlugin` 是一个 **ViewPlugin**。它的任务很单一：每当编辑器状态可能影响标记显隐时，重新遍历一遍语法树，为每个标记节点产出一个 `Decoration.mark` 装饰，并把结果存到实例的 `decorations` 字段里。CodeMirror 会拿这个字段去给 DOM 文本套类。

ViewPlugin 的生命周期分两步：

1. **构造（constructor）**：编辑器首次挂载时调用一次，产出初始装饰。
2. **更新（update）**：每次发生 `ViewUpdate`（用户输入、移动光标、滚动、重配置等）时调用。这里的关键问题是：**不是每次更新都要重算装饰。** 比如单纯滚动（在 Lezer 已经解析完的前提下）、或者拖拽中途，重算既浪费又可能引起闪烁。所以 `update` 里要先问一句「这次更新要不要重建？」。

这句问话正是上一讲 `checkUpdateAction` 的职责。本讲你会看到它被原样接入。

#### 4.1.2 核心流程

```
编辑器挂载
  └─ constructor(view)
       └─ this.decorations = this.build(view)     // 第一次构建

每次 ViewUpdate
  └─ update(update)
       └─ checkUpdateAction(update) === 'rebuild' ?
            ├─ 是 → this.decorations = this.build(update.view)   // 重建
            └─ 否（'skip' 或 'none'）→ 什么都不做，保留旧装饰

build(view):
  1. 收集「活跃行」（光标/选区覆盖到的行号集合）
  2. 读取拖拽状态 isDrag
  3. syntaxTree(state).iterate({ enter }) 遍历，对每个标记节点产出装饰
  4. 装饰按位置排序，打包成 DecorationSet 返回
```

为什么 `update` 里只判断 `'rebuild'`？因为 `'skip'`（拖拽中）和 `'none'`（无需变化）这两种情况下，**保留上一次的装饰就是正确答案**。代码用一个 `if` 把这两路合并掉了，非常简洁。

#### 4.1.3 源码精读

先看插件骨架（构造、`update`、`build` 的外壳）：

[livePreview.ts:47-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L47-L59) —— 用 `ViewPlugin.fromClass` 定义插件类：实例字段 `decorations` 在构造时由 `build` 填充；`update` 里只有当 `checkUpdateAction(update) === 'rebuild'` 时才重新 `build`。

```ts
constructor(view: EditorView) {
  this.decorations = this.build(view);
}

update(update: ViewUpdate) {
  if (checkUpdateAction(update) === 'rebuild') {
    this.decorations = this.build(update.view);
  }
}
```

末尾的配置对象告诉 CodeMirror「本插件的装饰来自实例的 `decorations` 字段」：

```ts
{
  decorations: (v) => v.decorations,
}
```

再看 `build` 开头对「活跃行」和「拖拽态」的收集：

[livePreview.ts:61-75](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L61-L75) —— 把选区覆盖到的所有行号塞进 `activeLines` 集合（块级标记用它判断），并从 `mouseSelectingField` 读出拖拽态 `isDrag`。

```ts
const activeLines = new Set<number>();
for (const range of state.selection.ranges) {
  const startLine = state.doc.lineAt(range.from).number;
  const endLine = state.doc.lineAt(range.to).number;
  for (let l = startLine; l <= endLine; l++) activeLines.add(l);
}
const isDrag = state.field(mouseSelectingField, false);
```

注意 `state.field(mouseSelectingField, false)` 的第二个参数 `false` 是「字段不存在时不要报错，返回 `undefined`」。这是一种防御性写法：万一宿主没启用 `mouseSelectingField`，插件也不会崩。`isDrag` 会在后面决定要不要加 `-visible` 类。

`build` 的结尾把装饰打包：

[livePreview.ts:141-144](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L141-L144) —— 先手动按 `from` 升序排序，再把第二参数设为 `true` 告知 CodeMirror「数据已排序」，从而高效构建装饰树。

```ts
return Decoration.set(
  decorations.sort((a, b) => a.from - b.from),
  true
);
```

至于 `checkUpdateAction` 内部如何把更新分类，上一讲（u2-l3）已经讲透，这里给一个速查表，方便你在读 `update` 时对照：

[pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65) —— 按优先级短路判定，文档/视口/重配置变化 → `rebuild`；拖拽刚结束 → `rebuild`；拖拽中 → `skip`；选区变化 → `rebuild`；其余 → `none`。

| 更新情形 | 返回动作 | `livePreviewPlugin` 的反应 |
|----------|----------|----------------------------|
| 文档改变 / 视口改变 / 重配置 | `rebuild` | 重新 `build` |
| 拖拽刚结束（上一帧在拖，这一帧不拖了） | `rebuild` | 重新 `build`，让标记按最终选区状态显示 |
| 拖拽中 | `skip` | 什么都不做，保留旧装饰（消除闪烁） |
| 仅选区改变 | `rebuild` | 重新 `build`（光标进出标记要重算显隐） |
| 其余（如纯滚动且视口未变） | `none` | 什么都不做 |

#### 4.1.4 代码实践

**实践目标**：亲眼看到「装饰随状态变化而重建」，并验证 `update` 的短路逻辑。

**操作步骤**（源码阅读 + 跑测试）：

1. 打开测试文件 [livePreview.test.ts:176-210](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L176-L210)，它有「文档改变后重建」「选区改变后插件仍可用」两个用例。
2. 在仓库根目录运行 `npm test`（Vitest + jsdom）。
3. 在 `src/plugins/livePreview.ts` 的 `update` 方法第一行临时加一行 `console.log('update:', checkUpdateAction(update));`（仅用于观察，实践后请还原）。

**需要观察的现象**：测试通过；控制台里能看到 `update:` 打印出 `rebuild`（文档/选区变化时）以及 `none`（无变化时）。

**预期结果**：`should rebuild decorations on document change` 用例插入 `**bold** text` 后，`decorations.size` 比初始值更大。

**待本地验证**：`console.log` 在 Vitest 默认输出里是否可见取决于配置；若看不到，可改用 `expect(checkUpdateAction(update)).toBe(...)` 风格的断言来验证。实践完成后请删除临时代码，不要真的修改源码。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `update` 里的 `if` 判断、直接每次都 `this.build`，会有什么问题？

**答案**：功能上不会错（每次都重建也能得到正确装饰），但性能下降，且在拖拽中途频繁重建会引起标记反复显隐的闪烁——这正是 `checkUpdateAction` 在拖拽中返回 `'skip'` 想避免的。

**练习 2**：`build` 为什么不读 `view.viewport`（可见视口）来限制遍历范围？

**答案**：行内标记的显隐取决于光标位置，与视口无关；而且 Lezer 的语法树在视口外也可能已经解析好。这里全树遍历足够简单正确，没有按视口裁剪的必要（对比之下，有些重量级渲染插件会按视口懒算）。

---

### 4.2 syntaxTree.iterate：识别「标记节点」

#### 4.2.1 概念说明

这是本讲最重要的一个区分：**标记节点（mark node）vs 内容节点（content node）**。

以 `**粗体**` 为例，Lezer 的 Markdown 语法树大致长这样：

```
StrongEmphasis          ← 内容节点：整个 "粗体"（含星号）的结构容器
├── EmphasisMark "**"   ← 标记节点：第一个 **，纯标点
├── (文本 "粗体")
└── EmphasisMark "**"   ← 标记节点：第二个 **
```

- **内容节点**（`StrongEmphasis`、`Emphasis`、`InlineCode`、`ATXHeading1`…）描述「这是一段什么内容」，是结构性的。
- **标记节点**（`EmphasisMark`、`CodeMark`、`HeaderMark`…）就是那些**当渲染显示时应当被隐藏的原始符号**：`**`、`` ` ``、`#`、`-`、`>`。

所以 Live Preview 隐藏的就是标记节点。`livePreviewPlugin` 的核心动作，就是从语法树里把所有标记节点捞出来。而「给内容套样式」（粗体变粗、标题变大）是另一个插件 `markdownStylePlugin`（下一讲 u2-l5）的事——这就是本库的**职责分离**：本插件只管「标记藏不藏」，不管「内容长什么样」。

`syntaxTree(state).iterate({ enter })` 是 Lezer 提供的遍历 API：它深度优先地访问每个节点，对每个节点调用你给的 `enter` 回调。回调里你可以读 `node.name`、`node.from`、`node.to`，并返回 `false` 来阻止继续下钻到子节点。

#### 4.2.2 核心流程

```
syntaxTree(state).iterate({
  enter(node) {
    1. node.name 在 markTypes 列表里吗？不在 → return（跳过）
    2. 在被跳过的父节点（代码块）内部吗？是 → return      （见 4.3）
    3. 是公式专用的 CodeMark 吗？是 → return              （见 4.3）
    4. 是块级标记还是行内标记？
         - 块级（#/-> ）  → 用「行是否活跃」决定加不加 -visible
         - 行内（**/~~/`）→ 调 shouldShowSource 决定加不加 -visible   （见 4.4）
    5. push 一个 Decoration.mark 装饰
  }
})
```

第 1 步是本节的重点：用一个数组 `markTypes` 列出所有关心的标记名，用 `includes` 过滤。

#### 4.2.3 源码精读

`markTypes` 名单定义在 `enter` 回调内部：

[livePreview.ts:81-90](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L81-L90) —— 六类标记节点，对应 `*`/`_`、`~~`、`` ` ``、`#`、`-`/`*`/`+`、`>`。不在名单里的节点直接 `return`。

```ts
const markTypes = [
  'EmphasisMark',       // * 或 _
  'StrikethroughMark',  // ~~
  'CodeMark',           // `
  'HeaderMark',         // #
  'ListMark',           // - 或 *
  'QuoteMark',          // >
];
if (!markTypes.includes(node.name)) return;
```

> 小提示：`ListMark` 实际上覆盖 `-`、`*`、`+` 三种无序列表符号（Lezer 的 CommonMark 语法把这三种都解析成 `ListMark`）。所以「列表项的 `+` 标记」其实**已经被处理了**，并不是「未处理的标记类型」。这一点对后面的实践任务很重要——别想当然。

外层遍历的入口：

[livePreview.ts:78-79](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L78-L79) —— 对整棵语法树做一次 `iterate`。

```ts
syntaxTree(state).iterate({
  enter: (node) => { /* ... */ },
});
```

> 关于 `~~`：测试里有一句注释提醒——默认的 CommonMark 解析器**不一定支持**删除线，需要 GFM 扩展。本项目 demo 用的是 `markdown({ base: markdownLanguage, extensions: [Table] })`（见 `demo/main.ts`），只加了表格扩展，没加删除线扩展，所以 `~~strikethrough~~` 在默认配置下不会产生 `StrikethroughMark` 节点。测试 [livePreview.test.ts:62-72](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L62-L72) 因此只断言「插件不崩溃」，而不断言「检测到了」。

#### 4.2.4 代码实践

**实践目标**：用日志看清 Lezer 到底为一段 Markdown 产生了哪些节点，验证「标记节点 vs 内容节点」的区分。

**操作步骤**（源码阅读型，观察为主）：

1. 在 `src/plugins/livePreview.ts` 的 `enter` 回调第一行临时插入（实践后还原）：

   ```ts
   // 示例代码：仅用于观察节点名，不是项目原有代码
   enter: (node) => {
     console.log(node.name, state.doc.sliceString(node.from, node.to));
     // ...原有逻辑
   }
   ```

2. 在 demo（`demo/` 目录 `npm run dev`）里，把任意编辑器内容改成：

   ```markdown
   # 标题
   **粗体** *斜体* `代码`
   - 列表项
   > 引用
   ```

**需要观察的现象**：控制台会打印出一串节点名和对应文本。你会看到成对出现的 `EmphasisMark "**"`、单个 `HeaderMark "#"`、`ListMark "-"`、`QuoteMark ">"`、`CodeMark "`"`，以及它们外层的 `StrongEmphasis`、`ATXHeading1`、`BulletList`、`Blockquote` 等内容节点。

**预期结果**：你能指出哪些是标记节点（应在 `markTypes` 名单里、会被本插件处理），哪些是内容节点（不在名单里、被 `markdownStylePlugin` 处理）。

**待本地验证**：实际打印出的节点名以你本地 Lezer 版本为准；若某节点名与本文不符，以本地日志为准。

#### 4.2.5 小练习与答案

**练习 1**：`**粗体**` 会被 `enter` 访问到几个 `EmphasisMark` 节点？各覆盖原文的哪几个字符？

**答案**：2 个，分别覆盖开头和结尾的 `**`。标记节点是「成对」出现的，本插件给每一对中的每一个都单独套类。

**练习 2**：为什么用 `markTypes.includes(node.name)` 而不是用一个大 `switch`？

**答案**：数组 + `includes` 让「关心哪些标记」成为一个清晰的数据清单，新增/删除标记只改数组，逻辑分支（块级 vs 行内）另写。数据与逻辑分离，便于维护。

---

### 4.3 isInsideSkippedParent：跳过代码块与公式标记

#### 4.3.1 概念说明

并不是所有 `EmphasisMark` / `CodeMark` 都该被本插件处理。考虑这段：

````markdown
```js
const a = "**not bold**";
```
````

代码块里面的 `**` 只是普通字符串，根本不是 Markdown 强调。如果本插件也给它们套上「隐藏标记」的类，会把代码内容弄乱。所以需要一条规则：**凡是位于代码块（`FencedCode` / `CodeBlock`）内部的标记，一律跳过。**

`isInsideSkippedParent` 就是这条规则的实现：它从当前节点出发，沿 `parent` 链一路向上，只要碰到任何一个名字在 `SKIP_PARENT_TYPES` 名单里的祖先，就返回 `true`（该跳过）。

此外还有第二条例外：**数学公式专用的 `CodeMark`**。本项目把行内公式 `` `$E=mc^2$` `` 复用了 `InlineCode` 节点（用反引号包裹 `$…$`，详见 u3-l2）。这些反引号 `CodeMark` 应该由 `mathPlugin` 接管，而不是被本插件当作普通行内代码隐藏。所以本插件对 `CodeMark` 多做了一次文本判断：如果它所在的 `InlineCode` 文本形如 `` `$…$` ``，就跳过。

#### 4.3.2 核心流程

```
对每个标记节点：

(1) isInsideSkippedParent(node)?
     沿 parent 链向上，任一祖先名 ∈ {FencedCode, CodeBlock} → 跳过

(2) node.name === 'CodeMark' 且父节点是 InlineCode，
    且该 InlineCode 文本以 ` 开头 $、结尾 $` → 跳过（交给 mathPlugin）

否则 → 继续处理
```

#### 4.3.3 源码精读

被跳过的父节点类型名单：

[livePreview.ts:14-18](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L14-L18) —— 一个 `Set`，成员是 `FencedCode`（带围栏 ```` ``` ```` 的代码块）和 `CodeBlock`（缩进式代码块）。

```ts
const SKIP_PARENT_TYPES = new Set(['FencedCode', 'CodeBlock']);
```

向上查找的实现：

[livePreview.ts:23-34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L23-L34) —— `while (parent)` 循环逐层上爬，命中名单即 `return true`；爬到根（`parent` 为 `null`）仍未命中则 `return false`。

```ts
function isInsideSkippedParent(node): boolean {
  let parent = node.node.parent;
  while (parent) {
    if (SKIP_PARENT_TYPES.has(parent.name)) {
      return true;
    }
    parent = parent.parent as { name: string; parent: unknown } | null;
  }
  return false;
}
```

调用点：

[livePreview.ts:92-95](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L92-L95) —— 命中即 `return`，不产出装饰。

```ts
if (isInsideSkippedParent(node)) {
  return;
}
```

公式 `CodeMark` 的第二条例外：

[livePreview.ts:98-107](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L98-L107) —— 取父节点 `InlineCode` 的完整文本，若以 `` `$ `` 开头、以 `` $` `` 结尾，判定为行内公式，跳过。

```ts
if (node.name === 'CodeMark') {
  const parent = node.node.parent;
  if (parent && parent.name === 'InlineCode') {
    const text = state.doc.sliceString(parent.from, parent.to);
    if (text.startsWith('`$') && text.endsWith('$`')) {
      return;
    }
  }
}
```

测试侧的对应用例：

[livePreview.test.ts:212-223](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L212-L223) —— 验证 `` `$E=mc^2$` `` 这类公式标记不会让插件出错（它应被 `mathPlugin` 接管）。

> 注意这两条「跳过」规则的区别：`isInsideSkippedParent` 是**按祖先节点名**判断（结构层面），而公式例外是**按节点内文本内容**判断（语义层面）。两者互补。

#### 4.3.4 代码实践

**实践目标**：验证代码块内部的标记不会被本插件处理。

**操作步骤**（源码阅读 + 构造测试）：

1. 参考 `createTestView` 的写法 [livePreview.test.ts:12-27](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L12-L27)，自己写一个临时测试（放在 `__tests__` 下，或用 `vitest` 的 `it.skip`/临时文件，跑完删除）：

   ```ts
   // 示例代码：用于验证代码块内标记被跳过
   it('skips marks inside fenced code', () => {
     const view = createTestView('```\n**not bold**\n```');
     const plugin = view.plugin(livePreviewPlugin);
     // 统计 inline 标记装饰数量
     let count = 0;
     plugin?.decorations.between(0, view.state.doc.length, (_f, _t, deco) => {
       if ((deco.spec as any).class?.includes('cm-formatting-inline')) count++;
     });
     expect(count).toBe(0); // 代码块里的 ** 不应被当成强调标记
     view.destroy();
   });
   ```

2. 运行 `npm test`。

**需要观察的现象**：代码块里的 `**not bold**` 不产生 `cm-formatting-inline` 装饰。

**预期结果**：`count` 为 `0`，断言通过。

**待本地验证**：具体 `**` 在 FencedCode 内是否会被 Lezer 解析成 `EmphasisMark` 取决于语法；若 Lezer 根本没把它解析成标记节点，本测试照样成立（因为根本没节点进入 `enter`）。无论哪种情况，结论一致：代码块内标记不会被错误隐藏。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `isInsideSkippedParent` 要「逐层向上」而不是只看直接父节点？

**答案**：因为标记节点可能嵌套较深，例如 `FencedCode` → `*`（代码内容）→ …… → `EmphasisMark`。直接父节点未必是 `FencedCode`，必须爬到任意祖先命中才算数。逐层向上保证了任意深度嵌套都能识别。

**练习 2**：如果以后要新增「跳过数学块（```` ```math ```` 围栏块）内部的标记」，该改哪里？

**答案**：把 `'FencedCode'` 不能简单替换（它仍要处理普通代码块），但需要判断该 `FencedCode` 的语言是否为 `math`——这要看 `FencedCode` 的 `CodeInfo` 子节点。最小改动是扩充 `SKIP_PARENT_TYPES` 或在 `isInsideSkippedParent` 之外加一条针对 math 围栏的判断。这正是后面 `codeBlock.ts` / `math.ts` 用 `CodeInfo` 提取语言时处理的同类问题（见 u5-l2、u3-l3）。

---

### 4.4 Decoration.mark + CSS 类切换：平滑显隐动画

#### 4.4.1 概念说明

走到这里，我们已经锁定了一个「该被处理的标记节点」。剩下的问题只有两个：

1. 这个标记现在该「可见」（光标碰到它了）还是「隐藏」（光标不在）？
2. 用什么手段让它在 DOM 上平滑地出现/消失？

第 1 个问题的答案来自 `shouldShowSource`（u2-l2）。第 2 个问题的答案就是本节的标题：**用 `Decoration.mark` 套 CSS 类，靠 CSS 过渡动画显隐**，而不是用 `Decoration.replace` 把它替换掉。

为什么要「mark + CSS」而不是「replace + Widget」？这是本讲的关键设计取舍：

- **文本必须留在 DOM 里、必须可编辑。** Live Preview 的核心体验是「光标进入时，标记变成可编辑的原始字符」。`Decoration.mark` 只是给现有文本加一个类，文本始终在 `contentDOM` 里，光标能自然地走进去、删改它。而 `Decoration.replace` 会把这段文本替换成另一个 DOM（Widget）或干脆隐藏，文本就脱离了可编辑内容流——你得自己处理「点哪里→落到哪个文档位置」（`codeBlockWidget` 后来正是为这个付出了 Canvas 丈量的复杂代价，见 u5-l3）。
- **显隐要平滑。** 加/去一个类，浏览器会用 CSS `transition` 平滑过渡 `max-width`、`opacity`，几乎零成本且顺滑。而 replace 模式是「挂载/卸载一段新 DOM」，容易出现布局抖动。
- **光标映射简单。** 文本 DOM 和文档位置一一对应，CodeMirror 的坐标计算天然正确。

所以一条分界线很清楚：**本插件处理的都是「同一串字符、只是显示与否」的场景，适合 mark + CSS；而 math/table/code 这些「渲染出和原文完全不同的东西」的场景，才用 replace + Widget（见第三单元起）。**

代码里还把标记分成两类，用不同的动画手法：

- **行内标记**（`EmphasisMark`/`StrikethroughMark`/`CodeMark`）：用 `max-width: 0 → 4ch` + `opacity` 过渡，让标记像「从缝隙里滑出来」。
- **块级标记**（`HeaderMark`/`ListMark`/`QuoteMark`）：用 `font-size: 0.01em → 1em` + `opacity` 过渡，让整行行首符号淡入。

两者的「是否可见」判定略有不同，下面分别看。

#### 4.4.2 核心流程

```
判定 isBlock = node.name ∈ {HeaderMark, ListMark, QuoteMark}
lineNum = 该标记所在行号

if (isBlock):
    块级动画（font-size）
    可见条件 = 该行是活跃行(lineNum ∈ activeLines) 且 非拖拽
    class = 可见 ? 'cm-formatting-block cm-formatting-block-visible'
                 : 'cm-formatting-block'
else:
    行内动画（max-width）
    可见条件 = shouldShowSource(state, node.from, node.to) 且 非拖拽
    class = 可见 ? 'cm-formatting-inline cm-formatting-inline-visible'
                 : 'cm-formatting-inline'

push Decoration.mark({ class }).range(node.from, node.to)
```

注意行内和块级的「可见判定」用了不同依据：

- **行内**调用 `shouldShowSource(state, node.from, node.to)`，按标记自身的精确区间判断光标是否相交（端点也算命中）。
- **块级**只看「光标是否在该行」（`activeLines`），因为 `#`、`-`、`>` 这类行首符号没必要精确到字符级——只要光标在那一行，整行的行首符号都亮出来更自然。

两条都额外叠加了 `&& !isDrag`：拖拽中一律不显示，避免选区扫过时标记反复闪烁。

#### 4.4.3 源码精读

块级 vs 行内的分流，以及各自的类名拼装：

[livePreview.ts:109-137](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L109-L137) —— 先算出 `isBlock`、`lineNum`、`isActiveLine`，再分支产出装饰。

块级分支：

[livePreview.ts:115-123](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L115-L123) —— 用 `isActiveLine && !isDrag` 决定是否叠加 `-visible` 类。

```ts
if (isBlock) {
  const cls =
    isActiveLine && !isDrag
      ? 'cm-formatting-block cm-formatting-block-visible'
      : 'cm-formatting-block';
  decorations.push(
    Decoration.mark({ class: cls }).range(node.from, node.to)
  );
}
```

行内分支：

[livePreview.ts:124-137](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L124-L137) —— 先用 `node.from >= node.to` 过滤掉零长度区间，再调 `shouldShowSource` 决定可见性。

```ts
else {
  if (node.from >= node.to) return;            // 防御：零长度区间不处理
  const isTouched = shouldShowSource(state, node.from, node.to);
  const cls =
    isTouched && !isDrag
      ? 'cm-formatting-inline cm-formatting-inline-visible'
      : 'cm-formatting-inline';
  decorations.push(
    Decoration.mark({ class: cls }).range(node.from, node.to)
  );
}
```

这里复用的决策函数：

[shouldShowSource.ts:31-53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L31-L53) —— 三步短路：开关未开 → `false`；拖拽中 → `false`；否则按选区相交判定。

它内部第三步的相交判定（行内标记「可见」的数学依据）：

[shouldShowSource.ts:45-50](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L45-L50) —— 遍历每个选区 Range，只要有一个与标记区间 \([from, to]\) 相交即返回 `true`。

两个区间 \([a,b]\) 与 \([c,d]\) 相交的充要条件是 \(a \le d \land c \le b\)。这里选区是 \([\text{range.from}, \text{range.to}]\)、标记是 \([\text{from}, \text{to}]\)，所以代码写成：

```ts
if (range.from <= to && range.to >= from) {
  return true;
}
```

接下来看这些 CSS 类如何变成动画。基础类（隐藏态）：

[default.ts:42-61](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L42-L61) —— `.cm-formatting-inline` 的 `max-width: 0`、`opacity: 0`，并声明 `max-width/opacity/transform` 的过渡。

```ts
'.cm-formatting-inline': {
  maxWidth: '0',
  opacity: '0',
  transform: 'scaleX(0.8)',
  transition: `
    max-width 0.2s cubic-bezier(0.2, 0, 0.2, 1),
    opacity 0.15s ease-out,
    transform 0.15s ease-out
  `,
  // ...
},
```

可见态类（叠加后）：

[default.ts:63-69](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L63-L69) —— `.cm-formatting-inline-visible` 把 `max-width` 撑到 `4ch`、`opacity` 到 `1`，触发上面声明的过渡，于是标记「滑出来」。

```ts
'.cm-formatting-inline-visible': {
  maxWidth: '4ch',
  opacity: '1',
  transform: 'scaleX(1)',
  // ...
},
```

块级标记的两态：

[default.ts:72-86](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L72-L86) —— `.cm-formatting-block` 用 `font-size: 0.01em` + `opacity: 0`，`.cm-formatting-block-visible` 改为 `font-size: 1em` + `opacity: 0.6`，靠 `font-size` 过渡淡入。

关键点：动画完全靠「类名切换 → CSS 过渡」完成。插件只负责决定加不加 `-visible`，不操作 DOM、不计时——过渡交给浏览器，顺滑且高性能。

测试如何验证这条逻辑：

[livePreview.test.ts:114-136](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L114-L136) —— 光标在 `**bold**` 内部（位置 4）时，装饰应含 `cm-formatting-inline`（可见）。

[livePreview.test.ts:138-155](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L138-L155) —— 光标移到文本末尾（位置 13）时，`**bold**` 的装饰不再含 `visible` 类。

[livePreview.test.ts:157-173](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L157-L173) —— 光标在标题行（位置 5）时，行首 `#` 应含 `cm-formatting-block-visible`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：学会给 `livePreviewPlugin` 扩展一种「新的标记类型」的显隐处理，完整复用 `shouldShowSource` 与 CSS 类切换逻辑。

> 先纠正一个误区：任务示例里说的「列表项的 `+` 标记」其实**已经被 `ListMark` 覆盖**了（Lezer 把 `-`、`*`、`+` 都解析成 `ListMark`）。所以 `+` 不是一个「未处理」的标记。下面的步骤先教你**如何确认**这一点，再找一个真正未处理的标记来扩展。

**操作步骤**：

1. **先探测节点名**（确认 `+` 已被处理）。在 `enter` 回调开头临时加一行日志（实践后还原）：

   ```ts
   // 示例代码
   enter: (node) => {
     console.log(node.name, JSON.stringify(state.doc.sliceString(node.from, node.to)));
     // ...
   }
   ```

   在 demo 里输入 `+ 加号列表`，观察控制台：你会看到 `ListMark "+"`，说明 `+` 已在 `markTypes` 名单里、已被处理。**不要重复添加它。**

2. **找一个真正未处理的标记**。常见候选是 GFM 任务列表项的方括号 `- [ ]` / `- [x]`（其标记节点名因 Lezer/GFM 扩展版本而异，**待本地确认**：用上面同样的日志探测它的 `node.name`，比如可能是 `TaskMarker` 或 `Task`）。假设你探测到的节点名为 `XxxMark`。

3. **扩展 `markTypes`**：

   ```ts
   // 示例代码：把探测到的真实节点名填进去
   const markTypes = [
     'EmphasisMark', 'StrikethroughMark', 'CodeMark',
     'HeaderMark', 'ListMark', 'QuoteMark',
     'XxxMark', // 新增
   ];
   ```

4. **决定它是块级还是行内**，从而落到正确的分支。任务方括号是行首符号、性质接近块级，可归入块级（复用 `isActiveLine && !isDrag`）；如果你想精确到字符级，则归入行内（复用 `shouldShowSource`）。两种写法都无需新建逻辑，只要让该 `node.name` 进入对应分支：

   - 归入块级：把 `'XxxMark'` 加入第 109 行的 `isBlock` 数组 `[ 'HeaderMark', 'ListMark', 'QuoteMark' ]`。
   - 归入行内：保持 `isBlock` 为 `false`，自动走 `shouldShowSource` 分支。

5. **（可选）补样式**。若想让它的显隐动画与现有标记一致，直接复用 `cm-formatting-inline` / `cm-formatting-block` 即可，无需改主题；若想要专属外观，可在 `editorTheme` 里加新类（属于 u7-l1 的内容）。

**需要观察的现象**：扩展后，把光标移到该标记所在行/区间，标记平滑出现；移开后平滑消失；拖拽选区扫过时不闪烁。

**预期结果**：新增标记的显隐行为与现有六类标记一致，且**没有引入新的判定逻辑**——全部复用 `shouldShowSource`（行内）或 `activeLines`（块级）。

**待本地验证**：第 2 步的节点名必须以你本地日志为准，切勿照抄本文假设的 `XxxMark`/`TaskMarker`。若你用的 Markdown 配置（如 demo 的 `markdown({ base: markdownLanguage, extensions: [Table] })`）根本不解析该语法，则不会有对应节点产生，扩展也就无从生效——这本身说明「扩展前先探测」是必要步骤。

#### 4.4.5 小练习与答案

**练习 1**：行内标记为什么调 `shouldShowSource`，而块级标记只看 `activeLines`？

**答案**：行内标记（如 `**`）藏在文字中间，光标必须真正碰到它的精确区间才该显示，所以用基于区间相交的 `shouldShowSource`。块级标记（如行首 `#`）是整行性质，只要光标在该行就该亮出来，用「行级」的 `activeLines` 判断更自然、也更省事。

**练习 2**：把本插件的 `Decoration.mark` 换成 `Decoration.replace`（替换成空 Widget）来「隐藏」标记，会破坏什么？

**答案**：会破坏可编辑性——标记文本被替换出可编辑内容流，光标无法自然走进去编辑；同时失去 CSS 过渡带来的平滑动画，变成硬切；并且 CodeMirror 的坐标/位置映射会出问题，需要像 `codeBlockWidget` 那样额外做点击定位（u5-l3）。这正是本库对「同字符仅切换显隐」场景坚持用 mark + CSS 的原因。

**练习 3**：`!isDrag` 这个条件能否去掉？

**答案**：不能去掉。`shouldShowSource` 内部虽然也会在拖拽中返回 `false`（行内分支因此不会加可见类），但块级分支依赖的是 `activeLines`，它**不知道**拖拽状态——若不加 `!isDrag`，拖拽选区扫过多个标题行时，这些行的 `#` 会随选区移动忽闪。`!isDrag` 是块级分支防闪烁的关键，对行内分支则是双保险。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次「带日志的完整追踪」。

**任务**：在本地跑起 demo，把某个编辑器的初始内容改成下面这段，然后回答三个问题。

```markdown
# 标题里也有 **粗体**

普通段落 **bold** 和 `code`。

- 列表项
- 第二项

> 引用块

```js
const s = "**not bold**";
```

行内公式 `$E=mc^2$`。
```

**要回答的问题**（对应三个最小模块）：

1. **syntaxTree.iterate**：用 4.2.4 的临时日志，列出 `enter` 收到的所有标记节点（名字 + 文本）。哪些成对出现？哪些单个出现？
2. **isInsideSkippedParent**：代码块里的 `**not bold**` 是否产生了 `EmphasisMark`？如果有，是否被 `isInsideSkippedParent` 跳过？行内公式的反引号 `CodeMark` 是否被公式例外跳过？
3. **Decoration.mark + CSS**：把光标分别放进 `**bold**`、`#` 标题行、`-` 列表行，用浏览器 DevTools 检查对应文本节点上的 class：分别出现了哪些类？哪个类带 `-visible`？验证你的观察与 4.4 的「行内用 `shouldShowSource`、块级用 `activeLines`」一致。

**验收标准**：你能用一句话向别人解释「光标进入 `**bold**` 时，是哪段代码、加了哪个 CSS 类，让星号平滑出现的」——并且这句话同时涵盖了 `shouldShowSource` 的返回值和 `cm-formatting-inline-visible` 的过渡动画。

> 提示：若用 DevTools 观察不到类名变化，确认 demo 里该编辑器确实启用了 `livePreviewPlugin`（参考 `demo/main.ts` 中各编辑器的 extensions 列表，并非所有编辑器都启用了它）。

## 6. 本讲小结

- `livePreviewPlugin` 是一个 `ViewPlugin`：构造时 `build` 一次，更新时靠 `checkUpdateAction` 判断是否 `rebuild`，`skip`/`none` 时保留旧装饰。
- 它只处理**标记节点**（`EmphasisMark` 等 6 类），用 `markTypes` 数组 + `syntaxTree.iterate` 过滤；内容节点（`StrongEmphasis` 等）的样式交给另一个插件 `markdownStylePlugin`，职责分离。
- `isInsideSkippedParent` 沿父节点链上爬，跳过 `FencedCode`/`CodeBlock` 内部的标记；另有一条按文本判断的公式 `CodeMark` 例外，把它们让给 `mathPlugin`。
- 行内标记用 `shouldShowSource(state, from, to)` 决定可见（区间相交，端点命中）；块级标记用 `activeLines`（行级）决定可见；两者都叠加 `!isDrag` 防闪烁。
- 显隐全靠 `Decoration.mark` 套 `cm-formatting-inline`/`cm-formatting-block` 两态类，由 CSS 的 `max-width`/`font-size` 过渡完成动画——文本始终留在 DOM 里、始终可编辑。
- 正因为只做「同字符的显隐切换」，这里用 mark + CSS 而非 replace + Widget；后者留给 math/table/code 那种「渲染出完全不同内容」的场景。

## 7. 下一步学习建议

- **下一讲（u2-l5）`markdownStylePlugin`**：看本插件的「搭档」如何给内容节点（标题、粗体、行内代码、链接）套样式类（`cm-header-1`、`cm-strong`…），并理解两者的关注点分离。
- **横向对照（第三单元起）**：等学到 `mathPlugin`（u3-l2）和 `codeBlockWidget`（u5-l3）时，回头对比「mark + CSS」与「replace + Widget」两种策略的取舍，体会本讲为什么坚持前者。
- **动手扩展**：用本讲 4.4.4 的方法，给一个真实存在的标记类型加上显隐处理，作为通往 u7-l4「自定义插件开发」的热身。
