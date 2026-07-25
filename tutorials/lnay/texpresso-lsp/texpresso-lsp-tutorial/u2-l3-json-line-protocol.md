# JSON 行协议与命令分发

## 1. 本讲目标

上一讲（u2-l2）我们看清了 `TexpressoProcessManager` 如何用 `child_process.spawn` 拉起 `texpresso` 子进程，以及 `start()`/`stop()` 的生命周期。但子进程起来之后，LSP 服务器和它**究竟怎么对话**，我们故意留了一截没讲。本讲就补上这一截。

读完本讲，你应该能够：

- 解释为什么读 `stdout` 不能「来一次 `data` 事件就当作一条完整消息」，理解**半包/粘包**问题以及 `stdout_buffer` 行缓冲为何能解决它。
- 说出 texpresso-lsp 与 texpresso 之间自定义协议的**信封格式**：一行一个 JSON 数组 `[command, ...data]`（即 NDJSON），并理解编码与解码两端的对称写法。
- 看懂 `emit(command, data)` 如何把解析出的命令分发给 `server.ts` 里的各个 `.on(...)` 监听器，以及 `sendCommand` 如何把命令写回子进程的 `stdin`。

本讲是后续「文档同步（u2-l4）」「实时预览（u3-l1）」「SyncTeX 搜索（u3-l2）」的共同底座——这些功能本质上都是在「收一条事件」或「发一条命令」。

## 2. 前置知识

在进入源码前，先用大白话建立三个心智模型。

**① 流（Stream）是「一截一截」来的，不是「一条一条」来的。**
Node 里的 `stdout`/`stdin` 是字节流。你读它时，它不会体贴地按「一条消息」为单位递交，而是按「操作系统刚好凑齐的一块字节」（一个 `Buffer`）递交。所以一次 `data` 事件里：

- 可能只有**半条**消息（半包）——下一条消息还得等下一个 `data` 事件才到齐；
- 可能挤着**好几条**消息（粘包）；
- 也可能「半条旧的 + 一条完整的 + 半条新的」混在一起。

直接对每个 `data` 事件做 `JSON.parse` 是初学者最常踩的坑。正确做法是**先按分隔符切成完整的行，再逐行解析**。

**② 换行符 `\n` 是双方约定的「消息边界」。**
既然流没有边界，那就人为加一个：每条消息末尾加一个 `\n`。于是「一段字节流」就被切成了「一行一行的文本」。这种「每行一个 JSON」的格式叫 **NDJSON**（Newline-Delimited JSON，也叫 JSONL）。texpresso-lsp 和 texpresso 之间用的就是它。

**③ EventEmitter 是「发布-订阅」。**
`EventEmitter`（Node 内置）是一个小邮局：`emit("synctex", data)` 是「往 `synctex` 信箱投一封信」，`on("synctex", fn)` 是「订阅 `synctex` 信箱，来一封就调 `fn`」。`TexpressoProcessManager extends EventEmitter`，所以它可以 `emit`，`server.ts` 可以 `on`。本讲的「事件分发」就是靠这个机制。

> 名词速查：**半包**指一条消息被切在两次读取里；**粘包**指多条消息粘在一次读取里。这俩是流式编程的经典问题，和 TCP 的「粘包」是同一类思路。

## 3. 本讲源码地图

本讲只涉及两个文件，但各看其中一部分：

| 文件 | 本讲关注的内容 | 行号范围 |
| --- | --- | --- |
| `src/process-manager.ts` | 协议的**收**（stdout 行缓冲 + JSON 解析 + emit）和**发**（sendCommand 写 stdin） | L7、L27-L46、L48-L62、L103-L110 |
| `src/server.ts` | 协议的**消费端**：5 个 `.on(...)` 监听器；以及 5 处 `sendCommand(...)` 调用 | L74-L116、L176、L194、L201、L222、L250-L253 |

一句话概括：`process-manager.ts` 负责把字节流翻译成事件、把命令翻译成字节流；`server.ts` 负责决定「收到什么事件做什么事」和「什么时候发什么命令」。

## 4. 核心概念与源码讲解

### 4.1 行缓冲：解决流式数据的半包与粘包

#### 4.1.1 概念说明

`stdout` 的 `data` 事件按「字节块」递交，不按「消息」递交。如果直接 `JSON.parse(data)`，一旦一条 JSON 被切成两块，第二次 `parse` 就会抛错；而把多条 JSON 粘在一块时，又没法一次 `parse` 出多条。

解决办法是维护一个**缓冲区字符串** `stdout_buffer`：

1. 每来一块 `data`，先把它**追加**到缓冲区末尾；
2. 用 `\n` 把缓冲区**切成行**；
3. **最后一行可能是不完整的半行**（因为它后面还没有 `\n`），把它**留在缓冲区**里等下次拼；
4. 前面那些**完整行**才送去做后续解析。

这样一来，无论流怎么乱切，只要消息以 `\n` 结尾，最终都能被正确还原成一行一行的完整 JSON。

#### 4.1.2 核心流程

下面用一个具体的字节流追踪，看缓冲区如何演化。假设 texpresso 要发两条消息：

- 消息 A：`["synctex","/a.tex",5]`
- 消息 B：`["append-lines",["foo"]]`

线缆上的 NDJSON 是：

```
["synctex","/a.tex",5]\n["append-lines",["foo"]]\n
```

但操作系统把它们拆成了两次 `data` 事件，且第一块恰好把消息 B 切断：

| 时刻 | 到达的 chunk | 追加后 buffer | `split("\n")` | `pop()` 留存 | 送出处理的完整行 |
| --- | --- | --- | --- | --- | --- |
| T1 | `["synctex","/a.tex",5]\n["append-li` | 同左 | `['["synctex","/a.tex",5]', '["append-li']` | `'["append-li'` | `["synctex","/a.tex",5]` |
| T2 | `nes",["foo"]]\n` | `["append-lines",["foo"]]` | `['["append-lines",["foo"]]', '']` | `''` | `["append-lines",["foo"]]` |

关键看 T1：消息 B 的前半截 `["append-li` 被 `pop()` 留在缓冲区，**没有**被拿去解析；到 T2 它和后续字节拼成完整消息后才被处理。这就是「半包」被正确处理的证据。

注意 T2 末尾：因为 chunk 以 `\n` 结尾，`split` 会多出一个空串 `''`，`pop()` 恰好把这个 `''` 留作缓冲区，等于把缓冲区**清空**——这是个很巧妙的一致性。

#### 4.1.3 源码精读

缓冲区字段声明见 [src/process-manager.ts:7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L7)，一个初始为空串的私有字符串。

真正的行缓冲逻辑在 `stdout` 的 `data` 回调里，见 [src/process-manager.ts:27-46](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L27-L46)。我们先把「行缓冲」这三行单独拎出来：

```ts
this.process.stdout?.on("data", (data: Buffer) => {
    this.stdout_buffer += data;                         // ① 追加到缓冲区
    const lines = this.stdout_buffer.split("\n");       // ② 按换行切行
    this.stdout_buffer = lines.pop()?? '';              // ③ 末行（可能半行）留存
    // ... 接下来对 lines 里剩下的完整行做解析
```

逐行解释：

- **① `this.stdout_buffer += data`**：`data` 是 `Buffer`，用 `+=` 拼到字符串上会自动按 UTF-8 解码成字符串。这一步把「新到的字节」接在「上次没处理完的尾巴」后面。
- **② `split("\n")`**：以换行符为刀，把缓冲区切成数组。注意切出来的最后一个元素，**可能**是不完整的半行（如果缓冲区不以 `\n` 结尾）。
- **③ `lines.pop() ?? ''`**：`pop()` 取出并返回数组最后一个元素，同时把它从数组里移除。这个被取出的「末尾元素」回存到 `stdout_buffer`，留给下一个 `data` 事件拼接；而 `lines` 里剩下的，就都是「曾经以 `\n` 结尾的完整行」了。

`pop()` 为什么是对的？因为它取走的恰好是「**最后一段没有被 `\n` 收尾的文本**」——也就是最可能不完整的那一段。前面的行都曾经被 `\n` 分隔过，是完整的。

关于 `?? ''`：`String.prototype.split` 永远返回长度 ≥ 1 的数组，所以 `pop()` 在这里实际上**永远不会**返回 `undefined`，`?? ''` 是一段「防御性死代码」。它的意义在于满足 TypeScript 的类型检查（`pop()` 的返回类型是 `string | undefined`），也顺手兜底了「理论上 buffer 为空」的极端情形。这和上一讲 u2-l1 讲过的 `??`（空值合并）用法一脉相承：**只在 `null`/`undefined` 时回落**，空串 `''` 不会被替换掉。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「半包」被缓冲区正确拼接，而不是被当成坏消息丢掉。

**操作步骤**：

1. 新建一个目录，写入下面两个文件（**示例代码**，非项目源码）。

`ndjson-child.js`（扮演 texpresso 子进程）：

```js
// ndjson-child.js —— 示例代码：从 stdin 读 NDJSON，逐行解析后回写 NDJSON
let buf = '';
process.stdin.on('data', (chunk) => {
  buf += chunk.toString();
  const lines = buf.split('\n');
  buf = lines.pop() ?? '';               // 末尾可能是不完整的半行，留到下次
  for (const line of lines) {
    if (line === '') continue;           // 跳过空行
    try {
      const [cmd, ...data] = JSON.parse(line);
      process.stdout.write(JSON.stringify(['echo', cmd, ...data]) + '\n');
    } catch (e) {
      process.stdout.write(JSON.stringify(['parse-fail', line]) + '\n');
    }
  }
});
```

`ndjson-parent.js`（扮演 LSP 服务器）：

```js
// ndjson-parent.js —— 示例代码：spawn 子进程，缓冲 + 解析 + 事件分发
const { spawn } = require('child_process');
const { EventEmitter } = require('events');

const child = spawn('node', ['ndjson-child.js']);
const bus = new EventEmitter();

let buf = '';
child.stdout.on('data', (chunk) => {       // 与 process-manager.ts 的 stdout 回调同构
  buf += chunk.toString();
  const lines = buf.split('\n');
  buf = lines.pop() ?? '';
  for (const line of lines) {
    if (line === '') continue;
    try {
      const [cmd, ...data] = JSON.parse(line);
      bus.emit(cmd, data);                 // emit(command, data)
    } catch (e) {
      bus.emit('parse-fail', [line]);
    }
  }
});

bus.on('echo', (d) => console.log('收到 echo:', JSON.stringify(d)));
bus.on('parse-fail', (d) => console.log('parse-fail:', JSON.stringify(d)));

function sendCommand(command, data) {       // 与 process-manager.ts 的 sendCommand 同构
  child.stdin.write(JSON.stringify([command, ...data]) + '\n');
}

// ① 正常一条消息
sendCommand('open', ['/a.tex', 'hello']);

// ② 半包：故意把同一条消息切成两段，隔 100ms 发
const msg = JSON.stringify(['change-range', '/a.tex', 0, 0, 0, 5, 'world']);
child.stdin.write(msg.slice(0, 10));                       // 先发前 10 个字符（无换行）
setTimeout(() => child.stdin.write(msg.slice(10) + '\n'), 100); // 100ms 后补全 + 换行

// ③ 非法 JSON，验证容错
setTimeout(() => child.stdin.write('not-json\n'), 200);
```

2. 运行：`node ndjson-parent.js`。

**需要观察的现象**：

- 第 ② 步那条 `change-range` 被切成两段发送，但子进程**没有**报 `parse-fail`，最终被完整还原——这正是 `pop()` 留存半行、等待拼接的功劳。
- 控制台**不会**在「先发前 10 个字符」那一刻打印任何 echo，要等 100ms 后半行补齐才打印一次。

**预期结果（待本地验证）**：

```
收到 echo: ["open","/a.tex","hello"]
收到 echo: ["change-range","/a.tex",0,0,0,5,"world"]
parse-fail: ["not-json"]
```

3.（可选思考）把子进程里的 `buf = lines.pop() ?? '';` 临时改成 `buf = '';`（即**不**留存半行），重跑，观察第 ② 步是否变成两条 `parse-fail`，从而直观体会「没有行缓冲」会丢什么。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `this.stdout_buffer = lines.pop() ?? '';` 改成 `lines.shift()`，会发生什么？

**答案**：`shift()` 取的是**数组第一个**元素。这会把「最早到达、早已完整的行」当成「待拼半行」留存，导致：(a) 完整行被推迟或永远不处理；(b) 真正的半行（在数组末尾）反而被当成完整行拿去解析而失败。逻辑彻底错乱。`pop()` 之所以正确，是因为「待拼的半行」一定出现在流的最末尾。

**练习 2**：缓冲区用字符串 `stdout_buffer` 拼接，在多字节 UTF-8 字符上有什么潜在风险？

**答案**：`data += Buffer` 会立即按 UTF-8 解码。如果一个多字节字符（如中文）恰好被切成两块，第一块末尾会得到一个「残缺的替换字符」，且 `split` 时若残缺字符恰好挨着 `\n`（罕见），可能影响边界判断。对纯 ASCII 的 JSON 影响几乎为零，但这是字符串缓冲方案的固有局限；工业级实现会用 `StringDecoder` 或按字节边界处理。texpresso 的 JSON 输出以 ASCII 为主，故当前实现足够。

### 4.2 JSON 数组协议：编码与解码

#### 4.2.1 概念说明

行缓冲解决了「怎么切出完整一行」，接下来要解决「一行文本代表什么」。texpresso-lsp 和 texpresso 约定：**每一行是一个 JSON 数组，第 0 个元素是命令名（字符串），其余元素是这条命令携带的数据**。

也就是说，双方用的「信封」是这样的：

\[ \text{一行消息} = \texttt{JSON.stringify}([c,\ d_1,\ d_2,\ \ldots,\ d_n]) + \texttt{"}\backslash\texttt{n"} \]

解码时：

\[ c = a[0], \qquad D = a.\text{slice}(1) = [d_1, d_2, \ldots, d_n] \]

其中 \(c\) 是命令名字符串，\(D\) 是数据数组。这个约定在「发」和「收」两端**完全对称**——发方 `JSON.stringify([command, ...data])`，收方 `JSON.parse(line)` 后取 `[0]` 和 `slice(1)`。这种对称性让协议非常容易理解和维护。

为什么用数组而不是对象 `{command, data}`？这是一种**偏向紧凑**的设计取舍：省去了字段名（`"command"`、`"data"`）的字符开销，解析也只需下标和 `slice`；代价是可读性弱一些，且字段顺序是隐式契约。对于「两个进程之间的高频小消息」，数组方案是常见且合理的选择。

#### 4.2.2 核心流程

**收方向（stdout → 事件）**，对每一行完整文本：

```
完整行文本 line
   │
   ├─ 若 line === "" → 跳过（空行）
   │
   └─ JSON.parse(line)
         │
         ├─ 成功 → 得到数组 a，例如 ["synctex", "/a.tex", 5]
         │
         └─ 失败(catch) → 伪造一个 ["parse-fail", line]，假装它是「一条命令」
```

注意「失败」这一支很巧妙：它**没有**抛异常中断循环，而是把坏行包装成一条 `["parse-fail", line]` 消息，让它走和正常消息**同样的后续流程**。这样坏数据被「降级」成一条普通事件，而不是让整个服务器崩掉。

**发方向（命令 → stdin）**：见 4.3 节。

#### 4.2.3 源码精读

解析与容错这段在 [src/process-manager.ts:32-45](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L32-L45)，紧接在 4.1 讲过的行缓冲之后：

```ts
lines
    .filter((line) => line != "")          // ① 跳过空行
    .map((line) => {
        try {
            return JSON.parse(line);        // ② 正常解析
        } catch (e) {
            return ["parse-fail", line];    // ③ 容错：坏行包装成一条「命令」
        }
    })
    .forEach((command_list) => {
        const command = command_list[0];            // 取第 0 个 = 命令名
        const data = command_list.slice(1);         // 取其余 = 数据数组
        this.emit(command, data);                   // 交给事件分发（见 4.3）
    });
```

逐点解释：

- **① `.filter((line) => line != "")`**：跳过空行。空行哪来的？当缓冲区以 `\n` 结尾时，`split` 会产生末尾的空串（见 4.1.2 表格 T2）；流里也可能本就有连续两个 `\n`。空行必须过滤，否则 `JSON.parse("")` 会抛 `SyntaxError`——虽然 ③ 有 `catch` 兜底，但空行并非真错误，过滤掉能避免无意义的 `parse-fail` 噪音。
- **② `JSON.parse(line)`**：把一行文本还原成 JS 值（约定为数组）。
- **③ `["parse-fail", line]`**：解析失败时的容错产物。它**长得和正常消息一模一样**——也是个「第 0 位是命令名、其余是数据」的数组。因此它会被下面的 `forEach` 当成一条名为 `"parse-fail"` 的命令原封不动地 `emit` 出去。这是一种**用协议自身格式表达错误**的优雅做法。

再看发方向的编码，见 [src/process-manager.ts:108-109](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L108-L109)：

```ts
const message = JSON.stringify([command, ...data]);   // [command, ...data] → JSON 文本
this.process?.stdin?.write(message + "\n");           // 末尾补 \n 作为消息边界
```

`JSON.stringify([command, ...data])` 正是解码的逆运算：把命令名放第 0 位、数据展开在后面，序列化成数组文本；再 `+ "\n"` 补上 NDJSON 的边界。两端对称，一目了然。

#### 4.2.4 代码实践

**实践目标**：用 4.1.4 的示例程序，验证「空行」与「非法 JSON」两条边界。

**操作步骤**：

1. 复用 4.1.4 的两个文件。
2. 在 `ndjson-parent.js` 末尾再加几行测试（**示例代码**）：

```js
setTimeout(() => child.stdin.write('\n'), 250);                 // 只发一个换行（空行）
setTimeout(() => child.stdin.write('{"cmd":"obj"}\n'), 300);    // 合法 JSON 但不是数组
```

3. 重跑 `node ndjson-parent.js`。

**需要观察的现象**：

- 空行不会触发 `parse-fail`（被 `filter` 拦掉）。
- `{"cmd":"obj"}` 虽然是合法 JSON，但它**不是数组**，后面 `command_list[0]` 会取到 `"obj"`、`slice(1)` 得到 `[]`——它会被当成一条命令 `emit("obj", [])`，而父进程没有 `on("obj")`，于是**静默丢弃**（见 4.3 关于无监听者的说明）。这提醒我们：协议的正确性依赖于「双方都遵守数组约定」，解析层并不强制校验 `Array.isArray`。

**预期结果（待本地验证）**：空行无任何输出；`{"cmd":"obj"}` 也无任何输出（因为没有对应的 echo 回来，也没有 parse-fail）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接 `JSON.parse(data)`（对整个 `data` 事件解析），而要先做行缓冲？

**答案**：因为一次 `data` 事件可能包含半条消息（半包）或多条消息（粘包）。半条消息会让 `JSON.parse` 失败并丢失数据；多条消息无法用一次 `parse` 还原。行缓冲保证交给 `JSON.parse` 的始终是「以 `\n` 结尾的完整一行」，即恰好一条完整 JSON。

**练习 2**：`["parse-fail", line]` 这条「伪命令」最终去了哪里？有人处理它吗？

**答案**：它会被 `forEach` 里的 `this.emit("parse-fail", [line])` 发出去（`command_list[0]` 是 `"parse-fail"`，`slice(1)` 是 `[line]`）。但搜遍 `server.ts`，**没有任何 `texpressoProcess.on("parse-fail", ...)`**（可用 `grep parse-fail` 验证，全仓库只在 `process-manager.ts:38` 出现一次）。所以这条事件目前被**静默丢弃**——`EventEmitter` 在某事件无监听者时不会报错（除非是特殊的 `"error"` 事件）。这是一种「降级但不崩溃」的容错策略，缺点是坏数据没有显式上报。更深入的错误处理在第 u3-l3 讲讨论。

### 4.3 命令写入与事件分发：协议的两端

#### 4.3.1 概念说明

前两节讲清了「字节 ⇄ JSON 数组」的转换。这一节站在更高处看：**这些数组在两个方向上分别由谁产生、由谁消费**。

- **发方向（LSP → texpresso）**：`server.ts` 在各种事件回调（打开文档、改动、保存、光标高亮……）里调用 `texpressoProcess.sendCommand(cmd, data)`；`sendCommand` 把它编成 NDJSON 写进子进程的 `stdin`。
- **收方向（texpresso → LSP）**：`process-manager.ts` 解析 `stdout` 后 `emit(command, data)`；`server.ts` 用若干 `texpressoProcess.on(cmd, fn)` 订阅自己关心的命令，在回调里决定「做什么」。

所以 `process-manager.ts` 是一个**翻译官 + 邮局**：对字节流做翻译（收）、对命令做投递（emit）；而真正「业务逻辑」全在 `server.ts` 的监听器里。这正是「薄封装」思想的体现——`process-manager.ts` 对 LaTeX 一无所知。

#### 4.3.2 核心流程

```
       ┌────────────── 发方向 ──────────────┐
       │  server.ts 的某个回调               │
       │     ↓ texpressoProcess.sendCommand │
       │  process-manager: JSON.stringify   │
       │     + "\n" → stdin.write           │
       └─────────── 流向 texpresso ─────────┘

       ┌────────────── 收方向 ──────────────┐
       │  texpresso stdout                  │
       │     ↓ data 事件                    │
       │  process-manager: 行缓冲 → 解析    │
       │     ↓ this.emit(command, data)     │
       │  server.ts: .on(command, fn)       │
       │     ↓ 执行业务（记日志/spawn 编辑器）│
       └────────────────────────────────────┘
```

#### 4.3.3 源码精读

**先看发方向**——`sendCommand` 的全貌见 [src/process-manager.ts:103-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L103-L110)：

```ts
public async sendCommand(command: string, data: any[]) {
    if (!this.isRunning || !this.process?.stdin || !this.process?.stdout) {
        throw new Error("Process is not running or stdio not available");   // 守卫
    }
    const message = JSON.stringify([command, ...data]);   // 编码（4.2 已讲）
    this.process?.stdin?.write(message + "\n");            // 写入子进程 stdin
}
```

第一行的**守卫**很重要：只有进程在运行、且 `stdin`/`stdout` 都可用时才允许写。否则抛错。注意它只检查 `stdout` 是否存在却不读它——这里 `stdout` 的存在性只是「进程健康」的近似信号。这个守卫是「发命令」的安全带，更完整的错误与生命周期分析在第 u3-l3 讲。

**再看 `server.ts` 里所有调用 `sendCommand` 的地方**——这等于在问「LSP 服务器到底会主动给 texpresso 发哪些命令」：

| 调用处 | 命令 | data | 触发场景 |
| --- | --- | --- | --- |
| [src/server.ts:176](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L176) | `open` | `[path, text]` | 文档打开，把全文发给 texpresso |
| [src/server.ts:222](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L222) | `change-range` | `[path, 起行, 起列, 止行, 止列, 文本]` | 文档增量改动 |
| [src/server.ts:194](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L194) | `rescan` | `[]` | 保存并重编译后让 texpresso 重新扫描 |
| [src/server.ts:201](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L201) | `close` | `[path]` | 文档关闭 |
| [src/server.ts:250-253](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L250-L253) | `synctex-forward` | `[filePath, lineNumber]` | 光标高亮时触发正向 SyncTeX |

**最后看收方向**——`process-manager.ts` 里所有 `emit` 的源头，以及 `server.ts` 里所有 `.on(...)` 监听器。

`emit` 一共有两类来源（均在 `process-manager.ts`）：

1. **动态命令**（来自解析出的 JSON 数组首元素），见 [src/process-manager.ts:41-45](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L41-L45)：`this.emit(command, data)`。`command` 是什么名字，完全取决于 texpresso 发了什么——比如 `"synctex"`、`"append-lines"`，或坏数据生成的 `"parse-fail"`。
2. **固定的进程生命周期事件**，由底层 `ChildProcess` 转发而来：

   - [src/process-manager.ts:48-50](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L48-L50)：`emit("stderr", ...)`，转发子进程的标准错误输出。
   - [src/process-manager.ts:52-54](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L52-L54)：`emit("error", ...)`，转发子进程的 `error` 事件（如启动失败）。
   - [src/process-manager.ts:56-62](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L56-L62)：`emit("exit", {code, signal})`，转发子进程退出。

`server.ts` 里的 5 个监听器，正好把上面这些事件「接住」并转成 LSP 日志或业务动作：

| 监听器 | 监听的事件 | 来源 | 做什么 |
| --- | --- | --- | --- |
| [src/server.ts:74-78](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L74-L78) | `error` | 进程级（固定） | `connection.console.error` 记日志 |
| [src/server.ts:79-81](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L79-L81) | `stderr` | 进程级（固定） | 把子进程 stderr 写进 LSP 日志 |
| [src/server.ts:83-93](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L83-L93) | `exit` | 进程级（固定） | 记录退出码与信号 |
| [src/server.ts:95-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L95-L110) | `synctex` | 动态命令 | 反向搜索：替换 `%f`/`%l` 后 `spawn` 编辑器命令 |
| [src/server.ts:112-116](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L112-L116) | `append-lines` | 动态命令 | 目前**仅记日志**（后续可扩展） |

把两张表对起来看，就能讲清「协议的完整双向图景」：`server.ts` 发 `open`/`change-range`/`rescan`/`close`/`synctex-forward` 这 5 条命令；`server.ts` 听 `error`/`stderr`/`exit`/`synctex`/`append-lines` 这 5 个事件。发什么、听什么，一目了然。

> 关于「无监听者」：动态 `emit` 出去的事件若没人 `on`，`EventEmitter` 默认什么都不做（静默）。唯一的例外是名为 `"error"` 的事件——若无监听者，它会**抛出并可能崩溃进程**。所以 `server.ts:74` 的 `on("error", ...)` 不仅是为了记日志，也是为了**防止 `error` 事件无人处理而导致崩溃**。这点在第 u3-l3 讲会更系统地展开。

#### 4.3.4 代码实践

**实践目标**：亲手实现一个最小的「命令写入 + 事件分发」回环，把 4.1.4 的示例改造成能 `sendCommand` 并 `on(...)` 收事件的形式。

**操作步骤**：

1. 复用 4.1.4 的两个文件。它们已经具备 `sendCommand` 和 `bus.emit/bus.on`，本步骤只是把它们**当成协议的两端来观察**。
2. 在 `ndjson-parent.js` 里，把监听器扩展成「按命令名分类」（**示例代码**）：

```js
bus.on('echo', (d) => console.log('[echo]    ', JSON.stringify(d)));
bus.on('parse-fail', (d) => console.log('[parse-fail]', JSON.stringify(d)));
// 再加一个「故意没人监听」的命令，观察静默现象：
sendCommand('nobody-listens', ['hi']);
```

3. 运行 `node ndjson-parent.js`。

**需要观察的现象**：

- `nobody-listens` 这条命令被子进程原样 `echo` 回来，父进程 `bus.emit('echo', ...)` 触发 `[echo]` 打印——注意：子进程把任何收到的命令都包成 `['echo', 原命令, ...原data]` 回送，所以「没人监听」的是**原命令**，而 `echo` 是有人监听的。
- 如果想让父进程真正「静默丢弃」，需要让子进程**不**回 echo、而是直接转发原命令名。可把子进程的回写行改成 `process.stdout.write(JSON.stringify([cmd, ...data]) + '\n');`，重跑后观察 `[nobody-listens]` 是否真的没有任何输出（因为父进程没有 `on('nobody-listens')`）。

**预期结果（待本地验证）**：改造后，`nobody-listens` 那条**不会**打印任何内容；而 `open`、`change-range` 依然能在 `echo` 信箱里看到回声。

#### 4.3.5 小练习与答案

**练习 1**：5 处 `sendCommand` 调用里，哪一条的 `data` 是空数组？为什么？

**答案**：[src/server.ts:194](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L194) 的 `sendCommand("rescan", [])`。`rescan` 只是一个「信号型」命令——告诉 texpresso「去重新扫描」，不需要携带额外数据，所以 `data` 为空数组，线缆上的内容是 `["rescan"]\n`。

**练习 2**：如果 texpresso 突然发来一条全新的命令 `"new-feature"`，而 `server.ts` 没有对应监听器，会发生什么？需要改 `process-manager.ts` 吗？

**答案**：`process-manager.ts` **完全不用改**——它是「协议层」，对任何命令名都一视同仁地 `emit(command, data)`。`"new-feature"` 会被原样投递，只因 `server.ts` 没有 `on("new-feature", ...)` 而被静默丢弃。要让新命令生效，只需在 `server.ts` 里新增一个 `texpressoProcess.on("new-feature", fn)`。这正是「薄封装」的好处：扩展业务时，协议层零改动。（扩展点的完整讨论见第 u3-l4 讲。）

**练习 3**：`emit("error", error)` 为什么必须有对应的 `on("error", ...)`？

**答案**：在 Node 的 `EventEmitter` 里，`"error"` 是特殊事件名——如果它被 `emit` 时没有任何监听者，默认会**重新抛出**该错误，可能导致进程崩溃。`server.ts:74` 的 `on("error", ...)` 既记录日志，又「吃掉」了这个错误，避免崩溃。其他名字的事件（如 `synctex`、`parse-fail`）无此待遇，无人监听就静默结束。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「协议侦探」小任务。

**任务**：在不运行真实 `texpresso` 的前提下，仅凭阅读源码 + 4.1.4 的 NDJSON 示例，回答并验证以下问题。

1. **画双向时序图**：在一张图上画出「保存文档触发预览刷新」涉及的协议往返。提示：`onDidSave`（[src/server.ts:179-196](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L179-L196)）里 spawn 了 `texpresso-tonic` 编译，编译结束后才 `sendCommand("rescan", [])`。要求标清：哪一步走 `stdin`（发命令）、哪一步可能走 `stdout`（收事件）、消息长什么样（写出 `["rescan"]\n` 这样的线缆文本）。

2. **补全事件清单**：用 `grep -n "texpressoProcess.on" src/server.ts` 列出全部监听器，逐个写出「事件名 / 来源（进程级 or 动态命令）/ 处理动作」三列表格，与 4.3.3 的表对照，确认没有遗漏。

3. **动手验证 `pop()`**：用 4.1.4 的示例，把 `msg.slice(0, 10)` 的切点从 10 改成 1（即每次只发 1 个字符），观察是否依然能正确还原（这能强化「无论流怎么切，行缓冲都能还原」的直觉）。记录你看到的输出（待本地验证）。

4. **思考题**：当前 `sendCommand` 把命令**同步**写进 `stdin`，但没有等待 texpresso 的应答。如果短时间内连续发送 100 条 `change-range`，会出现什么？线缆上的字节顺序由谁保证？（提示：`stdin.write` 是按调用顺序写入同一个流的；texpresso 那侧的读取顺序则由它自己的实现决定。）

## 6. 本讲小结

- **行缓冲**：`stdout` 是「按字节块」递交的流，存在半包/粘包。`stdout_buffer` 配合 `split("\n")` 与 `lines.pop()`，把「末尾可能不完整的半行」留存到下次拼接，保证交给解析器的永远是完整一行。
- **`lines.pop() ?? ''`**：`pop()` 取走最末尾的半行；`?? ''` 是防御性写法（`split` 实际不会返回空数组），同时满足 TypeScript 类型检查。
- **JSON 数组协议（NDJSON）**：每行一个 JSON 数组 `[command, ...data]`，第 0 位是命令名、其余是数据。发方 `JSON.stringify([command, ...data]) + "\n"`，收方 `JSON.parse` 后取 `[0]` 与 `slice(1)`，两端对称。
- **容错策略**：空行被 `filter` 过滤；`JSON.parse` 失败时不崩溃，而是伪造一条 `["parse-fail", line]` 走相同流程，最终被 `emit("parse-fail", ...)`——但 `server.ts` 目前**没有**监听它，故静默丢弃。
- **事件分发**：`process-manager.ts` 用 `emit(command, data)` 投递「动态命令」，外加固定转发 `error`/`stderr`/`exit` 三个进程级事件；`server.ts` 用 5 个 `.on(...)` 接住并把它们变成日志或业务动作。
- **薄封装**：协议层对命令名一无所知、一视同仁；新增一条 texpresso 命令时，`process-manager.ts` 零改动，只需在 `server.ts` 加监听器或加 `sendCommand` 调用。

## 7. 下一步学习建议

本讲把「字节 ⇄ JSON ⇄ 事件/命令」这条管道彻底打通。接下来：

- **第 u2-l4「文档同步机制」**：会大量使用本讲的 `sendCommand`——`onDidOpen` 发 `open`、`onDidChangeTextDocument` 发 `change-range`、`onDidClose` 发 `close`。建议带着「这些命令的 data 数组每一项对应 LSP change 的哪个字段」的问题去读。
- **第 u3-l1「实时预览与编译流程」**：会用到 `rescan` 命令和 `append-lines` 事件，正好是本讲表格里「发一条 / 听一条」的实例。
- **第 u3-l3「错误处理与进程生命周期」**：会更系统地讨论 `error`/`stderr`/`exit` 三个进程级事件的转发与 `sendCommand` 守卫、`parse-fail` 的去向，是本讲 4.2/4.3 的延伸。
- **动手预习**：在进入 u2-l4 前，建议先在 `server.ts` 里用 `grep -n sendCommand` 把 5 处调用和本讲的表格对一遍，确认你能脱口说出每处的命令名和 data 结构。
