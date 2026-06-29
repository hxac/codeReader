# SyncTeX 双向搜索与 PDF 阅读器集成

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「正向搜索（编辑器 → PDF）」和「逆向搜索（PDF → 编辑器）」分别由谁驱动、走哪条路径。
- 读懂并写出 `texlab.forwardSearch.executable` 与 `texlab.forwardSearch.args`，正确使用 `%f` / `%p` / `%l` 三个占位符。
- 为自己平台上的 PDF 阅读器（SumatraPDF / Evince / Okular / Zathura / qpdfview / Skim / Sioyek）拼出正向搜索配置。
- 理解逆向搜索如何借助 `texlab inverse-search` 子命令把坐标转发回编辑器，并与 LSP 的 `window/showDocument` 衔接。

## 2. 前置知识

本讲承接 [u3-l1「编译与预览的整体流程」](u3-l1-build-and-preview-workflow.md)，请先确认你已经掌握以下几点：

- **SyncTeX**：TeX 引擎加 `-synctex=1` 编译后会产出一个 `.synctex.gz` 文件，它是一张「源文件行 ↔ PDF 页面位置」的对照表。双向跳转的全部基础就是这张表。
- **`build.forwardSearchAfter`**：保存编译后顺手做一次正向搜索的开关。它本身不指定「跳到哪、用什么阅读器」，必须与 `texlab.forwardSearch.*` 配套，否则没有目标可跳。
- **三占位符**：texlab 在执行外部命令前会替换 `%f`（当前 TeX 文件路径）、`%p`（当前 PDF 路径）、`%l`（当前行号）。其中 `%p`、`%l` 只在 `forwardSearch.args` 里出现。

> 一个容易混淆的点：正向搜索配置写在 **texlab 的配置里**，占位符是 texlab 的 `%f`/`%p`/`%l`；逆向搜索配置写在 **PDF 阅读器自己的设置里**，占位符是阅读器的（如 `%{input}`、`%1`）。本讲会反复强调这条界线。

## 3. 本讲源码地图

本讲涉及的真实源码只有两份 wiki 页面：

| 文件 | 作用 |
| --- | --- |
| `Previewing.md` | 列出 7 款阅读器的正向/逆向搜索配方，以及 `texlab inverse-search` 子命令的用法。 |
| `Configuration.md` | 定义 `texlab.forwardSearch.executable` 与 `texlab.forwardSearch.args` 两项配置的类型、默认值与占位符。 |

## 4. 核心概念与源码讲解

### 4.1 forwardSearch 配置：正向搜索的「引擎」

#### 4.1.1 概念说明

**正向搜索（forward search）** 指从编辑器里光标所在的位置，跳到 PDF 中对应的页面与坐标。

texlab 自己不会画 PDF，它的做法和「调用 TeX 引擎编译」一样——执行一条外部命令：把阅读器的可执行程序当作 `texlab.forwardSearch.executable`，把传给阅读器的参数当作 `texlab.forwardSearch.args`，让阅读器自己用 SyncTeX 跳转。

所以正向搜索能不能用，完全取决于两点：阅读器是否支持 SyncTeX，以及你是否把 `texlab.forwardSearch.*` 配对了。

#### 4.1.2 核心流程

```
编辑器光标在第 L 行
        │
        ▼
触发正向搜索（两种入口）
   ├─ build.forwardSearchAfter = true：编译完成后自动触发
   └─ 编辑器主动发起 textDocument/forwardSearch 自定义请求
        │
        ▼
texlab 用当前文件的 %f / %p / %l 替换 forwardSearch.args 中的占位符
        │
        ▼
启动 forwardSearch.executable，阅读器收到参数后跳转到目标位置
```

关键点：`forwardSearch.executable` 与 `forwardSearch.args` 的默认值都是 `null`。也就是说——**零配置时正向搜索根本不会执行**。你必须显式给出这两项，正向搜索才有目标可跳。

#### 4.1.3 源码精读

先看配置项本身的定义。[Configuration.md:L130-L138](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L130-L138) 定义了 `texlab.forwardSearch.executable`：类型是 `string | null`，默认值 `null`，并且强调「previewer needs to support SyncTeX」。

再看参数项。[Configuration.md:L141-L155](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L141-L155) 定义了 `texlab.forwardSearch.args`，类型 `string[] | null`，默认 `null`。这里正式列出三个占位符的含义：

- `%f`：当前 TeX 文件路径；
- `%p`：当前 PDF 文件路径；
- `%l`：当前行号。

这条配置与编译之间如何衔接，见 [Previewing.md:L9-L15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L9-L15)：要让 PDF 跟随光标同步，需要「enable SyncTeX 且更新 `texlab.forwardSearch` 配置」，并搭配 `texlab.build.forwardSearchAfter`。同时官方提醒——**不要用 latexmk 的 `-pvc`**，因为 texlab 收不到 latexmk 编译完成的通知，无法可靠触发后续正向搜索；改用 `texlab.build.onSave` 即可。

> 类型签名里的 `| null` 不是装饰：它正是「未配置」的语义。`forwardSearch.*` 与 `build.*` 不同——`build.*` 有完整的 latexmk 默认值，开箱即用；而 `forwardSearch.*` 默认空，必须由你填上阅读器。

#### 4.1.4 代码实践

1. **实践目标**：在不依赖任何具体阅读器细节的前提下，验证 texlab 是否真的会按配置去执行外部命令。
2. **操作步骤**：
   - 在项目里写一个最小可编译的 `main.tex`（含 `\documentclass{article}` 与 `document` 环境），用 `latexmk` 编译一次，确保产出 `main.pdf` 与 `main.synctex.gz`。
   - 临时把 `forwardSearch.executable` 指向一个你能观测的程序，例如 Linux/macOS 下的 `echo`、Windows 下的 `cmd /c echo`（**示例配置**，仅用于观测）：
     ```json
     {
       "texlab.build.onSave": true,
       "texlab.build.forwardSearchAfter": true,
       "texlab.forwardSearch.executable": "echo",
       "texlab.forwardSearch.args": ["forward-search-called", "%f", "%p", "%l"]
     }
     ```
   - 保存 `main.tex` 触发编译。
3. **需要观察的现象**：texlab 在编译完成后会尝试执行 `echo forward-search-called <文件路径> <PDF路径> <行号>`，占位符会被替换成真实值。
4. **预期结果**：你能在 texlab 的日志/输出里看到这条命令被调用，且 `%f`/`%p`/`%l` 已被替换为实际路径与行号。若没有任何动静，说明 `forwardSearchAfter` 未开或这两项仍为 `null`。
5. 说明：`echo` 不会真的跳转，本步只验证「调用链是否打通」。真实的阅读器配置见 4.2。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `forwardSearch.executable` 留空（`null`）时，即便 `build.forwardSearchAfter=true`，正向搜索也不会发生？

**答案**：`forwardSearchAfter` 只决定「编译后要不要触发正向搜索」，但触发之后 texlab 需要 `forwardSearch.executable` + `args` 才知道调用谁、怎么调。两者默认 `null`，等于没有目标，所以什么都不会执行。

**练习 2**：`%p` 这个占位符能出现在 `texlab.build.args` 里吗？

**答案**：不能。`%p`（PDF 路径）和 `%l`（行号）只出现在 `forwardSearch.args`；`build.args` 只认 `%f`（要编译的 TeX 文件）。

---

### 4.2 各阅读器正向搜索配方

#### 4.2.1 概念说明

不同阅读器调用 SyncTeX 的命令行参数差异极大：有的有专用 forward-search 子命令（SumatraPDF），有的用 URL 片段（Okular、qpdfview），有的靠独立脚本走 D-Bus（Evince）。但它们都遵循同一个套路——

> **texlab 把 `%f`/`%p`/`%l` 填进 `forwardSearch.args`，阅读器收到后自己跳转。**

所以下面这些配方的本质，就是把阅读器的「跳转命令」用 texlab 的三个占位符改写一遍。

#### 4.2.2 核心流程：配方对照表

下表汇总 7 款阅读器的正向搜索入口（均为官方配方）。注意「参数形态」一列：同一份信息（文件、行、PDF）在不同阅读器里被塞进完全不同的位置。

| 阅读器 | 平台 | `forwardSearch.args` 形态要点 |
| --- | --- | --- |
| SumatraPDF | Windows | `-reuse-instance %p -forward-search %f %l` |
| Evince | Linux | 经 `evince-synctex` 脚本走 D-Bus |
| Okular | Linux | `--unique file:%p#src:%l%f`（URL 片段） |
| Zathura | Linux | `--synctex-forward %l:1:%f %p` |
| qpdfview | Linux | `--unique %p#src:%f:%l:1`（URL 片段） |
| Skim | macOS | 经 `displayline -r %l %p %f` |
| Sioyek | 跨平台 | 正/逆向合并进同一条 args |

> 一个跨阅读器的共同点：`%f`、`%p`、`%l` 的**含义对所有阅读器都一样**（texlab 这边统一替换），但它们在命令行里出现的**位置和顺序**因阅读器而异。抄配方时务必连顺序一起抄。

#### 4.2.3 源码精读

**SumatraPDF（Windows，官方首选）**：官方在 Windows 上推荐 SumatraPDF，因为 Adobe Reader 会锁住已打开的 PDF、阻碍后续编译。配方见 [Previewing.md:L42-L53](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L42-L53)，参数 `["-reuse-instance", "%p", "-forward-search", "%f", "%l"]`——`-reuse-instance` 复用已打开的窗口，`%p` 指明 PDF，`-forward-search %f %l` 指明源文件与行号。

**Evince（Linux）**：Evince 的 SyncTeX 走 D-Bus 通信，命令行无法直接驱动，需安装 `evince-synctex` 脚本，见 [Previewing.md:L67-L81](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L67-L81)。注意它的 args 末尾嵌了一段 `"\"texlab -i %f -l %l\""`——这段不是正向搜索用的，而是 Evince 在逆向搜索时回调的命令，由 `evince-synctex` 一并塞进 D-Bus 会话。

**Okular（Linux）**：见 [Previewing.md:L97-L101](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L97-L101)。它把跳转信息编码成一个 URL 片段 `file:%p#src:%l%f`：`%p` 是 PDF，`#src:` 后面是 `行号`+`源文件路径`，`--unique` 保证只开一个实例。

**Zathura（Linux）**：见 [Previewing.md:L124-L128](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L124-L128)。参数 `["--synctex-forward", "%l:1:%f", "%p"]`，其中 `%l:1:%f` 是「行号:列号:源文件」三段，列号固定填 `1`。

**qpdfview（Linux）**：见 [Previewing.md:L150-L153](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L150-L153)。同样用 URL 片段：`["--unique", "%p#src:%f:%l:1"]`，`#src:` 后是「源文件:行号:列号」。

**Skim（macOS，官方首选）**：macOS 上官方推荐 Skim，因为它是唯一原生支持 SyncTeX 的阅读器。配方见 [Previewing.md:L181-L185](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L181-L185)，调用 Skim 自带的 `displayline` 脚本：`["-r", "%l", "%p", "%f"]`。可选加 `-g` 让 Skim 跳转后不抢占前台焦点。**重要**：要在 Skim 偏好设置里**关掉**「Reload automatically」（Skim → Preferences → Sync → Check for file changes），否则会与 texlab 的重建流程冲突。

**Sioyek（跨平台）**：最特殊，正向与逆向搜索合并写在同一条 `forwardSearch.args` 里，见 [Previewing.md:L204-L218](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L204-L218)。它通过 `--forward-search-file %f --forward-search-line %l %p` 做正向跳转，同时用 `--inverse-search "texlab inverse-search -i \"%%1\" -l %%2"` 预先登记逆向回调命令。其中的 `%%1`/`%%2` 是 Sioyek 自身的占位符转义写法，并非 texlab 的占位符——再次印证「逆向搜索那段的占位符属于阅读器」。

#### 4.2.4 代码实践

1. **实践目标**：为你平台上的阅读器写一份正确的正向搜索配置。
2. **操作步骤**：
   - Windows 选 SumatraPDF、Linux 选 Zathura、macOS 选 Skim（任选其一即可）。
   - 从上方源码精读中对应阅读器的配方，**逐字逐顺序**抄进你的 `texlab.forwardSearch.executable` 与 `texlab.forwardSearch.args`（注意把 SumatraPDF 配方里的 `{User}` 换成你的实际用户名，Skim 的路径写绝对路径）。
   - 打开 `texlab.build.onSave=true` 与 `texlab.build.forwardSearchAfter=true`。
3. **需要观察的现象**：保存 `.tex` 后，先触发编译，编译结束后阅读器自动跳到光标对应的 PDF 位置。
4. **预期结果**：光标在第 N 行，PDF 也跳到第 N 行对应的段落。
5. **排错**：如果只编译不跳转——检查 `forwardSearch.*` 是否仍为 `null`；如果跳转位置错乱——检查 `%f`/`%p`/`%l` 在 args 里的顺序是否抄错。

#### 4.2.5 小练习与答案

**练习 1**：Okular 和 qpdfview 都用 URL 片段编码跳转信息，但参数顺序不同。请说明 `%f`/`%p`/`%l` 各自出现在两者的什么位置。

**答案**：
- Okular：`file:%p#src:%l%f` → PDF 在 `%p`，`#src:` 后是「`%l` 行号 + `%f` 源文件」。
- qpdfview：`%p#src:%f:%l:1` → PDF 在 `%p`，`#src:` 后是「`%f` 源文件 : `%l` 行号 : `1` 列号」。
顺序恰好相反，所以不能互相套用。

**练习 2**：为什么 Evince 不能像 Zathura 那样直接把 `zathura` 换成 `evince` 就能用？

**答案**：Evince 的 SyncTeX 通过 D-Bus 通信，命令行无法直接驱动，必须借助 `evince-synctex` 脚本把命令行请求转成 D-Bus 调用。所以 `forwardSearch.executable` 填的是 `evince-synctex` 而非 `evince`。

---

### 4.3 逆向搜索与 `texlab inverse-search` 子命令

#### 4.3.1 概念说明

**逆向搜索（inverse search）** 与正向相反：在 PDF 里点击某处，跳回编辑器中对应的源码位置。

这里有一个架构上的不对称：

- **正向搜索**：texlab 主动执行阅读器命令（有 `texlab.forwardSearch.*` 配置）。
- **逆向搜索**：是**阅读器**在 PDF 被点击时，主动调用一个「外部命令」。这个外部命令就是 texlab 提供的 `texlab inverse-search` 子命令——它接收文件和行号，再通过 LSP 的 `window/showDocument` 请求让编辑器跳转。

换句话说，逆向搜索时 texlab 成了「被阅读器调用的程序」，而不是「调用阅读器的程序」。

#### 4.3.2 核心流程

```
用户在 PDF 上点击（如 Ctrl+Click / Shift+Click / Alt+DoubleClick）
        │
        ▼
阅读器用自己的占位符拼出一条命令并执行
   例如 Zathura: texlab inverse-search -i %{input} -l %{line}
        │
        ▼
texlab inverse-search 接到 --input <FILE> --line <LINE>
        │
        ▼
texlab 通过 LSP 向编辑器发送 window/showDocument 请求
        │
        ▼
编辑器打开 <FILE> 并跳到 <LINE>
```

关键前提：编辑器必须支持 `window/showDocument` 请求；并且阅读器要能找到 `texlab` 这个可执行程序（路径固定或在 `PATH` 中）。若编辑器不支持、或 `texlab` 路径不固定，就需要按编辑器自身的方式适配，社区也有现成插件可帮忙。

#### 4.3.3 源码精读

逆向搜索机制的总体说明在 [Previewing.md:L21-L29](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L21-L29)：编辑器需支持 `window/showDocument`，然后用 `texlab inverse-search --input <FILE> --line <LINE>` 转发逆向搜索。子命令同时支持长写法 `--input`/`--line` 与短写法 `-i`/`-l`，各阅读器配方里两种都有出现。同段还列出了两个可帮助配置逆向搜索的插件：Neovim 的 `f3fora/nvim-texlabconfig`、Emacs 的 `ROCKTAKEY/lsp-latex`。

下面是各阅读器的逆向搜索配置（均写在**阅读器自己的设置**里，不是 texlab 配置）：

- **SumatraPDF**：在其设置文件（Menu → Settings → Advanced Options）写 `InverseSearchCmdLine = "texlab inverse-search --input "%f" --line %l"`，触发键为 `Alt+DoubleClick`。见 [Previewing.md:L57-L63](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L57-L63)。这里的 `%f`/`%l` 是 **SumatraPDF 自己的占位符**。
- **Evince**：用 `evince-synctex` 时逆向搜索已自动配好，`Ctrl+Click` 触发。见 [Previewing.md:L83-L86](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L83-L86)。
- **Okular**：在 Settings → Configure Okular → Editor 里选「Custom Text Editor」，命令填 `texlab inverse-search -i "%f" -l %l`，触发键 `Shift+Click`。见 [Previewing.md:L103-L113](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L103-L113)。
- **Zathura**：在 `~/.config/zathura/zathurarc` 里写 `set synctex true` 和 `set synctex-editor-command "texlab inverse-search -i %{input} -l %{line}"`，触发键 `Ctrl+Click`。见 [Previewing.md:L130-L139](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L130-L139)。`%{input}`/`%{line}` 是 **Zathura 的占位符**。
- **qpdfview**：在 Edit → Settings → Behavior → Source editor 里填 `texlab inverse-search -i "%1" -l %2`，再选一个鼠标修饰键，触发键 `Modifier+Click`。见 [Previewing.md:L156-L166](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L156-L166)。`%1`/`%2` 是 **qpdfview 的占位符**。
- **Skim**：在 Skim → Preferences → Sync → PDF-TeX Sync support 里选编辑器预设，触发键 `Shift+⌘+Click`。见 [Previewing.md:L191-L194](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L191-L194)。
- **Sioyek**：逆向搜索命令已合并进正向搜索的 `--inverse-search` 参数（见 4.2.3），无需单独配置。

> **占位符归属小结**：正向搜索里的 `%f`/`%p`/`%l` 是 **texlab 的**；逆向搜索配置里的 `%f`/`%l`、`%{input}`/`%{line}`、`%1`/`%2`、`%%1`/`%%2` 是 **各自阅读器的**。两套占位符长得像，但属于不同系统，配置时不要张冠李戴。

#### 4.3.4 代码实践

1. **实践目标**：验证 `texlab inverse-search` 子命令确实能把坐标转发回编辑器。
2. **操作步骤**：
   - 确认 `texlab` 可执行程序在 `PATH` 中，或在阅读器配置里用了绝对路径。
   - 在命令行直接手动调用（**示例命令**，把路径换成你工程里的真实文件与行号）：
     ```bash
     texlab inverse-search --input /path/to/main.tex --line 42
     ```
   - （此命令需要 texlab 正作为某个编辑器的 LSP 服务器在运行，否则没有 `window/showDocument` 的接收方。）
3. **需要观察的现象**：编辑器把 `main.tex` 打开并把光标定位到第 42 行。
4. **预期结果**：编辑器跳转到指定文件的指定行。
5. 说明：若编辑器不支持 `window/showDocument`，或 texlab 未在该编辑器进程内运行，此命令不会有可见效果——这正是 Previewing.md 提到「需要按编辑器适配或借助插件」的原因。具体能否在你环境中跑通，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：正向搜索和逆向搜索，哪一个是「texlab 调用别人」，哪一个是「别人调用 texlab」？

**答案**：正向搜索是 texlab 主动执行阅读器命令（texlab 调用别人）；逆向搜索是阅读器在 PDF 被点击时调用 `texlab inverse-search`（别人调用 texlab），texlab 再通过 `window/showDocument` 让编辑器跳转。

**练习 2**：Zathura 的逆向搜索配置里写的是 `%{input}`/`%{line}`，为什么不是 texlab 的 `%f`/`%l`？

**答案**：因为这条配置写在 Zathura 自己的 `zathurarc` 里，由 Zathura 负责替换占位符，所以用的是 Zathura 的占位符语法 `%{input}`/`%{line}`；只有 texlab 配置（`forwardSearch.args`）里才会用 texlab 的 `%f`/`%l`。

## 5. 综合实践

把本讲三个模块串起来，完成一次完整的双向搜索闭环：

1. 准备一个最小多文件工程：`main.tex` 用 `\input{chapter1}` 引入 `chapter1.tex`，两文件里各写一段带 `\section` 的正文（确保编译后 PDF 有两页以上）。
2. **编译 + 正向搜索**：
   - 配置 `texlab.build.onSave=true`、`texlab.build.forwardSearchAfter=true`。
   - 按你平台选阅读器（Windows→SumatraPDF / Linux→Zathura / macOS→Skim），把 4.2 的正向搜索配方填进 `texlab.forwardSearch.*`。
   - 把光标放在 `chapter1.tex` 某一行，保存。预期：编译完成后 PDF 跳到该行对应位置。
3. **逆向搜索**：按 4.3 在阅读器侧配好逆向搜索（如 Zathura 的 `zathurarc`、SumatraPDF 的 `InverseSearchCmdLine`）。在 PDF 对应 `chapter1.tex` 的位置点击，预期：编辑器跳回 `chapter1.tex` 的源码行。
4. **记录链路**：用一张表标注「正向搜索由哪几项配置驱动」「逆向搜索由谁触发、`texlab inverse-search` 在其中扮演什么角色」「占位符在两个方向分别归谁」。

> 若逆向搜索无反应，依次排查：编辑器是否支持 `window/showDocument`、`texlab` 是否在 `PATH`、阅读器触发键是否正确、阅读器占位符是否抄错。

## 6. 本讲小结

- 正向搜索（编辑器 → PDF）由 texlab 执行外部命令驱动，配置项是 `texlab.forwardSearch.executable` + `texlab.forwardSearch.args`，默认均为 `null`，必须显式配置。
- `forwardSearch.args` 用 texlab 的三占位符：`%f`（TeX 文件）、`%p`（PDF）、`%l`（行号）；其中 `%p`/`%l` 只在此处出现。
- 7 款阅读器的配方形态各异（URL 片段、专用子命令、D-Bus 脚本），但套路一致：把阅读器的跳转命令用 `%f`/`%p`/`%l` 改写，且顺序必须照抄。
- 逆向搜索（PDF → 编辑器）由阅读器在点击时调用 `texlab inverse-search --input/-i <FILE> --line/-l <LINE>`，再经 LSP 的 `window/showDocument` 让编辑器跳转。
- 逆向搜索配置写在阅读器自己的设置里，占位符是阅读器的（如 `%{input}`、`%1`），与 texlab 的 `%f`/`%l` 不是同一套，切勿混淆。
- 前置依赖：编译需加 `-synctex=1` 产出 `.synctex.gz`；正向搜索常配合 `build.forwardSearchAfter`，且不要用 latexmk `-pvc`。

## 7. 下一步学习建议

- 继续阅读 [Previewing.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md) 中你所用阅读器的完整段落，留意官方是否更新了触发键或路径。
- 如果你想换成 **tectonic** 引擎，进入 [u3-l3「使用 Tectonic 作为替代 TeX 引擎」](u3-l3-tectonic-engine.md)，注意 tectonic 默认是否产出 SyncTeX 文件，否则本讲的双向搜索会失效。
- 想了解正向/逆向搜索在协议层是如何被 `textDocument/forwardSearch` 自定义请求与 `window/showDocument` 承载的，可进入 [u4-l1「自定义 LSP 消息：build 与 forwardSearch」](u4-l1-custom-lsp-messages.md)。
