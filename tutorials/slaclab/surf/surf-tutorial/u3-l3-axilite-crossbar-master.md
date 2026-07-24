# AXI-Lite 交叉开关、主机与异步跨域

## 1. 本讲目标

学完本讲后，你应当能够：

- 读懂 `AxiLiteCrossbar` 的**地址解码**与**多主多从路由**机制，并能用 `AxiLiteCrossbarMasterConfigArray` 描述一组地址窗口。
- 说出 `AxiLiteMaster` 如何把一次 `REQ/ACK` 请求翻译成 AXI-Lite 的 5 个通道握手，并跟踪读/写事务的状态流转。
- 解释 `AxiLiteAsync` 为什么用**每通道一个异步 FIFO** 来跨时钟域，以及它在复位时如何用「强制 ready / 强制 valid+错误响应」避免死锁。
- 独立配置一个「1 主 3 从」的交叉开关，并计算每个从机的基地址与窗口大小。

## 2. 前置知识

本讲直接建立在两篇已完成的讲义之上，请确认你已经掌握：

- **u3-l1 AXI-Lite 记录类型与包**：AXI-Lite 的 5 个通道（AR/R/AW/W/B）被切成「读/写 × 主/从」4 个记录（`AxiLiteReadMasterType` 等），握手口诀是「VALID 与数据归生产方、READY 归消费方」，响应码有 `AXI_RESP_OK_C/EXOKAY_C/SLVERR_C/DECERR_C`，每个记录都有 `_INIT_C` 初值常量。本讲会大量复用这些记录。
- **u2-l2 FIFO 构建块**：异步 FIFO 用 Gray 码指针配合同步器跨时钟域，相邻指针仅差 1 比特，所以同步延迟只会让对端指针「滞后」而不会错乱，从而保证「写侧偏满、读侧偏空」的单向保守安全。`AxiLiteAsync` 的跨域正是建立在这个机制之上。
- **u1-l5 双进程 RTL 风格**：`RegType` 记录 + `REG_INIT_C` 初值 + `r/rin` + `comb`（算次态 `v`）+ `seq`（打寄存器）。本讲的 `AxiLiteCrossbar`、`AxiLiteMaster` 都沿用这套骨架。

一个容易混淆的点先在这里说清：**交叉开关的「slave slot」接的是外部 AXI 主机，「master slot」接的是外部 AXI 从机**。因为交叉开关对 CPU 来说扮演「从机」，对下游外设来说扮演「主机」。所以本讲标题里的「1 主 3 从」对应参数是 `NUM_SLAVE_SLOTS_G=1`（1 个 CPU 主机）、`NUM_MASTER_SLOTS_G=3`（3 个外设从机）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [axi/axi-lite/rtl/AxiLitePkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd) | 定义交叉开关配置类型 `AxiLiteCrossbarMasterConfigType`、默认配置、初值函数 `axiReadMasterInit/axiWriteMasterInit`、以及自动生成地址表的 `genAxiLiteConfig`。 |
| [axi/axi-lite/rtl/AxiLiteCrossbar.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd) | N 主 M 从的交叉开关：地址解码 + 多 slave 仲裁 + 解码失败回 DECERR。 |
| [axi/axi-lite/rtl/AxiLiteMaster.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteMaster.vhd) | 把单次 `REQ/ACK` 请求翻译成一次 AXI-Lite 读或写事务的状态机。 |
| [axi/axi-lite/rtl/AxiLiteAsync.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd) | AXI-Lite 跨时钟域桥：每通道一个 `FifoASync`，外加双向复位同步与防御性握手。 |
| axi/axi-lite/tb/AxiLiteCrossbarTb.vhd | 交叉开关的测试台外壳：1 个 CPU 口扇出 2 个从机，其中 1 个从机再级联二级交叉开关。 |
| tests/axi/axi_lite/test_AxiLiteCrossbar.py | 上面测试台的 cocotb 回归测试，覆盖正常路由、DECERR、并发压力。 |

---

## 4. 核心概念与源码讲解

### 4.1 交叉开关地址解码（AxiLiteCrossbar）

#### 4.1.1 概念说明

当一片 FPGA 里只有一个 CPU、却有几十个外设寄存器块时，不可能给每个外设单独拉一组 AXI 总线。常见的做法是让所有外设共享一条 AXI-Lite 总线，再用一个**地址解码器**把访问按地址分发到对应外设。`AxiLiteCrossbar` 就是 SURF 的通用地址解码器，而且它做成了一张**交叉开关（crossbar）**：

- **输入侧（slave slot）**：可以有多个 CPU/主机（`NUM_SLAVE_SLOTS_G`，最多 16）同时发起访问。
- **输出侧（master slot）**：可以有多个外设/从机（`NUM_MASTER_SLOTS_G`，最多 64）被访问。
- **路由规则**：每个 master slot 对应一段**地址窗口**；交叉开关根据事务地址命中哪个窗口，把请求转发到那个外设。
- **冲突处理**：当多个主机同时访问同一个外设时，交叉开关在输出侧做**轮询仲裁**，串行化这些请求。

这样，任意主机可以访问任意从机，构成「多对多」的连通图，故称交叉开关。

#### 4.1.2 核心流程

地址窗口用 `AxiLiteCrossbarMasterConfigType` 描述，三个字段：

- `baseAddr`：窗口起始地址（低 `addrBits` 位必须为 0，即按窗口大小对齐）。
- `addrBits`：窗口大小为 \( 2^{\text{addrBits}} \) 字节；解码时只比较地址的高位。
- `connectivity`：16 位掩码，第 `s` 位为 `'1'` 表示 slave slot `s` 被允许路由到这个 master。

一条事务的生命周期可以拆成两段状态机：

1. **slave 侧状态机（每个 slave slot 一份，读写各一套）**：在 `S_WAIT_AXI_TXN_S` 等待握手；收到地址后，用 `StdMatch` 把地址高位与每个窗口的 `baseAddr` 高位比较，命中则置 `wrReqs(m)/rdReqs(m)` 向 master `m` 发请求；无人命中则进入 `S_DEC_ERR_S`，用 `DEC_ERROR_RESP_G`（默认 `AXI_RESP_DECERR_C`）回错。被目标 master 仲裁选中（`master(m).wrAcks(s)='1'`）后进入 `S_TXN_S`，把下游外设的响应原样转回主机。
2. **master 侧状态机（每个 master slot 一份，读写各一套）**：先把所有 slave 对自己的请求收集成 `mWrReqs/mRdReqs`，用 `ArbiterPkg.arbitrate` 在多个同时请求之间轮询选出一个 `ackNum`，然后把被选中 slave 的整条总线**直连**到这个 master 的输出；等下游外设回完响应、slave 侧撤销请求后，回到 `M_WAIT_REQ_S` 等下一次。

伪代码（写通道，单 slave slot `s`、单 master `m`）：

```
slave.wrState = S_WAIT_AXI_TXN_S:
    if awvalid & wvalid:
        for each master m:  if StdMatch(awaddr[31:addrBits_m], baseAddr_m[31:addrBits_m]) & conn_m[s]:
            wrReqs[m] = 1; wrReqNum = m
        if 没有任何 wrReqs:  -> S_DEC_ERR_S   (回 DECERR)
        else:                 -> S_ACK_S

master.wrState = M_WAIT_REQ_S:
    arbitrate(mWrReqs, prevAckNum) -> ackNum, wrAcks, wrValid
    if wrValid:
        mAxiWriteMaster(m) = sAxiWriteMaster(ackNum)   # 总线直连
        -> M_WAIT_READYS_S    # 等下游 awready/wready
```

#### 4.1.3 源码精读

**地址窗口配置类型与默认值**——三个字段定义在包里，默认配置给出 4 个 64KB 窗口：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L242-L262](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L242-L262) 定义 `AxiLiteCrossbarMasterConfigType`（`baseAddr/addrBits/connectivity`）和 `AXIL_XBAR_CFG_DEFAULT_C`，后者给出 4 个窗口：`0x0000_0000`、`0x0001_0000`、`0x0002_0000`、`0x0003_0000`，每个 `addrBits=16`（64KB）。

**窗口上界与合法性检查**——交叉开关里有一个本地函数把窗口上界算出来，配合三段 `assert` 在综合/仿真时拦截错误配置：

[axi/axi-lite/rtl/AxiLiteCrossbar.vhd:L56-L64](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd#L56-L64) `getHighAddr` 把 `baseAddr` 的低 `addrBits` 位全部置 1，得到窗口上界 `baseAddr + 2^addrBits - 1`。

[axi/axi-lite/rtl/AxiLiteCrossbar.vhd:L141-L164](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd#L141-L164) 两段断言：`noneZeroCheck` 要求 `baseAddr` 在低 `addrBits` 位内必须为 0（即窗口对齐）；`gen_assert_master_config` 用 `getHighAddr` 检查任意两个窗口不重叠。配置写错时会在 elaboration 阶段直接 `severity failure` 报错并打印出冲突的两个窗口。

**地址解码**——核心是 `StdMatch` 比较，允许配置里写 `'-'`（don't care）：

[axi/axi-lite/rtl/AxiLiteCrossbar.vhd:L212-L237](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd#L212-L237) 写通道解码：当 `awvalid & wvalid` 时，遍历每个 master，用 `StdMatch(awaddr(31 downto addrBits), baseAddr(31 downto addrBits))` 比较高位，同时要求 `connectivity(s)='1'`。命中则置请求位 `wrReqs(m)`。若 `uOr(wrReqs)='0'`（无人命中），把 `awready/wready` 拉高收掉这笔错地址事务，进入 `S_DEC_ERR_S`。读通道解码逻辑完全对称，见 [L287-L309](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd#L287-L309)。

> 解读：`addrBits` 同时承担两个角色——它既是「窗口大小」\( 2^{\text{addrBits}} \)，又是「解码时忽略的低位宽度」。例如 `addrBits=16` 表示窗口 64KB，比较时丢掉低 16 位，等价于「高 16 位相等就算命中」。

**解码失败响应**：

[axi/axi-lite/rtl/AxiLiteCrossbar.vhd:L240-L249](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd#L240-L249) `S_DEC_ERR_S` 用 `DEC_ERROR_RESP_G`（默认 `AXI_RESP_DECERR_C`="11"）回 `bresp`，并等主机 `bready` 收走后回空闲。注意 `DEC_ERROR_RESP_G` 是个泛型，可被覆盖（例如想让解码失败表现为 SLVERR）。

**master 侧仲裁与总线直连**：

[axi/axi-lite/rtl/AxiLiteCrossbar.vhd:L367-L385](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd#L367-L385) `M_WAIT_REQ_S`：先复位输出为 `axiWriteMasterInit(config)`（安全静止态），再用 `arbitrate(mWrReqs, r.master(m).wrAckNum, ...)` 在多个 slave 请求间轮询，得到 `wrAckNum` 和 `wrAcks`。`wrValid='1'` 后，下一拍直接 `v.mAxiWriteMasters(m) := sAxiWriteMasters(conv_integer(r.master(m).wrAckNum))`——**把选中 slave 的整条 master 记录原样搬到输出**，这就是「总线直连」。

**防止高位地址被改写**：因为 master 输出是从 slave 总线搬来的，而 slave 给的地址低位可能落在窗口内任意位置，但**高位必须始终等于该窗口的 `baseAddr` 高位**（否则就路由到别处去了）。代码用一小段赋值把高位钉死，帮助综合器优化：

[axi/axi-lite/rtl/AxiLiteCrossbar.vhd:L419-L422](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd#L419-L422) 每拍都把 `awaddr(31 downto addrBits)` 强制写成 `baseAddr(31 downto addrBits)`。注释明确说「逻辑上不会发生，但 Vivado 推断不出来，显式写有助于优化」。

#### 4.1.4 代码实践：配置一个「1 主 3 从」交叉开关

**实践目标**：用 `AxiLiteCrossbarMasterConfigArray` 手写 3 段不重叠的地址窗口，并算出每个从机的基地址与大小；再与 `genAxiLiteConfig` 的一行生成结果对照。

**操作步骤**：

1. 新建一个练习用顶层（**示例代码**，不要写进 SURF 源码树），声明如下配置：

   ```vhdl
   -- 示例代码：1 个 CPU 主机 (NUM_SLAVE_SLOTS_G=1) 访问 3 个外设从机
   constant NUM_SLAVES_C : positive := 3;

   -- 手写三段窗口：每段 64KB (addrBits=16)，基地址分别是 0x0000_0000 / 0x0001_0000 / 0x0002_0000
   constant XBAR_CFG_C : AxiLiteCrossbarMasterConfigArray(NUM_SLAVES_C-1 downto 0) := (
      0 => (baseAddr => x"0000_0000", addrBits => 16, connectivity => x"FFFF"),
      1 => (baseAddr => x"0001_0000", addrBits => 16, connectivity => x"FFFF"),
      2 => (baseAddr => x"0002_0000", addrBits => 16, connectivity => x"FFFF"));

   U_XBAR : entity surf.AxiLiteCrossbar
      generic map (
         NUM_SLAVE_SLOTS_G  => 1,                 -- 1 个 CPU 主机
         NUM_MASTER_SLOTS_G => NUM_SLAVES_C,      -- 3 个外设从机
         MASTERS_CONFIG_G   => XBAR_CFG_C)
      port map ( ... );
   ```

2. 上面手写的三段窗口，等价于包函数的一行生成（`baseBot=18, addrBits=16` 表示用 bit[17:16] 这 2 位编码从机编号，可编码 0..3，够 3 个用）：

   ```vhdl
   constant XBAR_CFG_C : AxiLiteCrossbarMasterConfigArray(NUM_SLAVES_C-1 downto 0) :=
      genAxiLiteConfig(NUM_SLAVES_C, x"0000_0000", 18, 16);
   ```

   `genAxiLiteConfig` 的实现见 [axi/axi-lite/rtl/AxiLitePkg.vhd:L1026-L1075](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L1026-L1075)：它把 `addr(baseBot-1 downto addrBits)` 设为从机编号 `i`，从而均匀切分地址空间。

**需要观察/计算的现象**：填写下表（窗口大小 \( =2^{\text{addrBits}} \)，上界 \( =\text{baseAddr}+2^{\text{addrBits}}-1 \)，即 `getHighAddr`）：

| master slot | baseAddr | addrBits | 窗口大小 | 上界（getHighAddr） | 覆盖地址范围 |
| --- | --- | --- | --- | --- | --- |
| 0 | 0x0000_0000 | 16 | 64 KB | 0x0000_FFFF | 0x0000_0000 – 0x0000_FFFF |
| 1 | 0x0001_0000 | 16 | 64 KB | 0x0001_FFFF | 0x0001_0000 – 0x0001_FFFF |
| 2 | 0x0002_0000 | 16 | 64 KB | 0x0002_FFFF | 0x0002_0000 – 0x0002_FFFF |

**预期结果**：

- 三段窗口互不重叠，`gen_assert_master_config` 不会报错。
- 访问 `0x0000_1234` 命中 slot 0；访问 `0x0001_8000` 命中 slot 1；访问 `0x0002_0000` 命中 slot 2。
- 访问未映射的 `0x0010_0000` 会被 `S_DEC_ERR_S` 收掉，主机收到 `bresp/rresp = "11"`（DECERR）。
- 想直接验证路由与 DECERR 行为，可运行仓库自带的 cocotb 回归（见综合实践），它正是用 `genAxiLiteConfig(2, x"0000_0000", 22, 20)` 配置顶层的 [AxiLiteCrossbarTb.vhd:L54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/tb/AxiLiteCrossbarTb.vhd#L54)，并在 `bad_addr = 0x0020_0000` 上断言 `AxiResp.DECERR`。

> 若你不方便跑仿真，可只做「源码阅读型实践」：对照上面的表，逐窗口确认 `StdMatch(awaddr(31 downto 16), baseAddr(31 downto 16))` 在这些地址上的真假，标注「待本地验证」即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面 slot 1 的 `baseAddr` 误写成 `0x0001_8000`（仍 `addrBits=16`），综合时会发生什么？
**答案**：`noneZeroCheck` 会触发，因为它要求 `baseAddr(addrBits-1 downto 0) = 0`，而 `0x0001_8000` 的低 16 位是 `0x8000 ≠ 0`。断言以 `severity failure` 报错，指出该 slot 的 baseAddr 在 addrBits 范围内必须为零。

**练习 2**：`connectivity => x"0001"` 表示什么？为什么二级交叉开关（只有 1 个 slave slot）可以这样写？
**答案**：`connectivity` 第 `s` 位为 `'1'` 才允许 slave slot `s` 路由到该 master。`x"0001"` 只有 bit0 为 1，表示只允许 slave slot 0 访问。二级交叉开关本身只有 1 个 slave slot（编号必为 0），所以写 `x"0001"` 等价于「全部允许」。该用法见 [AxiLiteCrossbarTb.vhd:L58-L66](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/tb/AxiLiteCrossbarTb.vhd#L58-L66)。

**练习 3**：`uOr(v.slave(s).wrReqs) = '0'` 这个判断在什么情况下为真？
**答案**：当本次写事务的地址没有命中**任何**一个 master 窗口（或命中的窗口 `connectivity(s)='0'` 不许这个 slave 访问）时，所有 `wrReqs` 位都为 0，`uOr` 结果为 `'0'`，于是进入 `S_DEC_ERR_S` 回 DECERR。

---

### 4.2 主机事务状态机（AxiLiteMaster）

#### 4.2.1 概念说明

很多 SURF 模块（比如 `I2cRegMaster`、`UartAxiLiteMaster`、各种 SRP 适配器）本身需要**主动发起**一次 AXI-Lite 读或写，而不是被动响应。但直接操作 AXI 的 5 个通道（管 awvalid/wvalid/bready/arvalid/rready 谁先谁后）很繁琐。`AxiLiteMaster` 提供了一个极简的 **`REQ/ACK` 接口**，把「发一次读/写」封装成一次请求-应答：

- `req : AxiLiteReqType`（输入）：`request`（发起）、`rnw`（1=读，0=写）、`address`、`wrData`。
- `ack : AxiLiteAckType`（输出）：`done`（事务完成）、`resp`（响应码）、`rdData`（读回的数据）。

调用方只要：拉高 `req.request`、给出地址/方向/数据，然后等 `ack.done` 变 1，读 `ack.resp/rdData` 即可。模块内部用一个 5 状态机把这一拍请求展开成完整的 AXI-Lite 握手时序。

#### 4.2.2 核心流程

状态机定义见 [axi/axi-lite/rtl/AxiLiteMaster.vhd:L42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteMaster.vhd#L42)：`S_IDLE_C → S_WRITE_C/S_READ_C → S_WRITE_AXI_C/S_READ_AXI_C → S_IDLE_C`。

```
S_IDLE_C:  等到 req.request=1 且 ack.done=0
           req.rnw=1 -> S_READ_C ; req.rnw=0 -> S_WRITE_C
S_WRITE_C: 准备写：awaddr/wdata/wstrb=全1、awvalid=wvalid=bready=1 -> S_WRITE_AXI_C
S_WRITE_AXI_C: awready 到则撤 awvalid；wready 到则撤 wvalid；
               bvalid 到则撤 bready、ack.done=1、ack.resp=bresp -> S_IDLE_C
S_READ_C:  准备读：araddr、arvalid=rready=1 -> S_READ_AXI_C
S_READ_AXI_C: arready 到则撤 arvalid；rvalid 到则撤 rready、读 rdata/rresp；
              arvalid 与 rready 都撤后 ack.done=1 -> S_IDLE_C
```

注意读写都是**「先拉 VALID，等 READY 再撤 VALID，最后收响应」**的标准 AXI 节拍，只是写多了 AW/W 两个通道。

#### 4.2.3 源码精读

**IDLE 与方向选择**：

[axi/axi-lite/rtl/AxiLiteMaster.vhd:L75-L90](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteMaster.vhd#L75-L90) 空闲时把 master 记录维持成 `AXI_LITE_WRITE/READ_MASTER_INIT_C`（即所有 valid=0、被动 ready=1）。`req.request='1' and r.ack.done='0'` 时，按 `req.rnw` 分流到读或写。`req.request='0'` 时把 `ack` 清成初值——这是让调用方「看到」上一笔已结束的方式。

**写事务准备**：

[axi/axi-lite/rtl/AxiLiteMaster.vhd:L93-L102](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteMaster.vhd#L93-L102) 一次性把 `awaddr/awprot=0/wstrb=全1/wdata` 装好，并拉高 `awvalid/wvalid/bready`。`wstrb=全1` 表示整字写——这正好避开 u3-l1 提到的「AXI-Lite 用 WSTRB 会被回 SLVERR」的雷区。

**写事务收尾**：

[axi/axi-lite/rtl/AxiLiteMaster.vhd:L105-L118](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteMaster.vhd#L105-L118) 分别在 `awready/wready` 到来时撤销对应 valid（AXI 规定 VALID 在 READY 出现前不能自撤）；`bvalid` 到来时撤销 `bready`，把 `bresp` 存进 `ack.resp`，置 `ack.done`，回 IDLE。

**读事务**：

[axi/axi-lite/rtl/AxiLiteMaster.vhd:L121-L146](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteMaster.vhd#L121-L146) 读路径对称：`arvalid/rready` 拉高 → `arready` 到撤 `arvalid` → `rvalid` 到撤 `rready` 并锁存 `rdata/rresp`。判定完成的条件是 `arvalid='0' and rready='0'`（请求已发完且响应已收完）。

**REQ/ACK 记录定义**：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L215-L237](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L215-L237) `AxiLiteReqType`（request/rnw/address/wrData）与 `AxiLiteAckType`（done/resp/rdData），均带 `_INIT_C` 初值。

#### 4.2.4 代码实践：跟踪一次写事务的握手时序

**实践目标**：在纸上（或用仿真波形）画出 `AxiLiteMaster` 一次写事务各通道 VALID/READY 的时序，理解「VALID 等 READY」节拍。

**操作步骤**：

1. 假设调用方在某拍置 `req.request='1', req.rnw='0', req.address=0x0001_0004, req.wrData=0xDEADBEEF`。
2. 从 `S_IDLE_C` 出发，逐拍标注 `state`、`awvalid`、`wvalid`、`bready`，以及外部从机回的 `awready`、`wready`、`bvalid`、`bresp`、`ack.done`。
3. 分两种情况画：(a) 从机第一拍就同时回 `awready=wready=1`；(b) 从机先回 `awready=1`、隔一拍才回 `wready=1`。

**需要观察的现象**：

- `awvalid` 与 `wvalid` 在同一拍（`S_WRITE_C` 末）被拉高，二者必须都等到各自 READY 才分别撤销。
- `ack.done` 只在 `bvalid='1'` 那拍置 1，`ack.resp` 锁存当时的 `bresp`。
- 情况 (b) 中，`awvalid` 比 `wvalid` 早一拍撤销——这正是「分别按各自 READY 撤销」的体现。

**预期结果**：时序图应显示 `S_IDLE → S_WRITE(1 拍) → S_WRITE_AXI(若干拍) → S_IDLE`，且 `bready` 在 `bvalid` 到来那拍之后立即撤销。若你用 cocotb 跑 [tests/axi/axi_lite/test_AxiLiteMaster.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_lite/test_AxiLiteMaster.py) 的回归，可在波形里直接对照。结果若与上述不符，以本地仿真波形为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `S_WRITE_C` 里要把 `wstrb` 设成全 `'1'`？
**答案**：AXI-Lite 规定凡使用 WSTRB（任意一位为 0）的访问都可能被从机回 SLVERR（见 u3-l1 的响应码说明）。`AxiLiteMaster` 走整字写，故 `wstrb=全1` 以避免误触发 SLVERR。

**练习 2**：读事务为什么用 `arvalid='0' and rready='0'` 作为完成判据，而不是只看 `rvalid`？
**答案**：`rvalid='1'` 那拍只是「数据到了」，主机还要在同一拍用 `rready='1'` 把它收走，并在下一拍把 `rready` 撤掉。所以必须等到「请求已发完（arvalid 撤）」且「响应已收完（rready 撤）」两个条件都成立，才算整笔事务干净结束，才能置 `ack.done`。

**练习 3**：调用方如何知道可以发下一笔请求？
**答案**：看 `ack.done`。`done='1'` 表示上一笔已完成，调用方可以撤销 `req.request`（模块会在 `request='0'` 时把 `ack` 清回初值），再发起新请求。在 `done='1'` 期间重复置 `request='1'` 不会被受理（因为 IDLE 里要求 `r.ack.done='0'`）。

---

### 4.3 异步桥跨域（AxiLiteAsync）

#### 4.3.1 概念说明

AXI-Lite 的 5 个通道里，数据是多比特的（地址 32 位、数据 32 位、响应 2 位）。回忆 u2-l1 的铁律：**多比特信号不能直接用同步器跨时钟域**，因为各比特走线延迟不同，同步后可能采到乱码中间值。安全的做法只有一种——**握手跨域**：用异步 FIFO 把每个通道整笔搬运过去。

`AxiLiteAsync` 就是这么做的：它给 5 个通道各配一个 `FifoASync`（来自 u2-l2），写侧在自己的时钟域把整笔通道数据打入 FIFO，读侧在另一个时钟域取出来，从而每个通道都安全跨域。它还解决了两个衍生问题：

1. **复位也要跨域**：两边的复位彼此异步，必须各自用 `RstSync` 同步到对方时钟域。
2. **跨域途中复位不能死锁**：如果对端域正在复位、FIFO 不工作，本端域的主机却还在等响应，就会永远挂起。桥必须主动给一个「错误响应」把这笔事务收掉。

#### 4.3.2 核心流程

模块用 `COMMON_CLK_G` 二选一：

- `COMMON_CLK_G=true`：两边同频同相，直接连线穿透（`mAxiReadMaster <= sAxiReadMaster` 等）。
- `COMMON_CLK_G=false`：异步模式，结构如下

```
            sAxiClk 域                          mAxiClk 域
AR 通道:  araddr/arprot/arvalid ──FifoASync──> araddr/arprot/arvalid   (Slave->Master)
R  通道:  rresp/rdata/rvalid     <──FifoASync── rresp/rdata/rvalid     (Master->Slave)
AW 通道:  awaddr/awprot/awvalid ──FifoASync──> awaddr/awprot/awvalid   (Slave->Master)
W  通道:  wdata/wstrb/wvalid     ──FifoASync──> wdata/wstrb/wvalid     (Slave->Master)
B  通道:  bresp/bvalid           <──FifoASync── bresp/bvalid           (Master->Slave)

复位: sAxiClkRst --RstSync(mAxiClk)--> s2mRst   (给 Slave->Master FIFO 用)
      mAxiClkRst --RstSync(sAxiClk)--> m2sRst   (给 Master->Slave FIFO 用，且驱动防御逻辑)
```

READY 信号不进 FIFO，而是在对端域就地生成（`arready = not full`、`rready = not full`），因为「FIFO 没满」天然就是一个跨域安全的流控信号。每个 FIFO 都开 FWFT（首字直通）、用分布式 RAM、`SYNC_STAGES_G=3`（3 级指针同步）。

#### 4.3.3 源码精读

**同频直通**：

[axi/axi-lite/rtl/AxiLiteAsync.vhd:L94-L101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L94-L101) `GEN_SYNC`：`COMMON_CLK_G=true` 时 4 条记录直接对穿，省掉全部 FIFO。

**复位双向同步**：

[axi/axi-lite/rtl/AxiLiteAsync.vhd:L106-L126](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L106-L126) 两个 `RstSync`：把 slave 域复位同步到 master 域得 `s2mRst`，把 master 域复位同步到 slave 域得 `m2sRst`。这两个本地复位分别喂给写时钟在对应域的 FIFO。

**AR 通道跨域（Slave→Master）**：

[axi/axi-lite/rtl/AxiLiteAsync.vhd:L133-L189](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L133-L189) `U_ReadSlaveToMastFifo` 把 `arprot(3b)+araddr(Nb)` 打包成 `NUM_ADDR_BITS_G+3` 位跨域。写使能 `readSlaveToMastWrite = arvalid and (not full)`；slave 侧 `arready` 就地由 `not full` 生成；master 侧 `arvalid` 由 FIFO 的 FWFT `valid` 驱动，`arready` 回连 FIFO 读使能。这是一个教科书式的「ready=not full、valid=FWFT」异步 FIFO 握手。

**防御性 ready（对端复位时不卡死请求通道）**：

[axi/axi-lite/rtl/AxiLiteAsync.vhd:L175-L176](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L175-L176) `sAxiReadSlave.arready <= ite(m2sRst='0', not readSlaveToMastFull, '1')`。当 master 域在复位（`m2sRst='1'`）时，slave 侧强制 `arready='1'`，把主机发来的读地址「吞掉」，避免主机死等。

**防御性 valid+错误响应（对端复位时给主机一个错误回应）**：

[axi/axi-lite/rtl/AxiLiteAsync.vhd:L242-L247](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L242-L247) R 通道返回侧：`m2sRst='1'` 时，`sAxiReadSlave.rresp` 强制为 `AXI_ERROR_RESP_G`（默认 SLVERR），`rvalid` 强制为 `'1'`。于是即便对端域瘫痪，本端主机也能立刻收到一个 SLVERR 响应并把事务结束掉，而不是无限期挂起。写通道 B 响应有同样处理，见 [L420-L423](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L420-L423)。`AXI_ERROR_RESP_G` 这个泛型默认 `AXI_RESP_SLVERR_C`，可按需改成 DECERR。

**W/AW/B 通道**：与 AR/R 完全同构，只是位宽不同（AW 同 AR 为 `NUM_ADDR_BITS_G+3`；W 为 36 位 = 32 数据 + 4 wstrb；B 为 2 位 bresp），分别在 [L254-L310](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L254-L310)、[L317-L368](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L317-L368)、[L375-L424](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteAsync.vhd#L375-L424)。

> 设计要点：READY 从不跨 FIFO，而是在「目标侧」就地由 `not full`（或防御性强制值）产生。这样流控天然安全——FIFO 满就回压、空就等待，无需对 READY 本身做跨域同步。

#### 4.3.4 代码实践：跟踪一次跨域读的往返路径

**实践目标**：在一张图上标出一次跨域读请求与读响应各经过哪个 FIFO、走哪个方向，并指出 `ready` 信号在哪里就地生成。

**操作步骤**：

1. 设 slave 域主机发起读：`sAxiReadMaster.arvalid/araddr`。
2. 按 4.3.2 的框图，依次标注：
   - 请求地址 `araddr/arprot` 经 `U_ReadSlaveToMastFifo`（Slave→Master）到达 master 域的 `mAxiReadMaster.arvalid/araddr`。
   - master 域外设回 `mAxiReadSlave.rdata/rresp/rvalid`。
   - 响应 `rdata/rresp` 经 `U_ReadMastToSlaveFifo`（Master→Slave）回到 slave 域的 `sAxiReadSlave.rdata/rresp/rvalid`。
3. 在图上标出 `sAxiReadSlave.arready` 与 `mAxiReadMaster.rready` 分别由哪个 FIFO 的 `full` 信号生成。

**需要观察的现象**：

- 一次读要走**两个** FIFO：去程一个（AR）、回程一个（R）。写则要走**三个**：去程 AW、W 两个，回程 B 一个。
- 每个 FIFO 的 `rd_clk/wr_clk` 分别接对应的时钟域；`rst` 接的是「写时钟域」那份同步后复位（`s2mRst` 或 `m2sRst`）。
- 若把 master 域复位拉高（`m2sRst='1'`），slave 域主机的读会立即收到 `rresp=SLVERR` 而非挂起。

**预期结果**：你画出的路径应与 4.3.2 框图一致。若想用仿真确认「跨域复位收到 SLVERR」这一行为，可参考 [tests/axi/axi_lite/test_AxiLiteAsync.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_lite/test_AxiLiteAsync.py) 的回归（在双时钟域下驱动 AXI-Lite 事务）。具体波形以本地仿真为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `COMMON_CLK_G=true` 时不实例化任何 FIFO？
**答案**：同频同相时两边时钟边沿对齐，没有跨域问题，5 个通道可以直接连线穿透（`GEN_SYNC` 分支）。开 FIFO 反而会白白增加延迟和资源。

**练习 2**：READY 信号为什么不用 FIFO 跨域？
**答案**：READY 是消费方的流控，把它就地由「FIFO 没满」（`not full`）生成即可。FIFO 内部的满标志是用 Gray 指针跨域同步得到的（u2-l2），本身就是安全的保守信号；再额外同步 READY 反而多一层延迟且容易出错。

**练习 3**：把 `AXI_ERROR_RESP_G` 从默认的 SLVERR 改成 DECERR，会改变什么行为？
**答案**：仅改变「对端域复位时，本端给主机的兜底响应码」——从 SLVERR（"10"）变成 DECERR（"11"）。正常跨域事务不受影响。这让系统设计者可以按需要把「跨域桥对端掉线」归类为从机错误或解码错误。

---

## 5. 综合实践：搭一个「CPU → 交叉开关 → 3 个外设」的小系统

把本讲三个模块串起来。**目标**：用一张顶层框图把以下三件事连成一条完整的数据通路，并对每个环节给出可验证的预期。

1. **地址映射**：用 4.1.4 的「1 主 3 从」配置实例化一个 `AxiLiteCrossbar`（`NUM_SLAVE_SLOTS_G=1, NUM_MASTER_SLOTS_G=3`）。三个 master slot 分别接一个 `AxiDualPortRam`（充当外设寄存器/RAM，u3 系列后续会专门讲）。
2. **主机驱动**：在 slave slot 0 上挂一个 `AxiLiteMaster`，用它的 `REQ/ACK` 接口发起一次写（地址 `0x0001_0010`、数据 `0x1234_5678`）和一次读（地址 `0x0002_0020`）。
3. **跨域**：把其中一个外设放到另一个时钟域，在交叉开关的 master slot 2 与该外设之间插入一个 `AxiLiteAsync`（`COMMON_CLK_G=false`）。

**操作步骤**：

1. 画框图，标出每个 `AxiLiteCrossbar`/`AxiLiteMaster`/`AxiLiteAsync`/`AxiDualPortRam` 的时钟域归属。
2. 对第 2 步的写事务，口头跟踪：`AxiLiteMaster` 发出 `awaddr=0x0001_0010` → 交叉开关 slave 侧解码命中 slot 1（`StdMatch` 比较高 16 位 `0x0001`）→ master 侧仲裁选中、总线直连 → 写入 slot 1 的 RAM → `bresp=OKAY` 经交叉开关转回 → `AxiLiteMaster` 置 `ack.done`。
3. 对第 3 步的读，额外跟踪 `araddr` 如何经 `AxiLiteAsync` 的 AR-FIFO 进入外设时钟域、`rdata` 如何经 R-FIFO 回来。
4. （可选）跑现成回归：仓库已经提供了一个「1 主扇出 2 从、其中 1 从级联二级交叉开关」的测试台 [AxiLiteCrossbarTb.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/tb/AxiLiteCrossbarTb.vhd) 和 cocotb 测试 [test_AxiLiteCrossbar.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_lite/test_AxiLiteCrossbar.py)。在仓库根目录按 u1-l2 / u9-l1 描述的工具链执行一次子系统回归（例如 `./.venv/bin/python -m pytest -q tests/axi/axi_lite/test_AxiLiteCrossbar.py`），观察三个窗口的回环、`0x0020_0000` 的 DECERR、以及并发压力是否全绿。

**预期结果**：

- 写 `0x0001_0010` 应改变 slot 1 RAM 的内容，读回一致；读 `0x0002_0020` 在跨域外设中取回对应字。
- 任何访问 `0x0010_0000`（未映射）都应得到 DECERR。
- 跨域外设所在的另一个时钟域被复位时，slot 2 上的访问应立即收到 SLVERR（`AXI_ERROR_RESP_G` 默认值），不挂起。
- 若你只做阅读型实践，把上述每一步在源码里的对应行号标出来即可，仿真部分标注「待本地验证」。

## 6. 本讲小结

- `AxiLiteCrossbar` 用 `AxiLiteCrossbarMasterConfigArray`（每段含 `baseAddr/addrBits/connectivity`）描述地址窗口；解码靠 `StdMatch` 比较地址高位，`addrBits` 同时决定窗口大小 \( 2^{\text{addrBits}} \) 与解码忽略的低位数；解码失败回 `DEC_ERROR_RESP_G`（默认 DECERR）。
- 交叉开关分两层状态机：slave 侧做地址解码与请求转发，master 侧用 `arbitrate` 在多主机竞争同一外设时轮询，并把选中主机的总线**直连**到输出；配置错（窗口不对齐、重叠）会在 elaboration 阶段被 `assert ... severity failure` 拦截。
- `AxiLiteMaster` 用极简的 `REQ/ACK` 接口（`AxiLiteReqType/AxiLiteAckType`）封装一次读/写，内部 5 状态机按「VALID 等 READY 再撤、最后收响应」展开成标准 AXI-Lite 握手。
- `AxiLiteAsync` 给 5 个通道各配一个 `FifoASync` 跨时钟域，READY 就地由 `not full` 生成、不跨 FIFO；双向复位用 `RstSync` 同步；对端域复位时强制 `ready` 吞请求、强制 `valid+AXI_ERROR_RESP_G` 给主机一个错误响应，从而永不死锁。
- `genAxiLiteConfig` 一行即可均匀切分地址空间生成配置数组，是手写多窗口配置的推荐捷径。

## 7. 下一步学习建议

- 接着读 **u3-l4 AxiVersion 与内存映射辅助块**：那里会用本讲的交叉开关把 `AxiVersion`、`AxiDualPortRam`、`AxiLiteRegs` 等挂到一张完整的地址地图上，并对照 PyRogue 的寄存器偏移。
- 若你想看交叉开关在真实大系统里的用法，可以读 [protocols/srp/tb/SrpV3AxiLiteTb.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/tb/SrpV3AxiLiteTb.vhd) 或任意 `Pgp*Wrapper.vhd`，它们都用 `genAxiLiteConfig` + `AxiLiteCrossbar` 组织本地寄存器空间。
- 跨域主题会在 **u8 AXI4/DMA/桥接** 里再次出现（AXI4 的跨域、`AxiToAxiLite` 桥），届时可以把本讲的「每通道一个 FIFO」思路推广到更宽的数据总线。
- 想加深对地址解码 helper 的理解，可回看 u3-l2 的 `axiSlaveRegister/axiSlaveDefault`——那是「叶子从机内部」的字粒度解码，而本讲是「总线拓扑层」的窗口粒度解码，两者是不同层级的地址解码。
