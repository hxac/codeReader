# CSR 接口、内存映射与启动控制

## 1. 本讲目标

CoralNPU 是一颗挂在 AXI 总线上的协处理器，它本身不会「自己跑起来」——必须由外部主机（SoC 里的主 CPU）通过一组**外部可见的 CSR（Control & Status Register）**来加载程序、释放复位、并在结束时读取状态。本讲学完后，你应当能够：

1. 说出 CoralNPU 对外暴露的三个核心 CSR（`RESET_CONTROL` / `PC_START` / `STATUS`）的地址偏移、位域含义与读写权限。
2. 在 [CoreAxiCSR.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala) 中精确定位这三个寄存器的硬件实现，解释复位/时钟门控/PC/状态位是如何被 AXI 写读事务驱动的。
3. 默写出 CoralNPU 的标准启动序列：**加载 ITCM → 写 PC → 释放时钟门控 → 释放复位 → 轮询 STATUS**，并能解释每一步对应的硬件行为。
4. 读懂 `CoreAxiCSRTest` 是如何用 Chisel 仿真验证这些 CSR 的读写与错误回报的。

本讲承接 [u3-l2 AXI 接口与外部系统集成](u3-l2-axi-integration.md)——上一讲讲了 CoralNPU 怎么用 AXI 搬运指令/数据，本讲则聚焦于 AXI 之上的「控制面」：外部主机如何通过 CSR 操控这颗核的生命周期。

## 2. 前置知识

- **CSR（控制状态寄存器）**：这里指的是 CoralNPU **对外可见的控制寄存器**，由主机经 AXI 读写，用来查询/控制 CoralNPU。它和 RISC-V 指令集里的 `Zicsr` CSR（如 `mstatus`、`mtvec`，由核内软件经 `csrrw` 指令访问）**不是一回事**——[integration_guide.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md) 在 CSR 章节开头专门强调了这一点。
- **同步复位（synchronous reset）**：复位信号只在时钟有效沿才起作用。这意味着要让复位生效，时钟必须至少跑一个周期——这正是启动序列里「先开时钟、再解除复位」的根本原因。
- **时钟门控（clock gating）**：把某个模块的时钟停掉以省电。CoralNPU 上电默认就把核流水线的时钟门控掉，外部主机必须显式「开钟」。
- **AXI 读写事务**：写 = AW（写地址）+ W（写数据）+ B（写响应）；读 = AR（读地址）+ R（读数据）。本讲的 CSR 就是一个 AXI subordinate（从机）。
- **内存映射（memory-mapped）**：寄存器被映射到一段地址空间，主机像读写内存一样用普通的 load/store 指令访问它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [doc/integration_guide.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md) | 集成手册，给出 CoralNPU 的内存映射表、启动流程 C 代码示例，以及三个 CSR 的位域定义表。是本讲的「规格说明书」。 |
| [hdl/chisel/src/coralnpu/CoreAxiCSR.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala) | CSR 的 Chisel 实现。`CoreCSR` 是核心寄存器逻辑，`CoreAxiCSR` 是把 `CoreCSR` 包上一层 AXI subordinate 接口的封装。本讲精读的核心。 |
| [hdl/chisel/src/coralnpu/CoreAxiCSRTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSRTest.scala) | Chisel 仿真测试，验证 CSR 的初始化、读、写、以及写非法地址时的错误回报。 |
| [hdl/chisel/src/coralnpu/CoreAxi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala) | 内核顶层，把 `CoreCSR` 的输出（`reset`/`cg`/`pcStart`）接到核的复位、时钟门控与 PC 输入上。用来理解 CSR 的「下游消费者」。 |

---

## 4. 核心概念与源码讲解

### 4.1 外部可见 CSR 与内存映射

#### 4.1.1 概念说明

外部主机要控制 CoralNPU，本质上就是回答三个问题：

- **怎么开始？**（设好起始 PC、解除复位）
- **现在状态如何？**（核停了吗？出错了吗？）
- **怎么复位/省电？**（把核拉回复位、门控时钟）

这三个问题对应三个寄存器：`RESET_CONTROL`（复位与时钟门控）、`PC_START`（起始地址）、`STATUS`（状态）。它们被集中放在一段连续的地址区——即**CSR 区**。

#### 4.1.2 核心流程

[integration_guide.md:156-164](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L156-L164) 给出了 CoralNPU 的本地内存映射：

| 区域 | 地址范围 | 大小 | 说明 |
| :--- | :--- | :--- | :--- |
| ITCM | 0x0000 - 0x1FFF | 8KB | 指令存储 |
| DTCM | 0x10000 - 0x17FFF | 32KB | 数据存储 |
| CSR | 0x30000 - TBD | TBD | 控制/查询 CoralNPU 的 CSR 接口 |

可以看到 **CSR 区的基地址是 0x30000**。在 CSR 区内部，三个核心寄存器再以 4 字节为粒度排布：

\[
\text{完整地址} = \underbrace{\text{0x30000}}_{\text{CSR 区基址}} + \underbrace{\text{offset}}_{\text{寄存器内偏移}}
\]

- `RESET_CONTROL`：offset `0x0` → 全局地址 `0x30000`
- `PC_START`：offset `0x4` → 全局地址 `0x30004`
- `STATUS`：offset `0x8` → 全局地址 `0x30008`

这三个地址（`0x30000`/`0x30004`/`0x30008`）正是 [integration_guide.md:186-214](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L186-L214) 启动示例代码里反复出现的裸地址。注意：若 CoralNPU 在整个 SoC 中被映射到别的基址（例如文档示例里的 `0x70000000`），那么所有这些地址还要再加上 SoC 基址——但 CSR **内部**的相对偏移永远不变。

> 说明：`0x30000` 这个区基址由 SoC/fabric 的地址译码负责（命中 CSR 区的事务被路由给 CSR subordinate），而 `CoreCSR` 硬件本身只认低位偏移 `0x0/0x4/0x8`。本讲 4.2 会看到这一点。

#### 4.1.3 源码精读

三个寄存器的位域定义在 [integration_guide.md:222-248](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L222-L248)：

**`RESET_CONTROL`（offset 0x0，复位值 3 = 0b11）**

| 位 | 名称 | 含义 | 权限 |
| :--- | :--- | :--- | :--- |
| 0 | `RESET` | 1=核在复位中；0=不复位 | R/W |
| 1 | `CLOCK_GATE` | 1=时钟门控（停钟）；0=时钟运行 | R/W |
| 31:2 | 保留 | 写忽略，读返回 0 | R |

复位值 `0b11` 意味着**上电时核同时处于复位 + 时钟门控**——最安全、最省电的初始态。

**`PC_START`（offset 0x4，复位值 0）**：32 位起始地址，核解除复位后从这里开始取指。

**`STATUS`（offset 0x8，只读）**

| 位 | 名称 | 含义 |
| :--- | :--- | :--- |
| 0 | `HALTED` | 1=核已停（例如执行了 `mpause`） |
| 1 | `FAULT` | 1=核遇到 fault |

主机轮询 `STATUS` 即可知道程序是否跑完（`HALTED`）或出错（`FAULT`）。

#### 4.1.4 代码实践

**目标**：把「文档里的 CSR 表」和「源码里的偏移」对上号。

**步骤**：

1. 打开 [integration_guide.md:222-248](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L222-L248)，记下三个寄存器的 offset。
2. 打开 [CoreAxiCSR.scala:100-105](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L100-L105)，找到下面这段 `coreRegMap`：

```scala
// Map of core control registers.
val coreRegMap = Map(
  0x0 -> resetReg,
  0x4 -> pcStartReg,
  0x8 -> statusReg,
)
```

**观察**：源码里的键 `0x0 / 0x4 / 0x8` 与文档 `RESET_CONTROL / PC_START / STATUS` 的 offset 完全一一对应。这就是「文档是规格、源码是实现」的对照点。

**预期结果**：你能口述「写 0x30004 就是写 `pcStartReg`，读 0x30008 就是读 `statusReg`」。

#### 4.1.5 小练习与答案

**练习 1**：上电瞬间 `STATUS` 寄存器的 `HALTED` 和 `FAULT` 位各是多少？核是否在跑？

> **答案**：复位值是 0，即 `HALTED=0`、`FAULT=0`。但此时核并未在跑——因为它处于复位 + 时钟门控状态（`RESET_CONTROL=3`），`STATUS=0` 只表示「既没主动停也没报错」，并不代表「正在执行」。

**练习 2**：为什么 `STATUS` 是只读的，而 `RESET_CONTROL` 可读写？

> **答案**：`STATUS` 反映的是核的**实时**状态（停/故障），由硬件自己置位，主机只能读；`RESET_CONTROL` 是主机下发给核的**控制命令**，必须可写。

---

### 4.2 CoreCSR：三个寄存器的硬件实现

#### 4.2.1 概念说明

`CoreAxiCSR.scala` 里其实有两个模块：

- `CoreCSR`：纯粹的「寄存器 + 读写逻辑」，对外是一个内部 fabric 接口（`FabricIO`）。
- `CoreAxiCSR`：在 `CoreCSR` 外面套一个 `AxiSlave`，把 AXI 事务翻译成 fabric 读写，方便作为 AXI subordinate 被集成/测试。

在真实的内核顶层 [CoreAxi.scala:58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L58) 里，直接实例化的是 `CoreCSR`（因为顶层已有自己的 fabric 多路选择）；`CoreAxiCSR` 主要用于把 CSR 单独拎出来做 AXI 级测试。两者的寄存器逻辑是同一份。

理解这一节的关键，是搞清三件事：**复位值怎么来的**、**写事务怎么改寄存器**、**这些寄存器怎么驱动核的 reset/clock/PC**。

#### 4.2.2 核心流程

整条 CSR 控制链可以画成：

```
                AXI write/read
   外部主机 ─────────────────────► CoreCSR (resetReg/pcStartReg/statusReg)
                                         │
        ┌────────────────────────────────┼────────────────────────┐
        ▼                                ▼                        ▼
   io.reset(=resetReg[0])        io.cg(=resetReg[1])      io.pcStart
        │                                │                        │
        ▼                                ▼                        ▼
   拉核进入/退出复位              门控/放开核流水线时钟      注入核的起始 PC

   核内部 ──halted/fault──► statusReg := Cat(fault, halted) ──► 外部读 STATUS
```

要点：

1. **上电默认 `resetReg = 3`**（复位 + 时钟门控），所以 `io.reset=1`、`io.cg=1`。
2. 主机写 `RESET_CONTROL`（offset 0x0）→ 直接改 `resetReg`，进而改 `io.reset`/`io.cg`。
3. 主机写 `PC_START`（offset 0x4）→ 改 `pcStartReg`。
4. `statusReg` **不是主机写的**，而是每个周期由核反馈的 `halted`/`fault` 拼接而成，主机只能读。
5. 读路径：fabric 给出读地址 → 按 `coreRegMap`/`csrRegMap`/`debugReadMap` 三张表查到对应寄存器 → 打一拍流水（`Pipe`，为时序）→ 返回数据。

#### 4.2.3 源码精读

**① 复位值与寄存器声明**——[CoreAxiCSR.scala:47-54](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L47-L54)：

```scala
// Bit 0 - Reset (Active High)
// Bit 1 - Clock Gate (Active High)
// By default, be in reset and with the clock gated.
val resetReg = RegInit(3.U(p.fetchAddrBits.W))
// pcStartReg loads from boot_addr wire on the first clock after reset.
val pcStartReg = RegInit(0.U(p.fetchAddrBits.W))
val bootAddrCapture = RegInit(true.B)
val statusReg = RegInit(0.U(p.fetchAddrBits.W))
```

注释和复位值 `3.U` 直接对应文档里 `RESET_CONTROL` 复位值 = 3。`bootAddrCapture` 是个小机关（见 ③）。

**② 写逻辑**——[CoreAxiCSR.scala:156-163](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L156-L163)：

```scala
// Register write logic.
resetReg := Mux(writeEn && writeAddr === 0x0.U, writeData(31,0), resetReg)
pcStartReg := Mux(writeEn && writeAddr === 0x4.U, writeData(63,32), pcStartReg)
```

这里有两个非常关键的实现细节：

- **写使能受 `internal` 限制**：`writeEn = io.fabric.writeDataAddr.valid && !io.internal`（[第 61 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L61)）。`internal=1` 表示这笔事务来自 CoralNPU **内部**，此时写使能被屏蔽——即控制 CSR 只能由**外部**主机写，核自己不能改自己的复位/PC。
- **按地址选 32 位「车道」**：写 `0x0` 取 `writeData(31,0)`（第 0 个 32 位车道），写 `0x4` 取 `writeData(63,32)`（第 1 个车道）。这是因为 AXI 数据总线宽 128 位（`axi2DataBits`，见 [Parameters.scala:166](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L166)），一个 128 位 beat 里 packed 了 4 个 32 位寄存器。注意硬件**直接按地址取固定 bit 切片**，并不解析 AXI 的 `strb` 写使能字节掩码——所以主机必须把数据放到与地址对应的 32 位车道上（这一点 `CoreAxiCSRTest` 的 Write 用例有体现）。

**③ 输出与 status 拼装**——[CoreAxiCSR.scala:151-154](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L151-L154)：

```scala
io.reset := resetReg(0)
io.cg := resetReg(1)
io.pcStart := Mux(bootAddrCapture, io.bootAddr, pcStartReg)
statusReg := Cat(io.fault, io.halted)
```

- `io.reset` 取 `resetReg` 的 bit 0，`io.cg` 取 bit 1——与文档位域定义严丝合缝。
- `statusReg := Cat(io.fault, io.halted)`：`Cat` 把 `fault` 放高位、`halted` 放低位，于是 bit 0 = `HALTED`、bit 1 = `FAULT`，正好匹配 STATUS 表。
- `io.pcStart`：复位后**第一个周期**用 `bootAddr`（硬件复位向量），之后（`bootAddrCapture` 变 false）切到主机写的 `pcStartReg`。这保证即便主机不写 `PC_START`，核也能从一个默认的 `boot_addr` 取指；一旦主机写了 `0x4`，就以主机设定的地址为准。

**④ 这些输出怎么消费**——在内核顶层 [CoreAxi.scala:96-115](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L96-L115)：

```scala
val core_reset = Mux(io.te, ..., (csr.io.reset || dm.io.ndmreset).asAsyncReset)
cg.io.enable := ... || (!csr.io.cg && !core.io.wfi) || ...
csr.io.halted := core.io.halted
csr.io.fault  := core.io.fault
core.io.csr.in.value(0) := csr.io.pcStart
```

可以看到：`csr.io.reset` 直接参与核的复位（`core_reset`）；`csr.io.cg` 经取反进入时钟门控使能（`!csr.io.cg` 表示「允许开钟」）；`csr.io.pcStart` 作为 `value(0)` 注入核的 CSR 输入，也就是真正生效的起始 PC；核的 `halted`/`fault` 又反馈回 `statusReg`，形成闭环。

#### 4.2.4 代码实践

**目标**：读懂 `CoreAxiCSRTest` 的 Write 用例，验证「写 `0x4` 只改 `pcStartReg`，不动 reset/cg」。

**步骤**：

1. 打开 [CoreAxiCSRTest.scala:65-98](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSRTest.scala#L65-L98)。
2. 关注三处断言：
   - 初始：`cg.expect(1)`、`reset.expect(1)`、`pcStart.expect(0)`——对应 `resetReg=3`、`pcStartReg=0`。
   - 写地址 `0x4`、数据 `(0x20000000 << 32)`（即把 `0x20000000` 放在第 1 个 32 位车道 `[63:32]`）。
   - 写完后：`pcStart.expect(0x20000000)`，而 `cg.expect(1)`、`reset.expect(1)` **不变**。

**观察**：写 `PC_START` 不会误触发复位/时钟变化，三个寄存器互相独立。这正是启动序列里「可以先写 PC、再操作复位」的安全保证。

> 待本地验证：若你想亲手跑这个仿真，可用 Bazel 执行该测试目标（具体标签见 `hdl/chisel/src/coralnpu/` 下的 `BUILD` 文件）。受限于环境，本讲不假装已运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `statusReg` 在 `coreRegMap` 里（能被读到），却没有出现在写逻辑里？

> **答案**：`statusReg` 是**只读**的状态镜像，每个周期被 `Cat(io.fault, io.halted)` 覆盖。把它放进读 map 是为了让主机能读 STATUS；它不在写逻辑里，因为主机无权改写核的实时状态。

**练习 2**：若主机向 `0x4` 写入时把数据放在 `writeData(31,0)` 而非 `writeData(63,32)`，会发生什么？

> **答案**：`pcStartReg` 仍取 `writeData(63,32)`（硬件按地址固定取切片），所以 `pcStart` 会拿到 `[63:32]` 那一车道的内容（可能是 0 或旧值），而不是主机本想写的值。即「写不进去」——数据必须放在与地址匹配的车道上。

---

### 4.3 启动序列与 CoreAxiCSRTest 验证

#### 4.3.1 概念说明

知道每个寄存器的作用后，还要知道**按什么顺序**操作它们。顺序错了会出问题：CoralNPU 用的是**同步复位**——复位只在时钟沿生效。如果时钟还被门控着就去解除复位，复位信号根本传不进核里，核会处于未定义状态。

#### 4.3.2 核心流程

[integration_guide.md:166-215](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L166-L215) 给出的标准启动序列：

1. **加载 ITCM**：把程序镜像写进 `0x0000` 起的指令存储（用 AXI 或 DMA）。
2. **写 `PC_START`**（向 `0x30004` 写起始地址；若程序本就链接在 0，可跳过）。
3. **释放时钟门控**：向 `0x30000` 写 `1`（= `0b01`：`RESET=1`、`CLOCK_GATE=0`）。此时核仍在复位，但时钟已运行，让同步复位能正确传播。文档要求**等待一个周期**。
4. **释放复位**：向 `0x30000` 写 `0`（= `0b00`：`RESET=0`、`CLOCK_GATE=0`）。核从 `PC_START` 开始执行。
5. **轮询 `STATUS`**：读 `0x30008`，bit 0 = `HALTED`、bit 1 = `FAULT`。

> **易错点**：第 3 步「释放时钟门控」写的是 `1`，看起来像「要复位」，其实 `1 = 0b01` 表示「复位保持、时钟放开」。因为时钟门控位 `CLOCK_GATE` 在 **bit 1**，清掉它只需把 bit 1 置 0、bit 0 保持 1——即写 `1`。务必结合位域理解，不要把数值 `1` 望文生义地当成「开/关」。

为什么可以「先写 PC（第 2 步）再开钟（第 3 步）」？因为 CSR 接口所在的时钟域**始终在线**，`cg` 门控的只是核**流水线**的时钟。写 `pcStartReg` 是对 CSR 模块寄存器的普通写，与核流水线是否在跑无关。这一点在 4.2 的源码里也能印证：`cg.io.enable` 只控制核内部时钟，不影响 `CoreCSR` 自身。

#### 4.3.3 源码精读

启动序列的每一步都能在源码里找到对应硬件行为：

- **第 2/3/4 步的写**：都走 [CoreAxiCSR.scala:157-159](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L157-L159) 的写逻辑。写 `0x0` 改 `resetReg`，写 `0x4` 改 `pcStartReg`。
- **第 3→4 步的位域语义**：[第 151-152 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L151-L152) `io.reset := resetReg(0)`、`io.cg := resetReg(1)`。写 `1`（`0b01`）→ `reset=1`、`cg=0`（开钟但仍在复位）；写 `0`（`0b00`）→ `reset=0`、`cg=0`（完全放出）。
- **第 5 步的状态**：[第 154 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L154) `statusReg := Cat(io.fault, io.halted)`，主机读 `0x8` 即得到 `halted`（bit0）/`fault`（bit1）。

`CoreAxiCSRTest` 还验证了两类边界，对应 [CoreAxiCSRTest.scala:34-63](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSRTest.scala#L34-L63)（Read）与 [CoreAxiCSRTest.scala:140-187](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSRTest.scala#L140-L187)（WriteInvalid）：

- **Read 用例**：poke `coralnpu_csr.value(0) = 0xCAFEB0BA`，从 `0x100`（内部 CSR 区基址，见 [第 92 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L92) `kCsrBaseAddr = 0x100`）读回 `3405689018`（= `0xCAFEB0BA`），证明读路径与内部 CSR 镜像正常。
- **WriteInvalid 用例**：向 `0x104`（内部 CSR 区，只读）写数据。源码 [第 173-180 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L173-L180) 的 `allWriteRegs` 只包含 `0x0`、`0x4` 和 debug 寄存器，`0x104` 不在其中，于是 `writeResp` 返回错误码 `resp=2`（AXI 的 `SLVERR`，[第 171 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSRTest.scala#L171) 断言），且 `cg/reset/pcStart` 全部不变——写非法地址不会污染寄存器状态。

#### 4.3.4 代码实践（本讲主实践）

**目标**：综合 integration_guide 的 CSR 表与 CoreAxiCSR 源码，写出一段**完整的 C 启动伪代码**，并用源码逐行佐证每个数值。

**操作步骤**：

1. 回顾 4.1 的 CSR 表，确认三个寄存器的全局地址：`RESET_CONTROL=0x30000`、`PC_START=0x30004`、`STATUS=0x30008`。
2. 对照 4.2 的写逻辑，确认：写 `RESET_CONTROL=1` 意为「开钟、保持复位」，写 `0` 意为「解除复位」。
3. 写出下面这段 C 伪代码（基于 [integration_guide.md:169-215](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L169-L215) 的示例整理）：

```c
// 示例代码（伪代码，基于 integration_guide 启动流程整理）
// 假设 CoralNPU 已被映射到某段地址，下列为 CSR 区内偏移
volatile uint32_t* reset_ctrl = (uint32_t*)0x30000L; // RESET_CONTROL, offset 0x0
volatile uint32_t* pc_start   = (uint32_t*)0x30004L; // PC_START,     offset 0x4
volatile uint32_t* status     = (uint32_t*)0x30008L; // STATUS,       offset 0x8

// 1. 加载程序到 ITCM（略，可用 AXI/DMA 拷贝镜像到 0x0000）

// 2. 设起始 PC（若程序链接在 0 可省略）
*pc_start = ENTRY_ADDR;

// 3. 释放时钟门控：写 1 = 0b01 -> RESET=1, CLOCK_GATE=0（开钟，仍在复位）
*reset_ctrl = 1;
// 同步复位需要时钟跑一拍，等待一个周期
__asm__ volatile("nop");  // 或平台相关的 delay

// 4. 释放复位：写 0 = 0b00 -> RESET=0, CLOCK_GATE=0（核开始执行）
*reset_ctrl = 0;

// 5. 轮询 STATUS，等待 halted 或 fault
uint32_t s;
do {
    s = *status;
} while (((s & 1) == 0) && ((s & 2) == 0));  // bit0=halted, bit1=fault

if (s & 2) {
    // 发生 fault
} else {
    // 正常 halted，程序跑完（CRT 执行了 mpause）
}
```

**需要观察的现象 / 预期结果**：

- 第 3 步写 `1` 后，源码里 `resetReg` 被写为 `1` → `io.cg=0`（核时钟开始）、`io.reset=1`（仍在复位）。
- 第 4 步写 `0` 后，`io.reset=0` → 顶层 `core_reset` 解除（见 [CoreAxi.scala:96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L96)），核从 `pcStart` 取指。
- 第 5 步：程序正常结束时 CRT 的 `mpause` 会触发 `core.io.halted`，经 `statusReg` 反映为 `STATUS` 的 bit0 = 1，循环退出。
- 若中途访存/取指出错（见 u3-l2 的 fault 机制），`io.fault` 置位，`STATUS` bit1 = 1。

> 待本地验证：上述伪代码取自 integration_guide 的官方示例并按本讲源码做了对齐；在真实 SoC 上需把 `0x30000` 等 CSR 地址加上 CoralNPU 的 SoC 基址，并保证第 3 步与第 4 步之间确有一个以上的时钟周期。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 3 步和第 4 步合并成「直接写 `0`」（跳过先写 `1`），可能出什么问题？

> **答案**：写 `0` = `0b00`，意味着从「复位 + 时钟门控（resetReg=3）」直接跳到「不复位 + 不门控」。但由于此前时钟还被门控着，同步复位信号在门控打开的瞬间是否已被正确采样，取决于具体时序——这违反了 integration_guide「复位时必须让时钟跑一拍」的要求，可能让核从未完全复位的未知态启动。先写 `1` 就是为了「开钟 + 保持复位」，让复位稳稳传播一拍。

**练习 2**：`CoreAxiCSRTest` 的 `WriteInvalid` 用例里，向 `0x104` 写数据后，`resp` 为什么是 `2`？

> **答案**：`0x104` 落在内部 CSR 只读区，不在 `allWriteRegs`（仅含 `0x0`、`0x4`、debug 寄存器）里。源码的 `writeResp` 对未命中地址返回 false，`AxiSlave` 据此回报 AXI 错误响应 `SLVERR`（编码为 `2`），并保证寄存器不被改动。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「CSR 逆向追踪」小任务：

1. **从地址到寄存器**：给定主机要向 CoralNPU 写一条「释放复位」的命令，写出它该访问的完整 CSR 地址、应写入的数值，并用 [CoreAxiCSR.scala:151-159](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSR.scala#L151-L159) 解释这个数值如何最终变成 `io.reset=0`。
2. **从状态到信号**：主机读到 `STATUS = 2`，画出从核内 `core.io.fault=1` → `statusReg`（`Cat(fault, halted)`）→ AXI 读数据返回的完整数据通路，指出哪一位是 `FAULT`。
3. **补一个边界用例**：参照 [CoreAxiCSRTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxiCSRTest.scala) 的现有用例，**口头设计**一个测试：写 `RESET_CONTROL = 2`（即 `0b10`，`RESET=0`、`CLOCK_GATE=1`），预测 `io.reset`、`io.cg` 各是多少，核能否开始执行？为什么这种配置没有出现在官方启动序列里？

> 提示：`2 = 0b10` 意为「解除复位但门控时钟」——核的复位被放开了，可流水线时钟却停着，核无法取指推进，是一个无意义的中间态。官方序列因此要求先开钟（写 1）再解除复位（写 0），始终避免「不复位且门控」的组合。

## 6. 本讲小结

- CoralNPU 对外暴露三个核心 CSR：`RESET_CONTROL`(0x0)、`PC_START`(0x4)、`STATUS`(0x8)，位于 CSR 区基址 `0x30000` 之上。
- `RESET_CONTROL` 的 bit0=`RESET`、bit1=`CLOCK_GATE`，**复位值为 3**（上电即复位 + 门控时钟）；`STATUS` 的 bit0=`HALTED`、bit1=`FAULT`，只读，由硬件实时镜像。
- 标准启动序列：加载 ITCM → 写 `PC_START` → 写 `RESET_CONTROL=1`（开钟、保持复位，等一拍）→ 写 `RESET_CONTROL=0`（解除复位）→ 轮询 `STATUS`。顺序的核心原因是同步复位要求时钟先跑一拍。
- 源码里 `statusReg := Cat(io.fault, io.halted)`、`io.reset := resetReg(0)`、`io.cg := resetReg(1)` 是三个 CSR 位域语义的「源头」；写逻辑按地址取 128 位总线上固定的 32 位车道，且 `internal` 事务被屏蔽。
- `CoreAxiCSRTest` 验证了正常读/写、只写 `PC_START` 不影响复位、以及写非法（只读）地址返回 `SLVERR`(resp=2) 且不污染寄存器。

## 7. 下一步学习建议

- 本讲的 CSR 是「外部主机控制 CoralNPU 生命周期的入口」。接下来建议学习 **u8 外设与 DMA**——DMA 引擎（[DmaEngine.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala)）正是主机用来「把程序/数据批量搬进 ITCM/DTCM」的高效手段，对应启动序列的第 1 步。
- 若你对「核停了之后如何被调试」感兴趣，可直接进入 **u9-l1 RISC-V Debug 模块**：本讲提到的 debug CSR（`CoreCsrAddrs.DbgReqAddr` 等，offset `0x800+`）就是 Debug 模块经 AXI CSR 暴露给外部调试器的接口，与 `RESET_CONTROL` 共用同一套读写机制。
- 想看完整的「主机驱动 CoralNPU」Python 实现，可复习 **u2-l4 cocotb 测试框架入门** 里 `CoreMiniAxiInterface.execute_from` 的启动序列代码——它就是本讲 C 伪代码的 cocotb 翻版。
