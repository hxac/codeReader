# Simlink：与 Rogue 的 VHPI/ZMQ 协同仿真

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `axi/simlink/` 解决的问题：把仿真器（VCS）里的 AXI-Lite / AXI-Stream 接口，经 ZeroMQ 桥接到 Python 的 **Rogue** 框架，让 Python 脚本像驱动真实硬件一样驱动仿真中的 RTL。
- 解释 VHDL 的 `FOREIGN` 属性如何把一个空架构的实体委托给 C 代码（VHPI），以及 `VhpiGeneric` 框架如何在时钟沿上把端口值在 VHDL 与 C 之间搬运。
- 读懂 `RogueTcpMemory`（AXI-Lite 内存映射桥）和 `RogueTcpStream`（AXI-Stream 帧桥）两条核心数据通路，包括它们的 ZMQ 报文格式与状态机。
- 跟踪一次「Python Rogue 读寄存器 → ZMQ → 仿真器 → AXI-Lite」的完整往返。

## 2. 前置知识

### 2.1 什么是协同仿真（co-simulation）

纯 VHDL 仿真（GHDL / VCS）里，激励和校验都要用 VHDL 测试台（testbench）写。但当 RTL 越来越复杂、外设越来越多（PGP、以太网、JESD204B……），用 VHDL 重写一遍协议栈来做激励非常痛苦。协同仿真的思路是：**RTL 仿真器只跑被测设计，把它的对外接口“虚拟外接”到一个真实的软件环境**（这里是 Python + Rogue），由软件来扮演对端设备、注入数据、读寄存器。

SURF 选择的桥接层是 **ZeroMQ（ZMQ）**：一种轻量级消息队列库，用 PUSH/PULL 套接字在进程间传多帧（multipart）二进制消息。仿真器里的 C 代码和 Python 进程各自打开 ZMQ 套接字，通过 `127.0.0.1` 上的 TCP 端口交换报文。

### 2.2 VHPI 是什么

VHPI（VHDL Procedural Interface）是 VHDL 标准定义的 C 语言接口。仿真器允许在 VHDL 里用 `attribute FOREIGN` 标记某个架构，声明“这个架构体没有 VHDL 实现，请到指定的 C 共享库里找”。于是 C 代码就能读写 VHDL 信号、注册回调，相当于在 VHDL 里嵌入了一段 C 驱动。本讲涉及的 VHPI 接口都遵循 **IEEE 1076-2008**（与 u1-l2 里 GHDL 的 `--std=08` 一致），但请注意：**GHDL 目前不支持 VHPI**，所以 simlink 主要面向 Synopsys VCS。

### 2.3 前置讲义承接

- u3-l1 讲过 AXI-Lite 的五个通道（AR/R/AW/W/B）如何折叠成 `AxiLiteReadMasterType` 等记录。本讲的内存映射桥正是把这套扁平的 AXI-Lite 信号暴露给 C。
- u4-l1 讲过 AXI-Stream 的 `AxiStreamMasterType`（tValid/tData/tKeep/tLast/tUser 等）与 `AxiStreamConfigType`。本讲的流桥内部固定使用 8 字节（64 位）数据宽度，并把 SSI 的 SOF/EOF/EOFE 侧带塞进 TUSER（见 u5-l1）。

## 3. 本讲源码地图

`axi/simlink/` 的目录结构本身就揭示了“三套桥 + 一套公共框架 + 仿真器差异”的分工：

| 路径 | 作用 |
|------|------|
| [axi/simlink/sim/RogueTcpMemory.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/sim/RogueTcpMemory.vhd) | AXI-Lite 内存映射桥的 **VHDL 外壳**（空架构 + `FOREIGN` 属性），VCS 专用 |
| [axi/simlink/ghdl/RogueTcpMemory.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/ghdl/RogueTcpMemory.vhd) | 同名实体的 **GHDL 占位版**（`FOREIGN` 被注释掉），让 GHDL 能分析通过 |
| [axi/simlink/src/RogueTcpMemory.c](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c) / [.h](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.h) | AXI-Lite 桥的 C 实现：ZMQ 收发 + AXI-Lite 状态机 |
| [axi/simlink/sim/RogueTcpStream.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/sim/RogueTcpStream.vhd) / [src/RogueTcpStream.c](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c) | AXI-Stream 帧桥的 VHDL 外壳与 C 实现 |
| [axi/simlink/src/VhpiGeneric.c](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.c) / [.h](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.h) | 公共 VHPI 框架：端口句柄、enum↔int 转换、时钟沿回调 |
| [axi/simlink/tb/RogueTcpMemoryWrap.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/tb/RogueTcpMemoryWrap.vhd) / [RogueTcpStreamWrap.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/tb/RogueTcpStreamWrap.vhd) | 把裸桥包成 SURF 记录接口的薄封装，工程里实际例化的是它们 |
| [axi/simlink/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/ruckus.tcl) | 构建清单：按是否 GHDL 在 `sim/`（带 VHPI）与 `ghdl/`（占位）间选择 |
| [axi/simlink/src/Makefile](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/Makefile) | 把 `src/*.c` 编译成 `libAxiSim.so`（依赖 VCS 头与 libzmq） |

旁路参考：`RogueSideBand`（opCode/remData 侧带桥）与本讲同构，可在综合实践中自行阅读；真实工程用法见 [protocols/pgp/pgp3/core/tb/RoguePgp3Sim.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/tb/RoguePgp3Sim.vhd)。

---

## 4. 核心概念与源码讲解

### 4.1 VHPI + ZMQ 协同仿真总框架

#### 4.1.1 概念说明

simlink 的核心技巧是 **“空壳实体 + C 实现”**：VHDL 实体的端口声明完全保留（这样它能像普通模块一样被例化、连进记录总线），但架构体是空的，只挂一个 `FOREIGN` 属性告诉仿真器去调 C。所有真正的逻辑——ZMQ 收发、握手状态机——都在 C 里。这样做的收益是：仿真器无需理解 Python，Python 也无需理解 VHDL，两边只认 ZMQ 报文。

整套机制由四层叠成：

1. **VHDL 外壳**：声明端口 + `FOREIGN` 属性。
2. **C 入口三件套**：`Elab`（注册错误回调）、`Init`（建端口句柄表 + 注册时钟回调）、`Update`（每个时钟沿执行的业务逻辑）。
3. **VhpiGeneric 框架**：把“读端口→转 int→调 Update→转 enum→写端口”的样板抽公共，三个桥共用。
4. **ZMQ 传输**：每个桥在本地绑定两个端口（PULL 收、PUSH 发），与 Python 端的 Rogue 对接。

#### 4.1.2 核心流程

一次“时钟沿驱动”的整体流程如下：

```
VHDL clock 翻转
   └─► 仿真器触发 vhpiCbValueChange 回调（注册在 clock 信号上）
         └─► VhpiGenericCallBack():
               1. vhpi_get_value 读所有输入端口
               2. VhpiGenericConvertIn: std_logic 枚举值(2/3) → 0/1 整数
               3. vhpi_get_time 记录仿真时间
               4. 调用 桥特定的 Update()  ← 真正的业务逻辑在这
               5. VhpiGenericConvertOut: 0/1 整数 → 枚举值
               6. vhpi_put_value 把输出端口值推回 VHDL
```

`Update()` 内部靠“比较上一拍时钟值”来识别上升沿，与 u1-l5 的 `seq` 进程识别 `rising_edge` 是同一思路，只是发生在 C 侧。

ZMQ 的拓扑是经典的 **PUSH/PULL 管道**，方向要记牢：

- 仿真器侧 **绑定（bind）** 两个端口在 `127.0.0.1`：
  - `zmqPull` 绑在 `port`：**接收** Python 发来的请求（PULL 套接字 = 管道的收端）。
  - `zmqPush` 绑在 `port+1`：**发送** 给 Python（PUSH 套接字 = 管道的发端）。
- Python 侧的 Rogue `Memory` / `Stream` 设备则用相反的套接字类型连接（connect）到这两个端口。

#### 4.1.3 源码精读

**VHDL 外壳——空架构 + FOREIGN 属性**

[RogueTcpMemory.vhd:57-58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/sim/RogueTcpMemory.vhd#L57-L58) 把架构体标记为外部实现：

```vhdl
attribute FOREIGN of RogueTcpMemory : architecture is
   "vhpi:AxiSim:VhpiGenericElab:RogueTcpMemoryInit:RogueTcpMemory";
```

这个字符串的格式是 `vhpi:<库名>:<elab函数>:<init函数>:<架构名>`。即：

- `AxiSim`：共享库 `libAxiSim.so`（由 `src/Makefile` 编译，名字对应 src/Makefile 里的 `LIB := $(OUT)/libAxiSim.so`）。
- `VhpiGenericElab`：elaboration 阶段调用，只注册错误回调。
- `RogueTcpMemoryInit`：初始化阶段调用，建端口表 + 注册时钟回调。
- `RogueTcpMemory`：架构名，用于匹配。

实体的端口（[RogueTcpMemory.vhd:20-52](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/sim/RogueTcpMemory.vhd#L20-L52)）就是 AXI-Lite 五通道的扁平化版（32 位数据），与 u3-l1 的记录字段一一对应。

**GHDL 占位版——为什么需要它**

[ruckus.tcl:7-14](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/ruckus.tcl#L7-L14) 按是否设置了 `GHDLFLAGS` 在两套实体间二选一：

```tcl
if {![info exists ::env(GHDLFLAGS)]} {
   loadSource -lib surf -sim_only -dir "$::DIR_PATH/sim"   ;# 带 FOREIGN 属性
} else {
   loadSource -lib surf -sim_only -dir "$::DIR_PATH/ghdl"   ;# 占位版
}
```

GHDL 不支持 VHPI，所以 [ghdl/RogueTcpMemory.vhd:57-61](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/ghdl/RogueTcpMemory.vhd#L57-L61) 把 `FOREIGN` 注释掉、端口保持一致。这样 GHDL 分析时实体存在但不真正驱动任何信号，避免破坏整体构建图——这正是 u1-l3 讲的“`sim/` 放仅仿真模型、按工具分流”的约定。

**VhpiGeneric 框架——端口值在 C 与 VHDL 间搬运**

[VhpiGeneric.h:20-21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.h#L20-L21) 定义了两个让业务代码极简的宏：

```c
#define getInt(idx)      (portData->intValue[idx])
#define setInt(idx, val) (portData->intValue[idx] = val)
```

`portDataT` 结构体（[VhpiGeneric.h:24-55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.h#L24-L55)）里，每个端口都同时保存 VHPI 句柄、原始 `vhpiValueT`、以及一个 `intValue`（整数化的值，便于 C 运算）。`outEnable` 控制输出端口是否驱动（关闭时写成高阻 `4`，见下文）。

[VhpiGeneric.c:94-127](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.c#L94-L127) 是回调主函数，严格按 4.1.2 的六步执行：

```c
void VhpiGenericCallBack(vhpiCbDataT *cbData) {
    portDataT *portData = (portDataT *)cbData->user_data;
    for (x=0; x < portData->portCount; x++)          // 1. 读输入
        if ( portData->portDir[x] != vhpiOut )
            vhpi_get_value(portData->portHandle[x], portData->portValue[x]);
    VhpiGenericConvertIn(portData);                   // 2. enum → int
    vhpi_get_time(&(portData->simTime), NULL);        // 3. 仿真时间
    portData->stateUpdate(portData);                  // 4. 业务逻辑
    VhpiGenericConvertOut(portData);                  // 5. int → enum
    for (x=0; x < portData->portCount; x++)           // 6. 写输出
        if ( portData->portDir[x] != vhpiIn )
            vhpi_put_value(portData->portHandle[x], portData->portValue[x], vhpiForcePropagate);
}
```

注意 std_logic 的九值逻辑被压成 0/1：`VhpiGenericConvertIn` 把枚举值 `3`（forcing 1）当作 1、其余当作 0（[VhpiGeneric.c:31-53](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.c#L31-L53)）；`VhpiGenericConvertOut` 则把 0/1 转回 `2`/`3`，`outEnable=0` 时写 `4`（高阻）。这对仿真够用，但意味着这些桥**不建模 X/Z**，是行为级而非精确门级模型。

**时钟回调的注册**

[VhpiGeneric.c:210-224](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.c#L210-L224) 只对 **port 0**（即 `s_clock`）注册 `vhpiCbValueChange`：

```c
for (x=0; x < 1; x++) {
    cbData->reason = vhpiCbValueChange;
    cbData->obj    = portData->portHandle[x];   // = clock
    cbData->cbf    = VhpiGenericCallBack;
    ...
}
```

这就是为什么三个桥都在 `Update()` 里自己做上升沿检测——回调在 clock 的每次跳变（上升和下降）都会触发，由业务代码区分。

**C 共享库的构建**

[src/Makefile:14-21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/Makefile#L14-L21) 揭示了运行前提：

```makefile
CFLAGS := -Wall -fPIC -I$(VCS_HOME)/include -DVCS_VERSION=$(VCS_VERSION) `pkg-config --cflags libzmq`
LFLAGS := -lrt -pthread `pkg-config --libs libzmq`
LIB    := $(OUT)/libAxiSim.so
```

即需要 VCS 头文件（`VCS_HOME`、`VCS_VERSION`）和 libzmq。`VCS_VERSION` 宏还影响 [VhpiGeneric.c:145-149](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.c#L145-L149) 里 `vhpi_register_cb` 的调用形式（≥2016 版用 `vhpiReturnCb`）。

#### 4.1.4 代码实践

**实践目标**：在不依赖 VCS 的前提下，靠源码阅读建立“一次时钟沿的 C 执行轨迹”的完整心智模型。

**操作步骤**：

1. 打开 `axi/simlink/src/RogueTcpMemory.c`，定位 `RogueTcpMemoryUpdate`（第 242 行）。
2. 在脑中（或纸面）跟踪一次“复位释放后的第一个上升沿”：`getInt(s_reset)` 从 1 变 0，`data->port==0` 为真，于是 `data->port = getInt(s_port)` 读到 `portNum`，调用 `RogueTcpMemoryRestart` 绑定 ZMQ 端口。
3. 对照 [VhpiGeneric.c:154-208](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/VhpiGeneric.c#L154-L208) 的 `VhpiGenericInit`，确认 `s_clock`/`s_reset`/`s_port` 三个输入的方向与位宽（1/1/16）被正确登记。

**需要观察的现象**：`port==0` 这个守卫意味着端口绑定**只发生一次**（首次进入非复位的数据搬运分支），之后 `port` 已是非零值，不再重复 bind。

**预期结果**：你能画出“时钟沿 → 回调 → Update → 读 ZMQ/写 AXI 信号 → 回调收尾写回 VHDL”的闭环。若手头没有 VCS，标注「待本地验证」实际运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 GHDL 版的 `RogueTcpMemory` 要保留完全相同的端口声明、只注释掉 `FOREIGN`？
**答**：这样测试台和上层封装（`RogueTcpMemoryWrap`）可以无条件例化该实体；GHDL 分析时端口匹配通过、不报错，只是实体不驱动信号。保持端口一致让同一套测试台能在 VCS（真协同仿真）与 GHDL（占位）下都编译通过，符合 u1-l3 的工具分流约定。

**练习 2**：`VhpiGenericCallBack` 注册在 clock 信号上，那它在每个时钟周期触发几次？`Update` 又如何区分上升沿？
**答**：clock 每次跳变（上升、下降）都触发一次回调，故每周期 2 次。`Update` 用 `if (data->currClk != getInt(s_clock))` 检测到跳变后，再判断 `if (data->currClk)`（即新值是否为 1）来识别上升沿，下降沿直接被忽略。

---

### 4.2 内存映射桥：RogueTcpMemory（AXI-Lite）

#### 4.2.1 概念说明

`RogueTcpMemory` 让 Python 端像访问真实 PCIe/UDP 寄存器空间一样，读写仿真器里的 AXI-Lite 从机。它在 C 侧实现了一个完整的 AXI-Lite **主端状态机**：收到 Python 的事务请求后，按拍驱动 AR/AW/W 通道，等 R/B 通道响应，再把结果打包回 Python。本质上它是一个“用 ZMQ 远程驱动的 AXI-Lite Master”。

注意它与 u8-l3 的 I2C/SPI 桥不同：那些桥是 AXI-Lite **从机**（CPU 访问一段窗口、桥翻译成串行事务）；而 `RogueTcpMemory` 是 AXI-Lite **主机**（Python 驱动它去访问仿真里的从机）。方向相反。

#### 4.2.2 核心流程

**ZMQ 报文格式**（记住帧数与字段，这是协议核心）：

请求帧（Python → 仿真器，发到 `port` 的 PULL 套接字），按顺序：

| 帧 | 字段 | 字节数 | 含义 |
|----|------|--------|------|
| 0 | id | 4 | 事务 ID（回显用） |
| 1 | addr | 8 | 64 位起始地址 |
| 2 | size | 4 | 字节数（读：可为 4；写：等于数据长度） |
| 3 | type | 4 | 操作码 |
| 4 | data | size | 写数据（仅写事务有） |

读事务是 4 帧，写事务是 5 帧。操作码（[RogueTcpMemory.h:48-51](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.h#L48-L51)）：`T_READ=0x1`、`T_WRITE=0x2`、`T_POST=0x3`、`T_VERIFY=0x4`。

响应帧（仿真器 → Python，发到 `port+1` 的 PUSH 套接字），固定 **6 帧**：id / addr / size / type / data / **result**（最后一帧是 AXI 响应码 bresp/rresp）。

**状态机**（[RogueTcpMemory.h:53-58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.h#L53-L58)）：

```
ST_IDLE ──收到ZMQ请求──► ST_START
                            │
            ┌───────────────┴───────────────┐
        WRITE/POST                        READ
            │                               │
            ▼                               ▼
        驱动 AW+W                        驱动 AR
            │                               │
            ▼                               ▼
        ST_WRESP                        ST_RADDR ──arready──► ST_RDATA
        (收 bvalid)                                       (收 rvalid)
            │                                               │
            └────────────┬──────────────────────────────────┘
                  curr==size?
                  是 → Send(回Python) → ST_IDLE
                  否 → ST_PAUSE → (等 rvalid/bvalid 落) → ST_START（下一拍）
```

关键点：`size` 是**字节数**，每拍搬 4 字节（32 位 AXI-Lite），地址每拍自增 `curr`，所以一次请求会被拆成 `size/4` 个 AXI-Lite 拍。`curr` 是字节偏移。

#### 4.2.3 源码精读

**ZMQ 端口绑定——两个端口**

[RogueTcpMemory.c:38-54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L38-L54) 创建 PULL/PUSH 套接字并绑定到相邻两端口：

```c
data->zmqCtx  = zmq_ctx_new();
data->zmqPull = zmq_socket(data->zmqCtx, ZMQ_PULL);   // 收：绑 port
data->zmqPush = zmq_socket(data->zmqCtx, ZMQ_PUSH);   // 发：绑 port+1
snprintf(buffer, sizeof(buffer), "tcp://127.0.0.1:%i", data->port);
zmq_bind(data->zmqPull, buffer);
snprintf(buffer, sizeof(buffer), "tcp://127.0.0.1:%i", data->port+1);
zmq_bind(data->zmqPush, buffer);
```

**接收 Python 请求（非阻塞轮询）**

[RogueTcpMemory.c:109-157](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L109-L157) 用 `ZMQ_DONTWAIT` 轮询 PULL 套接字，逐帧拼装。`do...while(more)` 循环依靠 `ZMQ_RCVMORE` 判断是否还有后续帧，把多帧消息收到 `msg[0..4]`：

```c
if (zmq_recvmsg(data->zmqPull, &(msg[x]), ZMQ_DONTWAIT) > 0) {
    if ( x != 4 ) x++;
    msgCnt++;
    more = 0; moreSize = 8;
    zmq_getsockopt(data->zmqPull, ZMQ_RCVMORE, &more, &moreSize);
}
```

收到后校验帧数（读 4 帧、写 5 帧）与各帧尺寸，把 id/addr(8B)/size/type 拷进结构体、写数据拷进 `data->data[]`，置 `state = ST_START`。注意 `addr` 是 **64 位**（`uint64_t addr`，[RogueTcpMemory.h:90](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.h#L90)），但 AXI-Lite 线宽只有 32 位——下文会看到高 32 位被自然截断。

**状态机主体——上升沿内分发**

[RogueTcpMemory.c:270-303](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L270-L303) 是 `ST_START` 分支，区分写/读两条路径：

```c
case ST_START:
    if ( data->type == T_WRITE || data->type == T_POST ) {     // 写路径
        setInt(s_awaddr, (data->addr+data->curr));             // 地址自增
        setInt(s_awvalid, 1);
        setInt(s_bready, 1);
        data32  = data->data[data->curr++];         // 取 4 字节，小端拼装
        ... // 低/次/高/最高字节
        setInt(s_wdata, data32);
        setInt(s_wstrb, 0xF);
        setInt(s_wvalid, 1);
        data->state = ST_WRESP;
    } else {                                                    // 读路径
        setInt(s_araddr, (data->addr+data->curr));
        setInt(s_arvalid, 1);
        setInt(s_rready, 1);
        data->state = ST_RADDR;
    }
```

可以看到：地址每拍加 `curr`（字节偏移），每拍 `curr` 自增 4，数据按**小端**拼装（低字节在低位）。

**收响应——ST_WRESP 与 ST_RDATA**

[RogueTcpMemory.c:307-321](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L307-L321)（写响应）：等 `bvalid`，把 `bresp` 存为 `result`，`curr==size` 则 `Send` 回 Python 并回 `ST_IDLE`，否则进 `ST_PAUSE`。读路径 [RogueTcpMemory.c:334-352](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L334-L352) 类似：等 `rvalid`，把 `rdata` 按小端拆 4 字节写回 `data->data[]`，存 `rresp` 为 `result`。

`ST_PAUSE`（[RogueTcpMemory.c:355-359](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L355-L359)）等 `rvalid` 与 `bvalid` 都落下再回 `ST_START`，避免上一拍的 valid 还没撤就发下一拍，保证握手干净。

**回送 Python——6 帧响应**

[RogueTcpMemory.c:62-89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L62-L89) 依次初始化 id/addr/size/type/result(在 msg[5]) + data(msg[4]) 六个消息帧，前 5 帧带 `ZMQ_SNDMORE`、末帧不带，把完整事务回送给 Python，并复位 `state=0`（IDLE）、`curr=0`。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的报文格式与 4.2.3 的状态机对上号，验证“一次 4 字节读 = 恰好经历 IDLE→START→RADDR→RDATA→Send→IDLE”。

**操作步骤**：

1. 假设 Python 发起一次读：`type=T_READ(1)`、`addr=0x0000_F000`、`size=4`。这是 4 帧 ZMQ 请求。
2. 在 [RogueTcpMemory.c:125-157](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L125-L157) 确认 `msgCnt==4` 命中、`state=ST_START`。
3. 跟踪 `ST_START` 走 else 分支：`araddr = addr+0 = 0xF000`（注意 64 位 addr 在 32 位线上被截成 `0x0000F000`），`arvalid=1`，`state=ST_RADDR`。
4. 跟踪 `ST_RADDR`：`arready` 拉高后撤 `arvalid`，进 `ST_RDATA`。
5. 跟踪 `ST_RDATA`：`rvalid` 拉高后，读 `rdata` 存进 `data->data[0..3]`，`curr` 变 4。`curr==size(4)` 成立 → `Send` 回 Python（6 帧，data 帧含 4 字节，result 帧 = rresp）→ 回 `ST_IDLE`。

**需要观察的现象**：`size=4` 时仅一拍即完成；若 `size=8`，则 `ST_RDATA` 后 `curr(4)!=8` 会走 `ST_PAUSE → ST_START`，第二拍 `araddr=0xF004`。

**预期结果**：你能列出每个状态在每拍的 `arvalid/araddr/rready` 取值。实际运行需 VCS + libzmq，否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ZMQ 请求里 `addr` 是 8 字节、而 AXI-Lite 的 `araddr` 只有 32 位？多余的高位去哪了？
**答**：`setInt(s_araddr, data->addr+data->curr)` 把 64 位值赋给 32 位的 `intValue`，高 32 位被自然截断。这保留了与 Rogue 通用内存接口（支持 64 位地址）的兼容，但在 AXI-Lite 桥里实际只用低 32 位。

**练习 2**：`T_POST`（posted write）在 AXI 协议里通常意味着“无响应”。但本桥的 `ST_START` 里 `T_POST` 与 `T_WRITE` 走完全相同的写路径、最终也 `Send` 了响应。这说明什么？
**答**：说明本桥**没有**真正实现 posted-write 的“无响应”语义，只是把 `T_POST` 当作普通写来处理并照常回 `result`。type 字段更多是一个透传给 Python 的标签，真正的差异在 Python/Rogue 侧如何对待这个响应。读源码时不要被名字误导。

---

### 4.3 流桥：RogueTcpStream（AXI-Stream / SSI）

#### 4.3.1 概念说明

`RogueTcpStream` 桥接的是**整帧** AXI-Stream 数据（而非单拍寄存器）。它的两端是对称的帧通道：

- **inbound（ib\*）**：DUT 发出的帧（`ibValid/ibData*/ibKeep/ibLast` 是输入）→ C 侧逐拍收齐一帧 → 整帧 ZMQ 推给 Python。
- **outbound（ob\*）**：Python 注入的帧 → ZMQ 收到后存在 C 侧缓冲 → 逐拍驱动 `obValid/obData*/obKeep/obLast` 喂给 DUT。

内部固定使用 **8 字节（64 位）数据宽度**、每字节一个 TUSER 位、TKEEP_NORMAL 模式。它还理解 **SSI 侧带**：当 `ssi=1` 时，会在首拍 TUSER 标 SOF、末拍标 EOF/EOFE，这与 u5-l1 的 SSI 帧语义对齐。

#### 4.3.2 核心流程

**ZMQ 报文格式**（流桥用 4 帧，与内存桥不同）：

发往 Python（inbound 收齐一帧后，[RogueTcpStream.c:67-94](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L67-L94)）：

| 帧 | 字段 | 字节数 | 含义 |
|----|------|--------|------|
| 0 | flags | 2 | 低字节=首拍 user(fuser)，高字节=末拍 user(luser) |
| 1 | chan | 1 | 通道号（桥内固定 0，多通道由上层 Wrap 处理） |
| 2 | err | 1 | 错误位（SSI 下取自 luser 的 EOFE 位） |
| 3 | data | N | 整帧字节 |

来自 Python（outbound，[RogueTcpStream.c:142-167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L142-L167)）同样是这 4 帧，`msgCnt==4` 才视为有效。

**每拍的帧组装逻辑**（`Update` 内，上升沿）：

```
inbound:
  if (ibValid):
      首拍 → 记 ibFuser = userLow & 0xFF
      逐字节(0..7): if keep位 → 追加 ibData[], 更新 ibLuser
      if (ibLast) → RogueTcpStreamSend（整帧发 Python）

outbound:
  if (obSize==0) → RogueTcpStreamRecv（轮询，收一帧进 obData[]）
  if (obValid==0 且 obSize>0):
      首拍 → obUserLow = obFuser; 否则 0
      逐字节(0..7): 拼 dLow/dHigh、设 keep、末字节置 obLuser
      obValid = 1
      若 obCount>=obSize → obLast=1，清缓冲
```

SSI 位注入在收向 Python 时：`flags = fuser | (luser<<8)`，`err = luser & 0x1`；在收自 Python 时：`obFuser |= 0x02`（首拍 user 的 SOF 位），`err` 则置 `obLuser |= 0x01`（EOFE 位）。

#### 4.3.3 源码精读

**实体端口——ob/ib 两组**

[RogueTcpStream.vhd:27-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/sim/RogueTcpStream.vhd#L27-L43) 的端口分成 ob（C 驱动，out）和 ib（C 接收，in）。注意数据被拆成 Low/High 各 32 位（合 64 位），User 也拆成 Low/High 各 32 位——因为 VHPI 的 `intValue` 是 32 位单元，64 位总线要拆两段处理。

**inbound：逐拍拼帧**

[RogueTcpStream.c:294-318](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L294-L318) 在 `ibValid` 时，按 TKEEP 把有效字节压进 `ibData[]`：

```c
for (x=0; x< 8; x++) {
    if ( x < 4 ) {
        data->ibData[data->ibSize] = (dLow >> (x*8)) & 0xFF;
        if ( (keep >> x) && 1 ) data->ibLuser = (uLow >> (x*8)) & 0xFF;
    } else {
        data->ibData[data->ibSize] = (dHigh >> ((x-4)*8)) & 0xFF;
        if ( (keep >> x) && 1 ) data->ibLuser = (uHigh >> ((x-4)*8)) & 0xFF;
    }
    if ( (keep >> x) && 1 ) data->ibSize++;
}
if ( getInt(s_ibLast) ) RogueTcpStreamSend(data, portData);   // 帧末拍 → 发整帧
```

这里 `ibLuser` 跟踪“最后一个有效字节对应的 user”，这样末拍的 EOFE/SOF 信息能准确落位。注意只有 `keep` 有效的字节才计入，所以帧的 `ibSize` 是**有效字节数**而非拍的累计。

**outbound：逐拍拆帧驱动**

[RogueTcpStream.c:330-375](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L330-L375) 把 `obData[]` 每拍取 8 字节拼成 dLow/dHigh，并据剩余字节数生成 keep、在首/末字节写 fuser/luser：

```c
for (x=0; x< 8; x++) {
    if ( x < 4 ) dLow |= (data->obData[data->obCount] << (x*8));
    else         dHigh |= (data->obData[data->obCount] << ((x-4)*8));
    data->obCount++;
    if ( data->obCount <= data->obSize ) keep |= (1 << x);   // 有效字节置 keep
}
...
if ( data->obCount >= data->obSize ) { setInt(s_obLast, 1); ... }  // 帧末
```

**SSI 侧带的编解码**

[RogueTcpStream.c:79-86](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L79-L86)（发往 Python 时打包）：`flags = ibFuser | (ibLuser<<8)`，`err = ibLuser & 0x1`。[RogueTcpStream.c:164-167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L164-L167)（收自 Python 时解包 + 注入 SSI 位）：

```c
if ( data->ssi ) {
    data->obFuser |= 0x02;          // 首拍 user 的 SOF 位
    if ( err ) data->obLuser |= 0x01;  // 末拍 user 的 EOFE 位
}
```

即 SSI 模式下，桥会自动给注入帧的首拍打上 SOF、把错误标志翻成 EOFE——这正是 u5-l1 讲的“SOF/EOF/EOFE 编码进 TUSER”。

**工程里真正被例化的封装**

[RogueTcpStreamWrap.vhd:49-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/tb/RogueTcpStreamWrap.vhd#L49-L56) 定义了桥内部固定的 8 字节配置：

```vhdl
constant INT_CONFIG_C : AxiStreamConfigType := (
   TSTRB_EN_C    => false,
   TDATA_BYTES_C => 8,          -- 64 位
   TDEST_BITS_C  => 8,
   TKEEP_MODE_C  => TKEEP_NORMAL_C,
   TUSER_BITS_C  => 8,          -- 每字节 1 位 user
   TUSER_MODE_C  => TUSER_NORMAL_C);
```

Wrap 用 `AxiStreamResize`（u4-l2）在用户的 `AXIS_CONFIG_G` 与 `INT_CONFIG_C` 之间做位宽整形，单通道时直连、多通道时配 `AxiStreamDeMux`/`AxiStreamMux`（u4-l3）按 TDEST 路由，每个通道一个 `RogueTcpStream` 实例、端口随通道号递增（[RogueTcpStreamWrap.vhd:106-108](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/tb/RogueTcpStreamWrap.vhd#L106-L108)：`portMap(i) = PORT_NUM_G + CHAN_MAP_C(i)*2`）。

#### 4.3.4 代码实践

**实践目标**：对照真实工程用法，看清“DUT 发一帧 → 整帧进 Python”的逐拍字节流。

**操作步骤**：

1. 打开 [protocols/pgp/pgp3/core/tb/RoguePgp3Sim.vhd:94-110](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/tb/RoguePgp3Sim.vhd#L94-L110)。这里为每个 VC 例化一个 `RogueTcpStreamWrap`，`SSI_EN_G=true`、`CHAN_COUNT_G=1`、`PORT_NUM_G = PORT_NUM_G + i*2`。
2. 假设 DUT 在某 VC 上发一帧 12 字节（两拍：第一拍 8 字节全有效 keep=0xFF，第二拍 4 字节有效 keep=0x0F、tLast=1）。
3. 在 [RogueTcpStream.c:294-318](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L294-L318) 推演：第一拍 ibSize 从 0→8；第二拍 ibSize 从 8→12，且 `ibLast` 命中 → 调 `RogueTcpStreamSend`。
4. 在 [RogueTcpStream.c:67-107](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpStream.c#L67-L107) 确认发往 Python的是 4 帧：flags(2)/chan(1)/err(1)/data(12)。

**需要观察的现象**：只有 `keep` 置位的字节才进 `ibData`，所以 ZMQ data 帧长度（12）= 有效字节数，而不是按拍的 8×2=16。

**预期结果**：你能在 Python 侧用 Rogue 收到恰好 12 字节、且 flags 的低字节含首拍 SOF user。实际运行需 VCS，否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：流桥内部为什么固定用 8 字节（64 位）宽度，而不是沿用用户配置的 `AXIS_CONFIG_G`？
**答**：因为 C 侧的 `getInt/setInt` 以 32 位为单位、64 位总线要拆 Low/High 两段硬编码处理（见实体端口）。固定 8 字节让 C 代码的字节循环 `for x in 0..7` 写死，简化实现；位宽适配交给 VHDL 侧的 `AxiStreamResize`（Wrap 内），实现了“C 端简单、VHDL 端灵活”的分工。

**练习 2**：`ibLuser` 为什么不是简单地取最后一拍的 `ibUserLow/High`，而要在字节循环里随 `keep` 逐字节更新？
**答**：末拍可能只有部分字节有效（如 keep=0x0F 只剩低 4 字节），EOFE 等 SSI 标记落在“最后一个**有效**字节”的 user 位上。若直接取整拍 user，会取到无效字节通道上的错误值。逐字节按 `keep` 更新才能精准锁定末有效字节的 user。

---

## 5. 综合实践

**任务**：完整描述一次「Python Rogue 脚本读寄存器」的端到端往返，把本讲三个模块串起来。这是一个源码阅读 + 数据流跟踪型实践（无需运行 VCS）。

设定：测试台用 `RogueTcpMemoryWrap`（`PORT_NUM_G=9000`）把仿真器里的某 AXI-Lite 从机接到 Rogue；Python 端执行 `dev.MyReg.get()`，读地址 `0x0000_0010`、4 字节。

请按下列检查点逐处对照源码填写每个字段/信号的值：

1. **Python → ZMQ**：Rogue 的 Memory 设备向 `tcp://127.0.0.1:9000`（PULL 端）发送请求。写出 4 个帧的字段与字节：id（任意）、addr=`0x0000000000000010`（8 字节）、size=`4`、type=`0x1`(T_READ)。
2. **ZMQ → C（Recv）**：对照 [RogueTcpMemory.c:109-157](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L109-L157)，确认 `msgCnt==4`、`data->addr=0x10`、`data->size=4`、`data->type=1`、`state=ST_START`。
3. **C 驱动 AXI-Lite（ST_START → RADDR → RDATA）**：对照 [RogueTcpMemory.c:297-352](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L297-L352)，逐拍列出 `araddr=0x10`、`arvalid`、`rready`、收 `rvalid` 后把 `rdata` 小端拆进 `data->data[0..3]`。
4. **C → ZMQ（Send）**：对照 [RogueTcpMemory.c:58-94](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/simlink/src/RogueTcpMemory.c#L58-L94)，确认回送 6 帧（id/addr/size/type/data(4)/result=rresp），经 `port+1=9001`（PUSH 端）发回 Python。
5. **ZMQ → Python**：Rogue 解析 data 帧得到 4 字节寄存器值，`get()` 返回。

**延伸思考**：

- 若把读改成写 `dev.MyReg.set(0xDEADBEEF)`，请求变 5 帧（多了 data 帧），状态机走 `ST_START → ST_WRESP`，回送仍 6 帧。请自行跟踪。
- 若同时还要传流数据（如 PGP 帧自检），则 `RogueTcpMemoryWrap`（寄存器）与 `RogueTcpStreamWrap`（流）会**各自独立**占用一对端口，互不干扰——这就是 [RoguePgp3Sim.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/tb/RoguePgp3Sim.vhd) 里寄存器端口与每 VC 流端口分开编号的原因。

> 说明：以上为源码级跟踪。要真正在波形里看到 ZMQ 报文与 AXI 信号联动，需要 Synopsys VCS + 编译好的 `libAxiSim.so`（`src/Makefile`，需 `VCS_HOME`、`VCS_VERSION`、libzmq）+ Python Rogue 环境三者齐备；若本地不具备，请标注「待本地验证」。

## 6. 本讲小结

- `axi/simlink/` 用 **VHDL 空壳实体 + `FOREIGN` 属性 + C 实现**，把仿真器的 AXI-Lite / AXI-Stream 接口经 **ZeroMQ** 桥接到 Python Rogue，实现软硬件协同仿真。
- **VhpiGeneric** 是三套桥共用的框架：在 clock 信号上注册值变化回调，每拍执行“读端口 → enum 转 int → 调 Update → int 转 enum → 写端口”；业务逻辑只需写 `Update`。
- **RogueTcpMemory** 是 AXI-Lite **主机**：收 4/5 帧请求（id/addr(8B)/size/type[/data]），用 7 态状态机逐拍驱动五通道，回送 6 帧响应；地址随字节偏移自增，每拍搬 4 字节。
- **RogueTcpStream** 是 AXI-Stream **帧桥**：inbound 逐拍按 TKEEP 拼帧、整帧 4 帧（flags/chan/err/data）发 Python，outbound 反向逐拍拆帧驱动；内部固定 64 位、理解 SSI 的 SOF/EOFE 侧带。
- **GHDL 不支持 VHPI**，故 `ghdl/` 提供端口一致的占位实体，`ruckus.tcl` 据此在 `sim/` 与 `ghdl/` 间分流；真正运行需 VCS 编出 `libAxiSim.so`。
- 工程里实际例化的是 `RogueTcpMemoryWrap` / `RogueTcpStreamWrap`（含位宽整形与多通道路由），真实用法范例见 `RoguePgp3Sim.vhd`。

## 7. 下一步学习建议

- **横向补全第三种桥**：阅读 `axi/simlink/src/RogueSideBand.c` 与 `tb/RogueSideBandWrap.vhd`，它的 9 端口 opCode/remData 桥结构与本讲两桥同构，是巩固 VHPI 模式的好练习。
- **结合 PGP 协议栈**：本讲是 u7（PGP/RSSI/JESD204B）的仿真支撑层。建议在学完 u7-l1（PGP3）后，回到 `RoguePgp3Sim.vhd`，把它作为“PGP 链路的软件对端”整体读一遍，体会 simlink 如何让 PGP 帧自检无需真实光纤。
- **回到软件镜像**：u9 的 PyRogue 篇会讲 Python 侧的设备模型（`RemoteVariable` 等）。本讲解释了“Python 如何经 ZMQ 到达仿真里的寄存器”，u9 将解释“Python 侧的变量如何映射到那些寄存器地址”，二者合起来才是完整的 Rogue 闭环。
- **构建与 CI 视角**：可对照 u1-l2 的 GHDL/CI 讨论理解为何 simlink 的 cocotb 回归依赖 VCS 而非 GHDL——这是仓库把 VHPI 模块隔离在 `sim/`、并用 `ghdl/` 占位的根本原因。
