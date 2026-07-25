# 架构取舍与二次开发

## 1. 本讲目标

本讲是整个学习手册的收尾篇。前面 12 篇讲义我们一直在「钻进去」——一篇精读一个机制（配置、进程、协议、文档同步、预览、SyncTeX、错误处理）。本讲要「跳出来」，站在整体视角审视 `texpresso-lsp` 这套代码的设计取舍与可扩展性。学完本讲你应该能够：

1. 说清「薄封装（thin wrapper）」设计哲学的内涵——为什么用一个 Node/TypeScript 小程序包裹 `texpresso` 是合理的，以及这种选择带来了哪些固有的能力边界。
2. 掌握「新增一对 `texpresso` 命令」需要改哪些代码、哪些地方**完全不用改**（这正是二次开发最高频的场景）。
3. 理解「运行期工作区配置」如何热更新——`onInitialized` 拉初值、`onDidChangeConfiguration` 监听变更、消费方现读现用，以及它与「不可变的初始化选项」的对称与不对称。
4. 能在源码里识别出所有的 `TODO` / `FUTURE` 注释、未使用的脚手架类型、README 里尚未实现的功能，并能判断它们各自的工程含义与改进思路。

本讲不再引入新的运行期机制，而是把 `server.ts` / `types.ts` / `process-manager.ts` / `README.md` 当作一份「待评审的设计文档」来读。前置讲义里已经讲透的细节本讲只做一句话回顾，重点放在「为什么这样设计」与「怎样安全地改造它」。

## 2. 前置知识

阅读本讲前，你需要已经建立以下心智模型（都在前置讲义讲过，这里只做最简回顾）：

- **三层架构**：编辑器（LSP 客户端）⇄ `texpresso-lsp`（翻译官，本项目的全部代码都在这一层）⇄ `texpresso` 子进程（真正的渲染/编译/SyncTeX 计算都在这里）。本项目是「中间的薄薄一层」。见 [src/server.ts:65-70](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L70)。
- **NDJSON 协议与命令分发**：子进程 stdout 每行一个 JSON 数组 `[command, ...data]`；`process-manager` 用 `emit(command, data)` 动态分发（命令名是运行期字符串，传输层对命令名一视同仁）。见 [src/process-manager.ts:41-44](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L41-L44)。
- **两类配置**：`ServerConfig`（初始化选项，握手时一次性传入、运行期不可改）与 `WorkspaceSettings`（工作区设置，可热更新）。见 [src/types.ts:15-27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L15-L27)。
- **`connection` 是混入对象**：用对象展开把 LSP 库方法与应用状态（`init_options` / `workspace_config` / `is_texpresso_tonic_running`）合并到一起，所以任何回调都能直接读到最新配置。见 [src/server.ts:33-38](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L33-L38)。

再补充一个本讲反复用到的事实：项目对外只声明了**两个** LSP 能力（`textDocumentSync` 与 `documentHighlightProvider`），见 [src/server.ts:118-123](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L118-L123)。「能力清单这么短」本身就是薄封装的体现——稍后会展开。

## 3. 本讲源码地图

本讲把四个文件摆在一起，做横向的「设计评审」：

| 文件 | 行数 | 在本讲的角色 |
| --- | --- | --- |
| [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) | 282 | 业务编排层：所有 `emit` 监听器、所有 `sendCommand` 调用、所有 `TODO`/`FUTURE` 注释、配置热更新都在这里 |
| [src/process-manager.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts) | 115 | 传输层：命令无关的 `emit` 与 `sendCommand`，是「薄封装」最关键的承重墙 |
| [src/types.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts) | 48 | 类型定义：2 个在用、5 个未用的脚手架类型 |
| [README.md](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/) | 77 | 设计意图的「第一手供词」：作者亲口说明的「cheap nodeJS / thin interface」取舍与未实现功能清单 |

一句话定位：`server.ts` 是「**做什么**」，`process-manager.ts` 是「**怎么搬运**」，`types.ts` 是「**打算做什么但还没做**」，`README.md` 是「**作者为什么这么做**」。

## 4. 核心概念与源码讲解

### 4.1 薄封装哲学

#### 4.1.1 概念说明

「薄封装（thin wrapper）」是一种架构选择：**把绝大部分真正的工作交给一个成熟的外部组件，自己只做「协议翻译」这一件最适合自己的事。** 在 `texpresso-lsp` 里，这个划分非常彻底——

- **本项目（JS/TS 这一层）负责的事**：LSP 协议的收发、把编辑器事件翻译成 `texpresso` 命令、把 `texpresso` 事件翻译回编辑器动作、JSON 的解析与拼装、子进程的生灭管理。
- **本项目明确不碰的事**：LaTeX 的渲染（由 `texpresso` 子进程做）、编译（由 `texpresso-tonic` 做）、PDF 与源码的坐标映射（SyncTeX，由 `texpresso` 算）。

作者在 README 里把这套取舍讲得很直白，这是本课程里少有的「设计意图第一手供词」：

[README.md:11-17](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L11-L17) —— 注意三句话的关键词：`experimental`（实验性，别指望生产级）、`"cheap" nodeJS implementation`（用最省事的语言快速搭起来）、`thin interface where the JavaScript is not responsible for much`（接口很薄、JS 不扛活）、`JSON parsing and manipulation is stuff that is well suited to`（而 JSON 处理恰恰是 JS 的强项）。**这四句合起来就是整个项目的「宪法」。**

为什么 JS/TS 适合做这层薄壳？因为它要处理的全部是 JSON 文本与异步事件流，这正是 Node 的看家本领；而渲染 LaTeX 需要的字体排版、SyncTeX 需要的 PDF 解析，全是 C 的强项（`texpresso` 本体就是 C 写的）。让每种语言干自己擅长的事，正是「薄封装」成立的前提。

#### 4.1.2 核心流程

薄封装的「薄」最直观地体现在**能力清单的长度**上。一个 LSP 服务器能对外宣称多少能力，取决于它的 `onInitialize` 返回的 `capabilities` 对象。本项目只声明了两项：

```text
onInitialize 返回的 capabilities
   ├─ textDocumentSync: Incremental      （接收编辑器的文档增量变更）
   └─ documentHighlightProvider: true    （借壳触发正向 SyncTeX，见 u3-l2）

就这两项。没有 hover、completion、definition、diagnostics……
```

[src/server.ts:118-123](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L118-L123) —— 能力清单越短，意味着 `texpresso-lsp` 对编辑器承诺的事越少，也就越不容易出错、越好维护。

薄封装带来的另一个红利是**新增命令的边际成本极低**。传输层（`process-manager`）对命令名是「无视」的——`emit` 与 `sendCommand` 都把命令名当成普通字符串搬运，不针对任何具体命令写逻辑。用成本模型表达：

\[
\text{接入 } N \text{ 条命令的总成本} \;=\; \underbrace{0}_{\text{每条命令的传输层改动}} \times N \;+\; N \cdot \underbrace{c_{\text{业务}}}_{\text{每条命令的业务处理}}
\]

也就是说，传输层对每一条命令都「免税」，你只需为每条命令的业务处理付一次成本。如果传输层是「命令敏感」的（每条命令都要在传输代码里登记一次），上式第一项就会变成 \( c_{\text{传输}} \cdot N \)，N 越大越痛。这正是 4.2 要展开的扩展点红利。

当然，薄封装也有代价，集中体现在**对外部组件的强依赖**上：

- `texpresso` 子进程一旦崩溃，`texpresso-lsp` 没有任何自愈能力（见 u3-l3：`exit` 只打日志不重启），因为「渲染」这摊活被完全外包了，外包方罢工，薄壳只能跟着瘫痪。
- 协议紧密耦合：`texpresso` 那边 NDJSON 的命令名或数据格式一变，这边就得跟着改。

这是典型的**「用依赖换简单」**的取舍——README 里 `experimental` 一词已诚实标明了它的定位。

#### 4.1.3 源码精读

**能力清单短得不能再短。**

[src/server.ts:118-123](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L118-L123) —— 整个 `capabilities` 对象只有两个字段。对比一个「厚」的 LSP 服务器（如 rust-analyzer），它的 capabilities 动辄十几项。这里的两项恰好对应「最小可用」：文档同步是预览的前提，`documentHighlightProvider` 是正向搜索的借壳入口（详见 u3-l2）。

**传输层对命令名「一视同仁」。**

[src/process-manager.ts:103-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L103-L110) —— `sendCommand(command, data)` 把任意命令名 `command` 与任意数据 `data` 拼成 `[command, ...data]` 再 `JSON.stringify`。这里没有任何 `if (command === "open")` 之类的分支——**命令名不参与任何传输层逻辑**。

[src/process-manager.ts:41-44](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L41-L44) —— 收方向同理：`emit(command, data)` 用运行期拿到的字符串 `command` 作为事件名直接分发。一条新命令只要子进程会发、`server.ts` 会接，传输层**一行都不用改**。这就是上面那个成本公式里第一项为 0 的代码依据。

**应用状态与库方法同居于 `connection`。**

[src/server.ts:33-38](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L33-L38) —— 用对象展开 `...createConnection(ProposedFeatures.all)` 把整个 LSP 连接的方法混入一个普通对象，再往里塞 `init_options` / `workspace_config` / `is_texpresso_tonic_running` 三个应用字段。这种「不建类、直接混入」的写法很省代码，也是「便宜实现」气质的一部分——它让任何回调都能用 `connection.xxx` 同时访问协议方法和业务状态。

#### 4.1.4 代码实践

**实践目标**：亲手把「本项目负责什么 / 不负责什么」这件事在源码里验证一遍，建立一张清晰的责任边界图。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts)，逐个回调判断：「这一段是在做协议翻译，还是在做真正的渲染/编译计算？」把它们填进下表（已给出前两行作示范）：

   | 回调 / 代码块 | 行号 | 做的事属于 | 责任归属 |
   | --- | --- | --- | --- |
   | `onDidOpen` | L169-177 | 把文档全文翻译成 `open` 命令发出去 | 协议翻译（本项目） |
   | `onDidSave` | L179-196 | spawn `texpresso-tonic`、exit 后发 `rescan` | 编排（本项目点火，tonic 干活） |
   | synctex 监听器 | L95-110 | ？ | ？ |
   | `onDocumentHighlight` | L229-268 | ？ | ？ |
   | stdout 里的 `emit(command, data)` | process-manager L41-44 | ？ | ？ |

2. 回答一个判断题：在整份 `server.ts` 里，能不能找到**任何一处**真正「计算 PDF 像素」或「解析 `.synctex.gz` 文件」的代码？预期是找不到——这证明渲染与坐标映射确实被完全外包给了子进程。

**需要观察的现象 / 预期结果**：

- 步骤 1 的表中，`onDidOpen` / `onDidSave` / synctex / `onDocumentHighlight` 全部属于「翻译或编排」，没有一处是「真正的渲染/编译计算」。
- 步骤 2 预期：**找不到**任何渲染或坐标计算代码。这就是薄封装的可验证特征——责任边界完全由「哪些代码不存在」来界定。

> 若本地不便逐行核对，可标注「待本地验证」，但结论应稳定。

#### 4.1.5 小练习与答案

**练习 1**：作者在 README 里用了 `experimental` 和 `"cheap"` 两个词来描述本项目。这两个词分别对使用者意味着什么？

**参考答案**：`experimental` 意味着它不是生产级产品，可能有不完整的特性（如未实现的正向搜索）和已知的健壮性缺口（如进程崩溃不可恢复，见 u3-l3），使用者应把它当作「快速验证想法的工具」而非「稳定基础设施」。`"cheap"`（便宜/省事）强调实现成本低——用 Node/TS 几百行代码就搭起一座 LSP 桥梁，换来的是可快速迭代，代价是放弃了一些「重」的工程化能力（如自动重连、精细日志分级）。两个词合起来框定了本项目的定位：**够用的实验性薄壳**。

**练习 2**：如果未来要让 `texpresso-lsp` 在 `texpresso` 子进程崩溃后自动重连，这算是「修 bug」还是「改变架构定位」？

**参考答案**：更偏向后者。自动重连意味着 `texpresso-lsp` 不再是「点火就忘」的薄壳，而要承担「进程监督 + 状态恢复 + 重放未完成命令」的职责——这恰恰是 README 里 `JavaScript is not responsible for much` 那句话所回避的「重活」。所以加这个能力会显著加厚这层壳，与当前的薄封装哲学有张力。它未必不该做，但做之前要先想清楚：是不是该让 `texpresso` 本体自己更健壮，而不是在薄壳里打补丁。

---

### 4.2 新增命令的扩展点

#### 4.2.1 概念说明

二次开发里最高频的需求是：「`texpresso` 新增了一条命令（或一个事件），我怎样在 `texpresso-lsp` 里接上它？」因为传输层是命令无关的（4.1 已证），这件事被拆成了**两个互相独立的端点**，每个端点各自改一处即可：

- **收方向（`texpresso` → 服务器）**：在 `server.ts` 里加一个 `texpressoProcess.on("命令名", handler)` 监听器。**`process-manager.ts` 不用动**，因为 `emit(command, data)` 是动态分发的，新命令名会自动走到这条通道。
- **发方向（服务器 → `texpresso`）**：在 `server.ts` 里某处加一行 `texpressoProcess.sendCommand("命令名", [...data])`。**`process-manager.ts` 也不用动**，因为 `sendCommand` 是通用的。

这两个端点彼此独立：你可以只收不发（被动监听某事件），也可以只发不收（主动触发某命令而不关心回复）。这正是「薄封装」最甜的红利——u3-l3 结尾曾把它作为预告，本节正式展开。

本项目里现成的「收方向」例子有两个：`synctex`（反向搜索，有完整业务逻辑）与 `append-lines`（**目前只打一行日志，没有任何业务处理**——这正是本讲实践任务的抓手）。现成的「发方向」例子有四个：`open`、`change-range`、`close`、`rescan`、`synctex-forward`。

#### 4.2.2 核心流程

接入一条新命令的检查清单（以「收一个 `foo` 事件」+「发一个 `bar` 命令」为例）：

```text
【收方向：texpresso 发 "foo" 事件】
   子进程 stdout 一行 ["foo", ...]
        │  process-manager.ts 完全不改
        ▼
   emit("foo", data)   ── 自动分发（L41-44，命令名无关）
        │
        ▼
   你在 server.ts 加一行：
   texpressoProcess.on("foo", (data) => { /* 你的业务 */ })

【发方向：服务器发 "bar" 命令】
   你在 server.ts 某回调里加一行：
   texpressoProcess.sendCommand("bar", [/* 数据 */])
        │  process-manager.ts 完全不改
        ▼
   JSON.stringify(["bar", ...]) + "\n"  → stdin  （L108-109）
```

一句话记忆：**「收改监听、发改调用、传输层永不动」**。

对照本项目已有的命令对，可以验证这张清单：

| 命令名 | 方向 | `server.ts` 里的落点 | `process-manager.ts` 是否改动 |
| --- | --- | --- | --- |
| `synctex` | 收 | L95-110 监听器 | 否 |
| `append-lines` | 收 | L112-116 监听器（仅打日志） | 否 |
| `open` | 发 | L176 | 否 |
| `change-range` | 发 | L222 | 否 |
| `close` | 发 | L201 | 否 |
| `rescan` | 发 | L194 | 否 |
| `synctex-forward` | 发 | L250-253 | 否 |

最后一列全是「否」——这就是薄封装的硬证据。

#### 4.2.3 源码精读

**收方向的「完整版」样板：`synctex` 反向搜索。**

[src/server.ts:95-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L95-L110) —— 这是「收一个事件并做有意义的业务」的范例：从 `data` 取路径与行号、替换 `%f`/`%l` 占位符、`spawn` 编辑器命令。它读到的 `data` 就是 `command_list.slice(1)`（即命令名之后的所有元素）。

[src/process-manager.ts:42-43](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L42-L43) —— `command_list[0]` 当事件名、`command_list.slice(1)` 当数据。**这段代码不知道 `synctex` 这个词的存在**——它对所有命令一视同仁，所以新增命令时它天然免疫。

**收方向的「待完成版」样板：`append-lines`（本讲实践抓手）。**

[src/server.ts:112-116](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L112-L116) —— 监听器里只有一句 `connection.console.log(`Append lines received: ${JSON.stringify(data)}`)`，**没有任何业务处理**。这条事件目前等于「收到即丢弃进日志」。它是一个完美的扩展练习入口：传输层已经把 `data` 送到了，只等你在监听器里写真正的处理逻辑。

> 注意：本仓库没有文档说明 `append-lines` 的 `data` 具体结构（它取决于 `texpresso` 子进程实际发什么）。设计处理逻辑前，需要先观察真实运行时 `JSON.stringify(data)` 打出的内容来确认格式——这点会在 4.2.4 实践里强调。

**发方向的样板：`sendCommand` 的各个调用点。**

[src/server.ts:176](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L176) —— `sendCommand("open", [path, text])`：命令名 + 数据数组，就这两步。

[src/server.ts:194](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L194) —— `sendCommand("rescan", [])`：即便没有数据，也要传空数组 `[]`，因为协议规定第 0 位之后都是数据。

[src/process-manager.ts:108-109](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L108-L109) —— `JSON.stringify([command, ...data])` 把命令名与数据展平成单个 JSON 数组，再补一个 `\n` 作为 NDJSON 的行分隔符。发方向同样**不认识任何具体命令名**。

#### 4.2.4 代码实践

**实践目标**：为「只打日志」的 `append-lines` 事件设计一个更有意义的处理逻辑，并精确写出「收 / 发两端各需要补哪些代码」——这是规格里点名的核心实践。

**操作步骤**（设计 + 编码型实践；若要真正运行需本机有 `texpresso`）：

1. **先确认数据格式**。`append-lines` 的 `data` 结构本仓库未文档化。做法是：开启编辑器的 LSP 输出面板（本项目的 `connection.console.log` 会以 `window/logMessage` 形式打过去），触发一次会产生 `append-lines` 的操作，观察 `Append lines received: [...]` 这一行的真实内容，记下 `data` 是「字符串数组」「对象数组」还是别的。**格式未确认前，下面的处理逻辑只能写成与格式弱相关的骨架。**

2. **设计一个「格式化输出到 LSP 日志」的处理**（最小改进）。把当前的一行 `JSON.stringify` 替换成更可读的逐行输出。示例代码（非项目原有代码）：

   ```ts
   // 示例代码：替换 src/server.ts:112-116 的监听器体
   texpressoProcess.on("append-lines", (data) => {
       // data 的确切结构需先按步骤 1 确认；这里假设它是若干文本行的数组
       if (Array.isArray(data)) {
           connection.console.log(
               `append-lines: 收到 ${data.length} 项`,
           );
           data.forEach((item, i) => {
               connection.console.log(`  [${i}] ${JSON.stringify(item)}`);
           });
       } else {
           connection.console.warn(`append-lines: 非预期格式 ${JSON.stringify(data)}`);
       }
   });
   ```

3. **（进阶）设计「转换成 LSP diagnostics」的处理**。若步骤 1 确认 `data` 里含有「文件路径 + 行号 + 文本」之类的结构，可考虑用 `connection.sendDiagnostics({ uri, diagnostics })` 把它变成编辑器里的波浪线提示。这需要先在 `types.ts` 借用现成的 `CustomDiagnostic` 类型（见 4.4.3），并构造 `Range`/`Position`。这属于较重的改造，建议在理解 4.4 之后再动手。

4. **填写「两端改动清单」**——这是本实践的核心交付物。请按下表回答「要接入一个新的 `append-lines` 业务处理，需要在协议两端各补什么代码」：

   | 端点 | 文件 | 是否需要改动 | 改什么 |
   | --- | --- | --- | --- |
   | 收方向·emit 侧 | `process-manager.ts` L41-44 | 否（动态分发，命令名无关） | 无 |
   | 收方向·监听侧 | `server.ts` L112-116 | **是** | 把 `console.log` 换成你的业务逻辑 |
   | 发方向·sendCommand 侧 | `server.ts` | ？（见下） | ？ |

   关键不对称（请你想清楚再填）：`append-lines` 是 `texpresso` **主动发给我们**的事件，我们**从不主动向 `texpresso` 发 `append-lines`**。所以「发方向」这一端对本事件而言**根本不需要任何代码**。这恰好说明 4.2.2 清单里「收 / 发彼此独立」——并非每条命令都两端齐备。

**需要观察的现象 / 预期结果**：

- 步骤 1：能在输出面板看到 `Append lines received: [...]` 的真实内容，从而确定 `data` 结构（若本机无 `texpresso`，标注「待本地验证」）。
- 步骤 2：替换后，`append-lines` 的日志从「一坨 JSON」变成「逐行可读」。
- 步骤 4：应得出结论——接入 `append-lines` 业务**只需改 `server.ts` 的监听器一处**，`process-manager.ts` 零改动，且不需要新增任何 `sendCommand` 调用。

> 若无法本地运行，请把步骤 2 的代码作为「待验证的设计方案」提交，并明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假设 `texpresso` 新增了一条「编译进度」事件 `progress`，你想把它显示到编辑器。你需要改 `process-manager.ts` 吗？

**参考答案**：不需要。`process-manager.ts` 的 `emit(command, data)`（L41-44）对命令名是动态的，`"progress"` 这个新名字会自动走通整条收方向通道。你只需在 `server.ts` 加一行 `texpressoProcess.on("progress", (data) => connection.console.log(...))` 即可。这就是 4.1 成本公式里「传输层免税」的实操体现。

**练习 2**：`sendCommand("rescan", [])` 里为什么必须传空数组 `[]`，而不能写成 `sendCommand("rescan")`？

**参考答案**：因为 `sendCommand` 的签名是 `(command: string, data: any[])`，且发送时用 `[command, ...data]` 展平（见 [src/process-manager.ts:108](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L108)）。协议规定每条消息都是一个数组、第 0 位是命令名、其余是数据。`rescan` 没有数据，所以数据部分是空数组，展平后就是 `["rescan"]`——仍是一个合法的「命令名 + 零个数据元素」的数组。若省略 `[]`，`data` 为 `undefined`，`...undefined` 会抛错。

**练习 3**：4.2.4 的「两端改动清单」里，「发方向」对 `append-lines` 不需要任何代码。试举一个**反过来**的例子——只发不收的命令。

**参考答案**：`rescan` 就是。`server.ts` 在 tonic 退出后 `sendCommand("rescan", [])`（L194）主动发给子进程，但 `server.ts` 里**没有** `texpressoProcess.on("rescan", ...)` 监听器——我们不关心 `texpresso` 对 `rescan` 有无回复（甚至它根本不回复）。这说明「发不一定要配收」，两端的独立性是真实的。

---

### 4.3 运行期配置热更新

#### 4.3.1 概念说明

`texpresso-lsp` 有两类配置，它们的生命周期截然不同（u2-l1 已建立过这条区分，本节聚焦「热更新」这一面）：

- **`ServerConfig`（初始化选项）**：握手时经 `initializationOptions` 一次性传入，存进 `connection.init_options`，**运行期不可改**。要改只能重启服务器。包括 `root_tex`、`texpresso_path`、`inverse_search`。
- **`WorkspaceSettings`（工作区设置）**：经 LSP 的 `workspace/configuration` 机制拉取与监听，存进 `connection.workspace_config`，**运行期可热更新**。目前只有 `preview_follow_cursor` 一个开关。

「热更新」之所以能成立，依赖三件事咬合：

1. **拉初值**：握手完成后（`onInitialized`）主动向客户端要一次当前配置。
2. **监听变更**：用户在编辑器里改设置时，客户端会推 `didChangeConfiguration`，服务器监听并更新本地副本。
3. **现读现用**：消费方（目前是 `onDocumentHighlight`）每次执行时直接读 `connection.workspace_config` 的最新值，不做任何缓存。

这套机制的精妙在于：**消费方完全不需要知道配置何时变了**——它永远读「此刻的真相」，因为真相只存在一个地方（`connection.workspace_config`）。

#### 4.3.2 核心流程

```text
握手阶段
  onInitialize  ► 读 initializationOptions（一次性，不可变）► connection.init_options
  ─────────────  （此处不碰 workspace_config）
握手完成后
  onInitialized ► connection.workspace.getConfiguration()
                   ► connection.workspace_config.preview_follow_cursor = 用户值 ?? 默认值

运行期：用户在编辑器改了设置
  客户端推送 didChangeConfiguration
  onDidChangeConfiguration ► change.settings.preview_follow_cursor
                   ► connection.workspace_config.preview_follow_cursor = 新值

运行期：消费
  onDocumentHighlight ► 读 connection.workspace_config.preview_follow_cursor（永远是最新）
```

注意一个不对称：`onInitialize` 只合并 `init_options`、**完全不碰** `workspace_config`；`workspace_config` 的初值要等到 `onInitialized` 才拉。这意味着在「`onInitialize` 结束」到「`onInitialized` 执行」之间的极短窗口里，`workspace_config` 还是默认值。因为此时文档同步等业务尚未真正开始，这个窗口在实践中无害，但它是理解两条配置生命周期差异的关键细节。

#### 4.3.3 源码精读

**默认值与合并运算符。**

[src/server.ts:29-31](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L29-L31) —— `defaultWorkspaceSettings` 的默认值：`preview_follow_cursor: true`。所有「用户没给值」的情况都回落到这里。

**拉初值：`onInitialized`。**

[src/server.ts:134-150](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L134-L150) —— 握手完成后异步拉取工作区配置。核心是 [L136](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L136) 的 `connection.workspace.getConfiguration()`，拿到后用 `??` 合并：

[src/server.ts:138-140](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L138-L140) —— `config.preview_follow_cursor ?? defaultWorkspaceSettings.preview_follow_cursor`。`??`（空值合并）的含义见 u2-l1：仅当左侧为 `null`/`undefined` 时才取右侧。整个调用包了 try/catch 且 **swallow**（见 u3-l3：拉不到配置也不影响主流程，回落默认值继续跑）。

**监听变更：`onDidChangeConfiguration`。**

[src/server.ts:153-167](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L153-L167) —— 用户改设置时触发。注意 [L157](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L157) 用的是 `!== undefined` 而非 `??`：只有当新配置里**显式包含** `preview_follow_cursor` 字段时才更新，避免用一次「全量配置推送」里恰好没有该字段就把本地值抹掉。这是一处比 `onInitialized` 更细致的守卫。

**现读现用：`onDocumentHighlight`。**

[src/server.ts:232-237](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L232-L237) —— `if (!connection.workspace_config.preview_follow_cursor)` 现场读取。这里没有任何「订阅」「缓存」「上次值」的概念——它直接读 `connection.workspace_config` 这个共享对象，而该对象会被 `onDidChangeConfiguration` 实时改写。**「单一真相 + 现读现用」是热更新无需通知消费方的根本原因。**

#### 4.3.4 代码实践

**实践目标**：亲手验证 `preview_follow_cursor` 的运行期热更新，观察「改设置 → 行为立即变化」的闭环。

**操作步骤**：

1. 确保服务器已连上编辑器、预览正常工作、`preview_follow_cursor` 为默认 `true`。在编辑器里移动光标，观察预览窗口是否跟随（应跟随）。
2. 在编辑器的工作区设置里把 `texpresso.preview_follow_cursor` 改为 `false`（README 的设置名见 [README.md:43-54](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L43-L54)）。
3. 再次移动光标，观察预览窗口是否还跟随。
4. 同时打开 LSP 输出面板，寻找 [src/server.ts:160-162](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L160-L162) 打出的 `Updated workspace settings: preview_follow_cursor = false`，以及 [src/server.ts:233-235](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L233-L235) 打出的 `Document highlight ignored: preview_follow_cursor is disabled`。
5. 把它改回 `true`，确认行为恢复。

**需要观察的现象 / 预期结果**：

- 步骤 2→3：改完设置后**无需重启**服务器，光标移动不再触发预览跟随。
- 步骤 4：输出面板应能看到「配置已更新」和「document highlight 被忽略」两条日志，正好对应 `onDidChangeConfiguration`（写）与 `onDocumentHighlight`（读）两端。

**如果无法在本地复现**（如缺 `texpresso` 或编辑器扩展），请标注「待本地验证」，但应能根据源码推断出上述闭环。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `preview_follow_cursor` 能热更新，而 `inverse_search`（反向搜索命令）不能？要改 `inverse_search` 必须怎么做？

**参考答案**：因为两者属于不同的配置类别。`preview_follow_cursor` 是 `WorkspaceSettings`，有 `onInitialized` 拉取 + `onDidChangeConfiguration` 监听的完整热更新链路（L134-167），消费方（`onDocumentHighlight`）现读现用。而 `inverse_search` 是 `ServerConfig`（初始化选项），只在 `onInitialize` 里合并一次（L58-60）、存进 `connection.init_options` 后就冻结，代码里没有任何 `onDidChangeConfiguration` 去更新它。要改 `inverse_search` 必须重启 LSP 服务器、重新握手传入新的 `initializationOptions`。这是「初始化选项 vs 工作区设置」最实际的区别。

**练习 2**：`onDidChangeConfiguration` 里用 `!== undefined` 判断（L157），而 `onInitialized` 里用 `??` 合并（L138-140）。为什么这里要更小心？

**参考答案**：`onInitialized` 是「首次拉取」，本地值还是默认值，用 `??` 合理。而 `onDidChangeConfiguration` 收到的是一次「全量配置推送」，`change.settings` 里可能**恰好没有** `preview_follow_cursor` 字段（比如用户改的是别的设置）。若此时用 `??`，左侧 `undefined` 会触发回落默认值 `true`，反而把用户之前显式设的 `false` 抹掉。用 `!== undefined` 守卫，确保「只有推送里真的带了该字段才更新」，避免误伤。这是一处体现工程细致度的小细节。

---

### 4.4 技术债与未来工作

#### 4.4.1 概念说明

「技术债（technical debt）」指代码里那些「现在能跑、但留了隐患或半成品」的地方。识别技术债是二次开发的必备能力——改造代码前，你得先知道哪里是「设计如此」、哪里是「还没做完」、哪里是「已知有坑」。

`texpresso-lsp` 里的技术债分四类：

1. **显式标注的 `TODO` / `FUTURE` 注释**：作者亲手留下的「待办」，是最善意、最易识别的债。
2. **未使用的脚手架类型**：`types.ts` 里定义了却没被 `server.ts` 引用的接口/枚举，预示着「计划要做但还没做」的功能。
3. **README 里尚未勾选的功能**：作者公开承认的缺口。
4. **散落在前面讲义里的隐性隐患**：如防重入标志名不副实、文档同步无 try/catch、进程崩溃不重启等（u3-l1、u3-l3 已逐条点出）。

本模块把它们汇总成一张「债务清单」，方便你二次开发时按图索骥。

#### 4.4.2 核心流程

```text
技术债四象限
   ├─ 显式注释（作者主动标注）
   │     ├─ server.ts:187-188  FUTURE  跟踪未解析引用以跳过多余编译
   │     ├─ server.ts:193      TODO    仅当 .aux 的 sha 变化才 rescan
   │     └─ server.ts:206      注释    "can this be out of sync?"（文档同步竞态）
   │
   ├─ 未使用脚手架（types.ts 里没人 import）
   │     ├─ CustomDiagnostic / DiagnosticTag  （自定义诊断）
   │     ├─ CustomRule                         （自定义规则）
   │     └─ AnalysisResult / DocumentStatistics（文档分析）
   │
   ├─ README 公开缺口
   │     └─ README.md:9  [ ] Forward search following cursor
   │
   └─ 隐性隐患（前序讲义已点出）
         ├─ is_texpresso_tonic_running 名不副实（只 warn 不 return）   u3-l1
         ├─ 文档同步回调无 try/catch                                    u3-l3
         ├─ 进程崩溃不可恢复                                            u3-l3
         ├─ onDidSave 忽略 event 参数（总是编译 root）                  u3-l1
         └─ inverse_search 整对象合并的缺陷                             u2-l1
```

#### 4.4.3 源码精读

**显式 `FUTURE` / `TODO` 注释——作者亲手留下的路标。**

[src/server.ts:187-188](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L187-L188) —— `// FUTURE keep track of whether there are any undefined reference errors to potentially avoid unnecessary extra compiles?`。作者的设想：如果当前没有未解析的交叉引用，保存时就无需再跑一遍 tonic 编译。这是一个**性能优化**方向的未来工作。

[src/server.ts:193](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L193) —— `// TODO maybe only do this if changes detected to sha of aux file?`。作者的设想：rescan 之前先比对 `.aux` 辅助文件的哈希，没变就不 rescan，避免无谓的预览刷新。同样是性能优化。

[src/server.ts:206](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L206) —— `// can this be out of sync?`。这是 `onDidChangeTextDocument` 里用低层 API 配合 `documents.get(...)` 手动取文档时留下的疑问——存在「变更通知与文档管理器内部状态错位」的潜在竞态（u2-l4 已讨论）。这是唯一一条**正确性**方向的注释，比上面两条性能 TODO 更值得警惕。

**未使用的脚手架类型——「计划要做但还没做」的化石。**

[src/types.ts:4-7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L4-L7) 与 [src/types.ts:9-12](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L9-L12) —— `CustomDiagnostic` 与 `DiagnosticTag`：扩展自 LSP 标准 `Diagnostic`，多了 `code` 与 `tags` 字段。这套类型是为「自定义诊断（波浪线提示）」准备的，但 `server.ts` 里从未 `import` 它们（可以用搜索验证：`server.ts` 只从 `./types` 引入了 `ServerConfig` 与 `WorkspaceSettings`，见 [src/server.ts:15](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L15)）。

[src/types.ts:30-34](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L30-L34) —— `CustomRule`：一个「命名 + 严重级 + check 函数」的结构，明显是为「用户可配置的自定义检查规则」设计的，同样未使用。

[src/types.ts:37-40](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L37-L40) 与 [src/types.ts:43-48](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L43-L48) —— `AnalysisResult` 与 `DocumentStatistics`：行数、字符数、长行数、平均行长等统计字段。这是为「文档分析」功能准备的脚手架，也未使用。

这五个类型合起来勾勒出一个**尚未实现的「自定义诊断 + 文档分析」子系统**的雏形。它们是「化石」，但也是「路标」——如果你想做这个方向的功能，类型已经替你起好了头。

**README 公开缺口。**

[README.md:5-9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L5-L9) —— 功能清单里 `[ ] Forward search following cursor` 是唯一未勾选项。但请注意「代码与文档的不一致」：`server.ts` 里其实**已经有**正向搜索的半成品（`onDocumentHighlight` → `synctex-forward`，见 u3-l2），只是它借壳 `documentHighlightProvider`、并非标准的「跟随光标」语义，所以 README 仍标为未完成。这是一个典型的「文档落后于代码」现象，二次开发时要两边对照看。

#### 4.4.4 代码实践

**实践目标**：把本模块的「债务清单」里两条最值得做的未来工作，各设计出一个可落地的实现思路。这是规格里点名的第二部分实践。

**操作步骤**（设计型实践，无需运行）：

1. **「Forward search following cursor」的实现思路**。先读 [src/server.ts:229-268](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L229-L268) 的现有半成品，回答：
   - 它为什么「不算正式的正向搜索」？（提示：触发器是 `documentHighlight`，而非真正的光标移动通知；且只有当编辑器主动请求高亮时才触发。）
   - 要做成「真正的跟随光标」，缺的是什么？LSP 标准里有没有「光标移动」通知？（提示：标准 LSP 没有光标移动通知，这也是作者借壳的原因。可能的实现方向：依赖编辑器扩展主动发通知，或轮询，或继续强化 `documentHighlight` 这条借壳链路。）

2. **把 `append-lines` 接成 diagnostics 的实现思路**（承接 4.2.4 的进阶项）。设计需要补的代码：
   - 在 `types.ts` 借用 [src/types.ts:4-7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L4-L7) 的 `CustomDiagnostic`。
   - 在 [src/server.ts:112-116](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L112-L116) 的监听器里，把 `data` 转成 `{ uri, diagnostics: CustomDiagnostic[] }`，调用 `connection.sendDiagnostics(...)`。
   - 列出你**还不知道**的信息（如 `data` 的确切结构、`append-lines` 是否携带文件路径与行号），并说明这些未知如何阻塞你的设计——这正是「先确认格式再写逻辑」的工程纪律。

3. **整理你自己的「债务优先级表」**：把本模块清单里的所有项按「影响正确性 / 影响性能 / 仅是未完成功能」分类，并给出你会先修哪一项的理由。

**需要观察的现象 / 预期结果**：

- 步骤 1 应得出：现有正向搜索是「借壳半成品」，要做成真正跟随光标，瓶颈在 LSP 协议本身没有光标移动通知，需要编辑器侧配合。
- 步骤 2 应得出：`append-lines` → diagnostics 的改造在协议层零负担（只需改 `server.ts` 一处监听器 + 复用一个 `types.ts` 类型），但**被 `data` 格式未知这一外部依赖卡住**，必须先观测真实输出。

#### 4.4.5 小练习与答案

**练习 1**：`types.ts` 里的 `CustomDiagnostic` 等五个类型从未被 `server.ts` 引用，算不算「死代码」？应该删掉吗？

**参考答案**：它们不是「死代码」而是「脚手架/路标」。死代码是指曾经有用、现已无用的代码；而这五个类型是**为尚未实现的功能预先定义的接口**，作者保留它们是在表达「我打算做自定义诊断/文档分析」。删不删取决于项目取向：若坚持 YAGNI（You Aren't Gonna Need It）原则、确定不会做，可删以减负；若把它当作实验性项目、保留未来可能性，则留着合理。从 README 的 `experimental` 定位看，留着更符合作者意图。

**练习 2**：[src/server.ts:193](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L193) 的 TODO 建议用 `.aux` 文件的 sha 判断是否 rescan。这个优化为什么对 LaTeX 工作流特别有意义？

**参考答案**：因为 LaTeX 编译会生成 `.aux` 等辅助文件，而很多保存（如只改了注释、纯文本）并不会改变 `.aux` 的内容。若每次保存都无条件 `rescan`，预览会做很多无谓刷新。比对 `.aux` 的哈希，只在内容真正变化时才 rescan，能显著减少预览抖动。这是针对 LaTeX 多遍编译特性的专门优化，作者用 TODO 标注说明他清楚价值、只是还没做。

**练习 3**：本模块把 [src/server.ts:206](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L206) 的 `// can this be out of sync?` 归为「正确性方向」的技术债，而把两条 FUTURE/TODO 归为「性能方向」。为什么正确性方向的债更值得优先处理？

**参考答案**：性能债只会让系统「慢」或「费资源」，功能仍然正确；正确性债会让系统「出错」（如文档同步错位会导致预览与源码不一致）。错误通常比慢更不可接受，且正确性问题往往在并发、竞态等难以复现的条件下才暴露，风险更高。所以工程上一般优先修正确性债，再优化性能债。这条注释对应的隐患在 u2-l4 已有讨论。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**「端到端接入一个新命令对」**的演练。它同时检验你对薄封装哲学（4.1）、扩展点（4.2）、配置热更新（4.3）、技术债识别（4.4）的综合掌握。

**场景**：假设 `texpresso` 即将支持一条新事件 `word-count`（子进程在编译后把文档的「字数统计」通过 stdout 发出来），以及一条新命令 `refresh-preview`（让服务器主动要求子进程刷新预览）。你的任务是：

1. **画出责任边界**（承接 4.1）：在这两个新能力里，`texpresso-lsp` 分别只负责什么、不负责什么？（例如：字数是 `texpresso` 算出来的，`texpresso-lsp` 只负责接收并展示。）
2. **写出两端改动清单**（承接 4.2）：对 `word-count`（收方向）与 `refresh-preview`（发方向），分别指出：
   - `process-manager.ts` 要不要改？（预期：都不要）
   - `server.ts` 要加哪几行？（给出监听器 / `sendCommand` 调用的骨架代码，标注「示例代码」）
   - 其中哪一条命令只有一端、不需要配对？
3. **决定配置归属**（承接 4.3）：如果你想新增一个开关 `enable_word_count`，让它能运行期热更新，应该放进 `ServerConfig` 还是 `WorkspaceSettings`？需要在 `onInitialized` / `onDidChangeConfiguration` / 消费方分别加什么？参考 [src/server.ts:134-167](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L134-L167) 的现成模式。
4. **识别会撞上的技术债**（承接 4.4）：你的新监听器里如果调 `connection.sendDiagnostics` 或 `console.log`，会复用 `types.ts` 的哪个脚手架类型？如果 `word-count` 的 `data` 格式未知，你会卡在哪一步（呼应 4.2.4 与 4.4.4 的工程纪律）？

**完成标准**：你的答卷应当能让人一眼看出——

- 4.1 的薄封装让 `process-manager.ts` 在整个任务里**零改动**；
- 4.2 的「收改监听、发改调用」清单被精确填出，且你能正确指出哪条命令不需要配对端；
- 4.3 的热更新三件套（拉初值 / 监听变更 / 现读现用）被你正确套用到新开关上；
- 4.4 让你提前意识到「`data` 格式未知」这个外部依赖会阻塞实现，并把脚手架类型复用进去。

> 这四步合起来，就是把「读懂源码」转化为「改造源码」的最小可行路径——也是本学习手册的最终目标。

## 6. 本讲小结

- **薄封装是本项目的宪法**：README 明确写了「experimental / cheap nodeJS / thin interface / JSON 适合 JS」四句箴言（L11-17）。本项目只负责协议翻译与编排，绝不碰渲染、编译、SyncTeX 计算——这些全外包给 `texpresso` 子进程。能力清单只有两项（L118-123），是「薄」的最直观体现。
- **薄封装的数学红利**：传输层对命令名「免税」，接入 N 条命令的传输层成本为 \( 0 \cdot N \)，只剩每条命令的业务成本。代码依据是 `emit`（L41-44）与 `sendCommand`（L103-110）都不认识任何具体命令名。
- **新增命令的扩展点是两个独立端点**：收方向加 `texpressoProcess.on(...)`、发方向加 `sendCommand(...)`，`process-manager.ts` 永不动；两端彼此独立，可只收不发（如 `synctex`/`append-lines`）或只发不收（如 `rescan`）。
- **运行期热更新靠「单一真相 + 现读现用」**：`onInitialized` 拉初值、`onDidChangeConfiguration` 监听变更、`onDocumentHighlight` 现读 `connection.workspace_config`。只有 `WorkspaceSettings` 能热更新；`ServerConfig`（如 `inverse_search`）握手后即冻结，改它必须重启。
- **技术债分四类**：显式 `FUTURE`/`TODO` 注释（L187-188、L193、L206）、`types.ts` 五个未用脚手架类型、README 未勾选的正向搜索、以及散见各讲的隐性隐患。其中 L206 的文档同步竞态是正确性方向、最该优先警惕。
- **文档与代码可能不一致**：README 标正向搜索未实现，但 `server.ts` 已有 `documentHighlight`→`synctex-forward` 半成品。二次开发时务必两边对照，不能只信 README。

## 7. 下一步学习建议

本讲是整部学习手册的最后一篇。到这里，你已经从「项目是什么」一路读到「如何改造它」。建议接下来从以下几个方向继续深耕，不再有「下一讲」，而是进入自主探索阶段：

- **横向通读一遍源码**：带着本讲建立的「责任边界图」重读 [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) 全文与 [src/process-manager.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts) 全文。此时你应当能把每一个回调、每一个 `emit`/`sendCommand`、每一处 `try/catch` 都对应到某一篇讲义——如果还有说不清的，回去重读那一篇。
- **动手做一件最小改造**：从本讲 4.2.4 的 `append-lines` 实践入手，先把它从「只打日志」改成「逐行可读输出」，跑通一次「改一处 → 见效果」的闭环；再尝试 4.4.4 的 diagnostics 改造。这是把「读懂」变成「会改」的捷径。
- **向上游 `texpresso` 看一眼**：本项目的所有命令名（`open`/`change-range`/`rescan`/`synctex`/`synctex-forward`/`append-lines`）都来自 `texpresso` 子进程的协议。去阅读 [let-def/texpresso](https://github.com/let-def/texpresso) 与本项目 README 提到的 [let-def/texpresso#36](https://github.com/let-def/texpresso/issues/36) issue，理解「薄壳的另一侧」是怎么定义这些命令的，你会对本项目的协议层有更立体的认识。
- **写一个编辑器扩展**：本项目只提供 LSP 服务端；要让用户真正用起来，还需要编辑器侧的客户端扩展（如 README 提到的 [lnay/zed-texpresso](https://github.com/lnay/zed-texpresso)）。试着为你常用的编辑器（VS Code / Neovim 等）写一个最小 LSP 客户端，连接 `texpresso-lsp --stdio`——这会补全你对「三层架构」最外侧那一层的理解。
- **关注版本演进**：本项目仍在迭代（最近一次发布是 v1.3.0，HEAD `c13ec89`）。后续若 `texpresso` 协议或 LSP 能力有变，可对照本讲的「扩展点」与「技术债」两节判断影响面——这正是本手册设计成可增量更新（`keep`/`update`/`rebuild`）的意义。
