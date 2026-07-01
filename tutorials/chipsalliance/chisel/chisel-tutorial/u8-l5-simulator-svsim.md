# 仿真与 svsim / simulator

> 本讲覆盖高级层最后一块拼图：你写好的 Chisel 模块，是如何被「跑起来」验证行为的。我们会沿着调用链自顶向下走完一遍：从用户的一行 `simulate(new MyMod){...}`，到 Verilator/VCS 编译出的仿真可执行文件，再到进程间用文本协议一拍一拍驱动它。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 **ChiselSim（`chisel3.simulator`）** 与 **`svsim`** 两个包的职责边界——前者 Chisel 感知、后者仿真器感知但 Chisel 无关。
- 看懂 `trait Simulator[T <: Backend]` 如何把「细化 → 编译 → 运行」串成一条流水线，并理解 `simulate` 内部的几大步骤。
- 理解 `svsim.Simulation` 如何启动一个仿真子进程，并用 `Controller` 上的命令/消息协议（Command/Message）驱动它。
- 解释 `Backend` 抽象如何把 Verilator 与 VCS 这两种差异巨大的仿真器收敛到同一个 `generateParameters` 接口。
- 掌握 `PeekPokeAPI` 的 `poke`/`peek`/`expect`/`step` 如何映射到底层的 `SetBits`/`GetBits`/`Tick` 命令，以及它的「延迟求值」机制。
- 独立画出从 `Simulator.run` 到 Verilator 后端的完整调用链。

## 2. 前置知识

本讲默认你已掌握：

- **elaboration（细化）** 与 ChiselStage 生成 SystemVerilog（见 [u1-l4](u1-l4-first-module-and-verilog.md)、[u5-l4 CIRCT 集成](u5-l4-circt-firtool.md)）——仿真前必须先把模块编译成可被仿真器接受的 SystemVerilog。
- `Module` / `RawModule` 的区别（见 [u3-l1](u3-l1-module-rawmodule.md)）——`Module` 自带隐式 `clock`/`reset`，这决定了仿真时的复位与时钟。
- `Data` 端口、方向与 `Record`/`Vec` 的递归结构（见 [u2-l1](u2-l1-data-hierarchy.md)、[u2-l3](u2-l3-bundle.md)）——仿真激励是逐个叶子端口施加的。
- Scala 的 `ProcessBuilder`（启动外部进程）与 `DynamicVariable`（线程局部上下文）的基础概念。

几个术语先对齐：

- **DUT（Design Under Test）**：被测设计，即你的 Chisel 模块综合出的电路。
- **Testbench（测试台）**：包裹 DUT 的一层 SystemVerilog 代码，负责实例化 DUT 并暴露 DPI 接口供宿主程序驱动。
- **DPI（Direct Programming Interface）**：SystemVerilog 与 C 互调的标准（IEEE 1800）。`svsim` 用它让 Scala 与仿真器交换端口值。
- **激励（stimulus）**：测试过程中你向 DUT 输入端口施加的值序列与采样时机。
- **ChiselSim**：Chisel 7 起内置的仿真前端（取代了外部的 ChiselTest 库）。

## 3. 本讲源码地图

本讲涉及的关键源码文件与各自职责：

| 文件 | 所属子项目 | 作用 |
|------|-----------|------|
| `src/main/scala/chisel3/simulator/Simulator.scala` | `chisel`（整合层） | 高层仿真入口：`trait Simulator`，提供 `simulate`/`simulateTests`，编排细化→编译→运行 |
| `src/main/scala/chisel3/simulator/package.scala` | `chisel` | 定义 `ChiselSim`、`SimulatedModule`、用 `ChiselWorkspace` 把 Chisel 模块喂给 `svsim` |
| `src/main/scala/chisel3/simulator/SimulatorAPI.scala` | `chisel` | 用户面 API：`simulate`/`simulateRaw`/`simulateTest`，封装 `Simulator` |
| `src/main/scala/chisel3/simulator/PeekPokeAPI.scala` | `chisel` | `poke`/`peek`/`expect`/`step` 的类型类实现 |
| `src/main/scala/chisel3/simulator/HasSimulator.scala` | `chisel` | 选 Verilator 还是 VCS 的类型类（默认 Verilator） |
| `svsim/src/main/scala/Simulation.scala` | `svsim` | 仿真进程封装：`Simulation`、`Controller`、Command/Message 协议 |
| `svsim/src/main/scala/Workspace.scala` | `svsim` | 工作目录管理、testbench/DPI 生成、`compile` 调用 backend |
| `svsim/src/main/scala/Backend.scala` | `svsim` | `trait Backend` 抽象与公共编译设置 |
| `svsim/src/main/scala/verilator/Backend.scala` | `svsim` | Verilator 后端实现：拼装 `verilator` 命令行 |
| `svsim/src/main/scala/vcs/Backend.scala` | `svsim` | VCS 后端实现 |

> 记住一条代码归属原则（承接 [u1-l3](u1-l3-repository-layout.md)）：`svsim` 子项目完全不知道 Chisel 的存在，它只认「SystemVerilog 源文件 + 端口元信息」；所有 Chisel 特有的逻辑都在 `chisel3.simulator` 里，通过隐式类「注入」到 `svsim` 的扩展点上。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：**Simulator 抽象 → svsim.Simulation → Backend → PeekPokeAPI → 综合调用链**。

### 4.1 Simulator：把仿真拆成「细化—编译—运行」三段

#### 4.1.1 概念说明

仿真的本质是：把你的 Chisel 模块变成一个**可执行的仿真程序**，然后**驱动它**若干拍、**观察**输出。ChiselSim 把这件事拆成清晰的三段：

1. **细化（elaborate）**：运行模块构造体，经 `ChiselStage` + firtool 产出 SystemVerilog 源文件（[u5-l4](u5-l4-circt-firtool.md) 已讲过这条链路）。
2. **编译（compile）**：把 SystemVerilog 喂给 Verilator/VCS，编出 `simulation` 可执行文件。
3. **运行（run）**：启动这个可执行文件，通过 `Controller` 一拍一拍地 poke/peek。

`trait Simulator[T <: Backend]` 就是这三段的编排者，`T` 是它绑定的后端类型。

#### 4.1.2 核心流程

```
simulate(module, ...)(body)
  │
  ├─ new Workspace(path, ...)            # 建工作目录
  ├─ workspace.reset()                   # 清空旧产物
  ├─ workspace.elaborateGeneratedModule  # 第①段：产出 SystemVerilog + 推断端口
  │
  └─ _simulate(workspace, elaboratedModule, settings)(body)
       │
       ├─ 更新 commonCompilationSettings  # include 目录、库、plusargs、layer 过滤…
       ├─ workspace.generateAdditionalSources   # 生成 testbench.sv + DPI bridge
       │
       ├─ workspace.compile(backend)(...) # 第②段：调 backend → make → 得到 Simulation
       │
       └─ simulation.runElaboratedModule(...){ module =>  # 第③段：运行
            body(module)
            module.completeSimulation()
          }
```

关键设计：**细化与编译是两段独立的、可能很慢的工序**，因此它们的开始/结束时间都被记录下来，包进一个 `BackendInvocationDigest` 返回——这样上层可以分别度量「编译耗时」与「运行耗时」。

#### 4.1.3 源码精读

`Simulator` trait 声明了它持有什么：一个 `backend`、一个工作目录 `workspacePath`，以及公共与后端特有的编译设置：

- [Simulator.scala:94-103](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/Simulator.scala#L94-L103) —— `trait Simulator[T <: Backend]` 的抽象字段。注意它是 `trait`，需要用户/类型类给出具体实例（见 4.3）。

`simulate` 方法是公开入口，把三段串起来：

- [Simulator.scala:150-172](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/Simulator.scala#L150-L172) —— `final def simulate`。先建并 `reset` Workspace，调用 `elaborateGeneratedModule`（第①段），再交给私有 `_simulate`。

真正干活的是 `_simulate`，它的「编译」与「运行」两段：

- [Simulator.scala:329-348](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/Simulator.scala#L329-L348) —— 第②段：`workspace.compile(backend)(...)`。`try/catch` 把编译失败包成 `CompilationFailed`，不抛异常而是返回（这样上层能区分「编译失败」与「运行失败」）。
- [Simulator.scala:362-371](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/Simulator.scala#L362-L371) —— 第③段：`simulation.runElaboratedModule(...){ module => body(module); module.completeSimulation() }`。注意末尾的 `completeSimulation()`——它保证所有还在缓冲里的命令被冲刷进仿真进程（见 4.4）。

运行结束后还有一步关键后处理 `postProcessLog`：

- [Simulator.scala:109-133](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/Simulator.scala#L109-L133) —— 扫描 `simulation-log.txt`，用 `backend.assertionFailed` 这个正则挑出「断言失败」行。源码注释解释了为什么必须做这一步：svsim 在失败时只会返回一个含糊的 `UnexpectedEndOfMessages`，而 VCS 不会在断言失败时立即退出。

> 把 digest 与异常类型放在一起看：编译失败 → `CompilationFailed`；运行期断言失败 → `Exceptions.AssertionFailed`；超时 → `Exceptions.Timeout`；测试桩主动置失败 → `Exceptions.TestFailed`。这套分类是 `simulateTests` 汇总成 `SimulationOutcome` 的依据（[Simulator.scala:202-216](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/Simulator.scala#L202-L216)）。

#### 4.1.4 代码实践

**实践目标**：在源码里定位三段工序的边界。

**操作步骤**：

1. 打开 `src/main/scala/chisel3/simulator/Simulator.scala`。
2. 在 `simulate` 方法中，找到三行：`workspace.elaborateGeneratedModule(...)`、`workspace.compile(backend)(...)`、`simulation.runElaboratedModule(...)`。
3. 阅读它们各自的注释。

**需要观察的现象**：第①段调用的返回类型是 `ElaboratedModule[T]`；第②段返回 `Simulation`；第③段返回用户 `body` 的结果 `U`。三者类型清晰递进。

**预期结果**：你能用一句话分别概括三段的输入/输出，例如「`elaborateGeneratedModule` 吃 `() => RawModule`，吐 `ElaboratedModule`（SystemVerilog + 端口表）」。

#### 4.1.5 小练习与答案

1. **问**：为什么 `compile` 失败时 `Simulator` 选择返回 `CompilationFailed(error)` 而不是直接 `throw`？
   **答**：为了让上层能把「编译失败」与「运行期失败」区分开——前者往往是环境/代码问题（如 Verilator 不在 PATH），后者是真正的功能错误。`BackendInvocationDigest.result` 会在 `CompilationFailed` 分支里再抛出（[Simulator.scala:82-85](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/Simulator.scala#L82-L85)），但在此之前调用者有机会拿到编译耗时等元信息。

2. **问**：`Simulator` 是 `trait`，谁来实现它的 `backend`/`workspacePath`？
   **答**：由 `HasSimulator` 类型类提供的匿名子类实现，见 [HasSimulator.scala:48-55](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/HasSimulator.scala#L48-L55)（4.3 节展开）。

### 4.2 svsim.Simulation：一个仿真进程 + 一套文本协议

#### 4.2.1 概念说明

`svsim` 把「仿真器」抽象成一个**子进程**：编译产物是一个可执行文件（叫 `simulation`），`svsim` 用 `ProcessBuilder` 启动它，然后通过它的**标准输入/标准输出**收发指令。

这是一种非常朴素但稳健的设计——只要仿真器（Verilator/VCS）能编出一个带 `main` 的可执行文件，并愿意从 stdin 读命令、向 stdout 写结果，就能被驱动。`svsim` 不依赖任何仿真器专有 API，只靠这套自定的**命令/消息协议**。

`Simulation` 类就是这个子进程的 Scala 侧封装；`Controller` 是协议的读写器；`Port` 是单个端口的句柄。

#### 4.2.2 核心流程

```
simulation.run(verbose, traceEnabled, ...){ controller =>
   // 用户的 body 在这里，持有 controller
   controller.port("io_a").set(2)      # → 发 SetBits 命令
   controller.run(1)                    # → 发 Run(1) 命令，推进 1 个时间步
   val v = controller.port("io_out").get(false)  # → 发 GetBits 命令，读回值
}
```

协议两端：

| 方向 | 类型 | 取值（单字符码） | 含义 |
|------|------|-----------------|------|
| Scala → sim | Command | `D` Done / `L` Log / `G` GetBits / `S` SetBits / `R` Run / `T` Tick / `W` Trace | 驱动仿真 |
| sim → Scala | Message | `r` Ready / `e` Error / `k` Ack / `b` Bits / `l` Log | 仿真回应 |

**惰性批处理**是核心优化：返回 `Unit` 的方法（如 `set`、`run`）只把命令写进缓冲区、不立即读回应；只有返回值的方法（如 `get`）才会冲刷缓冲并等待对应消息。这能把多条命令合并成一批读写，显著降低进程间往返开销。

**命令格式举例**（详见 `sendCommand`，源码注释指向 `simulation-driver.cpp`）：

- `SetBits` 写成 `S <id> <十六进制值>`；
- `Run(n)` 写成 `R <n 的十六进制>`，表示推进 `n` 个 timestep；
- `Tick` 最复杂，编码时钟的高低电平持续时长与最大周期：`T <id> <in>,<out>-<per>*<max>`。

#### 4.2.3 源码精读

`Simulation` 类持有可执行文件名、设置、工作目录与模块端口元信息：

- [Simulation.scala:10-15](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L10-L15) —— 构造参数。它由 `Workspace.compile` 在编译成功后 new 出来。

`run` 方法负责进程生命周期：

- [Simulation.scala:28-53](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L28-L53) —— 用 `ProcessBuilder(command)` 启动仿真可执行文件，设置工作目录与环境变量（`SVSIM_EXECUTION_SCRIPT` 等），再用 stdin/stdout 流构造 `Controller`。
- [Simulation.scala:92-101](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L92-L101) —— 收尾：`process.waitFor()` + `destroyForcibly()`，并检查 `exitValue() != 0` 抛 `Nonzero exit status`。

命令与消息的编解码都在 `Controller`：

- [Simulation.scala:256-331](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L256-L331) —— `sendCommand`：用单字符码 + 空格分隔把每条命令序列化成一行文本写进 stdin。
- [Simulation.scala:143-217](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L143-L217) —— `readNextAvailableMessage`：从 stdout 读一个字符判别消息类型，再按格式解析（`Bits` 的位数用 8 位十六进制前缀给出，值为十六进制串）。

批处理与防死锁逻辑：

- [Simulation.scala:221-233](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L221-L233) —— `completeInFlightCommands()`：冲刷缓冲，逐个匹配已排队的期望消息。
- [Simulation.scala:246-254](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L246-L254) —— `expectNextMessage`：默认把期望入队（延迟匹配），但当 `conservativeCommandResolution` 为真、或队列深度超过 1000 时强制立即处理。注释指明这是为了规避死锁（issue #5128）——因为子进程的回应缓冲也是有限的。

单端口操作在 `Port`：

- [Simulation.scala:396-415](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L396-L415) —— `set`（发 SetBits，要求 `isSettable`，即输入端口）与 `get`（发 GetBits，要求 `isGettable`）。`require` 会拦截「试图 poke 一个输出端口」这类错误。
- [Simulation.scala:417-423](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L417-L423) —— `tick`：用 `Tick` 命令驱动一个时钟。这正是 `clock.step()` 在底层的落点。

> 协议的 C 语言对端是 `simulation-driver.cpp`——一个被打包进 jar 的资源文件，由 `Workspace.generateAdditionalSources` 拷进工作目录（[Workspace.scala:351](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L351)）。它就是仿真可执行文件的 `main`，负责读命令、调 DPI 读写端口、推进仿真、写消息。

#### 4.2.4 代码实践

**实践目标**：理清协议两端，体会「命令写、消息读」的对应关系。

**操作步骤**：

1. 在 `Simulation.scala` 的 `Command` 与 `Message` 两个 object（[Simulation.scala:363-390](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L363-L390)）里，列出全部命令与消息种类。
2. 对照 `sendCommand` 的 `match` 分支，为每种命令写出它在线路上的文本形态（如 `SetBits("3", 5)` → `S 3 5`）。
3. 找到 `Controller` 构造体末尾的 `expectNextMessage { case Ready => }`（[Simulation.scala:360](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Simulation.scala#L360)）——它说明仿真进程启动后的第一条消息必然是 `Ready`。

**需要观察的现象**：`set` 的返回是 `Ack`（`k ack`），而 `get` 的返回是 `Bits`（`b` + 位数 + 值）。返回值类型决定了消息类型。

**预期结果**：你能解释「为什么 `set` 不立即返回值却能批量、而 `get` 必须停下来等」——因为 `Ack` 是固定形态可入队延迟匹配，`Bits` 带回具体值必须当场取走。待本地验证：若你能在工作目录里找到 `execution-script.txt`（运行时记录的命令脚本），可对照其内容与协议格式。

#### 4.2.5 小练习与答案

1. **问**：`port("io_out").get()` 抛「cannot get port ... not gettable」会发生在什么端口上？
   **答**：发生在非 `isGettable` 的端口上——即 `Output` 之外、或被 firtool 优化掉的零宽端口。`isGettable` 由 ChiselSim 在推断端口时按方向打标（见 4.4 / [package.scala:269-274](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L269-L274)）：`Input` 可读可写、`Output` 只可读。

2. **问**：为什么 `expectNextMessage` 在队列超过 1000 时要强制立即处理？
   **答**：避免死锁。命令和消息在两个管道里异步流动，子进程侧的回应缓冲是有限的；若 Scala 侧无限入队而不消费，子进程写出阻塞，最终双方互相等待。详见源码引用的 issue #5128。

### 4.3 Backend 抽象：Verilator 与 VCS 的收敛点

#### 4.3.1 概念说明

Verilator（开源、编译成 C++ 再用 g++ 链接）与 VCS（Synopsys 商业、原生商业仿真器）的命令行、文件组织、追踪格式天差地别。`svsim` 用 `trait Backend` 把这些差异收敛到一个方法：

```
def generateParameters(...): Backend.Parameters
```

每个后端实现这个方法，返回**编译器调用**与**仿真器调用**的命令行参数。`Workspace.compile` 拿到这些参数后，生成一个 Makefile 并跑 `make`——这样无论后端是什么，编译入口都是统一的 `make simulation`。

后端还需要实现两个小东西：`escapeDefine`（处理宏定义转义差异）与 `assertionFailed: Regex`（识别日志里的断言失败行，供 4.1 的 `postProcessLog` 使用）。

#### 4.3.2 核心流程

```
workspace.compile(backend)(tag, common, backendSpecific, ...)
  │
  ├─ backend.generateParameters(          # 后端把设置翻译成命令行
  │     outputBinaryName = "simulation",
  │     topModuleName = "svsimTestbench",  # 注意：顶层是 testbench，不是你的 DUT！
  │     ...)
  │
  ├─ 写 sourceFiles.F（参与编译的源文件清单）
  ├─ 生成 Makefile（simulation / replay 两个目标）
  │
  └─ ProcessBuilder("make", "-C", workdir, "simulation").start()
        → Verilator/VCS 编译 → 产出 ./simulation 可执行文件
        → 返回 new Simulation("simulation", ...)
```

**反直觉点 1**：传给后端的 `topModuleName` 是 `svsimTestbench`（[Workspace.scala:22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L22)），**不是你的模块名**。你的 DUT 被 testbench 实例化在里面，实例名叫 `dut`（[Workspace.scala:13](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L13)）。

**反直觉点 2**：编译真正执行的是 `make`，而非直接调 Verilator。Makefile 同时提供 `replay` 目标，让你能在失败后手动重跑/重放调试（[Workspace.scala:460-528](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L460-L528)）。

#### 4.3.3 源码精读

Backend 抽象本体：

- [Backend.scala:253-274](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Backend.scala#L253-L274) —— `trait Backend`。`type CompilationSettings` 是路径依赖类型，强制每个后端自带一份强类型的设置（如 Verilator 的 `TraceStyle`、VCS 的 `XProp`）。

返回值结构：

- [Backend.scala:290-305](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Backend.scala#L290-L305) —— `Backend.Parameters`，含 `compilerPath`、`compilerInvocation`（编译命令行）、`simulationInvocation`（运行时 plusargs）。字段都是 `private[svsim]`，意味着这是内部契约、未来可能变。

Verilator 后端：

- [verilator/Backend.scala:352-360](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/verilator/Backend.scala#L352-L360) —— `initializeFromProcessEnvironment()`：执行 `which verilator` 找到可执行文件路径，找不到就抛「verilator not found on the PATH」。
- [verilator/Backend.scala:362-364](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/verilator/Backend.scala#L362-L364) —— `final class Backend(executablePath) extends svsim.Backend`。
- [verilator/Backend.scala:384-392](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/verilator/Backend.scala#L384-L392) —— Verilator 的基础调用：`--cc --exe --build -o ../simulation --top-module svsimTestbench --Mdir verilated-sources --assert`。`--cc` 让 Verilator 生成 C++，`--exe`+`--build` 编出可执行文件，`--assert` 开启断言检查。
- [verilator/Backend.scala:494](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/verilator/Backend.scala#L494) —— `assertionFailed = "^.*Assertion failed in.*".r`：这就是 4.1 节 `postProcessLog` 用来挑断言失败行的正则。

谁造出 `Simulator[Backend]` 的实例？是 `HasSimulator` 类型类：

- [HasSimulator.scala:43-56](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/HasSimulator.scala#L43-L56) —— `simulators.verilator(...)`：返回一个匿名 `Simulator[svsim.verilator.Backend]`，其 `backend` 由 `Backend.initializeFromProcessEnvironment()` 给出，`tag = "verilator"`，`workspacePath` 取自测试目录。
- [HasSimulator.scala:79](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/HasSimulator.scala#L79) —— `implicit def default: HasSimulator = simulators.verilator()`：低优先级默认后端就是 Verilator，所以你什么都不 import，默认用 Verilator。

> `tag` 决定了工作目录名 `workdir-<tag>`（[Workspace.scala:395](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L395)）。这让你能在同一 workspace 上用多个后端编译而不冲突。

#### 4.3.4 代码实践

**实践目标**：看清 Verilator 命令行是怎么拼出来的。

**操作步骤**：

1. 打开 `svsim/src/main/scala/verilator/Backend.scala` 的 `generateParameters`（[第 365 行起](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/verilator/Backend.scala#L365)）。
2. 找到这几段：基础调用（`--cc --exe --build`）、`traceStyle`（`--trace`/`--trace-fst`）、`coverageSettings`、`-CFLAGS`（含 `-D SVSIM_ENABLE_VERILATOR_SUPPORT` 等宏）。
3. 注意末尾返回的 `Backend.Parameters`：`compilerPath = executablePath`（即 `which verilator` 的结果）。

**需要观察的现象**：`-CFLAGS` 里的宏（如 `SVSIM_ENABLE_VERILATOR_TRACE`）会与 testbench 里的 `` `ifdef `` 对应（见 [Backend.scala:314-335](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Backend.scala#L314-L335) 的 `HarnessCompilationFlags`）——后端选择决定了哪些 testbench 代码会被编译进来。

**预期结果**：你能解释「为什么开波形要在两处配合」——既要在 `CompilationSettings.traceStyle` 里让 Verilator 加 `--trace`，又会自动定义 `SVSIM_ENABLE_VCD_TRACING_SUPPORT` 宏让 testbench 的 `$dumpvars` 生效。

#### 4.3.5 小练习与答案

1. **问**：为什么 `topModuleName` 传的是 `svsimTestbench` 而不是用户模块名？
   **答**：因为仿真顶层是 `svsim` 自动生成的 testbench，它实例化你的 DUT（实例名 `dut`）并承担 DPI 桥接。仿真器的顶层必须是这个 testbench，否则 DPI 导出函数没有正确的层次作用域。

2. **问**：想换用 VCS，用户代码要改什么？
   **答**：只需 `import chisel3.simulator.HasSimulator.simulators.vcs`，把隐式 `HasSimulator` 从默认的 Verilator 切到 VCS（[HasSimulator.scala:59-71](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/HasSimulator.scala#L59-L71)）。其余代码（`simulate`、`poke`、`step`）完全不变——这正是 Backend 抽象的价值。

### 4.4 PeekPokeAPI：用户层的激励与采样

#### 4.4.1 概念说明

`svsim` 的 `Controller`/`Port` 是底层、原始的：要按字符串名取端口、用 `BigInt` 传值。`chisel3.simulator.PeekPokeAPI` 在它之上提供了一套**类型安全、Chisel 感知**的 DSL：直接对 `dut.io.a`（一个 `UInt`）调用 `poke(2.U)`、`peek()`、`expect(5.U)`，对 `dut.clock` 调用 `step()`。

它通过**隐式类（类型类）**实现：`TestableUInt`、`TestableBool`、`TestableClock`、`TestableRecord`、`TestableVec` 等，把「可测」能力附加到每种 `Data` 子类型上。聚合类型（`Record`/`Vec`）的 poke/peek 会递归下钻到叶子端口，对每个叶子调底层的 `set`/`get`。

它还内置了一套**延迟求值**的小状态机：连续多次 `poke` 不会立即推进仿真，而是在下一次 `peek` 前自动 `run(0)` 一次，让组合逻辑 settle。

#### 4.4.2 核心流程

```
dut.io.a.poke(2.U)        # TestableUInt.poke
  └─ simulatedModule.willPoke()       # 置标志：下次 peek 前要 run(0)
  └─ simulationPort.set(2)            # → Controller SetBits

dut.clock.step(1)         # TestableClock.step
  └─ simulatedModule.willEvaluate()   # 清标志
  └─ simulationPort.tick(period/2, 1, 0, 1, None)   # → Controller Tick

dut.io.sum.expect(5.U)    # TestableUInt.expect
  └─ check(...)  └─ simulationPort.check{ value => 比较值 }   # → Controller GetBits + 回调
```

`step` 的时间换算：设时钟 `period = 10`，则每个半周期 `timestepsPerPhase = period/2 = 5`，跑 `cycles` 个完整周期等于推进 \( 2 \times \text{timestepsPerPhase} \times \text{cycles} = \text{period} \times \text{cycles} \) 个 timestep。

#### 4.4.3 源码精读

类型类总入口与具体隐式类：

- [PeekPokeAPI.scala:942-962](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L942-L962) —— `trait PeekPokeAPI`：一组 `implicit def` 把每种 `Data` 转成对应 `Testable*`。混入这个 trait（如 `ChiselSim`）就获得了全部 poke/peek 语法。
- [PeekPokeAPI.scala:647-649](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L647-L649) —— `TestableUInt`：`encode` 把读回的 `BigInt` 还原成 `UInt` 字面量。

叶子端口的 poke/peek 落点（在 `TestableElement`）：

- [PeekPokeAPI.scala:316-328](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L316-L328) —— `peekValue` 调 `willPeek()` 再 `simulationPort.get(...)`；`poke(value)` 调 `willPoke()` 再 `simulationPort.set(value)`。这两个 `will*` 就是延迟求值的钩子。

时钟步进：

- [PeekPokeAPI.scala:600-618](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L600-L618) —— `TestableClock.step(cycles, period)`：先 `willEvaluate()`，再发 `Tick`。`cycles == 0` 时退化成 `controller.run(0)`（只让组合逻辑 settle，不推进时钟）。

延迟求值的状态机（在 `AnySimulatedModule`）：

- [package.scala:79-103](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L79-L103) —— `willPoke` 置 `evaluateBeforeNextPeek = true`；`willPeek` 若发现该标志为真，就先 `controller.run(0)` 再清。这就是「poke 后第一次 peek 会自动结算」的实现。

`SimulatedModule` 如何把 Chisel 的 `Data` 映射到 `Simulation.Port`：

- [package.scala:46-74](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L46-L74) —— `SimulatedModule` 持有 `elaboratedModule` 与 `controller`；`port(data)` 用一张预先建好的 `Data → Simulation.Port` 表查（端口在细化时按名字登记）。注意它会先 `reifyIdentityView(data)`，所以 DataView（见 [u8-l2](u8-l2-dataview.md)）只要是「落到单一 Data」的恒等视图也能 poke。

当前模块的线程局部上下文：

- [package.scala:104-111](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L104-L111) —— `AnySimulatedModule.current` 由 `DynamicVariable` 提供。`expect` 这类不带显式模块参数的方法，就是靠它找到「当前正在跑的模块」。`require` 禁止嵌套仿真。

#### 4.4.4 代码实践

**实践目标**：用一个最小模块跑通 poke → step → expect，并验证延迟求值。

> 这是本讲**可运行**的实践。前置条件：机器上装有 Verilator（在 `PATH` 中能 `which verilator`）。

**操作步骤**：

1. 新建一个 ScalaTest 测试文件（示例代码，非项目原有文件）：

   ```scala
   // 示例代码：src/test/scala/chiselTests/simulator/MyAdderSpec.scala
   package chiselTests.simulator

   import chisel3._
   import chisel3.simulator.scalatest.ChiselSim
   import org.scalatest.funspec.AnyFunSpec
   import org.scalatest.matchers.must.Matchers

   class MyAdder extends Module {
     val io = IO(new Bundle {
       val a   = Input(UInt(8.W))
       val b   = Input(UInt(8.W))
       val sum = Output(UInt(8.W))   // 一拍延迟的 a+b
     })
     io.sum := RegNext(io.a + io.b)
   }

   class MyAdderSpec extends AnyFunSpec with ChiselSim with Matchers {
     it("adds one cycle later") {
       simulate(new MyAdder) { dut =>
         dut.io.a.poke(2.U)
         dut.io.b.poke(3.U)
         dut.clock.step()        // 推进一拍，寄存器更新
         dut.io.sum.expect(5.U)  // 2+3 在上一拍被采样
       }
     }
   }
   ```

2. 运行：`./mill chisel[].test.chiselTests.simulator.MyAdderSpec`（命令待本地验证，具体目标名取决于 mill 任务命名）。
3. 在生成的工作目录（通常在 `test-run-dir/...` 下）找到 `workdir-verilator/`，查看 `compilation-log.txt`、`simulation-log.txt`、`Makefile`。

**需要观察的现象**：

- `RegNext` 让求和延迟一拍，所以必须 `step()` 之后再 `expect`，顺序不能颠倒。
- 工作目录里能看到完整的 SystemVerilog（`primary-sources/`）、生成的 `testbench.sv`、`c-dpi-bridge.cpp`、`sourceFiles.F`、`Makefile`。

**预期结果**：测试通过；若把 `expect(5.U)` 改成 `expect(6.U)`，会抛 `FailedExpectationException`，其消息由 [PeekPokeAPI.scala:226-234](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L226-L234) 的 `dramaticMessage` 渲染。

> 若环境没有 Verilator：退化为「源码阅读型实践」——只写代码不运行，重点阅读 `simulate` 调用栈，标注 `poke(2.U)` 最终走到 [PeekPokeAPI.scala:325](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L325) 的 `simulationPort.set(value)`。

#### 4.4.5 小练习与答案

1. **问**：下面代码为何读到的 `sum` 可能不是预期的「当前 a+b」？
   ```scala
   dut.io.a.poke(2.U)
   dut.io.sum.peek()   // 读到的是？
   ```
   **答**：取决于 `sum` 是否是寄存器输出。若 `sum := io.a`（组合），由于延迟求值，`poke` 后第一次 `peek` 会自动 `run(0)` 结算组合逻辑，读到 2；若 `sum := RegNext(io.a)`（时序），`peek` 读到的是上一拍的旧值，需 `clock.step()` 后才能看到新值。

2. **问**：`dut.io.sum.expect(5.U)` 里的 `5.U` 为什么必须是字面量（`isLit`）？
   **答**：因为 `expect` 在仿真侧只拿到一个 `BigInt`（端口的位串），需要一个确定的期望值来比较；`require(expected.isLit)`（[PeekPokeAPI.scala:512](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/PeekPokeAPI.scala#L512)）确保你给的是字面量而非未绑定的硬件类型。

### 4.5 综合调用链：从 `simulate` 到 Verilator 的全链路

#### 4.5.1 概念说明

前 4 节分别看了零件。这一节把它们拧成一根线，回答实践任务的核心问题：**「从 `Simulator.run` 到 Verilator 后端，到底调用了哪些东西？」**

关键在于理解 `chisel3.simulator` 与 `svsim` 之间的**桥**：`svsim` 定义了一个空壳扩展点 `Workspace.elaborate(moduleInfo)`，注释明说「真正做细化的包（如 Chisel）会用隐式类重载它」。Chisel 侧正是用 `ChiselWorkspace` 这个隐式类（[package.scala:130](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L130)）把细化、端口推断、SystemVerilog 生成注入进去。

#### 4.5.2 核心流程（完整调用链）

```
用户: simulate(new MyAdder){ dut => dut.io.a.poke(2.U); dut.clock.step(); dut.io.sum.expect(5.U) }
   │  (SimulatorAPI.simulate, SimulatorAPI.scala:81)
   │  → ResetProcedure.module(...)(dut) + stimulus(dut)
   ▼
hasSimulator.getSimulator(testingDir)            # HasSimulator.scala:79 默认 Verilator
   → Simulator[svsim.verilator.Backend] 实例       # HasSimulator.scala:43-56
   ▼
Simulator.simulate(...)                           # Simulator.scala:150
   ├─ workspace.elaborateGeneratedModule(...)      # 第①段 细化（见下）
   └─ _simulate(...)                              # Simulator.scala:233
        ├─ workspace.generateAdditionalSources    # 生成 testbench.sv + DPI bridge + 拷 simulation-driver.cpp
        ├─ workspace.compile(backend)(...)        # 第②段 编译（见下）
        └─ simulation.runElaboratedModule{...}     # package.scala:114，进入第③段
              └─ simulation.run{ controller =>     # Simulation.scala:33 启动仿真子进程
                   new SimulatedModule(...)         # package.scala:46
                   AnySimulatedModule.withValue(module){ body(module) }  # 注入线程局部
              }
   ▼
body 执行（poke/step/expect）：
   poke(2.U) → TestableUInt.poke → simulationPort.set → Controller SetBits → 写入子进程 stdin
   step()    → TestableClock.step → simulationPort.tick → Controller Tick
   expect    → simulationPort.check → Controller GetBits → 读子进程 stdout 的 Bits 消息
```

第①段细化（`ChiselWorkspace.elaborateGeneratedModule`）内部：

- [package.scala:175-200](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L175-L200) —— `generateWorkspaceSources`：跑 `(new circt.stage.ChiselStage).execute(--target systemverilog --split-verilog, ...)`，带 `ChiselGeneratorAnnotation`、`FirtoolOption`、`TargetDirAnnotation`。这正是 [u5-l4](u5-l4-circt-firtool.md) 讲的 CIRCT 链路被复用的地方。
- [package.scala:249-285](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L249-L285) —— `getModuleInfoPorts`：递归把 `Record`/`Vec` 拍平成叶子端口，名字用下划线拼接（如 `io_in_bits_a`），按方向打 `isGettable`/`isSettable`，并丢掉零宽端口（firtool 会优化掉）。

第②段编译（`Workspace.compile`）内部：

- [Workspace.scala:385-405](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L385-L405) —— `compile`：调 `backend.generateParameters(topModuleName = "svsimTestbench", ...)`，扫描源文件，写 `sourceFiles.F` 与 Makefile。
- [Workspace.scala:533-565](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L533-L565) —— `ProcessBuilder("make", "-C", workdir, "simulation")`：真正触发 Verilator 编译；非零退出码抛带完整编译日志的异常。

#### 4.5.3 源码精读（桥接点）

`ChiselWorkspace` 隐式类如何挂到 `svsim.Workspace` 上：

- [package.scala:130-141](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L130-L141) —— `implicit class ChiselWorkspace(workspace: Workspace)` 提供 `elaborateGeneratedModule`。它调 `generateWorkspaceSources`（产出 SV）、`getModuleInfoPorts`（推断端口）、`initializeModuleInfo`（最终调 `workspace.elaborate(info)`，把端口元信息存进 svsim）。

`ChiselSimulation` 隐式类如何挂到 `svsim.Simulation` 上：

- [package.scala:113-128](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L113-L128) —— `implicit class ChiselSimulation(simulation: Simulation)` 提供 `runElaboratedModule`：内部调 `simulation.run{ controller => new SimulatedModule; AnySimulatedModule.withValue(module){ body } }`。它把 svsim 的裸 `Controller` 包成 Chisel 感知的 `SimulatedModule`，并设置线程局部上下文。

testbench/DPI 生成（连接 Chisel 端口与 C 协议的胶水）：

- [Workspace.scala:128-353](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L128-L353) —— `generateAdditionalSources`：生成 `testbench.sv`（实例化 DUT 为 `dut`，每个端口导出 `getBitsImpl_*`/`setBitsImpl_*` DPI 函数，定义 `simulation_body` 任务），生成 `c-dpi-bridge.cpp`（C 包装：先 `setScopeToTestBench` 再调 DPI），并拷贝 `simulation-driver.cpp` 资源（[Workspace.scala:351](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/svsim/src/main/scala/Workspace.scala#L351)）。

> 这就是「为什么顶层是 `svsimTestbench`」的根因：`simulation-driver.cpp` 是 `main`，它通过 DPI 调 testbench 导出的端口函数，而 testbench 内部用 `$bits`/赋值访问真正挂在其 `dut` 实例上的端口。

#### 4.5.4 代码实践

**实践目标**：把整条链路在源码里逐站标出来，画成一张图。

**操作步骤**：

1. 准备一张白纸或文本文件，从「`simulate(new MyAdder){...}`」起笔。
2. 沿 4.5.2 的流程图，在每一站标注它所在的**文件:行号**（例如「`SimulatorAPI.simulate` → `SimulatorAPI.scala:81`」「`workspace.compile` → `Workspace.scala:385`」「`make simulation` → `Workspace.scala:533`」）。
3. 在链路终点（仿真子进程）旁标注三个参与文件：`testbench.sv`、`c-dpi-bridge.cpp`、`simulation-driver.cpp`，并说明它们各自由谁生成。
4. 反向走一遍：一次 `expect(5.U)` 从用户代码到子进程 stdout 的 `Bits` 消息，经过哪几层。

**需要观察的现象**：链路穿越了两个子项目（`chisel` 与 `svsim`），且两者的衔接**全靠隐式类**（`ChiselWorkspace`、`ChiselSimulation`），没有把 svsim 的内部细节漏进 Chisel。

**预期结果**：你能脱稿复述「细化（ChiselStage→SV）→ 端口推断 → testbench/DPI 生成 → Verilator 编译（make）→ 子进程协议驱动」五步，并指出每步的源码位置。

#### 4.5.5 小练习与答案

1. **问**：`chisel3.simulator` 与 `svsim` 之间的耦合点是什么？为什么这样设计？
   **答**：耦合点是两个隐式类 `ChiselWorkspace`（给 `Workspace` 加 `elaborateGeneratedModule`）与 `ChiselSimulation`（给 `Simulation` 加 `runElaboratedModule`）。这样 `svsim` 完全不依赖 Chisel，可被任何能产出 SystemVerilog 的前端复用；Chisel 特有逻辑集中在 `chisel3.simulator`，符合 [u1-l3](u1-l3-repository-layout.md) 的代码归属原则。

2. **问**：如果 firtool 把某个端口优化掉了（变成零宽），仿真时会发生什么？
   **答**：`getModuleInfoPorts` 会在登记端口时用 `element.getWidth > 0` 过滤掉它（[package.scala:267-277](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/simulator/package.scala#L267-L277)），所以它不会出现在 testbench 的端口表里，也不会生成 DPI 函数——poke/peek 它会因找不到端口而失败。

## 5. 综合实践

**任务**：实现一个「延迟一拍的加法器」并用 ChiselSim 完整跑通，再回答三个诊断问题。这把本讲的「分层」「协议」「Backend」「PeekPoke」「链路」全部串起来。

1. **写模块**（示例代码）：

   ```scala
   class DelayedAdder extends Module {
     val io = IO(new Bundle {
       val a   = Input(UInt(8.W))
       val b   = Input(UInt(8.W))
       val sum = Output(UInt(8.W))
     })
     io.sum := RegNext(io.a + io.b)
   }
   ```

2. **写测试**（用本仓库真实的测试风格，参见 [PeekPokeAPISpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/simulator/PeekPokeAPISpec.scala)）：

   ```scala
   class DelayedAdderSpec extends AnyFunSpec with ChiselSim with Matchers {
     it("registers the sum one cycle later") {
       simulate(new DelayedAdder) { dut =>
         dut.io.a.poke(2.U); dut.io.b.poke(3.U)
         dut.clock.step()
         dut.io.sum.expect(5.U)
       }
     }
   }
   ```

3. **运行并探查工作目录**（若 Verilator 可用）：找到 `workdir-verilator/`，打开 `testbench.sv` 找到 `dut` 实例化与 `getBitsImpl_sum`/`setBitsImpl_a`；打开 `Makefile` 看 Verilator 的真实命令行。

4. **回答三个诊断问题**（写在你的笔记里）：
   - 为什么 `expect` 必须在 `step()` 之后？（提示：`RegNext`、寄存器更新时机。）
   - 一次 `poke(2.U)` 在链路上走了哪几层才变成 Verilator 仿真器里的端口值？（提示：`TestableUInt.poke` → `simulationPort.set` → `Controller SetBits` → DPI `setBitsImpl_a` → testbench 里的 `a = value`。）
   - 若把 `Settings` 改成开波形，链路上哪两处会同时变化？（提示：Verilator 的 `--trace` 与 testbench 的 `SVSIM_ENABLE_VCD_TRACING_SUPPORT` 宏，见 4.3.4。）

> 本任务可在本仓库直接做：把它放进 `src/test/scala/chiselTests/simulator/`，用 `./mill chisel[].test` 跑（具体 mill 目标名待本地验证）。若环境无 Verilator，至少完成「写代码 + 画 4.5.2 的链路图 + 回答诊断问题」。

## 6. 本讲小结

- Chisel 仿真分**两层包**：`chisel3.simulator`（Chisel 感知的高层 API）与 `svsim`（Chisel 无关、靠文本协议驱动仿真进程的底层库），二者用 `ChiselWorkspace`/`ChiselSimulation` 两个隐式类桥接。
- `trait Simulator[T <: Backend]` 把仿真编成「细化 → 编译 → 运行」三段，分别产出 `ElaboratedModule`、`Simulation`、用户结果，并用 `BackendInvocationDigest` 分别记录编译与运行耗时。
- `svsim.Simulation` 把仿真器抽象成一个**子进程**，靠 `Controller` 在 stdin/stdout 上收发 Command/Message 文本协议；返回 `Unit` 的命令可批量缓冲，返回值的命令才同步等待。
- `trait Backend` 用一个 `generateParameters` 方法收敛 Verilator 与 VCS 的差异；编译统一走 `make simulation`，默认后端是 Verilator（`HasSimulator.default`）。
- `PeekPokeAPI` 用一组 `Testable*` 隐式类，把类型安全的 `poke`/`peek`/`expect`/`step` 映射到底层 `SetBits`/`GetBits`/`Tick`，并用 `willPoke`/`willPeek` 实现「poke 后首次 peek 自动结算」的延迟求值。
- 仿真顶层是自动生成的 `svsimTestbench`（不是你的模块），DUT 作为 `dut` 实例化其中，端口经 DPI 桥（`testbench.sv` + `c-dpi-bridge.cpp` + `simulation-driver.cpp`）与 Scala 互通。

## 7. 下一步学习建议

- **回到测试体系**：本讲的 `simulate` 是 [u9-l1 单元测试体系](u9-l1-testing.md) 的运行时底座，建议接着读 `UnitTest` trait 与 `FileCheck`，看「内联测试」是如何落到这条仿真链路上的。
- **阅读 stimulus 子包**：`src/main/scala/chisel3/simulator/stimulus/` 里的 `RunUntilFinished`、`ResetProcedure`、`SimulationTestStimulus` 提供了比手写 poke 更高层的激励模式（如自动复位、跑到完成），是 `simulateTest`/cookbook 示例（见 [FSM.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/cookbook/FSM.scala)）的支撑。
- **关注 Layer 与仿真的交互**：[u8-l3 Layers 与 Probe](u8-l3-layers-and-probe.md) 讲的 Layer 在仿真时会变成文件级 include/ifdef 过滤——本讲 `Settings.verilogLayers`（`shouldIncludeFile`/`shouldIncludeDirectory`）正是这个机制在编译设置里的入口，值得回头对照。
- **动手扩展**：尝试实现一个最小的自定义 `Backend`（例如指向一个固定路径的 Verilator），体会 `generateParameters` 这一个方法就是后端的全部契约。
