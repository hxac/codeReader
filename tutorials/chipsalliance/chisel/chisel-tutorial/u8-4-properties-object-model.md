# Properties：参数化对象模型（OMR）

## 1. 本讲目标

本讲讲解 Chisel 的 `chisel3.properties` 包——一套用于在 RTL 中携带**非硬件数据**的机制。读完本讲你应当能够：

- 说清 `Property[T]` 与普通 `Data`（如 `UInt`）的本质区别：它不综合成线或门，只承载可序列化的元数据。
- 理解 `PropertyType` 这个**类型类（typeclass）**如何把 Scala 的 `Int/String/Boolean/...` 映射成 FIRRTL 的 `Integer/String/Bool/...` 属性类型。
- 会用 `Class` 定义一个「只能装 Property 端口」的类模块，用 `Object`（`DynamicObject`/`StaticObject`）实例化它，理解二者与硬件 `Module` 的平行关系。
- 知道 `Path` 如何在 `Property[Path]` 里**指向真实硬件**（某模块实例、某根线、某块内存），从而让元数据能「引用」设计。
- 建立 **OMR（Object Model Refinement，对象模型精炼）** 的心智模型：为什么要把对象模型做成 FIRRTL IR 的一等公民。

## 2. 前置知识

本讲是「专家层」内容，假设你已掌握（对应前置讲义）：

- **Data 与 binding 系统**（u2-l1、u4-l3）：知道一个 `Data` 既可以是「类型」也可以是「硬件值」，由 `binding` 字段区分；`Element` 是叶子类型分支的根。
- **Module 生命周期**（u3-l1）：知道 `Module(...)` 经 `evaluate → generateComponent → initializeInParent` 三步收口，构造体「只登记不施工」。
- **Definition / Instance**（u8-l1）：知道 `Definition(new Mod)` 造蓝图、`Instance(defn)` 克隆端口，以及 `@instantiable` / `@public` 宏如何把内部字段暴露成可跨层访问的 `_lookup`。
- **内部 FIRRTL IR 三层树**（u4-l2）：`Circuit ⊃ Component ⊃ Command`，命令经 `pushCommand` 落入模块的 `_body: Block`。

**两个本讲要用到的新术语：**

- **类型类（typeclass）**：Scala 里用 `implicit` 参数解析的一套「能力证书」。`PropertyType[T]` 就是「`T` 能不能当属性类型」的证书，编译期自动查找，查不到就编译报错。
- **非硬件数据（non-hardware data）**：参与 elaboration、能进 IR、能被序列化，但**绝不**变成 Verilog 里的信号。它穿越整条编译链，最终由下游 CIRCT/firtool 抽取成 JSON 等元数据。

## 3. 本讲源码地图

本讲涉及的源码集中在 `core/src/main/scala/chisel3/properties/`，并关联到内部 IR 与连线实现：

| 文件 | 作用 |
| --- | --- |
| [package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/package.scala) | 包对象，仅一段「non-hardware data」的实验性声明。 |
| [Property.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala) | `Property[T]` 类型、`PropertyType` 类型类及其所有内建实例、属性运算（`+`/`===`/`&&` 等）。本讲最重的文件。 |
| [Class.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala) | `Class`（属性容器类模块）、`ClassType`、`Definition`/`Instance` 上的扩展方法。 |
| [Object.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala) | `DynamicObject`（按字符串名取字段，不安全）与 `StaticObject`（配合 `Definition`/`Instance`，类型安全）。 |
| [Path.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala) | `Path`：把一个硬件模块/信号/内存包装成可放进 `Property[Path]` 的引用。 |
| 内部 IR：[internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | `PropertyLit`/`PropExpr`/`PropAssign`/`PropertyAssert`/`DefObject`/`DefClass` 等 IR 节点。 |
| 绑定：[internal/Binding.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala) | `ClassBinding`/`ObjectFieldBinding`/`PropertyValueBinding`。 |
| 连线：[internal/MonoConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala) | `propConnect`：属性专用的连线算法。 |
| 测试：[PropertySpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala)、[ClassSpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala)、[ObjectSpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/ObjectSpec.scala) | 以断言形式记录了属性在 CHIRRTL 里的真实文本表现，是本讲实践的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 Property：非硬件数据载体与 PropertyType 类型类

#### 4.1.1 概念说明

先建立一个反直觉的结论：**`Property[T]` 长得像 `UInt`，但它不是硬件。**

回顾 u2-l1，所有硬件信号都继承自 `Data`。`Property[T]` 也继承自 `Element`（叶子分支），所以它**能**像 `UInt` 一样出现在端口里、用 `:=` 连接。但它的目的是承载**非硬件**信息——整数、字符串、布尔、甚至「指向某个硬件的路径」。这些信息：

- **没有位宽**（`width` 恒为 `UnknownWidth`），不能 `.asUInt`，不能被 `UInt` 驱动；
- **不综合成线/门**，最终不出现在 Verilog 里，而是被下游 firtool 抽取成**对象模型 JSON（OMIR）**；
- 只能在 elaboration 期确定（常量或对其它属性的运算）。

这就是 **OMR（Object Model Refinement）** 的核心动机：在生成器里，你常常想随设计吐出一份机器可读的元数据（寄存器映射、地址表、文档、配置参数）。传统做法是在 Scala 侧另建数据结构 + 注解，容易和真实设计脱节。Properties 把元数据做成 FIRRTL IR 的一等公民——它和设计同源、能引用真实硬件（靠 `Path`）、随编译链流动、最终落成 JSON。

**`PropertyType` 类型类**回答「哪些 Scala 类型能当属性」：

| Scala 类型 `T` | FIRRTL 属性类型 | 字面量写法 |
| --- | --- | --- |
| `Int` / `Long` / `BigInt` | `Integer` | `Property(123)` |
| `Double` | `Double` | `Property(123.456)` |
| `String` | `String` | `Property("fubar")` |
| `Boolean` | `Bool` | `Property(false)` |
| `Path` | `Path` | `Property(Path(data))` |
| `ClassType`（某 `Class` 的类型） | `Class` / `AnyRef` | 仅作类型，无字面量 |

类型不匹配时，编译器在**编译期**就报错（找不到隐式 `PropertyType[T]`），而不是等到 elaboration。

#### 4.1.2 核心流程

`Property[T]` 的「值」有两种来源，对应两个 `apply`：

1. **类型（type）**：`Property[Int]()` 造一个未赋值的属性类型，常用于声明 `IO(Input(Property[Int]()))`。
2. **字面量（literal）**：`Property(123)` 造一个带值的属性常量。

字面量的值不是 `LitArg`（那是给 `UInt` 用的整数位串），而是一个专门的 `PropertyLit` IR 节点，因为属性字面量可能是字符串、布尔等非整数。整个登记过程仍遵循 u4-l2 的「bind + pushCommand 成对」模式，但属性字面量只 `bind`（绑成 `PropertyValueBinding`）、由 `PropAssign` 单独驱动，不走普通 `Connect`。

属性之间的连线（`:=`）也不走 `MonoConnect` 的普通 `Connect`，而是走一个**专用的** `propConnect`，它发出的是 `PropAssign` 命令。属性上的运算（`+`、`===`、`&&`）则生成 `PropExpr`（一棵运算树），同样由 `PropAssign` 落地。

用一个伪流程概括「声明一个 `Property[String]` 输出并赋字面量」：

```
Property("cfg")                                  // 用户写法
  → PropertyType 隐式解析: stringPropertyTypeInstance   // 编译期，映射 String→fir.StringPropertyType
  → Property.apply(lit)                           // 运行期
  → ir.PropertyLit(tpe, "cfg")                    // 造字面量 Arg
  → literal.bindLitArg(result)                    // result.bind(PropertyValueBinding) + setRef
emitCHIRRTL 看到 PropAssign → 打印 propassign propOut, String("cfg")
```

#### 4.1.3 源码精读

`Property` 本身是一个 `sealed trait`，继承 `Element`（因而间接是 `Data`），但用一系列 override 把所有「硬件化」的能力禁用掉：

[Property.scala:215](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L215) —— `sealed trait Property[T] extends Element`，注意它的类注释（[Property.scala:208-214](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L208-L214)）明确指出：properties 没有 width、不能放进聚合 `Data`、不能与 `Data` 相连。

禁用硬件能力的几处关键 override：

- [Property.scala:235-238](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L235-L238)：`_asUIntImpl` 直接 `Builder.error(...does not support .asUInt)` 并返回 `0.U`。
- [Property.scala:239-241](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L239-L241)：`_fromUInt` 抛异常，禁止被 `UInt` 驱动。
- [Property.scala:265](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L265)：`def width: Width = UnknownWidth`，恒为未知位宽。
- [Property.scala:243-249](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L243-L249)：`firrtlConnect` 重写——当右侧也是 `Property` 时调用 `MonoConnect.propConnect`，否则报错。这正是「属性连线走专用通道」的入口。

`PropertyType` 类型类与内建实例集中在一个伴生 `object` 里：

- [Property.scala:25-46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L25-L46)：`sealed trait PropertyType[T]`，声明两个关联类型 `type Type`（属性 IR 里的类型）与 `type Underlying`（内部表示），以及 `getPropertyType()`、`convert`、`convertUnderlying` 三个方法。`@implicitNotFound`（[Property.scala:24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L24)）给出编译期友好报错。
- [Property.scala:62-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L62-L68)：`SimplePropertyType[T]`，让 `Type = Underlying = T` 的简单情形省去样板。
- [Property.scala:121-153](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L121-L153)：`intPropertyTypeInstance`、`stringPropertyTypeInstance`、`boolPropertyTypeInstance` 等，逐一把 Scala 类型映射到 `fir.IntegerPropertyType`/`fir.StringPropertyType`/`fir.BooleanPropertyType`。

`Property.apply` 的两个重载：

- [Property.scala:745-747](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L745-L747)：`def apply[T]()(implicit tpe: PropertyType[T])` 造「类型」。
- [Property.scala:751-755](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L751-L755)：`def apply[T](lit: T)` 造「字面量」——构造 `ir.PropertyLit`，再 `literal.bindLitArg(result)`。

属性字面量的 IR 节点与绑定：

- [IR.scala:191-204](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L191-L204)：`case class PropertyLit`，注释明确「不是 `LitArgs`，因为属性字面量不全是整数」；`bindLitArg` 做 `bind(PropertyValueBinding)` + `setRef`。
- [Binding.scala:252](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L252)：`case object PropertyValueBinding extends UnconstrainedBinding with ReadOnlyBinding`——属性字面量「不受模块约束 + 只读」，这与 u4-l3 讲的硬件 `LitBinding` 一脉相承但更受限。

属性连线的专用通道：

- [MonoConnect.scala:433-457](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L433-L457)：`propConnect` 先 `reify` 掉可能的 DataView（u8-l2），再做方向 `checkConnect`，最后向当前 `RawModule` 或 `Class` 压入 `PropAssign(...)`（**不是** `Connect`）。

测试里的断言是「值→文本」的最佳凭证：

- [PropertySpec.scala:38-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L38-L45)：`propOut := Property(123)` 产出的 CHIRRTL 必须包含 `propassign propOut, Integer(123)`。
- [PropertySpec.scala:106-113](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L106-L113)：字符串字面量产出 `propassign propOut, String("fubar")`。

#### 4.1.4 代码实践

**目标**：亲手验证「属性类型声明」「属性字面量」「属性连线」在 CHIRRTL 里的真实文本，并确认它们不出现在 Verilog 里。

**操作步骤**（这是源码阅读 + 运行型实践，依据见上述测试）：

1. 在一个能运行 Chisel 的工程里（见 u1-l2 的 `publishLocal`，或直接在仓库里用 `./mill`），写一个最小脚本：

   ```scala
   // 示例代码：可放进 src/test/scala 或一个 REPL
   import chisel3._
   import chisel3.properties.Property
   import circt.stage.ChiselStage

   class PropDemo extends RawModule {
     val in  = IO(Input(Property[String]()))   // 属性类型端口
     val out = IO(Output(Property[String]()))
     out := in                                 // 属性连线
     val lit = IO(Output(Property[Int]()))
     lit := Property(42)                       // 属性字面量
   }

   println(ChiselStage.emitCHIRRTL(new PropDemo))
   println(ChiselStage.emitSystemVerilog(new PropDemo))
   ```

2. 先看 `emitCHIRRTL` 的输出，定位三行：`input in : String`、`propassign out, in`、`propassign lit, Integer(42)`。
3. 再看 `emitSystemVerilog` 的输出，搜索 `in`、`out`、`lit`。

**需要观察的现象**：
- CHIRRTL 里出现 `propassign`（而非 `connect`）和 `String`/`Integer` 属性类型。
- **Verilog 里完全没有 `in/out/lit` 这些端口**——它们是非硬件数据，综合时被丢弃。

**预期结果**：CHIRRTL 含 `propassign`，Verilog 不含这些属性端口。如果 firtool 报「unused」之类，那是正常的。若你想确认 Verilog 为空端口，可与一个等价的 `UInt` 端口模块对比。

> 若环境无法运行，参见 [PropertySpec.scala:30-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L30-L45) 的断言作为「待本地验证」的预期。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Property[MyThing]()`（`MyThing` 是任意自定义类）会编译失败？提示：看 `Property.apply` 的隐式参数。

> **答案**：`apply[T]()(implicit tpe: PropertyType[T])` 需要一个 `PropertyType[MyThing]` 的隐式实例。`object PropertyType` 只为 `Int/Long/BigInt/Double/String/Boolean/Path/ClassType` 等预定义了实例，自定义类没有，故 `@implicitNotFound` 触发编译报错。这正是 [PropertySpec.scala:16-21](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L16-L21) 验证的行为。

**练习 2**：`Property(5).asUInt` 会发生什么？

> **答案**：返回 `0.U` 并经 `Builder.error` 记录一条错误，因为 [Property.scala:235-238](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L235-L238) 的 `_asUIntImpl` 显式拒绝。

---

### 4.2 Class：只装 Property 的类模块

#### 4.2.1 概念说明

`Class` 是「属性世界的 `Module`」。它和硬件 `Module` 一样：有名字、有端口、能在体内实例化别的 `Class`、能被 `Definition`/`Instance` 包裹（u8-l1）。但有一条铁律——**`Class` 的端口只能是 `Property` 类型，体内不能造任何硬件**（不能 `Wire(UInt())`、不能 `Reg`、不能 `Mem`）。

为什么要有 `Class`？因为 OMR 要表达的是「对象图」：一个对象有若干属性字段，对象之间可以互相引用。`Class` 就是这个对象图的「类定义」，`Object`（下一节）是它的「实例」。整张对象图随设计进入 FIRRTL IR，下游 firtool 把它抽成 JSON。

`ClassType` 是「某个 `Class` 的类型标签」，可以放进 `Property[ClassType]`——于是属性不仅能装标量，还能**装「指向某个对象实例的引用」**，从而表达对象间关系。

#### 4.2.2 核心流程

`Class` 继承 `BaseModule`（与 `RawModule` 同级，**不**带 clock/reset），其生命周期仍是 u3-l1 的三段式，但 `generateComponent` 被重写以处理属性专属的收尾：

```
Class(new MyClass)
  → BaseModule 构造：currentModule := this，开 _body: Block
  → 构造体：IO(Property[...]) 登记端口；out := in 走 propConnect 压 PropAssign；
            Class.unsafeGetDynamicObject / Instance 在体内造 Object（压 DefObject）
  → generateComponent（Class 自己的实现）：
      1. evaluateAtModuleBodyEnd / _closed = true
      2. namePorts()
      3. 遍历 getIds：给 DynamicObject/StaticObject/Data 各自命名、设 ref
      4. 构造 Port IR
      5. _component = Some(DefClass(this, name, ports, _body))
  → initializeInParent 返回空（Class 不在父模块接线）
```

关键点：`Class` 收口产出的 IR 节点是 `DefClass`（不是 `DefModule`）；体内只允许三类命令——`PropAssign`（属性赋值）、`PropertyAssert`（属性断言）、`DefObject`（实例化对象）。其它命令（普通 `Connect`、`DefReg` 等）在 `addCommand` 层面就被拒绝，因为 `Class` 只暴露了这三个 `addCommand` 重载。

#### 4.2.3 源码精读

`Class` 的定义与端口约束：

- [Class.scala:35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L35)：`class Class extends BaseModule`。类注释（[Class.scala:28-34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L28-L34)）强调「classes cannot construct hardware, only graphs of non-hardware Property information」。
- 「端口必须是 Property」的检查不在 `Class.scala` 本身，而在 `IO` 登记阶段；测试 [ClassSpec.scala:62-74](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L62-L74) 验证了非 Property 端口会抛 `Class ports must be Property type, but found Bool.`。

`Class` 重写的 `generateComponent`：

- [Class.scala:36-89](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L36-L89)：注意 [Class.scala:47-74](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L47-L74) 对 `_ids` 的分类处理——`DynamicObject` 强制命名并把其 `Property[ClassType]` 的 ref 指向自身（[Class.scala:50-57](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L50-L57)），`StaticObject` 把 ref 指向其底层 `ModuleClone`（[Class.scala:58-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L58-L63)）；最终 [Class.scala:85](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L85) 产出 `DefClass(this, name, ports, _body)`。

只允许三类命令的 `addCommand`：

- [Class.scala:97-109](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L97-L109)：`addCommand(c: PropAssign)`、`addCommand(c: PropertyAssert)`、`addCommand(c: DefObject)` 三个重载。注释点明「Most commands are unsupported in Class」。

`ClassType` 与类型标签：

- [Class.scala:124-149](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L124-L149)：`case class ClassType(name: String)`，内含一个 `sealed trait Type`（[Class.scala:136](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L136)），仅作类型标签、无成员，用于 `Property[cls.Type]()`。这把「类名」编码成可在编译期区分的类型，避免不同 `Class` 的引用被混用。

`Definition[Class]` 上的安全扩展方法：

- [Class.scala:216-254](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L216-L254)：`ClassDefinitionOps` 提供 `getPropertyType`（[Class.scala:224-235](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L224-L235)）与 `getClassType`，推荐用它们替代不安全的 `unsafeGetReferenceType`/`unsafeGetClassTypeByName`。

`DefClass` IR 节点与序列化：

- [IR.scala:658](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L658)：`case class DefClass(id: Class, name: String, ports: Seq[Port], block: Block) extends Component`——与 `DefModule` 平级的一种顶层 `Component`。
- [Serializer.scala:686-695](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L686-L695)：`DefClass` 序列化为 `class <name> :`，端口与命令逐行打印——这就是 `emitCHIRRTL` 看到的 `class Foo :`。

测试佐证：

- [ClassSpec.scala:47-60](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L47-L60)：带 `Property[Int]` 端口的 `Class` 序列化为 `input in : Integer` / `output out : Integer`。
- [ClassSpec.scala:76-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L76-L84)：`out := in` 在体内产出 `propassign out, in`。

#### 4.2.4 代码实践

**目标**：定义一个带 `Property[String]` 参数的 `Class`，观察它在 CHIRRTL 里以 `class` 形式独立存在，且端口类型为 `String`。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import chisel3.properties.{Class, Property}
import chisel3.experimental.hierarchy.Definition
import circt.stage.ChiselStage

class DocBundle extends Class {
  override def desiredName = "DocInfo"
  val name = IO(Input(Property[String]()))
  val size = IO(Input(Property[Int]()))
  val json = IO(Output(Property[String]()))
  json := name            // 仅示意属性赋值（实际可做字符串拼接运算）
}

println(ChiselStage.emitCHIRRTL(new RawModule {
  Definition(new DocBundle)   // 用 Definition 触发 Class 的 elaboration
}))
```

**需要观察的现象**：CHIRRTL 顶部出现 `class DocInfo :`，其下有 `input name : String`、`input size : Integer`、`output json : String`、`propassign json, name`。

**预期结果**：与 [ClassSpec.scala:47-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L47-L84) 的断言一致。注意 `Class` 必须被 `Definition`/`Instance` 或放进某个 `RawModule` 体内才会被 elaboration 收集——单独 `new DocBundle` 不会触发。

> 若把 `Input(Property[String]())` 改成 `Input(Bool())`，应得到与 [ClassSpec.scala:62-74](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L62-L74) 一致的「Class ports must be Property type」错误。

#### 4.2.5 小练习与答案

**练习 1**：`Class` 和 `RawModule` 都继承 `BaseModule`，它们最大的区别是什么？

> **答案**：`Class` 的端口只能是 `Property` 类型、体内不能造硬件、收口产出 `DefClass`（序列化为 `class`）；`RawModule` 是普通硬件模块、无 clock/reset、端口是任意 `Data`、收口产出 `DefModule`（序列化为 `module`）。二者分别服务「对象图」与「硬件图」两个平行的世界。

**练习 2**：为什么 `Class` 只暴露 `addCommand(PropAssign/PropertyAssert/DefObject)` 三个重载？

> **答案**：因为对象图里只有「属性赋值、属性断言、实例化对象」这三种有意义的操作；任何硬件命令（`Connect`/`DefReg`/`DefWire`...）在属性世界都无意义，故在 `addCommand` 层直接拒收，参见 [Class.scala:93-119](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L93-L119)。

---

### 4.3 Object：Class 的实例（DynamicObject 与 StaticObject）

#### 4.3.1 概念说明

`Object` 是「`Class` 的实例」，类比于 `Module` 的实例化。一个 `Object` 持有对某个 `Class` 的引用，可以在端口里以 `Property[ClassType]` 的形式被传递，从而让对象图中的节点互相指向。

Chisel 提供两条创建路径，**安全性**是它们的核心区别：

- **`DynamicObject`**：通过字符串类名构造，`getField[T]("name")` 按字符串取字段，**不检查字段是否存在、类型/方向是否正确**。灵活但危险，由 `Class.unsafeGetDynamicObject` 或 `DynamicObject(new Class{...})` 创建。
- **`StaticObject`**：配合 u8-l1 的 `Definition`/`Instance` API 与 `@instantiable`/`@public` 宏工作，字段访问走类型安全的 `_lookup`，是推荐做法。

二者最终都落到同一个 IR 节点 `DefObject`（序列化为 `object <name> of <ClassName>`），区别只在「编译期/构造期是否做了类型检查」。

#### 4.3.2 核心流程

以 `DynamicObject` 为例，创建流程：

```
DynamicObject(new TestClass {...})
  → DynamicObject._applyImpl:
      1. Module.evaluate(bc)            // 先把 Class 细化一次
      2. Class.unsafeGetDynamicObject(cls.name):
           - new DynamicObject(ClassType(name))
           - 在当前 RawModule/Class 上压 DefObject(name)
           - 把该 Object 的 Property[ClassType] ref 绑成 OpBinding/ClassBinding
      3. obj.setSourceClass(cls)        // 记住源 Class，供收口时回填 ref
  → 收口时（Class.generateComponent）给 Object 命名、把 ClassType ref 指向 Object
```

字段访问（`getField`）：构造一个新的 `Property[T]`，把它的 ref 设成 `Node(this)` + 字段名（即 `obj.field`），并绑成 `ObjectFieldBinding`——这种绑定表示「我是某个对象实例的字段」，受该对象所属模块约束。

`StaticObject` 流程类似，但它不持有 `Class` 实例，而是持有 `Instance[Class]` 背后的 `ModuleClone`（u8-l1），收口时把 ref 指向 `ModuleClone` 的 ref。

#### 4.3.3 源码精读

`DynamicObject` 类：

- [Object.scala:33-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L33-L78)：`class DynamicObject private[chisel3] (val className: ClassType)`，注意构造器是 `private[chisel3]`——用户不能直接 `new`，只能走工厂。
- [Object.scala:35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L35)：`private val tpe = Property[className.Type]()`——每个 Object 自带一个 `Property[ClassType]`，`getReference` 返回它（[Object.scala:59](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L59)），用于把「这个对象实例」当作属性值传递。
- [Object.scala:65-70](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L65-L70)：`getField[T](name)`——注释明确 *WARNING: 调用方自行保证字段存在且类型/方向正确*；它 `setRef(Node(this), name)` 并 `bind(ObjectFieldBinding(_parent.get))`。

`DynamicObject` 工厂的 `unsafeGetDynamicObject`：

- [Class.scala:189-214](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L189-L214)：按类名造 `DynamicObject`，根据当前上下文是 `RawModule` 还是 `Class` 分别压 `DefObject` 并绑 `OpBinding`/`ClassBinding`。注释两次警告 *caller's responsibility to ensure the Class exists*。
- [Binding.scala:106](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L106)：`case class ClassBinding(enclosure: Class) extends ConstrainedBinding with ReadOnlyBinding`——属性端口在 `Class` 体内的绑定，只读、受该 `Class` 约束。
- [Binding.scala:108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L108)：`ObjectFieldBinding`。

`DynamicObject._applyImpl`（直接包裹 `new Class{...}` 的入口）：

- [Object.scala:82-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L82-L95)：先 `Module.evaluate` 细化 `Class`，再 `unsafeGetDynamicObject`，最后 `setSourceClass` 以便收口回填 Class 的 ref。

`StaticObject`（类型安全版）：

- [Object.scala:105-117](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L105-L117)：`private[chisel3] class StaticObject(baseModule: BaseModule)`，由 `Instance[Class].getPropertyReference` 创建（[Class.scala:263-288](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Class.scala#L263-L288)）。

`DefObject` IR 节点与序列化：

- [IR.scala:371](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L371)：`case class DefObject(sourceInfo, id: HasId, className: String) extends Definition`。
- [Serializer.scala:293-294](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L293-L294)：序列化为 `object <name> of <className>`。

测试佐证（含 `getField` 字段连线）：

- [ObjectSpec.scala:67-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chiselTests/properties/ObjectSpec.scala#L67-L84)：`out := obj1.getField[Int]("out")` 产出 `object obj1 of Test` 与 `propassign out, obj1.out`。
- [ClassSpec.scala:151-176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L151-L176)：`@instantiable class Test extends Class` + `@public val in/out` + `Instance(cls)`，能写出 `obj2.in := obj1.out`——这是 StaticObject 的类型安全写法。

#### 4.3.4 代码实践

**目标**：实例化一个 `Class` 得到两个 `Object`，把其中一个的输出字段连到另一个的输入字段，观察 `object ... of ...` 与字段级 `propassign`。

**操作步骤**（直接取自 [ClassSpec.scala:151-176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L151-L176) 的类型安全写法）：

```scala
// 示例代码
import chisel3._
import chisel3.experimental.hierarchy.{instantiable, public, Definition, Instance}
import chisel3.properties.{Class, Property}
import circt.stage.ChiselStage

@instantiable
class Pipe extends Class {
  @public val in  = IO(Input(Property[Int]()))
  @public val out = IO(Output(Property[Int]()))
  out := in
}

val chirrtl = ChiselStage.emitCHIRRTL(new RawModule {
  val cls = Definition(new Pipe)
  val obj1 = Instance(cls)
  val obj2 = Instance(cls)
  obj2.in := obj1.out      // 对象间字段连线
})
println(chirrtl)
```

**需要观察的现象**：CHIRRTL 含 `class Pipe`、`object obj1 of Pipe`、`object obj2 of Pipe`、`propassign obj2.in, obj1.out`。

**预期结果**：与 [ClassSpec.scala:168-175](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L168-L175) 的 FileCheck 完全一致。

> **对比练习**：把 `Instance(cls)` 换成 `Class.unsafeGetDynamicObject("Pipe")` + `getField[Int]("in")`，也能得到同样的 CHIRRTL（参见 [ObjectSpec.scala:86-100](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/ObjectSpec.scala#L86-L100)），但失去了编译期类型检查——拼错字段名要等到运行期才暴露。

#### 4.3.5 小练习与答案

**练习 1**：`DynamicObject` 的 `getField[Int]("out")` 与 `Instance` + `@public` 的 `obj.out`，生成的 IR 有区别吗？

> **答案**：没有。二者最终都生成 `obj.out` 形式的 ref（`Node(obj)` + 字段名）并绑成 `ObjectFieldBinding`，序列化出同样的 `propassign ..., obj.out`。区别只在用户侧的类型安全性（[Object.scala:61-70](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L61-L70)）。

**练习 2**：为什么 `DynamicObject` 的主构造器是 `private[chisel3]`？

> **答案**：为了保证实例化总是经过受控的工厂（`Class.unsafeGetDynamicObject` 或 `DynamicObject._applyImpl`），这些工厂负责在正确的上下文里压 `DefObject` 命令、绑定 `Property[ClassType]` 的 ref、记录源 `Class`，否则用户直接 `new` 会绕过这些登记（[Object.scala:33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Object.scala#L33)）。

---

### 4.4 Path：在属性里指向真实硬件

#### 4.4.1 概念说明

到目前为止，属性装的都是「纯数据」（整数、字符串……）。但 OMR 经常需要**引用设计里的真实硬件**——例如「这个寄存器映射项对应硬件里的 `Top/inst/data` 这根线」。`Path` 就是这个引用类型：把一个模块实例、一根 `Data`、一块 `MemBase` 包成 `Path`，放进 `Property[Path]`。

`Path` 的难点在于**作用域**：一个 `Path` 只能引用「当前模块或其后代」的硬件——你不能在一个与目标毫无祖孙关系的模块里造指向它的 Path，因为下游 firtool 无法解析跨层引用。`Path` 因此在构造时捕获 `Module.currentModule` 作为「观察点」，并算出**相对目标**（`toRelativeTarget`）。

`Path` 还区分两种目标类型：实例目标（`OMInstanceTarget`，指向一个模块实例）与引用目标（`OMReferenceTarget`，指向模块内的某根线/某块内存）；以及「成员（Member）」变体（`OMMember*Target`），用于更细粒度的对象模型定位。

#### 4.4.2 核心流程

构造一个 `Path` 并赋值：

```
Property(Path(inst.data))
  → Path.apply(data: Data):
      - 捕获 scope = Module.currentComponent/Module.currentModule（观察点）
      - 返回匿名 TargetPath：toTarget() = data.toRelativeTarget(scope)
  → Property.apply(lit: Path):  // pathTypeInstance 把 Path 映射到 fir.PathPropertyType
      - convertUnderlying 调 path.convert()
      - TargetPath.convert():
          - 据 isMemberPath 与 target 的具体子类(ModuleTarget/InstanceTarget/ReferenceTarget)
            选出 "OMReferenceTarget"/"OMInstanceTarget"/"OMMember*Target" 等前缀
          - PathPropertyLiteral("OMReferenceTarget:<target.serialize>")
  → 序列化为 propassign propOut, path("OMReferenceTarget:~|Top/inst:Foo>data")
```

关键：`Path` 的字面量是一个**字符串**（`"OMReferenceTarget:..."`），这个字符串编码了从观察点到目标的相对路径，下游 firtool 据此在对象模型 JSON 里写一个指向真实硬件的引用。

#### 4.4.3 源码精读

`Path` 抽象与目标子类：

- [Path.scala:12-14](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala#L12-L14)：`sealed abstract class Path`，唯一方法 `convert(): PathPropertyLiteral`。
- [Path.scala:18-39](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala#L18-L39)：`TargetPath`，`convert` 依据 `isMemberPath` 与 target 子类拼出 `OMInstanceTarget`/`OMReferenceTarget`/`OMMemberInstanceTarget`/`OMMemberReferenceTarget` 前缀。
- [Path.scala:43-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala#L43-L45)：`DeletedPath`——指向「已不存在」的目标，序列化为 `OMDeleted:`。

构造方法（重载）：

- [Path.scala:51-58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala#L51-L58)：`Path.apply(module: BaseModule)`——捕获 `Module.currentComponent` 作 scope，`toTarget = module.toRelativeTarget(scope)`。
- [Path.scala:63-71](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala#L63-L71)：`Path.apply(data: Data)`，同样捕获 scope。
- [Path.scala:75-83](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala#L75-L83)：`Path.apply(mem: MemBase[_])`。

`Path` 作为 `PropertyType` 实例：

- [Property.scala:155](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L155)：`implicit val pathTypeInstance: SimplePropertyType[Path]`，映射到 `fir.PathPropertyType`，`convert` 调 `_.convert()`。
- [Property.scala:157-189](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L157-L189)：`modulePathTypeInstance` 等一系列实例，让任意 `BaseModule`/`Data`/`MemBase` 都能**直接**当 `Property` 字面量（自动包成 `Path`）——这就是为什么测试里能写 `Property(inst.data)` 而不必显式 `Path(...)`。

测试佐证（最权威的「值→文本」凭证）：

- [PropertySpec.scala:161-201](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L161-L201)：把模块实例、`Wire`、`SyncReadMem`、`SRAM` 各自包成 `Path`，断言序列化结果，例如 `propassign propOutB, path("OMReferenceTarget:~|Top/inst:Foo>data")`、`propassign propOutA, path("OMInstanceTarget:~|Top/inst:Foo")`。
- [PropertySpec.scala:236-249](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L236-L249)：验证「Path 不能引用非祖先模块里的目标」——跨域引用会在 elaboration/firtool 期被拒。

#### 4.4.4 代码实践

**目标**：在一个顶层模块里实例化一个子模块，用 `Property[Path]` 端口把「子模块实例」与「子模块内的一根线」作为元数据暴露出来，观察 `path(...)` 字面量。

**操作步骤**（精简自 [PropertySpec.scala:161-201](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L161-L201)）：

```scala
// 示例代码
import chisel3._
import chisel3.properties.{Path, Property}
import circt.stage.ChiselStage

class Top extends Module {
  override def desiredName = "Top"
  val propOutA = IO(Output(Property[Path]()))   // 指向子模块实例
  val propOutB = IO(Output(Property[Path]()))   // 指向子模块内一根线
  val inst = Module(new Module {
    override def desiredName = "Foo"
    val data = WireInit(false.B)
  })
  propOutA := Property(inst)            // 等价 Property(Path(inst))
  propOutB := Property(inst.data)       // 等价 Property(Path(inst.data))
}

println(ChiselStage.emitCHIRRTL(new Top))
```

**需要观察的现象**：CHIRRTL 含
`propassign propOutA, path("OMInstanceTarget:~|Top/inst:Foo")` 与
`propassign propOutB, path("OMReferenceTarget:~|Top/inst:Foo>data")`。

**预期结果**：与 [PropertySpec.scala:192-194](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L192-L194) 的断言一致。`~|Top/inst:Foo` 即「从顶层 `~` 经实例路径 `Top/inst` 到模块 `Foo`」，`>data` 表示模块内的 `data` 引用。

> **进阶（待本地验证）**：`Path` 与所有属性一样不会进 Verilog。若要看到最终的对象模型 JSON，需在 firtool 侧启用对象模型发射（如 `-emit-omir` 之类的 firtool 选项，经 `ChiselStage` 的 `firtoolOpts` 透传，参见 u5-l4）。具体开关名与输出路径请以本地 `firtool --help` 为准。

#### 4.4.5 小练习与答案

**练习 1**：`Property(inst)` 与 `Property(Path(inst))` 效果一样吗？为什么能省略 `Path`？

> **答案**：一样。因为 [Property.scala:157-164](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala#L157-L164) 的 `modulePathTypeInstance` 为所有 `BaseModule` 提供了隐式转换，`convertUnderlying` 自动把模块包成 `Path(module)`。

**练习 2**：`OMInstanceTarget` 与 `OMReferenceTarget` 的区别是什么？

> **答案**：前者指向一个**模块实例**整体（如 `Top/inst:Foo`），后者指向模块内的**某根具体信号/引用**（如 `Top/inst:Foo>data`）。判定逻辑在 [Path.scala:22-38](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Path.scala#L22-L38)，依据 target 是 `ModuleTarget`/`InstanceTarget` 还是 `ReferenceTarget`。

---

## 5. 综合实践

把四个最小模块串起来，构造一个**最小但完整**的对象模型：用 `Class` 定义一个「设备描述」对象模型，含字符串名称、整数基址、以及一个 `Path` 指向真实硬件寄存器；在顶层实例化它（`Object`），并通过 `Property[ClassType]` 把对象引用传到顶层端口。

**实践目标**：验证一条完整的 OMR 数据流——Scala 对象模型 → `Class`/`Object`/`Property[Path]` → CHIRRTL 的 `class`/`object`/`propassign ... path(...)`。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import chisel3.experimental.hierarchy.{instantiable, public, Definition, Instance}
import chisel3.properties.{Class, Path, Property}
import circt.stage.ChiselStage

// 1) 定义对象模型：一个 Class，含字符串、整数、Path 三类属性
@instantiable
class DeviceDesc extends Class {
  override def desiredName = "DeviceDesc"
  @public val name   = IO(Input(Property[String]()))
  @public val base   = IO(Input(Property[Int]()))
  @public val regref = IO(Input(Property[Path]()))
  @public val json   = IO(Output(Property[String]()))
  json := name
}

class Top extends RawModule {
  override def desiredName = "Top"
  // 真实硬件：一个寄存器
  val reg = RegInit(0.U(8.W))
  // 实例化对象模型
  val defn = Definition(new DeviceDesc)
  val dev  = Instance(defn)
  // 用字面量驱动对象的输入属性
  dev.name   := Property("uart0")
  dev.base   := Property(0x1000)
  dev.regref := Property(reg)             // Path 指向真实硬件寄存器
  // 顶层端口：把「这个对象实例」作为属性暴露
  val om     = IO(Output(defn.getPropertyType))
  om := dev.getPropertyReference
}

println(ChiselStage.emitCHIRRTL(new Top))
```

**需要观察的现象（逐一核对）**：

1. 出现 `class DeviceDesc :`，下含 `input name : String`、`input base : Integer`、`input regref : Path`、`output json : String`。
2. 出现 `object dev of DeviceDesc`。
3. 出现若干 `propassign dev.name, String("uart0")`、`propassign dev.base, Integer(4096)`、`propassign dev.regref, path("OMReferenceTarget:~|Top>reg")`。
4. 顶层端口 `om : Inst<DeviceDesc>`，并有 `propassign om, dev`（对象引用作为属性值传递）。

**预期结果**：上述各项均能在 CHIRRTL 中找到对应文本；`emitSystemVerilog` 不会出现任何 `name/base/regref/om` 端口——它们全部是非硬件元数据，留给下游 firtool 抽取为对象模型 JSON。

**核对依据**：`class`/端口/`propassign` 见 [ClassSpec.scala:47-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/properties/ClassSpec.scala#L47-L84)；`object ... of ...` 与 `propassign om, obj1` 见 [ObjectSpec.scala:34-39](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/ObjectSpec.scala#L34-L39)；`path(...)` 见 [PropertySpec.scala:192-194](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala-2/chiselTests/properties/PropertySpec.scala#L192-L194)。

> 若环境暂无法运行 `./mill`，可把上述断言当作「待本地验证」的预期清单，逐条对照源码与测试理解。

## 6. 本讲小结

- **`Property[T]` 是非硬件数据**：继承 `Element` 但禁用 `asUInt`/位宽/被 `UInt` 驱动，不综合成信号；由 `PropertyType` 类型类在编译期把 `Int/String/Boolean/Path/ClassType` 映射成 FIRRTL 属性类型，连线走专用的 `MonoConnect.propConnect` 发出 `PropAssign`。
- **`Class` 是属性世界的 `Module`**：继承 `BaseModule`，端口只能是 `Property`，体内只允许 `PropAssign`/`PropertyAssert`/`DefObject` 三类命令，收口产出 `DefClass`（序列化为 `class`）。
- **`Object` 是 `Class` 的实例**：`DynamicObject`（按字符串名取字段、不安全）与 `StaticObject`（配合 `Definition`/`Instance`/`@instantiable`/`@public`、类型安全）二者同产 `DefObject`（`object ... of ...`）；对象引用以 `Property[ClassType]` 在端口间传递。
- **`Path` 在属性里指向真实硬件**：把模块实例/信号/内存包成 `Path`，捕获当前 scope 算相对目标，序列化为 `path("OMReferenceTarget:~|...>...")`；只能引用祖先链上的目标。
- **OMR 是把这些机制用起来的方法学**：用 `Class`+`Object` 建对象图、用 `Property[Path]` 让对象图引用真实硬件、整张图随设计进入 FIRRTL IR，由下游 CIRCT/firtool 抽取为 JSON 元数据。
- **IR 与绑定的一一对应**：`PropertyLit`（字面量，绑 `PropertyValueBinding`）、`PropExpr`（运算树）、`PropAssign`（赋值）、`PropertyAssert`（断言）、`DefObject`（对象实例）、`DefClass`（类模块）；绑定侧 `ClassBinding`（只读、受 Class 约束）、`ObjectFieldBinding`（对象字段）。

## 7. 下一步学习建议

- **u9-l4（Annotation 注解系统）**：Properties 的对象模型最终多以注解/JSON 形式离开 Chisel，建议结合 Annotation 系统理解「元数据如何穿越 Phase 管道」。
- **u8-l3（Layers 与 Probe）**：Layer 也是「可选地剥离代码」的机制，与 Properties 的「非硬件数据」对照阅读，能更清楚 Chisel 区分「硬件 / 验证 / 元数据」三套世界的设计意图。
- **下游 firtool**：本讲止步于 CHIRRTL。要看到真正的对象模型 JSON，需阅读 CIRCT/firtool 的 OMIR 发射文档与 `-emit-omir` 类选项（待本地验证）。
- **继续读源码**：[Property.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/properties/Property.scala) 里 `PropertyArithmeticOps`/`PropertyEqualityOps`/`PropertyBooleanOps` 等类型类揭示了属性的运算语义（`+`/`===`/`&&` 生成 `PropExpr`），可作为深入练习。
