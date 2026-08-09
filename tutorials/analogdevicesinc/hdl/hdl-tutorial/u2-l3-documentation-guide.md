# 文档体系导航

## 1. 本讲目标

通过本讲，你应当能够：

- 看懂 `docs/` 目录的 Sphinx 文档骨架，知道每类内容（架构、构建、移植、编码规范、IP、参考设计、寄存器表）分别放在哪里。
- 在不读源码的情况下，也能快速定位到「某个 IP 的框图、参数、接口、寄存器表」这类查阅型信息。
- 在本地用 Sphinx 把整本文档构建出来，得到一个可离线浏览的 HTML 站点。

本讲是一篇「查得开地图」的导览：不深入任何一项技术细节，而是把全仓文档的「索引页」交到你手上。前置依赖是 [u1-l2 仓库与目录结构导览](u1-l2-repo-structure.md)——你已经知道 `library/` 是 IP 积木、`projects/` 是整板参考设计；本讲告诉你这些内容对应的**可读文档**在哪里。

## 2. 前置知识

### 2.1 什么是 Sphinx / reStructuredText

ADI HDL 的文档不是 Markdown，而是用 **reStructuredText（简称 rst）** 写的，由 **Sphinx** 这个文档生成器编译成 HTML 网站。

- **rst**：一种带结构的纯文本标记语言，类似 Markdown，但更强于「交叉引用」和「目录树（toctree）」。
- **toctree**：rst 里声明「这个页面下挂哪些子页面」的指令，Sphinx 据此生成左侧导航树。
- **角色（role）与指令（directive）**：rst 里 `:ref:`label`` 是交叉引用角色，`.. hdl-regmap::` 这种以 `..` 开头的是指令。ADI 用自定义指令把寄存器表、框图、参数表自动渲染进文档。

### 2.2 adi_doctools 是什么

ADI 自己写了一个 Sphinx 扩展 `adi_doctools`，它注册了一批 `hdl-*` 自定义指令（如 `hdl-regmap`、`hdl-parameters`、`hdl-component-diagram`），让文档可以「从源码与文本文件自动生成表格」，而不需要手写。这是 ADI 文档能保持与代码同步的关键。

### 2.3 三个名词对照

| 名词 | 在本仓库指什么 |
| --- | --- |
| 文档源 | `docs/` 下的 `.rst` 文本与 `.svg` 图片 |
| 文档构建 | 把 rst 编译成 HTML 的过程（`make html`） |
| 在线文档 | GitHub Pages 上已经构建好的站点 `analogdevicesinc.github.io/hdl` |

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 仓库首页，含「本地构建文档」的命令清单 |
| `docs/index.rst` | 文档总入口，定义三大顶层栏目（user_guide / library / projects） |
| `docs/conf.py` | Sphinx 配置，声明扩展、主题与外部仓库交叉引用 |
| `docs/Makefile` / `docs/make.bat` | 触发 `sphinx-build` 的封装（Linux/mac 与 Windows） |
| `docs/requirements.txt` | 文档构建依赖（adi_doctools、sphinx、matplotlib、svglib） |
| `docs/user_guide/index.rst` | 「用户指南」索引页：架构、构建、移植、规范等 |
| `docs/library/index.rst` | 「IP 核」索引页：按类别列出近百个 IP 的文档 |
| `docs/projects/index.rst` | 「参考设计」索引页：列出各评估板工程文档 |
| `docs/library/axi_dmac/index.rst` | 单个 IP 文档的样本，演示框图/参数/接口/寄存器四大块 |
| `docs/regmap/adi_regmap_dmac.txt` | 寄存器表纯文本源，被 `hdl-regmap` 指令消费 |

## 4. 核心概念与源码讲解

### 4.1 docs 目录结构总览

#### 4.1.1 概念说明

`docs/` 是一个独立的 Sphinx 项目。它的最外层是一个 `index.rst`「总入口」，往下用 toctree 分成三大栏目：

1. **User Guide（用户指南）**：面向「用 HDL 的人」，讲架构、怎么构建、怎么移植、编码规范。
2. **Library / IP Cores（IP 核）**：面向「查某个 IP 的人」，每个 IP 一页框图+参数+接口+寄存器。
3. **Projects（参考设计）**：面向「查某块评估板工程的人」，每个工程一页说明。

另外还有一个 `regmap/` 目录，里面是 33 个纯文本寄存器表，本身不直接出现在导航树里，而是被各 IP 文档的 `hdl-regmap` 指令「引用渲染」。

#### 4.1.2 核心流程

文档从源到可浏览站点，逻辑上经历：

```text
docs/*.rst + docs/regmap/*.txt + docs/library/*/block_diagram.svg
        │
        ├── index.rst 用 3 个 toctree 把 user_guide / library / projects 串成目录树
        │
        ▼
   sphinx-build（加载 conf.py 中声明的 adi_doctools 扩展）
        │
        ├── 把 hdl-regmap 指令 → 渲染成寄存器表
        ├── 把 hdl-parameters / hdl-interfaces 指令 → 渲染成参数/接口表
        ├── 把 block_diagram.svg → 嵌入页面
        │
        ▼
   docs/_build/html/   （离线 HTML 站点）
```

关键点：**导航结构 = toctree 的嵌套**。你看懂了 `index.rst` 的三个 toctree，就看懂了整个文档的「目录」。

#### 4.1.3 源码精读

**总入口 `docs/index.rst` 用三个 toctree 划分三大栏目**：

[docs/index.rst:31-47](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/index.rst#L31-L47)：这三段 `.. toctree::` 分别挂载 `user_guide/index`、`library/index`、`projects/index` 三个子索引页。`maxdepth` 控制左侧导航展开多少层。

注意顶部一行：

[docs/index.rst:26-26](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/index.rst#L26-L26)：`.. hdl-build-status::` 是 adi_doctools 提供的自定义指令，会自动渲染一张「各工程构建状态」的徽章表——这正是自定义扩展能力的体现。

**Sphinx 配置 `docs/conf.py` 声明了 adi_doctools 扩展**：

[docs/conf.py:15-18](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/conf.py#L15-L18)：`extensions` 列表里只有 `sphinx.ext.todo` 和 `adi_doctools` 两个。没有后者，所有 `hdl-*` 指令都会报错，文档无法构建。

[docs/conf.py:20-22](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/conf.py#L20-L22)：`needs_extensions` 钉死了 `adi_doctools` 必须是 `0.3.47` 及以上版本，保证渲染行为一致。

[docs/conf.py:56-56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/conf.py#L56-L56)：HTML 主题是 `cosmic`（这是 adi_doctools 提供的主题，不是 Sphinx 自带主题）。

[docs/conf.py:29-38](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/conf.py#L29-L38)：`interref_repos` 列表声明了跨仓库交叉引用目标（kuiper、no-OS、linux、pyadi-iio 等）。这正是文档里那些 `:git-no-OS:`、`:dokuwiki:` 角色能跳到外部仓库的原因——它把 ADI 各个文档仓库织成一张网。

#### 4.1.4 代码实践

**实践目标**：在文档树里「导航」一次，亲手走通 toctree 的嵌套。

**操作步骤**：

1. 打开 `docs/index.rst`，找到三个 `.. toctree::`，记下它们各自挂载的子索引页文件名。
2. 打开 `docs/conf.py`，确认 `extensions` 与 `html_theme`。
3. 统计三个目录的规模：`docs/user_guide/`（页面级 rst）、`docs/library/`（IP 文档目录数）、`docs/projects/`（工程文档目录数）、`docs/regmap/`（寄存器表文件数）。

**需要观察的现象**：`docs/library/` 与 `docs/projects/` 下的子目录数，与 `library/`、`projects/` 源码目录数并不完全相等——并非每个 IP / 工程都已经有 rst 文档。

**预期结果**：截至当前 HEAD，`docs/library/` 约 58 个条目、`docs/projects/` 约 103 个条目、`docs/regmap/` 33 个寄存器表文件。（具体数字以本地 `ls | wc -l` 为准。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `docs/index.rst` 要用三个 toctree，而不是把所有页面塞进一个 toctree？
**答案**：三个 toctree 对应三种不同读者意图（用、查 IP、查工程），分别设置不同的 `maxdepth` 和 `:titlesonly:`，让左侧导航更清爽；同时也方便三个栏目各自独立维护。

**练习 2**：如果构建文档时报「unknown directive hdl-regmap」，最可能的原因是什么？
**答案**：`adi_doctools` 扩展没有正确安装或版本不满足 `needs_extensions` 的 `0.3.47` 要求，导致 `hdl-regmap` 这个自定义指令未被注册。

---

### 4.2 user_guide 与 library 文档的用途

#### 4.2.1 概念说明

三大栏目里，最常用的两个是 **user_guide**（告诉你「怎么做」）和 **library**（告诉你「某个 IP 是什么」）。它们的写作风格截然不同：

- **user_guide**：线性阅读型的「操作手册」，一篇解决一个问题（怎么构建、怎么移植、怎么写规范代码）。
- **library**：查阅型的「数据手册」，每个 IP 一页，结构高度统一（框图 → 参数 → 接口 → 寄存器 → 工作原理）。

projects 栏目则是「这个评估板工程由哪些 IP 组成、怎么连」，本讲只点到为止，详细连线留待后续讲义。

#### 4.2.2 核心流程

**查阅一个 IP 的标准动线**：

```text
想了解 axi_dmac
   │
   ▼
docs/library/index.rst  →  按「DMA」分类找到 axi_dmac/index
   │
   ▼
docs/library/axi_dmac/index.rst
   ├── hdl-component-diagram   （组件框图）
   ├── block_diagram.svg       （数据通路框图）
   ├── hdl-parameters          （综合参数表，如 DMA_DATA_WIDTH_SRC）
   ├── hdl-interfaces          （端口/接口表，如 s_axi、m_axis）
   ├── hdl-regmap :name: DMAC  （寄存器表，渲染自 docs/regmap/adi_regmap_dmac.txt）
   └── Theory of Operation     （工作原理长文）
```

**查找一条「怎么做」的标准动线**：

```text
想知道怎么构建 / 移植 / 写规范代码
   │
   ▼
docs/user_guide/index.rst  →  在隐藏 toctree 里找对应页面
   ├── build_hdl       （构建工程）
   ├── architecture    （三层架构）
   ├── porting_project （移植到新载板）
   └── hdl_coding_guidelines （编码规范）
```

#### 4.2.3 源码精读

**user_guide 的索引页是一个「隐藏 toctree + 编号清单」**：

[docs/user_guide/index.rst:16-32](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/index.rst#L16-L32)：这段 `.. toctree:: :hidden:` 列出了 14 个子页面，从 Introduction、Git repository、Releases，到 Build、Architecture、Porting、Coding guidelines、Contributing。注意每行形如 `Build an HDL project <build_hdl>`——尖括号前是显示标题，括号内是不带后缀的 rst 文件名。`hidden` 表示不在页面正文里渲染成列表（正文用的是下面那段编号清单 `:ref:`build_hdl`:`)。

对应到本讲实践任务要找的四份关键文档，它们的引用标签正是：

| 用途 | rst 文件 | 引用标签（`:ref:` 用） |
| --- | --- | --- |
| HDL 架构说明 | `docs/user_guide/architecture.rst` | `architecture` |
| 构建/生成编程文件 | `docs/user_guide/build_hdl.rst` | `build_hdl` |
| 移植工程到新载板 | `docs/user_guide/porting_project.rst` | `porting_project` |
| HDL 编码规范 | `docs/user_guide/hdl_coding_guidelines.rst` | `hdl_coding_guidelines` |

**library 索引按类别组织 IP**：

[docs/library/index.rst:10-18](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/index.rst#L10-L18)：开头是 `Frameworks`（i3c_controller、jesd204、spi_engine 三大框架），其后依次是 ADC/DAC、Data Offload、DMA、Video、Utilities、Obsolete IPs 等分类。每个条目 `axi_dmac/index` 指向该 IP 自己的文档目录。

**单个 IP 文档的「四件套」结构（以 axi_dmac 为样本）**：

[docs/library/axi_dmac/index.rst:6-7](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_dmac/index.rst#L6-L7)：`.. hdl-component-diagram::` 渲染组件关系图。

[docs/library/axi_dmac/index.rst:67-69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_dmac/index.rst#L67-L69)：用普通 `.. image:: block_diagram.svg` 嵌入手绘的数据通路框图（每个 IP 目录下都有一张同名 svg）。

[docs/library/axi_dmac/index.rst:74-74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_dmac/index.rst#L74-L74)：`.. hdl-parameters::` 自动生成参数表（如 `DMA_DATA_WIDTH_SRC`），数据来自 IP 打包脚本。

[docs/library/axi_dmac/index.rst:149-149](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_dmac/index.rst#L149-L149)：`.. hdl-interfaces::` 自动生成接口/端口表。

[docs/library/axi_dmac/index.rst:214-215](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_dmac/index.rst#L214-L215)：`.. hdl-regmap:: :name: DMAC` 把寄存器表渲染进来。`:name: DMAC` 这个名字用来在 `docs/regmap/` 里找到对应的纯文本源文件——`adi_doctools` 会匹配 `adi_regmap_dmac.txt`（名字来自文档里 `TITLE` 块的第三行 `DMAC`，见下）。

**寄存器表的纯文本源**：

[docs/regmap/adi_regmap_dmac.txt:1-13](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt#L1-L13)：这是一个简单的人类可读格式——`TITLE` 块声明外设名与简称（`DMAC`），再用 `REG ... ENDREG` 描述寄存器、`FIELD ... ENDFIELD` 描述位域与读写属性（如 `RO` 只读）。`hdl-regmap` 指令就是把它解析成 HTML 表格。这种「文本单独存放、文档引用渲染」的设计，让寄存器表可以被脚本自动生成与校验。

#### 4.2.4 代码实践

**实践目标**：用上面的动线，亲手「查」一个 IP 的框图与寄存器。

**操作步骤**：

1. 在 `docs/library/index.rst` 的 `DMA` 分类下找到 `axi_dmac/index`。
2. 打开 `docs/library/axi_dmac/index.rst`，依次定位 `hdl-component-diagram`、`block_diagram.svg`、`hdl-parameters`、`hdl-interfaces`、`hdl-regmap` 五个指令。
3. 打开 `docs/regmap/adi_regmap_dmac.txt`，对照文档中寄存器表的 `VERSION`（`0x000`）寄存器，确认文本源里的 `REG 0x000 VERSION` 与文档表格一致。

**需要观察的现象**：文档里寄存器表的每一行，都能在 `docs/regmap/*.txt` 里找到对应的 `REG`/`FIELD` 块；二者是「源 ↔ 渲染」关系。

**预期结果**：`adi_regmap_dmac.txt` 的 `VERSION` 寄存器地址是 `0x000`，分 `VERSION_MAJOR [31:16]`、`VERSION_MINOR [15:8]`、`VERSION_PATCH [7:0]` 三个只读位域，与 `index.rst` 文档中「The `VERSION` (`0x000`) register」的描述一致。

#### 4.2.5 小练习与答案

**练习 1**：`hdl-regmap :: :name: DMAC` 是怎么找到 `adi_regmap_dmac.txt` 这个文件的？
**答案**：adi_doctools 用 `:name:` 给出的简称 `DMAC` 去匹配 `docs/regmap/` 下各 txt 文件 `TITLE` 块里登记的简称；`adi_regmap_dmac.txt` 的 TITLE 第三行正是 `DMAC`，因此被选中。

**练习 2**：user_guide 索引页的 toctree 用了 `:hidden:`，那读者怎么看到这 14 个子页面的入口？
**答案**：`:hidden:` 只是不在页面正文渲染目录列表，子页面仍会出现在左侧导航树；此外页面正文里还有一段编号清单（用 `:ref:`label`` 交叉引用）作为人类可读的入口。

---

### 4.3 本地文档构建流程

#### 4.3.1 概念说明

在线文档（GitHub Pages）是官方预先构建好的快照。当你需要：

- 离线浏览；
- 看当前 HEAD 的最新文档（在线版可能有延迟）；
- 验证自己改的 rst 是否渲染正确；

就需要在本地把文档「构建」出来。构建本质上就是用 `sphinx-build` 把 rst 编译成 HTML，但因为 ADI 用了 `adi_doctools` 扩展，需要先装好这个扩展。

#### 4.3.2 核心流程

README 把本地构建文档明确标为「developer purposes only（仅供开发者）」，流程是固定的四步：

```text
1. 升级 pip            →  pip install pip --upgrade
2. 安装文档工具依赖     →  (cd docs ; pip install -r requirements.txt --upgrade)
3. （推荐）构建库 IP     →  (cd library ; make)        ← 生成 hdl-parameters 等所需元数据
4. 用 Sphinx 构建文档   →  (cd docs ; make html)       ← 产出 docs/_build/html
```

其中第 3 步容易被忽略但很关键：很多 `hdl-*` 指令（如 `hdl-parameters`、`hdl-interfaces`）依赖 `library/` 下的 IP 打包产物（component.xml 等），所以建议先 `make` 一次库，再构建文档，否则部分表格可能为空或报缺失。

#### 4.3.3 源码精读

**README 给出的官方构建步骤**：

[README.md:96-120](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L96-L120)：这就是上面四步命令的出处。注意它被包在一个 `<details>` 折叠块里，标题写明 `Building documentation (developer purposes only)`，说明这不是普通用户必经步骤。最终产物路径在 [README.md:118-118](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L118-L118) 写明：`docs/_build/html`。

**文档构建依赖清单**：

[docs/requirements.txt:1-4](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/requirements.txt#L1-L4)：四行分别是 `adi-doctools.tar.gz`（从 GitHub release 下载的 ADI 自研扩展）、`sphinx`、`matplotlib`、`svglib`。后两个用于把 svg 框图和 matplotlib 图正确渲染。

**docs/Makefile 是 sphinx-build 的薄封装**：

[docs/Makefile:1-13](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/Makefile#L1-L13)：`make html` 实际执行的是 `sphinx-build -M html . _build`。注意 [docs/Makefile:2-2](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/Makefile#L2-L2) 的 `SPHINXOPTS ?= -j $(shell nproc)`——默认按 CPU 核数并行构建，能显著加速。Windows 用户则用等价的 [docs/make.bat](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/make.bat)，它做了「检查 sphinx-build 是否在 PATH」的友好提示。

#### 4.3.4 代码实践

**实践目标**：在本地生成一次文档站点，并打开看一眼。

**操作步骤**（在仓库根目录执行）：

```bash
# 1. 确保 pip 够新（README 要求 > 23）
pip install pip --upgrade

# 2. 安装文档工具
(cd docs ; pip install -r requirements.txt --upgrade)

# 3. （推荐）先构建库，生成 IP 元数据
(cd library ; make)

# 4. 构建 HTML 文档
(cd docs ; make html)

# 5. 在浏览器打开（或用任意静态服务器）
#    直接打开文件：
xdg-open docs/_build/html/index.html    # Linux
#    或起一个本地服务器：
(cd docs/_build/html ; python3 -m http.server 8000)
#    然后浏览器访问 http://localhost:8000
```

**需要观察的现象**：

- 第 4 步终端会打印每个 rst 页面的构建日志，最后输出 `build succeeded`。
- `docs/_build/html/` 目录下出现完整的 HTML 站点，含 `index.html`。
- 打开后，左侧导航应能看到 User Guide / IP Cores / Projects 三大栏目；点开某个 IP 能看到框图与寄存器表。

**预期结果**：构建成功，`docs/_build/html/library/axi_dmac/` 下有完整页面，寄存器表正常渲染。

**待本地验证**：如果跳过第 3 步 `(cd library ; make)`，部分 IP 的参数/接口表可能为空——请对比「做了第 3 步」与「跳过第 3 步」两种情况下 `axi_dmac` 页面参数表的差异，观察现象并记下来。完整构建库 IP 需要安装 Vivado/Quartus 工具链；若本机没有工具链，第 3 步可能失败，此时文档主体仍可构建，只是部分自动生成的表格会缺失，属正常现象。

> 说明：以上命令来自 README，本讲义并未实际运行它们；是否能在你的机器上一次跑通，取决于 Python 与工具链环境，标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 建议「构建库（`make`）」之后再「构建文档」？
**答案**：因为 `hdl-parameters`、`hdl-interfaces` 等指令依赖 `library/` 下 IP 的打包产物（如 component.xml）来自动生成参数与接口表；先构建库能保证这些元数据存在，文档表格才完整。

**练习 2**：`make html` 实际执行的底层命令是什么？为什么构建比较快？
**答案**：底层是 `sphinx-build -M html . _build`，并通过 `SPHINXOPTS ?= -j $(shell nproc)` 默认按 CPU 核数并行编译，所以比单线程快很多。

---

## 5. 综合实践

**任务**：为 ADI HDL 文档做一份「个人速查地图」。

把本讲的三条动线合并成一个可操作的小任务：

1. **建立 user_guide 速查表**：打开 `docs/user_guide/index.rst`，把 14 个子页面的「显示标题 ↔ rst 文件名 ↔ `:ref:` 标签」整理成一张表。重点标出本讲实践任务要求定位的四份文档：
   - 架构说明 → `architecture.rst`
   - 构建指南 → `build_hdl.rst`
   - 移植指南 → `porting_project.rst`
   - HDL 编码规范 → `hdl_coding_guidelines.rst`
2. **建立 library 查阅清单**：从 `docs/library/index.rst` 任选 3 个你感兴趣的 IP（建议至少包含 `axi_dmac`），分别记录它们的：分类、框图 svg 文件名、寄存器表对应的 `docs/regmap/*.txt` 文件名。
3. **本地构建验证**：按 4.3.4 的命令清单尝试本地构建一次文档（若工具链受限，至少完成到第 4 步 `make html`）。构建完成后，在浏览器里导航到你整理的某个 IP 页面，确认框图与寄存器表都渲染出来了。

**交付物**：一张 Markdown 表格（user_guide 速查）+ 一份 IP 查阅清单 + 一句「本地构建是否成功」的结论。

这个任务把「找文档」「读文档结构」「构建文档」三件事串起来，完成后你就拥有了独立查阅 ADI HDL 全部文档的能力。

## 6. 本讲小结

- `docs/` 是一个 Sphinx 项目，`docs/index.rst` 用三个 toctree 把文档分成 **User Guide / Library(IP) / Projects** 三大栏目。
- **user_guide** 是线性操作手册（架构、构建、移植、规范等），入口在 `docs/user_guide/index.rst` 的隐藏 toctree 里。
- **library** 是查阅型数据手册，每个 IP 一页，统一结构为「组件图 → 框图 → 参数 → 接口 → 寄存器 → 原理」。
- 寄存器表是「文本源 + 指令渲染」分离的：`docs/regmap/*.txt` 是源，`.. hdl-regmap:: :name: XXX` 在 IP 页面里把它渲染成表。
- `adi_doctools` 扩展（`conf.py` 中声明，版本 `0.3.47`）是所有 `hdl-*` 指令和 `cosmic` 主题的来源，没有它文档无法构建。
- 本地构建按 README 的四步命令：升级 pip → 装依赖 → 构建库 → `make html`，产物在 `docs/_build/html`。

## 7. 下一步学习建议

- 想真正理解一份 user_guide 文档的内容？下一站读 **`docs/user_guide/architecture.rst`**，它对应讲义 [u2-l1 三层工程架构](u2-l1-three-layer-architecture.md)，把本讲提到的「三层设计」讲透。
- 想理解构建系统？进入 **u3 单元（构建系统：Make 与 Tcl 流水线）**，从 [u3-l1 GNU Make 的整体编排](u3-l1-make-orchestration.md) 开始。
- 想深入某个 IP 的数据通路？进入 **u5 单元**，从 [u5-l1 DMA 引擎 axi_dmac 深入](u5-l1-axi-dmac.md) 开始，把本讲当作查阅入口、把 u5 当作原理深读。
- 如果你要给项目贡献文档，先读 `docs/user_guide/docs_guidelines.rst`（文档写作规范），它和本讲的「四件套结构」直接相关。
