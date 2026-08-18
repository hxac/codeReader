# 包注册表：packages.js 与 Pkg 模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `bin/packages.js` 导出的 `core` 与 `nonCore` 两组清单各包含哪些包、为什么这样分组。
2. 逐行解释 `Pkg` 构造函数如何为一个包确定**目录路径**（`dir`）与**入口文件**（`main`），特别是 main 探测的三条优先级规则。
3. 区分 `loadPackages()` 返回的三个集合——`packages`、`packageNames`、`buildPackages`——分别被 `cm.js` 里的哪些命令消费。
4. 通过代码实践，把 `Pkg.main` 的运行时探测结果与 `tsconfig.json` 里手工维护的 `paths` 静态映射做交叉验证。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（前面几讲已建立）：

- **本仓库不含编辑器源码**。`bin/cm.js` 是中央装配脚本，三十多个 `@codemirror/*` 包要靠 `cm install` 逐个克隆到仓库根目录下才会出现。
- **cm.js 的命令分发骨架**。`start()` 用一张「命令名 → 函数」的映射表分发子命令（见 [bin/cm.js:L13-L36](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L13-L36)）。
- **CommonJS 模块**。`packages.js` 是一个 CommonJS 模块，用 `exports.xxx = ...` 暴露接口，用 `require("./packages")` 引入。
- **两个 Node 内置模块**：
  - `fs.existsSync(path)` / `fs.readdirSync(path)`：判断路径是否存在 / 列出目录下的条目名（字符串数组）。
  - `path.join(...parts)`：跨平台拼接路径。

另外补充两个本讲会反复出现的术语：

- **入口文件（main）**：一个包的 TypeScript 源码中「从外部 import 时应先加载的那个文件」。构建工具从它出发，递归找到整个包的所有源文件。
- **npm workspaces**：根 `package.json` 里 `"workspaces": ["*"]`（见 [package.json:L22-L24](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L22-L24)）会把根目录下每个克隆出来的包目录收编为工作区，使它们互相引用时无需先发布到 npm。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [bin/packages.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L1-L67) | 包注册表，全仓库唯一的「包清单」数据源 | `core`/`nonCore` 常量、`Pkg` 类、`loadPackages()` |
| [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L1-L366) | 中央 CLI | 消费 `loadPackages()` 三个集合的各个命令 |
| [tsconfig.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L1-L52) | TypeScript 编译配置 | `paths` 字段：`@codemirror/*` → 各包源码入口的静态映射 |

`packages.js` 全文只有 67 行，却承担了「这个项目到底由哪些包组成」这一最基础的问题。它是本仓库数据与逻辑分离设计中的「数据层」。

## 4. 核心概念与源码讲解

### 4.1 core 与 nonCore 两组包清单

#### 4.1.1 概念说明

CodeMirror 6 的三十多个包在维护上分成两类：

- **核心包（core，12 个）**：编辑器赖以运转的骨架——状态（state）、视图（view）、语言基础设施（language）、命令（commands）、搜索、自动补全、语法检查（lint）、协同编辑（collab）、语言数据（language-data）、合并视图（merge）、LSP 客户端（lsp-client），以及把常用扩展打包在一起的 `codemirror`（对应 `basic-setup` 仓库）。
- **外围包（nonCore，24 个）**：具体的语言支持（`lang-javascript`、`lang-python`……）、旧版高亮模式集合（`legacy-modes`）和主题（`theme-one-dark`）。

这个分类不是装饰——`cm.js` 里 `cm build-readme` 命令**只允许**对 nonCore 包运行（见 [bin/cm.js:L346-L350](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L346-L350)），因为核心包的文档走 codemirror.net 的主文档流水线，而外围包的 README 需要内嵌自己的 API 文档。

#### 4.1.2 核心流程

```text
packages.js 被加载
    │
    ├─ exports.core    = [12 个核心包名]
    ├─ exports.nonCore = [24 个外围包名]
    └─ exports.all     = core.concat(nonCore)   ← 36 个名字
```

三个清单都是**纯字符串数组**，不携带任何路径信息——路径是 `Pkg` 构造函数在运行时算出来的。这份数据源被两处共享：`cm.js` 顶层 `require("./packages")`（[bin/cm.js:L9](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L9)），以及 `build-readme.js`（下一讲详解）。

#### 4.1.3 源码精读

先看两组常量的定义：

[bin/packages.js:L1-L16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L1-L16) 引入 `fs` 与 `path.join`，然后声明 12 个核心包名：

```js
const fs = require("fs"), {join} = require("path")

exports.core = [
  "state",
  "view",
  "language",
  "commands",
  "search",
  "autocomplete",
  "lint",
  "collab",
  "language-data",
  "merge",
  "lsp-client",
  "codemirror",
]
```

[bin/packages.js:L17-L44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L17-L44) 声明 24 个外围包名，并把两组拼接成 `all`：

```js
exports.nonCore = [
  "lang-javascript",
  // ……中间 22 项省略……
  "legacy-modes",
  "theme-one-dark"
]

exports.all = exports.core.concat(exports.nonCore)
```

注意两个细节：

1. **数组里的名字是目录名，不完全是 npm 包名**。`"state"` 对应 npm 包 `@codemirror/state`、目录 `<仓库根>/state`；而 `"codemirror"` 对应 npm 包 `codemirror`（无 `@codemirror/` 作用域，见 [tsconfig.json:L25](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L25)），它的 git 仓库名又叫 `basic-setup`（见 [bin/cm.js:L93](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L93)）。同一个东西有「数组项 / 目录名 / npm 包名 / git 仓库名」四种称呼，读代码时要留心。
2. **`legacy-modes` 是唯一的非 TypeScript 包**。它收纳 CodeMirror 5 时代的 JavaScript 高亮模式，因此在本文件后续的 main 探测中被整体跳过。

#### 4.1.4 代码实践

1. **实践目标**：确认清单的实际长度与内容，建立「36 个包」的直观感受。
2. **操作步骤**：
   - 在仓库根目录执行 `node bin/cm.js packages`（该命令的实现见 [bin/cm.js:L106-L108](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L106-L108)，它逐行打印 `packages` 数组里每个包的名字）。
   - 用管道数一下行数：`node bin/cm.js packages | wc -l`。
3. **需要观察的现象**：输出是按 core 在前、nonCore 在后的顺序排列的 36 个名字，第一行是 `state`，最后一行是 `theme-one-dark`。
4. **预期结果**：行数等于 36（12 + 24），与 `exports.all` 的长度一致。
5. 该命令不依赖包是否已克隆（只读数组），可以直接运行；但输出行数依赖你的终端不被换行截断，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cm packages` 在还没执行 `cm install` 时也能正常输出？

答案：`listPackages()` 只遍历 `packages` 数组打印名字（[bin/cm.js:L106-L108](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L106-L108)），从不访问文件系统中的包目录。不过要注意：`start()` 里除 `install` 与 `--help` 外的所有命令（包括 `packages`）都会先经过 `assertInstalled()` 的目录存在性守卫（[bin/cm.js:L13-L15](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L13-L15)），所以「不访问目录」的前提是守卫已经放行——即你已经 install 过。

**练习 2**：如果想知道某个名字属于核心包还是外围包，不运行代码能判断吗？

答案：可以看规律——`lang-` 前缀的 22 个语言包、`theme-` 前缀的 1 个主题包、`legacy-modes` 都是 nonCore；其余 12 个是 core。但 `language` 与 `language-data` 是核心包，`lang-*` 才是外围包，命名上只有一字之差，最可靠的依据始终是 [bin/packages.js:L3-L42](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L42) 这两张清单。

### 4.2 Pkg 构造函数：目录定位与 main 探测

#### 4.2.1 概念说明

只有一个名字数组还不够——每个命令都需要知道「这个包在磁盘的哪里」「它的入口文件是哪个」。`Pkg` 类就是把字符串名字实例化为可操作对象的一层：

- `p.name`：包名（即目录名）。
- `p.dir`：包在磁盘上的绝对路径。
- `p.main`：包入口文件的绝对路径；**可能为 `null`**。

`main` 为什么可能为 `null`？两种情况：

1. 包还没被克隆（目录不存在）——`cm install` 之前的自然状态。
2. 包是 `legacy-modes`——它没有 TypeScript 入口，永远不会被构建。

这个 `null` 语义正是下游 `buildPackages` 过滤的依据，是整个注册表里最关键的设计。

#### 4.2.2 核心流程

构造单个 `Pkg` 的决策流程：

```text
new Pkg(name)
    │
    ├─ dir = <仓库根>/<name>          （__dirname 的上一级）
    ├─ main = null                     （悲观默认值）
    │
    ├─ name == "legacy-modes" ？ ──是──→ 直接结束，main 保持 null
    ├─ 目录不存在？        ──是──→ 直接结束，main 保持 null
    │
    ├─ files = readdirSync(dir/src) 中匹配 /^[^.]+\.ts$/ 的项
    │       （排除 .d.ts、点开头的文件、无扩展名条目）
    │
    └─ main = 按三条规则探测：
         规则 1：src 下只有一个 .ts 文件 → 就是它
         规则 2：存在 index.ts          → index.ts
         规则 3：存在 <去掉 theme-/lang- 前缀的名字>.ts → 该文件
         都不满足 → throw "Couldn't find a main script for " + name
```

三条规则的优先级是**先特殊后一般**：唯一文件是极强的信号，直接采纳；否则按惯例找 `index.ts`；再否则看是否有与包同名的文件。用序数表达，规则 \(k\) 只在规则 \(1 \dots k-1\) 全部落空时才被尝试。

#### 4.2.3 源码精读

[bin/packages.js:L46-L60](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L46-L60) 是 `Pkg` 的全部实现：

```js
class Pkg {
  constructor(name) {
    this.name = name
    this.dir = join(__dirname, "..", name)
    this.main = null
    if (name != "legacy-modes" && fs.existsSync(this.dir)) {
      let files = fs.readdirSync(join(this.dir, "src")).filter(f => /^[^.]+\.ts$/.test(f))
      let main = files.length == 1 ? files[0] : files.includes("index.ts") ? "index.ts"
          : files.includes(name.replace(/^(theme-|lang-)/, "") + ".ts") ? name.replace(/^(theme-|lang-)/, "") + ".ts" : null
      if (!main) throw new Error("Couldn't find a main script for " + name)
      this.main = join(this.dir, "src", main)
    }
  }
}
exports.Pkg = Pkg
```

逐段拆解：

- `join(__dirname, "..", name)`（L49）：`__dirname` 是 `bin/` 目录，`..` 回到仓库根，再拼包名。所以 `Pkg` 天生只服务于「包目录都在仓库根下」这一布局，不能指向任意路径。
- 守卫条件（L51）：`name != "legacy-modes" && fs.existsSync(this.dir)` 用**短路求值**把两个「不探测」的情形一并挡掉。注意顺序——即便目录不存在，`dir` 字段也已被赋值，这正是 `assertInstalled()` 能用 `p.dir` 检查缺失包的原因（[bin/cm.js:L72-L79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)）。
- 过滤正则 `/^[^.]+\.ts$/`（L52）：要求「至少一个非点字符 + `.ts` 结尾」。它一箭三雕——排除 `.d.ts`（类型声明文件不是入口）、排除点开头的隐藏文件、排除子目录名。
- 三元表达式链（L53-L54）：把三条规则压成一行嵌套三元，读的时候从左到右正是优先级从高到低。`name.replace(/^(theme-|lang-)/, "")` 把 `lang-javascript` 变成 `javascript`、`theme-one-dark` 变成 `one-dark`，然后找 `javascript.ts` / `one-dark.ts`。
- `throw`（L55）：目录在、却没有可识别的入口，视为注册表与实际仓库结构脱节，直接抛错。

一个值得注意的执行时机细节：`cm.js` 在**模块顶层**就调用了 `loadPackages()`（[bin/cm.js:L9-L11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L9-L11)），早于 `start()`（[bin/cm.js:L366](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L366)）。所以 L55 的 `throw` 发生在 `start()` 里那个 `new Promise(...).catch(e => error(e))` 的统一错误出口（[bin/cm.js:L35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35)）建立之前，会以未捕获异常的原始栈形式直接崩溃，而不是走 `error()` 的简洁输出。

对照 `tsconfig.json` 的静态映射，可以反推出各包实际命中的规则。例如：

- `@codemirror/view` → `./view/src/index.ts`（[tsconfig.json:L15](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L15)）——命中规则 2（或恰好只有一个文件时的规则 1）。
- `@codemirror/commands` → `./commands/src/commands.ts`（[tsconfig.json:L16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L16)）——`commands` 不以 `theme-`/`lang-` 开头，规则 3 原样找 `commands.ts`。
- `@codemirror/lang-java` → `./lang-java/src/java.ts`（[tsconfig.json:L27](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L27)）——规则 3 剥掉 `lang-` 前缀后找到 `java.ts`。
- `@codemirror/language-data` → `./language-data/src/language-data.ts`（[tsconfig.json:L19](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L19)）——**易错点**：`language-data` 以 `langu` 开头而非 `lang-`，正则 `^(theme-|lang-)` 不匹配，前缀不被剥除，规则 3 找的就是 `language-data.ts`。

#### 4.2.4 代码实践

1. **实践目标**：不依赖克隆，独立验证三条探测规则与过滤正则的行为。
2. **操作步骤**：在任意目录新建一个临时脚本（**示例代码**，非项目原有文件）：

   ```js
   // detect-main.js —— 复刻 packages.js L52-L54 的探测逻辑做沙盒实验
   function detect(files, name) {
     return files.length == 1 ? files[0] : files.includes("index.ts") ? "index.ts"
       : files.includes(name.replace(/^(theme-|lang-)/, "") + ".ts")
         ? name.replace(/^(theme-|lang-)/, "") + ".ts" : null
   }
   console.log(detect(["only.ts"], "anything"))            // 规则 1
   console.log(detect(["a.ts", "index.ts", "b.ts"], "x"))  // 规则 2
   console.log(detect(["a.ts", "java.ts"], "lang-java"))   // 规则 3（剥前缀）
   console.log(detect(["a.ts", "language-data.ts"], "language-data")) // 规则 3（不剥前缀）
   console.log(detect(["a.ts", "b.ts"], "lang-java"))      // 无命中 → null
   console.log(["a.ts", "b.d.ts", ".hidden.ts"].filter(f => /^[^.]+\.ts$/.test(f)))
   ```

   运行 `node detect-main.js`。
3. **需要观察的现象**：前四行分别输出 `only.ts`、`index.ts`、`java.ts`、`language-data.ts`；第五行输出 `null`；最后一行只保留 `a.ts`（`b.d.ts` 与 `.hidden.ts` 被正则排除）。
4. **预期结果**：与上面注释的标注一致。若第五行不是 `null`，说明你复刻的逻辑与原文有出入，回头对照 [bin/packages.js:L53-L54](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L53-L54)。
5. 该脚本纯内存运行、不触碰仓库文件，可直接执行；预期输出如上，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：假设某个包的 `src/` 下恰好只有一个 `index.ts`，命中的是规则 1 还是规则 2？

答案：规则 1。三元表达式先判断 `files.length == 1`。不过两种规则此时给出相同结果，所以从外部（比如 tsconfig 映射）无法区分——这正是本讲综合实践中「分类统计」需要打开目录数文件才能完成的原因。

**练习 2**：为什么 `Pkg` 不在目录缺失时抛错，却在「目录存在但找不到 main」时抛错？

答案：目录缺失是**正常的生命周期状态**——`cm install` 之前所有包目录都不存在，此时 `main = null` 让 `cm install` 自己也能运行（它要负责创建这些目录）。而「目录存在却无入口」意味着克隆下来的仓库结构和注册表的预期不符，属于真正的错误，越早暴露越好。

**练习 3**：`grep` 命令为什么单独为 `legacy-modes` 扫 `mode/` 目录下的 `.js`/`.d.ts` 文件？

答案：见 [bin/cm.js:L319-L326](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L319-L326)。`legacy-modes` 收纳的是 CodeMirror 5 时代的 JavaScript 模式（放在 `mode/` 而非 `src/`），没有 TypeScript 源码，所以 `Pkg` 跳过它（`main` 为 `null`），`grep` 只能走特殊分支。这印证了「`legacy-modes` 是注册表里唯一的异类」。

### 4.3 loadPackages()：三个导出集合

#### 4.3.1 概念说明

`loadPackages()` 是把「36 个名字」变成「三份可用视图」的工厂函数。三个集合的差异完全由**消费场景**决定：

| 导出 | 形态 | 包含范围 | 主要消费者 |
| --- | --- | --- | --- |
| `packages` | 数组 | 全部 36 个 `Pkg`（含 `main == null` 的） | `assertInstalled`、`install`、`status`、`commit`、`push`、`grep`、`run`、`unreleased` |
| `packageNames` | 对象（名 → `Pkg`） | 同上，按名字索引 | `release`（按用户输入的包名取 `Pkg`）、`buildReadme` |
| `buildPackages` | 数组 | 仅 `main != null` 的包 | `build`、`clean`、`devserver`、`test` |

一句话总结：**凡是要「动磁盘上每个仓库」的命令用 `packages`；要「按名查包」的用 `packageNames`；要「编译或测试源码」的用 `buildPackages`」。

#### 4.3.2 核心流程

```text
loadPackages()
    │
    ├─ packages    = all.map(n => new Pkg(n))       ← 36 次构造
    ├─ packageNames = Object.create(null)            ← 无原型的空对象
    │       for (p of packages) packageNames[p.name] = p
    └─ buildPackages = packages.filter(p => p.main)  ← 剔除 legacy-modes 与未克隆包
```

注意「函数式三连」的风格：`map` 做实例化、循环做索引、`filter` 做裁剪，中间没有任何可变的全局状态——`loadPackages()` 每次调用都返回全新的三份数据。

#### 4.3.3 源码精读

[bin/packages.js:L62-L67](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L62-L67)：

```js
exports.loadPackages = function loadPackages() {
  let packages = exports.all.map(n => new Pkg(n))
  let packageNames = Object.create(null)
  for (let p of packages) packageNames[p.name] = p
  return {packages, packageNames, buildPackages: packages.filter(p => p.main)}
}
```

两个容易被忽略的细节：

- `Object.create(null)`（L64）创建一个**没有原型**的对象。如果用普通的 `{}`，当某个包恰好叫 `toString` 或 `constructor` 这类 `Object.prototype` 上的属性名时，`packageNames[p.name]` 会取到继承的函数而非包对象。用空原型对象彻底规避了这类碰撞，也让 `Object.keys()` 的行为可预期。
- 函数表达式 `function loadPackages()` 带名字而非匿名——这样在栈追踪里能看到函数名，便于排错。

再看 `cm.js` 侧如何消费。顶层一次、install 尾部一次：

[bin/cm.js:L9-L11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L9-L11) 模块加载即执行：

```js
const {loadPackages, nonCore} = require("./packages")

let {packages, packageNames, buildPackages} = loadPackages()
```

[bin/cm.js:L98-L103](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L98-L103) `install()` 的收尾处**重新赋值**这三个变量：

```js
console.log("Running npm install")
run("npm", ["install", "--ignore-scripts"], root, {shell: process.platform == "win32", stdout: "inherit"})
console.log("Building modules")
;({packages, packageNames, buildPackages} = loadPackages())
build()
```

这是本模块最精妙的一处：`install` 启动时所有包目录尚不存在，顶层那次 `loadPackages()` 得到的 `buildPackages` 是空数组；克隆完成后必须**重新调用** `loadPackages()` 并用解构赋值覆盖外层的 `let` 变量，随后的 `build()` 才能拿到全部入口。main 探测之所以放在运行时而非写死，正是为了支持这种「装完再探测」的流程。

三个集合的消费示例（各选一个代表命令）：

- `buildPackages` → `build()` 把入口列表交给构建工具（[bin/cm.js:L118-L123](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L118-L123)）；`clean()` 逐包删 `dist`（[bin/cm.js:L290-L293](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L290-L293)）。
- `packageNames` → `release()` 用用户传入的包名做查表（[bin/cm.js:L234](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L234)）。
- `packages` → `assertInstalled()` 检查每个目录存在性（[bin/cm.js:L72-L79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)）；`grep()` 遍历所有仓库（[bin/cm.js:L319](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L319)）。

特别地，`devserver()` 里有一处 `.filter(f => f)`（[bin/cm.js:L158](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L158)）——`buildPackages.map(p => p.main)` 理论上已经不含 `null`，这层防御性过滤是历史残留的双保险，读代码时不必困惑。

#### 4.3.4 代码实践

本讲的主实践（也是大纲指定的任务）。

1. **实践目标**：亲手调用 `loadPackages()`，观察三个集合的实际内容，并与 `tsconfig.json` 的 `paths` 交叉验证。
2. **操作步骤**：
   - 前置：先完成 `node bin/cm.js install`（否则所有 `main` 都是 `null`，交叉验证无从谈起）。
   - 在仓库根目录新建 `list-pkgs.js`（**示例代码**，用完可删；注意别放进 `bin/` 以免污染工具链）：

     ```js
     // list-pkgs.js —— 打印每个包的 name/dir/main 三元组并对照 tsconfig
     const {loadPackages} = require("./bin/packages")
     const tsconfig = require("./tsconfig.json")

     let {packages, packageNames, buildPackages} = loadPackages()
     console.log(`total=${packages.length} buildable=${buildPackages.length}`)
     for (let p of packages) {
       // tsconfig 的键是 npm 包名：非 codemirror 的都加 @codemirror/ 前缀
       let key = p.name == "codemirror" ? "codemirror" : "@codemirror/" + p.name
       let mapped = (tsconfig.compilerOptions.paths[key] || ["（无映射）"])[0]
       let mainRel = p.main ? p.main.replace(process.cwd() + "/", "") : "（null）"
       let mark = p.main && "./" + mainRel == mapped ? "OK " : "DIFF"
       console.log(`${mark}  ${p.name.padEnd(18)} ${mainRel}  |  tsconfig: ${mapped}`)
     }
     ```

   - 运行 `node list-pkgs.js`。
3. **需要观察的现象**：
   - 首行输出 `total=36 buildable=35`（36 减去 `legacy-modes`）。
   - 大多数行标记 `OK`——探测出的 main 与 `paths` 映射指向同一文件。
   - `legacy-modes` 一行 main 为 `（null）`，且 tsconfig 中查不到 `@codemirror/legacy-modes` 的映射，标记 `DIFF`。
4. **预期结果**：除了 `legacy-modes`，还可能出现一个值得深究的差异——`lang-liquid` 的 `paths` 映射指向 `./lang-vue/src/liquid.ts`（[tsconfig.json:L43](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L43)），即**另一个包仓库的内部**，而 `Pkg` 探测会指向 `lang-liquid` 自己目录下的 `src/liquid.ts`。这说明 `packages.js` 的动态探测与 `tsconfig.json` 的手工静态映射是**两套独立维护的机制**，可能出现分歧。至于你的工作区里哪一份真正生效、`lang-liquid.git` 仓库自身现状如何，**待本地验证**（可在克隆出的目录里用 `ls lang-liquid/src` 与 `ls lang-vue/src` 对照确认）。
5. 运行结果依赖 install 完成度，以上现象标注为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`cm install` 运行到一半时（比如克隆完 10 个包）按下 Ctrl+C，下次直接运行 `node bin/cm.js build` 会发生什么？

答案：`start()` 中除 `install`/`--help` 外的命令都要先过 `assertInstalled()`（[bin/cm.js:L13-L15](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L13-L15)），它会遍历 `packages` 检查每个 `p.dir` 是否存在，发现缺失就打印 `module <name> is missing. Did you forget to run 'cm install'?` 并以退出码 1 终止（[bin/cm.js:L72-L79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)）。你只能重新跑完 `cm install`。

**练习 2**：`loadPackages()` 为什么设计成函数，而不是在模块加载时算好直接导出三个常量？

答案：导出时机只有一次，而包目录的存在状态会变。`install()` 需要在克隆完成后重新执行探测并覆盖结果（[bin/cm.js:L101](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L101)）。做成函数让「探测」成为可重复调用的动作，配合外层 `let` 变量的重新解构赋值实现数据刷新。

**练习 3**：`buildPackages` 里为什么 `legacy-modes` 一定会缺席，而其他未克隆的包也缺席？

答案：`filter(p => p.main)` 依据的是 `main != null`。`legacy-modes` 被构造函数的第一个条件显式跳过（[bin/packages.js:L51](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L51)），未克隆的包被第二个条件（目录不存在）跳过，两者 `main` 都保持初始的 `null`。

## 5. 综合实践

**任务：给注册表做一次「体检报告」。**

综合运用本讲三个模块的知识，产出一份能回答以下三个问题的报告：

1. 36 个包各自命中了 main 探测的哪条规则？
2. `Pkg.main` 的探测结果与 `tsconfig.json` 的 `paths` 映射是否一致？
3. 三个集合（`packages` / `packageNames` / `buildPackages`）的成员差集是什么？

操作步骤：

1. 完成 `node bin/cm.js install`。
2. 在 4.3.4 的 `list-pkgs.js` 基础上扩展（**示例代码**）：对每个 `main != null` 的包，用 `fs.readdirSync(join(p.dir, "src")).filter(f => /^[^.]+\.ts$/.test(f))` 取回文件列表，按 `files.length == 1` / `包含 index.ts` / `包含剥前缀同名文件` 三类打上规则标签，统计每类的数量。
3. 输出三行汇总：规则 1 / 规则 2 / 规则 3 各命中多少个包，以及 `packages − buildPackages` 的差集（预期至少含 `legacy-modes`）。
4. 对照 4.3.4 的 `OK/DIFF` 标记，把所有 `DIFF` 的包单独列出，并给每个 DIFF 写一句原因分析（提示：`legacy-modes` 是「无映射」；`lang-liquid` 是「映射指向 lang-vue 内部」）。
5. 最后用 `node bin/cm.js packages | head -3` 验证报告里的顺序与 `exports.core` 的前三项一致。

预期结果：一份 36 行的明细表加三行统计；`buildable` 应为 35。若出现计划外的 `DIFF` 行，说明你工作区的包版本与本仓库注册表预期有出入——这本身就是有价值的发现。完整输出**待本地验证**。

## 6. 本讲小结

- `bin/packages.js` 是全仓库唯一的包清单数据源：`core`（12 个核心包）与 `nonCore`（24 个语言/主题/遗留包）拼接成 36 项的 `all`，`nonCore` 还是 `cm build-readme` 的准入判断（[bin/cm.js:L347](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L347)）。
- `Pkg` 构造函数以「名字 → 目录 → 入口」的顺序实例化：`dir` 恒有值，`main` 可能因未克隆或是 `legacy-modes` 而为 `null`。
- main 探测三条规则的优先级是「唯一 .ts 文件 > `index.ts` > 剥掉 `theme-`/`lang-` 前缀后的同名文件」，都落空则抛错；过滤正则 `/^[^.]+\.ts$/` 同时排除 `.d.ts` 与点开头文件。
- `loadPackages()` 返回三个视图：`packages`（全量，供 git 类命令遍历）、`packageNames`（空原型索引对象，供按名查表）、`buildPackages`（仅可构建包，供编译/测试命令）。
- `cm.js` 顶层与 `install()` 尾部分别调用一次 `loadPackages()`，后者用重新探测的结果覆盖变量后再触发首次构建——这是 main 必须运行时探测的根本原因。
- `packages.js` 的动态探测与 `tsconfig.json` 的静态 `paths` 是两套独立维护的机制，`lang-liquid` 的映射指向 `lang-vue` 内部就是两者分歧的实例。

## 7. 下一步学习建议

下一讲（u2-l2《构建流水线：cm build 背后的工具链》）将顺着本讲的 `buildPackages.map(p => p.main)` 继续往下走：这份入口列表如何被交给 `@marijn/buildtool` 与 `@codemirror/buildhelper`，`tsconfig.json` 的 `paths` 如何让各包源码越过 npm 直接互相引用，以及 `cm clean` 与构建产物的关系。

在此之前，建议你先自行做两个热身阅读：

1. [bin/cm.js:L118-L123](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L118-L123) 的 `build()`——只有 6 行，是下一讲的起点。
2. `tsconfig.json` 的 `include` 字段（[tsconfig.json:L51](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L51)）——思考一下 `*/src/*.ts` 这个 glob 与本讲 `Pkg` 只扫 `src/` 下一层的探测方式为什么是配套的。
