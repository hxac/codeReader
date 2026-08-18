# 多仓库日常：status/commit/push/run/grep 协同

## 1. 本讲目标

CodeMirror 的编辑器本体分散在 36 个独立的包仓库里（见 u2-l1 的包注册表），日常开发中最频繁的痛点不是写代码，而是「同时对 36 个仓库做同一件事」：看看哪些仓库有未提交的改动、把一批修复合并提交、把有新提交的仓库推送出去、在所有仓库里搜一个符号、或在每个仓库里跑一条命令。本讲精读 `bin/cm.js` 中为此设计的一组工作流命令，学完后你应当能够：

1. 说出 `status()`、`commit()`、`push()`、`runCmd()`、`grep()` 五个命令共同的实现模式——「以 `packages` 数组为中心的循环 + `run()` 捕获输出 + 字符串谓词过滤」。
2. 理解两种从 `git status -sb` 的纯文本输出中判断状态的技巧：精确字符串比较（status）与带词边界的正则匹配（push），并知道它们各自的适用边界。
3. 会用 `cm grep` 在全部包源码中定位符号，理解它如何收集文件、为何对 `legacy-modes` 走特殊分支，以及文件名里多一个点号会带来的小陷阱。
4. 会用 `cm run` 逐包执行任意命令，并预判它「遇错即停」的行为。

## 2. 前置知识

本讲建立在前几讲已确认的事实之上，这里只做简短回顾，细节请回看对应讲义：

- **包注册表（u2-l1）**：`bin/packages.js` 导出 `loadPackages()`，返回三个视图——`packages`（全部 36 个 `Pkg` 实例）、`packageNames`（按名索引）、`buildPackages`（`main` 非空的 35 个，排除没有 TS 入口的 `legacy-modes`）。`Pkg.dir` 恒为仓库根目录下与包同名的子目录。
- **子进程封装 `run()`（u1-l2 / u1-l3）**：基于 `execFileSync`，`cwd` 参数指定工作目录（默认仓库根），`stdout` 默认 `"pipe"`（输出被捕获并以字符串返回，因为 `encoding: "utf8"`），也可传 `"inherit"` 直通终端；`stderr` 恒为 `process.stderr` 直通。子进程以非零码退出时 `execFileSync` 会抛异常。
- **命令分发（u1-l3）**：`start()` 用「命令名 → 函数」映射表分发，参数下限校验复用函数的 `length` 属性（rest 参数不计入），上限不校验——这一点在给 `grep` 加选项的实践里会用到。
- **git 基础**：`git status -sb` 是「短格式 + 分支头」的状态输出。干净且与远端同步的仓库只输出一行分支信息；有改动时追加 ` M 文件`、`?? 文件` 等行；本地领先远端时分支行变成 `## main...origin/main [ahead 1]`。本讲的命令全部把这类**人类可读的文本输出当作机器数据来解析**——这是理解它们设计取舍的钥匙。

另外一个环境事实：这些命令都要求包目录已经克隆到本地（除 `install` 与 `--help` 外的命令都要先通过 `assertInstalled()` 守卫）。仓库根的 `.gitignore` 把 `/state`、`/view`、`/lang-javascript` 等全部包目录列为忽略项——所以克隆完成后 `git status` 仍是干净的，36 个仓库「藏」在中央仓库根目录之下，靠 npm workspaces 与 tsconfig paths（u2-l2）拼成一个逻辑 monorepo。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js) | CLI 入口，全部子命令实现 | `status()`、`commit()`、`push()`、`grep()`、`runCmd()`，以及被它们复用的 `run()` 与 `start()` 分发 |
| [bin/packages.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js) | 唯一的包清单数据源 | `loadPackages()` 返回的 `packages` 数组——五个工作流命令共同的遍历对象 |

这组命令的完整清单也记录在帮助文本里：[bin/cm.js:50-54](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L50-L54) 依次列出 `cm commit`、`cm push`、`cm run`、`cm grep`（`cm status` 在第 42 行）。注意帮助文本与映射表是手工同步的（u1-l3 的结论），映射表才是权威清单。

## 4. 核心概念与源码讲解

先给出全组命令共享的骨架，再逐个模块展开。伪代码：

```text
for pkg in packages:              # 遍历全部 36 个包（注意不是 buildPackages）
    out = run(<git 或其他命令>, args, cwd=pkg.dir)   # stdout 被 pipe 捕获成字符串
    if 谓词(out):                 # 字符串比较 / 真值判断 / 正则 test
        执行动作（打印、git commit、git push、执行命令）
```

五个命令对这三步做了不同的填充，对比如下表：

| 命令 | 判定依据命令 | 谓词 | 动作 |
| --- | --- | --- | --- |
| `status()` | `git status -sb` | `output != "## main...origin/main\n"` | 打印包名 + 原始输出 |
| `commit()` | `git diff` 与 `git diff --cached` | 两者输出任一非空（真值） | 在该包执行 `git commit <用户参数>` 并打印结果 |
| `push()` | `git status -sb` | `/\bahead\b/.test(output)` | 在该包执行 `git push <用户参数>`（结果不打印） |
| `grep()` | 无（先收集文件） | 扩展名精确匹配 | 一次性对所有文件运行系统 `grep` |
| `runCmd()` | 无 | 无（无条件执行） | 在该包执行任意命令并打印输出 |

### 4.1 status()：『有变化才输出』的字符串比较过滤

#### 4.1.1 概念说明

对 36 个仓库逐个跑 `git status` 会刷出一屏无用信息——绝大多数仓库在绝大多数时刻都是干净的。`status()` 解决的问题是**降噪**：只展示「有意思」的仓库。它判定「有意思」的方式出奇地简单粗暴：把 `git status -sb` 的输出与「干净且同步」时的标准输出做**全等比较**，不相等就算有意思。

#### 4.1.2 核心流程

1. 遍历 `packages`（全部 36 个包，包含 `legacy-modes`）。
2. 在每个 `pkg.dir` 里执行 `git status -sb`，用 `run()` 默认的 `pipe` 模式把 stdout 捕获为字符串。
3. 若输出**恰好等于** `"## main...origin/main\n"`（只有一个分支行、无任何文件行、无 ahead/behind 标记），视为「没意思」，跳过。
4. 否则打印 `包名:` 与原始输出。

形式化地说，谓词是 \( P(o) = o \neq o_{\text{clean}} \)，其中 \( o_{\text{clean}} \) 是「main 分支、有 origin/main 上游、工作区与暂存区均干净」这一种且仅这一种状态的输出。

#### 4.1.3 源码精读

[bin/cm.js:110-116](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L110-L116) —— `status()` 全文只有 6 行：

```js
function status() {
  for (let pkg of packages) {
    let output = run("git", ["status", "-sb"], pkg.dir)
    if (output != "## main...origin/main\n")
      console.log(`${pkg.name}:\n${output}`)
  }
}
```

几个值得咀嚼的细节：

- `run()` 不传第四参，`stdout` 走默认 `"pipe"`，返回值就是捕获的文本（[bin/cm.js:64-66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L64-L66)）。与前几讲里 `install()` 大量使用 `{stdout: "inherit"}` 直通终端形成对照：**要给人看进度就用 inherit，要当数据用就 pipe**。
- 比较串里的换行不可省略：`git status -sb` 的输出以 `\n` 结尾，少了它永远不相等，过滤器就失效成「全部输出」。
- 全等比较的隐含约定：分支必须叫 `main` 且上游必须是 `origin/main`。这个约定由 `install()` 兜底——它克隆时就是 main 分支并 `reset --hard FETCH_HEAD`（u1-l2）。若你在某个包里切到别的分支，即便工作区干净，输出也会变成 `## 别的分支`，于是被判定为「有意思」——这通常正是你想要的提醒。
- 触发「有意思」的典型情形：工作区修改（` M src/...`）、未跟踪文件（`?? ...`）、已暂存修改（`M  src/...`）、领先/落后远端（`[ahead 1]`、`[behind 2]`）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「精确比较」过滤的行为边界。
2. **操作步骤**（要求本地已完成 `cm install`；本分析环境未克隆包目录，**待本地验证**）：
   - 进入任一包目录手动运行 `git status -sb`，把输出与源码里的比较串逐字符对照（注意行尾）。
   - 挑一个包（如 `state`），在其 `src/` 下任意文件末尾加一行注释，回到仓库根运行 `node bin/cm.js status`。
   - 用 `git checkout -- <文件>` 还原，再次运行 `cm status`。
3. **需要观察的现象**：改动后只有 `state` 一个包的段落出现；还原后命令无任何输出（退出码为 0）。
4. **预期结果**：与 4.1.2 的谓词推导一致——一个包的输出恢复正常后即从结果中消失。

#### 4.1.5 小练习与答案

**练习 1**：如果某包处于 detached HEAD 状态（`git status -sb` 输出 `## HEAD (no branch)`），`cm status` 会显示它吗？

答案：会。输出不等于比较串，谓词为真，该包被打印。这正是全等比较的「保守」优点：任何偏离基线的状态都会浮出水面。

**练习 2**：为什么 `status()` 遍历 `packages` 而不是 `buildPackages`？

答案：`buildPackages` 只含 `main` 非空的 35 个包，排除 `legacy-modes`；而 `legacy-modes` 同样是一个会修改、需要提交推送的 git 仓库。状态类命令关心「仓库」而非「可构建性」，所以用全量 `packages`。

**练习 3**：把比较改成正则 `/^## main\.\.\.origin\/main\n$/.test(output)` 会更严谨吗？

答案：不会更好，反而等价或更脆弱。当前输出只有一个分支行加结尾换行，全等比较已经是最精确的形式；引入正则只是把同样的约束换了写法，还多一层转义出错的风险。（`$` 在带 `\n` 结尾的字符串上配合 `m` 标志与否还有细微差别，徒增心智负担。）

### 4.2 commit() 与 push()：变更检测驱动的批量 git 操作

#### 4.2.1 概念说明

改完一批代码后，你可能同时动了三四个包仓库。`commit()` 让你**只对有改动的包**执行同一条 `git commit`，省去逐个 `cd` 的麻烦；`push()` 则只把**有新提交（领先远端）**的包推送出去。两者是同一模式的两种实例化：先用一条 git 查询命令的输出判断「这个包需要动吗」，需要才执行真正的写操作。

#### 4.2.2 核心流程

`commit()`：

1. 遍历 `packages`。
2. 依次捕获 `git diff`（未暂存改动）与 `git diff --cached`（已暂存改动）的输出。
3. 任一非空（JavaScript 中空字符串为假值）→ 在该包目录执行 `git commit` 拼上用户传入的全部参数，并打印 git 的输出。
4. 两者皆空 → 什么都不做，连包名都不打印。

`push()`：

1. 遍历 `packages`。
2. 捕获 `git status -sb`，用 `/\bahead\b/` 测试输出。
3. 命中（分支行含 `[ahead N]`）→ 执行 `git push` 拼上用户参数；返回值被丢弃，stdout 不展示。

注意两者的判定口径不同：`commit` 看**工作区/暂存区差异**，`push` 看**本地提交与远端的领先关系**。一个包可以「已全部提交但未推送」（commit 跳过、push 命中），也可以「有未提交改动但无新提交」（commit 命中、push 跳过）——两次过滤是正交的。

#### 4.2.3 源码精读

[bin/cm.js:295-300](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L295-L300) —— `commit()`：

```js
function commit(...args) {
  for (let pkg of packages) {
    if (run("git", ["diff"], pkg.dir) || run("git", ["diff", "--cached"], pkg.dir))
      console.log(pkg.name + ":\n" + run("git", ["commit"].concat(args), pkg.dir))
  }
}
```

[bin/cm.js:302-307](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L302-L307) —— `push()`：

```js
function push(...args) {
  for (let pkg of packages) {
    if (/\bahead\b/.test(run("git", ["status", "-sb"], pkg.dir)))
      run("git", ["push", ...args], pkg.dir)
  }
}
```

值得注意的细节：

- **真值判断当谓词**：`git diff` 无改动时输出空串（假值），有改动输出 diff 文本（真值）。`||` 短路——有未暂存改动时连第二条 `git diff --cached` 都不执行。
- **未跟踪文件是盲区**：`?? 文件` 只出现在 `git status` 里，`git diff` 与 `git diff --cached` 都看不到。只有未跟踪新文件的包会被 `commit` 跳过，但会被 `status` 显示——两个命令在这里形成互补，日常流程通常是先 `cm status` 查看，再手动处理新文件。
- **参数透传的两种写法**：`["commit"].concat(args)` 与 `["push", ...args]` 等价，是同一意图的两种风格。参数来自 `process.argv.slice(3)`（u1-l3），`cm commit -m "Fix x"` 传进来是三个独立数组元素，`execFileSync` 逐元素传参、不走 shell，因此**没有 shell 引号转义问题**——这是 `run()` 默认 `shell: false` 的直接收益。
- **`\bahead\b` 的词边界**：`\b` 保证匹配独立的单词 `ahead`，不会误伤其他单词的子串。分支行 `[ahead 1]` 命中；`[behind 2]` 不含 ahead，不命中（落后时推送本来也无意义）；`[ahead 1, behind 2]`（分叉）命中——此时 `git push` 会被远端拒绝，但那属于 git 自身的报错，会经 `stderr` 直通显示并抛异常进入 `error()`。
- **push 的静默成功**：`run(...)` 的返回值没有被使用，stdout 默认被捕获后直接丢弃。推送成功时你什么也看不到；只有 git 写 stderr（如推送进度、错误）时才有输出——因为 `run()` 的 `stdio` 第三项恒为 `process.stderr`（u1-l3）。
- **失败即中断**：某包的 `git commit` 非零退出（例如 `-m` 缺失）会抛异常，沿 `start()` 的 Promise 捕获进入 `error()`，整个循环终止（u1-l3 的统一错误出口）。

#### 4.2.4 代码实践

1. **实践目标**：在不产生真实提交的前提下验证 `commit()` 的过滤与参数透传。
2. **操作步骤**（需本地环境，**待本地验证**）：
   - 挑一个包加一行注释制造未暂存改动，运行 `node bin/cm.js status` 确认只有它出现。
   - 运行 `node bin/cm.js commit --dry-run -m "practice"`。`--dry-run` 是透传给 `git commit` 的标准选项，只显示将要提交什么，不落盘。
   - 还原改动后再次运行同一条命令。
3. **需要观察的现象**：第一次只有目标包打印出 `git commit --dry-run` 的摘要；第二次命令整体无输出。
4. **预期结果**：与 4.2.2 的口径分析一致——`--dry-run` 也走 `git diff` 判定，未改动的包根本不会执行 commit。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `push()` 判定用 `git status -sb` 而不是 `git log origin/main..main` 之类的提交列表？

答案：`status` 已经在 `status()` 里被用作数据源，复用同一命令的输出可以少记一种输出格式；且 `[ahead N]` 标记正是「本地有远端没有的提交」的直接信号。用词边界正则一行搞定，无需解析提交列表——这是「把 git 当字符串 API 用」这一仓库风格的典型取舍。

**练习 2**：`cm push --tags` 会发生什么？

答案：`--tags` 经 `...args` 拼进 `git push --tags`，只在**领先远端**的包里执行。想给所有包推 tag 时要意识到这个过滤的存在——无新提交但有本地 tag 的包不会被推。

**练习 3**：一个包既有未暂存改动、又有已暂存改动、还有未跟踪文件，`cm commit -m x` 之后 `cm status` 还会显示它吗？

答案：会。`git commit` 只提交暂存区内容，未暂存改动与未跟踪文件仍在，`git status -sb` 输出依然异于比较串。理解「commit 收口的是暂存区」这一 git 语义是预判这些命令行为的前提。

### 4.3 grep()：跨包文件收集与 legacy-modes 特例

#### 4.3.1 概念说明

想在全部包源码里找一个符号（比如 `EditorView` 在哪些包被引用），普通 `grep -r` 会被 `node_modules`、`dist` 产物淹没。`grep()` 的做法是**先按注册表精确圈定搜索范围，再一次性调用系统 grep**：每个包只取 `src/` 与 `test/` 下的 TypeScript 文件，外加 demo 的入口文件。`legacy-modes` 是唯一没有 TS 源码的包（u2-l1 里 `Pkg.main` 因此保持 null），所以给它开了专搜 `mode/` 目录下 `.js`/`.d.ts` 的特殊分支。

#### 4.3.2 核心流程

1. 文件清单从 `demo/demo.ts` 起步（中央仓库自己的一份源码）。
2. 对每个包：`legacy-modes` → 收集 `mode/` 下的 `.js` 与 `.d.ts`；其余包 → 收集 `src/` 与 `test/` 下的 `.ts`。
3. 收集动作由内部函数 `add(dir, ext)` 完成：`readdirSync` 读目录（读不到就静默返回——容忍尚未克隆或没有 `test/` 的包），对每个文件名提取「从第一个点号开始的后缀」，与扩展名白名单做**数组精确成员判断**。
4. 把模式与全部文件一次性交给系统 `grep --color -nH -e <pattern> <files...>`，工作目录是当前工作目录；捕获的输出整体打印。
5. grep 非零退出（典型情形：无匹配）→ `execFileSync` 抛异常 → 捕获后 `process.exit(1)`。

#### 4.3.3 源码精读

[bin/cm.js:309-332](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L309-L332) —— `grep()` 全文：

```js
function grep(pattern) {
  let files = [join(root, "demo", "demo.ts")]
  function add(dir, ext) {
    let list
    try { list = fs.readdirSync(dir) }
    catch (_) { return }
    for (let f of list) if (ext.includes(/^[^.]*(.*)/.exec(f)[1])) {
      files.push(path.relative(process.cwd(), join(dir, f)))
    }
  }
  for (let pkg of packages) {
    if (pkg.name == "legacy-modes") {
      add(join(pkg.dir, "mode"), [".js", ".d.ts"])
    } else {
      add(join(pkg.dir, "src"), [".ts"])
      add(join(pkg.dir, "test"), [".ts"])
    }
  }
  try {
    console.log(run("grep", ["--color", "-nH", "-e", pattern].concat(files), process.cwd()))
  } catch(e) {
    process.exit(1)
  }
}
```

逐点拆解：

- **后缀提取正则** `/^[^.]*(.*)/`：`[^.]*` 贪婪匹配到第一个点号为止，捕获组拿到「从第一个点号到结尾」的整段。对 `index.ts` 得到 `".ts"`；对 `foo.d.ts` 得到 `".d.ts"`——这正是 legacy-modes 的白名单里必须写 `.d.ts` 而不能只写 `.ts` 的原因。推论：**文件名里含多个点号的文件（提取结果是多段后缀）不会出现在 `.ts` 白名单的命中里**。这个取舍让实现只需一行正则，代价是命名风格受限——各包源码文件名恰好都不带多点，约定即接口。
- **`ext.includes(...)` 是数组方法**：做的是精确成员判断，不是子串判断（子串判断是 `String.prototype.includes`）。`".ts"` 不会匹配白名单里的 `".d.ts"`。
- **容错的 try/catch**：目录不存在时 `readdirSync` 抛错，`catch` 后直接返回——尚未克隆的包不会让 `cm grep` 崩溃（尽管正常流程下 `assertInstalled()` 已挡住这种情况，防御仍有价值：克隆了但确无 `test/` 的包就是靠它跳过的）。
- **路径相对于 `process.cwd()`**：`path.relative(process.cwd(), ...)` 把绝对路径改写成相对当前目录的路径，且 `run()` 的工作目录也传了 `process.cwd()`——所以输出里的文件路径前缀取决于你在哪里调用 `cm`，通常就是仓库根。
- **`-nH` 与 `--color`**：`-n` 带行号，`-H` 强制显示文件名前缀（多文件时 grep 本来就会显示，`-H` 保证单文件情形也一致）。`--color` 想上色，但注意 grep 子进程的 stdout 是被 `run()` 捕获的**管道**而非终端，很多 grep 实现对非终端 stdout 会按 `auto` 处理并省略颜色码——实际是否有色取决于系统 grep 的行为，可在本地观察验证。
- **单次调用**：所有文件拼进一条 grep 命令行，一次进程启动完成全仓搜索，没有逐包递归。
- **无匹配 = 静默失败**：grep「没有匹配行」时以退出码 1 结束，`execFileSync` 视非零退出为错误抛出，外层 catch 只做 `process.exit(1)`——不打印任何「未找到」提示。搜索落空时你只会看到命令沉默地结束，退出码是 1。

#### 4.3.4 代码实践

1. **实践目标**：验证搜索范围与多点文件名的盲区。
2. **操作步骤**（需本地环境，**待本地验证**）：
   - 在仓库根运行 `node bin/cm.js grep EditorView`，统计输出涉及的包数量与目录类别（应只出现 `src/`、`test/` 与 `demo/demo.ts`，`legacy-modes` 的匹配应来自 `mode/` 下的 `.js`/`.d.ts`）。
   - 再运行一个必然无匹配的搜索，如 `node bin/cm.js grep zzz_no_such_symbol`，随后执行 `echo $?` 查看退出码。
3. **需要观察的现象**：第一次输出大量带行号的匹配；第二次无任何输出且退出码为 1。
4. **预期结果**：与 4.3.3 最后两点的分析一致。可进一步观察：输出里是否存在文件名含多个点号的文件？结合后缀提取正则解释你看到的现象。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `grep()` 的文件收集基于 `packages`（全量 36 包）而不是文件系统扫描？

答案：注册表是唯一权威清单——它能天然排除 `node_modules`、`dist`、构建产物，并且让 `legacy-modes` 这类结构特殊的包可以按名走显式分支。用文件系统扫描则需要维护一长串忽略规则。

**练习 2**：如果给 `add()` 的白名单传 `[".ts", ".test.ts"]`，能搜到名为 `foo.test.ts` 的文件吗？

答案：能。`/^[^.]*(.*)/` 对 `foo.test.ts` 提取出 `".test.ts"`，白名单里恰好有这一项即可精确命中。这说明白名单匹配的其实是「从第一个点号起的整段后缀」，把多点文件名当作一种独立后缀对待。

**练习 3**：`cm grep` 为什么不像 `status` 那样逐包执行，而是攒一个大文件列表？

答案：grep 是纯只读、无状态的查询，单次进程处理全部文件比分 36 次启动进程快得多，且能统一输出排序与去重逻辑；而 status/commit/push 必须逐包执行，因为 git 命令以仓库目录为单位。

### 4.4 runCmd()：逐包执行任意命令

#### 4.4.1 概念说明

`runCmd()`（命令名 `run`）是这组工作流命令里的「万能逃生口」：把任意一条命令在每个包目录里各跑一遍。它是前三个命令的泛化形式——`cm status` 理论上可以近似为一条精心构造的 `cm run`，但专职命令带过滤、更安静；`run` 则不设过滤、无条件执行，适合「确认环境」「批量查看」类任务。

#### 4.4.2 核心流程

1. 遍历 `packages`，先打印 `包名:` 作为分节头。
2. 在 `pkg.dir` 里以用户给定的命令与参数执行 `run()`（默认 pipe 捕获），把捕获的 stdout 打印出来。
3. 任一包执行失败（非零退出码等）→ 打印 `e.toString()` 并 `process.exit(1)`，**循环立即终止**，后续包不再执行。

注意函数签名 `runCmd(cmd, ...args)`：rest 参数不计入 `length`，所以 `cmdFn.length` 为 1——`cm run` 至少要带一个参数，否则触发 `help(1)`（u1-l3 的参数下限校验）；参数上限不校验，命令与参数个数任意。

#### 4.4.3 源码精读

[bin/cm.js:334-344](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L334-L344) —— `runCmd()` 全文：

```js
function runCmd(cmd, ...args) {
  for (let pkg of packages) {
    console.log(pkg.name + ":")
    try {
      console.log(run(cmd, args, pkg.dir))
    } catch (e) {
      console.log(e.toString())
      process.exit(1)
    }
  }
}
```

对比 4.2 里的 `commit`/`push` 可以看清差异：

- **自带 try/catch 而非任由异常上抛**：错误经 `console.log`（走 stdout）打印错误对象，再主动 `process.exit(1)`。这与 `commit` 依赖 `start()` 外层 Promise 捕获（错误走 `error()` 的 stderr，u1-l3）是两种错误出口；`runCmd` 选择自己处理，语义是「用户命令失败不奇怪，直接报告并停」。
- **`e.toString()`**：`execFileSync` 抛出的是 Error 对象，`toString` 得到 `Error: Command failed: ...` 一类的摘要，比完整堆栈安静。
- **遇错即停是一个设计决策**：第 3 个包失败后第 4~36 个包不执行。批量任务要么全链路执行，要么尽早止损——这里选了后者。想「跳过失败继续跑」就得改循环结构（见综合实践的思考题）。
- `run()` 的 `cwd` 是 `pkg.dir`，但命令的**参数是原样共享的**——`cm run ls src` 在每个包里都执行 `ls src`，利用了各包目录结构的高度一致性（都由 u2-l1 的 `Pkg` 模型约定）。

#### 4.4.4 代码实践

1. **实践目标**：确认遍历范围与错误路径。
2. **操作步骤**（需本地环境，**待本地验证**）：
   - `node bin/cm.js run pwd` —— 记录输出的分节数量与每个路径。
   - `node bin/cm.js run node -v` —— 每节应打印同一个 Node 版本号。
   - `node bin/cm.js run false` —— `false` 是 UNIX 里恒返回非零的命令，用来确定性触发错误路径。
3. **需要观察的现象**：`pwd` 的输出是 36 个分节、每节一个包目录的绝对路径（包含 `legacy-modes`）；`false` 在**第一个包**就打印 `Error: Command failed: ...` 并退出，后续包没有任何输出。
4. **预期结果**：与 4.4.2 的流程一致——`run` 是「36 次无条件执行 + 首错即停」。

#### 4.4.5 小练习与答案

**练习 1**：`cm run git status -sb` 与 `cm status` 的输出有何不同？

答案：前者对全部 36 个包无条件打印原始 `git status -sb` 输出（干净包也有分支行），后者只打印偏离基线的包。专职命令的价值就在这道过滤器。

**练习 2**：`cm run npm test` 与 `cm test` 是一回事吗？

答案：不是。`cm run npm test` 在每个包目录里独立执行该包自己的 npm test；`cm test`（u2-l4）经 `@marijn/testtool` 把 35 个可构建包的测试统一收集成 Node/浏览器双轨一次性运行。前者逐包、无浏览器轨道；后者集中编排。

**练习 3**：如何让 `runCmd` 失败后继续跑完剩余包？

答案：把 `process.exit(1)` 从 catch 里移除、仅记录失败（例如 `let failed = true`），循环结束后再按 `failed` 决定退出码——这正是 u2-l4 里 `test()` 把布尔失败映射为 `process.exit(failed ? 1 : 0)` 的做法。（这是修改思路，属示例代码，非仓库现有实现。）

## 5. 综合实践

把本讲五个命令串成一次完整的多仓库巡查，并完成规格要求的扩展任务。

**第一部分：三命令巡查**（需本地已完成 `cm install`；本分析环境未克隆包目录，以下均为基于源码的预期，**待本地验证**）。

1. `node bin/cm.js status` —— 记录出现了哪些包。若工作区干净，预期**无输出**；这就是「有变化才输出」过滤的含义。
2. `node bin/cm.js run pwd` —— 记录输出的分节范围。预期 36 节（35 个包 + `legacy-modes`），每节是 `包名:` 加一个仓库根下的绝对路径。
3. `node bin/cm.js grep EditorView` —— 记录匹配的分布。预期覆盖 `demo/demo.ts` 与各包 `src/`、`test/` 下的 `.ts` 文件，`legacy-modes` 若有匹配应来自 `mode/` 的 `.js`/`.d.ts`。
4. 三者的「范围」对比就是本讲的主线：status 是**仓库级**的过滤视图，run 是**仓库级**的无过滤视图，grep 是**文件级**的内容视图。

**第二部分：为 grep 增加 `--test-only` 选项**。

利用 u1-l3 的结论——分发层不校验参数上限——`cm grep EditorView --test-only` 已经能把 `"--test-only"` 作为第二个参数送进 `grep()`，无需改动 `start()`。只需修改 `grep()` 本身（以下为**示例代码**，非仓库现有实现）：

```js
function grep(pattern, ...rest) {          // 接收额外选项
  let testOnly = rest.includes("--test-only")
  let files = testOnly ? [] : [join(root, "demo", "demo.ts")]
  function add(dir, ext) { /* 原实现不变 */ }
  for (let pkg of packages) {
    if (pkg.name == "legacy-modes") {
      if (!testOnly) add(join(pkg.dir, "mode"), [".js", ".d.ts"])  // 它没有 test 目录
    } else {
      if (!testOnly) add(join(pkg.dir, "src"), [".ts"])
      add(join(pkg.dir, "test"), [".ts"])
    }
  }
  /* try { console.log(run("grep", ...)) } catch (e) { process.exit(1) } 原样保留 */
}
```

要点：

1. 函数签名改为 `(pattern, ...rest)` 不影响 `cmdFn.length` 仍为 1（rest 不计入），参数校验行为不变。
2. `--test-only` 下排除 `demo/demo.ts`、各包 `src/` 与 `legacy-modes/mode`（它没有 `test/` 目录，搜索它等于噪声）。
3. 同步更新 [bin/cm.js:54](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L54) 的帮助文本，把 `cm grep <pattern>` 改为 `cm grep <pattern> [--test-only]` 之类的描述——帮助文本与实现是手工同步的，漏了就会误导人（`test()` 的 `--chrome` 就是这样与帮助脱节的，见 u2-l4）。
4. 验证：`node bin/cm.js grep EditorView --test-only` 的输出应只含各包 `test/` 路径；对照修改前的完整输出确认差集恰为 `src/`、`mode/` 与 `demo.ts` 的匹配。
5. 提醒：这是学习用的本地练习。按 CONTRIBUTING.md 的规范，本仓库不欢迎随手提交的生成代码；练习完成后可用 `git checkout -- bin/cm.js` 还原。

## 6. 本讲小结

- 五个工作流命令共享一个骨架：遍历 `packages`（全量 36 包，含 `legacy-modes`）→ 用 `run()` 在 `pkg.dir` 捕获命令输出 → 字符串谓词过滤 → 对命中的包执行动作。
- `status()` 用与 `"## main...origin/main\n"` 的**全等比较**实现「有变化才输出」，任何偏离基线的状态（改动、未跟踪、ahead/behind、换分支）都会浮出。
- `commit()` 用 `git diff` / `git diff --cached` 输出的真值判断「有改动」，未跟踪文件是盲区，由 `status()` 互补；`push()` 用 `/\bahead\b/` 词边界正则判断「有新提交」，成功时静默。
- `grep()` 先按注册表收集 `src/`、`test/` 的 `.ts`（`legacy-modes` 特例走 `mode/` 的 `.js`/`.d.ts`），再单次调用系统 grep；后缀提取正则决定了多点文件名不进 `.ts` 白名单；无匹配时静默退出码 1。
- `runCmd()` 是无过滤的逐包万能执行器，自带 try/catch、遇错即停、错误走 stdout 并主动 `process.exit(1)`。
- 这组命令体现了「把 git/工具的文本输出当 API 用」的工具哲学：几十行代码换掉一整套 monorepo 框架，代价是依赖输出格式的稳定性与 `rm`、`grep` 等 UNIX 工具的存在。

## 7. 下一步学习建议

本讲的命令都在解析 `git status` 的**工作区状态**文本；下一讲 u3-l1《变更日志挖掘：用正则从 git log 提取分类变更》将升级到解析 `git log --format=%B%n` 的**提交信息正文**——同样是「run() 捕获 + 正则处理」，但用带前瞻断言 `(?=...)` 的分段正则把提交划入 BREAKING/FIX/FEATURE 三类，为发布流程供料。建议先精读 [bin/cm.js:162-168](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L162-L168) 的 `changelog()`，对照本讲 4.3.3 的正则技巧寻找呼应；有余力的话再回看 [bin/cm.js:200-217](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L200-L217) 的 `updateDependencyVersion()`，它把「逐包遍历 + git 操作」的模式用在了依赖版本联动上。
