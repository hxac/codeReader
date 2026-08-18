# 变更日志挖掘：用正则从 git log 提取分类变更

## 1. 本讲目标

CodeMirror 的三十多个包仓库每次发布新版本时，CHANGELOG 里的「Breaking changes / Bug fixes / New features」三个小节并不是人手写的，而是由脚本从提交信息里自动挖掘出来的。学完本讲，你应该能够：

1. 读懂 `changelog()` 如何用 `git log --format=%B%n --reverse` 拉取两个版本之间的提交信息，并理解这三个参数各自的必要性。
2. 逐个 token 拆解那条分段正则——特别是惰性量词 `*?` 与前瞻断言 `(?=...)` 如何配合划定「一个标记段落」的边界。
3. 说出 `{fix: [], feature: [], breaking: []}` 这个数据结构，以及 `releaseNotes()` 如何把它渲染成带日期、带文档锚点重写的 Markdown。

本讲只涉及一个源码文件 `bin/cm.js`，但它是整个发布流水线（u3-l2）的数据入口。

## 2. 前置知识

### 2.1 git log 的三个知识点

**（1）版本范围 `A..B`**。`git log 1.0.0..main` 表示「从 `main` 可达、但不从 `1.0.0` 可达的提交」，也就是「打完 `1.0.0` 标签之后新增的提交」。这里的 `1.0.0` 是标签名——在 CodeMirror 的包仓库里，**版本号字符串同时就是 git tag 名**（`doRelease()` 打 tag 时用的正是版本号，见 [bin/cm.js:266](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L266)）。

**（2）`--format=%B%n`**。`%B` 是「原始完整提交信息」（主题行 + 正文段落），`%n` 是一个换行符。git 还会在每个条目之间再加一个换行符，所以相邻两条提交信息之间会形成空行分隔。本讲写作时在本仓库实测，三条单行提交的输出字节形态是：

```
主题行\n\n\n主题行\n\n\n主题行\n\n\n
```

**（3）`--reverse`**。`git log` 默认最新提交在前；`--reverse` 把顺序反过来，变成时间正序（最旧在前）。这保证了后面挖出来的变更列表天然按时间排列。

### 2.2 正则的四个知识点

| 语法 | 名称 | 作用 |
| --- | --- | --- |
| `*?` | 惰性量词 | 尽量少匹配字符，遇到第一个能让整个模式成立的位置就停 |
| `(?=...)` | 前瞻断言 | 只检查「后面是不是这个样子」，本身不消耗字符 |
| `[^]` | 空补集技巧 | 「非空字符集」= 任意字符（**包括换行**），等价于 `[\s\S]`；`.` 默认不匹配换行，所以这里不能用 `.` |
| `g` 标志 + `exec` 循环 | 全局匹配 | `re.exec()` 每次从 `re.lastIndex` 继续，配合 `while` 循环扫出所有匹配 |

### 2.3 承接上一讲

u2-l5 已经讲过 `status()`、`push()` 等命令如何「把 git 的文本输出当机器数据解析」。本讲的 `changelog()` 是这个思路的登峰造极：不是解析一行状态，而是把整段提交历史切成结构化数据。同时它会复用 u1-l3 讲过的 `run()` 封装与 `error()` 统一错误出口。

## 3. 本讲源码地图

本讲全部源码集中在 `bin/cm.js` 一个文件里：

| 函数 | 行号 | 在本讲中的角色 |
| --- | --- | --- |
| `run()` | 64–66 | 子进程封装：changelog 的数据入口 |
| `error()` | 59–62 | git 命令失败时的兜底出口（u1-l3 已讲） |
| `changelog()` | 162–168 | **本讲主角**：挖提交、归三类 |
| `bumpVersion()` | 170–177 | 消费三类结果决定新版本号（u3-l2 详讲） |
| `releaseNotes()` | 179–193 | 把三类结果渲染成 Markdown |
| `doRelease()` | 248–269 | 发布主流程，前两者的调用方 |
| `unreleased()` | 282–288 | 「预览未发布变更」命令，第二个消费方 |

## 4. 核心概念与源码讲解

### 4.1 changelog()：把提交历史挖成三类变更

#### 4.1.1 概念说明

发布一个新版本前，维护者必须回答两个问题：**改了什么**（写进 CHANGELOG），以及**该升哪一位版本号**（语义化版本）。这两个问题的答案都藏在「上个版本标签到当前 main 之间的提交信息」里。

CodeMirror 的贡献约定是：在提交信息里另起一个段落，用大写关键字开头标记这个提交的类别——`FIX:`（修了 bug）、`FEATURE:`（新增功能）、`BREAKING:`（破坏性变更）。值得注意的是，这个约定**并没有写在 README 或 CONTRIBUTING 里**，`cm.js` 里那条正则本身就是事实上的规范。写错了标记（比如小写 `fix:`）不会有任何报错，变更只会静默丢失——这正是 `cm unreleased` 命令存在的价值：发布前先预览一遍挖掘结果，确认没有条目意外缺席。

`changelog(pkg, since)` 做的事，就是把这段自由文本历史变成一个机器友好的对象：

```
{fix: ["..."], feature: ["..."], breaking: ["..."]}
```

三个数组内的条目都按提交时间正序排列，后续的 `bumpVersion()` 数长度、`releaseNotes()` 按类别渲染，都只消费这个结构。

#### 4.1.2 核心流程

```
changelog(pkg, since)
  │
  │  git log --format=%B%n --reverse  since..main   （在包仓库目录里执行）
  ▼
一段大文本：所有提交信息按时间正序拼接，彼此以空行分隔
  │
  ▼
分段正则全局扫描：每命中一次 = 找到一个「空行之后、以
BREAKING:/FIX:/FEATURE: 开头的段落」，段落正文取到下一个空行为止
  │
  ▼
按关键字小写化归入 result.fix / result.feature / result.breaking
多行正文拍平成单行（换行替换为空格）
  │
  ▼
返回 {fix: [...], feature: [...], breaking: [...]}
```

伪代码（把正则细节抽象掉）：

```text
对 since..main 范围内每条提交信息（时间正序）：
    对信息里每个「以 BREAKING:/FIX:/FEATURE: 开头的独立段落」：
        把段落正文（不含关键字行）追加到对应类别的数组尾部
```

注意归类的粒度是**段落**而不是提交：一条提交信息里同时写了 `FIX:` 和 `FEATURE:` 两个段落，两个数组会各得一条。

#### 4.1.3 源码精读

完整的 `changelog()` 只有 6 行：

[bin/cm.js:162-168](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L162-L168) —— 在包仓库目录里按时间正序拉取 `since..main` 范围的提交信息，用分段正则把标记段落归入 fix/feature/breaking 三类。

```js
function changelog(pkg, since) {
  let commits = run("git", ["log", "--format=%B%n", "--reverse", since + "..main"], pkg.dir)
  let result = {fix: [], feature: [], breaking: []}
  let re = /\n\r?\n(BREAKING|FIX|FEATURE):\s*([^]*?)(?=\r?\n\r?\n|\r?\n?$)/g, match
  while (match = re.exec(commits)) result[match[1].toLowerCase()].push(match[2].replace(/\r?\n/g, " "))
  return result
}
```

**第一行：拉数据。** [bin/cm.js:163](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L163) —— `since` 是当前版本号字符串（同时也是 tag 名），`pkg.dir` 把工作目录切到对应包仓库，`run()` 返回捕获到的 stdout 字符串。三个 git 参数缺一不可：`--format=%B%n` 制造段落间的空行边界，`--reverse` 保证时间正序，`since..main` 圈定「未发布」的范围。

**第二行：结果容器。** [bin/cm.js:164](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L164) —— 三个键的初始化顺序是 `fix、feature、breaking`（大致按变更的日常频率排），这只会影响调试打印时的键序；`releaseNotes()` 渲染时用的是自己另一张表的顺序。

**第三行：那条正则。** [bin/cm.js:165](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L165) —— 逐段拆解如下：

| 片段 | 含义 |
| --- | --- |
| `\n\r?\n` | 匹配一个空行（段落边界）。`\r?` 是对 Windows 换行 `\r\n` 的容忍——`%B` 输出的是原始提交信息，换行风格取决于提交者的平台 |
| `(BREAKING\|FIX\|FEATURE):` | 捕获组 1：类别关键字。**区分大小写**，且必须大写；`FIXES:`、`fix:` 都不匹配 |
| `:\s*` | 冒号后的空白弹性伸缩：`FIX:x`（无空格）、`FIX: x`（一个空格）、甚至 `FIX:` 换行后再写正文，都能应付 |
| `([^]*?)` | 捕获组 2：段落正文。`[^]` 匹配包括换行在内的任意字符，惰性 `*?` 让它尽量短 |
| `(?=\r?\n\r?\n\|\r?\n?$)` | 前瞻断言：正文在「下一个空行」或「字符串末尾（允许一个结尾换行）」处停止 |

惰性量词与前瞻的配合是这条正则的灵魂，值得单独推演一遍。惰性 `*?` 从空串开始试探，每扩张一个字符就检查一次前瞻是否成立——**前瞻就是惰性匹配的停止条件**。没有它，`*?` 会永远满足于匹配空串。于是正文恰好覆盖到「下一个空行之前」的 所有字符：

- 若标记段落后面还有别的段落，前瞻由 `\r?\n\r?\n`（下一个空行）命中，正文在该段落末尾闭合；
- 若标记段落是整个输出的最后一段，`\r?\n?$`（末尾）兜底闭合。

由 `--format=%B%n` 的字节形态（段落后必然跟着空行）可知，实际几乎总是走第一个分支。另外注意 `$` 没有配 `m` 标志，它只匹配**整个字符串**的末尾，中间位置不可能误命中第二分支。

这个「空行 + 关键字」的前置条件同时也是一道**语义过滤器**：只有独立成段的标记才算数。正文里随口提到一句 "this fix: ..." 不会被误判，因为它前面没有空行、也不在段首。推论是：标记必须写在提交信息的**正文段落**里，主题行（第一行）直接写 `FIX: xxx` 且该提交恰好是范围内第一条时，将因前面没有空行而落选——不过按约定提交总要有一句主题行，实际不会发生。

**第四行：扫描循环。** [bin/cm.js:166](https://github.com/code.haverbeke.berlin-codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L166) —— `g` 标志让 `re.exec()` 每次从上次结束的 `lastIndex` 继续扫，`while` 循环就遍历出所有标记段落；`match[1].toLowerCase()` 把关键字映射成结果对象的键（`BREAKING` → `breaking`），`match[2].replace(/\r?\n/g, " ")` 把多行正文拍平成单行。一个细节：正则字面量声明在函数体内，每次调用都新建对象、`lastIndex` 从 0 开始；若把它提升成模块级常量，`g` 标志会让 `lastIndex` 跨调用残留，第二次调用就会漏匹配——这是 `g` 标志的经典陷阱。

#### 4.1.4 代码实践

**实践目标**：在一个与本项目无关的练习仓库里，亲手构造带 `FIX:`、`FEATURE:`、`BREAKING:` 段落的提交，再用从 `cm.js` 抄来的正则复刻 `changelog()`，验证三类分组正确、顺序保持时间正序、无标记提交被忽略。

**操作步骤**（以下 shell 命令均为示例，在 bash 下执行；`git init -b` 需要 git ≥ 2.28，旧版可先 `git init` 再 `git checkout -b main`）：

```bash
# 1. 建练习仓库
mkdir /tmp/cm-changelog-lab && cd /tmp/cm-changelog-lab
git init -q -b main
git config user.email lab@example.com
git config user.name "Lab User"

# 2. 基线提交 + 版本标签（版本号即 tag 名，模仿真实约定）
echo one > file.txt && git add file.txt
git commit -q -m "Initial commit"
git tag 1.0.0

# 3. 三类标记提交。用两个 -m 生成「主题行 + 空行 + 标记段落」的标准结构；
#    含反引号的正文务必用单引号或 $'...'，避免 bash 解释
echo two > file.txt && git add file.txt
git commit -q -m 'Add a foldGutter option' \
            -m 'FEATURE: new `foldGutter` option for [gutters](##view.gutter)'

echo three > file.txt && git add file.txt
git commit -q -m 'Fix crash on empty documents' \
            -m $'FIX: no longer throws when the document is empty\nwith a two-line explanation'

echo four > file.txt && git add file.txt
git commit -q -m 'Rework state creation' \
            -m 'BREAKING: `EditorState.create` now requires a `doc` argument'

# 4. 一个无标记提交 + 时间上更晚的第二个 FIX（用于观察数组内顺序）
echo five > file.txt && git add file.txt
git commit -q -m 'Touch something else' -m 'just some remarks, no marker here'

echo six > file.txt && git add file.txt
git commit -q -m 'Fix another crash' -m 'FIX: second fix, committed after the first one'
```

然后在 `/tmp/cm-changelog-lab/lab.js` 写入复刻脚本（**示例代码**，非本项目文件；正则与分组逻辑逐字抄自 [bin/cm.js:162-168](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L162-L168)）：

```js
// 示例代码：复用 bin/cm.js 中 changelog() 的正则与分组逻辑
const child = require("child_process")

function run(cmd, args, wd) {
  return child.execFileSync(cmd, args,
    {cwd: wd, encoding: "utf8", stdio: ["ignore", "pipe", process.stderr]})
}

function changelog(dir, since) {
  let commits = run("git", ["log", "--format=%B%n", "--reverse", since + "..main"], dir)
  let result = {fix: [], feature: [], breaking: []}
  let re = /\n\r?\n(BREAKING|FIX|FEATURE):\s*([^]*?)(?=\r?\n\r?\n|\r?\n?$)/g, match
  while (match = re.exec(commits)) result[match[1].toLowerCase()].push(match[2].replace(/\r?\n/g, " "))
  return result
}

console.log(JSON.stringify(changelog(__dirname, process.argv[2] || "1.0.0"), null, 2))
```

执行 `node lab.js 1.0.0`。

**需要观察的现象**：

1. 五条范围内提交只有四条被捕获——无标记的 "Touch something else" 不出现；
2. `FIX:` 段落的两行正文被拍平成一行（换行变空格）；
3. `fix` 数组内两条的先后顺序与提交时间一致（先 "no longer throws..."，后 "second fix..."）——这正是 `--reverse` 的效果；
4. `feature` 条目里的 `](##view.gutter)` **原样保留**——锚点重写不发生在这里，而发生在 `releaseNotes()`（见 4.3）。

**预期结果**（由源码逻辑推演，具体以本地运行为准，待本地验证）：

```json
{
  "fix": [
    "no longer throws when the document is empty with a two-line explanation",
    "second fix, committed after the first one"
  ],
  "feature": [
    "new `foldGutter` option for [gutters](##view.gutter)"
  ],
  "breaking": [
    "`EditorState.create` now requires a `doc` argument"
  ]
}
```

#### 4.1.5 小练习与答案

**练习 1**：把脚本里的 `--reverse` 参数删掉再运行，`fix` 数组会变成什么样？为什么？

**答案**：两条 fix 的顺序倒过来（"second fix..." 在前）。`git log` 默认最新提交在前，去掉 `--reverse` 后提交信息按时间倒序拼接，挖掘结果也随之倒序。这也说明 CHANGELOG 条目的时间正序完全依赖这一个参数。

**练习 2**：把正则里的 `([^]*?)` 改成 `(.*?)` 会发生什么？

**答案**：多行段落只能捕获第一行。`.` 默认不匹配换行符，惰性匹配会在第一行末尾就停住（前瞻 `\r?\n\r?\n` 在下一个空行处才成立，`.` 却走不过去，最终捕获不到任何内容或行为异常）。`[^]` 是「空补集 = 任意字符（含换行）」的技巧写法，等价于 `[\s\S]`。

**练习 3**：如果贡献者把标记写成了小写 `fix: 修复了空文档崩溃`，发布时会发生什么？

**答案**：什么也不发生——没有任何报错或警告，这条变更不会出现在 fix/feature/breaking 任何一类里，最终从 CHANGELOG 中静默消失。正则的关键字分支区分大小写。这就是为什么发布前要跑 `cm unreleased`（[bin/cm.js:282-288](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L282-L288)）人工预览一遍挖掘结果。

### 4.2 run()：让 git 成为可编程的数据源

#### 4.2.1 概念说明

`changelog()` 的数据入口是 `run()`——u1-l3 已经从「CLI 骨架」角度讲过它，本讲从「数据管道」角度再精读一次：它把一条 git 命令变成**一个可以喂给正则的字符串**。这个封装的品质决定了上层解析的可靠性：工作目录是否正确、参数是否会被 shell 曲解、stdout 是被捕获还是直通终端、子进程失败时异常往哪里去。

#### 4.2.2 核心流程

```
调用方（如 changelog）
  │  run("git", ["log", ...], pkg.dir)
  ▼
execFileSync("git", [...], {cwd: pkg.dir, stdio: [ignore, pipe, stderr]})
  │
  ├─ 子进程成功 → stdout 以 utf8 字符串返回给调用方
  └─ 子进程非零退出 → 抛出异常 → 沿同步调用栈上抛
        → 被 start() 的 Promise 包裹捕获 → error() 打印并退出码 1
```

#### 4.2.3 源码精读

[bin/cm.js:64-66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L64-L66) —— 以数组参数、无 shell 的方式同步执行子进程，默认捕获 stdout 返回字符串：

```js
function run(cmd, args, wd = root, {shell = false, stdout = "pipe"} = {}) {
  return child.execFileSync(cmd, args, {shell, cwd: wd, encoding: "utf8", stdio: ["ignore", stdout, process.stderr]})
}
```

对 `changelog()` 而言，四个设计点都不可或缺：

- **`cwd: wd`**：`changelog` 传入 `pkg.dir`，git 命令落在对应包仓库里。多仓库体系下这是 `run()` 与普通 `execFileSync` 封装最大的差异（u2-l5 的五个命令同样依赖它）。
- **数组参数、默认不走 shell**：提交信息里可能含有引号、反引号、`$` 等对 shell 有特殊含义的字符，逐项传参完全绕开转义与注入问题。
- **`encoding: "utf8"` + `stdout: "pipe"`**：返回值直接就是字符串，无需再手工 decode。想直通终端时传 `{stdout: "inherit"}`（`install()` 里的用法），但解析数据的调用方永远用默认值。
- **`stdio: ["ignore", stdout, process.stderr]`**：stdin 设为 ignore，git 不会因为等待输入而挂起脚本；stderr 直接继承当前进程的 stderr，git 的报错（比如 tag 不存在）原样打到终端，同时异常退出仍以异常形式抛给 JS。

**异常链路**：假设 `since` 传入的版本号在包仓库里没有对应 tag，`git log` 会非零退出，`execFileSync` 抛异常。`changelog()` 不捕获它，异常沿 `doRelease()` → `release()` 的同步调用栈上抛，最终被 [bin/cm.js:35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35) 的 `new Promise(...).catch(e => error(e))` 捕获，进入 [bin/cm.js:59-62](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L59-L62) 的 `error()`——stderr 打印、退出码 1。这正是 u1-l3 讲过的统一错误出口在真实场景中的落地。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `run()` 捕获到的原始字符串形态，理解正则面对的「原材料」长什么样。

**操作步骤**：在本仓库根目录（无需安装包仓库，只读操作）执行：

```bash
node -e 'const c = require("child_process"); \
  console.log(JSON.stringify(c.execFileSync("git", ["log", "--format=%B%n", "--reverse", "-2"], \
  {encoding: "utf8", stdio: ["ignore", "pipe", process.stderr]})))'
```

这条命令就是 `run()` 的最小复刻（**示例代码**）。

**需要观察的现象**：输出是一个 JSON 字符串，换行全部转义成 `\n`，可以逐个数清楚每条提交信息之间的换行个数。

**预期结果**（本讲写作时实测）：

```
"Bring back CI status badge in readme\n\n\nMake CI badge smaller\n\n\n"
```

每条提交信息（`%B` 自带一个结尾 `\n`）后面跟着 `%n` 的 `\n` 和 git 的条目分隔 `\n`，因此相邻信息之间是 `\n\n\n`（两个空行），而信息内部的「主题行 + 空行 + 标记段落」结构提供一个 `\n\n`（一个空行）——前者超出、后者正好命中正则的段落边界 `\n\r?\n`。看清楚这个字节级事实，4.1 的正则就不再神秘。

#### 4.2.5 小练习与答案

**练习 1**：为什么 stdin 要设成 `"ignore"` 而不是默认的 `"pipe"`？

**答案**：`execFileSync` 会等到子进程退出才返回。如果子进程试图读 stdin 而父进程没有提供数据也没有关闭管道，脚本就会永久挂起。`"ignore"` 相当于把子进程的 stdin 接到 `/dev/null`，读操作立即得到 EOF。

**练习 2**：为什么 stderr 直通终端（`process.stderr`）而不是也捕获回来？

**答案**：本仓库的脚本从不解析 stderr——它是给人看的诊断信息，实时直通即可；而 stdout 是数据（`git log` 的输出、`git status -sb` 的状态行），必须捕获。stdio 数组的第三项传一个流对象（`process.stderr`）即表示「继承这个流」。

**练习 3**：如果把 `git` 换成一个不存在的命令，异常会走到哪里？

**答案**：`execFileSync` 同步抛出异常，与子进程非零退出走完全相同的链路：`changelog()` 不拦，一路上抛到 `start()` 的 `.catch(error)`，`error()` 打印异常对象、进程以退出码 1 结束。CLI 的任何一条命令都不会因未捕获异常打印难看的堆栈。

### 4.3 releaseNotes()：分类结果如何变成 CHANGELOG

#### 4.3.1 概念说明

`changelog()` 产出的 `{fix, feature, breaking}` 是纯数据；`releaseNotes()` 负责把它渲染成最终贴进 `CHANGELOG.md` 的 Markdown 片段。它是一个**纯函数**：不读文件、不执行命令、不碰时间以外的任何外部状态，这使得它可以在正式发布之外的场景（`cm unreleased` 预览）里被安全复用。

渲染要处理三件事：日期抬头、小节排序、以及一个不显眼但关键的重写——提交信息里的文档链接 `[EditorView](##view.EditorView)` 是简写锚点，只有改成指向 `https://codemirror.net/docs/ref/#view.EditorView` 之后，在文档站上渲染出来的 README/CHANGELOG 里才能点击跳转。

#### 4.3.2 核心流程

```
changes = {fix, feature, breaking} + version
  │
  ├─ 生成日期字符串（年-月-日，月日补零）
  ├─ 按 breaking → fix → feature 的固定顺序遍历三个类别：
  │     空类别整节省略；非空则输出 "### Breaking changes" 等标题
  │     每条正文做锚点重写  ](##  →  ](https://codemirror.net/docs/ref/#
  ▼
返回 {head: "## <version> (<date>)\n\n", body: "### ...\n\n..."}
```

返回值拆成 `head` 与 `body` 两段是有意为之：三个消费方对两段的用法各不相同。

#### 4.3.3 源码精读

[bin/cm.js:179-193](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L179-L193) —— 把三类变更渲染成带日期抬头的 Markdown，并把文档简写锚点重写为 codemirror.net 的完整链接：

```js
function releaseNotes(changes, version) {
  let pad = n => n < 10 ? "0" + n : n
  let d = new Date, date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())

  let types = {breaking: "Breaking changes", fix: "Bug fixes", feature: "New features"}

  let refTarget = "https://codemirror.net/docs/ref/"
  let head = `## ${version} (${date})\n\n`, body = ""
  for (let type in types) {
    let messages = changes[type]
    if (messages.length) body += `### ${types[type]}\n\n`
    messages.forEach(message => body += message.replace(/\]\(##/g, "](" + refTarget + "#") + "\n\n")
  }
  return {head, body}
}
```

逐点说明：

- **日期**（[第 180–181 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L180-L181)）：`pad` 把月、日补成两位，产出 `2026-08-18` 这样的抬头日期。注意 `getMonth()` 从 0 计数，所以要 `+1`。
- **小节顺序**（[第 183 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L183)）：`types` 表的插入顺序是 breaking、fix、feature，`for...in` 对字符串键按插入顺序遍历，于是 CHANGELOG 里破坏性变更永远排在最前面——读者最先看到的正是升级最需要警惕的内容。
- **空类别省略**（[第 189 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L189)）：`if (messages.length)` 守卫，没有变更的类别连标题都不输出，CHANGELOG 不会出现空小节。
- **锚点重写**（[第 190 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L190)）：`/\]\(##/g` 把 Markdown 链接目标里的 `](##` 换成 `](https://codemirror.net/docs/ref/#`。贡献者在提交信息里只需写 `[gutters](##view.gutter)`，渲染后自动变成指向参考文档对应锚点的完整链接。
- **head/body 拆分**（[第 186、192 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L186-L192)）：抬头（版本号 + 日期）与正文分离，方便消费方各取所需。

**两个调用方**如何消费这个结果：

- [bin/cm.js:253、258、262](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L248-L269) —— `doRelease()` 里 `changelog(pkg, currentVersion)` 挖数据、`releaseNotes(changes, newVersion)` 渲染，然后 `notes.head + notes.body` **拼接在旧 CHANGELOG 内容之前**写回文件（新版本永远在最上面）；此外 `notes.body` 还会被写进 git tag 的注释（[第 266 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L266)）。
- [bin/cm.js:282-288](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L282-L288) —— `unreleased()` 对 36 个包逐个跑 `changelog(pkg, 当前版本)`，三类皆空就保持静默，否则只打印 `releaseNotes(changes, ver).body`——**只用 body，不要 head**，因为预览的不是一次具体发布，日期与版本号尚不存在。

另外，`bumpVersion()`（[bin/cm.js:170-177](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L170-L177)）消费的是同一个结构——数三类数组的长度决定升哪一位版本号，这属于 u3-l2 的主题，此处只交代衔接关系。

#### 4.3.4 代码实践

**实践目标**：给 4.1.4 练习仓库挖出的数据套上 `releaseNotes()`，得到一段可以直接贴进 CHANGELOG 的 Markdown，并对照锚点重写效果。

**操作步骤**：在 `/tmp/cm-changelog-lab/lab.js` 末尾追加（**示例代码**，逐字抄自 [bin/cm.js:179-193](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L179-L193)，`refTarget` 保持原值）：

```js
function releaseNotes(changes, version) {
  let pad = n => n < 10 ? "0" + n : n
  let d = new Date, date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
  let types = {breaking: "Breaking changes", fix: "Bug fixes", feature: "New features"}
  let refTarget = "https://codemirror.net/docs/ref/"
  let head = `## ${version} (${date})\n\n`, body = ""
  for (let type in types) {
    let messages = changes[type]
    if (messages.length) body += `### ${types[type]}\n\n`
    messages.forEach(message => body += message.replace(/\]\(##/g, "](" + refTarget + "#") + "\n\n")
  }
  return {head, body}
}

let changes = changelog(__dirname, "1.0.0")
let notes = releaseNotes(changes, "2.0.0")   // 版本号从哪来？见下方思考题
console.log(notes.head + notes.body)
```

执行 `node lab.js`。

**需要观察的现象**：小节顺序是 Breaking changes → Bug fixes → New features；`[gutters](##view.gutter)` 被改写为完整链接；日期是运行当天。

**预期结果**（由源码逻辑推演，待本地验证；日期以实际运行日为准）：

```markdown
## 2.0.0 (2026-08-18)

### Breaking changes

`EditorState.create` now requires a `doc` argument

### Bug fixes

no longer throws when the document is empty with a two-line explanation

second fix, committed after the first one

### New features

new `foldGutter` option for [gutters](https://codemirror.net/docs/ref/#view.gutter)
```

思考题：脚本里为什么写 `2.0.0`？因为 `bumpVersion("1.0.0", changes)` 查到 `changes.breaking.length` 非零，按「有 breaking → major + 1」的规则推出 `2.0.0`（规则全表见 u3-l2）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `for (let type in types)` 能保证小节顺序稳定？

**答案**：ES2015 起规范保证对象字符串键按**插入顺序**枚举（整数键除外）。`types` 字面量里 breaking 写在最前，所以渲染顺序固定为 Breaking changes → Bug fixes → New features，与 `changes` 对象本身的键序无关。

**练习 2**：`](##` 重写为什么放在 `releaseNotes()` 而不是 `changelog()` 里？

**答案**：职责分离——`changelog()` 产出**保真的原始数据**（正文原样保留，`unreleased()` 预览时看到的就是贡献者写的原文），`releaseNotes()` 负责**面向展示的渲染**（锚点只在最终渲染到 codemirror.net 文档体系时才需要展开）。如果提前在挖掘阶段重写，数据就被展示格式污染了。

**练习 3**：`doRelease()` 写 CHANGELOG 时为什么是 `notes.head + notes.body + 旧内容` 而不是追加到末尾？

**答案**：CHANGELOG.md 按惯例**新版本在最上**，读者打开文件先看到最近的变化；旧内容作为后缀原样保留，历史逐版向下排。

## 5. 综合实践

把本讲三个模块串成一条完整的数据流水线：**提交信息 → changelog() 挖掘 → bumpVersion() 推演版本 → releaseNotes() 渲染 → CHANGELOG 片段**。

在 4.1.4 的练习仓库（tag `1.0.0`，含 1 条 breaking、2 条 fix、1 条 feature）基础上完成：

1. **预演版本号**：对照 [bin/cm.js:170-177](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L170-L177) 的规则表，手推 `bumpVersion("1.0.0", changes)` 的返回值（应为 `2.0.0`）；再在 `lab.js` 里把这条函数抄进去验证手推结果。规则速查：

   | 当前版本 | changes | 新版本 |
   | --- | --- | --- |
   | `0.x.y` | 有 breaking | `0.(x+1).0` |
   | `0.x.y` | 无 breaking | `0.x.(y+1)` |
   | `x.y.z`（x≥1） | 有 breaking | `(x+1).0.0` |
   | `x.y.z`（x≥1） | 无 breaking 有 feature | `x.(y+1).0` |
   | `x.y.z`（x≥1） | 仅有 fix | `x.y.(z+1)` |
   | 任意 | 三类皆空 | 抛错 `No new release notes!` |

2. **完整渲染**：运行 4.3.4 的脚本得到 Markdown 片段，将其保存为 `CHANGELOG.md`（练习仓库内）。
3. **反向验证**：给练习仓库再打一个 tag（`git tag 2.0.0`）后执行 `node lab.js 2.0.0`，确认输出变成三个空数组——范围内没有新提交，`changelog` 挖不到任何东西。这正是 `unreleased()` 对「已全部发布的包」保持静默的原理。
4. **边界实验**：再提交一条主题行直接以 `fix:` 小写开头的提交，重跑 `node lab.js 2.0.0`，确认它不出现在任何类别里——体会「标记写错 = 静默丢失」的后果，理解 `cm unreleased` 预览环节的必要性。

全部步骤均为离线练习，不触碰本仓库的任何文件。

## 6. 本讲小结

- `changelog()` 用 `git log --format=%B%n --reverse since..main` 拉取「上次发布以来」的提交信息：`%B%n` 制造空行边界、`--reverse` 保证时间正序、`since..main` 圈定范围（版本号字符串即 tag 名）。
- 分段正则 `\n\r?\n(BREAKING|FIX|FEATURE):\s*([^]*?)(?=\r?\n\r?\n|\r?\n?$)` 的灵魂是**惰性量词 + 前瞻断言**：前瞻给出停止条件，正文恰好覆盖到下一个空行之前；`[^]` 让匹配可以跨行；关键字区分大小写、必须独立成段。
- 产出结构 `{fix: [], feature: [], breaking: []}`：按段落（而非提交）归类，多行正文拍平为单行，数组保持时间正序。
- `run()` 以数组参数、无 shell 的 `execFileSync` 把 git 变成可编程数据源：cwd 定位包仓库、stdout 捕获为 utf8 字符串、stderr 直通终端、失败时异常沿同步调用栈直达 `error()` 统一出口。
- `releaseNotes()` 是纯函数，按 breaking → fix → feature 固定顺序渲染小节、空类别省略、把 `](##` 简写锚点重写为 codemirror.net 参考文档链接；`doRelease` 用它的 head+body 前插 CHANGELOG 并写进 tag 注释，`unreleased` 只用 body 做发布前预览。
- 提交标记约定没有文档记载，正则本身就是规范——写错标记不报错、只会静默丢失，所以发布流程里 `cm unreleased` 的人工预览不可省略。

## 7. 下一步学习建议

本讲产出并渲染了三类变更数据，但刻意把「版本号怎么升、CHANGELOG 怎么提交、tag 怎么打」留给了下一讲 **u3-l2《发布流程：语义化版本与 CHANGELOG 自动生成》**——它将贯通 `release()` → `doRelease()` → `bumpVersion()`/`releaseNotes()`/`editReleaseNotes()` 的完整链路，包括 `--edit` 调用外部编辑器人工修订、以及被暂时停用的 `updateDependencyVersion()` 依赖联动。阅读源码时建议按此顺序推进：

1. [bin/cm.js:225-246](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L225-L246)（`release()` 参数解析与拉取远端）；
2. [bin/cm.js:248-269](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L248-L269)（`doRelease()` 主流程，本讲两个函数的汇合点）；
3. [bin/cm.js:271-280](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L271-L280)（`editReleaseNotes()` 与 `unreleased()`）。

有余力的读者还可以去任一 `@codemirror/*` 包仓库（`cm install` 克隆后即在本仓库根目录下）打开它的 `CHANGELOG.md`，对照本讲的渲染逻辑逐段还原每一条记录当初的提交信息形态。
