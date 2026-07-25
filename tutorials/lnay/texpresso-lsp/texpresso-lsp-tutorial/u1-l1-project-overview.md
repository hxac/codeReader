# 项目总览：texpresso-lsp 是什么

## 1. 本讲目标

本讲是整个学习手册的第一篇，目标是让你在「不写任何代码」的情况下，建立一个对 texpresso-lsp 这个项目的整体认知。读完本讲，你应该能够：

1. 用一两句话向别人解释 texpresso-lsp 是做什么的、为什么要做它。
2. 说出 TeXpresso 可执行文件和这个 LSP 服务器之间是什么关系（谁包谁、谁调用谁）。
3. 画出「编辑器 ⇄ LSP 服务器 ⇄ texpresso 子进程」三层架构的数据流图。
4. 对照 README 的功能清单，知道哪些功能已经实现、哪些还没实现。

本讲**不要求**你看懂每一行 TypeScript，重点是建立「地图感」。具体的协议细节、进程管理细节会在后续讲义中逐层展开。

---

## 2. 前置知识

本讲面向零基础读者，但有几个概念如果能先有个大概印象，会读得更顺。不熟悉的也没关系，下面会用大白话解释。

### 2.1 什么是 LaTeX，为什么需要「预览」

LaTeX 是一种用纯文本写文档（尤其是带公式的论文、书籍）的排版系统。你写的是 `.tex` 源文件，但最终看到的是排版好的 PDF。问题在于：从「改一行源码」到「看到 PDF 变化」中间通常要经过编译，这个过程慢且打断思路。所以编辑器都希望提供**实时预览**——你一改源码，旁边的 PDF 窗口立刻更新。

### 2.2 什么是「正反向搜索」（forward / inverse search）

这是 LaTeX 编辑器里两个经典功能：

- **正向搜索（forward search）**：光标在源码某一行 → 在 PDF 里高亮对应位置。
- **反向搜索（inverse search）**：在 PDF 里点击某个位置 → 跳回源码对应的那一行。

这两个功能依赖一个叫 **SyncTeX** 的工具来建立「源码行 ↔ PDF 位置」的对应关系。本项目的核心功能之一就是提供这种正反向搜索。

### 2.3 什么是 LSP（Language Server Protocol）

LSP（语言服务器协议）是编辑器和「语言服务器」之间通信的一套约定，最初由微软为 VS Code 设计，现在被几乎所有现代编辑器（VS Code、Zed、Neovim、Emacs 等）支持。

打个比方：LSP 就像餐厅里的「点菜单」。不管服务员（编辑器）是哪国人，只要大家都按同一份菜单（LSP 协议）写菜名，后厨（语言服务器）就能看懂。这样一来，**一个语言服务器可以被所有支持 LSP 的编辑器复用**，而不必每个编辑器都自己实现一遍。

本项目就是一个 LSP **服务器**（server 端）。它通过标准输入/输出（stdio）和编辑器通信。

### 2.4 什么是子进程（subprocess / child process）

一个程序（比如这里的 LSP 服务器）可以在运行时「再启动另一个程序」并和它通信，被启动的那个程序就叫子进程。本项目运行时会启动一个叫 `texpresso` 的外部程序作为子进程，由它真正去做渲染和预览，而 LSP 服务器只负责「翻译」消息。这就是后面会反复出现的「三层架构」的来源。

---

## 3. 本讲源码地图

本讲只涉及两个关键文件，但它们足以让你看清整个项目的全貌。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md) | 项目说明文档 | 项目定位、功能清单、运行方式 |
| [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) | LSP 服务器的全部主逻辑（唯一源码入口） | 三层架构在代码里长什么样 |

> 备注：项目源码非常精简，`src/` 下只有三个文件：`server.ts`（主逻辑）、`process-manager.ts`（管理 texpresso 子进程）、`types.ts`（类型定义）。后两个文件会在进阶讲义中精读，本讲只在「三层架构」里点到为止。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：项目背景与目的、功能清单、三层架构概览。

### 4.1 项目背景与目的

#### 4.1.1 概念说明

texpresso-lsp 要解决的问题是：**让任意支持 LSP 的编辑器都能用上 TeXpresso 提供的 LaTeX 实时预览与正反向搜索能力**。

这里有两层东西需要区分清楚：

1. **TeXpresso**：一个独立的可执行程序（C 语言写的），由社区开发者维护，擅长快速渲染 LaTeX 预览。它本身不是 LSP 服务器，只是个命令行工具。
2. **texpresso-lsp**（本项目）：一个用 TypeScript 写的「薄壳」LSP 服务器。它自己不做渲染，而是启动一个 TeXpresso 子进程，把编辑器发来的 LSP 消息翻译成 TeXpresso 能听懂的命令，再把 TeXpresso 产生的事件翻译回去。

项目作者在 README 里明确说了：这是一个**实验性**（experimental）的实现，是一个「便宜的、快速的 nodeJS 实现」，用来快速验证想法。但作者也认为，由于它很「薄」（JavaScript 几乎不承担什么重活，只负责 JSON 的解析和转发，而这正是 JS 擅长的事），所以这种实现「在最终形态上可能也是够用的」。

#### 4.1.2 核心流程

从「为什么存在」的角度，整个项目的动机可以概括为下面这条因果链：

1. TeXpresso 是个好工具，但它是个命令行程序，编辑器不能直接用。
2. 如果给它套一层 LSP，任何 LSP 编辑器都能即插即用。
3. 这一层（本项目）尽量薄：只做「消息翻译 + JSON 解析」，把重活留给 TeXpresso。

所以理解本项目，关键是理解它「不做的事」和「做的事」：

- **不做**：LaTeX 解析、PDF 渲染、SyncTeX 计算。
- **做**：LSP 协议接入、子进程管理、JSON 消息翻译与转发。

#### 4.1.3 源码精读

README 的 `## Purpose` 一节把项目定位说得很清楚：

[README.md#L11-L17](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L11-L17) —— 这段文字说明了三件事：

1. 这是一个 **experimental（实验性）** 的 LSP 服务器。
2. 它 **wrapping around**（包裹在……之外）TeXpresso 可执行文件——注意「包裹」这个词，它就是「薄壳」的同义词。
3. 它是一个 **cheap 的 nodeJS 实现**，但作者认为因为接口很薄，JS 只负责 JSON 解析，这种做法在性能和易用性上都合适，所以也可能成为最终方案。

在源码层面，这种「包裹」关系最直接的体现就是：LSP 服务器一启动，就立刻去创建一个管理 texpresso 子进程的对象。在 `server.ts` 的 `onInitialize`（LSP 初始化握手）里可以看到：

[src/server.ts#L65-L70](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L70) —— 在 LSP 初始化阶段，服务器用配置里的 `texpresso_path` 创建一个 `texpressoProcessManager`，并立即 `await` 它启动（`start()`）。这一句就是「LSP 服务器包裹 texpresso 子进程」的代码落点。

> 这里的 `["-json", "-lines"]` 是传给 texpresso 可执行文件的命令行参数，告诉它「以 JSON 模式输出、并报告行信息」。具体参数含义会在进程管理讲义（u2-l2）中展开。

#### 4.1.4 代码实践

**实践目标**：体会「薄壳」设计——确认 LSP 服务器本身没有实现任何 LaTeX 渲染逻辑。

**操作步骤**：

1. 打开 [README.md#L11-L17](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L11-L17)，找到作者描述项目是「thin interface」的那段话。
2. 在 [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) 全文里搜索（用编辑器的查找功能）是否有任何「渲染」「pdf」「dvi」「tex 解析」相关的实现。你会发现：**几乎没有**。整份文件里出现的 `spawn`（启动外部程序）调用才是它真正「干活」的地方。

**需要观察的现象**：`server.ts` 里出现 `spawn` 的地方，都是去启动**别的**程序（texpresso 本体、texpresso-tonic、编辑器命令），而不是自己处理 LaTeX。

**预期结果**：你会确认「这个项目自己一行 LaTeX 都没渲染」，从而理解为什么作者说它是「thin interface」。这是后续所有讲义的基础认知。

#### 4.1.5 小练习与答案

**练习 1**：如果有一天 TeXpresso 可执行文件被改名或换成了另一个渲染引擎，texpresso-lsp 的哪些部分需要改？

> **参考答案**：主要改两处——配置里 `texpresso_path` 的默认值（[src/server.ts#L20-L27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L20-L27) 里 `defaultInitOpts` 的 `texpresso_path`），以及传给它的命令行参数（如 `["-json", "-lines"]`）。由于是「薄壳」设计，渲染逻辑本身不在这个项目里，所以改动量很小。

**练习 2**：README 说本项目是「cheap 的 nodeJS 实现」，但又说它「可能也 fit for purpose」。这两个说法矛盾吗？

> **参考答案**：不矛盾。「cheap」指它写得快、是快速验证用的；「fit for purpose」指因为接口很薄、JS 只做 JSON 解析（这是 JS 擅长的），所以即便作为正式方案，性能和工程上也站得住。前者谈开发成本，后者谈运行时适用性。

---

### 4.2 已实现与未实现的功能清单

#### 4.2.1 概念说明

README 开头用一个清单（checkbox）列出了项目目前的功能状态。这是你了解「这个项目现在能干什么、不能干什么」最快的方式。三个功能分别是：

- **Live preview（实时预览）**：改源码后预览窗口自动刷新。✅ 已实现。
- **Inverse search（反向搜索）**：在 PDF 点击 → 跳回源码行。✅ 已实现。
- **Forward search following cursor（跟随光标的正向搜索）**：光标移动 → PDF 跟着高亮到对应位置。⬜ 尚未完成。

注意第三个功能虽然标了「未实现」，但代码里其实已经有了**部分**实现（借用了 `documentHighlight` 请求来触发），只是 README 还把它当作未完成项。这种「README 与代码不完全同步」的情况在实验性项目里很常见，读源码时要留心。

#### 4.2.2 核心流程

三个功能分别对应不同的代码触发点，理解它们的「入口」就抓住了主线：

| 功能 | 触发方式 | 代码入口（server.ts） |
| --- | --- | --- |
| 实时预览 | 文档保存 `onDidSave` → 启动 `texpresso-tonic` 编译 → 退出后 `rescan` | `documents.onDidSave` |
| 反向搜索 | texpresso 发来 `synctex` 事件 → 拼接命令启动编辑器 | `texpressoProcess.on("synctex", ...)` |
| 正向搜索 | 编辑器请求 `onDocumentHighlight` → 发 `synctex-forward` 命令 | `connection.onDocumentHighlight` |

这张表先有个印象即可，每个入口的具体逻辑会在进阶/专家讲义里精读。本讲只需要知道「这三个功能分别从哪里进来」。

#### 4.2.3 源码精读

README 顶部的功能清单：

[README.md#L5-L9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L5-L9) —— 三个 checkbox：前两个是 `[x]`（完成），第三个是 `[ ]`（未完成）。

下面把这三个功能在 `server.ts` 里的代码落点一一对应起来，让你体会「功能清单」和「代码」是如何对应的。

**① 实时预览**（保存即重新编译）：

[src/server.ts#L179-L196](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L179-L196) —— 当文档保存时，启动 `texpresso-tonic`（编译器）重新编译；编译进程退出后，给 texpresso 发一条 `rescan` 命令让它刷新预览。这就是「实时预览」的完整链路（防重入标志 `is_texpresso_tonic_running` 先忽略，进阶讲义会讲）。

**② 反向搜索**（PDF → 源码）：

[src/server.ts#L95-L110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L95-L110) —— 监听 texpresso 发来的 `synctex` 事件，取出文件路径和行号，用配置里的 `inverse_search` 命令（默认是 `zed %f:%l`）替换掉占位符后，用 `spawn` 启动编辑器跳到对应位置。

**③ 正向搜索**（源码 → PDF，跟随光标）：

[src/server.ts#L229-L268](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L229-L268) —— 巧妙地「借用」了编辑器的 `documentHighlight`（文档高亮）请求作为触发器：每次编辑器请求高亮时，服务器其实不去算高亮，而是把当前光标位置通过 `synctex-forward` 命令发给 texpresso，让 PDF 跳到对应位置。函数末尾返回空数组 `[]`（不给真正的高亮）。这个「借用」设计的副作用和取舍会在专家讲义（u3-l2）里讨论。

#### 4.2.4 代码实践

**实践目标**：把 README 的功能清单和 `server.ts` 的代码入口手动对一遍，建立「文档 ↔ 代码」的对应能力。

**操作步骤**：

1. 打开 [README.md#L5-L9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L5-L9)。
2. 对每一个功能，去 `server.ts` 找到上表列出的代码入口，读一读那个函数（不要求看懂细节，看懂「它在做什么事」即可）。
3. 特别注意第三个「正向搜索」：README 说它未实现，但 [src/server.ts#L229-L268](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L229-L268) 里明明有 `synctex-forward` 的发送逻辑。

**需要观察的现象**：你会发现 README 的 checkbox 和代码实际状态存在落差——正向搜索其实有「半成品」实现。

**预期结果**：你能用自己的话解释「为什么 README 标了未实现，但代码里却有正向搜索的代码」。一种合理推测是：作者认为这个实现还达不到「跟随光标」的完整体验（比如它依赖 `documentHighlight` 这个副作用触发器，语义上不纯粹），所以暂不勾选。具体是否成立，待后续讲义结合上下文确认。

#### 4.2.5 小练习与答案

**练习 1**：反向搜索用的是哪个外部命令？占位符 `%f` 和 `%l` 分别代表什么？

> **参考答案**：默认命令是 `zed`（见 [src/server.ts#L20-L27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L20-L27) 的 `defaultInitOpts.inverse_search`）。`%f` 代表文件路径（file），`%l` 代表行号（line）。在 [src/server.ts#L102-L105](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L102-L105) 里会被替换成真实的路径和行号。

**练习 2**：正向搜索的代码已经存在，README 却没勾选。请列出至少一个可能的原因。

> **参考答案**：可能原因——它「借用」了 `documentHighlight` 请求来触发（[src/server.ts#L229-L268](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L229-L268)），语义上不纯粹，作者可能认为还不算「正式的跟随光标正向搜索」；也可能存在行为不稳定、未充分测试等问题。

---

### 4.3 三层架构概览

#### 4.3.1 概念说明

理解本项目最重要的一个心智模型，就是下面这张**三层架构**图：

```
┌──────────────┐   LSP / JSON-RPC（基于 stdio）   ┌──────────────────────┐   自定义 JSON 行协议（stdin/stdout）   ┌─────────────────────┐
│              │ ──────────────────────────────▶ │                      │ ───────────────────────────────────▶ │                     │
│   编辑器      │                                  │  texpresso-lsp       │                                       │  texpresso 子进程    │
│ (LSP 客户端)  │                                  │  (TypeScript/Node)   │                                       │  (TeXpresso 本体)    │
│ Zed/VSCode…  │ ◀────────────────────────────── │   「翻译官 + 薄壳」    │ ◀────────────────────────────────── │  + texpresso-tonic   │
└──────────────┘        日志/通知/事件              └──────────────────────┘          JSON 事件（synctex 等）            └─────────────────────┘
```

三层各自的职责：

1. **第一层：编辑器（LSP 客户端）**——例如 Zed、VS Code。它按 LSP 协议向服务器发送「打开文档、改动、保存、光标高亮请求」等消息，并接收服务器返回的通知/日志。
2. **第二层：texpresso-lsp（本项目）**——中间的「翻译官」。它接收 LSP 消息，翻译成 texpresso 私有的 JSON 命令发给子进程；反过来，把子进程吐出的 JSON 事件翻译成编辑器能理解的动作（比如启动编辑器跳转）。
3. **第三层：texpresso 子进程**——真正干活的。包括 `texpresso` 本体（渲染预览、计算 SyncTeX）和 `texpresso-tonic`（负责编译）。

为什么是三层而不是两层？因为 LSP 协议（编辑器说的语言）和 texpresso 的私有协议（子进程说的语言）**不是同一种语言**，中间需要一个翻译。这就是本项目存在的根本理由。

#### 4.3.2 核心流程

用一条「用户保存文档」的故事线，把三层串起来看数据是怎么流动的：

1. 用户在编辑器里按保存。
2. **编辑器 → LSP 服务器**：编辑器通过 LSP 发送 `textDocument/didSave` 通知。
3. **LSP 服务器内部**：`server.ts` 的 `documents.onDidSave` 被触发，启动 `texpresso-tonic` 子进程重新编译。
4. **LSP 服务器 → texpresso 子进程**：编译结束后，服务器用 `sendCommand("rescan", [])` 给 texpresso 发一条 JSON 命令。
5. **texpresso 子进程**：重新扫描、刷新预览窗口。
6. （反向时）**texpresso 子进程 → LSP 服务器**：texpresso 在 stdout 输出一行 JSON（如 `["synctex", path, line]`），被服务器解析成事件。
7. **LSP 服务器 → 编辑器（间接）**：服务器收到反向搜索事件后，用 `spawn` 直接启动编辑器命令（如 `zed file.tex:42`）跳到对应位置。

注意第 6、7 步：从 texpresso 回到编辑器，**并没有走 LSP**，而是服务器直接 `spawn` 了一个编辑器进程。这是本项目架构上比较特别的一点，理解它有助于你后面读代码时不困惑。

数据流方向速记：

- 编辑器 ⇄ LSP 服务器：**LSP 协议**（双向）。
- LSP 服务器 → texpresso：**JSON 命令**（写 stdin）。
- texpresso → LSP 服务器：**JSON 事件**（读 stdout）。
- LSP 服务器 → 编辑器（反向跳转）：**直接 spawn 编辑器命令**（不走 LSP）。

#### 4.3.3 源码精读

下面把三层架构在 `server.ts` 里的「接线点」逐一指出来。

**第二层（LSP 服务器）的诞生**：通过 `vscode-languageserver` 库创建一个标准 LSP 连接，并让文档管理器监听它。

[src/server.ts#L33-L44](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L33-L44) —— 用 `createConnection(ProposedFeatures.all)` 建立一个基于 stdio 的 LSP 连接，`new TextDocuments(TextDocument)` 创建文档管理器，再 `documents.listen(connection)` 让文档事件挂到连接上。这一段就是「第二层接入第一层」的代码。

**第二层 ⇄ 第三层（与 texpresso 子进程）的接线**：在 `onInitialize` 里创建进程管理器并启动。

[src/server.ts#L65-L71](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L71) —— `new TexpressoProcessManager(...)` 创建并 `await start()` 启动 texpresso 子进程。这就是「第二层接入第三层」的代码。注意它发生在 `onInitialize`（LSP 握手）阶段——也就是说，编辑器一连接，texpresso 子进程就被拉起来了。

**第二层 → 第三层（发命令）的典型调用**：保存后发 `rescan`。

[src/server.ts#L190-L195](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L190-L195) —— `texpressoProcess.sendCommand("rescan", [])`，把一条 JSON 命令写进 texpresso 的 stdin。`sendCommand` 的具体实现（把 `[command, ...data]` 序列化成 JSON 并加换行）在 `process-manager.ts`，进阶讲义（u2-l3）会精读。

**第三层 → 第二层（收事件）的典型调用**：监听 `synctex`。

[src/server.ts#L95-L110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L95-L110) —— `texpressoProcess.on("synctex", ...)` 注册对 texpresso 事件的监听。texpresso 在 stdout 吐出的 JSON 行会被 `process-manager.ts` 解析后 `emit` 成事件，这里再消费。

**启动整个服务器**：最后一行。

[src/server.ts#L281](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L281) —— `connection.listen()` 让 LSP 服务器开始在 stdio 上监听编辑器的消息。这一行是整个程序的「发车」动作。

> 入口链路补充：`package.json` 的 `"bin": "bin/texpresso-lsp.sh"` 指向 [bin/texpresso-lsp.sh](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh)，这个脚本只有一行 `require('../dist/server.js')`，即加载编译后的 `server.ts`。所以「编辑器执行 `texpresso-lsp --stdio`」最终跑的就是本文件。这条入口链路在 u1-l3 会详细拆解。

#### 4.3.4 代码实践

**实践目标**：亲手画出三层架构的数据流图，把抽象的「三层」变成你自己的产出。

**操作步骤**：

1. 拿一张纸或打开任意画图/笔记工具。
2. 画三个方框，分别标「编辑器」「texpresso-lsp」「texpresso 子进程」。
3. 在方框之间画箭头，并标注每条箭头**用什么协议/方式**通信（提示：LSP、JSON 命令写 stdin、JSON 事件读 stdout、直接 spawn 编辑器）。
4. 在每条箭头上至少写**一个**具体的例子消息，比如：
   - 编辑器 → LSP：`didSave`
   - LSP → texpresso：`["rescan"]` 或 `["synctex-forward", path, line]`
   - texpresso → LSP：`["synctex", path, line]`
   - LSP → 编辑器（反向）：`spawn("zed", ["file.tex:42"])`

**需要观察的现象**：画完你会发现，有一条「回路」很特别——从 texpresso 回到编辑器的那条，并没有走 LSP 协议，而是 LSP 服务器直接启动了编辑器进程。

**预期结果**：你得到一张和 4.3.1 类似、但由你自己标注了具体消息的数据流图。这张图建议保留，后续每读一篇讲义都可以往上补充细节。

> 如果无法确定某个消息的确切格式（比如 `sendCommand` 到底序列化成什么样的字符串），可以先标「待确认」，等到 u2-l3「JSON 行协议」讲义再回来填。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能让编辑器直接和 texpresso 子进程通信，非要中间加一层 LSP 服务器？

> **参考答案**：因为两者说的「语言」不同——编辑器说的是 LSP 协议，texpresso 说的是它自己的 JSON 行协议。中间这层负责翻译。另外，LSP 是被众多编辑器广泛支持的标准，加这层之后所有 LSP 编辑器都能复用，而不必每个编辑器都单独适配 texpresso。

**练习 2**：在「反向搜索」流程里，从 texpresso 回到编辑器走的是哪条路？为什么不是 LSP？

> **参考答案**：走的是「LSP 服务器直接 `spawn` 编辑器命令」（见 [src/server.ts#L109](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L109) 的 `spawn(command, subs_args)`）。没有走 LSP，是因为 LSP 协议本身没有「让编辑器跳转到某个文件某一行」的标准方法，所以服务器干脆直接以命令行方式启动编辑器。

**练习 3**：`texpresso` 子进程是在什么时刻被启动的？

> **参考答案**：在 LSP 的 `onInitialize` 握手阶段就被启动了（[src/server.ts#L65-L71](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L71)）。也就是说编辑器一连接、握手完成，texpresso 子进程就已经在跑了。

---

## 5. 综合实践

本讲的综合实践把三个模块串起来，请你完成一份**「texpresso-lsp 项目认知卡片」**，包含两部分：

**第一部分：数据流图。** 基于 4.3.4 的实践，画出「编辑器 / texpresso-lsp / texpresso 子进程」三者之间的数据流图，要求：

- 标出三层各自的名称和职责。
- 标出每条通信路径用的协议/方式。
- 至少为每条路径写一个**来自真实源码**的例子消息（可参考 4.3.4 列出的例子，并到 `server.ts` 里核对）。

**第二部分：功能清单核对表。** 列出当前已实现和未实现的功能，并各写一句「它在代码哪里」的说明：

| 功能 | 状态 | 代码位置（server.ts） | 一句话说明 |
| --- | --- | --- | --- |
| 实时预览 | ✅ 已实现 | `documents.onDidSave`（L179 起） | 保存时启动 tonic 编译，退出后 rescan 刷新预览 |
| 反向搜索 | ✅ 已实现 | `texpressoProcess.on("synctex", ...)`（L95 起） | 收到 synctex 事件后 spawn 编辑器跳转 |
| 正向搜索（跟随光标） | ⬜ 未完成（有半成品） | `connection.onDocumentHighlight`（L229 起） | 借用高亮请求触发 synctex-forward |

完成后，建议把这张卡片保存下来——它是你后续阅读所有讲义时的「总索引图」。

> **关于运行验证**：本讲是认知导向的，不要求你真正跑起来项目。如果你确实想本地运行并观察日志，可以参考 README 的 Setup 章节；但运行与环境搭建（Node、TeXpresso 可执行文件、编辑器配置）属于下一讲 u1-l2 的内容，这里先不展开。

---

## 6. 本讲小结

- **texpresso-lsp 是什么**：一个用 TypeScript 写的、实验性的 LSP 服务器，它「包裹」TeXpresso 可执行文件，让任何 LSP 编辑器都能用上 LaTeX 实时预览与正反向搜索。
- **它为什么是「薄壳」**：JS 只负责 LSP 接入和 JSON 解析转发，真正的渲染/编译/SyncTeX 计算都交给 texpresso 子进程，所以叫 thin interface。
- **功能现状**：实时预览、反向搜索已实现；跟随光标的正向搜索 README 标为未完成，但代码里已有基于 `documentHighlight` 的半成品实现。
- **三层架构**：编辑器（LSP 客户端）⇄ texpresso-lsp（翻译官）⇄ texpresso 子进程（干活的人）。中间层用两种不同的语言分别和两端对话。
- **通信方式**：编辑器⇄服务器走 LSP；服务器→texpresso 走 JSON 命令写 stdin；texpresso→服务器走 JSON 事件读 stdout；反向跳转时服务器直接 spawn 编辑器命令（不走 LSP）。
- **入口**：编辑器执行 `texpresso-lsp --stdio`，最终运行的是 `server.ts` 编译后的 `dist/server.js`，其 `connection.listen()` 让服务器开始工作。

---

## 7. 下一步学习建议

本讲建立了整体地图，接下来建议：

1. **先动手把项目跑起来**：进入下一讲 **u1-l2《构建、运行与编辑器集成》**，学习 `npm install` / `npm run build` / `npm start` 的用法，以及如何配置编辑器连接 `texpresso-lsp --stdio`。亲眼看到日志输出，会让你对三层架构的感受更深。
2. **再看目录与入口链路**：**u1-l3《目录结构与入口文件链路》** 会拆解 `package.json` / `tsconfig.json` / `bin/texpresso-lsp.sh` 之间的调用关系。
3. **建立 LSP 心智模型**：**u1-l4《LSP 基础与连接建立》** 会精读 `server.ts` 顶部的 `createConnection`、`onInitialize` 和 `capabilities`，为进阶讲义打基础。

建议阅读的源码（按顺序）：

- [README.md](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md)（本讲已读，可重读 Setup 一节）
- [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts)（本讲已建立整体印象，后续逐段精读）
- [package.json](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json)（下一讲重点）
