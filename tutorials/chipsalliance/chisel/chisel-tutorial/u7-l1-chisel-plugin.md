# ChiselPlugin 编译器插件

## 1. 本讲目标

本讲带你走进 Chisel 的「编译期魔法」来源——`chisel-plugin`。学完后你应该能够：

- 说清 `ChiselPlugin` 作为 scalac `-Xplugin` 注入点的角色，以及它注册了哪几个 `PluginComponent`。
- 理解 `BundleComponent` 如何在编译期为 `Record`/`Bundle` 自动生成 `cloneType`、`elements`、`_usingPlugin` 等方法，从而让 Bundle 字段自动获得 Scala 变量名。
- 理解 `ChiselComponent` 如何把源码里的 `val 名字 = ...` 在编译期改写成 `withName("名字")(...)`，实现零运行期成本的自动命名。
- 理解 `IdentifierComponent` 如何为 `Module` 生成模块定义名提案（`_moduleDefinitionIdentifierProposal`）。
- 能读懂插件源码的 AST 改写逻辑，并知道没有插件时 Chisel 会怎样崩溃。

## 2. 前置知识

阅读本讲前，你应当已经掌握（对应前置讲义 u1-l5、u4-l1）：

- **elaboration（细化）**：Chisel 在运行期执行你写的 Scala 构造体，调用 `chisel3.*` API「长出」电路（见 u1-l5）。
- **Builder 全局状态机**：细化期间 `Builder` 维护当前模块、命令队列等，前端 API「只登记不施工」（见 u4-l1）。
- **Scala 编译器插件（scalac plugin）**：scalac 允许在编译流程中插入自定义「阶段（phase）」，对程序的抽象语法树（AST）做改写。插件由 `-Xplugin:<jar路径>` 加载。
- **AST 与 quasiquote**：Scala 编译器内部用 `Tree` 表示代码节点；`q"..."` 是 quasiquote，能像写字符串模板一样构造/拼装 AST。
- **`cloneType` 与命名**：Chisel 需要为每个 `Data` 类型提供「克隆自身」的能力（`cloneType`），也需要给硬件信号分配可读名字，二者历史上靠手写或反射，现在由插件在编译期自动完成。

一句话直觉：**Chisel 把「读源码、抓变量名、生成样板代码」这些本该运行期靠反射做的事，前移到了编译期，靠 scalac 插件直接改写 AST。** 这就是为什么你写 `val foo = UInt(8.W)`，生成的 Verilog 里信号就叫 `foo`，而你从不需要手写 `cloneType`。

## 3. 本讲源码地图

本讲涉及的关键文件（全部位于 `plugin/` 子项目，目录 `plugin/src/main/scala-2/`，仅在 Scala 2 下生效）：

| 文件 | 作用 |
| --- | --- |
| `plugin/.../ChiselPlugin.scala` | 插件入口：实现 scalac `Plugin` trait，注册三个组件，解析命令行选项，做版本/文件守卫。 |
| `plugin/.../ChiselUtils.scala` | 组件共享工具：类型常量、`inferType`、`isABundle`/`isARecord`/`isData` 等谓词。 |
| `plugin/.../ChiselComponent.scala` | 组件一：改写 `ValDef`，把变量名编译期注入 `withName`/`prefix`；为 Module 注入源信息方法。 |
| `plugin/.../BundleComponent.scala` | 组件二：为 `Record`/`Bundle` 生成 `_cloneTypeImpl`、`_elementsImpl`、`_usingPlugin`、`_typeNameConParams`。 |
| `plugin/.../IdentifierComponent.scala` | 组件三：为 `Module` 生成 `_moduleDefinitionIdentifierProposal`（模块定义名提案）。 |

消费端（被插件生成的方法所服务的核心代码，位于 `core/`）：

| 文件 | 作用 |
| --- | --- |
| `core/.../Aggregate.scala` | `Record`/`Bundle`：声明 `_cloneTypeImpl`（默认抛异常）、`_usingPlugin`（默认 false）、`Bundle` 断言插件已运行。 |
| `core/.../naming/Identifier.scala` | `IdentifierProposer`：把构造参数转成合法标识符提案，供组件三调用。 |

## 4. 核心概念与源码讲解

### 4.1 ChiselPlugin：编译器插件的入口与守卫

#### 4.1.1 概念说明

scalac 插件模型有两个角色：

- **`Plugin`**：插件本体，被 scalac 在启动时加载。它对外暴露一个 `components: List[PluginComponent]`，即「我这个插件包含哪几个改写组件」。scalac 只认 `Plugin`，不直接认 `PluginComponent`。
- **`PluginComponent`**：一个挂在编译某个阶段（phase）之后运行的具体改写单元，通常 `runsAfter = "typer"`，即在类型检查完成后、对已类型化的 AST 做改写。

`chisel-plugin` 的 `Plugin` 类就叫 `ChiselPlugin`。它的职责很薄：

1. 声明插件名 `"chiselplugin"`。
2. 把三个组件（`ChiselComponent`、`BundleComponent`、`IdentifierComponent`）登记进 `components`。
3. 在 `init(options, error)` 里解析用户传给插件的命令行选项（如 `-P:chiselplugin:useBundlePlugin`），并对老选项给出「已是默认行为」的告警。
4. 通过伴生对象的 `runComponent` 做一道**前置守卫**：检查 Scala 版本与文件跳过名单，决定某个编译单元到底要不要跑插件。

#### 4.1.2 核心流程

```
scalac 启动
  └─ 加载 -Xplugin 指向的 jar，发现 ChiselPlugin
       ├─ init(options)：解析 -P:chiselplugin:<opt>，告警老选项
       └─ 注册 components = [ChiselComponent, BundleComponent, IdentifierComponent]
            └─ 每个组件 runsAfter="typer"
                 └─ 对每个编译单元，apply(unit) 调
                      ChiselPlugin.runComponent(...)(unit)
                        ├─ Scala 版本是否 2.12+ 且未被 skipFile？
                        │    ├─ 是 → 运行 transformer（返回 true）
                        │    └─ 否 → 跳过（返回 false），并 log 原因
```

注意：`runComponent` 是三个组件共用的守卫，所以「Scala 3 / Scala 2.11 / 被显式跳过的文件」一概不跑这三个组件（Scala 3 另有 `plugin/src/main/scala-3/` 的实现，本讲只讲 Scala 2 路径）。

#### 4.1.3 源码精读

插件本体与组件登记：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala:L50-L58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala#L50-L58) —— `class ChiselPlugin(val global: Global) extends Plugin`，`name = "chiselplugin"`，并把三个组件按序放进 `components` 列表。`global` 是 scalac 的全局编译环境，插件靠它访问类型表、符号表与 reporter。

选项解析 `init`：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala:L60-L85](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala#L60-L85) —— `init` 先对 Scala ≤ 2.13.8 发「Chisel 7 是最后支持版本」的告警，再逐个处理选项：`useBundlePlugin`/`genBundleElements` 现在都是默认行为（遇到则提示用户可移除该 scalacOption），`INTERNALskipFile:<路径>` 把文件加入跳过集合，未知选项则 `error`。

共用守卫 `runComponent`：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala:L25-L46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala#L25-L46) —— 拆分 Scala 版本号，要求主版本 `2` 且次版本 `>= 12`；再查 `arguments.skipFiles`。两项都过才返回 `true`，否则返回 `false` 并用 `global.log` 记录跳过原因（可用 `-Ylog:chiselbundlephase` 打开）。

`ChiselPluginArguments`：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala:L11-L19](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala#L11-L19) —— 一个 case class，集中存放各选项字符串常量与 `skipFiles` 可变集合。

补充：插件如何被挂到编译器上。在本仓库内部，构建定义 `HasScalaPlugin` 把 `plugin` 子模块产出的 jar 通过 `-Xplugin:` 注入：

[build.mill:L225-L235](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L225-L235) —— `scalacOptions` 追加 `-Xplugin:${pluginModule.jar().path}`，`scalacPluginClasspath` 追加该 jar。用户项目则用 `addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full)`（见 [README.md:L224-L232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L224-L232)），sbt 会把它展开成同样的 `-Xplugin`。

#### 4.1.4 代码实践

**实践目标**：确认插件确实参与了编译，并列出它注册的全部组件。

**操作步骤**：

1. 打开 [plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselPlugin.scala)，定位 `class ChiselPlugin` 的 `val components`。
2. 记下三个组件类的名字与它们各自的 `phaseName`（到各自文件里找：`chiselcomponent`、`chiselbundlephase`、`identifiercomponent`）。
3. 在 [build.mill](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill) 的 `HasScalaPlugin` 里确认 `-Xplugin:` 字符串。

**需要观察的现象**：三个组件都 `runsAfter = "typer"`，且共享 `ChiselPlugin.runComponent` 这一道守卫。

**预期结果**：你能写出一张「插件名 → 三个组件（phaseName）→ 各自 runsAfter=typer」的清单。

**待本地验证**：如果想看插件实际跑没跑，可在一个测试编译里加 scalac 选项 `-Ylog:chiselbundlephase`，观察日志是否出现 `runComponent` 决定的「Skipping / 运行」信息（需自行配置 scalacOptions，本讲不假定已运行）。

#### 4.1.5 小练习与答案

**练习 1**：`ChiselPlugin` 的 `components` 列表里组件顺序是 `ChiselComponent, BundleComponent, IdentifierComponent`。这三个组件都 `runsAfter = "typer"`，它们彼此之间有固定先后吗？顺序重要吗？

**答案**：三者都只声明对 `"typer"` 的依赖（`runsAfter`），彼此没有显式 `runsBefore`/依赖关系，因此具体先后由 scalac 在「typer 之后」这一组里调度，对本讲的功能而言不强相关——它们改写的 AST 节点（`ValDef` vs `Record` 类 vs `Module` 类）基本互不重叠。顺序对正确性不构成保证，不应依赖。

**练习 2**：为什么 `runComponent` 要专门检查 Scala 版本？

**答案**：因为这套 AST 改写是基于 Scala 2 的 `scala.tools.nsc` 内部 API 写的，Scala 2.11 及更早、以及 Scala 3 的编译器内部结构不同，插件无法工作。与其在构建里做复杂分支，插件直接在运行时判断：版本不匹配就静默跳过（返回 `false`），由 Scala 3 那套独立实现兜底。

---

### 4.2 ChiselComponent：编译期自动命名（withName / prefix）

#### 4.2.1 概念说明

Chisel 在 elaboration 时会给每个新建的硬件对象（`UInt`、`Wire`、`Module` 实例等）分配一个名字。理想情况下，这个名字应该等于你在源码里写的变量名（`val foo = Wire(...)` → 名字 `foo`）。但运行期反射拿变量名既慢又不可靠。

`ChiselComponent` 的做法是：**在编译期扫描每个 `val` 定义（AST 里的 `ValDef`），把它的变量名以字符串字面量的形式，包进对 `chisel3.withName(...)` 的调用**。这样到了运行期，`withName` 拿到的就是一个现成的字符串，无需反射。

它还处理两类相关改写：

- **前缀（prefix）**：当 `val` 命名的是一个「会产生子信号」的容器（如另一个 Module、或带 `AffectsChiselPrefix` 的对象）时，不仅给它命名，还用 `chisel3.experimental.prefix(name){...}` 给它内部新建的所有信号加一个前缀，于是 `val alu = Module(new ALU)` 内部的信号会带上 `alu_` 前缀。
- **源信息（source locator）**：为每个 `Module` 类注入一个 `_sourceInfo` 方法，记录该类源文件的路径与行列号，供错误定位使用。

#### 4.2.2 核心流程

```
对编译单元的 AST 做后序遍历（TypingTransformer.transform）
  └─ 命中 case ValDef(mods, name, tpt, rhs) 且通过 okVal 过滤
       ├─ 推断 tpt 的类型 tpe
       ├─ 是 Data 且直接位于 Bundle 体内？
       │    └─ rhs 改写为 chisel3.withName("name")(rhs)   // 只命名，不加前缀
       ├─ 是 Data / NamedComponent / AffectsChiselPrefix？
       │    └─ rhs 改写为 chisel3.withName("name")(prefix("name")(rhs))  // 命名 + 前缀
       │       （若 name 以 '_' 开头，前缀去掉首 '_' 以免 __ 双下划线）
       ├─ 是 Module / Instance？
       │    └─ rhs 改写为 chisel3.withName("name")(rhs)   // 只命名
       └─ 否则 → super.transform 原样下钻
  └─ 命中元组解构 val (a, b) = ...（okUnapply）
       └─ rhs 改写为 chisel3.withNames("a","b",...)(rhs)
  └─ 命中 Module 类定义 ClassDef
       └─ 注入 protected def _sourceInfo = SourceLine(path, line, column)
```

`okVal` 负责把不该命名的 `val`（构造参数、合成字段、抽象字段、`null`、空 rhs 等）过滤掉，避免误伤。

#### 4.2.3 源码精读

类型匹配谓词（决定一个 `val` 属于哪一类）：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala:L88-L105](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala#L88-L105) —— `shouldMatchData`、`shouldMatchNamedComp`、`shouldMatchModule`、`shouldMatchInstance`、`shouldMatchChiselPrefixed` 分别用「是否是某基类的子类型」来判断。注意 `shouldMatchNamedComp` 把 `Data`、`MemBase`、`VerificationStatement`、`DynamicObject`、`Disable`、`AffectsChiselName` 都算作「可命名组件」（注释解释：因为 `NamedComponent` 是 internal 的，插件只能匹配它的公开子类型）。

`okVal` 过滤：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala:L108-L130](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala#L108-L130) —— 排除带 `PARAM`/`SYNTHETIC`/`DEFERRED`/`CASEACCESSOR`/`PARAMACCESSOR` 等 flag 的定义，以及 `rhs` 为 `null` 或空树的情况。

核心改写 `transform`：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala:L185-L228](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala#L185-L228) —— 这是「自动命名」的真正落点。读法：

- `val str = stringFromTermName(name)`：从 `ValDef` 的 `name`（一个 `TermName`）取出字符串，`.trim()` 去掉 scalac 实现细节里尾随的空格。
- Bundle 内的 Data：`q"chisel3.withName($str)($newRHS)"`——只命名。
- 普通可命名/可前缀对象：先算 `prefix = if (str.head == '_') str.tail else str`（去首下划线），再 `q"...prefix.apply[$tpt](name=$prefix)(f=$newRHS)"`，若是 NamedComp 再套一层 `withName`。
- Module/Instance：只 `withName`，不加前缀。

`withName` 的运行期落点：

[core/src/main/scala/chisel3/package.scala:L490-L495](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L490-L495) —— `withName` 记录「调用前最后一个 `_id`」，执行 `nameMe`（此时新建了硬件对象），再把名字回填给这些新建的对象。这就是为什么「变量名」能在运行期被精确地绑到这个 `val` 新建的那个信号上。

源信息注入：

[plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala:L248-L274](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselComponent.scala#L248-L274) —— 命中 `Module` 的 `ClassDef` 时，用 `module.pos.source.file` 取源文件，经 `SourceInfoFileResolver.resolve` 解析路径，构造 `chisel3.experimental.SourceLine(path, line, column)`，并生成 `protected def _sourceInfo` 方法挂到类上。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「变量名 → CHIRRTL 信号名」的编译期注入效果，并验证前缀行为。

**操作步骤**：

1. 阅读测试 [src/test/scala/chiselTests/naming/NamePluginSpec.scala:L13-L49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/naming/NamePluginSpec.scala#L13-L49)。
2. 关注第一个用例：在一个 `Module` 里写 `{ val mywire = Wire(UInt(3.W)) }`（用花括号限制造一个内部作用域，让 `mywire` 不成为端口），断言 `emitCHIRRTL` 的输出包含 `"wire mywire :"`。
3. 再看 `"interact with prefixing"` 用例：`prefix("first"){ builder() }` 与 `prefix("second"){ builder() }`，FileCheck 断言分别出现 `wire first_wire :` 与 `wire second_wire :`。

**需要观察的现象**：源码里只写了 `val wire = Wire(UInt(3.W))`，没有任何 `withName` 调用，但生成的 CHIRRTL 里信号名就是 `wire`、`first_wire`、`second_wire`。

**预期结果**：理解到这正是 `ChiselComponent` 把 `val wire = ...` 在编译期改写成了类似 `val wire = chisel3.withName("wire")(chisel3.experimental.prefix("first")(Wire(UInt(3.W))))` 的形式。

**待本地验证**：运行该测试（`./mill chisel[].test -- NamePluginSpec`）观察是否通过；若环境无 firtool，则只做源码阅读理解。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Bundle 体内的 `val foo = UInt(8.W)` 只套 `withName`，而不像普通 Wire 那样额外套 `prefix`？

**答案**：Bundle 的字段本身就是叶子信号的名字来源，它不会「再产生一批需要带前缀的子信号」；而且 Bundle 字段在 `BundleComponent` 里还会被 `_elementsImpl` 单独收集。给它加前缀没有意义，反而会污染命名，所以这里只命名、不加前缀。

**练习 2**：`prefix` 计算时为什么对以 `_` 开头的名字做 `str.tail`（去掉首字符）？

**答案**：以 `_` 开头通常表示「这是一个临时信号」。用户的本意是标记临时性，而不是想把 `_` 当成前缀文本的一部分；Chisel 自身的命名算法也会用 `_` 表示临时。如果照搬，就会出现 `__foo` 这样的双下划线，故去掉首 `_`。

---

### 4.3 BundleComponent：自动 cloneType / elements / _usingPlugin

#### 4.3.1 概念说明

`BundleComponent` 是插件里功能最重的一个组件。它只改写「是 `Record` 子类型且非 abstract」的类定义（`Bundle` 是 `Record` 的子类）。它一次性完成四件事（这四件事在源码注释里写得明明白白）：

1. **`_usingPlugin = true`**（仅 Bundle）：给类打上「插件已处理过我」的标记。
2. **`_cloneTypeImpl`**：自动生成 `cloneType`——用主构造器重新 `new` 一个同类型实例，从而免去用户手写 `cloneType`。
3. **`_elementsImpl`**（仅 Bundle）：把类里所有「硬件字段」连同父类字段收集成一个 `Vector[(String, Any)]`，免去运行期反射发现字段。
4. **`_typeNameConParams`**：对混入了 `HasAutoTypename` 的 Record，导出构造参数列表，用于生成类型名。

为什么需要这些？因为 `Record` 在核心库里把 `_cloneTypeImpl` 声明成「默认抛异常」（详见消费端），**只有插件改写后才能正常工作**；而 `Bundle` 甚至在构造时直接 `assert(_usingPlugin, ...)`——也就是说，**没有插件，Bundle 根本无法实例化**。

#### 4.3.2 核心流程

```
transform 命中 ClassDef 且 isARecord && 非 abstract
  ├─ extractConArgs：找到主构造器及其参数访问器，对每个参数造 this.<ref>
  │    └─ 若参数是 Data，先 cloneTypeFull（克隆以保留方向、避免字段别名）
  ├─ generateAutoCloneType：new RecordType(构造参数克隆...) → 包成 _cloneTypeImpl 方法
  ├─ （仅 Bundle）generateElements：收集本类 + 父类的 public Data/Option[Data]/Seq[Data] 字段
  │    └─ 包成 _elementsImpl，返回 Vector((name, this.field), ...)
  ├─ （仅 Bundle）注入 override protected def _usingPlugin: Boolean = true
  └─ （仅 HasAutoTypename）generateAutoTypename：导出 _typeNameConParams
最后 deriveClassDef 把这些方法拼进类的 Template，重新 typed。
```

关键字段收集约定：`getAllBundleFields` 会**递归到父类**（深度 1 层），并把当前类的字段 `.reverse`——这与「先定义的字段位于高位」的约定一致（见 u2-l3）。

#### 4.3.3 源码精读

四项职责的官方说明：

[plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala:L12-L32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala#L12-L32) —— 类的 doc 注释列出四项操作；`phaseName = "chiselbundlephase"`，`runsAfter = "typer"`。

自动 cloneType：

[plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala:L85-L112](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala#L85-L112) —— `generateAutoCloneType` 用 `New(ttpe, conArgs)` 构造一个「用克隆后的构造参数 new 出新实例」的表达式，挂成 `protected def _cloneTypeImpl`。`cloneTypeFull` 见 [L45-L46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala#L45-L46)，调用 `DataMirror.internal.chiselTypeClone`，克隆的是**类型**（剥去硬件绑定），从而避免字段别名、保留方向信息。

自动 elements：

[plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala:L114-L178](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala#L114-L178) —— `generateElements` 的 `isBundleField` 判定一个成员是否算硬件字段：是访问器、且类型是 `Data` / `Option[Data]` / `Seq[Data]`（`Seq[Data]` 非法但容错传递，运行期再报错；若类混入 `IgnoreSeqInBundle` 则忽略）。收集结果 `.reverse` 后包成 `def _elementsImpl = Vector.apply[(String, Any)](...)`。

主变换 `transform`：

[plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala:L238-L270](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala#L238-L270) —— 依次产出 `cloneTypeImplOpt`、（Bundle 的）`elementsImplOpt`、（Bundle 的）`usingPluginOpt`（[L252-L257](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala#L252-L257) 注入 `_usingPlugin = true`）、`autoTypenameOpt`，再用 `deriveClassDef` 拼进模板。注意 `getConstructorAndParams` 还会**拦截用户手写** `cloneType`/`_cloneTypeImpl`/`_elementsImpl`/`_usingPlugin`/`_typeNameConParams`，直接 reporter.error（[L64-L79](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/BundleComponent.scala#L64-L79)）。

消费端：默认实现是「抛异常 / false」：

- [core/src/main/scala/chisel3/Aggregate.scala:L1184-L1188](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1184-L1188) —— `Record._cloneTypeImpl` 默认抛 `Internal Error! This should have been implemented by the chisel3-plugin.`。
- [core/src/main/scala/chisel3/Aggregate.scala:L1261-L1266](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1261-L1266) —— `abstract class Bundle` 构造体里 `assert(_usingPlugin, mustUsePluginMsg)`，消息提示「插件现在是必需的」。
- [core/src/main/scala/chisel3/Aggregate.scala:L1362](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1362) —— `Record._usingPlugin` 默认 `false`。
- [core/src/main/scala/chisel3/Aggregate.scala:L1297](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1297) —— `Bundle.elements` 由 `_processRawElements(_elementsImpl)` 计算，即直接消费插件生成的 `_elementsImpl`。

这条链路完整回答了「Bundle 字段为什么能自动获得 Scala 中的变量名」：字段名由 `ChiselComponent` 的 `withName` 注入，而「字段集合」本身由 `BundleComponent` 的 `_elementsImpl` 在编译期静态列出（不靠反射），二者协同。

#### 4.3.4 代码实践

**实践目标**：验证带构造参数的 Bundle 能被自动 clone，且无需手写 `cloneType`。

**操作步骤**：

1. 阅读测试 [src/test/scala/chiselTests/AutoClonetypeSpec.scala:L12-L33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/AutoClonetypeSpec.scala#L12-L33)，观察 `class BundleWithIntArg(val i: Int) extends Bundle { val out = UInt(i.W) }` 这类带参 Bundle、以及 `SubBundle` 继承 `BaseBundleVal` 的多层继承情形。
2. 注意：这些类**都没有手写 `cloneType`**，但它们能被 `IO(...)` / `Wire(...)` 正常克隆使用——因为插件为它们生成了 `_cloneTypeImpl`。
3. 在 `BundleComponent.scala` 的 `generateAutoCloneType` 里对照：构造参数 `i` 是 `Int`（非 Data），故走 `else select` 分支（原样引用）；若参数是 `Data`，则走 `cloneTypeFull` 克隆。

**需要观察的现象**：带 `val i: Int` 构造参数的 Bundle 在被 `Wire(new BundleWithIntArg(8))` 时不会崩溃，且字段宽度正确反映 `i=8`。

**预期结果**：理解 `_cloneTypeImpl` 生成的代码等价于 `def _cloneTypeImpl = new BundleWithIntArg(this.i)`——把当前实例的构造参数原样传给新实例。

**待本地验证**：可写一个最小模块 `Wire(new BundleWithIntArg(8))` 并 `emitCHIRRTL`，确认 `out` 字段为 8 位；若环境无 firtool，则只做源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：如果你在自定义 Bundle 里手写 `override def cloneType: MyBundle = ...`，会发生什么？

**答案**：编译期报错。`getConstructorAndParams` 会命中 `case d: DefDef if isNullaryMethodNamed("cloneType", d)`，调用 `global.reporter.error(...)`，消息为「Bundles cannot override cloneType. Let the compiler plugin generate it.」。插件独占这项职责，不允许用户插手。

**练习 2**：为什么 `extractConArgs` 对类型为 `Data` 的构造参数要做 `cloneTypeFull`，而对 `Int` 这样的参数直接原样引用？

**答案**：`Data` 参数是「硬件类型」，若直接把当前实例的字段引用传给新实例，会造成两个 Bundle 共享同一个硬件对象（字段别名），导致方向、绑定等信息错乱。`cloneTypeFull` 克隆的是「纯类型」（剥去硬件绑定），保证新实例各字段独立。`Int` 是普通 Scala 值，不可变且与硬件无关，原样传递即可。

**练习 3**：`Bundle` 类构造时 `assert(_usingPlugin, ...)`，这个断言失败意味着什么？

**答案**：意味着编译这个 Bundle 类时插件没有运行（例如项目没挂 `-Xplugin`，或被 `INTERNALskipFile` 跳过，或 Scala 版本不符）。此时 `_usingPlugin` 保持默认值 `false`，断言抛错，提示用户「The Chisel compiler plugin is now required」。这是一种 fail-fast：宁可立刻崩，也不让没插件的 Bundle 带病运行。

---

### 4.4 IdentifierComponent：模块定义名的生成

#### 4.4.1 概念说明

`IdentifierComponent` 处理的是**模块定义名（definition identifier）**——即生成的 Verilog 里 `module Foo_bar;` 这个名字怎么来。Chisel 允许模块带构造参数（如 `class Foo(val w: Int) extends Module`），并希望定义名能反映参数（如 `Foo_8`），便于在参数化设计中区分不同实例。

这个组件为每个 `Module` 类（非 trait、非匿名、非 `BaseModule` 本身）生成一个方法：

```scala
protected def _moduleDefinitionIdentifierProposal: String
```

它的方法体是：`IdentifierProposer.makeProposal(类名, getProposal(this.参数1), getProposal(this.参数2), ...)`——把类名和各构造参数的「提案」用 `_` 拼起来。

#### 4.4.2 核心流程

```
transform 命中 ClassDef 且 isAModule && !isExactBaseModule && 非 trait && 非 $anon
  ├─ generateIdentifierMethod：
  │    ├─ 取主构造器与参数访问器
  │    ├─ 第一个提案 = stringFromTypeName(module.name)  // 类名
  │    ├─ 对每个构造参数：this.<ref> → IdentifierProposer.getProposal(...)
  │    └─ 用 IdentifierProposer.makeProposal(..) 用 '_' 拼接
  └─ 把该方法挂进 Module 类的 Template
```

`IdentifierProposer.getProposal` 会智能处理：若参数本身是 `HasCustomIdentifier` 就用其自定义名；若是 `BaseModule` 就用它的 definition identifier（递归）；若是 `Iterable` 就展开；否则 `filterProposal(obj.toString)`，把任意对象的字符串描述清洗成合法标识符。

#### 4.4.3 源码精读

主变换与生成：

[plugin/src/main/scala-2/chisel3/internal/plugin/IdentifierComponent.scala:L82-L104](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/IdentifierComponent.scala#L82-L104) —— 命中 Module 类后调用 `generateIdentifierMethod`，再用 `deriveClassDef` 把 `_moduleDefinitionIdentifierProposal` 拼进模板。守卫条件排除了 trait、匿名类（`$anon`）和 `BaseModule` 本身（避免给基类生成）。

提案拼接逻辑：

[plugin/src/main/scala-2/chisel3/internal/plugin/IdentifierComponent.scala:L49-L80](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/IdentifierComponent.scala#L49-L80) —— `str = stringFromTypeName(module.name)`（类名），随后对主构造器每个参数 `vp`，从 `paramLookup` 找到对应字段符号，造 `this.<ref>`，包成 `IdentifierProposer.getProposal(this.<ref>)`，最后 `makeProposal(..)` 拼接。注意它把所有参数列表 `vparamss` 展平，并跳过 by-name 参数（见 [L37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/IdentifierComponent.scala#L37)，与 `ChiselPluginArguments` 同文件无关，是 `getConstructorAndParams` 的过滤）。

运行期提案算法：

[core/src/main/scala/chisel3/naming/Identifier.scala:L12-L56](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/naming/Identifier.scala#L12-L56) —— `IdentifierProposer` 是一个普通 Scala 对象（不是插件）。`filterProposal` 把任意字符串清洗成合法标识符（遇到 `@` 截断、非法字符替换为 `_`，如 `chisel3.internal.Blah@123412` → `chisel3_internal_Blah`）；`getProposal` 按类型分派；`makeProposal` 用 `_` 把非空提案拼接。插件生成的 `_moduleDefinitionIdentifierProposal` 方法体，运行期正是调到这些函数。

#### 4.4.4 代码实践

**实践目标**：理解「模块定义名 = 类名 + 构造参数提案」的生成路径。

**操作步骤**：

1. 在 [IdentifierComponent.scala:L49-L80](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/IdentifierComponent.scala#L49-L80) 找到 `generateIdentifierMethod`，确认方法体等价于 `makeProposal(类名, getProposal(this.参数1), ...)`。
2. 在 [Identifier.scala:L18-L42](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/naming/Identifier.scala#L18-L42) 读 `filterProposal`，手动推演：若一个 `Int` 参数值为 `8`，其 `toString` 是 `"8"`，`filterProposal("8")` 结果是 `"8"`；若参数是某个 `Module` 实例 `alu`，则会递归取它的 definition identifier。
3. 设想 `class Adder(val width: Int) extends Module`，实例化两次 `width=8` 与 `width=16`，推演各自的 `_moduleDefinitionIdentifierProposal` 返回值。

**需要观察的现象**：定义名能反映参数，且非法字符（如 `@`、括号）被清洗。

**预期结果**：`Adder(8)` → 提案 `"Adder_8"`；`Adder(16)` → `"Adder_16"`。两个不同参数的模块实例会得到不同的定义名。

**待本地验证**：可写一个带 `Int` 参数的 `Module` 子类，`emitSystemVerilog` 后查看生成模块名是否带参数后缀；若环境无 firtool，则只做源码推演。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `IdentifierComponent` 的守卫条件里要排除 `isExactBaseModule`（即 `BaseModule` 本身）和 trait？

**答案**：`BaseModule` 是所有模块的抽象基类，它本身不会被实例化成具体硬件，给它生成定义名无意义；trait 无法被独立实例化、也没有构造参数列表可言。两者都不该被改写。

**练习 2**：`makeProposal` 用 `_` 拼接各提案，并 `filter(_ != "")`。为什么需要过滤空字符串？

**答案**：`filterProposal` 在输入无法生成合法标识符时会返回空串（如纯符号字符串）。若不过滤，就会出现 `Foo__8`（连续下划线）或以 `_` 开头的非法名。过滤空提案保证拼接结果干净。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端追踪」：

**任务**：写一个最小的参数化设计，追踪插件在它身上做的全部改写。

```scala
// 示例代码（非项目原有代码）
import chisel3._
import chisel3.experimental.BaseModule

class MyBundle(val w: Int) extends Bundle {
  val data = UInt(w.W)
  val last = Bool()
}

class Pipe(val width: Int) extends Module {
  val io = IO(new Bundle {
    val in  = Input(new MyBundle(width))
    val out = Output(new MyBundle(width))
  })
  val reg = Reg(io.in.bits)
  io.out.bits := reg
  io.out.valid := io.in.valid
}
```

请完成：

1. **命名追踪（4.2）**：指出 `val reg = Reg(...)` 会被 `ChiselComponent` 改写成什么形式（写出等价的 `withName`/`prefix` quasiquote）；说明 `io` 这个 `val` 为何属于「Bundle 体内的 Data」之外的另一类（提示：它的类型是 `Bundle`，是 Data，但不在 Bundle 体内，而是在 Module 体内）。
2. **Bundle 改写追踪（4.3）**：`MyBundle` 会被 `BundleComponent` 注入哪几个方法？`_cloneTypeImpl` 生成的等价代码是什么（注意构造参数 `w: Int` 是非 Data）？为什么 `data` 字段能被收进 `_elementsImpl`？
3. **定义名追踪（4.4）**：`Pipe(width=8)` 的 `_moduleDefinitionIdentifierProposal` 会返回什么字符串？请写出 `makeProposal(...)` 的参数序列。
4. **无插件推演**：假设把 `-Xplugin` 去掉重新编译 `MyBundle`，运行期会在哪一行、以什么消息崩溃？（引用 [Aggregate.scala:L1261-L1266](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1261-L1266) 与 [L1184-L1188](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1184-L1188)）

**参考要点**：

1. `val reg` 等价于 `val reg = chisel3.withName("reg")(Reg(io.in.bits))`（Module 体内的 Data，命名 + 视情况前缀；`Reg` 结果是 Data 但不产生需前缀的子模块，故主要是命名）。`io` 是 Module 体内的 Bundle 类型 Data，按 `isData || isPrefixed` 分支处理（命名 + 前缀）。
2. 注入 `_cloneTypeImpl`、`_elementsImpl`、`_usingPlugin=true`。`_cloneTypeImpl` 等价 `new MyBundle(this.w)`。`data` 是 `UInt`（Data）的 public 访问器，命中 `isBundleField`，故被收进 `_elementsImpl`。
3. 返回 `"Pipe_8"`，参数序列为 `makeProposal("Pipe", getProposal(this.width))`，其中 `getProposal(8)` 经 `filterProposal("8")` = `"8"`。
4. 实例化 `MyBundle` 时 `assert(_usingPlugin, mustUsePluginMsg)` 先失败（`_usingPlugin` 保持默认 `false`）；即便绕过，调用 `cloneType` 时 `_cloneTypeImpl` 会抛「Internal Error! This should have been implemented by the chisel3-plugin.」。

## 6. 本讲小结

- `ChiselPlugin` 是 scalac `-Xplugin` 入口，登记三个 `PluginComponent`（`ChiselComponent`/`BundleComponent`/`IdentifierComponent`），它们都 `runsAfter="typer"`，并共用 `runComponent` 做 Scala 版本与跳过文件的守卫。
- `ChiselComponent` 在编译期把 `val 名字 = ...` 改写成 `withName("名字")(prefix(...)(...))`，把变量名以字符串字面量注入运行期，从而实现零反射的自动命名；同时为 Module 注入 `_sourceInfo` 源信息方法。
- `BundleComponent` 为 `Record`/`Bundle` 自动生成 `_cloneTypeImpl`（用克隆后的构造参数重新 new）、`_elementsImpl`（静态收集硬件字段，免去反射）、`_usingPlugin`（仅 Bundle）、`_typeNameConParams`（仅 `HasAutoTypename`）。
- 消费端 `Record._cloneTypeImpl` 默认抛异常、`_usingPlugin` 默认 false、`Bundle` 构造时 `assert(_usingPlugin)`——这套「默认即崩」的设计把插件变成了硬依赖。
- `IdentifierComponent` 为 Module 生成 `_moduleDefinitionIdentifierProposal`，把类名与各构造参数的提案用 `IdentifierProposer.makeProposal` 拼成模块定义名（如 `Pipe_8`）。
- 三个组件改写的 AST 节点基本不重叠（`ValDef` / `Record` 类 / `Module` 类），共用 [ChiselUtils.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/plugin/src/main/scala-2/chisel3/internal/plugin/ChiselUtils.scala) 的类型常量与谓词。

## 7. 下一步学习建议

- **下一讲 u7-l2（SourceInfo 宏与隐式源信息）**：本讲提到 `ChiselComponent` 为 Module 注入 `_sourceInfo`，而每个 Chisel API 调用处的文件名/行号则由 `SourceInfoTransform` 宏注入，二者共同构成 Chisel 的源信息体系，建议紧接着读。
- **u7-l3（Namer 与 Identifier 命名）**：本讲的 `withName`/`IdentifierProposer` 是命名信息的「产生端」，而 elaboration 后期 `Namer`/`Identifier`/`Namespace` 负责把这些提案落地为最终的 Verilog 信号名并去重，是天然的续篇。
- **延伸阅读**：`plugin/src/main/scala-3/chisel3/internal/plugin/BundlePhase.scala` 是 Scala 3 的等价实现（用 Scala 3 宏而非 scalac 内部 API），对比阅读能加深对「为什么 Scala 2 路径要单独存在」的理解。
