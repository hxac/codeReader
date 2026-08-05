# 核心流水线各级 RTL

## 1. 本讲目标

本讲把上一讲（u7-l1）当作黑盒的 `VX_core` 打开，逐级拆解 Vortex 核心的 **6 级流水线** 在 RTL 里到底长什么样。读完本讲你应该能够：

- 在 `hw/rtl/core/` 中准确定位 Schedule / Fetch / Decode / Issue / Execute / Commit 六级各自对应的 `.sv` 模块；
- 看懂 `VX_core.sv` 如何用一组 `*_if` 接口把这六级“穿成一根管子”，并理解级间数据通路的形状；
- 说出 Issue 级内部 IBuffer / Scoreboard / Operands / Dispatcher 四个小模块的分工，以及 Execute 级如何用 `fu_type` 把指令路由到 ALU/LSU/FPU/SFU/TCU；
- 建立一条贯穿全讲的心智主线：**RTL 的每一级都与 SimX 的同名模块一一对应**，这正是 model_parity 的物理基础。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**第一个直觉：什么是“级（stage）”。** 一条指令在 Vortex 核心里不是一步算完的，而是像流水线一样，从 Schedule 一路走到 Commit，每一级在一个时钟周期内做完自己的事，再把半成品交给下一级。Vortex 官方文档把这根管子定义为 6 级：

> Schedule → Fetch → Decode → Issue → Execute → Commit

其中 Schedule 由 Warp Scheduler + IPDOM Stack + Inflight Tracker 组成；Issue 内部又细分为 IBuffer、Scoreboard、Operands Collector；Execute 内部按运算类型分成 ALU / FPU / LSU / SFU / TCU 五类功能单元。这条定义来自 [docs/designs/microarchitecture.md:38-76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L38-L76)，本讲的 RTL 模块就是它的硬件实现。

**第二个直觉：什么是 `*_if` 接口。** Vortex 用 SystemVerilog 的 `modport` 接口（如 `VX_fetch_if`、`VX_decode_if`）来表示级与级之间的连线。一个接口里通常包含 `valid` / `ready`（握手）和 `data`（载荷）。你可以把每个 `*_if` 想成 SimX 里那条带背压的 `SimChannel`（见 u5-l1）：上一级 `valid` 拉高、下一级 `ready` 拉高，这一拍数据才“流过去”。理解了这一点，`VX_core.sv` 里那一大堆接口声明就不再是天书，而是一张流水线接线图。

> 本讲假定你已经读过 u7-l1（Vortex 顶层与 socket/cluster RTL）和 u6 系列（SimX 角度的同一套流水线）。我们会反复拿 SimX 当“镜像”来对照。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `hw/rtl/core/` 下：

| 文件 | 对应流水级 | 作用 |
| --- | --- | --- |
| `VX_core.sv` | 总装 | 把六级模块实例化、用 `*_if` 接口串起来，并接出 icache/dcache/KMU 等外部总线 |
| `VX_fetch.sv` | Fetch | 用 PC 向 icache 发请求，回收指令字；RVC 压缩指令时挂上 `VX_decompressor` |
| `VX_decode.sv` | Decode | 把 32 位指令字译码成 `decode_t` 结构，并向调度器反馈控制指令 |
| `VX_ibuffer.sv` | Issue（子模块） | 每 warp 一个的指令缓冲队列 |
| `VX_scoreboard.sv` | Issue（子模块） | 寄存器冒险检测，决定指令能否发射 |
| `VX_operands.sv` / `VX_dispatcher.sv` | Issue（子模块） | 读操作数 / 按功能单元分发 |
| `VX_issue.sv` / `VX_issue_slice.sv` | Issue | 上面四个子模块的容器，按 `ISSUE_WIDTH` 切片 |
| `VX_execute.sv` | Execute | 按 `fu_type` 实例化 ALU/LSU/FPU/TCU/SFU |
| `VX_commit.sv` | Commit | 多功能单元结果仲裁、写回、通知调度器 |

此外会引用 `VX_gpu_pkg.sv`（功能单元编号定义）和 `VX_alu_unit.sv`（作为功能单元内部结构的样本）。

## 4. 核心概念与源码讲解

### 4.1 VX_core.sv：六级流水线的总装车间

#### 4.1.1 概念说明

`VX_core.sv` 本身不做任何运算，它是一个“总装车间”：把六级流水线模块当成零件实例化，再用一堆 `*_if` 接口把它们首尾相连。理解了这一张接线图，你就掌握了整条流水线的形状。它还负责把核心对外暴露的总线（icache/dcache/KMU/各类加速器）接到正确的级上。

#### 4.1.2 核心流程

`VX_core` 内部的数据流可以这样描述（伪代码）：

```
KMU/调度输入 → VX_scheduler ──schedule_if──▶ VX_fetch
                                              │
                                            fetch_if
                                              ▼
                                           VX_decode ──decode_sched_if──▶ (回 scheduler)
                                              │
                                           decode_if
                                              ▼
                          VX_issue ──── dispatch_if[EX × ISSUE_WIDTH] ───▶ VX_execute
                                                                     │
                                          commit_if[EX × ISSUE_WIDTH]
                                              ▼
                                           VX_commit ──writeback_if──▶ (回 issue 的寄存器堆)
                                                     └─commit_sched_if──▶ (回 scheduler)
```

两个关键形状要记住：

1. **Execute 与 Issue/Commit 之间的接口是二维数组**：`dispatch_if[NUM_EX_UNITS * ISSUE_WIDTH]`。这是因为指令既按“发射通道（issue slot）”区分，又按“功能单元（EX_ALU/EX_LSU/…）”区分。
2. **存在多条反馈回路**：`decode_sched_if`、`issue_sched_if`、`commit_sched_if` 都从后级回流到 Scheduler，让调度器知道“这条 warp 已经译码/发射/退休了”，从而管理 warp 状态机。

#### 4.1.3 源码精读

接口声明区先把六级的内部连线一次性摆出来，注意 `schedule_if` / `fetch_if` / `decode_if` 三个串联接口，以及二维的 `dispatch_if` / `commit_if`：

[VX_core.sv:74-89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L74-L89) — 声明 schedule→fetch→decode 的级间接口，以及 `dispatch_if` / `commit_if` 两个 `NUM_EX_UNITS * ISSUE_WIDTH` 维度的二维数组、`writeback_if`。这就是流水线的“骨架”。

接着是六个模块的实例化，按流水线顺序自上而下排列，非常直观：

[VX_core.sv:272-304](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L272-L304) — 实例化 `VX_scheduler`（Schedule 级），它的 `kmu_bus_if` 吃 CTA 输入，`schedule_if` 输出给 fetch。

[VX_core.sv:306-318](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L306-L318) — 实例化 `VX_fetch`（Fetch 级），接 `schedule_if`、出 `fetch_if`，并向 icache 发请求。

[VX_core.sv:335-343](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L335-L343) — 实例化 `VX_decode`（Decode 级）。

[VX_core.sv:345-361](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L345-L361) — 实例化 `VX_issue`（Issue 级），输入 `decode_if` 与回流的 `writeback_if`，输出 `dispatch_if`。

[VX_core.sv:363-418](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L363-L418) — 实例化 `VX_execute`（Execute 级），它把 `dispatch_if` 转成 `commit_if`，并把各类加速器总线（DXA/TEX/OM/RASTER/RTU）接到对应子单元。

[VX_core.sv:420-431](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L420-L431) — 实例化 `VX_commit`（Commit 级），出 `writeback_if` 和 `commit_sched_if`。

最后，`busy` 信号把所有级的“忙”状态 OR 起来，告诉上层（socket/cluster）这个 core 是否还有未完成的工作：

[VX_core.sv:577-580](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_core.sv#L577-L580) — `busy = sched_busy || dcr_busy || ~(&lsu_sched_empty) || ~mem_unit_empty`（开图形扩展时再加两项）。注意访存路径也参与 busy 判定，确保 flush/退休前所有 store 都落地。

#### 4.1.4 代码实践

**目标**：建立流水线骨架的直观印象。

1. 打开 `hw/rtl/core/VX_core.sv`，定位 §4.1.3 列出的六个实例化块。
2. 在一张纸上画出 §4.1.2 的伪代码图，把每个 `*_if` 接口标注到对应的实例化端口上。
3. 找到 `dispatch_if` 的声明（第 87 行）和它在 `VX_issue`（`.dispatch_if`）与 `VX_execute`（`.dispatch_if`）两侧的连接，确认它是同一根线两头接。

**需要观察的现象**：六个实例化块的代码顺序与流水线级别顺序完全一致（scheduler→fetch→decode→issue→execute→commit），这种“阅读顺序即数据流顺序”的写法是 Vortex RTL 的一贯风格。

**预期结果**：你能用一句话说清“哪一级的哪个端口连到哪一级的哪个端口”。

#### 4.1.5 小练习与答案

**练习 1**：`VX_core` 里为什么需要 `writeback_if` 这条从 Commit 回流到 Issue 的线？
**答案**：Issue 级的 Operands 要从寄存器堆读源操作数，而寄存器堆的最新值是 Commit 级写回的；同时 Scoreboard 也需要知道哪个目的寄存器已被释放。所以写回结果必须回流到 Issue。

**练习 2**：`busy` 信号里为什么除了 `sched_busy` 还要 OR 上 `~mem_unit_empty`？
**答案**：即使调度器已经没有在跑的 warp，仍可能有 store 还没真正写到 dcache/内存。只有访存路径也空了，core 才算真正空闲，才能安全地对外宣告 `busy=0`（例如让上层做 cache flush）。

---

### 4.2 Fetch 级：从 PC 到指令字

#### 4.2.1 概念说明

Fetch 级的任务很纯粹：调度器给它一个 `(PC, wid, tmask, cta_id)`，它就用这个 PC 去 icache 取一个指令字回来，交给 Decode。难点有两个：一是 icache 的响应是异步的（可能 miss），需要用 tag 把“请求”和“响应”配对；二是 RISC-V 压缩指令（RVC）允许 2 字节对齐的 16 位指令，这让“取一个字”变得不那么简单。

#### 4.2.2 核心流程

```
schedule_if(PG) ──▶ 计算 icache 地址（字对齐）──▶ icache 请求 elastic_buffer ──▶ icache
                                                                          │
                                                                  tag_store 记下 (PC,tmask,cta_id)
                                                                          ▼
icache 响应 ──▶ 用 tag 取回 (PC,tmask,cta_id) ──▶ [RVC? decompressor : 直通] ──▶ fetch_if
```

- **tag_store** 是一个双口 RAM，按 `wid` 索引，发请求时写入 `(PC, tmask, cta_id)`，收响应时按同一个 `wid` 读回——这样异步响应就能找回它对应的 PC 和线程掩码。
- **RVC 分支**：若开启了压缩指令扩展，插入一个 `VX_decompressor`，负责处理“半字对齐”和“跨字补取”；不开 RVC 时走直通路径。

#### 4.2.3 源码精读

tag_store 的写入与读回——注意它按 `wid` 索引、在请求 fire 时写、响应时读：

[VX_fetch.sv:63-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_fetch.sv#L63-L78) — 用 `VX_dp_ram` 做 tag 存储，`waddr=req_tag(wid)`、`raddr=rsp_tag(wid)`，把请求时刻的 PC/tmask/cta_id 在响应时刻还原。

RVC 路径里“一个 warp 同时只能有一个在途 icache 请求”的不变量（注释里称 Invariant B），否则同一个 warp 的两个响应会撞同一个 tag_store 槽：

[VX_fetch.sv:102-118](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_fetch.sv#L102-L118) — `inflight` 位图：请求 fire 置位、响应 fire 清位，保证每 warp 至多一个在途请求。

不开 RVC 时的直通路径，逻辑极简——直接把 PC 翻译成 icache 地址、把响应数据接成 `fetch_if`：

[VX_fetch.sv:187-208](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_fetch.sv#L187-L208) — `assign fetch_if.data.instr = icache_bus_if.rsp_data.data;` 等，是理解 Fetch 最小形态的好入口。

最后所有路径共用一个请求侧 elastic buffer，把核心内部的请求寄存到 icache 边界：

[VX_fetch.sv:214-227](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_fetch.sv#L214-L227) — `VX_elastic_buffer` 做 icache 请求的弹性缓冲，`OUT_REG(1)` 表示对外总线寄存输出。

#### 4.2.4 代码实践

**目标**：理解“请求—响应配对”机制。

1. 阅读 [VX_fetch.sv:63-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_fetch.sv#L63-L78)，确认 `req_tag` 和 `rsp_tag` 都来自 `wid`。
2. 设想 icache miss 了 100 拍：请求在第 0 拍写入 tag_store，响应在第 100 拍读回——中间这 100 拍里，这个 warp 不能再发第二个 icache 请求（见 `inflight`）。
3. 思考：为什么 tag_store 用 `wid` 而不是用请求序号做索引？

**预期结果**：你能解释“icache 响应是异步的，Fetch 靠 wid 索引的 tag_store 把响应拼回它原来的 PC/tmask”。

**待本地验证**：若你在仿真环境跑 `--debug`，可在 trace 里看到同一 wid 的 `req` 和 `rsp` 相隔若干周期，且 PC 一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 RVC 路径要维护“每 warp 至多一个在途请求”？
**答案**：tag_store 每 warp 只有一个槽。若同一 warp 同时有两个在途请求，先回来的响应会读走后一个请求写入的 PC/tmask，造成配对错乱。

**练习 2**：直通路径（无 RVC）里 `schedule_if.ready` 何时为真？
**答案**：见 [VX_fetch.sv:198](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_fetch.sv#L198)，`assign schedule_if.ready = icache_req_ready;`——icache 接受请求的同一拍，调度器就被认为“送走了”这个 PC。

---

### 4.3 Decode 级：指令字 → 译码结构 + 反馈调度器

#### 4.3.1 概念说明

Decode 拿到 32 位原始指令字，把它翻译成后级好用的 `decode_t` 结构：属于哪个功能单元（`ex_type`）、具体什么操作（`op_type`）、用到哪些寄存器（`rs1/rs2/rs3/rd`）、是否写回（`wb`）、字节选择（`bytesel`）等。它还承担一个重要副作用：**遇到控制指令（如 `is_wstall`）时，通过 `decode_sched_if` 反馈给调度器**，让调度器暂停在该 warp 的取指上——这正对应 microarchitecture 文档里“Decode: Notify warp scheduler on control instructions”。

#### 4.3.2 核心流程

```
fetch_if(instr 32b) ──▶ 抽取 opcode/funct3/funct7/rd/rs1/rs2 ──▶ 大 case 块填 ex_type/op_type/op_args
                                                                        │
                                                     组装 use_regs/reg_ids/wb/bytesel
                                                                        ▼
                                                     elastic_buffer ──▶ decode_if(decode_t)
                                                                        │
                                                 fetch_fire 时锁存 ──▶ decode_sched_if ──▶ scheduler
```

#### 4.3.3 源码精读

指令字段抽取——这是所有译码逻辑的原料：

[VX_decode.sv:50-61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_decode.sv#L50-L61) — 从 `instr` 里切出 `opcode/funct3/funct7/rd/rs1/rs2/rs3`。

译码产物的寄存器声明，对应 `decode_t` 的各个字段：

[VX_decode.sv:40-48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_decode.sv#L40-L48) — `ex_type`、`op_type`、`op_args`、`reg_ids`、`use_regs`、`rd_xregs/wr_xregs`、`bytesel`、`is_wstall`。后面成百行的 `case` 就是给这些寄存器赋值。

译码结果经一个 SIZE=0 的 elastic_buffer 输出（SIZE=0 表示纯组合直通 + 握手，不做深度缓冲），同时把字段拼装成 `decode_if.data`：

[VX_decode.sv:861-873](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_decode.sv#L861-L873) — 输出 elastic_buffer，把译码字段打包成 `decode_if.data`。

反馈调度器的逻辑——`is_wstall` 决定是否“解锁”调度器继续取下一条：

[VX_decode.sv:879-898](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_decode.sv#L879-L898) — `decode_sched_unlock_r <= ~is_wstall;`，即控制类指令在译码时会通过 `decode_sched_if` 通知调度器暂停（不解锁），这与 u6-l2 讲过的 `is_wstall` 标志一致。

#### 4.3.4 代码实践

**目标**：验证“Decode 反馈调度器”这条回路。

1. 打开 [VX_decode.sv:879-898](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_decode.sv#L879-L898)。
2. 全文搜索 `is_wstall`，看哪些指令会把它置 1（例如宏指令、RTU 的 TRACE2，见 [VX_decode.sv:830](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_decode.sv#L830)）。
3. 解释：为什么这些指令必须在译码时就停住取指？

**预期结果**：你发现 `is_wstall` 指令大多是“不能让后级指令越过自己”的同步/展开类指令，所以译码阶段就要卡住前端，与 u6-l2 sequencer 的“全部展开完毕才 resume 取指”是同一个约束的 RTL 侧实现。

#### 4.3.5 小练习与答案

**练习**：Decode 的输出 elastic_buffer 用了 `SIZE(0)`（[VX_decode.sv:863](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_decode.sv#L863)），这意味着什么？
**答案**：SIZE=0 表示不缓存数据、只做 valid/ready 握手传递。Decode 本身是纯组合译码，不需要内部排队；深度缓冲交给下游的 IBuffer 承担。

---

### 4.4 Issue 级：IBuffer / Scoreboard / Operands / Dispatcher

#### 4.4.1 概念说明

Issue 是流水线里最“厚”的一级，内部其实有四个小模块接力：

- **IBuffer**：每 warp 一个的 FIFO，缓存已译码指令，解耦“译码速率”和“发射速率”。
- **Scoreboard**：记录每个寄存器是否“在使用中”，据此判断队头指令能否发射（冒险检测）。
- **Operands**：指令获准发射后，从寄存器堆读出源操作数。
- **Dispatcher**：按指令的 `ex_type` 把“指令 + 操作数”分发到对应功能单元的入口队列。

Vortex 还把 Issue 按 `ISSUE_WIDTH` 切成多个并行的 `VX_issue_slice`，每个 slice 服务一组 warp，从而支持每周期发射多条指令。

#### 4.4.2 核心流程

```
decode_if ──▶ VX_ibuffer(每 warp 一个队列) ──▶ VX_scoreboard(队头能否发射?)
                                                    │ 否 → 停在 staging buffer
                                                    │ 是 ▼
                              VX_operands(读 rs1/rs2/rs3) ──▶ VX_dispatcher
                                                                    │
                              按 ex_type 路由到 dispatch_if[EX] ──▶ Execute
```

指令要顺利发射，必须同时通过三道关（对应 Scoreboard 里的三条件，详见源码精读）：
1. **数据就绪**：源寄存器和特殊寄存器都没被在途指令占用（`data_ready`）；
2. **功能单元没满**：目标 FU 的入口队列没溢出（`~fu_goingfull`）；
3. **FU 锁未冲突**：该 FU 没被另一条带 `fu_lock` 的指令独占（`~(fu_locked && fu_lock_sel)`）。

#### 4.4.3 源码精读

`VX_issue` 把工作切成 `ISSUE_WIDTH` 个 slice，并把二维 dispatch 接口做“转置”：

[VX_issue.sv:56-90](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_issue.sv#L56-L90) — `for (issue_id ...) VX_issue_slice`；末尾的转置循环 `dispatch_if[ex_id*ISSUE_WIDTH + issue_id]` 把“按 issue 切”的数据重排成“按 EX 单元”的视图，供 Execute 消费。

每个 slice 内部就是四个子模块的接力：

[VX_issue_slice.sv:41-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_issue_slice.sv#L41-L96) — 依次实例化 `VX_ibuffer`、`VX_scoreboard`、`VX_operands`、`VX_dispatcher`，用 `ibuffer_if` / `scoreboard_if` / `operands_if` 串联。

IBuffer 是每 warp 一个的 elastic buffer，并且会在出口接一个 `VX_uop_sequencer` 做宏指令展开：

[VX_ibuffer.sv:42-93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ibuffer.sv#L42-L93) — 每 warp 一个 `VX_elastic_buffer(SIZE=IBUF_SIZE)`；当 `UOP_MAX>0` 时再串 `VX_uop_sequencer`，把一条宏指令（如 WGMMA）裂成多条微操作。这正是 u6-l2 讲的“sequencer 微操作展开”的 RTL 落点。

Scoreboard 的三条件发射门控（本级的“心脏”）：

[VX_scoreboard.sv:187-190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L187-L190) — `operands_ready_n = data_ready && ~fu_goingfull[ex_sel] && ~(fu_locked_n[ex_sel] && fu_lock_sel);`。这三项就是 §4.4.2 列的三道关，对应 SimX 里“记分板冒险 + FU lock + 派发信用”三连（u6-l3）。

Scoreboard 用 `inuse_regs` 位图跟踪寄存器占用，发射时置位 rd、写回时清位：

[VX_scoreboard.sv:137-157](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L137-L157) — `wb_inuse_regs` 在 writeback 时释放 rd；`inuse_regs_n` 在 staging_fire 时占住 rd，构成“占用—释放”的状态机。

Dispatcher 按 `ex_type` 把指令送进对应 FU 的入口队列，并向 Scoreboard 回送 `fu_release` 信用：

[VX_dispatcher.sv:43-82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_dispatcher.sv#L43-L82) — `fu_release[i] = dispatch_if[i].valid && dispatch_if[i].ready;` 即 FU 接收一条指令就归还一个信用，这对应 SimX 的 `fu_credits`（u6-l3）。

#### 4.4.4 代码实践

**目标**：把 Issue 四子模块的数据流走一遍。

1. 从 [VX_issue_slice.sv:41-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_issue_slice.sv#L41-L96) 出发，画出 `decode_if → ibuffer → scoreboard → operands → dispatcher → dispatch_if` 的链路。
2. 在 [VX_scoreboard.sv:187-190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L187-L190) 处，设想一条 `add rd, rs1, rs2`，问：如果 `rs1` 正被一条在途的 `load` 占用，`data_ready` 会是什么？
3. 在 [VX_dispatcher.sv:43-45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_dispatcher.sv#L43-L45) 处确认：每条被 FU 接收的指令都会产生一个 `fu_release`。

**预期结果**：`data_ready` 为 0，指令停在 staging buffer 不能发射，直到那条 load 写回释放 `rs1`。这与你将在 u6-l3 SimX 侧看到的“位掩码冒险检测”是同一套语义。

**待本地验证**：若开启 `--debug` 并打印 stall trace，可在 [VX_scoreboard.sv:214-216](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L214-L216) 的 `TRACE` 里看到 `opds_busy` 位被置起的周期。

#### 4.4.5 小练习与答案

**练习 1**：为什么 IBuffer 要“每 warp 一个”而不是全核心一个共享队列？
**答案**：不同 warp 的指令进度互不相同，且 Scoreboard、Operands 都是按 warp 维护状态的（每 warp 一组 `inuse_regs`）。每 warp 独立队列保证一个 warp 的 stall 不会堵住其他 warp，这是 SIMT 时间复用隐藏延迟的关键。

**练习 2**：Dispatcher 的 `fu_release` 和 Scoreboard 的 `fu_goingfull`/`fu_locked` 是什么关系？
**答案**：`fu_release` 是 FU 归还的“我还能再吃一条”信用；`fu_goingfull` 表示 FU 入口队列即将满；`fu_locked` 表示 FU 被带 `fu_lock` 的指令独占。三者共同决定一条新指令能否进入该 FU。

---

### 4.5 Execute 级：按 fu_type 路由的功能单元

#### 4.5.1 概念说明

Execute 级不再是一个大模块，而是 **一组并列的功能单元**。指令按译码时确定的 `ex_type`（即 `fu_type`）被路由到对应单元：ALU 算整数/分支、LSU 算访存、FPU 算浮点、SFU 算 warp 控制/CSR/各类加速器、TCU 算矩阵乘加。`VX_execute.sv` 就是这组单元的容器，本身几乎不含逻辑，只做实例化和接口扇出——这与 SimX 里 `func_units_[(int)fu_type]` 数组（u6-l4）是同一思路。

功能单元编号定义在 `VX_gpu_pkg.sv`，是一段“按使能位累加”的枚举：

[VX_gpu_pkg.sv:229-235](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L229-L235) — `EX_ALU=0, EX_LSU=1, EX_SFU=2`，而 `EX_FPU`/`EX_TCU` 由 `VX_CFG_EXT_F_ENABLED`/`VX_CFG_EXT_TCU_ENABLED` 决定是否存在，`NUM_EX_UNITS = EX_TCU+1`。这正是 SimX 里 `FUType` 既是路由键又作数组下标（u6-l4）的 RTL 对应。

#### 4.5.2 核心流程

```
dispatch_if[EX × ISSUE_WIDTH] ──▶ VX_execute
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
   VX_alu_unit                VX_lsu_unit                   VX_sfu_unit
   (整数/分支)                 (load/store)                 (warp 控制/CSR/加速器分派)
        │                            │                            │
        └─────────────▶ commit_if[EX × ISSUE_WIDTH] ◀──────────────┘
```

每个功能单元内部还有统一的“分—算—聚”三段结构（以 ALU 为例）：`lane_dispatch` 把发射通道散到各物理 block → 每 block 内 `pe_switch` 选 PE（整型/乘除）→ `alu_int`/`muldiv` 真正计算 → `lane_gather` 把结果聚回 commit 接口。

#### 4.5.3 源码精读

`VX_execute` 容器——五个功能单元的实例化，注意它们各自只接自己那一段 `dispatch_if`/`commit_if`：

[VX_execute.sv:91-99](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv#L91-L99) — 实例化 `VX_alu_unit`，接 `dispatch_if[EX_ALU * ISSUE_WIDTH +: ISSUE_WIDTH]`。

[VX_execute.sv:103-113](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv#L103-L113) — 实例化 `VX_lsu_unit`，并暴露 LSU client 接口给 `VX_core` 里的 `VX_lsu_scheduler`。

[VX_execute.sv:116-125](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv#L116-L125) — `VX_fpu_unit`（仅在 `VX_CFG_EXT_F_ENABLE` 时存在）。

[VX_execute.sv:128-145](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv#L128-L145) — `VX_tcu_unit`（张量核，仅 WGMMA 使能时）。

[VX_execute.sv:147-182](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv#L147-L182) — `VX_sfu_unit`。注意它接了大量加速器总线（DXA/TEX/OM/RASTER/RTU），印证了 u6-l4 的结论：**SFU 是一个分派器**，把指令再路由到 WCTL/CSR/DXA/TEX/OM/RASTER/RTU 等子单元。

ALU 单元内部的“分—算—聚”三段，作为功能单元内部结构的样本：

[VX_alu_unit.sv:47-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_alu_unit.sv#L47-L56) — `VX_lane_dispatch`：把 `ISSUE_WIDTH` 个发射通道散到 `NUM_ALU_BLOCKS` 个物理 block。

[VX_alu_unit.sv:75-101](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_alu_unit.sv#L75-L101) — `VX_pe_switch` 按操作类型选 PE（整型 vs 乘除），再接 `VX_alu_int`（必要时还有 `VX_alu_muldiv`）。

[VX_alu_unit.sv:116-125](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_alu_unit.sv#L116-L125) — `VX_lane_gather`：把各 block 的结果聚回 `commit_if`。这套 lane_dispatch/pe_switch/lane_gather 正是 SimX `FuncUnit<NUM_BLOCKS>` 的 Inputs/Outputs channel 模型（u6-l4）的 RTL 镜像。

#### 4.5.4 代码实践

**目标**：理解“容器 + 子单元”的两层结构。

1. 打开 [VX_execute.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv)，确认它除了实例化没有任何运算逻辑。
2. 选 ALU 作为代表，对照 [VX_alu_unit.sv:47-125](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_alu_unit.sv#L47-L125)，标注出 lane_dispatch → pe_switch → alu_int → lane_gather 的四步。
3. 思考：为什么 SFU 一个单元要接这么多加速器总线，而 ALU 一个都不接？

**预期结果**：你发现 ALU 是“纯计算”单元（输入操作数、输出结果），而 SFU 是“分派器”单元（要把指令再转给真正的加速器硬件），这与 u6-l4 的结论一致。

#### 4.5.5 小练习与答案

**练习 1**：`EX_FPU` 和 `EX_TCU` 的编号为什么不是固定的常数？
**答案**：浮点扩展和 TCU 扩展是可选的（由 `VX_CFG_EXT_F_ENABLE`/`VX_CFG_EXT_TCU_ENABLE` 控制，见 [VX_gpu_pkg.sv:232-233](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L232-L233)）。不开时这两个单元不存在，后面的编号要前移，所以用“使能位累加”来定义，保证 `dispatch_if`/`commit_if` 数组始终紧凑无空洞。

**练习 2**：ALU 里的 `pe_select`（[VX_alu_unit.sv:68-73](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_alu_unit.sv#L68-L73)）依据什么选 PE？
**答案**：依据指令的操作类型 `alu.xtype == ALU_TYPE_MULDIV`——乘除法走 muldiv PE，其他整数运算走 int PE。

---

### 4.6 Commit 级：仲裁、写回、通知调度器

#### 4.6.1 概念说明

Execute 的五个功能单元算完后，结果都涌向 Commit。Commit 要做三件事：① 把多个 FU 可能同时到达的结果**仲裁**成按 issue 通道的单一序列；② 把结果**写回**寄存器堆（并交给 Issue 的 Operands/Scoreboard）；③ 通知**调度器**“某条 warp 的指令退休了”，让调度器更新在途指令计数和 warp 状态。

#### 4.6.2 核心流程

```
commit_if[EX × ISSUE_WIDTH] ──▶ 每 issue 通道一个 VX_stream_arb(优先级 P)
                                       │
                              commit_arb_if[ISSUE_WIDTH]
                                       │
              ┌────────────────────────┴────────────────────────┐
              ▼                                                 ▼
        writeback_if[ISSUE_WIDTH]                      committed_warps mask
        (写回寄存器堆)                                  (经 commit_sched_if 回 scheduler)
```

注意 `commit_arb` 用了优先级仲裁（`ARBITER="P"`），这与 u7-l1 提到的“卡流水线的路径用优先级 P”一致——保证被选中的结果可预测，利于与 SimX 做 cycle 级 model_parity。

#### 4.6.3 源码精读

每 issue 通道一个优先级仲裁器，把 `NUM_EX_UNITS` 个 FU 的结果归并：

[VX_commit.sv:37-68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_commit.sv#L37-L68) — `VX_stream_arb(ARBITER="P")`，输入是 `NUM_EX_UNITS` 个 FU 的 commit，输出单一 `commit_arb_if[i]`；`committed_warps[i] = fire && data.eop`，即只有一条指令的最后一个微操作（`eop`）才记为“该 warp 退休一条”。

把各 issue 通道退休的 wid 汇成 per-warp 掩码，反馈给调度器：

[VX_commit.sv:71-87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_commit.sv#L71-L87) — `committed_warp_mask` 按 `committed_slot_wid` 置位，寄存一拍后经 `commit_sched_if.committed_warps` 回送调度器。这条线对应 microarchitecture 文档里 Commit 级“update the Scoreboard”之外的“通知调度器退休”。

写回数据组装——按 `bytesel` 做字节对齐与 tmask 门控：

[VX_commit.sv:91-126](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_commit.sv#L91-L126) — 计算 `size_mask`、`base_byteen`，对每个 SIMD lane 按 `tmask[lane]` 决定是否写（`writeback_byteen[lane] = tmask[lane] ? base_byteen : '0`）。这正是 SIMT“用 tmask 控制哪些线程真正写回”的硬件实现，与 u6 系列反复强调的“用发射快照 tmask 门控写回”一致。

#### 4.6.4 代码实践

**目标**：理清 Commit 的“仲裁—写回—通知”三件事。

1. 在 [VX_commit.sv:49-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_commit.sv#L49-L64) 找到优先级仲裁器，确认它把 `NUM_EX_UNITS` 路输入归并成一路。
2. 在 [VX_commit.sv:106-109](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_commit.sv#L106-L109) 确认写回字节掩码由 `tmask[lane]` 门控。
3. 追问：一条被 `eop` 标记的微操作退休时，调度器收到的是什么？

**预期结果**：调度器收到的 `commit_sched_if.committed_warps` 是一个 per-warp 位图，对应 warp 的退休计数会被推进，从而可能让该 warp 重新进入可调度状态。

#### 4.6.5 小练习与答案

**练习 1**：为什么用 `data.eop` 而不是每个微操作都记一次“退休”？
**答案**：一条宏指令（如 WGMMA）会被展开成多个微操作，只有最后一个（`eop`）才代表“整条指令完成”。用 `eop` 计数才能保证调度器的在途指令计数与架构语义一致。

**练习 2**：写回时为什么要 `writeback_byteen[lane] = tmask[lane] ? base_byteen : '0`？
**答案**：分支发散后，warp 内只有部分线程（tmask 为 1）应该真正写回结果。被 mask 掉的线程字节掩码清零，避免污染寄存器堆。

---

## 5. 综合实践

本讲的贯穿任务是**填写一张「流水级 → RTL 模块 → SimX 模块」对照表**，把本讲所有最小模块串起来。

**操作步骤**：

1. 先复习 [docs/designs/microarchitecture.md:38-76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L38-L76) 的 6 级定义。
2. 对照本讲给出的所有永久链接，在 RTL 里逐一确认每个模块。
3. 回忆 u6 系列（SimX）讲过的同名模块，填入下表（下表已给出 RTL 侧，SimX 侧请你自己补全）：

| 流水级 | RTL 模块（本讲） | 关键子模块 / 说明 | 对应 SimX 模块（待你填写） |
| --- | --- | --- | --- |
| Schedule | `VX_scheduler.sv` | Warp Scheduler + IPDOM Stack + Inflight Tracker | scheduler.cpp / barrier_unit.cpp 等（u6-l1） |
| Fetch | `VX_fetch.sv` | tag_store 配对、RVC 接 `VX_decompressor` | decompressor.cpp / decode.cpp 前端（u6-l2） |
| Decode | `VX_decode.sv` | 译码成 `decode_t`、`is_wstall` 反馈调度器 | decode.cpp（u6-l2） |
| Issue | `VX_issue.sv` + `VX_issue_slice.sv` | IBuffer + Scoreboard + Operands + Dispatcher | scoreboard.cpp / opc_unit.cpp / operands.cpp / dispatcher.cpp（u6-l3） |
| Execute | `VX_execute.sv` | ALU/LSU/FPU/SFU/TCU 容器 | alu_unit.cpp / lsu_unit.cpp / fpu_unit.cpp / sfu_unit.cpp（u6-l4） |
| Commit | `VX_commit.sv` | 优先级仲裁 + 写回 + 通知调度器 | （写回逻辑分布在 opc_unit/各 unit 的 commit 通道） |

4. **自检题**：挑表中任意两行，用自己的话说清“RTL 这一级行为”和“SimX 同名模块行为”为什么必须一致。

**预期结果**：你能不查资料地完整说出 6 级各自的 RTL 入口文件，并指出每级的 SimX 镜像——这恰恰是 u7-l4 要讲的 model_parity 的前置基础。

> 说明：本实践为源码阅读型实践，无需运行仿真；若你已配好 SimX/RTL 环境（见 u1-l3、u1-l4），可在 `--debug` trace 里交叉验证某条指令依次经过这六级的时序。

## 6. 本讲小结

- `VX_core.sv` 是流水线“总装车间”，用 `schedule_if`/`fetch_if`/`decode_if`/`dispatch_if`/`commit_if`/`writeback_if` 把六级模块串成一根管子，并维护 `busy` 状态。
- **Fetch** 用 wid 索引的 `tag_store` 把异步 icache 响应拼回原 PC/tmask；RVC 时挂 `VX_decompressor`。
- **Decode** 把 32 位指令字译码成 `decode_t`，并通过 `is_wstall`→`decode_sched_if` 反馈调度器暂停取指。
- **Issue** 内部是 IBuffer（每 warp 队列 + uop 展开）→ Scoreboard（三条件发射门控：数据就绪 + FU 未满 + FU 锁未冲突）→ Operands（读源操作数）→ Dispatcher（按 `ex_type` 分发 + 归还 `fu_release` 信用）的接力。
- **Execute** 是一组按 `EX_ALU/EX_LSU/EX_SFU/EX_FPU/EX_TCU` 编号并列的功能单元容器，每个单元内部是 lane_dispatch → pe_switch → 计算 → lane_gather 的“分—算—聚”结构。
- **Commit** 用优先级仲裁归并多 FU 结果，按 `tmask` 门控字节写回，并以 `eop` 为粒度向调度器回报退休。

## 7. 下一步学习建议

- 下一讲 **u7-l3（调度器与 warp 控制 RTL）** 会深入 `VX_scheduler.sv`、`VX_split_join.sv`、`VX_ipdom_stack.sv`，讲清本讲一直当作黑盒的 Schedule 级内部——warp 状态机与分支发散/汇聚。
- 如果你对 Issue 级的冒险检测意犹未尽，可直接精读 `VX_scoreboard.sv` 全文，并与 u6-l3 的 SimX `scoreboard.cpp` 对照，体会“位掩码冒险检测”在两套实现里的一致性。
- 对 Execute 级的访存路径感兴趣的读者，可先跳读 `VX_lsu_unit.sv`/`VX_lsu_slice.sv`，完整的 LSU 流水线留到 u8-l4 专讲。
- 学完 u7-l3 后，配合 u7-l4（model_parity）理解为什么 RTL 改动必须同步更新 SimX 时序模型——本讲建立的“级间对应表”正是那条纪律的工作底稿。
