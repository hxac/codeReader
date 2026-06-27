# 仓库目录结构一览

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 `online-doc` 仓库的顶层布局，说出每个根目录文件/目录是做什么的。
- 区分「内容目录」（`docs/`）与「工程目录」（`scripts/`、`.github/`），知道哪一边是给人看的文档、哪一边是维护文档的工具。
- 在 `docs/` 下快速定位任意一篇讲义、任意一张图片、任意一个站点脚本。
- 用 `git ls-files` 这样的只读命令自己盘点仓库结构。

本讲承接 u1-l1（项目定位）和 u1-l2（本地运行）。上一讲我们知道了「仓库里几乎全是 Markdown，唯一的程序代码是 `scripts/check_links.py`」；本讲就把整个仓库的「抽屉格局」摊开，让你以后找任何东西都不迷路。

## 2. 前置知识

- **Git 跟踪的文件 vs 工作区里的文件**：仓库里有些文件被 Git 跟踪（会被 `git ls-files` 列出、会进入提交），有些则不会（例如本地生成的临时文件）。本讲只讨论被跟踪的文件，并一律用 `git ls-files` 来盘点，这样结果可复现。
- **目录树（directory tree）**：用缩进表示「谁是谁的子目录」的写法。本讲会画一棵到二级深度的树。
- **Docsify 站点的「根目录」概念**：回顾 u1-l2，`docsify serve docs` 把 `docs/` 当作服务器根目录，浏览器访问站点时看到的一切内容都来自 `docs/`。这一点决定了为什么绝大多数文件都在 `docs/` 下。

> 一句话直觉：这个仓库像一个「双开门柜子」——左边 `docs/` 是给读者看的教材内容，右边 `scripts/` + `.github/` 是维护这套教材的「工具间」；根目录几个小文件则是柜子上的「铭牌和说明书」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/README.md) | 仓库「门牌」，说明项目基于 Docsify，并列出本地启动命令。 |
| [docs/toc.md](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md) | 侧边栏目录，**集中体现了内容如何按 Lv0–Lv9+ 分层**。 |
| [docs/index.html](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html) | Docsify 单页应用（SPA）的唯一入口，配置项与脚本都在这里挂载。 |
| [scripts/check_links.py](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py) | 链接检查器，整个仓库里**唯一一段有分量的程序代码**。 |
| [.github/workflows/check-links.yml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml) | GitHub Actions 工作流，在 PR/push 时自动跑链接检查。 |
| [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml) | 链接检查器的配置文件（扫描范围、忽略规则等）。 |
| [requirements.txt](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt) | 链接检查器的 Python 依赖。 |

## 4. 核心概念与源码讲解

### 4.1 顶层文件与目录

#### 4.1.1 概念说明

仓库根目录是整个项目的「入口大堂」。用 `git ls-files | cut -d/ -f1 | sort -u` 可以列出所有被跟踪的「顶层条目」，一共只有 7 个：

```
.github/        工程目录：CI 工作流
.gitignore      Git 忽略规则
README.md       GitHub 门牌 + 本地启动说明
check-links.toml 链接检查器配置
docs/           内容目录：文档站根
requirements.txt 链接检查器 Python 依赖
scripts/        工程目录：工具脚本
```

可以清楚地分成三类：

1. **门牌/说明类**：`README.md`（给 GitHub 访客看）、`.gitignore`（告诉 Git 忽略哪些文件）。
2. **内容类**：`docs/`——站点内容全在这里，体量最大。
3. **工程/工具类**：`scripts/`（检查器代码）、`.github/`（CI 配置）、`check-links.toml` + `requirements.txt`（检查器的配置与依赖）。

这个「内容 vs 工程」的二分法是本讲最重要的心智模型：以后改动站点内容去 `docs/`，改动检查逻辑去 `scripts/`。

#### 4.1.2 核心流程

当你 clone 这个仓库后，根目录的文件按如下方式协同：

1. 访客打开 GitHub 仓库主页 → 看到 `README.md`，得知「这项目基于 Docsify」。
2. 访客按 `README.md` 指引跑 `docsify serve docs` → 浏览器读取 `docs/index.html`，进入内容目录 `docs/`。
3. 贡献者改完文档提交 → 触发 `.github/workflows/check-links.yml` → 调用 `scripts/check_links.py`，读取 `check-links.toml` 配置来校验链接。

也就是说：根目录文件本身不参与站点渲染，而是「指挥」内容目录和工具目录如何运转。

#### 4.1.3 源码精读

`README.md` 用两段说明了项目性质和启动方式：

[README.md:9-19](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/README.md#L9-L19) —— 这段说明本项目「基于 Docsify」，并给出 `npm i docsify-cli -g`（第 12 行）和 `docsify serve docs`（第 18 行）两条命令。注意这里只有「怎么跑」，没有「编译器源码在哪」——再次印证 u1-l1 的结论：仓库交付的是文档而非编译器。

工程类配置的体量都很小，例如依赖清单只有三行：

[requirements.txt:1-3](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt#L1-L3) —— `httpx`、`markdown-it-py[linkify]`、`selectolax` 三个包，全是给 `scripts/check_links.py` 用的（发请求、解析 Markdown、解析 HTML），与「看文档」本身无关。

#### 4.1.4 代码实践

**目标**：亲手列出当前 HEAD 的全部顶层条目，确认本讲的「7 个」是否属实。

**步骤**：

1. 在仓库根目录执行 `git ls-files | cut -d/ -f1 | sort -u`。
2. 数一数输出的行数，并与本节开头列出的 7 个条目对照。
3. 再执行 `git ls-files | wc -l`，得到被跟踪文件总数。

**需要观察的现象**：第一条命令输出若干行（每行一个顶层条目），第二条命令输出一个数字。

**预期结果**：顶层条目为 7 个；被跟踪文件总数为 91（截至当前 HEAD `d172f89`）。如果你的环境里有未被跟踪的本地文件（例如 `online-doc-tutorial/`），它**不会**出现在 `git ls-files` 里——这正说明该命令只反映仓库已提交的内容。

#### 4.1.5 小练习与答案

**练习 1**：为什么根目录的 `README.md` 不在 `docs/` 里，而 `docs/` 下又有一个 `README.md`？

**参考答案**：根目录 `README.md` 是 GitHub 仓库主页展示的「门牌」，面向仓库访客；`docs/README.md` 才是文档站的首页内容，面向通过浏览器看站点的读者。两者受众不同，所以分开放。

**练习 2**：`requirements.txt` 里的依赖是给「看文档的人」用的，还是给「跑检查器的人」用的？

**参考答案**：是给跑检查器的人（以及 CI）用的。浏览文档只需要 `docsify serve docs`，不依赖任何 Python 包；这三个包只服务于 `scripts/check_links.py`。

---

### 4.2 docs 内容分层

#### 4.2.1 概念说明

`docs/` 是整个仓库的「主角」，体量最大。它的内部结构可以用一句话概括：**一个站点外壳 + 若干按实验阶段分目录的教学内容 + 一个静态资源目录**。

先看 `docs/` 顶层有什么（共 5 个文件 + 14 个子目录）：

| 类型 | 文件/目录 | 说明 |
| --- | --- | --- |
| 站点外壳 | `index.html` | SPA 唯一入口 |
| 站点外壳 | `toc.md` | 侧边栏目录 |
| 站点外壳 | `footer.md` | 页脚 |
| 站点外壳 | `README.md` | 首页正文 |
| 站点外壳 | `.nojekyll` | 告诉 GitHub Pages 别用 Jekyll 处理（避免忽略下划线开头的文件） |
| 教学内容 | `preface/`、`lv0-env-config/` … `lv9p-reincarnation/`、`misc-app-ref/` | 13 个内容子目录 |
| 静态资源 | `assets/` | CSS / JS / 图标（见 4.4） |

#### 4.2.2 核心流程：实验阶段如何映射成目录

教学内容的 13 个子目录，与 `toc.md` 里侧边栏的章节一一对应。命名规律值得记住：

- `preface/` → 「写在前面」。
- `lv0-env-config/` … `lv9-array/` → Lv0 到 Lv9 共 10 个阶段；目录名 = `lv<编号>-<英文主题>`。
- `lv9p-reincarnation/` → Lv9+（**`p` 是 plus 的缩写**，即「Lv9+ 新的开始」可选进阶）。
- `misc-app-ref/` → 「杂项/附录/参考」（miscellaneous / appendix / reference）。

每个内容子目录内部结构高度统一，通常是：

```
lv1-main/
├── README.md        # 本阶段总览（章节首页）
├── structure.md     # 各小节正文
├── lexer-parser.md
├── ...
└── testing.md       # 本阶段测试说明（几乎每个阶段都有）
```

即「一个 `README.md` 当首页 + 若干小节 `.md` + 一个 `testing.md` 讲测试」。`README.md` 在 Docsify 路由里对应「章节根路径」，例如 `/lv1-main/`（回顾 u1-l1 的 SysY→Koopa IR→RISC-V 流水线，每个 Lv 给编译器增加一项能力，正好落在对应目录里）。

#### 4.2.3 源码精读

`docs/toc.md` 是看懂分层的最佳入口——它就是侧边栏本身，**目录名的中文标题在这里逐一对应**：

[docs/toc.md:1-5](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L1-L5) —— 第 1 行 `[写在前面](/preface/)` 对应 `preface/` 目录；第 5 行 `[Lv0. 环境配置](/lv0-env-config/)` 对应 `lv0-env-config/`。

[docs/toc.md:10-11](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L10-L11) —— `[Lv1. main 函数](/lv1-main/)` 与子条目 `[Lv1.1. 编译器的结构](/lv1-main/structure)`，揭示了「章节路径 `/lv1-main/` 指向 `lv1-main/README.md`，子条目 `/lv1-main/structure` 指向 `lv1-main/structure.md`」的路由规律。

[docs/toc.md:50-51](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L50-L51) —— `[Lv9+. 新的开始](/lv9p-reincarnation/)`，印证了 `lv9p-` 中的 `p` = plus = Lv9+。

[docs/toc.md:57-67](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L57-L67) —— 「杂项/附录/参考」一节，列出 SysY 规范、Koopa IR 规范、RISC-V 指令速查等参考资料，全部指向 `/misc-app-ref/`。

站点外壳方面，`docs/index.html` 是 SPA 入口（详见 u1-l4）；`docs/.nojekyll` 是个空文件，仅靠「存在」起作用：它让 GitHub Pages 跳过 Jekyll，从而不漏掉 `assets/` 下以下划线开头的资源。

#### 4.2.4 代码实践

**目标**：盘点 `docs/` 下到底有多少个 `.md` 文件，并验证「每个阶段都有 `README.md`」的规律。

**步骤**：

1. 执行 `git ls-files 'docs/*.md' | wc -l` 统计 `.md` 文件总数（Git 的路径通配符 `*` 在此会跨目录匹配，结果涵盖子目录）。
2. 执行 `git ls-files | grep 'lv.*-\(env-config\|main\|array\)/README.md'`（或直接肉眼浏览第 3 节源码地图列出的文件清单），观察每个 `lv*` 目录是否都含一个 `README.md`。

**需要观察的现象**：第一条命令输出一个数字；第二条命令列出各阶段目录的 `README.md`。

**预期结果**：`docs/` 下 `.md` 文件共 **70 个**（3 个在 `docs/` 顶层：`README.md`、`toc.md`、`footer.md`，其余 67 个分布在 13 个内容子目录里）。每个 `lv*` 阶段目录都有自己的 `README.md`。如果你得到的数字不是 70，先确认 HEAD 是否为 `d172f89`。

> **待本地验证**：若你切换到其它 commit，文件数会随提交历史变化；本讲给出的「91 / 70」均对应当前 HEAD `d172f89`。

#### 4.2.5 小练习与答案

**练习 1**：你会在哪个目录里找到「Lv6. if 语句」的讲义？

**参考答案**：`docs/lv6-if/`。命名规律是 `lv<编号>-<英文主题>`，if 语句对应 `lv6-if`。

**练习 2**：`lv9p-reincarnation/` 里的 `p` 是什么意思？为什么不直接叫 `lv10`？

**参考答案**：`p` 是 plus，即「Lv9+」，表示主线 9 个阶段之后的**可选进阶**，不是强制第 10 阶段，所以不叫 Lv10。它对应 `toc.md` 第 50 行的 `[Lv9+. 新的开始]`。

**练习 3**：`docs/toc.md` 在站点里扮演什么角色？

**参考答案**：它是 Docsify 的侧边栏（回顾 u1-l2 的 `loadSidebar: 'toc.md'`），同时它本身也是「目录名 ↔ 中文标题 ↔ 路由路径」的对照表，是理解内容分层的索引。

---

### 4.3 工程目录（scripts / .github）

#### 4.3.1 概念说明

工程目录与「教学内容」无关，是维护这套文档的「工具间」，只有两块：

- `scripts/`：放可执行脚本，目前只有 `check_links.py`——一个链接检查器。
- `.github/`：GitHub 专属目录，其下 `.github/workflows/` 放 CI 工作流，目前只有 `check-links.yml`。

这两块配合根目录的 `check-links.toml`（配置）和 `requirements.txt`（依赖）一起，构成「写完文档 → 自动检查链接」的闭环。

#### 4.3.2 核心流程

1. CI 被触发（PR 或 push 到 master，且改动命中指定路径）。
2. 工作流 `check-links.yml` 在 `ubuntu-latest` 上 checkout 代码、用 `pip install -r requirements.txt` 装依赖。
3. 运行 `python scripts/check_links.py --config check-links.toml --verbose`。
4. 检查器扫描 `README.md` 和 `docs/`（见 4.4 的 `exclude` 规则），按 Docsify 路由风格解析每条链接，校验本地/远程链接是否有效，发现坏链则让 CI 失败。

这个流程会在第四单元逐行精读，本讲只需理解它在「目录里处于什么位置」。

#### 4.3.3 源码精读

`scripts/check_links.py` 的开头用一段 docstring 直接点明了它如何理解本仓库的 Docsify 路由：

[scripts/check_links.py:1-15](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L1-L15) —— 第 6–8 行说明路由规则：`/path/to/page` 解析成 `docs/path/to/page.md`、`/path/to/` 解析成 `docs/path/to/README.md`、`?id=heading` 解析成标题锚点。这段注释正好对应 4.2 看到的「章节路径指向 README.md」规律，是工具与内容目录之间的契约。

该脚本顶部的默认配置 `DEFAULT_CONFIG` 也透露了它扫描的范围：

[scripts/check_links.py:37-44](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L37-L44) —— `'paths': ['README.md', 'docs']` 说明扫描根目录 `README.md` 和整个 `docs/`；`'exclude': ['docs/assets/**']` 说明它**故意跳过** `docs/assets/`（静态资源里的链接不检查，见 4.4）。

CI 侧，工作流文件定义了触发条件与执行步骤：

[.github/workflows/check-links.yml:3-22](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L3-L22) —— 触发路径只包含 `README.md`、`docs/**`、`scripts/check_links.py`、`requirements.txt`、`check-links.toml` 和工作流自身；只有这些地方有改动才会跑检查，避免无关改动浪费 CI。

[.github/workflows/check-links.yml:29-32](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L29-L32) —— 两个步骤：`pip install -r requirements.txt` 装依赖，然后 `python scripts/check_links.py --config check-links.toml --verbose` 执行检查。注意它**没有**加 `--no-http`，所以 CI 会真实发起 HTTP 请求校验远程链接。

#### 4.3.4 代码实践

**目标**：不运行代码，仅靠目录定位，验证「工具链所需的 4 个文件各就各位」。

**步骤**：

1. 列出工程相关文件：`git ls-files 'scripts/*' '.github/**/*'`。
2. 对照清单核对：`scripts/check_links.py`、`.github/workflows/check-links.yml` 是否都在；再确认根目录有 `check-links.toml` 和 `requirements.txt`（`git ls-files check-links.toml requirements.txt`）。
3. 阅读 `.github/workflows/check-links.yml` 第 30–32 行，确认 CI 调用的命令里出现的 `check-links.toml` 确实是根目录那个配置文件。

**需要观察的现象**：第一条命令列出 scripts 与 .github 下的全部被跟踪文件；后两条命令各自确认对应文件存在。

**预期结果**：`scripts/` 下只有 `check_links.py` 一个文件；`.github/` 下只有 `.github/workflows/check-links.yml`；`check-links.toml` 与 `requirements.txt` 都在根目录。工具链「代码 + 配置 + 依赖 + 触发器」四件套齐全。

#### 4.3.5 小练习与答案

**练习 1**：如果你只改动了 `docs/assets/icons/favicon.ico`，CI 会不会因此触发链接检查？

**参考答案**：会触发（因为 `docs/**` 命中），但检查器又会把 `docs/assets/**` 排除掉（`exclude` 规则）。所以「CI 跑了」但「那部分改动不会被当作链接来源」，两个机制是不同层面的过滤。

**练习 2**：为什么 `check_links.py` 是仓库里「唯一有分量的代码」，却放在很不起眼的 `scripts/` 目录？

**参考答案**：因为本项目的主交付物是文档，不是程序。检查器是辅助文档质量的工具，属于「工程支撑」，所以归类到 `scripts/`，与内容目录 `docs/` 隔离。

---

### 4.4 assets 静态资源

#### 4.4.1 概念说明

`docs/assets/` 是站点「非 Markdown」素材的集中存放地，分三类：

| 子目录 | 内容 | 在站点中的作用 |
| --- | --- | --- |
| `docs/assets/css/` | `main.css` | 自定义样式，覆盖 Docsify 默认主题 |
| `docs/assets/js/` | `prism-koopa.js`、`sidebar.js`、`giscus.js` | 自定义脚本：Koopa IR 语法高亮、侧边栏滚动同步、评论插件 |
| `docs/assets/icons/` | `favicon.ico`、`apple-touch-icon-precomposed-152.png` | 站点图标 |

此外，**正文用到的图片并不统一放进 `assets/`**，而是就近放在各自章节目录里，例如 `docs/lv4-const-n-var/riscv-stack-frame.png`、`docs/lv9p-reincarnation/mandelbrot.png`、`docs/misc-app-ref/judging-1.png`。这是本仓库的一个约定：`assets/` 只放「全站共享」的样式与脚本，章节插图跟着章节走。

#### 4.4.2 核心流程

`assets/` 里的资源如何被站点使用：

1. `docs/index.html` 通过 `<link>` / `<script>` 引入 `assets/css/main.css`、`assets/js/*.js`（这些会在 u2 详讲）。
2. 浏览器以相对于站点根（即 `docs/`）的路径加载它们。
3. 链接检查器在扫描时**跳过**整个 `assets/`，不把其中的 CSS/JS 当作「需要校验链接的源文件」（见 4.3 的 `exclude` 规则）。

#### 4.4.3 源码精读

可以用一条命令看清 `assets/` 的完整构成：

`git ls-files 'docs/assets/**'` 会列出（截至当前 HEAD）：

```
docs/assets/css/main.css
docs/assets/icons/apple-touch-icon-precomposed-152.png
docs/assets/icons/favicon.ico
docs/assets/js/giscus.js
docs/assets/js/prism-koopa.js
docs/assets/js/sidebar.js
```

而检查器把它整体排除的依据，就是 4.3 已引用的默认配置第 42 行：

[scripts/check_links.py:42](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L42) —— `'exclude': ['docs/assets/**']`，用 `**` 通配递归排除整个资源目录。

至于「章节插图跟着章节走」的约定，可以从内容目录里的非 `.md` 文件看出来，例如：

`git ls-files 'docs/lv4-const-n-var/*'` 会同时列出 `README.md`、`const.md` 等正文，以及 `example-stack-frame.png`、`riscv-stack-frame.png` 两张插图——它们和正文同目录，引用时用相对路径即可。

#### 4.4.4 代码实践

**目标**：把「全站共享资源」与「章节插图」区分开。

**步骤**：

1. 执行 `git ls-files 'docs/assets/**'`，确认 `assets/` 下只有 CSS/JS/图标三类，没有 Markdown。
2. 执行 `git ls-files 'docs/**/*.png'`，列出所有 PNG 图片，观察它们的路径前缀。

**需要观察的现象**：第一条命令输出 6 个非 Markdown 资源文件；第二条命令输出若干 `.png`，且**没有一个**位于 `assets/` 下。

**预期结果**：所有 PNG 都散落在各章节目录（`lv4-const-n-var/`、`lv8-func-n-global/`、`lv9p-reincarnation/`、`misc-app-ref/`），印证「章节插图就近放置」的约定；`assets/` 仅收纳全站样式与脚本。

#### 4.4.5 小练习与答案

**练习 1**：我想给全站换一套正文字体，应该改哪个文件？

**参考答案**：`docs/assets/css/main.css`（自定义样式都在这里）。

**练习 2**：`assets/js/` 下三个脚本分别负责什么？（提示：回顾 u2 大纲或直接看文件名）

**参考答案**：`prism-koopa.js` 负责 Koopa IR 语法高亮，`sidebar.js` 负责侧边栏与正文滚动同步，`giscus.js` 负责接入 GitHub Discussions 评论。它们的详细实现会在第二单元精读。

---

## 5. 综合实践

把本讲的知识串起来，完成一张「仓库目录地图」。

**任务**：

1. 在本地或纸上画出仓库的目录树（到二级深度即可），形如：

   ```
   online-doc/
   ├── README.md
   ├── check-links.toml
   ├── requirements.txt
   ├── .github/workflows/check-links.yml
   ├── scripts/check_links.py
   └── docs/
       ├── index.html / toc.md / footer.md / README.md / .nojekyll
       ├── assets/{css,js,icons}/
       ├── preface/
       ├── lv0-env-config/ … lv9-array/
       ├── lv9p-reincarnation/
       └── misc-app-ref/
   ```

2. 在每个目录后面用一句话标注职责（内容 / 工程 / 外壳 / 资源）。
3. 用 `git ls-files 'docs/*.md' | wc -l` 得到 `docs/` 下的 `.md` 文件数（应为 70），并解释为何此数字与「被跟踪文件总数 91」不同（差额来自非 `.md` 文件：`index.html`、`.nojekyll`、图片、CSS/JS 等）。
4. 最后做一次「定位练习」：分别说出以下需求该去哪个目录或文件——
   - 「改 Docsify 配置」→ `docs/index.html`
   - 「改 Lv7 while 讲义正文」→ `docs/lv7-while/`
   - 「改全站样式」→ `docs/assets/css/main.css`
   - 「改链接检查器逻辑」→ `scripts/check_links.py`
   - 「改 CI 触发条件」→ `.github/workflows/check-links.yml`

**预期结果**：得到一张带注释的二级目录树，以及一组「需求 → 路径」的准确映射；`docs/` 下 `.md` 数为 70，被跟踪总数 91，差额 21 个非 `.md` 文件可由 `git ls-files | grep -vc '\.md$'`（注意根目录 `README.md` 也是 `.md`）自行核验。

## 6. 本讲小结

- 仓库根目录共 7 个顶层条目，可分三类：门牌说明（`README.md`、`.gitignore`）、内容（`docs/`）、工程（`scripts/`、`.github/`、`check-links.toml`、`requirements.txt`）。
- `docs/` 是主交付物，结构为「站点外壳（`index.html`/`toc.md`/`footer.md`/`README.md`/`.nojekyll`）+ 13 个按实验阶段分层的内容子目录 + `assets/` 资源目录」。
- 内容子目录命名规律：`lv<编号>-<英文主题>`，`lv9p-` 的 `p` = plus（Lv9+），`misc-app-ref/` 是杂项/附录/参考；每个阶段目录通常含一个 `README.md` + 若干小节 + 一个 `testing.md`。
- 工程目录 `scripts/` + `.github/` 与根目录的配置/依赖一起构成链接检查闭环；检查器按 Docsify 路由解析链接，并在扫描时排除 `docs/assets/**`。
- `assets/` 只放全站共享的 CSS/JS/图标；章节插图就近放在各自章节目录里。
- 当前 HEAD `d172f89` 下，仓库共 91 个被跟踪文件，其中 `docs/` 下有 70 个 `.md` 文件。

## 7. 下一步学习建议

- 想搞清楚站点外壳如何把这一切串成网页，请进入 **u1-l4（站点入口与导航机制）**，精读 `docs/index.html`、`toc.md`、`footer.md`。
- 想理解全站样式与脚本，进入 **第二单元（Docsify 站点机制）**，先读 `docs/assets/css/main.css` 与 `docs/assets/js/*.js`。
- 想读仓库里唯一的程序代码，直接跳到 **第四单元（链接检查器架构）**，从 `scripts/check_links.py` 的总览与配置（u4-l1）开始。
- 如果想巩固本讲的「目录直觉」，建议用 `git ls-files` 配合 `docs/toc.md` 多做几次「中文标题 ↔ 目录名 ↔ 路由路径」的三向对照练习。
