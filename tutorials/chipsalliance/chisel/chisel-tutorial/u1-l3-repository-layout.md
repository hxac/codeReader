# 目录结构与子项目划分

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 Chisel 仓库里**几个核心子项目**各自的名字、源码根目录和职责。
- 根据 `build.mill` 与每个子目录里的 `package.mill`，**读出子项目之间的依赖关系**（谁依赖谁）。
- 理解一条关键的代码归属原则：**什么样的代码应该放进 `core`，什么样的代码应该放进 `src/main`**。
- 自己动手画出一张「子项目依赖关系图」，并把每个子项目对应到仓库里的真实目录路径。

本讲只关心「代码放在哪儿、为什么放在那儿」，不深入任何子项目的内部实现——那是后面单元要做的事。

## 2. 前置知识

在开始前，请先确认你已经理解了上一篇（u1-l1）讲过的两个事实：

- **Chisel 不是一门独立语言，而是嵌在 Scala 里的硬件构造 DSL（EDSL）。** 你写的 `.scala` 文件在被 Scala 编译器编译的同时，还会被一个叫 `chisel-plugin` 的**编译器插件**做额外处理（比如给 `Bundle` 里的字段自动取名字）。
- **构建工具是 mill，不是 sbt。** 仓库根目录的 `./mill` 是一个会自动下载并启动 mill 的 wrapper，`build.mill` 是构建定义本体。

本讲会频繁提到两个构建概念，先用大白话解释：

| 概念 | 通俗解释 |
| --- | --- |
| **子项目（sub-project / compilation unit）** | 一个可以独立编译的代码单元。Chisel 把不同职责的代码拆成多个子项目，每个子项目各自编译，再通过依赖关系拼起来。 |
| **moduleDeps** | mill 里的一个字段，声明「本子项目编译时依赖哪些其它子项目」。它就是依赖图的「边」。 |
| **交叉版本（cross version）** | 同一份源码要同时支持 Scala 2.13 和 Scala 3，所以每个子项目都用 `Cross[...]` 为多个 Scala 版本各编译一次。 |

> 小提示：你会看到很多 `object cross extends Cross[Xxx](v.scalaCrossVersions)`，意思是「这个子项目对 `v.scalaCrossVersions`（即 2.13 和 3）各做一次交叉编译」。这行代码本身不是依赖关系，只是「编译多份」的声明。

## 3. 本讲源码地图

本讲主要看两个文件，外加每个子项目里定义自己的 `package.mill`：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 官方对子项目划分的权威说明，尤其是「Chisel Sub-Projects」和「Chisel Architecture Overview」两节。 |
| `build.mill` | 根构建文件，定义了版本/依赖的单一事实来源（`object v`）、公共 trait（`ChiselCrossModule`、`HasScalaPlugin` 等）以及最顶层的 `chisel`、`unipublish` 模块。 |
| `core/package.mill`、`plugin/package.mill`、`macros/package.mill`、`firrtl/package.mill`、`svsim/package.mill`、`stdlib/package.mill` | 每个子项目目录下的构建文件，定义该子项目自身的 `moduleDeps`（依赖边）。 |

需要强调：**仓库里只有一个根 `build.mill`**，每个子项目（`core/`、`plugin/`…）目录下放的是一个 `package.mill`。这是 mill 的「嵌套构建」写法——根 `build.mill` 提供公共定义，子目录的 `package.mill` 复用它们并补充各自细节。

## 4. 核心概念与源码讲解

本讲要覆盖的最小模块包括：**core（主体源码）、plugin（编译器插件）、macros（宏）、src/main（整合 + util）**，并补齐到 README 提到的全部子项目（再算上 `firrtl`、`svsim`、`stdlib`，一共 7 个带源码的子项目）。

### 4.1 构建文件的组织：build.mill 与 package.mill

#### 4.1.1 概念说明

Chisel 仓库体量很大，但构建定义却被刻意「收拢」成了两层：

- **根 `build.mill`**：放所有人共享的东西——Scala 交叉版本、所有 Maven 依赖、版本号算法、CIRCT(firtool) 的下载逻辑，以及若干公共 trait（`ChiselCrossModule`、`HasScalaPlugin`、`HasCommonOptions`）。
- **每个子目录的 `package.mill`**：只声明「我是谁、我依赖谁、我有哪些特殊编译选项」。

这种分层的好处是：你想知道某个子项目的依赖，直接打开它目录下的 `package.mill` 看 `moduleDeps` 即可，不用通读整个 `build.mill`。

#### 4.1.2 核心流程

识别一个子项目的三步法：

1. 在仓库里找到它的目录（如 `core/`）。
2. 打开该目录下的 `package.mill`，找到 `trait Xxx extends ...` 这一行——它列出了该子项目混入了哪些公共 trait（这决定了它是否自动挂载插件、是否开宏注解等）。
3. 在同一个 `package.mill` 里找 `override def moduleDeps = ...`——这一行就是它的依赖边。没有这行的子项目，就没有内部依赖。

#### 4.1.3 源码精读

根 `build.mill` 把版本与依赖集中在一个对象里，作为「单一事实来源」：

[build.mill:16-17](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L16-L17) —— `object v` 集中管理 Scala 版本、所有依赖坐标。所有 `package.mill` 都通过 `import build._` 引用它，避免版本号散落各处。

[build.mill:212-215](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L212-L215) —— 公共 trait `ChiselCrossModule`，注释里点名「它也会被 `firrtl` 和 `svsim` 混入」，是这些子项目共享的交叉版本基类。

而典型的子项目定义（以 `core` 为例）形如：

[core/package.mill:10-13](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/package.mill#L10-L13) —— `object 'package'` 里声明 `cross extends Cross[Core](...)`，表示 core 子项目按交叉版本编译。

#### 4.1.4 代码实践

**目标**：确认「根 `build.mill` + 各目录 `package.mill`」的布局。

1. 在仓库根目录执行 `ls *.mill`，确认只有一个根 `build.mill`。
2. 执行 `ls core/*.mill plugin/*.mill macros/*.mill`，确认每个子项目目录下是 `package.mill` 而不是 `build.mill`。
3. 打开任意一个 `package.mill`（例如 `macros/package.mill`），观察它的第一行 `package build.macros`——这正是 mill 把它归到 `build` 命名空间下的方式。

**预期结果**：你会看到根目录一个 `build.mill`，每个子项目目录各一个 `package.mill`，所有 `package.mill` 的 `package` 语句都以 `build.` 开头。

#### 4.1.5 小练习与答案

**练习**：为什么子项目不各自写一个完整的 `build.mill`，而要共用根 `build.mill` 里的 `object v`？

**答案**：因为 Scala 版本号、Maven 依赖坐标是全局共享的。集中到 `object v` 后，升级一个依赖只需改一处，所有子项目自动生效，避免版本漂移。

---

### 4.2 core 子项目：Chisel 的主体源码

#### 4.2.1 概念说明

`core` 是 Chisel **绝大部分源代码**的所在地。你在 Chisel 里天天用的那些类型和构造器——`Data`、`UInt`、`Bundle`、`Vec`、`Module`、`IO`、`Reg`、`Mem`、`when`——它们的实现都在 `core` 里。可以说：**`core` 定义了「Chisel 这门语言的核心词汇表」**。

它的源码根目录是 `core/src/main/scala/chisel3/`。

#### 4.2.2 核心流程

`core` 在依赖图里的位置：

- **依赖**：`firrtl` 和 `macros`（见下面的 `moduleDeps`）。
- **不依赖**：`plugin`、`svsim`、`src/main`、`stdlib`。
- **第三方依赖**：`os-lib`、`upickle`、`firtool-resolver`（用于在运行时定位/下载 firtool）。

也就是说，`core` 是一个相对「干净」的语言核心，它只依赖 FIRRTL IR 的 Scala 定义和宏基础设施。

#### 4.2.3 源码精读

README 对 `core` 的权威描述：

[README.md:361](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L361) —— `core` 是 Chisel 绝大部分源码所在，依赖 `firrtl`、`svsim` 和 `macros`。

> 注：README 这里写的是 `core` 依赖 `firrtl`/`svsim`/`macros`，是站在「最终用户拿到的 chisel 制品」角度的笼统说法；若严格看 `core/package.mill` 的 `moduleDeps`，`core` 直接依赖的是 `firrtl` 与 `macros`，而 `svsim` 是在更上层的 `chisel` 模块里引入的（见 4.5）。本讲后面画图时以 `package.mill` 的 `moduleDeps` 为准。

`core` 的依赖边定义在它自己的 `package.mill` 里：

[core/package.mill:19-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/package.mill#L19-L22) —— `moduleDeps` 声明 core 依赖 `firrtlModule` 和 `macrosModule`。

`core` 里那些核心类型的物理位置（供你建立直观印象，**待本地验证**具体行号会随版本变化）：

- `core/src/main/scala/chisel3/Data.scala` —— 所有硬件类型的根 `Data`。
- `core/src/main/scala/chisel3/Module.scala` —— `Module` 基类。
- `core/src/main/scala/chisel3/Aggregate.scala` —— `Bundle` / `Vec` / `Record`。
- `core/src/main/scala/chisel3/Bits.scala` —— `UInt` / `SInt` / `Bool`。

#### 4.2.4 代码实践

**目标**：用 `ls` 感受 `core` 的体量，确认它是「主体源码」。

1. 执行 `ls core/src/main/scala/chisel3/ | wc -l`，数一下 `chisel3` 包下有多少个 `.scala` 文件。
2. 执行 `ls core/src/main/scala/chisel3/`，找找 `Data.scala`、`Module.scala`、`Bits.scala`、`Aggregate.scala` 是否都在。

**需要观察的现象**：文件数量明显比其它子项目多（几十个），并且你能一眼看到那些最熟悉的类型名。

**预期结果**：你会看到 `core` 是所有子项目里源码最多的一个，印证它是「主体源码」。

#### 4.2.5 小练习与答案

**练习 1**：`core` 依赖 `macros`，但不依赖 `plugin`。请猜测原因。

**答案**：`macros` 提供的是编译期宏（如 `SourceInfoTransform`，给每个 API 调用注入文件名/行号），`core` 的源码里会**调用**这些宏，所以需要依赖 `macros`。而 `plugin` 是挂给「正在被编译的 Chisel 用户代码」用的编译器插件，`core` 自己编译时并不需要它。

**练习 2**：`core/package.mill` 里有 `def moduleDir = super.moduleDir / os.up`。结合 `core` 的源码确实在 `core/src/main/scala/` 下，这说明 mill 在定位源码时以什么为基准？

**答案**：以子项目目录（这里是 `core/`）为基准，源码遵循 `<子项目>/src/main/scala/` 的约定。`moduleDir / os.up` 是构建脚本内部用来定位仓库根路径的一种写法，不影响「源码就在 `core/src/main/scala/`」这一事实。

---

### 4.3 plugin 子项目：编译器插件

#### 4.3.1 概念说明

`plugin` 是一个 **scalac 编译器插件**，发布的制品名叫 `chisel-plugin`。它的作用是在 Scala 编译 Chisel 代码时，偷偷做一些「Chisel 专属」的改写——最典型的就是给 `Bundle` 子类里的每个 `val` 字段自动注入它的 Scala 变量名（这样生成的 Verilog 里才有可读的信号名），以及给每个 Chisel API 调用打上「标识符」标记。

它和普通子项目最大的不同：**它本身没有内部依赖**，而且它要为「每一个 Scala 小版本」单独编译（因为编译器插件必须和宿主 Scala 版本精确匹配）。

源码根目录：`plugin/src/main/scala-2/chisel3/internal/plugin/`（Scala 2 版本，另有 `scala-3/`）。

#### 4.3.2 核心流程

plugin 的特殊之处：

1. 制品名被改写为 `chisel-plugin`（而不是默认的 `plugin`）。
2. `crossFullScalaVersion = true`，意味着它不为「2.13 / 3」两个大版本编译，而是为**每一个小版本**（2.13.0…2.13.18、3.3.7、3.8.x 等）各编译一份。
3. 它不依赖任何其它 Chisel 子项目，只依赖 Scala 编译器本身的 jar。

#### 4.3.3 源码精读

README 对 `plugin` 的描述：

[README.md:364](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L364) —— `plugin` 是编译器插件，无内部依赖。

它的关键构建定义：

[plugin/package.mill:20](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/package.mill#L20) —— 把制品名改成 `chisel-plugin`。这正是你在用户项目的 `build.sbt` 里写 `addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % ...)` 时引用的名字。

[plugin/package.mill:25](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/package.mill#L25) —— `crossFullScalaVersion = true`，要求按完整 Scala 版本交叉编译（插件必须精确匹配 Scala 版本）。

它的实际源码文件（已在仓库中确认存在）：

- `plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala` —— 插件入口。
- `plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala` —— 给 `Bundle` 字段命名的组件。
- `plugin/src/main/scala-2/chisel3/internal/plugin/IdentifierComponent.scala` —— 标识符注入组件。

#### 4.3.4 代码实践

**目标**：理解「为什么用户项目必须同时加插件和库」。

1. 阅读 README 里给用户项目的 `build.sbt` 示例：[README.md:226-232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L226-L232)。注意它**同时**用了 `addCompilerPlugin(...chisel-plugin...)` 和 `libraryDependencies += ...chisel...` 两行。
2. 思考：如果只加库、不加插件，会发生什么？

**需要观察的现象**：示例里两行缺一不可。

**预期结果**：你会得出结论——`chisel` 库提供 API（来自 `core`/`src/main`），`chisel-plugin` 提供编译期改写，二者是分开发布的两个制品，用户必须同时引用。

#### 4.3.5 小练习与答案

**练习**：为什么 `plugin` 要 `crossFullScalaVersion = true`，而 `core` 只需按 `2.13`/`3` 两个大版本交叉？

**答案**：编译器插件直接介入 scalac 内部，对 Scala 版本极其敏感，一个小版本的不匹配就可能加载失败；而普通库只要二进制兼容即可，按大版本（2.13 / 3）交叉就够。

---

### 4.4 macros 子项目：宏

#### 4.4.1 概念说明

`macros` 收纳了 Chisel 用到的**编译期宏**。宏是一种「在编译时生成代码」的机制——比如你在 Chisel 里写 `RegNext(in)`，背后有一个宏在编译时悄悄把这个调用改写成「带上一份源信息（文件名 + 行号）的版本」，这样一旦出错，Chisel 能告诉你出错的是你源码的哪一行。

`macros` 和 `plugin` 一样**没有内部依赖**，但它和 `plugin` 的本质区别是：宏是通过 `import` + 正常编译链路生效的「白盒/黑盒宏」，而 `plugin` 是挂到 scalac 上的编译器插件。

源码根目录：`macros/src/main/scala-2/chisel3/internal/`。

#### 4.4.2 核心流程

- `core` 依赖 `macros`（因为 `core` 的 API 里会触发这些宏）。
- `macros` 不依赖任何 Chisel 子项目，只依赖 `scala-reflect`（Scala 2 下）。

#### 4.4.3 源码精读

README 对 `macros` 的描述：

[README.md:363](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L363) —— `macros` 是 Chisel 用到的大部分宏，无内部依赖。

构建定义：

[macros/package.mill:15-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/package.mill#L15-L24) —— `trait Macros`，注意它的 `moduleDeps` 没有被 `override`，即没有内部依赖；Scala 2 下额外引入 `scala-reflect`。

已确认存在的宏源码文件：

- `macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala` —— 注入源信息的核心宏。
- `macros/src/main/scala-2/chisel3/internal/naming/Identifier.scala` —— 与命名相关的宏。

#### 4.4.4 代码实践

**目标**：从依赖关系印证「`core` 依赖 `macros`」。

1. 对比两个文件：`core/package.mill`（有 `moduleDeps = ... macrosModule`）和 `macros/package.mill`（没有 `moduleDeps` override）。
2. 在 `core` 源码里随便挑一个文件，例如 `core/src/main/scala/chisel3/Reg.scala`，搜索它是否 `import` 了 `chisel3.internal.sourceinfo`（这是 `macros` 提供的包）。

**需要观察的现象**：`core` 能找到对 `macros` 包的引用，而 `macros` 内部不会引用 `core`。

**预期结果**：依赖方向是 `core → macros`，单向的。**待本地验证**：具体 `import` 行号请自行在 `Reg.scala` 中搜索确认。

#### 4.4.5 小练习与答案

**练习**：`macros` 和 `plugin` 都没有内部依赖，也都参与「编译期改写」。它们最关键的区别是什么？

**答案**：生效方式不同。`plugin` 通过 `-Xplugin` 挂到 scalac 上、对**所有**被编译的代码全局生效（用户必须在 `build.sbt` 里显式添加）；`macros` 是普通的 Scala 宏，通过 `import` 和隐式参数按需触发，`core` 把它当作正常依赖引入即可。

---

### 4.5 src/main 子项目：整合层 + util 标准库

#### 4.5.1 概念说明

`src/main`（在 `build.mill` 里定义为 `object chisel`，下文称 **`chisel` 模块**）是「把一切整合起来」的顶层子项目。它做两件事：

1. **整合**：把 `core`、`svsim` 拉到一起，并自动挂载 `plugin`，形成最终用户依赖的那个 `chisel` 库。
2. **util 标准库**：提供 `chisel3.util` 一系列可复用生成器（`Decoupled`、`Queue`、`Arbiter`、`Counter` 等）。

它也是 `unitTest`、`test` 等命令的落脚点——你在 u1-l2 里跑的 `./mill chisel[].compile`、`chisel[].test`、`chisel[].unitTest`，作用对象都是这个 `chisel` 模块。

源码根目录：仓库根下的 `src/main/scala/`（注意是仓库根的 `src/`，不是某个子目录里的）。

#### 4.5.2 核心流程

`chisel` 模块在依赖图里的位置非常关键：

- **依赖** `core` 和 `svsim`（见 `moduleDeps`）。
- **自动挂载** `plugin`（通过混入 `HasScalaPlugin` trait）。
- `core` 已经依赖了 `firrtl`/`macros`，所以 `chisel` 模块间接也用得到它们。

因此用户只要依赖 `chisel` 这一个制品，就同时获得了：core 的类型、util 的生成器、svsim 的仿真能力，以及配套的 plugin。

#### 4.5.3 源码精读

`chisel` 模块的定义和依赖：

[build.mill:303-305](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L303-L305) —— `object chisel extends Cross[Chisel]`，`trait Chisel` 混入了 `HasScala2MacroAnno`、`HasScalaPlugin` 和 `ScalafmtModule`。注意它混入了 `HasScalaPlugin`——这就是「自动挂载插件」的来源。

[build.mill:319](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L319) —— `moduleDeps = ... ++ Seq(coreModule, svsimModule)`，声明 chisel 模块依赖 `core` 和 `svsim`。

「自动挂载插件」的实现就在 `HasScalaPlugin` 里：

[build.mill:225-235](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L225-L235) —— `HasScalaPlugin` 在 `scalacOptions` 里追加 `-Xplugin:<plugin 的 jar 路径>`，从而在编译 `chisel` 模块自身时也挂上 `chisel-plugin`。

README 对 `src/main` 的描述：

[README.md:365](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L365) —— `src/main` 是「把一切整合起来」的 main，包含 `util` 库，依赖 `core`。

util 库的物理位置：`src/main/scala/chisel3/util/`（已确认包含 `Decoupled.scala`、`Queue.scala`、`Arbiter.scala`、`Counter.scala` 等）。

#### 4.5.4 代码实践

**目标**：亲眼确认 `chisel` 模块的依赖边和 util 库位置。

1. 打开 `build.mill` 第 319 行附近，确认 `moduleDeps` 里是 `coreModule` 和 `svsimModule`。
2. 执行 `ls src/main/scala/chisel3/util/`，确认 `Decoupled.scala`、`Queue.scala`、`Arbiter.scala` 在这里（而不是在 `core` 里）。
3. 执行 `ls src/main/scala/`，注意这里还有 `circt/stage/`、`chisel3/stage/`、`chisel3/simulator/`、`chiseltest/`——这些「整合型 / 流程型」代码都归在 `src/main`，不归 `core`。

**需要观察的现象**：util 和各种 stage/simulator 代码在 `src/main/scala`，而纯类型在 `core`。

**预期结果**：你会清晰看到 `core` 与 `src/main` 的分工——这正是下一节要讲的「代码归属原则」。

#### 4.5.5 小练习与答案

**练习**：为什么 `util`（`Queue`、`Arbiter` 等）放在 `src/main` 而不是 `core`？

**答案**：因为 `util` 是「纯 Chisel 代码」——它只用公开的 Chisel API 来组合出可复用生成器，不碰 `chisel3.internal` 的私有 API。按 Chisel 的归属原则，这类代码归 `src/main`，详见 4.7。

---

### 4.6 firrtl / svsim / stdlib：其余三个子项目

为了凑齐「7 个子项目」，还差三个：`firrtl`、`svsim`、`stdlib`。它们都已经在前面出现过，这里统一说明。

#### 4.6.1 概念说明

| 子项目 | 源码根目录 | 职责 | 内部依赖 |
| --- | --- | --- | --- |
| `firrtl` | `firrtl/src/main/scala/firrtl/` | 旧版 Scala FIRRTL 编译器的「残骸」，定义了 FIRRTL IR 的 Scala 数据结构（`firrtl.ir.*`），大部分最终会被吸收进 `core`。 | 无 |
| `svsim` | `svsim/src/main/scala/` | 一个底层库，负责编译并控制 SystemVerilog 仿真，后端为 Verilator 和 VCS。 | 无 |
| `stdlib` | `stdlib/src/main/scala/chisel3/std/` | 较新的「标准库」子项目（区别于 `src/main` 里的 `chisel3.util`），依赖 `chisel` 模块。 | `chisel` |

> 注意区分两个「标准库」概念：`chisel3.util`（在 `src/main` 里，最常用）和 `chisel3.std`（独立的 `stdlib` 子项目，较新）。不要混淆。

#### 4.6.2 核心流程

- `firrtl`：无内部依赖，是 `core` 的依赖之一。
- `svsim`：无内部依赖，是 `chisel` 模块的依赖之一。
- `stdlib`：依赖 `chisel` 模块（它是少数「建在 `chisel` 之上」的子项目）。

#### 4.6.3 源码精读

README 对 `firrtl` 和 `svsim` 的描述：

[README.md:362](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L362) —— `firrtl` 是旧 Scala FIRRTL 编译器的残骸。

[README.md:366](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L366) —— `svsim` 是控制 SystemVerilog 仿真的底层库，目标后端为 Verilator 与 VCS。

它们的构建定义（都没有 `moduleDeps` override，即无内部依赖）：

[firrtl/package.mill:15](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/package.mill#L15) —— `trait Firrtl extends ChiselCrossModule ...`，无 `moduleDeps` override。

[svsim/package.mill:15](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/package.mill#L15) —— `trait Svsim extends ChiselCrossModule ...`，无 `moduleDeps` override。

而 `stdlib` 是建在 `chisel` 之上的：

[stdlib/package.mill:15-20](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/stdlib/package.mill#L15-L20) —— `trait Stdlib extends HasScalaPlugin ...`，`moduleDeps = Seq(chiselModule)`，即依赖 `chisel`。

#### 4.6.4 代码实践

**目标**：确认这三个子项目的源码根目录。

1. 执行 `ls firrtl/src/main/scala/firrtl/`，看到 `ir/`、`passes/`、`Parser.scala`、`Emitter.scala` 等——这是旧 FIRRTL 编译器的结构。
2. 执行 `ls svsim/src/main/scala/`，看到 `Simulation.scala`、`Backend.scala`、`verilator/`、`vcs/`——这是仿真后端抽象。
3. 执行 `ls stdlib/src/main/scala/chisel3/std/`，确认它和 `src/main/scala/chisel3/util/` 是两套不同的东西。

**预期结果**：三个子项目的源码根目录与上表一致。

#### 4.6.5 小练习与答案

**练习**：`firrtl` 和 `svsim` 都没有内部依赖，`stdlib` 却依赖 `chisel`。从「职责」角度解释这个差异。

**答案**：`firrtl`/`svsim` 提供的是「底层基础设施」（IR 定义、仿真控制），它们不使用 Chisel 的用户 API，所以独立。`stdlib` 是用 Chisel 公开 API 写出来的可复用库，必然要建立在 `chisel` 之上，所以依赖 `chisel`。

---

### 4.7 子项目依赖关系与代码归属原则

#### 4.7.1 概念说明

把 4.2–4.6 的依赖边拼起来，就得到了 Chisel 的子项目依赖图。同时，Chisel 还有一条不成文但很重要的「代码归属原则」，决定了新代码该放进哪个子项目。

#### 4.7.2 核心流程：依赖图

下面这张图把所有 `moduleDeps` 边和 plugin 挂载关系可视化（箭头表示「依赖于」）：

```
   firrtl(无依赖)      macros(无依赖)
        │                   │
        └────────┬──────────┘
                 ▼
              ┌──────┐
              │ core │  (主体源码: Data/Module/Bundle/Vec/...)
              └──┬───┘
       ┌─────────┼──────────────┐
       │         │              │
       ▼         ▼              ▼
   svsim(无依赖)  ┌──────────────┐
                  │ src/main =   │
                  │ chisel 模块  │◄── 自动挂载 plugin (HasScalaPlugin, -Xplugin)
                  │ (整合 + util)│
                  └──────┬───────┘
                         │
                         ▼
                     ┌────────┐
                     │ stdlib │
                     └────────┘

   plugin (制品名 chisel-plugin): 独立编译, 无内部依赖, 被 chisel/stdlib 自动挂载
   unipublish: 把 chisel + stdlib + plugin 聚合成发布制品 "chisel"
```

对应的「边」一一可在源码里找到：

| 依赖边（A 依赖 B） | 源码出处 |
| --- | --- |
| `core → firrtl`, `core → macros` | `core/package.mill` 的 `moduleDeps` |
| `chisel(src/main) → core`, `chisel → svsim` | `build.mill` 第 319 行 |
| `chisel(src/main) → plugin`（编译器插件挂载） | `build.mill` 的 `HasScalaPlugin`（第 225–235 行） |
| `stdlib → chisel` | `stdlib/package.mill` 的 `moduleDeps` |
| `firrtl`、`macros`、`svsim`、`plugin` | 无内部依赖（各自 `package.mill` 没有 `moduleDeps` override） |

#### 4.7.3 核心流程：代码归属原则

README 用一句话概括了「代码该放哪儿」：

[README.md:368](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L368) —— 大意是：**会大量触及 `chisel3` 包内私有 API 的代码，应放进 `core`；而「纯 Chisel」代码（只用公开 API）应放进 `src/main`。**

这条原则解释了为什么：

- `Data`、`Module`、`Builder` 这些需要操作 `chisel3.internal.*` 私有状态的东西在 `core`。
- `Queue`、`Arbiter` 这些只用公开 API 拼出来的生成器在 `src/main` 的 `util`。
- `chisel3.std` 这种更新的、纯 API 组合的库，甚至单独成了 `stdlib` 子项目。

#### 4.7.4 代码实践（本讲核心实践，详见第 5 节）

请按第 5 节「综合实践」的要求，亲手画出这张依赖图，并把每条边对应到上表的源码行。

#### 4.7.5 小练习与答案

**练习**：假设你要给 Chisel 新增一个「只用 `Module`/`IO`/`Reg` 等公开 API 实现的 CRC 校验生成器」，应该放进 `core` 还是 `src/main`？为什么？

**答案**：放进 `src/main`（具体说可考虑 `util` 或 `stdlib`）。因为它只用到公开 API，不触及 `chisel3.internal` 私有状态，符合「纯 Chisel 代码归 `src/main`」的原则。放进 `core` 反而会让 `core` 不必要地膨胀。

## 5. 综合实践

**任务**：画一张「Chisel 子项目依赖关系图」，并标注每个子项目对应的目录路径，再为每条依赖边给出源码证据。

操作步骤：

1. 在一张纸或文本编辑器里，画出 7 个子项目节点：`core`、`firrtl`、`macros`、`plugin`、`src/main`(=`chisel` 模块)、`svsim`、`stdlib`。
2. 给每个节点标上它的源码根目录（参考第 4 节各小节的表格）。
3. 根据 `package.mill` / `build.mill` 的 `moduleDeps` 画箭头：
   - 打开 `core/package.mill`，把 `core → firrtl`、`core → macros` 画上。
   - 打开 `build.mill` 第 319 行，把 `chisel → core`、`chisel → svsim` 画上。
   - 打开 `stdlib/package.mill`，把 `stdlib → chisel` 画上。
   - 用一条**不同颜色**的线表示 `chisel → plugin`（这是 `-Xplugin` 挂载，不是普通 `moduleDeps`），证据在 `build.mill` 的 `HasScalaPlugin`。
4. 在每个无内部依赖的节点（`firrtl`、`macros`、`svsim`、`plugin`）旁边注明「无内部依赖」，并说明你是怎么确认的（它们的 `package.mill` 没有 `moduleDeps` override）。
5. 最后，在图旁边写一行：`unipublish` 把 `chisel + stdlib + plugin` 聚合成对外发布的 `chisel` 制品。

**需要观察的现象**：画完后，你应该能看到一个清晰的「下层基础设施（firrtl/macros/svsim/plugin）→ core → src/main(chisel) → stdlib」自底向上的分层。

**预期结果**：你得到的图与本讲 4.7.2 中的示意图一致，且每条边都能在源码里指到具体行号。

> 如果你想再验证一层，可以运行 `./mill show chisel[2.13].moduleDeps`（**待本地验证**该 mill 目标名），让 mill 自己把依赖输出出来，和你手画的图对照。

## 6. 本讲小结

- Chisel 的构建由一个根 `build.mill`（公共定义）加每个子目录的 `package.mill`（各自依赖与选项）组成。
- 7 个带源码的子项目：`core`（主体源码）、`firrtl`（旧 FIRRTL 残骸）、`macros`（宏）、`plugin`（编译器插件，制品名 `chisel-plugin`）、`src/main`（= `chisel` 模块，整合 + `util`）、`svsim`（仿真底层库）、`stdlib`（较新的标准库）。
- 依赖关系：`core → firrtl + macros`；`chisel(src/main) → core + svsim`，并自动挂载 `plugin`；`stdlib → chisel`；`firrtl/macros/svsim/plugin` 无内部依赖。
- `plugin` 比较特殊：它按完整 Scala 版本交叉编译，且通过 `HasScalaPlugin` 的 `-Xplugin` 被自动挂载到 `chisel`/`stdlib`，用户项目则需在 `build.sbt` 里 `addCompilerPlugin`。
- **代码归属原则**：触及 `chisel3` 私有 API 的代码归 `core`，纯 Chisel 代码归 `src/main`（或 `stdlib`）。
- 用户依赖的 `chisel` 制品，其实是由 `unipublish` 把多个子项目聚合而成的。

## 7. 下一步学习建议

本讲只回答了「代码放在哪儿」。接下来建议：

- 想看懂「这些子项目最终怎么被编译成 Verilog」，进入 **u1-l5（编译流程总览）**，它会串联起 `core` 的 Builder、内部 IR 和 `src/main` 里的 `circt.stage.ChiselStage`。
- 想亲手跑一个最小例子，先做 **u1-l4（Hello Chisel：第一个模块与 Verilog 生成）**，它会把本讲的目录结构落到一个能生成 Verilog 的具体命令上。
- 后续单元会逐层下沉：单元 2–3 讲 `core` 里的数据类型与模块系统，单元 4 讲 `core` 的 Builder/IR 内部机制，单元 5 讲 `src/main` 里的 Stage/CIRCT 管道，单元 6 讲 `util` 标准库——每个单元都对应本讲里的某个子项目目录。
