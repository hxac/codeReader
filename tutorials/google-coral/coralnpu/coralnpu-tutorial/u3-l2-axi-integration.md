# AXI 接口与外部系统集成

## 1. 本讲目标

上一讲（u3-l1）我们看到了 CoralNPU 如何用 `CoralNPUChiselSubsystem` 把核、总线、外设装配成一个完整 SoC。本讲把视角收回到「CoralNPU 作为一个 IP 核，如何被外部系统使用」这一集成视角。

CoralNPU 对外是一颗挂在 **AXI4 总线**上的协处理器：它既能**被动**地接受外部主机的访问（被写程序、被读结果），又能**主动**地发起访问（取指/访存落到外部 DDR、ROM 时）。这两种身份分别对应两个 AXI 接口：**slave**（从机）和 **master**（主机）。

学完本讲，你应当能够：

- 说清 CoralNPU 对外暴露的 `s_axi`（slave）与 `m_axi`（master）两个 AXI4 接口的信号语义与固定取值约束。
- 在 `CoreAxi.scala` 顶层中定位这两个接口，并解释内核的指令/数据访问如何被分派到 ITCM/DTCM 或外部 AXI。
- 解释 `IBus2Axi`、`DBus2Axi` 这两个适配器如何把内核内部的「取指请求」「外部访存请求」翻译成标准的 AXI 突发事务。
- 对照 `doc/integration_guide.md` 的信号表，核对 RTL 实现与文档是否一致。

## 2. 前置知识

本讲默认你已经读过 u1-l1（项目定位）、u2-l3（仿真器启动链）和 u3-l1（SoC 顶层装配）。在进入正文前，先回顾几个关键概念。

### 2.1 AXI4 是什么

AXI4（Advanced eXtensible Interface）是 ARM AMBA 总线家族里最常用的一种**点对点、主从式、五通道**协议。一次 AXI 连接里有一个 **manager/master**（主动发起事务的一方）和一个 **subordinate/slave**（被动响应的一方），双方通过五条独立的通道握手：

| 通道 | 方向 | 作用 |
| --- | --- | --- |
| AW（Address Write） | master → slave | 给出一次**写**事务的地址与控制信息 |
| W（Write data） | master → slave | 给出写数据 |
| B（Write response） | slave → master | 回复写事务的完成状态 |
| AR（Address Read） | master → slave | 给出一次**读**事务的地址与控制信息 |
| R（Read data） | slave → master | 返回读数据与状态 |

每条通道都用 `valid`/`ready` 做 **Decoupled 握手**：只有当双方都拉高时（`fire`），数据才算被收下。这一点和 Chisel 里的 `Decoupled` 是一一对应的，CoralNPU 的 AXI bundle 直接用 `Decoupled` 实现。

> 提示：AXI 的读地址（AR）和读数据（R）是分开的两个通道，所以一次读事务在时间上是「先发 AR，过若干拍后从 R 收数据」。这正是 `IBus2Axi` 状态机要处理的核心时序问题。

### 2.2 突发事务（burst）的关键字段

一次 AXI 事务往往搬运**一串连续字节**，称为一个 burst。描述一个 burst 的关键字段是：

- `addr`：起始地址。
- `len`：burst 中的 **beat（数据拍）数减一**，即 \( \text{beat 数} = \text{len} + 1 \)。
- `size`：每个 beat 搬运的字节数的以 2 为底对数，即每拍 \( 2^{\text{size}} \) 字节。
- `burst`：地址递增方式。`FIXED=0`（地址不变）、`INCR=1`（地址递增）、`WRAP=2`（回绕）。
- `id`：事务标识，slave 必须把同一个 `id` 反映到响应里，master 才能把响应和请求对上号。

这些字段的取值约束，正是本讲实践任务要核对的重点。

### 2.3 CoralNPU 内部的三条总线

上一讲和后续 u6 讲会反复出现内核的三条访存通道，这里先点明它们在本讲中的角色：

- **ibus**（instruction bus）：取指总线。地址落在 ITCM 时走快速 SRAM；落在 ITCM 之外（如外部 ROM/DDR）时，由 `IBus2Axi` 转成 AXI 读事务从 `m_axi` 取回。
- **dbus**（data bus）：LSU 的数据总线，主要接 DTCM（单周期 SRAM）。
- **ebus**（external bus）：LSU 的「外部」数据总线。当地址落在 DTCM/外设区之外、需要访问片外存储时，LSU 把请求送到 `io.ebus.dbus`，再由 `DBus2Axi` 转成 AXI 事务从 `m_axi` 收发。

理解「ibus/ebus 在 miss 掉本地 TCM 时都要借道 `m_axi`」，是读懂本讲第 4.3 节的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/integration_guide.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md) | 面向集成者的说明文档：列出对外信号、AXI master/slave 的字段取值约束、内存映射与启动流程。 |
| [hdl/chisel/src/coralnpu/CoreAxi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala) | **AXI 顶层包装**。把内核 `Core`、ITCM/DTCM、`AxiSlave`、`IBus2Axi`、`DBus2Axi`、CSR、Debug 模块装配在一起，对外暴露 `axi_slave` / `axi_master`。 |
| [hdl/chisel/src/coralnpu/AxiSlave.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala) | AXI **从机**实现：把外部主机的读写事务翻译成对内部 fabric（ITCM/DTCM/CSR）的访问。 |
| [hdl/chisel/src/coralnpu/IBus2Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/IBus2Axi.scala) | 取指请求 → AXI **读**事务的适配器（只读）。 |
| [hdl/chisel/src/coralnpu/DBus2Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/DBus2Axi.scala) | LSU 外部数据请求 → AXI **读/写**事务的适配器。 |
| [hdl/chisel/src/bus/Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala) | AXI 协议的 Chisel bundle 定义（地址/数据/响应、`defaults()`、枚举）。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看对外接口与顶层装配（4.1），再看被动响应外部访问的从机端口（4.2），最后看主动发起访问的两个适配器（4.3）。

### 4.1 AXI 接口语义与 CoreAxi 顶层装配

#### 4.1.1 概念说明

把 CoralNPU 当作一个 IP 黑盒，它对外其实只有两类与总线打交道的端口：

- **`s_axi`（slave）**：一个 AXI4 **从机**接口。外部主机（比如 SoC 里的应用 CPU）通过它向 ITCM/DTCM 写入程序与数据、读写控制 CoralNPU 的 CSR。也就是说，「外部 → CoralNPU」走这里。
- **`m_axi`（master）**：一个 AXI4 **主机**接口。CoralNPU 自己在运行时，如果取指或访存的地址落在本地的 ITCM/DTCM 之外，就要通过这个端口去访问片外存储或外设。也就是说，「CoralNPU → 外部」走这里。

除此之外还有时钟复位（`clk`/`reset`）、中断（`irqn`）、状态（`wfi`/`halted`/`fault`）、调试（`debug`）等非总线信号。`doc/integration_guide.md` 把它们整理成了一张总表。

#### 4.1.2 核心流程

`CoreAxi` 这个 Chisel 模块就是上面这个黑盒的 RTL 实现，它的内部数据流可以这样概括：

```
                 外部主机                                   外部存储/外设
                     │ s_axi(slave)                              ▲ m_axi(master)
                     ▼                                           │
                ┌─────────┐   fabric    ┌─────────┐              │
                │ AxiSlave│────────────▶│ ITCM/DTCM│              │
                └─────────┘   (内部)    │  /CSR    │              │
                     │                  └──────────┘              │
                     │                       ▲                    │
                     │                       │                    │
   内核 Core ◀──── ibus ─── ITCM(miss) ── IBus2Axi ────读地址仲裁──┤
   (取指/访存)                                        │            │
   内核 Core ◀──── ebus ──── DTCM(miss) ── DBus2Axi ──┘            │
   (LSU 外部)                                       写通道直接────┘
```

要点：

1. **外部写程序**：主机经 `s_axi` 把 ELF 内容写进 ITCM/DTCM，`AxiSlave` 把写事务翻译成对内部 fabric 的写。
2. **内核取指**：内核给出 `ibus.addr`。若地址在 ITCM 内，直接读本地 SRAM（快）；否则由 `IBus2Axi` 转成 AXI 读事务，经 `m_axi` 取回。
3. **内核外部访存**：LSU 把 miss DTCM 的请求送到 `ebus`，由 `DBus2Axi` 转成 AXI 读/写事务，经 `m_axi` 收发。
4. **读返回数据按 id 分流**：因为 `m_axi` 的读通道被 ibus 与 ebus **共用**，CoralNPU 给两类请求打上不同的 `id`，再用 `id` 把返回的读数据路由回正确的请求方。

#### 4.1.3 源码精读

先看 `CoreAxi` 对外声明的两个 AXI 端口。注意 slave 端口用的是 `Flipped(new AxiMasterIO(...))`——`AxiMasterIO` 是「主机视角」的 bundle，对一个从机来说需要翻转方向：

[CoreAxi.scala:31-32](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L31-L32) 声明 `axi_slave`（翻转的主机 IO，即从机）与 `axi_master`（主机）。端口的位宽来自参数 `p.axi2AddrBits / p.axi2DataBits / p.axi2IdBits`，这三个参数在 [Parameters.scala:164-166](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L164-L166) 中定义：地址 32 位、id 6 位、数据位宽等于 `lsuDataBits`（与向量位宽一致）。

这段声明与 `doc/integration_guide.md` 的总表完全对应：

[integration_guide.md:27-28](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L27-L28) 文档对 `s_axi` 与 `m_axi` 的一句话描述：slave 用于写 TCM 或触碰 CSR，master 用于读写外部存储/CSR。

接下来看顶层如何决定一次取指走 ITCM 还是走 AXI。`memoryRegions(0)` 即 ITCM 区段，`inItcm` 是一个组合判断：

[CoreAxi.scala:163-186](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L163-L186) 这段是取指的「多路选择」：

- `inItcm := memoryRegions(0).contains(core.io.ibus.addr)` 判断地址是否落在 ITCM。
- 命中 ITCM 时，请求送进 ITCM 仲裁器（快速 SRAM 路径）。
- 未命中时，请求送给 `IBus2Axi`，由它转成 AXI 读：

[CoreAxi.scala:176-178](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L176-L178) 这里实例化 `IBus2Axi(p, id = 1)`——注意 **id=1**，代表「指令取指」。`ibus.valid && !inItcm` 才驱动它。

再看数据侧（ebus）的适配器实例化：

[CoreAxi.scala:239-241](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L239-L241) `ebus2axi = DBus2Axi(p, id = 0)`——**id=0**，代表「数据访存」。它接 `core.io.ebus.dbus`，负责把 LSU 的外部请求转成 AXI。

`m_axi` 的写通道只有 ebus 用，所以直接相连；读通道则要仲裁 ibus 与 ebus 两路读地址，并按 id 分流返回数据：

[CoreAxi.scala:243-260](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L243-L260) 这段是 master 端口的仲裁与分流核心：

- 写通道：`io.axi_master.write <> ebus2axi.io.axi.write`（只有数据路径会写外部存储）。
- 读地址：用 `CoralNPURRArbiter` 在 `ebus2axi`（id=0）与 `ibus2axi`（id=1）之间做轮询仲裁，输出到 `io.axi_master.read.addr`。
- 读数据：返回的读数据先进一个深度 2 的 skid buffer（`Queue(..., 2)`），再用 `readDataSkid.bits.id` 把它分发：`id===0` 送回 ebus，`id===1` 送回 ibus。

> ⚠️ **文档与代码的一处出入（重要观察）**：`doc/integration_guide.md` 在描述 master 信号时写「`id` Always 0」「R/B 通道 id should be 0 … CoralNPU only emits txns with an id of 0」。但上面的 RTL 实现显示，**读地址通道实际上会发出 id=0（数据）与 id=1（指令）两种**，并且正是靠外部 slave 把 id 原样反映到 R 通道，CoralNPU 才能把读数据路由回正确的请求方（见 [CoreAxi.scala:254-257](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L254-L257)）。也就是说：写通道确实只用 id=0（与文档一致），而读通道用 id 区分指令/数据——文档的「恒为 0」是对集成者的简化概括。本讲的实践任务会让你亲自核对这个差异。

最后，`AxiSlave` 的实例化与外部 slave 端口的对接在：

[CoreAxi.scala:226-236](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L226-L236) 创建 `AxiSlave`，把内部 fabric 接给它，再用 `GateDecoupled` 把 `io.axi_slave` 的五条通道与 slave 连起来。`axiSlaveEnable` 是一个上电即拉高的寄存器，配合 `GateDecoupled` 起到「复位后稳定一拍再放行」的作用。

#### 4.1.4 代码实践

**实践目标**：核对 RTL 顶层与集成指南的接口表是否一致，并亲手标出 master 端口的字段取值。

**操作步骤**：

1. 打开 [doc/integration_guide.md:35-76](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L35-L76)，把 AXI **master** 信号表抄下来（`prot/id/len/size/burst/lock/cache/qos/region`）。
2. 打开 [hdl/chisel/src/bus/Axi.scala:48-57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala#L48-L57)，读 `AxiAddress.defaults()` 方法，确认它给这些字段设的默认值（`burst=1/INCR`、`cache=0`、`lock=0`、`qos=0`、`region=0`、`size=log2(数据字节宽)`）。
3. 打开 `IBus2Axi.scala` 和 `DBus2Axi.scala`，确认它们在发地址时都调用了 `defaults()`，并且把 `prot` 单独覆盖成 `2`、`id` 覆盖成传入的 id。
4. 列一张对照表，左列是文档取值，右列是 RTL 实际取值。

**需要观察的现象**：除 `id` 在读通道上为 0/1 之外，其余字段的 RTL 默认值应与文档完全一致（`prot=2`、`burst=INCR=1`、`cache=0`、`lock=0`、`qos=0`、`region=0`）。

**预期结果**：你会得到一张表，清楚地标出 master 读写两路在 `id` 上的差异，其余字段与文档一致。这正是回答本讲实践任务的直接产物。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_slave` 要写成 `Flipped(new AxiMasterIO(...))` 而不是直接 `new AxiMasterIO(...)`？

**参考答案**：`AxiMasterIO` 是按「主机视角」定义的（例如 `addr` 是输出、`resp` 是输入）。CoralNPU 在 `s_axi` 上扮演的是**从机**，所有通道方向都要反过来，所以用 `Flipped` 翻转。这样在 `CoreAxi` 内部连接时，方向才能和 `AxiSlave`（也是从机）对齐。

**练习 2**：`CoreAxi` 里 `readAddrArb` 仲裁的是哪两路请求？为什么写通道不需要仲裁？

**参考答案**：仲裁的是 `ebus2axi` 的读地址（id=0，数据）和 `ibus2axi` 的读地址（id=1，取指）。写通道不需要仲裁，是因为**只有数据路径（ebus）会发起写事务**——取指永远不会写存储，所以 `io.axi_master.write` 可以直接连到 `ebus2axi.io.axi.write`。

---

### 4.2 AxiSlave：外部主机访问 CoralNPU 的从机端口

#### 4.2.1 概念说明

`s_axi` 是外部世界「操控」CoralNPU 的唯一标准入口：主机通过它把程序写进 ITCM、把数据写进 DTCM、读写 CSR 来启动/停止内核、读回结果。`AxiSlave` 模块的任务，就是把外部发来的标准 AXI 事务，翻译成对**内部 fabric**（一组通往 ITCM/DTCM/CSR 的统一读写接口）的访问。

它要处理三件典型的 AXI 复杂度：

1. **读/写通道并发**：AW 与 AR 是两条独立通道，slave 内部要仲裁。
2. **突发多拍**：一个事务可能含多拍数据，地址要按 `burst`/`size` 递增（INCR）或回绕（WRAP）或不变（FIXED）。
3. **错误回报**：当目标外设忙（`periBusy`）或不可达时，要在 B/R 通道回 `SLVERR`。

#### 4.2.2 核心流程

`AxiSlave` 的内部流水可以画成：

```
io.axi.read.addr  ─┐
                   ├─ RR仲裁 ─▶ axiAddr(read/write 标记) ─▶ cmdAddr(按beat递增)
io.axi.write.addr ─┘                                          │
                                                              │
   写路径：write.data ─▶ ──┬──▶ fabric.writeData ─▶ ITCM/DTCM/CSR
                          └──▶ writeResponse(B 通道, OKAY/SLVERR)
   读路径：cmdAddr ─▶ fabric.readDataAddr ─▶ fabric.readData ─▶ R 通道
```

具体步骤：

1. AR 与 AW 地址各进一个深度 2 的 `Queue`，再经 `CoralNPURRArbiter` 轮询仲裁，得到统一的 `axiAddr`（带 `write` 标记）。
2. `axiAddr` 进一级 `Queue(pipe=true)` 形成 `axiAddrCmd`，记下当前事务的 `cmdAddr`，并在每拍按 burst 模式更新地址。
3. **写**：写数据进深度 3 的队列，当「写地址有效 && 数据有效 && 响应就绪」时把数据发到 fabric，最后一拍（`last`）产生 B 响应；fabric 返回 `writeResp` 决定是 `OKAY` 还是 `SLVERR`。
4. **读**：在读数据队列有空位（至少 2）且外设不忙时，把 `cmdAddr` 发给 fabric，一拍后把 fabric 返回的数据配上 `id/last/resp` 推进读数据队列，最终从 R 通道送出。
5. 事务结束（写收到 B、读发完最后一拍）后，`axiAddrCmd.ready` 拉高，迎接下一个事务。

#### 4.2.3 源码精读

先看 `AxiSlave` 的端口：它翻转了一个 `AxiMasterIO`（即对外是从机），并暴露一个面向内部 fabric 的 `FabricIO`，外加 `periBusy` 与 `txnInProgress`：

[AxiSlave.scala:42-50](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L42-L50) 这是 `AxiSlave` 的 IO 定义。

读/写地址的仲裁与「写标记」的产生：

[AxiSlave.scala:52-63](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L52-L63) 两路地址先进队列再 RR 仲裁，`axiAddr.bits.write := (addrArbiter.io.chosen === 1.U)`——选中的是 1 号（写地址）通道就标记为写。

写路径把数据发往 fabric 并决定响应码：

[AxiSlave.scala:76-91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L76-L91) `maybeWriteData` 同时要求「写激活 && 数据有效 && 响应就绪」；fabric 的 `writeResp` 决定回 `OKAY` 还是 `SLVERR`，与 [integration_guide.md:117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L117) 文档承诺的 slave B 通道响应码（0/OKAY 或 2/SLVERR）一致。

读路径在「队列有空位且不忙」时发地址，一拍后回数据：

[AxiSlave.scala:104-127](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L104-L127) 注意 `maybeIssueRead` 用 `>=2` 的队列余量做预判，而真正发到 fabric 用 `issueRead = maybeIssueRead && !io.periBusy`；`readData.resp` 同样按 fabric 是否返回有效数据在 `OKAY/SLVERR` 间选择。

最后是按 burst 模式递增地址的状态机，这是 AXI slave 最容易出 bug 的地方：

[AxiSlave.scala:132-157](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L132-L157) `addrNext` 用 `MuxUpTo1H` 在三种 burst 间选一：

- `FIXED`：地址不变（`cmdAddr`）。
- `INCR`：地址加 \( 2^{\text{size}} \)，即 `cmdAddr + (1.U << size)`。
- `WRAP`：地址加一个 size 步长，越过 `cmdAddrBase + 数据字节宽` 就回绕到 `cmdAddrBase`。

而 `cmdAddr` 的更新条件见 [AxiSlave.scala:151-157](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L151-L157)：新命令来到时载入新地址，否则在每拍有效写/读且外设不忙时按 `addrNext` 递增。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「外部主机写 DTCM 一个 4 字节字」在 `AxiSlave` 内部的字段演化。

**操作步骤**：

1. 假设主机发起一次 AW+len=0+size=2（4 字节）、burst=INCR 的单拍写事务，地址为 `0x00010004`（DTCM 内，参见 [integration_guide.md:160-164](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L160-L164) 的内存映射）。
2. 在 [AxiSlave.scala:52-63](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L52-L63) 标出：AW 走 1 号通道进仲裁，`write=True`，`axiAddrCmd.bits.addr.addr=0x00010004`。
3. 在 [AxiSlave.scala:76-91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala#L76-L91) 标出：W 通道数据到来后，`fabric.writeDataAddr=cmdAddr=0x00010004`、`writeDataStrb` 来自 W.strb；fabric 完成后 B 通道回 `OKAY`，`id` 反映 AW 带来的 id。
4. 因为 `len=0`（单拍），`writeData.bits.last=True`，写响应立即发出，事务结束。

**需要观察的现象**：单拍写事务不触发地址递增（`addrNext` 计算了但事务已结束），`cmdAddr` 在下一命令到来前保持不变。

**预期结果**：你能用一张时序草图描述「AW 拍 → W 拍 → B 拍」三步在 `AxiSlave` 内部对应的信号变化，并指出 `fabric` 是最终把数据送进 DTCM 的那一环。

> 待本地验证：如需观察真实波形，可参照 u2-l4 的 cocotb 测试台，用 `axi_slave` 写一次 DTCM 并 dump VCD，但本实践以源码阅读为主。

#### 4.2.5 小练习与答案

**练习 1**：`periBusy` 信号高时，`AxiSlave` 会怎样表现？

**参考答案**：`periBusy` 表示目标外设正忙、不能接受新访问。此时 `issueRead` 与写数据的 `ready` 都会被压住（写：`writeData.ready := maybeWriteData && !io.periBusy`；读：`issueRead := maybeIssueRead && !io.periBusy`），即 slave **暂不推进** fabric 访问，但地址/数据仍留在各自的队列里，等 `periBusy` 拉低后继续，从而实现背压（backpressure）而不丢事务。

**练习 2**：为什么读路径要预先判断「队列余量 ≥ 2」才发地址？

**参考答案**：fabric 的读数据有一拍延迟（地址当拍发出、下一拍数据回来），读数据要进 `readDataQueue` 再送到 R 通道。预留至少 2 个空位可以保证 fabric 返回的数据总有地方落，避免因队列满而丢失返回数据、破坏 AXI 协议。

---

### 4.3 IBus2Axi / DBus2Axi：内部总线到 AXI 主机的适配

#### 4.3.1 概念说明

`AxiSlave` 解决了「外部 → CoralNPU」，而 `IBus2Axi` 与 `DBus2Axi` 解决反向的「CoralNPU → 外部」。内核内部用的是一套**自定义的简单总线**（`IBusIO`/`DBusIO`，非 AXI），它们没有 AXI 那种五通道、突发、id 的概念，只是「给一个地址、要一拍数据」。要把这种简单请求送到标准 AXI 总线上，就需要两个适配器：

- **`IBus2Axi`**：把取指请求（`IBusIO`，**只读**）翻译成 AXI **读**事务。
- **`DBus2Axi`**：把 LSU 的外部数据请求（`DBusIO`，可读可写）翻译成 AXI **读+写**事务。

它们是 master 侧的「协议转换器」。

#### 4.3.2 核心流程

**IBus2Axi（只读）**：取指的一个特点是「对同一地址可能连续请求多次」（流水线重复取同一条指令线）。为减少重复的 AXI 事务，`IBus2Axi` 做了一个**行对齐 + 缓存一拍**的小状态机：

```
ibus.addr ─▶ 行对齐 saddr ─▶ 若与上次相同且有缓冲 ── 直接返回 sdata
                              否则发 AR(addr=saddr, id, prot=2)
                              ◀──────────── R(data, resp)
ibus.rdata ◀── sdata      ibus.fault ◀── resp != OKAY
```

要点：地址先按「行」对齐（清零低位），命中缓冲就复用上次取回的数据，不命中就发一次新的 AR；返回的 `resp` 非零时产生取指 fault。

**DBus2Axi（读+写）**：数据访存没有取指那种「重复同行」的特点，但需要支持写。它的写路径是标准的「AW → W → B」三阶段，读路径是「AR → R」两阶段，用一组 `xFired` 寄存器记录每个子通道是否已完成，全部完成才算事务结束：

```
写：dbus.write=1 ─▶ AW(addr, size=Ctz(dbus.size), prot=2, id)
                   W(data, strb, last=True)
                   ◀ B(resp)            ── 三者都 fire 才 writeFinished
读：dbus.write=0 ─▶ AR(addr, size=Ctz(dbus.size), prot=2, id)
                   ◀ R(data, resp)      ── 都 fire 才 readFinished
dbus.rdata ◀── readNext(打一拍)    fault ◀── resp != OKAY
```

其中 `size` 字段由 LSU 给出的字节使能掩码经 `Ctz`（count trailing zeros，计算末尾零个数）得到：因为 `dbus.size` 是 one-hot（代码里有 `PopCount(...) === 1` 断言），`Ctz` 正好给出 \( \log_2(\text{字节数}) \)，也就是 AXI 的 `size`。

#### 4.3.3 源码精读

先看 `IBus2Axi`。它的 AXI 侧只用了读通道（`AxiMasterReadIO`）——印证了「取指只读」：

[IBus2Axi.scala:28-33](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/IBus2Axi.scala#L28-L33) IO 定义：左侧 `ibus` 接内核取指，右侧 `axi` 是只读的 AXI 主机接口。

地址做行对齐，是 `IBus2Axi` 的关键一行：

[IBus2Axi.scala:35-43](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/IBus2Axi.scala#L35-L43) `linebit = log2Ceil(p.lsuDataBits/8)`，`saddr` 把地址的低位（`linebit` 以下）清零，保证取的是整「行」。`addrMatch` 判断当前地址是否与上次取的一致。

发 AR 通道时设置的关键字段：

[IBus2Axi.scala:54-57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/IBus2Axi.scala#L54-L57) `addr=saddr`、`id=id.U`（取指实例化为 1）、`prot=2`。其余字段（`burst/cache/...`）由 [Axi.scala:48-57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala#L48-L57) 的 `defaults()` 设为 `INCR/cache=0` 等，与集成指南一致。

读数据 ready 恒高，并把非 OKAY 响应报成 fault：

[IBus2Axi.scala:69-75](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/IBus2Axi.scala#L69-L75) `io.axi.data.ready := true.B`；当 `resp =/= 0` 时 `io.ibus.fault.valid` 拉高，`epc` 记录触发取指异常的指令地址。

再看 `DBus2Axi`。它的 apply 工厂实际返回 V2 版本：

[DBus2Axi.scala:26-30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/DBus2Axi.scala#L26-L30) `DBus2Axi.apply` 返回 `new DBus2AxiV2(p, id)`，`CoreAxi` 里用的就是这个 V2。

写地址与写数据通道，注意 `size` 由 `Ctz` 得到、`last` 恒真：

[DBus2Axi.scala:57-71](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/DBus2Axi.scala#L57-L71) 写地址 `prot=2`、`id=id.U`、`size=Ctz(io.dbus.size)`；写数据进深度 2 的队列，`last := true.B`（CoralNPU 的 ebus 访存是单拍 burst，即 `len=0`）。

读路径与「打一拍再返回」以匹配 dbus 时序：

[DBus2Axi.scala:94-123](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/DBus2Axi.scala#L94-L123) 读地址同样 `prot=2/id/size=Ctz`；`readNext` 是一个延迟寄存器，注释明确说「Insert delay register to match dbus interface expectations」——因为 AXI 的 R 数据相对 AR 有可变延迟，而 dbus 期望稳定的数据时序，所以统一打一拍。

dbus 的 ready 与 fault 汇总：

[DBus2Axi.scala:127-141](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/DBus2Axi.scala#L127-L141) `dbus.ready` 按读/写分别等于 `readFinished/writeFinished`；`fault` 在 B/R 通道 `resp` 非 OKAY 时有效，并区分 `write`、记下 `addr/epc`。

> 小结：两个适配器都把 `prot` 固定为 2（unprivileged/insecure/data）、`burst` 用默认 INCR、`cache=0`，这与 [integration_guide.md:42-49](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L42-L49) 文档对 master 的描述一致；只有 `id` 在读路径上区分 0/1，如 4.1.3 所述。

#### 4.3.4 代码实践

**实践目标**：解释「`IBus2Axi` 与 `DBus2Axi` 分别把哪类内核请求转成 AXI 事务」，并列出 master 端字段的固定取值。

**操作步骤**：

1. 在 [CoreAxi.scala:176](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L176) 确认 `IBus2Axi` 接的是 `core.io.ibus`，且只在 `!inItcm`（取指未命中 ITCM）时驱动——所以它转换的是**取指请求**，且只产生 AXI **读**事务（接口类型 `AxiMasterReadIO`）。
2. 在 [CoreAxi.scala:239](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L239) 确认 `DBus2Axi` 接的是 `core.io.ebus.dbus`（LSU 的外部数据总线）——所以它转换的是**数据访存请求**，可产生读或写事务。
3. 汇总 master 端固定取值，填下表（答案直接来自 `Axi.defaults()` 与两个适配器）：

   | 字段 | master 端取值 | 来源 |
   | --- | --- | --- |
   | `prot` | 恒为 2 | `IBus2Axi.scala:57`、`DBus2Axi.scala:62/99` |
   | `burst` | 恒为 1（INCR） | `Axi.scala:52`（defaults） |
   | `cache` | 恒为 0 | `Axi.scala:54`（defaults） |
   | `lock/qos/region` | 恒为 0 | `Axi.scala:53/55/56`（defaults） |
   | `size` | 读：默认数据宽；ebus 写/读：`Ctz(dbus.size)` | `Axi.scala:51`、`DBus2Axi.scala:61/98` |
   | `len` | 恒为 0（单拍） | `Axi.scala:50`（defaults），ebus 写 `last=True` |
   | `id` | **写**：0；**读**：0（ebus 数据）或 1（ibus 取指） | `CoreAxi.scala:239/176` + 读数据分流 `254-257` |

**需要观察的现象**：`IBus2Axi` 只连接 AR/R 两个通道（只读），`DBus2Axi` 连接全部五通道中的 AW/W/B/AR/R（读+写）。两者的 `prot`、`burst`、`cache` 等取值完全相同，差异只在 `id` 与是否带写。

**预期结果**：你得到一张完整的「字段-取值-来源」表，能清楚回答「IBus2Axi 转换取指请求、只读；DBus2Axi 转换数据请求、可读可写；master 的 prot=2、burst=INCR、cache=0、id 在读通道上为 0/1」。

> 待本地验证：若想确认 `len=0`，可在仿真里用 `--instr_trace` 配合抓取 `m_axi` 的 AR/AW 通道，但本实践以静态阅读为主。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `IBus2Axi` 的 AXI 侧是 `AxiMasterReadIO` 而 `DBus2Axi` 是完整的 `AxiMasterIO`？

**参考答案**：取指永远是**读**操作（CPU 不会写指令存储），所以 `IBus2Axi` 只需要 AR/R 两个通道，用 `AxiMasterReadIO` 即可，省去写逻辑。而 LSU 的数据访问既可能 load（读）也可能 store（写），所以 `DBus2Axi` 需要完整的 `AxiMasterIO`（AW/W/B + AR/R）。

**练习 2**：`DBus2Axi` 里 `size := Ctz(io.dbus.size)`，如果 LSU 想写一个 4 字节字，`dbus.size` 与最终 AXI `size` 分别是什么？

**参考答案**：`dbus.size` 是 one-hot 的字节使能掩码，4 字节 = 第 2 位为 1（`PopCount` 为 1，满足断言），即二进制 `100`。`Ctz` 求末尾零个数为 2，所以 AXI `size = 2`，表示每拍 \( 2^2 = 4 \) 字节，与文档 [integration_guide.md:45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L45) 对 `size`（Bytes-per-beat）的定义一致。

**练习 3**：`IBus2Axi` 为什么要对地址做行对齐（`saddr`）？

**参考答案**：取指按「行」组织，一条指令线宽为 `lsuDataBits/8` 字节。对齐到行边界可以保证一次 AXI 读取回完整的一行指令，并且配合 `addrMatch` 的「同址复用」缓冲，避免流水线对同一行重复取指时反复发起 AXI 事务，降低对外部总线的压力。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**外部主机视角**」的端到端追踪。

**任务**：假设你是 SoC 里的应用 CPU，要把一段程序写进 CoralNPU 的 ITCM 并启动它。请结合本讲源码，画出整条数据通路并标注每一步用到的模块/信号。

**建议步骤**：

1. **写程序进 ITCM**：你向 `s_axi` 发起一组写事务（目标地址 `0x0~` 区段）。追踪它进入 [AxiSlave.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/AxiSlave.scala)，经地址仲裁 → `fabric` → ITCM（参考 [CoreAxi.scala:226-236](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L226-L236)）。
2. **写 CSR 启动内核**：你向 `s_axi` 写 CSR 地址 `0x30004`（PC_START）与 `0x30000`（RESET_CONTROL），同样经 `AxiSlave` → `fabric` → CSR（参见 [integration_guide.md:182-206](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L182-L206)）。
3. **内核开始取指**：内核从 PC 取指，若地址在 ITCM 内走快速路径；**若你的程序链接到了 ITCM 之外**，则取指经 [IBus2Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/IBus2Axi.scala) 转成 AXI 读、从 `m_axi` 取回（参考 [CoreAxi.scala:176-186](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L176-L186)）。
4. **内核访问外部数据**：若程序 load/store 的地址在 DTCM 之外，LSU 走 `ebus`，经 [DBus2Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/DBus2Axi.scala) 从 `m_axi` 收发（参考 [CoreAxi.scala:239-260](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L239-L260)）。
5. **读回结果**：你再次经 `s_axi` 读 DTCM（`0x10000~`）取回计算结果。

**交付物**：一张包含 `外部主机 ↔ s_axi ↔ AxiSlave ↔ fabric ↔ {ITCM/DTCM/CSR}` 与 `{ibus/ebus} ↔ {IBus2Axi/DBus2Axi} ↔ m_axi ↔ 外部存储` 两个方向通路的完整框图，并在每条边上标注关键信号（`prot/id/burst/size/len` 等）。

> 待本地验证：完整的动态验证可结合 u2-l4 的 cocotb 测试台（`CoreMiniAxiInterface` 同时扮演 `s_axi` 的主机与 `m_axi` 的从机），在本讲我们以源码追踪与画图为主。

## 6. 本讲小结

- CoralNPU 对外暴露两个 AXI4 接口：`s_axi`（slave，外部 → CoralNPU，写程序/数据/CSR）和 `m_axi`（master，CoralNPU → 外部，取指/访存 miss TCM 时用）。
- `CoreAxi.scala` 是 AXI 顶层包装：取指在 ITCM 与 `IBus2Axi` 间二选一；数据走 DTCM（`dbus`）或 `DBus2Axi`（`ebus`）；master 读通道用 `id=0`（数据）/`id=1`（取指）做仲裁与读数据分流。
- `AxiSlave` 把外部 AXI 事务翻译成对内部 fabric 的访问，处理读/写并发仲裁、突发地址递增（FIXED/INCR/WRAP）与 `periBusy` 背压，错误回报 `OKAY/SLVERR`。
- `IBus2Axi` 只读（行对齐 + 同址复用），把取指转成 AXI 读；`DBus2Axi` 读+写，用 `Ctz(dbus.size)` 算 `size`，单拍 burst（`len=0`、`last=True`）。
- master 侧字段基本恒定：`prot=2`、`burst=INCR=1`、`cache=0`、`lock/qos/region=0`；写通道 `id=0`，读通道 `id` 区分 0/1——文档「id 恒为 0」是对集成者的简化概括，RTL 读路径实际用了 0/1 两个 id。
- 非零 AXI 响应会被两个适配器报告为内核 fault（取指 fault / 访存 fault），分别带 `epc` 等现场信息。

## 7. 下一步学习建议

- **横向看总线**：本讲的 AXI 是「对外」协议，而 SoC **内部**用的是 TileLink-UL。下一讲 **u3-l3（TileLink-UL 与 AXI 桥接）** 会讲 `Axi2TLUL` / `TLUL2Axi`，把本讲的 AXI 与内部 TL-UL 串联起来，建议紧接着读。
- **纵向看存储**：本讲多次提到「ITCM/DTCM 走快速路径」，其内部结构在 **u6-l1（LSU）** 与 **u6-l2（TCM/SRAM）** 中展开；想理解 `dbus/ebus` 在 LSU 里如何分派，就读 u6-l1。
- **回到启动链**：本讲的 CSR 启动序列（写 PC → 释放时钟 → 释放复位）在 **u3-l5（CSR 接口、内存映射与启动控制）** 里有更完整的寄存器级讲解，可与 u2-l3 的仿真器视角对照。
- **协议细节**：若想深究 AXI bundle 的字段定义与 `defaults()`，可直接通读 [hdl/chisel/src/bus/Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala)，它是本讲所有 AXI 类型的源头。
