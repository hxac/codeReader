# 调度器与 warp 控制 RTL

## 1. 本讲目标

上一讲（u7-l2）我们把 `VX_core` 的六级流水线各级模块摆到了桌面上。本讲要钻进流水线的最前端——**Schedule 级**，以及它与 Issue 级之间最关键的两道控制机制：

- 每拍由谁发射（warp 调度器 `VX_scheduler.sv`）；
- 发射前指令在哪里等、靠什么判断能不能动（`VX_ibuffer.sv` 与 `VX_scoreboard.sv`）；
- 分支发散时如何把一个 warp 拆成两条执行路径、最后再合回来（`VX_split_join.sv` + `VX_ipdom_stack.sv`）。

学完后你应当能够：

1. 读懂 `VX_scheduler` 的「双拍掩码 + 优先编码」选择逻辑，说清楚一个 warp 从被 CTA 唤醒到被选中发射经历了哪些状态翻转。
2. 说清楚 IBuffer、Scoreboard 在 RTL 里各自拦的是什么，以及它们如何与调度器形成反压。
3. 用自己的话讲清 **IPDOM（Immediate Post-Dominator，立即后支配点）重汇聚** 的原理，并把 SPLIT/JOIN 两条指令映射到 `thread_masks` 寄存器与 IPDOM 栈的保存/恢复动作上。
4. 对照 SimX 同名模块（见 u6-l1、u6-l3），理解 RTL 与 SimX 在 warp 状态机上的逐拍对应——这是 model_parity 的物理基础。

## 2. 前置知识

在进入 RTL 前，先用三段话把心智模型建立起来（细节在 u1-l1、u4-l2 已铺过，这里只复习到够用）。

**SIMT 与 warp。** Vortex 用 SIMT（Single Instruction, Multiple Thread）执行：一个 **warp** 内的所有 **thread** 共享一个 PC，每拍一起往前走一步；哪些 thread 真正参与运算、写回，由一个 `NUM_THREADS` 位的 **tmask（thread mask）** 控制。所以「PC 是 warp 级的，寄存器是 thread 级的」——这是本讲反复出现的一句话。

**分支发散（divergence）。** 当一个 warp 执行到 `if (cond)` 而 warp 内各 thread 的 `cond` 取值不一致时，这个 warp 就「发散」了：一部分 thread 走 then，一部分走 else。GPU 不能让一个 warp 同时走两条路，于是采用 **IPDOM 重汇聚**：先跑一边，跑到两条路径的最近共同后继（立即后支配点）时，再跑另一边，最后在汇合点把两边合起来。Vortex 用 `SPLIT`/`JOIN` 两条指令 + 一个硬件栈（IPDOM 栈）来实现它。仓库 `docs/references.md` 明确指出这就是 MICRO 2007 提出的 baseline 立即后支配点重汇聚栈（见 [references.md:38-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/references.md#L38-L39)）。

**六级流水线里的位置。** 回顾 [microarchitecture.md:40-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L40-L47)：Schedule 级含 Warp Scheduler 与 IPDOM Stack；Issue 级含 IBuffer 与 Scoreboard。本讲就是把这些方框打开。

> 术语速查：`wid`（warp id）、`tmask`（线程激活掩码）、`active_warps`（已激活的 warp 集合）、`stalled_warps`（被挂起、本拍不能选的 warp 集合）、`PC`（warp 的取指地址）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hw/rtl/core/VX_scheduler.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv) | Schedule 级主体。维护每 warp 的 PC/tmask/激活/挂起状态，每拍仲裁选出一个 warp 发射，并响应分支、SPLIT/JOIN、屏障、wspawn、trap 等控制事件。 |
| [hw/rtl/core/VX_ibuffer.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ibuffer.sv) | 每 warp 一个的指令缓冲 FIFO，并挂接微操作展开器 `VX_uop_sequencer`。 |
| [hw/rtl/core/VX_scoreboard.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv) | 发射门控：寄存器冒险检测 + 功能单元容量/锁检测，三关全过才允许发射。 |
| [hw/rtl/core/VX_split_join.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_split_join.sv) | 把 SPLIT/JOIN 的语义翻译成对 IPDOM 栈的压/弹与对 tmask 的切换。 |
| [hw/rtl/core/VX_ipdom_stack.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ipdom_stack.sv) | 每 warp 独立的发散栈存储（双口 RAM + 指针/标志），保存 {重汇聚掩码, 重汇聚 PC}。 |
| [hw/rtl/core/VX_wctl_unit.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_wctl_unit.sv) | Execute 级 SFU 内的 warp 控制子单元：执行到 SPLIT/JOIN/TMC/WSPAWN/BAR 时，算出 `split_t`/`join_t` 等结构送给调度器。 |
| [hw/rtl/VX_gpu_pkg.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv) | 共享类型/参数包：`split_t`、`join_t`、`DV_STACK_SIZE` 等。 |
| [hw/rtl/interfaces/VX_warp_ctl_if.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/interfaces/VX_warp_ctl_if.sv) | `wctl_unit`（Execute）↔ `scheduler`（Schedule）的控制接口。 |

---

## 4. 核心概念与源码讲解

### 4.1 调度器核心：warp 状态机与每拍仲裁（VX_scheduler.sv）

#### 4.1.1 概念说明

调度器要回答的核心问题是：**「下一拍取哪条 warp 的指令？」** 这个问题看似简单，但前提是调度器必须随时知道每条 warp 的「人生阶段」——它被 CTA 唤醒了吗？它现在能不能动（还是卡在分支、屏障、等待操作数上）？它的 PC 和 tmask 是什么？为此调度器为每个 warp 维护四组状态，全部按 warp 索引（数组下标即 `wid`）。

调度器还承担一个「事件汇总中心」的角色：取指前的 `decode`、发射后的 `issue`、执行完的 `commit`，以及来自 SFU 的各类 warp 控制指令（分支、SPLIT/JOIN、屏障、wspawn、trap），都把结果汇到这里来更新 warp 状态。所以你会看到它的端口特别多。

#### 4.1.2 核心流程

调度器每拍做三件事，顺序很关键：

1. **算下一拍状态（组合逻辑）**：把本拍所有事件（CTA 派发、分支解析、SPLIT/JOIN、屏障释放……）叠加到 `*_n`（next）变量上。
2. **选 warp（组合逻辑）**：从「激活且未挂起且 ibuffer 没满」的 warp 里，用优先编码选一个。
3. **寄存（时序）**：把 `*_n` 打入寄存器，把选中的 `{tmask, PC, wid, cta_id}` 经弹性缓冲送进 Fetch。

挑选公式可以写成：

\[
\text{ready\_warps} = \text{active\_warps}\ \&\ \sim\text{stalled\_warps}
\]

\[
\text{preferred\_warps} = \text{ready\_warps}\ \&\ \sim\text{ibuf\_full}
\]

最终从 `schedule_warps` 里取最低位（优先编码）。

#### 4.1.3 源码精读

先看四组核心状态寄存器，它们是整个调度器的「主存」：

[VX_scheduler.sv:51-55](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L51-L55) — `active_warps`/`stalled_warps` 是每 warp 一位的集合；`thread_masks`/`warp_pcs` 是每 warp 一组（tmask 或 PC）的数组。注意这里同时声明了当前值与下一拍值 `_n`，构成「双拍」更新风格。

一个 warp 的一生从被 CTA 唤醒开始。CTA 派发器是调度器内部例化的子模块，它从 KMU 总线收 CTA、按 `NUM_THREADS` 把一个 CTA 切成若干 warp，再唤醒：

[VX_scheduler.sv:90-112](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L90-L112) — `cta_dispatcher` 把 `cta_fire/cta_wid/cta_PC/cta_tmask/cta_param` 喂回调度器。注意 L88 的 `cta_warp_done`：当一条 `TMC`（thread mask clear）把 tmask 写成全 0，这条 warp 就「退休」了，派发器会复用它的槽位塞下一个 CTA。

被唤醒后，warp 的初始状态在组合块里写入：

[VX_scheduler.sv:179-186](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L179-L186) — `cta_fire` 时：置 `active=1`、装填初始 tmask；PC 的处理很巧妙——若是新 warp 走完整 prologue（`cta_init`）用 `cta_PC`，若是**复用**同一条 warp 跑下一个 CTA，则把 PC 回拨 20 字节（5 条指令），重新执行那段「按 CSR 取 kernel 入口与参数」的固定派发窗口（承接 u4-l1 讲过的 CTA rewind）。

「挂起」是调度器的核心抓手。一旦某条 warp 的指令被选中发射，它就立刻被挂起，直到下游某级把它解锁：

[VX_scheduler.sv:267-269](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L267-L269) — 发射即挂起（`stalled_warps_n[schedule_wid] = 1`）。

[VX_scheduler.sv:188-191](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L188-L191) — 解锁来源之一：decode 级处理完一条指令后回 `decode_sched_if.unlock`，warp 才能再次被选。也就是说，**一条 warp 从发射到 decode 完成期间，占着「挂起」状态不让别人插队本 warp，但调度器会去选别的就绪 warp**——这就是 warp 级时间复用隐藏延迟的方式（与 SimX `scheduler.cpp` 的 `active_warps_`/`stalled_warps_` 双掩码完全同构，见 u6-l1）。

PC 的推进与是否开启 RVC 压缩指令有关：

[VX_scheduler.sv:271-283](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L271-L283) — 开了 `EXT_C` 时由解压器告诉调度器本条是 2 字节还是 4 字节（`is_rvc ? +2 : +4`）；否则在指令真正进入 Fetch 后（`schedule_if_fire`）统一 `+4`。

最后看「选 warp」的实现，这是调度器最直观的一段：

[VX_scheduler.sv:447-486](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L447-L486) — `ready_warps` 去掉 ibuffer 已满的 warp 得到 `preferred_warps`；`g_ibuf_cnt`（L457-472）是每 warp 一个的 ibuffer 占用计数器，`incr` 是本调度器选中它、`decr` 是下游 ibuffer 弹出。这里还有一处对 L1 缓存的特殊处理（L475-486）：不开 L1 时若所有 ibuffer 满会死锁（icache/dcache 共享总线），于是退化为只要 ready 就可调度的 `schedule_warps = preferred_warps`。

[VX_scheduler.sv:488-495](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L488-L495) — `VX_priority_encoder` 选最低位 wid，并给出 onehot。**固定优先级（而非轮转）是刻意的**：它让 wid=0 的 warp 在 model_parity 对拍时行为确定，保证 SimX 与 RTL 退休指令逐条一致。

选出的 `{tmask, PC, wid, cta_id, uuid}` 经一个深度为 2 的弹性缓冲送出：

[VX_scheduler.sv:524-537](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L524-L537) — `out_buf` 缓存 `ready_in` 反压，`OUT_REG=1` 让输出寄存，方便 Fetch 用 BRAM 取指。

> 小贴士：`busy` 信号（[L580-582](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L580-L582)）= 「还有激活 warp 或仍有在途指令」`||` CTA 派发器忙。它就是 u5-l2 里 `any_running()` 的 RTL 对应——只有 `busy=0` 时整核才停机，确保所有写都已落地。

#### 4.1.4 代码实践

**目标**：把调度器的状态机画出来，理解「发射即挂起」如何驱动 warp 间切换。

**操作步骤（源码阅读型）**：

1. 打开 [VX_scheduler.sv:172-302](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L172-L302) 的组合 `always @(*)` 块。
2. 列出所有把 `stalled_warps_n[...]=0`（解锁）的分支：`decode_sched_if.unlock`、`wspawn`、`tmc`、`split`、`join`、`bar_unlock`、`wsync`、`branch`、（可选）`async_trap`。再找到唯一把 `stalled=1` 的地方（`schedule_fire`）。
3. 画一张状态图：节点 = `{active, stalled}` 两位；边 = 上述事件，标注触发条件。

**需要观察的现象 / 预期结果**：

- 一个 warp 一旦 `schedule_fire` 就进入 `stalled=1`，必须等到 decode 解锁才能回 `stalled=0`。
- 因此同一拍里，调度器绝不会连续两次选同一条刚发射的 warp——这避免了一条 warp 「自我插队」。
- 把这张图与 u6-l1 里 SimX `Scheduler` 的 `stalled_warps_`/`stalled_warps_next_` 双拍更新对照，确认两者语义一致（这是 model_parity 的前提）。

> 本实践为源码阅读型，命令运行结果「待本地验证」（若想看真实状态翻转，可按 u13-l2 用 `--debug` 在 rtlsim 上抓 `warp-state` trace，调度器在 [L680-690](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L680-L690) 恰好打印 `warp-state: wid=.. active=.. stalled=.. tmask=..`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么调度器用优先编码（固定选最低 wid）而不是轮转仲裁？

**参考答案**：固定优先级让每拍选中的 wid 完全由当前的 `active/stalled/ibuf_full` 位图决定，没有内部轮转指针带来的不确定性；这样 SimX 与 RTL 在同一组 warp 状态下必然选出同一条 warp，退休指令逐条对齐，满足 model_parity 的「精确一致」要求。

**练习 2**：`cta_fire` 时，复用一条旧 warp（`!cta_init`）和唤醒一条新 warp，PC 的初值有何不同？为什么？

**参考答案**：新 warp 走完整 prologue，`warp_pcs = cta_PC`（kernel 起点）；复用旧 warp 时把 PC 回拨 20 字节（5 条指令），回到那段「读 CSR 取入口与参数再跳转」的固定派发窗口，省掉一次性 prologue，直接进入下一个 CTA 的 kernel 调用。

---

### 4.2 IBuffer 与 Scoreboard：发射前的两道关卡

#### 4.2.1 概念说明

调度器选出的指令并不会立刻被执行单元吃掉。在 Issue 级，它要先过两道关：

- **IBuffer（指令缓冲）**：每 warp 一个的小 FIFO，把 decode 出来的指令暂存起来，**解耦「decode 速率」与「发射速率」**。调度器只管往里塞，执行单元有空才从队头取。它还顺带挂着 `VX_uop_sequencer`，把 WGMMA 这类宏指令展开成多个微操作（uop）——这是 u6-l2 讲过的 sequencer 的 RTL 版本。
- **Scoreboard（记分板）**：判断队头指令**能不能动**。它拦三类东西：源寄存器还没就绪（RAW/WAW/WAR 冒险）、目标功能单元队列快满了（容量反压）、功能单元被某条多 uop 指令锁住了（fu_lock）。

两者关系是串联：IBuffer 存指令 → Scoreboard 看队头能否过 → 能过则进操作数收集与派发。

#### 4.2.2 核心流程

```
decode ──► IBuffer(FIFO, 每 warp) ──► uop_sequencer(可选展开)
                                            │
                                            ▼
                        staging(pipe_buf) ──► Scoreboard 判定
                                            │  (regs_busy? xregs_busy?
                                            │   fu_goingfull? fu_locked?)
                                            ▼
                              就绪的 warp ──► 仲裁器 ──► Operands/Dispatcher
```

Scoreboard 的判定（组合）：

\[
\text{operands\_ready} = \neg\text{regs\_busy}\ \wedge\ \neg\text{xregs\_busy}\ \wedge\ \neg\text{fu\_goingfull[ex]}\ \wedge\ \neg(\text{fu\_locked[ex]} \wedge \text{fu\_lock})
\]

#### 4.2.3 源码精读

**IBuffer** 的核心是一个按 issue-slot（`PER_ISSUE_WARPS`）展开的生成块，每个槽是一个弹性 FIFO：

[VX_ibuffer.sv:42-77](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ibuffer.sv#L42-L77) — `VX_elastic_buffer` 实例 `instr_buf`，深度 `VX_CFG_IBUF_SIZE`，`OUT_REG=1`。注意 `valid_in` 用 `decode_wis == w` 过滤，把 decode 流按 warp 所属的 issue-slot 分发到对应 FIFO。两个常量 `1'b1 // fu_lock`、`1'b1 // fu_unlock`（L70-71）是单 uop 指令的默认锁标记。

[VX_ibuffer.sv:79-93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ibuffer.sv#L79-L93) — 若 `UOP_MAX>0`（启用了需要展开的扩展，如 TCU），FIFO 后接 `VX_uop_sequencer`；否则直通。

> 反压回调度器：`decode_if.ibuf_pop[w]`（L78）告诉调度器「这个 warp 出队了一条」，正是 4.1.3 里 `g_ibuf_cnt` 的 `decr` 来源；而 ibuffer 满会通过调度器的 `ibuf_full` 把 warp 从可选集合里剔除。

**Scoreboard** 要同时管好几件互相独立的事，所以代码用多个生成块拆开。先看功能单元容量反压：

[VX_scoreboard.sv:50-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L50-L64) — 每个 FU 一个 `VX_pending_size`，`incr=fu_issue`（发射时 +1）、`decr=fu_release`（FU 接收时 -1）；`alm_full` 即 `fu_goingfull`。注释点明这是「going-full 而非 full」的 1 槽 guard band——留余量吸收寄存反压延迟，防止已发射指令在共享操作数收集通路上卡住、饿死别的 FU。

寄存器冒险检测是记分板的灵魂，按 warp 展开：

[VX_scoreboard.sv:101-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L101-L178) — `inuse_regs` 是「当前被占用」的寄存器位图。关键三步：
- L137-146：写回（`writeback_fire`）时**释放**目的寄存器（`rd`）；
- L148-157：staging 指令发射时**占用**目的寄存器；
- L162-178：把 `inuse_regs_n` 与当前指令的操作数集合相交，只要有一位命中，对应操作数就 `busy`。

注意一个与 SimX 完全一致的细节——**释放按写回、占用按发射**，且因为 SIMD 分包响应可能乱序，须等所有分包写回才释放（这里用 `eop` 门控 `writeback_fire`，L109-111）。这与 u6-l3 讲的「commit_packet 分包计数释放」是同一思想。

最终的就绪判定与仲裁：

[VX_scoreboard.sv:186-203](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L186-L203) — `operands_ready_n` 组合上面四关，寄存一拍得 `operands_ready`。

[VX_scoreboard.sv:276-288](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scoreboard.sv#L276-L288) — 多个就绪 warp 经 `VX_generic_arbiter`（>8 路用矩阵、否则轮转，`STICKY=1` 贪心）选一个发射。

#### 4.2.4 代码实践

**目标**：确认 IBuffer「满了会反压调度器」这条链路是连通的。

**操作步骤**：

1. 在 [VX_ibuffer.sv:78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ibuffer.sv#L78) 找到 `ibuf_pop`，沿接口回溯到调度器 [VX_scheduler.sv:460](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L460) 的 `decr`。
2. 跟踪 `ibuf_full`（[L462](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L462)）如何把一个 warp 从 `preferred_warps` 里抹掉（[L474](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L474)）。
3. 回答：如果某条 warp 的 ibuffer 满，但其它 warp 的 ibuffer 没满，调度器会怎么做？

**预期结果**：调度器会跳过 ibuffer 满的那条 warp，照常选其它就绪 warp——这正是 warp 级并行隐藏延迟的体现；只有当**所有**就绪 warp 的 ibuffer 都满（`all_ibuf_full`）时，才退化为允许调度（避免 L1 缺失下的死锁，见 [L480-485](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L480-L485)）。

#### 4.2.5 小练习与答案

**练习 1**：Scoreboard 用同一套 `inuse_regs` 位图同时挡 RAW、WAW、WAR 三种冒险，会不会过度保守？为什么 Vortex 仍然这么做？

**参考答案**：会偏保守（例如纯 WAR 本可重排），但 Vortex 是顺序发射、按 warp 的单线程控制流，且要和 SimX 逐拍对齐；用统一位图实现简单、时序友好、行为确定，足以在 SIMT 下隐藏延迟，故接受这点保守性。这与 u6-l3 SimX scoreboard 的「位掩码冒险检测」是同一选择。

**练习 2**：`fu_goingfull` 用 `alm_full` 而不是 `full` 来反压，留了 1 槽 guard band，目的是什么？

**参考答案**：从记分板判定「不能发」到该判定真正抑制下游取指，中间有寄存延迟；若等队列真满了才反压，已发射的指令会在共享的操作数收集通路上排不进 FU 队列、占着通路饿死其它 FU。提前一槽反压（going-full）保证已发指令总有位置，避免队头阻塞。

---

### 4.3 分支发散与汇聚：SPLIT/JOIN 与 VX_split_join

#### 4.3.1 概念说明

现在进入本讲的重头戏。当一个 warp 执行到 `if (cond)` 且 warp 内意见不一，编译器（经 Vortex 后端）会在分支处插入一条 **SPLIT** 指令，在两条路径的汇合点插入一条 **JOIN** 指令。硬件要做的，是配合一个**栈**，先跑一边、再跑另一边、最后合体——这就是 IPDOM 重汇聚。

Vortex 把这套语义拆成两层：

- **`VX_wctl_unit`（Execute 级，SFU 内）**：执行到 SPLIT/JOIN 时，**算出**两边各自的 tmask、重汇聚 PC，封装成 `split_t`/`join_t` 结构，经 `VX_warp_ctl_if` 送给调度器。
- **`VX_split_join`（Schedule 级）**：把这些结构**翻译**成对 IPDOM 栈的压/弹动作，以及送给调度器的「下一拍 tmask 与 PC」。

数据结构定义在共享包里：

[VX_gpu_pkg.sv:619-629](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L619-L629) — `split_t` 含 `is_dvg`（是否真发散；只有 then/else 都非空才算）、`then_tmask`/`else_tmask`、`next_pc`；`join_t` 含到达掩码 `tmask` 与 `stack_ptr`。

#### 4.3.2 核心流程：一次发散→汇聚的全过程

设一个 8 线程的 warp（tmask=`11111111`），遇到 `if (cond)`，结果 thread 0,1 走 then，其余走 else。记 then 掩码 `T`、else 掩码 `E`、重汇聚点 `next_pc`。

```
时刻 A：执行到 SPLIT
  wctl_unit 算出：is_dvg=1, then_tmask=T, else_tmask=E, next_pc
  split_join：把 {T|E, next_pc} 压入 IPDOM 栈（标记位=0）
  scheduler：thread_masks[wid] ← T   （先跑 then 边）
              stalled ← 0            （放行）
              PC 继续推进（then 边的代码）

时刻 B：then 边跑到汇合点，执行 JOIN（第一次）
  split_join 读栈：发现标记位=0（这是第一次汇聚）
    => 还有一边（else）没跑！
    => join_is_else=1, join_tmask = ~arriving & (T|E) = E
    => join_pc = next_pc
    => 把栈顶标记位改写为 1（不弹栈）
  scheduler：thread_masks[wid] ← E, PC ← next_pc, stalled ← 0
              （去跑 else 边）

时刻 C：else 边也跑到汇合点，执行 JOIN（第二次）
  split_join 读栈：标记位=1（两边都跑过了）
    => join_is_else=0, join_tmask = (T|E) 全员
    => PC 不变（已在汇合点）
    => 栈指针 -= 1（真正弹栈）
  scheduler：thread_masks[wid] ← T|E（恢复满员），stalled ← 0
```

关键直觉：**同一条 JOIN 指令会被执行两次**——第一次切到另一边，第二次才真正合并并弹栈。这就是「立即后支配点」的硬件落地。

#### 4.3.3 源码精读

先看 `VX_wctl_unit` 如何算出两边掩码与「小边优先」：

[VX_wctl_unit.sv:81-103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_wctl_unit.sv#L81-L103) — `then_tmask = taken & 当前tmask`，`else_tmask = ~taken & 当前tmask`。`taken[i]` 来自各 lane 的条件位（L77-79）。

[VX_wctl_unit.sv:116-127](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_wctl_unit.sv#L116-L127) — **小边优先**：`then_first = (then数 <= else数)`，把线程数较少的那边作为 `taken_tmask` 塞进 `split.then_tmask`。直觉是先跑短边、后跑长边，尽量减少「掩码执行」（被 mask 掉的线程空转）的周期数。`split.next_pc = SPLIT 的 PC + 4`（L127），即 RTL 保存的重汇聚 PC。`is_dvg = has_then && has_else`（L124）——只有两边都非空才算真发散，否则只是一致跳转，不压栈。

[VX_wctl_unit.sv:131-133](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_wctl_unit.sv#L131-L133) — JOIN 的字段：`sjoin.tmask = then|else`（在 JOIN 处重建当前到达掩码，实际就等于当前活跃边）、`sjoin.stack_ptr = rs1`（JOIN 从寄存器拿到它要汇聚的栈层号）。

`stack_ptr` 是怎么来的？SPLIT 执行后，`VX_split_join` 把当前栈顶指针经 `warp_ctl_if.dvstack_ptr` 回送给 `wctl_unit`，后者把它作为 SPLIT 的「结果」写回寄存器（[VX_wctl_unit.sv:233-256](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_wctl_unit.sv#L233-L256)），编译器再让匹配的 JOIN 读这个寄存器。于是**每对 SPLIT/JOIN 靠这个回传的栈层号精确配对**，天然支持嵌套发散。

现在看 `VX_split_join` 如何把上面这些翻译成栈操作。整个模块在 `NT_BITS != 0`（即 `NUM_THREADS>1`）时才启用：

[VX_split_join.sv:38-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_split_join.sv#L38-L44) — 待压栈的值 `ipdom_val = {then_tmask | else_tmask, next_pc}`——即「全员掩码 + 重汇聚 PC」。

[VX_split_join.sv:46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_split_join.sv#L46) — `sjoin_is_dvg = (sjoin.stack_ptr != ipdom_wr_ptr[wid])`：JOIN 携带的栈层号若与当前栈顶不同，说明这是「带发散上下文」的真汇聚，需要查栈；否则是非发散的一致汇聚，直接放行不解栈。

[VX_split_join.sv:48-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_split_join.sv#L48-L64) — 例化 `VX_ipdom_stack`：`push = split_valid && split.is_dvg`，`pop = sjoin_valid && sjoin_is_dvg`，从栈里读回 `{orig_tmask, next_pc}` 与标记位 `ipdom_idx`。

[VX_split_join.sv:66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_split_join.sv#L66) — 这是整个机制最精妙的一行：

```verilog
wire [...] join_tmask_n = ipdom_idx ? orig_tmask : (~sjoin.tmask & orig_tmask);
```

- `ipdom_idx==0`（第一次汇聚）：取**另一边** `~arriving & 全员`；
- `ipdom_idx==1`（第二次汇聚）：取**全员** `orig_tmask`。

[VX_split_join.sv:68-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_split_join.sv#L68-L78) — 一级流水寄存器把 `{valid, wid, is_dvg, is_else=×ipdom_idx, tmask, pc}` 打拍输出（`OUT_REG=1`，对齐调度器的组合块时序）。

最后看调度器如何消费这些信号：

[VX_scheduler.sv:213-229](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L213-L229) — `split` 处理：若是真发散，`thread_masks ← then_tmask`（切到第一边），无论是否发散都解锁 warp。`join` 处理：若 `join_is_dvg`，则按 `join_is_else` 决定是否改写 PC（第一边跑完→跳到 `next_pc` 跑另一边），并更新 tmask；最后解锁。这段就是 4.3.2 流程图里「scheduler」那几行的源码。

> 若 `NUM_THREADS==1`（标量配置），发散无从谈起，整个机制被 `g_disable` 分支旁路掉（[VX_split_join.sv:82-93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_split_join.sv#L82-L93)），JOIN 直接放行——这也是为什么它用 `if (NT_BITS != 0)` 编译期守卫。

#### 4.3.4 代码实践

**目标**：用真实测试程序观察 SPLIT/JOIN，并亲手画出 tmask 在寄存器与栈之间的保存/恢复。

**操作步骤**：

1. 阅读 [tests/regression/diverge/kernel.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/diverge/kernel.cpp)。它专门构造了多种发散形态：嵌套 `if/else`（L102-114）、switch-case（L129-135）、含 `__noinline__` 的 8 层深嵌套链 `nested_chain_1to8`（L18-76）。这些都会被编译器翻译成 SPLIT/JOIN 序列。
2. 聚焦 L102-114 的两段嵌套 `if/else`。假设一个 4 线程的 warp，`task_id` 分别为 0,1,2,3，推导外层 `if (task_id > 1)` 与内层 `if (task_id > 2)` 的 then/else 掩码。
3. 画出时序表（参考 4.3.2 的格式），列出：每条 SPLIT 压栈后 `thread_masks` 变化、每条 JOIN 时栈顶标记位与恢复的掩码。注意**嵌套**发散会让栈深 ≥ 2。

**需要观察的现象 / 预期结果**：

- 外层 SPLIT 先把 warp 切成 `{0,1}` 与 `{2,3}` 两边；跑 `{0,1}` 这边时遇到内层 SPLIT，再切成 `{0}` 与 `{1}`——此时 IPDOM 栈深度为 2。
- 内层 JOIN 先汇聚 `{0}/{1}`，外层 JOIN 再汇聚 `{0,1}/{2,3}`，**LIFO 顺序**，栈逐层弹回。
- 由于 `then_first` 取小边，跑序会偏向线程少的一边。

> 运行验证（可选）：按 u1-l4 `./ci/blackbox.sh --driver=simx --app=diverge` 跑通；按 u13-l2 `--debug` 抓 trace，可在调度器输出里看到 `warp-state: ... tmask=...` 的逐拍翻转（[VX_scheduler.sv:685-687](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L685-L687)）。具体 trace 行「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `is_dvg = has_then && has_else`？若一个 warp 内所有线程都走同一边，会怎样？

**参考答案**：若全员一致走 then（或 else），没有真正的发散，不需要切分、不需要压栈——这种分支等价于一次普通跳转。`is_dvg=0` 时 split_join 不压栈，调度器只解锁 warp、不改 tmask，省掉一次栈操作。

**练习 2**：`then_first` 让线程少的一边先跑。若 then 有 1 个线程、else 有 7 个，先跑哪边？两次 JOIN 之间哪一边在「空转」？

**参考答案**：`then_first` 为真（1 ≤ 7），`split.then_tmask` 取 then 边，先跑 1 个线程的 then 边；此时另外 7 个线程被 mask 掉、随 warp 一起前进但空转。直到第一次 JOIN 切到 else 边，那 7 个线程才真正工作，而此时 then 那 1 个线程反过来空转。先跑短边使「总空转线程周期」较小。

---

### 4.4 IPDOM 栈：发散状态的保存与恢复（VX_ipdom_stack.sv）

#### 4.4.1 概念说明

`VX_split_join` 只决定「压还是弹、切到哪边」，**真正把状态存下来**的是 `VX_ipdom_stack`。它是一个**每个 warp 独立**的栈，深度由配置决定：

[VX_gpu_pkg.sv:80-81](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L80-L81) — `DV_STACK_SIZE = UP(NUM_THREADS-1)`。最坏情况下一个 warp 的 `NUM_THREADS` 个线程可层层两两发散，嵌套深度上限为 `NUM_THREADS-1`，故栈深如此取值（例如 32 线程 → 深度 31）。

它的两个独门设计值得专门讲：

1. **「标记位 + 减法」实现两次 JOIN**：栈里每一项除了 `{mask, pc}` 还存一位「访问标记」。第一次 JOIN 读到标记 0，只改写标记为 1、不弹；第二次 JOIN 读到标记 1，才真正弹（指针减 1）。弹栈时指针「减去标记位的值」——0 或 1——一行代码同时表达「不弹/弹」。
2. **每 warp 独立的指针，但共享一块 RAM**：每个 warp 有自己的 `wr_ptr`/`empty`/`full` 寄存器，但存储用一块按 `{warp, 深度}` 编址的双口 RAM，节省面积。

#### 4.4.2 核心流程

```
push（SPLIT 发散）:
  RAM[wid, wr_ptr] ← {标记0, (then|else, next_pc)}
  wr_ptr[wid] ++

pop（JOIN 发散，读旧值、写新值同址）:
  {标记old, (mask, pc)} ← RAM[wid, rd_ptr]      // 读旧
  RAM[wid, rd_ptr] ← {标记1, (mask, pc)}         // 写新（标记置1）
  wr_ptr[wid] -= 标记old                          // 0→不弹, 1→真弹
```

`rd_ptr` 来自 JOIN 携带的 `stack_ptr`（即当初 SPLIT 回传的层号）。

#### 4.4.3 源码精读

每 warp 的指针与空/满标志：

[VX_ipdom_stack.sv:41-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ipdom_stack.sv#L41-L74) — 生成块为每个 warp 维护 `wr_ptr_r/empty_r/full_r`。`push_s`/`pop_s` 用 `wid==i` 把总线上的操作路由到对应 warp。三条运行时断言（L49-51）禁止「满压、空弹、同拍既压又弹」。

[VX_ipdom_stack.sv:59-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ipdom_stack.sv#L59-L67) — push 时 `wr_ptr++`、`empty=0`、`full=(指针到顶)`；pop 时 `wr_ptr -= q_idx`（`q_idx` 即读出的标记位）、`empty=(rd_ptr==0)&&q_idx`。**`wr_ptr - q_idx` 是全栈最浓缩的设计**。

地址生成按 `DEPTH` 与 `NUM_WARPS` 是否 >1 分四种情况，把 `{层号, wid}` 拼成 RAM 地址：

[VX_ipdom_stack.sv:76-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ipdom_stack.sv#L76-L96) — 例如两者都 >1 时 `waddr = push ? {wr_ptr[wid], wid} : {rd_ptr, wid}`，读写都带 wid 作高位。

最后是那块双口 RAM，标记位的读写把戏在这里：

[VX_ipdom_stack.sv:98-113](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ipdom_stack.sv#L98-L113) — `BRAM_DATAW = 1 + WIDTH`，最高位就是标记。`wdata = push ? {1'b0, d_val} : {1'b1, q_val}`：压栈写标记 0；弹栈时把**刚读出的值原样写回、但标记置 1**。`rdata = {q_idx, q_val}` 把标记位作为 `q_idx` 暴露给上层，正是 `VX_split_join` 那行三元运算的判据。`RDW_MODE="R"` 表示读返回旧值（read-during-write 返回前值），保证第一次 JOIN 读到的是标记 0。

> 与 SimX 对照：u6-l1 里 SimX 的 `warp_t` 持有 `ipdom_stack`，其压入 `{reconvergence mask, reconvergence PC}`、SPLIT 先走线程较少一边、JOIN 两次汇聚——语义与本节 RTL 完全一致。RTL 用「标记位 + 减法」把两次 JOIN 压进同一存储槽，是面积更省的等价实现。

#### 4.4.4 代码实践

**目标**：把 4.3.4 画出的时序表，再补上「栈内容」一列，亲手走一遍 RAM 的读写。

**操作步骤**：

1. 仍用 4.3.4 的嵌套 `if/else`（4 线程 task_id=0..3）。
2. 对每个 SPLIT，写下：写入地址 `{wid, wr_ptr}`、写入数据 `{标记0, (全员mask, next_pc)}`、写后 `wr_ptr`。
3. 对每个 JOIN，写下：读地址、读出的标记位（0 还是 1）、回写数据 `{标记1, 旧值}`、`wr_ptr -= 标记` 后的结果。
4. 核对：第二次（最深层）JOIN 时 `q_idx` 应为 1，指针才真正回退。

**预期结果**：你会看到「先压两层 → 内层先两次 JOIN 弹一层 → 外层再两次 JOIN 弹一层」的 LIFO 节奏；每一层的第一次 JOIN 指针不动（`-=0`），第二次 JOIN 指针退一（`-=1`）。栈最终回到空。

> 这是一个纯阅读/推演型实践，不需要运行；若要核对，可用 u13-l2 的 trace 在 rtlsim 上观察（结果「待本地验证」）。

#### 4.4.5 小练习与答案

**练习 1**：栈深为何取 `UP(NUM_THREADS-1)` 而不是无限大？超过会怎样？

**参考答案**：一个 warp 至多有 `NUM_THREADS` 个线程，最深的二分发散嵌套不会超过 `NUM_THREADS-1` 层，故栈深按此上限取值即可覆盖所有合法程序。若程序（理论上）超出，`full` 标志置位，[L49](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_ipdom_stack.sv#L49) 的运行时断言会触发「writing to a full stack」报错。

**练习 2**：弹栈时为什么要把读出的值「原样写回、只改标记位」？

**参考答案**：因为同一个栈项要被 JOIN 读两次：第一次读标记 0（切到另一边）、第二次读标记 1（合体并真弹）。第一次读后必须把标记记为「已访问过一次」，第二次读才能区分。把旧值带标记 1 写回，正是为第二次 JOIN 留下「这是第二次」的记号；而指针 `-= q_idx`（读出的旧标记）让第一次 `-=0`（不弹）、第二次 `-=1`（弹），巧妙复用同一存储。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从 CTA 唤醒到嵌套发散汇聚」的完整纸面推演。

**背景程序**：[tests/regression/diverge/kernel.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/diverge/kernel.cpp) 的 L102-114 嵌套 `if/else`。设 `NUM_THREADS=4`，warp 内 `task_id = {0,1,2,3}`。

**任务**：

1. **调度器状态**（4.1）：写出该 warp 被 CTA 唤醒后，`active_warps`/`stalled_warps`/`thread_masks`/`warp_pcs` 的初值；并标出第一次 `schedule_fire` 后 `stalled` 的翻转。
2. **IBuffer/Scoreboard**（4.2）：说明 SPLIT 这条指令从 decode 进入 ibuffer，到 scoreboard 放它过去（它没有寄存器冒险、但要走 SFU），经历了哪些判定。
3. **SPLIT/JOIN**（4.3）：列出外层与内层两对 SPLIT/JOIN 各自的 `then_tmask`/`else_tmask`/`is_dvg`，以及 `then_first` 决定的跑序。
4. **IPDOM 栈**（4.4）：画出栈在每次压/弹后的内容（含标记位与指针值），确认嵌套的 LIFO 弹栈正确。

**交付物**：一张时序表，列含「时刻 / 事件 / active / stalled / thread_masks / PC / 栈内容（含标记）/ wr_ptr」。完成后，你应当能解释：为什么这个程序在 SimX 与 RTL 上退休的指令序列必然一致（答案就在「固定优先级调度 + 确定的 tmask 切换 + 确定的栈操作」这三点确定性上——即 model_parity 的根基）。

## 6. 本讲小结

- **调度器 = 状态机 + 仲裁器**：`VX_scheduler` 用 `active_warps`/`stalled_warps`/`thread_masks`/`warp_pcs` 四组每 warp 状态驱动一个优先编码器，每拍选一个就绪 warp 发射，发射即挂起、由下游事件解锁。
- **双拍更新**：组合块算 `*_n`、时序块打入，保证本拍释放的 warp 不会本拍被重选——这是与 SimX cycle 级对齐的前提。
- **IBuffer 解耦、Scoreboard 门控**：IBuffer 是每 warp FIFO（带 uop 展开），满则反压调度器；Scoreboard 用统一位图挡 RAW/WAW/WAR，加上 FU 容量与 fu_lock，三关全过才发射。
- **SPLIT/JOIN = IPDOM 重汇聚**：`VX_wctl_unit` 算两边掩码（小边优先），`VX_split_join` 翻译成栈操作；先跑一边、JOIN 切另一边、再 JOIN 合体。
- **IPDOM 栈的精华是「标记位 + 减法」**：同一栈项被 JOIN 读两次，靠标记位区分「切边/合体」，弹栈指针 `-= q_idx` 一行表达「不弹/弹」。
- **RTL 与 SimX 同构**：本讲每个机制都能在 u6-l1/u6-l3 的 SimX 代码里找到对应，这是 model_parity 门控的物理基础。

## 7. 下一步学习建议

- 下一讲 **u7-l4（SimX↔RTL model parity）** 会把本讲反复提到的「逐拍一致」正式变成一条 CI 门控规则，建议结合本讲的确定性来源（固定优先级、确定 tmask、确定栈操作）一起读。
- 想看 Execute 级如何产生 SPLIT/JOIN 的完整上下文，可回看 u6-l4 的 SFU 分派器部分，以及精读 [VX_wctl_unit.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_wctl_unit.sv) 里 `wspawn`/`bar`/`wsync` 的处理（本讲只细讲了 split/join）。
- 想验证本讲推演，可按 u13-l1 跑 `diverge` 回归，按 u13-l2 用 `--debug` 抓 trace，对照调度器的 `warp-state` 打印逐拍核对。
