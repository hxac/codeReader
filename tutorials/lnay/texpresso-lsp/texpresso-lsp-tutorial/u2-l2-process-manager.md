# 进程管理器 TexpressoProcessManager

## 1. 本讲目标

上一讲（u2-l1）我们理清了配置如何流入服务器：初始化选项里有一项 `texpresso_path`，它是 `texpresso` 可执行文件的路径。但「拿到路径」和「真的把程序跑起来、还能跟它双向通信」之间还有很大距离。本讲就把这段距离补上。

读完本讲，你应当能够：

- 说清楚 `child_process.spawn` 的作用、参数，以及为什么它是本项目唯一合适的子进程启动方式。
- 读懂 `TexpressoProcessManager` 类的字段、构造函数，特别是 `args.push(this.rootTex)` 这一行在干什么。
- 描述 `start()` 与 `stop()` 的完整生命周期，以及 `isRunning` 这个布尔标志如何充当「单一状态真相」。
- 理解 `start()` 里那段「`Promise` + `setTimeout`」代码在等什么、为什么必须等、超时了会怎样。

本讲只聚焦「进程的创建、生命周期、状态」，**不**展开 stdout 的逐行缓冲与 JSON 解析——那是下一讲 u2-l3（JSON 行协议）的主题。本讲在涉及 stdout 处理器时只会点到为止。

## 2. 前置知识

### 2.1 什么是子进程

你平时在终端敲 `texpresso main.tex`，shell 会启动一个新的程序，这个新程序就是你这次命令的「子进程」。在 Node.js 里，我们不需要 shell，可以直接用代码启动子进程。`texpresso-lsp` 这个 LSP 服务器自己是一个 Node 进程，它要在运行期间拉起一个 `texpresso` 程序作为自己的子进程，两者再通过标准输入/输出（stdin/stdout）互相通信。

### 2.2 Node 的 child_process 模块

Node 内置的 `child_process` 模块提供几种启动子进程的 API，初学者最容易混淆：

| API | 输出方式 | 返回时机 | 适合场景 |
| --- | --- | --- | --- |
| `exec` / `execFile` | 把全部输出**缓冲**到一个字符串 | 子进程退出后回调 | 跑一次性命令，比如 `git log` |
| `spawn` | 以**流**的方式持续吐出 stdout/stderr | 立刻返回 `ChildProcess` 对象 | 长期运行、需要持续收发数据的程序 |
| `fork` | 同 spawn | 同 spawn | 专门启动另一个 Node 子进程 |

`texpresso` 是一个长期运行、需要服务器不停往它的 stdin 写命令、不停从它的 stdout 读事件的程序。因此 `spawn`（流式）是唯一合适的，缓冲式的 `exec` 根本无法胜任。

### 2.3 EventEmitter 与「包装器」模式

Node 的 `EventEmitter` 是一个内置基类，任何继承它的对象都能做两件事：

- `this.emit(事件名, 数据)` —— 主动「发射」一个事件；
- `obj.on(事件名, 回调)` —— 让别人「监听」这个事件。

`texpresso-lsp` 的设计是：`TexpressoProcessManager` 继承 `EventEmitter`，它把底层子进程的 `error`、`exit`、`stderr` 以及解析出来的命令事件，用 `this.emit(...)` 重新发出去；而 `server.ts` 用 `texpressoProcess.on(...)` 来接收。这就是经典的**包装器（wrapper / adapter）模式**——把「一个难用的东西」（裸 `ChildProcess`）包装成「一个好用的、会主动报事件的东西」。

### 2.4 TypeScript 的「参数属性」简写

普通面向对象语言里，要先声明字段、再在构造函数里赋值。TypeScript 提供一个简写：在构造函数参数前加 `private`（或 `public`），编译器会**自动**帮你声明同名字段并赋值。本讲的构造函数就用到了这个简写，看到 `private executablePath: string` 时要知道它等价于「声明字段 + 赋值」两步。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 角色 |
| --- | --- |
| [`src/process-manager.ts`](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts) | 定义 `TexpressoProcessManager` 类，封装 `texpresso` 子进程的**创建、生命周期、状态管理**，并暴露事件。整个文件约 115 行，是本讲的主角。 |
| [`src/server.ts`](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) | 在 `onInitialize` 里 `new TexpressoProcessManager(...)` 并 `start()`，在 `onShutdown` 里 `stop()`，并注册各种事件监听。本讲只引用它与生命周期相关的几处。 |

文件顶部的导入（[src/process-manager.ts:1-2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L1-L2)）已经能看出本讲的全部主线：

```ts
import { ChildProcess, spawn } from "child_process";
import { EventEmitter } from "events";
```

——用 `spawn` 启动一个 `ChildProcess`，并把自己包装成一个 `EventEmitter`。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**spawn 子进程** → **生命周期 start/stop** → **状态管理与超时等待**。这三者恰好对应「把程序跑起来 → 让它活着并最终关掉 → 确认它真的起来了」。

### 4.1 spawn 子进程

#### 4.1.1 概念说明

这个模块回答：**`TexpressoProcessManager` 要 spawn 的命令到底长什么样、参数从哪来？**

要 spawn 一个子进程，`spawn` 需要三样东西：

1. **可执行文件路径**（`executablePath`）：要运行的程序，本项目里就是 `texpresso`。
2. **参数数组**（`args`）：传给程序的命令行参数，比如 `["-json", "-lines"]`。
3. **选项**（options）：最关键的是 `stdio`，决定 stdin/stdout/stderr 怎么处理。

`stdio: "pipe"` 的意思是「给这三条标准流各建一根管道」，父进程（Node）可以通过 `child.stdin` 写、`child.stdout` 读、`child.stderr` 读。这正是双向通信的前提。

`TexpressoProcessManager` 继承自 `EventEmitter`（[src/process-manager.ts:4](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L4)），构造函数里第一句 `super()` 就是调用 `EventEmitter` 的构造函数，初始化事件机制。所以这个类从出生起就具备 `emit`/`on` 能力。

#### 4.1.2 核心流程

从「配置」到「实际 spawn 命令」的流程：

1. `server.ts` 在 `onInitialize` 中读取配置 `texpresso_path` 和 `root_tex`。
2. 调用 `new TexpressoProcessManager(texpresso_path, ["-json","-lines"], root_tex)`。
3. 构造函数把 `root_tex` 追加到 `args` 末尾。
4. 随后调用 `start()`，`start()` 内部才真正执行 `spawn(...)`。
5. 得到 `ChildProcess` 对象，存入字段 `this.process`。

伪代码：

```
executablePath = "texpresso"
args           = ["-json", "-lines"]
rootTex        = "main.tex"

# 构造函数内：
args.push(rootTex)        # args 变成 ["-json", "-lines", "main.tex"]

# start() 内部：
child = spawn(executablePath, args, { stdio: "pipe" })
# 等价于在终端执行：texpresso -json -lines main.tex
```

#### 4.1.3 源码精读

先看类的字段与构造函数（[src/process-manager.ts:4-16](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L4-L16)）：

```ts
export class TexpressoProcessManager extends EventEmitter {
    private process: ChildProcess | null = null;
    private isRunning: boolean = false;
    private stdout_buffer: string = '';

    constructor(
        private executablePath: string,
        private args: string[],
        private rootTex: string,
    ) {
        super();
        this.args.push(this.rootTex);
    }
```

逐行说明：

- `process: ChildProcess | null`：保存子进程对象，未启动时为 `null`，所以类型是「可空」。
- `isRunning: boolean`：进程是否在运行，本讲的「状态」主角，初始 `false`。
- `stdout_buffer: string`：stdout 的行缓冲区，**属于下一讲 u2-l3**，这里先不展开。
- 构造函数的三个 `private` 参数：用 2.4 节讲的「参数属性」简写，自动生成三个同名只读字段。
- `super()`：初始化 `EventEmitter`。
- `this.args.push(this.rootTex)`（[src/process-manager.ts:15](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L15)）：**本模块的关键一行**。它把主 `.tex` 文件名追加到参数数组末尾。这样 `args` 从 `["-json","-lines"]` 变成 `["-json","-lines","main.tex"]`。注意 `this.args` 和外部传入的数组是**同一个引用**（构造函数参数属性不会拷贝数组），所以这行会就地修改原数组——在这个项目里无害，因为那个数组是当场字面量创建的，但你写自己的代码时要留意这种副作用。

为什么要单独把 `rootTex` 作为第三个构造参数、再在构造函数里 `push` 进去？因为从语义上，`root_tex` 是「要预览的主文件」，和 `-json`、`-lines` 这类「运行模式开关」不是一类东西，分开传更清晰；而 `texpresso` 命令行约定主文件放在**最后**的位置参数，所以最终要把它追加到末尾。

再看真正的 spawn 调用（[src/process-manager.ts:24](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L24)，位于 `start()` 内）：

```ts
this.process = spawn(this.executablePath, this.args, { stdio: "pipe" });
this.isRunning = true;
```

`spawn` 立刻返回一个 `ChildProcess`，随即把 `isRunning` 置为 `true`。注意：此刻**程序未必真的起来了**——这正是 4.3 节要解决的问题。

最后看 `server.ts` 里的实例化点（[src/server.ts:65-70](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L70)），把配置和构造函数对上号：

```ts
texpressoProcess = new TexpressoProcessManager(
    connection.init_options.texpresso_path,   // executablePath → "texpresso"
    ["-json", "-lines"],                       // args（模式开关）
    connection.init_options.root_tex          // rootTex → "main.tex"
);
await texpressoProcess.start();
```

于是实际执行的命令就是：`texpresso -json -lines main.tex`。`-json` 让 `texpresso` 用 JSON 通信（下一讲详述），`-lines` 启用按行同步模式，最后的 `main.tex` 是要预览的主文件。

#### 4.1.4 代码实践

**实践目标**：把「配置 → 构造函数 → 最终命令行」这条链路走通，并用 Node 复刻一个最小 spawn。

**操作步骤**：

1. 打开 [src/server.ts:65-70](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L70)，确认三个实参分别取自 `connection.init_options` 的哪个字段。
2. 打开 [src/process-manager.ts:15](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L15)，理解 `push` 之后 `args` 的最终内容。
3. 在项目根目录新建一个 `spawn-demo.js`（**示例代码**，可以放在仓库任意临时位置，不要放进 `src/`），内容如下：

```js
// spawn-demo.js （示例代码，非项目原有文件）
const { spawn } = require("child_process");

// 模拟 TexpressoProcessManager 的 spawn 方式
const child = spawn("node", ["--version"], { stdio: "pipe" });

console.log("子进程 PID:", child.pid);

child.stdout.on("data", (data) => {
  console.log("stdout:", data.toString().trim());
});

child.on("exit", (code, signal) => {
  console.log(`进程退出: code=${code}, signal=${signal}`);
});
```

4. 运行 `node spawn-demo.js`。

**需要观察的现象**：

- 打印出的 `child.pid` 是一个数字（子进程的进程 ID）。
- 紧接着 `stdout:` 打印出 Node 的版本号（如 `v20.x.x`）。
- 最后打印 `进程退出: code=0, signal=null`。

**预期结果**：你能看到 spawn 立刻返回了一个带 `pid` 的对象，而输出是**稍后**通过 `stdout` 的 `data` 事件异步到达的——这印证了 4.1.1 节「spawn 是流式、异步」的特性。如果系统里没有 `node` 命令，把 `"node"` 换成 `"ls"`、参数换成 `[]` 同样可以观察。

> 说明：本仓库未配置测试，本实践以独立脚本验证 spawn 行为，不依赖项目构建。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `this.args.push(this.rootTex)` 这一行删掉，`texpresso` 启动时会发生什么？

**参考答案**：`texpresso` 将收不到主文件参数，执行命令变成 `texpresso -json -lines`（缺少末尾的 `main.tex`）。`texpresso` 大概率因为缺少输入文件而报错或行为异常。这行的作用就是保证主文件作为位置参数传给 `texpresso`。

**练习 2**：构造函数里 `args` 是用 `private` 修饰的参数属性。它和把 `rootTex` 也设成参数属性后，`this.args.push(this.rootTex)` 里 `this.rootTex` 指向什么？

**参考答案**：指向构造时传入、并由参数属性自动生成的实例字段 `this.rootTex`（即 `main.tex`）。因为参数属性已把它存为字段，所以构造函数体内能通过 `this.rootTex` 访问到。

---

### 4.2 生命周期 start/stop

#### 4.2.1 概念说明

这个模块回答：**子进程什么时候被创建、什么时候被销毁，谁来管？**

任何进程都有生命周期：**创建 → 运行 → 停止**。在 `texpresso-lsp` 里，这个生命周期被绑定到 LSP 的握手与关闭上：

- LSP 客户端发起 `initialize` 握手 → 服务器在 `onInitialize` 里 `start()` 拉起 `texpresso`。
- LSP 客户端发送 `shutdown` → 服务器在 `onShutdown` 里 `stop()` 关掉 `texpresso`。

`TexpressoProcessManager` 用一个布尔字段 `isRunning` 作为「单一状态真相」：所有关于「进程是否活着」的判断都看它，避免多处各维护一份状态导致不一致。

#### 4.2.2 核心流程

`start()` 的流程：

```
start():
  若 isRunning == true   → 抛错（防止重复启动）
  spawn() 得到 child
  isRunning = true
  挂载 stdout / stderr / error / exit 监听
  等待首次 stdout（详见 4.3）
  若中途抛错 → isRunning = false，重新抛出
```

`stop()` 的流程：

```
stop():
  若 isRunning == false 或 process == null → 直接 return（幂等）
  child.kill()          发送 SIGTERM
  等 child 的 exit 事件
    → isRunning = false
    → process = null
    → resolve()
```

注意两个 guard（守卫）的不对称：`start()` 发现已在运行就**抛异常**（这是错误用法，要 loudly fail）；`stop()` 发现没在运行就**静默返回**（关闭一个已经关掉的东西不该报错，这叫幂等）。这种「启动严格、关闭宽容」的取舍很常见。

#### 4.2.3 源码精读

先看 `start()` 的整体结构（[src/process-manager.ts:18-86](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L18-L86)）。本模块先看它的守卫与事件挂载，4.3 节再看它的超时等待：

```ts
public async start(): Promise<void> {
    if (this.isRunning) {
        throw new Error("Texpresso process is already running");
    }
    try {
        this.process = spawn(this.executablePath, this.args, { stdio: "pipe" });
        this.isRunning = true;
        // ... stdout / stderr 监听（stdout 详解见 u2-l3）...
        this.process.on("error", (error: Error) => { this.emit("error", error); });
        this.process.on("exit", (code, signal) => {
            this.isRunning = false;
            this.emit("exit", { code, signal });
        });
        // ... 超时等待（见 4.3）...
    } catch (error) {
        this.isRunning = false;
        throw error;
    }
}
```

几个要点：

- `isRunning = true`（[src/process-manager.ts:25](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L25)）紧跟 `spawn`，表示「对象已认为进程在跑」。
- 底层 `ChildProcess` 的 `error` 事件被**转发**为自身的 `error` 事件（[src/process-manager.ts:52-54](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L52-L54)）——注意 `error` 在 `EventEmitter` 里是特殊事件名，若无人监听会抛出并可能崩溃进程；`server.ts` 里恰好注册了监听（见下）。
- 底层 `exit` 事件被转发为自身的 `exit` 事件，并把 `isRunning` 置回 `false`（[src/process-manager.ts:56-62](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L56-L62)）——**这是进程「自然死亡」时更新状态的唯一路径**。
- `catch` 块里把 `isRunning` 复位为 `false` 再 `throw`（[src/process-manager.ts:82-85](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L82-L85)）：保证启动失败后状态是干净的，不会留下「声称在跑但其实没跑」的不一致状态。

再看 `stop()`（[src/process-manager.ts:88-101](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L88-L101)）：

```ts
public async stop(): Promise<void> {
    if (!this.isRunning || !this.process) {
        return;            // 幂等：没在跑就直接返回
    }
    return new Promise<void>((resolve) => {
        this.process?.kill();              // 发 SIGTERM
        this.process?.on("exit", () => {   // 等真正退出
            this.isRunning = false;
            this.process = null;
            resolve();
        });
    });
}
```

- `kill()` 不带参数时默认发送 `SIGTERM` 信号，是一种「礼貌地请对方退出」的方式。
- 返回的 `Promise` 要等到 `exit` 事件才 `resolve`——也就是说 `stop()` 不仅发信号，还**等到进程真的退出**才完成，这对优雅关闭很重要。
- 局限性：如果子进程忽略 `SIGTERM` 不肯退出，这个 `Promise` 将永远不 `resolve`，`stop()` 会挂住。本讲只指出这一点，更完整的错误场景分析放在 u3-l3。

最后把视线拉回 `server.ts`，看这一对 `start/stop` 是在哪里被调用的（[src/server.ts:65-70](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L70) 与 [src/server.ts:270-279](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L270-L279)）：

```ts
// onInitialize 里：
await texpressoProcess.start();        // 握手阶段拉起

// onShutdown 里：
connection.onShutdown(async () => {
    try {
        await texpressoProcess.stop(); // 关闭阶段优雅停止
        connection.console.log("Texpresso process stopped successfully");
    } catch (error) { /* 记日志 */ }
});
```

生命周期与 LSP 生命周期严格对齐：握手即生，关闭即灭。

#### 4.2.4 代码实践

**实践目标**：确认 `start()`/`stop()` 的调用时机与各自的守卫行为。

**操作步骤**：

1. 在 `server.ts` 中搜索 `texpressoProcess.start()` 与 `texpressoProcess.stop()`，确认它们分别位于 `onInitialize`（[src/server.ts:70](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L70)）与 `onShutdown`（[src/server.ts:272](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L272)）。
2. 回到 [src/process-manager.ts:18-21](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L18-L21) 与 [src/process-manager.ts:88-91](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L88-L91)，对比两处守卫的写法。
3. 阅读下面的「现象预测」，在脑中推演，无需真跑。

**需要观察/思考的现象**：

- 假设在同一个 `TexpressoProcessManager` 实例上**连续调用两次** `start()`：第一次成功，第二次会发生什么？
- 假设进程已**自然崩溃**（`exit` 事件已触发，`isRunning` 已被置 `false`）后，再调用 `stop()`：会发生什么？

**预期结果**：

- 第二次 `start()` 会命中 `if (this.isRunning)` 守卫，抛出 `"Texpresso process is already running"`。
- 进程崩溃后再 `stop()`，会命中 `if (!this.isRunning || !this.process)` 守卫，**直接返回**，不报错、不二次 kill——这正是「关闭幂等」的价值。

> 说明：是否能在你的环境真正触发，取决于是否装了 `texpresso`，若没有，则以源码推演为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `start()` 在重复调用时选择「抛异常」，而 `stop()` 在没运行时选择「静默返回」？

**参考答案**：重复 `start()` 几乎一定是调用方的 bug（逻辑错误），应当尽快暴露，所以抛异常；而 `stop()` 常在「不确定是否还活着」的清理路径上被调用（比如 `onShutdown`），对一个已经死掉的进程再 stop 不应当算错误，所以静默返回（幂等）。这是「严格创建、宽容销毁」的常见取舍。

**练习 2**：`stop()` 里的 `Promise` 什么时候 `resolve`？如果子进程收到 `SIGTERM` 后不退出会怎样？

**参考答案**：在子进程真正触发 `exit` 事件后才 `resolve`。若子进程捕获并忽略 `SIGTERM`、迟迟不退出，`exit` 事件不会触发，`Promise` 永不 `resolve`，`stop()` 会一直挂住（这是当前实现的一个局限）。

---

### 4.3 状态管理与超时等待

#### 4.3.1 概念说明

这个模块回答：**`spawn` 之后立刻把 `isRunning` 设成 `true`，但程序真的起来了吗？怎么确认？**

这是子进程编程里一个经典的坑：`spawn()` 是「发射后不管（fire-and-forget）」式的——它**立刻**返回一个 `ChildProcess` 对象，但这只代表「Node 已经向操作系统发出了启动请求」，**不代表目标程序真的起来了**。如果 `texpresso_path` 写错了、可执行文件根本不存在，`spawn` 不会立刻报错，而是稍后通过 `error` 事件通知。

如果在握手阶段不确认 `texpresso` 真的可用，就贸然向客户端返回「服务器就绪」，后面所有命令都会发向一个根本没起来的进程，问题会被推迟、被掩盖。所以 `start()` 需要一个**「就绪检测」**：等到有证据表明 `texpresso` 真的活了，才算启动成功；如果等太久，就判定失败。

这里的「证据」被选定为 **stdout 的第一个数据块**——即 `texpresso` 第一次往 stdout 写东西。配合一个 5 秒的超时，就构成了「`Promise` + 超时」的经典异步等待模式。

#### 4.3.2 核心流程

「`Promise` + 超时等待首次 stdout」的执行过程，本质是**两个异步任务赛跑（race）**：

```
注册一个 5 秒的定时器 setTimeout  ──┐
                                    ├── 谁先触发谁定胜负
注册 stdout 的 data 监听         ──┘

分支 A：5 秒内 stdout 来了第一个 data
  → clearTimeout(定时器)
  → resolve()  → start() 成功返回

分支 B：5 秒到了 stdout 仍无数据
  → reject("Process start timeout")
  → start() 抛错 → onInitialize 整个握手失败

分支 C：stdout 根本不存在（极少见）
  → clearTimeout + reject("Process stdout not available")
```

用文字公式表达这个 race（「先到者胜」）：

\[
\text{start 的结果} = \text{first}(\ \text{stdout首个data},\ \text{5秒超时}\ )
\]

先到的是「stdout 数据」就成功，先到的是「超时」就失败。

#### 4.3.3 源码精读

这段是本讲最值得逐行读的代码（[src/process-manager.ts:64-81](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L64-L81)）：

```ts
// Wait for process to start
await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(() => {
        reject(new Error("Process start timeout"));
    }, 5000);

    const stdout = this.process?.stdout;
    if (!stdout) {
        clearTimeout(timeout);
        reject(new Error("Process stdout not available"));
        return;
    }

    stdout.on("data", () => {
        clearTimeout(timeout);
        resolve();
    });
});
```

逐行说明：

- `setTimeout(..., 5000)`：开一个 5 秒的定时器，到点就 `reject`——这是「兜底」，防止无限等待。
- `this.process?.stdout`：用可选链 `?.` 安全取 stdout（理论上 `pipe` 模式下一定存在，但类型上是可空的，所以守卫）。
- 若 stdout 不存在：先 `clearTimeout` 取消定时器，再 `reject`，并 `return` 退出。注意「**先取消定时器再 reject**」是好习惯，避免定时器在 Promise 已经 settle 后还空跑。
- `stdout.on("data", ...)`：注册一个 `data` 监听器，**首次**收到数据就 `clearTimeout` 并 `resolve`。

这里有几个初学者容易忽略的细节：

1. **`resolve`/`reject` 的幂等性**：JavaScript 的 `Promise` 一旦 settle（成功或失败），后续再调用 `resolve`/`reject` 都是**空操作**。所以即便 `data` 事件后来又触发了多次，重复 `resolve()` 不会有副作用。
2. **存在两个 `data` 监听器**：注意 [src/process-manager.ts:27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L27) 那个负责「逐行缓冲 + JSON 解析 + emit」的主处理器，和这里第 77 行「只为了 resolve 一次」的监听器，是**同一个 stdout 上的两个独立监听器**。Node 的 `EventEmitter` 允许同一事件挂多个监听器，它们都会被触发。这里的第二个监听器**之后并未被移除**，会一直挂着——但由于 `resolve` 幂等，它在此后每次 `data` 时只是空跑一次，无害（属于一个可以优化的细节，比如改用 `stdout.once(...)`）。
3. **超时传播到握手**：超时 `reject` 的错误会被 `start()` 的 `catch` 重新抛出（[src/process-manager.ts:82-85](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L82-L85)），进而被 `server.ts` 里 `onInitialize` 的 `try/catch` 捕获（[src/server.ts:124-129](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L124-L129)）并记录日志后再次抛出——**最终导致整个 LSP 握手失败**。也就是说：「5 秒内 `texpresso` 没有动静」=「服务器拒绝上线」。这是一个很强的契约。
4. **一个隐含假设**：这种「等首次 stdout」的就绪检测，**假设 `texpresso` 启动时会主动往 stdout 写点东西**（比如就绪提示）。如果某个版本的 `texpresso` 启动后安静等待命令、不主动输出，那么即便它其实正常启动了，也会被误判为超时失败。这是该设计的一个前提条件。

`isRunning` 作为状态真相，在三个地方被写：`start()` 成功时置 `true`（第 25 行）、`exit` 事件时置 `false`（第 59 行）、`start()` 的 `catch` 与 `stop()` 中置 `false`。读它的地方除了内部守卫，还有对外暴露的 `isProcessRunning()`（[src/process-manager.ts:112-114](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L112-L114)），它只是一个简单的 getter。

#### 4.3.4 代码实践

**实践目标**：理解超时等待的触发条件与失败后果，能预测不同故障下的行为。

**操作步骤**：

1. 重读 [src/process-manager.ts:64-81](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L64-L81)，把「定时器」和「data 监听」两条线分别标出来。
2. 顺着 [src/server.ts:70](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L70) → [src/server.ts:124-129](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L124-L129)，追踪超时错误如何一路传到 `onInitialize` 的 `catch`。
3. 做下面的「现象预测」。

**需要思考的现象（故障推演）**：

- 场景一：初始化选项里 `texpresso_path` 指向一个**不存在的路径**。`spawn` 会立刻报错吗？5 秒后会怎样？
- 场景二：`texpresso` 存在，但它启动后**不输出任何 stdout**，安静等待命令。`start()` 会成功吗？
- 场景三：`texpresso` 正常启动并在 1 秒内输出了一行。定时器会被取消吗？

**预期结果**：

- 场景一：`spawn` **不会**立刻报错；约 5 秒后命中超时 `reject("Process start timeout")`（实际中若路径不存在，底层通常先触发 `error` 事件并被转发，但 `start()` 的超时是兜底逻辑）。
- 场景二：**不会**成功——会超时失败。这暴露了 4.3.3 节第 4 点的隐含假设。
- 场景三：会。`data` 监听先触发，执行 `clearTimeout(timeout)` 把那个还没到点的定时器取消掉，然后 `resolve()`，`start()` 成功。

> 说明：场景一/二的实际触发依赖于 `texpresso` 是否安装及其输出行为，本机若无 `texpresso` 则标注「待本地验证」；逻辑推演结论如上。

#### 4.3.5 小练习与答案

**练习 1**：把等待首次 stdout 改成「等待首次 stderr」是否可行？为什么作者选了 stdout？

**参考答案**：技术上能改，但语义不同。stderr 通常用来报错，把它当作「就绪信号」不可靠（程序正常启动未必写 stderr）。作者选 stdout，是因为 `texpresso` 在 `-json` 模式下会通过 stdout 发送就绪/事件数据，stdout 有数据是「程序已起来并能通信」更可信的信号。

**练习 2**：第 77 行的 `stdout.on("data", ...)` 监听器在 `resolve` 之后并不会被移除。这会带来实际问题吗？如何小幅改进？

**参考答案**：由于 `Promise` 只会 settle 一次，后续触发的 `resolve` 是空操作，所以**没有功能性 bug**，只是每次 stdout 来数据都会多调用一次空操作回调，属于轻微的冗余。改进方式是把 `stdout.on("data", ...)` 换成 `stdout.once("data", ...)`，这样首次触发后监听器会自动移除，更干净。

---

## 5. 综合实践

把本讲三个模块串起来：**spawn 一个进程、用「超时 + 首次输出」确认它就绪、观察它退出、并能主动停止它**。

新建文件 `mini-process-manager.js`（**示例代码**，非项目原有文件）：

```js
// mini-process-manager.js （示例代码）
const { spawn } = require("child_process");

// 一个极简版的 TexpressoProcessManager，只保留本讲关心的生命周期与超时
class MiniProcessManager {
  constructor(executablePath, args) {
    this.executablePath = executablePath;
    this.args = args;
    this.process = null;
    this.isRunning = false;
  }

  async start(timeoutMs = 5000) {
    if (this.isRunning) throw new Error("already running");

    this.process = spawn(this.executablePath, this.args, { stdio: "pipe" });
    this.isRunning = true;
    console.log("[start] spawned, pid =", this.process.pid);

    // 转发 exit：进程「自然死亡」时更新状态
    this.process.on("exit", (code, signal) => {
      this.isRunning = false;
      console.log(`[exit] code=${code} signal=${signal}`);
    });

    // 超时 + 首次 stdout 赛跑（对应 process-manager.ts:64-81）
    await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("Process start timeout")),
        timeoutMs
      );
      const stdout = this.process.stdout;
      if (!stdout) {
        clearTimeout(timer);
        reject(new Error("stdout not available"));
        return;
      }
      stdout.once("data", () => {       // 用 once 而非 on，避免遗留监听器
        clearTimeout(timer);
        resolve();
      });
    });
    console.log("[start] 收到首个 stdout，视为就绪");
  }

  stop() {
    if (!this.isRunning || !this.process) {
      console.log("[stop] 未在运行，幂等返回");
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      this.process.kill();              // 发 SIGTERM
      this.process.on("exit", () => {
        this.isRunning = false;
        this.process = null;
        resolve();
      });
    });
  }
}

// 用一个「每 300ms 输出一行 JSON」的 node 子进程来模拟 texpresso 的行为
const mgr = new MiniProcessManager("node", [
  "-e",
  "setInterval(() => console.log(JSON.stringify(['ping'])), 300)",
]);

mgr.start()
  .then(() => new Promise((r) => setTimeout(r, 1200)))  // 让它跑一会儿
  .then(() => mgr.stop())
  .then(() => console.log("[done] 已停止"))
  .catch((e) => console.error("[failed]", e.message));
```

**实践目标**：在一个脚本里完整复刻本讲的「spawn → 超时就绪检测 → 退出转发 → 主动 stop」闭环，加深对三个最小模块及其协作的理解。

**操作步骤**：

1. 把上面的脚本保存为 `mini-process-manager.js`，运行 `node mini-process-manager.js`。
2. 观察输出顺序：`[start] spawned` → `[start] 收到首个 stdout` →（若干 `[exit]` 之前的 `[ping]` 由子进程产生）→ `stop()` 后的 `[exit]` → `[done]`。
3. 把构造参数里的 `"node"` 换成一个**不存在的命令**（如 `"no-such-program"`），再运行，观察是否在约 5 秒后打印 `[failed] Process start timeout`（或更早的 error）。
4. 把子进程代码里的 `console.log(...)` 去掉（让子进程**不输出 stdout**），再运行，观察 `start()` 是否会超时失败——这验证了 4.3 节「就绪检测依赖子进程主动输出」的隐含假设。

**需要观察的现象**：

- 正常情况下，`[start] 收到首个 stdout` 几乎立刻出现（远早于 5 秒），说明就绪检测快速通过。
- `stop()` 调用后，`[exit]` 才打印，证明 `stop()` 等到了真正的退出事件。
- 故障情况下，会在超时或报错路径打印 `[failed] ...`。

**预期结果**：你将直观看到「`spawn` 立刻返回 pid」与「程序真正就绪」之间的时间差，以及超时机制如何把这个差值变成可判定的成功/失败。

> 说明：本实践为独立示例脚本，不修改项目源码，也不依赖 `texpresso` 可执行文件。

## 6. 本讲小结

- `TexpressoProcessManager` 用 `child_process.spawn(executablePath, args, { stdio: "pipe" })` 启动 `texpresso` 子进程；流式的 `spawn`（而非缓冲式的 `exec`）是长期双向通信的唯一合适选择。
- 构造函数的 `this.args.push(this.rootTex)` 把主文件追加到参数末尾，最终执行命令为 `texpresso -json -lines main.tex`；`private` 参数属性是 TypeScript 的字段简写。
- 生命周期与 LSP 对齐：`onInitialize` 里 `start()`，`onShutdown` 里 `stop()`；`start()` 重复调用会抛异常（严格），`stop()` 在未运行时静默返回（幂等）。
- `isRunning` 是进程状态的「单一真相」，在 `start` 成功、`exit` 事件、`catch`/`stop` 三类路径上被写入。
- `start()` 用「`Promise` + 5 秒 `setTimeout`」与「首次 stdout 的 `data` 事件」赛跑，作为就绪检测；超时或无 stdout 都会导致 `start()` 失败，进而使整个 LSP 握手失败。
- 该类继承 `EventEmitter`，把底层 `ChildProcess` 的 `error`/`exit`/`stderr` 转发为自身事件供 `server.ts` 监听——这是典型的包装器模式。

## 7. 下一步学习建议

本讲把「进程怎么起来、怎么活着、怎么确认就绪」讲清了，但 `start()` 里那个 stdout 主处理器（[src/process-manager.ts:27-46](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L27-L46)）我们刻意没有展开——它涉及 `stdout_buffer` 逐行缓冲、`JSON.parse` 容错、以及 `emit(command, data)` 的事件分发。这正是下一讲 **u2-l3「JSON 行协议与命令分发」**的主题：服务器与 `texpresso` 之间如何用「换行分隔的 JSON（NDJSON）」收发命令与事件。

建议在进入 u2-l3 前，先回头扫一眼 `sendCommand`（[src/process-manager.ts:103-110](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L103-L110)）——它写了 stdin，与本讲讲的读 stdout 恰好是通信的两个方向，对照阅读会更有整体感。
