# WidgetType 与 Decoration.replace 渲染模式

## 1. 本讲目标

前面几讲我们看到的 `livePreviewPlugin`、`markdownStylePlugin`，都是用 `Decoration.mark` 给文本**套 CSS 类**——文本本身还留在 DOM 里，只是改了样式。这一讲我们要学习 Live Preview 的**第二种渲染策略**：用 `Decoration.replace` 把一段 Markdown 原文**整个替换**成一个由我们自己生成的真实 DOM 节点。

学完本讲你应该能够：

1. 说清 `WidgetType` 子类必须实现/重写的 `eq`、`toDOM`、`ignoreEvent` 各自的职责与触发时机。
2. 区分 `Decoration.replace({ widget })`（行内替换）与 `Decoration.replace({ widget, block: true })`（块级替换）两种用法，以及为什么块级替换**必须**由 `StateField` 提供。
3. 理解 `eq` 相等性判定如何帮 CodeMirror 跳过无谓的 DOM 重建，从而避免渲染闪烁与性能浪费。

本讲以 `MathWidget`（数学公式渲染）作为范例，它结构最简单、最能体现「替换渲染」的精髓。

---

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（它们在 u2 单元讲过）：

- **Decoration 的四种类型**：`mark`（加类名）、`replace`（替换/隐藏原文，可带 widget）、`widget`（零宽插入）、`line`（整行）。本讲的主角是 `replace`。
- **ViewPlugin 与 StateField 的区别**：`ViewPlugin` 适合行内装饰、随视口滚动重算；`StateField` 的值属于 EditorState、能进入状态快照。**块级装饰只能由 StateField 提供**，这是本讲的一个关键结论。
- **shouldShowSource**：决策函数，返回 `true` 表示显示源码（可编辑），返回 `false` 表示显示渲染结果。本讲里 Widget 的「显隐」完全由它驱动。
- **KaTeX**：一个把 LaTeX 数学语法（如 `E = mc^2`）渲染成 HTML 的库，是本项目的可选依赖（peer dependency），没装也能优雅降级。

> 关键直觉：`mark` 是「化妆」（原文还在，只改外观），`replace + Widget` 是「换人」（原文被藏起来，换成我们造的 DOM）。什么时候必须换人？当渲染结果和原文**结构完全不同**时——比如把 `$E=mc^2$` 这串字符变成漂亮的数学排版，CSS 永远做不到这件事。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| [src/widgets/mathWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts) | 数学公式 Widget，继承 `WidgetType` | 作为 `WidgetType` 生命周期的范例（`eq`/`toDOM`/`ignoreEvent`） |
| [src/plugins/math.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts) | 公式插件，行内用 `mathPlugin`（ViewPlugin），块级用 `blockMathField`（StateField） | 作为 `Decoration.replace` 两种用法（带 `block` / 不带 `block`）的范例 |
| [src/utils/mathCache.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts) | KaTeX 渲染结果缓存 | 理解 `toDOM` 内部如何产出 HTML |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | 显示源码还是渲染结果的决策函数 | 理解 Widget 在「渲染态 / 源码态」之间如何切换 |
| [src/widgets/linkWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts) / [src/widgets/imageWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts) | 链接、图片 Widget | 作为横向对照，证明全库所有 Widget 都遵循同一套模式 |

记住这张地图的分层（来自 u1-l2）：`plugins` 是编排者（决定在哪个区间放哪种装饰），`widgets` 是 DOM 制造者（继承 `WidgetType` 产出真实 DOM），`utils` 提供纯函数与缓存。本讲跨越 `widgets` 与 `plugins` 两层。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **WidgetType 生命周期**——一个 Widget 要实现哪几个方法，各在什么时候被调用。
2. **Decoration.replace + block**——如何把 Widget 装到文档里，行内与块级的差异与限制。
3. **eq 相等性优化**——为什么 `eq` 能省掉大量 DOM 重建。

### 4.1 WidgetType 生命周期（eq / toDOM / ignoreEvent）

#### 4.1.1 概念说明

`WidgetType` 是 CodeMirror 6 提供的抽象基类（来自 `@codemirror/view`）。它的作用是：**告诉 CodeMirror「在这个位置，我要插入一段我自己造的 DOM」**。一个 Widget 实例代表「一个待渲染的 DOM 片段」。

要自己造 DOM，你就得继承 `WidgetType` 并回答 CodeMirror 的三个问题：

| 方法 | CodeMirror 的问题 | 你的回答 |
| --- | --- | --- |
| `toDOM()` | 「这个 Widget 长什么样？给我一个 DOM 节点。」 | `document.createElement(...)` 造好返回。 |
| `eq(other)` | 「我手里这个旧 Widget 和你新算出来的 Widget，是不是同一个？需不需要重建 DOM？」 | 比较关键字段，相同返回 `true`（复用旧 DOM），不同返回 `false`（重建）。 |
| `ignoreEvent(event)` | 「这个鼠标/键盘事件发生在 Widget 上，要不要让 CodeMirror 介入处理？」 | 返回 `true` 表示忽略（CodeMirror 不管，留给你的 DOM 自己处理），返回 `false` 表示交给 CodeMirror。 |

> 注意：`WidgetType` 的方法不是生命周期钩子的全部，但它们是**你必须主动实现**的核心三件套。其中 `toDOM` 是必填，`eq` 和 `ignoreEvent` 有默认实现但几乎总要重写。

#### 4.1.2 核心流程

一个 Widget 从「被创建」到「出现在屏幕上」的流程：

```text
插件 build() 阶段：
  发现一个需要渲染的语法节点
  → new MathWidget(source, isBlock)        // ① 创建 Widget 实例
  → Decoration.replace({ widget }).range(from, to)  // ② 装进 replace 装饰
  → 装饰集合交给 CodeMirror

CodeMirror 渲染阶段（对每个 replace+widget 装饰）：
  有没有「同位置 + eq 返回 true」的旧 Widget？
  ├─ 有：复用旧 DOM，跳过 toDOM            // ③ eq 命中，省掉重建
  └─ 无：调用 widget.toDOM() 得到新 DOM     // ④ 真正造 DOM
        → 插入到文档对应位置，替换掉原文

事件阶段（鼠标点在 Widget 上）：
  → 调用 widget.ignoreEvent(event)
  ├─ true：CodeMirror 不处理，事件留给你的 DOM
  └─ false：CodeMirror 当作在编辑器里的点击（如定位光标）
```

「先 `eq`，命中才省略 `toDOM`」这条顺序是本讲的性能关键，4.3 节会展开。

#### 4.1.3 源码精读

先看 `MathWidget` 的整体骨架，它把三件套都实现了：

类声明与构造函数——继承 `WidgetType`，用 `readonly` 参数属性直接定义 `source` 和 `isBlock` 两个字段：[src/widgets/mathWidget.ts:L15-L25](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L15-L25)（`MathWidget extends WidgetType`，构造时接收公式源码与是否块级两个参数）。

`eq`——只比较 `source` 和 `isBlock` 两个字段，二者都相等才返回 `true`：[src/widgets/mathWidget.ts:L32-L34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L32-L34)（注释明确写了「用于优化：相等则跳过重渲染」）。注意它没有比较 DOM 引用之类的东西——Widget 是纯数据，`eq` 比的是「这两个数据会不会生成一样的 DOM」。

`toDOM`——根据 `isBlock` 决定根标签是 `div` 还是 `span`，套上 `cm-math-block`/`cm-math-inline` 类，再调用 `renderMath` 得到 HTML 塞进 `innerHTML`，并用 `try/catch` 兜底错误：[src/widgets/mathWidget.ts:L39-L53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L39-L53)。这里有两个细节值得记住：

1. `toDOM` 每次被调用都是**从零造一个新 DOM**，所以它的代价要尽量低（这里靠 `mathCache` 把昂贵的 KaTeX 渲染缓存掉了）。
2. `toDOM` 内部捕获异常，渲染失败时改成展示错误文本并加 `cm-math-error` 类——这是「永不让 toDOM 抛错」的良好实践，否则会把整个编辑器拖崩。

`ignoreEvent`——固定返回 `false`：[src/widgets/mathWidget.ts:L60-L62](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L60-L62)（注释说「返回 false 以允许点击进入编辑模式」）。意思是：当用户点击渲染好的公式，CodeMirror 应当介入——这正是 Live Preview「点一下就定位光标、显示源码」的交互来源。

横向对比一下，全库的 Widget 都遵循同一套三件套，只是 `eq` 比较的字段不同：

| Widget | `eq` 比较的字段 | `ignoreEvent` | 参考 |
| --- | --- | --- | --- |
| `MathWidget` | `source`、`isBlock` | 固定 `false` | [mathWidget.ts:L32-L34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L32-L34) |
| `LinkWidget` | `text`、`url`、`isWikiLink` | 固定 `false` | [linkWidget.ts:L77-L83](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts#L77-L83) |
| `ImageWidget` | `src`、`alt`、`title` | 固定 `false` | [imageWidget.ts:L54-L60](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L54-L60) |
| `CodeBlockWidget` | 全部 `CodeBlockData` | 依事件类型判断 | 见 u5 |

记住这条规律：**`eq` 比较的永远是与渲染结果相关的「身份字段」——哪些字段变了，DOM 就必须重建**。`MathWidget` 不比较位置（`from/to`），因为同一公式无论在第几行，渲染出来的 DOM 都一样。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试，理解 `eq`/`ignoreEvent` 的可观测行为。

**操作步骤**：

1. 打开 [src/widgets/__tests__/codeBlockWidget.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts)，定位到 `describe('equality', ...)`（L247-L285）与 `describe('ignoreEvent', ...)`（L315-L333）。
2. 阅读这几个用例，注意它们断言的是 `widget1.eq(widget2)` 的布尔返回值，而不是 DOM。
3. 在仓库根目录运行 `npm test -- codeBlockWidget`（待本地验证确切过滤参数），观察测试是否通过。

**需要观察的现象**：`eq` 测试只用「数据相同/不同」来驱动断言，与 DOM、与 CodeMirror 实例都无关——这印证了 `eq` 是一个**纯数据比较**，可以脱离编辑器单独测试。

**预期结果**：数据完全相同 → `eq` 返回 `true`；任一字段不同 → 返回 `false`。

#### 4.1.5 小练习与答案

**练习 1**：假如 `MathWidget.eq` 改成永远返回 `false`，会发生什么？

> **参考答案**：CodeMirror 每次更新都会认为「旧 Widget 和新 Widget 不相等」，于是**每次都调用 `toDOM` 重建 DOM**。对数学公式这意味着反复触发 KaTeX 渲染（虽有 `mathCache` 兜底，但仍多一次 `innerHTML` 赋值和 DOM 重建），轻则掉帧，重则在频繁更新时可见闪烁。`eq` 的意义就是把这种无谓重建挡掉。

**练习 2**：`MathWidget.ignoreEvent` 固定返回 `false`，为什么对公式来说是合理的？如果是一个带「复制按钮」的代码块 Widget，`ignoreEvent` 又该怎么做？

> **参考答案**：公式本身没有可交互的内部控件，用户点它通常是想「定位光标、进入编辑」，所以把事件交给 CodeMirror 处理（返回 `false`）正合适。而代码块 Widget 内部有复制按钮、有需要原生选中的代码行，这些事件不应该被 CodeMirror 抢走，所以 `CodeBlockWidget.ignoreEvent` 会**按事件类型区分**（如对 `mousedown` 返回 `true`，见 L315-L333 的测试），而不是一刀切。

---

### 4.2 Decoration.replace + block：替换渲染策略

#### 4.2.1 概念说明

有了 Widget，还得把它「装」到文档里。装它的容器就是 `Decoration.replace`：

```typescript
Decoration.replace({ widget: new MathWidget(source, isBlock) }).range(from, to)
```

`replace` 的语义是：**把 `[from, to)` 这段原文从视图中隐藏，在它的位置插入 `widget.toDOM()` 产出的 DOM**。注意三件事：

1. **原文没有真正消失**：文档状态（`state.doc`）里那段 `$E=mc^2$` 还在，只是**视图层**把它藏起来换成了渲染结果。这就是「Live Preview 不改你的数据，只改你看到的样子」的本质。
2. **`from`、`to` 决定隐藏范围**：通常就是整个语法节点的区间（含 `` ` ``、`$` 等标记符号），这样渲染态下用户完全看不到原始语法。
3. **`block` 选项决定行内还是块级**：不传 `block`（默认 `false`）是行内替换，渲染结果嵌在同一行里；传 `block: true` 是块级替换，渲染结果**独占若干行、垂直撑开**。

#### 4.2.2 核心流程

`math.ts` 同时演示了两种 `replace`，它们的来源与归属完全不同：

```text
行内公式 `$E=mc^2$`（写成 InlineCode 节点）：
  mathPlugin（ViewPlugin）.build()
  → 找到 InlineCode 节点，文本形如 `$...$`
  → shouldShowSource(...) == false 且非拖拽
  → Decoration.replace({ widget })                // 不带 block，行内替换
       .range(node.from, node.to)

块级公式 ```math ... ```（FencedCode 节点）：
  blockMathField（StateField）.create/update → buildBlockMathDecorations()
  → 找到 FencedCode，其 CodeInfo 子节点语言 == 'math'
  → shouldShowSource(...) == false 且非拖拽
  → Decoration.replace({ widget, block: true })   // 带 block，块级替换
       .range(node.from, node.to)
```

两条路的关键差异是**提供装饰的机制不同**：

- 行内 → `ViewPlugin`（[math.ts:L36-L93](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L36-L93)）。
- 块级 → `StateField`（[math.ts:L152-L181](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L152-L181)）。

> 为什么块级必须用 StateField？因为块级装饰会**改变行的垂直高度**（一个公式块可能占好几行）。CodeMirror 在测量视口、计算滚动时，必须能从 **EditorState** 里读到这些块级装饰——而 `ViewPlugin` 的装饰属于视图层、在测量之后才更新，来不及参与布局。这条规则在 u2-l1 已经铺垫，这里是它在真实代码里的落地。源码注释也写得很直白：[math.ts:L150](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L150)「块级装饰不能用 ViewPlugin 提供」。

#### 4.2.3 源码精读

**行内替换（不带 block）**：在 `mathPlugin.build` 里，当公式不需要显示源码时，用 `Decoration.replace({ widget })`：[src/plugins/math.ts:L69-L73](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L69-L73)。注意它紧挨着一个 `else` 分支——当需要显示源码（光标在公式内、或正在拖拽）时，改用 `Decoration.mark({ class: 'cm-math-source' })`：[src/plugins/math.ts:L75-L80](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L75-L80)。

这是一个非常重要的模式——**同一个语法节点，根据 `shouldShowSource` 在两种装饰之间二选一**：

| 状态 | 产出装饰 | 视觉效果 |
| --- | --- | --- |
| 渲染态（光标不在内） | `Decoration.replace({ widget })` | 原文被替换成漂亮排版 |
| 源码态（光标在内/拖拽中） | `Decoration.mark({ class: 'cm-math-source' })` | 显示 `` `$E=mc^2$` `` 原文，加底色提示 |

判断条件 `!isTouched && !isDrag` 里有两个输入：`isTouched` 来自 `shouldShowSource`（[math.ts:L67](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L67)），`isDrag` 来自 `mouseSelectingField`（[math.ts:L53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L53)）。两者任一为真都进入源码态——拖拽时强制显示源码是为了消除选区闪烁（u2-l3 讲过）。

**块级替换（带 block: true）**：在 `buildBlockMathDecorations` 里，逻辑结构与行内版几乎对称，只是 `replace` 多了 `block: true`：[src/plugins/math.ts:L116-L124](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L116-L124)。它的源码态分支也对应改成了**逐行**装饰——因为块级公式横跨多行，要用 `Decoration.line({ class: 'cm-math-source-block' })` 给每一行都加底色：[src/plugins/math.ts:L126-L136](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L126-L136)。

对比一下两种 replace 的写法：

```typescript
// 行内（ViewPlugin 提供，不改变行结构）
Decoration.replace({ widget }).range(node.from, node.to)

// 块级（StateField 提供，会撑开多行）
Decoration.replace({ widget, block: true }).range(node.from, node.to)
```

**关于「识别」的一个技巧**：行内公式没有专门的语法节点，库复用了 `InlineCode` 节点——只要节点文本以 `` `$ `` 开头、以 `` $` `` 结尾、长度大于 4，就当作行内公式处理：[src/plugins/math.ts:L58-L66](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L58-L66)。这是一种「借用既有语法」的务实做法，避免改动 Lezer 语法定义（u3-l2 会专门讲）。

#### 4.2.4 代码实践

**实践目标**：用真实编辑器观察 `replace` 装饰的存在与切换。

**操作步骤**：

1. 进入 `demo` 目录运行 `npm run dev`（u1-l3 讲过，这是 Vite 启动网页，端口通常 5173）。
2. 在浏览器打开页面，找到启用了 `mathPlugin` 的编辑器（参考 README / demo 配置）。
3. 输入一行：`计算 `$E=mc^2$` 的值`。
4. 把光标移到行尾（不在公式内），观察公式被渲染。
5. 用方向键把光标移进 `` `$...$` `` 内部，观察渲染结果消失、变成带底色的源码。

**需要观察的现象**：光标在公式外时，公式区是一段渲染好的排版（原文被 `replace` 隐藏）；光标一进入，渲染排版消失、原始字符 `` `$E=mc^2$` `` 显形——这正是 `replace` ↔ `mark` 两种装饰的实时切换。如果想看测试层面的等价验证，可读 [src/plugins/__tests__/math.test.ts:L121-L137](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L121-L137)（断言光标进入公式时会出现 `cm-math-source` 类）。

**预期结果**：光标进入 → 出现源码态标记；光标移出 → 恢复渲染态。若 KaTeX 未安装，渲染态会显示错误提示（见 4.2.3 的 `try/catch` 兜底）。

#### 4.2.5 小练习与答案

**练习 1**：把块级公式的 `block: true` 去掉会怎样？

> **参考答案**：渲染结果不再独占整块，而是「压扁」嵌进文档流里，垂直方向不再撑开行高，排版会错乱；更根本的问题是，这种改变行高的块级装饰若不经 StateField 提供会无法正确参与布局。`block: true` 同时承担「视觉上独占成块」和「告诉 CodeMirror 这会改变垂直结构」两层含义。

**练习 2**：为什么源码态不用 `Decoration.replace`（不带 widget）来隐藏原文，而用 `Decoration.mark`？

> **参考答案**：源码态的目的恰恰相反——**要让原文可见、可编辑**。`replace` 的本职是隐藏原文，方向就错了。用 `mark` 给原文加个底色类（`cm-math-source`），既保留原文可编辑，又给用户一个「这段现在处于编辑态」的视觉提示，这才是 Live Preview 想要的效果。

---

### 4.3 eq 相等性优化：避免无谓重渲染

#### 4.3.1 概念说明

每次编辑器更新（打字、移动光标、滚动），插件都可能重新 `build` 一遍装饰集合，于是会造出一批**全新的 Widget 实例**。如果每次都因为「是新实例」就重建 DOM，编辑器会剧烈闪烁、CPU 飙升。

CodeMirror 的解法是：**在替换旧装饰为新装饰时，对同一位置的 widget 调用 `oldWidget.eq(newWidget)`**。

- 返回 `true` → 两个 Widget 等价，**复用旧 DOM**，不调 `toDOM`。
- 返回 `false` → 不等价，销毁旧 DOM，调用 `newWidget.toDOM()` 造新 DOM。

于是 `eq` 成了一道**重建闸门**：你只要把「会影响渲染结果」的字段放进 `eq`，就能让绝大多数更新（比如单纯移动光标）跳过昂贵的 DOM 重建。

#### 4.3.2 核心流程

可以把 CodeMirror 的判定抽象成下面这段伪逻辑：

```text
对每个新的 replace+widget 装饰 D_new（位置 [from, to)）：
  查找旧装饰集合里同位置的 D_old（其 widget 为 W_old）
  if W_old 存在 且 W_old.eq(W_new) == true：
      保留 W_old 的 DOM            // 命中：跳过 toDOM
  else:
      W_new.toDOM() → 新 DOM        // 未命中：重建
      替换掉旧 DOM
```

关键在于：**Widget 实例本身是新建的（`new MathWidget(...)`），但只要 `eq` 说相等，旧 DOM 就被保留**。所以「实例是新的」和「DOM 要重建」是两回事——`eq` 是二者之间的缓冲。

#### 4.3.3 源码精读

回到 `MathWidget.eq`：[src/widgets/mathWidget.ts:L32-L34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L32-L34)。

```typescript
eq(other: MathWidget): boolean {
  return other.source === this.source && other.isBlock === this.isBlock;
}
```

这两个字段为什么「够用」？因为 `toDOM` 的输出**完全**由 `source` 和 `isBlock` 决定：

- `toDOM` 选 `div` 还是 `span` → 由 `isBlock` 决定（[mathWidget.ts:L40](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L40)）。
- `toDOM` 的类名 → 由 `isBlock` 决定（[mathWidget.ts:L41](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L41)）。
- `toDOM` 的内容 → 由 `renderMath(source, isBlock)` 决定（[mathWidget.ts:L44](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L44)），而 `renderMath` 的缓存键又是 `'inline:'+source` / `'block:'+source`（[mathCache.ts:L27](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L27)）。

既然 `toDOM` 的全部输出都依赖这两个字段，那 `eq` 比较这两个字段就是**充分且必要**的——既不漏判（不会该重建却复用），也不多判（不会该复用却重建）。这就是写 `eq` 的核心心法：**`eq` 比较的字段集合，必须等于「决定 `toDOM` 输出的字段集合」**。

对比其他 Widget，字段更多但道理一样：

- `LinkWidget.eq` 比 `text`、`url`、`isWikiLink`（[linkWidget.ts:L77-L83](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts#L77-L83)）。
- `ImageWidget.eq` 比 `src`、`alt`、`title`（[imageWidget.ts:L54-L60](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L54-L60)）。

> 注意 `ImageWidget.eq` **没有**比较 `options`（如 `maxWidth`、`showAlt`）。这是一个有意为之的取舍：`options` 一般是全局配置、不会频繁变；即便它变了 `eq` 也不会触发重建。如果未来要让「改配置即时刷新」生效，就得把相关字段补进 `eq`。读源码时要留意这种「`eq` 与 `toDOM` 字段不完全对应」的边界情况。

#### 4.3.4 代码实践

**实践目标**：通过测试断言，验证 `eq` 的纯函数本质。

**操作步骤**：

1. 阅读 [src/widgets/__tests__/codeBlockWidget.test.ts:L247-L285](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L247-L285) 的 `equality` 分组。
2. 仿照其风格，在本地为 `MathWidget` 写三个断言（**示例代码**，非项目原有）：
   ```typescript
   import { MathWidget } from '../mathWidget';
   const a = new MathWidget('E=mc^2', false);
   expect(a.eq(new MathWidget('E=mc^2', false))).toBe(true);   // 数据全等
   expect(a.eq(new MathWidget('E=mc^2', true))).toBe(false);   // isBlock 不同
   expect(a.eq(new MathWidget('F=ma', false))).toBe(false);    // source 不同
   ```
3. 运行该测试（待本地验证），确认三条断言都通过。

**需要观察的现象**：`eq` 测试完全不需要 `EditorView`、不需要 KaTeX、不需要 DOM——它就是一个普通的方法调用断言。这说明「相等性」是可以脱离 CodeMirror 单独验证的纯逻辑。

**预期结果**：三条断言全绿。

#### 4.3.5 小练习与答案

**练习 1**：假设你给 `MathWidget` 加了一个 `fontSize` 字段并用于 `toDOM`（控制公式字号），但忘了加进 `eq`，会出什么 bug？

> **参考答案**：改了 `fontSize` 后，`eq` 仍然返回 `true`（因为只比 `source`、`isBlock`），CodeMirror 复用旧 DOM，**字号不会更新**。这是一个典型的「`eq` 字段集合小于 `toDOM` 依赖集合」导致的过期渲染 bug。修复方法：把 `fontSize` 也加进 `eq`。

**练习 2**：反过来，如果把 `eq` 写成永远返回 `true`（无论字段如何），又会有什么问题？

> **参考答案**：即使 `source` 变了（用户改了公式），`eq` 仍说相等，CodeMirror 复用旧 DOM，**公式内容不会跟着更新**——屏幕上还是旧公式。这说明 `eq` 的字段集合不能小于 `toDOM` 依赖集合；正确写法是两者**严格相等**。

---

## 5. 综合实践

把三个最小模块串起来，完成本讲的实践任务：**实现一个最简 WidgetType 子类，把 `===text===` 替换成带下划线的 `<u>` 元素，正确实现 `eq` 与 `toDOM`，并用 `Decoration.replace` 应用它**。

下面是**示例代码**（非项目原有），展示完整三件套与 `replace` 的接线。建议在 `demo` 里新建一个文件试跑：

```typescript
// 示例代码：UnderlineWidget —— 把 ===text=== 渲染成 <u>text</u>
import { WidgetType, Decoration, EditorView, ViewPlugin } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';

// ① Widget：决定 DOM 长什么样 + 相等性
class UnderlineWidget extends WidgetType {
  constructor(readonly text: string) {
    super();
  }
  // eq：决定 toDOM 输出的唯一字段是 text，所以只比它
  eq(other: UnderlineWidget): boolean {
    return other.text === this.text;
  }
  toDOM(): HTMLElement {
    const u = document.createElement('u');
    u.textContent = this.text;   // 用 textContent 避免 XSS
    return u;
  }
  ignoreEvent(): boolean {
    return false;                // 点击交给 CodeMirror，便于定位光标
  }
}

// ② ViewPlugin：找到 ===text=== 区间，用 replace 替换（行内，不带 block）
export const underlinePlugin = ViewPlugin.fromClass(
  class {
    decorations;
    constructor(view: EditorView) {
      this.decorations = this.build(view);
    }
    update(update) {
      // 简化：文档变化才重建（生产代码可用 checkUpdateAction，见 u2-l3）
      if (update.docChanged || update.viewportChanged) {
        this.decorations = this.build(update.view);
      }
    }
    build(view) {
      const decos = [];
      const doc = view.state.doc.toString();
      // 用正则找 ===text===（示例用正则；真实插件优先用语法树节点）
      const re = /===(.+?)===/g;
      let m;
      while ((m = re.exec(doc)) !== null) {
        const from = m.index;
        const to = m.index + m[0].length;
        decos.push(
          Decoration.replace({ widget: new UnderlineWidget(m[1]) }).range(from, to)
        );
      }
      return Decoration.set(decos, true);
    }
  },
  { decorations: (v) => v.decorations }
);
```

**验收清单（对照本讲三个模块）**：

1. **WidgetType 生命周期**：`UnderlineWidget` 实现了 `eq`、`toDOM`、`ignoreEvent` 三件套；`toDOM` 用 `textContent` 而非 `innerHTML`，安全且满足「造一个新 DOM」的要求。
2. **Decoration.replace**：用 `Decoration.replace({ widget }).range(from, to)` 行内替换；原文 `===text===` 在视图层被 `<u>text</u>` 取代，但 `state.doc` 不变。
3. **eq 优化**：`eq` 只比较 `text`——而这正是 `toDOM` 输出的唯一决定因素，字段集合严格对应，既不漏判也不多判。

**进阶（可选）**：接入 `shouldShowSource` 与 `checkUpdateAction`（来自 u2 单元），让光标进入 `===text===` 时显示原文、移出时恢复下划线渲染——这就和 `mathPlugin` 的「渲染态/源码态双模式」完全一致了。若想更进一步，参考 u7-l4 把它做成完整的自定义插件并导出。

> 待本地验证：上述示例未包含在本仓库中，需要你自行新建文件并在 demo 中接入后运行观察。正则方案仅用于演示「replace 替换」的核心；本项目的真实插件都基于语法树节点（如 `mathPlugin` 找 `InlineCode`），更健壮。

---

## 6. 本讲小结

- Live Preview 有两种渲染策略：`Decoration.mark + CSS`（原文还在、只改样式，适合标记显隐）与 `Decoration.replace + Widget`（原文被藏、换成自造 DOM，适合公式/表格/代码等结构化渲染）。本讲学的是后者。
- 一个 `WidgetType` 子类的核心三件套：`toDOM`（造 DOM）、`eq`（判定是否复用旧 DOM）、`ignoreEvent`（事件是否交给 CodeMirror）。全库的 `MathWidget`/`LinkWidget`/`ImageWidget` 都遵循同一套模式。
- `Decoration.replace({ widget })` 行内替换；`Decoration.replace({ widget, block: true })` 块级替换。**块级装饰必须由 StateField 提供**，因为它改变行的垂直结构，必须能从 EditorState 读到——`math.ts` 用 `mathPlugin`（ViewPlugin）处理行内、用 `blockMathField`（StateField）处理块级，正是这条规则的体现。
- 同一语法节点常在两种装饰间二选一：渲染态用 `replace+widget`，源码态用 `mark` 加提示类，由 `shouldShowSource` 与 `mouseSelectingField` 共同驱动切换。
- `eq` 是重建闸门：写 `eq` 的心法是「比较字段集合 = 决定 `toDOM` 输出的字段集合」，多判会漏更新，少判会白重建。

---

## 7. 下一步学习建议

本讲建立了「Widget + replace」这套通用渲染模式。接下来：

- **u3-l2（mathPlugin：行内数学公式）**：深入 `mathPlugin` 这个 ViewPlugin，看它如何用 `checkUpdateAction` 驱动更新，以及为什么行内公式要「借用」`InlineCode` 节点。
- **u3-l3（blockMathField 与 mathCache）**：专门讲块级公式的 StateField 实现与 KaTeX 缓存设计，是本讲「块级必须用 StateField」结论的延伸。
- **u4 表格子系统、u5 代码块子系统**：本讲的 Widget 模式在表格、代码块里被用到极致（尤其代码块的 `inline` 模式会把 HAST 转成 `Decoration.mark`，是 replace 与 mark 的混合用法）。

建议在进入 u3-l2 前，先回到本讲的「综合实践」，亲手把 `UnderlineWidget` 跑起来——真正写过一遍三件套，再去读 `mathPlugin` 的源码会顺畅很多。
