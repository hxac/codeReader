# 多核与 L2 仲裁

## 1. 本讲目标

本讲把视角从「单核内部」拉到「核与核之间」。Nyuzi 是一个可参数化的多核处理器：把 `config.svh` 里的 `NUM_CORES` 从默认的 1 调大，顶层就会实例化出多个完全相同的核，它们**共享同一个 L2 缓存**。读完本讲，你应当能够：

- 说清顶层 [nyuzi.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv) 如何用一个 `generate` 循环实例化多个核、如何给每个核分配唯一的 `CORE_ID`，以及核间信号（L2 请求、`thread_en`、调试数据）如何用「数组 + 位切片」组织。
- 解释 `l2_cache_arb_stage` 如何在多个核同时向共享 L2 发请求时做**公平仲裁**：掌握 `rr_arbiter` 的轮询原理，以及「缺失回填的 restarted 请求优先于新请求」这条规则为何能防止 miss 队列堆积。
- 理解 L2 只有一条**单点响应总线**，却要服务所有核：响应被**广播**给每个核，再由每个核用 `core == CORE_ID` 过滤出属于自己的那一份，同时所有核都会 **snoop**（监听）DCache 响应以维护缓存一致性。
- 说清「把 `NUM_CORES` 改成多核」需要哪些配套修改，以及为何 `tests/core/multicore` 这个测试**必须在改完配置后重建硬件模型**才能跑。

本讲是 u3-l1（顶层 nyuzi）与 u6-l3（L2 缓存四阶段流水线）在多核维度上的合流，也直接承接 u10-l2 末尾留下的伏笔：那里的 `thread_en` 已经是 `TOTAL_THREADS`（全核）位宽，本讲就回答「多个核如何共享同一个 L2、如何被公平调度」。

## 2. 前置知识

进入源码前，先用直觉建立三个概念。

**(1) 共享资源与仲裁（arbitration）。**
单核时，一个核独占 L2，请求什么时候到、L2 就什么时候处理。多核时，多个核可能在同一拍都向 L2 发请求，而 L2 一个周期只能处理一条（它是一条流水线，入口宽度为 1）。于是需要一个**仲裁器（arbiter）**：每拍从所有「正在请求」的核里挑出一个放行，其余的等下一拍。「公平」的要求是——不能让某个核饿死（starvation），所以最常用的策略是**轮询（round-robin）**：这一拍选了核 A，下一拍就优先考虑 A 之后的核。

**(2) 广播（broadcast）与过滤（filter）。**
L2 处理完一条请求后要回一个响应（「数据取回来了」/「store 已生效」/「flush 完成」）。但 L2 只有一组响应信号。多核下，硬件没有给每个核单独拉一组响应线，而是：L2 把**每一条响应都广播给所有核**，响应包里带一个 `core` 字段标记「这是给谁的」；每个核自己判断 `response.core == 自己的 CORE_ID`，是自己的才认领，不是就忽略。这就像办公室只有一个广播喇叭，每次喊人时先报工号，只有工号对上的人才应答——省线，代价是每个核都要听一遍。

**(3) 核的身份与全局线程号。**
多核后，「线程号」必须升级为「全局线程号」才能唯一。Nyuzi 的约定是：读控制寄存器 `CR_THREAD_ID`（软件宏 `CR_CURRENT_THREAD`，编号 0）得到一个 32 位值，**高位拼核号、低位拼核内线程号**。于是 8 核 × 4 线程 = 32 个全局线程，编号 0~31 唯一。LL/SC 的链接状态、性能计数、多核测试里「该谁打印下一个字符」都靠这个全局编号来区分。

> 相关类型见 [defines.svh:44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L44)：`TOTAL_THREADS = THREADS_PER_CORE * NUM_CORES`。核号类型 `core_id_t` 见 [defines.svh:65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L65)。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `hardware/core/nyuzi.sv` | **顶层**。用 `generate` 循环实例化 `NUM_CORES` 个核，实例化唯一的 `l2_cache`/`io_interconnect`/`on_chip_debugger`，把各核的 suspend/resume 脉冲聚合成全局 `thread_en`。本讲主角之一。 |
| `hardware/core/l2_cache_arb_stage.sv` | **L2 仲裁级**。L2 四阶段流水线的第一级：在「各核新请求」与「AXI 回填的 restarted 请求」之间选择，多核时用 `rr_arbiter` 公平仲裁。本讲主角之二。 |
| `hardware/core/rr_arbiter.sv` | **轮询仲裁器**。纯组合给出 `grant_oh`，靠 `priority_oh` 指针轮转保证公平。被 L2 仲裁级、IO 互连、`thread_select_stage` 复用。 |
| `hardware/core/l2_cache.sv` | **L2 外壳**。声明唯一的单点响应端口 `l2_response`，把四阶段流水线 + AXI 接口组装起来。 |
| `hardware/core/l1_l2_interface.sv` | **响应消费者**。每个核一个实例（带 `CORE_ID` 参数），用 `ack_for_me = response.core == CORE_ID` 过滤广播响应，并产生 snoop 信号。 |
| `hardware/core/l2_cache_read_stage.sv`（辅助） | 用 `{core, id}` 拼出全局线程号作为 LL/SC 同步槽位。 |
| `hardware/core/defines.svh` / `config.svh` | `NUM_CORES`、`TOTAL_THREADS`、`core_id_t`、L2 请求/响应包结构。 |
| `tests/core/multicore/multicore.S` + `runtest.py` | **多核功能测试**。要求 8 核配置，验证 32 个全局线程按共享计数器轮转各打印一个字符。 |

---

## 4. 核心概念与源码讲解

### 4.1 多核实例化与核的身份

#### 4.1.1 概念说明

多核的「多」从哪里来？答案出乎意料地简单：**全靠一个 `generate` 循环**。顶层 `nyuzi` 不为每个核单独写一份实例化代码，而是用一个 `for` 循环把同一个 `core` 模块实例化 `NUM_CORES` 次，每次把循环变量当作这个核的 `CORE_ID` 传进去。于是「核 0」「核 1」……「核 N-1」在 RTL 里就是同一个模块的 N 份拷贝，唯一的区别是它们各自携带的 `CORE_ID` 参数不同。

`NUM_CORES` 是 `config.svh` 里的编译期宏（默认 1，见 [config.svh:40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40)）。这意味着：**核数在「综合/编译时」就烧死了**，运行时不能改。改核数 = 改 `config.svh` + 重新 `make`。这是后面「为何要重建硬件模型」的根因。

多核引入了一个单核不存在的约束：多个核的信号要在顶层「收拢」成数组。Nyuzi 的做法是把所有「每核一份」的信号声明成**以核号为下标的数组**：

- `l2i_request[NUM_CORES]`：每个核发往 L2 的请求包，`l2i_request[k]` 是核 k 的请求。
- `l2i_request_valid[NUM_CORES-1:0]`：每个核的请求有效位，拼成一个位图（第 k 位 = 核 k 有请求）。
- `l2_ready[NUM_CORES]`：L2 反向告诉每个核「你这条请求我收下了」。
- `thread_en`：例外——它是 `TOTAL_THREADS` 位（全核所有线程），不是按核数组，而是按「核 × 核内线程」线性排布，下发时按核切片（见 4.1.3）。

每个核怎么知道「我是几号」？靠 `CORE_ID` 参数。它在核内部被用来：给自己的 L2 请求打上 `core = CORE_ID` 标记（这样响应才能路由回来）、在响应里过滤 `core == CORE_ID`、读 `CR_THREAD_ID` 时把核号拼进高位、让调试器判断「现在调试的是不是我」。

#### 4.1.2 核心流程

```
                 NUM_CORES（config.svh 编译期宏，默认 1）
                              │
                              ▼
   nyuzi.sv: genvar core_idx; generate for core_idx in 0..NUM_CORES-1
                              │
        ┌─────────────────────┴──────────────────────┐
        ▼                                            ▼
  core #(.CORE_ID(core_idx))               每核独立的：
   - l2i_request[core_idx]  ──► (数组)     - 寄存器组、PC、指令 FIFO
   - l2i_request_valid[core_idx]           - L1 I/D Cache、TLB
   - l2_ready[core_idx]      ◄── (数组)    - 整条流水线、记分牌
   - thread_en[核 k 的那一段]               - 自己的 control_registers
        │                                            │
        └──────────────┬─────────────────────────────┘
                       ▼
              所有核的请求汇入「唯一的 l2_cache」（共享）
              所有核的响应来自「唯一的 l2_response」（广播，见 4.3）
```

关键点：**核是私有的，L2 是共享的**。每个核有自己全套的 L1 缓存、TLB、流水线、寄存器；但 L2 缓存全局只有一个，被所有核共享。这正是「多核要仲裁」的原因——共享入口只有一条。

#### 4.1.3 源码精读

**核心实例化循环**见 [nyuzi.sv:115-139](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L115-L139)：

```systemverilog
genvar core_idx;
generate
    for (core_idx = 0; core_idx < `NUM_CORES; core_idx++)
    begin : core_gen
        core #(
            .CORE_ID(core_id_t'(core_idx)),   // 每核唯一身份
            .NUM_INTERRUPTS(NUM_INTERRUPTS),
            .RESET_PC(RESET_PC)
        ) core(
            .l2i_request_valid(l2i_request_valid[core_idx]),  // 数组下标 = 核号
            .l2i_request(l2i_request[core_idx]),
            .l2_ready(l2_ready[core_idx]),
            .thread_en(thread_en[core_idx * `THREADS_PER_CORE+:`THREADS_PER_CORE]),
            ...
            .*);
    end
endgenerate
```

逐行解读这一段是理解多核的关键：

- `.CORE_ID(core_id_t'(core_idx))`（[nyuzi.sv:120](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L120)）：循环变量 `core_idx` 被强转成 `core_id_t` 后作为参数传入，于是核 0 拿到 `CORE_ID=0`、核 1 拿到 `CORE_ID=1`。这个参数会一路传给核内的 `l1_l2_interface`、`io_request_queue`、`control_registers`（见 [core.sv:380-391](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L380-L391)），让每个子模块都知道「我属于哪个核」。
- `.l2i_request(l2i_request[core_idx])`：核 k 的请求接到请求数组的第 k 项。`l2i_request` 声明为 `l2req_packet_t l2i_request[\`NUM_CORES]`（[nyuzi.sv:37](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L37)）。`l2i_request_valid` 是位图 `logic[\`NUM_CORES - 1:0]`（[nyuzi.sv:38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L38)）——后面仲裁器正是吃这个位图。
- `.thread_en(thread_en[core_idx * \`THREADS_PER_CORE+:\`THREADS_PER_CORE])`（[nyuzi.sv:127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L127)）：`thread_en` 是全核 `TOTAL_THREADS` 位的线（[nyuzi.sv:41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L41)），这里用 `+:` 位切片把「属于核 k 的那 `THREADS_PER_CORE` 位」切给核 k。这是 u10-l2 讲过的 `thread_en` 聚合的下发端。

**唯一的共享 L2 与 IO 互连**在循环之外实例化，全局只有一份，见 [nyuzi.sv:96-100](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L96-L100)：`l2_cache l2_cache(.*)` 与 `io_interconnect io_interconnect(.*)`。它们通过 `.*` 自动连上顶层的 `l2i_request_valid`/`l2i_request`/`l2_ready` 数组以及唯一的 `l2_response` 线（[nyuzi.sv:57-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L57-L58)）。

**`thread_en` 的跨核聚合**完全沿用 u10-l2 的设计，见 [nyuzi.sv:77-94](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L77-L94)：一个 `for` 循环把各核的 `core_suspend_thread[i]` / `core_resume_thread[i]`（都是 `TOTAL_THREADS` 宽）按位或成全局掩码，再按 `thread_en <= (thread_en | thread_resume_mask) & ~thread_suspend_mask` 维护。注意这两个数组声明为 `logic[\`NUM_CORES - 1:0][TOTAL_THREADS - 1:0]`（[nyuzi.sv:46-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L46-L47)）——每个核都能看到全部线程的位图（因为软件一次 `CR_RESUME_THREAD` 写入的值是全核位图），所以即便单核也能正确处理。

**核数的合法性断言**见 [nyuzi.sv:71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L71)：`assert(\`NUM_CORES >= 1 && \`NUM_CORES <= (1 << CORE_ID_WIDTH))`。这里的 `CORE_ID_WIDTH` 见 [defines.svh:55](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L55)，但要注意一个反直觉细节：`core_id_t` 被硬编码为 `logic[3:0]`（4 位，最多 16 核），见 [defines.svh:57-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L57-L65)——注释说明这是为了绕开「单核时 `CORE_ID_WIDTH` 为 0、导致 `[−1:0]` 非法范围」的综合工具 bug。所以要综合超过 16 核，必须手动加宽 `core_id_t`。`config.svh` 的约束注释也写明「NUM_CORES must be 1-16」，见 [config.svh:31-32](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L31-L32)。

**核号如何被软件读到**：`CR_THREAD_ID`（编号 0）的读值是 `{CORE_ID, dt_thread_idx}`——高位核号、低位核内线程号，见 [control_registers.sv:286](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L286)。软件宏 `CR_CURRENT_THREAD` 就等于 0（[asm_macros.h:25](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L25)）。多核测试正是靠它区分「我是第几号全局线程」。

> 关于「核号位数」的小结：单核时 `CORE_ID_WIDTH = $clog2(1) = 0`，但 `core_id_t` 仍是 4 位物理宽度，所以 `0` 号核的 `CORE_ID` 用 0 填充即可；多核时核号落在低若干位。仲裁级里用 `grant_idx[CORE_ID_WIDTH - 1:0]` 切片（[l2_cache_arb_stage.sv:82-84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L82-L84)）就是利用这个宽度。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「多核 = generate 循环 + CORE_ID 参数 + 数组信号」，并理解 `thread_en` 如何在多核下切片。

**步骤**：

1. 打开 [nyuzi.sv:115-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L115-L139)，确认 `core` 模块被实例化 `NUM_CORES` 次，唯一差异是 `.CORE_ID(core_id_t'(core_idx))`。
2. 在 [nyuzi.sv:37-41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L37-L41) 数出哪些信号是「按核数组」（`l2i_request`、`l2i_request_valid`、`ior_request`、`l2_ready`、`ii_ready`、`cr_data_to_host`），哪些是「全核位图」（`thread_en`）。
3. 在 [nyuzi.sv:127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L127) 确认 `thread_en` 的 `+:` 切片：设 `THREADS_PER_CORE=4`，则核 0 拿到 `thread_en[3:0]`、核 1 拿到 `thread_en[7:4]`、……、核 7 拿到 `thread_en[31:28]`。

**需要观察的现象 / 预期结果**：你能画出一个表，把 `NUM_CORES=8` 时 32 位 `thread_en` 的每 4 位分属哪个核说清楚；并理解为何 `l2i_request` 必须是数组（每个核一份请求包）而 `l2_response` 不是数组（只有一条广播响应线，见 4.3）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `l2i_request` 是「以核号为下标的数组」，而 `l2_response` 却只有「单条线」？
**参考答案**：请求方向是「N 个生产者 → 1 个消费者（L2）」，每个核要能独立持有自己的请求包，所以用数组收拢 N 份；响应方向是「1 个生产者（L2）→ N 个消费者」，L2 一拍只回一个响应，用单条线广播、各核按 `core` 字段过滤即可，不需要 N 条线（那会浪费布线，且 L2 一拍也产不出 N 个响应）。

**练习 2**：若要把 Nyuzi 综合成 20 个核，除了改 `NUM_CORES` 还要改什么？
**参考答案**：还要加宽 [defines.svh:65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L65) 的 `core_id_t`（当前硬编码 `logic[3:0]`，最多 16 核），否则核号 16~19 会溢出。这正是该行注释「To synthesize more, increase this width」的含义。

---

### 4.2 L2 请求仲裁

#### 4.2.1 概念说明

多个核共享一个 L2，而 L2 的入口（仲裁级）一拍只放行一条请求。于是 `l2_cache_arb_stage` 要在两类请求源之间做选择：

1. **各核的新请求**（`l2i_request_valid` 位图 + `l2i_request` 数组）：核 k 的 L1 缺失、store、flush、invalidate 等发来的请求。
2. **AXI 总线回填后「重启」的请求**（`l2bi_request_valid`）：L2 此前发生了缺失，把请求发给 AXI 去主存取数据；数据取回后，这条请求要**带着新数据重新进入 L2 流水线**完成填充（u6-l3 讲过的 fill 流程）。

仲裁级的核心规则只有一条，但至关重要：**回填的重启请求优先于各核的新请求**。原因写在模块头注释里：这是为了「避免 miss 队列被填满」（[l2_cache_arb_stage.sv:23-24](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L23-L24)）。如果让新请求抢占重启请求，那么已经在途的缺失迟迟完不成填充，miss 队列里的表项就释放不掉，最终堆满、再也接不了新缺失。

在「各核新请求」内部，多核时用 `rr_arbiter` 做**轮询公平仲裁**；单核时直接旁路仲裁器（只有一颗核，没什么可仲裁的）。

> 名词解释：**round-robin（轮询）**——按一个不断旋转的优先级指针选下一个候选者，保证长期来看每个请求者获得均等的带宽，不会饿死。**one-hot（独热）**——N 位信号里至多一位为 1，`grant_oh` 就是这种编码，直接指明「选中了哪一个」。

#### 4.2.2 核心流程

每拍，仲裁级先回答两个问题：**「这一拍能不能接收请求？」**（`can_accept_request`），以及**「接收谁的？」**（`grant_oh`）。

```
                       两类请求源
          ┌────────────────────┴────────────────────┐
          ▼                                         ▼
  ① 回填重启请求 l2bi_request_valid            ② 各核新请求
     （AXI 取回数据后重启）                      l2i_request_valid[NUM_CORES-1:0]
          │                                         │  （位图：第 k 位 = 核 k 有请求）
          │                                         ▼
          │                            NUM_CORES>1 ? ──► rr_arbiter ──► grant_oh（独热）
          │                            NUM_CORES=1  ──► 直通 grant_oh[0]=valid[0]
          │                                         │
          └────────────────► 优先级判断 ◄────────────┘
                              │
              if (l2bi_request_valid)  → 选重启请求（带 l2a_l2_fill 标志）
              else if (|l2i_request_valid && can_accept_request) → 选 grant_request
                              │
                              ▼
                    l2a_request / l2a_request_valid  → 送入 L2 标签级（下一阶段）

   can_accept_request = !l2bi_request_valid && !l2bi_stall
   l2_ready[k] = grant_oh[k] && can_accept_request   （告诉核 k：你这拍被接收了）
```

两个要点：

1. **重启优先**：只要 `l2bi_request_valid` 为真，本拍就一定选重启请求（见 4.2.3 的 `always_ff`），各核新请求被压到下一拍。同时这也意味着 `can_accept_request` 在重启请求存在时为假（[l2_cache_arb_stage.sv:58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L58)），于是 `l2_ready` 对所有核都为假——各核的新请求本周不被消费、保持原样等下拍。
2. **握手与组合环规避**：`l2_ready[k]` 既依赖 `grant_oh[k]` 又依赖 `can_accept_request`，而 `can_accept_request` 依赖 `l2bi_stall`。模块头注释特别强调（[l2_cache_arb_stage.sv:25-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L25-L27)）：各核的 `valid` 位**绝不能依赖** `l2_ready`，否则会形成 `valid → l2_ready → valid` 的组合环。

#### 4.2.3 源码精读

**接收能力与握手**见 [l2_cache_arb_stage.sv:53-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L53-L67)：

```systemverilog
assign can_accept_request = !l2bi_request_valid && !l2bi_stall;   // 58
...
generate
    for (request_idx = 0; request_idx < `NUM_CORES; request_idx++)
        assign l2_ready[request_idx] = grant_oh[request_idx] && can_accept_request;  // 65
endgenerate
```

`l2_ready[k]` 为真表示「核 k 的请求本拍被接收，核 k 可以腾出 miss 队列表项」。注意它**同时**要求「被仲裁选中（`grant_oh[k]`）」与「L2 这拍确实能收（`can_accept_request`）」。

**多核仲裁 vs 单核旁路**见 [l2_cache_arb_stage.sv:69-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L69-L92)：

```systemverilog
generate
    if (`NUM_CORES > 1) begin
        core_id_t grant_idx;
        rr_arbiter #(.NUM_REQUESTERS(`NUM_CORES)) request_arbiter(   // 74
            .request(l2i_request_valid),
            .update_lru(can_accept_request),
            .grant_oh(grant_oh), .*);
        oh_to_idx #(.NUM_SIGNALS(`NUM_CORES)) oh_to_idx_grant(       // 80
            .one_hot(grant_oh),
            .index(grant_idx[CORE_ID_WIDTH - 1:0]));
        assign grant_request = l2i_request[grant_idx[CORE_ID_WIDTH - 1:0]];  // 84
    end
    else begin
        // Single core
        assign grant_oh[0] = l2i_request_valid[0];                   // 89
        assign grant_request = l2i_request[0];                        // 90
    end
endgenerate
```

多核分支（`NUM_CORES > 1`）实例化 `rr_arbiter`，把 `NUM_REQUESTERS` 设为 `NUM_CORES`，请求位图就是 `l2i_request_valid`；`update_lru` 接 `can_accept_request`（**只有真把请求消费掉的那一拍才轮转优先级**，否则空转不该改变轮次）。`grant_oh` 经 `oh_to_idx` 转成核号 `grant_idx`，再用它从请求数组里挑出 `grant_request`。

单核分支（`NUM_CORES == 1`）直接把唯一的请求透传——没有竞争，省掉仲裁器。这种 `if (NUM_CORES > 1) ... else ...` 的 `generate` 是 Nyuzi 让单核构建不付多核代价的常见手法（顶层调试数据选择 [nyuzi.sv:108-113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L108-L113) 也是同样写法）。

**重启请求优先**的核心时序逻辑见 [l2_cache_arb_stage.sv:94-111](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L94-L111)：

```systemverilog
always_ff @(posedge clk) begin
    l2a_data_from_memory <= l2bi_data_from_memory;
    if (l2bi_request_valid) begin
        // Restarted request from external bus interface
        l2a_request <= l2bi_request;
        l2a_l2_fill <= !l2bi_collided_miss && !restarted_flush;   // 标记这是一次回填填充
        l2a_restarted_flush <= restarted_flush;
    end
    else begin
        // New request from a core
        l2a_request <= grant_request;
        l2a_l2_fill <= 0;
        l2a_restarted_flush <= 0;
    end
end
```

`if (l2bi_request_valid)` 分支优先：有重启请求时，整条 L2 流水线本拍处理的是「带回来数据的缺失请求」（`l2a_l2_fill=1` 告诉后续阶段「这是回填，要写入缓存」），各核新请求被忽略到下一拍。`l2a_request_valid` 的产生逻辑（[l2_cache_arb_stage.sv:113-136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L113-L136)）同样遵循「重启优先，其次有新请求且 `can_accept_request`」。其中两条断言（[l2_cache_arb_stage.sv:124-125](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L124-L125)）保证 `IINVALIDATE`/`DINVALIDATE` 这类「不应引发缺失、所以不应被重启」的包绝不会出现在重启通道——这是设计不变量。

**仲裁器本身的实现**见 `rr_arbiter`（[rr_arbiter.sv:27-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L27-L76)）。它是**纯组合**给出 `grant_oh`（与 `request` 同周期有效，[rr_arbiter.sv:41-60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L41-L60)），靠一个寄存器 `priority_oh`（优先级指针，独热）记录「该从谁开始找」。组合逻辑的语义是：从 `priority_oh` 指向的位置开始**向后**找第一个同时「有请求」的请求者，把它授予；若 `priority_oh` 自己有请求则授予它。每拍若 `update_lru` 为真，指针「左移旋转一位」（`priority_oh_nxt = {grant_oh[NUM_REQUESTERS-2:0], grant_oh[NUM_REQUESTERS-1]}`，[rr_arbiter.sv:63-64](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L63-L64)），即「刚被选中的排到队尾」，实现公平轮转，见 [rr_arbiter.sv:66-75](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L66-L75)。复位时 `priority_oh <= 1`（从请求者 0 起算）。

> 这个 `rr_arbiter` 是全项目复用的「公共零件」：L2 仲裁级（核间）、IO 互连（核间 + 核内线程间，[io_interconnect.sv:67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L67)）、`thread_select_stage`（核内线程间）都用它。区别只在 `NUM_REQUESTERS` 参数和 `update_lru` 的接法。

#### 4.2.4 代码实践（源码阅读型）

**目标**：分析三个核同时请求 L2 时，`rr_arbiter` 如何公平地逐拍放行。

**步骤**：

1. 假设 `NUM_CORES=4`，某拍 `l2i_request_valid = 4'b0111`（核 0、1、2 同时请求，核 3 无请求），`l2bi_request_valid=0`、`l2bi_stall=0`，当前 `priority_oh` 指向核 0。
2. 阅读 [rr_arbiter.sv:41-60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L41-L60)：从优先位置核 0 开始找第一个有请求者 → 授予核 0，`grant_oh = 4'b0001`。
3. 因 `can_accept_request=1`，`update_lru=1`，本拍后 `priority_oh` 旋转到核 1（[rr_arbiter.sv:63-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L63-L64)）。下一拍从核 1 起找 → 授予核 1，指针转到核 2；再下一拍授予核 2。

**需要观察的现象 / 预期结果**：连续三拍分别放行核 0、核 1、核 2，**没有谁被饿死**，且与「固定优先级仲裁」不同——若用固定优先级且核 0 持续请求，核 1、2 永远排不上。这就是轮询的公平性。若中途某拍 `l2bi_request_valid` 跳起（回填数据到了），阅读 [l2_cache_arb_stage.sv:97-103](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L97-L103) 确认：那拍所有核的 `l2_ready` 都为 0（`can_accept_request=0`），重启请求插队先行，核的新请求原地等待——正是「重启优先防 miss 队列堆积」的体现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rr_arbiter` 的 `update_lru` 接的是 `can_accept_request`，而不是「恒为 1」？
**参考答案**：只有当 L2 **真正消费**了一条请求（`can_accept_request` 为真）时，才应让优先级指针前进。若 L2 因 `l2bi_stall` 等原因这拍收不下请求，仲裁结果作废，指针不该转动——否则会跳过某些请求者、破坏公平性。对比 `thread_select_stage` 里 `update_lru` 恒为 1，是因为那里每拍必然发射一条（只要有人就绪）。

**练习 2**：若把「重启优先」改成「新请求优先」，会出现什么后果？
**参考答案**：新请求会不断抢占在途缺失的回填重启，导致那些已发出 AXI 读、取回了数据的缺失迟迟无法完成填充、miss 队列表项释放不掉；队填满后再也接不了新缺失，L2 乃至整机卡死。重启优先正是为了让缺失尽快闭合、腾出表项。

**练习 3**：单核时为什么把仲裁器整个旁路掉（`NUM_CORES == 1` 分支），而不是仍实例化一个 `NUM_REQUESTERS=1` 的 `rr_arbiter`？
**参考答案**：一是省面积与功耗（仲裁器是组合逻辑、随核数增大变贵）；二是避免「单核时 `CORE_ID_WIDTH=$clog2(1)=0`」带来的位宽退化问题（`[CORE_ID_WIDTH-1:0]` 会变成非法的 `[-1:0]`）。直接 `grant_oh[0]=valid[0]` 既清晰又安全。

---

### 4.3 响应广播与核间过滤

#### 4.3.1 概念说明

仲裁解决了「请求怎么进 L2」，本节解决「响应怎么回各核」。L2 一拍只产生一个响应包（在四阶段流水线的最后一级 `l2_cache_update_stage`），却要服务所有核。Nyuzi 选择了最省线的方案：**单点响应总线 + 广播 + 核号过滤**。

响应包里带两个路由字段：`core`（给哪个核）和 `id`（该核内哪个 miss 队列表项/线程）。L2 把每条响应都广播到一条全局的 `l2_response` 线上，**所有核都连着这条线、都收到每一条响应**。每个核的 `l1_l2_interface` 里有一句 `ack_for_me = response.core == CORE_ID`——只有核号对得上的核才把这条响应当成「给我的」去更新自己的 L1、唤醒自己的线程；其余核只看一眼就丢掉。

但有一个**例外**：DCache 相关的响应，**所有核都要看**，哪怕核号对不上。这就是 **snoop（监听）**——多核缓存一致性的基础。当一个核 store 了某个缓存行、L2 给出响应时，其他核若自己的 L1 里也有这一行的副本，就必须更新或作废它，否则各核 L1 会看到陈旧数据。所以 `l2i_snoop_en` 对所有 DCache 响应都拉起（不分核），见 [l1_l2_interface.sv:237](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L237)。这正是「广播」的额外红利：同一条响应总线天然就能驱动 snoop，无需另拉一致性总线。

> 名词解释：**snoop（侦听/监听）一致性**——所有缓存共享一条总线，每个缓存都「偷听」总线上的事务，发现涉及自己持有行的操作就自行更新/作废。Nyuzi 的 L2→L1 响应广播天然提供了这条「总线」。

#### 4.3.2 核心流程

```
   L2 update_stage 产出唯一响应包 l2_response（含 core, id, packet_type, address, data...）
                              │
                              ▼  单条广播线，连到每个核的 l1_l2_interface（CORE_ID 参数各不同）
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   core 0 的             core 1 的             core N-1 的
   l1_l2_interface       l1_l2_interface       l1_l2_interface
        │                     │                     │
   ① 过滤路由：          ① 过滤路由：          ① 过滤路由：
      ack_for_me =           ack_for_me =           ack_for_me =
      (resp.core == 0)       (resp.core == 1)       (resp.core == N-1)
        │                     │                     │
   ② 若是给我的：         ② 若是给我的：         ② 若是给我的：
      更新 L1 标签/数据       更新 L1 标签/数据       更新 L1 标签/数据
      唤醒等待线程            唤醒等待线程            唤醒等待线程
        │                     │                     │
   ③ DCache 响应一律 snoop（不分核）：命中本核 L1 的行 → 更新/作废（一致性）
```

关键点：

1. **路由靠 `{core, id}`**。`core` 选核，`id`（类型 `l1_miss_entry_idx_t`，即核内线程号）选该核内具体哪条 miss 队列表项——这正是 u6-l2 讲过的「响应靠 `id` 路由回正确表项」，多核后前面再加一层核号。
2. **只有「给我的」才更新 L1 / 唤醒线程**；但 **snoop 对所有核生效**。

#### 4.3.3 源码精读

**唯一的单点响应端口**在 L2 外壳上声明，见 [l2_cache.sv:49-50](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L49-L50)：`output logic l2_response_valid` 与 `output l2rsp_packet_t l2_response`——只有一组，不是数组。这组信号在顶层 `nyuzi.sv` 里声明为全局线（[nyuzi.sv:57-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L57-L58)），并通过 `.*` 同时连到**每一个核实例**的 `l2_response` 输入端口——这就是「广播」的物理实现：同一根线，所有核都挂在上面。

**响应包结构**定义见 [defines.svh:383-392](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L383-L392)：

```systemverilog
typedef struct packed {
    logic status;                 // store_sync 是否成功
    core_id_t core;               // ★ 路由：给哪个核
    l1_miss_entry_idx_t id;       // ★ 路由：核内哪条 miss 表项/线程
    l2rsp_packet_type_t packet_type;
    cache_type_t cache_type;
    l2_addr_t address;
    cache_line_data_t data;
} l2rsp_packet_t;
```

请求包 `l2req_packet_t` 同样带 `core`/`id`（[defines.svh:364-373](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L364-L373)），由请求核在发出时打上 `core = CORE_ID`（[l1_l2_interface.sv:407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L407)），L2 原样回填进响应（`l2_response.core <= l2r_request.core`，[l2_cache_update_stage.sv:139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L139)）。请求核写入的核号，在响应里被各核用来认领。

**核号过滤**在每个核的 `l1_l2_interface` 里完成，见 [l1_l2_interface.sv:305](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L305)：

```systemverilog
assign ack_for_me = response_stage2_valid && response_stage2.core == CORE_ID;
```

（`response_stage2` 是把广播响应打一拍寄存后的结果，[l1_l2_interface.sv:254-259](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L254-L259)。）只有 `ack_for_me` 为真的核，才会驱动 `dcache_update_en`/`icache_update_en`/`dcache_l2_response_valid` 等信号去更新自己的 L1（[l1_l2_interface.sv:311-344](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L311-L344)），并用 `response_stage2.id` 路由回对应的 miss 队列表项、唤醒等待线程。

**snoop（一致性监听）**对所有核生效、不分核号，见 [l1_l2_interface.sv:237](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L237)：

```systemverilog
assign l2i_snoop_en = l2_response_valid && l2_response.cache_type == CT_DCACHE;
```

只要是一条 DCache 响应，**每个核**的 `l2i_snoop_en` 都拉起，各自去查自己的 L1 是否有这一行、是否需要更新/作废。注意「LRU fill」（回填该核自己的 L1）才加 `l2_response.core == CORE_ID` 限制（[l1_l2_interface.sv:239-243](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L239-L243)），而 snoop 不加——这正区分了「给我的数据」与「别人动了、我也要跟着改」两件事。单核配置下 snoop 也被复用，用于消除虚拟地址同义词（synonym，u6-l1 讲过）。

**多核对 LL/SC 同步状态的影响**：u10-l1 讲过，LL/SC 的链接状态存于共享 L2（因为只有 L2 能看到所有核的写）。多核后，「全局线程号」成为链接槽位的键——见 [l2_cache_read_stage.sv:208](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L208)：

```systemverilog
assign request_sync_slot = GLOBAL_THREAD_IDX_WIDTH'({l2t_request.core, l2t_request.id});
```

它把请求包里的 `core`（核号）与 `id`（核内线程号）拼成 `GLOBAL_THREAD_IDX_WIDTH` 位（= `$clog2(TOTAL_THREADS)`，[l2_cache_read_stage.sv:90](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L90)）的全局线程号，作为 `load_sync_address` 数组的下标。于是每个**全局**线程有自己独立的链接槽，`store_sync` 的成功判定（「链接有效且行号相等」）在多核间依然正确——这是 u10-l1 单核结论在多核下的自然推广。

#### 4.3.4 代码实践（源码阅读型）

**目标**：跟踪一条 L2 响应从「单点输出」到「被正确的核认领、同时被所有核 snoop」的全过程。

**步骤**：

1. 在 [l2_cache_update_stage.sv:137-144](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L137-L144) 确认响应包如何被组装：`l2_response.core <= l2r_request.core`、`l2_response.id <= l2r_request.id`——即响应原样带回请求核打的标记。
2. 在 [nyuzi.sv:57-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L57-L58) 与 [nyuzi.sv:117-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L117-L139) 确认这组信号通过 `.*` 连到每个核实例——同一根线，所有核都收。
3. 在 [l1_l2_interface.sv:305](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L305) 看 `ack_for_me` 如何用 `core == CORE_ID` 过滤；在 [l1_l2_interface.sv:311-344](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L311-L344) 看只有 `ack_for_me` 才驱动 L1 更新与线程唤醒。
4. 在 [l1_l2_interface.sv:237](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L237) 看 snoop **不加** `core == CORE_ID` 限制——所有核都监听 DCache 响应。

**需要观察的现象 / 预期结果**：你能用自己的话讲清「同一条 `l2_response`，为什么只有一个核会更新自己的 L1 缓存行、却所有核都可能更新自己 L1 里同一行的副本」——前者是路由（`core` 匹配），后者是一致性（snoop 不分发核）。这正是「单点广播」同时承载路由与一致性的精妙之处。

#### 4.3.5 小练习与答案

**练习 1**：响应包里同时有 `core` 和 `id`，各起什么作用？为什么缺一不可？
**参考答案**：`core` 选「哪个核」（跨核路由），`id` 选「该核内哪条 miss 队列表项/哪个线程」（核内路由）。缺 `core`，响应无法在多核间找到正确目标；缺 `id`，即便到了正确的核，也无法知道该唤醒哪个等待线程、更新哪条 miss 表项（u6-l2 讲过同核内 miss 队列以线程号为索引）。

**练习 2**：为什么 `l2i_snoop_en` 不加 `l2_response.core == CORE_ID`，而 `l2i_dcache_lru_fill_en` 要加？
**参考答案**：snoop 是**一致性**动作——别的核对某行的修改，只要我 L1 里也有副本就必须跟进，与「这条响应是不是给我的」无关，所以不分核。而 LRU fill 是「把回填数据写进**我自己**的 L1」——只有这条响应确实是核 k 的缺失导致的回填（`core == k`）时，核 k 才该填，其余核不该把别人的数据填进自己缓存。

**练习 3**：多核下，核 0 的线程 0 与核 1 的线程 0 都在做 LL/SC，它们的链接状态会串扰吗？
**参考答案**：不会。链接槽位以**全局**线程号 `{core, id}` 为键（[l2_cache_read_stage.sv:208](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L208)）：核 0 线程 0 的键是 `{0,0}`，核 1 线程 0 的键是 `{1,0}`，是两个不同槽位。多核只是把 u10-l1 的「核内线程号」键升级为「全局线程号」键，语义不变。

---

## 5. 综合实践

**任务**（对应本讲指定的实践任务）：把 `NUM_CORES` 从默认的 1 改为多核，说清需要哪些配套修改、仲裁如何公平工作、以及为何 `tests/core/multicore` 必须重建硬件模型。本任务为「源码阅读 + 配置修改 + 现象分析」型，**不会真实运行**（运行需本地已构建工具链，见 u1-l2），故凡涉及运行结果均标注「待本地验证」。

### 5.1 第一步：说清「改 NUM_CORES 需要哪些配套修改」

1. **改配置**。打开 [config.svh:40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40)，把 `\`define NUM_CORES 1` 改为目标值（多核测试要求 8）。确认仍在合法区间 1~16（[config.svh:31-32](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L31-L32)、[nyuzi.sv:71](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L71)）；若要超过 16 核，还须加宽 [defines.svh:65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L65) 的 `core_id_t`。
2. **核对派生量**。`NUM_CORES` 变了，`TOTAL_THREADS = THREADS_PER_CORE * NUM_CORES`（[defines.svh:44](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L44)）自动跟着变；`thread_en` 位宽（[nyuzi.sv:41](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L41)）、`l2i_request` 数组大小（[nyuzi.sv:37](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L37)）等都由 `NUM_CORES` 派生，无需手改。
3. **检查相关隐性约束**。`L1D_WAYS`/`L1I_WAYS` 仍须 ≥ `THREADS_PER_CORE`（[nyuzi.sv:72-73](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L72-L73)）；L2 配置无需改，但若改了 `L2_WAYS`，需同步改 testbench 的 `flush_l2_cache`（[config.svh:28-30](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L28-L30) 的注释提醒）。
4. **软件侧**。若程序里写死了线程总数（如 [multicore.S:23](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L23) 的 `\`#define TOTAL_THREADS 32`），要与新配置一致（8 核 × 4 = 32）。
5. **重建**。`NUM_CORES` 是编译期宏，必须重新 `cmake . && make`（或至少重跑 Verilator），让 `generate` 循环重新展开成新的核数、重新生成 `bin/nyuzi_vsim`。

> 结论：多核化的「代码修改」其实很少（往往只改 `config.svh` 一行），但它是**编译期参数**，必须重建硬件模型才能生效——这是本题第三问的答案。

### 5.2 第二步：分析仲裁如何在多核同时请求时保持公平

设 `NUM_CORES=8`，某段时间内 8 个核都在密集访存、频繁 L1 缺失，于是 `l2i_request_valid` 上经常同时有多位为 1。阅读 [l2_cache_arb_stage.sv:69-92](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L69-L92) 与 [rr_arbiter.sv:41-75](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L41-L75)，回答：

- **谁优先**：回填重启请求（`l2bi_request_valid`）无条件插队优先（[l2_cache_arb_stage.sv:97-103](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L97-L103)），防止 miss 队列堆积。
- **新请求之间**：`rr_arbiter` 从 `priority_oh` 指向的核起，找第一个有请求的核授予；每实际消费一条（`can_accept_request`）就把指针左旋一位（[rr_arbiter.sv:63-75](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L63-L75)），使 8 个核长期获得均等的 L2 带宽，无人饿死。
- **反压**：没被选中的核，其 `l2_ready[k]=0`（[l2_cache_arb_stage.sv:65](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L65)），该核的 miss 队列表项不释放，请求原地等待下一拍——但该核的流水线靠细粒度多线程（u10-l2）继续跑别的线程，不会整机停顿。

> 待本地验证：若已构建 8 核 `nyuzi_vsim`，可对一段 8 核并行访存程序用 `+trace` 跑协同仿真（u8-l3），在 trace 里统计各核被 `grant` 的次数，应大致相等（轮询公平）；并观察回填重启请求出现时，新请求被让位的周期。

### 5.3 第三步：解释 multicore 测试为何需要重建硬件模型

阅读 [runtest.py:29-34](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/runtest.py#L29-L34) 与 [multicore.S:19-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L19-L47)。该测试的注释明确写：「This test only works with 8 cores enabled. In hardware/core/config.sv, set NUM_CORES to 8.」（[multicore.S:19-22](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L19-L22)）。原因有三层：

1. **核数是编译期参数**。默认 `NUM_CORES=1`，默认构建出的 `nyuzi_vsim` 只有一个核。该测试要 8 核 × 4 线程 = 32 个全局线程协作，单核模型物理上没有另外 7 个核，跑不起来。必须改 `config.svh` 并重建。
2. **重建的是 Verilator 模型**。该测试装饰器写明 `@test_harness.test(['verilator'])`（[runtest.py:29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/runtest.py#L29)）——它验证的是**真实硬件多核行为**：共享 L2 的仲裁、snoop 一致性、跨核对共享内存的 LL/SC 同步。`nyuzi_vsim` 由 Verilator 把参数化 RTL 编译成周期精确 C++ 模型，改核数后必须重新 Verilate 才能得到 8 核模型。
3. **为何不在模拟器上跑**。值得注意：C 模拟器 `nyuzi_emulator` 其实**支持**多核——核数是它的**运行时命令行参数**（默认 1，[main.c:157](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L157)、`-c` 解析见 [main.c:263](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L263)，`assert(num_cores * threads_per_core <= 32)` 见 [processor.c:199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L199)）。但模拟器是功能级模型（平坦内存数组，**不建模** L2 仲裁、snoop、缓存一致性时序），无法验证本测试针对的「多核共享缓存一致性」这一硬件专属语义。所以该测试只盯 verilator——这正是 u8-l1 所说「模拟器是功能金标准、但不给周期数、不建模微架构」的一个具体体现。

> 关于测试逻辑本身：[multicore.S:29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L29) 用 `start_all_threads`（[asm_macros.h:80-83](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L80-L83)，写 `0xffffffff` 到 `CR_RESUME_THREAD` 唤醒全部 32 个全局线程）；每个线程读 `CR_CURRENT_THREAD` 得到自己的全局线程号（[multicore.S:30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L30)），自旋等待一个共享计数器 `current_thread` 等于自己（[multicore.S:33-36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L33-L36)），打印 `'A' + 全局线程号`（[multicore.S:39-40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L39-L40)），把计数器加一、再挂起自己（[multicore.S:42-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/multicore.S#L42-L47)）。于是 32 个线程借助共享内存里的计数器 + LL/SC 隐含的串行化，依次打印 `A`..`` ` `` 共 32 个字符——这正是 [runtest.py:33-34](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/multicore/runtest.py#L33-L34) 校验的字符串。它一次性压测了「多核仲裁 + snoop 一致性 + LL/SC 跨核同步 + 软件线程调度」四件事。

> 待本地验证：若你已按 5.1 把 `NUM_CORES` 改为 8 并重建出 8 核 `nyuzi_vsim`，可进入 `tests/core/multicore` 执行 `./runtest.py`，预期输出恰好是 `` ABCDEFGHIJKLMNOPQRSTUVWXYZ[\]^_` ``（32 个字符）。若看到字符乱序或缺失，多半是 snoop 一致性或 LL/SC 在多核下出了问题。

## 6. 本讲小结

- **多核 = generate 循环 + CORE_ID 参数 + 数组信号**。顶层 [nyuzi.sv:115-139](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L115-L139) 用 `for` 循环实例化 `NUM_CORES` 个 `core`，每核以 `core_idx` 为 `CORE_ID`；每核一份的信号收拢成「以核号为下标的数组」（`l2i_request` 等），`thread_en` 则是 `TOTAL_THREADS` 位全核位图、按 `+:` 切片下发（[nyuzi.sv:127](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L127)）。核是私有、L2 是共享。
- **`NUM_CORES` 是编译期参数**，改它必须改 [config.svh:40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40) 并重建硬件模型；合法区间 1~16，超过需加宽 [defines.svh:65](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L65) 的 `core_id_t`（[nyuzi.sv:71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L71)）。
- **L2 仲裁**在 [l2_cache_arb_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv)：回填重启请求**优先**于各核新请求以防 miss 队列堆积（[L94-111](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L94-L111)）；新请求之间多核用 `rr_arbiter` 轮询公平仲裁、单核旁路（[L69-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L69-L92)）；`l2_ready[k] = grant_oh[k] && can_accept_request`，且 valid 不得依赖 ready 以避免组合环（[L25-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L25-L27)）。
- **`rr_arbiter`** 是纯组合、全项目复用的轮询仲裁器：从 `priority_oh` 指针起找首个有请求者授予，每实际消费一条（`update_lru`）把指针左旋一位，保证公平不饿死（[rr_arbiter.sv:41-75](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L41-L75)）。
- **响应走单点广播**：L2 只有一组 `l2_response`（[l2_cache.sv:49-50](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L49-L50)），连到所有核；每个核用 `ack_for_me = response.core == CORE_ID` 认领自己的响应（[l1_l2_interface.sv:305](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L305)），路由键是 `{core, id}`。
- **snoop 一致性**复用同一条广播线：所有核都对 DCache 响应监听（不分核，[l1_l2_interface.sv:237](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L237)），命中本核 L1 的行即更新/作废；LL/SC 链接状态以 `{core,id}` 全局线程号为槽位键（[l2_cache_read_stage.sv:208](https://github.com/jbush001-NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L208)），多核间正确隔离。

## 7. 下一步学习建议

- **回顾 u6-l3（L2 缓存四阶段流水线）**：本讲的仲裁级是 L2 四阶段的第一级，缺失填充的「重启请求」、collided miss、脏行写回等概念都来自那一讲；结合阅读能把「请求如何进 L2、缺失如何闭合」补成完整闭环。
- **回顾 u10-l1（LL/SC 与 membar）**：本讲把它的「链接状态存于共享 L2」推进到多核——槽位键从核内线程号升级为 `{core,id}` 全局线程号（[l2_cache_read_stage.sv:208](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L208)）。对照 `tests/stress/atomic` 与 `tests/core/multicore` 两个测试，理解跨核原子性是如何被验证的。
- **回顾 u3-l1（顶层 nyuzi）**：本讲是顶层层面的深化；若想看 AXI 系统总线、IO 互连、片上调试器如何与多核顶层拼接，可重读该讲与 u6-l4。
- **建议继续阅读的源码**：`hardware/core/io_interconnect.sv`（IO 总线的两级仲裁：核间 + 核内线程间，同样用 `rr_arbiter`）、`hardware/core/l2_cache_update_stage.sv`（响应包如何组装、`status`/`core`/`id` 如何回填）、`hardware/core/l1_l2_interface.sv:237-344`（snoop 与 ack_for_me 的完整配合）。
- **后续方向**：本单元（u10）至此完结。若关注片上调试如何选核（`on_chip_debugger` 的 `ocd_core` 与 [nyuzi.sv:110](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L110) 的 `data_to_host` 选择），可进入 u11-l1；若关注多核下的性能观测，可进入 u11-l2。
