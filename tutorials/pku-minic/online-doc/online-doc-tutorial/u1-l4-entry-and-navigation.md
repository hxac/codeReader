# 站点入口与导航机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么整个站点只有 `index.html` 这一个 HTML 文件，以及它是如何被加载的；
- 看懂 `index.html` 里「挂载点 → 配置 → 核心库 → 插件脚本」这条启动链；
- 解释 `loadSidebar: 'toc.md'` 是怎样把一个普通 Markdown 列表变成左侧导航栏的；
- 理解 `footer.md` 的渲染需要「配置 + 插件」两件东西配合；
- 能够动手在侧边栏里新增一个条目并验证它可正常跳转。

## 2. 前置知识

本讲承接 u1-l1（项目定位）、u1-l2（本地运行）和 u1-l3（目录结构）。开始前，请确认你已经理解下面几点（这些本讲不再重复展开）：

- **本仓库是一个 Docsify 文档站**，浏览器端会把 `.md` 渲染成网页，而不是在构建期生成 HTML。
- 本地预览用 `docsify serve docs`，`docs/` 目录就是网站根目录。
- Docsify 的**路由规则**：`/preface/` 会解析到目录里的 `README.md`，`/preface/lab` 会解析到 `preface/lab.md`，`?id=xxx` 解析到页面内的标题锚点。
- u1-l2 已经「点名」过三个关键配置：挂载点 `id="app"`、`loadSidebar: 'toc.md'`、`loadFooter: 'footer.md'`，以及统一配置对象 `window.$docsify`。

本讲要做的事情，就是**把这三处点名过的配置放大**，钻进 `index.html`、`toc.md`、`footer.md` 这三个文件本身，看清它们各自长什么样、怎么协作。

为了读懂本讲，你只需要两个基础概念：

- **单页应用（SPA）**：整个网站自始至终只有一张网页，切换「页面」时浏览器并不重新加载整个 HTML，而是由 JavaScript 抓取新内容、替换页面里的局部区域。
- **挂载点（mount point）**：HTML 里预留的一个空 `<div>`，JavaScript 把渲染好的内容「挂」到这个位置上。本站的挂载点是 `<div id="app"></div>`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `docs/index.html` | 站点唯一的 HTML 外壳 | 挂载点、配置对象、脚本加载顺序 |
| `docs/toc.md` | 侧边栏内容 | 列表缩进与 Docsify 路由链接 |
| `docs/footer.md` | 页脚内容 | 它与 `docsify-footer` 插件的配合关系 |

这三个文件的关系可以用一句话概括：`index.html` 负责「怎么启动」，`toc.md` 负责「左侧怎么导航」，`footer.md` 负责「每页底部显示什么」。

## 4. 核心概念与源码讲解

### 4.1 `index.html`：单页应用的唯一入口

#### 4.1.1 概念说明

Docsify 是一个**单页应用**框架。这意味着，无论你在浏览器里访问首页、`/lv1-main/structure` 还是 `/misc-app-ref/koopa`，服务器最终返回的都是**同一个 `index.html`**。真正的内容（各种 `.md`）是在浏览器里由 JavaScript 根据当前网址动态抓取、再渲染进页面的。

因此 `index.html` 不承载任何正文，它只是一层「外壳」（shell），干三件事：

1. 预留一个空的**挂载点** `<div id="app"></div>`，等 JS 往里填内容；
2. 声明站点的**配置**（写在 `window.$docsify` 里），告诉 Docsify「侧边栏读哪个文件、要不要搜索、主题叫什么」；
3. 按固定**顺序**加载 Docsify 核心库和一串插件脚本。

> 为什么 `index.html` 这么重要：它是浏览器加载的第一份、也是唯一一份 HTML。其余 `.md` 都是被它「召唤」进来的。

#### 4.1.2 核心流程

`index.html` 在浏览器里的启动过程大致如下（伪流程）：

```text
浏览器解析 index.html
        │
        ├── 渲染 <head> 里的样式表（主题、KaTeX、自定义 main.css）
        │
        ├── 遇到 <div id="app"></div>：先留空，作为挂载点
        │
        ├── 执行内联 <script>：把配置写入 window.$docsify
        │     （此时 docsify 核心库还没加载，配置先就位）
        │
        ├── 加载 docsify.min.js（核心库）
        │     └── 读取 window.$docsify，启动路由，抓取 README.md 填进 #app
        │
        └── 依次加载插件脚本
              ├── 主题、搜索、分页、复制按钮
              ├── 代码高亮（Prism 各语言）
              ├── docsify-footer：渲染页脚
              ├── sidebar.js：侧边栏滚动同步
              └── giscus.js：评论
```

这里有一个**顺序细节**至关重要：`window.$docsify` 这段配置写在一个**内联** `<script>` 里，并且出现在核心库 `<script src="...docsify.min.js">` **之前**。浏览器从上到下执行脚本，所以等核心库加载时，配置对象早已就绪，核心库才能正确读取它。如果顺序反过来（先加载核心库、再写配置），核心库就读不到配置了。

#### 4.1.3 源码精读

先看挂载点。整个 `<body>` 里唯一的内容元素就是这一个空 div：

[docs/index.html#L17-L18](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L17-L18) — `<body>` 内只有一个空的 `<div id="app"></div>`，这就是 Docsify 渲染内容的挂载点，初学者可以理解成「正文会塞进这个坑里」。

紧接着的内联脚本定义了全局配置对象：

[docs/index.html#L19-L46](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L19-L46) — 这是 `window.$docsify = { ... }` 的内联 `<script>`，注意它位于核心库加载之前，所以配置能先就位。本讲重点关注其中与「导航」相关的两项。

其中两项直接决定本讲的导航机制：

[docs/index.html#L23](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L23) — `loadSidebar: 'toc.md'`：告诉 Docsify 把 `toc.md` 当作整站的左侧边栏加载。

[docs/index.html#L29](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L29) — `loadFooter: 'footer.md'`：告诉 Docsify 把 `footer.md` 当作页脚加载（注意：光有这一行还不够，还需要第 60 行的插件，见 4.3）。

另外两项与侧边栏体验相关，也值得留意：

[docs/index.html#L24](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L24) — `subMaxLevel: 3`：让 Docsify 自动把正文里 1~3 级标题（`#`/`##`/`###`）作为锚点子项追加到侧边栏，所以侧边栏里除了 `toc.md` 手写的条目，还会出现「正文章节」的二级展开。

然后是核心库与插件脚本的加载区，顺序从上到下：

[docs/index.html#L47](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47) — 加载 `docsify.min.js`，这是 Docsify 的核心，负责读配置、跑路由、抓 `.md` 并渲染进 `#app`。

[docs/index.html#L48-L65](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L48-L65) — 一串插件脚本，依次是主题、搜索、Prism 代码高亮（含本地的 `prism-koopa.js`）、页脚插件、分页、KaTeX、复制按钮，以及本地的 `sidebar.js`。

> 小结：`index.html` 的本质是一份「启动清单」——先留坑（`#app`），再报配置（`window.$docsify`），最后按顺序把核心库和插件一个个搬进来。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 Docsify 是「先加载 `index.html`，再动态抓取 `.md`」的，而不是一次性把所有 HTML 都下回来。

**操作步骤**：

1. 用 `docsify serve docs` 启动站点，浏览器打开本地地址（默认 `http://localhost:3000`）。
2. 按 `F12` 打开开发者工具，切到 **Network（网络）** 面板，勾选「Preserve log（保留日志）」。
3. 刷新首页，观察请求列表。

**需要观察的现象**：

- 第一个请求是 `index.html`；
- 紧随其后会出现 `README.md`（首页正文）、`toc.md`（侧边栏）、`footer.md`（页脚）等 `.md` 请求。

**预期结果**：你会清楚地看到正文、侧边栏、页脚都是**单独的 Markdown 文件**被浏览器抓取的，这正是 SPA 的工作方式。如果看不到 `.md` 请求（例如只看到 `index.html`），说明 Network 面板没刷新或缓存干扰，刷新一次即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `window.$docsify = {...}` 这段内联 `<script>` 整体挪到 `<script src="...docsify.min.js">` 之后，站点会怎样？

> **参考答案**：核心库加载时 `window.$docsify` 还没定义，它会用默认配置启动，于是 `loadSidebar`/`loadFooter` 等设置全部失效——侧边栏、页脚、搜索等都会消失或退回默认行为。这就是「配置必须在核心库之前」的原因。

**练习 2**：挂载点的 `id="app"` 能改成 `id="content"` 吗？

> **参考答案**：单纯改 HTML 不行。Docsify 默认就是找 `#app` 这个元素挂载；要换 id 需要在配置里通过 Docsify 的 `el` 选项显式指定新挂载点（本项目并未这样做）。所以在本项目里，`id` 必须保持 `app`。

---

### 4.2 `toc.md`：侧边栏的结构与导航

#### 4.2.1 概念说明

`loadSidebar: 'toc.md'` 一句话就告诉了 Docsify「侧边栏用 `toc.md`」。但 `toc.md` 本身**没有任何魔法**——它就是一份普通的 Markdown **无序列表**。Docsify 把这份列表渲染成左侧那棵可点击、可层叠的导航树。

理解 `toc.md` 只需要抓住两点：

- **缩进 = 层级**：顶格写（`* `）的是一级条目，缩进两个空格（`  * `）的是它的子条目。
- **链接 = Docsify 路由**：每条 `[显示文字](/路由)` 里的路由，遵循 u1-l3 讲过的路由规则——`/preface/` 是目录首页（`README.md`），`/preface/lab` 是具体文件（`lab.md`）。

所以，编辑侧边栏 == 编辑一份带缩进的 Markdown 列表，不需要任何额外语法。

#### 4.2.2 核心流程

Docsify 渲染侧边栏的流程可以概括为：

```text
读取 toc.md
   │
   ├── 解析成 Markdown 列表（保留缩进层级）
   │
   ├── 渲染成 <ul><li>...</li></ul> 的嵌套结构
   │       └── 顶格条目 = 一级；缩进条目 = 子级
   │
   ├── 给每个 <a href> 绑定点击：切到对应 Docsify 路由
   │
   └── subMaxLevel: 3 再把「当前页面正文」的 H1~H3
       作为锚点追加进侧边栏（自动二级展开）
```

对应到 `toc.md` 的写法约定：

- 一级条目对应**实验阶段或大分类**（如 `Lv0. 环境配置`、`Lv1. main 函数`、`杂项/附录/参考`）；
- 二级条目（缩进 2 空格）对应该阶段下的**小节**（如 `Lv1.1. 编译器的结构`）。

#### 4.2.3 源码精读

先看 `toc.md` 的开头，也就是「写在前面」这一组：

[docs/toc.md#L1-L4](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L1-L4) — 「写在前面」是一个一级条目（顶格的 `* [写在前面](/preface/)`），下面缩进两格的三个条目是它的子项。注意 `/preface/` 末尾带斜杠，会解析到 `preface/README.md`；`/preface/lab` 不带斜杠，解析到 `preface/lab.md`。

再看一个典型的实验阶段块，体会缩进与路由：

[docs/toc.md#L10-L15](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L10-L15) — `Lv1. main 函数` 是一级条目（路由 `/lv1-main/` → `lv1-main/README.md`），其下 `Lv1.1`~`Lv1.5` 是缩进的子条目（路由 `/lv1-main/structure` → `lv1-main/structure.md` 等）。

整份 `toc.md` 就是这种「一级分组 + 二级小节」的重复结构，从 Lv0 一路排到 Lv9+，最后以「杂项/附录/参考」收尾：

[docs/toc.md#L57-L67](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L57-L67) — 「杂项/附录/参考」作为最后一个一级条目，下面罗列了 SysY 规范、Koopa IR 规范、RISC-V 指令速查等参考资料，全部指向 `/misc-app-ref/...` 路由。

> 一句话：`toc.md` = 一棵「用 Markdown 缩进表达的目录树」，链接全部走 Docsify 路由。

#### 4.2.4 代码实践

**实践目标**：验证「改 `toc.md` 就能改侧边栏」，并确认新条目能正常跳转。

**操作步骤**：

1. 在 `docs/toc.md` 第 4 行（「写在前面」块的最后一项之后、空行之前）新增一行缩进的测试条目，例如：

   ```markdown
     * [测试条目](/lv1-main/structure)
   ```

   （注意行首是**两个空格**，让它成为「写在前面」的子项；`/lv1-main/structure` 是一个已存在的路由，对应 `docs/lv1-main/structure.md`。）

2. 保存文件，确保 `docsify serve docs` 正在运行。
3. 浏览器刷新站点，展开左侧「写在前面」。

**需要观察的现象**：侧边栏的「写在前面」下多出一个「测试条目」；点击它能跳转到 `Lv1.1 编译器的结构` 页面。

**预期结果**：条目出现且跳转正常，说明侧边栏完全由 `toc.md` 这份列表决定。验证完，可以用 `git checkout docs/toc.md` 还原，保持仓库整洁。

#### 4.2.5 小练习与答案

**练习 1**：为什么「写在前面」用 `/preface/`（带斜杠），而它的子项用 `/preface/lab`（不带斜杠）？

> **参考答案**：带斜杠的 `/preface/` 是目录路由，Docsify 解析为该目录的 `README.md`，适合做章节首页；不带斜杠的 `/preface/lab` 是文件路由，解析为 `lab.md`，适合指向具体小节文件。这正是 u1-l3 讲过的两条路由规则。

**练习 2**：如果把某个子条目的缩进从「两个空格」改成「顶格」，侧边栏会怎样？

> **参考答案**：它会从「子条目」变成「一级条目」，在侧边栏里脱离原章节、单独成为一项。这印证了层级完全由缩进决定，而非任何额外标记。

---

### 4.3 `footer.md`：页脚与它的加载机制

#### 4.3.1 概念说明

`footer.md` 负责每页底部的页脚（版权声明、作者、协议等）。它本身也是一份普通 Markdown。

这里有一个**容易踩坑**的点：让页脚真正显示出来，需要**两样东西配合**，缺一不可：

1. 配置项 `loadFooter: 'footer.md'`（在 `index.html` 的 `window.$docsify` 里）——它只负责告诉 Docsify「页脚文件是哪一个」；
2. 第三方插件脚本 `docsify-footer.min.js`——它才是真正「把页脚渲染到页面底部」的实现。

换句话说，`loadFooter` 是「指名」，插件是「干活」。只写配置不加载插件，页脚不会出现；反过来也一样。这和 `loadSidebar` 不同——侧边栏是 Docsify 核心自带的，不需要额外插件。

#### 4.3.2 核心流程

```text
页面渲染正文后
   │
   ├── Docsify 读到 loadFooter: 'footer.md'
   │     └── 知道要加载 footer.md 这个文件
   │
   ├── docsify-footer 插件介入
   │     └── 抓取 footer.md，渲染成 HTML
   │
   └── 把渲染结果插入到每页正文下方（页脚位置）
```

#### 4.3.3 源码精读

配置项与插件脚本这两处配合：

[docs/index.html#L29](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L29) — `loadFooter: 'footer.md'`：指明页脚文件名。

[docs/index.html#L60](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L60) — 加载 `@alertbox/docsify-footer` 插件，真正负责把页脚渲染到页面底部。它与上一行的配置是「配对」关系。

再看页脚文件本身的内容：

[docs/footer.md#L1-L3](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/footer.md#L1-L3) — `footer.md` 就是一个极简的 Markdown：一个 `<br>` 换行，加一行带内联样式的版权说明（版本号、作者、CC BY-NC-SA 4.0 协议）。它会被渲染到每个页面的最下方。

> 对比记忆：侧边栏 `toc.md` 配 `loadSidebar`（核心自带，免插件）；页脚 `footer.md` 配 `loadFooter` **还**要配 `docsify-footer` 插件。这是两者机制上的关键差别。

#### 4.3.4 代码实践

**实践目标**：验证「配置 + 插件」缺一不可，并体验修改页脚内容。

**操作步骤**：

1. 启动站点后，先在 `docs/footer.md` 第 3 行末尾追加一句，例如 `（这是我的测试页脚）`，保存刷新——你会看到每页底部多出这句话。
2. 还原 `footer.md`，然后**临时**把 `index.html` 第 60 行（`docsify-footer.min.js` 那个 `<script>`）整行注释掉（在开头加 `<!--`、结尾加 `-->`），保存刷新。

**需要观察的现象**：第 2 步之后，虽然 `loadFooter: 'footer.md'` 配置还在，但页面底部的页脚消失了。

**预期结果**：这证明了「光有配置、没有插件」页脚不会渲染。验证完务必还原 `index.html`：`git checkout docs/index.html`。**待本地验证**：不同浏览器/缓存下，注释掉插件后页脚是否完全消失，以你本机实际现象为准。

#### 4.3.5 小练习与答案

**练习 1**：把页脚文件名从 `footer.md` 改成 `bottom.md`，需要改哪几个地方？

> **参考答案**：至少两处——把 `index.html` 里的 `loadFooter: 'footer.md'` 改成 `loadFooter: 'bottom.md'`，并把 `docs/footer.md` 重命名为 `docs/bottom.md`。插件脚本 `docsify-footer` 不用改，它会按配置去加载你指定的文件名。

**练习 2**：为什么不加载 `docsify-footer` 插件，页脚就不显示，而侧边栏却不需要额外插件？

> **参考答案**：侧边栏是 Docsify **核心库自带**的能力，`loadSidebar` 直接被核心识别；而页脚是 Docsify **核心不包含**的功能，需要由第三方 `docsify-footer` 插件来实现。所以页脚是「配置指名 + 插件干活」两段式，侧边栏是「核心直管」一段式。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个完整的小任务：**给侧边栏加一个带页脚感知的测试章节入口，并验证整条导航链路。**

1. **准备**：`docsify serve docs` 启动站点，浏览器与开发者工具 Network 面板就位。
2. **改侧边栏**：在 `docs/toc.md` 的「写在前面」块下，新增一个一级测试条目（顶格写），指向一个已存在的页面，例如：

   ```markdown
   * [我的测试入口](/preface/prerequisites)
   ```

3. **验证入口**：刷新站点，确认左侧出现「我的测试入口」；点击它，确认跳转到「前置知识」页面；在 Network 面板确认这次跳转触发了一次 `prerequisites.md` 的请求（SPA 抓取，而非整页刷新）。
4. **观察正文锚点**：进入某个内容较多的页面（如 `/lv1-main/structure`），展开侧边栏，留意 `subMaxLevel: 3` 自动追加的正文章节锚点。
5. **核对页脚**：滚动到页面底部，确认页脚（来自 `footer.md` + `docsify-footer` 插件）正常显示。
6. **收尾**：用 `git checkout docs/toc.md` 还原你的测试条目，保持仓库干净。

完成后，你应当能向别人讲清楚：「点侧边栏 → Docsify 改路由 → 抓对应 `.md` → 渲染进 `#app` → 页脚由插件补上」这一整条链路。

## 6. 本讲小结

- `index.html` 是整个 SPA **唯一的 HTML**，它靠「挂载点 `#app` + 配置 `window.$docsify` + 一串脚本」启动一切。
- 启动顺序很重要：**配置必须写在核心库之前**，否则核心库读不到配置。
- `loadSidebar: 'toc.md'` 让一份**普通 Markdown 列表**变成左侧导航树；层级靠缩进，链接走 Docsify 路由。
- `subMaxLevel: 3` 会把正文 H1~H3 自动追加为侧边栏锚点。
- 页脚是「配置 + 插件」两段式：`loadFooter` 指名，`docsify-footer` 插件干活，二者缺一不可——这点和核心自带的侧边栏不同。
- 改导航 = 改 `toc.md`；改页脚 = 改 `footer.md`；都不需要碰任何构建步骤。

## 7. 下一步学习建议

- 想彻底搞懂 `window.$docsify` 里**其余**配置项（搜索、分页、复制按钮、主题切换）的来龙去脉，请接着学 **u2-l1 Docsify 配置与主题样式系统**。
- 想看 Docsify 是如何被本仓库扩展的（Koopa IR 语法高亮、侧边栏滚动同步、giscus 评论），请学 **u2-l2** 和 **u2-l3**。
- 如果你对「Docsify 路由到底如何映射到本地文件」想从**代码**层面验证，强烈推荐直接跳到 **u4-l3 Docsify 路由解析与本地链接校验**——那里的 `scripts/check_links.py` 会把本讲提到的 `/path/` → `README.md` 等规则用 Python 严格实现出来，是检验你理解的最佳对照。
