# SyncTeX 正反向搜索

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「反向搜索」与「正向搜索」在 texpresso-lsp 中的两条不同事件链，以及它们各自走的是 LSP 还是子进程通道。
- 读懂 `synctex` 事件监听器，解释 `%f` / `%l` 占位符如何被替换、编辑器命令如何被拼接并 `spawn` 出去。
- 读懂 `onDocumentHighlight` 处理器，解释为什么它「始终返回空数组」却能驱动正向搜索，并说出这种「借壳」设计的副作用。
- 理解 `preview_follow_cursor` 工作区开关在正向搜索链路里的「门禁」作用，以及它的运行期热更新机制。

## 2. 前置知识

在进入本讲前，你需要先建立以下几个心智模型（它们来自前置讲义，这里只做最简回顾）：

- **SyncTeX 是什么**：SyncTeX 是 LaTeX 生态的一个辅助协议/文件格式，它记录「PDF 页面坐标 ⇄ 源码行号」的映射（通常存为 `.synctex.gz`）。有了它，点击 PDF 能跳到源码（反向），移动光标能让 PDF 滚到对应位置（正向）。**关键是：在 texpresso-lsp 里，这个映射的计算完全发生在 `texpresso` 子进程内部，LSP 服务器一行都不算——它只是个快递员。**
- **NDJSON 行协议**（u2-l3）：`texpresso` 子进程的 stdout 每行是一个 JSON 数组 `[command, ...data]`；`TexpressoProcessManager` 按行缓冲、解析后用 `emit(command, data)` 分发；发方向用 `sendCommand(command, data)` 往 stdin 写一行 JSON。本讲会反复用到这套机制。
- **文档同步**（u2-l4）：编辑器把文档事件翻译成 `open` / `change-range` / `close` 命令。本讲的正向搜索和它在同一个「LSP 事件 → texpresso 命令」翻译框架内。
- **两类配置**（u2-l1）：初始化选项（`ServerConfig`，握手时一次性传入）与工作区设置（`WorkspaceSettings`，运行期可热更新）。反向搜索的 `inverse_search` 属于前者，正向搜索的 `preview_follow_cursor` 属于后者——这个差异本讲会重点展开。

一个贯穿全讲的直觉：**反向搜索是「子进程主动通知，服务器去 spawn 编辑器」；正向搜索是「编辑器主动请求，服务器去通知子进程」。** 两个方向的数据流刚好相反，载体也不同（一个走 `spawn`，一个走 LSP 请求）。抓住这点，后面的源码就很好读。

## 3. 本讲源码地图

本讲只涉及一个主源码文件，但它会调用另两个文件里的基础设施：

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| `src/server.ts` | LSP 服务器主体 | `synctex` 事件监听器（反向搜索）、`onDocumentHighlight` 处理器（正向搜索）、默认配置 |
| `src/process-manager.ts` | 子进程管理 + NDJSON 协议 | `emit("synctex", data)` 的事件来源、`sendCommand` 的写入逻辑 |
| `src/types.ts` | 类型定义 | `inverse_search` 接口与 `%f` / `%l` 占位符约定 |

## 4. 核心概念与源码讲解

### 4.1 synctex 事件与反向搜索

#### 4.1.1 概念说明

**反向搜索（inverse search）** 的场景是：用户在 `texpresso` 弹出的 PDF 预览窗口里按住 Ctrl/Cmd 点击某个位置，希望编辑器跳到对应的源码行。

这件事的完整计算（把「PDF 上的某个点击坐标」翻译成「源码文件路径 + 行号」）由 `texpresso` 子进程完成——它手上有 `.synctex` 映射数据。算完后，`texpresso` 把结果作为一条 **NDJSON 消息** 发到自己的 stdout：

```
["synctex", "<源码路径>", <行号>]
```

texpresso-lsp 要做的只有三步：

1. 收到这条消息（经 `TexpressoProcessManager` 解析后变成一次 `"synctex"` 事件）。
2. 把路径和行号塞进用户配置的编辑器命令里。
3. `spawn` 这个编辑器命令，让编辑器在对应文件:行打开。

注意第三步：**服务器是把编辑器当作子进程拉起来的，这条链路根本不走 LSP。** 这是整个项目里少有的「服务器主动向外 reach out」的地方，和正向搜索（走 LSP 请求）形成鲜明对比。

#### 4.1.2 核心流程

反向搜索的端到端时序：

```
texpresso PDF 窗口
   │  (用户 Ctrl/Cmd 点击)
   ▼
texpresso 子进程：用 .synctex 算出 (path, line)
   │  stdout 写一行: ["synctex", path, line]\n
   ▼
TexpressoProcessManager：行缓冲 → JSON.parse → emit("synctex", [path, line])
   │  (EventEmitter 分发，见 u2-l3)
   ▼
server.ts：texpressoProcess.on("synctex", ...) 触发
   │  读取 inverse_search 配置，替换 %f/%l
   ▼
spawn(command, subs_args)  ← 把编辑器作为子进程启动
   │
   ▼
编辑器打开 path 的第 line 行
```

这里有一个值得记的结论：**`"synctex"` 这个事件名不是 texpresso-lsp 定义的，而是协议层「盲转发」的结果。** 回顾 u2-l3，process-manager 对 stdout 每一行的处理是「第 0 位当命令名，其余当数据，原样 `emit`」——它不认识任何命令名。所以只要 `texpresso` 那边发 `["synctex", ...]`，这边就自然出现一个 `"synctex"` 事件。这正是「薄封装」的体现。

#### 4.1.3 源码精读

先看事件是怎么从子进程冒出来的（这是 u2-l3 讲过的协议层，这里只取关键几行）：

process-manager 把每行 JSON 解析后，用第 0 位作事件名、其余作 payload 发出去：

[src/process-manager.ts:41-45](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L41-L45) — 逐行解析 stdout：取出 `command_list[0]` 当命令名、`command_list.slice(1)` 当数据，调用 `this.emit(command, data)`。这一行就是 `"synctex"` 事件的诞生地。

再看 server.ts 里对这个事件的监听与处理（反向搜索的核心）：

[src/server.ts:95-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L95-L110) — 反向搜索处理器：先打一条 `warn` 日志记录收到的原始 `data`，然后从 `data[0]` / `data[1]` 取出路径与行号，读取用户配置的 `inverse_search.command` 与 `inverse_search.arguments`，做占位符替换后 `spawn(command, subs_args)` 启动编辑器。

这里有两个细节值得圈出：

- **`data` 是无类型的（`any`）**。因为 `emit` 的参数源自 `JSON.parse(line)`，类型系统无法约束。所以 `data[0]`（路径）和 `data[1]`（行号）都没有编译期保证——这既是「薄封装」的代价，也埋下了鲁棒性隐患（见 4.2）。
- **`spawn(command, subs_args)` 没有附加任何监听器**。与 `onDidSave` 里 spawn `texpresso-tonic` 时挂了 `.on("exit", ...)`（见 u3-l1）不同，这里的 spawn 是真正的「点火就忘」（fire-and-forget）：服务器拉起编辑器后既不读它的输出、也不关心它何时退出——因为编辑器是个交互式长驻进程，服务器本就不该管它的生命周期。

占位符替换的具体机制（第 99–105 行）放到 4.2 单独精读，因为它值得一个独立的最小模块。

#### 4.1.4 代码实践

**实践目标**：在不真正启动 texpresso 的前提下，验证 `"synctex"` 事件如何被监听、如何触达 `spawn`。

**操作步骤**：

1. 重新读一遍 [src/server.ts:95-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L95-L110)，确认监听器注册在 `onInitialize` 回调**内部**（第 46–131 行的 `onInitialize` 函数体里）。这意味着：监听器在握手成功、子进程启动后才挂上，握手失败时根本不会注册。
2. 用如下「示例代码」单独模拟这条事件链（这是一个最小 Node 脚本，复刻 process-manager 的 emit 与 server.ts 的监听，不属于项目原有代码）：

   ```js
   // 示例代码：模拟反向搜索的事件链
   const { EventEmitter } = require("events");
   const { spawn } = require("child_process");

   // 仿照 TexpressoProcessManager 继承 EventEmitter
   const fakeProcess = new EventEmitter();

   // 仿照 server.ts 第 95-110 行的监听器
   const inverse_search = { command: "echo", arguments: ["%f:%l"] };
   fakeProcess.on("synctex", (data) => {
     const [path, line] = data;
     const subs = inverse_search.arguments.map((a) =>
       a.replace("%f", path).replace("%l", line)
     );
     spawn(inverse_search.command, subs, { stdio: "inherit" }); // echo 只是把结果打到stdout
   });

   // 仿照 texpresso 子进程发出的一条消息
   fakeProcess.emit("synctex", ["/abs/main.tex", 42]);
   ```

3. 把上面的 `command` 从 `"echo"` 换成 `""`（空字符串）再跑一次。

**需要观察的现象**：第 2 步会打印 `/abs/main.tex:42`，证明事件链与替换逻辑都通了；第 3 步会因为 `spawn("")` 抛错而崩溃，且**这个错误发生在事件回调内部、没有被 try/catch 包裹**。

**预期结果**：你能清楚地看到反向搜索的整条链路；同时能体会到 4.1.3 提到的鲁棒性隐患——一旦 `command` 非法或 `data` 缺字段，`synctex` 回调里没有 `try/catch` 兜底（这与 `onDocumentHighlight` 里包了 `try/catch` 的风格不同，留待 u3-l3 系统讨论）。

> 注：上面是「源码阅读 + 离线模拟型实践」，不需要真实的 texpresso 或 PDF。

#### 4.1.5 小练习与答案

**练习 1**：反向搜索的 `spawn` 为什么不像 `onDidSave` 那样挂 `.on("exit", ...)`？

> **参考答案**：因为反向搜索 spawn 的是**编辑器**（交互式、长驻、由用户控制生命周期），服务器既不读它的输出、也不需要等它退出去触发后续动作；而 `onDidSave` spawn 的 `texpresso-tonic` 是**一次性编译进程**，服务器需要靠它的 `exit` 事件来知道「编译结束，该发 `rescan` 了」（见 u3-l1）。

**练习 2**：如果用户没有在初始化选项里提供 `inverse_search`，反向搜索点击 PDF 后会发生什么？

> **参考答案**：会回落到默认值 `defaultInitOpts.inverse_search`，即 `{ command: "zed", arguments: ["%f:%l"] }`（见 [src/server.ts:23-26](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L23-L26)）。于是服务器会尝试 `spawn("zed", ["<path>:<line>"])`——如果用户机器上没装 zed，spawn 会触发 `error` 事件并报错。

---

### 4.2 占位符替换与命令拼接

#### 4.2.1 概念说明

不同编辑器在命令行「打开文件并定位到某一行」的写法各不相同：

- zed：`zed <file>:<line>`
- VS Code：`code -g <file>:<line>`（或 `code --goto`）
- codium：`codium -g <file>:<line>`
- vim：`vim "+call cursor(<line>,1)" <file>`

texpresso-lsp 不可能为每种编辑器硬编码。它的做法是：让用户在 `inverse_search.arguments` 里写一个**字符串数组模板**，用两个占位符：

- `%f` —— 代表源码文件路径（file）
- `%l` —— 代表行号（line）

服务器收到具体的 `(path, line)` 后，对每个参数字符串做一次替换，再把替换后的数组连同 `command` 一起 `spawn`。这是一个非常轻量但够用的模板机制，约定记录在类型定义里：

[src/types.ts:18-21](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L18-L21) — `inverse_search` 接口：`command` 是可执行文件名，`arguments` 是字符串数组，注释明确写了「use `%f` and `%l` as placeholders for file and line number」。

#### 4.2.2 核心流程

替换逻辑只有一行，但细节值得拆开看。对每个参数 `arg`，依次执行：

```
arg.replace("%f", path).replace("%l", line)
```

即「先把 `%f` 换成路径，再把 `%l` 换成行号」，然后 `spawn(command, 替换后的数组)`。

举几个具体例子（默认配置是 `command:"zed", arguments:["%f:%l"]`，假设 `path="/abs/main.tex"`, `line=42`）：

| `inverse_search` 配置 | 替换后的 `subs_args` | 最终执行命令 |
| --- | --- | --- |
| `{ command:"zed", arguments:["%f:%l"] }` | `["/abs/main.tex:42"]` | `zed /abs/main.tex:42` |
| `{ command:"code", arguments:["-g","%f:%l"] }` | `["-g","/abs/main.tex:42"]` | `code -g /abs/main.tex:42` |
| `{ command:"codium", arguments:["-g","%f:%l"] }` | `["-g","/abs/main.tex:42"]` | `codium -g /abs/main.tex:42` |

#### 4.2.3 源码精读

替换的源头是配置合并——握手时用 `??` 把用户的 `inverse_search` 整体替换默认值：

[src/server.ts:58-60](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L58-L60) — 把用户传入的 `inverse_search`（若非 null/undefined）整体赋给 `connection.init_options.inverse_search`。注意 u2-l1 提醒过的陷阱：这是「整对象合并」，如果用户只给了 `command` 没给 `arguments`，`arguments` 不会被默认值补齐，运行时会得到 `undefined`，到 4.2.3 这步 `.map` 就会抛错。

替换本身在这一行：

[src/server.ts:99-105](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L99-L105) — 取 `data[0]` 为路径、`data[1]` 为行号；用 `.map` 遍历 `arguments`，对每个 `arg` 做 `arg.replace("%f", path).replace("%l", line)`，得到替换后的 `subs_args`。

这里有三个容易被忽略的细节，都和「`data` 是 `any`」以及「`String.prototype.replace` 的行为」有关：

1. **行号类型不确定**。`line = data[1]` 来自 JSON 解析，它可能是 `number`（如 `42`）也可能是 `string`（如 `"42"`，取决于 texpresso 那边怎么编码）。`.replace("%l", line)` 的第二参数期望是字符串，若是 `number`，JS 会隐式 `ToString`。功能上能跑，但这是「无类型约束」带来的隐患。
2. **`replace` 只替换第一个匹配**。`String.prototype.replace` 当第一个参数是字符串（而非正则）时，只替换**首次**出现。所以 `["%f:%l"]` 没问题，但若有人写 `["%f:%l:%l"]`，第二个 `%l` 不会被替换。正常使用不会触发，但属于隐藏的语义边界。
3. **链式替换的顺序依赖**。先替换 `%f` 再替换 `%l`。如果某个真实的文件路径里恰好含有子串 `%l`（极罕见），先填进去的路径会被第二步的 `.replace("%l", line)` 误伤。这是个理论上的小 bug，实践中几乎不会触发。

替换完，拼好命令并 spawn：

[src/server.ts:106-109](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L106-L109) — 打一条 `log` 记录「即将执行的完整命令」（用 `subs_args.join(" ")` 拼成可读字符串，方便调试），然后 `spawn(command, subs_args)` 真正启动编辑器。这条日志是排查「为什么编辑器没跳过去」的第一手信息——在 LSP 输出窗口里能看到它。

#### 4.2.4 代码实践

**实践目标**：为你常用的编辑器编写一份 `inverse_search` 配置，并离线验证 `%f` / `%l` 的替换结果。

**操作步骤**：

1. 在 [src/server.ts:99-105](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L99-L105) 的逻辑基础上，用下面这段「示例代码」离线模拟替换（不依赖 texpresso）：

   ```js
   // 示例代码：验证 %f/%l 替换
   function buildInverseCommand(inverse_search, path, line) {
     const subs_args = inverse_search.arguments.map((arg) =>
       arg.replace("%f", path).replace("%l", line)
     );
     return `${inverse_search.command} ${subs_args.join(" ")}`;
   }

   // 改成你自己的编辑器配置
   const configs = [
     { command: "zed",   arguments: ["%f:%l"] },          // zed
     { command: "code",  arguments: ["-g", "%f:%l"] },    // VS Code
     { command: "codium",arguments: ["-g", "%f:%l"] },    // VSCodium
   ];
   for (const c of configs) {
     console.log(buildInverseCommand(c, "/home/me/paper/main.tex", 42));
   }
   ```

2. 预测每行输出，再运行验证。
3. **挑战**：试着给 vim 写一份配置（提示：`vim "+call cursor(%l,1)" %f`，注意这里 `%f` 在最后），用上面的函数跑一下，观察输出是否合理。

**需要观察的现象**：三条输出分别是

```
zed /home/me/paper/main.tex:42
code -g /home/me/paper/main.tex:42
codium -g /home/me/paper/main.tex:42
```

vim 那条会输出 `vim +call cursor(42,1) /home/me/paper/main.tex`（注意 `"` 引号在 `spawn` 的参数数组里会作为字面字符保留——这其实是 vim 配置用数组模板表达时的一个瑕疵，真实使用可能需要包裹 shell，留给你思考）。

**预期结果**：你能为任意编辑器写出可用的 `inverse_search` 配置，并理解替换发生在「数组每个元素内部」而非「整体字符串」上。若本地装了 texpresso-lsp 与某编辑器，可进一步把配置写进初始化选项、在 PDF 里 Ctrl/Cmd 点击，去 LSP 输出窗口对照 [src/server.ts:106-108](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L106-L108) 的日志验证——若无法本地运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `arguments` 设计成字符串数组，而不是单个空格分隔的字符串？

> **参考答案**：因为 `spawn(command, args)` 的第二参数本身就是字符串数组，每个元素是一个独立的 argv 项，**不经过 shell 解析**。这样含空格的路径（如 `/home/me/my paper/main.tex`）不会被错误地拆成两个参数。如果用单字符串再手工分词，就会重新引入 shell 转义的麻烦。

**练习 2**：如果用户的 `inverse_search = { command: "code" }`（漏写了 `arguments`），反向搜索触发时会怎样？

> **参考答案**：由于 [src/server.ts:58-60](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L58-L60) 是「整对象合并」，缺省的 `arguments` 不会从默认值补齐，运行时 `connection.init_options.inverse_search.arguments` 为 `undefined`，调用 `.map` 会抛 `Cannot read properties of undefined`。这正是 u2-l1 指出的「整对象合并」缺陷的一个具体后果。

---

### 4.3 synctex-forward 与 documentHighlight、preview_follow_cursor 开关

#### 4.3.1 概念说明

**正向搜索（forward search）** 的场景是：用户在编辑器里移动光标，希望 texpresso 的 PDF 预览自动滚到光标所在源码位置——即「预览跟随光标」（preview follows cursor）。

这条链路要解决一个尴尬的问题：**LSP 协议里并没有一个标准的「光标移动了」通知。** LSP 有 `textDocument/didChange`（文档内容变了），但光标单纯移动、内容没变时，编辑器不会主动告诉服务器。

texpresso-lsp 用了一个巧妙的「借壳」办法：**借用 `textDocument/documentHighlight` 请求。** 这个请求的本职工作是「当光标停在某个符号上时，高亮文档里所有对该符号的引用」。但关键是——**这个请求里携带了当前光标位置 `params.position`**。而「当前光标位置」恰恰是正向搜索唯一需要的输入。

于是服务器声明自己是个 `documentHighlightProvider`（u1-l4 提到的「借壳」），然后在这个请求的处理器里偷偷干私活：读取光标位置、发给 texpresso 做 forward SyncTeX，最后**返回一个空数组**——意思是「我没有真正的高亮要显示」。编辑器拿到空数组，什么高亮都不画；而 texpresso 那边的 PDF 已经滚好了。

这套机制（`synctex-forward` 命令 + `documentHighlight` 借壳 + `preview_follow_cursor` 开关）是在同一个提交 `9680772`（"Add workspace setting 'preview_follow_cursor'"）里一起引入的，这也解释了为什么 README 把「Forward search following cursor」标成半完成状态（`[ ]`）——它是个靠 hijack 实现的近似方案。

#### 4.3.2 核心流程

正向搜索的端到端时序（注意与反向搜索对比）：

```
编辑器：光标移动（在某些时机）
   │  发 textDocument/documentHighlight 请求，带 params.position
   ▼
server.ts：onDocumentHighlight 处理器触发
   │  ① 检查 workspace_config.preview_follow_cursor
   │     若为 false → 直接 return []（不发命令）
   ▼
   │  ② 把 0-based LSP 行号转成 1-based：lineNumber = position.line + 1
   ▼
   │  ③ sendCommand("synctex-forward", [filePath, lineNumber])
   │     写一行 JSON 到 texpresso 的 stdin
   ▼
texpresso 子进程：根据 .synctex 把源码行映射到 PDF 坐标，滚动预览
   │
   ▼
server.ts：return []  ← 永远返回空数组，编辑器不画任何高亮
```

这里有一个核心的**行号约定差异**需要数学化：

\[ L_{\text{synctex}} = L_{\text{LSP}} + 1 \]

LSP 的 `position.line` 是 **0-based**（第一行是 0），而 SyncTeX / LaTeX / 人类习惯都是 **1-based**（第一行是 1）。所以发给 texpresso 之前必须 `+1`。反向搜索方向相反——texpresso 给我们的行号已经是 1-based，直接塞给编辑器命令即可（编辑器 CLI 普遍也用 1-based），所以反向那条链路里看不到 `±1` 转换。这个不对称是个易错点。

#### 4.3.3 源码精读

先看「门禁」——`preview_follow_cursor` 开关。它是工作区设置，默认为 `true`：

[src/server.ts:29-31](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L29-L31) — 默认工作区设置 `preview_follow_cursor: true`。

[src/server.ts:232-237](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L232-L237) — 在 `onDocumentHighlight` 开头检查开关：若为 `false`，打一条 `log` 说明「已忽略」，立即 `return []`，根本不会发 `synctex-forward`。这是正向搜索的总闸。

这个开关之所以放在工作区设置（而非初始化选项），是因为它**设计成可运行期热更新**。回顾 u2-l1：`onInitialized` 首次拉取、`onDidChangeConfiguration` 监听变化，二者都写入 `connection.workspace_config.preview_follow_cursor`。所以用户可以在编辑器设置里随时开关「跟随光标」，无需重启服务器——这与 `inverse_search`（初始化选项，握手后不可变）形成对照。

再看核心的 `onDocumentHighlight` 处理器：

[src/server.ts:229-268](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L229-L268) — 完整的 `onDocumentHighlight` 处理器。它先过 `preview_follow_cursor` 门禁，然后在 `try` 块里：解析 URI 取路径、把行号 `+1`、发 `synctex-forward` 命令；`catch` 里只打错误日志；**最后无条件 `return []`**。

关键的行号转换在这行：

[src/server.ts:243](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L243) — `const lineNumber = params.position.line + 1;` 把 0-based LSP 行号转成 1-based，供 SyncTeX 使用。

发命令到子进程：

[src/server.ts:250-253](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L250-L253) — `sendCommand("synctex-forward", [filePath, lineNumber])`。这一行落到 [src/process-manager.ts:103-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L103-L110) 的 `sendCommand`：先做进程健康守卫（`isRunning` 且 stdin/stdout 可用），再把 `["synctex-forward", filePath, lineNumber]` `JSON.stringify` 后加一个 `\n` 写进 stdin。

最后是「永远返回空数组」这一行——它是整个借壳设计的灵魂：

[src/server.ts:264-267](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L264-L267) — 注释写得很直白：「Return empty array since we're not providing actual highlights, just using this as a trigger for the synctex-forward command」。即：服务器声称自己是 documentHighlight 提供方，但真正的高亮一个都不给，只是拿这个请求当光标位置触发器。

这个借壳设计有三个值得记的**副作用与局限**：

1. **请求频率取决于编辑器策略**。`documentHighlight` 何时发、发多发少，完全由编辑器决定（有的在光标停留几百毫秒后发，有的几乎不发）。这意味着「跟随光标」的实时性强依赖于编辑器，并非服务器能控制。这也是 README 把它标为「未完成」的根本原因。
2. **占用了 `documentHighlightProvider` 能力**。[src/server.ts:121](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L121) 声明 `documentHighlightProvider: true`，意味着这个服务器永远无法再提供「真正的」符号高亮——能力被 hijack 占用了。
3. **对编辑器是一种「欺骗」**。编辑器以为服务器会返回有意义的高亮，结果每次都拿到空数组。好在空数组是合法返回值，不会报错，但编辑器侧可能为每次请求付出调度成本。

#### 4.3.4 代码实践

**实践目标**：解释「为什么 `onDocumentHighlight` 始终返回空数组却能驱动正向搜索」，并亲手验证 `preview_follow_cursor` 的开关效果。

**操作步骤**：

1. **阅读型任务——回答「为什么返回 `[]` 还有效」**。重新读 [src/server.ts:229-268](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L229-L268)，把处理器分成「副作用部分」和「返回值部分」两块，标出各自的起止行。你应该能得出结论：正向搜索的真正动作（`sendCommand("synctex-forward", ...)`）发生在第 250–253 行，属于**副作用**；而 `return []`（第 266 行）只是给编辑器一个「合法但空」的回复。编辑器只关心返回值合法与否，不关心服务器在处理过程中偷偷干了什么——这就是借壳能成立的原因。
2. **配置型任务——验证开关**。在你的编辑器工作区设置里加 `"texpresso.preview_follow_cursor": false`（见 [README.md:43-52](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L43-L52) 的格式）。结合 [src/server.ts:232-237](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L232-L237) 与 [src/server.ts:153-167](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L153-L167)（`onDidChangeConfiguration`）预测：切换这个开关是否需要重启服务器？切换后移动光标，PDF 还会不会跟随？
3. **离线模拟——把整个正向链路串起来**。下面这段「示例代码」把 `onDocumentHighlight` 的核心逻辑（门禁 + 行号转换 + 发命令）抽出来单跑：

   ```js
   // 示例代码：模拟 onDocumentHighlight 的核心逻辑
   const workspace_config = { preview_follow_cursor: true };
   const sentCommands = []; // 伪造的 sendCommand 记录

   function onDocumentHighlight(uri, position /* {line, character} */) {
     if (!workspace_config.preview_follow_cursor) {
       console.log("ignored: preview_follow_cursor disabled");
       return [];
     }
     const filePath = uri;
     const lineNumber = position.line + 1; // 0-based → 1-based
     sentCommands.push(["synctex-forward", filePath, lineNumber]);
     return []; // 永远空数组
   }

   // 模拟编辑器在 main.tex 第 41 行（0-based）发来请求
   console.log(onDocumentHighlight("/abs/main.tex", { line: 41, character: 0 }));
   console.log("sent:", sentCommands);
   // 关掉开关再试
   workspace_config.preview_follow_cursor = false;
   console.log(onDocumentHighlight("/abs/main.tex", { line: 41, character: 0 }));
   console.log("sent after disable:", sentCommands);
   ```

**需要观察的现象**：第 3 步第一次调用返回 `[]` 但 `sentCommands` 里多了 `["synctex-forward", "/abs/main.tex", 42]`（注意 41→42 的转换）；关掉开关后第二次调用返回 `[]` 且 `sentCommands` 不再增长。

**预期结果**：你清楚地看到「返回值（空数组）」与「副作用（发命令）」是两回事——空数组不阻碍副作用发生，这就是借壳设计的机制。配置开关的运行期热更新（第 2 步）——若本地有环境，验证切换无需重启；若无环境，结合源码逻辑可断言「无需重启」，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么不新发明一个 LSP 请求或自定义通知来报告光标位置，而要借用 `documentHighlight`？

> **参考答案**：因为标准 LSP 没有现成的「光标移动」通知，而自定义请求需要编辑器侧配合实现客户端代码——这与 texpresso-lsp「尽量薄、尽量不依赖编辑器定制」的哲学冲突。`documentHighlight` 是标准请求、几乎所有 LSP 编辑器都会在光标停留时自动发、且自带光标位置，是最省事的「免费」光标信号源。代价是占用了 documentHighlight 能力、且频率不可控。

**练习 2**：`onDocumentHighlight` 里有 `try/catch`，但 4.1 的 `synctex` 监听器里没有。如果 `sendCommand` 抛错（比如子进程已退出），两边各会发生什么？

> **参考答案**：正向这边，[src/server.ts:258-262](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L258-L262) 的 `catch` 会把错误打到 LSP 日志，处理器仍正常 `return []`，对编辑器无影响——错误被吞掉。反向那边没有 `try/catch`，若 `spawn` 抛错会直接冒泡成未捕获异常（在事件回调里），行为更剧烈。这种不一致是当前代码的鲁棒性技术债，留待 u3-l3 系统梳理。

**练习 3**：如果把 [src/server.ts:243](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L243) 的 `+ 1` 去掉，正向搜索会出现什么偏差？

> **参考答案**：发出去的行号会比真实光标行少 1，texpresso 的 PDF 会滚到「上一行」对应的预览位置。这是一个**一致的 1 行偏移**——功能没坏，但永远差一行，这正是 0-based 与 1-based 约定不匹配的典型后果。

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个综合任务：

**任务：为 VS Code（或 codium）同时配好正反向搜索，并画出两条链路的对比图。**

1. **写初始化选项**（反向搜索）。参照 [README.md:28-40](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L28-L40) 与 [src/types.ts:18-21](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L18-L21)，为 `code` / `codium` 写出 `inverse_search` 配置（提示：`command` 用编辑器可执行文件名，`arguments` 用 `["-g", "%f:%l"]`）。用 4.2.4 的「示例代码」离线验证替换结果。
2. **写工作区设置**（正向搜索）。参照 [README.md:43-52](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L43-L52)，把 `texpresso.preview_follow_cursor` 设为 `true`，并说明它走的是哪条配置链路（`onInitialized` / `onDidChangeConfiguration`）。
3. **画对比图**。画一张表或一张图，从「触发者、数据载体、是否走 LSP、是否 spawn、行号是否需要 ±1 转换、配置类型」六个维度对比反向与正向搜索。预期结论大致如下：

   | 维度 | 反向搜索 | 正向搜索 |
   | --- | --- | --- |
   | 触发者 | texpresso 子进程（emit `synctex`） | 编辑器（发 `documentHighlight` 请求） |
   | 数据载体 | 子进程 stdout → EventEmitter | LSP 请求参数 |
   | 是否走 LSP | 否 | 是 |
   | 是否 spawn | 是（spawn 编辑器命令） | 否（sendCommand 写 stdin） |
   | 行号转换 | 不需要（已是 1-based） | 需要（`+1`） |
   | 配置类型 | 初始化选项 `inverse_search` | 工作区设置 `preview_follow_cursor` |

4. **思考题（选做）**：当前的 `preview_follow_cursor` 是「全有或全无」的布尔开关。如果要让「跟随光标」只在用户主动按下某个快捷键时触发一次（而不是持续跟随），你会怎么改造？提示：想想 `documentHighlight` 借壳之外，还有没有别的 LSP 请求/通知适合做一次性触发器（如 `textDocument/definition` 或自定义命令），以及分别要在协议两端（`emit` 与 `sendCommand`）改什么。这道题为 u3-l4「架构取舍与二次开发」做铺垫。

## 6. 本讲小结

- **两条链路方向相反、载体不同**：反向搜索是「子进程 emit `synctex` 事件 → 服务器 `spawn` 编辑器命令」，**不走 LSP**；正向搜索是「编辑器发 `documentHighlight` 请求 → 服务器 `sendCommand("synctex-forward")` 写子进程 stdin」，**走 LSP**。
- **texpresso-lsp 不计算 SyncTeX 映射**：`.synctex` 的坐标↔行号换算全在 `texpresso` 子进程内完成，服务器只做 `%f`/`%l` 替换、行号 `+1` 转换与消息搬运——这是「薄封装」哲学在搜索功能上的再一次体现。
- **`%f`/`%l` 是数组级模板**：替换发生在 `inverse_search.arguments` 每个字符串元素内部，配合 `spawn(command, args)` 的数组接口，天然规避了 shell 转义问题；但「整对象合并」让漏写 `arguments` 时不会被默认值补齐。
- **正向搜索靠 `documentHighlight` 借壳**：服务器声明 `documentHighlightProvider: true`，在处理器里用 `params.position` 触发 `synctex-forward`，然后**返回空数组**——返回值与副作用分离是这套 hijack 成立的关键。
- **行号有 0-based/1-based 不对称**：正向需 `position.line + 1`，反向不需要转换，易错点。
- **两类配置的运行期差异**：`inverse_search` 是初始化选项（握手后不可变），`preview_follow_cursor` 是工作区设置（`onDidChangeConfiguration` 热更新），正向搜索的总闸由后者担任。

## 7. 下一步学习建议

- 本讲提到的「`synctex` 回调缺 `try/catch`、`onDocumentHighlight` 有 `try/catch`」的不一致，正是 **u3-l3 错误处理与进程生命周期** 的入口——建议接着读它，系统梳理进程 error/stderr/exit 事件转发、`sendCommand` 守卫、`parse-fail` 容错与 `onShutdown` 的协同。
- 如果你对「为什么用 documentHighlight 借壳」「这套实现有哪些技术债」「如何新增一对 texpresso 命令」感兴趣，**u3-l4 架构取舍与二次开发** 会从整体视角回答这些问题，并把本讲综合实践里的「一次性触发器」改造讲透。
- 想再巩固协议层基础，可重读 **u2-l3（NDJSON 行协议）** 里 `emit` 与 `sendCommand` 的对称性——本讲的 `synctex` 事件与 `synctex-forward` 命令正是这对对称收发的两个具体实例。
