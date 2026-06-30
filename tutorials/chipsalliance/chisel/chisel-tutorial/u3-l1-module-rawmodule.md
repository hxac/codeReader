# Module 与 RawModule 生命周期

## 1. 本讲目标

本讲是「模块与连线」单元的第一讲，目标是带你钻进 Chisel 里最核心的一个概念——**模块（Module）的内部生命**。

读完本讲，你应当能够：

- 说清 `Module`、`RawModule`、`BaseModule` 三者的继承关系与职责分工。
- 解释当你写下 `Module(new MyMod)` 时，Chisel 在背后按什么顺序做了哪几步（`evaluate → generateComponent → initializeInParent`）。
- 理解 `generateComponent` 如何把一个 Scala 对象「收尾」成内部 FIRRTL IR 的 `DefModule`。
- 区分 `Module`（自带隐式 `clock`/`reset`）和 `RawModule`（不带），并知道为什么 Module 能拿到「隐式时钟」。

本讲只读不改源码。所有结论都基于 `core/src/main/scala/chisel3/Module.scala` 与 `core/src/main/scala/chisel3/RawModule.scala` 的真实代码。

## 2. 前置知识

在进入源码前，先用一段话把背景补齐。前面几讲你已经知道：

- Chisel 是嵌在 Scala 里的硬件构造 DSL。你写的 `class Foo extends Module` **本质上是一个 Scala 类**，它的**构造体**就是你的电路描述。
- **Elaboration（细化）**：Chisel 在运行时执行你的构造体，把它「长」成一棵内部 IR 树。你写在构造体里的每一行（`IO(...)`、`:=`、`when(...)`）只是向一个全局记录器**登记命令**，本身不产生硬件或文件（见 u1-l4、u1-l5）。
- 真正触发 elaboration 的是 `ChiselStage.emitSystemVerilog`，它会顺着 Phase 管道走完「Scala → Builder → 内部 IR → CIRCT」整条链路。

本讲要回答的核心问题是：**这条链路里，从「执行构造体」到「收拢成 IR」中间那一小段，到底发生了什么？** 答案全部藏在 `Module.scala` 与 `RawModule.scala` 里。

几个 Scala 术语会反复出现，先给初学者一句话解释：

- **伴生对象（companion object）**：与类同名、用 `object` 声明的单例对象。`Module` 类是你要继承的基类，`Module` 对象（`object Module`）是创建模块的**工厂**，二者都叫 `Module`。
- **按名调用（by-name parameter）**：形如 `bc: => T` 的参数，传入的表达式不会立刻求值，而是在函数内部**用到它时**才求值。这是 elaboration 能「延迟执行」你的构造体的关键。
- **惰性求值（lazy）**：`lazy val` 只在第一次被访问时才计算，之后缓存结果。本讲会看到一个叫 `Delayed` 的「惰性盒子」专门用来解决初始化顺序问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) | 本讲主战场。包含 `object Module`（工厂）、`class Module`（带 clock/reset 的基类）、`BaseModule`（公共骨架）、`ImplicitClock`/`ImplicitReset`（隐式时钟/复位 trait）。 |
| [core/src/main/scala/chisel3/RawModule.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala) | `RawModule` 定义：不带隐式 clock/reset 的基类，并实现了 elaboration 收尾的核心方法 `generateComponent`。 |
| [core/src/main/scala-2/chisel3/ModuleIntf.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ModuleIntf.scala) | 用户入口 `apply` 方法（一个宏）与 `do_apply`，最终调用 `_applyImpl`。 |
| [core/src/main/scala/chisel3/internal/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/package.scala) | `Delayed` 惰性盒子，解决隐式 clock/reset 的初始化顺序问题。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 内部 FIRRTL IR 节点定义，`generateComponent` 产出的 `DefModule` 就在这里。 |

## 4. 核心概念与源码讲解

本讲按「先骨架、后两条分支、再生命周期、最后收尾」的顺序，拆成四个最小模块：

- **4.1 BaseModule**：所有模块的公共骨架（状态字段 + 两个抽象方法）。
- **4.2 Module 与 RawModule**：用户继承的两条分支，差别在于「有没有隐式 clock/reset」。
- **4.3 Module() 工厂方法与 elaboration 生命周期**：`_applyImpl / evaluate` 把构造体跑起来的过程。
- **4.4 generateComponent**：把构造完的模块对象收拢成 IR `Component` 的收尾步骤。

### 4.1 BaseModule：所有模块的公共骨架

#### 4.1.1 概念说明

`BaseModule` 是 Chisel 里**所有模块的最底层抽象**。它定义在 `chisel3.experimental` 包里（注意：不是 `chisel3` 根包），普通用户通常不会直接继承它，但 `RawModule`、`Module`、`BlackBox`、以及 hierarchy 的 `Definition` 都最终继承自它。

它的职责可以一句话概括：**在 elaboration 期间，当一个模块被构造时，为它登记所有「需要被 Builder 全局跟踪」的可变状态**。这些状态包括：

- `_ids`：本模块内创建的所有 `HasId` 对象（Wire/Reg/端口/子模块实例……）。
- `_ports`：本模块声明的端口。
- `_body`：一个 `Block`，承载构造体里登记的所有命令（命令的容器）。
- `_namespace`：本模块私有的命名空间（FIRRTL 里模块间命名空间互不相通）。
- `_component`：收尾后产出的 IR `Component`，初始为 `None`。
- `_closed`：模块是否已「关闭」，关闭后不能再写命令。

`BaseModule` 还声明了两个抽象方法，规定了「每个具体模块必须实现的两件事」：

```scala
private[chisel3] def generateComponent(): Option[Component]   // 收尾：产出 IR
private[chisel3] def initializeInParent(): Unit               // 在父模块里连好本模块的实例
```

这两个方法在 `Module` 与 `RawModule` 里有不同实现，是本讲后半部分的主角。

#### 4.1.2 核心流程

当一个模块的 Scala 构造体开始执行时（也就是 `new MyMod` 那一刻），`BaseModule` 的构造体（`super` 链中最先跑的一段）会做这几件事：

```text
BaseModule 构造体执行：
  1. this._parent.foreach(_.addId(this))   // 把自己登记进父模块的 _ids
  2. 校验 Builder.readyForModuleConstr      // 必须先被 Module() 工厂「允许」才能构造
  3. Builder.currentModule = Some(this)     // 告诉 Builder「现在正在构造的是我」
  4. getBody.foreach(Builder.pushBlock(_))  // 把自己的 _body 推为当前命令记录块
```

第 2 步是关键的安全阀：它强制用户**必须**把模块实例化包在 `Module(new ...)` 里。如果你直接写 `new MyMod` 而不包 `Module(...)`，`readyForModuleConstr` 为 `false`，立刻报错。这保证了 Builder 全局状态在每个模块构造时都能被正确设置。

> 为什么用 `readyForModuleConstr` 这种标志位？因为 Scala 的构造是递归的、立刻发生的，Builder 无法「拦截」`new`。它只能用一个全局开关：工厂方法 `evaluate` 在构造前把开关打开（见 4.3），构造体里检查开关、用完立刻关上。

#### 4.1.3 源码精读

先看类声明，确认它的继承链与所在包：

[core/src/main/scala/chisel3/Module.scala:420-423](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L420-L423) — `BaseModule` 继承 `HasId`（有全局唯一 id）、`IsInstantiable`（可被 Definition/Instance 体系实例化）、`ReflectSelectable`，是模块抽象的根。

再看构造体里登记全局状态的那段：

[core/src/main/scala/chisel3/Module.scala:486-498](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L498) — 校验 `readyForModuleConstr`、设置 `Builder.currentModule`、把 `_body` 推入 Builder 的块栈。这就是上面流程图四步的源码。

`_closed` 标志与「关闭后不可再写」的约束：

[core/src/main/scala/chisel3/Module.scala:503-506](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L503-L506) — `_closed` 初始为 `false`，`isClosed` 是对外只读视图。

两个抽象方法（每个具体模块必须实现）：

[core/src/main/scala/chisel3/Module.scala:662-669](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L662-L669) — `generateComponent()` 产出 IR `Component`；`initializeInParent()` 在父模块里完成实例的初始化连线。

模块的「名字」也在 `BaseModule` 里定义，并且是 `lazy val`——只有在 elaboration 收尾、命名空间准备好之后才能确定：

[core/src/main/scala/chisel3/Module.scala:732-752](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L732-L752) — `name` 经全局命名空间去重后确定；`desiredName`（[L711](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L711)）默认取类名，可被用户覆盖。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲眼看到「直接 `new` 一个模块会失败」这条安全阀。

1. 在 [Module.scala:486-492](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L492) 找到那段 `this match { case _: PseudoModule => ... case other => if (!Builder.readyForModuleConstr) throwException(...) }`，确认报错文案是 `"attempted to instantiate a Module without wrapping it in Module()."`。
2. 思考：`PseudoModule`（如 hierarchy 的 `Instance`）为什么被排除在校验之外？在仓库里搜索 `PseudoModule` 的定义，用一句话写下你的理解。
3. 把 4.1.2 流程图里的四步，对应到 [Module.scala:486-498](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L498) 的具体行，标出每一步的行号。

**预期结果**：你能用行号精确指出「校验开关」「设置 currentModule」「推入 body 块」分别是哪几行。

#### 4.1.5 小练习与答案

**练习 1**：`BaseModule` 里为什么要单独维护一个 `_namespace`，而不是用 Builder 的全局命名空间？

**参考答案**：因为 FIRRTL 语义里**各模块的内部命名空间是互不相通的**（两个不同模块里都可以有一个叫 `_T_0` 的信号），所以每个 `BaseModule` 持有自己的 [Namespace](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L559)（`_namespace = Namespace.empty`），只负责本模块内部信号的去重；模块**名**本身才进全局命名空间（见 `name` 的 [L744](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L744)）。

**练习 2**：`generateComponent()` 的返回类型为什么是 `Option[Component]` 而不是 `Component`？

**参考答案**：因为有些「模块」（如 hierarchy 的某些克隆）**不生成真正的 IR Component**，但仍可能持有一个。返回 `Option` 让调用方在 [evaluate](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L92-L95) 里用 `for (component <- componentOpt) Builder.components += component` 优雅地跳过这种情况。

---

### 4.2 Module 与 RawModule：两条继承分支

#### 4.2.1 概念说明

绝大多数时候，你二选一地继承这两个类：

- **`RawModule`**：不带隐式 `clock`/`reset`，支持**多次** `IO()` 声明。当你写的是「胶水逻辑」「顶层」「不需要时钟的纯组合电路」时用它。
- **`Module`**：在 `RawModule` 基础上**自动加两个端口** `clock` 和 `reset`，并把它们设为「隐式时钟/复位」。这是描述**时序逻辑**（寄存器、状态机）时的默认选择，因为 `Reg`、`when` 等构造默认就用这对隐式 clock/reset。

继承关系是：

```text
BaseModule  (chisel3.experimental，公共骨架)
   ↑
RawModule   (chisel3，实现 generateComponent；不带 clock/reset)
   ↑
Module      (chisel3，extends RawModule with ImplicitClock with ImplicitReset)
```

所以 `Module` **就是**一个 `RawModule`，只是多了两个端口和隐式时钟机制。这一点在源码里看得一清二楚。

`Module` 还有两个常用混入 trait：`RequireAsyncReset` 和 `RequireSyncReset`，用来**固定** reset 的类型（异步/同步）。另外 `resetType` 方法可被覆盖来选择复位类型。

#### 4.2.2 核心流程

`Module` 是怎么「自动」长出 clock/reset 端口的？关键在两个 trait：`ImplicitClock` 和 `ImplicitReset`。它们的 trait 体里各自有一行：

```scala
Builder.currentClock = Some(Delayed(implicitClock))   // ImplicitClock
Builder.currentReset = Some(Delayed(implicitReset))   // ImplicitReset
```

而 `implicitClock` / `implicitReset` 是**抽象方法**，`Module` 覆盖它们指向自己刚创建的 `clock` / `reset` 端口。这里有个 Scala 初始化顺序的坑：trait 体执行时，`Module` 类体里的 `val clock` 可能**还没初始化**。解决办法就是 `Delayed`——一个惰性盒子：现在只存一个「按名调用」的引用，等以后真正需要读时钟值时（`lazy val value`）才求值，那时 `clock` 早已就绪。

`Module` 的复位端口类型由 `mkReset` 决定：

```text
resetType == Default:
  顶层模块（无 _parent，且非 Definition 上下文） -> Bool()      （同步复位）
  子模块                                     -> Reset()      （待推断）
resetType == Uninferred  -> Reset()
resetType == Synchronous -> Bool()
resetType == Asynchronous -> AsyncReset()
```

#### 4.2.3 源码精读

先看两条分支的声明，确认 `Module extends RawModule`：

[core/src/main/scala/chisel3/RawModule.scala:19-23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L19-L23) — `abstract class RawModule extends BaseModule`，文档明确说「不含隐式 clock/reset、支持多次 IO()」。

[core/src/main/scala/chisel3/Module.scala:225-232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L225-L232) — `abstract class Module extends RawModule with ImplicitClock with ImplicitReset`，文档说「包含隐式 clock 和 reset」。

`Module` 自动创建的两个端口：

[core/src/main/scala/chisel3/Module.scala:237-243](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L237-L243) — 用 `IO(Input(Clock()))` 和 `IO(Input(mkReset))` 直接造出 `clock`/`reset` 端口，并把它们设为 `implicitClock`/`implicitReset`。

复位类型决策 `mkReset`：

[core/src/main/scala/chisel3/Module.scala:262-274](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L262-L274) — 见上面流程图；顶层默认 `Bool()`、子模块默认 `Reset()`。

两个隐式 trait，注意它们用 `self: RawModule =>` 把自己钉死在 `RawModule` 上：

[core/src/main/scala/chisel3/Module.scala:307-313](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L307-L313) — `ImplicitClock`，trait 体里 `Builder.currentClock = Some(Delayed(implicitClock))`。

[core/src/main/scala/chisel3/Module.scala:336-342](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L336-L342) — `ImplicitReset`，对称地设置 `currentReset`。

`Delayed` 惰性盒子：

[core/src/main/scala/chisel3/internal/package.scala:218-227](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/package.scala#L218-L227) — 注释写得很直白：`This is effectively a "LazyVal" box`。`value` 是 `lazy val`，这就是它能绕过初始化顺序的原因。

#### 4.2.4 代码实践

**实践目标**：用真实代码看到 `Module` 比 `RawModule` 多出两个端口。

下面是一个**示例代码**（可直接放进一个 `object Main extends App`，配合 `ChiselStage.emitSystemVerilog` 运行；如本地未配置 firtool，标注为「待本地验证」）：

```scala
import chisel3._
import circt.stage.ChiselStage

// 分支一：Module，自带 clock/reset
class WithClk extends Module {
  val io = IO(new Bundle { val in = Input(UInt(8.W)); val out = Output(UInt(8.W)) })
  io.out := io.in
}

// 分支二：RawModule，没有 clock/reset
class NoClk extends RawModule {
  val io = IO(new Bundle { val in = Input(UInt(8.W)); val out = Output(UInt(8.W)) })
  io.out := io.in
}

object Main extends App {
  println("=== WithClk (Module) ===")
  println(ChiselStage.emitSystemVerilog(new WithClk))
  println("=== NoClk (RawModule) ===")
  println(ChiselStage.emitSystemVerilog(new NoClk))
}
```

**需要观察的现象**：

1. `WithClk` 生成的 Verilog 端口列表里会有 `input clock` 和 `input reset`（即使你没用）。
2. `NoClk` 生成的 Verilog 端口列表里**只有** `in` 和 `out`，没有 clock/reset。

**预期结果**：两个模块功能一样（`out = in`），但端口集合不同，印证「Module 就是 RawModule + 两个端口 + 隐式时钟」。

**进一步**：把 `class WithClk extends Module` 改成 `class WithClk extends Module with RequireAsyncReset`，重新生成，观察 `reset` 端口类型是否从 `reset`（Bool）变成 `async reset`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ImplicitClock` trait 要写成 `self: RawModule =>` 而不是普通 trait？

**参考答案**：这是一个**自类型（self type）**约束，要求混入 `ImplicitClock` 的类**必须同时是** `RawModule`。因为隐式时钟机制依赖 `RawModule` 才有的端口/构造能力，普通的 `class Foo extends ImplicitClock` 会被编译器拒绝，从而防止误用。

**练习 2**：如果我写一个 `class Foo extends RawModule`，里面用 `Reg(UInt(8.W))`，会发生什么？

**参考答案**：`Reg` 需要隐式时钟（它要在某个 `clock` 边沿更新）。`RawModule` 不混入 `ImplicitClock`，`Builder.currentClock` 为 `None`，所以直接写 `Reg(...)` 会在 elaboration 时报「缺少隐式 clock」的错误。解决办法是用 `withClock(myClock)(Reg(...))` 显式提供时钟（见 [MultiClock.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/MultiClock.scala)）。

---

### 4.3 Module() 工厂方法与 elaboration 生命周期

#### 4.3.1 概念说明

前面 4.1 讲了「构造体执行时」发生什么，但**谁触发了构造体执行**？答案就是 `Module` **伴生对象**上的工厂方法。

你写的永远是 `Module(new MyMod(...))`，而不是 `new MyMod(...)`。这个 `Module(...)` 调用的就是 `object Module` 上的 `apply` 方法。它做两件事：

1. **执行你的构造体**（真正 `new MyMod`，触发 elaboration）。
2. **在父作用域里登记这次实例化**（向父模块 push 一条 `DefInstance` 命令，并在父里初始化本模块）。

注意区分同名但不同的两个 `Module`：

- `object Module`（工厂）：本节讲它。
- `class Module`（基类）：4.2 讲它。

由于 `apply` 是一个**宏**（用来在调用点注入源码行号信息，见 u7-l2），真正的实现分两层：宏 `apply` → `do_apply` → `_applyImpl`。

#### 4.3.2 核心流程

`Module(new MyMod)` 的完整生命周期（这是本讲最重要的一张图）：

```text
Module(new MyMod)
   │  （宏展开 + 注入 SourceInfo）
   ▼
do_apply(bc)
   │
   ▼
_applyImpl(bc)                              [Module.scala:36]
   │
   ├──► evaluate(bc)                         [Module.scala:65]   ── 跑构造体
   │        │
   │        │  1. 设 readyForModuleConstr = true（允许构造）
   │        │  2. Builder.State.guard { bc } ── 执行 new MyMod
   │        │       ├── BaseModule 构造体（登记状态，设 currentModule）
   │        │       ├── 你的 IO/Reg/when 代码（登记命令到 _body）
   │        │       └── generateComponent()  ── 收尾（见 4.4）
   │        │  3. Builder.components += component
   │        └──► 返回 module
   │
   ├──► （若有父模块且 module._component 有值）
   │        pushCommand(DefInstance(...))     ── 在父里登记「这里实例化了 MyMod」
   │        module.initializeInParent()       ── 在父里连好实例（Module 在这里连 clock/reset）
   │
   └──► 返回 module
```

一句话总结：`_applyImpl` 负责「在父里挂载」，`evaluate` 负责「把自己长出来」。`evaluate` 内部又调用 `generateComponent` 完成「收尾成 IR」。

#### 4.3.3 源码精读

工厂对象与用户入口：

[core/src/main/scala/chisel3/Module.scala:34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L34) — `object Module extends ModuleObjIntf`。

[core/src/main/scala-2/chisel3/ModuleIntf.scala:19-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ModuleIntf.scala#L19-L22) — `apply` 是宏；`do_apply` 直接转调 `_applyImpl`。这就是「宏 → 实现」的两层结构。

`_applyImpl` 全文（本讲核心之一）：

[core/src/main/scala/chisel3/Module.scala:36-62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L36-L62) — 先 `evaluate` 得到 module；若处于父模块上下文，则根据产物类型 push 一条命令：普通模块 push `DefInstance`（[L56](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L56)），`Class` 产物则 push `DefObject`；最后调用 `module.initializeInParent()`（[L58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L58)）。注意它对 `Class` 做了拦截：`Module()` 不能用在 `Class` 上（[L45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L45)），那是 `Definition()` 的领地。

`evaluate` 全文（本讲核心之二）：

[core/src/main/scala/chisel3/Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108) — 关键行：

- [L66-72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L66-L72)：检查并设置 `readyForModuleConstr`（这就是 4.1 里那个安全阀的「打开」动作）。
- [L77-103](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L77-L103)：在 `Builder.State.guard` 保护下执行 `bc`（即 `new MyMod`，你的构造体在这里跑完）；之后校验 `whenDepth == 0`（防止 `when` 没闭合），调用 `module.generateComponent()`，把产出的 component 加进 `Builder.components`。
- [L105-107](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L105-L107)：pop elaboration trace，调用 `module.moduleBuilt()`（触发 `afterModuleBuilt` 钩子，见 [RawModule.scala:230](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L230)）。

`DefInstance` 命令长什么样：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:363](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L363) — `case class DefInstance(sourceInfo, id: BaseModule, ports: Seq[Port])`，这就是父模块命令队列里代表「实例化了某个子模块」的 IR 节点。

#### 4.3.4 代码实践（源码阅读型，对应 spec 任务）

**实践目标**：在源码里精确走一遍 `Module(new MyMod)` 的调用链，并用自己的话写下来。

1. 打开 [ModuleIntf.scala:19-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ModuleIntf.scala#L19-L22)，确认用户写的 `Module(new MyMod)` 实际进入 `do_apply`，再进入 `_applyImpl`。
2. 打开 [Module.scala:36-62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L36-L62)（`_applyImpl`），找到 `evaluate` 调用（[L38](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L38)）和「父作用域挂载」段（[L42-58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L42-L58)）。
3. 打开 [Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108)（`evaluate`），找到：开关打开（[L72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L72)）、构造体执行（[L78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L78)）、收尾（[L92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L92)）、登记 component（[L94](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L94)）。
4. **用一段话写下你的说明**：`Module(new MyMod)` 调用时发生了哪几步？参考答案见 4.3.5。

**预期结果**：你能脱离讲义，凭源码行号向别人讲清这条链路。这是读懂后续 `Builder`/`Elaborate` 阶段（u4、u5）的必备基础。

#### 4.3.5 小练习与答案

**练习 1**：`evaluate` 里为什么要用 `Builder.State.guard(Builder.State.default) { ... }` 包住构造体执行？

**参考答案**：`Builder.State.guard` 会保存当前 Builder 全局状态、切到一个干净默认状态执行代码块、结束后再恢复。模块构造必须在一个**隔离的上下文**里进行（避免父模块的 `currentModule`、块栈等污染子模块），所以需要这层保护。这也是 elaboration 可递归（模块里实例化模块）的基础。

**练习 2**：`_applyImpl` 里为什么要判断 `Builder.currentModule.isDefined`？

**参考答案**：因为存在「顶层模块」——它没有父模块，是整个电路的入口。顶层 `Module(new Top)` 调用时 `Builder.currentModule` 为空（`None`），此时不能也不需要向父作用域 push `DefInstance`；只有作为子模块被实例化时（`currentModule` 有值）才需要挂载（[L42](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L42)）。

**练习 3**：如果用户写 `val m = new MyMod`（漏了 `Module(...)` 包裹），会在哪一行报错？

**参考答案**：在 [BaseModule 构造体 Module.scala:489-491](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L489-L491) 报 `"attempted to instantiate a Module without wrapping it in Module()."`，因为此时 `readyForModuleConstr` 仍是 `false`。

---

### 4.4 generateComponent：收尾与生成 IR Component

#### 4.4.1 概念说明

`generateComponent` 是 elaboration 的**收尾步骤**。当你的构造体跑完，模块对象身上已经积累了一堆「待加工」的东西：一堆没定名字的 `_ids`、一批端口、一个装满命令的 `_body`。`generateComponent` 把它们加工成一个干净的 IR `Component`（对 `RawModule`/`Module` 而言就是 `DefModule`），并：

- **命名**：给所有 `_ids` 分配可读名字（Wire/Reg/端口/实例……）。
- **关闭**：置 `_closed = true`，此后模块不可再写命令。
- **校验**：检查端口是否都能命名。
- **组装**：构造 `DefModule(id, name, isPublic, layers, ports, block)`。

`generateComponent` 是 `BaseModule` 上的抽象方法，`RawModule` 给出了真实实现；`Module` **不**覆盖它（继承 `RawModule` 的实现）。所以无论你继承 `Module` 还是 `RawModule`，收尾逻辑是同一份。

> 提醒：[BaseModule.scala:662-665](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L662-L665) 是抽象声明，[RawModule.scala:158-196](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L158-L196) 是实现。`Module.scala` 里搜不到 `override def generateComponent`，这正好印证「Module 复用 RawModule 的收尾」。

#### 4.4.2 核心流程

`RawModule.generateComponent` 的步骤（行号见 4.4.3）：

```text
1. require(!_closed)                        // 不能重复收尾
2. for (id <- _ids) nameId(id)              // 第一轮：给已有 id 命名
3. evaluateAtModuleBodyEnd()                // 执行 atModuleBodyEnd 钩子（可能新增 id）
4. _closed = true                           // 关闭模块
5. checkPorts()                             // 校验端口可命名
6. for (id <- _ids.view.drop(numInitialIds)) nameId(id)  // 第二轮：给钩子产生的新 id 命名
7. 构造 firrtlPorts                          // 把端口转成 IR Port
8. _body.close()                            // 关闭命令块
9. val component = DefModule(this, name, _isPublic, layers, firrtlPorts, _body)
10. _component = Some(component); 返回       // 缓存并返回
```

`nameId` 是一个大的模式匹配，按对象类型分派默认名：端口给 `PORT`、Wire 给 `_WIRE`、Reg 给 `REG`、运算节点给 `_T`、内存端口给 `MPORT` 等，再经 `_namespace` 去重成唯一名。这就是你最终在 Verilog 里看到 `_T_1`、`_T_2`、`reg`、`io_in` 这类名字的由来。

#### 4.4.3 源码精读

`generateComponent` 实现（本讲核心之三）：

[core/src/main/scala/chisel3/RawModule.scala:158-196](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L158-L196) — 对照上面流程图的十步。重点行：

- [L162-165](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L162-L165)：第一轮 `nameId`。
- [L170](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L170)：`_closed = true`。
- [L180-183](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L180-L183)：把端口转成 IR `Port`。
- [L186](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L186)：`_body.close()`。
- [L190-191](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L190-L191)：组装 `DefModule`。

`nameId` 命名分派：

[core/src/main/scala/chisel3/RawModule.scala:111-156](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L111-L156) — 看不同 binding 如何得到不同默认名，例如 `WireBinding → "_WIRE"`（[L147](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L147)）、`RegBinding → "REG"`（[L144](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L144)）、`OpBinding → "_T"`（[L138-L139](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L138-L139)）。

命令如何登记进 `_body`（命令的来源）：

[core/src/main/scala/chisel3/RawModule.scala:78-82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L78-L82) — `addCommand` 校验未关闭、有当前块，然后把命令加进 `Builder.currentBlock`。你构造体里的每一行 `:=`、`when` 最终都经这里进入 `_body`。

最终产物 `DefModule` 的结构：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:596-603](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L596-L603) — `case class DefModule(id, name, isPublic, layers, ports, block)`。它持有一个 `Block`（即装满命令的 `_body`），这就是模块的「身体」。它的父类 `Component` 见 [L587-592](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L587-L592)。

#### 4.4.4 代码实践

**实践目标**：跟踪一个 `Wire` 在 `generateComponent` 里如何获得名字，并验证 `Module` 不覆盖 `generateComponent`。

1. 在 `core/src/main/scala/chisel3/Module.scala` 全文搜索 `generateComponent`，确认 `class Module` 里**没有** `override def generateComponent`（只有抽象声明在 `BaseModule`）。这证明 `Module` 直接复用 `RawModule` 的收尾。
2. 在 [RawModule.scala:135-153](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L135-L153) 找到 `case id: Data =>` 分支，看清一个 `WireBinding` 的 Data 会走 [L147](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L147) 得到默认名 `_WIRE`，再由 `_namespace` 去重。
3. 写一个**示例代码**验证命名：

```scala
import chisel3._
import circt.stage.ChiselStage

class Naming extends Module {
  val io = IO(new Bundle { val out = Output(UInt(8.W)) })
  val w = Wire(UInt(8.W))   // 期望被命名为 _WIRE_0 之类
  w := 1.U
  io.out := w
}
object Main extends App {
  println(ChiselStage.emitSystemVerilog(new Naming))
}
```

**需要观察的现象**：生成的 Verilog/CHIRRTL 里出现以 `_WIRE`（或去重后形如 `_WIRE_0`）为基的内部信号名。如果想看 CHIRRTL 原文，可用 `ChiselStage.emitCHIRRTL(new Naming)`（CHIRRTL 序列化见 u4-l4）。

**预期结果**（如本地未配置 firtool，标注为「待本地验证」）：你能把 Verilog 里的信号名，反向对应到 [nameId](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L111-L156) 里的某个分支。

#### 4.4.5 小练习与答案

**练习 1**：`generateComponent` 为什么要做**两轮** `nameId`（[L163](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L163) 和 [L176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L176)）？

**参考答案**：因为 `evaluateAtModuleBodyEnd()`（[L168](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L168)）可能注册「在模块体末尾追加的硬件生成器」，这些生成器会产生**新的 id**（新端口/线）。第一轮命名已有 id，钩子跑完后再用 `_ids.view.drop(numInitialIds)` 对新增的 id 做第二轮命名。

**练习 2**：`generateComponent` 返回 `Option[Component]`，但 `RawModule` 的实现里 `_component = Some(component)` 后总是返回 `Some`。那什么时候会返回 `None`？

**参考答案**：`RawModule`/`Module` 总是生成 component。返回 `None` 的是其它 `BaseModule` 子类（如 hierarchy 体系里的 `ModuleClone`/`InstanceClone` 等「伪模块」），它们不产出独立 IR Component。`Option` 正是为这些情况设计的（见 4.1.5 练习 2）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**端到端跟踪任务**。

**任务**：写一个极简的「带使能的寄存器」模块，分别用 `Module` 和 `RawModule` 两种写法，然后**用人脑+源码**完整走一遍 `Module(new ...)` 的生命周期，最后对照生成的 Verilog 验证你的理解。

**示例代码**：

```scala
import chisel3._
import circt.stage.ChiselStage

// 写法 A：Module，自带 clock/reset
class RegEnModule extends Module {
  val io = IO(new Bundle {
    val en  = Input(Bool())
    val in  = Input(UInt(8.W))
    val out = Output(UInt(8.W))
  })
  val r = RegInit(0.U(8.W))
  when(io.en) { r := io.in }
  io.out := r
}

// 写法 B：RawModule，必须自己声明并接 clock
class RegEnRaw extends RawModule {
  val clock = IO(Input(Clock()))
  val io = IO(new Bundle {
    val en  = Input(Bool())
    val in  = Input(UInt(8.W))
    val out = Output(UInt(8.W))
  })
  val r = withClock(clock)(RegInit(0.U(8.W)))
  when(io.en) { r := io.in }
  io.out := r
}

object Main extends App {
  println(ChiselStage.emitSystemVerilog(new RegEnModule))
  println(ChiselStage.emitSystemVerilog(new RegEnRaw))
}
```

**你需要做的**：

1. 对写法 A，在脑中执行 4.3.2 的生命周期图：`Module(new RegEnModule)` → `evaluate` → 构造体（`IO` 登记端口、`RegInit` 登记寄存器命令、`when` 登记条件命令）→ `generateComponent`（给 `r`、`io.*` 命名、关闭、组装 `DefModule`）→ 父作用域挂载。
2. 对照生成的 Verilog：确认写法 A 的端口里有 `clock`/`reset`（来自 4.2 的 `IO(Input(Clock()))`），写法 B 没有（但有你自己声明的 `clock`）。
3. 解释：写法 B 里为什么必须 `withClock(clock)(...)`？（提示：回顾 4.2.5 练习 2，`RawModule` 没有隐式时钟。）
4. 进阶：在 [RawModule.scala:111-156](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L111-L156) 找到 `r`（一个 `RegInit`，绑定类型 `RegBinding`）会被赋予什么默认名。

**预期结果**（如本地未配置 firtool，则前两步标注「待本地验证」，后两步可纯靠源码完成）：你能把一段十行左右的 Chisel 代码，从「用户写出来」一路讲清到「变成 Verilog 里的某个信号名」，整条链路不靠猜。

## 6. 本讲小结

- `BaseModule`（`chisel3.experimental` 包）是所有模块的公共骨架，持有 elaboration 期全部可变状态（`_ids`/`_ports`/`_body`/`_namespace`/`_component`/`_closed`），并声明两个抽象方法 `generateComponent` 与 `initializeInParent`。
- `RawModule extends BaseModule`：不带隐式 clock/reset、支持多次 `IO()`，是「胶水/顶层/纯组合」场景的基类。
- `Module extends RawModule with ImplicitClock with ImplicitReset`：自动加 `clock`/`reset` 端口并把它们设为隐式；`RequireAsyncReset`/`RequireSyncReset`/`resetType` 可调整复位类型。
- 隐式 clock/reset 靠 `ImplicitClock`/`ImplicitReset` trait 在 Builder 上登记一个 `Delayed`（惰性盒子）引用，绕过 Scala 初始化顺序问题。
- `Module(new MyMod)` 的生命周期是：宏 `apply` → `do_apply` → `_applyImpl`（在父里挂载）→ `evaluate`（跑构造体 + `generateComponent` + 登记 component）→ `initializeInParent`。
- `generateComponent`（`RawModule` 实现，`Module` 复用）是收尾步骤：命名 → 关闭 → 校验端口 → 组装成 IR `DefModule`；`nameId` 按绑定类型分派默认名，是 Verilog 里 `_T`/`_WIRE`/`REG` 等名字的源头。

## 7. 下一步学习建议

本讲把「一个模块如何被构造并收尾成 IR」讲完了。接下来建议：

- **u3-l2 IO、方向与 Flipped 翻转**：深入 `IO()` 内部，看端口方向（`SpecifiedDirection`/`ActualDirection`）如何决定，与本讲 4.2 的 `IO(Input(Clock()))` 呼应。
- **u3-l3 / u3-l4 Connectable 与连线实现**：本讲多次提到「`:=` 登记命令」，下一站就看命令是如何被 `MonoConnect`/`BiConnect` 校验方向并登记的。
- **u4-1 Builder 全局状态机**：本讲反复出现的 `Builder.currentModule`/`Builder.components`/`Builder.State.guard` 都属于 Builder，u4 会把它作为一个整体讲透。
- **延伸阅读源码**：`core/src/main/scala/chisel3/MultiClock.scala`（`withClock`/`withReset`，理解 RawModule 里如何手动提供时钟）、`core/src/main/scala/chisel3/internal/firrtl/IR.scala`（`Component`/`DefModule`/`DefInstance`/`Port` 等 IR 节点的完整定义）。
