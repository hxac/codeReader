# Docsify 配置与主题样式系统

## 1. 本讲目标

本讲带你打开 `docs/index.html` 这个「单页应用外壳」，把里面两件最容易被忽视、却决定整站观感的事情彻底讲清楚：

1. `window.$docsify` 这个全局配置对象到底配了哪些开关、每一项对应什么插件、改了会怎样。
2. 站点的「主题样式」是怎么一层层叠加出来的——从 CDN 上的 `docsify-themeable` 基础变量、到明暗两套配色表、再到本地 `main.css` 的覆盖。

学完后你应当能够：

- 看懂 `window.$docsify` 里每一项配置的作用，并能安全地修改文案类配置（如搜索框占位符、分页按钮文字）。
- 解释「明暗主题切换」在 HTML 层面是如何依靠带 `title` 的备选样式表（alternate stylesheet）实现的。
- 知道为什么 `main.css` 必须放在所有 CDN 样式表之后加载，以及如何用 CSS 自定义属性（CSS 变量）覆盖默认主题。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们在 u1-l2、u1-l4 中已建立）：

- **Docsify 是浏览器端运行的库**：它通过 `docs/index.html` 里的 `<script>` 标签从 CDN 加载，把 Markdown 渲染成网页。本机不需要安装 docsify 本体，只需要 `docsify-cli` 提供的本地静态服务器。
- **`index.html` 是唯一入口**：它做三件事——预留挂载点 `<div id="app">`、声明全局配置 `window.$docsify`、按固定顺序加载核心库与插件脚本。
- **配置必须早于核心库加载**：`window.$docsify` 这个对象赋值的 `<script>` 块，必须写在加载 `docsify.min.js` 的 `<script>` 之前，否则核心库读不到配置。
- **`loadSidebar` / `loadFooter` 是「指名」而非「渲染」**：`loadSidebar: 'toc.md'` 由核心库处理（侧边栏是核心自带能力），而 `loadFooter: 'footer.md'` 只是告诉核心「页脚文件叫什么」，真正的页脚渲染由第三方 `docsify-footer` 插件完成。

本讲还会用到几个前端基础术语，先统一解释：

| 术语 | 含义 |
|------|------|
| **CSS 自定义属性 / CSS 变量** | 形如 `--theme-hue: 204;` 的自定义属性，定义在 `:root` 上即可全站使用，写法是 `var(--theme-hue)`。改一个变量就能批量改变观感。 |
| **`:root` 选择器** | 指向文档根元素 `<html>`，通常把全局 CSS 变量定义在这里。 |
| **备选样式表（alternate stylesheet）** | 一组带 `title` 属性的 `<link rel="stylesheet">`，浏览器同一时刻只激活其中一个；把另一个标记为 `rel="stylesheet alternative"` 即可让它在「关闭」状态等待切换。 |
| **CDN** | 内容分发网络。本仓库用 `//npm.elemecdn.com/...` 从 CDN 加载第三方库，省去本地安装。 |
| **HSB / HSV 色彩模型** | 用「色相（Hue）/ 饱和度（Saturation）/ 明度（Lightness 或 Value）」三个分量描述颜色。`docsify-themeable` 正是用这套模型推导整站配色。 |

## 3. 本讲源码地图

本讲只涉及两个文件，但它们牵动了一整串 CDN 资源：

| 文件 / 资源 | 作用 |
|-------------|------|
| `docs/index.html` | 单页应用外壳。`<head>` 里串联了 5 条样式表（主题基础、明暗配色、KaTeX、本地 main.css），`<body>` 里先声明 `window.$docsify`，再按序加载核心库与十余个插件。 |
| `docs/assets/css/main.css` | 本仓库唯一的自定义样式文件。用 CSS 变量覆盖主题色、侧边栏缩进、代码字号，并为 giscus 评论容器定尺寸。 |
| `//npm.elemecdn.com/docsify-themeable@0/...` | 主题基础库，提供整套 CSS 变量（如 `--theme-hue`、`--sidebar-nav-indent`、`--code-font-size`），是配色系统的「底座」。 |
| `//npm.elemecdn.com/docsify-darklight-theme@3/...` | 明暗切换库，提供切换按钮 UI，并在两套配色表之间切换。 |
| `//npm.elemecdn.com/katex@latest/dist/katex.min.css` | KaTeX 数学公式所需的字形与样式（数学字体）。 |

> 提示：CDN 资源不在仓库里，无法用永久链接引用其内部代码；但它们在 `index.html` 中的 `<link>` / `<script>` 行是可引用的真实源码点，本讲会逐行指给你看。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应规格中的四块：`$docsify 配置项`、`主题与明暗切换`、`katex 与字体`、`自定义 main.css`。

### 4.1 `$docsify` 配置项

#### 4.1.1 概念说明

`window.$docsify` 是一个普通的 JavaScript 对象字面量，挂在全局 `window` 上。Docsify 核心库启动时会读取它，把里面的每一个键当作一项「开关」或「参数」来调整渲染行为。

关键认知有两点：

1. **配置分两类**：一类由**核心库**直接消费（如 `name`、`repo`、`loadSidebar`、`subMaxLevel`、`auto2top`）；另一类是**某个插件的专属配置**（如 `search` 给搜索插件、`pagination` 给分页插件、`copyCode` 给复制代码插件）。判断一项配置属于谁，要看 `index.html` 里是否加载了对应的插件脚本。
2. **配置是声明式的**：你只写「想要什么」，不写「怎么做」。比如 `auto2top: true` 一行，背后是核心库在每次路由切换后把页面滚到顶部——你不必关心滚动逻辑。

#### 4.1.2 核心流程

```text
浏览器解析 index.html
        │
        ▼
执行内联 <script>：给 window.$docsify 赋值（配置就位）
        │
        ▼
加载 docsify.min.js（核心库启动，读取 window.$docsify）
        │
        ▼
按 <script> 顺序加载各插件脚本
   每个插件启动时，从同一个 window.$docsify 里
   取走属于自己的那一段配置（search / pagination / ...）
        │
        ▼
核心库渲染 Markdown 页面，应用所有配置
```

之所以「配置脚本必须在核心库之前」，正是因为核心库一启动就要读 `window.$docsify`；若那时对象还没赋值，配置就失效。

#### 4.1.3 源码精读

先看整个配置对象的全貌：

[docs/index.html:20-45](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L20-L45) —— 这是 `window.$docsify` 的完整定义，下面逐段拆解。

把配置项整理成一张「谁消费它」的表：

| 配置项 | 行号 | 消费者 | 作用 |
|--------|------|--------|------|
| `name` | 21 | 核心库 | 侧边栏顶部的站点名称 |
| `repo` | 22 | 核心库 | 右上角 GitHub 角标链接 |
| `loadSidebar: 'toc.md'` | 23 | 核心库 | 把 `toc.md` 作为整站侧边栏 |
| `subMaxLevel: 3` | 24 | 核心库 | 自动把正文 H1~H3 追加为侧边栏锚点 |
| `search` | 25-28 | search 插件 | 全文搜索配置 |
| `loadFooter: 'footer.md'` | 29 | 核心 + footer 插件 | 指定页脚文件（渲染由插件完成） |
| `auto2top: true` | 30 | 核心库 | 切换页面后自动滚到顶部 |
| `customPageTitle` | 31-33 | title 插件 | 给浏览器标签页标题加后缀 |
| `pagination` | 34-39 | pagination 插件 | 章节上下页按钮 |
| `copyCode` | 40-44 | copy-code 插件 | 代码块「复制」按钮文案 |

本讲规格要求重点掌握 `loadSidebar / subMaxLevel / search / pagination / copyCode` 这五项，逐一精读：

**搜索插件配置**

[docs/index.html:25-28](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L25-L28) —— `search` 对象。`paths: 'auto'` 表示自动扫描所有页面建立索引；`placeholder: '搜索文档内容'` 就是搜索框里那行灰字。对应的插件脚本是：

[docs/index.html:51](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L51) —— 加载 `docsify/lib/plugins/search.min.js`，它启动时会读 `window.$docsify.search`。

**分页插件配置**

[docs/index.html:34-39](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L34-L39) —— `pagination` 对象。`previousText` / `nextText` 是按钮文案；`crossChapter: true` 允许跨章节跳转；`crossChapterText: true` 让按钮显示目标章节名。对应脚本：

[docs/index.html:62](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L62) —— 加载 `docsify-pagination`。

**复制代码插件配置**

[docs/index.html:40-44](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L40-L44) —— `copyCode` 对象，三个键分别是「鼠标悬停时按钮文字 / 复制失败文案 / 复制成功文案」。对应脚本：

[docs/index.html:64](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L64) —— 加载 `docsify-copy-code@2.1.0`。

**侧边栏相关（核心自带）**

[docs/index.html:23-24](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L23-L24) —— `loadSidebar: 'toc.md'` 与 `subMaxLevel: 3`。`subMaxLevel` 的值 `3` 表示只把 H1、H2、H3 三级标题作为锚点收进侧边栏；改成 `2` 就只收 H1~H2，侧边栏会更简洁。

#### 4.1.4 代码实践

**实践目标**：亲手修改一项「文案类」配置，观察它在页面上的落点，建立「配置 → 界面」的直觉。

**操作步骤**：

1. 确认本地已按 u1-l2 启动站点：`docsify serve docs`，浏览器打开 `http://localhost:3000`。
2. 打开 `docs/index.html`，找到 [第 27 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L27) 的 `placeholder: '搜索文档内容'`。
3. 把文案改成你自己的，例如 `placeholder: '输入关键字试试'`。
4. 保存文件，浏览器刷新页面（`docsify serve` 会自动加载最新文件，无需重启）。
5. 观察侧边栏顶部的搜索框。

**需要观察的现象**：搜索框里的灰色占位文字应变成你新写的文案。

**预期结果**：占位符即时更新，证明 `search.placeholder` 这一项配置确实由搜索插件消费、并渲染到了搜索框。

> 如果修改后没有任何变化：检查你是否改对了 `<script>` 块（注意是第 19~46 行那个内联脚本，不是别处）；检查浏览器是否命中了缓存（可强制刷新 Ctrl+Shift+R）。

#### 4.1.5 小练习与答案

**练习 1**：如果你想把侧边栏里自动收录的正文标题从「H1~H3」改成「只收录 H1~H2」，应该改哪个配置？改成什么值？

> **参考答案**：改 [第 24 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L24) 的 `subMaxLevel: 3` 为 `subMaxLevel: 2`。

**练习 2**：`copyCode` 配置由谁消费？你怎么在 `index.html` 里找到对应的脚本？

> **参考答案**：由 `docsify-copy-code` 插件消费。在 `index.html` 的 `<script>` 列表中能找到 [第 64 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L64) 加载的 `docsify-copy-code@2.1.0/dist/docsify-copy-code.min.js`。

---

### 4.2 主题与明暗切换

#### 4.2.1 概念说明

本仓库的「主题」并不是一个文件，而是**三层资源的叠加**：

1. **`docsify-themeable`（底座）**：提供一整套 CSS 变量（如 `--theme-hue`、`--sidebar-nav-indent`）。整站配色都由这些变量推导而来。
2. **明暗两套配色表**：`theme-simple.css`（亮色）和 `theme-simple-dark.css`（暗色），它们给同一批变量填不同的值。
3. **`docsify-darklight-theme`（切换器）**：在侧边栏放一个明暗切换按钮，并在两套配色表之间切换；选择会持久化，刷新后保持。

这三层缺一不可：没有底座就没有可调的变量；没有两套配色表就没有「明」「暗」可切；没有切换器用户就无法手动切换。

#### 4.2.2 核心流程

明暗切换的底层，是浏览器原生的「**备选样式表**」机制：

```text
<head> 里有两条带 title 的样式表：
  ┌─ theme-simple.css        title="light"  rel="stylesheet"            ← 默认激活（首选）
  └─ theme-simple-dark.css   title="dark"   rel="stylesheet alternative" ← 默认关闭（备选）

浏览器规则：同一组带 title 的样式表，同一时刻只激活一条。
点击切换按钮 → darklight-theme 的 JS 把 dark 那条 enabled，
               把 light 那条 disabled → 配色整体翻转。
选择写入 localStorage → 下次进站自动恢复。
```

这正是为什么第 11 行的暗色表要写成 `rel="stylesheet alternative"`、并带 `title="dark"`——少了 `alternative` 它就会和亮色表同时生效，配色会打架。

#### 4.2.3 源码精读

**三份样式表的加载顺序**（在 `<head>` 中）：

[docs/index.html:9-13](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L9-L13) —— 依次加载：darklight 主题基础样式、亮色表、暗色表、KaTeX 样式、本地 `main.css`。

逐行看明暗两套表的关键差异：

[docs/index.html:10](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L10) —— 亮色表 `theme-simple.css`，`title="light"`，是默认激活的首选样式表。

[docs/index.html:11](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L11) —— 暗色表 `theme-simple-dark.css`，`title="dark"`，`rel="stylesheet alternative"` 把它标为「备选」，默认关闭，等待切换器激活。

**切换器与底座的脚本加载**（在 `<body>` 中，核心库之后）：

[docs/index.html:47-50](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47-L50) —— 这四行是主题系统的「加载序」：

- 第 47 行先加载 `docsify.min.js`（核心，渲染引擎）；
- 第 48 行加载 `docsify-themeable`（变量底座）；
- 第 49、50 行加载 `docsify-darklight-theme` 的两份脚本（切换器 UI 与逻辑）。

顺序不可乱：底座必须先于切换器，切换器才能在底座提供的变量体系上工作。

#### 4.2.4 代码实践

**实践目标**：亲手切换明暗主题，并定位「选择被记在哪里」，验证它确实是持久化的。

**操作步骤**：

1. 启动站点（`docsify serve docs`），浏览器打开首页。
2. 在侧边栏找到明暗切换按钮（一个日月图标），点击切换到暗色。
3. 观察整站配色翻转（背景变深、文字变浅）。
4. 按 `F12` 打开浏览器开发者工具 → Application（应用）标签 → 左侧 `Local Storage` → 选 `http://localhost:3000`。
5. 找到与主题相关的键（darklight 主题通常以 `DARK_LIGHT_THEME` 之类命名），观察它的值。
6. **硬刷新页面**（Ctrl+Shift+R），看主题是否保持。

**需要观察的现象**：切换后配色立刻翻转；Local Storage 里出现一条记录当前主题的键值；刷新后主题不回退。

**预期结果**：证明明暗选择通过 localStorage 持久化，下次访问自动恢复。

> 待本地验证：具体的 localStorage 键名以你本地开发者工具中实际看到的为准（不同版本的 darklight-theme 可能略有差异）。重点是确认「存在这样一条持久化记录」。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 11 行暗色表的 `rel="stylesheet alternative"` 改成普通的 `rel="stylesheet"`，会发生什么？

> **参考答案**：暗色表将不再是「备选」而是和亮色表**同时激活**，两套配色规则会同时生效。由于后加载的暗色表在 CSS 层叠中通常胜出，页面可能整体偏暗、且切换按钮失去意义——这破坏了「二选一」的切换机制。

**练习 2**：为什么 `docsify-themeable` 的脚本（第 48 行）必须排在 `docsify-darklight-theme`（第 49~50 行）之前？

> **参考答案**：themeable 是「底座」，提供整套 CSS 变量体系；darklight 是建立在它之上的「切换器」。底座先就位，切换器才能在其变量体系上正确工作，否则切换时可能找不到该覆盖的变量。

---

### 4.3 KaTeX 与字体

#### 4.3.1 概念说明

编译原理文档里常常出现数学公式（比如文法、状态机、复杂度的表达）。本仓库用 **KaTeX** 来渲染 LaTeX 语法的公式。KaTeX 的接入需要两样东西配合：

1. **渲染逻辑**：`docsify-katex` 插件，它在 Markdown 渲染时识别 `$...$`（行内）和 `$$...$$`（独立块）语法，调用 KaTeX 把公式转成 HTML。
2. **字形与样式**：`katex.min.css`，它通过 `@font-face` 声明加载一整套**数学字体**（含大量数学符号字形），并为公式排版定尺寸。

二者缺一不可：只有渲染逻辑没有字体，公式里的积分号、求和号会显示成「豆腐块」（缺字方框）；只有字体没有渲染逻辑，`$...$` 不会被解析。

> 注意：本讲规格里的「字体」特指 KaTeX 依赖的**数学符号字体**，由 `katex.min.css` 一并引入，而非正文字体。

#### 4.3.2 核心流程

```text
Markdown 原文:  ... 中缀表达式的项数 $O(n^2)$ ...
                      │
                      ▼
docsify-katex 插件扫描正文，匹配 $...$ / $$...$$
                      │
                      ▼
调用 KaTeX 把 LaTeX 源码编译成带 class 的 HTML 节点
                      │
                      ▼
浏览器套用 katex.min.css：用数学字体绘制符号、按排版规则对齐
                      │
                      ▼
页面上出现正确渲染的公式：O(n²)
```

#### 4.3.3 源码精读

**KaTeX 样式表（含数学字体）**

[docs/index.html:12](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L12) —— 加载 `katex@latest/dist/katex.min.css`。这一条 `<link>` 同时带来了公式排版样式与 `@font-face` 声明的数学字体。

**KaTeX 渲染插件脚本**

[docs/index.html:63](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L63) —— 加载 `docsify-katex.js`，这是真正识别 `$...$` 并调用 KaTeX 的渲染插件。

**对照：本仓库没有额外引入正文字体**。`<head>` 里只有 KaTeX 的字体 CSS，正文使用 docsify-themeable 默认的字体栈，因此「字体」在本仓库的语境下就是指 KaTeX 数学字体。

#### 4.3.4 代码实践

**实践目标**：在真实页面上写一个公式，验证 KaTeX 链路（插件 + 字体）确实通。

**操作步骤**：

1. 启动站点。
2. 任选一个会被渲染的 `.md`（例如 `docs/lv1-main/README.md`），在末尾加一行（示例代码，非项目原有内容）：
   ````markdown
   测试公式：行内 $a^2 + b^2 = c^2$，独立块：

   $$
   E = mc^2
   $$
   ````
3. 保存，浏览器打开对应页面。
4. 观察公式渲染情况：上标是否正确抬高、符号是否完整。

**需要观察的现象**：行内公式紧凑嵌在文字中，独立公式居中独占一行，上标 `²` 由数学字体正确绘制（而非普通文字的 `2`）。

**预期结果**：证明 `docsify-katex`（第 63 行）与 `katex.min.css`（第 12 行）协同工作正常。

> 待本地验证：若公式未渲染、原样显示 `$...$`，通常是 `docsify-katex.js` 没加载成功（检查第 63 行 CDN 是否可达）；若渲染了但符号是方框，则是 `katex.min.css` 字体未加载（检查第 12 行）。

> 注意：实践结束后请把测试内容删掉，不要把临时测试公式提交进仓库。

#### 4.3.5 小练习与答案

**练习 1**：如果某天页面上公式渲染正常，但积分号 `∫`、求和号 `∑` 显示成方框，最可能是哪一条资源出了问题？

> **参考答案**：是 [第 12 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L12) 的 `katex.min.css` 没加载成功——它负责通过 `@font-face` 引入数学符号字体，缺了它符号就没有字形可显示。

**练习 2**：识别 `$...$` 并把它转成公式 HTML 的是哪一份资源？样式和字形又由谁提供？

> **参考答案**：识别与转换由 [第 63 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L63) 的 `docsify-katex.js` 插件完成；样式与字形由 [第 12 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L12) 的 `katex.min.css` 提供。

---

### 4.4 自定义 `main.css`

#### 4.4.1 概念说明

`docs/assets/css/main.css` 是本仓库**唯一**自己写的样式文件，只有 35 行，却决定了整站的几处关键观感。它的核心写法是「**覆盖 CSS 变量**」：不改 docsify-themeable 的源码，只在 `:root` 上把同名变量重新赋值，底座推导出的所有派生样式就会跟着变。

之所以它「说了算」，靠的是两条 CSS 规则：

1. **加载顺序**：`main.css` 是 `<head>` 里最后一条样式表（[第 13 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L13)）。当选择器优先级相同时，**后加载者胜出**，因此它能覆盖前面 CDN 样式表里的同名变量。
2. **CSS 层叠**：变量定义在 `:root` 上是全局的；后赋的值会覆盖先赋的同名变量，所有用 `var(...)` 引用它的地方都自动更新。

#### 4.4.2 核心流程

```text
docsify-themeable 在 :root 定义了一堆变量并给默认值
        │
        ▼
theme-simple(-dark).css 给这些变量填亮(暗)配色值
        │
        ▼
main.css 在 :root 把部分变量再次赋值（覆盖）
   例如 --theme-hue / --code-font-size / --sidebar-nav-indent
        │
        ▼
浏览器按「后加载、同优先级则后者胜」解析
        │
        ▼
所有 var(...) 引用处拿到的是 main.css 里的新值
→ 主题色、代码字号、侧边栏缩进等随之改变
```

补充一点数学：本仓库主题色用 HSB 模型描述，主色由三个变量决定——色相 `--theme-hue`、饱和度 `--theme-saturation`、明度 `--theme-lightness`。docsify-themeable 内部据此推导出整套配色，其关系可粗略记为：

\[
\text{主色} = \mathrm{HSL}\bigl(H,\, S,\, L\bigr),\quad H=\text{--theme-hue},\ S=\text{--theme-saturation},\ L=\text{--theme-lightness}
\]

只要改这三个分量，整站主色（链接、强调、侧边栏高亮等）就会整体迁移。

#### 4.4.3 源码精读

完整文件只有一段，分四块来看：

**① 主题色（HSB 三分量）**

[docs/assets/css/main.css:1-6](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L1-L6) —— 把主色定为色相 204、饱和度 85%、明度 50%。

- `--theme-hue: 204`：色相 204 大致是**青蓝色**（0=红、120=绿、240=蓝，204 偏蓝）。
- `--theme-saturation: 85%`：高饱和，颜色鲜艳。
- `--theme-lightness: 50%`：中等明度，不偏暗也不偏淡。

这就是本站链接、强调色那一抹蓝的来源。

**② 侧边栏变量**

[docs/assets/css/main.css:8-18](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L8-L18) —— 调整侧边栏：缩进 `--sidebar-nav-indent: 0.7em`、页链接内边距、站点名字重 `normal`，并把 `.sidebar-nav` 字号设为 `0.9em`（比正文略小）。

**③ 代码块字号**

[docs/assets/css/main.css:20-23](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L20-L23) —— `--code-font-size: calc(var(--font-size-m) * 0.9)`。这里用 `calc()` 做了一次**相对计算**：代码字号 = 中号正文字号 × 0.9。`--font-size-m` 是 themeable 底座提供的基准字号变量，这样代码字号会随正文基准联动缩放。

**④ giscus 评论容器**

[docs/assets/css/main.css:25-34](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L25-L34) —— 给评论 iframe 容器 `.giscus-frame` 定尺寸：最大宽度取 `--content-max-width`（与正文同宽）、居中、左右各留 `45px` 内边距，让评论区和正文对齐。

#### 4.4.4 代码实践

**实践目标**：通过改一个 CSS 变量，感受「改一处、动一片」的变量化主题机制。

**操作步骤**：

1. 启动站点。
2. 打开 `docs/assets/css/main.css`，找到 [第 3 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L3) 的 `--theme-hue: 204;`。
3. 把它改成另一个色相值，例如 `--theme-hue: 0;`（红色）或 `--theme-hue: 120;`（绿色）。
4. 保存，浏览器刷新首页。
5. 观察侧边栏高亮、正文里的链接颜色。

**需要观察的现象**：全站的强调色（链接、侧边栏当前项、按钮等）整体从青蓝变成你设的色相。

**预期结果**：证明主色完全由 `--theme-hue` 这一个变量驱动，改一行即可换肤。

> 进阶玩法：把 [第 22 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L22) 的 `* 0.9` 改成 `* 1.1`，观察代码块字号变大；再把 `.sidebar-nav` 的 `font-size: 0.9em`（[第 17 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L17)）改成 `1.1em`，观察侧边栏文字变大。这就是「调整正文字号 / 各处字号」的统一入口。

> 注意：实践结束后请还原修改，避免把实验性样式提交进仓库。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `main.css` 必须放在 `<head>` 所有 CDN 样式表**之后**（第 13 行）加载？如果放到最前面会怎样？

> **参考答案**：CSS 层叠规则下，选择器优先级相同时**后加载者覆盖先加载者**。`main.css` 靠的就是「排在最后」来覆盖 themeable / theme-simple 里的同名变量。若放到最前面，它会被后面的 CDN 样式表覆盖回去，自定义就失效了。

**练习 2**：本仓库把主色设为青蓝色，对应 `--theme-hue` 的值是多少？想改成绿色应设为多少？

> **参考答案**：青蓝色对应 `--theme-hue: 204`（[第 3 行](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/css/main.css#L3)）。绿色色相约为 120，改成 `--theme-hue: 120;` 即可。

**练习 3**：代码字号变量 `--code-font-size` 用到了 `var(--font-size-m)`，这个 `--font-size-m` 是 `main.css` 里定义的吗？

> **参考答案**：不是。`--font-size-m` 由 docsify-themeable 底座提供（默认基准字号），`main.css` 只是**引用**它来做相对计算（`calc(var(--font-size-m) * 0.9)`）。这体现了「站在底座肩膀上做微调」的思路。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**给站点换一套轻量皮肤**」的小任务：

**任务**：在不引入任何新文件的前提下，仅通过修改 `docs/index.html` 与 `docs/assets/css/main.css` 两处，让站点呈现一组自定义外观，并验证每一处改动都生效。

**要求**：

1. **配置层（4.1）**：把 `search.placeholder` 改成自定义文案（如 `搜索编译实验内容`），刷新后确认搜索框占位符更新。
2. **主题层（4.2）**：在侧边栏点击明暗切换按钮，分别截图（或描述）亮色与暗色两种状态；用开发者工具确认选择被写入 localStorage。
3. **公式层（4.3）**：在某页面临时加一个 `$$ ... $$` 公式，确认 KaTeX 正常渲染后**删掉**它。
4. **样式层（4.4）**：把 `--theme-hue` 改成一个你喜欢的色相（如 280 紫），刷新后确认链接、侧边栏高亮整体变色；再把代码字号系数从 `0.9` 调成 `1.05`，确认代码块字号变大。

**验收标准**：四项改动各自可观察、可解释；能说清每项改动分别由「哪个配置 / 哪条样式表 / 哪个插件 / 哪个 CSS 变量」负责。完成后**还原所有修改**，保持仓库干净。

> 这个综合实践的关键不是「改得多」，而是建立「配置 → 插件 → 样式表 → CSS 变量」这条因果链的直觉：看到一处界面效果，能反向定位到它由哪一层资源决定。

## 6. 本讲小结

- `window.$docsify` 是声明式配置对象，分「核心消费」与「插件专属」两类；改文案类配置（`search.placeholder`、`copyCode`、`pagination` 文案）最安全、最直观。
- 配置脚本必须排在核心库之前加载，否则核心读不到配置；每个插件从同一个对象里取走属于自己的那一段。
- 主题是三层叠加：themeable 提供变量底座、`theme-simple` 与 `theme-simple-dark` 是明暗两套配色表、darklight-theme 是切换器；明暗切换的底层是带 `title` 的备选样式表机制。
- KaTeX 公式需要「渲染插件 + 数学字体 CSS」配合：`docsify-katex.js` 负责识别 `$...$`，`katex.min.css` 负责字形与排版。
- `main.css` 靠「加载在最后 + 覆盖 `:root` 变量」生效；主色由 `--theme-hue/saturation/lightness` 三个 HSB 分量驱动，改一行即可换肤。
- CSS 变量是整站的「总开关」：`--code-font-size`、`--sidebar-nav-indent`、`--content-max-width` 等都可在 `main.css` 里微调。

## 7. 下一步学习建议

本讲把 `index.html` 的「配置与主题」讲透了，但里面还有两类资源我们只点到为止，正适合作为下一站的入口：

- **自定义语法高亮**：第 59 行加载的本地脚本 `assets/js/prism-koopa.js`，是本仓库为 Koopa IR 专门写的一套 Prism 语法规则。推荐接着学习 **u2-l2《Koopa IR 语法高亮插件》**，看 Prism 的 token + 正则是怎么定义出一种新语言的着色规则的。
- **插件式扩展**：第 65、81 行加载的本地 `sidebar.js`、`giscus.js` 揭示了 Docsify 的插件钩子机制。推荐接着学习 **u2-l3《侧边栏同步与评论插件》**，理解 `doneEach` 等生命周期钩子如何操纵 DOM。

读完这两篇，你就能完整覆盖 `index.html` 里**所有本地资源**，从「会用 Docsify」进阶到「能扩展 Docsify」。
