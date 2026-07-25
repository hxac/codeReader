# 文档同步机制

## 1. 本讲目标

上一讲（u2-l3）我们打通了「字节 ⇄ JSON ⇄ 事件/命令」这条管道：知道了 `sendCommand` 如何把一个 `[command, ...data]` 数组序列化成一行 NDJSON 写进子进程的 `stdin`，也知道了 `emit` 如何把收到的命令分发出去。但那套管道里**跑的到底是什么业务命令**，我们一直没有展开。

本讲就来回答一个最基本的问题：**用户在编辑器里打开、编辑、关闭一个 `.tex` 文件时，texpresso-lsp 是怎么把「文档内容」同步给 texpresso 子进程的？**

读完本讲，你应该能够：

- 解释 `TextDocumentSyncKind.Incremental`（增量同步）的含义，以及它和 `Full`、`None` 两种模式的差别。
- 读懂 `onDidOpen` 如何把**整份文档文本**通过 `open` 命令交给 texpresso，以及 `onDidChangeTextDocument` 如何把**一处改动**编码成 `change-range` 的六元数组。
- 说出 `onDidClose` 发出的 `close` 命令为什么只带路径、不带文本。
- 理解 `URI.parse(...).path` 在三个事件里反复出现的作用，以及为什么 `onDidChangeTextDocument` 里那句 `// can this be out of sync?` 注释值得警惕。

本讲只看一个文件 `src/server.ts`（外加对 `process-manager.ts` 里 `sendCommand` 的回看），是 u2-l3 协议层之上的**第一个业务消费者**。后续的实时预览（u3-l1）、SyncTeX（u3-l2）都建立在「文档已经同步好了」这个前提之上。

## 2. 前置知识

进入源码前，先用大白话建立两个心智模型。

### 2.1 LSP 的文档同步：编辑器和服务器如何保持「同一份文档」

在没有 LSP 的年代，编辑器要想让某个外部工具看到自己正在编辑的内容，只能频繁地「保存文件 → 让工具重新读盘」。LSP 提供了一套更精细的机制：**编辑器不必落盘，也能把「文档现在的样子」实时告诉语言服务器**。这套机制叫**文档同步（document synchronization）**。

它的生命周期是一个三段式：

1. **打开（didOpen）**：用户在编辑器里打开一个文件。编辑器发 `textDocument/didOpen` 通知，**并附上当前全文**。从此服务器内存里就有了一份这个文档的副本。
2. **变更（didChange）**：用户每次敲键、删除、粘贴，编辑器发 `textDocument/didChange` 通知，告诉服务器「哪里改了」。
3. **关闭（didClose）**：用户关掉文件标签页。编辑器发 `textDocument/didClose` 通知，服务器可以释放相关资源。

> 注意区分 `didChange`（内容变了，**未保存**）和 `didSave`（保存到磁盘）。前者是内存中文档的实时变化，后者是一次落盘动作。本讲只讲 `didChange`；`didSave` 触发的是编译流程，留到 u3-l1。

### 2.2 三种同步粒度：None / Full / Incremental

LSP 允许服务器在握手时声明自己想要的同步粒度，用一个枚举 `TextDocumentSyncKind` 表示：

| 取值 | 含义 | didChange 携带的内容 |
| --- | --- | --- |
| `None` (0) | 不同步 | 服务器根本收不到 didChange |
| `Full` (1) | 全量同步 | 每次变化都把**整份新文档全文**发过来 |
| `Incremental` (2) | 增量同步 | 只发**改动的那一小块**（一个范围 + 新文本） |

texpresso-lsp 选的是 `Incremental`，在握手返回的 `capabilities` 里声明（我们在 u1-l4 见过这一处）：

- 声明位置：[src/server.ts:120](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L120) —— `textDocumentSync: TextDocumentSyncKind.Incremental`。

选增量的原因很直觉：LaTeX 文档动辄成千上万字符，每敲一个字就把全文重发一遍太浪费。增量模式下，一次敲键只会在网络上携带「第几行第几列到第几行第几列、替换成什么」这样几十个字节的信息。

**这条声明会反向约束 didChange 的格式**：既然服务器说自己只接受增量，编辑器发来的 `contentChanges` 里每一项就都会带一个 `range`（改动范围）。源码里 `TextDocumentContentChangeEvent.isIncremental(change)` 这个判断（见 4.2）正是用来确认「这一项确实带了 range」的防御性检查——如果哪天收到一项没带 range 的（即 Full 式的全量变更），它会被静默跳过。

### 2.3 两套 API 的差别（本讲最关键的一处理解）

`vscode-languageserver` 给了我们**两种**接收文档事件的方式：

- **高层 API（`documents.onDidOpen` / `onDidClose` / `onDidChangeContent`）**：来自 `TextDocuments` 文档管理器。它返回的 `event.document` 是管理器**已经维护好的、权威的**文档对象，你拿到的永远是它更新过的最新视图。
- **低层 API（`connection.onDidChangeTextDocument` 等）**：直接挂在 `connection` 上，拿到的是**原始的 LSP 通知参数** `params`，还没经过文档管理器的加工。

本讲的代码做了一个**不对称**的选择：打开和关闭用了高层 API，而「变更」却用了低层 API。这句 `// can this be out of sync?` 注释的根源，正是这个不对称——后面 4.2 会展开。

> 名词速查：**文档管理器（`TextDocuments`）**是 `vscode-languageserver-textdocument` 提供的一个对象，它在内部帮你「收 didChange → 应用到内存文档模型」；你只需 `documents.listen(connection)` 就能让它挂上去自动运转（见 [src/server.ts:44](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L44)）。

## 3. 本讲源码地图

本讲只看 `src/server.ts` 一个文件（外加对 `process-manager.ts` 中 `sendCommand` 的回看）：

| 关注点 | 位置 | 一句话作用 |
| --- | --- | --- |
| 文档管理器挂载 | [src/server.ts:44](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L44) | `documents.listen(connection)` 让管理器开始收 open/change/close |
| 增量同步声明 | [src/server.ts:120](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L120) | 握手时告诉编辑器「我要增量同步」 |
| `onDidOpen` | [src/server.ts:169-177](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L169-L177) | 打开 → 发 `open [path, 全文]` |
| `onDidChangeTextDocument` | [src/server.ts:204-226](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L204-L226) | 变更 → 发 `change-range [path, 起/止行列, 新文本]` |
| `onDidClose` | [src/server.ts:198-202](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L198-L202) | 关闭 → 发 `close [path]` |
| `sendCommand`（回看） | [src/process-manager.ts:103-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L103-L110) | 把命令编码成 `["cmd", ...data]\n` 写进 stdin |

一句话：三个事件处理器各自把 LSP 的文档事件**翻译**成一条 texpresso 命令，再交给 u2-l3 讲过的 `sendCommand` 发出去。文档同步的本质就是「**LSP 文档事件 → texpresso 命令**」的翻译层。

## 4. 核心概念与源码讲解

### 4.1 onDidOpen：把整份文档交给 texpresso

#### 4.1.1 概念说明

`texpresso` 是一个渲染器，它要能渲染一份 LaTeX 文档，**前提是它得先拿到这份文档的全文**。当用户在编辑器里打开一个 `.tex` 文件时，LSP 的 `didOpen` 通知正好就携带了全文——这是一个绝佳的「**冷启动喂全文**」时机。

所以 `onDidOpen` 的职责很单一：把「这个文件 + 它的全文」通过 `open` 命令交给 texpresso。之后 texpresso 就在内存里持有了一份副本，后续的增量改动只需要发「差异」即可。

这里还要顺带处理一件事：LSP 里文档是用 **URI**（形如 `file:///home/user/main.tex`）来标识的，而 texpresso 是个命令行程序，它认的是**文件系统路径**（`/home/user/main.tex`）。所以需要把 URI 转换成 path。本模块先用到 `URI.parse(...).path`，4.3 会专门讲这个转换。

#### 4.1.2 核心流程

```
编辑器打开 main.tex
   │  发 textDocument/didOpen { uri, text(全文) }
   ▼
documents.onDidOpen(event)
   │  ① document = event.document        // 拿到文档对象
   │  ② path   = URI.parse(document.uri).path   // URI → 路径
   │  ③ text   = document.getText()      // 取全文
   ▼
texpressoProcess.sendCommand("open", [path, text])
   │  （回到 u2-l3 的协议层）
   ▼
stdin 写入:  ["open","/home/user/main.tex","全文…"]\n
```

线缆上最终写出的是一行 NDJSON：第 0 位是命令名 `"open"`，第 1 位是路径，第 2 位是全文文本。

#### 4.1.3 源码精读

处理器本体见 [src/server.ts:169-177](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L169-L177)：

```ts
documents.onDidOpen(async (event: TextDocumentChangeEvent<TextDocument>) => {
    const document = event.document;
    const uri = URI.parse(event.document.uri);
    const path = uri.path;
    const text = document.getText();

    connection.console.warn(`asking texpresso to open document: ${path}`);
    texpressoProcess.sendCommand("open", [path, text]);
});
```

逐行看：

- `documents.onDidOpen(...)`：用的是**高层 API**（2.3 节），`event.document` 是文档管理器维护的权威对象，它此时已经持有全文。
- `URI.parse(event.document.uri)`（[第 171 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L171)）：把 `file://...` 形式的 URI 字符串解析成一个 URI 对象，`URI` 来自 [src/server.ts:18](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L18) 的 `import { URI } from "vscode-uri"`。
- `document.getText()`：取出整份文档的文本。这是 `Full` 级别的信息量，但只在「打开」这一次发，代价可接受。
- `sendCommand("open", [path, text])`（[第 176 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L176)）：交给协议层。回到 [src/process-manager.ts:108-109](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L108-L109)：

```ts
const message = JSON.stringify([command, ...data]);
this.process?.stdin?.write(message + "\n");
```

即把 `["open", path, text]` 序列化后加一个换行符写进 stdin——正是 u2-l3 讲过的 NDJSON 封包。

#### 4.1.4 代码实践

**实践目标**：亲手观察 `open` 命令在「线缆上」到底长什么样，建立一个对 NDJSON 封包的直觉。本仓库没有测试或示例目录，所以下面是一个**模拟脚本**（示例代码，不依赖真实的 texpresso，仅复刻 `sendCommand` 的编码逻辑）。

**操作步骤**：

1. 新建 `sync-sim.js`（放在任意目录，不写入项目源码树），内容如下（**示例代码**）：

```js
// sync-sim.js —— 模拟 server.ts 三个同步命令的 NDJSON 线缆格式
const path = '/home/user/main.tex';

// 复刻 sendCommand：["command", ...data] + "\n"
function sendCommand(command, data) {
  const message = JSON.stringify([command, ...data]);
  console.log('[stdin ->]', message);
}

// 模拟 onDidOpen：发送整份文本
function sendOpen(text) {
  sendCommand('open', [path, text]);
}

sendOpen('Hello\nWorld\n');
```

2. 运行 `node sync-sim.js`。

**需要观察的现象**：

- 输出一行，形如 `["open","/home/user/main.tex","Hello\nWorld\n"]`。
- 注意文本里的换行符 `\n` 在 JSON 里被转义成 `\n` 两个字面字符——这正是 `JSON.stringify` 的功劳，保证「一条消息 = 一行」的不变量不被文档内部的换行破坏。

**预期结果**：你看到的应该是单行 JSON，且其中文档内所有的真实换行都变成 `\n` 转义序列。这就是为什么 u2-l3 强调「`\n` 是消息边界」不会和「文档内容里的换行」冲突——后者在序列化时已经被转义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `open` 命令要在「打开」时发送全文，而不是等第一次 `didChange` 再发？

**答案**：因为 texpresso 需要一个**完整的初始状态**才能开始渲染。`didChange` 只描述「差异」，没有基准全文的话，texpresso 无从知道「在什么之上做改动」。`didOpen` 携带全文恰好提供了这个基准，之后所有 `didChange` 都在这份基准上叠加。这是增量同步协议的标准设计：先 Full 一次，再 Incremental 多次。

**练习 2**：如果把 `URI.parse(event.document.uri).path` 直接换成 `event.document.uri`，会发生什么？

**答案**：发出去的 `open` 命令里路径会变成 `file:///home/user/main.tex` 这样带 `file://` 前缀的 URI 字符串。texpresso 是按文件系统路径找文件的命令行程序，它多半不认识 `file://` 前缀，会导致找不到文件或渲染失败。所以必须把 URI 规约成纯路径。

### 4.2 onDidChangeTextDocument：增量 change-range 的六元数组

#### 4.2.1 概念说明

用户每敲一个字，编辑器就发一条 `didChange`。在增量模式下，每条变更用一个 `TextDocumentContentChangeEvent` 描述，它有两个核心字段：

- `range`：**改了哪个范围**，用「起止的行列」框定一段连续区域；
- `text`：**把这个范围替换成什么文本**（可以是空串，表示纯删除）。

texpresso-lsp 需要把这个「范围 + 新文本」翻译成 texpresso 能懂的 `change-range` 命令。做法是把 `range` 的四个坐标（起行、起列、止行、止列）和 `text` 摊平成一个**六元数组**，连同路径一起发出去。

> 注意 LSP 的坐标是 **0-based**：第一行是 `0`，一行第一个字符是 `0`。这点很重要，4.2.4 会用它做一道对比题。

这里还有一个本讲最值得品味的细节：作者没有用高层的 `documents.onDidChangeContent`，而是用了低层的 `connection.onDidChangeTextDocument`，并在取文档对象时留了一句 `// can this be out of sync?`（[第 206 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L206)）。4.2.4 的实践会专门剖析它。

#### 4.2.2 核心流程

```
编辑器：用户把第 1 行的 "Hello" 改成 "Hi"
   │  发 textDocument/didChange {
   │      uri,
   │      contentChanges: [{ range: {start:{line:0,c:0}, end:{line:0,c:5}}, text:"Hi" }]
   │  }
   ▼
connection.onDidChangeTextDocument(params)
   │  ① document = documents.get(uri)   // 仅作「是否已打开」的守卫
   │  ② if (!document) return;          // 没打开就忽略
   │  ③ path = URI.parse(uri).path
   │  ④ 遍历 contentChanges：
   │       if (isIncremental(change))   // 确认带 range
   │           change_data = [path, sl, sc, el, ec, change.text]
   ▼
sendCommand("change-range", change_data)
   ▼
stdin 写入:  ["change-range","/home/user/main.tex",0,0,0,5,"Hi"]\n
```

`change_data` 是六元数组（路径 + 4 个坐标 + 新文本）；`sendCommand` 再在前面拼上命令名 `"change-range"`，所以最终线缆上是**七元**数组。

#### 4.2.3 源码精读

处理器本体见 [src/server.ts:204-226](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L204-L226)：

```ts
connection.onDidChangeTextDocument(
    async (params: DidChangeTextDocumentParams) => {
        const document = documents.get(params.textDocument.uri); // can this be out of sync?
        if (!document) return;
        const path = URI.parse(params.textDocument.uri).path;
        params.contentChanges.forEach((change) => {
            if (TextDocumentContentChangeEvent.isIncremental(change)) {
                const change_data = [
                    path,
                    change.range.start.line,
                    change.range.start.character,
                    change.range.end.line,
                    change.range.end.character,
                    change.text,
                ];
                connection.console.warn(`asking texpresso to change: ...`);
                texpressoProcess.sendCommand("change-range", change_data);
            }
        });
    },
);
```

几个要点：

- **用的是低层 API** `connection.onDidChangeTextDocument`（[第 204 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L204)），而不是 `documents.onDidChangeContent`。这是和 4.1/4.3 的不对称之处。`DidChangeTextDocumentParams` 类型来自 [src/server.ts:9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L9) 的导入。
- `documents.get(uri)`（[第 206 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L206)）：**只用来当守卫**——「这个文档在我这儿打开过吗？」如果没打开（返回 `undefined`），直接 `return` 忽略。
- **真正进入命令的数据全部来自原始 `params`**：`change.range` 和 `change.text`，与上面 `documents.get` 取到的 `document` 对象**毫无关系**。这一点是 4.2.4 分析「out of sync」的关键。
- `TextDocumentContentChangeEvent.isIncremental(change)`（[第 210 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L210)）：类型守卫，确认这一项变更带了 `range`（即增量式）。`TextDocumentContentChangeEvent` 类型来自 [src/server.ts:10](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L10)。因为我们握手声明了 `Incremental`，正常情况下这里恒为真；它是防御性的。
- `change_data` 构造（[第 211-218 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L211-L218)）：把路径、四个坐标、新文本摊平成六元数组。
- `forEach`（[第 209 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L209)）：一条 `didChange` 通知里**可能含多项变更**（某些编辑器会合并一次操作里的多处改动），所以遍历 `contentChanges`，每一项各发一条 `change-range`。

#### 4.2.4 代码实践

**实践目标①**：对照 `change_data` 的六元数组，逐项说清每个元素对应 LSP `change` 的哪个字段。

把 `change_data`（六元）和 `sendCommand` 拼好后的线缆消息（七元）列成下表：

| 位置 | `change_data` 下标 | 值（示例） | 对应 LSP 字段 | 含义 |
| --- | --- | --- | --- | --- |
| `["change-range",` | —（命令名） | `"change-range"` | `sendCommand` 第 1 参 | 固定命令名 |
| `, path,` | `[0]` | `"/home/user/main.tex"` | `URI.parse(uri).path` | 文档路径 |
| `, 0,` | `[1]` | `0` | `change.range.start.line` | 起始行（0-based） |
| `, 0,` | `[2]` | `0` | `change.range.start.character` | 起始列（0-based） |
| `, 0,` | `[3]` | `0` | `change.range.end.line` | 结束行（0-based） |
| `, 5,` | `[4]` | `5` | `change.range.end.character` | 结束列（0-based） |
| `, "Hi"]` | `[5]` | `"Hi"` | `change.text` | 替换后的新文本 |

线缆文本：`["change-range","/home/user/main.tex",0,0,0,5,"Hi"]\n`。

**操作步骤**：把 4.1.4 的 `sync-sim.js` 扩展一下，新增一个模拟「把第 1 行 Hello 改成 Hi」的函数（**示例代码**）：

```js
function sendChangeRange(c) {
  const data = [
    path,
    c.range.start.line, c.range.start.character,
    c.range.end.line,   c.range.end.character,
    c.text,
  ];
  sendCommand('change-range', data);
}

sendChangeRange({
  range: { start: { line: 0, character: 0 }, end: { line: 0, character: 5 } },
  text: 'Hi',
});
```

运行 `node sync-sim.js`，确认输出与上表「线缆文本」一致。

**实践目标②**：剖析 [第 206 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L206) 那句 `// can this be out of sync?` 暗示的一致性隐患。

逐层推理：

1. **这句注释之所以存在，是因为作者用了低层 API**。如果像 4.1/4.3 那样用高层 `documents.onDidChangeContent`，回调里的 `event.document` 就是文档管理器**权威且已更新**的视图，根本不存在「会不会不同步」的疑问。而用了 `connection.onDidChangeTextDocument`（低层）之后，作者不得不**手动**再 `documents.get(uri)` 去捞一次文档对象，于是自然产生了「我捞到的这个对象，和当前这条原始通知到底一不一致？」的疑虑。
2. **好消息：即便不同步，也不影响发出的命令**。因为 `change-range` 的数据**全部来自原始 `params`**（`change.range`、`change.text`），`documents.get` 取到的 `document` **只用于第 207 行的 `if (!document) return` 守卫**，没参与命令构造。所以即便文档模型是陈旧的，转发给 texpresso 的内容依然是正确的。
3. **坏消息：守卫本身可能误判**。最坏的情况是 `documents.get(uri)` 返回 `undefined`（管理器认为此文档未打开），从而 `return` 掉一条本该转发的 `change-range`——这会让 texpresso 那边的文档副本**漏掉一次改动**，与服务器的内存文档产生分歧。反过来，也可能放过一条本该忽略的变更。
4. **能否真正出错，取决于 `vscode-languageserver` 内部的注册/触发顺序**：`documents.listen(connection)`（[第 44 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L44)，模块加载早期执行）会让文档管理器在 `connection` 上注册自己的 didChange 处理；而本处的 `connection.onDidChangeTextDocument`（[第 204 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L204)，源码更靠后）又注册了第二个。两个处理器谁先跑、是否共存，是库的内部行为——**本仓库不含 `node_modules`，无法在此直接核实**（待本地验证：可阅读 `vscode-languageserver` 与 `vscode-languageserver-textdocument` 的源码确认 `onDidChangeTextDocument` 是「累加」还是「覆盖」语义，以及管理器内部更新与该回调的先后）。

**结论**：作者用注释诚实地标出了「这地方我拿不准」。从代码看，当前**最脆弱的点是守卫的早退**，而非命令数据本身。这是一处典型的「能用、但不严谨」的技术债，适合作为二次开发的切入点（参见 u3-l4）。

#### 4.2.5 小练习与答案

**练习 1**：一次 `didChange` 通知里的 `contentChanges` 含 3 项，会发出几条 `change-range`？

**答案**：最多 3 条。`forEach` 会对每一项各发一条（见 [第 209-223 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L209-L223)）。说「最多」是因为每项还要过 `isIncremental` 判断——若某项没带 `range`（Full 式），它会被跳过、不发。

**练习 2**：用户做了一个**纯删除**（选中 "Hello" 后按退格），`change-range` 的 `text` 字段会是什么？

**答案**：空串 `""`。纯删除相当于「把这段范围替换成什么都没有」，所以 `change.text` 是 `""`。线缆上会写成 `["change-range",...,0,0,0,5,""]`。texpresso 据此把指定范围清空。

**练习 3（对比题）**：把本模块的坐标处理和 [src/server.ts:243](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L243) 的正向搜索对比——那里写的是 `params.position.line + 1`，而 `change-range` 这里直接传 `change.range.start.line`，没有 `+1`。这说明什么？

**答案**：说明代码对 texpresso 两个命令的行号约定**处理不一致**。`synctex-forward` 做了 `+1`（把 LSP 的 0-based 行转成 1-based 再发给 texpresso），而 `change-range` 没有。两种可能：①texpresso 的 `change-range` 本就用 0-based、`synctex` 用 1-based（即 texpresso 自身两边约定不同，代码是正确的）；②其中一处是 bug。具体是哪种，需对照 texpresso（C 源码）来确认（待确认）。这道题的价值在于：**读源码时要敢于横向对比同类处理**，不一致处往往藏着真问题或重要约定。

### 4.3 onDidClose 与 URI 处理：收尾与路径归一

#### 4.3.1 概念说明

当用户关掉一个文档标签页，编辑器发 `didClose`。对 texpresso 而言，这意味着「这份文档我不再编辑了，你可以释放跟它相关的资源」。所以 `close` 命令**只携带路径，不携带文本**——texpresso 只需要知道「关哪个文件」，不需要再传一遍内容。

本模块还要把贯穿三个事件的 **URI → path 转换**集中讲清楚。你会发现 `URI.parse(...).path` 在 `onDidOpen`（[171 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L171)）、`onDidChangeTextDocument`（[208 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L208)）、`onDidClose`（[199 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L199)）里重复出现，是三个处理器共享的一个「归一化」步骤。

#### 4.3.2 核心流程

```
编辑器关闭 main.tex 标签页
   │  发 textDocument/didClose { uri }
   ▼
documents.onDidClose(event)
   │  ① path = URI.parse(event.document.uri).path   // 三处复用的归一化
   ▼
sendCommand("close", [path])
   ▼
stdin 写入:  ["close","/home/user/main.tex"]\n
```

`close` 命令的 data 数组只有一个元素——路径。线缆上是二元数组 `["close", path]`。

#### 4.3.3 源码精读

处理器本体见 [src/server.ts:198-202](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L198-L202)：

```ts
documents.onDidClose(async (event: TextDocumentChangeEvent<TextDocument>) => {
    const uri = URI.parse(event.document.uri);
    const path = uri.path;
    texpressoProcess.sendCommand("close", [path]);
});
```

对比 4.1 的 `onDidOpen`，结构几乎一样，只是：

- 不再调用 `document.getText()`（不需要文本）；
- 命令名换成 `"close"`；
- data 数组从 `[path, text]` 缩成 `[path]`。

**关于 URI → path 的转换**（三处共享）：

LSP 用 URI 标识文档，最常见的是 `file` 方案，例如：

| 编辑器发来的 `uri` | `URI.parse(uri).path` |
| --- | --- |
| `file:///home/user/main.tex` | `/home/user/main.tex` |
| `file:///C%3A/Users/me/main.tex` | `/C:/Users/me/main.tex` |

`.path` 做了两件事：**剥掉 `file://` 前缀**（方案与主机部分），并**解码百分号转义**（如 `%3A` → `:`）。这样 texpresso 拿到的就是一个干净的、能直接用于文件系统定位的路径字符串。

> 待确认 / 边界情况：在 Windows 上，`vscode-uri` 的 `.path` 会产生形如 `/C:/...` 的路径（带一个前导斜杠）。texpresso 是否能正确处理这种形式，取决于它自身的实现，本仓库无法验证。若你在 Windows 上实践发现路径问题，这是一个值得排查的方向。

**三个处理器对 URI 的处理是重复的**：`onDidOpen`、`onDidChangeTextDocument`、`onDidClose` 各写了一遍 `URI.parse(uri).path`。这是一处可抽取为辅助函数（如 `uriToPath(uri)`）的重复，属于显而易见的简化点（见 u3-l4 的「架构取舍」讨论）。

#### 4.3.4 代码实践

**实践目标**：验证 `URI.parse(...).path` 的归一化效果，并确认 `close` 命令的线缆格式。

**操作步骤**：

1. 在 4.1.4 的 `sync-sim.js` 里引入 `vscode-uri`（若已 `npm install`，可直接 `require`；否则用下面纯手工方式）。最简便的是复刻 `.path` 的核心效果（**示例代码**）：

```js
// sync-sim-close.js —— 演示 close 命令 + URI.path 归一化（示例代码）
// 这里手工模拟 URI.parse(uri).path 的「剥前缀 + 解码」效果，
// 真实项目里应直接用 require('vscode-uri').URI.parse(uri).path
function uriToPath(uri) {
  // 剥掉 file:// 协议头
  let p = uri.replace(/^file:\/\//, '');
  // 解码百分号转义（简化版）
  p = decodeURIComponent(p);
  return p;
}

const path = uriToPath('file:///home/user/main.tex');
console.log('path =', path);                 // /home/user/main.tex
// 复刻 sendCommand
console.log('[stdin ->]', JSON.stringify(['close', path]));
```

2. 运行 `node sync-sim-close.js`。

**需要观察的现象**：

- `path` 输出为 `/home/user/main.tex`（已剥掉 `file://`）。
- 线缆行是 `["close","/home/user/main.tex"]`，注意它**只有两个元素**，没有文本。

**预期结果**：与上表一致。如果你本机已 `npm install` 了依赖，可把手工函数换成真实的 `require('vscode-uri').URI.parse(...).path`，对比两者输出是否相同（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`close` 命令为什么不带文本？如果带上全文会怎样？

**答案**：因为关闭语义只是「通知 texpresso 释放资源」，不需要再同步内容——关闭前的最后一次 `change-range` 已经把文档更新到最新了。带上全文是无用功，既浪费带宽，也可能让 texpresso 误以为「要重新打开/重置」而打乱状态机。

**练习 2**：三个处理器都写了 `URI.parse(uri).path`。如果某天要把路径规则改成「相对路径」，需要改几处？

**答案**：得改**三处**（[171](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L171)、[199](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L199)、[208 行](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L208)）。这正是重复代码的代价：一处逻辑散落三地，修改时容易遗漏。把它抽成一个 `uriToPath(uri)` 辅助函数，就能把改动收敛到一处——这是 u3-l4 会讨论的典型简化点。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**同步翻译官**」小任务。不运行真实 texpresso，仅靠阅读源码 + 上面的模拟脚本作答。

**任务背景**：假设用户在编辑器里对一个**新打开**的 `report.tex` 做了如下操作序列：

1. 打开文件（全文是 `LaTeX\nis\nfun\n`）；
2. 把第 3 行（`fun`）改成 `awesome`；
3. 关闭文件。

**要求**：

1. **写出三条线缆文本**。分别写出 texpresso-lsp 会向 texpresso 的 stdin 写出哪三行 NDJSON。提示：
   - 第 1 步对应 [src/server.ts:176](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L176) 的 `open`；
   - 第 2 步对应 [src/server.ts:211-218](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L211-L218) 的 `change-range`，注意 `fun` 在第 3 行，LSP 行号是 0-based，所以 `start.line` 和 `end.line` 都是 `2`，列分别是 `0` 和 `3`；
   - 第 3 步对应 [src/server.ts:201](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L201) 的 `close`。

2. **用模拟脚本验证**。扩展 4.1.4 / 4.2.4 的 `sync-sim.js`，按上面序列调用 `sendOpen` / `sendChangeRange` / `sendClose` 三个函数，运行 `node sync-sim.js`，对照你手写的三条线缆文本是否一致。

3. **画一张端到端数据流图**。在一张图上标出：编辑器（LSP 客户端）、texpresso-lsp（`documents` 管理器 + 三个处理器 + `sendCommand`）、texpresso 子进程（stdin）三者的位置，并画出三条消息分别从哪个处理器流向 stdin。要求体现「**LSP didOpen/didChange/didClose → 三个处理器 → open/change-range/close 命令**」的翻译关系。

4. **反思题**：上文中第 2 步如果发生在「文件还没 didOpen」的异常时序里（比如某编辑器实现有 bug，先发了 didChange），[src/server.ts:206-207](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L206-L207) 的守卫会怎么处理？texpresso 那边会收到这条 `change-range` 吗？结合 4.2.4 的分析作答。

**预期结果（第 1 题参考）**：

```
["open","/home/user/report.tex","LaTeX\nis\nfun\n"]
["change-range","/home/user/report.tex",2,0,2,3,"awesome"]
["close","/home/user/report.tex"]
```

第 4 题参考：守卫 `documents.get(uri)` 会返回 `undefined`（因为没打开），触发 `if (!document) return`，这条 `change-range` **不会**发给 texpresso。这正是 4.2.4 所说的「守卫早退」的真实后果——它既是一种保护（避免对未知文档发命令），也是一种潜在丢更新风险。

## 6. 本讲小结

- **文档同步 = 翻译层**：三个 LSP 文档事件被翻译成三条 texpresso 命令——`didOpen → open [path, 全文]`、`didChange → change-range [path, 起/止行列, 新文本]`、`didClose → close [path]`，全部经 u2-l3 的 `sendCommand` 写入 stdin。
- **增量同步**：服务器在 [src/server.ts:120](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L120) 声明 `TextDocumentSyncKind.Incremental`，因此 didChange 只携带「范围 + 新文本」而非全文；`TextDocumentContentChangeEvent.isIncremental` 是对此的防御性确认。
- **open 发全文、close 只发路径**：open 是冷启动喂基准全文，close 是通知释放资源，二者职责对称、数据量一多一少。
- **六元数组**：`change-range` 的 data 是 `[path, startLine, startChar, endLine, endChar, text]`，源自 LSP 的 `change.range` 四个坐标加 `change.text`，坐标是 0-based。
- **URI → path 归一化**：`URI.parse(uri).path` 在三个处理器里重复出现，剥掉 `file://` 并解码转义，把 LSP 的 URI 变成 texpresso 认的文件路径；这是一处可抽取的重复。
- **「can this be out of sync?」**：源于作者用低层 `connection.onDidChangeTextDocument`（而非高层 `onDidChangeContent`）+ 手动 `documents.get` 守卫的不对称写法。命令数据本身来自原始 `params` 不受影响，但守卫的早退可能误丢合法变更；真正的语义取决于 `vscode-languageserver` 内部行为（待本地验证）。

## 7. 下一步学习建议

本讲把「文档内容如何流进 texpresso」讲透了。接下来：

- **第 u3-l1「实时预览与编译流程」**：会讲 `onDidSave`（[src/server.ts:179-196](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L179-L196)）——它不属于文档同步，而是「保存即重编译」的预览刷新链路，spawn `texpresso-tonic` 后发 `rescan`。建议带着「文档同步保证 texpresso 有最新内容，编译在此基础上触发」的视角去读。
- **第 u3-l2「SyncTeX 正反向搜索」**：会用到本讲提到的 0-based/1-based 对比（`change-range` 不 `+1` 而 `synctex-forward` `+1`），届时可一并验证那个行号约定疑点。
- **第 u3-l4「架构取舍与二次开发」**：本讲指出的两处技术债——`URI.parse` 的三处重复、`can this be out of sync?` 的不对称写法——都适合在那里作为「重构与扩展」的练习靶子。
- **动手预习**：进入 u3-l1 前，建议用 `grep -n "sendCommand\|\.on(" src/server.ts` 把全部「发命令 / 收事件」的调用点和本讲、u2-l3 的清单对一遍，确认你已能脱口说出每条命令的 data 结构。
