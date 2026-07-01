# CIRCT 集成：调用 firtool

## 1. 本讲目标

在 [u5-l3](u5-l3-convert-checks-emit.md) 里，我们已经看到 Chisel 的内部 IR 经过 `Convert`/`Checks`/`Emitter` 几个 Phase 处理后，会被整理成一份 CHIRRTL 文本。但「CHIRRTL 文本」本身还不是 Verilog——真正把它编译成可综合 SystemVerilog 的，是 LLVM 的 CIRCT 项目里的 `firtool` 工具。

本讲就要回答最后一个关键问题：**Chisel 是怎么调用 firtool 的？** 学完后你应当能够：

- 说清楚 `circt.stage.phases.CIRCT` 这个 Phase 如何把 firtool 当作一个**外部进程**来调用（而不是把它当作库直接嵌入 JVM）。
- 解释 `CIRCTTarget` 与 `CIRCTTargetAnnotation` 如何决定 firtool 编译出的产物（Verilog、SystemVerilog、FIRRTL 方言、HW 方言、btor2 等）。
- 完整画出一条「`firtoolOpts` 参数 → `FirtoolOption` 注解 → `CIRCTOptions.firtoolOptions` → 命令行」的透传路径，并能定位每一处源码。
- 认识 `CIRCTConverter` / `CIRCTPassManager` 这两个名字，并知道它们在当前版本里**已经被弃用**（这是规格里容易踩坑的地方，本讲会专门澄清）。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **Phase 管道与 AnnotationSeq**（见 [u5-l1](u5-l1-stage-phase-pipeline.md)）：每个 Phase 是 `AnnotationSeq => AnnotationSeq` 的纯变换，配置都以注解形式在管道里流动。
- **内部 FIRRTL IR 与序列化**（见 [u4-l2](u4-l2-internal-firrtl-ir.md)、[u4-l4](u4-l4-ir-serializer.md)）：elaboration 长出的是 Scala 对象树，`ElaboratedCircuit.lazilySerialize` 把它变成 CHIRRTL 文本流。
- **firtool 是什么**：全称「MLIR-based FIRRTL Compiler（MFC）」，是 LLVM CIRCT 项目提供的命令行工具。它接收 FIRRTL/CHIRRTL 文本，经过一串 MLIR lowering pass，最终吐出 SystemVerilog（或 FIRRTL 方言、HW 方言、btor2 等）。可以把 firtool 类比成「硬件世界的 gcc」。
- **外部进程 vs 进程内调用**：调用一个外部命令行工具，本质是操作系统 fork 出一个子进程，通过标准输入（stdin）/标准输出（stdout）/标准错误（stderr）与之通信；而「进程内调用」是把对方作为库直接加载到自己进程里执行。这两种方式在错误处理、性能、部署方式上有很大差别，本讲的核心结论之一就是 Chisel 走的是**外部进程**路线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/circt/stage/phases/CIRCT.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala) | **本讲主角**。`CIRCT` Phase 的全部实现：序列化电路、解析 firtool 二进制、拼命令、跑子进程、回收产物。 |
| [src/main/scala/circt/stage/Annotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala) | 定义 `CIRCTTarget`、`CIRCTTargetAnnotation`、`FirtoolOption`、`FirtoolBinaryPath`、`PreserveAggregate` 等注解及其命令行选项。 |
| [src/main/scala/circt/stage/CIRCTOptions.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/CIRCTOptions.scala) | `CIRCTOptions` 配置类，把零散注解投影成一个结构化对象（`target`、`firtoolOptions`、`splitVerilog` 等）。 |
| [src/main/scala/circt/stage/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/package.scala) | `CIRCTOptionsView`：把 `AnnotationSeq` 折叠成 `CIRCTOptions` 的「投影器」。 |
| [src/main/scala/circt/stage/ChiselStage.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala) | 用户入口 `emitSystemVerilog`，把 `firtoolOpts` 数组转成 `FirtoolOption` 注解。 |
| [core/src/main/scala/chisel3/internal/CIRCTConverter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/CIRCTConverter.scala) | **已弃用**。历史上「进程内」访问 CIRCT 的访问者基类。 |
| [core/src/main/scala/chisel3/internal/CIRCTPassManager.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/CIRCTPassManager.scala) | **已弃用**。历史上进程内编排 CIRCT lowering pass 的抽象基类。 |

---

## 4. 核心概念与源码讲解

### 4.1 CIRCT Phase 总览：把 firtool 当外部进程调用

#### 4.1.1 概念说明

最容易产生的误解是：以为 Chisel 把 CIRCT「编译进」了自己的 jar 包，在 JVM 进程内直接调用 CIRCT 的 API 把电路编译成 Verilog。**这是旧版本的做法，当前版本不是这样。**

当前版本（参见本讲主角 `circt.stage.phases.CIRCT`）的做法是：

1. 把 elaboration 出来的电路序列化成一段 **CHIRRTL 文本**。
2. 在磁盘上找到（或下载）一个名为 `firtool` 的**可执行文件**。
3. 用操作系统的子进程机制把这个可执行文件**跑起来**，把 CHIRRTL 文本通过 **stdin** 喂给它。
4. 从子进程的 **stdout** 读回编译产物（SystemVerilog 文本），从 **stderr** 读回日志/错误。
5. 检查退出码：非 0 就抛出带详细信息的异常；为 0 就把产物包成一个注解（`EmittedVerilogCircuitAnnotation`）塞回 AnnotationSeq。

这种「外包给外部进程」的设计有几个直接后果，理解它们就理解了本讲的灵魂：

- **你必须机器上有 firtool**。没有 firtool，`emitSystemVerilog` 直接报错（见后文 `FirtoolNotFound`）。Chisel 用 `firtool-resolver` 库在首次运行时按版本号自动下载。
- **firtool 的版本要与 Chisel 匹配**。Chisel 编译时把一个目标 firtool 版本写进 `BuildInfo.firtoolVersion`，运行时按这个版本去解析二进制。版本不匹配通常能跑，但可能有兼容性问题。
- **firtool 的全部命令行选项原则上都能透传**。这就是后面 `firtoolOpts` 机制存在的根本原因——它本质上就是把字符串原样追加到子进程命令行末尾。

#### 4.1.2 核心流程

`CIRCT.transform` 的执行步骤（伪代码）：

```
输入: annotations (AnnotationSeq)
┌─ 1. view[CIRCTOptions]：把注解投影成结构化配置
├─ 2. 若 target == CHIRRTL → 直接 return（不需要跑 firtool）
├─ 3. 序列化: circuit.lazilySerialize(filteredAnnos) → CHIRRTL 文本流
├─ 4. 解析二进制:
│     若用户指定了 firtoolBinaryPath → 用它
│     否则 → firtoolresolver.Resolve(version) 自动解析/下载
├─ 5. 拼命令 cmd = [binary, "-format=fir", "-warn-on-...",
│                  ++ firtoolOptions, ++ targetOption, ...]
├─ 6. os.proc(cmd).call(stdin=CHIRRTL文本, stdout/stderr=缓冲)
├─ 7. 若 exitCode != 0 → 抛 FirtoolNonZeroExitCode
└─ 8. 按 target 把 stdout 包装成对应注解 (SystemVerilog→EmittedVerilogCircuitAnnotation)
输出: passthroughAnnotations ++ finalAnnotations
```

数据流向可视化：

```
 AnnotationSeq
      │  view[CIRCTOptions]
      ▼
 CIRCTOptions (target, firtoolOptions, splitVerilog, firtoolBinaryPath, ...)
      │
      ├──► circuit.lazilySerialize ──► CHIRRTL 文本 ──┐
      │                                                │ stdin
      └──► firtoolresolver.Resolve ──► firtool 路径 ──►│──► [firtool 子进程]
                                                        │      │
                                          stdout ◄──────┴──────┘
                                              │
                                              ▼
                                  EmittedVerilogCircuitAnnotation
                                              │
                                              ▼
                                     emitSystemVerilog 返回的 .sv 字符串
```

#### 4.1.3 源码精读

`CIRCT` 是一个标准的 Phase，声明了一个硬前置依赖（`AddImplicitOutputFile`），不撤销任何其它 Phase 的效果：

[src/main/scala/circt/stage/phases/CIRCT.scala:124-135](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L124-L135) —— `class CIRCT extends Phase` 的声明与依赖声明。注意类注释里写明它「calls and runs CIRCT, specifically `firtool`」。

进入 `transform` 后第一步就是把注解投影成结构化配置，并对 `CHIRRTL` 目标做**提前返回**——因为 CHIRRTL 文本就是 Chisel 自己序列化的产物，根本不需要 firtool 参与：

[src/main/scala/circt/stage/phases/CIRCT.scala:137-144](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L137-L144) —— `view[CIRCTOptions]` 投影配置；`target match { case Some(CIRCTTarget.CHIRRTL) => return annotations }` 说明只有目标在 CHIRRTL 之外时才会真正调用 firtool。这解释了为什么 [u5-l2](u5-l2-elaborate-phase.md) 里 `emitCHIRRTL` 不需要 firtool。

接着是序列化与二进制解析两段。序列化直接调用 [u4-l4](u4-l4-ir-serializer.md) 讲过的 `ElaboratedCircuit.lazilySerialize`（惰性流，支持超 2 GiB 电路）：

[src/main/scala/circt/stage/phases/CIRCT.scala:189-194](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L189-L194) —— 从 `chiselOptions.elaboratedCircuit` 取出电路，调用 `lazilySerialize(filteredAnnotations)` 得到 CHIRRTL 文本流。

[src/main/scala/circt/stage/phases/CIRCT.scala:213-223](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L213-L223) —— 二进制路径解析：优先用用户给的 `firtoolBinaryPath`，否则用 `firtoolresolver.Resolve(new LoggerShim(logger), version)` 按版本号自动解析；解析失败抛 `FirtoolNotFound`。这里的 `version` 来自 `chisel3.BuildInfo.firtoolVersion`。

最关键的「跑子进程」一行用的是 `os.proc(cmd).call(...)`（os-lib 库，即 [u1-l2](u1-l2-build-and-run.md) 提到的 `os-lib` 依赖）：

[src/main/scala/circt/stage/phases/CIRCT.scala:263-286](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L263-L286) —— 先 `logger.info` 打印完整命令（开日志时能看到 `Running CIRCT: ...`），用 `ByteArrayOutputStream` 收集 stdout/stderr，`stdin` 设为 CHIRRTL 文本流（`Left(it)` 分支）或管道（`Right` 文件分支），然后 `os.proc(cmd).call(check = false, ...)`。`check = false` 表示不要在非 0 退出码时自动抛异常——因为这里要自己读取 stdout/stderr 后再抛更友好的 `FirtoolNonZeroExitCode`。`exitValue != 0` 时抛出这个异常，异常消息用 `dramaticMessage` 包装了退出码、stdout、stderr，便于排查。

> 说明：`firtoolresolver.Resolve` 是 `org.chipsalliance::firtool-resolver` 这个依赖提供的（见 [build.mill:91](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L91)）。它会按版本号去下载对应平台的 `circt-full-shared-*.tar.gz`（下载 URL 模板见 [build.mill:106-107](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L106-L107)）并缓存。如果你已经手动装了 firtool，可以用 `--firtool-binary-path` 或环境变量直接指向它，跳过下载。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「Chisel 把 firtool 当子进程调用」这件事。

**操作步骤**：

1. 准备一个最简模块（示例代码）：

```scala
// 示例代码
import chisel3._
class Counter extends Module {
  val io = IO(new Bundle { val out = Output(UInt(8.W)) })
  val cnt = RegInit(0.U(8.W))
  cnt := cnt + 1.U
  io.out := cnt
}
```

2. 用 `sbt console` 或一个测试运行：
```scala
println(ChiselStage.emitSystemVerilog(new Counter))
```

3. 把日志级别调到 `Info` 以上，观察输出里是否有一行类似：
```
[info] Running CIRCT: '/path/to/firtool' -format=fir -warn-on-... ...
```

**需要观察的现象**：

- 控制台会出现 `Running CIRCT:` 开头的日志，后面跟着完整的 firtool 命令行——这就是 Chisel 实际执行的那条子进程命令。
- 如果机器上没有 firtool，会看到 `FirtoolNotFound` 异常（带 `dramaticMessage` 风格的错误框）。

**预期结果**：能看到 `Running CIRCT` 这条命令，且最终打印出 `module Counter(...)` 的 SystemVerilog。

**若无法确定运行结果**：具体的日志前缀与 firtool 路径依赖你本机的安装位置，属「待本地验证」；但 `Running CIRCT` 这行日志一定来自 [CIRCT.scala:263](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L263)，这点可在源码里直接确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `os.proc(cmd).call` 要传 `check = false`，而不是让 os-lib 在非 0 退出码时直接抛异常？

**参考答案**：因为需要在抛异常之前先收集好 stdout/stderr，并自己包装成带「退出码 + 完整输出 + 版本建议」的 `FirtoolNonZeroExitCode`，给用户更有用的诊断信息。若让 os-lib 默认抛异常，错误信息会丢失 stdout/stderr。

**练习 2**：`CIRCT` 这个 Phase 的 `invalidates(a: Phase) = false`（[CIRCT.scala:135](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L135)）是什么含义？

**参考答案**：表示这个 Phase 不会让任何其它 Phase 的效果「作废/需要重跑」。它只是读取电路、产出 Verilog 注解，不改写已有注解，因此不触发 PhaseManager 的重排（见 [u5-l1](u5-l1-stage-phase-pipeline.md) 的 `invalidates` 机制）。

---

### 4.2 CIRCTTarget 与 CIRCTTargetAnnotation：选择编译产物

#### 4.2.1 概念说明

firtool 是一个多功能编译器：同一份 CHIRRTL 输入，它可以输出不同「方言/语言」的产物。Chisel 用一个枚举 `CIRCTTarget` 来表达「我要 firtool 输出哪一种」，并用注解 `CIRCTTargetAnnotation` 把这个选择送进管道。

六种目标：

| 目标 | 含义 | 是否需要调用 firtool |
| --- | --- | --- |
| `CHIRRTL` | 规范化前的 FIRRTL 文本（Chisel 自己的序列化产物） | **否**，提前返回 |
| `FIRRTL` | FIRRTL MLIR 方言 | 是 |
| `HW` | CIRCT 的 HW 方言（更低层） | 是 |
| `Verilog` | Verilog 语言 | 是 |
| `SystemVerilog` | SystemVerilog 语言（**默认/最常用**） | 是 |
| `Btor2` | btor2 格式（用于有界模型检验） | 是 |

为什么 `CHIRRTL` 特殊？因为 Chisel 的前端 + Serializer 已经能直接产出 CHIRRTL 文本（见 [u4-l4](u4-l4-ir-serializer.md)），不需要 firtool 做任何 lowering。所以 `CIRCT` Phase 一看目标是 CHIRRTL 就直接 `return`（见 4.1.3 引用的第 141–144 行）。

#### 4.2.2 核心流程

`CIRCTTarget` 本身只是个标签。真正把它「翻译成 firtool 命令行参数」的是 `CIRCT.transform` 里一段 `match`。映射规则（精简）：

```
target            split   追加给 firtool 的选项
─────────────────────────────────────────────────────────
FIRRTL            false   "-ir-fir"
HW                false   "-ir-hw"
Verilog           true    "--split-verilog", "-o=<dir>"
Verilog           false   (无, 默认输出单文件 SV)
SystemVerilog     true    "--split-verilog", "-o=<dir>"
SystemVerilog     false   (无, 默认输出单文件 SV)
Btor2             false   "--btor2"
```

注意一个细节：`Verilog` 和 `SystemVerilog` 在不 split 时**不追加任何目标选项**——因为 firtool 默认就是输出单文件 SystemVerilog，所以「什么都不加」就是「要 SystemVerilog」。

子进程跑完后，还要按同样的 `target` 把 stdout 包成对应类型的注解：SystemVerilog/Verilog → `EmittedVerilogCircuitAnnotation`，FIRRTL/HW → `EmittedMLIR`，Btor2 → `EmittedBtor2CircuitAnnotation`。这就是 `ChiselStage.emitSystemVerilog` 最后能 `collectFirst { case EmittedVerilogCircuitAnnotation(a) => a }` 拿到结果的原因。

#### 4.2.3 源码精读

目标类型定义在一个密封 trait 里，穷尽了所有可能：

[src/main/scala/circt/stage/Annotations.scala:40-62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L40-L62) —— `object CIRCTTarget` 内定义 `sealed trait Type` 及 `CHIRRTL`/`FIRRTL`/`HW`/`Verilog`/`SystemVerilog`/`Btor2` 六个 case object。

`CIRCTTargetAnnotation` 把目标装进注解，并注册了命令行选项 `--target`：

[src/main/scala/circt/stage/Annotations.scala:64-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L64-L86) —— `case class CIRCTTargetAnnotation(target: CIRCTTarget.Type)`；`options` 把字符串 `chirrtl|firrtl|hw|verilog|systemverilog|btor2` 映射成对应注解，拼错会抛 `OptionsException`。命令行入口就是这里注册的，由 [Shell.scala:66](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Shell.scala#L66) 的 `CIRCTTargetAnnotation` 加进解析器。

「目标 → firtool 选项」的翻译在 CIRCT Phase 里：

[src/main/scala/circt/stage/phases/CIRCT.scala:241-261](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L241-L261) —— 这段 `(circtOptions.target, split) match { ... }` 就是上表的实现。注意 `Verilog`/`SystemVerilog` 在 `split=false` 时返回 `None`（不加任何目标选项），正是依赖 firtool 的默认行为。

产物包装（按目标分流）：

[src/main/scala/circt/stage/phases/CIRCT.scala:300-318](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L300-L318) —— `SystemVerilog` 分支产出 `EmittedVerilogCircuitAnnotation(EmittedVerilogCircuit(outputFileName, result, ".sv"))`，`FIRRTL`/`HW` 产出 `EmittedMLIR`，`Btor2` 产出 `EmittedBtor2CircuitAnnotation`。`result` 就是子进程的 stdout。

#### 4.2.4 代码实践

**实践目标**：用同一个模块，对比不同 `CIRCTTarget` 产出的差异。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._
import circt.stage._
class Adder extends Module {
  val io = IO(new Bundle {
    val a, b = Input(UInt(8.W))
    val out = Output(UInt(8.W))
  })
  io.out := io.a + io.b
}

// 1. SystemVerilog（最常用，跑完整 firtool）
println(ChiselStage.emitSystemVerilog(new Adder))

// 2. FIRRTL 方言（MLIR 文本，能看到 dialect 形态）
println(ChiselStage.emitFIRRTLDialect(new Adder))

// 3. HW 方言（更低层）
println(ChiselStage.emitHWDialect(new Adder))
```

这三个 `emit*` 方法定义在 [ChiselStage.scala:153-188](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L153-L188)，它们分别塞入 `CIRCTTargetAnnotation(FIRRTL)` / `(HW)`，其余流程完全相同。

**需要观察的现象**：

- `emitSystemVerilog` 输出 `module Adder(...)` 的可综合代码。
- `emitFIRRTLDialect` 输出形如 `firrtl.circuit ... firrtl.module ...` 的 MLIR 文本（带 `firrtl.` 前缀的方言操作）。
- `emitHWDialect` 输出 `hw.module ...` 形态，已经脱离 FIRRTL 方言。

**预期结果**：三种产物依次越来越「底层」，反映 firtool 内部 lowering 链 `CHIRRTL → FIRRTL 方言 → Low FIRRTL → HW 方言 → SV` 的不同停泊点。

**待本地验证**：各输出的确切文本依 firtool 版本而定；但「越往底层越脱离 FIRRTL 语法」这一趋势是确定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CIRCTTarget.Verilog` 与 `CIRCTTarget.SystemVerilog` 在 `split=false` 时不追加任何目标选项？

**参考答案**：firtool 默认产物就是单文件 SystemVerilog，所以「不加选项 = 要 SystemVerilog」。`Verilog` 在此场景下与 `SystemVerilog` 行为一致，故共用默认值。

**练习 2**：如果目标是 `CHIRRTL`，`CIRCT` Phase 还会启动 firtool 子进程吗？

**参考答案**：不会。[CIRCT.scala:141-144](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L141-L144) 一旦看到 `Some(CIRCTTarget.CHIRRTL)` 就直接 `return annotations`，根本走不到解析二进制、跑子进程那几步。

---

### 4.3 firtoolOpts、FirtoolOption 与 CIRCTOptions：选项透传机制

#### 4.3.1 概念说明

本模块对应规格里要求掌握的核心：**`firtoolOpts` 的传递路径**。

firtool 有上百个命令行选项（控制随机化、优化等级、聚合类型是否展开、是否输出调试信息等）。Chisel 不可能为每一个都造一个 Scala API，于是采用了一个非常通透的设计：

> 用户给一个字符串数组 `firtoolOpts`，Chisel 把每个字符串原封不动地追加到 firtool 子进程命令行末尾。

实现这个「原封不动透传」需要经过四道关卡，每道关卡都对应一段源码：

| 关卡 | 形态 | 位置 |
| --- | --- | --- |
| ① 用户 API | `Array[String]` 参数 | `ChiselStage.emitSystemVerilog` |
| ② 注解 | 每个字符串包成一个 `FirtoolOption` 注解 | `FirtoolOption` |
| ③ 结构化配置 | 投影成 `CIRCTOptions.firtoolOptions: Seq[String]` | `CIRCTOptionsView` |
| ④ 命令行 | 追加到子进程 `cmd` | `CIRCT.transform` |

`CIRCTOptions` 是一个普通的配置类，字段都是不可变值；`CIRCTOptionsView` 是把「一堆注解」折叠成「一个 `CIRCTOptions`」的投影器（`OptionsView` 模式，见 [u5-l1](u5-l1-stage-phase-pipeline.md)）。这一层抽象的好处是：`CIRCT` Phase 不直接遍历注解，而是先 `view` 出一个结构化对象再读字段，代码更清晰。

#### 4.3.2 核心流程

以用户写 `ChiselStage.emitSystemVerilog(gen, firtoolOpts = Array("-disable-all-randomization"))` 为例，完整链路：

```
emitSystemVerilog(firtoolOpts = Array("-disable-all-randomization"))
        │  ① firtoolOpts.map(FirtoolOption(_))
        ▼
Seq(FirtoolOption("-disable-all-randomization"))   ← 注解形式，进入 AnnotationSeq
        │  ② 穿过 Elaborate → Convert → ... → CIRCT 各 Phase（注解被原样携带）
        ▼
CIRCT.transform 里: view[CIRCTOptions](annotations)
        │  ③ CIRCTOptionsView: case FirtoolOption(a) => acc.copy(firtoolOptions = acc.firtoolOptions :+ a)
        ▼
CIRCTOptions(firtoolOptions = List("-disable-all-randomization"))
        │  ④ circtOptions.firtoolOptions ++ 进 cmd
        ▼
firtool ... -warn-on-unprocessed-annotations -disable-all-randomization ...
```

#### 4.3.3 源码精读

① 用户入口，注意它同时塞入了「目标 = SystemVerilog」的注解，并把 `firtoolOpts` 逐个包成 `FirtoolOption`：

[src/main/scala/circt/stage/ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213) —— `emitSystemVerilog` 的全部实现。关键两行：`CIRCTTargetAnnotation(CIRCTTarget.SystemVerilog)`（决定目标）和 `firtoolOpts.map(FirtoolOption(_))`（把字符串数组转注解）。最后 `collectFirst { case EmittedVerilogCircuitAnnotation(a) => a }.get.value` 取出 4.2 讲的产物注解里的 `.sv` 文本。

② `FirtoolOption` 注解定义（进程外传递的「信封」）：

[src/main/scala/circt/stage/Annotations.scala:118-135](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L118-L135) —— `case class FirtoolOption(option: String) extends NoTargetAnnotation with CIRCTOption`，命令行对应 `--firtool-option <option>`。混入 `CIRCTOption` 标记 trait 是为了下一步投影器能识别它。

③ 投影器把注解折叠成结构化字段：

[src/main/scala/circt/stage/package.scala:15-35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/package.scala#L15-L35) —— `CIRCTOptionsView`。其中 [package.scala:28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/package.scala#L28) 的 `case FirtoolOption(a) => acc.copy(firtoolOptions = acc.firtoolOptions :+ a)` 就是把每个 `FirtoolOption` 的字符串依次 `:+` 追加到 `firtoolOptions` 列表，**保持顺序、原样不改**。其它字段（`target`、`firtoolBinaryPath`、`splitVerilog`、`dumpFir`、`preserveAggregate`）也都在这里投影。

④ 命令行拼接：

[src/main/scala/circt/stage/phases/CIRCT.scala:225-230](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L225-L230) —— `cmd` 拼接的开头：`Seq(binary, input.fold(_ => "-format=fir", _.toString)) ++ Seq("-warn-on-unprocessed-annotations") ++ Seq("-output-annotation-file", ...) ++ circtOptions.firtoolOptions`。这里 `circtOptions.firtoolOptions` 就是用户给的字符串数组，被直接追加进命令。后面的 `logLevel.toCIRCTOptions`、`preserveAggregate`、`includeDirs`、`target` 选项也都拼在同一行 `++` 链里。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手用 `firtoolOpts` 改变 firtool 行为，并完整定位它的传递路径。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._
class RegOnly extends Module {
  val io = IO(new Bundle { val out = Output(UInt(8.W)) })
  val r = Reg(UInt(8.W))   // 未初始化、无复位值的寄存器
  r := r + 1.U
  io.out := r
}

// (A) 默认：firtool 会为未初始化寄存器生成随机化 initial 块
val svDefault = ChiselStage.emitSystemVerilog(new RegOnly)

// (B) 关闭全部随机化
val svNoRand = ChiselStage.emitSystemVerilog(
  new RegOnly,
  firtoolOpts = Array("-disable-all-randomization")
)

println("==== 默认 ===="); println(svDefault)
println("==== 关闭随机化 ===="); println(svNoRand)
```

**需要观察的现象**：

- 默认产物里通常能看到形如 `initial _RAND = ...` 或对未初始化寄存器赋随机值的 `initial` 块（firtool 默认为仿真做 X-propagation 随机化）。
- 加了 `-disable-all-randomization` 后，这些随机化 `initial` 块应当消失。

**预期结果**：两份 SystemVerilog 的差别，恰好是「有/无随机化 initial 块」。

**定位传递路径**（源码阅读型）：按 4.3.3 的四处链接，依次确认——
1. [ChiselStage.scala:205](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L205) 的 `firtoolOpts.map(FirtoolOption(_))`；
2. [Annotations.scala:122](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L122) 的 `case class FirtoolOption`；
3. [package.scala:28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/package.scala#L28) 的投影 `firtoolOptions :+ a`；
4. [CIRCT.scala:229](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L229) 的 `circtOptions.firtoolOptions` 追加进 `cmd`。

打开 Info 日志后，`Running CIRCT: ...` 那条命令里应当能看到 `-disable-all-randomization` 出现在末尾。

**待本地验证**：随机化 `initial` 块的确切写法依赖 firtool 版本；若你看到的默认产物没有随机化块，可能是默认选项被项目级配置覆盖，需结合本机环境确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `firtoolOpts` 用 `Seq[String]` 累积（`:+`）而不是只保留最后一个？

**参考答案**：因为用户可能传多个互不冲突的选项（如 `Array("-disable-all-randomization", "-emit-chisel-ir-omir")`），它们都要原样追加到命令行，所以必须保序累加成一个列表。

**练习 2**：如果用户传了一个 firtool 不认识的 `firtoolOpts` 字符串，会发生什么？

**参考答案**：因为命令行里有 `-warn-on-unprocessed-annotations` 且 Chisel 不校验这些字符串，firtool 自己会处理：要么报「unknown option」并以非 0 退出（触发 [CIRCT.scala:285-286](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L285-L286) 的 `FirtoolNonZeroExitCode`），要么对它认识的选项照常生效。校验责任在 firtool 一侧，Chisel 只负责透传。

---

### 4.4 CIRCTConverter 与 CIRCTPassManager：历史角色与弃用

#### 4.4.1 概念说明

规格里把 `CIRCTConverter` 和 `CIRCTPassManager` 列为最小模块，并描述「Chisel 通过 CIRCTConverter / PassManager 调用 firtool」。**这个描述对应的是旧版本（Chisel 5.x 及更早）的架构，在当前版本里已经被弃用。** 这是本讲必须澄清的关键事实，否则会与上一模块讲的「外部进程」结论矛盾。

历史上，Chisel 确实有过一条「**进程内**」调用 CIRCT 的路线：通过 JNI/Panama 绑定，把 CIRCT 的 C++ 库直接加载进 JVM，用一个 `CIRCTPassManager` 在内存里编排 lowering pass，再用一个 `CIRCTConverter`（访问者模式）遍历 Chisel 内部 IR 并喂给 CIRCT。这条路线的好处是无需 fork 子进程、数据不出 JVM。

但自 **Chisel 6.0** 起，这条进程内路线被废弃，两个抽象基类都被打上了 `@deprecated` 注解。取而代之的是本讲 4.1 讲的「**外部 firtool 子进程**」路线（由 `circt.stage.phases.CIRCT` 实现）。源码里的弃用注解直接给出了替代品名字：

```
@deprecated("There no CIRCTConverter anymore, use circtpanamaconverter directly", "Chisel 6.0")
```

> 「`circtpanamaconverter`」指的是基于 Project Panama（JDK 的原生互操作 API）实现的、进程内桥接 CIRCT 的另一条实验性路径，不在 `CIRCT` Phase 的默认调用链里。本讲只需知道它存在，并明白当前默认且推荐的 Verilog 生成路径是「外部 firtool 子进程」。

#### 4.4.2 核心流程

虽然 `CIRCTConverter`/`CIRCTPassManager` 已弃用，但它们记录的 lowering 阶段划分，正好就是 **firtool 子进程内部**执行的同一套 lowering 链。这套链可以理解为把高层 IR 一步步「降」到 Verilog：

```
CHIRRTL (Chisel 序列化产物)
   │  Lowering: CHIRRTL → Low FIRRTL
   ▼
Low FIRRTL
   │  Lowering: Low FIRRTL → HW 方言
   ▼
HW 方言
   │  Lowering: HW → SystemVerilog
   ▼
SystemVerilog 文本
```

`CIRCTPassManager` 的方法名就是这条链各段的命名（见下文源码）。换句话说：**旧版「进程内」和现在「外部 firtool」跑的是同一套 lowering 语义，只是执行载体从「JVM 内的 C++ 库」换成了「外部 firtool 可执行文件」。** 这也是为什么 `CIRCTTarget` 能产出 FIRRTL/HW/SV 等中间停泊点（4.2）——它们就是这条链上的中途产物。

#### 4.4.3 源码精读

`CIRCTConverter` 是一个抽象类，用访问者模式（一堆 `visitXxx` 方法）遍历 Chisel 内部 IR 节点。注意类头上的 `@deprecated`：

[core/src/main/scala/chisel3/internal/CIRCTConverter.scala:12-21](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/CIRCTConverter.scala#L12-L21) —— `@deprecated(... "Chisel 6.0")` 注解 + `abstract class CIRCTConverter`。它声明了 `mlirStream`/`firrtlStream`/`verilogStream` 等输出流，以及 `passManager()`、`visitCircuit`、`visitDefModule`、`visitConnect`、`visitDefReg` 等访问者方法。这些 `visit*` 方法覆盖了 [u4-l2](u4-l2-internal-firrtl-ir.md) 讲过的全部命令节点（`DefReg`/`DefPrim`/`Connect`/`When`/`DefSeqMemory` 等）。

`CIRCTPassManager` 同样被弃用，其方法名清晰对应 lowering 链各段：

[core/src/main/scala/chisel3/internal/CIRCTPassManager.scala:5-16](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/CIRCTPassManager.scala#L5-L16) —— `@deprecated(... "Chisel 6.0")` + `abstract class CIRCTPassManager`。方法 `populateCHIRRTLToLowFIRRTL()`、`populateLowFIRRTLToHW()`、`populateLowHWToSV()`、`populateExportVerilog(...)`、`run()` 正好对应 4.4.2 那条 lowering 链。

> 重要提示：在 `circt.stage.phases.CIRCT`（本讲 4.1 的主角）里**没有任何地方引用** `CIRCTConverter` 或 `CIRCTPassManager`。可以全局搜索确认——这两个类只是历史残留，保留是为了兼容老代码，默认 Verilog 生成路径完全不经过它们。

#### 4.4.4 代码实践

**实践目标**：确认这两个类已弃用、且不在当前主路径上。

**操作步骤**：

1. 用编辑器/IDE 打开 [CIRCTConverter.scala:12](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/CIRCTConverter.scala#L12) 与 [CIRCTPassManager.scala:5](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/CIRCTPassManager.scala#L5)，阅读 `@deprecated` 注解文本。
2. 在仓库内搜索 `CIRCTConverter` 的使用点（排除定义文件自身与测试）。可用只读命令：`git grep -n "new CIRCTConverter\|extends CIRCTConverter\|extends CIRCTPassManager"`。
3. 对照 [CIRCT.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala) 全文，确认它用的是 `os.proc(cmd).call`（子进程），而非这两个类。

**需要观察的现象**：

- `@deprecated` 注解出现在两个类的声明正上方。
- 主路径 `CIRCT.scala` 里搜不到对这两个类的引用。

**预期结果**：确认「`CIRCTConverter`/`CIRCTPassManager` 是历史遗留、已被弃用」，当前真正调用 firtool 的是 `circt.stage.phases.CIRCT` 的子进程机制。

#### 4.4.5 小练习与答案

**练习 1**：规格里说「Chisel 通过 CIRCTConverter / PassManager 调用 firtool」，这句话在当前版本是否准确？

**参考答案**：不准确。这两个类自 Chisel 6.0 起被 `@deprecated`，且 `circt.stage.phases.CIRCT` 主路径完全不引用它们。当前调用 firtool 的方式是「spawn 外部 `firtool` 子进程」。规格的描述反映的是旧版（≤5.x）架构。

**练习 2**：既然弃用了，为什么 `CIRCTPassManager` 的方法名（`CHIRRTLToLowFIRRTL` 等）仍然有助于理解本讲？

**参考答案**：因为这些方法名精确标注了 firtool 内部执行的 lowering 链各段（CHIRRTL → Low FIRRTL → HW → SV）。旧版「进程内」和现在「外部 firtool」跑的是同一套语义 lowering，方法名等于一份自文档化的 lowering 路线图，能帮助理解 4.2 里 `CIRCTTarget` 为何能停在 FIRRTL/HW/SV 等中间点。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个端到端的「调用链追踪」任务。

**任务**：写一个带未初始化寄存器的模块，分别用「默认」和「`firtoolOpts = Array("-disable-all-randomization")`」两种方式生成 SystemVerilog，然后**画出从用户代码到 firtool 子进程的完整数据流图，并在图上标注每个环节对应的源码文件与行号**。

**建议步骤**：

1. 编写模块（示例代码）：
   ```scala
   // 示例代码
   import chisel3._
   class Pipe extends Module {
     val io = IO(new Bundle {
       val in  = Input(UInt(8.W))
       val out = Output(UInt(8.W))
     })
     val r = RegNext(io.in)   // 一拍延迟寄存器，无复位
     io.out := r
   }
   ```

2. 运行：
   ```scala
   val a = ChiselStage.emitSystemVerilog(new Pipe)
   val b = ChiselStage.emitSystemVerilog(new Pipe, firtoolOpts = Array("-disable-all-randomization"))
   ```

3. 对照本讲，画一张包含以下节点与标注的图（参考 4.1.2 与 4.3.2 的两张图）：
   - `emitSystemVerilog` → [ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213)
   - `FirtoolOption` 注解 → [Annotations.scala:122](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L122)
   - `CIRCTOptionsView` 投影 → [package.scala:15-35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/package.scala#L15-L35)
   - `CIRCTTarget`/`CIRCTTargetAnnotation` → [Annotations.scala:40-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L40-L86)
   - 电路序列化 `lazilySerialize` → [CIRCT.scala:189-194](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L189-L194)
   - firtool 二进制解析 → [CIRCT.scala:213-223](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L213-L223)
   - 命令拼接与子进程执行 → [CIRCT.scala:225-286](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L225-L286)
   - 产物包装 → [CIRCT.scala:300-318](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L300-L318)

4. 在图上用红笔标出「`-disable-all-randomization` 这一字符串依次经过了哪四个关卡」（对应 4.3.1 的表）。

**验收标准**：你能不查资料，指着图上的每个箭头说出「这一步在哪份文件第几行做的、数据形态从什么变成了什么」。做到这一点，本讲就真正吃透了。

## 6. 本讲小结

- Chisel 当前通过 `circt.stage.phases.CIRCT` 把 **firtool 当作外部子进程**调用：序列化电路成 CHIRRTL 文本喂给 stdin，从 stdout 读回 Verilog，靠 `os.proc(cmd).call` 驱动，非 0 退出码抛 `FirtoolNonZeroExitCode`。
- `CIRCTTarget`（六种）与 `CIRCTTargetAnnotation` 决定 firtool 输出什么；`CHIRRTL` 目标会提前返回、根本不启动 firtool。
- `firtoolOpts` 的完整透传路径是四道关卡：`Array[String]` 参数 → `FirtoolOption` 注解 → `CIRCTOptionsView` 投影成 `CIRCTOptions.firtoolOptions: Seq[String]` → 追加进子进程命令 `cmd`。字符串原样不改、保序累积。
- `CIRCTOptions` 是结构化配置类，`CIRCTOptionsView` 是把注解折叠成它的投影器；`CIRCT` Phase 一律先 `view[CIRCTOptions]` 再读字段，不直接遍历注解。
- `CIRCTConverter` 与 `CIRCTPassManager` 自 **Chisel 6.0 起 `@deprecated`**，对应旧版「进程内」调用 CIRCT 的路线；当前主路径不引用它们。但其方法名仍精确记录了 firtool 内部的 lowering 链（CHIRRTL → Low FIRRTL → HW → SV）。
- firtool 二进制由 `firtool-resolver` 库按 `BuildInfo.firtoolVersion` 自动解析/下载，也可用 `--firtool-binary-path` 手动指定。

## 7. 下一步学习建议

到这里，**单元 5（Stage 与 CIRCT 集成）** 闭环了：你已经能完整解释「一行 `emitSystemVerilog` 如何变成 SystemVerilog 文本」的端到端链路——从前端 API、Builder、内部 IR、Serializer、Phase 管道，到本讲的 firtool 子进程调用。

接下来建议：

- **若关心仿真**：进入 [u8-l5 仿真与 svsim](u8-5-simulator-svsim.md)，看 `chisel3.simulator.Simulator` 如何在生成 Verilog 之后，再次驱动 Verilator/VCS 这类**仿真器**子进程跑测试——你会再次看到熟悉的「spawn 外部进程」模式。
- **若关心注解系统**：进入 [u9-l4 Annotation 注解系统](u9-4-annotation-system.md)，深入了解本讲反复出现的 `AnnotationSeq`/`NoTargetAnnotation`/`CIRCTOption` 背后的统一注解模型，学会自定义注解穿越整个管道。
- **若想验证理解**：用 `--dump-fir`（见 [CIRCT.scala:197-206](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L197-L206)）把中间 `.fir` 文件落盘，亲手对比「CHIRRTL 文本（喂给 firtool 的输入）」与「最终 SystemVerilog（firtool 的输出）」，把 lowering 链的每一步在文本层面看清楚。
- **延伸阅读**：CIRCT 官方文档（`circt.llvm.org`）关于 firtool 命令行选项与 FIRRTL lowering 的说明，能帮你把本讲的 `firtoolOpts` 透传机制和 firtool 自身能力对齐。
