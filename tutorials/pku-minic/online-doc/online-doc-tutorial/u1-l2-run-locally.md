# 在本地把文档站跑起来

## 1. 本讲目标

上一篇（[u1-l1 项目定位](u1-l1-project-overview.md)）我们搞清楚了「这是什么仓库」。本讲解决下一个最自然的问题：**怎么把它在本地跑起来，亲眼看到它长什么样？**

读完本讲，你应当能够：

- 用两条命令在本地启动文档站，并用浏览器打开它；
- 解释 `docsify serve docs` 里的 `docs` 为什么不能少——它和站点的「内容根」是同一个东西；
- 看懂 `requirements.txt` 里三行 Python 依赖分别是什么、各自被仓库里哪个工具用到。

本讲偏「动手」，但所有命令都来自仓库自身的 `README.md`，不是凭空编的。跟着做一遍，你就能拥有一个可点击、可搜索的本地文档站。

## 2. 前置知识

本讲需要你大致了解下面几个概念（不熟也没关系，下面会解释）：

- **命令行（shell / 终端）**：用文字而不是鼠标操作电脑的方式。下文形如 `npm i ...`、`docsify serve ...` 的内容都是在终端里敲的命令。
- **Node.js / npm**：Node.js 是一个能运行 JavaScript 的运行时；npm 是随它一起安装的「包管理器」，类似 Python 的 pip，用来安装别人写好的 JS 工具。本讲用它安装 `docsify-cli`。
- **Python / pip**：Python 是一门编程语言，pip 是它的包管理器。本仓库唯一的程序代码 `scripts/check_links.py` 是 Python 写的，需要用 pip 装依赖。
- **静态文件服务器（static server）**：一个只负责「把磁盘上的文件按 HTTP 协议发给浏览器」的小服务，不做任何动态计算。`docsify serve` 本质上就是启动这样一个服务器。
- **单页应用（SPA, single-page application）**：浏览器只加载一个 HTML 外壳，之后页面内容的切换都靠 JavaScript 在前端完成、不重新请求整个网页。Docsify 就是一个极简的 SPA。

如果你还没装 Node.js / npm 或 Python / pip，请先到各自官网安装。下面假设这两套工具已经在你的命令行里可用（键入 `node -v` 和 `python -V` 能出版本号）。

## 3. 本讲源码地图

本讲只看三个文件，它们刚好对应「装、跑、补依赖」三件事：

| 文件 | 作用 |
| --- | --- |
| `README.md`（仓库根目录） | 给出官方的安装命令 `npm i docsify-cli -g` 与启动命令 `docsify serve docs`。本讲所有命令都出自这里。 |
| `docs/index.html` | 文档站的**唯一入口**——浏览器加载的就是这个 HTML。它告诉 docsify 该去哪取侧边栏、页脚，以及把内容挂载到页面哪个位置。 |
| `requirements.txt` | Python 依赖清单。装上它才能运行仓库唯一的脚本工具 `scripts/check_links.py`（链接检查器，第四单元会精读）。 |

> 一个容易忽略的点：**浏览文档站本身根本不需要 Python**。Python 依赖只为「运行链接检查器」服务。两件事可以完全分开做，下面第 4.3 节会专门说明。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：①docsify-cli 安装；②`docsify serve` 启动；③Python 依赖一览。

### 4.1 docsify-cli 安装

#### 4.1.1 概念说明

要本地预览 Docsify 站点，最省事的方式是用官方命令行工具 **docsify-cli**。它是一个 npm 包，装好之后会在你的终端里提供一个 `docsify` 命令。

这里有一个新手最容易搞混的点，务必记住：

> **docsify-cli 不是 docsify 本身。**

- **docsify（库）**：真正干活的程序，是一段在**浏览器**里运行的 JavaScript。它负责把 Markdown 文件渲染成网页。在本仓库里，它是通过 `<script>` 标签从 CDN 直接加载的，**不需要你安装**。
- **docsify-cli（工具）**：一个**开发期**的小帮手。它的核心作用就是在你电脑上启动一个「静态文件服务器」，好让你能通过 `http://localhost:...` 这种地址在浏览器里预览站点。它本身并不参与渲染。

打个比方：docsify 是「演员」，docsify-cli 是「给你搭了个临时舞台让你能彩排」的工作人员。

为什么要加 `-g`（global，全局安装）？因为只有全局安装，终端里才会出现 `docsify` 这个命令，你才能在任意目录下敲它。不加 `-g` 的话，这个命令只在某个项目目录里能用，不方便。

#### 4.1.2 核心流程

从「什么都没装」到「能用 docsify 命令」，流程是：

```text
确认已安装 Node.js 与 npm（node -v / npm -v 能出版本号）
      │
      ▼
npm i docsify-cli -g      ← 全局安装，获得 docsify 命令
      │
      ▼
docsify --version（或 docsify -V）  ← 验证命令可用
```

#### 4.1.3 源码精读

仓库根目录的 README 把安装方式写得明明白白：

> 安装说明：[README.md:L9-L13](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/README.md#L9-L13) ——「This repository based on Docsify, you can install it by running:」，紧跟一个代码块 `npm i docsify-cli -g`。

关键原文（示例节选）：

```text
This repository based on Docsify, you can install it by running:

$ npm i docsify-cli -g
```

至于「docsify 本体其实在 index.html 里通过 CDN 加载、不需要安装」这件事，可以直接在站点入口里找到证据：

> [docs/index.html:L47](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47) 这一行是 `<script src="//npm.elemecdn.com/docsify/lib/docsify.min.js"></script>`——浏览器会从这里下载真正的 docsify 库。这就印证了 4.1.1 的结论：**docsify-cli 只是个本地预览服务器，docsify 库是 CDN 现拉的。**

#### 4.1.4 代码实践

这是一个「安装 + 验证」型实践。

1. **实践目标**：在本地装好 docsify-cli，确认 `docsify` 命令可用。
2. **操作步骤**：
   - 先确认环境：终端运行 `node -v` 和 `npm -v`，应分别打印版本号（本仓库的运行环境是 Node v20.x，仅供参考）。
   - 执行全局安装：`npm i docsify-cli -g`。
   - 验证：`docsify --version`（或 `docsify -V`）。
3. **需要观察的现象**：安装过程会下载若干依赖；最后 `docsify --version` 应打印一个版本号，而不是「command not found」。
4. **预期结果**：终端能打印出 docsify-cli 的版本字符串。
5. **待本地验证**：具体的版本号取决于你安装时的发布版本，无法预填。此外，部分系统对「全局安装」有权限限制：若报 `EACCES` 之类权限错误，macOS/Linux 上可按 npm 官方建议修复 npm 的全局目录权限，或临时加 `sudo`（企业/教学机请遵守所在环境的权限策略）。

#### 4.1.5 小练习与答案

**练习 1**：有人说「我没装 docsify-cli，所以这个网站打不开了」，这句话对吗？
**参考答案**：不完全对。docsify-cli 只负责**本地预览**。正式的站点是部署在 GitHub Pages 上的（见根目录 README 给出的 `https://pku-minic.github.io/online-doc/`），访问线上版本不需要装任何东西。docsify-cli 只是让你能在自己电脑上离线预览、改完即时看到效果。

**练习 2**：`docsify-cli` 和 docsify 库，哪个才是「把 Markdown 渲染成网页」的那段程序？
**参考答案**：是 **docsify 库**。它在浏览器里运行，通过 `docs/index.html` 里的 CDN `<script>` 标签加载（见 [docs/index.html:L47](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47)）。`docsify-cli` 只是一个本地静态服务器，不参与渲染。

---

### 4.2 docsify serve 启动

#### 4.2.1 概念说明

装好 docsify-cli 后，启动站点的命令是：

```bash
docsify serve docs
```

这条命令做了一件简单但关键的事：**在本地启动一个静态文件服务器，把服务器的「根目录」设为 `docs/`，并默认监听 3000 端口。** 之后你用浏览器访问 `http://localhost:3000`，服务器就会把 `docs/` 里的文件发给浏览器。

为什么参数是 `docs`、而不是 `.`（当前目录）？这正是上一篇（u1-l1）反复强调的「`docs/` 才是内容根」的体现。下面这个对应关系要记牢：

| 命令里的路径 | 它在服务器眼里是什么 |
| --- | --- |
| `docsify serve docs` | 把 `docs/` 当作网站根目录 `/` |
| 于是 `docs/index.html` | 对应网址 `http://localhost:3000/`（首页入口） |
| 于是 `docs/README.md` | 对应首页正文内容 |
| 于是 `docs/toc.md` | 对应侧边栏 |

Docsify 是**单页应用**：浏览器只加载一次 `index.html` 这个「外壳」，之后切换章节时，是 docsify 在前端用 JavaScript 去**请求对应的 Markdown 文件**、再渲染进页面。也就是说，本地服务器存在的意义，就是把这些 `.md` 文件通过 HTTP 提供给浏览器去抓取。

#### 4.2.2 核心流程

从「敲下命令」到「看到首页」，数据流是：

```text
docsify serve docs
      │  本地服务器监听 :3000，以 docs/ 为根
      ▼
浏览器访问 http://localhost:3000
      │
      ▼
服务器返回 docs/index.html（单页外壳）
      │  浏览器执行其中的 <script>，拉取并启动 docsify 库
      ▼
docsify 读取 window.$docsify 配置（要去哪取侧边栏、页脚……）
      │
      ▼
docsify 按需请求 README.md / toc.md / footer.md 等并渲染
```

#### 4.2.3 源码精读

启动命令同样写在根目录 README 里：

> 启动说明：[README.md:L15-L19](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/README.md#L15-L19) ——「To launch a local server for testing, run:」，代码块给出 `docsify serve docs`。

而服务器启动后浏览器加载的入口，就是 `docs/index.html`。它有三个值得注意的地方：

第一，**挂载点**：

> [docs/index.html:L18](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L18) 是 `<div id="app"></div>`。docsify 会把渲染出来的网页内容塞进这个 `id="app"` 的空 div 里。这就是「单页外壳」里唯一留给内容的坑位。

第二，**配置对象 `window.$docsify`**（节选）：

> [docs/index.html:L20-L45](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L20-L45) 定义了 docsify 的全部行为。其中：
> - [docs/index.html:L23](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L23) `loadSidebar: 'toc.md'`——告诉 docsify 去取 `toc.md` 当侧边栏；
> - [docs/index.html:L29](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L29) `loadFooter: 'footer.md'`——去取 `footer.md` 当页脚。

注意这些路径都是**相对路径**（`toc.md`、`footer.md`），它们最终会被解析成 `docs/toc.md`、`docs/footer.md`——**正因为服务器把 `docs/` 当作根目录**。这就是为什么命令里那个 `docs` 参数绝不能少：少掉它，这些相对路径就全对不上了，站点会「缺胳膊少腿」。

第三，**真正的 docsify 库**：

> [docs/index.html:L47](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47) `<script src="...docsify.min.js"></script>` 在配置之后被加载，真正启动渲染引擎。

#### 4.2.4 代码实践

这是一个「启动 + 看效果」型实践。

1. **实践目标**：在本地启动文档站，用浏览器确认能看到首页。
2. **操作步骤**：
   - 在**仓库根目录**（即和 `README.md` 同级，能看到 `docs/` 文件夹的那一层）打开终端。
   - 运行 `docsify serve docs`。
   - 终端会提示服务已启动，并给出本地地址（docsify-cli 默认是 `http://localhost:3000`）。
   - 用浏览器打开该地址。
3. **需要观察的现象**：
   - 终端持续运行（这是「常驻」进程，不要关掉它；要停止按 `Ctrl+C`）；
   - 浏览器里出现标题「北大编译实践在线文档」，左侧有侧边栏（内容来自 `docs/toc.md`），正文是 `docs/README.md` 的首页内容，底部有页脚。
4. **预期结果**：能正常浏览首页，并在侧边栏点击不同章节完成跳转。
5. **待本地验证**：实际首页的排版、主题（明/暗）以你本机渲染为准；若 3000 端口被占用，docsify-cli 通常会自动换端口，请以终端打印的实际地址为准。本人未在此处执行该命令，以上为依据源码与 docsify-cli 默认行为的预期描述。

#### 4.2.5 小练习与答案

**练习 1**：如果把启动命令改成 `docsify serve .`（serve 当前仓库根目录而不是 `docs/`），访问首页会怎样？
**参考答案**：会出问题。此时服务器把仓库根目录当作 `/`，入口 HTML 变成 `/docs/index.html` 而不是 `/`；更关键的是 `index.html` 里写的是相对路径 `toc.md`、`footer.md`，它们会去仓库根目录找、找不到，导致侧边栏和页脚缺失甚至整站无法正常工作。**必须 `docsify serve docs`。**

**练习 2**：docsify 把 Markdown 变成网页，是在「服务器端」还是「浏览器端」完成的？
**参考答案**：在**浏览器端**。本地服务器只负责把 `.md` 文件原样发给浏览器；真正解析 Markdown、渲染页面的是浏览器里运行的 docsify 库（[docs/index.html:L47](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47)）。所以 docsify 是一个前端单页应用。

---

### 4.3 Python 依赖一览

#### 4.3.1 概念说明

仓库根目录有个 `requirements.txt`，它是 **pip 的依赖清单**——里面列着运行仓库**唯一**的脚本工具 `scripts/check_links.py` 所需的 Python 包。

先把范围划清楚（这点很重要）：

> **这三个 Python 依赖和「看文档」毫无关系。** 你哪怕一个 Python 包都不装，照样能用 `docsify serve` 浏览整站。Python 依赖只在你**想运行链接检查器**时才需要。

`requirements.txt` 里一共三个包，每个都对应检查器里的一块职责：

| 依赖 | 是什么 | 在检查器里干什么 |
| --- | --- | --- |
| `httpx>=0.28,<1` | 现代 Python HTTP 客户端（支持同步与异步） | 发起请求，**校验远程链接**是否还活着（第四单元 u4-l4 详解） |
| `markdown-it-py[linkify]>=3,<5` | Markdown 解析器；`[linkify]` 是它的一个「额外功能」 | 解析 Markdown，**抽取其中的链接和图片**；`linkify` 还能把正文里的裸链接（如 `https://...`）也识别成链接 |
| `selectolax>=0.3,<1` | 基于 C 的高性能 HTML 解析器 | 解析文档里的**原始 HTML 片段**，抽取其中的 `href` / `src` 链接 |

版本号里的 `>=...,<...` 是「版本区间」约束，例如 `httpx>=0.28,<1` 表示「至少 0.28、但低于 1.0」，确保用到的是兼容版本。

#### 4.3.2 核心流程

从「装依赖」到「能跑检查器」，流程是：

```text
pip install -r requirements.txt
      │
      ▼
httpx / markdown-it-py / selectolax 装入当前 Python 环境
      │
      ▼
即可运行：python scripts/check_links.py --config check-links.toml --no-http
      │  （检查器本身的用法在第四单元 u4-l1 详解）
      ▼
扫描 docs/ 下的 .md，提取并校验其中的链接
```

#### 4.3.3 源码精读

依赖清单只有三行：

> [requirements.txt:L1-L3](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt#L1-L3) 分别列出 `httpx`、`markdown-it-py[linkify]`、`selectolax` 三个包及其版本区间。

这三行并不是随便写的——它们与 `check_links.py` 顶部的 import 一一对应：

> - [scripts/check_links.py:L31](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L31) `import httpx`——对应 `httpx` 依赖，用于发 HTTP 请求校验远程链接；
> - [scripts/check_links.py:L32](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L32) `from markdown_it import MarkdownIt`——对应 `markdown-it-py` 依赖，用于解析 Markdown；
> - [scripts/check_links.py:L34](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L34) `from selectolax.parser import HTMLParser`——对应 `selectolax` 依赖，用于解析 HTML。

至于「`[linkify]` 这个额外功能到底用没用上」，也有据可查：

> [scripts/check_links.py:L59-L60](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L59-L60) 构造解析器时写的是 `MarkdownIt('commonmark', {'html': True, 'linkify': True}).enable('linkify')`——`'linkify': True` 加上 `.enable('linkify')` 正是开启「自动识别裸链接」的开关，所以 `requirements.txt` 里才特意带上 `[linkify]` 这个额外依赖。

#### 4.3.4 代码实践

这是一个「安装 + 自检」型实践。

1. **实践目标**：装好链接检查器依赖，并确认三个包都能被 Python 正确导入。
2. **操作步骤**：
   - 在仓库根目录运行：`pip install -r requirements.txt`。
   - 做一个最小自检（避免「装了但 import 失败」）：`python -c "import httpx, markdown_it, selectolax; print('ok')"`。
3. **需要观察的现象**：pip 逐个下载安装三个包；自检命令打印出 `ok` 而不报 `ModuleNotFoundError`。
4. **预期结果**：三个包都能成功导入。
5. **待本地验证**：若你用的是较新的 Python 发行版，部分系统建议用 `pip install --user -r requirements.txt` 或在虚拟环境（venv）中安装，以避免污染系统 Python；具体是否需要，以你所在环境的提示为准。本人未在此处实际执行安装，以上为依据 `requirements.txt` 的预期描述。

#### 4.3.5 小练习与答案

**练习 1**：我只想去本地看看文档，不想管什么链接检查。我必须 `pip install -r requirements.txt` 吗？
**参考答案**：不必。浏览文档只需要 `docsify serve docs`（Node.js 生态）。Python 依赖只服务于 `scripts/check_links.py`。两件事互不依赖。

**练习 2**：`requirements.txt` 里 `markdown-it-py[linkify]` 的 `[linkify]` 是什么意思？去掉会怎样？
**参考答案**：`[linkify]` 是 pip 的「额外依赖（extras）」语法，表示安装 markdown-it-py 时一并装上让它支持「自动识别裸链接」的附加库。检查器在 [scripts/check_links.py:L59-L60](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L59-L60) 开启了 `linkify`，若去掉 `[linkify]`，那行代码可能在运行时报缺少依赖。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性任务（这也是本讲的核心实践）：

1. **安装 docsify-cli**：`npm i docsify-cli -g`，用 `docsify --version` 确认命令可用。
2. **启动站点**：在仓库根目录运行 `docsify serve docs`，浏览器打开终端给出的地址（默认 `http://localhost:3000`）。
3. **确认首页**：在浏览器里确认能看到「北大编译实践在线文档」标题、左侧侧边栏（来自 `docs/toc.md`）、正文（来自 `docs/README.md`）和底部页脚（来自 `docs/footer.md`）。随手点几个侧边栏章节，确认能正常跳转。
4. **安装 Python 依赖**：回到终端另开一个窗口（不要关掉正在跑的服务），执行 `pip install -r requirements.txt`，再用 `python -c "import httpx, markdown_it, selectolax; print('ok')"` 自检。

**验收标准**：

- 前三步成功 → 你拥有了一个可点击、可搜索的本地文档站；
- 第四步打印 `ok` → 你已为后续第四单元精读 `scripts/check_links.py` 备齐了运行环境。

> 说明：以上命令的运行结果取决于你本机环境，本人未在此处代为执行；若某一步报错，请优先核对 Node.js / npm / Python / pip 是否已正确安装、端口是否被占用、全局安装是否有权限。这一步在本沙箱环境确有 Node v20 与 Python 3.12，但未实际联网安装，故标记为「待本地验证」。

## 6. 本讲小结

- 本地预览文档站分两步：先 `npm i docsify-cli -g` 装 CLI，再 `docsify serve docs` 启动，这两条命令都写在根目录 [README.md:L9-L19](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/README.md#L9-L19)。
- **docsify-cli ≠ docsify**：前者只是本地静态服务器，后者才是浏览器里真正渲染 Markdown 的库，由 `docs/index.html` 通过 CDN 加载（[docs/index.html:L47](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47)）。
- `docsify serve docs` 的 `docs` 不能少：它把 `docs/` 设为服务器根目录，这样 `index.html` 里 `loadSidebar: 'toc.md'`、`loadFooter: 'footer.md'` 等相对路径才能正确解析。
- 浏览器加载 [docs/index.html](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html) 这个单页外壳，docsify 在前端按需抓取 `.md` 文件渲染——渲染发生在浏览器端。
- [requirements.txt](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt) 的三个 Python 包（httpx / markdown-it-py[linkify] / selectolax）只为运行 `scripts/check_links.py` 服务，和浏览文档无关。

## 7. 下一步学习建议

现在你已经能把站点跑起来，建议接着：

- **u1-l3《仓库目录结构一览》**：系统了解 `docs/`、`scripts/`、`.github/`、`assets/` 各自装了什么，建立全局地图。
- **u1-l4《站点入口与导航机制》**：在跑起来的站点基础上，深入 `index.html`、`toc.md`、`footer.md` 三者如何协作组成导航。
- **u2-l1《Docsify 配置与主题样式系统》**：如果想动手改 `$docsify` 配置或自定义样式，从这里开始。

等你进入第四单元时，本讲装的 Python 依赖就会派上用场——届时我们将逐行精读 `scripts/check_links.py`。
