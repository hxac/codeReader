# 总线与外设桥接（I2C / SPI / UART / SACI）

## 1. 本讲目标

学完本讲，你应当能够：

- 理解「外设寄存器桥」要解决的根本问题：让 CPU 或外部主机像读写内存一样去访问 I2C / SPI / SACI 这些**慢速、串行、寄存器式**的外设。
- 掌握 SURF 的三个「从机桥」（I2C、SPI、SACI）共享的统一套路：吃 AXI-Lite 从口 → 把 AXI 地址切片成「设备号 / 命令 / 寄存器地址」→ 每次访问触发一次外设 req/ack 事务 → 把成功/失败翻译成 AXI 响应码。
- 识别出 UART 桥是**反方向**的「主机桥」：外部主机通过 UART 文本协议驱动 FPGA 内部的 AXI-Lite 总线，它复用了 u3-l3 的 `AxiLiteMaster`。
- 理解 `I2cRegMasterMux` 如何把一个慢速外设引擎在多个主机之间轮询复用，并用 `lockReq` 保证多事务的原子性。

## 2. 前置知识

本讲默认你已经掌握 u3-l3 的内容，尤其是：

- **AXI-Lite 记录类型**：`AxiLiteReadMasterType` 等四个记录、「VALID 与数据归生产方、READY 归消费方」的归属口诀（u3-l1）。
- **AXI-Lite 从机四步骨架**：`axiSlaveWaitTxn` 解码事务、`axiSlaveRegister(R)` 绑定地址、`axiSlaveReadResponse/WriteResponse` 回响应、`axiSlaveDefault` 兜底未映射地址（u3-l2）。
- **`AxiLiteMaster` 与 req/ack 接口**：`AxiLiteReqType`（`request/rnw/address/wrData`）与 `AxiLiteAckType`（`done/resp/rdData`）如何把单次读/写封装成五通道握手（u3-l3）。
- **`AxiLiteCrossbar`**：如何把多段地址窗口接到多个从机（u3-l3）。本讲的几个从机桥最终都是挂在交叉开关的某个从机窗口里。

再用一句通俗的话补一个外设常识：I2C / SPI / SACI 都是「主控发一拍命令、外设回一拍数据」的**寄存器式**串行协议，单次访问往往要几十到几千个时钟周期，比 AXI-Lite 的单拍访问慢得多。桥的核心难点不是协议本身，而是「如何把一个慢动作塞进 AXI-Lite 的一次事务里」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [protocols/i2c/rtl/I2cPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cPkg.vhd) | I2C 记录类型（`I2cRegMasterInType/OutType`）、设备映射类型 `I2cAxiLiteDevType` 与 `MakeI2cAxiLiteDevType`、`maxAddrSize`、错误码常量。 |
| [protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd) | **I2C 从机桥**：把 N 个 I2C 设备映射到一段 AXI-Lite 地址窗口，做地址切片与 req/ack 翻译。 |
| [protocols/i2c/rtl/I2cRegMaster.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMaster.vhd) | I2C 字节级协议引擎：把一条寄存器请求拆成 START/地址/读/写/STOP 的字节序列驱动 `I2cMaster`。 |
| [protocols/i2c/axi/AxiI2cRegMasterCore.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/axi/AxiI2cRegMasterCore.vhd) | 集成顶层：把 `I2cRegMasterAxiBridge` + `I2cRegMaster` 拼起来，并算好 `PRESCALE/FILTER`。 |
| [protocols/i2c/rtl/I2cRegMasterMux.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterMux.vhd) | **主控复用器**：把 2~8 个主机轮询复用到一个 `I2cRegMaster`，带总线锁定。 |
| [protocols/spi/rtl/AxiSpiMaster.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd) | **SPI 从机桥**：AXI-Lite 从口 → 每次 access 触发一次 SPI 事务，支持多片选与影子 RAM。 |
| [protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd) | **SACI 从机桥**：AXI-Lite → SLAC ASIC 控制协议，地址切片出 chip/cmd/addr，带超时与总线仲裁。 |
| [protocols/saci/saci1/rtl/SaciMasterPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/SaciMasterPkg.vhd) | SACI 记录类型 `SaciMasterInType/OutType`（req/chip/op/cmd/addr/wrData ↔ ack/fail/rdData）。 |
| [protocols/uart/rtl/UartAxiLiteMaster.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMaster.vhd) | **UART 主机桥**顶层：UART PHY + FSM，对外是 AXI-Lite **主口**。 |
| [protocols/uart/rtl/UartAxiLiteMasterFsm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd) | UART 文本协议解析 FSM，用 u3-l3 的 `AxiLiteMaster` 发起事务。 |

## 4. 核心概念与源码讲解

### 4.1 I2C 寄存器桥：把一条 I2C 总线映射成内存窗口

#### 4.1.1 概念说明

设想 CPU 想读一个 I2C 温度传感器的寄存器。原生 I2C 协议要求：发 START → 发 7 位设备地址 + 写 → 发寄存器地址 → 重发 START → 发设备地址 + 读 → 收数据 → STOP。这是一串慢速字节操作，CPU 不可能直接驱动。

「寄存器桥」的想法是：在 FPGA 里放一个状态机替 CPU 干这些脏活，**对外暴露一段普通的 AXI-Lite 地址窗口**。CPU 只需对某个地址做一次 32 位读/写，桥就把这次访问翻译成上面那一串 I2C 字节操作，等外设回完，再把结果塞回 AXI-Lite 的读数据通道。

`I2cRegMasterAxiBridge` 解决的关键问题是：**一条 I2C 总线上往往挂多个设备**，如何在一段连续地址窗口里把「设备号」和「设备内寄存器地址」都编进去？答案是把 AXI 地址切成三段。

#### 4.1.2 核心流程

地址布局（从低位到高位）：

```
[ 设备索引 (log2 N 位) | 寄存器地址 (maxAddrSize 位) | 2'b00 字对齐 ]
```

- 最低 2 位永远是 AXI-Lite 的字对齐位，丢弃不用（和 u3-l2 一致）。
- 中间是寄存器地址，宽度取**所有设备里最大的** `addrSize`，保证布局统一。
- 最高位是设备索引，宽度 `log2(设备数)`；若只有 1 个设备则这段宽度为 0，不占位。

一次访问的时序：

1. CPU 发 AXI-Lite 读/写，桥用 `axiSlaveWaitTxn` 解码，**先不回响应**。
2. 从地址里切出设备号 `devInt`，查 `DEVICE_MAP_G(devInt)` 得到该设备的 I2C 地址、数据宽度、地址宽度、端序等。
3. 组装一条 `I2cRegMasterInType` 请求（含 I2C 设备地址、寄存器地址、写数据、读/写标志），拉高 `regReq`。
4. 等字节级引擎 `I2cRegMaster` 完成全部 START/地址/数据/STOP，回 `regAck`。
5. 根据 `regFail` 决定回 `AXI_RESP_OK_C` 还是 `AXI_RESP_SLVERR_C`，调用 `axiSlaveReadResponse/WriteResponse` 关闭这次 AXI 事务。

注意第 1 步到第 5 步之间 AXI 事务一直「张着嘴」没回响应——这正是把慢外设塞进 AXI-Lite 的代价：**整个窗口的访问延迟等于一次完整 I2C 事务**。AXI-Lite 本身没有超时，所以这种「张嘴等」是合法的。

#### 4.1.3 源码精读

**地址切片常量** —— 整个桥的精髓在这一段：

[protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd:55-74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd#L55-L74) 定义了三段地址范围。`I2C_REG_ADDR_SIZE_C := maxAddrSize(DEVICE_MAP_G)` 取所有设备最大的 `addrSize`；寄存器地址段从 bit 2 开始；设备索引段紧接其后，宽度由 `log2(DEVICE_MAP_LENGTH_C)` 决定，并在设备数为 1 时退化为 0 宽（`ite(... = 1, LOW, LOW+log2-1)`）。

**设备映射类型** —— 每个设备的「个性」用一个记录描述：

[protocols/i2c/rtl/I2cPkg.vhd:162-177](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cPkg.vhd#L162-L177) 定义 `I2cAxiLiteDevType`（`i2cAddress/dataSize/addrSize/endianness/repeatStart`）和构造函数 `MakeI2cAxiLiteDevType`。该函数会根据传入 `i2cAddress` 是 7 位还是 10 位自动判定 `tenbit` 标志（[I2cPkg.vhd:243-251](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cPkg.vhd#L243-L251)）。默认映射在 [I2cPkg.vhd:181-185](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cPkg.vhd#L181-L185) 给了 4 个示例设备。

**把 AXI 访问翻译成 I2C 请求** —— comb 进程里的 `setI2cRegMaster` 函数：

[protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd:103-123](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd#L103-L123) 从设备表查参数，把 AXI 地址里的寄存器地址段赋给 `regAddr`、把 `wData` 赋给 `regWrData`，并用 `wordCount(addrSize,8)-1` 算出地址字节数编码 `regAddrSize`（`wordCount` 定义见 [base/general/rtl/StdRtlPkg.vhd:803-811](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L803-L811)）。

**读写分支与响应翻译** —— 桥没有显式状态机，状态隐含在 `regReq` 是否为高：

[protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd:128-168](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd#L128-L168) 是核心。先 `axiSlaveWaitTxn` 解码；写命中时（L130）切设备号、置 `regOp='1'/regReq='1'`；读命中时（L141）同理置 `regOp='0'`。关键的收尾在 L154：当 `i2cRegMasterOut.regAck='1'` 且本地 `regReq='1'` 时，撤掉请求，用 `ite(regFail='1', SLVERR, OK)` 选响应码——**这就是把 I2C 失败翻译成 AXI SLVERR 的那一行**。读失败时还会把 8 位 `regFailCode` 塞进 `rdata` 低字节（L162-164）。

> I2C 错误码本身定义在 [I2cPkg.vhd:52-55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cPkg.vhd#L52-L55)：`0x01` 非法地址、`0x02` 写 ACK 错、`0x03` 仲裁丢失、`0x04` 超时。

**字节级引擎 I2cRegMaster** —— 桥只管「发一条请求」，真正的 I2C 时序在这里：

[protocols/i2c/rtl/I2cRegMaster.vhd:44-51](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMaster.vhd#L44-L51) 是 7 态状态机 `WAIT_REQ_S → ADDR_S → WRITE_S/READ_TXN_S → READ_S → REG_ACK_S`（外加 `BUS_ACK_S`）。它在 [L104-118](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMaster.vhd#L104-L118) 例化 bit 级核 `I2cMaster`，逐字节搬运地址与数据，端序由 [L87-100](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMaster.vhd#L87-L100) 的 `getIndex` 决定。`PRESCALE_G/FILTER_G` 的算法写在文件头注释 [L6-7](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMaster.vhd#L6-L7)：`PRESCALE = clk/(5·i2c_freq) − 1`，`FILTER = min_pulse/clk_period + 1`。

**集成顶层** —— 工程里通常不直接用桥，而用拼好的 `AxiI2cRegMasterCore`：

[protocols/i2c/axi/AxiI2cRegMasterCore.vhd:48-52](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/axi/AxiI2cRegMasterCore.vhd#L48-L52) 用 `getTimeRatio` 把「AXI 时钟频率 / I2C 频率 / 最小脉冲」换算成 `PRESCALE_C/FILTER_C`；[L91-124](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/axi/AxiI2cRegMasterCore.vhd#L91-L124) 把 `I2cRegMasterAxiBridge` 与 `I2cRegMaster` 用一组 `i2cRegMasterIn/Out` 信号对接，桥的 AXI 口还可经 `AXIL_PROXY_G` 套一层 `AxiLiteMasterProxy`（让远端经 SRP/PGP 也能访问这片 I2C）。

> 小提醒：本桥用最朴素的同步复位 `if (axiRst='1') then v := REG_INIT_C`（[L173-175](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd#L173-L175)），并未声明 u1-l5 的 `RST_POLARITY_G/RST_ASYNC_G`。不少纯 AXI 时钟域的叶子模块都这样做，把它当作约定俗成即可。

#### 4.1.4 代码实践

**实践目标**：给 `I2cRegMasterAxiBridge` 设计一份两设备映射，手算出「AXI 地址 → 设备/寄存器」的完整对应规则。

**操作步骤**：

1. 设定两个真实风格的设备：
   - 设备 0：温度传感器，I2C 7 位地址 `0x48`（`"1001000"`），8 位数据，8 位寄存器地址。
   - 设备 1：EEPROM，I2C 7 位地址 `0x50`（`"1010000"`），8 位数据，16 位寄存器地址。

2. 用 `MakeI2cAxiLiteDevType` 写出 `DEVICE_MAP_G`（**示例代码**，非仓库原有）：
   ```vhdl
   constant MY_DEV_MAP_C : I2cAxiLiteDevArray(0 to 1) := (
      0 => MakeI2cAxiLiteDevType("1001000", 8,  8, '0'),  -- 温度传感器 0x48
      1 => MakeI2cAxiLiteDevType("1010000", 8, 16, '0')); -- EEPROM    0x50
   ```

3. 推导地址布局：`maxAddrSize = max(8,16) = 16`，所以寄存器地址段 = bit `[17:2]`（16 位 = 64 KB），设备索引段 = bit `[18]`（`log2(2)=1` 位）。布局为 `[dev[18] | regaddr[17:2] | 2'b00[1:0]]`。

4. 算出两个设备的基地址：
   - 设备 0 基地址 = `0x0_0000`。
   - 设备 1 基地址 = bit18 = `0x4_0000`。

**需要观察的现象 / 预期结果**：

- 写设备 0 的寄存器 `0x03`：AXI 地址 = `0x03 << 2 = 0x0000_000C`。
- 读设备 1 的寄存器 `0x0100`：AXI 地址 = `0x4_0000 + (0x0100<<2) = 0x0004_0400`。
- 设备 0 的 `regAddrSize` 编码 = `wordCount(8,8)-1 = 0`（1 字节地址），设备 1 = `wordCount(16,8)-1 = 1`（2 字节地址）—— 验证 [I2cRegMasterAxiBridge.vhd:117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd#L117) 的算法。
- 因为窗口按最大设备（16 位地址）统一开，设备 0 实际只用得到 `[9:2]`，高位 `[17:10]` 是「浪费但被忽略」的地址位。

把以上四条写进一张「AXI 地址 → (设备, I2C 地址, 寄存器地址)」映射表，就完成了任务。

#### 4.1.5 小练习与答案

**练习 1**：如果 `DEVICE_MAP_G` 只有 1 个设备，设备索引段宽度是多少？为什么不会浪费地址位？
**答案**：0 位。代码 [L68-71](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterAxiBridge.vhd#L68-L71) 用 `ite(LENGTH=1, LOW, LOW+log2-1)` 把 `HIGH` 设回 `LOW`，即空范围，整段窗口全部给寄存器地址。

**练习 2**：一次 I2C 读访问，CPU 看到的 AXI 响应延迟大约是多少个 axiClk？
**答案**：约等于一次完整 I2C 事务的时长。若 `I2C_SCL_FREQ_G=100 kHz`、读 1 字节寄存器地址 + 1 字节数据，START/地址/重启/读/STOP 合计约 10 个 SCL 周期 ≈ 100 µs；在 156.25 MHz 下约 15625 拍。这正是「张嘴等」的代价。

---

### 4.2 SPI / SACI 桥：地址切片 + 每次访问一次外设事务

#### 4.2.1 概念说明

SPI 桥（`AxiSpiMaster`）和 SACI 桥（`AxiLiteSaciMaster`）与 I2C 桥同属「从机桥」，套路几乎一致：吃 AXI-Lite 从口、切片地址、一次访问触发一次外设事务、回响应。差别只在「外设事务长什么样」：

- **SPI**：一次访问把 `[读/写位 | 寄存器地址 | 数据]` 拼成一个移位包，整包从 MOSI 移出、MISO 移入。多片选时用地址里的高位选片。
- **SACI**（SLAC ASIC Control Interface，用于控制前端 ASIC）：一次访问给出 `chip/cmd/addr/wrData`，等 ASIC 回 `ack/fail/rdData`。SACI 还多了总线仲裁与超时保护。

它们共同揭示了一个模式：**AXI 地址不只是「寄存器偏移」，而是被复用成一个结构化命令字**——把外设需要的所有控制字段（片选、命令码、地址）都编进 32 位 AXI 地址里。

#### 4.2.2 核心流程

SPI 桥的地址布局（多片选时）：

```
[ 片选 (log2 NUM_CHIPS) | 寄存器地址 (ADDRESS_SIZE_G) | 2'b00 ]
```

移位包结构：`PACKET_SIZE_C = (RW?1:0) + ADDRESS_SIZE_G + DATA_SIZE_G`。读访问时数据段先置全 1（让从机能在共享 SDIO 上回驱），写访问时填入 `wdata`。整个包一次性移出，移完 `rdEn` 拉高，桥回 AXI 响应。

`MODE_G` 决定方向约束：`"RO"`（只读）收到写访问直接回 `DECERR`，`"WO"`（只写）收到读访问回 `DECERR`。`SHADOW_EN_G` 打开后，写值会同时存进一块 `DualPortRam`「影子 RAM」，读访问直接查影子而不必真去访问芯片——既加速又保护只写寄存器。

SACI 桥的地址布局：

```
[ chip (bits 23:22) | cmd (bits 20:14, 7 位) | addr (bits 13:2, 12 位) | 2'b00 ]
```

SACI 是面向 ASIC 的，比 I2C/SPI 多两层保护：一是**总线仲裁**（`saciBusReq/saciBusGr`），未拿到总线（`saciBusGr=0`）时访问直接回 `SLVERR`；二是**超时**（`AXIL_TIMEOUT_G`），若 ASIC 久不回 `ack`，定时器到点强制回 `SLVERR` 并复位 SACI 核。

#### 4.2.3 源码精读

**SPI 包宽与地址切片**：

[protocols/spi/rtl/AxiSpiMaster.vhd:73](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L73) 定义 `PACKET_SIZE_C`。读访问的地址/片选切片见 [L162-164](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L162-L164)：寄存器地址取 `araddr(2+ADDRESS_SIZE_G-1 downto 2)`，片选取 `araddr(CHIP_BITS_C+ADDRESS_SIZE_G+1 downto 2+ADDRESS_SIZE_G)`——和 I2C 桥完全同构的切片手法。写访问同款逻辑在 [L197-203](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L197-L203)。

**SPI 的方向约束**：

[protocols/spi/rtl/AxiSpiMaster.vhd:165-166](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L165-L166) 与 [L187-189](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L187-L189) 是 `MODE_G="WO"` 读回 `DECERR`、`MODE_G="RO"` 写回 `DECERR` 的两处。响应码用法与 u3-l1/u3-l2 完全一致。

**SPI 5 态 FSM 与影子 RAM**：

状态机定义在 [AxiSpiMaster.vhd:80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L80)（`WAIT_AXI_TXN_S/WAIT_CYCLE_S/WAIT_SPI_TXN_DONE_S/...`），comb 主体 [L145-254](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L145-L254) 用 `axiSlaveWaitTxn` 解码、在 `WAIT_SPI_TXN_DONE_S` 等 `rdEn` 回响应。影子 RAM 的 `DualPortRam` 例化见 [L111-142](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L111-L142)，其 `doutb => shadowData` 把缓存值暴露给硬件其他模块直接读。底层 SPI 移位核在 [L263-283](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L263-L283) 例化 `SpiMaster`。文件头 [L5-13](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L5-L13) 注释说明了多芯片两条路：多核挂多 crossbar 从口，或单核用 `coreMCsb` 片选。

**SACI 地址切片与记录**：

[protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd:182-187](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L182-L187) 是写访问切片：`chip := awaddr(22+CHIP_BITS-1 downto 22)`、`cmd := awaddr(20 downto 14)`、`addr := awaddr(13 downto 2)`、`wrData := wdata`。SACI 的请求/应答记录 `SaciMasterInType/OutType`（`req/chip/op/cmd/addr/wrData ↔ ack/fail/rdData`）定义在 [SaciMasterPkg.vhd:31-45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/SaciMasterPkg.vhd#L31-L45)，与 I2C 的 `I2cRegMasterInType` 思想一致。

**SACI 的总线仲裁 + 超时双保险**：

超时常量 [AxiLiteSaciMaster.vhd:59](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L59) `TIMEOUT_C := AXIL_TIMEOUT_G/AXIL_CLK_PERIOD_G - 1`。`IDLE_S` 里只有 `saciBusGr=1 且 asicRstL=1` 才发请求，否则立即回 `SLVERR`（[L174-213](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L174-L213)）；`SACI_REQ_S` 里 `(ack=1 and fail=1) or timer=TIMEOUT_C` 都会被翻译成 `SLVERR` 并复位 SACI 核（[L216-224](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L216-L224)）。底层协议核在 [L120-144](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L120-L144) 例化 `SaciMaster2`。注意 SACI 桥还在 [L107-118](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L107-L118) 用一串 `assert` 把「时钟 < 超时 < ...」的量纲关系在 elaboration 阶段钉死。

> 三者对比：I2C/SPI 桥「张嘴等」依赖外设最终会回 ack；SACI 桥不信任这一点，自带超时兜底。给慢且可能挂死的外设做桥，超时几乎是必需品。

#### 4.2.4 代码实践

**实践目标**：阅读 `AxiSpiMaster`，推导一次「读 SPI 芯片寄存器 0x1234」在 AXI 总线上长什么样，并画出地址到命令字的映射。

**操作步骤**：

1. 假设配置：`ADDRESS_SIZE_G=15`、`DATA_SIZE_G=8`、`MODE_G="RW"`、`SPI_NUM_CHIPS_G=1`。
2. 算出 `PACKET_SIZE_C = 1 + 15 + 8 = 24` 位，即每次 SPI 事务移位 24 位（1 位 R/W + 15 位地址 + 8 位数据）。
3. 读寄存器 `0x1234`：AXI 地址 = `0x1234 << 2 = 0x48D0`（寄存器地址放在 `araddr(16:2)`，单芯片时无片选位）。
4. 跟踪 comb：读命中后 `wrData(23):='1'`（读标志）、`wrData(22:8) := 0x1234`（地址）、`wrData(7:0) := 全1`（数据段浮空让从机回驱）。

**需要观察的现象 / 预期结果**：

- 写入 `wrData` 后 `wrEn` 拉高一拍，状态进 `WAIT_CYCLE_S → WAIT_SPI_TXN_DONE_S`（[L209-230](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L209-L230)）。
- 24 位移完后 `rdEn='1'`，`rdData(7:0)` 回填进 `axiReadSlave.rdata`，回 OKAY 响应。
- 若把 `MODE_G` 改成 `"WO"`，同样的读访问会立刻回 `DECERR` 而不移位——验证 [L165-166](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L165-L166)。

**待本地验证**：上述移位时序需在有 `SpiMaster` 仿真模型的工程中验证；本实践为源码阅读型，重点是把「AXI 地址 → 24 位命令字」的映射写清楚。

#### 4.2.5 小练习与答案

**练习 1**：SPI 桥的 `SHADOW_EN_G` 打开后，读访问还会真的去访问 SPI 芯片吗？
**答案**：不会。读访问进 `WAIT_CYCLE_SHADOW_S → SHADOW_READ_DONE_S`（[L216-235](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L216-L235)），直接从影子 `DualPortRam` 取上次写过的值回响应，单拍级完成、不发起移位。

**练习 2**：SACI 桥为什么需要 `asicRstL` 这个输入？若它为 0 会怎样？
**答案**：ASIC 复位期间不应接受访问。`IDLE_S` 里 `asicRstL=0` 时走 else 分支，读写都立即回 `SLVERR`（[L207-213](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L207-L213)），避免向尚未就绪的 ASIC 发命令。

---

### 4.3 UART 主桥 + 主控复用：反向桥与共享外设引擎

#### 4.3.1 概念说明

前两节的桥都是「CPU → 外设」的**从机桥**。`UartAxiLiteMaster` 走的是反方向：FPGA 里没有 CPU，外部主机（PC）通过一根 UART 串口，**反过来**驱动 FPGA 内部的 AXI-Lite 总线。所以它的 AXI 口是 **主口**（`mAxilReadMaster/WriteMaster` 是 `out`），不是从口。

它的工作方式是一条极简文本协议：主机发 `w ADDR DATA\r` 写、`r ADDR\r` 读，FPGA 解析后用 u3-l3 的 `AxiLiteMaster` 发起一次 AXI-Lite 事务，再把结果以 ASCII 回显。

另一件常事是「**多个主机抢一个慢外设引擎**」。比如系统里 CPU、监控核、SRP 远端都想访问同一个 I2C 设备，但 `I2cRegMaster` 只有一套。`I2cRegMasterMux` 把 2~8 路主机请求轮询复用到这一个引擎上，并提供 `lockReq` 让某个主机连续占有多笔事务（原子性），被锁期间其他主机的请求被立刻回错误码而不是死等。

#### 4.3.2 核心流程

**UART 主桥**（两段式结构）：

1. `UartWrapper`（PHY）：波特率发生 + TX/RX + 收发 FIFO，对外是字节级流式接口（`uartRxValid/Data/Ready`、`uartTxValid/Data/Ready`）。
2. `UartAxiLiteMasterFsm`：逐字节解析文本，按格式 `w|W ADDR DATA[\r|\n]` / `r|R ADDR[\r|\n]` 累积出 `AxiLiteReqType`（`address/wrData/rnw`），遇到行尾拉高 `request`；`AxiLiteMaster` 完成后回显地址、数据与 1 位响应码。

**主控复用器 I2cRegMasterMux**（轮询 + 锁）：

1. 每拍若未锁定，`sel := sel + 1` 轮询。
2. 选中路 `regIn(sel).regReq='1'` 时，把该路请求直通给唯一引擎，并把引擎的 `regOut` 回送给该路。
3. 若该路同时拉高 `lockReq`，则锁定 `sel` 不再轮询，让同一主机连续发起多笔；锁定期间其他路的请求立刻回 `regFail='1'/regFailCode=0x0F`。

#### 4.3.3 源码精读

**UART 主桥是纯结构顶层**：

[protocols/uart/rtl/UartAxiLiteMaster.vhd:66-110](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMaster.vhd#L66-L110) 例化 `UartWrapper`（PHY）与 `UartAxiLiteMasterFsm`，两者用字节流信号对接，FSM 再驱动 `mAxil*Master/Slave`。注意端口方向：`mAxilWriteMaster : out`、`mAxilWriteSlave : in`——**这是主口**，与前面所有桥相反。泛型 `BAUD_RATE_G/PARITY_G/DATA_WIDTH_G` 等都是串口本身的参数。

**UART 文本协议 FSM**：

协议格式写在注释 [UartAxiLiteMasterFsm.vhd:145-149](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L145-L149)。9 态状态机见 [L51-60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L51-L60)（`WAIT_START_S → SPACE_ADDR_S → ADDR_SPACE_S → WR_DATA_S/WAIT_EOL_S → AXIL_TXN_S → RD_DATA_S → DONE_S`）。它用 `hexToSlv`/`slvToHex`（[L87-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L87-L99)）在 ASCII 与十六进制间转换，每收一个 hex 字符就把 `address`/`wrData` 左移 4 位拼入。关键复用：它在 [L103-114](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L103-L114) 例化 u3-l3 的 `AxiLiteMaster`，把 `r.axilReq`（`AxiLiteReqType`）变成五通道握手。响应码在 `DONE_S` 回显：`uartTx(slvToHex(resize(axilAck.resp,4)))`（[L292-305](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L292-L305)）。

> 这里的 `AxiLiteReqType`/`AXI_LITE_REQ_INIT_C` 正是 u3-l3 介绍的 req/ack 接口，定义在 [AxiLitePkg.vhd:215-225](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L215-L225)。本桥是它的典型消费者。

**主控复用器**：

[protocols/i2c/rtl/I2cRegMasterMux.vhd:4-8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterMux.vhd#L4-L8) 头注释点明了「锁定以执行连续多笔事务」的设计意图。轮询逻辑在 [L71-75](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterMux.vhd#L71-L75)：未锁定时每拍 `sel+1`，锁定时跟随 `lockReq(selInt)`。选中路发请求时直通并按需授权锁定（[L81-86](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterMux.vhd#L81-L86)）。**锁冲突的快速失败**在 [L89-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterMux.vhd#L89-L99)：锁定期间任何非选中路的请求立刻收到 `regFail='1'/regFailCode="00001111"(0x0F)`，避免它们无限等待。这套 `regIn/regOut` 数组类型 `I2cRegMasterInArray/OutArray` 定义在 [I2cPkg.vhd:111-126](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cPkg.vhd#L111-L126)。

> 工程里常见的拓扑是：两个 `I2cRegMasterAxiBridge`（各自挂一段 AXI 窗口）+ 一个 `I2cRegMasterMux` + 一个 `I2cRegMaster`，让两个独立地址窗口共享同一条物理 I2C 总线。

#### 4.3.4 代码实践

**实践目标**：用终端模拟器手动走一遍 UART 主桥的文本协议，理解「一行 ASCII = 一次 AXI-Lite 事务」。

**操作步骤**：

1. 假设 `UartAxiLiteMaster` 已挂在某 SoC 的 AXI-Lite 交叉开关主口上，串口配置 `115200 8N1`。
2. 用 `minicom`/`picocom` 连上串口。
3. 发起一次写：键入 `w deadbeef 12345678` 然后回车。对照 [L146](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L146)，FSM 会先把 `deadbeef` 累积进 `axilReq.address`、`12345678` 进 `axilReq.wrData`，行尾后置 `rnw='0'/request='1'`。
4. 发起一次读：键入 `r deadbeef` 回车，预期回显形如 `r deadbeef <读出数据> <resp>`。

**需要观察的现象 / 预期结果**：

- 写命令会被原样回显（`uartTx(uartRxData)` 在每个接收态都回显），最后追加一个空格和 1 位响应码（`0`=OKAY）再加 CR。
- 读命令回显地址后，FSM 进入 `RD_DATA_S` 逐 nibble 打印 8 位十六进制数据（[L279-285](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L279-L285)）。
- 地址/数据大小写不敏感（`w|W`、`r|R` 都接受，见 [L163-174](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/uart/rtl/UartAxiLiteMasterFsm.vhd#L163-L174)）；行尾 CR 或 LF 都触发事务。

**待本地验证**：需在真实硬件或带 UART loopback 的仿真里验证回显内容；本实践重点是掌握协议格式与 FSM 的逐字符累积过程。

#### 4.3.5 小练习与答案

**练习 1**：UART 主桥和 I2C/SPI 从机桥，谁的 AXI 口是 `out`（主控）？为什么方向相反？
**答案**：UART 主桥的 `mAxil*Master` 是 `out`（主口），因为外部主机要**主动发起**对 FPGA 内部寄存器的访问；I2C/SPI 桥的 `axiReadMaster/WriteMaster` 是 `in`（从口），因为它们被动响应 CPU 的访问、再转给外设。

**练习 2**：`I2cRegMasterMux` 在锁定期间，被锁出去的主机收到什么？为什么这样设计？
**答案**：收到 `regAck='1'/regFail='1'/regFailCode=0x0F`（[L89-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/i2c/rtl/I2cRegMasterMux.vhd#L89-L99)）。立即失败好过无限等待——主机据此重试或上报，避免因一个主机长时间占用而导致其他主机在 AXI 侧长时间「张嘴等」。

**练习 3**：如果要给 SPI 也做一个类似 `I2cRegMasterMux` 的多主复用器，复用 `AxiSpiMaster` 的哪一层最合适？
**答案**：复用「req/ack 事务层」最合适。但 `AxiSpiMaster` 直接吃的是 AXI-Lite 从口、没有独立的请求记录层；要复用得像 I2C 那样把「AXI 桥」和「协议引擎」拆开（I2C 把 `I2cRegMasterAxiBridge` 与 `I2cRegMaster` 分离，中间用 `I2cRegMasterInType/OutType` 连）。SPI 目前是两者合一的，所以多主复用一般改用交叉开关分多个 `AxiSpiMaster` 各占一片选（见 [AxiSpiMaster.vhd:5-13](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/spi/rtl/AxiSpiMaster.vhd#L5-L13) 注释的建议）。

## 5. 综合实践

**任务**：为一个带「1 片 SPI ADC + 2 个 I2C 设备 + 1 个 SACI ASIC」的板卡，设计 AXI-Lite 地址空间分配与桥接拓扑，并写出每个外设寄存器的访问地址。

要求：

1. 用一个 `AxiLiteCrossbar`（u3-l3）做 1 主 3 从的地址解码，三个从口分别接 `AxiSpiMaster`、`AxiI2cRegMasterCore`、`AxiLiteSaciMaster`。
2. 给三个从口各分配一段地址窗口（建议各 1 MB，按 2 的幂对齐），写出 `AxiLiteCrossbarMasterConfigArray` 的 `baseAddr/addrBits`。
3. 对每个外设各举一个寄存器访问，算出 CPU 看到的 32 位 AXI 地址：
   - SPI：`ADDRESS_SIZE_G=12, DATA_SIZE_G=8, MODE_G="RW"`，读 ADC 寄存器 `0x2A`。
   - I2C：设备 0（7 位 `0x48`，8 位数据/8 位地址）、设备 1（7 位 `0x50`，8 位数据/16 位地址），读 EEPROM（设备 1）寄存器 `0x0010`。
   - SACI：`SACI_NUM_CHIPS_G=1`，读 cmd=`0x03`、addr=`0x0F` 的 ASIC 寄存器。
4. 说明若 SACI 的 `saciBusGr` 长期为 0，CPU 访问会得到什么响应、为什么 SACI 桥比 I2C/SPI 桥更「抗挂死」。

**检查清单**：

- 三个从口地址窗口不重叠、按 `addrBits` 对齐。
- SPI 读地址 = SPI 窗口基址 + `0x2A<<2`。
- I2C 读地址 = I2C 窗口基址 + 设备 1 基偏移（bit18=`0x40000`）+ `0x10<<2`。
- SACI 读地址 = SACI 窗口基址 + 把 cmd/addr 编进 `awaddr(20:14)/(13:2)` 的那个值（可参考 [AxiLiteSaciMaster.vhd:197-202](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/saci/saci1/rtl/AxiLiteSaciMaster.vhd#L197-L202) 反推）。
- SACI 在 `saciBusGr=0` 时回 `SLVERR`，因为它有超时/仲裁兜底；I2C/SPI 桥没有，依赖外设最终回 ack。

> 提示：这是纸面设计练习（待本地验证），重点是把「AXI 地址 = 窗口基址 + 桥内切片编码」这条链路在三种桥上各走一遍。

## 6. 本讲小结

- **外设寄存器桥**把慢速串行外设（I2C/SPI/SACI）包装成普通 AXI-Lite 内存窗口，CPU 一次 32 位访问 = 桥替你跑完整套串行协议。
- 三个**从机桥**共享统一套路：`axiSlaveWaitTxn` 解码 → 把 AXI 地址**切片成「设备/片选 + 命令 + 寄存器地址」**（最低 2 位恒为字对齐）→ 触发一次外设 req/ack → 用 `axiSlave*Response` 回 OKAY/SLVERR/DECERR。
- **I2C 桥**用 `DEVICE_MAP_G` 描述每设备个性，寄存器地址窗口按最大设备的 `addrSize` 统一开；多设备靠地址高位 `log2(N)` 位选设备。
- **SPI/SACI 桥**把 AXI 地址当结构化命令字复用；SPI 有 `MODE_G` 方向约束与影子 RAM，SACI 多了总线仲裁与超时双保险，最抗挂死。
- **UART 主桥是反方向**：外部主机经 UART 文本协议（`w/r ADDR DATA`）驱动 FPGA 内部 AXI-Lite 主口，复用 u3-l3 的 `AxiLiteMaster`。
- **`I2cRegMasterMux`** 用轮询 + `lockReq` 把多主机复用到一个慢引擎，锁定时其他路立即回 `0x0F` 错误码而非死等。

## 7. 下一步学习建议

- 想看「远端经链路访问这些桥」的完整链路：结合 **u5-l3（SRPv3）** 与本讲的 `AXIL_PROXY_G`（`AxiLiteMasterProxy`），理解 CPU 如何经 PGP/以太网触达 I2C/SPI 寄存器。
- 想理解这些桥如何接入系统拓扑：回看 **u3-l3（AxiLiteCrossbar）**，把本讲的三个从机桥当成交叉开关的从口练习地址解码。
- 想做软件侧镜像：进 **u9-l4（PyRogue 设备模型）**，对照这里的地址布局，用 `RemoteVariable` 把每个外设寄存器暴露给 Python。
- 继续外设协议族：可阅读 `protocols/i2c/rtl/I2cMaster.vhd`、`protocols/spi/rtl/SpiMaster.vhd`、`protocols/saci/saci1/rtl/SaciMaster2.vhd` 这些 bit 级核，看桥下层的字节/位移时序是如何实现的。
