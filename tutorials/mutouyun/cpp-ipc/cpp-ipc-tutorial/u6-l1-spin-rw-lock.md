# spin_lock、rw_lock 与 yield/sleep 退避

> 前置承接：本讲建立在 [u3-l4（等待模型）](u3-l4-wait-model.md) 之上。你已经知道 `wait_for` 在「自旋」与「条件变量阻塞」之间架了桥，也听过 `yield` / `sleep` 的退避阈值是 4 / 16 / 32。但 u3-l4 把 `yield` / `sleep` 当作黑盒——它只说「先轻量自旋、够了再阻塞」，没打开它们的函数体，更没讲 `spin_lock` 与 `rw_lock` 这两把锁本身。本讲就拆开 [include/libipc/rw_lock.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h) 这个公共头文件，从最底层的硬件暂停指令 `IPC_LOCK_PAUSE_` 开始，逐层讲清退避工具与两把自旋锁的实现。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `IPC_LOCK_PAUSE_` 是什么、它如何按平台展开成 `pause` / `YieldProcessor` / `yield` 等硬件暂停指令，没有专用指令时又如何回退。
- 逐行讲清 `yield(k)` 的四级阶梯退避（空转 → PAUSE → yield → sleep）与阈值 4 / 16 / 32，并解释为什么不在第一次冲突就立即 `sleep`。
- 对比 `sleep(k)` 与 `yield(k)`：二者共享阈值 32，但「终点」不同——一个落进条件变量阻塞，一个落进 1 毫秒睡眠。
- 读懂 `spin_lock` 的 `exchange` 自旋实现，并指出它在库里的真实落点（保护 `id_pool`）。
- 读懂 `rw_lock` 如何用一个 32 位原子量同时编码「读者计数 + 写标志位」，并讲清它的写者优先逻辑。

## 2. 前置知识

进入源码前，先用三段话建立直觉。

**自旋锁（spin lock）是什么。** 普通互斥锁（如 `std::mutex`）争用失败时会陷入内核把线程挂起，单次开销在微秒级。自旋锁则在用户态反复读一个原子标志，「锁释放了吗？锁释放了吗？……」直到成功。它的优势是**低延迟**：一旦锁被释放，等待方在纳秒级就能感知并抢到，没有上下文切换；代价是**忙等占 CPU**——锁若长时间不释放，自旋的核就被白白烧满。所以自旋锁只适合「临界区极短、持有时间可预测」的场景。

**读写锁（read-write lock）解决什么。** 很多场景里访问是「读多写少」：多个线程可以安全地并发读同一份数据，只有写才需要独占。读写锁据此提供两种模式：`lock_shared`（共享锁，多个读者可同时持有）与 `lock`（独占写锁，与所有其他锁互斥）。libipc 的 `rw_lock` 进一步采用**写者优先**——一旦有写者排队，新来的读者必须等写者走完，避免源源不断的读者把写者饿死。

**退避（backoff）为什么是分级的。** 纯自旋锁在「锁马上就释放」时最优，但在「锁还要持有一会儿」时最浪费。libipc 的策略是**乐观假设冲突是瞬时的**：先纯空转几次（最快，假设马上就好），再发一条硬件 PAUSE 指令（告诉 CPU「我在自旋」，减少流水线与功耗开销），再让出 CPU 时间片（`yield`，给别的线程机会），最后才退到毫秒级睡眠。这就是阈值 4 / 16 / 32 的由来——冲突越久，让步越重。这条退避链是 `yield(k)`；而 `sleep(k)` 复用同一个阈值 32，但它的「终点」是条件变量阻塞，专门服务 [u3-l4](u3-l4-wait-model.md) 里的 `wait_for`。

> 与 u3-l4 的分工：u3-l4 讲 `yield` / `sleep` 在数据通路 `wait_for` 里**怎么用**；本讲讲它们的函数体**怎么写**，并补上 u3-l4 没碰的两把锁 `spin_lock` / `rw_lock`。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个公共头文件里。

| 文件 | 作用 |
| --- | --- |
| [include/libipc/rw_lock.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h) | 本讲唯一主角。一份文件同时定义了：硬件暂停指令 `IPC_LOCK_PAUSE_`、退避函数 `yield` / `sleep`、自旋锁 `spin_lock`、读写锁 `rw_lock`。 |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 真实落点：`chunk_info_t` 用 `spin_lock` 保护 `id_pool`（大消息外部存储，见 [u3-l3](u3-l3-large-message-storage.md)）；`chunk_storage_info` 用 `rw_lock` 的读写双路径管理 chunk 仓库；`wait_for` 用 `ipc::sleep` 做退避。 |
| [src/libipc/prod_cons.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h) | 无锁循环队列里 CAS 失败后调 `ipc::yield(k)` 让步（见 [u4](u4-l1-queue-abstraction.md)）。本讲只引用它作为 `yield` 的典型调用方。 |

## 4. 核心概念与源码讲解

### 4.1 硬件暂停指令 IPC_LOCK_PAUSE_

#### 4.1.1 概念说明

现代 CPU 为了让自旋锁「更省电、流水线更高效」，专门提供了一条**自旋提示指令**：x86 上叫 `PAUSE`，ARM 上叫 `YIELD`，Windows API 上叫 `YieldProcessor`。它的语义对程序逻辑没有可见影响（既不加锁也不让出 CPU），但会给 CPU 一个提示：「这段循环是在自旋等锁」。CPU 据此可以避免流水线过度投机执行、降低功耗，并在超线程（SMT）场景下把执行资源让给同一个物理核上的另一个硬件线程。

libipc 把这条指令抽象成一个宏 `IPC_LOCK_PAUSE_`，让上层退避代码不必关心平台差异。

#### 4.1.2 核心流程

宏的展开按「编译器 → 架构」两级匹配：

```
是 MSVC？               → YieldProcessor()           (Windows, 任意架构)
否则是 GCC 且 x86/x64？  → 内联汇编 "pause"
否则是 GCC 且 IA64？     → 内联汇编 "hint @pause"
否则是 GCC 且 ARM？      → 内联汇编 "yield"
以上都不命中？           → 回退为编译器栅栏 atomic_signal_fence(seq_cst)
```

兜底分支很关键：在没有专用暂停指令的平台上，至少用 `std::atomic_signal_fence(std::memory_order_seq_cst)` 做一个**编译器栅栏**，阻止编译器把空自旋循环优化掉。

#### 4.1.3 源码精读

宏定义用 `#pragma push_macro/pop_macro` 保护，避免污染用户的 `IPC_LOCK_PAUSE_` 宏命名空间。MSVC 分支如下：

[include/libipc/rw_lock.h:L18-L24](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L18-L24) —— Windows 上 `IPC_LOCK_PAUSE_()` 展开成 `YieldProcessor()`。

GCC 下再按 CPU 架构细分，x86/x64 分支最具代表性：

[include/libipc/rw_lock.h:L25-L32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L25-L32) —— GCC + x86/x64 时，`IPC_LOCK_PAUSE_()` 是一条内联汇编 `__asm__ __volatile__("pause")`。注释里贴了 Intel SDM 手册的页码作为依据。ARM 分支（[L40-L46](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L40-L46)）换成 `"yield"`，IA64 分支换成 `"hint @pause"`。

最后的兜底：

[include/libipc/rw_lock.h:L49-L54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L49-L54) —— 若上面所有条件都不命中（例如某些冷门架构），`IPC_LOCK_PAUSE_()` 回退为 `std::atomic_signal_fence(std::memory_order_seq_cst)`。注意这是「信号栅栏」而非真正的 CPU 指令，它的实际作用是阻止编译器把 `for(;;)` 自旋循环优化成死循环或删除循环体。

#### 4.1.4 代码实践

**实践目标**：确认你本机编译器会让 `IPC_LOCK_PAUSE_` 走哪条分支。

**操作步骤**：

1. 打开 [rw_lock.h:L18-L54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L18-L54)，对照你的编译器（GCC 还是 MSVC）与 CPU 架构。
2. 用一条预处理命令打印出 GCC 预定义的架构宏，判断命中哪条分支：

   ```bash
   # 示例命令：查看 GCC 在本机预定义了哪些架构宏（待本地验证）
   echo | g++ -dM -E - | grep -E '__x86_64__|__i386__|__arm__|__ia64__'
   ```

3. 若输出含 `__x86_64__`，则 `IPC_LOCK_PAUSE_()` 是 `pause` 指令；若含 `__arm__` 则是 `yield` 指令。

**需要观察的现象**：在主流 x86 Linux 与 ARM（如树莓派、Apple Silicon 跨编译）上，命中的分支应不同。

**预期结果**：x86_64 Linux → `__x86_64__` 命中 → `pause` 指令。若你用的是上述脚本未覆盖的架构，则会落到 L49 的编译器栅栏兜底。

#### 4.1.5 小练习与答案

**练习 1**：为什么兜底分支用 `atomic_signal_fence` 而不是直接留空（`{}`）？
**答案**：留空时，编译器可能判定 `for(k...) yield(k)` 循环体「无副作用」从而把它优化掉或重排，破坏自旋等待语义。`atomic_signal_fence(seq_cst)` 作为编译器栅栏，能阻止这种优化，保证循环体确实被执行。

**练习 2**：`PAUSE` 指令会改变程序的可见行为（比如让锁的获取顺序发生变化）吗？
**答案**：不会。它只是性能提示，对程序逻辑等价于空操作（nop），不参与任何内存序或同步关系。

---

### 4.2 yield(k)：四级阶梯退避

#### 4.2.1 概念说明

`yield(k)` 是 libipc 里**纯自旋场景**（`spin_lock`、`rw_lock`、无锁队列的 CAS 循环）统一调用的退避函数。它带一个「冲突计数器」`k`：每冲突一次 `k` 加一，`k` 越大，让步越重。它的核心思想是 **乐观假设冲突是瞬时的**——绝大多数锁竞争在几次重试内就解决，所以先用最便宜的手段；只有冲突持续很久时，才逐步升级到更贵的让步方式。

#### 4.2.2 核心流程

`yield(k)` 把 `k` 划成四级阶梯：

| k 的范围 | 行为 | 单次代价 | 累计迭代数 |
| --- | --- | --- | --- |
| `[0, 4)` | 什么都不做（纯空转） | 极低（几个时钟周期） | 4 次 |
| `[4, 16)` | 发 `IPC_LOCK_PAUSE_()` 指令 | 低（硬件暂停提示） | 12 次 |
| `[16, 32)` | `std::this_thread::yield()` 让出时间片 | 中（操作系统调度） | 16 次 |
| `[32, ∞)` | `sleep_for(1ms)` 后**直接 return** | 高（挂起约 1ms） | 之后每次都睡 1ms |

```
冲突 0~3 次:  纯空转        ┐
冲突 4~15 次: PAUSE 指令     ├─ 共约 32 次「快速」重试
冲突 16~31 次: yield 让出    ┘
冲突 ≥32 次:  每次 sleep 1ms ── 退化为周期性轮询
```

注意末尾的细节：进入 `sleep_for` 分支后函数**立即 return，不再 `++k`**，所以 `k` 停在 32 不再增长——此后每次调用都直接睡 1 毫秒。前三个分支都会落到函数末尾的 `++k`。

#### 4.2.3 源码精读

[include/libipc/rw_lock.h:L62-L74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L62-L74) —— `yield` 的完整实现。`K` 是计数器类型（实际调用处都是 `unsigned k`）。注意 `else { sleep_for(1ms); return; }` 的提前返回：它跳过了末尾的 `++k`，使 `k` 钉在 32。

典型调用方：无锁队列的 CAS 抢占循环。例如 `single-multi` 单播变体里，读者用 `compare_exchange_weak` 抢占读游标，失败就调 `yield(k)`：

[src/libipc/prod_cons.h:L95-L100](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L95-L100) —— `pop` 中 CAS 抢占 `rd_` 失败后 `ipc::yield(k)`，把让步策略完全交给本讲这个函数。

#### 4.2.4 代码实践

**实践目标**：亲手填出 `yield(k)` 的四级阈值表，并论证「为何不在第一次冲突就立即 sleep」。

**操作步骤**：

1. 通读 [rw_lock.h:L62-L74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L62-L74)，把 4.2.2 的表格逐行对着源码核验一遍（尤其是 `return` 与 `++k` 的位置）。
2. 用下面这段**示例代码**（非项目原有代码）模拟一次长冲突，打印 `k` 与对应分支：

   ```cpp
   // 示例代码：仅用于观察 yield 的阶梯行为，不是项目原有代码
   #include "libipc/rw_lock.h"
   #include <cstdio>
   int main() {
       unsigned k = 0;
       for (int i = 0; i < 40; ++i) {
           const char* tag =
               (k < 4)  ? "spin   " :
               (k < 16) ? "pause  " :
               (k < 32) ? "yield  " : "sleep1ms";
           std::printf("iter=%2d k=%2u -> %s\n", i, k, tag);
           ipc::yield(k);   // 注意：k>=32 后每次会真实睡眠 1ms
       }
   }
   ```

3. 编译运行（需把 `include/` 加入头文件路径）：

   ```bash
   # 待本地验证
   g++ -std=c++17 -Iinclude demo_yield.cpp -pthread -o demo_yield && ./demo_yield
   ```

**需要观察的现象**：前 4 次是 `spin`（瞬时返回），第 5~16 次是 `pause`，第 17~32 次是 `yield`，第 33 次起每次输出之间间隔约 1 毫秒。

**预期结果**：你会在 `iter≥32` 之后明显感到程序变慢——因为真的开始每轮睡 1ms（40 次里后 8 次约多花 8ms）。

**论证「为何不在第一次冲突就立即 sleep」**：自旋锁的全部价值在于「锁马上就释放」时用纳秒级开销抢到。第一次冲突就 `sleep_for(1ms)` 意味着每次竞争至少白等 1 毫秒，彻底摧毁低延迟卖点。真实负载下，绝大多数冲突在头几次空转或 PAUSE 内就解决（k 远到不了 16），分级退避正是为了在「快路径高效」与「长冲突不烧满 CPU」之间取得平衡。

#### 4.2.5 小练习与答案

**练习 1**：如果改成「k < 1 就 sleep」，对短临界区自旋锁的性能影响是什么？
**答案**：每次锁竞争都至少多 1ms 延迟，自旋锁相对 `std::mutex` 的低延迟优势荡然无存，退化为比互斥锁更差（既睡了又没有内核帮忙排队）。

**练习 2**：为什么 `[16,32)` 这一段用 `std::this_thread::yield()` 而不是直接 `sleep_for`？
**答案**：`yield()` 只是「让出当前时间片」，操作系统可能马上重新调度本线程，开销远小于 `sleep_for`（后者会真正挂起、至少等一次时钟中断）。在中等等级的冲突下，`yield` 既给了别的线程机会，又保持低延迟，是承上启下的中间档。

---

### 4.3 sleep(k)：与 yield 共享阈值的忙等闸门

#### 4.3.1 概念说明

`sleep(k)` 名字容易误导——它**不是**用来在自旋锁里睡觉的，而是 [u3-l4](u3-l4-wait-model.md) 里 `wait_for` 模板的退避伙伴。它和 `yield(k)` 共享同一个阈值 `N=32`，但「终点」完全不同：

- `yield` 的终点是 `sleep_for(1ms)`（纯自旋场景，没有条件变量可用）。
- `sleep` 的终点是**调用者传入的回调 `f`**，在 `wait_for` 里这个回调就是条件变量阻塞 `waiter.wait_if(...)`。

换句话说，`sleep` 是一个「先轻量自旋重试谓词 N 次，再转入真正的内核阻塞」的闸门。

#### 4.3.2 核心流程

`sleep` 有两个重载。带回调的版本（核心）：

```
k < 32 ?  yield() 然后 ++k        ← 轻量自旋，给谓词一次重试机会
        : 调用回调 f() 然后 return ← 转入真正的阻塞（不 ++k）
```

不带回调的版本把回调默认成 `sleep_for(1ms)`，于是退化成「自旋 32 次后每轮睡 1ms」——和 `yield` 的终点一致，只是阶梯更简单（没有 PAUSE 中间档）。

#### 4.3.3 源码精读

[include/libipc/rw_lock.h:L76-L86](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L76-L86) —— 带回调的 `sleep<N=32>`。`k < N` 时 `yield()` 并 `++k`；否则调用回调 `f()` 后 `return`（同样跳过 `++k`，使 `k` 钉在阈值）。模板参数 `N` 默认 32，与 `yield` 的阈值呼应。

[include/libipc/rw_lock.h:L88-L93](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L88-L93) —— 无回调重载，回调默认为 `sleep_for(1ms)`。

最关键的调用点是 `wait_for` 模板，它把条件变量阻塞塞进回调里：

[src/libipc/ipc.cpp:L379-L391](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L379-L391) —— `wait_for` 在谓词仍不满足时调 `ipc::sleep(k, [...] { ret = waiter.wait_if(pred, tm); k = 0; })`。前 32 次只是 `yield()` 后回头重试谓词（带副作用的 `push`/`pop`），仍不满足才真正在 `wait_if` 里阻塞；被唤醒后回调把 `k` 清零，外层据此判断是「被条件变量唤醒」还是「超时」。

#### 4.3.4 代码实践

**实践目标**：讲清 `sleep` 与 `yield` 在 `wait_for` 里的协作，以及为什么 `sleep` 要在回调里把 `k` 清零。

**操作步骤**：

1. 对照 [ipc.cpp:L379-L391](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L379-L391)，跟踪一次「队列空、接收方 `recv`」的流程：谓词「`pop` 失败」为真 → 进 `sleep` → 前 32 轮 `yield` 后重试 `pop` → 仍空 → `wait_if` 阻塞。
2. 回答：为什么回调里要 `k = 0`？

**需要观察的现象**：在源码层面理解「32 次轻量重试 + 一次真阻塞」的节奏。

**预期结果**：`k = 0` 是为了让外层 `if (k == 0) break;` 识别出「回调确实执行过（说明走过条件变量）」，从而在被唤醒且谓词已满足时跳出循环；若 `k` 仍是 32，说明根本没进过回调（始终在自旋），则继续循环。这是 `wait_for` 区分「自旋成功」与「被唤醒成功」的关键。

#### 4.3.5 小练习与答案

**练习 1**：`sleep` 的模板参数 `N` 为什么默认取 32，和 `yield` 一样？
**答案**：二者服务于同一种「乐观假设冲突瞬时」的哲学，统一阈值便于心智模型——无论纯自旋还是 `wait_for`，都是「先快速重试约 32 次，再升级」。`sleep` 用模板参数 `N` 暴露出来，是为了允许调用方按需调整，而 `yield` 的阈值是写死的。

**练习 2**：如果不带回调的 `sleep(k)` 被误用在 `wait_for` 里会发生什么？
**答案**：谓词不满足时不会阻塞在条件变量上，而是每轮睡 1ms 轮询，既浪费 CPU 又无法被 `broadcast` 即时唤醒，丢失唤醒会拖高延迟——这正是 `wait_for` 必须传入 `wait_if` 回调而非用无回调重载的原因。

---

### 4.4 spin_lock：exchange 自旋锁

#### 4.4.1 概念说明

`spin_lock` 是 libipc 最简单的同步原语：一个 32 位原子量 `lc_`，`0` 表示空闲、非 `0` 表示被占。它用 `exchange` 原子操作实现 test-and-set 语义，失败时调用 4.2 的 `yield(k)` 退避。它不是给用户直接做粗粒度互斥的，而是库内部用在**极短、极热**的临界区上。

#### 4.4.2 核心流程

```
lock():   k=0
          循环: old = exchange(lc_, 1, acquire)
                若 old==0 → 成功拿到锁（lc_ 已被置 1），退出
                否则       → yield(k) 退避后重试
unlock(): store(lc_, 0, release)
```

注意 `exchange` 的特点：**无论成败都会写入 1**。这比「先读、读到 0 才写」的 test-and-test-and-set 更简单（少一次分支与读），代价是每次失败尝试都会产生一次写操作、触发缓存行所有权转移。对于临界区极短、竞争不激烈的场景，这点写流量换来的代码简洁是划算的。

#### 4.4.3 源码精读

[include/libipc/rw_lock.h:L101-L114](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L101-L114) —— `spin_lock` 全部代码。`lock` 的 `for` 循环把「条件 / 增量」写在头部：条件 `lc_.exchange(1, acquire)` 返回旧值，旧值非 0 即继续；增量位置写 `yield(k)`，每次冲突让步并 `++k`。`unlock` 用 `release` 序保证临界区内的写对下一个获取者可见。

库里最典型的真实落点：大消息外部存储的 `chunk_info_t` 用一把 `spin_lock` 保护 `id_pool`（见 [u3-l3](u3-l3-large-message-storage.md)）。`id_pool` 的 `acquire`/`release` 是 O(1) 链表操作，临界区极短，正适合自旋锁：

[src/libipc/ipc.cpp:L197-L199](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L197-L199) —— `chunk_info_t` 成员 `ipc::spin_lock lock_;`。

[src/libipc/ipc.cpp:L283-L287](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L283-L287) —— `acquire_storage` 在锁保护下 `pool_.prepare()` + `pool_.acquire()` 拿一个唯一 id，然后立刻解锁。锁只罩住「分配 id」这一步，写数据在锁外进行。

#### 4.4.4 代码实践

**实践目标**：体会 `spin_lock` 适合「极短临界区」，并验证它在 `acquire_storage` 里只罩住 `id_pool` 操作。

**操作步骤**：

1. 打开 [ipc.cpp:L283-L287](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L283-L287) 与 [L316-L318](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L316-L318)（`release_storage` 同样锁内 `pool_.release`），确认 `lock_.lock()/unlock()` 只包住 `id_pool` 调用。
2. 用下面这段**示例代码**（非项目原有代码）观察 `spin_lock` 在两线程争用下的行为，并对比把临界区拉长后 CPU 飙升：

   ```cpp
   // 示例代码：观察 spin_lock 争用，非项目原有代码
   #include "libipc/rw_lock.h"
   #include <thread>
   #include <atomic>
   #include <cstdio>
   int main() {
       ipc::spin_lock lk;
       std::atomic<long> sum{0};
       auto worker = [&] {
           for (int i = 0; i < 1'000'000; ++i) {
               lk.lock();
               sum += 1;          // 极短临界区
               lk.unlock();
           }
       };
       std::thread a(worker), b(worker);
       a.join(); b.join();
       std::printf("sum=%ld (expect 2000000)\n", sum.load());
   }
   ```

3. 编译运行，并观察单核 CPU 占用：

   ```bash
   # 待本地验证
   g++ -std=c++17 -Iinclude demo_spin.cpp -pthread -O2 -o demo_spin && ./demo_spin
   ```

**需要观察的现象**：`sum` 应为 2000000（自旋锁保证累加原子可见）。争用期间一个核会接近 100%（忙等）。把 `sum += 1` 换成更长的计算，CPU 占用基本不变但耗时会上升。

**预期结果**：正确性达成；直观感受「自旋锁适合短临界区、长临界区会烧满 CPU」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `spin_lock::lock` 用 `exchange` 而不是 `compare_exchange`？
**答案**：`exchange` 一条原子指令就能完成 test-and-set，代码最简洁；`compare_exchange` 需要先读再条件写。在临界区极短、竞争温和时，`exchange` 额外产生的写流量可忽略，简洁性更重要。若竞争激烈、写流量成为瓶颈，可考虑 test-and-test-and-set（先读，只在读到 0 时才 exchange）。

**练习 2**：`lock` 与 `unlock` 的内存序分别是 `acquire` 与 `release`，少了哪一半？为什么仍然正确？
**答案**：`exchange(acquire)` 提供 acquire 语义，`store(release)` 提供 release 语义，acquire-release 配对完整，能保证临界区内写对下一个获取者可见。没有用 seq_cst 是因为自旋锁只需要「获取者看到释放者临界区的写」，不需要全局顺序，用更轻的 acquire/release 足够。

---

### 4.5 rw_lock：单原子量编码的读写锁

#### 4.5.1 概念说明

`rw_lock` 是本讲最精巧的部分：它用**一个 32 位原子量** `lc_` 同时编码「当前有多少个读者」和「是否有写者」。诀窍是把 32 位拆成两段——最高位（bit 31）当**写标志位** `w_flag`，其余位（bit 0~30）当**读者计数** `w_mask`。于是：

- 共享锁（读）：`lc_` 的低 31 位 `+1`。
- 独占锁（写）：置最高位 `w_flag`。

它还实现了**写者优先**：写者一旦置上 `w_flag` 排队，新来的读者就会看到该标志而自旋等待，不会插队把写者饿死。

#### 4.5.2 核心流程

先看两个魔法常量怎么来的（`lc_ui_t` = `uint32_t`）：

\[
\text{w\_mask} = \texttt{numeric\_limits<int32\_t>::max()} = \texttt{0x7FFFFFFF} \quad(\text{低 31 位全 1})
\]

\[
\text{w\_flag} = \text{w\_mask} + 1 = \texttt{0x80000000} \quad(\text{仅最高位为 1})
\]

**写锁 `lock()`** 分两阶段：

```
阶段一: 抢「写者槽位」(对抗其他写者)
   循环: old = fetch_or(lc_, w_flag, acq_rel)
         old == 0          → 没人占用，直接拿到，return
         old 有 w_flag     → 别的写者占着，yield 重试
         old 无 w_flag但非0 → 有读者在，本写者已置上 w_flag，break 进阶段二

阶段二: 等现有读者读完
   循环: 只要 lc_ & w_mask != 0（还有读者），就 yield
         读者全部 drain 后，lc_ 只剩 w_flag，循环退出，本写者持锁
```

**为什么是写者优先**：写者在阶段一用 `fetch_or(w_flag)` 把标志位「焊」上；此后任何新读者调 `lock_shared` 都会看到 `w_flag` 而自旋，无法插队。只有**已经在锁内**的旧读者能继续读到自然 drain 完。

**读锁 `lock_shared()`**：加载 `lc_`，若 `w_flag` 置位则 yield 重试；否则 CAS 把 `lc_` 加 1（增加一个读者），失败则用更新后的旧值重试。

**解锁**：`unlock()` 直接 `store(0)`（写者持锁期间没有读者，`lc_` 必然只剩 `w_flag`，清零即可）；`unlock_shared()` 用 `fetch_sub(1)` 减一个读者。

#### 4.5.3 源码精读

常量定义，注释里贴心地标了二进制：

[include/libipc/rw_lock.h:L116-L124](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L116-L124) —— `lc_` 为 `atomic<uint32_t>`；`w_mask` 取「有符号 32 位最大值」即低 31 位全 1，`w_flag = w_mask + 1` 即最高位。

写锁两阶段：

[include/libipc/rw_lock.h:L134-L145](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L134-L145) —— `lock()`。注意 `if (!old) return;` 是无竞争快路径；`if (!(old & w_flag)) break;` 是「有读者、无写者」时跳出阶段一进阶段二；最后那个空载 `for` 是阶段二等读者 drain。

读锁与解锁：

[include/libipc/rw_lock.h:L151-L169](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L151-L169) —— `lock_shared` 用 `compare_exchange_weak(old, old+1)` 自增读者计数；`unlock_shared` 用 `fetch_sub(1, release)`。

库里真实落点：`chunk_storage_info` 用一个静态 `rw_lock` 保护 `chunk_storages()` 这个 map 的查找与插入，典型的「读多写少」：

[src/libipc/ipc.cpp:L262-L268](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L262-L268) —— 先持 `std::shared_lock<ipc::rw_lock>` 走只读查找路径（快，并发友好）；未命中时 `unlock()` 切到 `std::lock_guard<ipc::rw_lock>` 独占写路径做 `emplace`。这正是读写锁「读路径共享、写路径独占」的标准用法。

#### 4.5.4 代码实践

**实践目标**：手动演算 `rw_lock` 的写者优先路径，并在源码里找到它的「读多写少」真实用法。

**操作步骤**：

1. 假设初始 `lc_ = 0`。两个读者 A、B 先后 `lock_shared`：A 的 CAS 把 `lc_` 从 0 改成 1，B 改成 2。此刻 `lc_ = 2`（2 个读者）。
2. 写者 W 调 `lock()`：`fetch_or(w_flag)` 返回 `old = 2`（无 `w_flag`），W 置上 `w_flag`，`lc_ = 0x80000002`，`break` 进阶段二；阶段二等到两个读者各 `fetch_sub(1)` 后 `lc_ = 0x80000000`，`lc_ & w_mask == 0`，W 持锁。
3. 在 W 持锁期间，新读者 C 调 `lock_shared`：加载到 `old & w_flag` 非零 → 自旋等待，无法插队。这就是写者优先。
4. 打开 [ipc.cpp:L262-L268](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L262-L268)，确认 `shared_lock` → `lock_guard` 的「读升级写」模式。

**需要观察的现象**：在纸面演算中，`w_flag` 一旦被写者置上，后续读者的 `lock_shared` 必然看到它而阻塞。

**预期结果**：你能用自己的话讲清「阶段一抢写者槽位、阶段二等读者 drain、期间新读者被挡」三件事，并理解为何 `unlock()` 用 `store(0)` 而非「只清 `w_flag`」——因为写者持锁时读者数为 0，`lc_` 只剩 `w_flag`，直接清零最简单且正确。

#### 4.5.5 小练习与答案

**练习 1**：两个写者同时调 `lock()`，会不会出现「两个写者都拿到锁」的 bug？
**答案**：不会。`fetch_or(w_flag)` 是原子的，两个写者被串行化：先执行的那个看到 `old` 无 `w_flag`（读者或全空），置位后 `break` 进阶段二；后执行的那个看到 `old` 已含 `w_flag`，于是在阶段一 `yield` 自旋，不会 `break`。因此同一时刻只有一个写者能进入阶段二、最终持锁。

**练习 2**：把读者计数放在「低 31 位」最多能支持多少个并发读者？这个上限对 `chunk_storage_info` 的用法有影响吗？
**答案**：低 31 位最大可表示 \(2^{31}-1\) 个读者，约 21 亿，远超任何实际并发量，对 `chunk_storage_info`（保护一个进程内 map）毫无影响。选 31 位而非更少，纯粹是因为「把最高位独立出来当写标志」最自然、位运算最简单。

---

## 5. 综合实践

把本讲四块内容串起来：**分析 `acquire_storage` 在高并发大消息下的自旋锁退避行为**。

背景：当多个发送方同时发送大消息（走 [u3-l3](u3-l3-large-message-storage.md) 的外部存储），它们会竞争同一把 `chunk_info_t::lock_`（一把 `spin_lock`）来从 `id_pool` 分配 chunk id。这是一段极短但可能高竞争的临界区。

任务：

1. **定位临界区**：读 [ipc.cpp:L283-L287](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L283-L287)，确认 `spin_lock` 只罩住 `pool_.prepare()` + `pool_.acquire()`，而写数据（[L289-L292](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L289-L292)）在锁外——这是短临界区设计的体现。
2. **推理退避路径**：当 N 个发送方同时抢这把锁，失败的线程会走进 `spin_lock::lock` 的 `yield(k)`。结合 4.2，说明前 4 次冲突纯空转、4~16 次发 PAUSE、16~32 次 yield、超过 32 次每轮睡 1ms。
3. **动手实验**（示例代码，待本地验证）：写一个小程序，开 8 个线程各调用一段「`spin_lock::lock` → 空循环 100 次 → `unlock`」模拟 `id_pool` 操作，用 `perf stat` 或 `/usr/bin/time -v` 观察吞吐与 CPU 占用；再把临界区内的空循环从 100 改到 100000，观察自旋锁性能如何坍塌。
4. **对比 `rw_lock`**：回到 [ipc.cpp:L262-L268](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L262-L268)，思考为什么 map 查找用 `rw_lock` 而 `id_pool` 用 `spin_lock`——前者是「读多写极少且可能并发读」，后者是「每次都要独占、但临界区极短」。

**验收标准**：你能解释「短临界区配 spin_lock + yield 退避、读多写少配 rw_lock」这条选型准则，并说出把 `spin_lock` 用在长临界区上的后果。

## 6. 本讲小结

- `IPC_LOCK_PAUSE_` 是硬件自旋提示指令的跨平台抽象：x86→`pause`、ARM→`yield`、Windows→`YieldProcessor`，无专用指令时回退为编译器栅栏防止循环被优化。
- `yield(k)` 用阈值 4 / 16 / 32 把退避分成「空转 → PAUSE → yield → 1ms 睡眠」四级，体现「乐观假设冲突瞬时」的哲学——绝大多数冲突在前几档就解决，所以不在第一次冲突就 sleep。
- `sleep(k)` 与 `yield(k)` 共享阈值 32 但终点不同：前者在 `wait_for` 里转入条件变量阻塞（回调里还会把 `k` 清零以区分「自旋成功」与「被唤醒」），后者落进 1ms 轮询。
- `spin_lock` 用 `exchange(acquire)` 做 test-and-set、`store(release)` 解锁，失败时调 `yield(k)`；它专用于库内极短临界区，真实落点是 `chunk_info_t` 保护 `id_pool`。
- `rw_lock` 用单个 32 位原子量编码状态：最高位 `w_flag` 是写标志、低 31 位 `w_mask` 是读者计数；写者分「抢槽位 + 等读者 drain」两阶段，期间新读者被挡，实现写者优先；真实落点是 `chunk_storage_info` 的「shared_lock 查找 / lock_guard 插入」双路径。

## 7. 下一步学习建议

本讲讲的是**用户态自旋型**同步原语，它们都假设「持有者正常解锁」。但跨进程共享内存里，持有锁的进程可能**突然崩溃**——自旋锁会因此死锁。下一讲 [u6-l2 健壮互斥量](u6-l2-robust-mutex.md) 将讲清跨进程的**健壮锁**：Linux/POSIX 用 robust mutex 检测 `EOWNERDEAD`、Windows 用 abandoned mutex 检测 `WAIT_ABANDONED`，在持有者死亡后自动恢复一致性。读完后建议继续 [u6-l3 condition 与 semaphore](u6-l3-condition-semaphore.md) 与 [u6-l4 detail::waiter](u6-l4-waiter.md)，把本讲的 `yield`/`sleep` 退避与 `wait_for` 的条件变量阻塞串成完整的等待链路。
