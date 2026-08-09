# CodeMirror 6 核心概念速览

## 1. 本讲目标

本讲是「Live Preview 核心机制」单元的入口。从上一单元你已经知道：这个库是一组 CodeMirror 6 扩展，靠「装饰（Decoration）」和决策函数 `shouldShowSource` 把 `**bold**` 这样的纯文本渲染成富文本效果。但要真正读懂后面几讲（`shouldShowSource`、`livePreviewPlugin`、`blockMathField` 等），你需要先建立一套 CodeMirror 6 的核心词汇表。

学完本讲，你应该能够：

1. 说出 `EditorState`（不可变状态）、`EditorView`（可变视图）、`Transaction`（事务）三者如何配合更新编辑器。
2. 区分 `ViewPlugin` 与 `StateField` 这两种「提供装饰」的方式，并解释为什么块级装饰必须用 `StateField`。
3. 掌握 `Decoration` 的四种类型（`mark` / `replace` / `widget` / `line`）以及它们在本项目里的真实用法。
4. 理解 `Facet`、`Compartment`、`syntaxTree` 各自承担的角色，为后续逐行精读源码扫清障碍。

本讲不追求覆盖 CodeMirror 6 的全部 API，只讲「读这个库的源码必须懂」的那部分概念。

## 2. 前置知识

在进入概念前，先用三句话对齐几个常被混淆的术语（更完整的背景见项目设计文档 [CODEMIRROR_LIVE_PREVIEW_DESIGN.md:50-92](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L50-L92)，这段解释了「为什么选 CodeMirror 6 而不是 ProseMirror/Tiptap」）：

- **纯文本模型**：CodeMirror 6 底层只存一份纯文本字符串。`**bold**` 就是 8 个字符，不会被拆成「语义节点」。这正是 Live Preview 的前提——源码永远在，只是被「装饰」藏起来或替换掉。
- **装饰（Decoration）**：一段「给文档某个区间附加样式 / 替换内容 / 插入 DOM」的描述。装饰不改变文本本身，只改变它**怎么显示**。
- **扩展（Extension）**：CodeMirror 6 的一切能力（语法高亮、快捷键、装饰、主题）都以扩展形式拼装。上一单元你拼过的那个 `extensions: [...]` 数组，就是这台机器的全部入口。

如果这几个词还陌生，建议先回去做一遍 [快速集成](./u1-l4-first-live-preview-editor.md) 那一讲的 demo，再回来。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 | 本讲用来演示什么 |
|------|------|------------------|
| [CODEMIRROR_LIVE_PREVIEW_DESIGN.md](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md) | 项目设计文档 | 给出 CodeMirror 6 核心概念的标准定义与示例 |
| [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) | 库的统一导出（桶文件） | 看各类扩展如何被分类导出 |
| [src/core/facets.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts) | `collapseOnSelectionFacet` | Facet 的真实定义 |
| [src/core/mouseSelecting.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts) | `mouseSelectingField` + `setMouseSelecting` | StateField + StateEffect 的真实定义 |
| [src/plugins/livePreview.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts) | 行内/块级标记动画插件 | ViewPlugin + `Decoration.mark` 的真实用法 |
| [src/plugins/math.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts) | 数学公式插件 | 同一文件里 ViewPlugin（行内）与 StateField（块级）的对照 |
| [src/plugins/markdownStyle.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts) | 内容节点样式插件 | `Decoration.mark` 与 `Decoration.line` 的真实用法 |
| [demo/main.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts) | demo 入口 | `EditorState.create` + `Compartment.reconfigure` 的真实接线 |

记住一条主线：**这些概念在本项目里都不是抽象理论，而是每个都能在一个具体源码文件里指出来**。本讲就是带你在源码里把它们一一认出来。

---

## 4. 核心概念与源码讲解

### 4.1 EditorState、EditorView 与 Transaction

#### 4.1.1 概念说明

CodeMirror 6 的状态模型可以用一句话概括：**状态是只读的，视图是可变的，二者之间用一个叫「事务」的东西单向流动。**

- `EditorState`：编辑器的**不可变状态快照**。它包含文档全文、选区、以及所有扩展的当前值。一旦创建就不能改——要「改」只能生成一个新的 State。
- `EditorView`：编辑器的**可变视图层**。它持有 DOM、监听用户事件、负责把某个 State 渲染出来。`view` 对象在编辑器生命周期里是同一个，但它内部的 state 会被不断替换。
- `Transaction`（事务）：对状态的一次「拟议变更」。它打包了「文本改动 + 选区改动 + 效果（effect）+ 配置重置」等信息。`view.dispatch(tr)` 会用这个事务算出一个新 State，再让视图切换过去。

为什么要设计成不可变？因为不可变状态让「撤销、协作、按需重算装饰」变得简单：每次更新都得到一份全新的、可对比的快照，而不是在原对象上偷偷改字段。

#### 4.1.2 核心流程

更新一次编辑器的数据流如下（与设计文档 [数据流图](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L291-L327) 一致）：

```
用户输入 / 点击 / 拖拽
        │
        ▼
   EditorView  ← 捕获 DOM 事件，组装成 Transaction
        │  view.dispatch({ changes, selection, effects })
        ▼
   Transaction  ← 描述「要怎么改」
        │
        ▼
   新 EditorState  ← 旧 state 应用 transaction 后的快照
        │
        ├─→ StateField.update(value, tr)
        └─→ ViewPlugin.update(update)
        │
        ▼
   重新计算 Decorations → 更新 DOM
```

关键点：`EditorView` 是**唯一**直接碰 DOM 的角色；所有业务逻辑（包括这个库的全部插件）都运行在「State / Transaction」这一层，最终通过装饰去影响 DOM。

#### 4.1.3 源码精读

设计文档里给出的标准两步式初始化（你上一单元已经手写过）：

[CODEMIRROR_LIVE_PREVIEW_DESIGN.md:114-134](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L114-L134) — 定义了 `EditorState.create({ doc, extensions })` 生成不可变状态，再 `new EditorView({ state, parent })` 挂到 DOM，并通过 `view.dispatch({ changes, selection })` 触发更新。

本项目真实的初始化在 demo 里：

[demo/main.ts:323-354](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L323-L354) — 用 `EditorState.create({ doc, extensions: [...] })` 构造初始状态，把库里的扩展一个个塞进 `extensions` 数组，再 `new EditorView({ state, parent })` 挂到 `#editor-basic`。这就是「不可变状态 + 可变视图」在真实代码里的样子。

`Transaction` 的痕迹在本项目里最直接体现在 `dispatch` 派发 effect 上。比如 mouseSelecting 接线：

[demo/main.ts:309-319](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L309-L319) — `view.dispatch({ effects: setMouseSelecting.of(true) })` 派发一个只携带 effect、不含文本改动的事务。这说明 Transaction 不仅能改文档，也能只「传话」给 StateField（这里把「用户正在拖拽」这个状态传进去）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在真实代码里数清楚「一次 dispatch 触发了哪些 Transaction 字段」。

**步骤**：

1. 打开 [demo/main.ts:309-319](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L309-L319)，对照下面的提示。
2. 找出 `mousedown` 回调里 `view.dispatch(...)` 的参数对象。它**没有** `changes`、**没有** `selection`，只有 `effects`。
3. 再打开 [demo/main.ts:452-485](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L452-L485) 的两个按钮回调，它们的 dispatch 参数**只有** `effects: modeCompartment.reconfigure([...])`。

**需要观察的现象**：

- 同一个 `view.dispatch`，参数对象的不同字段对应 Transaction 的不同「通道」：`changes`（改文本）、`selection`（改选区）、`effects`（传 effect / 重配扩展）。

**预期结果**：你能列出 Transaction 至少三个独立通道（changes / selection / effects），并解释「切换 Live/Source 模式」走的是 effects 通道、不碰文档内容。

> 待本地验证：若想看得更直观，可在 demo 里临时加一句 `EditorView.updateListener.of(u => console.log(u.transactions))`，在浏览器控制台观察每次按键产生的事务结构。

#### 4.1.5 小练习与答案

**练习 1**：如果 `EditorState` 是不可变的，那「用户敲了一个字」之后，编辑器显示的是新内容还是旧内容？

> **答案**：显示新内容。用户敲字会生成一个带 `changes` 的 Transaction，`dispatch` 后得到一份**新的** `EditorState`，`EditorView` 切换到这份新状态并重绘。旧的 State 被丢弃（或留在撤销栈里），但它本身没有被修改。

**练习 2**：下面的说法对吗——「`EditorView` 对象在编辑器运行期间会不断被替换成新实例」？

> **答案**：不对。`EditorView` 在编辑器生命周期里是**同一个**实例（直到 `view.destroy()`），被不断替换的是它内部持有的 `state`。

---

### 4.2 Decoration：控制显示的四种类型

#### 4.2.1 概念说明

`Decoration`（装饰）是 CodeMirror 6 控制文本「怎么显示」的核心机制。它本身不改文档字符，而是给某个区间附加样式、或用自定义 DOM 把它替换掉。理解 Live Preview 的关键，就是理解四种装饰类型分别能做什么。

设计文档 [Decoration 一节](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L136-L152) 列出了四种：

| 类型 | 作用 | 区间要求 | 本项目典型用途 |
|------|------|----------|----------------|
| `Decoration.mark` | 给一段文本范围**加属性/类名** | 必须有 from-to 区间 | 给 `**` 标记加 `cm-formatting-inline` 类，靠 CSS 显隐 |
| `Decoration.replace` | **替换（隐藏）**一段文本，可选用 widget 顶替 | 必须有 from-to 区间 | 把 `$E=mc^2$` 整段替换成 KaTeX 渲染结果 |
| `Decoration.widget` | 在某个**点**插入一个零宽度的 widget DOM | 只有一个位置 pos | 插入预览面板等独立 DOM |
| `Decoration.line` | 给**整行**加类名 | 一个位置 pos（取该位置所在行） | 给标题行加 `cm-heading-line` |

一条容易混淆的点：`replace` 和 `widget` 都能配合 `WidgetType`。区别是——`Decoration.replace({widget})` 会**先隐藏原文本再用 widget 顶上去**；而 `Decoration.widget({widget})` 是**在某个位置插入** DOM、原文本不动。本项目里最常用的是前三种（mark / replace / line）。

#### 4.2.2 核心流程

一个插件产出装饰的标准流程是：

```
1. 新建空数组 decorations: Range<Decoration>[]
2. 遍历语法树 / 正则，找到感兴趣的区间 [from, to]
3. 根据决策（如 shouldShowSource）选择装饰类型：
     - 想加样式类      → Decoration.mark({class}).range(from, to)
     - 想整段换 DOM    → Decoration.replace({widget}).range(from, to)
     - 想给整行加样式   → Decoration.line({class}).range(from)   // 注意只传 from
4. Decoration.set(decorations, true) 打包成 DecorationSet
5. 通过 ViewPlugin / StateField 把 DecorationSet 提供给视图
```

第 5 步「怎么提供给视图」是下一模块（4.3）的主题。注意 `Decoration.set` 的第二个参数 `true` 表示「区间允许精确嵌套」，本项目多处都传了它（见下方源码）。

#### 4.2.3 源码精读

**mark 类型 —— 给标记加类名：**

[livePreview.ts:121-123](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L121-L123) — 对块级标记（`#`/`-`/`>`）用 `Decoration.mark({ class: 'cm-formatting-block ...' }).range(node.from, node.to)`，靠类名切换 CSS 来做显隐动画。原文并未消失，只是被 CSS 压成极小字号。

[livePreview.ts:134-136](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L134-L136) — 行内标记（`*`/`~~`/`` ` ``）同样用 `Decoration.mark`，类名带不带 `-visible` 决定是展开还是收起。

打包语句在 [livePreview.ts:141-144](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L141-L144) — `Decoration.set(decorations.sort(...), true)`，注意排序后再打包。

**replace 类型（带 widget）—— 用渲染结果替换源码：**

[math.ts:69-73](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L69-L73) — 行内公式渲染态：`Decoration.replace({ widget }).range(node.from, node.to)`，把 `` `$E=mc^2$` `` 整段替换成 KaTeX 的 widget，原文被隐藏。

**line 类型 —— 给整行加样式：**

[math.ts:127-135](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L127-L135) — 块级公式编辑态：对围栏块逐行 `Decoration.line({ class: 'cm-math-source-block' }).range(line.from)`，只传一个 `from`（取该位置所在行）。

[markdownStyle.ts:86-90](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L86-L90) — 标题除了用 mark 加 `cm-header-X`，还额外 `Decoration.line({ class: 'cm-heading-line' }).range(node.from)` 给整行加装饰。这是「同一个节点同时用 mark + line 两种装饰」的典型写法。

> 补充：`Decoration.widget`（零宽插入）在本项目实际插件里较少直接使用，设计文档在 [5.1/5.2 节](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L930-L941) 用它演示「编辑态在公式下方插入预览面板」的思路（`side: 1, block: true`），可作为概念参考，不必在此刻深究。

#### 4.2.4 代码实践（源码阅读型）

**目标**：在一个文件里同时认出 mark 和 line 两种装饰。

**步骤**：

1. 打开 [markdownStyle.ts:71-92](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L71-L92)。
2. 找到 `Decoration.mark(...)` 那一行（约 81-83 行），记下它用的类名变量。
3. 紧接着找到 `Decoration.line(...)` 那一行（约 87-89 行），注意它**只传 `node.from` 一个参数**。

**需要观察的现象**：

- mark 装饰 `.range(node.from, node.to)` 有两个端点；line 装饰 `.range(node.from)` 只有一个端点。

**预期结果**：你能解释为什么 line 装饰只需要一个位置——因为它装饰的是「该位置所在的整行」，行边界由编辑器自己算。

#### 4.2.5 小练习与答案

**练习 1**：要把 `**world**` 里的两个 `**` 标记藏起来（光标不在时），应该用哪种装饰？

> **答案**：用 `Decoration.mark` 给 `**` 区间加类名（如 `cm-formatting-inline`），再用 CSS（`max-width:0` 等）把它收起。**不**用 `replace`，因为 replace 会完全隐藏原文、无法做 CSS 过渡动画，也无法在光标进入时平滑展开。

**练习 2**：`Decoration.replace` 和 `Decoration.widget` 都能放 widget，区别是什么？

> **答案**：`Decoration.replace({widget}).range(from,to)` 会隐藏 `[from,to]` 区间的原文、用 widget 顶替；`Decoration.widget({widget}).range(pos)` 是在某一点插入一个零宽 widget，原文不动。前者是「替换」，后者是「插入」。

---

### 4.3 ViewPlugin 与 StateField：提供装饰的两种方式

#### 4.3.1 概念说明

装饰本身只是描述，还需要有人把它「提供」给视图。CodeMirror 6 提供两条路：`ViewPlugin` 和 `StateField`。本项目两者都用，而且**故意在同一份 `math.ts` 里同时示范了两种**——理解它们的区别是本讲最重要的一环。

- **ViewPlugin（视图插件）**：基于一个类，有 `constructor(view)` 和 `update(viewUpdate)` 生命周期。它能访问 `view.viewport`（当前可见区域）等信息，适合做「随视口/选区变化而重算」的**行内装饰**。装饰通过 `decorations: v => v.decorations` 这个访问器暴露。
- **StateField（状态字段）**：是 `EditorState` 的一部分，有 `create(state)` 和 `update(value, tr)`。它的值随事务更新，并通过 `provide: f => EditorView.decorations.from(f)` 把装饰提供给视图。适合做**需要稳定、能进状态快照**的装饰，尤其是**块级装饰（block decoration）**。

一句话记忆：**行内装饰可以走 ViewPlugin；块级装饰必须走 StateField。**

#### 4.3.2 核心流程：为什么块级装饰必须用 StateField

这是本讲的核心论点，也是设计文档 [1.2 节](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L110-L242) 想让读者建立的关键认知。

块级装饰（`Decoration.replace({widget, block: true})`）会**插入新的块级 DOM、改变行的垂直结构**（行高、行数都会变）。而编辑器的**视口（viewport）**——「当前屏幕上能看到哪些行」——正是**根据这些垂直结构算出来的**。问题来了：

```
渲染一帧的顺序：
  1. 用户操作 → 新 EditorState
  2. 用 State 计算 viewport（决定要渲染哪些行）
  3. 才运行 ViewPlugin.update（这时 viewport 已经定了）
  4. 应用装饰、重绘 DOM
```

如果块级装饰由 ViewPlugin 提供，那么在第 2 步算视口时它**还没被算出来**（ViewPlugin 还没跑），视口就会算错，导致滚动、行高、可见性都跟着错乱。StateField 的值是 `EditorState` 的一部分，在第 2 步**已经可用**，所以块级装饰必须放进 StateField。

> 这个结论也是项目代码里写明的。`math.ts` 顶部注释 [math.ts:11-15](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L11-L15) 明确写道：行内公式用 ViewPlugin，块级公式用 StateField，「block decorations must be provided via StateField」。`blockMathField` 上方的注释 [math.ts:146-151](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L146-L151) 重复了同一句理由。

#### 4.3.3 源码精读

**ViewPlugin 的真实形态（行内公式）：**

[math.ts:36-48](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L36-L48) — `mathPlugin = ViewPlugin.fromClass(class { ... })`：`constructor(view)` 里首次构建装饰，`update(update: ViewUpdate)` 里根据 `checkUpdateAction(update) === 'rebuild'` 决定是否重建。它处理的是 `Decoration.replace({widget}).range(...)`（不带 `block`，见 [math.ts:71-73](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L71-L73)）这种**行内替换**。

[livePreview.ts:47-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L47-L59) 是另一个 ViewPlugin 例子，结尾的 `{ decorations: (v) => v.decorations }`（[livePreview.ts:146-149](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L146-L149)）就是 ViewPlugin 暴露装饰的访问器写法。

**StateField 的真实形态（块级公式）：**

[math.ts:152-181](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L152-L181) — `blockMathField = StateField.define<DecorationSet>({ create, update, provide })`：

- `create(state)` 初始构建；
- `update(deco, tr)` 处理事务：`tr.docChanged || tr.reconfigured` 时重建，拖拽中跳过，选区变化时重建；
- `provide: (f) => EditorView.decorations.from(f)`（[math.ts:180](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L180)）——这是 StateField 把装饰交给视图的标准写法，和 ViewPlugin 的 `decorations` 访问器形成对照。

它产出的就是带 `block: true` 的块级替换：[math.ts:119-124](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L119-L124) — `Decoration.replace({ widget, block: true }).range(node.from, node.to)`。

**对比小结**（同样在 [CODEMIRROR_LIVE_PREVIEW_DESIGN.md:154-203](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L154-L203)）：

| 维度 | ViewPlugin | StateField |
|------|-----------|------------|
| 生命周期参数 | `ViewUpdate`（含 viewport） | `(value, tr)` 事务 |
| 暴露装饰 | `decorations: v => v.decorations` | `provide: f => EditorView.decorations.from(f)` |
| 能否进状态快照 | 否 | 是 |
| 适合 | 行内装饰、随视口重算 | 块级装饰、需要稳定/可撤销 |
| 本项目代表 | `mathPlugin`、`livePreviewPlugin` | `blockMathField`、`tableField` |

#### 4.3.4 代码实践（本讲主任务·源码阅读型）

**目标**：阅读设计文档的架构概览章节，写一段话解释「为什么块级装饰必须通过 StateField 而不能通过 ViewPlugin 提供」。

**步骤**：

1. 读 [CODEMIRROR_LIVE_PREVIEW_DESIGN.md:110-242](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/CODEMIRROR_LIVE_PREVIEW_DESIGN.md#L110-L242)，重点关注 StateField 与 ViewPlugin 两小节（154-203 行）。
2. 对照真实代码：打开 [math.ts:11-15](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L11-L15) 与 [math.ts:146-151](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L146-L151) 的注释，确认项目自己给出的理由。
3. 写一段 100～150 字的中文解释。

**需要包含的要点**：

- 块级装饰会改变行的垂直结构（行高/行数）。
- 视口（viewport）的计算依赖这个垂直结构。
- 视口是根据 `EditorState` 在 ViewPlugin 运行**之前**算出的；StateField 的值属于 State，此时已可用，而 ViewPlugin 的装饰此时还没算出来。

**预期结果（参考答案）**：

> 块级装饰（`block: true`）会插入新的块级 DOM，改变编辑器的行高与可见行数，而 CodeMirror 计算「视口里要渲染哪些行」时必须先知道这些垂直结构。视口是根据 `EditorState` 算出来的，这一步发生在 `ViewPlugin.update` 运行**之前**。StateField 的值是 State 的一部分，算视口时已经可用；ViewPlugin 的装饰却要等它自己 `update` 跑完才出现——为时已晚。因此块级装饰只能由 StateField 提供。本项目正是据此把行内公式做成了 `mathPlugin`（ViewPlugin），把块级公式做成了 `blockMathField`（StateField）。

#### 4.3.5 小练习与答案

**练习 1**：`livePreviewPlugin` 处理的是行内标记（`*`、`#` 等），它用的是 ViewPlugin 还是 StateField？为什么这么选？

> **答案**：用的是 ViewPlugin（见 [livePreview.ts:47](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L47)）。因为它产出的是 `Decoration.mark`（行内加类名），不插入块级 DOM、不改变行垂直结构，用更轻量的 ViewPlugin 即可，还能方便地访问视口做按需重算。

**练习 2**：如果一个插件要渲染 Mermaid 图表（一个独立的大块 DOM，会撑高很多行），应该用 ViewPlugin 还是 StateField？

> **答案**：StateField。因为它会显著改变垂直结构，属于块级装饰范畴——和本项目把块级公式放在 `blockMathField` 是同一个道理。

---

### 4.4 Facet、Compartment 与 syntaxTree

这一模块把剩下三个高频出现的概念一起讲掉：`Facet`（配置）、`Compartment`（动态重配）、`syntaxTree`（语法树迭代）。它们没有前三个那么「核心」，但读源码时几乎每页都会撞见。

#### 4.4.1 概念说明

- **Facet（配置面）**：一种「可以从多个扩展共同往里塞值、最后 combine 成一个结果」的配置机制。本项目用它做总开关：`collapseOnSelectionFacet` 为 `true` 才启用 Live Preview。读取方式是 `state.facet(facet)`。
- **Compartment（配置隔间）**：把一组扩展装进一个「隔间」，之后可以用 `compartment.reconfigure(newExtensions)` 在运行时**整体替换**这组扩展，而不必重建编辑器。本项目用它做 Live / Source 模式切换。
- **syntaxTree（语法树）**：由 `@codemirror/lang-markdown` + Lezer 解析器产出，是 Markdown 的语法树。本项目几乎所有插件都靠 `syntaxTree(state).iterate({ enter })` 遍历它来「找到 `**`、`#`、表格、代码块」等节点。`node.name` 是节点类型（如 `EmphasisMark`、`FencedCode`），`node.from/node.to` 是它在文档里的字符区间。

#### 4.4.2 核心流程

**Facet 的「多输入 combine」模型**：

```
扩展A: collapseOnSelectionFacet.of(true)
扩展B: collapseOnSelectionFacet.of(false)   // 假如又塞了一次
            │ combine: values => values[0] ?? false
            ▼
最终值: 取第一个输入，没有则 false
            │
   插件读取: state.facet(collapseOnSelectionFacet)
```

**syntaxTree 驱动装饰的标准模式**（本项目最典型的套路）：

```
syntaxTree(state).iterate({
  enter(node) {
    if (node.name === 'FencedCode') {        // 按节点名过滤
      const info = node.node.getChild('CodeInfo')
      const lang = state.doc.sliceString(info.from, info.to)  // 取围栏语言
      ... 产出装饰 ...
    }
  }
})
```

这个模式你在 [livePreview.ts:78-139](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L78-L139)、[math.ts:102-141](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L102-L141)、[markdownStyle.ts:71-92](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts#L71-L92) 都能看到，是后续几讲的「主旋律」。

#### 4.4.3 源码精读

**Facet 的真实定义：**

[facets.ts:8-10](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts#L8-L10) — `collapseOnSelectionFacet = Facet.define<boolean, boolean>({ combine: (values) => values[0] ?? false })`。两个泛型分别是「单个输入的类型」和「combine 后的类型」；`combine` 取第一个输入、缺省为 `false`。这就是「未启用时默认显示源码」的开关来源。

**StateField + StateEffect 的拖拽状态（Facet 的近亲）：**

[mouseSelecting.ts:6](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L6) 定义 Effect：`setMouseSelecting = StateEffect.define<boolean>()`。

[mouseSelecting.ts:13-23](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L13-L23) 定义 Field：`update` 里遍历 `tr.effects`，遇到 `setMouseSelecting` 就更新值。这是「用 StateEffect 往 StateField 里传话」的标准写法，下一讲（u2-l3）会专门展开。

**Compartment 的真实接线：**

[demo/main.ts:321](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L321) 创建隔间 `basicModeCompartment = new Compartment()`，并在 [demo/main.ts:330-346](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L330-L346) 用 `basicModeCompartment.of([...])` 装进初始扩展。

切换到 Source 模式时：[demo/main.ts:476-485](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L476-L485) — `view.dispatch({ effects: basicModeCompartment.reconfigure([collapseOnSelectionFacet.of(false), markdownStylePlugin]) })`。注意 Source 模式关键就是 `collapseOnSelectionFacet.of(false)` 且**移除了 `livePreviewPlugin`**，于是所有标记全部可见。这正好呼应 4.3 的结论：切换模式走的是 effects 通道、不碰文档。

**syntaxTree 遍历的真实写法：**

[livePreview.ts:78-90](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L78-L90) — `syntaxTree(state).iterate({ enter: (node) => {...} })`，用一个 `markTypes` 数组按 `node.name` 过滤出标记节点。这是「语法树驱动装饰」的最小可读范例。

#### 4.4.4 代码实践（源码阅读型）

**目标**：追踪「模式切换」从按钮点击到 `collapseOnSelectionFacet` 取值变化的完整链路。

**步骤**：

1. 读 [demo/main.ts:476-485](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L476-L485)（Source 按钮）与 [demo/main.ts:452-474](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L452-L474)（Live 按钮），对比两个 `reconfigure([...])` 数组的差异。
2. 读 [facets.ts:8-10](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts#L8-L10)，确认 `combine` 的取值规则。
3. 想象 `shouldShowSource` 内部 `state.facet(collapseOnSelectionFacet)` 在两种模式下分别取到什么。

**需要观察的现象**：

- Live 模式数组里有 `collapseOnSelectionFacet.of(true)` + `livePreviewPlugin`；Source 模式数组里是 `collapseOnSelectionFacet.of(false)` 且没有 `livePreviewPlugin`。

**预期结果**：你能解释「点 Source 按钮 → facet 变 false → shouldShowSource 直接返回 false → 所有元素都显示源码」，以及「移除 livePreviewPlugin 让标记不再被 CSS 收起」这两条同时生效，才得到纯源码视图。

> 待本地验证：在 demo 里点切换按钮，用开发者工具的 Elements 面板观察 `cm-formatting-inline` 类是否随模式消失/出现。

#### 4.4.5 小练习与答案

**练习 1**：`Facet` 的 `combine` 为什么是 `values => values[0] ?? false` 而不是 `values => values.every(Boolean)`？

> **答案**：这是项目自己的取舍——它只认「第一个被注册的输入」。多数情况下只会有一个 `collapseOnSelectionFacet.of(...)`，取第一个足够；`?? false` 保证「一个都没塞」时默认关闭。换成 `every` 会变成「所有输入都为 true 才开」，语义不同。

**练习 2**：`Compartment.reconfigure(...)` 是通过 transaction 的哪个通道传递的？

> **答案**：effects 通道。它本质是一个 effect，`dispatch({ effects: compartment.reconfigure([...]) })`。所以切换模式不会改动文档内容（changes 通道为空）。

**练习 3**：`syntaxTree(state).iterate({ enter })` 里，`node.from` / `node.to` 是相对什么的坐标？

> **答案**：是**文档字符偏移量**（从文档开头算起的字符位置），不是行号、也不是 DOM 坐标。它可以直接传给 `Decoration.x.range(node.from, node.to)`。

---

## 5. 综合实践

把本讲四个概念串起来，做一次「源码追踪接力」。在 demo 里随便挑一个编辑器（比如 basic），输入：

```
普通文字 `$E=mc^2$` 与一段块级公式：

\`\`\`math
\int_0^1 x\,dx
\`\`\`
```

然后按下面的接力顺序，把每个概念对应到一处源码：

1. **EditorState / View / Transaction**：用户敲字 → 你在 [demo/main.ts:323-354](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L323-L354) 创建的 State 被 transaction 更新。
2. **syntaxTree**：`math` 插件用 `syntaxTree(state).iterate` 找到行内 `InlineCode` 节点和块级 `FencedCode` 节点（[math.ts:55-85](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L55-L85)、[math.ts:102-141](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L102-L141)）。
3. **Decoration 四类型**：行内公式光标在外时用 `Decoration.replace`（[math.ts:71-73](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L71-L73)）；块级公式光标在内时逐行用 `Decoration.line`（[math.ts:127-135](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L127-L135)）。
4. **ViewPlugin vs StateField**：行内那条由 `mathPlugin`（ViewPlugin）出，块级那条由 `blockMathField`（StateField）出——同一个 `math.ts` 文件里两套并存，正好印证「块级必须用 StateField」。

**交付物**：画一张简图，标出从「敲字」到「公式被渲染/展开」之间，State、Transaction、syntaxTree、Decoration、ViewPlugin/StateField 各自出现的顺序和位置。这张图就是后续几讲的「总地图」。

## 6. 本讲小结

- `EditorState` 是不可变快照，`EditorView` 是可变视图，二者用 `Transaction`（含 changes / selection / effects 三个通道）单向流动更新。
- `Decoration` 有四种类型：`mark`（加类名）、`replace`（替换/隐藏，可带 widget）、`widget`（零宽插入）、`line`（整行）；本项目最常用前三种。
- 行内装饰可走 `ViewPlugin`，**块级装饰必须走 `StateField`**，因为视口在 ViewPlugin 运行之前就要根据 State 算好，而块级装饰会改变垂直结构。
- `Facet` 做配置开关、`Compartment` 做运行时重配（走 effects 通道）、`syntaxTree` 驱动几乎所有插件去「按节点名找区间」。
- 本项目在 [src/plugins/math.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts) 里同时示范了 ViewPlugin（行内）与 StateField（块级），是理解这两种方式最好的对照样本。

## 7. 下一步学习建议

概念词汇表建好后，下一讲 [u2-l2 shouldShowSource：显示源码还是渲染](./u2-l2-should-show-source.md) 会进入本库真正的「大脑」——那个决定一段 Markdown 显示源码还是渲染结果的决策函数。它会用到本讲讲过的 `state.facet(...)`、`state.field(...)`、`state.selection.ranges`，建议接着读。

随后 [u2-l3](./u2-l3-facet-mouseSelecting-updatehelper.md) 会把本讲点到为止的 `Facet`、`StateField + StateEffect`、`checkUpdateAction` 三块状态支撑讲透；[u2-l4](./u2-l4-live-preview-plugin.md) 和 [u2-l5](./u2-l5-markdown-style-plugin.md) 则分别精读 `livePreviewPlugin`（ViewPlugin + `Decoration.mark`）和 `markdownStylePlugin`（ViewPlugin + `mark`/`line`）——你现在已经具备了读懂它们的全部前置概念。

如果想立刻加深对 Decoration 的手感，推荐直接去读 [src/plugins/markdownStyle.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/markdownStyle.ts) 全文，它是本项目里最短、最干净的一个 ViewPlugin 范例。
