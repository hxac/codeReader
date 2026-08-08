# Chisel 6 仿真测试框架

## 1. 本讲目标

本讲是测试单元（U9）的第一讲。读完本讲，你应该能够：

- 说清楚 chisel-npu 为什么用 `chisel3.simulator.EphemeralSimulator` 而不是老的 `chiseltest`，以及两者在「谁来跑仿真」上的根本差别。
- 掌握 `simulate(new Module) { dut => ... }` 这套 Chisel 6 原生测试范式，熟练使用 `poke / step / peek / expect` 四个原语。
- 学会复用项目 `testUtil` 包里的共享工具（`PrintHelper` 打印矩阵/向量、`WidthConst` 宽度常量），不再在每个 spec 里重复造轮子。
- 牢记一条最容易踩的坑：**对 `ChiselEnum` 字段，`poke(op)` 可以用，但 `expect(op)` 在 EphemeralSimulator 里不可用**，需要用 `peek().litValue` 间接比较。
- 理解现代 spec 用 `NpuAssembler` 构造 32 位指令字（`poke` 整条指令）而不是直接戳 bundle 子字段，这样能顺带把译码器路径也测到。

## 2. 前置知识

本讲承接 [u1-l2 开发环境与构建运行方式](u1-l2-build-and-run.md)，假定你已经知道：

- 项目用 Docker 镜像 `fangruil/chisel-dev` 打包 `sbt + firtool + verilator`，`make test` 等价于容器内 `sbt test`。
- Chisel 源码经 `sbt run` → FIRRTL → `firtool` 翻译成 SystemVerilog，再由 verilator 做软件仿真。

此外，你需要一点点 Scala 与 Chisel 的基础概念（讲义里会顺手解释）：

- **`Module` 与 `IO`**：Chisel 里每个硬件模块继承 `Module`，用 `IO(new Bundle {...})` 声明对外端口。
- **`Vec`**：Chisel 里「同类型端口数组」，例如 `Vec(n, SInt(8.W))` 是 n 个 8 位有符号数；用 `io.out_a(i)` 取第 i 个。
- **`ChiselEnum`**：Chisel 用来定义硬件枚举的类型，例如本项目的 `VecOp`（52 个内部操作码）、`VecWidth`（VX/VE/VR）。它和 Scala 的 `Enumeration` 不是一回事。
- **`RegInit`**：上电有初值的寄存器，仿真第 0 拍（还没 `step` 之前）就持有初值。
- **ScalaTest `AnyFlatSpec`**：Scala 最常见的测试风格，`"X" should "Y" in { ... }` 是它的固定句式。

> 一句话直觉：Chisel 6 的 EphemeralSimulator 把你写的 RTL 真正 elaborate 成 Verilog、调起 verilator 编译运行，然后给你一个 `dut`（device under test）句柄，让你像「拨开关、看示波器」一样 `poke`（置输入）、`step`（走一拍时钟）、`peek`/`expect`（读输出/断言输出）。

## 3. 本讲源码地图

本讲涉及的关键文件（测试侧为主）：

| 文件 | 作用 |
|:---|:---|
| [src/test/scala/alu/vec/VALUArithSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala) | 本讲主参考：一个结构清晰的 EphemeralSimulator spec，演示 `simulate`、`poke`、`step`、`expect` 与随机测试。 |
| [src/test/scala/utils/printHelper.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/utils/printHelper.scala) | `testUtil` 共享工具之一：把仿真中 `peek` 出来的 Chisel `Vec` 打印成可读矩阵/向量。 |
| [src/test/scala/utils/widthHelper.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/utils/widthHelper.scala) | `testUtil` 共享工具之二：与 `VecWidth` 枚举值对齐的整数常量。 |
| [src/test/scala/isa/InstrDecoderSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala) | 现代 spec 范式：用 `NpuAssembler` 构造指令字、`peek().litToBoolean` 断言 `illegal`。 |
| [src/test/scala/alu/mma/sa/DataFeederSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala) | `testUtil` 实战用法示范：`import testUtil._` + `new PrintHelper()`。 |
| [src/main/scala/alu/mma/sa/systolicArray.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala) | 综合实践的被测对象：`SystolicArray2D`，含 `reg_h/reg_v` 移位寄存器。 |
| [AGENTS.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md) | 仓库的测试约定（权威）：EphemeralSimulator、enum 比较、NpuAssembler 范式。 |
| [build.sbt](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt) | 测试依赖声明（注意 `chiseltest` 的「声明但不用」细节）。 |

## 4. 核心概念与源码讲解

### 4.1 EphemeralSimulator:Chisel 6 原生仿真器

#### 4.1.1 概念说明

在 Chisel 3 时代，社区习惯用一个独立库 `chiseltest` 来写仿真测试，它自带线程化激励器（`fork`/`join`、`timescope`）和一堆便利断言。但 chisel-npu 用的是 **Chisel 6.7.0**，它的主仓库 `org.chipsalliance::chisel` 已经内置了一个轻量仿真器：`chisel3.simulator.EphemeralSimulator`。

> **EphemeralSimulator（瞬时仿真器）**：名字里的「ephemeral」强调它「用完即弃」——每次 `simulate(...)` 都会即时把被测模块 elaborate 出 Verilog、调 firtool+verilator 现场编译出一个临时仿真核，跑完测试就清理掉。

两者的根本差别在「谁来执行仿真」：

| 维度 | `chiseltest`（老） | `EphemeralSimulator`（本项目） |
|:---|:---|:---|
| 来源 | 独立库 `edu.berkeley.cs::chiseltest` | Chisel 6 主库自带 |
| 后端 | 自带 treadle 解释器 + 可选 verilator | 直接走 firtool → verilator（与综合同一前端） |
| 高级特性 | `fork/join`、`timescope`、阶梯时钟 | 只有 `poke/step/peek/expect` 四件套 |
| 适合 | 复杂并发激励 | 确定性的逐拍驱动测试（本项目正是这种） |

AGENTS.md 把这条作为权威约定钉死：

[AGENTS.md:117-122](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L117-L122) —— 明确写「Uses `chisel3.simulator.EphemeralSimulator` (native Chisel 6). **Not** `chiseltest`」。

**一个诚实的小细节**：[build.sbt:14](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L14) 里仍然声明了 `"edu.berkeley.cs" %% "chiseltest" % "5.0.2" % "test"` 这条依赖。但我们在全仓 `.scala` 文件里搜索 `import chiseltest`，**一处也找不到**；所有 21 个 spec 全部 `import chisel3.simulator.EphemeralSimulator._`。也就是说这条依赖目前是「声明了但没被任何测试使用」的遗留物。读源码时以 `import` 为准，不要被 build.sbt 误导。

#### 4.1.2 核心流程

一个 EphemeralSimulator 测试的骨架只有四步：

```text
1. import chisel3.simulator.EphemeralSimulator._   // 把 simulate/poke/step/peek/expect 引入作用域
2. simulate(new MyModule(args)) { dut => ... }     // 现场编译出 DUT，dut 是它的句柄
3. 在闭包里：
     dut.io.xxx.poke(value)   // 置输入（拨开关）
     dut.clock.step(n)        // 推进 n 拍时钟（按按钮）
     dut.io.yyy.peek()        // 读输出（看示波器），返回带 litValue 的句柄
     dut.io.yyy.expect(value) // 读输出并断言等于 value（拨开关 + 自动比对）
4. 闭包正常返回 → 该用例通过；expect/assert 失败 → 抛异常 → 用例失败
```

几个关键语义，初学者最易搞错：

- **第 0 拍就有初值**。`RegInit` 指定的初值在 `simulate` 一进入、还没 `step` 时就生效。所以「poke → 立刻 peek」测的是组合逻辑，「poke → step → peek」测的是寄存了一拍的结果。
- **`step(0)` 表示「不推进时钟，只求值组合逻辑」**，专门用来测纯组合模块（如译码器）。
- **`poke` 是「写当前拍的输入」**，`expect` 是「检查当前拍的输出」，二者都在「当前仿真时刻」生效，`step` 才把时间往前推。
- **未驱动的输入端口**：EphemeralSimulator 对未 poke 的输入默认给 0，但某些「异步读用到该地址」的场景，firtool 会报 `uninitialized sink`（见 [AGENTS.md:142](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L142) 关于 `MultiWidthRegisterBlock.io.ext_r_addr` 的告警）。养成「把所有输入都显式 poke」的习惯最稳妥。

#### 4.1.3 源码精读：VALUArithSpec

VALUArithSpec 是本讲的主参考，结构非常典型。先看它的导入和类骨架：

[VALUArithSpec.scala:6-9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L6-L9) —— 第 7 行的 `import chisel3.simulator.EphemeralSimulator._` 就是整套测试范式的「总开关」。

再看「拨控制开关」的辅助函数 `pokeCtrl`：

[VALUArithSpec.scala:28-36](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L28-L36) —— 注意第 29 行 `dut.io.ctrl.op.poke(op)`，这里的 `op` 是 `VecOp.Type`（一个 `ChiselEnum` 成员）。**这说明 `poke(enum)` 是合法的**（这点很重要，4.3 节会对比 `expect(enum)` 不合法）。

接着是一个子用例 `runVaddWrap`，完整展示了「随机激励 → step → 逐 lane 断言」的循环：

[VALUArithSpec.scala:52-63](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L52-L63) —— 关键三句：

- 第 57 行 `dut.clock.step()` 推进一拍（因为 VALU 输出有 1 拍 `RegNext` 延迟，见 u5-l1）。
- 第 60 行 `dut.io.out_vx(i).expect((exp & 0xFF).U, s"vadd wrap lane $i")`：`out_vx(i)` 是 `SInt`，**不是 enum**，所以 `expect` 完全可用；第二个参数是失败时的提示串。
- `& 0xFF` 是把 Scala 有符号 `Int` 截成 8 位无符号位模式，再 `.U` 包成 `UInt` 字面量——这是处理「负数如何 poke/expect」的标准技巧（参见 [AGENTS.md:100](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L100)）。

最后是把这些子用例「合并成一条 ScalaTest 用例」的写法，并用 `withClue` 给每段加上前缀，失败时能立刻定位是哪个子操作挂了：

[VALUArithSpec.scala:177-190](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L177-L190) —— 整个 `simulate(new VALU(K, N))` 只编译一次 DUT，10 个子用例在同一个仿真进程里复用它，避免反复编译（verilator 编译是耗时大头）。这是写「大批量随机测试」时的性能要点。

#### 4.1.4 代码实践：运行一个现成 spec

**实践目标**：亲眼看到一个 EphemeralSimulator spec 在容器里跑起来，并观察 ScalaTest 的 `-oDT`（按耗时排序）输出。

**操作步骤**：

1. 进入项目根目录，确认 Docker 可用（前置 u1-l2）。
2. 用单测快捷脚本只跑这一个 spec（不必跑全量 `sbt test`）：

   ```bash
   tool/test-specific-spec.sh alu.vec.VALUArithSpec
   ```

   该脚本内容只有一行，就是把类名透传给容器内的 `sbt "testOnly ..."`：

   [tool/test-specific-spec.sh:1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/test-specific-spec.sh#L1)

3. 第一次运行会触发 verilator 编译 VALU，耗时较长；之后复用缓存会快很多。

**需要观察的现象**：

- 控制台先打印 Chisel/firtool 的 elaborate 日志，再打印 verilator 编译日志，最后是 ScalaTest 结果。
- 因为 [build.sbt:25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L25) 配了 `-oDT`，结果会按「最慢的用例排最前」列出每条用例耗时。

**预期结果**：`VALU arith` 用例显示通过（绿条 / `[info] VALUArithSpec`），并附耗时。

> 待本地验证：具体耗时取决于机器与缓存状态，无法在此预判数值；重点是确认「一条 spec = 一次 elaborate + 一次 verilator 编译 + N 条断言」这条链路真的发生了。

#### 4.1.5 小练习与答案

**练习 1**：把 `runVaddWrap` 里的 `dut.clock.step()` 删掉会怎样？为什么？
**答案**：VALU 的输出经 `RegNext` 寄存一拍，若不 `step`，`out_vx` 还停留在上一拍的值（首次则是初值），`expect` 几乎必然失败。`step` 的作用就是「让寄存器吃进本拍输入、吐出结果」。

**练习 2**：为什么 `expect` 的入参要写成 `(exp & 0xFF).U` 而不是直接 `exp`？
**答案**：`exp` 是有符号 `Int`，可能为负（例如 -1）。直接传给 `expect` 会因类型/位宽不符报错；`& 0xFF` 先取低 8 位（得到 255），`.U` 包成无符号字面量，恰好对应 8 位补码位模式。

---

### 4.2 testUtil 共享工具包

#### 4.2.1 概念说明

仿真时，`peek()` 读出来的不是普通 Scala `Int`，而是一个带 `litValue`（返回 `BigInt`）的「字面量句柄」。如果直接 `println(vec)`，打印出来的是一堆 Chisel 内部对象描述，完全没法看。于是项目把「把仿真中的 `Vec`/矩阵打印成人类可读格式」这件事抽成一个共享工具包 `testUtil`，避免每个 spec 各写一遍。

`testUtil` 目前提供两样东西：

- **`PrintHelper`**：把 `Array[Int]` 或 Chisel `Vec[SInt]` 打印成 Python 风格的矩阵/向量。
- **`WidthConst`**：与 `VecWidth` 枚举数值对齐的整数常量 `VX=0 / VE=1 / VR=2`，方便在断言里直接写数字。

#### 4.2.2 核心流程

`PrintHelper` 成对提供「打印 Scala 数组」和「打印 Chisel Vec」两组方法：

```text
printMatrix(mat: Array[Int], n)         // 打印 Scala 数组（n×n 矩阵）
printVector(vec: Array[Int], n)         // 打印 Scala 数组（向量）
printMatrixChisel(mat: Vec[SInt], n)    // 打印仿真中的 Chisel Vec（矩阵），内部 peek().litValue
printVectorChisel(vec: Vec[SInt], n)    // 打印仿真中的 Chisel Vec（向量），内部 peek().litValue
```

后两者的核心动作是：对每个元素 `mat(i*n+j).peek().litValue` 取出当前拍的值，拼成字符串打印。换句话说，**`PrintHelper` 是「带 `peek` 副用」的调试打印机**——调用它等于在当前仿真时刻拍一张所有输出的快照。

#### 4.2.3 源码精读

先看包定义和导入。注意 `printHelper.scala` 自己也 `import` 了 EphemeralSimulator，因为它的方法要在 `simulate` 闭包里调用 `peek`：

[printHelper.scala:1-6](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/utils/printHelper.scala#L1-L6) —— `package testUtil` + `import chisel3.simulator.EphemeralSimulator._`。

打印 Chisel 矩阵的实现：

[printHelper.scala:28-38](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/utils/printHelper.scala#L28-L38) —— 第 33 行 `mat(i*n+j).peek().litValue.toString()` 是全包的关键一行：`peek()` 读当前拍值，`.litValue` 拿到 `BigInt`，`.toString()` 转十进制。`litValue` 对 `SInt` 返回**有符号**解释（这一点与 `UInt` 不同），所以负数会正常显示成 `-1` 而不是 `255`。

宽度常量工具非常短，但解决了「枚举值难记」的小痛点：

[widthHelper.scala:5-9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/utils/widthHelper.scala#L5-L9) —— 注释解释了为什么需要它：`NCoreVALUBundle.width`（即 `regCls`）是 `UInt(2.W)`，在断言里直接写 `0/1/2` 比记枚举名更方便，而这套常量保证数字与 `VecWidth` 枚举值严格一致。

实战中怎么用？看 DataFeederSpec 的开头：

[DataFeederSpec.scala:5-16](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala#L5-L16) —— 第 5 行 `import testUtil._`，第 16 行 `val print_helper = new testUtil.PrintHelper()`，之后就能调 `print_helper.printMatrix(...)`。再看它在循环里读输出的写法：

[DataFeederSpec.scala:79-81](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala#L79-L81) —— `dut.io.reg_a_out(__i).peek().litValue.toInt`，与 `PrintHelper` 内部完全同一个套路，说明这套「`peek().litValue.toInt` 取整数」是全项目一致的惯例。

#### 4.2.4 代码实践：读懂 PrintHelper 在 spec 里的用法

**实践目标**：理解 `PrintHelper` 不是「装饰性打印」，而是「在指定仿真时刻拍快照」的调试工具。

**操作步骤**：

1. 打开 [src/test/scala/alu/mma/sa/DataFeederSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala)。
2. 定位到主循环里第 79 行附近的 `_a_in_str_out` 拼接段，以及第 95 行的 `dut.clock.step()`。
3. 回答：为什么这些 `peek().litValue.toInt` 必须放在 `step()` **之前**，而下一组对 `reg_accum_out` 的 `expect`（第 97-103 行）放在 `step()` **之后**？

**需要观察的现象**：在脑中单步推演——`step` 之前的 `peek` 看到的是「本拍输入产生的组合输出」，`step` 之后的 `expect` 校验的是「累加器那条长延迟路径走了 `2n-2` 拍之后才出现的输出」。

**预期结果**：能说出「`peek`/`expect` 读取的是当前仿真时刻的值，`step` 才推进时间，所以同一组端口在 `step` 前后可能代表不同拍的值」。

> 待本地验证：可选地运行 `tool/test-specific-spec.sh alu.mma.sa.DataFeederSpec`，在控制台观察 `print_helper` 打印出来的逐拍矩阵，验证你对时序的理解。

#### 4.2.5 小练习与答案

**练习 1**：`printMatrixChisel` 为什么要接收 `Vec[SInt]` 而不是 `Vec[UInt]`？如果被测端口是 `UInt` 怎么办？
**答案**：这是该工具当前的签名限制（项目里 SA 的输出是 `SInt`）。若端口是 `UInt`，最简单是先 `peek().litValue.toInt` 自己拼字符串；或扩展一个 `printMatrixChiselU` 重载。`peek().litValue` 对 `UInt` 返回无符号值，对 `SInt` 返回有符号值，类型不同要分别处理。

**练习 2**：`WidthConst` 里的 `VX/VE/VR` 与 `VecWidth` 枚举是什么关系？为什么不直接用枚举？
**答案**：`WidthConst` 是「枚举数值的 Scala Int 镜像」，纯粹方便在 `expect(expWidth.U)` 这种需要 `UInt` 字面量的地方写数字。因为 `regCls` 字段是 `UInt(2.W)`（不是 enum 类型端口），用 `.U` 字面量比较最直接。

---

### 4.3 ChiselEnum 的 peek 技巧与 NpuAssembler 测试范式

#### 4.3.1 概念说明

这是本讲最容易踩的坑，AGENTS.md 把它标红：

[AGENTS.md:121](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L121) —— **「ChiselEnum fields: `poke(op)` works but `expect(op)` does NOT in EphemeralSimulator」**。

直观理解：

- `poke` 是「把一个 Scala 值塞进输入端口」。EphemeralSimulator 知道怎么把 enum 成员翻译成它的整数编码再驱动，所以 `dut.io.ctrl.op.poke(VecOp.vadd)` 合法。
- `expect` 在 EphemeralSimulator 里只对「`UInt`/`SInt`/`Bool` 这类数值端口」有现成实现；对一个 `ChiselEnum` 类型的**输出**端口调 `expect(VecOp.vadd)`，会因为找不到匹配的重载而**编译不过**（或在运行期无法比对）。

因此，要校验一个 enum 输出端口当前等于哪个枚举值，必须绕一下。

#### 4.3.2 核心流程：三种应对方案

针对「enum 输出端口无法 `expect`」，项目里存在三种合法写法（前两种是 AGENTS.md 推荐，第三种是 InstrDecoderSpec 实际采用的务实做法）：

```text
方案 A（推荐，peek 后比 litValue）:
   assert(dut.io.field.peek().litValue == VecOp.vadd.litValue)
   // peek enum 端口拿 BigInt，再与枚举成员的 litValue 比

方案 B（AGENTS.md 文档写法，先转 UInt 再 peek）:
   assert(dut.io.field.asInstanceOf[chisel3.UInt].peek().litValue == VecOp.vadd.litValue)

方案 C（项目最常用：间接验证）:
   不直接校验 enum 端口，而是校验「这个 enum 决定的下游行为」。
   例如译码出的 VecOp 是否正确，由 VALU 功能测试 poke 同一个 op 后看输出对不对来证明。
```

对**纯数值端口**则没有任何限制，`expect` 直接可用：

| 端口类型 | `poke` | `expect` | 备注 |
|:---|:---:|:---:|:---|
| `UInt` / `SInt` | ✅ | ✅ | 最常见，如 `out_vx`、`illegal`(Bool) |
| `Bool` | ✅ | ✅ | 可用 `peek().litToBoolean` 转 Scala Boolean 再 assert |
| `ChiselEnum` | ✅ | ❌ | 需 peek().litValue 间接比，或方案 C |

第二条同样重要的范式演变：**现代 spec 不再直接戳 bundle 子字段，而是用 `NpuAssembler` 构造整条 32 位指令字去 poke `io.instr`**。这样新增 bonus——顺带把译码器路径也测了。AGENTS.md 把它列为约定：

[AGENTS.md:122](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L122) —— 「All new test specs use `isa.NpuAssembler` to build instruction words instead of poking bundle fields directly」。

#### 4.3.3 源码精读：InstrDecoderSpec（现代范式 + enum 处理）

InstrDecoderSpec 是「NpuAssembler + enum 间接验证」的典范。先看它如何构造并 poke 指令字：

[InstrDecoderSpec.scala:12-26](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L12-L26) —— 三个要点：

- 第 12 行 `import NpuAssembler._`：把 `vadd`、`vcvt_f32_s32` 等命名助手引入作用域，直接当函数用。
- 第 25 行 `dut.io.instr.poke((instr.toLong & 0xFFFFFFFFL).U)`：`instr` 是 Scala `Int`，可能 bit31 置位为负，所以先 `.toLong & 0xFFFFFFFFL` 归一到无符号 32 位再 `.U`（与 [AGENTS.md:100](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L100) 一致）。这是与 VALUArithSpec 的 `& 0xFF` 同源但 32 位版本的技巧。
- 第 26 行 `dut.clock.step(0)  // combinational`：译码器是**纯组合**逻辑，不推进时钟，只求值当前组合输出。

接着是对 `illegal`（Bool 端口）的断言——用 `peek().litToBoolean` 转 Scala Boolean 再 `assert`：

[InstrDecoderSpec.scala:27-30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L27-L30) —— 这正是上表里 Bool 端口的处理方式，`peek().litToBoolean` 把硬件 Bool 变成 Scala `Boolean`。后端 spec 也用同一招（见 [NCoreBackendQuantSpec.scala:67-68](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L67-L68)）。

然后看它**怎么处理无法 expect 的 enum 字段**。`family`、`op`、`dtype` 都是 ChiselEnum，于是作者选择「只 expect 数值字段，enum 字段写注释说明由谁间接覆盖」：

[InstrDecoderSpec.scala:32-41](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L32-L41) —— 第 32-37 行只对 `regCls`/`saturate`/`round`/`rd`/`rs1`/`rs2` 这些 `UInt`/`Bool` 字段 `expect`；第 38-40 行的注释明确写了「family/op/dtype 这些 ChiselEnum 由 VALU 功能测试间接验证」。这就是方案 C 的真实落地。

> 对比一下「老派」与「现代」范式：VALUArithSpec 直接 `poke dut.io.ctrl.op`（enum poke，合法），测的是「给定 op，输出对不对」；InstrDecoderSpec 则 `poke` 一条 `NpuAssembler` 拼出的指令字，测的是「给定指令字，译码出来的字段对不对」——后者多覆盖了译码器本身。

#### 4.3.4 代码实践：给 SystolicArray2D(n=2) 写最小测试

**实践目标**：亲手用 EphemeralSimulator 写一个 spec，练习 `simulate / poke / peek().litValue / step / expect`，并用 `peek` 验证 SA 的移位逻辑；同时体会「`out_a/out_b` 是 `SInt` 数值端口，可以 `expect`；如果它换成 enum 就不能 `expect`」。

被测对象 `SystolicArray2D` 是一个纯数据分发网络：`vec_a` 从最左列沿行右传（`reg_h`），`vec_b` 从最顶行沿列下传（`reg_v`），每穿一级延迟一拍。先看它的 IO 和移位寄存器：

[systolicArray.scala:10-20](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L10-L20) —— `out_a`/`out_b` 都是 `Vec(n*n, SInt(nbits.W))`；`reg_h`/`reg_v` 各 `(n-1)*n` 个寄存器、初值 0。

对 `n=2`：`vec_a`/`vec_b` 各 2 个，`out_a`/`out_b` 各 4 个，`reg_h`/`reg_v` 各 2 个。其内部 skew 逻辑：

[systolicArray.scala:27-42](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L27-L42)

**操作步骤**：

1. 在 `src/test/scala/alu/mma/sa/` 下新建 `SystolicArray2DSpec.scala`（示例代码，**非项目原有文件**），键入以下内容：

   ```scala
   // 示例代码（非项目原有文件）：本讲实践产物
   package alu.mma.sa

   import chisel3._
   import chisel3.simulator.EphemeralSimulator._
   import org.scalatest.flatspec.AnyFlatSpec
   import testUtil._   // 可选：用 PrintHelper 打印调试

   class SystolicArray2DSpec extends AnyFlatSpec {

     "SystolicArray2D n=2" should "skew vec_a right and vec_b down by one tick" in {
       simulate(new SystolicArray2D(n = 2, nbits = 8)) { dut =>
         val ph = new PrintHelper()   // 仅在需要打印时使用

         // ---- tick 0：reg_h/reg_v 初值全 0 ----
         dut.io.vec_a(0).poke(10.S); dut.io.vec_a(1).poke(20.S)
         dut.io.vec_b(0).poke(1.S);  dut.io.vec_b(1).poke(2.S)

         // peek litValue 观察当前拍（组合）输出
         val a0 = (0 until 4).map(i => dut.io.out_a(i).peek().litValue.toInt)
         val b0 = (0 until 4).map(i => dut.io.out_b(i).peek().litValue.toInt)
         println("tick0 out_a = " + a0.mkString("[", ",", "]"))
         println("tick0 out_b = " + b0.mkString("[", ",", "]"))

         // out_a/out_b 是 SInt（数值端口），不是 enum —— 可以用 expect
         dut.io.out_a(0).expect(10.S)   // vec_a(0) 直通
         dut.io.out_a(2).expect(20.S)   // vec_a(1) 直通
         dut.io.out_b(0).expect(1.S)
         dut.io.out_b(1).expect(2.S)

         dut.clock.step()               // 推进一拍：reg_h/reg_v 吃进本拍值

         // ---- tick 1：第 1 行/列应携带 tick0 的延迟值 ----
         dut.io.vec_a(0).poke(30.S); dut.io.vec_a(1).poke(40.S)
         dut.io.vec_b(0).poke(3.S);  dut.io.vec_b(1).poke(4.S)

         val a1 = (0 until 4).map(i => dut.io.out_a(i).peek().litValue.toInt)
         val b1 = (0 until 4).map(i => dut.io.out_b(i).peek().litValue.toInt)
         println("tick1 out_a = " + a1.mkString("[", ",", "]"))
         println("tick1 out_b = " + b1.mkString("[", ",", "]"))

         // 验证 skew：第 1 列/行输出 = 上一拍的第 0 列/行
         dut.io.out_a(1).expect(10.S)   // reg_h(0) ← tick0 的 vec_a(0)
         dut.io.out_a(3).expect(20.S)   // reg_h(1) ← tick0 的 vec_a(1)
         dut.io.out_b(2).expect(1.S)    // reg_v(0) ← tick0 的 vec_b(0)
         dut.io.out_b(3).expect(2.S)    // reg_v(1) ← tick0 的 vec_b(1)
       }
     }
   }
   ```

2. 运行：

   ```bash
   tool/test-specific-spec.sh alu.mma.sa.SystolicArray2DSpec
   ```

**需要观察的现象**：控制台打印两行 `tick0` / `tick1` 的 `out_a` / `out_b` 数组。

**预期结果**（由源码逐行推导）：

- tick0：`out_a = [10, 0, 20, 0]`，`out_b = [1, 2, 0, 0]`（第 1 列/行因 `reg` 初值为 0 而输出 0）。
- tick1：`out_a = [30, 10, 40, 20]`，`out_b = [3, 4, 1, 2]`（第 1 列/行恰好是 tick0 喂入的值，证明移位寄存器把数据「错拍」传到了下一级）。

> 待本地验证：上述预期值是基于 [systolicArray.scala:27-42](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L27-L42) 的组合/寄存逻辑逐行手推的结果。请本地运行确认；若你看到 `out_a`/`out_b` 的下标排列与手推不同，以仿真输出为准并回头核对源码里 `out_a(n*i+j)` 的索引方式。

**关键反思（本实践的核心）**：本测试全程没有 `expect` 任何 enum 字段——因为 `out_a`/`out_b` 是 `SInt` 数值端口，`expect` 合法可用。如果 SA 的输出换成某个 `ChiselEnum`，你就必须改用 `peek().litValue` 间接比较（4.3.2 方案 A/B）或改为间接功能验证（方案 C）。这条边界是本讲要建立的最重要心智模型。

#### 4.3.5 小练习与答案

**练习 1**：如果要在 InstrDecoderSpec 里**直接**断言 `dut.io.decoded.family` 等于 `OpFamily.VALU_ARITH`，请写出合法的表达式。
**答案**：用方案 A：
```scala
assert(dut.io.decoded.family.peek().litValue == OpFamily.VALU_ARITH.litValue,
       "family should be VALU_ARITH")
```
或 AGENTS.md 文档写法（方案 B）`dut.io.decoded.family.asInstanceOf[chisel3.UInt].peek().litValue`。注意两边的 `litValue` 都是 `BigInt`，用 `==` 比较即可。

**练习 2**：为什么 `poke((instr.toLong & 0xFFFFFFFFL).U)` 里的 `.toLong` 不能省？
**答案**：`instr` 是 Scala `Int`，当 bit31 为 1 时它是负数；`.U` 在某些情况下会把负 Int 当成非法字面量或解释错符号。`.toLong & 0xFFFFFFFFL` 先把它升成 `Long` 再掩码到无符号 32 位，保证位模式正确。参见 [AGENTS.md:100](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L100)。

**练习 3**：把 `dut.clock.step(0)` 改成 `dut.clock.step(1)` 会对 InstrDecoderSpec 造成什么影响？
**答案**：译码器是纯组合逻辑（无寄存器），`step(0)` 只求值组合输出、不推进时钟，是正确用法。改成 `step(1)` 虽然不会让译码结果出错（组合输出与拍数无关），但会无谓推进一拍时钟，且语义上误导读者以为译码器有时序，属于不良写法。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个端到端的小任务。

**任务**：在 4.3.4 的 `SystolicArray2DSpec` 基础上，做三件事——

1. **接入 `testUtil`**：把 tick0/tick1 里手写的 `map(...).mkString` 打印，换成调用 `ph.printVectorChisel`（注意它要的是 `Vec[SInt]`，而 `out_a` 是 `Vec[SInt]`，正好匹配；但你只能逐段传 `n` 个元素，所以需要思考如何把 `n*n` 长度的 `out_a` 切成 n 段 n 元向量来打印，或者退回到手写循环——体会共享工具的适用边界）。
2. **扩到第 3 拍**：继续 `poke` 一组新输入并 `step`，预测第 3 拍 `out_a`/`out_b` 的值（提示：此时 `reg_h`/`reg_v` 已被 tick1 的值覆盖，所以第 1 列/行输出的是 tick1 的值，而不是 tick0 的——移位寄存器只存「上一拍」，不是「历史」）。
3. **回答 enum 问题**：如果你的同事坚持要在测试里写 `dut.io.out_a.expect(...)` 那样的「对 enum 端口 expect」，你会用本讲的哪条结论纠正他？请引用 AGENTS.md 的原文。

**自检清单**：

- [ ] 你的 spec 能用 `tool/test-specific-spec.sh alu.mma.sa.SystolicArray2DSpec` 跑通。
- [ ] 你能用一句话解释「为什么 `poke(enum)` 合法但 `expect(enum)` 不合法」。
- [ ] 你能说出 `peek().litValue` 在 `SInt` 与 `UInt` 上分别返回有符号/无符号 `BigInt`。
- [ ] 你理解「现代 spec 用 NpuAssembler poke 指令字」比「老 spec 直接戳 bundle 子字段」多覆盖了译码器路径。

## 6. 本讲小结

- chisel-npu 全部 21 个 spec 用的是 Chisel 6 自带的 `chisel3.simulator.EphemeralSimulator`，**不是** `chiseltest`（build.sbt 里的 chiseltest 依赖是声明但未被 import 的遗留物）。
- 测试范式是 `simulate(new Module) { dut => poke / step / peek / expect }`；`step(0)` 专门测纯组合逻辑，`step()`/`step(1)` 推进寄存器一拍。
- 共享工具 `testUtil`（`PrintHelper`、`WidthConst`）封装了「把仿真 `Vec` 打印成可读矩阵」和「与枚举对齐的宽度常量」，惯例取值写法是 `dut.io.x(i).peek().litValue.toInt`。
- **核心坑**：`ChiselEnum` 端口 `poke` 合法、`expect` 非法；要校验 enum 输出须用 `peek().litValue` 间接比（或 `asInstanceOf[UInt]`），或干脆走「间接功能验证」（InstrDecoderSpec 的实际做法）。
- 现代 spec 用 `NpuAssembler` 构造 32 位指令字去 `poke io.instr`，顺带覆盖译码器；处理负数指令字用 `(instr.toLong & 0xFFFFFFFFL).U`。
- 单测快捷脚本 `tool/test-specific-spec.sh <全限定类名>` 透传给容器内 `sbt "testOnly ..."`，是日常只跑一个 spec 的标准入口。

## 7. 下一步学习建议

下一讲 [u9-l2 测试套件组织与 CI 流水线](u9-l2-test-suite-and-ci.md) 会从「单条 spec 怎么写」上升到「整个测试套件怎么组织、CI 怎么跑」：

- 读 `src/test/scala` 的镜像式目录组织，理解单元测试（VALU/PE/SA）与集成测试（`NCoreBackend*Spec`）的分层。
- 对比 `tool/test-specific-spec.sh` 跑单测与 `sbt test` 跑全量的耗时差异。
- 阅读 `.github/workflows/actions.yml`，看 CI 在 `fangruil/chisel-dev:amd64` 里执行 `sbt run` + `sbt test` 的确切命令序列。

如果你想立刻巩固本讲的 enum 技巧，建议再去读一遍 [VALUReduceSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUReduceSpec.scala)，看它如何用 `peek().litValue` 验证归约结果的「广播不变量」（所有 lane 的 `out_vr(i).litValue` 必须相等）。
