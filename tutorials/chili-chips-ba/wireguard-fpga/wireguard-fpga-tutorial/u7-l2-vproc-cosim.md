# VProc 虚拟处理器协同仿真

## 1. 本讲目标

本讲承接 u7-l1（仿真测试台总体架构），深入测试台里最关键的那块「可替换 soc_cpu」——**VProc 虚拟处理器**。学完后你应当能够：

- 说清 VProc 是什么、它为什么能让「主机上原生编译的 C++ 程序」驱动 HDL 里的 `soc_if` 总线；
- 读懂 `VUserMain0.cpp` 里 `write` / `read` / `tick` 三个 API 的含义与调用约定，并理解它们如何经 `soc_cpu.VPROC` 的少量组合逻辑翻译成一次真实的总线事务；
- 认识协同仿真 HAL（`csr_cosim.h`）这一层抽象：同一份应用源码，靠一个 `VPROC` 宏就能在「真硬件 CSR 访问」与「VProc 事务」之间切换。

一句话定位：**VProc 让你在仿真里用一个「假 CPU」跑「真测试程序」，把控制面的软件逻辑连同它对 CSR 的访问，整体协同仿真起来。**

## 2. 前置知识

本讲默认你已经读过 u7-l1，知道：

- `tb.sv` 是测试台，`top` 是被测设计（DUT），二者用 BFM 连接；
- `soc_cpu` 是 `top` 里的 CPU 子系统，仿真时可以用 VProc 顶替真实的 picoRV32 RTL；
- 控制面总线 `soc_if` 是一条 vld/rdy 握手总线（见 u2-l4）。

补充几个本讲要用到的术语：

- **协同仿真（co-simulation）**：HDL 仿真器（本项目用 Verilator）与一段运行在主机上的 C/C++ 程序「同呼吸、共进退」地一起跑，HDL 里的某个模块由这段 C 程序从背后驱动，而不是用 RTL 实现。
- **DPI-C**：SystemVerilog 直接编程接口（Direct Programming Interface），是 Verilator 让 HDL 与 C/C++ 互调的标准通道。VProc 正是靠它把「主机线程」与「仿真时间」缝在一起。
- **node（节点）**：VProc 允许在一个仿真里实例化多个虚拟处理器，每个用一个整数 `node` 区分。WireGuard 里 `soc_cpu` 固定为 node 0，四个以太网 VIP（u7-l4 会讲）是 node 1–4。
- **delta cycle（增量周期）**：VProc API 的一个高级参数，本项目恒置 0（不使用），可先忽略。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [4.sim/models/soc_cpu.VPROC.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv) | VProc 的 HDL 外壳：把 VProc 的通用存储映射总线翻译成本项目自有的 `soc_if` 协议，并附带一个 `mem_model` 当 IMEM。 |
| [4.sim/usercode/VUserMain0.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMain0.cpp) | node 0 的用户程序入口，演示 `write`/`read`/`tick` 的最简用法。 |
| [4.sim/models/cosim/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/README.md) | VProc + mem_model 协同仿真组件的总说明。 |
| [4.sim/models/cosim/include/VProcClass.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VProcClass.h) | VProc 的 C++ API 封装类 `VProc`。 |
| [4.sim/models/cosim/include/VUser.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VUser.h) | VProc 的 C API 原型与 `GO_TO_SLEEP` 等常量。 |
| [3.build/csr_build/generated-files/csr_cosim.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h) | 自动生成的协同仿真 HAL：把每个 CSR 字段包成调用 VProc API 的 C++ 类。 |
| [3.build/csr_build/generated-files/wireguard_regs.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/wireguard_regs.h) | 用 `VPROC` 宏在硬件 HAL 与协同仿真 HAL 之间二选一的顶层头。 |
| [4.sim/MakefileVProc.mk](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk) | 编排「用户代码 → libuser.a / libvproc.a → Verilator 可执行仿真」整条链的 Makefile。 |

---

## 4. 核心概念与源码讲解

### 4.1 VProc 虚拟处理器与 VProc API

#### 4.1.1 概念说明

先建立一个直觉：真实的 picoRV32 是一段 RTL，它每个时钟周期从 IMEM 取指、译码、访存，把读写事务打到 `soc_if` 总线上。**VProc 把这个过程「掏空」**——HDL 里只留一个空壳模块 `VProc`，它本身不会取指，而是等着主机上的 C++ 程序通过 DPI-C 「下指令」：你要写哪个地址、要读哪个地址、要空转几个周期。HDL 这一侧只负责把这些请求翻译成总线时序。

这样做的好处写在 [4.sim/models/cosim/README.md:L12-L21](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/README.md#L12-L21)：一个**原生编译**（直接用主机的 gcc/g++ 编，而不是交叉编译到 RISC-V）的测试程序，可以在 HDL 处理器组件上「运行」，用 C/C++ API 发起读写事务、推进仿真时间。对 WireGuard 而言，VProc 的通用存储映射总线被一层很薄的逻辑翻译成了本地 `soc_if` 接口。

> 为什么叫「协同」仿真？因为主机线程与仿真器必须**步调一致（lock-step）**：用户程序在「仿真时间」意义上跑得无限快，只有当它调用一次 `write`/`read`/`tick` 时，才把控制权交还仿真器、让仿真时间前进。每调用一次 API，就是一个同步点。这点很关键，下面会反复用到。

#### 4.1.2 核心流程

VProc 提供 C 与 C++ 两套 API。WireGuard 用的是 C++ 封装类 `VProc`（[VProcClass.h:L36-L110](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VProcClass.h#L36-L110)），它把每个方法都转发到底层 C 函数。三个最常用的方法：

```text
构造  VProc vp0(node)            // 绑定到哪个 node（soc_cpu=0）
写    vp0.write(addr, data)      // → VWrite(addr, data, delta=0, node)
读    vp0.read(addr, &data)      // → VRead (addr, &data, delta=0, node)
推进  vp0.tick(n)                // → VTick(n, node)：让仿真前进 n 个时钟周期
```

一次 `write` 的内部流程（伪代码）：

```text
用户线程: 把 {addr, data, WRITE} 放进交换结构 → 信号量通知仿真器 → 阻塞等待
仿真器  : 在本 node 的 VProc HDL 上拉起 WE/Addr/DataOut → 等总线应答 WRAck → 把结果回填 → 唤醒用户线程
用户线程: 被唤醒，write() 返回（仿真时间已前进 ≥1 周期）
```

`read` 同理，只是方向相反，并把读回的 `data_in` 交给用户。`tick(n)` 不发起任何总线事务，单纯让仿真前进 n 个时钟周期——常用来「等一会儿」让 DUT 把状态跑起来。

底层 C 原型见 [VUser.h:L70-L76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VUser.h#L70-L76)，两个常用常量见 [VUser.h:L32-L33](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VUser.h#L32-L33)：

- `GO_TO_SLEEP = 0x7fffffff`：传给 `tick`，表示「本 node 长睡」，不再产生总线流量，但仿真可继续。
- `DELTA_CYCLE = -1`：delta 周期模式，本项目不用。

> 地址口径提醒：`write`/`read` 的 `addr` 形式上是「无符号整数」，对 WireGuard 而言它是**字节地址**（见 [4.sim/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md) 的 API 说明）。`delta` 参数恒取默认 0。

#### 4.1.3 源码精读

C++ 封装类把方法一行映射到底层 C 函数。[VProcClass.h:L43-L44](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VProcClass.h#L43-L44) 的 `write`/`read`：

```cpp
int write (const unsigned addr, const unsigned data, const int delta=0)
    { return VWrite(addr, data, delta, node); };
int read  (const unsigned addr, unsigned *data, const int delta=0)
    { return VRead (addr, data, delta, node); };
```

`node` 是构造时绑定的成员（[VProcClass.h:L98](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VProcClass.h#L98)），所以一个 `VProc` 对象天然只服务于一个 node。

除了整字读写，类还提供了更细粒度的字节/半字访问，全部走带字节使能的 `VWriteBE`：见 [VProcClass.h:L46-L63](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VProcClass.h#L46-L63)。例如 `writeWord` 用 `be=0xf` 写整字，`writeByte` 根据 `byteaddr` 的低 2 位算出位移与掩码。`tick` 与 burst 版本在 [VProcClass.h:L65-L67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VProcClass.h#L65-L67)。这些细粒度方法在 WireGuard 的「裸测试程序」里并不直接用，但协同仿真 HAL（4.3 节）会大量用到 `VWriteBE`。

#### 4.1.4 代码实践

**目标**：用「源码阅读 + 心算」建立对 API 的直觉，不依赖上板。

**步骤**：

1. 打开 [VProcClass.h:L43-L67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VProcClass.h#L43-L67)。
2. 追踪一次 `vp0.writeWord(0x20000049, 0xAB)` 的展开：它会调用
   `VWriteBE(0x20000048, 0xAB << 8, 0x2, 0, 0)`。
3. 解释三件事：为什么字节地址 `0x...49` 被对齐成 `0x...48`？为什么数据左移 8 位？为什么字节使能 `be=0x2`（二进制 `0010`）？

**需要观察的现象（心算即可）**：

- `0x49 & 0x3 = 1`，故位移 `8*1 = 8` 位，数据落在字内的第 1 字节；
- 对齐后字地址 `0x20000048`，其低 2 位被清零，对应 `bus.addr = vp_addr[31:2]`（4.2 节会看到）；
- `be=0x2` 选中字内第 1 字节。

**预期结果**：你能说清「字节地址 → 字地址 + 字内位移 + 字节使能」这一整套换算，这正是 4.3 节 HAL 自动生成代码要反复做的事。

#### 4.1.5 小练习与答案

**练习 1**：`vp0.tick(0)` 与 `vp0.tick(GO_TO_SLEEP)` 行为有何不同？
**答案**：`tick(0)` 推进 0 个周期但仍是一次同步点（控制权会切到仿真器再切回）；`tick(GO_TO_SLEEP)`（值为 `0x7fffffff`）让本 node 长期睡眠、不再发总线事务，但仿真本身继续运行——这正是程序末尾 `while(true) vp0->tick(GO_TO_SLEEP)` 的作用。

**练习 2**：为什么 `read` 的 `data` 参数是指针、而 `write` 是值？
**答案**：`read` 要把仿真器读回的数据「带出来」，故用输出指针（`unsigned *data`）；`write` 只把数据送进去，用值即可。

---

### 4.2 soc_cpu.VPROC 总线适配与 VUserMain0 入口

#### 4.2.1 概念说明

VProc 自身只有一条「通用存储映射总线」（`Addr`/`DataOut`/`WE`/`BE`/`DataIn`/`RD`/`WRAck`/`RDAck`）。WireGuard 的控制面用的却是自有协议 `soc_if`（vld/rdy/we/addr/wdat/rdat，见 u2-l4）。二者之间需要一个适配器——这就是 `soc_cpu.VPROC.sv`。

这层适配之所以重要，是因为它让 VProc 与真实的 picoRV32 **端口完全一致**：二者都叫 `module soc_cpu`、都暴露 `soc_if.MST bus` 加一组 IMEM 烧写口。所以 u7-l1 讲的「Makefile 用 `sed` 把 `top.filelist` 里 `ip.cpu` 那几行删掉、用 `soc_cpu.VPROC` 顶替」才能成立——**插拔式（plug-and-play）**，DUT 的其余部分（fabric、CSR、DMEM）一行不改。

至于「程序入口」，仿真器自己的 `main()` 已经存在，且一个仿真里可能有多个 VProc node，所以**每个 node 有一个形如 `VUserMain<n>` 的入口**，node 0 即 `VUserMain0`，用它取代普通程序的 `main`。这要求 `VUserMain0` 必须是 **C 链接**（`extern "C"`），因为调用它的 VProc 层与 DPI-C 都是 C。

#### 4.2.2 核心流程

地址空间（[soc_cpu.VPROC.sv:L45-L57](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L45-L57)）：

```text
0x0000_0000 - 0x0FFF_FFFF  IMEM / 程序空间   (由 soc_cpu.VPROC 内的 mem_model 承载)
0x1000_0000 - 0x1FFF_FFFF  DMEM / 数据空间   (经 soc_if → soc_fabric → u_dmem)
0x2000_0000 - 0x3FFF_FFFF  CSR               (经 soc_if → soc_fabric → u_soc_csr)
```

关键判别位是 `vp_addr[31:28]`（[soc_cpu.VPROC.sv:L127](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L127)）：为 `0` 即落在 IMEM，访问本地的 `mem_model`；非 `0` 则打到 `soc_if` 总线，由 `soc_fabric` 译码到 DMEM 或 CSR。

一次用户 `write(0x20000048, val)` 的完整旅程：

```text
VUserMain0: vp0->write(addr,wdat)
   └─(DPI-C)→ VProc HDL: 拉 vp_we=1, vp_addr, vp_wdat, vp_be
        └─ soc_cpu.VPROC 组合逻辑:
             bus.we   = vp_be & {4{vp_we}}        (字节使能与写标志合成)
             bus.vld  = (vp_we|vp_rd) & ~cpu_access
             bus.addr = vp_addr[31:2]             (字节地址→字地址)
             bus.wdat = vp_wdat
        └─ soc_fabric 译码 addr[31:29]==1 → CSR 从口
        └─ u_soc_csr 收下 → bus.rdy=1
   └─(应答)→ vp_wack = vp_we & (bus.rdy|cpu_access) → 唤醒用户线程
```

读事务类似，多一步把 `bus.rdat`（或 IMEM 的 `imem_readdata`）回填到 `vp_rdat`，再由 `vp_rack` 唤醒用户线程。

#### 4.2.3 源码精读

**模块签名与 node 参数**。[soc_cpu.VPROC.sv:L87-L99](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L87-L99)：

```systemverilog
module soc_cpu #(
   parameter [31:0] ADDR_RESET     = 32'h 0000_0000,  // Unused
   parameter int    NUM_WORDS_IMEM = 8192,            // Unused
   parameter [3:0]  NODE           = 0                // CPU defaults to VProc node 0
)(
   soc_if.MST bus,
   input logic imem_cpu_rstn, imem_we,
   input logic [31:2] imem_waddr,
   input logic [31:0] imem_wdat
);
```

注意三个要点：①端口与 RTL 版 `soc_cpu` 完全一致（`soc_if.MST bus` + IMEM 烧写口），这是「可替换」的前提；②`NODE` 默认 0，与用户程序的 `node=0` 对应；③`ADDR_RESET`/`NUM_WORDS_IMEM` 在 VProc 版里**未使用**（IMEM 不靠它们，而是靠 `mem_model`），保留只为参数兼容。

**总线翻译的核心组合逻辑**（[soc_cpu.VPROC.sv:L127-L145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L127-L145)）：

```systemverilog
assign cpu_access = vp_addr[31:28] == '0;   // 命中本地 IMEM?
assign bus.we     = vp_be & {4{vp_we}};     // 字节使能 × 写标志
assign bus.vld    = (vp_we | vp_rd) & ~cpu_access;
assign bus.addr   = vp_addr[31:2];          // 字地址（丢掉低 2 位）
assign bus.wdat   = vp_wdat;
assign imem_write = vp_we & cpu_access;
assign imem_read  = vp_rd & cpu_access;
```

这几行就是 README 说的「不到十个组合门」：把 VProc 的通用信号拼成 `soc_if` 所需的 `we/vld/addr/wdat`，并区分 IMEM 与总线访问。回想 u2-l4：`soc_if` 的 `we` 兼任「读/写标志」与「字节写使能掩码」，这里 `vp_be & {4{vp_we}}` 正好同时表达了「这是一次写」与「写哪几个字节」。

**应答与读数据选择**。[soc_cpu.VPROC.sv:L150-L156](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L150-L156)：

```systemverilog
assign vp_wack = vp_we & (bus.rdy | cpu_access);              // 写应答
assign vp_rack = vp_rd & (bus.rdy | imem_readdatavalid);      // 读应答
assign vp_rdat = cpu_access ? imem_readdata : bus.rdat;       // 读数据来源
```

写应答在「总线就绪（`bus.rdy`）或访问本地 IMEM」时拉起；读应答在「总线就绪或 IMEM 读数据有效」时拉起；读数据按是否命中 IMEM 二选一。注意 Verilator 下同样的逻辑被放进 `always @(negedge bus.clk)`（[soc_cpu.VPROC.sv:L161-L173](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L161-L173)），用时钟**下沿**采样，确保 VProc 看到的输入信号已稳定——这是 Verilator 协同仿真的常见手法（用 `\`ifndef VERILATOR` 区分两套实现）。

**VProc 与 mem_model 的实例化**。[soc_cpu.VPROC.sv:L180-L205](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L180-L205) 实例化 VProc（`DISABLE_DELTA(1)` 关掉 delta 模式，`.Node(NODE)` 绑定节点号）；[soc_cpu.VPROC.sv:L212-L244](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L212-L244) 实例化 `mem_model u_imem`，它的一个端口给 VProc 读程序用，另一个 `wr_port_*` 端口接 UART 的在线烧写（u2-l5）。

**用户入口 VUserMain0**。[VUserMain0.cpp:L33-L70](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMain0.cpp#L33-L70)：

```cpp
static const int node = 0;
extern "C" void VUserMain0(void) {            // C 链接入口，取代 main
    VPrint("VProc soc_cpu entered VUserMain%d()\n\n", node);
    VProc* vp0 = new VProc(node);             // 绑定 node 0
    vp0->tick(100);                           // 先空转 100 周期等复位稳定

    uint32_t addr  = 0x10001000;              // DMEM 区域（字节地址）
    uint32_t wdata = 0x900dc0de;
    vp0->write(addr, wdata);                  // 写
    VPrint("Written   0x%08x  to  addr 0x%08x\n", wdata, addr);
    vp0->tick(3);                             // 空转 3 周期

    uint32_t rdata;
    vp0->read(addr, &rdata);                  // 读回
    if (rdata == wdata) VPrint("Read back 0x%08x ...\n", rdata);
    else VPrint("***ERROR: ...\n", ...);

    while(true) vp0->tick(GO_TO_SLEEP);       // 长睡，仿真继续
}
```

它演示了「构造 → tick 等待 → write → tick → read → 比对 → 长睡」的标准骨架。`addr=0x10001000` 落在 DMEM 区（`addr[31:28]=1`，非 IMEM），所以这次写穿越 `soc_cpu.VPROC` → `soc_fabric` → `u_dmem`，是端到端走过真实 DUT 总线的一次事务。`VPrint` 在 Verilator 下就是 `printf`（[VUser.h:L89-L93](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/include/VUser.h#L89-L93)），输出直接打到仿真终端。

#### 4.2.4 代码实践

**目标**：把现有的「写 DMEM 再读回」改成「写一个 CSR 寄存器再读回」，验证 VProc 经 `soc_cpu.VPROC` 驱动 CSR 的通路。

**操作步骤**：

1. 复制 `4.sim/usercode/VUserMain0.cpp` 为一份实验副本（不要改原文件之外的东西）。
2. 把 `addr` 从 `0x10001000`（DMEM）改成一个 **CSR 字节地址**，且该寄存器须是 `sw=rw`（可写可读），例如 `ethernet[0]` 的 MAC 低字（CSR 基址 `0x20000000` + `0x4c/4` 区，具体偏移见 [csr_cosim.h:L3082-L3084](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L3082-L3084) 与 `csr.rdl`）。把 `wdata` 改成你自己的模式（如 `0xDEADBEEF`）。
3. 编译运行（按 README 记载的用法）：
   ```bash
   cd 4.sim
   make -f MakefileVProc.mk USER_C=VUserMain0.cpp run
   ```
   `USER_C` 指定要编进 `libuser.a` 的 soc_cpu 用户源（变量定义见 [MakefileVProc.mk:L9](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L9)；README 在 [4.sim/README.md:L236](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L236) 给出了 `USER_C="VUserMain0.cpp ..."` 的示例）。

**需要观察的现象**：

- 仿真终端是否打印你新设的 `Written 0x...` 与 `Read back 0x...`；
- 读回值是否等于写入值（对 `sw=rw` 寄存器应相等）。

**预期结果**：对可读可写的 CSR，读回值应与写入值一致；若你选了 `sw=r;hw=w`（只读）或硬件会改写的寄存器（如 FCR 的 `idle`、计数器），读回值会不同——这本身就是一个理解 CSR 读写属性的练手。

> 关于构建路径的说明（**待本地验证**）：`MakefileVProc.mk` 在 `BUILD=ISS` 时会把 `models/rv32/usercode/VUserMain0.cpp`（rv32 指令集模拟器的集成入口）编进 `libvproc.a`（[MakefileVProc.mk:L60-L61](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L60-L61)、[L225-L236](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L225-L236)）。而本讲的 `4.sim/usercode/VUserMain0.cpp` 是**裸 API 的独立示例**，用 `USER_C` 显式带入。两条路径下「哪个 `VUserMain0` 真正被链接」取决于 `BUILD` 与 `USER_C` 的组合，u7-l3 会专门讲 ISS 路径；若本地出现 `VUserMain0` 重复定义，请确认只激活了一条入口路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bus.addr = vp_addr[31:2]` 而不是直接 `vp_addr`？
**答案**：`soc_if` 用的是**字地址** `soc_addr_t = logic[31:2]`（见 u2-l4 的 `soc_pkg`），最低 2 位字节偏移已被 `we`（字节写使能）吸收。所以丢掉低 2 位。

**练习 2**：`VUserMain0` 末尾为什么必须有 `while(true) vp0->tick(GO_TO_SLEEP)`？
**答案**：用户线程一旦从入口返回，对应的 VProc node 就不再被驱动。`tick(GO_TO_SLEEP)` 让 node 0 进入长睡、不再发总线事务，但把控制权持续交还仿真器，使仿真（以及其它 node，如以太网 VIP）能继续推进，直到达到 `TIMEOUTUS` 超时或 `$finish`。

**练习 3**：把 `addr` 设成 `0x00001000`（IMEM 区）会发生什么？
**答案**：`vp_addr[31:28]=='0` → `cpu_access=1`，事务不进 `soc_if`，而是读写本地 `mem_model u_imem`；`bus.vld` 因 `~cpu_access` 而为 0，DUT 总线看不到任何动作。

---

### 4.3 协同仿真 HAL 抽象层

#### 4.3.1 概念说明

直接用 `vp0->write/read` 驱动 CSR 固然直观，但要手算每个寄存器的字节地址、字节使能、字段位移，既易错又难维护。还记得 u3-l2 讲过的「单一真源 `csr.rdl` 经 PeakRDL 同时生成硬件 RTL 与软件 HAL」吗？协同仿真也有一份自动生成的 HAL——**`csr_cosim.h`**，由 `sysrdl_cosim.py` 遍历 RDL 节点树、给每个寄存器/字段套上一个 C++ 类外壳生成。

这层抽象的核心价值：**同一份应用源码，靠一个 `VPROC` 宏就能在两种目标间切换**——编译给真硬件时，HAL 的读写是真正的指针解引用（CPU 总线做字节使能）；编译给 VProc 协同仿真时，HAL 的读写变成对 `VWrite`/`VWriteBE`/`VRead` 的调用。应用层（如 `main.cpp`）对此毫无感知，写 `csr->dpe->fcr->pause(1)` 在两个世界都成立。这就是 [4.sim/README.md:L539-L585](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md) 所述的「协同仿真 HAL」。

#### 4.3.2 核心流程

HAL 的生成与切换关系：

```text
                  csr.rdl (单一真源)
            ┌──────────┴──────────┐
   peakrdl regblock              sysrdl_cosim.py
        ↓                              ↓
   csr.sv/csr_pkg.sv (RTL)     csr_hw.h ──┐
                              csr_cosim.h ─┤ 两套 HAL，类层级同名
                                          ↓
                              wireguard_regs.h:
                                  #ifdef VPROC → csr_cosim.h
                                  #else        → csr_hw.h
                                          ↓
                              应用源码 main.cpp / WGMAIN：一份代码，两处编译
```

`csr_cosim.h` 顶层类 `csr_vp_t`（[csr_cosim.h:L3072-L3102](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L3072-L3102)）默认基址 `0x20000000`，并按 RDL 层级挂出 `cpu_fifo/uart/gpio/ethernet[4]/hw_id/hw_version/dpe/routing_table/cryptokey_table`——与 `csr_hw.h` 完全对应（u3-l2 已建立）。差别只在每个叶子方法的实现：

- 整字访问 `full()` → `VWrite(reg, data, 0, SOC_CPU_VPNODE)`；
- 字段读 → `VRead` 后按 `_bm`/`_bp` 掩码与位移取出；
- 字段写（非字节对齐）→ 先 `VRead` 读回整字、改字段、再 `VWriteBE` 写回（读改写 RMW）；
- 每次访问后跟一句 `VTick(rand() % 33, ...)`——**随机化两次访问之间的周期数**，免费给 DUT 做随机时序压测。

#### 4.3.3 源码精读

**HAL 切换头**。[wireguard_regs.h:L13-L19](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/wireguard_regs.h#L13-L19) 只做一件事：用 `VPROC` 宏选头。仿真构建时 `MakefileVProc.mk` 在 `DEFS` 里定义了 `-DVPROC`（[MakefileVProc.mk:L105](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L105)），于是协同仿真 HAL 生效。

**协同仿真专有的几个宏**。[csr_cosim.h:L28-L38](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L28-L38)：

```cpp
#define WGMAIN                VUserMain0   // 入口名：协同仿真用 VUserMain0，硬件用 main
#define NO_DELTA_UPDATE       0
#define SOC_CPU_VPNODE        0            // VProc node 号
#define SOC_CPU_CLK_PERIOD_PS 18518        // 时钟周期(ps)，用于把 usleep 换算成 VTick 周期数
```

`WGMAIN` 是入口名替换的关键（见 4.2.1）。`SOC_CPU_CLK_PERIOD_PS` 用来把应用里的延时函数（如 `wg_usleep`）换算成 `VTick` 周期数——应用调 `usleep(t)`，HAL 把它翻译成 `VTick(t / 周期)`。

**一个叶子字段类的实例**。看 `cpu_fifo.rx.data_31_0` 的 `tdata` 字段（[csr_cosim.h:L62-L99](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L62-L99)），整字写 `full()`：

```cpp
inline void full(const uint32_t data) {
    VWrite(reg, data, NO_DELTA_UPDATE, SOC_CPU_VPNODE);
    VTick(rand() % 33, SOC_CPU_VPNODE);          // 随机延时
};
```

字段读 `tdata()`（[csr_cosim.h:L85-L92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L85-L92)）：

```cpp
inline uint32_t tdata() {
    uint32_t rdata;
    VRead(reg + 0, &rdata, NO_DELTA_UPDATE, SOC_CPU_VPNODE);
    VTick(rand() % 33, SOC_CPU_VPNODE);
    return (((uint32_t)rdata << 0) & CSR__..._TDATA_bm) >> CSR__..._TDATA_bp;
};
```

**非字节对齐字段的读改写**。当一个字段不是整字、又不落在字节边界时（如 `control` 里的 `tuser_src`，占 `tuser` 的某几位），HAL 自动生成 RMW：先 `VRead` 读整字，按掩码清掉目标字段，或上新值，再 `VWriteBE` 写回。见 [csr_cosim.h:L258-L266](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L258-L266)。对照 u3-l3 讲过的「cpu_fifo 把字段对齐到 8 位边界以避免 RMW」——那里是**设计上的避免**；而 HAL 这里是**通用兜底**：对没对齐的字段，宁可做 RMW 也要保证语义正确。

**层级组装**。`csr_vp_t` 把所有子模块按 RDL 地址偏移 new 出来（[csr_cosim.h:L3075-L3091](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L3075-L3091)），数组型表（`routing_table` 64 项、`cryptokey_table` 64 项）用 `for` 循环按 `sizeof(...)/4` 步长铺开——这与 u4-l6 讲的「external regfile 在 RTL 里由 `tdp_ram` 实现」是同一张地址图的两副面孔（硬件侧双口 RAM、软件侧 HAL 指针偏移）。

#### 4.3.4 代码实践

**目标**：体会「一份应用源码、两套 HAL」的切换，不改任何源码、只看编译宏。

**操作步骤**：

1. 读 [wireguard_regs.h:L13-L19](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/wireguard_regs.h#L13-L19)，确认 `VPROC` 决定包含哪个 HAL。
2. 在 [MakefileVProc.mk:L105](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L105) 找到 `DEFS = -DVERILATOR -DVPROC_SV -DVPROC`，确认仿真构建必带 `-DVPROC`。
3. 设想一段应用代码：
   ```cpp
   #include "wireguard_regs.h"
   void WGMAIN(void) {
       csr_vp_t* csr = new csr_vp_t();
       csr->dpe->fcr->pause(1);          // 请求 DPE 暂停
       while (!csr->dpe->fcr->idle()) {} // 轮询静止
       csr->dpe->fcr->pause(0);          // 恢复
   }
   ```
   （这段呼应 u3-l4 的 FCR 原子更新握手。）

**需要观察的现象（源码阅读型）**：

- 编译给真硬件（无 `VPROC`）：`WGMAIN` 展开成 `main`，`pause(1)` 是一次带字节使能的指针 store；
- 编译给 VProc（有 `VPROC`）：`WGMAIN` 展开成 `VUserMain0`，`pause(1)` 变成 `VWriteBE(...)+VTick(rand()%33)`。

**预期结果**：你能说清「同一行 `csr->dpe->fcr->pause(1)` 在两种构建下分别落到哪段底层代码」，这正是 HAL 抽象层的意义——应用逻辑与访问机制解耦。

> 待本地验证：完整跑通需要在 `3.build` 先生成 HAL、再在 `4.sim` 编仿真，依赖 VProc/mem_model 仓库被 `MakefileVProc.mk` 自动 clone（[L271-L276](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L271-L276)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么协同仿真 HAL 在每次访问后要 `VTick(rand() % 33)`？
**答案**：在两次访问间注入 0–32 个随机周期，等价于免费做了一次随机时序压测——能暴露 DUT 对「背靠背」「长间隔」CSR 访问的处理差异，而这是固定节拍的 RTL 自测难以覆盖的。

**练习 2**：`csr_cosim.h` 与 `csr_hw.h` 的 `csr_vp_t` 类层级为什么必须同名同结构？
**答案**：为了让应用源码（`main.cpp`/`WGMAIN`）零修改地在两套 HAL 上编译。差别只在叶子方法的实现（VProc 事务 vs 指针解引用），类名与字段名完全一致，靠 `wireguard_regs.h` 的宏二选一。

**练习 3**：用 HAL 写一个非字节对齐字段（如 `tuser_src`）时，底层会发生几次总线事务？
**答案**：两次——先 `VRead` 读回整字，本地改字段，再 `VWriteBE` 写回（RMW）。这也解释了为什么 u3-l3 的 cpu_fifo 设计要尽量把字段对齐到字节边界：省掉这次读。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「用协同仿真 HAL 驱动 DPE 暂停/恢复」的端到端追踪任务：

1. **写入口**：仿照 [VUserMain0.cpp:L39-L70](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMain0.cpp#L39-L70) 的骨架，用 HAL 写一段 `WGMAIN`：`new csr_vp_t()` → `csr->dpe->fcr->pause(1)` → 轮询 `idle()` → 改一个 routing 表项 → `pause(0)`。
2. **追底层**：把 `pause(1)` 一路展开——HAL 的 `VWriteBE`（[csr_cosim.h:L1517-L1525](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_cosim.h#L1517-L1525)）→ VProc C API → DPI-C → `soc_cpu.VPROC` 组合逻辑（[soc_cpu.VPROC.sv:L127-L156](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L127-L156)）→ `soc_fabric` 译码 → `u_soc_csr` → DPE 的 `fcr.pause` 字段。画出这条链路。
3. **对端 RTL**：在 `dpe.sv`/`dpe_multiplexer.sv` 里找到 `pause` 怎么影响 mux 的状态机（承接 u4-l2），确认 HAL 写下的 `pause(1)` 最终真的让 mux 在包边界停下。
4. **构建观察**：`make -f MakefileVProc.mk USER_C=<你的入口>.cpp run`，在终端用 `VPrint` 观察 `pause/idle` 的来回；若开了波形（`rungui`），在 `wave.fst` 里对照 `fcr.pause`/`fcr.idle` 信号与 `bus.vld` 事务的时序。

> 这个任务把「VProc API（4.1）→ soc_cpu.VPROC 适配 + VUserMain0 入口（4.2）→ HAL 抽象（4.3）」三者首尾相连，并对接到 Unit 4 的 DPE 数据面，是对协同仿真通路的一次完整走查。

---

## 6. 本讲小结

- **VProc 是「假 CPU、真驱动」**：HDL 里只留空壳，靠主机上的原生 C++ 程序经 DPI-C 下发读写/推进时间，把控制面软件逻辑整体协同仿真起来；用户线程与仿真器以 `write`/`read`/`tick` 为同步点，lock-step 推进。
- **三个核心 API**：`write(addr,data)`、`read(addr,&data)`、`tick(n)`；地址为字节地址，`delta` 恒 0；`tick(GO_TO_SLEEP)` 用于程序末尾长睡。
- **`soc_cpu.VPROC` 是适配器**：用不到十个组合门把 VProc 通用总线翻译成 `soc_if`（`we = vp_be & {4{vp_we}}`、`addr = vp_addr[31:2]`），并区分 IMEM（本地 `mem_model`）与总线访问；端口与 RTL 版 `soc_cpu` 完全一致，故可插拔顶替 picoRV32。
- **入口是 `VUserMain0`**：每个 node 一个 `VUserMain<n>`，须 C 链接；node 0 对应 `soc_cpu`，取代普通程序的 `main`。
- **协同仿真 HAL（`csr_cosim.h`）是抽象层**：自动生成、与硬件 HAL（`csr_hw.h`）类层级同名，靠 `VPROC` 宏经 `wireguard_regs.h` 二选一，使应用源码在「真硬件」与「VProc 协同仿真」间零修改切换；底层把字段访问翻成 `VWrite`/`VWriteBE`/`VRead`，并附 `VTick(rand()%33)` 做随机时序压测。
- **现状衔接**：裸 API 示例 `4.sim/usercode/VUserMain0.cpp` 与 rv32 ISS 集成入口 `models/rv32/usercode/VUserMain0.cpp` 是两条不同路径（后者由 u7-l3 详解），由 `BUILD`/`USER_C` 决定实际链接哪一个。

## 7. 下一步学习建议

- **u7-l3（rv32 RISC-V ISS 与软件集成）**：本讲的 `VUserMain0` 还是「手写测试程序」。下一讲把 `VUserMain0` 换成 rv32 指令集模拟器的集成入口，让**真实交叉编译的 RISC-V 固件二进制**跑在 VProc 上，并用 `-x`/`-X` 划定哪些地址送 HDL、哪些用 `mem_model` 内部消化。
- **u7-l4（以太网 VIP udpIpPg）**：node 1–4 的 VProc 实例，体会「同一套 VProc 机制，配不同 API（`genUdpIpPkt`/`UdpVpSendRawEthFrame`）」如何驱动四口以太网。
- **回看 u3-l2 / u3-l4**：对照本讲 HAL 的底层实现，巩固「单一真源生成多产物」与 FCR 原子更新在协同仿真里是如何被驱动的。
- **延伸阅读**：[VProc 手册](https://github.com/wyvernSemi/vproc/blob/master/doc/VProc.pdf) 与 [mem_model 手册](https://github.com/wyvernSemi/mem_model/blob/main/doc/mem_model_manual.pdf)（[cosim/README.md:L60-L62](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/README.md#L60-L62) 给了链接），了解 burst 事务、中断回调等本讲未展开的高级 API。
