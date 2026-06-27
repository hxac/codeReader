# 侧边栏同步与评论插件

## 1. 本讲目标

本讲承接 [u2-l1（Docsify 配置与主题样式系统）](u2-l1-docsify-config-and-theme.md) 与 [u2-l2（Koopa IR 语法高亮插件）](u2-l2-koopa-syntax-highlight.md)。

在前两讲里，你已经知道 `window.$docsify` 是声明式全局配置对象，也知道 Prism 高亮语言可以「注册」到全局被 Docsify 使用。但这两类扩展都是**被动**的——配好就等 Docsify 来读。本讲要讲的是**主动**扩展：**Docsify 插件（plugin）**。

学完本讲你应该能够：

1. 说清 Docsify 插件的生命周期钩子（`init` / `beforeEach` / `afterEach` / `doneEach` / `mounted` / `ready`）分别在何时触发，以及它们各自适合做什么。
2. 读懂 `docs/assets/js/sidebar.js`：它如何用 `doneEach` 钩子在每次切换章节后，把侧边栏里「当前激活项」滚动到可视区。
3. 读懂 `docs/assets/js/giscus.js`：它如何用 `ready` + `doneEach` 两个钩子，把 GitHub Discussions 评论框搬进正文区，并按当前页面 / 当前明暗主题同步评论框。
4. 自己写一个最小插件并挂到 `$docsify.plugins` 上验证钩子被触发。

---

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个概念。

### 2.1 单页应用（SPA）与「每次都重新渲染」

Docsify 是一个**单页应用（Single Page Application, SPA）**。浏览器从头到尾只加载一个 `index.html` 外壳（见 [u1-l4](u1-l4-entry-and-navigation.md)）。当你点击侧边栏切换章节时，页面**不会整体刷新**，而是由 Docsify 的 JS 抓取对应 `.md` 文件、解析成 HTML、再替换掉正文区的 DOM。

这就带来一个关键事实：**每次切换章节，正文区都会被重新生成一遍**。任何依赖「当前正文」或「当前侧边栏激活项」的逻辑，都不能只跑一次，而必须在**每次渲染完成后**再跑。Docsify 用「钩子」来给你这个时机。

### 2.2 浏览器 DOM 与坐标

本讲源码会用到几个浏览器 API，先建立直觉：

- `document.querySelector(sel)`：用 CSS 选择器 `sel` 在页面里找到**第一个**匹配的元素节点。
- `el.getBoundingClientRect()`：返回元素相对于**浏览器可视窗口（viewport）**的位置，其中 `.top` 是元素顶端到窗口顶部的距离（像素）。
- `el.scrollIntoView()`：把元素滚动到可视区内。
- `el.appendChild(child)`：把节点 `child` 搬到 `el` 内部的末尾。
- `el.classList.add('x')`：给元素加上 CSS 类 `x`。
- `location.hash`：地址栏 `#` 后面的部分，Docsify 用它当作当前路由。

### 2.3 Giscus 是什么

**Giscus** 是一个第三方评论服务：它把 GitHub 仓库的 **Discussions（讨论区）**当作评论存储，访客在网页上发表的评论，实际上是发到该仓库的某个 Discussion 分类下。它的工作方式是在页面里嵌一个 `<iframe>`（由 `https://giscus.app/client.js` 创建），评论数据都通过这个 iframe 与 GitHub 通信。

在一个普通多页网站里，把 Giscus 的 `<script>` 直接贴到每个页面底部即可。但在 Docsify 这种 SPA 里，评论框需要随着「当前是哪一页」「当前是亮色还是暗色主题」动态调整——这正是本讲 `giscus.js` 插件要解决的问题。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/assets/js/sidebar.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js) | 侧边栏滚动同步插件：每次渲染后把当前激活的侧边栏条目滚动到可视区。 |
| [docs/assets/js/giscus.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js) | 评论插件：把 Giscus 评论框搬进正文区，并按页面 / 主题同步。 |
| [docs/index.html](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html) | 站点外壳：按顺序加载 Docsify 核心、各插件，以及 Giscus 客户端脚本。 |

两个插件文件都极其短小（`sidebar.js` 50 行，`giscus.js` 48 行），但它们是仓库里**唯二**展示「Docsify 插件机制」的本地代码，是理解 Docsify 扩展生命周期的最佳样本。

---

## 4. 核心概念与源码讲解

### 4.1 `$docsify.plugins` 钩子机制

#### 4.1.1 概念说明

Docsify 插件就是一个**普通函数**，签名为：

```js
function myPlugin(hook, vm) { ... }
```

- `hook`：**生命周期钩子绑定器**。你调用 `hook.xxx(fn)` 来注册「在 xxx 时机执行 fn」。
- `vm`：Docsify 应用实例（可拿到当前路由 `vm.route` 等）。本讲的两个插件都没用到它，只用到了 `hook`。

注册插件的方式是把这个函数**推入** `window.$docsify.plugins` 数组：

```js
$docsify.plugins.push(myPlugin)
```

Docsify 核心在渲染流程的各个关键节点，会遍历 `plugins` 数组里每个插件先前用 `hook.xxx(fn)` 登记的回调并依次调用它们。

Docsify 提供的钩子（按一次章节渲染的时间顺序）：

| 钩子 | 触发时机 | 典型用途 |
| --- | --- | --- |
| `init` | 脚本加载、Docsify 初始化时，**只触发一次** | 一次性全局设置 |
| `beforeEach` | 每次解析 Markdown **之前**；收到原始 Markdown 文本 | 改写正文 Markdown |
| `afterEach` | 每次把 Markdown 解析成 HTML **之后**；收到 HTML | 改写正文 HTML |
| `doneEach` | 每次渲染**完成**、DOM 已更新**之后**；无参数 | 操作已渲染好的 DOM |
| `mounted` | Docsify 挂载到页面时，**只触发一次** | 初始化依赖 DOM 的逻辑 |
| `ready` | 首次渲染完成、Docsify 就绪，**只触发一次** | 一次性的、需要等首屏就绪的逻辑 |

> 记忆口诀：**「改内容用 before/afterEach，操作 DOM 用 doneEach，只跑一次用 ready/mounted」**。本讲两个插件正是分别用到了 `doneEach` 与 `ready`+`doneEach`。

#### 4.1.2 核心流程

插件能生效，依赖一条**时序链**：

```text
index.html 内联脚本定义 window.$docsify
        │
        ▼
加载 docsify.min.js（核心）：读取配置，初始化 $docsify.plugins 为数组，开始【异步】抓取首个 .md
        │
        ▼  （核心已就绪，但首次渲染是异步的，还没完成）
同步加载 sidebar.js / giscus.js：各自执行 $docsify.plugins.push(...)
        │
        ▼  （plugins 数组里现在有了我们的插件）
首次渲染完成 → Docsify 按注册顺序调用各插件的 ready / doneEach 等钩子
        │
        ▼
此后每次切换章节 → 只触发 beforeEach → afterEach → doneEach
```

关键点：**两个插件脚本是同步 `<script>`，它们在页面加载阶段就执行完毕，早于 Docsify 首次（异步）渲染真正触发钩子的时刻**。因此 push 进去的插件一定会被随后的钩子调用看到。如果反过来——把插件脚本写成 `defer`/`async` 且加载得很晚，就有可能错过 `ready`/首次 `doneEach`。

#### 4.1.3 源码精读

两个插件文件都在末尾用同一句方式注册自己：

- [docs/assets/js/sidebar.js:50-50](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js#L50-L50)：`$docsify.plugins.push(scrollBarSyncPlugin)` —— 把滚动同步插件推入数组。
- [docs/assets/js/giscus.js:48-48](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js#L48-L48)：`$docsify.plugins.push(giscusPlugin)` —— 把评论插件推入数组。

注意它们引用的是全局变量 `$docsify`（即 `window.$docsify`），这个对象在 [docs/index.html:19-46](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L19-L46) 的内联 `<script>` 里被定义。虽然该配置对象里**没有**显式写 `plugins` 字段，但 Docsify 核心在初始化时会保证 `$docsify.plugins` 是一个数组，所以 `.push(...)` 不会报错。

再看加载顺序，在 [docs/index.html:65-81](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L65-L81) 中：

- 第 47 行已先加载 `docsify.min.js`（核心）；
- 第 65 行加载 `assets/js/sidebar.js`；
- 第 66–80 行加载 Giscus 客户端 `client.js`（带 `async`）；
- 第 81 行加载 `assets/js/giscus.js`。

这个顺序印证了 4.1.2 的时序链：核心先就绪并开好 `.plugins` 数组，两个本地插件随后把自己 push 进去。

#### 4.1.4 代码实践

> 这是本讲指定的主实践任务：在 `doneEach` 钩子里加日志，验证钩子被触发。

1. **实践目标**：亲眼确认 `doneEach` 在「每次切换章节后」都会被调用。
2. **操作步骤**：
   - 打开 [docs/assets/js/sidebar.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js)，在 `hook.doneEach(() => {` 之后、`const activeNode = getActiveNode()` 之前，插入一行（示例代码）：
     ```js
     // 示例代码：打印当前路由
     console.log('[doneEach] route =', location.hash)
     ```
   - 启动站点：`docsify serve docs`（见 [u1-l2](u1-l2-run-locally.md)）。
   - 浏览器打开本地地址，按 `F12` 打开开发者工具的 Console 面板。
   - 在侧边栏点击切换 3 个不同章节。
3. **需要观察的现象**：每次点击切换章节后，Console 都会新打印一行 `[doneEach] route = ...`，其中的 `...` 随地址栏 `#` 后的内容变化。
4. **预期结果**：切换 N 次章节，就有 N 条（首屏加载时还会有 1 条）日志，证明 `doneEach` 是「每次渲染后」触发而非只触发一次。
5. 验证完后**记得删掉这行日志**（本仓库是文档项目，不要留下调试代码）。

#### 4.1.5 小练习与答案

**练习 1**：如果想让一段逻辑「整个站点生命周期里只跑一次」，应该用 `doneEach` 还是 `ready`？为什么？

> **答案**：用 `ready`。`ready` 只在首次渲染完成、Docsify 就绪时触发一次；`doneEach` 每次切换章节都会触发。`giscus.js` 里「把评论框搬进正文区」只需做一次，所以放在 `ready`（见 4.3）。

**练习 2**：`$docsify.plugins.push(fn)` 中的 `fn` 接收两个参数 `(hook, vm)`，本讲两个插件用到了 `vm` 吗？

> **答案**：都没用到。两个插件的函数签名虽然写了 `(hook, vm)`，但函数体内只用了 `hook`。`vm` 是 Docsify 实例，留作需要读取路由 / 配置时备用。

---

### 4.2 sidebar 滚动同步实现

#### 4.2.1 概念说明

本仓库的侧边栏（`toc.md`，见 [u1-l4](u1-l4-entry-and-navigation.md)）很长——它覆盖 Lv0 到 Lv9+ 十几个实验阶段，加上 `subMaxLevel: 3` 把正文 H1~H3 也展开成锚点，侧边栏条目多达上百项，高度远超浏览器窗口。

问题随之而来：当你点击或滚动到很靠下的章节时，**侧边栏里对应的激活项可能位于可视区之外（被滚动到下方看不见）**，读者难以感知「我现在在哪里」。

`sidebar.js` 就是为解决这个问题：**每次渲染完成后，如果当前激活的侧边栏条目在可视区下方不可见，就把它滚动进可视区**。

> 说明：该文件第 1–2 行注释 `Reference: https://github.com/iPeng6/docsify-sidebar-collapse` / `Modified by MaxXing.` 表明它是在一个开源插件基础上裁改而来的精简版本。

#### 4.2.2 核心流程

```text
doneEach 触发（每次渲染完成）
        │
        ▼
getActiveNode()              # 找到当前激活的侧边栏 <li> 节点
   ├─ 先查 .sidebar-nav .active
   └─ 查不到 → 用 location.hash 反查对应 <a>，再向上找 <li> 并补 .active
        │
        ▼
syncScrollTop(activeNode)    # 若该节点在窗口下方之外，scrollIntoView()
```

只有一个钩子 `doneEach`，逻辑分两步：**找激活节点 → 按需滚动**。

#### 4.2.3 源码精读

插件主体非常短，见 [docs/assets/js/sidebar.js:4-9](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js#L4-L9)：

```js
const scrollBarSyncPlugin = (hook, vm) => {
  hook.doneEach(() => {
    const activeNode = getActiveNode()
    syncScrollTop(activeNode)
  })
}
```

它在 `doneEach` 里依次调用两个辅助函数。

**第一步：按需滚动** [docs/assets/js/sidebar.js:11-18](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js#L11-L18)：

```js
const syncScrollTop = (activeNode) => {
  if (activeNode) {
    const curTop = activeNode.getBoundingClientRect().top
    if (curTop > window.innerHeight) {
      activeNode.scrollIntoView()
    }
  }
}
```

- `getBoundingClientRect().top` 得到激活条目顶端相对**可视窗口**的纵坐标 `curTop`。
- 判定 `curTop > window.innerHeight`：即条目顶端已经在窗口下沿**更下方**（位于可视区下方、看不见），此时才调用 `scrollIntoView()` 把它滚进来。
- 注意：这个条件**只处理「在下方之外」**这一种情况，不处理「在上方之外」。这是该精简版的有意取舍。

**第二步：找激活节点** [docs/assets/js/sidebar.js:20-35](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js#L20-L35)：

```js
const getActiveNode = () => {
  let node = document.querySelector('.sidebar-nav .active')
  if (!node) {
    const curLink = document.querySelector(
      `.sidebar-nav a[href="${decodeURIComponent(location.hash).replace(/ /gi, '%20')}"]`
    )
    node = findTagParent(curLink, 'LI', 2)
    if (node) {
      node.classList.add('active')
    }
  }
  return node
}
```

- 首选：直接查 `.sidebar-nav .active`——Docsify 通常会把当前章节对应的侧边栏项加上 `.active` 类。
- 兜底：若没有 `.active`（例如跳到的是 `subMaxLevel` 展开出来的某级标题锚点，Docsify 未必标记），就用 `location.hash` 反查侧边栏里 `href` 匹配的 `<a>` 链接，再向上找到它所在的 `<li>` 并**手动补上** `.active`。
  - `decodeURIComponent(location.hash)` 把地址栏里的 `%20` 之类还原；
  - `.replace(/ /gi, '%20')` 再把空格统一成 `%20`，保证选择器和 `href` 写法一致、能匹配上。
  - 字符串模板里直接拼接出 `a[href="..."]` 选择器交给 `querySelector`。

**第三步：向上找父级 `<li>`** [docs/assets/js/sidebar.js:37-48](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js#L37-L48)：

```js
const findTagParent = (curNode, tagName, level) => {
  if (curNode && curNode.tagName === tagName) return curNode
  let l = 0
  while (curNode) {
    l++
    if (l > level) return
    if (curNode.parentNode.tagName === tagName) {
      return curNode.parentNode
    }
    curNode = curNode.parentNode
  }
}
```

- 从 `curNode` 出发沿 `parentNode` 向上走，最多走 `level`（这里传 `2`）层，找到第一个标签名为 `'LI'` 的祖先就返回它；超过层数还没找到就返回 `undefined`。
- 这层限制是为了避免无谓地一直向上爬到 `<body>`，只在「链接紧邻的列表项」范围内找。

#### 4.2.4 代码实践

1. **实践目标**：观察 `syncScrollTop` 的判定条件对滚动行为的影响。
2. **操作步骤**：
   - 在 [docs/assets/js/sidebar.js:14-14](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js#L14-L14) 的 `if (curTop > window.innerHeight)` 上方，加一行（示例代码）：
   ```js
   // 示例代码：观察激活条目的纵坐标与窗口高度
   console.log('[sync]', { curTop, innerHeight: window.innerHeight })
   ```
   - 启动站点，把侧边栏手动滚动到很靠下的某个章节并点击进入。
3. **需要观察的现象**：当激活条目在窗口下方之外时，`curTop` 会大于 `innerHeight`，随后页面滚动；当激活条目已在可视区时，`curTop` 较小、不触发滚动。
4. **预期结果**：你会看到一组 `{ curTop, innerHeight }` 日志，能直观对应「何时滚动、何时不滚动」。
5. 验证后删掉调试代码。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `getActiveNode` 里要有一个「查不到 `.active` 就用 `location.hash` 反查」的兜底分支？直接只查 `.sidebar-nav .active` 行不行？

> **答案**：Docsify 对「正文标题锚点（`?id=` / hash 指向某个 H2、H3）」并不总是给侧边栏对应项加 `.active`，尤其在 `subMaxLevel` 展开的多级条目里。只查 `.active` 会漏掉这些情况，导致找不到激活节点、无法滚动。兜底分支用 `location.hash` 精确定位 `<a>` 再补 `.active`，保证覆盖更全。

**练习 2**：把 `findTagParent(curLink, 'LI', 2)` 的第三个参数改成 `1`，会怎样？

> **答案**：向上查找的层数从 2 减到 1。若该链接的 `<li>` 父级恰好在第 2 层（例如链接被多包了一层），改成 1 就会找不到、返回 `undefined`，进而无法给该条目补 `.active`。该参数控制「向上容忍多少层嵌套」。待本地验证具体条目的嵌套层级。

---

### 4.3 giscus 评论接入

#### 4.3.1 概念说明

Giscus 的客户端脚本 `https://giscus.app/client.js` 被 [docs/index.html:66-80](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L66-L80) 直接引入，并配置了仓库、分类、语言、主题等 `data-*` 属性。这段脚本会在 `<body>` 里插入一个 `.giscus` 容器，里面套着一个 `.giscus-frame` 的 `<iframe>`，评论就显示在这个 iframe 里。

但直接这样用，在 Docsify SPA 里有两个问题：

1. **位置不对**：Giscus 容器默认出现在 `<body>` 末尾，而 Docsify 的正文区是 `.content`，两者分离，评论框会游离在正文之外。
2. **不能随页面 / 主题变化**：切换章节后，评论框还是「原来那一页」的讨论；切换明暗主题后，评论框配色也不跟着变。

`giscus.js` 插件用 `ready` + `doneEach` 两个钩子分别解决这两件事：

- `ready`（只跑一次）：把 `.giscus` 容器搬到 `.content` 里，并初始化一次主题。
- `doneEach`（每次渲染后）：根据当前页面更新 iframe 的 `term` 参数（决定显示哪一页的评论），并给主题切换按钮绑定点击事件来同步评论框主题。

#### 4.3.2 核心流程

```text
client.js 在 <body> 注入 .giscus 容器（含 .giscus-frame iframe）
        │
        ▼
ready 钩子（仅一次）
   ├─ content.appendChild(giscusContainer)   # 把评论框搬进正文区
   └─ toggleGiscusTheme(...)                 # 按当前明暗初始化评论框主题
        │
        ▼
doneEach 钩子（每次切换章节后）
   ├─ setupGiscusTerm(iframe)                # 用 data-page 改写 iframe src 的 term=
   └─ themeToggle.addEventListener('click', ...)  # 点击主题按钮时同步评论框主题
```

两个关键改写都是对 iframe 的 `src` 做**字符串替换**：把 `term=...` 或 `theme=...` 这一段替换成新值，再写回 `src`，浏览器会按新 URL 重新加载评论 iframe。

#### 4.3.3 源码精读

插件主体见 [docs/assets/js/giscus.js:1-28](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js#L1-L28)：

```js
const giscusPlugin = (hook, vm) => {
  hook.ready(() => {
    const content = document.querySelector('.content')
    const giscusContainer = document.querySelector(`.giscus`)
    if (content && giscusContainer) {
      content.appendChild(giscusContainer)
    }
    const iframe = document.querySelector('.giscus-frame')
    const themeToggle = document.getElementById('docsify-darklight-theme')
    if (iframe && themeToggle) {
      toggleGiscusTheme(themeToggle, iframe)
    }
  })
  hook.doneEach(() => {
    const iframe = document.querySelector('.giscus-frame')
    if (iframe) {
      setupGiscusTerm(iframe)
      const themeToggle = document.getElementById('docsify-darklight-theme')
      if (themeToggle) {
        themeToggle.addEventListener('click', () => toggleGiscusTheme(themeToggle, iframe))
      }
    }
  })
}
```

- `ready` 段 [docs/assets/js/giscus.js:2-15](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js#L2-L15)：找到 `.content` 和 `.giscus`，用 `appendChild` 把评论容器搬进正文区；再读 `#docsify-darklight-theme`（主题切换按钮，见 [u2-l1](u2-l1-docsify-config-and-theme.md)）和 iframe，调用 `toggleGiscusTheme` 做一次初始主题同步。两处都做了 `if (...)` 存在性保护，避免元素还没就绪时报错。
- `doneEach` 段 [docs/assets/js/giscus.js:16-27](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js#L16-L27)：每次切章节后，调 `setupGiscusTerm(iframe)` 让评论框指向当前页，并给主题按钮 `addEventListener('click', ...)` 绑定同步逻辑。

**按页面更新评论主题 `setupGiscusTerm`** [docs/assets/js/giscus.js:30-36](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js#L30-L36)：

```js
const setupGiscusTerm = (iframe) => {
  const src = iframe.getAttribute('src')
  const term = document.body.getAttribute('data-page').replace(/\.\w+$/, '')
  const newSrc = src.replace(/term=[^&]*/, `term=${encodeURIComponent(term)}`)
  iframe.setAttribute('src', newSrc)
}
```

- Docsify 会把当前页面路径写到 `<body>` 的 `data-page` 属性（如 `docs/lv1-main/structure.md`）。
- `.replace(/\.\w+$/, '')` 去掉末尾的文件扩展名（如 `.md`），得到干净的页面标识 `term`。
- 正则 `/term=[^&]*/` 匹配 iframe `src` 里 `term=` 开始、到下一个 `&` 或串尾的一段，替换成新的 `term=<当前页>`（`encodeURIComponent` 转义特殊字符）。Giscus 用 `term` 把「一个页面」映射到「一个 Discussion 讨论串」，所以每个页面有各自独立的评论。

**按主题切换评论配色 `toggleGiscusTheme`** [docs/assets/js/giscus.js:38-46](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js#L38-L46)：

```js
const toggleGiscusTheme = (toggle, iframe) => {
  const isDark = toggle.getAttribute('data-link-title') === 'dark'
  const theme = isDark ? 'dark_dimmed' : 'light'
  const src = iframe.getAttribute('src')
  const newSrc = src.replace(/theme=[^&]*/, `theme=${theme}`)
  iframe.setAttribute('src', newSrc)
}
```

- `docsify-darklight-theme` 把当前主题名（`light` 或 `dark`）写在切换按钮的 `data-link-title` 属性里。
- 据此映射成 Giscus 认识的主题名：暗色用 `dark_dimmed`，亮色用 `light`。
- 同样用 `src.replace(/theme=[^&]*/, ...)` 替换 iframe 的 `theme=` 段，重载 iframe 即可换肤。

最后注册插件 [docs/assets/js/giscus.js:48-48](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js#L48-L48)：`$docsify.plugins.push(giscusPlugin)`。

> 补充观察：`doneEach` 里每次都会 `addEventListener('click', ...)` 再绑一次。在多次切换章节后，主题按钮上会累积多个监听器，点击一次会触发多次 `toggleGiscusTheme`。这是阅读型观察，理解钩子「每次都跑」的副作用即可，不必修改。

#### 4.3.4 代码实践

1. **实践目标**：直观看到评论框的 `term` 会随当前页面变化。
2. **操作步骤**：
   - 启动站点（`docsify serve docs`）并打开开发者工具 Console。
   - 进入任意章节，在 Console 执行：
     ```js
     // 示例代码：查看评论 iframe 当前的 src
     console.log(document.querySelector('.giscus-frame')?.getAttribute('src'))
     ```
   - 记下输出里 `term=` 后面的值；再切换到另一个章节，重复执行上面这行。
3. **需要观察的现象**：两次输出里 `term=` 后的值不同，分别对应两个页面的路径（扩展名已被去掉）；`theme=` 后的值会随你点击右上角明暗切换按钮而变成 `light` 或 `dark_dimmed`。
4. **预期结果**：印证 `setupGiscusTerm` 用 `data-page` 改写了 `term=`，`toggleGiscusTheme` 改写了 `theme=`。
5. 若本地因网络无法加载 `giscus.app` 的 iframe，控制台拿不到 `.giscus-frame`，则记为「待本地验证」并改用阅读 `setupGiscusTerm` 源码理解 `term` 的来源。

#### 4.3.5 小练习与答案

**练习 1**：为什么搬移 `.giscus` 容器的逻辑放在 `ready`，而更新 `term` 的逻辑放在 `doneEach`？

> **答案**：搬容器是一次性布局操作，搬一次即可，所以放在只触发一次的 `ready`；而 `term` 依赖「当前是哪一页」，每次切换章节都会变，必须放在每次渲染后都触发的 `doneEach` 里更新。

**练习 2**：`setupGiscusTerm` 里为什么要有 `.replace(/\.\w+$/, '')`？去掉它会怎样？

> **答案**：`data-page` 形如 `docs/lv1-main/structure.md`，末尾 `.md` 是文件扩展名。Giscus 的 `term` 用作页面到讨论串的映射键，带不带 `.md` 通常都能工作，但去掉扩展名能得到更干净、稳定的标识，避免 `structure.md` 与 `structure` 被当成两个不同页面各开一个讨论串。去掉这行后，`term` 会带上扩展名，可能造成同一页面被识别成不同的讨论串。待本地验证 Giscus 实际行为。

---

## 5. 综合实践

把本讲三个模块串起来：**自己写一个最小的 Docsify 插件**，同时用到「钩子」和「DOM 操作」两件事。

1. **实践目标**：用一个新插件验证 `doneEach` 钩子触发，并练习查询 DOM。
2. **操作步骤**：
   - 新建文件 `docs/assets/js/mini-trace.js`（**示例代码**，仅用于练习）：
     ```js
     // 示例代码：最小 Docsify 插件
     const miniTracePlugin = (hook, vm) => {
       hook.doneEach(() => {
         const term = document.body.getAttribute('data-page') // 模块 3 的做法
         const active = document.querySelector('.sidebar-nav .active') // 模块 2 的做法
         console.log('[mini-trace]', {
           route: location.hash,                                   // 模块 1 的做法
           term,
           activeText: active ? active.textContent.trim() : '(无)',
         })
       })
     }
     $docsify.plugins.push(miniTracePlugin)
     ```
   - 在 [docs/index.html:81-81](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L81) 之后（即 `giscus.js` 之后）加一行：
     ```html
     <script src="assets/js/mini-trace.js"></script>
     ```
   - 启动站点，切换 3 个章节，观察 Console。
3. **需要观察的现象**：每次切换章节都打印一条 `[mini-trace]`，其中 `route` 随地址栏变化、`term` 随页面变化、`activeText` 是侧边栏当前激活项的文字。
4. **预期结果**：一条日志同时体现了「钩子在每次渲染后被调用」「DOM 可被查询」两件事——这正是 Docsify 插件的本质：**在生命周期钩子里操作 DOM**。
5. 练习完成后删除 `mini-trace.js` 及其在 `index.html` 的引用，保持仓库整洁。

---

## 6. 本讲小结

- Docsify 插件是签名为 `(hook, vm) => {}` 的普通函数，通过 `$docsify.plugins.push(fn)` 注册；核心在渲染各环节调用钩子。
- 钩子按用途分：**改内容用 `beforeEach` / `afterEach`，操作 DOM 用 `doneEach`，只跑一次用 `ready` / `mounted`**。
- `sidebar.js` 只用 `doneEach`：找到当前激活的侧边栏 `<li>`（先查 `.active`，再用 `location.hash` 兜底），若它在窗口下方之外就 `scrollIntoView()`。
- `giscus.js` 用 `ready` + `doneEach`：`ready` 把 `.giscus` 容器搬进 `.content` 并初始化主题；`doneEach` 用 `data-page` 改写 iframe `src` 的 `term=`（每页独立评论），并给主题按钮绑定点击事件同步 `theme=`。
- 两个改写都是对 iframe `src` 做 `String.replace`，靠重载 iframe 生效。
- 插件脚本必须在 Docsify 首次（异步）渲染触发钩子**之前**完成 `push`——靠同步 `<script>` 在页面加载阶段执行来保证。

---

## 7. 下一步学习建议

- **横向对比本单元三类扩展**：配置项（`window.$docsify`，[u2-l1](u2-l1-docsify-config-and-theme.md)）是被动声明，Prism 语法对象（[u2-l2](u2-l2-koopa-syntax-highlight.md)）是注册到全局被核心使用，而本讲的 `$docsify.plugins` 是主动挂钩——三者都是「Docsify 如何被扩展」的不同侧面，值得放在一起记。
- **进入第三单元**：插件写法你已经掌握，接下来 [u3-l1（实验分层与编译流水线映射）](u3-l1-lab-layering-and-pipeline.md) 会把视线从「站点工程」转回「文档内容」，看 Lv0–Lv9 如何对应 SysY→Koopa IR→RISC-V 的编译流水线。
- **如果对工程代码更感兴趣**：可跳到 [u4-l1（链接检查器总览、配置与入口）](u4-l1-linkchecker-config-and-entry.md)，那里是仓库里最有分量的 Python 代码 `scripts/check_links.py`。
- **延伸阅读**：Docsify 官方文档的「Write a plugin」一节列出了全部钩子的完整签名与参数，可作为本讲钩子表格的权威补充。
