# 层级化设计：Definition / Instance

## 1. 本讲目标

Chisel 的传统实例化方式是 `Module(new Adder)`——每写一次，就**重新构造并细化（elaborate）一遍**这个模块。这在多数情况下没问题，但当同一个模块被实例化上百次、或你希望「先定义一次蓝图，再用蓝图造很多份实例」时，它就显得笨拙：定义无法被复用、内部信号无法被外部按名引用、跨模块的参数也无法统一管理。

`chisel3.experimental.hierarchy` 包提供的 **Definition / Instance / Instantiate** 三件套就是为了解决这个问题。它把「**定义一个模块**」和「**实例化一个模块**」这两件事在 API 层面显式分开，并配上一套「查表（lookup）」机制，让你能从外部按名访问实例的内部端口与信号。

学完本讲，你应当能够：

1. 用 `Definition(new Mod)` 把一个模块固化为「定义」，用 `Instance(defn)` 从同一份定义造出多个实例。
2. 说出 `@instantiable` / `@public` 两个注解的作用，理解它们其实是由宏在编译期生成的「查表扩展方法」。
3. 解释 `IsInstantiable`、`IsLookupable` 这两个标记 trait 各自的适用场景。
4. 理解 `Definition` 持有「原型（Proto）」、`Instance` 持有「克隆（Clone）」这一内部区分。
5. 用 `Instantiate(new Mod(...))` 这一行糖写出更简洁的实例化代码，并知道它的缓存与限制。

## 2. 前置知识

本讲是「专家层」第一篇，需要你已经掌握以下概念（对应前置讲义）：

- **Module 的细化生命周期**（u3-l1）：`Module(new MyMod)` 经宏 `do_apply → _applyImpl` 完成定制（`evaluate`）、收口（`generateComponent`）、上挂（`initializeInParent`）三步。模块构造体里的每一行（`IO` / `:=` / `when`）**只登记不施工**，真正固化发生在收口阶段。本讲的 `Definition` / `Instance` 正是建立在这套生命周期之上的更高层封装。
- **Stage / Phase 管道**（u5-l1）：`ChiselStage.emitSystemVerilog` 经 `PhaseManager` 串联 `Elaborate → … → CIRCT` 完成编译。本讲的 `Builder.build` 就是 `Elaborate` 阶段内部用来「跑一遍构造体、长出电路」的入口。
- **Data / Bundle / IO 方向**（u2、u3）：本讲会大量出现端口（`IO`）、连线（`:=`），你需要知道一个 `Data` 是「类型」还是「已绑定硬件值」。
- **Builder 全局状态机**（u4-l1）：`Builder` 在细化期维护 `currentModule`、`components` 等全局状态。本讲中 `Definition.apply` 会临时创建一个新的 `DynamicContext`，但要把产物回流到外层 Builder。

几个本讲要用到的新术语：

- **蓝图（blueprint）与实体（instance）**：硬件里的「模块定义」就像一份图纸，「实例」就是按图纸造出来的具体电路。Verilog 里对应 `module Adder ... endmodule`（定义）和 `addera inst0 (.in(...));`（实例）。
- **查表（lookup）**：从外部按名取出一个实例内部某个信号的操作。这是 hierarchy API 的核心抽象。
- **类型类（typeclass）**：Scala 里用 `implicit` 解析的一组「针对某类型提供某能力」的实现，本讲里的 `Lookupable[B]` 就是典型。

## 3. 本讲源码地图

本讲涉及的关键文件，全部位于 `core` 子项目（少量宏在 `macros` 子项目）：

| 文件 | 作用 |
| --- | --- |
| `core/.../experimental/hierarchy/core/Definition.scala` | 用户可见的 `Definition[A]` 类型与其工厂 `Definition.apply` |
| `core/.../experimental/hierarchy/core/Instance.scala` | 用户可见的 `Instance[A]` 类型与其工厂 `Instance.apply` |
| `core/.../experimental/hierarchy/Instantiate.scala` | 一行糖 `Instantiate(new Mod(...))` 与缓存去重 |
| `core/.../experimental/hierarchy/core/IsInstantiable.scala` | 标记 trait：表示「可被 Definition/Instance 包装」 |
| `core/.../experimental/hierarchy/core/IsLookupable.scala` | 标记 trait：表示「纯元数据，所有实例都相同，可原样返回」 |
| `core/.../experimental/hierarchy/core/Underlying.scala` | 内部 sealed trait：`Proto`（原型）与 `Clone`（克隆）二分 |
| `core/.../experimental/hierarchy/core/Hierarchy.scala` | `Definition` 与 `Instance` 的共同父类型，定义 `_lookup` |
| `core/.../experimental/hierarchy/core/Lookupable.scala` | 查表的类型类及其内置实现（Data/Module/Option/Tuple/Int…） |
| `core/scala-2/.../experimental/hierarchy/HierarchyPackage.scala` | 用户用的 `@instantiable` / `@public` 注解（Scala 2 版） |
| `macros/scala-2/.../internal/InstantiableMacro.scala` | `@instantiable` 宏：自动加标记、生成查表扩展方法 |
| `core/.../experimental/hierarchy/ModuleClone.scala` | `Instance` 内部用来克隆端口、指向新实例的伪模块 |
| `src/test/scala/chiselTests/experimental/hierarchy/Examples.scala` | 官方测试里的 `AddOne` / `AddTwo` 等标准示例 |

> 备注：本讲引用的 `@instantiable` / `@public` 宏实现是 Scala 2 版（`scala-2` 目录）。Scala 3 版在 `core/src/main/scala-3/.../hierarchy/` 下（`HierarchyMarker.scala`、`HierarchyLookup.scala`），机制等价，本讲不展开。

---

## 4. 核心概念与源码讲解

本讲按 5 个最小模块组织：先讲什么样的类型「可被包装」（`IsInstantiable` 与两个注解），再分别讲 `Definition`、`Instance`、`Instantiate`，最后讲把内部信号「取出来」的查表机制（`IsLookupable` 与 `Lookupable`）。

### 4.1 IsInstantiable：可实例化标记与 @instantiable / @public 宏

#### 4.1.1 概念说明

如果你打开 `Definition.apply` 和 `Instance.apply` 的签名，会发现它们都要求参数类型同时满足两个约束：

\[
T <: \text{BaseModule} \;\text{with}\; \text{IsInstantiable}
\]

也就是说，只有「**是一个模块**」且「**被标记为可实例化**」的类型，才能被 `Definition` / `Instance` 包装。`IsInstantiable` 本身只是一个空标记 trait：

```scala
// IsInstantiable.scala
trait IsInstantiable
```

[IsInstantiable.scala:10](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/IsInstantiable.scala#L10)

它「表示一个类可以从 `Instance` 中被返回」——即它的实例可以作为 `@public val` 的类型，被嵌套地查表访问（例如把一组 `Wire` 包进一个 `IsInstantiable` 容器类，再从外部逐层取出）。

但用户**几乎从不手动写 `extends IsInstantiable`**。正确做法是给类贴 `@instantiable` 注解，让宏自动帮你做两件事：

1. 给类加上 `IsInstantiable` 父类（若尚未有）；
2. 为每一个标了 `@public` 的 `val`，生成一个「查表扩展方法」。

而 `@public` 注解则标在那些你想从外部（通过 `Definition` / `Instance`）访问的字段上。

#### 4.1.2 核心流程

当编译器看到一个被 `@instantiable` 标记的类时，`@instantiable` 宏（`InstantiableMacro.impl`）会做如下改写：

```
@instantiable class Foo extends Module { @public val out = ... }
        │
        ▼  宏改写
class Foo extends Module with IsInstantiable { val out = ... }   // ① 自动加 IsInstantiable
object Foo {
  implicit class ...(d: Definition[Foo]) { def out = d._lookup(_.out) }  // ② 给 Definition 生成扩展
  implicit class ...(i: Instance[Foo])   { def out = i._lookup(_.out) }  // ③ 给 Instance 生成扩展
}
```

于是当你在父模块里写 `i.out`（其中 `i: Instance[Foo]`），Scala 编译器会：

1. 找到宏生成的那个针对 `Instance[Foo]` 的隐式类；
2. 把 `i.out` 重写成 `i._lookup(_.out)`；
3. `_lookup` 召唤隐式的 `Lookupable` 类型类，完成实际的「取信号」。

#### 4.1.3 源码精读

用户层的两个注解只是 `chisel3.internal` 包内真正注解的别名：

[HierarchyPackage.scala:17](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/experimental/hierarchy/HierarchyPackage.scala#L17) 与 [HierarchyPackage.scala:45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/experimental/hierarchy/HierarchyPackage.scala#L45) 把 `@instantiable` / `@public` 转发到内部实现。

真正的逻辑在宏里。`InstantiableMacro.impl` 处理类体，把每个 `@public val` 改写为一个查表方法：

```scala
// InstantiableMacro.scala（精简）
extensions += q"def ${aVal.name} = ___module._lookup(_.${aVal.name})"
```

[InstantiableMacro.scala:32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/InstantiableMacro.scala#L32) — 注意三重下划线前缀 `___module` 是为了避免与用户自己定义的 `val module` 冲突。

宏同时保证类继承 `IsInstantiable`，并生成两个隐式类（分别针对 `Definition` 和 `Instance`）：

```scala
// 若类尚未继承 IsInstantiable，则自动追加（InstantiableMacro.scala:67）
allParents = ... :+ tq"chisel3.experimental.hierarchy.IsInstantiable"
// 生成两份查表扩展（InstantiableMacro.scala:71-72）
q"implicit class ...(___module: Definition[Foo]) { ..$extensions }"
q"implicit class ...(___module: Instance[Foo])   { ..$extensions }"
```

[InstantiableMacro.scala:67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/InstantiableMacro.scala#L67) 自动补 `IsInstantiable`；[InstantiableMacro.scala:71-L72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/InstantiableMacro.scala#L71-L72) 生成两份隐式类。两个内部注解定义为 `StaticAnnotation`：[InstantiableMacro.scala:117-L120](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/InstantiableMacro.scala#L117-L120)。

> 关键结论：**`i.out` 不是普通字段访问，而是宏生成的 `i._lookup(_.out)` 调用**。正因如此，直接调用 `_lookup` 会被拒绝——它需要一个只能由宏提供的 `implicit MacroGenerated` 证据（见 4.5）。

#### 4.1.4 代码实践

**实践目标**：验证「不带 `@instantiable` 就无法用 `Definition` 包装」。

1. 阅读测试示例 `Examples.scala` 中的 `AddOne`（一个被 `@instantiable` 标记、`in/out/innerWire` 三个字段都标了 `@public` 的模块）。
2. 在你自己的工程里写两份等价的模块：一份带 `@instantiable`，一份不带，然后分别尝试 `Definition(new ...)`。

```scala
// 示例代码
import chisel3._
import chisel3.experimental.hierarchy.{instantiable, public, Definition, Instance}

@instantiable
class MarkedAdder extends Module {
  @public val in  = IO(Input(UInt(8.W)))
  @public val out = IO(Output(UInt(8.W)))
  out := in + 1.U
}

class UnmarkedAdder extends Module {     // 没有 @instantiable
  val in  = IO(Input(UInt(8.W)))
  val out = IO(Output(UInt(8.W)))
  out := in + 1.U
}

class Top extends Module {
  val d  = Definition(new MarkedAdder)   // ✅ 编译通过
  val i0 = Instance(d)
  // val bad = Definition(new UnmarkedAdder)  // ❌ 编译错误：UnmarkedAdder 不是 IsInstantiable
}
```

3. **需要观察的现象**：取消 `bad` 那行的注释，编译应失败，错误信息会指出 `UnmarkedAdder` 不满足 `with IsInstantiable` 约束。
4. **预期结果**：带注解的版本编译通过；不带的版本编译报错。
5. 实际编译输出**待本地验证**（需要 mill 与 Scala 编译环境）。

#### 4.1.5 小练习与答案

**练习 1**：`@instantiable` 宏给类加了哪个父类？为什么用户不必手写它？

**参考答案**：自动追加 `chisel3.experimental.hierarchy.IsInstantiable`（仅当类尚未继承时）。因为 `Definition.apply` / `Instance.apply` 都要求 `T <: BaseModule with IsInstantiable`，宏自动补齐这个标记，省去用户手写模板代码，也避免遗漏。

**练习 2**：`@public` 能不能标在 `def` 或 `private val` 上？

**参考答案**：不能。`InstantiableMacro.impl` 中对 `def` 直接报 `Cannot mark a def as @public`，对 `private`/`protected` val 报 `Cannot mark a private or protected val as @public`（见 [InstantiableMacro.scala:23-L29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/InstantiableMacro.scala#L23-L29)）。`@public` 只能用于普通 `val`。

---

### 4.2 Definition：把模块固化为「定义」（蓝图）

#### 4.2.1 概念说明

`Definition[A]` 表示「**类型 A 的一个定义**」——一份已经细化好、可以反复用来实例化的蓝图。它的类型签名是：

```scala
final case class Definition[+A] private[chisel3] (private[chisel3] val underlying: Underlying[A])
    extends IsLookupable with SealedHierarchy[A]
```

[Definition.scala:24-L26](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L24-L26) — `Definition` 是一个 `case class`，协变 `+A`，内部持有一个 `Underlying[A]`。

`Underlying` 是一个 sealed 二分类型，区分「原型」与「克隆」：

```scala
// Underlying.scala
sealed trait Underlying[+T]
final case class Clone[+T](isClone: IsClone[T]) extends Underlying[T]  // 克隆
final case class Proto[+T](proto: T)            extends Underlying[T]  // 原型
```

[Underlying.scala:6-L12](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Underlying.scala#L6-L12)

**核心结论**：`Definition` 持有的永远是 `Proto`（原型）——它就是那个被细化过一次的「真身」模块对象；而 `Instance` 持有的将是 `Clone`（克隆）。这是二者最根本的内部区别。

#### 4.2.2 核心流程

`Definition(new Adder)` 的执行流程（在父模块的构造体内调用时）：

```
Definition(new Adder)
  │
  ├── ① Builder.captureContext()              捕获外层 Builder 的设置
  ├── ② new DynamicContext(...)               造一个全新的细化账本
  ├── ③ dynamicContext.inDefinition = true    打开「正在定义」开关
  ├── ④ Builder.build(Module(proto), ctx)     在新上下文里跑一遍构造体（细化）
  ├── ⑤ 把产物回流到外层 Builder               components / annotations / layers ...
  └── ⑥ module.toDefinition                   返回 Definition(Proto(module))
```

注意第 ④ 步：它调用 `Builder.build(Module(proto), ...)`，也就是**真的把 `new Adder` 的构造体执行了一遍**——这正是「定义 = 细化一次」。第 ③ 步的 `inDefinition = true` 会影响模块命名（见下文源码精读）。

#### 4.2.3 源码精读

工厂方法 `Definition.apply` 的核心：

```scala
// Definition.scala（精简）
def apply[T <: BaseModule with IsInstantiable](proto: => T)(implicit sourceInfo: SourceInfo): Definition[T] = {
  val dynamicContext = {
    val context = Builder.captureContext()
    new DynamicContext(Nil, context.throwOnFirstError, ... )   // 用外层设置填新账本
  }
  dynamicContext.inDefinition = true                            // ① 关键开关
  val (ir, module) = Builder.build(Module(proto), dynamicContext) // ② 在新上下文里细化
  Builder.components ++= ir._circuit.components                 // ③ 产物回流外层
  Builder.annotations ++= ir._circuit.annotations
  Builder.layers ++= dynamicContext.layers
  // ... 更多回流 ...
  dynamicContext.definitions.foreach(Builder.addDefinition)
  module._circuit = Builder.currentModule
  module.toDefinition                                            // ④ 返回 Definition(Proto(module))
}
```

[Definition.scala:103-L139](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L103-L139) 是 `apply` 的完整实现。

几个要点：

- 第 [108-L128](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L108-L128) 行：通过 `Builder.captureContext()` 把外层 `DynamicContext` 的各种设置（是否遇错即抛、是否用旧位宽推断、警告过滤等，回顾 u4-l1）拷到一个**新的** `DynamicContext`。这意味着 `Definition` 的细化是在一个相对独立的账本里进行的。
- [Definition.scala:129](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L129) `inDefinition = true`：这个开关在 `Builder.build` 内部起作用——它**跳过对模块的立即命名**。源码注释说「This avoids definition name index skipping with D/I」，即避免在使用 Definition/Instance 时模块名编号跳号（让 `AddOne`、`AddOne_1`、`AddOne_2` 顺序连续，参见 [Builder.scala:1081-L1083](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1081-L1083)，命名细节留到 u7-l3）。
- [Definition.scala:130](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L130) `Builder.build(Module(proto), dynamicContext)`：注意它把 `proto` 又包了一层 `Module(proto)`。这是因为 `Builder.build` 期望一个能产出顶层模块的 thunk，`Module(proto)` 触发我们在 u3-l1 学过的模块生命周期（`evaluate → generateComponent → initializeInParent`）。
- 第 [131-L136](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L131-L136) 行：把新账本里长出来的 `components`、`annotations`、`layers`、`definitions` 等**回流到外层 Builder**——否则这个定义就成了「孤岛」，下游 CIRCT 编译时看不到它。
- [Definition.scala:138](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L138) `module.toDefinition`：返回包裹了原型的定义。

> 一个 `Definition` 可以被多个 `Instance` 共享——这正是它作为「蓝图」的价值：模块体只细化一次，生成一份 `module Adder`，后续每个实例都引用它。

#### 4.2.4 代码实践

**实践目标**：观察「同一份 `Definition` 在 CHIRRTL 里只产生一个模块定义」。

1. 阅读官方示例 `AddTwo`（`Examples.scala`），它在内部对一个 `Definition(new AddOne)` 做了两次 `Instance`：

```scala
// Examples.scala（真实仓库代码，节选自 AddTwo，第 60-70 行）
@instantiable
class AddTwo extends Module {
  @public val in  = IO(Input(UInt(32.W)))
  @public val out = IO(Output(UInt(32.W)))
  @public val definition = Definition(new AddOne)
  @public val i0: Instance[AddOne] = Instance(definition)
  @public val i1: Instance[AddOne] = Instance(definition)
  i0.in := in
  i1.in := i0.out
  out := i1.out
}
```

[Examples.scala:60-L70](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/experimental/hierarchy/Examples.scala#L60-L70)

2. 用 `ChiselStage.emitCHIRRTL(new AddTwo)` 生成 CHIRRTL 文本。
3. **需要观察的现象**：CHIRRTL 中应只出现 **一个** `module AddOne :`，但有两个实例化语句 `inst i0 of AddOne` 和 `inst i1 of AddOne`。
4. **预期结果**：模块定义只有一份；实例有多份。
5. 实际输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Definition.apply` 要新建一个 `DynamicContext`，而不是直接复用外层的？

**参考答案**：为了让「定义」的细化在一个相对隔离的账本里进行（独立的错误收集、命名空间等），但完成后又通过第 131-136 行把产物回流到外层 Builder，保证下游编译能看到。这是一种「隔离执行、统一回收」的模式。

**练习 2**：`Definition` 内部持有的是 `Proto` 还是 `Clone`？为什么？

**参考答案**：`Proto`。`Definition` 代表蓝图本身，持有的是被细化过一次的原始模块对象（真身）。`Clone` 留给 `Instance`（每个实例都是对蓝图的一次「克隆式引用」）。

---

### 4.3 Instance：从定义「实例化」出实体

#### 4.3.1 概念说明

`Instance[A]` 表示「**类型 A 的一份实例**」。它的签名与 `Definition` 几乎对称：

```scala
final case class Instance[+A] private[chisel3] (private[chisel3] val underlying: Underlying[A])
    extends SealedHierarchy[A]
```

[Instance.scala:22-L23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L22-L23) — 注意构造器里有一段断言：`Proto` 不能包一个 `Clone`（[Instance.scala:24-L27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L24-L27)）。

关键区别在于它持有的 `Underlying` 是 **`Clone`**，而不是 `Proto`。`Instance.apply` 内部用一个叫 `ModuleClone` 的「伪模块」来表达这个克隆。

#### 4.3.2 核心流程

`Instance(definition)` 的执行流程：

```
Instance(defn)
  │
  ├── ① 检查定义是否已存在/已导入（否则可能补一个 ExtModule 占位）
  ├── ② CloneModuleAsRecord(defn.proto)    克隆原型的端口，得到一个 ModuleClone
  ├── ③ clone._madeFromDefinition = true   标记「来自 Definition」
  ├── ④ 把定义里已知的 layer 注册进 Builder
  └── ⑤ new Instance(Clone(clone))         包装成 Instance（持有 Clone）
```

注意：**`Instance` 不会重新跑一遍模块构造体**（那是 `Definition` 已经做过的事）。它只是把原型的端口克隆一份、指向一个新的实例位置——就像 Verilog 里写 `inst i0 of Adder`，并不会重新定义 `module Adder`。

#### 4.3.3 源码精读

工厂方法 `Instance.apply`：

```scala
// Instance.scala（精简）
def apply[T <: BaseModule with IsInstantiable](definition: Definition[T])(
  implicit sourceInfo: SourceInfo
): Instance[T] = {
  val existingMod = Builder.definitions.view.map(_.proto).exists { ... } // ① 是否已定义/导入
  if (!existingMod) {
    val extModName = Builder.importedDefinitionMap.getOrElse(definition.proto.name, throwException(...))
    Definition(new ImportedDefinitionExtModule(extModName, definition))  // 补 ExtModule 占位
  }
  val ports = experimental.CloneModuleAsRecord(definition.proto)         // ② 克隆端口
  val clone = ports._parent.get.asInstanceOf[ModuleClone[T]]             //    取出 ModuleClone
  clone._madeFromDefinition = true                                       // ③ 标记来源
  definition.proto._moduleLayers.foreach(layer.addLayer)                 // ④ 注册 layer
  new Instance(Clone(clone))                                             // ⑤ 包装成 Instance
}
```

[Instance.scala:172-L208](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L172-L208) 是完整实现。逐点说明：

- 第 [178-L197](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L178-L197) 行：先查 `Builder.definitions`，看这个定义是否已经在当前编译单元里出现过。若没有（典型场景是「导入的外部定义」），就补一个 `ImportedDefinitionExtModule`（一个会被发射成 FIRRTL `ExtModule` 的占位），这样下游 FIRRTL 才不会报「找不到模块」。
- [Instance.scala:199](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L199) `CloneModuleAsRecord(definition.proto)`：这是整个克隆机制的核心（`CloneModuleAsRecord` 定义在 [experimental/package.scala:32-L50](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/package.scala#L32-L50)），它内部会创建一个 `ModuleClone`。
- [Instance.scala:200](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L200)：`ports._parent` 就是那个新造的 `ModuleClone`。
- [Instance.scala:207](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L207) `new Instance(Clone(clone))`：最终包装成持有 `Clone` 的实例。

`ModuleClone` 是什么？它是一个**伪模块（PseudoModule）**，不真正生成 FIRRTL 组件，而是把端口引用「重定向」到新的实例位置：

```scala
// ModuleClone.scala（精简）
private[chisel3] class ModuleClone[T <: BaseModule](val getProto: T)(implicit si: SourceInfo)
    extends PseudoModule with core.IsClone[T] {
  override def desiredName: String = getProto.name       // 模块名与原型一致
  private[chisel3] def generateComponent(): Option[Component] = {
    _component = getProto._component                      // 直接借用原型的组件
    None                                                     // 自己不产出组件
  }
  private[chisel3] def initializeInParent(): Unit = ()    // 不在父模块里做初始化
  // ...
}
```

[ModuleClone.scala:14-L16](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/ModuleClone.scala#L14-L16) 是类声明（`getProto` 指回原型）；`generateComponent` 返回 `None`（[ModuleClone.scala:29-L35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/ModuleClone.scala#L29-L35)）说明它不在 IR 里增加新模块，只是借用原型的组件并改名。`ModuleClone` 还维护一份 `ioMap`，把原型的端口映射到克隆出来的端口上（[ModuleClone.scala:38-L47](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/ModuleClone.scala#L38-L47)），这正是后续查表（4.5）能取到正确信号的依据。

**与传统 `Module(new Adder)` 的对比**：

| 维度 | `Module(new Adder)` | `Definition(new Adder)` + `Instance(defn)` |
| --- | --- | --- |
| 模块体细化次数 | 每次调用都重新细化一遍 | `Definition` 细化一次，`Instance` 不再细化 |
| 内部信号外部可见性 | 不可见（除非手动暴露） | 标了 `@public` 的字段可从外部查表访问 |
| 多份实例共享蓝图 | 否（每次独立） | 是（多 `Instance` 共享一个 `Definition`） |
| 适合场景 | 简单一次性实例化 | 大量重复实例、需要跨层访问内部节点 |

#### 4.3.4 代码实践

**实践目标**（本讲主实践任务之一）：用 `Definition + Instance` 重写一个简单加法器实例化，对比传统 `Module(new ...)` 写法，并生成 Verilog 验证等价性。

1. 写两份顶层模块，功能等价（`out = in + 2`，由两级 `+1` 串联实现）：

```scala
// 示例代码
import chisel3._
import chisel3.experimental.hierarchy.{instantiable, public, Definition, Instance}
import circt.stage.ChiselStage

@instantiable
class AddOne(val width: Int) extends Module {
  @public val in  = IO(Input(UInt(width.W)))
  @public val out = IO(Output(UInt(width.W)))
  out := in + 1.U
}

// 写法 A：传统 Module(new ...)
class TopTraditional extends Module {
  val in  = IO(Input(UInt(8.W)))
  val out = IO(Output(UInt(8.W)))
  val a = Module(new AddOne(8))
  val b = Module(new AddOne(8))
  a.in := in
  b.in := a.out
  out  := b.out
}

// 写法 B：Definition + Instance（共享一份定义）
class TopHierarchy extends Module {
  val in  = IO(Input(UInt(8.W)))
  val out = IO(Output(UInt(8.W)))
  val defn = Definition(new AddOne(8))   // 蓝图只造一次
  val a = Instance(defn)
  val b = Instance(defn)
  a.in := in
  b.in := a.out
  out  := b.out
}

// 触发 Verilog 生成
object Demo extends App {
  println(ChiselStage.emitSystemVerilog(new TopTraditional))
  println(ChiselStage.emitSystemVerilog(new TopHierarchy))
}
```

2. 分别对两份顶层执行 `ChiselStage.emitSystemVerilog(...)`。
3. **需要观察的现象**：两份 Verilog 的**行为完全等价**（都是两级 `+1`），结构上都应包含一个 `AddOne` 子模块和两个实例（`a`、`b`）。
4. **预期结果**：两份 Verilog 在端口与组合逻辑上等价；`TopHierarchy` 的内部 `AddOne` 定义只出现一次。
5. 实际输出**待本地验证**。

> 选做：把第 4.2.4 节里 `AddTwo` 的 CHIRRTL 也打印出来对比，确认 `inst i0 of AddOne` / `inst i1 of AddOne` 都引用同一个 `module AddOne`。

#### 4.3.5 小练习与答案

**练习 1**：`Instance.apply` 调用了 `CloneModuleAsRecord(definition.proto)`。这一步会重新执行 `AddOne` 的构造体吗？

**参考答案**：不会。`Definition` 已经在 `Builder.build(Module(proto), ...)` 里把构造体跑过一遍了。`Instance` 只是用 `ModuleClone` 克隆端口、把引用重定向到新的实例位置（对应 Verilog 的 `inst ... of ...`），并不重新细化。

**练习 2**：`ModuleClone` 的 `generateComponent` 为什么返回 `None`？

**参考答案**：因为 `ModuleClone` 是伪模块，它本身不对应一个新的 FIRRTL 模块定义，而是直接借用原型已生成的组件（`_component = getProto._component`）。返回 `None` 表示「我不向 IR 贡献新组件」，避免同一个模块被重复发射。

---

### 4.4 Instantiate：一行糖与缓存去重

#### 4.4.1 概念说明

每次都要先 `Definition(new ...)` 再 `Instance(defn)` 略显啰嗦。`Instantiate(new Mod(...))` 把这两步合一：它返回一个 `Instance[Mod]`，背后等价于「先取（或创建）定义，再实例化」。更重要的是，它带了一套**缓存去重**：如果两处用完全相同的参数 `Instantiate` 同一个模块，会复用同一份 `Definition`，从而只生成一个模块定义。

```scala
val i0: Instance[MyModule] = Instantiate(new MyModule(arg1, arg2))
```

[Instantiate.scala:13-L34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L13-L34) 的 scaladoc 给出了基本用法，并点明了三条限制（见 4.4.3）。

#### 4.4.2 核心流程

```
Instantiate(new Mod(args))
        │  （宏把构造调用拆成 参数args 与 构造函数f）
        ▼
_instanceImpl(args, f, tt)
        │
        ├── _definitionImpl(args, f, tt)
        │       ├── 构造 CacheKey（含装箱后的 args、类型 tt、模块前缀、elideBlocks）
        │       └── Builder.contextCache.getOrElseUpdate(key, Definition.apply(f(args)))
        │                              └─ 命中则复用，未命中才真正造定义
        └── Instance.apply(得到的 Definition)
```

缓存的关键在于：`Definition.apply` 只在**首次**遇到某组参数时执行；之后命中缓存就直接复用那一份定义。

#### 4.4.3 源码精读

两个内部实现方法：

```scala
// Instantiate.scala（精简）
protected def _instanceImpl[K, A <: BaseModule](args: K, f: K => A, tt: Any)(
  implicit sourceInfo: SourceInfo
): Instance[A] = Instance.apply(_definitionImpl(args, f, tt))(sourceInfo)

protected def _definitionImpl[K, A <: BaseModule](args: K, f: K => A, tt: Any): Definition[A] = {
  val modulePrefix = Module.currentModulePrefix
  Builder.contextCache
    .getOrElseUpdate(
      CacheKey[A](boxAllData(args), tt, List(modulePrefix), Builder.elideLayerBlocks), {
        Definition.apply(f(args))(UnlocatableSourceInfo)   // 仅未命中时才造定义
      }
    )
    .asInstanceOf[Definition[A]]
}
```

[Instantiate.scala:100-L106](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L100-L106) 是 `_instanceImpl`；[Instantiate.scala:109-L124](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L109-L124) 是 `_definitionImpl`。

缓存键的设计值得细看。`CacheKey` 包含：装箱后的参数 `boxAllData(args)`、类型标记 `tt`、模块前缀、是否裁剪 layer 块：

```scala
private case class CacheKey[A <: BaseModule](args: Any, tt: Any, modulePrefix: List[String], elideBlocks: Boolean)
    extends BuilderContextCache.Key[Definition[A]]
```

[Instantiate.scala:97-L98](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L97-L98)

难点在于参数里可能含 `Data`（比如 `new Foo(myWire)`）。`Data` 默认用**引用相等**比较，但两个内容相同的 `UInt(8.W)` 类型理应命中缓存。于是 `Instantiate` 用一个 `DataBox` 把 `Data` 转成**结构相等**：

```scala
// Instantiate.scala（精简）
private class DataBox(private val d: Data) {
  override def hashCode: Int = convertDataForHashing(d).hashCode
  override def equals(that: Any): Boolean = that match {
    case that: DataBox =>
      if (this.d.isLit) that.d.isLit && (this.d.litValue == that.d.litValue) && sameTypes
      else if (isSynthesizable(this.d)) this.d.equals(that.d)
      else sameTypes && !isSynthesizable(that.d)
    case _ => false
  }
}
```

[Instantiate.scala:44-L84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L44-L84) 是 `DataBox` 全貌；`boxAllData` 递归把参数里的 `Data`/`Product`/`Iterable` 全部装箱（[Instantiate.scala:87-L93](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L87-L93)）。

还有两个细节：

- [Instantiate.scala:120](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L120) 用 `UnlocatableSourceInfo` 造定义：因为定义可能被多处复用，源定位信息会不稳定，故统一用「无定位」源信息。
- scaladoc 列出的三条限制（[Instantiate.scala:28-L34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/Instantiate.scala#L28-L34)）：① 内部类（inner class）模块的缓存不生效（`WeakTypeTag` 不一致）；② **不能用命名参数**传模块构造函数（只能位置参数）；③ 用户自定义的、包装了 `Data` 的类型用默认引用相等，不会命中缓存。

#### 4.4.4 代码实践

**实践目标**：体会 `Instantiate` 的缓存去重——用相同参数 `Instantiate` 同一模块两次，应只生成一份定义。

1. 写如下对比（接 4.3.4 的 `AddOne`）：

```scala
// 示例代码
class TopInstantiate extends Module {
  val in  = IO(Input(UInt(8.W)))
  val out = IO(Output(UInt(8.W)))
  val a = Instantiate(new AddOne(8))   // 注意：位置参数，不能写 new AddOne(width = 8)
  val b = Instantiate(new AddOne(8))   // 参数相同 → 复用同一 Definition
  a.in := in
  b.in := a.out
  out  := b.out
}
```

2. 用 `ChiselStage.emitCHIRRTL(new TopInstantiate)` 生成 CHIRRTL。
3. **需要观察的现象**：CHIRRTL 中只有一个 `module AddOne :`，两个实例 `a`、`b` 都引用它——和 4.2.4 手写 `Definition + Instance` 的结果一致。
4. **预期结果**：缓存命中，定义去重。
5. 实际输出**待本地验证**。

> **实践变体**：把其中一处改成 `Instantiate(new AddOne(4))`（参数不同），观察此时生成 `module AddOne`（8 位）与 `module AddOne_1`（4 位）两份定义——因为 `CacheKey` 不同、缓存未命中。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Instantiate` 要用 `DataBox` 而不是直接把 `Data` 放进 `CacheKey`？

**参考答案**：因为 `Data` 默认用引用相等（`eq`）判等和默认 `hashCode`，两个内容相同但对象不同的 `UInt(8.W)` 类型会被判不等、无法命中缓存。`DataBox` 改用结构相等（类型相同且字面量相同），让「同样的参数」能正确复用定义。

**练习 2**：下面哪种写法 `Instantiate` 的缓存可能失效？

- (a) `Instantiate(new AddOne(8))`
- (b) `Instantiate(new AddOne(width = 8))`
- (c) 在方法内定义的内部类 `class Adder extends Module`，再 `Instantiate(new Adder)`

**参考答案**：(b) 和 (c) 都有问题。(b) 违反「不能命名参数」的限制；(c) 是内部类，`WeakTypeTag` 每次不同，缓存无法命中（见 scaladoc 三条限制）。(a) 是正确的位置参数用法。

---

### 4.5 IsLookupable 与 Lookupable：@public 字段如何被「查表」返回

#### 4.5.1 概念说明

前几节我们一再看到 `_lookup`，但没说清它到底怎么把 `i.in`（`in` 是实例**内部**的端口）变成一个**在当前父模块上下文中可用的外部信号**。这是 hierarchy API 最巧妙的部分，由两个东西配合实现：

1. **`Lookupable[B]` 类型类**：定义「如何把类型 `B` 的值从原型上下文『搬』到调用者上下文」。Chisel 为常见类型（`Data`、`BaseModule`、`MemBase`、`Option`、`Either`、各种 `Tuple`、`Iterable`、以及 `Int/String/Boolean` 等基础类型）都内置了实现。
2. **`IsLookupable` 标记 trait**：给那些「**所有实例都相同**」的纯元数据（如参数 case 类）用。被标为 `IsLookupable` 的值在查表时**原样返回**，不做任何克隆。

#### 4.5.2 核心流程

当你写 `i0.in`（`i0: Instance[AddOne]`，`in` 是 `@public val in: UInt`），展开与执行过程是：

```
i0.in
  │  ① 宏生成的隐式类把它改写为
  ▼
i0._lookup(_.in)
  │  ② 召唤隐式 Lookupable[UInt]，调用
  ▼
lookup.instanceLookup(_.in, i0)
  │  ③ 对 Data 走 lookupData 分支
  ▼
doLookupData(proto的in端口, cache, ioMap, getInnerDataContext)
  │  ④ 把原型端口「克隆」到当前上下文，缓存结果
  ▼
返回当前上下文中对应的 UInt 信号  →  你可以用它做 a.in := in
```

而对 `IsLookupable` 类型的字段（如 `val p: Parameters`），第 ③ 步走的是 `isLookupable` 分支——直接 `that(hierarchy.proto)`，原样返回原型里的值，不克隆。

#### 4.5.3 源码精读

`_lookup` 定义在 `Hierarchy`、`Definition`、`Instance` 三处（后两者override）。它要求两个隐式参数：`Lookupable[B]` 与 `MacroGenerated`：

```scala
// Hierarchy.scala
def _lookup[B, C](that: A => B)(
  implicit lookup: Lookupable[B], macroGenerated: chisel3.internal.MacroGenerated
): lookup.C
```

[Hierarchy.scala:38-L43](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Hierarchy.scala#L38-L43) 是共同声明；`Definition._lookup` 转发到 `lookup.definitionLookup`（[Definition.scala:42-L49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L42-L49)），`Instance._lookup` 转发到 `lookup.instanceLookup`（[Instance.scala:60-L67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Instance.scala#L60-L67)）。

> `macroGenerated: MacroGenerated` 这个隐式参数是「门禁」——它只能由宏生成的隐式类提供（宏会在类里注入 `implicit val mg: MacroGenerated`，见 [InstantiableMacro.scala:15](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/InstantiableMacro.scala#L15)）。所以你**不能**手写 `i0._lookup(_.in)`，只能用 `@public` 触发宏改写。这就是 DefinitionSpec 测试 (0.b) 所验证的「访问 macro-only API 会报错」。

`Lookupable` 类型类的核心接口：

```scala
// Lookupable.scala（精简）
trait Lookupable[-B] {
  type C                                       // 查表返回类型（可能与 B 不同）
  def instanceLookup[A](that: A => B, instance: Instance[A]): C
  def definitionLookup[A](that: A => B, definition: Definition[A]): C
}
```

[Lookupable.scala:42-L70](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L42-L70) 是 trait 定义。注意 `type C`：对大多数类型 `C = B`（用 `type Simple[B] = Aux[B, B]`，[Lookupable.scala:87](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L87)）；但对「裸 `BaseModule`」查表，返回的是 `Instance[B]`（`C ≠ B`），这是历史遗留（已被 deprecate，建议用 `.toInstance`）。

最关键的内置实现是 `lookupData`（处理所有 `Data`）：

```scala
// Lookupable.scala（精简）
implicit def lookupData[B <: Data](implicit sourceInfo: SourceInfo): Simple[B] = new Lookupable[B] {
  type C = B
  def instanceLookup[A](that: A => B, instance: Instance[A]): C = {
    val ret = that(instance.proto)                 // 先从原型取出该字段
    val ioMap = getIoMap(instance)                 // 拿到 端口映射
    doLookupData(ret, instance.cache, ioMap, instance.getInnerDataContext)  // 克隆到当前上下文
  }
  // definitionLookup 类似，但 ioMap = None
}
```

[Lookupable.scala:449-L484](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L449-L484) 是 `lookupData`。`doLookupData`（[Lookupable.scala:215-L237](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L215-L237)）做三件事：若该 `Data` 还未绑定（纯类型）原样返回；若在 `ioMap` 里命中直接用克隆端口；否则用 `cloneDataToContext` 把它克隆进当前上下文，并写进 `cache` 以免重复克隆。这里的 `cache` 就是 `Hierarchy` 上那个 `HashMap[Data, Data]`（[Hierarchy.scala:21](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Hierarchy.scala#L21)），保证多次 `i0.in` 返回同一个信号。

而 `IsLookupable` 走的是完全不同的、最简单的分支：

```scala
// Lookupable.scala
def isLookupable[X]: Simple[X] = new LookupableImpl[X] {
  type C = X
  override protected def impl[A](that: A => X, hierarchy: Hierarchy[A]): C = that(hierarchy.proto)  // 原样返回
}
// ...
implicit def lookupIsLookupable[B <: IsLookupable](implicit sourceInfo: SourceInfo): Simple[B] = isLookupable[B]
implicit val lookupString: Simple[String] = isLookupable[String]
implicit val lookupInt:    Simple[Int]    = isLookupable[Int]
// ... Boolean, Long, Double, BigInt ...
```

[Lookupable.scala:93-L97](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L93-L97) 是 `isLookupable` 工厂；[Lookupable.scala:684-L697](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L684-L697) 把 `IsLookupable` 与所有基础类型都接到这个最简分支。

`IsLookupable` 的 scaladoc 把它的语义讲得很清楚——**只有当返回的元数据对所有实例都完全相同时才该用它**：

```scala
// IsLookupable.scala 给的例子
case class Params(debugMessage: String) extends IsLookupable
class MyModule(p: Params) extends Module { printf(p.debugMessage) }
val myParams = Params("Hello World")
val definition = Definition(new MyModule(myParams))
val i0 = Instance(definition)
val i1 = Instance(definition)
require(i0.p == i1.p)   // p 能被访问，只因它 extends IsLookupable
```

[IsLookupable.scala:5-L25](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/IsLookupable.scala#L5-L25)

> 三类字段查表语义对照：
> - **`Data` / `BaseModule` / `MemBase`**：克隆到调用者上下文（不同实例得到不同信号）。
> - **`IsLookupable` / 基础类型（`Int/String/...`）**：原样返回（对所有实例相同）。
> - **容器（`Option/Either/Tuple/Iterable`）**：递归对内部元素套用上述规则。

#### 4.5.4 代码实践

**实践目标**：对比「`Data` 字段被克隆」与「`IsLookupable` 参数被原样返回」。

1. 参考官方示例 `UsesParameters`（一个带 `Parameters` 参数的模块）：

```scala
// Examples.scala（真实仓库代码，第 196-201 行）
case class Parameters(string: String, int: Int) extends IsLookupable
@instantiable
class UsesParameters(p: Parameters) extends Module {
  @public val y = p          // IsLookupable 类型
  @public val x = Wire(UInt(3.W))   // Data 类型
}
```

[Examples.scala:196-L201](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/experimental/hierarchy/Examples.scala#L196-L201)

2. 在父模块里造两个实例，分别访问 `.x` 与 `.y`：

```scala
// 示例代码
class Top extends Module {
  val defn = Definition(new UsesParameters(Parameters("hi", 42)))
  val i0 = Instance(defn)
  val i1 = Instance(defn)
  // x 是 Data：两次访问得到两个不同信号（克隆进当前上下文）
  val w = Wire(UInt(3.W))
  w := i0.x
  // y 是 IsLookupable：原样返回，i0.y == i1.y == Parameters("hi", 42)
  printf(i0.y.string)        // 访问参数里的字段
}
```

3. **需要观察的现象**：`i0.y` 与 `i1.y` 是同一个不可变 `Parameters` 对象（`==` 成立）；而 `i0.x` 与 `i1.x` 是两个不同的 `UInt` 信号（分别指向实例 `i0`、`i1` 的内部线网）。
4. **预期结果**：`IsLookupable` 字段对所有实例相同；`Data` 字段按实例克隆。
5. 实际行为**待本地验证**（可用一个最小 ScalaTest 断言 `i0.y == i1.y`）。

#### 4.5.5 小练习与答案

**练习 1**：为什么直接写 `i0._lookup(_.in)` 会编译报错，而 `i0.in`（`in` 标了 `@public`）可以？

**参考答案**：`_lookup` 需要一个 `implicit MacroGenerated` 证据，该证据只由 `@instantiable` 宏生成的隐式类内部提供（宏注入 `implicit val mg: MacroGenerated`）。直接调用 `_lookup` 时无法从隐式作用域找到这个证据，故报错；而 `@public` 触发宏把 `i0.in` 改写进那个隐式类内部，证据可用。

**练习 2**：把一个会随实例变化的硬件相关值标成 `IsLookupable` 是否安全？

**参考答案**：不安全。`IsLookupable` 的语义是「该值对所有实例完全相同、原样返回」。若该值其实随实例不同（例如包含对某实例内部信号的引用），用 `IsLookupable` 会返回原型的值而非当前实例的对应物，导致逻辑错误。带硬件引用的值应让其字段类型走 `Data`/`Instance` 分支（克隆），或为自定义类型实现 `Lookupable`（用 `Lookupable.productN` 工厂，见 [Lookupable.scala:101-L163](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L101-L163)）。

---

## 5. 综合实践

把本讲五个最小模块串起来，完成下面这个「带参数的流水线加法器」小任务。

**需求**：实现一个 `AddPipeline(width, depth)`，内部串联 `depth` 个 `AddOne(width)`，输入到输出逐级 `+1`。要求：

1. `AddOne` 用 `@instantiable` + `@public` 暴露 `in/out`；
2. 用 `Instantiate` 创建这 `depth` 个实例（体会缓存去重——同 `width` 的 `AddOne` 只应生成一份定义）；
3. 把一个 `Parameters`（`extends IsLookupable`）作为构造参数传入，并从外部通过 `@public` 访问它；
4. 生成 CHIRRTL 与 SystemVerilog，验证：模块定义只有一份（去重成功）、实例数量等于 `depth`、行为是 `out = in + depth`。

```scala
// 示例代码
import chisel3._
import chisel3.experimental.hierarchy.{instantiable, public, Instantiate}
import circt.stage.ChiselStage

case class PipeParams(stages: Int) extends chisel3.experimental.hierarchy.IsLookupable

@instantiable
class AddOne(val width: Int) extends Module {
  @public val in  = IO(Input(UInt(width.W)))
  @public val out = IO(Output(UInt(width.W)))
  out := in + 1.U
}

@instantiable
class AddPipeline(val width: Int, params: PipeParams) extends Module {
  val in  = IO(Input(UInt(width.W)))
  val out = IO(Output(UInt(width.W)))
  @public val cfg = params                       // IsLookupable：外部可读、原样返回

  val stages = (0 until params.stages).map { _ =>
    Instantiate(new AddOne(width))               // 相同 width → 缓存命中，共用一份定义
  }
  stages.head.in := in
  stages.zip(stages.tail).foreach { case (a, b) => b.in := a.out }
  out := stages.last.out
}

class Top extends Module {
  val in  = IO(Input(UInt(8.W)))
  val out = IO(Output(UInt(8.W)))
  val pipe = Module(new AddPipeline(8, PipeParams(4)))   // out = in + 4
  pipe.in := in
  out     := pipe.out
}

object Demo extends App {
  println(ChiselStage.emitCHIRRTL(new Top))
  println(ChiselStage.emitSystemVerilog(new Top))
}
```

**验证清单**：

1. CHIRRTL 中 `module AddOne :` 只出现一次，但有 4 个 `inst ... of AddOne`。
2. `pipe.cfg.stages` 从外部可读且等于 4（`IsLookupable` 原样返回）。
3. SystemVerilog 行为：`out = in + 4`。
4. 把 `PipeParams(4)` 改成 `PipeParams(2)`，重新生成，确认实例数随之变化。

实际运行结果**待本地验证**（需要 mill 构建环境与 firtool）。如果你暂时无法运行，可以改为**源码阅读型实践**：对照 `Examples.scala` 里的 `AddTwoParameterized`（[Examples.scala:82-L91](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/experimental/hierarchy/Examples.scala#L82-L91)），画出它「接收一个 `Int => Seq[Instance[...]]` 函数来动态生成实例链」的调用图，并说明为什么这种「把实例化策略作为高阶函数参数」的写法只有借助 Definition/Instance 的查表机制才能成立。

## 6. 本讲小结

- **`@instantiable` / `@public` 是宏**：前者自动给类加 `IsInstantiable` 父类，后者把被标字段改写成针对 `Definition`/`Instance` 的 `_lookup` 扩展方法——所以 `i.in` 其实是宏生成的查表调用。
- **`Definition` 持 `Proto`、`Instance` 持 `Clone`**：`Definition(new Mod)` 在隔离的 `DynamicContext` 里把模块体细化一次（蓝图）；`Instance(defn)` 不再细化，只用 `ModuleClone` 克隆端口、指向新实例位置（实体），可被多份实例共享。
- **`Instantiate` 是带缓存的糖**：`Instantiate(new Mod(args))` = 取/造 `Definition`（按参数结构去重，靠 `DataBox` 把 `Data` 转成结构相等）+ `Instance`；不能用命名参数、内部类缓存失效。
- **查表靠 `Lookupable` 类型类**：`_lookup` 召唤 `Lookupable[B]`，`Data`/`Module`/`Mem` 被克隆进调用者上下文（每实例不同），结果缓存在 `Hierarchy.cache`；而 `IsLookupable` 与基础类型（`Int/String/...`）原样返回（所有实例相同）。
- **`_lookup` 的 `MacroGenerated` 门禁**：直接调用 `_lookup` 会因缺少隐式证据而编译失败，强制用户走 `@public`——这保证了查表只发生在宏可控的位置。
- **与传统 `Module(new ...)` 的本质差异**：传统方式每次都重新细化、内部不可见；hierarchy 方式定义一次、可多份实例化、内部 `@public` 字段可被外部按名访问，适合大规模重复结构与需要跨层引用的设计。

## 7. 下一步学习建议

本讲是单元 8「高级机制与扩展点」的开篇。建议接着学习：

1. **u8-l2 DataView**：`Lookupable` 在查表时大量使用了 `reify`、`isView`、`cloneViewToContext`（见 [Lookupable.scala:299-L341](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Lookupable.scala#L299-L341)），这些正是 DataView 机制。学完 DataView 你会真正看懂「把一种 Data 类型零拷贝视图成另一种」如何与 hierarchy 协同。
2. **u8-4 Properties（对象模型）**：`Definition`/`Instance` 的 `apply` 签名里反复出现 `BaseModule with IsInstantiable`，而 `properties.Class` 恰是另一类 `IsInstantiable`。两者会合流，建议结合阅读 `core/.../properties/Class.scala`。
3. **u7-3 Namer 与 Identifier**：本讲提到的 `inDefinition` 开关如何影响模块名编号、`ModuleClone.setRefAndPortsRef` 如何给实例命名，都需要命名管线知识，建议回顾该讲。
4. **延伸阅读源码**：`InstanceSpec.scala` 与 `InstantiateSpec.scala` 包含大量边界用例（导入定义、BlackBox、Analog、嵌套 `IsInstantiable` 容器），是检验你是否真正理解查表克隆机制的最佳材料。
