# AxiVersion 与内存映射辅助块

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `AxiVersion` 这个「几乎每个 SURF 工程都会例化」的标准版本/状态寄存器块，能背出它的寄存器偏移表。
- 理解「固件版本、git hash、构建字符串」这些**编译期常量**是如何通过 `BUILD_INFO_G` 注入、再经 AXI-Lite 暴露给软件的。
- 掌握 `AxiDualPortRam` 这类「把一块双口 RAM 挂到 AXI-Lite 地址空间」的辅助块模式，理解它的状态机与读延迟处理。
- 认识 `AxiLiteRegs` 这种「参数化生成一批读写寄存器」的通用封装，并把它和 u3-l2 的手写端点模式对照。
- 知道 RTL 寄存器布局必须与 `python/surf/` 下的 PyRogue 镜像逐字段对齐（偏移、位宽、读写属性三者一致），为 u9 的 PyRogue 篇打基础。

## 2. 前置知识

本讲直接承接 **u3-l2（AXI-Lite 寄存器端点模式）**。你需要已经掌握：

- `AxiLiteReadMasterType` / `AxiLiteReadSlaveType` 等四个记录（见 u3-l1）。
- 双进程骨架：`RegType` / `REG_INIT_C` / `r` / `rin` / `comb` / `seq`（见 u1-l5）。
- 端点四步骨架：`axiSlaveWaitTxn` 解码 → `axiSlaveRegister` / `axiSlaveRegisterR` 绑定地址 → `axiSlaveDefault` 兜底回 `DECERR`（见 u3-l2）。

三个本讲会用到的补充概念：

- **编译期常量 vs. 运行期寄存器**：像固件版本号这种值，在综合时就已经确定（来自 git 提交、构建服务器、构建时间）。SURF 把它打包成一个位向量 `BUILD_INFO_G`，综合时塞进 ROM，软件运行时只能读、不能写——这种寄存器用 `axiSlaveRegisterR`（只读）暴露。
- **AXI-Lite 的字粒度**：AXI-Lite 数据宽度固定 32 位，地址按**字节**编址，但寄存器按**字（4 字节）**对齐，所以 `0x000`、`0x004`、`0x008` 才是连续寄存器。
- **双口 RAM 的读延迟**：块 RAM 读地址打入后，数据要 1～3 拍后才出来。把 RAM 挂到 AXI-Lite 上时，读响应不能立刻返回，必须等数据就绪——这正是 `AxiDualPortRam` 状态机要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [axi/axi-lite/rtl/AxiVersion.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd) | 标准版本/状态寄存器块：固件版本、git hash、构建字符串、上电秒计数、FPGA 重载、Device DNA 等。是本讲主线范例。 |
| [axi/axi-lite/rtl/AxiDualPortRam.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd) | 内存映射辅助块：把一块双口 RAM 的 A 口交给 AXI-Lite、B 口交给用户逻辑，支持字节写、读延迟、跨时钟域。 |
| [axi/axi-lite/rtl/AxiLiteRegs.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteRegs.vhd) | 通用寄存器文件封装：参数化生成 N 个读寄存器 + M 个写寄存器，地址布局固定（读在 0x000 段、写在 0x100 段）。 |
| [base/general/rtl/StdRtlPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd) | 提供 `BuildInfoType` / `BuildInfoRetType` / `toBuildInfo`，定义了 `BUILD_INFO_G` 这个 2240 位向量的内部切片布局。 |
| [python/surf/axi/_AxiVersion.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py) | PyRogue 软件镜像：用 `pr.RemoteVariable` 逐字段镜像 RTL 的寄存器布局，是「RTL ↔ 软件」对齐的活样本。 |

> 说明：本讲的「内存映射辅助块」以 `AxiDualPortRam` 为代表。仓库里还有 `AxiLiteRegs`（参数化寄存器文件）等同类辅助块；它们都遵循同一思路——把一片存储/一组寄存器接到 AXI-Lite 上。

---

## 4. 核心概念与源码讲解

### 4.1 AxiVersion：标准版本寄存器块

#### 4.1.1 概念说明

`AxiVersion` 回答一个工程问题：**软件怎么知道它正在跟哪一份固件对话？**

板卡上电后，CPU 通过 AXI-Lite 读到的第一件事通常是「这是谁、什么版本、什么时候编的」。`AxiVersion` 把这些诊断信息集中到一个固定布局的寄存器块里：

- **编译期常量**（只读）：固件版本号、git 提交的 SHA-1、构建服务器与时间戳字符串、设备 ID。这些值在综合时由构建系统通过 `BUILD_INFO_G` 注入。
- **运行期状态**（只读）：上电以来的秒数（`UpTimeCnt`）、Xilinx 芯片 DNA、DS2411 板卡序列号。
- **控制寄存器**（读写）：一个可读写的 `ScratchPad`（用来验证总线连通性）、用户复位、以及 FPGA 自重载控制。

之所以说它是「标准块」，是因为几乎每个 SURF 顶层工程都会例化一个 `AxiVersion` 并放在地址空间的最低端——它既是健康检查点，也是「总线通不通」的第一个试金石。

#### 4.1.2 核心流程

`AxiVersion` 的运行逻辑极简，全部写在 `comb` 进程里，复用 u3-l2 的端点四步骨架：

```text
每拍 comb 进程：
  1. v := r                              -- 复制现态
  2. axiSlaveWaitTxn(ep, ...)            -- 解码本拍 AXI 事务，填 writeEnable/readEnable
  3. 逐行 axiSlaveRegister / axiSlaveRegisterR  -- 把地址 0x000..0x800 绑到信号
  4. axiSlaveDefault(..., DECERR)        -- 兜底：未映射地址回 DECERR
  5. 1Hz 定时器：到点就让 upTimeCnt + 1  -- 独立于 AXI 的后台计数
  6. (同步复位分支)
  7. rin <= v                            -- 次态送 seq 打寄存器
```

后台的「上电秒计数」是一个独立的 1 Hz 定时器：用 `CLK_PERIOD_G` 算出 1 秒对应的时钟周期数，数满就给 `upTimeCnt` 加一。这部分逻辑和 AXI 事务**并行**，互不阻塞。

编译期常量的注入路径则是：

```text
构建系统(脚本) ──> BUILD_INFO_G: slv(2239 downto 0) ──> toBuildInfo() ──> BuildInfoRetType 记录
                                                                  ├── buildString (64×32b)
                                                                  ├── fwVersion  (32b)
                                                                  └── gitHash    (160b)
                                                          ──> axiSlaveRegisterR 暴露成只读寄存器
```

#### 4.1.3 源码精读

**（1）泛型与端口**——注意三个复位泛型（u1-l4 约定）之外，多了 `BUILD_INFO_G`、`CLK_PERIOD_G` 和一组「可选外设使能」开关：

[AxiVersion.vhd:26-42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L26-L42) 用 `BUILD_INFO_G : BuildInfoType`（无默认值，必须由上层传入）注入编译期信息；`EN_DEVICE_DNA_G` / `EN_DS2411_G` / `EN_ICAP_G` 三个布尔开关决定是否例化对应的硬件外设。这说明 `AxiVersion` 是可裁剪的：不需要 DNA 读取的工程可以让那部分逻辑完全不综合。

[AxiVersion.vhd:43-65](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L43-L65) 给出标准 AXI-Lite 四记录端口，外加 `userValues : in Slv32Array(0 to 63)`——一组 64 个用户自定义常量输入（由上层把任意 32 位值送进来，软件只读）。

**（2）把 `BUILD_INFO_G` 解包成记录**：

[AxiVersion.vhd:73-74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L73-L74) 调用 `toBuildInfo(BUILD_INFO_G)` 得到 `BUILD_INFO_C`，再取出 `buildString` 存成 `BUILD_STRING_ROM_C`。这个解包函数定义在 StdRtlPkg 里，切片布局如下：

[StdRtlPkg.vhd:694-702](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L694-L702) —— `BUILD_INFO_G` 是一个 2240 位向量，分成三段：低 2048 位（256 字节）是构建字符串、中间 32 位是版本号、高 160 位是 git SHA-1。

```
BUILD_INFO_G(2047 downto 0)    = buildString  (Slv32Array 0..63，共 256 字节)
BUILD_INFO_G(2079 downto 2048) = fwVersion    (32 位)
BUILD_INFO_G(2239 downto 2080) = gitHash      (160 位，正好是 SHA-1)
```

**（3）把构建字符串钉成分布式 ROM**：

[AxiVersion.vhd:107-112](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L107-L112) 用 `rom_style = "distributed"` 和 `rom_extract = "TRUE"` 把 256 字节的构建字符串约束成分布式 ROM（查表即得，无需写口）——这是只读常量的标准处理。

**（4）寄存器映射（本讲核心）**：

[AxiVersion.vhd:186-207](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L186-L207) 是完整的地址绑定。逐行读：

- `axiSlaveWaitTxn(...)` 解码事务（u3-l2）。
- `axiSlaveRegisterR(ep, x"000", 0, BUILD_INFO_C.fwVersion)`——只读，把版本号绑到 `0x000`。
- `axiSlaveRegister(ep, x"004", 0, v.scratchPad)`——读写，测试寄存器在 `0x004`。
- `axiSlaveRegisterR(ep, x"400", userValues)`——**数组重载**：函数内部对数组循环，把 `userValues(i)` 绑到 `0x400 + i*4`（详见下文 4.3）。
- `axiSlaveRegisterR(ep, x"800", BUILD_STRING_ROM_C)`——同样是数组重载，256 字节字符串铺在 `0x800` 起。
- `axiSlaveDefault(..., AXI_RESP_DECERR_C)`——兜底，未命中地址回 DECERR。

整理成完整的寄存器偏移表：

| 偏移 | 名称 | 位宽 | 读写 | RTL 来源 | 含义 |
|------|------|------|------|----------|------|
| `0x000` | FpgaVersion | 32 | RO | `BUILD_INFO_C.fwVersion` | 固件版本号 |
| `0x004` | ScratchPad | 32 | RW | `r.scratchPad` | 读写测试寄存器 |
| `0x008` | UpTimeCnt | 32 | RO | `r.upTimeCnt` | 上电/复位后秒数 |
| `0x100` | FpgaReloadHalt | 1 | RW | `r.haltReload` | 阻止自动重载 |
| `0x104` | FpgaReload | 1 | RW | `r.fpgaReload` | 写 1 触发 FPGA 重载 |
| `0x108` | FpgaReloadAddress | 32 | RW | `r.fpgaReloadAddr` | 重载起始地址 |
| `0x10C` | UserReset | 1 | RW | `r.userReset` | 用户复位输出 |
| `0x300` | FdSerial | 64 | RO | `fdValue` | DS2411 板卡序列号 |
| `0x400`–`0x4FF` | UserConstants | 32×64 | RO | `userValues(0..63)` | 64 个用户常量 |
| `0x500` | DeviceId | 32 | RO | `DEVICE_ID_G` | 设备 ID |
| `0x600` | GitHash | 160 | RO | `BUILD_INFO_C.gitHash` | git SHA-1 |
| `0x700` | DeviceDna | 128 | RO | `dnaValue` | Xilinx Device DNA |
| `0x800`–`0x8FF` | BuildStamp | 2048 | RO | `BUILD_STRING_ROM_C` | 构建字符串(256 字节) |

**（5）后台 1 Hz 定时器**：

[AxiVersion.vhd:70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L70) 用 `getTimeRatio(1.0, CLK_PERIOD_G)` 算出 1 秒的周期数。设 `CLK_PERIOD_G = 8.0e-9`（125 MHz），则：

\[ \text{TIMEOUT\_1HZ\_C} = \frac{1.0}{8.0\times10^{-9}} - 1 = 124{,}999{,}999 \]

[AxiVersion.vhd:212-234](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L212-L234) 中，`timer` 数到 `TIMEOUT_1HZ_C` 就清零并让 `upTimeCnt + 1`，同时推进可选的「自动重载」倒计时。注意这段逻辑写在 `axiSlaveDefault` 之后、复位之前，和 AXI 事务完全独立。

**（6）双进程骨架**：[AxiVersion.vhd:173-263](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L173-L263) 是标准的 `comb`（算次态）+ `seq`（打寄存器，`RST_ASYNC_G` 决定复位走哪边），与 u1-l5 完全一致，不再展开。

#### 4.1.4 代码实践

> 本实践是「源码阅读型」，目标是把寄存器偏移表落实成你的肌肉记忆，为 PyRogue 篇做准备。

1. **实践目标**：独立从 RTL 推导出 `AxiVersion` 的寄存器偏移表，并与 PyRogue 镜像逐条核对。
2. **操作步骤**：
   - 打开 [AxiVersion.vhd:188-204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L188-L204)，对每一行 `axiSlaveRegister(R)` 记录三件事：**偏移地址、绑定的信号、是 R（只读）还是可写**。
   - 对数组重载形式（`0x400` 和 `0x800`），自己算出它覆盖的地址区间（元素个数 × 4 字节）。
   - 打开 PyRogue 镜像 [_AxiVersion.py:35-219](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L35-L219)，把每个 `pr.RemoteVariable` 的 `offset` 和 `bitSize` 抄下来，和你的表逐行比对。
3. **需要观察的现象**：RTL 的 `x"000"` 对应 PyRogue 的 `offset=0x00`；RTL 的 `gitHash`（160 位）对应 PyRogue 的 `bitSize=160`；RTL 的 `BUILD_STRING_ROM_C`（256 字节）对应 PyRogue 的 `bitSize=8*256`。
4. **预期结果**：两边至少 13 个寄存器的**偏移、位宽、读写属性**三者完全一致。若发现任一不一致，就是 bug（软件会读到错位的值）。
5. 不需要运行任何命令；这是一次纯阅读对照。如想进一步验证，可待 u9 的 cocotb 篇用 GHDL 仿真实际读这些寄存器（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FpgaVersion`、`GitHash` 用 `axiSlaveRegisterR`（只读），而 `ScratchPad` 用 `axiSlaveRegister`（读写）？

> **答案**：版本号和 git hash 是综合时注入的编译期常量，软件只需要读；`ScratchPad` 是运行期可写寄存器，软件要写它来验证总线连通，所以用可写版本。

**练习 2**：若 `CLK_PERIOD_G` 改成 `4.0e-9`（250 MHz），`UpTimeCnt` 每秒加一的节奏会变吗？

> **答案**：不会。`TIMEOUT_1HZ_C = getTimeRatio(1.0, CLK_PERIOD_G) - 1` 会自动变成 `1.0/4.0e-9 - 1 = 249,999,999`，定时器数得更慢一拍，但仍是每秒触发一次，`UpTimeCnt` 仍是「每秒加一」。这正是用 `getTimeRatio` 而非硬编码周期的妙处。

**练习 3**：地址 `0x200` 没有任何 `axiSlaveRegister` 绑定，软件读它会得到什么响应？

> **答案**：`axiSlaveDefault` 兜底返回 `AXI_RESP_DECERR_C`（地址译码错误），读数据未定义。这正是 u3-l2 讲过的「未映射地址回 DECERR」。

---

### 4.2 AxiDualPortRam：把双口 RAM 挂到 AXI-Lite

#### 4.2.1 概念说明

`AxiVersion` 处理的是「一个个零散的 32 位寄存器」。但很多时候你需要的是**一整块连续内存**——比如一块缓冲区、一张查找表、一组大数据。`AxiDualPortRam` 就是干这个的：

- 它内部例化一块双口 RAM。
- **A 口**接 AXI-Lite：软件可以通过 AXI 地址读写整块 RAM。
- **B 口**接用户逻辑：你的 RTL 可以用普通的 `clk/en/we/addr/din/dout` 接口访问同一块 RAM。

两口共享同一块存储，但各自有时钟——于是它天然支持「软件域写、硬件域读」或反过来的跨域数据交换。它的核心权衡是「读延迟」：块 RAM 的读数据要 1～3 拍才出来，AXI-Lite 的读响应必须等数据就绪才能返回。

#### 4.2.2 核心流程

`AxiDualPortRam` 用一个两状态的小状态机来对齐读延迟：

```text
IDLE_S:
  若 writeEnable:  把 awaddr/wstrb 打进 RAM 写口(A 口), 立刻回写响应(BRESP=OK)  -- 写是单周期
  若 readEnable:   把 araddr 打进 RAM 读口, 装 4 拍倒计时 rdLatecy, 转 RD_S
RD_S:
  每拍 rdLatecy - 1; 到 0 时回读响应(RRESP=OK, 数据已在 axiDout 上), 回 IDLE_S
```

写事务是**单周期**的：地址和字节写使能直接来自 AXI 的 `awaddr`/`wstrb`，写响应在 `IDLE_S` 当拍就给出。读事务必须**等**：因为 RAM 读是流水线的，`RD_S` 的倒计时（`rdLatecy := 4`）留足了 1～3 拍读延迟的余量，等数据稳定再回 RRESP。

地址解码的位切片也值得记住：

```text
AXI 字节地址 awaddr/araddr:
  [1:0]            字节偏移(AXI-Lite 恒忽略)
  RAM 地址位        映射到 RAM 的字地址(ADDR_WIDTH_G 位)
  若 DATA_WIDTH_G>32: 还有额外的段选择位(选宽字里的哪 32 位)
```

当 `COMMON_CLK_G = false` 时，A 口（AXI 域）的写还要跨到 B 口（用户 `clk` 域）——用一个 `SynchronizerFifo` 把 `{strobe, addr, data}` 原子地搬到用户时钟域，这样用户逻辑能通过 `axiWrValid/axiWrAddr/axiWrData/axiWrStrobe` 观察到软件的每一次写。

#### 4.2.3 源码精读

**（1）泛型解读**：

[AxiDualPortRam.vhd:26-41](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L26-L41) 关键泛型：
- `SYNTH_MODE_G`（inferred/xpm/altera_mf）和 `MEMORY_TYPE_G`（block/ultra/distributed）选择 RAM 后端——和 u2-l3 的 RAM 构建块完全对接。
- `READ_LATENCY_G (0..3)` 决定 RAM 读出几拍——直接驱动上面的状态机倒计时。
- `AXI_WR_EN_G` / `SYS_WR_EN_G`：分别控制 A 口（AXI）和 B 口（系统）是否可写。两者组合出三种用法：AXI 只读 + 系统写（软件观测硬件填的表）、AXI 写 + 系统只读（软件配置硬件读的表）、双侧皆写（真双口）。
- `COMMON_CLK_G`：两口是否同钟，决定是否需要跨域 FIFO。

**（2）合法配置断言**：

[AxiDualPortRam.vhd:132-136](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L132-L136) 用 `assert ... severity failure` 在 elaboration 阶段拦住不支持的「后端 + 类型 + 读延迟」组合（例如 inferred + distributed 只支持 0～1 拍读延迟）。配置错误直接编译失败，而不是上板乱跑。

**（3）按用法生成三种 RAM**：

[AxiDualPortRam.vhd:203-303](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L203-L303)（inferred 分支）用三个互斥 generate 覆盖三种用法：
- `AXI_R0_SYS_RW`（[206-235](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L206-L235)）：AXI 只读、系统写 → 例化 `DualPortRam`（单口写、单口读）。
- `AXI_RW_SYS_RO`（[239-267](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L239-L267)）：AXI 写、系统只读 → 同样例化 `DualPortRam`，但 A 口接 AXI 写。
- `AXI_RW_SYS_RW`（[270-301](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L270-L301)）：双侧皆写 → 例化 `TrueDualPortRam`。

这正是 u2-l3 讲的 `SimpleDualPortRam` / `DualPortRam` / `TrueDualPortRam` 在 AXI 场景下的复用。xpm/altera 分支（[138-201](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L138-L201)）则统一用 `TrueDualPortRamXpm` / `TrueDualPortRamAlteraMf`。

**（4）读延迟状态机（核心）**：

[AxiDualPortRam.vhd:365-404](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L365-L404)：
- `IDLE_S` 收到写：设 `v.axiAddr := awaddr(...)`、`v.axiWrStrobe := wstrb`，调 `axiSlaveWriteResponse` 当拍回 BRESP。
- `IDLE_S` 收到读：设 `v.axiAddr := araddr(...)`、`v.rdLatecy := 4`，转 `RD_S`。
- `RD_S`：每拍 `rdLatecy - 1`，到 0 时调 `axiSlaveReadResponse` 回 RRESP（此时 RAM 数据已稳定在 `axiDout` 上）。

读数据在 [AxiDualPortRam.vhd:354-359](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L354-L359) 组合地 mux 到 `rdata`，宽字（>32 位）时用 `decAddrInt` 选出对应的 32 位段。

**（5）跨时钟域写**：

[AxiDualPortRam.vhd:307-333](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L307-L333) 用 `SynchronizerFifo` 把 AXI 域的 `{strobe, addr, data}` 打包搬到用户 `clk` 域，输出 `axiWrValid/axiWrAddr/axiWrData/axiWrStrobe`。这是 u2-l1/u2-l2 的跨域积木在此处的典型应用。

#### 4.2.4 代码实践

1. **实践目标**：通过修改一个泛型，观察 RAM 行为的差别，理解读延迟状态机存在的必要性。
2. **操作步骤**：
   - 阅读状态机 [AxiDualPortRam.vhd:365-404](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd#L365-L404)，回答：为什么写响应在 `IDLE_S` 当拍给出，而读响应要进 `RD_S` 等几拍？
   - 设想把 `READ_LATENCY_G` 从 2 改成 0（分布式 RAM、0 拍读），追状态机：`RD_S` 的 `rdLatecy := 4` 倒计时是否还必要？（提示：0 拍读时数据当拍就出，但状态机仍提供固定节拍，保证 AXI 握手时序合规。）
3. **需要观察的现象**：写事务 1 拍完成（BRESP 即时）；读事务需 `rdLatecy` 拍后才回 RRESP。
4. **预期结果**：能解释「RAM 的物理读延迟」与「状态机的等待拍数」为何要解耦——状态机用固定 4 拍余量覆盖了 0～3 拍的所有合法 `READ_LATENCY_G`。
5. 完整的波形级验证需用 GHDL/cocotb 跑（待本地验证），此处为源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：`AXI_WR_EN_G = false` 且 `SYS_WR_EN_G = true` 描述的是哪种用法？软件读这块 RAM 看到的是什么？

> **答案**：这是「软件只读、硬件写」模式（对应 `AXI_R0_SYS_RW` 分支）。软件读到的是用户逻辑实时填进 RAM 的内容——典型场景是硬件把传感器数据/统计写进 RAM，软件轮询读取。

**练习 2**：为什么 A 口的写（`axiWrStrobe`）要经 `SynchronizerFifo` 才能到 B 口域，而读不需要？

> **答案**：写是 A 口发起、要让 B 口域看见，多比特的 `{addr, data, strobe}` 必须原子跨域，所以用 FIFO。读则相反：B 口（RAM）的输出 `dout` 直接给用户逻辑、`axiDout` 给 AXI 读路径，各自在自己域内用，不需要把多比特信号跨回去。

---

### 4.3 AxiLiteRegs：寄存器文件封装与 PyRogue 对齐

#### 4.3.1 概念说明

`AxiVersion` 是「手写每一个寄存器」——好处是地址布局完全自由，代价是每加一个寄存器要写一行 `axiSlaveRegister`。当你只需要「一批同质、连续排列的读写寄存器」时（比如一个简单外设的配置表），`AxiLiteRegs` 给了一个**参数化捷径**：

- 用 `NUM_READ_REG_G` / `NUM_WRITE_REG_G` 指定数量。
- 它自动把读寄存器铺在 `0x000` 段、写寄存器铺在 `0x100` 段，每个 4 字节。
- 用户只要把 `readRegister`（输入）和 `writeRegister`（输出）两个数组接上即可。

这是一个「固定布局、零手写」的封装，和 `AxiVersion` 的「自由布局、逐行手写」形成对照——两者各有适用场景。

本模块还要讲清一件跨 RTL/软件的大事：**寄存器布局必须与 PyRogue 镜像逐字段对齐**。SURF 的约定是，每个有 AXI-Lite 接口的 RTL 块，在 `python/surf/` 下都有一个同名 PyRogue 设备类，用 `pr.RemoteVariable` 把同样的偏移/位宽/读写属性镜像一遍。`AxiVersion` 和 `_AxiVersion.py` 就是这套对齐机制最完整的范例。

#### 4.3.2 核心流程

`AxiLiteRegs` 的 comb 进程用 `for` 循环批量绑定：

```text
axiSlaveWaitTxn(...)                          -- 解码
for i in 0..NUM_READ_REG_G-1:                 -- 读寄存器铺在 0x000 + i*4
    axiSlaveRegisterR(ep, (i*4)+0, readRegister(i))
for i in 0..NUM_WRITE_REG_G-1:                -- 写寄存器铺在 0x100 + i*4
    axiSlaveRegister(ep, (i*4)+256, v.writeRegister(i))
axiSlaveDefault(..., DECERR)                  -- 兜底
```

注意地址是**用表达式算出来**的（`toSlv((i*4)+0, 9)` 和 `toSlv((i*4)+256, 9)`），9 位地址覆盖 0x000–0x1FF。读写寄存器各占一个 256 字节的段。

PyRogue 对齐的流程则在软件侧：

```text
RTL: axiSlaveRegisterR(ep, x"600", 0, gitHash)   -- 偏移 0x600, 160 位, 只读
     ↓ 必须一一对应
PyRogue: pr.RemoteVariable(name='GitHash', offset=0x600, bitSize=160, mode='RO')
```

三件事必须一致：**偏移（offset）、位宽（bitSize）、读写属性（mode）**。

#### 4.3.3 源码精读

**（1）`AxiLiteRegs` 的循环绑定**：

[AxiLiteRegs.vhd:93-101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteRegs.vhd#L93-L101) ——读寄存器在 `(i*4)+0`（即 `0x000` 段），写寄存器在 `(i*4)+256`（即 `0x100` 段）。这就是它的「固定布局」。

[AxiLiteRegs.vhd:51-58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteRegs.vhd#L51-L58) 的 `writeRegIni` 函数允许写寄存器有一个统一的或逐个的初值（`INI_WRITE_REG_G` 传 1 个元素就广播到全部，传满 N 个就逐个赋值），并由 [76-81](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteRegs.vhd#L76-L81) 的 assert 校验范围匹配——这是参数化封装里典型的「单值广播 vs 数组」二选一约定。

**（2）数组重载形式的 helper**：回头看 `AxiVersion` 里 `axiSlaveRegisterR(ep, x"400", userValues)` 这种「第三参数是数组」的调用，它定义在 AxiLitePkg 里：

[AxiLitePkg.vhd:956-966](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L956-L966) ——对数组循环，把 `regs(i)` 绑到 `addr + i*4`。所以 `userValues(0..63)` 自动铺到 `0x400, 0x404, …, 0x4FC`。这正是 `AxiVersion` 能用一行代码铺 64 个用户常量的原因。

**（3）PyRogue 对齐的活样本**：对照 `AxiVersion` 与 `_AxiVersion.py` 的几个典型字段：

| RTL（AxiVersion.vhd） | PyRogue（_AxiVersion.py） | 对齐点 |
|---|---|---|
| `axiSlaveRegisterR(ep, x"000", 0, fwVersion)` | [_AxiVersion.py:35-44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L35-L44) `FpgaVersion, offset=0x00, bitSize=32, mode='RO'` | 偏移 0x00 / 32 位 / 只读 |
| `axiSlaveRegisterR(ep, x"600", 0, gitHash)` | [_AxiVersion.py:181-190](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L181-L190) `GitHash, offset=0x600, bitSize=160, mode='RO'` | 偏移 0x600 / 160 位 / 只读 |
| `axiSlaveRegisterR(ep, x"800", BUILD_STRING_ROM_C)` | [_AxiVersion.py:210-219](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L210-L219) `BuildStamp, offset=0x800, bitSize=8*256, base=pr.String` | 偏移 0x800 / 2048 位 / 字符串 |
| `axiSlaveRegisterR(ep, x"400", userValues)` | [_AxiVersion.py:157-168](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L157-L168) `addRemoteVariables(UserConstants, offset=0x400, stride=4, number=numUserConstants)` | 偏移 0x400 / 步长 4 / 批量 |

注意 `UserConstants` 的 RTL 是固定 64 个、PyRogue 用 `number=numUserConstants` 按需生成——两边都按 `stride=4` 排列，所以软件只镜像自己关心的前 N 个，地址仍连续对齐。

**（4）派生变量（LinkVariable）**：[_AxiVersion.py:79-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L79-L87) 的 `UpTime` 是 `UpTimeCnt` 的**派生**显示——把秒数格式化成 `HH:MM:SS`。这种 `LinkVariable` 不占寄存器地址，纯软件侧加工，RTL 里没有对应物。同理 `GitHashShort`、`BuildDate` 等都是从原始寄存器派生出来的易读视图。

#### 4.3.4 代码实践

1. **实践目标**：用 `AxiLiteRegs` 的思路，给一个假想外设规划寄存器布局，并写出对应的 PyRogue 镜像骨架。
2. **操作步骤**：
   - 假设某外设有 2 个只读状态寄存器、3 个读写配置寄存器。用 `AxiLiteRegs` 的固定布局，写出它们的地址（答案：状态在 `0x000`、`0x004`；配置在 `0x100`、`0x104`、`0x108`）。
   - 仿照 [_AxiVersion.py:35-44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L35-L44)，为这 5 个寄存器各写一个 `pr.RemoteVariable`，确保 `offset` 和 `mode` 与上一步对齐。
3. **需要观察的现象**：状态寄存器的 `mode` 应为 `'RO'`，配置寄存器应为 `'RW'`；每个 `offset` 严格对应 RTL 的 `(i*4)+0` 或 `(i*4)+256`。
4. **预期结果**：得到一份 RTL 与 PyRogue 地址完全一致的对照表。
5. 此为设计型实践，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：`AxiLiteRegs` 把读寄存器放在 `0x000` 段、写寄存器放在 `0x100` 段。如果 `NUM_READ_REG_G = 32`，读段会不会溢出到写段？

> **答案**：不会。32 个读寄存器占 `0x000`–`0x07C`（32×4=128 字节），写段从 `0x100` 起，中间 `0x080`–`0x0FF` 留空。地址用 9 位（`toSlv(..., 9)`）表达，覆盖 0x000–0x1FF，读段（0x000–0x07F）与写段（0x100–0x1FF）天然不重叠。

**练习 2**：若你改了 RTL 把 `ScratchPad` 从 `0x004` 挪到 `0x010`，但忘了改 PyRogue，会发生什么？

> **答案**：软件按旧镜像往 `0x004` 读写，实际碰到的是新布局里 `0x004` 上的别的寄存器（或 DECERR），读到错位的值。这正是为什么 SURF 强调「改 RTL 必须同步改 PyRogue 和 ruckus.tcl」（见 u1-l1 的 AGENTS 约定）。

**练习 3**：`AxiVersion` 的 `GitHash`（160 位）超过了一个 32 位 AXI-Lite 字，它是怎么被读到的？

> **答案**：`axiSlaveRegisterR` 对 `slv` 类型的处理会自动跨多个字——160 位占 5 个 32 位字，铺在 `0x600`–`0x610`。PyRogue 端用 `bitSize=160` 一次读完整 5 个字并拼成一个大整数。两边靠「位宽 + 起始偏移」隐式约定跨字边界。

---

## 5. 综合实践

把本讲三块内容串起来，做一次「从需求到 RTL+软件镜像」的完整设计演练。

**场景**：你要给一个温度传感器外设做一个 AXI-Lite 接口，要求软件能读到：固件版本、当前温度（16 位，硬件实时更新）、以及 16 字节的设备名；还要能写一个「采样周期」配置寄存器。

**任务**：

1. **选型**：判断哪些用 `AxiVersion` 风格（手写端点）、哪些用 `AxiDualPortRam`、哪些用 `AxiLiteRegs`。
   - 参考答案：版本号复用 `AxiVersion`（直接例化即可）；温度 + 采样周期这种少量零散寄存器用 `AxiVersion`/`AxiLiteRegs` 风格的手写端点（`axiSlaveRegisterR` 读温度、`axiSlaveRegister` 写周期）；16 字节设备名可以用一个小的 `AxiDualPortRam`（`AXI_R0_SYS_RW`，软件只读、硬件初始化）或直接 4 个只读寄存器。
2. **画地址表**：为每个字段分配不冲突的偏移（如温度 `0x000`、采样周期 `0x004`、版本 `0x100`、设备名 `0x200` 起 4 个字）。
3. **写 RTL 骨架**：仿照 [AxiVersion.vhd:186-207](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L186-L207)，用 `axiSlaveWaitTxn` / `axiSlaveRegister(R)` / `axiSlaveDefault` 四步骨架绑定这些地址。注意：跨时钟域的温度值要先同步再暴露（u3-l2 的铁律）。
4. **写 PyRogue 镜像**：仿照 [_AxiVersion.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py)，为每个寄存器写一个 `pr.RemoteVariable`，`offset/bitSize/mode` 三者与 RTL 严格一致；设备名用 `base=pr.String`。
5. **自检**：把 RTL 地址表和 PyRogue `offset` 并排对照，确认零偏差。

这个练习不需要综合或仿真，重点是养成「RTL 寄存器布局 = PyRogue 镜像布局」的工程直觉，为 u9 的 PyRogue 篇和 cocotb 回归篇打底。

## 6. 本讲小结

- `AxiVersion` 是 SURF 几乎每个工程都例化的标准版本/状态寄存器块，把固件版本、git hash、构建字符串、上电秒数、DNA 等 diagnostics 集中到固定地址布局。
- 编译期常量通过 `BUILD_INFO_G`（一个 2240 位向量，切片为 buildString/fwVersion/gitHash）注入，用 `toBuildInfo` 解包，再用 `axiSlaveRegisterR` 暴露成只读寄存器。
- 后台 1 Hz 定时器用 `getTimeRatio(1.0, CLK_PERIOD_G)` 自适应时钟周期，让 `UpTimeCnt` 严格每秒加一，与 AXI 事务并行运行。
- `AxiDualPortRam` 把一块双口 RAM 挂到 AXI-Lite：A 口给软件、B 口给用户逻辑；写单周期完成、读用 `IDLE_S→RD_S` 状态机等读延迟；跨时钟域写经 `SynchronizerFifo` 搬运。
- `AxiLiteRegs` 是「固定布局、参数化批量生成」的寄存器文件封装（读在 `0x000` 段、写在 `0x100` 段），与 `AxiVersion` 的「自由布局、逐行手写」互补；数组重载形式的 helper 能用一行铺 64 个寄存器。
- **RTL 寄存器布局必须与 PyRogue 镜像逐字段对齐**（偏移、位宽、读写属性三者一致），`AxiVersion` ↔ `_AxiVersion.py` 是这套对齐机制最完整的范例；改 RTL 必须同步改 PyRogue 和 ruckus.tcl。

## 7. 下一步学习建议

- **向数据平面过渡**：本讲的 `AxiVersion` / `AxiDualPortRam` 都是「寄存器/内存映射」层（AXI-Lite）。下一单元 u4 进入 **AXI-Stream 数据平面**，从 [AxiStreamPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd) 的流式记录开始，那里会用类似的「记录 + INIT_C + 包函数」思路，但面向的是流式数据而非寄存器。
- **回到总线拓扑**：若你想知道多个 `AxiVersion` / `AxiDualPortRam` 块如何接到一条总线上被 CPU 寻址，复习 u3-l3 的 `AxiLiteCrossbar` 地址解码。
- **为 PyRogue 篇蓄力**：本讲的 `_AxiVersion.py` 已经是 u9-l4（PyRogue 设备模型）的完整预习材料。建议现在就把它的 `pr.RemoteVariable` / `RemoteCommand` / `LinkVariable` / `addRemoteVariables` 四种构件记住，到 u9 会展开讲它们的注册、导出与再导出约定。
- **想动手验证**：等到 u9-l1/l2 学完 cocotb 工具链后，可以用 GHDL 仿真一个例化了 `AxiVersion` 的最小设计，用 Python 实际读出 `FpgaVersion`、`GitHash`、`UpTimeCnt`，亲手验证本讲的寄存器偏移表。
