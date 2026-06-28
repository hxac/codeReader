# 构建配置 texlab.build.*

## 1. 本讲目标

[u2-l1](u2-l1-config-overview.md) 教会了我们「配置归客户端、用三要素读懂任意一项、`%f`/`%p`/`%l` 怎么替换」这套通用语言。本讲把这套语言**第一次落到一个完整的子系统**——`texlab.build.*`，也就是「texlab 到底怎么把一个 `.tex` 文件编译成 PDF」。

读完本讲，你应该能够：

1. 用 `texlab.build.executable` / `texlab.build.args` **切换构建工具**（latexmk / tectonic / 裸 pdflatex 等），并能正确写出 `args`（flag 与参数必须拆成数组独立元素）。
2. 说清 `onSave`、`forwardSearchAfter`、`useFileList` 三个布尔开关**各自控制什么**，以及它们的性能与体验权衡。
3. 配置 `auxDirectory` / `logDirectory` / `pdfDirectory` / `filename` 这些「构建产物定位」项，并理解一个关键区分：**用 latexmk 时 texlab 会自动推断产物目录，非 latexmk 工具则必须你手动两头对齐**（既要在 `args` 里让引擎把产物放对地方，又要在这些设置项里告诉 texlab 去哪找）。
4. 认出已弃用的 `texlab.auxDirectory`，知道该用哪几项替代它。

`texlab.build.*` 是 Configuration.md 里**最常用**的一组配置，也是后续 [u3 编译与预览](u3-l1-build-and-preview-workflow.md) 整条工作流的地基。

## 2. 前置知识

本讲承接两篇讲义：

- **[u2-l1 配置总览](u2-l1-config-overview.md)**：你必须已经掌握「三要素（Type / Default value / Placeholders）」读法、占位符 `%f`/`%p`/`%l` 的含义，以及「flag 与参数要拆成数组独立元素」这条拆分规则。本讲会直接复用，不再从零解释。
- **[u1-l2 项目识别与根目录检测](u1-l2-project-detection.md)**：你必须知道「根目录」是怎么定的（`.texlabroot` → `Tectonic.toml` → `.latexmkrc` → 根源文件四步优先级）。本讲的「产物目录」与根目录**强耦合**：默认产物目录就是「根目录」本身。

本讲还要用到几个通俗概念：

- **构建工具（build tool）**：真正执行编译的程序。最常见的是 `latexmk`（一个自动决定要跑几遍、自动调用 bibtex/biber 的「构建管家」），也可以是 `tectonic`（自带包管理的一体化引擎）或最底层的 `pdflatex` / `xelatex` / `lualatex`。texlab 本身**不是**构建工具，它只是按你给的 `executable` + `args` 去**调用**构建工具。
- **构建产物（build artifacts）**：编译一次会生成一堆文件，除了最终的 `.pdf`，还有辅助文件 `.aux`/`.fls`/`.fdb_latexmk`、日志 `.log`/`.blg` 等。本讲的几个 `*Directory` 项就是告诉 texlab「这些产物分别放在哪个目录」。
- **`latexmkrc`**：`latexmk` 的配置文件（项目根目录下的 `.latexmkrc` 或 `latexmkrc`）。可以在里面用 Perl 语法写 `$out_dir = 'build';` 之类，让 latexmk 把产物输出到指定目录。

> 提示：本仓库是 texlab 的**官方 wiki**（纯文档）。本讲的「源码」主要是 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)，并会交叉引用 [Project-Detection.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md) 与 [Previewing.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md)。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `Configuration.md` | 列出 `texlab.build.*` 全部 9 个配置项（含 1 个已弃用项） | 几乎所有源码精读取自这里（build.executable / args / onSave / forwardSearchAfter / useFileList / auxDirectory / logDirectory / pdfDirectory / filename / 已弃用 auxDirectory） |
| `Project-Detection.md` | 根目录检测四步法 | 说明 `.latexmkrc` / `Tectonic.toml` 如何同时决定**根目录**与**产物目录**，这是理解「自动推断」的关键 |
| `Previewing.md` | 编译与预览总览 | 说明 `onSave` 与 `forwardSearchAfter` 在端到端工作流里的位置，以及「为何不用 latexmk `-pvc`」 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `build.executable` / `build.args`**——调用哪个构建工具、传什么参数。
- **4.2 构建触发：`onSave` / `forwardSearchAfter`**——什么时候编译、编译完要不要顺手跳一次正向搜索。
- **4.3 输出目录与产物定位：`auxDirectory`/`logDirectory`/`pdfDirectory`/`filename`/`useFileList`**——产物放哪、texlab 去哪找，以及 latexmk 的自动推断与已弃用项。

### 4.1 `build.executable` / `build.args`

#### 4.1.1 概念说明

texlab 自己不会编译 LaTeX。编译是这样一个过程：

```
texlab 拿到 build.executable + build.args
        │  （把 args 里的 %f 替换成当前 TeX 文件路径）
        ▼
   以子进程方式调用： <executable> <args...>
        │
        ▼
   构建工具（latexmk / tectonic / pdflatex ...）真正读 .tex、生成 .pdf 与各种产物
```

所以 `build.executable` 回答「**谁**来编译」，`build.args` 回答「**怎么**编译」（传哪些命令行参数）。这两项配套使用：换一个 `executable`，通常 `args` 也得跟着换。

#### 4.1.2 核心流程

给定默认配置，一次编译实际执行的命令大致是：

```text
executable = "latexmk"
args       = ["-pdf", "-interaction=nonstopmode", "-synctex=1", "%f"]

# 假设当前文件是 /work/paper/main.tex，texlab 把 %f 替换后实际执行：
latexmk -pdf -interaction=nonstopmode -synctex=1 /work/paper/main.tex
```

三个默认 flag 各自的作用（这些是 latexmk / TeX 的通用知识，不是 texlab 发明的）：

| flag | 作用 |
| --- | --- |
| `-pdf` | 让 latexmk 用 pdflatex 产出 PDF（而非 DVI/PS） |
| `-interaction=nonstopmode` | 出错时不要停下来等用户输入，适合由程序驱动 |
| `-synctex=1` | 让 TeX 引擎写出 `.synctex.gz`，供 SyncTeX 双向跳转使用 |

切换构建工具时，`executable` 与 `args` 要一起改。例如换成 `tectonic`（详见 [u3-l3](u3-l3-tectonic-engine.md)），`args` 就不再是 latexmk 的 flag，而是 tectonic 的 CLI。换回裸 `pdflatex`，则要写成 `["-synctex=1", "-interaction=nonstopmode", "%f"]` 之类。

> 关键提醒（复用 u2-l1 的拆分规则）：`args` 是字符串**数组**，每个元素是命令行的一段。texlab **不会**替你按空格切分，所以 `-outdir build` 必须写成两个元素 `["-outdir", "build"]`，而不能写成 `"-outdir build"`。

#### 4.1.3 源码精读

- [Configuration.md:5-12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L5-L12)：定义 `texlab.build.executable`——「LaTeX 构建工具的可执行文件」，`Type: string`，`Default value: latexmk`。
  - 结论：默认用 latexmk。想换引擎，把这一项设成对应可执行名即可（如 `tectonic`、`pdflatex`）。

- [Configuration.md:15-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L15-L30)：定义 `texlab.build.args`——传给构建工具的额外参数。
  - [Configuration.md:18-22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L18-L22)：明确两件事——① flag 与它的参数要拆成数组**独立元素**（`-foo bar` → `["-foo", "bar"]`）；② 占位符 `%f` 由服务器替换。
  - [Configuration.md:24-26](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L24-L26)：占位符清单，`%f` = 要编译的 TeX 文件路径（这是 `build.args` **唯一**的占位符）。
  - [Configuration.md:30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L30)：默认值 `["-pdf", "-interaction=nonstopmode", "-synctex=1", "%f"]`——默认就含 `%f`，且每个 flag 各占一个元素（符合拆分规则）。这就是「零配置即可编译」的原因。

> 注意一个细节：wiki 原文里举例用的是 `latex.build.args`（少了个 `t`）来说明拆分规则，那是文档里的笔误示例；真实的配置键是 `texlab.build.args`。不要被这个笔误带偏。

#### 4.1.4 代码实践

**实践目标**：亲手切换一次构建工具，验证 `executable` 与 `args` 必须配套，以及 `%f` 被替换。

**操作步骤**：

1. 准备一个最小工程：`main.tex` 含 `\documentclass{article}` + `\begin{document}Hello\end{document}`。
2. 在编辑器配置里**保持默认**（即不写 `build.*`），保存触发一次编译，确认用 `latexmk` 跑通、生成 `main.pdf`。
3. 把 `texlab.build.executable` 改成 `pdflatex`，并把 `texlab.build.args` 改成（示例配置）：
   ```jsonc
   // 示例配置：改用裸 pdflatex
   "texlab.build.executable": "pdflatex",
   "texlab.build.args": [
     "-synctex=1",
     "-interaction=nonstopmode",
     "%f"
   ]
   ```
4. 再次保存触发编译。

**需要观察的现象**：

- 第 2 步：texlab 调用的是 `latexmk`（注意 latexmk 会自动跑多遍、自动处理引用）。
- 第 4 步：texlab 调用的是 `pdflatex`（只跑一遍，交叉引用可能需要手动多编译几次才能正确）。

**预期结果**：你直观感受到「换 `executable` 就要换 `args`」。如果第 4 步你忘了改 `args`、仍带着 latexmk 的 `-pdf` flag 传给 pdflatex，pdflatex 会因为不认识 `-pdf` 而报错——这印证了二者必须配套。

> 具体编译是否成功取决于本机是否装了对应引擎，**待本地验证**。若没有 LaTeX 发行版，可改为「源码阅读型实践」：对照 [Configuration.md:30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L30) 的默认 `args`，逐个 flag 查阅 latexmk 手册，说明每个 flag 的作用。

#### 4.1.5 小练习与答案

**练习 1**：默认配置下，texlab 实际执行的编译命令是什么？为什么零配置就能编译？

> **参考答案**：执行 `latexmk -pdf -interaction=nonstopmode -synctex=1 <当前文件>`。因为 `build.executable` 默认 `latexmk`、`build.args` 默认已含 `%f`（见 [Configuration.md:30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L30)），无需任何配置即可工作。

**练习 2**：要让 latexmk 把产物输出到 `build` 子目录，`build.args` 里该怎么加？为什么不能写 `"-outdir=build"` 一个元素？（注：写成 `-outdir=build` 单元素 latexmk 其实能识别，但请按 texlab 的「拆分」规则思考另一种等价写法。）

> **参考答案**：等价写法是 `["-outdir", "build"]` 两个元素。依据 [Configuration.md:18-22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L18-L22)，texlab 不替你按空格切分，flag 与参数必须是数组独立元素。（`-outdir=build` 这种「等号连写」是单个 token，latexmk 能解析；但涉及「flag + 空格 + 参数」的形态必须拆分。）

---

### 4.2 构建触发：`onSave` / `forwardSearchAfter`

#### 4.2.1 概念说明

`executable`/`args` 定义了「**怎么**编译」，但还有两个问题没回答：「**什么时候**编译」和「编译完要不要顺手跳一次 PDF」。这正是 `onSave` 与 `forwardSearchAfter` 两个布尔开关。

注意这两项默认都是 `false`——也就是说，**默认情况下 texlab 不会因为你保存文件就自动编译**。你需要显式打开 `onSave` 才有「保存即编译」的体验。

#### 4.2.2 核心流程

编译有两条触发路径（详见 [Previewing.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L1-L3)）：

```text
路径 A（手动/自定义请求）：
  编辑器 → 发 textDocument/build 自定义请求 → texlab 调用 executable+args 编译

路径 B（自动/onSave）：
  用户保存文件 → （若 onSave=true）texlab 自动调用 executable+args 编译
```

当 `forwardSearchAfter=true` 时，路径 B 会再接一步：

```text
保存 → 编译 → （若 forwardSearchAfter=true）自动执行一次正向搜索（跳到 PDF 对应行）
```

这样就串起了「**保存→编译→跳转**」的闭环。注意第二步跳转依赖 `forwardSearch.*` 配置（阅读器 executable + args + `%p`/`%l`），那部分在 [u3-l2](u3-l2-synctex-viewers.md) 讲。

> **为什么不直接用 latexmk 的 `-pvc`（持续监视）？** Previewing.md 明确给出了原因：texlab **不会被 latexmk 通知**文档已编译完成（[Previewing.md:13-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L13-L15)）。用 `-pvc`，latexmk 自己在那边一遍遍编译，但 texlab 不知情，于是 `forwardSearchAfter` 这类「编译后动作」无法可靠触发。所以官方推荐用 `texlab.build.onSave` 让**编译本身**由 texlab 主导。

一句话决策表：

| 想要的体验 | 配置 |
| --- | --- |
| 手动按键才编译 | 什么都不开（`onSave=false`，由编辑器发 `textDocument/build`） |
| 保存即编译 | `build.onSave = true` |
| 保存即编译 + 自动跳 PDF | `build.onSave = true` **且** `build.forwardSearchAfter = true`（外加 `forwardSearch.*` 配好阅读器） |

#### 4.2.3 源码精读

- [Configuration.md:44-50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L44-L50)：定义 `texlab.build.onSave`——设为 `true` 则「保存文件后编译项目」，`Type: boolean`，`Default value: false`。
  - 这是「保存即编译」的总开关，默认关。

- [Configuration.md:34-40](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L34-L40)：定义 `texlab.build.forwardSearchAfter`——设为 `true` 则「编译后执行一次正向搜索」，`Type: boolean`，`Default value: false`。
  - 它必须与 `forwardSearch.*`（阅读器配置）配合，单独打开没有意义。

- [Previewing.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L1-L3)：确认编译的两条触发路径——自定义请求 `textDocument/build`，以及「保存后编译」（设 `texlab.build.onSave` 为 true）。
- [Previewing.md:9-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L9-L15)：说明 `forwardSearchAfter` 需要 SyncTeX 与 `forwardSearch` 配置配合，并**明确不建议**用 latexmk `-pvc`，原因如上。

#### 4.2.4 代码实践

**实践目标**：亲手体会 `onSave` 默认是关的，打开后才有「保存即编译」。

**操作步骤**：

1. 用一个能编译的工程，**先不配置** `build.*`（即 `onSave` 为默认 `false`）。
2. 修改 `main.tex` 内容并保存。观察是否自动编译。
3. 打开开关（示例配置）：
   ```jsonc
   // 示例配置：开启保存即编译
   "texlab.build.onSave": true
   ```
4. 再次修改并保存。

**需要观察的现象**：

- 第 2 步：保存后**没有**自动编译（除非你的编辑器扩展自己发了 `textDocument/build`）。
- 第 4 步：保存后**立即**触发一次编译，过几秒能看到 PDF 更新（或编译错误诊断）。

**预期结果**：你确认 `onSave` 是「保存即编译」的必要开关，且默认关闭。这与 [Configuration.md:50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L50) 的 `Default value: false` 一致。

> 编译是否真的发生、耗时多少，**待本地验证**（取决于工程大小与本机性能）。若没有可运行环境，可改为阅读 [Previewing.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L1-L3)，复述两条触发路径的区别。

#### 4.2.5 小练习与答案

**练习 1**：用户配了 `build.forwardSearchAfter = true` 但没配 `forwardSearch.executable`，会发生什么？

> **参考答案**：编译仍会正常进行，但「编译后正向搜索」这一步无法真正跳转——因为 `forwardSearch.executable` 默认 `null`（未配置阅读器）。`forwardSearchAfter` 只是把「编译后调一次正向搜索」的**意图**打开，真正执行还需要 `forwardSearch.*` 配套（见 [Configuration.md:34-40](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L34-L40) 与 u2-l1 对 `forwardSearch.args` 默认 `null` 的说明）。

**练习 2**：为什么官方不推荐用 latexmk 的 `-pvc` 来做「保存即编译 + 跳转」？

> **参考答案**：因为 `-pvc` 让 latexmk 自己持续编译，而 texlab **不会被 latexmk 通知**编译完成（[Previewing.md:13-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L13-L15)），导致 `forwardSearchAfter` 等「编译后动作」无法可靠触发。改用 `texlab.build.onSave` 让编译由 texlab 主导即可。

---

### 4.3 输出目录与产物定位：`auxDirectory`/`logDirectory`/`pdfDirectory`/`filename`/`useFileList`

#### 4.3.1 概念说明

编译会撒出一堆产物文件。texlab 需要知道它们分别在哪，原因有二：

1. **正向搜索要打开正确的 PDF**——texlab 得找到 `.pdf` 才能让阅读器跳过去（`pdfDirectory` + `filename`）。
2. **诊断与项目识别要读日志和辅助文件**——比如读 `.log` 上报编译告警、读 `.aux`/`.fls` 用于项目检测。

这里有本讲**最重要的一条区分**，务必记住：

> **用 latexmk（尤其是配了 `.latexmkrc`）时，texlab 会自动推断产物目录；改用非 latexmk 工具时，texlab 不再自动推断，你必须手动两头对齐。**

「两头对齐」的意思是：产物去哪儿，得在**两个地方**都告诉清楚——

- 在 `build.args` 里让**引擎**真的把产物输出到该目录；
- 在 `auxDirectory`/`logDirectory`/`pdfDirectory` 里让 **texlab** 知道去该目录找。

只改一头都会出问题：只改 `args`，texlab 找不到 PDF（正向搜索失败）；只改设置项，引擎还是把产物丢在默认目录（设置与实际不符）。

#### 4.3.2 核心流程

先看三个目录项的关系（三者结构完全一致，只是管不同产物）：

| 配置项 | 管的产物 | Type | 默认值 |
| --- | --- | --- | --- |
| `texlab.build.auxDirectory` | `.aux` 等辅助文件 | `string` | `.`（根目录） |
| `texlab.build.logDirectory` | `.log` 等日志文件 | `string` | `.`（根目录） |
| `texlab.build.pdfDirectory` | `.pdf` 等输出文件 | `string` | `.`（根目录） |

注意「根目录」一词——它指向的是 [u1-l2](u1-l2-project-detection.md) 讲的**根目录**（由 `.texlabroot`/`Tectonic.toml`/`.latexmkrc`/根源文件决定），**不是**「当前打开文件所在目录」。这三项的默认值 `.` 都是以根目录为基准的。

「自动推断」只在 latexmk 场景生效，且与根目录检测**耦合**在一起。看 Project-Detection 的四步法（[Project-Detection.md:17-23](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L17-L23)）：

```text
确定根目录时（向上探测，命中即停）：
  1. .texlabroot     → 产物目录直接取 build.auxDirectory/logDirectory/pdfDirectory 这三个设置
  2. Tectonic.toml   → 产物目录取 Tectonic 的 src/build 目录
  3. .latexmkrc      → 产物目录从 latexmkrc 里的设置自动推断；推断失败才回退到上面三个设置
  4. 根源文件        → （无特殊产物目录逻辑，用默认 .）
```

所以 `.latexmkrc` 同时干了两件事：**既标志根目录，又让 texlab 自动读出产物目录**。这就是为什么「用 latexmk + latexmkrc」时你通常不用手动配三个 `*Directory`。

`filename` 与 `useFileList` 是这一组的另外两项，职责不同：

- `texlab.build.filename`：覆盖「编译产物 PDF 的文件名」。默认 `null` 时，texlab 按约定找——主文档叫 `foo.tex` 就找 `foo.pdf`。当你的输出文件名与主文档名不一致（例如多主文件、或引擎改了输出名），才需要显式设它，好让正向搜索找对 PDF。
- `texlab.build.useFileList`：设为 `true` 时，texlab 把引擎生成的 `.fls` 文件作为**项目检测的额外输入**。`.fls`（由 TeX 引擎在带 `-recorder` 时生成）记录了本次编译实际读写了哪些文件，能帮 texlab 发现「靠解析 `\input` 没找到」的依赖。代价是**可能影响性能**（每份 `.fls` 都要解析），所以默认 `false`。

#### 4.3.3 源码精读

三个目录项结构一致，以 `auxDirectory` 为例，另两个对照即可：

- [Configuration.md:66-75](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L66-L75)：定义 `texlab.build.auxDirectory`——「**非 latexmk 时**」定义 `.aux` 文件所在目录。
  - [Configuration.md:68-69](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L68-L69)：两个关键限定——① 「When not using `latexmk`」（仅在非 latexmk 时生效）；② 「you need to set the aux directory in `latex.build.args` too」（你还必须在 `args` 里设对输出目录，即「两头对齐」）。
  - [Configuration.md:71](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L71)：「When using a `latexmkrc` file, `texlab` will automatically infer the correct setting」（用 latexmkrc 时 texlab 自动推断）。
  - [Configuration.md:75](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L75)：默认 `.`（与根目录同目录）。

- [Configuration.md:79-88](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L79-L88)：`texlab.build.logDirectory`，结构与上面完全相同（[L84](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L84) 同样声明 latexmkrc 自动推断）。
- [Configuration.md:92-101](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L92-L101)：`texlab.build.pdfDirectory`，结构相同（[L97](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L97) 同样声明 latexmkrc 自动推断）。

`filename` 与 `useFileList`：

- [Configuration.md:105-112](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L105-L112)：`texlab.build.filename`——覆盖构建产物文件名，用于正向搜索时找对 PDF。`Default value: null`，默认按 `foo.tex → foo.pdf` 约定查找（[L112](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L112)）。
- [Configuration.md:54-62](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L54-L62)：`texlab.build.useFileList`——设 `true` 则用引擎生成的 `.fls` 作为项目检测的额外输入；[L58](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L58) 明确「may have an impact on performance」，故默认 `false`。

latexmkrc 自动推断与根目录检测的耦合（交叉引用）：

- [Project-Detection.md:19-20](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L19-L20)：命中 `.texlabroot` 时，产物目录取这三个 `*Directory` 设置。
- [Project-Detection.md:22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L22)：命中 `.latexmkrc` 时，从 latexmkrc 设置自动推断产物目录；**推断失败才回退**到三个 `*Directory` 设置。

已弃用项（避免使用）：

- [Configuration.md:117-126](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L117-L126)：`(DEPRECATED) texlab.auxDirectory`——旧的顶层「构建产物目录」，已被 `texlab.build.auxDirectory`（及其拆分出的 `logDirectory`/`pdfDirectory`）取代。新配置应一律用 `texlab.build.*` 版本。

> 一句话对比：旧 `texlab.auxDirectory` 只有一个笼统的「产物目录」；新设计把它**拆成三个**（aux / log / pdf），因为这三类产物完全可以放在不同目录（例如 PDF 进 `build/`、aux 进 `build/aux/`）。

#### 4.3.4 代码实践

**实践目标**：体会「两头对齐」——只改 `args` 或只改设置项都会出错。

**操作步骤**：

1. 用非 latexmk 工具（例如 `pdflatex`）编译一个工程，目标把 PDF 输出到 `./build`。
2. **故意只改一边**（错误示范，示例配置）：
   ```jsonc
   // 错误示范：只在 args 里指定输出目录，没告诉 texlab
   "texlab.build.executable": "pdflatex",
   "texlab.build.args": [
     "-synctex=1",
     "-interaction=nonstopmode",
     "-output-directory", "build",
     "%f"
   ]
   // 缺少：texlab.build.pdfDirectory 等
   ```
3. 编译后尝试正向搜索（跳到 PDF），观察是否失败。
4. **改成两头对齐**（正确示范，示例配置）：
   ```jsonc
   // 正确示范：args 与 *Directory 都指向 build
   "texlab.build.executable": "pdflatex",
   "texlab.build.args": [
     "-synctex=1",
     "-interaction=nonstopmode",
     "-output-directory", "build",
     "%f"
   ],
   "texlab.build.auxDirectory": "build",
   "texlab.build.logDirectory": "build",
   "texlab.build.pdfDirectory": "build"
   ```

**需要观察的现象**：

- 第 3 步：PDF 实际生成在 `build/main.pdf`，但 texlab 默认去根目录找 `main.pdf`，正向搜索可能找不到（**待本地验证**具体表现，可能取决于 texlab 版本是否有兜底）。
- 第 4 步：texlab 在 `build/` 下找到产物，正向搜索恢复正常。

**预期结果**：你直观验证了「非 latexmk 工具必须两头对齐」。这与 [Configuration.md:68-69](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L68-L69) 的「you need to set the aux directory in `latex.build.args` too」完全对应。

> 提示：如果你用的是 latexmk，则不必手动配这三个目录——把 `$out_dir = 'build';` 写进 `.latexmkrc`，texlab 会自动推断（[Configuration.md:71](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L71)、[Project-Detection.md:22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L22)）。

#### 4.3.5 小练习与答案

**练习 1**：`texlab.build.auxDirectory` 项里写「When not using `latexmk`」和「When using a `latexmkrc` file, texlab will automatically infer」——这两句话是否矛盾？

> **参考答案**：不矛盾。它们描述两种互斥场景：① **非 latexmk** 工具时，这项才生效（且要和 `args` 两头对齐）；② 用 **latexmkrc** 时，texlab 自动推断、你不必手填。换言之，这项主要服务于「非 latexmk」场景；latexmk 场景下它通常被自动推断覆盖（见 [Configuration.md:68-71](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L68-L71)）。

**练习 2**：默认 `pdfDirectory` 是 `.`（根目录）。如果你的主文档在 `src/main.tex`、根目录是工程根，texlab 默认会去哪里找 PDF？

> **参考答案**：去**根目录**（工程根）找 `main.pdf`，而不是 `src/main.pdf`。因为三个 `*Directory` 的默认 `.` 是相对**根目录**而言的（[Configuration.md:75](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L75) 注明「the same directory as the root directory」）。根目录的确定见 [u1-l2](u1-l2-project-detection.md)。

**练习 3**：`useFileList` 默认为什么是 `false`？

> **参考答案**：因为开启它要把引擎生成的 `.fls` 文件作为项目检测的额外输入，解析这些文件「may have an impact on performance」（[Configuration.md:58](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L58)），所以默认关闭，按需打开。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务（本讲规格指定的实践）。

**任务**：编写一份配置，让 texlab **用 latexmk 把 PDF 输出到 `./build` 目录、保存时自动编译**；然后说明：若改用**非 latexmk** 工具，需要额外显式设置哪些目录项、以及为什么。

### 第一部分：latexmk 方案（推荐——产物目录交给 latexmkrc 自动推断）

latexmk 方案的核心，是把「输出目录」写进 `.latexmkrc`，texlab 会据此**自动推断**，于是你**不必**手填三个 `*Directory`。

工程根目录下放一个 `.latexmkrc`（示例代码，Perl 语法）：

```perl
# 示例：.latexmkrc（latexmk 配置文件，非 texlab 配置）
$out_dir = 'build';   # 让 latexmk 把所有产物输出到 build/
```

编辑器里的 texlab 配置（示例配置，VS Code 风格 JSONC）：

```jsonc
// ===== 示例配置：latexmk + 输出到 build + 保存即编译 =====
{
  // Type: string ；Default value: "latexmk"
  // 用 latexmk 编译。默认即可，这里显式写出便于阅读。
  "texlab.build.executable": "latexmk",

  // Type: string[] ；Default value: ["-pdf", "-interaction=nonstopmode", "-synctex=1", "%f"]
  // 这里沿用默认 flag。输出目录交给 .latexmkrc 的 $out_dir 控制，
  // 因此 args 里不必再写 -outdir（写也无妨，但两边要一致）。
  "texlab.build.args": [
    "-pdf",
    "-interaction=nonstopmode",
    "-synctex=1",
    "%f"
  ],

  // Type: boolean ；Default value: false
  // 打开「保存即编译」。
  "texlab.build.onSave": true

  // 注意：这里【不需要】写 auxDirectory/logDirectory/pdfDirectory。
  // 因为有 .latexmkrc，texlab 会从 $out_dir 自动推断产物在 build/
  // （见 Configuration.md:71 与 Project-Detection.md:22）。
}
```

**为什么这样能行**：`.latexmkrc` 同时承担两个角色——它既是**根目录标志**（命中后该目录被当作根），又让 texlab **自动推断**产物目录（[Project-Detection.md:22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L22)、[Configuration.md:71](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L71)）。于是正向搜索能正确地在 `build/` 下找到 PDF。

### 第二部分：改用非 latexmk 工具时，需要额外设置什么、为什么

若把 `executable` 换成非 latexmk 工具（如 `pdflatex`、`tectonic`），上面「自动推断」**不再适用**——因为 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md) 明确：自动推断只在「using a `latexmkrc` file」时发生。此时你必须**额外显式设置**：

| 必须额外设置 | 作用 | 为什么非 latexmk 时必须设 |
| --- | --- | --- |
| `texlab.build.pdfDirectory` | 告诉 texlab 去哪找 PDF | 自动推断失效，texlab 默认只去根目录找 `foo.pdf`，找不到 `build/foo.pdf`，正向搜索会失败 |
| `texlab.build.auxDirectory` | 告诉 texlab 去哪找 `.aux` | 项目检测/标签解析需要读 aux；位置不对会漏掉交叉引用信息 |
| `texlab.build.logDirectory` | 告诉 texlab 去哪找 `.log` | 编译告警/诊断从日志里来；位置不对诊断会缺失 |
| （在 `build.args` 里）让引擎真的输出到 `build` | 让产物实际落到 `build/` | 设置项只告诉 texlab「去哪找」，**引擎是否会真的输出到那**取决于 `args`；两者必须一致 |

对应的非 latexmk 配置（示例配置）：

```jsonc
// ===== 示例配置：非 latexmk（pdflatex）+ 输出到 build + 保存即编译 =====
{
  "texlab.build.executable": "pdflatex",

  // 引擎侧：让 pdflatex 把产物输出到 build/（"两头对齐"的第一头）
  "texlab.build.args": [
    "-synctex=1",
    "-interaction=nonstopmode",
    "-output-directory", "build",   // 拆成两个元素，符合拆分规则
    "%f"
  ],

  "texlab.build.onSave": true,

  // texlab 侧：告诉 texlab 产物在 build/（"两头对齐"的第二头）
  "texlab.build.auxDirectory": "build",
  "texlab.build.logDirectory": "build",
  "texlab.build.pdfDirectory": "build"
}
```

**一句话总结「为什么」**：latexmk + latexmkrc 时，texlab 能从 latexmkrc **一处**读出产物目录（单一事实来源）；非 latexmk 时没有这个「单一来源」，产物位置变成了「引擎实际输出位置」与「texlab 期望位置」**两件独立的事**，所以必须在 `args`（控制引擎）和 `*Directory`（告知 texlab）两头分别对齐。

**自检清单**：

- [ ] 你的 latexmk 方案能编译并在 `build/` 产出 PDF，保存即触发（对应 4.1 + 4.2）。
- [ ] 你能解释为何 latexmk 方案**不需要**手填三个 `*Directory`（对应 4.3：latexmkrc 自动推断）。
- [ ] 你的非 latexmk 方案里，`args` 与三个 `*Directory` 指向**同一**目录（对应 4.3：两头对齐）。
- [ ] 你没有使用已弃用的 `texlab.auxDirectory`，而是用 `texlab.build.auxDirectory` 等（对应 4.3：弃用项）。

> 真正端到端跑通「保存→编译→跳到 PDF」还需要配 `forwardSearch.*`，那是 [u3-l1](u3-l1-build-and-preview-workflow.md)、[u3-l2](u3-l2-synctex-viewers.md) 的内容；本综合实践聚焦在**构建与产物定位**这一段。

## 6. 本讲小结

- **`build.executable` / `build.args`**：`executable` 决定「谁编译」（默认 `latexmk`），`args` 决定「怎么编译」（默认 `["-pdf", "-interaction=nonstopmode", "-synctex=1", "%f"]`）。换工具要换 args；`%f` 是 `build.args` 唯一占位符；flag 与参数必须拆成数组独立元素（[Configuration.md:5-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L5-L30)）。
- **`onSave` / `forwardSearchAfter`**：两者默认都是 `false`。`onSave` 打开「保存即编译」；`forwardSearchAfter` 打开「编译后顺手跳一次 PDF」，需配合 `forwardSearch.*`。官方**不建议**用 latexmk `-pvc`，因为 texlab 不会被 latexmk 通知编译完成（[Configuration.md:34-50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L34-L50)、[Previewing.md:13-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L13-L15)）。
- **产物目录**：`auxDirectory`/`logDirectory`/`pdfDirectory` 三项结构一致，默认 `.`（根目录）。**非 latexmk** 时生效，且要与 `args` **两头对齐**；**用 latexmkrc** 时 texlab 自动推断（[Configuration.md:66-101](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L66-L101)）。
- **latexmkrc 的双重角色**：它既是根目录标志，又让 texlab 自动读出产物目录；推断失败才回退到三个 `*Directory` 设置（[Project-Detection.md:22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L22)）。
- **`filename`**：覆盖产物 PDF 文件名，默认 `null`（按 `foo.tex → foo.pdf` 约定），用于正向搜索找对 PDF（[Configuration.md:105-112](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L105-L112)）。
- **`useFileList`**：开则用引擎的 `.fls` 反哺项目检测，但有性能代价，默认 `false`（[Configuration.md:54-62](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L54-L62)）。
- **弃用项**：顶层 `texlab.auxDirectory` 已弃用，改用 `texlab.build.auxDirectory` 及拆分出的 `logDirectory`/`pdfDirectory`（[Configuration.md:117-126](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L117-L126)）。

## 7. 下一步学习建议

本讲把「怎么编译、产物去哪」讲清了。接下来建议：

1. **[u3-l1 编译与预览的整体流程](u3-l1-build-and-preview-workflow.md)**：把本讲的 `onSave`/`forwardSearchAfter` 与 `forwardSearch.*`、SyncTeX 串成端到端的「保存→编译→跳转」工作流，理解整条链路由哪些配置项驱动。
2. **[u3-l2 SyncTeX 双向搜索与阅读器集成](u3-l2-synctex-viewers.md)**：为你的平台选阅读器（SumatraPDF/Zathura/Skim 等），配好 `forwardSearch.executable`/`args`，并理解逆向搜索子命令。
3. **[u3-l3 使用 Tectonic 作为替代引擎](u3-l3-tectonic-engine.md)**：本讲提到了「换非 latexmk 工具要两头对齐」，tectonic 是最常见的替代引擎之一；那一讲给出 tectonic 的 `args` 写法与 `--outdir` 对齐技巧。
4. **想查具体项**：随时回到 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)，用 u2-l1 教的「三要素」法独立阅读。
