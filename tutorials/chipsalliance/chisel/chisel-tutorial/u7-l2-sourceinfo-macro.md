# SourceInfo 宏与隐式源信息

> 所属单元：u7 编译器插件与宏　|　前置讲义：u7-l1 ChiselPlugin 编译器插件

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「源信息（SourceInfo）」在 Chisel 里到底是什么、解决什么问题。
- 理解 `(implicit sourceInfo: SourceInfo)` 这种签名背后的两套**编译期**机制：一套负责「在调用点捕获文件名/行号」，另一套负责「把公开 API 改写成内部 `do_` 方法」。
- 画出从用户写下一行 `val r = RegNext(in)` 到 `Builder` 拿到 `SourceLine(file, line, col)` 的完整链路。
- 区分 `SourceLine`、`UnlocatableSourceInfo`、`DeprecatedSourceInfo` 三种源信息的含义与各自出现的场景。
- 把本讲与 u7-l1（scalac 编译器插件）区分开：插件负责「命名」，本讲的宏负责「定位」，二者都是编译期设施但各管一摊。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是源信息？** 当你写 `val r = RegNext(in)`，Chisel 在 elaboration（细化）时会把这一行翻译成内部 IR 节点（`DefReg` + `Connect`）。如果日后下游报错（比如位宽不匹配、方向冲突），Chisel 想告诉你「错在你源码的第几行」，就必须在**生成节点的那一刻**就把「文件名 + 行号 + 列号」一起记进节点。这份「这条硬件来自源码哪里」的元数据，就是 **SourceInfo**。

**为什么用宏而不是普通运行时取栈？** Scala 运行时也能 `new Exception().getStackTrace` 取行号，但代价昂贵、且在宏展开/匿名函数里栈会被打乱。Chisel 选择在**编译期**就把行号写成字面量（直接把 `42` 这个数字编进字节码），运行时零开销。这只能用 Scala 宏（macro）做到——宏能在编译期读到「这一行代码在源文件中的位置」。

**Scala 宏的两副面孔。** 本讲会同时遇到两种宏，先记住名字：
- **隐式宏（implicit macro）**：长得像 `implicit def materialize: SourceInfo = macro ...`，当编译器找不到隐式 `SourceInfo` 时自动触发，负责**捕获位置**。
- **黑盒改写宏（blackbox macro）**：长得像 `def +(that: T) = macro SourceInfoTransform.thatArg`，在编译期把一次方法调用**改写**成另一个方法调用，负责**桥接公开 API**。

二者最终会**组合**在一起完成一次注入。下面逐层拆开。

## 3. 本讲源码地图

| 文件 | 所属子项目 | 作用 |
| --- | --- | --- |
| `core/src/main/scala/chisel3/experimental/SourceInfo.scala` | core | 源信息的**数据模型**：`SourceInfo` trait 及 `SourceLine`/`UnlocatableSourceInfo`/`DeprecatedSourceInfo` 三个实现 |
| `core/src/main/scala-2/chisel3/experimental/SourceInfoIntf.scala` | core（仅 Scala 2） | 声明 `implicit def materialize` 隐式宏的接口 trait |
| `core/src/main/scala-2/chisel3/internal/SourceInfoMacro.scala` | core（仅 Scala 2） | `generate_source_info`：真正读 `enclosingPosition` 并产出 `SourceLine` 的宏实现 |
| `macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala` | macros | 一整套**黑盒改写宏**，把公开 API 改写成 `do_` 方法并插入 `implicitly[SourceInfo]` |
| `core/src/main/scala/chisel3/Reg.scala` | core | `RegNext.apply` —— 以「普通隐式参数」方式承接 `SourceInfo` 的典型例子 |
| `core/src/main/scala-2/chisel3/NumIntf.scala` | core（仅 Scala 2） | `+`/`*` 等运算符 —— 以「黑盒宏改写」方式承接 `SourceInfo` 的典型例子 |
| `core/src/main/scala/chisel3/internal/Builder.scala` | core | `pushOp` / `error`：源信息的**消费者**，把它写进 IR 节点或错误报告 |

> 提醒：Chisel 同时交叉编译 Scala 2 与 Scala 3。宏是两套语言里差异最大的部分，故 Scala 2 用 `scala.reflect.macros`（本讲主线），Scala 3 用 `inline def` + quote/splice（`core/src/main/scala-3/...SourceInfoIntf.scala`）。两套**语义等价**，本讲以 Scala 2 为主线精读。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，外加一节把三者串起来的链路分析：

- **4.1 SourceInfo 数据模型**（含 `DeprecatedSourceInfo`）
- **4.2 `materialize` 隐式宏：编译期捕获位置（机制 A）**
- **4.3 `SourceInfoTransform` 桥接宏：把公开 API 改写成 `do_`（机制 B）**
- **4.4 两套机制的协作：`RegNext(in)` 与 `a + b` 的完整链路**

### 4.1 SourceInfo：源信息的数据模型

#### 4.1.1 概念说明

`SourceInfo` 不是一个「值」，而是一个**封装「这条硬件来自源码哪里」的标签**。它设计成 `sealed` 家族，只有有限几种实现：

- `SourceLine(filename, line, col)` —— 真正带位置信息的「正常情况」。
- `UnlocatableSourceInfo` —— 「技术原因无法定位」，比如 `Reg` 因 Scala 宏不支持命名/默认参数而退化的兜底。
- `DeprecatedSourceInfo` —— 「这函数已废弃，懒得给它生成源信息」。

这种「和类型（sum type）」设计的好处是：消费方（错误报告、IR 序列化）必须用模式匹配处理每种情况，不会漏掉「没有位置信息」的分支。

#### 4.1.2 核心流程

源信息的生命周期：

```text
编译期宏捕获 (file, line, col)
        │
        ▼
   构造 SourceLine(...)          ← 正常情况
        │  (无法捕获时)
        ├──► UnlocatableSourceInfo   ← 技术限制兜底
        ├──► DeprecatedSourceInfo    ← 废弃 API 兜底
        ▼
   作为 implicit sourceInfo 沿调用链传递
        │
        ▼
   写进 IR 节点 (DefPrim/DefReg/Connect 都带 sourceInfo 字段)
        │
        ▼
   出错时由 Builder.error(m)(sourceInfo) 拼进错误信息
```

#### 4.1.3 源码精读

`SourceInfo` 是一个密封 trait，只要求两个能力：拼一条人类可读消息、（可能地）给出文件名：

[SourceInfo.scala:7-17](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/SourceInfo.scala#L7-L17) —— `sealed trait SourceInfo`，声明 `makeMessage` 与 `filenameOption` 两个契约。

「没有位置信息」的两种实现都收拢在 `NoSourceInfo` 子 trait 下：

[SourceInfo.scala:27-31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/SourceInfo.scala#L27-L31) —— `UnlocatableSourceInfo` 与 `DeprecatedSourceInfo` 两个 `case object`。注意 `UnlocatableSourceInfo` 的注释明确点出：「Scala macros don't support named or default arguments」，这正是后面 `RegNext` 为何走「普通隐式」而非「黑盒改写宏」的根因。

真正带位置的 `SourceLine`，它的 `serialize` 决定了源信息最终如何写进 FIRRTL 文本：

[SourceInfo.scala:37-51](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/SourceInfo.scala#L37-L51) —— `case class SourceLine(filename, line, col)`，`makeMessage` 产出形如 `@[file:line:col]` 的字符串，`serialize` 产出 FIRRTL 用的 `"file line:col"`。

> 顺带一提：`object SourceInfo` 里还有一个运行时**回退**手段 `materializeFromStacktrace`（[SourceInfo.scala:83-87](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/SourceInfo.scala#L83-L87)）。它靠 `Thread.currentThread().getStackTrace` 跳过所有 `chisel3.*` 内部帧、取第一帧用户代码来定位。这是**运行时**手段，只有在编译期宏抓不到位置时才用，下一节的主角是它的编译期表亲。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：摸清 `SourceInfo` 家族有几个成员、各自含义。
2. **步骤**：打开 `SourceInfo.scala`，画出 `SourceInfo` 的继承树（`sealed trait SourceInfo` → `NoSourceInfo` / `SourceLine`；`NoSourceInfo` → `UnlocatableSourceInfo`、`DeprecatedSourceInfo`）。
3. **观察**：`makeMessage` 在 `SourceLine` 与 `NoSourceInfo` 上分别返回什么。
4. **预期**：`SourceLine` 返回 `@[file:line:col]`；`NoSourceInfo` 系列返回空串 `""`。
5. **结论**：消费方可以用统一的 `makeMessage`，不必关心有没有位置——这正是 sealed 家族的价值。

#### 4.1.5 小练习与答案

**Q1**：为什么 `SourceInfo` 设计成 `sealed trait` 而不是普通 `trait`？
**答**：`sealed` 把所有实现收拢在同一文件，编译器能在模式匹配里检查穷尽性，避免漏处理「没有位置信息」的情况。

**Q2**：`DeprecatedSourceInfo` 与 `UnlocatableSourceInfo` 有何区别？
**答**：前者是「主动放弃」（函数已废弃，不值得花力气生成源信息）；后者是「被动放弃」（受 Scala 宏不支持命名/默认参数等技术限制，无法生成）。

---

### 4.2 `materialize` 隐式宏：编译期捕获位置（机制 A）

#### 4.2.1 概念说明

这是整套机制的**引擎**。问题陈述：我希望任何声明了 `(implicit sourceInfo: SourceInfo)` 的方法，在**用户不显式传参**时，编译器能自动变出一个**携带当前调用点行号**的 `SourceLine`。

Scala 的隐式查找规则里有一条：「找类型 `T` 的隐式值时，`T` 的伴生对象里的隐式成员也是候选」。Chisel 把一个**隐式宏** `materialize` 放进了 `object SourceInfo`：

```scala
object SourceInfo extends SourceInfoIntf          // SourceInfoIntf 带来 materialize
trait SourceInfoIntf { self: SourceInfo.type =>
  implicit def materialize: SourceInfo = macro SourceInfoMacro.generate_source_info
}
```

于是，凡是要隐式 `SourceInfo` 的地方，若作用域里没有更近的候选，编译器就会触发 `materialize`——而它本身是个宏，能在编译期读到「这次隐式查找发生在源码的哪一行」，直接把行号写成字面量。

#### 4.2.2 核心流程

```text
用户写 RegNext(in)
   │  方法签名: apply(next)(implicit sourceInfo: SourceInfo)
   │  作用域里没有显式 SourceInfo
   ▼
Scala 隐式查找: 需要 SourceInfo
   │  候选 = object SourceInfo 的 implicit 成员（伴生对象规则）
   ▼
命中 materialize（它是个 macro）
   │  宏在【编译期】执行 SourceInfoMacro.generate_source_info
   ▼
读 c.enclosingPosition → (file, line, col)
   │
   ▼
产出 AST 字面量: SourceLine("MyModule.scala", 12, 16)
   │  行号 12 已被"焊死"进字节码，运行时不再计算
   ▼
作为 sourceInfo 参数传入 RegNext.apply
```

#### 4.2.3 源码精读

先看声明——`SourceInfoIntf` 把隐式宏挂到 `SourceInfo` 伴生对象上：

[SourceInfoIntf.scala:26-28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/experimental/SourceInfoIntf.scala#L26-L28) —— `implicit def materialize: SourceInfo = macro SourceInfoMacro.generate_source_info`。`self: SourceInfo.type =>` 这个自类型注解，确保该 trait 只能被 `object SourceInfo` 混入，从而让 `materialize` 成为伴生对象的成员。

再看真正的宏实现——这是「行号从哪里来」的最终答案：

[SourceInfoMacro.scala:14-28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/internal/SourceInfoMacro.scala#L14-L28) —— `generate_source_info`：取 `c.enclosingPosition`，经 `SourceInfoFileResolver.resolve` 把绝对路径裁成项目相对路径，再用准引用（quasiquote）`q"..."` 产出 `SourceLine(path, line, col)` 这棵 AST。

关键的三行：

```scala
val p = c.enclosingPosition                 // 编译期：当前调用点的 Position
val path = SourceInfoFileResolver.resolve(p.source.file.file.toPath)
q"_root_.chisel3.experimental.SourceLine($path, ${p.line}, ${p.column})"
```

`c.enclosingPosition` 是 Scala 宏 API 提供的「触发这次宏展开的源码位置」。注意它返回的 `line`/`column` 是**编译期常量**，被直接焊进生成的 AST——这就是「零运行时开销」的来源。

路径裁剪逻辑在：

[SourceInfoFileResolver.scala:23-29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/SourceInfoFileResolver.scala#L23-L29) —— 优先用 `-Dchisel.project.root`，否则用 `user.dir`，把绝对路径前缀剥掉，让报错信息显示成 `src/.../Foo.scala` 而不是一长串机器路径。注意文件头注释：这个文件被**软链接**进编译器插件，保证插件与 core 里的路径解析逻辑完全一致。

#### 4.2.4 代码实践（本讲主任务：RegNext 的隐式注入）

> 这是规格指定的实践任务，属于「源码阅读型实践」，目的是把 `RegNext(in)` 这一行背后发生的事讲清楚。

1. **目标**：说清「当你调用 `RegNext(in)` 时，隐式 `SourceInfo` 是如何被宏注入并提供行号给 Builder 的」。
2. **步骤**：
   - 打开 [Reg.scala:76-90](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L76-L90)，确认 `RegNext.apply` 的签名是 `def apply[T <: Data](next: T)(implicit sourceInfo: SourceInfo): T`——它**直接**声明隐式参数，**没有**用黑盒改写宏（对比 4.3 的 `+`）。
   - 回到 4.2.3 的 `materialize`/`generate_source_info`，确认隐式值从哪里来。
   - 跟进 `RegNext` 内部：它调用 `Reg(model)`、`reg := next`，这些都把同一个 `sourceInfo` 继续往下传，最终进入 `DefReg` / `Connect` 节点（见 [Builder.scala:895-903](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L895-L903) 的 `pushCommand`）。
3. **需要写出的说明**（参考答案，可直接对照）：

   > 当我在第 12 行写 `val r = RegNext(in)` 时，编译器看到 `RegNext.apply` 需要一个隐式 `SourceInfo`，而我的代码里没有显式提供。按 Scala 隐式查找规则，编译器去 `SourceInfo` 的**伴生对象**里找候选，命中 `SourceInfoIntf` 注入的 `implicit def materialize`。`materialize` 本身是个宏，于是编译器在**编译期**执行 `SourceInfoMacro.generate_source_info`：它读取 `c.enclosingPosition`，拿到 `(文件名, 12, 列号)`，用准引用生成 `SourceLine("MyModule.scala", 12, 16)` 这棵 AST——行号 `12` 被焊死进字节码。这个 `SourceLine` 就成了 `RegNext.apply` 的 `sourceInfo` 实参，随后随 `Reg(model)` / `reg := next` 流入 `DefReg`、`Connect` 等 IR 节点；日后 `Builder.error` 报错时（[Builder.scala:939-943](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L939-L943)），就能用这份 `sourceInfo` 指出「错在 MyModule.scala 第 12 行」。

4. **预期结果**：能复述「编译期 `enclosingPosition` → `SourceLine` 字面量 → 随隐式参数注入 → 写进 IR 节点 → 喂给错误报告」这条链，且指出 `RegNext` 走的是「普通隐式」而非「黑盒改写宏」。
5. **待本地验证**：若想肉眼看到行号，可在 `RegNext` 后面故意写一行会触发 elaboration 错误的代码（例如把两个 `Output` 端口用 `:=` 相连），运行 `ChiselStage.emitSystemVerilog`，观察抛出的错误信息是否带上你源码的文件名与行号。

#### 4.2.5 小练习与答案

**Q1**：如果把 `materialize` 从 `object SourceInfo` 里删掉，会发生什么？
**答**：所有 `(implicit sourceInfo: SourceInfo)` 的方法在没有显式 `SourceInfo` 时都会编译失败（`could not find implicit value of type SourceInfo`）。`materialize` 是全局兜底的隐式源。

**Q2**：`materialize` 是编译期宏，为什么说它「零运行时开销」？
**答**：它在编译期读 `enclosingPosition` 并把行号直接生成进 AST 字面量；运行时拿到的是一个已经构造好的 `SourceLine` 对象，不需要再读栈、不需要反射。

---

### 4.3 `SourceInfoTransform` 桥接宏：把公开 API 改写成 `do_`（机制 B）

#### 4.3.1 概念说明

机制 A（`materialize`）已经能让任何 `(implicit sourceInfo: SourceInfo)` 的方法拿到行号。那为什么还需要机制 B？

因为有一类公开 API **不能**直接写成带隐式参数的形式——**链式 apply**。考虑 `VecInit(1.U, 2.U)(idx)` 或 `Wire(UInt(8.W))(cond)`：如果你把 `apply` 声明成 `def apply(x)(implicit si: SourceInfo)`，编译器会把后面的 `(idx)` 当成「显式传入隐式参数」，从而报类型错误。换句话说，**隐式参数与链式 apply 二者只能选其一**。

Chisel 的解法是：公开方法**不**带 `SourceInfo` 参数，而是用黑盒宏在编译期把它**改写**成一个内部 `do_` 方法，由宏负责显式插入 `implicitly[SourceInfo]`：

```scala
// 公开方法：没有 SourceInfo 参数，支持链式 apply
def +(that: T): T = macro SourceInfoTransform.thatArg
// 宏把它改写成：
//   this.do_+(that)(implicitly[SourceInfo])
// 然后由机制 A 把 implicitly[SourceInfo] 解析成 SourceLine
def do_+(that: T)(implicit sourceInfo: SourceInfo): T
```

文件头那段大段注释把这套设计讲得很清楚：[SourceInfoDoc.scala:26-34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/SourceInfoDoc.scala#L26-L34)。

#### 4.3.2 核心流程

```text
用户写 a + b
   │  公开方法: def +(that) = macro SourceInfoTransform.thatArg
   ▼
编译期执行 thatArg 宏
   │  thisObj      = a
   │  doFuncTerm   = "do_" + "+" = do_+
   │  implicitSourceInfo = q"implicitly[SourceInfo]"
   ▼
改写产物: a.do_+(b)(implicitly[SourceInfo])
   │
   ├──► 解析 implicitly[SourceInfo]  →  触发机制 A  →  SourceLine(file,line,col)
   ▼
调用 a.do_+(b)(sourceInfo)
   │  do_+ → _impl_+ → binop → pushOp(DefPrim(sourceInfo, ...))
   ▼
DefPrim 节点带上调用点行号
```

#### 4.3.3 源码精读

公共基石是 `SourceInfoTransformMacro` trait，它定义了两个在所有改写里反复用到的片段：

[SourceInfoTransform.scala:21-26](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala#L21-L26) —— `thisObj`（即 `c.prefix.tree`，方法调用者，如 `a`）与 `implicitSourceInfo`（一棵 `implicitly[SourceInfo]` 的 AST）。

最典型的「单参数」改写 `thatArg`，只有一行：

[SourceInfoTransform.scala:201-203](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala#L201-L203) —— `def thatArg(that) = q"$thisObj.$doFuncTerm($that)($implicitSourceInfo)"`，把 `a.+(b)` 改写成 `a.do_+(b)(implicitly[SourceInfo])`。

那么 `doFuncTerm`（即 `do_+`）从哪来？答案在 `AutoSourceTransform`：

[SourceInfoTransform.scala:166-185](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala#L166-L185) —— `doFuncTerm` 用模式匹配从 `c.macroApplication` 里抠出被调用方法的名字，再前缀 `do_`。这是「自动」命名机制：不必为每个运算符手写 `do_+`/`do_-`，宏自己根据被调方法名合成 `do_<原名>`。

接入点在 `NumIntf`——`+`/`*` 等运算符的公开声明：

[NumIntf.scala:24-27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/NumIntf.scala#L24-L27) —— `final def +(that: T): T = macro SourceInfoTransform.thatArg` 紧跟着抽象的 `def do_+(that: T)(implicit sourceInfo: SourceInfo): T`。`final` 是为了禁止子类覆盖公开方法（否则宏改写可能被绕过）。

`do_+` 的实现在 `BitsIntf`（桥接到 `_impl_+`），最终走到 `binop`，把 `sourceInfo` 焊进 `DefPrim`：

[Bits.scala:161-164](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L161-L164) —— `binop` 里 `pushOp(DefPrim(sourceInfo, dest, op, this.ref, other.ref))`。注意同函数里 `implicit val info: SourceInfo = sourceInfo` 这一行（[Bits.scala:162](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L162)）——它把显式参数重新提升为隐式值，让 `pushOp` 内部任何调用 `Builder.error` 的地方都能拿到行号。

> 这个文件里还有针对 `Wire`/`VecInit`/`Mem`/`Mux`/`Probe` 等的**专用**改写宏（如 [SourceInfoTransform.scala:84-103](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala#L84-L103) 的 `MemTransform`/`MuxTransform`），原理都一样：公开方法不带 `SourceInfo`，宏改写成 `do_apply(...)(implicitly[SourceInfo])`。它们之所以「专用」而非复用 `AutoSourceTransform`，是因为参数结构复杂（柯里化、类型参数）或需要返回不同形状的 `do_` 方法。

#### 4.3.4 代码实践（源码阅读 + 对照型）

1. **目标**：对比「黑盒改写宏」与「普通隐式」两种注入方式的公开签名差异。
2. **步骤**：
   - 打开 [NumIntf.scala:24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/NumIntf.scala#L24)（`+` 用 `macro SourceInfoTransform.thatArg`）。
   - 打开 [Reg.scala:79](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L79)（`RegNext.apply(next)(implicit sourceInfo)`，**不**用宏）。
   - 在 `SourceInfoTransform.scala` 里找到 `thatArg`（[L201-203](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala#L201-L203)）与 `doFuncTerm`（[L175-184](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala#L175-L184)），手写一次「`a + b` 改写成什么」。
3. **观察**：`+` 的公开方法体是 `= macro ...`（没有方法体），而 `RegNext.apply` 有正常方法体。
4. **预期**：`a + b` → `a.do_+(b)(implicitly[SourceInfo])`；`RegNext(in)` → 直接按隐式参数解析，没有 `do_` 中间层。
5. **结论**：能区分两类——「带链式 apply / 运算符」的用黑盒宏改写；「普通工厂方法」用普通隐式参数即可。

#### 4.3.5 小练习与答案

**Q1**：为什么 `+` 不能直接声明成 `def +(that: T)(implicit si: SourceInfo): T`？
**答**：这样会把 `a + b (c)` 这类链式 apply 的 `(c)` 误当成显式隐式实参，触发类型错误；黑盒宏改写让公开方法不带 `SourceInfo`，从而兼容链式调用。

**Q2**：`thatArg` 改写出的 `doFuncTerm` 是怎么得到 `do_+` 的？
**答**：`AutoSourceTransform.doFuncTerm` 用 `c.macroApplication match { case q"$_.$funcName[..$_](...$_)" => funcName }` 抠出方法名 `+`，再 `TermName("do_" + funcName)`。

---

### 4.4 两套机制的协作：完整链路

读完前两节，你会发现机制 A 与 B **不是二选一，而是协作**。下面把两条最常走的路径画清楚，作为本讲的总收束。

**路径一：`a + b`（黑盒宏改写 + 隐式宏）**

```text
a + b
 │  公开方法 = macro SourceInfoTransform.thatArg          (机制 B)
 ▼
a.do_+(b)(implicitly[SourceInfo])
 │  implicitly[SourceInfo] 触发隐式查找                    (机制 A)
 ▼
a.do_+(b)(SourceLine("Foo.scala", 7, 10))
 │  do_+ → _impl_+ → binop                                (见 Bits.scala:161-164)
 ▼
pushOp(DefPrim(SourceLine(...), dest, AddOp, a.ref, b.ref))
 │                                                         (见 Builder.scala:899-903)
 ▼
DefPrim 节点进入当前模块的命令队列，带上了第 7 行
```

**路径二：`val r = RegNext(in)`（仅隐式宏，不走黑盒改写）**

```text
RegNext(in)
 │  apply(next)(implicit sourceInfo: SourceInfo)          (Reg.scala:79)
 │  作用域无显式 SourceInfo → 伴生对象 materialize        (机制 A)
 ▼
RegNext.apply(in)(SourceLine("Foo.scala", 12, 16))
 │  内部: Reg(model) 与 reg := next 复用同一 sourceInfo
 ▼
DefReg(SourceLine(...), r, clock)  +  Connect(SourceLine(...), r, in)
```

为什么 `RegNext` 选「普通隐式」而不是「黑盒改写宏」？因为 `RegNext`/`Reg` 支持**命名/默认参数**（如 `RegNext(next, init = 0.U)`、带默认 `clock`），而 Scala 宏对命名/默认参数的支持很差——这正是 `UnlocatableSourceInfo` 注释里那条「technical limitation」（[SourceInfo.scala:25-27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/SourceInfo.scala#L25-L27)）的由来。普通隐式参数没有这个限制，所以这类工厂方法绕开了黑盒改写，直接吃 `materialize` 提供的 `SourceLine`。

> 注意：`UnlocatableSourceInfo` 这个「无法定位」的兜底，更多用于 `Reg` 等个别极端情况；`RegNext` 因为走普通隐式，**通常能**拿到正确的 `SourceLine`，并不会退化成 `UnlocatableSourceInfo`。

## 5. 综合实践

**任务：用一次故意的 elaboration 错误，肉眼验证「源信息真的被焊进了 IR」。**

1. 写一个最小模块，故意制造一处方向冲突（两个 `Output` 端口用 `:=` 互连），并把出错行单独写在已知行号上：

   ```scala
   // 示例代码（非项目原有代码）
   import chisel3._
   class Bad extends Module {
     val a = IO(Output(UInt(8.W)))
     val b = IO(Output(UInt(8.W)))
     b := a          // 故意：两个 output 相连，触发 MonoConnect 方向错误
   }
   ```

2. 用 `ChiselStage.emitSystemVerilog(new Bad)` 触发 elaboration。
3. **观察**错误信息是否包含源文件名与「`b := a`」所在行号。
4. **解释**（用本讲学到的链路）：`b := a` 内部调用 `connect`，它的 `sourceInfo` 由 `materialize` 宏在编译期捕获为本文件本行；该 `sourceInfo` 随 `Builder.error(m)(sourceInfo)`（[Builder.scala:939-943](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L939-L943)）拼进错误信息。
5. **延伸思考**：如果把出错行挪到另一个文件、改到别的行号，错误信息里的行号是否随之变化？为什么？（提示：行号在编译期由 `enclosingPosition` 决定，与运行时无关。）
6. **待本地验证**：不同 Chisel 版本对错误信息里源信息格式的渲染可能略有差异；若行号未如预期显示，请先确认编译时确实加载了 `chisel-plugin`（见 u7-l1）以及未启用 `--no-check-comb-loops` 之类影响报错路径的选项。

## 6. 本讲小结

- `SourceInfo` 是「这条硬件来自源码哪里」的元数据，`sealed` 家族含 `SourceLine`（带位置）、`UnlocatableSourceInfo`（技术限制兜底）、`DeprecatedSourceInfo`（废弃 API 兜底）。
- **机制 A**：`object SourceInfo` 经 `SourceInfoIntf` 混入 `implicit def materialize`（隐式宏），实现是 `SourceInfoMacro.generate_source_info`——在**编译期**读 `c.enclosingPosition`，把行号焊成 `SourceLine` 字面量，运行时零开销。
- **机制 B**：`SourceInfoTransform` 一整套**黑盒改写宏**，把不带 `SourceInfo` 的公开方法（如 `+`、`Wire`、`VecInit`、`Mem`、`Mux`）改写成内部 `do_` 方法，并插入 `implicitly[SourceInfo]`，从而兼容链式 apply。
- 两套机制**协作**：机制 B 改写出 `implicitly[SourceInfo]` 后，由机制 A 解析成 `SourceLine`。
- `RegNext`/`Reg` 因支持命名/默认参数（Scala 宏的盲区），走「普通隐式参数」而非黑盒改写，但仍靠机制 A 拿到行号。
- 源信息最终随 `pushOp(DefPrim(sourceInfo, ...))` / `pushCommand` 写进 IR 节点，并在 `Builder.error(m)(sourceInfo)` 处用于错误定位——这就是 Chisel 报错能指回你源码行号的根本原因。
- 与 u7-l1 的关系：编译器插件负责「**命名**」（Bundle 字段名、标识符），本讲的宏负责「**定位**」（文件名+行号）；二者都是编译期设施，但各管一摊、互不替代。

## 7. 下一步学习建议

- **顺着源信息的消费端走**：阅读 [Builder.scala:899-903](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L899-L903) 的 `pushOp` 与 [Builder.scala:939-943](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L939-L943) 的 `error`，看 `sourceInfo` 如何从 IR 节点流到错误报告。
- **进入 u7-l3 命名机制**：本讲解决了「定位」，下一讲《Namer 与 Identifier 命名》解决「信号叫什么名字」，二者合起来就是「报错信息里 `Foo.scala:12` 的 `_T_3` 是谁、来自哪」的完整答案。
- **想看 Scala 3 版本**：对照 `core/src/main/scala-3/chisel3/experimental/SourceInfoIntf.scala` 的 `implicit inline def materialize: SourceInfo = ${ SourceInfoMacro.generate_source_info }`，体会 quote/splice 宏与 Scala 2 黑盒宏在「同一种语义、两套语法」上的映射。
- **可选深入**：阅读 `macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala` 的全部专用改写宏（`VecTransform`、`MemTransform`、`MuxTransform`、`IntLiteralApplyTransform` 等），体会「同一套 `implicitly[SourceInfo]` 注入模式」如何适配千差万别的公开 API 形状。
