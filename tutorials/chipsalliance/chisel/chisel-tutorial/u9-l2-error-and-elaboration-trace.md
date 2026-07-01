# 错误处理与 ElaborationTrace

## 1. 本讲目标

Chisel 在 elaboration（细化）期间会运行大量用户写下的 Scala 构造代码。这些代码随时可能出错：方向冲突、位宽不匹配、重复绑定……如果每错一处就立刻抛异常，用户只能一次看到一个问题、改完再跑，体验极差。

本讲要回答三个问题：

1. Chisel 如何**收集**错误——为什么一次运行能同时报出多条错误，并给每条错误附上「文件:行:列」和源码原文？
2. Chisel 如何**分级**警告——警告与错误如何相互转换，如何用过滤器按 ID/来源抑制或升级？
3. `ElaborationTrace` 到底是什么——它与「错误定位」是什么关系？

学完后你应当能：读懂 `ErrorLog` 的聚合与汇报流程；看懂 `WarningID`/`WarningFilter` 的过滤 DSL；正确理解 `ElaborationTrace` 的火焰图计时用途；并知道「定位用户代码」的真正代码在哪。

> **依赖说明**：本讲建立在 [u4-l1 Builder 全局状态机](u4-l1-builder-global-state.md) 之上——`Builder` 维护的 `DynamicContext` 里就挂着本讲的主角 `errors: ErrorLog`。涉及源信息（`SourceLine`）的隐式注入机制可参考 u7-l2（SourceInfo 宏），本讲只把它当成「已带好 file:line:col 的隐式参数」使用。

## 2. 前置知识

- **elaboration（细化）**：运行你写的 Scala 构造体、「长出」电路的过程（见 u1-l5、u4-l1）。错误处理几乎都发生在这一阶段。
- **`Builder` 全局状态机**：elaboration 期间的单例门面，所有 `Builder.error`/`Builder.warning` 都转发到当前 `DynamicContext.errors`（见 u4-l1）。
- **`SourceInfo` / `SourceLine`**：由编译器宏在每个 Chisel API 调用处隐式注入的源信息对象，携带 `filename`/`line`/`col`。本讲不重复其注入原理，只消费它的 `file:line:col`。
- **Java 栈轨迹（stack trace）**：`Throwable.getStackTrace` 返回的 `StackTraceElement[]`，每一帧形如 `className.method(file:line)`。本讲的「裁剪」就是对这个数组动手。
- **`ChiselException`**：Chisel 自定义的根异常，位于 [core/src/main/scala/chisel3/package.scala:345-345](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L345-L345)，是绝大多数 Chisel 运行期错误的载体。

> **一个必须先澄清的命名误区**：本讲标题里的「ElaborationTrace」与「堆栈追踪裁剪」是**两件事**。源码里的 `ElaborationTrace` 类是一个**性能计时器**（产出火焰图数据），它**不**负责在错误信息里定位用户代码。真正做「错误定位」的是 `Error.scala` 里的 `trimStackTraceToUserCode`（裁剪异常栈）和 `getErrorLineInFile`（抓取源码行）。本讲会分别讲清二者，并在 4.3 节专门点明这个区别，以免你被名字误导。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [core/src/main/scala/chisel3/internal/Error.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala) | **本讲核心**。错误/警告的聚合器 `ErrorLog`、警告过滤器 `WarningFilter`、汇报入口 `checkpoint`、异常栈裁剪 `trimStackTraceToUserCode`、源码行抓取 `getErrorLineInFile`，全在这里。 |
| [core/src/main/scala/chisel3/internal/Warning.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Warning.scala) | 警告的 ID 枚举 `WarningID`（只增不删）与 `Warning` 样例类。 |
| [core/src/main/scala/chisel3/internal/ElaborationTrace.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala) | **性能计时器**。用 `pushModule`/`popModule` 测量每个模块的细化耗时，写成火焰图格式文件。 |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | `Builder.error`/`warning`/`deprecated` 转发到 `ErrorLog`；`buildImpl` 收尾调用 `errors.checkpoint`。 |
| [src/main/scala/chisel3/stage/phases/Elaborate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala) | Phase 管道里调用 `Builder.build` 的阶段；异常在这里被 `trimStackTraceToUserCode` 裁剪。 |
| [core/src/main/scala/chisel3/Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) | 模块构造前后调用 `elaborationTrace.pushModule`/`popModule`，构成计时区间。 |
| [src/main/scala/chisel3/stage/ChiselAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala) | 把 `--throw-on-first-error`/`--full-stacktrace`/`--warnings-as-errors`/`--warn-conf` 等 CLI 选项变成注解。 |

## 4. 核心概念与源码讲解

### 4.1 Error：错误收集与聚合（含「定位用户代码」的真正实现）

#### 4.1.1 概念说明

很多编译器遇到第一个错误就停。Chisel 选择另一条路：**先收集，后汇报**。elaboration 期间，`MonoConnect`/`BiConnect`、位宽检查等发现的问题不是立刻 `throw`，而是调用 `Builder.error(msg)` 把错误塞进一个集合；等整个电路细化完，`Builder.buildImpl` 收尾时调用 `errors.checkpoint(logger)` 一次性把所有错误打印出来，再抛出**一个**汇总异常。

这样做的好处显而易见：你写错三处，一次运行就能看到三条错误及各自位置，改一轮就能推进。

这套机制有三个关键角色：

- **`ErrorLog`**：聚合器，持有一个去重集合 `errors` 和一个去重映射 `deprecations`。
- **`ErrorEntry`**：一条错误的载体，含若干行文本（消息 + 源码行 + caret）和一个 `isFatal` 标志。
- **`Errors`**：汇报结束、确认存在致命错误时抛出的汇总异常。

此外，本模块还顺带讲清「错误信息里的用户代码位置从哪来」——它由两条路径提供：

- **收集型错误**：位置来自隐式 `SourceInfo`（`SourceLine`），源码原文由 `getErrorLineInFile` 从磁盘读出。
- **抛出型异常**：Java 栈轨迹由 `trimStackTraceToUserCode` 裁剪掉 chisel3/scala/java 等框架帧，只留用户代码。

#### 4.1.2 核心流程

一条 `Builder.error("...")` 调用的完整旅程：

```
Builder.error(msg)(implicit sourceInfo)        // 用户层入口
  └─ 若当前在 DynamicContext 内 → errors.error(msg, sourceInfo)
  │     └─ ErrorLog.logWarningOrError(msg, Some(si), isFatal=true)
  │           ├─ location     = sourceInfo.serialize  → "文件:行:列"
  │           ├─ 源码行+caret = getErrorLineInFile(sourceRoots, sl)  → ["源码原文", "   ^"]
  │           ├─ entry        = ErrorEntry(消息::源码行, isFatal=true)
  │           ├─ 若 throwOnFirstError && isFatal → 立刻 throwException(entry)
  │           └─ 否则 errors += entry   （LinkedHashSet 自动去重）
  └─ 若不在 DynamicContext 内 → throwException(msg)   （无处可存，直接抛）

……整个电路细化完成……

Builder.buildImpl 收尾：
  errors.checkpoint(logger)
    ├─ 逐条 logger.error(entry.serialize)   // 把消息+源码行+caret 打到日志
    ├─ 统计 allErrors / allWarnings 数量并打印汇总行
    └─ 若 allErrors 非空 → throw new Errors("Fatal errors ...")   // 一个汇总异常
```

异常栈裁剪则发生在**更外层**的 `Elaborate` 阶段：

```
Elaborate.transform:
  try Builder.build(...)            // 内部已 checkpoint、可能抛 Errors/ChiselException
  catch NonFatal(a):
    if (!printFullStackTrace) a.trimStackTraceToUserCode()   // 裁剪栈
    throw a
```

裁剪算法（`trimStackTraceToUserCode`）对一个 `StackTraceElement[]` 做六步变换：

1. 记录「顶部是否在裁剪名单内」`droppedFromTop`；
2. 从顶部 `dropWhile` 删掉所有根包属于 `{chisel3, scala, java, jdk, sun, sbt}` 的帧；
3. 反转，从底部 `dropWhile` 直到命中锚点类（默认是 `Builder` 的类名）；
4. 从锚点处再 `dropWhile` 删掉裁剪名单内的帧；
5. 反转回原序；按需在头部/尾部插入省略号帧 `..` 与「Stack trace trimmed ...」提示；
6. 用 `throwable.setStackTrace(...)` 原地改写异常的栈。

#### 4.1.3 源码精读

**① 聚合器与去重容器** —— `ErrorLog` 持有的两个可变集合。注意用的是 `LinkedHashSet`（保留插入顺序 + 自动去重）和 `LinkedHashMap`：

[core/src/main/scala/chisel3/internal/Error.scala:413-414](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L413-L414) —— 这是「同一条错误不重复打印」与「按出现顺序打印」的底层保障。

**② 错误/警告的统一落盘逻辑** —— `logWarningOrError` 把消息、位置、源码行组装成 `ErrorEntry`：

[core/src/main/scala/chisel3/internal/Error.scala:300-311](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L300-L311) —— 关键三处：`errorLocationString` 拿到 `文件:行:列`；`getErrorLineInFile` 读出**源码原文和 caret 行**；`throwOnFirstError && isFatal` 时立刻抛、否则 `errors += entry` 累积。

**③ 抓取源码原文 + caret** —— 这是「错误信息里为什么能看到我写的那行代码」的答案：

[core/src/main/scala/chisel3/internal/Error.scala:32-57](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L32-L57) —— 它按 `sourceRoots` 在磁盘上找到源文件，读到第 `sl.line` 行，再生成一串空格 + `^`（列号对齐）作为指示符。读不到文件就返回空（`NonFatal` 兜底），不会让报错本身崩掉。

**④ 汇报入口 `checkpoint`** —— 打印全部累积项，必要时抛汇总异常：

[core/src/main/scala/chisel3/internal/Error.scala:345-411](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L345-L411) —— 末尾 `if (!allErrors.isEmpty) throw new Errors(...)`：把「致命错误数」与「警告数」分别染色打印，存在致命错误才抛异常，否则 `errors.clear()` 清空已汇报的警告。

**⑤ 汇总异常 `Errors`**：

[core/src/main/scala/chisel3/internal/Error.scala:239-244](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L239-L244) —— `Errors` 混入 `NoStackTrace`，因为真正的栈意义不大（错误已逐条列出）；`throwException` 则是无 context 时的「立即抛」兜底。

**⑥ 用户层入口** —— `Builder` 把调用转发给 `ErrorLog`：

[core/src/main/scala/chisel3/internal/Builder.scala:938-951](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L938-L951) —— 注意 `error` 的分支：在 `DynamicContext` 内走收集，不在则 `throwException`（解释了为何脱离 elaboration 上下文时报错会直接抛）。

**⑦ 收尾触发汇报** —— `buildImpl` 在正常结束和异常两条路径上都调用 `checkpoint`：

[core/src/main/scala/chisel3/internal/Builder.scala:1086-1093](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1086-L1093) —— 即便构造体抛了异常，`catch` 分支也先 `checkpoint` 把已收集的错误吐出来再 `throw e`，避免「异常吞掉了未汇报的错误」。

**⑧ 异常栈裁剪的真正位置** —— 在 `Elaborate` 阶段，而非 `ElaborationTrace`：

[src/main/scala/chisel3/stage/phases/Elaborate.scala:78-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L78-L86) —— 只有 `!printFullStackTrace`（即未加 `--full-stacktrace`）时才裁剪。

**⑨ 裁剪算法本体** —— `trimStackTraceToUserCode` 的六步变换，以及裁剪名单与锚点：

[core/src/main/scala/chisel3/internal/Error.scala:75-115](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L75-L115)，名单见 [core/src/main/scala/chisel3/internal/Error.scala:21-30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L21-L30) —— `packageTrimlist` 就是 `{chisel3, scala, java, jdk, sun, sbt}`，锚点默认是 `Builder` 的类名。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 Chisel「一次收集多条错误」，并验证 `--throw-on-first-error` 改变行为；再定位到「定位用户代码」的真正源码位置。

**操作步骤**（示例代码，非项目原有代码）：

1. 写一个故意带两处方向冲突的模块：

```scala
// 示例代码：BadConnect.sc 或你自己的 sbt/mill 工程里
import chisel3._

class BadConnect extends Module {
  val io = IO(new Bundle {
    val a = Output(UInt(8.W))
    val b = Output(UInt(8.W))
    val c = Output(UInt(8.W))
  })
  io.a := io.b   // 两个 Output 互连，方向冲突（错误 1）
  io.b := io.c   // 同样的方向冲突（错误 2）
}

object BadDemo extends App {
  println(chisel3.stage.ChiselStage.emitSystemVerilog(new BadConnect))
}
```

2. 运行 `BadDemo`（或 `./mill` 对应工程里的 `runMain`）。

**需要观察的现象 / 预期结果**（部分细节**待本地验证**，取决于版本与连线算法的报错文案）：

- 终端会**一次性**打印**两条** `[error]`，每条都带形如 `文件:行:列` 的位置，其后紧跟**你写的那行源码原文**和一列对齐的 `^` caret。
- 最后抛出形如 `Fatal errors during hardware elaboration. Look above for error list.` 的异常（`Errors`）。
- 在 [core/src/main/scala/chisel3/internal/Error.scala:32-57](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L32-L57) 中能找到读出「源码原文 + caret」的逻辑——**这才是「错误定位」的真正实现**。

3. 加上「首错即抛」选项重跑：

```scala
println(
  chisel3.stage.ChiselStage.emitSystemVerilog(
    new BadConnect,
    args = Array("--throw-on-first-error")
  )
)
```

**预期**：这次只看到**第一条**错误，并且它带着一个**真实的 Java 栈轨迹**（因为是在 `logWarningOrError` 里 `throwException` 立即抛出的，没有走聚合）。对比无该选项时的「两条 + 汇总异常」，体会「收集 vs 立即抛」的差异。

4. 切换 `--full-stacktrace`：观察栈轨迹从「裁剪后只含用户代码 + 省略号」变成「完整原始栈」，对应 [src/main/scala/chisel3/stage/phases/Elaborate.scala:82-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L82-L84) 的分支。

> 说明：原始任务描述建议「在 `ElaborationTrace.scala` 中找到定位逻辑」，但源码事实是——错误定位逻辑在 **`Error.scala`**（`getErrorLineInFile` + `trimStackTraceToUserCode`），`ElaborationTrace.scala` 只做计时。本实践据此修正了查找位置。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ErrorLog.errors` 用 `LinkedHashSet` 而不是 `List` 或普通 `Set`？

> **答案**：`Set` 会去重但不保证顺序；`List` 保序但不去重；`LinkedHashSet` 两者兼顾——既能把「完全相同的错误」合并成一条，又按首次出现顺序打印，输出稳定可读。

**练习 2**：把 `--throw-on-first-error` 打开后，为什么错误信息反而带上了完整栈轨迹？

> **答案**：该选项让 `logWarningOrError` 在第一条致命错误处直接 `throwException(entry.serialize(...))`，抛出的是普通 `ChiselException`，未经 `checkpoint` 汇总，栈轨迹因此保留并指向出错现场；提示语「Rerun with --throw-on-first-error if you wish to see a stack trace」正是为此设计。

**练习 3**：如果某次报错只显示 `(unknown)` 而没有 `文件:行:列`，最可能的原因是什么？

> **答案**：该调用点的隐式 `SourceInfo` 是 `NoSourceInfo`（而非 `SourceLine`），`errorLocationString` 对 `NoSourceInfo` 返回 `(unknown)`。通常是该 API 未经过 SourceInfo 宏包装，或上下文里没有可注入的源信息。

---

### 4.2 Warning：警告分类与过滤

#### 4.2.1 概念说明

警告比错误「软」：它不阻断 elaboration，但提示潜在问题（如把 `UInt` 强转成枚举、动态位选择越界等）。Chisel 给每类警告分配一个**稳定整数 ID**，并提供一套**过滤器 DSL**，让你按 ID 或来源文件，把某类警告**抑制**（Suppress，彻底不打印）、**保留为警告**（Warn，默认）或**升级为致命错误**（Error）。

这样在 CI 里你可以「把所有警告当错误」强制零警告，也可以「只对某个历史文件临时抑制」。

三个角色：

- **`WarningID`**：枚举，ID 只增不删（保证向后兼容）。
- **`Warning`**：样例类，含 `SourceInfo`、`id`、`msg`；`apply` 会自动在消息前贴上 `[W003]` 标签。
- **`WarningFilter`**：过滤器，`applies(warning)` 判定是否命中，`action` 决定处理方式。

#### 4.2.2 核心流程

```
Builder.warning(warning)                 // 或库代码里构造 Warning(id, msg)
  └─ ErrorLog.warning(warning)
        ├─ action = warningFilters.collectFirst { 命中者 }.action
        │            .getOrElse(Warn)     // 默认 Warn
        ├─ action match:
        │    Error    → isFatal=true  → 走 logWarningOrError 当致命错误
        │    Warn     → isFatal=false → 当警告
        │    Suppress → None          → 直接丢弃，什么都不做
        └─ doReport.foreach(logWarningOrError(...))
```

过滤器 DSL 的语法（`WarningFilter.parse`）：

```
<filter>[&<filter>...]:<action>

<filter> ::= any | src=<glob> | id=<整数>
<action> ::= :e (Error) | :w (Warn) | :s (Suppress)
```

例子：`any:e`（全部升级为错误，等价 `--warnings-as-errors`）、`id=3:s`（抑制 3 号警告）、`src=Foo.scala:w`（对 `Foo.scala` 的警告保持警告）、`id=3&src=Foo.scala:e`（3 号且来自 `Foo.scala` 时升级为错误）。

ID 必须落在 \([1, \text{maxId}-1]\) 区间（`maxId` 比真实最大 ID 大 1），且只接受纯数字，避免 `+`/`-` 符号歧义。

#### 4.2.3 源码精读

**① 警告 ID 枚举（只增不删）** —— 顶部注释明令「永不删除、只许末尾追加」，因为 ID 是用户过滤器里的稳定契约：

[core/src/main/scala/chisel3/internal/Warning.scala:7-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Warning.scala#L7-L24) —— `NoID = Value(0)` 保留位，1–8 是真实警告（如 `UnsafeUIntCastToEnum`、`DynamicIndexTooWide`）。

**② Warning 构造时自动贴标签**：

[core/src/main/scala/chisel3/internal/Warning.scala:28-37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Warning.scala#L28-L37) —— `f"[W${id.id}%03d] "` 把 ID 格式化成三位补零前缀，这就是你看到的 `[W003]`；`noInfo` 在拿不到隐式 `SourceInfo` 时从栈轨迹现造一个。

**③ 过滤器匹配逻辑** —— `applies` 同时考虑 ID 与来源 glob，`None` 表示「通配」：

[core/src/main/scala/chisel3/internal/Error.scala:124-146](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L124-L146) —— `this.id.forall(_ == warning.id)`：过滤器没指定 id 就视为匹配；`src` 用 `PathMatcher.matches` 做 glob 匹配。

**④ 三种 Action 与默认值**：

[core/src/main/scala/chisel3/internal/Error.scala:148-151](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L148-L151) —— `Suppress`/`Warn`/`Error`。

**⑤ 应用过滤器并决定动作** —— `ErrorLog.warning`：

[core/src/main/scala/chisel3/internal/Error.scala:319-331](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L319-L331) —— `collectFirst` 取**第一个**命中过滤器的 action，没命中则默认 `Warn`；`Suppress` 映射为 `None` 直接跳过。

**⑥ DSL 解析器** —— `WarningFilter.parse`：

[core/src/main/scala/chisel3/internal/Error.scala:165-236](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L165-L236) —— 用 `lastIndexOf(':')` 切出动作后缀，`&` 切多过滤项；任何语法错误都返回 `Left((列号, 提示))`，用于在上层拼出带 caret 的解析错误。

**⑦ CLI 选项到注解** —— `--warnings-as-errors`、`--warn-conf`：

[src/main/scala/chisel3/stage/ChiselAnnotations.scala:69-129](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L69-L129) —— `WarningsAsErrorsAnnotation.asFilter` 直接产出 `WarningFilter(None, None, Error)`（等价 `any:e`）；`WarningConfigurationAnnotation(value)` 在构造期就急切解析字符串（`--warn-conf`），解析失败立刻抛带 caret 的错误。

#### 4.2.4 代码实践

**实践目标**：触发一个已知 ID 的警告，分别用「升级为错误」「抑制」两种过滤器观察输出差异。

**操作步骤**：

1. 制造一个会发警告的小电路。例如利用「动态索引过宽」类警告（具体哪条警告会被触发**待本地验证**，可在 [Warning.scala:7-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Warning.scala#L7-L24) 中查 ID，假设为 `id=N`）：

```scala
// 示例代码
import chisel3._
class WarnDemo extends Module {
  val io = IO(new Bundle {
    val sel = Input(UInt(8.W))
    val out = Output(UInt(8.W))
  })
  val v = VecInit(0.U, 1.U, 2.U, 3.U)   // 4 个元素，仅需 2 位选择
  io.out := v(io.sel)                    // sel 8 位过宽 → 可能触发警告
}
```

2. 不加任何过滤运行 `emitSystemVerilog(new WarnDemo)`：观察默认的 `[warn] [W0xx]` 输出。

3. 用 `--warn-conf id=N:e` 重跑（把 `N` 换成你观察到的 ID）：警告应变成 `[error]`，且最终抛出 `Fatal errors...`。

4. 用 `--warn-conf id=N:s` 重跑：该警告**完全消失**，无任何输出。

**需要观察的现象**：同一处代码，三种过滤策略下分别给出「警告 / 错误（中断）/ 静默」三种结果，对应 [Error.scala:319-331](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L319-L331) 的三分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WarningID` 的注释要求「永不删除、只许末尾追加」？

> **答案**：ID 是用户配置（`--warn-conf id=3:s`）与 CI 脚本依赖的稳定契约。删除或重排会让历史配置命中错误的警告类别，甚至让 `id=3` 指向完全不同的含义。

**练习 2**：`WarningFilter.applies` 中为什么用 `this.id.forall(_ == warning.id)` 而不是 `this.id == Some(warning.id)`？

> **答案**：`forall` 在 `this.id` 为 `None`（过滤器未指定 id）时返回 `true`，即「不限定 id 就通配」；用 `==` 会让未指定 id 的过滤器只匹配 `Some(...)` 而漏掉通配语义。

**练习 3**：`--warnings-as-errors` 与 `--warn-conf any:e` 行为上有何区别？

> **答案**：功能上几乎等价——前者经 `WarningsAsErrorsAnnotation.asFilter` 产出 `WarningFilter(None, None, Error)`，正是 `any:e`。区别在前者是固定 CLI 开关、后者是通用 DSL（可叠加多个、可只针对特定 src 或 id）。

---

### 4.3 ElaborationTrace：elaboration 计时与火焰图

#### 4.3.1 概念说明

> **重要澄清**：`ElaborationTrace` **不是**错误定位机制，也**不参与**堆栈裁剪。它是一个**性能分析工具**：测量每个模块的细化耗时，并把结果写成火焰图（flamegraph）可消费的文本格式。把它放在本讲，是因为它的名字容易让人误以为它和「错误追踪（trace）」相关——实际上这里的 trace 指的是「**计时轨迹**」。

它的启用方式完全不是错误选项，而是一个环境变量/系统属性：

- 设 `CHISEL_TRACE_FILE=/path/trace.txt`（或 `-Dchisel.trace.file=...`）即开启；
- 不设则 `enabled = false`，`pushModule`/`popModule` 全是空操作，零开销。

产出文件可用 [flamegraph.pl](https://github.com/brendangregg/FlameGraph) 或 [inferno](https://github.com/jonhoo/inferno) 可视化，帮你找出「哪个模块细化最慢」。

#### 4.3.2 核心流程

```
Module._applyImpl 开头：Builder.elaborationTrace.pushModule()   // 记 startNanos、压栈
  ……细化模块体（可能递归细化子模块，形成嵌套 push/pop）……
Module._applyImpl 结尾：Builder.elaborationTrace.popModule(name) // 记 endNanos、moduleName、出栈

Elaborate 阶段收尾：elaborationTrace.finish()
  对每个 event：沿 parent 链回溯构造 "父;子;孙" 栈名，
  写出一行 "<栈名> <耗时微秒>"
```

耗时计算：每个事件 \(t\) 的微秒时长为

\[
\text{durationMicros}(t) = \frac{t.\text{endNanos} - t.\text{startNanos}}{1000}
\]

嵌套关系由 `TraceEvent.parent` 链表达（栈式记录），`finish` 时回溯拼接出火焰图所需的「调用栈」语义。

#### 4.3.3 源码精读

**① 事件节点** —— 一个事件 = 一段「模块细化」计时区间：

[core/src/main/scala/chisel3/internal/ElaborationTrace.scala:9-14](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala#L9-L14) —— `parent` 指向更外层模块的事件，构成嵌套；`moduleName`/`endNanos` 是 `var`，因为名字在 pop 时才确定。

**② 开关与启停** —— 靠环境变量惰性开启：

[core/src/main/scala/chisel3/internal/ElaborationTrace.scala:40-61](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala#L40-L61) —— `enabled = traceFilePath.isDefined`；每个方法都以 `if (enabled)` 守卫，关闭时是纯 no-op。

**③ 写出火焰图格式** —— `finish`：

[core/src/main/scala/chisel3/internal/ElaborationTrace.scala:64-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala#L64-L81) —— 沿 `parent` 链用 `while` 回溯拼出 `父;子` 的栈名，再附上微秒耗时，正是火焰图输入行格式。

**④ 计时区间由模块生命周期触发**：

- 入口：[core/src/main/scala/chisel3/Module.scala:73-73](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L73-L73) —— `Builder.elaborationTrace.pushModule()`。
- 出口：[core/src/main/scala/chisel3/Module.scala:105-105](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L105-L105) —— `Builder.elaborationTrace.popModule(module.desiredName)`。

**⑤ 实例的创建与收尾** —— `Elaborate` 阶段：

[src/main/scala/chisel3/stage/phases/Elaborate.scala:44-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L44-L67) —— `new ElaborationTrace` 注入 `DynamicContext`，细化后 `elaborationTrace.finish()` 落盘。

**⑥ 挂载点** —— 作为 `DynamicContext` 的一个字段随 elaboration 全程传递：

[core/src/main/scala/chisel3/internal/Builder.scala:482-482](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L482-L482) 与访问器 [core/src/main/scala/chisel3/internal/Builder.scala:866-866](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L866-L866)。

#### 4.3.4 代码实践

**实践目标**：用火焰图找出一个层级化设计中「细化最慢的模块」。

**操作步骤**：

1. 准备一个有若干层子模块的设计（模块越多、嵌套越深，火焰图越有意义；具体设计**待本地验证**，可随便写一个会实例化多级子模块的顶层）。
2. 设环境变量后运行生成：

```bash
# 示例命令（具体 chisel 命令视你的工程而定）
CHISEL_TRACE_FILE=$PWD/trace.txt <你的 chisel 运行命令>
```

3. 查看产出的 `trace.txt`：每行形如 `Top;SubA;Leaf 123`（栈名 + 微秒）。
4. 可视化（需本机装相应工具，**待本地验证**）：

```bash
flamegraph.pl trace.txt > trace.svg   # 或 inferno-flamegraph trace.txt > trace.svg
```

**需要观察的现象**：`trace.txt` 中层级嵌套正确反映模块父子关系；宽度（耗时）最大的模块在火焰图里最宽，即为细化热点。注意：不设 `CHISEL_TRACE_FILE` 时 `trace.txt` 不会生成，`push/pop` 也无任何开销——可在 [ElaborationTrace.scala:42-42](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala#L42-L42) 确认 `enabled` 判定。

#### 4.3.5 小练习与答案

**练习 1**：不设 `CHISEL_TRACE_FILE` 时，`ElaborationTrace` 会拖慢 elaboration 吗？

> **答案**：不会。`enabled = traceFilePath.isDefined` 为 `false`，`pushModule`/`popModule`/内部逻辑全被 `if (enabled)` 短路成空操作，开销可忽略。

**练习 2**：为什么 `TraceEvent` 的 `moduleName` 和 `endNanos` 是 `var`？

> **答案**：事件在 `pushModule`（进入模块构造前）就创建，此时还不知道模块名与结束时刻——模块名要到 `popModule(module.desiredName)` 时才传入，结束时刻也要那时才记录，故必须是可变字段。

**练习 3**：本讲标题把 `ElaborationTrace` 与「堆栈追踪裁剪」并称，这与源码事实是否一致？

> **答案**：不一致。`ElaborationTrace` 只做细化**计时**与火焰图输出；堆栈裁剪是 `Error.scala` 的 `trimStackTraceToUserCode`，由 `Elaborate` 阶段调用。二者同名「trace」但含义不同（计时轨迹 vs 调用栈），切莫混淆。

## 5. 综合实践

把本讲三条主线串起来，做一次「错误诊断全流程」演练。

**任务**：写一个同时包含「两处致命错误」和「一处可升级警告」的模块，按下表依次实验并记录现象：

| 步骤 | 配置 | 预期（待本地验证细节） | 对应源码 |
|------|------|------------------------|----------|
| 1 | 无任何选项 | 一次打印 2 条 `[error]`（带 `文件:行:列` + 源码原文 + caret）+ 1 条 `[warn] [W0xx]`，最后抛 `Errors` | [Error.scala:345-411](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L345-L411) |
| 2 | `--throw-on-first-error` | 只见第 1 条错误 + 真实栈轨迹，立即中断 | [Error.scala:307-309](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L307-L309) |
| 3 | `--warn-conf id=N:e`（N=你那条警告的 ID） | 该警告升级为 `[error]`，错误总数变 3 | [Error.scala:319-331](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L319-L331) |
| 4 | `--warn-conf id=N:s` | 该警告彻底消失 | 同上 |
| 5 | `--full-stacktrace`（配合步骤 2） | 栈轨迹不再被裁剪，含 chisel3/scala 框架帧 | [Elaborate.scala:82-85](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L82-L85) 与 [Error.scala:75-115](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L75-L115) |
| 6 | `CHISEL_TRACE_FILE=trace.txt` | 额外得到火焰图数据文件（与错误无关，纯计时） | [ElaborationTrace.scala:64-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala#L64-L81) |

**收尾思考题**：步骤 1 里，为什么两处方向冲突能被**同时**报出，而不是改一处、跑一次、再发现第二处？请用「收集 → `checkpoint` 汇报」的两段式模型解释，并指出 `Builder.buildImpl` 在正常路径与异常路径上都会调用 `checkpoint` 的意义（[Builder.scala:1086-1093](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1086-L1093)）。

## 6. 本讲小结

- Chisel 对 elaboration 错误采用**先收集后汇报**：`Builder.error` 把错误存进 `ErrorLog` 的 `LinkedHashSet`（去重 + 保序），`buildImpl` 收尾用 `errors.checkpoint` 一次打印全部，再抛唯一一个 `Errors` 汇总异常。
- 错误信息里的「**用户代码位置**」有两条来源：收集型错误靠隐式 `SourceInfo`（`SourceLine`）+ `getErrorLineInFile` 读源码原文与 caret；抛出型异常靠 `trimStackTraceToUserCode` 裁掉 `{chisel3,scala,java,...}` 框架帧。**二者都在 `Error.scala`，不在 `ElaborationTrace.scala`**。
- `--throw-on-first-error` 把「收集」翻成「首错即抛」，从而获得真实栈轨迹；`--full-stacktrace` 关闭栈裁剪。
- 警告按**稳定整数 ID**（`WarningID`，只增不删）分类；`WarningFilter` 的 DSL（`any/src=/id= : e/w/s`）可对警告**抑制 / 保留 / 升级**，`--warnings-as-errors` 即 `any:e`。
- **`ElaborationTrace` 是性能计时器**，靠 `CHISEL_TRACE_FILE` 开启，用 `pushModule`/`popModule` 测各模块耗时并产出火焰图数据，与错误定位无关。
- 异常栈裁剪的调用点是 `Elaborate` 阶段（[Elaborate.scala:78-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L78-L86)），裁剪算法本体在 `Error.scala`。

## 7. 下一步学习建议

- **u9-l1 单元测试体系**：本讲的「两处错误一次报出」可以用 `FileCheck` 写成断言，把错误输出固定进回归测试；二者天然配合。
- **u7-l2 SourceInfo 宏**：想彻底搞懂「`文件:行:列` 是怎么在每个 API 调用处自动注入的」，就去看 SourceInfo 宏——本讲的 `SourceLine` 正是它的产物。
- **继续阅读** [core/src/main/scala/chisel3/internal/Error.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala) 全文，重点是把 `logWarningOrError` → `ErrorEntry` → `checkpoint` → `Errors` 这条链在脑子里跑通；再对照 [ChiselAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala) 理解每个 CLI 选项如何变成 `WarningFilter` 注解。
