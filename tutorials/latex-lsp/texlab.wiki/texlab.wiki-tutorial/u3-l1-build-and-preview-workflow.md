# 编译与预览的整体流程

## 1. 本讲目标

[u2-l2](u2-l2-build-config.md) 把我们带进了 `texlab.build.*` 这个子系统，知道了「**怎么**编译」「**什么时候**编译」「产物放哪」。但那些知识是一颗颗**零件**。本讲要把零件装回原位，看清整台机器怎么转——也就是 texlab 把「**保存一个 `.tex` → 编译出 PDF → 在 PDF 里跳到光标对应位置**」这条端到端链路，**到底是怎么串起来的**。

读完本讲，你应该能够：

1. 说出编译的**两条触发路径**——自定义请求 `textDocument/build` 与 `onSave` 自动构建——并说清它们的区别和各自适合的场景。
2. 画出 `forwardSearchAfter` 串起的「**保存 → 编译 → 正向搜索**」完整工作流，并能指出流程里每一步分别由哪个配置项驱动（`build.*`？`build.filename`？`forwardSearch.*`？）。
3. 解释 **SyncTeX** 是什么、它产生的 `.synctex.gz` 文件起什么作用，以及「正向搜索（编辑器→PDF）」和「逆向搜索（PDF→编辑器）」分别走哪条配置/命令链路。
4. 讲清 wiki 里那句「**不推荐用 latexmk `-pvc`**」背后的原因，并能据此判断自己该用哪种预览方案。

本讲是 [u3 单元](u3-l2-synctex-viewers.md) 的总纲：[u3-l2](u3-l2-synctex-viewers.md) 会给出各 PDF 阅读器的逐家配方，[u3-l3](u3-l3-tectonic-engine.md) 会讲 tectonic 替代引擎，而本讲先把**整体骨架**立起来。

## 2. 前置知识

本讲承接两篇讲义：

- **[u2-l2 构建配置 texlab.build.*](u2-l2-build-config.md)**：你必须已经掌握 `build.executable`/`build.args`（默认 `latexmk` + `["-pdf","-interaction=nonstopmode","-synctex=1","%f"]`）、`build.onSave`、`build.forwardSearchAfter` 这几项的含义与默认值。本讲把**这些当成已知**，不再从零定义，而是讲它们如何配合。
- **[u1-l2 项目识别与根目录检测](u1-l2-project-detection.md)**：编译发生在**根目录**上（TeX 引擎的 `\input` 基于工作目录而非源文件目录）。你需要记得：保存任意一个项目内文件，触发的是对**根文档**的编译，而不是对当前文件的孤立编译。

本讲还要用到几个通俗概念：

- **SyncTeX**：TeX 引擎的一个功能。编译时打开 `-synctex=1`（注意它**就在 `build.args` 的默认值里**），引擎会额外产出一个 `.synctex.gz` 文件，里面记录「源代码第几行 ↔ PDF 哪一页哪一个坐标」的对应关系。没有这个文件，「点击源码跳 PDF」就无从谈起。
- **正向搜索（forward search）**：从**编辑器**跳到 **PDF**——光标在某一行，跳到 PDF 里对应的位置。由 `texlab.forwardSearch.*` 配置驱动。
- **逆向搜索（inverse search）**：从 **PDF** 跳回**编辑器**——在 PDF 里点击，跳到源代码对应行。由 PDF 阅读器回调 `texlab inverse-search` 子命令驱动。
- **PDF 阅读器（previewer / viewer）**：负责显示 PDF 的程序，如 SumatraPDF、Zathura、Skim 等。texlab 自己**不显示 PDF**，而是按 `forwardSearch.executable` + `args` 调用阅读器，并要求阅读器**支持 SyncTeX**。

> 提示：本仓库是 texlab 的**官方 wiki**（纯文档）。本讲的「源码」主要是 [Previewing.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md)（编译与预览总览）和 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)，并会少量引用 [LSP-Internals.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md)（自定义请求的接口形态）。自定义请求的完整消息结构会在 [u4-l1](u4-l1-custom-lsp-messages.md) 深入，本讲只用到「工作流层面」的事实。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `Previewing.md` | 编译与预览的**总览页**：列出预览的几种方案、两条编译触发路径、为何不用 `-pvc`、逆向搜索子命令 | 几乎所有「工作流层面」的事实取自这里（触发路径、`-pv`/`-pvc` 的取舍、逆向搜索入口） |
| `Configuration.md` | `texlab.build.*` 与 `texlab.forwardSearch.*` 全部配置项 | `build.onSave`、`build.forwardSearchAfter`、`build.filename`、`forwardSearch.executable`、`forwardSearch.args` 在流程里的角色 |
| `LSP-Internals.md` | 自定义 LSP 消息接口 | 仅引用 `textDocument/build` 与 `textDocument/forwardSearch` 的方法名与返回状态（完整结构留给 [u4-l1](u4-l1-custom-lsp-messages.md)） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 编译触发方式**——自定义请求 `textDocument/build` vs `onSave` 自动构建，二者的区别与选择。
- **4.2 forwardSearchAfter 工作流**——把「编译」和「跳 PDF」串成一条自动流水线，看清 `build.filename` 与 `forwardSearch.*` 在其中扮演的衔接角色。
- **4.3 SyncTeX 概念**——`.synctex.gz` 是什么、正向搜索与逆向搜索各自走哪条链路、为什么官方不推荐 `-pvc`。

> 与 u2-l2 的分工：u2-l2 讲的是「`onSave`、`forwardSearchAfter` **是什么**（定义、默认值、要不要开）」；本讲讲的是「它们**怎么协同**形成一条端到端工作流」，并把 SyncTeX 这个 u2-l2 只是点到为止的概念讲透。如果你对这两个开关的「定义」还陌生，请先回看 [u2-l2 的 4.2 节](u2-l2-build-config.md)。

### 4.1 编译触发方式：自定义请求 vs onSave

#### 4.1.1 概念说明

「编译」这个动作本身，texlab 是通过调用外部构建工具（默认 `latexmk`）完成的——这部分 [u2-l2](u2-l2-build-config.md) 已经讲透。但「**谁去按下这个编译按钮**」却有两种完全不同的途径。Previewing.md 开篇一句话就把两条路径点明了：

> `texlab` supports compiling LaTeX using a custom request (`textDocument/build`) and by building a document after saving if configured to do so.

翻译过来就是：编译可以由**编辑器主动发一个自定义请求**触发（路径 A），也可以由**保存文件这个动作**触发（路径 B）。两条路径最终调用的都是同一套 `build.executable` + `build.args`，区别只在于「**谁来发起、什么时候发起**」。

#### 4.1.2 核心流程

```text
┌──────────────── 路径 A：自定义请求（手动 / 显式）────────────────┐
│                                                                  │
│  用户按键/菜单                                                    │
│     │                                                            │
│     ▼                                                            │
│  编辑器(LSP 客户端) 发送自定义请求: method = "textDocument/build" │
│     │                                                            │
│     ▼                                                            │
│  texlab 收到请求 → 调用 build.executable + build.args 编译        │
│     │                                                            │
│     ▼                                                            │
│  texlab 回复响应: BuildResult { status: Success|Error|... }      │
│     │                                                            │
│     ▼                                                            │
│  编辑器据 status 决定如何反馈（成功/失败提示）                     │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

┌──────────────── 路径 B：onSave（自动）────────────────┐
│                                                       │
│  用户保存文件 (Ctrl+S)                                 │
│     │                                                 │
│     ▼  （仅当 build.onSave = true）                    │
│  texlab 监测到保存事件 → 自动调用 build.executable     │
│     │                   + build.args 编译根文档         │
│     ▼                                                 │
│  （可选）若 forwardSearchAfter = true → 接着正向搜索    │
│                                                       │
└───────────────────────────────────────────────────────┘
```

两条路径的对比：

| 维度 | 路径 A：`textDocument/build` 自定义请求 | 路径 B：`onSave` 自动构建 |
| --- | --- | --- |
| 触发者 | 编辑器**主动**发请求 | 用户**保存文件**这一动作 |
| 是否需配置 | 不依赖 `build.*` 开关（编辑器直接发请求） | 需 `build.onSave = true` 才生效 |
| 同步性 | **同步**：编辑器会拿到 `BuildResult`（含 `status`） | **异步**：texlab 在后台编译，编辑器通常不直接拿到结果 |
| 反馈 | 编辑器可据 `status`（成功/出错/崩溃/取消）给提示 | 靠诊断、日志或下次正向搜索间接体现 |
| 适合场景 | 想要「按键才编译、且想知道编译结果」的精细控制 | 想要「无脑保存、自动出 PDF」的顺滑体验 |

> **关于 `BuildStatus`**：路径 A 的响应里带一个 `status` 枚举（`Success`/`Error`/`Failure`/`Cancelled`）。本讲只用到这里——它告诉我们「自定义请求是**有回执**的」。完整的请求/响应结构（`BuildTextDocumentParams`、`BuildResult`）会在 [u4-l1](u4-l1-custom-lsp-messages.md) 逐字段拆解。

#### 4.1.3 源码精读

- [Previewing.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L1-L3)：开篇明确编译的**两条触发路径**——「自定义请求 `textDocument/build`」与「保存后构建（设 `texlab.build.onSave` 为 true）」。这是本讲整条工作流的总纲。

- [Configuration.md:44-50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L44-L50)：`texlab.build.onSave`——设为 `true` 则「保存文件后编译项目」，`Type: boolean`，`Default value: false`。
  - 关键结论：**默认关**。也就是说，如果只装了 texlab、什么 `build.*` 都不配，保存文件**不会**自动编译；你要么自己打开 `onSave`，要么靠编辑器扩展发 `textDocument/build`。

- [Configuration.md:5-12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L5-L12) 与 [Configuration.md:15-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L15-L30)：`build.executable`（默认 `latexmk`）与 `build.args`（默认含 `%f`）。
  - 这两项是两条路径**共用**的「编译引擎」。无论走 A 还是 B，最终执行的都是同一套 `executable` + `args`。

- [LSP-Internals.md:7-10](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L7-L10)：`Build Request` 小节开头——「构建请求由客户端发给服务器，用于编译给定 LaTeX 文档」，并用 `textDocumentBuild` experimental capability 声明该请求存在。
  - 这说明路径 A 是一条**可选的**自定义 LSP 消息：客户端可以声明支持它，也可以不支持。结构细节见 [u4-l1](u4-l1-custom-lsp-messages.md)。

#### 4.1.4 代码实践

**实践目标**：亲手对比两条触发路径，体会「`onSave` 默认关、自定义请求要编辑器主动发」的差异。

**操作步骤**：

1. 准备一个能编译的最小工程（`main.tex` 含 `\documentclass{article}` 与 `document` 环境），先**不配置**任何 `build.*`（即 `onSave` 取默认 `false`）。
2. **路径 B 探测**：修改并保存 `main.tex`，观察是否自动编译（检查是否生成/更新 `main.pdf`）。
3. **路径 A 探测**：如果你用的编辑器扩展有「Build / 编译」按钮或快捷键（它内部会发 `textDocument/build`），按一下，观察是否编译。
4. 打开自动构建开关（示例配置）：
   ```jsonc
   // 示例配置：开启保存即编译
   "texlab.build.onSave": true
   ```
5. 再次修改并保存。

**需要观察的现象**：

- 第 2 步：保存后**没有**自动编译（除非你的编辑器扩展在保存时自己发了 `textDocument/build`）。
- 第 3 步：按「编译」按钮后**编译发生**（前提是编辑器扩展实现了路径 A）。
- 第 5 步：保存后**立即**触发编译。

**预期结果**：你直观体会到「两条路径都需要有人主动发起」——`onSave` 让「保存」这个动作自动发起；自定义请求则让编辑器在用户按键时发起。两者不冲突，可以并存。

> 实际能否编译成功取决于本机是否安装了 LaTeX 发行版（如 TeX Live / MiKTeX），**待本地验证**。若无发行版，可改为「源码阅读型实践」：对照 [Previewing.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L1-L3) 与 [Configuration.md:44-50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L44-L50)，说明为何「零配置保存不会编译」。

#### 4.1.5 小练习与答案

**练习 1**：小明说「装好 texlab 后，只要保存 `.tex` 就会自动编译」。这句话哪里不对？

> **参考答案**：错在「保存就自动编译」。`build.onSave` 默认是 `false`（见 [Configuration.md:44-50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L44-L50)）。除非编辑器扩展在保存时主动发了 `textDocument/build`，否则保存本身不会触发编译。要实现「保存即编译」，必须显式设 `texlab.build.onSave = true`。

**练习 2**：两条触发路径最终调用的「编译命令」一样吗？

> **参考答案**：一样。二者共用同一套 `build.executable` + `build.args`（见 [Configuration.md:5-12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L5-L12) 与 [Configuration.md:15-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L15-L30)）。区别只在于「谁来发起」：路径 A 由编辑器发 `textDocument/build`，路径 B 由保存事件驱动（需 `onSave=true`）。

**练习 3**：路径 A 是同步的、会返回 `BuildResult`；路径 B 看起来没有这种回执。这给「编译后自动跳 PDF」带来了什么影响？

> **参考答案**：路径 A 既然有回执，编辑器可以在拿到「编译成功」后再决定跳转；路径 B 没有显式回执，但 texlab **自己**知道编译何时完成（因为是它发起的子进程），所以 texlab 可以在编译结束后**内部**接着触发后续动作（正是下一节 `forwardSearchAfter` 干的事）。换句话说，路径 B 的「编译后动作」靠 texlab 内部衔接，而非靠回执。

---

### 4.2 forwardSearchAfter 工作流：串起「编译 → 正向搜索」

#### 4.2.1 概念说明

光「编译」还不够爽——你更想要的是：**改完代码一保存，PDF 自动更新，而且自动翻到我正在编辑的那一页那一行**。这正是 `forwardSearchAfter` 这个开关要解决的问题：它把「编译」和「正向搜索（跳到 PDF）」**自动接成一条流水线**。

Previewing.md 是这样描述的：

> If you want the PDF viewer to stay synchronized with the cursor position in your editor, you can instruct `texlab` to execute a forward search after every build (`texlab.build.forwardSearchAfter`).

也就是说，`forwardSearchAfter = true` 的语义是：「**每次编译之后，都自动跑一次正向搜索**」。但这条流水线要能跑通，光开一个开关不够，它**依赖一整套配置协同**——这正是本节要理清的。

#### 4.2.2 核心流程

把 4.1 的路径 B 接上正向搜索，完整的「保存→编译→跳转」链路是这样的：

```text
① 用户保存文件 (Ctrl+S)
        │
        ▼  开关 1：build.onSave = true
② texlab 自动调用 build.executable + build.args 编译根文档
   （args 里的 -synctex=1 让引擎产出 .synctex.gz）
        │
        ▼  开关 2：build.forwardSearchAfter = true
③ texlab 在编译结束后，自动发起一次「正向搜索」
        │
        ├─ 需要知道「PDF 在哪」 → 由 build.filename / pdfDirectory 定位 PDF 路径
        ├─ 需要知道「跳到哪一行」→ 当前光标所在行（编辑器提供）
        └─ 需要知道「用哪个阅读器、怎么调」→ forwardSearch.executable + forwardSearch.args
        │   （args 里的 %f/%p/%l 在执行前被替换：%f=TeX 路径, %p=PDF 路径, %l=行号）
        ▼
④ PDF 阅读器打开 PDF，并借助 .synctex.gz 跳到 %l 对应的位置
```

这条链路里，三个环节各有「负责的配置项」，把它们列成一张驱动表：

| 步骤 | 动作 | 由谁驱动 |
| --- | --- | --- |
| ①→② | 保存触发编译 | `texlab.build.onSave = true` |
| ② 本身 | 怎么编译、产出 `.synctex.gz` | `texlab.build.executable` / `texlab.build.args`（`-synctex=1` 必须在） |
| ②→③ | 编译后顺手跳 PDF | `texlab.build.forwardSearchAfter = true` |
| ③ 定位 PDF | 找到要打开的 PDF 文件 | `texlab.build.filename`（可选，覆盖默认推断）+ `pdfDirectory` |
| ③ 执行跳转 | 用哪个阅读器、传什么参数 | `texlab.forwardSearch.executable` / `texlab.forwardSearch.args`（含 `%f`/`%p`/`%l`） |

两个要点：

1. **`forwardSearchAfter` 单开没用**。它只是「编译后再跳一次」的开关；真正「怎么跳」由 `forwardSearch.*` 决定。如果 `forwardSearch.executable` 是 `null`（默认值），跳转就无处可去——对应到 LSP 层，正向搜索会返回 `Unconfigured` 状态（[u4-l1](u4-l1-custom-lsp-messages.md) 详述）。
2. **`build.filename` 是「编译」与「跳转」之间的桥**。编译产出 `foo.pdf`，正向搜索要去打开它。默认 texlab 会按「主文档 `foo.tex` → `foo.pdf`」推断；但如果你给 PDF 改了名（或输出到别的目录），就得用 `build.filename` 告诉 texlab 正确的 PDF 名，否则跳转会找错文件。

> 与 u2-l2 的衔接：u2-l2 讲过 `onSave` 与 `forwardSearchAfter` 两个开关「是什么、默认关、要不要开」。本节的新增价值是：把这两个开关放进**完整链路**，并补上 u2-l2 没展开的两个衔接件——`build.filename`（编译产物如何被正向搜索找到）与 `forwardSearch.*`（跳转本身如何执行）。`forwardSearch.*` 的逐项细节与各阅读器配方在 [u3-l2](u3-l2-synctex-viewers.md)。

#### 4.2.3 源码精读

- [Configuration.md:34-40](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L34-L40)：`texlab.build.forwardSearchAfter`——设为 `true` 则「编译后执行一次正向搜索」，`Type: boolean`，`Default value: false`。
  - 这是把 ②→③ 衔接起来的开关，默认关。

- [Configuration.md:105-113](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L105-L113)：`texlab.build.filename`——「覆盖构建产物的默认文件名」，并明确「此设置用于在正向搜索时找到正确的 PDF 文件」。
  - 这句注释直接点明了 `build.filename` 在工作流里的角色：它**专为正向搜索服务**，是编译与跳转之间的桥。默认 `null` 表示按 `foo.tex → foo.pdf` 推断。

- [Configuration.md:130-138](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L130-L138)：`texlab.forwardSearch.executable`——「PDF 预览器的可执行文件」，并要求阅读器**支持 SyncTeX**。`Type: string | null`，`Default value: null`。
  - 默认 `null` 意味着：不配阅读器，正向搜索就没目标。这就是「`forwardSearchAfter` 单开没用」的根源。

- [Configuration.md:141-156](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L141-L156)：`texlab.forwardSearch.args`——传给预览器的参数，占位符 `%f`/`%p`/`%l` 由服务器替换。
  - 三个占位符都在这里出现：`%f`（当前 TeX 文件）、`%p`（当前 PDF 文件）、`%l`（当前行号）。注意 `%p` 与 `%l` **只**在 `forwardSearch.args` 出现（`build.args` 只有 `%f`），因为只有「跳 PDF」才需要知道 PDF 路径和行号。

- [Previewing.md:9-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L9-L15)：描述「让 PDF 阅读器与编辑器光标保持同步」的方案——设 `texlab.build.forwardSearchAfter`，并强调需要启用 SyncTeX、更新 `texlab.forwardSearch` 配置。

#### 4.2.4 代码实践

**实践目标**：把「保存→编译→跳 PDF」整条流水线跑通，并记录每一步由哪个配置项驱动（本讲的「主实践」）。

**操作步骤**：

1. 准备一个多页、能在 PDF 里看清行位置差异的工程（比如 `main.tex` 写若干段 `\section`，每段几行文字），确保能编译出 `main.pdf`。
2. 先**只开**编译，不开跳转（示例配置）：
   ```jsonc
   // 示例配置：阶段一，只开保存即编译
   "texlab.build.onSave": true
   ```
   保存，确认 PDF 会更新，但 PDF **不会**自动翻页跟随光标。
3. 再开「编译后跳转」，并配上阅读器（以 Zathura 为例，详细配方见 [u3-l2](u3-l2-synctex-viewers.md)）：
   ```jsonc
   // 示例配置：阶段二，编译后自动正向搜索（Linux/Zathura）
   "texlab.build.onSave": true,
   "texlab.build.forwardSearchAfter": true,
   "texlab.forwardSearch.executable": "zathura",
   "texlab.forwardSearch.args": ["--synctex-forward", "%l:1:%f", "%p"]
   ```
4. 把光标移到 `main.tex` 的某个**靠后**的章节某一行，保存。
5. 试着给 PDF 改名（如把 `\jobname` 相关设置或用 `build.filename` 改成 `report.pdf`），观察不设 `build.filename` 时正向搜索是否能找到正确 PDF。

**需要观察的现象与对应的驱动项**：

| 现象 | 由哪一步/哪个配置驱动 |
| --- | --- |
| 保存即编译 | `build.onSave = true` |
| `.synctex.gz` 文件出现 | `build.args` 里的 `-synctex=1` |
| 编译后 PDF 自动翻到光标行 | `build.forwardSearchAfter = true` |
| 用 Zathura 打开并跳转 | `forwardSearch.executable` + `forwardSearch.args`（`%f`/`%p`/`%l` 被替换） |
| PDF 改名后跳转找不到文件 | 缺 `build.filename`，texlab 按 `foo.tex→foo.pdf` 推断失败 |

**预期结果**：你能清楚指认「保存→编译」由 `onSave` 驱动、「编译→跳转」由 `forwardSearchAfter` 驱动、「跳转目标与方式」由 `forwardSearch.*` 驱动、「PDF 定位」由 `build.filename`/`pdfDirectory` 兜底。

> 实际效果取决于本机是否安装 LaTeX 发行版与对应 PDF 阅读器，**待本地验证**。若无环境，可做「源码阅读型实践」：对照本节「驱动表」与 [Configuration.md:34-40](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L34-L40)、[Configuration.md:105-113](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L105-L113)、[Configuration.md:130-156](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L130-L156)，解释「为何 `forwardSearchAfter=true` 但 `forwardSearch.executable=null` 时跳转不会发生」。

#### 4.2.5 小练习与答案

**练习 1**：用户设了 `build.forwardSearchAfter = true`，但完全没配 `forwardSearch.*`。保存后会怎样？

> **参考答案**：编译会照常发生（前提是 `onSave=true`），但「编译后跳转」这一步**不会真正打开任何 PDF**。因为 `forwardSearch.executable` 默认 `null`（见 [Configuration.md:130-138](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L130-L138)），正向搜索找不到要调用的阅读器。在 LSP 层这对应 `ForwardSearchStatus = Unconfigured`（详见 [u4-l1](u4-l1-custom-lsp-messages.md)）。结论：`forwardSearchAfter` 必须与 `forwardSearch.*` 配套。

**练习 2**：`build.args` 里有 `%f`，`forwardSearch.args` 里有 `%f`/`%p`/`%l`。为什么 `build.args` 没有 `%p`/`%l`？

> **参考答案**：占位符按「动作需要什么」分配。编译（`build.args`）只需要知道「**编译哪个 TeX 文件**」，所以只有 `%f`。正向搜索（`forwardSearch.args`）需要「跳到 PDF 的哪一行」，所以需要 `%p`（PDF 路径）与 `%l`（行号），外加 `%f`（TeX 路径，供 SyncTeX 做源-PDF 映射）。见 [Configuration.md:24-26](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L24-L26) 与 [Configuration.md:141-156](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L141-L156)。

**练习 3**：把编译产物 PDF 改名为 `report.pdf` 后，正向搜索跳到了错误文件（或找不到）。该用哪一项修复？为什么？

> **参考答案**：用 `texlab.build.filename`。见 [Configuration.md:105-113](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L105-L113)：它的注释明确说「用于在正向搜索时找到正确的 PDF 文件」，默认 `null` 时 texlab 按 `foo.tex → foo.pdf` 推断；改名后推断失败，需用 `build.filename` 显式指定。

---

### 4.3 SyncTeX 概念：编辑器与 PDF 的双向跳转

#### 4.3.1 概念说明

前两节反复提到「正向搜索靠 SyncTeX」「阅读器要支持 SyncTeX」。本节把这个概念讲透。

**SyncTeX 是什么**：它是 TeX 引擎提供的一项「**源代码与 PDF 位置同步**」机制。打开 `-synctex=1` 编译时，引擎会额外产出一个 `.synctex.gz` 文件，里面是一张「源文件 + 行号 ↔ PDF 页 + 坐标」的对照表。有了这张表，才能在「源码第 42 行」和「PDF 第 3 页中上部」之间互相对应。

为什么这件事需要单独的机制？因为 TeX 排版高度非线性——源码里第 42 行的内容，经过断行、分页、浮动体摆放后，可能落在 PDF 第 3 页，也可能被推到第 5 页。**没有 SyncTeX 的对照表，你无法可靠地从一个世界跳到另一个世界**。

由此分出两个方向：

- **正向搜索（forward search）**：编辑器 → PDF。光标在源码某行，借助 `.synctex.gz` 查出对应的 PDF 位置，让阅读器跳过去。**由 `texlab.forwardSearch.*` 配置驱动**（4.2 已讲）。
- **逆向搜索（inverse search）**：PDF → 编辑器。在 PDF 里点击，借助 `.synctex.gz` 查出对应的源码行，让编辑器跳过去。**由 PDF 阅读器回调 `texlab inverse-search` 子命令驱动**（本节讲）。

#### 4.3.2 核心流程

两个方向走的链路完全不同，画在一起对比：

```text
┄┄┄┄┄┄┄┄┄┄┄ 正向搜索（编辑器 → PDF）┄┄┄┄┄┄┄┄┄┄┄
  编辑器（光标在某行）
      │
      ▼  texlab 用 .synctex.gz 把「行」映射到「PDF 位置」
  texlab 调用 forwardSearch.executable + forwardSearch.args
  （%f=TeX 路径, %p=PDF 路径, %l=行号 被替换）
      │
      ▼
  PDF 阅读器打开 PDF 并跳到对应位置

┄┄┄┄┄┄┄┄┄┄┄ 逆向搜索（PDF → 编辑器）┄┄┄┄┄┄┄┄┄┄┄
  用户在 PDF 阅读器里点击某处
      │
      ▼  阅读器用 .synctex.gz 把「PDF 位置」映射回「源文件+行」
  阅读器执行配置好的回调命令：
      texlab inverse-search --input <FILE> --line <LINE>
      │
      ▼
  texlab 通过 window/showDocument 请求，让编辑器跳到该 <FILE>:<LINE>
```

关键点：

1. **两个方向都需要 `.synctex.gz`**。而这个文件是由编译时 `build.args` 里的 `-synctex=1` 产生的（[u2-l2](u2-l2-build-config.md) 已指出它在默认 `args` 里）。所以「关掉 `-synctex=1`」会让**双向跳转一起失效**。
2. **逆向搜索是「PDF 阅读器 → texlab → 编辑器」三段式**。阅读器需要被配置成「点击时执行 `texlab inverse-search ...`」（具体怎么配因阅读器而异，[u3-l2](u3-l2-synctex-viewers.md) 逐家给出）；`texlab inverse-search` 收到后，再通过标准 LSP 的 `window/showDocument` 请求让编辑器跳转。
3. **正向搜索可以是「点一下跳一次」，也可以是「编译后自动跳」**。后者正是 4.2 的 `forwardSearchAfter`。

> **为何不用 latexmk `-pvc`？** 这是本节最容易被问、也最容易踩坑的问题。Previewing.md 给出了明确理由：texlab **不会被 latexmk 通知**文档何时编译完成（[Previewing.md:13-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L13-L15)）。`-pvc` 让 latexmk 自己在后台一遍遍监视、编译，但整个过程对 texlab 是**黑盒**——texlab 不知道某次编译什么时候结束、成功与否，于是「编译后自动跳 PDF」（`forwardSearchAfter`）这类动作就**无法可靠触发**。官方因此推荐：把「**编译本身**」交给 texlab 主导（用 `build.onSave`），而不是交给 latexmk 的 `-pvc`。
>
> 注意区分三个相近的 latexmk flag：
>
> | flag | 含义 | 与 texlab 的关系 |
> | --- | --- | --- |
> | `-pv` | 编译后预览（编译**一次**后打开 PDF） | 可加进 `build.args`，作为简单的「编译后开 PDF」 |
> | `-pvc` | 持续监视、反复编译并预览 | **不推荐**，因为 texlab 感知不到编译事件 |
> | 无 | 普通编译 | 配合 `build.onSave` 由 texlab 主导，是官方推荐姿势 |

#### 4.3.3 源码精读

- [Previewing.md:9-12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L9-L12)：说明「让 PDF 阅读器与编辑器光标同步」需要启用 **SyncTeX** 并更新 `texlab.forwardSearch` 配置。
  - 明确了正向搜索的两个前提：SyncTeX 已启用（即 `-synctex=1` 有产出 `.synctex.gz`）、`forwardSearch.*` 已配。

- [Previewing.md:13-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L13-L15)：**不建议用 `-pvc`** 的官方理由——「texlab 不会被 latexmk 通知文档何时编译完成」，并给出替代方案「改用 `texlab.build.onSave`」。
  - 这是本讲反复强调的那条原则的**原始出处**。

- [Previewing.md:6-8](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L6-L8)：给出 latexmk 用户的「简单预览」方案——写 `.latexmkrc` 并在 `texlab.build.args` 里加 `-pv` flag。
  - 这是一种**不依赖** `forwardSearchAfter` 的轻量预览方式（编译一次后开 PDF），适合不需要「自动跟随光标」的场景。

- [Previewing.md:21-22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L21-L22)：逆向搜索的入口——若编辑器支持 `window/showDocument` 请求，可用 `texlab inverse-search --input <FILE> --line <LINE>` 把逆向搜索转发给编辑器。
  - 这揭示了逆向搜索的三段式本质：阅读器 → `texlab inverse-search` → `window/showDocument` → 编辑器跳转。

- [Configuration.md:130-133](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L130-L133)：`forwardSearch.executable` 注释里明确「预览器需要**支持 SyncTeX**」。
  - 这是挑选 PDF 阅读器的硬约束：不支持 SyncTeX 的阅读器（如会锁文件的 Adobe Reader）无法用于正向搜索（Previewing.md 还特别推荐 Windows 上用 SumatraPDF，正因为 Adobe Reader 会锁住 PDF 阻止后续编译）。

- [Configuration.md:15-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L15-L30)：`build.args` 默认值含 `-synctex=1`。
  - 这是 `.synctex.gz` 能自动产出的原因。换构建工具时（如 tectonic，见 [u3-l3](u3-l3-tectonic-engine.md)），要确保新工具的等价开关仍打开，否则双向跳转失效。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`.synctex.gz` 是双向跳转的命脉」——把它去掉，看双向跳转怎么失效；再体验一次逆向搜索。

**操作步骤**：

1. 在 4.2 已配好正向搜索的基础上，确认编译产物目录里**有** `main.synctex.gz`（或 `.synctex.gz` 结尾的文件）。
2. **正向搜索自测**：光标移到 `main.tex` 某行，触发正向搜索（保存让 `forwardSearchAfter` 自动跳，或手动触发），确认 PDF 跳到对应位置。
3. **逆向搜索自测**（以 Zathura 为例，需先配好 `zathurarc`，详见 [u3-l2](u3-l2-synctex-viewers.md)）：在 PDF 里 `Ctrl+Click` 某处，确认编辑器跳回对应源码行。
4. **破坏性实验**：临时把 `build.args` 里的 `-synctex=1` 去掉，重新编译，确认产物里**没有** `.synctex.gz`；再次尝试正向/逆向搜索。
5. （可选）把 `-synctex=1` 加回去，恢复正常。

**需要观察的现象**：

- 第 2、3 步：双向跳转正常工作。
- 第 4 步：去掉 `-synctex=1` 后，没有 `.synctex.gz`，正向搜索「跳不准或跳不动」，逆向搜索也无法定位到正确源码行。

**预期结果**：你直观验证了 SyncTeX 是双向跳转的**基础设施**——而它是否启用，完全由 `build.args` 里的 `-synctex=1` 决定。这也解释了为什么官方推荐用 texlab 主导编译（`onSave`）而非 latexmk `-pvc`：只有 texlab 主导，它才能保证 `.synctex.gz` 已就绪、并在编译完成后可靠地触发跳转。

> 逆向搜索能否真正跳回编辑器，取决于你的编辑器是否支持 `window/showDocument` 请求，以及 PDF 阅读器是否允许配置回调命令，**待本地验证**。若环境不全，可做「源码阅读型实践」：对照 [Previewing.md:21-22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L21-L22) 与 [Configuration.md:130-133](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L130-L133)，解释「为何换一个不支持 SyncTeX 的 PDF 阅读器后，正向搜索会失败」。

#### 4.3.5 小练习与答案

**练习 1**：用户抱怨「正向搜索时灵时不灵」。你怀疑是 `.synctex.gz` 的问题。该怎么排查？

> **参考答案**：先确认 `build.args` 里**始终带** `-synctex=1`（默认值就有，见 [Configuration.md:15-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L15-L30)）；再确认编译产物目录里确实有 `.synctex.gz`。若换了构建工具（如 tectonic）却没带等价开关，或产物目录配置不一致（`pdfDirectory` 与实际输出对不上），都会导致 `.synctex.gz` 缺失或找不到，跳转自然失效。

**练习 2**：正向搜索与逆向搜索，各自由谁「发起」？

> **参考答案**：
> - **正向搜索**由 **texlab**（应编辑器请求或 `forwardSearchAfter`）发起，调用 `forwardSearch.executable` + `args` 打开 PDF。
> - **逆向搜索**由 **PDF 阅读器**发起——用户在 PDF 点击，阅读器执行配置好的 `texlab inverse-search --input <FILE> --line <LINE>`（见 [Previewing.md:21-22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L21-L22)），texlab 再通过 `window/showDocument` 让编辑器跳转。

**练习 3**：为什么官方推荐用 `build.onSave` 而非 latexmk `-pvc`？用一句话说清。

> **参考答案**：因为 texlab 不会被 latexmk 通知文档何时编译完成（[Previewing.md:13-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Previewing.md#L13-L15)）；用 `-pvc` 时编译在 latexmk 黑盒里进行，texlab 无法在「编译完成」这个时机可靠地触发 `forwardSearchAfter`。改用 `onSave` 让编译由 texlab 主导，它才能在编译结束后准确衔接跳转。

---

## 5. 综合实践

把本讲三个模块串成一个完整的「**配置 + 观测 + 解释**」任务。

**任务**：为你的环境配置出一条「**保存即编译、编译后自动跳 PDF、且能从 PDF 点回源码**」的双向同步工作流，并用一张表解释每一步的驱动项。

**操作步骤**：

1. **确认前置**：本机已装 LaTeX 发行版（含 `latexmk`）和一个支持 SyncTeX 的 PDF 阅读器（Windows: SumatraPDF；Linux: Zathura；macOS: Skim）。
2. **写出完整配置**（以 Linux/Zathura 为例，阅读器配方取自 [u3-l2](u3-l2-synctex-viewers.md)，可按你的平台替换）：
   ```jsonc
   // 示例配置：完整的保存→编译→跳转 + 逆向搜索工作流
   {
     "texlab.build.onSave": true,                       // ① 保存触发编译
     "texlab.build.forwardSearchAfter": true,           // ② 编译后自动跳 PDF
     // build.executable / build.args 保持默认即可（默认 latexmk + -synctex=1）
     "texlab.forwardSearch.executable": "zathura",      // ③ 正向搜索用哪个阅读器
     "texlab.forwardSearch.args": [
       "--synctex-forward", "%l:1:%f", "%p"
     ]
     // 逆向搜索还需在阅读器侧配置回调 texlab inverse-search（见 u3-l2）
   }
   ```
3. **正向链路自测**：在 `main.tex` 靠后的某行输入文字，保存，观察「编译 → PDF 自动翻到该行」。
4. **逆向链路自测**：在 PDF 里点击某处（Zathura 为 `Ctrl+Click`），观察编辑器是否跳回对应源码行。
5. **填驱动表**：仿照 4.2.4 的表格，把你能观测到的每个现象，标注上「由哪个配置项/命令驱动」。

**验收标准**：

- 能完整跑通「保存→编译→PDF 跳转」与「PDF 点击→源码跳转」两个方向。
- 能用一句话解释：为何这条链路里「`onSave`、`forwardSearchAfter`、`forwardSearch.*`、`-synctex=1`」**缺一不可**。
- 能说清：如果改用 latexmk `-pvc`，链路会在哪一环断裂、为什么。

> 完整双向同步依赖编辑器、发行版、PDF 阅读器三者协同，**待本地验证**。若仅做源码阅读，请依据本讲三处源码精读，画出「保存→编译→正向搜索」与「PDF 点击→`inverse-search`→`window/showDocument`→编辑器」两条链路图。

## 6. 本讲小结

- 编译有**两条触发路径**：编辑器主动发的自定义请求 `textDocument/build`（路径 A，同步、有 `BuildResult` 回执），与保存驱动的 `build.onSave`（路径 B，异步、由 texlab 内部衔接后续动作）。二者共用同一套 `build.executable` + `build.args`。
- `build.onSave` 与 `build.forwardSearchAfter` **默认都是 `false`**；「保存即编译」「编译后跳 PDF」都需要显式打开。
- `forwardSearchAfter` 把「编译」与「正向搜索」串成流水线，但它**必须**与 `forwardSearch.*`（阅读器 executable + args + `%f`/`%p`/`%l`）配套，否则无目标可跳；`build.filename` 则是「编译产物」与「正向搜索找 PDF」之间的桥。
- **SyncTeX** 通过 `.synctex.gz` 提供「源行 ↔ PDF 位置」对照表，是双向跳转的基础设施；该文件由 `build.args` 里的 `-synctex=1` 产出，去掉它双向跳转一起失效。
- **正向搜索**（编辑器→PDF）由 texlab 调用 `forwardSearch.*` 驱动；**逆向搜索**（PDF→编辑器）由阅读器回调 `texlab inverse-search`、再经 `window/showDocument` 驱动。
- 官方**不推荐 latexmk `-pvc`**，因为 texlab 感知不到 latexmk 的编译完成时机，`forwardSearchAfter` 无法可靠触发；推荐用 `build.onSave` 让编译由 texlab 主导。

## 7. 下一步学习建议

- **[u3-l2 SyncTeX 双向搜索与 PDF 阅读器集成](u3-l2-synctex-viewers.md)**：本讲只讲了 `forwardSearch.*` 的角色与逆向搜索的原理，但没给具体阅读器的命令行配方。下一讲会逐家给出 SumatraPDF / Evince / Okular / Zathura / qpdfview / Skim / Sioyek 的正向与逆向搜索配置，把你本讲的「骨架」填上「血肉」。
- **[u3-l3 使用 Tectonic 作为替代 TeX 引擎](u3-l3-tectonic-engine.md)**：本讲强调 `-synctex=1` 是双向跳转的前提。若你换用 tectonic，需要知道它的等价开关与产物目录如何与 `pdfDirectory`/`auxDirectory` 对齐——下一讲专门讲这个。
- **[u4-l1 自定义 LSP 消息：build 与 forwardSearch](u4-l1-custom-lsp-messages.md)**：本讲把 `textDocument/build` 与 `textDocument/forwardSearch` 当作工作流里的事实来用（路径 A 有回执、`Unconfigured` 状态等）。若你想为编辑器**亲手实现** texlab 客户端、或想看懂完整的请求/响应 JSON 结构（`BuildTextDocumentParams`、`BuildStatus`、`ForwardSearchStatus` 的全部取值），请进入 LSP 协议层那一讲。
