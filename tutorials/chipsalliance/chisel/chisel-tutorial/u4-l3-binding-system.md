# Binding 系统：从类型到硬件值

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清一个 `Data` 对象身上 **「类型（type）」与「硬件值（hardware）」** 的本质区别，以及 Chisel 用什么字段来区分二者。
- 读懂 `core/src/main/scala/chisel3/internal/Binding.scala` 里的 `Binding` trait 层级，并能按「端口 / 节点 / 寄存器 / 线网 / 字面量」对号入座。
- 理解 `binding_=` 为什么是**一次性写入**，以及 `requireIsChiselType` / `requireIsHardware` 这两道关卡是如何借助 `isSynthesizable` 实现的。
- 跟踪一条完整链路：从用户写下 `5.U`、`Wire(...)`、`IO(...)`、`Reg(...)`，到这些构造在源码里分别调用 `bind(哪个 Binding 子类)`。
- 在自己的模块里通过 `.toString` 观察到一个信号的 binding 类别，并把它对应到源码中的某个 `Binding` 子类。

## 2. 前置知识

本讲承接 **u2-l1（Data 抽象基类与类型层级）** 与 **u4-l1（Builder 全局状态机）**，默认你已经知道：

- **Data** 是所有硬件信号类型的根抽象；同一个类（如 `UInt`）既能当「类型模板」用，也能当「接线后的硬件值」用。
- **elaboration（细化）**：运行 Scala 构造体「长出」电路的过程；期间 `Builder` 维护全局状态。
- **「只登记不施工」**：`IO`/`Wire`/`Reg`/`when` 等调用本身不生成硬件，而是把命令登记进当前模块。

如果你还没建立「同一对象在绑定前后是两种身份」的直觉，本讲正是要把这层窗户纸捅破。先记住一句话：

> **一个 `Data` 是「类型」还是「硬件值」，不取决于它的 Scala 类，而取决于它身上有没有挂一个 `Binding`。**

这里的 `Binding` 就是本讲的主角。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [core/src/main/scala/chisel3/internal/Binding.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala) | `Binding` 及其全部子类的定义，是本讲的核心。 |
| [core/src/main/scala/chisel3/Data.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala) | `Data` 根抽象里存放 `_bindingVar`、`binding_=`、`isSynthesizable`、`bind(...)` 声明等。 |
| [core/src/main/scala/chisel3/Element.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala) | 叶子类型 `Element.bind` 的实现：一次性写入 binding 与方向。 |
| [core/src/main/scala/chisel3/Aggregate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala) | `Vec.bind` / `Record.bind` 的递归绑定：把同一个 `ChildBinding` 分发给所有子元素。 |
| [core/src/main/scala/chisel3/experimental/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/package.scala) | `requireIsHardware` / `requireIsChiselType` 两道检查关卡。 |
| [core/src/main/scala/chisel3/UIntFactory.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/UIntFactory.scala) | `UInt` 字面量工厂 `.Lit`，字面量绑定的起点之一。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | `LitArg.bindLitArg`：把字面量绑成 `ElementLitBinding` 的确切位置。 |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | `pushOp`：原语运算结果被绑成 `OpBinding` 的位置。 |

---

## 4. 核心概念与源码讲解

### 4.1 Data 的 binding 字段：类型与硬件值的分水岭

#### 4.1.1 概念说明

在 Chisel 里，下面两行都产出 `UInt`：

```scala
val t = UInt(8.W)        // 这是一个「类型模板」
val w = Wire(UInt(8.W))  // 这是一个已经接好线的「硬件值」
```

二者在 Scala 层面都是 `UInt` 类的实例，但它们的「身份」完全不同：

- `t` 是**纯类型（Chisel type）**：它描述「8 位无符号」这种形状，还不能参与综合，不能被 `:=` 赋值，只能用来当 `Wire`/`IO`/`Reg` 的模板。
- `w` 是**硬件值（hardware）**：它已经在某个模块里落了地，可以被读写、可以综合成 Verilog 里的一根线。

Chisel 用 `Data` 身上一个叫 **binding** 的字段来区分这两种身份：

- `binding` 为 `None`（实现里是 `null`）→ 纯类型。
- `binding` 为 `Some(...)` → 已绑定的硬件值，且 `Some` 里装的那个对象会告诉你**它是哪种硬件**（端口？线网？寄存器？字面量？运算结果？）。

所以「绑定（binding）」做两件事：① 给一个 `Data` 盖章「你现在是硬件了」；② 记录「你是哪一种硬件、属于哪个模块」。这正是源码注释里写的那句——binding stores information about this node's position in the hardware graph（binding 记录该节点在硬件图里的位置）。

#### 4.1.2 核心流程

把「类型 → 硬件值」的转身抽象成下面这个状态机：

```text
        ┌──────────────┐   某个工厂调用 bind(XXXBinding(...))
        │  纯类型       │ ─────────────────────────────────▶ ┌──────────────┐
        │ binding=None │                                     │  硬件值       │
        └──────────────┘                                     │ binding=Some │
                                                             └──────────────┘
```

关键规则有三条：

1. **单向、一次性**：`binding_=` 一旦写入，再次写入会抛 `RebindingException`。也就是说一个 `Data` 终其一生只能从「类型」变成「硬件值」一次，不能反复横跳。
2. **检查关卡**：`requireIsChiselType` 要求 `binding` 为空；`requireIsHardware` 要求 `binding` 非空（更精确地说是 `isSynthesizable`）。这就解释了为什么 `Wire(5.U)` 这种「把硬件值当类型模板」的写法会报错。
3. **信息载体**：`Some(...)` 里那个 `Binding` 子类记录「你是哪种硬件」，供后续连线算法（u3-l4 的 MonoConnect/BiConnect）、可见性检查、序列化使用。

#### 4.1.3 源码精读

先看 `Data` 里这个字段本身。它用了一个可空的 `var` 来省内存，外面用 `Option` 包装：

[Data.scala:424-425](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L424-L425) —— 用 `null` 占位、`Option(_bindingVar)` 暴露，注释写着「using nullable var for better memory usage」。

写入入口 `binding_=`，注意里面的「重绑即报错」逻辑：

[Data.scala:428-433](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L428-L433) —— `if (_binding.isDefined) throw RebindingException(...)`，这就是「一次性写入」的来源。

> 顺带一提，方向的写入 `direction_=` 也有同样的保护，见 [Data.scala:401-406](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L401-L406)。binding 与 direction 都是「结算一次、终身不变」。

再看判定「是不是硬件」的 `isSynthesizable`：

[Data.scala:439-443](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L439-L443) —— 核心逻辑：

```scala
private[chisel3] final def isSynthesizable: Boolean = _binding.map {
  case ChildBinding(parent) => parent.isSynthesizable
  case _: TopBinding => true
  case (_: SampleElementBinding[_] | _: MemTypeBinding[_] | _: FirrtlMemTypeBinding) => false
}.getOrElse(false)
```

读法：`_binding` 为 `None`（纯类型）→ `getOrElse(false)` → **不是**硬件；为 `Some(TopBinding)` → **是**硬件。这就把「binding 是否存在」直接翻译成了「能否综合」。

最后看两道关卡如何用它：

[experimental/package.scala:54-64](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/package.scala#L54-L64) —— `requireIsHardware`：`if (!node.isSynthesizable)` 就抛 `ExpectedHardwareException`，并贴心提示「Perhaps you forgot to wrap it in Wire(_) or IO(_)?」。

[experimental/package.scala:87-92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/package.scala#L87-L92) —— `requireIsChiselType`：反着来，`if (node.isSynthesizable)` 就抛 `ExpectedChiselTypeException`。

于是 `Wire`、`IO`、`Vec`、`Reg` 这些工厂方法的第一行几乎都是 `requireIsChiselType(t)`，确保你传进来的是模板而不是已经接好的硬件值；而 `:=`、`<>`、运算符内部则是 `requireIsHardware(this)`，确保参与运算的都是已绑定硬件。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次 `RebindingException`，体会「binding 只能写一次」。

**操作步骤**（示例代码，仅供理解机制，正常开发不会这么写）：

```scala
// 假设在一个 Module 内
import chisel3._
import chisel3.internal.binding.WireBinding
val w = Wire(UInt(8.W))   // w 现在 binding = Some(WireBinding(...))
// 反射式地再次绑定（演示用，private[chisel3] 在库外不可直接调）
// w.bind(WireBinding(...)) // <- 若能调用，会抛 RebindingException
```

由于 `bind` 是 `private[chisel3]`，库外无法直接二次调用，因此更实际的观察方式是：

1. 打开 [Data.scala:428-433](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L428-L433)，确认 `binding_=` 的守卫逻辑。
2. 打开 [experimental/package.scala:54-92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/package.scala#L54-L92)，把 `requireIsHardware` 与 `requireIsChiselType` 的判定条件抄下来。

**需要观察的现象**：两道关卡都把判定最终归结到 `isSynthesizable`，而 `isSynthesizable` 又归结到「`_binding` 是否为 `Some`」。

**预期结果**：你能用一句话回答「为什么 `Wire(5.U)` 会报 `must be a Chisel type, not hardware`」——因为 `5.U` 的 `_binding` 已经是 `Some(ElementLitBinding(...))`，`isSynthesizable` 为 `true`，于是 `requireIsChiselType` 抛错。

#### 4.1.5 小练习与答案

**练习 1**：`UInt(8.W)` 和 `Wire(UInt(8.W))` 的 `_binding` 分别是什么？

> **答案**：前者是 `null`（即 `None`），是纯类型；后者是 `Some(WireBinding(enclosure, parentBlock))`，是硬件值。

**练习 2**：为什么 `binding_=` 要设计成「只能写一次」？

> **答案**：因为 binding 记录的是该信号在硬件图中的身份与归属（属于哪个模块、是哪种硬件）。如果允许反复改写，同一个对象就可能先后变成「端口」「寄存器」「线网」，这会破坏后续连线、命名、可见性检查的所有不变量；一次性写入让身份在 elaboration 期间恒定，错误能被尽早、稳定地检出。

---

### 4.2 Binding 的分类体系：一套 trait 拼装

#### 4.2.1 概念说明

`Binding.scala` 里并没有一个扁平的「大枚举」，而是用一组 **trait 拼装（mixin composition）** 来描述绑定。每个具体绑定类（如 `PortBinding`、`WireBinding`）都是若干 trait 的组合。理解了这几个 trait，就理解了全部子类的共性。

先记住这几个维度：

| trait | 含义 | 关键方法 |
|-------|------|----------|
| `Binding` | 所有绑定的根 | `location: Option[BaseModule]`（这信号住在哪个模块） |
| `TopBinding` | 「顶层」绑定，代表真正的硬件（而非指向父节点的指针） | —— |
| `UnconstrainedBinding` | **不受模块边界约束**，任何模块都能读它（典型：字面量） | `location = None` |
| `ConstrainedBinding` | **受模块边界约束**，只能在特定模块被读写 | `enclosure: BaseModule`、`location = Some(enclosure)` |
| `ReadOnlyBinding` | 只读，不能被（重新）赋值 | 连线时被拦 |
| `BlockBinding` | 落在某个 `Block`（命令块）里 | `parentBlock: Option[Block]` |

一个直观的读法：

- 字面量「谁都能用」→ `UnconstrainedBinding`，`location = None`。
- 端口/寄存器/线网「属于某个模块」→ `ConstrainedBinding`，带 `enclosure`。
- 运算结果、字面量「不能被赋值」→ `ReadOnlyBinding`。
- 寄存器/线网/运算结果「登记在命令块里」→ `BlockBinding`。

#### 4.2.2 核心流程

每个具体绑定 = 选用上面几个 trait 拼出来。例如：

```text
WireBinding  = ConstrainedBinding + BlockBinding        // 属于某模块、登记在 Block 里、可读写
RegBinding   = ConstrainedBinding + BlockBinding        // 同上
OpBinding    = ConstrainedBinding + ReadOnlyBinding + BlockBinding  // 运算结果，只读
PortBinding  = ConstrainedBinding                       // 模块端口，不挂在 Block 上
ElementLitBinding = UnconstrainedBinding + ReadOnlyBinding + (LitBinding)  // 字面量，谁都能读、只读
```

注意一个要点：`location` 字段在 `Binding` 根 trait 里是抽象的，由各分支给出具体语义——`UnconstrainedBinding` 恒为 `None`，`ConstrainedBinding` 恒为 `Some(enclosure)`。后续的可见性检查 `isVisibleFromModule`（见 [Data.scala:688-703](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L688-L703)）正是靠这个 `location` 来判断「当前模块能不能看到这个信号」。

#### 4.2.3 源码精读

根 trait 与两大顶层分支：

[Binding.scala:48-64](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L48-L64) —— `Binding`（根）、`TopBinding`、`UnconstrainedBinding`（`location = None`）、`ConstrainedBinding`（带 `enclosure`）。读注释：Constrained-ness 指的是「是否被模块边界约束」。

只读与块内两个正交维度：

[Binding.scala:67-72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L67-L72) —— `ReadOnlyBinding`（不能被重新赋值）与 `BlockBinding`（带 `parentBlock`）。这两者经常和 `ConstrainedBinding` 一起混入。

把维度翻译成人类可读字符串的辅助函数（调试时很有用）：

[Data.scala:502-517](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L502-L517) —— `_bindingToString` 把每种顶层绑定映射成一个简短词：`OpBinding→"OpResult"`、`PortBinding→"IO"`、`RegBinding→"Reg"`、`WireBinding→"Wire"` 等。下一节的实践会借助它来观察。

#### 4.2.4 代码实践

**实践目标**：在不运行的情况下，仅凭 trait 拼装预测「这个绑定能不能被赋值」「它有没有模块归属」。

**操作步骤**：

1. 打开 [Binding.scala:76-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L76-L95)，把 `PortBinding`、`OpBinding`、`RegBinding`、`WireBinding`、`MemoryPortBinding` 各自混入了哪些 trait 填进下表（示例代码：自行补全）：

   | 类 | Constrained? | ReadOnly? | Block? | 能否被 := 赋值 |
   |----|:---:|:---:|:---:|----|
   | `PortBinding` | ✓ | ✗ | ✗ | 输出端口可以、输入端口不行（由方向决定） |
   | `OpBinding` | … | … | … | … |
   | `RegBinding` | … | … | … | … |
   | `WireBinding` | … | … | … | … |

**需要观察的现象**：`OpBinding` 混入了 `ReadOnlyBinding`，所以 `(a + b)` 的结果不能再被 `:=` 赋值；`RegBinding`/`WireBinding` 没混入 `ReadOnlyBinding`，所以可以被赋值。

**预期结果**：你能仅凭 trait 组合判断「该绑定是否只读」，并对应到 [Data.scala:553-555](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L553-L555) 里 `connect` 方法对 `ReadOnlyBinding` 的拦截（`Cannot reassign to read-only`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么字面量要混入 `UnconstrainedBinding`，而端口要混入 `ConstrainedBinding`？

> **答案**：字面量（如 `5.U`）是一个全局常量，任何模块都允许直接引用它，没有「归属模块」的概念，故 `location = None`；端口则物理属于某个具体模块，跨模块引用要受可见性规则约束，故必须记录 `enclosure`。

**练习 2**：`OpBinding` 同时混入了 `ConstrainedBinding` 与 `ReadOnlyBinding`，这两者分别说明什么？

> **答案**：`ConstrainedBinding` 说明运算结果归属于产生它的那个模块（`enclosure: RawModule`），跨模块不可见；`ReadOnlyBinding` 说明运算结果只能出现在连线的右侧（源），不能作为左侧（汇）被赋值——因为它的值由运算本身决定，不允许再被驱动。

---

### 4.3 具体绑定：端口、节点、寄存器、线网

#### 4.3.1 概念说明

> ⚠️ **关于「NodeBinding」**：本讲的规格里提到一个最小模块叫 `NodeBinding`，但**在当前 HEAD（b2a0e03）的源码里并不存在名为 `NodeBinding` 的类**（你可以用 `grep -rn NodeBinding core/src` 自行验证，结果为空）。「node（节点）」在 FIRRTL 里泛指命名的中间信号，在 Chisel 的 Binding 体系里，这个角色被拆成了两个具体的绑定类：
> - **`OpBinding`**：原语运算（`+`、`&`、`===` …）的结果，即「运算产生的中间节点」。
> - **`WireBinding`**：`Wire(...)` 显式声明的命名线网，也是「节点」。
>
> 所以本节把 `PortBinding`、`OpBinding`/`WireBinding`（即规格里 `NodeBinding` 的实际落点）、`RegBinding` 一并讲清楚。

把日常用到的硬件构造和它们的绑定类对上号：

| 你写的代码 | 调用的工厂 | 最终的 binding |
|-----------|-----------|----------------|
| `IO(Input(UInt(8.W)))` | `IO` → `bindIoInPlace` | `PortBinding(enclosure)` |
| `Wire(UInt(8.W))` | `WireFactory.apply` | `WireBinding(enclosure, parentBlock)` |
| `Reg(UInt(8.W))` | `Reg` 工厂 | `RegBinding(enclosure, parentBlock)` |
| `a + b`（原语运算） | `pushOp` | `OpBinding(enclosure, parentBlock)` |
| `mem(addr)`（内存端口） | `MemBase.makePort` | `MemoryPortBinding(enclosure, parentBlock)` |

注意它们几乎都是 `ConstrainedBinding with BlockBinding`——「属于某模块 + 登记在某 Block」。差别只在「能不能被赋值」「是不是端口」。

#### 4.3.2 核心流程

绑定的写入统一走 `Data.bind(target, parentDirection)`，它是个抽象方法，由叶子 `Element` 与聚合 `Aggregate`（`Vec`/`Record`）各自实现：

```text
工厂方法（如 Wire.apply）
  │
  │  1. requireIsChiselType(t)            // 把关：必须是类型
  │  2. t.bind(XXXBinding(enclosure, block))   // 写入 binding（仅一次）
  │  3. pushCommand(DefWire/DefReg/...)    // 同时登记一条 IR 命令
  ▼
对叶子 Element：直接写 binding + 结算方向
对聚合 Vec/Record：给自己写顶层 binding，再把 ChildBinding(this) 分发给每个子元素
```

注意「**bind + pushCommand 成对**」这个模式（u4-l2 已建立）：写入 binding 是给信号定身份，push 一条 `DefWire`/`DefReg` 命令是让 IR 树记住它。两者形影不离。

#### 4.3.3 源码精读

**PortBinding / WireBinding / RegBinding / OpBinding 的定义**：

[Binding.scala:76-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L76-L95) —— 一组 `case class`，构造参数几乎都是 `enclosure: BaseModule/RawModule` 与 `parentBlock: Option[Block]`。其中 `OpBinding` 多混入 `ReadOnlyBinding`。

**叶子 `Element.bind`**：一次性写 binding + 结算方向。

[Element.scala:22-27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L22-L27) —— `binding = target` 触发 `binding_=`（含重绑守卫），再用 `SpecifiedDirection.fromParent` + `ActualDirection.fromSpecified` 算出最终方向。三行代码完成了「定型」。

**聚合 `Record.bind`（Bundle 的父类）**：递归分发 `ChildBinding`。

[Aggregate.scala:895-919](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L895-L919) —— 先给自己写顶层 binding，再 `for (...) child.bind(childBinding, resolvedDirection)` 把同一个 `ChildBinding(this)` 挂到每个字段上。这解释了 u2-l1 里「`topBindingOpt` 要穿过 `ChildBinding` 找到根」的设计。

**聚合 `Vec.bind`**：思路相同，额外给 `sample_element` 挂一个 `SampleElementBinding`。

[Aggregate.scala:245-260](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L245-L260) —— `sample_element.bind(SampleElementBinding(this), ...)`，其余元素统一拿 `ChildBinding(this)`。

**`Wire` 工厂如何落地 `WireBinding`**：

[Data.scala:1089-1102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L1089-L1102) —— 第 1097 行 `x.bind(WireBinding(Builder.forcedUserModule, Builder.currentBlock))`，紧接着第 1099 行 `pushCommand(DefWire(sourceInfo, x))`。这就是「bind + pushCommand 成对」的典型现场。

**运算结果如何落地 `OpBinding`**：

[Builder.scala:899-903](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L899-L903) —— `pushOp` 里第 901 行 `cmd.id.bind(OpBinding(forcedUserModule, currentBlock))`，第 902 行 `pushCommand(cmd)`。所以每写一次 `a + b`，结果对象就被绑成 `OpBinding` 并登记为一条 `DefPrim` 命令。

#### 4.3.4 代码实践

**实践目标**：跟踪 `val s = a + b` 这一行的绑定全过程。

**操作步骤**：

1. 在 [Builder.scala:899-903](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L899-L903) 找到 `pushOp`。
2. 回溯：`a + b` 经 u2-l2 讲过的 `binop → pushOp(DefPrim)` 链路进入这里。
3. 记下 `s` 的 binding 类型与它被登记成的命令类型。

**需要观察的现象**：`s` 的 `_binding` 是 `Some(OpBinding(...))`，同时模块命令队列里多了一条 `DefPrim`（具体加法运算）命令。

**预期结果**：你能解释「为什么 `s` 不能再被 `s := x` 赋值」——因为 `OpBinding` 混入了 `ReadOnlyBinding`，[Data.scala:553-555](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L553-L555) 的 `connect` 会拦下并报 `Cannot reassign to read-only`。若要把运算结果存下来再改，需要先 `val s = Wire(...); s := a + b`。

#### 4.3.5 小练习与答案

**练习 1**：`PortBinding` 为什么没有 `parentBlock`（不混入 `BlockBinding`），而 `WireBinding` 有？

> **答案**：端口是模块对外的接口，不属于模块内部任何一个命令块（`when` 块等）；而 `Wire`/`Reg`/运算结果都登记在模块体内某个具体的 `Block` 中（可能嵌在 `when` 里），所以需要 `parentBlock` 来记录「声明在哪个作用域」，这对后续可见性检查（`visibleFromBlock`，见 [Data.scala:704](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L704)）至关重要。

**练习 2**：`Record.bind` 为什么要把同一个 `ChildBinding(this)` 分发给所有子元素，而不是给每个子元素一个独立的 `PortBinding`/`WireBinding`？

> **答案**：因为绑定的是「整个 Bundle」这个聚合对象，子字段只是它的组成部分。子元素通过 `ChildBinding(parent)` 指回父节点，再由 `topBindingOpt`（[Data.scala:445-450](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L445-L450)）逐级上溯找到根绑定（如 `PortBinding`）。这样「整个 IO 是端口」这一事实只需存一次，子字段共享之。

---

### 4.4 字面量绑定 LitBinding

#### 4.4.1 概念说明

字面量是 Chisel 里最特殊的一类硬件值：它**既是硬件（能参与运算、能被综合），又不受模块边界约束（任何模块都能引用同一个 `5.U`）**。于是它有专属的绑定家族 `LitBinding`，统一混入 `UnconstrainedBinding with ReadOnlyBinding`——「谁都能读、且只读」。

`LitBinding` 自己是个 `sealed trait`，下面三个具体子类对应三种字面量形态：

| 子类 | 装的是 | 典型来源 |
|------|--------|----------|
| `ElementLitBinding(litArg: LitArg)` | 单个叶子的字面值 | `5.U`、`true.B` |
| `BundleLitBinding(litMap: Map[Data, LitArg])` | 一个 Bundle 各字段的字面值 | `MyBundle.Lit(_.a -> 1.U, _.b -> 2.U)` |
| `VecLitBinding(litMap: VectorMap[Data, LitArg])` | 一个 Vec 各元素的字面值 | `Vec.Lit(1.U, 2.U)` |

它们都把字面值装在一个 `LitArg`（内部 IR 里的字面量节点）里。注意 `LitArg` 同时是 FIRRTL Arg 的一种，所以字面量在发射时几乎可以「原样」吐进 FIRRTL 文本。

#### 4.4.2 核心流程

以 `5.U` 为例，字面量的绑定链路非常短：

```text
5.U
 └─ (隐式转换) 调用 UInt 字面量工厂 UIntFactory.Lit(5, Width())
      └─ 构造 ULit(5, Width())            // 一个 LitArg
           └─ lit.bindLitArg(result)      // 见 IR.scala:125
                ├─ result.bind(ElementLitBinding(lit))   // 写入 binding（只读 + 不受约束）
                └─ result.setRef(this)                   // 顺手把 ref 也指给这个字面量
```

注意字面量**只 bind、不 pushCommand**——它不需要登记成 `DefWire`/`DefReg` 那样的命令，因为它本身就是一个常量 Arg，会直接内联到引用它的命令里。这与 4.3 节的 `Wire`/`Op` 形成对照。

#### 4.4.3 源码精读

`LitBinding` 家族定义：

[Binding.scala:244-252](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L244-L252) —— `sealed trait LitBinding extends UnconstrainedBinding with ReadOnlyBinding`，下面三个 `case class`。注意它们都带「不受约束 + 只读」的双重身份。

字面量工厂（以 UInt 为例）：

[UIntFactory.scala:18-23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/UIntFactory.scala#L18-L23) —— 构造 `ULit(value, width)`，再 `lit.bindLitArg(result)`。

真正写入 `ElementLitBinding` 的那一行：

[IR.scala:120-129](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L120-L129) —— `bindLitArg` 里第 126 行 `elem.bind(ElementLitBinding(this))`，第 127 行 `elem.setRef(this)`。这是「字面量绑定」最确切的落点。

`Element` 如何把聚合字面量「翻译」回叶子字面量：

[Element.scala:29-52](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L29-L52) —— 当一个叶子是 `BundleLitBinding`/`VecLitBinding` 的子字段时，`topBindingOpt` 会在 [Element.scala:31-40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L31-L40) 把它「翻译」成该字段对应的 `ElementLitBinding`（或 `DontCareBinding`）。所以从叶子视角看，永远只会碰到 `ElementLitBinding`。

#### 4.4.4 代码实践

**实践目标**：验证 `5.U` 与 `Wire(UInt(8.W))` 走的是两条完全不同的绑定路径。

**操作步骤**：

1. 在 [UIntFactory.scala:18-23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/UIntFactory.scala#L18-L23) 与 [IR.scala:120-129](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L120-L129) 之间画一条调用链：`.U → Lit → ULit → bindLitArg → bind(ElementLitBinding)`。
2. 对比 [Data.scala:1089-1102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L1089-L1102) 里 `Wire` 的路径：`bind(WireBinding) + pushCommand(DefWire)`。

**需要观察的现象**：字面量路径里**没有** `pushCommand`，而 `Wire` 路径里 `bind` 与 `pushCommand` 成对出现。

**预期结果**：你能解释「为什么 `5.U` 不会在生成的 Verilog 里单独占一行 wire 声明，而是被内联到使用处」——因为它只是个 `ElementLitBinding`（一个 Arg），不是一条 `DefWire` 命令。

#### 4.4.5 小练习与答案

**练习 1**：`ElementLitBinding`、`BundleLitBinding`、`VecLitBinding` 三者都继承 `LitBinding`，为什么还需要区分？

> **答案**：因为字面量可能挂在不同层级。`5.U` 这种叶子字面量直接用 `ElementLitBinding(litArg)`；而 `Vec.Lit(1.U, 2.U)` 或 `Bundle.Lit(...)` 是把多个字面值打包挂到聚合的**根**上（用 `litMap` 记录每个子字段→字面值），方便整体传递与拷贝；当访问到其中某个叶子时，`Element.topBindingOpt`（[Element.scala:31-40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L31-L40)）会按需翻译回 `ElementLitBinding`。

**练习 2**：为什么字面量绑定混入了 `UnconstrainedBinding`（`location = None`），而不像端口那样带 `enclosure`？

> **答案**：字面量是全局常量，不属于任何模块，任何模块都合法地能引用 `0.U`、`5.U`。如果给它一个 `enclosure`，反而会无端限制它的可见性。这也使得 [Data.scala:700](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L700) 的可见性检查里 `case Some(_: UnconstrainedBinding) => true` 直接放行。

---

## 5. 综合实践

把本讲四节串起来，做一个「binding 体检」小任务。

**任务**：写一个最小模块，在里面分别制造四类硬件值——**字面量、端口、Wire、运算结果**，然后借助 `.toString` 把每类信号的 binding 类别「显形」，最后把它们逐一对应到 `Binding.scala` 里的具体类与源码行号。

**示例代码**（这是为本实践编写的示例，非项目原有代码）：

```scala
// 依赖 chisel3 与一个可执行的 ChiselStage 环境
import chisel3._

class BindingProbe extends Module {
  val io = IO(new Bundle { val in = Input(UInt(8.W)); val out = Output(UInt(8.W)) })

  val literal = 5.U                       // 期望 binding: ElementLitBinding
  val w      = Wire(UInt(8.W))            // 期望 binding: WireBinding
  val sum    = io.in + w                  // 期望 binding: OpBinding
  // io.in / io.out                       // 期望 binding: PortBinding（根上）

  w := literal
  io.out := sum

  // 把每类信号的 toString 打到控制台（toString 会经 stringAccessor 渲染 binding 类别）
  println(s"literal = $literal")
  println(s"wire    = $w")
  println(s"sum     = $sum")
  println(s"io.in   = ${io.in}")
}
```

**操作步骤**：

1. 用 `ChiselStage.emitSystemVerilog(new BindingProbe)` 触发 elaboration（参考 u1-l4）。在 elaboration 期间，上面的 `println` 会执行。
2. 阅读渲染逻辑 [Data.scala:473-497](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L473-L497)（`stringAccessor`）与 [Data.scala:502-517](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L502-L517)（`_bindingToString`），理解输出里的 `Wire[...]`、`IO[...]`、`OpResult[...]` 是怎么来的。
3. 把观察到的字符串与 binding 类一一对应：

   | 观察到的 toString 片段 | 对应的 Binding 子类 | 定义位置 |
   |----|----|----|
   | `UInt<3>(5)`（字面量，经 litOption 分支渲染） | `ElementLitBinding` | [Binding.scala:246](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L246) |
   | 含 `Wire[UInt<8>]` | `WireBinding` | [Binding.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L92) |
   | 含 `IO[UInt<8>]` | `PortBinding` | [Binding.scala:76](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L76) |
   | 含 `OpResult[UInt<...]` | `OpBinding` | [Binding.scala:81-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L81-L84) |

**需要观察的现象**：四类信号 `toString` 的渲染各不相同，且差异正来自它们身上挂着的不同 `Binding`。

**预期结果**（精确的字段名/位宽以本地为准，标注「待本地验证」）：`literal` 打印形如 `UInt<3>(5)`；`w` 打印形如 `BindingProbe.w: Wire[UInt<8>]`；`io.in` 打印形如 `BindingProbe.io.in: IO[UInt<8>]`；`sum` 打印形如 `... OpResult[UInt<8>]`。

**进阶**（可选）：把 `io.out := sum` 改成 `sum := io.in`（对运算结果赋值），重新 elaboration，预期会被 [Data.scala:553-555](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L553-L555) 拦下，报 `Cannot reassign to read-only`——这是 `OpBinding` 混入 `ReadOnlyBinding` 的直接体现。

## 6. 本讲小结

- 一个 `Data` 是「类型」还是「硬件值」，由它身上的 **`_binding` 字段**决定：`None` 是纯类型、`Some` 是硬件值。写入入口 `binding_=`（[Data.scala:428-433](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L428-L433)）**只能写一次**，重绑抛 `RebindingException`。
- `requireIsChiselType` / `requireIsHardware`（[experimental/package.scala:54-92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/package.scala#L54-L92)）都归结到 `isSynthesizable`（[Data.scala:439-443](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L439-L443)），即「binding 是否为 `Some`」。
- `Binding` 是一套 **trait 拼装**：`ConstrainedBinding`（带 `enclosure`，受模块约束）/ `UnconstrainedBinding`（`location=None`，谁都能读）、`ReadOnlyBinding`（只读）、`BlockBinding`（带 `parentBlock`）。具体子类是这些 trait 的组合。
- 端口=`PortBinding`、线网=`WireBinding`、寄存器=`RegBinding`、运算结果=`OpBinding`、内存端口=`MemoryPortBinding`。**规格里的 `NodeBinding` 在当前源码中不存在**，「节点」角色实际由 `OpBinding` 与 `WireBinding` 承担。
- 字面量有专属家族 `LitBinding`（`ElementLitBinding`/`BundleLitBinding`/`VecLitBinding`），混入「不受约束 + 只读」，其确切绑定落点在 [IR.scala:126](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L126) 的 `bindLitArg`，且**只 bind、不 pushCommand**。
- 聚合类型（`Vec`/`Record`）的 `bind` 会把同一个 `ChildBinding(this)` 递归分发给所有子元素，子元素经 `topBindingOpt` 上溯到根绑定。

## 7. 下一步学习建议

- **走向连线算法**：binding 决定身份后，下一步就是「谁能连谁」。建议阅读 **u3-l4（MonoConnect 与 BiConnect）**，看 `MonoConnect`/`BiConnect` 如何读取 `BindingDirection`（[Binding.scala:15-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L15-L45)）与各 `Binding` 子类来判定方向合法性。
- **走向可见性与命名**：`location`/`enclosure` 是 u7-l3（Namer 与 Identifier）和可见性检查的基础，可结合 [Data.scala:688-715](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L688-L715) 一起读。
- **走向 DataView（u8-l2）**：`ViewBinding`/`AggregateViewBinding` 是 Binding 体系的现代扩展，理解了本讲的 trait 拼装后再读 `dataview` 会很自然。
- **想动手验证**：把综合实践里的 `BindingProbe` 真正跑起来（参考 u1-l2 的 `./mill` 用法），对照 `println` 输出与本讲给出的源码行号，把「类型→硬件值」的转身看个真切。
