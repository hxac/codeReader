# 发射、记分板与操作数收集

## 1. 本讲目标

本讲聚焦 Vortex 核心 6 级流水线中的 **Issue（发射）** 级，以及它前后紧密咬合的两个伙伴——**Scoreboard（记分板）** 与 **Operands（操作数收集）**，再加上把指令分发到各功能单元的 **Dispatcher（分发器）**。

学完本讲你应当能够：

1. 说清楚一条指令从 **ibuffer** 出发，到进入 **ALU/FPU/LSU/SFU** 之间经历了哪几道关卡，以及每一道关卡在挡什么。
2. 读懂 `Scoreboard` 如何用「每 warp 的寄存器位掩码」判断一条指令能否发射，并理解它为何要等**所有 SIMD 分包都提交**才释放目的寄存器。
3. 理解 `OpcUnit` 如何持有「每 warp、每线程」的整数/浮点寄存器堆，`Operands` 如何在发射时刻读源操作数、在提交时刻写回结果。
4. 画出 `Operands → OpcUnit 读寄存器 → Dispatcher → 功能单元` 的完整数据通路。

本讲只覆盖 SimX 视角；RTL 侧对应实现（`VX_scoreboard.sv` / `VX_ibuffer.sv` 等）在 u7 单元讲解，本讲末尾会点到 SimX↔RTL 一致性这条主线。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**（1）一条指令的「影子」：`instr_trace_t`。** SimX 不是一个真的硬件，它用 `instr_trace_t` 结构体来代表一条指令在流水线里流动的「影子」（trace）。这条影子携带了指令的 `wid`（warp 号）、`PC`、`tmask`（哪些线程活跃）、`fu_type`（去哪个功能单元）、`src_regs`/`dst_reg`（源/目的寄存器）、`src_data`/`dst_data`（操作数的实际数值）等所有信息。流水线的每一级本质上都是在搬运、填充、消费这个结构体。`instr_trace_t` 定义在 [sim/simx/instr_trace.h:26-179](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/instr_trace.h#L26-L179)。

**（2）SIMT 模型回顾。** Warp 是调度的基本单位（共享 PC），thread 是执行的基本单位（各有寄存器）。所以「读寄存器」永远要读「所有活跃线程」的那一份，`tmask` 决定哪些线程参与。这一点直接决定了 `OpcUnit` 的寄存器堆为什么是 `[reg][thread]` 的二维结构。

**（3）冒险（hazard）的三种类型。** 经典处理器里，先后两条指令若操作同一寄存器，会有三种冲突：

- **RAW**（Read After Write，写后读）：后一条要用前一条的结果，必须等前一条写完。
- **WAW**（Write After Write，写后写）：两条都要写同一寄存器，必须保持顺序。
- **WAR**（Write After Read，读后写）：后一条要写、前一条还在读。

Vortex 的 Scoreboard 用最简单也最保守的策略：**只要一个寄存器正被某条在途指令占用（无论读还是写），后面任何要用它的指令都不能发射。** 这同时挡住了 RAW、WAW、WAR 全部三种冒险。

**（4）配置量提醒。** 本讲出现的几个关键配置量（来自 `VX_config.toml`）：

| 配置量 | 默认值（`NUM_WARPS=4` 时） | 含义 |
|---|---|---|
| `VX_CFG_ISSUE_WIDTH` | `up(NUM_WARPS/16)` = 1 | 每个 core 的发射通道数（issue lane） |
| `VX_CFG_NUM_OPCS` | `up(NUM_WARPS/(4·IW))` = 1 | 每个发射通道内的操作数收集器（OPC）数 |
| `VX_CFG_IBUF_SIZE` | 4 | 每 warp 的指令缓冲深度 |
| `VX_CFG_DISPATCH_QUEUE_SIZE` | 4 | Dispatcher 到 FU 的派发队列深度 |
| `VX_CFG_NUM_ALU_BLOCKS` / `NUM_ALU_LANES` | 1 / `SIMD_WIDTH` | ALU 物理块数 / 每块 SIMD 通道 |

其中 `up(x)` 是一个「保底为 1」的上取整宏，定义为 \(\,(x \neq 0)\,?\,x\,:\,1\,\)，见 [ci/gen_config.py:570-580](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L570-L580)。注意 `expr:` 里的 `/` 在本配置系统里被强制改写成**整数除法**（见 [ci/gen_config.py:1236-1242](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1236-L1242)），所以 `up(4/16)=up(0)=1`。这意味着默认配置下只有 **1 个发射通道、1 个 OPC**，走「直通（pass-thru）」路径；当 warp 数增大（如 32 个 warp 时 `ISSUE_WIDTH=2, NUM_OPCS=4`）时才会启用多 OPC 仲裁。本讲会同时讲清这两种形态。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `sim/simx/` 下）：

| 文件 | 作用 |
|---|---|
| [core.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp) | 把 ibuffer / scoreboard / operands / dispatcher / 功能单元**接线**的总装车间，`issue()` 与 `commit()` 在此 |
| [scoreboard.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.h) | 寄存器占用位掩码、`in_use` 冒险检测、`reserve/release`、分包计数 `commit_packet` |
| [opc_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.h) | **真正的寄存器堆**：每 warp、每线程的整型/浮点寄存器，提供 `read_src` / `writeback` |
| [operands.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.h) | 操作数收集级：按发射通道实例化若干 OPC，统一对外提供 `fetch_operands` / `writeback` |
| [dispatcher.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.h) | 把一条 warp 指令按 SIMD 通道**拆成多个 packet**，轮流送入功能单元的物理块 |

## 4. 核心概念与源码讲解

### 4.1 IBuffer 与发射门控

#### 4.1.1 概念说明

**IBuffer（Instruction Buffer，指令缓冲）** 是每 warp 一个的小型 FIFO，夹在 Decode 级和 Issue 级之间。它的作用是「解耦取指/译码的速度与发射的速度」：译码好的 `instr_trace_t` 先压进 ibuffer，发射级再从里面挑能发射的指令。这样即使一条指令因为冒险暂时发不出去，后面的取指/译码也能继续往 ibuffer 里填（只要没满）。

**发射门控（issue gating）** 是指：在每个时钟周期，发射级要对 ibuffer 队头的指令过一道道「关卡」，全过了才真正送进操作数收集级。这些关卡依次是：

1. **Scoreboard 冒险检测**：源/目的寄存器是否正被占用？
2. **FU lock（功能单元锁）**：某些特殊指令需要独占某个功能单元，是否已被别的 warp 占着？
3. **派发信用（dispatch credit）**：目标 FU 的派发队列是否快满了？

只有全部放行，这条指令才会被仲裁器（arbiter）选中、送进 Operands。

#### 4.1.2 核心流程

每个 core 的 `issue()` 每个周期做两件事（伪代码）：

```
对每个发射通道 iw:
  (A) 把上一拍 Operands 输出端的 trace 推进到对应 FU 的 Dispatcher 输入端
  (B) 从本通道的多个 warp 里挑一条可发射指令：
      for 每个 warp w (lane iw):
         peek ibuffer 队头 trace
         uop = sequencer.get(trace)        # 取当前微操作（见 u6-l2）
         if scoreboard.in_use(uop):        # 关卡 1：冒险
            记 scrb_stall
         else if FU 被锁:                  # 关卡 2
            跳过
         else if ray pool 满（仅 RTU）:     # 关卡 2.5
            跳过
         else:
            ready_set 置位
            若 FU 队列将满 → suppress_set 置位   # 关卡 3
      if ready_set 非空:
         w = arbiter.grant(ready_set, suppress_set)   # 选一条
         把 uop 发给 Operands.Input
         Operands.fetch_operands(uop)      # 立即读源操作数
         若 uop.wb: scoreboard.reserve(uop) # 占用目的寄存器
         sequencer.advance()                # 推进微操作（宏指令可能裂多条）
```

关键点：**Scoreboard 检测、读操作数、reserve 必须在「同一拍、同一条 uop」上完成**——一旦决定发射，就立刻读源（拿到当前值）、立刻占用目的（防止后续指令抢），三者原子。

#### 4.1.3 源码精读

ibuffer 在 core 构造时按 warp 数量创建，深度为 `VX_CFG_IBUF_SIZE`（默认 4）：

```cpp
// 每个 warp 一个 ibuffer，深度 VX_CFG_IBUF_SIZE
for (uint32_t i = 0; i < ibuffers_.size(); ++i) {
  snprintf(sname, 100, "%s-ibuffer%d", name.c_str(), i);
  ibuffers_.at(i) = TFifo<instr_trace_t*>::Create(sname, 1, VX_CFG_IBUF_SIZE);
}
```
出处 [sim/simx/core.cpp:111-114](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L111-L114)。Decode 级译码完一条指令后，先检查 ibuffer 是否已满，满了就记 `ibuf_stalls` 并停一拍，否则 push 进去：

```cpp
auto& ibuffer = ibuffers_.at(trace->wid);
if (ibuffer->full()) {
  ++perf_stats_.ibuf_stalls;
  return;
}
// ...译码填充 trace 各字段...
ibuffer->push(trace);
```
出处 [sim/simx/core.cpp:445-499](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L445-L499)。注意其中一段过滤逻辑：写 `x0` 的指令被标记为不写回（`wb=false`），所以不会占用记分板：

```cpp
// x0 写在 RISC-V 里是静默的，不占用 scoreboard 槽位
trace->wb = (dst.type != RegType::None)
         && !(dst.type == RegType::Integer && dst.idx == 0);
```
出处 [sim/simx/core.cpp:470-474](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L470-L474)。

发射级本体在 `issue()`。下面是「关卡 1 + 关卡 3」的核心片段，先查记分板，再按信用抑制：

```cpp
auto uop_trace = seq->get(trace);  // 取当前微操作
if (scoreboard_->in_use(uop_trace)) {
  // 关卡 1：有冒险，记 stall
  any_scrb_blocked = true;
} else {
  // 关卡 2：FU 锁
  if (fu_locked_.at(iw).test(fu) && uop_fu_lock) continue;
  ready_set.set(w);                 // 标记可发射
  // 关卡 3：目标 FU 派发队列将满 → 抑制
  if (fu_credits_.at(iw).at(fu) >= VX_CFG_DISPATCH_QUEUE_SIZE - 1)
    suppress_set.set(w);
}
```
出处 [sim/simx/core.cpp:527-574](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L527-L574)。仲裁选中后，在同一拍内完成「发送给 Operands → 读源 → reserve 目的寄存器」三连：

```cpp
if (operands_.at(iw)->Input.try_send(uop_trace)) {
  operands_.at(iw)->fetch_operands(uop_trace);   // 立即读源操作数
  ++fu_credits_.at(iw).at((int)uop_trace->fu_type);  // 花掉一个信用
  if (uop_trace->wb) {
    scoreboard_->reserve(uop_trace);             // 占用目的寄存器
  }
  ...
}
```
出处 [sim/simx/core.cpp:596-605](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L596-L605)。**信用（credit）机制**值得一提：信用在发射时花掉（`++`），在功能单元真正接收指令时归还（`--`），见 [sim/simx/core.cpp:666-669](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L666-L669)。这与 RTL 的记分板口径一致——它统计的是「还在操作数收集阶段、尚未抵达队列的在途指令数」，而非「已经在队列里的」。

#### 4.1.4 代码实践

**实践目标：观察 ibuffer 与 scoreboard 停顿的统计。**

1. 在 build 目录运行一个有较多寄存器依赖的小程序，并开启性能统计。例如：
   ```
   ./ci/blackbox.sh --driver=simx --app=vecadd --perf=1
   ```
   （若 `--perf` 在你的 `blackbox.sh` 版本中不可用，可改用 `make -C tests/regression run-simx` 后查看 perf 报告。）
2. 在程序结束打印的性能报告（由 `sw/runtime/common/perf.cpp` 汇总）中找到以下 core 级计数器：
   - `ibuf_stalls`：ibuffer 满导致的停顿周期数
   - `scrb_stalls`：记分板冒险导致的停顿周期数
   - `opds_stalls`：操作数收集（bank 冲突）导致的停顿周期数
3. **预期结果**：寄存器依赖密集的程序（如连续的 `a=b+c; d=a+e;`）`scrb_stalls` 会明显偏高；发射速度超过功能单元处理速度时 `ibuf_stalls` 会上升。
4. 这些计数器在 `Core::PerfStats` 中声明，见 [sim/simx/core.h:48-50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.h#L48-L50)。如果无法本地运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ibuffer 要做成「每 warp 一个」，而不是全 core 共享一个？

> **答案**：不同 warp 的指令彼此没有寄存器冒险（寄存器是每 warp 私有的），把它们分到各自的 FIFO 里，可以让调度器公平地从多个 warp 间挑指令（warp 级并行，隐藏延迟）；共享 FIFO 则会被队头的阻塞 warp 卡住整个 core。

**练习 2**：`fu_credits_` 为什么在「发射时 +1」而不是「指令到达队列时 +1」？

> **答案**：因为指令从 Operands 输入端走到 FU 队列还要经过若干拍的操作数收集。若只在到达队列时计数，发射级会在那几拍里过度发送、把队列冲爆；提前花信用相当于把「在途但未抵达」的指令也算进容量，与 RTL 记分板的口径一致。

---

### 4.2 Scoreboard：寄存器冒险检测与顺序完成

#### 4.2.1 概念说明

`Scoreboard` 是一个**状态机式的共享资源**，每个 core 一个，所有 warp 共用。它维护两套状态：

1. **`in_use_regs_`**：一个三维位掩码数组 `[warp][寄存器类型(Integer/Float)][寄存器号]`，某一位为 1 表示该寄存器正被一条在途指令占用。
2. **`owners_`**：一个映射 `寄存器全局 id → 占用它的 instr_trace_t*`，用于在停顿时告诉调用者「是谁在占用」。

它的四个核心动作构成一个完整生命周期：**`in_use`（查询）→ `reserve`（占用）→ `commit_packet`（分包完成计数）→ `release`（释放）**。

#### 4.2.2 核心流程

```
发射时刻:                    提交时刻（每个 SIMD 分包完成）:
  if in_use(uop): 拒绝发射        commit_packet(trace):
  else:                              n = ++commit_counts[reg]
     reserve(uop):                   if n == trace.num_pkts:
        in_use_regs[wid][type][idx] = 1     release(trace):
        owners[reg_id] = uop                  owners.erase(reg_id)
                                              in_use_regs[wid][type][idx] = 0
```

寄存器的全局 id 由 `(wid, type, idx)` 拼成：`reg_id = (wid << ID_BITS) | reg.id()`，其中 `reg.id()` 又把类型编进高位，见 [sim/simx/scoreboard.h:71-73](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.h#L71-L73) 与 [sim/simx/types.h:155-172](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L155-L172)。

**为什么提交时要计数（`commit_packet`）而不是写回一次就释放？** 这是本模块最精妙的一点，见下一小节的源码注释。

#### 4.2.3 源码精读

**`in_use()`** 同时检查目的寄存器和所有源寄存器。只要任一被占用，就返回 `true`（不能发射）：

```cpp
bool Scoreboard::in_use(instr_trace_t* trace) const {
  if (trace->wb) {
    if (in_use_regs_.at(trace->wid).at((int)trace->dst_reg.type).test(trace->dst_reg.idx))
      return true;
  }
  for (uint32_t i = 0; i < trace->src_regs.size(); ++i) {
    if (trace->src_regs[i].type != RegType::None) {
      if (in_use_regs_.at(trace->wid).at((int)trace->src_regs[i].type).test(trace->src_regs[i].idx))
        return true;
    }
  }
  return false;
}
```
出处 [sim/simx/scoreboard.cpp:41-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.cpp#L41-L56)。注意它对源、目的「一视同仁」地查位掩码——这正是前面说的「保守策略，同时挡 RAW/WAW/WAR」。

**`reserve()`** 把目的寄存器位置 1，并在 `owners_` 记下占用者（只对会写回的指令调用）：

```cpp
void Scoreboard::reserve(instr_trace_t* trace) {
  uint32_t reg_id = get_reg_id(trace->dst_reg, trace->wid);
  in_use_regs_.at(trace->wid).at((int)trace->dst_reg.type).set(trace->dst_reg.idx);
  assert(owners_.count(reg_id) == 0);
  owners_[reg_id] = trace;
}
```
出处 [sim/simx/scoreboard.cpp:80-86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.cpp#L80-L86)。注意断言 `owners_.count(reg_id) == 0`：因为 `in_use` 已挡住 WAW，所以 `reserve` 时该位必然是 0。

**`commit_packet()`** 是「分包完成计数」的核心，注释解释了为什么不能简单看 `eop`：

```cpp
bool Scoreboard::commit_packet(instr_trace_t* trace) {
  uint32_t reg_id = get_reg_id(trace->dst_reg, trace->wid);
  auto& n = commit_counts_[reg_id];
  ++n;
  if (n >= trace->num_pkts) {
    return true;   // 所有分包都写回了，可以 release
  }
  return false;
}
```
出处 [sim/simx/scoreboard.cpp:145-154](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.cpp#L145-L154)。`commit_counts_` 的语义在头文件注释里讲得很清楚：Dispatcher 的 `eop` 标志的是「最后一个**派发**出去的分包」，但**缓存响应可能乱序到达**，导致 `eop` 分包反而先到 commit 级。若在 `eop` 时就释放目的寄存器，下一条依赖指令就会读到还没写回的那些线程的**旧值**。所以记分板改用**计数提交**——每收到一个分包的写回就 +1，直到等于 `num_pkts` 才释放。见 [sim/simx/scoreboard.h:59-65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.h#L59-L65)。

**`release()`** 清除位掩码并抹掉 owner（同时处理一种 RTU 光追回调陷阱的边角情况 `pending_reserve_`，此处可暂略，它属于 u10 光追专题）：

```cpp
void Scoreboard::release(instr_trace_t* trace) {
  ...
  owners_.erase(reg_id);
  commit_counts_.erase(reg_id);
  // ... pending_reserve_ 的 RTU 移交逻辑 ...
  in_use_regs_.at(trace->wid).at((int)trace->dst_reg.type).reset(trace->dst_reg.idx);
}
```
出处 [sim/simx/scoreboard.cpp:88-106](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.cpp#L88-L106)。

`commit()` 中的调用顺序印证了这条生命周期（每个分包写回一次）：

```cpp
if (trace->wb) {
  operands_.at(iw)->writeback(trace);              // 先把结果写进寄存器堆
  if (scoreboard_->commit_packet(trace)) {          // 再计数
    scoreboard_->release(trace);                    // 全部分包到齐才释放
  }
}
```
出处 [sim/simx/core.cpp:734-739](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L734-L739)。

#### 4.2.4 代码实践

**实践目标：用 trace 证实「乱序完成」与「计数释放」的存在。**

1. 阅读本模块源码，回答：`commit_packet` 依赖的 `trace->num_pkts` 是在哪里被算出来的？（提示：见 4.4 节 Dispatcher 的 `sop` 时刻。）
2. 用 `--debug=3`（或在 `tests/regression` 下 `make run-simx DEBUG=3`）运行 `tests/regression/demo`，在 trace 里搜索 `scoreboard-stall`：
   ```
   grep "scoreboard-stall" trace.txt
   ```
3. **预期结果**：当出现一条 `*** scoreboard-stall: dependents={...}` 时，对照其 `dependents` 列表，能看到是哪条在途指令（`#uuid`）占用了它需要的寄存器。找一对有 RAW 依赖的相邻指令，确认后一条确实等前一条 `commit` 之后才发射。
4. 若无法本地生成 trace，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假如把 `commit_packet` 改成「写回一次就立即 release」，会在什么场景下出错？

> **答案**：当一条 warp 指令被 Dispatcher 拆成多个 SIMD 分包、且缓存响应乱序返回时，`eop` 分包可能先于其它分包写回。若此时就释放目的寄存器，紧随其后的依赖指令会发射并读到「部分线程还没更新」的旧值，产生错误结果。

**练习 2**：`in_use()` 检查源寄存器和目的寄存器用的是同一套位掩码。这会挡住哪种冒险？是否过于保守？

> **答案**：同一套位掩码意味着「只要某寄存器正被在途指令占用（不论读写），后续任何使用都被挡」，从而同时挡住 RAW、WAW、WAR。这是保守的（尤其 WAR 在乱序机里其实可放宽），但 Vortex 是顺序发射+顺序完成的简化模型，保守换来了正确性与和 RTL 的一致性。

---

### 4.3 Operands / OpcUnit：每 warp 寄存器堆与操作数收集

#### 4.3.1 概念说明

`Operands` 是「操作数收集」级，**每发射通道（issue lane）一个实例**。它的真正职责其实落在内部的 `OpcUnit`（Operand Collector Unit，操作数收集器）上：

- **`OpcUnit` 持有真正的寄存器堆。** 整个 SimX core 里「寄存器的实际数值」只存在 `OpcUnit` 的 `regs_` 里。每个 warp 在它所属的 OpcUnit 里占一个 slot，slot 内是 `[寄存器号][线程号]` 的二维数组（整型 `ireg_file` 和浮点 `freg_file`）。
- **`Operands` 是 OpcUnit 的「门面 + 路由器」。** 它对外只暴露 `fetch_operands`（读源）和 `writeback`（写目的），内部根据 `wid` 把请求路由到正确的 OpcUnit 与 slot。

之所以拆出多个 OpcUnit，是为了**分 bank 并行读**：当 warp 很多时，单端口寄存器堆会成为吞吐瓶颈，于是把 warp 散到多个 OpcUnit，每个独立持有自己那批 warp 的寄存器，可并行收集操作数。

#### 4.3.2 核心流程

**warp → OpcUnit/slot 的路由**（这是本模块的数学骨架）。给定一个全局 `wid`：

```
lane = wid % VX_CFG_ISSUE_WIDTH     → 选哪个 Operands 实例（即哪个发射通道）
wis  = wid / VX_CFG_ISSUE_WIDTH     → warp 在该通道内的序号
opc  = wis % VX_CFG_NUM_OPCS        → 该通道内哪个 OpcUnit
slot = wis / VX_CFG_NUM_OPCS        → 该 OpcUnit 内的本地 slot
```

这套路由公式在 [sim/simx/opc_unit.h:22-28](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.h#L22-L28) 有完整注释。代码里它被拆成两份：`Operands`（`operands.cpp`）算 `opc_idx` 与 `slot` 来分发请求，`OpcUnit` 用 `wid_to_opc_slot` 反算 slot 来定位寄存器堆。

```
发射时刻 fetch_operands(trace):
  opc = opc_units_[opc_idx(trace.wid)]
  for 每个源寄存器 i:
     opc.read_src(trace.src_data[i], trace.wid, i, trace.src_regs[i])
     // 把 [reg][0..NUM_THREADS] 整列复制进 trace.src_data[i]

提交时刻 writeback(trace):
  opc.writeback(trace, trace.wid)
  // 按 trace.tmask，把 trace.dst_data[t] 写回 [reg][t]
  // 支持 bytesel：部分宽度写用 OR-merge
```

#### 4.3.3 源码精读

**寄存器堆的真实结构**——这是整个 core 唯一的「寄存器实体」：

```cpp
struct warp_regs_t {
  std::vector<std::vector<Word>>     ireg_file;   // [reg][thread]
  std::vector<std::vector<uint64_t>> freg_file;   // [reg][thread]
  ...
};
std::vector<warp_regs_t> regs_;     // 按 warp_slot 索引
```
出处 [sim/simx/opc_unit.h:64-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.h#L64-L74)。注意浮点寄存器是 64 位（用于 NaN-boxing 的单精度浮点），整型是 `Word`（`XLEN` 位）。

**`read_src`** 把一个源寄存器在所有线程上的值整列读出：

```cpp
void OpcUnit::read_src(std::vector<reg_data_t>& out, uint32_t wid,
                       uint32_t src_index, const RegOpd& reg) const {
  const auto& slot = regs_[wid_to_opc_slot(wid)];
  switch (reg.type) {
  case RegType::Integer: {
    const auto& src = slot.ireg_file[reg.idx];
    for (uint32_t t = 0; t < num_threads_; ++t) out[t].u = src[t];   // 整列复制
  } break;
  case RegType::Float: {
    const auto& src = slot.freg_file[reg.idx];
    for (uint32_t t = 0; t < num_threads_; ++t) out[t].u64 = src[t];
  } break;
  ...
  }
}
```
出处 [sim/simx/opc_unit.cpp:101-125](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp#L101-L125)。`Operands::fetch_operands` 对每个源寄存器循环调用它，结果填进 `trace->src_data[i]`，见 [sim/simx/operands.cpp:166-181](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.cpp#L166-L181)。

**`writeback`** 按 `tmask` 写回，并支持 **bytesel 部分写**（pack load 等只写半个字/字节的指令用）：

```cpp
uint64_t mask = expand_bytesel64(trace->dst_bytesel);  // 默认 0xFF → 全写
// Integer 分支（节选）：
Word cur = bank.at(t);
Word incoming = trace->dst_data[t].i;
bank.at(t) = (cur & ~Word(mask)) | (incoming & Word(mask));   // OR-merge
```
出处 [sim/simx/opc_unit.cpp:137-190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp#L137-L190)。`(cur & ~mask) | (incoming & mask)` 是经典的「按字节选择合并」：mask 为 1 的字节用新值，为 0 的字节保留旧值。`expand_bytesel64` 把 8 位的字节选择掩码展成 64 位每字节 0xFF 的掩码，见 [sim/simx/opc_unit.cpp:129-135](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp#L129-L135)。

**多 OpcUnit 的接线**：`Operands` 构造时按 `VX_CFG_NUM_OPCS` 创建若干 OpcUnit，并把它们的输出经一个轮转仲裁器（`rsp_arb_`）汇成单端输出；当 `NUM_OPCS < 2` 时走直通（pass-thru）：

```cpp
if (VX_CFG_NUM_OPCS >= 2) {
  rsp_arb_ = TraceArbiter::Create(..., ArbiterType::RoundRobin, VX_CFG_NUM_OPCS, 1);
  for (...) opc_units_.at(i)->Output.bind(&rsp_arb_->Inputs.at(i));
  rsp_arb_->Outputs.at(0).bind(&this->Output);
} else {
  this->Input.bind(&opc_units_.at(0)->Input);   // 直通
  opc_units_.at(0)->Output.bind(&this->Output);
}
```
出处 [sim/simx/operands.cpp:110-122](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.cpp#L110-L122)。默认配置（`NUM_OPCS=1`）就走 `else` 分支。

**OpcUnit 自身的流水线节拍**：它用一个「保持槽」`cur_trace_` + `release_cycle_` 模拟操作数收集的延迟，并统计 **bank 冲突停顿**：

```cpp
void OpcUnit::on_tick() {
  // 1. 到点了就把持有的 uop 往下游送
  if (cur_trace_ != nullptr && cur_cycle >= release_cycle_) {
    if (!Output.try_send(cur_trace_, 1)) return;
    cur_trace_ = nullptr;
  }
  // 2. 接收下一个 uop 进保持槽，计算 bank 冲突停顿
  if (cur_trace_ == nullptr && !Input.empty()) {
    auto trace = Input.peek();
    uint32_t stalls = compute_bank_conflicts(trace);  // 源寄存器同 bank 则 +1 拍
    cur_trace_ = trace;
    release_cycle_ = cur_cycle + 1 + stalls;
    Input.pop();
  }
}
```
出处 [sim/simx/opc_unit.cpp:79-99](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp#L79-L99)。bank 冲突逻辑：若一条指令的两个源寄存器落在同一 bank（按 `idx % VX_CFG_NUM_GPR_BANKS`），就多耗一拍，见 [sim/simx/opc_unit.cpp:60-77](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp#L60-L77)。这正是 `opds_stalls` 性能计数器的来源。

> 一个容易踩的坑：`Operands::fetch_operands` 是在 `issue()` 里**直接同步调用**的（见 4.1.3），而 `writeback` 是在 `commit()` 里同步调用的。`OpcUnit::on_tick` 里的「保持槽 + 延迟」模拟的是 **trace 流出 Operands 之前**的那段收集延迟，与「读/写寄存器堆本身」是两回事——读写是当拍完成的，延迟只影响 trace 何时被允许进入下游 Dispatcher。

#### 4.3.4 代码实践

**实践目标：定位寄存器堆的「唯一真身」。**

1. 在仓库里全局搜索 `ireg_file` 与 `freg_file`，确认它们**只**在 `OpcUnit` 内定义、没有第二份寄存器堆副本。这能让你建立「SimX 的寄存器实体唯一存在于 OpcUnit」的心智模型。
2. 阅读 [sim/simx/operands.cpp:160-164](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.cpp#L160-L164) 的 `get_exit_code()`：它从 `opc_units_[0]` 的 slot 0、寄存器 `x3`、线程 0 读出程序退出码（RISC-V 测试约定）。这印证了「退出码就是寄存器堆里的一个值」。
3. **预期结果**：你能用一句话回答「Vortex SimX 里 `a0` 寄存器的值物理上存在哪里」——存在该 warp 所属 OpcUnit 的 `regs_[slot].ireg_file[10][thread]`。

#### 4.3.5 小练习与答案

**练习 1**：为什么浮点寄存器堆是 64 位（`uint64_t`），而整型是 `Word`？

> **答案**：RISC-V 用 NaN-boxing 在 64 位浮点寄存器里承载单精度（32 位）浮点数——高 32 位全 1 标记「这是一个单精度值」。所以即使 `XLEN=32`，浮点寄存器也需 64 位宽。trace 打印里 `(values[t].u64 >> 32) == 0xffffffff` 的判断就是在识别这种 box 模式，见 [sim/simx/operands.cpp:72-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.cpp#L72-L78)。

**练习 2**：bytesel 写回的 `(cur & ~mask) | (incoming & mask)` 对一条普通 `add`（`dst_bytesel=0xFF`）会退化成什么？

> **答案**：`mask = 0x...FF`（全 1），`cur & ~mask = 0`，`incoming & mask = incoming`，于是 `bank[t] = incoming`，即整字覆盖写。只有 pack load 这类部分宽度写才会带非 `0xFF` 的 bytesel，触发真正的合并。

---

### 4.4 Dispatcher：SIMD 分包与派发到功能单元

#### 4.4.1 概念说明

`Dispatcher`（分发器）夹在 Operands 和功能单元之间，**每类功能单元一个**（ALU/FPU/LSU/SFU/TCU 各一个）。它解决两个「宽度不匹配」的问题：

1. **发射通道数 ≠ 功能单元物理块数。** 发射是按 `VX_CFG_ISSUE_WIDTH` 个通道进行的，但一个 FU 可能只有 `NUM_*_BLOCKS` 个物理执行块（如 ALU 默认 1 块）。Dispatcher 把多个发射通道的请求**汇聚（aggregate）**到数量更少的物理块输入上。
2. **一条 warp 指令的活跃线程 ≠ 一个物理块一次能处理的线程。** 一个物理块有 `num_lanes` 条 SIMD 通道（如 ALU 默认 `SIMD_WIDTH=NUM_THREADS`），若一条指令的活跃线程（`tmask`）跨多个「lane 组」，就需要拆成多个 **packet** 分批送入。

#### 4.4.2 核心流程

```
on_tick() 每拍:
  for b in 当前批次 batch_idx 的各物理块 block_size_:
    if 输入 Inputs[batch*block_size + b] 空: 跳过
    if 输出 Outputs[b] 满: 跳过 (背压)
    trace = peek 输入
    if num_packets_ == 1:                # 不需拆分，整条送出
       pop; output.send(trace)
    else:                                # 需按 lane 组拆分
       算出下一个 packet 的 [start, end] lane 组区间
       if start != end:                  # 本 lane 组只是中间一段
          new_trace = 拷贝 trace
          设 block_pid = start+1         # 下拍接着发下一组
       else:                             # 最后一组
          标记 block_pid = -1; pop 输入
       new_trace.tmask = 仅本 lane 组的位
       new_trace.pid = start
       new_trace.sop = (首组)
       new_trace.eop = (末组)
       output.send(new_trace)
  若本批次所有 block 都处理完: 切换到下一批次 (轮转)
```

**关键产出：`num_pkts`。** 在某个 trace 第一次被处理（`block_pid==0`，即 `sop`）时，Dispatcher 扫一遍 `tmask`，数出「有多少个 lane 组是活跃的」，写入 `trace->num_pkts`。这个值随后被 Scoreboard 的 `commit_packet` 用来判断「所有分包是否都写回了」（见 4.2）。

#### 4.4.3 源码精读

**构造**：输入按 `VX_CFG_ISSUE_WIDTH` 个通道建，输出按 `block_size_`（=该 FU 的 `NUM_*_BLOCKS`）个物理块建，队列深 `VX_CFG_DISPATCH_QUEUE_SIZE`：

```cpp
Dispatcher::Dispatcher(..., uint32_t buf_size, uint32_t block_size, uint32_t num_lanes)
  : Inputs(VX_CFG_ISSUE_WIDTH, this)
  , Outputs(block_size, SimChannel<instr_trace_t*>(this, buf_size))
  , block_size_(block_size)
  , num_lanes_(num_lanes)
  , num_blocks_(VX_CFG_ISSUE_WIDTH / block_size)
  , num_packets_(VX_CFG_NUM_THREADS / num_lanes)
  ...
```
出处 [sim/simx/dispatcher.cpp:19-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L19-L32)。四类 FU 的创建参数（块数、lane 数）见 [sim/simx/core.cpp:225-232](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L225-L232)，例如 ALU 是 `block_size=VX_CFG_NUM_ALU_BLOCKS=1, num_lanes=VX_CFG_NUM_ALU_LANES=SIMD_WIDTH`。

**`num_pkts` 的计算**（`sop` 时刻），注释解释了稀疏发散 tmask 的处理：

```cpp
if (block_pid == 0) {
  uint32_t n_pkts = 0;
  for (uint32_t j = 0; j < VX_CFG_NUM_THREADS; j += num_lanes_) {
    for (uint32_t k = 0; k < num_lanes_; ++k) {
      if (trace->tmask.test(j + k)) { ++n_pkts; break; }  // 该 lane 组有任一活跃线程 → 一个包
    }
  }
  trace->num_pkts = n_pkts == 0 ? 1 : n_pkts;
}
```
出处 [sim/simx/dispatcher.cpp:79-87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L79-L87)。整段拆分与 `sop/eop` 设置见 [sim/simx/dispatcher.cpp:88-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L88-L124)。

**Dispatcher → FU 的衔接**由 core 的 `execute()` 完成一一转发，并在 FU 接收时归还信用：

```cpp
for (uint32_t fu = 0; fu < (uint32_t)FUType::Count; ++fu) {
  for (uint32_t b = 0; b < nb; ++b) {
    auto trace = dispatch->Outputs.at(b).peek();
    if (func_unit->input(b).try_send(trace)) {
      dispatch->Outputs.at(b).pop();
      uint32_t iw = trace->wid % VX_CFG_ISSUE_WIDTH;
      if (fu_credits_.at(iw).at(fu) > 0)
        --fu_credits_.at(iw).at(fu);   // FU 接收 → 归还信用
    }
  }
}
```
出处 [sim/simx/core.cpp:653-669](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L653-L669)。FuncUnit 的 `input(b)` / `output(b)` 抽象接口定义在 [sim/simx/func_unit.h:26-58](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L26-L58)，它用类型擦除（`FuncUnitBase`）让 core 能把不同 `NUM_BLOCKS` 的 FU 放进同一容器。

#### 4.4.4 代码实践

**实践目标：画出 Issue 级的完整数据通路。**

阅读以下调用点后，画一张从 ibuffer 到功能单元的数据流图：

1. ibuffer 队头 trace → [core.cpp:596](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L596) 发送到 `Operands.Input`；
2. `Operands`（[operands.cpp:140](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.cpp#L140)）按 `wid` 路由到对应 `OpcUnit.Input`；
3. `OpcUnit`（[opc_unit.cpp:91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp#L91)）在保持槽停留 `1 + bank冲突` 拍后从 `Output` 送出；
4. `Operands.Output` → [core.cpp:511](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L511) 推进到 `Dispatcher.Inputs[iw]`（按 `fu_type` 选 dispatcher）；
5. `Dispatcher`（[dispatcher.cpp:43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L43)）按 lane 组拆成 packet，从 `Outputs[b]` 送出；
6. [core.cpp:664](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L664) 转发进 `FuncUnit.input(b)`。

在图上标注：每一段用到的 channel 名、`fetch_operands`/`reserve` 发生的位置、信用 `fu_credits_` 的增减位置。

**预期结果**：一张清晰的「6 跳」数据通路图，能解释「读寄存器（步骤 2-3）」与「派发到 FU（步骤 4-6）」是流水化的两段，中间由 Operands 的输出 channel 隔开。

#### 4.4.5 小练习与答案

**练习 1**：默认配置下 `num_packets_ = VX_CFG_NUM_THREADS / num_lanes = 4/4 = 1`。此时 Dispatcher 的拆分逻辑还会执行吗？

> **答案**：不会。`num_packets_ == 1` 时走 `else` 分支，直接 `pop` 并 `output.send(trace)`，不拆分（见 [dispatcher.cpp:118-122](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L118-L122)）。只有当 FU 的 lane 数小于活跃线程跨度时才拆分，这正是 `num_pkts` 在稀疏 tmask 下可能小于 `num_packets_` 的原因。

**练习 2**：Dispatcher 的输出数是 `block_size_`（物理块数），而输入数是 `VX_CFG_ISSUE_WIDTH`（发射通道数）。当 `ISSUE_WIDTH > NUM_BLOCKS` 时，多个发射通道如何映射到少量物理块？

> **答案**：Dispatcher 用 `batch_idx_` 在 `num_blocks_ = ISSUE_WIDTH / block_size` 个批次间**轮转**，每个批次只服务连续的 `block_size_` 个输入，把它们聚合到对应的 `block_size_` 个输出上（见 [dispatcher.cpp:46-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L46-L47) 与 [dispatcher.cpp:128-134](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L128-L134)）。这是一种「时分复用」的汇聚。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**源码阅读 + 数据通路绘制**的综合任务。

**任务：跟踪一条 `add x1, x2, x3` 从发射到提交的完整旅程，并解释每一步记分板状态的变化。**

操作步骤：

1. **准备假设**：假设某 warp 的 ibuffer 队头是 `add x1, x2, x3`（`fu_type=ALU, wb=true, dst_reg=x1, src_regs=[x2,x3,None]`），且 `x2`、`x3` 当前都没被占用，但该 warp 此前有一条 `ld x4, ...` 还没退休（占用 `x4`，但与本指令无关）。
2. **走查关卡**：按 4.1 的流程，确认这条 `add` 能通过 `scoreboard.in_use`（因为只查 `x1/x2/x3`，不查 `x4`）、能进入 `ready_set`、能被仲裁选中。
3. **发射三连**：在 [core.cpp:596-605](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L596-L605) 标注：`fetch_operands` 读了 `x2/x3` 的值进 `src_data`；`reserve` 把 `x1` 位置 1，`owners_[x1的reg_id] = add这条trace`。
4. **走完通路**：按 4.4.4 的图，trace 经 Operands → Dispatcher → ALU → commit arbiter 回到 commit 级。
5. **提交释放**：在 [core.cpp:734-739](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L734-L739) 标注：`writeback` 把 `dst_data` 写进 `ireg_file[1][t]`（按 tmask）；因为 ALU 默认 `num_packets_=1`，`commit_packet` 第一次就返回 `true`，于是 `release` 把 `x1` 位清 0、`owners_` 抹掉 `add`。
6. **画图交付**：画出这条 `add` 的时间线，横轴是周期，标注「`x1` 在记分板中被占用」的区间（从 `reserve` 到 `release`）。

**预期结果**：你能清楚说出「目的寄存器 `x1` 从发射那一刻起被锁住，直到这条 `add` 在 commit 级写回完成才解锁；在此期间任何要用 `x1` 的后续指令都会被 `in_use` 挡住」。

## 6. 本讲小结

- **Issue 级是一道多关卡门**：ibuffer 队头指令要依次通过 scoreboard 冒险检测、FU lock、派发信用三关，全过才能被仲裁器选中发射，见 [core.cpp:516-651](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L516-L651)。
- **Scoreboard 用位掩码保守地挡住 RAW/WAW/WAR**：只要某寄存器正被在途指令占用（不论读写），后续使用一律停，见 [scoreboard.cpp:41-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.cpp#L41-L56)。
- **顺序完成靠「分包计数」而非 `eop`**：因缓存响应乱序，Scoreboard 必须等所有 SIMD 分包都写回（`commit_packet` 计数到 `num_pkts`）才释放目的寄存器，见 [scoreboard.cpp:145-154](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scoreboard.cpp#L145-L154)。
- **寄存器堆的唯一真身在 OpcUnit**：`regs_[slot].ireg_file/freg_file` 是 `[reg][thread]` 二维数组，`Operands` 只是按 `wid` 路由的门面，见 [opc_unit.h:64-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.h#L64-L74)。
- **Dispatcher 解决两重宽度失配**：把 `ISSUE_WIDTH` 个通道汇聚到 `NUM_*_BLOCKS` 个物理块，并把一条指令按 lane 组拆成多个 packet，同时算出 `num_pkts` 供记分板使用，见 [dispatcher.cpp:43-135](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L43-L135)。
- **发射三连的原子性**：`fetch_operands`（读源）、`reserve`（占目的）、`advance`（推进微操作）在同一拍完成，保证了读到的源值与锁住的目的寄存器对同一条 uop 而言是一致的。

## 7. 下一步学习建议

- **下一讲 u6-l4（功能单元 ALU/FPU/LSU/SFU）** 将接住 Dispatcher 送出的 packet，讲解 `FuncUnit<NUM_BLOCKS>` 这套 CRTP 基类与各单元私有 `execute()` 如何承载 ISA 语义，构成流水线的 Execute 级。
- **commit 级的另一半**：本讲提到了 `commit()` 里的写回与释放，但 retire 计数、warp 释放、`resume_warp` 等控制流的完整细节散落在 [core.cpp:687-766](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L687-L766)，建议在学完 u6-l4 后回读一遍。
- **对照 RTL**：u7 单元会讲 `VX_scoreboard.sv`、`VX_ibuffer.sv` 等 RTL 实现。带着本讲建立的「位掩码冒险检测 + 分包计数释放」模型去看 RTL，会非常容易对应——这也是 Vortex 强制的 SimX↔RTL model_parity 在这一级的具体体现。
