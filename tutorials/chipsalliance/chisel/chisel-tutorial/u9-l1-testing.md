# 单元测试体系

## 1. 本讲目标

Chisel 既是「给用户写硬件的 DSL」，也是「一个正在被持续开发、自身需要被验证的编译器」。本讲聚焦**后者**——Chisel 是如何测试自己的。读完本讲，你应当能够：

1. 说清 `UnitTest` 这个标记 trait 的作用，以及 Chisel 如何在运行期**扫描 classpath 自动发现**所有带该标记的类/对象/模块。
2. 理解 `chisel3.UnitTests` 命令行工具如何把一堆 `UnitTest` 模块收拢成一份 CHIRRTL，再交给 `firtool` / `circt-test` 编译执行（与 u1-l2 的 `unitTest` mill 任务、u5-l4 的 CIRCT 集成衔接）。
3. 掌握 `FileCheck`：用 LLVM 同名工具对生成的 CHIRRTL/Verilog **文本**做断言，这是 Chisel 源码里最高频的「对照生成结果」测试手段。
4. 看懂 `chiseltest` 包对象：它是 Chisel 7 中为兼容旧 ChiselTest API 而保留的**桥接层**，最终委托给 `chisel3.simulator`（ChiselSim）。
5. 区分 Chisel 自身的三条测试脉络——内联单元测试（firtool+circt-test）、FileCheck 文本断言、ScalaTest 风格仿真测试——知道每条该在什么时候用。

---

## 2. 前置知识

本讲默认你已掌握以下内容（来自前置讲义）：

- **构建与命令**（u1-l2）：仓库用 mill 构建，`./mill chisel[].compile` / `chisel[].test` / `chisel[].unitTest` 是三条核心命令；`firtool` 与 `circt-test` 由 `object circt` 按 `etc/circt.json` 自动下载。
- **编译总览**（u1-l5）与 **CIRCT 集成**（u5-l4）：Chisel elaboration 产出内部 IR，经 `Converter` 与 `ChiselStage` 交给 CIRCT（firtool），最终产出 SystemVerilog 或被仿真。
- **模块与 elaboration**（u3-l1）：`Module` / `RawModule` 的构造体「只登记不施工」，真正固化在收口阶段。

本讲会用到几个新术语，先在此统一解释：

| 术语 | 含义 |
|------|------|
| **内联单元测试（inline unit test）** | 直接写在主源码（`src/main`）里、用 `UnitTest` 标记的小型自测模块，不需要单独的 `src/test` 工程。 |
| **classpath / runpath** | JVM 加载类的搜索路径；`classpath` 是默认的，`runpath` 是用户显式指定的覆盖路径。 |
| **ScalaTest** | Scala 生态最主流的测试框架，用 `AnyFlatSpec` + `it should "..." in { ... }` 组织用例。 |
| **DUT / testbench** | Device Under Test（被测设计）/ 测试平台（驱动 DUT、检查输出的外围电路）。 |
| **PeekPoke** | 「戳（poke，写激励）/ 瞄（peek，读响应）」式仿真 API，是 ChiselSim 暴露给用户的高层接口。 |
| **circt-test** | CIRCT 工具链里的一个可执行文件，能直接运行带 `clock/init/done/success` 约定接口的 MLIR 测试模块。 |

一个**关键认知**先放在前面：Chisel 的测试**不是单一框架**，而是三套各司其职的机制叠加。本讲的三个最小模块——`UnitTest`、`FileCheck`、`chiseltest` 包对象——分别对应这三套机制的核心入口。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/main/scala/chisel3/UnitTest.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTest.scala) | 定义 `UnitTest` 标记 trait、`DiscoverUnitTests` 扫描器、`AllUnitTests` 收集模块。**注意：文件在 `chisel3/` 目录下，但包名是 `chisel3.test`。** |
| [src/main/scala/chisel3/UnitTestMain.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTestMain.scala) | `object UnitTests` 命令行工具：解析参数、过滤、把所有 `UnitTest` 模块 elaboration 成一份 CHIRRTL。 |
| [build.mill](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill) | `unitTest` mill 任务：串起 `UnitTests` → `firtool` → `circt-test` 三步。 |
| [src/main/scala/chisel3/testing/FileCheck.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/FileCheck.scala) | `trait FileCheck`：把字符串交给 LLVM `FileCheck` 二进制做模式匹配断言。 |
| [src/main/scala/chisel3/testing/scalatest/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/scalatest/package.scala) | 给 ScalaTest 用的 `FileCheck` 混入 trait（同时提供测试目录）。 |
| [src/main/scala/chiseltest/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/package.scala) | `package object chiseltest`：ChiselTest 兼容层，提供 `poke/peek/expect` 等隐式类，委托给 `PeekPokeAPI`。 |
| [src/main/scala/chiseltest/ChiselScalatestTester.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/ChiselScalatestTester.scala) | `trait ChiselScalatestTester`：`test(new Mod){ dut => ... }` 入口，委托给 `ChiselSim`。 |
| [src/main/scala/chisel3/simulator/PeekPokeAPI.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala) | `PeekPokeAPI`：真正实现 `poke/peek/expect` 的类型分派层，chiseltest 兼容层与 ChiselSim 共用它。 |
| [src/main/scala/chisel3/simulator/EphemeralSimulator.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/EphemeralSimulator.scala) | `EphemeralSimulator`：最简仿真入口，内部即 `new ChiselSim{}`。 |
| [src/main/scala/chisel3/SimulationTestHarness.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/SimulationTestHarness.scala) | 定义 `clock/init/done/success` 固定 IO 约定，ChiselSim 与 circt-test 都靠它识别测试模块。 |

---

## 4. 核心概念与源码讲解

### 4.1 UnitTest：可被自动发现的内联测试标记

#### 4.1.1 概念说明

很多项目的测试都堆在一个独立的 `src/test` 目录里，和主源码分离。Chisel 除此以外，还提供了一种**内联单元测试**机制：你可以把一个「自测型」的小模块直接写在主源码（`src/main`）里，给它打上 `UnitTest` 标记，Chisel 就能在运行期自动把它找出来、和其他 `UnitTest` 一起 elaboration 成一份电路、统一交给 firtool/circt-test 跑。

这种机制特别适合**对照硬件行为本身**做穷尽式自检（例如「Gray 码编解码对所有 16 位输入是否可逆」），把测试逻辑和被测逻辑放在同一个文件里，避免分散。

`UnitTest` 本身**极其简单**——它只是一个空的标记 trait：

[src/main/scala/chisel3/UnitTest.scala:13-16](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTest.scala#L13-L16) —— 这是 trait 的全部定义，没有任何方法。

真正干活的是 `DiscoverUnitTests`：它在运行期遍历 classpath/runpath 上的所有 `.class` 文件，用反射判断哪些是 `UnitTest` 的子类型，然后逐个触发它们的构造。注意 **trait 是空的，所有「魔法」都在发现与构造阶段**，这和 Scala 的 `App` / `main` 约定、scalatest 的套件发现思路一脉相承。

> ⚠️ **一个常被踩的坑**：`UnitTest` 的源码文件路径是 `src/main/scala/chisel3/UnitTest.scala`，但它的**包名是 `chisel3.test`**（不是 `chisel3`）。所以引用时要写 `import chisel3.test.UnitTest` 或 `import chisel3.test._`。仓库里的真实用法（如 [src/test/scala/chisel3/util/GrayCode.scala:7](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chisel3/util/GrayCode.scala#L7)）正是 `import chisel3.test.UnitTest`。

#### 4.1.2 核心流程

把 `UnitTest` 从「写好的标记类」变成「跑起来的测试」，需要经过发现、过滤、收拢、编译、执行五个阶段。下面用伪代码描述整条链路（`-R` runpath、`-f` filter 等参数来自 `chisel3.UnitTests` 命令行）：

```
# 阶段 A：发现（DiscoverUnitTests）
for 每个 classpath/runpath 条目 entry:
    if entry 是 .jar:    枚举 jar 内所有 .class
    elif entry 是目录:   递归遍历目录下所有 .class
    对每个 className:
        clazz = loader.loadClass(className)        # 用自定义 URLClassLoader
        if UnitTest.isAssignableFrom(clazz):       # 是 UnitTest 子类型?
            if clazz 是 BaseModule:  回调 = () => Definition(new clazz)   # 模块要包成 Definition
            elif clazz 是单例(object): 回调 = () => 读取 MODULE$ 字段
            else:                    回调 = () => clazz.newInstance()     # 普通类调构造器
            handler(className, 回调)

# 阶段 B：过滤 + 收拢（UnitTests main，class AllUnitTests）
for 每个 (className, 回调):
    if filter 非空 且 无任一正则匹配 className: skip
    if exclude 中有正则匹配 className:           skip
    if list 模式: 打印 className 并 continue
    else:         回调()                          # 触发该测试模块的 elaboration

# 阶段 C：elaboration 成一份 CHIRRTL
chirrtl = ChiselStage.emitCHIRRTL(new AllUnitTests)   # AllUnitTests 是个 RawModule，
                                                       # 构造体里调 DiscoverUnitTests 触发所有测试模块
# 阶段 D：编译（firtool）
firtool --ir-hw unit_tests.fir -o unit_tests.mlir --default-layer-specialization=enable

# 阶段 E：执行（circt-test）
circt-test unit_tests.mlir
```

发现阶段的过滤判定可以写成一条布尔表达式。设 `F` 为 filter 正则集合、`X` 为 exclude 正则集合、`r ~ c` 表示「正则 r 能在类名 c 中匹配到」，则一个测试 `c` 会被生成（而非跳过）当且仅当：

\[
\text{generate}(c) \;=\; \bigl(|F|=0 \;\lor\; \exists r\in F,\; r\sim c\bigr) \;\land\; \neg\bigl(\exists r\in X,\; r\sim c\bigr)
\]

即「没有任何 filter 时全收；有 filter 时至少命中一条；且不命中任何 exclude」。

#### 4.1.3 源码精读

**① 标记 trait 与发现入口。** `UnitTest` trait 见上文。发现入口是 `DiscoverUnitTests.apply`：

[src/main/scala/chisel3/UnitTest.scala:34-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTest.scala#L34-L63) —— 决定扫描范围（runpath 非空用它，否则用 `java.class.path`），并构造一个禁用缓存的 `URLClassLoader`，再对每个文件调 `discoverFile`。

**② 扫描 JAR 与目录。** `discoverFile` 区分三种情况：

[src/main/scala/chisel3/UnitTest.scala:75-101](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTest.scala#L75-L101) —— JAR 文件用 `JarFile.entries` 枚举；目录递归 `visit`；其余文件忽略。二者最终都把 `.class` 路径经 `pathToClassName`（把 `/` 换成 `.`、去掉 `.class` 后缀）转成类名，交给 `discoverClass`。

**③ 反射判定与构造。** 这是「标记 trait 如何变成行动」的核心：

[src/main/scala/chisel3/UnitTest.scala:111-155](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTest.scala#L111-L155) —— 关键三步：(1) `loadClass`，捕获四种加载异常直接跳过；(2) `classOf[UnitTest].isAssignableFrom(clazz)` 判定是否子类型（并排除 `UnitTest` 自身）；(3) 若是 `BaseModule`，把构造器包成 `Definition(...)`，因为模块必须经 elaboration 才能落地。对单例对象，通过反射读取 `MODULE$` 字段保证其被构造。

**④ 收集模块 `AllUnitTests`。** 它把「发现并构造所有测试」包装成一个 `RawModule`：

[src/main/scala/chisel3/UnitTest.scala:163-165](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTest.scala#L163-L165) —— 构造体里调 `DiscoverUnitTests((_, gen) => gen(), Seq())`，即对每个发现到的测试都执行其 `gen()` 回调。因为这一调用发生在 `RawModule` 的 elaboration 期间，每个被发现的模块都会作为子模块实例化进这棵电路树。

**⑤ 命令行工具 `chisel3.UnitTests`。** 这是 mill `unitTest` 任务真正调用的入口（`mainClass = "chisel3.UnitTests"`）：

[src/main/scala/chisel3/UnitTestMain.scala:23-60](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTestMain.scala#L23-L60) —— 用 scopt 的 `OptionParser` 解析参数：`-R/--runpath`（发现路径）、`-o/--output`（输出文件，`-` 表 stdout）、`-l/--list`（只列出类名不生成）、`-v/--verbose`、`-f/--filter`（包含正则）、`-x/--exclude`（排除正则）。注意它重写了 `terminate` 不让 `help` 触发 `sys.exit`，这是为了方便在测试里调用 `main`。

[src/main/scala/chisel3/UnitTestMain.scala:72-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTestMain.scala#L72-L95) —— `handler` 是每个被发现测试的回调，依次执行 filter / exclude / list 三道关卡，最后才 `gen()` 真正生成。

[src/main/scala/chisel3/UnitTestMain.scala:106-109](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/UnitTestMain.scala#L106-L109) —— **生成阶段的核心两行**：把 `DiscoverUnitTests(handler, runpath)` 包进一个本地 `class AllUnitTests extends RawModule`，再用 `ChiselStage.emitCHIRRTL(new AllUnitTests)` 把整棵测试电路 elaboration 成 CHIRRTL 文本。这正是「一堆 UnitTest → 一份 FIR」的收拢点，也呼应了 u1-l5 / u5 中 `ChiselStage` 的角色。

**⑥ mill 任务把三段串起来。** `unitTest` 定义在 `trait Chisel`（即 `chisel[]` 模块，见 [build.mill:305](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L305)）内：

[build.mill:370-413](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/build.mill#L370-L413) —— 三步：(1) `test.runner().run(mainClass = "chisel3.UnitTests", args = "-o" unit_tests.fir ++ runpath ++ chiselArgs)` 生成 `unit_tests.fir`，其中 `runpathArgs` 用 `-R` 把范围**限制在 Chisel 自己的 classpath**（`localClasspath() ++ test.localClasspath()`），避免扫到依赖里的测试；(2) `firtool --ir-hw unit_tests.fir -o unit_tests.mlir --default-layer-specialization=enable` 把 CHIRRTL 编译成 MLIR（HW 方言），`--default-layer-specialization=enable` 与 u8-l3 的 Layer 机制相关——把验证层代码特化进来；(3) `circt-test unit_tests.mlir` 运行全部测试。`-G/-C/-T` 三个短选项分别把额外参数透传给这三步。

#### 4.1.4 代码实践

**实践目标**：亲手写一个 `UnitTest`，验证它被自动发现并 elaboration 成 CHIRRTL。

**操作步骤**（这是「源码阅读 + 本地运行」型实践，需要本地有 mill 与 firtool/circt-test，参考 u1-l2）：

1. 阅读现成的真实用例 [src/test/scala/chiselTests/UnitTestMainSpec.scala:82-92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/UnitTestMainSpec.scala#L82-L92)——它定义了三种 `UnitTest`：普通类、单例 object、`RawModule with UnitTest`，构造时各打印一行。
2. 在 `src/test/scala/chiselTests/` 下仿照写一个最小测试模块（**示例代码，非项目原有**）：

   ```scala
   package chiselTests.sampleTests
   import chisel3._
   import chisel3.test.UnitTest

   class GreetTest extends RawModule with UnitTest {
     Console.err.println("Hello from my unit test")
   }
   ```

3. 用 `chisel3.UnitTests` 的 `-l`（list）模式确认它被发现了：

   ```bash
   ./mill chisel[].test.runMain chisel3.UnitTests -l -f "^chiselTests\\.sampleTests\\."
   ```
   （`-f` 用正则把范围限定到 `sampleTests` 包，避免扫到仓库里成千上万个测试。）

**需要观察的现象**：步骤 3 的 stdout 里应出现 `chiselTests.sampleTests.GreetTest` 一行。

**预期结果**：说明 `DiscoverUnitTests` 在 classpath 上扫描时，通过反射识别出了你的 `UnitTest` 子类型。若把 `-l` 去掉（生成模式），它会和别的测试一起被 `emitCHIRRTL` 收进一份 FIR。

> 待本地验证：本环境未安装 mill/firtool，上述命令的精确输出需在你本地仓库运行后确认。`runMain` 调用 `main` 的方式与 mill `unitTest` 任务内部用 `test.runner().run(...)` 等价。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `UnitTest` 是一个空的 trait，却能让 Chisel「自动发现」所有测试？发现逻辑实际写在哪里？

> **参考答案**：`UnitTest` 只起**类型标记**作用，给反射一个可判定的「身份」（`classOf[UnitTest].isAssignableFrom(clazz)`）。真正的发现逻辑在 `DiscoverUnitTests`：它遍历 classpath/runpath 上所有 `.class`，逐个 `loadClass` 后用 `isAssignableFrom` 过滤出子类型，再用反射触发构造。trait 为「锚点」，`DiscoverUnitTests` 为「扫描器」。

**练习 2**：一个标记为 `UnitTest` 的 `RawModule` 子类，和普通 `class X extends UnitTest` 在被发现后处理方式有何不同？

> **参考答案**：`discoverClass` 会检查 `classOf[BaseModule].isAssignableFrom(clazz)`。若是模块（`RawModule` 也是 `BaseModule`），构造回调被包成 `() => Definition(field.get(null).asInstanceOf[BaseModule])`（或 `Definition(clazz.newInstance...)`），因为模块必须经 `Definition`/elaboration 才能长成电路；普通类/object 则直接构造。

**练习 3**：mill `unitTest` 任务用 `-R` 把 runpath 限制为 `localClasspath() ++ test.localClasspath()`，为什么不直接扫整个 `java.class.path`？

> **参考答案**：整个 classpath 还包含所有外部依赖（Scala 标准库、firrtl、svsim 等），其中可能也带有 `UnitTest` 子类型或同名类，会污染测试集、拖慢扫描。限定到 Chisel 自己的产物目录，确保只跑「Chisel 项目自己定义的测试」。

---

### 4.2 FileCheck：用 LLVM 工具断言生成的 IR/Verilog 文本

#### 4.2.1 概念说明

Chisel 是编译器，编译器最容易写坏的恰恰是「生成出来的东西长什么样」——某个节点名有没有变、某条 `connect` 在不在、位宽对不对。这类**对生成文本的断言**，靠 `assertEquals(verilog, "...")` 把整段 Verilog 硬编码进来太脆弱（任何空白变化都会挂）。业界（LLVM、CIRCT、FIRRTL）的标准解法是 **FileCheck**：用一个专门的工具，在输入文本里按顺序「搜」一系列 `CHECK` 模式，只要每条都能按顺序命中就算通过，无关部分一律忽略。

Chisel 把这个外部工具包装成了一个 Scala trait，让你能对**任意字符串**（通常是 `emitCHIRRTL` / `emitSystemVerilog` 的输出）直接写检查：

```scala
myChirrtlString.fileCheck()(
  """|CHECK:      module MyMod :
     |CHECK:        output out
     |""".stripMargin
)
```

Chisel 自身的 `src/test` 里大量使用这套机制——上一节的 [UnitTestMainSpec](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/UnitTestMainSpec.scala) 就是用 `fileCheck` 来断言 `UnitTests` 命令行的输出。

#### 4.2.2 核心流程

FileCheck 工具只能读**文件**，不能直接吃字符串。所以 Chisel 的 `fileCheck` 方法做了「字符串→临时文件→子进程→判退出码」的桥接：

```
input（被检查的字符串，如生成的 CHIRRTL）
check（CHECK 模式字符串）
   │
   ├── 在 testingDirectory 下建临时目录
   ├── 写 input → tempDir/input
   ├── 写 check → tempDir/check       （CHECK 模式本身就是 FileCheck 的输入脚本）
   │
   └── os.proc("FileCheck", check文件, 额外参数).call(stdin = input文件)
          │
          ├── 退出码 0  → 成功，删除临时目录
          └── 退出码非0 → 抛 NonZeroExitCode（带 stderr）
          └── "Cannot run program" → 抛 NotFound（你没装 FileCheck）
```

FileCheck 的常用模式（来自 [LLVM 官方文档](https://llvm.org/docs/CommandGuide/FileCheck.html)）：

| 模式 | 含义 |
|------|------|
| `CHECK:` | 按顺序在后续行里找匹配 |
| `CHECK-NEXT:` | 必须紧接上一条匹配的下一行 |
| `CHECK-SAME:` | 必须和上一条在同一行 |
| `CHECK-DAG:` | 不要求顺序，只要都出现 |
| `CHECK-NOT:` | 两条之间不得出现该模式 |

#### 4.2.3 源码精读

**① 两种 FileCheck——对象 vs trait。** 文件里有两个同名 `FileCheck`：

[src/main/scala/chisel3/testing/FileCheck.scala:12-46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/FileCheck.scala#L12-L46) —— `object FileCheck` 只装两种异常：`NotFound`（机器上没装 FileCheck 二进制）与 `NonZeroExitCode`（检查失败），都用 `dramaticMessage` 包成醒目的报错。`trait FileCheck` 才是用户混入的 trait。

**② 核心方法 `fileCheck`。**

[src/main/scala/chisel3/testing/FileCheck.scala:48-51](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/FileCheck.scala#L48-L51) —— `trait FileCheck` 通过隐式类 `StringHelpers` 给 `String` 挂上 `fileCheck` 方法。

[src/main/scala/chisel3/testing/FileCheck.scala:86-129](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/FileCheck.scala#L86-L129) —— 完整流程：(1) 从隐式 `testingDirectory: HasTestingDirectory` 拿到写文件的目录；(2) `os.temp.dir` 建临时目录，`os.write.over` 把 `check` 和 `input` 分别写成两个文件；(3) `os.proc("FileCheck", checkFile, extraArgs).call(stdin = inputFile, check = false)` 启动子进程，`check = false` 表示非零退出码不直接抛异常而是返回 `CommandResult`；(4) 按退出码分流：0 成功删临时目录，非 0 抛 `NonZeroExitCode`，`"Cannot run program"` 抛 `NotFound`。

**③ 需要 `HasTestingDirectory`。** 注意方法签名末尾的 `implicit testingDirectory: HasTestingDirectory`——FileCheck 要写临时文件，得知道写到哪。默认实现见 [HasTestingDirectory.scala:90-94](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/HasTestingDirectory.scala#L90-L94)（`def default = timestamp`，即 `build/chiselsim/<时间戳>/`）。

**④ ScalaTest 集成的便捷 trait。** 直接混 `chisel3.testing.FileCheck` 还得自己提供 `HasTestingDirectory`；ScalaTest 用户通常混这个聚合 trait：

[src/main/scala/chisel3/testing/scalatest/package.scala:25](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/scalatest/package.scala#L25) —— `trait FileCheck extends chisel3.testing.FileCheck with TestingDirectory { self: TestSuite => }`，一次把 FileCheck API 和测试目录都备齐（`TestingDirectory` 在同包提供默认 `HasTestingDirectory`）。这正是 `UnitTestMainSpec` 里 `with FileCheck` 的来源。

#### 4.2.4 代码实践

**实践目标**：用一个最小例子跑通 `fileCheck`，理解 CHECK 模式如何按顺序命中。

**操作步骤**：

1. 阅读官方示范用法 [src/main/scala/chisel3/testing/FileCheck.scala:53-75](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/testing/FileCheck.scala#L53-L75)（doc 里的 `Foo` 例子：检查 `"Hello world!"`）。
2. 写一个 ScalaTest（**示例代码**），把任意字符串交给 FileCheck：

   ```scala
   import chisel3.testing.scalatest.FileCheck
   import org.scalatest.flatspec.AnyFlatSpec
   import org.scalatest.matchers.should.Matchers

   class FileCheckDemo extends AnyFlatSpec with Matchers with FileCheck {
     "FileCheck" should "match in order" in {
       "Hello world!".fileCheck()(
         """|CHECK:      Hello
            |CHECK-SAME: world
            |""".stripMargin
       )
     }
   }
   ```

3. 故意把第二条改成 `CHECK-SAME: galaxy`，重跑，观察失败信息。

**需要观察的现象**：步骤 2 通过；步骤 3 抛 `FileCheck.Exceptions.NonZeroExitCode`，错误信息里会打印出 FileCheck 子进程的 stderr（指出哪一条 CHECK 没命中、在输入的哪一行）。

**预期结果**：证明 FileCheck 是「按顺序、逐条搜」的——`CHECK-SAME` 要求与上一条同行，所以 `Hello` 和 `world` 必须在同一行才能命中。若机器没装 `FileCheck` 二进制，会改成抛 `NotFound`（提示你去装）。

> 待本地验证：本环境未安装 LLVM `FileCheck` 工具，需在本地确认。许多系统可通过包管理器安装（如 `apt install filecheck` 或随 LLVM 一起提供）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Chisel 不直接用 `assert(chirrtl.contains("module MyMod"))`，而要引入外部 FileCheck 工具？

> **参考答案**：`contains` 只能做单点子串匹配，无法表达「顺序」「紧邻」「同行」「不出现」等约束，也无法在一次失败时清晰指出「第几条 CHECK 在哪一行没命中」。FileCheck 是为「检查编译器生成文本」专门设计的，模式丰富、报错定位精确，且对无关文本（空白、重命名）更鲁棒。

**练习 2**：`fileCheck` 方法为什么需要 `implicit testingDirectory: HasTestingDirectory`？

> **参考答案**：FileCheck 二进制只能读文件，所以必须把 `input` 字符串和 `check` 模式都先落盘成临时文件。`HasTestingDirectory` 告诉它把这些中间文件写到哪个目录（默认 `build/chiselsim/<时间戳>/`），既避免污染工作区，也方便失败时人工查看残留文件排查。

**练习 3**：退出码 0 与「FileCheck 进程不存在」分别会触发什么？

> **参考答案**：退出码 0 → 检查通过，删临时目录静默返回；非 0 退出码 → 抛 `NonZeroExitCode`（带命令和 stderr）；若系统找不到 `FileCheck` 可执行文件，`os.proc` 抛 `IOException("Cannot run program ...")`，被捕获后转抛 `NotFound`，提示「你是否忘了安装」。

---

### 4.3 chiseltest 包对象：ChiselTest 兼容入口 → ChiselSim

#### 4.3.1 概念说明

历史上（Chisel 3/5/6），用户写仿真测试用的是独立的 **chiseltest** 库，典型写法是：

```scala
test(new MyModule) { dut =>
  dut.io.in.poke(42.U)
  dut.clock.step()
  dut.io.out.expect(42.U)
}
```

到了 **Chisel 7**，旧的 chiseltest 库被移除，仿真能力被吸收进主仓库的 `chisel3.simulator`（即 **ChiselSim**，详见 u8-l5）。但为了让海量旧测试代码「不改一行就能在新版跑起来」，Chisel 提供了一个 **兼容层**——本讲的 `chiseltest` 包对象。它的定位在源码注释里写得非常直白：

[src/main/scala/chiseltest/package.scala:3-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/package.scala#L3-L32) —— 「ChiselTest Compatibility Layer for Chisel 7…preserves the familiar ChiselTest API while delegating to Chisel 7's ChiselSim underneath」。

所以本模块的核心心智模型是：**`chiseltest` 包对象不是一套独立实现，而是一层「隐式类垫片」**，它把 `dut.io.in.poke(...)` 这类旧 API 调用，转发到底层 `chisel3.simulator.PeekPokeAPI` 提供的 `toTestableData` 等真正的实现上。`import chiseltest._` 带来的不是新引擎，而是「把旧语法接到新引擎上的适配器」。

> 注意区分：`UnitTest`（4.1）是「内联、由 firtool+circt-test 跑」的自检机制；而 `chiseltest`（本节）是「ScalaTest 风格、由 ChiselSim/Verilator 跑」的仿真测试机制。两者面向不同场景，不要混淆。

#### 4.3.2 核心流程

一次 chiseltest 风格测试的执行链路：

```
用户代码：  test(new MyModule) { dut => dut.io.in.poke(42.U); dut.clock.step(); dut.io.out.expect(42.U) }
   │
   │  test(...) 来自 trait ChiselScalatestTester（package chiseltest，非包对象本身）
   ▼
TestBuilder/Tes│Runner.apply(body)
   │
   │  new ChiselSim{}.simulate(dutGen){ dut => body(dut) }     # 委托给 ChiselSim
   ▼
ChiselSim.simulateRaw → svsim → Verilator（默认）编译并启动仿真进程
   │
   │  在 body 里：dut.io.in.poke(42.U)
   ▼
隐式类 testableData(x).poke(value)         # 由 import chiseltest._ 注入
   │
   │  toTestableData(x).poke(value)        # 转发
   ▼
PeekPokeAPI.TestableData(x).poke(value)    # 真正实现：调 simulationPort.set(value)
   │
   ▼
svsim 向仿真进程发送 SetBits 命令（见 u8-l5 的 Command/Message 协议）
```

关键点：**「旧 API 表面」在包对象，「真实现」在 `PeekPokeAPI`，二者通过 `toTestableData` 这座桥连接**。`poke/peek/expect` 在兼容层只是无脑转发。

#### 4.3.3 源码精读

**① 兼容垫片：隐式类 `testableData`。**

[src/main/scala/chiseltest/package.scala:44-57](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/package.scala#L44-L57) —— 这是包对象里最核心的一段。`implicit class testableData[T <: Data](x: T)` 给任意 `Data` 挂上 `poke/peek/expect`，但每个方法体都只有一行：转给 `toTestableData(x)`。注意 `peek`/`expect` 还要手动 `materialize` 一个隐式 `SourceInfo`（因为兼容层没有像主 API 那样靠宏自动注入源信息，详见 u7-l2）。

[src/main/scala/chiseltest/package.scala:104-138](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/package.scala#L104-L138) —— `testableUInt` 在此基础上多了 `poke(BigInt)` / `poke(Int)` / `peekInt()` 等重载，方便直接用 Scala 数值驱动。

[src/main/scala/chiseltest/package.scala:169-180](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/package.scala#L169-L180) —— `testableClock` 提供 `step()` / `step(cycles)`，转发到 `toTestableClock(x).step(...)`；`setTimeout` 是个 no-op 桩（ChiselSim 没有对应概念）。

**② 兼容桩：被忽略的旧注解。**

[src/main/scala/chiseltest/package.scala:39-41](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/package.scala#L39-L41) —— `WriteVcdAnnotation` 与 `VerilatorBackendAnnotation` 是「Dummy annotations…ignored in Chisel 7」：旧代码常传它们，新版把它们做成空 case object 让代码能编译。注意：`WriteVcdAnnotation` 在 `ChiselScalatestTester` 里其实被**特殊识别**并真的开 VCD（见下），并非完全无用；`VerilatorBackendAnnotation` 才是真忽略。

**③ `toTestableData` 的真身在 `PeekPokeAPI`。** 这是「桥」的另一端：

[src/main/scala/chisel3/simulator/PeekPokeAPI.scala:888-939](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L888-L939) —— `implicit class TestableData[T <: Data]` 的 `poke`/`peek`/`expect` 用一次 `match` 把 `Data` 分派到具体类型：`Bool→TestableBool`、`UInt→TestableUInt`、`Record→TestableRecord`、`Vec→TestableVec` 等。真正和仿真器交互的是 [PeekPokeAPI.scala:321-328](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L321-L328) 的 `peek`/`poke`：调 `simulationPort.get(...)` / `simulationPort.set(value)`，最终经 svsim 发命令给仿真进程。

[src/main/scala/chisel3/simulator/PeekPokeAPI.scala:942-962](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L942-L962) —— `trait PeekPokeAPI` 用一组 `implicit def toTestableXxx` 把 `UInt/Clock/Record...` 转成对应 `TestableXxx`。**chiseltest 包对象正是 `import` 了这里的 `toTestableData` 等**（通过 `import chisel3.simulator.EphemeralSimulator._`，而 `EphemeralSimulator extends PeekPokeAPI`）。

**④ 链条的起点：`EphemeralSimulator`。**

[src/main/scala/chisel3/simulator/EphemeralSimulator.scala:19-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/EphemeralSimulator.scala#L19-L32) —— `object EphemeralSimulator extends PeekPokeAPI`，`simulate` 内部 `chiselSim.simulateRaw(module, settings)(body)`。它 `extends PeekPokeAPI` 正是为什么 `import chisel3.simulator.EphemeralSimulator._` 能把 `toTestableData` 等隐式转换带进 `chiseltest` 包对象的作用域。

**⑤ `test(...)` 入口在 `ChiselScalatestTester`。** 虽然在另一个文件，但同属 `package chiseltest`，故 `import chiseltest._` 一并可用：

[src/main/scala/chiseltest/ChiselScalatestTester.scala:53-76](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/ChiselScalatestTester.scala#L53-L76) —— `trait ChiselScalatestTester` 提供 `def test[T <: Module](dutGen: => T): TestBuilder[T]`，返回 `TestBuilder` 支持 `.withAnnotations(...)` 链式调用或直接 `apply(body)`。

[src/main/scala/chiseltest/ChiselScalatestTester.scala:103-111](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/ChiselScalatestTester.scala#L103-L111) —— `TestBuilder.apply` 真正启动仿真：`new ChiselSim{}` 后 `chiselSim.simulate(dutGen){ dut => ... body(dut) }`。这就是「兼容 API → ChiselSim」的最终落点。

#### 4.3.4 代码实践

**实践目标**：写一个最小的 chiseltest 风格 ScalaTest，验证 `test(new Mod){ dut => ... }` 走的是 ChiselSim。

**操作步骤**：

1. 在 `src/test/scala/chiselTests/` 写一个被测模块 + 测试（**示例代码**）：

   ```scala
   package chiselTests
   import chisel3._
   import chiseltest._
   import org.scalatest.flatspec.AnyFlatSpec

   class Passthrough extends Module {
     val io = IO(new Bundle { val in = Input(UInt(8.W)); val out = Output(UInt(8.W)) })
     io.out := io.in
   }

   class PassthroughSpec extends AnyFlatSpec with chiseltest.ChiselScalatestTester {
     "Passthrough" should "forward its input" in {
       test(new Passthrough) { dut =>
         dut.io.in.poke(42.U)
         dut.clock.step()
         dut.io.out.expect(42.U)
       }
     }
   }
   ```

2. 运行：`./mill chisel[].test.test chiselTests.PassthroughSpec`

**需要观察的现象**：仿真被编译（首次会调用 Verilator，耗时较长），随后测试通过；若把 `expect(42.U)` 改成 `expect(0.U)`，会抛 `FailedExpectationException`（来自 [PeekPokeAPI.scala:226-234](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L226-L234)），并打印 observed vs expected。

**预期结果**：证明 `dut.io.in.poke` / `dut.io.out.expect` 这些「旧 chiseltest 写法」确实经由包对象的隐式类 → `toTestableData` → `PeekPokeAPI` → svsim → Verilator 跑通，无需改动语法即可在 Chisel 7 工作。

> 待本地验证：本环境未配置 Verilator，需在本地确认。`poke` 后首次 `peek`/`expect` 会自动 `run(0)` 结算（u8-l5 介绍的延迟求值），所以这里即使 `step(0)` 也能读到组合逻辑结果。

#### 4.3.5 小练习与答案

**练习 1**：`chiseltest` 包对象里的 `testableData.poke` 方法体只有一行 `toTestableData(x).poke(value)`，为什么不直接在这里实现 poke 逻辑？

> **参考答案**：因为包对象是**兼容垫片**，目的是「保留旧 API 表面、复用新引擎实现」。真正和仿真器打交道的逻辑（分派到 `UInt/Bool/Record/Vec`、调 `simulationPort.set`）已经在 `chisel3.simulator.PeekPokeAPI` 里写好且被 ChiselSim 主路径使用。兼容层只做转发，避免两套实现分叉，保证「旧写法」和「新写法」行为一致。

**练习 2**：旧代码里常见的 `WriteVcdAnnotation` 在 Chisel 7 还有效吗？

> **参考答案**：部分有效。包对象把它做成 `case object`（[package.scala:40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chiseltest/package.scala#L40)）让旧代码能编译；`ChiselScalatestTester` 会**特殊识别**它并真的开启 VCD 波形（写入 `build/chiselsim/<时间戳>/workdir-verilator/trace.vcd`）。而 `VerilatorBackendAnnotation` 才是真正被忽略的桩（ChiselSim 默认就用 Verilator）。

**练习 3**：`peek`/`expect` 在兼容层里都手动 `materialize` 了一个隐式 `SourceInfo`，为什么主 API 不需要这样做？

> **参考答案**：主 API（`chisel3.*`）靠编译器插件 + 宏（见 u7-l2 `SourceInfoTransform`）在每个调用点自动注入携带文件名/行号的 `SourceInfo`，用于精确报错。兼容层是普通隐式类、不经宏改写，拿不到自动注入的 `SourceInfo`，所以用 `SourceInfo.materialize` 在运行期手动合成一个（行号信息不如宏注入精确，但足以让 `expect` 失败时报出位置）。

---

## 5. 综合实践

把三个最小模块串起来，完成一个**「定义模块 → 内联 UnitTest 自检 → FileCheck 断言生成文本 → chiseltest 仿真验证」**的小闭环。目标被测对象是一个简单的寄存器型加法累加器。

**任务**：

1. **定义模块**（写在 `src/main/scala/` 或 `src/test/scala/chiselTests/`，**示例代码**）：

   ```scala
   package chiselTests
   import chisel3._
   import chisel3.util._
   import chisel3.test.UnitTest

   class Accumulator extends Module {
     val io = IO(new Bundle {
       val en  = Input(Bool())
       val in  = Input(UInt(8.W))
       val out = Output(UInt(8.W))
     })
     val acc = RegInit(0.U(8.W))
     when(io.en) { acc := acc + io.in }
     io.out := acc
   }
   ```

2. **内联 UnitTest + FileCheck**：写一个 ScalaTest，调用 `chisel3.UnitTests.main` 生成 CHIRRTL，用 FileCheck 断言 CHIRRTL 里出现了累加寄存器与 `connect`（完全模仿 [UnitTestMainSpec.scala:59-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/UnitTestMainSpec.scala#L59-L63) 的套路）：

   ```scala
   import chisel3.testing.scalatest.FileCheck
   import org.scalatest.flatspec.AnyFlatSpec

   class AccumulatorUnitTestSpec extends AnyFlatSpec with FileCheck {
     "Accumulator" should "elaborate into CHIRRTL with a reg" in {
       val out = new java.io.ByteArrayOutputStream()
       Console.withOut(out) {
         chisel3.UnitTests.main(Array("-f", "^chiselTests\\.Accumulator$"))
       }
       out.toString.fileCheck()(
         """|CHECK:      module Accumulator :
            |CHECK:        reg acc :
            |""".stripMargin
       )
     }
   }
   ```
   （这里把 `Accumulator` 也标记为 `UnitTest`，或把它作为子模块实例化进一个 `UnitTest`，保证 `UnitTests` 能发现它。）

3. **chiseltest 仿真**：用第 4.3.4 节的写法，`poke` 两个数后 `step`、`expect` 累加结果。

4. **跑原生 unitTest 任务**：`./mill chisel[].unitTest`，观察它如何把所有 `UnitTest` 收拢成 `unit_tests.fir`、经 firtool 编译、由 circt-test 执行。

**预期结果**：你会在不同层面验证同一个模块——FileCheck 看「生成出来的形状对不对」，chiseltest 看「跑起来的行为对不对」，`unitTest` 任务看「批量内联测试能不能被 firtool+circt-test 跑通」。三者覆盖了 Chisel 测试体系的全部三条脉络。

> 待本地验证：综合实践依赖 mill、firtool、circt-test、FileCheck 与 Verilator 均已就绪（参考 u1-l2 的环境准备）。CHECK 模式中的 `reg acc` 精确文本需以本地实际生成的 CHIRRTL 为准——可先 `emitCHIRRTL` 打印一次再据此编写 CHECK 行。

---

## 6. 本讲小结

- Chisel 的测试不是单一框架，而是**三条脉络**叠加：内联 `UnitTest`（firtool+circt-test 跑）、`FileCheck`（文本断言）、`chiseltest`（ScalaTest + ChiselSim 仿真）。
- `UnitTest`（`package chisel3.test`，注意文件路径与包名不一致）是个空标记 trait；`DiscoverUnitTests` 在运行期反射扫描 classpath/runpath，把所有子类型找出来，模块类还会被包成 `Definition(...)` 触发 elaboration。
- `chisel3.UnitTests` 命令行工具（scopt 解析 `-R/-o/-l/-f/-x`）把发现的测试收拢进一个 `AllUnitTests` 模块，用 `ChiselStage.emitCHIRRTL` 产出一份 CHIRRTL；mill `unitTest` 任务再依次调 `firtool --ir-hw` 与 `circt-test` 完成编译与执行。
- `FileCheck` trait 把「字符串 + CHECK 模式」写成临时文件，启动 LLVM `FileCheck` 子进程按顺序匹配；退出码 0 通过，非 0 抛 `NonZeroExitCode`，未安装抛 `NotFound`。ScalaTest 用户通常混 `chisel3.testing.scalatest.FileCheck` 一步到位。
- `chiseltest` 包对象是 **Chisel 7 的兼容垫片**：用一组隐式类（`testableData/testableUInt/testableClock` 等）保留旧 `poke/peek/expect/step` API，但每个方法都只转发到 `chisel3.simulator.PeekPokeAPI` 的 `toTestableData`；`test(...)` 入口在 `ChiselScalatestTester`，最终委托 `ChiselSim` → svsim → Verilator。
- 三者的衔接点：`UnitTests` 用 `ChiselStage`（u1-l5/u5）生成 CHIRRTL，`unitTest` 任务用 firtool/circt-test（u5-l4），chiseltest 用 ChiselSim/svsim（u8-l5）——本讲是前面 Stage 与仿真讲义在「测试」场景下的汇合。

---

## 7. 下一步学习建议

- **延续仿真主线**：本讲的 chiseltest 兼容层只是入口，真正的仿真引擎在 `chisel3.simulator` 与 `svsim`——继续读 **u8-l5（仿真与 svsim / simulator）**，看 `Simulator`/`Simulation`/`Backend` 如何把模块编译成可执行仿真。
- **深入错误与诊断**：测试失败时报错信息的来源是 elaboration 期的错误收集与堆栈裁剪——读 **u9-l2（错误处理与 ElaborationTrace）**，理解 `Builder.error` 与 `trimStackTraceToUserCode`。
- **调试信息机制**：`unitTest` 任务里的 `--default-layer-specialization=enable` 与 `printf` 调试代码隔离有关——读 **u9-l3（Debug 层与调试信息）** 与 **u8-l3（Layers 与 Probe）**，理解验证/调试代码如何被组织进可剥离的层。
- **动手扩展**：尝试为本讲综合实践的 `Accumulator` 再加一条 `expectPartial`（针对 Bundle 部分字段）的 chiseltest 用例，并对照 [PeekPokeAPI.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala) 验证你对分派逻辑的理解。
