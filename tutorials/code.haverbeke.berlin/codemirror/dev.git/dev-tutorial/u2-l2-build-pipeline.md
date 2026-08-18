# 构建流水线：cm build 背后的工具链

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整梳理从 `cm build` 命令到各包 `dist/` 产物的调用链：命令注册 → 入口清单 → 构建工具 → 产物落地。
2. 说清楚 `buildPackages` 这份「构建原料清单」是如何从 `Pkg.main` 探测规则过滤出来的，以及它与全量 `packages` 的差异。
3. 知道 `clean()` 删除哪些东西、它和 `cm install` 里的清理动作如何分工。
4. 理解 `tsconfig.json` 的 `paths` 如何把 `@codemirror/*` 指向兄弟仓库的 **TS 源码**，以及它和 npm workspaces 是两套各自独立、方向一致的解析机制。

## 2. 前置知识

本讲默认你已读完 u1-l2（install 与装配）和 u2-l1（包注册表），这里把会用到的概念再通俗地过一遍：

- **npm workspaces**：根 `package.json` 里 `"workspaces": ["*"]` 告诉 npm「当前目录下每个子目录都是一个包」。`npm install` 时 npm 会把_clone_出来的 36 个包目录收编为工作区，并在根 `node_modules/` 里为它们创建符号链接，使 `require("@codemirror/state")` 这类引用不需要真的去 npm 下载。
- **package.json 的 main 字段**：每个发布到 npm 的包都用 `main` 告诉 Node「入口文件在哪」。CodeMirror 各包的 `main` 指向自己仓库构建后的 `dist/` 产物（在各包仓库内定义）。也就是说：**没构建，包就不可用**——这就是本讲这条流水线存在的意义。
- **TypeScript 的 paths 别名**：`tsconfig.json` 里的 `paths` 是给 TypeScript 编译器（以及 IDE）看的模块解析映射表，可以把 `@codemirror/state` 这样的包名直接映射到一个 `.ts` 源文件，绕过默认的 node_modules 查找。
- **execFileSync 与 run() 封装**：cm.js 用 `run(cmd, args, wd)` 执行子进程并默认捕获 stdout（u1-l2 已精读），本讲的 `clean()` 就是用它调 `rm` 命令的。
- **惰性 require**：cm.js 顶部注释要求不能在文件顶层 require `node_modules` 里的任何东西，所有工具依赖都写在函数体内、调用时才加载（u1-l2 已解释原因）。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js) | `build()`（L118-123）与 `clean()`（L290-293）的实现，以及 install 结尾的首次构建调用 |
| [bin/packages.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js) | `Pkg` 构造函数的 `main` 探测规则（L46-59）和 `loadPackages()` 的 `buildPackages` 过滤（L62-67） |
| [tsconfig.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json) | `paths` 映射表（L13-49）：把 35 个包名静态指向各自源码入口 |
| [package.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json) | `prepare` 脚本钩子（L6）与 `workspaces` 配置（L22-24） |

注意：`cm build` 真正的编译/打包逻辑在 `@marijn/buildtool` 和 `@codemirror/buildhelper` 两个包里，**它们的源码不在本仓库**（要等 `cm install` 之后才出现在 `node_modules/` 中）。本讲聚焦本仓库如何驱动它们。

## 4. 核心概念与源码讲解

### 4.1 build()：从命令行到 dist 产物的主链路

#### 4.1.1 概念说明

本仓库的哲学是「不重复造轮子」：它自己不写编译器也不写打包器，`build()` 只是一个**编排者**（orchestrator），回答三个问题：

1. **构建什么**——`buildPackages.map(p => p.main)`，即 35 个可构建包的入口文件绝对路径列表（见 4.2）。
2. **交给谁构建**——`@marijn/buildtool` 的 `build()` 函数。
3. **用什么配置**——`@codemirror/buildhelper` 的 `src/options` 模块导出的 `options` 对象，被原样透传。

`build()` 本体只有 6 行，但它是整条流水线的咽喉：install 的最后一步是它，`npm run prepare` 钩子是它，`npm run dev` 背后的 watch 也建立在同一份入口清单上。

#### 4.1.2 核心流程

```text
cm build
 └─ start()：命令映射表查到 build（cm.js L20）
     └─ build()（cm.js L118）
         ├─ console.info("Building...")           # 开始提示
         ├─ t0 = Date.now()                        # 记录起始时间戳（毫秒）
         ├─ await buildtool.build(
         │     buildPackages.map(p => p.main),     # 35 个入口 .ts 的绝对路径
         │     buildhelper/src/options.options     # 惰性 require 的配置对象
         │  )                                      # 各包 dist/ 产物在此时生成
         └─ console.info(`Done in X.XXs`)          # 结束提示 + 耗时
```

耗时输出的数学很简单：设起始与结束时间戳为 \( t_0 \)、\( t_1 \)（毫秒），则打印值为

\[ \text{elapsed} = \frac{t_1 - t_0}{1000} \text{（秒）}，\text{保留两位小数（toFixed(2)）} \]

值得注意的三个细节：

- `build()` 是 `async` 函数，`await` 让出事件循环；`start()` 里 `new Promise(r => r(cmdFn.apply(null, args))).catch(e => error(e))` 的写法保证异步构建失败也会落入统一的 `error()` 出口。
- `build()` **不构建 demo**：`demo/demo.ts` 只在 `devserver()` 的 watch 列表里被额外纳入（L158 第二个数组参数）。
- 两个 require 都写在函数体内，遵守「顶部只用 Node 内置模块」的约束。

#### 4.1.3 源码精读

先看命令注册与帮助文本——`build` 和 `clean` 都是映射表里的普通条目：

[bin/cm.js:L17-L35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L17-L35) 中 `build`（L20）、`clean`（L25）注册进命令表；除 `install` 和 `--help` 外的所有命令（包括 build）都要先过 L15 的 `assertInstalled()` 守卫——所以**在没跑过 `cm install` 的裸检出里直接执行 `cm build`，会在碰任何构建逻辑之前就报 `module state is missing` 退出**（守卫实现见 [bin/cm.js:L72-L79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)）。帮助文本里对应的两行在 [bin/cm.js:L43-L44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L43-L44)：

```js
  cm build                Build the bundle files
  cm clean                Delete files created by the build
```

`build()` 本体：

```js
async function build() {
  console.info("Building...")
  let t0 = Date.now()
  await require("@marijn/buildtool").build(buildPackages.map(p => p.main),
                                           require("@codemirror/buildhelper/src/options").options)
  console.info(`Done in ${((Date.now() - t0) / 1000).toFixed(2)}s`)
}
```

这是 [bin/cm.js:L118-L123](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L118-L123)：打印开始提示、记录毫秒时间戳、把入口列表和配置交给 `@marijn/buildtool` 的 `build()`、最后打印耗时。注意 `buildPackages` 不是在这里现算的——它在进程启动时就由 [bin/cm.js:L11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L11) 的 `loadPackages()` 缓存为模块级变量，只有 `install()` 完成装配后会重新加载刷新一次（见下）。

`build()` 有两个触发入口。第一个是 install 的收尾：

```js
  console.log("Running npm install")
  run("npm", ["install", "--ignore-scripts"], root, {shell: process.platform == "win32", stdout: "inherit"})
  console.log("Building modules")
  ;({packages, packageNames, buildPackages} = loadPackages())
  build()
```

这是 [bin/cm.js:L98-L102](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L98-L102)。两个要点：

- `npm install` 带 `--ignore-scripts`，会跳过包括 `prepare` 在内的所有生命周期脚本，所以 install 必须在结尾**显式**调用 `build()`；
- 调用前先用 `loadPackages()` 重新探测——克隆完成后各包目录从无到有，`main` 才能被探测到（u1-l2 已讲过这一点，这里看到它服务的就是 build 的输入）。

第二个触发入口是 npm 的 `prepare` 钩子：[package.json:L3-L8](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L3-L8) 中 `"prepare": "node bin/cm.js build"`。当你（不带 `--ignore-scripts`）在仓库根执行普通 `npm install` 时，npm 会自动运行 prepare，从而间接触发同一条构建链。

最后对照一下 `devserver()` 对同一份清单的用法：

```js
  require("@marijn/buildtool").watch(buildPackages.map(p => p.main).filter(f => f),
                                      [join(root, "demo/demo.ts")], options)
```

这是 [bin/cm.js:L153-L160](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L153-L160) 中的关键行：同一份 `buildPackages.map(p => p.main)` 列表交给的是 `watch`（监听并增量重建）而非一次性 `build`，另外把 `demo/demo.ts` 追加进监听，并在 buildhelper 的 `options` 基础上叠加了 `sourceMap` 开关（L154-157 的对象展开）。注意这里的 `.filter(f => f)` 其实是**防御式冗余**——`buildPackages` 在 `loadPackages()` 里已经保证 `main` 非空（见 4.2），`build()` 不加 filter 与之等价。

还有一个耐人寻味的观察：`build()` 依赖的 `@marijn/buildtool` **并不在**根 [package.json:L11-L17](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L11-L17) 的 `devDependencies` 里（那里只有 `@codemirror/buildhelper`、`esmoduleserve`、`serve-static`、`getdocs-ts`、`builddocs`）。它能被 require 到，是因为 npm 安装时把某个包的依赖**提升**（hoisting）到了根 `node_modules/`——具体来自哪个包，可在安装后用 `npm ls @marijn/buildtool` 查证（待本地验证）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `cm build` 的输出结构与耗时统计，确认产物落在各包 `dist/`。
2. **操作步骤**：
   - 前置：已完成 u1-l2 的 `node bin/cm.js install`（否则会先被 `assertInstalled()` 拦下）。
   - 在仓库根执行 `node bin/cm.js build`。
   - 记录终端输出（预期两行：`Building...` 与 `Done in X.XXs`，X 取决于机器）。
   - 执行 `ls state/dist`（任选一个包，如 state）查看产物文件。
3. **需要观察的现象**：输出极简——只有开始与结束两行，中间编译过程不打印细节；耗时数字是 `((Date.now() - t0) / 1000).toFixed(2)` 的结果。
4. **预期结果**：`state/dist` 下出现构建产物文件（具体文件名由 buildtool 决定，不在本仓库源码内，待本地验证）。若在未 install 的裸检出上执行，则得到 `module state is missing. Did you forget to run 'cm install'?` 且退出码为 1——这本身就是一个验证 `assertInstalled` 守卫顺序的小实验。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `require("@marijn/buildtool")` 必须写在 `build()` 函数体内，而不能放到文件顶部？

**答案**：cm.js 顶部的注释（[bin/cm.js:L3-L4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L3-L4)）要求 install 脚本在 `node_modules` 存在之前也能运行，所以顶层只能 require Node 内置模块；所有第三方依赖一律在使用现场惰性加载。

**练习 2**：`cm install` 已经运行了 `npm install`，为什么还要在结尾手动调用 `build()`？`prepare` 钩子不是会自动构建吗？

**答案**：install 用的是 `npm install --ignore-scripts`（L99），该标志跳过包括 `prepare` 在内的生命周期脚本，钩子被抑制；所以 L102 必须显式调用 `build()`。普通的不带该标志的 `npm install` 才会通过 `prepare`（[package.json:L6](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L6)）自动触发构建。

**练习 3**：`cm build` 会构建 `demo/demo.ts` 吗？

**答案**：不会。`build()` 只传 `buildPackages` 的入口；`demo.ts` 是 `devserver()` 里 `watch(...)` 调用的第二个参数才纳入监听的（[bin/cm.js:L158](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L158)）。

### 4.2 构建的输入：Pkg.main 探测与 buildPackages 过滤

#### 4.2.1 概念说明

`build()` 拿到的入口列表从哪来？u2-l1 已经从「数据模型」角度精读过 `Pkg` 类，本讲换一个视角——把它看作流水线的**原料清单生成器**：

- `packages`：36 个包的全量清单（core 12 + nonCore 24），用于遍历仓库类的命令（status/commit/push/run）。
- `buildPackages`：其中 `main` 非空的子集（安装齐全时是 35 个），专供编译、测试类命令使用。
- `Pkg.main`：每个包入口 `.ts` 文件的**绝对路径**，由三条优先级规则在运行时探测得出。

「运行时探测」是这套设计最有意思的地方：本仓库不维护每个包的入口配置文件，而是直接看磁盘上 `src/` 目录里有什么文件来推断。代价是必须在所有包克隆完成后调用才有效。

#### 4.2.2 核心流程

`Pkg` 构造函数对每个包名执行以下判定：

```text
Pkg(name)
 ├─ dir = 仓库根/<name>
 ├─ main = null                          # 默认值
 └─ 若 (name == "legacy-modes" 或目录不存在) → 跳过，main 保持 null
     └─ files = readdir(dir/src) 中匹配 /^[^.]+\.ts$/ 的文件   # 顶层、无点的 .ts
         ├─ 规则 1：files 只有一个文件        → main = 该文件
         ├─ 规则 2：files 含 index.ts        → main = index.ts
         ├─ 规则 3：files 含 <剥掉 theme-/lang- 前缀的名字>.ts → main = 该文件
         └─ 全部落空                        → throw "Couldn't find a main script"
```

然后 `loadPackages()` 做过滤：

```text
loadPackages()
 ├─ packages    = all(36).map(n => new Pkg(n))
 ├─ packageNames = { 名字 → Pkg } 的查表对象
 └─ buildPackages = packages.filter(p => p.main)   # 只有 main 非空的包才可构建
```

#### 4.2.3 源码精读

Pkg 构造与探测规则在 [bin/packages.js:L46-L59](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L46-L59)：

```js
class Pkg {
  constructor(name) {
    this.name = name
    this.dir = join(__dirname, "..", name)
    this.main = null
    if (name != "legacy-modes" && fs.existsSync(this.dir)) {
      let files = fs.readdirSync(join(this.dir, "src")).filter(f => /^[^.]+\.ts$/.test(f))
      let main = files.length == 1 ? files[0] : files.includes("index.ts") ? "index.ts"
          : files.includes(name.replace(/^(theme-|lang-)/, "") + ".ts") ? ... : null
      if (!main) throw new Error("Couldn't find a main script for " + name)
      this.main = join(this.dir, "src", main)
    }
  }
}
```

这段代码做了三件事：把包目录定为仓库根下的同名目录；把 `main` 初始化为 `null`（legacy-modes 这个唯一的非 TS 包、以及一切未克隆的包都停留在此）；否则按「唯一 .ts 文件 > index.ts > 剥掉 `theme-`/`lang-` 前缀的同名文件」三条规则探测入口，全落空则直接抛错。

三个导出视图在 [bin/packages.js:L62-L67](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L62-L67)，其中 L66 的 `packages.filter(p => p.main)` 就是 `buildPackages` 的出生地——`build()`、`devserver()`、`test()` 消费的都是它。

现在做一次静态对照，验证「运行时探测」和 tsconfig 的「静态映射」指向同一批文件：

| 包名 | tsconfig `paths` 目标（静态） | `Pkg.main` 命中规则（运行时） |
| --- | --- | --- |
| `@codemirror/state` | `./state/src/index.ts`（[tsconfig.json:L14](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L14)） | 规则 2：index.ts |
| `@codemirror/commands` | `./commands/src/commands.ts`（[tsconfig.json:L16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L16)） | 规则 3：与包同名 |
| `codemirror` | `./codemirror/src/codemirror.ts`（[tsconfig.json:L25](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L25)） | 规则 3：与包同名 |
| `@codemirror/lang-javascript` | `./lang-javascript/src/index.ts`（[tsconfig.json:L26](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L26)） | 规则 2：index.ts |
| `@codemirror/lang-php` | `./lang-php/src/php.ts`（[tsconfig.json:L39](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L39)） | 规则 3：剥 `lang-` 前缀 |
| `@codemirror/theme-one-dark` | `./theme-one-dark/src/one-dark.ts`（[tsconfig.json:L48](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L48)） | 规则 3：剥 `theme-` 前缀 |

数一数可以发现：`paths` 映射表（[tsconfig.json:L13-L49](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L13-L49)）恰好 35 条，与安装齐全时的 `buildPackages` 数量一致——36 个包减去没有 TS 入口的 legacy-modes。**tsconfig 的静态映射正是运行时 `main` 探测结果的镜像**，两套机制各自维护、必须人工保持同步（这是本设计的一个维护成本，u3-l4 会再讨论）。

#### 4.2.4 代码实践

1. **实践目标**：用脚本把运行时探测结果打印出来，与 tsconfig 的静态映射逐条比对。
2. **操作步骤**（在仓库根执行，前置已完成 `cm install`）：

   ```js
   // 示例代码：打印可构建包的入口清单（node print-mains.js，文件可放在任意临时位置）
   const {loadPackages} = require("./bin/packages")
   for (let p of loadPackages().buildPackages)
     console.log(p.name.padEnd(24), "→", p.main)
   ```

   然后打开 `tsconfig.json`，任选 3 条 `paths`，核对脚本输出中同名包的 `main` 路径是否指向同一文件。
3. **需要观察的现象**：每行输出形如 `state → /…/dev/state/src/index.ts`；`legacy-modes` **不会出现**（它不在 `buildPackages` 里）。
4. **预期结果**：35 行输出；抽样的每一条都与 `paths` 目标一致。若某包 `src/` 被改得三条规则都探测不到入口，`loadPackages()` 会直接抛 `Couldn't find a main script for <name>`。

#### 4.2.5 小练习与答案

**练习 1**：`packages` 和 `buildPackages` 分别是多大？差在谁身上？

**答案**：`packages` 是 36（[bin/packages.js:L3-L16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L16) 的 core 12 + [L17-L42](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L17-L42) 的 nonCore 24，由 [L44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L44) 拼出）；`buildPackages` 是 35。差的那个是 `legacy-modes`——构造函数里显式排除（L51），它的 `main` 恒为 `null`，进不了 `filter(p => p.main)`。

**练习 2**：某包 `src/` 下同时有 `a.ts`、`b.ts` 和 `index.ts`，`main` 会探测成什么？如果只有 `a.ts` 和 `b.ts`、没有 `index.ts`，但包名是 `lang-foo` 且存在 `foo.ts` 呢？

**答案**：第一种命中规则 2，`main = index.ts`（规则 1 只在「恰好一个文件」时生效）；第二种依次落空规则 1、2 后命中规则 3——剥掉 `lang-` 前缀得到 `foo`，`foo.ts` 存在，故 `main = foo.ts`。

**练习 3**：在没跑 `cm install` 的裸检出里，`loadPackages()` 的 `buildPackages` 是多少个？这会导致 `build()` 传给 buildtool 一个什么列表？

**答案**：0 个——所有包目录都不存在，全部 `main` 为 `null`，`filter(p => p.main)` 结果为空数组；`build()` 将传入空入口列表。（当然实际走不到这一步，`assertInstalled()` 会先拦截。）

### 4.3 clean()：删除构建产物，与 install 清理的分工

#### 4.3.1 概念说明

`clean()` 是 `build()` 的逆操作：删除构建生成的文件。它同样不自己动手删文件，而是借助 `run()` 调用系统的 `rm` 命令。理解它的关键在于**分清两处清理各自的管辖范围**：

- `cm clean`：用户主动触发的「构建产物清理」，只删 `dist/` 目录。
- `cm install` 更新已有包时的清理：目的是让包对齐远端最新状态，除了 `dist` 还要清掉编译生成的 `test/*.js` 残留。

#### 4.3.2 核心流程

```text
cm clean
 └─ clean()（cm.js L290）
     └─ for pkg of buildPackages:          # 注意：只遍历 35 个可构建包
          run("rm", ["-rf", "dist"], pkg.dir)   # 在各包目录里删除整个 dist/
```

两处清理的对比：

| 维度 | `clean()` | install 更新分支的清理 |
| --- | --- | --- |
| 位置 | [bin/cm.js:L290-L293](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L290-L293) | [bin/cm.js:L88-L90](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L88-L90) |
| 命令 | `rm -rf dist` | `rm -f dist/* test/*.js` |
| 删除对象 | `dist` **整个目录** | `dist` 内的文件（目录保留）+ 编译产物 `test/*.js` |
| 遍历集合 | `buildPackages`（35） | `packages`（36，含 legacy-modes） |
| 触发时机 | 手动执行 `cm clean` | 每次 `cm install` 时对已存在的包执行 |

#### 4.3.3 源码精读

`clean()` 全文只有 3 行，见 [bin/cm.js:L290-L293](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L290-L293)：

```js
function clean() {
  for (let pkg of buildPackages)
    run("rm", ["-rf", "dist"], pkg.dir)
}
```

它遍历 `buildPackages`（不是 `packages`——legacy-modes 不参与本流水线，自然没有 `dist` 要清），用 `run()`（[bin/cm.js:L64-L66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L64-L66)）在每个包目录里执行 `rm -rf dist`：`-r` 递归、`-f` 忽略不存在等错误，即「目录在就删、不在也别报错」。

对照 install 更新已有包时的清理，[bin/cm.js:L85-L95](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L85-L95)：

```js
    if (fs.existsSync(pkg.dir)) {
      console.log(`${pkg.name} exists, updating to origin`)
      run("git", ["fetch", "origin", "main"], pkg.dir, {stdout: "inherit"})
      run("git", ["reset", "--hard", "FETCH_HEAD"], pkg.dir, {stdout: "inherit"})
      run("rm", ["-f", "dist/*", "test/*.js"], pkg.dir)
    }
```

`reset --hard` 只回滚 git 跟踪的文件，而 `dist/` 与编译后的 `test/*.js` 通常不被 git 跟踪——若不手动删除，旧版本留下的产物会和新源码混在一起。所以 install 的清理是「对齐远端」的一部分；而 `clean()` 是纯粹的反构建操作，不碰 `test/*.js`。帮助文本对它的描述就一句「Delete files created by the build」（[bin/cm.js:L44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L44)）。

顺带一个可移植性观察：两处都依赖 UNIX 的 `rm` 命令，在没有 `rm` 的 Windows 环境会失败——这是本工具链公认的跨平台短板之一，u3-l4 会集中讨论。

#### 4.3.4 代码实践

1. **实践目标**：验证 `cm clean` 删除且仅删除各包的 `dist` 目录。
2. **操作步骤**（前置：已 install 且已 build）：
   - 执行 `ls state/dist`，确认产物存在。
   - 执行 `node bin/cm.js clean`。
   - 再次执行 `ls state/dist`，观察报错；再执行 `ls legacy-modes`（或 `ls state/src`），确认源码纹丝不动。
3. **需要观察的现象**：`state/dist` 不存在（`No such file or directory`）；`src/` 目录及其文件完好。
4. **预期结果**：35 个可构建包的 `dist` 目录全部消失，源码与 `test/*.ts` 不受影响。若想恢复，直接再跑一次 `node bin/cm.js build`（这正是综合实践要做的事）。

#### 4.3.5 小练习与答案

**练习 1**：`cm clean` 之后，哪些消费方会立刻「找不到东西」？

**答案**：一切按包 `package.json` 的 `main` 字段解析到 `dist/` 产物的消费方——例如通过 npm workspaces 链接在 Node 侧 `require("@codemirror/...")` 的代码。TS 源码级解析（tsconfig `paths`、dev server 的源码即时编译）不直接依赖 `dist`，受影响较小（具体边界待本地验证）。

**练习 2**：为什么 install 的清理要额外删 `test/*.js`，而 `clean()` 不用？

**答案**：install 的场景是「把包重置到远端最新提交」，此时旧版源码编译出的 `test/*.js` 残留会和新版 `.ts` 混杂；`clean()` 的语义只是撤销本流水线的构建产物 `dist`，编译测试文件不在其管辖范围。

**练习 3**：如果把 `clean()` 里的 `buildPackages` 换成 `packages`，会发生什么？

**答案**：多遍历一个 `legacy-modes` 包，在它目录里执行 `rm -rf dist`。由于 `-f` 的存在，即使它没有 `dist` 也不会报错——行为上无害，但语义上多余，所以用 `buildPackages` 更精确。

### 4.4 双轨解析：npm workspaces 与 tsconfig paths

#### 4.4.1 概念说明

36 个包的源码分散在 36 个仓库里，却要互相 `import`（比如 `lang-javascript` 依赖 `language` 和 `view`）。本仓库用**两套互相独立的机制**让这种引用在开发期成立：

1. **npm workspaces（Node/npm 侧）**：`"workspaces": ["*"]` 让 `npm install` 把根目录下所有克隆出来的包目录当作工作区，用符号链接把它们挂进根 `node_modules/`。运行时和 npm 的依赖解析由此走通——但链接指向的包入口是 `package.json` 的 `main`，也就是 `dist/` 产物，**构建之后才有效**。
2. **tsconfig `paths`（TypeScript 侧）**：把每个 `@codemirror/*` 包名直接映射到兄弟仓库的 **`.ts` 源文件**。编译器做类型检查、IDE 做跳转补全时，完全不需要 `dist` 存在——这正是「首次构建前整个仓库也能获得完整类型支持」的原因。

两套机制一个面向运行时、一个面向编译期，互不通信，但指向同一批包，必须人工保持一致（新增包时两处都要登记，u3-l4 实战会演练）。

#### 4.4.2 核心流程

同一个 `import "@codemirror/state"`，两条解析路径：

```text
TypeScript 编译器 / IDE                    Node 运行时 / npm
  moduleResolution: "node"                   workspaces: ["*"] 建立的符号链接
  └─ 先查 paths 映射                         └─ node_modules/@codemirror/state
      "@codemirror/state"                        └─ 读该包 package.json 的 main
      → ./state/src/index.ts                        → dist/ 下的产物（构建后存在）
      （源码，无需构建即可类型检查）
```

#### 4.4.3 源码精读

workspaces 配置在 [package.json:L22-L24](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L22-L24)：

```json
  "workspaces": [
    "*"
  ]
```

通配符 `*` 匹配根目录下的每个子目录——这就是为什么 `cm install` 把 36 个包克隆到仓库根之后，它们会被 npm 自动「收编」。

`paths` 映射表在 [tsconfig.json:L13-L49](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L13-L49)，每条形如：

```json
      "@codemirror/state": ["./state/src/index.ts"],
      "@codemirror/lang-php": ["./lang-php/src/php.ts"],
```

注意两个特征：目标是**相对仓库根的源码 `.ts` 文件**（不是 `dist` 产物）；35 条与 `buildPackages` 一一对应（见 4.2.3 的对照表）。配合 [tsconfig.json:L12](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L12) 的 `"moduleResolution": "node"`（paths 是在 node 风格解析之上的覆盖），以及 [tsconfig.json:L51](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L51) 的 `include: ["*/src/*.ts", "*/test/*.ts", "demo/demo.ts"]`——`include` 同样用通配符罩住所有包的 `src` 与 `test` 顶层文件和 demo 入口。在一个未执行 `cm install` 的裸检出里，这些通配目标与 `paths` 目标全都是不存在的路径：**整个 tsconfig 的有效性以多仓库装配完成为前提**。

#### 4.4.4 代码实践

1. **实践目标**：亲手把两条 `paths` 记录追到真实源码文件，建立「静态映射 ↔ 磁盘文件」的肌肉记忆。
2. **操作步骤**（前置：已完成 `cm install`）：
   - 打开 `tsconfig.json`，选定 `@codemirror/lang-php`（L39）和 `@codemirror/theme-one-dark`（L48）两条。
   - 执行 `ls lang-php/src/php.ts theme-one-dark/src/one-dark.ts`，确认文件存在。
   - 用编辑器打开这两个文件，确认它们是该包的导出入口（`export` 语句所在）。
   - 思考题自测：把 `@codemirror/view` 那条 `paths` 暂时注释掉，IDE 里 `import "@codemirror/view"` 的跳转会落到哪里？
3. **需要观察的现象**：两个 `.ts` 文件真实存在且包含导出；注释掉 `paths` 条目后，TS 解析回退到 node_modules 路径（经 workspaces 链接命中包的 `main`，即 `dist` 产物），未构建时类型解析会退化或失败。
4. **预期结果**：`paths` 指向的源码文件可直接打开；回退实验中跳转目标从源码变为 `node_modules/@codemirror/view` 链接路径。回退后的具体表现（报错还是解析到旧产物）取决于本地是否留有 `dist`，待本地验证；做完实验记得还原 `tsconfig.json`。

#### 4.4.5 小练习与答案

**练习 1**：workspaces 和 `paths` 分别服务于哪个解析器？各自的解析终点是什么文件？

**答案**：workspaces 服务于 npm/Node——安装期建立符号链接，解析终点是各包 `package.json` 的 `main` 指向的 `dist/` 产物；`paths` 服务于 TypeScript 编译器与 IDE——解析终点是 `paths` 直接给出的 `.ts` 源文件。

**练习 2**：为什么 `paths` 的目标必须是源码而不是 `dist` 产物？

**答案**：两个原因：其一，`dist` 在首次构建之前根本不存在，指向它会让全新检出的类型检查全面失败；其二，开发时要改的就是源码，类型检查与跳转必须落在源码上才能反映未构建的最新改动。

**练习 3**：`paths` 映射有多少条？为什么不是 36 条？

**答案**：35 条（L14-L48）。36 个包里 `legacy-modes` 是纯 JavaScript 包、没有 TS 入口，被运行时 `Pkg` 探测（L51）和静态 `paths` 映射同时排除——两套机制在这件事上保持了一致。

## 5. 综合实践

把本讲内容串成一个完整的「构建—清理—重建—溯源」循环（前置：u1-l2 的 `cm install` 已完成）：

1. **构建并计时**：执行 `node bin/cm.js build`，抄下 `Done in X.XXs` 的耗时；再执行一次，对比第二次（有缓存时的）耗时差异。
2. **盘点产物**：任选三个包（建议 state、lang-php、theme-one-dark），`ls <包名>/dist` 记录产物文件清单。
3. **清理验证**：执行 `node bin/cm.js clean`，确认三个包的 `dist` 目录全部消失、`src/` 完好。
4. **重建恢复**：再执行 `node bin/cm.js build`，确认 `dist` 恢复原样——至此你亲手走完了 `build()` 与 `clean()` 互逆的闭环。
5. **静态映射溯源**：在 `tsconfig.json` 里挑两条 `paths`（如 L39 的 lang-php、L48 的 theme-one-dark），用 `ls` 和编辑器追到源码入口文件，并对照 4.2 的三条探测规则说出每条分别命中哪条规则。
6. **选做（深挖工具链）**：执行 `npm ls @marijn/buildtool` 查明这个未在根 `devDependencies` 声明的包是被谁依赖进来的；再打开 `node_modules/@codemirror/buildhelper/src/options.ts`（构建后即可读），看看 `options` 对象里到底装了什么、是否读取了 `tsconfig.json`——这是把「buildhelper 的配置从哪来」从待确认变成确认的一步。

预期结果：全部步骤可重复执行；第 5 步的两条映射都能落到真实存在、含 `export` 的 `.ts` 文件。第 1、6 步的具体数值与文件内容因环境而异，待本地验证。

## 6. 本讲小结

- `build()`（cm.js L118-123）是纯编排层：把 `buildPackages.map(p => p.main)` 的 35 个入口连同 buildhelper 的 `options` 交给 `@marijn/buildtool`，自身只负责提示与计时；编译细节在被依赖包里，不在本仓库。
- 构建有两个触发入口：install 结尾的显式调用（因 `--ignore-scripts` 抑制了 prepare 钩子），以及普通 `npm install` 经 `prepare` 脚本的自动触发。
- `buildPackages` 由 `Pkg.main` 运行时探测（唯一 .ts > index.ts > 剥前缀同名）加 `filter(p => p.main)` 过滤得出；tsconfig 的 35 条 `paths` 是这份结果的静态镜像。
- `clean()` 只对各 buildPackages 执行 `rm -rf dist`；install 的更新分支额外清 `dist/*` 与 `test/*.js` 以对齐远端——两处清理管辖范围不同。
- npm workspaces 与 tsconfig `paths` 是面向运行时与编译期的两套独立解析机制，前者终于 `dist` 产物、后者直达源码，新增包时两处都要同步登记。

## 7. 下一步学习建议

下一讲 u2-l3《dev server 内部：esmoduleserve 与测试路由》将沿着本讲的 `watch` 分支继续深入：`devserver()` 如何用同一份入口清单启动增量监听、`startServer()` 的三层路由如何把 ES 模块请求交给 esmoduleserve 即时编译。建议在此之前，把本讲综合实践的第 6 步做掉——读过 buildhelper 的 `options` 源码后，你会对 dev server「源码即服务」的机制有更完整的预备。此外可以带着一个问题进入下一讲：`cm build` 生成的 `dist` 产物在 dev server 的请求路径中究竟扮演什么角色？
