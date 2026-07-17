# 同步内存操作 LL/SC 与 membar

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `load_sync` / `store_sync` 这一对指令（即 **LL/SC，Load-Linked / Store-Conditional**）的语义：为什么它能实现「读—改—写」（RMW）的原子操作，以及成功/失败的判定依据。
- 跟踪一次同步访存在硬件里的完整往返：L1 把它当作 cache miss 发往 L2、L2 在共享层登记并裁决、响应回到 L1 唤醒线程——并理解「同步状态」究竟保存在哪里、为何保存在那里。
- 解释 `membar`（内存屏障）指令如何借助 store 队列保证内存操作的可见性顺序，以及它与普通 store 的异同。
- 独立用 `load_sync` / `store_sync` 写出一个自旋锁，并能解释 `stress/atomic` 测试为何能在多线程竞争下验证原子性。

本讲是「并发、同步与多核」单元（u10）的第一篇，承接 u6-1（L1 数据缓存）的 store 队列与 miss 处理，以及 u4-3（线程选择与记分牌）的挂起/唤醒机制。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面几个概念（它们在前面讲义里已建立）：

- **多硬件线程**：Nyuzi 每核默认有 4 个硬件线程，复位后只有线程 0 醒着；软件用 `CR_RESUME_THREAD` 唤醒其余线程（见 u9-2、u10-2）。
- **L1 数据缓存**：每核私有、虚拟索引/物理标签（VI/PT）、两拍流水（标签级 `dcache_tag_stage` + 数据级 `dcache_data_stage`）；写操作经每线程一条的 **store 队列**缓冲（见 u6-1）。
- **L1 缺失后的往返**：L1 缺失经 `l1_load_miss_queue` / `l1_store_queue` 进 `l1_l2_interface`，发往共享的 L2，L2 响应回来后更新 L1 并用位图唤醒被挂起的线程（见 u6-2、u6-3）。
- **L2 缓存**：所有核共享、物理索引/物理标签、四阶段流水线；L1 已经完成地址翻译，所以 L2 不再需要 TLB（见 u6-3）。
- **M 格式访存指令与「set 型」结果**：所有访存指令都是 M 格式，4 位 `memory_op_t` 区分类型，`[29]` 区分 load/store；比较类指令的结果是 0 或 1 直接写回寄存器（见 u2-3、u2-2）。
- **精确异常与回滚**：硬件用「挂起线程 + 回滚 PC」的方式让指令在合适时机重发（见 u7-3）。

什么是**原子操作**？考虑多线程里最常见的 `counter++`。它其实是一条「读 `counter`、加 1、写回」的序列。如果两个线程交错执行，可能出现：

```
线程A 读到 5
            线程B 读到 5
线程A 写回 6
            线程B 写回 6   ← 丢了 一次 自增！
```

这种竞争（race condition）要求「读—改—写」三步在别人看来是不可分割的一步，即**原子**的。LL/SC 就是 Nyuzi 提供的原子原语。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `hardware/core/defines.svh` | 定义 `MEM_SYNC`（memory_op_t）、`CACHE_MEMBAR`（cache_op_t）、`L2REQ_LOAD_SYNC`/`L2REQ_STORE_SYNC`（L2 请求类型）、L2 响应包里的 `status` 字段。 |
| `hardware/core/dcache_data_stage.sv` | L1 数据级：把同步 load 当作 cache miss 强制发往 L2「登记」；用 `dd_load_sync_pending` 区分第一/第二次请求；识别 `membar`。 |
| `hardware/core/l1_store_queue.sv` | 本讲主角。每线程一条 store 表项，承载同步 store 的挂起/发送/响应/唤醒，并实现 membar 的「等所有 store 完成」。 |
| `hardware/core/l1_l2_interface.sv` | 把 L1 的同步访存翻译成 `L2REQ_LOAD_SYNC`/`L2REQ_STORE_SYNC`，并把 L2 响应里的 `status` 透传回 store 队列。 |
| `hardware/core/l2_cache_read_stage.sv` | L2 读阶段：维护每线程一个「链接地址」槽，裁决某次 `store_sync` 是否成功（`can_store_sync`）。 |
| `tools/emulator/processor.c` | C 模拟器里的功能参考实现：`last_sync_load_addr` + `invalidate_sync_address`，是理解语义的金标准。 |
| `tests/stress/atomic/atomic.S` | 压力测试程序：4 线程用 `load_sync`/`store_sync` 原子地自增共享数组。 |
| `tests/stress/atomic/runtest.py` | 上述测试的驱动脚本：dump 内存后校验每个槽最终都等于 10。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 LL/SC 语义**、**4.2 同步状态（store 队列与 L2 的协作）**、**4.3 membar 排序**。

### 4.1 LL/SC 语义

#### 4.1.1 概念说明

实现原子操作有两条主流路线：

- **CAS（Compare-And-Swap）**：一条指令完成「如果内存里==期望值，就写入新值」，把读、比、写三步打包进硬件。
- **LL/SC（Load-Linked / Store-Conditional）**：拆成两条指令。`load_sync`（LL）普通地读一个值，但同时「登记」这个地址；随后软件做任意计算；`store_sync`（SC）尝试写回，但**只有在登记之后没有任何其他写命中该地址**时才真正写入并报告成功，否则放弃写入并报告失败。

LL/SC 的优点是不限定只改一个字：两条指令之间可以插入任意运算（加法、位运算、甚至构造一个结构体），因此可以构建出任何原子操作（自增、原子交换、无锁链表 push……）。它的代价是：失败时软件要自己重试。

在 Nyuzi 里，LL 与 SC 共用同一个访存类型编码 `MEM_SYNC`，靠 `[29]` 位区分 load/store，与普通访存完全同构（回顾 u2-3）。`store_sync` 是「set 型」指令——和 u2-2 的比较指令一样，它的目标寄存器里写回的是 0 或 1：**1 表示这次条件存储成功，0 表示失败**。失败时内存内容不变，软件据此分支回去重试。

两个关键设计点先记住，后面源码会反复印证：

1. **监视粒度是缓存行（64 字节）**，而不是单个字。任何落在同一缓存行内的写都会令「链接」失效。
2. **链接状态登记在 L2（共享层），而不是 L1**。因为只有共享的 L2 才能同时观察到所有核、所有线程的写，从而正确裁决冲突；L1 是每核私有的，无法可靠地看到别核的写。

#### 4.1.2 核心流程

一个标准的原子自增模板（这正是 `atomic.S` 里用的）：

```text
1:  load_sync  Rd, (ptr)      # 读出 *ptr，并在 L2 登记 ptr 所在缓存行
    计算  Rd = Rd + 1          # 任意本地运算
    store_sync Rd, (ptr)      # 尝试写回；Rd ← 1（成功）/ 0（失败）
    bz Rd, 1b                  # 失败则跳回重试整条序列
```

从硬件视角看，这一对指令的语义由三处协作实现：

- **L1（`dcache_data_stage`）**：负责把同步 load「伪装成一次 cache miss」强制送往 L2 去登记，并区分第一遍（去登记）与第二遍（取结果）。
- **L2（`l2_cache_read_stage`）**：保存每线程一个链接地址，裁决 `store_sync` 成功与否。
- **store 队列（`l1_store_queue`）**：把同步 store 挂起、发往 L2、等响应、唤醒线程、把成功/失败结果交还写回级。

成功条件用一句话概括：`store_sync` 的目标地址所在的缓存行，**必须等于**该线程此前 `load_sync` 登记的缓存行，且该登记**仍然有效**（没有被任何一次写冲掉）。数学化一点，记登记的缓存行号为 \( L_{\text{linked}} \)，本次 store 的缓存行号为 \( L_{\text{store}} \)，则：

\[
\text{success} \;=\; \text{valid}(L_{\text{linked}}) \;\land\; \bigl(L_{\text{store}} = L_{\text{linked}}\bigr)
\]

任一其它线程（或本线程）对同一缓存行的写，都会把 `valid` 清零，于是下一次 `store_sync` 失败。

#### 4.1.3 源码精读

先看编码。`MEM_SYNC` 是 `memory_op_t` 的一个取值：

[hardware/core/defines.svh:127-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L127-L139) —— `memory_op_t` 枚举，`MEM_SYNC = 4'b0101` 与字节/半字/字访存并列，说明它在解码层就是一类普通访存，只是在下游被特殊处理。

再看 L2 这一侧的「链接地址」登记表，这是 LL/SC 语义的真正裁决处：

[hardware/core/l2_cache_read_stage.sv:92-96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L92-L96) —— 每线程一个 `load_sync_address` 与 `load_sync_address_valid`。注意数组下标范围是 `TOTAL_THREADS`（全芯片所有线程），每个线程至多持有一个未完成的链接。

模拟器 `processor.c` 是不含任何微架构细节的功能金标准，读它最能看清语义：

[tools/emulator/processor.c:1319-1322](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1319-L1322) —— `MEM_SYNC` 的 load 分支：读出值，同时记下 `last_sync_load_addr = 物理地址 / 64`（缓存行号）。

[tools/emulator/processor.c:1375-1393](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1375-L1393) —— `MEM_SYNC` 的 store 分支：若目标缓存行号等于登记的行号则成功（寄存器写 1、写内存），否则失败（寄存器写 0、不动内存）。注释提到的「两个副作用」（既改寄存器又改内存）是个重要细节，后面 4.2 会专门讲。

那么 `last_sync_load_addr` 何时被清掉？答案是「任何一次普通写都会令其失效」：

[tools/emulator/processor.c:674-683](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L674-L683) —— `invalidate_sync_address` 遍历本核所有线程，凡登记行号命中本次写地址的，一律置为 `INVALID_ADDR`。硬件 L2 的 `load_sync_address_valid` 起的是完全对应的作用（见 4.2.3）。

> 结论：LL/SC 的成功/失败判定，本质上就是一个「**这一行有没有被别人写过**」的监视器。把这个监视器放在 L2，是因为 L2 是所有核都能看到的、唯一的共享写入汇聚点。

#### 4.1.4 代码实践

**实践目标**：用 `load_sync`/`store_sync` 写一个原子自增，并在模拟器里观察成功与失败的轨迹。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 `tests/stress/atomic/atomic.S`，阅读 `sync_fetch_and_increment` 宏：
   [tests/stress/atomic/atomic.S:28-34](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/stress/atomic/atomic.S#L28-L34) —— 这正是本节开头的「原子自增模板」的真实代码：`load_sync` → `add_i` → `store_sync` → `bz s25, 1b` 重试。
2. 思考：单线程运行时 `store_sync` 会不会失败？为什么？（提示：见 4.2 中「store 之间是否可能冲掉自己的链接」。）
3. （可选运行）若已按 u1-2 装好工具链，执行：

   ```bash
   cd tests/stress/atomic
   python3 runtest.py        # 仅在 verilator 目标上跑
   ```

**需要观察的现象**：程序结束后，从 `0x100000` 起的 512 个 32 位槽，每个都应当等于 10。

**预期结果**：测试通过。如果去掉 `load_sync`/`store_sync` 改成普通 `load_32`/`store_32`，则在 4 线程竞争下部分槽的值会偏离 10（丢失自增），这正是原子性被破坏的表现。

**待本地验证**：若未搭建环境，请标注「待本地验证」，仅完成步骤 1–2 的源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LL/SC 的监视粒度是「缓存行」而不是「精确到字节」？这样做有什么副作用？

**参考答案**：因为硬件用一个「行号」来登记和比对，用缓存行（64 字节）可以复用已有的缓存寻址逻辑、状态位最少。副作用是**伪共享（false sharing）**：两个线程各自原子地操作同一缓存行里**不同**的字，也会互相冲掉链接、被迫重试，降低性能。

**练习 2**：`store_sync` 失败时，目标寄存器得到 0、内存不变。那么 `load_sync` 会失败吗？

**参考答案**：不会。`load_sync` 总是成功——它就是一次普通读取，附带「登记地址」这个副作用。失败只可能发生在 `store_sync`（条件存储）上。

**练习 3**：用 LL/SC 实现「原子地把 `*ptr` 置为 0，并返回旧值」。写出指令序列。

**参考答案**：

```asm
1:  load_sync  Rd, (ptr)     # Rd = 旧值，登记链接
    move       Rtmp, 0
    store_sync Rtmp, (ptr)   # 尝试写 0
    bz Rtmp, 1b               # 失败重试
    # 成功后 Rd 即为旧值
```

---

### 4.2 同步状态：store 队列与 L2 的协作

理解了语义，本模块回答最关键也最反直觉的问题：**硬件究竟把「链接」存在哪里、又怎么把成功/失败的结果送回寄存器？** 答案分两端——L1 的 `l1_store_queue` 负责编排「两遍协议」，L2 的 `l2_cache_read_stage` 负责裁决。

#### 4.2.1 概念说明

为什么同步访存需要「两遍」？因为一次成功的 `store_sync` 必须到 **L2** 才能裁决（L1 看不到别核的写），而裁决结果（成功/失败）又要写回**本线程的寄存器**。于是硬件把它拆成两趟往返：

- **第一遍**：把这条同步指令（load 或 store）当一次 cache miss，送到 L2。对 `load_sync`，L2 顺手登记链接地址；对 `store_sync`，L2 裁决并返回 `status`。
- **第二遍**：线程被唤醒后**重新执行同一条指令**，这一次从 L1 取结果（load 直接读缓存、store 读回 `sync_success` 标志）。

为了不让两遍混淆，`dcache_data_stage` 给每个线程设了一个 toggle 位 `dd_load_sync_pending`：第一遍为 0，过完一遍翻成 1，第二遍据此走不同分支。

`store_sync` 还有个特别之处：它有**两个副作用**——既写内存（若成功），又把成功/失败写进目标寄存器。`l1_store_queue` 必须把这条 store 挂在表项里，直到 L2 响应到来，再把 `status` 锁存进 `sync_success` 字段，供第二遍的写回级读取。

#### 4.2.2 核心流程

**`load_sync`（同步读）的流程**：

```text
第一遍（dd_load_sync_pending == 0）:
  dcache_data_stage: cache_hit 被 sync_access_req 强制为假
                     → 判为 cache miss → 发 L2REQ_LOAD_SYNC 给 L2
  L2 read_stage    : 记录 load_sync_address[slot] = 本行, valid = 1
                     → 返回该行数据
  L1 响应          : 回填 L1、唤醒线程；dd_load_sync_pending 翻为 1
第二遍（dd_load_sync_pending == 1）:
  dcache_data_stage: 此时 cache_hit 可为真（数据已在 L1）
                     → 像普通 load 一样读出并写回寄存器
  （链接已登记在 L2，等后续 store_sync 用）
```

**`store_sync`（同步写）的流程**：

```text
  dcache_data_stage: 识别 store + sync → dd_store_sync = 1，送入 store 队列
  l1_store_queue   : 入队 pending_stores[slot]，sync=1
                     → 发 L2REQ_STORE_SYNC 给 L2
                     → 置 thread_waiting = 1，回滚 PC 挂起本线程
  L2 read_stage    : 算 can_store_sync = valid && 行号相等 && 是 STORE_SYNC
                     若成功: 写内存、令该行所有线程的 valid 失效、status=1
                     若失败: 不写、status=0（且不失效，见下文反活锁说明）
  L1 响应          : 命中本线程表项 → response_received=1,
                                  sync_success = status
                     （表项不清空！保留结果等线程来取）
  唤醒线程         : 第二遍重发同一条 store_sync，
                     写回级读 sq_store_sync_success → 写入目标寄存器
                     → restarted_sync_request 清空表项
```

一个**反活锁**的设计点值得单独记住（源码里有长注释）：当 `store_sync` 失败时，L2 **不会**去失效该行的链接。如果失败也失效，那么竞争激烈时所有线程都会不断互相清掉链接，谁也成功不了（livelock）。失败时保留链接，把「是否放弃」的决定权交给软件重试逻辑，能避免这种情形。详见 4.2.3 的 L2 源码。

#### 4.2.3 源码精读

**(a) L1 数据级：把同步 load 当作 miss**

[hardware/core/dcache_data_stage.sv:193](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L193) —— `sync_access_req` 标记本指令是 `MEM_SYNC`。

[hardware/core/dcache_data_stage.sv:361-363](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L361-L363) —— 命中判定的核心：`cache_hit = |way_hit_oh && (!sync_access_req || dd_load_sync_pending[...]) && dt_tlb_hit`。意思是——对同步访存，**只有在第二遍**（`dd_load_sync_pending==1`）才允许命中；第一遍即便数据已在 L1 也强制 miss，目的是「去 L2 登记一次」。

[hardware/core/dcache_data_stage.sv:512](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L512) —— `dd_cache_miss_sync = sync_access_req`，把 miss 请求标记为同步，供下游翻译成 `L2REQ_LOAD_SYNC`。

[hardware/core/dcache_data_stage.sv:517-538](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L517-L538) —— `dd_load_sync_pending` 的 toggle 逻辑：每次同步访存过一遍就翻转一次，从而区分「去登记」和「取结果」。注释还点出一个边界情况——中断可能插在两遍之间，需要据此取消整条同步操作（这也正是 u8-3 提到的「store_sync + 中断」不在协同仿真覆盖范围内的根因）。

**(b) store 队列：挂起、发送、响应、唤醒**

这是本模块最复杂的文件。先看每个线程的 store 表项结构：

[hardware/core/l1_store_queue.sv:75-88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L75-L88) —— `pending_stores[THREADS_PER_CORE]` 的字段：`sync`（是否同步）、`request_sent`（已发往 L2）、`response_received`（L2 已回）、`sync_success`（裁决结果）、`thread_waiting`（线程是否挂起等响应）、`valid`、数据/掩码/地址。**注意表项是每线程一条**——正因为每线程至多一个未完成 store_sync，所以线程号就能当索引。

[hardware/core/l1_store_queue.sv:133-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L133-L135) —— `restarted_sync_request`：判定「这是第二遍」。条件是表项 valid、已收到响应、且是 sync 类型。这正是上一节流程图里「第二遍重发」的硬件识别。

[hardware/core/l1_store_queue.sv:162-163](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L162-L163) —— 同步 store 的挂起点：`if (dd_store_sync) rollback[thread_idx] = !restarted_sync_request;`。含义：**第一遍**同步 store 总要挂起线程（等 L2 响应），哪怕 store 队列有空位——因为它必须等一次往返；**第二遍**（`restarted_sync_request`）则不挂起，直接取结果走人。这段紧挨着的注释（行 156-161）解释了「为什么第一遍无条件挂起」：为了避开「丢失唤醒」的时序难题。

[hardware/core/l1_store_queue.sv:224-227](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L224-L227) —— `thread_waiting` 的置位/清零：被唤醒（`sq_wake_bitmap`）则清 0，被回滚挂起（`rollback`）则置 1。挂起与唤醒由位图汇总，最终在 4.3 之外的调度逻辑里令线程停跑/复跑。

[hardware/core/l1_store_queue.sv:294-300](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L294-L300) —— 响应处理：若是 sync 表项，则**不立即清空**，而是置 `response_received=1` 并锁存 `sync_success = storebuf_l2_sync_success`；若是普通 store，则直接 `valid<=0` 收尾。注释（行 291-293）说明：同步表项要一直留到线程醒来取走结果。

[hardware/core/l1_store_queue.sv:334](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L334) —— `sq_store_sync_success <= pending_stores[dd_store_thread_idx].sync_success;`：把锁存的成功/失败结果送到写回级，写回级据此把 0/1 写进目标寄存器。

**(c) L1-L2 接口：翻译请求类型并透传 status**

[hardware/core/l1_l2_interface.sv:414](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L414) —— load 侧：`packet_type = dcache_dequeue_sync ? L2REQ_LOAD_SYNC : L2REQ_LOAD;`。

[hardware/core/l1_l2_interface.sv:438-439](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L438-L439) —— store 侧：`else if (sq_dequeue_sync) packet_type = L2REQ_STORE_SYNC;`。

[hardware/core/l1_l2_interface.sv:337](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L337) 与 [hardware/core/l1_l2_interface.sv:345](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L345) —— 响应回来时，`storebuf_l2_response_valid` 与 `storebuf_l2_sync_success = response_stage2.status`，把 L2 响应包里的 `status` 字段（见 defines 的 `l2rsp_packet_t`）原样透传给 store 队列。

**(d) L2 读阶段：裁决 can_store_sync**

[hardware/core/l2_cache_read_stage.sv:208-212](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L208-L212) —— 成功条件的硬件实现：`can_store_sync = load_sync_address[slot] == {tag,set_idx} && load_sync_address_valid[slot] && packet_type==L2REQ_STORE_SYNC`。其中 `request_sync_slot = {core, id}`（行 208），即「核号拼线程号」，所以每线程全局唯一一个链接槽。

[hardware/core/l2_cache_read_stage.sv:261-289](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L261-L289) —— 状态机：`L2REQ_LOAD_SYNC` 登记（行 263-267）；`L2REQ_STORE(_SYNC)` 命中时，若 `STORE` 或 `can_store_sync`，则遍历所有线程把命中该行的 `valid` 清零（行 274-281），最后 `l2r_store_sync_success <= can_store_sync`（行 289）。注意 274 行的条件：`STORE || can_store_sync`——**普通 STORE 一定会失效链接，而失败的 STORE_SYNC 不会**，这正是前述「反活锁」的硬件实现，注释 272-273 写得很明白。

#### 4.2.4 代码实践

**实践目标**：用 `load_sync`/`store_sync` 实现一个**自旋锁**，并据此分析竞争下 `store_sync` 失败时的回滚路径。

**操作步骤**：

1. 阅读并理解锁的原语。下面是一份**示例代码**（非仓库原有），对照 4.1.3 的语义写就：

   ```asm
   # 示例代码：基于 LL/SC 的自旋锁。ptr 指向一个字，0=未锁，非0=已锁。
   .macro lock ptr
   1:  load_sync s1, (\ptr)     # 读锁状态，并在 L2 登记该行
       bnz s1, 1b               # 若已锁（非0），回去重试
       move s2, 1
       store_sync s2, (\ptr)    # 尝试抢锁：写入 1
       bz s2, 1b                # s2==0 表示抢锁失败（有人抢先写过这行），重试
   .endm

   .macro unlock ptr
       move s1, 0
       store_32 s1, (\ptr)      # 普通写即可释放；它会令任何等在该行的链接失效
   .endm
   ```

2. **分析失败回滚**：假设线程 A、B 同时抢同一把锁，都执行到 `load_sync` 读到 0。回答：
   - A 先成功 `store_sync`（写 1）。这一步在 L2 会做什么？（提示：见 4.2.3(d) 的失效逻辑。）
   - 紧接着 B 执行 `store_sync`。它的链接还会有效吗？`can_store_sync` 取何值？目标寄存器得到什么？由哪条指令让它跳回 `1b` 重试？
3. 把上述回滚链路与 4.2.2 流程图里的「挂起—响应—唤醒—第二遍」对应起来：B 的 `store_sync` 第一遍被 `l1_store_queue` 挂起（`thread_waiting=1`），L2 返回 `status=0`，B 被唤醒，第二遍读回 `sync_success=0`，`bz s2, 1b` 命中，回滚取指到 `load_sync` 重试。

**需要观察的现象**：任一时刻至多一个线程持有锁（临界区不重叠）；抢锁失败的线程不会推进，而是在 `1b` 处反复 `load_sync→store_sync`。

**预期结果**：临界区受到互斥保护。若把 `lock` 宏里的 `load_sync`/`store_sync` 换成普通 `load_32`/`store_32`，两个线程可能同时读到 0、同时写入 1，互斥被破坏。

**待本地验证**：若没有运行环境，步骤 1–3 作为源码阅读与推演练习完成即可，并标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `l1_store_queue` 的表项是「每线程一条」，而 miss 队列却能合并多个线程对同一行的请求？

**参考答案**：同步协议要求每线程至多一个未完成的 store_sync（线程在等响应期间被挂起、不会再发新的），所以线程号就是天然索引，不需要合并。而 load miss 队列面向普通缺失，多个线程可能同时缺失同一行，合并能省带宽（见 u6-2）。两者面向的场景不同。

**练习 2**：模拟器里 `MEM_SYNC` 的 store 成功后有一段注释说「cosim 只能跟踪每条指令一个副作用」——它具体是怎么处理的？

**参考答案**：成功的 `store_sync` 有两个副作用（写内存、把寄存器置 1）。模拟器**不**调用会记录副作用的 `set_scalar_reg`，而是直接 `thread->scalar_reg[destsrcreg] = 1` 手动置位，只让「写内存」这一条副作用进入 trace。这样协同仿真（u8-3）逐事件比对时，模拟器与硬件都只产生一个内存写事件，能够对齐。

**练习 3**：`store_sync` 失败时，L2 为什么**不**失效该行的链接？如果失效会怎样？

**参考答案**：失效会导致活锁——竞争激烈时各线程不断互相清链接，谁也成功不了。保留链接把「放弃与否」交给软件重试，让被监视的写（真正成功的 store）才清链接，从而打破活锁。

---

### 4.3 membar：内存屏障与排序

#### 4.3.1 概念说明

有了 store 队列和 L2 回填，**store 不再是「立刻对全机可见」的**。一条 store 进了队列，要等发往 L2、写进 L2 之后，别的核才看得到。这意味着：从别的核的视角看，本核的一串 store 可能「乱序可见」，或者还没落地。

这在多数场景没问题，但在两类场景必须管：

- **生产者—消费者**：生产者写完数据后写一个「就绪标志」；消费者看到标志后才去读数据。如果标志的 store 先于数据的 store 被看见，消费者就读到旧数据。
- **与设备/IO 的交互**：写控制寄存器的顺序往往有严格要求。

`membar`（memory barrier，内存屏障）就是用来划这条线的指令：它保证**在它之前的所有 store 都到达全局可见点之后**，它之后的内存操作才能继续推进。

Nyuzi 把 `membar` 设计成一条**缓存控制指令**（`CACHE_MEMBAR`），而不是真的访存。它最巧妙的地方在于：**它复用了 store 队列的回滚机制，却不入队任何 store**——只是「站在队尾等所有在途 store 走完」。

#### 4.3.2 核心流程

```text
  dcache_data_stage: 识别 CACHE_MEMBAR → dd_membar_en = 1
  l1_store_queue   : 若本线程还有 pending store（未收到响应）：
                       → rollback = 1，挂起本线程（像一条 store 一样占住回滚通道）
                     若所有 pending store 都已完成（队列为空）：
                       → 不回滚，membar 当周期完成，线程继续
  （membar 本身不入队任何表项）
```

换句话说，membar 让线程在 store 队列清空之前一直「原地打转」——每周期重发、每周期因队列非空而被回滚挂起，直到前面所有 store 都被 L2 确认。这等于在程序序里插了一道墙：墙前的 store 必须全部落地，墙后的访存才能开始。

#### 4.3.3 源码精读

[hardware/core/defines.svh:141-151](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L141-L151) —— `cache_op_t` 枚举，`CACHE_MEMBAR = 3'b100` 与 `CACHE_DTLB_INSERT`、`CACHE_DFLUSH`、`CACHE_DINVALIDATE` 等并列。它们都走 store 队列这条「缓存控制」通道。

[hardware/core/dcache_data_stage.sv:219-220](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L219-L220) 与 [hardware/core/dcache_data_stage.sv:344-345](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L344-L345) —— `membar_req` 的产生与 `dd_membar_en` 的输出。

[hardware/core/l1_store_queue.sv:21-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L21-L28) —— 模块顶部注释，明确写出 membar 的设计意图：「A memory barrier request waits until all pending store requests finish. It acts like a store in terms of rollback logic, but doesn't enqueue anything.」

[hardware/core/l1_store_queue.sv:120](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L120) —— `membar_requested_this_entry`：识别本周期是本线程的 membar。

[hardware/core/l1_store_queue.sv:168-170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L168-L170) —— membar 的回滚判定：`else if (membar_requested_this_entry && pending_stores[thread_idx].valid && !got_response_this_entry) rollback = 1;`。含义非常直白：**只要本线程还有未完成的 store（valid 且非本周期刚收到响应），membar 就回滚挂起**；一旦前面的 store 全部完成（表项清空），条件不成立，membar 放行。

注意 membar 与同步 store 的回滚虽然都走同一条 `rollback` 信号，但语义不同：同步 store 是「我有一条自己的 store 在等响应」，membar 是「我在等**别人**（此前所有 store）走完」。两者共享回滚通道，正是模块注释说的「acts like a store in terms of rollback logic」。

#### 4.3.4 代码实践

**实践目标**：理解 `membar` 在生产者—消费者场景里为何必要。

**操作步骤**（源码阅读 + 推演）：

1. 设想一段**示例代码**（非仓库原有）：

   ```c
   // 示例代码：生产者写数据，再写就绪标志
   void produce(volatile int *data, volatile int *ready) {
       *data = 42;          // store 1
       __asm__("membar");   // 屏障：确保 data 先落地
       *ready = 1;          // store 2
   }
   ```

2. 推演：若**没有** `membar`，store 1 和 store 2 都进 store 队列。它们发往 L2 的顺序、被别核看见的顺序是否一定与程序序一致？（提示：回顾 u6-2/u6-3，store 队列会做写合并、按仲裁节拍发送，L2 也按到达顺序处理——单核内通常保序，但跨核可见性取决于回填时序。）
3. 推演：加上 `membar` 后，store 1 必须先收到 L2 响应、队列清空，store 2 才会被允许继续。于是「数据先于标志可见」得到保证。
4. 在仓库里搜索 `membar` 的真实用法：

   ```bash
   grep -rn "membar" software/ tools/
   ```

   阅读其中一两处，确认它是被用来隔开 store 顺序的。

**需要观察的现象**：membar 之前提交的 store 数 = 队列中被清空的表项数；membar 之后的第一条访存指令在队列清空前不会推进。

**预期结果**：membar 在功能上等价于「等待 store 队列排空」，从而在程序序里建立了一道单向屏障。

**待本地验证**：单步时序需用 verilator 波形观察；若仅做阅读，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`membar` 和 `CACHE_DFLUSH`（flush）都走 store 队列，但它们做的事完全不同。请说明各自的作用。

**参考答案**：`membar` 不动任何缓存内容，只等此前所有 store 完成，纯粹是「排序」语义；`CACHE_DFLUSH` 则把指定缓存行的脏数据写回下一级内存并可能作废该行，是「清理」语义。两者共用回滚通道只是实现复用。

**练习 2**：membar 是否会阻塞 load？

**参考答案**：membar 的回滚只针对发起它的那个线程，且只在「本线程有 pending store」时回滚。它不直接阻塞 load 指令的发射；它保证的是该线程**此前 store 的可见性**先于**此后的访存**。对本线程自己而言，load-after-store 一致性由 store 队列的旁路（bypass）保证（见 u6-1、u6-2），membar 主要面向**跨核/跨设备**的可见性顺序。

**练习 3**：为什么 membar 选择「复用 store 回滚机制」而不是单独设计一套阻塞逻辑？

**参考答案**：复用回滚意味着线程在等待期间被挂起、把流水线让给别的硬件线程（多线程隐藏延迟），既不空耗周期，也不用新增状态机；它天然能感知「store 队列是否排空」这一现成条件。这是以最小硬件代价拿到排序语义的巧妙设计。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**原子计数器 + 释放屏障**」的小任务：

**背景**：4 个线程各自对一个共享计数器 `counter` 做若干次原子自增；主线程在所有工作线程结束后，读取 `counter` 校验总数。

**要求**：

1. 用 4.1 的 LL/SC 模板实现 `atomic_add(ptr, n)`（原子加）。
2. 用 4.2 学到的知识，画出一次**竞争失败**时硬件的状态转移：从 `store_sync` 第一遍入队、挂起，到 L2 返回 `status=0`、唤醒、第二遍读回 0、分支重试。在图上标出 `pending_stores` 各字段（`sync`/`request_sent`/`response_received`/`sync_success`/`thread_waiting`）的变化时刻。
3. 工作线程结束前，用 4.3 的 `membar` 确保自己的最后一次自增对主线程可见，再写一个 `done` 标志。解释：如果省掉这个 `membar`，主线程有没有可能看到 `done=1` 却读到尚未回填的 `counter`？
4. （可选）参考 `tests/stress/atomic/runtest.py` 的写法（[tests/stress/atomic/runtest.py:32-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/stress/atomic/runtest.py#L32-L52)）：用 `dump_file` 把 `counter` 所在内存 dump 出来，断言其值等于「线程数 × 每线程自增次数」。

**自检要点**：

- 你的 `atomic_add` 在 `store_sync` 失败时，是否跳回了 `load_sync`（而不是只重试 `store_sync`）？为什么必须回到 `load_sync`？（提示：失败时链接可能已失效，需要重新登记。）
- 你的 `done` 标志写在 `membar` 之后还是之前？顺序错了会怎样？

## 6. 本讲小结

- **LL/SC 语义**：`load_sync` 读值并登记地址、`store_sync` 条件写回（成功写 1、失败写 0 且不动内存）；监视粒度是 64 字节缓存行；成功判定 \( \text{success} = \text{valid} \land (L_{\text{store}} = L_{\text{linked}}) \)。
- **链接状态存在 L2**：因为共享的 L2 才能同时观察所有核的写，是裁决冲突的唯一正确位置；每线程全局一个链接槽，下标为 `{core, thread_id}`。
- **两遍协议**：L1 把同步访存当 cache miss 发往 L2，第一遍登记/裁决，线程挂起；响应回来唤醒线程，第二遍重发同一条指令取结果。`dd_load_sync_pending` 与 `restarted_sync_request` 分别在 L1 与 store 队列区分两遍。
- **store 队列是同步 store 的中枢**：入队、发送、挂起、锁存 `sync_success`、唤醒、清表项，全部在 `l1_store_queue` 内完成；表项每线程一条，索引即线程号。
- **反活锁设计**：失败的 `store_sync` 不失效链接（只有普通 store 与成功的 store_sync 才失效），避免竞争下的 livelock。
- **membar 是排序而非访存**：它复用 store 队列的回滚机制，等此前所有 store 落地，自身不入队任何表项；用最小硬件代价建立内存可见性顺序。

## 7. 下一步学习建议

- **u10-2 多线程调度与挂起恢复**：本讲反复出现的「挂起线程—L2 响应—位图唤醒」，将在 u10-2 里从调度器视角统一讲透，把 `thread_blocked`/`wake_bitmap` 与 `thread_en` 串成一条完整链路。
- **u10-3 多核与 L2 仲裁**：本讲的 `request_sync_slot = {core, id}` 已经是多核的影子。u10-3 会展开多核共享 L2 时的仲裁（`l2_cache_arb_stage` + `rr_arbiter`）与响应广播，是 LL/SC 在多核下成立的物理基础。
- **回顾 u8-3 协同仿真**：现在你应当能理解为何「store_sync + 中断」被列在协同仿真的盲区里——中断会插在两遍协议之间、取消整条同步操作（见 4.2.3 的 `dd_load_sync_pending` 中断处理），这种时序在锁步比对里难以对齐。
- **延伸阅读**：`software/libs/libos/bare-metal/schedule.c`（u9-2）里的 `__sync_bool_compare_and_swap` 底层就是 `load_sync`/`store_sync`；读它会看到本讲的硬件原语如何被编译器内建函数包装成上层同步 API。
