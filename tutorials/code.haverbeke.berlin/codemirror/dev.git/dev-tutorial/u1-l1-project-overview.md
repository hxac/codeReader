# 这个仓库是什么：CodeMirror 中央开发仓库总览

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话说清本仓库与 npm 上三十多个 `@codemirror/*` 包之间的关系：**这里没有编辑器源码，只有开发脚本、bug 跟踪和演示页面**。
- 列出仓库根目录下每个文件与目录的职责，知道哪些条目是「克隆后才出现」的。
- 说出 README 中 `install` / `build` / `dev` 三条命令分别做什么。
- 读懂 `package.json` 中 `scripts` 与 `workspaces` 两块配置，并把 `test`、`test-node`、`prepare`、`dev` 四个脚本对应到 `cm` 子命令。
- 知道从哪里获取使用文档（codemirror.net）、在哪里提问（论坛）、如何报告 bug 和提交 PR。

## 2. 前置知识

本讲是整套手册的第一篇，不要求你了解 CodeMirror 的内部实现，但需要以下基础：

- **CodeMirror 是什么**：一个运行在浏览器里的代码编辑器组件（你在网页上见过的那些带语法高亮、自动补全的编辑框，很多就是它做的）。CodeMirror 6 被拆成了三十多个独立发布到 npm 的小包，例如 `@codemirror/state`（文档状态）、`@codemirror/view`（视图层）、`@codemirror/lang-javascript`（JavaScript 语言支持）。
- **npm scripts**：`package.json` 里的 `scripts` 字段定义了一些快捷命令，用 `npm run <名字>` 执行。
- **npm workspaces**：让一个仓库根目录「收编」若干子目录作为工作区，依赖统一提升（hoist）到根目录的 `node_modules`，子包之间可以直接互相引用。
- **Node.js 命令行程序**：会用终端执行 `node xxx.js` 即可。
- **git 基础**：clone、commit、pull request 的概念。

一个容易混淆的点先说清楚：**如果你只是想在项目里使用 CodeMirror，直接 `npm install` 各个 `@codemirror/*` 包即可，完全不需要本仓库**。README 第 7 行明确写了这一点。本仓库服务的对象是「想参与 CodeMirror 本身开发」的人。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为仓库根目录下的真实路径）：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md) | 仓库门面：定位说明 + install/build/dev 三条核心命令 |
| [CONTRIBUTING.md](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md) | 贡献指南：求助渠道、bug 报告规范、PR 流程、代码风格 |
| [package.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json) | 4 个 npm scripts + workspaces 配置，是所有命令的「入口别名」 |
| `bin/cm.js` | 命令行工具本体，所有子命令在这里分发（下一讲精读，本讲只看它的帮助文本） |
| `bin/packages.js` | 包注册表：core 与 nonCore 两组包名清单 |
| `bin/build-readme.js` | 为各包生成带 API 文档的 README（第 3 单元讲） |
| `tsconfig.json` | TypeScript 配置，把 `@codemirror/*` 映射到本地克隆的源码 |
| `demo/demo.ts`、`demo/index.html` | 演示页面：用 `EditorView` 搭一个真实编辑器 |
| `demo/test/` | 两个指向根 `node_modules/mocha` 的符号链接，供浏览器测试页使用 |
| `LICENSE` | MIT 许可证 |
| `.github/FUNDING.yml` | 赞助信息 |
| `.gitignore` / `.npmignore` | 忽略构建产物与本地额外检出的目录（如 `/website`） |

克隆完成后、执行 `cm install` **之前**，仓库根目录的完整结构是：

```text
dev/
├── .github/            # 目前只有 FUNDING.yml（赞助配置）
├── bin/
│   ├── build-readme.js # 文档生成脚本
│   ├── cm.js           # 命令行入口（本套手册的主角）
│   └── packages.js     # 包清单注册表
├── demo/
│   ├── demo.ts         # 演示编辑器的源码
│   ├── index.html      # 演示页面外壳
│   ├── test/           # mocha.js / mocha.css 符号链接（指向根 node_modules）
│   └── website -> ../website/output/   # 指向本地额外检出的网站构建产物
├── .gitignore
├── .npmignore
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── package.json
└── tsconfig.json
```

两个「克隆后才生效」的细节值得注意：

- `demo/test/mocha.js` 和 `mocha.css` 是符号链接，指向根目录 `node_modules/mocha/` 下的文件——在运行 `npm install` 之前它们是断链。
- `demo/website` 指向仓库根的 `website/output/`，而 `/website` 被列在 [.gitignore:L6](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/.gitignore#L6-L6) 中，说明它是本地手动检出的 CodeMirror 网站仓库，不属于本仓库版本控制范围。
- 执行 `cm install` 后，根目录还会多出三十多个包目录（`state/`、`view/`、`lang-javascript/`……）和 `node_modules/`，届时目录会「膨胀」很多——这是多仓库装配的结果，下一讲详解。

## 4. 核心概念与源码讲解

### 4.1 README 中的三条核心命令：install / build / dev

#### 4.1.1 概念说明

README 是理解这个仓库定位的最短路径。它只有 23 行，却回答了三个关键问题：

1. **这个仓库是什么？**——CodeMirror 的中央仓库，装着 bug 跟踪器和开发脚本（第 5 行）。
2. **谁需要它？**——只有想参与开发的人；使用者请直接装 npm 包（第 7 行）。
3. **怎么开始？**——三条命令：`install`（装配环境）、`build`（构建）、`dev`（起开发服务器）。

「中央仓库」这个定位是理解一切的前提：编辑器本体分散在 `@codemirror/state`、`@codemirror/view` 等三十多个**各自独立、单独发版**的 git 仓库里，本仓库不包含它们的源码，而是提供一套脚本把这些仓库克隆到本地并协调它们一起构建、测试、发布。

#### 4.1.2 核心流程

一个新贡献者的标准启动流程：

```text
克隆本仓库
    │
    ├─ 确认 Node.js ≥ 16          （README 第 9 行）
    │
    ├─ node bin/cm.js install     克隆所有包仓库 → 装依赖 → 首次构建
    │
    ├─ （可选）node bin/cm.js build   全量重建所有包
    │       └─ 也可以只进某个子包目录，运行它自己的 npm run prepare
    │
    └─ npm run dev                起服务器：监听 8090 端口
            ├─ http://localhost:8090       → demo 演示页
            └─ http://localhost:8090/test/ → 浏览器测试页
```

要点：

- `install` 是一次性动作，`build` 是可反复执行的动作，`dev` 是长驻进程。
- `dev` 服务器会**在代码变化时自动重建对应包**，所以日常开发基本不需要手动 build。
- 三条命令的统一入口都是 `bin/cm.js`，`npm run dev` 只是 `node bin/cm.js devserver` 的别名（见 4.2）。

#### 4.1.3 源码精读

先看定位声明——这是全篇最重要的一句：

> [README.md:L5-L7](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L5-L7)
>
> ```md
> This is the central repository for CodeMirror. It holds the bug tracker
> and development scripts.
>
> If you want to **use** CodeMirror, install the separate packages from
> npm, and ignore the contents of this repository. If you want to
> **develop on** CodeMirror, this repository provides scripts to install
> and work with the various packages.
> ```

这两行把读者分成了两类：**use**（用 npm 包，忽略本仓库）和 **develop on**（用本仓库的脚本装配环境）。注意 "bug tracker" 也住在这个仓库里——README 顶部的 [ISSUES](https://code.haverbeke.berlin/codemirror/dev/issues) 链接正指向这里的 issue 区。

接着是装配命令：

> [README.md:L9-L11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L9-L11)
>
> ```md
> To get started, make sure you are running node.js version 16. After
> cloning the repository, run
>
>     node bin/cm.js install
> ```

`bin/cm.js` 就是一切的入口。第 9 行对 Node 版本有明确要求（16+）。

然后是构建：

> [README.md:L13-L15](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L13-L15)
>
> ```md
> to clone the packages that make up the system, install dependencies,
> and build the packages. At any time you can rebuild packages, either
> by running `npm run prepare` in their subdirectory, or all at once with
>
>     node bin/cm.js build
> ```

注意这里出现了**两个层次**的重建入口：进某个子包目录跑 `npm run prepare`（单包，脚本定义在各包自己的 `package.json` 里，克隆后才可见），或在根目录跑 `node bin/cm.js build`（所有包）。

最后是开发服务器：

> [README.md:L17-L21](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L17-L21)
>
> ```md
> Developing is best done by setting up
>
>     npm run dev
>
> which starts a server that automatically rebuilds the packages when
> their code changes and exposes a dev server on port 8090 running the
> demo and browser tests.
> ```

`npm run dev` = `node bin/cm.js devserver`（见 [package.json:L7](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L7-L7)），监听 8090 端口，提供 demo（`/`）和浏览器测试（`/test/`）两个页面。

三条命令在 `cm.js` 的帮助文本里也能一一对照：

> [bin/cm.js:L39-L55](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L39-L55)
>
> ```text
>   cm install [--ssh]      Clone and symlink the packages, install deps, build
>   cm build                Build the bundle files
>   cm devserver [--source-map]
>                           Start a dev server on port 8090
>   ...
> ```

帮助文本里还列出了 `status`、`commit`、`push`、`release`、`grep` 等十余个子命令——它们是第 2、3 单元的主角，本讲只需混个脸熟。

#### 4.1.4 代码实践

**实践目标**：不看讲义，独立列出本仓库的三条核心命令及其作用，并与 `cm` 工具自己的帮助文本互相印证。

**操作步骤**：

1. 通读 [README.md](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md) 全文（只有 23 行）。
2. 在仓库根目录执行：
   ```bash
   node bin/cm.js --help
   ```
3. 把 README 提到的 `install`、`build`、`devserver` 三条命令，与 `--help` 输出中的对应行逐条对照。

**需要观察的现象**：

- `--help` 输出的第一段 `Usage:` 文本与 [bin/cm.js:L39-L55](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L39-L55) 中的模板字符串完全一致。
- README 的 `npm run dev` 在帮助文本中对应的是 `cm devserver`，且帮助文本标明了端口 8090 和 `--source-map` 选项。

**预期结果**：`--help` 退出码为 0 并打印用法；你能在输出中找到 `install`、`build`、`devserver` 三行，描述与 README 一致。（本实践不执行 `install` 本身——那需要克隆三十多个仓库，留给下一讲。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 说使用者应该「ignore the contents of this repository」？

<details>
<summary>参考答案</summary>

因为 CodeMirror 6 的功能代码全部在独立发布的 npm 包里（`@codemirror/state`、`@codemirror/view`、`codemirror` 等），本仓库只包含开发脚本、issue 跟踪和 demo，不含任何编辑器实现。使用者 `npm install` 对应包即可，本仓库对他们没有直接用处。
</details>

**练习 2**：README 提到「在子包目录里运行 `npm run prepare`」和「`node bin/cm.js build`」两种重建方式，它们的区别是什么？

<details>
<summary>参考答案</summary>

前者是**单包**重建：`prepare` 脚本定义在每个包仓库自己的 `package.json` 里（克隆后才存在），只重建你所在的那个包。后者是**全量**重建：`cm build` 遍历注册表中所有可构建的包逐一构建。日常改一个包时用前者更快，CI 或首次装配场景用后者。
</details>

**练习 3**：`npm run dev` 启动的服务器暴露了哪两个页面？端口号在哪里文档化？

<details>
<summary>参考答案</summary>

demo 演示页（`http://localhost:8090`）和浏览器测试页（`http://localhost:8090/test/`）。端口号 8090 在 [README.md:L21](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L21-L21) 和 [bin/cm.js:L46](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L46-L46)（帮助文本）两处都有记载。
</details>

### 4.2 package.json：scripts 与 workspaces 配置

#### 4.2.1 概念说明

根目录的 [package.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json) 只有 26 行，却是整个工具链的「总开关」。它做了三件事：

1. **定义 4 个 npm scripts**，把常用 `cm` 子命令包装成更顺手的 `npm run <name>`。
2. **声明 `workspaces: ["*"]`**，把根目录下所有（克隆出来的）子目录收编为 npm 工作区。
3. **列出 5 个 devDependencies**，即工具链自身需要的构建/文档/服务器依赖。

`workspaces: ["*"]` 是多仓库方案能工作的关键一环：`cm install` 把三十多个包仓库克隆到根目录后，根目录的一次 `npm install` 会把所有子包的依赖统一装进根 `node_modules`，并让子包之间可以按包名互相引用源码——相当于把「物理上分散的多个仓库」在本地拼装成「逻辑上的一个 monorepo」。

#### 4.2.2 核心流程

```text
npm run <script>                实际执行
─────────────────────────────────────────────────────
npm run test          ──────►   node bin/cm.js test               （浏览器 + Node 双轨测试）
npm run test-node     ──────►   node bin/cm.js test --no-browser  （仅 Node 轨道）
npm run prepare       ──────►   node bin/cm.js build              （构建所有包）
npm run dev           ──────►   node bin/cm.js devserver          （8090 开发服务器）
```

另一个值得注意的机制是 `prepare` 这个名字本身：它是 npm 的**生命周期脚本**，按 npm 约定会在依赖安装完成后自动执行。也就是说，当 `cm install` 在根目录跑 `npm install` 时，根 `prepare`（即 `cm build`）会被连带触发——这正是 README 所说 install 会 "build the packages" 的钩子之一。具体触发链在下一讲读 `install()` 源码时验证。

#### 4.2.3 源码精读

先看整体和 scripts：

> [package.json:L1-L8](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L1-L8)
>
> ```json
> {
>   "description": "Development environment for the CodeMirror 6 packages",
>   "scripts": {
>     "test": "node bin/cm.js test",
>     "test-node": "node bin/cm.js test --no-browser",
>     "prepare": "node bin/cm.js build",
>     "dev": "node bin/cm.js devserver"
>   },
> ```

description 一句话点明身份：这是「CodeMirror 6 各包的开发环境」。四个 scripts 是四个薄薄的别名，全部转发给 `bin/cm.js`——命令逻辑永远只有一份，npm scripts 只是入口皮层。`test-node` 通过给 `cm test` 追加 `--no-browser` 参数来跳过浏览器轨道。

再看依赖与工作区声明：

> [package.json:L11-L17](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L11-L17)
>
> ```json
>   "devDependencies": {
>     "@codemirror/buildhelper": "^1.0.2",
>     "esmoduleserve": "^0.2.0",
>     "serve-static": "^1.14.1",
>     "getdocs-ts": "^1.0.0",
>     "builddocs": "^1.0.0"
>   },
> ```

这 5 个依赖对应工具链的三大职能：`@codemirror/buildhelper` 负责打包构建；`esmoduleserve` + `serve-static` 负责开发服务器的模块编译与静态文件托管；`getdocs-ts` + `builddocs` 负责从 TypeScript 源码提取注释并渲染 API 文档（供 `build-readme` 使用）。

> [package.json:L22-L25](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L22-L25)
>
> ```json
>   "workspaces": [
>     "*"
>   ],
>   "private": true
> ```

`"*"` 通配根目录下所有含 `package.json` 的子目录——`cm install` 克隆出的每个包仓库都天然满足条件。`"private": true` 防止这个开发环境被误发布到 npm。

还有一条与「不要提前加载依赖」相关的防御性注释，值得现在就留下印象（下一讲展开）：

> [bin/cm.js:L3-L4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L3-L4)
>
> ```js
> // NOTE: Don't require anything from node_modules here, since the
> // install script has to be able to run _before_ that exists.
> ```

`cm.js` 顶部只 `require` Node 内置模块，因为 `install` 命令必须在 `node_modules` 存在**之前**就能运行——这也解释了为什么 `package.json` 的 devDependencies 虽然存在，`cm.js` 的启动路径却不依赖它们。

#### 4.2.4 代码实践

**实践目标**：验证 4 个 npm scripts 与 `cm` 子命令的对应关系，并确认工具链依赖的落位。

**操作步骤**：

1. 打开 [package.json:L3-L8](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L3-L8)，抄下四个 scripts 的命令行。
2. 执行 `npm run`（不带参数），npm 会列出所有可用脚本及说明。
3. 执行 `node bin/cm.js --help`，把帮助文本里的 `test`、`build`、`devserver` 三行与步骤 1 的记录逐条比对。

**需要观察的现象**：

- `npm run` 列出的脚本集合恰好是 `test`、`test-node`、`prepare`、`dev` 四个。
- `test-node` 与 `test` 的差别只在 `--no-browser` 一个参数上。

**预期结果**：四个脚本全部能映射到 `cm` 子命令——`test → cm test`、`test-node → cm test --no-browser`、`prepare → cm build`、`dev → cm devserver`。若尚未执行 `cm install`，`npm run` 本身仍可正常运行（列出清单不需要依赖）；但真正执行 `npm run test` 或 `npm run dev` 会因缺少克隆的包而失败或提示未安装，属正常现象（待本地验证具体报错形式）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cm.js` 顶部刻意不 `require` 任何 `node_modules` 里的模块？

<details>
<summary>参考答案</summary>

因为 `cm install` 的职责恰恰是「创建 `node_modules`」——克隆包、安装依赖。如果 `cm.js` 在文件顶部就加载第三方依赖，那么在全新环境里第一次运行 `node bin/cm.js install` 会立刻因模块不存在而崩溃。所以启动路径只允许使用 Node 内置模块（见 [bin/cm.js:L3-L5](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L3-L5)）。
</details>

**练习 2**：`workspaces: ["*"]` 在 `cm install` 前后分别会匹配到什么？

<details>
<summary>参考答案</summary>

install 之前，根目录下没有含 `package.json` 的子目录（demo、bin 都没有），通配符基本匹配不到工作区；install 之后，三十多个克隆下来的包仓库各带 `package.json`，全部被收编为工作区，依赖统一提升到根 `node_modules`，子包之间可以互相按包名引用。
</details>

**练习 3**：devDependencies 里的 5 个包分别服务什么职能？

<details>
<summary>参考答案</summary>

`@codemirror/buildhelper`：构建打包；`esmoduleserve`：开发服务器上按需编译 ES 模块；`serve-static`：静态文件托管；`getdocs-ts`：从 TS 源码提取文档注释；`builddocs`：把注释渲染成 HTML 文档。后两者只被 `build-readme` 命令使用。
</details>

### 4.3 CONTRIBUTING.md：贡献流程

#### 4.3.1 概念说明

[CONTRIBUTING.md](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md) 回答「我想改代码/报 bug 该怎么做」。它分三大块：

1. **Getting help**：求助去论坛，不去 issue 区。
2. **Submitting bug reports**：报 bug 的质量规范（版本、环境、精确复现步骤、可复现脚本）。
3. **Contributing code**：从注册账号到提交 PR 的完整 checklist，外加一节硬性的编码规范。

需要特别如实转述的一条项目政策：文档第 38-41 行明确声明**不欢迎由 AI 语言模型编写（部分或全部）的代码**，理由是无法保证不鹦鹉学舌受版权保护的内容，且质量往往不高、浪费审阅时间。这是本项目的既定政策，参与贡献前必须知晓。

#### 4.3.2 核心流程

贡献代码的主链路（对应文档 36-66 行）：

```text
拥有 Codeberg 或 GitHub 账号
    │
    ├─ 用它在 code.haverbeke.berlin 注册账号
    │
    ├─ fork 目标包仓库
    │
    ├─ 本地检出代码（推荐用本 dev 仓库一次检出全部核心模块）
    │
    ├─ 修改 + commit（遵循编码规范）
    │
    ├─ 若易于测试/易回归 → 在相关包的 test/ 目录加测试
    │     └─ 放进已有的 test-*.js，或新建文件
    │
    ├─ npm run test 确认全部通过
    │
    └─ 提交 PR（一个 PR 只包含一个 feature/fix）
```

报 bug 的质量链路（对应文档 12-34 行）：

```text
确认是 bug 而非使用问题 ──否──► 去 discuss.codemirror.net 发帖
    │是
    ├─ 说明出现问题的代码版本（浏览器问题还需浏览器/系统版本）
    ├─ 精确描述：预期是什么、实际发生了什么、维护者如何复现
    └─ 不易复现时 ──► 用官方 sandbox（codemirror.net/try/）写触发脚本
```

#### 4.3.3 源码精读

求助与报 bug 的入口分工：

> [CONTRIBUTING.md:L7-L19](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L7-L19)
>
> ```md
> Community discussion, questions, and informal bug reporting is done on
> the discuss.CodeMirror forum.
>
> Report bugs on the issue tracker. ...
>
> - The issue tracker is for *bugs*, not requests for help. Questions
>   should be asked on the forum.
> ```

两条渠道边界清晰：论坛管「问」，issue 管「坏」。

贡献代码的关键步骤（账号与本地检出）：

> [CONTRIBUTING.md:L43-L53](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L43-L53)
>
> ```md
> - Make sure you have a Codeberg or GitHub account.
> - Use that to create a code.haverbeke.berlin account.
> - Fork the relevant repository.
> - Create a local checkout of the code. You can use the dev repository
>   to easily check out all core modules.
> ```

注意第 51-53 行：官方推荐**用本 dev 仓库**来检出全部核心模块——这正是本套手册教你做的事。

测试与验证要求：

> [CONTRIBUTING.md:L59-L66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L59-L66)
>
> ```md
> - If your changes are easy to test or likely to regress, add tests in
>   the relevant `test/` directory. Either put them in an existing
>   `test-*.js` file, if they fit there, or add a new file.
> - Make sure all tests pass. Run `npm run test` to verify tests pass.
> - Submit a pull request. Don't put more than one feature/fix in a
>   single pull request.
> ```

`test/` 目录和 `test-*.js` 命名约定在各**包仓库**内部（克隆后可见，如 `state/test/`）；本仓库根的 `demo/test/` 只是浏览器测试页的 mocha 资产挂载点，不放测试用例。`npm run test` 就是 4.2 节讲过的 `cm test` 别名。

编码规范（提交 PR 前的硬性要求）：

> [CONTRIBUTING.md:L79-L99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L79-L99)
>
> ```md
> - TypeScript, targeting an ES2018 runtime.
> - 2 spaces per indentation level, no tabs.
> - No semicolons except when necessary.
> - Follow the surrounding code when it comes to spacing, brace placement, etc.
> - Brace-less single-statement bodies are encouraged.
> - getdocs-style doc comments above items that are part of the public API.
> - CodeMirror does *not* follow JSHint or JSLint prescribed style.
> ```

读源码时你会反复看到这些风格的影子：`cm.js` 里大量无分号、无花括号的单语句体（如 [bin/cm.js:L34-L35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L34-L35) 的 `if (!cmdFn || cmdFn.length > args.length) help(1)`）正是这条规范的体现。

#### 4.3.4 代码实践

**实践目标**：把 CONTRIBUTING 的文字规范转成一份可执行的个人 checklist，并用仓库内证据验证其中两条。

**操作步骤**：

1. 通读 [CONTRIBUTING.md](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md)，把「Contributing code」一节的每个 bullet 改写成一条可勾选的待办项。
2. 验证「2 空格缩进、无分号」：打开 [bin/cm.js:L13-L36](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L13-L36)，观察 `start()` 函数的缩进与语句结尾。
3. 验证「测试放 `test/` 目录、命名 `test-*.js`」：留意本仓库根并没有任何 `test-*.js` 文件，结合 [demo/test/](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/demo.ts) 目录里只有 mocha 符号链接这一事实，得出「测试用例在各包仓库内」的结论（执行 `cm install` 后可到任一包目录下确认，待本地验证）。

**需要观察的现象**：

- `cm.js` 源码确实使用 2 空格缩进，绝大多数语句结尾没有分号。
- 本仓库自身没有测试用例文件——它是一个「纯工具」仓库。

**预期结果**：你得到一份 8-10 条的个人贡献 checklist；并能解释为什么在本仓库里找不到 `test-*.js`（因为被测对象是各包，测试也住在各包仓库里）。

#### 4.3.5 小练习与答案

**练习 1**：我想问「怎么在 Vue 里集成 CodeMirror」，应该去 issue 区还是论坛？为什么？

<details>
<summary>参考答案</summary>

论坛（discuss.codemirror.net）。[CONTRIBUTING.md:L18-L19](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L18-L19) 明确规定 issue tracker 只收 bug，求助类问题应发到论坛。
</details>

**练习 2**：为什么规范鼓励「一个 PR 只包含一个 feature/fix」？

<details>
<summary>参考答案</summary>

对应 [CONTRIBUTING.md:L65-L66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L65-L66)。混合多个改动的 PR 会显著加重审阅负担：审阅者难以逐项判断正确性，一旦其中一个改动有问题，整个 PR 都会被阻塞或返工。这也是小型维护者团队常见的工程约定。
</details>

**练习 3**：`npm run test`（CONTRIBUTING 要求贡献前运行）最终执行的是什么？

<details>
<summary>参考答案</summary>

[package.json:L4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L4-L4) 定义 `test` 为 `node bin/cm.js test`，即遍历所有包、分别跑其浏览器测试与 Node 测试的双轨测试命令（细节在第 2 单元第 4 讲）。
</details>

## 5. 综合实践

**任务**：为这个仓库建立一张你自己的「认知地图」，并打通 npm scripts 与 `cm` 子命令的对应关系。

**步骤**：

1. **画目录树**：在不看本讲义的情况下，参照第 3 节的方法，用 `ls -la` 检查仓库根、`bin/`、`demo/`、`demo/test/`，手工画出目录树，并为每个条目标注一句话用途。特别标注三类条目：
   - 克隆后就有的（`bin/`、`demo/`、三个 `.md`/配置文件）；
   - 只有 `cm install` 之后才有效的（`demo/test/` 下的 mocha 符号链接、未来出现的三十多个包目录与 `node_modules/`）；
   - 指向版本控制之外的（`demo/website` 符号链接，参见 [.gitignore:L6](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/.gitignore#L6-L6) 的 `/website`）。
2. **做映射表**：列出 `package.json` 中 `test`、`test-node`、`prepare`、`dev` 四个脚本各自对应的 `cm` 子命令，并写明每条命令的用途。用 `node bin/cm.js --help` 的输出核对你的答案。
3. **交叉验证**：把 README 提到的命令（`node bin/cm.js install`、`node bin/cm.js build`、`npm run dev`）与你映射表中的条目对照，确认 README、package.json、cm.js 帮助文本三处信息自洽。

**参考答案（映射表）**：

| npm script | 实际命令 | 用途 |
| --- | --- | --- |
| `npm run test` | `cm test` | 跑所有包的浏览器 + Node 双轨测试 |
| `npm run test-node` | `cm test --no-browser` | 只跑 Node 轨道测试 |
| `npm run prepare` | `cm build` | 构建所有包（npm 生命周期钩子，装完依赖后自动触发） |
| `npm run dev` | `cm devserver` | 启动 8090 端口开发服务器（demo + 浏览器测试） |

**预期结果**：一张三分层目录树 + 一张四行映射表；三处文档信息互相印证，无矛盾。目录树的「克隆后才有效」分层是下一讲 `cm install` 的伏笔——那些断链的符号链接和多出来的包目录，正是 install 要修复和创建的东西。

## 6. 本讲小结

- 本仓库是 CodeMirror 的**中央开发仓库**：只有开发脚本（`bin/cm.js` 等）、bug 跟踪和 demo，编辑器本体分散在三十多个独立的 `@codemirror/*` 包仓库中。
- 使用者直接装 npm 包即可，无需本仓库；本仓库面向**贡献者**。
- 三条核心命令：`cm install`（克隆包 + 装依赖 + 首次构建）、`cm build`（全量重建）、`npm run dev`（8090 端口开发服务器，自动重建 + demo + 浏览器测试）。
- 根 `package.json` 的 4 个 scripts 全是 `bin/cm.js` 的别名：`test→cm test`、`test-node→cm test --no-browser`、`prepare→cm build`、`dev→cm devserver`；`workspaces: ["*"]` 把克隆出的包目录收编为 npm 工作区。
- 贡献流程的要点：论坛问问题、issue 报 bug、一个 PR 一个改动、测试放各包的 `test/` 目录、提交前 `npm run test`；项目明确不欢迎 AI 生成的代码贡献。
- 仓库里有若干「克隆后才生效」的条目（mocha 符号链接、`/website` 忽略项），它们是理解下一讲 `cm install` 装配行为的线索。

## 7. 下一步学习建议

- **下一讲（u1-l2）《从零跑起来：cm install 与多仓库装配》**：精读 `bin/cm.js` 中的 `install()`、`assertInstalled()` 与 `run()`，看它如何把 [bin/packages.js:L3-L42](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L42) 列出的 core 与 nonCore 包逐个克隆到本地，并理解文件顶部「不要提前 require node_modules」注释的真正含义。
- 在那之前，建议先自行浏览一遍 [bin/cm.js:L17-L33](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L17-L33) 的命令映射表，对全部子命令建立整体印象——后续每一讲都会从中取一个函数精读。
- 想提前了解 CodeMirror 编辑器本身的读者，可以看官方文档 https://codemirror.net/docs/ref （README 第 23 行给出的入口），但注意那是「使用者视角」的 API 文档，与本手册的「工具链视角」互补而非重复。
