# config.cuh：VM 配置与共享内存布局

> 本讲对应手册单元 U5·L1，承接 [U1·L3]（编译并运行 low-latency-llama demo）。前面你已经能把 `mk_llama` 跑起来，但「内核里到底有几个 warp、共享内存怎么切」这些数字从哪来的，一直被我们略过了。本讲就回到这一切数字的**唯一权威出处**——[include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) 里的 `default_config`，把 VM 的硬件参数表逐行拆开。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `NUM_CONSUMER_WARPS` / `NUM_WARPS` / `NUM_THREADS` 三者关系，并解释为什么内核一共 20 个 warp、640 个线程。
2. 手算 `STATIC_SHARED_MEMORY` 的值（10 248... 不对，是 10 496 字节），并解释公式里 `×4`、`×8`、`×INSTRUCTION_PIPELINE_STAGES` 各自对应共享内存里的哪一块。
3. 解释 `MAX_SHARED_MEMORY → DYNAMIC_SHARED_MEMORY → NUM_PAGES` 的推导链，并说清为什么文件末尾会有 `static_assert(NUM_PAGES == 13)`。
4. 理解 `INSTRUCTION_WIDTH` / `TIMING_WIDTH` / `DYNAMIC_SEMAPHORES` 三个「宽度」如何决定每条指令在共享内存里的固定布局。
5. 看懂 `CONSUMER_REGISTERS` / `NON_CONSUMER_REGISTERS` 这对寄存器配额，以及它们和 `__launch_bounds__(NUM_THREADS, 1)` 的配合关系。

## 2. 前置知识

- **CUDA warp（线程束）**：GPU 上线程调度的基本单位，固定 32 个线程为一个 warp。本仓库里所有「几个 warp 干活」的说法，都以 32 为单位换算成线程数。
- **共享内存（shared memory）**：每个 SM 上的一块高速片上内存，所有线程块内线程可见、低延迟。Hopper（H100）上每个块**最多可申请 228 KiB**（233 472 字节）。它在 CUDA 里有两种声明方式：
  - **静态共享内存**：`__shared__ T x[N];`，大小在编译期写死，编译器替你分配。
  - **动态共享内存**：`extern __shared__ T x[];`，大小在**启动内核时**才通过 `cudaFuncSetAttribute(..., cudaFuncAttributeMaxDynamicSharedMemorySize, ...)` 指定。本仓库把「页（page）」这种运行时才知道要多大的数据放在动态区。
- **`constexpr` 与 `static_assert`**：C++ 的编译期常量与编译期断言。`default_config` 里几乎每个字段都是 `static constexpr int`，意味着这些值在**编译时**就固定，并且可以被 `static_assert` 检查——改错一个参数，编译直接报错而不是运行时崩溃。
- **寄存器配额（registers per thread）**：每个线程能用多少个寄存器是有限的。给计算密集的 warp 多分寄存器、给搬运数据的 warp 少分寄存器，是 GPU kernel 常见的调优手段。H100 每个 SM 有 65 536 个寄存器。
- **ThunderKittens（`kittens`）**：本仓库依赖的 CUDA 抽象库，提供 `kittens::semaphore`（异步信号量）、`kittens::WARP_THREADS`、`kittens::MAX_SHARED_MEMORY` 等常量。注意它在本仓库里是 git 子模块（见 `.gitmodules`），如果本地没 `git submodule update`，`ThunderKittens/` 目录会是空的，本讲里凡引用 kittens 常量的地方都会标注「需本地确认」。

> 如果你还不清楚「指令 / 页 / 信号量」这套 VM 抽象，建议先翻一眼 [U3·L1]（base globals 与 instruction）第 4 节，再回来。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | `default_config` 全部参数的定义 | **本讲主角**，逐行精读 |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | VM 主内核 `mk` | 如何用 config 字段声明静态/动态共享内存、按 warp 分工、设 launch bounds |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `instruction_state_t` / `page` / `state` 等结构 | config 字段如何变成共享内存里的结构体布局 |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | controller warp 主循环 | 对 `DYNAMIC_SEMAPHORES` / `NUM_PAGES` 的编译期约束 |
| [include/controller/instruction_fetch.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh) | 取指令 | 对 `INSTRUCTION_WIDTH` 的约束与用法 |
| [include/controller/page_allocator.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh) | 页分配器 | `NUM_PAGES` 如何塞进一个 32 位 warp 掩码 |
| [demos/low-latency-llama/llama.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) | llama demo 的 globals | `block()` / `dynamic_shared_memory()` 如何把 config 翻译成启动参数 |
| [demos/low-latency-llama/llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) | pybind11 入口 | 用 `default_config` 实例化内核 `mk` |

## 4. 核心概念与源码讲解

本讲把 `default_config` 拆成三个最小模块：**(A) warp / 线程配置**、**(B) page 与共享内存划分**、**(C) instruction / timing 布局**。它们之间不是独立的——C 决定了「静态共享内存」有多大，B 用静态大小算出「动态共享内存」从而数出页数，A 决定了谁来读写这些页。建议按 A → C → B 的依赖顺序理解，但下面按规格顺序讲解。

先把整个结构体速览一遍：

[include/config.cuh:7-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L7-L52) 定义了 `default_config`，里面全是 `static constexpr` 字段。下面这张表把所有字段归类，后面三小节再逐一展开。

| 类别 | 字段 | 值 | 一句话作用 |
| --- | --- | --- | --- |
| 流水线 | `INSTRUCTION_PIPELINE_STAGES` | 2 | 指令双缓冲级数 |
| 流水线 | `INSTRUCTION_PIPELINE_STAGES_BITS` | 1 | 表示级数所需的位数（⌈log₂2⌉=1） |
| 指令布局 | `INSTRUCTION_WIDTH` | 32 | 每条指令 32 个 int（128 字节） |
| 计时布局 | `TIMING_WIDTH` | 128 | 每条指令的计时槽位数 |
| 信号量 | `DYNAMIC_SEMAPHORES` | 32 | 每条指令最多用 32 个动态信号量 |
| warp 配置 | `NUM_CONSUMER_WARPS` | 16 | 计算 warp 数 |
| warp 配置 | `NUM_WARPS` | 20 | 16 计算 + 4 服务 |
| warp 配置 | `NUM_THREADS` | 640 | 20 × 32 |
| 块/簇 | `NUM_BLOCKS` / `CLUSTER_BLOCKS` | 1 / 1 | 单块、单簇 |
| 共享内存 | `MAX_SHARED_MEMORY` | kittens 常量（Hopper 228 KiB） | 块共享内存上限 |
| 共享内存 | `SCRATCH_BYTES` | 4096 | 每条指令的临时 scratch 区 |
| 共享内存 | `STATIC_SHARED_MEMORY` | 10 496（编译期算出） | 静态区大小 |
| 共享内存 | `DYNAMIC_SHARED_MEMORY` | 222 976（Hopper） | 动态区大小 |
| 页 | `PAGE_SIZE` | 16384（16 KiB） | 单页字节数 |
| 页 | `NUM_PAGES` | 13（编译期算出） | 动态区能放多少整页 |
| 寄存器 | `CONSUMER_REGISTERS` | 104 | 计算 warp 每线程寄存器数 |
| 寄存器 | `NON_CONSUMER_REGISTERS` | 64 | 服务 warp 每线程寄存器数 |
| 调试 | `TIMING_RECORD_ENABLED` | false | 是否记录计时 |
| 调试 | `GMEM_SPIN_LOOP_SLEEP_NANOS` | 20（声明为 `bool`） | 全局内存自旋等待睡眠（见正文说明） |

---

### 4.1 warp / 线程配置

#### 4.1.1 概念说明

Megakernels 把一个 GPU 线程块（block）改造成一台**虚拟机**：块里的 warp 被分成两类——

- **计算 warp（consumer）**：负责真正做矩阵乘、attention 等重计算。它们数量最多，是算力主体。
- **服务 warp**：负责取指令（controller）、从全局内存搬数据进来（loader）、把结果搬出去（storer）、发射 Tensor Memory Accelerator / wgmma 等硬件动作（launcher）。它们是 VM 的「操作系统」和「I/O 通道」。

之所以要固定 4 个服务 warp，是因为这四个角色各自有独立的 `main_loop`，且彼此通过共享内存里的信号量解耦同步（见 [U4·L1] 的 SM 分配策略）。`default_config` 用「1 个 controller + 1 个 loader + 1 个 storer + 1 个 launcher」的固定组合，再叠上 16 个 consumer。

> 注意：[config.cuh:24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L24) 的注释写的是 "One controller warp, one load warp, one store warp, and one **mma** warp"。这里的 "mma" 是历史措辞，实际的第 4 个服务 warp 现在是 launcher（见 4.1.3 的 switch 分发）。注释和代码已不完全一致，以 switch 为准。

#### 4.1.2 核心流程

warp → 线程的推导是一条简单的乘法链：

```
NUM_CONSUMER_WARPS = 16          # 计算主体
NUM_WARPS          = 4 + 16 = 20 # 4 个服务 + 16 个计算
NUM_THREADS        = NUM_WARPS × WARP_THREADS(=32) = 640
```

内核启动后，每个线程先看自己的 warp 编号：

- `warpid() < NUM_CONSUMER_WARPS`（即前 16 个 warp）→ consumer，调高寄存器配额，进入 `consumer::main_loop`；
- 否则（后 4 个 warp）→ 服务 warp，调低寄存器配额，再按 `warpgroup::warpid()` 在 `{loader, storer, launcher, controller}` 中选一个 `main_loop`。

寄存器配额这一步是关键：consumer 每线程 **104** 个寄存器，服务 warp 每线程 **64** 个寄存器。粗算一下整块是否塞得下单 SM 的寄存器文件：

\[ 512\text{(consumer 线程)} \times 104 + 128\text{(服务线程)} \times 64 = 53\,248 + 8\,192 = 61\,440 \;\leq\; 65\,536 \]

这就是为什么 `__launch_bounds__(NUM_THREADS, 1)` 敢于要求「每个 SM 至少跑满 1 个这样的块」——640 个线程连同这套寄存器配额刚好放得进 H100 一个 SM 的 65 536 个寄存器。

#### 4.1.3 源码精读

warp 数量的定义在三行里完成（[config.cuh:25-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L25-L28)）：

```cpp
static constexpr int NUM_CONSUMER_WARPS = 16;
static constexpr int NUM_WARPS = 4 + NUM_CONSUMER_WARPS;            // = 20
static constexpr int NUM_THREADS = NUM_WARPS * ::kittens::WARP_THREADS; // = 640
static constexpr int NUM_BLOCKS = 1;
static constexpr int CLUSTER_BLOCKS = 1;
```

这里 `::kittens::WARP_THREADS` 就是 32（CUDA 标准 warp 大小，ThunderKittens 把它定义成常量；本仓库子模块未拉取时需本地确认，但值必然是 32）。`NUM_BLOCKS = 1` / `CLUSTER_BLOCKS = 1` 表示这台 VM 单块单簇运行——一个 block 就是一台完整的虚拟机。

寄存器配额在 [config.cuh:50-51](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L50-L51)：

```cpp
static constexpr int CONSUMER_REGISTERS = 104;
static constexpr int NON_CONSUMER_REGISTERS = 64;
```

它们在主内核里被实际使用。看 [megakernel.cuh:118-139](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L139)：

```cpp
if (kittens::warpid() < config::NUM_CONSUMER_WARPS) {
    kittens::warpgroup::increase_registers<config::CONSUMER_REGISTERS>(); // 104
    ::megakernel::consumer::main_loop<config, globals, ops...>(g, mks);
} else {
    kittens::warpgroup::decrease_registers<config::NON_CONSUMER_REGISTERS>(); // 64
    switch (kittens::warpgroup::warpid()) {
    case 0: ::megakernel::loader::main_loop<...>(g, mks);     break;
    case 1: ::megakernel::storer::main_loop<...>(g, mks);     break;
    case 2: ::megakernel::launcher::main_loop<...>(g, mks);   break;
    case 3: ::megakernel::controller::main_loop<...>(g, mks); break;
    default: asm volatile("trap;");
    }
}
```

这段把 config 的抽象数字落成了硬件动作：先按 warp 编号分敌我，再调整寄存器配额，最后分发到对应角色的主循环。`default: asm volatile("trap;")` 是一道防线——如果哪天 `NUM_WARPS` 改得不再是 `4 + 16`，多出来的服务 warp 会直接触发 trap 而不是悄悄跑飞。

`NUM_THREADS` 和 `CLUSTER_BLOCKS` 最终喂给内核的启动边界（[megakernel.cuh:166-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L166-L171)）：

```cpp
template <typename config, typename globals, typename... ops>
__launch_bounds__(config::NUM_THREADS, 1)
    __cluster_dims__(config::CLUSTER_BLOCKS) __global__
    void mk(const __grid_constant__ globals g) { ... }
```

`__launch_bounds__(640, 1)` 告诉编译器：这个内核每块最多 640 线程，且每个 SM 至少安排 1 个块——编译器据此决定每线程能用多少寄存器（配合上面的 104/64 手动设置）。`__cluster_dims__(1)` 则把线程块簇大小也固定为 1。

最后，`NUM_THREADS` 还会通过 globals 翻译成启动时的 block 维度（[llama.cuh:144-146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L144-L146)）：

```cpp
dim3 grid() { return dim3(sm_count); }     // 每个 SM 一台 VM
dim3 block() { return dim3(config::NUM_THREADS); }   // 640 线程
int dynamic_shared_memory() { return config::DYNAMIC_SHARED_MEMORY; }
```

#### 4.1.4 代码实践

**实践目标**：验证「20 warp / 640 线程」这条推导链，并看清寄存器配额如何影响每 SM 的占用。

**操作步骤**（源码阅读型实践）：

1. 打开 [config.cuh:25-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L25-L27)，按 `4 + 16`、`20 × 32` 手算，确认 `NUM_WARPS=20`、`NUM_THREADS=640`。
2. 打开 [megakernel.cuh:118-122](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L122)，确认 consumer 走 `increase_registers<104>`、服务 warp 走 `decrease_registers<64>`。
3. 用上面的寄存器公式算一次：`512×104 + 128×64 = 61 440`，对比 H100 每 SM 的 65 536。
4. （可选）想直接看到这些数字，可以在 demo 的 `llama.cu` 的 `PYBIND11_MODULE` 之前加一句 `megakernel::print_config<default_config>();`，重新 `make` 后导入模块即会打印（`print_config` 定义在 [config.cuh:58-81](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L58-L81)）。

**需要观察的现象**：`NUM_CONSUMER_WARPS=16 / NUM_WARPS=20 / NUM_THREADS=640` 三行依次出现；寄存器总和 61 440 小于 65 536。

**预期结果**：推导链成立；若你把 `NUM_CONSUMER_WARPS` 想象成 18（即 22 warp、704 线程），重算 `576×104 + 128×64 = 68 096 > 65 536`，就会超过单 SM 寄存器上限——这正是 16 这个数字被选定的约束之一。**待本地验证**：在装有 H100/B200 的机器上 `make` 后 `python -c "import mk_llama"` 看 `print_config` 输出。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NUM_CONSUMER_WARPS` 改成 8，`NUM_THREADS` 会变成多少？整块寄存器占用是多少？

**答案**：`NUM_WARPS = 4 + 8 = 12`，`NUM_THREADS = 12 × 32 = 384`。consumer 线程数 = 8×32 = 256，服务线程数 = 4×32 = 128；寄存器占用 = `256×104 + 128×64 = 26 624 + 8 192 = 34 816`，远低于 65 536。

**练习 2**：`__launch_bounds__(config::NUM_THREADS, 1)` 里第二个参数 `1` 的含义是什么？为什么 Megakernels 敢设成 1？

**答案**：第二个参数是 `minBlocksPerMultiprocessor`，表示「编译器应保证每个 SM 至少能并发跑这么多块」。设成 1 意味着允许每个线程占用更多寄存器（因为不需要在 SM 上塞第二块）。Megakernels 一个块就是一台完整 VM，要独占尽量多的寄存器/共享内存，所以刻意只跑 1 块/SM。

---

### 4.2 page 与共享内存划分

#### 4.2.1 概念说明

VM 的「工作内存」是**页（page）**：一块固定大小（`PAGE_SIZE = 16 KiB`）的共享内存区域，loader 把数据搬进某个物理页，consumer 从页里读、storer 把页里结果搬出去。页的数量 `NUM_PAGES` 直接决定 VM 同时能持有多少块中间数据——这又完全由「共享内存还剩多少」决定。

难点在于：H100 的共享内存上限是固定的（228 KiB），而这块内存要被**静态**和**动态**两拨人瓜分：

- **静态区**（`__shared__` 声明）：指令缓冲、信号量、计时槽等编译期就确定的结构。这部分由编译器分配，`extern __shared__` 看不见。
- **动态区**（`extern __shared__`）：页数组放在这里，因为它的总大小 = `NUM_PAGES × PAGE_SIZE`，要等 config 算完才知道。

所以推导链是：先算出静态区有多大，再用「上限 − 静态」得到动态区，最后「动态区 ÷ 页大小」数出页数。`config.cuh` 用一行公式把这条链编码成了编译期常量，并在末尾用 `static_assert(NUM_PAGES == 13)` 把结果钉死。

#### 4.2.2 核心流程

共享内存划分的完整推导（以 Hopper `MAX_SHARED_MEMORY = 233 472` 为例，该值来自 kittens，需本地确认）：

```
                     ┌──────────── MAX_SHARED_MEMORY = 233 472 (228 KiB) ────────────┐
                     │                                                                │
        STATIC_SHARED_MEMORY (10 496)                DYNAMIC_SHARED_MEMORY (222 976)  │
        ┌─────────────────────────────┐              ┌──────────────────────────┐     │
        │ 指令缓冲/计时/信号量/scratch │              │ page[0] ... page[12]     │ 尾部 │
        │ (编译器分配的 __shared__)    │              │ 13 × 16 KiB = 212 992    │ 9 984│
        └─────────────────────────────┘              └──────────────────────────┘     │
                                                                                       │
   NUM_PAGES = DYNAMIC_SHARED_MEMORY / PAGE_SIZE = 222 976 / 16 384 = 13 (整除) ───────┘
```

静态区公式（[config.cuh:34-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L34-L37)）逐项展开：

\[ \text{STATIC} = 512 + \underbrace{S}_{\text{级数}} \times \Big(\underbrace{4096}_{\text{SCRATCH}} + \underbrace{(32+128)\times 4}_{\text{指令+计时}} + \underbrace{32\times 8}_{\text{信号量}}\Big) \]

- `512`：给「散落」的信号量（`page_finished`、`instruction_arrived/finished`、`semaphores_ready` 等）预留的保守预算，是个估算上取整的常数，不是精确账。
- 括号内是**每条流水级**的固定开销：`SCRATCH_BYTES=4096` 临时区 + 指令与计时槽（`(INSTRUCTION_WIDTH+TIMING_WIDTH)×4` 个字节，`×4` 是因为 `int` 占 4 字节）+ 动态信号量数组（`DYNAMIC_SEMAPHORES×8`，每个 `kittens::semaphore` 8 字节）。
- 再乘以 `INSTRUCTION_PIPELINE_STAGES=2`（双缓冲，有两份），就得到静态区主体。

把数代进去（详见 4.2.4 的手算）：静态 = `512 + 2×(4096+640+256) = 512 + 9984 = 10 496` 字节。

#### 4.2.3 源码精读

先看三段核心定义。共享内存上限直接借用 kittens（[config.cuh:30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L30)）：

```cpp
static constexpr int MAX_SHARED_MEMORY = ::kittens::MAX_SHARED_MEMORY;
```

> 这个常量在 ThunderKittens 子模块里定义。Hopper（H100）上是 228 KiB = 233 472 字节；Blackwell（B200）会更大。**需本地确认**：在你的机器上 `grep -rn MAX_SHARED_MEMORY ThunderKittens/include/` 查实际值。

静态/动态划分与页数（[config.cuh:33-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L33-L44)）：

```cpp
static constexpr int SCRATCH_BYTES = 4096;
static constexpr int STATIC_SHARED_MEMORY =
    512 + INSTRUCTION_PIPELINE_STAGES *
              (SCRATCH_BYTES + (INSTRUCTION_WIDTH + TIMING_WIDTH) * 4 +
               DYNAMIC_SEMAPHORES * 8);
static constexpr int DYNAMIC_SHARED_MEMORY =
    ::kittens::MAX_SHARED_MEMORY - STATIC_SHARED_MEMORY;

static constexpr int PAGE_SIZE = 16384;
static constexpr int NUM_PAGES = DYNAMIC_SHARED_MEMORY / PAGE_SIZE;
static_assert(NUM_PAGES == 13, "NUM_PAGES must be 13");
```

注意 `NUM_PAGES` 用的是**整数除法**（C++ 中两个 `int` 相除向下取整），这正是后面 4.2.4 要解释「为什么是 13 而不是 14」的关键。

这些常量如何变成真实的共享内存？看主内核里的声明（[megakernel.cuh:23-39](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L23-L39)）：

```cpp
__shared__ alignas(128) instruction_state_t<config>
    instruction_state[config::INSTRUCTION_PIPELINE_STAGES];        // 静态：2 份指令状态
__shared__ kittens::semaphore
    page_finished[config::NUM_PAGES][config::INSTRUCTION_PIPELINE_STAGES_BITS],
    instruction_arrived[config::INSTRUCTION_PIPELINE_STAGES],
    instruction_finished[config::INSTRUCTION_PIPELINE_STAGES],
    semaphores_ready;                                              // 静态：散落信号量
extern __shared__ int __shm[];                                     // 动态区起点
void *aligned_shm_addr =
    (void *)((1023 + (uint64_t)&__shm[0]) & ~(uint64_t)1023);      // 向上对齐到 1024
typename state<config>::page_array_t &pages =                      // 把动态区解释成页数组
    *reinterpret_cast<typename state::page_array_t *>(aligned_shm_addr);
```

也就是说：静态区里的东西用普通 `__shared__` 声明（对应公式里的 STATIC 部分），动态区只有一句 `extern __shared__ int __shm[]`，然后手动对齐到 1024 字节、`reinterpret_cast` 成 `page_array_t`（即 `page<config>[NUM_PAGES]`）。`page` 结构本身见 [util.cuh:60-68](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L60-L68)，大小正好是 `PAGE_SIZE/sizeof(int)` 个 int = 16 KiB；页数组类型见 [util.cuh:142-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L142-L143)。

`NUM_PAGES` 还出现在多处需要它「不超过 32」的地方。controller 主循环开头就有硬约束（[controller.cuh:21-22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L21-L22)）：

```cpp
static_assert(config::DYNAMIC_SEMAPHORES <= 32);
static_assert(config::NUM_PAGES <= 32);
```

为什么是 32？因为一个 warp 正好 32 个 lane，页分配用 lane 编号当页号、用一个 32 位掩码做 warp 级同步。看页分配器（[page_allocator.cuh:26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L26)）：

```cpp
constexpr uint32_t membermask = 0xFFFFFFFF >> (32 - config::NUM_PAGES);
```

`NUM_PAGES=13` 时掩码 = `0x1FFF`（低 13 位为 1），用于 `bar.warp.sync` 只同步参与页分配的那 13 个 lane。如果 `NUM_PAGES` 超过 32，这套「lane = 页号」的映射就崩溃了——这正是上面那条 `static_assert` 防的。

最后，`DYNAMIC_SHARED_MEMORY` 在启动时被报给运行时，让驱动分配这么多动态共享内存（[llama.cuh:146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L146) 的 `dynamic_shared_memory()`），由 `bind_kernel` 在启动内核前转成 `cudaFuncAttributeMaxDynamicSharedMemorySize` 设置。

#### 4.2.4 代码实践（本讲主任务）

**实践目标**：亲手算出 `STATIC_SHARED_MEMORY` 与 `NUM_PAGES`，并解释 `static_assert(NUM_PAGES == 13)` 存在的原因。

**操作步骤**：

1. 从 [config.cuh:9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L9)、[:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14)、[:18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L18)、[:22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L22)、[:33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L33) 抄出五个原始数字：`STAGES=2`、`INSTRUCTION_WIDTH=32`、`TIMING_WIDTH=128`、`DYNAMIC_SEMAPHORES=32`、`SCRATCH_BYTES=4096`。
2. 算「每级」开销：

   - 指令+计时：`(32 + 128) × 4 = 640` 字节（`×4` 把 int 个数换成字节）。
   - 信号量：`32 × 8 = 256` 字节（每个 `kittens::semaphore` 8 字节）。
   - scratch：`4096` 字节。
   - 每级合计：`4096 + 640 + 256 = 4992` 字节。

3. 乘级数、加底数：`2 × 4992 = 9984`；`9984 + 512 = 10 496` 字节 ⇒ **`STATIC_SHARED_MEMORY = 10 496`**（= 10.25 KiB）。
4. 取 Hopper 上限 `MAX_SHARED_MEMORY = 233 472`（228 KiB，kittens 值，**待本地确认**），算动态区：`233 472 − 10 496 = 222 976` 字节 ⇒ **`DYNAMIC_SHARED_MEMORY = 222 976`**。
5. 数页：`NUM_PAGES = 222 976 / 16 384 = 13.609… → 13`（整数除法向下取整）⇒ **`NUM_PAGES = 13`**，与 `static_assert` 吻合。

**需要观察的现象 / 解释为何断言为 13**：

- 13 个整页实际只占 `13 × 16 384 = 212 992` 字节，动态区还剩 `222 976 − 212 992 = 9 984` 字节**尾部空闲**（页数组用不到，被浪费）。
- 想要 14 页，需要动态区 ≥ `14 × 16 384 = 229 376` 字节，即 `MAX_SHARED_MEMORY ≥ 229 376 + 10 496 = 239 872` 字节（≈ 234.25 KiB），**超过 Hopper 的 228 KiB 硬件上限**。所以 13 是「扣完成本后能塞下的最大整页数」，不是随手写的。
- 那为什么要 `static_assert` 而不是让它自由计算？因为 `NUM_PAGES` 被太多地方当**定值**用：`page_finished[NUM_PAGES][BITS]` 的数组维度、[page_allocator.cuh:26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L26) 的 32 位 membermask、[controller.cuh:22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L22) 的 `NUM_PAGES <= 32`。如果有人改了 `SCRATCH_BYTES`、`DYNAMIC_SEMAPHORES`，或换了 `MAX_SHARED_MEMORY` 不同的 GPU，整除可能**静默**地把页数变成 12 或 14，进而撑爆某个 32 位假设或让某条 `bar.warp.sync` 掩码错位。`static_assert(NUM_PAGES == 13)` 把这种「静默的数值漂移」变成**编译期报错**，附带消息 `"NUM_PAGES must be 13"`，提醒你同步更新所有依赖页数的代码。

**预期结果**：手算得到 `STATIC=10 496`、`DYNAMIC=222 976`、`NUM_PAGES=13`，与代码一致；能复述「13 是硬件上限减去静态成本后的最大整页数，断言防止参数改动导致页数漂移」。**待本地验证**：换用 B200（`KITTENS_BLACKWELL`）时 `MAX_SHARED_MEMORY` 更大，重算 `NUM_PAGES` 会大于 13，此时该 `static_assert` 会**编译失败**——这是把 demo 从 H100 移植到 B200 时必须先处理的一处。

#### 4.2.5 小练习与答案

**练习 1**：若把 `SCRATCH_BYTES` 从 4096 翻倍到 8192（其余不变，Hopper），`NUM_PAGES` 会变成多少？

**答案**：每级 = `8192 + 640 + 256 = 9088`；静态 = `512 + 2×9088 = 18 688`；动态 = `233 472 − 18 688 = 214 784`；`214 784 / 16 384 = 13.10… → 13`。仍是 13（因为还剩足够空间放 13 整页），所以 `static_assert` 不会触发。但尾部空闲缩到 `214 784 − 212 992 = 1792` 字节，余量变紧。

**练习 2**：`NUM_PAGES = DYNAMIC_SHARED_MEMORY / PAGE_SIZE` 用的是整数除法。如果改成「向上取整」会出什么问题？

**答案**：向上取整会让 `NUM_PAGES = 14`，但动态区实际只有 13 个整页（13.6 页）的空间。`page_array_t` 会声明 14 个 16 KiB 的页 = 229 376 字节，超过动态区 222 976 字节，运行时越界写共享内存，行为未定义。所以这里**必须**向下取整。

**练习 3**：`static_assert(NUM_PAGES <= 32)`（[controller.cuh:22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L22)）和 `membermask = 0xFFFFFFFF >> (32 - NUM_PAGES)`（[page_allocator.cuh:26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L26)）是什么关系？

**答案**：页分配器用「warp 内 lane 编号 = 物理页号」的映射，并用一个 32 位掩码调用 `bar.warp.sync` 只同步前 `NUM_PAGES` 个 lane。掩码最多 32 位，所以 `NUM_PAGES` 不能超过 32；那条 `static_assert` 就是这条数学约束的编译期守卫。

---

### 4.3 instruction / timing 布局

#### 4.3.1 概念说明

VM 的「程序」是一串**指令**，每条指令是一段固定大小的数据，存在全局内存里、由 controller 取到共享内存。为了让取指、解码、执行能**重叠**（流水线），Megakernels 给每条指令维护一整套伴随状态：

- **指令本体**（`instruction_t`）：`INSTRUCTION_WIDTH` 个 int，第 0 个 int 是 opcode，其余是指令参数。
- **计时槽**（`timing_t`）：`TIMING_WIDTH` 个 int，记录这条指令在各阶段的时钟戳，用于性能剖析（默认关闭）。
- **动态信号量数组**：每条指令最多用 `DYNAMIC_SEMAPHORES` 个信号量，在 consumer/loader/storer 之间做生产者-消费者同步。

这三个「宽度」直接决定了 4.2 里那块**每级静态开销**的大小，所以它们既是「指令格式」也是「内存预算」。

此外还有两个派生量：

- `INSTRUCTION_PIPELINE_STAGES`：指令流水线级数（=2，双缓冲）。controller 在执行第 N 条指令时，可以同时取第 N+1 条。
- `INSTRUCTION_PIPELINE_STAGES_BITS`：表示「级数」所需的位数。双缓冲只有两个槽，用 1 个 bit 的相位（phase bit）就能在信号量里区分「这一轮」和「上一轮」——这是异步信号量常见的相位翻转技巧。

#### 4.3.2 核心流程

一条指令在共享内存里的伴随状态由结构体 `instruction_state_t<config>` 固定下来（每级一份，共 `STAGES` 份）：

```
instruction_state_t<config> {            // 每级一份，alignas(128)
    instruction_t instructions;          // INSTRUCTION_WIDTH=32 个 int = 128 字节
    timing_t      timings;               // TIMING_WIDTH=128 个 int = 512 字节
    int           pid_order[NUM_PAGES];  // 13 个 int (+ padding 到 32 对齐)
    semaphore     semaphores[DYNAMIC_SEMAPHORES]; // 32 个 × 8 字节 = 256 字节
    int           scratch[SCRATCH_BYTES/4];        // 1024 个 int = 4096 字节
}
```

把这个结构体的字节数和 4.2.2 的「每级开销」对一下：`128 + 512 + (pid_order+padding) + 256 + 4096`，其中指令+计时 = 640、信号量 = 256、scratch = 4096，正好是公式里括号内那三项（`pid_order` 等小字段被那个 `512` 底数吸收）。所以 config 公式不是凭空写的，它就是 `instruction_state_t` 的大小 × 级数，外加一点常数余量。

取指时，controller 用 lane 并行搬运：因为一条指令有 `INSTRUCTION_WIDTH=32` 个 int，正好等于一个 warp 的 32 个 lane，于是「1 个 lane 搬 1 个 int」一次取完。这也是 `INSTRUCTION_WIDTH <= 32` 那条断言的由来——超过 32 就不能一个 warp 一次取完了。

#### 4.3.3 源码精读

三个宽度与类型别名（[config.cuh:14-22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14-L22)）：

```cpp
static constexpr int INSTRUCTION_WIDTH = 32; // 128 bytes per instruction.
using instruction_t = int[INSTRUCTION_WIDTH];

static constexpr int TIMING_WIDTH = 128;
using timing_t = int[TIMING_WIDTH];

static constexpr int DYNAMIC_SEMAPHORES = 32;
```

注释 `// 128 bytes per instruction.` 正好对应 `32 int × 4 字节 = 128 字节`。`instruction_t` / `timing_t` 是两个类型别名，下面会作为 `instruction_state_t` 的成员类型。

流水线级数与位数（[config.cuh:9-12](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L9-L12)）：

```cpp
static constexpr int INSTRUCTION_PIPELINE_STAGES = 2;
// num bits required to represent num pipeline stages
static constexpr int INSTRUCTION_PIPELINE_STAGES_BITS = 1;
```

`BITS = 1` 是因为 2 级只需 1 位相位。它被用作二维数组 `page_finished[NUM_PAGES][BITS]` 的第二维（见 [megakernel.cuh:26-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L26-L27) 和 [util.cuh:145-148](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L145-L148)）：每页每级一个信号量，用相位 bit 区分「第 N 轮」与「第 N+2 轮」复用同一个信号量槽。注意这套设计目前**硬编码假设级数=2**，[semaphore_constructor.cuh:25-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L25-L26) 直接 `static_assert(INSTRUCTION_PIPELINE_STAGES == 2, "Need to be changed.")`——改级数会触发它。

伴随状态结构体（[util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19)）：

```cpp
template <typename config> struct __align__(128) instruction_state_t {
    config::instruction_t instructions;                       // 32 int
    config::timing_t timings;                                 // 128 int
    int pid_order[config::NUM_PAGES];                         // 13 int
    int padding[((config::NUM_PAGES + 31) & ~31) - config::NUM_PAGES]; // 凑齐 32 的倍数
    kittens::semaphore semaphores[config::DYNAMIC_SEMAPHORES]; // 32 个
    int scratch[config::SCRATCH_BYTES / 4];                   // 1024 int
};
```

注意 `padding` 把 `pid_order` 凑成 32 的整数倍——方便用 warp 级的 `__shfl_sync(0xffffffff, ...)` 整组广播（见 [controller.cuh:119-123](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L119-L123)）。`__align__(128)` 让每级状态 cache 行对齐，避免跨级伪共享。

取指时如何用满一个 warp（[instruction_fetch.cuh:22-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L22-L26)）：

```cpp
static_assert(config::INSTRUCTION_WIDTH <= 32);
if (laneid < config::INSTRUCTION_WIDTH) {
    instruction[laneid] = src_ptr[laneid];   // lane i 搬第 i 个 int
}
```

`INSTRUCTION_WIDTH=32`、warp 有 32 个 lane，恰好一一对应；断言保证「不会出现一条指令装不满一个 warp」的情形。

最后，这两个宽度还被 `instruction_layout` / `timing_layout` 包装成 kittens 全局张量布局（[config.cuh:53-56](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L53-L56)），供 globals 结构在全局内存里摆放指令表和计时表（详见 [U3·L1]）：

```cpp
template <typename config>
using instruction_layout = kittens::gl<int, 1, -1, -1, config::INSTRUCTION_WIDTH>;
template <typename config>
using timing_layout = kittens::gl<int, 1, -1, -1, config::TIMING_WIDTH>;
```

> 旁注：[util.cuh:69-71](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L69-L71) 还有一个 `mini_page<config>` 模板引用 `config::MINI_PAGE_SIZE`，但 `default_config` **并没有**定义这个字段，全仓库也没有用 `default_config` 实例化 `mini_page`。它是预留给「自定义 config」的钩子，本讲不做展开——别误以为 `default_config` 漏定义了什么。

> 另一处小细节：[config.cuh:48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L48) 的 `GMEM_SPIN_LOOP_SLEEP_NANOS` 被声明成 `bool` 却赋值 `20`，会被规约成 `true(1)`。从字段名看它本意是「全局内存自旋等待的睡眠纳秒数」，类型应为整型——这更像是一处待修正的类型笔误。本讲只提示存在，不在此深究。

#### 4.3.4 代码实践

**实践目标**：把「三个宽度」和 `instruction_state_t` 的实际字节大小对上，验证 config 公式确实是这个结构体的体积。

**操作步骤**（源码阅读 + 手算）：

1. 从 [util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19) 抄出 `instruction_state_t` 的成员，估算单份大小：
   - `instructions` = 32 int = 128 字节
   - `timings` = 128 int = 512 字节
   - `pid_order + padding` = `((13+31)&~31) = 32` 个 int = 128 字节
   - `semaphores` = 32 × 8 = 256 字节
   - `scratch` = 4096/4 = 1024 int = 4096 字节
   - 合计 ≈ 128 + 512 + 128 + 256 + 4096 = 5120 字节（未计 `__align__(128)` 的尾部填充）。
2. 对照 4.2.2 公式括号内的「每级」：`SCRATCH(4096) + (IW+TW)×4(640) + DSEM×8(256) = 4992`。
3. 解释差异：结构体里 `pid_order+padding`（128 字节）在公式中被「摊」进了那个 `512` 的常数底数（多份级数摊下来，512 足够覆盖这些零碎字段），公式追求的是「够用的上界」而非字节级精确。

**需要观察的现象**：结构体主项（指令 128 + 计时 512 + 信号量 256 + scratch 4096 = 4992）与公式括号内完全一致；`pid_order` 等小字段落在常数余量里。

**预期结果**：能说清「config 的 STATIC 公式 ≈ `STAGES × sizeof(instruction_state_t)` 的主项 + 余量」，从而理解改任何一个宽度都会同时改变指令格式和静态内存预算。**待本地验证**：用 `std::cout << sizeof(instruction_state_t<default_config>)` 在 host 端打印精确字节数（含对齐填充）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `INSTRUCTION_WIDTH` 恰好是 32，而不是 16 或 64？

**答案**：因为取指用「1 个 lane 搬 1 个 int」并行加载，一个 warp 正好 32 个 lane，`INSTRUCTION_WIDTH=32` 让一条指令能被一个 warp 在一步内取完（见 [instruction_fetch.cuh:24-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L24-L26)）。这也是 `static_assert(INSTRUCTION_WIDTH <= 32)` 的由来；若要 64，就得改成两步取指。

**练习 2**：`INSTRUCTION_PIPELINE_STAGES_BITS = 1` 是怎么算出来的？如果级数改成 4，它该是多少？

**答案**：它是表示「级数」所需的位数 = ⌈log₂(级数)⌉。级数=2 ⇒ 1 位；级数=4 ⇒ 2 位。改了之后 `page_finished[NUM_PAGES][BITS]` 的第二维、以及 `wait_page_ready`/`finish_page` 里对相位 bit 的循环（[util.cuh:156-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L156-L168)）都得相应调整——而且会先撞上 [semaphore_constructor.cuh:25](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L25) 那条「级数必须为 2」的断言。

**练习 3**：`TIMING_RECORD_ENABLED = false` 时，`timing_t timings[128]` 这块共享内存还会被分配吗？这有什么好处和坏处？

**答案**：会。`timings` 是 `instruction_state_t` 的成员，无论是否启用记录，结构体大小不变，共享内存照常占用 512 字节/级。好处是开关 `TIMING_RECORD_ENABLED` 不改变内存布局（`record()` 在 `false` 时编译成空操作，见 [util.cuh:190-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L190-L196)），`NUM_PAGES` 等推导不受影响；坏处是关掉计时也省不下这块内存。

---

## 5. 综合实践

把三个最小模块串起来，做一次「参数扰动 → 影响追踪」的桌面演练。

**场景**：假设你想给 consumer 更多临时计算空间，把 [config.cuh:33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L33) 的 `SCRATCH_BYTES` 从 4096 改成 12 288（每级多 8 KiB），其余参数不动，目标 GPU 仍是 H100。

**请按顺序回答并验证**：

1. **重算 STATIC**：每级 = `12 288 + 640 + 256 = 13 184`；静态 = `512 + 2×13 184 = 26 880` 字节。
2. **重算 DYNAMIC**：`233 472 − 26 880 = 206 592` 字节。
3. **重算 NUM_PAGES**：`206 592 / 16 384 = 12.6… → 12`。
4. **判断编译结果**：`NUM_PAGES` 从 13 变成 12，会立刻撞上 [config.cuh:44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L44) 的 `static_assert(NUM_PAGES == 13)`，**编译失败**，错误信息 `"NUM_PAGES must be 13"`。
5. **追因**：页数变了会撑破哪些假设？至少三处——[page_allocator.cuh:26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L26) 的 membermask、[util.cuh:142-148](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L142-L148) 的 `page_array_t` / `page_finished` 维度、以及 demo 里任何硬编码「13 页」假设的指令（如 attention 里把某些固定逻辑页映射到物理页的代码）。
6. **给出修复方向**：要么把 `static_assert` 同步改成 `== 12` 并审计所有依赖页数的代码；要么缩小 `SCRATCH_BYTES` 增量让 `NUM_PAGES` 维持 13；要么换用共享内存更大的 B200（但又会牵动 [semaphore_constructor.cuh:25](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L25) 等其它假设）。

这个练习把本讲的核心论点走了一遍：**`default_config` 是一张紧密耦合的参数表，改一个字段会通过编译期算式连锁影响 warp 数、共享内存划分、页数和寄存器预算，而 `static_assert` 就是那张防漂移的安全网。**

> **待本地验证**：在真实仓库上做第 1～3 步的 `constexpr` 手算后，可以临时把 `SCRATCH_BYTES` 改成 12 288、`make` 一次，观察 nvcc 是否确实在 [config.cuh:44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L44) 报错（做完记得还原，本讲禁止修改源码）。

## 6. 本讲小结

- `default_config` 是 VM 的**唯一参数表**，全部 `static constexpr`，编译期固定，由 [config.cuh:7-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L7-L52) 定义。
- **warp/线程**：`NUM_CONSUMER_WARPS=16` + 4 个服务 warp（loader/storer/launcher/controller）= `NUM_WARPS=20` = `NUM_THREADS=640`；consumer 每线程 104 寄存器、服务 warp 64 寄存器，合计 61 440 刚好放进单 SM 的 65 536。
- **共享内存**：`STATIC_SHARED_MEMORY = 512 + 2×(4096+640+256) = 10 496` 字节（≈ `STAGES × sizeof(instruction_state_t)` 主项 + 余量）；`DYNAMIC = MAX − STATIC = 222 976`（Hopper）。
- **页数**：`NUM_PAGES = DYNAMIC / 16 384 = 13`（整除），是扣完成本后的最大整页数；`static_assert(NUM_PAGES == 13)` 防止参数改动导致页数静默漂移。
- **指令/计时布局**：`INSTRUCTION_WIDTH=32`（一条指令一个 warp 一次取完）、`TIMING_WIDTH=128`、`DYNAMIC_SEMAPHORES=32`，三者共同决定 `instruction_state_t` 的体积，进而决定静态内存预算。
- **耦合性**：所有字段通过编译期算式互锁，`static_assert`（页数=13、≤32、信号量≤32、级数=2、指令宽≤32）是这套耦合的编译期守卫。

## 7. 下一步学习建议

- **下一讲（建议）**：进入 `instruction_state_t` / `state` 的运行时视角，看 `util.cuh` 里 VM 状态机如何用 config 定义的页、信号量、相位 bit 来做生产者-消费者同步（即 `wait_page_ready` / `finish_page` / `await_instruction` 那一组接口）。本讲讲清了「内存有多大」，下一讲该讲「这块内存怎么被动态使用」。
- **推荐继续阅读的源码**：
  - [include/util.cuh:73-212](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L73-L212) 的 `state<config>`——看 config 的每个字段如何成为 VM 状态的成员。
  - [include/megakernel.cuh:82-111](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L82-L111) 的信号量初始化——看 `NUM_PAGES`、`INSTRUCTION_PIPELINE_STAGES_BITS` 如何决定初始化循环的边界。
  - 若要做移植实验，对照 [config.cuh:44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L44) 和 [controller.cuh:21-22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L21-L22)，思考把目标 GPU 从 H100 换成 B200 时哪些断言会先触发。
