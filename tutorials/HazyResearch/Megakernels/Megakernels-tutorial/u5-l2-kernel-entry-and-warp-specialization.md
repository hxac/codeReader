# 内核入口与 warp 角色特化

> 本讲对应手册单元 **U5·L2**，承接 [U5·L1]（`u5-l1-*.md`，VM 内核总览与同步原语）。建议你已建立「`mk` 是一个常驻 SM 的持久化内核、内部把不同 warp 派给不同功能单元」的直觉（可先读 [U1·L1] 的 4.1.2、[U1·L2] 的 VM 结构图）。本讲从「内核刚被启动的那一瞬间」切入，逐行拆解 `mk` 如何初始化共享内存与信号量，再把 20 个 warp 派成 **16 个 consumer + 4 个职能 warp（loader / storer / launcher / controller）**。

---

## 1. 本讲目标

学完本讲后，你应当能够：

1. 读懂 `mk` 内核的三个启动注解：`__launch_bounds__(NUM_THREADS, 1)`、`__cluster_dims__(CLUSTER_BLOCKS)`、`__grid_constant__ globals g`，并说出它们各自约束的是什么。
2. 画出 `mk_internal` 里「声明共享数组 → 构造 `state` → 分布式初始化信号量 → fence + 同步」这条初始化主线，说清每个信号量数组的初始阈值与谁负责 `init` / `arrive`。
3. 解释 `if (kittens::warpid() < NUM_CONSUMER_WARPS)` 与 `switch (kittens::warpgroup::warpid())` 的区别——尤其是为什么 4 个职能 warp 用的是**warpgroup 内编号（0–3）**而不是绝对 warp 号（16–19）。
4. 说清 `consumer` 为何要 `increase_registers`、4 个职能 warp 为何要 `decrease_registers`，以及这与 Hopper/Blackwell 的 `setmaxnreg` 寄存器再分配的关系。
5. 解释 `megakernel_wrapper` 为什么要在算子列表最前面**插一个 `NoOp`**，以及它如何保证「VM 能正确处理 0」。

---

## 2. 前置知识

### 2.1 warp、warpgroup 与线程编号

- **warp（线程束）**：GPU 上 32 个线程组成的基本执行单元。本项目里 `::kittens::WARP_THREADS = 32`。
- **warpgroup（线程束组）**：Hopper 引入的概念，**4 个连续 warp = 1 个 warpgroup = 128 个线程**。张量核（MMA/TC）指令在 Hopper/Blackwell 上通常以**整个 warpgroup** 为发射单位，所以本项目里「能做矩阵乘的 consumer warp」总是 4 的倍数。
- 两种「warp 号」要严格区分（这是本讲最容易踩的坑）：
  - `kittens::warpid()`：**整个 block 内的绝对 warp 号**（0, 1, 2, …）。
  - `kittens::warpgroup::warpid()`：**所在 warpgroup 内的相对编号**，取值恒为 0–3。
  - 二者关系：绝对 warp 号 = `warpgroup_id × 4 + warpgroup::warpid()`。例如绝对 17 号 warp 属于第 4 个 warpgroup（16–19），其组内编号是 1。

### 2.2 信号量（semaphore）与「相位（phase）」

Megakernels 的 warp 间同步主要靠**计数信号量**（`kittens::semaphore`），而不是互斥锁。它的核心操作：

- `init_semaphore(s, count)`：把信号量 `s` 的**到达阈值**设为 `count`（即需要累计 `count` 次到达才会「翻转/放行」）。
- `arrive(s, n)`：让本 warp 给 `s` 贡献 `n` 次到达。
- `wait(s, phase_bit)`：阻塞直到 `s` 达到当前相位阈值后放行。

很多信号量在初始化时会被**预先 `arrive` 一次**，从而一开始就处于「已满足」状态（代码里 `tensor_finished` 的注释明确写了 "Flip to state 0, to mark that it starts as available"）。更细的相位算术属于 [U5·L1] 的范畴，本讲只关心「谁、在哪里、用什么阈值初始化了哪些信号量」。

### 2.3 寄存器是 SM 上的「总量受限资源」

GPU 每个以太 SM 的**寄存器文件（register file）总量是固定的**（Hopper 每 SM 256KB / 65536 个 32-bit 寄存器）。一个 block 里所有 warp 共享这份预算。如果你给某些 warp 分配更多寄存器，留给其它 warp 的就变少。

Hopper/Blackwell 提供 `setmaxnreg` 这条 PTX 指令，允许**按 warpgroup 设置不同的「最大寄存器数」**。Megakernels 正是利用它，给「干重活的 consumer」更多寄存器、给「干轻活的职能 warp」更少寄存器。`kittens::warpgroup::increase_registers<N>()` / `decrease_registers<N>()` 就是对这条指令的封装。

### 2.4 流水线级（pipeline stage）与指令队列

回顾 [U4·L1]：每条 SM 队列被 `NoOp` 填充到**等长**后，张量化成 `[num_sms, queue_len, 32]`。本讲的 `mk` 就是「某一个 SM 上跑完自己那条 `queue_len` 长指令队列」的内核。为了隐藏延迟，它把指令取指做成 **2 级流水线**（`INSTRUCTION_PIPELINE_STAGES = 2`）。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | **本讲主角**。`mk` 内核入口、`mk_internal` 初始化与角色分派、`megakernel_wrapper` | 全文（173 行，本讲几乎逐行覆盖） |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | `default_config`：warp 数、寄存器数、流水线级数、页数等常量 | `NUM_CONSUMER_WARPS` / `NUM_WARPS` / `CONSUMER_REGISTERS` / `NON_CONSUMER_REGISTERS` / `CLUSTER_BLOCKS` |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `state<config>`（VM 状态对象）、`instruction_state_t`、`MAKE_WORKER` 宏 | 构造 `state` 所需的字段、`MAKE_WORKER` 生成的 `main_loop` 骨架 |
| [include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) | `NoOp<config>` 算子（opcode 0） | 被 `megakernel_wrapper` 前置插入 |
| [include/loader.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/loader.cuh) 等 | loader / storer / launcher / consumer 的 `main_loop`（经 `MAKE_WORKER` 生成） | 角色分派后的落点 |

> ⚠️ Megakernels 依赖的 ThunderKittens（`kittens.cuh`）在当前仓库是**未检出的 git 子模块**（`ThunderKittens/` 为空目录）。因此本讲对 `kittens::warpid()`、`warpgroup::warpid()`、`increase_registers` 等只描述其语义（结合标准 CUDA/PTX 行为与本项目用法），不给 TK 子模块内的伪造永久链接；它们在本项目的**调用点**都标注了真实链接。

---

## 4. 核心概念与源码讲解

### 4.1 内核入口 `mk` 与启动注解

#### 4.1.1 概念说明

`mk` 是整个项目唯一的 `__global__` 内核（[U1·L2] 已指出「无论哪层、哪个算子，最终都落到这一个内核」）。它本身几乎不做计算，只做两件事：把 Python 传来的 `globals` 接住，再委托给 `megakernel_wrapper::run`。

真正给编译器和驱动下「约束」的是它身上的三个注解：

| 注解 | 值（默认配置） | 约束对象 |
| --- | --- | --- |
| `__launch_bounds__(NUM_THREADS, 1)` | `(640, 1)` | 编译器的**寄存器/资源预算** |
| `__cluster_dims__(CLUSTER_BLOCKS)` | `(1)` | 驱动的**线程块簇（cluster）规模** |
| `__grid_constant__ globals g` | — | `globals` 参数的**存放方式** |

其中 `NUM_THREADS = NUM_WARPS(20) × WARP_THREADS(32) = 640`。

#### 4.1.2 核心流程

```
mk(g)                          # __global__，每个 SM 网格启动若干 block
 ├─ __launch_bounds__ 告诉编译器：本 block 最多 640 线程、每 SM 至少 1 个 block
 ├─ __cluster_dims__  告诉驱动：每簇 1 个 block（默认无多块簇）
 ├─ g 经 __grid_constant__ 进入只读常量缓存
 └─ megakernel_wrapper::run(g)  # 转发到 mk_internal（见 4.4）
```

#### 4.1.3 源码精读

[megakernel.cuh:166-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L166-L171) —— 这就是 `mk` 内核本体，三个注解一字排开：

```cpp
template <typename config, typename globals, typename... ops>
__launch_bounds__(config::NUM_THREADS, 1)
    __cluster_dims__(config::CLUSTER_BLOCKS) __global__
    void mk(const __grid_constant__ globals g) {
    megakernel_wrapper<config, globals, ops...>::run(g);
}
```

逐项解读：

- **`__launch_bounds__(640, 1)`**：第一参数 = 每 block 最大线程数 = 640（正好是 20 warp × 32）；第二参数 = `minBlocksPerMultiprocessor = 1`，提示编译器「至少要让 1 个这样的 block 能驻留一个 SM」。这个提示直接影响编译器为该内核分配的**每线程最大寄存器数**——它和 4.3 节的 `setmaxnreg` 是控制寄存器预算的两道闸。
- **`__cluster_dims__(1)`**：定义线程块簇为 1×1×1（即单个 block 自成一簇）。注意代码里所有「跨簇同步」都包了 `if (config::CLUSTER_BLOCKS == 1) … else …`（见 [megakernel.cuh:107-111](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L107-L111)），默认配置走单块分支，簇同步实际不跨 block。
- **`__grid_constant__ globals g`**：`g`（即 Python 侧的 globs）按值传入，但被放进**只读常量内存**，所有线程通过常量缓存读它，开销极低。这正是「整层只传一份 globs、所有 warp 共享」能成立的前提。

这三个常量都来自 [config.cuh:25-29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L25-L29)：

```cpp
static constexpr int NUM_CONSUMER_WARPS = 16;
static constexpr int NUM_WARPS = 4 + NUM_CONSUMER_WARPS;     // = 20
static constexpr int NUM_THREADS = NUM_WARPS * ::kittens::WARP_THREADS;  // = 640
static constexpr int NUM_BLOCKS = 1;
static constexpr int CLUSTER_BLOCKS = 1;
```

#### 4.1.4 代码实践

1. **目标**：验证「20 warp / 640 线程」是配置算出来的，不是硬编码。
2. **步骤**：打开 [config.cuh:25-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L25-L27)，手算 `NUM_WARPS` 与 `NUM_THREADS`：`4 + 16 = 20`，`20 × 32 = 640`。
3. **观察**：注意 `NUM_WARPS = 4 + NUM_CONSUMER_WARPS` 这个写法——它直接编码了「4 个职能 warp + N 个 consumer」的结构。
4. **预期结果**：你能说清 `__launch_bounds__` 的两个参数分别取自 `NUM_THREADS=640` 和字面量 `1`；如果把 `NUM_CONSUMER_WARPS` 改大，`NUM_THREADS` 会同步增大。
5. **待本地验证**：若你改了 `NUM_CONSUMER_WARPS` 重新编译，内核是否会因寄存器/共享内存超限而启动失败——需在带 GPU 的环境确认。

#### 4.1.5 小练习与答案

**练习 1**：`__launch_bounds__(640, 1)` 的第二个参数 `1` 是什么含义？为什么这个内核把它设成 `1`？

**答案**：它是 `minBlocksPerMultiprocessor`，告诉编译器「每个 SM 上至少要能驻留 1 个这样的 block」。这个内核每个 block 就占满 640 线程 + 大量共享内存 + 高寄存器，一个 SM 通常只能放下少量（甚至 1 个）block；设成 1 是诚实地反映这一点，让编译器据此放宽每线程寄存器上限（配合 `setmaxnreg` 做不对称分配）。

**练习 2**：`__grid_constant__ globals g` 与「直接按值传 `globals g`」有什么区别？

**答案**：`__grid_constant__` 把参数放进常量内存并通过常量缓存广播给所有线程，只读且访问极快；普通按值传递则会复制到每个线程的参数区，开销更大且不能享受常量缓存。本内核所有 warp 都要反复读 `g`（取指令张量、权重指针等），用 `__grid_constant__` 是必要的优化。

---

### 4.2 共享内存与信号量初始化

#### 4.2.1 概念说明

`mk_internal` 是真正干活的函数。它一进来要先**在共享内存里搭好 VM 的全部「状态容器」**，并把它们的同步信号量初始化到正确的初始相位，否则后续 5 类 warp 一开跑就会 `wait` 在错误的状态上死锁。这部分代码集中在 [megakernel.cuh:16-111](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L16-L111)。

要初始化的容器分两大块：

1. **静态共享数组**（编译期已知大小，`__shared__` 直接声明）：
   - `instruction_state[STAGES]`：指令流水线的 2 个槽位，每个槽含「指令本体 + timings + pid_order + 动态信号量 + scratch」（结构定义见 [util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19)）。
   - 一堆 `kittens::semaphore` 数组：`page_finished`、`instruction_arrived`、`instruction_finished`、（Blackwell 的）`tensor_finished`、`semaphores_ready`。
2. **动态共享内存**（`extern __shared__`，运行时按字节切分）：放 `pages`（数据页）。

#### 4.2.2 核心流程

```
mk_internal(g)
 ├─ start_time = clock64()                              # 计时起点
 ├─ 声明静态 __shared__ 数组（instruction_state + 各信号量）
 ├─ extern __shared__ __shm[] → 向上对齐到 1024B → pages   # 动态共享内存安家
 ├─ 用上述数组构造 state<config> mks{...}                # VM 状态对象
 ├─ 分布式初始化（按 threadIdx.x 切分给不同线程）：
 │     · t<128           : 清零 timings
 │     · t<STAGES(=2)    : init instruction_arrived(阈值1) / instruction_finished(阈值19)
 │     · t<NUM_PAGES(=13): init + 预 arrive page_finished
 │     · t==0            : init(+预arrive) tensor_finished；init semaphores_ready
 ├─ fence.proxy.async.shared::cta  +  __syncthreads()    # 让初始化对本 block 可见
 └─ everyone::sync() / cluster::sync()                   # 跨 warp/簇确认可见
```

#### 4.2.3 源码精读

**(a) 静态数组声明** —— [megakernel.cuh:23-33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L23-L33)：

```cpp
__shared__ alignas(128) instruction_state_t<config>
    instruction_state[config::INSTRUCTION_PIPELINE_STAGES];
__shared__ kittens::semaphore
    page_finished[config::NUM_PAGES][config::INSTRUCTION_PIPELINE_STAGES_BITS],
    instruction_arrived[config::INSTRUCTION_PIPELINE_STAGES],
    instruction_finished[config::INSTRUCTION_PIPELINE_STAGES],
#ifdef KITTENS_BLACKWELL
    tensor_finished,
#endif
    semaphores_ready;
```

- `instruction_state` 用 `alignas(128)` 强制 128 字节对齐——避免跨 cache line 访问带来的性能惩罚。
- `page_finished` 是二维数组 `[NUM_PAGES=13][BITS=1]`，给「每页 × 每相位」配一个信号量。
- `instruction_arrived` / `instruction_finished` 按**流水线级数（2）**索引。

**(b) 动态共享内存安家** —— [megakernel.cuh:34-39](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L34-L39)：

```cpp
extern __shared__ int __shm[];
void *aligned_shm_addr =
    (void *)((1023 + (uint64_t)&__shm[0]) & ~(uint64_t)1023);  // 向上对齐到 1024B
typename state<config>::page_array_t &pages =
    *reinterpret_cast<typename state<config>::page_array_t *>(aligned_shm_addr);
```

`extern __shared__` 拿到的是启动时分配的动态共享内存起点；`(x + 1023) & ~1023` 是经典的「向上取整到 1024 字节」对齐技巧（TMA 等异步拷贝要求严格对齐）；对齐后的指针被重解释为 `page_array_t`（即 `page<config>[NUM_PAGES]`，13 个 16KB 数据页）。

**(c) 构造 `state` 对象** —— [megakernel.cuh:49-66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L49-L66)：把上面所有共享数组的**引用**连同初始的 `instruction_index=0`、`instruction_ring=0`、`start_time` 等塞进 `state<config> mks`。之后所有 warp 都通过 `mks` 访问这些共享状态（`state` 的字段见 [util.cuh:73-212](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L73-L212)）。

**(d) 分布式初始化信号量** —— 关键在于**用 `threadIdx.x` 把初始化工作切分给不同线程并行做**，而不是单线程串行：

[megakernel.cuh:75-93](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L75-L93) —— 清零 timings + 初始化指令级与页级信号量：

```cpp
if (threadIdx.x < config::TIMING_WIDTH) {                 // 128 个线程各清一列 timings
    for (int i = 0; i < config::INSTRUCTION_PIPELINE_STAGES; i++)
        instruction_state[i].timings[threadIdx.x] = 0;
}
if (threadIdx.x < config::INSTRUCTION_PIPELINE_STAGES) {  // 2 个线程各 init 一个流水级
    init_semaphore(instruction_arrived[threadIdx.x], 1);  // 阈值 1（controller arrive 1 次即放行）
    init_semaphore(instruction_finished[threadIdx.x],
                   config::NUM_WARPS - 1);                // 阈值 19（除 controller 外全部就绪）
}
if (threadIdx.x < config::NUM_PAGES) {                    // 13 个线程各负责一页
    for (int i = 0; i < config::INSTRUCTION_PIPELINE_STAGES_BITS; i++) {
        auto count = config::NUM_CONSUMER_WARPS * (1 << i);  // = 16
        init_semaphore(page_finished[threadIdx.x][i], count);
        arrive(page_finished[threadIdx.x][i], count);     // 预 arrive，初始即「已满足」
    }
}
```

[megakernel.cuh:94-102](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L94-L102) —— 仅由 0 号线程初始化全局级信号量：

```cpp
if (threadIdx.x == 0) {
#ifdef KITTENS_BLACKWELL
    init_semaphore(tensor_finished, config::NUM_CONSUMER_WARPS);  // 阈值 16
    arrive(tensor_finished, config::NUM_CONSUMER_WARPS);          // 翻转到「初始可用」
#endif
    init_semaphore(semaphores_ready, 1);                          // 阈值 1，不预 arrive
}
```

把上面的阈值与是否「预 arrive」整理成表（**本讲的信号量速查表**）：

| 信号量 | 形状 | 初始阈值 | 是否预 arrive | 初始状态 | 谁后来 arrive 它 |
| --- | --- | --- | --- | --- | --- |
| `instruction_arrived` | `[2]` | `1` | 否 | 未满足 | controller（每条指令 arrive 1） |
| `instruction_finished` | `[2]` | `NUM_WARPS-1 = 19` | 否 | 未满足 | 19 个非 controller warp 各 arrive 1 |
| `page_finished` | `[13][1]` | `NUM_CONSUMER_WARPS = 16` | **是**（arrive 16） | 已满足 | loader/consumer 完成页时 arrive |
| `tensor_finished`（Blackwell） | 标量 | `16` | **是**（arrive 16） | 已满足 | launcher（注释明示「starts as available」） |
| `semaphores_ready` | 标量 | `1` | 否 | 未满足 | controller 构造完动态信号量后 arrive |

注意一个关键设计差异：**`page_finished` / `tensor_finished` 被预 arrive**（一开始就放行，因为第一条指令时这些资源天然可用），而 **`instruction_arrived` / `instruction_finished` / `semaphores_ready` 不预 arrive**（它们必须等 controller 真正干完活才能放行）。

**(e) 让初始化对所有 warp 可见** —— [megakernel.cuh:104-111](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L104-L111)：

```cpp
asm volatile("fence.proxy.async.shared::cta;\n" ::: "memory");
__syncthreads();
if (config::CLUSTER_BLOCKS == 1)
    kittens::everyone::sync(15);   // 所有 warp 到达，确认信号量初始化对全部线程可见
else
    kittens::everyone::tma::cluster::sync();
```

- `fence.proxy.async.shared::cta` 排序异步共享内存代理的写操作（防止信号量初值被乱序的 async 拷贝覆盖）；
- `__syncthreads()` 是 block 级屏障；
- 最后的 `everyone::sync` 确保跨 warp 看到一致初值——**这步不可省**，否则某个 warp 可能在信号量还没 `init` 完时就去 `wait`，读到垃圾值而死锁。

#### 4.2.4 代码实践

1. **目标**：理解「初始化被 `threadIdx.x` 切片并行」这一设计。
2. **步骤**：对照 [megakernel.cuh:75-102](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L75-L102)，列出「每个 `if` 分支由哪段 `threadIdx.x` 区间的线程执行、各自 init 了什么」。
3. **观察**：你会发现 0–1 号线程 init 指令级信号量、0–12 号线程 init 页级信号量、0 号线程还额外 init 了 `tensor_finished` 和 `semaphores_ready`——存在重叠（同一线程可能同时进入多个 `if`）。这正是「按 id 切片」而非「互斥分工」的特点。
4. **预期结果**：你能画出一张「`threadIdx.x` 区间 → 负责初始化的信号量」的覆盖图，并解释为什么 `page_finished` 要预 arrive 而 `instruction_arrived` 不要。
5. **待本地验证**：若在 `MK_DEBUG` 下编译运行（见 [megakernel.cuh:113-116](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L113-L116) 的 `mks.print()`），能否在控制台看到初始化后的 `instruction_index=0, instruction_ring=0`——需带 GPU 环境确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `instruction_finished` 的阈值是 `NUM_WARPS - 1 = 19` 而不是 `NUM_WARPS = 20`？

**答案**：因为 controller warp 自己不参与「完成本条指令」的计数——它负责的是「产生」下一条指令（取指、分页、构造信号量）。`instruction_finished` 是给 controller 用来判断「上一个流水级槽位里的指令是否已被其余所有 warp 用完、可以安全复用」的，所以只统计除 controller 外的 19 个 warp。controller 在 [controller.cuh:40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L40) 的 `wait(instruction_finished[…])` 正是在等这个。

**练习 2**：`page_finished` 的阈值为什么是 `NUM_CONSUMER_WARPS * (1 << i)`，在默认配置（`BITS=1`）下等于 16？

**答案**：`page_finished` 追踪「某页是否已被所有 consumer 消费完」。`(1 << i)` 是相位展开项：默认 `INSTRUCTION_PIPELINE_STAGES_BITS = 1`，只有 `i=0`，所以阈值 = `16 × 1 = 16`，即「16 个 consumer 都到达」才算该页本轮用完。多 bit 时阈值翻倍，对应更深的流水线相位（相位细节见 [U5·L1]）。

---

### 4.3 warp 角色分派 switch 与寄存器再分配

#### 4.3.1 概念说明

初始化完成后，`mk_internal` 进入本讲最核心的一步：**按 warp 号把 20 个 warp 派给 5 类角色**。这是「warp 角色特化（warp specialization）」——同一份内核代码，不同 warp 进入不同的 `main_loop`，从外面看就像一台机器里既有 CPU（controller）又有执行单元（consumer/launcher）和访存单元（loader/storer）。

派分有两个层次，必须区分清楚：

1. **第一层（`if`）**：`if (kittens::warpid() < NUM_CONSUMER_WARPS)` —— 用**绝对 warp 号**判断。绝对 0–15 号 → consumer。
2. **第二层（`switch`）**：`else` 分支里的 `switch (kittens::warpgroup::warpid())` —— 用**warpgroup 内编号**判断。这 4 个 warp 是绝对 16–19 号，恰好构成**第 4 个 warpgroup（16–19）**，其组内编号是 0/1/2/3，正好匹配 `case 0..3`。

> **关键洞察**：`switch` 不能用 `kittens::warpid()`！因为剩下这 4 个 warp 的绝对号是 16–19，而 `case` 标签是 0–3。只有用 `warpgroup::warpid()`（组内编号 0–3）才能对上。这就是「16 个 consumer 占满前 4 个 warpgroup、4 个职能 warp 恰好组成第 5 个 warpgroup」这一布局的直接结果。

派分的同时，还对两类 warp 做了**寄存器再分配**：consumer 调高寄存器上限、职能 warp 调低。

#### 4.3.2 核心流程

```
                         ┌─ warpid 0–15 (WG0–WG3, 组内0–3)
                         │     increase_registers<104>
if (warpid() < 16) ──────┤     consumer::main_loop(g, mks)        # 16 个算力主力
                         │
                         └─ 否则 warpid 16–19 (WG4)
                               decrease_registers<64>
                               switch (warpgroup::warpid()):       # 组内编号
                                 case 0 (warpid 16): loader::main_loop      # 搬数据进显存→共享
                                 case 1 (warpid 17): storer::main_loop      # 把结果搬回显存
                                 case 2 (warpid 18): launcher::main_loop    # 派发 MMA 给 consumer
                                 case 3 (warpid 19): controller::main_loop  # 取指/分页/造信号量
                                 default:           trap            # 不应到达
```

#### 4.3.3 源码精读

[megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140) —— 这是整个 VM 的「角色分派台」：

```cpp
if (kittens::warpid() < config::NUM_CONSUMER_WARPS) {
    kittens::warpgroup::increase_registers<config::CONSUMER_REGISTERS>();   // 104
    ::megakernel::consumer::main_loop<config, globals, ops...>(g, mks);
} else {
    kittens::warpgroup::decrease_registers<config::NON_CONSUMER_REGISTERS>(); // 64
    switch (kittens::warpgroup::warpid()) {
    case 0: ::megakernel::loader::main_loop<config, globals, ops...>(g, mks);     break;
    case 1: ::megakernel::storer::main_loop<config, globals, ops...>(g, mks);     break;
    case 2: ::megakernel::launcher::main_loop<config, globals, ops...>(g, mks);   break;
    case 3: ::megakernel::controller::main_loop<config, globals, ops...>(g, mks); break;
    default: asm volatile("trap;");
    }
}
```

**两个寄存器常量**来自 [config.cuh:50-51](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L50-L51)：

```cpp
static constexpr int CONSUMER_REGISTERS = 104;      // consumer 上限调高
static constexpr int NON_CONSUMER_REGISTERS = 64;   // 职能 warp 上限调低
```

**为什么 consumer 要「增加」、职能 warp 要「减少」？**

- **consumer（增到 104）**：它们跑 MMA/张量核算子，需要同时持有大量活跃数据——累加器、K/V tile、中间激活等。寄存器越多，越能把这些值留在寄存器里**避免溢出（register spill）到共享/全局内存**，从而压住延迟。104 是在不让整个 block 的寄存器总量超限的前提下，能给 consumer 的较高上限。
- **职能 warp（降到 64）**：loader/storer 做的是数据搬运、launcher 做派发、controller 做控制流与信号量记账——它们的「活跃状态」很瘦，64 个寄存器绑绑有余。**主动调低它们的上限，等于把寄存器预算让给 consumer**。

这背后是 Hopper/Blackwell 的 `setmaxnreg` PTX 指令：它允许**按 warpgroup 设置不同的最大寄存器数**。`increase_registers<N>` / `decrease_registers<N>` 正是对它的封装。注意它们都是 **warpgroup 粒度**（名字里带 `warpgroup::`）——这再次印证了「consumer 是 4 的倍数（4 个 warpgroup）、职能 warp 恰好 1 个 warpgroup」的布局不是巧合，而是被硬件寄存器分配粒度强制的。

**`main_loop` 从哪来？** loader / storer / launcher / consumer 四个角色的 `main_loop` 都不是手写的，而是由 [util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304) 的 `MAKE_WORKER` 宏展开生成（例如 [loader.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/loader.cuh#L7) 就一行 `MAKE_WORKER(loader, TEVENT_LOADER_START, false)`）。生成的骨架是「按指令队列循环：`await_instruction` → 按 opcode 分派到对应 op 的 `<角色>::run` → `next_instruction`」。controller 因为逻辑复杂（取指+分页+造信号量），有手写的 [controller.cuh:14-165](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L14-L165)，不走 `MAKE_WORKER`。

#### 4.3.4 代码实践（本讲主实践：画 20-warp 角色分配表）

1. **目标**：把全部 20 个 warp 的角色、warpgroup 归属、组内编号、`main_loop` 落点、寄存器上限画成一张表，并据此解释寄存器增减。
2. **操作步骤**：
   - 读 [megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140) 的分派代码，以及 [config.cuh:25-27, 50-51](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L25-L27)。
   - 用 `warpgroup_id = warpid / 4`、`组内编号 = warpid % 4` 把每个 warp 定位。
3. **需观察/产出的角色分配表**：

| 绝对 `warpid` | warpgroup | `warpgroup::warpid()` | 角色 | `main_loop` 落点 | 寄存器上限 |
| --- | --- | --- | --- | --- | --- |
| 0 | WG0 | 0 | consumer | `consumer::main_loop` | 104（increase） |
| 1 | WG0 | 1 | consumer | 同上 | 104 |
| 2 | WG0 | 2 | consumer | 同上 | 104 |
| 3 | WG0 | 3 | consumer | 同上 | 104 |
| 4–7 | WG1 | 0–3 | consumer | 同上 | 104 |
| 8–11 | WG2 | 0–3 | consumer | 同上 | 104 |
| 12–15 | WG3 | 0–3 | consumer | 同上 | 104 |
| **16** | **WG4** | **0** | **loader** | `loader::main_loop` | 64（decrease） |
| **17** | **WG4** | **1** | **storer** | `storer::main_loop` | 64 |
| **18** | **WG4** | **2** | **launcher** | `launcher::main_loop` | 64 |
| **19** | **WG4** | **3** | **controller** | `controller::main_loop` | 64 |

4. **预期解释（寄存器增减原因）**：
   - consumer 共 16 个 = 4 个 warpgroup，每个跑张量核算子，需大量寄存器保活累加器/tile → `increase_registers<104>`，减少 spill。
   - loader/storer/launcher/controller 共 4 个 = 1 个 warpgroup（WG4），逻辑瘦 → `decrease_registers<64>`，把寄存器预算让给 consumer。
   - 因为 `setmaxnreg` 是 warpgroup 粒度，所以「consumer 必须是 4 的倍数、职能 warp 必须正好 4 个」是硬件约束的自然结果。
5. **待本地验证**：若你能用 `nvcc --ptx` 生成 PTX，可在分派代码附近搜到形如 `setmaxnreg` 的指令，确认 104/64 确实落到 PTX 层——需带 GPU 工具链确认。

#### 4.3.5 小练习与答案

**练习 1**：如果有人误把 `switch (kittens::warpgroup::warpid())` 改成 `switch (kittens::warpid())`，会发生什么？

**答案**：职能 warp 的绝对号是 16–19，而 `case` 标签只有 0–3，于是这 4 个 warp 全部落到 `default: asm volatile("trap;")`，内核立即触发硬件陷阱崩溃。这正说明用 `warpgroup::warpid()`（组内编号）不是风格选择，而是**正确性必需**——它把 16–19 映射回 0–3 才能对上 case。

**练习 2**：为什么 consumer 用 `if (warpid() < 16)` 判定，而职能 warp 用 `switch` 各取一个？

**答案**：consumer 数量多（16 个）且**行为完全一致**（都跑同一个 `consumer::main_loop`），用范围判断最简洁；职能 warp 只有 4 个且**各不相同**（4 种不同 `main_loop`），且数量正好等于一个 warpgroup，用「组内编号 0–3」做 `switch` 既能一一对应、又能复用 warpgroup 的寄存器分配粒度。

**练习 3**：把 `CONSUMER_REGISTERS` 从 104 调到 128，可能带来什么风险？

**答案**：SM 寄存器总量固定，consumer 多占寄存器会挤压整 block 的预算，可能导致编译器不得不减少每 SM 可驻留的 block 数（与 `__launch_bounds__(…, 1)` 的 `minBlocksPerMultiprocessor=1` 产生张力），甚至在极端情况下启动失败（shared/register 超限）。这是个需要在 GPU 上实测调参的权衡。

---

### 4.4 megakernel_wrapper 与 NoOp 转发

#### 4.4.1 概念说明

`mk` 不直接调 `mk_internal`，中间还隔着一层 `megakernel_wrapper`。这层的全部作用就一句话：**在算子列表最前面插一个 `NoOp`**。`NoOp`（[noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh)）是 opcode 为 0 的「空操作」算子，每个角色的 `run` 都是空转或仅做最小同步。

为什么要强制插入它？因为 [U4·L1] 讲过：所有 SM 队列会被 `NoOp` 填充到等长，于是**指令张量里必然大量存在 opcode=0 的槽位**。VM 必须能正确「认识并跳过」0，否则遇到 0 就会查不到对应算子而崩溃。`megakernel_wrapper` 在编译期把 `NoOp` 注入算子分发表，确保 opcode 0 永远有合法落点。

#### 4.4.2 核心流程

```
mk<config, globals, ops...>(g)
 └─ megakernel_wrapper<config, globals, ops...>::run(g)
      └─ mk_internal<config, globals, NoOp<config>, ops...>(g)
              ↑ 注意：NoOp 被插到了 ops... 最前面
```

#### 4.4.3 源码精读

[megakernel.cuh:158-164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L158-L164) —— `megakernel_wrapper` 把 `NoOp<config>` 插到模板参数最前：

```cpp
// Forward a NoOp to the VM, to ensure that the VM can support zeros.
template <typename config, typename globals, typename... ops>
struct megakernel_wrapper {
    __device__ inline static void run(const globals &g) {
        mk_internal<config, globals, NoOp<config>, ops...>(g);
    }
};
```

注释 "to ensure that the VM can support zeros" 直接点明意图。`NoOp<config>` 的 opcode 在 [noop.cuh:9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L9) 定义为 `0`，各角色的 `run` 都是空/最小同步（如 [noop.cuh:45-48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L45-L48) 的 `consumer::run` 体为空）。

为什么用「插到 `ops...` 前面」而不是「用户自己加」？因为 `mk` 是模板，`ops...` 由调用方（如 [llama.cu:29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L29) 的 `mk<default_config, llama_1b_globals, attention_partial_op, ...>`）决定。如果要求每个调用方都记得加 `NoOp`，迟早会漏；放在 `megakernel_wrapper` 这层统一注入，既保证 opcode 0 始终在分发表里，又对调用方透明。

这层「转发 + 插入」也解释了为什么 [U4·L1] 强调 `NoOp` 填充不是可选项：填充产生的大量 opcode 0，必须能被这层注入的 `NoOp` 算子正确消费，整个流程才闭环。

#### 4.4.4 代码实践

1. **目标**：追踪「`mk` → `megakernel_wrapper` → `mk_internal`」这条转发链，确认 `NoOp` 被插入。
2. **步骤**：
   - 读 [megakernel.cuh:159-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L159-L171)，对比 `mk` 的模板参数 `<config, globals, ops...>` 与 `mk_internal` 实际收到的 `<config, globals, NoOp<config>, ops...>`。
   - 再看一个真实调用点 [llama.cu:29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L29)，数一下用户传了几个 op。
3. **观察**：`mk_internal` 收到的算子列表永远比用户传入的多 1 个，多的那个就是 `NoOp<config>`，且在最前。
4. **预期结果**：你能说清「即使某条 SM 队列全是 NoOp（opcode 全 0），VM 也能正常跑完而不崩溃」的根本原因——因为 `NoOp` 在编译期就被编进了 `dispatch_op` 的分发表（[util.cuh:42-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L42-L55)）。
5. **待本地验证**：无。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `megakernel_wrapper` 是 `struct` 带一个 `static` 成员函数，而不是普通函数？

**答案**：因为它要做**模板偏特化/转发**：把变长模板参数 `<config, globals, ops...>` 接住，再在转发时**追加** `NoOp<config>`。用 `struct` + `static` 成员是 C++ 处理「带变长模板参数的工具层」的常见写法，便于和 `mk` 的模板签名对接（`mk` 里直接 `megakernel_wrapper<config, globals, ops...>::run(g)`）。

**练习 2**：如果删掉 `megakernel_wrapper`，让 `mk` 直接调 `mk_internal<config, globals, ops...>(g)`（不插 `NoOp`），会出什么问题？

**答案**：当某个 SM 队列含 opcode=0 的 `NoOp` 槽位（队列填充必然产生）时，`dispatch_op` 在 [util.cuh:36-40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L36-L40) 的 base case 会 `asm volatile("trap;")`——因为没有 opcode==0 的算子注册，直接触发陷阱崩溃。所以这层插入是正确性必需。

---

## 5. 综合实践：通读「启动 → 初始化 → 分派 → 转发」全链路

把本讲四个模块串起来，完成一次完整的「内核入口走查」：

1. **入口注解**（4.1）：从 [megakernel.cuh:166-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L166-L171) 读出 `__launch_bounds__(640,1)` / `__cluster_dims__(1)` / `__grid_constant__`，确认 640 = 20×32。
2. **转发层**（4.4）：顺着 `mk → megakernel_wrapper::run → mk_internal`，确认 `NoOp` 被插到算子表最前（[megakernel.cuh:158-164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L158-L164)）。
3. **共享内存/信号量初始化**（4.2）：在 [megakernel.cuh:23-111](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L23-L111) 标出三件事——(a) 静态数组声明、(b) 动态 `pages` 对齐安家、(c) 按 `threadIdx.x` 分布式 init 各信号量并 `fence+sync`。把 4.2.3 的信号量速查表抄一遍。
4. **角色分派**（4.3）：在 [megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140) 画出 4.3.4 的 20-warp 角色表，标注每个 warp 的绝对号、warpgroup、组内编号、落点、寄存器上限。
5. **产出**：用一段话（或一张图）讲清「一个 `mk` block 启动后，640 个线程如何先合力初始化共享状态、再按 warp 号散开成 5 类角色、各入各的 `main_loop`」。重点说清两处易错点：为什么 `switch` 用 `warpgroup::warpid()` 而不是 `warpid()`；为什么 consumer 增寄存器、职能 warp 减寄存器。

> 这一步不需要 GPU，全部是源码阅读型实践。完成后你就掌握了 `mk` 内核「从启动到各 warp 就位」的完整画面，下一讲进入各 `main_loop` 内部时就有了稳固的脚手架。

---

## 6. 本讲小结

- **`mk` 内核本体**只做转发，真正的约束来自三个注解：`__launch_bounds__(640,1)`（编译器寄存器预算）、`__cluster_dims__(1)`（簇规模）、`__grid_constant__`（globs 进常量缓存）——[megakernel.cuh:166-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L166-L171)。
- **初始化主线**：声明静态共享数组 → 动态 `pages` 对齐到 1024B → 构造 `state mks` → 按 `threadIdx.x` 分布式 init 各信号量 → `fence + __syncthreads + everyone::sync` 让初值全可见。
- **信号量分两类**：`page_finished` / `tensor_finished` 被预 arrive（初始已满足）；`instruction_arrived`（阈值1）/ `instruction_finished`（阈值19）/ `semaphores_ready`（阈值1）不预 arrive（等 controller 放行）。
- **角色分派两层次**：`if (warpid() < 16)` 选出 16 个 consumer；`else` 里 `switch (warpgroup::warpid())` 用**组内编号 0–3** 把绝对 16–19 号 warp 分别派给 loader/storer/launcher/controller——这是 4 个职能 warp 恰好组成第 5 个 warpgroup 的直接结果。
- **寄存器再分配**：consumer `increase_registers<104>`（保活张量核数据、减少 spill），职能 warp `decrease_registers<64>`（逻辑瘦、让出预算），底层是 warpgroup 粒度的 `setmaxnreg`——这解释了「consumer 必须是 4 的倍数」。
- **`megakernel_wrapper`** 在算子表最前强制插入 `NoOp`（opcode 0），保证 VM 能正确处理队列填充产生的全 0 槽位而不触发 `trap`。

---

## 7. 下一步学习建议

1. **进入各 `main_loop` 内部**：本讲到「各 warp 就位」为止。下一步建议精读 [util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304) 的 `MAKE_WORKER` 宏，看它生成的 `await_instruction → dispatch_op → next_instruction` 骨架，理解 consumer/loader/storer/launcher 如何共享同一段循环模板。
2. **精读 controller**：[controller/controller.cuh:14-165](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7fe454a6cde578e/include/controller/controller.cuh#L14-L165) 是唯一手写的 `main_loop`，串联了取指、页分配、信号量构造三步，是把本讲的 `instruction_arrived`/`instruction_finished` 真正「点亮」的地方。
3. **补齐同步原语细节**：本讲对信号量只讲了「阈值 + 是否预 arrive」；相位的 `wait(s, phase_bit)` 算术、`invalidate_semaphore`、双 buffer 流水线的相位翻转，留给 [U5·L1] 与后续讲义展开。
4. **对照 Python 侧**：重读 [U4·L1] 的 `NoOp` 填充与 [U4·L2] 的张量化，确认「Python 把队列补齐到 opcode 含 0 → `megakernel_wrapper` 编译期注入 `NoOp` → 运行期 `dispatch_op` 命中」这条闭环。
