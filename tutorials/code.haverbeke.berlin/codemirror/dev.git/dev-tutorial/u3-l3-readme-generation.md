# 文档流水线：build-readme 如何生成带 API 文档的 README

## 1. 本讲目标

每个 `@codemirror/*` 外围包仓库根目录的 `README.md` 里，`## API Reference` 之后那一大段 API 文档不是人手写的，而是由本仓库的 `bin/build-readme.js` 从 TypeScript 源码的文档注释里自动抽取、渲染成 HTML、再拼接回模板的。学完本讲，你应该能够：

1. 读懂包内 `src/README.md` 模板中 `@引用` 占位符的工作方式——那条 `/(^|\n)@[^]*@\w+|\n@\w+/` 正则如何圈出「将被文档替换的区域」，以及 `$$$` 替换点如何被精确安放。
2. 看懂 `imports` 三级解析器数组：兄弟核心包导向 codemirror.net、外部生态导向 lezer 文档站 / LSP 规范、兜底的 `browserImports`，以及把 `href` 与 `id` 改写成 `user-content-` 前缀锚点的三步后处理。
3. 区分普通包与 `legacy-modes` 在文档生成上的两条代码路径：一个入口的 `gather` 对多入口的 `gatherMany`，模板切割对模板尾接。

本讲的主角是一个 65 行的脚本，但它是「文档即代码」理念在这个项目里最完整的落地。

## 2. 前置知识

### 2.1 两个上游工具：getdocs-ts 与 builddocs

`bin/build-readme.js` 顶部 require 了两个不在本仓库里的工具，它们都登记在本仓库的开发依赖里（[package.json:15-16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L15-L16) —— `getdocs-ts` 与 `builddocs`）。从本脚本对它们的调用方式可以确定各自的分工：

| 工具 | 本脚本用到的 API | 观察到的职责 |
| --- | --- | --- |
| getdocs-ts | `gather({filename, basedir})` | 解析一个 TS 入口文件，收集其中带文档注释的导出条目 |
| getdocs-ts | `gatherMany(mods)` | 一次解析多个入口（`mods` 是 `{name, filename, basedir}` 数组），返回与输入对齐的条目集数组 |
| builddocs | `build(options, items)` | 把条目集渲染成 HTML 片段（签名、参数、文档注释） |
| builddocs | `browserImports` | 内置的 import 解析器，作为三级解析链的兜底 |

这两个包的内部实现不在本仓库范围内，本讲只依据 `build-readme.js` 的**调用方式**来描述它们；`cm install` 之后，读者可以在 `node_modules/getdocs-ts` 与 `node_modules/builddocs` 里读到完整源码验证。

### 2.2 README 模板的约定

每个外围包仓库里，`src/README.md` 是**模板**：一段普通的 Markdown（标题、徽章、简介、Usage 示例），末尾 `## API Reference` 标题下列出若干个 `@名字` 形式的引用，每个独占一行。下面是真实模板的骨架（节选自 lang-html 包仓库上游镜像的 `src/README.md`；`cm install` 后本地对应 `<仓库根>/lang-html/src/README.md`）：

```markdown
# @codemirror/lang-html [![NPM version](...)]

[ [**WEBSITE**](...) | **ISSUES** | **FORUM** | **CHANGELOG** ]

This package implements HTML language support for the
[CodeMirror](https://codemirror.net/) code editor.
（……中间是简介、许可证、行为准则等散文……）

## Usage

```javascript
import {EditorView, basicSetup} from "codemirror"
import {html} from "@codemirror/lang-html"
（……示例代码……）
```

## API Reference

@html

@htmlLanguage

@htmlCompletionSource

@TagSpec

@htmlCompletionSourceWith

@autoCloseTags
```

`build-readme` 的工作就是：保留模板里 `@引用` 之前的全部 Markdown，把 `@引用` 块替换为从 TS 源码渲染出来的 HTML 文档，写进包仓库根目录的 `README.md`。**根 README.md 是生成物**——直接改它会在下次生成时被覆盖，要改介绍文字得改模板。

### 2.3 GitHub 锚点与 `user-content-` 前缀

GitHub 渲染 README 时会对内嵌 HTML 做安全净化，其中一个副作用是：HTML 里的 `id` 锚点会被加上 `user-content-` 前缀。如果生成端老老实实写 `id="html"`，GitHub 渲染后实际锚点变成 `user-content-html`，页内链接 `href="#html"` 就跳不到目标。所以脚本干脆**在生成端主动预演 GitHub 的改写**：`id` 与页内 `href` 统一写成 `user-content-` 前缀 + 小写。源码文件头第 1 行的注释把这一目标概括为一个词：[bin/build-readme.js:1-2](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L1-L2) —— "build **github-proof** readmes that contain the package's API docs **as HTML**"。

### 2.4 承接前几讲

- **u2-l1** 讲过 `bin/packages.js`：`core` 是 12 个核心包名单、`nonCore` 是 24 个外围包名单、`Pkg` 类给出 `pkg.dir`（包仓库在本仓库根下的目录）与 `pkg.main`（探测出的 TS 入口），`loadPackages()` 返回 `packages / packageNames / buildPackages` 三件套。本讲大量消费这些结构。
- **u1-l3** 讲过 `cm.js` 的命令表、`cmdFn.length > args.length` 的参数下限校验、以及 `error()` 统一错误出口。本讲的命令接线完全建立在其上。
- **u1-l2** 讲过 `cm install` 装配出的多仓库布局：36 个包目录是兄弟目录、依赖经 npm workspaces 互联。本讲第一个 import 解析器之所以用 `../<包名>/` 前缀判断，正是这个布局的直接产物。

## 3. 本讲源码地图

| 位置 | 行号 | 在本讲中的角色 |
| --- | --- | --- |
| `bin/build-readme.js` | 9-65 | **本讲主角** `buildReadme(pkg)`：模板 → HTML → 后处理 → README |
| `bin/build-readme.js` | 10-23 | `imports` 三级解析器数组 |
| `bin/build-readme.js` | 25-26 | 模板读取（legacy-modes 与普通包路径不同） |
| `bin/build-readme.js` | 28-42 | legacy-modes 分支：`gatherMany` 多入口 |
| `bin/build-readme.js` | 43-52 | 普通包分支：占位符提取与 `gather` 调用 |
| `bin/build-readme.js` | 55-64 | HTML 后处理与 `$$$` 模板替换 |
| `bin/cm.js` | 346-350 | 命令包装：准入校验 + 写文件 |
| `bin/cm.js` | 13-36 | 命令分发骨架（u1-l3 已讲，本讲只看接线点） |
| `bin/packages.js` | 3-16 | `core` 名单——解析器与 href 改写共用的判据 |
| `bin/packages.js` | 46-59 | `Pkg` 类——`pkg.dir`、`pkg.main` 的来源 |
| `package.json` | 15-16 | getdocs-ts / builddocs 两个文档工具依赖 |

## 4. 核心概念与源码讲解

### 4.1 流水线全景：一条命令如何变成带文档的 README

#### 4.1.1 概念说明

外围包的 API 文档面临一个经典的「双宿主」问题：同一份文档既要出现在 npm 与 GitHub 的 README 里（用户第一眼看到的地方），核心包的文档又统一住在 codemirror.net 文档站。手写两份必然失同步，所以这个项目选择：**文档只写在 TS 源码的文档注释里**，README 的文档区由脚本生成，跨包链接由脚本按「目标属于谁」改写到正确的站点。

整条流水线只有一个入口命令：`cm build-readme <pkg>`。它与 u3-l1 讲过的 `release` 流水线同住 `cm.js`，但职责独立——那条挖掘提交历史生成 CHANGELOG，这条抽取类型注释生成 README。

#### 4.1.2 核心流程

```
node bin/cm.js build-readme lang-html
  │
  │  cm.js：assertInstalled → 命令表查到 buildReadme → nonCore 准入校验
  ▼
buildReadme(pkg)                      （bin/build-readme.js）
  │
  │ ①读模板：pkg.dir/src/README.md（legacy-modes 读 pkg.dir/mode/README.md）
  │ ②收集条目：gather({filename: pkg.main, basedir: pkg.dir})
  │            （legacy-modes：扫描 mode/*.d.ts → gatherMany）
  │ ③渲染：build({mainText: 占位符块, anchorPrefix, imports}, items) → HTML
  │ ④后处理：剥 span → id 加 user-content- → href 三分支改写
  ▼
template.replace("$$$", html) → 写回 pkg.dir/README.md
```

伪代码（普通包路径，抽象掉细节）：

```text
读模板
占位块 = 正则从模板里圈出「@引用 区」
items  = gather(包的 TS 入口)                # 带文档注释的导出条目
html   = build(占位块, items, imports)       # 每个 @引用 展开成一个 API 小节
html   = 后处理(html)                        # 对齐 GitHub 锚点约定
模板   = 把占位块换成 "$$$"
返回   = 模板.replace("$$$", html)
```

#### 4.1.3 源码精读

先看 cm.js 这一侧的接线。命令表里注册的一行：

[bin/cm.js:29](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L29) —— 把 `build-readme` 子命令映射到本地函数 `buildReadme`（注意键名带连字符，所以写成字符串键）：

```js
"build-readme": buildReadme,
```

帮助文本里的说明：

[bin/cm.js:49](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L49) —— 一句话说明它只服务于**非核心**包（这是本讲反复出现的一条分界线）：

```text
cm build-readme <pkg>   Regenerate the readme file for a non-core package
```

命令包装本体：

[bin/cm.js:346-350](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L346-L350) —— 三步：准入校验、查包、把生成结果写到包仓库根目录的 README.md：

```js
function buildReadme(name) {
  if (!nonCore.includes(name)) help(1)
  let pkg = packageNames[name]
  fs.writeFileSync(join(pkg.dir, "README.md"), require("./build-readme").buildReadme(pkg))
}
```

- **第一行：准入。** 核心包名（state、view……）直接 `help(1)`——打印用法并以退出码 1 结束。核心包的 API 文档住在 codemirror.net，不在各自仓库 README 里，所以根本不适用这条流水线；这也与 4.5 里「核心包链接全部外抛到 codemirror.net」的改写规则互为表里。
- **第二行：查表。** `packageNames` 是 u2-l1 讲过的名字 → `Pkg` 实例映射（[bin/packages.js:62-67](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L62-L67)）。既然上一行已确保 `name` 在 `nonCore` 里，这里必然命中，不会是 undefined。
- **第三行：惰性 require + 写文件。** `require("./build-readme")` 放在函数体内而不是文件顶部，呼应 cm.js 开头 [bin/cm.js:3-4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L3-L4) 的约定：cm.js 自己必须在 `node_modules` 存在**之前**就能运行（`cm install` 本身要用它）；而 `build-readme.js` 顶部就 require 了 getdocs-ts / builddocs，只有真正执行这个子命令、node_modules 确定就绪时才加载它。写到 `pkg.dir/README.md`（包仓库根），不是模板所在的 `src/README.md`——再次强调「根 README 是生成物」。

再看 build-readme.js 的骨架：

[bin/build-readme.js:4-7](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L4-L7) —— 四组依赖：本仓库的包名单、两个文档工具、Node 内置的 path/fs：

```js
const {core} = require("./packages")
const {gather, gatherMany} = require("getdocs-ts")
const {build, browserImports} = require("builddocs")
const {join} = require("path"), fs = require("fs")
```

[bin/build-readme.js:9](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L9) —— 整个模块只导出一个函数，输入是 u2-l1 的 `Pkg` 实例：

```js
exports.buildReadme = function(pkg) {
```

#### 4.1.4 代码实践

**实践目标**：在不生成任何文档的前提下，摸清命令的守卫链——参数校验、安装守卫、nonCore 准入分别在什么时机拦截什么输入。

**操作步骤**（前置：u1-l2 的 `cm install` 已完成；若尚未安装，第 1 步就能观察到另一层守卫）：

```bash
# 1. 未安装包仓库时（若已安装可跳过）：观察 assertInstalled 的拦截
node bin/cm.js build-readme lang-html
#    预期打印 "module state is missing. Did you forget to run 'cm install'?" 并退出

# 2. 无参数：观察 cmdFn.length 校验
node bin/cm.js build-readme
#    预期打印帮助文本、退出码 1

# 3. 传入核心包名：观察 nonCore 准入
node bin/cm.js build-readme state
#    预期同样打印帮助文本、退出码 1

# 4. 查看帮助里的条目
node bin/cm.js --help
```

**需要观察的现象**：第 2、3 步失败方式完全相同（帮助文本 + 退出码 1），但拦截点不同——第 2 步被 [bin/cm.js:34](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L34) 的 `cmdFn.length > args.length` 拦下（`buildReadme` 形参 1 个、实参 0 个），第 3 步通过了参数校验、被 [bin/cm.js:347](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L347) 的 nonCore 判断拦下；第 1 步（若可做）则发生在两者之前的 [bin/cm.js:15](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L15)。

**预期结果**：三层守卫依次是 `assertInstalled`（包目录缺失）→ 参数个数 → nonCore 名单。具体输出以本地运行为准（待本地验证；守卫顺序由 [bin/cm.js:13-35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L13-L35) 的语句顺序唯一确定）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build-readme` 只对 nonCore 包开放，而 `cm release` 对所有包（包括核心包）开放？

**答案**：这是文档宿主的分工——核心包的 API 文档统一渲染到 codemirror.net 文档站，不在各自仓库的 README 里内嵌文档；外围包没有独立文档站，才需要「README 内嵌 HTML 文档」这条流水线。`release` 处理的 CHANGELOG 则每个包仓库都要有，与文档宿主无关。

**练习 2**：`cm.js` 为什么不把 `require("./build-readme")` 提到文件顶部？

**答案**：`build-readme.js` 顶层就 require 了 `getdocs-ts` 和 `builddocs`，它们是 node_modules 里的开发依赖。cm.js 必须在 node_modules 尚不存在时（`cm install` 之前）保持可运行，所以对任何依赖 node_modules 的模块都必须延迟到命令真正执行时再加载——与 u1-l3 讲过的设计约定一致。

**练习 3**：`buildReadme` 的写文件目标为什么是 `pkg.dir/README.md` 而不是 `pkg.dir/src/README.md`？

**答案**：`src/README.md` 是**模板**（人维护的输入），根目录 `README.md` 是**生成物**（脚本输出，npm 与 GitHub 展示的就是它）。二者若混用，下一次生成就可能覆盖掉手写内容。

### 4.2 imports 解析器数组：三级跨包链接策略

#### 4.2.1 概念说明

渲染 `@html` 的文档时，它的签名是 `html(config?: Object = {}) → LanguageSupport`——`LanguageSupport` 定义在核心包 `language` 里，不在 lang-html 自己的条目集中。builddocs 需要为这类「外来类型」生成一个超链接，链到哪儿？这就是 `imports` 数组回答的问题。

`imports` 是 builddocs 的扩展点：一个**按序尝试**的解析器列表。每个解析器收到一个 `type` 对象（含 `type.type`——类型名字符串，和 `type.typeSource`——getdocs-ts 记录的该类型定义所在源文件路径），返回一个 URL 字符串表示「链到这里」，返回 `undefined` 表示「我处理不了，问下一个」。本仓库定制了前两级，第三级交给 builddocs 内置的 `browserImports`。

三级的设计逻辑正是这个生态的链接地图：

| 级别 | 目标 | 链接去向 |
| --- | --- | --- |
| 1 | 兄弟**核心**包（state/view/language…） | codemirror.net 参考文档 |
| 2 | 外部生态（lezer 系、style-mod、LSP 类型） | 各自的文档站 / 规范页 |
| 3 | 其余（浏览器与标准库类型） | `browserImports` 内置规则 |

#### 4.2.2 核心流程

```
builddocs 渲染时遇到一个类型引用 T
  │
  ├─ T 在本文档条目集中？ → 页内锚点（不需要 imports）
  │
  └─ 不在 → 依次调用 imports[0]、imports[1]、imports[2]
        某个返回字符串 → 用它做 href，停止
        全部返回 undefined → 视 allowUnresolvedTypes 决定报错或留死链
```

第 1 级的判断依据值得注意：`type.typeSource` 是**相对于被解析文件的路径**。在中央仓库布局（u1-l2）里，lang-html 引用的 `@codemirror/language` 经 npm workspaces 指向兄弟目录，从 `lang-html/src/` 看过去正是 `../language/...` 形态——所以「路径以 `../<核心包名>/` 开头」就能识别兄弟核心包。

#### 4.2.3 源码精读

第 1 级——兄弟核心包：

[bin/build-readme.js:10-12](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L10-L12) —— 在 `core` 名单里找出与 `typeSource` 前缀匹配的兄弟包，把类型链到 codemirror.net 的 `#包名.类型名` 锚点：

```js
type => {
  let sibling = type.typeSource && core.find(name => type.typeSource.startsWith("../" + name + "/"))
  if (sibling) return "https://codemirror.net/docs/ref#" + sibling + "." + type.type
}
```

三个细节：

- **`type.typeSource &&` 的防御**：`typeSource` 可能为 `undefined`（内置类型等），先短路再调用 `startsWith`，避免 `undefined.startsWith` 抛 TypeError。
- **尾部斜杠消歧**：`startsWith("../language/")` 不会误匹配 `../language-data/`——第 11 个字符是 `/` 不是 `-`。若写成不带斜杠的 `"../" + name`，`language` 就会抢先匹配掉 `language-data` 的路径。`core` 名单（[bin/packages.js:3-16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L16)）里恰好有 `language` 与 `language-data` 这对前缀包含关系，这个斜杠不是装饰。
- **`core.find` 只认 12 个核心包**：引用另一个**外围**包（比如 lang-html 引用 lang-css 的类型）时 `find` 落空，返回 `undefined`，交给下一级——第 2 级也不认识它，最终落到 `browserImports`。对外围包之间的引用没有专门的链接规则。

第 2 级——外部生态，一张逐行查表：

[bin/build-readme.js:13-23](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L13-L23) —— 按 `typeSource` 里出现的模块路径字样，把类型链到四个外部站点：

```js
type => {
  if (/\blezer\/tree\b/.test(type.typeSource)) return `https://lezer.codemirror.net/docs/ref/#tree.${type.type}`
  if (/\blezer\/common\b/.test(type.typeSource)) return `https://lezer.codemirror.net/docs/ref/#common.${type.type}`
  if (/\blezer\/lr\b/.test(type.typeSource)) return `https://lezer.codemirror.net/docs/ref/#lr.${type.type}`
  if (/\blezer\/markdown\b/.test(type.typeSource)) return `https://code.haverbeke.berlin/lezer/markdown#user-content-${type.type.toLowerCase()}`
  if (/\bstyle-mod\b/.test(type.typeSource)) return "https://code.haverbeke.berlin/marijn/style-mod#documentation"
  if (/\bvscode-languageserver-/.test(type.typeSource))
    return `https://microsoft.github.io/language-server-protocol/specifications/specification-current#` +
      type.type[0].toLowerCase() + type.type.slice(1)
  if (type.type == "TextEdit") console.log(type.typeSource, type.type)
}, browserImports]
```

整理成表：

| typeSource 含 | 链接目标 | 锚点形态 |
| --- | --- | --- |
| `lezer/tree`、`lezer/common`、`lezer/lr` | lezer.codemirror.net/docs/ref | `#<模块>.<类型名>`（保留大小写） |
| `lezer/markdown` | code.haverbeke.berlin/lezer/markdown | `#user-content-<小写类型名>` |
| `style-mod` | code.haverbeke.berlin/marijn/style-mod | 固定锚点 `#documentation`（不区分具体类型） |
| `vscode-languageserver-` | LSP 官方规范页 | `#<首字母小写的类型名>`，如 `TextEdit` → `#textEdit` |

两个值得停下来看的点：

- **`lezer/markdown` 一行自己就带了 `user-content-` 前缀和小写化**——因为目标页面正是 GitHub 渲染的文档页，它的锚点天然带前缀。这与 4.5 要讲的「给自家 README 预演 GitHub 改写」是同一个约定的两个应用方向：一处是链接**别人**的 GitHub 锚点，一处是生成**自己**的 GitHub 锚点。
- **第 22 行的 `console.log` 是一处调试残留**：`if (type.type == "TextEdit") console.log(type.typeSource, type.type)`——它对解析结果没有任何影响，只在类型名恰为 `TextEdit` 时打印来源路径。结合下一行 LSP 分支也处理 `TextEdit`，可推测是排查「TextEdit 到底从哪条 import 路径来」时留下的探针。生产上无害，但是阅读真实代码时识别「考古层」的好例子。

第 3 级：

[bin/build-readme.js:23](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L23) —— 数组末尾直接放入 builddocs 导出的 `browserImports`，作为兜底解析器。它的具体映射规则（浏览器/DOM/标准库类型链向何处）属于 builddocs 内部实现，可在安装后阅读 `node_modules/builddocs` 源码确认。

#### 4.2.4 代码实践

**实践目标**：把第 1 级解析器复制出来单独运行，验证「核心包外链、外围包不链」的分界，以及尾部斜杠的消歧作用。这是一段纯函数逻辑，不需要真实的 TS 源码。

**操作步骤**：在仓库根目录执行（**示例代码**，函数体逐字抄自 [bin/build-readme.js:11-12](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L11-L12)）：

```bash
node -e 'const {core} = require("./bin/packages")
let resolve = type => {
  let sibling = type.typeSource && core.find(name => type.typeSource.startsWith("../" + name + "/"))
  if (sibling) return "https://codemirror.net/docs/ref#" + sibling + "." + type.type
}
console.log(resolve({typeSource: "../state/dist/index.d.ts", type: "EditorState"}))
console.log(resolve({typeSource: "../language-data/dist/index.d.ts", type: "languages"}))
console.log(resolve({typeSource: "../language/dist/index.d.ts", type: "LanguageSupport"}))
console.log(resolve({typeSource: "../lang-css/dist/index.d.ts", type: "cssLanguage"}))
console.log(resolve({}))'
```

**需要观察的现象**：五次调用的返回值里，前三个是 codemirror.net 链接，后两个是 `undefined`；特别对比第 2、3 行——`language-data` 与 `language` 这对前缀包含的名字各自解析正确。

**预期结果**（由源码逻辑推演，待本地验证）：

```text
https://codemirror.net/docs/ref#state.EditorState
https://codemirror.net/docs/ref#language-data.languages
https://codemirror.net/docs/ref#language.LanguageSupport
undefined
undefined
```

`../lang-css/` 落空是因为 `core.find` 只遍历 12 个核心包名（lang-css 属于 nonCore）；`{}` 落空是 `type.typeSource &&` 短路。返回 `undefined` 在真实流水线里意味着「交给下一级解析器」。

#### 4.2.5 小练习与答案

**练习 1**：若把第 1 级的 `startsWith("../" + name + "/")` 改成 `startsWith("../" + name)`（去掉尾部斜杠），会发生什么？

**答案**：`name` 为 `language` 时，`../language-data/...` 也会被匹配（`../language` 是它的前缀），且 `core.find` 按 `core` 数组顺序返回第一个命中——`language`（第 3 位）排在 `language-data`（第 9 位）之前，于是 `language-data` 包的类型会被错误地链到 `#language.<类型>` 锚点。尾部斜杠是消歧的关键。

**练习 2**：第 2 级解析器里 `style-mod` 的返回值没有用到 `type.type`，为什么？

**答案**：style-mod 的文档页（code.haverbeke.berlin 的 README）没有为每个导出条目生成稳定锚点，所以所有来自 style-mod 的类型统一链到该页的文档区固定锚点 `#documentation`，不区分具体类型。这属于「链接目标能力所限」的务实处理。

**练习 3**：三级解析器的顺序可以随意调换吗？

**答案**：前两级互不重叠（一个认 `../<核心包>/` 相对路径，一个认外部模块字样），互换无影响；但 `browserImports` 作为兜底必须放最后——它是通用规则，若放前面可能把本应链到 codemirror.net 或 lezer 文档站的类型先「截胡」掉。解析器链的语义是「按序尝试、首个命中生效」。

### 4.3 普通包分支：placeholders 提取与 gather 调用

#### 4.3.1 概念说明

普通包（23 个 lang-\*/theme-\* 包）的模板是 2.2 节展示的形态：一段 Markdown 加末尾的 `@引用` 块。这条分支要做三件事：

1. 用一条正则从模板里**圈出** `@引用` 块（它的原文将成为 builddocs 的 `mainText`，即「文档主体大纲」）；
2. 用 `gather` 从包的 TS 入口收集全部带文档注释的条目；
3. `build` 把大纲里的每个 `@名字` 展开成完整 API 小节，得到 HTML。

`pkg.main` 来自 u2-l1 讲过的探测规则（[bin/packages.js:52-56](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L52-L56)）：`src/` 下唯一 `.ts` 文件优先，其次 `index.ts`，再次剥掉 `theme-`/`lang-` 前缀的同名文件。所以「文档从哪个文件抽」这件事，在 `cm.js` 启动时构造 `Pkg` 实例那一刻就已经定了。

#### 4.3.2 核心流程

```
template = 读 pkg.dir/src/README.md
   │
   ▼
placeholders = /(^|\n)@[^]*@\w+|\n@\w+/.exec(template)
   │  匹配成功 → match[0] = 占位块原文，match.index = 起始下标
   │  匹配失败 → null → 下一行 placeholders[0] 抛 TypeError
   ▼
items = gather({filename: pkg.main, basedir: pkg.dir})
html  = build({mainText: placeholders[0], name, anchorPrefix: "",
               allowUnresolvedTypes: false, imports}, items)
   ▼
template = 前段 + "\n$$$" + 后段     （原占位块被替换点顶替）
```

那条正则是本模块的灵魂，两个分支：

- **主分支 `(^|\n)@[^]*@\w+`**：行首 `@` 开头，中间任意字符（`[^]` 含换行），以「`@` + 单词字符」结尾。`[^]*` 是贪婪量词——先一口吞到字符串末尾，再**回溯**寻找能让后面 `@\w+` 成立的位置，也就是全文**最后一个**「`@` 后跟单词字符」的地方。于是整段匹配 = 从**第一个行首 @** 一直延伸到**最后一个 @单词**。
- **备用分支 `\n@\w+`**：模板里只有一个 `@引用` 时（主分支需要至少两个 `@单词` 才能成立），匹配那单独一行。注意它要求 `@` 前有换行——若唯一的 `@引用` 恰好写在文件第一行行首，两个分支都无法命中。

对 2.2 节的真实模板推演一遍：全文第一个「行首 @」是 `## API Reference` 下的 `@html`（标题行 `# @codemirror/lang-html` 里的 `@` 前面有 `# `，不算行首）；最后一个 `@单词` 是 `@autoCloseTags`。所以占位块 = `\n@html` 到 `@autoCloseTags` 的整段，`@引用` 之间的空行都包含在内；徽章、简介、Usage 示例全部保留在「前段」。

#### 4.3.3 源码精读

[bin/build-readme.js:25-26](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L25-L26) —— 模板路径的三元表达式是两条代码路径的第一个岔口（legacy-modes 的模板不在 `src/` 而在 `mode/` 下）；`html` 是渲染结果的累积器：

```js
let template = fs.readFileSync(join(pkg.dir, pkg.name == "legacy-modes" ? "mode" : "src", "README.md"), "utf8")
let html = ""
```

[bin/build-readme.js:44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L44) —— 圈出占位块，一行正则、无任何防御：

```js
let placeholders = /(^|\n)@[^]*@\w+|\n@\w+/.exec(template)
```

逐 token 拆解：

| 片段 | 含义 |
| --- | --- |
| `(^\|\n)` | 匹配串必须从行首开始（字符串开头或换行之后），捕获的换行计入匹配串 |
| `@` | 第一个行首 @ 引用的 `@` 本体 |
| `[^]*` | 空补集技巧（u3-l1 讲过）：任意字符**含换行**，贪婪吞到底 |
| `@\w+` | 回溯的落点：最后一个「@ + 单词字符」，即块内最后一个 @引用 的名字 |
| `\|\n@\w+` | 备用分支：只有一个 @引用 时，匹配 `\n@名字` 这一小段 |

[bin/build-readme.js:45-51](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L45-L51) —— 用占位块当大纲、入口文件当数据源，渲染出 HTML：

```js
html = build({
  mainText: placeholders[0],
  name: pkg.name,
  anchorPrefix: "",
  allowUnresolvedTypes: false,
  imports
}, gather({filename: pkg.main, basedir: pkg.dir}))
```

- `mainText: placeholders[0]`——builddocs 的大纲输入：块内每个 `@名字` 被展开为该条目的完整小节（标题、签名、参数、文档注释），`typeSource` 等类型引用交给 `imports` 解析成外链。
- `anchorPrefix: ""`——普通包的锚点不加前缀；对比 4.4 里 legacy-modes 用 `mode名.` 前缀防撞名。
- `allowUnresolvedTypes: false`——字面含义：不允许存在解析不了的类型。三级解析器全部返回 `undefined` 的类型会触发失败，而不是悄悄留一个死链接（具体报错形式可在安装后阅读 builddocs 源码确认）。

[bin/build-readme.js:52](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L52) —— 把占位块从模板里挖掉，原位塞进替换点 `$$$`：

```js
template = template.slice(0, placeholders.index) + "\n$$$" + template.slice(placeholders.index + placeholders[0].length)
```

注意 `(^|\n)` 捕获的换行在 `placeholders[0]` 里、随占位块一起被切掉，所以补上的 `"\n$$$"` 让替换点仍然独占一行。`$$$`（三个美元符）是刻意挑的**不太可能在文档散文里出现的记号**，最后由 4.5 的 `template.replace("$$$", html)` 一次性消费。

**脆弱点**：若模板里一个行首 `@` 都没有（比如新建包时忘了写 `@引用`），`exec` 返回 `null`，下一行 `placeholders[0]` 直接抛 `TypeError: Cannot read properties of null`。这个异常沿调用栈上抛，被 [bin/cm.js:35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35) 的 `.catch(e => error(e))` 接住，走 u1-l3 讲过的 `error()` 出口打印并退出。报错信息只有一行 TypeError，不会提示「你的模板缺 @引用」——初次贡献新包时这是一个容易踩的坑。

#### 4.3.4 代码实践

**实践目标**：用一段最小模板亲眼验证正则的匹配边界与切割结果，把「贪婪 + 回溯」从概念变成字节级事实。

**操作步骤**：在仓库根目录执行（**示例代码**，正则与切割逻辑逐字抄自 [bin/build-readme.js:44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L44) 与 [bin/build-readme.js:52](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L52)）：

```bash
node -e 'let t = "# @codemirror/demo\n\nintro\n\n## Usage\n\nexample\n\n## API Reference\n\n@html\n\n@htmlLanguage\n\n@autoCloseTags"
let m = /(^|\n)@[^]*@\w+|\n@\w+/.exec(t)
console.log("index   = " + m.index)
console.log("match   = " + JSON.stringify(m[0]))
console.log("kept    = " + JSON.stringify(t.slice(0, m.index)))
console.log("tail    = " + JSON.stringify(t.slice(m.index + m[0].length)))
console.log("spliced = " + JSON.stringify(t.slice(0, m.index) + "\n$$$" + t.slice(m.index + m[0].length)))'
```

再做两个变体实验：把模板末尾的三个 `@引用` 删到只剩一个 `@html`（观察备用分支接管）；把唯一的 `@html` 挪到文件第一行（观察 `exec` 返回 `null`）。

**需要观察的现象**：① 标题行 `# @codemirror/demo` 里的 `@` **不**触发匹配（不在行首）；② 匹配串从 `\n@html` 的换行开始、到 `@autoCloseTags` 结束，中间空行全在块内；③ `kept` 以 `## API Reference\n` 结尾（保留了第一个换行），`spliced` 里 `$$$` 独占一行紧跟标题后的空行；④ 单引用变体匹配到 `\n@html` 为止；⑤ 首行引用变体打印 `null` 的下标访问报错。

**预期结果**（由正则语义推演，待本地验证）：

```text
index   = 62                        （"## API Reference\n" 之后那个换行的位置）
match   = "\n@html\n\n@htmlLanguage\n\n@autoCloseTags"
kept    = "...## API Reference\n"
tail    = ""
spliced = "...## API Reference\n\n$$$"
```

（`index` 的具体数值随模板长度变化，以本地输出为准。）

#### 4.3.5 小练习与答案

**练习 1**：为什么主分支 `(^|\n)@[^]*@\w+` 需要**两个** `@` 才能命中？模板里只有一个 `@引用` 时靠什么兜底？

**答案**：主分支的结构是「行首 @ + 任意内容 + @单词」——第一个 `@` 是起点标记，结尾还必须有第二个 `@单词`，所以至少要有两个 `@` 引用（或者一个 `@引用` 之后再出现任何 `@单词`）才能成立。单引用模板靠备用分支 `\n@\w+` 兜底；但它要求 `@` 前有换行，唯一引用若在文件第一行则两分支皆空、`exec` 返回 `null`，脚本在 `placeholders[0]` 处抛 TypeError。

**练习 2**：如果模板的 `@引用` 块中间夹了一段散文（比如两个 `@` 引用之间写了一句提示语），会发生什么？

**答案**：散文落在占位块的跨度之内（主分支从第一个行首 @ 贪到最后一个 @单词），因此会作为 `mainText` 的一部分交给 builddocs——按 builddocs 对大纲文本的处理方式，非 `@引用` 的内容会作为普通段落保留在渲染结果里（具体渲染形态待本地验证）。保守的结论是：占位块的边界由「第一个行首 @」与「最后一个 @单词」决定，与语义上的「API Reference 小节」并非严格等同。

**练习 3**：`gather({filename: pkg.main, basedir: pkg.dir})` 的两个参数为什么都需要？只传 `filename` 不行吗？

**答案**：`pkg.main` 是绝对路径（[bin/packages.js:56](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L56) 拼了 `pkg.dir` 与 `src/`），定位入口文件本身够了；但 getdocs-ts 解析入口的 import 时需要一个基准目录来解析相对路径、识别兄弟包（4.2 的 `../state/` 前缀判断正依赖于此），`basedir: pkg.dir` 提供这个基准。两个参数一个定「从哪开始」，一个定「相对谁解析」。

### 4.4 legacy-modes 分支：gatherMany 多入口的特殊路径

#### 4.4.1 概念说明

`legacy-modes` 是全家最特殊的包（u2-l1 讲过）：它是 CodeMirror 5 时代旧模式的集合，没有 `src/` 目录、没有 TS 入口，`Pkg` 构造函数对它**显式跳过**——`pkg.main` 恒为 `null`（[bin/packages.js:51](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L51)），也因此被 `buildPackages = packages.filter(p => p.main)` 排除在构建之外。它的文档源是 `mode/` 目录下的一批 `.d.ts` 声明文件——**几十个模式、几十个入口**，每个模式在 README 里要有自己的一小节。

于是 4.3 的「一个入口 + 模板占位块」模型对它完全不适用，代码走进了另一条分支：扫描全部入口、`gatherMany` 一次收集、逐模式渲染、锚点加前缀防撞名、文档追加到模板**末尾**而不是替换中间的占位块。

#### 4.4.2 核心流程

```
template = 读 pkg.dir/mode/README.md          （注意：mode/ 不是 src/）
   │
   ▼
mods = readdirSync(pkg.dir/mode)
        .filter(以 .d.ts 结尾)
        .map(file => ({name: 去后缀的文件名,
                       filename: mode/file 的绝对路径,
                       basedir: pkg.dir}))
   │
   ▼
items = gatherMany(mods)                       （一次解析全部入口，返回对齐的数组）
   │
   ▼
for 每个 mod：
    html += <h3>mode/<名字></h3> + build({name, anchorPrefix: "名字.", imports}, items[i])
   │
   ▼
template += "\n$$$"                            （替换点追加在模板末尾）
```

#### 4.4.3 源码精读

[bin/build-readme.js:28-32](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L28-L32) —— 扫描 `mode/` 下的 `.d.ts` 声明文件，组装成 `gatherMany` 的入口描述数组：

```js
if (pkg.name == "legacy-modes") {
  let mods = fs.readdirSync(join(pkg.dir, "mode")).filter(f => /\.d\.ts$/.test(f)).map(file => {
    let name = /^(.*)\.d\.ts$/.exec(file)[1]
    return {name, filename: join(pkg.dir, "mode", file), basedir: pkg.dir}
  }), items = gatherMany(mods)
```

- 过滤条件是 `.d.ts`（编译/声明文件）而非 `.ts`——文档从声明文件抽取。`mode/` 目录里实际有哪些文件，`cm install` 后 `ls legacy-modes/mode/` 一看便知（待本地验证）。
- `name` 取文件名去掉 `.d.ts`，既是小节标题也是锚点前缀。

[bin/build-readme.js:33-41](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L33-L41) —— 逐模式渲染，每段前面手工拼一个 HTML 小节标题：

```js
for (let i = 0; i < mods.length; i++) {
  let {name} = mods[i]
  html += `\n<h3 id="${name}">mode/<a href="#${name}">${name}</a></h3>\n` + build({
    name: pkg.name,
    anchorPrefix: name + ".",
    allowUnresolvedTypes: false,
    imports
  }, items[i])
}
```

两个设计点：

- **`anchorPrefix: name + "."`**：几十个模式的导出名可能撞车（多个模式都可能导出 `StreamParser` 类型的实例），给每个模式的条目锚点加上 `模式名.` 前缀，保证 README 内锚点唯一。对比普通包的 `anchorPrefix: ""`——单入口包不存在撞名问题。
- **手拼 `<h3>`**：小节标题不是 builddocs 渲染的，而是模板字符串直接拼的 HTML，`id` 与 `href` 此刻还是「裸」形态，等 4.5 的后处理统一加前缀。

[bin/build-readme.js:42](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L42) —— 替换点直接**追加**到模板末尾：

```js
template += "\n$$$"
```

legacy-modes 的模板（`mode/README.md`）里没有 `@引用` 占位块，所以不切割、只尾接——全部模式的文档小节按扫描顺序排在模板正文之后。

两条路径的对照表：

| 维度 | 普通包分支 | legacy-modes 分支 |
| --- | --- | --- |
| 模板位置 | `src/README.md`（L25 三元表达式左支） | `mode/README.md`（右支） |
| 文档来源 | `gather` 单入口 `pkg.main` | `readdirSync` + `gatherMany` 多入口 |
| 渲染调用 | `build({mainText: 占位块, anchorPrefix: ""}, items)` | 循环 `build({anchorPrefix: 名字.}, items[i])`，各配手拼 `<h3>` |
| 替换点位置 | 原占位块处（L52 切割拼接） | 模板末尾（L42 尾接） |
| 模板中占位符 | `@引用` 块，必须有 | 无，不需要 |

#### 4.4.4 代码实践

**实践目标**：观察 legacy-modes 分支的产物形态——多小节、模式名前缀锚点、文档追加在模板之后。

**操作步骤**（前置：`cm install` 已完成）：

```bash
# 1. 先看文档源的形态
ls legacy-modes/mode/*.d.ts | head -5
wc -l legacy-modes/mode/README.md

# 2. 生成 README
node bin/cm.js build-readme legacy-modes

# 3. 观察变化
git -C legacy-modes diff --stat README.md
git -C legacy-modes diff README.md | head -60

# 4. 数一数小节标题
grep -c '<h3 id=' legacy-modes/README.md
```

**需要观察的现象**：`README.md` 的 diff 集中在**文件末尾追加**的一大段 HTML；每个模式一个 `<h3 id="user-content-<模式名>">mode/<a href="#user-content-<模式名>">…</a></h3>` 小节（4.5 的后处理会给手拼的 `id`/`href` 统一加上前缀）；模式内条目的锚点带 `模式名.` 前缀。

**预期结果**：`<h3 id=` 的出现次数应与 `mode/` 下 `.d.ts` 文件数量一致；diff 不触碰模板正文（`mode/README.md` 本身不变）。具体模式列表与数量以本地为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 legacy-modes 用 `gatherMany(mods)` 一次收集，而不是循环调用多次 `gather`？

**答案**：从调用形态看，`gatherMany` 接收入口描述数组、返回与输入对齐的条目集数组——把「多入口」当作一等场景。这样多个模式文件若引用相同的依赖，解析工作可以共享，也保证了各模式条目集与 `mods` 数组下标一一对应（循环里 `items[i]` 直接配对）。逐个 `gather` 在语义上也能实现，但放弃了批量接口的意图（其内部差异可在安装后阅读 getdocs-ts 源码确认）。

**练习 2**：`anchorPrefix` 为什么用「模式名」而不是「包名」作前缀？

**答案**：撞名发生在**模式之间**（几十个入口各自的导出），不是包之间——README 是 `legacy-modes` 一个包内的单页文档，用模式名前缀（如 `shell.`）即可隔离不同模式的同名条目；包名前缀对页内唯一性没有帮助。普通包单入口、无撞名，所以前缀为空串。

**练习 3**：legacy-modes 分支如果也走 L44 的占位正则会发生什么？

**答案**：它的模板 `mode/README.md` 里没有 `@引用`（替换点是代码尾接的，不是模板里写的），正则 `exec` 返回 `null`，`placeholders[0]` 抛 TypeError。这正是 L28 用 `if (pkg.name == "legacy-modes")` 提前分流的原因之一——两条路径对模板的约定根本不同。

### 4.5 HTML 后处理与模板替换：github-proof 的三步改写

#### 4.5.1 概念说明

builddocs 吐出的 HTML 是「通用形态」：`id` 与页内 `href` 是裸锚点，签名里可能带 `<span>` 标签。但这份 HTML 的最终宿主是 GitHub 渲染的 README，一个 HTML 净化规则很严格的环境。所以拼接之前，脚本用三个链式 `replace` 做后处理：

1. **剥掉所有 `<span>` 标签**——GitHub 环境里自定义样式类没有意义，留着只是噪音与体积；
2. **`id` 统一改成 `user-content-<小写>`**——预演 GitHub 给锚点加前缀的改写；
3. **页内 `href="#..."` 按目标归属三分**——核心包外抛 codemirror.net、本包带前缀的剥前缀、其余落回 `user-content-` 页内锚点。

最后一步 `template.replace("$$$", html)` 把后处理完的 HTML 拼进模板的替换点，`buildReadme` 返回成品，由 cm.js 写盘。

#### 4.5.2 核心流程

```
html（builddocs 原始输出）
  │
  │ ①删除所有 <span> / </span> 开闭标签
  │ ②id="X"        → id="user-content-x"（小写）
  │ ③href="#X" 拆出首段 first：
  │     first ∈ core 名单     → href="https://codemirror.net/docs/ref/#X"
  │     first == 包名且有后续  → 剥掉「包名.」前缀 → href="#user-content-<剩余小写>"
  │     其他                  → href="#user-content-<小写>"
  ▼
return template.replace("$$$", html)     （替换第一个也是唯一的 $$$）
```

第 ③ 步的 `first` 是锚点 id 的**第一段**：builddocs 生成的锚点 id 里，`.` 与 `^` 用作条目路径的分隔符（例如 `state.EditorState` 表示 state 包的 EditorState），`/^[^^.]*/` 取第一个分隔符之前的片段来判断「这个链接指向谁」。

#### 4.5.3 源码精读

[bin/build-readme.js:55-62](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L55-L62) —— 三步后处理，一次链式完成：

```js
html = html.replace(/<\/?span.*?>/g, "")
  .replace(/id="(.*?)"/g, (_, id) => `id="user-content-${id.toLowerCase()}"`)
  .replace(/href="#(.*?)"/g, (_, id) => {
    let first = /^[^^.]*/.exec(id)[0]
    if (core.includes(first)) return `href="https://codemirror.net/docs/ref/#${id}"`
    if (first == pkg.name && id.length > first.length) id = id.slice(first.length + 1)
    return `href="#user-content-${id.toLowerCase()}"`
  })
```

**第 55 行：剥 span。** `/<\/?span.*?>/g` 匹配 `<span...>` 或 `</span>`（`\/?` 兼容闭斜杠、`.*?` 惰性吃掉标签内属性、`g` 全局）。只删标签本身，标签包裹的文字原样保留。

**第 56 行：改 id。** 所有 `id="..."`（包括 4.4 手拼的 `<h3 id="模式名">`）统一变成 `id="user-content-<小写>"`。这一步对 ②③ 两步是**先行**的——先把所有 id 定型，再改 href 去对齐它们。

**第 57-62 行：改 href，三分支。** 只处理页内链接（`href="#` 开头）；外链（4.2 解析器产出的完整 URL）不含 `#` 开头的 href 形态，不受影响。

- [第 58 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L58) 的 `/^[^^.]*/` 是个精巧的小正则：字符类里**第一个** `^` 是否定符，**第二个** `^` 是字面量脱字符——整体含义「匹配到第一个 `^` 或 `.` 之前的所有字符」。于是 `first` 就是锚点路径的首段。
- [第 59 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L59)：`first` 是核心包名（`core.includes` 精确匹配 [bin/packages.js:3-16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L16) 的 12 个名字）→ 链接整体外抛到 `https://codemirror.net/docs/ref/#<完整id>`。这与 4.2 第 1 级解析器是同一条规则的两端：解析器管「类型引用」的链接，这里管「页内锚点形态」的链接，殊途同归于 codemirror.net。一个有意思的不对称：此处 URL 是 `docs/ref/#`（带斜杠），解析器 L12 是 `docs/ref#`（不带）——两种写法都指向同一锚点，属无害的不一致。
- [第 60 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L60)：`first` 等于**本包名**且 id 比包名长（即 `包名.条目` 形态）→ 剥掉 `包名.` 前缀再走页内锚点。本包条目的锚点本来就没有前缀（4.3 的 `anchorPrefix: ""`），这个分支把「带包名前缀的引用」归一化成页内可跳转的形态。
- [第 61 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L61)：其余情况（本包裸条目名、或指向其他外围包的引用）一律 `#user-content-<小写>`。注意对「指向其他外围包」的链接没有专门规则——是否真的出现这类链接取决于各包模板与条目引用形态（待本地验证）。

[bin/build-readme.js:64](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L64) —— 最后的拼接，返回给 cm.js 写盘：

```js
return template.replace("$$$", html)
```

`String.prototype.replace` 以字符串为第一参数时替换**第一个**匹配项。普通包模板里的 `$$$` 在原占位块处（L52 塞入），legacy-modes 的在末尾（L42 尾接），每份模板恰有一个，一次替换即完成。

一个值得知道的语言细节：`replace` 的**替换串**里 `$` 有特殊含义（`$$` 插入一个 `$`、`$&` 插入匹配串）。此处 `html` 是替换串，若 builddocs 输出的 HTML 里恰好含有 `$$` 或 `$&` 这类序列，会被意外改写——文档 HTML 里出现这些序列的概率极低，实践中从未构成问题，但读代码时能意识到这一点，才算真正读懂了这一行。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：完整跑一次 spec 规定的任务——任选一个 nonCore 包生成 README，用包仓库内的 git diff 观察新增 HTML，归纳锚点规律，再回到模板找占位符原文。

**操作步骤**（前置：`cm install` 已完成；以 lang-html 为例，可换成任何 nonCore 包）：

```bash
# 1. 先读模板，人工标出占位块
less lang-html/src/README.md          # 找到 "## API Reference" 下的 @html、@htmlLanguage 等行

# 2. 生成 README
node bin/cm.js build-readme lang-html

# 3. 在包仓库里看 diff
git -C lang-html diff README.md

# 4. 归纳锚点规律（三条 grep 各对应一个观察点）
grep -o 'id="[^"]*"' lang-html/README.md | head -10          # 观察 id 前缀与小写
grep -o 'href="[^"]*"' lang-html/README.md | sort -u | head -20
grep -c '<span' lang-html/README.md                          # 预期为 0

# 5. 对照模板：占位块去哪了
grep -n 'API Reference' lang-html/README.md
```

**需要观察的现象**：

1. diff 只动 `## API Reference` 之后的区域：原来那批 `@html`、`@htmlLanguage`……占位行消失，原位出现一段 HTML 文档；徽章、简介、Usage 示例分毫未动。
2. 所有 `id` 形如 `id="user-content-html"`、`id="user-content-tagspec"`——一律 `user-content-` 前缀 + 全小写（模板里的 `@TagSpec` 变成 `tagspec`）。
3. `href` 呈三类：指向 `https://codemirror.net/docs/ref/#...` 的（引用了核心包类型，如 `LanguageSupport` → language 包）；指向 `https://lezer.codemirror.net/...` 或 LSP 规范的（4.2 第 2 级解析器的产出）；留在页内的 `#user-content-<小写>`。
4. `<span` 计数为 0——第 55 行的剥除生效。
5. 一个提示：上游镜像当前的 README 是项目迁移到 code.haverbeke.berlin 之后**新版文档工具**生成的 Markdown 风格文档区；本仓库快照（本讲分析的 HEAD）的脚本生成的是内嵌 HTML 的形态。本地首次重新生成时，diff 很可能表现为「Markdown 文档区整体替换为 HTML 文档区」——这不是出错，恰是观察脚本版本演进的好机会。

**预期结果**：模板中 `@html` 等占位行的原文与生成后 `id="user-content-html"` 等锚点一一对应；`grep -n 'API Reference'` 显示标题行保留在原位、HTML 文档紧随其后。具体 diff 内容以本地为准（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `id` 和页内 `href` 要**同时**加 `user-content-` 前缀？只改一边会怎样？

**答案**：`id` 是锚点的定义、`href` 是锚点的引用，二者必须精确一致才能跳转。GitHub 渲染时会自动给 `id` 加 `user-content-` 前缀但**不会**同样改写 `href`——若生成端不改，渲染结果是 `id="user-content-html"` 对 `href="#html"`，链接全部落空。既然 GitHub 的行为不可控，脚本就在生成端把两边都写成最终形态：`id` 预演 GitHub 的改写，`href` 主动对齐改写后的目标。

**练习 2**：`id` 改写用了**惰性** `(.*?)` 而剥 span 用了 `.*?`、切首段的 `/^[^^.]*/` 则没有量词——第 56 行 `id="(.*?)"` 的惰性在这里必要吗？

**答案**：必要性与输入形态有关。`id="..."` 的值里不会出现引号，惰性与贪婪都能匹配到下一个引号；但一行 HTML 里可能有多个 `id="..."`（或 id 之后还有其他 `="..."` 片段）时，贪婪 `(.*)"` 会一直吃到**最后一个**引号导致跨段误配。惰性让每次匹配严格落在最近的一对引号之间，是这类「属性值提取」正则的稳妥写法。

**练习 3**：如果生成的 HTML 里恰好包含字面量 `$$$`，拼接结果会怎样？

**答案**：`template.replace("$$$", html)` 替换的是**模板**里的第一个 `$$$`，替换串是 `html`——所以 HTML 内容里就算有 `$$$` 也不会被当成替换点（replace 只扫模板）。真正的风险在替换串的特殊 `$` 序列：`html` 里若有 `$$` 会被替换成一个 `$`、`$&` 会被替换成 `$$$` 本身。文档 HTML 里出现这些序列的概率几乎为零，但这是理解 `String.replace` 字符串替换语义的好例子。

## 5. 综合实践

把五个模块串成一次完整的「文档生成考察」。前置：`cm install` 已完成（u1-l2）。选一个小外围包（推荐 `theme-one-dark`，条目最少）与 `legacy-modes`（多入口路径）各做一轮：

1. **画替换地图**：打开 `<pkg>/src/README.md`，用纸笔把模板分成三段——「保留的前段 / 被替换的占位块 / 尾段」，并在占位块上标出第一个行首 `@` 与最后一个 `@单词` 的位置（对照 4.3 的正则语义）。
2. **预测锚点**：不看生成结果，先按 4.5 的三分支规则，手写预测 3 个 `id` 与 3 个 `href` 的最终形态（记得小写化与前缀）。
3. **生成并核对**：执行 `node bin/cm.js build-readme <pkg>`，用 `git -C <pkg> diff README.md` 对照你的两张「地图」与预测；找出至少一个 codemirror.net 外链、一个页内 `user-content-` 锚点，并说明它们各自命中了哪条规则（第 59 行 / 第 61 行）。
4. **多入口轮**：对 legacy-modes 重复第 3 步，确认 `<h3 id="user-content-<模式名>">` 小节数量与 `mode/*.d.ts` 文件数一致、模式内条目锚点带 `模式名.` 前缀（4.4）。
5. **边界实验**：给 `<pkg>/src/README.md` 的占位块手工加一行 `@<该包某个导出名>`（从 `<pkg>/src/index.ts` 的 export 里挑），重跑 build-readme，观察新条目小节出现、锚点符合规律；然后恢复现场：

   ```bash
   git -C <pkg> checkout -- src/README.md README.md
   ```

6. **收尾**：对照 4.2 的解析器表，把 diff 里出现的每一条外部链接归类（核心包 / lezer / style-mod / LSP / browserImports 兜底），做成一张「本包文档外链清单」。

全部步骤只在本地克隆的包仓库里改动，第 5 步结束务必恢复；不要把生成物 commit 或 push（本讲只做观察实验）。所有观察结果待本地验证。

## 6. 本讲小结

- `cm build-readme <pkg>`（[bin/cm.js:346-350](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L346-L350)）只对 nonCore 包开放：核心包的文档住在 codemirror.net，README 内嵌文档是外围包专属策略；生成物写入包仓库根 `README.md`，模板是 `src/README.md`（legacy-modes 是 `mode/README.md`）。
- `imports` 三级解析器数组（L10-23）：兄弟核心包（`../<包名>/` 前缀，尾部斜杠消歧 `language`/`language-data`）→ codemirror.net；lezer 系 / style-mod / vscode-languageserver 类型 → 各自文档站与 LSP 规范；`browserImports` 兜底。返回 `undefined` 即「交给下一级」。
- 普通包分支：`/(^|\n)@[^]*@\w+|\n@\w+/` 用「贪婪 `[^]*` + 回溯到最后一个 `@单词`」圈出从第一个行首 `@` 到最后一个 `@引用` 的占位块，交给 `build` 当 `mainText` 大纲；占位块原位换成 `$$$`。模板缺 `@引用` 时 `exec` 返回 `null`，`placeholders[0]` 抛 TypeError 走 `error()` 出口。
- legacy-modes 分支：无 `src/`、无 TS 入口、模板无占位块——扫描 `mode/*.d.ts` 组装多入口，`gatherMany` 批量收集，逐模式 `build`（`anchorPrefix: 模式名.` 防撞名）并手拼 `<h3>` 小节标题，`$$$` 尾接在模板末尾。
- 三步后处理（L55-62）实现 "github-proof"：剥 `<span>`；`id` 统一 `user-content-<小写>`；页内 `href` 按首段三分——核心包外抛 `https://codemirror.net/docs/ref/#<id>`、本包前缀剥除、其余页内 `user-content-` 锚点——本质是在生成端预演 GitHub 对 README HTML 的锚点改写。
- 最终 `template.replace("$$$", html)` 一次性拼接（替换串中 `$` 序列有特殊语义，此处侥幸无害）；「根 README 是生成物、文档注释是唯一事实源、跨包链接由规则表集中管理」是这条流水线教给我们的文档工程范式。

## 7. 下一步学习建议

本讲补齐了发布流水线的最后一块拼图：u3-l1 从提交历史挖掘 CHANGELOG，u3-l2 把它变成版本号与发布提交，本讲让 API 文档随源码自动进 README。下一讲 u3-l4（综合实战）将把这些工具链用于真实的二次开发场景——改源码、跑测试、更新文档、走发布流程。在那之前，建议按以下顺序自测与延伸：

1. 重读 [bin/build-readme.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L1-L65) 全文（65 行），不看讲义复述两条分支的差异与汇合点（L55 的后处理对两条路径一视同仁）。
2. 安装后阅读 `node_modules/getdocs-ts` 与 `node_modules/builddocs` 的源码/文档，验证本讲按调用方式推断的行为：`gather` 如何遍历 import、`build` 如何展开 `mainText`、`browserImports` 的映射表、`allowUnresolvedTypes: false` 的确切报错形态。
3. 挑一个模板里 `@引用` 较多的包（如 lang-html 有 6 个），对照生成结果逐个锚点核对 4.5 的三分支预测，误判之处回到对应源码行找原因。
