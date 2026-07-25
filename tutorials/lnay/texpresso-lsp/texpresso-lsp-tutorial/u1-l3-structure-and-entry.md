# 目录结构与入口文件链路

## 1. 本讲目标

[u1-l1](u1-l1-project-overview.md) 帮你建立了「编辑器 ⇄ texpresso-lsp ⇄ texpresso 子进程」的三层架构心智模型，[u1-l2](u1-l2-build-and-run.md) 教你把项目真正跑起来、接进编辑器。但前两讲都是**操作视角**——「敲什么命令、配什么选项」。本讲换成**读地图的视角**：把整个仓库摊开，逐个文件看清它是什么、放在哪里、和别的文件怎么连起来。

换句话说，u1-l2 回答的是「**怎么跑**」，本讲回答的是「**这个项目由哪些文件组成、程序到底从哪一个文件开始执行、又是怎么一路找到它的**」。

学完本讲你应该能够：

1. 默写出 texpresso-lsp 的目录布局，并说出每个文件/目录的职责。
2. 把 `package.json` 当成一份「项目元数据文档」逐字段读懂，尤其是 `name` / `version` / `main` / `bin` / `dependencies` / `devDependencies` 各自的含义，并能解释 `dependencies` 与源码里 `import` 语句的对应关系。
3. 逐项解释 `tsconfig.json` 里 `rootDir` / `outDir` / `target` / `module` / `strict` 等选项的作用，画出「`src/server.ts` → `dist/server.js` → `bin/texpresso-lsp.sh` → npm `bin` 字段 → 全局命令」的完整入口调用链。
4. 结合真实代码说明 `strict: true` 对编写代码的具体约束。

本讲只做「结构梳理 + 入口链路」，不展开任何运行期行为（握手、收发命令、子进程管理留给后续单元）。

## 2. 前置知识

- **Node.js 的 CommonJS 模块系统**：Node 默认用 `require()` / `module.exports` 组织代码。一个 `.js` 文件被 `require` 时，Node 会执行它并拿到其 `module.exports`。本项目的 bin 脚本正是用 `require('../dist/server.js')` 来启动整个程序的。
- **npm 包的 `name` 与全局命令**：`package.json` 的 `name` 字段既是 npm 仓库里的包名，也是 `npm install -g` 之后在命令行里敲的命令名。
- **入口（entry point）**：一个程序总得有「第一行被执行的代码」。Node 程序的入口由「谁启动了它」决定——直接 `node xxx.js` 时 `xxx.js` 是入口；通过 npm 全局命令启动时，入口由 `package.json` 的 `bin` 字段间接指定。
- **TypeScript 的 `import` 与编译产物**：`.ts` 源码用 ES 的 `import` 语法；经过 `tsc` 编译后，会按 `tsconfig.json` 的 `module` 选项转换成 Node 能跑的 `require()` 形式，并落到 `outDir` 指定的目录。
- **`.gitignore` / `.editorconfig` 等辅助文件**：它们不影响程序运行逻辑，但决定了「哪些文件进版本库」「代码用什么缩进」等工程规范。本讲会把它们也纳入「地图」。

> 本讲建立在 u1-l2 已经讲过的「四条 npm 脚本」与「`bin`/`main` 字段基本含义」之上。如果那些内容你已经清楚，本讲会在它们的基础上补充更完整的字段解读和编译映射细节；若有陌生处，可随时回看 [u1-l2](u1-l2-build-and-run.md) 的 4.1 与 4.2 节。

## 3. 本讲源码地图

| 文件 | 类别 | 作用 |
| --- | --- | --- |
| [package.json](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json) | 元数据 | 项目的「身份证」：名字、版本、入口、脚本、依赖全在这里。 |
| [tsconfig.json](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json) | 配置 | TypeScript 编译器配置，决定「源码 → 产物」的映射与严格程度。 |
| [bin/texpresso-lsp.sh](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh) | 入口脚本 | 全局命令 `texpresso-lsp` 真正调用的文件，只有两行。 |
| [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) | 源码·入口 | **唯一源码入口**：LSP 连接、握手、文档同步、事件分发都在这里。 |
| [src/process-manager.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts) | 源码·模块 | 子进程管理器，被 `server.ts` 导入。 |
| [src/types.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts) | 源码·模块 | 类型定义，被 `server.ts` 导入。 |
| [.editorconfig](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.editorconfig) / [.gitignore](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.gitignore) | 工程辅助 | 编辑器风格约定 / 版本库忽略规则。 |
| [.github/workflows/npm-publish.yml](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.github/workflows/npm-publish.yml) | CI | 发布到 npm 的工作流，里面也藏着一条「构建链」。 |

---

## 4. 核心概念与源码讲解

### 4.1 目录布局

#### 4.1.1 概念说明

读一个陌生项目，第一步永远是「看目录」。texpresso-lsp 是一个**小型项目**——根目录下加上配置文件也就十来个文件，源码只有 `src/` 下三个 `.ts`。正因为小，我们完全可以做到「逐文件说清楚」，建立一张后续所有讲义都能回查的地图。

目录里的文件可以分成五类：

1. **源码**（`src/`）：真正被编译、运行的 TypeScript。
2. **入口脚本**（`bin/`）：把「全局命令」与「编译产物」粘起来的薄胶水。
3. **项目元数据与编译配置**（`package.json` / `tsconfig.json` / `package-lock.json`）：告诉 npm 和 tsc「这是什么、怎么构建」。
4. **工程辅助**（`.editorconfig` / `.gitignore`）：约束编辑器行为与版本库内容。
5. **CI**（`.github/workflows/`）：自动化发布流程。

#### 4.1.2 核心流程

下面这张树是用 `git ls-files` 列出的「版本库里真实存在的文件」（`node_modules/` 和 `dist/` 是构建产物，被 `.gitignore` 忽略，不会进版本库）：

```text
texpresso-lsp/
├── .editorconfig                # 编辑器风格：*.ts 用 4 空格缩进
├── .github/
│   └── workflows/
│       └── npm-publish.yml      # 发布到 npm 的 CI
├── .gitignore                   # 忽略 node_modules/、dist/ 等
├── README.md                    # 用户文档
├── bin/
│   └── texpresso-lsp.sh         # 全局命令入口（两行）
├── package-lock.json            # 依赖版本锁定
├── package.json                 # 项目元数据（入口/脚本/依赖）
├── tsconfig.json                # tsc 编译配置
└── src/                         # ← 唯一的源码目录
    ├── process-manager.ts       # 子进程管理器
    ├── server.ts                # ★ 唯一源码入口
    └── types.ts                 # 类型定义
```

构建之后，仓库里还会多出两个**不在版本库中**的目录：

```text
node_modules/   # npm install 生成，存放第三方依赖
dist/           # tsc 生成，存放 .js 编译产物（被 .gitignore 忽略）
```

一个关键观察：`src/` 下虽然有三个 `.ts`，但程序运行时**只有 `server.ts` 是入口**，另外两个是被它 `import` 进来的「零件」。这就是下一节要细讲的「唯一源码入口」。

#### 4.1.3 源码精读

**`.gitignore` 决定了哪些东西不进版本库**：

[.gitignore:7-9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.gitignore#L7-L9) —— 把 `dist/` 和 `build/` 列为忽略项。这意味着编译产物**不会提交到 git**，任何人克隆仓库后都必须自己 `npm run build` 才能得到 `dist/server.js`。这一点直接关系到 4.3 节的入口链路：`bin/texpresso-lsp.sh` 依赖的 `dist/server.js` 在「开箱即用」的意义上是不存在的，必须先构建。

**`.editorconfig` 约定了源码缩进风格**：

[.editorconfig:9-11](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.editorconfig#L9-L11) —— 对所有 `*.ts` 文件规定 `indent_style = space`、`indent_size = 4`。你打开 [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) 看到的 4 空格缩进就是它统一出来的，多人协作时不会出现「Tab / 空格混用」的脏 diff。

**`src/` 的三个文件谁是「入口」**？看 `server.ts` 顶部的 import 就能判断：

[src/server.ts:15-16](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L15-L16) —— `server.ts` 导入了 `./types` 和 `./process-manager`。反过来，`types.ts` 和 `process-manager.ts` 都**没有**导入 `server.ts`。所以在源码的依赖关系图里，`server.ts` 是「根」：没有别的源码文件依赖它，它依赖别的文件。这就是「`server.ts` 是唯一源码入口」的依据——它既是模块依赖图的根，也是（编译后）被 bin 脚本 `require` 的那个文件。

**CI 里也藏着一条「构建链」**：

[.github/workflows/npm-publish.yml:30-32](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.github/workflows/npm-publish.yml#L30-L32) —— 发布流程依次跑 `npm ci`（按 lock 文件装依赖）→ `npm run build`（生成 `dist/`）→ `npm publish`。注意：正因为 `.gitignore` 不跟踪 `dist/`，CI 必须在发布前当场 `npm run build` 把产物造出来，否则发布的包里就没有可执行代码。这条 CI 链路与本地开发链路是同一套（详见 [u1-l2](u1-l2-build-and-run.md) 4.1 节）。

#### 4.1.4 代码实践

**实践目标**：用只读 git 命令亲自核对本节那张目录树，区分「版本库内文件」与「构建产物」。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files`，对照上面那棵树，逐项确认每个文件都在列表里。
2. 执行 `ls dist/ 2>/dev/null`（若尚未构建则目录不存在）。若已按 [u1-l2](u1-l2-build-and-run.md) 构建过，应看到 `server.js`、`process-manager.js`、`types.js`。
3. 执行 `git status dist/`（假设 `dist/` 已存在），观察 Git 是否把 `dist/` 当作未跟踪文件——它应当**被忽略**（不出现），这正是 [.gitignore:7-9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.gitignore#L7-L9) 的效果。

**需要观察的现象**：

- 第 1 步列出的文件与 4.1.2 节的树**完全一致**，没有任何多余的源码文件。
- 第 3 步 `dist/` 即便存在于磁盘上，也不会出现在 `git status` 的未跟踪列表里。

**预期结果**：你能清楚区分「提交进 git 的源码/配置」与「本地生成的 `node_modules/`、`dist/`」两类文件，并理解为什么克隆仓库后必须先构建。

> 若本地未安装 Node 环境无法构建，第 2 步可标注「待本地验证」，但第 1、3 步只需 git 即可完成。

#### 4.1.5 小练习与答案

**练习 1**：`package-lock.json` 是干什么的？它和 `node_modules/` 谁进版本库、谁不进？

**参考答案**：`package-lock.json` 锁定了每个依赖（及其传递依赖）的**确切版本**，保证不同机器上 `npm ci` 装出完全相同的依赖树。它**进版本库**（`git ls-files` 能看到它）；而 `node_modules/` 体积大、可由 lock 文件重建，所以被 [.gitignore:2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.gitignore#L2) 忽略，不进版本库。

**练习 2**：为什么说 `server.ts` 是「唯一源码入口」？请用 import 方向来解释。

**参考答案**：看依赖方向——[src/server.ts:15-16](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L15-L16) 导入了 `./types` 和 `./process-manager`，而这两个文件都没有反向导入 `server.ts`。所以在源码依赖图里 `server.ts` 是不被任何本地文件依赖的「根节点」。再加上运行期它（编译产物）正是被 [bin/texpresso-lsp.sh](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh) `require` 的对象，两相印证，它就是唯一入口。

---

### 4.2 package.json 关键字段

#### 4.2.1 概念说明

`package.json` 不只是「依赖清单」，它是整个项目的**元数据身份证**。本节把它逐字段读一遍。其中 `scripts` / `bin` / `main` 的操作含义 u1-l2 已讲透，这里只做指针；本节的重点是补上 u1-l2 没展开的部分——尤其是 **`dependencies` 与源码 `import` 的对应关系**，以及 `name` / `version` 的语义。

一个核心直觉：**`dependencies` 列表，本质上就是源码里那些「非本地、非 Node 内置」的 `import` 来源清单。** 反过来，你看到源码 `import` 了什么外部包，就一定能在 `dependencies` 里找到它。这条对应关系是判断「依赖是否多余 / 是否缺失」的最快方法。

#### 4.2.2 核心流程

把 `package.json` 当文档来读的顺序与对应关系：

```text
name        ──> npm 包名 ＝ 全局命令名（texpresso-lsp）
version     ──> 当前版本（1.3.0，对应 git 标签 v1.3.0）
main        ──> 作为库被 require() 时的入口：dist/server.js
bin         ──> 作为命令行工具被运行时的入口脚本：bin/texpresso-lsp.sh
scripts     ──> build/start/dev/watch（详见 u1-l2 4.1）
dependencies  ──┐
                ├─ 对应 ──> 源码里 import 的外部包
devDependencies ─┘          （见 4.2.3 的三对三映射）
```

#### 4.2.3 源码精读

先看文件全貌：

[package.json:1-23](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L1-L23) —— 整个 `package.json` 只有 23 行，字段非常克制。

**`name` 与 `version` 的语义**：

[package.json:2-3](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L2-L3) —— `name: "texpresso-lsp"`、`version: "1.3.0"`。`name` 同时是 npm 仓库里的包名（`npm install -g texpresso-lsp`）和全局命令名（装完敲 `texpresso-lsp`）；`version` 是 `1.3.0`，恰好对应当前 HEAD 所在的提交 `c13ec89 "v1.3.0"`（见本仓库最近一次 commit）。这条隐含约定让你能从 `package.json` 一眼看出手里是哪个发布版本。

**`main` 与 `bin` 的分工**（u1-l2 已详述，这里只做结构定位）：

[package.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L5-L6) —— `main: "dist/server.js"` 是「被 `require` 时的入口」；`bin: "bin/texpresso-lsp.sh"` 是「被当作命令运行时的入口」。本项目主要作为命令行工具被使用，所以实践中走 `bin` 这条路；`main` 更多是 npm 的惯例占位。详细操作含义见 [u1-l2](u1-l2-build-and-run.md) 4.2 节。

**`scripts` 字段**（u1-l2 已详述）：[package.json:7-12](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L7-L12) 定义了 `build` / `start` / `dev` / `watch` 四条命令，逐条解读见 [u1-l2](u1-l2-build-and-run.md) 4.1.2 的对照表。本节不再重复。

**`dependencies` ↔ 源码 import 的「三对三」映射**（本节重点）：

[package.json:13-17](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L13-L17) —— 三个运行时依赖。它们恰好一一对应源码里 `import` 的三个外部来源：

| `dependencies` 里的依赖 | 源码里 `import` 的位置 |
| --- | --- |
| `vscode-languageserver` | [src/server.ts:1-13](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L1-L13)（`vscode-languageserver/node`）、[src/types.ts:1](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L1)（`vscode-languageserver/node`） |
| `vscode-languageserver-textdocument` | [src/server.ts:14](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L14)（`vscode-languageserver-textdocument`） |
| `vscode-uri` | [src/server.ts:18](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L18)（`vscode-uri`） |

> 注意：[src/server.ts:17](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L17) 和 [src/process-manager.ts:1](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L1) 还 `import` 了 `child_process`，[src/process-manager.ts:2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L2) `import` 了 `events`——这两个是 **Node.js 内置模块**，无需在 `dependencies` 里声明。所以「外部依赖三条 + 内置模块两条」就是全部 import 来源，没有一条 import 找不到出处。

这条对应关系还能反过来用：如果哪天 `dependencies` 里出现了一个源码里从未 `import` 的包，那它要么是多余的，要么是被间接需要的——这是一个很实用的代码审阅技巧。

**`devDependencies` ↔ 构建工具链**：

[package.json:18-22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L18-L22) —— 三个开发依赖，与 `scripts` 一一对应：

| `devDependencies` | 用在哪条脚本 |
| --- | --- |
| `typescript`（即 `tsc`） | `npm run build` / `npm run watch` |
| `ts-node` | `npm run dev`（免编译直接跑 `.ts`） |
| `@types/node` | 给 `tsc` 提供 Node 内置 API 的类型声明（让 `child_process` 等有类型） |

运行期（用户实际使用 LSP 时）**不需要**这三个包，所以它们放在 `devDependencies` 而不是 `dependencies`——这也是 `npm install --production` 时不会装它们的原因。

#### 4.2.4 代码实践

**实践目标**：验证「`dependencies` ↔ 源码 import」的三对三映射，并体验一次「依赖审计」。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "^import" src/`，列出所有 import 语句。
2. 把结果里的包名分成两类：「外部包」（`vscode-*`）和「Node 内置」（`child_process` / `events`）。
3. 对照 [package.json:13-17](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L13-L17)，确认每个外部包都在 `dependencies` 里、每个内置模块都不在。
4. 思考：如果你在 `server.ts` 里新写一行 `import { xxx } from "lodash"`，需要同时改 `package.json` 吗？

**需要观察的现象**：

- 第 1 步输出的外部包恰好是 `vscode-languageserver`、`vscode-languageserver-textdocument`、`vscode-uri` 三个，与 `dependencies` 一致。
- 内置模块 `child_process`、`events` 不在 `dependencies` 里。

**预期结果**：

- 确认「源码 import 的外部来源 = `dependencies`」这条等式成立，项目没有冗余依赖、也没有缺失依赖。
- 第 4 步结论：需要。新增外部依赖必须同时加入 `dependencies`（用 `npm install lodash` 会自动写入），否则别人装包后运行会报「找不到模块」。

> 本实践只需 `grep` 和阅读，无需运行 Node；结论是确定的。

#### 4.2.5 小练习与答案

**练习 1**：`vscode-languageserver` 在 `dependencies` 里，但源码里 `import` 的写法是 `from "vscode-languageserver/node"`。这冲突吗？

**参考答案**：不冲突。`vscode-languageserver/node` 是该包内部的**子路径**（subpath import），`/node` 指向包内专门面向 Node 环境的入口。npm 解析时仍把它当作 `vscode-languageserver` 这个包，所以依赖声明只需写包名即可。这也是 [src/server.ts:1-13](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L1-L13) 一大串 LSP 能力（`createConnection`、`TextDocuments` 等）都来自同一个包的原因。

**练习 2**：为什么 `@types/node` 放在 `devDependencies` 而不是 `dependencies`？

**参考答案**：`@types/node` 只在**编译期**（`tsc` 类型检查）和**开发期**（编辑器智能提示）被用到，运行期 Node 自身就提供了那些 API，不需要类型声明。把它放进 `dependencies` 会让终端用户多装一个对他们毫无用处的包；放进 `devDependencies` 则在 `npm install --production` / 全局安装时被跳过，是正确的分类。同理 `typescript` 和 `ts-node` 也是编译/开发工具，故同属 `devDependencies`（见 [package.json:18-22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L18-L22)）。

---

### 4.3 tsconfig 编译映射与入口链路

#### 4.3.1 概念说明

`tsconfig.json` 是 TypeScript 编译器 `tsc` 的配置。它回答两个问题：**「从哪里读源码、往哪里写产物」**和**「按什么规则编译、严格到什么程度」**。本节会逐项读完它，并把它的每条选项与「入口调用链」串起来。

入口链路要解决的问题是：当你（或编辑器）在命令行敲 `texpresso-lsp --stdio` 时，控制权是怎样**从命令名一路传递到 `server.ts` 里那行 `connection.listen()`** 的？这条链跨过了四个环节，每个环节都由 `package.json` 或 `tsconfig.json` 的某个字段支撑。本节最后会画出完整的链路图。

补充一个前置概念：**`module: commonjs`**。源码 `server.ts` 用的是 ES 的 `import` 语法，但 Node 运行的是 CommonJS（`require`）。`tsc` 在 `module: commonjs` 下会把 `import x from "y"` 编译成 `require("y")` 形式——这正是 [bin/texpresso-lsp.sh:2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh#L2) 里那句 `require('../dist/server.js')` 能成立的前提。换句话说，**bin 脚本的 `require` 之所以能加载 `dist/server.js`，是因为 `tsconfig.json` 把模块系统指定成了 commonjs**。

#### 4.3.2 核心流程

**编译映射**（源码 → 产物，由 `rootDir` / `outDir` 决定）：

```text
src/server.ts          ─┐
src/process-manager.ts  ├─  tsc (rootDir=./src → outDir=./dist)  ─►  dist/server.js
src/types.ts           ─┘                                        dist/process-manager.js
                                                                 dist/types.js
```

三个 `.ts` 一一对应三个 `.js`，目录结构保持一致。其中 `dist/server.js` 内部还会 `require("./process-manager")` 和 `require("./types")`——这正是 `server.ts` 顶部那两条 import 编译后的结果。

**运行期入口链路**（命令 → 源码，反向追溯）：

```text
        编辑器启动：texpresso-lsp --stdio
                   │
                   ▼  npm 全局安装时，以 package.json 的 name 为命令名、bin 指向的脚本为目标创建符号链接
        命令 texpresso-lsp   ◄── package.json:6  "bin": "bin/texpresso-lsp.sh"
                   │
                   ▼  shebang #!/usr/bin/env node 用 Node 执行该脚本
        bin/texpresso-lsp.sh   ◄── package.json:6
                   │
                   ▼  require('../dist/server.js')
        dist/server.js         ◄── tsconfig rootDir→outDir 把 src/server.ts 编译到此
                   │
                   ├── require("./process-manager")  ─► dist/process-manager.js
                   ├── require("./types")            ─► dist/types.js
                   │
                   ▼  文件末尾
        connection.listen()    ◄── src/server.ts:281  服务器开始监听 stdio
```

**strict 模式如何约束代码**：`strict: true` 会一次性打开一组严格检查（`noImplicitAny`、`strictNullChecks`、`useUnknownInCatchVariables` 等）。它的效果不是抽象的——本仓库里就有三处**看得见**的影响（见 4.3.3）。

#### 4.3.3 源码精读

先看 `tsconfig.json` 全貌：

[tsconfig.json:1-14](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L1-L14) —— 配置相当精简，下面逐项拆解。

**编译映射的两个核心字段**（u1-l2 已点到，这里补全链路含义）：

[tsconfig.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L5-L6) —— `outDir: "./dist"`、`rootDir: "./src"`。`rootDir` 告诉 tsc「源码根在这里」，`outDir` 告诉它「产物写到那里」。tsc 会**保持 `rootDir` 之下的相对目录结构**原样映射到 `outDir`，所以扁平的 `src/*.ts` 就变成了扁平的 `dist/*.js`。这条映射正是 4.3.2 图里「三个 `.ts` → 三个 `.js`」的来源，也决定了 [bin/texpresso-lsp.sh](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh) 里 `require('../dist/server.js')` 这个路径写得对。

**`target: ES2020` 与源码语法的对应**：

[tsconfig.json:3](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L3) —— `target` 决定产物 JS 的语法等级。设成 `ES2020` 意味着 tsc **可以原样保留** ES2020 才有的语法，例如空值合并 `??` 和可选链 `?.`。这正好对应源码里大量出现的写法：[src/server.ts:54-60](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L54-L60) 的 `??`、[src/process-manager.ts:27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L27) 的 `this.process.stdout?.on(...)`。如果 `target` 设得更低（如 ES5），tsc 就得把这些语法「降级」编译成更啰嗦的等价代码；设成 ES2020 则直接保留，产物更干净。

**`module: commonjs` 让 `require` 成立**：

[tsconfig.json:4](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L4) —— 把模块格式编译成 CommonJS。源码里的 `import { ServerConfig } from "./types"` 会被编译成 `require("./types")`。于是 `dist/server.js` 既是一个合法的 CommonJS 模块（能被 bin 脚本 `require`），它内部对 `./process-manager`、`./types` 的依赖也以 `require` 形式存在于同一个 `dist/` 目录下、能被正确解析。**没有这一项，整条入口链就断在「bin 脚本无法加载产物」这一步。**

**其余三个开关**：

[tsconfig.json:8-10](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L8-L10) —— `esModuleInterop: true` 让你能用更自然的 `import x from "pkg"` 语法去引入 CommonJS 的第三方包（如 `vscode-uri`），而不必写成 `import * as x`；`skipLibCheck: true` 跳过对 `node_modules` 里 `.d.ts` 类型声明的检查，加快编译、避免被第三方类型里的错误拖累；`forceConsistentCasingInFileNames: true` 强制 import 路径的文件名大小写必须与磁盘一致，防止「在 macOS/Windows（大小写不敏感）上能跑、到了 Linux CI（大小写敏感）上就崩」的隐蔽 bug——对一个有 CI（[.github/workflows/npm-publish.yml](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.github/workflows/npm-publish.yml)）的项目尤其重要。

**`include` / `exclude` 圈定编译范围**：

[tsconfig.json:12-13](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L12-L13) —— `include: ["src/**/*"]` 表示只编译 `src/` 下的所有文件；`exclude: ["node_modules", "dist"]` 表示绝不编译依赖目录和旧产物。这解释了为什么 `server.ts` 即便没被显式列为「入口」，也会被编译——它在 `src/**/*` 通配范围内，且它 import 的 `./types`、`./process-manager` 同样在范围内，于是三个文件一起被编译。

**`strict: true` 在代码里的三处具体影响**：

[tsconfig.json:7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L7) —— `strict: true` 打开一整套严格检查。它的效果在本仓库里有非常具体的体现：

1. **`strictNullChecks` 让 `??` 成为必需**：[src/process-manager.ts:30](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L30) 写的是 `this.stdout_buffer = lines.pop() ?? '';`。因为 `Array.pop()` 的返回类型是 `string | undefined`，而 `stdout_buffer` 声明为 `string`（[src/process-manager.ts:7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L7)），在严格模式下必须用 `?? ''` 把 `undefined` 兜住，否则 tsc 报错。关掉严格模式后这段代码「看起来」就不需要 `??` 了——但那是放弃了类型安全。

2. **`strictNullChecks` 逼出守卫语句**：[src/process-manager.ts:104](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L104) 的 `if (!this.isRunning || !this.process?.stdin || !this.process?.stdout)` 是一道显式空值守卫——因为 `this.process` 可能是 `null`，必须先判断再用，否则访问 `.stdin` 会被 tsc 拦下。

3. **`useUnknownInCatchVariables` 让 `catch` 必须收窄类型**：[src/server.ts:124-128](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L124-L128) 里 `catch (error)` 的 `error` 在严格模式下是 `unknown`（而不是 `any`），所以不能直接 `error.message`，必须先用 `error instanceof Error ? error.message : String(error)` 收窄。同样的写法还出现在 [src/server.ts:258-261](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L258-L261) 和 [src/server.ts:274-277](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L274-L277)。

这三处说明：**`strict: true` 不是「装饰品」，它实实在在地塑造了源码长什么样**——你会更频繁地看到 `??`、`?.`、显式类型注解和 `instanceof` 收窄。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍「源码 → 产物 → 入口脚本 → 命令」的链路，并用一个对照实验体会 `strict: true` 的约束力。这是本讲的核心实践，对应学习目标里「画出入口调用链 + 说明 strict 影响」。

**操作步骤**：

1. **构建**（若尚未构建）：`npm install` → `npm run build`。
2. **核对产物**：`ls dist/`，应看到 `server.js`、`process-manager.js`、`types.js` 三个文件，与 [tsconfig.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L5-L6) 的映射一致。
3. **追踪 require 链**：打开 `dist/server.js`，用 `grep "require(" dist/server.js` 查看编译后的 require 调用，确认它内部 `require("./process-manager")`、`require("./types")`、以及对外部包的 `require("vscode-languageserver/node")` 等——这些都源自 [src/server.ts:1-18](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L1-L18) 的 import 被 `module: commonjs` 编译的结果。
4. **验证链路终点**：用 `grep -n "connection.listen" dist/server.js`，确认 `connection.listen()` 被编译进了产物（对应 [src/server.ts:281](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L281)）。这就是「`require('../dist/server.js')` 之后服务器会自动开始监听」的原因——`listen()` 是模块顶层语句，加载即执行。
5. **画链路图**：把 4.3.2 节那张运行期入口链路图，结合你刚才 `grep` 到的真实 require，重新画一遍并标注每个箭头由哪个文件/字段支撑。
6. **（可选）strict 对照实验**：临时把 [tsconfig.json:7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L7) 改成 `"strict": false`，再 `npm run build`，对比是否还能编译通过、tsc 报错数量变化。**实验结束后务必改回 `true`**（本讲不得改动源码，这只是临时本地实验）。

**需要观察的现象**：

- 第 2 步：三个 `.js` 产物一一对应三个 `.ts` 源码。
- 第 3 步：`dist/server.js` 里能看到 `require("./process-manager")` 等字样，证明 ES `import` 已被编译成 CommonJS `require`。
- 第 4 步：`connection.listen` 确实在产物中，且位于模块顶层（不在任何函数内）。
- 第 6 步（若执行）：关闭 strict 后，原本因严格模式而必须写的 `??`、`?.` 守卫可能不再是「必需」，tsc 对空值的报错会大量消失——这反向印证了 strict 的约束力。

**预期结果**：你能完整复述并画出「`texpresso-lsp` 命令 → bin 脚本 → `dist/server.js` → 内部 require → `connection.listen()`」的链路，并理解链路上每个环节分别由 `package.json` 的 `bin` 字段、bin 脚本的 `require`、`tsconfig.json` 的 `rootDir/outDir/module` 共同支撑。

> 若本地无 Node 环境，第 1–4 步无法执行，请标注「待本地验证」；但第 5 步的链路图可基于本讲源码精读直接画出。第 6 步涉及临时改 `tsconfig.json`，属可选实验，请确保恢复。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `tsconfig.json` 的 `module` 从 `commonjs` 改成 `esnext`（保持 `target: ES2020`），入口链路会断在哪里？为什么？

**参考答案**：会断在 [bin/texpresso-lsp.sh:2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh#L2) 的 `require('../dist/server.js')`。因为 `module: esnext` 会让 tsc 保留 ES 的 `import` / `export` 语法不转换，而 Node（尤其是较旧版本、且未开 ESM 模式的 CommonJS 上下文里）的 `require` 加载一个含 `import` 语句的文件会报错（`Cannot use import statement outside a module` 之类）。这正是本项目选 `commonjs` 的原因：它让产物能与 bin 脚本的 `require`、Node 的默认 CommonJS 行为严丝合缝。（结论可在本地用一次临时实验验证。）

**练习 2**：`rootDir: "./src"` 和 `outDir: "./dist"` 如果删掉 `rootDir` 只留 `outDir`，产物会变吗？

**参考答案**：产物内容不变，但**产物在 `dist/` 内的目录层级可能变**。显式指定 `rootDir: "./src"` 是告诉 tsc「把 `src/` 当作根，`src/` 之下的相对路径原样映射到 `outDir`」，于是 `src/server.ts` → `dist/server.js`。若去掉 `rootDir`，tsc 会自动推断根目录为所有被编译文件的公共父目录——本项目的源码恰好都在 `src/` 下，推断结果通常也是 `src/`，所以大多数情况下产物路径相同；但显式写 `rootDir` 更稳健、可读性更好，避免将来加入 `src/` 之外的源码时产物目录结构意外改变。

**练习 3**：`forceConsistentCasingInFileNames: true` 防的是什么 bug？本项目里能找到对应的潜在风险点吗？

**参考答案**：它防止「文件名大小写不一致」导致的跨平台 bug——例如某人把 `./types` 误写成 `./Types`，在大小写不敏感的 macOS/Windows 上能跑，但到了大小写敏感的 Linux（比如本项目 CI 所用的 ubuntu-latest，见 [.github/workflows/npm-publish.yml:24](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.github/workflows/npm-publish.yml#L24)）上就会找不到文件。本项目里 [src/server.ts:15-16](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L15-L16) 的 `./types`、`./process-manager` 都是全小写、与磁盘文件名一致，所以目前没有问题——这个开关是一道预防性保险。

---

## 5. 综合实践

**任务**：产出一份《texpresso-lsp 入口链路说明书》，把本讲三个最小模块（目录布局、`package.json` 字段、`tsconfig` 映射与入口链路）串成一份可交付的「地图 + 调用链」文档。

请按顺序完成：

1. **绘制目录树**：用 `git ls-files` 列出真实文件，画出 4.1.2 节那棵树，并用三种颜色/标记区分「源码」「入口脚本」「配置/工程辅助」三类文件。
2. **填字段对照表**：为 `package.json` 的每个字段（`name` / `version` / `description` / `main` / `bin` / `scripts` / `dependencies` / `devDependencies`）写一句话作用说明；其中 `dependencies` 必须列出它与源码 import 的对应关系（参考 4.2.3 的三对三表）。
3. **画入口链路图**：按 4.3.2 节的运行期链路，画出从「`texpresso-lsp --stdio`」到「`connection.listen()`」的完整箭头图，并在**每个箭头上标注它由哪个文件的哪一行/哪个字段支撑**（例如 `bin 字段 → package.json:6`、`require → bin/texpresso-lsp.sh:2`、`编译映射 → tsconfig.json:5-6`）。
4. **用 grep 验证**：执行 `grep "require(" dist/server.js`（构建后），把产物里真实出现的 `require` 列出来，逐条对应回 [src/server.ts:1-18](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L1-L18) 的某条 import，验证「import → require」的编译转换。
5. **写 strict 影响小节**：列出本仓库里 3 处因 `strict: true` 才必须那样写的代码（参考 4.3.3 的三处），说明关掉 strict 会发生什么。
6. **反思**：在说明书结尾用一段话回答——「为什么克隆仓库后必须先 `npm run build` 才能用全局命令？」提示：把 [.gitignore:7-9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.gitignore#L7-L9)（不跟踪 `dist/`）、[bin/texpresso-lsp.sh:2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh#L2)（依赖 `dist/server.js`）、[tsconfig.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L5-L6)（构建才生成 `dist/`）三者串起来。

> 涉及构建与 grep 的步骤若本地无法执行，请如实标注「待本地验证」，但第 1、2、3、6 步可完全基于本讲源码精读与只读 git 命令完成。

## 6. 本讲小结

- texpresso-lsp 是小型项目：版本库内只有 `src/`（3 个 `.ts`）、`bin/`（1 个入口脚本）、若干配置与 CI 文件；`node_modules/`、`dist/` 是构建产物，被 [.gitignore](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.gitignore) 忽略。
- `src/server.ts` 是**唯一源码入口**：源码依赖图里它是根（[src/server.ts:15-16](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L15-L16) 导入另外两个文件，无人导入它），运行期它的编译产物正是被 bin 脚本 `require` 的对象。
- `package.json` 是项目元数据：`name` 既是包名也是全局命令名，`main`/`bin` 分别是「被 require」与「被运行」两个入口；`dependencies` 与源码 `import` 一一对应（外部三个包 ↔ 三条外部 import），`devDependencies` 对应构建工具链。
- `tsconfig.json` 的 `rootDir: ./src` / `outDir: ./dist` 决定了 `src/*.ts → dist/*.js` 的产物映射；`module: commonjs` 把 ES `import` 编译成 `require`，这是 bin 脚本能加载产物的根本前提。
- 完整入口链路：`texpresso-lsp --stdio` →（`bin` 字段）→ `bin/texpresso-lsp.sh` →（`require`）→ `dist/server.js` →（内部 `require`）→ 另两个产物 → 顶层 `connection.listen()` 开始服务。
- `strict: true` 实实在在塑造了代码风格：本仓库里 `??`、`?.` 守卫、`catch` 中的 `instanceof` 收窄都是它的直接产物（[process-manager.ts:30](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L30)、[process-manager.ts:104](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts#L104)、[server.ts:124-128](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L124-L128)）。

## 7. 下一步学习建议

至此你已经把项目的「骨架」摸清：知道每个文件是干什么的、程序从哪里开始执行、编译怎么把 `.ts` 变成可被 `require` 的 `.js`。但本讲全程停留在**静态结构**——我们只看了 `server.ts` 顶部的 import 和底部的 `connection.listen()`，中间那一大段（握手、配置合并、文档同步、事件监听）一个字都没展开。

建议下一步：

- 阅读 [u1-l4 LSP 基础与连接建立](u1-l4-lsp-connection-basics.md)：紧接本讲末尾的 `connection.listen()` 往上看，弄清 `createConnection` / `TextDocuments` / `onInitialize` 如何让本讲的「入口」真正变成一个「会说话的 LSP 服务器」。
- 如果你更想先深入「零件」而非「协议」，可以提前读 [u2-l1 配置体系与类型定义](u2-l1-config-and-types.md)：把本讲反复提到的 `./types`（[src/types.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts)）正式看一遍，理解 `ServerConfig` / `WorkspaceSettings` 的结构。
- 想了解「另一个被导入的零件」怎么管理子进程，可读 [u2-l2 进程管理器 TexpressoProcessManager](u2-l2-process-manager.md)，它会拆解 [src/process-manager.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/process-manager.ts) 的 `spawn` 与生命周期。
