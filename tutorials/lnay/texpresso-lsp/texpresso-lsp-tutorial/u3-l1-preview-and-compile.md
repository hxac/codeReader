# 实时预览与编译流程

## 1. 本讲目标

上一讲（u2-l4）我们把「文档内容」同步给了 texpresso 子进程：打开发全文、编辑发增量、关闭发路径。但那套同步只保证 texpresso **内存里**有最新文本，并不等于预览窗口里看到的就是「编译正确」的结果。LaTeX 里有一类东西——交叉引用（`\ref`）、文献（`\cite`）、目录——必须经过一次**完整的编译遍**才能解析出来，而增量编辑的文本里这些引用还是「未解析」状态。

本讲就来回答：**用户按下「保存」之后，texpresso-lsp 是怎么触发一次编译、编译完了又怎么让预览刷新的？**

精读的代码只有十几行，集中在一个回调里：`documents.onDidSave`。读完本讲，你应该能够：

- 画出从「保存事件」到「预览更新」的完整链路：`onDidSave` → `spawn texpresso-tonic` → 进程 `exit` → `sendCommand("rescan")`。
- 解释 `texpresso-tonic` 是什么、它和长驻的 `texpresso` 预览进程**各司什么职**，以及它的路径是如何用字符串拼接得到的。
- 说清 `rescan` 命令的作用，以及它旁边那句 `// TODO maybe only do this if changes detected to sha of aux file?` 注释想优化什么。
- 评估 `is_texpresso_tonic_running` 这个布尔标志**到底有没有起到「防重入」的作用**——这一点比看上去微妙，也是本讲实践任务的核心。

本讲只看一个文件 `src/server.ts`，是 u2-l4 文档同步之上的自然延续：`didChange` 管「内容实时同步」，`didSave` 管「落盘后重编译」。

## 2. 前置知识

进入那十几行源码之前，先建立三个直觉。

### 2.1 为什么「编辑时同步」还不够，还要「保存时编译」

LaTeX 是一门**需要多遍编译**的语言。第一遍编译时，`\ref{sec:intro}` 还不知道 `sec:intro` 到底在第几页；编译器把这个「待解析的引用」写进一个辅助文件（`.aux`）；第二遍编译时再读 `.aux` 把引用填成真正的页码/章节号。所以即使 texpresso 预览进程已经拿到了你最新的文本，那些引用可能还是问号——**少了一次「解析引用」的编译遍**。

`texpresso-tonic` 就是来补这一遍的：它是对根文档做的一次**编译器遍（compiler pass）**，产物是更新过的辅助数据。编译完之后，还得通知那个长驻的预览进程「辅助数据变了，重新读一遍」——这就是 `rescan` 命令。本讲代码里 `onDidSave` 做的就是「先编译、再 rescan」这两步。

> 这个「保存即重编译」机制正是 commit `4823c82`「Do a proper compiler pass on save to update file」引入的——在那之前 `onDidSave` 是个空函数（见 4.1.4 的实践）。这也解释了为什么 `is_texpresso_tonic_running` 字段是和这段代码**同一批**加进 `connection` 对象的。

### 2.2 两种 spawn：长驻进程 vs 一次性进程

回顾 u2-l2 / u2-l3，texpresso-lsp 里其实出现了**两种**用 `child_process.spawn` 拉起子进程的方式：

| | 长驻的 `texpresso` 预览进程 | 本讲的 `texpresso-tonic` 编译进程 |
| --- | --- | --- |
| 拉起者 | `TexpressoProcessManager.start()` | `onDidSave` 里直接 `spawn(...)` |
| 生命周期 | 握手时启动、`onShutdown` 时停止，全程常驻 | 每次「保存」启动一次、编译完自行退出 |
| 通信 | 双向：`stdin` 写命令、`stdout` 读事件（NDJSON） | 单向：只等它 `exit`，不跟它说话 |
| 数量 | 始终一个 | 理论上每次保存一个（这正是 4.3 要讨论的隐患） |

理解这层差别，才能看懂本讲代码里那个 `spawn(...).on("exit", ...)` 的写法：tonic 是一个**用完即弃**的进程，我们只关心它「什么时候结束」，结束就触发下一步。

### 2.3 「防重入」是什么意思，为什么需要它

**重入（re-entrancy）** 指：上一次保存触发的 tonic 还在编译，用户又按了一次保存，于是**第二个** tonic 被启动。这通常不是我们想要的——两个编译遍同时跑既浪费 CPU，又可能抢着读写同一份 `.aux` 文件。

「防重入」就是用一个标志记下「现在有没有 tonic 在跑」，如果有，就别再启动新的。本讲的 `is_texpresso_tonic_running` 字段**名字**看起来就是干这个的。但本讲的一个关键发现是：**它当前的实现并没有真正阻止重入，只是打了一条警告日志**。这一点我们先记住，4.3 会拿源码逐行验证。

## 3. 本讲源码地图

本讲只涉及一个文件，但会从三个角度反复读其中同一段代码。

| 文件 | 本讲关注的部位 | 作用 |
| --- | --- | --- |
| `src/server.ts` | 第 36 行 `is_texpresso_tonic_running: false` | 在 `connection` 对象上声明这个布尔标志的初始值 |
| `src/server.ts` | 第 179–196 行 `documents.onDidSave(...)` | 本讲主角：保存事件的全部处理逻辑 |
| `src/server.ts` | 第 187–188 行 `// FUTURE ...` 与第 193 行 `// TODO ...` | 作者留的两条优化设想 |
| `src/process-manager.ts`（回看） | 第 103–110 行 `sendCommand` | `rescan` 命令最终经它写进 `texpresso` 的 `stdin` |

## 4. 核心概念与源码讲解

### 4.1 onDidSave 链路：保存如何触发编译

#### 4.1.1 概念说明

`onDidSave` 是 `TextDocuments` 文档管理器提供的高层回调（和 u2-l4 里 `onDidOpen` / `onDidClose` 同属一套）。每当用户在编辑器里**保存**一个文档，编辑器发出 `textDocument/didSave` 通知，文档管理器就触发这个回调。

和「变更（didChange）」不同，「保存」是一次**落盘**动作——它在语义上意味着「用户告一段落了，这是个值得重新编译的稳定状态」。所以 texpresso-lsp 把重编译放在 `didSave` 而不是 `didChange` 上：否则每敲一个字就编译一遍，既慢又无意义。

#### 4.1.2 核心流程

整个 `onDidSave` 的流程可以画成下面这条链（这是本讲实践任务要你手绘的时序图的文字版）：

```
用户在编辑器按保存
        │
        ▼
编辑器发 textDocument/didSave
        │
        ▼
documents.onDidSave 回调被触发
        │
        ├─ 读取 root_tex 路径
        ├─ 拼出 texpresso-tonic 的路径（texpresso_path + "-tonic"）
        ├─ 看 is_texpresso_tonic_running：若为真，只打一条 warn（但不停下来！）
        ├─ 把 is_texpresso_tonic_running 置为 true
        └─ spawn(texpresso-tonic, ["-k", path])          ← 编译遍开始
                  │
                  │  （编译中，Node 事件循环继续干别的活）
                  ▼
            texpresso-tonic 进程 exit
                  │
                  ▼
            .on("exit") 回调被触发
                  ├─ 打日志 "texpresso-tonic ended"
                  ├─ is_texpresso_tonic_running = false
                  └─ texpressoProcess.sendCommand("rescan", [])  ← 通知预览进程刷新
```

注意这条链横跨了**两个**异步边界：保存事件本身是异步通知；tonic 的 `exit` 又是另一个异步事件。中间那段「编译中」的时间里，Node 可以继续处理别的 LSP 消息——也包括再一次保存。

#### 4.1.3 源码精读

先看 `is_texpresso_tonic_running` 是在哪里「出生」的。它和 `init_options`、`workspace_config` 一样，是通过对象展开混进 `connection` 的一个自定义字段，初值是 `false`：

- [src/server.ts:33-38](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L33-L38) —— `connection` 对象把三个**应用自定义字段**（`init_options`、`workspace_config`、`is_texpresso_tonic_running`）与 `createConnection(ProposedFeatures.all)` 的 LSP 能力展开合并。`is_texpresso_tonic_running` 在这里被声明为 `false`。注意它和 `init_options`（配置）性质不同：它是**运行期可变状态**，不是配置。

然后是本讲主角——完整的 `onDidSave` 回调：

- [src/server.ts:179-196](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L179-L196) —— `documents.onDidSave` 的全部逻辑：拼 tonic 路径 → 打 warn → 置标志 → spawn tonic → 在 `exit` 回调里复位标志并发 `rescan`。

拆开逐段看。第一段，取路径：

```ts
const path = connection.init_options.root_tex;
const texpresso_tonic_path = connection.init_options.texpresso_path + "-tonic";
```

这里有一个容易被忽略的细节：**回调的参数 `event` 完全没被用到**。也就是说，不管用户保存的是哪个 `.tex` 文件，这里编译的永远是 `root_tex`（根文档）。这符合 LaTeX 的编译模型——编译总是从根文档入口走 `\input` / `\include` 把子文件串起来——但同时也意味着：保存一个**非根**文件，触发的也是整份根文档的编译。

第二处细节是 tonic 路径的来源：它不是独立配置项，而是由 `texpresso_path` **字符串拼接** `"-tonic"` 得到的。所以若 `texpresso` 在 `/usr/local/bin/texpresso`，就要求 `/usr/local/bin/texpresso-tonic` 也存在。两个可执行文件被假定成「同一个安装目录下的成对文件」。

接着是 spawn 本体（这一行同时属于 4.2 和 4.3 的讨论）：

- [src/server.ts:190-195](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L190-L195) —— `spawn(texpresso_tonic_path, ["-k", path]).on("exit", ...)`。`.on("exit")` 直接挂在 `spawn` 返回的 `ChildProcess` 上：tonic 一退出，就复位标志并向长驻进程发 `rescan`。

注意 `spawn` 的返回值**没有被存进任何变量**——这是典型的「fire-and-forget」（点火就忘）：我们不需要后续对 tonic 进程发消息或 kill 它，只需要知道它什么时候结束，所以拿到 `ChildProcess` 后立刻挂上 `exit` 监听就够用了。

#### 4.1.4 代码实践

**实践目标**：确认这套「保存即编译」机制是哪个版本加进来的，并理解它替换了什么。

**操作步骤**：

1. 运行 `git show 4823c82 -- src/server.ts`，查看「Do a proper compiler pass on save to update file」这次提交对 `onDidSave` 的改动。
2. 对比改动前后的 `onDidSave`：改动前是空函数 `documents.onDidSave(async (event) => {});`，改动后才有了上面这段逻辑。
3. 同一次提交还把 `is_texpresso_tonic_running: false` 加进了 `connection` 对象——确认字段与逻辑是**同批引入**的。

**需要观察的现象**：改动前，保存文件**完全不会**触发任何编译或 rescan，预览里的引用会一直停留在未解析状态。

**预期结果**：你会清楚地看到，本讲的整条链路（标志 + spawn + rescan）是在 `4823c82` 这一次提交里作为一个整体落地的。

> 说明：本实践是「源码阅读型」，不需要真正运行 texpresso（它依赖外部可执行文件与一份可编译的 LaTeX 工程）。若你本地有 TeXpresso 环境，可进一步在保存后观察 LSP 日志里依次出现 `spawning texpresso-tonic` → `texpresso-tonic ended` 两行，对应 4.1.2 时序图的两端；否则记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`onDidSave` 的参数 `event` 为什么没被使用？如果用户保存的不是根文档，会发生什么？

**参考答案**：因为编译总是以根文档 `root_tex` 为入口，所以代码直接用 `connection.init_options.root_tex` 取路径，忽略 `event` 里那个「实际被保存的文档」。保存任何文件（包括非根的子文件）都会触发一次**整份根文档**的编译。这在 LaTeX 的 `\input`/`\include` 模型下是合理的，但它也意味着无法做到「只重编译被改动的子文件」。

**练习 2**：`texpresso_tonic_path` 是怎么得到的？如果用户在初始化选项里把 `texpresso_path` 设成了 `/opt/texpresso/bin/texpresso`，那么 tonic 必须在哪？

**参考答案**：由 `connection.init_options.texpresso_path + "-tonic"` 字符串拼接得到。所以 tonic 必须在 `/opt/texpresso/bin/texpresso-tonic`。两处可执行文件被假定成「成对存在」。

---

### 4.2 texpresso-tonic 编译：为什么需要一个独立的编译器进程

#### 4.2.1 概念说明

`texpresso-tonic` 是 TeXpresso 项目（上游 `let-def/texpresso`）提供的一个**配套编译器**，和预览器 `texpresso` 是两个不同的可执行文件。本仓库并不包含它的源码，只是把它当成一个「外部命令」来调用。

为什么编译要拆成一个**独立进程**，而不是在长驻的预览进程内部完成？因为编译遍可能比较重（要跑完整个 LaTeX 工具链、解析所有引用），把它放在一个**独立的、用完即退**的进程里，能让长驻的预览进程保持轻量、持续响应增量编辑。这是一种「**重活外包给短命进程**」的常见设计。

#### 4.2.2 核心流程

tonic 这一侧的流程很短：

1. `onDidSave` 触发 → `spawn(texpresso_tonic_path, ["-k", path])`。
2. 操作系统拉起 tonic 进程，它对 `path`（根文档）做一次编译遍，更新辅助文件。
3. tonic 进程自行结束 → 触发 `exit` 事件。
4. Node 的 `exit` 回调里继续做 4.3 的事（复位标志 + rescan）。

tonic 进程和 texpresso-lsp 之间**没有 NDJSON 通信**——既不读它的 stdout，也不写它的 stdin。这是它和长驻预览进程（u2-l3 那套协议）最大的区别。我们对 tonic 唯一的关心就是「你什么时候退出」。

> 关于命令行参数 `["-k", path]`：`path` 就是根文档路径；`-k` 是 `texpresso-tonic` 这个外部工具自己的开关。`-k` 的确切语义由 TeXpresso 的 tonic 工具定义，本仓库源码与文档都没有解释，**待确认**（从上下文猜测可能与「保留辅助文件 / keep auxiliary」有关，但这属于上游工具的行为，不应在未核实前断言）。

#### 4.2.3 源码精读

关键就是 `spawn` 这一行的前半段：

- [src/server.ts:189-190](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L189-L190) —— 先打日志 `spawning texpresso-tonic`，再 `spawn(texpresso_tonic_path, ["-k", path])`。注意这里直接用了从 `child_process` 导入的裸 `spawn`（见文件顶部 [src/server.ts:17](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L17)），而不是 `TexpressoProcessManager`——因为 tonic 不需要双向通信、不需要 EventEmitter 包装，直接用最原始的 `spawn` 就够了。

旁边还有一条作者对未来的设想：

- [src/server.ts:187-188](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L187-L188) —— `// FUTURE keep track of whether there are any undefined reference errors / to potentially avoid unnecessary extra compiles?`。作者的优化思路是：如果上一遍编译**已经没有未解析的引用**了，那这次保存可能根本不需要再跑一遍 tonic，可以跳过。

#### 4.2.4 代码实践

**实践目标**：用一个最小 Node 脚本，模拟「点火就忘、只等 exit」的 spawn 用法，体会 tonic 这侧的通信模型。

**操作步骤**：

1. 新建一个临时脚本（这是**示例代码**，不是项目原有文件）：

   ```js
   // demo-fire-and-forget.js —— 示例代码
   const { spawn } = require("child_process");
   console.log("spawning");                      // 对应 server.ts:189
   const p = spawn("sleep", ["1"]);              // 对应 spawn(tonic, ...)
   p.on("exit", (code, signal) => {              // 对应 .on("exit", ...)
       console.log(`ended, code=${code}, signal=${signal}`);
   });
   console.log("主线程不阻塞，继续往下走");
   ```

2. 运行 `node demo-fire-and-forget.js`。

**需要观察的现象**：`spawning` 和「主线程不阻塞」几乎立刻打印，约 1 秒后才打印 `ended`。说明 `spawn` 立刻返回、主线程不被阻塞，`exit` 是稍后异步触发的——这正是 tonic 编译期间 Node 还能处理其他事件的原理。

**预期结果**：你会直观看到 `spawn` 和它的 `exit` 回调之间有一段「异步空隙」，这段空隙正是 4.3 所讨论的「重入窗口」。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把编译逻辑做进长驻的 `texpresso` 预览进程，而要单独 spawn 一个 `texpresso-tonic`？

**参考答案**：编译遍较重，独立成短命进程可以避免拖累长驻预览进程的响应性；同时「用完即退」让每次编译的状态互相隔离，不会累积。这是一种「重活外包给短命进程」的取舍。

**练习 2**：代码里对 tonic 的 stdout / stderr **没有任何处理**。这会带来什么问题？

**参考答案**：tonic 编译失败时的报错信息（打在 stderr 上）会被直接丢弃，用户和 LSP 日志都看不到失败原因。你只能从「预览里的引用一直没更新」间接推断编译可能失败了。这是一个可观测性上的缺口。

---

### 4.3 rescan 与防重入：编译完成后如何刷新预览（以及那个标志到底防住了什么）

#### 4.3.1 概念说明

tonic 编译完，辅助文件（引用、文献的解析结果）已经更新到磁盘。但**长驻的预览进程并不知道这件事**——它内存里还是旧的辅助数据。`rescan` 就是用来通知预览进程「去把辅助文件重新读一遍」的命令。这就是「保存 → 编译 → rescan」能刷新预览里引用的完整原因。

至于 `is_texpresso_tonic_running`，它名义上是「防重入」标志。但本节的核心结论是：**在当前实现里，它并没有真正阻止重入**。我们先看代码到底怎么用它，再分析它的实际效果。

#### 4.3.2 核心流程

`exit` 回调里只有三步：

1. 打日志 `texpresso-tonic ended`。
2. `is_texpresso_tonic_running = false`（复位）。
3. `sendCommand("rescan", [])`（通知预览进程刷新）。

而标志的「检查」发生在回调最开头：

```
读 is_texpresso_tonic_running
   ├─ 为 true  → 打一条 warn（"already running"）
   └─ 为 false → 什么都不打
无论哪种情况，都继续往下：
   is_texpresso_tonic_running = true
   spawn(tonic)              ← 注意：这里没有 early return！
```

关键就在「无论哪种情况都继续往下」——`if` 分支里**只有一条 `warn` 日志，没有任何 `return`**。所以这个标志目前的作用仅仅是「在日志里留个痕」，并没有拦住第二次 spawn。

#### 4.3.3 源码精读

先看「检查」这段，注意 `if` 体里只有 `warn`：

- [src/server.ts:183-186](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L183-L186) —— `if (connection.is_texpresso_tonic_running) { connection.console.warn(...) }`，紧接着**无条件** `connection.is_texpresso_tonic_running = true`。`if` 块里没有 `return`，所以即使检测到「已在运行」，流程仍会走到下面的 `spawn`。这就是「只警告、不拦截」。

再看 `exit` 回调里的复位与 rescan：

- [src/server.ts:191-194](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L191-L194) —— 进程退出后：复位 `is_texpresso_tonic_running = false`，然后 `texpressoProcess.sendCommand("rescan", [])`。

`rescan` 是一条发给**长驻预览进程**的命令，最终走的是 u2-l3 那套 NDJSON 协议：

- [src/process-manager.ts:103-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L103-L110) —— `sendCommand` 把 `["rescan"]`（命令名 + 空 data）序列化成一行 JSON 写进 `texpresso` 的 `stdin`。注意它开头有健康守卫：`if (!this.isRunning || !this.process?.stdin || !this.process?.stdout)` 就会抛错。由于预览进程是长驻的，正常情况下 `rescan` 都能发出去。

旁边那句 TODO 是作者对 rescan 的优化设想：

- [src/server.ts:193](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L193) —— `// TODO maybe only do this if changes detected to sha of aux file?`。思路是：计算 `.aux` 文件内容的哈希（sha），只有哈希真的变了（说明引用解析结果确实变了）才发 `rescan`，否则跳过。这是为了避免「保存了一个和引用无关的改动，却也白跑一次 rescan」。

**现在来正面回答那个标志到底防住了什么。** 设想用户在 tonic 编译期间（大约几百毫秒到几秒）连续按了两次保存。Node 虽然是单线程、`onDidSave` 同步段不会被打断，但两次保存是**两次独立的事件**，第二次完全可以在第一次的 tonic 还没退出时到来：

| 时刻 | 事件 | `is_texpresso_tonic_running` | tonic 进程数 | 发出的 rescan |
| --- | --- | --- | --- | --- |
| t1 | 保存1：进入 onDidSave | false → true | 1（tonic1 启动） | — |
| t2 | 保存2：进入 onDidSave（tonic1 还在跑） | 已是 true → 打 warn，仍置 true | **2**（tonic2 也启动了！） | — |
| t3 | tonic1 退出 | true → false | 1 | rescan（第 1 次） |
| t4 | tonic2 退出 | false → false | 0 | rescan（第 2 次） |

由此得出三个结论：

1. **重入没被防住**：第二个 tonic 照样被 spawn 了，`if` 只是打了个 warn。
2. **标志会「说谎」**：t3 时刻 tonic1 退出把标志复位成 false，但此时 tonic2 还在跑——标志已经不能反映「到底有没有 tonic 在运行」。
3. **rescan 被多发了一次**：每个 tonic 退出都会发一次 rescan，所以两次保存 = 两次编译 + 两次 rescan。

所以严格地说，当前代码**既不存在「丢更新」**（每次保存的编译都跑了），**也没有真正的「防重入」**（标志只警告、不拦截）；它的真实效果是「允许并发编译 + 多发 rescan + 一个不太可靠的运行状态标志」。这是一个典型的「**名字像守卫，实则是日志**」的实现 smell，也是 u3-l4 会讨论的技术债之一。

#### 4.3.4 代码实践

**实践目标**：用一个最小脚本复现「连续两次触发时标志会说谎」的现象，验证 4.3.3 的分析。

**操作步骤**：

1. 新建下面这个**示例代码**脚本（用 `sleep` 模拟 tonic 的编译耗时，不依赖真实 texpresso）：

   ```js
   // demo-reentrancy.js —— 示例代码：复现 is_texpresso_tonic_running 的缺陷
   const { spawn } = require("child_process");
   let isRunning = false;

   function onSave(label) {
       if (isRunning) console.log(`[${label}] warn: already running`);
       isRunning = true;                         // 无条件置真，没有 early return
       console.log(`[${label}] spawning (模拟 tonic)`);
       spawn("sleep", ["1"]).on("exit", () => {  // 模拟 tonic 编译 1 秒
           console.log(`[${label}] ended`);
           isRunning = false;                    // 复位
           console.log(`[${label}] 发出 rescan`);
       });
   }

   // 紧挨着触发两次保存，中间间隔 0.1 秒（远小于 1 秒编译时间）
   onSave("保存1");
   setTimeout(() => onSave("保存2"), 100);
   ```

2. 运行 `node demo-reentrancy.js`。

**需要观察的现象**：
- 开头立刻看到两次 `spawning`——证明第二次没被拦下，两个「tonic」并发存在。
- 约 1 秒后两个 `ended` 依次出现，各发一次 `rescan`——共两次 rescan。
- 在「保存1 ended」之后、「保存2 ended」之前的那段窗口里，`isRunning` 已经是 `false`，但第二个进程其实还在跑——标志在说谎。

**预期结果**：脚本输出印证 4.3.3 的表格——当前实现**没有真正防重入**，且 `is_texpresso_tonic_running` 在并发场景下不能如实反映「是否有 tonic 在运行」。

> 进一步思考（可选）：如果把 `if (isRunning) { warn }` 改成 `if (isRunning) { warn; return; }`（加 early return），会发生什么？答：第二次保存会被**直接丢弃**，不触发编译——这就从「允许并发但冗余」变成了「真正防重入但会**丢更新**」（保存2 的改动要等保存3 才有机会被编译）。两种方案各有代价，这正是「防重入」设计需要权衡的地方。

#### 4.3.5 小练习与答案

**练习 1**：`rescan` 是发给谁的？它和 tonic 是什么关系？

**参考答案**：`rescan` 发给**长驻的 `texpresso` 预览进程**（经 `sendCommand` 写进它的 stdin）。关系是：tonic 负责「编译一遍、更新辅助文件」，rescan 负责「让预览进程把更新后的辅助文件重新读进来」。两者一前一后，才完成「保存 → 预览里的引用刷新」。

**练习 2**：为什么说当前 `is_texpresso_tonic_running`「没有真正防重入」？请用一句话指出代码层面的证据。

**参考答案**：因为 `if (connection.is_texpresso_tonic_running)` 的分支体里**只有一条 `warn` 日志、没有 `return`**，所以即使检测到「已在运行」，流程仍然会继续走到 `spawn`，第二个 tonic 照样会被启动。

**练习 3**：第 193 行的 TODO 想优化什么？它和第 187–188 行的 FUTURE 是什么关系？

**参考答案**：TODO 想用 `.aux` 文件的 sha 哈希来判断「辅助数据有没有真的改变」，只有变了才发 rescan，避免无谓的 rescan。它和 FUTURE 是**配套的两层优化**：FUTURE 针对「要不要跑这一遍 tonic」（若无未解析引用就不编译），TODO 针对「编译完了要不要 rescan」（若 .aux 没变就不 rescan）。前者省编译，后者省刷新。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个任务。

**任务**：手绘一张「从保存事件到预览更新」的**完整时序图**，并附一段**并发评估**。

要求：

1. **时序图**至少包含四个参与者：`用户/编辑器`、`texpresso-lsp (onDidSave)`、`texpresso-tonic 进程`、`texpresso 预览进程`。画出下列交互的时间顺序：
   - 编辑器 → onDidSave：`didSave` 通知。
   - onDidSave 内部：读 `root_tex`、拼 tonic 路径、（看标志打 warn）、置标志、`spawn tonic`。
   - onDidSave → tonic 进程：启动。
   - tonic 进程 → onDidSave：`exit`（异步）。
   - onDidSave（在 exit 回调里）→ 预览进程：`sendCommand("rescan")`。
2. 在图上**标出两个异步空隙**：`spawn` 与 `exit` 之间的「编译窗口」，以及 `exit` 与 `rescan` 之间的瞬时回调。
3. **并发评估**：基于你在 4.3 看到的真实代码，回答——如果在「编译窗口」内用户再次保存，当前实现会不会：
   - (a) 启动第二个 tonic？（会 / 不会，给出代码证据）
   - (b) 丢掉第二次保存对应的编译？（会 / 不会）
   - (c) 让 `is_texpresso_tonic_running` 的值变得不可靠？（会 / 不会，说明在哪段时间内不可靠）
4. （可选进阶）写一段**改进建议**：若要「既防重入又不丢更新」，你会怎么改？（提示：考虑「正在运行时记一个 `pending` 标志，等当前 tonic 退出后再补跑一次」的方案。）

**交付物**：一张时序图（ASCII 或任意画图工具均可）+ 上面三个问题的简短回答。这个任务把本讲的「链路理解」和「并发分析」两件事一次性串了起来。

## 6. 本讲小结

- 「保存即重编译」的完整链路是：`onDidSave` → `spawn texpresso-tonic` → 进程 `exit` → `sendCommand("rescan")`，其中编译和刷新分别由**两个不同的外部可执行文件**承担。
- `texpresso-tonic` 是一个**用完即弃**的独立编译器进程，texpresso-lsp 对它「点火就忘」：不读它的 stdout、不写它的 stdin，只挂一个 `exit` 监听。它的路径由 `texpresso_path + "-tonic"` 字符串拼接得到。
- `rescan` 是发给**长驻预览进程**的命令，让它重读 tonic 刚更新的辅助文件，从而使预览里的引用刷新。它走的是 u2-l3 的 NDJSON 协议，并受 `sendCommand` 的健康守卫保护。
- `is_texpresso_tonic_running` **名义上是防重入标志，实际只打警告、不拦截**：`if` 分支里没有 `return`，第二次保存仍会启动第二个 tonic，标志在并发下也会变得不可靠。这是当前实现的一个技术债。
- 作者留了两条优化设想：FUTURE（若无未解析引用就跳过编译）与 TODO（若 `.aux` 的 sha 未变就跳过 rescan），分别针对「要不要编译」「要不要刷新」两层。
- `onDidSave` 忽略参数 `event`，无论保存哪个文件都编译根文档 `root_tex`——符合 LaTeX 的编译模型，但无法做子文件级增量编译。

## 7. 下一步学习建议

本讲讲完了一条「由保存驱动的、LSP 外部」的编译链路。接下来：

- **u3-l2（SyncTeX 正反向搜索）**：转向另一条功能链路——预览窗口和源码之间如何互相跳转。它会复用本讲的 `sendCommand`、`spawn`、`connection.init_options` 等机制，并展示「借壳 `documentHighlight` 触发 `synctex-forward`」的巧思。
- **u3-l3（错误处理与进程生命周期）**：系统梳理本讲（以及 u2-l2）里那些 `spawn`、`exit`、`error` 事件是如何被转发与兜底的。本讲 tonic 进程的 stderr 被完全忽略（4.2.5 提到的可观测性缺口），正是 u3-l3 会讨论的「错误处理覆盖度」问题的一部分。
- 若你对本讲的并发隐患感兴趣，可以带着 4.3 的分析去读 **u3-l4（架构取舍与二次开发）**，那里会从整体视角讨论这些技术债，并给出「新增一个 texpresso 命令」的扩展范式——你可以试着把「补跑一次 tonic」的改进方案落实成一次真实的二次开发。
