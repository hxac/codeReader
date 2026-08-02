# AXI 接口与 host 驱动

## 1. 本讲目标

本讲把 Ventus（乘影）从「一块孤立的 RTL」变成「可以挂进 SoC 的 IP」。学完后你应当掌握：

- AXI4 / AXI4-Lite 五个通道的握手语义，以及 Ventus 在两条 AXI 总线上分别扮演的**从**（slave）与**主**（master）角色。
- `AXI4Lite2CTA` 如何用一组 32 位寄存器把 host CPU 的「派发一个 kernel」请求翻译成 `host2CTA_data`，以及每个寄存器偏移对应的字段。
- `AXI4Adapter` 如何把 L2 缓存发出的 TileLink A/D 通道请求（`Get`/`PutFullData`）桥接成 AXI4 的 AR/R/AW/W/B 突发，完成「宽 cacheline ↔ 窄 64 位」的位宽转换。
- `GPGPU_axi_top` / `GPGPU_axi_adapter_top` 如何把上面两个适配器与 `GPGPU_top` 拼成一个对外只暴露一组 AXI4-Lite slave + 一组 AXI4 master 的完整 IP，并据此跑通 `make fpga-verilog`。

本讲是硬件集成层，承接 u3-l1（CTA 调度器的 host 接口语义）与 u6-l5（L2 缓存对外的 TileLink `out_a`/`out_d` 端口）。

## 2. 前置知识

### 2.1 为什么需要 AXI

在前面的讲义里，`GPGPU_top` 的对外端口是两组「语义化」接口：`host_req`/`host_rsp`（接 CPU 派发 kernel）和 `out_a`/`out_d`（接外存 DDR）。这两个端口是 Chisel 自定义 Bundle，**任何想集成 Ventus 的 SoC 都得自己写包装**。AXI（Advanced eXtensible Interface，ARM 的片上总线标准）就是用来统一这种包装的「通用插头」：把语义化端口翻译成业界通用的 AXI4-Lite（轻量控制寄存器）与 AXI4（带突发的数据总线），Ventus 就能像普通 IP 一样挂到任何 AXI 互联上。

### 2.2 AXI4-Lite 五通道与 valid/ready 握手

AXI4-Lite 是 AXI4 的极简子集：每次交易只传一个数据，无突发。它有 5 个**独立**的通道，每个通道都是一对 `valid`/`ready` 握手信号——主（master）拉高 `valid` 并给出有效载荷，从（slave）拉高 `ready` 表示愿意接收，**同一拍 `valid && ready` 同时为真**称为一次 `fire`，交易成立。

| 通道 | 方向 | 作用 |
|------|------|------|
| AW（Write Address） | 主→从 | 给出写地址 |
| W（Write Data） | 主→从 | 给出写数据 + 字节使能 |
| B（Write Response） | 从→主 | 回写响应（OKAY/ERR） |
| AR（Read Address） | 主→从 | 给出读地址 |
| R（Read Data） | 从→主 | 回读数据 + 响应 |

一次**写交易** = AW（给地址）+ W（给数据）+ B（要响应）；一次**读交易** = AR（给地址）+ R（回数据）。AW/W 之间没有严格先后，但 B 必须在 W 之后。本讲中 **GPU 是 AXI4-Lite 的「从」**——host CPU 是主，往 GPU 的寄存器里写 kernel 参数。

### 2.3 AXI4 突发（burst）

完整 AXI4 在 AR/AW 上多了 `len`（突发长度，即拍数 − 1）、`size`（每拍字节数 = \(2^{size}\)）、`burst`（突发类型，1 = INCR 递增）等字段，允许一次地址交易搬运一整块连续数据。本讲中 **GPU 是 AXI4 的「主」**——它主动向外部 DDR 发起读/写突发。

### 2.4 TileLink A/D 通道回顾

u6-l5 已建立：L2 对外用精简版 TileLink，只有两个通道——A 通道（请求，`Get`=读、`PutFullData`/`PutPartialData`=写）、D 通道（响应，`AccessAck`=写回应、`AccessAckData`=读数据带回）。`AXI4Adapter` 做的就是 A↔{AR/AW}、D↔{R/B} 的协议翻译。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ventus/src/axi/AXI4Lite.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite.scala) | 定义 AXI4-Lite 五通道 Bundle 与位宽常量（prot/resp/id 宽度）。 |
| [ventus/src/axi/AXI4Lite2CTA.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite2CTA.scala) | AXI4-Lite **从**：把 host 的寄存器读写翻译成 `host2CTA_data` 派发与 `CTA2host_data` 完成。 |
| [ventus/src/axi/AXI4Adapter.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Adapter.scala) | AXI4 **主**：把 L2 的 TileLink A/D 桥接成 AXI4 突发，完成位宽转换。 |
| [ventus/src/top/GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | 定义 `host2CTA_data`/`CTA2host_data` Bundle，以及 `GPGPU_axi_top`、`GPGPU_axi_adapter_top` 两个 AXI 顶层组合。 |
| [ventus/fpga_test/scrs/driver/naive_driver.h](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.h) | FPGA 上跑的 host 驱动示例（C），演示「写参数→置 valid→轮询完成→读 wg_id」的软件流程。 |
| [Makefile](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile) | `fpga-verilog` 目标，以 `GPGPU_axi_adapter_top` 为顶层生成可综合 Verilog。 |

> ⚠️ **关于 `naive_driver.h` 的偏移表**：它里面的寄存器偏移反映的是**较早的寄存器布局**，与当前 HEAD 的 `AXI4Lite2CTA.scala` **不一致**（例如它把 done 寄存器放在 0x38/0x3c，而当前 RTL 在 0x40/0x44）。本讲一律以 `AXI4Lite2CTA.scala` 的 `regs()` 下标为唯一事实来源，driver 只用来理解**软件流程**，不复用它过时的偏移数值。

## 4. 核心概念与源码讲解

### 4.1 AXI4Lite 通道定义

#### 4.1.1 概念说明

`AXI4Lite.scala` 是一个纯数据结构文件：它把第 2.2 节描述的五个通道逐个定义成 Chisel `Bundle`，并固定三类位宽常量。这是后面 `AXI4Lite2CTA` 的「插座」——后者只要 `Flipped(new AXI4Lite(32,32))` 就声明了一个 32 位地址、32 位数据的 AXI4-Lite 从端口。把通道拆成独立 Bundle 的好处是：每个通道的方向（Output/Input）自描述，连线时不容易搞反主从。

#### 4.1.2 核心流程

AXI4-Lite 通道本身没有「流程」，它只是 wires。但每个通道遵循同一套握手规则：

```
每拍：if (valid && ready) → 一次 fire，载荷被接收
valid 由源端驱动，ready 由目的端驱动
规则：源端拉 valid 后必须等 ready；目的端不允许等 valid 才给 ready（组合依赖会死锁）
```

位宽常量：`protWidth = 3`（保护位，本仓库固定为 0，表示非特权、非安全、数据访问）、`respWidth = 2`（响应码，0=OKAY）、`idWidth = 12`（事务 ID，用于多主/多从场景下匹配请求与响应，本仓库基本恒为 0）。

#### 4.1.3 源码精读

顶层 `AXI4Lite` 把五个通道子 Bundle 聚到一起，这是对外接口的「总插头」：

[ventus/src/axi/AXI4Lite.scala:117-129](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite.scala#L117-L129) —— `AXI4Lite` Bundle 聚合 aw/w/b/ar/r 五个通道，伴随 object 定义三类位宽常量。

每个通道的方向约定一致——以「主端看出去」为 Output。例如写地址通道 `AXI4LAW`：

[ventus/src/axi/AXI4Lite.scala:16-22](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite.scala#L16-L22) —— `awaddr`/`awvalid` 是 Output（主→从），`awready` 是 Input（从→主）；`AXI4Lite2CTA` 用 `Flipped` 把整个接口取反，于是它作为「从」时这些方向自动翻转。

写数据通道 `AXI4LW` 多一个 `wstrb`（字节使能，`dataWidth/8` 位），用于部分写；读数据通道 `AXI4LR` 把数据方向反过来（`rdata` 是 Input）；写响应 `AXI4LB` 的 `bresp`/`bvalid` 是 Input（从→主）。这些方向细节决定了连线时谁驱动谁。

#### 4.1.4 代码实践

**实践目标**：建立「通道方向 = 谁驱动谁」的直觉，避免后续连线搞反主从。

**操作步骤**：

1. 打开 `AXI4Lite.scala`，对每个通道（`AXI4LAW`/`AXI4LW`/`AXI4LB`/`AXI4LAR`/`AXI4LR`）列一张表，标注每个信号是 `Output` 还是 `Input`。
2. 回答：把 `AXI4Lite` 不加 `Flipped` 直接当模块 IO，该模块是主还是从？加了 `Flipped` 呢？

**需要观察的现象**：五个通道里，主端发出的都是 `valid` + 载荷（`Output`），收到的都是 `ready` + 响应/数据（`Input`）。

**预期结果**：`new AXI4Lite(...)` 不翻转时是「主端口」视角；`Flipped(new AXI4Lite(...))` 是「从端口」视角。`AXI4Lite2CTA` 用 `Flipped`，所以它是从。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AXI4Lite` 要把 `protWidth`/`respWidth`/`idWidth` 放进 `object AXI4Lite` 而不是每个 Bundle 各自定义？
**答案**：这三位宽是协议级常量，五个通道共享（如 `awid`/`bid`/`arid`/`rid` 都用 `idWidth`）。放进 object 当单一事实来源，避免各通道写不一致的魔法数字。

**练习 2**：AXI4-Lite 一次写交易需要走哪几个通道、按什么顺序？
**答案**：AW（给地址）与 W（给数据）可乱序到达，但都必须在 B（响应）之前；B 由从端在收完 W 后回给主端。

---

### 4.2 AXI4Lite2CTA：host 寄存器接口与 kernel 派发

#### 4.2.1 概念说明

`AXI4Lite2CTA` 是 Ventus 对 host CPU 暴露的「控制面」从端口。它内部维护 **20 个 32 位寄存器**（`regs(0)`~`regs(19)`），host 通过 AXI4-Lite 读写它们。这些寄存器分两类：

- **参数寄存器**：host 写入一个 kernel 的全部资源描述（wg_id、warp 数、每 warp 线程数、起始 PC、各寄存器/共享内存用量、3D 维度、asid 等）。
- **完成寄存器**：硬件回填，host 轮询以获知哪个 kernel 跑完了。

写完所有参数后，host 向「使能位」`regs(0)` 写 1 触发一次派发：模块把 `regs` 里散落的字段组装成一个 `host2CTA_data` Bundle，经 `Decoupled` 接口送给下游的 `CTAinterface`（即 u3-l1 的调度器入口）。这就完成了「host 视角的 WG」到「硬件 CTA 请求」的桥接。

#### 4.2.2 核心流程

模块里有**两条独立状态机**，一条处理 AXI4-Lite 读写，一条负责把参数推出给 CTA：

```
(A) AXI4-Lite 读写主 FSM（一次交易走完一个通道序列）：
    sIdle →(awvalid)→ sWriteAddr → sWriteData → sWriteResp → sIdle
    sIdle →(arvalid)→ sReadAddr  → sReadData               → sIdle
    写：锁存 word 地址 awaddr[31:2]、收 wdata、回 bresp
    读：锁存 word 地址 araddr[31:2]、回 rdata

(B) 派发输出 FSM（把参数寄存器推给 CTAinterface）：
    out_sIdle →(regs(0)==1, 即 input_valid)→ out_sOutput
    out_sOutput：拉高 io.data.valid，组装 host2CTA_data
                 →(io.data.fire)→ 回 out_sIdle，并把 regs(0) 清 0（自动撤销触发）
```

关键点：`(A)` 在 `sIdle` 里**额外**判断 `out_state === out_sIdle` 才接受新的写请求——也就是说，**正在派发一个 kernel 期间，host 写不进新值**，避免寄存器被中途改写。`(B)` 的触发是「一次性」的：host 写 1 到 `regs(0)`，硬件发完一个 WG 后自动清零，host 通过轮询 `regs(0)` 归零即可知道「派发已被接收」。

寄存器到 `host2CTA_data` 字段的映射是本模块的「核心对照表」（见 4.2.3）。地址换算：`awaddr[31:2]` 直接当 word 下标用（丢掉最低 2 位做 4 字节对齐），所以**字节偏移 = word 下标 × 4**。

#### 4.2.3 源码精读

先看寄存器堆与对外接口。模块声明一个 20 项的 `VecInit` 寄存器堆，并暴露 AXI4-Lite 从口 + 一个 `Decoupled(host2CTA_data)` 输出 + 一个 `Decoupled(CTA2host_data)` 输入（完成回报）：

[ventus/src/axi/AXI4Lite2CTA.scala:20-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite2CTA.scala#L20-L30) —— `io.ctl` 是 AXI4-Lite 从口，`io.data` 把参数以 `host2CTA_data` 形式送出，`io.rsp` 收 CTA 完成回报；`regs` 是 20 个 32 位寄存器。

**派发触发与字段组装**（核心对照表）。`input_valid = regs(0)(0)`；当它为 1 且输出 FSM 处于 `out_sOutput` 时拉高 `io.data.valid`，并把每个 `regs(i)` 装配成 `host2CTA_data` 的对应字段：

[ventus/src/axi/AXI4Lite2CTA.scala:83-105](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite2CTA.scala#L83-L105) —— 把 `regs(1)..regs(15)`、`regs(18)`、`regs(19)` 一一映射到 `host_wg_id`、`host_num_wf`、`host_wf_size`、`host_start_pc`、各类寄存器/共享内存用量、`host_pds_baseaddr`、`host_csr_knl`、`host_kernel_size_3d(0..2)`、`host_pds_size_per_wf`、`host_asid`。注意 `host_gds_size_total := 0.U` 是硬连线，不从寄存器来；`regs(19)` 同时喂给 `host_asid` 和 `host_kernel_asid`。

据此得到当前 HEAD 的**权威寄存器映射表**（字节偏移 = 下标 × 4）：

| 下标 | 字节偏移 | 方向 | `host2CTA_data` 字段 / 含义 |
|------|----------|------|------------------------------|
| 0  | 0x00 | R/W（bit0） | `input_valid`：写 1 触发派发，发完自动清 0 |
| 1  | 0x04 | W | `host_wg_id`（workgroup id） |
| 2  | 0x08 | W | `host_num_wf`（block 内 warp 数，对应 CSR_NUMW） |
| 3  | 0x0C | W | `host_wf_size`（每 warp 线程数，对应 CSR_NUMT） |
| 4  | 0x10 | W | `host_start_pc`（取指起始地址） |
| 5  | 0x14 | W | `host_vgpr_size_total`（向量寄存器总用量） |
| 6  | 0x18 | W | `host_sgpr_size_total`（标量寄存器总用量） |
| 7  | 0x1C | W | `host_lds_size_total`（共享内存用量） |
| 8  | 0x20 | W | `host_vgpr_size_per_wf` |
| 9  | 0x24 | W | `host_sgpr_size_per_wf` |
| 10 | 0x28 | W | `host_gds_baseaddr`（全局数据内存基址） |
| 11 | 0x2C | W | `host_pds_baseaddr`（私有数据栈基址） |
| 12 | 0x30 | W | `host_csr_knl`（kernel CSR 区基址） |
| 13 | 0x34 | W | `host_kernel_size_3d(0)`（x 维 WG 数） |
| 14 | 0x38 | W | `host_kernel_size_3d(1)`（y 维 WG 数） |
| 15 | 0x3C | W | `host_kernel_size_3d(2)`（z 维 WG 数） |
| 16 | 0x40 | R（HW 写） | 完成的 `wg_id`（来自 `CTA2host_data`） |
| 17 | 0x44 | R（HW 写 bit0） | 完成挂起标志：=1 表示有未取走的完成 |
| 18 | 0x48 | W | `host_pds_size_per_wf`（每 warp 私有栈大小） |
| 19 | 0x4C | W | `host_asid` / `host_kernel_asid`（MMU 用，见 u7-l1） |

**完成回报通路**。当下游 `io.rsp.valid`（某个 WG 跑完）且当前没有挂起的完成（`!regs(17)(0)`）时，接收回报、把完成 `wg_id` 锁存进 `regs(16)`、置 `regs(17):=1`：

[ventus/src/axi/AXI4Lite2CTA.scala:32-37](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite2CTA.scala#L32-L37) —— 完成回报被锁存到 `regs(16)`（wg_id）与 `regs(17)`（挂起标志）。`regs(17)` 为 1 期间 `io.rsp.ready` 拉低，新的完成被反压。

> 关于「重新使能」：源码中没有任何路径自动清 `regs(17)`，因此 host 必须在取走完成（读 `regs(16)`）后**主动写 0 到偏移 0x44** 来清挂起标志、释放对 `io.rsp` 的反压。这与 `naive_driver.h` 里 `GpuWatchTask` 末尾「写 0 到 valid 寄存器」的软件模式一致（见 4.4.3），属于 host 驱动的握手。

**AXI4-Lite 主状态机**。一次写交易依次走 `sWriteAddr → sWriteData → sWriteResp`，分别握手 AW、W、B；读交易走 `sReadAddr → sReadData`：

[ventus/src/axi/AXI4Lite2CTA.scala:124-182](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite2CTA.scala#L124-L182) —— `sIdle` 同时侦测 `awvalid`（且 `out_state===out_sIdle`）与 `arvalid` 决定走写还是读分支；写分支在 `sWriteData` 收 `wdata` 并置 `write:=true`，随后 `when(write){ regs(addr) := dataOut }`（第 120-122 行）把数据落到对应寄存器。

注意地址换算 `awaddr(addrWidth-1, 2)`：取 32 位地址的 bit[31:2]，丢掉最低 2 位，结果直接当 `regs` 的 word 下标。

#### 4.2.4 代码实践

**实践目标**：把寄存器映射表与硬件状态机对上号，理解「写参数 → 置 valid → 自动清 valid」的一次性触发。

**操作步骤**（源码阅读型，无需上板）：

1. 在 `AXI4Lite2CTA.scala` 第 83-119 行旁，按本节的映射表逐字段核对：`regs(2)` 喂 `host_num_wf`，对应 u2-l1 的 `CSR_NUMW`；`regs(3)` 喂 `host_wf_size`，对应 `CSR_NUMT`，须与硬件 `num_thread` 一致。
2. 跟踪 `out_sIdle → out_sOutput` 转移条件（`input_valid`，即 `regs(0)(0)`）与回退条件（`io.data.fire` 后 `regs(0):=0.U`，第 113-118 行）。
3. 回答：为什么主 FSM 在 `sIdle` 要加 `out_state===out_sIdle` 这个守卫？

**需要观察的现象**：host 写完 0x04~0x4C 后写 0x00=1；下一个 `io.data.fire` 后 `regs(0)` 自动归零。

**预期结果**：host 轮询读 0x00，看到 0 即表明该 WG 已被 `CTAinterface` 接收，可以准备派发下一个。若 `out_state` 不在 idle，主 FSM 不接受写，host 会被 `awready` 卡住——这就是「派发期间不可改寄存器」的保护。

> 待本地验证：上述「轮询 0x00 归零」与「写 0x44 清挂起」的时序，可在仿真中用 AXI4-Lite VIP（或手写 BFM）写一个最小 WG 后观察波形确认。

#### 4.2.5 小练习与答案

**练习 1**：若 host 只写了 `regs(0)=1` 而没写 `regs(2)`（`host_num_wf`），会发生什么？
**答案**：`regs` 是 `RegInit` 全 0，所以 `host_num_wf=0`，CTA 调度器收到一个「0 个 warp」的非法 WG。模块本身不会拦截，依赖下游/host 软件保证参数完整。这正是 driver 要**按顺序写完所有参数再置 valid** 的原因。

**练习 2**：为什么 `regs(0)` 要在派发完成后自动清零，而不是让 host 自己写 0？
**答案**：自动清零让 host 能用「轮询归零」判断派发已被接收，构成天然的一次性触发与单次握手；若由 host 清零，则需额外协议区分「已接收」与「未接收」，更易出错。

**练习 3**：地址为什么用 `awaddr[31:2]` 而不是 `awaddr[31:0]`？
**答案**：寄存器按 32 位（4 字节）编址，最低 2 位是字节内偏移、对 4 字节寄存器无意义，丢弃后得到 word 下标，直接索引 `regs`。

---

### 4.3 AXI4Adapter：L2 TileLink ↔ AXI4 外存桥接

#### 4.3.1 概念说明

`AXI4Adapter` 解决的是「数据面」的翻译：L2 缓存（u6-l5）对外吐的是 TileLink 精简版的 A/D 通道，且每拍数据是一个**整条 cacheline 那么宽**的 beat；而外部 DDR 通常接 64 位窄 AXI4 总线。所以这个适配器要做两件事：

1. **协议翻译**：TileLink `Get`（读）→ AXI4 的 AR + R；TileLink `PutFullData`（写）→ AXI4 的 AW + W + B；返回时把 AXI4 的 R/B 拼回 TileLink 的 D（`AccessAckData`/`AccessAck`）。
2. **位宽转换**：把一次宽 TL 交易拆成一段 AXI4 **突发**（burst），拍数 `total_times = L2 数据位宽 / 64`。

`AXI4Adapter` 是 AXI4 **主**端：它代表 GPU 向 DDR 主动发起读/写突发。

#### 4.3.2 核心流程

```
total_times = cache_params.data_bits / AXI_params.dataBits   // 一次突发拍数

读（TL Get → AXI AR/R → TL AccessAckData）：
  AR 通道：opcode==Get 时 ar.valid 拉高
           addr   = TL address
           size   = log2(64/8) = 3        // 每拍 8 字节
           len    = total_times - 1       // 突发 total_times 拍
           burst  = 1 (INCR)
           id     = TL source             // 用 source 当 AXI id，回程据此还原
  R 通道：每拍把 r.data 收进 buffer_read(counter_read)，counter 累加
          r.last 时 buffer_read_valid:=1（整段凑齐）
  回填 D：outd.valid，opcode=AccessAckData，data=拼接后的整段宽数据，source=读到的 id

写（TL PutFullData → AXI AW/W/B → TL AccessAck）：
  AW 通道：opcode==PutFullData 时 aw.valid 拉高（addr/size/len/id 同读）
  W 通道：把 TL 一拍宽数据按 64 位切片填进 buffer_write，按 counter_write 逐拍发出
          counter 到 total_times-1 时 w.last:=1
  B 通道：b.ready:=1，收到写响应后回填 D：opcode=AccessAck，source=b.id
```

贯穿全程的「身份钥匙」是 **source/id 字段**：去程把 TL 的 `source` 塞进 AXI 的 `ar.id`/`aw.id`，回程 AXI 的 `r.id`/`b.id` 原样带回，用来组装 D 通道的 `source`。这与 u6-l5 讲的「L2 source 字段是请求—响应配对钥匙」完全对接——L2 MSHR 只认 `source`，不关心下面是 AXI 还是别的。

#### 4.3.3 源码精读

模块参数 `InclusiveCacheParameters_lite_withAXI` 把 L2 的 cache 参数与 AXI 参数打包；突发拍数由两者位宽比决定：

[ventus/src/axi/AXI4Adapter.scala:44-60](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Adapter.scala#L44-L60) —— 对外暴露 AXI4 主口 `AXI_master_bundle`、L2 侧 A 通道输入 `l2cache_outa`、D 通道输出 `l2cache_outd`；`total_times` 是位宽转换比，`buffer_read`/`buffer_write` 各开 `total_times` 个寄存器暂存一次突发。

**读通道（Get → AR）**。AR 各字段从 TL A 通道直接装配，关键是 `size`/`len`/`id`：

[ventus/src/axi/AXI4Adapter.scala:65-80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Adapter.scala#L65-L80) —— `ar.valid` 仅在 `opcode===Get` 时拉高；`size=log2(dataBits/8)`（每拍字节数）、`burst=1`（INCR）、`len=total_times-1`（突发拍数）、`id=TL source`。

**读数据回收**。R 通道每拍按 `counter_read` 把数据塞进对应槽位，`r.last` 时把整段标记有效，随后一次性拼成宽 D 通道数据：

[ventus/src/axi/AXI4Adapter.scala:99-121](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Adapter.scala#L99-L121) —— R 拍按计数填 `buffer_read`；`r.last` 归零计数并置 `buffer_read_valid`。

**写通道（PutFullData → AW/W/B）**。AW 握手后把 TL 一拍宽数据切片填进 `buffer_write`，W 通道按 `counter_write` 逐拍送出：

[ventus/src/axi/AXI4Adapter.scala:145-159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Adapter.scala#L145-L159) —— 在 `outa.fire` 且写操作时，把 `outa.data` 按 64 位切片写入各 `buffer_write` 段；W 通道按 `counter_write` 选段输出，到 `total_times-1` 时 `w.last` 置位。

**D 通道响应复用**。D 通道同时承载「读数据」与「写回应」，用 `b.valid` 二选一：写回应给 `AccessAck`，读给 `AccessAckData`，并把 id 还原成 source：

[ventus/src/axi/AXI4Adapter.scala:167-173](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Adapter.scala#L167-L173) —— `outd.valid` 由 `b.valid`（写回应）或 `buffer_read_valid`（读数据）触发；`source` 与 `opcode` 据此二选一，data 在写回应时为 0、读时为拼接的宽数据。第 173 行 `outa.ready` 综合各忙标志，确保不在突发进行中接收新请求。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 L2 读缺失（`Get`）如何变成一段 AXI4 突发再把数据拼回。

**操作步骤**：

1. 假设 `total_times = 4`（L2 beat 256 位、AXI 64 位）。在纸上画出：L2 发一个 `Get` → `AXI4Adapter` 发 AR（`len=3`）→ DDR 回 4 拍 R → `buffer_read(0..3)` 凑齐 → D 通道回一个 `AccessAckData`（256 位）。
2. 标注每拍 `counter_read` 的取值（0→1→2→3→0 on last）。
3. 回答：为什么 `outa.ready`（第 173 行）要 AND 上 `!buffer_read_busy && ar.ready` 等一堆条件？

**需要观察的现象**：一次 TL `Get` 对应恰好 `total_times` 拍 R，且只有 `r.last` 后 D 通道才出现一次有效。

**预期结果**：D 通道 `source` 与原 AR 的 `id`（即 TL `source`）一致，L2 MSHR 据此把数据塞回对应 miss 条目（接续 u6-l5 的回填链路）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 AXI 数据位宽从 64 改成 128（`AXI4BundleParameters(32,128,...)`），`total_times` 与突发行为如何变化？
**答案**：`total_times = data_bits/128`，减半；AR `len` 同步减半，一次突发搬完同样 cacheline 所需拍数更少，但每拍更宽。

**练习 2**：为什么用 TL `source` 当 AXI `id`，而不是固定为 0？
**答案**：L2 用 `source` 区分多个在途 miss；若 AXI id 固定，回程 R/B 就无法区分属于哪次请求。把 `source` 当 id 带过去再带回来，D 通道才能还原正确的 `source` 给 L2 MSHR 配对。

**练习 3**：写回应（B 通道）与读数据（R 通道）都走同一个 D 通道回 L2，会不会冲突？
**答案**：两者由 `b.valid` 与 `buffer_read_valid` 二选一驱动（第 167-170 行），且各自只在对应交易完成时拉高一拍，硬件上不会同拍争用；但 D 通道是串行化的，所以读写回应在 L2 侧是顺序消费的。

---

### 4.4 GPGPU_axi_top / GPGPU_axi_adapter_top：顶层组合与 FPGA 集成

#### 4.4.1 概念说明

`AXI4Lite2CTA` 和 `AXI4Adapter` 各管一摊（控制面 / 数据面），真正把它们和 `GPGPU_top` 拼成一个完整 IP 的是 `GPGPU_axi_top`。它的对外接口极简：一个 AXI4-Lite **从**口（接 host CPU 配置寄存器）+ 一个 AXI4 **主**口（接外部 DDR）。对集成方而言，Ventus 就是一个标准的「AXI4-Lite slave + AXI4 master」加速器 IP。

`GPGPU_axi_adapter_top` 是再外面一层薄包装，区别只在 AXI4 主口的 `idBits` 取值不同——它专为 FPGA 综合流程（`make fpga-verilog`）而设。

#### 4.4.2 核心流程

```
GPGPU_axi_top 内部例化三个模块并连线：

  io.s (AXI4-Lite slave) ──ctl──► AXI4Lite2CTA ──data(host2CTA_data)──► GPGPU_top.io.host_req
                                 ◄─rsp(CTA2host_data)── GPGPU_top.io.host_rsp

  GPGPU_top.io.out_a(0) ──► AXI4Adapter.io.l2cache_outa
  GPGPU_top.io.out_d(0) ◄── AXI4Adapter.io.l2cache_outd
  AXI4Adapter.io.AXI_master_bundle ──► io.m (AXI4 master) ──► 外部 DDR
```

要点：
- `GPGPU_top` 默认有 `NL2Cache` 路 `out_a/out_d`，这里只连第 0 路（`out_a(0)`/`out_d(0)`），即单 L2 / 单 DDR 通道的典型配置。
- `host_req`/`host_rsp` 是 u3-l1 调度器入口与完成出口，`AXI4Lite2CTA` 直接对接它们，无需 `CTAinterface` 的额外翻译——因为 `host2CTA_data` 本来就是为这组接口定义的。

#### 4.4.3 源码精读

先看 `host2CTA_data` / `CTA2host_data` 两个 Bundle 的定义，它们是 host↔CTA 的数据契约（u3-l1 已从调度器侧讲过，这里看字段全集）：

[ventus/src/top/GPGPU_top.scala:32-53](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L32-L53) —— `host2CTA_data` 含 wg_id、num_wf、wf_size、start_pc、kernel_asid、kernel_size_3d、csr_knl、各 vgpr/sgpr/lds/pds 用量与基址、asid 等全部派发参数；`CTA2host_data` 极简，只有一个 `inflight_wg_buffer_host_wf_done_wg_id`（完成的 wg_id）。字段位宽取自 `CTA_SCHE_CONFIG`（u2-l3）。

`GPGPU_axi_top` 的组合与连线：

[ventus/src/top/GPGPU_top.scala:116-137](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L116-L137) —— 例化 `GPGPU_top`、`AXI4Lite2CTA(32,32)`、`AXI4Adapter`，按上图连接：`io.s↔axi_lite_adapter.io.ctl`、`io.m↔axi_adapter.io.AXI_master_bundle`、`out_a(0)/out_d(0)↔axi_adapter`、`host_req↔axi_lite_adapter.io.data`、`host_rsp↔axi_lite_adapter.io.rsp`。AXI4 主口参数 `AXI4BundleParameters(32,64,source_bits)`：32 位地址、64 位数据、id 宽度取 L2 `source_bits`（见 Parameters.scala）。

`GPGPU_axi_adapter_top` 是 FPGA 用薄包装，AXI4 主口 `idBits` 改为更小的 `log2Up(num_sm)+log2Up(num_warp)+1`（更省综合资源）：

[ventus/src/top/GPGPU_top.scala:138-148](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L138-L148) —— 仅 `io.s`/`io.m` 透传给内部的 `GPGPU_axi_top`，二者接口形状一致。

**FPGA 构建入口**。`make fpga-verilog` 以 `GPGPU_axi_adapter_top` 为顶层，先发 chirrtl 再用 firtool 分拆存储器：

[Makefile:28-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L28-L30) —— `--module top.GPGPU_axi_adapter_top` 生成 chirrtl，`firtool --split-verilog --repl-seq-mem --repl-seq-mem-file=mem.conf` 把 SRAM 分离到 `mem.conf`，便于综合时替换为 BRAM 宏单元（详见 u7-l5）。

**host 驱动软件流程**（以 `naive_driver.h` 为参考，注意其偏移过时）。`GpuSendTask` 依次写各参数寄存器，最后写 valid=1 并轮询 valid 归零；`GpuWatchTask` 轮询完成标志、读 done wg_id、再写 0 重新使能：

[ventus/fpga_test/scrs/driver/naive_driver.h:96-141](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.h#L96-L141) —— `GpuSendTask`：按序 `Gpu_WriteReg` 写 wg_id/num_wf/wf_size/start_pc/各资源量，最后写 `GPU_VALID_OFFSET=1` 触发，再轮询该寄存器归零判断「已被接收」；`GpuWatchTask`：轮询 `GPU_WG_VALID_OFFSET==1`，读 `GPU_WG_ID_DONE_OFFSET` 取回完成的 wg_id，最后写 0 到 `GPU_WG_VALID_OFFSET` 重新使能。这套「写参数→置 valid→轮询归零→轮询完成→读 wg_id→写 0 重使能」的流程与 4.2 分析的硬件状态机完全对应。

> ⚠️ 再次提醒：`naive_driver.h` 里 `GPU_*_OFFSET` 的具体数值（如 done 在 0x38/0x3c）是旧布局，**上板前必须按 4.2.3 的映射表重新对齐**到当前 RTL（done 在 0x40/0x44）。

#### 4.4.4 代码实践

**实践目标**：理清「两条 AXI 总线 + 三模块」的整体拓扑，为上板/集成画一张可交付的框图。

**操作步骤**：

1. 执行 `make fpga-verilog`（或查阅 `gen_fpga_verilog/` 已生成产物），确认顶层模块名是 `GPGPU_axi_adapter_top`。
2. 在生成 Verilog 里找到顶层端口，列出 `s_aw_*`/`s_w_*`/`s_b_*`/`s_ar_*`/`s_r_*`（AXI4-Lite 从）与 `m_ar_*`/`m_r_*`/`m_aw_*`/`m_w_*`/`m_b_*`（AXI4 主）。
3. 对照 4.4.2 的连接图，在框图上标出 host CPU、`AXI4Lite2CTA`、`GPGPU_top`、`AXI4Adapter`、外部 DDR 的位置与数据方向。

**需要观察的现象**：顶层只有两组 AXI 端口，内部 `host_req`/`host_rsp`/`out_a`/`out_d` 都被适配器消化。

**预期结果**：得到一张「host CPU —(AXI4-Lite)→ Ventus —(AXI4)→ DDR」的完整 SoC 集成框图，控制面与数据面分离清晰。

> 待本地验证：`make fpga-verilog` 是否成功、`mem.conf` 是否生成，依赖本机 firtool/Mill 环境（u1-l2）。

#### 4.4.5 小练习与答案

**练习 1**：`GPGPU_axi_top` 里为什么只连 `out_a(0)`/`out_d(0)`，而不是全部 `NL2Cache` 路？
**答案**：这是单 L2 / 单 DDR 通道的典型配置；多 L2 时需多个 `AXI4Adapter` 或一个多路 AXI 互联，本顶层未做，留给集成方按需扩展。

**练习 2**：`GPGPU_axi_top` 与 `GPGPU_axi_adapter_top` 的唯一实质区别是什么？为什么 FPGA 用后者？
**答案**：AXI4 主口的 `idBits` 不同（前者用 `source_bits`，后者用 `log2Up(num_sm)+log2Up(num_warp)+1`）。后者 id 宽度更小、更省 FPGA 逻辑资源，适合综合；前者更贴近 L2 source 完整位宽。

**练习 3**：host 驱动里「轮询 valid 归零」和「轮询 done 置位」分别用于确认什么？
**答案**：前者确认 CTA 调度器**已接收**该 WG（4.2 的输出 FSM 已 fire 并清 `regs(0)`）；后者确认该 WG **已执行完成**（4.2 的完成回报锁存了 `regs(16/17)`）。

---

## 5. 综合实践

**任务**：编写一段 AXI4-Lite 写序列（伪 C 或波形描述），完整派发一个最小 kernel 并回收完成。把本讲的寄存器映射、握手状态机、host 软件流程串起来。

**要求**：

1. 假设要派发一个 kernel：`wg_id=5`，`num_wf=2`，`wf_size=32`（与硬件 `num_thread` 一致），`start_pc=0x80000000`，其余资源量给一组合理小值（如 `vgpr_size_total=64`、`sgpr_size_total=16`、`lds_size_total=256`，per-wf 各减半）。
2. 用 4.2.3 的**权威映射表**（不要用 `naive_driver.h` 的旧偏移），写出依次写每个寄存器的 AXI4-Lite 写交易（每笔 = AW 给地址 → W 给数据 → B 收响应）。
3. 最后写偏移 0x00 = 1 触发派发。
4. 轮询读 0x00，描述何时归零、其含义。
5. 轮询读 0x44（完成挂起标志），置位后读 0x40 取回完成的 `wg_id`，确认它 == 5。
6. 写 0x44 = 0 重新使能，准备下一个 kernel。

**示例伪代码**（基于 4.2.3 映射表，示例代码非项目原有）：

```c
// 示例代码：基于 AXI4Lite2CTA.scala 当前 HEAD 的寄存器映射
axi_write(0x04, 5);            // host_wg_id
axi_write(0x08, 2);            // host_num_wf
axi_write(0x0C, 32);           // host_wf_size  == 硬件 num_thread
axi_write(0x10, 0x80000000);   // host_start_pc
axi_write(0x14, 64);           // host_vgpr_size_total
axi_write(0x18, 16);           // host_sgpr_size_total
axi_write(0x1C, 256);          // host_lds_size_total
axi_write(0x20, 32);           // host_vgpr_size_per_wf
axi_write(0x24, 8);            // host_sgpr_size_per_wf
axi_write(0x2C, 0x00010000);   // host_pds_baseaddr（示例值）
axi_write(0x48, 32);           // host_pds_size_per_wf
// ...其余字段按需
axi_write(0x00, 1);            // 触发派发
while (axi_read(0x00) != 0) ;  // 等待 CTA 接收（regs(0) 自动清零）
while (axi_read(0x44) == 0) ;  // 等待完成挂起标志置位
u32 done = axi_read(0x40);     // 取回完成的 wg_id，应 == 5
assert(done == 5);
axi_write(0x44, 0);            // 清挂起、重新使能下一笔完成
```

**验收点**：

- 每条 `axi_write` 对应一组 AW/W/B 三通道交易，能解释 `awaddr[31:2]` 如何定位到 `regs` 下标。
- 能说清 `0x00` 自动归零（输出 FSM fire）与 `0x44` 由硬件置位（完成回报锁存）这两个「硬件自动改写」寄存器的机制。
- 能解释为什么这套流程里 host 不需要额外的中断或 DMA——控制面全靠轮询这 20 个寄存器。

> 待本地验证：可在 Verilator 仿真中给 `GPGPU_axi_top` 接一个最小 AXI4-Lite BFM 跑上述序列，或上 FPGA 用 MicroBlaze 跑对齐偏移后的 `naive_driver`。

## 6. 本讲小结

- Ventus 用两条 AXI 总线对外集成：**AXI4-Lite 从口**（`AXI4Lite2CTA`，控制面，host 写寄存器派发 kernel）+ **AXI4 主口**（`AXI4Adapter`，数据面，GPU 访问外部 DDR）。
- `AXI4Lite2CTA` 用 20 个 32 位寄存器承载 `host2CTA_data` 全部字段；写 `regs(0)=1` 触发一次性派发，硬件发完后自动清零；完成回报锁存进 `regs(16/17)`，host 读后写 0 重新使能。
- `AXI4Adapter` 把 L2 的 TileLink A/D（`Get`/`PutFullData`）翻译成 AXI4 突发（AR/R 与 AW/W/B），并完成「宽 cacheline ↔ 64 位」位宽转换；用 TL `source` 当 AXI `id` 实现回程配对。
- `GPGPU_axi_top` 把两个适配器与 `GPGPU_top` 拼成标准「AXI4-Lite slave + AXI4 master」IP；`GPGPU_axi_adapter_top` 是 FPGA 综合用的薄包装（`idBits` 更省资源），由 `make fpga-verilog` 生成。
- host 驱动流程为「写参数 → 置 valid → 轮询 valid 归零（已被接收）→ 轮询完成 → 读 wg_id → 写 0 重使能」；`naive_driver.h` 的流程正确但其偏移表已过时，须以 `AXI4Lite2CTA.scala` 为准。

## 7. 下一步学习建议

- **u7-l1（MMU 与 TLB/PTW）**：本讲的 `host_asid`/`host_kernel_asid`（`regs(19)`）就是为 MMU 准备的；开启 `MMU_ENABLED` 后，PTW 的页表遍历请求也会经 `AXI4Adapter` 这条数据面通路访问 DDR，可与本讲对照阅读。
- **u7-l5（FPGA 部署与参数定制）**：本讲的 `make fpga-verilog` 流程、`mem.conf` 与 SRAM 分离将在该讲展开，配合 `fpga_test` 的 Vivado 工程上板验证。
- **继续阅读源码**：`ventus/src/top/GPGPU_top.scala` 的 `CTAinterface`（u3-l1）看 `host2CTA_data` 如何进入调度器；`ventus/src/L2cache/Parameters.scala` 看 `source_bits` 的完整组成，理解 `AXI4Adapter` id 宽度的来源。
