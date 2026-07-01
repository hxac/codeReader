# 命令记录与内部 FIRRTL IR

## 1. 本讲目标

上一讲（u4-l1）我们看清了 `Builder` 这个全局状态机：它通过 `DynamicContext` 持有 `currentModule`、`blockStack`、`components` 等可变状态，把 elaboration（细化）过程串起来。但还有一个关键问题没有回答：

> 当你在模块构造体里写下一行 `val r = RegNext(in)` 时，这行代码到底变成了什么？它被记录在哪里？长什么样？

本讲就回答这个问题。读完本讲，你应当能够：

1. 说出 Chisel **内部 IR**（Intermediate Representation，中间表示）的三层树形结构：`Circuit ⊃ Component ⊃ Command`。
2. 解释一条硬件构造是如何经过 `Builder.pushCommand → RawModule.addCommand → Block.addCommand` 被追加进当前模块的命令缓冲区的。
3. 认识最常见的几类命令节点：`DefPrim`、`DefReg`、`DefRegInit`、`DefWire`、`Connect`，并能区分 Chisel 内部 IR 节点与 FIRRTL IR 节点（尤其搞清楚 `DefNode` 到底属于哪一边）。
4. 手动追踪 `val r = RegNext(in)` 这一行，准确说出 Builder 会 push 哪几条命令。

本讲是「只登记不施工」这一核心结论的最直接证据：你写的每一行硬件代码，在 elaboration 期间都只是一条被追加进 `Block` 的命令对象，真正的 Verilog 由下游 CIRCT/firtool 产出。

## 2. 前置知识

- **IR（中间表示）**：编译器在「源码」和「目标产物」之间用的一种结构化、易处理的数据表示。Chisel 的内部 IR 不是文本，而是一棵 Scala 对象树。
- **elaboration（细化）**：运行你写的 Scala 构造体，逐步「长出」这棵 IR 树的过程（详见 u4-l1）。
- **Command（命令）**：本讲的主角。一条命令对应一个硬件动作的「登记」，比如声明一个寄存器、连接两个信号。
- **`private[chisel3]`**：Scala 的访问控制，表示「仅 chisel3 包内部可见」。本讲涉及的所有 IR 类都是 `private[chisel3]` 的，用户无法直接 `new`，只能通过 Chisel 公开 API 间接产生。
- 建议先读完 u4-l1（Builder 全局状态机），尤其记住「命令不存在 `DynamicContext` 里，而存在每个模块的 `_body: Block` 里」这个结论——本讲正是要展开它。

## 3. 本讲源码地图

本讲聚焦三个文件，它们都在 `core` 子项目里：

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | **本讲主战场**。定义了 Chisel 内部 IR 的全部节点：`Arg`、`Command` 及其子类、`Component`、`Circuit`，以及承载命令的 `Block`。 |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | 提供 `pushCommand` / `pushOp` 入口，并在 `buildImpl` 收尾时把所有 `Component` 组装成一棵 `Circuit`。 |
| [core/src/main/scala/chisel3/internal/firrtl/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/package.scala) | 仅剩几个 `@deprecated` 的类型别名（`Width`/`KnownWidth`/`UnknownWidth` 已搬到 `chisel3` 顶层）。顺带说明命名空间的迁移历史。 |

辅助理解、会偶尔引用的文件：

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/RawModule.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala) | `RawModule.addCommand`：把命令真正写进当前 `Block`。 |
| [core/src/main/scala/chisel3/Reg.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala) | `Reg` / `RegNext` / `RegInit` 的实现，是本讲追踪示例的起点。 |
| [core/src/main/scala/chisel3/internal/MonoConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala) | `:=` 连线最终在这里 `pushCommand(Connect(...))`。 |
| [core/src/main/scala/chisel3/internal/firrtl/Converter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala) | 把 Chisel 内部 IR 翻译成 `firrtl.ir` 节点（`DefNode` 在这里才出现），用于澄清「DefNode 属于哪一边」。 |

## 4. 核心概念与源码讲解

### 4.1 三层 IR 树：Circuit ⊃ Component ⊃ Command

#### 4.1.1 概念说明

Chisel 的内部 IR 是一棵三层嵌套的 Scala 对象树：

```
Circuit（整个电路，对应一个顶层模块名）
  └─ Component × N   （每个模块/黑盒/类是一个 Component）
       └─ Command × N （模块体内的每条硬件动作是一条 Command）
```

- **`Circuit`** 是顶层容器，持有所有 `Component`、注解、layer、option 等。一个 elaboration 只产出一棵 `Circuit`。
- **`Component`** 是「模块级」节点。最常见的子类是 `DefModule`（普通模块），此外还有 `DefBlackBox`、`DefClass`、`DefIntrinsicModule` 等。每个 `Component` 持有自己的端口列表 `ports: Seq[Port]`。
- **`Command`** 是「模块体内的一条动作」。声明寄存器、声明线、连接信号、条件块都各是一条 `Command`。

这种分层和 FIRRTL 的语法结构高度同构：FIRRTL 里一个 `circuit` 包含多个 `module`，每个 `module` 体内是一串语句。Chisel 的内部 IR 几乎就是「用 Scala 对象表达的 FIRRTL 雏形」——这也是它被放在 `chisel3.internal.firrtl` 包下的原因。

#### 4.1.2 核心流程

IR 树是**自底向上**长出来的，与读取时**自顶向下**的方向相反：

```text
写入方向（elaboration 期间，逐条累加）：
  你写一行硬件代码
    → pushCommand(某条 Command)
      → 追加进 当前模块的 Block._commandsBuilder
        → 模块收口(generateComponent)时，Block 连同命令一起封进 DefModule
          → DefModule 被追加进 DynamicContext.components
            → Builder.buildImpl 收尾时，components 组装成 Circuit

读取方向（收尾/发射时）：
  Circuit.components → 每个 Component（如 DefModule）
    → DefModule.block.getCommands() → 逐条 Command
```

关键点：**命令在写入时只知道把自己塞进「当前的 Block」，它不知道、也不需要知道整棵 Circuit 长什么样**。Circuit 这层是最后由 `Builder` 统一组装的。这就解释了为什么前端 API 可以做到「只登记、不管全局」。

#### 4.1.3 源码精读

**`Command` 抽象基类**——所有命令的唯一共同点就是携带一个 `sourceInfo`（源信息，用于报错定位）：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:313-315](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L313-L315) —— `Command` 只声明了 `sourceInfo`，是所有命令的根。

命令里有一大类是「定义一个具名硬件对象」（寄存器、线、节点、实例……），它们共享 `Definition` 基类，额外持有 `id`（被定义的硬件对象）和由 `id` 推出的 `name`：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:317-320](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L317-L320) —— `Definition extends Command`，凡是带 `id: HasId` 的命令都继承它；`name` 直接取 `id.getRef.name`。

> 区分记忆：`Definition` 子类 = 「我新建了一个信号并给它起名」（如 `DefReg`/`DefWire`/`DefPrim`）；普通 `Command` = 「我对已有信号做了一个动作」（如 `Connect`/`When`/`DefInvalid`）。

**`Component` 抽象基类**——模块级节点，持有 `id`（对应的 `BaseModule`）、`name`、`ports`：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:587-592](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L587-L592) —— `Component extends Arg`，声明 `id`/`name`/`ports` 三个抽象成员。

最常见 的 `Component` 子类是 `DefModule`，它的字段几乎就是「一个模块的全部静态信息」：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:596-603](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L596-L603) —— `DefModule` 持有 `id: RawModule`、模块 `name`、是否公开 `isPublic`、关联的 `layers`、端口 `ports`，以及最重要的 **`block: Block`**——模块体内的所有命令都装在这个 `Block` 里。

端口本身也是一个独立的小数据结构 `Port`：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:531](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L531) —— `Port(id: Data, dir: SpecifiedDirection, associations, sourceInfo)`：一个端口 = 信号 + 方向 + 源信息。

**`Circuit`** 顶层容器：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:660-672](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L660-L672) —— `Circuit` 持有顶层 `name`、所有 `components`、注解工厂 `annotations`、`renames`、类型别名 `typeAliases`、`layers`、`options`、`domains` 等。注意 `components` 是一个 `Seq[Component]`——这就是「模块集合」。

组装这棵 `Circuit` 的地方在 `Builder.buildImpl` 的收尾段：

[core/src/main/scala/chisel3/internal/Builder.scala:1164-1176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1164-L1176) —— `Builder` 用 `components.toSeq` 等材料 `new Circuit(...)`，再包成 `ElaboratedCircuit` 返回。其中 `components` 来自 `DynamicContext.components`：

[core/src/main/scala/chisel3/internal/Builder.scala:533](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L533) —— `DynamicContext` 里的 `val components = ArrayBuffer[Component]()`，每个模块收口时把自己的 `DefModule` 追加进来。

#### 4.1.4 代码实践

**实践目标**：在源码里亲眼确认「三层嵌套」结构。

**操作步骤**：

1. 打开 [IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala)。
2. 定位 `class Circuit`（约 L660），确认它持有一个 `components: Seq[Component]`。
3. 定位 `abstract class Component`（约 L587），确认它有 `id`/`name`/`ports`。
4. 定位 `case class DefModule`（约 L596），确认它有一个 `block: Block` 字段——这就是命令的归宿。

**需要观察的现象**：`DefModule` 并没有直接持有一个 `Seq[Command]`，而是持有 `block: Block`。命令被间接装在 `Block` 里（下一节展开）。

**预期结果**：你能画出 `Circuit → components[i] → DefModule.block → Block._commands → Command` 这条引用链。

#### 4.1.5 小练习与答案

**练习 1**：`Component` 和 `Command` 都不是 `case class` 就是 `abstract class`，它们各自的继承形式是什么？为什么要这么设计？

> **答案**：`Command` 是 `abstract class`（仅声明 `sourceInfo`），`Component` 也是 `abstract class extends Arg`（声明 `id`/`name`/`ports`）。两者都用抽象类而非 `trait`，是因为它们携带字段与具体实现（如 `Component.secretPorts` 在构造时就从 `id.secretPorts` 取值），需要构造时机；同时它们有大量 `case class` 子类，用 `abstract class` 作为密封-ish 的公共基类便于模式匹配。

**练习 2**：一个 `Circuit` 里有 3 个模块，其中顶层模块实例化了另外 2 个子模块。`Circuit.components` 的长度是多少？

> **答案**：3。Chisel 会把每个被实例化（乃至仅 `Definition` 定义）的模块都 flat 地收进 `components`（一个 `ArrayBuffer[Component]`），层级关系靠模块体内的 `DefInstance` 命令表达，而不是靠 `components` 的嵌套。`components.last` 通常是顶层模块（见 `Builder.buildImpl` 里对 `components.last` 设 `isPublic` 的逻辑）。

---

### 4.2 命令记录机制：pushCommand 与 addCommand

#### 4.2.1 概念说明

前端 API（`Reg`、`Wire`、`:=`、`when`……）并不会去碰 `Circuit` 或 `DefModule`，它们只调用一个统一入口：`Builder.pushCommand(cmd)`。这个入口负责把命令塞进**当前正在构造的模块**的**当前 `Block`**。

这里有两层「当前」的概念（u4-l1 已建立，这里复用）：

- **当前模块**：`Builder.forcedUserModule`，即 `DynamicContext.currentModule` 指向的那个 `RawModule`。
- **当前 Block**：`Builder.currentBlock`，即 `DynamicContext.blockStack` 栈顶的那个 `Block`。模块体顶层是一个 `Block`；进入 `when` / `elsewhen` 会压入新的 `Block`（见 u3-l5），命令因此被分流到不同区域。

`Block` 是命令的真正容器：内部用一个 `ArraySeq` 的 `Builder` 累加命令，收口时 `close()` 冻结成不可变 `Seq`。模块关闭后再 `addCommand` 会直接报错。

#### 4.2.2 核心流程

一条命令从被构造到落袋的完整链路：

```text
Reg.apply / Wire.apply / MonoConnect.issueConnect 等
  │  构造一个 Command 对象（如 DefReg、DefWire、Connect）
  ▼
Builder.pushCommand(cmd: Command)            // 统一入口
  │  forcedUserModule.addCommand(cmd)        // 转发给当前模块
  ▼
RawModule.addCommand(cmd: Command)
  │  require(!_closed)                        // 模块未关闭
  │  require(Builder.currentBlock.isDefined)  // 必须在某个 Block 里
  │  Builder.currentBlock.get.addCommand(cmd) // 转发给当前 Block
  ▼
Block.addCommand(cmd: Command)
  │  require(!_closed)                        // Block 未关闭
  │  _commandsBuilder += c                    // 追加进缓冲区
  ▼
（模块收口时）Block.close() → _commands 冻结 → 进 DefModule.block
```

注意 `pushCommand` 返回的就是传入的命令本身（`def pushCommand[T](c: T): T`），这让调用方可以一行完成「登记 + 拿到 id」，例如 `Reg.apply` 里 `pushCommand(DefReg(...))` 后直接返回 `reg`。

还有一个为运算（`a + b` 这类原语运算）特制的入口 `pushOp`，它在 `pushCommand` 之外多做一件事：把运算结果 `Data` 绑定成 `OpBinding`，这样这个中间节点才是一个「合法的、可被引用的硬件值」。

#### 4.2.3 源码精读

**统一入口 `pushCommand`**：

[core/src/main/scala/chisel3/internal/Builder.scala:895-898](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L895-L898) —— `pushCommand` 把命令转发给 `forcedUserModule.addCommand`，并原样返回该命令。

**为原语运算特制的 `pushOp`**：

[core/src/main/scala/chisel3/internal/Builder.scala:899-903](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L899-L903) —— `pushOp` 先把 `cmd.id` 绑定为 `OpBinding(forcedUserModule, currentBlock)`，再 `pushCommand(cmd)`。这就是 `a + b` 产生一个可引用中间节点的关键（详见 4.3）。

**`RawModule.addCommand`**——模块侧的守卫 + 转发：

[core/src/main/scala/chisel3/RawModule.scala:78-82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L78-L82) —— 两个 `require` 分别保证「模块未关闭」与「当前有 Block」，然后把命令交给 `Builder.currentBlock.get.addCommand`。

**`Block.addCommand`**——真正的落袋点：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:419-422](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L419-L422) —— 一行 `_commandsBuilder += c`，命令进入缓冲区。

**`Block` 的整体结构**（理解命令如何被持有与冻结）：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:406-456](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L406-L456) —— `Block` 在建造期用 `_commandsBuilder`（一个 `ArraySeq.newBuilder[Command]`）累加命令；`close()` 时把它冻结成 `_commands: Seq[Command]` 并把 builder 置 `null`（此后 `_closed` 为真，再 `addCommand` 会触发上面的 `require` 报错）。此外它还支持 `_secretCommands`（一种可在 Block 关闭后再追加、且发射时排在普通命令之后的「秘密命令」，用于一些内部补登记场景）。

**`blockStack` 与 `currentBlock`**——「当前 Block」的来源：

[core/src/main/scala/chisel3/internal/Builder.scala:775-794](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L775-L794) —— `blockStack` 是 `DynamicContext.blockStack`（一个 `List[Block]` 栈），`currentBlock` 取栈顶。`pushBlock`/`popBlock` 配合 `RawModule.withRegion` 实现「进入 when/ifRegion/elseRegion 就换一个 Block」，这正是条件块命令分流的机制（u3-l5）。

#### 4.2.4 代码实践

**实践目标**：确认「命令落袋点只有一处」，并理解模块关闭后不可再写。

**操作步骤**：

1. 在 `Builder.scala` 里搜索 `def pushCommand`、`def pushOp`，确认所有前端入口最终都汇到 `forcedUserModule.addCommand`。
2. 在 `RawModule.scala` 里读 `addCommand` 的两个 `require`。
3. 在 `IR.scala` 里读 `Block.addCommand` 与 `Block.close`。

**需要观察的现象**：`Block.addCommand` 与 `RawModule.addCommand` 各有一个 `require(!_closed, ...)`。这构成两道闸门——模块关了不能写、Block 关了也不能写。

**预期结果**：你能解释「为什么不能在模块构造体之外的某个回调里（模块已关闭）再创建硬件」——因为会命中 `require(!_closed)`。

**待本地验证**：可选地写一个最小模块，在 `Module` 构造体内 `override` 或在 `ElaborationThread` 之外尝试 `Wire(...)`，观察是否抛出 "Can't write to module after module close"（仅作理解，不必真去触发）。

#### 4.2.5 小练习与答案

**练习 1**：`pushCommand` 和 `pushOp` 有什么区别？为什么 `a + b` 必须走 `pushOp` 而不能直接 `pushCommand`？

> **答案**：`pushOp` 在 `pushCommand` 基础上多了一步 `cmd.id.bind(OpBinding(...))`。`a + b` 会产生一个**新的中间 `Data`**（运算结果），它必须被绑定成 `OpBinding` 才算「合法硬件值」，才能被后续 `.ref` 引用、被连线。直接 `pushCommand` 只登记命令、不绑定结果，这个中间节点就无法被引用。

**练习 2**：进入一个 `when(cond)` 块后，新写的命令去哪儿了？

> **答案**：去了一个新的 `Block`。`WhenContext` 借 `withRegion` 调 `Builder.pushBlock(ifRegion)`，把 `currentBlock` 切成 `When` 的 `ifRegion` 这个新 `Block`；块内命令因此与块外分离。`otherwise` 时再切到 `elseRegion`。退出时 `popBlock` 恢复原 Block。命令本身（`DefReg`/`Connect` 等）不变，变的是它们落入哪个 `Block`。

---

### 4.3 关键命令节点：DefPrim / DefReg / DefWire / Connect

#### 4.3.1 概念说明（含 DefNode 辨析）

IR.scala 里定义了几十种 `Command` 子类，但日常 RTL 最常产生的就这几类：

| 命令节点 | 对应的 Chisel 写法 | 含义 |
| --- | --- | --- |
| `DefPrim` | `a + b`、`a & b`、`a < b`…… | 一条原语运算，结果是一个新的中间节点 |
| `DefReg` | `Reg(t)`、`RegNext(n)` 内部 | 声明一个无复位值的寄存器 |
| `DefRegInit` | `RegInit(...)` | 声明一个带复位值的寄存器 |
| `DefWire` | `Wire(t)` | 声明一根线 |
| `Connect` | `sink := source` | 把源信号连到汇信号 |
| `DefInvalid` | `DontCare` 连线时 | 把汇标记为「不关心」 |
| `When` | `when(...)` | 条件块（内含 ifRegion/elseRegion 两个 Block） |

⚠️ **关键辨析——`DefNode` 不在 Chisel 内部 IR 里**。学习目标里提到的 `DefNode`，其实是 **FIRRTL IR**（`firrtl.ir.DefNode`）的节点，**不是** `chisel3.internal.firrtl.ir` 里的类。你在 IR.scala 里搜不到 Chisel 版的 `DefNode`。两者的对应关系是：

- Chisel 内部：原语运算 → `DefPrim`（一条 `Definition`，带 `op: PrimOp` 和 `args: Arg*`）。
- 经 Converter 翻译后：`DefPrim` → FIRRTL 的 `fir.DefNode(name, DoPrim(...))`（一个带名字的节点，其表达式是 `DoPrim`）。

也就是说，Chisel 用 `DefPrim` 表达「运算即定义」，而 FIRRTL 习惯把运算结果显式命名为一个 `node`（`DefNode`）。这个差异在 Converter 里被抹平。

此外，每条 `DefPrim` 带一个 `PrimOp`（如 `add`/`and`/`eq`/`mux`），IR.scala 顶部用一个大 `object PrimOp` 集中定义了全部原语。而 `Connect(loc, exp)` 里的 `loc`/`exp` 不是字符串，而是 `Arg`——Chisel 用 `Arg` 这棵小 AST（`Ref`/`Node`/`ULit`/`Slot`/`Index`/`ModuleIO`……）来引用操作数，从而能表达 `bundle.field`、`vec[i]` 这类层级引用。

#### 4.3.2 核心流程

不同前端 API 产生不同命令，但都经 `pushCommand` 落袋：

```text
Reg(t)        →  reg.bind(RegBinding)   →  pushCommand(DefReg(info, reg, clock))
RegInit(t, i) →  reg.bind(RegBinding)   →  pushCommand(DefRegInit(info, reg, clock, reset, init))
Wire(t)       →  x.bind(WireBinding)    →  pushCommand(DefWire(info, x))
a + b         →  (经宏与 helper)        →  pushOp(DefPrim(info, result, AddOp, a, b))
sink := src   →  MonoConnect.elemConnect → issueConnect → pushCommand(Connect(info, sink.lref, src.ref))
```

注意一个一致的模式：**「绑定（bind）」与「登记（pushCommand）」成对出现**。`bind` 给 `Data` 贴上 binding 类型（`RegBinding`/`WireBinding`/`OpBinding`/...），让它从「纯类型」变成「硬件值」；`pushCommand` 则把对应的 IR 节点记进当前 Block。两者缺一不可：只 bind 不登记，IR 树里没有这个节点；只登记不 bind，这个 `Data` 在后续连线/引用时会通不过 `requireIsHardware` 检查（u4-l3 会展开 binding 系统）。

#### 4.3.3 源码精读

**`DefPrim` 与原语运算 `PrimOp`**：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:322](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L322) —— `DefPrim(sourceInfo, id: T, op: PrimOp, args: Arg*)`：一条原语运算命令，`id` 是运算结果，`op` 是操作码，`args` 是操作数。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:27-72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L27-L72) —— `object PrimOp` 集中定义了 `AddOp`/`SubOp`/`TimesOp`/`BitAndOp`/`EqualOp`/`MultiplexOp`（mux）等全部原语，每个就是一个带名字的 `PrimOp(name)`，序列化时直接用这个名字。

**`DefWire` / `DefReg` / `DefRegInit`**：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:326](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L326) —— `DefWire(sourceInfo, id: Data)`：一根线，只有源信息和 id。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:328](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L328) —— `DefReg(sourceInfo, id: Data, clock: Arg)`：寄存器，多一个 `clock` 操作数。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:330](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L330) —— `DefRegInit(sourceInfo, id, clock, reset, init)`：带复位的寄存器，额外持有 `reset` 与 `init` 两个操作数（复位值直接编码在节点里，**不需要**单独发一条 `Connect`）。

**`Connect`**：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:483](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L483) —— `Connect(sourceInfo, loc: Arg, exp: Arg)`：把 `exp`（源）连到 `loc`（汇）。`loc`/`exp` 都是 `Arg`，能表达层级引用。

**产生这些命令的前端代码**：

[core/src/main/scala/chisel3/Reg.scala:36-48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L36-L48) —— `Reg.apply`：先 `reg.bind(RegBinding(...))`，再 `pushCommand(DefReg(sourceInfo, reg, clock))`，最后返回 `reg`。典型的「bind + pushCommand」成对模式。

[core/src/main/scala/chisel3/Reg.scala:79-90](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L79-L90) —— `RegNext.apply(next)`：克隆出一个未知宽度的类型模板 `model`，调用 `Reg(model)`（产生 `DefReg`），再做 `reg := next`（产生 `Connect`）。注意它对 `Bits` 故意用 `cloneTypeWidth(Width())` 清掉宽度，让宽度留给 FIRRTL 推断。

[core/src/main/scala/chisel3/Reg.scala:171-182](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L171-L182) —— `RegInit(t, init)` 双参版本：`bind(RegBinding)` 后 `pushCommand(DefRegInit(info, reg, clock.ref, reset.ref, init.ref))`。对比 `Reg`，复位信息直接进节点。

[core/src/main/scala/chisel3/Data.scala:1089-1102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L1089-L1102) —— `Wire.apply`（`object Wire`）：`x.bind(WireBinding(...))` 后 `pushCommand(DefWire(sourceInfo, x))`，与 `Reg` 完全同构。

**`:=` 产生 `Connect` 的落点**：

[core/src/main/scala/chisel3/internal/MonoConnect.scala:408-415](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L408-L415) —— `issueConnect`：源是 `DontCare` 就 `pushCommand(DefInvalid(...))`，否则 `pushCommand(Connect(sourceInfo, sink.lref, source.ref))`。这就是叶子信号 `:=` 的最终落点。

[core/src/main/scala/chisel3/internal/MonoConnect.scala:419-431](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L419-L431) —— `elemConnect`：先做方向检查 `checkConnect`，再调 `issueConnect`。聚合类型（Bundle/Vec）会先被递归拆解到叶子，再走到这里（详见 u3-l4）。

**澄清 `DefNode` 归属的Converter 证据**：

[core/src/main/scala/chisel3/internal/firrtl/Converter.scala:134-136](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L134-L136) —— Converter 在处理 `DefPrim` 时，产出的是 FIRRTL 的 `fir.DefNode(...)`（表达式为 `convertPrim(...)`，即 `DoPrim`）。这证明 `DefNode` 是 FIRRTL IR 节点，由 `DefPrim` 翻译而来，而非 Chisel 内部 IR 节点。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：手动追踪 `val r = RegNext(in)`，准确说出 Builder push 了哪些命令。这是本讲规格指定的实践任务。

**操作步骤**：

1. 从调用入口 `RegNext.apply(next)` 起步：[Reg.scala:79-90](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L79-L90)。它做了三件事：克隆类型模板 `model` → 调 `Reg(model)` → 执行 `reg := next`。
2. 进入 `Reg.apply(model)`：[Reg.scala:36-48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L36-L48)。这里 `reg.bind(RegBinding(...))`，然后 **`pushCommand(DefReg(sourceInfo, reg, clock))`** —— **第 1 条命令**。
3. 追踪 `pushCommand` → `RawModule.addCommand` → `Block.addCommand`（4.2.3 已读过），确认这条 `DefReg` 落进模块顶层 `Block`。
4. 回到 `RegNext`，处理 `reg := next`：经 `:=` 操作符 → `MonoConnect.elemConnect` → `issueConnect`（[MonoConnect.scala:408-415](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L408-L415)），**`pushCommand(Connect(sourceInfo, reg.lref, next.ref))`** —— **第 2 条命令**。
5. 在 IR.scala 里列出 `Command` 的主要子类作为对照清单（见下方「参考答案」）。

**需要观察的现象**：`RegNext(in)` 这一**行**用户代码，实际上对应**两条** IR 命令：先 `DefReg`（声明寄存器），再 `Connect`（把 `in` 连进去）。两者一前一后进入同一个顶层 `Block`。

**预期结果 / 参考答案**：

`val r = RegNext(in)` 大约 push 以下命令（假设 `in` 是叶子 `Element`，如 `UInt`）：

```text
1. DefReg(sourceInfo, r, clock)                    // 来自 Reg(model)，声明寄存器 r
2. Connect(sourceInfo, r.lref, in.ref)             // 来自 reg := next，把 in 连给 r
```

如果 `in` 是聚合类型（`Bundle`/`Vec`），第 2 条会按字段/元素递归展开成**多条** `Connect`（每个叶子一条），但第 1 条 `DefReg` 仍然只有一条（声明整个寄存器）。如果 `in` 是 `DontCare`，第 2 条会变成 `DefInvalid`。

`Command` 的主要子类清单（按用途分组，均定义于 [IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala)）：

- **Definition（定义具名硬件对象）**：`DefPrim`(L322)、`DefWire`(L326)、`DefReg`(L328)、`DefRegInit`(L330)、`DefMemory`(L332)、`DefSeqMemory`(L334)、`FirrtlMemory`(L342)、`DefMemPort`(L354)、`DefInstance`(L363)、`DefInstanceChoice`(L364)、`DefObject`(L371)、`DefIntrinsicExpr`(L647)、`Printf`(L533)、`Stop`(L487)、`Verification`(L573)。
- **普通 Command（对已有信号做动作）**：`Connect`(L483)、`PropAssign`(L484)、`DefInvalid`(L324)、`Attach`(L486)、`When`(L458)、`Block`(L406，命令容器)、`LayerBlock`(L514)、`DefContract`(L524)、`DefIntrinsic`(L655)、`ProbeDefine`/`ProbeForce`/... (L547-L551)、`FirrtlComment`(L582)。

**待本地验证**：可选地用 `ChiselStage.emitSystemVerilog(new RawModule { val in = IO(Input(UInt(8.W))); val r = RegNext(in) })` 生成 Verilog，对照观察：你会看到一个 `reg [7:0] r`（对应 `DefReg`）和一条 `always @(posedge clock) r <= in`（对应 `Connect`）。由于内部 `Command` 是 `private[chisel3]`，无法从用户侧直接打印命令列表，故以 Verilog 产物间接验证。

#### 4.3.5 小练习与答案

**练习 1**：`RegInit(0.U(8.W))` 会 push 几条命令？和 `RegNext` 有何不同？

> **答案**：只 push **一条** `DefRegInit(sourceInfo, reg, clock, reset, init)`（见 [Reg.scala:180](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L180)）。因为复位值 `init` 直接编码进 `DefRegInit` 节点，不需要额外 `Connect`。而 `RegNext` 用的是 `DefReg`（无复位字段），所以「把 next 连进去」必须靠单独的 `Connect`。这也是 `DefReg` 与 `DefRegInit` 字段差异（后者多 `reset`/`init`）的直接体现。

**练习 2**：`val s = a + b`（`a`、`b` 是 `UInt`）会 push 什么命令？走 `pushCommand` 还是 `pushOp`？

> **答案**：走 **`pushOp`**，push 一条 `DefPrim(sourceInfo, s, AddOp, a, b)`。`pushOp` 会额外把 `s` 绑定为 `OpBinding`，使 `s` 成为可引用的中间节点。对比 `Reg`/`Wire` 走普通 `pushCommand` + `RegBinding`/`WireBinding`——区别在于运算结果需要 `OpBinding` 且带上 `currentBlock` 信息（用于决定它属于哪个区域）。

**练习 3**：为什么说「`DefNode` 不是 Chisel 内部 IR 节点」？请给出源码证据。

> **答案**：在 IR.scala 里没有 `DefNode` 这个 case class；原语运算在 Chisel 内部用 `DefPrim` 表达。`DefNode` 是 `firrtl.ir.DefNode`（FIRRTL IR），由 Converter 在 [Converter.scala:134-136](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L134-L136) 把 `DefPrim` 翻译而来（`fir.DefNode(name, DoPrim(...))`）。所以 `DefNode` 属于「下游 FIRRTL」，`DefPrim` 才是「Chisel 内部」。

## 5. 综合实践

把本讲三个最小模块串起来，做一个**命令预测 + 产物对照**的小任务。

**任务**：给定下面这个最小模块（示例代码，非项目原有代码）：

```scala
// 示例代码
import chisel3._
class Delta extends Module {
  val in  = IO(Input(UInt(8.W)))
  val out = IO(Output(UInt(8.W)))
  val r   = RegNext(in)      // 第 1 行
  val p   = r + 1.U          // 第 2 行
  out := p                   // 第 3 行
}
```

请完成：

1. **预测命令**：逐行写出 Builder 会向顶层 `Block` push 哪些命令（用 `DefReg`/`Connect`/`DefPrim` 等节点名，并标出大致操作数）。注意 `+` 会被宏展开成 `do_+` → `_impl_+` → `binop` → `pushOp(DefPrim(..., AddOp, ...))`（见 u2-l2）。
2. **画引用链**：画出从 `Circuit` 到这些命令的引用路径（`Circuit.components → DefModule → block: Block → _commands → Command`）。
3. **对照验证**：用 `ChiselStage.emitSystemVerilog(new Delta)` 生成 SystemVerilog，把你预测的每条命令与 Verilog 中的声明/赋值一一对应（`DefReg` ↔ `reg` 声明、`DefPrim(AddOp)` ↔ `assign _T = r + ...`、两条 `Connect` ↔ `always ... <=` 与 `assign out =`）。

**参考预测**（建议你先自己写，再对照）：

```text
第 1 行  val r = RegNext(in)
  → DefReg(r, clock)                      // Reg(model)
  → Connect(r, in)                        // r := in

第 2 行  val p = r + 1.U
  → DefPrim(p, AddOp, r, 1.U)             // pushOp，p 绑为 OpBinding
     （1.U 是 ULit 字面量，作为 Arg 直接内联，不单独 push 命令）

第 3 行  out := p
  → Connect(out, p)                       // MonoConnect.issueConnect
```

> 注意：字面量 `1.U` 不产生独立命令——它只是一个 `ULit` `Arg`，被嵌进 `DefPrim` 的 `args` 里。端口 `in`/`out` 的声明也不在本模块体命令里，而在 `DefModule.ports` 中。

**待本地验证**：实际命令顺序、临时节点命名（如 `_T`）由下游命名器（u7-l3）决定，可能与你手写的名字不同；但**命令的种类与条数**应与预测一致。

## 6. 本讲小结

- Chisel 内部 IR 是三层树：**`Circuit ⊃ Component ⊃ Command`**。`Circuit` 持有所有模块（`components: Seq[Component]`），每个 `DefModule` 持有一个 `block: Block`，`Block` 里装着所有 `Command`。
- 命令的统一入口是 **`Builder.pushCommand`**，它转发给 `RawModule.addCommand`，最终落进 `Builder.currentBlock.get.addCommand`（即 `Block._commandsBuilder`）。原语运算走特制的 `pushOp`，多一步 `OpBinding`。
- 命令分两大类：`Definition`（定义具名硬件对象，带 `id`/`name`，如 `DefReg`/`DefWire`/`DefPrim`）与普通 `Command`（对已有信号做动作，如 `Connect`/`When`/`DefInvalid`）。
- **`DefNode` 不是 Chisel 内部 IR 节点**，而是 FIRRTL IR 节点；Chisel 内部用 `DefPrim` 表达原语运算，经 Converter 翻译成 FIRRTL 的 `DefNode`。
- 一致的编码模式是「**bind + pushCommand 成对出现**」：`Reg`/`Wire`/`RegInit`/运算各自先给 `Data` 贴上对应的 binding，再 push 对应命令节点。
- 追踪结论：**`val r = RegNext(in)` 产生 2 条命令**——`DefReg(r, clock)` 与 `Connect(r, in)`；而 `RegInit(...)` 只产生 1 条 `DefRegInit`（复位值内嵌）。

## 7. 下一步学习建议

- **下一讲 u4-l3「Binding 系统」**：本讲反复出现的 `bind(RegBinding)` / `OpBinding` / `WireBinding` 到底是什么？`Data` 的 `binding` 字段如何区分「纯类型 / 字面量 / 端口 / 节点 / 寄存器」？这是承上启下的一讲。
- **u4-l4「IR 序列化：Serializer」**：想知道这棵 IR 树打印出来长什么样？`Serializer` 把 `DefPrim`/`Connect` 等节点序列化成人类可读的 CHIRRTL 文本，是调试 Chisel 电路的利器。
- **u4-l5「Converter」**：深入了解 `DefPrim → fir.DefNode`、`DefReg → fir.DefRegister` 的完整映射规则，看清「Chisel 内部 IR → FIRRTL IR」这一跳。
- **延伸阅读**：在 [IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) 里通读一遍 `Arg` 子类（`Ref`/`Node`/`ULit`/`SLit`/`Slot`/`Index`/`ModuleIO`），理解 `Connect` 的操作数如何表达 `bundle.field`、`vec[i]` 这类层级引用——这会帮你打通本讲刻意略过的 `Arg` 这层细节。
