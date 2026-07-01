# 外设接口抽象与 GPIO

## 1. 本讲目标

本讲聚焦「如何给 CoralNPU SoC 挂一个新外设」。学完后你应该能够：

- 说清「内存映射寄存器（MMIO）」外设的工作模型——CPU/主机把外设当成一段地址，读写地址就是读写外设里的寄存器。
- 读懂 `peripheral` 包里 `ConnectAxiRead` / `ConnectAxiWrite` 这对工具：它们用一张「寄存器映射表」自动接好 AXI 协议，让你只写寄存器逻辑、不写握手。
- 读懂 `bus.GPIO`：一个真正接进 SoC 的 **TL-UL 从机**，用「手写一拍状态机」实现同样的 MMIO 语义。
- 把两种风格做对比，并按清单把一个新外设（寄存器映射 → 端口声明 → 接入 socket → 分配地址）挂进 SoC。

## 2. 前置知识

本讲依赖第 3 单元（u3-l2 AXI、u3-l3 TileLink-UL、u3-l4 Crossbar/Socket）。回顾要点：

- **AXI** 与 **TL-UL** 都是 CoralNPU 内部使用的内存映射总线协议，都用 `Decoupled`（`valid`/`ready`/`bits`）握手。
  - AXI 把读/写拆成多组通道（读地址、读数据、写地址、写数据、写响应）。
  - TL-UL 只有两个通道：A（请求 `Get` 读 / `PutFullData` 写）与 D（响应 `AccessAckData` 带数据 / `AccessAck` 不带数据）。
- **主机（host/manager）发起事务，从机（device/subordinate）响应事务**。一个外设通常是一个总线**从机**：别人来读写它。
- **MMIO（Memory-Mapped I/O）**：外设内部有一组寄存器，每 个寄存器占一个地址偏移；主机往某地址写 = 配置外设，从某地址读 = 查询外设状态。
- **Socket1N / SocketM1**：SoC 用交叉开关把多个主机路由到多个从机（见 u3-l4）。新外设要挂进去，本质是「在交叉开关里给它分配一段地址，并把它的 TL-UL 端口登记成一个从机」。

> 一个贯穿全讲的关键对比：CoralNPU 的 `peripheral` 包用 **AXI** 做示范（`ConnectAxiRead/Write`），而真正进 SoC 的外设（GPIO 等）用 **TL-UL**。两种协议握手细节不同，但「寄存器映射 + 响应状态机」的思路完全一致。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `hdl/chisel/src/peripherals/PeripheralInterface.scala` | 定义 `ConnectAxiRead` / `ConnectAxiWrite` 两个工具对象：把「名字→地址/值」的寄存器映射表自动接成 AXI 从机的读/写响应。 |
| `hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala` | 定义 `CounterAxiPeripheral`（一个带 count/limit/enable 寄存器的 AXI 外设范例）和它的仿真测试 `PeripheralInterfaceSpec`。 |
| `hdl/chisel/src/bus/GPIO.scala` | 生产用 TL-UL 从机 `GPIO`：DATA_IN/DATA_OUT/OUT_EN 三个寄存器，直接手写 TL-UL 一拍响应状态机。 |
| `hdl/chisel/src/bus/GPIOTest.scala` | `GPIOSpec`：用 `tlWrite`/`tlReadData` 驱动 GPIO，验证输出与输入回读。 |
| `hdl/chisel/src/soc/SoCChiselConfig.scala` | SoC 配置「单一真相源」：`gpio` 模块的类、参数、总线连接、对外端口都在此声明。 |
| `hdl/chisel/src/soc/CrossbarConfig.scala` | 互联拓扑表：给 `gpio` 分配地址区间 `0x40030000`，并登记哪些主机能访问它。 |

## 4. 核心概念与源码讲解

### 4.1 内存映射外设的两种写法

#### 4.1.1 概念说明

写一个 MMIO 外设，核心只有两件事：

1. **寄存器映射**：决定「地址偏移 → 寄存器」的对应关系。例如 `0x00 → DATA_IN`、`0x04 → DATA_OUT`、`0x08 → OUT_EN`。
2. **总线响应**：当主机发来读/写事务时，按地址把数据读出或写入，并按协议回一个响应（成功 `OKAY`/`AccessAck`，或失败 `SLVERR`/`error`）。

CoralNPU 里这两件事有两种实现风格：

- **「映射表 + 工具」风格（AXI）**：把映射表交给 `ConnectAxiRead/ConnectAxiWrite`，握手与响应由工具自动生成，你只关心寄存器逻辑。代价是它们写死成 AXI 协议。
- **「手写状态机」风格（TL-UL）**：自己写一个 `tl_a.ready`/`tl_d.valid` 的小状态机。代码多一点，但能精确控制延迟、能直接接 TL-UL fabric。**生产 SoC 里的外设都走这条路。**

#### 4.1.2 核心流程

无论哪种风格，单次事务都遵循同一条逻辑链：

```
主机发起请求(地址+opcode+可选数据)
   │
   ▼
外设译码地址 → 命中某个寄存器？
   │                  │
  命中               未命中
   │                  │
   ▼                  ▼
读: 把寄存器值放进 D.data   返回错误响应
写: 把 A.data 写进寄存器   (SLVERR / error)
   │
   ▼
按协议回一拍响应(OKAY/AccessAck 或 错误码)
```

关键约束：**从机一次只处理一个未完成的请求时**，最简单的实现是「只要还有上一拍的响应没发出去，就不接收新请求」——GPIO 就是这么做的。

#### 4.1.3 源码精读

两种风格的「寄存器映射」都长得几乎一样。先看 GPIO 的寄存器表（TL-UL 风格）：

```scala
// GPIO.scala
object GpioRegs {
  val DATA_IN  = 0x00.U   // 只读：反映引脚输入
  val DATA_OUT = 0x04.U   // 读写：要输出的数据
  val OUT_EN   = 0x08.U   // 读写：每位输出使能
}
```

完整定义见
[hdl/chisel/src/bus/GPIO.scala:35-39](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L35-L39)
（GPIO 的三 个寄存器偏移量常量）。

再看 AXI 范例里的寄存器表——它用了 Scala `Map`，把名字同时映射到偏移和「值来源」：

```scala
// PeripheralInterfaceTest.scala (CounterAxiPeripheral)
val readMap = Map.apply(
    "count"  -> (0, count),
    "limit"  -> (4, limit),
    "enable" -> (8, enable),
)
val writeMap = Map.apply(
    "count"  -> 0,
    "limit"  -> 4,
    "enable" -> 8,
)
```

完整片段见
[hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala:33-44](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala#L33-L44)
（计数器外设的读映射 `name->(地址, 寄存器值)` 与写映射 `name->地址`）。注意：读表带「值」（即寄存器本身），写表只带「地址」——因为写值要由运行时事务提供。

#### 4.1.4 代码实践

**实践目标**：建立「外设 = 寄存器映射 + 总线响应」的直觉。

**操作步骤（源码阅读型）**：

1. 打开 `GPIO.scala`，数一数 `GpioRegs` 里有几个寄存器、各自偏移是多少、哪个是只读。
2. 打开 `PeripheralInterfaceTest.scala` 的 `CounterAxiPeripheral`，对照它的 `readMap`/`writeMap` 列出 count/limit/enable 的偏移。
3. 思考：如果要在 GPIO 里加一个「方向中断使能」寄存器，你会加在哪一行、用什么偏移（避免与现有 0x00/0x04/0x08 冲突）？

**需要观察的现象 / 预期结果**：偏移量一旦定下，软件侧的驱动代码（往哪个地址写）和硬件侧的译码（`is(偏移)`）必须完全一致——这是软硬件接口契约。本步无需运行，「待本地验证」项无。

#### 4.1.5 小练习与答案

**Q1**：GPIO 的 `DATA_IN` 为什么是只读？写出依据代码。
**答**：见 [GPIO.scala:79](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L79)，写分支里 `is(DATA_IN) { tl_d_error := true.B }`——往 `DATA_IN` 写会触发错误，因为它是反映物理引脚 `gpio_i` 的输入，由外部驱动，软件不能写。

**Q2**：CounterAxiPeripheral 的 `readMap` 和 `writeMap` 形状为什么不同？
**答**：读需要一个「当前值」放进响应，所以 `readMap` 是 `name -> (地址, 寄存器)`；写只需要知道「写到了哪个地址」并把事务带来的数据 latch 进对应寄存器，所以 `writeMap` 只到 `name -> 地址`，真正的数据由 `ConnectAxiWrite` 返回的 `writeData` 提供。

---

### 4.2 PeripheralInterface：用「映射表」自动接 AXI

#### 4.2.1 概念说明

`PeripheralInterface.scala` 文件名叫「外设接口」，但它并**没有**定义一个统一的抽象端口类。它的真正内容是两个工具对象：

- `ConnectAxiRead`：输入一张读映射表 `name -> (地址, 值)`，返回一个已接好的 `AxiMasterReadIO`——即一个能对 AXI 读请求自动回数的「AXI 从机读通道」。
- `ConnectAxiWrite`：输入一张写映射表 `name -> 地址` 和一条 AXI 写通道，返回「每个寄存器本拍是否被写」的 `Map[String, Bool]` 以及「写进来的数据」。

这对工具把 AXI 握手（缓冲请求、回响应、判 `OKAY`/`SLVERR`）全部封装掉，外设作者只需关心「寄存器该不该更新」。它是 **AXI 协议**的便利封装，与 SoC 内部的 TL-UL fabric 是两套体系。

> 关于 `ValidIO`：`ConnectAxiRead` 用 `MakeInvalid` / `MakeValid` 造「带有效位的值」。`MakeInvalid(gen)` 产出 `valid=false, bits=0`；`MakeValid(b)` 产出 `valid=true, bits=b`。这样查表命中就 `valid`、没命中就 `invalid`，从而决定回 `OKAY` 还是 `SLVERR`。定义在 [hdl/chisel/src/common/Library.scala:57-83](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Library.scala#L57-L83)。

#### 4.2.2 核心流程

读响应（`ConnectAxiRead`）：

```
1. 主机在 axi.addr 上发读地址(Decoupled)
2. Queue(addr, 1, pipe=true) 缓冲一拍请求
3. 对每个缓冲请求: MuxLookup(addr, MakeInvalid) 查 readMap
     命中 → ValidIO(valid=true, bits=寄存器值)
     未命中 → valid=false
4. 发一拍读数据: id=请求id, data=查到的bits,
                 resp = valid ? OKAY(0) : SLVERR(2), last=true
5. axi.data <> 这些响应
```

写响应（`ConnectAxiWrite`）：

```
1. 主机在 axi.addr 发写地址、axi.data 发写数据(两条 Decoupled)
2. 各缓冲一拍(Queue)
3. 仅当地址&数据都 valid 且主机 ready 时才回写响应
4. 比对 writeAddr 与 writeMap 各地址 → writes(name): Bool
5. resp = 任一命中 ? OKAY(0) : SLVERR(2)
6. 返回 (writes, writeData) 交给外设去更新寄存器
```

#### 4.2.3 源码精读

`ConnectAxiRead` 全文
[hdl/chisel/src/peripherals/PeripheralInterface.scala:27-50](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterface.scala#L27-L50)：
它把 AXI 读地址用 1 深队列缓冲，再用 `MuxLookup` 按地址在映射表里查值，查到就回 `OKAY`、查不到（`MakeInvalid`）就回 `SLVERR`（resp=2）。关键三 行——查值、取 `bits`、根据 `valid` 决定响应码——见
[hdl/chisel/src/peripherals/PeripheralInterface.scala:35-42](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterface.scala#L35-L42)
（`MuxLookup` 默认值用 `MakeInvalid`；`resp.data := regRead.bits`；`resp.resp := OKAY/SLVERR`；`resp.last := true.B`）。

`ConnectAxiWrite` 全文
[hdl/chisel/src/peripherals/PeripheralInterface.scala:57-77](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterface.scala#L57-L77)：
它把写地址和写数据各缓冲一拍，**等两边都 valid 才回响应**（AXI 写需要地址+数据齐了才算一次完整写）。然后把写地址和映射表里每个地址比较，得到「这个寄存器本拍是否被写」的布尔映射 `writes`，并把响应码设为「任一命中则 `OKAY` 否则 `SLVERR`」。最关键的一行是
[hdl/chisel/src/peripherals/PeripheralInterface.scala:70](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterface.scala#L70)
（`writes = writeMap.view.mapValues(_.U(32.W) === writeAddr)`——对每个寄存器名算「本拍写地址是否等于它的地址」）。

范例外设 `CounterAxiPeripheral` 把这对工具用起来：
[hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala:23-68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala#L23-L68)
（一个带 count/limit/enable 的 AXI 外设）。它的对外端口是 `Flipped(new AxiMasterIO(32,32,6))`——即一个 **AXI 从机端口**，外部主机连进来。读通道直接交给工具：

```scala
io.axi.read <> ConnectAxiRead(6, readMap)          // 第 38 行
val (writes, writeData) = ConnectAxiWrite(6, writeMap, io.axi.write)  // 第 45 行
```

于是「AXI 协议」部分一行就接好了，剩下的纯粹是计数器业务逻辑：
[hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala:54-65](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala#L54-L65)
（`enable =/= 0` 时每拍 `count + 1`，到 `limit` 回 0；若主机直接写 count 寄存器则用 `writeData` 覆盖；`writes("limit")`/`writes("enable")` 命中时更新对应寄存器）。

#### 4.2.4 代码实践

**实践目标**：跑通范例外设的仿真测试，验证你对 `writes`/`writeData` 的理解。

**操作步骤**：

1. 这个测试是 `chisel_test`，定义在 `hdl/chisel/src/peripherals/BUILD`（目标名 `peripheral_tests`）。运行：
   ```bash
   bazel test //hdl/chisel/src/peripherals:peripheral_tests
   ```
   （命令与目标名基于 BUILD 文件确认；具体能否在本地一次跑通取决于 Chisel/仿真环境，待本地验证。）
2. 读 `PeripheralInterfaceSpec` 的 `"Enable"` 用例：写 `enable=1` 后连跑 64 拍，断言 `io.count` 从 0 逐拍加 1。
3. **预测再验证**：在跑之前，先据 [第 54-57 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterfaceTest.scala#L54-L57) 的逻辑，回答「如果写 `limit=8`、`enable=1`，第 9 拍 `count` 应该是多少？」。

**需要观察的现象 / 预期结果**：`enable` 非零后 `count` 每拍 +1；`incCount >= limit` 时回 0。所以 `limit=8` 时 count 序列为 0,1,…,7,0,1,…。`"Read"`/`"Write"` 用例还验证：读未映射地址（如偏移 3）回 `resp=2 (SLVERR)`，写未映射地址（如偏移 6）同样回 `resp=2`。

#### 4.2.5 小练习与答案

**Q1**：为什么 `ConnectAxiWrite` 要「等地址和数据都 valid 才回响应」？
**答**：AXI 的一次写事务由 AW（写地址）和 W（写数据）两路独立通道组成，只有两路都到达，从机才拥有完整的「写哪里+写什么」，才能更新寄存器并回 B（写响应）。代码里 `writeAddrReq.ready := writeDataReq.valid && ...` 与 `writeDataReq.ready := writeAddrReq.valid && ...` 实现了这个汇合点（见 [PeripheralInterface.scala:64-67](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/peripherals/PeripheralInterface.scala#L64-L67)）。

**Q2**：若把 `readMap` 里某个地址写错成和另一个重复，会发生什么？
**答**：`MuxLookup` 会用匹配到的第一个表项，导致读到错误的寄存器值；这是软硬件地址契约错误，硬件不会自动报错——所以映射表的地址必须唯一。

---

### 4.3 GPIO：TL-UL 从机的「手写状态机」实现

#### 4.3.1 概念说明

`bus.GPIO` 是真正接进 CoralNPU SoC 的外设，挂在 **TL-UL** 总线上当从机（`Flipped(Host2Device)`）。它不像 `ConnectAxi*` 那样有现成工具包办握手，而是自己写一个极简的「单拍响应」状态机。读懂数 GPIO，就读懂了 SoC 里 SPI/DMA 等所有 TL-UL 外设的共同骨架。

GPIO 提供三 组 8 位引脚接口：输出 `gpio_o`、输出使能 `gpio_en_o`、输入 `gpio_i`，宽度由 `GPIOParameters(width)` 决定（SoC 里配成 8 位）。

#### 4.3.2 核心流程

GPIO 的 TL-UL 响应是一个「单 outstanding」状态机——任意时刻最多一个未完成的响应：

```
tl_a.ready := !tl_d_valid        # 还有响应没发完 → 不收新请求

when (tl_a.fire) {                # 收到一个 A 请求
  tl_d_valid := true              #   标记「有一个响应待发」
  if (opcode 是 Put 写) {
      D.opcode = AccessAck
      按地址写 data_out / out_en   # DATA_IN 例外: 置 error
  } else {                        # Get 读
      D.opcode = AccessAckData
      按地址读 gpio_i / data_out / out_en → D.data
  }
  地址未命中三寄存器 → error
}

when (tl_d.fire) {                # 响应被主机收走
  tl_d_valid := false             #   可以接下一个请求了
}
```

地址只取低 12 位 `address(11,0)`，因为 SoC 给 GPIO 分配了 4KB（`0x1000`）窗口，低 12 位就是窗口内的寄存器偏移。

#### 4.3.3 源码精读

模块声明与端口：
[hdl/chisel/src/bus/GPIO.scala:25-32](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L25-L32)
（`io.tl` 是 `Flipped(new OpenTitanTileLink.Host2Device(...))` 即 TL-UL 从机端口；另有 `gpio_o`/`gpio_en_o` 输出、`gpio_i` 输入）。寄存器只有两个可写的：`data_out`、`out_en`，复位为 0（[第 42-43 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L42-L43)）。引脚输出直接由寄存器驱动（[第 46-47 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L46-L47)）。

地址译码取低 12 位：
[hdl/chisel/src/bus/GPIO.scala:53](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L53)
（`val addr_offset = tl_a.bits.address(11, 0)`）。

整个响应状态机在
[hdl/chisel/src/bus/GPIO.scala:56-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L56-L97)：
- [第 63 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L63) `tl_a.ready := !tl_d_valid`——单 outstanding 的核心。
- [第 65-69 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L65-L69) 在 `tl_a.fire` 时锁存 `source`/`size` 并置 `tl_d_valid`。
- [第 71-72 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L71-L72) 用 opcode 判写（`PutFullData`/`PutPartialData`）；写分支 [第 74-80 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L74-L80) 按 `addr_offset` 写 `data_out`/`out_en`，且写 `DATA_IN` 置 `error`（只读保护）。
- 读分支（`otherwise` 即 `Get`）[第 81-89 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L81-L89) 把 `gpio_i`/`data_out`/`out_en` 选进 `tl_d_data`。
- [第 91-92 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L91-L92) 地址未命中三个已知寄存器则置 `error`。
- [第 95-97 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L95-L97) `tl_d.fire` 时清 `tl_d_valid`，开放下一个请求。

最后把锁存的结果一次性驱动到 D 通道：
[hdl/chisel/src/bus/GPIO.scala:99-107](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L99-L107)（`tl_d.valid/opcode/data/size/source/error/sink/param/user` 各字段赋值）。

> 设计要点：GPIO 把 D 通道做成**寄存器输出**（`tl_d_*` 都是 `Reg`），响应延迟固定为 1 拍，时序干净；代价是不能背靠背每拍都接新请求（必须等上一拍 D 发完）。对于慢速 GPIO 这种取舍完全合理。对比 `ConnectAxiRead` 用 `Queue(..., pipe=true)` 组合直通，思路不同但目的一样。

测试 `GPIOSpec`（`tlWrite`/`tlReadData` 来自 `TLULTestUtils`，见 [u3-l4 同族工具]）验证两条路径：
[hdl/chisel/src/bus/GPIOTest.scala:25-43](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIOTest.scala#L25-L43)（写 `DATA_OUT=0xa5`→`gpio_o=0xa5`，写 `OUT_EN=0xff`→`gpio_en_o=0xff`，回读一致）与
[hdl/chisel/src/bus/GPIOTest.scala:45-58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIOTest.scala#L45-L58)（`gpio_i` 驱动 `0x5a`→读 `DATA_IN=0x5a`）。

#### 4.3.4 代码实践

**实践目标**：跑通 GPIO 测试，确认三寄存器行为；再做一个「读未知地址」的最小扩展预测。

**操作步骤**：

1. 测试是 `chisel_test` 目标 `//hdl/chisel/src/bus:gpio_test`（见 `hdl/chisel/src/bus/BUILD`）：
   ```bash
   bazel test //hdl/chisel/src/bus:gpio_test
   ```
   （环境就绪应全绿；待本地验证。）
2. 阅读 `GPIOSpec` 的断言：`tlWrite(dut.io.tl, clock, 0x04.U, 0xa5.U)` 之后立刻 `peek` 到 `gpio_o==0xa5`，说明 `DATA_OUT` 直连引脚、无额外缓冲。
3. **思考实验**：若调用 `tlReadData(dut.io.tl, clock, 0x20.U)`（未映射地址），据 [第 91-92 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L91-L92)，`tl_d_error` 会被置位——`tlReadData`（见 [TLULTestUtils.scala:76-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLULTestUtils.scala#L76-L84)）只返回数据、不暴露 error，所以读回的数据是未定义的 0，但 D 通道会带 `error=true`。

**需要观察的现象 / 预期结果**：写 `DATA_OUT` 立即反映到 `gpio_o`；写 `OUT_EN` 立即反映到 `gpio_en_o`；`gpio_i` 的值可从 `DATA_IN(0x00)` 读回。

#### 4.3.5 小练习与答案

**Q1**：为什么 GPIO 用 `tl_a.ready := !tl_d_valid` 而不是 `true.B`？
**答**：因为它把响应存在寄存器里（`tl_d_*` 是 `Reg`），一次只能记一个响应。若上一拍响应还没被主机收走（`tl_d_valid` 仍为真）就又接新请求，新值会覆盖未发出的旧响应。`!tl_d_valid` 保证了「最多一个未完成事务」（单 outstanding）。

**Q2**：把 GPIO 从 8 位改成 16 位需要改哪几处？
**答**：`GPIOParameters(width=16)` 传 `width=16`（`gpio_o/en_o/i` 的位宽随之变成 16，见 [GPIO.scala:29-31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L29-L31)），`data_out`/`out_en` 的 `RegInit` 也跟着变（[第 42-43 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/GPIO.scala#L42-L43)）；再在 SoC 配置里把 `GPIOModuleParameters(width = 8)` 改成 16、把三 个 `externalPort` 的 `Logic(8)` 改成 `Logic(16)`。寄存器偏移不用变（仍按字节编址，一次写 32 位通道里低 16 位有效）。

---

### 4.4 把外设挂进 SoC：deviceConnections 与 externalPorts

#### 4.4.1 概念说明

写好外设模块只是第一步；它要能在 SoC 里被 CPU 访问，还得在两份配置里登记：

- **`SoCChiselConfig.scala`**：声明「有这么个模块」——它的类名、参数、总线端口连到哪个 socket、哪些物理引脚要提升到顶层。
- **`CrossbarConfig.scala`**：声明「它住在哪里」——给它分配一段地址区间，并登记哪些主机可以路由到它。

这两份就是 u3-l1 / u3-l4 讲过的「配置驱动的单一真相源」。GPIO 是其中最干净的范例。

#### 4.4.2 核心流程

挂一个 TL-UL 外设的完整闭环：

```
1. 写外设模块 (如 GPIO)，io.tl 为 Flipped(Host2Device)
2. SoCChiselConfig 加一个 ChiselModuleConfig:
     name = "xxx", moduleClass = "bus.XXX",
     deviceConnections = Map("io.tl" -> "xxx"),   # 作为 TL-UL 从机接到 socket
     externalPorts = Seq( ...物理引脚... )          # 提升到顶层
3. CrossbarConfig 加一个 DeviceConfig:
     DeviceConfig("xxx", Seq(AddressRange(BASE, SIZE)))
   并把 "xxx" 加进相关主机的 device 列表
4. (可选) 写 GPIOTest 风格的测试
```

`deviceConnections = Map("io.tl" -> "gpio")` 的含义：把模块的 `io.tl` 端口（TL-UL device 端）连到名字叫 `"gpio"` 的 socket/从机入口——这正是 u3-l4 里 Socket1N 的「从机端口」。`externalPorts` 则把 `gpio_o` 等非总线端口提升到 SoC 顶层（参见 u3-l1 的 `ExternalPort` 端口提升）。

#### 4.4.3 源码精读

GPIO 的模块配置：
[hdl/chisel/src/soc/SoCChiselConfig.scala:200-210](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L200-L210)（`gpio` 模块：`moduleClass = "bus.GPIO"`、`GPIOModuleParameters(width = 8)`、`deviceConnections = Map("io.tl" -> "gpio")`、三个 `externalPort` 分别提升 `gpio_o`/`gpio_en_o`/`gpio_i` 为 8 位顶层引脚）。

GPIO 的地址分配与可达性：
[hdl/chisel/src/soc/CrossbarConfig.scala:113](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L113)
（`DeviceConfig("gpio", Seq(AddressRange(0x40030000, 0x1000)))`——基址 `0x40030000`、大小 `0x1000` 即 4KB）。这正是为什么 GPIO 里 `addr_offset = address(11,0)`——低 12 位正好覆盖 4KB 窗口内的寄存器偏移。
[hdl/chisel/src/soc/CrossbarConfig.scala:127](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L127) 与
[第 129、135 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L129-L135) 把 `"gpio"` 列入 `coralnpu_core`、`dma`、`test_host_32` 等主机的从机列表，于是这些主机都能路由到 GPIO。

> 串起来：CPU 发 `0x40030004` 的写 → Socket1N 按 `0x4003_0000~0x4003_0fff` 译码选中 `gpio` → 到达 GPIO 的 `io.tl.a`，`addr_offset = 0x004` 命中 `DATA_OUT` → `data_out` 被更新 → `gpio_o` 引脚变化。一条从 CPU 到引脚的完整通路就通了。

#### 4.4.4 代码实践

**实践目标**：把「挂新外设」抽象成可复用的清单，用 GPIO 验证每一项都能在源码里定位。

**操作步骤（源码阅读型 + 设计型）**：

1. 在 GPIO 的配置上逐条勾选下面的「接入清单」，每条都到源码里找到对应行号。
2. **设计练习**：假设要新增一个只读的「版本号寄存器」外设 `Revision`（TL-UL 从机，单寄存器，偏移 0x00，返回固定 32 位值 `0x0001_0000`，地址区间 `0x40040000~0x40040FFF`）。仿照 GPIO 写出：
   - 外设模块骨架（端口、寄存器映射、TL-UL 单拍响应）；
   - `SoCChiselConfig` 里要加的 `ChiselModuleConfig`；
   - `CrossbarConfig` 里要加的 `DeviceConfig` 与主机路由项。

**「挂一个新 TL-UL 外设」最小接入清单**：

| # | 事项 | GPIO 对应 |
| --- | --- | --- |
| 1 | 外设 `Module`，`io.tl = Flipped(Host2Device)` + 业务端口 | `GPIO.scala:25-32` |
| 2 | 寄存器映射 object（偏移量） | `GPIO.scala:35-39` |
| 3 | TL-UL 单拍响应状态机（`tl_a.ready:=!tl_d_valid`；fire 分派读/写；fire D 清 valid；未知地址置 error） | `GPIO.scala:56-97` |
| 4 | `SoCChiselConfig` 加 `ChiselModuleConfig`（类、参数、`deviceConnections`、`externalPorts`） | `SoCChiselConfig.scala:200-210` |
| 5 | `CrossbarConfig` 加 `DeviceConfig(地址区间)` 并加入主机路由 | `CrossbarConfig.scala:113,127` |
| 6 | （可选）`GPIOSpec` 风格测试，用 `tlWrite`/`tlReadData` 驱动 | `GPIOTest.scala:25-58` |

**需要观察的现象 / 预期结果**：清单前 3 项是「外设内部」，后 2 项是「SoC 登记」——缺任何一项外设都不可达（缺 4 则没实例化，缺 5 则没有地址、访问会命中 `TlulErrorResponder` 返回错误，参见 u3-l4）。本步无需编译运行；若你真的动手写，编译命令为 `bazel build //hdl/chisel/src/soc:<soc_lib 目标>`，目标名以 BUILD 为准（待本地验证）。

#### 4.4.5 小练习与答案

**Q1**：如果忘了在 `CrossbarConfig` 里给 `gpio` 加 `DeviceConfig`，CPU 访问 `0x40030000` 会发生什么？
**答**：地址译码不会命中任何从机，请求落到 u3-l4 讲过的 `TlulErrorResponder`，D 通道返回带 `error` 的响应而不是把数据送给 GPIO；GPIO 模块即便被实例化也收不到事务。

**Q2**：`deviceConnections = Map("io.tl" -> "gpio")` 里左右两个 `"gpio"` 含义相同吗？
**答**：不同。左边 key `"io.tl"` 是**模块上的端口名**（GPIO 的 TL-UL device 端口）；右边 value `"gpio"` 是**socket/从机入口的名字**，要和 `CrossbarConfig` 里 `DeviceConfig("gpio", ...)` 的从机名一致。它俩同名只是约定，不是必须。

---

## 5. 综合实践

**任务**：以 GPIO 为模板，设计并「纸上实现」一个 TL-UL 双寄存器外设 `LedBlinker`，把本讲四 个最小模块串起来。

要求：

1. **寄存器映射**（模块 4.1）：定义两个寄存器——`PERIOD`（偏移 `0x00`，读写，控制翻转周期）、`CTRL`（偏移 `0x04`，读写，bit0=使能）。再加一个只读 `STATE`（偏移 `0x08`）反映当前输出电平。
2. **TL-UL 状态机**（模块 4.3）：照搬 GPIO 的单 outstanding 写法（`tl_a.ready := !tl_d_valid`；`tl_a.fire` 时按 opcode 分派；未知地址置 `error`）。
3. **业务逻辑**：内部有一个自由计数器，`CTRL.enable=1` 且计数到 `PERIOD` 时翻转一个输出寄存器，引脚 `led_o` 输出该寄存器。
4. **SoC 登记**（模块 4.4）：写出 `SoCChiselConfig` 的 `ChiselModuleConfig`（`moduleClass="bus.LedBlinker"`、`deviceConnections=Map("io.tl"->"led")`、`externalPorts=Seq(ExternalPort("led_o", Bool, Out, "io.led_o"))`）和 `CrossbarConfig` 的 `DeviceConfig("led", Seq(AddressRange(0x40050000, 0x1000)))`，并把 `"led"` 加入 `coralnpu_core` 的主机路由。
5. **验证设想**：仿照 `GPIOSpec`，用 `tlWrite` 写 `CTRL=1`、`PERIOD=N`，然后步进时钟 `2N` 拍，断言 `led_o` 发生过翻转。

**交付物**：一段不超过 60 行的 Chisel 骨架 + 两段配置片段 + 一段测试设想。完成后对照 4.4.4 的接入清单自查六 条是否齐全。

> 这个任务如果真要跑，需要把它加入 `hdl/chisel/src/bus/BUILD` 的 `chisel_library` 并新建 `chisel_test`，命令以 BUILD 实际目标为准（待本地验证）。本讲不要求上板，重点是走通「寄存器映射 → TL-UL 响应 → SoC 登记」的完整心智模型。

## 6. 本讲小结

- CoralNPU 外设本质是「**内存映射寄存器 + 总线响应状态机**」；写外设只需定寄存器偏移，再按协议回响应。
- `peripheral` 包的 `ConnectAxiRead`/`ConnectAxiWrite` 是 **AXI** 风格的便利工具：把 `Map` 形式的寄存器映射表自动接成 AXI 从机的读/写响应，外设只写业务逻辑（范例 `CounterAxiPeripheral`）。
- `bus.GPIO` 是 **TL-UL** 风格的生产实现：手写「`tl_a.ready := !tl_d_valid`」单拍响应状态机，DATA_IN(只读)/DATA_OUT/OUT_EN 三寄存器，是 SoC 里所有 TL-UL 外设的骨架。
- AXI 工具省代码、TL-UL 手写更可控；二者思路一致，但真正进 SoC fabric 的外设都走 TL-UL。
- 挂新外设 = 在 `SoCChiselConfig` 登记 `ChiselModuleConfig`（类/参数/`deviceConnections`/`externalPorts`）+ 在 `CrossbarConfig` 登记 `DeviceConfig`（地址区间）与主机路由；GPIO 的地址是 `0x40030000`、4KB 窗口。
- 软硬件契约：寄存器偏移在硬件译码（`is(偏移)`）和软件驱动（写哪个地址）两侧必须一字不差；未映射地址一律回错误响应（AXI `SLVERR` / TL-UL `error`）。

## 7. 下一步学习建议

- **横向对比同类 TL-UL 外设**：读 `hdl/chisel/src/bus/SpiMaster.scala`、`DmaEngine.scala`，它们和 GPIO 共享同一套 TL-UL 从机骨架，但寄存器更多、状态机更复杂，能加深对 4.3 的理解。
- **回到总线层**：复习 u3-l4（Socket1N/SocketM1）的地址译码与仲裁，理解 `deviceConnections`/`DeviceConfig` 在交叉开关里到底怎么变成路由表。
- **进阶验证**：本讲用了 chisel 级 `GPIOSpec`；后续可在 cocotb（u2-l4 / u11-l3）里写一个端到端测试，经真实 AXI 主机→TL-UL 桥→GPIO 跑一遍，体会 frontdoor 与 backdoor 的区别。
- **如果要做 SoC 级实验**：尝试按 4.4 的清单真的把一个新外设接进 `SoCChiselConfig`/`CrossbarConfig`，用 `bazel build` 生成 SoC 顶层，并用 `git diff` 观察配置改动如何反映到生成的 RTL 端口上。
