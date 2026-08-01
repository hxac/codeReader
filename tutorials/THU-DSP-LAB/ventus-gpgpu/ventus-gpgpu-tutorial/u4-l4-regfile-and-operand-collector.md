# 寄存器堆与操作数收集

## 1. 本讲目标

上一讲（u4-l3）我们解决了「哪些指令可以发射」的问题——记分板（Scoreboard）把存在数据冒险、控制冒险的指令按在队头，让无冲突的指令经 `ibuffer2issue` 流到双发射口。但一条指令要真正进执行单元，还差最后一样东西：**操作数**。`vadd.vv v3, v1, v2` 要算之前，必须先把 `v1`、`v2` 两个向量寄存器的值读出来。

本讲就讲清楚这一步：

- 寄存器堆（Register File）是怎样用多个 **bank**（存储体）组织的，标量与向量有什么不同；
- 为什么要把寄存器「交织（interleave）」分散到多个 bank，而不是做一个多端口大阵列；
- `operandCollector`（操作数收集器）如何为每一条就绪指令，在多个周期内从各个 bank 把 3 个源操作数和 mask 收齐，再交给执行单元；
- warp 启动时由 CTA 调度器分配的 `sgpr_base` / `vgpr_base` 基址，在这里如何变成真实的 bank 地址。

学完后，你应该能画出「一条向量指令从拿到控制信号，到 3 个源操作数和 mask 被组装好送进 issue」的逐拍时序图。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**为什么要「操作数收集器」？** GPU 一个 SM 里同时跑着多个 warp（默认 8 个），每个 warp 每拍都可能要发射指令，每条指令又要读 2~4 个寄存器。如果用一个统一的、端口数极大的寄存器堆来满足所有并发读请求，硬件面积会随端口数平方增长（多端口 SRAM 的代价）。Ventus 借鉴了经典 GPU 设计（Lindholm 等人提出的 operand collector 思路）：把寄存器堆切成若干 **bank**，每个 bank 只有一个读端口、一个写端口，再配一个**收集器**，让各 warp 的读请求先去竞争 bank，竞争到了再去读，读到的数据按「哪条指令的哪个操作数」送回对应的收集槽。这样用少量端口就能虚拟出「很多读口」的效果，代价是多花几拍去收集。

**标量寄存器 vs 向量寄存器。** Ventus 沿用 RVV（RISC-V 向量扩展）思路（见 u2-l1、u4-l2）：

- **标量寄存器（SGPR，`x` 寄存器）**：一个 warp 共用一个值，宽度 32 bit。
- **向量寄存器（VGPR，`v` 寄存器）**：一个 warp 内每个 thread（默认 32 个）各有一个 32 bit 值，合起来是一个 `Vec(32, UInt(32.W))`。所以一个向量寄存器条目存的是 32 个 lane 的数据。

这就解释了为什么本讲会看到两套 bank：`RegFileBank`（标量，每条目 32 bit）和 `FloatRegFileBank`（向量，每条目是 32 个 32 bit）。

> 关于「端口数」的说明：本讲主题里提到「标量 3 读 / 向量 4 读」，指的是**一条指令需要收集的操作数槽数**——标量指令通常用 op1/op2/op3 共 3 个槽，向量指令额外再要一个 mask 槽共 4 个。物理上每个 bank 仍是 1 读 1 写，靠 4 个 bank 并行 + 收集器把「每条指令最多 4 个操作数」的需求满足。这是 bank 化设计的核心动机，4.1 节会展开。

## 3. 本讲源码地图

本讲聚焦两个文件，并参考参数定义与流水线总装文件来定位连接关系：

| 文件 | 作用 |
| --- | --- |
| [ventus/src/pipeline/regfile.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/regfile.scala) | 寄存器堆 bank：`RegFileBank`（标量）、`FloatRegFileBank`（向量），以及立即数生成器 `ImmGen` |
| [ventus/src/pipeline/operandCollector.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala) | 操作数收集器全家桶：`collectorUnit`（收集槽）、`operandArbiter`（读仲裁）、`crossBar`（回程交叉开关）、`instDemux`（指令分发到空闲收集槽）、`operandCollector`（顶层总装，含写回与双发射） |
| [ventus/src/top/parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | 关键参数：`num_bank`、`num_collectorUnit`、`num_vgpr`/`num_sgpr`、`widSliceHigh`、`SGPR_ID_WIDTH` 等 |
| [ventus/src/pipeline/pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala) | 流水线总装：例化 `operandCollector`，把它的输入/输出接到 `ibuffer2issue`、`csrfile`、写回与执行单元 |
| [ventus/src/pipeline/CSR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala) | 每个 warp 的 `sgpr_base` / `vgpr_base` 基址在 warp 启动（派发）时被写入 CSR |

先记住一组默认参数（来自 `parameters`，详见 4.1.3），后面所有地址计算都用到：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `num_bank` | 4 | 寄存器堆 bank 数（标量、向量各 4 个） |
| `num_warp` | 8 | 每 SM 的 warp 数 |
| `num_collectorUnit` | = `num_warp` = 8 | 收集槽个数，每个 warp 可独占一个 |
| `num_thread` | 32 | 每 warp 线程数（向量 lane 数） |
| `num_sgpr` / `num_vgpr` | 2048 / 1024 | 全 SM 共享的标量/向量寄存器总槽位 |
| `depth_regBank` | 8 | bank 内地址位宽 = `log2Ceil(num_vgpr/num_bank)` |

## 4. 核心概念与源码讲解

本讲按「自底向上」拆成 4 个最小模块：先看单个 bank 怎么存怎么读（4.1），再看一个收集槽怎么把一条指令的操作数凑齐（4.2），接着看读请求如何竞争 bank、读回的数据如何路由（4.3），最后用顶层 `operandCollector` 把派发、写回、双发射串起来（4.4）。

### 4.1 寄存器堆 Bank：RegFileBank 与 FloatRegFileBank

#### 4.1.1 概念说明

寄存器堆不是「每个 warp 一块独立 SRAM」，而是**全 SM 所有 warp 共享一组 bank**，靠 `(warp 基址 + 寄存器号)` 寻址。这样做的好处是：CTA 调度器（u3-l2）可以按每个 workgroup 实际需要的寄存器量，动态分配一段连续的寄存器窗口（基址），从而灵活支持不同规模的 kernel，而不是把寄存器静态等分给每个 warp。

因为共享，就必须解决「多个 warp 同拍都要读寄存器」的端口冲突。Ventus 的办法是把总池切成 `num_bank=4` 个 bank，再把寄存器**交织（interleave）**分散进去：连续编号的寄存器轮流落在不同 bank。这样一条向量指令要读的 `v1`、`v2` 大概率落不同 bank，可以同拍并行读出。

#### 4.1.2 核心流程：交织寻址与读写

设 warp 编号为 \(w\)，要访问的（经 regext 扩展后的 8 位）寄存器号为 \(j\)，`num_bank = B = 4`。寄存器 \(j\) 落在哪个 bank、bank 内地址是多少，由下面两个公式决定（4.2 节会在 `collectorUnit` 里看到一模一样的实现）：

\[ \text{bank}(w, j) = \big(\, w_{\text{low}} + j_{\text{low}} \,\big) \bmod B \]

\[ \text{addr}(w, j) = \big\lfloor \text{base}(w) / B \big\rfloor + \big\lfloor j / B \big\rfloor \]

其中 \(w_{\text{low}} = w[\text{widSliceHigh}:0]\)（默认取 wid 低 2 位），\(j_{\text{low}} = j[\log_2 B - 1 : 0]\)（寄存器号低 2 位），\(\text{base}(w)\) 是该 warp 的 `sgpr_base` 或 `vgpr_base`。

直观理解：

- **bank 号取「warp 低位 + 寄存器号低位」**：既让同一 warp 的连续寄存器散到不同 bank，也让不同 warp 进一步错开，减少跨 warp 的冲突。
- **bank 内地址取「基址 + 寄存器号」整除 B**：因为每 B 个连续寄存器分别进 B 个 bank，每个 bank 只承担 1/B 的条目，地址自然要除以 B。

读写时序：每个 bank 用 `SyncReadMem`（同步读，读延迟 1 拍）。地址这一拍送上，数据下一拍出。为了处理「上一拍刚写、这一拍就要读同一个地址」的 RAW 冒险，bank 内带一个 **bypass（前递）**：若上一拍写过同一地址，直接把写数据前递给读口，不读旧值。写口每拍至多写一个地址。

#### 4.1.3 源码精读

**标量 bank `RegFileBank`** —— 每个 bank 一个读口、一个写口，带 bypass：

[regfile.scala:L18-L39](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/regfile.scala#L18-L39) 例化了 `SyncReadMem(NUMBER_SGPR_SLOTS/num_bank, UInt(32.W))`（默认 2048/4 = 512 个 32 位标量槽），核心两行：

```scala
bypassSignal := RegNext((io.rsidx === io.rdidx) & io.rdwen)
io.rs := Mux(bypassSignal, RegNext(io.rd), regs.read(io.rsidx))
```

即「上一拍写了同一地址就前递写数据，否则正常读」。`GVM_ENABLED` 分支（协同仿真，见 u7-l4）把 `SyncReadem` 换成 `Vec` 寄存器以便 DPI-C 读取全部值，并多一个 `all_regs` 输出口，本质读写逻辑相同。

**向量 bank `FloatRegFileBank`** —— 每个条目是一个完整向量（32 个 lane）：

[regfile.scala:L51-L66](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/regfile.scala#L51-L66) 用 `SyncReadMem(NUMBER_VGPR_SLOTS/num_bank, Vec(num_thread, UInt(32.W)))`（默认 1024/4 = 256 个向量槽，每槽 32 个 32 位）。两点与标量不同：

```scala
io.rs := Mux(bypassSignal, RegNext(io.rd), regs.read(io.rsidx))   // 读出整条向量
// v0 mask 读口被注释/简化为全 1，省一个读口：
io.v0 := WireInit(VecInit.fill(num_thread)(~(0.U(xLen.W))))
when (io.rdwen) { regs.write(io.rdidx, io.rd, internalMask) }      // 带 per-lane 写掩码
```

- 读 `io.rs` 一次返回 32 个 lane 的值（一个完整向量寄存器）。
- 写入 `regs.write(io.rdidx, io.rd, internalMask)` 的第三个参数是 **per-lane 写掩码** `rdwmask`：只有 mask 为真的 lane 才更新，其余保持——这正是 SIMT 下「被屏蔽的 thread 不写回」所需要的。
- `io.v0`（v0 掩码寄存器）在当前实现里被**强制置全 1**。源码注释明确写着「v0 mask is not used in the current implementation, remove it to reduce a read port」。这意味着硬件这条「从 bank 读 v0 当掩码」的路径目前是关闭的，实际的逐 thread 掩码在 `pipe.scala` 里由 SIMT stack 的 `out_mask` 提供（见 4.4.3 与 u5-l5）。这是一个读源码才能发现的真实细节，不要被「主题描述」误导。

**关键参数**都在 [parameters.scala:L18-L34](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L18-L34)：`num_bank=4`、`num_collectorUnit=num_warp`、`num_vgpr=128*num_warp`、`num_sgpr=256*num_warp`、`depth_regBank=log2Ceil(num_vgpr/num_bank)`，以及

```scala
def widSliceHigh = scala.math.min(log2Ceil(num_bank) - 1, depth_warp - 1)
```

它决定参与 bank 编号的 wid 位数（默认 `min(1,2)=1`，即 wid 低 2 位），保证不会越界取到 wid 之外。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证「每个 warp 拥有多少寄存器」「交织后连续寄存器确实落在不同 bank」。

**步骤**：

1. 在 `parameters.scala` 确认 `num_sgpr=256*num_warp`、`num_vgpr=128*num_warp`，计算每个 warp 分摊：默认每 warp 256 个标量寄存器、128 个向量寄存器。
2. 取 warp \(w=0\)，假设 `sgpr_base(0)=0`、`vgpr_base(0)=0`。用 4.1.2 的公式手算标量寄存器 `x0..x7` 各自的 (bank, addr)：
   - \(j=0\): bank = (0+0)%4 = **0**, addr = 0/4 + 0 = 0
   - \(j=1\): bank = (0+1)%4 = **1**, addr = 0
   - \(j=2\): bank = **2**, addr = 0
   - \(j=3\): bank = **3**, addr = 0
   - \(j=4\): bank = (0+0)%4 = **0**, addr = 4/4 = 1
3. 观察：`x0~x3` 落在 4 个不同 bank 的同一地址 → 同拍可全部读出；`x4` 回到 bank0 但地址进到 1。

**需要观察的现象 / 预期结果**：连续 4 个寄存器一定落在 4 个不同 bank，因此「一条指令同时读 v1、v2」这种相邻寄存器访问天然无 bank 冲突；只有当两条不同指令、不同 warp 抢同一 bank 时才需要仲裁（4.3 节）。结果可手算确认，无需运行仿真；若要上机核对，可改 `num_bank` 重新 `make verilog`，观察 `GPGPU_top.v` 中 bank 实例数量变化（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 bank 号用「warp 低位 + 寄存器号低位」再取模，而不是直接用寄存器号取模？
**答案**：只按寄存器号取模的话，不同 warp 的「同名寄存器」（如所有 warp 的 v0）会落在同一 bank；当多个 warp 同时读各自的 v0（这在 SIMT 中很常见）就会全部冲突。加入 warp 低位后，不同 warp 的同名寄存器被错开到不同 bank，降低跨 warp 冲突。

**练习 2**：向量 bank 的写入为什么需要 per-lane 写掩码，而标量 bank 不需要？
**答案**：向量指令在掩码下执行时，被屏蔽的 thread（lane）不应更新它的寄存器值，所以写回要按 lane 选择性更新（`regs.write(addr, data, mask)`）。标量寄存器一个 warp 只有一个值，不存在「部分 thread 写」的概念，整写即可。

---

### 4.2 操作数收集单元 collectorUnit

#### 4.2.1 概念说明

`collectorUnit`（收集槽）是操作数收集器的核心。可以这样理解：它是一个「为某一条指令专门预留的小格子」，格子有 4 个槽位，分别对应一条指令的 op1、op2、op3 和 mask。一条指令被分进来后，格子负责把这 4 个操作数从各个 bank 一个个「搬」进对应的槽位，全部到齐后再作为一个完整的 `issueIO` 整包送给执行单元。

默认 `num_collectorUnit = num_warp = 8`，即每个 warp 可以常驻一个收集槽，避免频繁争抢。空闲的槽位由 `instDemux`（4.4 节）分配给新就绪的指令。

#### 4.2.2 核心流程：三态有限状态机

`collectorUnit` 用一个三态 FSM 推进：

```
        control.fire(拿到一条指令)            4个操作数全部ready
  s_idle ────────────────────────►  s_add  ──────────────────►  s_out
   ▲                                   │                          │
   │                                   │ (逐拍从bank收回数据,      │
   │              issue.fire           │  ready位陆续置1)          │
   └──────────────────────────────────────────────────────────────┘
                  (操作数送出, 下一条进)
```

- **s_idle**：等待 `control` 口送来一条指令（`CtrlSigs`）。一旦 `fire`，把控制信号锁存进 `controlReg`，按 `sel_alu1/2/3` 计算出 4 个槽位的「寄存器号、类型、是否需要去 bank 读」。对于**立即数、PC、不需掩码**等不需要读 bank 的槽位，直接把值算好、`ready` 位置 1；需要读 bank 的槽位 `ready` 保持 0，进入 `s_add`。
- **s_add**：每拍向仲裁器发读请求（`outArbiterIO(i).valid`），等 bank 的数据经 crossbar 回来（`bankIn(i).fire`），把数据写进对应槽位并 `ready(i):=1`。当 4 个 `ready` 全部等于 4 个 `valid`（即全部收齐），进入 `s_out`。
- **s_out**：把 4 个槽位的内容打包成 `issueIO`（`alu_src1/2/3` + `mask` + `control`）输出；下游 `fire` 后回到 `s_idle` 接下一条。

每个槽位的「类型」`rsType` 是关键路由标签：

| `rsType` | 含义 | 处理方式 |
| --- | --- | --- |
| 0 | PC 或 mask | 不发 bank 读请求（mask 走单独的 v0 通路，见 4.1.3） |
| 1 | 标量寄存器 | 向标量 bank 仲裁器发请求 |
| 2 | 向量寄存器 | 向量 bank 仲裁器发请求 |
| 3 | 立即数 | 由 `ImmGen` 现场生成，不发请求 |

#### 4.2.3 源码精读

**接口**——4 个操作数槽位 + 收齐后整包发射：[operandCollector.scala:L49-L59](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L49-L59)。注意 `control`（输入指令）、`bankIn`（4 路 bank 回程数据）、`outArbiterIO`（4 路向 bank 的读请求）、`issue`（收齐后输出）、以及 warp 基址 `sgpr_base` / `vgpr_base`。

**bank 号与 bank 内地址的计算**——正是 4.1.2 那两个公式的实现，[operandCollector.scala:L100-L127](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L100-L127)：

```scala
io.outArbiterIO(i).bits.bankID := ... controlReg.wid(widSliceHigh, 0) + regIdx(i)(log2Ceil(num_bank)-1, 0)   // bank号
// 标量 (rsType==1):
io.outArbiterIO(i).bits.rsAddr := (io.sgpr_base(controlReg.wid) >> log2Ceil(num_bank).U) + (regIdx(i) >> log2Ceil(num_bank).U)
// 向量 (rsType==2): 用 vgpr_base
```

即 bank = `wid低位 + regIdx低位`，addr = `base>>log2(num_bank) + regIdx>>log2(num_bank)`。

**s_idle 里区分「要不要读 bank」**——[operandCollector.scala:L223-L254](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L223-L254)：当 `sel_alu1===A1_IMM` 时直接把 `ImmGen` 结果填进 op1 并 `ready(0):=1`；`A1_PC` 时填 PC；op2 的 `A2_IMM` / `A2_SIZE`（常量 4）同理；不需掩码（`!mask`）时把 mask 槽置全活动并 `ready(3):=1`。这些槽位「当场就绪」，不必等 bank。

> 一个容易踩坑的点：`A3_X` 与 `A3_PC` 在 [DecodeUnit.scala:L31-L35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/DecodeUnit.scala#L31-L35) 里**数值相同（都=0）**。因此没有第三操作数的指令（如 `vadd.vv`）会被当作 `A3_PC` 处理，在 [operandCollector.scala:L243-L247](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L243-L247) 命中 `sel_alu3===A3_PC && branch=/=B_R` 分支，op3 被填成 `imm+pc`（无害的 don't-care）并立即 `ready(2):=1`。这样收集槽才不会因为「op3 没人提供数据」而卡死。

**回程数据装配**——[operandCollector.scala:L265-L318](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L265-L318)：根据 `bankIn(i).bits.regOrder`（这是第几个操作数，由 crossbar 带回）把数据写进 `rsReg(0/1/2)` 或 `mask`，并置对应 `ready`。对标量读（`A1_RS1`）有个巧妙处理：

```scala
A1_RS1 -> Mux(regIdx(0).orR, VecInit.fill(num_thread)(io.bankIn(i).bits.data(0)), 0.U...)
```

即标量值只存在 lane0（`data(0)`），读出后**广播到全部 32 个 lane**（因为后续执行单元是按向量 lane 并行的，标量要复制）；若 `regIdx` 为 0（即 `x0`）则读出 0，等价于「`x0` 恒零」。

最后，[operandCollector.scala:L319-L322](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L319-L322) 把收齐的 4 项打包：`io.issue.bits.alu_src1/2/3 := rsReg(0/1/2); io.issue.bits.mask := mask`。

#### 4.2.4 代码实践（源码阅读型）

**目标**：用一条具体指令走一遍 `collectorUnit` 的状态与数据。

**指令**：`vadd.vv v3, v1, v2`（warp 0，不带掩码）。译码后大致是 `sel_alu1=A1_VRS1`、`reg_idx1=1`；`sel_alu2=A2_VRS2`、`reg_idx2=2`；`sel_alu3=A3_X(=0)`、`branch=B_N`；`mask=false`。

**步骤**（逐拍推导，假设 `vgpr_base(0)=0`，且无其他 warp 抢占 bank1/bank2）：

| 拍 | 状态/动作 | 关键信号 |
| --- | --- | --- |
| T | `control.fire`，进入 `s_add` | op0: bank=(0+1)%4=**1**, addr=0, rsType=2(向量); op1: bank=(0+2)%4=**2**, addr=0, rsType=2; op2 立即就绪(imm+pc); op3=mask 立即就绪(全活动) |
| T+1 | 仲裁器把 bank1 的 op0 请求、bank2 的 op1 请求都授权 | `vectorBank(1).rsidx=0`(读 v1), `vectorBank(2).rsidx=0`(读 v2); SyncReadMem 锁存读 |
| T+2 | bank 数据出来，crossbar 按 `RegNext(readchosen)` 路由回本 CU | `bankIn(regOrder=0)` 带回 v1 → `rsReg(0):=v1, ready(0):=1`; `bankIn(regOrder=1)` 带回 v2 → `rsReg(1):=v2, ready(1):=1` |
| T+2 末 | 4 个 ready 全齐 → `s_out` | `io.issue.valid` 拉高，`alu_src1=v1, alu_src2=v2` |
| T+3 | 下游 `issue.fire` → 回 `s_idle` | 释放本收集槽，可接下一条 |

**需要观察的现象 / 预期结果**：由于 v1、v2 落在 bank1、bank2 两个不同 bank，两个读请求能在 T+1 同拍被授权、T+2 同拍一起返回，收集只需约 2 拍（不算派发与发射握手）。若 v1、v2 恰好落在同一 bank（例如读 v1、v5），则仲裁器只能先选一个，另一个要等下一拍，收集多花 1 拍——这就是 bank 冲突的代价。结果可手算；上机可在仿真波形里观察 `collectorUnit` 的 `state` 与各 `ready` 位（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：一条标量 `add x5, x6, x7`（两个寄存器源），收集槽里哪几个槽位需要真正读 bank？哪几个立即就绪？
**答案**：op0（x6，`A1_RS1`，标量）和 op1（x7，`A2_RS2`，标量）需要读标量 bank；op2（`A3_X` 当 `A3_PC` 处理）立即填 `imm+pc` 就绪；mask 槽因 `!mask` 立即填全活动就绪。所以只发 2 个标量 bank 读请求。

**练习 2**：为什么标量读出后要广播到全部 lane，而向量不用？
**答案**：执行单元（如 vALU）是按 32 个 lane 并行运算的，标量参与运算时每个 lane 都要用到同一个标量值，所以把 `data(0)` 复制成 `Vec(32)`。向量本身每个 lane 就有独立值，直接用整条 `data` 即可。

---

### 4.3 读仲裁器与交叉开关：operandArbiter + crossBar

#### 4.3.1 概念说明

收集槽向 bank 发读请求，但 bank 只有 4 个、读口各一个，而最多有 `num_collectorUnit × 4 = 32` 个请求同时要求读。这就需要两个组件：

- **`operandArbiter`（读仲裁器）**：每个 bank 配一个循环优先级仲裁器（`RRArbiter`），从所有可能瞄准该 bank 的请求里挑一个授权。标量请求、向量请求各自一组仲裁器（因为标量、向量是不同的物理 bank）。
- **`crossBar`（交叉开关）**：bank 读出的数据要送回「正确的收集槽的正确槽位」。crossbar 根据仲裁结果（是谁的请求赢了）把数据路由回去。

#### 4.3.2 核心流程

去程（请求 → bank）：

```
collectorUnit(0~7) 各自的 outArbiterIO[0..3]   (最多 32 个请求)
              │ 每个请求自带 bankID 与 rsType(标量/向量)
              ▼
   operandArbiter: 每个 bank 一个 RRArbiter
      ├── 标量仲裁器: 只接 rsType==1 且 bankID==i 的请求
      └── 向量仲裁器: 只接 rsType==2 且 bankID==i 的请求
              │ 输出授权哪一个请求 (readchosen) + 它的 rsAddr
              ▼
        scalarBank(i).rsidx / vectorBank(i).rsidx   (SyncReadMem 锁存, 下拍出数据)
```

回程（bank → 收集槽）：

```
bank 数据 (scalarBank(i).rs / vectorBank(i).rs)
              │ 配合 RegNext(readchosen)  ← 关键: 读延迟 1 拍, 所以选择信号也要延迟 1 拍对齐
              ▼
        crossBar: 把 (bank i 的数据) 路由到 (CUId, regOrder)
              ▼
collectorUnit(U).bankIn[0..3]   → 写进对应槽位, ready 置位
```

`readchosen` 是「这个 bank 这一拍授权了第几号请求」，由它解码出 `CUId = chosen >> 2`（哪个收集槽）和 `regOrder = chosen % 4`（该槽的第几个操作数）。

#### 4.3.3 源码精读

**仲裁器**——[operandCollector.scala:L329-L378](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L329-L378)。为每个 bank 各建一个标量 `RRArbiter`、一个向量 `RRArbiter`，输入数都是 `4*num_collectorUnit`（每个 CU 有 4 个槽）。授权条件在 [L362-L365](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L362-L365)：

```scala
bankArbiterScalar(i).io.in(j*4+k).valid := io.readArbiterIO(j)(k).valid &&
  (io.readArbiterIO(j)(k).bits.bankID === i.U) && (io.readArbiterIO(j)(k).bits.rsType === 1.U)
bankArbiterVector(i).io.in(j*4+k).valid := ... && (rsType === 2.U)
```

即「请求有效 ∧ 瞄准本 bank ∧ 类型匹配」才参与本仲裁器竞争。`RRArbiter` 保证各请求公平轮转，避免某个 warp 饿死。

**交叉开关**——[operandCollector.scala:L380-L435](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L380-L435)。解码授权号：

```scala
CUIdScalar(i) := io.chosenScalar(i) >> 2.U        // 哪个收集槽
regOrderScalar(i) := io.chosenScalar(i) % 4.U     // 该槽第几个操作数
```

然后三层 `for` 循环把 bank i 的数据连到 `io.out(CUId)(regOrder)`，标量读出的单个值广播成 `Vec(num_thread)`（[L417](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L417)），向量则整条传递（[L424](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L424)）。源码注释点明：「crossbar 到收集槽没有冲突，无需处理 stall；但 bank 冲突时某些 bank 输出无效」——因为同一 bank 一拍只能读一个，未被授权的请求要等。

**顶层把仲裁结果接给 bank，并把 `readchosen` 延迟一拍喂给 crossbar**——[operandCollector.scala:L542-L557](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L542-L557)：

```scala
vectorBank(i).rsidx := Arbiter.io.readArbiterOutVector(i).bits.rsAddr
...
crossBar.io.chosenScalar := RegNext(Arbiter.io.readchosenScalar)   // 延迟1拍对齐读延迟
crossBar.io.dataInScalar.rs(i) := scalarBank(i).rs
```

这组 `RegNext` 是理解时序的关键：地址在 T+1 拍送上 bank，数据在 T+2 拍才出，所以「这个地址是谁要的」也必须延迟到 T+2 拍再用来路由。

#### 4.3.4 代码实践（源码阅读型）

**目标**：定位「读延迟 1 拍」在代码里的两处对齐证据。

**步骤**：

1. 在 [regfile.scala:L20](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/regfile.scala#L20) 与 [L53](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/regfile.scala#L53) 确认 bank 用的是 `SyncReadMem`（同步读，1 拍延迟），不是 `Mem`（组合读）。
2. 在 [operandCollector.scala:L549-L552](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L549-L552) 找到 4 处 `RegNext(...readchosen.../...valid...)`，说明选择信号被刻意延迟一拍以匹配读延迟。

**需要观察的现象 / 预期结果**：能清楚说明「为什么 crossbar 用的是 `RegNext(readchosen)` 而不是 `readchosen`」——因为若不延迟，T+1 拍的选择信号会在 T+1 拍就去路由，但此时 bank 数据还没出来（要到 T+2），路由就会拿到过期数据。这是 RTL 时序对齐的典型手法，无需运行即可确认。

#### 4.3.5 小练习与答案

**练习 1**：标量请求和向量请求为什么要分别用两套仲裁器？
**答案**：标量 bank（`RegFileBank`）和向量 bank（`FloatRegFileBank`）是两组物理上独立的存储，读口也各是各的。一个标量请求只能去标量 bank、一个向量请求只能去向量 bank，所以每个 bank 号都要各配一个标量仲裁器和一个向量仲裁器，分别处理两类请求。

**练习 2**：如果两个收集槽同拍都要读 bank0 的标量寄存器，会发生什么？
**答案**：bank0 的标量 `RRArbiter` 只能授权其中一个，另一个本轮拿不到数据、`ready` 位不置位，留在 `s_add` 等下一拍重试。`RRArbiter` 的轮转保证两轮下来两个都能读到，不会饿死，代价是后到的那个收集多花一拍。

---

### 4.4 顶层 operandCollector：指令派发、写回与双发射组装

#### 4.4.1 概念说明

`operandCollector` 是把前三个模块装在一起的顶层。它对内管理「空闲收集槽分配」「bank 读写」「双发射仲裁」，对外暴露 4 组端口：

- **指令入口**：`controlV` / `controlX`——接 u4-l3 的 `ibuffer2issue` 双发射口（向量、标量各一路）。
- **写回入口**：`writeVecCtrl` / `writeScalarCtrl`——接写回单元（u5-l6），执行完的结果写回寄存器堆。
- **操作数出口**：`out(0)`（向量）/ `out(1)`（标量）——已收齐操作数的指令，送给执行单元。
- **warp 基址**：`sgpr_base` / `vgpr_base`——来自 CSR 文件，每个 warp 一份。

#### 4.4.2 核心流程：一条指令在顶层里的完整旅程

```
ibuffer2issue.out_v ──► instDemux ──► (选一个空闲 collectorUnit) ──► collectorUnit.control
ibuffer2issue.out_x ──► instDemux ──► (选一个空闲 collectorUnit) ──► collectorUnit.control
                                                                    │
              (collectorUnit 经 outArbiterIO→operandArbiter→bank→crossBar 收齐操作数)
                                                                    ▼
              collectorUnit.issue ──► DualIssueIO仲裁 ──► out(0)=向量 / out(1)=标量
                                                                    │
执行单元结果 ──► writeVecCtrl/writeScalarCtrl ──► 算出写回的 bank号+addr ──► 对应 bank 写口
```

四个细节：

1. **空闲槽分配 `instDemux`**：`controlV`、`controlX` 两路指令竞争空闲收集槽。`num_warp>1` 时用优先编码各选一个（保证标量、向量可同拍各进一个不同的槽）；`num_warp==1` 时因槽也只剩一个，额外用 `priorityXorV` 来回切换优先级，避免某一路被持续阻塞。
2. **读通路**：4.2 + 4.3 已详述。
3. **写回通路**：写回也用同一套交织公式算 bank 号与地址，只让命中那个 bank 的写口 `rdwen` 拉高；标量写还多一个 `reg_idxw.orR` 条件，使写 `x0` 被忽略（`x0` 恒零）。
4. **双发射出口 `DualIssueIO`**：多个收集槽的 `issue` 输出，先按 `inst_is_vec` 分流（标量进 `arb_x`、向量进 `arb_v`），再各用一个 `RRArbiter` 选一个，分别从 `out(1)`、`out(0)` 送出——于是标量、向量可以同拍各发射一条。

#### 4.4.3 源码精读

**顶层例化与连线**——[operandCollector.scala:L518-L578](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L518-L578)：例化 `num_collectorUnit` 个 `collectorUnit`、`operandArbiter`、各 `num_bank` 个 `vectorBank`/`scalarBank`、`crossBar`、`instDemux`。指令两路入口接 `instDemux.in(0)=controlV`、`in(1)=controlX`（[L570-L571](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L570-L571)），`instDemux` 把每条指令分给一个空闲槽（[L574-L577](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L574-L577)），并把 `sgpr_base`/`vgpr_base` 广播给所有槽。

**写回地址计算**——[operandCollector.scala:L603-L622](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L603-L622)，仍是交织公式：

```scala
wbVecBankId  := io.writeVecCtrl.bits.reg_idxw(log2Ceil(num_bank)-1,0) + io.writeVecCtrl.bits.warp_id(widSliceHigh,0)
wbVecBankAddr := (io.vgpr_base(warp_id) >> log2Ceil(num_bank).U) + (reg_idxw >> log2Ceil(num_bank).U)
...
vectorBank(wbVecBankId).rdwen := io.writeVecCtrl.bits.wvd & io.writeVecCtrl.valid
scalarBank(wbScaBankId).rdwen := io.writeScalarCtrl.bits.wxd & io.writeScalarCtrl.bits.valid & io.writeScalarCtrl.bits.reg_idxw.orR
```

注意标量写多了 `reg_idxw.orR`，即「目标为 `x0` 时写使能关闭」，落实 `x0` 恒零。所有 bank 的写数据/写地址都摆好，但只有命中 bank 的 `rdwen` 为真，等价于只写一个 bank。

**双发射仲裁 `DualIssueIO`**——[operandCollector.scala:L627-L671](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L627-L671)：`inst_is_vec` 判定一条指令走向量还是标量（`tc/fp/mul/sfu/mem` 或 `isvec` 为向量；`csr/barrier` 为标量），随后两个 `RRArbiter` 分别聚合并从 `out_v`/`out_x` 送出。

**在流水线里的对接**——[pipe.scala:L288-L291](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L288-L291) 把入口、写回接好：

```scala
operand_collector.io.controlV <> ibuffer2issue.io.out_v
operand_collector.io.controlX <> ibuffer2issue.io.out_x
operand_collector.io.writeVecCtrl   <> wb.io.out_v
operand_collector.io.writeScalarCtrl<> wb.io.out_x
```

基址来自 CSR 文件 [pipe.scala:L164-L165](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L164-L165)：`operand_collector.io.sgpr_base := csrfile.io.sgpr_base`。而 CSR 文件里，每个 warp 的基址在 warp 派发到达时由 CTA 调度器写入——[CSR.scala:L174-L177](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L174-L177) 与 [L300-L301](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L300-L301)：`sgpr_base_dispatch := io.CTA2csr.bits.CTAdata.dispatch2cu_sgpr_base_dispatch`。这就把 u3-l2/u3-l3 分配出的寄存器基址，接到了本讲的寻址上，形成完整闭环。

最后，操作数出口被组装进执行数据并叠加 SIMT 掩码——[pipe.scala:L362-L368](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L362-L368)：

```scala
exe_dataV.io.enq.bits.in1 := operand_collector.io.out(0).bits.alu_src1
...
exe_dataV.io.enq.bits.mask := operand_collector.io.out(0).bits.mask.zip(simt_stack.io.out_mask).map{ case (a,b) => a & b }
```

即向量指令的最终掩码 = 「收集槽给的掩码」AND「SIMT stack 的活动掩码」（u5-l5 详述）。这也印证了 4.1.3 里 v0 读口被关闭后，真正的逐 thread 掩码由 SIMT stack 提供。

#### 4.4.4 代码实践（源码阅读型·本讲主任务）

**目标**：把「ibuffer2issue 给出控制信号 → 收集槽读 bank → crossbar 回程 → 双发射出口」这条链路在源码里完整走通，并用一张时序图把每拍的数据与握手标清楚。

**步骤**：

1. **入口**：在 [pipe.scala:L288-L289](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L288-L289) 确认 `ibuffer2issue.out_v/out_x` 直连 `operandCollector.controlV/controlX`。
2. **派发到槽**：跟到 [operandCollector.scala:L570-L577](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L570-L577)，确认 `instDemux` 选一个 `ready` 的 `collectorUnit`，把 `CtrlSigs` 送进它的 `control`。
3. **请求 bank**：在 [L100-L127](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L100-L127) 看每个槽算出 `bankID` 与 `rsAddr`，经 `outArbiterIO` 发出。
4. **仲裁与读**：在 [L542-L547](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L542-L547) 看 `operandArbiter` 把 `rsAddr` 接到 bank 的 `rsidx`，`SyncReadMem` 下一拍出数据。
5. **回程路由**：在 [L549-L559](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L549-L559) 看 crossbar 用 `RegNext(readchosen)` 把数据送回 `collectorUnit.bankIn`，触发 [L265-L318](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L265-L318) 装配槽位、置 `ready`。
6. **发射出口**：收齐后 `collectorUnit.issue` 经 [L666-L671](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L666-L671) 的 `DualIssueIO` 从 `out(0)/out(1)` 送出，再到 [pipe.scala:L362-L368](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L362-L368) 组装成 `exe_dataV`。

**需要观察的现象 / 预期结果**：画出如下时序图（以 4.2.4 的 `vadd.vv v3,v1,v2` 为例）：

```
拍:        T          T+1            T+2              T+3
control:   fire ──► (锁存controlReg)
outArbiter:          op0→bank1 ──┐
                     op1→bank2 ──┤
bank.rsidx:          b1=v1,b2=v2 ◄┘ (SyncReadMem锁存)
bank.rs:                            v1,v2 出数据
crossbar:                          RegNext(chosen) 路由 ──► bankIn(0)=v1, bankIn(1)=v2
collectorUnit.ready:               ready(0/1):=1  → 全齐 → s_out
issue:                                             valid ──────────────► fire ──► out(0)
```

标注：`control` 用 `Decoupled`（valid/ready 握手）、`bankIn` 用 `Decoupled`、`issue` 用 `Decoupled`；bank 读是 `SyncReadMem` 1 拍延迟，故 crossbar 用 `RegNext` 对齐。预期可纯读码完成；上机可在仿真中打印 `collectorUnit.state`、各 `ready`、`issue.fire` 验证（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：写回时为什么所有 bank 的 `rd`/`rdidx` 都被赋了同样的值，只有命中 bank 的 `rdwen` 才拉高？
**答案**：因为写哪个 bank 是由 `wbVecBankId`/`wbScaBankId`（交织公式算出）决定的，只有那一个 bank 该写。把数据/地址广播给所有 bank、但只让命中 bank 的写使能有效，是 RTL 里常见的写法——省去一个多路选择器，等价于「只写命中 bank」。

**练习 2**：`sgpr_base` / `vgpr_base` 是什么时候、由谁写进 CSR 的？为什么必须每个 warp 一份？
**答案**：在 warp 被派发到 SM 时（`CTA2csr.valid`），CTA 调度器（u3 单元）为该 warp 分配的寄存器基址经 `dispatch2cu_sgpr_base_dispatch`/`..._vgpr_base_dispatch` 写入 CSR（[CSR.scala:L300-L301](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L300-L301)）。每个 warp 占用全局寄存器池里不同的一段窗口，基址不同，所以必须每 warp 一份，收集槽寻址时按 `wid` 取对应的基址。

---

## 5. 综合实践

**任务**：用本讲全部知识，解释「为什么把 `num_bank` 从 4 改成 2 会拖慢向量指令的收集」，并用源码与公式给出预判。

**步骤**：

1. **重算交织**：设 `num_bank=2`，warp 0，`vgpr_base(0)=0`。用公式算 `v1`、`v2`、`v3` 各自的 bank：bank = (0 + j低位) mod 2。`v1`(j=1)→bank1，`v2`(j=2, 低位0)→bank0，`v3`(j=3)→bank1。
2. **分析一条 `vfadd.vv v3, v1, v2`**：需要读 v1(bank1)、v2(bank0)——两个不同 bank，仍能并行；但要读 v1、v3（都 bank1）就冲突了。统计：bank 数减半后，任意两个寄存器落在同 bank 的概率从 1/4 升到 1/2。
3. **回看仲裁器**：在 [operandCollector.scala:L339-L346](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L339-L346) 确认 bank 数变少时，每个 `RRArbiter` 的输入不变（仍 `4*num_collectorUnit` 个），但能同拍授权的请求数减少（每 bank 仍只 1 个），冲突时更多请求要排队。
4. **给出结论**：`num_bank` 减小 → bank 冲突概率上升 → 收集槽停在 `s_add` 的平均拍数变多 → 操作数收集吞吐下降。反之增大 `num_bank` 能减少冲突但增加面积（更多 bank 实例）。这是面积与性能的典型折中。

**预期结果**：写出一段不少于 3 句的判断，包含「冲突概率变化」「对 `s_add` 停留拍数的影响」「面积代价」三点。若本地有仿真环境，可改 `parameters.scala` 的 `num_bank` 重新 `make verilog` + 仿真，对比同一个 vecadd 用例的周期数变化（待本地验证）。

## 6. 本讲小结

- 寄存器堆按 **bank 交织**组织：标量 `RegFileBank`（每条目 32 bit）、向量 `FloatRegFileBank`（每条目 `Vec(32, UInt(32.W))`），每 bank 1 读 1 写、带写前递 bypass；全 SM 共享，靠 `(warp 基址 + 寄存器号)` 寻址。
- bank 号 = `(wid 低位 + regIdx 低位) mod num_bank`，bank 内地址 = `(base + regIdx) / num_bank`；交织使连续寄存器天然分散到不同 bank，降低冲突。
- `collectorUnit` 是为单条指令预留的收集槽，三态 FSM（idle→add→out）把 op1/op2/op3/mask 四个槽从各 bank 收齐；立即数/PC/不需掩码的槽当场就绪，标量读出后广播到全部 lane。
- `operandArbiter` 用每 bank 一个 `RRArbiter` 公平授权读请求；`crossBar` 用 `RegNext(readchosen)` 匹配 `SyncReadMem` 的 1 拍读延迟，把数据路由回正确的收集槽与槽位。
- 顶层 `operandCollector` 还承担：`instDemux` 把双发射口的指令分给空闲槽；写回用同一交织公式算 bank/addr、只命中 bank 写有效（标量写 `x0` 被屏蔽）；`DualIssueIO` 把收齐的指令按标量/向量分流后从 `out(1)/out(0)` 双发射。
- warp 基址 `sgpr_base`/`vgpr_base` 来自 CSR，在 warp 派发时由 CTA 调度器写入，把 u3 的资源分配与本讲的寻址闭环；注意向量 v0 掩码读口在当前实现被关闭，真正逐 thread 掩码由 SIMT stack 在 `pipe.scala` 叠加。

## 7. 下一步学习建议

本讲把「操作数如何就位」讲完了，操作数一经 `out(0)/out(1)` 送出，下一步就进入**执行单元**。建议：

- **u5-l1（发射与执行单元总览）**：看 `Issue` 模块如何把 `exe_dataV`/`exe_dataX` 分发到 vALU/vFPU/LSU/SFU 等执行单元，本讲的 `out` 正是它的输入。
- **u5-l2（标量 ALU 与向量 ALU）**：看收集好的 `alu_src1/2/3` + `mask` 如何被并行 lane 使用，理解「标量广播、向量按 lane」的最终去向。
- **u5-l6（写回与 CSR）**：本讲的 `writeVecCtrl`/`writeScalarCtrl` 来自写回单元，可对照阅读，看执行结果如何回到这里写进 bank。

如果想再深入本讲相关源码，可继续读 [operandCollector.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala) 中 `unifiedBank`（标量/向量合并 bank 的备选设计）与 `instDemux` 在 `num_warp==1` 时的优先级翻转逻辑。
