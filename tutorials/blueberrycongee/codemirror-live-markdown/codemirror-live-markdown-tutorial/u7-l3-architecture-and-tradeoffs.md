# 整体架构与设计取舍

> 本讲是「主题、测试、架构与二次开发」单元的第三篇，也是整套手册的架构收尾篇。它不再引入新模块，而是把前面所有讲义讲过的零件拼回一台完整的机器，回答三个问题：**数据从输入到画面怎么流动？为什么这样切分？性能靠什么撑住？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出整个库从「用户动作」到「DOM 画面」的完整单向数据流，并指出每一层由谁负责。
2. 说清 `reading` / `live` / `source` 三种视图模式在真实代码里是**靠扩展组合切换**的，而不是一个内置开关；并解释为什么「切到 source 模式」必须**移除 `livePreviewPlugin`** 而不能只把 Facet 关掉。
3. 在 `mark + CSS` 与 `replace + Widget` 两种装饰策略之间做出正确取舍，并说明取舍依据。
4. 总结 `ViewPlugin` / `StateField` / `WidgetType` / `Facet` 四种 CodeMirror 概念在本库里各自扮演的架构角色。
5. 列举本库的三类性能优化手段（拖拽跳过重建、双层缓存、选择性重建）及其代价。

## 2. 前置知识

本讲默认你已读完前面六单元的核心讲义。下面三句话帮你快速回忆，正式讲解中不再重复细节：

- **Live Preview 的本质**：底层存储的始终是 Markdown 纯文本（`**bold**`），但通过 CodeMirror 的「装饰（Decoration）」机制控制哪些字符显示、哪些隐藏，让非活动文本呈现渲染效果，光标进入时再显出原始标记可编辑（参见 u1-l1、u2-l1）。
- **决策心脏 `shouldShowSource(state, from, to)`**：一个纯函数，三步短路判定某段区间此刻该显示源码（`true`）还是渲染结果（`false`）（参见 u2-l2）。
- **两种装饰策略**：`Decoration.mark` 给字符加 CSS 类做显隐动画；`Decoration.replace` 用 `WidgetType` 把原文替换成自定义 DOM（参见 u3-l1）。

一个关键认知：本仓库里有一份 `CODEMIRROR_LIVE_PREVIEW_DESIGN.md` 设计文档，它描述的是一个**理想化的完整蓝图**（含 mermaid、callout、reading 模式插件等），而真正发布在 `src/index.ts` 里的是它的一个**模块化子集**。本讲会反复对比「文档怎么画的」与「代码怎么做的」，这种差异本身就是架构取舍的一部分。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [CODEMIRROR_LIVE_PREVIEW_DESIGN.md](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md) | 设计蓝图：三视图模式、数据流图、性能优化思路、已知限制 |
| [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) | 库的真实入口：按 Core / Plugins / Theme / Utils 四类 re-export，是「实际有哪些零件」的权威清单 |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | 决策心脏：决定显示源码还是渲染结果 |
| [src/core/pluginUpdateHelper.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts) | `checkUpdateAction`：把更新归纳成 rebuild/skip/none 三动作 |
| [src/core/facets.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts) | `collapseOnSelectionFacet`：Live Preview 总开关 |
| [src/core/mouseSelecting.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts) | 拖拽状态追踪：`mouseSelectingField` + `setMouseSelecting` |
| [src/utils/mathCache.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts) | KaTeX 渲染结果缓存 |
| [src/utils/imageLoader.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts) | 图片异步加载、双缓存、路径安全 |
| [src/plugins/livePreview.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts) | mark+CSS 策略的代表：标记显隐动画 |
| [src/widgets/mathWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts) | replace+Widget 策略的代表：公式 DOM 渲染 |
| [demo/main.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts) | 演示页：用 Compartment 真实切换 Live / Source 模式 |

## 4. 核心概念与源码讲解

### 4.1 三种视图模式与状态切换

#### 4.1.1 概念说明

设计文档把视图抽象成三种模式：

| 模式 | 标记显示 | 可编辑 | 典型场景 |
|------|----------|--------|----------|
| `reading` | 全部隐藏 | 否 | 只读阅读 |
| `live` | 活动行显示、其余隐藏 | 是 | 日常写作 |
| `source` | 全部显示 | 是 | 调试 / 排版微调 |

设计文档把它写成 `type ViewMode = 'reading' | 'live' | 'source'`，很容易让人误以为库里有一个「mode 字段」或者一个 `readingModePlugin`。但翻看真实导出你会发现：**`src/index.ts` 里根本没有 `readingModePlugin`，也没有一个 mode 字段**。三种模式是靠**不同的扩展组合**拼出来的，切换靠 `Compartment.reconfigure` 在运行时换扩展。这是「灵活集成、简单 API」这条设计原则的直接体现——库不替你决定模式语义，只提供零件。

#### 4.1.2 核心流程

真实的模式切换流程（以 demo 为准）：

```
Live 模式扩展包：
  collapseOnSelectionFacet.of(true)   ← 总开关打开
  + mouseSelectingField                ← 拖拽状态
  + livePreviewPlugin                  ← 标记显隐（mark+CSS）
  + markdownStylePlugin                ← 内容样式
  + (mathPlugin / blockMathField / tableField / imageField / linkPlugin ...)
        │
        │  点 "Source" 按钮
        ▼  Compartment.reconfigure([...])
Source 模式扩展包：
  collapseOnSelectionFacet.of(false)   ← 总开关关闭
  + markdownStylePlugin                ← 仅保留内容样式
  （livePreviewPlugin 与各渲染插件被整体移除）
```

关键点：**source 模式不仅把 Facet 关掉，还把 `livePreviewPlugin` 一起移除了**。下一小节的源码精读会解释为什么这两步缺一不可。

#### 4.1.3 源码精读

设计文档先给出三种模式的概念定义：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:98-109](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L98-L109) —— 定义 `ViewMode` 类型与三模式对照表，是理解整体目标的起点。

真正落地的切换逻辑在 demo 里。Live 模式按钮重新装回完整扩展包：

[demo/main.ts:452-474](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L452-L474) —— `basicModeCompartment.reconfigure([...])` 装回 `livePreviewPlugin` 等全部扩展，并把 Facet 设为 `true`。

Source 模式按钮则是关键的反例：

[demo/main.ts:476-485](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L476-L485) —— 注意它**同时做了两件事**：`collapseOnSelectionFacet.of(false)` 且**不再包含 `livePreviewPlugin`**，只剩 `markdownStylePlugin`。

为什么不能只关 Facet？看决策函数的第一步：

[src/core/shouldShowSource.ts:31-36](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L31-L36) —— Facet 为 `false` 时函数**直接返回 `false`**（含义：显示渲染结果、隐藏标记）。

再看 `livePreviewPlugin` 如何消费这个返回值：

[src/plugins/livePreview.ts:128-136](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L128-L136) —— `isTouched` 为 `false` 时，标记只拿到 `cm-formatting-inline`（隐藏态 CSS 类）。

把这两段连起来就得到一个反直觉的结论：**如果只把 Facet 关成 `false`、却保留 `livePreviewPlugin`，那么所有标记的 `shouldShowSource` 都返回 `false`，于是所有标记都被套上隐藏态 CSS 类——结果是「全部标记被隐藏」的 reading 效果，而不是「全部标记可见」的 source 效果。** 要真正显示全部标记，必须**移除 `livePreviewPlugin`**，让 CodeMirror 回退到「没有任何装饰、原文照常显示」的默认行为。这正是 demo 同时做两步的原因，也是「模式 = 扩展组合」这一设计取舍最精妙的地方。

> 设计文档 L270-272 设想了一个独立的 `readingModePlugin`，但真实实现没有它——reading 效果是「Facet false + livePreviewPlugin 仍在」的副产品，外加 `EditorState.readOnly` 即可。这是蓝图与实现的典型差异。

#### 4.1.4 代码实践

**实践目标**：亲手验证「关 Facet」与「移除 livePreviewPlugin」的区别。

1. 操作步骤：进入 `demo` 目录运行 `npm run dev`，在浏览器打开页面，定位到第一个编辑器（`editor-basic`）上方的 Live / Source 按钮。
2. 想象一个对照实验（可在本地修改 demo 验证）：把 Source 按钮的 `reconfigure` 改成**只**保留 `collapseOnSelectionFacet.of(false)` 与 `livePreviewPlugin`（不删 livePreviewPlugin），其余不变。
3. 需要观察的现象：切换到「Source」后，`**`、`#`、`>` 等标记**仍然不可见**（呈现 reading 效果），而不是预期的「全部标记可见」。
4. 预期结果：这验证了上面的结论——source 模式必须移除 `livePreviewPlugin`。
5. 如不便修改 demo，可纯阅读分析得出同样结论，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果要新增一个「reading 模式」按钮（只读、全部标记隐藏、但数学公式和表格仍渲染），该装哪些扩展、移除哪些？

> **参考答案**：装 `collapseOnSelectionFacet.of(true)`（或 false，因为不编辑则光标不触发源码态）+ `markdownStylePlugin` + `blockMathField` + `tableField` + `imageField` + `linkPlugin` + `EditorState.readOnly.of(true)`；保留 `livePreviewPlugin`（让标记隐藏）；无需 `mouseSelectingField`（只读不拖选）。本质上 reading = live 的渲染插件 + 只读。

**练习 2**：为什么 `source` 模式下 `markdownStylePlugin` 仍然保留？

> **参考答案**：`markdownStylePlugin` 只给内容节点加**静态样式类**（如 `cm-header-1`），与光标位置无关，它让源码态文字也带上字号、字重等基本排版；移除它会让 source 模式退化成完全无样式的纯文本。

---

### 4.2 数据流与四种架构角色

#### 4.2.1 概念说明

整个库是一条**严格单向**的数据流，和 CodeMirror 6 自身的不可变状态模型完全对齐：用户动作产生 Transaction，Transaction 算出新的 EditorState，状态变化驱动插件重算装饰，装饰最终落到 DOM。本库的每一个零件都嵌在这条流水线的某个固定工位上，工位由 CodeMirror 的四种核心概念划分。

#### 4.2.2 核心流程

设计文档给出的数据流（与本库实现一致）：

```
用户操作（输入 / 选择 / 点击 / 拖拽）
         │
         ▼
   EditorView          ← 接收 DOM 事件
         │  dispatch({ changes, selection, effects })
         ▼
   Transaction         ← 三通道：changes / selection / effects
         │
         ▼
   EditorState         ← 不可变的新状态快照
         │
         ├──────────────────────┐
         ▼                      ▼
   StateField.update()    ViewPlugin.update()
         │                      │
         ▼                      ▼
      重算 DecorationSet（其中：shouldShowSource 决定每段显示哪一面）
         │
         ▼
      DOM 更新（应用 mark 类 / 插入替换 Widget）
```

这条流水线上有四种角色，分工严格：

| 角色 | 在本库的职责 | 代表实例 | 值归属 |
|------|--------------|----------|--------|
| **Facet** | 全局配置开关（多输入 combine） | `collapseOnSelectionFacet` | state |
| **StateField** | 跨事务状态 **+ 块级装饰** | `mouseSelectingField`、`blockMathField`、`tableField`、`imageField`、`codeBlockField` | state 快照 |
| **ViewPlugin** | 行内装饰 **+ 视口相关逻辑** | `livePreviewPlugin`、`markdownStylePlugin`、`mathPlugin`、`linkPlugin` | view |
| **WidgetType** | 把原文替换成自定义 DOM | `MathWidget`、`TableWidget`、`CodeBlockWidget`、`ImageWidget`、`LinkWidget` | 由 replace 装饰驱动 |

而 `shouldShowSource` 不属于上面任何一类——它是嵌在「重算 DecorationSet」这一步里的**纯决策函数**，被 StateField 与 ViewPlugin 共同调用，决定每段区间走渲染态还是源码态。它是流水线上的「分拣阀」。

#### 4.2.3 源码精读

真实导出清单就是这条流水线的零件目录。`src/index.ts` 按 Core / Plugins / Theme / Utils 四类组织，恰好对应「决策与状态 / 装饰编排 / 表现 / 纯工具」四层：

[src/index.ts:9-14](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L9-L14) —— Core 层导出 `collapseOnSelectionFacet`（Facet）、`mouseSelectingField`/`setMouseSelecting`（StateField+Effect）、`shouldShowSource`（决策函数）、`checkUpdateAction`（更新助手）。

[src/index.ts:16-31](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L16-L31) —— Plugins 层混用了两类：带 `Plugin` 后缀的是 ViewPlugin（`livePreviewPlugin`、`markdownStylePlugin`、`mathPlugin`、`linkPlugin`），带 `Field` 后缀的是 StateField（`blockMathField`、`tableField`、`imageField`、`codeBlockField`）。命名本身就是架构线索。

决策函数 `shouldShowSource` 三步短路，是「分拣阀」的具体实现：

[src/core/shouldShowSource.ts:31-53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L31-L53) —— 第一步读 Facet（L33-36）、第二步读拖拽 StateField（L39-42）、第三步遍历选区做相交判定（L45-50）。注意它同时**读取了 Facet 和 StateField 两种状态源**，把「全局开关」与「瞬时拖拽」统一收口到一个布尔返回值，这是它作为单一事实来源的关键。

数据流的源头——用户拖拽如何变成 Transaction 的 effects 通道——在 demo 的接线里：

[demo/main.ts:309-319](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L309-L319) —— `mousedown` 派发 `setMouseSelecting.of(true)`，`mouseup`（套 `requestAnimationFrame`）派发 `of(false)`。这就是把 DOM 事件翻译进 Transaction 的 effects 通道、再被 StateField 接住的完整路径。

设计文档的同款数据流图可作为对照：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:291-327](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L291-L327) —— 文档版的 User → View → Transaction → State → StateField/ViewPlugin → DOM 流水线，与本库实现一致。

#### 4.2.4 代码实践

**实践目标**：跟踪一次按键穿越整条流水线。

1. 操作步骤：在第一个 demo 编辑器里，把光标移到 `**bold**` 的 `bold` 中间再移出。
2. 跟踪链路：
   - 光标移动 → CodeMirror 产生带 `selection` 的 Transaction；
   - `livePreviewPlugin.update` 被调用，`checkUpdateAction` 判定为 `rebuild`；
   - `build` 遍历语法树，对 `**` 两个 `EmphasisMark` 调 `shouldShowSource`；
   - 光标在 `bold` 中时返回 `true` → 加 `-visible` 类（标记展开）；移出后返回 `false` → 去掉 `-visible` 类（标记收起）。
3. 需要观察的现象：`**` 平滑展开/收起，文字始终留在原位可编辑。
4. 预期结果：整条链路中**没有任何一处改变文档文本**，`doc.toString()` 始终是 `**bold**`——所有变化只发生在装饰层与 DOM 层。这正是 Live Preview「底层纯文本不变」的核心承诺。

#### 4.2.5 小练习与答案

**练习 1**：`mouseSelectingField` 为什么是 StateField 而不是 Facet？

> **参考答案**：Facet 是「配置」（combine 多输入），通常静态；而拖拽状态是**随时间变化的运行时状态**，需要在 `mousedown`/`mouseup` 之间被反复改写、且要进入 EditorState 快照供 `shouldShowSource` 读取。StateField + StateEffect 正是为「跨事务、可被 effects 改写的状态」设计的。

**练习 2**：`shouldShowSource` 是 StateField、ViewPlugin、WidgetType 还是 Facet？为什么？

> **参考答案**：都不是，它是一个**普通纯函数**，既不持有状态也不产出装饰。它被多个 StateField 与 ViewPlugin 调用，把「Facet 开关 + 拖拽状态 + 选区」三种输入压成一个布尔决策，扮演流水线上的分拣阀，是「显示哪一面」的单一事实来源。

---

### 4.3 两种装饰策略取舍：mark+CSS 与 replace+Widget

#### 4.3.1 概念说明

CodeMirror 的 Decoration 有四种，本库真正形成对照的是两种**渲染策略**：

- **mark + CSS**：用 `Decoration.mark` 给一段字符加 CSS 类，文本本身**原样留在 DOM 里**，靠 CSS（`max-width`/`font-size` 过渡）控制显隐。代表：`livePreviewPlugin`、`markdownStylePlugin`。
- **replace + Widget**：用 `Decoration.replace` 把一段原文**隐藏**，替换成 `WidgetType.toDOM` 生成的全新 DOM。代表：`mathPlugin`、`tableField`、`imageField`、`codeBlockField`。

#### 4.3.2 核心流程

选择策略的决策树：

```
面对一段 Markdown 元素
        │
        ├── 渲染结果与原文「同字符、只是显隐」？
        │     例：**bold** 里的 ** —— 隐藏时是空、显示时是 **
        │     → 选 mark + CSS（文本留在 DOM，动画切换可见性，可平滑编辑）
        │
        └── 渲染结果与原文「完全不同的视觉」？
              例：$E=mc^2$ → KaTeX 排版；| a | b | → HTML 表格
              → 选 replace + Widget（隐藏原文，插入自定义 DOM）
```

两条附加约束：

1. **可编辑性**：mark+CSS 下文本始终在 DOM，光标可直接进入编辑；replace+Widget 下原文被隐藏，需要靠点击 `ignoreEvent`/`posAtDOM` 把光标「定位回」源码区间才能编辑。
2. **块级强制 StateField**：带 `block: true` 的 replace 装饰会改变行高，必须属于 EditorState 才能参与视口测量，因此**块级 replace 必走 StateField**（`blockMathField`、`tableField`、`imageField`），不能用 ViewPlugin。

#### 4.3.3 源码精读

mark+CSS 的代表——`livePreviewPlugin` 对行内标记的处理：

[src/plugins/livePreview.ts:124-137](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L124-L137) —— 对每个标记节点只 push 一条 `Decoration.mark`，类名在 `cm-formatting-inline` 与 `cm-formatting-inline-visible` 之间切换。文本的 `from`/`to` 区间**从未被替换**，`**` 两个字符始终物理存在于编辑器 DOM 中，只是被 CSS 收起。

设计文档解释了为什么用 `max-width` 而非 `display:none`：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:518-525](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L518-L525) —— 对照表说明 `display:none` 不可过渡、切换会跳动；`max-width:0` 可动画、自适应、是行内标记的最佳方案；块级标记因不能改行高而用 `font-size:0.01em`。

replace+Widget 的代表——`MathWidget` 把公式渲染成全新 DOM：

[src/widgets/mathWidget.ts:32-53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L32-L53) —— `eq` 仅比较 `source` 与 `isBlock`（L32-34）；`toDOM` 调 `renderMath` 把 KaTeX 渲染结果塞进 `innerHTML`（L43-45）。这里**原文 `$E=mc^2$` 已被 replace 隐藏**，DOM 里只剩 KaTeX 排版的 span 树，两者视觉完全不同，这正是必须用 replace 的原因。

`eq` 的取舍心法：`eq` 比较的字段集合 = 决定 `toDOM` 输出的字段集合。`MathWidget` 的 `toDOM` 只依赖 `source` 与 `isBlock`，故 `eq` 也只比这两项——`eq` 返回 `true` 时 CodeMirror 复用旧 DOM，跳过昂贵的 `toDOM`（即跳过 KaTeX 重算）。多比会漏更新、少比会白重建。

#### 4.3.4 代码实践

**实践目标**：判断一个新元素该用哪种策略。

1. 操作步骤：假设要为新语法 `==高亮==`（设计文档 L1514 有示例）选策略。
2. 分析：渲染结果是一个带黄色背景的 `<mark>`，与原文 `==高亮==` 视觉不同 → 倾向 replace+Widget。但设计文档 L1594-1610 选择的是**混合**——对内容 `高亮` 用 `Decoration.mark({class:'cm-highlight'})`，对两端 `==` 标记复用 `cm-formatting-inline` 动画类。
3. 需要观察的现象：内容部分走 mark（保留文本、加背景）、标记部分走 mark+CSS 动画（显隐 `==`），整段都不用 replace。
4. 预期结果：体会「策略不是非此即彼，可以按子区间分别决策」——这正是本库把 `**bold**` 拆成 `EmphasisMark`（标记）+ `StrongEmphasis`（内容）分别处理的原因。
5. **待本地验证**：可在本地按 u7-l4 的指导实现一个 `==高亮==` 插件观察效果。

#### 4.3.5 小练习与答案

**练习 1**：图片 `![alt](url)` 为什么用 replace+Widget 而非 mark+CSS？

> **参考答案**：图片的渲染结果是一个真实的 `<img>` DOM（还要异步加载、有 loading/error 状态机），与原文 `![alt](url)` 在字符层面毫无对应关系。mark+CSS 只能给已有字符加样式，造不出 `<img>`，所以必须用 replace 把原文隐藏、换成 `ImageWidget.toDOM` 产出的图片元素。

**练习 2**：`markdownStylePlugin` 给标题加 `cm-header-1` 为什么不需要 `shouldShowSource`？

> **参考答案**：它做的是**与光标无关的静态外观**（字号、字重），无论光标在不在标题行，标题都该是大字粗体。它的 `update` 只在 `docChanged`/`viewportChanged` 时重建，不响应选区变化，所以根本不需要决策函数。

---

### 4.4 性能优化手段

#### 4.4.1 概念说明

Live Preview 比普通编辑器更吃性能：每次光标移动都可能触发「整篇文档重算装饰」。本库用三类手段把开销压下去，对应三条思路：**少算（拖拽时跳过）、算了别再算（缓存）、没必要就别算（选择性重建）**。

#### 4.4.2 核心流程

三类优化与各自代价：

| 手段 | 思路 | 代价 / 取舍 |
|------|------|-------------|
| 拖拽跳过重建 | 拖选期间所有插件冻结装饰 | 拖选时画面「冻结在拖拽前」，结束后补建一次 |
| 双层缓存 | KaTeX 结果 / 图片结果缓存，`eq` 挡 `toDOM` | 占内存；失败结果不缓存以允许重试 |
| 选择性重建 | `checkUpdateAction` 只在必要时 rebuild | 需正确枚举所有「该重建」的情形，否则漏更新 |

#### 4.4.3 源码精读

**(1) 拖拽跳过重建——双层防护。**

第一层在决策函数里：

[src/core/shouldShowSource.ts:39-42](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L39-L42) —— 拖拽中 `shouldShowSource` 一律返回 `false`（显示渲染态），让所有元素退回稳定的渲染态。

第二层在更新助手 `checkUpdateAction`：

[src/core/pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65) —— 这是性能优化的中枢。它把更新归成三动作：结构性变化（`docChanged`/`viewportChanged`/`reconfigured`）→ `rebuild`（L37-43）；拖拽**刚结束** → `rebuild` 补一次（L50-52）；拖拽**进行中** → `skip` 完全跳过（L55-57）；选区变 → `rebuild`（L60-62）；其余 → `none`（L64）。

`livePreviewPlugin` 直接复用它，`update` 只有一行核心逻辑：

[src/plugins/livePreview.ts:55-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L55-L59) —— 只有 `checkUpdateAction` 返回 `rebuild` 才重算装饰；`skip` 与 `none` 都直接跳过，不重建。拖拽期间成百上千次的鼠标移动只命中 `skip`，零重建开销。

**(2) 双层缓存——KaTeX 与图片。**

KaTeX 渲染结果缓存，键区分行内/块级，且只缓存成功结果：

[src/utils/mathCache.ts:26-55](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L26-L55) —— 键为 `'inline:源码'` 或 `'block:源码'`（L27），命中直接返回（L29-31），否则渲染并写缓存（L49）。注意失败时返回错误 span（L53）而**不写缓存**——这样下次能重试，是偏向正确性的取舍。它与 `MathWidget.eq` 构成两级闸门：`eq` 挡住 `toDOM` 执行，`mathCache` 在 widget 重建时挡住 KaTeX 重计算。

图片的双缓存（结果缓存 + 在途 Promise 去重）：

[src/utils/imageLoader.ts:34-37](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L34-L37) —— 两个模块级 `Map`：`imageCache` 长期存成功结果，`loadingPromises` 短期做在途请求去重。

[src/utils/imageLoader.ts:88-97](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L88-L97) —— 先查结果缓存（L88-91），再查在途 Promise（L94-97），都没有才发新请求。同一张图被多个 widget 同时加载时只发一次网络请求。

[src/utils/imageLoader.ts:125](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L125) 与 [src/utils/imageLoader.ts:142-143](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L142-L143) —— 成功才写 `imageCache`（L125），失败**不写**（L142-143 注释明说「allow retry」），与 KaTeX 缓存的取舍一致。

**(3) 选择性重建。** 这正是 `checkUpdateAction` 返回 `none` 的语义——当选区没变、文档没变、没在拖拽时，插件**什么都不做**，复用旧 DecorationSet。这一项的代价是开发者必须正确枚举所有「该重建」的情形（见小练习）。

设计文档把性能优化列为正式章节，可作体系化参考：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:1190-1404](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L1190-L1404) —— 含位置缓存、KaTeX 预渲染队列、拖拽优化、视口优化。注意其中「KaTeX 预渲染队列 / `requestIdleCallback`」在真实 `mathCache.ts` 里并未实现（真实实现是更简单的同步缓存），这是又一处蓝图与实现的差异。

#### 4.4.4 代码实践

**实践目标**：观察拖拽优化与缓存命中的实际效果。

1. 操作步骤：在含多张图片与多个公式的 `longDoc` 编辑器里，用鼠标**按住拖动**划过一大段文本。
2. 需要观察的现象：拖动过程中画面应保持稳定、不闪烁（因为 `checkUpdateAction` 返回 `skip`，所有插件冻结）；松手后画面才一次性更新（拖拽结束补建一次）。
3. 操作步骤（缓存）：把同一个公式 `$E=mc^2$` 在文档里写两遍，打开浏览器开发者工具。若能给 `renderMath` 临时加一行 `console.log(source)`，会看到两个相同公式只触发一次真实 KaTeX 渲染（第二次命中缓存）。
4. 预期结果：拖拽稳定无闪烁；重复公式只渲染一次。
5. 修改源码加日志属于示例操作，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果 `checkUpdateAction` 里删掉「拖拽进行中返回 `skip`」这一分支（L55-57），会出现什么问题？

> **参考答案**：拖拽时鼠标每移动一格都会产生选区变化的 Transaction，命中 L60-62 的 `selectionSet → rebuild`，于是每次移动都全篇重算装饰并重渲染 Widget，导致严重卡顿与画面闪烁。这正是该分支存在的理由。

**练习 2**：为什么 KaTeX 与图片的失败结果都**不缓存**？

> **参考答案**：失败往往是暂时的（网络抖动、KaTeX 暂时不可用）。若缓存失败结果，用户重试或网络恢复后仍会拿到旧的失败展示，无法自愈。牺牲一点重复计算换取「可重试」的正确性，是偏向正确性的取舍。

**练习 3**：`checkUpdateAction` 现有四类「该重建」的情形（结构性变化、拖拽结束、拖拽中跳过、选区变化）。如果未来新增一个「随主题色变化而重算」的需求，该加在哪一类？

> **参考答案**：主题色变化通常通过 Compartment `reconfigure` 触发，会被 `update.transactions.some(t => t.reconfigured)` 捕获（L40），归入第一类「结构性变化 → rebuild」。无需新增分支，现有判定已覆盖。

## 5. 综合实践

设计文档第 12 节列出了本库的已知限制：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:2084-2092](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L2084-L2092) —— 嵌套标记、大文件性能、复杂表格、协作编辑、移动端、IME 输入等限制。

请结合本讲与已知限制，完成下面的写作任务（这是本讲义规格指定的实践）：

> 写一篇 **300 字左右**的小结，回答两个问题：
>
> 1. **如果用一句话概括这个库的架构核心，你会怎么说？**（提示：可以从「纯文本不变 + 装饰决定显示 + `shouldShowSource` 统一决策」三个角度提炼）。
> 2. **指出 `shouldShowSource` 在整个架构中扮演的角色**，并结合已知限制说明它的边界——例如它只判定「光标相交」，对「嵌套标记」「IME 输入闪烁」等限制无能为力，为什么？

写作要求：必须引用至少一处真实源码位置（永久链接）作为论据；不要泛泛而谈，要落到具体函数或数据流节点。

**参考思路（不是唯一答案）**：一句话概括可以是「**底层始终是纯文本，靠 `shouldShowSource` 这一纯函数把 Facet 开关、拖拽状态与选区相交压成一个布尔决策，再由 ViewPlugin/StateField 据此在 mark+CSS 与 replace+Widget 两种装饰间分拣，最终驱动 DOM**」。`shouldShowSource` 扮演的是流水线上的**单一分拣阀**——它把「显示哪一面」收口为一处，是整个 Live Preview 的决策心脏；但它的判定仅基于「光标/选区是否与区间相交」，对嵌套标记的语法树重叠、IME 组合输入的中间态都无感知，因此这些场景成为已知限制，需要额外的语法层或事件层方案弥补。

## 6. 本讲小结

- **数据流是严格单向的**：用户动作 → Transaction（changes/selection/effects 三通道）→ 不可变 EditorState → StateField/ViewPlugin 重算装饰 → DOM。本库所有零件都嵌在这条流水线的固定工位。
- **三种视图模式靠扩展组合切换**，不是内置字段。`reading`/`live`/`source` 由 `Compartment.reconfigure` 换扩展包实现；切 source 模式**必须移除 `livePreviewPlugin`**，只关 Facet 会得到 reading 效果——这是「Facet false + livePreviewPlugin 仍在 ⇒ 全部标记隐藏」的推论。
- **四种架构角色分工严格**：Facet（配置开关）、StateField（跨事务状态 + 块级装饰）、ViewPlugin（行内装饰 + 视口逻辑）、WidgetType（replace 出自定义 DOM）；`shouldShowSource` 是被它们共享的纯决策函数，是「显示哪一面」的单一事实来源。
- **两种装饰策略各有适用场景**：同字符显隐用 mark+CSS（文本留 DOM、可平滑编辑、动画靠 `max-width`/`font-size`）；视觉完全不同用 replace+Widget（隐藏原文、插入新 DOM），块级 replace 强制走 StateField。
- **性能靠三类手段**：拖拽跳过重建（`checkUpdateAction` 返回 `skip` + `shouldShowSource` 拖拽短路，双层防护）、双层缓存（KaTeX/图片结果缓存 + `eq` 挡 `toDOM`，失败不缓存以允许重试）、选择性重建（仅在结构性变化/拖拽结束/选区变时 rebuild，否则 `none`）。
- **设计文档是蓝图、`src/index.ts` 是实现**：文档设想的 mermaid、callout、reading 模式插件、KaTeX 预渲染队列等，真实代码里是更精简的模块化子集。理解这种差异本身就是理解架构取舍。

## 7. 下一步学习建议

- 本讲是架构收尾，若你想把这些零件**亲手组装成一个新插件**，请接着学 **u7-l4 二次开发：编写自己的 Live Preview 插件**——它会带你复用 `shouldShowSource`、`checkUpdateAction`、`WidgetType` 实现一个 `==高亮==` 插件。
- 若想验证自己对架构的理解，建议重读 **u7-l2 测试策略**，尝试为某个插件补一个「拖拽期间不重建」的测试，把本讲的性能结论变成可执行的断言。
- 进阶阅读：对照设计文档的「后续优化方向」（[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:2113-2139](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L2113-L2139)），思考虚拟滚动、协作编辑（Yjs）会如何冲击现有的「单 StateField 全篇重算」模型——这是从读懂架构走向演进架构的练习。
