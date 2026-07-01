# RISC-V Debug 模块

## 1. 本讲目标

CoralNPU 是一个跑在芯片里的协处理器：外部主机用 AXI 把程序灌进去、启动它、等它跑完再读结果（参见 [u3-l5](u3-l5-csr-boot-control.md)）。但如果程序跑飞了、需要下断点、想单步执行、或者想在不打断程序的情况下偷看某个寄存器的值，光靠「启动—轮询—读结果」就不够了——我们需要一种**在内核运行时从外部观察和控制它内部状态**的能力，这就是**调试（Debug）**。

本讲讲解 CoralNPU 如何实现 [RISC-V External Debug Specification](https://riscv.org//technical/) 的一个子集。读完本讲，你应该能够：

1. 说清 Debug 模块（`DebugModule`）在 CoralNPU 系统中的**位置**：它夹在「外部 AXI 主机」和「标量核内部」之间，经一层 AXI CSR 桥接暴露给调试器。
2. 掌握 **halt / resume** 握手：调试器如何让核停下来、如何让它继续跑。
3. 掌握**抽象命令（Abstract Command）**——尤其是 Access Register 命令——如何读写标量通用寄存器（GPR）、浮点寄存器（FPR）和 CSR。
4. 理解外部调试器经 **`req_addr` / `req_op` / `status`** 这组 AXI CSR 与 Debug 模块通信的**轮询协议**。

本讲依赖 [u5-l3（寄存器堆与 CSR）](u5-l3-regfile-csr.md) 和 [u3-l5（CSR 接口与启动控制）](u3-l5-csr-boot-control.md)。

---

## 2. 前置知识

如果你用过 GDB 调试 C 程序，本讲的很多概念会很亲切。我们用通俗语言把几个关键术语过一遍。

- **halt / resume（停机 / 恢复）**：调试器让 CPU 在某条指令边界停下来（halt），此时 CPU 进入一种特殊的「调试模式」，不再执行普通指令；调试器观察完后让它从原地继续跑（resume）。注意它与「复位」不同——halt 不破坏寄存器和内存，只是把 PC 冻住。
- **single-step（单步）**：resume 之后只执行一条指令就再次 halt。它是「设断点」之外最常用的逐条观察手段。
- **GPR / FPR / CSR**：GPR 是 32 个整数通用寄存器（`x0`–`x31`，ABI 名 `a0`、`sp` 等）；FPR 是浮点寄存器；CSR 是 RISC-V 的控制状态寄存器（如 `mstatus`、`mepc`）。调试器经常需要在不改写程序的前提下读写它们。
- **抽象命令（Abstract Command）**：RISC-V 调试规范定义的一种「让 Debug 模块替调试器干一件复杂活」的机制。调试器只需往 `command` 寄存器写一个编码，Debug 模块就自动完成「读一个 GPR」「写一个 CSR」「搬一段内存」并把结果放进 `data0`。它把「怎么去读 GPR」这件硬件细节藏在模块内部，对调试器暴露统一接口。
- **DMI / 内存映射调试端口**：在真实芯片里，调试器通过 JTAG 等慢速物理口访问 Debug 模块的「内部寄存器」。CoralNPU 没有专用调试口，而是把 Debug 模块的内部寄存器**映射到一段 AXI 地址空间**——外部主机像读写普通外设一样去驱动调试器。这就是本讲的 `req_addr` / `req_op` 协议。
- **pyOCD / GDB Server**：`pyOCD` 是一个开源的调试服务器，它把「GDB 协议」翻译成「对 Debug 模块的底层读写」。CoralNPU 提供了一个基于 pyOCD 的 GDB server（`coralnpu_test_utils/core_mini_axi_pyocd_gdbserver.py`），让你能用真实的 `riscv32-unknown-elf-gdb` 连上仿真器调试。这是本讲综合实践的运行载体。

> 阅读提示：RISC-V 调试规范里有一个核心概念叫 **Debug Module（DM）**，它独立于 CPU 流水线，通过一组握手信号（`haltreq`/`resumereq`/`halted`…）控制 CPU。CoralNPU 的 `DebugModule` 类就是它的硬件实现，对应文件 `Debug.scala`。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [doc/microarch/debug.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md) | Debug 模块的接口表、AXI CSR 协议、内部寄存器位域、抽象命令示例。本讲的「规格说明书」。 |
| [hdl/chisel/src/coralnpu/scalar/Debug.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala) | `DebugModule` 的 Chisel 实现：内部寄存器、halt/resume 握手、抽象命令执行、对 CSR/寄存器堆/TCM 的数据搬移。**本讲的主角。** |
| [hdl/chisel/src/coralnpu/CoreAxiCSR.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala) | `CoreCSR`：把 AXI 写事务翻译成 Debug 模块的 `req`/`rsp` 握手，即「AXI CSR 桥」。定义 `req_addr`/`req_op`/`status` 等地址。 |
| [hdl/chisel/src/coralnpu/CoreAxi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala) | 实例化 `DebugModule`，仲裁两路请求源，并把 `haltreq`/`resumereq` 接到标量核、把 `halted`/`running` 回采给模块。 |
| [coralnpu_test_utils/core_mini_axi_pyocd_gdbserver.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_pyocd_gdbserver.py) | pyOCD GDB server：用 Python 把 GDB 命令翻译成对 AXI CSR 调试端口的读写。综合实践的运行入口。 |
| [tests/cocotb/core_mini_axi_debug.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/core_mini_axi_debug.py) | 端到端 cocotb 测试：用 GDB 命令做 halt/break/单步/读寄存器，验证 Debug 模块行为。 |

---

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，按「自外向内」的顺序：先看 Debug 模块在系统里处在什么位置（4.1），再看它内部那组控制/状态寄存器（4.2），然后是 halt/resume 控制流（4.3）、抽象命令读写寄存器（4.4），最后把外部 AXI 协议串起来（4.5）。

### 4.1 Debug 模块在系统中的位置

#### 4.1.1 概念说明

RISC-V 调试规范里，Debug Module（DM）是一个**独立于 CPU 流水线**的旁路模块。它对**外**给调试器提供一个简单的「读/写内部寄存器」接口；对**内**通过几根控制线（`haltreq`、`resumereq`）命令 CPU 停下或继续，并通过几根状态线（`halted`、`running`）回看 CPU 当前在不在调试模式。CPU 内部的寄存器堆、CSR、内存都不直接暴露给调试器——调试器要读写它们，必须请 DM 代劳（这就是「抽象命令」）。

CoralNPU 没有专用 JTAG 调试口，而是把 DM 挂在 AXI 上，于是数据通路变成两层：

```
外部 GDB/调试器
     │  AXI 读写（地址落在 0x30800 起）
     ▼
CoreAxiCSR ── CoreCSR（AXI CSR 桥）          ← 把 AXI 写翻译成 req 脉冲
     │  Decoupled(req/rsp)
     ▼
DebugModule（Debug.scala）                     ← 本讲主角
     │  haltreq / resumereq / abstract cmd
     ▼
标量核（SCore：寄存器堆 / CSR / 取指-派发）
```

#### 4.1.2 核心流程

1. 调试器把一笔 AXI 写事务发到 CSR 区（基址 `0x30000`，见 [u3-l5](u3-l5-csr-boot-control.md)），地址落在 `0x30800` 一带的「调试端口」寄存器上。
2. `CoreCSR` 把这次写翻译成对 `DebugModule` 的一笔 `req`（带 `address`/`data`/`op`），并把它排队送进去。
3. `DebugModule` 解释这笔 `req`：要么直接读写自己的内部寄存器（如 `dmcontrol`），要么触发一条抽象命令去碰核内资源。
4. `DebugModule` 回一个 `rsp`（带 `data`/`op=SUCCESS/FAILED`），`CoreCSR` 把它放进响应队列。
5. 调试器轮询 `status` 寄存器，看到「有响应」就读 `rsp_data`，最后再写一次 `status` 把响应消费掉。

#### 4.1.3 源码精读

`DebugModule` 对外的「调试器侧」接口是一个 `Decoupled` 的请求/响应对，外加一组连到核的控制/状态线。接口在 [Debug.scala:94-119](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L94-L119) 声明，其中：

- `ext.req` / `ext.rsp`：与调试器之间的请求/响应握手（请求带 `address`/`data`/`op`，响应带 `data`/`op`）。
- `haltreq` / `resumereq`（输出）与 `halted` / `running` / `resumeack`（输入）：控制核停/继续的握手。
- `csr` / `csr_rd`：抽象命令读写 **CSR** 时，DM 把命令发给核的 CSR 模块、并接收读回数据。
- `scalar_rd` / `scalar_rs`：抽象命令读写 **GPR** 时用的写口与读口。
- `float_rd` / `float_rs`：抽象命令读写 **FPR** 时用的端口（仅 `enableFloat` 时存在）。
- `itcm` / `dtcm`：抽象命令做 **内存访问**（Access Memory）时直连 TCM 的 fabric 口。

真正实例化 `DebugModule` 的地方在顶层 `CoreAxi` 里：[CoreAxi.scala:67-68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L67-L68) 创建了 `dm`，并用一个 2 路轮询仲裁器 `dmReqArbiter` 把**两路请求源**汇入同一个 DM（[CoreAxi.scala:71-73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L71-L73)）：

- `in(0)`：`io.dm.req`，即 CoreAxi 对外暴露的 `dm` 端口——它最终连到 `CoreAxiCSR`，也就是**外部 AXI 调试器**的入口；
- `in(1)`：`csr.io.debug.req`，CoreAxi 内部自建的那份 `CoreCSR`。

因为两路共享同一个 DM，[CoreAxi.scala:76-94](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L76-L94) 用一个 1 项的 `inflight` 队列记住「当前在飞的是哪一路」，再据此把响应 `rsp` 路由回正确的请求方（`rspId===1.U` 回 CoreCSR，`===0.U` 回外部 dm 端口）。这部分是「布线」，理解到「DM 只有一个，外面有两路想用它，所以要仲裁和回送路由」即可。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 DM 的「调试器侧」与「核侧」端口分界。

**步骤**：
1. 打开 [Debug.scala:96-119](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L96-L119)，把每个端口按「调试器方向 / 核方向」分两类。
2. 在 [CoreAxi.scala:126-130](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L126-L130) 找到 `haltreq`/`resumereq` 如何被接到核、`halted`/`running` 如何被回采。

**需要观察的现象**：
- `dm.io.halted(0) := core.io.dm.debug_mode`——「halted」其实等价于「核当前在 debug_mode」，而不是某个独立的停机寄存器。
- `dm.io.running(0) := !core.io.dm.debug_mode`——running 恰是 halted 的反。

**预期结果**：你应当得出结论——DM 自己**不直接知道**核停没停，它依赖核回采的 `debug_mode` 信号；DM 只负责「请求停 / 请求继续」，停没停下来由核反馈。**待本地验证**：可结合第 5 节综合实践，在波形里确认 `haltreq` 拉高后若干拍 `halted` 才变高。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DM 需要一个仲裁器把两路请求汇入？能不能让 `CoreCSR` 和外部 dm 端口各自接一个 DM？

> **答案**：DM 内部维护的是**同一份**核的状态（`data0`、`dmcontrol`、抽象命令执行进度），若有两个 DM，两者看到的核状态会不一致（例如一个刚把 `a0` 读进 `data0`，另一个却返回旧值）。所以 DM 必须单点，多路请求靠仲裁器排队。

**练习 2**：`halted` 信号是 DM 自己产生的，还是核回采的？

> **答案**：核回采的。代码里 `dm.io.halted(0) := core.io.dm.debug_mode`，DM 只是个转发/镜像。

---

### 4.2 内部寄存器：dmcontrol / dmstatus / abstractcs / command

#### 4.2.1 概念说明

DM 内部有一组寄存器，调试器通过读写它们来「下命令」和「看状态」。规范给每个寄存器分配了一个字节地址（注意：这是 **DM 内部地址**，不是 AXI 地址）。本讲最关键的四个：

| DM 内部地址 | 名字 | 作用 |
|------------|------|------|
| `0x04` | `data0` | 抽象命令的数据寄存器：读 GPR 的结果放这、写 GPR 的源数据也放这。 |
| `0x10` | `dmcontrol` | 控制寄存器：发 halt/resume、复位 DM、激活 DM。 |
| `0x11` | `dmstatus` | 状态寄存器（只读）：核在跑 / 停了 / 已复位。 |
| `0x16` | `abstractcs` | 抽象命令状态：`busy`（命令在执行）、`cmderr`（错误码）。 |
| `0x17` | `command` | 抽象命令寄存器：写一个编码就触发一条抽象命令。 |

地址常量集中定义在 [Debug.scala:34-43](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L34-L43) 的 `DebugModuleAddress` 对象里。

#### 4.2.2 核心流程与位域

请求结构体 `DebugModuleReqIO` 提供了一组「识别这次请求在打哪个寄存器、是什么命令」的辅助方法，见 [Debug.scala:53-82](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L53-L82)。其中对 `command` 寄存器内容的拆解尤其重要（`command` 的 `data` 字段就是抽象命令的编码）：

- `cmdtype = data(31,24)`——命令类型（[Debug.scala:71](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L71)），`0` = Access Register，`2` = Access Memory。
- `write  = data(16)`——读还是写（[Debug.scala:72](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L72)）。
- `regno  = data(15,0)`——目标寄存器编号（[Debug.scala:76](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L76)）。
- `aarsize= data(22,20)`——访问位宽（[Debug.scala:75](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L75)），CoralNPU 只支持 `2`（32 位）。

各寄存器的位域与文档 [debug.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md) 的表格一一对应，下面把源码里真正用到的位提炼出来（比文档更精确）：

**dmcontrol（[Debug.scala:128-163](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L128-L163)）**

| 位 | 名字 | 含义（源码依据） |
|----|------|----------------|
| 31 | `haltreq` | 写 `dmcontrol` 时取 `data(31)`，电平保持到下次写（[L137-L139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L137-L139)） |
| 30 | `resumereq` | 取 `data(30)`，核回 `resumeack` 时自动清零（[L141-L144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L141-L144)） |
| 1  | `ndmreset` | 取 `data(1)`，输出到核复位（[L163](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L163)） |
| 0  | `dmactive` | 取 `data(0)`；为 0 时把 `cmderr`、`data0` 复位（[L129](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L129)、[L237](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L237)、[L285](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L285)） |

> 注意 `dmcontrol` 复位初值是 `1.U`（[L128](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L128)），即上电时 `dmactive=1`、DM 默认就处于激活态。`LegalizeDmcontrol`（[L151-L159](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L151-L159)）会把 `hartsel` 限制在 0–1（因为 `nHart=1`，只有一个核）。

**dmstatus（[Debug.scala:165-178](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L165-L178)）** 是只读的组合信号，每拍实时镜像核的状态：`anyhalted`/`allhalted`（位 8/9）来自 `io.halted`，`anyrunning`/`allrunning`（位 10/11）来自 `io.running`，`version`（位 3:0）恒为 `3`。

**abstractcs（[Debug.scala:240-250](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L240-L250)）** 的两个有用位：

| 位 | 名字 | 含义 |
|----|------|------|
| 12 | `busy` | `= abstractCmdValid && !abstractCmdComplete`——抽象命令正在执行（[L239](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L239)） |
| 10:8 | `cmderr` | 抽象命令错误码（见 4.4），写 1 清零（W1C） |

**command（[Debug.scala:71-76](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L71-L76)）**：写它即触发抽象命令，编码见 4.4 节。

#### 4.2.3 源码精读

`dmstatus` 的拼接最能体现「DM 的状态其实是核状态的镜像」这一点：

```scala
// Debug.scala:165-178 —— dmstatus 完全由 io.halted/io.running 组合而来
dmstatus := Cat(
  0.U(14.W),
  resumeack.reduce(_&_).asUInt, // allresumeack
  resumeack.reduce(_&_).asUInt, // anyresumeack
  0.U(4.W),
  io.running.reduce(_&_).asUInt, // allrunning
  io.running.reduce(_|_).asUInt, // anyrunning
  io.halted.reduce(_&_).asUInt,  // allhalted
  io.halted.reduce(_|_).asUInt,  // anyhalted
  1.U(1.W), // authenticated
  0.U(3.W),
  3.U(4.W)  // version
)
```

因为只有一个 hart（`nHart=1`），`reduce(_&_)` 与 `reduce(_|_)` 其实等价于取 `halted(0)`/`running(0)`，但代码仍按规范写成 all/any 两套位，方便未来扩展多核。

#### 4.2.4 代码实践（源码阅读型）

**目标**：核对文档位域表与源码一致。

**步骤**：对照 [debug.md 的「Internal Debug Module Registers」](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md) 与 [Debug.scala:240-250](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L240-L250) 的 `abstractcs` 拼接。

**需要观察的现象**：文档把 `abstractcs` 的位 7:0 笼统标为 reserved，但源码在位 3:0 放了 `datacount=1`（表示 DM 有 1 个 `data` 寄存器）。

**预期结果**：源码比文档表更细——`datacount`（位 3:0）= 1。这是正常现象，文档做了简化。

#### 4.2.5 小练习与答案

**练习**：调试器想确认「DM 还活着、没被复位」，应该读哪个寄存器的哪一位？

> **答案**：读 `dmcontrol` 的位 0 `dmactive`。它复位值为 1；若调试器之前写过 `dmcontrol=0` 把 DM 复位，再写回 1 后可通过读 `dmactive` 确认 DM 已重新激活。`cmderr` 也会在 `dmactive` 从 0→1 时被清零（[L237](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L237)）。

---

### 4.3 halt / resume 握手

#### 4.3.1 概念说明

halt/resume 是一组**电平握手**：调试器把 `haltreq` 拉高，请求核进入 debug_mode；核真的停下来后，把 `halted` 拉高告诉 DM「我停了」。resume 类似：调试器拉 `resumereq`，核继续跑后会回一个 `resumeack` 脉冲，DM 收到后自动把 `resumereq` 清掉。

关键点：**抽象命令只在核 halt 之后才能执行**。这是规范要求——你不能在核正跑着、寄存器值随时在变的时候去读 GPR。

#### 4.3.2 核心流程

halt/resume 的状态流转可以这样描述（伪状态机，实际由几条 `MuxCase` 表达）：

```
调试器写 dmcontrol.haltreq=1
        │
        ▼  (DM 把 haltreq 电平保持)
   核收到 debug_req，进入 debug_mode
        │
        ▼  (CoreAxi 把 debug_mode 接到 dm.io.halted)
   dmstatus.anyhalted/allhalted = 1   ← 调试器可轮询确认
        │
        ▼  调试器执行抽象命令（读 a0 → data0）
        │
调试器写 dmcontrol.resumereq=1
        │
        ▼  核退出 debug_mode，继续执行
   CoreAxi 检测到 debug_mode 的 1→0 跳变，回采一个 resumeack 脉冲
        │
        ▼
   DM 收到 resumeack，自动把 resumereq 清零；resumeack 寄存器置位
```

#### 4.3.3 源码精读

`haltreq` 是电平保持型——写一次就一直有效，直到下次写 `dmcontrol`：

```scala
// Debug.scala:135-149
val dmcontrol_wvalid = (req.fire && req.bits.isAddrDmcontrol && req.bits.isWrite)
for (i <- 0 until nHart) {
  haltreq(i) := MuxCase(haltreq(i), Seq(
    dmcontrol_wvalid -> req.bits.data(31),   // 写 dmcontrol 时刷新
  ))
  val resumereq_i = MuxOR(dmcontrol_wvalid, req.bits.data(30))
  resumereq(i) := MuxCase(resumereq(i), Seq(
    dmcontrol_wvalid -> resumereq_i,         // 写时置位
    io.resumeack(i) -> false.B,              // 收到 resumeack 自动清零
  ))
  ...
}
```

而 `haltreq` 如何真正让核停下来，发生在 `CoreAxi`：它把 `haltreq(0)` 同时当成**中断/调试请求**注入核（[CoreAxi.scala:108](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L108) `core.io.irq := irq_reg || dm.io.haltreq(0)` 和 [L126](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L126) `core.io.dm.debug_req := dm.io.haltreq(0)`）。核进入 debug_mode 后：

```scala
// CoreAxi.scala:126-130 —— DM 与核的握手闭环
core.io.dm.debug_req := dm.io.haltreq(0)
core.io.dm.resume_req := dm.io.resumereq(0)
dm.io.resumeack(0) := !core.io.dm.debug_mode && RegNext(core.io.dm.debug_mode, false.B) // 1→0 跳变即 ack
dm.io.halted(0)     := core.io.dm.debug_mode
dm.io.running(0)    := !core.io.dm.debug_mode
```

第 128 行尤其精妙：`resumeack` 不是核主动发的信号，而是 `CoreAxi` 用「`debug_mode` 的下降沿」**综合**出来的——核一旦真的恢复执行（debug_mode 从 1 变 0），就产生一个 `resumeack` 脉冲。

> 关于 **single-step**：文档概述里提到 DM 支持 single-step，但单步的**触发逻辑并不在 `Debug.scala` 里**。它实际由核内的 CSR（DCSR 的 `step` 位）驱动：[SCore.scala:220-223](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L220-L223) 在每条指令派发后产生一个 `stepTriggered`，作为一次内部 `debug_req` 注入 CSR 模块。换言之，DM 只负责「外部 haltreq」这一路，单步是核自己产生的另一路 debug_req，二者在 [SCore.scala:222-223](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L222-L223) 汇成 `csr.io.dm.debug_req`。读 `Debug.scala` 时不要去找「step 寄存器」——它不在那。

#### 4.3.4 代码实践（阅读测试）

**目标**：看一个真实的 halt→读寄存器→resume 端到端用例。

**步骤**：打开 [tests/cocotb/core_mini_axi_debug.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/core_mini_axi_debug.py)，找到 `math.elf` 那段（[L55-L66](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/core_mini_axi_debug.py#L55-L66)）。

**需要观察的现象**：GDB 命令序列 `break math` → `continue` → `continue` → `finish` → 检查 `$a0 == 5`。每一条 `continue` 背后都对应一次「resumereq → resumeack」握手；`break` 命中时核被 halt，GDB 才能读 `$a0`。

**预期结果**：理解「GDB 的一条 `continue`/`break` 在硬件层就是一串对 `dmcontrol` 的写和 `dmstatus` 的轮询」。**待本地验证**：运行第 5 节综合实践可看到这些命令实际通过。

#### 4.3.5 小练习与答案

**练习**：为什么 `resumereq` 用「写置位、收到 ack 自动清零」，而 `haltreq` 却是「电平保持」？

> **答案**：`haltreq` 是持续性请求——核可能在跑长指令，调试器要持续要求它停，直到它真停下（halted=1）；而 `resume` 是一次性动作，核一旦恢复就应立刻撤销请求，否则会立刻又被请求停（形成抖动）。所以 `resumereq` 设计成脉冲式：收到 `resumeack` 即自清。

---

### 4.4 抽象命令：Access Register

#### 4.4.1 概念说明

抽象命令是 DM 最有用的能力：调试器写一个编码到 `command` 寄存器，DM 就替你去读/写一个 GPR/FPR/CSR，把结果放进/取出 `data0`。本讲聚焦 **Access Register（cmdtype=0）**。

`command` 寄存器对 Access Register 的编码（[debug.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md)）：

| 位 | 名字 | 含义 |
|----|------|------|
| 31:24 | `cmdtype` | `0` = Access Register |
| 22:20 | `aarsize` | 访问位宽，CoralNPU 只支持 `2`（32 位） |
| 16 | `write` | `1`=写、`0`=读 |
| 15:0 | `regno` | 目标寄存器编号 |

`regno` 的取值范围把三类寄存器分开（[Debug.scala:193-196](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L193-L196)）：

- `0x0000–0x0FFF`：CSR
- `0x1000–0x101F`：标量 GPR（`x0`–`x31`），故 `a0`（= `x10`）的 `regno = 0x1000 + 10 = 0x100A`
- `0x1020–0x103F`：浮点 FPR

#### 4.4.2 核心流程：读 GPR a0 的完整数据流

这是本讲的重头戏。读 `a0` 的 `command` 编码是 `cmdtype=0, write=0, regno=0x100A`，即 `data = 0x0000_100A`。从 AXI 写入到最终读出 `data0` 的流程：

```
1) 调试器经 AXI 写「内部地址 0x17(command)、数据 0x0000100A、操作 WRITE」
       │  （这一步怎么送达 DM，见 4.5）
       ▼
2) DebugModule 收到 req：abstractCmdValid=1（写 command 寄存器）
       │  regnoIsScalar=1（0x1000 ≤ 0x100A < 0x1020）
       │  cmdtypeIsAccessRegister=1, write=0(读)
       ▼
3) 前置检查：必须 io.halted(0)=1，且 aarsize==2，否则置 cmderr
       │
       ▼
4) 标量读路径：io.scalar_rs.idx := regno(4,0) = 10   ← 组合读 a0
       │  寄存器堆当拍把 a0 的值送到 io.scalar_rs.data
       ▼
5) abstractCmdComplete 命中「AccessRegister && regnoIsScalar && !write」=1（当拍完成）
       │
       ▼
6) data0 ← io.scalar_rs.data   ← a0 的值落进 data0（L281）
       │  rsp.op = SUCCESS；req.fire
       ▼
7) 调试器再发一笔「读内部地址 0x04(data0)、操作 READ」
       │
       ▼
8) DM 把 data0 作为 rsp.data 回送 → 调试器拿到 a0 的值
```

关键在于：GPR 的读是**组合完成**的（寄存器堆读口当拍出数据），所以 `busy` 几乎不会为真。`busy` 只在需要等待核内慢路径时才出现——例如读 CSR 要等 `csr_rd.valid`、读内存要等 `itcm.readData.valid`、写 GPR 要等 `scalar_rd.fire`（写口反压）。

#### 4.4.3 源码精读

**前置检查与 cmderr**（[Debug.scala:232-238](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L232-L238)）：

```scala
cmderr := MuxCase(cmderr, Seq(
  abstractcs_wvalid -> (cmderr & ~(req.bits.data(10,8))), // W1C 清除
  (abstractCmdValid && !io.halted(0)) -> 4.U(3.W),         // 核没 halt → cmderr=4
  (... && sizeInvalid) -> 2.U(3.W),                        // aarsize!=2 → cmderr=2
  (... && cmdtypeIsAccessMemory && !(itcm||dtcm)) -> 5.U(3.W),
  !dmactive -> 0.U(3.W),
))
```

错误码含义：`2`=不支持的 size，`4`=核未 halt，`5`=访问的内存地址不在 TCM。注意错误经 `cmderr`（即 `abstractcs`）回报，**而不是** `rsp.op`——`rsp.op` 对合法的 Access Register 通常仍是 `SUCCESS`（[L304](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L304)）。源码注释 [L305](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L305) 明说：「We report failure (if necessary) via cmderr」。

**完成判定** `abstractCmdComplete`（[Debug.scala:213-222](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L213-L222)）：用一张大 `MuxCase` 列出所有「命令算完成」的条件。读 GPR 的条件就是 `cmdtypeIsAccessRegister && regnoIsScalar && !req.bits.write`——纯组合，当拍成立。

**GPR 读写驱动**（[Debug.scala:252-256](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L252-L256)）：

```scala
val scalarRegno = req.bits.regno(4,0)               // 取低 5 位作寄存器号
io.scalar_rd.valid := ... && regnoIsScalar && req.bits.write  // 写 GPR
io.scalar_rd.bits.addr := scalarRegno
io.scalar_rd.bits.data := data0                     // 写源 = data0
io.scalar_rs.idx := MuxOR(... && regnoIsScalar && !req.bits.write, scalarRegno) // 读 GPR
```

**结果回收** `data0` 的更新（[Debug.scala:278-286](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L278-L286)）是一个多路选择：读 GPR 命中时 `data0 ← io.scalar_rs.data`；读 CSR 时 `data0 ← io.csr_rd.bits`；读内存时 `data0 ← io.itcm/dtcm.readData.bits`（还要按地址做字节旋转）。这一处是「把核内资源搬到调试器可见的 `data0`」的总汇集点。

**CSR 访问的特殊通路**：读 GPR 是 DM 直连寄存器堆读口，但读 CSR 不行——CSR 必须走核的 CSR 执行机制。所以 [Debug.scala:223-228](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L223-L228) 把它拼成一条 `CsrCmd`（写用 `CSRRW`、读用 `CSRRC`），由 SCore 里的 CSR 仲裁器执行（[SCore.scala:199-200](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L199-L200)），结果经 `csr_rd` 回送。

#### 4.4.4 代码实践（阅读测试）

**目标**：看真实测试如何断言抽象命令读寄存器。

**步骤**：在 [tests/cocotb/BUILD:204-216](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/BUILD#L204-L216) 找到测试用例清单，关注 `core_mini_axi_debug_abstract_access_registers`、`core_mini_axi_debug_scalar_registers`、`core_mini_axi_debug_abstract_access_nonexistent_register`。

**需要观察的现象**：有专门测「访问不存在的寄存器」的用例——它对应 `regnoInvalid` 分支（[Debug.scala:196](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L196)），此时 [L303](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L303) 直接回 `rsp.op=FAILED`（这是少数不走 cmderr、直接 FAILED 的情况）。

**预期结果**：理解「合法 regno→SUCCESS+可能 cmderr；非法 regno→直接 FAILED」的双轨错误回报。

#### 4.4.5 小练习与答案

**练习 1**：调试器忘了先 halt 就直接发「读 a0」，会发生什么？

> **答案**：`abstractCmdValid && !io.halted(0)` 成立，`cmderr` 被置为 `4`（[L234](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L234)）。同时由于 `abstractCmdComplete` 里有 `|| !io.halted(0)`（[L222](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L222)），命令「立即完成」、`busy` 不拉高，`rsp.op` 仍是 SUCCESS——错误只能靠调试器去读 `abstractcs.cmderr` 才能发现。

**练习 2**：读 GPR 几乎不拉 `busy`，那什么情况下 `busy` 会真的拉高？

> **答案**：当完成条件依赖核内慢路径时——读 CSR 要等 `io.csr_rd.valid`、写 GPR 要等 `io.scalar_rd.fire`（写口可能反压）、读内存要等 `io.itcm/dtcm.readData.valid`。见 [Debug.scala:213-221](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L213-L221)。

---

### 4.5 AXI CSR 通信协议：req_addr / req_op / status

#### 4.5.1 概念说明

到目前为止，我们一直说「调试器写 DM 内部地址 0x17」，但 DM 内部地址不是 AXI 地址——调试器怎么「写 0x17」？答案是 `CoreCSR` 提供了一组**AXI 内存映射寄存器**作为「DM 内部地址空间」的代理。这组寄存器在 [debug.md 的「AXI CSR Interface」](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md) 里定义，落在 CSR 区基址 `0x30000` 的 `0x800` 偏移处，即绝对地址 `0x30800` 一带：

| AXI 地址 | 名字 | 作用 |
|----------|------|------|
| `0x30800` | `req_addr` | 写：要访问的 DM 内部地址（如 `0x17`=command） |
| `0x30804` | `req_data` | 写：要写入的数据 |
| `0x30808` | `req_op` | 写：操作码（`1`=READ、`2`=WRITE）；写它即**触发**一次 DM 请求 |
| `0x3080c` | `rsp_data` | 读：DM 返回的数据 |
| `0x30810` | `rsp_op` | 读：DM 返回的状态（`0`=SUCCESS、`2`=FAILED） |
| `0x30814` | `status` | 读：bit0=模块空闲可接新请求、bit1=有响应可读；写：消费响应 |

这些偏移定义在 [CoreAxiCSR.scala:22-29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L22-L29) 的 `CoreCsrAddrs`。

#### 4.5.2 核心流程：一次「写 DM 内部寄存器」的状态机

写操作的协议是一个**轮询握手**（不是中断驱动），调试器要主动 poll：

```
1) 读 status，等 bit0==1（DM 空闲）
2) 写 req_addr  = 目标内部地址（如 0x10）
3) 写 req_data  = 数据
4) 写 req_op    = 2(WRITE)            ← CoreCSR 据此产生单拍 req 脉冲
       │
       ▼  CoreCSR 把 (addr,data,op) 送进 DebugModule
5) 读 status，等 bit1==1（响应就绪）
6) 读 rsp_op   确认 SUCCESS
7) 写 status    消费（ack）响应       ← 出队 rsp_queue
```

读操作类似，只是第 3 步省略、第 6 步多读一个 `rsp_data`。

#### 4.5.3 源码精读

`CoreCSR` 是把 AXI 写「翻译」成 DM `req` 的核心。它用三个保持寄存器缓存最近一次写的 `addr`/`data`/`op`（[CoreAxiCSR.scala:57-59](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L57-L59)），然后**只在写 `req_op` 那一拍**产生一个单周期 valid 脉冲：

```scala
// CoreAxiCSR.scala:71-80
val req_valid_pulse = RegInit(false.B)
val write_to_op_reg = writeEn && writeAddr === CoreCsrAddrs.DbgReqOp
req_valid_pulse := Mux(write_to_op_reg && io.debug.req.ready, true.B, false.B)
io.debug.req.valid := req_valid_pulse
io.debug.req.bits.address := debugReqAddrReg
io.debug.req.bits.data    := debugReqDataReg
val (req_op, req_op_valid) = DmReqOp.safe(debugReqOpReg)
io.debug.req.bits.op := Mux(req_op_valid, req_op, DmReqOp.NOP)
```

注意「写 `req_op` 才触发」是个很巧妙的设计：调试器可以先从容地写 `req_addr`、`req_data`（它们只是改保持寄存器），最后写 `req_op` 时才真正「扣扳机」。这样调试器不用担心写顺序的时序。

响应侧用一个深度 1 的队列缓存 DM 的 `rsp`（[CoreAxiCSR.scala:67-68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L67-L68)），并用「写 `status` 寄存器」作为出队信号（[L83-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L83-L84)）：

```scala
val write_to_status_reg = writeEn && writeAddr === CoreCsrAddrs.DbgStatus
rsp_queue.io.deq.ready := write_to_status_reg   // 写 status 即 ack 响应
```

而 `status` 读出的值（[L114](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L114)）正好对应文档定义的两bit：

```scala
val debugStatusReg = Cat(rsp_queue.io.deq.valid, io.debug.req.ready)
//                       bit1: 有响应可读          bit0: 空闲可接新请求
```

读写映射表见 [CoreAxiCSR.scala:115-122](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L115-L122)（读）与 [L161-163](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L161-L163)（写保持寄存器）。

> 小细节：`req_data` 是 64 位 AXI 总线里取**高 32 位**（`writeData(63,32)`，[L162](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L162)）写入保持寄存器——这是因为调试端口寄存器和旁边 0x0/0x4 复用/PC 寄存器被分组到同一个 AXI 对齐块里，各占一个 32 位车道。这是 [u3-l5](u3-l5-csr-boot-control.md) 讲过的「按地址对齐取固定车道」的同一套机制。

#### 4.5.4 代码实践（源码阅读型）

**目标**：把 AXI 地址 ↔ DM 内部地址的翻译链走通。

**步骤**：
1. 在 [CoreAxiCSR.scala:22-29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L22-L29) 确认六个偏移。
2. 对照 [debug.md 的 AXI CSR Interface 表](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md)，确认 `0x800 + 基址 0x30000 = 0x30800`。
3. 走一遍「读 a0」所需的全部 AXI 写/读序列：先 halt（写 req_addr=0x10, req_data=0x80000000, req_op=2），轮询 dmstatus（req_addr=0x11, req_op=1）等 anyhalted=1，再发抽象命令（req_addr=0x17, req_data=0x0000100A, req_op=2），轮询 abstractcs busy 清零，最后读 data0（req_addr=0x04, req_op=1）。

**需要观察的现象**：每一次「写 req_op」才扣扳机，写 req_addr/req_data 只是改保持寄存器、不触发 DM。

**预期结果**：能用一张表把「GDB 概念 → AXI 地址序列」对上。**待本地验证**。

#### 4.5.5 小练习与答案

**练习**：为什么协议要求调试器在最后「写 status」来 ack 响应，而不是让硬件自动出队？

> **答案**：让调试器显式 ack，可以保证调试器**已经取走** `rsp_data`/`rsp_op` 之后才释放响应槽。若硬件一返回就自动出队，深度仅 1 的 `rsp_queue` 可能在调试器还没读 `rsp_data` 时就被下一笔响应覆盖。显式 ack 把「读结果」和「释放」解耦，符合 RISC-V 调试规范的 poll-then-ack 习惯。

---

## 5. 综合实践

本实践把四个最小模块串起来：用真实 GDB 经 pyOCD 驱动 Debug 模块，做一次「halt → 读 a0 → 单步 → resume」的完整调试，并在源码里追踪对应的硬件行为。它正是 [debug.md「Reading a GPR (a0)」示例](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md) 的可运行版本。

### 实践目标

1. 跑通一个端到端调试用例，确认 Debug 模块在仿真上可用。
2. 把「GDB 一条命令」对应到「一组 AXI 写 + DM 抽象命令 + 寄存器堆读口」的硬件链路。

### 操作步骤

**A. 运行官方 cocotb 调试回归（推荐，最快看到效果）**

仓库已经备好了一个用 GDB 驱动 Debug 模块的 cocotb 测试套件，目标为 `//tests/cocotb:core_mini_axi_debug_cocotb`（见 [tests/cocotb/BUILD:361-368](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/BUILD#L361-L368)）。在仓库根目录执行（Verilator 路径）：

```bash
bazel test --test_filter=core_mini_axi_debug_scalar_registers //tests/cocotb:core_mini_axi_debug_cocotb
```

如果想看 GDB server 的完整命令交互（读 `f0`、设断点、单步），跑最综合的用例：

```bash
bazel test --test_filter=core_mini_axi_debug_gdbserver //tests/cocotb:core_mini_axi_debug_cocotb
```

涉及的 GDB 命令脚本在 [tests/cocotb/core_mini_axi_debug.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/core_mini_axi_debug.py) 里（`info reg f0`、`break`、`continue`、单步），驱动它的 pyOCD GDB server 在 [core_mini_axi_pyocd_gdbserver.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_pyocd_gdbserver.py)。

**B. 源码追踪：把「读 a0」画成时序链**

对照 [debug.md「Reading a GPR (a0)」](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/debug.md) 的四步，在源码里逐跳标注：

1. **触发命令**：调试器写 `req_addr=0x17`、`req_data=0x0000100A`、`req_op=2`（[CoreAxiCSR.scala:71-80](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L71-L80) 产生单拍 `req.valid` 脉冲）。
2. **DM 解释命令**：[Debug.scala:190-197](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L190-L197) 得出 `abstractCmdValid=1`、`regnoIsScalar=1`、`write=0`。
3. **读寄存器堆**：[Debug.scala:252-256](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L252-L256) 把 `scalarRegno=10` 送到 `scalar_rs.idx`，寄存器堆组合回送 `a0` 的值。
4. **结果落 data0**：[Debug.scala:280-281](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L280-L281) `data0 ← io.scalar_rs.data`；调试器随后读 `req_addr=0x04, req_op=1` 取走它（[CoreAxiCSR.scala:119](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L119) 的 `DbgRspData` 读映射）。

**C. 列出内部寄存器关键位域**

按本讲 4.2 的格式，整理一张表（dmcontrol / dmstatus / abstractcs / command），每行写「位域名—位号—源码行号—一句话作用」。

### 需要观察的现象

- 测试输出里 GDB 命令逐条通过（`break` 命中、`$a0==5`、单步后 PC 改变）。
- 源码链里「写 req_op」是唯一的扳机，其余写只是改保持寄存器。
- 读 GPR 不拉 `busy`；读 CSR/内存才会出现 `busy=1` 期间。

### 预期结果

- `bazel test` 报 `PASSED`。
- 你得到一张完整的「GDB 命令 → AXI 地址序列 → DM 内部寄存器 → 核内资源」对照表。

> 若本机没有 Bazel/Verilator 环境，**步骤 A 标注为「待本地验证」**，仅完成 B、C 的源码追踪即可达成本讲学习目标。

---

## 6. 本讲小结

- CoralNPU 的 Debug 模块（`DebugModule`）实现 RISC-V External Debug 规范的子集，是一个**独立于流水线的旁路模块**：对外经 AXI CSR 桥（`CoreCSR`）暴露给调试器，对内用 `haltreq`/`resumereq` 控制核、用抽象命令读写核内资源。
- 外部调试器通过 `0x30800` 一带的六个 AXI 寄存器（`req_addr`/`req_data`/`req_op`/`rsp_data`/`rsp_op`/`status`）与 DM 通信，协议是 **poll-then-ack**：写 `req_op` 扣扳机、轮询 `status`、读结果、写 `status` 消费响应。
- **halt/resume** 是电平握手：`haltreq` 保持到核进入 `debug_mode`（`halted` 由 `debug_mode` 回采）；`resumereq` 在 `debug_mode` 的下降沿（即 `resumeack`）自动清零。**单步**不在 `Debug.scala`，而在 CSR 的 DCSR step 位（[SCore.scala:220-223](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L220-L223)）。
- **抽象命令 Access Register**：写 `command` 寄存器即触发；`regno` 区分 CSR（`0x0000–0x0FFF`）/ GPR（`0x1000–0x101F`）/ FPR（`0x1020–0x103F`）。读 GPR 经组合读口当拍完成、结果落 `data0`；读 CSR 须经核的 CSR 执行通路。
- 错误回报是**双轨**的：非法 `regno` 直接 `rsp.op=FAILED`；其余错误（未 halt、size 不支持、地址不在 TCM）经 `abstractcs.cmderr` 回报，`rsp.op` 仍可能是 SUCCESS——调试器必须检查 `cmderr`。
- `CoreAxi` 用一个 2 路轮询仲裁器把「外部 AXI 调试器」和「内部 CoreCSR」两路请求汇入唯一的 DM，并用 `inflight` 队列把响应路由回正确的请求方。

---

## 7. 下一步学习建议

- **RVVI 指令追踪（[u9-l2](u9-l2-rvvi-trace.md)）**：本讲关注「调试器如何控制核」，下一讲转向「如何在仿真中逐条观察核执行的指令」，二者都是「可观测性」主题，可对照阅读 `RvviTrace.scala`。
- **CSR 模块（[u5-l3](u5-l3-regfile-csr.md)）**：单步、debug_mode、`dcsr` 都在 CSR 里。想搞懂「核收到 debug_req 后如何精确地在指令边界进入 debug_mode」，需要回到 CSR 与派发屏障（[SCore.scala:90-91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L90-L91)、`dispatch.io.single_step`/`debug_mode`）。
- **动手扩展（可选）**：试着读 [Access Memory 命令](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L324-L345)（`cmdtype=2`，直连 ITCM/DTCM fabric 口、支持 `aampostincrement` 自增），对照 `data1` 寄存器的更新逻辑（[L288-L291](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Debug.scala#L288-L291)），理解调试器如何批量搬运内存——这是 GDB `load`/`dump` 命令背后的硬件基础。
