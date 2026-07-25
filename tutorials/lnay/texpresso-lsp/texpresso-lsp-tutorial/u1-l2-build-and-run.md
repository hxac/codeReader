# 构建、运行与编辑器集成

## 1. 本讲目标

上一讲（[u1-l1](u1-l1-project-overview.md)）我们建立了「编辑器 ⇄ texpresso-lsp ⇄ texpresso 子进程」三层架构的心智模型，并知道了这是个实验性的「薄壳」项目。本讲要回答的是一个非常现实的问题：**这套东西到底怎么在本机跑起来、怎么接进我的编辑器？**

学完本讲你应该能够：

1. 看懂 `package.json` 里的四个 npm 脚本（`build` / `start` / `dev` / `watch`）各自做什么，并能选用合适的命令。
2. 说清楚 texpresso-lsp 的前置依赖（`texpresso` 可执行文件）与 `PATH` 的关系，理解为什么可以「不写 `texpresso_path`」。
3. 理解一条初始化选项（initialization options）从「编辑器启动命令」一路流到 `server.ts` 内部配置对象的过程。
4. 为任意一个支持 LSP 的编辑器编写连接 `texpresso-lsp --stdio` 的配置。

本讲只解决「让服务器在编辑器里活着」这件事，不展开它启动之后如何收发命令（那是 [u2-l3](u2-l3-json-line-protocol.md) 的主题）。

## 2. 前置知识

- **Node.js 与 npm**：本项目是 Node 程序，用 npm 管理依赖和脚本。你需要大致知道 `npm install`、`npm run <脚本名>`、`npm install -g`（全局安装）的区别。
- **TypeScript 与 `tsc`**：源码是 `.ts`，运行前要用 TypeScript 编译器 `tsc` 编译成 `.js`。本项目的 `tsc` 配置在 `tsconfig.json` 里。
- **可执行文件与 `PATH`**：`PATH` 环境变量是一个目录列表，Shell 在执行命令名（如 `texpresso`）时，会依次在这些目录里查找同名可执行文件。
- **LSP 的 stdio 传输**：LSP 服务器和编辑器之间需要一个「传输通道」。最常见的就是 stdio（标准输入/输出）——编辑器把服务器当成一个子进程启动，用它的 stdin/stdout 收发 JSON-RPC 消息。这就是为什么命令里总有 `--stdio`。
- **初始化选项（initialization options）**：LSP 握手的第一步是 `initialize` 请求，客户端可以在这个请求里附带一个自由结构的 `initializationOptions` 字段，把「启动期配置」塞给服务器。本项目正是用它来传 `root_tex` / `texpresso_path` / `inverse_search`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md) | 最重要的「上手说明书」：安装步骤、初始化选项示例、工作区设置、通用 npm 指令都在这里。 |
| [package.json](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json) | 声明依赖、npm 脚本、程序入口（`main`）和可执行命令（`bin`）。 |
| [bin/texpresso-lsp.sh](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh) | 全局安装后真正被 `texpresso-lsp` 命令调用的入口脚本，只有两行。 |
| [tsconfig.json](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json) | TypeScript 编译配置，决定了「源码目录 → 产物目录」的映射。 |
| [src/server.ts](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts) | 服务器主文件。本讲只看它的顶部：默认配置、读取初始化选项、用配置启动子进程。 |

---

## 4. 核心概念与源码讲解

### 4.1 依赖安装与构建命令

#### 4.1.1 概念说明

一个 TypeScript 写的 Node 项目，从「克隆源码」到「能被运行」之间有两件事要做：

1. **安装依赖**：把 `package.json` 里声明的第三方库（如 `vscode-languageserver`）下载到本地的 `node_modules/`。
2. **编译**：把 `.ts` 源码用 `tsc` 翻译成 Node 能直接执行的 `.js`。

texpresso-lsp 把这两步分别交给 `npm install` 和 `npm run build`，而具体「跑什么命令」就定义在 `package.json` 的 `scripts` 字段里。

#### 4.1.2 核心流程

开发期从源码运行的标准流程：

```text
克隆仓库
  └─> npm install          # 安装依赖（含 devDependencies，如 tsc / ts-node）
        └─> npm run build   # tsc 把 src/*.ts 编译到 dist/*.js
              └─> npm start # node dist/server.js 启动编译产物
```

四个脚本各自适合的场景：

| 脚本 | 实际命令 | 何时用 |
| --- | --- | --- |
| `npm run build` | `tsc` | 一次性编译，产物落到 `dist/` |
| `npm start` | `node dist/server.js` | 运行**编译后**的产物（需先 build） |
| `npm run dev` | `ts-node src/server.ts` | 开发时**免编译直接跑源码**（ts-node 内存里编译） |
| `npm run watch` | `tsc -w` | 边写边增量编译，文件一保存就重新生成 `dist/` |

注意：`npm start` 跑的是 `dist/server.js`，所以**第一次必须先 `npm run build`**，否则 `dist/` 不存在会报错。`npm run dev` 则绕过 `dist/`，直接跑 `src/server.ts`，适合调试。

#### 4.1.3 源码精读

`package.json` 的 `scripts` 字段定义了上述全部命令：

[package.json:7-12](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L7-L12) —— 定义 `build` / `start` / `dev` / `watch` 四个脚本，分别是 `tsc`、`node dist/server.js`、`ts-node src/server.ts`、`tsc -w`。

这些脚本背后依赖的库同样在 `package.json` 中声明：

[package.json:13-22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L13-L22) —— `dependencies` 是运行时必需的（`vscode-languageserver` 等），`devDependencies` 只有开发/编译时需要（`typescript`、`ts-node`、`@types/node`）。

编译产物落在哪、源码从哪读，由 `tsconfig.json` 决定：

[tsconfig.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L5-L6) —— `rootDir: "./src"` 表示源码根目录，`outDir: "./dist"` 表示编译产物目录。这条映射关系是「`src/server.ts` → `dist/server.js`」的来源。

[tsconfig.json:7](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L7) —— `strict: true` 开启严格类型检查，这意味着改源码时类型错误会被 `tsc` 拦下，是本项目代码质量的一道保险。

这套「`npm ci` + `npm run build`」的流程并非只存在于本地，项目的发布 CI 就是用同样的两步来构建并发布 npm 包的：

[.github/workflows/npm-publish.yml:30-32](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.github/workflows/npm-publish.yml#L30-L32) —— 发布流程里依次执行 `npm ci`（按 lock 文件确定性安装依赖）、`npm run build`（编译）、`npm publish`（发布到 npm 仓库）。这也正是你「`npm install -g texpresso-lsp`」能拿到现成包的原因。

> 补充：README 中特意写明 `# no tests yet:`（[npm-publish.yml:15-20](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/.github/workflows/npm-publish.yml#L15-L20) 的注释块），CI 里目前**没有测试步骤**，这也是本讲义后续实践多以「源码阅读 + 手动观察」为主的原因。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次「从源码到产物」的构建，并验证产物确实生成。

**操作步骤**：

1. 在仓库根目录执行 `npm install`，观察 `node_modules/` 是否生成、`vscode-languageserver` 是否被安装。
2. 执行 `npm run build`。
3. 用 `ls dist/` 查看编译产物。

**需要观察的现象**：

- 第 1 步结束后，`node_modules/` 出现，且包含 `vscode-languageserver`、`vscode-uri` 等依赖。
- 第 2 步结束后，多出一个 `dist/` 目录。

**预期结果**：

- `dist/server.js`、`dist/process-manager.js`、`dist/types.js` 三个文件生成（与 `src/` 下三个 `.ts` 一一对应，映射规则来自 [tsconfig.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L5-L6)）。

> 若本地未配置 Node 环境，无法实际执行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果不执行 `npm run build` 就直接 `npm start`，会发生什么？为什么？

**参考答案**：`npm start` 等价于 `node dist/server.js`（见 [package.json:9](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L9)）。若未编译，`dist/` 目录或 `dist/server.js` 不存在，Node 会抛出「找不到模块」错误。解决办法是先 `npm run build`，或改用 `npm run dev`（直接跑 `.ts` 源码）。

**练习 2**：`npm run dev` 与 `npm start` 都能启动服务器，它们运行的是同一份代码吗？

**参考答案**：逻辑上是同一份源码（`src/server.ts`），但物理形态不同：`npm run dev` 用 `ts-node` 在内存里即时编译并运行 `.ts`；`npm start` 运行的是 `tsc` 预先编译到磁盘的 `dist/server.js`。调试时常用前者，正式运行/分发用后者。

---

### 4.2 运行方式：从源码到进程

#### 4.2.1 概念说明

构建之后，运行方式分两条路：

- **作为开发者**：直接 `npm start` 或 `npm run dev`，跑当前仓库的代码。
- **作为终端用户**：`npm install -g texpresso-lsp` 全局安装后，在命令行直接敲 `texpresso-lsp --stdio`。

第二条路之所以能成立，靠的是 `package.json` 的 `bin` 字段 + 一个两行的入口脚本。理解这条链路是本节的核心。

另外有一个**外部前置依赖**：`texpresso` 可执行文件。texpresso-lsp 自己不带渲染器，它会在握手时去 spawn 一个 `texpresso` 子进程。所以「能跑」的前提是系统里先有 `texpresso`。

#### 4.2.2 核心流程

编辑器调用 `texpresso-lsp --stdio` 时的调用链（全局安装场景）：

```text
编辑器启动命令: texpresso-lsp --stdio
       │
       ▼  (npm 根据 package.json 的 bin 字段找到脚本)
bin/texpresso-lsp.sh        # #!/usr/bin/env node / require('../dist/server.js')
       │
       ▼
dist/server.js              # 编译产物，真正的主程序
       │
       ▼  (onInitialize 握手时)
child_process.spawn("texpresso", ...)   # 启动外部 texpresso 子进程
```

开发者本地场景则省去前两步，直接 `node dist/server.js` 或 `ts-node src/server.ts`。

#### 4.2.3 源码精读

**入口脚本只有两行**：

[bin/texpresso-lsp.sh:1-2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh#L1-L2) —— shebang `#!/usr/bin/env node` 声明用 Node 解释执行；`require('../dist/server.js')` 加载编译产物。注意它**不处理 `--stdio` 参数**，只是把控制权交给 `server.js`。

那么 `texpresso-lsp` 这个命令名怎么和这个脚本对应上的？答案在 `package.json` 的 `bin` 字段：

[package.json:6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L6) —— `"bin": "bin/texpresso-lsp.sh"`。npm 在全局安装（`npm install -g`）时，会以包名 `texpresso-lsp` 为名字，在系统 PATH 目录里创建一个指向该脚本的符号链接，于是命令行就能直接调用 `texpresso-lsp`。（该脚本本身有可执行权限，见仓库中文件模式为 `100755`。）

`main` 与 `bin` 的分工：

[package.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/package.json#L5-L6) —— `main: "dist/server.js"` 是「作为库被 `require` 时的入口」；`bin: "bin/texpresso-lsp.sh"` 是「作为命令行工具被直接运行时的入口」。本项目主要被当作命令行工具用，所以实践中走的是 `bin` 这条路。

关于 `--stdio`：这是 LSP 的约定俗成标志。`server.ts` 调用 `createConnection(ProposedFeatures.all)` 创建连接（见 [src/server.ts:31-36](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L31-L36)），`vscode-languageserver` 库会检查 `process.argv` 里的 `--stdio` 来选择 stdio 传输方式。所以 README 才反复强调命令必须带 `--stdio`。

README 里关于前置依赖 `texpresso` 的说明（必须在 PATH 里或显式指定路径）：

[README.md:19-22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L19-L22) —— 第 1 步要求安装 TeXpresso（且需从特定分支构建），第 2 步要求把 `texpresso` 放进 PATH，否则就要在初始化选项里用 `texpresso_path` 显式指定。

通用运行指令（开发场景）：

[README.md:56-76](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L56-L76) —— README 的「Generic npm package instructions」一节，给出 `npm install` → `npm run build` → `npm start` 的标准三步，以及开发用的 `npm run dev`。

#### 4.2.4 代码实践

**实践目标**：追踪「命令名 → 脚本 → 产物」这条链路，并用最小脚本复现全局命令的原理。

**操作步骤**：

1. 在仓库根目录执行 `node bin/texpresso-lsp.sh`（前提是已 `npm run build`），观察是否等价于 `npm start`。
2. 阅读这两行脚本：[bin/texpresso-lsp.sh:1-2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh#L1-L2)，确认它只是 `require('../dist/server.js')`。
3. （可选）写一个最小复现：新建一个临时目录，放一个 `package.json`，设置 `"bin": "./mybin.sh"`，写个两行脚本 `#!/usr/bin/env node / console.log("hi")`，执行 `npm link`，然后在任意目录运行你定义的命令名。

**需要观察的现象**：

- 第 1 步与 `npm start` 行为一致：服务器进程启动并阻塞在 stdio 等待（因为没有编辑器发握手，它会等待输入）。可用 `Ctrl+C` 退出。
- 第 3 步能看到 npm 把你的命令名注册到了系统 PATH 指向的目录。

**预期结果**：理解 `package.json` 的 `bin` 字段是「命令名 → 脚本」映射的唯一来源，`texpresso-lsp` 这个命令并非凭空出现。

> 实际运行结果待本地验证；若未安装 `texpresso`，第 1 步虽能启动 Node 进程，但握手阶段会因 spawn 失败而报错（参见 [u3-l3](u3-l3-error-and-lifecycle.md) 的错误处理）。

#### 4.2.5 小练习与答案

**练习 1**：`bin/texpresso-lsp.sh` 里写的是 `require('../dist/server.js')`，这个相对路径是相对于谁解析的？

**参考答案**：相对于**该脚本文件自身所在目录**（即 `bin/`）。Node 中 `require` 的相对路径始终相对于「发起 require 的那个文件」定位，所以 `../dist/server.js` 指向仓库根目录下的 `dist/server.js`，正好对应 [tsconfig.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L5-L6) 编译出的产物。这也说明：**全局安装的包必须带着 `dist/` 一起发布**，否则脚本会找不到入口。

**练习 2**：为什么 `texpresso-lsp.sh` 这个文件名以 `.sh` 结尾，shebang 却是 `#!/usr/bin/env node` 而不是 `bash`？

**参考答案**：文件名后缀只是惯例、对运行无影响（Linux 靠 shebang 和可执行位决定解释器，不靠后缀）。真正决定它用 Node 执行的是第 1 行 shebang（[bin/texpresso-lsp.sh:1](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh#L1)），所以它本质是个 Node 脚本，叫 `.sh` 只是命名习惯。

---

### 4.3 编辑器集成与初始化选项

#### 4.3.1 概念说明

让服务器「跑起来」只是一半，另一半是「让编辑器找到并使用它」。这需要两件事：

1. **告诉编辑器怎么启动服务器**：即命令 `texpresso-lsp --stdio`。
2. **在握手时把配置塞给服务器**：即初始化选项 `root_tex` / `texpresso_path` / `inverse_search`。

关键概念是**初始化选项（initialization options）**：它是 LSP `initialize` 请求里的一个字段，结构由服务器自定义。texpresso-lsp 用它接收三个配置项：

| 配置项 | 含义 | 默认值 |
| --- | --- | --- |
| `root_tex` | 主 `.tex` 文件路径（可相对工作区根） | `main.tex` |
| `texpresso_path` | `texpresso` 可执行文件路径 | `texpresso`（即依赖 PATH） |
| `inverse_search.command` / `arguments` | 反向搜索时调用的编辑器命令及参数模板 | `zed` / `["%f:%l"]` |

默认值的存在意味着：**只要 `texpresso` 在 PATH 里、你的主文件叫 `main.tex`、你用 zed，那初始化选项可以完全不填。** 这就是 README 说 `texpresso_path` 「can be missed if texpresso is in PATH」的原因。

另外还有一类**工作区设置**（workspace settings），如 `texpresso.preview_follow_cursor`，它和初始化选项不同：初始化选项只在启动时传一次，工作区设置则可以在运行期热更新。本讲只关注前者，后者留到 [u2-l1](u2-l1-config-and-types.md) 与 [u3-l4](u3-l4-architecture-and-extensions.md)。

#### 4.3.2 核心流程

初始化选项从编辑器到服务器内部的流转：

```text
编辑器配置文件
  └─> initialize 请求的 initializationOptions 字段
        └─> server.ts onInitialize 回调接收 params
              └─> 用 ?? (空值合并) 与 defaultInitOpts 逐字段合并
                    └─> 写入 connection.init_options
                          └─> 用它构造 TexpressoProcessManager，启动子进程
```

合并规则（关键）：对每个字段，**客户端传了就用客户端的，没传（undefined/null）就用默认值**。这靠 `??`（空值合并运算符）实现。

#### 4.3.3 源码精读

默认配置 `defaultInitOpts` 就是上一节表格里那些默认值的具体化：

[src/server.ts:20-27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L20-L27) —— 定义 `defaultInitOpts`：`root_tex: "main.tex"`、`texpresso_path: "texpresso"`（注释明确「assumes texpresso is in PATH」）、`inverse_search` 默认指向 `zed`。

这份默认配置被挂到一个混合对象上，作为「运行期可变的服务器配置」：

[src/server.ts:31-36](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L31-L36) —— `connection` 对象把 `init_options: defaultInitOpts`、`workspace_config`、`is_texpresso_tonic_running` 这些自定义字段，与 `createConnection(ProposedFeatures.all)` 返回的 LSP 连接能力**展开合并**在一起。后续代码统一用 `connection.init_options.xxx` 读取配置。

真正接收编辑器配置、做合并的地方在 `onInitialize` 回调里：

[src/server.ts:52-61](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L52-L61) —— 若客户端传了 `params.initializationOptions`，就逐字段用 `??` 覆盖默认值：`root_tex`、`texpresso_path`、`inverse_search` 三个字段各自独立判断。这就是「不填的字段回落到默认」的实现机制。

合并完的配置立刻被用来启动子进程：

[src/server.ts:65-69](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L69) —— `new TexpressoProcessManager(connection.init_options.texpresso_path, ["-json", "-lines"], connection.init_options.root_tex)`。这里能清楚看到：**初始化选项里的 `texpresso_path` 决定了去 spawn 哪个可执行文件**，`root_tex` 作为参数传给子进程。

README 给出的初始化选项示例（编辑器侧的写法）：

[README.md:27-40](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L27-L40) —— 给出 `root_tex`、`texpresso_path`、`inverse_search` 三项的完整示例，并解释 `%f`、`%l` 是文件路径与行号的占位符。注意注释里写明 `root_tex` 默认 `main.tex`、`texpresso_path` 在 PATH 里时可省略——这两点恰好对应 [src/server.ts:20-27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L20-L27) 的默认值。

对于 zed 用户，README 还提供了一个更省事的替代方案——装扩展而非手写配置：

[README.md:41](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L41) —— 指向 `lnay/zed-texpresso` 扩展，该扩展会替你完成「启动 `texpresso-lsp --stdio` 并传入初始化选项」这整套流程。

#### 4.3.4 代码实践

**实践目标**：为你常用的编辑器写一份连接 texpresso-lsp 的初始化配置，并预测它如何被服务器的 `??` 合并逻辑处理。

**操作步骤**：

1. 重读 [README.md:27-40](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L27-L40) 的初始化选项示例。
2. 假设你的 `texpresso` 已经在 PATH 里、主文件就是 `main.tex`、但你用的是 VS Code（而非 zed）。请编写一份**只包含 `inverse_search`** 的初始化选项 JSON，把 `command` 改成 `code`（或 `codium`），`arguments` 写成能打开指定文件并跳到指定行的形式（VS Code 支持 `code --goto file:line`）。
3. 对照 [src/server.ts:52-61](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L52-L61)，逐字段推断：你的配置里没写 `root_tex` 和 `texpresso_path`，服务器最终会用什么值？

**需要观察的现象**：

- 由于省略了两个字段，`init_params.root_tex` 为 `undefined`，`??` 运算符会让它回落到 `defaultInitOpts.root_tex`（即 `"main.tex"`）。
- 同理 `texpresso_path` 回落到 `"texpresso"`。

**预期结果**：你写出的配置形如：

```jsonc
{
  "inverse_search": {
    "command": "code",
    "arguments": ["--goto", "%f:%l"]
  }
}
```

服务器运行时 `connection.init_options.root_tex === "main.tex"`、`texpresso_path === "texpresso"`、`inverse_search` 为你写的 `code` 配置。

> 该配置能否在具体编辑器中生效，取决于编辑器的 LSP 客户端是否支持自定义 `initializationOptions` 字段。VS Code 原生语言服务器扩展可在客户端构造时传入；若用通用 LSP 插件（如 coc.nvim、zed），按其文档填写即可。实际接入结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果编辑器配置里 `texpresso_path` 写成了空字符串 `""`（而不是省略），服务器会按「PATH 查找」来启动吗？

**参考答案**：**不会**。`??`（空值合并）只在值为 `undefined` 或 `null` 时回落，空字符串 `""` 是个有效值，不会被替换。于是 `connection.init_options.texpresso_path` 会是 `""`，导致 `spawn("")` 失败。要让默认值生效，必须**完全不传**这个字段（让其保持 `undefined`）。这是 `??` 与 `||` 的关键差异。

**练习 2**：为什么 README 说工作区设置（如 `preview_follow_cursor`）「can be changed at runtime」，而初始化选项却不能？

**参考答案**：初始化选项只在 LSP 握手的 `initialize` 请求里传一次（[src/server.ts:52-61](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L52-L61) 在 `onInitialize` 里读取一次就不再监听）；而工作区设置通过 `onDidChangeConfiguration` / `onInitialized` 等机制持续监听，服务器能随时拉取最新值（详见 [README.md:43-54](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L43-L54) 与讲义 [u2-l1](u2-l1-config-and-types.md)）。所以把「可能频繁改动」的项放进工作区设置、把「启动期一次性」的项放进初始化选项，是合理的设计分工。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，完成一次「从零到接入编辑器」的完整过程，并产出一页接入笔记。

请按顺序完成并记录每一步的输出：

1. **准备前置依赖**：确认本机已有 `texpresso` 可执行文件，并执行 `which texpresso`（或 `where texpresso`）记录它的路径。若没有，先按 [README.md:19-22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/README.md#L19-L22) 安装。
2. **从源码构建并运行**：`npm install` → `npm run build` → 确认 `dist/server.js` 存在（映射来自 [tsconfig.json:5-6](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/tsconfig.json#L5-L6)）。
3. **走通命令链路**：执行 `node bin/texpresso-lsp.sh`（等价于 `texpresso-lsp --stdio`，见 [bin/texpresso-lsp.sh:1-2](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh#L1-L2)），观察进程是否启动并在 stdio 上等待。
4. **编写编辑器配置**：为你的编辑器写一份初始化选项 JSON，至少包含 `inverse_search`，并据实决定是否补 `texpresso_path`（若 `which texpresso` 有结果则可省略）。
5. **画出数据流图**：绘制「编辑器启动命令 → `bin` 脚本 → `dist/server.js` → `onInitialize` 读取初始化选项 → `??` 合并默认值 → `new TexpressoProcessManager` 启动 `texpresso` 子进程」的完整时序。
6. **反思**：在笔记里写明，为什么「`texpresso` 在 PATH 里」能让初始化选项变短（结合 [src/server.ts:22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L22) 的默认值与 [src/server.ts:55-57](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L55-L57) 的 `??` 合并来解释）。

> 无法在本机执行的步骤，请如实标注「待本地验证」，不要编造输出。

## 6. 本讲小结

- `package.json` 的 `scripts` 定义了四条命令：`build`（`tsc`）、`start`（跑 `dist/server.js`）、`dev`（`ts-node` 免编译跑源码）、`watch`（增量编译）。
- `tsconfig.json` 的 `rootDir: ./src` / `outDir: ./dist` 决定了 `src/server.ts` → `dist/server.js` 的产物映射；`strict: true` 提供类型安全。
- 全局安装后的 `texpresso-lsp` 命令，本质是 `package.json` 的 `bin` 字段指向 [bin/texpresso-lsp.sh](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/bin/texpresso-lsp.sh)，该脚本再 `require('../dist/server.js')`。
- `--stdio` 是 LSP 约定的传输选择标志，`createConnection(ProposedFeatures.all)` 据此选用 stdio 传输。
- 外部前置依赖 `texpresso` 必须在 PATH 里，或在初始化选项里用 `texpresso_path` 显式指定。
- 初始化选项（`root_tex` / `texpresso_path` / `inverse_search`）经 `onInitialize` 的 `??` 合并默认值后写入 `connection.init_options`，并立即用于启动子进程。

## 7. 下一步学习建议

至此你已经能让 texpresso-lsp 在编辑器里「活着」。但本讲刻意没碰两件事：**服务器内部如何管理子进程**、**它和 texpresso 之间用什么协议通信**。

建议下一步：

- 阅读 [u2-l1 配置体系与类型定义](u2-l1-config-and-types.md)：把本讲出现的 `ServerConfig` / `WorkspaceSettings` 在 `types.ts` 里的正式定义看清楚，并区分初始化选项与工作区设置。
- 阅读 [u2-l2 进程管理器 TexpressoProcessManager](u2-l2-process-manager.md)：深入本讲末尾那句 `new TexpressoProcessManager(...)` 背后的实现，看 `spawn`、生命周期、状态管理如何运作。
- 顺带可以读 [u1-l3 目录结构与入口文件链路](u1-l3-structure-and-entry.md)，它对本讲的「bin/main/tsconfig 链路」有更系统的补充。
