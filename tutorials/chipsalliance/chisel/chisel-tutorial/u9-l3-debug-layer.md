# Debug 层与调试信息

## 1. 本讲目标

Chisel 里和「debug（调试）」相关的机制有**两套，彼此独立、只是恰好同名**。本讲要把它们彻底分清，并各自讲透。读完本讲你应该能够：

1. 说清楚 **Debug 层**（`chisel3.layers.Verification.Debug`）是什么，以及为什么 `printf` 会被自动放进去。
2. 跟踪一条「用户写 `printf(...)` → 命令被塞进 Debug 层块」的完整源码路径。
3. 理解 **调试元数据**（`chisel3.debug` 包）如何用 Scala 反射把模块的构造器参数抽成 JSON，并发射成 `circt_debug_*` 内建函数（intrinsic）。
4. 掌握 `EmitDebugIntrinsicsAnnotation` 开关、`AddDebugIntrinsics` 阶段、`SuppressDebugParams` 抑制标记三者的协作关系。
5. 自己动手生成 Verilog / CHIRRTL，亲眼观察「调试代码被隔离」与「调试元数据被附上」两种现象。

## 2. 前置知识

本讲假设你已经学过：

- **u4-l2 命令记录与内部 FIRRTL IR**：知道 elaboration 期间每条 Chisel API 调用都被 `Builder.pushCommand` 记录成 `Command`，内部 IR 是 `Circuit ⊃ Component ⊃ Command` 三层树；`DefPrim`、`Connect`、`When` 都是命令。
- **u8-l3 Layers 与 Probe**：知道 **Layer（层）** 是「编译期可选层」——层块（`layer.block`）里的代码只在 Verilog 编译期被启用时才存在，仿真可开、交付可删；Extract 型层会落地成独立 SystemVerilog 模块 + `bind`，靠 include 文件启用；内建有 `Verification` 层及其子层 `Assert`/`Assume`/`Cover`。
- **u5-l1 Stage / Phase 管道**：知道编译被拆成若干 `Phase`，靠 `AnnotationSeq`（注解）传递数据，`PhaseManager` 按依赖自动排序。

几个本讲会用到的术语：

- **intrinsic（内建函数）**：FIRRTL/CIRCT 里以 `intrinsic(name<...> ...)` 形式出现、由特定编译 pass 解释的「扩展节点」。Chisel 内部 IR 里用 `DefIntrinsic` 命令表示。
- **反射（reflection）**：在运行期（这里是 elaboration 期）通过 `java.lang.reflect` 读取一个对象的类、字段、方法的能力。`DebugMeta` 用它来「问」一个模块对象它的构造器参数是什么。
- **secret command（秘密命令）**：模块命令块里一条由编译器注入、与用户命令分账管理的命令通道。它**仍然会被序列化进 CHIRRTL**，只是不和用户写的命令混在一起。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/layers/Layers.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/Layers.scala) | 定义内建 `Verification` 层及其子层，含本讲主角之一 `Verification.Debug`。 |
| [core/src/main/scala/chisel3/SimLog.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SimLog.scala) | `printf` 的真正实现，把 `Printf` 命令塞进 `Verification.Debug` 层块。 |
| [core/src/main/scala/chisel3/Printf.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printf.scala) | 用户调用的 `printf` 入口，转发到 `SimLog.StdErr`。 |
| [core/src/main/scala/chisel3/Layer.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala) | `layer.block` 的实现，含两个 `skipIf*` 开关。 |
| [core/src/main/scala/chisel3/debug/DebugMeta.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMeta.scala) | 用反射抽取模块/信号的**构造器参数**并序列化成 JSON（`ClassParam`、`CtorParamExtractor`）。 |
| [core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala) | 遍历内部 IR，为模块/端口/信号/内存/枚举注入 `circt_debug_*` 内建函数（`DebugIntrinsics`）。 |
| [core/src/main/scala/chisel3/debug/EmitDebugIntrinsicsAnnotation.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/EmitDebugIntrinsicsAnnotation.scala) | 控制是否发射调试内建函数的 opt-in 注解（`--with-experimental-debug-intrinsics`）。 |
| [core/src/main/scala/chisel3/debug/SuppressDebugParams.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/SuppressDebugParams.scala) | marker trait：标记「构造器参数是结构性的」，跳过重复的 `params=` 抽取。 |
| [src/main/scala/chisel3/stage/phases/AddDebugIntrinsics.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddDebugIntrinsics.scala) | Phase 管道里的「注入调试内建函数」阶段。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 内部 IR：`DefIntrinsic` 命令、`addSecretCommand` 秘密命令通道。 |

---

## 4. 核心概念与源码讲解

先给一张「两种 debug」的全景图，避免后面把两者搅在一起：

| | (A) Debug 层 | (B) 调试元数据 |
| --- | --- | --- |
| 全名 | `chisel3.layers.Verification.Debug` | `chisel3.debug` 包 |
| 解决什么 | 把 `printf`/`flush` 等**调试代码**在 Verilog 编译期隔离进可选层 | 把 Chisel **类型/参数信息**以机器可读形式附给下游工具 |
| 形态 | 一个内建 Layer（Extract） | 一组 `circt_debug_*` 内建函数（`DefIntrinsic`） |
| 是否默认开 | 默认就生效（printf 自动入层） | 默认关，需 `--with-experimental-debug-intrinsics` |
| 何时引入 | 2026-06 新增（commit `cc46629d01`「Add Debug Layer」） | 更早，分三个 commit 引入（`d8ef710981` 等） |

记住一句话：**(A) 是「隔离调试代码」，(B) 是「附上调试信息」**。下面四节分别拆解。

### 4.1 Debug 层：printf 的自动隔离

#### 4.1.1 概念说明

回顾 u8-l3：`Verification` 是一个内建的 Extract 型根层，下面挂着 `Assert`、`Assume`、`Cover`，分别自动收容断言、假设、覆盖。本讲的主角 **`Verification.Debug`** 是它的第四个子层，专门用来收容「调试用的打印」。

它为什么被新增？直接看提交说明（commit `cc46629d01`）最清楚：

> Add a new `Verification.Debug` layer which is intended to store all "debugging" collateral. Automatically put `printf`s into this layer. Previously, `printf`s were put into the `Verification` layer. Splitting `printf`s out is motivated by users wanting to have the ability to synthesize asserts, but _not_ synthesize prints.

翻译过来就是：**用户想要「断言可综合、打印不可综合」**。但旧设计里 `printf` 与 `Assert` 都挂在 `Verification` 层下，而 `Verification.Assert` 依赖 `Verification`，于是无法「只要 assert、不要 printf」。把 `printf` 单独拆进新的 `Verification.Debug` 层后，二者解耦——交付时可以只编译进 assert 那一层，扔掉 debug 那一层。

要点：

- Debug 层是 **Extract 型**：会落地成独立 SystemVerilog 文件（`verification/debug` 目录）+ `bind`，靠 include 文件启用，与主设计物理分离。
- 它**没有** `Temporal` 子层（不像 Assert/Assume/Cover）——打印不需要复杂时序断言的那种变体。
- `printf` **默认**就进 Debug 层，用户无需手写 `layer.block`。

#### 4.1.2 核心流程

当你写下 `printf("count = %d\n", count)` 时，命令是这样被「塞进层」的：

```
printf(...)                      // 用户代码
  └─> chisel3.printf.apply       // Printf.scala，顶层入口
        └─> SimLog.StdErr.printf // 转发到默认 SimLog（stderr）
              └─> printfWithReset        // 包一层 !reset 守卫
                    └─> printfWithoutReset
                          └─> layer.block(layers.Verification.Debug,
                                          skipIfAlreadyInBlock = true,
                                          skipIfLayersEnabled = true) {
                               Builder.pushCommand(Printf(...))   // Printf 命令进层块
                             }
```

关键在于最后一步：`Printf` 命令（u4-l2 讲过的 `Command`）不是直接进当前模块的普通命令队列，而是被包进一个 `Verification.Debug` 的**层块（LayerBlock）**。序列化成 CHIRRTL 时，这条 `printf` 会出现在 `layer Verification.Debug` 块下面；交给 CIRCT 后，它被编译到独立的 `verification/debug` 输出文件。

`layer.block` 这两个布尔开关决定「何时不再额外包层」（见 [Layer.scala:343-357](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L343-L357)）：

- `skipIfAlreadyInBlock = true`：如果当前已经身处某个层块内，就**不再新建** Debug 层块——直接把 `Printf` 内联到外层。
- `skipIfLayersEnabled = true`：如果当前模块已经**启用了任何层**，也不再新建层块。

这两个开关合起来表达：「如果用户已经手动用 `layer.block` 把代码圈进某个层（比如自定义的 `Trace` 层），或已经启用了层，那 `printf` 就别再自作主张包一层 Debug。」这正是官方文档示例里「把 `printf` 放进用户自定义 `Trace` 层」能覆盖默认行为的原理。

#### 4.1.3 源码精读

**① Debug 层的定义** —— [core/src/main/scala/chisel3/layers/Layers.scala:62-71](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/Layers.scala#L62-L71)

```scala
object Debug
    extends Layer(LayerConfig.Extract(CustomOutputDir(Paths.get("verification", "debug"))))(
      _parent = implicitly[Layer],
      _sourceInfo = UnlocatableSourceInfo
    )
```

它声明在 `object Verification` 内部，故全名 `chisel3.layers.Verification.Debug`；`Extract` + `CustomOutputDir("verification","debug")` 决定它落地成 `verification/debug` 目录下的独立文件。它是 `Verification` 的直接子层，被登记进默认层清单（见同 PR 对 `layers/package.scala` 的改动）。

**② printf 的入口转发** —— [core/src/main/scala/chisel3/Printf.scala:30-31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printf.scala#L30-L31)

```scala
def apply(pable: Printable)(implicit sourceInfo: SourceInfo): chisel3.printf.Printf =
  SimLog.StdErr.printf(pable)(sourceInfo)
```

用户写的 `printf(...)` 经宏包成 `Printable`，最终调到默认的 `SimLog.StdErr`。

**③ 把 Printf 命令塞进 Debug 层块** —— [core/src/main/scala/chisel3/SimLog.scala:73-88](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SimLog.scala#L73-L88)

```scala
private[chisel3] def printfWithoutReset(pable: Printable)(...) = {
  val clock = Builder.forcedClock
  val printfId = new chisel3.printf.Printf(pable)
  ...
  layer.block(layers.Verification.Debug, skipIfAlreadyInBlock = true, skipIfLayersEnabled = true) {
    Builder.pushCommand(Printf(printfId, sourceInfo, _filename, clock.ref, pable))
  }
  printfId
}
```

`flush`（刷缓冲）走完全相同的层路径，见 [SimLog.scala:52](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SimLog.scala#L52)。注意外层还有 `when(!Module.reset.asBool)` 守卫，故复位期间不打印。

> 对照旧版本：本提交（`cc46629d01`）就是把这两处的 `layers.Verification` 改成了 `layers.Verification.Debug`——这就是「printf 被放入 Debug 层」的那一处代码改动，也是本讲实践任务要定位的目标。

**④ 层块的两个开关判定** —— [core/src/main/scala/chisel3/Layer.scala:353-354](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L353-L354)

```scala
if (
  skipIfAlreadyInBlock && Builder.layerStack.size > 1 ||
  skipIfLayersEnabled  && Builder.enabledLayers.nonEmpty  ||
  Builder.elideLayerBlocks
) return tc.identity(thunk)
```

三个条件任一成立就「不建层、直接内联执行 thunk」，把 `Printf` 命令留在当前普通命令流里。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `printf` 被隔离进 `Verification.Debug` 层，并定位「printf 入层」的源码路径。

**操作步骤**（这是本讲主实践，对应规格里的实践任务）：

1. 写一个最小模块，内含一条 `printf`：

   ```scala
   // 示例代码
   import chisel3._

   class DebugPrinter(val w: Int) extends Module {
     val in  = IO(Input(UInt(w.W)))
     val out = IO(Output(UInt(w.W)))
     out := in + 1.U
     printf("in=%d out=%d\n", in, out)   // 重点观察这一行去了哪里
   }
   ```

2. 用 `ChiselStage` 生成 CHIRRTL 文本（CHIRRTL 能直接看到层结构）：

   ```scala
   // 示例代码
   val chirrtl = circt.stage.ChiselStage.emitCHIRRTL(new DebugPrinter(8))
   println(chirrtl)
   ```

3. 定位源码路径：运行 `git log --oneline | grep "Add Debug Layer"` 找到提交 `cc46629d01`，再 `git show cc46629d01 -- core/src/main/scala/chisel3/SimLog.scala`，确认 `layer.block(layers.Verification, ...)` 被改成了 `layer.block(layers.Verification.Debug, ...)`。

**需要观察的现象**：

- CHIRRTL 里会出现 `layer ... Debug, bind, "verification${sep}debug"` 的层声明（`${sep}` 是路径分隔符）。
- `printf(p"""...""", ...)` 这一行会**缩进**在 `layer Verification.Debug :` 块**内部**，而不是平铺在模块体顶层。

**预期结果**（待本地验证）：在 `emitSystemVerilog`（`--split-verilog`）下，该 `printf` 会被编译进 `verification/debug/` 目录下的独立 `.sv` 文件，并通过 `bind` 挂回主模块；若不启用该层，主设计里看不到任何打印痕迹。

> 提示：若想验证 `skipIfAlreadyInBlock` 的效果，可把 `printf` 包进一个用户自定义的 `Trace` 内联层（仿照官方 `docs/src/explanations/layers.md` 的 Design Verification 示例），再对比 CHIRRTL——此时 `printf` 不再额外产生 `Verification.Debug` 块。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能简单地用 `` `ifndef SYNTHESIS `` 宏包裹 `printf` 来实现「不综合打印」？为什么 Chisel 选择用独立层？

**参考答案**：宏包裹依赖仿真器/综合器对 `` `ifndef SYNTHESIS `` 的一致解释，不同工具行为不一，且宏在预处理期生效、粒度粗。层是 FIRRTL/CIRCT 编译期的一等机制，能精确地把一整段代码连同它依赖的辅助逻辑物理剥离到独立模块，靠 include 文件按需启用，工具无关、语义明确（见提交说明里引用的 CIRCT PR #10526）。

**练习 2**：`Verification.Debug` 有没有 `Temporal` 子层？为什么？

**参考答案**：没有。`Temporal` 子层（`HasTemporalInlineLayer`）是为「复杂时序断言、部分仿真器不支持」而设的变体，`Assert/Assume/Cover` 才需要；`printf` 是普通组合打印，不需要这种变体，故 `Debug` 不混入该 trait（见 [Layers.scala:62-65](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/Layers.scala#L62-L65) 的注释）。

---

### 4.2 DebugMeta：构造器参数的反射模型

#### 4.2.1 概念说明

现在转向第二套机制——`chisel3.debug` 包。它的目标：在 elaboration 之后，给每个模块、信号附上一份**机器可读的类型说明**，让下游工具（CIRCT 的 UHDM/FIRRTL 调试信息等）能回答「这个模块是用什么参数例化的？这个信号的 Chisel 类型叫什么？」。

本节讲其中第一块拼图：`DebugMeta.scala`。注意一个易混点：**文件名叫 `DebugMeta`，但里面真正的主角是 `CtorParamExtractor`（构造器参数抽取器）和 `ClassParam`**。它解决一个非常具体的问题：

```scala
class Foo(val width: Int, val hasReset: Boolean) extends Module { ... }
```

当 Chisel 生成这个模块时，下游工具怎么知道它是 `width=8, hasReset=true` 例化的？`CtorParamExtractor` 用 **Java 反射**「问」这个模块对象的类：你的主构造器有哪些参数？每个参数的值是多少？再把结果序列化成 JSON。

为什么用反射而不是要求用户手写？因为模块参数是普通 Scala 构造器参数，Chisel 运行时并不「认识」它们——只能靠反射在运行期动态打探。

#### 4.2.2 核心流程

抽取一个对象构造器参数的流程：

```
getCtorParams(target)                  // 公共入口
  ├─ visited.clear()；visited += target   // 初始化防环表
  └─ getCtorParamsImpl(target, depth=0)
       └─ descriptor(target)            // 查/建 ClassDescriptor
            ├─ CtorParamsPlatform.ctorParams(target)  // 平台相关：列主构造器参数 (名, 类型名)
            └─ 对每个参数名 getDeclaredMethod → accessor 方法表
       └─ 对每个参数：
            paramValue → method.invoke(obj) → renderValue
                                                ├─ Data      → 类型名字符串（不下钻成硬件）
                                                ├─ Boolean   → JSON Bool
                                                ├─ 数值       → 字符串
                                                └─ 其它对象   → 递归 getCtorParamsImpl（受深度限制）
```

几个保护机制保证抽取「安全且有界」：

- **深度限制** `MaxParamDepth = 8`：嵌套对象最多下钻 8 层。
- **长度限制** `MaxRenderedLen = 256`：单个值超长就截断加 `...[truncated]`。
- **防环** `visited`（identity 比较 `eq`）：遇到已访问对象不再下钻，避免循环引用。
- **不进标准库** `isOpaqueStdlibClass`：`java.*`/`scala.*` 的对象直接 `toString`，不下钻。

结果是一个 `Seq[ClassParam]`，每个 `ClassParam(name, typeName, value: Option[ujson.Value])`，最后会被 `upickle` 序列化成 JSON 字符串，塞进内建函数的 `params=` 属性（下一节）。

#### 4.2.3 源码精读

**① 数据模型与 JSON 序列化** —— [core/src/main/scala/chisel3/debug/DebugMeta.scala:16-33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMeta.scala#L16-L33)

```scala
private[debug] case class ClassParam(
  name:     String,
  typeName: String,
  value:    Option[ujson.Value] = None
)
```

注意 `value` 是 `Option`：有些参数反射不到值（比如私有且无 accessor），就留 `None`。upickle 默认把 `Option` 序列化成数组，这里特意改写成「值或 null」以贴近 JSON 习惯。

**② 把 Data 翻译成「类型名」** —— [core/src/main/scala/chisel3/debug/DebugMeta.scala:40-47](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMeta.scala#L40-L47)

```scala
private[debug] def dataToTypeName(data: Data): String = sanitize(data match {
  case t: Record => ... s"...[${t.className}]" ...
  case t => t.toString.split(" ").last
})
```

`Record`（Bundle 之父）带绑定信息，其它类型取 `toString` 的最后一段。`sanitize` 抹掉控制字符与引号，保证产出能安全塞进 JSON 字符串。

**③ 入口与防环初始化** —— [core/src/main/scala/chisel3/debug/DebugMeta.scala:92-96](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMeta.scala#L92-L96)

```scala
private[debug] def getCtorParams(target: Any): Seq[ClassParam] = {
  visited.clear()
  target match { case ref: AnyRef => visited += ref; case _ => }
  getCtorParamsImpl(target, 0)
}
```

每次入口重置 `visited`——安全的前提是 elaboration 单线程（注释明说）。

**④ 用反射建「类描述符」** —— [core/src/main/scala/chisel3/debug/DebugMeta.scala:74-90](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMeta.scala#L74-L90)

```scala
private def buildDescriptor(target: Any, cls: Class[_]): ClassDescriptor = {
  val params = try CtorParamsPlatform.ctorParams(target) catch { case NonFatal(e) => Seq.empty }
  val accessors = params.iterator.flatMap { case (name, _) =>
    try {
      val m = cls.getDeclaredMethod(name); m.setAccessible(true); Some(name -> m)
    } catch { case NonFatal(_) => None }
  }.toMap
  ClassDescriptor(params, accessors)
}
```

`CtorParamsPlatform.ctorParams` 是平台相关实现（Scala 2 与 Scala 3 不同）列主构造器参数；拿到参数名后用 `getDeclaredMethod` 找同名 getter、`setAccessible(true)` 强制可访问。整段用 `try/catch NonFatal` 包裹——反射失败只是「拿不到这个参数」，绝不让调试元数据功能拖垮整个编译。

**⑤ 把值渲染成 JSON** —— [core/src/main/scala/chisel3/debug/DebugMeta.scala:119-143](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMeta.scala#L119-L143)

`renderValue` 按 Scala 类型分流：含 `Data` 的 `Seq` 列出元素类型、单个 `Data` 出类型名、`Boolean` 出 JSON bool、数值出字符串、`AnyRef` 在深度未超且未访问过且非标准库类时**递归**下钻。这套规则保证产出既有用又不会无限膨胀。

#### 4.2.4 代码实践

**实践目标**：看到带构造器参数的模块，其参数被抽成 JSON 附到 `circt_debug_moduleinfo` 上。

**操作步骤**：

1. 写一个带构造器参数的模块（参数要能被反射到——用 `val`）：

   ```scala
   // 示例代码
   class DebugParamModule(val width: Int, val hasReset: Boolean) extends Module {
     override def desiredName = s"DebugParamModule_w${width}_r$hasReset"
     val in  = IO(Input(UInt(width.W)))
     val out = IO(Output(UInt(width.W)))
     if (hasReset) { val reg = RegInit(0.U(width.W)); reg := in; out := reg }
     else out := in
   }
   ```

2. 加上开关生成 CHIRRTL：

   ```scala
   // 示例代码
   val chirrtl = circt.stage.ChiselStage.emitCHIRRTL(
     new DebugParamModule(5, true),
     args = Array("--with-experimental-debug-intrinsics"))
   println(chirrtl)
   ```

**需要观察的现象**：找到形如 `intrinsic(circt_debug_moduleinfo<...>)` 的行，其 `params=` 属性里有一段 JSON，含 `"name":"width","typeName":"Int","value":"5"` 与 `"name":"hasReset","typeName":"Boolean","value":true`。

**预期结果**（与仓库测试 `DebugIntrinsicsSpec` 的「emits moduleinfo with constructor params serialized in params field」一致）：数值参数序列化成 JSON 字符串，`Boolean` 保持原生 JSON bool。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `width: Int` 的值 `5` 在 JSON 里是字符串 `"5"` 而不是数字 `5`？

**参考答案**：见 `renderValue`（[DebugMeta.scala:124](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMeta.scala#L124)）——所有 `Byte/Short/Int/Long/Float/Double` 统一走 `ujson.Str(v.toString)`。这是一种保守策略：硬件位宽可能很大（超过 JSON 数字安全整数范围），统一用字符串避免精度/溢出问题；`Boolean` 是唯一保留原生类型的例外。

**练习 2**：如果一个模块的某个构造器参数是私有且没有同名 getter，`params=` 里会怎样？

**参考答案**：`buildDescriptor` 里 `getDeclaredMethod(name)` 找不到 accessor 就跳过该参数（返回 `None`），`paramValue` 进而返回 `None`，对应 `ClassParam.value = None`，序列化为 JSON `null`。不会报错。

---

### 4.3 DebugMetaEmitter：circt_debug 内建函数的发射

#### 4.3.1 概念说明

`DebugMeta.scala` 负责「抽参数」，`DebugMetaEmitter.scala` 负责「把信息发出去」。它的对外入口是 `private[chisel3] object DebugIntrinsics`，核心方法 `generate(circuit: Circuit)` 遍历整棵内部 IR（u4-l2 讲过的 `Circuit ⊃ Component ⊃ Command`），给每样值得记录的东西注入一条 `DefIntrinsic` 命令——也就是一个 `circt_debug_*` 内建函数。

它发射的内建函数一共四种半：

| 内建函数 | 作用于 | 携带的关键属性 |
| --- | --- | --- |
| `circt_debug_moduleinfo` | 每个**模块**（含 `DefClass`） | `typeName`、`params`（构造器参数 JSON） |
| `circt_debug_var` | **根信号**（端口顶层、Wire、Reg、Prim 结果…） | `typeName`、`name`、`params`、可选 `enumTypeName` |
| `circt_debug_subfield` | 聚合类型（Bundle/Vec）的**叶子字段** | `typeName`、`name`、`parent`（指向根 var 的全限定名） |
| `circt_debug_enumdef` | 每个**枚举类型**（仅一次） | `typeName`、`fqn`、`variants`、`width` |
| `circt_debug_var`（内存变体） | `Mem`/`SyncReadMem`/`FirrtlMemory` | `typeName`（含元素类型与深度）、`name` |

一个关键设计：**注入的内建函数走「秘密命令」通道**（`block.addSecretCommand`），而不是和用户命令混在一起。秘密命令是内部 IR 里一条由编译器注入、与用户命令分账管理的命令缓冲（见 [IR.scala:444-448](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L444-L448)），它**仍然会被序列化进 CHIRRTL**，所以你在 `emitCHIRRTL` 输出里能直接看到这些 `intrinsic(...)`。

`DefIntrinsic` 命令本身的形状（[IR.scala:655-656](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L655-L656)）：

```scala
case class DefIntrinsic(sourceInfo: SourceInfo, intrinsic: String, args: Seq[Arg], params: Seq[(String, Param)])
    extends Command
```

`intrinsic` 是名字（如 `"circt_debug_var"`），`params` 是属性键值表（`StringParam`/`IntParam`），`args` 是可选的操作数（双向端口不挂操作数）。

#### 4.3.2 核心流程

```
DebugIntrinsics.generate(circuit)
  └─ 对每个 component：emitter.generate(component)
       ├─ DefModule / DefClass  → processModule
       │     ├─ 模块自身   → circt_debug_moduleinfo（addSecretCommand）
       │     ├─ 每个端口    → createIntrinsic(Data) 递归（含 subfield）
       │     └─ processBlock：遍历命令
       │           ├─ DefPrim/DefWire/DefReg/DefRegInit → createIntrinsic(Data)
       │           ├─ DefMemory/DefSeqMemory/FirrtlMemory → createIntrinsicMem
       │           └─ When/LayerBlock/DefContract → 递归 processBlock
       ├─ DefBlackBox / DefIntrinsicModule → 跳过（不发 moduleinfo）
       └─ 其它 → 抛 InternalErrorException
```

几个关键细节：

- **去重**：`emittedIds`（按 `Data` 身份）保证一个信号只发一次 `var`；`emittedEnums`（按全限定名）保证一个枚举类型只发一次 `enumdef`。
- **parent 透传根 FQN**：聚合类型递归时，每个叶子的 `parent` 指向**根变量**的全限定名，而非紧邻的父 Bundle。代码注释点明这是为了配合 CIRCT 的 `CirctDebugVarConverter` 按「精确相等」把叶子匹配回根。
- **跳过黑盒/内建模块**：黑盒（`DefBlackBox`）和内建模块（`DefIntrinsicModule`）不发 `moduleinfo`——它们不是 Chisel 细化出来的。
- **`_` 前缀的合成名跳过**：以 `_` 开头的根变量名「活不到最终 FIRRTL」，直接不发（见 `createDebugIntrinsic`）。

#### 4.3.3 源码精读

**① 顶层入口** —— [core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala:16-21](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L16-L21)

```scala
private[chisel3] object DebugIntrinsics {
  def generate(circuit: Circuit): Unit = {
    val emitter = new ComponentDebugEmitter
    circuit.components.foreach(emitter.generate)
  }
```

无状态、对每个 component 跑一遍内部 emitter。

**② 按 Component 类型分流** —— [DebugMetaEmitter.scala:29-38](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L29-L38)

`DefModule` 与 `DefClass` 都走 `processModule`；`DefBlackBox`/`DefIntrinsicModule` 直接 `()` 跳过；未知类型抛 `InternalErrorException`。

**③ 模块级发射：自身 + 端口 + 体** —— [DebugMetaEmitter.scala:40-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L40-L45)

```scala
private def processModule(id: BaseModule, allPorts: Seq[Port], block: Block): Unit = {
  emittedIds.clear()
  createIntrinsic(id, id._getSourceLocator).foreach(block.addSecretCommand)   // moduleinfo
  allPorts.foreach { p => createIntrinsic(p.id, None, p.sourceInfo).foreach(block.addSecretCommand) }
  processBlock(block)
}
```

注意 `allPorts = ports ++ ctx.secretPorts`——秘密端口（如 probe 引出的隐藏端口）也一起处理。

**④ 遍历命令体，挑出要标注的硬件** —— [DebugMetaEmitter.scala:61-70](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L61-L70)

```scala
private def generate(cmd: Command): Seq[Command] = cmd match {
  case e: DefPrim[_]        => createIntrinsic(e.id, None, e.sourceInfo)
  case DefWire(si, id)      => createIntrinsic(id, None, si)
  case DefReg(si, id, _)    => createIntrinsic(id, None, si)
  case DefRegInit(si, id, _, _, _) => createIntrinsic(id, None, si)
  case DefMemory(si, id, t, size)  => createIntrinsicMem(id, t, size, si)
  case DefSeqMemory(...)            => createIntrinsicMem(id, t, size, si)
  case FirrtlMemory(...)            => createIntrinsicMem(id, t, size, si)
  case _                            => Seq.empty
}
```

这正是 u4-l2 学过的命令节点（`DefPrim`/`DefWire`/`DefReg`/`DefRegInit`/`DefMemory`/`DefSeqMemory`）——这里逐个挑出来打调试标签。

**⑤ 递归聚合类型：var + subfield** —— [DebugMetaEmitter.scala:108-124](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L108-L124)

```scala
private def createIntrinsic(target: Data, parent: Option[String], si: SourceInfo): Seq[Command] = {
  if (!emittedIds.add(target)) return Seq.empty            // 去重
  val typeName = dataToTypeName(target)
  val childParent: Option[String] = parent.orElse(Some(signalRef(target)))  // 叶子继承根 FQN
  val subCmds: Seq[Command] = target match {
    case e: EnumType => createEnumDefIntrinsic(e, si).toSeq
    case record: Record => record.elements.values.flatMap(createIntrinsic(_, childParent, si)).toSeq
    case vecLike: VecLike[_] => vecLike.toSeq.flatMap(e => createIntrinsic(e, childParent, si))
    case _ => Nil
  }
  subCmds ++ createDebugIntrinsic(target, typeName, parent, extractParams(target), si).toSeq
}
```

`Record`（Bundle）和 `VecLike`（Vec）会递归到每个子元素，先发子元素（带 `parent`），再发自身（根，无 `parent`）。

**⑥ 决定发 var 还是 subfield** —— [DebugMetaEmitter.scala:169-200](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L169-L200)

```scala
val intrinsicName = if (parent.isDefined) "circt_debug_subfield" else "circt_debug_var"
...
if (parent.isEmpty && name.startsWith("_")) return None      // 合成名活不到最终 FIRRTL
val ssaOperands: Seq[Arg] =
  if (target.direction.isInstanceOf[ActualDirection.Bidirectional]) Nil else Seq(Node(target))
```

`parent` 有无决定 `var`/`subfield`；双向端口不挂 SSA 操作数（FIRRTL 要求操作数是 passive）；`enumTypeName`/`enumFqn` 仅枚举类型附加。

**⑦ 内存专用发射** —— [DebugMetaEmitter.scala:86-106](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L86-L106)

内存的 `typeName` 形如 `Mem[UInt<8>[16]]`（元素类型 + 深度），名字取自 `getOptionRef.localName` 或 `MemBase.instanceName`；空名内存记一条 warn 后跳过。

**⑧ 枚举定义发射（含位宽）** —— [DebugMetaEmitter.scala:134-158](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L134-L158)

特意把枚举的真实位宽 `width` 传下去——注释解释：否则 `LowerIntrinsics` 回退到 `i64`，下游 `EmitUHDI` 会把一个小枚举误报成 `uint64`。这是 Chisel 类型信息精确传递的一个细节体现。

#### 4.3.4 代码实践

**实践目标**：数清楚一个含 Bundle 端口 + Reg + 枚举的模块，会发射多少条、哪几种 `circt_debug_*`。

**操作步骤**：

1. 写一个稍微复杂一点的模块：

   ```scala
   // 示例代码
   object MyState extends ChiselEnum { val Idle, Busy, Done = Value }
   class MyIO(val w: Int) extends Bundle { val data = UInt(w.W); val last = Bool() }

   class DebugDemo extends Module {
     val io = IO(new Bundle { val enq = Input(new MyIO(8)); val state = Output(MyState()) })
     val st = RegInit(MyState.Idle)
     io.state := st
   }
   ```

2. 生成并 grep：

   ```bash
   # 示例命令（在 Scala REPL / 测试里调 emitCHIRRTL 后保存到文件 demo.chirrtl）
   grep -c "circt_debug_moduleinfo" demo.chirrtl   # 预期 1
   grep -c "circt_debug_var"         demo.chirrtl   # 根信号
   grep -c "circt_debug_subfield"    demo.chirrtl   # Bundle 叶子
   grep -c "circt_debug_enumdef"     demo.chirrtl   # 预期 1（MyState）
   ```

**需要观察的现象**：`MyIO` 的 `data`/`last` 两个字段各产生一条 `circt_debug_subfield`，且其 `parent=` 都指向 `enq` 这个根变量的名字；`MyState` 只产生一条 `circt_debug_enumdef`（携带 `variants`）。

**预期结果**（待本地验证，可对照仓库测试 `DebugDataTypesSpec`）：根变量数 = 端口数 + 寄存器数；`subfield` 数 = 所有 Bundle/Vec 展开后的叶子数；`enumdef` 数 = 不同的枚举类型数。

#### 4.3.5 小练习与答案

**练习 1**：为什么黑盒模块（`DefBlackBox`）不发 `circt_debug_moduleinfo`？

**参考答案**：黑盒是外部 SystemVerilog 模块，Chisel 不细化其内部、也不知道它的「构造器参数」语义；它的实现不在 Chisel 类型系统内，强行附调试元数据既无意义也无处可挂。故 `generate` 对 `DefBlackBox`/`DefIntrinsicModule` 直接 `()` 跳过（[DebugMetaEmitter.scala:34-35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L34-L35)）。

**练习 2**：一个 `Vec` 端口会产生多少条 `circt_debug_subfield`？它们共享同一个 `parent` 吗？

**参考答案**：`VecLike` 分支会 `vecLike.toSeq.flatMap(...)` 对每个元素递归，每个元素再按自身结构展开。每个叶子 `subfield` 的 `parent` 都指向该 `Vec` 端口的根变量 FQN（因 `childParent = parent.orElse(Some(signalRef(target)))`，叶子继承根）。具体条数取决于元素类型——若元素是 `UInt`，则每个元素贡献若干叶子。

---

### 4.4 开关、抑制与 Phase 集成

前三节讲了「数据从哪来、发成什么」。本节讲**控制层**：何时发、发到哪、哪些不要发。涉及三个组件：`EmitDebugIntrinsicsAnnotation`（开关）、`AddDebugIntrinsics`（Phase）、`SuppressDebugParams`（抑制）。

#### 4.4.1 概念说明

**(1) `EmitDebugIntrinsicsAnnotation`：opt-in 总开关**

调试内建函数**默认不发射**——它是实验性能力，会增加输出体积。必须显式 opt-in：命令行加 `--with-experimental-debug-intrinsics`，或往 `AnnotationSeq` 里塞这个注解。它的 trait 组合透露了它的性质：

- `NoTargetAnnotation`：不绑定到电路里某个具体目标，是「全局」注解。
- `Unserializable`：**不**随注解文件（`.anno.json`）持久化——它是「本次编译的临时开关」。
- `HasShellOptions`：自带命令行选项。

**(2) `AddDebugIntrinsics`：Phase 管道里的执行者**

它是 u5-l1 讲过的 `Phase`（`AnnotationSeq => AnnotationSeq`）。它的依赖声明很有讲究：

- `prerequisites = Seq(Dependency[Elaborate])`：必须先长出电路（`Elaborate` 产出 `ChiselCircuitAnnotation`）才能遍历它。
- `optionalPrerequisiteOf = Seq(Dependency[Convert], Dependency[AddDedupGroupAnnotations])`：它**主动要求排在 `Convert` 之前**——因为要在内部 IR 还没翻译成标准 FIRRTL IR 时注入 secret command。
- `invalidates = false`：它只往模块里加 secret command，不撤销别的 Phase 的效果。

没有开关注解时它是 **no-op**（原样返回注解）；有注解时它对 `ChiselCircuitAnnotation` 里的内部 circuit 调 `DebugIntrinsics.generate`，并**吃掉注解**——这是为了幂等：再跑一遍不会再加一遍内建函数。

**(3) `SuppressDebugParams`：跳过冗余的 params 抽取**

有些类型的「主构造器参数」其实就是它的子字段结构，已经被 `circt_debug_subfield` 充分表达了，再抽一遍 `params=` 是冗余的（典型例子：`chisel3.util.MixedVec`）。混入这个 marker trait 的类型，`DebugMetaEmitter` 会跳过 `params=` 抽取。

为什么用 marker trait 而不是直接按类型名判断？文件注释说得很直白：`MixedVec` 住在 `chisel` 模块（依赖 `core`），**`core` 无法按类型引用它**，只能用 trait 让对方去 mix in。

#### 4.4.2 核心流程

把三者和 Phase 管道（u5-l1）串起来：

```
用户: --with-experimental-debug-intrinsics
  └─ Shell 解析 → EmitDebugIntrinsicsAnnotation 加入 AnnotationSeq
        └─ PhaseManager 调度（按依赖拓扑序）:
              Elaborate  → 产出 ChiselCircuitAnnotation（含内部 circuit）
              AddDebugIntrinsics  → 检测到注解:
                    对每个 ChiselCircuitAnnotation 调 DebugIntrinsics.generate(circuit._circuit)
                    （往各模块注入 circt_debug_* secret command）
                    吃掉 EmitDebugIntrinsicsAnnotation（防重复）
              Convert  → 把（已注入 secret command 的）内部 IR 翻成标准 FIRRTL IR
              ... → CIRCT(firtool) → SystemVerilog
```

关键点：注入发生在 **Elaborate 之后、Convert 之前**——也就是在 Chisel 私有内部 IR 阶段动手，secret command 随后随 IR 一起被 `Convert`/序列化带出去。

`AddDebugIntrinsics.transform` 的核心逻辑（[AddDebugIntrinsics.scala:20-31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddDebugIntrinsics.scala#L20-L31)）是 `flatMap`：把 `EmitDebugIntrinsicsAnnotation` 映射成空、把 `ChiselCircuitAnnotation` 映射成「先 `generate` 再原样返回」、其它注解透传。

`SuppressDebugParams` 的作用点在 emitter 的 `suppressDebugParams` 判定（[DebugMetaEmitter.scala:72-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L72-L78)）：对 `Bits`/`Clock`/`Reset`/`Analog`/`Vec`/`EnumType` 以及混入 `SuppressDebugParams` 的类型，`extractParams` 直接返回空，于是 `paramsAttr` 不附加 `params=`。

#### 4.4.3 源码精读

**① opt-in 注解与命令行选项** —— [core/src/main/scala/chisel3/debug/EmitDebugIntrinsicsAnnotation.scala:14-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/EmitDebugIntrinsicsAnnotation.scala#L14-L24)

```scala
case object EmitDebugIntrinsicsAnnotation extends NoTargetAnnotation with Unserializable with HasShellOptions {
  override val options: Seq[ShellOption[Unit]] = Seq(
    new ShellOption[Unit](
      longOption = "with-experimental-debug-intrinsics",
      toAnnotationSeq = _ => Seq(EmitDebugIntrinsicsAnnotation),
      helpText = "Emit circt_debug_* intrinsics carrying Chisel type metadata",
      helpValueName = None
    )
  )
}
```

它在 [circt/stage/Shell.scala:61](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Shell.scala#L61) 被注册进 CLI，所以 `--with-experimental-debug-intrinsics` 对 `ChiselStage` 生效。

**② Phase 依赖与幂等消费** —— [src/main/scala/chisel3/stage/phases/AddDebugIntrinsics.scala:14-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddDebugIntrinsics.scala#L14-L32)

```scala
class AddDebugIntrinsics extends Phase {
  override def prerequisites = Seq(Dependency[Elaborate])
  override def optionalPrerequisiteOf = Seq(Dependency[Convert], Dependency[AddDedupGroupAnnotations])
  override def invalidates(a: Phase) = false

  def transform(annotations: AnnotationSeq): AnnotationSeq =
    if (!annotations.contains(EmitDebugIntrinsicsAnnotation)) annotations   // 没开关 → no-op
    else annotations.flatMap {
      case EmitDebugIntrinsicsAnnotation => Nil                              // 吃掉，防重复
      case a: ChiselCircuitAnnotation => DebugIntrinsics.generate(a.elaboratedCircuit._circuit); Seq(a)
      case a => Seq(a)
    }
}
```

**③ 它被挂进两条 PhaseManager 管道** —— [src/main/scala/circt/stage/ChiselStage.scala:37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L37) 与 [ChiselStage.scala:60](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L60)：命令行 `class ChiselStage.run` 与库 API `object ChiselStage.phase` 都把 `AddDebugIntrinsics` 排在 `Elaborate` 与 `Convert` 之间。

**④ 抑制标记** —— [core/src/main/scala/chisel3/debug/SuppressDebugParams.scala:14](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/SuppressDebugParams.scala#L14)

```scala
private[chisel3] trait SuppressDebugParams { self: Data => }
```

一个空 marker trait（`self: Data =>` 要求混入方必须是 `Data`）。它被 `suppressDebugParams` 的 case 匹配命中后跳过 `params=`（[DebugMetaEmitter.scala:76](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/DebugMetaEmitter.scala#L76)）。

#### 4.4.4 代码实践

**实践目标**：验证「开关默认关、打开后发射、Phase 幂等」三件事。

**操作步骤**：

1. **默认关**：

   ```scala
   // 示例代码
   val off = circt.stage.ChiselStage.emitCHIRRTL(new Module {
     val in = IO(Input(UInt(8.W))); val out = IO(Output(UInt(8.W))); out := in
   })
   assert(!off.contains("circt_debug_"))   // 默认没有任何调试内建函数
   ```

2. **打开后发射**：同样的模块加 `args = Array("--with-experimental-debug-intrinsics")`，输出应含 `circt_debug_moduleinfo` 与 `circt_debug_var`（与仓库测试 `DebugIntrinsicsSpec`「emits circt_debug_* intrinsics with --with-experimental-debug-intrinsics」一致）。

3. **幂等性**（对照测试「consumes EmitDebugIntrinsicsAnnotation so a second pass does not double-emit」）：手动构造注解，连跑两次 `new AddDebugIntrinsics().transform(...)`，第二次后 `circt_debug_*` secret command 的数量应与第一次完全相等（因为第一次已吃掉注解，第二次变 no-op）。

**需要观察的现象 / 预期结果**：步骤 1 输出无 `circt_debug_`；步骤 2 出现；步骤 3 两次计数相等。

> 若本地不便跑 Scala，至少阅读 [DebugIntrinsicsSpec.scala:123-134](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chisel3/debug/DebugIntrinsicsSpec.scala#L123-L134)（开关）与 [DebugIntrinsicsSpec.scala:136-183](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chisel3/debug/DebugIntrinsicsSpec.scala#L136-L183)（幂等），它们就是上述断言的源码依据。

#### 4.4.5 小练习与答案

**练习 1**：`EmitDebugIntrinsicsAnnotation` 混入 `Unserializable` 是什么意思？为什么这样设计？

**参考答案**：`Unserializable` 表示该注解不会被写进可序列化的注解文件（`.anno.json`）。这样设计是因为它是「本次编译要不要附带调试元数据」的临时开关，与电路本身无关；若被持久化，会在无意中让所有读该注解文件的下游编译都带上调试内建函数，造成意外。

**练习 2**：为什么 `AddDebugIntrinsics` 要 `optionalPrerequisiteOf = Seq(Dependency[Convert], ...)` 而不是直接 `prerequisites`？

**参考答案**：`prerequisites` 是「我需要别人先跑」；`optionalPrerequisiteOf` 是「我主动要求排在别人前面」（u5-l1）。这里要在 `Convert` 把内部 IR 翻成标准 FIRRTL IR **之前**注入 secret command，所以必须声明自己要在 `Convert` 之前执行。用 `optionalPrerequisiteOf` 而非硬编码顺序，正是 Phase 依赖式调度的精髓。

**练习 3**：`SuppressDebugParams` 为什么做成 marker trait 而不是一个类型名单？

**参考答案**：因为最典型的受益者 `MixedVec` 在 `chisel` 模块，而 `DebugMetaEmitter` 在 `core`；`core` 被 `chisel` 依赖，不能反向引用 `chisel` 的类型（[SuppressDebugParams.scala:6-13](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/debug/SuppressDebugParams.scala#L6-L13) 注释）。marker trait 让 `core` 定义能力、`chisel` 的类型去 mix in，绕过依赖方向问题。

---

## 5. 综合实践

把本讲两套机制放进**同一个模块**，验证它们互不干扰、各司其职。

**任务**：写一个带构造器参数、带 `printf`、带 Bundle 端口和寄存器的模块，一次性生成 SystemVerilog，分别观察「调试代码隔离」和「调试元数据附上」两种现象。

```scala
// 示例代码
import chisel3._

class DebugCombo(val width: Int) extends Module {
  val io = IO(new Bundle {
    val in  = Input(UInt(width.W))
    val out = Output(UInt(width.W))
  })
  val reg = RegInit(0.U(width.W))
  reg := io.in
  io.out := reg
  printf(p"cycle: in=${io.in} out=${io.out}\n")   // (A) 调试代码
}

// 一次生成，两套机制各自体现
val sv = circt.stage.ChiselStage.emitSystemVerilog(
  new DebugCombo(8),
  args = Array("--with-experimental-debug-intrinsics", "--split-verilog")
)
```

**要回答的问题**：

1. **(A) 调试代码隔离**：`printf` 最终落在哪个输出文件里？（预期：`verification/debug/` 目录下的独立 `.sv`，通过 `bind` 挂回 `DebugCombo`；主模块文件里看不到 `printf`。）若改用 `emitCHIRRTL`，应在 `layer Verification.Debug` 块下看到这条 `printf`。
2. **(B) 调试元数据附上**：`DebugCombo` 模块对应的 `circt_debug_moduleinfo` 的 `params=` 里有没有 `"width":"8"`？（预期：有。）
3. **独立性**：去掉 `--with-experimental-debug-intrinsics` 重跑，(A) 是否照常隔离？（预期：是——开关只控制 (B)，不影响 (A)。）反过来，把 `printf` 那行删掉重跑，(B) 是否照常附上？（预期：是。）

**预期结论**：Debug 层（隔离代码）与调试元数据（附上信息）是两条完全独立的通路——前者由 `SimLog` 在 elaboration 期把命令塞进层块，后者由 `AddDebugIntrinsics` Phase 在 Convert 之前注入 secret command。它们共享「debug」之名，却由不同的源码路径、在不同的编译阶段、解决不同的问题。

> 若本地无法运行，可改用「源码阅读型」完成：画出两条通路各自的调用链（`printf→SimLog→layer.block(Verification.Debug)` 与 `--flag→AddDebugIntrinsics→DebugIntrinsics.generate→addSecretCommand`），并标注它们各自发生在哪个 Phase。

## 6. 本讲小结

- Chisel 里「debug」有两套**独立**机制：(A) **Debug 层**（`Verification.Debug`）隔离调试代码，(B) **`chisel3.debug` 包**附上机器可读的调试元数据。
- **(A) Debug 层**是 2026-06 新增（`cc46629d01`）的内建 Extract 子层；`printf`/`flush` 在 `SimLog.printfWithoutReset` 里被 `layer.block(layers.Verification.Debug, skipIfAlreadyInBlock=true, skipIfLayersEnabled=true)` 自动收容，动机是「断言可综合、打印不可综合」。
- **(B) 调试元数据**用 Java 反射（`CtorParamExtractor`）抽取模块构造器参数并序列化成 JSON，由 `DebugIntrinsics.generate` 遍历内部 IR，发射 `circt_debug_moduleinfo`/`var`/`subfield`/`enumdef` 四类内建函数，走 `addSecretCommand` 秘密命令通道。
- 整套 (B) 受 **`EmitDebugIntrinsicsAnnotation`**（`--with-experimental-debug-intrinsics`，默认关、`Unserializable`）控制，由 **`AddDebugIntrinsics`** Phase（`prerequisites=Elaborate`，`optionalPrerequisiteOf=Convert`，吃注解保幂等）在 Convert 之前执行。
- **`SuppressDebugParams`** marker trait 让 `core` 无法按类型引用的 `chisel` 模块类型（如 `MixedVec`）也能跳过冗余 `params=`，绕过 `core → chisel` 的依赖方向。
- 两条通路各自独立：开关只控元数据、不影响 printf 入层；删 printf 也不影响元数据发射。

## 7. 下一步学习建议

- **下游如何消费这些调试信息**：本讲只讲「发射」，`circt_debug_*` 内建函数最终由 CIRCT 的 `CirctDebugVarConverter`/`LowerIntrinsics` pass 解释。建议阅读 CIRCT 仓库的 `circt_debug` 相关 pass，理解 `typeName`/`parent`/`variants` 如何变成 UHDM 调试信息。
- **回到层体系**：本讲的 Debug 层只是 `Verification` 层树的一片叶子。结合 u8-l3 重读 [docs/src/explanations/layers.md](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/docs/src/explanations/layers.md) 的「Design Verification Example」，体会 Extract/Inline、`bind`、`` `ifdef `` 宏三者的协作。
- **u9-l4 Annotation 注解系统**：`EmitDebugIntrinsicsAnnotation` 是 `AnnotationSeq` 数据流的一个实例，下一讲会系统讲解 Annotation 如何贯穿整个 Phase 管道并影响下游。
- **如果想深入反射边界**：阅读 `CtorParamsPlatform`（Scala 2 与 Scala 3 两个实现），看 Chisel 如何在两套编译器上稳定地列出主构造器参数——这是 `macros`/`plugin` 之外另一处「平台相关」代码。
