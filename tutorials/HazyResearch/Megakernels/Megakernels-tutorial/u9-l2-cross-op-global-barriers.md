# 跨 op 全局屏障：Bar + atomicAdd 自旋

> 阶段：advanced · 依赖：u9-l1（跨 op 数据依赖与全局内存同步的动机）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `globals.Bar` 这个三维计数器张量的索引 `[layer, opcode-1, head]` 每一维的含义，并解释为什么中间一维是「opcode-1」。
2. 在地图上画出一个生产者 op 如何用 `atomicAdd` 往自己的槽里「投票」，一个消费者 op 又如何读到对方投满的信号。
3. 理解「计数阈值」是怎么算出来的——为什么 attention 等待 Q/K/V 时阈值是 `4`，而 RMS_QKV 等待上一层 DownProj 时阈值是 `512`。
4. 看懂 `gmem_wait` 这种「`volatile` 读 + `__nanosleep` 自旋」的等待模式，以及它和框架内部 semaphore 的区别。
5. 亲手画出「单层内 QKV → PartialAttention」的 Bar 计数流转图。

## 2. 前置知识

在讲机制前，先用大白话对齐几个概念。

**什么是「跨 op 同步」？**

在 Megakernels 里，一个 token 的一次前向被拆成一条由 op 组成的流水线（RMS_QKV → PartialAttention → AttentionReduction → …）。这些 op 被**调度到不同的 SM 上并发执行**。于是会出现这样的依赖：

> PartialAttention 要算 \(Q K^\top\)，它必须等 RMS_QKV 把 \(Q\) 写完、把 \(K/V\) 追加进 KV cache 之后才能开始。

问题是：RMS_QKV 和 PartialAttention 跑在**不同的 SM、不同的 thread block** 上，GPU 并没有一条「跨 block、跨 SM」的轻量同步原语。`__syncthreads()` 只能同步一个 block 内的线程；cluster barrier（`cooperative_groups`）范围也有限。于是 Megakernels 选择了一个非常朴素但有效的办法：

> **在全局内存里放一个计数器数组，生产者做完一块就 `atomicAdd` 投一票，消费者在一个 `while` 循环里反复读这个计数器，直到它涨到期望值。**

这就是本讲的主角 `globals.Bar`（Bar = Barrier）。

**它和框架内部的 semaphore 有什么不同？**

你可能已经见过 op 内部的 `kittens::semaphore`（如 `Q_arrived`、`K_arrived`），那是**同一个 thread block 内** loader/consumer/storer 之间的共享内存信号量，走的是 GPU 的 mbarrier 硬件。本讲的 `Bar` 完全不同：它**存在全局内存（global memory）里**，用于**不同 SM、不同 op 之间**传递「我做完了」的消息。代价是慢（要过 L2/显存），但好处是任意 SM 都能读写。

**两个名词约定**

- **生产者（producer）**：往 `Bar` 里 `atomicAdd`、负责「发信号」的 op。
- **消费者（consumer）**：在 `gmem_wait` 里自旋读 `Bar`、负责「等信号」的 op。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `demos/low-latency-llama/llama.cuh` | 定义 opcode 枚举、模型维度常量、`globals_t`，以及 `Bar` 张量的类型与成员。 |
| `demos/low-latency-llama/rms_matvec_rope_append.cu` | 生产者 RMS_QKV op：`atomicAdd` 写 Bar；以及它作为消费者等待上一层 DownProj 的 `gmem_wait`（`EXPECTED_ARRIVAL_COUNT=512`）。 |
| `demos/low-latency-llama/attention_partial.cu` | 消费者 PartialAttention op：`wait_for_kv` 与 consumer 的自旋等待（阈值 `4`）；它又是 AttentionReduction 的生产者。 |
| `demos/low-latency-llama/matvec_adds.cu` | DownProj/O_Proj 等 `MatVecAddOp` 的通用模板，展示了「向量化计数 `atomicAdd(..., inst.iters)`」的另一种写法。 |
| `include/config.cuh` | 自旋睡眠参数 `GMEM_SPIN_LOOP_SLEEP_NANOS`。 |
| `megakernels/demos/latency/python_vm.py` | Python 参考 VM，用 `assert` 锁定了每个 barrier 应当到达的计数值，是我们理解阈值的「答案卷」。 |
| `megakernels/generators.py` | 每个 token 生成前把 `Bar` 清零的宿主逻辑。 |

---

## 4. 核心概念与源码讲解

### 4.1 Bar 张量布局：`[layer, opcode-1, head]` 三维计数器

#### 4.1.1 概念说明

`Bar` 的本质是一堆 `uint` 计数器。它的类型在 `llama.cuh` 里这样声明：

```cpp
// num_layers by 6 ops per layer by up to 48 heads (Q + K + V)
using barriers =
    kittens::gl<uint, 1, -1, -1, num_attention_heads + 2 * num_kv_heads>;
```

[llama.cuh:104-105](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L104-L105) 定义了 barriers 的逻辑布局（`kittens::gl` 是 kittens 的多维张量视图）。把它拍平成三维就是：

\[ \text{Bar}[\text{layer},\ \text{opcode}-1,\ \text{head}] \]

每一维的含义：

- **第 1 维 `layer`**：哪一层。屏障是按层隔离的——第 \(L\) 层的计数不会污染第 \(L+1\) 层。注意跨层依赖时消费者读的是 `layer_idx - 1`（见 4.3）。
- **第 2 维 `opcode-1`**：**生产者 op 的编号减 1**。每个 op 写入「自己的」那一槽；想等它的下一个 op 就去读这一槽。`-1` 只是因为 opcode 从 1 开始计数，而数组下标从 0 开始。
- **第 3 维 `head`**：同一层同一 op 内部，再按「头」细分。对 QKV 来说这一维特别重要：32 个 Q 头 + 8 个 K 头 + 8 个 V 头，正好排成 `num_attention_heads + 2*num_kv_heads = 32+16 = 48` 个槽位。

为什么用 `opcode-1` 当槽位编号？因为**模型的 op 顺序是固定的流水线**，op 之间是严格的「前驱→后继」依赖。把每个 op 映射到一个固定槽位，后继只要知道前驱的 opcode，就能直接定位到信号。这套约定让索引变得纯粹是「编译期常量 + 指令参数」的组合，不需要运行时查表。

#### 4.1.2 核心流程：一条边上的计数流转

一条「生产者 P → 消费者 C」的边，其计数生命周期是：

1. **初始化**：每个 token 开始前，宿主把整张 `Bar` 清零（见 4.1.3）。
2. **生产者投票**：P 每完成一个最小工作块（对 QKV 是一个 16 元素块；对 DownProj 是一段输出），就 `atomicAdd(&Bar[layer, P.opcode-1, h], n)`。
3. **消费者自旋**：C 在 `gmem_wait` 里 `while(Bar[layer, P.opcode-1, h] < 期望值) __nanosleep(...)`。
4. **放行**：计数达到期望值，C 退出循环，开始读 P 写的数据。

关键在于：**P 写的槽位 = C 读的槽位**，都是 `Bar[layer, P.opcode-1, h]`。它们靠「同一个 opcode 常量 + 同一个 head」天然对齐。

opcode → 槽位的映射表（opcode 来自 [llama.cuh:7-13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L7-L13)）：

| 生产者 op | opcode | 写入槽 `opcode-1` | 后继消费者 | 读取槽 | 期望计数值 |
|---|---|---|---|---|---|
| RMS_QKV | 1 | 0 | PartialAttention | 0 | 4（每个头） |
| PartialAttention | 2 | 1 | AttentionReduction | 1 | num_partials（每个头） |
| AttentionReduction | 3 | 2 | O_ProjResidual | 2 | 128 |
| O_ProjResidual | 4 | 3 | RMS_DoubleMatVecSiLU(upgate) | 3 | 128 |
| RMS_DoubleMatVecSiLU | 5 | 4 | DownProjResidual | 4 | 512 |
| DownProjResidual | 6 | 5 | 下一层 RMS_QKV / RMS_LM_Head | 5 | 512 |

> 表中的「期望计数值」由 Python 参考 VM 的 `assert` 锁定，是本讲要重点推导的「阈值」。后两行会在 4.3 详述。

#### 4.1.3 源码精读

**`Bar` 的成员声明**与上面对应的类型：

[llama.cuh:108](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L108) —— `barriers Bar;`，作为 `globals_t` 的成员，被映射进全局内存，对所有 SM 可见。

**`Bar` 的清零**（每个 token 一开始）：宿主在生成循环里调用：

[generators.py:116](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L116) —— `self.schedule.globs.barriers.fill_(self.barrier_fill_val)`，默认 `barrier_fill_val=0`（[generators.py:101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L101)）。Python 参考 VM 里则是 `barriers.zero_()`（[generators.py:188](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L188)）。

这一点至关重要：**`Bar` 是「一次性累积」的计数器，不随工作块回收**。一个 token 的整条流水线跑完后，计数器就停留在最大值；下一个 token 必须先清零，否则消费者第一次 `while(count < 期望值)` 就会直接通过（因为旧值已经够了）——这会导致读到上个 token 的脏数据。所以清零是正确性的前提。

#### 4.1.4 代码实践：画出 QKV → PartialAttention 的索引对齐

**实践目标**：亲手验证「生产者的 `block_idx/4`」和「消费者的 head 下标」是同一套坐标。

**操作步骤**：

1. 打开 [rms_matvec_rope_append.cu:15-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L15-L17)，记下三个常量：

   - `K_BLK_START = 2048 / 16 = 128`：Q 块的下标范围是 `[0, 128)`。
   - `V_BLK_START = 2560 / 16 = 160`：K 块的下标范围是 `[128, 160)`。
   - V 块下标范围 `[160, 192)`（2560 起，8 个 V 头 × 64 维 / 16 = 32 块，到 3072/16=192）。

2. 生产者写入用的 head 下标是 `block_idx / 4`（[rms_matvec_rope_append.cu:161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L161)）。验证它落点：

   | 数据 | block_idx 范围 | `block_idx/4` 范围 | 对应 head 槽段 |
   |---|---|---|---|
   | Q | [0,128) | [0,32) | Q 头 0..31 |
   | K | [128,160) | [32,40) | K 头 32..39 |
   | V | [160,192) | [40,48) | V 头 40..47 |

   恰好填满 48 个槽，而且和 `num_attention_heads=32`、`num_kv_heads=8` 的边界完全吻合。

3. 再对照消费者 [attention_partial.cu:314-324](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L314-L324) 读的下标：K 用 `LLAMA_1B_NUM_ATTENTION_HEADS + kv_head_idx`（= 32+h），V 用 `... + LLAMA_1B_NUM_KV_HEADS + kv_head_idx`（= 40+h），Q 用 `q_head_start_idx + head_offset`（= h*4+0..3）。**与上表完全一致**——这就是两端能对上号的根本原因。

**需要观察的现象**：生产者写 `block_idx/4`、消费者读 `32+h`/`40+h`/`h*4+offset`，两套写法描述的是同一个 48 槽坐标系。

**预期结果**：你应当能徒手对任意 `kv_head_idx` 算出它读写哪几个槽（见 4.3.4 的图）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `Bar` 的第二维是 `opcode-1` 而不是 `opcode`？
**答**：opcode 从 1 开始（`OPCODE_RMS_QKV_MatVecRopeAppend = 1`），而数组下标从 0 开始。`opcode-1` 让第一个 op 占用下标 0，避免浪费一个槽，也让「槽号 = op 在流水线中的序号」。

**Q2**：head 维度为什么是 `num_attention_heads + 2*num_kv_heads = 48`？
**答**：因为 Q 头数 = `num_attention_heads = 32`（GQA 下每个 query 头独立计票），而 K、V 头数 = `num_kv_heads = 8`（GQA 共享），所以额外需要 `2*8 = 16` 个槽，合计 48。

---

### 4.2 生产者端：用 `atomicAdd` 写计数

#### 4.2.1 概念说明

生产者要把「我做完了一块」这个事实告诉所有潜在的消费者。难点在于：**同一时刻可能有大量线程/SM 在往同一个（或相邻）槽里投票**。比如 RMS_QKV 的 storer 由多个 warp 执行，每个 warp 处理不同的 16 元素块，它们都会往 `Bar` 里加 1。

如果用普通的 `Bar[i] = Bar[i] + 1`（读-改-写三步），两个 warp 同时执行就会丢更新（经典的 lost update）。GPU 提供的 `atomicAdd(addr, val)` 是一条**原子的**读-改-写指令，硬件保证不会丢票。这就是为什么这里**必须用 atomic**：它是无锁的，不需要任何临界区或锁，却能把来自任意 SM 的投票正确累加。

#### 4.2.2 核心流程：RMS_QKV storer 的投票

RMS_QKV 是 matvec pipeline 的 storer，每输出一个 16 元素块（`block_idx`）就走一次 `store()`：

1. 把这一块的 RoPE 结果写进全局内存（Q 进 `q_post_rope`，K/V 进 `k_cache`/`v_cache`），用 TMA 异步 store。
2. **`store_async_wait()`**：必须等 store 真正对全局内存可见，再投票——否则消费者可能看到计数已满、却读不到数据。
3. `atomicAdd(&g.Bar[{layer, opcode-1, block_idx/4}], 1)`：往对应 head 槽投一票。

#### 4.2.3 源码精读

RMS_QKV 的投票代码在 storer 的 `store()` 里：

```cpp
kittens::tma::store_async_wait(); // not just read wait! full wait! must
                                 // be visible in global!
// asm volatile("fence.acq_rel.gpu;\n"); // possible we need sc here but I don't think so.
atomicAdd(&g.Bar[{inst.layer_idx, opcode - 1, block_idx / 4}], 1);
```

[rms_matvec_rope_append.cu:156-162](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L156-L162)。注意三点：

- **先 `store_async_wait()` 再 `atomicAdd`**：注释明确强调「full wait, must be visible in global」。TMA store 是异步的，不等完就投票等于在数据还没落地时就喊「好了」。
- **head 下标 `block_idx/4`**：把 16 元素块号映射回头号（见 4.1.4）。
- **`opcode - 1`**：opcode = `OPCODE_RMS_QKV_MatVecRopeAppend = 1`，所以写入槽 0；这正是 PartialAttention 要读的槽。opcode 常量定义见 [llama.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L7)，opcode 字段绑定在 [rms_matvec_rope_append.cu:11-13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L11-L13)。

对照 Python 参考 VM（答案卷）：RMS_QKV 也是「每块加一」：

```python
barriers[block_idx // 4] += 1
```

[python_vm.py:248](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L248)（`barriers` 此处已取到 `[layer, opcode-1]` 切片）。两端逻辑一致，证明 CUDA 的 `atomicAdd` 就是对应 Python 里单线程的 `+= 1`。

**另一种写法：整段一起加。** DownProj 等基于 `MatVecAddOp` 的 op 不是逐块 `+1`，而是一条指令结束时一次 `+iters`：

```cpp
atomicAdd(&g.Bar[{inst.layer, opcode - 1, 0}], inst.iters);
```

[matvec_adds.cu:174](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L174)，其中 `iters = end_block_idx - start_block_idx`（[matvec_adds.cu:34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L34)）。这把「逐块投票」合并成「按指令粒度投票」，减少了 atomic 次数；阈值对应地变成「所有指令 iters 之和」（见 4.3 的 512）。PartialAttention 则又回到「逐头 +1」：[attention_partial.cu:664-666](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L664-L666) 给 `q_head_start_idx + laneid` 这几个 Q 头各加 1。

#### 4.2.4 代码实践：追踪一次投票的时序

**实践目标**：确认「数据可见 → 才投票」这一顺序。

**操作步骤**：

1. 读 [rms_matvec_rope_append.cu:154-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L154-L163)，把三件事按发生顺序列出来：TMA store → `store_async_wait()` → `atomicAdd`。
2. 假设把 `store_async_wait()` 这一行删掉（**仅作思考实验，不要真的改源码**），推理：消费者 [attention_partial.cu:398-404](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L398-L404) 看到 `Bar[...] >= 4` 时，`q_post_rope` 里的 Q 数据是否一定已经写好？

**需要观察的现象**：理解 atomic 投票只保证「计数被正确累加」，**不**保证「被计数的 store 已对其它 SM 可见」。可见性必须靠 `store_async_wait()`（及被注释掉的 `fence.acq_rel.gpu`）单独保证。

**预期结果**：删掉等待后，存在竞态——计数先于数据到达，PartialAttention 可能读到未初始化的 Q。这正是注释里「full wait! must be visible in global!」的用意。

> 说明：本实践为源码阅读型，不需要编译运行；若想真机验证，可在 storer 的 `store_async_wait()` 后、`atomicAdd` 前插入一个长 `__nanosleep` 制造时序窗口，观察是否出现错误结果（**待本地验证**）。

#### 4.2.5 小练习与答案

**Q1**：能不能用 `+1` 代替 `atomicAdd`？为什么？
**答**：不能（在多线程/多 SM 并发写同一槽时）。`+1` 是「读-改-写」三步，并发执行会丢更新；`atomicAdd` 硬件保证原子，是计数型屏障的正确选择。

**Q2**：RMS_QKV 投票时为什么要先 `store_async_wait()`？
**答**：TMA store 异步，计数若先于数据可见，消费者会读到尚未写好的数据。`store_async_wait()` 保证数据落地后再投票。

---

### 4.3 消费者端：`gmem_wait` + `__nanosleep` 自旋等待

#### 4.3.1 概念说明

消费者这一侧要做的事很简单：**反复读那个计数器，直到它达到期望值**。这种「忙等」叫**自旋（spin-wait）」。GPU 上没有阻塞式的「等通知」机制（不同 SM 之间），所以只能让线程在一个 `while` 循环里不停读全局内存。

但纯死循环会让 SM 满负荷空转、挤占带宽和调度。于是每轮循环插一个 `__nanosleep(N)`，让硬件把这次迭代「睡」\(N\) 纳秒，降低轮询频率、把资源让给同一 SM 上跑着的其它 warp（Megakernels 一个 SM 上有 controller/loader/consumer/storer 多个 warp，自旋的 warp 不能把它们饿死）。

读的时候还要把指针转成 `volatile int*`：

```cpp
while (*(volatile int *)&g.Bar[{...}] < EXPECTED) { __nanosleep(...); }
```

`volatile` 告诉编译器「不要把这次读优化掉、不要缓存到寄存器」，每次循环都必须真正去全局内存取最新值。没有 `volatile`，编译器可能把读提到循环外，循环就永远退不出了。

#### 4.3.2 核心流程与阈值公式

消费者等待的「期望值」是本讲的核心数字。它有一个统一的推导公式：

\[ T = \frac{\text{生产者写入的数据总量}}{\text{生产者每票对应的工作粒度}} \]

落到 Llama-1B（`head_dim=64`、`hidden_dim=2048`、`intermediate_dim=8192`、`matvec_block_size=16`，见 [llama.cuh:18-22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L18-L22)）：

- **Q/K/V 每个 head**：\(T_{\text{head}} = \text{head\_dim} / \text{matvec\_block\_size} = 64/16 = 4\)。因为一个头有 64 维，RMS_QKV 每 16 维投一票，所以每个头要投 4 次才算写完。
- **整条 hidden 向量**：\(T_{\text{hidden}} = 2048/16 = 128\)（O_Proj 等产出 hidden_states）。
- **intermediate 向量**：\(T_{\text{inter}} = 8192/16 = 512\)（upgate 产出 silu_out；DownProj 读取它）。

公式给出的 4、128、512 正是 Python 参考 VM 里 `assert` 锁定的值。

#### 4.3.3 源码精读

**案例 A：PartialAttention 等 Q/K/V（阈值 4）。**

PartialAttention 在两个地方自旋。launcher 的 `wait_for_kv` 等 K、V：

```cpp
while (*(volatile int *)&g.Bar[{inst.layer_idx, OPCODE_RMS_QKV_MatVecRopeAppend - 1,
       LLAMA_1B_NUM_ATTENTION_HEADS + inst.kv_head_idx}] < 4) {
    __nanosleep(config::GMEM_SPIN_LOOP_SLEEP_NANOS);
}
```

[attention_partial.cu:314-318](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L314-L318)（K 头），紧随其后 [attention_partial.cu:320-326](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L320-L326) 等 V 头（下标 `+ LLAMA_1B_NUM_KV_HEADS`）。consumer 里再用一个循环等本组 4 个 Q 头：

```cpp
for (int head_offset = 0; head_offset < GQA_RATIO; head_offset++) {
    while (*(volatile int *)&g.Bar[{inst.layer_idx, OPCODE_RMS_QKV_MatVecRopeAppend - 1,
           q_head_start_idx + head_offset}] < 4) {
        __nanosleep(config::GMEM_SPIN_LOOP_SLEEP_NANOS);
    }
}
```

[attention_partial.cu:396-405](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L396-L405)。这里的 `4` 就是 \(T_{\text{head}}\)，`q_head_start_idx = kv_head_idx * GQA_RATIO`（GQA_RATIO=4，[attention_partial.cu:9-10](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L9-L10)）。Python 参考 VM 对同一约束做 `assert == 4`：[python_vm.py:282-290](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L282-L290)。

**案例 B：RMS_QKV 等上一层 DownProj（阈值 512）。**

RMS_QKV 自己也是消费者——它要等上一层的 hidden_states 写完。它的 `gmem_wait` 读的是**上一层**的 DownProj 槽（`layer_idx - 1`）：

```cpp
if (inst.layer_idx > 0) {
    while (*(volatile int *)&g.Bar[{inst.layer_idx - 1,
           OPCODE_DownProjResidual - 1, 0}] < EXPECTED_ARRIVAL_COUNT) {
        __nanosleep(Config::GMEM_SPIN_LOOP_SLEEP_NANOS);
    }
}
```

[rms_matvec_rope_append.cu:52-59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L52-L59)，其中 `EXPECTED_ARRIVAL_COUNT = 512`（[rms_matvec_rope_append.cu:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L17)）。这 512 正是 \(T_{\text{inter}}\)——DownProj 对 intermediate 维度（8192）的归约被切成多段，每段每 16 元素投一票（`atomicAdd(..., inst.iters)`，[matvec_adds.cu:174](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L174)），全层累加得 8192/16 = 512。Python 参考 VM 注释直接写明 `# 8192 / 16`：[python_vm.py:110](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L110)（DownProj 读 upgate 槽），以及 RMS_QKV 读上一层时的 `assert op_barriers[0] == 512`：[python_vm.py:176](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L176)。最后的 RMS_LM_Head 同样 `assert == 512`：[python_vm.py:255](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L255)。

**自旋睡眠参数**：`config::GMEM_SPIN_LOOP_SLEEP_NANOS`，定义在 [config.cuh:48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L48)。需要留意一个已知小瑕疵：它被声明成 `bool` 却赋值 `20`，会被规约成 `true(1)`，所以实际睡眠大约是 1 纳秒（详见 u5-l1 的说明）。这不影响屏障正确性，只影响轮询节奏。

#### 4.3.4 代码实践：画出 QKV → PartialAttention 的计数流转图

**实践目标**：把「生产者投票 → 消费者放行」在一张图上画出来，并用具体数字填进去。

**操作步骤**：取 `kv_head_idx = 2`（则 `q_head_start_idx = 2*4 = 8`）。

1. 生产者 RMS_QKV 逐块投票，写出每个 head 槽最终累积到的值（都应是 4）：

   ```
   Q 头 8,9,10,11  → Bar[L, 0, 8/9/10/11]   各 +=1 ×4  → 4
   K 头 34         → Bar[L, 0, 34]          +=1 ×4      → 4
   V 头 42         → Bar[L, 0, 42]          +=1 ×4      → 4
   ```

2. 消费者 PartialAttention 在 `wait_for_kv` + consumer 循环里分别自旋读这些槽，直到都 `>= 4`。

3. 画成流转图（`slot = opcode-1 = 0`）：

   ```
   RMS_QKV (opcode 1)                       PartialAttention (opcode 2)
   ============ producer ============       ============ consumer ============
                                           
   Q块0..3   atomicAdd ─┐                   
   ...                 ├──► Bar[L,0, 8..11] ──► while(<4) spin  ──► load Q
   Q块28..31 atomicAdd ─┘   (4 票/头)        
                                           
   K块0..3   atomicAdd ──► Bar[L,0, 34]     ──► while(<4) spin  ──► load K (TMA)
   V块0..3   atomicAdd ──► Bar[L,0, 42]     ──► while(<4) spin  ──► load V (TMA)
   ```

4. 对照源码核对每个箭头：生产端 [rms_matvec_rope_append.cu:161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L161)；消费端 K/V [attention_partial.cu:314-326](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L314-L326)、Q [attention_partial.cu:396-405](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L396-L405)。

**需要观察的现象**：消费者必须 6 个槽（4 个 Q + 1 个 K + 1 个 V）全部到 4 才放行；任何一个没到都会一直自旋。

**预期结果**：每个 head 槽的「4」来自 `head_dim/matvec_block_size`；6 个槽对应一个 KV 头组（GQA_RATIO=4 个 Q 头 + 1 K + 1 V）。这就解释了为什么阈值是 4、为什么索引用 `32+h`/`40+h`。

#### 4.3.5 小练习与答案

**Q1**：为什么读 `Bar` 时要写成 `*(volatile int *)&`？
**答**：`volatile` 阻止编译器把读取优化/缓存到寄存器，保证每轮循环都真正从全局内存取最新计数；否则编译器可能把读提到循环外，造成死循环。

**Q2**：PartialAttention 等的阈值为什么是 4 而不是 1？
**答**：一个 Q/K/V 头有 64 维，RMS_QKV 以 16 维为一块、每块投一票，所以一个头要投 \(64/16 = 4\) 次才算写完。阈值 4 = `head_dim / matvec_block_size`。

**Q3**：RMS_QKV 的 `gmem_wait` 为什么读 `layer_idx - 1` 且阈值是 512？
**答**：RMS_QKV 在第 \(L\) 层开头要读上一层的输出 hidden_states，它由第 \(L-1\) 层的 DownProj 写入，所以读 `layer_idx - 1` 的 DownProj 槽（`OPCODE_DownProjResidual-1`）。DownProj 对 intermediate(8192) 维归约、每 16 维投一票，全层共 \(8192/16 = 512\) 票，故阈值 512。

---

## 5. 综合实践

把三个最小模块串起来：**为一个 KV 头组写一份「屏障契约」**。

**任务**：选定 `kv_head_idx = 5`，完成下表，并回答两个问题。

| 角色 | op / opcode | 读写槽 `[layer, ?, head]` | 写入/等待的值 | 关键源码 |
|---|---|---|---|---|
| 生产者 RMS_QKV | opcode 1 | `[L, 0, ?]` | 每个 head 槽累加到 ? | rms_matvec_rope_append.cu:161 |
| 消费者 PartialAttention（Q） | opcode 2 | `[L, 0, ?]` | 等到 ? | attention_partial.cu:396-405 |
| 消费者 PartialAttention（K） | opcode 2 | `[L, 0, ?]` | 等到 ? | attention_partial.cu:314-318 |
| 消费者 PartialAttention（V） | opcode 2 | `[L, 0, ?]` | 等到 ? | attention_partial.cu:320-326 |

需要填的 `?`：对 `kv_head_idx=5`，`q_head_start_idx = 20`，所以 Q 头是 20/21/22/23，K 头是 37，V 头是 45；所有累加/等待值都是 4。

**附加问题**：

1. 如果把 `block_idx/4` 改成 `block_idx/8`（假设），屏障会出什么问题？（提示：K/V 头会与 Q 头槽位重叠，且阈值 4 不再匹配，计数语义被破坏。）
2. 为什么这里用 `atomicAdd` 而不是框架内部的 `kittens::semaphore`？（提示：semaphore 是 block 内 / 共享内存的硬件 mbarrier，无法跨 SM、跨 op；`Bar` 在全局内存，任意 SM 可见，适合 op 间同步。）

**进阶（可选运行）**：Megakernels 提供了一个 Python 参考 VM（[python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py)），它用 `assert` 把每个屏障的期望计数值钉死。差分测试脚本 [diff_test.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py) 会让 CUDA megakernel 与该参考 VM 跑同一批指令并比对。运行它（一个基于 `pydra` 的脚本，默认 `layer_limit=1`、`setting="latency"`）若全绿，说明 CUDA 端的 atomic 投票与自旋等待产出的计数值与 `assert == 4` / `assert == 512` 一致。具体调用方式与依赖请参考 u1-l3 的构建运行说明；本机是否可跑需**待本地验证**。

## 6. 本讲小结

- `globals.Bar` 是全局内存里的三维计数器 `[layer, opcode-1, head]`：`layer` 隔离层、`opcode-1` 是生产者自己的槽（因为 opcode 从 1 起）、`head` 把 Q(32)+K(8)+V(8)=48 个头分开计票。
- 生产者用 `atomicAdd` 投票：RMS_QKV 逐块 `atomicAdd(...,1)`（head 下标 = `block_idx/4`），DownProj 等则按指令粒度 `atomicAdd(..., inst.iters)`；**必须先 `store_async_wait()` 保证数据可见再投票**。
- 消费者用 `gmem_wait` 自旋：`while(*(volatile int*)&Bar[...] < T) __nanosleep(...)`；`volatile` 防止读被优化掉，`__nanosleep` 给同 SM 其它 warp 让路。
- 阈值有统一公式 \(T = \text{数据量}/\text{投票粒度}\)：每个 head = `64/16 = 4`；hidden = `2048/16 = 128`；intermediate = `8192/16 = 512`。
- `Bar` 是「累积式」计数器，每个 token 开始前由宿主 `fill_(0)` 清零，否则旧计数会让消费者误判放行。
- 这套机制与框架内部 `kittens::semaphore` 互补：semaphore 管 block 内同步（共享内存硬件 mbarrier），`Bar` 管跨 SM、跨 op 的全局同步。

## 7. 下一步学习建议

- **接着看后续的屏障边**：阅读 `attention_reduction.cu`（opcode 3）如何自旋等待 PartialAttention 的逐头计数（阈值 = `num_partials`），以及它如何作为 O_Proj 的生产者；把本讲的流转图扩成一条贯穿整层的依赖链。
- **对比 `MatVecAddOp` 的「批量投票」**：精读 [matvec_adds.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu)，体会逐块 `+1` 与按指令 `+iters` 两种粒度的取舍。
- **跑差分测试验证阈值**：用 [diff_test.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py) 对照 Python 参考 VM，观察 `assert == 4` / `== 512` 是否真的成立。
- **回顾屏障的生命周期**：回到 [generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py) 的 `fill()`，理解为什么「每个 token 清零」是正确性前提，并思考是否有「可回收」的更高效屏障设计。
