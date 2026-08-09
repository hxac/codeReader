# mathPlugin：行内数学公式

## 1. 本讲目标

本讲承接上一讲（u3-l1）建立的「`WidgetType` + `Decoration.replace` 替换渲染」认知，把它落到一个真实的、可运行的插件上：`mathPlugin`。

读完本讲，你应当能够：

- 说清楚为什么行内公式复用 `InlineCode` 节点，而不是为 `$...$` 写一套自定义语法。
- 画出 `mathPlugin` 的更新生命周期：构造 → `update` → `checkUpdateAction` → `build`，并知道何时重建、何时跳过。
- 解释「渲染态用 `Decoration.replace`、编辑态用 `Decoration.mark`」这种双模式的取舍，并理解 `mathPlugin` 在拖拽期间为何要「强制源码态」。

本讲只讲**行内公式**（`` `$...$` ``，对应 `mathPlugin` 这个 `ViewPlugin`）。块级公式（` ```math ` 围栏块，对应 `blockMathField` 这个 `StateField`）留到下一讲 u3-l3。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义，这里只做最简提示，不重复展开）：

| 概念 | 来自 | 一句话提示 |
| --- | --- | --- |
| `shouldShowSource(state, from, to)` | u2-l2 | 三步短路判定：开关 → 拖拽 → 选区相交，返回 `true` 表示「显示源码」。 |
| `checkUpdateAction(update)` | u2-l3 | 把一次 `ViewUpdate` 归结为 `rebuild` / `skip` / `none` 三种动作。 |
| `mouseSelectingField` / `setMouseSelecting` | u2-l3、u1-l4 | 记录「是否正在拖选」的 `StateField`，由外部 `mousedown`/`mouseup` 派发 Effect 改写。 |
| `WidgetType`（`toDOM`/`eq`/`ignoreEvent`） | u3-l1 | `toDOM` 造 DOM、`eq` 决定能否复用旧 DOM、`ignoreEvent` 决定事件是否交给 CodeMirror。 |
| `Decoration.replace` vs `Decoration.mark` | u2-l1、u3-l1 | replace 隐藏原文并替换成 Widget DOM；mark 给原文加 CSS 类、原文仍在。 |

如果上面任何一个概念你还不熟，建议先回去补对应讲义再继续——本讲会直接复用它们，不再从头解释。

补充一个关于 KaTeX 的常识：KaTeX 是一个把 LaTeX 公式源码（如 `E=mc^2`）渲染成 HTML+CSS 的库。本库把 `katex` 列为**可选依赖**，由 `renderMath` 在运行时读取 `window.katex`；没安装则优雅降级为错误提示。本讲不展开渲染细节，那是 u3-l3 的内容。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 本讲用到的部分 |
| --- | --- | --- |
| [src/plugins/math.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts) | 装饰编排者 | `mathPlugin`（行内公式的 `ViewPlugin`，L36–L93） |
| [src/widgets/mathWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts) | DOM 渲染单元 | `MathWidget` 类与工厂函数 `createMathWidget`（L15–L74） |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | 决策核心 | 决定区间显示源码还是渲染（L31–L53） |
| [src/core/pluginUpdateHelper.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts) | 更新决策 | `checkUpdateAction`，驱动 `ViewPlugin` 更新（L35–L65） |
| [src/utils/mathCache.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts) | 纯函数与缓存 | `renderMath`，行内公式的渲染入口（L26–L55） |
| [src/plugins/\_\_tests\_\_/math.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts) | 测试 | 行内公式的行为断言（L67–L165） |

数据流一览（先有个整体印象，后面逐段精读）：

```
用户输入 `$E=mc^2$`
        │
        ▼
markdown() 用 Lezer 解析 → 语法树里得到一个 InlineCode 节点
        │
        ▼
mathPlugin.build() 遍历语法树，命中 InlineCode，再做文本模式判定
        │
        ▼
shouldShowSource(...) 判定光标是否触碰该区间
        │
   ┌────┴─────┐
   ▼          ▼
未触碰      触碰/拖拽
   │          │
   ▼          ▼
Decoration.replace   Decoration.mark
 (MathWidget)        (cm-math-source)
渲染成 KaTeX          保留原文可编辑
```

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **InlineCode 节点技巧**：怎么识别行内公式。
2. **ViewPlugin + checkUpdateAction**：插件什么时候重新计算装饰。
3. **渲染/编辑双模式**：`replace` 与 `mark` 如何二选一。

### 4.1 InlineCode 节点技巧：复用已有语法识别行内公式

#### 4.1.1 概念说明

标准 Markdown 的行内公式约定是 `$...$`（如 `$E=mc^2$`）。但本项目用的语法不是裸 `$...$`，而是**反引号包裹、内部用 `$`** 的写法：`` `$E=mc^2$` ``。README 的功能矩阵和示例代码里也是这么写的。

```ts
mathPlugin,                        // Inline math: `$E=mc^2$`
```

为什么会这样？这是本讲最重要的设计取舍：**`mathPlugin` 没有为数学公式写一套自定义语法，而是「搭便车」复用了 Lezer 已有的 `InlineCode` 节点。**

回顾一下，`@codemirror/lang-markdown` 的 `markdown()` 扩展会用 Lezer 解析 Markdown。Lezer 对 `` `...` ``（反引号包裹的内容）有一个现成的节点类型叫 `InlineCode`。它**没有**内置 `$...$` 规则。如果要让 `$...$` 变成一等公民的语法节点，就得写一段自定义的 Lezer 语法覆盖（`override`），成本不低，还会和已有的行内规则产生歧义。

本项目的取巧做法是：

- 让用户把公式写成 `` `$...$` ``。这样 Lezer 自然把它解析成一个 `InlineCode` 节点（和普通行内代码 `` `code` `` 走同一条解析路径）。
- `mathPlugin` 在遍历语法树时，专门挑出 `InlineCode` 节点，**再在文本层面**判断它到底是一个普通代码块，还是一个公式。

这是一种「结构层面复用、文本层面区分」的技巧：用现成的节点结构定位候选区间，用正则/前缀匹配来分流。好处是零语法成本、即装即用；代价是用户必须接受 `` `$...$` `` 这个略不标准的写法。

#### 4.1.2 核心流程

识别一个 `InlineCode` 节点是不是行内公式，`build` 里有三步判定：

1. **节点类型筛选**：`node.name === 'InlineCode'`，只看行内代码节点。
2. **取全文**：`state.doc.sliceString(node.from, node.to)`，拿到包括首尾反引号和 `$` 的完整文本。
3. **模式匹配**：必须同时满足
   - 以 `` `$ `` 开头（`fullText.startsWith('`$')`），
   - 以 `` $` `` 结尾（`fullText.endsWith('$`')`），
   - 长度大于 4（`fullText.length > 4`）。

为什么是 `length > 4`？最短的可能候选是 `` `$$` ``（反引号、`$`、`$`、反引号，共 4 个字符），它的「源码」是空串——没有意义的公式。`length > 4` 要求至少 5 个字符，也就是源码至少 1 个字符，把空公式挡在门外。

满足这三步，才算一个真正的行内公式。之后用 `fullText.slice(2, -2)` 去掉首尾各 2 个字符（前导 `` `$ `` 和末尾 `` $` ``），得到不含定界符的公式源码。

用伪代码表示：

```
对语法树里每个节点:
    若 node.name == "InlineCode":
        text = 取节点全文
        若 text 以 "`$" 开头 且 以 "$`" 结尾 且 长度 > 4:
            source = text[2 .. -2]      # 剥掉定界符
            进入「渲染/编辑」判定（见 4.3）
```

#### 4.1.3 源码精读

`mathPlugin` 的 `build` 方法是这一切的入口。先看它如何遍历语法树并定位 `InlineCode`：

[math.ts:50-88](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L50-L88) —— 用 `syntaxTree(state).iterate({ enter })` 遍历语法树，`enter` 回调里只处理 `node.name === 'InlineCode'` 的节点。注意注释明确写了 `// Only handle inline formulas`（只处理行内公式），块级公式交给同文件下方的 `blockMathField`。

紧接着是模式匹配与源码剥离的核心几行：

[math.ts:58-67](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L58-L67) —— 这里完整体现了 4.1.2 说的三步判定：

```ts
if (node.name === 'InlineCode') {
  const fullText = state.doc.sliceString(node.from, node.to);

  if (
    fullText.startsWith('`$') &&
    fullText.endsWith('$`') &&
    fullText.length > 4
  ) {
    const source = fullText.slice(2, -2);
    const isTouched = shouldShowSource(state, node.from, node.to);
    // ...渲染/编辑分支（见 4.3）
```

- `node.from`/`node.to` 是该 `InlineCode` 节点在文档里的字符区间（含首尾反引号）。
- `slice(2, -2)` 精确剥掉 `` `$ `` 与 `` $` ``，例如全文 `` `$E=mc^2$` `` → 源码 `E=mc^2`。
- `shouldShowSource` 的返回值 `isTouched` 决定走渲染分支还是源码分支，4.3 节详解。

注意这里**没有** `else` 给「不匹配模式的 `InlineCode`」——也就是说，普通行内代码 `` `const x = 1` `` 会被直接跳过，什么装饰都不产生，保持 CodeMirror 原生的行内代码外观。这正是「文本层面区分」的好处：同一个节点类型，按内容各走各的。

`MathWidget` 拿到剥好的 `source` 后如何渲染？工厂函数很简单：

[mathWidget.ts:72-74](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L72-L74) —— `createMathWidget(source, false)`：第二个参数 `false` 表示「行内模式」（`isBlock=false`），工厂只是 `new MathWidget(source, false)`。

而 `MathWidget` 内部把渲染工作委托给 `renderMath`：

[mathWidget.ts:39-53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L39-L53) —— `toDOM` 行内模式创建一个 `<span class="cm-math-inline">`，调用 `renderMath(this.source, this.isBlock)` 得到 KaTeX 输出的 HTML 字符串塞进 `innerHTML`；整个过程用 `try/catch` 兜底，公式解析失败时降级成 `[Math Error: ...]` 文本，**绝不向外抛错**（u3-l1 讲过 `toDOM` 不能抛异常这条铁律）。

`renderMath` 的行内/块级由 `displayMode` 参数控制，缓存键里也带了这个区分：

[mathCache.ts:26-31](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L26-L31) —— 行内公式调用 `renderMath(source, false)`，缓存键形如 `inline:E=mc^2`；块级则形如 `block:...`。因此同一个源码 `` $x^2$ `` 的行内与块级渲染结果不会互相污染缓存。缓存细节留到 u3-l3。

#### 4.1.4 代码实践

这是一个**源码阅读 + 边界推演**型实践，目标是让你对三步判定烂熟于心。

1. **实践目标**：手动验证模式匹配，确认哪些 `InlineCode` 节点会被当成公式、哪些不会。
2. **操作步骤**：
   - 仿照下面的判定逻辑，在脑子里（或写个小函数）对若干候选全文跑一遍 `startsWith('\`$') && endsWith('$\`') && length > 4`：
     - `` `$x$` ``（5 字符）
     - `` `$$` ``（4 字符）
     - `` `$E=mc^2$` ``
     - `` `plain code` ``（普通行内代码）
     - `` `not math$` ``（只有结尾没有开头的 `$`）
   - 参考测试 [math.test.ts:111-119](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L111-L119) 的 `should detect inline math formula` 用例，确认 `` `$E=mc^2$` `` 会产出装饰。
3. **需要观察的现象**：把每个候选的「是否命中」「若命中的 `source`」填进下表。
4. **预期结果**：

   | 候选全文 | 是否命中 | `source`（若命中） |
   | --- | --- | --- |
   | `` `$x$` `` | ✅ | `x` |
   | `` `$$` `` | ❌（长度不大于 4） | — |
   | `` `$E=mc^2$` `` | ✅ | `E=mc^2` |
   | `` `plain code` `` | ❌（不以 `` `$ `` 开头） | — |
   | `` `not math$` `` | ❌（不以 `` `$ `` 开头） | — |

5. 想本地验证，可在仓库根目录运行 `npm test`，确认行内公式相关用例通过。

#### 4.1.5 小练习与答案

**练习 1**：如果用户写 `` `$ a + b $` ``（`$` 和内容之间有空格），`source` 会是什么？它还能被识别为公式吗？

> **答案**：能被识别——它以 `` `$ `` 开头、以 `` $` `` 结尾、长度大于 4。`source = fullText.slice(2, -2)` = ` a + b `（前后各带一个空格）。是否美观取决于 KaTeX 对空格的处理，但从 `mathPlugin` 的判定看它确实命中。这说明判定只看「定界符形态」，不看内容是否「合法的 LaTeX」。

**练习 2**：为什么判定用 `length > 4` 而不是 `length > 3`？

> **答案**：`length > 3` 会把 `` `$$` ``（4 字符）也放进来，此时 `slice(2,-2)` 得到空串 `''`，是一个无意义的空公式。`length > 4` 保证源码至少 1 个字符。

---

### 4.2 ViewPlugin + checkUpdateAction：驱动 mathPlugin 的更新生命周期

#### 4.2.1 概念说明

`mathPlugin` 是一个 `ViewPlugin`（u2-l1 讲过：`ViewPlugin` 适合随视口重算的行内装饰）。它的生命周期遵循 u2-l4 里 `livePreviewPlugin` 用过的同一套模式：

- **构造时**算一次装饰。
- **每次 `update`** 时，用一个统一的决策函数判断「要不要重算」。

这个统一的决策函数就是 u2-l3 引入的 `checkUpdateAction`。`mathPlugin` 不自己手写「文档变了才重建」之类的条件，而是直接复用它，这是本库「core 层沉淀公共逻辑、plugins 层只负责编排」的典型体现。

`checkUpdateAction` 把一次更新（`ViewUpdate`）归结为三种动作之一：

- `'rebuild'`：需要重算装饰。
- `'skip'`：跳过本次更新（拖拽期间，防闪烁）。
- `'none'`：无需动作。

`mathPlugin` 只关心一件半事：是不是 `'rebuild'`。是，就重算；不是（`'skip'` 或 `'none'`），就什么都不做，沿用上一次的装饰。

#### 4.2.2 核心流程

`checkUpdateAction` 的判定顺序（短路，前面命中就返回）：

```
1. docChanged || viewportChanged || reconfigured   → rebuild
2. 拖拽刚结束（wasDragging && !isDragging）        → rebuild
3. 正在拖拽（isDragging）                          → skip
4. 选区改变（selectionSet）                        → rebuild
5. 都不是                                           → none
```

对应到 `mathPlugin` 的行为：

| 更新来源 | `checkUpdateAction` 返回 | `mathPlugin` 动作 |
| --- | --- | --- |
| 打字 / 删除（`docChanged`） | `rebuild` | 重算装饰（公式增删、内容变） |
| 视口滚动（`viewportChanged`） | `rebuild` | 重算（新进入视口的公式要渲染） |
| 扩展重配（`reconfigured`） | `rebuild` | 重算 |
| 拖拽结束（mouseup） | `rebuild` | 重算（从源码态回到渲染态） |
| **拖拽进行中** | `skip` | **不重算，装饰冻结** |
| 光标移动（`selectionSet`） | `rebuild` | 重算（公式在渲染态/源码态间切换） |
| 无关更新 | `none` | 不重算 |

关键点：**拖拽进行中返回 `'skip'`，所以 `mathPlugin` 在拖拽期间完全不重建**，装饰被「冻结」在拖拽开始那一刻的状态。这避免了拖选时公式在 widget 和源码之间来回横跳的闪烁。等到 `mouseup`、拖拽结束，才触发一次 `rebuild` 刷新到最新光标状态。

#### 4.2.3 源码精读

`mathPlugin` 用 `ViewPlugin.fromClass` 定义，类结构非常精简：

[math.ts:36-48](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L36-L48) —— 三件套：

```ts
class {
  decorations: DecorationSet;

  constructor(view: EditorView) {
    this.decorations = this.build(view);     // 构造时算一次
  }

  update(update: ViewUpdate) {
    if (checkUpdateAction(update) === 'rebuild') {   // 只认 rebuild
      this.decorations = this.build(update.view);
    }
  }
```

- `constructor`：编辑器初次创建时算一次装饰。
- `update`：只在 `checkUpdateAction(update) === 'rebuild'` 时重算。注意它对 `'skip'` 和 `'none'` 一视同仁——都不重建、沿用旧 `this.decorations`。

插件规范的第二参数把装饰暴露给 CodeMirror：

[math.ts:90-93](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L90-L93) —— `{ decorations: (v) => v.decorations }` 告诉 CodeMirror「这个插件的装饰就是实例的 `decorations` 字段」。这是 `ViewPlugin.fromClass` 的标准写法（u2-l4 的 `livePreviewPlugin` 同款）。

再回到决策函数本身，确认它的判定顺序：

[pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65) —— 注意判定次序：**结构性变化（doc/viewport/reconfigured）排在最前**，优先级高于拖拽判定；拖拽判定（L46–L57）又排在选区判定（L60–L62）之前。这意味着「拖拽中 + 文档恰好变化」会命中第 1 条 `rebuild`，而不是第 3 条 `skip`——这一点在 4.3 节会用到。

`math.test.ts` 有两个用例直接验证这个生命周期：

- [math.test.ts:139-150](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L139-L150) —— 文档变化后装饰被重建（`should rebuild on document change`）。
- [math.test.ts:152-164](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L152-L164) —— 选区变化后插件仍可用（`should rebuild on selection change`）。

#### 4.2.4 代码实践

1. **实践目标**：把 `checkUpdateAction` 的判定顺序内化为一张决策表。
2. **操作步骤**：对照 [pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65)，为下列五种情形分别写出 `checkUpdateAction` 的返回值，并说明 `mathPlugin` 会做什么。
3. **需要观察的现象**：注意「拖拽中 + 文档变化」这种组合情形——它命中哪一条？
4. **预期结果**：

   | 情形 | 返回动作 | `mathPlugin` 行为 | 理由 |
   | --- | --- | --- | --- |
   | 文档改变（打字） | `rebuild` | 重算 | 公式可能增删或内容变化 |
   | 拖拽中（仅选区变化） | `skip` | 冻结装饰 | 防止 widget/源码来回横跳闪烁 |
   | 拖拽刚结束（mouseup） | `rebuild` | 重算 | 刷新到最新光标状态 |
   | 选区改变（点击移动光标，非拖拽） | `rebuild` | 重算 | 公式要在渲染态/源码态间切换 |
   | 拖拽中 + 同时打字 | `rebuild` | 重算 | 结构性变化优先级高于拖拽 `skip` |

5. 最后一行是本练习的「陷阱题」：结构性变化（第 1 条）先于拖拽（第 3 条）判定，所以即使正在拖拽，只要文档变了就会 `rebuild`。这一点在 4.3 节解释 `build` 里的 `!isDrag` 防御时会呼应。

#### 4.2.5 小练习与答案

**练习 1**：`mathPlugin.update` 里为什么对 `'skip'` 和 `'none'` 都不做处理？这两种情况有什么共同点？

> **答案**：两者都意味着「不需要新装饰」。`'skip'` 是「故意先不算」（拖拽中冻结），`'none'` 是「确实没变化」。对 `mathPlugin` 而言，结果都是「沿用 `this.decorations`」，所以统一只判断 `=== 'rebuild'` 即可，写法最简。

**练习 2**：如果删掉 `update` 方法，让插件每次更新都重新 `build`，会有什么问题？

> **答案**：拖拽期间每次鼠标移动产生的选区变化都会触发一次全树遍历 + 重建，既浪费性能，又会因为公式在 widget/源码间反复切换而闪烁。`checkUpdateAction` 的 `'skip'` 正是为了挡掉这些无谓重建。

---

### 4.3 渲染/编辑双模式：Decoration.replace 与 Decoration.mark 的二选一

#### 4.3.1 概念说明

行内公式有两种「样子」要呈现：

- **渲染态**：光标不在公式上时，把 `` `$E=mc^2$` `` 原文隐藏，替换成 KaTeX 渲染出来的漂亮公式。
- **编辑态**：光标进入公式时，把原文显示出来、可以打字修改，并加一点视觉提示（比如背景色）表明「这是一个可编辑的公式源码」。

这两种态用了**两种不同的 Decoration**：

| 模式 | Decoration | 作用 | 原文去哪了 |
| --- | --- | --- | --- |
| 渲染态 | `Decoration.replace({ widget })` | 用 `MathWidget` 的 DOM 替换原文 | 被隐藏，换成 KaTeX 输出 |
| 编辑态 | `Decoration.mark({ class: 'cm-math-source' })` | 给原文加 CSS 类 | 原文保留，可编辑 |

为什么渲染态用 `replace`、编辑态用 `mark`？

- **渲染态必须用 replace**：因为要展示的是完全不同的 DOM（KaTeX 的 HTML），不是给原文加样式就能得到的；只有 `replace + widget` 能把原文藏起来、塞进新 DOM（u3-l1 已建立）。
- **编辑态只能用 mark**：因为你要在原文里打字。`replace` 会把原文隐藏，光标进不去、改不了；`mark` 只加样式、原文和光标都还在，这才是「可编辑」。这正是 u2-l4 里 `livePreviewPlugin` 坚持用 `mark` 而非 `replace` 的同类理由——只要「原文本身要留着可编辑」，就只能用 `mark`。

所以同一个公式节点，**随光标位置在两种 Decoration 之间整体切换**，而不是叠加。

#### 4.3.2 核心流程

`build` 对每个命中公式节点的判定（承接 4.1 的源码剥离之后）：

```
isTouched = shouldShowSource(state, node.from, node.to)   # 光标/选区是否触碰
isDrag    = state.field(mouseSelectingField, false)       # 是否正在拖拽

若 !isTouched 且 !isDrag:        → Decoration.replace + MathWidget  （渲染态）
否则（触碰 或 拖拽）:              → Decoration.mark + cm-math-source   （编辑态）
```

注意一个关键细节：判定条件是 `!isTouched && !isDrag`，即**两个条件都得满足才渲染**。换句话说，**只要正在拖拽（`isDrag` 为真），就一律进编辑态**——哪怕光标根本不在这个公式上。

这看起来和 `shouldShowSource` 内部已有的拖拽短路（u2-l2 第二步：拖拽时返回 `false`，即「显示渲染态」）矛盾。其实不矛盾，而是 `mathPlugin` 在 `shouldShowSource` 之上**又加了一道** `!isDrag` 闸门，刻意**反转**了拖拽期间的默认行为。原因有二：

1. **widget 的挂载/卸载比 CSS 类切换贵得多**。`livePreviewPlugin`（`mark`+CSS）拖拽时只是切个类名，几乎无成本；而数学 widget 拖拽时若反复 mount/unmount，会出现明显的布局抖动。
2. **`checkUpdateAction` 在「拖拽中 + 文档变化」时会返回 `rebuild`**（4.2 提到的优先级）。也就是说，`build` 确实存在「在拖拽中被调用」的边角路径。这道 `!isDrag` 就是为这种边角情形兜底：即便被迫重建，拖拽中也别挂 widget，先以稳定的源码态示人，等拖拽结束再统一刷新。

下面这张真值表把所有情形列清楚（对一个具体的行内公式节点）：

| `isTouched` | `isDrag` | `!isTouched && !isDrag` | 产出 | 用户看到 |
| --- | --- | --- | --- | --- |
| false（光标远离） | false（未拖拽） | true | `replace` + `MathWidget` | KaTeX 渲染公式 |
| true（光标在内） | false | false | `mark` `cm-math-source` | 原文 `` `$...$` `` + 提示样式 |
| true（选区覆盖） | false | false | `mark` `cm-math-source` | 原文 + 提示样式 |
| false（光标远离） | true（拖拽中） | false | `mark` `cm-math-source` | **强制显示原文**，不挂 widget |
| 任意 | true（拖拽中） | false | `mark` `cm-math-source` | 强制显示原文 |

最后一行是本模块的核心结论：**拖拽期间，所有行内公式统一退回源码态，杜绝 widget 抖动。**

#### 4.3.3 源码精读

渲染/编辑的二选一就写在 4.1 引用的同一段 `build` 里，紧接源码剥离之后：

[math.ts:69-81](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L69-L81) ——

```ts
if (!isTouched && !isDrag) {
  const widget = createMathWidget(source, false);   // 行内：isBlock=false
  decorations.push(
    Decoration.replace({ widget }).range(node.from, node.to)
  );
} else {
  decorations.push(
    Decoration.mark({ class: 'cm-math-source' }).range(node.from, node.to)
  );
}
```

逐行拆解：

- `!isTouched && !isDrag`：渲染态需要「未被触碰」且「未在拖拽」同时成立，缺一不可。
- `createMathWidget(source, false)`：第二个参数 `false` = 行内模式。注意行内公式这里**不带 `block: true`**——这正是它能用 `ViewPlugin` 而非 `StateField` 的原因（u3-l1 讲过：`block` 装饰必须进 `StateField`；行内 `replace` 可以走 `ViewPlugin`）。
- `Decoration.replace({ widget }).range(node.from, node.to)`：替换范围覆盖**整个 `InlineCode` 节点**（含首尾反引号和 `$`），所以渲染后用户看不到任何定界符。
- `else` 分支 `Decoration.mark({ class: 'cm-math-source' })`：原文保留可编辑，只加 `cm-math-source` 类。这个类在 `editorTheme` 里定义样式（如淡淡的背景），起到「这里是一段公式源码」的提示作用，但**不改变文本内容**。

`isTouched` 怎么来的？就是 `shouldShowSource`：

[math.ts:67](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L67) —— `const isTouched = shouldShowSource(state, node.from, node.to)`。`shouldShowSource` 内部三步（开关 → 拖拽 → 选区相交）在 u2-l2 已详述。这里我们只取它的「触碰」语义：光标或选区与 `[node.from, node.to]` 相交即为 `true`。

而 `isDrag` 是 `mathPlugin` 自己再读一次拖拽态：

[math.ts:53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L53) —— `const isDrag = state.field(mouseSelectingField, false)`。注意第二参数 `false` 是「字段不存在时降级为 `false`」（防御性读取，u2-l3 讲过）。

`shouldShowSource` 内部其实已经读过一次拖拽态（拖拽时直接返回 `false`），`mathPlugin` 这里**再读一次**并用于自己的分支判定。这就是「双层拖拽防护」：`shouldShowSource` 管「算不算触碰」，`mathPlugin` 管「触碰了要不要渲染」。

测试如何验证编辑态？关键用例是「光标进入公式时产出 `cm-math-source`」：

[math.test.ts:121-137](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L121-L137) —— 文档 `` `$E=mc^2$` ``、光标放在位置 5（落在节点区间内），用 `decorations.between(...)` 遍历装饰，断言能找到带 `cm-math-source` 类的 mark。这正是 `isTouched=true` 走 `else` 分支的证据。

> 关于 `MathWidget.eq`：u3-l1 讲过「`eq` 是重建闸门」。行内公式的 `eq` 只比较 `source` 和 `isBlock`（行内恒为 `false`，实际只比 `source`）。这意味着两个公式源码相同就复用旧 DOM、不重渲染。本讲不重复展开，记住「源码变了才换 DOM」即可。详见 [mathWidget.ts:32-34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L32-L34)。

#### 4.3.4 代码实践

这是本讲规格指定的核心实践：**追踪光标进入/退出行内公式时，`mathPlugin` 各产出什么装饰**，用表格对照。

1. **实践目标**：亲手让编辑器在渲染态和编辑态间切换，并验证每种态的 Decoration 种类与 CSS 类。
2. **操作步骤**（两种方式任选其一）：
   - **方式 A（读测试）**：阅读 [math.test.ts:67-165](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L67-L165) 的 `inline math plugin` 分组。注意 `createTestView(doc, cursorPos)`（L68–L81）如何把 `markdown()`、`mouseSelectingField`、`collapseOnSelectionFacet.of(true)`、`mathPlugin` 组装起来，并用 `cursorPos` 决定光标位置。
   - **方式 B（动手跑）**：在仓库根目录新建一个临时测试文件（或临时修改现有测试的副本），构造文档 `` `$E=mc^2$` text``：
     - 第一次 `createTestView(doc, 15)`（光标在 `text` 里，远离公式），遍历装饰记录种类。
     - 再 `view.dispatch({ selection: { anchor: 5 } })`（把光标移进公式），遍历装饰记录种类。
     - 可在遍历回调里 `console.log(deco.spec)` 观察每种装饰的类型与类名。
3. **需要观察的现象**：光标在公式外时，装饰的 `spec` 里有 `widget`（一个 `MathWidget` 实例）、没有 `class`；光标移进公式后，装饰变成带 `class: 'cm-math-source'`、没有 `widget`。
4. **预期结果**（对照表）：

   | 光标位置 | `isTouched` | 产出 Decoration | 关键字段 | 用户可见 |
   | --- | --- | --- | --- | --- |
   | 远离公式（如在末尾 `text`） | `false` | `Decoration.replace` | `spec.widget` = `MathWidget(source, false)` | KaTeX 渲染的公式，定界符不可见 |
   | 进入公式内部 | `true` | `Decoration.mark` | `spec.class` = `cm-math-source` | 原文 `` `$E=mc^2$` `` + 提示背景，可编辑 |
   | 选区横跨公式 | `true` | `Decoration.mark` | `spec.class` = `cm-math-source` | 同上（编辑态） |
   | 拖拽进行中（任意位置） | 受 `!isDrag` 覆盖 | `Decoration.mark` | `spec.class` = `cm-math-source` | 强制原文态，无 widget |

5. **运行验证**：`npm test` 应全部通过；若你用 `console.log`，注意测试环境 mock 了 KaTeX（见测试文件 L11–L27 的 `beforeEach`），渲染态的 widget 内部是 mock 的 `<span class="katex ...">...</span>`，而不是真实 KaTeX 输出。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `build` 里的 `&& !isDrag`，只留 `if (!isTouched)`，拖拽期间会发生什么？

> **答案**：拖拽期间 `shouldShowSource` 因自身拖拽短路返回 `false`（`isTouched=false`），于是 `!isTouched` 为真，会走渲染分支挂上 widget。结合 4.2 的「拖拽中 + 文档变化 → `rebuild`」边角路径，`build` 可能在拖拽中被调用，从而在拖选过程中反复 mount/unmount widget，产生布局抖动。`!isDrag` 正是为了堵住这个缺口。

**练习 2**：编辑态为什么不能用 `Decoration.replace`（哪怕 replace 一个「显示原文的 widget」）？

> **答案**：`replace` 会用 widget 的 DOM 替换掉文档原文，光标落进的是 widget 内部的 DOM，而不是真正的文档文本——你在里面打字并不会修改文档、CodeMirror 也无法把光标映射回文档位置，编辑就失效了。`mark` 保留原文、光标在真实文档文本上，才能正常编辑。这就是「需要可编辑就必须用 mark」的根本原因。

---

## 5. 综合实践

把三个模块串起来，做一次**完整的调用链追踪**：从用户输入到一个行内公式被渲染，再到光标进入它被切回源码。

**任务**：在文档 `` 公式 `$E=mc^2$` 很重要 `` 中，追踪下面这条时间线，每一步写出「触发源 → `checkUpdateAction` 返回 → `build` 命中哪个节点 → 产出什么 Decoration」。

1. **初始加载**：编辑器刚创建，光标在文档末尾。
2. **光标移入公式**：用户点击或按方向键，光标进入 `$E=mc^2$` 区间内。
3. **开始拖拽**：用户在公式上按下鼠标并拖动（`mousedown` → `setMouseSelecting(true)`）。
4. **拖拽结束**：用户松开鼠标（`mouseup` → `setMouseSelecting(false)`）。

**参考答案**（请先自己写，再对照）：

| 步骤 | 触发源 | `checkUpdateAction` | `build` 命中 | 产出 | 用户看到 |
| --- | --- | --- | --- | --- | --- |
| 1 初始加载 | 构造函数 | —（直接 `build`） | `InlineCode`，`isTouched=false`、`isDrag=false` | `replace` + `MathWidget` | 渲染公式 |
| 2 光标移入 | `selectionSet` | `rebuild` | `isTouched=true`、`isDrag=false` | `mark` `cm-math-source` | 原文 + 提示，可编辑 |
| 3 开始拖拽 | `setMouseSelecting(true)` | `skip`（拖拽中） | 不调用 `build`，装饰冻结 | （沿用步骤 2 的 mark） | 原文态，无抖动 |
| 4 拖拽结束 | `setMouseSelecting(false)` | `rebuild`（拖拽刚结束） | 依当前光标重算 | 取决于光标最终位置：在内→mark，在外→replace | 刷新到正确态 |

**加分思考**：步骤 3 为什么 `build` 不被调用？——因为 `checkUpdateAction` 在「正在拖拽」时返回 `skip`，`mathPlugin.update` 只在 `rebuild` 时才调 `build`。这道 `skip` 与 `build` 内的 `!isDrag` 构成「双层防护」：前者挡住绝大多数拖拽期重建，后者兜底「拖拽中被强制重建」的边角路径。

如果想在本地真的跑一遍，参考 [math.test.ts:68-81](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/math.test.ts#L68-L81) 的 `createTestView` 写法，用 `view.dispatch({ selection })` 模拟光标移动、用 `view.dispatch({ effects: setMouseSelecting.of(true) })` 模拟拖拽（`setMouseSelecting` 从 `src/core/mouseSelecting.ts` 导入），并在每次 dispatch 后打印 `view.plugin(mathPlugin)?.decorations` 观察变化。

---

## 6. 本讲小结

- **InlineCode 节点技巧**：`mathPlugin` 不写自定义语法，而是让用户写 `` `$...$` ``，复用 Lezer 现成的 `InlineCode` 节点，再在文本层面用「以 `` `$ `` 开头、以 `` $` `` 结尾、长度 > 4」三步判定分流公式与普通代码。
- **ViewPlugin + checkUpdateAction**：`mathPlugin` 是 `ViewPlugin`，构造时算一次、`update` 时只认 `checkUpdateAction` 的 `rebuild`；拖拽中返回 `skip` 使装饰冻结，光标移动/文档变化才重建。
- **渲染/编辑双模式**：未触碰且未拖拽 → `Decoration.replace` + `MathWidget`（渲染态，原文被替换）；触碰或拖拽 → `Decoration.mark` + `cm-math-source`（编辑态，原文保留可编辑）。
- **双层拖拽防护**：`shouldShowSource` 内部已有拖拽短路，`build` 又加一道 `!isDrag`，刻意让拖拽期间所有公式退回稳定的源码态，杜绝 widget mount/unmount 抖动，并兜底「拖拽中被强制重建」的边角路径。
- **行内能用 ViewPlugin 的关键**：行内 `replace` 不带 `block: true`，所以可以走 `ViewPlugin`；块级公式带 `block` 必须走 `StateField`，那是下一讲的内容。

---

## 7. 下一步学习建议

本讲只讲了**行内公式**（`mathPlugin`，`ViewPlugin`）。自然的下一步是 u3-l3 **块级公式**：

- 阅读 [math.ts:98-181](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L98-L181) 的 `buildBlockMathDecorations` 和 `blockMathField`，对比它与 `mathPlugin` 的三处不同：
  1. 用 `StateField` 而非 `ViewPlugin`（因为块级装饰带 `block: true`，会改变行高，必须进入 `EditorState`）。
  2. 节点识别从 `InlineCode` 换成 `FencedCode`，靠 `CodeInfo` 子节点取语言（`lang === 'math'`）、`CodeText` 子节点取源码。
  3. 编辑态从「单个 mark」换成「逐行 `Decoration.line` 加 `cm-math-source-block`」。
- 同时精读 [mathCache.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts) 的缓存键设计（`inline:` / `block:` 前缀）与 `clearMathCache`，理解它如何降低重复公式的 KaTeX 开销。

掌握行内与块级两种公式后，你就完整理解了 `WidgetType` + `Decoration.replace` 这条渲染主线，可以进入 u4 表格子系统了。
