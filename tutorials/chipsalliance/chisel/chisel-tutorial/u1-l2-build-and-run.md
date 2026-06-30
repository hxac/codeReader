# 构建、测试与本地发布

## 1. 本讲目标

本讲带你从「拿到源码」走到「能编译、能测试、能把本地版本发布出去给别的项目用」。

读完本讲你应该能够：

- 说清楚 Chisel 为什么用 **mill**（而不是 sbt）来构建，以及 `./mill` 这个入口到底做了什么。
- 看懂 [build.mill](build.mill) 里 `v` 对象如何管理版本号、Scala 交叉版本、以及 firtool（CIRCT）版本。
- 理解仓库如何拆成 `core` / `plugin` / `macros` / `firrtl` / `svsim` / `src` 等多个子模块，以及它们之间的依赖。
- 掌握三条核心命令：`./mill chisel[].compile`、`./mill chisel[].test`、`./mill chisel[].unitTest`。
- 学会把一份本地 SNAPSHOT 版本 `publishLocal` 到本地 Ivy 仓库，并在另一个项目的 `build.sbt` 里引用它。

承接上一篇（u1-l1）：你已经知道 Chisel 是嵌在 Scala 里的 EDSL，最终要靠 **firtool** 产出 SystemVerilog。那么「源码 → 能跑出 Verilog 的工程」之间，构建系统、依赖、发布到底是怎么串起来的？本讲回答这个问题。

## 2. 前置知识

本讲是偏「工程操作」的一讲，不需要你写过 Chisel 电路，但建议先了解这几个概念：

- **构建工具（build tool）**：负责把一堆 `.scala` 源码编译成 `.class`/`.jar`、拉取第三方依赖、组织测试与发布。Scala 生态里最常见的是 **sbt**，而 Chisel 仓库用的是 **mill**。两者目标相同，只是配置写法不同（mill 用 Scala 对象，sbt 用 `build.sbt` 这门 DSL）。
- **交叉编译（cross-version / cross-publish）**：同一份源码要针对多个 Scala 版本（如 `2.13.18`、`3.7`）各编译一次、各发布一个制品。这是 Scala 库的常规操作。
- **firtool / CIRCT**：LLVM CIRCT 项目提供的命令行编译器，把 FIRRTL/CHIRRTL 编译成 SystemVerilog。它是 Chisel 的「后端」，没有它就出不了 Verilog。
- **本地仓库（local Ivy repo）**：`~/.ivy2/local/` 是你机器上的本地依赖仓库。`publishLocal` 会把编译好的 jar 放进去，别的项目就能用「本地版本号」引用它，而不必等它发布到 Maven Central。
- **SNAPSHOT 版本**：语义版本号末尾带 `-SNAPSHOT`，表示「正在开发、随时会变」的版本。Chisel 的 `main` 分支每次提交都会产生一个新的 SNAPSHOT。

> 小提示：本讲的命令都以仓库根目录为当前目录运行，入口都是 `./mill`（仓库自带的可执行脚本）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`mill`](mill) | mill 启动脚本（仓库自带的 wrapper），负责下载并启动正确版本的 mill。 |
| [`build.mill`](build.mill) | **核心构建文件**。定义版本配置对象 `v`、交叉编译 trait `ChiselCrossModule`、`chisel`/`unipublish` 等模块、CIRCT 下载逻辑 `circt`、以及 `unitTest` 命令。 |
| [`release.mill`](release.mill) | 发布相关定义。包含 `ChiselPublishModule`（含 `publishVersion`）和聚合发布 trait `Unipublish`（含 `publishLocal`）。 |
| [`README.md`](README.md) | 用户/贡献者文档。其中的「Compiling and Testing Chisel」「Running Projects Against Local Chisel」两节是本讲命令的权威出处。 |
| [`SETUP.md`](SETUP.md) | 本地环境搭建说明：JVM、sbt、firtool、Verilator、FileCheck 的安装。 |
| [`etc/circt.json`](etc/circt.json) | 一个极小的 JSON，记录当前构建期望的 firtool 版本（`firtool-1.151.0`）。 |

## 4. 核心概念与源码讲解

### 4.1 mill 构建工具与命令入口

#### 4.1.1 概念说明

Chisel 仓库不要求你预先装好 mill。仓库根目录带了一个名为 `mill` 的可执行 shell 脚本（wrapper）。你只需要装好 **JVM**，运行 `./mill <任务>`，脚本会自动下载所需版本的 mill，再去解析 `build.mill`、执行你指定的任务。

为什么用 mill 而不是 sbt？因为 mill 的构建定义就是**普通的 Scala 对象**（`object v`、`trait Chisel`、`object chisel` …），可以自由地写函数、复用 trait、加注释，比 `build.sbt` 那门专用 DSL 更接近普通 Scala 代码，对阅读源码也更友好。

> 上一篇 u1-l1 提到，Chisel 代码本身是「普通 Scala 程序」。这里的构建脚本也是同一思想：用 Scala 来描述如何构建 Scala。

#### 4.1.2 核心流程

`./mill <模块>.<任务>` 的执行过程大致是：

1. 运行仓库自带的 `./mill` 脚本。
2. 脚本按内嵌默认版本下载并启动 mill（一个 JVM 程序）。
3. mill 编译并加载 `build.mill`（必要时也加载 `release.mill`，它们都在 `package build` 下）。
4. mill 解析命令行里的 `<模块>.<任务>`，定位到对应 Scala 对象的目标方法（`def xxx = Task.Command { ... }`），按依赖关系增量执行。
5. 输出结果（编译产物在 `out/` 目录，或直接打印到终端）。

#### 4.1.3 源码精读

启动脚本本体里写死了默认 mill 版本：

[mill:5-5](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/mill#L5-L5) —— 这里 `DEFAULT_MILL_VERSION="1.1.0"` 是 wrapper 的兜底版本。

真正决定「本次构建用哪个 mill」的是 `build.mill` 顶部的 mill 指令头：

[build.mill:1-5](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L1-L5) —— 第 1 行 `//| mill-version: 1.0.6-jvm` 声明本构建绑定 mill `1.0.6-jvm`；第 2–5 行 `//| mvnDeps:` 列出构建脚本自身需要的额外依赖（如 `mill-contrib-jmh`、`mill-mima`、`github4s` 等）。这些 `//|` 开头的注释是 mill 的「构建脚本元数据」约定，会被 wrapper 读取。

> 注意区分两个版本号：wrapper 脚本里的 `1.1.0` 只是「没指定时下载哪个 mill」的兜底；真正生效的是 `build.mill:1` 里的 `1.0.6-jvm`。

#### 4.1.4 代码实践

**实践目标**：确认你能在本地拉起 mill 并看到它的版本。

操作步骤：

1. 确认已装 JVM（`java -version` 能正常输出）。
2. 在仓库根目录运行 `./mill --version`。

需要观察的现象：mill 会先下载（首次较慢），随后打印 mill 版本与 JVM 信息。

预期结果：看到类似 `Mill Build Tool version X.Y.Z` 的输出。

> 待本地验证：不同网络环境下首次下载耗时差异较大；若下载失败可设置代理或手动放置 mill。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Chisel 仓库根目录会自带一个叫 `mill` 的脚本，而不是让你自己 `brew install mill`？

**参考答案**：自带 wrapper 可以把「用哪个 mill 版本」固化在仓库里，保证所有开发者/CI 用同一版本构建，避免「我机器上能编译」的问题。`build.mill:1` 的 `mill-version` 头就是这层版本锁定的体现。

---

### 4.2 `v` 对象：版本号、Scala 交叉版本与 firtool 版本

#### 4.2.1 概念说明

`build.mill` 里有一个集中的配置对象 `object v`，它几乎是「整份构建的单一事实来源」：当前支持的 Scala 版本、如何把交叉版本号映射成完整 Scala 版本、依赖库的版本，以及最重要的——**Chisel 自己的版本号**怎么算。理解 `v`，就能回答「我本地构建出的 Chisel 到底叫什么版本」。

#### 4.2.2 核心流程

`v.version` 的计算完全基于 git，思路是「距离上一个 tag 有多远」：

1. 用 `git describe --tags --abbrev=0` 取最近一个 tag（如 `v7.13.0`）。
2. 用 `git rev-list --count HEAD --not <tag>` 数从那个 tag 到 HEAD 有多少次提交。
3. 若提交数为 0，说明 HEAD 正好就在某个 tag 上，版本就是 tag 去掉前缀 `v`（如 `7.13.0`）。
4. 否则，版本形如 `<tag 去掉 v>+<提交数>-<commit hash 前 8 位>-SNAPSHOT`，例如 README 里举的例子 `7.1.1+16-767b9eb3-SNAPSHOT`。
5. 末尾带 `-SNAPSHOT` 即被 `isSnapshot` 判定为快照版。

用文字公式表示这个 SNAPSHOT 版本号：

\[ \text{version} = \text{previousTagNoV} \;+\; \text{"+"} \;+\; \text{commitsSinceTag} \;+\; \text{"-"} \;+\; \text{hash8} \;+\; \text{"-SNAPSHOT"} \]

#### 4.2.3 源码精读

`v` 对象整体定义在这里，集中存放 Scala/firtool 版本与依赖坐标：

[build.mill:16-191](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L16-L191) —— `object v extends Module`，包含所有版本与依赖常量。

其中 Scala 交叉版本与映射规则：

[build.mill:47-52](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L47-L52) —— `scalaCrossVersions = Seq("2.13", "3")` 表示只用「简化的交叉版本标签」`2.13` 和 `3`；`scalaCrossToVersion` 再把它们展开成完整版本（见下一行附近的 `2.13.$scala213MinorVersion` 与 `3.$scala3MinorVersion`）。

[build.mill:19-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L19-L22) —— `scala213MinorVersion = 18`（即 `2.13.18`）、`scala3MinorVersion = "3.7"`（即 `3.7`）。要支持更新的 Scala 小版本，就改这两个常量。

firtool 版本来自一个外部小文件，而不是硬编码在构建里：

[build.mill:32-39](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L32-L39) —— `circtJson` 读取 `etc/circt.json`，`firtoolVersion` 从中取出 `version` 字段并去掉 `firtool-` 前缀。本仓库当前该文件内容为 `{"version": "firtool-1.151.0"}`，对应最近的提交 `[cd] Bump CIRCT from firtool-1.150.2 to firtool-1.151.0`。

最关键的版本号计算逻辑：

[build.mill:160-187](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L160-L187) —— `def version = Task.Input { ... }`。注意它是 `Task.Input`，意味着每次都会重新执行（因为依赖 git 状态）。其中第 184 行 `s"$previousTagNoV+$commitsSincePreviousTag-$currentCommit-SNAPSHOT"` 正是上文 SNAPSHOT 公式的实现。

[build.mill:190-190](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L190-L190) —— `def isSnapshot = Task { version().endsWith("-SNAPSHOT") }`，被发布逻辑用来判断是否快照。

#### 4.2.4 代码实践

**实践目标**：理解当前 HEAD 会算出什么版本号，但不实际运行（实跑见第 5 节综合实践）。

操作步骤（源码阅读型）：

1. 运行 `git describe --tags --abbrev=0` 看最近 tag（当前为 `v7.13.0`）。
2. 对照 [build.mill:160-187](build.mill) 的逻辑，判断：如果当前 HEAD 不是 `v7.13.0` 本身，版本号会是什么形状？
3. 在 [etc/circt.json](etc/circt.json) 里确认当前 firtool 版本。

需要观察的现象/预期结果：你能口头复述版本号格式 `<tag>+<N>-<hash>-SNAPSHOT`，并指出 firtool 版本来自 `etc/circt.json` 而非 `build.mill`。

#### 4.2.5 小练习与答案

**练习 1**：如果你 `git checkout` 到一个正好打 tag 的提交（比如 `v7.13.0` 本身），`v.version` 会返回什么？

**参考答案**：由于 `commitsSincePreviousTag == 0`，会命中 [build.mill:177-178](build.mill) 的分支，直接返回 `previousTagNoV`，即 `7.13.0`（不带 `-SNAPSHOT`）。

**练习 2**：为什么 `firtoolVersion` 要从 `etc/circt.json` 读，而不是直接写在 `build.mill` 里？

**参考答案**：把易变的「工具版本」单独放进一个小 JSON，便于自动化脚本（如 dependabot 改 `circt.json` 触发「Bump CIRCT」提交）精确地只改一处，而不用触碰构建逻辑本身。

---

### 4.3 多模块组织与 ChiselCrossModule

#### 4.3.1 概念说明

Chisel 不是单一工程，而是被拆成若干个**子模块**（sub-project），每个子模块是一个独立的编译单元，有自己的源码目录和依赖。这样做是为了职责分离，也方便分别发布或复用。各个子模块对应仓库里的目录：

| 子模块 | 目录 | 职责（摘自 README） |
| --- | --- | --- |
| `core` | [`core/`](core) | Chisel 主体源码，依赖 `firrtl`/`svsim`/`macros` |
| `firrtl` | [`firrtl/`](firrtl) | 旧 Scala FIRRTL 编译器的残留部分 |
| `macros` | [`macros/`](macros) | Chisel 用到的大部分宏，无内部依赖 |
| `plugin` | [`plugin/`](plugin) | scalac 编译器插件，无内部依赖 |
| `src/main` | [`src/`](src) | 把上述模块整合到一起的「main」，并包含 `chisel3.util` 标准库 |
| `svsim` | [`svsim/`](svsim) | 编译并控制 SystemVerilog 仿真的底层库 |

> 目录划分的细节会在 u1-l3「目录结构与子项目划分」里展开。本讲只需建立「多模块」的概念。

#### 4.3.2 核心流程

mill 里「一个模块」通常是一个 `trait`/`object`，继承自 `ScalaModule`（或其交叉编译变体 `CrossSbtModule`）。多个模块之间通过 `moduleDeps` 声明依赖。为了避免每个模块都重复写「Scala 版本怎么映射」，Chisel 抽出了一个公共 trait `ChiselCrossModule`，让所有需要交叉编译的模块都混入它。

#### 4.3.3 源码精读

公共交叉编译 trait：

[build.mill:212-215](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L212-L215) —— `trait ChiselCrossModule extends CrossSbtModule`，把 `crossScalaVersion`（形如 `"2.13"`/`"3"`）统一映射成完整 Scala 版本。注释说「Keep this lean, it's mixed in to firrtl and svsim as well」，说明 `firrtl`、`svsim` 也复用它。

自动注入编译器插件的能力（这是 Chisel 能用 `Module`/`IO` 这类 API 的前提之一，详见第 7 单元插件讲义）：

[build.mill:225-235](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L225-L235) —— `trait HasScalaPlugin` 在 `scalacOptions` 里追加 `-Xplugin:<plugin jar>`，并把插件加入 classpath。`chisel` 模块会混入它，从而编译期自动挂上 `chisel-plugin`。

顶层 `chisel` 模块与它的依赖：

[build.mill:303-305](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L303-L305) —— `object chisel extends Cross[Chisel](v.scalaCrossVersions)`，按 `v.scalaCrossVersions`（即 `2.13`、`3`）做交叉；`trait Chisel` 混入了 `HasScala2MacroAnno`、`HasScalaPlugin`、`ScalafmtModule`。

[build.mill:319-319](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L319-L319) —— `override def moduleDeps = ... ++ Seq(coreModule, svsimModule)`：`chisel` 模块依赖 `core` 与 `svsim`（它们又会带上各自的依赖），这构成了图中的依赖边。

把所有模块汇总以便「一键编译」：

[build.mill:66-82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L66-L82) —— `def buildUnits()` 列出所有要参与编译/测试的单元（含各模块及其 `.test`），供 `compileAll` 遍历。

[build.mill:194-196](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L194-L196) —— `def compileAll() = Task.Command { Task.traverse(v.buildUnits())(_.compile)() }`：遍历 `buildUnits()` 并全部编译，是个常用的「一次性编译所有」快捷命令。

#### 4.3.4 代码实践

**实践目标**：用源码画出「`chisel` 依赖谁」的关系。

操作步骤：

1. 打开 [build.mill:319](build.mill)，记下 `chisel` 的 `moduleDeps`。
2. 在 `build.mill` 中搜索 `coreModule` / `svsimModule` 的定义（`def coreModule = core.cross(...)`、`def svsimModule = svsim.cross(...)`）。
3. 在纸上画出：`chisel → core`、`chisel → svsim`，再补上 README 说的 `core → firrtl/macros`。

预期结果：得到一张有向依赖图，`core` 在中间偏左，`chisel` 在最上层。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `plugin` 模块要和 `core` 分开，而且 `plugin` 没有「内部依赖」？

**参考答案**：`plugin` 是挂进 scalac 的编译期插件，必须在用户的 Scala 编译器里独立加载，不能依赖 Chisel 运行时；所以它单独成模块、不依赖 `core`。`core` 编译时反而需要先有 `plugin`（见 `HasScalaPlugin` 把 plugin jar 当作 `-Xplugin`）。

---

### 4.4 编译、测试与 unitTest 命令

#### 4.4.1 概念说明

有了模块定义，接下来就是「怎么编译、怎么测试」。Chisel 仓库有**两套测试**：

1. **ScalaTest 单元测试**：普通的 Scala 单测（`extends AnyFreeSpec` 之类），用 `./mill chisel[].test` 运行。
2. **Chisel UnitTest**：一类特殊的「把电路测试编译成 FIR、再用 firtool + circt-test 跑」的测试（标记了 `chisel3.UnitTest` trait 的模块），用 `./mill chisel[].unitTest` 运行。

这两套测试的目标不同：前者验证 Scala 层行为，后者验证「生成的电路」在 firtool/circt-test 下是否如预期工作。第 9 单元会专门讲测试体系，这里只需会用命令。

> 命令里的 `chisel[]` 是 mill 的交叉编译语法：方括号留空表示「用默认交叉版本」，等价于自动选 `2.13`。也可以写成 `chisel[2.13]` 或 `chisel[3]` 指定具体版本。

#### 4.4.2 核心流程

- `./mill chisel[].compile`：编译 `chisel` 模块（及其依赖 `core`/`svsim` 等）的主源码。
- `./mill chisel[].test`：编译并运行 `chisel` 模块的 ScalaTest 测试。
- `./mill chisel[].unitTest`：执行一段特殊流水线——
  1. 调用 `chisel3.UnitTests` 主类，把所有标记 UnitTest 的模块 elaboration 成一个汇总 `.fir` 文件。
  2. 用 `firtool --ir-hw` 把 `.fir` 编译成 `.mlir`。
  3. 用 `circt-test` 跑这份 `.mlir` 里的全部测试。

#### 4.4.3 源码精读

README 给出的命令权威写法：

[README.md:297-318](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L297-L318) —— 先 `./mill chisel[].compile`，再 `./mill chisel[].test` 与 `./mill chisel[].unitTest`。同时说明运行这些测试前需要把 `verilator`、`yosys`、`espresso`、`slang`、`filecheck` 放到 `PATH` 上。

`unitTest` 这条特殊流水线的实现：

[build.mill:370-413](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L370-L413) —— `def unitTest(...)` 接收三组参数：`chiselArgs`（透传给 `chisel3.UnitTests`）、`firtoolArgs`（透传给 firtool，快捷键 `-C`）、`circtTestArgs`（透传给 circt-test，快捷键 `-T`）。

其中关键三步：

[build.mill:386-392](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L386-L392) —— 第 1 步，用 `test.runner().run(...)` 调起 `mainClass = "chisel3.UnitTests"`，生成汇总 FIR 文件 `unit_tests.fir`。`-R` 参数把测试类加入 classpath 并限定 UnitTest 发现范围。

[build.mill:394-404](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L394-L404) —— 第 2 步，调用 `circt.binDir()/firtool`，以 `--ir-hw` 把 FIR 编译为 MLIR。

[build.mill:406-412](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L406-L412) —— 第 3 步，调用 `circt-test` 跑这份 MLIR。

注意第 2、3 步用到的 `circt.binDir()` 来自下一个模块讲的自动下载逻辑——也就是说 `unitTest` 隐式依赖了「能拿到 firtool/circt-test 二进制」。

ScalaTest 测试子模块的定义：

[build.mill:323-368](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L323-L368) —— `object test extends CrossSbtTests with TestModule.ScalaTest ...`，依赖 `scalatest`/`scalacheck`，并会在测试前编译若干 `.c` 共享库供仿真链接测试使用（`sharedTestLibs`）。这部分细节属于第 9 单元，本讲略过。

#### 4.4.4 代码实践

**实践目标**：完成一次完整编译，并（若环境允许）跑通一部分测试。

操作步骤：

1. 先按 [SETUP.md:14-87](SETUP.md) 把 firtool、verilator、filecheck 等装好并加入 `PATH`（用 `which verilator` 等逐个确认）。
2. 运行 `./mill chisel[].compile`。
3. 可选：运行 `./mill chisel[].test`（较慢）。

需要观察的现象：编译成功会打印各模块编译完成、最终无 `BUILD FAILED`。

预期结果：`compile` 应当能成功；`test`/`unitTest` 是否全绿取决于本地工具链是否齐全。

> 待本地验证：`unitTest` 依赖 firtool 与 circt-test 二进制，若 `CIRCT_INSTALL_PATH` 未配置会触发自动下载（见 4.5）。若网络受限，可能需要手动放置二进制。

#### 4.4.5 小练习与答案

**练习 1**：`./mill chisel[].test` 与 `./mill chisel[].unitTest` 跑的是同一批测试吗？

**参考答案**：不是。`test` 跑 ScalaTest 单测（验证 Scala 层），`unitTest` 跑标记了 `chisel3.UnitTest` 的电路测试，后者还要经 firtool 编译、circt-test 执行，见 [build.mill:370-413](build.mill)。

**练习 2**：命令里的 `chisel[]` 中括号为什么可以留空？

**参考答案**：这是 mill 的交叉编译选择语法，留空表示用默认交叉版本（这里即 `2.13`），等价于 `chisel[2.13]`。

---

### 4.5 CIRCT(firtool) 自动下载与本地发布 unipublish

#### 4.5.1 概念说明

最后一块拼图是「firtool 从哪来」和「怎么把本地 Chisel 发出去」。

- **firtool 自动下载**：`unitTest` 与 Verilog 生成都需要 firtool。Chisel 的构建自带一个 `circt` 模块，会在首次需要时按 `etc/circt.json` 指定的版本，从 CIRCT 官方 release 下载对应平台的 tar 包并解压；也可用环境变量 `CIRCT_INSTALL_PATH` 指向一个已解压目录来跳过下载。
- **unipublish（统一发布）**：Chisel 把 `core`/`firrtl`/`svsim`/`macros`/`chisel` 等多个子模块**聚合**成一个制品来发布，名字就叫 `chisel`。`unipublish` 就是这个「聚合发布模块」。`publishLocal` 会把它（连同编译器插件）一起发布到本地 Ivy 仓库。

#### 4.5.2 核心流程

firtool 获取流程（见 `circt.installDir`）：

1. 检查环境变量 `CIRCT_INSTALL_PATH`，若有就直接用它。
2. 否则按 `v.firtoolVersion`（来自 `etc/circt.json`）+ 当前 OS/架构拼出 CIRCT release URL。
3. 下载 tar 包、解压到 mill 的持久化目录（`Task(persistent = true)`，避免重复下载）。
4. `circt.binDir` 指向解压后的 `bin/`，供 `unitTest` 取用 `firtool`/`circt-test`。

本地发布流程：

1. `./mill show unipublish[2.13].publishVersion` 查询当前版本号（由 `v.version` 算出，见 4.2）。
2. `./mill unipublish[2.13].publishLocal` 触发发布：先发插件，再发聚合制品，落到 `~/.ivy2/local/org.chipsalliance/`。
3. 在别的项目的 `build.sbt` 里把这个版本号填进 `libraryDependencies`/`addCompilerPlugin`，sbt 就会从本地仓库解析。

#### 4.5.3 源码精读

firtool 自动下载逻辑：

[build.mill:250-300](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L250-L300) —— `object circt extends Module`，检测 OS/架构并构造下载 URL。

[build.mill:277-297](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L277-L297) —— `def installDir: T[os.Path] = Task(persistent = true)`：优先用 `CIRCT_INSTALL_PATH`；否则下载并解压。`persistent = true` 让结果跨次构建复用，不重复下载。

聚合发布模块 `unipublish`：

[build.mill:416-420](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L416-L420) —— `object unipublish extends Cross[Unipublish](v.scalaCrossVersions)`，`publishableVersions` 决定哪些 Scala 版本允许发布（快照版可发 Scala 3，正式版只发 2.13）。

`Unipublish` 的定义在 `release.mill`，它把多个子模块聚合：

[release.mill:37-40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/release.mill#L37-L40) —— `trait Unipublish extends ChiselCrossModule with ChiselPublishModule with Mima`，并把 `artifactName` 覆盖为 `"chisel"`（所以最终制品叫 `chisel`，而不是 `core` 之类）。

[release.mill:126-126](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/release.mill#L126-L126) —— `def components = Seq(firrtl.cross, svsim.cross, macros.cross, core.cross, chisel).map(_(crossScalaVersion))`：`unipublish` 的「内容」就是把这五个子模块在当前交叉版本下的产物聚合起来。

版本号来源：

[release.mill:17-33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/release.mill#L17-L33) —— `trait ChiselPublishModule`，其中 [release.mill:32](release.mill) `override def publishVersion = v.version()`：发布版本直接复用 4.2 节 `v.version` 算出的 git 版本号。

`publishLocal` 的特殊处理（顺带把插件也发了）：

[release.mill:114-123](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/release.mill#L114-L123) —— `override def publishLocal(...)`：先调 `plugin.cross(pluginVersion).publishLocal(...)`，再调 `super.publishLocal(...)`。这正是为什么光发 `unipublish` 就能让别的项目同时拿到库和编译器插件。

README 给出的「本地发布 + 引用」完整流程：

[README.md:320-341](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L320-L341) —— `./mill show unipublish[2.13].publishVersion` 查版本；`./mill unipublish[2.13].publishLocal` 发布；产物落在 `~/.ivy2/local/org.chipsalliance/`；然后在 `build.sbt` 里用 `addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full)` 与 `libraryDependencies += "org.chipsalliance" %% "chisel" % chiselVersion` 引用。

#### 4.5.4 代码实践

**实践目标**：在本地发布一份 SNAPSHOT 并记录其版本号（本讲综合实践会真正执行它）。

操作步骤（先做「查询」，发布留到第 5 节一起做）：

1. 运行 `./mill show unipublish[2.13].publishVersion`，记下输出（形如 `7.13.0+N-<hash>-SNAPSHOT`，具体取决于你当前 HEAD 距上一个 tag 的提交数）。
2. 对照 [release.mill:32](release.mill) 与 [build.mill:160-187](build.mill)，确认这个版本号确实来自 git。

需要观察的现象：命令打印一行版本字符串。

预期结果：得到一个以 `-SNAPSHOT` 结尾（若不在 tag 上）的版本号。

> 待本地验证：实际版本号取决于本地 git 状态；不要照抄 README 里的旧例子 `7.1.1+16-...`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `unipublish` 的 `publishLocal` 要先发 `plugin` 再发自己？

**参考答案**：因为使用 Chisel 的项目需要**同时**有 `chisel` 库和 `chisel-plugin` 编译器插件（见 `HasScalaPlugin`）。把插件发布也放进 `unipublish.publishLocal`，可以保证一条命令就把「库 + 插件」都发到本地仓库，使用者拿到一致的版本对。见 [release.mill:121-122](release.mill)。

**练习 2**：怎样让构建「不要每次都去网上下载 firtool」？

**参考答案**：设置环境变量 `CIRCT_INSTALL_PATH` 指向一个已解压的 CIRCT 目录，`circt.installDir` 就会直接用它而跳过下载，见 [build.mill:278-280](build.mill)。

---

## 5. 综合实践

**任务**：把「编译 → 查版本 → 本地发布 → 在新项目引用」这条链完整跑通一遍，并把每一步的产物记下来。

操作步骤：

1. **准备环境**：按 [SETUP.md](SETUP.md) 装 JVM；运行 `./mill --version` 确认 mill 可用。
2. **编译**：运行 `./mill chisel[].compile`，确认无报错（这是本讲指定的核心实践）。
3. **查版本**：运行 `./mill show unipublish[2.13].publishVersion`，把输出的版本号（设为 `$V`）抄下来。
4. **本地发布**：运行 `./mill unipublish[2.13].publishLocal`；完成后检查 `~/.ivy2/local/org.chipsalliance/` 下是否出现了 `$V` 目录。
5. **引用**：在一个独立 sbt 项目的 `build.sbt` 里写：
   ```scala
   // build.sbt
   scalaVersion := "2.13.18"   // 与 v.scala213MinorVersion 对齐
   val chiselVersion = "<把 $V 填这里>" // 例如 7.13.0+N-xxxxxxxx-SNAPSHOT
   addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full)
   libraryDependencies += "org.chipsalliance" %% "chisel" % chiselVersion
   ```
   然后 `sbt compile`，验证能解析到你刚发布的本地版本。

需要观察的现象：

- 第 2 步看到各子模块编译成功。
- 第 3 步 `$V` 末尾通常带 `-SNAPSHOT`（除非你 checkout 到了某个 tag）。
- 第 4 步本地仓库出现对应版本目录。
- 第 5 步新项目的 `sbt compile` 能成功解析到 `org.chipsalliance:chisel:$V`。

预期结果：你得到一份可被其他项目引用的本地 SNAPSHOT Chisel，并理解它「从 git 算出版本号 → 聚合多模块发布 → 落到本地 Ivy」的全过程。

> 待本地验证：若 `publishLocal` 因 ScalaDoc/网络问题失败，可先只验证到第 3 步（查版本）与第 2 步（编译）。整条链在 CI 环境与本地网络下表现可能不同。

> 进阶（可选）：如果你已读完 u1-l4，可以在新项目里写一个最小 `Adder` 模块，用 `ChiselStage.emitSystemVerilog` 打印 Verilog，确认本地发布的 Chisel 真的能产出 SystemVerilog——这就把「构建/发布」与「生成 Verilog」两端连通了。

## 6. 本讲小结

- Chisel 用 **mill** 构建，根目录的 `./mill` 是 wrapper，`build.mill` 是构建定义本体；`build.mill:1` 的 `mill-version` 头锁定了 mill 版本。
- `object v` 是版本与依赖的「单一事实来源」：Scala 交叉版本、firtool 版本（来自 `etc/circt.json`）、以及基于 git 的 `version`/`isSnapshot` 都在这里。
- 仓库拆成 `core`/`firrtl`/`macros`/`plugin`/`src`/`svsim` 等子模块；公共交叉编译逻辑抽到 `trait ChiselCrossModule`，`chisel` 通过 `moduleDeps` 依赖 `core`/`svsim`。
- 三条核心命令：`./mill chisel[].compile`（编译）、`./mill chisel[].test`（ScalaTest）、`./mill chisel[].unitTest`（firtool+circt-test 流水线，实现在 [build.mill:370-413](build.mill)）。
- firtool 由 `object circt` 按 `etc/circt.json` 版本自动下载（或用 `CIRCT_INSTALL_PATH` 跳过）；本地发布走聚合模块 `unipublish`，`./mill unipublish[2.13].publishLocal` 会同时发库与插件，落到 `~/.ivy2/local/org.chipsalliance/`。
- 发布版本号 = `v.version`（git 算出），引用时把该版本填进新项目的 `build.sbt` 即可。

## 7. 下一步学习建议

- 下一篇 **u1-l3「目录结构与子项目划分」** 会更细地展开每个子模块目录的内部结构（如 `core/src/main/scala`、`src/main/scala/chisel3/util` 的归属），与本讲的「多模块」概念直接衔接。
- 想立刻看到 Verilog 的读者，可跳到 **u1-l4「Hello Chisel：第一个模块与 Verilog 生成」**，结合本讲的「本地发布」实践，在独立项目里跑通 `ChiselStage.emitSystemVerilog`。
- 想理解 `unitTest` 背后那套测试机制的读者，记下 [build.mill:370-413](build.mill) 这段，等到 **第 9 单元（测试、诊断与二次开发）** 时会专门拆解 `chisel3.UnitTests` 与 FileCheck。
- 对构建细节感兴趣的读者，建议通读一遍 [build.mill](build.mill) 与 [release.mill](release.mill)：它们是「用 Scala 写构建」的很好范例，也是后续任何「改构建/加依赖」工作的入口。
