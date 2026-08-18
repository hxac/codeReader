# 二次开发实战：扩展工具链与架构取舍

## 1. 本讲目标

这是学习手册的收官之讲。前面十讲我们一直在「读」这套工具链，本讲换成「写」：亲手扩展它。学完后你应该能够：

1. 独立完成两类最典型的二次开发：
   - 向 `bin/packages.js` 注册一个新包，并让它被 install / build / test / status 全部命令看见；
   - 向 `bin/cm.js` 新增一个子命令（`cm doctor`），并正确接入命令映射表、`help()` 文本与 `assertInstalled()` 守卫。
2. 说清楚注册新包时 `tsconfig.json` 的 `paths` 与根 `package.json` 的 `workspaces` 这两套机制各自负责什么、哪部分是通配符自动生效的、哪部分必须手工同步。
3. 评估「多仓库 + 中央脚本」这种架构相对单体 monorepo 在依赖联动、测试与发布上的代价和收益。
4. 识别 `cm.js` 中依赖 `rm`、`grep` 等外部 UNIX 工具的位置，理解它们带来的跨平台问题。

## 2. 前置知识

本讲综合前面前置讲义的结论，动手前请确认以下概念你已经掌握（不清楚的可回看对应讲义）：

- **包注册表三视图**（u2-l1）：`bin/packages.js` 用 `core` + `nonCore` 两组包名拼出 36 项的 `all`，`loadPackages()` 返回 `packages`（全量）、`packageNames`（按名索引）、`buildPackages`（`main` 非空的 35 个）三个视图，分别服务不同命令。
- **入口探测规则**（u2-l1、u2-l2）：`Pkg` 构造函数在包目录的 `src/` 下按「唯一 `.ts` 文件 > `index.ts` > 剥掉 `theme-`/`lang-` 前缀的同名文件」探测入口 `main`；目录不存在或包是 `legacy-modes` 时 `main` 保持 `null`。
- **命令分发骨架**（u1-l3）：`start()` 用一张「命令名 → 函数」的对象映射表分发子命令，参数下限校验复用函数的 `length` 属性（默认值与 rest 参数不计入），除 `install` 与 `--help` 外都先过 `assertInstalled()` 守卫。
- **双轨解析**（u2-l2）：npm `workspaces` 面向运行时、解析到各包 `dist/` 产物；tsconfig `paths` 面向编译期、直达 `.ts` 源码，两套机制各自独立、须人工保持一致。
- **发布与文档流水线**（u3-l2、u3-l3）：`release` 按包逐一执行，`bumpVersion` 分 0.x 与正式版两套规则；`build-readme` 仅对 `nonCore` 包开放，用 `src/README.md` 模板生成包根 README。

另外补充两个本讲要用到的底层概念：

- **扩展点（extension point）**：指程序里专门留给后来者挂钩的位置。这套仓库的扩展点不是接口或插件 API，而是三个「数据 + 约定」：一张包名清单、一张命令映射表、一份 `paths` 映射。理解这一点，二次开发就变成了「在正确的清单里加一行」。
- **glob 展开发生在 shell**：Unix 下 `rm dist/*` 里的 `*` 是由 shell 展开成文件名列表后再传给 `rm` 的，`rm` 自己不认识通配符。而 `cm.js` 的 `run()` 默认 `shell: false`（直接 `execvp`），不经过 shell——记住这一点，第 4.4 节会看到它带来的一个微妙问题。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| `bin/packages.js` | 扩展点一：包清单（`core`/`nonCore`）与 `Pkg` 构造、`loadPackages()` |
| `bin/cm.js` | 扩展点二：命令映射表、`help()`、`assertInstalled()`；跨平台问题的现场 |
| `tsconfig.json` | 扩展点三：`paths` 手工映射与 `include` 通配收录 |
| `package.json` | npm `workspaces: ["*"]` 通配与四个 cm 别名脚本 |
| `CONTRIBUTING.md` | 二次开发产物的整理标准：代码风格与提交规范 |
| `bin/build-readme.js` | 消费 `core`/`nonCore` 区分的外围示例（第 4.1 节引用） |

## 4. 核心概念与源码讲解

### 4.1 扩展点一：packages.js 的清单与 Pkg 构造

#### 4.1.1 概念说明

`bin/packages.js` 是全仓库唯一的包数据源：**一个包只要不在这份清单里，就对所有 cm 命令不存在**——install 不会克隆它、build 不会编译它、test 不会收集它的测试、status 不会遍历它。所以「新增一个包」的第一步永远是往清单里加名字。

清单分成语义不同的两组：

- `core`：12 个构成编辑器本体的核心包。它们还有一个下游消费者——文档流水线会把指向兄弟核心包的类型链接改写为 `codemirror.net` 文档锚点。
- `nonCore`：24 个语言、主题等外围包。`cm build-readme` 命令只对这组开放。

清单里的每个名字都会被 `Pkg` 构造函数实例化成带目录与入口信息的对象，再由 `loadPackages()` 包装成三个视图。这条链路在第 2 节回顾过，这里聚焦「注册新包时它如何决定成败」。

#### 4.1.2 核心流程

注册一个新包后，数据流是这样的：

```text
名字进入 core / nonCore
        │
        ▼
Pkg(name) 实例化
  ├─ dir  = 仓库根/<name>            （固定拼接，不查磁盘）
  └─ main = 三规则探测 src/ 下的入口   （查磁盘！）
        │  目录不存在 → main = null（不报错）
        │  目录存在但探测落空 → 抛错 "Couldn't find a main script"
        ▼
loadPackages() 产出三个视图
  ├─ packages      → install 克隆、status/commit/push/run/grep 遍历、assertInstalled 检查
  ├─ packageNames  → release/doctor 这类「按名取包」的命令
  └─ buildPackages → build 编译、devserver watch、test 收集
```

注意两个坑：

1. **先注册后建目录，会让全部命令瘫痪**。`assertInstalled()` 检查的是全量 `packages`，不是 `buildPackages`——注册了名字但目录还没创建，`cm build`、`cm status` 等都会在守卫处退出；而 `cm install` 又会去克隆 `https://code.haverbeke.berlin/codemirror/<名字>.git` 这个不存在的远端仓库而失败。正确顺序是：**先创建包目录与 `src/` 入口，再（或同批）修改清单**。
2. **目录存在但入口探测落空会直接抛错**，且发生在 `require("./packages")` 的模块加载期，任何 cm 命令都跑不起来。实验包的 `src/` 里放一个 `index.ts` 是最省心的选择。

#### 4.1.3 源码精读

先看两组清单与 `all` 的拼接——[bin/packages.js:3-16](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L16) 定义 `core` 数组（12 个核心包名），[bin/packages.js:17-42](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L17-L42) 定义 `nonCore` 数组（24 个外围包名，含唯一的非 TS 包 `legacy-modes`），[bin/packages.js:44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L44) 把两者拼接为 `all`。新包的名字就是往这两个数组之一追加一个字符串。

再看 `Pkg` 构造——[bin/packages.js:46-59](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L46-L59) 中：

- 第 49 行把 `dir` 固定为「仓库根下与包同名的目录」，这是与 `install()` 克隆路径的隐式约定；
- 第 51 行的 `name != "legacy-modes" && fs.existsSync(this.dir)` 是两条「免检」通道：`legacy-modes` 无 TS 入口、目录未克隆的包，`main` 保持 `null` 而不报错；
- 第 52 行只收录 `src/` 下不含额外点号的 `.ts` 文件；
- 第 53-54 行是三条探测规则：唯一文件直接用，否则找 `index.ts`，否则找剥掉 `theme-`/`lang-` 前缀后与包同名的文件；
- 第 55 行在三条规则全落空时抛错——这就是上面第 2 个坑的来源。

最后，[bin/packages.js:62-67](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L62-L67) 的 `loadPackages()` 把 36 个 `Pkg` 实例装进三个视图，其中 `buildPackages` 以 `p.main` 真值过滤——新包只要入口探测成功，就自动进入构建、watch 与测试收集的范围，**无需再改任何构建配置**。

顺带看一个 `core`/`nonCore` 区分的下游消费者：[bin/build-readme.js:11-12](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L11-L12) 用 `core.find(...)` 把指向兄弟核心包的类型链接改写为 `codemirror.net` 文档地址；而入口守卫在 [bin/cm.js:347](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L347)——`if (!nonCore.includes(name)) help(1)`，这就是「build-readme 仅对 nonCore 开放」的实现。实验包加入哪一组，直接影响它能否享受（或被排除出）这条文档流水线。

#### 4.1.4 代码实践：观察清单与探测规则

1. **实践目标**：在不动任何源码的前提下，看清 36 个包的 `main` 探测结果，为下一步注册实验包建立基线。
2. **操作步骤**：
   - 在仓库根目录写一个临时脚本 `probe.js`（**示例代码**，验完即删）：

     ```js
     // probe.js（示例代码）
     const {loadPackages} = require("./bin/packages")
     for (let p of loadPackages().packages)
       console.log(p.name.padEnd(18), p.main ? "buildable" : "main = null")
     ```

   - 运行 `node probe.js`。
3. **需要观察的现象**：输出 36 行；已克隆的环境里只有 `legacy-modes` 一行是 `main = null`；尚未运行 `cm install` 的环境里全部是 `main = null`（因为目录都不存在，走的是第 51 行的免检通道）。
4. **预期结果**：`buildable` 的行数应该等于 `buildPackages.length`（已克隆环境为 35）。可再运行 `node bin/cm.js packages` 对照包名清单——它由 [bin/cm.js:106-108](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L106-L108) 直接打印 `packages.map(p => p.name)`。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `legacy-modes` 没有入口文件却不触发第 55 行的抛错？
   **答案**：第 51 行的条件 `name != "legacy-modes"` 让它直接跳过整个探测块，`main` 保持 `null`，随后被 `buildPackages` 的真值过滤排除。它是全清单唯一的非 TS 包。
2. **练习**：如果把名字 `my-lang` 加入 `nonCore` 但不创建目录，`cm build` 会发生什么？`cm install` 呢？
   **答案**：`cm build` 在 `assertInstalled()`（遍历全量 `packages`）处退出，报 `module my-lang is missing`；`cm install` 走克隆分支，按 [bin/cm.js:93](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L93) 的 URL 拼接规则去克隆不存在的 `my-lang.git` 而失败。
3. **练习**：包名 `codemirror` 对应的远端仓库叫什么？这由哪行代码决定？
   **答案**：`basic-setup`。由 [bin/cm.js:93](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L93) 的三元表达式 `pkg.name == "codemirror" ? "basic-setup" : pkg.name` 决定——注册新包名时，远端仓库名必须与包名完全一致（除非也加特例）。

### 4.2 双轨联动：tsconfig paths 与 npm workspaces

#### 4.2.1 概念说明

包注册进清单后，第二件要理解的事是：**「包目录存在」如何变成「别的代码能引用它」**。这套仓库里有两条互相独立的解析轨道，上一讲（u2-l2）已经分辨过它们的分工，本讲从「新包接入」的视角再走一遍：

- **npm workspaces 轨道（运行时）**：根 `package.json` 的 `workspaces: ["*"]` 用通配符把仓库根下**任何**含 `package.json` 的子目录收编为工作区，npm 会在 `node_modules` 里为它们创建指向包目录的符号链接。运行时代码 `import ... from "@codemirror/lang-tiny"` 解析到该包 `package.json` 的 `main` 字段，即 `dist/` 产物。
- **tsconfig paths 轨道（编译期）**：`tsconfig.json` 的 `paths` 把 `@codemirror/*` 裸名手工映射到各包 `src/` 下的 `.ts` 源码，供 TypeScript 编译与类型检查直达源码。

关键洞察是**哪些自动、哪些手工**：

| 机制 | 新包接入方式 |
| --- | --- |
| `workspaces: ["*"]` | **自动**——目录里有 `package.json` 即被纳入 |
| tsconfig `include: ["*/src/*.ts", ...]` | **自动**——通配收录新包的源码参与编译 |
| tsconfig `paths` | **手工**——要让兄弟包按裸名 `import` 新包，必须自己加映射 |

#### 4.2.2 核心流程

新包目录 `lang-tiny/` 出现后的解析链路：

```text
lang-tiny/package.json 存在
        │
        ├─ npm workspaces("*") 自动收编 → node_modules/@codemirror/lang-tiny → lang-tiny/
        │       运行时 import 解析到 package.json 的 main → dist/index.js（须先 cm build）
        │
        ├─ tsconfig include("*/src/*.ts") 自动收录 → lang-tiny/src/*.ts 参与编译检查
        │
        └─ tsconfig paths 需手工补一条：
                "@codemirror/lang-tiny": ["./lang-tiny/src/index.ts"]
                （当且仅当其他包或 demo 按裸名引用它时才必需）
```

注意 `paths` 的映射目标应当与 `Pkg.main` 的探测结果一致——`paths` 的 35 条映射正是 `buildPackages` 35 个入口的静态镜像（`legacy-modes` 无 TS 入口，故不在其中）。两套机制没有任何代码互相校验，**一致性完全靠人工维护**，这是这套架构最脆的一根线。

#### 4.2.3 源码精读

- [package.json:22-24](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L22-L24)：`"workspaces": ["*"]`——通配一切子目录，新包无需登记。另见 [package.json:3-8](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L3-L8)，四个脚本 `test`、`test-node`、`prepare`、`dev` 全是 cm 子命令的别名，新命令不需要在此登记，除非你也想给它起 npm 别名。
- [tsconfig.json:13-49](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L13-L49)：35 条 `paths` 手工映射。特别看 [tsconfig.json:43](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L43)——`"@codemirror/lang-liquid"` 映射到 `./lang-vue/src/liquid.ts`，一条**跨仓库**的映射：`liquid` 的源码物理上住在 `lang-vue` 仓库里。这恰好证明 `paths` 是一张自由手工维护的表，既不要求与目录名对应，也没有工具帮你生成。
- [tsconfig.json:51](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L51)：`include: ["*/src/*.ts", "*/test/*.ts", "demo/demo.ts"]`——通配收录所有包的源码与测试，新包的文件自动纳入。
- [tsconfig.json:4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L4)：`"types": ["mocha"]` 注入 mocha 全局类型——新包测试文件免 `import` 直接使用 `describe`/`it` 的前提（u2-l4 讲过）。

#### 4.2.4 代码实践：给实验包补 paths（本节只做「 dry-run」推演）

1. **实践目标**：验证「哪些自动、哪些手工」的判断，先不真正改文件，学会预测。
2. **操作步骤**：
   - 假设已创建 `lang-tiny/`（含 `package.json` 与 `src/index.ts`，综合实践会完整做一遍）；
   - 推演三个问题：不加 `paths` 时 `cm build` 能否编译它？`demo/demo.ts` 里 `import {...} from "@codemirror/lang-tiny"` 能否通过类型检查？`node_modules/@codemirror/lang-tiny` 是否存在？
3. **需要观察的现象与预期结果**：
   - `cm build`：**能**。`build()` 直接把 `buildPackages.map(p => p.main)` 的**文件路径**交给 buildtool（[bin/cm.js:121](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L121)），不走裸名解析；
   - 裸名 `import` 的类型解析：**不能**，TypeScript 找不到 `@codemirror/lang-tiny` 的类型入口（除非补 `paths`）；
   - `node_modules` 链接：取决于是否重新跑过 `npm install`——`workspaces` 的符号链接在 install 时建立（**待本地验证**：也可直接 `ls -l node_modules/@codemirror/` 观察现有包的链接形态）。
4. **预期结论**：三条问题的答案恰好对应「build 自动、类型手工、链接半自动」。

#### 4.2.5 小练习与答案

1. **练习**：为什么 `paths` 里没有 `legacy-modes` 的条目？
   **答案**：`paths` 是 `buildPackages` 入口的静态镜像，`legacy-modes` 的 `main` 为 `null`、不参与 TS 构建，自然没有可映射的 `.ts` 入口；它的源码在 `mode/` 目录下（`grep` 命令对它有专门分支，见 [bin/cm.js:320-321](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L320-L321)）。
2. **练习**：`workspaces: ["*"]` 与 `include: ["*/src/*.ts"]` 都是通配，为什么 `paths` 不能也通配？
   **答案**：`workspaces`/`include` 只需圈定「范围」，npm 与 tsc 会自己找 `package.json`/`.ts` 文件；而 `paths` 的值是「裸名 → 具体入口文件」的映射，入口文件名由 `Pkg` 的三规则在**运行时**探测得出，TS 配置层面没有等价的探测逻辑可写，只能逐条手写。
3. **练习**：`lang-liquid` 的映射指向 `lang-vue` 仓库内的文件，这说明 `paths` 与包目录名之间有什么关系？
   **答案**：没有任何强制关系。`paths` 是纯手工表，键是 npm 裸名、值是任意相对路径；正因为它完全自由，才更依赖人工与 `Pkg.main` 探测结果保持一致。

### 4.3 扩展点二：cm.js 的命令映射表与 assertInstalled 守卫

#### 4.3.1 概念说明

第二个扩展点是 `bin/cm.js` 的命令映射表。给 cm 加一条子命令的完整清单只有三项：

1. **实现函数**：一个普通函数声明（靠函数声明提升，可以写在文件后部）；
2. **映射表表项**：在 `start()` 的 `cmdFn` 对象里加 `名字: 函数`；
3. **帮助文本**：在 `help()` 的 usage 里加一行（不做也能跑，但 `cm --help` 看不见——现有命令 `unreleased` 就是「有实现无文档」的先例，映射表才是权威清单）。

守卫是**自动接入**的：映射表里除 `install` 与 `--help` 外的任何命令，执行前都要过 `assertInstalled()`。这意味着新命令天然继承两条约束——它假设用户已运行过 `cm install`，且它遍历的 `packages` 一定是「全部目录都存在」的世界。理解这一点能解释一个看似奇怪的现象（见 4.3.2 末尾）。

#### 4.3.2 核心流程

一次 `cm doctor lang-tiny` 的执行路径：

```text
node bin/cm.js doctor lang-tiny
  │
  ├─ start() 取 argv[2] = "doctor"，非 install/--help → assertInstalled()
  │    （任一包目录缺失 → 报错退出）
  ├─ args = ["lang-tiny"]
  ├─ cmdFn 表查找 → doctor 函数；cmdFn.length(=1) ≤ args.length(=1) → 校验通过
  ├─ new Promise(r => r(doctor("lang-tiny"))).catch(e => error(e))
  │    （同步 throw 与异步失败统一进 error()：stderr + 退出码 1）
  └─ doctor(name) 执行：重新 loadPackages() → packageNames[name] → 体检 → 打印
```

一个值得深思的现象：**`doctor` 永远诊断不出「未克隆」的包**。因为守卫遍历的是全量 `packages`，只要有一个包目录缺失，进程在进入 `doctor` 之前就退出了——不过守卫的报错信息 `module X is missing. Did you forget to run 'cm install'?` 本身就是答案的一半。`doctor` 真正有价值的检查是守卫覆盖不到的部分：**入口是否探测成功、构建产物是否存在**。

#### 4.3.3 源码精读

- [bin/cm.js:17-33](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L17-L33)：`cmdFn` 命令映射表——新命令的注册点就是往这个对象字面量里加一项。
- [bin/cm.js:34](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L34)：`if (!cmdFn || cmdFn.length > args.length) help(1)`——用函数 `length` 属性做参数个数**下限**校验。`doctor(name)` 的 `length` 是 1，因此 `cm doctor`（无参）会以用法错误退出；若写 `doctor(name, fix = false)`，默认值不计入 `length`，仍是 1。
- [bin/cm.js:35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35)：`new Promise(...).catch(e => error(e))`——统一错误出口，实现函数里可以直接 `throw`，不必自己 try/catch。
- [bin/cm.js:38-57](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L38-L57)：`help()` 的 usage 文本——与映射表手工同步，`help(0)` 供 `--help` 求助、`help(1)` 作用法错误，一份文本两种退出码。
- [bin/cm.js:72-79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)：`assertInstalled()` 守卫——遍历**全量** `packages` 检查目录存在性，缺失即报错退出；新命令自动被它保护。
- [bin/cm.js:11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L11) 与 [bin/cm.js:101](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L101)：模块加载时调用一次 `loadPackages()`；`install()` 在装完依赖后**重新调用**刷新三个视图。`doctor` 里重新调用 `loadPackages()` 沿用的正是这个模式——虽然对一次性 CLI 而言顶层变量同样可用，重调更贴近 install 的既有写法。

`doctor` 的参考实现（**示例代码**，按项目风格书写：无分号、2 空格缩进、单语句体免大括号）：

```js
function doctor(name) {
  let pkg = loadPackages().packageNames[name]
  if (!pkg) help(1)
  console.log(`${pkg.name}:`)
  console.log(`  entry: ${pkg.main || "(none)"}`)
  let dist = pkg.main && join(pkg.dir, "dist", path.basename(pkg.main).replace(/\.ts$/, ".js"))
  console.log(`  build: ${dist && fs.existsSync(dist) ? "ok" : "missing"}`)
}
```

配套改动两处：映射表加 `doctor,` 一项；`help()` 文本加一行 `cm doctor <package>    Check whether a package is cloned and built`。

#### 4.3.4 代码实践：实现并验证 cm doctor

1. **实践目标**：走通「实现函数 → 映射表 → help → 验证」的完整闭环，体会守卫的自动接入。
2. **操作步骤**：
   - 按上面的参考实现修改**自己克隆里的** `bin/cm.js`（本讲所有改动都只在本地练习，见第 5 节末尾的说明）；
   - 运行 `node bin/cm.js doctor state`；
   - 运行 `node bin/cm.js doctor`（不带参数）；
   - 运行 `node bin/cm.js doctor no-such-package`。
3. **需要观察的现象**：
   - 第一条命令输出 `state:` 及其入口路径与 `build: ok`（若已 `cm build` 过）；
   - 第二条命令打印 usage 并以退出码 1 退出（`length` 校验生效）；
   - 第三条命令同样打印 usage 退出（`help(1)` 分支生效）。
4. **预期结果**：三条命令分别验证实现、参数校验、非法包名三条路径。若 `state` 显示 `build: missing`，先运行 `node bin/cm.js build` 再试。（输出细节**待本地验证**。）

#### 4.3.5 小练习与答案

1. **练习**：把 `doctor` 的签名改成 `function doctor(name, verbose = false)`，`cm doctor`（无参）的行为会变吗？
   **答案**：不会。默认值不计入函数 `length`，[bin/cm.js:34](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L34) 的下限校验依旧要求至少 1 个参数，无参仍打印 usage 退出。
2. **练习**：为什么 `doctor` 无法报告「某包尚未克隆」？这个信息由谁负责给出？
   **答案**：`assertInstalled()` 在命令函数执行前遍历全量 `packages`，任一目录缺失即退出；「未克隆」的诊断由守卫的报错文本 `module X is missing. Did you forget to run 'cm install'?` 给出（[bin/cm.js:75](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L75)）。
3. **练习**：新命令忘记加进 `help()` 文本会怎样？举一个现有先例。
   **答案**：功能完全可用，只是 `cm --help` 不可见；先例是 `unreleased`——它在映射表（[bin/cm.js:23](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L23)）里有实现，usage 文本里却没有。

### 4.4 架构取舍：多仓库 + 中央脚本 vs 单体 monorepo

#### 4.4.1 概念说明

做完两类扩展后回头看：为什么 CodeMirror 选择「三十多个独立包仓库 + 一个中央脚本仓库」，而不是一个单体 monorepo？这不是偶然，而是一组明确的取舍：

- **收益**：每个包有独立的版本号、CHANGELOG 与发布节奏（`release` 按包执行）；npm 用户可以按需安装（只装 `state`/`view` 不装语言包）；issue 集中在中央仓库跟踪，而代码权分散在各包仓库；中央仓库本身极小（约 370 行脚本 + demo），新人十分钟就能读完全部工具链。
- **代价**：跨包重构要拆成多个仓库的多次提交（`commit`/`push` 靠逐包循环兜底）；包间依赖联动复杂到被**主动停用**；`paths`/`Pkg.main` 两套机制靠人工同步；工具链大量依赖外部 UNIX 命令，可移植性差。

#### 4.4.2 核心流程

用一张表对比两种方案在本讲关心的四个维度上的差异：

| 维度 | 多仓库 + 中央脚本（本仓库） | 单体 monorepo |
| --- | --- | --- |
| 版本与发布 | 每包独立版本、独立 tag（`release <pkg>`） | 通常统一版本，或需 changesets/lerna 类工具切分 |
| 跨包改动 | 逐仓库提交，`cm commit`/`cm push` 循环代劳 | 一次提交、一次 PR |
| 依赖联动 | 复杂，`updateDependencyVersion` 已写好但被 `if (false)` 停用 | 天然一致，改一处全仓生效 |
| 工具链 | 一个 370 行的脚本 + 少数构建工具，人人可读可改 | 需要专门的任务编排与缓存体系 |

#### 4.4.3 源码精读

**依赖联动的停用**——[bin/cm.js:240-245](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L240-L245)：注释写明「Turned off for now, since this creates a huge mess on accidental major version bumps」，并用 `if (false && ...)` 把整段逻辑封存；判定「显著版本位」的正则 `mainVersion` 在 [bin/cm.js:223](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L223)，被停用的实现 `updateDependencyVersion`（遍历其他包、正则改写依赖版本、amend 或新建提交）仍完整保留在 [bin/cm.js:200-217](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L200-L217)。这是评估多仓库代价时最有说服力的一处证据：跨包版本联动写出来了，却因为「容易造成大混乱」而不敢启用。

**逐包循环的日常**——[bin/cm.js:295-300](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L295-L300) 的 `commit` 用 `git diff` 真值判断有改动的包才提交，[bin/cm.js:302-307](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L302-L307) 的 `push` 用 `\bahead\b` 正则判断领先远端的包才推送——单体 monorepo 里一次搞定的事，这里靠字符串谓词在 36 个目录间循环。

**可移植性问题清单**——`run()` 默认 `shell: false` 直接 `execvp`（[bin/cm.js:64-66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L64-L66)），因此所有外部命令都必须真实存在于 PATH：

| 位置 | 依赖 | 问题 |
| --- | --- | --- |
| [bin/cm.js:90](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L90) | `rm -f dist/* test/*.js` | Windows 无 `rm`；且无 shell 时 `*` 无人展开，字面量 `dist/*` 直达 `rm`（见下） |
| [bin/cm.js:290-293](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L290-L293) | `rm -rf dist` | Windows 无 `rm`；此行无通配符，Unix 下行为正常 |
| [bin/cm.js:328](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L328) | `grep --color -nH` | Windows 无 `grep`；`--color`、`-H` 属 GNU 风格选项 |
| [bin/cm.js:274](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L274) | `EDITOR \|\| emacs` | 未设 `EDITOR` 环境变量时回退到 `emacs`，多数机器上不存在 |
| [bin/cm.js:99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L99) | `shell: process.platform == "win32"` | 唯一的跨平台特判：Windows 上 npm 是 `.cmd` 批处理，不经 shell 无法直接执行 |

关于 [bin/cm.js:90](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L90) 那行还有一个更微妙的隐患：glob 展开是 shell 的职责，`run()` 默认不经 shell，所以 `"dist/*"` 会作为**字面量**传给 `rm`。`rm` 自身不展开通配符，配合 `-f` 的「不存在也不报错」语义，这行的实际清理效果存疑（**待本地验证**：可在一个含 `dist/` 产物的包目录里手动执行对比 `rm -f dist/*` 与 `node -e 'require("child_process").execFileSync("rm", ["-f", "dist/*"], {cwd: process.cwd()})'` 后的目录内容）。这是一个很好的教训：**跨平台与「无 shell 执行」两件事叠加时，连最寻常的一行清理命令都值得重新审视**。

#### 4.4.4 代码实践：可移植性审计

1. **实践目标**：系统性地找出 `cm.js` 对运行环境的全部隐式假设，产出一份审计清单。
2. **操作步骤**：
   - 通读 [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L1-L367)，把所有 `run("X", ...)` 的第一个参数收集成表；
   - 按三分类标注：Node 内置可替代（`rm` → `fs.rmSync`）/ Unix 常见但 Windows 缺失（`grep`）/ 必备且跨平台（`git`、`npm`）；
   - 为 `clean()` 写一个跨平台版本（**示例代码**）：把 [bin/cm.js:290-293](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L290-L293) 的 `run("rm", ...)` 换成 `fs.rmSync(join(pkg.dir, "dist"), {recursive: true, force: true})`（README 要求 Node 16，`fs.rmSync` 自 14.14 起可用）。
3. **需要观察的现象**：审计表应至少覆盖 `git`、`npm`、`rm`、`grep`、`emacs`（或 `$EDITOR`）五个外部程序；跨平台版 `clean()` 在 Linux 上与原版行为一致。
4. **预期结果**：得出结论——`cm.js` 事实上假定类 Unix 环境，唯一的平台特判只有 [bin/cm.js:99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L99) 的 win32 npm。改动效果**待本地验证**。

#### 4.4.5 小练习与答案

1. **练习**：为什么 [bin/cm.js:99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L99) 单独给 npm 开了 `shell: true`，而 `git` 不需要？
   **答案**：Windows 上 `npm` 实际是 `npm.cmd` 批处理脚本，无 shell 的直接执行只能启动真正的可执行文件；`git` 在各平台都是原生可执行文件（`git.exe`），无需 shell。注意 `shell: true` 换来兼容性的代价是参数要过一遍 shell 解析，所以只在必要处启用。
2. **练习**：如果要把本仓库改造成单体 monorepo，最先失效的是哪些代码？
   **答案**：`Pkg.dir` 的「仓库根/包名」目录假设、`install()` 的逐包克隆/更新、`status`/`commit`/`push` 的逐包 git 循环、`changelog()` 的按包 `git log`、`doRelease()` 的按包 tag——即几乎所有以「一个包 = 一个 git 仓库」为前提的代码；而 `Pkg.main` 探测、`paths`、workspaces、`build()`/`test()` 的文件级逻辑可以基本保留。
3. **练习**：`updateDependencyVersion` 被停用，风险具体指什么？
   **答案**：它按 `mainVersion`（显著版本位）判断，一旦某次发布意外跨过不兼容边界（如误升 major），就会自动给**所有**依赖该包的包批量改 `package.json` 并产生大量「Bump dependency」提交——注释所说的「huge mess」。这是多仓库间联动自动化与人为可控性之间的典型权衡。

## 5. 综合实践：从零接入一个实验包并为它写体检命令

把本讲三个扩展点串成一个完整任务。**所有改动都在你自己克隆的本地仓库里做练习，不要提交上游**——第 6 步会说明原因。

1. **实践目标**：注册实验包 `lang-tiny`，让它被清单、构建、paths 三处正确认知，并用新命令 `cm doctor` 为它做体检。

2. **操作步骤**：

   1. **创建包目录**（顺序很重要，见 4.1.2 的坑）：

      ```text
      lang-tiny/
      ├── package.json
      ├── src/
      │   └── index.ts
      └── test/
          └── test-tiny.ts
      ```

      `package.json` 的字段（name 以 `@codemirror/` 为前缀、`type: module`、`main` 指向 `dist` 产物）可参照任一已克隆包（如 `lang-yaml`）的写法（**示例代码**，具体字段待对照确认）：

      ```json
      {
        "name": "@codemirror/lang-tiny",
        "version": "0.0.0",
        "main": "dist/index.js",
        "type": "module"
      }
      ```

      `src/index.ts` 先导出一个常量即可，例如 `export const tinyVersion = 1`。

   2. **注册进清单**：往 [bin/packages.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L17-L42) 的 `nonCore` 数组追加 `"lang-tiny"`。选 `nonCore` 而非 `core`：实验包不该进入 `core` 的文档链接改写逻辑（[bin/build-readme.js:11-12](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/build-readme.js#L11-L12)）。注意：不要对它跑 `cm build-readme`——它的 `src/` 里没有 `README.md` 模板，会失败。

   3. **验证入口探测**：目录里恰好只有一个 `.ts` 文件时命中「唯一文件」规则；再往 `src/` 加一个 `util.ts`，探测应落到 `index.ts` 规则——用 4.1.4 的 `probe.js` 或 `cm doctor`（下一步）观察 `main` 的变化。

   4. **补 paths 映射**：在 [tsconfig.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L13-L49) 的 `paths` 里加 `"@codemirror/lang-tiny": ["./lang-tiny/src/index.ts"]`——目标必须与探测出的 `main` 一致。

   5. **实现 `cm doctor`**：按 4.3.3 的参考实现改 `bin/cm.js`（实现函数 + `cmdFn` 表项 + `help()` 一行），跑一遍 4.3.4 的三条验证命令。

   6. **构建与测试**：依次运行——

      ```bash
      node bin/cm.js build                 # lang-tiny 应进入 buildPackages 并产出 dist/
      node bin/cm.js doctor lang-tiny      # 期望 entry 指向 src/index.ts、build: ok
      npm run test-node                    # test/test-tiny.ts 应被收集执行
      node bin/cm.js status                # 本仓库自身应显示对 bin/cm.js 等的未提交改动
      ```

      测试文件（**示例代码**）验证 `include` 通配与 mocha 全局类型同时生效：

      ```ts
      // lang-tiny/test/test-tiny.ts（示例代码）
      import {tinyVersion} from "@codemirror/lang-tiny"

      describe("lang-tiny", () => {
        it("exports its version", () => {
          if (tinyVersion != 1) throw new Error("unexpected version")
        })
      })
      ```

   7. **按 CONTRIBUTING 整理改动**：对照 [CONTRIBUTING.md:79-99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L79-L99) 的编码标准自查——2 空格缩进、除必要处无分号、单语句体免大括号、跟随周围代码的空格与花括号风格；并遵守 [CONTRIBUTING.md:65-66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L65-L66) 的「一个 PR 只含一个改动」原则（本练习里的 doctor 命令与实验包就应当是两个独立改动）。

   8. **清理现场**：练习结束后用 `git checkout -- bin/cm.js bin/packages.js tsconfig.json`（本仓库）撤销改动，`rm -rf lang-tiny/ probe.js` 移除实验文件，恢复原状。

3. **需要观察的现象**：
   - `cm build` 的输出比之前多处理一个包；
   - `cm doctor lang-tiny` 三行输出随「加 util.ts 前后」「build 前后」发生变化；
   - `npm run test-node` 的输出中出现 `lang-tiny` 的用例分组；
   - `cm status` 能看到中央仓库自身的未提交修改（各包仓库不受影响）。

4. **预期结果**：三个扩展点全部生效——清单让包「存在」，paths/workspaces 让包「可引用」，doctor 让包「可诊断」；每一步的输出细节**待本地验证**。

5. **重要提醒**：[CONTRIBUTING.md:38-41](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L38-L41) 明确写明 CodeMirror **不欢迎 AI 生成的代码**（无论部分还是全部）。本讲（以及整部手册）的实践都是为了理解源码而做的本地练习；如果你计划向 CodeMirror 提交真实 PR，请完全由自己编写代码、遵守上述规范并通过 `npm run test`。

## 6. 本讲小结

- 这套仓库有两个数据型扩展点：`packages.js` 的包名清单（决定包「是否存在」）与 `cm.js` 的 `cmdFn` 映射表（决定命令「是否存在」），扩展它们就是往清单里加一行。
- 注册新包的正确顺序是**先建目录与入口、再进清单**：`assertInstalled()` 检查全量 `packages`，名字先于目录注册会让所有命令瘫痪，而 `cm install` 又会去克隆不存在的远端仓库。
- 解析是双轨且半自动的：`workspaces: ["*"]` 与 tsconfig `include` 通配让新目录自动纳入，唯独 `paths` 必须手工补映射，且目标要与 `Pkg.main` 探测结果人工保持一致（`lang-liquid → lang-vue/src/liquid.ts` 这种跨仓库映射就是它纯手工本质的证据）。
- 新命令自动继承 `assertInstalled()` 守卫、`length` 参数下限校验与统一错误出口；`help()` 文本需手工同步，映射表才是权威命令清单。
- 「多仓库 + 中央脚本」的代价有实证：依赖联动 `updateDependencyVersion` 写好后被 `if (false)` 封存（怕意外 major 引发批量混乱）；收益是每包独立版本与发布、中央仓库工具链小到可整体读完。
- `cm.js` 依赖 `rm`、`grep`、`emacs` 等外部工具且默认无 shell 执行，事实上假定了类 Unix 环境；[bin/cm.js:90](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L90) 的 `rm -f dist/*` 还叠加了「glob 无人展开」的隐患——跨平台改造可从 `fs.rmSync` 替换 `rm` 开始。

## 7. 下一步学习建议

至此十三讲的手册已经走完，你已经从「这个仓库是什么」走到了「亲手扩展它」。接下来三条路任选：

1. **地图式复盘**：不带问题重读一遍 [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L1-L367) 全文（约 370 行），此时每个函数你都应该能说出它的三个视图、守卫与错误出口——检验学习成果的最好方式。
2. **进入一个真实包仓库**：挑一个已克隆的包（推荐 `state`，它是最底层的依赖）通读它的 `src/` 与 `test/`，观察「被这套工具链驱动的一个包」长什么样：`src/README.md` 模板、`test/test-*.ts` 命名、`dist/` 产物如何被 `cm build` 产出。
3. **深挖工具链的依赖库**：`@marijn/buildtool`、`@marijn/testtool`、`getdocs-ts`、`builddocs` 都在 `node_modules` 里且仓库可访问——它们分别承担增量编译、双轨测试、注释收集与 HTML 渲染，读懂任何一个都等于把本手册的一条支线延展下去。
