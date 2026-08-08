# editorTheme：CSS 变量主题与标记动画

## 1. 本讲目标

本讲是「主题、测试、架构与二次开发」单元的第一篇。前面我们已经看过 `livePreviewPlugin` 如何决定「标记藏不藏」（[u2-l4](u2-l4-live-preview-plugin.md)）、`markdownStylePlugin` 如何决定「内容长什么样」（[u2-l5](u2-l5-markdown-style-plugin.md)）。但它们都只是给文本**贴上 CSS 类名**（如 `cm-formatting-inline`、`cm-strong`），类名背后真正的颜色、字号、动画效果从哪里来？答案就是本讲的主角——`editorTheme`。

读完本讲，你应当能够：

- 看懂 `EditorView.theme({...})` 的「对象式样式」写法，知道 `&` 代表什么、属性名为什么是驼峰。
- 理解 `hsl(var(--name, fallback))` 这一模式如何让主题被**外部覆盖**，从而不改一行源码就能换肤（包括暗色模式）。
- 说清楚为什么标记的显隐用 `max-width` / `font-size` 过渡动画，而**不能用** `display: none`。
- 独立地为这个库实现一套暗色主题。

## 2. 前置知识

本讲假设你已经掌握：

- **CSS 自定义属性（CSS 变量）**：`:root { --foo: bar; }` 定义，`var(--foo)` 引用，`var(--foo, default)` 提供兜底值。
- **HSL 颜色**：`hsl(H S% L%)` 用色相（Hue，0–360）、饱和度（Saturation，百分比）、亮度（Lightness，百分比）三个通道描述颜色。本讲的 CSS 变量存的就是「三个通道值」（如 `220 9% 9%`），而不是 `#1a1a1a` 这样的十六进制——这是最容易踩的坑，后文会详解。
- **CSS 过渡（transition）与关键帧动画（@keyframes）**：前者让属性值在两个状态间平滑插值，后者定义逐帧动画。
- 本库的职责分层（来自 [u2-l4](u2-l4-live-preview-plugin.md) / [u2-l5](u2-l5-markdown-style-plugin.md)）：**插件负责贴类名（逻辑），主题负责给类名上色（表现）**。

> 名词解释：**标记节点（mark node）**指 Markdown 里纯符号的部分，如 `**`、`#`、`>`、`` ` ``；**内容节点**指被这些符号包裹的实际文字，如 `StrongEmphasis`。`livePreviewPlugin` 隐藏的是前者，`markdownStylePlugin` 美化的是后者，而 `editorTheme` 给两者都提供最终的视觉样式。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/theme/default.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts) | 本讲核心。用 `EditorView.theme({...})` 定义全部样式：基础样式、标记动画、Markdown 内容样式、数学公式、表格、图片、代码块、语法高亮。 |
| [src/theme/\_\_tests\_\_/default.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/__tests__/default.test.ts) | 主题的测试。验证 `editorTheme` 是合法 Extension、能挂到 `EditorView`、能与其它扩展共存、能处理文档更新。 |
| [demo/index.html](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html) | demo 页面。**真正定义 CSS 变量取值的地方**（`:root` 块），是理解「外部换肤」的最佳样本。 |
| [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) | 桶文件，`export { editorTheme } from './theme/default';` 把主题对外暴露。 |

## 4. 核心概念与源码讲解

### 4.1 EditorView.theme：对象式样式定义语法

#### 4.1.1 概念说明

CodeMirror 6 提供了一个 `EditorView.theme(spec)` 方法，让你用**一个 JavaScript 对象**描述一整套样式，它会自动生成一个 Extension。它的好处是：所有选择器都会被自动**限定在当前编辑器作用域内**（加上唯一前缀类名），不会污染页面其它元素；属性名用驼峰（`backgroundColor` 而非 `background-color`），符合 JS 习惯。

`editorTheme` 就是把整个库需要的几百条样式规则塞进一个这样的对象，对外只暴露一个 Extension，使用者只需在 `extensions` 数组里加一行 `editorTheme` 即可（见 [u1-l4](u1-l4-first-live-preview-editor.md)）。

#### 4.1.2 核心流程

`EditorView.theme` 对象的几个约定：

1. `'&'`：代表编辑器根元素（带 `cm-editor` 类的那个 `<div>`）。写在 `&` 下的属性直接作用于编辑器容器本身。
2. **任意 CSS 选择器字符串作为键**：如 `'.cm-content'`、`'.cm-line'`，甚至 `'.cm-codeblock-copy:hover'`、`'&.cm-focused .cm-selectionBackground'`。它们都会被自动加上编辑器作用域前缀。
3. **值为一个样式对象**：键是驼峰属性名，值是字符串（CSS 值）。
4. **特殊键 `'@keyframes xxx'`**：用来定义关键帧动画，值是 `{ from: {...}, to: {...} }` 形式的对象。

伪代码：

```
EditorView.theme({
  '&': { /* 根元素样式 */ },
  '.cm-content': { /* 内容区样式 */ },
  '.cm-x:hover': { /* 伪类样式 */ },
  '@keyframes spin': { from: {...}, to: {...} },
})
```

#### 4.1.3 源码精读

主题的定义入口在这两行，整体是一个巨大的对象字面量传给 `EditorView.theme`：

[src/theme/default.ts:13-14](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L13-L14) —— `export const editorTheme = EditorView.theme({...})`，把后面的对象变成一个 Extension。

`&` 代表编辑器根，这里设置背景透明、字号、高度：

[src/theme/default.ts:15-19](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L15-L19) —— 基础样式块，`'&'` 作用于编辑器容器本身（透明背景、16px 字号、100% 高度）。

嵌套选择器的写法，注意键里同时出现 `&`（编辑器根）和后代选择器：

[src/theme/default.ts:37-39](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L37-L39) —— `&.cm-focused .cm-selectionBackground`，表示「编辑器聚焦时，选区背景」用更深的蓝色（带 `!important`）。

关键帧动画用 `'@keyframes spin'` 这个特殊键定义，值是 `from/to` 对象（注意里面的属性名同样是驼峰 `transform`）：

[src/theme/default.ts:364-367](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L364-L367) —— 定义 `spin` 旋转动画，供图片加载 spinner 使用。

测试也印证了 `editorTheme` 就是一个普通 Extension，用法和别的扩展完全一样——塞进 `EditorState.create({ extensions: [...] })` 即可：

[src/theme/\_\_tests\_\_/default.test.ts:14-22](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/__tests__/default.test.ts#L14-L22) —— 把 `editorTheme` 当 Extension 用，断言 State 正常创建。

[src/theme/\_\_tests\_\_/default.test.ts:24-37](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/__tests__/default.test.ts#L24-L37) —— 挂到真实 `EditorView`，断言 `view.dom` 存在，最后 `view.destroy()` 清理（这是测试 CodeMirror 扩展的标准套路）。

#### 4.1.4 代码实践

**实践目标**：亲手用 `EditorView.theme` 写一条样式，确认它的对象式语法。

**操作步骤**：

1. 进入 demo 目录，参考 [demo/main.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts) 里任意一个 `EditorState.create({...})`。
2. 在 `extensions` 数组里、`editorTheme` **之后**再加一个临时主题：
   ```typescript
   import { EditorView } from '@codemirror/view';
   // ...在 extensions 末尾追加：
   EditorView.theme({
     '&': { outline: '2px dashed red' },   // 给编辑器根画个红框
     '.cm-content': { background: 'lemonchiffon' },
   }),
   ```
3. 运行 demo（根目录 `cd demo && npm run dev`，详见 [u1-l3](u1-l3-build-test-run-demo.md)），在浏览器打开。

**需要观察的现象**：编辑器外框变成红色虚线，内容区底色变成淡黄。

**预期结果**：说明你写的对象被正确编译成带作用域前缀的 CSS，且后写的主题会与 `editorTheme` 叠加（CodeMirror 的多个主题 Extension 共存，后者不覆盖前者未涉及的属性）。

> 这是「源码阅读型/动手型」实践；如果你不在本地运行，可以改在浏览器 DevTools 里直接给 `.cm-editor` 加 `outline`，观察同样的效果，标注「待本地验证」即可。

#### 4.1.5 小练习与答案

**练习 1**：如果想给「编辑器失去焦点时」的选区换个颜色，应该用哪个选择器键？

> **答案**：用 `'.cm-selectionBackground'`（不带 `&.cm-focused`）。源码里 `&.cm-focused .cm-selectionBackground` 专门处理聚焦态；非聚焦态走默认的那条（[src/theme/default.ts:34-36](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L34-L36)）。

**练习 2**：为什么源码里属性名写成 `backgroundColor` 而不是 `background-color`？

> **答案**：`EditorView.theme` 要求键是合法的 JS 标识符风格（驼峰），它会在内部转成 CSS 属性名。写 `background-color` 会因为连字符无法作为对象键字面量而需要加引号，且不符合 CodeMirror 的约定。

---

### 4.2 HSL CSS 变量与 fallback：可外部定制的主题

#### 4.2.1 概念说明

这是本讲最关键的设计：`editorTheme` 几乎所有颜色都不写死，而是写成

```
hsl(var(--md-heading, var(--foreground, 220 9% 9%)))
```

这种「三层套娃」。它的含义是：

1. 优先用外部定义的 `--md-heading`；
2. 若没定义，退一步用 `--foreground`；
3. 连 `--foreground` 也没有，最后用字面量 `220 9% 9%`（一个深灰）。

而**真正给这些变量赋值的地方不在 `editorTheme` 里**，而在使用者的页面上——demo 的 [demo/index.html](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html) 的 `:root` 块。这就是「不改源码即可换肤」的根因：主题只**消费**变量，不**定义**变量。

#### 4.2.2 核心流程

变量解析与着色的链路：

```
使用者页面 :root { --foreground: 0 0% 3.9%; ... }
        │
        ▼
editorTheme 里 hsl(var(--md-heading, var(--foreground, 220 9% 9%)))
        │   ① --md-heading 没定义 → 取 var(--foreground)
        │   ② --foreground = "0 0% 3.9%"
        ▼
hsl(0 0% 3.9%)  →  接近纯黑的颜色
        │
        ▼
浏览器把它应用到 .cm-header-1 等元素
```

**两个必须记住的规则**：

1. **变量存的是 HSL「通道值」，不是十六进制色**。因为外层包了 `hsl(...)`，变量展开后必须能拼成合法的 `hsl(H S% L%)`。demo 写的是 `--foreground: 0 0% 3.9%;`（三个通道），而不是 `#000`。
2. **`var(name, fallback)` 的 fallback 只在变量「未定义」时触发**。如果你给变量赋了一个非法值（比如把 `--md-heading` 设成 `#1a1a1a`），`var()` 会**成功**取到这个值，于是 `hsl(#1a1a1a)` 整条声明非法被浏览器丢弃——并不会回退到 fallback。这是一个隐蔽的坑（README 的 Theming 示例恰好用了十六进制，照抄会导致颜色失效，应改为通道值写法）。

> 关于暗色模式的换算：把亮色主题翻成暗色，亮度（L）大致取反。若亮色为 `hsl(H S% L%)`，暗色可近似取：
>
> \[ L_{dark} \approx 100\% - L_{light} \]
>
> 色相 H 与饱和度 S 一般保留。这是后面综合实践的依据。

#### 4.2.3 源码精读

标题颜色用了「三层 fallback」的典型写法（注意 `220 9% 9%` 是 HSL 通道值）：

[src/theme/default.ts:89-94](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L89-L94) —— `.cm-header-1` 的颜色为 `hsl(var(--md-heading, var(--foreground, 220 9% 9%)))`：先 `--md-heading`，再 `--foreground`，最后深灰兜底。

粗体、斜体、链接同理，分别走 `--md-bold` / `--md-italic` / `--md-link`：

[src/theme/default.ts:113-134](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L113-L134) —— `.cm-strong` / `.cm-emphasis` / `.cm-link` 等内容节点的颜色全部走 CSS 变量 + fallback 模式。

注意 `--muted-foreground` 这类「基础语义变量」被反复复用（标记颜色、 spinner 边框、错误提示等），改一处全局生效：

[src/theme/default.ts:49](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L49) —— 行内标记的灰度颜色 `hsl(var(--muted-foreground, 220 9% 46%) / 0.6)`，末尾 `/ 0.6` 是 HSL 的 alpha 通道（60% 不透明）。

**真正给变量赋值的地方**——demo 页面的 `:root`（注意值都是通道值，且 `--md-heading` 直接指向另一个变量）：

[demo/index.html:15-25](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html#L15-L25) —— `:root` 定义 `--foreground`、`--primary`、`--muted`、`--muted-foreground`、`--border`，并把 `--md-heading/--md-bold/--md-italic` 都设为 `var(--foreground)`、`--md-link` 设为 `var(--primary)`。这一段就是「外部主题」。

对比 `editorTheme` 全文搜索 `--background`：它只在按钮、tooltip 等「需要白底」的元素上出现（如 `.cm-table-toggle`），并未在 `&` 根元素上设背景色——根背景是 `transparent`（[src/theme/default.ts:15-19](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L15-L19)），所以编辑器底色由外层容器决定，进一步说明主题把「整体配色」交给了外部。

#### 4.2.4 代码实践

**实践目标**：只改 CSS 变量，让标题变成红色，验证「不动 `editorTheme` 源码也能改色」。

**操作步骤**：

1. 打开 demo，在 [demo/index.html](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html) 的 `:root` 里，把 `--md-heading: var(--foreground);` 改成 `--md-heading: 0 84% 60%;`（一个偏红橙色的 HSL 通道值）。
2. 刷新页面，看任一带 `#` 标题的编辑器。

**需要观察的现象**：所有标题文字变成红橙色，其余样式不变。

**预期结果**：因为 `.cm-header-*` 的颜色是 `hsl(var(--md-heading, ...))`，外部覆盖 `--md-heading` 即可全局生效。这验证了变量化的主题架构。

**反例验证（理解 fallback 陷阱）**：把 `--md-heading` 改成 `#ff0000`（十六进制），刷新。

**预期结果**：标题颜色**不会**变红，反而可能变回默认黑色或丢失颜色——因为 `hsl(#ff0000)` 是非法 CSS，整条 `color` 声明被丢弃，fallback 不触发。这个反例能帮你牢记「变量要存通道值」。

#### 4.2.5 小练习与答案

**练习 1**：把 `--muted-foreground` 改成 `0 0% 100%`（接近白），会影响哪些地方？

> **答案**：所有用 `hsl(var(--muted-foreground, ...))` 的元素都会变浅——包括行内标记颜色（[L49](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L49)）、块级标记（[L78](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L78)）、图片加载占位文字、spinner 边框、行号等。这正是「一处定义、多处复用」的力量。

**练习 2**：如果使用者**既没**在页面定义任何 `--md-*` **也没**定义 `--foreground`，标题会显示成什么颜色？为什么？

> **答案**：深灰 `hsl(220 9% 9%)`。因为 `var(--md-heading, var(--foreground, 220 9% 9%))` 会一路回退到最内层的字面量兜底值。这就是 fallback 保证「开箱即用」的机制。

---

### 4.3 标记过渡动画：为什么不用 display:none

#### 4.3.1 概念说明

Live Preview 的招牌效果是：光标移进 `**粗体**` 时，那两个 `**` 标记会平滑地「展开」显现，移出时又平滑「收起」消失。这个动画是 `editorTheme` 提供的，而触发它的类名切换由 `livePreviewPlugin` 负责（详见 [u2-l4](u2-l4-live-preview-plugin.md)）。

关键问题是：**为什么用 `max-width` / `font-size` 的过渡，而不是直接 `display: none`？** 两个原因：

1. **`display` 不可被动画化**。CSS 的 `transition` 无法对 `display` 插值，`display:none ↔ display:inline` 是瞬间的跳变，没有平滑效果。
2. **标记文本必须留在 DOM 里且可编辑**。`display:none` 的元素不占布局、无法被选中编辑，而 Live Preview 的核心诉求正是「光标进去就能改原始语法」。所以标记只能「视觉上藏起来」，不能「物理上移除」。

库对两类标记用了两种「视觉隐藏」技巧：

- **行内标记**（`**`、`*`、`` ` `` 等）：用 `max-width: 0` 把宽度压到 0，配合 `overflow: hidden` 裁掉内容，`opacity: 0` 顺便淡出。显示时 `max-width: 4ch` 展开。这是**横向展开**动画。
- **块级标记**（`#`、`>`、`-` 等，出现在行首）：用 `font-size: 0.01em` 把字号缩到约 1%（≈0.16px），视觉上几乎不可见，显示时恢复 `1em`。这是**缩放**动画。

#### 4.3.2 核心流程

行内标记显隐的状态机（由 `livePreviewPlugin` 切换类名）：

```
状态A（渲染态，光标在外）：类名 = "cm-formatting-inline"
   maxWidth:0, opacity:0, transform:scaleX(0.8)  → 标记不可见
                      │ 光标进入（livePreviewPlugin 加 -visible 类）
                      ▼
状态B（源码态，光标在内）：类名 = "cm-formatting-inline cm-formatting-inline-visible"
   maxWidth:4ch, opacity:1, transform:scaleX(1)  → 标记平滑展开
                      │ 光标离开（移除 -visible 类）
                      ▼
                   回到状态A（max-width/opacity/transform 各自过渡回去）
```

整个过程文本**始终在 DOM 中**，只是宽度/透明度在过渡，所以可以随时编辑。

块级标记同理，只是把 `max-width` 换成 `font-size`：

```
状态A：fontSize:0.01em, opacity:0   →  字号缩到 ~1%，不可见
状态B：fontSize:1em,    opacity:0.6 →  恢复正常字号，淡入到 60% 灰
```

#### 4.3.3 源码精读

行内标记的基础态——注意 `max-width:0` + `opacity:0` + `transform:scaleX(0.8)` 三重隐藏，以及 `transition` 同时过渡这三项：

[src/theme/default.ts:42-61](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L42-L61) —— `.cm-formatting-inline` 用 `display:inline-flex`、`overflow:hidden`、`maxWidth:'0'`、`opacity:'0'`，并声明 `transition` 同时过渡 `max-width 0.2s`、`opacity 0.15s`、`transform 0.15s`。`cubic-bezier(0.2,0,0.2,1)` 是一种先快后慢的缓动。

显示态只覆盖那三个属性，过渡自动产生动画：

[src/theme/default.ts:63-69](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L63-L69) —— `.cm-formatting-inline-visible` 把 `maxWidth` 改成 `4ch`、`opacity:'1'`、`transform:'scaleX(1)'`，因为基础态已声明 `transition`，这三个值的变化会平滑过渡。

块级标记用 `font-size` 而非 `max-width`，基础态缩到 `0.01em`：

[src/theme/default.ts:72-81](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L72-L81) —— `.cm-formatting-block` 用 `fontSize:'0.01em'`、`opacity:'0'`，`transition` 过渡 `font-size 0.2s` 和 `opacity 0.2s`。

[src/theme/default.ts:83-86](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L83-L86) —— `.cm-formatting-block-visible` 恢复 `fontSize:'1em'`、`opacity:'0.6'`。

**其它两类动画**（主题里也一并定义了关键帧）：

公式渲染态用 `@keyframes mathFadeIn` 做淡入（`scale(0.95)→scale(1)`）：

[src/theme/default.ts:147-152](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L147-L152) —— `.cm-math-inline` 声明 `animation: 'mathFadeIn 0.15s ease-out'`。

[src/theme/default.ts:187-190](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L187-L190) —— `@keyframes mathFadeIn` 定义 `from { opacity:0; transform:scale(0.95) } to { opacity:1; transform:scale(1) }`。

图片加载 spinner 用 `@keyframes spin` 做无限旋转：

[src/theme/default.ts:279-286](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L279-L286) —— `.cm-image-spinner` 是一个 16px 圆形，`border` 只给 `borderTopColor` 上色，`animation: 'spin 0.8s linear infinite'`。

[src/theme/default.ts:364-367](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L364-L367) —— `@keyframes spin` 从 `rotate(0deg)` 到 `rotate(360deg)`，配合只染色的顶边形成「转圈」视觉。

#### 4.3.4 代码实践

**实践目标**：直观体会「为什么不能 `display:none`」。

**操作步骤**：

1. 启动 demo，打开浏览器 DevTools，定位到一个行内标记元素：在 Live Preview 编辑器里输入 `**粗体**`，把光标移进去，用元素检查器选中带 `cm-formatting-inline-visible` 类的那个 `<span>`（里面是 `**`）。
2. **实验 A**：在 DevTools 的 `.cm-formatting-inline` 规则里，把 `max-width:0` 改成 `display:none`（同时移除光标让它回到隐藏态）。
3. **实验 B**：再把过渡时长改大——在 `.cm-formatting-inline` 的 `transition` 里把 `0.2s` 改成 `1s`。

**需要观察的现象**：

- 实验 A：光标进出时标记变成「瞬间出现/消失」，没有展开动画；且某些情况下标记可能无法被光标正确选中编辑。
- 实验 B：标记展开/收起明显变慢，能清楚看到 `max-width` 从 0 涨到 4ch 的横向拉伸过程。

**预期结果**：实验 A 证明 `display` 不可过渡、且影响可编辑性；实验 B 证明动画确由 `max-width` 等属性的 `transition` 驱动。

> 若不在本地运行，可只阅读上述源码并在脑中推演：`display:none` 会移除布局盒，CodeMirror 的光标/选区无法落在一个不渲染的节点上，因此 Live Preview 刻意避开了它。标注「待本地验证」即可。

#### 4.3.5 小练习与答案

**练习 1**：行内标记用 `max-width`，块级标记却改用 `font-size`，试推测原因。

> **答案**：行内标记（如 `**`）是 `inline-flex` 元素，有明确宽度概念，`max-width` 能精确控制其横向占位并配合 `overflow:hidden` 裁剪；块级标记出现在行首、参与块级布局，用 `max-width` 控制反而可能引起行盒异常。把 `font-size` 缩到 `0.01em` 是一种对块级文本更稳妥的「视觉隐藏」——字号趋零，文字自然不可见，又不破坏行结构。两者都满足「留在 DOM、可编辑、可过渡」三原则。

**练习 2**：`.cm-formatting-inline-visible` 里并没有再写一遍 `transition`，为什么它也能产生动画？

> **答案**：`transition` 声明在基础类 `.cm-formatting-inline` 上，且该类在显示态依然存在（显示态是基础类 + `-visible` 类的叠加）。浏览器看到 `max-width/opacity/transform` 的值因加了 `-visible` 而改变，就按已声明的 `transition` 平滑过渡。

---

## 5. 综合实践

**任务：用覆盖 CSS 变量的方式，为这个库实现一套暗色主题，并说明为什么完全不需要改动 `editorTheme` 源码。**

**背景**：`editorTheme` 只**消费**变量、不**定义**变量；真正的变量值在使用者页面。所以我们只要在页面 `:root`（或更具体的作用域）里重新赋值，就能换肤。把亮色翻成暗色，可套用亮度取反近似 \[ L_{dark} \approx 100\% - L_{light} \]。

**操作步骤**：

1. 复制 [demo/index.html](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html) 的 `:root` 块，在它下面新增一个暗色作用域，例如给 `<body>` 加类 `.dark`，或直接覆盖 `:root`：

   ```css
   /* 示例代码：暗色主题变量覆盖（值均为 HSL 通道值，供 editorTheme 的 hsl(...) 消费） */
   .dark {
     /* 把亮色 palette 翻成暗色：亮度大致取反 */
     --foreground: 210 20% 98%;        /* 原 0 0% 3.9% → 接近白 */
     --background: 222 47% 11%;        /* 深色底 */
     --primary: 217 91% 60%;           /* 亮蓝，暗底下更醒目 */
     --muted: 217 19% 17%;             /* 原 210 40% 96% → 深灰底 */
     --muted-foreground: 215 20% 65%;  /* 原 215 16% 47% → 提亮，保证对比度 */
     --border: 217 19% 27%;            /* 原 214 31% 91% → 深边框 */

     /* 语义变量：让标题/正文用浅色前景，链接用亮蓝 */
     --md-heading: var(--foreground);
     --md-bold: var(--foreground);
     --md-italic: var(--foreground);
     --md-link: var(--primary);
   }
   body { background: hsl(var(--background)); }
   .dark .editor { background: hsl(222 47% 11%); }
   ```

2. 给 `<body>` 加上 `class="dark"`（或在 demo 里加一个切换按钮动态增删这个类）。
3. 刷新页面，观察 5 个编辑器。

**需要观察的现象**：编辑器底色变深、文字变浅、链接变亮蓝、标记灰度自动跟随 `--muted-foreground` 变亮、表格/代码块底色跟随 `--muted` 变深——所有这些都不需要改 `editorTheme` 一行代码。

**预期结果**：因为 `editorTheme` 把所有颜色都写成 `hsl(var(--xxx, ...))`，覆盖 `--xxx` 即全局生效。

**回答关键问题——为什么不用改源码？**

- `editorTheme` 用 `EditorView.theme` 定义的是「**规则结构**」（哪些选择器、过渡、动画），而「**具体取值**」通过 CSS 变量**外置**到使用者页面。
- 它还自带逐层 fallback，即使你只覆盖部分变量（比如只改 `--foreground` 和 `--primary`），其余变量会通过 `var(--md-heading, var(--foreground, ...))` 这种链式回退自动跟随。
- 暗色模式只需在使用者侧定义一组新的变量取值（可挂在 `.dark` 作用域上动态切换），完全不需要 fork 或修改 `editorTheme`。这正是「主题与逻辑分离」「样式数据外置」架构的收益。

> 标注：上述暗色通道值为示例取值，具体观感「待本地验证」；你可以按自己审美微调饱和度与亮度。

## 6. 本讲小结

- `editorTheme` 用 `EditorView.theme({...})` 把整套样式写成一个对象式 Extension：`&` 代表编辑器根，键是带作用域的 CSS 选择器，属性名驼峰，`'@keyframes xxx'` 定义关键帧动画。
- 颜色几乎全部写成 `hsl(var(--name, fallback))` 的「变量 + 兜底」模式；变量存的是 **HSL 通道值**（如 `220 9% 9%`）而非十六进制，且 fallback 只在变量**未定义**时触发——这是换肤与避坑的关键。
- 真正给变量赋值的地方在使用者页面（demo 的 `:root`），`editorTheme` 只消费不定义，因此**覆盖变量即可换肤**，暗色模式也不例外，无需改源码。
- 标记显隐用 `max-width`（行内）/ `font-size`（块级）过渡动画，而非 `display:none`——因为 `display` 不可过渡、且会移除布局盒导致标记无法编辑。
- 公式淡入用 `@keyframes mathFadeIn`（scale+opacity），图片加载 spinner 用 `@keyframes spin`（无限旋转），都在主题里一并定义。
- 与前序讲义呼应：`livePreviewPlugin` 负责切换 `cm-formatting-*-visible` 类名（逻辑，[u2-l4](u2-l4-live-preview-plugin.md)），`markdownStylePlugin` 负责给内容节点贴 `cm-strong` 等类名（[u2-l5](u2-l5-markdown-style-plugin.md)），而 `editorTheme` 负责给这些类名赋予真正的颜色与动画（表现）。

## 7. 下一步学习建议

- **测试视角**：下一篇 [u7-l2 测试策略：用 Vitest + jsdom 测试 ViewPlugin 与 Widget](u7-l2-testing-strategy-vitest-jsdom.md) 会讲解如何在本项目的测试环境里验证扩展行为；你可以回头给 `editorTheme` 写一个「挂载后 `.cm-editor` 类存在」之类的断言（参考 [default.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/__tests__/default.test.ts)）。
- **架构视角**：[u7-l3 整体架构与设计取舍](u7-l3-architecture-and-tradeoffs.md) 会把本讲的「主题外置变量」「逻辑与表现分离」放进整个库的架构图里统一复盘。
- **二次开发**：若你想写自定义 Live Preview 插件（[u7-l4](u7-l4-custom-plugin-development.md)），记得复用 `editorTheme` 已有的变量体系与 CSS 类命名约定（`cm-formatting-*`、`cm-*-source` 等），让你的插件样式与原生体验一致。
- **延伸阅读源码**：通读 [src/theme/default.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts) 全文，按「基础 → 标记动画 → 内容样式 → 公式 → 表格 → 图片 → 链接 → 代码块 → 语法高亮」的注释分区通览，能把整个库所有可视化元素的样式来源一次看全。
