# workspace/executeCommand 工作区命令

## 1. 本讲目标

在 [u4-l1](u4-l1-custom-lsp-messages.md) 里我们学了 texlab 的两条**自定义请求**（`textDocument/build`、`textDocument/forwardSearch`）。它们是 texlab「自己发明」的方法名，靠 `experimental` capability 声明。本讲换一条路：texlab 还通过 LSP **标准方法** `workspace/executeCommand` 暴露了六条工作区命令。学完本讲你应当能够：

1. 说出 `workspace/executeCommand` 与自定义请求的区别，并解释「**所有命令至多接收一个参数**」这条约定为什么能简化客户端实现。
2. 掌握六条命令（`cleanAuxiliary`、`cleanArtifacts`、`changeEnvironment`、`findEnvironments`、`showDependencyGraph`、`cancelBuild`）各自的用途、参数类型与返回类型。
3. 理解 `findEnvironments` 返回的 `EnvironmentLocation`（`name` + `fullRange`）结构，以及它与 `changeEnvironment` 的分工。
4. 学会用 `showDependencyGraph` 把项目依赖树导出成 DOT 文本并渲染成图片，用 `cancelBuild` 取消 `onSave` 触发的构建来排查问题。

## 2. 前置知识

本讲属于 **advanced** 层级，默认你已经读过：

- **[u1-l2 项目识别与根目录检测](u1-l2-project-detection.md)**：本讲的 `showDependencyGraph` 输出的正是 u1-l2 里 Discovery 算法构建出来的**依赖树**。如果你还不清楚「项目」「依赖树」「根目录」是什么，请先读 u1-l2。
- **[u2-l2 构建配置 texlab.build.\*](u2-l2-build-config.md)**：`cleanAuxiliary`/`cleanArtifacts` 要清理的目录由 `texlab.build.auxDirectory` 等产物目录配置决定；`cancelBuild` 取消的正是 `texlab.build.onSave` 触发的构建。
- **[u4-l1 自定义 LSP 消息](u4-l1-custom-lsp-messages.md)**：本讲会反复对比「标准 `workspace/executeCommand`」与「自定义请求」，并承接其中的 `BuildStatus.Cancelled`。

复习几个关键术语：

- **LSP（语言服务器协议）**：编辑器（客户端）与 texlab（服务器）之间的 JSON-RPC 通信标准。
- **`workspace/executeCommand`**：LSP 的一个**标准方法**，客户端通过它「按名字」触发服务器端预注册的命令。
- **TeX 辅助文件（auxiliary files）**与**产物（artifacts）**：编译 LaTeX 时，除了最终的 PDF，还会产生 `.aux`、`.log`、`.fls`、`.fdb_latexmk`、`.synctex.gz` 等中间文件；其中 PDF（以及某些工具的 `.ps`）属于「产物」。
- **环境（environment）**：LaTeX 里 `\begin{xxx} ... \end{xxx}` 包裹的代码块，`xxx` 是环境名。
- **DOT**：[Graphviz](https://graphviz.org/doc/info/lang.html) 的图描述语言，可以渲染成图片。

## 3. 本讲源码地图

本讲几乎完全围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| `Workspace-commands.md` | wiki 中专门描述六条工作区命令的页面，是本讲的唯一权威来源。 |
| `Project-Detection.md` | 仅作交叉引用：解释 `showDependencyGraph` 输出的依赖树是怎么来的（Discovery 算法）。 |
| `Configuration.md` | 仅作交叉引用：解释 `cleanAuxiliary` 用到的产物目录配置、`cancelBuild` 用到的 `onSave`。 |
| `LSP-Internals.md` | 仅作交叉引用：承接 u4-l1，理解 `cancelBuild` 与 `BuildStatus.Cancelled` 的关系。 |

> 提示：本仓库是纯文档 wiki，「源码」就是这些 `.md` 页面。本讲中出现的 TypeScript 类型定义（如 `EnvironmentLocation`）都是 wiki **原文给出的协议契约**，不是我们要去别处找的实现代码。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先讲清六条命令共用的**调用机制**（4.1），再把六条命令按职能分成三组精读——清理（4.2）、环境操作（4.3）、依赖图与取消（4.4）。

### 4.1 调用入口：workspace/executeCommand 与「至多一个参数」约定

#### 4.1.1 概念说明

LSP 提供了两种让客户端「驱动服务器做事」的方式，本讲要分清它们：

- **自定义请求**（u4-l1 学的 `textDocument/build`、`textDocument/forwardSearch`）：texlab 自己发明的方法名，需要客户端在 `initialize` 握手时用 `experimental` capability 显式声明支持。它们是「协议扩展」，新编辑器可能不认。
- **`workspace/executeCommand`**（本讲）：这是 LSP 的**标准方法**。服务器在初始化时上报一批「命令标识符」（command identifiers），客户端之后任何时候都可以用标准报文「按名字」触发它们。六条 `texlab.*` 命令就走这条标准通道。

一句话区分：自定义请求是 texlab **加进协议的新方法**，而 `workspace/executeCommand` 是 texlab **复用协议已有方法、在里面挂了六个名字**。后者对客户端更友好——只要编辑器实现了标准的 `workspace/executeCommand`，就能直接调用，无需任何特殊适配。

wiki 开篇一句话点明了这一点，并附带一条贯穿所有命令的关键约定：

> The server provides the some commands through the `workspace/executeCommand` request.
> …all commands take **at most one argument**, that is, the `arguments` array should contain **exactly one element or no elements** depending on the command.

这条「至多一个参数」约定是写客户端时的护栏：你不需要为每条命令设计复杂的参数数组，只要根据命令类型决定 `arguments` 里放 **0 个**还是 **1 个**元素即可。下文每条命令都会标注它属于哪一类。

#### 4.1.2 核心流程

客户端调用一条工作区命令，本质是发送一条标准 JSON-RPC 2.0 报文：

```text
客户端                                texlab 服务器
  │                                      │
  │  workspace/executeCommand            │
  │  { command: "texlab.<名字>",         │
  │    arguments: [...] }                │
  │ ───────────────────────────────────► │
  │                                      │  执行命令
  │  result: <该命令的返回类型>           │
  │ ◄─────────────────────────────────── │
```

伪代码报文如下（以不需要参数的 `showDependencyGraph` 为例）：

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "workspace/executeCommand",
  "params": {
    "command": "texlab.showDependencyGraph",
    "arguments": []
  }
}
```

`arguments` 是数组，但根据「至多一个参数」约定，它要么是 `[]`（无参命令），要么是 `[ <单个参数> ]`（单参命令）。**不要**往里塞多个元素。

#### 4.1.3 源码精读

wiki 的开篇语同时定义了「调用通道」与「参数约定」这两件事，是本模块的权威依据：

- [Workspace-commands.md:L1-L4](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L1-L4)：说明六条命令通过标准 `workspace/executeCommand` 暴露，并强调 `arguments` 数组只含 0 或 1 个元素。这段是后续每条命令的「公共前提」，读懂它就能套用到全部六条命令。

下表给出六条命令的「参数个数」速查，便于你在构造报文时快速决定 `arguments` 的长度：

| 命令 | 参数类型 | `arguments` 长度 |
| --- | --- | --- |
| `texlab.cleanAuxiliary` | `TextDocumentIdentifier` | 1 |
| `texlab.cleanArtifacts` | `TextDocumentIdentifier` | 1 |
| `texlab.changeEnvironment` | `ChangeEnvironmentParams`（含位置 + `newName`） | 1 |
| `texlab.findEnvironments` | `TextDocumentPositionParams`（含位置） | 1 |
| `texlab.showDependencyGraph` | 无 | 0 |
| `texlab.cancelBuild` | 无 | 0 |

#### 4.1.4 代码实践

**实践目标**：亲手构造一条 `workspace/executeCommand` 报文，体会「标准方法 + 命令名 + 至多一个参数」的结构。

**操作步骤**：

1. 打开任意支持 texlab 的编辑器（如 VS Code + LaTeX Workshop、Neovim）。
2. 在编辑器的输出/调试面板里找到 texlab 的 LSP 通信日志（VS Code：`Output` → 选语言服务器通道）。
3. 不实际触发命令，仅人工写出调用 `texlab.showDependencyGraph` 的请求报文（参考 4.1.2 的伪代码）。

**需要观察的现象**：你写出的 `method` 字段是固定的 `"workspace/executeCommand"`（标准方法），而 `command` 字段才是 `texlab.*`（命令名）。两者是分离的——方法名属于 LSP，命令名属于 texlab。

**预期结果**：能复述「`workspace/executeCommand` 是 LSP 标准方法，`texlab.*` 是挂在上面的命令名；`arguments` 数组长度只能是 0 或 1」。若你不确定报文格式，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：自定义请求 `textDocument/build` 与工作区命令 `texlab.cancelBuild`，对客户端而言哪一条「更标准」、更不需要额外适配？为什么？

> **参考答案**：`texlab.cancelBuild` 更标准。它走的是 LSP 标准方法 `workspace/executeCommand`，任何实现了该方法的编辑器都能调用；而 `textDocument/build` 是 texlab 自定义的方法名，客户端必须在 `initialize` 时声明 `experimental.textDocumentBuild` capability 才能用。

**练习 2**：某客户端在调用 `texlab.findEnvironments` 时把光标位置和文档标识塞进了 `arguments` 的两个数组元素里，这违反了什么约定？

> **参考答案**：违反了「所有命令至多接收一个参数」的约定。`findEnvironments` 的参数应是一个 `TextDocumentPositionParams` 对象，整体作为 `arguments` 的**单个元素**，而不是把字段拆成多个数组元素。

### 4.2 cleanAuxiliary / cleanArtifacts —— 清理辅助文件与产物

#### 4.2.1 概念说明

编译一次 LaTeX，磁盘上会多出一堆「中间文件」。texlab 提供两条命令来打扫它们，区别在于**清理到什么程度**：

- **`texlab.cleanAuxiliary`**：删除**辅助文件**（auxiliary files）——即编译过程中产生的中间产物（`.aux`、`.log`、`.fls`、`.fdb_latexmk`、`.synctex.gz` 等），但**保留最终产物**（PDF）。
- **`texlab.cleanArtifacts`**：删除辅助文件**以及产物**（artifacts）——也就是说连 PDF 也一并清掉，相当于「全部重来」。

这两条命令背后复用了 `latexmk` 自身的清理约定，这一点 wiki 写得很直白：

- `cleanAuxiliary` 等价于执行 `latexmk -c`（小写 `-c`：clean，保留 PDF）。
- `cleanArtifacts` 等价于执行 `latexmk -C`（大写 `-C`：Clean，连 PDF 一起删）。

记住 latexmk 的大小写记忆法：**小写清中间件、大写清全部**。注意 wiki 用词是「**At the moment**」（目前），意味着这只是当前实现细节，未来可能改变——但作为学习者，抓住「`-c` 保 PDF、`-C` 删 PDF」这条语义边界就够了。

两条命令都接收一个 `TextDocumentIdentifier`，告诉 texlab「清理**哪个**文档对应的辅助文件」。清理范围还受你配置的**产物目录**影响（见 4.2.3）。

#### 4.2.2 核心流程

```text
texlab.cleanAuxiliary(doc)
        │
        ▼
  确定该 doc 的产物目录
  （由 texlab.build.auxDirectory / 根目录检测决定）
        │
        ▼
  在该目录执行 latexmk -c   ──►  删除 .aux/.log/.fls/...  保留 PDF

texlab.cleanArtifacts(doc)
        │
        ▼
  同上确定产物目录
        │
        ▼
  在该目录执行 latexmk -C   ──►  连 PDF 一起删除
```

关键点：texlab 不会去猜产物在哪，而是用「当前配置的输出目录」。如果你用 `latexmkrc`，texlab 会自动推断目录（u2-l2 讲过）；如果用非 latexmk 工具，则取 `texlab.build.auxDirectory` 等设置项的值（默认 `.`，即根目录）。

#### 4.2.3 源码精读

- [Workspace-commands.md:L6-L15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L6-L15)：`cleanAuxiliary` 的契约——删除编译产生的辅助文件，**当前实现就是带产物目录调用 `latexmk -c`**；参数类型是 `TextDocumentIdentifier`。
- [Workspace-commands.md:L17-L26](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L17-L26)：`cleanArtifacts` 的契约——删除辅助文件**和产物**，**当前实现是调用 `latexmk -C`**；参数同样是 `TextDocumentIdentifier`。

理解「清理范围」时，配合读 `Configuration.md`：

- [Configuration.md:L66-L75](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L66-L75)：`texlab.build.auxDirectory` 定义 `.aux` 文件所在目录，默认 `.`（根目录）；用 `latexmkrc` 时 texlab 自动推断。这正是 `cleanAuxiliary` 「带配置的输出目录」去清理的对象。

#### 4.2.4 代码实践

**实践目标**：观察 `cleanAuxiliary` 删除了哪些文件、保留了哪个文件。

**操作步骤**：

1. 编译一次你的工程，确认根目录（或 `auxDirectory` 指定目录）下出现了 `main.aux`、`main.log`、`main.pdf`、`main.synctex.gz` 等文件。
2. 用 `ls`（或文件管理器）记录清理**前**的目录内容。
3. 通过编辑器命令面板触发 `texlab.cleanAuxiliary`（或对应「清理辅助文件」入口），参数指向 `main.tex`。
4. 再次 `ls` 记录清理**后**的目录内容。

**需要观察的现象**：清理后 `.aux`、`.log`、`.synctex.gz` 等中间文件消失，而 `main.pdf` **应当仍在**（因为 `cleanAuxiliary` 等价于 `latexmk -c`，保留 PDF）。

**预期结果**：清理前后目录对比显示「中间文件被删、PDF 保留」。若你的编辑器没有暴露该命令入口，可改为阅读本节源码、口述 `latexmk -c` 与 `-C` 的区别，并标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：你想彻底重来、连 PDF 都删掉重建，应该用 `cleanAuxiliary` 还是 `cleanArtifacts`？它等价于哪条 latexmk 命令？

> **参考答案**：用 `cleanArtifacts`，它等价于 `latexmk -C`（大写 C），会同时删除辅助文件和 PDF 等产物。

**练习 2**：调用 `texlab.cleanAuxiliary` 后，PDF 还在但 `.aux` 没了，这会带来什么后果？（提示：结合 u3-l3 学过的 tectonic `--keep-intermediates`。）

> **参考答案**：`.aux` 被删后，texlab 无法从中读出章节编号信息，补全列表里章节的编号显示会暂时失效，直到下次编译重新生成 `.aux`。这正是 tectonic 建议加 `--keep-intermediates` 的同源原因。

### 4.3 findEnvironments / changeEnvironment —— 环境定位与重命名

#### 4.3.1 概念说明

LaTeX 的「环境」是 `\begin{xxx} ... \end{xxx}` 包裹的代码块（如 `equation`、`figure`、`itemize`）。环境可以嵌套，于是一个光标位置可能同时落在多层环境里。texlab 提供两条命令围绕「**定位环境**」展开，分工明确：

- **`texlab.findEnvironments`**：**查询**。返回「包含指定位置的**所有**环境」——注意是**所有层**，从最内层到最外层，不是只返回最内层。返回一个 `EnvironmentLocation[]`。
- **`texlab.changeEnvironment`**：**修改**。把「包含指定位置的**最内层**环境」的名字改成 `params` 里给的 `newName`。这是编辑操作，不是查询。

理解 `findEnvironments` 的关键在于它返回的 `EnvironmentLocation` 结构：

```ts
interface EnvironmentLocation {
    name: {          // 环境名本身
        text: string; // 例如 "equation"
        range: Range; // 名字 token（\begin{equation} 里的 "equation"）的位置
    };
    fullRange: Range; // 整个 \begin{...} ... \end{...} 的完整范围
}
```

这里有两个 `Range`，用途不同：

- `name.range`：只覆盖**环境名字**那段文本，适合做高亮、就地标注。
- `fullRange`：覆盖**整个环境块**（从 `\begin` 到 `\end`），适合做「选中整段」「折叠」等需要整块范围的操作。

`changeEnvironment` 正是「先用定位找到最内层环境的 `name`，再替换成 `newName`」——它内部依赖的就是 find 用的同款环境分析能力，只不过只动最内层那一个。

参数类型上，两条命令都基于 **`TextDocumentPositionParams`**（LSP 标准类型：文档标识 + 光标 `position`），其中 `changeEnvironment` 额外加了 `newName` 字段。这意味着它们都属于「单参命令」（见 4.1 速查表）。

#### 4.3.2 核心流程

```text
findEnvironments(doc, position)
        │
        ▼
分析 doc 的环境结构，从 position 向外
收集所有包含该 position 的环境（内→外）
        │
        ▼
返回 EnvironmentLocation[]   （例如 [{figure}, {document}]）

changeEnvironment(doc, position, newName)
        │
        ▼
定位包含 position 的最内层环境
        │
        ▼
把 \begin{旧}...\end{旧} 的「旧」改为 newName
（成对替换 begin/end 两处名字）
```

举个嵌套例子，光标在 `▲` 处：

```latex
\begin{document}          % 第 1 层（最外）
  \begin{figure}          % 第 2 层
    \begin{center} ▲      % 第 3 层（最内）
```

调用 `findEnvironments` 会返回 3 个 `EnvironmentLocation`（`center`、`figure`、`document`，从内到外）；调用 `changeEnvironment` 且 `newName="table"`，则只会把最内层的 `center` 改成 `table`，`figure`/`document` 不受影响。

#### 4.3.3 源码精读

- [Workspace-commands.md:L28-L38](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L28-L38)：`changeEnvironment` 的契约——接收 `newName`，改变**最内层**环境名；参数 `ChangeEnvironmentParams extends TextDocumentPositionParams`，在标准位置参数基础上多了 `newName: string`。
- [Workspace-commands.md:L40-L62](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L40-L62)：`findEnvironments` 的契约——参数是标准 `TextDocumentPositionParams`；**返回 `EnvironmentLocation[]`**，其中 `EnvironmentLocation` 含 `name`（其内是 `text` 与 `range`）与 `fullRange` 两个范围。注意 wiki 把返回类型的结构完整写在 `Returns` 段，是理解「两个 Range 分别覆盖什么」的唯一依据。

#### 4.3.4 代码实践

**实践目标**：用 `findEnvironments` 验证它返回的是「所有层」而非「最内层」，并看清 `name.range` 与 `fullRange` 的区别。

**操作步骤**：

1. 准备一段三层嵌套的 LaTeX（如 4.3.2 的 `document`→`figure`→`center` 示例）。
2. 把光标放在最内层 `center` 内部。
3. 通过编辑器扩展调用 `texlab.findEnvironments`（若编辑器未直接暴露，可查阅其 LaTeX 插件是否包装了该命令）。

**需要观察的现象**：返回的 `EnvironmentLocation[]` 长度应为 3，分别对应 `center`、`figure`、`document`；每项的 `fullRange` 比其 `name.range` 大得多（前者覆盖整块、后者只覆盖名字）。

**预期结果**：确认「`find` 返回所有层、`change` 只改最内层」的分工，并理解 `name.range` 与 `fullRange` 的用途差异。若你的编辑器没有直接入口，改为阅读 [Workspace-commands.md:L40-L62](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L40-L62) 后口述上述结果，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`findEnvironments` 返回 `EnvironmentLocation[]`，其中 `name.range` 和 `fullRange` 分别覆盖什么？如果编辑器想「折叠整个 figure 块」，该用哪个？

> **参考答案**：`name.range` 只覆盖环境名字文本（如 `figure` 这几个字符），`fullRange` 覆盖整个 `\begin{figure}...\end{figure}`。折叠整块要用 `fullRange`。

**练习 2**：调用 `changeEnvironment(newName="theorem")` 时，光标同时位于 `equation` 和 `document` 两层环境内，最终哪一层被改名？为什么？

> **参考答案**：只改最内层的 `equation`。`changeEnvironment` 明确作用于「inner-most environment that contains the specified position」（见 [Workspace-commands.md:L28-L38](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L28-L38)），外层 `document` 不受影响。

### 4.4 showDependencyGraph / cancelBuild —— 依赖图与构建取消

#### 4.4.1 概念说明

这一组两条命令都**无参数**，分别服务于「看清项目结构」和「中止编译」两件排错时最常用的事。

**`texlab.showDependencyGraph`**：返回项目**依赖图**的 **DOT 格式**文本。这里的「依赖图」正是 [u1-l2](u1-l2-project-detection.md) 里 Discovery 算法构建的那棵树——以根文档为根，以 `\input`/`\import` 等包含命令为边，连接所有编译进同一文档的 `.tex` 文件。DOT 是 Graphviz 的图描述语言，把返回的字符串喂给 Graphviz（如 `dot` 命令）就能渲染成图片。这条命令是**查清「我的项目到底由哪些文件组成、谁引用谁」**的最直接手段，常用于排查「为什么这个文件没被编译进去」。

**`texlab.cancelBuild`**：取消**所有当前进行中**的构建请求。注意括号里的强调——它连 `texlab.build.onSave`（保存自动编译）触发的构建也一并取消。这对排错很有用：当你保存了一个大文件、texlab 开始跑漫长的 `latexmk`，而你又想改几笔再编译时，一条 `cancelBuild` 就能停掉它。在 u4-l1 里我们见过 `BuildStatus.Cancelled`（值 3）——被取消的构建最终会以这个状态回执，而触发它取消的就是 `texlab.cancelBuild`。

#### 4.4.2 核心流程

```text
showDependencyGraph()
        │
        ▼
读取 Discovery 已经算好的依赖树
（以根文档为根、\input/\import 为边）
        │
        ▼
序列化为 DOT 文本，例如：
   digraph {
     "main.tex" -> "chapter1.tex";
     "main.tex" -> "chapter2.tex";
   }
        │
        ▼
返回 string（可直接交给 Graphviz 渲染）

cancelBuild()
        │
        ▼
向所有活动构建发取消信号
（含 onSave 触发的异步构建）
        │
        ▼
对应构建以 BuildStatus.Cancelled 结束
```

`showDependencyGraph` 的输出依赖 Discovery 的结果，所以**先得让 texlab 识别出项目**（打开根文档、让其完成 Discovery）才有图可看；这也正是本讲把 u1-l2 列为前置依赖的原因。

#### 4.4.3 源码精读

- [Workspace-commands.md:L64-L74](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L64-L74)：`showDependencyGraph` 的契约——**无参数**，返回 `string`，内容是依赖图的 [DOT](https://graphviz.org/doc/info/lang.html) 格式描述。注意它返回的是**文本**，渲染成图要你自己用 Graphviz 处理。
- [Workspace-commands.md:L76-L82](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L76-L82)：`cancelBuild` 的契约——**无参数**，取消**所有**当前活动构建，**显式包括 `texlab.build.onSave` 触发的**。

理解这两条命令的「上游」，配合读另外两个 wiki 页：

- [Project-Detection.md:L10-L12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L10-L12)：Discovery 通过解析 `\input`/`\import` 构建**依赖树**——这正是 `showDependencyGraph` 输出的内容来源。
- [Configuration.md:L44-L50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L44-L50)：`texlab.build.onSave`（默认 `false`）——保存即编译，是 `cancelBuild` 文档里点名要取消的那类构建的来源。
- [LSP-Internals.md:L55-L63](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L55-L63)：`BuildStatus.Cancelled = 3`——被 `cancelBuild` 取消的构建最终回执的状态码（u4-l1 已学）。

#### 4.4.4 代码实践

**实践目标**：把 `showDependencyGraph` 返回的 DOT 文本渲染成图片，体会它「返回文本、由你渲染」的设计。

**操作步骤**：

1. 打开一个多文件 LaTeX 工程（如 `main.tex` 用 `\input` 引入 `chapter1.tex`、`chapter2.tex`），等 texlab 完成 Discovery。
2. 调用 `texlab.showDependencyGraph`，把返回的 `string` 存成文件，例如 `dep.dot`。
3. 用 Graphviz 渲染（需本机装有 graphviz）：

   ```bash
   dot -Tpng dep.dot -o dep.png
   ```

4. 打开 `dep.png`，查看文件之间的引用关系。

**需要观察的现象**：图片是一张有向图，`main.tex` 为根（或被多条边指向的中心），箭头从主文件指向被 `\input` 的章节文件，与你工程的真实结构一致。

**预期结果**：成功渲染出反映 `\input` 关系的依赖图。若本机没有 graphviz，可改为把 DOT 文本粘贴到 [Graphviz Online](https://dreampuf.github.io/GraphvizOnline/) 等在线渲染器查看；若编辑器未暴露该命令，则标注「待本地验证」并口述渲染步骤。

#### 4.4.5 小练习与答案

**练习 1**：`showDependencyGraph` 返回什么类型的值？为什么 texlab 不直接返回一张图片？

> **参考答案**：返回 `string`（DOT 格式文本）。texlab 是语言服务器、只产出文本数据，渲染成图片是客户端/用户侧的事（交给 Graphviz）。这种「数据归服务器、呈现归客户端」的分工正是 LSP 的设计哲学。

**练习 2**：你保存了一个大文件触发 `onSave` 编译，但立刻想再改两笔。如何用本讲的命令停掉这次编译？被取消的构建会以哪个 `BuildStatus` 回执？

> **参考答案**：调用 `texlab.cancelBuild`——它明确取消包括 `onSave` 在内的所有活动构建（见 [Workspace-commands.md:L76-L82](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L76-L82)）。被取消的构建最终以 `BuildStatus.Cancelled`（= 3）回执。

## 5. 综合实践

把本讲三组命令串起来，在一个多文件工程里走一遍「定位 → 看图 → 清理」全流程：

1. **准备工程**：建一个 `main.tex`（含 `\documentclass` 与 `document` 环境），用 `\input` 引入 `chapter1.tex`、`chapter2.tex`；在 `chapter1.tex` 里写一段三层嵌套环境（如 `document`→`figure`→`center`）。先编译一次，确保产物文件齐全。

2. **调用 `texlab.findEnvironments`**：把光标放在最内层 `center` 内，调用命令，记录返回的 `EnvironmentLocation[]`——确认它包含三层（`center`/`figure`/`document`），并对比每项 `name.range` 与 `fullRange` 的大小。

3. **调用 `texlab.showDependencyGraph`**：把返回的 DOT 文本存为 `dep.dot`，用 `dot -Tpng dep.dot -o dep.png` 渲染，核对图中的边是否与你的 `\input` 关系一致（`main.tex` → `chapter1.tex`、`main.tex` → `chapter2.tex`）。

4. **调用 `texlab.cleanAuxiliary`**：清理前先 `ls` 记录目录（应能看到 `.aux`、`.log`、`.pdf` 等），执行清理后再 `ls`，确认中间文件消失而 PDF 保留。

5. **（可选）体验 `cancelBuild`**：把 `texlab.build.onSave` 设为 `true`，保存一个大文件触发编译，随即调用 `texlab.cancelBuild`，观察编译被中止、最终状态为 `Cancelled`。

> 排错提示：若 `showDependencyGraph` 返回空图或缺失文件，多半是 Discovery 没跑完或根目录识别有误——回到 [u1-l2](u1-l2-project-detection.md) 检查 `.texlabroot`/根源文件是否正确。若 `cleanAuxiliary` 没清掉文件，检查产物目录配置（u2-l2）是否指向了实际目录。

## 6. 本讲小结

- texlab 通过 LSP **标准方法** `workspace/executeCommand` 暴露六条 `texlab.*` 命令，对客户端比自定义请求更友好；六条命令都遵守「**至多一个参数**」约定，`arguments` 数组长度只能是 0 或 1。
- `cleanAuxiliary`（等价 `latexmk -c`，保留 PDF）与 `cleanArtifacts`（等价 `latexmk -C`，连 PDF 一起删）负责按配置的产物目录清理；二者参数都是 `TextDocumentIdentifier`。
- `findEnvironments` 返回**所有层**环境的 `EnvironmentLocation[]`（含 `name.range` 与 `fullRange` 两个范围）；`changeEnvironment` 只改**最内层**环境名；二者都基于 `TextDocumentPositionParams`，`changeEnvironment` 额外带 `newName`。
- `showDependencyGraph` 无参数、返回 DOT 文本（来自 Discovery 的依赖树，需用 Graphviz 渲染）；`cancelBuild` 无参数、取消所有活动构建（含 `onSave` 触发的），对应 `BuildStatus.Cancelled`。
- 记住大小写记忆法与分工记忆法：latexmk「小写清中间、大写清全部」；`find` 查所有层、`change` 改最内层。

## 7. 下一步学习建议

- 继续向 LSP 协议层深入：读 [u4-l3 枚举映射](u4-l3-enum-mapping.md)，看 texlab 如何把 LaTeX/BibTeX 结构映射到 `CompletionItemKind`/`SymbolKind`，补全协议层的认知。
- 想亲手扩展编辑器集成：把本讲六条命令与你所用编辑器的「命令面板」对一遍，确认哪些已暴露、哪些需要自己包一层 `workspace/executeCommand`。
- 排查项目识别问题：把 `showDependencyGraph` 当作 u1-l2 的实战工具，拿它验证 Discovery 是否把你预期的文件都纳入了项目。
- 想看 texlab 服务端的真实实现：本 wiki 只给协议契约，服务端源码在主仓库 `latex-lsp/texlab`（Rust），可按本讲提到的命令名（如 `findEnvironments`）去那边搜索对应的 `workspace/executeCommand` handler 做交叉印证。
