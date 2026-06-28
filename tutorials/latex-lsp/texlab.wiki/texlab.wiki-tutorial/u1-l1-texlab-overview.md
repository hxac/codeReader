# texlab 是什么：项目定位与 wiki 导览

## 1. 本讲目标

本讲是整本学习手册的起点。读完本讲，你应当能够：

- 说清楚 **texlab 是什么**：一个基于 LSP（Language Server Protocol）的 LaTeX/BibTeX 语言服务器。
- 理解 **LSP 客户端-服务器架构**，并明确在一个完整的 LaTeX 写作流程里，**编辑器、texlab、TeX 引擎、PDF 阅读器**这四个角色各自负责什么。
- 建立对整本 wiki 的 **全局认知**：知道 7 个页面分别讲什么、它们对应 texlab 的哪一项能力、以及建议的阅读顺序。

> 说明：本仓库 `latex-lsp/texlab.wiki` 是 texlab 的**官方 wiki**，本身就是文档而非可运行源码。因此本系列讲义所说的「源码」指的就是这 7 篇 wiki 文档。我们引用它们时同样给出永久链接和行号，和读普通代码项目的方式一致。

## 2. 前置知识

本讲假设你已经知道以下几件事，如果某项不熟悉，下面给出最简解释：

- **LaTeX**：一种基于 TeX 的排版系统，用 `.tex` 文本文件描述文档（标题、章节、公式、引用等），再由「TeX 引擎」编译成 PDF。本讲不要求你会写 LaTeX，只需要知道「写 .tex → 编译 → 得到 PDF」这个过程。
- **BibTeX**：管理参考文献的格式与工具，文件后缀通常是 `.bib`，配合 `.tex` 里的 `\cite` 命令使用。
- **编辑器（Editor）**：你用来敲代码的工具，比如 VS Code、Neovim、Emacs。编辑器本身通常**不懂** LaTeX 的语法细节。
- **语言服务器（Language Server）**：一个后台程序，专门负责理解某种语言的「智能」——补全、跳转、诊断、悬停提示等。编辑器只要学会和它对话，就能获得这些能力，而不必每个编辑器都自己实现一遍。
- **LSP（Language Server Protocol，语言服务器协议）**：编辑器和语言服务器之间「对话」的标准协议，由微软提出。它本质上是基于 JSON-RPC 的一组约定好的请求/响应/通知。

如果你完全没接触过 LSP，记住一句话即可：**「编辑器当客户端，语言服务器当服务端，两者用 LSP 说同一种话。」** 本讲会把这个模型讲透。

## 3. 本讲源码地图

本讲涉及的「源码」即 wiki 的 7 个 Markdown 文件。先用一张表建立全局印象：

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `Home.md` | wiki 首页，欢迎语 | 入口，确认项目性质 |
| `Project-Detection.md` | 讲 texlab 如何识别一个「项目」并确定根目录 | 说明 texlab 要先把散落的 .tex 组织成项目 |
| `Configuration.md` | texlab 全部配置项（`texlab.*` 命名空间）的总表 | 说明配置由客户端持有、服务器查询 |
| `Previewing.md` | 编译、预览、SyncTeX 与各 PDF 阅读器集成 | 说明 texlab 与 TeX 引擎、PDF 阅读器如何协作 |
| `Tectonic.md` | 用 tectonic 替代默认 TeX 引擎 | 说明 TeX 引擎是可替换的 |
| `LSP-Internals.md` | texlab 对 LSP 的自定义扩展（自定义消息、枚举映射） | 说明 texlab 如何扩展标准 LSP |
| `Workspace-commands.md` | texlab 通过 `workspace/executeCommand` 暴露的命令 | 说明 texlab 提供的工作区命令 |

本讲会重点引用 `Home.md`、`Configuration.md`、`LSP-Internals.md`、`Project-Detection.md`，其余三个页面在「Wiki 导览」一节统一介绍。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位**、**LSP 客户端-服务器架构**、**Wiki 导览**。

### 4.1 项目定位：texlab 到底是什么

#### 4.1.1 概念说明

很多人第一次听到 texlab，会把它和「LaTeX 编辑器」或「TeX 编译器」搞混。准确的定位是：

> **texlab 是一个语言服务器（language server），它让任意支持 LSP 的编辑器获得 LaTeX/BibTeX 的智能编辑能力。**

这里有三点必须分清：

1. **texlab 不是编辑器。** 它没有界面，不直接给你敲字。你在 VS Code / Neovim / Emacs 里敲字，texlab 在后台默默服务。
2. **texlab 不是 TeX 引擎。** 它本身不把 `.tex` 编译成 PDF——真正编译的是 `latexmk`、`pdflatex`、`tectonic` 这些工具。texlab 只是负责**调用**它们、收集它们的输出（日志、辅助文件）、把结果反馈给编辑器。
3. **texlab 也不是 PDF 阅读器。** 展示 PDF 的是 SumatraPDF、Skim、Zathura 等阅读器。

那 texlab 到底做了什么？它扮演的是**协调者（orchestrator）**：把编辑器、TeX 引擎、PDF 阅读器三者串起来，并提供语言层面的智能（补全、诊断、跳转、符号、格式化等）。理解这一点，是读懂整本 wiki 的钥匙。

#### 4.1.2 核心流程

用一个最小场景说明 texlab 的价值：你在编辑器里打开 `main.tex`。

```text
1. 编辑器启动 texlab 进程，建立 LSP 连接。
2. 你敲入 \begin{it   → texlab 推断你要补全 \begin{itemize}，把候选返回给编辑器。
3. 你保存文件        → texlab 调用 TeX 引擎编译；编译报错时把诊断返回给编辑器。
4. 你想看 PDF        → texlab 调用 PDF 阅读器并定位到当前行（正向搜索）。
```

注意第 2 步的「智能」和第 3、4 步的「编译/预览协调」都由 texlab 统一负责，编辑器只需发出标准 LSP 请求即可。

#### 4.1.3 源码精读

先看 wiki 首页，它非常简短：

[Home.md:1-1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Home.md#L1) —— 整个 `Home.md` 只有一句欢迎语 `Welcome to the texlab wiki!`。

这句看似平淡，其实传达了一个重要事实：**这个仓库本身只是文档**。仓库里没有 `src/`、没有 `Cargo.toml`、没有可编译运行的代码——你读到的所有「能力描述」都散落在其余 6 个页面里。因此，学习 texlab 的用法，本质上是**读懂这套 wiki**。

再看配置页的开头，它点明了 texlab 与配置的关系：

[Configuration.md:1-2](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L1) —— 原文 `This page describes the configuration settings that the server will query from the LSP client.`（本页描述服务器会**从 LSP 客户端查询**的配置项）。

这句话非常关键：配置不是写死在 texlab 里的，而是由**编辑器（LSP 客户端）持有**，texlab 在需要时去**查询**。这正好印证了「texlab 是被动服务、协调者」的定位。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是帮你确认「本项目 = 纯文档 wiki」这一事实。

1. **实践目标**：确认仓库里没有可运行源码，理解我们要学的是 7 篇文档。
2. **操作步骤**：
   - 在仓库根目录列出所有文件，确认只有 7 个 `.md` 文件与 `.git` 目录。
   - 打开 `Home.md`，确认它只有一行欢迎语。
   - 用编辑器/工具统计每个 `.md` 的行数，找出「最厚」的那篇（通常是 `Configuration.md`）。
3. **需要观察的现象**：没有 `src/`、`Cargo.toml`、`package.json` 之类的工程文件；目录结构极其简单。
4. **预期结果**：7 个 Markdown 文件，其中 `Configuration.md` 篇幅最大（约 550 行，集中了绝大部分配置项）。
5. 若无法本地列目录，明确写「待本地验证」——但本仓库结构简单，按上表对照即可。

#### 4.1.5 小练习与答案

**练习 1**：下面三个说法，哪个准确描述了 texlab？
(a) texlab 是一个 LaTeX 编辑器。
(b) texlab 是一个把 `.tex` 编译为 PDF 的 TeX 引擎。
(c) texlab 是一个为编辑器提供 LaTeX/BibTeX 智能能力的语言服务器。

> **答案**：(c)。texlab 不是编辑器也不是引擎，而是协调两者并提供语言智能的语言服务器。

**练习 2**：为什么说「配置由 LSP 客户端持有」而不是「配置写死在 texlab 里」？请用 `Configuration.md` 第 1 行的话回答。

> **答案**：因为 `Configuration.md` 开头明确写「the server will query [the settings] from the LSP client」，即服务器去**查询**客户端持有的配置，配置的归属方是客户端（编辑器）。

### 4.2 LSP 客户端-服务器架构：四个角色如何协作

#### 4.2.1 概念说明

LSP 的核心思想是**解耦**：把「编辑器的界面逻辑」和「语言智能」分开。没有 LSP 之前，每款编辑器想支持 LaTeX 都得自己写一套补全/诊断；有了 LSP 之后，只要编辑器实现一个轻量「客户端」，就能复用同一个语言服务器 texlab。

在 LaTeX 工作流里，实际上有**四个**角色协同，而不只是「客户端 + 服务器」两个：

| 角色 | 身份 | 职责 |
| --- | --- | --- |
| 编辑器 | LSP 客户端 | 用户编辑文件、发 LSP 请求、**持有配置**、展示诊断/补全 |
| texlab | LSP 服务器 | 解析、补全、诊断、符号、格式化；**协调**编译与预览 |
| TeX 引擎 | 外部进程 | 真正编译 `.tex` → PDF（latexmk/pdflatex/tectonic 等） |
| PDF 阅读器 | 外部进程 | 显示 PDF；通过 SyncTeX 支持正/逆向跳转 |

**关键认知**：编辑器 ↔ texlab 之间走的是**标准 LSP（JSON-RPC）**；而 texlab ↔ TeX 引擎、texlab ↔ PDF 阅读器之间走的是**调用外部命令**（进程 + 命令行参数）。texlab 把这三种不同的「对话」缝合在一起。

#### 4.2.2 核心流程

把这四者画出来，数据流大致如下：

```text
        ① LSP 请求/响应/通知（标准协议）
 编辑器 ───────────────────────────────────────► texlab
 (客户端) ◄─────────────────────────────────────  (服务器)
                                                   │
                          ② 调用外部命令            │  ③ 调用外部命令
                          build.executable/args     │  forwardSearch.executable/args
                          ▼                         ▼
                     TeX 引擎                   PDF 阅读器
                  (latexmk/tectonic…)         (SumatraPDF/Skim/Zathura…)
```

- ① 是**标准化**的：texlab 既支持标准 LSP 请求，也扩展了一些自定义请求（见 4.3 和后续 `LSP-Internals` 讲义）。
- ② 是**可替换**的：默认 `latexmk`，也可以换成 `tectonic`（见 `Tectonic.md`）。
- ③ 是**按平台配置**的：不同操作系统常用不同阅读器（见 `Previewing.md`）。

一句话总结：**texlab 不生产 PDF，它只是 PDF 流水线的调度中心。**

#### 4.2.3 源码精读

我们用三处原文，分别佐证上面的三条边。

**边 ①（标准 LSP + 自定义扩展）**：`LSP-Internals.md` 开篇说明 texlab 在标准协议之上做了扩展，并且这些扩展是**可选**的、由客户端决定是否支持：

[LSP-Internals.md:1-6](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L1) —— 原文 `We extend the Language Server Protocol with custom messages ... These messages are optional and it is up to the client to support them.`（我们用自定义消息扩展 LSP……这些消息是可选的，是否支持由客户端决定）。

这段话同时回答了两个问题：texlab 遵循 LSP（所以能跨编辑器），又在不破坏协议的前提下加了 LaTeX 专属能力（所以比通用 LSP 更懂 LaTeX）。

**边 ②（调用 TeX 引擎）**：编译相关的配置集中在 `Configuration.md`，默认引擎是 `latexmk`：

[Configuration.md:5-12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L5) —— `texlab.build.executable` 的 Type 是 `string`，Default value 是 `latexmk`。

[Configuration.md:15-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L15) —— `texlab.build.args` 定义传给构建工具的参数，占位符 `%f` 会被服务器替换为待编译的 TeX 文件路径；默认值为 `["-pdf", "-interaction=nonstopmode", "-synctex=1", "%f"]`。

这说明编译是 texlab **以子进程方式调用**外部工具完成的，`%f` 这种占位符正是「调用外部命令」的典型特征。

**边 ③（调用 PDF 阅读器）**：正向搜索的配置也在 `Configuration.md`：

[Configuration.md:130-155](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L130) —— `texlab.forwardSearch.executable` 定义 PDF 预览器可执行文件（需支持 SyncTeX）；`texlab.forwardSearch.args` 定义传给预览器的参数，占位符 `%f`(TeX 文件路径)、`%p`(PDF 路径)、`%l`(行号) 由服务器替换。

同样是用「可执行文件 + 参数 + 占位符」调用外部阅读器，与边 ② 的模式完全一致——这印证了 texlab 对引擎和阅读器都是「协调者」角色。

#### 4.2.4 代码实践

这是一个**调用链追踪型实践**，目标是亲手把「四角色 + 三条边」对号入座。

1. **实践目标**：用 wiki 原文为每条「边」找到证据配置项。
2. **操作步骤**：
   - 在 `Configuration.md` 中定位 `texlab.build.executable`、`texlab.build.args`（对应边 ②）。
   - 在 `Configuration.md` 中定位 `texlab.forwardSearch.executable`、`texlab.forwardSearch.args`（对应边 ③）。
   - 在 `LSP-Internals.md` 中找到任意一个自定义请求（如 `textDocument/build`，对应边 ① 的扩展部分）。
3. **需要观察的现象**：边 ② 和边 ③ 都用「executable + args + 占位符」的同一套模式；而边 ① 用的是 `method: '...'` + TypeScript 接口描述的请求/响应结构。
4. **预期结果**：你能在 wiki 里分别指出三类交互各自由哪个小节描述，并意识到「配置驱动外部命令、协议驱动编辑器通信」。
5. 若暂时没有装 texlab，无需运行任何命令，纯阅读即可完成本实践。

#### 4.2.5 小练习与答案

**练习 1**：编辑器、texlab、TeX 引擎、PDF 阅读器这四者中，哪两对之间**不**走标准 LSP？

> **答案**：texlab ↔ TeX 引擎、texlab ↔ PDF 阅读器 这两对不走 LSP，而是通过「调用外部命令（executable + args）」交互。只有 编辑器 ↔ texlab 走标准 LSP。

**练习 2**：`texlab.build.args` 里出现的 `%f`，和 `texlab.forwardSearch.args` 里的 `%f` 含义是否相同？

> **答案**：含义相同——都表示「TeX 文件路径」，且都由服务器（texlab）在调用外部命令前替换。`forwardSearch.args` 额外还有 `%p`(PDF 路径) 和 `%l`(行号) 两个占位符。

**练习 3**：如果要把默认的 `latexmk` 换成别的 TeX 引擎，应该改哪个配置项？依据来自哪篇 wiki？

> **答案**：改 `texlab.build.executable`（必要时同时改 `texlab.build.args`）。依据来自 `Configuration.md`；具体替换为 tectonic 的写法见 `Tectonic.md`。

### 4.3 Wiki 导览：7 个页面与建议阅读顺序

#### 4.3.1 概念说明

整本 wiki 共 7 个页面，本节给每一页一句话定位，并标出它对应 texlab 的哪一项能力。后续讲义会逐页深入，这里你只需要建立「目录索引」式的印象。

#### 4.3.2 核心流程

下表按**建议阅读顺序**排列（与本系列讲义的单元顺序基本一致）：

| 顺序 | 页面 | 一句话主题 | 对应 texlab 能力 |
| --- | --- | --- | --- |
| 1 | `Home.md` | wiki 欢迎页 / 入口 | —（项目门面） |
| 2 | `Project-Detection.md` | 如何识别「项目」、如何确定根目录 | 项目模型 / 依赖树 / 根目录检测 |
| 3 | `Configuration.md` | 全部 `texlab.*` 配置项总表 | 配置模型（贯穿所有子系统） |
| 4 | `Previewing.md` | 编译、预览、SyncTeX、各阅读器集成 | 编译协调 + 正/逆向搜索 |
| 5 | `Tectonic.md` | 用 tectonic 替代默认 TeX 引擎 | 可替换的编译后端 |
| 6 | `LSP-Internals.md` | 自定义 LSP 消息 + 枚举映射 | 对 LSP 的扩展 |
| 7 | `Workspace-commands.md` | `workspace/executeCommand` 暴露的命令 | 工作区命令（清理、依赖图等） |

为什么是这个顺序？

- 先读 `Project-Detection`：因为 texlab 的几乎所有功能都建立在「先搞清项目里有谁、根目录在哪」之上。
- 再读 `Configuration`：因为后续每篇讲义都会用到 `texlab.*` 配置项这套公共语言。
- 然后 `Previewing` / `Tectonic`：对应「编译 → 预览」这条最常用的主链路。
- 最后 `LSP-Internals` / `Workspace-commands`：进入协议层与命令层，偏进阶。

#### 4.3.3 源码精读

下面给出每个页面**开头几句**的精读，作为你快速进入该页的锚点。

**Project-Detection.md** —— 点明「项目」概念及其重要性：

[Project-Detection.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L1) —— 原文说明：每当你打开一个 TeX 文件，texlab 会找出所有编译进**同一文档**的文件（即「项目」）；服务器需要这些信息来实现大部分功能，例如 preamble 里导入的宏包应在其他项目文件中可见；项目还用于确定交给 TeX 引擎的**根文档**。

[Project-Detection.md:14-23](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L14) —— 给出确定**根目录**的四步优先级（`.texlabroot` → `Tectonic.toml` → `.latexmkrc` → 根源文件）。本讲只需知道「根目录检测存在一套优先级算法」即可，细节留到 u1-l2。

**Previewing.md** —— 点明编译触发方式与预览策略：

[Previewing.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L1) —— 原文说明 texlab 支持两种编译触发：自定义请求 `textDocument/build`，以及（配置后）保存即编译 `texlab.build.onSave`。

[Previewing.md:11-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L11) —— 关键建议：若要让 PDF 与编辑器光标保持同步，应启用 SyncTeX + `texlab.build.forwardSearchAfter`，并**不要**用 latexmk 的 `-pvc`（因为 texlab 收不到 latexmk 的编译完成通知），改用 `texlab.build.onSave`。

**Tectonic.md** —— 点明 tectonic 是可替换的现代引擎，并给出两条关键 hint：

[Tectonic.md:1-4](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L1) —— 原文：tectonic 是现代化、可替代的 TeX 引擎；用 tectonic 时 texlab 大部分功能开箱即用，但需要改配置。

[Tectonic.md:8-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L8) —— hint：务必把 `texlab.build.pdfDirectory`/`auxDirectory` 与 `--outdir` 对齐；建议加 `--keep-intermediates`（让 texlab 能读到章节编号显示在补全里）和 `--keep-logs`（否则 texlab 无法上报编译告警）。

**LSP-Internals.md** —— 除了 4.2.3 引用过的开篇，还有两块内容：自定义的 Build / Forward Search 请求（[LSP-Internals.md:7-10](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L7)、[LSP-Internals.md:66-69](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L66)），以及把 LaTeX/BibTeX 结构映射到 LSP 枚举的对照表（[LSP-Internals.md:111-116](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L111)）。本讲只要知道「这两块都属于对 LSP 的扩展」即可，细节留到第 4 单元。

**Workspace-commands.md** —— 点明命令约定：

[Workspace-commands.md:1-4](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L1) —— 原文：服务器通过 `workspace/executeCommand` 请求提供若干命令；为简化起见，**所有命令至多接收一个参数**（`arguments` 数组恰好含 0 或 1 个元素）。具体六条命令（cleanAuxiliary、cleanArtifacts、changeEnvironment、findEnvironments、showDependencyGraph、cancelBuild）留到 u4-l2 详解。

#### 4.3.4 代码实践

这是一个**文档索引型实践**，目标是让你离开本讲也能快速定位 wiki。

1. **实践目标**：为每个 wiki 页面写一句话「能力标注」。
2. **操作步骤**：
   - 依次打开 7 个 `.md` 文件，只读每篇的**开头 1–3 句**。
   - 用一句话概括该页对应 texlab 的哪一项能力（可参考 4.3.2 的表格，但请用自己的话写）。
3. **需要观察的现象**：你会发现 `Configuration.md` 是唯一一篇「横跨多个子系统」的页面（构建、诊断、符号、格式化、扩展点都在里面），其余 6 篇主题相对聚焦。
4. **预期结果**：得到一张属于自己的「7 页速查表」，每页一句，便于后续跳读。
5. 无需运行任何命令。

#### 4.3.5 小练习与答案

**练习 1**：如果你想了解「texlab 怎么知道 main.tex 和 chapter1.tex 属于同一个文档」，应该读哪一页？

> **答案**：`Project-Detection.md`。它讲的就是如何通过 `\input`/`\import` 等命令构建依赖树、识别项目。

**练习 2**：`LSP-Internals.md` 里除了「自定义消息」，还有哪一块内容？

> **答案**：还有「枚举映射（Enum Mapping）」——把 LaTeX/BibTeX 的结构（命令、环境、节、方程、标签、BibTeX 条目等）映射到 LSP 的 `CompletionItemKind` 和 `SymbolKind`。

**练习 3**：为什么建议先读 `Project-Detection` 再读 `Previewing`？

> **答案**：因为预览/编译都需要先把散落的 `.tex` 组织成「项目」并确定「根文档」交给 TeX 引擎；项目识别是编译协调的前置条件，所以先读前者。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性任务。

**任务**：画一张「四角色协作关系图」，并为每个 wiki 页面标注它对应 texlab 的哪项能力。

1. **画关系图**：在纸上或任意画图工具里画出下面四个节点，并连出三条边，边上标注「协议/机制」：
   - 编辑器（LSP 客户端）
   - texlab（LSP 服务器）
   - TeX 引擎（latexmk / tectonic …）
   - PDF 阅读器（SumatraPDF / Skim / Zathura …）

   参考答案（自我检查用）：
   - 编辑器 ↔ texlab：**标准 LSP（JSON-RPC）+ texlab 自定义扩展**。
   - texlab → TeX 引擎：**调用外部命令**，由 `texlab.build.executable` / `texlab.build.args` 驱动，`%f` 占位符。
   - texlab → PDF 阅读器：**调用外部命令**，由 `texlab.forwardSearch.executable` / `texlab.forwardSearch.args` 驱动，`%f` / `%p` / `%l` 占位符。
2. **标注能力**：把 7 个 wiki 页面名贴到图上**最相关**的节点或边旁边，并各写一句话。例如：
   - `Configuration.md` → 贴在 texlab 节点上，批注「配置由编辑器持有、texlab 查询」。
   - `Previewing.md` → 贴在「texlab → PDF 阅读器」这条边附近，批注「编译协调 + SyncTeX 正/逆向搜索」。
   - `Tectonic.md` → 贴在 TeX 引擎节点上，批注「可替换为 tectonic 后端」。
   - 其余四页自行标注。
3. **自我验证**：用一句话回答「texlab 自己编译 PDF 吗？」——如果你的图里 texlab 没有直接连到 PDF 输出，而是经由 TeX 引擎，那就对了。
4. **预期结果**：一张信息密度高、能向别人讲清「texlab 在整条 LaTeX 流水线里处于什么位置」的关系图，外加 7 条页面能力标注。
5. 本实践为纯阅读/画图任务，不涉及运行；如果你之后真的装了 texlab，可以再回到这张图，把每条边对应到一次真实操作。

## 6. 本讲小结

- **texlab 是语言服务器**，不是编辑器、不是 TeX 引擎、也不是 PDF 阅读器；它是把三者缝合起来的**协调者**。
- LaTeX 工作流里有**四个角色**：编辑器（LSP 客户端）、texlab（LSP 服务器）、TeX 引擎、PDF 阅读器。
- 只有**编辑器 ↔ texlab** 走标准 LSP；texlab 与 TeX 引擎、PDF 阅读器之间是**调用外部命令**（`executable` + `args` + `%f`/`%p`/`%l` 占位符）。
- **配置由 LSP 客户端持有**，texlab 在需要时查询（见 `Configuration.md` 开头）。
- 本仓库是**纯文档 wiki**，共 7 个页面；`Configuration.md` 最厚、横跨多个子系统，是后续多讲的基础。
- 建议阅读顺序：Home → Project-Detection → Configuration → Previewing → Tectonic → LSP-Internals → Workspace-commands。

## 7. 下一步学习建议

- 下一讲 **u1-l2「项目识别与根目录检测」** 会深入 `Project-Detection.md`，讲清 texlab 如何从单个 `.tex` 发现整个项目、以及确定根目录的四步优先级算法。这是理解 texlab 几乎所有功能的前提，强烈建议紧接着学。
- 在进入第 2 单元的配置细节之前，建议你先按本讲「综合实践」把关系图画一遍，建立空间感。
- 如果你急于上手，可以先跳到 `Previewing.md` 看编译预览配置；但请注意，没有「项目识别」和「配置模型」打底，部分行为会难以解释，所以仍推荐按顺序学习。
