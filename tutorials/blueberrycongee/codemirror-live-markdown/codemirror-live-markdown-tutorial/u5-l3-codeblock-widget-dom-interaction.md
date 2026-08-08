# codeBlockWidget：DOM 渲染、复制按钮与点击定位

## 1. 本讲目标

上一讲（u5-l2）我们讲清了代码块子系统的「决策与状态层」：`codeBlockField` 如何识别 `FencedCode` 节点、三种 `interaction` 模式（auto / toggle / inline）的本质区别、以及由 `setCodeBlockSourceMode` + `codeBlockSourceModeField` 维护的源码态。但那些都还停留在「该不该渲染、渲染什么」的层面。

本讲我们要真正打开渲染单元本身——`CodeBlockWidget`。当你读完本讲，应该能够：

- 读懂 `CodeBlockWidget.toDOM` 如何从零拼出一个带工具栏的代码块 DOM，以及它如何把代码**按行**切分、逐行套上 `data-line-index`；
- 区分 `auto` 与 `toggle` 两种模式下完全不同的事件处理策略，理解 `ignoreEvent` 与 `stopBubbleForBody` 各自拦在哪一层、为什么一个要 `preventDefault` 而另一个不能；
- 看懂 `measureClickOffset` 如何用 Canvas 的 `measureText` 配合二分查找，把鼠标点击的屏幕横坐标精确换算成源码中的字符偏移，以及在 Canvas 不可用时如何退化为按字号估算。

一句话：本讲解决「渲染出来的代码块，长什么样、按钮怎么响应、点一下怎么把光标放到正确字符上」这三件事。

## 2. 前置知识

本讲是专家级内容，假定你已经掌握以下概念（来自前置讲义，这里只做最简提示）：

- **WidgetType 三件套**（u3-l1）：`toDOM` 造 DOM、`eq` 决定能否复用旧 DOM（重建闸门）、`ignoreEvent` 决定事件是否交给 CodeMirror 默认处理。
- **Decoration.replace + block**（u3-l1、u5-l2）：`auto` / `toggle` 模式下，整个代码块区间被 `Decoration.replace({ widget, block: true })` 替换；`block: true` 改变行高，所以必须走 `StateField`。
- **三种 interaction 模式**（u5-l2）：`auto` 跟光标走、整块替换为 widget、走 HTML 高亮；`toggle` 跟显式按钮走、整块替换；`inline` 只换围栏行、正文留在 contentDOM 可原生编辑、走 HAST 高亮。本讲的 `CodeBlockWidget` 服务于前两种（`auto` 与 `toggle`），`inline` 模式不构造本 Widget。
- **DOM 事件三阶段**（Web 基础）：捕获（capture）→ 目标（target）→ 冒泡（bubble）。`addEventListener(type, fn, true)` 的第三参数为 `true` 表示在捕获阶段触发。`stopPropagation()` 阻止事件继续传播；`preventDefault()` 阻止浏览器默认行为（如原生选区）。
- **Canvas 2D 上下文**（Web 基础）：`canvas.getContext('2d')` 拿到的 `ctx` 有 `measureText(str).width`，可以量出某段文本在指定字体下的像素宽度，无需把文本真正画到页面上。

> 名词提示：「冒泡控制」在本讲里有两层含义——一层是浏览器 DOM 事件的 `stopPropagation`，一层是 CodeMirror 特有的 `ignoreEvent`。两者是不同的拦截机制，后面会反复对比，请留意区分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/widgets/codeBlockWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts) | 本讲主角。定义 `CodeBlockWidget`（渲染态）、`CodeBlockSourceToggleWidget`（源码态的「Code」按钮）、`CodeBlockData` 接口，以及点击定位、复制等全部实现。 |
| [src/widgets/\_\_tests\_\_/codeBlockWidget.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts) | 单元测试。覆盖渲染结构、复制（含 `execCommand` 回退）、`eq` 相等性、`ignoreEvent`、`data-line-index`、冒泡阻断等行为，是验证理解的最佳参照。 |
| [src/plugins/codeBlock.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts) | 调用方。`buildCodeBlockDecorations` 在渲染态构造 `createCodeBlockWidget({...})` 并包进 `Decoration.replace({ widget, block: true })`。本讲只需理解它「喂给 widget 什么数据」。 |
| [src/plugins/codeBlockEffects.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlockEffects.ts) | 定义 `setCodeBlockSourceMode` 这个 `StateEffect`。widget 里的「MD」按钮按下后派发的就是它。 |
| [src/theme/default.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts) | 给 widget 的各 CSS 类提供样式。关键是 `.cm-codeblock-line` 的 `display:block / padding:0 16px / fontSize:16px`——它既是「按行渲染」的视觉基础，也是 `measureClickOffset` 读取的尺寸来源。 |

## 4. 核心概念与源码讲解

### 4.1 toDOM 工具栏与按行渲染

#### 4.1.1 概念说明

`CodeBlockWidget` 是一个继承自 `WidgetType` 的类，它的全部职责就是：拿一份 `CodeBlockData`，在 `toDOM` 里造出一棵 DOM 树交给 CodeMirror 显示。

为什么要把代码「按行」渲染，而不是把高亮后的 HTML 一整坨塞进去？因为本讲后面的「点击定位」需要知道用户点的是**第几行**。如果只有一整块 HTML，点击坐标很难映射回源码位置；但如果每行是一个独立的 `<span>`，并打上 `data-line-index`，就能先定位行、再在行内定位列。

`CodeBlockData` 是输入契约，规定了 widget 需要哪些信息：

[src/widgets/codeBlockWidget.ts:L14-L34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L14-L34) —— 定义了 `code`（源码）、`language`（语言）、`showLineNumbers / showCopyButton / showSourceToggle`（三个开关）、以及四个位置字段。其中 `lineStarts` 是「每行起始位置在文档中的绝对偏移」，是后面点击换算的钥匙；`from / to` 是整个代码块（含围栏）的区间。

调用方 `codeBlock.ts` 是这样填充 `lineStarts` 的：从 `CodeText` 子节点的起点开始，遇到 `\n` 就把下一个位置记为一行的起点——

[src/plugins/codeBlock.ts:L452-L461](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L452-L461) —— 逐字符扫描 `code`，在换行处 push 行起点。这样 `lineStarts[i]` 就是第 `i` 行第一个字符的文档位置，长度恰等于代码行数。

#### 4.1.2 核心流程

`toDOM` 的整体构造顺序如下（伪代码）：

```text
toDOM(view):
  1. 创建 container.cm-codeblock-widget，写入 dataset(from / to / lineStarts)
  2. 根据 interaction 模式，挂载不同的事件监听（见 4.2，此处先跳过）
  3. 创建 toolbar.cm-codeblock-actions：
       - 若 showSourceToggle：加「MD」按钮 → 派发 setCodeBlockSourceMode(true)
       - 若 showCopyButton：加「Copy」按钮 → 写剪贴板（见 4.1.3）
     toolbar 有子元素才挂到 container
  4. 创建 pre > code，构建「围栏行 + 代码行 + 围栏行」的 HTML 字符串：
       a. highlightCode(code, lang) 得到整块高亮 HTML
       b. 按 \n 切分；若行数与原文不匹配，则逐行单独高亮
       c. openFence  → data-line-index="-1"
          每个代码行 → data-line-index=0,1,2...
          closeFence → data-line-index="-2"
       d. innerHTML = 所有 span 直接 join('')
  5. 返回 container
```

注意第 4.d 步：所有 `<span class="cm-codeblock-line">` 之间**没有换行符也没有 `<br>`**，直接 `join('')`。那为什么视觉上还能换行？因为主题里把 `.cm-codeblock-line` 设成了 `display:block`：

[src/theme/default.ts:L396-L405](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L396-L405) —— `display:'block'` 让每个 span 独占一行；`padding:'0 16px'`、`fontSize:'16px'`、`lineHeight:'1.75'` 共同决定了行内布局。围栏行额外套 `.cm-codeblock-fence` 变灰。

#### 4.1.3 源码精读

**容器与数据标记。** `toDOM` 一开头就建容器，并把位置信息写进 `dataset`——

[src/widgets/codeBlockWidget.ts:L74-L82](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L74-L82) —— `dataset.from / dataset.to / dataset.lineStarts` 把文档位置「随身携带」在 DOM 上。这样即便脱离 widget 实例，外部代码（或调试）也能从 DOM 读回位置。`showLineNumbers` 为真时追加 `cm-codeblock-line-numbers` 类。

**工具栏：MD 切换按钮。** 仅在 `showSourceToggle`（即 `toggle` 模式）下出现。按下后把光标移到代码块起点，并派发 `setCodeBlockSourceMode.of({ showSource: true })`，通知 `codeBlockSourceModeField` 把这块切到源码态——

[src/widgets/codeBlockWidget.ts:L160-L182](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L160-L182) —— 按钮文案是 `MD`，点击先 `preventDefault + stopPropagation`（避免触发代码区点击逻辑），再 `view.dispatch` 同时设置 `selection`、`effects`、`scrollIntoView`。这正是 u5-l2 讲的「toggle 模式由显式按钮驱动源码态」的触发点。

**工具栏：复制按钮。** 它背后是一段带双重回退的复制逻辑。点击后调 `this.copyText(code)`，成功显示 `Copied!` 两秒、失败显示 `Failed`——

[src/widgets/codeBlockWidget.ts:L185-L217](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L185-L217) —— 注意整段被 `try/catch` 包住，任何异常都落到 `Failed` 文案，绝不抛错（呼应 u3-l1 讲过的「toDOM 及其触发的逻辑不能抛错」原则）。

复制本身优先用 `navigator.clipboard.writeText`；若该 API 不可用或被拒，回退到 `execCommand('copy')`——

[src/widgets/codeBlockWidget.ts:L363-L418](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L363-L418) —— `copyText` 先试异步剪贴板 API，catch 后转 `fallbackCopyWithExecCommand`；后者创建一个屏外 `<textarea>`、临时保存并恢复原有选区（`previousRanges`）、调 `document.execCommand('copy')`。测试里专门验证了这条回退路径（见 4.1.4）。

**代码区：按行高亮渲染。** 这是本模块的核心。先对整块代码做一次高亮，再尝试按 `\n` 切分；如果切出来的行数和原文行数对不上（高亮器可能改动换行），就退化成**逐行单独高亮**——

[src/widgets/codeBlockWidget.ts:L234-L247](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L234-L247) —— 这种「先整体、不匹配再逐行」的策略，既享受了整体高亮（跨行语法如注释、模板字符串可能正确）的好处，又保证 `data-line-index` 与原文行一一对应（点击定位的前提）。逐行兜底里还做了 `|| this.escapeHtml(line) || ' '`，保证空行也产出可点击的占位。

**行索引的编码。** 围栏行用负数哨兵值，避免和正文行号冲突——

[src/widgets/codeBlockWidget.ts:L252-L270](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L252-L270) —— `data-line-index="-1"` 表示起始围栏（点击应跳到块首 `from`），`"-2"` 表示结束围栏（跳到块尾 `to`），`0..n` 才是真正的代码行。这套编码会被 4.2 的点击处理器读取。最后 `codeEl.innerHTML = allLines.join('')` 一次性写入（用 `innerHTML` 而非逐个 `appendChild`，性能更好）。

**eq：重建闸门。** 对照 u3-l1 的心法「eq 比较的字段集合要等于决定 toDOM 输出的字段集合」——

[src/widgets/codeBlockWidget.ts:L47-L56](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L47-L56) —— 比较了 `code / language / showLineNumbers / showCopyButton / showSourceToggle / from`。这里有个值得玩味的点：**没有比较 `to` 和 `lineStarts`**。这是有意为之——只要 `code`（内容）和 `from`（起点）没变，`to` 与 `lineStarts` 在文档后续编辑时即便被调用方重新计算，也总能由 `code` + `from` 推导出来，因此不必让 widget 重建。这种「比较关键字段、省略派生字段」的做法减少了无谓重建，但前提是调用方保证派生字段的一致性（读源码时可以思考：若调用方传了不一致的 `lineStarts` 会怎样？答案见练习）。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试，验证「按行渲染」「复制回退」「eq」三件事，并亲自用断言描述一个多行代码块的 DOM 结构。

**操作步骤**：

1. 打开 [src/widgets/\_\_tests\_\_/codeBlockWidget.test.ts:L114-L128](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L114-L128)。这条用例输入 `code:'line1\nline2\nline3'`，断言 `.cm-codeblock-line` 一共有 **5** 个（1 起始围栏 + 3 代码行 + 1 结束围栏）。
2. 再看 [src/widgets/\_\_tests\_\_/codeBlockWidget.test.ts:L349-L370](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L349-L370)，它逐一断言 5 个 span 的 `data-line-index` 依次是 `-1 / 0 / 1 / 2 / -2`。
3. 看复制回退用例 [src/widgets/\_\_tests\_\_/codeBlockWidget.test.ts:L206-L244](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L206-L244)：它先把 `navigator.clipboard` 置空、给 `document.execCommand` 打桩返回 `true`，点击后断言按钮变 `Copied!` 且 `execCommand` 被以 `'copy'` 调用。
4. 在本地仓库根目录运行：`npm test -- codeBlockWidget`（或 `npx vitest run src/widgets/__tests__/codeBlockWidget.test.ts`）。

**需要观察的现象**：

- 「should render line numbers when enabled」用例通过，`lines.length === 5` 成立——印证「围栏 + 正文 + 围栏」的三段式结构。
- 复制回退用例通过，证明 `execCommand` 分支被走到。

**预期结果**：全部用例绿色通过。如果 `highlightCode` 因 lowlight 未安装而返回纯文本，5 行结构依然成立（因为行数切分逻辑不依赖高亮是否成功）。

> 说明：上述命令的真实运行结果待本地验证（取决于 lowlight 是否安装、Node 版本等）。但用例的断言逻辑可直接从源码读出，不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：如果用户在一个 toggle 模式的代码块里编辑了内容（`code` 变了但 `from` 没变），`eq` 会返回什么？为什么这是对的？

> **答案**：返回 `false`，因为 `other.data.code !== this.data.code`，触发 widget 重建，`toDOM` 重新高亮、重新切行。这正是期望行为——内容变了就得重画。

**练习 2**：假设调用方传入了与 `code` 不一致的 `lineStarts`（比如少了一项），`eq` 能发现吗？会带来什么后果？

> **答案**：`eq` **不会**比较 `lineStarts`，所以它发现不了。后果是 widget 不重建、沿用旧的 `lineStarts`，导致点击第 N 行时光标落点错位（4.3 讲的点击换算会用错起点）。教训：调用方必须保证 `lineStarts` 与 `code` 同步——这正是 [src/plugins/codeBlock.ts:L452-L461](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L452-L461) 始终从 `code` 现场计算 `lineStarts` 的原因。

---

### 4.2 事件冒泡控制：ignoreEvent 与 stopBubbleForBody

#### 4.2.1 概念说明

代码块 widget 内部有两类截然不同的交互诉求，取决于 `interaction` 模式：

- **`auto` 模式**（`showSourceToggle === false`）：整个渲染块是「只读展示 + 点击跳转」。用户点一下代码，光标应立刻落到源码对应字符，进入编辑。此时我们**不希望**浏览器在 widget 里启动原生文本选区，也不希望 CodeMirror 把这次 mousedown 当成普通编辑区的选区拖拽。
- **`toggle` 模式**（`showSourceToggle === true`）：渲染块允许用户**用鼠标原生选区**框选代码文本（复制片段），同时按钮要能点。此时我们**希望**保留浏览器原生选区，但要挡住 CodeMirror 的事件处理，避免它抢走选区或误判拖拽。

这两种诉求几乎相反，所以 widget 用了两套不同的拦截机制。这里要先厘清两个容易混淆的拦截点：

| 拦截机制 | 作用层 | 语义 |
| --- | --- | --- |
| `ignoreEvent(event)` | CodeMirror 层 | 返回 `true` → CodeMirror 跳过对该事件的默认处理；返回 `false` → CodeMirror 正常处理。是 `WidgetType` 的方法。 |
| `stopBubbleForBody` / widget 自己的 `addEventListener` | 浏览器 DOM 层 | 在 widget 自身的 DOM 上 `stopPropagation`（阻止冒泡到 CodeMirror 的 contentDOM）/ `preventDefault`（阻止浏览器默认行为）。 |

#### 4.2.2 核心流程

```text
模式判定：enableClickToEdit = !showSourceToggle

若 toggle 模式（showSourceToggle=true）：
  在 container 上（捕获阶段）挂 stopBubbleForBody，监听 mousedown/mouseup/click：
    - 事件目标若命中 .cm-codeblock-copy 或 .cm-codeblock-toggle 按钮 → 直接 return（放行给按钮自己的 handler）
    - 否则 event.stopPropagation()     ← 阻止冒泡到 CodeMirror，但不 preventDefault，保留原生选区

若 auto 模式（enableClickToEdit && view）：
  在 container 上（捕获阶段）挂 mousedown 处理器：
    - 命中按钮 → return（理论上 auto 无按钮，防御性判断）
    - event.stopPropagation(); event.preventDefault();   ← 既挡 CodeMirror，又挡原生选区
    - 找到点击所在行 lineEl（target.closest('.cm-codeblock-line')）
    - 读 data-line-index：
        -1   → targetPos = from（块首围栏）
        -2   → targetPos = to（块尾围栏）
        0..n → targetPos = lineStarts[index] + measureClickOffset(...)（行首 + 列偏移）
    - view.dispatch({ selection:{anchor:targetPos}, scrollIntoView:true }); view.focus()

ignoreEvent(event)（CodeMirror 调用）：
  toggle 模式 → 所有 mouse* / click 类事件返回 true（忽略）
  否则 → mousedown 一律返回 true（忽略），其它事件返回 false
```

一句话总结两条路线的取舍：**auto 用 `preventDefault` 关掉原生选区、自己接管点击；toggle 只用 `stopPropagation` 挡住 CodeMirror、把选区留给浏览器。**

#### 4.2.3 源码精读

**toggle 模式的冒泡阻断器。** 注意它只 `stopPropagation`、**不** `preventDefault`——

[src/widgets/codeBlockWidget.ts:L85-L98](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L85-L98) —— 关键注释说明了意图：「Prevent editor-level pointer handlers from firing while keeping native text selection intact」。也就是说：冒泡被截断，CodeMirror（作为祖先节点 contentDOM 的监听者）收不到事件，因此不会去抢占选区；但默认行为没被取消，浏览器照常在 `<pre>` 里启动文本选区。按钮命中时提前 `return`，让事件继续冒泡，使按钮自身挂在 toolbar 上的 click handler 正常触发。

**auto 模式的点击跳转处理器。** 这是本模块和 4.3 的衔接点——

[src/widgets/codeBlockWidget.ts:L100-L155](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L100-L155) —— 第三参数 `true` 表示**捕获阶段**触发，确保在冒泡到 CodeMirror 之前就拦下。`stopPropagation + preventDefault` 双管齐下：前者挡 CodeMirror、后者挡原生选区。随后用 4.1 编码的 `data-line-index` 分流：负数哨兵跳到围栏端点，非负数跳到「行首 + 列偏移」，列偏移由下一节的 `measureClickOffset` 计算。最后 `dispatch` 设置选区并 `focus()`，让编辑器接管。

**ignoreEvent。** 这是 CodeMirror 的拦截入口——

[src/widgets/codeBlockWidget.ts:L426-L450](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L426-L450) —— toggle 模式下，`mousedown/mouseup/mousemove/click/dblclick/contextmenu` 一律返回 `true`（让 CodeMirror 别管，把控制权完全交给 widget 自己的 `stopBubbleForBody` 与浏览器原生）。非 toggle 模式下，`mousedown` 一律返回 `true`（与 auto 模式自己接管 mousedown 配合），其它事件返回 `false`。

> 一个诚实的细节：源码注释（L424）写「We handle clicks ourselves in codeBlock.ts with domEventHandlers」，但实际点击处理是写在 **widget 的 `toDOM` 里**（L104-L154 的 `addEventListener`），`codeBlock.ts` 中并没有名为 `domEventHandlers` 的处理逻辑。读源码时不要被这条注释误导——真正的点击接管在 widget 自身。这属于「名不副实的注释」，与 u2-l2 提醒过的「读测试/注释要警惕名不副实」同理。

**源码态的「Code」按钮 widget。** 顺带一提：当 toggle 模式切到源码态后，显示的是另一个 widget `CodeBlockSourceToggleWidget`，它的按钮文案是 `Code`，点击派发 `setCodeBlockSourceMode.of({ showSource: false })` 切回渲染态——

[src/widgets/codeBlockWidget.ts:L468-L498](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L468-L498) —— 它的 `ignoreEvent` 固定返回 `true`（L496-L498），完全自管事件。`MD`（渲染态→源码态）与 `Code`（源码态→渲染态）两个按钮分布在两个 widget 上，靠 `setCodeBlockSourceMode` 这一 `StateEffect` 互通，状态本身存在 `codeBlockSourceModeField` 里——这正是 u5-l2 讲的「消息 + 状态」搭档。

#### 4.2.4 代码实践

**实践目标**：用现有测试证明「toggle 模式下 mousedown 不被 preventDefault，但会停止冒泡」。

**操作步骤**：

1. 读 [src/widgets/\_\_tests\_\_/codeBlockWidget.test.ts:L439-L468](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L439-L468)「should stop mousedown bubbling from code lines in toggle mode」：它把 widget 挂到一个父 `div`，父级挂 `mousedown` 监听，然后在代码行上 `dispatchEvent` 一个可冒泡的 mousedown，断言父级监听**未被调用**——证明 `stopPropagation` 生效。
2. 对照 [src/widgets/\_\_tests\_\_/codeBlockWidget.test.ts:L374-L398](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L374-L398)「should allow text selection mousedown in toggle mode」：同样 dispatch mousedown，断言 `event.defaultPrevented === false`——证明 `preventDefault` **没有**被调用，原生选区得以保留。
3. 运行 `npx vitest run src/widgets/__tests__/codeBlockWidget.test.ts -t "toggle mode"`。

**需要观察的现象**：两个看似「相反」的断言同时成立——冒泡被阻断，但默认行为被保留。这正是 toggle 模式「挡 CodeMirror、留浏览器」的设计精髓。

**预期结果**：相关用例全部通过。

> 命令运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `stopBubbleForBody` 不能像 auto 模式那样也调 `preventDefault`？

> **答案**：因为 toggle 模式要让用户能用鼠标**框选代码文本**。`preventDefault` 会取消浏览器的原生选区启动，导致拖不动选区。所以 toggle 只能 `stopPropagation`（挡 CodeMirror），不能 `preventDefault`（留给浏览器）。auto 模式则相反——它不需要选区、要的是「点击即跳转」，所以两者都调。

**练习 2**：在 auto 模式下，`ignoreEvent` 对 `mousedown` 返回 `true`，同时 widget 的捕获监听又调了 `stopPropagation`。这两个机制是不是冗余？

> **答案**：有冗余但属于「双保险」。`stopPropagation` 在 DOM 层阻止事件冒泡到 CodeMirror 的 contentDOM；`ignoreEvent` 返回 `true` 是 CodeMirror 自己的跳过逻辑。即便其中一个因为事件阶段的细微差异（如 CodeMirror 在捕获阶段就注册了处理）没能完全拦住，另一个仍能兜底。生产级编辑器里这种「双重拦截」很常见，代价是代码稍显绕。

---

### 4.3 Canvas 点击定位：measureClickOffset

#### 4.3.1 概念说明

auto 模式下，用户点击代码块，光标要落到**源码中对应的字符**上。`toDOM` 的点击处理器已经能定位到「第几行」（靠 `data-line-index` + `lineStarts`），但同一行里点在最左边和最右边，光标位置显然不同。`measureClickOffset` 就是用来算「这一行里，点击坐标对应第几个字符」的。

为什么需要这么精确？因为最终的目标文档位置是：

\[
\text{targetPos} = \text{lineStarts}[\text{lineIndex}] + \text{charOffset}
\]

`charOffset` 差一个字符，光标就错一格。若只是粗暴地把点击都映射到行首，用户体验会很差——每次点击代码都得再按方向键移动。

思路很自然：在 Canvas 上用和代码区相同的字体「量」出文本宽度，找出「宽度最接近、又不超过点击横坐标」的那个前缀长度，就是字符偏移。等价于在一个单调递增的宽度数组上做查找——天然适合**二分查找**。

> 名词提示：`measureText` 是 Canvas 2D 上下文的方法，传入字符串返回其渲染像素宽度，**不实际绘制**，因此开销很小、可以反复调用。

#### 4.3.2 核心流程

`measureClickOffset(lineEl, clientX, sourceText)` 的算法：

```text
1. rect = lineEl.getBoundingClientRect()
   clickX = clientX - rect.left          # 点击相对行元素左上角的横坐标
   paddingLeft = parseFloat(computedStyle.paddingLeft)   # 主题里是 16px
   textClickX = clickX - paddingLeft     # 减掉内边距，得到相对「文本起点」的坐标
   若 textClickX <= 0 → 返回 0           # 点在文本左侧，算第 0 个字符

2. 取 Canvas 2D ctx；ctx.font = `${fontSize} ${fontFamily}`   # 与代码区同字体
   若 ctx 不存在（Canvas 不可用）→ 退化：
       charWidth = fontSize * 0.6        # 按经验字号估算单字宽
       charOffset = floor(textClickX / charWidth)
       return min(charOffset, sourceText.length)

3. 二分查找最大的前缀长度 left，使 measureText(text[0..left]).width <= textClickX：
       left=0, right=text.length
       while left < right:
         mid = floor((left + right + 1) / 2)      # 取上中位，避免死循环
         if measureText(text[0..mid]).width <= textClickX: left = mid
         else: right = mid - 1

4. 四舍五入到最近字符（打破平局）：
       若 left < text.length:
         currentWidth = measureText(text[0..left]).width
         nextWidth    = measureText(text[0..left+1]).width
         midPoint = (currentWidth + nextWidth) / 2
         若 textClickX > midPoint → left++      # 离下一个字符更近

5. return min(left, sourceText.length)            # 不越过源码末尾
```

数学上，设字符前缀宽度函数 \( W(k) = \text{measureText}(t[0..k]).\text{width} \)，它关于 \(k\) 单调不减。二分求的是：

\[
\text{left} = \max\{\, k \mid W(k) \le \text{textClickX}\,\}
\]

随后用相邻中点 \( (W(\text{left}) + W(\text{left}+1))/2 \) 决定落在 `left` 还是 `left+1`，本质是把字符边界对齐到两个前缀宽度的中点，让点击落在哪一侧就归哪个字符。

#### 4.3.3 源码精读

**坐标与内边距换算。** 先把屏幕坐标转换成「相对文本起点」的坐标——

[src/widgets/codeBlockWidget.ts:L303-L315](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L303-L315) —— `getBoundingClientRect` 拿到行元素在视口的位置，`clientX - rect.left` 得到相对左上角的横坐标；再减 `paddingLeft`（来自主题 `.cm-codeblock-line` 的 `padding:0 16px`）。`textClickX <= 0` 直接返回 0，处理点击落在文本左侧（如行号区、内边距）的情况。

**Canvas 不可用时的退化。** 这是本讲实践任务的重点——

[src/widgets/codeBlockWidget.ts:L318-L327](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L318-L327) —— 新建一个屏外 `<canvas>` 取 `getContext('2d')`；若返回 `null`（极少数环境不支持 2D 上下文），退化用 `fontSize * 0.6` 估算单字符宽度，`floor(textClickX / charWidth)` 得到字符偏移，并用 `min(..., sourceText.length)` 钳制不越界。`0.6` 是等宽/比例字体下「字宽≈0.6 倍字号」的经验值——不精确但能保证点击大致落到正确区域。注意退化分支里 `fontSize` 同样取自 `computedStyle.fontSize`（主题里是 `16px`），所以默认 `charWidth ≈ 9.6px`。

**二分查找主体。** 用源码文本 `sourceText` 量宽，保证偏移与源码字符对齐——

[src/widgets/codeBlockWidget.ts:L329-L347](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L329-L347) —— `ctx.font` 设成与代码区相同的 `fontSize + fontFamily`，确保量出的宽度和屏幕实际一致。二分用 `mid = floor((left + right + 1) / 2)` 的**上中位**写法：当 `width <= textClickX` 时 `left = mid`（保留 mid 作为候选），否则 `right = mid - 1`。上中位是为了在 `left` 与 `right` 相邻时避免死循环（若用下中位 `floor((l+r)/2)`，`left=mid` 会让 `left` 不前进）。循环结束时 `left` 即「最大的、宽度不超过点击点的前缀长度」。

**打破平局。** 决定落在当前字符还是下一个字符——

[src/widgets/codeBlockWidget.ts:L349-L360](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L349-L360) —— 取 `left` 与 `left+1` 两个前缀宽度的中点 `midPoint`，若点击横坐标超过中点，说明更靠近下一个字符，`left++`。最后 `min(left, sourceText.length)` 防止越过源码末尾（点击远超行尾时不会越界）。

**调用点：把行 + 列拼成文档位置。** 回到点击处理器看它怎么用——

[src/widgets/codeBlockWidget.ts:L131-L144](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L131-L144) —— `targetPos = lineStarts[lineIndex]`（行首文档位置）`+ measureClickOffset(lineEl, event.clientX, code.split('\n')[lineIndex])`（列偏移）。这里第三个参数特意传「该行源码文本」而非高亮后文本，是因为字符偏移要对应**源码**位置（高亮 HTML 多了 `<span>` 标签，字符数会对不上）。这就是为什么前面 4.1 要保证 `data-line-index` 与源码行一一对应——只有行号对了，`code.split('\n')[lineIndex]` 取出的才是正确的源码行。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `measureClickOffset` 的 Canvas 退化分支，并解释「为什么必须精确映射回源码 pos」。

**操作步骤（源码阅读型 + 本地实验）**：

1. **读懂退化逻辑**。确认 [src/widgets/codeBlockWidget.ts:L321-L327](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L321-L327) 在 `!ctx` 时走 `fontSize * 0.6` 估算。由于主题里 `.cm-codeblock-line` 的 `fontSize` 是 `16px`（见 [src/theme/default.ts:L396-L402](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf761af40ccf764653624c923f1/src/theme/default.ts#L396-L402)），估算单字宽 ≈ `9.6px`。若点击在距文本起点 `48px` 处，退化分支返回 `floor(48 / 9.6) = 5`。
2. **本地实验（可选）**：写一段临时脚本，构造一个 `CodeBlockWidget`，用 `Object.defineProperty` 把 `document.createElement('canvas')` 返回对象的 `getContext` 桩成返回 `null`，再通过反射调用私有方法 `measureClickOffset`（TypeScript 里可用 `(widget as any).measureClickOffset(...)`），断言退化分支返回的是 `floor(textClickX / 9.6)`。实验后务必删除临时脚本，**不要改动库源码**。
3. **对比两种算法的精度**：在同一行源码 `'const x = 1;'`（12 字符）上，假设各字符真实宽度（由 Canvas 量出）并不都是 9.6px（比如空格窄、`w` 宽），列出「Canvas 精确值」与「退化估算值」的差异，体会退化分支为何只是兜底。

**需要观察的现象**：

- Canvas 可用时，点击同一行的不同横坐标，`charOffset` 随宽度单调变化，且与肉眼所见字符对齐；
- Canvas 不可用时，退化分支按等宽假设估算，在比例字体或宽字符（如中文、`w/m`）处会出现 1~2 字符偏差。

**预期结果**：Canvas 路径精确；退化路径在等宽英文字符下近似可用，在异宽字符下有可见偏差——这正是「优先 Canvas、退化兜底」的设计动因。

**为什么必须精确映射回源码 pos**（实践任务的核心问题）：

1. **光标体验**：`targetPos = lineStarts[i] + charOffset` 直接作为 `selection.anchor`。`charOffset` 偏差 1，光标就错一格，用户每次点击都要二次修正，体验崩坏。
2. **字符对齐而非像素对齐**：高亮后的 DOM 含 `<span class="hljs-...">` 标签，其字符数与源码不同；只有用**源码文本**（`code.split('\n')[lineIndex]`）量宽，`charOffset` 才是源码偏移，才能正确加到 `lineStarts[i]` 上。若误用高亮文本，偏移会包含标签字符，光标位置全错。
3. **边界安全**：`min(left, sourceText.length)` 保证点击行尾之外时，光标停在行末而非越界。

> 上述本地实验的运行结果待本地验证；退化分支的算法可直接从源码读出，不依赖运行。

#### 4.3.5 小练习与答案

**练习 1**：二分查找为什么用 `mid = floor((left + right + 1) / 2)`（上中位）而不是 `floor((left + right) / 2)`（下中位）？

> **答案**：因为收缩方向是 `left = mid`（而非 `left = mid + 1`）。若用下中位，当 `left` 与 `right` 相邻时 `mid = left`，命中 `width <= textClickX` 后 `left = mid = left` 不变，陷入死循环。改用上中位让 `mid` 偏向 `right`，每轮都能真正缩小范围。这是「`left=mid` 型二分必须配上中位」的标准写法。

**练习 2**：`measureClickOffset` 为什么传入 `sourceText`（源码行）而不是直接用 `lineEl.textContent`（渲染后的文本）？

> **答案**：渲染后的 `textContent` 可能与源码不完全一致（高亮虽然不改变纯文本，但若将来 widget 在渲染时做了诸如空白替换、制表符展开等处理，两者就会不同）。用源码行 `code.split('\n')[lineIndex]` 量宽，能保证 `charOffset` 严格对应源码字符索引，从而 `lineStarts[i] + charOffset` 指向正确的文档位置。

**练习 3**：若浏览器既不支持 Canvas 2D、`fontSize` 又取不到（`parseFloat` 返回 `NaN`），退化分支会怎样？

> **答案**：`parseFloat(style.fontSize) || 16` 会兜底成 `16`（见 [L323](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L323)），`charWidth = 9.6`，仍能给出一个估算值。这是多层兜底：Canvas 失败 → 字号兜底 → 字号失败 → 默认 16。整个 `measureClickOffset` 永远不会抛错，最差也返回一个粗略但有效的偏移。

---

## 5. 综合实践

**任务**：在 `auto` 模式下，端到端追踪一次「用户点击代码块第 2 行第 3 个字符」的完整链路，并把每一步的输入/输出写成一张表。

请按以下顺序填写（参考本讲三个模块的源码）：

| 步骤 | 触发 | 读取/计算 | 产出 |
| --- | --- | --- | --- |
| 1. 浏览器派发 | `mousedown` 于代码行 span | — | 事件进入 widget container 的捕获监听 |
| 2. 拦截 | [L104-L116](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L104-L116) | 命中按钮? 否 | `stopPropagation + preventDefault` |
| 3. 定位行 | [L118-L135](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L118-L135) | `target.closest('.cm-codeblock-line')` → `data-line-index=1` | `targetPos = lineStarts[1]` |
| 4. 定位列 | [L138-L143](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L138-L143) | `measureClickOffset(lineEl, clientX, code.split('\n')[1])` | `charOffset`（如 `2`，0 基）|
| 5. 派发 | [L147-L151](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L147-L151) | `targetPos = lineStarts[1] + 2` | `view.dispatch({selection:{anchor:targetPos}})` + `focus()` |

完成后，回答两个问题：

1. 如果用户点的不是代码行，而是起始围栏（```` ```js ````），步骤 3 会读出哪个 `data-line-index`？`targetPos` 会是多少？（答案：`-1` → `widgetData.from`）
2. 如果此时把 `interaction` 从 `auto` 换成 `toggle`，步骤 2 的拦截行为会发生什么变化？（答案：换成 `stopBubbleForBody`，只 `stopPropagation` 不 `preventDefault`，且不再 `dispatch` 选区，而是保留原生选区让用户框选）

> 这个练习把三个模块（按行渲染提供 `data-line-index`、事件冒泡控制决定拦截方式、Canvas 点击定位算列偏移）串成一条完整数据流。能不查源码填完这张表，说明你真正理解了 `CodeBlockWidget`。

## 6. 本讲小结

- `CodeBlockWidget.toDOM` 拼出「容器 + 工具栏 + `pre>code`」三段结构，代码被**按行**切成带 `data-line-index` 的 `<span>`（围栏 `-1/-2`、正文 `0..n`），靠主题的 `display:block` 视觉换行；这是点击定位的前提。
- 工具栏按需出现：`toggle` 模式有「MD」按钮（派发 `setCodeBlockSourceMode(true)` 切源码态），复制按钮走 `navigator.clipboard` 优先、`execCommand` 回退的双重路径，全程 try/catch 不抛错。
- 事件处理按模式分叉：`auto` 用捕获监听 `stopPropagation + preventDefault` 接管点击并跳转光标；`toggle` 用 `stopBubbleForBody` 只 `stopPropagation` 不 `preventDefault`，挡住 CodeMirror 同时保留浏览器原生选区。`ignoreEvent` 作为 CodeMirror 层的兜底拦截与之配合。
- `eq` 只比较 `code/language/show*/from`，刻意省略 `to/lineStarts`，依赖调用方保证派生字段一致；这是「重建闸门」与「减少无谓重画」的平衡。
- `measureClickOffset` 用 Canvas `measureText` + 上中位二分查找精确换算列偏移，并以相邻前缀宽度中点打破平局；Canvas 不可用时退化为 `fontSize*0.6` 等宽估算。目标文档位置 = `lineStarts[i] + charOffset`，必须用源码文本量宽才能对齐字符。
- 源码里「click 由 codeBlock.ts 的 domEventHandlers 处理」的注释与实际不符——真正的点击接管在 widget 自身的 `toDOM` 里，读源码时要警惕这类名不副实的注释。

## 7. 下一步学习建议

- 下一讲 **u5-l4（inline 模式：保留 contentDOM 的可编辑高亮）** 会讲第三种 interaction 模式。它**不构造本讲的 `CodeBlockWidget`**，而是用 `highlightCodeHast` 产出 HAST 树、再经 `hastToMarkDecorations` 转成 `Decoration.mark` 高亮，并用 `codeBlockSelectionPlugin` 处理选区。学完后你可以对比三种模式各自动用的渲染管线（HTML/innerHTML vs HAST/mark vs mark+CSS）。
- 建议同步重读 [src/plugins/codeBlock.ts:L440-L511](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L440-L511)，把「调用方如何填充 `CodeBlockData`」与「widget 如何消费它」对照起来看，理解契约的上下游。
- 若对事件机制感兴趣，可结合 CodeMirror 官方文档阅读 `WidgetType.ignoreEvent` 的语义，并对照本讲的 `stopPropagation` 实践，体会「CodeMirror 拦截 vs DOM 冒泡」两套体系的边界。
