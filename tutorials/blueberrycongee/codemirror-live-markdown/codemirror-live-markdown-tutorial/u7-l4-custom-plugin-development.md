# 二次开发：编写自己的 Live Preview 插件

> 本讲是整套手册的**收官篇**。前面六单元你读完了库里的所有零件——决策函数、更新助手、两种装饰策略、各种 Widget。本讲不再介绍新零件，而是把这些零件**组装成你自己的插件**。我们会以「实现一个 `==高亮==` 语法插件」为贯穿全讲的范例，复用 `shouldShowSource`、`checkUpdateAction`、`mouseSelectingField`，走完从需求分析到 demo 验证的完整开发流程。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出开发一个 Live Preview 插件的**五步法**，并判断每一步该选什么（ViewPlugin 还是 StateField、mark+CSS 还是 replace+Widget、语法树还是正则扫描）。
2. 复用本库的 **core 机制**（`shouldShowSource`、`checkUpdateAction`、`mouseSelectingField`）写出与原生体验一致的插件——光标进入显源码、拖拽不闪烁、无谓更新被跳过。
3. 把写好的插件**导出**进 `src/index.ts`，并在 `demo` 里接入验证，理解 demo 借 Vite 别名「改源码即时生效」的开发回路。
4. 独立实现一个把 `==高亮==` 替换成 `<mark>` 元素的自定义 Live Preview 插件。

## 2. 前置知识

本讲默认你已读完 u3-l1（WidgetType 与 Decoration.replace）与 u7-l3（整体架构）。三句话快速回忆：

- **一个 Live Preview 插件的本质**就是一个产出 `DecorationSet` 的 CodeMirror 扩展——要么是 `ViewPlugin`（行内装饰、随视口重算），要么是 `StateField`（块级装饰、值进状态快照）。
- **决策心脏 `shouldShowSource(state, from, to)`**：把「Facet 开关 + 拖拽状态 + 选区相交」三件事压成一个布尔值，`true` 显源码、`false` 显渲染结果（u2-l2）。
- **`checkUpdateAction(update)`**：把一次更新归成 `rebuild`/`skip`/`none`，是 ViewPlugin 复用的更新逻辑（u2-l3）。

一个贯穿全讲的关键认知：本库的 `markdown()` 来自官方 `@codemirror/lang-markdown`，demo 里只追加了 `Table` 扩展（[demo/main.ts:328](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L328)）。这意味着 Lezer 语法树**认识** `**bold**`、`[link](url)`、表格，却**不认识** `==高亮==`、`[[wiki]]` 这类扩展语法。当目标语法不在语法树里时，插件就只能像 `linkPlugin` 处理 Wiki 链接那样**用正则全文扫描**——这是本讲范例的核心约束，也是它区别于 `livePreviewPlugin` 的根本之处。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/plugins/livePreview.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts) | **模板 A**：最简洁的 ViewPlugin 范例——语法树遍历 + mark+CSS + 复用 core |
| [src/plugins/link.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts) | **模板 B**：最完整的范例——语法树 + 正则双路径、skipRanges、replace+Widget、工厂函数、内联更新逻辑 |
| [src/widgets/linkWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts) | WidgetType 子类范例：`eq`/`toDOM`/`ignoreEvent` 三件套 + URL 安全 |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | 决策心脏，自定义插件复用它获得「光标进入显源码 + 拖拽防闪」 |
| [src/core/pluginUpdateHelper.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts) | `checkUpdateAction`，ViewPlugin 复用它获得「只在必要时重建」 |
| [src/core/mouseSelecting.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts) | `mouseSelectingField`，读取拖拽状态做双层防护 |
| [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) | 桶文件：自定义插件最终在这里 re-export 才能被使用者导入 |
| [demo/main.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts) | 演示页：把扩展装进 `EditorState.create` 并接线 `mouseSelecting` |
| [src/theme/default.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts) | 主题：已内置 `.cm-highlight` 样式（L140-144），自定义插件可借用 |
| [CODEMIRROR_LIVE_PREVIEW_DESIGN.md](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md) | 设计文档第 8 节给出了 `==高亮==` 的完整 StateField 范例（L1514-1615），可作为对照 |

## 4. 核心概念与源码讲解

### 4.1 自定义插件开发流程：五步法

#### 4.1.1 概念说明

所谓「Live Preview 插件」，说穿了就是一个**产出 `DecorationSet` 的 CodeMirror 扩展**。本库把这件事标准化成了一套可复制的工序——**五步法**。你只要照着 `livePreviewPlugin` 或 `linkPlugin` 的骨架填空，就能产出一个行为与原生一致的插件。五步法的每一步都对应一个**选择题**，选错的典型后果是「闪烁」「不可编辑」「拖拽卡顿」。

本库已有的插件就是五步法的不同填法：

| 插件 | 第 1 步：目标语法 | 第 2 步：载体 | 第 3 步：识别方式 | 第 4 步：复用 core | 第 5 步：装饰策略 |
|------|------------------|--------------|-------------------|--------------------|-------------------|
| `livePreviewPlugin` | 标记节点（`EmphasisMark`…） | ViewPlugin | 语法树 `node.name` | `shouldShowSource`+`checkUpdateAction` | mark+CSS |
| `mathPlugin` | 行内公式 `` `$…$` `` | ViewPlugin | 语法树 `InlineCode` 文本特征 | 同上 | replace+Widget（行内）/ mark（源码态） |
| `linkPlugin` | 标准链接 / Wiki 链接 | ViewPlugin | 语法树 `Link` / **正则扫描** | `shouldShowSource`（手写更新逻辑） | replace+Widget / mark |

本讲的范例（`==高亮==`）会走 `linkPlugin` 那一列，因为 `==` 不在语法树里。

#### 4.1.2 核心流程

```
第 1 步：需求分析 —— 要渲染什么 Markdown 元素？渲染结果与原文是「同字符显隐」还是「视觉全不同」？
   │
   ├─ 同字符显隐（如 ** 里的星号）          → 第 5 步倾向 mark+CSS
   └─ 视觉全不同（如 ==x==→<mark>x</mark>） → 第 5 步倾向 replace+Widget
   │
第 2 步：选载体 —— 装饰会不会改变行高？
   │
   ├─ 行内（不带 block:true）  → ViewPlugin（随视口重算，可复用 checkUpdateAction）
   └─ 块级（block:true 改行高）→ StateField（值必须进 EditorState 才能参与测量，见 u3-l1）
   │
第 3 步：识别语法 —— Lezer 语法树认不认这个语法？
   │
   ├─ 认（如 EmphasisMark、Link）          → syntaxTree.iterate({ enter: node => node.name === 'X' })
   └─ 不认（如 ==高亮==、[[wiki]]）         → 正则全文扫描（带 g 标志，每次扫描前重置 lastIndex）
   │
第 4 步：复用 core —— 把决策与更新交给现成函数
   │     shouldShowSource(state, from, to)  → 免费「光标进入显源码 + 拖拽防闪」
   │     checkUpdateAction(update)          → 免费「只在必要时重建」（仅 ViewPlugin）
   │     skipRanges（跳过代码块）           → 免费「不在代码块里误处理」
   │
第 5 步：产出装饰 —— 把决策翻译成 Decoration
   │
   ├─ 渲染态（shouldShowSource 为 false）→ replace+Widget（视觉全不同）或 mark（同字符）
   └─ 源码态（shouldShowSource 为 true） → mark 加提示类，原文可见可编辑
```

五步之间有强约束：第 2 步的「块级 → StateField」是硬规则（u3-l1、u7-l3）；第 3 步的「语法树 vs 正则」取决于第 1 步的目标语法。

#### 4.1.3 源码精读

**模板 A：`livePreviewPlugin` 是最简洁的 ViewPlugin 骨架。** 它的类结构是「一个 `decorations` 字段 + 一个 `build` 方法 + 一个 `update` 方法」的三件套：

[src/plugins/livePreview.ts:47-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L47-L59) —— 构造时 `build` 一次，`update` 时只在 `checkUpdateAction` 返回 `rebuild` 才重算。这是 ViewPlugin 插件的**最小可运行骨架**——你的自定义插件 90% 的代码量都在 `build` 里。

骨架的「出口」是这个访问器，它把字段值交给 CodeMirror：

[src/plugins/livePreview.ts:147-149](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L147-L149) —— `{ decorations: (v) => v.decorations }`。少了它，CodeMirror 拿不到你的装饰，插件等于白写。

`build` 的核心是 `syntaxTree(state).iterate({ enter })`——第 3 步的「识别语法」：

[src/plugins/livePreview.ts:78-90](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L78-L90) —— 用一张 `markTypes` 名单过滤节点。`livePreviewPlugin` 处理的是「标记符号节点」，所以名单里全是 `EmphasisMark`、`HeaderMark` 这类纯符号。如果你的插件要处理「内容节点」，名单换成 `StrongEmphasis`、`Link` 即可（参考 `markdownStylePlugin` 与 `linkPlugin`）。

最后一步「产出装饰」，行内标记只 push 一条 `Decoration.mark`：

[src/plugins/livePreview.ts:128-136](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L128-L136) —— `isTouched`（来自 `shouldShowSource`）决定类名是 `-visible` 还是隐藏态。这就是第 5 步「把决策翻译成装饰」的最短示例。

**模板 B：`linkPlugin` 是功能最全的范例。** 它比模板 A 多出三样本讲范例都要用到的技术：正则扫描、skipRanges、replace+Widget。它的导出形式也不同——是一个**工厂函数**而非裸 `ViewPlugin`，这样可以接收配置：

[src/plugins/link.ts:223-229](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L223-L229) —— `export function linkPlugin(options?)` 先把默认选项与传入选项合并，再 `return ViewPlugin.fromClass(...)`。如果你的插件需要配置（比如高亮颜色），照这个写法包一层工厂函数。

工厂函数内部仍是同样的三件套类结构，只是把 `build` 抽成了独立的 `buildLinkDecorations`：

[src/plugins/link.ts:231-237](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L231-L237) —— 构造时调一次 `buildLinkDecorations`。注意它把 `mergedOptions` 通过闭包传进 `build`，这是工厂函数模式的精髓。

> **两个模板怎么选？** 你的插件只要 mark+CSS、无配置 → 抄模板 A（`livePreviewPlugin`）；要 replace+Widget、要正则扫描、要配置 → 抄模板 B（`linkPlugin`）。本讲的 `==高亮==` 范例走模板 B。

#### 4.1.4 代码实践

**实践目标**：用五步法把 `==高亮==` 的需求拆解成选择清单。

1. 实践目标：在下表里填完本讲范例的五步决策，先不写代码，只做选择题。
2. 操作步骤：对照 4.1.2 的决策树，逐项判断。
3. 需要观察的现象：你会发现自己被「第 3 步」卡住——`==` 不在 Lezer 语法树里。
4. 预期结果（参考答案）：

   | 步骤 | 决策 | 依据 |
   |------|------|------|
   | 第 1 步 | 渲染 `==高亮==`，结果是与原文视觉不同的高亮块 | → replace+Widget |
   | 第 2 步 | 行内高亮，不改行高 | → ViewPlugin（可复用 checkUpdateAction） |
   | 第 3 步 | `==` 不在语法树 | → 正则全文扫描（仿 Wiki 链接） |
   | 第 4 步 | 复用 `shouldShowSource` + `checkUpdateAction` + skipRanges | 与原生体验一致 |
   | 第 5 步 | 渲染态 replace+Widget（`<mark>`），源码态 mark | 双模式 |

5. 关键结论先行：本范例的难点不在写代码，而在第 3 步认出「必须走正则」——这一点做错，插件会一个高亮都不渲染。

#### 4.1.5 小练习与答案

**练习 1**：如果一个插件的装饰带 `block: true`（会改变行高），第 2 步为什么不能选 ViewPlugin？

> **参考答案**：块级装饰会改变行的垂直结构，而视口测量在 ViewPlugin 运行之前就要依据 EditorState 完成。ViewPlugin 的装饰不属于状态快照，无法参与布局测量，所以块级 replace 必须用 StateField（值进 EditorState）。参见 u3-l1、u7-l3 §4.3。

**练习 2**：`livePreviewPlugin` 为什么没有用工厂函数包一层？

> **参考答案**：它没有任何可配置项——标记名单、CSS 类名都是固定的，所以直接 `export const livePreviewPlugin = ViewPlugin.fromClass(...)`。`linkPlugin` 需要 `openInNewTab`/`onWikiLinkClick` 等配置，才用工厂函数 `linkPlugin(options?)` 闭包传递。是否包工厂函数，取决于「插件有没有配置」。

---

### 4.2 复用 core 机制：shouldShowSource、checkUpdateAction 与 mouseSelecting

#### 4.2.1 概念说明

新手写插件最常犯的错是**重新发明轮子**：自己写「光标在不在区间内」、自己写「拖拽时跳过」、自己写「文档变了才重建」。结果往往是「光标进去了不显源码」「拖拽时疯狂闪烁」「每次按键都全篇重算」。

本库的 `src/core/` 把这三件事做成了一组**现成函数/字段**，复用它们能免费获得与原生插件完全一致的体验：

| core 机制 | 解决的问题 | 复用方式 |
|-----------|-----------|----------|
| `shouldShowSource(state, from, to)` | 「光标进入显源码 + 拖拽防闪」 | 在 `build` 里对每个区间调一次，按返回值选装饰 |
| `checkUpdateAction(update)` | 「只在必要时重建」（ViewPlugin） | 在 `update` 里 `if (checkUpdateAction(update) === 'rebuild')` |
| `mouseSelectingField` | 读取拖拽状态做第二层防护 | `state.field(mouseSelectingField, false)` |

注意 `shouldShowSource` **内部已经读取了** `collapseOnSelectionFacet`（总开关）和 `mouseSelectingField`（拖拽态），所以调一次它就同时拿到了「开关」与「拖拽」两个信息——你不需要再单独读。

#### 4.2.2 核心流程

复用 core 后，一个自定义 ViewPlugin 的「决策与更新」全部是声明式的：

```
build(view) 阶段（每个候选区间）：
  isTouched = shouldShowSource(state, from, to)
       │  ← 内部已读 Facet（关则 false）+ 已读 mouseSelectingField（拖拽则 false）
       │
       ├─ false → 渲染态装饰（replace+Widget 或隐藏 mark）
       └─ true  → 源码态装饰（提示类 mark，原文可见）
  （可选）isDrag = state.field(mouseSelectingField, false)  ← 第二层防护

update(update) 阶段：
  if (checkUpdateAction(update) === 'rebuild') { this.decorations = this.build(view); }
       ↑ 结构变化 / 拖拽结束 / 选区变 → rebuild
       ↑ 拖拽中 → skip（不重建，防闪）
       ↑ 其余 → none（复用旧装饰）
```

这套流程的妙处在于：你只管「渲染态长什么样、源码态长什么样」，**何时切换、何时重算**全由 core 接管。

#### 4.2.3 源码精读

**(1) `shouldShowSource`——一次调用拿到「开关 + 拖拽 + 相交」三件事。**

[src/core/shouldShowSource.ts:31-53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L31-L53) —— 三步短路：第一步读 Facet（L33-36，未开则 `false`）；第二步读拖拽 StateField（L39-42，拖拽中则 `false`）；第三步遍历选区做相交判定（L45-50，任一 Range 相交则 `true`）。相交判定的数学含义是两个闭区间有交集：

\[
\text{相交} \iff \text{range.from} \le \text{to} \;\wedge\; \text{range.to} \ge \text{from}
\]

自定义插件只要把自己的区间 `(from, to)` 传进去，就能拿到正确的「显源码 / 显渲染」决策，连同拖拽防闪一起免费获得。`mathPlugin` 就是这么用的：

[src/plugins/math.ts:67-81](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L67-L81) —— `isTouched` 决定走 `Decoration.replace`（渲染态）还是 `Decoration.mark`（源码态 `cm-math-source`），外加 `!isDrag` 第二层防护。这是「双模式 + 双层防护」的标准写法，本讲范例照抄。

**(2) `checkUpdateAction`——一行 `if` 拿到「何时重建」。**

[src/core/pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65) —— 把更新归成 `rebuild`/`skip`/`none` 三动作：结构性变化 → `rebuild`；拖拽结束 → `rebuild`；拖拽中 → `skip`；选区变 → `rebuild`；其余 → `none`。`livePreviewPlugin` 与 `mathPlugin` 的 `update` 都只有一行核心逻辑：

[src/plugins/livePreview.ts:55-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L55-L59) —— 你的自定义 ViewPlugin 的 `update` 长这样就够了。

**(3) 一个反面教材：`linkPlugin` 没有用 `checkUpdateAction`，而是手写了一遍等价逻辑。**

[src/plugins/link.ts:239-264](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L239-L264) —— 它手动展开了「文档变 → 重建；拖拽结束 → 重建；拖拽中 → 跳过；选区变 → 重建」，与 `checkUpdateAction` 完全等价。这是历史遗留写法——**新插件应当直接用 `checkUpdateAction`**，既短又不易漏（`linkPlugin` 就漏掉了 `reconfigured` 判定，不过对链接场景无影响）。

**(4) skipRanges——复用「跳过代码块」的成熟模式。**

`linkPlugin` 用一次语法树遍历收集代码块区间，再用「完全包含」判定把代码块内的链接排除。这套模式可直接搬到任何用正则扫描的插件里（避免把代码块里的 `==` 也高亮）：

[src/plugins/link.ts:107-119](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L107-L119) —— 收集 `skipRanges`（L107-114），再定义 `isInSkipRange` 用「`from >= r.from && to <= r.to`」做完全包含判定（L117-119）。注意它的 `SKIP_PARENT_TYPES` 比 `livePreviewPlugin` 多了 `InlineCode`：

[src/plugins/link.ts:93](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L93) —— `new Set(['FencedCode', 'CodeBlock', 'InlineCode'])`。本讲的 `==高亮==` 范例也应跳过这三类，否则行内代码 `` `a==b` `` 里的 `==` 会被误高亮。

#### 4.2.4 代码实践

**实践目标**：验证「复用 core」与「不用 core」在拖拽时的行为差异。

1. 操作步骤（阅读型）：对照 [src/core/pluginUpdateHelper.ts:55-57](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L55-L57) 的「拖拽中 → skip」分支，设想一个**没有复用 `checkUpdateAction`**、而是「每次 `update` 都无脑 `build`」的自定义插件。
2. 需要观察的现象（推理）：拖拽选区时鼠标每移动一格都会产生一次 `selectionSet` 的更新，无脑 `build` 会全篇重算装饰、重渲染所有 Widget。
3. 预期结果：复用 `checkUpdateAction` 的插件在拖拽中返回 `skip`、零重建；不复用的插件会卡顿闪烁。这就是「复用 core」的直接收益。
4. 本步骤为源码阅读型推理，**待本地验证**（可在本地写两个版本对比）。

#### 4.2.5 小练习与答案

**练习 1**：既然 `shouldShowSource` 内部已经处理了拖拽（拖拽时返回 `false`），为什么 `mathPlugin` 在 `build` 里还要再加一道 `!isDrag` 判断（[math.ts:69](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L69)）？

> **参考答案**：这是「双层防护」的刻意冗余。`shouldShowSource` 的拖拽短路让 `isTouched` 在拖拽中恒为 `false`，于是渲染态分支 `!isTouched && !isDrag` 在拖拽中本就会走渲染态；`!isDrag` 是第二道闸门，确保**即使将来 `shouldShowSource` 的拖拽逻辑被改动**，Widget 也不会在拖拽中与源码态来回切换造成抖动。冗余是为了稳健。

**练习 2**：如果你要给自定义插件加一个「只在文档变化时重建、不响应选区变化」的行为（类似 `markdownStylePlugin`），该改 `checkUpdateAction` 还是自己写 `update`？

> **参考答案**：自己写 `update`。`checkUpdateAction` 内置了「选区变 → rebuild」，无法关闭；如果你的插件与光标无关（纯静态样式），就别复用它，直接 `if (update.docChanged || update.viewportChanged) rebuild`。这正是 `markdownStylePlugin` 不复用 `checkUpdateAction`、也不调 `shouldShowSource` 的原因（u2-l5）。

---

### 4.3 导出与 demo 验证：从局部扩展到工程化交付

#### 4.3.1 概念说明

写完插件代码只是完成了一半。要让插件**可被使用者导入**、并**能在真实编辑器里看到效果**，还差两步：把它加进 `src/index.ts` 的桶文件导出，再把它接进 `demo` 验证。

`src/index.ts` 是包的**唯一入口**——`package.json` 的 `main`/`module`/`types` 都指向由它打包出来的产物（u1-l2、u1-l3）。一个插件文件哪怕写得再好，只要没在 `index.ts` 里 `export`，使用者就 `import` 不到。而 `demo` 是「改源码即时生效」的快速验证回路：它借 Vite 别名把包名直接指向 `src/index.ts`（u1-l3），所以你在 `src/` 下加的代码、在 `index.ts` 里加的导出，刷新浏览器就能看到。

#### 4.3.2 核心流程

```
开发回路：
  在 src/plugins/ 写插件文件（如 highlight.ts）
       │
       ├──→ 在 src/index.ts 加一行 export { highlightPlugin } from './plugins/highlight'
       │       （外部使用者才能 import）
       │
       └──→ 在 demo/main.ts 的某个 EditorState.create 的 extensions 数组里加入 highlightPlugin()
              + 确保该编辑器已接线 mouseSelectingField、collapseOnSelectionFacet.of(true)
       │
       ▼  demo 目录 npm run dev → 浏览器打开 → 刷新
  在真实编辑器里输入 ==高亮== 观察效果，改源码即时生效
```

两个易错点：① 忘了在 `index.ts` 导出 → demo 里 `import` 报错；② 接进了 demo 却没接线 `mouseSelectingField` → 拖拽时插件会闪烁（因为 `shouldShowSource` 读不到拖拽态）。

#### 4.3.3 源码精读

**导出：`index.ts` 是分类的桶文件。**

[src/index.ts:16-31](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L16-L31) —— Plugins 层逐行 re-export，命名约定是：ViewPlugin 用 `xxxPlugin`、StateField 用 `xxxField`。新增高亮插件（ViewPlugin）应在这里加一行 `export { highlightPlugin } from './plugins/highlight';`，类型用 `export type` 单独转发（参考 L27、L29 的写法）。

注意 Core 层已经把你要复用的三件套都导出了：

[src/index.ts:9-14](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L9-L14) —— `shouldShowSource`、`checkUpdateAction`、`mouseSelectingField`、`collapseOnSelectionFacet` 全部对外可用。这意味着**外部开发者**（不修改本库、只消费 npm 包的人）也能复用它们写自己的插件——这正是本库「模块化、灵活集成」理念的落点。

**验证：demo 的接线三件套。**

demo 里每个编辑器都遵循同一套接线模板。以第一个编辑器为例：

[demo/main.ts:323-349](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L323-L349) —— `EditorState.create` 的 `extensions` 数组。关键扩展依次是 `markdown(...)`（语法树）、`collapseOnSelectionFacet.of(true)`（总开关）、`mouseSelectingField`（拖拽态）、`livePreviewPlugin`/`markdownStylePlugin`（基础）、各特性插件、`editorTheme`。你的 `highlightPlugin()` 就加在这串里，挨着 `linkPlugin(...)` 即可。

光装进 `extensions` 还不够，必须**接线 mouseSelecting**，否则拖拽防闪失效：

[demo/main.ts:309-319](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L309-L319) —— `setupMouseSelecting` 把 `mousedown`/`mouseup` 翻译成 `setMouseSelecting` Effect 派发给 view。每个编辑器创建后都调一次它（如 [demo/main.ts:356](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L356)）。这一步是 u1-l4 强调过的「必备接线」，自定义插件复用了 `mouseSelectingField`，自然也依赖它。

最后，主题里其实**已经内置了 `.cm-highlight` 样式**，可直接借用：

[src/theme/default.ts:140-144](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L140-L144) —— `backgroundColor: 'hsl(50 100% 50% / 0.4)'` 给的是半透明黄色背景。不过本讲范例用的是 replace+Widget 渲染 `<mark>`，`<mark>` 有浏览器原生黄底样式，即便不写 CSS 也能看到效果。

#### 4.3.4 代码实践

**实践目标**：把本讲 §5 写好的 `highlightPlugin` 接进 demo 验证。

1. 实践目标：在 demo 第一个编辑器里看到 `==高亮==` 被替换成黄底 `<mark>`，光标进入时还原成源码。
2. 操作步骤：
   - 在 `src/plugins/highlight.ts` 写好插件（§5 给出完整代码）；
   - 在 `src/index.ts` 的 Plugins 层加 `export { highlightPlugin } from './plugins/highlight';`；
   - 在 `demo/main.ts` 顶部 import 列表（[demo/main.ts:8-24](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L8-L24)）加 `highlightPlugin`；
   - 在 `basicState` 的 `extensions`（[demo/main.ts:330-346](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L330-L346)）里加入 `highlightPlugin()`；
   - 进 `demo` 目录 `npm run dev`，浏览器打开页面。
3. 需要观察的现象：在第一个编辑器输入 `这是一段 ==重点== 文本`，离开光标时「重点」二字呈黄底高亮、`==` 不可见；把光标移进高亮区，`==重点==` 还原成可编辑源码。
4. 预期结果：拖拽划过高亮区时不闪烁（验证 mouseSelecting 接线生效）；代码块里 `` `a==b` `` 不被误高亮（验证 skipRanges 生效）。
5. 若本地不便运行 demo，可对照 §5 代码做源码阅读型验证，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果忘了在 `src/index.ts` 里导出 `highlightPlugin`，demo 里会发生什么？

> **参考答案**：demo 通过 Vite 别名把 `codemirror-live-markdown` 指向 `src/index.ts`（u1-l3）。`index.ts` 没 re-export `highlightPlugin`，demo 里的 `import { highlightPlugin } from 'codemirror-live-markdown'` 会拿到 `undefined`，浏览器控制台报错或编辑器初始化失败。导出是「文件存在」与「外部可用」之间的必经关卡。

**练习 2**：为什么把 `highlightPlugin()` 加进 `extensions` 后，还要确保该编辑器调过 `setupMouseSelecting`？

> **参考答案**：`highlightPlugin` 的 `build` 经由 `shouldShowSource` 间接读取 `mouseSelectingField`，而 `shouldShowSource` 用了 `state.field(mouseSelectingField, false)`（第二参数 `false` 表示字段不存在时降级返回 `false` 而非报错）。如果没接线，`mouseSelectingField` 永远是默认值 `false`，拖拽防闪逻辑形同虚设——拖拽时高亮区可能与源码态来回切换。接线是让 core 机制真正生效的前提。

---

## 5. 综合实践

把前三节的五步法、core 复用、导出验证串起来，下面实现本讲规格指定的完整任务：**把 Markdown 的 `==高亮==` 文本用 `Decoration.replace` 替换成 `<mark>` 元素，光标进入时显示源码，并复用 `shouldShowSource` 与 `checkUpdateAction`。**

### 5.1 设计决策回顾

按 §4.1 的五步法，本插件的决策是：行内 replace（不 block）→ ViewPlugin；`==` 不在 Lezer 语法树 → 正则全文扫描（仿 [link.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts) 的 Wiki 链接路径）；复用 `shouldShowSource` + `checkUpdateAction` + skipRanges。正则 `/==([^=\n]+)==/g` 直接取自设计文档的范例：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:1571](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L1571) —— 设计文档用 `const highlightRegex = /==([^=\n]+)==/g;` 扫描，捕获组为高亮内容。

### 5.2 完整插件代码（示例代码）

下面是从零编写的 `highlightPlugin`，刻意模仿 `linkPlugin` 的结构（工厂函数 + 独立 build + skipRanges + 双模式）。**这是示例代码，不是项目原有文件**，需你自行创建 `src/plugins/highlight.ts`：

```typescript
// 示例代码：src/plugins/highlight.ts
// 一个 ==高亮== 的 Live Preview 插件，复用本库 core 机制

import { syntaxTree } from '@codemirror/language';
import { Range } from '@codemirror/state';
import {
  Decoration,
  DecorationSet,
  EditorView,
  ViewPlugin,
  ViewUpdate,
  WidgetType,
} from '@codemirror/view';
// 复用 core：从本库导入（demo 经 Vite 别名指向 src/index.ts）
import {
  shouldShowSource,
  mouseSelectingField,
  checkUpdateAction,
} from 'codemirror-live-markdown';

// ---- 第 3 步：正则（== 不在语法树，必须正则扫描）----
const HIGHLIGHT_REGEX = /==([^=\n]+)==/g;

// ---- 代码块跳过名单（仿 link.ts 的 SKIP_PARENT_TYPES）----
const SKIP_PARENT_TYPES = new Set(['FencedCode', 'CodeBlock', 'InlineCode']);

// ---- 第 5 步：WidgetType 子类（仿 LinkWidget）----
class HighlightWidget extends WidgetType {
  constructor(readonly text: string) {
    super();
  }
  // eq 比较的字段集合 = 决定 toDOM 输出的字段集合（u3-l1 心法）
  eq(other: HighlightWidget): boolean {
    return other.text === this.text;
  }
  toDOM(): HTMLElement {
    const mark = document.createElement('mark'); // <mark> 自带浏览器黄底样式
    mark.textContent = this.text;
    mark.className = 'cm-highlight-widget';
    return mark;
  }
  // 返回 false：放行点击，让 CodeMirror 把光标定位回源码区间进入编辑
  ignoreEvent(): boolean {
    return false;
  }
}

// ---- build：识别 + 决策 + 产出装饰（仿 buildLinkDecorations）----
function buildHighlightDecorations(view: EditorView): DecorationSet {
  const decorations: Range<Decoration>[] = [];
  const state = view.state;
  const isDrag = state.field(mouseSelectingField, false);

  // 1) 收集 skipRanges（代码块区间），仿 link.ts:107-114
  const skipRanges: Array<{ from: number; to: number }> = [];
  syntaxTree(state).iterate({
    enter: (node) => {
      if (SKIP_PARENT_TYPES.has(node.name)) {
        skipRanges.push({ from: node.from, to: node.to });
      }
    },
  });
  const isInSkipRange = (from: number, to: number) =>
    skipRanges.some((r) => from >= r.from && to <= r.to);

  // 2) 正则全文扫描（仿 link.ts Wiki 链接路径，必须重置 lastIndex）
  const docText = state.doc.toString();
  HIGHLIGHT_REGEX.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = HIGHLIGHT_REGEX.exec(docText)) !== null) {
    const from = match.index;
    const to = from + match[0].length;

    if (isInSkipRange(from, to)) continue; // 跳过代码块

    // 第 4 步：复用 shouldShowSource，一次拿到「开关+拖拽+相交」
    const isTouched = shouldShowSource(state, from, to);

    if (!isTouched && !isDrag) {
      // 渲染态：replace + Widget，把 ==x== 整段换成 <mark>
      decorations.push(
        Decoration.replace({ widget: new HighlightWidget(match[1]) }).range(from, to)
      );
    } else {
      // 源码态：mark 加提示类，原文 ==x== 可见可编辑
      decorations.push(
        Decoration.mark({ class: 'cm-highlight-source' }).range(from, to)
      );
    }
  }

  return Decoration.set(decorations.sort((a, b) => a.from - b.from), true);
}

// ---- 工厂函数 + ViewPlugin（仿 linkPlugin，update 复用 checkUpdateAction）----
export function highlightPlugin() {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      constructor(view: EditorView) {
        this.decorations = buildHighlightDecorations(view);
      }
      update(update: ViewUpdate) {
        // 第 4 步：复用 checkUpdateAction，免费拿到 rebuild/skip/none
        if (checkUpdateAction(update) === 'rebuild') {
          this.decorations = buildHighlightDecorations(update.view);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}
```

### 5.3 操作步骤与验证

1. 创建 `src/plugins/highlight.ts`，粘贴上面代码。
2. 在 [src/index.ts:16-31](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L16-L31) 的 Plugins 层加 `export { highlightPlugin } from './plugins/highlight';`。
3. 按 §4.3.4 把 `highlightPlugin()` 接进 demo 第一个编辑器。
4. `cd demo && npm run dev`，浏览器输入 `==重点==` 观察黄底高亮；光标移入还原源码；拖拽不闪烁；`` `a==b` `` 不误高亮。

### 5.4 策略对比：replace+Widget vs 设计文档的 mark 方案

本讲按规格要求用 **replace+Widget**（整段 `==x==` 换成 `<mark>`）。但设计文档第 8 节给出的是另一种 **mark 方案**——保留文本、不给 replace，而是把内容与 `==` 标记分别加类：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:1593-1610](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L1593-L1610) —— 对内容 `textStart..textEnd` 加 `cm-highlight`（始终黄底），对两端 `==` 复用 `cm-formatting-inline` 动画类（光标进入时展开）。

两种方案的取舍（呼应 u7-l3 §4.3）：

| 维度 | replace+Widget（本讲） | mark 方案（设计文档） |
|------|------------------------|------------------------|
| 渲染态 DOM | 整段换成 `<mark>x</mark>`，`==` 消失 | 原文 `==x==` 全留，仅加类染色 |
| 显隐动画 | 无（replace 直接换） | `==` 用 `max-width` 平滑展开（与 `**` 一致） |
| 可编辑性 | 光标进入靠 `ignoreEvent:false` 定位回源码 | 文本始终在 DOM，光标直接进入 |
| 实现复杂度 | 需写 WidgetType（eq/toDOM） | 只 push 三条 mark，无 Widget |
| 与原生体验一致性 | 偏「公式」式（视觉突变） | 偏「加粗」式（平滑过渡，最一致） |

**结论**：若追求与 `**bold**` 完全一致的平滑体验，设计文档的 mark 方案更优（且 `cm-highlight` 样式主题已内置）；本讲选 replace+Widget 是为了演示 WidgetType 全套生命周期，并满足规格要求。体会「同一需求、两种策略、各有取舍」正是本讲的终极目标。

> 想进一步优化？设计文档还用了一个 **StateField + 位置缓存**的进阶技巧——只在选区「触及/离开」高亮区时才重建，避免每次光标移动全篇重算（[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:1542-1559](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L1542-L1559)）。这是在 `checkUpdateAction` 之上的更细粒度优化，可作为后续练习。

## 6. 本讲小结

- **Live Preview 插件 = 产出 `DecorationSet` 的扩展**，开发遵循**五步法**：需求分析（选策略）→ 选载体（ViewPlugin/StateField）→ 识别语法（语法树/正则）→ 复用 core → 产出装饰。`livePreviewPlugin` 是最简模板，`linkPlugin` 是最全模板。
- **第 2 步有硬规则**：块级装饰（`block:true`）必须用 StateField；行内用 ViewPlugin 并可复用 `checkUpdateAction`。
- **第 3 步看语法树认不认**：Lezer 认识的（`EmphasisMark`、`Link`）用 `syntaxTree.iterate` 按 `node.name` 找；不认识的（`==高亮==`、`[[wiki]]`）用带 `g` 标志的正则全文扫描，且每次扫描前**重置 `lastIndex`**。
- **复用 core 三件套**：`shouldShowSource` 一次调用免费拿到「开关 + 拖拽防闪 + 光标相交」，`checkUpdateAction` 一行 `if` 免费拿到「只在必要时重建」，`mouseSelectingField` 读取拖拽态做第二层防护。`linkPlugin` 手写等价逻辑是反面教材，新插件应直接复用。
- **skipRanges 是正则扫描插件的标配**：先收集 `FencedCode`/`CodeBlock`/`InlineCode` 区间，再用「完全包含」判定排除，避免代码块内的 `==` 被误处理。
- **导出与验证缺一不可**：插件必须在 `src/index.ts` re-export 才能被导入；接进 demo 的 `extensions` 后还须确保该编辑器接线了 `mouseSelectingField` 与 `collapseOnSelectionFacet.of(true)`，core 机制才会真正生效。

## 7. 下一步学习建议

- **恭喜读完整套手册**。若想把你写的 `highlightPlugin` 变成可交付的工程产物，回到 **u7-l2 测试策略**，仿照 `livePreview.test.ts` 为它补一个 Vitest + jsdom 测试：构造一个含 `==x==` 的 `EditorView`，捕获装饰断言渲染态是 `replace`、把光标移入后断言切到 `cm-highlight-source` mark——把本讲的代码实践升级成可回归的自动化测试。
- **进阶练习**：把本讲的 replace+Widget 版改写成设计文档的 **mark 方案**（保留文本、`cm-formatting-inline` 动画），对比两者在光标进出时的动画观感；再进一步尝试 StateField + 位置缓存（[设计文档 L1542-1559](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L1542-L1559)）做细粒度重建优化。
- **体系化阅读**：把 `livePreviewPlugin`（mark+CSS）、`mathPlugin`（replace+Widget 行内）、`blockMathField`（replace+Widget 块级 StateField）、`linkPlugin`（正则 + skipRanges）四者并排重读，它们就是本讲五步法的四种填法范本，能复现本库几乎所有插件的写法。
