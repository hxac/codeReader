# Connectable 与连线操作符

## 1. 本讲目标

本讲聚焦 Chisel 连线操作符背后的统一抽象。学完后你应该能够：

1. 说出 Chisel 现代连线体系里的四个核心算子 `:<=`、`:>=`、`:<>=`、`:#=` 各自的连接方向与适用场景。
2. 解释 `Connectable` 这个数据结构为何能把「被连接的硬件值」与「一份选择性豁免清单（waive / squeeze / exclude）」绑在一起。
3. 看懂 `Connection` trait 用 8 个布尔标志描述算子语义、并用一个递归算法 `doConnection` 同时实现四种算子的设计。
4. 理解 `connectable` 包对象如何通过隐式类，把上述算子「挂」到任意 `Data` 上，以及老接口 `:=` / `<>` 与新算子的映射关系。

---

## 2. 前置知识

在进入源码前，先统一三个直觉。

**(1) 连线不是赋值，是「按叶子匹配」。**
两个 `Bundle` 用一行 `a <> b` 连起来时，Chisel 不会把整块数据当一个整体赋值，而是递归拆到**叶子信号**（`UInt`/`Bool`/`Clock` 等 `Element`），逐个按方向决定谁驱动谁。这一点 u3-l2 讲过的方向模型是前提：每个叶子都有自己的 `ActualDirection`（最终是 input 还是 output）。

**(2) 「consumer / producer」是连线双方的固定角色。**
不管算子怎么写，操作符**左边**永远叫 consumer（消费者），**右边**永远叫 producer（生产者）。注意这与「谁是 input、谁是 output」是两回事——consumer 完全可以是个 input 端口。这两个名字只是描述你在源码里写在 `:` 的哪一侧。

**(3) 连线只「登记命令」，不立刻生成硬件。**
和 u1-l4、u3-l1 强调的一样，`a := b` 这一行在 elaboration 期只是往当前模块的命令队列里 `pushCommand` 了一条连接命令，真正固化成 FIRRTL `connect` 节点发生在收口阶段。本讲的全部机制都运行在这个「登记」阶段。

> 术语对照：本讲会频繁出现「aligned（同向）」「flipped（反向）」两个词，指叶子相对于 consumer 根的方向，与 u3-l2 的 `SpecifiedDirection.Flip` 一脉相承。

---

## 3. 本讲源码地图

本讲涉及的核心源码集中在 `core` 子项目的 `connectable` 包，外加 `Data.scala` 里老接口与隐式桥接的两段：

| 文件 | 作用 |
|---|---|
| [core/src/main/scala/chisel3/connectable/Connectable.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala) | 定义 `Connectable` 数据结构（被连数据 + 豁免清单），以及把 `:<=`/`:>=`/`:<>=`/`:#=` 四个算子挂上去的 `ConnectableOpExtension` 隐式类。 |
| [core/src/main/scala/chisel3/connectable/Connection.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala) | 定义 `Connection` trait 与四个 `case object`（算子语义），以及真正干活的递归算法 `doConnection`。 |
| [core/src/main/scala/chisel3/connectable/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/package.scala) | `connectable` 包对象，用隐式类把算子提供给所有 `Data`/`Vec`/`DontCare`。 |
| [core/src/main/scala/chisel3/connectable/Alignment.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala) | 辅助：`Alignment` 描述「叶子相对于根是同向还是反向」，是算子做方向决策的依据。 |
| [core/src/main/scala/chisel3/Data.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala) | 老接口 `:=`/`<>` 的定义，以及 `makeConnectableDefault` 等桥接逻辑。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：先讲 **Connectable**（被连数据的清单），再讲 **Connection**（算子语义与算法），最后讲 **connectable 包对象**（把算子挂到每个 `Data` 上）。

### 4.1 Connectable：被连接数据的「清单」

#### 4.1.1 概念说明

设想你要把两个 `Bundle` 连起来，但它们的字段「并不完全一样」：consumer 里有个 `error` 字段 producer 没有；某个字段你不想被自动截断位宽；又或者某个调试字段你这次根本不想连。如果连线算法只接受「字段完全相同」的两侧，这种现实需求就无法表达。

`Connectable` 就是为解决这个问题引入的包装类型：它把一个硬件值 `base` 与**三份可选集合**绑在一起——

- **waived（豁免）**：这个叶子即使最终没人连、或连不上，也不报错。
- **squeezed（挤压）**：允许这个叶子的位宽被悄悄截断而不报错。
- **excluded（排除）**：这个叶子完全不参与本次连线。

换句话说，`Connectable` = 一个硬件值 + 一份关于「哪些叶子可以放宽规则」的清单。这套清单是**纯数据**，本身不连线；连线时由 `Connection` 算法读取它来做决策。

#### 4.1.2 核心流程

`Connectable` 的典型生命周期是：

```
Data (硬件值)
   │  Data.makeConnectableDefault  ──┐
   ▼                                  │ 隐式触发
Connectable.apply(d)  ────────────────┘   生成空清单
   │
   │  链式调用 .waive(...) / .squeeze(...) / .exclude(...)
   ▼
Connectable(d, waived=..., squeezed=..., excluded=...)
   │
   ▼  交给算子 :<= / :>= / :<>= / :#= 读取
Connection.connect(consumer, producer, op)
```

`Connectable` 是不可变设计：`waive`/`squeeze`/`exclude` 都不修改自身，而是通过内部的 `copy` 返回一个带更新后清单的新 `Connectable`，因此可以链式调用。

#### 4.1.3 源码精读

先看 `Connectable` 的主类定义与四个字段：

[Connectable.scala:17-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L17-L22) —— `final class Connectable[+T <: Data]`，私有构造，持有 `base` 与三份 `Set[Data]`。构造时立刻 [Connectable.scala:23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L23) 用 `requireIsHardware(base, ...)` 守门，确保只能包装已绑定的硬件值，不能包装纯 Chisel 类型。

三个清单操作都走同一个 `copy` 模式。以 `waive` 为例：

[Connectable.scala:45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L45) —— `waive(members: (T => Data)*)` 接收一组「从 base 取叶子的函数」，把它们并入 `waived` 集合并返回新 `Connectable`。`squeeze`（[Connectable.scala:79-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L79-L81)）、`exclude`（[Connectable.scala:108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L108)）结构相同。

> 为什么传 `(T => Data)` 而不是直接传 `Data`？因为这样写 `io.waive(_.error)` 时，调用点用的是类型 `T` 的字段选择器，编译器能保证你豁免的确实是 `base` 类型里存在的字段，比传裸 `Data` 更安全。

除了按字段名选，还有几个「批量」快捷方式：`waiveAll`（[Connectable.scala:64-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L64-L67)）用 `DataMirror.collectMembers` 把所有叶子全收集进 `waived`；`squeezeAll`（[Connectable.scala:93-96](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L93-L96)）同理。还有一个组合快捷方式 `unsafe`（[Connectable.scala:39](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L39)），等价于 `waiveAll.squeezeAll`，即「字段不匹配也连、能截断就截断、一切都不报错」，适合「我只管把能连的都连上」的粗放场景。

最后看工厂方法 `Connectable.apply`：

[Connectable.scala:143-158](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L143-L158) —— 用三个谓词函数 `waiveSelection`/`squeezeSelection`/`excludeSelection`（默认全返回 `false`）遍历 `base` 的所有叶子，筛出初始的三份集合。默认情况下三份集合都是空，即「什么都不豁免」的严格 `Connectable`，这也是普通 `a :<= b` 走的路径。

#### 4.1.4 代码实践

**实践目标**：体会 `Connectable` 的清单如何让「字段不完全匹配」的两个 Bundle 也能连。

**操作步骤**（示例代码，非项目原有）：

```scala
import chisel3._
import chisel3.connectable.ConnectableOperators // 把 :<= 等算子引入作用域

class Producer extends Bundle {
  val data  = UInt(8.W)
  val valid = Bool()
  val debug = UInt(8.W)   // producer 独有
}
class Consumer extends Bundle {
  val data  = UInt(8.W)
  val valid = Bool()
  // 没有 debug 字段
}

class Top extends Module {
  val p = IO(Input(new Producer))
  val c = IO(Output(new Consumer))
  // 直接 c :<= p 会因 debug 字段悬空报错
  // 用 waive 豁免 producer 端那个无人对接的 debug：
  c :<= p.waive(_.debug)
}
```

**需要观察的现象**：如果不加 `.waive(_.debug)`，elaboration 会报「unconnected producer field debug」；加上之后即可通过。

**预期结果**：生成 Verilog 时，`data`、`valid` 正常连上，`debug` 被忽略。

> 待本地验证：若本地已按 u1-l2 配好 mill 与 firtool，可用 `ChiselStage.emitSystemVerilog(new Top)` 打印结果；否则可作为「源码阅读型实践」——阅读 [Connectable.scala:45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L45) 确认 `waive` 返回的是新对象而非原地修改。

#### 4.1.5 小练习与答案

**练习 1**：`Connectable` 为什么用 `private` 构造、只暴露 `apply` 工厂？
**答案**：为了让「清单」只能通过受控的 `apply` 或 `copy` 生成，外部无法凭空造一个 `base` 与清单不匹配的 `Connectable`，保证清单里的 `Data` 一定来自 `base` 的子树。

**练习 2**：`waive` 和 `exclude` 都能让某个字段「不报错」，二者有何区别？
**答案**：`waive` 的字段如果**两侧都有**仍然会被连上，只是「一侧缺失时不报错」；`exclude` 则是**完全不参与**本次连线，连匹配的对端也会被视作悬空（见 4.2.4 的 Base Case 3）。

---

### 4.2 Connection：用 8 个布尔标志描述算子语义

#### 4.2.1 概念说明

`Connectable` 解决了「被连数据 + 清单」，但还差一个关键问题：**到底怎么连？** 两个同向叶子要不要连？反向叶子谁来驱动谁？位宽不一致截断还是报错？字段缺失算 dangling 还是 unconnected？

Chisel 的答案是：把每一种「连线风格」抽象成一个 `Connection`，用一组布尔标志精确描述它的规则。系统里只有 4 种 `Connection`，对应 4 个算子：

| 算子 | case object | 别名 | 直觉 |
|---|---|---|---|
| `:<=` | `ColonLessEq` | aligned connection | producer 单向驱动 consumer 中**同向**的叶子 |
| `:>=` | `ColonGreaterEq` | flipped / backpressure | consumer 单向驱动 producer 中**反向**的叶子（典型：回压 `ready`） |
| `:<>=` | `ColonLessGreaterEq` | bi-directional / "tur-duck-en" | 同时做 `:<=` 和 `:>=`，双向 |
| `:#=` | `ColonHashEq` | coercion / mono | **无视方向**，producer 一律驱动 consumer |

符号助记（来自源码注释）：`:` 代表 consumer 侧，`=` 代表 producer 侧；`<` 表示「从 producer 流向 consumer」，`>` 表示「从 consumer 流向 producer」，`#` 表示「忽略 flip，强制从 producer 到 consumer」。

#### 4.2.2 核心流程

`Connection` 算法是一个**对两棵类型树同步递归**的过程：

```
connect(consumerRoot, producerRoot, op)
   │
   ▼
doConnection(consumerAlign, producerAlign)
   │  对当前层的 (consumer叶/聚合, producer叶/聚合) 做模式匹配：
   ├── Base Case：任一侧为空 → 看 op 是否允许悬空；不允许则收集一条 error
   ├── Base Case：方向不匹配且 op 要求同向 → 收集 "inversely oriented" error
   ├── Base Case：位宽需要截断且 op 不允许 → 收集 "mismatched widths" error
   ├── Base Case：被 waived/excluded → 跳过
   └── Recursive Case：两侧都是聚合 → 按字段名 zip，对每对孩子递归
                         两侧都是叶子 → 由 op 标志决定 (l, r)，落到叶子 connect(l, r)
   │
   ▼ 收尾：若有 error，统一 Builder.error(...)
```

关键在于：**这套递归对四种算子是同一份代码**，行为差异完全由 `op` 的 8 个布尔标志驱动。这就是 `Connection` 设计的精髓——把「策略」与「机制」分离。

#### 4.2.3 源码精读

先看 `Connection` trait 的 8 个标志：

[Connection.scala:17-26](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L17-L26) —— 这 8 个标志的含义：

| 标志 | 含义 |
|---|---|
| `connectToConsumer` | 算子是否会驱动 consumer 侧的同向叶子 |
| `connectToProducer` | 算子是否会驱动 producer 侧的反向叶子 |
| `alwaysConnectToConsumer` | 是否无视方向、强制驱动 consumer（`:#=` 专属） |
| `noWrongOrientations` | 是否禁止「方向相反」的匹配（`:#=` 设为 `false`） |
| `noMismatchedWidths` | 位宽需要截断时是否报错 |
| `mustMatch` / `noDangles` / `noUnconnected` | 字段是否必须一一对应、悬空是否报错 |

再看四个 `case object` 如何赋值。`ColonLessEq`：

[Connection.scala:28-37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L28-L37) —— `connectToConsumer=true, connectToProducer=false, alwaysConnectToConsumer=false, noWrongOrientations=true`。即「只驱动 consumer 同向叶子、要求方向一致」。

`ColonGreaterEq`（[Connection.scala:39-48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L39-L48)）恰好镜像：`connectToConsumer=false, connectToProducer=true`。

`ColonLessGreaterEq`（[Connection.scala:50-58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L50-L58)）两个都开：`connectToConsumer=true, connectToProducer=true`。它还带一个优化方法 `canFirrtlConnect`（[Connection.scala:59-74](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L59-L74)）：如果两侧类型完全等价、且都没有任何 waive/squeeze/exclude，就可以跳过逐字段递归，直接发一条 FIRRTL `<=`。

`ColonHashEq`（[Connection.scala:77-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L77-L86)）是 coercion 算子：`alwaysConnectToConsumer=true`、`noWrongOrientations=false`。即「不管 flip，一律把 producer 接到 consumer」。

四种算子的标志总览：

| 算子 | connectToConsumer | connectToProducer | alwaysConnectToConsumer | noWrongOrientations |
|---|---|---|---|---|
| `:<=`  | T | F | F | T |
| `:>=`  | F | T | F | T |
| `:<>=` | T | T | F | T |
| `:#=`  | T | F | T | F |

接着看真正干活的入口 `Connection.connect`：

[Connection.scala:104-112](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L104-L112) —— 公有 `connect` 只是转发到 `doConnection`。

`doConnection` 是本讲最核心的算法（[Connection.scala:131-239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L131-L239)）。它的主体是一个嵌套函数，对 `(conAlign, proAlign)` 做模式匹配。几个关键分支：

- [Connection.scala:156-157](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L156-L157) —— 一侧有叶子、另一侧空，但被 `waived`/`excluded`：跳过，不报错。
- [Connection.scala:167-169](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L167-L169) —— 两侧都有但方向相反，且 `op.noWrongOrientations`：收集 "inversely oriented" 错误（`:#=` 因为此标志为 `false` 而放过这种情况）。
- [Connection.scala:172-176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L172-L176) —— 位宽会截断且 `op.noMismatchedWidths`：收集 "mismatched widths" 错误。
- [Connection.scala:179-182](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L179-L182) —— 真正的悬空：一侧有、一侧空、又没被豁免。此时 `errorWord(op)` 决定叫 "dangling" 还是 "unconnected"（[Alignment.scala:163-170](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala#L163-L170)）。
- [Connection.scala:185-227](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L185-L227) —— Recursive Case：两侧都是聚合时，用 `matchingZipOfChildren` 按字段名配对后递归；两侧都是叶子时，由 `alignment.computeLandR(...)`（[Alignment.scala:125-134](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala#L125-L134)）依据算子标志算出本次要连的 `(l, r)`，落到私有 `connect(l, r)`。

最后看「落到叶子」的私有 `connect`：

[Connection.scala:114-129](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L114-L129) —— 对普通叶子调用 `l := r`（即 4.3 要讲的老接口 `:=`，它最终 `pushCommand` 一条 `Connect`），对 `Analog` 类型走特殊的 `connectAnalog`（[Connection.scala:257-269](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L257-L269)，委托 `attach`）。注意这里用一个 `try/catch` 把异常转成 `Builder.error`——这就是为什么连线错误不会立刻抛栈、而是一次性收集多条错误（详见 u9-l2）。

#### 4.2.4 代码实践

**实践目标**：用源码阅读验证「同一个算法、不同算子」的设计。

**操作步骤**：

1. 打开 [Connection.scala:131-239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L131-L239) 的 `doConnection`。
2. 找到 Base Case 4（[Connection.scala:167-169](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L167-L169)），确认它依赖 `connectionOp.noWrongOrientations`。
3. 对照 `ColonHashEq`（[Connection.scala:81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L81)）的 `noWrongOrientations = false`，得出结论：只有 `:#=` 不会在「方向相反」时报错。

**需要观察的现象**：把同样的两个反向端口分别用 `:<>=` 和 `:#=` 连。

**预期结果**：`:<>=` 对反向叶子按 `:>=` 分支正确连上（不报错，因为这是它支持的语义）；`:#=` 则无视方向一律从 producer 接到 consumer。两者都不触发 Base Case 4 的 "inversely oriented" 错误，但**原因不同**——前者因为算子本来就双向，后者因为 `noWrongOrientations=false`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `:<>=` 在两侧类型完全等价、且无豁免清单时，能跳过递归直接发 FIRRTL `<=`？
**答案**：因为此时等价于「逐字段双向连，方向都能匹配上」，而 FIRRTL 的 `<=` 本身就是这个语义的更严格版本（见 [Connection.scala:59-74](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L59-L74) 与 [Connectable.scala:333-340](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L333-L340)）。这是一条纯优化快路径，行为不变。

**练习 2**：`errorWord` 在什么情况下返回 "dangling"，什么时候返回 "unconnected"？
**答案**：见 [Alignment.scala:163-170](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala#L163-L170)。当 consumer 是反向字段却出现在一个「只驱动 consumer 同向」的算子里、或 producer 是同向字段却出现在「只驱动 producer 反向」的算子里——即这个字段在该算子下本不该由对端提供——就叫 "dangling"（悬垂）；其余字段缺失叫 "unconnected"（未连接）。

---

### 4.3 connectable 包对象：把算子挂到每个 Data 上

#### 4.3.1 概念说明

至此 `Connectable` 与 `Connection` 都还只是「内部类型」。用户写 `a :<= b` 时，`a` 只是一个普通 `UInt` 或 `Bundle`——它身上并没有 `:<=` 方法。Scala 解决这个问题的标准手段是**隐式类**：`connectable` 包对象里定义了一组隐式类，在需要时自动把任意 `Data` 包成一个带 `:<=`/`:>=`/`:<>=`/`:#=` 方法的对象（其内部正是 `ConnectableOpExtension`）。

所以包对象承担两个职责：

1. **桥接**：`Data → Connectable`，让算子对所有硬件值可用。
2. **变体**：为 `Vec` 与 `Seq`、`DontCare` 等特殊情况提供额外重载。

同时，老接口 `:=` 与 `<>` 也在 `Data` 上直接定义，它们走的是另一条路（`MonoConnect`/`BiConnect`），但二者通过隐式转换 `toConnectableDefault` 共享了同一份 ScalaDoc 与算子清单。

#### 4.3.2 核心流程

用户写 `a :<= b` 时发生的事：

```
a :<= b          // a: UInt, b: UInt
   │  编译器找不到 UInt 上的 :<= 方法
   │  → 查找隐式类，命中 ConnectableOperators(a)
   ▼
new ConnectableOperators(a)
   └─ extends ConnectableOpExtension( Data.makeConnectableDefault(a) )
                                                        │
                                                        ▼
                                              Connectable.apply(a)  // 空清单
   │
   ▼  调用 :<= 的方法体
prefix(a) { connect(consumerConnectable, b, ColonLessEq) }   // → doConnection
```

而老接口 `a := b` 直接走 [Data.scala:822-826](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L822-L826) 的 `this.connect(that)` → `MonoConnect`（u3-l4 详解）。**注意**：4.2 讲到的现代算子在落到叶子时，最终也会调用这个 `:=`，二者在叶子层是汇合的。

#### 4.3.3 源码精读

包对象的第一个隐式类是核心：

[package.scala:18-19](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/package.scala#L18-L19) —— `implicit class ConnectableOperators[T <: Data](consumer: T) extends Connectable.ConnectableOpExtension(Data.makeConnectableDefault(consumer))`。它把任意 `Data` 包成 `ConnectableOpExtension`，于是 `:<=`/`:>=`/`:<>=`/`:#=` 全部可用。`makeConnectableDefault` 在 [Data.scala:925-932](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L925-L932)，先用 `Connectable.apply` 建空清单，再扫描 `HasCustomConnectable` 让自定义类型有机会改写清单。

接下来看四个算子在 `ConnectableOpExtension` 里的定义。以 `:<=` 为例：

[Connectable.scala:274-277](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L274-L277) —— `final def :<=[S <: Data](lProducer: => S)(implicit evidence: T =:= S, sourceInfo: SourceInfo)`。两个要点：① 参数是**按名调用**（`=> S`），所以 RHS 可以安全地引用 consumer 的命名上下文（`prefix(consumer.base) { lProducer }` 把生成的中间信号挂到 consumer 名下，这就是生成的 Verilog 里 `_T` 名字能跟对地方的原因）；② 隐式证据 `T =:= S` 强制两侧 Chisel 类型相同。`:>=`（[Connectable.scala:295-298](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L295-L298)）、`:#=`（[Connectable.scala:362-365](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L362-L365)）结构一致，只是传入不同的 `Connection`。

`:<>=` 多了一步快路径：

[Connectable.scala:333-340](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L333-L340) —— 先用 `ColonLessGreaterEq.canFirrtlConnect` 判断能否走优化路径，能则 `doFirrtlConnect`（[Connectable.scala:318-326](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L318-L326)）直接发一条 FIRRTL `<=`（经 `firrtlConnect`，见 [Element.scala:64-72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L64-L72) 与 [Aggregate.scala:95-102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L95-L102)，即 `pushCommand(Connect(...))`），否则退回完整递归 `connect(..., ColonLessGreaterEq)`。

再看两个变体重载。`ConnectableVecOperators` 提供 `Vec` 与 Scala `Seq` 之间的连线：

[package.scala:32-38](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/package.scala#L32-L38) —— `Vec[T] :<= Seq[T]` 先校验长度一致（不一致 `Builder.error`），再逐元素 `a :<= b`。`:>=`/`:<>=`/`:#=`（[package.scala:45-87](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/package.scala#L45-L87)）同理。

`ConnectableDontCare` 让 `DontCare :>= producer` 合法（[package.scala:89-101](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/package.scala#L89-L101)），用于显式声明「这一侧我不在乎」。`DontCare` 本身定义在 [Data.scala:1245-1278](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L1245-L1278)，连线时会被翻译成 FIRRTL 的 `DefInvalid`（见 [Element.scala:67-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L67-L68)）。

最后看老接口。`:=` 与 `<>`：

[Data.scala:822-826](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L822-L826) —— `:=` 的注释明确写道：在 `chisel3._` 命名空间下，它「等价于 `this :#= that`」，即单向、无视 flip 的强连。它调用 [Data.scala:546-566](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L546-L566) 的 `connect`，最终走 `MonoConnect`。

[Data.scala:839-843](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L839-L843) —— `<>` 的注释写道：在 `chisel3._` 下走 `BiConnect` 算法，「近似等价于 `:<>=`」，但语义更复杂、未来可能被弃用。它调用 [Data.scala:567-588](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L567-L588) 的 `bulkConnect`。

> 一句话总结映射：`:=` ≈ `:#=`（mono/coercion），`<>` ≈ `:<>=`（bi-directional）。现代代码推荐显式用 `:<=`/`:>=`/`:<>=`/`:#=`，语义更清晰；`MonoConnect`/`BiConnect` 的内部细节是下一讲 u3-l4 的内容。

#### 4.3.4 代码实践

**实践目标**：亲手用 `<>`（≈ `:<>=`）与 `:#=` 连同一对端口，对比生成 Verilog 的差异。

**操作步骤**（示例代码，非项目原有）：

```scala
import chisel3._
import chisel3.connectable.ConnectableOperators

// 一个带「反向字段」的 Bundle，模拟握手：data/valid 同向，ready 反向
class Handshake extends Bundle {
  val data  = UInt(8.W)
  val valid = Bool()
  val ready = Flipped(Bool())   // 相对根是 flipped
}

class Bidir extends Module {
  val a = IO(new Handshake)       // 默认整体 Output
  val b = IO(Flipped(new Handshake)) // 整体翻转，b 的 ready 就变成同向了
  a :<>= b                        // 双向连：data/valid 一路、ready 反一路
}
```

把 `a :<>= b` 换成 `a :#= b` 再生成一次。

**需要观察的现象**：

- `:<>=` 会同时产生 `data`/`valid` 的正向连线和 `ready` 的反向连线（双向）。
- `:#=` 无视方向，把 `a` 的所有字段（含 `ready`）都强制由 `b` 驱动，可能产生「两个 driver」或被方向检查拦截。

**预期结果**：对比两份 Verilog 的 `assign` 方向。`:#=` 在含反向字段的 Bundle 上通常会触发方向/多驱动错误，这正是 `noWrongOrientations=false` 的代价——它假设你真的要「强制覆盖」。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `:<=` 的 producer 参数要声明成按名调用 `=> S`，而不是普通的 `S`？
**答案**：因为 producer 表达式里可能构造新的中间信号（如 `a :<= (b + c)` 的 `b + c`），按名调用配合 [Connectable.scala:275](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L275) 的 `prefix(consumer.base) { lProducer }`，能让这些中间信号继承 consumer 的命名前缀，使生成的 Verilog 名字更有可读性、更易调试。

**练习 2**：`Vec :<= Seq` 与 `Vec := Seq`（[Aggregate.scala:338-345](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L338-L345)）在长度不一致时行为有何不同？
**答案**：`Vec :<= Seq`（[package.scala:33-36](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/package.scala#L33-L36)）长度不一致时调用 `Builder.error` 收集一条错误并继续；而 `Vec := Seq`（[Aggregate.scala:339-342](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L339-L342)）用 `require` 直接抛异常、立即中止。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「同一个 Bundle、四种算子、四种结果」的对比实验。

**任务**：定义一个带反向字段的 Bundle，实例化两个端口，分别用 `:<=`、`:>=`、`:<>=`、`:#=` 连接，对照生成的 CHIRRTL/Verilog 解释每个算子的行为。

**建议步骤**：

1. 写如下模块（示例代码）：

   ```scala
   import chisel3._
   import chisel3.connectable.ConnectableOperators

   class Link extends Bundle {
     val payload = UInt(8.W)
     val flag    = Bool()
     val ack     = Flipped(Bool())   // 反向字段，模拟回压
   }

   class Demo extends Module {
     val p = IO(Output(new Link))     // producer 侧
     val c = IO(Flipped(new Link))    // consumer 侧（整体翻转）
     // 任选其一取消注释观察：
     // p :<=  c
     // p :>=  c
     // p :<>= c
     // p :#=  c
   }
   ```

2. 对照 [Connection.scala:28-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L28-L86) 的四个标志表，先**预测**每个算子会连哪些字段、哪些会报错。

3. 实际生成 CHIRRTL（`ChiselStage.emitCHIRRTL(new Demo)`）核对：哪些字段产生了 `connect`、哪些被跳过、是否出现错误信息。重点关注 `ack` 这个反向字段在不同算子下的方向。

4. 进阶：给 `p` 加一份豁免清单，例如 `p.waive(_.ack) :<= c`，再对照 [Connectable.scala:45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connectable.scala#L45) 与 [Connection.scala:156-157](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala#L156-L157)，确认豁免后原本的 "unconnected" 错误消失。

**预期成果**：你能不看源码，仅凭算子的 4 个布尔标志推断出生成的连线方向，并解释 `Connectable` 清单如何抑制错误。若本地无法运行 firtool，至少完成「预测 + 阅读源码验证」部分，并标注「待本地验证」。

---

## 6. 本讲小结

- Chisel 现代连线体系由四个算子构成：`:<=`（aligned，单向）、`:>=`（flipped/backpressure，单向反向）、`:<>=`（双向，tur-duck-en）、`:#=`（coercion，无视方向强制连）。
- `Connectable` = 硬件值 `base` + 三份豁免清单（waived/squeezed/excluded），用不可变 `copy` 支持链式调用，用来表达「字段不完全匹配」时的放宽规则。
- `Connection` trait 用 8 个布尔标志精确描述算子语义；一个统一的递归算法 `doConnection` 通过读取这些标志，同时实现四种算子——策略与机制分离。
- 递归到叶子时，现代算子最终落到私有 `connect(l, r)` → `l := r`，与老接口汇合；异常被 `try/catch` 转成 `Builder.error`，所以连线错误是一次性收集的。
- `connectable` 包对象用隐式类 `ConnectableOperators` 把算子挂到任意 `Data` 上，并为 `Vec`↔`Seq`、`DontCare` 提供重载。
- 老接口映射：`:=` ≈ `:#=`（mono），`<>` ≈ `:<>=`（bi）；二者内部走 `MonoConnect`/`BiConnect`，是下一讲的主题。

---

## 7. 下一步学习建议

本讲讲了「算子语义与调度算法」，但故意把 `l := r` 落到叶子后的事当黑盒。下一讲 **u3-l4「连线的内部实现：MonoConnect 与 BiConnect」** 会打开这个黑盒：

- 阅读 [core/src/main/scala/chisel3/internal/MonoConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala) 与 [BiConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala)，理解老接口 `:=`/`<>` 的方向匹配算法。
- 对比「新 `Connection.doConnection`」与「老 `MonoConnect`/`BiConnect`」两套递归，体会为何社区倾向于用前者替代后者。
- 之后可继续前往 u3-l5（`when` 与 `Reg`）、u3-l6（`Mem`），把时序逻辑与存储器补齐。
