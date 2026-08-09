# 快速集成：搭建第一个 Live Preview 编辑器

## 1. 本讲目标

学完本讲，你应该能够：

- 用 `EditorState.create` + `new EditorView` 把一个 CodeMirror 6 编辑器挂载到页面上；
- 写出 Live Preview 的最小扩展组合（`livePreviewPlugin` + `markdownStylePlugin` + `editorTheme` + `collapseOnSelectionFacet` + `mouseSelectingField`），并知道每个扩展各管什么；
- 解释为什么**必须**手动监听 `mousedown`/`mouseup` 并派发 `setMouseSelecting`，否则拖拽选区时会闪烁；
- 用 `Compartment` 在 Live（实时预览）与 Source（源码）模式之间动态切换。

本讲是入门单元的最后一篇。前三篇我们分别认识了项目（u1-l1）、读了目录与入口（u1-l2）、跑通了构建与 demo（u1-l3）；本篇把这些串起来，亲手搭出第一个能用的 Live Preview 编辑器。

## 2. 前置知识

动手前，请确认你了解以下几点（不熟也没关系，我会顺带复习）：

- **CodeMirror 6 的两个核心对象**
  - `EditorState`：编辑器的「状态」，是**不可变**的。文档内容、选区、扩展都存在里面。
  - `EditorView`：编辑器的「视图」，负责把 state 渲染成真实 DOM，并接收用户输入。
  - 改文档不是直接改 state，而是通过 `view.dispatch(...)` 派发一个 transaction（事务），生成新 state。
- **扩展（Extension）**：CodeMirror 6 的一切功能（语法、快捷键、主题、装饰）都是扩展，全部塞进 `EditorState.create({ extensions: [...] })`。
- **npm 包的导入**：本库从包名 `codemirror-live-markdown` 统一导出所有扩展与工具（见 u1-l2 讲过的桶文件 `src/index.ts`）。
- **demo 的运行方式**：进入 `demo` 目录执行 `npm install && npm run dev` 启动 Vite（见 u1-l3）。

如果你还没读过前三篇，建议先快速过一遍，尤其是 u1-l2（源码结构）和 u1-l3（构建与 demo）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md) | Quick Start 段落给出「最小可运行」的接入代码，是本讲主线。 |
| [demo/main.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts) | 真实 demo 入口，演示「带全部特性 + 模式切换」的完整集成。 |
| [demo/vite.config.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/vite.config.ts) | demo 的 Vite 配置，把包名别名指向 `src/index.ts`，所以改源码即时生效。 |
| [demo/index.html](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html) | demo 页面骨架，提供挂载点（`<div id="editor-basic">`）与 Live/Source 按钮。 |
| [src/core/mouseSelecting.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts) | 定义 `setMouseSelecting`（Effect）与 `mouseSelectingField`（StateField），是「鼠标拖拽接线」的核心。 |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | Live Preview 的决策函数，会读取 `mouseSelectingField`——这正是接线为何必要的根因。 |

> 说明：本讲聚焦「如何接入」，会少量引用 `mouseSelecting.ts` 与 `shouldShowSource.ts` 来解释「为什么要这样接」。这两个文件的深入剖析在后续 u2 单元。

## 4. 核心概念与源码讲解

### 4.1 用 EditorState + EditorView 挂载编辑器

#### 4.1.1 概念说明

任何 CodeMirror 6 编辑器都由两步构成：

1. 用 `EditorState.create({ doc, extensions })` 创建**状态**——`doc` 是初始文本，`extensions` 是功能清单。
2. 用 `new EditorView({ state, parent })` 创建**视图**，并把它挂到页面上的某个 DOM 节点（`parent`）。

本库没有改变这套流程，它只是往 `extensions` 里塞入若干扩展。所以「接入本库」本质上就是「正确地填写 extensions 数组」。

#### 4.1.2 核心流程

```
准备一个 <div id="editor"></div>
        │
        ▼
EditorState.create({ doc: "...", extensions: [ markdown(), 本库扩展... ] })
        │
        ▼
new EditorView({ state, parent: document.getElementById('editor') })
        │
        ▼
浏览器里出现可编辑的 Live Preview 编辑器
```

#### 4.1.3 源码精读

README 的 Quick Start 就是这套两步法的最简版本：

[demo/README.md:71-84](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L71-L84) —— 用 `new EditorView({ state: EditorState.create({...}), parent })` 一步完成「建状态 + 建视图 + 挂载」。这是接入的最小骨架。

demo 里把这两步拆开了，更清晰：

[demo/main.ts:323-349](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L323-L349) —— `EditorState.create({ doc, extensions: [...] })`，`doc` 是一份拼接好的 Markdown 文本。

[demo/main.ts:351-354](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L351-L354) —— `new EditorView({ state: basicState, parent: document.getElementById('editor-basic') })`。

挂载点在 HTML 里：[demo/index.html:149](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html#L149) —— `<div id="editor-basic" class="editor"></div>`，这就是 `parent` 指向的节点。

脚本入口：[demo/index.html:190](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html#L190) —— `<script type="module" src="/main.ts"></script>`，Vite 把 `main.ts` 当作入口打包。

一个小细节：demo 里写的是 `import ... from 'codemirror-live-markdown'`，但开发时并没有真的去 node_modules 找发布产物，而是被 Vite 别名直接指向源码：

[demo/vite.config.ts:11-21](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/vite.config.ts#L11-L21) —— `'codemirror-live-markdown'` 被解析为 `../src/index.ts`。这就是 u1-l3 讲过的「改源码即时生效」的原因。

#### 4.1.4 代码实践

目标：确认 demo 的挂载关系。

1. 打开 `demo/index.html`，找到 5 个 `id="editor-*"` 的 `<div>`。
2. 打开 `demo/main.ts`，搜索 `getElementById`，看每个 `EditorView` 分别挂到哪个 div。
3. 运行 `cd demo && npm install && npm run dev`，浏览器打开 `http://localhost:5173`。

观察：页面上出现 5 个独立编辑器，每个都对应一个 `editor-*` div。

预期结果：5 个编辑器都能显示各自的初始 Markdown 并可编辑。

> 待本地验证：端口与具体渲染效果请在你本地确认。

#### 4.1.5 小练习与答案

**练习 1**：如果页面上的 `<div id="editor-basic">` 不存在（比如拼写错了），`new EditorView({ parent: document.getElementById('editor-basic') })` 会怎样？
**答案**：`getElementById` 返回 `null`，`new EditorView` 在内部访问 `parent.appendChild` 时会抛错（demo 里用 `!` 非空断言绕过了编译期检查，但运行时仍会报错）。所以挂载点必须真实存在。

**练习 2**：为什么是「先 create state，再 new view」，而不是反过来？
**答案**：视图必须依赖一个状态才能渲染；state 是不可变的数据，view 是它的消费者。CodeMirror 6 的设计就是 state 驱动 view，没有 state 就无法构造 view。

---

### 4.2 扩展组合：从最小核心到可选特性

#### 4.2.1 概念说明

本库奉行「按需引入」（见 u1-l1）。接入时你要区分两类扩展：

- **核心扩展**：构成 Live Preview 体验的最小集合，几乎总要一起加。
- **特性扩展**：math / table / codeBlock / image / link 等，各自独立，想用哪个加哪个，并按需安装对应的可选依赖（`katex`、`lowlight`）或对等依赖（`@lezer/markdown` 的 `Table`）。

#### 4.2.2 核心流程

最小核心组合：

```
extensions = [
  markdown(),                          // ① Markdown 语法解析（@codemirror/lang-markdown，非本库）
  collapseOnSelectionFacet.of(true),   // ② 开启 Live Preview 总开关
  mouseSelectingField,                 // ③ 拖拽状态（4.3 详讲）
  livePreviewPlugin,                   // ④ 标记的显隐动画（隐藏 **、*、# 等）
  markdownStylePlugin,                 // ⑤ 内容样式（标题/粗体/斜体等加 CSS 类）
  editorTheme,                         // ⑥ 默认主题与动画 CSS
]
```

各扩展职责一句话总结：

| 扩展 | 职责 |
|------|------|
| `livePreviewPlugin` | 遍历语法树，给标记节点（`**`、`#`、`>` 等）套 CSS 类，控制显隐动画 |
| `markdownStylePlugin` | 给内容节点（标题、粗体、行内代码等）套样式类（如 `cm-strong`） |
| `editorTheme` | 提供 CSS 变量主题与过渡动画 |
| `collapseOnSelectionFacet` | Live Preview 的总开关（true=开，false=关） |
| `mouseSelectingField` | 记录「是否正在拖拽选区」，供防闪烁用 |

#### 4.2.3 源码精读

README 给出的最小核心组合：[README.md:74-81](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L74-L81) —— 注意 `markdown()` 来自 `@codemirror/lang-markdown`（CodeMirror 官方），不是本库；其余 5 项是本库核心扩展。

demo 的「全特性」组合（以 basicState 为例）：[demo/main.ts:323-349](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L323-L349) —— 核心之外还加了 `mathPlugin`、`blockMathField`、`tableField`、`codeBlockEditorPlugin()`、`imageField()`、`linkPlugin({...})`，并启用 `history()`、`keymap`、`EditorView.lineWrapping` 等编辑体验。

如何按需添加特性，README 有汇总：[README.md:99-129](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L99-L129) —— 每个特性都是独立插件。例如表格需要先 `markdown({ extensions: [Table] })` 让解析器认识 GFM 表格语法，再加 `tableField`。

注意 `markdownStylePlugin` 与 `livePreviewPlugin` 的分工：前者管「内容长什么样」，后者管「标记藏不藏」。它们职责分离，互不依赖（详见后续 u2-l4、u2-l5）。

#### 4.2.4 代码实践

目标：在 demo 里做「减法」，体会每个核心扩展的作用。

1. 打开 `demo/main.ts`，找到 `codeAutoState`：[demo/main.ts:380-394](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L380-L394)，它是一个相对精简的编辑器。
2. 本地实验（因 Vite 别名指向源码，改完即时生效）：
   - 注释掉 `livePreviewPlugin` → `**` 等标记不再隐藏，始终显示（变成纯源码外观）。
   - 注释掉 `markdownStylePlugin` → 文字仍是普通样式，标题不再加粗变大。
   - 注释掉 `editorTheme` → 标记失去平滑显隐动画（可能瞬间出现/消失）。
3. 每次只改一项，刷新页面对比。

预期结果：每移除一个核心扩展，对应的一类视觉效果消失。

> 这是「源码阅读 + 本地实验型」实践，请勿提交这些改动。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `markdown()` 不在本库的导出里？
**答案**：因为它是 CodeMirror 官方的语言包（`@codemirror/lang-markdown`），负责把文本解析成 Lezer 语法树。本库建立在语法树之上，只做「装饰」，不重复造解析器。这也呼应了「零强制锁定」的设计原则。

**练习 2**：如果只想要图片预览、不想加数学公式，需要安装 `katex` 吗？
**答案**：不需要。`katex` 只是 `mathPlugin`/`blockMathField` 的可选依赖；`imageField` 不依赖它。按需引入正是为了让你只装真正用到的依赖。

---

### 4.3 mouseSelecting 接线：为什么必须手动监听鼠标

这是本讲最关键、也最容易遗漏的一步。

#### 4.3.1 概念说明

Live Preview 的本质是：光标进入某段文本时显示原始语法、离开时隐藏。但「拖拽选中一段文字」时，选区会不断变大，每变一次，沿途的元素都会在「显示源码 / 隐藏源码」之间来回切换，造成肉眼可见的**闪烁**。

本库的解决办法：用一个 `StateField` 记录「当前是否正在拖拽」，拖拽期间一律不显示源码。但 CodeMirror 自己并不知道用户在拖拽鼠标——它只认 transaction。所以**库的使用者必须手动监听 `mousedown`/`mouseup`，把鼠标状态翻译成 Effect 派发给编辑器**。这就是所谓「接线（wiring）」。

涉及两个概念（u2 会深入）：

- **StateEffect**：一种「附带数据的信号」，通过 `view.dispatch({ effects })` 投递，用来改变 state 而不改变文档。
- **StateField**：state 里的一块可变存储，随 transaction 自动更新。

#### 4.3.2 核心流程

```
用户按下鼠标 (mousedown)
   │  使用者代码监听 → view.dispatch({ effects: setMouseSelecting.of(true) })
   ▼
mouseSelectingField 变为 true
   │
   ▼
shouldShowSource 读到 isDragging=true → 直接返回 false（一律显示渲染态）
   │  → livePreviewPlugin 不再给标记加 visible 类
   ▼
拖拽全程不闪烁
   │
用户松开鼠标 (mouseup)
   │  使用者代码监听 → 在 requestAnimationFrame 里 dispatch setMouseSelecting.of(false)
   ▼
恢复正常的光标敏感显隐
```

为什么 `mouseup` 里要套一层 `requestAnimationFrame`？因为松开鼠标后，CodeMirror 还要处理「选区最终落点」的更新；如果立刻把拖拽状态关掉，可能在选区更新的同一帧里又触发一次显隐切换。延迟一帧再关，能让最终选区先稳定下来。

#### 4.3.3 源码精读

**第一步：库提供的状态与 Effect**（这部分是库写好的，使用者直接用）。

[src/core/mouseSelecting.ts:6](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L6) —— `setMouseSelecting = StateEffect.define<boolean>()`，携带一个布尔值。

[src/core/mouseSelecting.ts:13-23](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L13-L23) —— `mouseSelectingField` 是记录该状态的 `StateField`：`create` 初始为 `false`，`update` 时遍历 effects，遇到 `setMouseSelecting` 就更新值。

**第二步：库内部如何使用这个状态**。

[src/core/shouldShowSource.ts:38-41](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L38-L41) —— `if (isDragging) return false;`，拖拽时一律判定为「不显示源码」。

[src/plugins/livePreview.ts:128-137](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L128-L137) —— `livePreviewPlugin` 还会再叠加一次 `isTouched && !isDrag` 判断：即便 `shouldShowSource` 说该显示，只要还在拖拽，就不给标记加 `cm-formatting-inline-visible` 类。这是双保险。

**第三步：使用者必须自己写的接线代码**（库无法替你做）。

[demo/main.ts:309-319](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L309-L319) —— demo 的 `setupMouseSelecting` 就是标准写法：

- `mousedown` 派发 `setMouseSelecting.of(true)`；
- `mouseup` 在 `requestAnimationFrame` 里派发 `setMouseSelecting.of(false)`。

每个视图创建后都要调用一次：[demo/main.ts:356](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L356) —— `setupMouseSelecting(basicView)`。

README 也把这段标注为 `// Required:`：[README.md:86-94](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L86-L94) —— 说明它是接入的**必备**步骤，不是可选项。

#### 4.3.4 代码实践

目标：亲眼看「不接线」会闪烁、「接线」后平滑。

1. 确保 demo 已运行（`cd demo && npm run dev`）。
2. 在调用 `setupMouseSelecting` 的地方（如 [demo/main.ts:356](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L356)）**临时注释掉这一行**，刷新页面。
3. 在编辑器里用鼠标拖拽，横跨一段 `**粗体**` 文本。
4. 观察现象：拖拽过程中，被扫过的粗体标记会反复闪烁。
5. 恢复调用 `setupMouseSelecting(basicView)`，再次拖拽，闪烁消失。

预期结果：未接线时拖拽闪烁，接线后拖拽平滑。

> 待本地验证：闪烁的明显程度取决于浏览器与机器，但「有 / 无」差异应当可观察。请勿提交这个临时改动。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mousedown` 挂在 `view.contentDOM` 上，而 `mouseup` 挂在 `document` 上？
**答案**：`mousedown` 只关心「点在编辑器内容区」，所以挂在 `view.contentDOM`；但用户可能把鼠标拖到编辑器外面再松开，此时 `mouseup` 不再落在 `contentDOM` 上，所以必须挂在 `document` 才能保证一定捕获到松开事件，避免拖拽状态卡在 `true`。

**练习 2**：如果忘了套 `requestAnimationFrame`，直接在 `mouseup` 里 `dispatch setMouseSelecting.of(false)`，最可能出什么问题？
**答案**：松开瞬间，最终选区的更新与「关闭拖拽态」几乎同时发生，可能在同一帧里又触发一次标记显隐切换，导致松开时仍有轻微闪烁或跳动。延迟一帧让选区先稳定，正是为了避免这种竞态。

---

### 4.4 用 Compartment 在 Live / Source 模式间动态切换

#### 4.4.1 概念说明

`EditorState` 是不可变的，扩展一旦塞进 `extensions`，似乎就不能改了。但 CodeMirror 6 提供了 `Compartment`（隔间）：把一组扩展包进 `compartment.of([...])`，之后可以通过 `view.dispatch({ effects: compartment.reconfigure([...]) })` **在运行时替换这组扩展**，而不必重建整个编辑器。

demo 用它实现了顶部「Live Preview / Source Mode」两个按钮的切换。

#### 4.4.2 核心流程

```
新建 const comp = new Compartment()
        │
在 EditorState.extensions 里写 comp.of([ Live 扩展组... ])
        │
点击 Live 按钮   → dispatch comp.reconfigure([ facet(true) + livePreviewPlugin + ... ])
点击 Source 按钮 → dispatch comp.reconfigure([ facet(false) + markdownStylePlugin ])
        │
        ▼
编辑器无需销毁重建，扩展实时替换
```

#### 4.4.3 源码精读

新建 Compartment：[demo/main.ts:321](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L321) —— `const basicModeCompartment = new Compartment();`。

把核心扩展包进 Compartment：[demo/main.ts:330-346](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L330-L346) —— 注意只有「与模式相关」的扩展进了 `basicModeCompartment.of([...])`，而 `history()`、`keymap`、`markdown()`、`EditorView.lineWrapping`、`editorTheme` 留在 Compartment 外面（它们两种模式都要用，不需要换）。

切换到 Live 模式（点 Live Preview 按钮）：[demo/main.ts:452-474](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L452-L474) —— 用 `basicModeCompartment.reconfigure([...])` 重新装入完整的 Live 扩展组，并切换按钮高亮态。

切换到 Source 模式（点 Source Mode 按钮）：[demo/main.ts:476-485](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L476-L485) —— 只装 `[collapseOnSelectionFacet.of(false), markdownStylePlugin]`。

这里有个关键细节：Source 模式**移除了 `livePreviewPlugin`**。为什么？因为只要 `livePreviewPlugin` 还在，它就会给标记套上 `cm-formatting-inline` 类，把 `**`、`#` 等折叠隐藏（见 [src/plugins/livePreview.ts:128-137](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L128-L137)）。移除它之后，没有任何装饰去隐藏标记，原始语法就全部以普通文本可见——这正是「源码模式」想要的。`collapseOnSelectionFacet.of(false)` 则把总开关关掉，确保 `shouldShowSource` 不再驱动显隐。

按钮本身在 HTML 里：[demo/index.html:145-149](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html#L145-L149) —— `basicLiveBtn` 与 `basicSourceBtn`。

#### 4.4.4 代码实践

目标：验证 Compartment 切换不重建编辑器。

1. 运行 demo，在「Basic Table」编辑器里输入一些文字，把光标停在中间。
2. 反复点击 Live Preview / Source Mode 按钮。
3. 观察：
   - 切换时编辑器没有被销毁重建（光标位置、滚动条、未保存的输入都还在）。
   - Source 模式下 `**`、`#`、`|` 等全部可见；Live 模式下，光标离开处它们被隐藏。
4. 预期结果：模式平滑切换，编辑状态不丢失。

> 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `editorTheme` 放在 Compartment 外面，而不是和 Live 扩展一起放进 Compartment？
**答案**：因为两种模式都需要主题样式。放进 Compartment 意味着它会被 `reconfigure` 整组替换；放外面则无论怎么切换都常驻，避免重复列出，也保证切换瞬间样式不闪烁。

**练习 2**：如果只把 `collapseOnSelectionFacet.of(false)` 切过去、但保留 `livePreviewPlugin`，会得到真正的「源码模式」吗？
**答案**：不会。那样会接近「阅读模式」——`shouldShowSource` 在 facet 关闭时一律返回 false，所有标记都被隐藏，反而看不到原始语法。要得到源码模式，必须像 demo 那样**移除 `livePreviewPlugin`**，让标记不再被套上隐藏类。

---

## 5. 综合实践

把本讲的三个要点（挂载、扩展组合、mouseSelecting 接线）合起来，亲手搭一个**最小的** Live Preview 编辑器页面。我们直接基于 README 的 Quick Start 模式，而不是 demo 的全特性版本。

### 目标

新建一个独立页面，挂一个 CodeMirror 编辑器，启用 `livePreviewPlugin`，正确接线 `mouseSelecting`，输入 `**粗体**`，观察标记在光标进入时显示、离开时隐藏。

### 操作步骤

**方式 A：复用 demo 的 Vite 环境（推荐，最快）**

1. 在 `demo/` 目录下新建文件 `demo/mini.html` 与 `demo/mini.ts`（不要删除现有文件）。
2. `demo/mini.html` 内容（示例代码）：

```html
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Mini Live Preview</title></head>
<body>
  <div id="editor" style="height:400px;border:1px solid #ccc"></div>
  <script type="module" src="/mini.ts"></script>
</body>
</html>
```

3. `demo/mini.ts` 内容（示例代码，仿照 README Quick Start）：

```typescript
// 示例代码
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import {
  livePreviewPlugin,
  markdownStylePlugin,
  editorTheme,
  mouseSelectingField,
  collapseOnSelectionFacet,
  setMouseSelecting,
} from 'codemirror-live-markdown';

const view = new EditorView({
  state: EditorState.create({
    doc: '# 标题\n\n这是一段 **粗体** 和 *斜体* 文本。',
    extensions: [
      markdown(),
      collapseOnSelectionFacet.of(true),
      mouseSelectingField,
      livePreviewPlugin,
      markdownStylePlugin,
      editorTheme,
    ],
  }),
  parent: document.getElementById('editor')!,
});

// 必备：接线拖拽状态，否则拖拽选区会闪烁
view.contentDOM.addEventListener('mousedown', () => {
  view.dispatch({ effects: setMouseSelecting.of(true) });
});
document.addEventListener('mouseup', () => {
  requestAnimationFrame(() => {
    view.dispatch({ effects: setMouseSelecting.of(false) });
  });
});
```

4. 确保 `demo` 目录已执行过 `npm install`，然后运行 `npm run dev`。
5. 浏览器访问 `http://localhost:5173/mini.html`（Vite 按文件名直接提供页面）。

### 需要观察的现象

- 页面出现一个编辑器，初始时「标题」被加粗放大、「粗体」二字显示为粗体效果，而 `**` 不可见。
- 用方向键或鼠标把光标移到「粗体」二字中间：`**` 两个星号**平滑淡入**出现，可编辑。
- 光标移开后：`**` 再次平滑隐藏。
- 用鼠标拖拽选中跨越「粗体」的一段文字：拖拽过程中**不闪烁**。

### 预期结果

一个最小可用的 Live Preview 编辑器，具备「光标进入显标记、离开隐标记、拖拽不闪烁」三个核心行为。

> 待本地验证：若 `/mini.html` 无法被 Vite 直接服务，可把脚本入口改名为 `main.ts` 放进一个新子目录，或直接在现有 `index.html` 里临时替换挂载实验。核心逻辑不变。请勿提交这些临时文件。

**方式 B：全新工程**——用 `npm install codemirror-live-markdown`（及其 peer 依赖）再配一个 Vite，把上面的 `mini.ts` 作为入口。步骤与 Quick Start 一致，这里不赘述。

## 6. 本讲小结

- 接入本库 = 用 `EditorState.create({ doc, extensions })` + `new EditorView({ state, parent })` 挂载编辑器，本质是「正确填写 extensions」。
- 最小核心组合：`markdown()` + `collapseOnSelectionFacet.of(true)` + `mouseSelectingField` + `livePreviewPlugin` + `markdownStylePlugin` + `editorTheme`；特性扩展（math / table / code / image / link）按需追加。
- `livePreviewPlugin` 管「标记藏不藏」，`markdownStylePlugin` 管「内容长什么样」，二者职责分离。
- **mouseSelecting 接线是必备步骤**：必须手动监听 `mousedown`（挂 `contentDOM`）和 `mouseup`（挂 `document`，套 `requestAnimationFrame`）并派发 `setMouseSelecting`，否则拖拽选区会闪烁。
- 用 `Compartment` 可在运行时切换 Live / Source 模式而不重建编辑器；Source 模式的关键是**移除 `livePreviewPlugin`**，让原始语法全部可见。
- demo 通过 Vite 别名把包名指向 `src/index.ts`，所以改源码即时生效。

## 7. 下一步学习建议

到这里，你已经能把库「用起来」。后续建议：

- 进入 **u2 单元（Live Preview 核心机制）**，先读 u2-l1《CodeMirror 6 核心概念速览》，搞清 `StateField` / `ViewPlugin` / `Decoration` / `Facet` 的区别——本讲里这些词都出现过，但还没展开。
- 重点读 u2-l2《shouldShowSource》和 u2-l3《Facet / mouseSelecting / checkUpdateAction》：本讲的「为什么接线」在那一单元会从源码层面彻底讲透。
- 如果想现在就动手改 demo，可在 `mini.ts` 里加一个 `imageField()` 或 `linkPlugin()`，先体验「按需加特性」，再回头读源码。

建议继续阅读的源码：`src/core/shouldShowSource.ts`（决策核心）、`src/core/mouseSelecting.ts`（接线背后的状态）、`src/plugins/livePreview.ts`（标记显隐的实现）。
