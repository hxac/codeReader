# Builder 全局状态机

## 1. 本讲目标

在前面的讲义里，我们反复强调一句话：模块构造体里的每一行（`IO`、`:=`、`when`、`Reg`）都「只登记不施工」。那么，**到底是谁在登记？登记到哪里？谁又负责把这些登记过的命令收拢成最终的电路？**

答案就是本讲的主角：`chisel3.internal.Builder`。它是整个 elaboration（细化）期间运行的全局状态机。

学完本讲，你应当能够：

- 说清 `Builder` 这个 `object` 在 elaboration 期间扮演的角色，以及它为什么必须用「全局可变状态」来实现。
- 列出 `DynamicContext` 持有的关键可变状态字段（`currentModule`、`components`、`whenStack`、`blockStack`、`errors` 等），并能区分「电路级状态」与「模块级状态」。
- 解释 `HasId` + `IdGen` 如何给每一个 Chisel 对象分配一个全局唯一的「身份证号」。
- 理解 `Namespace` 如何在命名时做去重，避免两个信号撞名。
- 跟踪一次 `Builder.build` 调用从「运行用户模块构造体」到「产出 `Circuit`」的完整流程。

本讲是单元 4（Builder 与 elaboration 内部机制）的入口，承接 u1-l5（编译流程总览）与 u3-l1（Module 生命周期），为后续 u4-l2（内部 FIRRTL IR）、u4-l3（Binding 系统）奠基。

## 2. 前置知识

本讲会用到几个 Scala 与软件工程概念，先用通俗语言解释：

- **elaboration（细化）**：运行你在 Scala 里写的硬件构造代码，让它「长出」一棵电路对象树的过程。详见 u1-l5。
- **全局可变状态（global mutable state）**：一段在程序运行期间一直存在、且任何代码都能读写的共享数据。Chisel 在 elaboration 时大量使用它，因为硬件构造代码天然是「顺序执行的指令式过程」，需要一个地方记账。
- **`DynamicVariable`**：Scala 标准库提供的「动态作用域变量」。它能把一个值绑定到一段代码块的执行上下文里，块内所有调用（哪怕跨函数）都能读到这个值，块结束后自动恢复。Chisel 用它来绑定「当前这一次 elaboration」的 `DynamicContext`。
- **`ThreadLocal`**：Java/Scala 提供的「每线程一份」的存储。Chisel 用它保证多线程下不同 elaboration 互不干扰。
- **`ArrayBuffer` / `LinkedHashSet`**：Scala 的可变集合，前者像动态数组（按插入顺序、可重复），后者是有序去重集合。

一个贯穿全讲的直觉：**Chisel 的 elaboration 本质上是一个「解释器」在跑你的 Scala 代码，而 `Builder` 就是这个解释器的「运行时环境（runtime environment）」**——就像 Python 解释器在执行脚本时维护的 `globals()`、调用栈、当前所在模块一样。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | 本讲核心。包含 `Builder` object、`DynamicContext`、`HasId`、`IdGen`、`Namespace`、`ChiselContext` 六大部件。 |
| [src/main/scala/chisel3/stage/phases/Elaborate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala) | 编译管道的 Elaborate 阶段。它 new 出 `DynamicContext`，再调用 `Builder.build` 启动细化。 |
| [core/src/main/scala/chisel3/Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) | `BaseModule` 抽象基类与 `Module.evaluate`。展示模块如何把自己挂到 `Builder.currentModule` 上。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | `Block` 类。命令（`Command`）真正存放的地方——注意，命令不在 `DynamicContext` 里，而在每个模块自己的 `_body: Block` 里。 |

> 提醒：所有永久链接里的 `b2a0e030da9a3e90b8221436b5317afb370f3347` 是当前 HEAD。后续若 HEAD 变化，需在 update 模式下刷新。

## 4. 核心概念与源码讲解

### 4.1 Builder：elaboration 期的全局状态机

#### 4.1.1 概念说明

`Builder` 是一个 `private[chisel3] object`（单例对象）。它本身**不存任何业务数据**，而是扮演两个角色：

1. **门面（facade）**：把 `DynamicContext` 里那一大堆可变字段，通过一堆 `def` 访问器暴露成「看起来像全局变量」的 `Builder.xxx` 调用，方便前端 API（如 `UInt` 运算、`when`、`Reg`）随时随地记账。
2. **生命周期管理者**：提供 `build` 入口，负责把一段「构造模块的代码」放进一个绑定了 `DynamicContext` 的作用域里执行，执行完再收尾组装成 `Circuit`。

为什么用全局状态？因为前端 API 是散落在各处的：`a + b` 在 `UInt.scala`、`when(...)` 在 `When.scala`、`Reg(...)` 在 `Reg.scala`。它们都需要往「当前模块的命令队列」里追加记录，而又不可能在每个函数签名里一路透传一个 context 参数（那会让用户 API 极其难看）。用 `Builder` 这个全局入口 + `DynamicVariable` 隐式传递 context，是最干净的折中。

#### 4.1.2 核心流程

```
用户调用 ChiselStage.emitSystemVerilog(new MyMod)
        │
        ▼
Phase 管道走到 Elaborate 阶段
        │  new DynamicContext(...)         ← 造一个空的「账本」
        │  Builder.build(Module(gen()), context)
        │           │
        │           ▼
        │  dynamicContextVar.withValue(Some(context)) {
        │      运行 Module(new MyMod)      ← 此后所有 Builder.xxx 都读到这个 context
        │          └─ 每条 IO/:=/when 都 pushCommand → 进入当前模块的 _body
        │      收尾：errors.checkpoint、组装 Circuit
        │  }
        ▼
返回 (ElaboratedCircuit, dut)
```

关键点：`Builder.build` 用 `dynamicContextVar.withValue(Some(dynamicContext))` 把 context 绑定到作用域，**绑定之后**才执行用户模块的构造体。所以构造体里的每一行都能通过 `Builder.pushCommand` 等访问器间接操作这个 context。

#### 4.1.3 源码精读

`Builder` 用一个 `DynamicVariable` 来持有「当前 context」：

[Builder.scala:569-573](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L569-L573) 定义了 `dynamicContextVar`，并强制「必须在 Builder 上下文内」才能取值——这就是为什么在 Scala REPL 里裸调用 `Wire()` 会报「must be inside Builder context」。

所有的「全局字段」其实都是对 `dynamicContext` 字段的转发。例如 `components`：

[Builder.scala:601](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L601) 一行 `def components: ArrayBuffer[Component] = dynamicContext.components`——`Builder` 自己不存 components，它去当前 context 里取。

最经典的「记账」入口是 `pushCommand` 与 `pushOp`：

[Builder.scala:895-903](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L895-L903)。`pushCommand` 把一条命令交给「当前用户模块」的 `addCommand`；`pushOp` 则先把结果 `Data` 绑定成 `OpBinding`（运算结果），再 `pushCommand`。这一段就是「只登记不施工」在源码里的具象：它只是往模块的 `Block` 里追加一条 `Command`，没有任何硬件综合发生。

而 `Builder.error` 展示了 elaboration 期的错误处理风格——**收集而非立即抛出**：

[Builder.scala:939-946](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L939-L946)。如果处在 Builder 上下文里，错误被塞进 `errors`（一个 `ErrorLog`）继续累积；只有不在上下文里（或显式要求 `throwOnFirstError`）才立即抛出。这让 Chisel 能一次性报告多处错误，而不是撞到第一个就停。

`build` 与 `buildImpl` 是入口：

[Builder.scala:1054-1062](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1054-L1062) 的 `build` 只是把执行包进 logger 的 scope；真正干活的是 [Builder.scala:1064-1092](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1064-L1092) 的 `buildImpl`：它用 `dynamicContextVar.withValue(Some(dynamicContext))` 绑定 context，然后执行传入的 `f`（即用户模块构造），期间捕获异常并调用 `errors.checkpoint(logger)` 把累积的错误冲刷出来。

收尾时组装顶层 `Circuit`：

[Builder.scala:1164-1176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1164-L1176)。这里把 `components`（所有细化出的模块）、`annotations`、layer/option/domain 集合等打包成一个 `Circuit`，并包成 `ElaboratedCircuit` 返回。注意 `components.last.name` 作为顶层电路名。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲眼看到 `Builder` 是「转发器」而非「存储者」。
2. **步骤**：
   - 打开 `Builder.scala`，定位 `object Builder`（约 563 行）。
   - 数一数形如 `def xxx = dynamicContext.xxx` 的访问器有多少个（`components`、`annotations`、`layers`、`currentModule`、`whenStack`……）。
   - 再定位 `pushCommand`（895 行）和 `pushOp`（899 行），确认它们调用的是 `forcedUserModule.addCommand(c)`，即命令最终进了**模块**，而不是 `Builder` 或 `DynamicContext` 本身。
3. **观察现象**：你会发现 `Builder` object 里几乎没有任何 `var/val` 业务字段（除了 `dynamicContextVar`、`suppressEnumCastWarning` 这类极少例外），全是对 `dynamicContext` 的转发。
4. **预期结果**：建立起「`Builder` = 门面 + 生命周期」、`DynamicContext` = 真正的账本、`Block` = 命令容器 三层心智。

#### 4.1.5 小练习与答案

**练习 1**：为什么前端 API（如 `UInt.+`）不需要把 context 当参数传来传去？
**答**：因为 `Builder` 用 `DynamicVariable` 把当前 `DynamicContext` 绑定在 elaboration 作用域里，任何 `Builder.xxx` 调用都会隐式读到同一个 context，等价于「隐式参数透传」，但用户 API 干净。

**练习 2**：如果在 Scala REPL 里直接敲 `val w = Wire(UInt(8.W))` 会发生什么？
**答**：会抛异常。因为此时没有进入 `Builder.build`，`dynamicContextVar.value` 是 `None`，`dynamicContext` 访问器的 `require(...)` 会抛出 "must be inside Builder context"。

---

### 4.2 DynamicContext：一次 elaboration 的全部可变状态

#### 4.2.1 概念说明

`DynamicContext` 是一个普通 `class`，每**一次** `Builder.build`（即每编译一个顶层电路）会 new 出**一个**实例。它就是这一整次 elaboration 的「账本」：当前在哪个模块、已细化出哪些模块、堆了多少层 `when`、累积了哪些错误……全部记在这里。

它和 `Builder` 的分工是：`Builder` 是永远存在的单例门面；`DynamicContext` 是「一次性」的、与具体那次 elaboration 绑定的数据。多线程下，每个线程各自 `withValue` 自己的 context，互不干扰。

> 还有一个更长寿的 `ChiselContext`（[Builder.scala:448-462](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L448-L462)），用 `ThreadLocal` 存储，放的是**跨 elaboration 也要保留**的状态，如全局 `IdGen`（见 4.3）、前缀栈。区分清楚：`DynamicContext` 是「这一趟细化」的，`ChiselContext` 是「这个线程」的。

#### 4.2.2 核心流程

`DynamicContext` 的字段分两类：

- **构造参数（只读 `val`）**：从注解/选项来，如 `useLegacyWidth`、`useSRAMBlackbox`、`warningFilters`、`sourceRoots`、`loggerOptions` 等，决定这次细化的「策略」。
- **可变状态（`var` / 可变集合）**：elaboration 过程中被不断读写，如 `currentModule`、`components`、`whenStack`、`blockStack`、`errors`。

一个**关键且容易搞错**的点：很多人以为命令（`Command`）也存在 `DynamicContext` 里，**其实不然**。`DynamicContext` 只有 `blockStack: List[Block]`（当前打开的 Block 栈）和 `components`（细化出的模块列表）。真正的命令序列挂在**每个模块自己的 `_body: Block`** 上（详见 u3-l1 与 4.1 的 `pushCommand`）。`blockStack` 只是「当前正在往哪个 Block 里追加命令」的指针栈。

#### 4.2.3 源码精读

构造参数列表：

[Builder.scala:465-483](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L465-L483)。可以看到 `annotationSeq`、`throwOnFirstError`、`useLegacyWidth`、`useSRAMBlackbox`、`warningFilters`、`sourceRoots`、`layerMap`、`suppressSourceInfo`、`elaborationTrace` 等都是 `val`——它们由 Elaborate 阶段从 `ChiselOptions` 读取后传入（见 4.5）。

可变状态字段（本讲最该背下来的一块）：

[Builder.scala:533-559](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L533-L559)。逐个点名：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `components` | `ArrayBuffer[Component]` | 已细化出的所有模块（按构造顺序），最后一个是顶层 |
| `annotations` | `ArrayBuffer[() => Seq[Annotation]]` | 用户用 `annotate()` 注册的注解（惰性求值） |
| `layers` / `options` / `domains` | `LinkedHashSet[...]` | 收集到的 layer/choice/domain 定义 |
| `currentModule` | `var Option[BaseModule]` | **当前正在构造的模块**——最重要的指针 |
| `unnamedViews` | `ArrayBuffer[Data]` | 无法映射到单一目标的 view，需要后续重命名 |
| `readyForModuleConstr` | `var Boolean` | 握手旗：确保 `Module()` 恰好构造一个模块（详见 u3-l1） |
| `whenStack` | `var List[WhenContext]` | 当前嵌套的 `when` 块栈 |
| `blockStack` | `var List[Block]` | 当前打开的命令块栈（命令真正追加到这里指向的 Block） |
| `currentClock` / `currentReset` | `var Option[Delayed[...]]` | 当前的隐式 clock/reset |
| `enabledLayers` / `layerStack` | ... | layer 相关栈 |
| `errors` | `ErrorLog` | 错误/警告收集器 |
| `namingStack` | `NamingStack` | 命名前缀栈（配合编译器插件，见 u7-l3） |
| `inDefinition` | `var Boolean` | 标记这次细化是否来自 `Definition` API |

注意 `globalNamespace` 与 `globalIdentifierNamespace`（[Builder.scala:514-515](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L514-L515)）也是 context 级的状态：前者给信号去重，后者给模块定义标识符去重（用 `$` 分隔）。

`currentModule` 的读写访问器：

[Builder.scala:705-711](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L705-L711)。读时若不在上下文返回 `None`，写时直接改 `dynamicContext.currentModule`。这是「进入一个子模块构造就把 currentModule 指过去，出来再恢复」的基础——而恢复由 `Builder.State` 快照机制完成。

`Builder.State`（快照/恢复）：

[Builder.scala:1230-1239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1230-L1239)。这是一个 case class，把 `currentModule`/`whenStack`/`blockStack`/`layerStack`/`prefix`/`clock`/`reset` 打包，配合 `State.save` / `State.restore` / `State.guard`（[Builder.scala:1241-1302](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1241-L1302)）实现「运行一段代码前后自动保存/恢复 Builder 状态」。`Module.evaluate` 就用它来隔离父子模块的状态（见 4.5）。

#### 4.2.4 代码实践（源码阅读型 + 可选运行）

1. **目标**：亲手列出 `DynamicContext` 持有的全部可变状态字段，并区分 `val`（策略）与 `var`（过程）。
2. **步骤**：
   - 打开 `Builder.scala` 第 465–560 行。
   - 用表格记录：字段名、`val` 还是 `var`、集合还是单值、它的用途。
   - 特别确认：**`DynamicContext` 里没有 `commands` 字段**。命令在 `_body: Block` 里。
3. **可选运行**（待本地验证，需 `./mill` 环境）：写一个最小模块并生成 CHIRRTL，对照输出里每个节点反推它走过了哪个字段。

   ```scala
   // 示例代码：仅用于观察，不是 Chisel 源码
   import chisel3._
   class Demo extends Module {
     val io = IO(new Bundle { val a = Input(UInt(8.W)); val y = Output(UInt(8.W)) })
     val r = RegUInt  // 占位，实际请用 RegNext(io.a)
     io.y := RegNext(io.a)
   }
   // println((new chisel3.stage.ChiselStage).emitCHIRRTL(new Demo))
   ```

   （上面 `RegUInt` 行为示意，正确写法见预期结果。）
4. **观察现象**：CHIRRTL 文本里会出现 `reg` / `node` / `connect` 等节点。
5. **预期结果**：你能把每条 `connect` 对应到一次 `pushCommand(Connect(...))`、把每个 `reg` 对应到一次 `pushCommand(DefReg(...))`，并理解它们都进了顶层模块的 `_body`，而模块本身被收进 `components`。

#### 4.2.5 小练习与答案

**练习 1**：`DynamicContext` 与 `ChiselContext` 各自的存活范围是什么？
**答**：`DynamicContext` 只存活于一次 `Builder.build`（一个电路的细化）；`ChiselContext` 用 `ThreadLocal` 存储，存活于整个线程，跨多次 elaboration 保留 `IdGen` 等状态。

**练习 2**：为什么 `currentModule` 是 `var` 而 `components` 是 `val`（但内部 `ArrayBuffer` 可变）？
**答**：`currentModule` 指针随「进入/离开子模块」频繁改写，故用 `var`；`components` 这个**引用**一旦创建不变（`val`），但我们要往这个集合里追加新模块，所以用可变的 `ArrayBuffer`。

---

### 4.3 HasId 与 IdGen：每个硬件对象的「身份证」

#### 4.3.1 概念说明

elaboration 期会诞生海量的对象：每一个 `UInt`、`Wire`、`Reg`、端口、模块都是一个 Scala 对象。Chisel 需要一种方式把它们彼此区分开、并能追溯「它属于哪个父模块」。这就是 `HasId` trait 的职责。

`HasId` 给每个对象两样东西：

- **`_id: Long`**：一个全局单调递增的编号，由 `IdGen` 分配。这是「对象身份证号」，用于稳定排序与去重（`getRecursiveFields` 等地方靠它保证遍历顺序确定）。
- **`_parent: Option[BaseModule]`**：它所属的父模块。构造对象时立刻读取 `Builder.currentModule` 记下。

`Data`、`BaseModule`、`MemBase` 等都混入 `HasId`，所以「所有能被命名的硬件对象」都有身份证。

#### 4.3.2 核心流程

```
new 一个 HasId 子类对象（如 Wire(UInt(8.W))）
   ├─ _id = Builder.idGen.next     ← 领一个新号码（自增）
   ├─ _parent = Builder.currentModule  ← 记下「我属于谁」
   └─ 之后由 Namer / _forceName 分配可读名字（见 4.4 与 u7-l3）
```

`IdGen` 极其简单：一个从 `-1` 开始的计数器，每次 `next` 自增并返回。

\[ id_n = n,\quad n = 0, 1, 2, \dots \]

第一个领号的对象拿到 `0`，之后严格递增，因此**任意两个对象的 `_id` 必不相同**——这就是「全局唯一身份证」的来源。

#### 4.3.3 源码精读

`IdGen`：

[Builder.scala:107-114](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L107-L114)。`counter` 从 `-1` 起，`next` 先 `+=1` 再返回，故首值为 `0`；`value` 返回当前值（不自增）。

`IdGen` 实例**不在** `DynamicContext` 里，而在长寿的 `ChiselContext`：

[Builder.scala:450](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L450) 把 `idGen` 放进 `ChiselContext`；[Builder.scala:593](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L593) 通过 `Builder.idGen` 访问。这意味着编号在整个线程的多次 elaboration 间持续累加，避免跨电路重用编号造成混淆。

`HasId` 的关键字段：

[Builder.scala:116-128](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L116-L128)。`_parentVar` 在构造时取 `Builder.currentModule.getOrElse(null)`（为省内存用 nullable var）；`_id` 在 [Builder.scala:124](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L124) 取 `Builder.idGen.next`。注意它还特意 `override hashCode/equals` 为 `super` 版本——这是为了**关闭 Scala 的 `==`/`##` 比较走 case-class 内容相等**的默认行为，强制用对象身份（引用）比较，避免两个不同 Wire 因为字段相同就被当成相等。

`HasId` 还承担命名：`autoSeed` / `suggestName` / `_forceName`（[Builder.scala:154-252](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L154-L252)）。命名的「种子（seed）」先记进 `suggested_seed`，等 `generateComponent` 收口时再由 `_forceName` 配合 `Namespace` 落实成最终名字（详见 4.4）。这部分会在 u7-l3（Namer 与 Identifier）深入。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解每个 Chisel 对象都有 `_id` 与 `_parent`。
2. **步骤**：
   - 打开 `Builder.scala` 第 116–128 行，确认 `HasId` 是 `trait`，混入 `chisel3.InstanceId`。
   - 用 IDE/grep 找出谁混入了 `HasId`（`Data`、`BaseModule`、`MemBase` 等）。
   - 看 `_id` 的赋值时机（构造即领号），并解释为什么用 nullable `_parentVar` 而非 `Option`（注释写了「for better memory usage」）。
3. **观察现象**：在大电路里 `_id` 数量会非常大，所以源码处处用「nullable var + `Option(...)` 包装」来省内存。
4. **预期结果**：能复述「构造即领号、领号即记父」的约定。

#### 4.3.5 小练习与答案

**练习 1**：两个不同的 `Wire(UInt(8.W))` 的 `_id` 会相同吗？为什么 `HasId` 要禁用内容相等的 `equals`？
**答**：不会相同，`IdGen` 严格自增。禁用内容相等是为了防止「两个 8 位 Wire」被误判为同一个对象——硬件信号只能用引用身份区分。

**练习 2**：为什么 `IdGen` 放在 `ChiselContext`（线程级）而不是 `DynamicContext`（细化级）？
**答**：因为同一进程/线程可能连续细化多个电路，全局累加编号可以避免跨电路复用编号，保持全进程唯一。

---

### 4.4 Namespace：信号命名的去重器

#### 4.4.1 概念说明

elaboration 出来的信号需要可读且**不撞名**的 Verilog 名字。可读由 Namer 负责（u7-l3），而不撞名就靠 `Namespace`。它本质上是一个「已用名字集合 + 冲突时自动加后缀」的工具。

`Namespace` 有两个用途：

1. **每个模块一个 `_namespace`**（[Module.scala:559](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L559)）：保证模块内信号名唯一。
2. **context 级 `globalNamespace`**：保证模块名、类型别名等全局唯一。

它还内置了关键字保护——构造时可传入一组保留字（如 Verilog 关键字），它们会被预先占用，从而永远不会被分配给用户信号。

#### 4.4.2 核心流程

调用 `namespace.name(candidate)`：

```
sanitize(candidate)            ← 先做合法化（去非法字符）
  ↓
getIndex(sanitized)            ← 查这个名字是否已被占用
  ├─ 未占用 → 记录并直接返回原名
  └─ 已占用 → rename(name, idx)  ← 递归试 name_idx、name_(idx+1)… 直到不撞
```

例如两个信号都想叫 `data`：第一个拿到 `data`，第二个拿到 `data_1`，第三个 `data_2`……

#### 4.4.3 源码精读

`Namespace` 类与冲突重命名：

[Builder.scala:31-51](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L31-L51)。内部用一个压缩的 `HashMap[String, Long]`（不是每个名字都存一条，重名只存一条 + 计数，省内存）。`rename` 是个 `@tailrec` 递归：拼出 `name${separator}${index}`，若仍冲突就 `index+1` 再试。默认分隔符是 `_`。

对外的主入口 `name`：

[Builder.scala:89-97](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L89-L97)。先 `sanitize`，再 `getIndex`：命中（`Some`）就 `rename`，未命中（`None`）就登记为 `1` 并返回原名。`leadingDigitOk` 参数是给 `Record` 字段名用的（字段名允许以数字开头，因为 FIRRTL 字段语义不同）。

`getIndex` / `prefix` 的精巧之处（[Builder.scala:55-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L55-L86)）：因为 HashMap 是压缩存储，判断「`data_2` 是否被占用」不能只查 map——还要看它的前缀 `data` 占到第几号。`prefix` 用手写的 while 循环（注释说「micro-optimized because it runs on every single name」）检测名字是否以 `_\d+` 结尾。

`HasId._forceName` 是 `Namespace` 的主要消费者：

[Builder.scala:217-252](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L217-L252)。它把候选名交给 `namespace.name(candidate)` 得到去重后的可用名，再 `setRef`。如果开 `errorIfDup` 且名字被改了（说明撞名），还会调 `Builder.error` 提示用户用 `suggestName` 起个更独特的名字。

#### 4.4.4 代码实践（运行型，待本地验证）

1. **目标**：观察撞名时 `Namespace` 自动加后缀。
2. **步骤**：写一个模块，故意让两个中间信号落到相同的建议名（例如在一个函数里构造两个未赋给 `val` 的 `Wire`，或两个同名临时节点），生成 Verilog/CHIRRTL。
   ```scala
   // 示例代码
   import chisel3._
   class Collision extends Module {
     val io = IO(new Bundle { val a = Input(UInt(8.W)); val y = Output(UInt(8.W)) })
     def tmp = WireDefault(io.a)      // 多次调用同名辅助
     io.y := tmp + tmp
   }
   // println((new chisel3.stage.ChiselStage).emitSystemVerilog(new Collision))
   ```
3. **观察现象**：生成的 Verilog 里会看到形如 `_tmp` 与 `_tmp_1`（或 `tmp_T` 之类）的不同节点名。
4. **预期结果**：尽管用户代码用了同一个名字种子，`Namespace` 保证了最终名字唯一。具体命名取决于 Namer 与编译器插件，**后缀数字以实际输出为准（待本地验证）**。

#### 4.4.5 小练习与答案

**练习 1**：`Namespace` 内部为什么用「压缩 HashMap」（重名只存一条 + 计数）而不是一个 `HashSet`？
**答**：为了省内存——大电路里名字极多，把 `data_1, data_2, …, data_N` 压缩成一条 `data -> N` 显著降低开销，代价是查询时要额外算前缀。

**练习 2**：模块级 `_namespace` 与 context 级 `globalNamespace` 各自管什么名字？
**答**：模块级 `_namespace` 管模块内的信号/节点名；`globalNamespace` 管模块名、类型别名等需要全局唯一的标识。

---

### 4.5 串起来：从 Elaborate 到 Builder.build 的执行流程

#### 4.5.1 概念说明

前面四个模块分别讲了部件，本节把它们串成一条完整链路：编译管道的 **Elaborate 阶段**如何造出 `DynamicContext`、调用 `Builder.build`，而 `Module(new MyMod)` 又如何在这个过程中把命令灌进 Builder。

这条链路是理解「一行 Scala 代码如何变成 IR 节点」的关键，也为 u4-l2（内部 FIRRTL IR）铺路。

#### 4.5.2 核心流程

```
[Elaborate.transform]  收到 ChiselGeneratorAnnotation(gen)
   │
   ├─ 从 annotations 用 view[ChiselOptions] 读出所有选项
   ├─ new ElaborationTrace
   ├─ new DynamicContext(annotations, throwOnFirstError, useLegacyWidth,
   │     ..., elaborationTrace)            ← 造账本，策略字段就位
   │
   ├─ Builder.build(Module(gen()), context)
   │     │  (buildImpl 内)
   │     ├─ dynamicContextVar.withValue(Some(context)) { ... }  ← 绑定 context
   │     ├─ 执行 Module(gen())
   │     │     └─ Module._applyImpl → evaluate(bc)
   │     │           ├─ Builder.State.guard(State.default){ ... }  ← 保存/恢复状态
   │     │           ├─ Builder.readyForModuleConstr = true
   │     │           ├─ 运行 bc（用户模块构造体）
   │     │           │     └─ 每条 IO/:=/when/Reg → pushCommand → 模块 _body
   │     │           ├─ module.generateComponent()  ← 收口：命名、关闭模块、产出 DefModule
   │     │           └─ Builder.components += component
   │     ├─ errors.checkpoint(logger)        ← 冲刷累积错误
   │     └─ 组装 Circuit(components, ...) → ElaboratedCircuit
   │
   └─ 返回 Seq(ChiselCircuitAnnotation(elaboratedCircuit), DesignAnnotation(...))
```

#### 4.5.3 源码精读

**Elaborate 阶段**：[Elaborate.scala:39-66](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L39-L66)。

它对每个 `ChiselGeneratorAnnotation(gen)` 用 `view[ChiselOptions]` 抽取选项，new 出 `ElaborationTrace` 与 `DynamicContext`：

[Elaborate.scala:44-66](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L44-L66)。注意第 65 行 `Builder.build(Module(gen()), context)` 就是整条链的扳机——`Module(gen())` 在这里被求值（即触发 elaboration）。异常会被 `trimStackTraceToUserCode()` 裁剪堆栈后再抛（[Elaborate.scala:81-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L81-L86)），让报错指向用户代码而非 Chisel 内部。

**Module.evaluate**：[Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108)。

它把模块构造包进 `Builder.State.guard(Builder.State.default){ ... }`——这意味着进入子模块时把 Builder 状态重置为默认（`currentModule` 等会在构造中重新设置），出来后恢复父模块状态。关键三步：

1. [Module.scala:72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L72) 置 `readyForModuleConstr = true`，允许紧随其后的 `BaseModule` 构造体通过校验。
2. 运行 `bc`（用户的 `new MyMod`），构造体里每一行命令进入模块 `_body`。
3. [Module.scala:92-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L92-L95) 调 `generateComponent()` 收口，把产出的 `component` 追加进 `Builder.components`。

**BaseModule 构造时把自己挂上**：[Module.scala:486-498](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L498)。

[Module.scala:494-497](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L494-L497) 在 `BaseModule` 主构造体里：校验 `readyForModuleConstr`、把它清回 `false`（防止一次 `Module()` 构造两个模块），然后 `Builder.currentModule = Some(this)` 并把 `_body` 压入 `blockStack`。**这一行就是「当前模块指针」被设置的瞬间**——此后该模块构造体里 `pushCommand` 都会落到它头上。

每个模块自带的容器：[Module.scala:559](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L559)（`_namespace`）、[Module.scala:563](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L563)（`_ids`），以及 [Module.scala:468](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L468) 的 `_body: Block`。

**命令真正落地的容器 `Block`**：[IR.scala:406-422](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L406-L422)。

`Block.addCommand` 把命令追加进 `_commandsBuilder`；`close()` 后转为不可变的 `_commands`。这印证了 4.2 的结论：**命令不在 `DynamicContext`，而在模块的 `_body` Block 里**。`DynamicContext.blockStack` 的栈顶就是「当前正在填充的那个 Block」。

#### 4.5.4 代码实践（综合源码追踪）

1. **目标**：用一个最小模块，把本讲五个部件（Builder / DynamicContext / HasId+IdGen / Namespace / build 入口）全部串一遍。
2. **步骤**：
   - 准备一个最小模块（如 u1-l4 的 `Adder`）。
   - 按下面的「断点清单」依次在源码里定位，说明每一步发生在哪个文件的哪一行：
     1. `Elaborate.transform` new 出 `DynamicContext`（[Elaborate.scala:46-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L46-L63)）。
     2. `Builder.build` 绑定 context（[Builder.scala:1068](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1068)）。
     3. `Module.evaluate` 置旗 + State 守卫（[Module.scala:72-77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L72-L77)）。
     4. `BaseModule` 构造把 `currentModule` 指向自己（[Module.scala:496](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L496)）。
     5. `io.y := RegNext(io.a)` 里的 `:=` 与 `RegNext` 各自 `pushCommand`（[Builder.scala:895](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L895)）→ 落进 `Block.addCommand`（[IR.scala:419](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L419)）。
     6. `generateComponent` 收口、`Builder.components += component`（[Module.scala:92-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L92-L95)）。
     7. `buildImpl` 组装 `Circuit`（[Builder.scala:1164-1176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1164-L1176)）。
   - （可选，待本地验证）用 `emitCHIRRTL` 打印 IR，把每个 `node`/`connect`/`reg` 对应到上面第 5 步的一次 `pushCommand`。
3. **观察现象**：你会看到「状态指针的推进」与「命令的追加」严格交错：进入模块→改 `currentModule`→灌命令→收口→追加进 `components`。
4. **预期结果**：能画出一张时序图，标注 `currentModule`、`blockStack`、`components` 在每一步的变化。

#### 4.5.5 小练习与答案

**练习 1**：如果用户在 `Module(new MyMod)` 的构造体里又写了一个 `Module(new SubMod)`，`currentModule` 会怎样变化？
**答**：构造 `SubMod` 时，`BaseModule` 主构造体把 `Builder.currentModule` 指向 `SubMod`；`SubMod` 收口后，`Module.evaluate` 的 `State.guard` 会把 `currentModule` 恢复成父模块 `MyMod`，继续填充 `MyMod` 的命令。

**练习 2**：`Builder.build` 为什么要用 `dynamicContextVar.withValue(...)` 而不是直接把 context 赋给一个 `var`？
**答**：`DynamicVariable` 提供「作用域绑定 + 自动恢复」语义，能正确处理嵌套 elaboration 与异常退出后的状态回收；直接用 `var` 无法在跨函数调用链里安全地恢复旧值，也不支持多线程隔离。

## 5. 综合实践

**任务：给 Builder 画一张「运行时环境」全景图，并用一个三模块电路验证它。**

要求：

1. 写一个顶层模块 `Top`，内部实例化两个子模块 `Adder` 与 `Reg`（寄存器），顶层把 `Adder` 的输出接到 `Reg` 的输入。
2. 生成 CHIRRTL 与 SystemVerilog。
3. 对照本讲内容，在一张图里标注（可手绘或文字描述）：
   - 这一次 elaboration 的 `DynamicContext` 在何时被 new（`Elaborate`）。
   - `components` 里最终有几个 `Component`（应该是 3 个：`Adder`、`Reg`（如与你自定义模块名冲突请改名）、`Top`，顺序如何）。
   - `currentModule` 在构造 `Adder` / `Reg` / `Top` 时分别指向谁，进入与退出子模块如何配对。
   - 每个子模块的命令各自落在它自己的 `_body: Block` 里，而 `blockStack` 在每个时刻的栈顶是谁。
   - 每个模块的 `_namespace` 如何保证各自内部信号不撞名。
4. 在 `Builder.scala` 第 533–559 行的字段表里，对每个字段标注「在本电路 elaboration 过程中，它被谁写过、读过」。

**验收标准**：你能用一句话回答「`io.y := RegNext(io.a)` 这行代码，运行时到底改了哪些全局状态」——预期答案是：它通过 `pushCommand` 往当前模块 `Top` 的 `_body` Block 里追加了一条 `Connect` 命令（以及 `RegNext` 触发的一条 `DefPrim`/节点定义），并未直接修改 `DynamicContext` 的任何字段（`blockStack` 栈顶不变，只是它指向的 Block 内容增长）。

## 6. 本讲小结

- `Builder` 是一个单例 **门面 + 生命周期管理者**：自己几乎不存数据，所有 `Builder.xxx` 都转发到当前 `DynamicContext`；它提供 `build` 入口绑定 context 并收尾组装 `Circuit`。
- `DynamicContext` 是**一次 elaboration 的账本**，持有 `currentModule`、`components`、`whenStack`、`blockStack`、`errors`、`namingStack` 等可变状态；策略性选项（如 `useLegacyWidth`）是只读 `val`。
- 关键澄清：**命令（`Command`）不存在 `DynamicContext` 里**，而是存在每个模块的 `_body: Block` 中；`DynamicContext.blockStack` 只是「当前 Block 指针栈」。`pushCommand` → `forcedUserModule.addCommand` → `Block.addCommand`。
- `HasId` + `IdGen` 给每个硬件对象分配全局唯一 `_id` 并记录 `_parent`；`IdGen` 放在长寿的 `ChiselContext`（线程级）以跨电路累加编号。
- `Namespace` 用压缩 HashMap + 后缀递增做命名去重，模块级 `_namespace` 管信号名，context 级 `globalNamespace` 管模块名/别名。
- 完整链路：`Elaborate` new `DynamicContext` → `Builder.build` 绑定 context → `Module.evaluate` 用 `State.guard` 守卫 → `BaseModule` 构造把 `currentModule` 指向自己 → 构造体灌命令 → `generateComponent` 收口 → `Builder.components += component` → `buildImpl` 组装 `Circuit`。

## 7. 下一步学习建议

- **u4-l2 命令记录与内部 FIRRTL IR**：本讲只说命令进了 `_body: Block`，下一讲会拆开 `Command` 的各种子类（`DefNode`/`DefReg`/`Connect`/`DefPrim` 等）和 `Circuit ⊃ Component ⊃ Command` 三层 IR 结构。
- **u4-l3 Binding 系统**：本讲提到 `pushOp` 会 `bind(OpBinding(...))`，下一讲系统讲解 `Binding` 如何区分「类型 / 字面量 / 端口 / 节点 / 寄存器」。
- **u7-l3 Namer 与 Identifier**：本讲的 `HasId.suggestName` / `_forceName` 与 `Namespace` 如何协作命名，在那里深入。
- **u9-l2 错误处理与 ElaborationTrace**：本讲提到的 `errors: ErrorLog` 收集式错误与 `ElaborationTrace`，在那里展开。
- 建议继续阅读 `Builder.scala` 的 `State` 机制（`save`/`restore`/`guard`）与 `Elaborate.scala`，亲手在一次小型 elaboration 上「单步跟踪」`currentModule` 的变化。
