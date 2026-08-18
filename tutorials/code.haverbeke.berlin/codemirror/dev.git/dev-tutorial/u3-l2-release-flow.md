# 发布流程：语义化版本与 CHANGELOG 自动生成

## 1. 本讲目标

上一讲（u3-l1）我们拆解了 `changelog()`——它用分段正则从 `git log` 中提取出 `{fix, feature, breaking}` 三类变更。本讲沿着这条数据继续向下走，贯通 `cm release` 命令的全链路。学完本讲，你应该能够：

1. 说出 `bumpVersion()` 中 **0.x 版本**与**正式版本**两套升级规则的差异，以及 0.x 阶段 feature 只升 patch 的原因。
2. 完整追踪从 `changes` 到 `releaseNotes` 再到 `git commit` 与 `git tag` 的每一步，理解「版本号字符串兼任 tag 名与变更区间端点」这个闭环设计。
3. 解释 `setModuleVersion` 与 `updateDependencyVersion` 的联动方式，以及依赖联动为什么被 `if (false && ...)` 暂时停用。
4. 会用 `cm unreleased` 在发布前预览各包尚未发布的变更，防止标记写错导致变更静默丢失。

## 2. 前置知识

### 2.1 语义化版本（SemVer）速览

版本号写作 `MAJOR.MINOR.PATCH`（如 `1.5.3`），惯用含义：

- **MAJOR（主版本）**：出现不兼容的 API 变更时 +1；
- **MINOR（次版本）**：向下兼容地新增功能时 +1；
- **PATCH（修订号）**：向下兼容地修复缺陷时 +1。

npm 依赖里常见的 `^` 前缀表示「兼容范围」：`^1.5.3` 等价于 `>=1.5.3 <2.0.0`。但 **0.x 是特例**：`^0.20.0` 等价于 `>=0.20.0 <0.21.0`——对 0.x 版本，`^` 锁住的是 **minor** 而不是 major。也就是说：

- 0.x 阶段：`0.MINOR` 是兼容性边界（minor 承担了正式版中 major 的角色）；
- 正式版（≥1.0.0）：major 是兼容性边界。

SemVer 规范本身声明 0.x 是「初始开发阶段，任何事情都可能变化」；社区惯例是把 0.x 的 minor 当作破坏性变更位。`bumpVersion()` 正是这个惯例的代码化。

### 2.2 git 附注标签与提交区间

- `git tag <名字> -m <消息>` 创建的是**附注标签**（annotated tag）：git 会生成一个独立的 tag 对象保存消息，之后可用 `git tag -n99 <tag>` 查看完整注释（`-n<num>` 中的数字指定最多展示的行数）。
- 标签名是合法的「revision」，可以直接充当提交区间的端点：`git log A..B` 表示「从 A 到 B 之间（不含 A）的提交」。本讲的发布闭环正依赖这一点。

### 2.3 承接 u3-l1：changelog() 的契约

u3-l1 已逐行拆过 [bin/cm.js:L162-L168](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L162-L168) 中的 `changelog(pkg, since)`，本讲只复用它的契约：

- 输入：包对象 `pkg` 与 `since`（一个**版本号字符串**，函数内部拼成 `since + "..main"` 作为 git 区间）；
- 输出：`{fix: [], feature: [], breaking: []}`，每个元素是拍平成单行的变更描述；
- 风险点：`BREAKING:`/`FIX:`/`FEATURE:` 标记必须独立成段且大写，写错不报错而是**静默丢失**——这正是后面 `unreleased()` 存在的理由。

## 3. 本讲源码地图

本讲只涉及一个源码文件 `bin/cm.js`，但跨越其中 9 个函数/常量：

| 位置 | 名称 | 职责 |
|---|---|---|
| [bin/cm.js:L17-L33](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L17-L33) | `start()` 的命令映射表 | 注册 `release`（L22）与 `unreleased`（L23）两个子命令 |
| [bin/cm.js:L170-L177](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L170-L177) | `bumpVersion()` | 按变更类型推演下一个版本号 |
| [bin/cm.js:L179-L193](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L179-L193) | `releaseNotes()` | 把分类变更渲染成 Markdown 发布说明（u3-l1 已精读） |
| [bin/cm.js:L195-L198](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L195-L198) | `setModuleVersion()` | 把新版本号写进包的 package.json |
| [bin/cm.js:L200-L217](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L200-L217) | `updateDependencyVersion()` | 联动升级其他包对本包的依赖版本（当前被停用） |
| [bin/cm.js:L219-L223](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L219-L223) | `version()` 与 `mainVersion` | 读包当前版本；提取「显著版本位」 |
| [bin/cm.js:L225-L246](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L225-L246) | `release()` | 子命令入口：解析参数、git pull、调用 doRelease |
| [bin/cm.js:L248-L269](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L248-L269) | `doRelease()` | 发布主体：版本决策、写 CHANGELOG、commit、tag |
| [bin/cm.js:L271-L280](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L271-L280) | `editReleaseNotes()` | `--edit` 时调外部编辑器人工修订发布说明 |
| [bin/cm.js:L282-L288](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L282-L288) | `unreleased()` | 遍历所有包，预览尚未发布的变更 |

提醒：`bin/cm.js` 末尾直接调用了 `start()`（[bin/cm.js:L366](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L366)），且没有任何 `module.exports`——所以**不能** `require("./bin/cm")` 来复用其中的函数，本讲的实践都是把函数原样复制到独立脚本（标注为「示例代码」）再运行。

## 4. 核心概念与源码讲解

### 4.1 bumpVersion()：0.x 与正式版两套升级规则

#### 4.1.1 概念说明

`bumpVersion(version, changes)` 是整个发布流程的「版本决策器」：输入包的当前版本号字符串和 `changelog()` 产出的三类变更，输出下一个版本号字符串。

它体内并存两套规则：

- **0.x 规则**：项目尚在初始开发阶段。出现 breaking 变更升 minor（`0.N.1 → 0.(N+1).0`），其余情况（无论 fix 还是 feature）一律只升 patch。feature 不单独升 minor，因为 0.x 阶段次版本号已经被「不兼容变更」占用，而 semver 对 0.x 的稳定性本来就不做承诺。
- **正式版规则（major ≥ 1）**：教科书式 SemVer——breaking 升 major、feature 升 minor、fix 升 patch。

另有一个安全阀：三类变更全为空时抛出 `"No new release notes!"`。因为全空要么说明确实没有可发布的内容，要么说明提交里的标记段落写错了（被 u3-l1 讲过的正则静默丢弃）——两种情况都不该发布一个「空版本」，在这里中断流程是正确行为。

#### 4.1.2 核心流程

决策表（`x ≥ 1` 表示 major 大于等于 1）：

| 当前版本 | 变更情况 | 结果 |
|---|---|---|
| `0.y.z` | 有 breaking | `0.(y+1).0` |
| `0.y.z` | 无 breaking（fix、feature 同等对待） | `0.y.(z+1)` |
| `x.y.z`（x≥1） | 有 breaking | `(x+1).0.0` |
| `x.y.z`（x≥1） | 无 breaking、有 feature | `x.(y+1).0` |
| `x.y.z`（x≥1） | 只有 fix | `x.y.(z+1)` |
| 任意 | 三类全空 | 抛 `Error("No new release notes!")` |

注意实现细节：版本号被 `split(".")` 拆成三个字符串段、用 `Number()` 转数字运算后再拼回去，所以 `0.1.9` 升 patch 会正确得到 `0.1.10`，不存在「字符串加一」的进位问题。

#### 4.1.3 源码精读

版本决策的完整实现只有 8 行，见 [bin/cm.js:L170-L177](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L170-L177)——它先拆版本号，第一行 `if` 处理全部 0.x 情形，随后三行按 breaking → feature → fix 的优先级处理正式版：

```js
function bumpVersion(version, changes) {
  let [major, minor, patch] = version.split(".")
  if (major == "0") return changes.breaking.length ? `0.${Number(minor) + 1}.0` : `0.${minor}.${Number(patch) + 1}`
  if (changes.breaking.length) return `${Number(major) + 1}.0.0`
  if (changes.feature.length) return `${major}.${Number(minor) + 1}.0`
  if (changes.fix.length) return `${major}.${minor}.${Number(patch) + 1}`
  throw new Error("No new release notes!")
}
```

- L171：解构拆出三段版本号（此时还是字符串）。
- L172：0.x 分支。三元表达式只区分「有没有 breaking」——feature 在这里被有意降级为 patch 级变更。
- L173-L175：正式版的三个优先级分支。注意 breaking 判断先于 feature、feature 先于 fix：三类同时存在时，以最严重的变更为准。
- L176：全空时的唯一抛错出口。这个异常没有被 `doRelease` 捕获，会沿 `start()` 里 `new Promise(...).catch(e => error(e))`（[bin/cm.js:L35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35)）汇入统一的 `error()`——打印到 stderr 并以退出码 1 结束（u1-l3 讲过的统一错误出口）。

#### 4.1.4 代码实践

**实践目标**：用真值表验证两套升级规则，特别是「0.x 的 feature 只升 patch」这一反直觉点。

**操作步骤**：把上面的 `bumpVersion` 原样复制进一个临时脚本（以下为示例代码，函数体逐字复制自 cm.js），在项目根目录外任意位置（如 `/tmp`）运行：

```js
// /tmp/bump-drill.js —— 示例代码：bumpVersion 逐字复制自 bin/cm.js L170-177
function bumpVersion(version, changes) {
  let [major, minor, patch] = version.split(".")
  if (major == "0") return changes.breaking.length ? `0.${Number(minor) + 1}.0` : `0.${minor}.${Number(patch) + 1}`
  if (changes.breaking.length) return `${Number(major) + 1}.0.0`
  if (changes.feature.length) return `${major}.${Number(minor) + 1}.0`
  if (changes.fix.length) return `${major}.${minor}.${Number(patch) + 1}`
  throw new Error("No new release notes!")
}
let cases = [
  ["0.1.3", {fix: ["a"], feature: [], breaking: []}],
  ["0.1.3", {fix: [], feature: ["b"], breaking: []}],
  ["0.1.3", {fix: [], feature: [], breaking: ["c"]}],
  ["1.2.3", {fix: ["a"], feature: [], breaking: []}],
  ["1.2.3", {fix: [], feature: ["b"], breaking: []}],
  ["1.2.3", {fix: [], feature: [], breaking: ["c"]}],
]
for (let [v, c] of cases) console.log(v, JSON.stringify(c), "->", bumpVersion(v, c))
try { bumpVersion("1.2.3", {fix: [], feature: [], breaking: []}) }
catch (e) { console.log("all empty -> throws:", e.message) }
```

```bash
node /tmp/bump-drill.js
```

**需要观察的现象**：7 行输出中，第 1、2 行结果相同，第 3 行 minor 加一，第 4-6 行分别升 patch/minor/major。

**预期结果**（由源码逐行推演，编写时未实际运行，请在本地验证）：

```
0.1.3 {"fix":["a"],"feature":[],"breaking":[]} -> 0.1.4
0.1.3 {"fix":[],"feature":["b"],"breaking":[]} -> 0.1.4
0.1.3 {"fix":[],"feature":[],"breaking":["c"]} -> 0.2.0
1.2.3 {"fix":["a"],"feature":[],"breaking":[]} -> 1.2.4
1.2.3 {"fix":[],"feature":["b"],"breaking":[]} -> 1.3.0
1.2.3 {"fix":[],"feature":[],"breaking":["c"]} -> 2.0.0
all empty -> throws: No new release notes!
```

#### 4.1.5 小练习与答案

**练习 1**：当前版本 `0.5.2`，自上次发布后有一条 `FEATURE:` 和两条 `FIX:` 标记，下一版本是什么？如果是 `5.2.0` 呢？
**答案**：`0.5.2` → `0.5.3`（0.x 分支只看有没有 breaking）；`5.2.0` → `5.3.0`（feature 优先于 fix）。

**练习 2**：`bumpVersion` 抛出的异常最终到哪里去了？为什么它不需要自己 `process.exit`？
**答案**：沿调用链 `doRelease → release` 一路上抛，被 `start()` 中 Promise 的 `.catch(e => error(e))` 捕获，由 `error()` 打印并以退出码 1 退出（参见 [bin/cm.js:L35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35) 与 [bin/cm.js:L59-L62](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L59-L62)）。这是 u1-l3 讲过的统一错误出口设计。

**练习 3**：为什么 0.x 阶段 feature 变更「只升 patch」不算 bug？
**答案**：semver 规范把 0.x 定义为初始开发阶段、不做稳定性承诺；社区惯例用 0.x 的 minor 表示不兼容变更（与 `^` 对 0.x 锁 minor 的范围语义一致），于是 minor 位已被占用，feature 只能落到 patch。

### 4.2 release() 与 doRelease()：从变更到 commit 与 tag 的主链路

#### 4.2.1 概念说明

`cm release <package>` 的产出是什么？答案有点出人意料：**只是包仓库里的一个本地 commit 和一个本地附注 tag**。整个 `cm.js` 中没有任何子命令执行 `git push` 或 `npm publish`——推送到远端可以用 u2-l5 讲过的 `cm push`（或手工 push），发布 npm 包完全不在本脚本职责内。帮助文本里写的 "Create commits to tag a release"（[bin/cm.js:L47-L48](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L47-L48)，注意 commits 是复数）也印证了这一点。

这条链路最精巧的设计是**版本号字符串的一物三用**：

1. `setModuleVersion` 把新版本号写进包的 package.json；
2. `git tag` 用同一个字符串做**标签名**；
3. 下一轮发布时 `changelog()` 读出 package.json 里的当前版本号，拼成 `版本号..main` 作为**提交区间端点**——由于标签名与版本号相同，git 区间恰好从「上一次打标签的提交」开始。

三者互为镜像，形成闭环：**package.json 的 version 字段、最新 tag、changelog 区间起点，永远是同一个字符串**。这也是为什么 `changelog()` 可以直接拿版本号当 git revision 用。

链路里还有两个辅助写操作：`setModuleVersion`（改版本号）与 `updateDependencyVersion`（联动升级其他包的依赖声明）。后者目前被 `if (false && ...)` 停用——原因见 4.2.3 最后一小节。

#### 4.2.2 核心流程

```
cm release <package> [--edit] [--version <version>]
 │
 ├─ ① 解析参数：--edit 开关 / --version <v> / 首个非 "-" 参数为包名，其余一律 help(1)
 ├─ ② 校验包名必须存在于包注册表（packageNames）
 ├─ ③ 在包目录执行 git pull（先与远端对齐）
 ├─ ④ doRelease(pkg, setVersion, {edit})
 │    ├─ currentVersion = 读包 package.json 的 version
 │    ├─ changes = changelog(pkg, currentVersion)      ← 以「旧版本号 tag」为区间起点
 │    ├─ newVersion = --version 的值 ?? bumpVersion(currentVersion, changes)
 │    │      （首次发布的新包：不 bump，保持现版本号）
 │    ├─ notes = releaseNotes(changes, newVersion)      ← 机器初稿 {head, body}
 │    ├─ [--edit] notes = editReleaseNotes(notes)       ← 人工修订，或清空文件取消发布
 │    ├─ setModuleVersion(pkg, newVersion)              ← package.json 写入新版本号
 │    ├─ CHANGELOG.md 头部前插 notes.head + notes.body  ← 新条目在最上面
 │    ├─ git add package.json CHANGELOG.md
 │    ├─ git commit -m "Mark version <v>"
 │    └─ git tag <v> -m "Version <v>\n\n<body>" --cleanup=verbatim
 │           （附注标签：标签名 = 版本号，注释 = 发布说明正文）
 └─ ⑤（停用）若「显著版本位」变化 → updateDependencyVersion 联动其他包
```

注意顺序上的两个要点：**`--edit` 的人工修订发生在任何 git 写操作之前**（取消发布不留痕迹）；**`changelog` 在 `setModuleVersion` 之前调用**（此时 package.json 里还是旧版本号，区间才是正确的「上次发布以来」）。

#### 4.2.3 源码精读

**入口与参数解析**（[bin/cm.js:L225-L234](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L225-L234)）——手写循环解析，不依赖任何参数解析库：

```js
function release(...args) {
  let setVersion, edit = false, pkgName, pkg
  for (let i = 0; i < args.length; i++) {
    let arg = args[i]
    if (arg == "--edit") edit = true
    else if (arg == "--version" && i < args.length) setVersion = args[++i]
    else if (!pkgName && arg[0] != "-") pkgName = arg
    else help(1)
  }
  if (!pkgName || !(pkg = packageNames[pkgName])) help(1)
```

- 第一个非 `-` 开头的参数当作包名，第二个位置参数或未知开关都会走到 `help(1)`（用法错误，退出码 1）。
- 一个值得留意的临界阅读点：L230 的 `arg == "--version" && i < args.length` 里，`i < args.length` 在循环体内**恒为真**（循环条件就是它），所以这并不是「`--version` 必须带值」的保护。若 `--version` 恰好是最后一个参数，`args[++i]` 取到 `undefined`，`setVersion` 为 `undefined`，随后 `doRelease` 会把它当作「未指定版本」走 `bumpVersion`——静默降级而非报错。这是老代码里典型的「看似防御、实则无效」的写法。
- `release` 的签名是 rest 参数（`...args`），函数 `length` 为 0，所以 u1-l3 讲过的「`cmdFn.length > args.length` 参数下限校验」对它不设限。

**同步远端并调用主体**（[bin/cm.js:L236-L238](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L236-L238)）——发布前先 `git pull`，保证版本决策基于远端最新状态；随后把版本指定与编辑开关交给 `doRelease`，取回 `{changes, newVersion}`：

```js
  run("git", ["pull"], pkg.dir)

  let {changes, newVersion} = doRelease(pkg, setVersion, {edit})
```

**发布主体**（[bin/cm.js:L248-L269](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L248-L269)），按执行顺序分四段看。

第一段（L249-L255）：**取变更、定版本**。

```js
  let log = join(pkg.dir, "CHANGELOG.md")
  let newPackage = !fs.existsSync(log)

  let currentVersion = version(pkg)
  let changes = newPackage ? {fix: [], feature: [], breaking: ["First numbered release."]} : changelog(pkg, currentVersion)
  if (defaultChanges && !changes.fix.length && !changes.feature.length && !changes.breaking.length) changes = defaultChanges
  if (!newVersion) newVersion = newPackage ? currentVersion : bumpVersion(currentVersion, changes)
```

- 包目录里没有 `CHANGELOG.md` 即视为**首次发布**：变更固定为一条 breaking 记录 "First numbered release."，且不 bump 版本——沿用 package.json 里已有的版本号（通常会是第一个正式编号）。
- `defaultChanges` 是个预留参数：仅当调用方传入**且**三类变更全空时兜底。当前仓库里唯一的调用点（L238）没有传它，它恒为 `null`——属于给外部实验留的口子。

第二段（L258-L262）：**生成说明、写入两个文件**。

```js
  let notes = releaseNotes(changes, newVersion)
  if (edit) notes = editReleaseNotes(notes)

  setModuleVersion(pkg, newVersion)
  fs.writeFileSync(log, notes.head + notes.body + (newPackage ? "" : fs.readFileSync(log, "utf8")))
```

- `releaseNotes`（4.3 节详述）产出 `{head, body}`；`--edit` 时先经人工修订。
- `setModuleVersion` 用正则替换改写 package.json 里的版本字段，见 [bin/cm.js:L195-L198](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L195-L198)：`/"version":\s*".*?"/` 没有 `g` 标志，只替换**第一处**匹配——它隐含假设「顶层 version 是文件里第一个出现的 version 字段」，npm 常规排版下成立。

```js
function setModuleVersion(pkg, version) {
  let file = join(pkg.dir, "package.json")
  fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace(/"version":\s*".*?"/, `"version": "${version}"`))
}
```

- 写 `CHANGELOG.md` 的表达式是「**前插**」：新条目（head + body）拼在旧文件内容之前，所以 CHANGELOG 是最新在上的倒序结构；首次发布时旧内容为空串。

第三段（L263-L266）：**提交与打标签**。

```js
  run("git", ["add", "package.json"], pkg.dir)
  run("git", ["add", "CHANGELOG.md"], pkg.dir)
  run("git", ["commit", "-m", `Mark version ${newVersion}`], pkg.dir)
  run("git", ["tag", newVersion, "-m", `Version ${newVersion}\n\n${notes.body}`, "--cleanup=verbatim"], pkg.dir)
```

- 提交信息固定为 `Mark version <版本号>`，只包含这两个文件。
- 标签名就是版本号字符串；标签注释 = `Version <版本号>` + 空行 + 发布说明正文（与写进 CHANGELOG 的 body 一字不差）。`--cleanup=verbatim` 告诉 git **原样保留**注释内容——默认清理模式会压缩空白行、删掉 `#` 开头的行，会破坏正文的分段结构。

第四段：**返回值**（L268）`return {changes, newVersion}`，供调用方（以及停用的联动块）使用。

**读版本的辅助函数**（[bin/cm.js:L219-L221](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L219-L221)）：

```js
function version(pkg) {
  return require(join(pkg.dir, "package.json")).version
}
```

它用 `require` 读 JSON（Node 会自动解析）。注意 `require` 有模块缓存：同一进程内第二次 `require` 同一个 package.json 会拿到**缓存中的旧对象**。当前流程在 `setModuleVersion` 改写文件之前就读完了版本号，所以无碍；但这个小细节与停用的联动块有微妙的相互作用，见练习 3。

**依赖联动与它的停用**（[bin/cm.js:L240-L245](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L240-L245)）：

```js
  // Turned off for now, since this creates a huge mess on accidental
  // major version bumps. Maybe add a manual utility for it?
  if (false && mainVersion.exec(newVersion)[0] != mainVersion.exec(version(pkg))[0]) {
    let updated = updateDependencyVersion(pkg, newVersion)
    if (updated.length) console.log(`Updated dependencies in ${updated.map(p => p.name).join(", ")}`)
  }
```

- 触发条件是「**显著版本位**变化」。`mainVersion = /^0.\d+|\d+/`（[bin/cm.js:L223](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L223)）对 `0.20.1` 匹配出 `"0.20"`、对 `1.5.0` 匹配出 `"1"`、对 `12.0.0` 匹配出 `"12"`——恰好就是 `^` 范围的不兼容边界（0.x 锁 minor、正式版锁 major）。只有跨过这条边界，其他包里形如 `"^0.20.0"` 的依赖范围才会失效，才需要联动升级。
- 注释原文说明了停用原因：**误操作造成的意外 major bump 会用这条联动在 36 个包仓库里批量改写 package.json 并生成一堆提交，制造「巨大的混乱」**；作者倾向于将来把它改成一个手工触发的独立工具。
- 由于 `false &&` 短路，条件右侧（包括 `version(pkg)`）根本不会被求值——`updateDependencyVersion` 目前是死代码。

**联动函数本身**（[bin/cm.js:L200-L217](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L200-L217)）仍然值得一读，它是「以字符串改写代替 JSON 解析」的典型风格：

```js
function updateDependencyVersion(pkg, version) {
  let changed = []
  for (let other of packages) if (other != pkg) {
    let pkgFile = join(other.dir, "package.json"), text = fs.readFileSync(pkgFile, "utf8")
    let updated = text.replace(new RegExp(`("@codemirror/${pkg.name}": ")(.*?)"`, "g"), (_, m) => m + "^" + version + '"')
    if (updated != text) {
      changed.push(other)
      fs.writeFileSync(pkgFile, updated)
      run("git", ["add", "package.json"], other.dir)
      let lastMsg = run("git", ["log", "-1", "--pretty=%B"], other.dir)
      if (/^Bump dependency /.test(lastMsg))
        run("git", ["commit", "--amend", "-m", lastMsg.trimEnd() + ", @codemirror/" + pkg.name], other.dir)
      else
        run("git", ["commit", "-m", "Bump dependency for @codemirror/" + pkg.name], other.dir)
    }
  }
  return changed
}
```

- 正则按字面匹配 `"@codemirror/<包名>": "<任意范围>"`（依赖声明格式，含 devDependencies 只要写法吻合），无论旧范围是精确版本还是 `^` 范围，一律改写为 `"^<新版本>"`。
- 最巧妙的是 `--amend` 分支：如果这个包上一条提交已经是 `Bump dependency ...`（同一轮发布里已联动过另一个包），就**修订该提交**并把包名追加到提交信息末尾——同一轮发布的多个依赖升级被合并成一条 `Bump dependency for @codemirror/a, @codemirror/b` 提交，而不是刷屏多条。

#### 4.2.4 代码实践

**实践目标**：在沙盒仓库里验证「版本号 tag 兼任 changelog 区间端点」的闭环，并手工重走 `doRelease` 第三段的提交与打标签步骤。

**操作步骤**（全部在一个临时目录进行，不触碰本仓库）：

```bash
mkdir /tmp/rel-sandbox && cd /tmp/rel-sandbox
git init -q -b main
git config user.email you@example.com && git config user.name you

# 初始版本 0.1.3，提交后立刻打上「版本号 tag」
echo '{"name": "@codemirror/sandbox", "version": "0.1.3"}' > package.json
git add -A && git commit -qm "initial"
git tag 0.1.3

# 两条带标记的提交（每个 -m 产生一个以空行分隔的段落）
git commit --allow-empty -m "Work" \
  -m "FEATURE: Add a shiny new thing" \
  -m "FIX: Repair a bug in the thing"
git commit --allow-empty -m "More work" \
  -m "BREAKING: The API changed entirely"

# 观察 changelog() 所用的原始输入：以版本号 tag 为区间端点
git log --format=%B%n --reverse 0.1.3..main
```

然后按 4.1.4 的方式把 `changelog` 与 `bumpVersion` 复制进脚本（示例代码）处理上面的 `git log` 文本：三个变更各归一类，`bumpVersion("0.1.3", changes)` 得到 `0.2.0`（breaking）。最后手工执行 `doRelease` 的写操作：

```bash
sed -i 's/"version": "0.1.3"/"version": "0.2.0"/' package.json   # setModuleVersion 的效果
# 用脚本生成的 notes 拼出 CHANGELOG.md（head 在前、body 在后），然后：
git add package.json CHANGELOG.md
git commit -m "Mark version 0.2.0"
git tag 0.2.0 -m "Version 0.2.0

### Breaking changes

The API changed entirely

### Bug fixes

Repair a bug in the thing

### New features

Add a shiny new thing" --cleanup=verbatim
```

**需要观察的现象**：`git log --format=%B%n --reverse 0.1.3..main` 只包含两条带标记的提交；打完 tag 后 `0.2.0..main` 区间为空。

**预期结果**（按源码推演，编写时未实际运行，**待本地验证**）：

- `git tag -n99 0.2.0` 显示的注释第一行为 `Version 0.2.0`，随后空行，然后是三个小节（Breaking changes → Bug fixes → New features，顺序来自 `releaseNotes` 的 `types` 对象键序）；
- `cat CHANGELOG.md` 首行为 `## 0.2.0 (当天日期)`；
- `git log --oneline -2` 顶部是 `Mark version 0.2.0`。

#### 4.2.5 小练习与答案

**练习 1**：如果 `doRelease` 跳过 `setModuleVersion` 直接打 tag，下一轮发布会出什么问题？
**答案**：package.json 里的版本号仍是旧值，下一轮 `changelog(pkg, currentVersion)` 拼出的区间会从**上上轮**的 tag 起算，把上一轮已经发布过的提交重复计入发布说明。

**练习 2**：`updateDependencyVersion` 里 `--amend` 分支解决什么问题？
**答案**：同一轮连续发布多个包时，某个下游包会被多次联动升级依赖；amend 把这些升级合并成一条 `Bump dependency for @codemirror/a, @codemirror/b` 提交，避免提交历史被刷屏（见 [bin/cm.js:L209-L213](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L209-L213)）。

**练习 3**：假设去掉停用块里的 `false &&`，`version(pkg)` 在 `setModuleVersion` 改写文件**之后**才被调用，为什么它仍能读到旧版本号？如果有人「修复」了这一点（每次强制重读磁盘），联动会怎样？
**答案**：`version()` 用 `require` 读 package.json，Node 的模块缓存会在同一进程内返回首次读取的旧对象——歪打正着地让比较「新版本 vs 旧版本」成立。若强制重读磁盘，`version(pkg)` 会返回新值，比较变成「新 vs 新」永远相等，联动被**静默跳过**——停用的代码里还埋着这样一个依赖隐式行为的陷阱。

### 4.3 releaseNotes() 与 editReleaseNotes()：机器初稿与人工修订

#### 4.3.1 概念说明

发布说明的生产分两步：

1. **机器初稿**：`releaseNotes(changes, version)` 把三类变更渲染成 Markdown——标题行 `## <版本> (日期)` 加上按 breaking → fix → feature 顺序排列的小节。u3-l1 已逐行拆过它的渲染规则（空类别省略、多行拍平、`](##` 简写锚点重写为 codemirror.net 文档链接），本讲不再重复，只关注它在链路中的位置：它的输出 `{head, body}` 被 `doRelease` 同时用于 CHANGELOG 前插和 tag 注释，也被 `unreleased` 复用于预览。
2. **人工终审**：`cm release <pkg> --edit` 时，`editReleaseNotes(notes)` 把初稿写进**中央仓库根目录**的临时文件 `notes.txt`，调用环境变量 `EDITOR`（缺省 `emacs`——作者的编辑器偏好）打开它，等编辑器退出后读回并删除临时文件。两种结局：
   - 读回内容**全为空白** → `process.exit(0)`，整个发布流程静默终止——这是「人工放弃发布」的通道；
   - 否则把第一行（版本标题）与其余内容重新切成 `{head, body}`，继续走后续的写入与提交。

`--edit` 的调用时机（`doRelease` 的 L259，在 `setModuleVersion`、commit、tag 之前）保证了取消发布时不留任何痕迹——设计上是「先审后写」。

#### 4.3.2 核心流程

```
notes = {head: "## 0.2.0 (2026-08-18)\n\n", body: "### Breaking changes\n\n...\n\n"}
   │ --edit
   ▼
写入 <中央仓库根>/notes.txt
   │ 启动 $EDITOR（缺省 emacs），阻塞等待退出
   ▼
读回 + 删除 notes.txt
   │
   ├─ 全空白 → process.exit(0)      ← 取消发布，此刻还没发生任何 git 写操作
   └─ 有内容 → /^(.*)\n+([^]*)/ 切分
        head = 第一行 + "\n\n"
        body = 其余全部
```

#### 4.3.3 源码精读

初稿的骨架在 [bin/cm.js:L179-L193](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L179-L193)——只回顾与本链路相关的三点：`head` 恒为 `## <版本> (<日期>)\n\n`；`body` 按 `types = {breaking, fix, feature}` 的键序（即插入序）拼接小节；函数返回 `{head, body}` 而不是整段字符串，正是为了方便「CHANGELOG 前插时用 head+body、tag 注释只用 body、预览只用 body」这三种不同的消费方式。

人工修订的实现在 [bin/cm.js:L271-L280](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L271-L280)：

```js
function editReleaseNotes(notes) {
  let noteFile = join(root, "notes.txt")
  fs.writeFileSync(noteFile, notes.head + notes.body)
  run(process.env.EDITOR || "emacs", [noteFile])
  let edited = fs.readFileSync(noteFile)
  fs.unlinkSync(noteFile)
  if (!/\S/.test(edited)) process.exit(0)
  let split = /^(.*)\n+([^]*)/.exec(edited)
  return {head: split[1] + "\n\n", body: split[2]}
}
```

- L272：临时文件放在中央仓库根 `root`（不是包目录）——它是跨包共用的草稿区。
- L274：`run()` 是 `execFileSync` 封装（u1-l2 讲过），进程会**阻塞**到编辑器退出；`EDITOR` 被当作命令名、`notes.txt` 作为其唯一参数（所以 `EDITOR` 里不能带参数）。
- L277：`/\S/.test(edited)` 检查是否含有任何非空白字符；读回的是 Buffer，测试时会隐式转成字符串。全空白即视为放弃，`process.exit(0)`——注意退出码是 **0**：放弃是正常路径，不是错误。
- L278：切分正则里 `(.*)` 只能匹配第一行（`.` 不匹配换行符），`\n+` 吃掉标题后的空行，`([^]*)` 接住其余全部内容；L279 在 head 末尾补回 `\n\n`，保证与 `releaseNotes` 的输出结构完全一致——「原样保存不改动」时这个函数是无损往返的。

#### 4.3.4 代码实践

**实践目标**：不安装 emacs，用「无操作编辑器」验证 `editReleaseNotes` 的无损往返与「清空即取消」两条路径。

**操作步骤**：把 `editReleaseNotes` 复制进临时脚本（示例代码，逐字复制自 cm.js，仅把 `root` 换成你的临时目录），然后：

```bash
# 路径一：EDITOR=true —— true 忽略参数、立即成功退出，等价于「打开看过但没改」
EDITOR=true node /tmp/edit-drill.js
# 路径二：EDITOR 指向一个把文件清空的脚本
printf '#!/bin/sh\n: > "$1"\n' > /tmp/blank-editor.sh && chmod +x /tmp/blank-editor.sh
EDITOR=/tmp/blank-editor.sh node /tmp/edit-drill.js; echo "exit code: $?"
```

脚本里先构造一份 `notes`，调用复制版 `editReleaseNotes`，打印返回的 `{head, body}` 与原稿对比。

**需要观察的现象**：路径一返回后进程正常结束，输出的 head/body 与原稿逐字节相同；路径二进程在编辑器退出后立即结束。

**预期结果**（按源码推演，**待本地验证**）：路径一两次输出完全一致（无损往返成立）；路径二进程退出码为 0、没有任何后续输出（`process.exit(0)` 在打印之前执行）。

#### 4.3.5 小练习与答案

**练习 1**：取消发布为什么用 `process.exit(0)` 而不是抛异常？
**答案**：异常会汇入 `error()`，打印错误并以退出码 1 结束——那是「故障」语义。人工放弃是正常决策，应当安静地以 0 退出，不制造红色噪音。

**练习 2**：如果编辑者不小心删掉了第一行标题（`## 0.2.0 (...)`），会发生什么？
**答案**：切分正则的 `(.*)` 会把 body 的第一行（`### Breaking changes`）当成标题，`head` 变成 `### Breaking changes\n\n`，CHANGELOG 的标题结构被破坏。切分逻辑只信任「第一行是标题」的约定，没有内容校验——人工修订时必须保留标题行。

**练习 3**：为什么 `releaseNotes` 返回 `{head, body}` 两个字符串而不是拼好的一段？
**答案**：三个消费点需要的部分不同——CHANGELOG 前插用 head+body、tag 注释只要 body（前面拼 `Version <v>`）、`unreleased` 预览也只要 body。拆开返回让同一份初稿服务三种用途。

### 4.4 unreleased()：发布前的预览

#### 4.4.1 概念说明

`cm unreleased` 是发布流水线的「事前检查」：遍历全部 36 个包，凡是 `changelog()` 能提取出变更的，就打印这个包**尚未发布**的发布说明正文。它的价值在于对抗 u3-l1 指出的风险——提交信息里的 `BREAKING:`/`FIX:`/`FEATURE:` 标记一旦拼写或分段不符合规范，变更会被正则**静默丢弃**，等真正 `cm release` 时轻则版本号算错（feature 漏记成 patch 发布），重则直接抛 "No new release notes!"。发布前跑一遍 `cm unreleased`，肉眼确认「该出现的变更都出现了」，是把静默错误提前暴露的最廉价手段。

#### 4.4.2 核心流程

```
for pkg of packages:                    # 全部 36 个包，包括没有克隆价值的 legacy-modes
    ver = version(pkg)                  # 读 package.json 当前版本号
    changes = changelog(pkg, ver)       # 自「当前版本号 tag」以来的变更
    if fix/feature/breaking 任一非空:
        打印 pkg.name + releaseNotes(changes, ver).body   # 只用 body，丢弃 head
```

注意一个细节：预览输出里**没有版本号和日期**。因为下一版本号要到 `bumpVersion` 才能确定，此刻根本不知道；`releaseNotes` 的第二个参数（这里传的是当前版本号）只影响被丢弃的 head，传什么都无所谓——这是「复用渲染函数但只取 body」的巧妙之处。

#### 4.4.3 源码精读

完整实现只有 7 行，见 [bin/cm.js:L282-L288](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L282-L288)：

```js
function unreleased() {
  for (let pkg of packages) {
    let ver = version(pkg), changes = changelog(pkg, ver)
    if (changes.fix.length || changes.feature.length || changes.breaking.length)
      console.log(pkg.name + ":\n\n" + releaseNotes(changes, ver).body)
  }
}
```

- 遍历的是全量 `packages`（u2-l1 讲过的三个视图之一），没有变更的包静默跳过——「输出为空」本身就是有效结果：所有包都没有未发布变更。
- `releaseNotes(changes, ver)` 的返回值只取 `.body`：`ver` 与当天日期都被算进 head 后丢弃。这也意味着预览正文与将来 `doRelease` 生成的正文**逐字一致**（只要两次调用之间没有新提交），预览即终稿。
- 它在命令映射表中注册于 [bin/cm.js:L23](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L23)，但帮助文本（[bin/cm.js:L39-L55](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L39-L55)）没有列出它——u1-l3 讲过「映射表才是权威清单」的又一例证。

#### 4.4.4 代码实践

**实践目标**：在真实仓库跑一次只读的 `cm unreleased`，理解输出（或空输出）的含义。

**操作步骤**（本命令只读 git log 与 package.json，不写任何文件，安全）：

```bash
# 前提：已执行过 cm install（命令开头的 assertInstalled 守卫要求所有包目录存在）
node bin/cm.js unreleased
# 输出可能很长时配合分页/截断：
node bin/cm.js unreleased | head -40
```

**需要观察的现象**：哪些包名出现、每个包下面的小节标题（Breaking changes / Bug fixes / New features）与条目内容。

**预期结果**（**待本地验证**，取决于各包仓库当前状态）：若所有包自上次发布后都没有带标记的提交，命令无任何输出并正常退出；若有，则每个有变更的包输出一段与未来 CHANGELOG 正文一致的 Markdown。也可以任选一个包交叉验证：`git -C <包目录> log --format=%B%n --reverse $(node -p "require('./<包目录>/package.json').version")..main` 手工重算该包的变更区间。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `unreleased` 的输出里看不到版本号和日期，而这并不是缺陷？
**答案**：下一版本号由 `bumpVersion` 依据变更内容决定，预览阶段尚未计算；日期在发布时刻才确定。两者都只出现在被丢弃的 `head` 里，`unreleased` 只消费 `body`。

**练习 2**：`changelog()` 提取不到任何变更的两种原因分别是什么？`unreleased` 如何帮助区分？
**答案**：一是确实没有新提交（正常，无需发布）；二是提交里有变更但标记段落写得不符合规范（如小写 `fix:` 或未独立成段），被正则静默丢弃。跑 `unreleased` 后对照 `git log` 里「明明有实质改动却没出现在预览里」的包，即可定位第二种情况。

**练习 3**：`unreleased` 与 `doRelease` 各调用一次 `releaseNotes`，两次调用可能产生差异吗？
**答案**：函数本身是纯函数（u3-l1 结论），差异只可能来自输入：两次调用之间又出现了新提交、或发布了 `--version` 指定的特殊版本导致实际版本与推演不同、或经 `--edit` 人工修订过正文。排除这些情况，预览正文就是最终写入 CHANGELOG 与 tag 注释的正文。

## 5. 综合实践

**任务：在沙盒仓库完整演练两轮发布，亲手复现 `doRelease` 的全部产物，并验证「版本号一物三用」的闭环。**

前置：一个临时目录（如 `/tmp/rel-full`），git 已配置用户信息。以下命令均不触碰本仓库。

**第 1 轮（0.1.3 → 0.2.0，breaking 发布）**

1. 建仓、写入 `{"name": "@codemirror/sandbox", "version": "0.1.3"}` 的 package.json，提交并 `git tag 0.1.3`（手工补上 `doRelease` 在上一轮才会留下的 tag，让闭环转起来）。
2. 按 4.2.4 的方式构造两条带标记提交（一条 FEATURE+FIX、一条 BREAKING）。
3. 写一个演练脚本（示例代码），把 `changelog`、`bumpVersion`、`releaseNotes` 三个函数逐字复制自 cm.js，对沙盒目录执行：打印 `changes`、推演的新版本号、以及 `notes.head + notes.body`。核对：breaking 1 条 → 新版本 `0.2.0`；body 小节顺序为 Breaking changes → Bug fixes → New features。
4. 手工执行 `doRelease` 的写操作：改写 package.json 版本号 → 把上一步的 head+body 写入 `CHANGELOG.md` → `git add` 两个文件 → `git commit -m "Mark version 0.2.0"` → `git tag 0.2.0 -m "Version 0.2.0\n\n<body>" --cleanup=verbatim`（多行注释在 shell 里用带空行的引号字符串写出）。
5. 验证：`git tag -n99 0.2.0` 的注释正文与 `cat CHANGELOG.md` 一致；`git log --oneline -2` 顶部的提交是 `Mark version 0.2.0`。

**第 2 轮（0.2.0 → 0.2.1，patch 发布）**

6. 追加一条只含 `FIX:` 段落的提交，重跑演练脚本——注意这次 `changelog` 的区间端点换成 `0.2.0..main`（第 1 轮打的 tag），只包含新提交。
7. 核对推演结果：只有 fix → `0.2.1`。重复步骤 4 打上 `0.2.1` 标签。
8. 验证闭环：`cat CHANGELOG.md` 此时应是 `0.2.1` 条目在前、`0.2.0` 条目在后（前插结构）；`git tag` 列出 `0.1.3`、`0.2.0`、`0.2.1` 三个标签；`git log --format=%B%n --reverse 0.2.0..main` 为空（区间已被新 tag 覆盖）。

**验收标准**：能不查讲义地说出——两次版本号分别由决策表哪一行得出；CHANGELOG 为什么新条目在上；tag 注释与 CHANGELOG 正文为什么一字不差；第二轮的变更区间为什么恰好只含新提交。全部命令输出为按源码推演的预期，编写本讲时未实际运行，**待本地验证**。

## 6. 本讲小结

- `bumpVersion` 内置两套规则：0.x 只有「breaking 升 minor、其余升 patch」两种结局（feature 有意降级）；正式版才是 breaking/feature/fix 三级的标准 SemVer；三类全空抛 `"No new release notes!"`，异常统一汇入 `error()`。
- 版本号字符串一物三用：package.json 的 version 字段、git tag 名、下一轮 `changelog` 的区间端点（`版本号..main`）——三者互为镜像，构成发布闭环；`changelog` 必须在 `setModuleVersion` 之前调用，区间才正确。
- `doRelease` 的产物只是**本地**的一个 `Mark version <v>` 提交和一个附注 tag（注释 = `Version <v>` + 发布说明正文，`--cleanup=verbatim` 保真）；CHANGELOG 采取新条目前插的倒序结构；不含 `git push` 与 `npm publish`。
- `--edit` 走 `editReleaseNotes`：草稿写到中央仓库根的 `notes.txt`，`$EDITOR`（缺省 emacs）修订；清空文件即以退出码 0 静默取消，且因调用时机在一切 git 写操作之前，取消不留痕迹。
- `updateDependencyVersion` 用字符串正则批量改写其他包的依赖为 `"^<新版本>"`，`--amend` 分支把同轮多次联动合并成一条提交；整个联动因「误判 major 时制造大面积混乱」被 `if (false && ...)` 停用，触发条件 `mainVersion` 提取的正是 `^` 范围的不兼容边界（0.x 的 `0.MINOR`、正式版的 major）。
- `cm unreleased` 遍历所有包预览未发布变更（只消费 `releaseNotes` 的 body），是对「标记写错被静默丢弃」这一上游风险的事前兜底。

## 7. 下一步学习建议

- 下一篇讲义 **u3-l3「文档流水线：build-readme 如何生成带 API 文档的 README」**：看发布体系之外的另一条生成型流水线——`getdocs-ts` 收集类型注释、`builddocs` 渲染 HTML、三级 import 解析器生成跨包文档链接，风格与本章的 `releaseNotes` 一脉相承。
- 在本地 `cm install` 之后做一次真实对照：任选一个包（如 `state`），`git -C state tag | tail -5` 看历史版本号标签，打开它的 `CHANGELOG.md` 与 `git tag -n99 <某版本>` 对比——你会看到与本章沙盒演练完全相同的结构。
- 通读 [bin/cm.js:L162-L288](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L162-L288) 这一段连续源码（changelog → unreleased），体会「数据提取（changelog）→ 决策（bumpVersion）→ 渲染（releaseNotes）→ 落盘（doRelease）」的分层数据流，为 u3-l4 的二次开发实战做准备。
