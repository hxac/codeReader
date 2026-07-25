# 错误处理与进程生命周期

## 1. 本讲目标

本讲把 `texpresso-lsp` 服务器里「出错时怎么办」「进程怎么生、怎么死」这条横切线单独抽出来讲清楚。学完本讲你应该能够：

1. 说清 `texpresso` 子进程的 `error` / `stderr` / `exit` 三类事件，是如何从 `process-manager.ts` 一路转发到 LSP 客户端日志的。
2. 区分代码里两种截然不同的「错误兜底」策略——`try/catch` 包住后吞掉打日志，与 `try/catch` 包住后继续 `throw` 让整个握手失败。
3. 理解 `sendCommand` 的健康守卫为什么是「薄封装」里唯一一道硬防线。
4. 解释 JSON 解析失败时伪造的 `["parse-fail", line]` 为什么不会让服务器崩溃。
5. 判断 `start()` / `stop()` / `onShutdown` 三者如何协同，以及 `stop()` 在进程已经死掉时被调用是否安全。

本讲是 `u2-l2`（进程管理器）和 `u2-l3`（JSON 行协议）的直接延伸：前两讲讲了「正常情况下进程怎么跑、命令怎么收发」，本讲专讲「不正常情况下系统如何体面地活着或退场」。

## 2. 前置知识

阅读本讲前，你需要先建立以下心智模型（这些都在前置讲义里讲过，这里只做一句话回顾）：

- **三层架构**：编辑器（LSP 客户端）⇄ `texpresso-lsp`（翻译官）⇄ `texpresso` 子进程。本讲聚焦「翻译官 ⇄ 子进程」这一段。
- **`TexpressoProcessManager` 是包装器**：它用 Node 的 `child_process.spawn` 拉起 `texpresso` 子进程，并继承 `EventEmitter`，把底层 `ChildProcess` 的事件转发成自身事件。见 [src/process-manager.ts:4-7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L4-L7)。
- **NDJSON 协议**：子进程 stdout 每行一个 JSON 数组 `[command, ...data]`，第 0 位是命令名，其余是数据。见 [src/process-manager.ts:27-46](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L27-L46)。
- **`connection` 是混入对象**：`...createConnection(ProposedFeatures.all)` 把 LSP 库方法混入，`connection.console` 就是其中之一，`console.error / warn / info / log` 都会把消息通过 LSP 的 `window/logMessage` 通知发回编辑器的输出面板。见 [src/server.ts:33-38](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L33-L38)。

补充一个 Node.js 的关键常识，本讲会反复用到：

> **EventEmitter 的 `error` 事件是「致命」的。** 如果一个 EventEmitter 发出 `'error'` 事件而没有任何监听器，Node 会直接抛出未捕获异常，进程崩溃。其它事件名（包括自定义的 `exit`、`stderr`、`parse-fail`）没有监听器时只会被静默丢弃，不会崩。

这条规则解释了本讲里好几个「为什么必须这么写」的设计。

## 3. 本讲源码地图

本讲只涉及两个文件，但要把它们当作「一组对话」来看：

| 文件 | 角色 | 本讲关注的部分 |
| --- | --- | --- |
| [src/process-manager.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts) | 子进程包装器 | 事件转发（error/stderr/exit）、stdout 解析容错、`start`/`stop` 生命周期、`sendCommand` 守卫 |
| [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) | LSP 服务器 | 事件监听与日志转发、各处 `try/catch` 边界、`onShutdown` 优雅停止 |

一句话定位：`process-manager.ts` 负责「产生」错误与生命周期事件，`server.ts` 负责「消费」这些事件——把它们变成 LSP 日志、决定要不要让握手失败、决定要不要清理进程。

## 4. 核心概念与源码讲解

### 4.1 事件转发：从子进程到 LSP 日志

#### 4.1.1 概念说明

`texpresso` 是一个独立的外部进程，它可能以多种方式「出状况」：

- **`error` 事件**：子进程本身没能正常启动，或底层管道出问题（例如 `texpresso` 可执行文件根本不在 PATH 里、没有执行权限）。这是 Node `ChildProcess` 级别的失败。
- **`stderr` 事件**：子进程启动成功了，但它往标准错误流写了内容。这通常意味着 `texpresso` 自己检测到了某个问题（如找不到 `.tex` 文件、字体缺失），只是用 stderr 抱怨，进程未必挂掉。
- **`exit` 事件**：子进程退出了，附带退出码 `code` 和信号 `signal`。`code === 0` 是正常结束，非 0 或被信号杀死（如 `SIGTERM`）则是异常结束。

这三类事件含义不同、严重程度不同，但 `texpresso-lsp` 选择把它们**统一转发到同一个出口**：LSP 客户端的日志面板。这样做的好处是运维简单——排查问题时只需看编辑器的输出面板；代价是日志级别不够精细（后面会看到 `exit` 即便是正常退出也被记成 `error` 级别）。

除了这三个「进程级」事件，还有一类「命令级」事件（`synctex`、`append-lines` 以及本讲重点的 `parse-fail`），它们也走同一条 `emit` 通道，但目的不是报错而是业务分发。本模块聚焦进程级三类，命令级在 4.3 讲。

#### 4.1.2 核心流程

事件转发的链路是「两级跳」：

```text
texpresso 子进程
   │  (Node ChildProcess 原生事件)
   ▼
process-manager.ts  ── this.process.on(...) ──►  this.emit(转发名, 数据)
   │  (EventEmitter 自定义事件)
   ▼
server.ts  ── texpressoProcess.on(转发名, ...) ──►  connection.console.error(...)
   │  (LSP window/logMessage)
   ▼
编辑器输出面板
```

关键设计：`process-manager` 把底层事件名**原样转发**（`error`→`error`、新增了 `stderr`、`exit`→`exit`）。`server.ts` 再把这三个监听器**全部接到 `connection.console.error`** 上。注意 `connection.console.error` 这里的「error」是 LSP 消息的日志**级别**（`LogMessageType.Error`），与事件名是否叫 error 无关。

#### 4.1.3 源码精读

**第一级：`process-manager.ts` 转发三类进程事件。**

stderr 转发——把子进程的 stderr 字节流转成字符串后 emit：

[src/process-manager.ts:48-50](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L48-L50) —— 把 `ChildProcess` 的 `stderr` 数据转发为自身 `"stderr"` 事件。

error 转发——注意这是「同名转发」`error`→`error`：

[src/process-manager.ts:52-54](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L52-L54) —— 把 `ChildProcess` 的 `error` 事件**同名**转发为自身 `"error"` 事件。这一步是「致命」的：根据前置知识里的规则，如果下游没有任何 `error` 监听器，这次 `emit` 会让 Node 进程崩溃。所以 `server.ts` 必须监听它（见下文）。

exit 转发——退出时先把 `isRunning` 置 false，再 emit 退出码与信号：

[src/process-manager.ts:56-62](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L56-L62) —— `exit` 处理器做两件事：先把 `this.isRunning = false`（这是进程状态的「单一真相」，4.3 会反复用到），再 emit `{ code, signal }`。

**第二级：`server.ts` 把三类事件接到 LSP 日志。**

这三个监听器都注册在 `onInitialize` 内、`start()` 成功之后：

[src/server.ts:74-78](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L74-L78) —— 监听 `error`，打 `error` 级日志。**正因为有这一行，前置知识里那条「无监听器的 error 事件会崩」的规则才不会触发。**

[src/server.ts:79-81](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L79-L81) —— 监听 `stderr`，前缀 `STDERR:` 后打日志。

[src/server.ts:83-93](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L83-L93) —— 监听 `exit`，把退出码和信号拼成 `EXITED: ...` 打日志。**注意这里也用了 `console.error`**——即便 `code === 0` 是正常退出，也会被记成 error 级别。这是当前实现的一个粗糙之处：日志级别并不区分「正常退出」与「异常退出」。

把三个监听器对照成表：

| 子进程事件 | process-manager 转发名 | server.ts 监听处 | 日志方法 | 日志级别 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `error` | `error`（同名） | L74-78 | `console.error` | Error | 无监听器会崩，故必须监听 |
| `stderr` | `stderr` | L79-81 | `console.error` | Error | 子进程抱怨内容，未必致命 |
| `exit` | `exit` | L83-93 | `console.error` | Error | 正常/异常退出均记为 Error 级 |

#### 4.1.4 代码实践

**实践目标**：亲手验证 `error` 事件的「无监听器即崩溃」规则，并观察 `texpresso-lsp` 如何靠监听器躲过这一劫。

**操作步骤**：

1. 写一个最小 Node 脚本（**示例代码**，非项目原有代码），故意对一个 EventEmitter 发出 `error` 事件但不加监听器：

   ```js
   // 示例代码：demo-error-event.js
   const { EventEmitter } = require("events");
   const ee = new EventEmitter();
   ee.emit("error", new Error("boom")); // 无监听器
   console.log("这行不会被打印");
   ```

2. 运行 `node demo-error-event.js`，观察输出。

3. 再加一行 `ee.on("error", () => console.log("接住了"))` 重新运行，对比行为。

4. 回到项目源码，确认 [src/server.ts:74-78](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L74-L78) 正是那个「接住」的监听器；并思考：如果 `texpresso` 不在 PATH 里，`spawn` 会在何时触发这个 `error` 事件。

**需要观察的现象**：

- 步骤 2 中进程应抛出未捕获异常并退出，最后一行不打印。
- 步骤 3 中进程正常结束，打印「接住了」。

**预期结果**：`error` 事件无监听器 → 进程崩溃；有监听器 → 安全。这从机制上证明了 `server.ts` 那个 `on("error", ...)` 不是可有可无的装饰，而是防崩溃的必需品。如果本地没有 Node 环境或无法确认行为，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`exit` 事件明明不一定是错误（`code === 0` 是正常退出），为什么 `server.ts` 仍用 `console.error` 记录它？

**参考答案**：因为 `texpresso` 是**长驻**预览进程（见 u3-l1），在会话期间它本不该退出。任何退出——无论退出码是多少——都意味着预览能力丧失，属于需要运维关注的异常状态，所以作者统一用 error 级别高亮。这是一个语义判断，而非技术必然。

**练习 2**：如果删掉 [src/server.ts:74-78](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L74-L78) 的 `error` 监听器，系统会出现什么后果？

**参考答案**：`process-manager.ts` 的 [L52-54](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L52-L54) 仍会 `this.emit("error", error)`，而 EventEmitter 在「无 error 监听器」时会抛出未捕获异常，导致整个 LSP 服务器进程崩溃。删掉这一行等价于拆掉了 4.1.4 实践里的那道防线。

---

### 4.2 try/catch 边界与 sendCommand 守卫

#### 4.2.1 概念说明

`texpresso-lsp` 里有两类「出错兜底」代码，策略截然相反，必须分清：

- **吞掉（swallow）**：`try/catch` 捕获异常后只 `console.error` 打日志，**不重新抛出**。调用方感觉不到出过错。适用于「非关键路径」——失败了也不影响服务器存活，只是该次功能用不了。
- **上抛（rethrow）**：`try/catch` 捕获、打日志后**继续 `throw error`**。让错误沿着调用栈继续传播。适用于「关键路径」——失败意味着整个动作没法成立。

与 `try/catch` 并列的另一道防线是 `sendCommand` 里的**健康守卫**：在向子进程写命令前，先检查进程是否还活着、stdio 是否可用，不满足就直接 `throw`。这是「薄封装」架构里唯一一道统一的硬防线——因为 `server.ts` 各处都能调 `sendCommand`，把检查集中在这一处避免每个调用点都自己判空。

#### 4.2.2 核心流程

先看 `sendCommand` 的守卫，它定义了「能不能发命令」的底线：

```text
sendCommand(command, data)
   │
   ├─ 进程没运行 / stdin 不可用 / stdout 不可用？
   │      └─ 是 ► throw "Process is not running or stdio not available"
   │
   └─ 否 ► JSON.stringify([command, ...data]) ► stdin.write(message + "\n")
```

再看 `server.ts` 里 `try/catch` 的两种用法分布：

```text
onInitialize          ► try/catch 后 rethrow  ► 握手失败（最严重）
onInitialized         ► try/catch 后 swallow  ► 配置拉取失败也不影响主流程
onDidChangeConfiguration ► try/catch 后 swallow
onDocumentHighlight   ► try/catch 后 swallow
onShutdown            ► try/catch 后 swallow  ► 即使停进程失败也不阻塞关闭

onDidOpen / onDidSave / onDidClose / onDidChangeTextDocument
                      ► 没有 try/catch！（隐患见 4.2.4）
```

记忆口诀：**「握手要狠，其余要稳」**——决定服务器能不能生的 `onInitialize` 选择让失败炸开；运行期所有回调选择吞掉以保活。

#### 4.2.3 源码精读

**`sendCommand` 守卫——薄封装里唯一的硬防线。**

[src/process-manager.ts:103-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L103-L110) —— 发命令前的健康检查：`!this.isRunning || !this.process?.stdin || !this.process?.stdout` 任一不满足即 `throw`。注意它同时检查了 stdin 和 stdout——因为协议是双向的，任何一边断了都无法正常通信。

**关键路径：`onInitialize` 的 try/catch 选择 rethrow。**

[src/server.ts:46-131](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L46-L131) 是整个 `onInitialize`，外层包了一个 `try`，对应的 `catch` 在：

[src/server.ts:124-129](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L124-L129) —— 捕获后先用 `error instanceof Error ? error.message : String(error)` 做类型收窄再打日志，**最后 `throw error`**。因为 `onInitialize` 的返回值就是 LSP 握手结果，这里 rethrow 会让整个握手失败——编辑器会显示「服务器启动失败」。这是合理的：连 `texpresso` 子进程都起不来，整个 LSP 就没有存在意义。

注意那个 `instanceof Error` 守卫：因为 TypeScript 的 `strict` 模式下 `catch` 子句的 `error` 类型是 `unknown`（见 u1-l3 对 strict 的讨论），必须先收窄成 `Error` 才能安全读 `.message`。同样的写法在 [src/server.ts:259-261](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L259-L261) 和 [src/server.ts:275-277](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L275-L277) 重复出现，是项目里处理 `unknown` 错误的固定套路。

**非关键路径：运行期回调选择 swallow。**

[src/server.ts:134-150](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L134-L150) —— `onInitialized` 拉取工作区配置，失败只 `console.error` 不 rethrow。即使配置拉不到，服务器仍能用默认值继续跑。

[src/server.ts:153-167](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L153-L167) —— `onDidChangeConfiguration` 同理 swallow。

[src/server.ts:229-268](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L229-L268) —— `onDocumentHighlight` 也 swallow，且保证最后 `return []`（见 u3-l2：它借壳触发正向 SyncTeX，返回值必须稳定为空数组）。

[src/server.ts:270-279](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L270-L279) —— `onShutdown` 同样 swallow：即使 `stop()` 抛错也不阻塞 LSP 关闭流程。

**关键不对称：文档同步回调没有 try/catch。**

[src/server.ts:169-177](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L169-L177)（`onDidOpen`）、[src/server.ts:198-202](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L198-L202)（`onDidClose`）、[src/server.ts:204-226](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L204-L226)（`onDidChangeTextDocument`）这三个回调都直接调 `texpressoProcess.sendCommand(...)`，**外层没有 try/catch**。这意味着：一旦子进程已死、`sendCommand` 的守卫抛错，这个异常会变成一个未处理的 Promise rejection（因为这些回调是 `async`）。这是当前实现的一处隐患，详见 4.2.4。

#### 4.2.4 代码实践

**实践目标**：定位代码中所有 `connection.console.error` / `warn` 调用点，说清每个的触发场景；并评估「文档同步回调无 try/catch」的隐患。

**操作步骤**（源码阅读型实践，无需运行）：

1. 在 `src/server.ts` 中用搜索定位所有 `console.error(` 与 `console.warn(`，按下表逐个填写「触发场景」一列。

   | 行号 | 方法 | 日志内容关键词 | 触发场景（请你填写） |
   | --- | --- | --- | --- |
   | [L75-77](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L75-L77) | error | `Texpresso process error` | 子进程 `error` 事件（如可执行文件不存在） |
   | [L80](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L80) | error | `STDERR:` | 子进程写 stderr |
   | [L89-91](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L89-L91) | error | `EXITED:` | 子进程退出 |
   | [L96-98](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L96-L98) | warn | `Synctex inverse search received` | 收到 synctex 反向搜索事件 |
   | [L125-127](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L125-L127) | error | `Failed to start texpresso process` | onInitialize 中 start() 失败 |
   | [L146-148](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L146-L148) | error | `Failed to initialize configuration` | onInitialized 拉配置失败 |
   | [L165](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L165) | error | `Failed to get configuration` | onDidChangeConfiguration 失败 |
   | [L175](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L175) | warn | `asking texpresso to open document` | 每次 didOpen |
   | [L184](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L184) | warn | `texpresso-tonic already running` | 保存时 tonic 仍在跑 |
   | [L219-221](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L219-L221) | warn | `asking texpresso to change` | 每次 didChange |
   | [L259-261](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L259-L261) | error | `Error handling document highlight` | onDocumentHighlight 内部异常 |
   | [L275-277](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L275-L277) | error | `Error stopping texpresso process` | onShutdown 中 stop() 失败 |

2. 然后回答这个推理题：假设用户在编辑过程中 `texpresso` 子进程意外崩溃了（`exit` 事件已触发，`isRunning` 被置 false），此时用户继续敲字触发 `onDidChangeTextDocument` → [src/server.ts:222](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L222) 调 `sendCommand("change-range", ...)`。请追踪 [src/process-manager.ts:104-106](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L104-L106) 的守卫会怎样，又因为 [src/server.ts:204-226](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L204-L226) 没有 try/catch，这个 throw 会变成什么。

**需要观察的现象 / 预期结果**：

- 步骤 1 应得到 12 个调用点（8 个 error、4 个 warn）。
- 步骤 2 预期：`sendCommand` 守卫因 `!this.isRunning` 为真而 `throw`；该回调是 `async` 且无 `try/catch`，故抛出的 Promise rejection 不会被捕获，成为 **unhandled promise rejection**——Node 会打印警告（在新版 Node 中进程甚至可能被终止）。这正是当前实现的一处健壮性缺口。

**如果无法确定运行结果**，请标注「待本地验证」——本实践重在阅读与推理，不强制运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `onInitialize` 的 catch 要 rethrow，而 `onInitialized` / `onShutdown` 的 catch 选择 swallow？

**参考答案**：`onInitialize` 决定服务器能否「生」——如果连 `texpresso` 子进程都起不来，继续存活没有意义，rethrow 让 LSP 握手失败、由编辑器向用户报错是更诚实的做法。而 `onInitialized`（拉配置）、`onShutdown`（停进程）都是「非关键路径」，失败时用默认值或尽力清理即可，swallow 能保证主流程不被次要失败拖垮。

**练习 2**：`sendCommand` 的守卫同时检查了 stdin 和 stdout，为什么不能只检查 stdin（毕竟写命令只用 stdin）？

**参考答案**：因为 NDJSON 是双向协议（见 u2-l3）：发命令走 stdin、收事件走 stdout。如果只有 stdin 能写而 stdout 已断，命令发出去也收不到回复，等于半瘫痪。同时检查两端，确保通信链路完整可用，避免「发出去了但永远等不到响应」的隐性故障。

---

### 4.3 parse-fail 容错与 shutdown

#### 4.3.1 概念说明

本模块把两个看似无关的话题放在一起，因为它们共同回答一个问题：**「意外来临时，系统如何体面地活着或退场？」**

- **parse-fail 容错**：子进程 stdout 的某一行如果不是合法 JSON，怎么办？最笨的做法是让 `JSON.parse` 抛错、整个 stdout 处理链路崩掉。`texpresso-lsp` 的做法更聪明——把解析失败的那一行**伪装成一条命令** `["parse-fail", line]`，让它走和正常命令一模一样的分发通道。结果是：坏行不会污染好行，整条流水线继续运转。

- **shutdown（优雅停止）**：LSP 客户端关闭时，会发 `shutdown` 请求。服务器应当在此时清理 `texpresso` 子进程，否则它会变成孤儿进程继续占用资源。`onShutdown` 调 `stop()`，`stop()` 又依赖 `isRunning` 这个状态标志做幂等判断。

两者共享同一个底层思想：**用一个布尔标志（`isRunning`）和「失败时降级而非崩溃」的策略，把不可控的外部世界挡在主流程之外。**

#### 4.3.2 核心流程

**parse-fail 的容错路径**——坏行如何被「降级」：

```text
stdout 一行 line
   │
   ├─ JSON.parse(line) 成功？  ──► 正常 command_list
   │
   └─ 失败(catch)  ──► 伪造 ["parse-fail", line]
                                   │
                                   ▼
                  与正常命令走同一条 forEach
                                   │
                                   ▼
                  emit("parse-fail", [line])
                                   │
                                   ▼
                  server.ts 没有注册 "parse-fail" 监听器
                                   │
                                   ▼
                  事件被静默丢弃（不崩，因为不是 "error" 事件）
```

**shutdown 的生命周期协同**——三个函数如何咬合：

```text
onInitialize ──► start()
                   ├─ isRunning 已为 true？► throw（防重复启动）
                   ├─ spawn + isRunning = true
                   └─ 绑定 exit 处理器（exit 时 isRunning = false）

（运行期进程崩溃）► exit 事件 ► isRunning = false ► 仅打日志，不重启

onShutdown   ──► stop()
                   ├─ isRunning 为 false 或 process 为 null？► 直接 return（幂等，安全）
                   └─ 否则：kill() ► 等 exit ► isRunning=false, process=null
```

注意 `isRunning` 是「进程状态的单一真相」（见 u2-l2）：它在 `start` 成功时置 true，在 `exit` 事件、`start` 的 catch、`stop` 三处被置 false。所有判断（`sendCommand` 守卫、`stop` 幂等）都只看这一个标志。

#### 4.3.3 源码精读

**parse-fail：把解析失败伪装成命令。**

[src/process-manager.ts:32-45](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L32-L45) —— `map` 阶段对每一行尝试 `JSON.parse`：

[src/process-manager.ts:35-39](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L35-L39) —— 解析失败时 `catch` 住，返回 `["parse-fail", line]` 而不是让异常逃逸。这条假命令的「命令名」是字符串 `"parse-fail"`，「数据」是原始行。

随后 `forEach` 用统一的 `emit(command, data)` 分发，对 `parse-fail` 一视同仁。而在 `server.ts` 里搜索 `parse-fail`，你会发现**没有任何 `texpressoProcess.on("parse-fail", ...)`**——也就是说这个事件发出后没有任何监听器接它。关键点：因为事件名是 `"parse-fail"` 而不是 `"error"`，根据前置知识的 EventEmitter 规则，无监听器只会被**静默丢弃**，不会崩溃。这就是「优雅降级」的完整闭环：坏行被安全吞掉，好行继续处理。

> 思考：如果想对 parse-fail 做更有意义的处理（例如打一条 warning 日志方便排查），只需在 `server.ts` 加一行 `texpressoProcess.on("parse-fail", (data) => connection.console.warn(...))` 即可，协议层零改动——这正是 u2-l3 讲过的「薄封装」红利。这也可以作为 u3-l4「扩展点」的预告。

**start() 的防重入与超时。**

[src/process-manager.ts:18-22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L18-L22) —— `start()` 入口先查 `isRunning`，已在跑就 `throw`，防止重复启动把状态搞乱。

[src/process-manager.ts:82-85](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L82-L85) —— 整个启动过程的兜底 catch：一旦 spawn 后任意步骤失败（含 [L65-81](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L65-L81) 的 5 秒就绪超时），先把 `isRunning = false` 复位再 `throw error`。这个「先复位状态再抛」很重要：它保证无论 start 成功还是失败，`isRunning` 都如实反映进程状态，不会出现「start 失败了但 isRunning 还是 true」的谎报。

**stop() 的幂等设计——本讲实践任务的核心。**

[src/process-manager.ts:88-101](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L88-L101) —— `stop()` 全文：

[src/process-manager.ts:89-91](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L89-L91) —— 第一道守卫：`if (!this.isRunning || !this.process) return;`。这就是「进程已退出时再调 stop 也安全」的依据。因为进程退出时 [L56-62](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L56-L62) 的 exit 处理器已经把 `isRunning` 置为 false，所以这里 `!this.isRunning` 为真，直接 `return`，不会去对一个死进程再 `kill()`。

[src/process-manager.ts:93-100](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L93-L100) —— 真正的停止逻辑：`kill()` 发 SIGTERM，然后**新注册一个 `exit` 监听器**，等子进程真正退出后才 resolve。注意这里设置 `this.process = null`（start 里的 exit 处理器只置 `isRunning=false`、不置 `process=null`，二者职责不同）。

**onShutdown：LSP 关闭钩子。**

[src/server.ts:270-279](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L270-L279) —— `onShutdown` 包了 try/catch（swallow 策略），调 `texpressoProcess.stop()`。即使 stop 出错也不阻塞 LSP 关闭。

#### 4.3.4 代码实践

**实践目标**：回答本讲规格里的核心问题——`stop()` 在进程已退出的情况下被调用是否安全？并给出代码依据。

**操作步骤**（源码阅读 + 推理型实践）：

1. 在脑中模拟「子进程意外崩溃」场景：`texpresso` 在运行期自己退出了。追踪 [src/process-manager.ts:56-62](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L56-L62) 的 exit 处理器执行：`isRunning` 变成什么？`process` 字段被置空了吗？

2. 接着模拟「随后用户关闭编辑器」场景：LSP 客户端发 `shutdown`，[src/server.ts:270-279](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L270-L279) 被调用，进而 [src/process-manager.ts:89-91](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L89-L91) 的守卫条件取值是什么？会走到 `kill()` 吗？

3. （进阶）再思考一个窄窗口竞态：如果在 `stop()` 通过了 L89 的守卫（当时 `isRunning` 还是 true）、但还没执行到 L94 的 `kill()` 之间，子进程恰好自己退出了——此时 [L95](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L95) 新注册的 `exit` 监听器还能被触发吗？如果不能，[L93](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L93) 的 Promise 会怎样？

**需要观察的现象 / 预期结果**：

- 步骤 1：`isRunning` 被置为 `false`；`process` **没有**被置空（start 里的 exit 处理器只管 `isRunning`）。
- 步骤 2：`!this.isRunning` 为 `true`（因为已 false），守卫短路 `return`，**不会**走到 `kill()`。结论：**安全**。依据就是 `isRunning` 这个「单一真相」在 exit 事件里被及时复位，使 `stop()` 天然幂等。
- 步骤 3（进阶，待本地验证）：`ChildProcess` 的 `exit` 事件只触发一次，事后新注册的监听器不会再被调用，因此该 Promise **可能永远不会 resolve**——这是当前 `stop()` 实现里一个窄窗口的理论隐患。不过正常关闭流程中，`stop()` 通常在进程仍存活时被调用，命中此竞态的概率很低。

> 小贴士：如果你想让 `stop()` 在步骤 3 的竞态下也安全，一种思路是在 L89 守卫之后、L94 之前，再读一次进程的 `killed` / `exitCode` 属性，或在注册新 `exit` 监听器时同时检查进程是否已退出。这属于二次开发范畴，留给 u3-l4 讨论。

#### 4.3.5 小练习与答案

**练习 1**：为什么 parse-fail 不会让服务器崩溃，而 `error` 事件无监听器却会？

**参考答案**：因为 EventEmitter 对 `error` 事件有特殊处理——无监听器时直接抛出未捕获异常；而其它事件名（包括自定义的 `parse-fail`、`stderr`、`exit`）无监听器时只是静默丢弃。`process-manager.ts` 把解析失败伪装成名为 `"parse-fail"` 的事件，名字不是 `"error"`，故即便 `server.ts` 没人监听它也不会崩。这是「优雅降级」能成立的关键。

**练习 2**：`start()` 的 catch（[L82-85](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L82-L85)）里为什么要先 `this.isRunning = false` 再 `throw error`，而不是直接 throw？

**参考答案**：因为在 L24-25 已经把 `isRunning` 置为 true 了，如果后续步骤（如 5 秒就绪超时）失败而直接 throw，`isRunning` 会停留在 true，造成「进程其实没起来、但标志说在跑」的谎报，后续 `sendCommand` 守卫会被骗过、`stop()` 也会走错分支。先复位再抛，保证 `isRunning` 始终是进程状态的真实反映。

**练习 3**：进程在运行期崩溃后，`server.ts` 会自动重启它吗？

**参考答案**：不会。[src/server.ts:83-93](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L83-L93) 的 exit 监听器只打日志、不复位 `texpressoProcess`，也没有重建逻辑。崩溃后 `isRunning=false`，后续 `sendCommand` 会抛错（且因文档同步回调无 try/catch，会变成未处理 rejection，见 4.2.4）。这是当前实现的局限——「崩溃即不可恢复」，可作为 u3-l4 二次开发的改进点。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**「子进程崩溃全链路追踪」**任务。

**场景**：用户正在编辑 LaTeX，`texpresso` 预览正常。突然，因为 `.tex` 文件里某处严重错误，`texpresso` 子进程崩溃退出。随后用户又敲了几个字，最后关闭了编辑器。

**任务**：请用一张时序图（文字版即可）画出从「子进程崩溃」到「编辑器关闭」之间，系统里发生的所有事件与状态变化，要求至少覆盖以下节点：

1. 哪个处理器把 `isRunning` 置为 false？给出源码行号。
2. `server.ts` 的哪个监听器把崩溃消息记进 LSP 日志？用什么级别？
3. 用户随后敲字触发 `onDidChangeTextDocument` → `sendCommand`，守卫在哪一行抛错？这个错误会被 try/catch 接住吗？
4. stdout 里如果恰好有一行不是合法 JSON（崩溃前夕的半截输出），`parse-fail` 路径如何处理它？
5. 编辑器关闭触发 `onShutdown` → `stop()`，此时 `isRunning` 是什么值？`stop()` 走哪条分支？是否安全？

**完成标准**：你的时序图应当能让人一眼看出——本讲的三个最小模块（事件转发、try/catch 与 sendCommand 守卫、parse-fail 与 shutdown）是如何在同一个「崩溃」场景下协作（或暴露缺口）的。重点关注哪一步是「优雅降级」（设计良好），哪一步是「隐患」（4.2.4 指出的未处理 rejection）。

## 6. 本讲小结

- **事件转发是两级跳**：`process-manager.ts` 把 `ChildProcess` 的 `error`/`stderr`/`exit` 转发为自身事件，`server.ts` 再统一接到 `connection.console.error` 打进 LSP 日志。其中 `error` 是同名转发，且**必须**被监听，否则 EventEmitter 会让进程崩溃。
- **两类 try/catch 策略对立**：`onInitialize` 选择 rethrow（握手失败最严重），`onInitialized`/`onDidChangeConfiguration`/`onDocumentHighlight`/`onShutdown` 选择 swallow（保活）。口诀是「握手要狠，其余要稳」。
- **sendCommand 守卫是薄封装里唯一的硬防线**：集中检查 `isRunning` 与 stdin/stdout 可用性，不满足即 throw。但文档同步回调没有 try/catch 包裹它，子进程崩溃后的写入会变成未处理 rejection，是当前实现的一处隐患。
- **parse-fail 是优雅降级的典范**：把解析失败的行伪装成 `["parse-fail", line]` 走统一分发通道，且因事件名不是 `error`，无监听器也不会崩，坏行被静默吞掉、好行继续处理。
- **`isRunning` 是进程状态的单一真相**：在 start 成功、exit 事件、start catch、stop 四处被写入。`stop()` 靠它实现幂等——进程已退出时 `!this.isRunning` 短路 return，所以 `stop()` 在进程死后被调用是**安全**的。
- **进程崩溃不可恢复**：exit 监听器只打日志不重启，崩溃后服务器进入「sendCommand 必抛错」的瘫痪态，是当前实现已知局限。

## 7. 下一步学习建议

- 下一讲 **u3-l4「架构取舍与二次开发」** 会把本讲暴露的所有隐患（文档同步无 try/catch、`stop()` 的窄竞态、崩溃不重启、parse-fail 只丢弃不告警）放到「薄封装哲学」的大背景下统一讨论，并给出二次开发的扩展点。建议你在读 u3-l4 前，先回头把本讲 4.2.4 与 4.3.4 的两个隐患想清楚，这样进入 u3-l4 时就能带着「要改什么」的问题去读。
- 如果想横向对照「LSP 服务器如何做健壮的错误处理」，可以回头重读 [src/server.ts:46-131](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L46-L131) 的 `onInitialize`，体会「握手阶段宁可失败也不能带病运行」这条原则。
- 进阶读者可以尝试动手：为本讲 4.2.4 指出的「文档同步回调无 try/catch」补一道兜底，或为 `parse-fail` 加一个 warning 监听器，作为通向 u3-l4 的热身。
