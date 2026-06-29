# 自定义 LSP 消息：build 与 forwardSearch

## 1. 本讲目标

学完本讲，你应该能够：

- 理解 texlab 如何在标准 LSP 之上「自定义」出 `textDocument/build` 与 `textDocument/forwardSearch` 两个请求，以及用 `experimental` capability 声明它们的机制；
- 读懂这两个请求的 params / result 结构（`BuildTextDocumentParams`、`BuildResult`、`TextDocumentPositionParams`、`ForwardSearchResult`）；
- 准确说出 `BuildStatus` 四种状态（Success / Error / Failure / Cancelled）与 `ForwardSearchStatus` 四种状态（Success / Error / Failure / Unconfigured）各自的语义和触发场景；
- 能为一个新编辑器写出调用这两个自定义请求的 JSON-RPC 报文示例，并据返回的 `status` 更新 UI。

## 2. 前置知识

本讲承接 **u3-l1「编译与预览的整体流程」**：你已经知道 texlab 编译文档有两条触发路径——`textDocument/build`（编辑器主动发起、同步、带回执）与 `build.onSave`（保存驱动、异步），二者共用同一套 `build.executable` + `build.args`；正向搜索由 `forwardSearch.*` 驱动，`build.forwardSearchAfter` 把「编译 → 正向搜索」串成流水线。本讲不再重复这条工作流，而是**下沉到协议层**：这两个动作作为「自定义 LSP 请求」时，报文长什么样、返回什么状态。

补充两个本讲要用、但初学者可能不熟的 LSP 基础概念：

- **JSON-RPC 2.0**：LSP 的传输层。每个请求是一个 JSON 对象，含 `jsonrpc`、`id`、`method`、`params`；对应的响应含相同的 `id` 与 `result`（或 `error`）。`method` 形如 `域名/动作`，标准方法有 `textDocument/definition` 等，texlab 在此之外又加了自定义方法。
- **capability 协商**：客户端与服务器在 `initialize` 握手时交换各自的「能力」清单（`capabilities`），告诉对方「我支持哪些功能」。标准能力有固定字段名；非标准（自定义）的能力则放进一个叫 `experimental` 的自由字段里——这正是 texlab 用来声明自定义请求的入口。

## 3. 本讲源码地图

本讲只精读一个 wiki 页面，但它定义了两个自定义请求的全部协议细节：

| 文件 | 作用 |
| --- | --- |
| `LSP-Internals.md` | texlab 对 LSP 的自定义扩展文档。前半部分「Custom Messages」定义 `textDocument/build` 与 `textDocument/forwardSearch` 两个请求及其状态枚举（**本讲核心**）；后半部分「Enum Mapping」是结构 → 符号种类映射表，留给 u4-l3。 |

补充参考（理解状态含义时有用，非本讲精读对象）：

- `Workspace-commands.md` 中的 `texlab.cancelBuild` 命令——它是触发 `BuildStatus.Cancelled` 的途径之一。

## 4. 核心概念与源码讲解

### 4.1 LSP 自定义消息与 experimental capability 扩展机制

#### 4.1.1 概念说明

标准 LSP 规定了语言服务器应提供的一组方法（补全、跳转定义、悬停、符号……）。但 LaTeX 工作流里有些动作（编译出 PDF、把光标位置跳到 PDF）并不在标准方法覆盖范围内。texlab 想让编辑器能通过 LSP 通道触发这些动作，于是**自定义**了两个新的请求方法：`textDocument/build` 和 `textDocument/forwardSearch`。

关键点（wiki 开篇就强调）：这些自定义消息是**可选的**，是否支持由客户端（编辑器）决定。

#### 4.1.2 核心流程

一个自定义请求要能跑通，需要三步：

1. **声明存在**：在 `initialize` 握手时，通过 `experimental` 字段里的某个键（`textDocumentBuild` / `textDocumentForwardSearch`）表明这个自定义请求「存在 / 被使用」。
2. **客户端发起**：客户端按标准 JSON-RPC 格式，用自定义 `method` 名发送请求。
3. **服务器响应**：texlab 执行对应动作（编译 / 调用正向搜索命令），返回带状态码的 `result`。

```text
客户端                          texlab(服务器)
  │  initialize { capabilities: { experimental: { textDocumentBuild: true, ... } } }
  │ ───────────────────────────────────────────────────────────▶
  │  InitializeResult { ... }
  │ ◀───────────────────────────────────────────────────────────
  │
  │  request  method="textDocument/build"  params={ textDocument, position? }
  │ ───────────────────────────────────────────────────────────▶
  │  response result={ status: BuildStatus }
  │ ◀───────────────────────────────────────────────────────────
```

#### 4.1.3 源码精读

wiki 开篇点明「自定义消息、可选、客户端决定」这条总原则：

[LSP-Internals.md:3-5](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L3-L5) —— texlab 扩展 LSP、加入自定义消息以提供更好的 LaTeX 集成；这些消息是可选的，是否支持由客户端决定。

随后两个请求各有一句相同的声明句式。以 build 为例：

[LSP-Internals.md:7-10](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L7-L10) —— 「Build Request」一节，说明用 `textDocumentBuild` 这个 experimental capability 来表明该自定义请求存在。

> **关于声明方向**：wiki 只说「用一个 experimental capability 来表明该自定义请求存在」，并未给出该键具体放在 `InitializeParams` 还是 `InitializeResult` 的哪一侧、以及值的确切形态。基于 LSP 规范（client / server 的 capabilities 都支持 `experimental` 自由字段）和「是否支持由客户端决定」这句话，惯例是**客户端**在 client capabilities 的 `experimental` 里置 `textDocumentBuild` / `textDocumentForwardSearch` 以示 opt-in。**具体 JSON 形态以 texlab 源码为准（待确认）**，本讲不臆造。

#### 4.1.4 代码实践

**实践目标**：搞清 experimental capability 在握手中的位置。

**操作步骤**：

1. 找一个能 dump LSP 流量的编辑器（如 VS Code 的 `Output → LaTeX` 面板，或给 texlab 加日志）。
2. 启动 texlab，截取 `initialize` 请求与响应。
3. 在两侧的 `capabilities` 里查找 `experimental` 字段，看 `textDocumentBuild` / `textDocumentForwardSearch` 出现在哪一侧、值是什么。

**需要观察的现象**：`capabilities` 对象里存在一个 `experimental` 字段，里面能看到 `textDocumentBuild`、`textDocumentForwardSearch` 等键。

**预期结果**：确认这两个自定义请求是通过 `experimental` 协商的，而非标准 `textDocument.*` 字段。若你的编辑器未实现 texlab 扩展，则看不到这两个键——这正是「可选」的含义。

> 若无法本地抓包，标记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 texlab 要用 `experimental` 字段，而不是直接定义标准方法名？
**答案**：标准 LSP 方法名是规范固定的（如 `textDocument/definition`），「编译 PDF」「跳转 PDF」不在标准之列；强行占用一个方法名会与未来规范冲突。`experimental` 是 LSP 专门留给非标准扩展的「逃生舱」，既不破坏规范又能协商。

**练习 2**：如果一个编辑器完全不实现这两个自定义请求，texlab 还能正常工作吗？
**答案**：能。这两个请求是可选的；编辑器不调用 `textDocument/build`，用户仍可依赖 `build.onSave` 自动编译，只是失去「点按钮主动编译并拿回执」的能力。

---

### 4.2 textDocument/build 请求：编译文档并回执状态

#### 4.2.1 概念说明

`textDocument/build` 是「请 texlab 现在编译这个文档」的请求。它是 `build.onSave` 的「手动 / 可编程」对应物：onSave 是保存时被动触发、异步、无回执；而 `textDocument/build` 是编辑器主动发起、**同步**等待、并带回执——编辑器因此能在状态栏显示「编译成功 / 失败」。两者底层共用同一套 `build.executable` + `build.args`（见 u2-l2）。

#### 4.2.2 核心流程

```text
编辑器点击 "Build"
   │
   ▼
发 textDocument/build 请求，params = { textDocument, position? }
   │
   ▼
texlab 执行 build.executable + build.args（%f 替换为该文档）
   │
   ▼
返回 BuildResult { status }，status ∈ { Success, Error, Failure, Cancelled }
```

`position` 字段很关键：它是「光标位置，供正向搜索使用」。也就是说，调用 build 时可以把光标位置一起带过去；如果配了 `build.forwardSearchAfter`，texlab 编译完就能用这个位置接着做正向搜索（见 u3-l1）。这就是 build 请求里埋着的「与 forwardSearch 联动」的钩子。

#### 4.2.3 源码精读

请求方法名与参数类型：

[LSP-Internals.md:14-15](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L14-L15) —— 方法名 `textDocument/build`，参数类型 `BuildTextDocumentParams`。

`BuildTextDocumentParams` 的结构：

[LSP-Internals.md:18-28](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L18-L28) —— `textDocument: TextDocumentIdentifier`（要编译的文档）与可选 `position?: Position`（光标位置，供正向搜索使用）。

响应结构 `BuildResult` 与 `BuildStatus` 枚举：

[LSP-Internals.md:36-41](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L36-L41) —— `BuildResult` 只含一个 `status: BuildStatus` 字段。

[LSP-Internals.md:43-63](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L43-L63) —— `BuildStatus` 四个值，逐条：

- [Success = 0](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L47) —— 编译正常结束，无错误。
- [Error = 1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L52) —— 编译结束但有错误（如 LaTeX 报错，但进程仍正常退出）。
- [Failure = 2](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L57) —— 编译进程未能启动或崩溃（如 `build.executable` 路径错、latexmk 不存在、被信号杀死）。
- [Cancelled = 3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L62) —— 编译被取消。

> `Cancelled` 的来源之一是 `texlab.cancelBuild` 命令：[Workspace-commands.md:76-80](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Workspace-commands.md#L76-L80) —— 取消所有进行中的构建（含 `build.onSave` 触发的）。客户端调用它后，被取消的 build 的状态即变为 `Cancelled`。

#### 4.2.4 代码实践

**实践目标**：写出 `textDocument/build` 的 JSON-RPC 请求与四种响应示例。

**操作步骤**：按下表构造报文（`id` 任取，仅作请求-响应配对用）。

请求（带光标位置，留给后续正向搜索）：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "textDocument/build",
  "params": {
    "textDocument": { "uri": "file:///home/user/paper/main.tex" },
    "position": { "line": 42, "character": 10 }
  }
}
```

四种可能的响应（`status` 0 / 1 / 2 / 3）：

```json
{ "jsonrpc": "2.0", "id": 1, "result": { "status": 0 } }   // Success
{ "jsonrpc": "2.0", "id": 1, "result": { "status": 1 } }   // Error
{ "jsonrpc": "2.0", "id": 1, "result": { "status": 2 } }   // Failure
{ "jsonrpc": "2.0", "id": 1, "result": { "status": 3 } }   // Cancelled
```

**需要观察的现象 / 预期结果**：客户端用同一 `id` 把请求与响应配对；根据 `status` 在 UI 上分别显示「成功 / 有错误 / 编译器没跑起来 / 被取消」。

> 以上为基于协议结构构造的**示例代码**，未实际发送。

#### 4.2.5 小练习与答案

**练习 1**：`BuildTextDocumentParams.position` 是必填吗？不填会怎样？
**答案**：可选（`position?`）。不填时编译照常进行；但如果同时开了 `forwardSearchAfter`，texlab 拿不到光标位置，正向搜索可能跳不到正确位置（或回退到默认）。

**练习 2**：`Error(1)` 与 `Failure(2)` 的本质区别？
**答案**：Error 是「编译器跑完了、但源码有错」（进程正常退出，退出码非 0 或解析到错误）；Failure 是「编译器根本没跑起来或中途崩溃」（进程启动失败 / 被 kill）。前者通常是 LaTeX 语法问题，后者通常是环境 / 配置问题。

**练习 3**：用户连点两次 "Build"，第一次的 build 会怎样？
**答案**：texlab 可能取消进行中的旧构建（或新请求排队），旧请求的状态会以 `Cancelled(3)` 返回。这也是 `texlab.cancelBuild` 之外触发 Cancelled 的常见路径。

---

### 4.3 textDocument/forwardSearch 请求：正向搜索并回执状态

#### 4.3.1 概念说明

`textDocument/forwardSearch` 是「请 texlab 现在把光标位置跳到 PDF 对应位置」的请求——即正向搜索（编辑器 → PDF，见 u3-l2）。和 build 一样，它是 `forwardSearchAfter` 自动联动的「手动 / 可编程」对应物：编辑器可以绑一个快捷键直接调它，而不必每次先编译。

注意它和 build 在参数上的差异：build 用自定义的 `BuildTextDocumentParams`（带可选 `position`）；forwardSearch 直接用 **LSP 标准类型** `TextDocumentPositionParams`（文档 + 位置，都必填）——因为正向搜索**必须**知道跳到哪一行。

#### 4.3.2 核心流程

```text
编辑器按 "SyncTeX 正向搜索" 快捷键（光标在某行）
   │
   ▼
发 textDocument/forwardSearch，params = TextDocumentPositionParams { textDocument, position }
   │
   ▼
texlab 用 forwardSearch.executable + forwardSearch.args（替换 %f / %p / %l）调用 PDF 阅读器
   │
   ▼
返回 ForwardSearchResult { status }，status ∈ { Success, Error, Failure, Unconfigured }
```

#### 4.3.3 源码精读

方法名、参数、capability 声明：

[LSP-Internals.md:68-74](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L68-L74) —— forward search 请求由客户端在用户请求正向搜索（经 SyncTeX）时发出；用 `textDocumentForwardSearch` experimental capability 声明；方法名 `textDocument/forwardSearch`；参数为标准类型 `TextDocumentPositionParams`。

响应结构 `ForwardSearchResult` 与 `ForwardSearchStatus` 枚举：

[LSP-Internals.md:81-86](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L81-L86) —— `ForwardSearchResult` 只含 `status: ForwardSearchStatus`。

[LSP-Internals.md:88-108](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L88-L108) —— `ForwardSearchStatus` 四个值，逐条：

- [Success = 0](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L92) —— 预览器（阅读器）进程执行命令无错误。
- [Error = 1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L97) —— 预览器执行了命令但有错误（如找不到 PDF、缺 `.synctex.gz`）。
- [Failure = 2](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L102) —— 预览器进程未能启动或崩溃（如 `forwardSearch.executable` 路径错）。
- [Unconfigured = 3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L107) —— **正向搜索命令未配置**（`forwardSearch.executable` / `args` 为 `null`，而这正是它们的默认值，见 u3-l2）。

> 注意「previewer」一词：wiki 把 PDF 阅读器称为 previewer（预览器进程），状态描述围绕「这个外部命令跑得怎么样」展开。

#### 4.3.4 代码实践

**实践目标**：写出 `textDocument/forwardSearch` 的请求与四种响应示例。

**操作步骤**：构造如下报文。

请求：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "textDocument/forwardSearch",
  "params": {
    "textDocument": { "uri": "file:///home/user/paper/main.tex" },
    "position": { "line": 42, "character": 10 }
  }
}
```

四种可能的响应：

```json
{ "jsonrpc": "2.0", "id": 2, "result": { "status": 0 } }   // Success
{ "jsonrpc": "2.0", "id": 2, "result": { "status": 1 } }   // Error
{ "jsonrpc": "2.0", "id": 2, "result": { "status": 2 } }   // Failure
{ "jsonrpc": "2.0", "id": 2, "result": { "status": 3 } }   // Unconfigured
```

**需要观察的现象 / 预期结果**：当 `forwardSearch.*` 未配置时，最常收到的就是 `status: 3 (Unconfigured)`——这是排查「为什么按了快捷键 PDF 不跳」的第一线索。

> 以上为**示例代码**，未实际发送。

#### 4.3.5 小练习与答案

**练习 1**：为什么 forwardSearch 的参数**没有**可选性、位置必填，而 build 的位置是可选的？
**答案**：正向搜索的语义就是「把某个位置跳到 PDF」，没有位置就无法跳转，故 `TextDocumentPositionParams` 的 position 必填；build 的核心是「编译整个文档」，位置只是顺带给正向搜索用的附带信息，故可选。

**练习 2**：收到 `Unconfigured(3)` 该去查哪个配置？
**答案**：`texlab.forwardSearch.executable` 与 `texlab.forwardSearch.args`。它们默认 `null`（见 u3-l2），未填就会返回 Unconfigured。

---

### 4.4 两枚举对照：为什么第 4 个状态不同

#### 4.4.1 概念说明

`BuildStatus` 和 `ForwardSearchStatus` 的前 3 个值完全对称（Success / Error / Failure = 0 / 1 / 2），但**第 4 个值不同**：build 是 `Cancelled(3)`，forwardSearch 是 `Unconfigured(3)`。这不是随意的，而是反映了两套外部命令的**配置默认值不同**。

#### 4.4.2 核心流程（对照表）

| 数值 | BuildStatus | ForwardSearchStatus |
| --- | --- | --- |
| 0 | Success | Success |
| 1 | Error | Error |
| 2 | Failure | Failure |
| 3 | **Cancelled**（被取消） | **Unconfigured**（命令未配置） |

**为什么 build 没有 Unconfigured？** 因为 `build.executable` 默认就是 `latexmk`（见 u2-l2），编译命令永远「已配置」，不需要一个「未配置」状态。

**为什么 forwardSearch 没有 Cancelled？** 因为正向搜索是一次性、近乎瞬时地调用阅读器命令，没有像编译那样可能持续数秒、需要被中途取消的长任务。

#### 4.4.3 源码精读

两个枚举并排出自同一文件，便于对照：

- Build 第 4 值：[LSP-Internals.md:59-62](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L59-L62) —— `Cancelled = 3`，注释「The build process was cancelled」。
- ForwardSearch 第 4 值：[LSP-Internals.md:104-107](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/LSP-Internals.md#L104-L107) —— `Unconfigured = 3`，注释「The previewer command is not configured」。

#### 4.4.4 代码实践

**实践目标**：在客户端代码里用同一段逻辑处理两种状态，体会「前 3 个共用、第 4 个分流」。

**操作步骤**：写一段伪代码（**示例代码**）：

```ts
// status: 0 Success / 1 Error / 2 Failure 在两种枚举里语义一致，可复用
function commonLabel(s: number): string {
  return ["成功", "有错误", "未能启动/崩溃"][s] ?? "未知";
}

function onBuildStatus(s: BuildStatus) {
  if (s <= 2) setStatus(commonLabel(s));        // 0/1/2 通用
  else setStatus("编译被取消");                   // 3：build 专属
}

function onForwardStatus(s: ForwardSearchStatus) {
  if (s <= 2) setStatus(commonLabel(s));        // 0/1/2 通用
  else setStatus("正向搜索未配置，请填写 forwardSearch.*"); // 3：forwardSearch 专属
}
```

**需要观察的现象 / 预期结果**：0 / 1 / 2 可复用同一套 UI 文案，只有 3 需要按请求类型分别提示。

#### 4.4.5 小练习与答案

**练习**：如果将来 texlab 给 forwardSearch 也加了「可取消」能力，枚举会怎么变？
**答案**：需要新增一个值（如 `Cancelled = 4`），不能复用 3，因为 3 已被 `Unconfigured` 占用——这正是 LSP / 枚举设计里「数值一旦发布即固定」的约束。

---

## 5. 综合实践

**任务**：你正在为一个全新的编辑器实现 texlab 客户端，要支持「Build 按钮」和「SyncTeX 正向搜索快捷键」。请产出一份最小集成方案。

1. **握手**：写出 `initialize` 请求里 `capabilities.experimental` 应包含的两个键（`textDocumentBuild`、`textDocumentForwardSearch`），并说明为什么必须放在 `experimental` 而非标准字段。（对应 4.1）
2. **Build 按钮**：写出点击后发送的 `textDocument/build` 请求 JSON（带上光标 `position`），并写出 4 种 `BuildStatus` 各自的典型触发场景与对应 UI 提示文案。（对应 4.2）
3. **正向搜索快捷键**：写出 `textDocument/forwardSearch` 请求 JSON，并解释收到 `Unconfigured(3)` 时该引导用户去配置什么。（对应 4.3）
4. **状态处理**：用一个对照表说明两个枚举的 0 / 1 / 2 如何复用、3 如何分流。（对应 4.4）

**验收标准**：

- 请求 JSON 的字段与方法名与 wiki 定义完全一致；
- 能正确说出每个 `status` 数值对应的枚举名与场景；
- 能解释 build 的 `position` 字段、forwardSearch 用标准 `TextDocumentPositionParams`、以及第 4 个状态不同的原因。

> 这是协议设计型实践，无需真实编译；所有报文均为示例代码。

## 6. 本讲小结

- texlab 在标准 LSP 之上自定义了 `textDocument/build` 与 `textDocument/forwardSearch` 两个请求，均**可选**，是否支持由客户端决定。
- 这两个请求通过 `experimental` capability（`textDocumentBuild` / `textDocumentForwardSearch`）在 `initialize` 握手中声明存在。
- `textDocument/build` 参数为自定义 `BuildTextDocumentParams`（`textDocument` + 可选 `position`，position 供正向搜索联动），返回 `BuildResult { status }`。
- `textDocument/forwardSearch` 参数为标准 `TextDocumentPositionParams`（位置必填），返回 `ForwardSearchResult { status }`。
- `BuildStatus` 与 `ForwardSearchStatus` 前 3 值对称（Success / Error / Failure），第 4 值不同（Cancelled vs Unconfigured），根源在于 `build.executable` 有默认值而 `forwardSearch.*` 默认 `null`。
- 客户端实现时，请求用 JSON-RPC 2.0，靠 `id` 配对请求与响应，再据 `status` 更新 UI。

## 7. 下一步学习建议

- **u4-l2（workspace 命令）**：本讲提到的 `texlab.cancelBuild`（产生 Cancelled）就在那里，连同 `cleanArtifacts`、`findEnvironments`、`showDependencyGraph` 等命令一起讲，是自定义消息的另一种形态（`workspace/executeCommand`）。
- **u4-l3（枚举映射）**：`LSP-Internals.md` 同文件下半部分「Enum Mapping」，讲 LaTeX / BibTeX 结构如何映射到 `CompletionItemKind` / `SymbolKind`。
- 若想验证报文，建议阅读你所用编辑器的 texlab 插件源码（如 VS Code 的 LaTeX Workshop），看它如何发送 `textDocument/build` 并处理 `BuildStatus`。
