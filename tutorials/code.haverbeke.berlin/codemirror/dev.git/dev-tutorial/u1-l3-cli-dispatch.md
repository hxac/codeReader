# 命令行骨架：cm.js 的命令分发与帮助系统

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行讲出 `bin/cm.js` 中 `start()` 的执行过程：从 `process.argv` 取命令、查映射表、校验参数个数、调用实现函数。
2. 理解 `cmdFn.length` 这个「用函数形参个数做参数校验」的技巧，并能算出任意一个子命令至少需要几个参数。
3. 理解 `new Promise(r => r(cmdFn.apply(null, args))).catch(e => error(e))` 为什么能把同步异常和异步失败都送到 `error()` 这个统一出口。
4. 能对照 `help()` 输出的用法文本，在源码里找到每个子命令对应的实现函数——甚至发现 `help()` 文本漏掉的命令。
5. 读懂 `run()` 这个子进程封装的每一个默认选项，明白 `stdout: "pipe"` 与 `"inherit"` 的区别。
6. 亲手给 `cm` 加一个 `echo` 子命令，完整走一遍「注册命令 → 写实现 → 更新帮助文本 → 验证」的流程。

## 2. 前置知识

本讲几乎不需要任何 CodeMirror 领域知识，但需要一点 Node.js 命令行基础。下面用通俗语言把概念补齐。

### 2.1 process.argv：命令行参数长什么样

当你在终端敲：

```bash
node bin/cm.js grep EditorView
```

Node.js 进程内部的 `process.argv` 是一个字符串数组：

```
索引 0: "/usr/local/bin/node"        ← node 可执行文件本身的路径
索引 1: "/path/to/dev/bin/cm.js"     ← 被执行的脚本路径
索引 2: "grep"                       ← 第一个用户参数：子命令名
索引 3: "EditorView"                 ← 第二个用户参数：子命令自己的参数
```

所以 `argv[2]` 永远是子命令名，`argv.slice(3)` 是传给子命令的参数列表。这是几乎所有手写 CLI 的第一步。

### 2.2 「命令 → 函数」映射表

比 `if (command === "build") ... else if (command === "test") ...` 更简洁的做法，是用一个对象字面量把命令名映射到函数：

```js
let cmdFn = {build, test, grep}[command]
```

查不到就是 `undefined`，一步完成「分发 + 判空」。这是本讲的主角。

### 2.3 函数的 length 属性（形参个数）

JavaScript 里每个函数都有一个 `length` 属性，等于**声明的、没有默认值的、不是 rest 的**形参个数：

```js
function a(x, y) {}        // a.length === 2
function b(x = 1, y) {}    // b.length === 0（x 有默认值，遇到默认值就停止计数）
function c(...args) {}     // c.length === 0（rest 参数不计数）
function d(x, ...args) {}  // d.length === 1
```

`cm.js` 直接拿它当「最少参数个数」来用——不需要任何参数解析库。

### 2.4 函数声明提升（hoisting）

`function foo() {}` 这种**函数声明**会在代码实际运行前就注册到作用域里。所以映射表写在文件开头、实现函数写在三百行之后，也完全合法。

### 2.5 退出码、stdout 与 stderr

- `process.exit(n)` 立即结束进程，`n` 是退出码：`0` 表示正常，非零（惯例用 `1`）表示出错。Shell 里用 `echo $?` 查看上一条命令的退出码。
- `console.log` 写到 **stdout**（标准输出），`console.error` 写到 **stderr**（标准错误）。两者都可以显示在终端上，但重定向时分开：`cm foo > out.txt` 只会捕获 stdout，错误信息仍然直接打到屏幕上。.usage 文本属于 stdout，报错属于 stderr，这是 Unix CLI 的基本礼节。

### 2.6 Promise 如何统一同步与异步错误

- `new Promise(executor)` 中，executor 函数体内 `throw` 出去的任何异常，都会让这个 Promise 变成 rejected（而不是像普通调用那样直接炸掉进程栈）。
- 如果往 `resolve(r)` 里传入另一个 Promise（例如 `async` 函数的返回值），外层 Promise 会「采纳」它：它失败，外层也失败。

这两条合起来，意味着 `.catch()` 一个出口就能同时接住「函数同步 throw」和「async 函数异步 reject」。`cm.js` 正是这么干的。

### 2.7 child_process.execFileSync 一句话版

`execFileSync(cmd, args, options)` 用给定的参数数组同步执行一个子进程：子进程结束前当前进程一直等待；退出码非零时它会**抛异常**；`encoding: "utf8"` 让它把子进程 stdout 以字符串返回。它是 `cm.js` 里所有 `git`/`npm`/`grep` 调用的底座。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js) | 唯一的主角。整个 CLI：`start()` 分发（L13-36）、`help()` 用法文本（L38-57）、`error()` 错误出口（L59-62）、`run()` 子进程封装（L64-66），以及全部子命令实现（L81 起）。 |
| [README.md](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md) | 辅助阅读：L9-21 介绍了 `install`、`build`、`npm run dev` 三条最常用命令的使用场景，可以当作 `help()` 文本的「用户视角版」。 |
| [package.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json) | 辅助阅读：L3-8 的四个脚本 `test`、`test-node`、`prepare`、`dev` 全部是 `node bin/cm.js <子命令>` 的别名——npm 脚本是进入这套 CLI 的第二个入口。 |

另外，[bin/cm.js:1-11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L1-L11) 是模块头部：L3-4 的注释提醒「不要在这里 require node_modules 的东西，因为 install 必须在 node_modules 存在之前就能跑」（u1-l2 已详述），L9-11 加载包注册表得到全局的 `packages` / `packageNames` / `buildPackages`。L72-79 的 `assertInstalled()` 会逐个检查包目录是否存在，是本讲会反复遇到的守卫。

## 4. 核心概念与源码讲解

### 4.1 start()：用一张映射表实现子命令分发

#### 4.1.1 概念说明

一个 CLI 工具要解决的第一件事是「分发」：用户敲了 `cm build`，程序得找到 `build` 对应的处理函数并调用它。成熟方案有 commander、yargs 等参数解析库，但 `cm.js` 选择了零依赖的手写方案——因为它必须能在 `node_modules` 尚不存在的全新克隆上运行（见 u1-l2）。这个方案的核心是三样东西：

1. **一张「命令名 → 函数」的对象映射表**，代替一长串 `if/else`；
2. **用函数的 `length` 属性做参数个数下限校验**，代替参数校验库；
3. **一个 Promise 包裹**，让所有实现函数不管同步还是异步出错，都汇入同一个 `error()` 出口。

#### 4.1.2 核心流程

`start()` 的执行过程可以画成：

```
node bin/cm.js <command> [args...]
        │
        ▼
① command = process.argv[2]        ← 取子命令名
        │
        ▼
② command 存在 且 不是 install/--help？
        │  是
        ▼
   assertInstalled()               ← 包目录守卫，缺包则打印错误并 exit 1
        │
        ▼
③ args = process.argv.slice(3)     ← 剩余参数
        │
        ▼
④ cmdFn = 映射表[command]           ← 查表，查不到为 undefined
        │
        ▼
⑤ cmdFn 不存在 或 cmdFn.length > args.length？
        │  是                              │  否
        ▼                                  ▼
   help(1) 打印用法并退出        ⑥ new Promise(r => r(cmdFn.apply(null, args)))
                                           │
                                     函数执行（同步 throw 或异步 reject）
                                           │
                                           ▼
                                    .catch(e => error(e)) 打印错误并 exit 1
```

#### 4.1.3 源码精读

先看完整函数（只有 24 行）：

[bin/cm.js:13-36](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L13-L36) —— `start()` 的全部：取命令、守卫、取参数、查映射表、校验、包裹执行。

下面拆成三段精读。

**第一段：取命令 + 守卫**

```js
let command = process.argv[2]
if (command && !["install", "--help"].includes(command)) assertInstalled()
let args = process.argv.slice(3)
```

对应 [bin/cm.js:14-16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L14-L16)：取出子命令名后，除了 `install` 和 `--help` 这两个「必须在没有包目录时也能跑」的命令之外，一律先过 `assertInstalled()`。注意 `command &&` 这个短路：**连命令都没给**（`node bin/cm.js`）时守卫不触发，会一路走到查表，`cmdFn` 为 `undefined`，最终由 `help(1)` 收场。

守卫本体在 [bin/cm.js:72-79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)：遍历包注册表，任何一个包目录缺失就向 stderr 打印 `module <名字> is missing. Did you forget to run 'cm install'?` 并以退出码 1 结束。

**第二段：映射表**

[bin/cm.js:17-33](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L17-L33) —— 把 15 个命令名（含 `--help`）一一映射到实现函数上。

```js
let cmdFn = {
  packages: listPackages,
  status,
  build,
  ...
  "build-readme": buildReadme,
  run: runCmd,
  "--help": () => help(0)
}[command]
```

三个值得注意的细节：

- **函数声明提升**：表里引用的 `install` 定义在 L81、`test` 定义在 L352，都比 L17 晚了两三百行。因为它们都是 `function` 声明，解析阶段就已注册，且 `start()` 直到文件末尾的 [bin/cm.js:366](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L366) 才被调用，此时一切就绪。
- **`--help` 也是表里的一项**：它没有独立实现函数，值是一个箭头函数 `() => help(0)`，把「帮助」当成一个普通命令统一分发，不需要特判。
- **ES6 简写**：`status,` 等价于 `status: status`。

**第三段：校验与执行**

[bin/cm.js:34-35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L34-L35) —— 整个 CLI 的安全网只有两行。

```js
if (!cmdFn || cmdFn.length > args.length) help(1)
new Promise(r => r(cmdFn.apply(null, args))).catch(e => error(e))
```

第一行同时挡住两种情况：**未知命令**（`cmdFn` 为 `undefined`）和**参数不足**（实现函数声明的形参个数超过了实际给出的参数个数）。第二行把调用包进 Promise：

- `cmdFn.apply(null, args)` 用 `args` 数组作为位置参数调用函数（`null` 表示不关心 `this` 指向）；
- executor 里如果**同步 throw**（例如 [bin/cm.js:176](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L176) 中 `bumpVersion` 在没有任何变更时 `throw new Error("No new release notes!")`），Promise 直接 reject；
- 如果函数是 `async`（例如 [bin/cm.js:118](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L118) 的 `build`），返回值是一个 Promise，`r(promise)` 会采纳它，其失败同样传到 `.catch`。

于是 `error(e)`（L59-62）成为**唯一**的失败出口：打印错误、退出码 1。

**`cmdFn.length` 技巧全景表**

把映射表里每个函数的声明方式核对一遍（「示例表格，数据逐一核对自源码」）：

| 命令 | 实现函数（行号） | 声明形式 | `length` | 至少需要参数 |
| --- | --- | --- | --- | --- |
| `packages` | `listPackages`（L106） | `()` | 0 | 0 |
| `status` | `status`（L110） | `()` | 0 | 0 |
| `build` | `build`（L118） | `async ()` | 0 | 0 |
| `devserver` | `devserver`（L153） | `(...args)` | 0 | 0 |
| `release` | `release`（L225） | `(...args)` | 0 | 0 |
| `unreleased` | `unreleased`（L282） | `()` | 0 | 0 |
| `install` | `install`（L81） | `(arg = null)` | 0 | 0 |
| `clean` | `clean`（L290） | `()` | 0 | 0 |
| `commit` | `commit`（L295） | `(...args)` | 0 | 0 |
| `push` | `push`（L302） | `(...args)` | 0 | 0 |
| `grep` | `grep`（L309） | `(pattern)` | 1 | **1** |
| `build-readme` | `buildReadme`（L346） | `(name)` | 1 | **1** |
| `test` | `test`（L352） | `(...args)` | 0 | 0 |
| `run` | `runCmd`（L334） | `(cmd, ...args)` | 1 | **1** |
| `--help` | 箭头函数（L32） | `()` | 0 | 0 |

规律非常清晰：**需要强制参数的命令（`grep`、`build-readme`、`run`）用普通形参声明；参数可选或数量不定的命令，一律用默认值参数或 rest 参数把 `length` 压成 0。**写新命令时选对声明形式，就等于选对了参数校验行为。

还有一个推论：校验只管「下限」，不管「上限」。`cm status foo` 中 `status.length (0) > args.length (1)` 为假，照常执行，`"foo"` 被 `apply` 传进去后被不声明形参的 `status` 静默忽略。另外，`install` 的 `--ssh` 选项校验发生在函数**内部**（L83：`if (arg && arg != "--ssh") help(1)`），说明「选项合法性」这类更细的校验留给实现函数自己——这是这套骨架的分工：骨架管分发和下限，函数管语义。

#### 4.1.4 代码实践

**实践 A：观察分发行为与退出码（只读，不改任何文件）**

1. **实践目标**：亲眼确认「未知命令 / 无参数 → `help(1)`」「`--help` → `help(0)`」以及 `assertInstalled` 守卫的触发时机。
2. **操作步骤**：
   - 在仓库根目录依次执行（每条后面跟上 `echo $?` 查看退出码，可写在一行：`node bin/cm.js --help; echo "exit=$?"`）：
     - `node bin/cm.js --help`
     - `node bin/cm.js`（不给任何参数）
     - `node bin/cm.js nonsense`（不存在的命令）
3. **需要观察的现象**：
   - `--help`：终端打印完整用法文本，`exit=0`。它和 `install` 是仅有的两个不查包目录的命令，所以在**尚未执行 `cm install` 的全新克隆上也能运行**。
   - 无参数：`command` 为 `undefined`，短路跳过守卫，`cmdFn` 为 `undefined`，同样打印用法文本，但 `exit=1`（错误出口）。
   - 未知命令：行为取决于你的环境——**已经 `cm install` 过**的机器上打印用法文本并 `exit=1`；**包还没克隆**的机器上会在守卫处先失败，打印 `module <某个包名> is missing. Did you forget to run 'cm install'?` 并 `exit=1`（因为 `nonsense` 不在 L15 的豁免列表里，`assertInstalled()` 先于查表执行）。
4. **预期结果**：三种情况退出码分别为 0、1、1；前两种的输出与 [bin/cm.js:39-55](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L39-L55) 的模板字符串逐字一致。
5. 本讲义写作环境无法执行项目脚本，以上均为基于源码的推演，**待本地验证**。

**实践 B：用 node 打印 process.argv（不碰项目文件）**

1. **实践目标**：把 2.1 节的 argv 结构图和真实输出对上号。
2. **操作步骤**：执行 `node -p "process.argv" bin/cm.js grep EditorView`（`-p` 会求值表达式并打印结果；这里只是借用 Node 进程，`bin/cm.js` 不会被执行，因为 `-p` 模式下它只是 argv 里的一个字符串）。
3. **需要观察的现象**：数组的索引 1 是 `bin/cm.js` 的路径，索引 2 是 `grep`，索引 3 是 `EditorView`。
4. **预期结果**：与 2.1 节的表格完全对应，从而确认「`argv[2]` 是命令、`slice(3)` 是参数」这两个下标的来历。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：执行 `node bin/cm.js grep`（不带模式参数）会发生什么？为什么？

**答案**：`grep` 在映射表中存在，但 `grep` 函数声明为 `function grep(pattern)`（L309），`length` 为 1，而 `args.length` 为 0，`1 > 0` 成立，于是走 `help(1)`：打印用法文本后以退出码 1 退出。参数下限校验正是靠 `cmdFn.length` 完成的。

**练习 2**：执行 `node bin/cm.js install --foo` 会发生什么？这条路径经过 `cmdFn.length` 校验吗？

**答案**：不经过——`install(arg = null)` 的 `length` 是 0（默认值参数不计数），任何数量的参数都能过 L34 的校验。然后 `arg` 为 `"--foo"`，在函数内部命中 [bin/cm.js:83](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L83) 的 `if (arg && arg != "--ssh") help(1)`，同样以 `help(1)` 收场。这说明选项级校验由实现函数负责。

**练习 3**：为什么映射表（L17）能引用定义在 L81 甚至 L352 的函数？

**答案**：因为它们都是函数声明（`function foo() {}`），会被提升到模块作用域顶部注册；而且映射表的求值发生在模块加载时，`start()` 的调用在文件最后一行（L366），真正查表时所有函数早已就绪。

### 4.2 help()：一份文本、两种退出码

#### 4.2.1 概念说明

`help()` 是 CLI 的「自描述界面」：用户第一次接触工具时读的就是它。它的设计要点是**一份文本服务两种出口**——

- 用户主动求助（`cm --help`）：这是正常流程，退出码应为 `0`；
- 用户用错了（未知命令、参数不足）：这是错误流程，退出码应为 `1`，但展示的用法文本是同一份。

所以 `help` 接收一个 `status` 参数，把它直接传给 `process.exit(status)`，一个函数同时覆盖两种语义。

#### 4.2.2 核心流程

```
调用 help(status)
   │
   ▼
console.log(用法模板字符串)   ← 写到 stdout
   │
   ▼
process.exit(status)          ← 0 = 正常求助，1 = 用法错误
```

模板字符串里每一行的格式是 `cm <命令> [选项]` 加两个空格缩进的说明，超过一定宽度就把说明折到下一行对齐（如 `devserver` 和 `release` 两行）。

#### 4.2.3 源码精读

[bin/cm.js:38-57](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L38-L57) —— `help()` 的全部：打印用法文本，然后以传入的退出码结束进程。

```js
function help(status) {
  console.log(`Usage:
  cm install [--ssh]      Clone and symlink the packages, install deps, build
  ...
  cm --help`)
  process.exit(status)
}
```

三个观察点：

1. **文本与映射表是手工同步的**。help 文本列出了 14 个命令，而映射表（L17-33）里有 15 项。逐一对照会发现：**`unreleased` 在映射表中存在（L23，实现函数在 [bin/cm.js:282-288](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L282-L288)，用于预览各包未发布的变更），但 help 文本没有列出它**。这是一个真实存在的「文档与代码脱节」样本——它提醒我们：读这类手写 CLI 时，映射表才是权威的命令清单，help 文本可能滞后。也正因如此，本讲的学习目标之一是「能对照 help() 文本找到每个子命令对应的实现函数」——并顺带学会用映射表反向核对。

2. **`process.exit(status)` 的两个调用方**：正常入口是映射表里的 `"--help": () => help(0)`（L32），错误入口是 L34 的 `help(1)`。`install` 内部校验失败（L83）、`release` 参数解析失败（L232-234）、`buildReadme` 收到核心包名（L347）时也都会调 `help(1)`——用法错误统一走这里。

3. **`console.log` 而非 `console.error`**：用法文本进 stdout。这样 `cm --help > usage.txt` 可以把它存档，而 `error()` 的报错走 stderr 不会被重定向捕获，两者各司其职。

#### 4.2.4 代码实践

1. **实践目标**：建立「help 文本 ↔ 映射表 ↔ 实现函数」三者的对照能力，并亲手找出文本遗漏的命令。
2. **操作步骤**：
   - 执行 `node bin/cm.js --help`（此命令在豁免列表中，未 install 也能跑）。
   - 把输出的每一行命令名抄下来，在 [bin/cm.js:17-33](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L17-L33) 的映射表里找到对应项，再顺着值跳到实现函数的行号，记成一张三列清单（命令 / 函数 / 行号）。4.1.3 节的表格就是这份作业的参考答案。
   - 对照完后回答：映射表里有哪个命令没出现在 help 文本里？
3. **需要观察的现象**：help 输出 14 个命令；映射表 15 项；差集是 `unreleased`。
4. **预期结果**：三列清单能对上 4.1.3 节的表格；`unreleased` 即遗漏项。**待本地验证**（本环境无法执行项目脚本）。
5. 附加思考（选做）：如果你来修这个脱节，是在 help 文本里补一行 `cm unreleased        Show unreleased changes per package`，还是接受「内部命令不上文档」？两种都有道理，后者可能正是作者的本意。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `help` 要接收 `status` 参数，而不是固定 `process.exit(0)`？

**答案**：同一份用法文本要服务两种出口——主动求助是正常路径（退出码 0），用法错误是失败路径（退出码 1，shell 脚本靠非零退出码感知失败）。参数化退出码让一个函数同时满足两者，避免复制整段模板字符串。

**练习 2**：`cm --help` 和 `cm`(无参数) 输出的文本完全相同，shell 里如何区分哪次是「成功」哪次是「失败」？

**答案**：看退出码：`node bin/cm.js --help; echo $?` 输出 0；`node bin/cm.js; echo $?` 输出 1。前者经由映射表的 `() => help(0)`，后者经由 L34 的 `help(1)`。

### 4.3 error() 与 run()：统一的错误出口与子进程封装

#### 4.3.1 概念说明

分发骨架之外，`cm.js` 还有两个贯穿全文件的小工具：

- **`error()`** 是程序自身失败的唯一出口：任何实现函数抛出的异常，最终都被 `start()` 的 `.catch(e => error(e))` 送来这里。它把错误对象打到 stderr 并以退出码 1 结束——比让 Node 打印一段原始未捕获异常栈要干净得多。
- **`run()`** 是所有子进程调用（`git`、`npm`、`rm`、`grep`……）的唯一封装。`cm.js` 作为「多仓库管家」，几乎每个命令的实质工作都是「切到某个包目录里执行一条子进程命令」，`run()` 把「在哪个目录执行、输出到哪、是否经过 shell」这些共性问题一次性收敛成四个默认值。

#### 4.3.2 核心流程

`error()` 的流程平凡：`console.error(err)` → `process.exit(1)`。

`run(cmd, args, wd = root, {shell = false, stdout = "pipe"} = {})` 的调用约定：

```
run(命令, 参数数组, 工作目录(默认仓库根), 选项(默认不走 shell、捕获 stdout))
        │
        ▼
child.execFileSync(cmd, args, {
   shell:  false            ← 不经过 shell，参数原样传递（无注入、无通配符展开）
   cwd:    wd               ← 子进程的工作目录：默认根目录，可切到某个包目录
   encoding: "utf8"         ← stdout 以字符串返回而不是 Buffer
   stdio: ["ignore", stdout, process.stderr]
        │      │            └ stderr 始终直通终端，错误实时可见
        │      └ stdout 由选项决定："pipe"=捕获为返回值 / "inherit"=直通终端
        └ stdin 被忽略，子进程不等待键盘输入
})
        │
        ▼
子进程退出码非零 → execFileSync 抛异常 → 被 start() 的 Promise 出口接住 → error()
子进程正常退出  → 返回 stdout 字符串（仅 stdout:"pipe" 时有意义）
```

#### 4.3.3 源码精读

[bin/cm.js:59-62](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L59-L62) —— `error()`：把错误对象写到 stderr，然后以退出码 1 结束进程。它只被 `start()` 的 `.catch` 调用（以及个别命令内部的 catch，如 `grep`），是全程序唯一的错误打印点。

[bin/cm.js:64-66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L64-L66) —— `run()`：基于 `execFileSync` 的子进程封装，用解构默认参数固化了 cwd、shell、stdout 三个默认行为。

```js
function run(cmd, args, wd = root, {shell = false, stdout = "pipe"} = {}) {
  return child.execFileSync(cmd, args, {shell, cwd: wd, encoding: "utf8", stdio: ["ignore", stdout, process.stderr]})
}
```

逐项解读它的设计：

- **`shell` 默认 `false`**：参数以数组形式直接传给子进程，不经 shell 解释。好处一是安全（参数里的特殊字符不可能被当成 shell 语法执行），好处二是行为可预测（`dist/*` 就是字面字符串，见 L90 对 `rm` 的调用——通配符展开由 `rm` 自己完成）。唯一开启 shell 的地方是 [bin/cm.js:99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L99)：Windows 上 `npm` 实为 `npm.cmd`，必须借助 shell 才能启动，所以那里传了 `{shell: process.platform == "win32", ...}`。
- **`wd` 默认 `root`**（L7 定义的仓库根目录）：调用方用第三个参数切到任意包目录，如 `run("git", ["status", "-sb"], pkg.dir)`。
- **`stdout` 的两种模式**是理解 `run()` 的钥匙，看两个真实消费者：
  - **捕获模式**（默认 `"pipe"`）：[bin/cm.js:110-116](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L110-L116) 的 `status()` 拿到 `git status -sb` 的输出字符串，和 `"## main...origin/main\n"` 做全等比较，只有「有变更或有新提交」时才打印——程序要**读**输出时用这种模式。
  - **直通模式**（`"inherit"`）：[bin/cm.js:88-89](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L88-L89) 的 `install()` 执行 `git fetch` / `git reset` 时传 `{stdout: "inherit"}`，让克隆、更新进度原样流到终端——程序不关心内容、只想让用户**看**到进度时用这种模式。
- **`stdio` 第三格 `process.stderr`**：子进程的 stderr 永远直通当前进程的 stderr，因此 `npm install` 的警告会实时出现在屏幕上，而不必等进程结束。
- **异常语义**：`execFileSync` 在子进程非零退出时抛异常。这一异常沿着实现函数冒泡，被 `start()` 的 Promise 包裹转成 rejection，最终到达 `error()`——「子进程失败」和「程序自己 throw」殊途同归。也有命令选择自己处理，例如 [bin/cm.js:327-331](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L327-L331) 的 `grep()` 用 `try/catch` 包住 `run("grep", ...)`（grep 没有匹配到内容时也返回非零退出码），catch 里直接 `process.exit(1)`。

把三个工具串起来看一遍完整链路：`cm status` → `start()` 查表得 `status`（length 0，无需参数）→ `status()` 循环对每个包调 `run("git", ["status","-sb"], pkg.dir)` 捕获输出 → 正常打印；若某个包目录里 `git` 失败，异常 → Promise reject → `error()` → stderr + 退出码 1。

#### 4.3.4 代码实践

1. **实践目标**：以 `status()` 为标本，读懂「run() 消费者」的典型写法，并体会 `assertInstalled` 守卫与错误出口如何配合。
2. **操作步骤**：
   - 精读 [bin/cm.js:110-116](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L110-L116) 的 `status()`（共 7 行），标出：哪一行调 `run()`、传了哪三个位置参数、用的是 stdout 的哪种模式、返回值被拿去和什么字符串比较。
   - 在**已执行过 `cm install`** 的环境中运行 `node bin/cm.js status`，观察哪些包被打印、哪些被静默（工作区干净的包不会出现）。
   - 在**未 install** 的环境中运行同一条命令，观察守卫输出。
3. **需要观察的现象**：已 install 环境——只有 `git status -sb` 输出不等于 `## main...origin/main\n` 的包被列出，格式为 `包名:` 加缩进的状态；未 install 环境——第一条输出就是 `module <包名> is missing. Did you forget to run 'cm install'?`，且不会出现任何 git 输出。
4. **预期结果**：与上一条描述一致；若把某个包目录改名后重跑 `status`，也会触发同样的守卫报错。**待本地验证**。
5. **提醒**：`status()` 是只读命令，不会修改任何仓库，适合反复实验。

#### 4.3.5 小练习与答案

**练习 1**：`run()` 里 `stdio` 的第三格为什么直接写 `process.stderr`，而不做成可配置的选项（像 stdout 那样）？

**答案**：因为存在「需要捕获 stdout」的真实场景（`status()` 要比较 git 输出、`changelog()` 要解析 git log），但**不存在**「需要捕获 stderr」的场景——stderr 是给人看的诊断信息，实时直通终端是最合理的行为，没必要为不存在的需求增加选项。这是一个「默认值设计只覆盖真实需求」的好例子。

**练习 2**：`cm.js` 里 `git clone` 的输出用 `{stdout: "inherit"}`，而 `git status -sb` 用默认的 `"pipe"`。如果两者反过来（clone 用 pipe、status 用 inherit），各会发生什么？

**答案**：clone 改用 pipe 后，克隆进度不会显示在终端（`run()` 的返回值也没有被使用），用户会面对长时间的黑屏等待，功能不坏但体验变差；status 改用 inherit 后，输出直接打到终端，`run()` 返回空字符串，`output != "## main...origin/main\n"` 恒为真，于是**所有包都会被打印**，「有变化才输出」的过滤彻底失效——这是功能性 bug，说明「选哪种 stdout 模式」取决于程序要不要消费输出。

**练习 3**：`grep()` 在 [bin/cm.js:327-331](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L327-L331) 自己 try/catch 了 `run()` 的异常，而不是放任它走到 `error()`。结合 grep 命令的语义，为什么这里值得特判？

**答案**：`grep` 在「没有任何匹配行」时也以非零码退出（这是 grep 自身的约定），这种情况对该命令来说不是程序错误。若不特判，一次无匹配就会打出一大段异常对象；特判后安静地以退出码 1 结束，符合 grep 类工具的使用直觉。

## 5. 综合实践：给 cm 新增一个 echo 命令

现在把本讲的全部知识串起来，完成规格中的实践任务：**在 `cm.js` 的映射表里新增一个 `echo` 命令，接收任意参数并原样打印，同时更新 `help()` 文本，并用 `node bin/cm.js echo hello` 验证。**

> 注意：这是学习性修改。本仓库的 CONTRIBUTING 明确不欢迎与议题无关的改动，练习完成后请用 `git checkout -- bin/cm.js` 还原，不要把 echo 命令提交上去。

### 5.1 实践目标

- 走通「注册命令 → 编写实现 → 更新帮助 → 验证行为」的完整闭环；
- 亲身体会「函数声明形式决定参数校验行为」：`echo` 要接收**任意数量**的参数，所以必须写成让 `length` 为 0 的形式。

### 5.2 操作步骤

1. **写实现函数**。在 [bin/cm.js:106-108](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L106-L108) 的 `listPackages` 附近添加（以下为**示例代码，非项目原有代码**）：

   ```js
   function echo(...args) {
     console.log(args.join(" "))
   }
   ```

   关键点：用 rest 参数 `...args` 而不是 `function echo(arg)`。rest 参数不计入 `length`，所以 `cm echo`（零参数）也能通过 L34 的校验；写成 `(arg)` 的话零参数会被 `help(1)` 拦下——对比 4.1.3 节表格里 `grep(pattern)` 与 `commit(...args)` 的差别。

2. **注册到映射表**。在 [bin/cm.js:17-33](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L17-L33) 的对象字面量里加一行，例如跟在 `packages: listPackages,` 之后：

   ```js
   packages: listPackages,
   echo,
   ```

   （`echo,` 是 `echo: echo` 的简写。）位置不影响功能——分发只查 key，不查顺序。

3. **更新帮助文本**。在 [bin/cm.js:39-55](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L39-L55) 的模板字符串里加一行（示例文本）：

   ```
   cm echo <args>          Echo the given arguments
   ```

4. **验证**。运行 `node bin/cm.js echo hello`。

### 5.3 需要观察的现象与预期结果

| 命令 | 预期输出 | 预期退出码 |
| --- | --- | --- |
| `node bin/cm.js echo hello` | `hello` | 0 |
| `node bin/cm.js echo a b c` | `a b c`（`join(" ")` 用空格连接） | 0 |
| `node bin/cm.js echo` | 空行（零参数也放行，因为 `length` 为 0） | 0 |
| `node bin/cm.js --help` | 用法文本中多出 `cm echo <args>` 一行 | 0 |

**一个必须预判的守卫问题**：`echo` 不在 L15 的豁免数组 `["install", "--help"]` 里，所以在**尚未执行 `cm install`** 的环境中，`node bin/cm.js echo hello` 会先被 `assertInstalled()` 拦下，输出 `module <包名> is missing. Did you forget to run 'cm install'?`。两条验证路线任选：

- **路线 A（推荐）**：先按 u1-l2 完成 `cm install`，再验证 echo——这也是日常开发的真实前置条件；
- **路线 B（只想快速看效果）**：临时把 L15 改成 `["install", "--help", "echo"].includes(command)`，验证通过后连同 echo 一起还原。这条路线本身就是对 4.1.3 第一段「豁免列表」的直接操练。

以上运行结果均为基于源码的推演，**待本地验证**。验证完成后执行 `git checkout -- bin/cm.js` 还原（若你改了多个文件，可先用 `git diff bin/cm.js` 确认改动范围再还原）。

### 5.4 思考延伸（选做）

- 如果希望 `cm echo` 零参数时打印用法并报错，实现函数该怎么声明？（答：`function echo(arg, ...rest)`，让 `length` 变为 1，L34 的校验自动生效——一行声明就是全部改动。）
- `echo` 的输出应该用 `console.log` 还是 `console.error`？（答：`console.log`——它是命令的正常结果，属于 stdout，用户可能用管道或重定向接走它。）

## 6. 本讲小结

- `start()` 用一张「命令名 → 函数」的对象映射表完成子命令分发，配合函数声明提升，实现函数可以散落在文件任意位置；`--help` 也只是表里的一项（值为箭头函数 `() => help(0)`），无需特判。
- 参数下限校验零成本地复用了函数的 `length` 属性：普通形参计入个数（`grep`、`build-readme`、`runCmd` 因此至少各需 1 个参数），默认值参数和 rest 参数不计入（`install`、`commit`、`test` 等因此允许零参数）；上限不校验，多出的参数被静默忽略。
- `new Promise(r => r(cmdFn.apply(null, args))).catch(e => error(e))` 是统一错误出口：同步 `throw`（如 `bumpVersion` 的 "No new release notes!"）和 `async` 函数的异步失败都汇入 `error()`——打到 stderr、退出码 1。
- `help(status)` 一份文本两种语义：`--help` 走退出码 0，用法错误走 1；help 文本与映射表靠人工同步，目前已脱节一项（`unreleased` 有实现、无文档），映射表才是权威清单。
- `run()` 以 `execFileSync` 为底座固化了四个默认：cwd 默认仓库根、默认不走 shell（仅 Windows 上的 npm 例外）、stdout 默认捕获为返回值（可切 `"inherit"` 直通终端）、stderr 永远直通；子进程非零退出抛出的异常同样汇入 `error()`。
- 除 `install` 与 `--help` 外的所有命令都要先过 `assertInstalled()` 包目录守卫——这就是给 CLI 加新命令时必须预判的运行前提。

## 7. 下一步学习建议

本讲搞定了「命令怎么被分发和执行」，下一讲 **u1-l4「看见编辑器：demo 应用与 dev 服务器初体验」**将沿着映射表里最常用的 `devserver` 命令往下走：`demo/demo.ts` 如何用 `EditorView` 搭建编辑器、`startServer()` 如何在 8090 端口提供页面。建议先做两件热身：

1. 预读 [bin/cm.js:125-160](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L125-L160) 的 `startServer()` 与 `devserver()`，找出它们调用的 `run` 之外的新依赖（`esmoduleserve`、`serve-static`、`http`）——注意这些都是**函数体内**惰性 require，呼应 u1-l2 讲过的「顶层零第三方依赖」原则。
2. 若想继续在分发骨架上练手，可以把 4.1.3 节的 length 对照表扩展成「每个命令的实现函数 + 主要依赖」速查表，后续单元（u2-l5 的多仓库工作流命令、u3-l2 的发布流程）都会反复用到它。
