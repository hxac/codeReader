# Convert / Checks / Emitter 阶段

## 1. 本讲目标

本讲承接 u5-l2（Elaborate 阶段）与 u4-l5（Converter），把目光从「长出电路」往后移一步，聚焦 Elaborate **之后**、CIRCT(firtool) **之前** 这一段管道里的三个 `Phase`：

- **Convert**：把 Chisel 内部 IR 翻译成 FIRRTL 编译器认识的「标准 IR」。
- **Checks**：在管道早期做合法性 / 一致性检查，拦截重复或冲突的注解。
- **Emitter**：当用户指定了输出文件名时，把电路写成一个 `.fir` 文件。
- **CircuitSerializationAnnotation**：贯穿发射动作的「数据载体」，把一个 `ElaboratedCircuit` 与文件名绑定在一起。

学完本讲，你应当能够：

1. 说清 Convert 的**输入注解 / 输出注解**分别是什么，以及它如何调用上一单元的 `Converter`。
2. 理解 Checks 用什么机制把自己「插队」到 Elaborate 之前，以及它具体检查哪些约束。
3. 看懂 Emitter 写文件的副作用，以及它为何要在注解序列里构造一个 `CircuitSerializationAnnotation`。
4. 解释 `CircuitSerializationAnnotation` 如何惰性地把内部 IR 序列化成 CHIRRTL 文本。

## 2. 前置知识

本讲默认你已经掌握 u5-l1（Stage / Phase 管道）和 u5-l2（Elaborate 阶段）的内容。这里快速复述三个关键概念：

- **Phase**：来自 `firrtl.options`，本质上是一个纯函数 `AnnotationSeq => AnnotationSeq`。它通过四组依赖关系（`prerequisites` / `optionalPrerequisites` / `optionalPrerequisiteOf` / `invalidates`）声明调度需求，由 `PhaseManager` 自动排定执行顺序。
- **AnnotationSeq（注解序列）**：贯穿整条编译管道的**唯一数据载体**。电路、配置、产物都以「注解」的形式在里面流动，每个 Phase 读取若干注解、产出若干注解。
- **`optionalPrerequisiteOf`**：一种「反向」依赖。一个 Phase 声明 `optionalPrerequisiteOf = Dependency[X]`，意思是「如果有 Phase X 要运行，请把我**排在它前面**」。本讲的 Checks 和 Emitter 都用它来插队。

还需要两个名词：

- **`ChiselCircuitAnnotation`**：u5-l2 中 Elaborate 的产物，里面装着 elaboration 长出来的 `ElaboratedCircuit`（Chisel 私有内部 IR 的对外封装）。它是本讲三个 Phase 的共同输入。
- **`ElaboratedCircuit._circuit`**：`ElaboratedCircuit` 内部藏着的真正内部 IR 根节点 `chisel3.internal.firrtl.ir.Circuit`（见 u4-l2 的 `Circuit ⊃ Component ⊃ Command` 三层树）。`_circuit` 是 `private[chisel3]`，只有同包的 Convert 等内部代码能直接访问。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/main/scala/chisel3/stage/phases/Convert.scala` | 把 `ChiselCircuitAnnotation` 经 `Converter` 转成 `FirrtlCircuitAnnotation`（已 deprecated）。 |
| `src/main/scala/chisel3/stage/phases/Checks.scala` | 管道早期的合法性检查：重复注解、冲突的 `RemapLayer`。 |
| `src/main/scala/chisel3/stage/phases/Emitter.scala` | 当指定了输出文件时，把电路写出到 `.fir` 文件。 |
| `src/main/scala/chisel3/stage/ChiselAnnotations.scala` | `ChiselCircuitAnnotation`、`CircuitSerializationAnnotation` 等注解的定义。 |
| `core/src/main/scala/chisel3/internal/firrtl/Converter.scala` | （u4-l5）真正做 IR 翻译的对象，Convert 调用它。 |
| `core/src/main/scala/chisel3/ElaboratedCircuit.scala` | `ElaboratedCircuit` trait，提供 `lazilySerialize` 等序列化入口。 |
| `src/main/scala/chisel3/stage/phases/AddSerializationAnnotations.scala` | 现代发射路径：往注解序列里**加**一个 `CircuitSerializationAnnotation`（与 Emitter 对照）。 |

## 4. 核心概念与源码讲解

### 4.1 Convert：从 Chisel IR 到 FIRRTL IR 的桥（已 deprecated）

#### 4.1.1 概念说明

u4-l5 讲过，源码里同时存在两个同名 `Circuit`：一个是 Chisel 私有的 `chisel3.internal.firrtl.ir.Circuit`（内部 IR），另一个是 FIRRTL 编译器认识的「标准 IR」`firrtl.ir.Circuit`。`Converter` 是两者之间的翻译桥，而 **Convert 这个 Phase 就是「按一下翻译按钮」的那一层薄包装**：它在注解序列里找到 `ChiselCircuitAnnotation`，取出其中的内部 IR，交给 `Converter.convert` 翻成标准 FIRRTL IR，再包成一个 `FirrtlCircuitAnnotation` 放回序列。

> ⚠️ 重要：这条「全量翻译成 `firrtl.ir.Circuit`」的路径自 **Chisel 7.11.0** 起已 `@deprecated`。现代发射（生成 Verilog）已经绕开它，改由 `ElaboratedCircuit` 直连 CIRCT。但 Convert 仍是理解「Chisel IR ↔ FIRRTL」对应关系最直接的代码入口，且 `ChiselStage` 的管道仍会把它拉进来运行，所以本讲仍然重点讲解它。

#### 4.1.2 核心流程

Convert 的工作可以浓缩为一行伪代码：

```
对每个 ChiselCircuitAnnotation(a):
    保留 a 原样
    新增 FirrtlCircuitAnnotation( Converter.convert(a.elaboratedCircuit._circuit) )
    把 a.elaboratedCircuit.annotations（FIRRTL 注解）也平铺进来
其余注解原样透传
```

数据流是「**一对一变多**」：一个 `ChiselCircuitAnnotation` 进来，变成「自己 + 一个 `FirrtlCircuitAnnotation` + 若干 FIRRTL 注解」。

调度上，Convert 声明 `prerequisites = Seq(Dependency[Elaborate])`，即必须等 Elaborate 把电路长出来之后才能翻译。

#### 4.1.3 源码精读

先看类的声明与 deprecated 标记：[Convert.scala:16-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala#L16-L22)。注意第 16 行的 `@deprecated(...)` 注解和第 19 行 `prerequisites = Seq(Dependency[Elaborate])`——这两点决定了它的「已弃用但仍排在 Elaborate 之后」的身份。

整个翻译逻辑只有短短几行：[Convert.scala:24-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala#L24-L32)。关键就是第 28 行这一句：

```scala
Some(FirrtlCircuitAnnotation(Converter.convert(a.elaboratedCircuit._circuit)))
```

它做了三件事：

1. `a.elaboratedCircuit._circuit` —— 取出 `ElaboratedCircuit` 内部那个 `private[chisel3]` 的内部 IR 根节点。
2. `Converter.convert(...)` —— 调用 u4-l5 讲过的翻译器，返回一个 `firrtl.ir.Circuit`。
3. `FirrtlCircuitAnnotation(...)` —— 包成 FIRRTL 编译器认识的注解（来自 `firrtl.stage` 包，见文件第 9 行 import）。

`Converter.convert(circuit)` 的真身在 [Converter.scala:548-558](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L548-L558)：它逐字段把 Chisel 的 `Circuit` 同构映射成 `fir.Circuit`——`components.map(convert)` 翻译所有模块、`layers.map(convertLayer)` 翻译层、`options.map(convertOption)` 翻译选项。

> 小贴士：Convert 的源码故意写得很薄——它**自己不做任何翻译**，只是把 `Converter` 这座桥接到 Phase 管道里。真正的翻译规则全在 u4-l5 详述的 `Converter` 对象中。

#### 4.1.4 代码实践

**实践目标**：亲手验证 Convert 的输入 / 输出注解，以及它如何调用 `Converter`。

**操作步骤**：

1. 打开 [Convert.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala)。
2. 对照 `transform` 方法，填写下面这张表：

   | 项 | 答案（从源码读出） |
   | --- | --- |
   | 输入注解（要处理的） | `ChiselCircuitAnnotation` |
   | 输出注解（新增的） | ？ |
   | 调用 Converter 的那一行位于第几行 | ？ |
   | 传入 Converter 的实参是什么 | ？ |

3. 跟进 `Converter.convert` 在 [Converter.scala:548](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L548)，确认它返回的类型是 `fir.Circuit`。

**预期结果**：输出注解为 `FirrtlCircuitAnnotation`（外加把 `elaboratedCircuit.annotations` 平铺进来）；调用行是第 28 行；实参是 `a.elaboratedCircuit._circuit`。

> 「待本地验证」：若你想在运行时观察这条注解，可在自建 sbt/mill 工程里手动构造注解序列、依次跑 `Elaborate` 与 `Convert`，再 `collectFirst { case a: FirrtlCircuitAnnotation => a }` 打印其 `circuit` 字段——但这一步依赖 deprecated API，仅作理解用途。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Convert 必须把 `prerequisites` 设成 `Dependency[Elaborate]`？

> **参考答案**：Convert 要翻译的 `ChiselCircuitAnnotation` 是 Elaborate 的产物。若 Elaborate 没先跑，注解序列里根本没有 `ChiselCircuitAnnotation`，Convert 的 `flatMap` 就匹配不到任何东西，什么都不会发生。

**练习 2**：Convert 既已 deprecated，为什么 `ChiselStage` 的管道还会运行它？

> **参考答案**：因为 `ChiselStage` 的 `targets` 里仍显式列了 `Dependency[Convert]`（见 4.4 节）。`@deprecated` 只是编译期警告，不影响运行时调度；现代发射虽已改走 `ElaboratedCircuit` 直连 CIRCT，但这条旧翻译路径尚未从管道移除。

---

### 4.2 Checks：合法性与一致性检查

#### 4.2.1 概念说明

Checks 是一个**纯校验型** Phase：它不产生任何新注解，只读取注解序列、检查其中是否有「重复或冲突」的项，发现问题就抛 `OptionsException` 中止编译；没问题就把序列原样返回。

它检查三类东西：

1. `PrintFullStackTraceAnnotation` 最多只能有一个（对应命令行 `--full-stacktrace`）。
2. `ChiselOutputFileAnnotation` 最多只能有一个（对应 `--chisel-output-file`）。
3. `RemapLayer` 不能把同一个旧 layer 重映射到多个不同新 layer。

#### 4.2.2 核心流程

```
遍历注解，把它们按类型分到三个桶 st / outF / lm
若 st.size > 1  -> 抛 OptionsException
若 outF.size > 1 -> 抛 OptionsException
把 lm 里的 RemapLayer 逐条塞进 HashMap：若 oldLayer 已存在 -> 抛 OptionsException
原样返回 annotations
```

关键在于 Checks 的**调度声明**：它把自己注册成 Elaborate 的「可选前提」——[Checks.scala:18-21](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Checks.scala#L18-L21) 里 `optionalPrerequisiteOf = Seq(Dependency[Elaborate])`。也就是说：**只要 Elaborate 要跑，Checks 就会自动被拉进来、并且排在 Elaborate 之前**。这就是为什么 u5-l2 里 Elaborate 的 `prerequisites` 也列了 `Dependency[chisel3.stage.phases.Checks]`——二者互为印证，确保 Checks 一定先于 Elaborate 执行，把非法配置挡在 elaboration 之前。

#### 4.2.3 源码精读

整个检查逻辑在 [Checks.scala:23-58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Checks.scala#L23-L58)。

第一步，用三个 `ListBuffer` 把注解分类：[Checks.scala:24-30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Checks.scala#L24-L30)。

第二步，检查「至多一个」的两条规则：[Checks.scala:32-46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Checks.scala#L32-L46)。注意报错信息里贴心地列出了对应的命令行选项与注解类名（如 `--full-stacktrace`、`--chisel-output-file`），方便用户定位是哪里重复了。

第三步，检查 `RemapLayer` 冲突：[Checks.scala:48-56](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Checks.scala#L48-L56)。用一个 `HashMap[Layer, Layer]` 记录「旧 layer → 新 layer」映射，遇到同一个 `oldLayer` 已存在就抛错。`RemapLayer` 与 u8-l3 的 Layer 机制相关：它允许把某一层整体改名，但同一个源层显然不能同时改两个名字。

最后第 58 行原样返回 `annotations`——印证了 Checks 是「只读不写」的纯校验 Phase。

#### 4.2.4 代码实践

**实践目标**：触发 Checks 的报错，观察它如何把非法配置挡在 elaboration 之前。

**操作步骤**：

1. 在一个使用 Chisel 的工程里，构造一条带**两个** `ChiselOutputFileAnnotation` 的注解序列，例如：

   ```scala
   // 示例代码：故意制造重复注解以触发 Checks
   import chisel3.stage.{ChiselOutputFileAnnotation, ChiselGeneratorAnnotation}
   val annos = Seq(
     ChiselGeneratorAnnotation(() => new MyMod),
     ChiselOutputFileAnnotation("a.fir"),
     ChiselOutputFileAnnotation("b.fir")   // 重复！
   )
   ```

2. 把它交给 `ChiselStage` 的管道运行（或在命令行重复 `--chisel-output-file`）。

**需要观察的现象**：编译立即失败，抛出 `firrtl.options.OptionsException`，信息形如 `At most one Chisel output file can be specified but found '2'`。

**预期结果**：错误在 Elaborate 之前抛出（因为 Checks 排在 Elaborate 前），你**看不到任何 elaboration 阶段的输出**——这正是 Checks 「前置守门」的效果。

> 「待本地验证」：具体报错文本以本地运行的输出为准。

#### 4.2.5 小练习与答案

**练习 1**：Checks 没有出现在 `ChiselStage.phase` 的 `targets` 里（见 4.4 节），它为什么还能被执行？

> **参考答案**：因为它声明了 `optionalPrerequisiteOf = Dependency[Elaborate]`。只要 Elaborate 在 targets 里，PhaseManager 就会沿这条反向依赖把 Checks 自动补进来，并排在 Elaborate 之前。此外 Elaborate 自己也把 Checks 列为 `prerequisites`，双保险。

**练习 2**：如果把 Checks 的 `transform` 最后改成 `annotations.filter(...)` 去掉某些注解，会影响后续 Phase 吗？

> **参考答案**：会。Checks 虽定位为「只读校验」，但它返回的 `AnnotationSeq` 就是后续所有 Phase 的输入。当前实现原样返回（第 58 行），所以无副作用；若改成过滤，就会改变整条管道看到的数据。

---

### 4.3 Emitter：把电路写出到 .fir 文件

#### 4.3.1 概念说明

Emitter 的职责很具体：**当用户通过 `ChiselOutputFileAnnotation` 指定了输出文件名时，把 elaboration 出来的电路序列化成一个 `.fir`（CHIRRTL 文本）文件**。它不产生供后续 Phase 使用的注解——它的产物是一个**磁盘文件**（副作用）。

> ⚠️ 注意区分两条都能产出 `.fir` 文件的路径：
> - **Emitter**（本节，较旧）：构造一个 `CircuitSerializationAnnotation`，**立即**调用它的 `doWriteToFile` 把文件写盘，然后**丢弃**这个注解——文件写出是纯副作用。
> - **AddSerializationAnnotations**（现代）：把 `CircuitSerializationAnnotation` **加进**注解序列，交给下游（CIRCT / firrtl 的 `CustomFileEmission` 机制）去写。
>
> 二者目的相似，机制不同。下文会对照源码点出 Emitter 的「丢弃」细节。

#### 4.3.2 核心流程

```
读取 ChiselOptions.outputFile 与 StageOptions
对注解序列 flatMap：
  若遇到 ChiselCircuitAnnotation 且 outputFile 已定义：
     算出带 .fir 后缀的文件名
     构造 CircuitSerializationAnnotation(elaboratedCircuit, filename)
     调用 csa.doWriteToFile(...)  -> 写盘（副作用！）
返回原 annotations
```

调度上，Emitter 用 `optionalPrerequisiteOf = Seq(Dependency[Convert])` 把自己插到 Convert 之前（见源码第 28 行）。

#### 4.3.3 源码精读

先看 Emitter 的调度声明：[Emitter.scala:21-29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Emitter.scala#L21-L29)。注意两点：它的 `prerequisites` 里有 `AddImplicitOutputFile`（确保若没显式给文件名，会从顶层模块名推导一个）；它的 `optionalPrerequisiteOf = Seq(Dependency[Convert])` 让它跑在 Convert 之前。

核心逻辑在 [Emitter.scala:31-44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Emitter.scala#L31-L44)。这里藏着一个值得细读的细节：

```scala
annotations.flatMap {
  case a: ChiselCircuitAnnotation if copts.outputFile.isDefined =>
    val filename = sopts.getBuildFileName(copts.outputFile.get, Some(".fir"))
    val csa = CircuitSerializationAnnotation(a.elaboratedCircuit, filename)
    csa.doWriteToFile(new File(filename), Nil)   // 真正写盘的副作用
    Some(a)                                       // 只保留原 ChiselCircuitAnnotation
  case a => Some(a)
}
annotations   // ← 方法返回的是原始 annotations，上面的 flatMap 结果被丢弃！
```

读懂这段需要抓住两点：

1. **写盘的副作用发生在 `csa.doWriteToFile(...)`**：这一行调用 `CircuitSerializationAnnotation` 的写文件方法（见 4.4 节），把电路序列化成 CHIRRTL 文本写入 `filename`。这是 Emitter 真正的「产出」。
2. **`flatMap` 的结果被丢弃**：方法体最后一行返回的是原始 `annotations`，而不是上面 `flatMap` 的计算结果。这意味着 Emitter 构造的 `csa` **不会**进入注解序列——它只是被临时拿来调用 `doWriteToFile`，写完即弃。所以 Emitter 对后续 Phase **不可见**，它的全部效果就是那个磁盘上的 `.fir` 文件。

> 这种「构造一个对象只为借用它的方法、用完即弃」的写法在阅读老代码时常会遇到。理解它的关键是分清**返回值**（决定后续 Phase 看到什么）与**副作用**（决定外部世界发生什么）。

#### 4.3.4 代码实践

**实践目标**：让 Emitter 真正写出一个 `.fir` 文件，并确认它就是 CHIRRTL 文本。

**操作步骤**：

1. 在自建工程里写一个最小模块，并用命令行指定输出文件（这会生成 `ChiselOutputFileAnnotation`，从而满足 Emitter 的 `outputFile.isDefined` 条件）：

   ```scala
   // 示例代码：通过 ChiselStage 命令行触发 .fir 文件发射
   import chisel3._
   import circt.stage.ChiselStage

   class CountReg extends Module {
     val io = IO(new Bundle { val en = Input(Bool()); val cnt = Output(UInt(8.W)) })
     val r = RegInit(0.U(8.W))
     when(io.en) { r := r + 1.U }
     io.cnt := r
   }

   // --target=chirlrtl 让 ChiselStage 在产出 CHIRRTL 处停下；
   // --chisel-output-file 指定 Emitter/AddSerializationAnnotations 的落盘文件名
   ChiselStage.emitSystemVerilog(
     new CountReg,
     args = Array("--target-dir", "build", "--chisel-output-file", "CountReg.fir")
   )
   ```

2. 到 `build/` 目录查看是否生成了 `CountReg.fir`。

**需要观察的现象**：文件内容是 CHIRRTL 文本（以 `circuit CountReg :` 开头，能看到 `regreset r`、`connect` 等关键字，与 u4-l4 Serializer 讲的序列化输出一致）。

**预期结果**：能看到一个 `.fir` 文件，内容就是 Serializer 产出的 CHIRRTL（这正是 `emitCHIRRTL` 打印到字符串里的同一份文本，只是写到了文件）。

> 「待本地验证」：实际是否生成文件、文件名是否带 `.fir` 后缀，以本地运行结果为准。注意现代 Chisel 更常用 `AddSerializationAnnotations` 路径，二者落盘效果接近。

#### 4.3.5 小练习与答案

**练习 1**：如果把 Emitter 方法最后一行的 `annotations` 改成上面那个 `flatMap` 的结果，对后续管道有什么影响？

> **参考答案**：那样会把 `ChiselCircuitAnnotation`（`flatMap` 里 `Some(a)` 保留的）作为结果返回。由于 `flatMap` 并没有把 `csa` 包进结果，即便改了返回值，`CircuitSerializationAnnotation` 仍不会进入序列——所以对后续 Phase 几乎无影响；真正改变的是返回序列的「重建方式」。要让 `csa` 流向下游，得改成 `Some(csa) ++ Some(a)`（这正是 `AddSerializationAnnotations` 的做法）。

**练习 2**：Emitter 的 `prerequisites` 为什么要包含 `AddImplicitOutputFile`？

> **参考答案**：Emitter 只在 `copts.outputFile.isDefined` 时才写文件。若用户没显式给文件名，需要 `AddImplicitOutputFile` 先从顶层模块名推导出一个 `ChiselOutputFileAnnotation`，Emitter 才有 `outputFile` 可用。

---

### 4.4 CircuitSerializationAnnotation：贯穿发射的序列化载体

#### 4.4.1 概念说明

`CircuitSerializationAnnotation`（下称 **CSA**）是 Emitter 和 AddSerializationAnnotations 共用的数据结构，它把「一个 `ElaboratedCircuit` + 一个文件名 + 一种格式」打包成一个注解，并提供把电路序列化成文本、写入文件的能力。可以说它是「**让电路变成磁盘文件**」的统一抽象。

它混入了 `BufferedCustomFileEmission`（来自 firrtl.options），这意味着它也能被下游的 `CustomFileEmission` 机制识别并自动写盘——这就是 AddSerializationAnnotations「把 CSA 加进序列、让下游去写」那条路能走通的原因。

#### 4.4.2 核心流程

CSA 的核心是「**惰性序列化**」：

```
CSA 内部用一个 Either[Circuit, ElaboratedCircuit] 存电路（旧 API 用 Left，新 API 用 Right）
emitLazily(annos):
  取出 ElaboratedCircuit，调用它的 lazilySerialize(annos)
  -> 返回 Iterable[String]（一段段 CHIRRTL 文本，来自 u4-l4 的 Serializer）
writeToFileImpl(file, annos):
  打开 BufferedWriter
  逐段 write 进文件
```

「惰性」是为了支持超大电路：`lazilySerialize` 返回的是 `Iterable[String]`（内部是 `Iterator[String]`），不必一次性把整个电路的文本驻留内存，可流式写出，理论上能发射超过 2 GiB 的电路（源码注释明说）。

#### 4.4.3 源码精读

先看伴生对象与格式定义：[ChiselAnnotations.scala:333-352](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L333-L352)。这里定义了 `sealed trait Format`（带 `extension` 方法）和目前唯一的格式 `FirrtlFileFormat`（扩展名 `.fir`）。推荐的构造方法是 `apply(elaboratedCircuit, filename)`，它把电路放在 `Right` 里。

类本体在 [ChiselAnnotations.scala:361-369](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L361-L369)。关键字段是私有的 `_circuit: Either[Circuit, ElaboratedCircuit]`——`Left` 兼容旧的「裸 Circuit」构造，`Right` 是新的 `ElaboratedCircuit`。它混入 `BufferedCustomFileEmission` 与 `WriteableCircuitAnnotation`，二者让 firrtl/CIRCT 的发射机制能识别它。

写文件的实现在 [ChiselAnnotations.scala:409-415](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L409-L415)：`writeToFileImpl` 打开一个 `BufferedWriter`，逐段写入 `emitLazily(annos)` 产出的字符串。Emitter 调用的 `doWriteToFile`（[第 418 行](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L418)）只是把这个 `protected` 方法暴露给 `chisel3` 包用。

序列化的源头在 [ChiselAnnotations.scala:424-427](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L424-L427)：`emitLazily` 把活儿交给 `ElaboratedCircuit.lazilySerialize(annos)`。而 `lazilySerialize` 在 [ElaboratedCircuit.scala:43](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L43) 与 [:51](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L51) 声明，实现里最终调用 u4-l4 讲过的 `Serializer`——也就是说，CSA 写出的文本，正是 `Serializer` 逐节点序列化出的 CHIRRTL。

> 对照：作为输入的 `ChiselCircuitAnnotation` 定义在 [ChiselAnnotations.scala:300-305](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L300-L305)，它只是 `ElaboratedCircuit` 的简单包装。所以整条链是：`ChiselCircuitAnnotation(elaboratedCircuit)` → Convert 翻译成 FIRRTL IR；或 → Emitter/CSA 序列化成 CHIRRTL 文本落盘。两条路都从同一个 `ElaboratedCircuit` 出发。

最后，把这三个 Phase 放回 `ChiselStage` 的全局管道看一眼：[ChiselStage.scala:32-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L32-L49)（命令行 `run`）与 [:57-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L57-L68)（库 API `phase`）。可以看到 `Convert` 显式出现在 targets 里，而 `Checks`、`Emitter` 是靠 `optionalPrerequisiteOf` 被自动拉入的——这正好印证了 4.2、4.3 两节的调度讲解。

#### 4.4.4 代码实践

**实践目标**：把 `emitCHIRRTL` 的字符串输出与 CSA 写出的 `.fir` 文件对照，确认二者是同一份 Serializer 产物。

**操作步骤**：

1. 对同一个模块分别用两种方式拿 CHIRRTL：

   ```scala
   // 示例代码：对照「字符串发射」与「文件发射」
   import chisel3._
   import circt.stage.ChiselStage

   class OneReg extends RawModule {
     val io = IO(new Bundle { val i = Input(UInt(8.W)); val o = Output(UInt(8.W)) })
     val r = RegNext(io.i); io.o := r
   }

   // 方式 A：直接拿 CHIRRTL 字符串
   val str = ChiselStage.emitCHIRRTL(new OneReg)

   // 方式 B：经 Emitter/CSA 写成 OneReg.fir（见 4.3.4 的命令行写法）
   ```

2. 把方式 A 打印的字符串与方式 B 生成的 `OneReg.fir` 文件内容做 `diff`。

**需要观察的现象**：两者内容基本一致（都是 `circuit OneReg :` 开头的 CHIRRTL），因为它们最终都调用 `Serializer`。

**预期结果**：`diff` 结果为空或仅有极小差异，证明 CSA 的 `emitLazily` 与 `emitCHIRRTL` 共享同一条序列化路径。

> 「待本地验证」：二者是否完全逐字相同以本地 diff 为准（可能存在尾部空行等微小差异）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CSA 的 `emitLazily` 要返回 `Iterable[String]` 而不是一个完整的 `String`？

> **参考答案**：为了支持流式、惰性地发射超大电路。返回一段段的 `Iterable[String]`（内部是迭代器），写文件时逐段写入即可，不必把整个电路文本一次性驻留内存，从而能发射超过 2 GiB 的电路（源码注释明说）。

**练习 2**：CSA 同时混入 `BufferedCustomFileEmission`，这给它带来了什么能力？

> **参考答案**：这让 firrtl/CIRCT 的 `CustomFileEmission` 机制能识别它——只要 CSA 出现在注解序列里，下游发射阶段就会自动调用它的写文件方法落盘。这正是 `AddSerializationAnnotations`（把 CSA 加进序列）那条现代路径能走通的基础，与 Emitter「自己手动 `doWriteToFile`」的老路径形成对照。

## 5. 综合实践

把本讲三个 Phase 串起来，画一张「注解流动图」并验证它。

**任务**：

1. 在源码中确认下列 Phase 的执行先后（结合各自的 `prerequisites` / `optionalPrerequisiteOf` 与 [ChiselStage.scala:32-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L32-L68)）：

   ```
   Checks  →  Elaborate  →  Emitter  →  Convert  →  ... →  CIRCT
   ```

   说出每一步**读什么注解、写什么注解或副作用**，填入下表：

   | Phase | 读入 | 产出 / 副作用 |
   | --- | --- | --- |
   | Checks | 全部注解（挑出 stacktrace/outputFile/RemapLayer） | 无（非法则抛 OptionsException） |
   | Elaborate | `ChiselGeneratorAnnotation` | `ChiselCircuitAnnotation` + `DesignAnnotation` |
   | Emitter | `ChiselCircuitAnnotation` + outputFile | 写 `.fir` 文件（副作用） |
   | Convert | `ChiselCircuitAnnotation` | `FirrtlCircuitAnnotation` + FIRRTL 注解 |

2. 用一个最小模块（如带一个 `RegNext` 的 `RawModule`）跑 `ChiselStage.emitSystemVerilog`，并加 `--chisel-output-file MyMod.fir`，确认 `build/` 下既出现了 `.fir`（Emitter/CSA 的产物），最终也产出了 `.sv`（CIRCT 的产物）。

3. 打开生成的 `MyMod.fir`，找到其中 `regreset` / `connect` 行，回溯到 u4-l4 的 `Serializer`：确认这条文本确实是 `DefRegInit` / `Connect` 命令经 `Serializer.serializeCommand` 序列化的结果。

**预期结果**：你能用一句话说清——「Checks 守门、Elaborate 长电路、Emitter 把电路写成 `.fir` 文件、Convert 把电路翻成 FIRRTL IR 交给后续 CIRCT」，并能在源码中为每一步指到具体文件与行号。

## 6. 本讲小结

- **Convert**（[Convert.scala:24-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala#L24-L32)）是个薄包装：读 `ChiselCircuitAnnotation`，调用 `Converter.convert(a.elaboratedCircuit._circuit)`，产出 `FirrtlCircuitAnnotation`；自 7.11.0 起 deprecated。
- **Checks**（[Checks.scala:23-58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Checks.scala#L23-L58)）是只读校验型 Phase，检查 `PrintFullStackTraceAnnotation` / `ChiselOutputFileAnnotation` 不重复、`RemapLayer` 不冲突；靠 `optionalPrerequisiteOf = Dependency[Elaborate]` 自动插队到 Elaborate 之前。
- **Emitter**（[Emitter.scala:31-44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Emitter.scala#L31-L44)）在指定了输出文件时，构造一个 CSA 并**立即** `doWriteToFile` 写盘；`flatMap` 结果被丢弃，所以它的全部效果是磁盘上的 `.fir` 文件，对后续 Phase 不可见。
- **CircuitSerializationAnnotation**（[ChiselAnnotations.scala:361-427](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L361-L427)）是「电路 + 文件名 + 格式」的序列化载体，混入 `BufferedCustomFileEmission`，通过惰性的 `emitLazily` → `ElaboratedCircuit.lazilySerialize` → `Serializer` 把内部 IR 流式写成 CHIRRTL 文本。
- 三个 Phase 都是 `AnnotationSeq => AnnotationSeq` 的纯变换（Emitter 额外带写盘副作用），靠 `prerequisites` / `optionalPrerequisiteOf` 被 `PhaseManager` 自动调度。
- 现代发射已偏向 `AddSerializationAnnotations`（把 CSA 加进序列让下游写）与 `ElaboratedCircuit` 直连 CIRCT，本讲的 Convert/Emitter 属于仍在运行但逐步淡出的旧路径。

## 7. 下一步学习建议

- **u5-l4（CIRCT 集成：调用 firtool）**：本讲到 Convert 产出 `FirrtlCircuitAnnotation` 为止；下一讲接续这条管道的最后一棒——`circt.stage.phases.CIRCT` 如何调用 LLVM 的 firtool 把（CHIRRTL/）FIRRTL 编译成最终 SystemVerilog。
- **重读 u4-l5（Converter）**：本讲只点了 `Converter.convert(circuit)` 的入口；要彻底看懂「Chisel IR 怎么逐字段映射成 FIRRTL IR」，建议回看 u4-l5 对 `convertCommand` / `extractType` 的逐行讲解。
- **阅读 `AddSerializationAnnotations`**：[AddSerializationAnnotations.scala:15-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddSerializationAnnotations.scala#L15-L32) 是与 Emitter 对照的现代写法，理解它如何把 CSA 加进序列、交给 `CustomFileEmission` 落盘，能帮你掌握新旧两条发射路径的全貌。
