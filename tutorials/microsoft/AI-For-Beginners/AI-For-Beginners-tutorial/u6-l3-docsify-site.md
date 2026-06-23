# 讲义：Docsify 文档站点

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 **Docsify** 是什么、它和 Jekyll/Hugo/Docusaurus 这类「静态站点生成器」有什么本质区别，以及为什么一个全是 Markdown 的课程仓库特别适合用它。
- 逐行看懂本仓库根目录 [`index.html`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/index.html) 里的 `window.$docsify` 配置，知道 `name`、`repo`、`relativePath`、`auto2top`、`routerMode` 各自的作用。
- 理解 `.nojekyll` 这个看似空白的文件为什么是「Docsify + GitHub Pages」组合里不可或缺的一环，并知道如何把这样的仓库部署成一个可在线浏览的文档站点。
- 在本地用一条 `python -m http.server` 命令把整个仓库跑成一个文档站点，亲手验证「纯前端渲染」的工作方式。

## 2. 前置知识

本讲依赖你在讲义 **u1-l2（仓库目录结构与内容组织）** 中建立的全局认知：本仓库的核心产物是 Markdown 讲义（`README.md`）与可执行 Notebook（`.ipynb`），目录按 `lessons/<编号-主题>/` 深层嵌套组织。请先回忆两个要点：

1. **找东西口诀**：学课程→`lessons`、跑示例→`examples`、看测验→`etc`。
2. **相对链接**：`README.md` 里的课程表大量使用 `./lessons/1-Intro/README.md` 这种**相对路径**指向子目录里的讲义。

有了这两点，你就能理解本讲反复出现的一个关键词——「相对路径」。如果你还接触过下面任何一个概念会更轻松，但没有也没关系，本讲会从零解释：

- **HTML / JavaScript**：浏览器能解析的网页语言。本讲只需要你认得 `<script>`、`<div>` 这类标签即可。
- **CDN（内容分发网络）**：一个公网地址，浏览器可以直接从它下载第三方库（如 Docsify 本身），不需要本地安装。
- **HTTP 服务**：把文件夹变成可通过 `http://` 访问的服务。`python -m http.server` 就是这样一个现成的工具。

## 3. 本讲源码地图

本讲只涉及仓库根目录与 `etc/` 下的极少量文件，全部加起来不到 80 行代码/文本：

| 文件 | 行数 | 作用 |
| :-- | :-- | :-- |
| [`index.html`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/index.html) | 29 行 | Docsify 的唯一入口页。声明了挂载点 `<div id="app">`、主题样式与 `window.$docsify` 配置，并从 CDN 加载 Docsify 运行库。 |
| [`etc/Mindmap.md`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/Mindmap.md) | 73 行 | 课程「思维导图」，用 Markdown 标题与列表画出整门课的知识树，所有节点是带绝对 GitHub 链接的条目。它是 Docsify 站点在浏览器里渲染出的核心导航内容之一。 |
| [`.nojekyll`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.nojekyll) | 1 行（空文件） | 告诉 GitHub Pages「不要用 Jekyll 处理本仓库」，从而让 Docsify 直接拿到原始 Markdown 文件。 |

> 提示：本仓库**没有** `_sidebar.md`、`_coverpage.md` 这类 Docsify 常见的可选文件（已用 `git ls-files` 确认）。这意味着站点走的是 Docsify 的「默认行为」——首页读 `README.md`、侧边栏由文档标题自动生成。这一点会在 4.1 节展开。

## 4. 核心概念与源码讲解

### 4.1 Docsify 工作原理

#### 4.1.1 概念说明

**Docsify** 是一个「运行时文档生成器」（runtime documentation generator）。要理解它，先要把它和另一类工具对比清楚：

- **静态站点生成器**（Jekyll、Hugo、Docusaurus、VuePress）：需要一次**构建（build）**。你写 Markdown，工具在本地或 CI 里把它们「编译」成一堆 `.html` 文件，再把这些 HTML 发布出去。
- **Docsify**：**没有构建步骤**。它只发布**一个** `index.html`。真正的 Markdown 文件是用户打开网页后，由浏览器在运行时去服务器**现取（fetch）**、再渲染出来的。

换句话说，Docsify 是一个「纯前端渲染」的方案。服务器（或 GitHub Pages）只需要老老实实把原始的 `.md` 文件原样吐出来，剩下的事情——解析 Markdown、生成导航、处理路由——全在用户的浏览器里完成。

这套机制对本仓库几乎是天作之合：

1. 本仓库的「源码」就是一堆 Markdown 和 Notebook，**不需要编译**。
2. 作者只多写了 29 行的 `index.html` 和一个空 `.nojekyll`，仓库立刻就变成了一个带侧边栏、带搜索、可以在线浏览的文档站点。
3. 仓库维护者改了任何一篇 `README.md`，线上站点自动跟着变，因为线上读的就是同一个 Markdown 文件，**没有任何中间产物会过期**。

#### 4.1.2 核心流程

当你在浏览器地址栏打开这个站点时，发生的事情可以拆成 6 步：

```text
1. 浏览器请求  https://<用户>.github.io/AI-For-Beginners/   → 服务器返回 index.html
2. 浏览器解析 index.html，发现两处外部资源：
     a. <link> 主题 CSS  → 从 jsdelivr CDN 拉 theme-simple.css
     b. <script> 运行库  → 从 jsdelivr CDN 拉 docsify.min.js
3. docsify.min.js 执行，读取 window.$docsify 配置对象
4. Docsify 把自己挂到 <div id="app"></div> 上，初始化为一个单页应用（SPA）
5. Docsify 用 fetch 拉取默认入口文档 README.md，用 marked（Markdown 解析器）转成 HTML，渲染进 #app
6. 用户点击页面里某个 ./lessons/.../README.md 链接时：
     - Docsify 拦截点击（不发生整页刷新）
     - 改写地址栏（SPA 路由）
     - 再 fetch 那个新的 .md，渲染出来
```

这里有两个对初学者最容易踩坑的细节，务必记住：

- **必须有 HTTP 服务**。第 5 步的 `fetch` 在 `file://` 协议下会被浏览器的安全策略拦截（跨域/CORS 限制）。所以你不能直接双击 `index.html` 打开，必须先用一个 HTTP 服务器（如 `python -m http.server`）把目录跑起来。这也是本讲实践任务的核心动机。
- **默认入口是 `README.md`**。Docsify 的约定是：哪个目录被访问，就读那个目录下的 `README.md` 作为首页。根目录被访问就读根的 `README.md`，于是你在站点首页看到的就是课程总览表。

> 术语小贴士：
> - **SPA（Single Page Application，单页应用）**：整个网站只有一个 HTML 页面，所有「跳转」都是 JavaScript 在后台换了内容、改了地址栏，没有真正的页面刷新。
> - **fetch**：浏览器内置的「按 URL 去服务器取一段数据」的 API，Docsify 用它来取 Markdown 文本。
> - **marked**：一个把 Markdown 文本翻译成 HTML 的 JavaScript 库，Docsify 内部依赖它。

#### 4.1.3 源码精读

先看入口页的骨架。整个 `index.html` 主体只有两块：一个挂载点和一个配置脚本。

挂载点——Docsify 启动后会把渲染出的整棵页面塞进这个空 `div`：

[index.html:16-16](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/index.html#L16-L16) —— 唯一的挂载容器 `<div id="app"></div>`，Docsify 渲染产物都注入这里。

加载 Docsify 运行库（注意它从 CDN 拉取，本地不需要 `npm install`）：

[index.html:26-26](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/index.html#L26-L26) —— 用协议相对地址 `//cdn.jsdelivr.net/.../docsify.min.js` 引入 Docsify 核心，浏览器连网即可运行。

再对照看导航内容是如何书写的。`etc/Mindmap.md` 是一份纯 Markdown 文件，它既是仓库里的源文件，也是 Docsify 在浏览器里渲染出的「知识树」内容。它的写法就是普通的标题加列表，每条带一个 GitHub 绝对链接：

[etc/Mindmap.md:3-11](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/Mindmap.md#L3-L11) —— 用 `##` 标题分大单元、用 `-` 列表列子主题，Docsify 会把这套标题层级自动渲染成可折叠的侧边栏与正文。

要点：这 73 行 Markdown **不是**给 Docsify 的特殊配置，它就是普通文档。Docsify 的强大之处正在于此——你正常写 Markdown，它就免费给你变成带导航的网站。

#### 4.1.4 代码实践

**实践目标**：亲手验证「Docsify 必须跑在 HTTP 服务下」这一结论，从而真正理解第 4.1.2 节第 5 步。

**操作步骤**：

1. 在仓库根目录（`index.html` 所在目录）打开终端。
2. 先**故意用错误方式**打开一次：用浏览器直接打开本地文件（地址栏形如 `file:///.../index.html`）。
3. 再用**正确方式**打开：执行下面命令启动一个静态服务。

   ```bash
   python -m http.server 3000
   ```

   然后浏览器访问 `http://localhost:3000/`。

**需要观察的现象**：

- 第 2 步（`file://` 打开）：页面可能只出现一个空白或残缺的框架，浏览器控制台（按 F12 → Console）多半会报类似 `CORS policy` 或 `Failed to fetch resource` 的红色错误，`README.md` 内容渲染不出来。
- 第 3 步（`http://` 打开）：首页正常显示课程总览（即根 `README.md` 的内容），左侧出现侧边栏，点击任意一课的链接能平滑切换内容、地址栏变化但页面不刷新（这就是 SPA）。

**预期结果**：能复现「`file://` 失败、`http://` 成功」的对比。如果你用的是完全离线、且无法访问 jsdelivr CDN 的环境，两种方式都可能失败——那是因为 Docsify 库本身要从 CDN 下载，属于「待本地验证」的联网前提，不是本讲逻辑出错。

> 提示：该命令默认占用 3000 端口；若端口被占用可换 `8000` 等其他端口，访问地址相应改变。用 `Ctrl+C` 停止服务。

#### 4.1.5 小练习与答案

**练习 1**：为什么直接双击 `index.html`（`file://` 协议）打不开站点，而用 `python -m http.server` 就能打开？

> **参考答案**：Docsify 在运行时要用 `fetch` 去读 `README.md` 等文件。浏览器的安全策略禁止 `file://` 页面发起这种本地文件读取（视为跨域），所以取不到 Markdown、页面空白。而 `python -m http.server` 把目录变成了一个真正的 HTTP 服务，`fetch` 走的是合法的 `http://` 请求，于是正常工作。

**练习 2**：本仓库没有 `_sidebar.md`，那站点左侧的导航是从哪里来的？

> **参考答案**：Docsify 在找不到 `_sidebar.md` 时，会自动根据**当前文档的标题层级**（`#`、`##`、`###`）生成一个侧边栏。所以导航内容直接来自 `README.md` 等文档自身的标题结构，不需要单独维护一份侧边栏文件。

---

### 4.2 index.html 配置

#### 4.2.1 概念说明

Docsify 的所有可调行为都集中在一个 JavaScript 全局对象 `window.$docsify` 上。本仓库的配置只有 5 个字段，非常克制。理解这 5 个字段，你就掌握了本站点的全部「行为开关」。

先建立直觉：Docsify 把「站点级设置」（名字、GitHub 角标、路由风格）和「渲染细节」（滚动、路径解析）都塞进同一个对象，作者只需挑需要的字段填进去。

#### 4.2.2 核心流程

配置加载的时序很短：

```text
1. 浏览器执行到 <script> 里的 window.$docsify = {...}，先把配置对象存好
2. 紧接着加载 docsify.min.js
3. docsify.min.js 启动时读取已存在的 window.$docsify，据此决定：站点名、是否显示 GitHub 角标、链接如何解析、URL 用什么风格、切换页面是否回到顶部
4. 如果某字段没写，Docsify 就用默认值
```

下面这张表把本仓库用到的 5 个字段一次性讲清楚：

| 字段 | 取值 | 作用 | 不写时的默认 |
| :-- | :-- | :-- | :-- |
| `name` | `'AI for Beginners'` | 显示在侧边栏顶部的站点名 | 无（不显示名字） |
| `repo` | `'https://github.com/Microsoft/AI-For-Beginners'` | 在页面右上角放一个指向该仓库的「GitHub 角标」链接 | 无角标 |
| `relativePath` | `true` | 链接按**当前文档所在目录**解析，而不是按站点根解析 | `false`（按根解析） |
| `auto2top` | `true` | 每次切换文档后自动滚回页面顶部 | `false`（保持滚动位置） |
| `routerMode` | `'history'` | 使用浏览器 History API 生成「干净 URL」（如 `/lessons/1-Intro/README.md`），而非默认的 `#/...` 哈希风格 | `'hash'`（地址栏带 `#`） |

其中最值得展开的是 `relativePath: true` 与 `routerMode: 'history'`，因为它们和本仓库的结构强相关：

- **`relativePath: true` 为什么重要**：回顾 u1-l2，本仓库目录深层嵌套，`README.md` 里满是指向 `./lessons/...` 的相对链接。如果把它关掉（默认按站点根解析），当你已经导航到某个子目录文档时，里面再写的相对链接会被错误地拼到站点根上，导致 404。开启它，链接就始终相对「当前这份文档」所在目录解析，深层导航才不会断。它正是为这种「目录即章节」的仓库准备的。
- **`routerMode: 'history'` 的代价**：干净 URL 更好看、更利于分享，但它依赖服务器对「任意路径都回退到 `index.html`」的支持（否则刷新子页面会 404）。这一点和讲义 **u6-l1（测验应用架构）** 里 Vue 应用的 `mode:'history'` + `routes.json` 回退是完全同源的原理。GitHub Pages 对根目录访问天然回退到 `index.html`，所以本仓库能安全使用 history 模式。

#### 4.2.3 源码精读

配置对象整体——5 个字段即全部行为：

[index.html:18-24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/index.html#L18-L24) —— `window.$docsify` 配置对象，依次设定站点名、仓库角标、相对路径解析、自动回顶、history 路由。

主题样式——本项目用 `docsify-themeable` 的 `theme-simple` 主题，让默认 Docsify 更美观、更可定制：

[index.html:12-12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/index.html#L12-L12) —— 通过 `<link>` 从 CDN 引入 `theme-simple.css`，决定整站配色与排版。

一个小细节值得注意：主题 CSS 用的是完整 `https://` 地址，而 Docsify 运行库用的是**协议相对**地址 `//cdn.jsdelivr.net/...`（见 4.1.3 的第 26 行引用）。协议相对地址会自动跟随当前页面协议（GitHub Pages 上是 `https`，本地 `http.server` 上是 `http`），两种环境下都能加载。

#### 4.2.4 代码实践

**实践目标**：通过「改一个字段、看一个变化」，确认 `window.$docsify` 确实在控制站点行为。

**操作步骤**：

1. 复制一份 `index.html` 到你自己的临时目录（**不要改仓库源文件**），例如 `cp index.html /tmp/my-index.html`，然后用 `python -m http.server` 在那个临时目录里跑起来。
2. 先访问，确认侧边栏顶部显示 `AI for Beginners`、右上角有 GitHub 角标。
3. 把临时副本里的 `name` 改成 `'我的 AI 笔记'`，把 `auto2top` 改成 `false`，刷新页面。
4. 故意把 `relativePath` 改成 `false`，刷新后点进某一课的子链接，观察链接是否还能正确打开。

**需要观察的现象**：

- 第 3 步：侧边栏名字变成「我的 AI 笔记」；当你滚动到页面底部再点一个链接时，新页面**不再**自动回到顶部（因为关掉了 `auto2top`）。
- 第 4 步：在子目录文档里继续点击相对链接时，很可能出现 404 或加载错误——这正是关闭 `relativePath` 的后果。

**预期结果**：能亲眼看到「改字段 → 行为变」的对应关系。本实践的准确刷新效果取决于浏览器缓存与 CDN 是否可达，若现象不明显可尝试强制刷新（`Ctrl+Shift+R`）。

#### 4.2.5 小练习与答案

**练习 1**：`relativePath: true` 解决了什么问题？请结合本仓库的目录结构回答。

> **参考答案**：本仓库目录深层嵌套，`README.md` 用 `./lessons/...` 这类相对链接串起各课。`relativePath: true` 让 Docsify 始终按「当前文档所在目录」解析这些相对链接，从而保证从根目录一路点进 `lessons/4-ComputerVision/...` 等子目录时链接不会断。若关闭它，链接会按站点根解析，子目录里的相对链接会拼错位置导致 404。

**练习 2**：`routerMode: 'history'` 比 Docsify 默认的 `'hash'` 模式好看，但它在部署上有什么额外要求？

> **参考答案**：history 模式生成不带 `#` 的干净 URL，但要求服务器在收到任意子路径请求时都回退返回 `index.html`，否则直接刷新子页面会 404。GitHub Pages 对仓库根的访问天然回退到 `index.html`，所以本仓库可用；自定义服务器则需要额外配置这一回退规则。

**练习 3**：本仓库既没有 `_sidebar.md`，又配置了 `name`，这两件事矛盾吗？

> **参考答案**：不矛盾。`_sidebar.md` 缺失只意味着「侧边栏内容由文档标题自动生成」；而 `name` 控制的是侧边栏**顶部显示的站点标题**，与是否自定义侧边栏内容无关。两者是不同维度的设置。

---

### 4.3 GitHub Pages 部署

#### 4.3.1 概念说明

到这里你已经知道 Docsify 在浏览器里怎么跑了。但还有一个问题：**这个站点是怎么被全世界访问到的？** 答案是 GitHub Pages——GitHub 内置的静态网站托管服务。

GitHub Pages 默认会用 **Jekyll**（一个静态站点生成器）处理你的仓库。这对 Docsify 是个麻烦：Jekyll 会按自己的规则忽略某些文件（例如以 `_` 开头的文件/目录、部分点文件），并尝试「编译」内容。而 Docsify 恰恰需要服务器把原始 Markdown **原样**返回。

解决办法就是那个看似什么都没有的空文件 `.nojekyll`。它的存在本身就是信号：**「GitHub Pages，请跳过 Jekyll，把这个仓库当成一堆普通静态文件原样发布。」**

#### 4.3.2 核心流程

把本仓库部署成文档站点的完整链路：

```text
1. 仓库根目录有 index.html、.nojekyll，以及大量 .md 文件
2. 仓库设置里开启 GitHub Pages：
     Source = Deploy from a branch
     Branch = main  /  /(root)
3. GitHub Pages 检测到 .nojekyll → 跳过 Jekyll 编译
4. GitHub Pages 把仓库根目录作为网站根目录发布：
     访问 https://microsoft.github.io/AI-For-Beginners/  →  返回 index.html
     Docsify 在浏览器里再按需 fetch 各个 .md
5. 之后每次 push 到 main，Pages 自动更新线上站点
```

需要特别澄清一点：本仓库 `.github/workflows/` 下只有 `codeql.yml`（安全扫描）和 `scorecard.yml`（依赖安全打分），**没有**专门发布 Pages 的 GitHub Actions 工作流。这说明本站的发布走的是 GitHub Pages 的**经典「从分支部署」**设置（在仓库 Settings → Pages 里配置），而不是较新的「GitHub Actions 部署」方式。这一点很关键：你在源码里**搜不到**部署逻辑，因为部署配置在 GitHub 网页设置里，不在仓库代码里。

> 术语小贴士：
> - **Jekyll**：GitHub Pages 默认使用的静态站点生成器，会「编译」仓库。Docsify 不需要、也不希望被它编译。
> - **`.nojekyll`**：一个零字节的标记文件，存在即生效，告诉 Pages 关闭 Jekyll。本仓库里它确实是空文件（见源码地图）。

#### 4.3.3 源码精读

那个「空白却关键」的文件——它是零字节的纯标记，存在即表示「别用 Jekyll 处理我」：

[.nojekyll:1-1](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.nojekyll#L1-L1) —— 空文件，仅靠「存在」生效，命令 GitHub Pages 跳过 Jekyll、原样发布所有文件。

回顾整个部署组合：`index.html` 提供 Docsify 入口与配置、`.nojekyll` 保证文件原样发布、根 `README.md` 提供首页内容、`etc/Mindmap.md` 等文档提供可浏览的知识树。**没有任何编译产物**，三者缺一不可。

#### 4.3.4 代码实践

**实践目标**：在不真正上线的前提下，理解「`.nojekyll` + 根目录 `index.html`」这套组合的发布含义，并验证你能用本地服务模拟线上站点。

**操作步骤**：

1. 用 `git ls-files | grep -E '^\.nojekyll$|^index\.html$'` 确认这两个文件确实被仓库跟踪（而非本地未提交）。
2. 用 `wc -c .nojekyll` 确认它是 0 字节（或近 0 字节）的空文件，理解「它不靠内容起作用，靠存在起作用」。
3. 执行 `python -m http.server 3000`，浏览器打开 `http://localhost:3000/`，把首页截图保存。这就是线上 GitHub Pages 站点在本地的一份近似预览。
4.（选做，理解部署）如果你有自己的 GitHub 仓库，可以把这套 `index.html` + `.nojekyll` 复制过去，在 Settings → Pages 选 `main`/(root)，几分钟后访问 `https://<你的用户名>.github.io/<仓库名>/`，体验完整发布链路。**注意：对外发布属于不可逆的公开行为，请仅在自有测试仓库上进行。**

**需要观察的现象**：

- 第 1 步：两个文件名都会被列出，说明它们是仓库的一部分，部署时会被一起发布。
- 第 2 步：`.nojekyll` 字节数为 0（或仅一个换行），印证「标记文件」本质。
- 第 3 步：本地预览页应与 `https://microsoft.github.io/AI-For-Beginners/` 的首页内容基本一致（都是渲染根 `README.md`）。

**预期结果**：你能用自己的话讲清「为什么部署 Docsify 只需要这两个文件，外加 GitHub Pages 的一次设置」。第 4 步若无条件执行，记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：删掉 `.nojekyll` 后，本仓库的 Docsify 站点一定会立刻坏掉吗？为什么？

> **参考答案**：不一定立刻坏。`.nojekyll` 的核心作用是阻止 Jekyll 忽略某些文件（如下划线开头的 `_sidebar.md` 等）。本仓库目前没有这类被 Jekyll 规则忽略的关键文件，主要入口是 `index.html` 和普通 `.md`，所以即使删掉，首页多半仍能显示。但一旦将来引入 `_sidebar.md` 这类文件，没有 `.nojekyll` 它们就会被 Jekyll 吞掉导致功能缺失。因此 `.nojekyll` 是「预防性」的标准配置。

**练习 2**：为什么在仓库源码里找不到「部署站点」的脚本或工作流？

> **参考答案**：因为本站使用 GitHub Pages 的经典「从分支部署」方式，部署配置保存在仓库的 **Settings → Pages** 网页设置里（指定 `main` 分支根目录），而不是写在仓库代码或 `.github/workflows/` 里。仓库里的工作流只有安全扫描（codeql、scorecard），与发布无关。

**练习 3**：把这套 Docsify 方案用在一个全是 Markdown 的笔记仓库上，最少需要哪几个文件？

> **参考答案**：最少需要三个：一个 `index.html`（含 `window.$docsify` 配置与挂载点 `#app`）、一个 `.nojekyll`（让 Pages 跳过 Jekyll）、以及一份 `README.md`（作为默认首页内容）。其余 `.md` 文档按需添加，都会自动被站点收录。

## 5. 综合实践

把本讲三个模块串起来，完成一个「本地复现线上文档站点」的小任务：

1. **起服务**：在仓库根目录运行 `python -m http.server 3000`，浏览器打开 `http://localhost:3000/`。
2. **看首页**：确认首页渲染的是根 `README.md`（即课程总览表），侧边栏由标题自动生成，右上角有指向 `Microsoft/AI-For-Beginners` 的 GitHub 角标（对应 `repo` 配置）。
3. **验证 SPA 路由**：点击课程表里某一课（例如 `Introduction and History of AI`）的链接，观察地址栏变成带路径的 history 风格 URL、内容平滑切换、页面不整页刷新；确认切换后页面回到顶部（对应 `auto2top: true`）。
4. **验证相对路径**：继续点进该课 README 里的子链接，确认都能正确打开（对应 `relativePath: true` 的作用）。
5. **截图存档**：把首页与某一课内页各截一张图，作为「本仓库可被 Docsify 渲染成站点」的证据。
6. **写一句话结论**：结合你看到的 URL、角标、滚动行为，用一句话说明 `index.html` 里哪个配置字段对应了你观察到的哪个现象。

> 如果在第 4 步发现某些子链接打不开，先排查是否因为该 `.md` 不存在或相对路径层级较深——这正是 `relativePath` 要解决的问题，可作为你理解该字段的实证。

## 6. 本讲小结

- **Docsify 是运行时渲染、零构建**的文档方案：浏览器先拿 `index.html`，再去 `fetch` Markdown 现取现渲染，因此必须跑在 HTTP 服务（如 `python -m http.server`）下，不能直接 `file://` 打开。
- 本仓库的 `index.html` 只有 29 行，核心是 `window.$docsify` 的 5 个字段：`name`（站名）、`repo`（GitHub 角标）、`relativePath: true`（相对路径解析，适配深层目录）、`auto2top: true`（切换回顶）、`routerMode: 'history'`（干净 URL）。
- `relativePath: true` 是为本仓库「目录即章节」结构服务的关键开关；`routerMode: 'history'` 则与 u6-l1 测验应用的 history 模式同源，都依赖服务器回退到 `index.html`。
- 站点没有 `_sidebar.md`/`_coverpage.md`，走 Docsify 默认行为：首页读 `README.md`、侧边栏由文档标题自动生成；`etc/Mindmap.md` 是普通 Markdown，却被自动渲染成知识树导航。
- 部署靠 **GitHub Pages 经典「从分支部署」** 设置（不在源码里），配上零字节的 `.nojekyll` 让 Pages 跳过 Jekyll、原样发布文件；仓库里没有 Pages 专用的 Actions 工作流。
- 最小可运行组合：`index.html` + `.nojekyll` + `README.md`，三者即可把任意 Markdown 仓库变成可在线浏览的文档站点。

## 7. 下一步学习建议

- **横向对比**：建议接下来阅读讲义 **u6-l1（测验应用架构 Vue.js）**，对比同样是 SPA、同样用 `history` 路由模式的两个应用——Docsify 站点（无构建、CDN 引入）与 quiz-app（Vue CLI 构建、有打包步骤）在工程化上的取舍差异。
- **纵向深挖翻译**：Docsify 站点本身只管英文内容的多语言切换有限，真正的多语言是靠 `translations/` 目录与 co-op-translator 实现的，详见下一讲 **u6-l4（多语言翻译机制）**。
- **动手扩展（可选）**：如果你想在自有仓库复用这套方案，可以尝试给 `index.html` 增加一个 `_sidebar.md` 自定义侧边栏，或加 `search: true`（需额外引入 docsify 插件）开启全文搜索，体会 Docsify「加配置即加功能」的设计哲学。**请在自有测试仓库上进行对外发布类操作。**
- **延伸阅读**：可对照官方文档了解 `window.$docsify` 的更多字段（如 `subMaxLevel`、`coverpage`、`loadSidebar`），理解本仓库为何选择「最克制」的 5 字段配置。
