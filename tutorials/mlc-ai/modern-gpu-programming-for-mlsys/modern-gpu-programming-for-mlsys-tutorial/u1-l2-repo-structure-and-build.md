# 仓库结构与本地构建

## 1. 本讲目标

上一讲（u1-l1）我们明确了这个仓库是什么：一本面向 Blackwell GPU 与 TIRx DSL 的开源内核编程教材。本讲把镜头拉近，回答三个非常具体的问题：

1. **这本书的"源代码"长什么样？** —— 说出仓库顶层每个目录的职责划分（`chapter_*/`、`appendix/`、`zh/`、`tirx_guide/`、`_extra/`、`img/`、`static/`）。
2. **如何在本地把整本书构建成网站并预览？** —— 掌握 `pip install` + `sphinx-build` + `python -m http.server` 这条三步链路。
3. **在线站点是怎么发布的？** —— 通过 `.github/workflows/build_deploy.yaml` 这条 GitHub Actions 流水线理解自动部署。

学完本讲，你就能在自己的机器上跑起一个和 <https://mlc.ai/modern-gpu-programming-for-mlsys/> 一模一样的本地书站，后续所有章节的阅读、练习、改进都可以在这个本地站点上进行。

## 2. 前置知识

本讲不需要任何 GPU 知识，但需要几个文档工程与 CI 方面的基础概念。用通俗语言逐一解释：

- **Sphinx**：一个文档生成工具（Python 社区最常用的一个），它把一组源文件（Markdown / reStructuredText）组装成一个带导航、搜索、主题样式的多页面网站。Python 官方文档、LLVM 文档都用它构建。
- **Markdown 与 MyST**：Markdown 是常见的轻量标记语言；**MyST**（Markedly Structured Text）是 Markdown 的一套超集扩展，让 Markdown 也能写 Sphinx 特有的指令，比如本仓库大量使用的 `{toctree}`（目录树）和 `{raw} html`（嵌入原生 HTML）。`myst-parser` 就是让 Sphinx 读懂 MyST 的插件。
- **reStructuredText（.rst）**：Sphinx 的"原生"标记语言，功能比 Markdown 强但写法更繁琐。本仓库主体用 Markdown，只有 `tirx_guide/`（TIRx 语言参考）保留 `.rst`。
- **toctree（目录树）**：Sphinx 组织页面的方式。源文件本身不决定顺序，而是在 `index.md` 里用 `{toctree}` 指令声明"哪些页面、按什么顺序、属于哪个分组"，Sphinx 据此生成侧边栏导航。
- **GitHub Actions**：GitHub 内置的持续集成（CI）服务。仓库里放一个 YAML 描述"什么事件发生时、在什么机器上、按什么步骤执行命令"，GitHub 就会在云端替你跑。
- **gh-pages 分支与 GitHub Pages**：一种约定——把构建产物（HTML）推到名为 `gh-pages` 的分支，GitHub 就可以把它当作静态网站发布。本仓库站点最终服务于 `mlc.ai` 域名。
- **pip 与虚拟环境**：`pip install -r requirements-docs.txt` 按清单安装 Python 依赖。建议先 `python -m venv .venv` 创建虚拟环境再安装，避免污染系统 Python。

一个好消息：**构建这本书不需要 GPU，也不需要安装 TVM**。依赖清单的注释里明确写了这一点——书中内核的运行才需要 Blackwell GPU（那是下一讲 u1-l3 的主题）。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 仓库门面：项目介绍、本地构建命令、内核运行环境、部署说明 |
| `index.md` | 英文版首页，内含全书五组 `{toctree}`（Part I–IV + 附录） |
| `conf.py` | 英文版 Sphinx 配置：扩展、排除规则、主题、语言切换按钮 |
| `requirements-docs.txt` | 构建书站的 Python 依赖清单（仅 5 个包，不含 tvm） |
| `.github/workflows/build_deploy.yaml` | CI 流水线：装依赖 → 建英文 → 建中文 → 部署 |
| `zh/index.md`、`zh/conf.py`、`zh/README.md` | 中文镜像的首页、独立 Sphinx 配置与说明 |
| `chapter_*/index.md` | 英文正文，每个主题一章，一章一个目录、一个 `index.md` |
| `appendix/` | 附录（基准测试、调试指南、示例脚本 `nsys_example.py` 等） |
| `tirx_guide/` | TIRx 语言参考与编译器内部（唯一的 `.rst` 区域） |
| `_extra/demo/*.html` | 自包含交互式演示（HTML+CSS+JS），经 iframe 嵌入正文 |
| `img/` 与 `img/scripts/` | 书中插图，以及用 matplotlib 重新生成这些插图的 Python 脚本 |
| `static/` | 站点级静态资源：logo、自定义 CSS/JS、TIRx 布局演示页 |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层目录地图

#### 4.1.1 概念说明

第一眼容易产生误解：这虽然是一个 GitHub 仓库，但它的"产品"不是可安装的软件包，而是一个**文档站点**。仓库的核心资产是 Markdown / RST 源文件和一批静态资源；所谓"构建"，是把源文件渲染成 HTML 书站，而不是编译出二进制。

目录组织遵循两条简单约定：

1. **一章一目录**：每个主题章节占一个 `chapter_xxx/` 目录，里面通常只有一个 `index.md`（例如 `chapter_background/`、`chapter_tmem/` 都只含一个文件）。这与 `index.md` 里 toctree 的条目 `chapter_xxx/index` 一一对应。
2. **中英同构镜像**：`zh/` 子树下有几乎相同的目录结构（`zh/chapter_tma/index.md` 对应 `chapter_tma/index.md`），文件路径一一对应，方便对照与同步翻译。

#### 4.1.2 核心流程

把仓库按"内容源 → 资产 → 配置 → CI"四层分类：

```text
modern-gpu-programming-for-mlsys/
├── index.md                 # 英文首页 + 全书 toctree（书的骨架）
├── chapter_*/index.md       # Part I–IV 各章正文（英文）
├── appendix/                # 附录正文 + 可运行示例脚本
├── tirx_guide/*.rst         # TIRx 语言参考与编译器附录（RST）
├── zh/                      # 中文镜像：zh/index.md、zh/chapter_*/、zh/appendix/…
│   ├── conf.py              #   中文版独立的 Sphinx 配置
│   └── _extra/demo_zh/      #   交互演示的中文版
├── _extra/                  # 交互演示（英文版），构建时原样拷入站点
├── img/                     # 书中插图（成图）
├── img/scripts/             # 生成插图的 matplotlib 脚本（不参与构建）
├── static/                  # logo、custom.css、demo-embed.js 等站点资源
├── conf.py                  # 英文版 Sphinx 配置
├── requirements-docs.txt    # 构建依赖
└── .github/workflows/build_deploy.yaml   # CI：构建 + 部署
```

#### 4.1.3 源码精读

先看全书的"骨架"——英文首页 `index.md` 的目录树。书的四个部分加附录，每组一个 `{toctree}`：

- [index.md:L45-L58](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L45-L58)：第一部分"理解 GPU"的 toctree，按学习顺序列出九个章节（`chapter_background/index` 到 `chapter_clc/index`）。注意条目写的是目录名加 `index`，Sphinx 会解析到 `chapter_background/index.md`。
- [index.md:L60-L93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L60-L93)：其余四组 toctree——第二部分 TIRx 概览、第三部分 GEMM、第四部分 Flash Attention 4、附录。附录条目混合了 `appendix/*` 与 `tirx_guide/*` 两种来源，说明语言参考虽然放在 `tirx_guide/` 目录，导航上仍归入附录分组。

再看 `README.md` 对内容的概览，它是判断"哪个目录对应书的哪一部分"的最快索引：

- [README.md:L16-L30](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L16-L30)：What's inside 一节，用五段话分别概括 Part I–IV 与附录，与上面的 toctree 分组完全对齐。

最后看两个"非正文"目录的组织方式：

- [img/scripts/README.md:L1-L25](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L1-L25)：`img/scripts/` 下每个 `gen_*.py` 脚本对应 `img/` 下的一张或几张图（例如 `gen_roofline.py` 生成 `../roofline.png`）。图是被检入仓库的成品，脚本是"图的源码"——这本身就是一种文档工程上的源码思维。
- [chapter_tma/index.md:L18-L23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L18-L23)：正文中嵌入交互演示的方式——用 MyST 的 `{raw} html` 指令写一个 `<iframe>`，`src` 指向 `../demo/tma_intro.html`。章节页面位于 `_build/html/chapter_tma/`，`../demo/` 即站点根下的 `demo/` 目录，那里是 `_extra/demo/` 被原样拷贝后的位置（机制见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：不借助本讲义，独立说出仓库每个顶层目录的职责，并验证"一章一目录、中英同构"两条约定。

**操作步骤**：

1. 进入仓库根目录，执行 `git ls-files | cut -d/ -f1 | sort | uniq -c`，统计每个顶层目录（及根文件）的文件数。
2. 执行 `git ls-files 'chapter_*'`，观察输出是否几乎全是 `chapter_xxx/index.md` 形态。
3. 执行 `ls zh/`，与顶层目录对比：哪些目录在 `zh/` 下有镜像、哪些没有。

**需要观察的现象**：

- `chapter_*` 目录几乎每个只含一个 `index.md`（`appendix/` 是例外，还含 `nsys_example.py` 等脚本）。
- `zh/` 下镜像了 `chapter_*`、`appendix/`、`tirx_guide/`、`_extra/`，但**没有** `img/` 镜像——中文正文通过 `../../img/xxx_zh.svg` 这样的相对路径直接复用根 `img/` 下的中文版图。

**预期结果**：两条约定得到验证；你能对着统计结果复述每个目录的职责。上述命令为只读 `git` / `ls` 操作，可安全执行。

#### 4.1.5 小练习与答案

**练习 1**：第四部分 Flash Attention 的英文源文件完整路径是什么？其中文对应文件又在哪里？

**答案**：英文是 `chapter_flash_attention/index.md`，中文是 `zh/chapter_flash_attention/index.md`——`zh/` 镜像保持同名路径，直接在英文路径前加 `zh/` 前缀即可定位。

**练习 2**：为什么每章单独一个目录放 `index.md`，而不是把整本书写进一个大 Markdown 文件？

**答案**：（1）Sphinx 的 toctree 条目 `chapter_xxx/index` 天然对应这种布局，页面 URL 形如 `/chapter_tma/index.html`，层级清晰；（2）章节之间独立，中英镜像、逐章修改互不干扰；（3）避免单文件过大，方便协作与 review。

**练习 3**：`tirx_guide/` 与 `chapter_*/` 在源文件格式上有什么差异？

**答案**：`chapter_*/` 全部是 Markdown（`.md`），而 `tirx_guide/` 下全部是 reStructuredText（`.rst`，如 `tirx_guide/language_reference/cuda/buffers.rst`）。`conf.py` 同时注册了两种后缀（见 4.2.3），所以两种格式可以在同一站点共存。

### 4.2 Sphinx/MyST 构建

#### 4.2.1 概念说明

Sphinx 构建的输入是"源文件 + 配置"，输出是一个纯静态 HTML 站点。配置文件 `conf.py` 决定：启用哪些扩展、哪些文件参与构建、用什么主题、额外拷贝哪些资产。理解了 `conf.py`，就理解了这个仓库的构建行为。

一个值得注意的历史背景：`conf.py` 开头注释写明本书**从 d2lbook（D2L 教材工具链）迁移到了纯 Sphinx + MyST-Parser + sphinx-book-theme**。所以仓库里没有 notebook 执行、autodoc 这类复杂机制，构建非常轻量。

#### 4.2.2 核心流程

本地构建与预览的完整链路只有三步：

```text
1. pip install -r requirements-docs.txt   # 安装 sphinx 等构建依赖
2. sphinx-build -b html . _build/html     # 源目录 "." → 输出 _build/html
3. python -m http.server -d _build/html 8000   # 起本地静态服务器预览
```

Sphinx 内部做的事情：读取 `conf.py` → 从 `root_doc`（即 `index.md`）出发沿 toctree 收集所有可达文档 → 逐页渲染 MyST 为 HTML → 套用 `sphinx_book_theme` 主题 → 把 `html_static_path`（`static/`）与 `html_extra_path`（`_extra/`）拷贝到输出目录。**只有 toctree 可达的文件才是内容**，其余文件通过 `exclude_patterns` 显式排除。

#### 4.2.3 源码精读

- [requirements-docs.txt:L1-L7](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/requirements-docs.txt#L1-L7)：全部构建依赖只有 5 个包：`sphinx`、`myst-parser`（MyST 支持）、`sphinx-book-theme`（主题）、`sphinx-copybutton`（代码块一键复制按钮）、`jieba`（中文分词库，服务中文版搜索）。注释明确说明：书没有 autodoc，**构建不需要 tvm**。
- [conf.py:L1-L3](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L1-L3)：文件头注释，交代构建命令与"d2lbook → 纯 Sphinx"的迁移背景。
- [conf.py:L12-L16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L12-L16)：启用 `myst_parser` 与 `sphinx_copybutton` 两个扩展；`source_suffix` 同时注册 `.md`（MyST）与 `.rst`；`root_doc = "index"` 指定从 `index.md` 开始。
- [conf.py:L18-L24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L18-L24)：MyST 扩展开关——`dollarmath`（`$...$` 数学）、`amsmath`、`colon_fence`（`:::` 围栏，正文里的 admonition 大量使用）、`deflist`；`myst_heading_anchors = 3` 为 h1–h3 自动生成锚点。
- [conf.py:L26-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L26-L40)：**`exclude_patterns`，本模块最关键的一段**。排除 `_build`（输出目录）、各处的 `README.md`（它们服务于 GitHub 而不是书站）、`_*.md`（草稿命名约定）、`img/scripts`（图片脚本不是文档）、`.git`/`.github`，以及 **`zh`**——英文构建完全不碰中文目录（中文由单独一次构建完成，见 4.3）。
- [conf.py:L43-L52](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L43-L52)：主题与资产——`html_theme = "sphinx_book_theme"`；`html_static_path = ["static"]` 把 `static/` 下的 CSS/JS 挂到站点；**`html_extra_path = ["_extra"]` 把 `_extra/` 里的内容原样拷贝到站点根**，这就是正文 iframe 能用 `../demo/tma_intro.html` 引到演示页的原因。
- [README.md:L32-L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L32-L49)：仓库自述的本地构建与预览命令，与 CI 使用的命令一致；还给出了远程服务器场景的端口转发提示（`ssh -L 8000:localhost:8000 user@your-server`）。

#### 4.2.4 代码实践

**实践目标**：在本地完成一次完整的英文版构建并预览。

**操作步骤**：

1. 克隆仓库并进入根目录：

   ```bash
   git clone https://github.com/mlc-ai/modern-gpu-programming-for-mlsys.git
   cd modern-gpu-programming-for-mlsys
   ```

2. （推荐）创建并激活虚拟环境：

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. 安装构建依赖并构建：

   ```bash
   pip install -r requirements-docs.txt
   sphinx-build -b html . _build/html
   ```

4. 起本地服务器预览：

   ```bash
   python -m http.server -d _build/html 8000
   ```

   浏览器打开 <http://localhost:8000>。若在远程服务器上构建，先做端口转发 `ssh -L 8000:localhost:8000 user@your-server`。

**需要观察的现象**：

- 构建日志逐页打印 `chapter_*/index` 等页面名，最后输出 "build succeeded"。
- `_build/html/` 下出现 `index.html`、各章目录、`static/`、`demo/`（来自 `_extra/`）、`search.html` 等。
- 打开任一章（如 TMA 一章），页内交互演示的 iframe 能正常加载并交互。

**预期结果**：本地站点与线上站 <https://mlc.ai/modern-gpu-programming-for-mlsys/> 内容一致。本实践命令均摘自 [README.md:L36-L45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L36-L45)，命令本身可信；具体构建耗时与警告输出取决于本机环境，**待本地验证**。常见排查：`sphinx-build: command not found` 说明依赖未装或虚拟环境未激活；某页面 404 多半是 `_build` 里残留了旧产物，删掉 `_build/` 重建即可。

#### 4.2.5 小练习与答案

**练习 1**：执行英文版构建时，`zh/` 目录下的文件会被渲染吗？为什么？

**答案**：不会。[conf.py:L34](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L34) 的 `exclude_patterns` 包含 `"zh"`，英文构建把它整个排除；中文版由第二次独立构建产出（见 4.3）。

**练习 2**：`_extra/demo/tma_intro.html` 是如何出现在最终站点 `_build/html/demo/tma_intro.html` 的？

**答案**：[conf.py:L50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L50) 设置 `html_extra_path = ["_extra"]`，Sphinx 会把该目录下的内容**不经渲染、原样拷贝**到 HTML 输出根目录，因此演示这类自包含 HTML+CSS+JS 资产可以按原样进入站点。

**练习 3**：为什么 `README.md` 和 `img/scripts/` 要被排除在构建之外？

**答案**：`README.md` 服务于 GitHub 仓库首页，不是书的一个章节（书中首页是 `index.md`）；`img/scripts/` 是生成插图的 Python 脚本，属于"图的源码"而非文档。不排除的话 Sphinx 会对这些不可达文件给出警告，CI 又把警告当错误（见 4.4），构建会失败。

### 4.3 zh/ 双语镜像

#### 4.3.1 概念说明

`zh/` 不是"附带的翻译文件堆"，而是**一整棵可独立构建的源树镜像**：它有自己的 `index.md`（中文首页 + 中文 toctree）、自己的 `conf.py`（中文 Sphinx 配置）、自己的 `_extra/demo_zh/`（演示的中文版）。英文构建排除 `zh/`，中文构建以 `zh/` 为源目录单独执行，两份产物最终拼在同一个站点下（英文在根、中文在 `/zh/`）。

这种"镜像 + 双次构建"的设计带来三个直接后果：

1. **路径同构**：中文文件路径 = `zh/` + 英文路径，定位翻译只需加前缀。
2. **配置解耦**：两版可以用不同的 JS、不同的发布策略（中文版按章逐步发布）。
3. **互跳按钮**：两个 `conf.py` 各自注入一个语言切换按钮，按"对方语言是否存在同名文件"决定跳转目标。

#### 4.3.2 核心流程

中文版构建链路与英文版几乎相同，只有源目录和输出目录不同：

```text
sphinx-build -b html zh _build/html/zh
             │         │
             │         └── 输出到英文站点下的 zh/ 子目录
             └── 以 zh/ 为源目录（读取 zh/conf.py）
```

语言切换的判定逻辑（双向对称）：

```text
当前是英文页 P ──► 检查 zh/P.md 是否存在
                    ├─ 存在   ──► 按钮跳转到 zh/P.html（同页中文版）
                    └─ 不存在 ──► 跳转到 zh/index.html（中文首页）
```

#### 4.3.3 源码精读

- [zh/README.md:L1-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/README.md#L1-L9)：中文版一页纸说明——中文内容都在 `zh/` 下，本地构建命令是 `sphinx-build -b html zh _build/html/zh`。
- [zh/index.md:L1-L11](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/index.md#L1-L11)：中文首页开篇，内容与英文 `index.md` 对应；项目名译作《面向机器学习系统的现代 GPU 编程》。
- [zh/index.md:L22-L70](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/index.md#L22-L70)：中文版五组 toctree，条目路径与英文版**完全相同**（`chapter_background/index` 等）——因为构建时源目录已是 `zh/`，同名的目录结构保证同一条目解析到中文文件。这是"中英同构镜像"约定的直接收益。
- [zh/conf.py:L1-L10](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/conf.py#L1-L10)：中文版配置头部，`language = "zh_CN"`，构建命令注释在文件第 2 行。
- [zh/conf.py:L25-L35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/conf.py#L25-L35)：中文版 `exclude_patterns`，注释写明发布策略——**中文版按章发布**：草稿保留在 `zh/` 中，未发布的页面通过排除规则不进入构建产物（这也解释了为什么 CI 在构建中文版前要先删掉旧的 `_build/html/zh`，防止已撤下的页面残留）。
- [zh/conf.py:L37-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/conf.py#L37-L44)：中文版的资产引用方式——logo、favicon、static 都用 `../` 前缀**复用根目录的 `static/`**；`html_extra_path = ["../_extra", "_extra"]` 同时带上英文演示（兜底）与中文演示 `demo_zh/`；JS 换成了 `demo-embed-zh-*.js` 与 `chinese-search.js`。
- [conf.py:L72-L98](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L72-L98)：英文版注入"切换到中文版"按钮的钩子函数——按 `zh/<当前页>.md` 是否存在决定目标是同页中文版还是中文首页，`search`/`genindex` 两页例外（两版都有）。
- [zh/conf.py:L57-L83](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/conf.py#L57-L83)：对称的中文版钩子，注入"Switch to English"按钮，判定逻辑反过来：检查上级目录（英文源树）是否存在同名文件。
- [zh/chapter_tma/index.md:L18-L23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_tma/index.md#L18-L23)：中文正文嵌入演示的 iframe，`src` 指向 `../demo_zh/tma_intro.html`（中文版演示）；而图片如 [zh/chapter_tma/index.md:L150](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_tma/index.md#L150) 用 `../../img/tma_sync_flow_zh.svg` 向上两级回到根 `img/`——因为 `zh/` 下没有 `img/` 镜像，中文插图（文件名带 `_zh` 后缀）直接放在根 `img/` 里共用。

#### 4.3.4 代码实践

**实践目标**：建立英文章节与中文文件的对照表（这正是本讲规格指定的实践任务）。

**操作步骤**：

1. 在已完成的英文构建（4.2.4）基础上，构建中文版：

   ```bash
   sphinx-build -b html zh _build/html/zh
   ```

2. 预览：沿用 `python -m http.server -d _build/html 8000`，访问 <http://localhost:8000/zh/index.html>。
3. 定位对照关系：英文 TMA 章源文件是 `chapter_tma/index.md`，在其中文镜像中对应 `zh/chapter_tma/index.md`。
4. 用一条命令列出全部对照，检查哪些中文文件尚不存在：

   ```bash
   for f in chapter_*/index.md appendix/*.md; do
     [ -f "zh/$f" ] && echo "有翻译: $f" || echo "暂缺:   $f"
   done
   ```

5. 在本地站点的任一英文章节页点击页头语言图标，再在中文页点 "Switch to English"，验证 4.3.2 的双向跳转逻辑。

**需要观察的现象**：

- `_build/html/zh/` 下生成中文站点；iframe 引用的是 `demo_zh/` 下的中文演示。
- 第 4 步的输出是一份"翻译覆盖清单"：多数章节为"有翻译"，少数可能"暂缺"（中文版按章发布，`zh/` 内尚未释放的页面不会出现在构建产物里）。
- 语言按钮在已翻译页面间同页互跳，在未翻译的英文页上则回到中文首页。

**预期结果**：得到一张英文路径 ↔ `zh/` 前缀路径的对照表，且与"镜像目录结构"的推断一致。步骤 4 的脚本为**示例代码**（本讲义编写时未替读者执行），清单中"暂缺"的具体集合取决于当前仓库状态，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`chapter_tma/index.md` 对应的中文文件路径是什么？

**答案**：`zh/chapter_tma/index.md`。镜像约定是"英文路径加 `zh/` 前缀"，目录与文件名保持不变。

**练习 2**：中文正文的 toctree 条目（如 `chapter_tma/index`）与英文版相同，为什么不会解析到英文文件？

**答案**：中文构建以 `zh/` 作为**源目录**启动（`sphinx-build -b html zh ...`），Sphinx 的一切路径解析都以 `zh/` 为根；同名条目因此解析到 `zh/chapter_tma/index.md`。这正是中英目录同构的意义：一份 toctree 可以两用。

**练习 3**：英文页的语言切换按钮在什么情况下跳转到中文首页而不是当前页的中文版？

**答案**：当 `zh/<当前页路径>.md` 不存在时（即该页尚未翻译或未发布）。判定代码在 [conf.py:L78-L87](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L78-L87)：`has_translation` 为假且页面不是 `search`/`genindex` 时，`target_page` 回退为 `index`。

### 4.4 CI 部署（GitHub Actions）

#### 4.4.1 概念说明

GitHub Actions 的流水线由一个 YAML 文件描述，核心概念四个：

- **workflow**：一个 `.github/workflows/*.yaml` 文件即一条流水线；
- **trigger（on）**：什么事件触发（push、pull_request、手动）；
- **job / step**：一个 job 跑在一台临时虚拟机上，由多个 step 顺序组成；
- **action**：可复用的步骤（如 `actions/checkout@v4` 表示检出代码）。

本仓库的流水线 `Build and Deploy` 做的事：**每次对 `main` 的 push 或 PR 都会完整构建英文 + 中文（把警告当错误，保证文档质量）；只有 `main` 上的 push 才会把产物部署到 `gh-pages` 分支**，站点由此发布到 `mlc.ai`。

#### 4.4.2 核心流程

```text
push/PR 到 main
   │
   ▼
checkout 代码 → 装 Python 3.12 → pip install -r requirements-docs.txt
   │
   ▼
sphinx-build -b html . _build/html -W --keep-going        ← 英文版
   │
   ▼
rm -rf _build/html/zh && sphinx-build -b html zh _build/html/zh -W --keep-going
   │                                                        ← 中文版
   ▼
upload-artifact（供下载检查）
   │
   ▼（仅 main 的 push 走这一步）
peaceiris/actions-gh-pages 把 _build/html 推到 gh-pages 分支
   → 站点更新：https://mlc.ai/modern-gpu-programming-for-mlsys/
```

#### 4.4.3 源码精读

- [.github/workflows/build_deploy.yaml:L4-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L4-L9)：触发条件——`push` 到 `main`、`pull_request` 到 `main`、`workflow_dispatch`（网页上手动触发）。
- [.github/workflows/build_deploy.yaml:L11-L13](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L11-L13)：声明 `contents: write` 权限并注释说明——部署用 `peaceiris/actions-gh-pages` 这个 action、以 `GITHUB_TOKEN` 把站点推到 `gh-pages` 分支。
- [.github/workflows/build_deploy.yaml:L19-L28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L19-L28)：环境准备——checkout；`setup-python` 装 Python 3.12 并按 `requirements-docs.txt` 开启 pip 缓存；安装构建依赖。
- [.github/workflows/build_deploy.yaml:L30-L31](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L30-L31)：英文构建命令 `sphinx-build -b html . _build/html -W --keep-going`。注意 `-W`：**把警告当作错误**；`--keep-going`：遇到问题也继续跑完，收集全部警告后统一失败。这就是"文档质量门禁"。
- [.github/workflows/build_deploy.yaml:L33-L34](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L33-L34)：中文构建——先 `rm -rf _build/html/zh` 清掉旧产物再构建，同样带 `-W --keep-going`。删除旧目录是为了配合中文版"按章发布"策略：已撤下的页面不能残留在产物里。
- [.github/workflows/build_deploy.yaml:L36-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L36-L40)：把 `_build/html` 上传为 artifact，供 PR 作者和维基者在云端下载检查，不用于发布。
- [.github/workflows/build_deploy.yaml:L42-L50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L42-L50)：部署步——`if: github.ref == 'refs/heads/main'` 保证**只有 main 分支的 push 才部署**；`publish_dir: ./_build/html` 指定把整个英文+中文产物推到 `gh-pages`。注释还说明：站点没有配自定义域名，服务于组织默认地址 `https://mlc.ai/modern-gpu-programming-for-mlsys/`。
- [README.md:L86-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L86-L89)：README 的 Deployment 一节，用一句话向读者概括了上述整条流水线。

#### 4.4.4 代码实践

**实践目标**：用 CI 同款命令在本地复现质量门禁，并从 workflow 文件推导"哪些事件会导致站点更新"。

**操作步骤**：

1. 在本地（4.2.4 环境基础上）执行 CI 同款严格构建：

   ```bash
   sphinx-build -b html . _build/html -W --keep-going
   ```

2. 打开 `.github/workflows/build_deploy.yaml`，只读文件，回答三个问题（见下方练习）。
3. 在 GitHub 仓库页面的 "Actions" 标签页观察一次真实的流水线运行：每个 step 的日志、artifact 下载入口、`gh-pages` 分支的最新 commit。

**需要观察的现象**：

- 步骤 1 若有任何 Sphinx 警告（如损坏的交叉引用、缺失文件），构建以非零码退出并列出全部警告位置；无警告则与 4.2.4 的普通构建输出基本一致。
- Actions 页面上：PR 触发的运行没有 "Deploy to GitHub Pages" 步（条件不满足被跳过），main push 触发的运行有且成功后站点更新。

**预期结果**：本地严格构建通过（或拿到一张可修复的警告清单）；能准确说出部署的触发边界。本地严格构建的结果**待本地验证**——它取决于你工作区的源文件状态。

#### 4.4.5 小练习与答案

**练习 1**：向 `main` 提一个 pull request，会触发部署吗？会触发构建吗？

**答案**：会触发构建，不会触发部署。PR 匹配 `on: pull_request: branches: [main]`（[L7-L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L7-L8)），因此两轮 Sphinx 构建都会跑（作为质量检查，并上传 artifact）；但部署步有 `if: github.ref == 'refs/heads/main'`（[L43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/.github/workflows/build_deploy.yaml#L43)），PR 的 ref 不满足，直接跳过。

**练习 2**：`-W --keep-going` 两个参数各自的作用是什么？为什么组合使用？

**答案**：`-W`（`--fail-on-warning`）把警告升级为错误；`--keep-going` 让 Sphinx 出错后不立即中止，继续收集剩余所有警告/错误再统一退出。组合的效果是"一次性看到全部问题"，避免修一个警告跑一次构建的循环。

**练习 3**：为什么中文构建前要先 `rm -rf _build/html/zh`？

**答案**：Sphinx 默认增量构建，不会主动删除输出目录里已不存在对应源文件的旧页面。中文版按章发布（见 [zh/conf.py:L33-L34](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/conf.py#L33-L34) 的注释），一旦某页被撤下发布，残留的旧 HTML 仍会被访问到；先删目录保证产物严格等于当前源。

## 5. 综合实践

**任务：把书在本地完整跑起来，并交付一份《仓库结构勘察报告》。**

这个任务贯穿本讲全部三个模块（Sphinx 构建、双语镜像、CI），做完后你对仓库的物理结构将了如指掌，后续十几讲的源码阅读都以此为基础。

1. **构建**：按 4.2.4 的步骤完成英文构建，再按 4.3.4 补充中文构建，最终 `_build/html/`（英文）与 `_build/html/zh/`（中文）并存。
2. **预览**：`python -m http.server -d _build/html 8000`，逐项检查：首页 toctree 分组、任一章正文、章内交互演示 iframe、英文页与中文页的语言切换按钮。
3. **勘察报告**（提交给自己，作为后续学习的地图）：
   - 一张顶层目录职责表（可抄送第 3 节再按自己的理解补充）；
   - 一张中英对照表：对每个 `chapter_*/index.md` 与 `appendix/*.md` 记录 `zh/` 对应文件是否存在（4.3.4 第 4 步的脚本输出）；
   - 一段话回答："我改了 `chapter_tma/index.md` 的一句正文，从 push 到线上站点更新，中间经历了什么？"（应能提到 push 触发 workflow → 严格构建英中两版 → main 分支才部署到 gh-pages）。
4. **对齐验证**：本地执行 CI 同款命令 `sphinx-build -b html . _build/html -W --keep-going`，确认自己的修改不会打破文档质量门禁。

预期产物：一个可浏览的本地书站 + 一份勘察报告。所有构建、预览命令均出自 [README.md:L32-L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L32-L49) 与 CI 文件，本讲义未替读者执行，**待本地验证**。

## 6. 本讲小结

- 这个仓库的产品是**文档站点**：`chapter_*/index.md` 是正文，`index.md` 的五组 `{toctree}` 是全书骨架，"一章一目录 + 中英同构镜像"是两条核心组织约定。
- 构建三步链路：`pip install -r requirements-docs.txt` → `sphinx-build -b html . _build/html` → `python -m http.server -d _build/html 8000`；构建**不需要 GPU 和 tvm**，依赖只有 5 个包。
- `conf.py` 的 `exclude_patterns` 排除了 `zh/`、`README.md`、`img/scripts` 等非正文内容；`html_extra_path = ["_extra"]` 把交互演示原样拷进站点，正文用 iframe 嵌入。
- `zh/` 是可独立构建的中文源树（有自己的 `conf.py`），中文版**按章发布**；语言切换按钮按"对方同名文件是否存在"决定同页跳转还是回首页。
- CI 流水线对每次 push/PR 做两轮 `-W --keep-going` 的严格构建（警告即失败），**只有 main 的 push** 才经 `gh-pages` 分支发布到 `mlc.ai`。

## 7. 下一步学习建议

- **下一讲 u1-l3（运行环境与内核运行方式）**：本地书站只能"读"，要"跑"书中内核还需要 Blackwell GPU（sm_100a）、`apache-tvm==0.26.0` 与 CUDA 版 PyTorch——下一讲把这套运行环境搭起来（或明确无 GPU 时的替代学习方式）。
- **继续阅读本讲涉及的源码**：通读 [conf.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py) 全文（仅百余行）与 [zh/conf.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/conf.py)，对比两份配置的异同，是理解 Sphinx 双语站点的最佳练习。
- **提前浏览**：在本地站点里翻一遍 `chapter_background`（GPU 执行层级）与 `_extra/demo/thread_hierarchy.html`，为单元二（GPU 执行模型）热身。
