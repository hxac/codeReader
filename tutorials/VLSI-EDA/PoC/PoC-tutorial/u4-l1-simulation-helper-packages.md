# 仿真辅助包：sim 命名空间

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 PoC 在 `src/sim/` 下提供了哪几个仿真辅助包，以及它们为什么按 VHDL 版本（v93 / v08）拆成两套。
- 理解「全局仿真状态」是如何用 VHDL-2008 的 **protected 类型 + shared variable** 在多个进程之间共享的，并掌握 `simInitialize` / `simFinalize` / `simAssertion` 这套入口。
- 会用 `simGenerateClock` 生成指定频率的时钟、用 `simGenerateWaveform` 与 `simGenerateWaveform_Reset` 生成复位等激励波形，并解释它们为什么能像并发语句一样「裸调用」。
- 会用 `T_RANDOM` 受保护类型以及底层的 `rand*` 过程生成均匀／正态／泊松分布的随机数。

本讲是第 4 单元（仿真、综合与目标平台）的起点，承接 u2-l1（公共包与 `common.files` 的版本条件编译）与 u2-l4（`physical` 包的 `FREQ`／`time` 物理类型）。下一讲 u4-l2 会用本讲介绍的入口去搭建真实核（如 `fifo_cc_got`）的测试台。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**测试台为什么需要「辅助包」。** 一个 VHDL 测试台（testbench）通常要做三件事：产生时钟与复位、施加激励、检查结果并报告通过／失败。这些代码在每个测试台里高度重复。PoC 把它们抽成一组可复用过程，让你在架构体里写两三行就能起一个完整的仿真环境。这就是 `src/sim/` 存在的意义。

**什么是「全局仿真状态」。** 测试台里有多个并发进程（时钟进程、写进程、检查进程……），它们需要共享一些信息：当前注册了几个测试、几个进程还活着、断言失败了多少次、是否该让时钟停下来。VHDL 普通变量是进程局部的，跨进程共享可变状态要用 **shared variable（共享变量）**，而要让共享变量在并发访问下不出错，它的类型必须是 **protected（受保护）类型**——可以类比成「带方法、且方法调用是线程安全」的对象。这套机制是 VHDL-2008 才标准化的，这正是 sim 包要按版本拆两套的根本原因。

**并发过程调用是怎么回事。** 当一个过程（procedure）体内含有 `wait` 语句，并且被直接写在架构体的并发区（而不是写在某个 `process` 里），VHDL 会把它等价展开成一个进程。所以 `simGenerateClock(clk, 100 MHz);` 写在 `begin` 后面，就等于一个不停翻转 `clk` 的隐式进程；`simInitialize;` 写在那儿，就等于一个初始化完便永久挂起的隐式进程。记住这一点，后面看到那些「裸调用」就不会疑惑。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/sim/` 下。它们的依赖与职责如下图（下方依赖上方）：

```
                 sim_types.vhdl            ← 版本无关：类型 + 纯 rand* 过程
                 /            \
   sim_protected.v08.vhdl   sim_waveform.vhdl   ← protected 状态机 / 时钟与波形
                 |
        sim_global.v08.vhdl                     ← 声明全局 shared variable
                 |
     sim_simulation.v08.vhdl                    ← 薄包装：sim* 自由过程
                 |
   sim_random.v08.vhdl                          ← T_RANDOM protected 类型（封装 rand*）
```

| 文件 | 作用 |
|------|------|
| `src/sim/sim_types.vhdl` | 版本无关的公共类型：测试／进程记录、`T_SIM_RAND_SEED` 种子、`T_PERCENT`/`T_DUTYCYCLE`/`T_PHASE` 等物理单位，以及纯过程版随机数 `randUniform/Normal/PoissonDistributedValue`。 |
| `src/sim/sim_protected.v08.vhdl` | 定义受保护类型 `T_SIM_STATUS`：内部维护测试表、进程表、断言计数器、时钟使能标志，是「全局仿真状态」的真正实现。 |
| `src/sim/sim_global.v08.vhdl` | 仅声明三个全局 `shared variable`：`globalSimulationStatus`、`globalLogFile`、`globalStdOut`。 |
| `src/sim/sim_simulation.v08.vhdl` | 薄包装包 `simulation`：把 `simInitialize`、`simFinalize`、`simCreateTest`、`simAssertion` 等做成自由过程，内部委托给 `globalSimulationStatus`。 |
| `src/sim/sim_random.v08.vhdl` | 受保护类型 `T_RANDOM`：把种子包成内部状态，对外暴露 `GetUniformDistributedValue` 等方法。 |
| `src/sim/sim_waveform.vhdl` | 版本无关：`simGenerateClock`（按 `FREQ` 或周期生成时钟）、`simGenerateWaveform`（按延时数组生成波形）、`simGenerateWaveform_Reset`（复位波形快捷构造）。 |

**版本拆分一览。** `src/sim/sim.files` 用 `VHDLVersion` 条件选两套实现（与 u2-l1 讲过的 `common.files` 同一套机制）：

- [src/sim/sim.files:9-23](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim.files#L9-L23) 中，`sim_types.vhdl` 与 `sim_waveform.vhdl` 两端恒定编译（版本无关）；中间一组在 `VHDLVersion < 2002` 时编译 `*_v93` 三件套（`sim_random.v93` / `sim_global.v93` / `sim_unprotected.v93` / `sim_simulation.v93`），在 `VHDLVersion <= 2008` 时编译 `*_v08` 三件套（`sim_random.v08` / `sim_protected.v08` / `sim_global.v08` / `sim_simulation.v08`）。
- v93 没有 protected 类型，于是用 `sim_unprotected.v93`（普通共享变量）替代 `sim_protected.v08`；接口（`sim_simulation`）对使用者保持一致。这正是把「状态机实现」与「对外接口」分层带来的好处。

> 提醒：pyIPCMI 默认按 VHDL-2008 编译，所以本讲后续精读默认走 v08 那一组。这套 `.files` 选择逻辑会在 u4-l3 详细展开。

## 4. 核心概念与源码讲解

### 4.1 全局仿真状态：protected 类型与共享变量

#### 4.1.1 概念说明

一个测试台需要一张「全局账本」：

- 注册了哪些测试（`test`）、每个测试下注册了哪些进程（`process`）。
- 跑了多少条断言（`AssertCount`）、其中失败多少条（`FailedAssertCount`），据此判定最终是 PASSED 还是 FAILED。
- 一组「时钟使能」标志，用来让时钟进程在仿真结束时优雅停止。

由于这些状态要被多个并发进程读写，PoC 把它们封装成一个 **protected 类型 `T_SIM_STATUS`**，再以 `shared variable` 形式全局唯一实例化。protected 类型的方法调用是互斥的，因此即便多个进程同时调用 `assertion`，计数器也不会出错——这正是「线程安全」的保证，也是它必须用 VHDL-2008 的原因（v93 退化为非保护的 `sim_unprotected`）。

#### 4.1.2 核心流程

测试台的典型生命周期：

```
simInitialize                → globalSimulationStatus.initialize
                               ├─ init(): 置 IsInitialized，createDefaultTest
                               └─ 记录 MaxAssertFailures / MaxSimulationRuntime
simGenerateClock(...)        → 自注册一个进程，循环翻转时钟，直到 isStopped
simRegisterProcess(...)      → 往进程表追加一项，返回 ProcID
simAssertion(cond, msg)      → AssertCount++; 若 false → fail() + FailedAssertCount++
simDeactivateProcess(ProcID) → 标记某进程结束
simFinalize                  → globalSimulationStatus.finalize
                               ├─ 逐个 finalizeTest
                               └─ writeReport → 打印 "SIMULATION RESULT = PASSED/FAILED/NO ASSERTS"
```

最终判定逻辑（[src/sim/sim_protected.v08.vhdl:203-213](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L203-L213)）：只要有任何一次 `fail`，整体就是 `FAILED`；若一条断言都没跑，是 `NO ASSERTS`；否则 `PASSED`。当失败次数达到 `MaxAssertFailures` 时会触发 `stopAllProcesses` 提前终止（[src/sim/sim_protected.v08.vhdl:228-238](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L228-L238)）。

#### 4.1.3 源码精读

**第 1 层：全局共享变量的声明。** `sim_global` 包内容极短，只声明三个全局对象——状态机、日志文件、标准输出：

[src/sim/sim_global.v08.vhdl:36-42](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_global.v08.vhdl#L36-L42) — 声明 `globalSimulationStatus : T_SIM_STATUS` 等。这是「全局仿真状态」真正落地的地方：整张账本就一个实例，所有进程都引用它。

**第 2 层：状态机的内部字段。** `T_SIM_STATUS` 的 protected body 里用一组普通变量记账：

[src/sim/sim_protected.v08.vhdl:93-118](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L93-L118) — `Passed`（一开始为 `TRUE`，一旦失败永不回真）、`AssertCount`／`FailedAssertCount`、`MainClockEnables`／`MainProcessEnables`（按 TestID 索引的布尔数组）、`Processes`／`Tests` 两张表。

注意 [src/sim/sim_protected.v08.vhdl:107-108](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L107-L108)：`MainClockEnables` 初值全 `TRUE`，这正是「时钟使能」开关——`isStopped` 就是在读它。

**第 3 层：初始化与终结。**

[src/sim/sim_protected.v08.vhdl:121-136](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L121-L136) — `init` 保证只初始化一次并创建默认测试；`initialize` 在此之上记录两个上限参数。

[src/sim/sim_protected.v08.vhdl:138-148](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L138-L148) — `finalize` 同样幂等（靠 `IsFinalized` 守卫），逐个终结测试后调用 `writeReport` 输出报告。

**第 4 层：对外自由过程只是薄包装。** `simulation` 包把 protected 方法包装成不带对象前缀的自由过程，使测试台代码更简洁：

[src/sim/sim_simulation.v08.vhdl:98-107](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl#L98-L107) — `simInitialize` 直接转发 `globalSimulationStatus.initialize(...)`；若给了 `MaxSimulationRuntime`，则用 `wait for` 兜底超时。

[src/sim/sim_simulation.v08.vhdl:109-112](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl#L109-L112) — `simFinalize` 一行转发。其余 `simCreateTest`、`simRegisterProcess`、`simAssertion`、`simFail` 都是同样的转发风格（见 [src/sim/sim_simulation.v08.vhdl:114-168](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl#L114-L168)）。

> 这种「protected 实现 + shared variable + 薄包装」的三层结构是 PoC 仿真基础设施的骨架，理解了它，后面两个模块（时钟、随机数）的 `simRegisterProcess`／`simIsStopped` 调用就顺理成章。

#### 4.1.4 代码实践

**目标：** 追踪 `simInitialize` 到 protected 方法的完整调用链，验证「自由过程只是转发」。

**步骤：**

1. 打开 [src/sim/sim_simulation.v08.vhdl:98](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl#L98)，看到 `simInitialize` 调 `globalSimulationStatus.initialize`。
2. 跳到 [src/sim/sim_protected.v08.vhdl:130](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L130)，`initialize` 再调 `init`（[121 行](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L121)），后者把 `State.IsInitialized` 置真并 `createDefaultTest`。
3. 回到 [src/sim/sim_global.v08.vhdl:39](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_global.v08.vhdl#L39)，确认 `globalSimulationStatus` 是唯一的 `shared variable` 实例。

**需要观察的现象：** 整条链上没有任何「业务逻辑」发生在自由过程里；状态改变全部发生在 protected body 内。

**预期结果：** 你能用自己的话回答：「为什么测试台里写 `simInitialize;` 就能让全局状态就绪？」——因为它转发到了全局唯一的 `globalSimulationStatus.initialize`。

#### 4.1.5 小练习与答案

**练习 1：** `simAssertion(false, "boom")` 之后，最终报告会显示什么结果？为什么？

**答案：** 会显示 `SIMULATION RESULT = FAILED`。因为 `assertion` 在条件为假时调 `fail`，把 protected 内部的 `Passed` 置为 `FALSE`（[src/sim/sim_protected.v08.vhdl:240-246](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L240-L246)），而 `Passed` 一旦为假永不回真，`writeReport_SimulationResult` 据此输出 FAILED。

**练习 2：** 如果不调用 `simFinalize`，报告还会打印吗？

**答案：** 不会。报告是 `finalize` → `writeReport` 打印的（[src/sim/sim_protected.v08.vhdl:138-148](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L138-L148)）。这就是 PoC 测试台都在检查进程末尾调一次 `simFinalize` 的原因（见 u4-l2）。

---

### 4.2 时钟与波形生成

#### 4.2.1 概念说明

仿真要驱动被测核，最基础的激励就是时钟和复位。PoC 在 `sim_waveform.vhdl` 里提供两类生成器：

- **`simGenerateClock`**：按频率（`FREQ`）或周期（`time`）生成方波时钟，可配相位（`T_PHASE`）、占空比（`T_DUTYCYCLE`）和抖动（`T_WANDER`）。
- **`simGenerateWaveform`**：按一段「延时数组」逐段驱动信号，每段延时后翻转（对单比特）或赋新值（对总线）。`simGenerateWaveform_Reset` 是构造复位波形的快捷函数。

它们都是**含 `wait` 的过程**，因此在并发区「裸调用」即等价为进程。每个生成器还会通过 `simRegisterProcess` 把自己登记到全局状态，并在循环条件里查 `simIsStopped`，从而能在 `simFinalize` 时被优雅停止（见 [src/sim/sim_protected.v08.vhdl:447-468](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_protected.v08.vhdl#L447-L468) 的 `stopAllClocks`/`isStopped`）。

频率与周期的换算由 `physical` 包的 `to_time(FREQ)` 完成（u2-l4 已讲）：

\[
\text{Period} = \frac{1}{f}
\]

对 \( f = 100\,\text{MHz} \)，得 \(\text{Period} = 10\,\text{ns}\)。

#### 4.2.2 核心流程

`simGenerateClock`（周期版，主体在 [src/sim/sim_waveform.vhdl:324-382](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L324-L382)）的逻辑：

```
1. 把 Phase / DutyCycle / Wander 三个物理量换算成 [0,1] 内的实数因子
2. 算出 Delay（相位偏移）、TimeHigh、TimeLow
3. simRegisterProcess(TestID, "...", IsLowPriority => TRUE)  ← 自登记
4. 处理首拍的相位偏移（Delay）
5. while not simIsStopped(TestID) loop        ← 受全局状态控制
       wait for TimeHigh; Clock <= '0'
       wait for TimeLow;  Clock <= '1'
   end loop
6. simDeactivateProcess + 再多跑 ClockAfterRun_cy(=5) 拍，让别人看到停止条件
```

`simGenerateWaveform`（单比特版，[src/sim/sim_waveform.vhdl:519-537](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L519-L537)）更简单：从 `InitialValue` 出发，逐段 `wait for Waveform(i)` 后把信号取反，因此一段 `T_SIM_WAVEFORM`（延时数组）天然描述一个「翻转序列」。

#### 4.2.3 源码精读

**时钟的四个重载。** 对外有两套参数：用 `FREQ` 或用 `time` 周期；带或不带 `TestID`：

[src/sim/sim_waveform.vhdl:51-80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L51-L80) — 四个声明；其中 `FREQ` 版先用 `to_time(Frequency)` 转成周期再委托给 `time` 版（[src/sim/sim_waveform.vhdl:295-298](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L295-L298)）。默认 `Phase := 0 deg`、`DutyCycle := 50 percent`、`Wander := 0 permil`，所以最简调用 `simGenerateClock(clk, 100 MHz)` 就是标准 50% 占空比方波。

**主循环的优雅停止。**

[src/sim/sim_waveform.vhdl:366-381](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L366-L381) — `while not simIsStopped(TestID)` 决定了时钟何时停；退出后再补 5 拍（`ClockAfterRun_cy`），好让其它进程来得及观测到停止条件，最后落 `'0'`。这是「时钟进程」与「全局状态机」协作的关键点。

**波形类型与构造。**

[src/sim/sim_waveform.vhdl:126-133](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L126-L133) — `T_SIM_WAVEFORM` 其实是 `TIME_VECTOR` 的子类型（一组延时）；`T_SIM_WAVEFORM_SL` 等则是 `(Delay, Value)` 元组数组，用于总线波形。

[src/sim/sim_waveform.vhdl:965-980](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L965-L980) — `simGenerateWaveform_Reset(Pause, ResetPulse)` 返回 `(0 => Pause, 1 => ResetPulse)`。注意函数体里先把参数赋给局部变量 `p`/`rp` 再返回——这是注释里写明的 **Mentor QuestaSim/ModelSim 10.4c 的 workaround**：直接聚合 `(0 => Pause, 1 => ResetPulse)` 在旧版 ModelSim 里会被错误地常数折叠成 `(0 ns, 10 ns)`。这种「为某家工具打补丁」的小细节在 PoC 里很常见。

#### 4.2.4 代码实践

**目标：** 看懂 `fifo_cc_got_tb` 头三行激励，并据此写一个最小可编译的时钟 + 复位发生器。

**步骤：**

1. 阅读 [tb/fifo/fifo_cc_got_tb.vhdl:65-68](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L65-L68)（真实测试台）：
   ```vhdl
   simInitialize;
   simGenerateClock(clk,        CLOCK_FREQ);                                       -- CLOCK_FREQ = 100 MHz
   simGenerateWaveform(rst,     simGenerateWaveform_Reset(Pause => 10 ns, ResetPulse => 10 ns));
   ```
2. 手算：`simGenerateWaveform_Reset(10 ns, 10 ns)` 返回 `(10 ns, 10 ns)`；单比特 `simGenerateWaveform` 从 `'0'` 出发，`wait 10 ns`→`'1'`，`wait 10 ns`→`'0'`。所以 `rst` 在 10 ns 处拉高、20 ns 处落低（高有效复位脉冲）。

**需要观察的现象（待本地验证）：** 在波形窗口里 `clk` 应是 10 ns 周期方波；`rst` 应在 10–20 ns 区间为 `'1'`，其余时间为 `'0'`。

**预期结果：** 你能说清「`Pause` 是复位拉高前的等待、`ResetPulse` 是复位高电平持续时长」。

#### 4.2.5 小练习与答案

**练习 1：** 想生成一个 25% 占空比的时钟，怎么调？

**答案：** 调第三参数 `DutyCycle`：`simGenerateClock(clk, 100 MHz, DutyCycle => 25 percent)`（`T_DUTYCYCLE` 用 `percent` 单位，见 [src/sim/sim_types.vhdl:134-142](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L134-L142)）。

**练习 2：** 为什么时钟过程退出主循环后还要多跑 5 拍？

**答案：** 让其它依赖该时钟的进程能再采样到几个边沿、从而发现 `simIsStopped` 已为真并自行退出，避免「时钟先停、别人卡死」的死锁（见 [src/sim/sim_waveform.vhdl:374-380](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L374-L380) 注释 "clock after run"）。

---

### 4.3 随机数：从纯过程到 protected 类型

#### 4.3.1 概念说明

PoC 的随机数分两层：

- **底层纯过程（`sim_types.vhdl`）：** 围绕一个种子记录 `T_SIM_RAND_SEED(Seed1, Seed2 : integer)`，提供 `randUniform/Normal/PoissonDistributedValue`。它们直接包装 `IEEE.math_real.Uniform`，本身无状态——状态在你的 `inout Seed` 变量里。这些过程版本无关，v93/v08 共用同一份源码。
- **高层 protected 类型（`sim_random.v08.vhdl`）：** `T_RANDOM` 把种子藏成内部状态，对外暴露 `GetUniformDistributedValue` 等方法，调用方不用自己维护种子。仅在 VHDL-2008 可用。

为什么要分两层？纯过程可移植（v93 也能用），但每次调用都要传种子；protected 类型用起来像「随机数发生器对象」，更顺手但依赖 2008。两种风格 PoC 都保留。

三个分布的用途：**Uniform（均匀）**用于随机激励取值；**Normal（正态／高斯）**用于模拟带抖动的时序（`simGenerateClock2` 用它叠抖动，见 [src/sim/sim_waveform.vhdl:431](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_waveform.vhdl#L431)）；**Poisson（泊松）**用于模拟稀有事件计数。

#### 4.3.2 核心流程

**均匀分布。** 直接转调 `IEEE.math_real.Uniform(Seed1, Seed2, Value)`，产生 \([0,1)\) 区间均匀实数；带 `Minimum/Maximum` 的重载再用 `scale` 缩放到目标区间（[src/sim/sim_types.vhdl:271-285](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L271-L285)）。

**正态分布（Box–Muller 变换）。** 取两个独立均匀数 \(u_1, u_2\)，构造标准正态样本再缩放：

\[
x = \sigma \cdot \sqrt{-2\ln u_1}\cdot \cos(2\pi u_2) + \mu
\]

对应代码 [src/sim/sim_types.vhdl:290-300](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L290-L300)。带上下界的整数版用「拒绝采样」：不断采样直到落入 `[Minimum, Maximum]`（[src/sim/sim_types.vhdl:302-314](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L302-L314)）。

**泊松分布。** 经典乘积法（[src/sim/sim_types.vhdl:331-351](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L331-L351)）：连续乘均匀数直到乘积跌破 \(e^{-\mu}\)，统计乘的次数。

**种子初始化要点。** `randGenerateInitialSeed` 返回**固定值** `(Seed1 => 5, Seed2 => 3423)`（[src/sim/sim_types.vhdl:171-177](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L171-L177)）；`randBoundSeed` 再用 `MAX_SEED1_VALUE = 2147483562`、`MAX_SEED2_VALUE = 2147483398`（`IEEE.math_real.Uniform` 要求的范围，[src/sim/sim_types.vhdl:168-185](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L168-L185)）把种子夹进合法区间。**默认种子是确定的，所以仿真默认可复现**——要换序列就显式传种子。

#### 4.3.3 源码精读

**种子类型与过程声明。**

[src/sim/sim_types.vhdl:97-100](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L97-L100) — `T_SIM_RAND_SEED` 就两个整数。[src/sim/sim_types.vhdl:114-128](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L114-L128) — 三种分布的声明，都把 `Seed : inout` 放第一个参数。

**Uniform 实现。**

[src/sim/sim_types.vhdl:266-269](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_types.vhdl#L266-L269) — 一行 `ieee.math_real.Uniform(Seed.Seed1, Seed.Seed2, Value)`，因为 `Seed` 是 `inout`，调用后种子自动前进一步。

**protected 封装。**

[src/sim/sim_random.v08.vhdl:61-89](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_random.v08.vhdl#L61-L89) — `T_RANDOM` 的方法接口：`SetSeed` 一组、`GetUniform/Normal/PoissonDistributedValue` 各一组（同时有 `procedure` 和 `impure function` 两种风格）。

[src/sim/sim_random.v08.vhdl:94-95](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_random.v08.vhdl#L94-L95) — protected body 内部用一个 `Local_Seed` 变量持有状态，初始化为 `randInitializeSeed`。这正是「把种子藏起来」的地方。

[src/sim/sim_random.v08.vhdl:129-134](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_random.v08.vhdl#L129-L134) — `GetUniformDistributedValue` 直接转调 `randUniformDistributedValue(Local_Seed, Result)`，与纯过程版完全一致，只是不用调用方管种子。

**真实用法。** [tb/arith/arith_div_tb.vhdl:174-192](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_div_tb.vhdl#L174-L192) 在进程里声明 `variable random : T_RANDOM;`，然后 `random.getUniformDistributedValue(0, 2**A_BITS-1)` 取区间随机整数，跑 1024 轮随机测试。这就是 `T_RANDOM` 的典型用法。

#### 4.3.4 代码实践

**目标：** 用 `T_RANDOM` 在一个进程里生成 10 个 `[0, 255]` 的随机字节，体会「对象化」用法。

**步骤：**

1. 在使用前 `use PoC.sim_random.all;`。
2. 在某 `process` 中写（示例代码，非项目原有）：

```vhdl
-- 示例代码
variable random : T_RANDOM;
variable b      : integer;
begin
  random.SetSeed(42, 7);                       -- 可选：固定种子保证可复现
  for i in 0 to 9 loop
    b := random.getUniformDistributedValue(0, 255);
    report "byte " & integer'image(i) & " = " & integer'image(b) severity NOTE;
  end loop;
  wait;
end process;
```

**需要观察的现象（待本地验证）：** 仿真控制台打印 10 行字节值。注释掉 `SetSeed` 后多次仿真，结果**不变**（因为默认种子固定）；改 `SetSeed` 的参数后序列才变。

**预期结果：** 理解「默认可复现、显式 `SetSeed` 才换序列」这一设计取舍。

#### 4.3.5 小练习与答案

**练习 1：** 同一个 `T_RANDOM` 实例被两个进程同时调用会怎样？

**答案：** protected 类型的方法调用是互斥的，所以不会撕裂状态；但两次调用的**先后顺序在仿真器里不确定**，因此跨进程共用一个实例会让序列不可复现。需要独立序列时，每个进程各声明一个 `T_RANDOM` 并各自 `SetSeed`。

**练习 2：** 想要一个均值为 50 的整数泊松流（模拟偶发事件），怎么写？

**答案：** `v := random.getPoissonDistributedValue(Mean => 50.0, Minimum => 0, Maximum => 1000);`（接口见 [src/sim/sim_random.v08.vhdl:83-85](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_random.v08.vhdl#L83-L85)）。

---

## 5. 综合实践

把本讲三个模块串起来，写一个**最小可编译的测试台骨架**：用 `simInitialize` 起全局状态、用 `simGenerateClock` 产生 100 MHz 时钟、用 `simGenerateWaveform` 产生复位脉冲，再用 `T_RANDOM` 生成若干随机激励（本任务直接对应本讲规格要求的实践任务）。

**目标：** 产出一个结构正确、可在 GHDL/ModelSim 下 elaboration 的 TB 顶层，作为后续 u4-l2 套真实 DUT 的模板。

**操作步骤：**

1. 新建文件 `tb/sim/sim_helper_demo_tb.vhdl`（示例代码，非项目原有），内容如下：

```vhdl
-- 示例代码
library IEEE;
use     IEEE.std_logic_1164.all;
use     IEEE.numeric_std.all;

library PoC;
use     PoC.physical.all;     -- FREQ
use     PoC.sim_types.all;
use     PoC.simulation.all;   -- simInitialize / simGenerateWaveform 等
use     PoC.waveform.all;     -- simGenerateClock
use     PoC.sim_random.all;   -- T_RANDOM

entity sim_helper_demo_tb is
end entity;

architecture tb of sim_helper_demo_tb is
  constant CLOCK_FREQ : FREQ := 100 MHz;
  signal clk : std_logic;
  signal rst : std_logic;
begin
  -- 4.1 全局仿真状态初始化
  simInitialize;

  -- 4.2 时钟（100 MHz → 10 ns 周期）与复位脉冲（10 ns 后拉高 10 ns）
  simGenerateClock(clk, CLOCK_FREQ);
  simGenerateWaveform(rst, simGenerateWaveform_Reset(Pause => 10 ns, ResetPulse => 10 ns));

  -- 4.3 用 T_RANDOM 生成随机激励并登记进程
  stim : process
    constant simProcessID : T_SIM_PROCESS_ID := simRegisterProcess("stim");
    variable random : T_RANDOM;
    variable b      : integer;
  begin
    wait until rising_edge(clk) and rst = '0';
    for i in 0 to 15 loop
      b := random.getUniformDistributedValue(0, 255);   -- 随机字节
      wait until rising_edge(clk);
    end loop;
    simDeactivateProcess(simProcessID);
    simAssertion(true, "demo finished");   -- 跑一条断言，让报告显示 PASSED
    simFinalize;                           -- 打印 TESTBENCH REPORT
    wait;
  end process;
end architecture;
```

2. 按 `sim.files`（[src/sim/sim.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim.files)）先把 `sim_types`→`sim_protected.v08`→`sim_global.v08`→`sim_simulation.v08`→`sim_random.v08`→`sim_waveform` 编进 `PoC` 库，再编译本 TB。

**需要观察的现象（待本地验证）：**

- 波形里 `clk` 周期 10 ns；`rst` 在 10–20 ns 为 `'1'`，之后为 `'0'`。
- 仿真结束后控制台打印 `POC TESTBENCH REPORT`，且 `SIMULATION RESULT = PASSED`。

**预期结果：** 你得到一个可复用的 TB 骨架，并验证了「全局状态 + 时钟 + 波形 + 随机数」四个入口能协同工作。若仿真器报错找不到 `waveform` 包，记得 `use PoC.waveform.all;`（`simGenerateClock` 在 `waveform` 包里，不在 `simulation` 包里——这是常见踩坑点）。

## 6. 本讲小结

- `src/sim/` 用 6 个包把测试台通用逻辑分层：`sim_types`（版本无关类型与纯 `rand*`）→ `sim_protected/sim_unprotected`（状态机，按 VHDL 版本二选一）→ `sim_global`（全局 `shared variable`）→ `sim_simulation`（`sim*` 自由过程薄包装）→ `sim_random`（`T_RANDOM` protected）→ `sim_waveform`（时钟与波形）。
- **全局仿真状态**靠 VHDL-2008 的 `T_SIM_STATUS` protected 类型 + 唯一 `shared variable` `globalSimulationStatus` 实现；`simInitialize`/`simFinalize`/`simAssertion` 只是转发到它的方法，最终由 `writeReport` 输出 PASSED/FAILED/NO ASSERTS。
- **时钟与波形**生成器是含 `wait` 的过程，并发区裸调用即等价进程；它们自登记到全局状态，并在 `while not simIsStopped` 循环里实现优雅停止。`simGenerateWaveform_Reset` 内含针对旧版 ModelSim 的 workaround。
- **随机数**分两层：`sim_types` 里围绕 `T_SIM_RAND_SEED` 的纯过程（Uniform/Normal/泊松），与 `sim_random` 里把种子封装成内部状态的 `T_RANDOM` protected 类型；默认种子固定，仿真默认可复现。
- 跨包引用要点：`simGenerateClock` 在 `PoC.waveform` 包、`simInitialize` 在 `PoC.simulation` 包、`T_RANDOM` 在 `PoC.sim_random` 包——使用时三句 `use` 缺一不可。
- 版本拆分（v93/v08）由 `sim.files` 用 `VHDLVersion` 条件控制，与 u2-l1 的 `common.files` 同机制；详细留在 u4-l3。

## 7. 下一步学习建议

- **u4-l2（测试台结构与编写）** 会用本讲的入口去搭 `fifo_cc_got_tb` 这类真实核测试台，包括 `for generate` 批量验证与 `tb/common` 下的板级 `my_config` 变体——那是本讲骨架的自然延伸。
- **u4-l3（VHDL 版本处理）** 会深入解释 `.v93.vhdl`/`.v08.vhdl` 后缀约定与 protected 类型在两版里的差异实现，把本讲提到的 `sim_protected` vs `sim_unprotected` 拆分讲透。
- 想直接看更多真实用例：`tb/misc/stat/stat_Histogram_tb.vhdl`（同时用了时钟、波形、`T_RANDOM`）和 `tb/arith/arith_div_tb.vhdl`（`T_RANDOM` 拒绝采样式随机测试）都是很好的对照阅读材料。
