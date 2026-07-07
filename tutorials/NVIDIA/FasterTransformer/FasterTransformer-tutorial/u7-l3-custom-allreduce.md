# 自定义 all-reduce kernel

## 1. 本讲目标

在 u7-l1 中我们已经知道：张量并行（TP）下，一个 transformer block 在「注意力输出投影」与「FFN 降维」两处各要做一次 `ftNcclAllReduceSum`，二者都依赖 NCCL。本讲回答一个工程问题——**能不能绕开 NCCL，让这步 all-reduce 更快？**

学完本讲你应当能够：

- 说清 NCCL all-reduce 在「小张量、节点内 8 卡」场景下的延迟来源，以及自定义 kernel 为何能更快。
- 读懂 `custom_ar_comm` 的通信通道抽象：它如何在节点内 8 张卡之间共享显存缓冲、用屏障做同步。
- 读懂 `custom_ar_kernels.cu` 中的 `oneShotAllReduceKernel` 与 `twoShotAllReduceKernel` 两个 reduce kernel 的差异与切换阈值。
- 掌握 `enable_custom_all_reduce` 这个配置开关从 INI 文件到 kernel 启动的完整链路，以及它在何种条件下会回退到 NCCL。

## 2. 前置知识

### 2.1 all-reduce 是什么

all-reduce 是一种集合通信原语：每个参与方（rank）一开始各持有一份同样形状的缓冲，通信结束后**每个 rank 都拿到所有 rank 缓冲的逐元素求和**。数学上，设共有 \(n\) 个 rank，第 \(i\) 个 rank 的缓冲为 \(B_i\)，则 all-reduce 后每个 rank 都得到：

\[
\mathrm{Result} = \sum_{i=0}^{n-1} B_i
\]

在张量并行里，行并行的 GEMM2 只算出了「部分和」（参见 u3-l4），必须 all-reduce 才能得到完整结果，所以 all-reduce 出现在每次前向的关键路径上。

### 2.2 NCCL 为何不是「零开销」

NCCL 是 NVIDIA 官方集合通信库，功能完备、跨节点可用，但它的实现是通用库：

- 它要兼容任意 rank 数、任意拓扑、任意数据量，因此内部要走 ring 或 tree 算法，并维护自己的协议栈与同步机制。
- 对「张量很小、只在单节点内 8 张卡之间通信」这种最常见的推理场景，NCCL 的启动延迟与算法本身的同步轮次反而成了主要开销——数据搬运本身可能只花几微秒，但库调度开销与之相当甚至更大。

FT 的自定义 all-reduce 思路是：**既然推理时 TP 几乎总是「单节点 8 卡」，干脆为这一种拓扑手写一个极简 kernel**，绕过 NCCL 的通用机制，用 GPU 之间的 P2P（peer-to-peer）直连显存访问 + 极轻量屏障，把延迟压到最低。

### 2.3 GPU 间 P2P 访问

DGX-A100 这类机器里，8 张 GPU 通过 NVSwitch 全互联，任意两张卡之间都能以 NVLink 带宽直接读写对方的显存，无需经过主机内存。CUDA 提供 `cudaDeviceCanAccessPeer` / `cudaDeviceEnablePeerAccess` 来开启这种「跨卡直接访存」能力。自定义 all-reduce 正是建立在 P2P 之上：**每张卡直接 load 其他卡的显存并求和，不再经由 NCCL 编排。**

### 2.4 release/acquire 内存序

多 GPU 通过 P2P 共享变量做同步时，必须保证「写屏障值」对其他卡可见后，其他卡才去读。CUDA 在 sm_70 及以上提供 `st.global.release.sys` / `ld.global.acquire.sys` 这种带 `.sys` 系统级内存序语义的内联汇编指令，保证跨设备的读写顺序。本讲的 reduce kernel 就用它来做屏障，这是理解同步正确性的关键。

> 名词提示：本讲频繁出现 rank、TP、all-reduce、P2P、NVSwitch、release/acquire、barrier（屏障）等术语，它们都承接 u7-l1，下面不再重复解释。

## 3. 本讲源码地图

本讲涉及的关键文件集中在 `utils/` 与 `kernels/` 两个目录，正好对应「通信通道」与「reduce kernel」两个最小模块：

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/utils/custom_ar_comm.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.h) | 定义抽象基类 `AbstractCustomComm`、模板实现 `CustomAllReduceComm<T>` 与工厂函数 `initCustomAllReduceComm`。 |
| [src/fastertransformer/utils/custom_ar_comm.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc) | 通信通道的实现：构造、P2P 开启、跨 rank 指针交换、缓冲交换、all-reduce 入口、初始化与回退逻辑。 |
| [src/fastertransformer/kernels/custom_ar_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.h) | 声明 reduce kernel 所需的常量宏、`AllReduceParams<T>` 参数结构、host 启动函数 `invokeOneOrTwoShotAllReduceKernel`。 |
| [src/fastertransformer/kernels/custom_ar_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu) | 真正的 reduce kernel：`oneShotAllReduceKernel`、`twoShotAllReduceKernel`、`kernelLaunchConfig` 以及启动分发。 |

此外会引用一处上层消费方代码，用于说明「NCCL vs 自定义」的切换条件：

- [src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc)：FFN 层末尾的 all-reduce，演示 `enable_custom_all_reduce_` 如何在运行期二选一。

## 4. 核心概念与源码讲解

### 4.1 通信通道抽象：AbstractCustomComm 与 CustomAllReduceComm

#### 4.1.1 概念说明

NCCL 的通信句柄（`ncclComm_t`）是一套通用、跨节点、有协议栈的对象。FT 想用一个**「可插拔的同构替代品」**来替换它，于是先抽出抽象基类 `AbstractCustomComm`，规定只要能提供「做一次 all-reduce、开启 P2P、交换缓冲」这几件事的对象，就可以替换 NCCL 通道。这样上层 layer（注意力层、FFN 层）的代码里就能用同一个指针 `custom_all_reduce_comm_`，到底走 NCCL 还是自定义 kernel，完全由这个指针是否为 `nullptr` 决定——这正是 u7-l1 提到的「通道可插拔」。

#### 4.1.2 核心流程

整个通信通道的生命周期是：

1. **初始化**：`initCustomAllReduceComm` 按 `enable_custom_all_reduce` 决定是否真的创建通道；若条件不满足则全部塞 `nullptr`（回退 NCCL）。
2. **建共享缓冲**：rank 0 调 `allocateAndExchangePeerAccessPointer`，为每张卡 `cudaMalloc` 一块通信缓冲与屏障数组，再开启 P2P，把所有指针「交换」给其它 rank，使每张卡都持有一张「指向 8 张卡缓冲」的指针表。
3. **每次 all-reduce**：调 `customAllReduce`，它把当前张量元素数写进参数、递增屏障标志、启动 reduce kernel。
4. **缓冲交换优化**：`swapInternalBuffer` 在 kernel 执行前把输出张量的指针与通信缓冲指针「对调」，让 kernel 直接把结果写进用户输出，省一次拷贝。

#### 4.1.3 源码精读

抽象基类只有四个纯虚函数，定义了「通道」必须能做的事：

[custom_ar_comm.h:31-40](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.h#L31-L40) 定义 `AbstractCustomComm`：`customAllReduce`（做一次 all-reduce）、`enableP2P`（开启跨卡直连）、`swapInternalBuffer`（缓冲指针对调）、`allocateAndExchangePeerAccessPointer`（分配并交换各 rank 的指针）。

模板子类 `CustomAllReduceComm<T>` 持有的核心字段是 `AllReduceParams<T> param_`（下一节展开），外加指向用户输出张量的指针 `output_tensor_`、临时指针 `tmp_tensor_data_` 以及本 rank 的 `rank_size_` / `rank_`：

[custom_ar_comm.h:42-63](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.h#L42-L63) 是 `CustomAllReduceComm<T>` 的声明。

注意模板参数 `T` 并不是「FP16/FP32」本身，而是它的「打包位宽类型」——这是因为 reduce kernel 内部按 128 位（`uint4`）一次搬运多个元素来吃满带宽。头文件用一个 traits 把真实数据类型映射成打包位宽：

[custom_ar_comm.h:70-85](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.h#L70-L85) 的 `CustomARCommTypeConverter` 默认把 `T` 映射成 `uint32_t`（对应 FP32），特化把 `half` 映射成 `uint16_t`，BF16 维持 `__nv_bfloat16`。上层 `createCustomComms` 正是用这个 converter 选定实例化类型（见 4.4）。

构造函数非常轻量，只记下 rank 信息，屏障标志初始化为 0，并**假设 all-reduce 发生在单节点内（DGX A100）**：

[custom_ar_comm.cc:21-29](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc#L21-L29) 把 `rank`、`local_rank`、`node_id` 都设成同一个值（`node_id=0`），这是「只在节点内通信」假设的直接体现。

真正的 all-reduce 入口函数把元素数与屏障标志塞进 `param_`，启动 kernel，最后把输出张量的数据指针「换回」原始位置：

[custom_ar_comm.cc:45-55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc#L45-L55) 是 `customAllReduce`：`barrier_flag` 每次调用通过 `FLAG(...)` 宏递增，用于让其它 rank 识别「这是新的一次 all-reduce」。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读」方式确认「通道可插拔」的设计意图，理解 `AbstractCustomComm` 为何能让 NCCL 与自定义 kernel 二选一。

**操作步骤**：

1. 打开 `custom_ar_comm.h`，观察 `AbstractCustomComm` 的四个纯虚函数签名。
2. 用 `Grep` 在 `src/fastertransformer/layers/` 下搜索 `custom_all_reduce_comm_`，看哪些层持有这个指针（你会看到注意力层与各 FFN 变体层都有）。
3. 对照 u3-l4 里的 `TensorParallelGeluFfnLayer`，找到它在 forward 末尾如何用这个指针。

**需要观察的现象**：layer 里通常写成 `if (custom_all_reduce_comm_ != nullptr) { 自定义路径 } else { ftNcclAllReduceSum }` 的形式——也就是说，**指针的有无就是开关**，没有第二个布尔变量参与选择 kernel。

**预期结果**：确认通道是「同构替换」——上层 layer 完全不感知底层是 NCCL 还是自定义 kernel，只在构造期决定指针是否为空。具体切换代码在 4.4 节精读。

#### 4.1.5 小练习与答案

**练习 1**：`AbstractCustomComm` 为什么要用纯虚基类而不是直接让所有层直接持有 `CustomAllReduceComm<T>*`？

> **答案**：因为 `CustomAllReduceComm` 是模板类，不同 `T`（`uint16_t`/`uint32_t`/`__nv_bfloat16`）是不同类型，而上层 layer 本身也是模板、不知道该用哪种 `T` 的通道。抽象基类是非模板的，可以统一用 `std::shared_ptr<AbstractCustomComm>` 存放，把「类型擦除」和「可插拔」一次性解决。

**练习 2**：构造函数里 `node_id = 0` 这个硬编码意味着什么？

> **答案**：意味着该实现**不支持跨节点**的自定义 all-reduce，只在单节点（node_id 恒为 0）内通信；跨节点的 all-reduce 仍需 NCCL（或流水并行把不同层分到不同节点，见 u7-l2）。

### 4.2 P2P 显存与屏障：节点内 8 卡的共享缓冲

#### 4.2.1 概念说明

reduce kernel 要让一张卡读到其他 7 张卡的显存，前提是「每张卡都知道其他卡缓冲的地址」并且「P2P 已开启」。这部分「建一次、反复用」的准备工作放在 `allocateAndExchangePeerAccessPointer` 与 `enableP2P` 里。同时，多卡并发执行 kernel 时必须同步，FT 用一个**每卡一份的屏障数组**（`peer_barrier_ptrs`）做「轻量级硬件同步」——它比 NCCL 的同步便宜得多。

#### 4.2.2 核心流程

建缓冲的过程由 rank 0 主导：

1. `enableP2P(8)`：对每对卡调用 `cudaDeviceCanAccessPeer` 断言「可互访」，再 `cudaDeviceEnablePeerAccess` 开启；代码里直接 `assert(peer_access_available)`，**不可互访就编译期/运行期失败**——这是「必须 DGX-A100 级全互联」的硬约束来源。
2. 对 8 张卡各 `cudaMalloc` 一块 `CUSTOM_AR_SIZE_THRESHOLD`（48 MB）通信缓冲，以及一块屏障数组（`rank_size * (MAX_ALL_REDUCE_BLOCKS+1)` 个 `uint32_t`）。
3. 把每张卡缓冲/屏障的指针**广播给所有 rank 的 `param_`**：执行完后，任意一个 rank 的 `param_.peer_comm_buffer_ptrs[i]` 都指向第 i 张卡的缓冲，`param_.peer_barrier_ptrs[i]` 指向第 i 张卡的屏障数组。这样 kernel 内部就能用一张「指针表」索引到全部 8 卡的缓冲。

`param_`（即 `AllReduceParams<T>`）就是把这些「运行期才能确定的地址」打包成一个 POD 结构，**按值传进 kernel**：

[custom_ar_kernels.h:45-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.h#L45-L56) 定义 `AllReduceParams<T>`：含 `peer_barrier_ptrs[RANKS_PER_NODE]`、`peer_comm_buffer_ptrs[RANKS_PER_NODE]`、本 rank 的 `local_output_buffer_ptr`，以及元素计数与 `barrier_flag`。

关键常量宏也在同一头文件：

[custom_ar_kernels.h:26-32](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.h#L26-L32) 定义 `CUSTOM_AR_SIZE_THRESHOLD=50331648`（48 MB 单卡通信缓冲上限）、`MAX_ALL_REDUCE_BLOCKS=24`、`RANKS_PER_NODE=8`、`FLAG(a)=a%0x146`。

#### 4.2.3 源码精读

`enableP2P` 用双层循环对每对卡断言并开启 P2P，注释明确点出依赖 NVSwitch：

[custom_ar_comm.cc:90-106](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc#L90-L106) `enableP2P`：注释 `// Custom AR Kernels need DGX A100 NVSWITCH connections` 与 `assert(peer_access_available)` 共同构成硬件门槛。

`allocateAndExchangePeerAccessPointer` 只在 rank 0 执行（`assert(rank_ == 0)`），由它统一在 8 张卡上分配，再把指针写进其它 rank 的 `param_`：

[custom_ar_comm.cc:57-88](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc#L57-L88) 分配 8 份缓冲与屏障，并用内层 `for (j=1..)` 把同一组指针塞进 rank 1..7 的 `param_`，最后把每张卡的 `local_output_buffer_ptr` 默认指向自己的通信缓冲。

注意：这段分配发生在**主机的不同设备上下文**里（每次 `cudaSetDevice(i)` 后 `cudaMalloc`），依赖 P2P 让其它卡能直接访问。

`swapInternalBuffer` 是一个聪明的「零拷贝」优化。它的思路是：既然 reduce kernel 无论如何都要把结果写进 `local_output_buffer_ptr`，那不如**直接把用户的输出张量指针当成 `local_output_buffer_ptr`**，让 kernel 往用户张量里写，省掉「写通信缓冲再拷回输出」这一步。它先做尺寸校验（超过 48 MB 就放弃、回退 NCCL）：

[custom_ar_comm.cc:108-122](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc#L108-L122) `swapInternalBuffer`：保存原始输出指针到 `tmp_tensor_data_`，把张量数据指针临时换成自己的通信缓冲，`local_output_buffer_ptr` 设为原始输出指针。返回 `true` 表示「这次能用自定义 kernel」。

析构函数用 `cudaPointerGetAttributes` 判断缓冲是否仍是设备内存（`type == 2`），是才 `cudaFree`，避免重复释放：

[custom_ar_comm.cc:31-43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc#L31-L43) 析构按属性判断后释放本 rank 的缓冲与屏障。

#### 4.2.4 代码实践

**实践目标**：理解「指针表」如何让单卡 kernel 索引到 8 卡缓冲。

**操作步骤**：

1. 阅读 `AllReduceParams<T>` 的两个字段数组 `peer_comm_buffer_ptrs[RANKS_PER_NODE]` 与 `peer_barrier_ptrs[RANKS_PER_NODE]`。
2. 跟踪 `allocateAndExchangePeerAccessPointer` 中双层循环：外层 `i` 决定在「哪张卡」分配，内层 `j` 把该指针「写到第 j 个 rank 的 `param_` 的第 i 个槽位」。
3. 画一张图：横轴是 rank 0..7，纵轴是「该 rank 的 `param_` 指向的 8 个缓冲」。

**需要观察的现象**：执行完后，第 r 个 rank 的 `param_.peer_comm_buffer_ptrs[i]` 与第 r' 个 rank 的 `param_.peer_comm_buffer_ptrs[i]`（同一个 i）指向**同一块物理显存**（第 i 张卡的缓冲）。

**预期结果**：得到一张「所有 rank 共享同一组 8 块缓冲」的指针表图。这正是 kernel 内部 `for (ii=0..7) vals[ii] = src_d[ii][offset]` 能一次读全 8 卡数据的基础。**待本地验证**：若你在 DGX-A100 上实际跑，可以用 `cudaPointerGetAttributes` 打印各 `peer_comm_buffer_ptrs[i]` 所属 device 验证它们确实分属 8 张卡。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `allocateAndExchangePeerAccessPointer` 用 `assert(rank_ == 0)`，即只让 rank 0 分配？

> **答案**：因为 8 张卡的 P2P 缓冲地址必须**全局一致**（每张卡都要知道全部 8 个地址）。若每张卡各自分配，地址无法互通；由 rank 0 在 8 个设备上统一分配，再把同一组指针广播给所有 rank，才能保证「第 i 个槽位」对所有 rank 指向同一块显存。

**练习 2**：`swapInternalBuffer` 里 `elts * sizeof(T) <= CUSTOM_AR_SIZE_THRESHOLD` 这个判断失败会发生什么？

> **答案**：返回 `false`，调用方据此把 `use_custom_all_reduce_kernel` 置为 `false`，于是这次 all-reduce 回退到 `ftNcclAllReduceSum`（NCCL）。也就是说「张量太大」是触发 NCCL 回退的运行期条件之一。

### 4.3 reduce kernel 精读：one-shot 与 two-shot

#### 4.3.1 概念说明

有了共享缓冲与屏障，剩下就是「把 8 卡缓冲求和写到每卡输出」这个计算本身。数据量不同时，最优策略不同：

- **数据量小**（≤ 384 KB）：用 **one-shot**。每个 block 都把 8 张卡**全部数据**读一遍、求和、写回。简单粗暴，一次往返完成，延迟最低。
- **数据量大**（> 384 KB）：one-shot 会让每个 block 读写过量数据，反而慢。改用 **two-shot**，即经典的「reduce-scatter + all-gather」两阶段：先把数据切成 8 段，每个 rank 只对**自己的那一段**算 8 卡求和（reduce-scatter），再把各段部分结果散播给所有 rank（all-gather）。

切换阈值由宏 `DEFALUT_ALGO_AR_SIZE_THRESHOLD = 393216`（384 KB，注意源码里 `DEFALUT` 是既有拼写）控制。

#### 4.3.2 核心流程

两个 kernel 都遵循同样的骨架：

1. **屏障同步**：block 0 的前若干线程把本次 `barrier_flag` 写到其它 rank 的屏障数组；所有线程忙等（busy-wait）直到看到所有 rank 都写入了同一个 flag，表示「大家都到齐了，缓冲里的数据可读」。
2. **按 128 位打包读写**：用 `uint4`（FP16 时 8 个元素、FP32 时 4 个元素）一次 `LDG.128` 读，用 `add128b` 做 SIMD 求和（FP16 走 `add.f16x2`、FP32 走 `add.f32`），最大化带宽利用率。
3. **求和写回**：one-shot 直接写到本 rank 输出；two-shot 先 reduce-scatter 写回通信缓冲，再做一次跨 block 屏障，最后 all-gather 到输出。

one-shot 与 two-shot 的区别用伪代码概括：

```
# one-shot：每个 block 处理整段数据的一段，但每段都读全部 8 卡
for offset in 本block负责的段:
    sum = buffer[0][offset] + buffer[1][offset] + ... + buffer[7][offset]
    output[offset] = sum

# two-shot：数据切成 8 段，每 rank 只彻底算一段，再互相拷
# 阶段1 reduce-scatter
for offset in 本rank负责的那一段:
    sum = Σ buffer[i][offset];  comm_buffer_self[offset] = sum
# 阶段2 all-gather
for offset in 其它段:
    output[offset] = 对应rank已经算好的 comm_buffer[offset]
```

#### 4.3.3 源码精读

**屏障原语**：跨设备同步靠两条内联汇编，sm_70+ 用 `.release.sys`/`.acquire.sys` 保证系统级（跨 GPU）内存序，老架构退化为 `volatile` + `__threadfence_system`：

[custom_ar_kernels.cu:42-61](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu#L42-L61) `st_flag_release` / `ld_flag_acquire`。

**128 位打包求和**：`add128b` 用特化把一个 `uint4`（128 位）拆成 4 个 32 位通道，每通道用 `hadd2`（两个 FP16 并行加）或 `fadd`（一个 FP32 加），从而一次处理 8 个 FP16 或 4 个 FP32：

[custom_ar_kernels.cu:82-115](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu#L82-L115) `add128b` 的 FP16/FP32/BF16 三种特化。

**one-shot kernel**：先屏障（前 `RANKS_PER_NODE` 个线程参与，block 0 负责通知，全部线程忙等），再 round-robin 从 8 卡缓冲读、求和、写本 rank 输出：

[custom_ar_kernels.cu:138-199](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu#L138-L199) `oneShotAllReduceKernel`。注意它处理的是 `elts_per_rank == elts_total`（即整段数据），每个 block 处理其中一片；忙等条件是 `barrier_d[tidx] < params.barrier_flag`。

**two-shot kernel**：多了一个 `rank_offset`，每个 rank 只处理自己那 `1/8` 段；先求和写回**通信缓冲 `src_d[0]`**（reduce-scatter），再用 `st_flag_release`/`ld_flag_acquire` 做一次跨 block 屏障（这是 one-shot 没有的第二步同步），最后把各段结果 round-robin 拷到本 rank 输出：

[custom_ar_kernels.cu:201-298](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu#L201-L298) `twoShotAllReduceKernel`。其中 [L268-L297](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu#L268-L297) 是「再同步 + all-gather」段。

**启动配置与分发**：`kernelLaunchConfig` 根据元素数与数据位宽算 `blocks_per_grid` / `threads_per_block`，并用 `MAX_ALL_REDUCE_BLOCKS=24` 封顶（屏障数组大小就是按这个上限预分配的）；`invokeOneOrTwoShotAllReduceKernel` 用尺寸阈值选算法：

[custom_ar_kernels.cu:302-362](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu#L302-L362) `kernelLaunchConfig`：`elts_per_thread = 16/data_type_bytes`（FP16 算 8、FP32 算 4），把元素数折算成线程数与 block 数，超过 24 个 block 时找因子缩小。

[custom_ar_kernels.cu:366-389](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/custom_ar_kernels.cu#L366-L389) `invokeOneOrTwoShotAllReduceKernel`：`elts_total * sizeof(T) <= DEFALUT_ALGO_AR_SIZE_THRESHOLD` 走 one-shot（`kernel_algo=0`），否则 two-shot（`kernel_algo=1`）。

#### 4.3.4 代码实践

**实践目标**：通过阅读 kernel，量化「one-shot 适合小张量、two-shot 适合大张量」这一选择的临界点。

**操作步骤**：

1. 在 `custom_ar_kernels.cu` 找到 `DEFALUT_ALGO_AR_SIZE_THRESHOLD = 393216`。
2. 设 TP=8、FP16，FFN 输出张量为 `[token_num, hidden_units]`，计算 `elts_total = token_num * hidden_units`，再算 `elts_total * sizeof(half)` 字节数。
3. 取 `megatron_345M` 的 `inter_size=4096`（即 `hidden_units=1024` 附近）与若干 `token_num`，判断它落在 one-shot 还是 two-shot 区间。
4. 阅读两个 kernel 的最内层循环，数一数：one-shot 每个 block 读 8 遍写 1 遍；two-shot 阶段 1 每个 block 只处理 1/8 数据，阶段 2 只做拷贝。

**需要观察的现象**：当 `token_num` 很小（如 batch=1、seq 短）时，张量远小于 384 KB，走 one-shot，整个 all-reduce 几乎只有「一次屏障 + 一次 8 卡读 + 一次写」，对应极低延迟；当张量增大越过阈值，自动切到 two-shot，避免 one-shot 下 block 读写过量。

**预期结果**：你会得到一张「token_num → 字节数 → 选中算法」的小表，直观看到临界点。结论是：**自定义 all-reduce 在小张量（典型于单 token 解码步、小 batch）下相对 NCCL 的延迟收益最显著**，因为 NCCL 的固定调度开销在小张量时占比极高。**待本地验证**：在 DGX-A100 上对同一张量分别用 `enable_custom_all_reduce=0/1` 跑 GPT 解码，用 Nsight Systems 量两次 all-reduce 的耗时差。

#### 4.3.5 小练习与答案

**练习 1**：one-shot kernel 里 `barrier_d[tidx] < params.barrier_flag` 用的是 `<` 比较，而 two-shot 里 `rank_barrier != params.barrier_flag` 用的是 `!=`。这两种写法都能正确同步吗？

> **答案**：都能在「flag 单调递增、且不会频繁回绕到仍在等待的旧值」的前提下工作。`FLAG(a) = a % 0x146`（mod 326）会让 flag 周期性回绕；one-shot 用 `<` 在大多数递增区间成立，two-shot 用 `!=` 配合 release/acquire 语义做精确匹配。这是两段代码风格不同的历史选择，理解时关注「它们都在等所有 rank 写入同一 flag」这一共性即可，不必纠结回绕边界的极端情形。

**练习 2**：`MAX_ALL_REDUCE_BLOCKS` 为什么是 24，而且 `kernelLaunchConfig` 要把 block 数强行压到 ≤ 24？

> **答案**：屏障数组大小 `rank_size * (MAX_ALL_REDUCE_BLOCKS + 1)` 是在初始化时一次性 `cudaMalloc` 的（见 4.2 的分配代码）。two-shot 的第二步同步按 block 索引 `bidx` 写屏障（`flag_block_offset = RANKS_PER_NODE + bidx*RANKS_PER_NODE`），因此 block 数不能超过预分配上限 24，否则会越界写屏障。`kernelLaunchConfig` 里的 `iter_factor` 循环就是为此封顶。

### 4.4 启用条件与 NCCL 回退：enable_custom_all_reduce 全链路

#### 4.4.1 概念说明

自定义 all-reduce 不是「开了就一定用」。它有**编译期、初始化期、运行期**三层条件，任何一层不满足都会回退到 NCCL。理解这三层条件，是本讲实践任务（说明何时能用、何时回退）的核心。

#### 4.4.2 核心流程

三层条件如下：

1. **编译期**：需要 CUDA Runtime ≥ 11.2（`CUDART_VERSION >= 11020`），因为依赖 `cudaMallocAsync` 异步内存池；否则打 warning 并全塞 `nullptr`。
2. **初始化期**：`rank_size` 必须恰好等于 `RANKS_PER_NODE = 8`（DGX-A100 单节点 8 卡）；否则同样打 warning（`BUILD_MULTI_GPU` 开启时）或直接 `FT_CHECK` 失败（未开 `BUILD_MULTI_GPU` 时），并塞 `nullptr`。
3. **运行期**：单次 all-reduce 的张量字节数必须 ≤ `CUSTOM_AR_SIZE_THRESHOLD`（48 MB）；否则 `swapInternalBuffer` 返回 `false`，本次回退 NCCL。

只要这三层全过，layer 才会调 `customAllReduce`；否则走 `ftNcclAllReduceSum`。

#### 4.4.3 源码精读

初始化函数 `initCustomAllReduceComm` 把上面前两层条件写成显式分支，回退路径统一为「push `nullptr`」：

[custom_ar_comm.cc:124-166](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/custom_ar_comm.cc#L124-L166) `initCustomAllReduceComm`：先判 `custom_all_reduce_comms == 0`（指针为空表示用户压根没开）；再判 `rank_size != RANKS_PER_NODE`（必须 8 卡）；最后判 `CUDART_VERSION >= 11020`。三个分支任一不满足都 `push_back(nullptr)`。

> 这段代码的 warning 文案 "Custom All Reduce only supports 8 Ranks currently" 与 README 的限制 1 完全对应。

运行期切换在 layer 里实现。以 FFN 层为例，它先尝试 `swapInternalBuffer`，根据返回值二选一：

[TensorParallelGeluFfnLayer.cc:44-65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L44-L65) `if (enable_custom_all_reduce_ && custom_all_reduce_comm_ != nullptr)` 决定是否换缓冲；真正 all-reduce 时 `if (!use_custom_all_reduce_kernel)` 走 NCCL，`else` 走 `customAllReduce`。这套「指针非空 + 尺寸达标」的双闸门就是 u7-l1 提到的 `enable_custom_all_reduce_` / `do_all_reduce_` 两个开关的落点。

最后看配置如何流入：Triton 部署时从 INI 读 `enable_custom_all_reduce`（默认 0），透传到模型构造，再由 `createCustomComms` 调 `initCustomAllReduceComm`：

[ParallelGptTritonModel.cc:115](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L115) `reader.GetInteger("ft_instance_hyperparameter", "enable_custom_all_reduce", 0)` 读取开关（FP16 分支；BF16/FP32 分支同形）。

[ParallelGptTritonModel.cc:449-454](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L449-L454) `createCustomComms` 用 `CustomARCommTypeConverter<T>::Type` 选定实例化类型，再调 `initCustomAllReduceComm`。

而 INI 文件里默认是关闭的：

[gpt_config.ini:15](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini#L15) `enable_custom_all_reduce=0`，默认回退 NCCL。

#### 4.4.4 代码实践

**实践目标**：完成本讲规定的实践任务——说清 custom all-reduce 相比 NCCL 在哪些场景下能获得更低延迟，并列出 README 的两条限制。

**操作步骤**：

1. 打开 [README.md:290-293](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L290-L293)，逐字摘抄「Support custom all reduce kernel」下的两条 Limitation。
2. 结合本讲 4.1–4.4，写一段话说明「低延迟场景」需要同时满足：拓扑 = 单节点全互联（DGX-A100 / NVSwitch）、`tensor_para_size = 8`、张量较小（小 batch 或单 token 解码步）。
3. 对照 `initCustomAllReduceComm` 的三个回退分支，给每个「不适合的场景」标注它会在哪一层被挡下、回退到 NCCL。

**需要观察的现象 / 预期结果**：你应该得到如下结论。

README 列出的两条限制（原文）：

1. **Only support tensor parallel size = 8 on DGX-A100.**（只支持 DGX-A100 上 TP=8）
2. **Only support CUDA with cudaMallocAsync.**（只支持带 `cudaMallocAsync` 的 CUDA，即 ≥ 11.2）

custom all-reduce 比 NCCL 延迟更低的场景：

- **拓扑**：必须是 DGX-A100 这类「8 卡全 NVSwitch 互联」的节点，任意两卡 P2P 直连可用（`enableP2P` 里 `assert(peer_access_available)` 才不爆）。跨节点或非全互联拓扑不适用。
- **tensor_para_size**：必须恰好等于 8（`rank_size == RANKS_PER_NODE`）。
- **数据规模**：张量越小收益越明显——典型如 GPT 自回归解码的每一步（`beam_width × hidden_units`，远小于 384 KB），此时 NCCL 的固定调度开销占比极高，自定义 kernel 的「一次屏障 + 一次 P2P 读 + 一次写」能显著省时。张量超过 48 MB 会运行期回退 NCCL。

不适合（自动回退 NCCL）的场景与对应挡板：

| 场景 | 被挡下的位置 | 结果 |
| --- | --- | --- |
| `tensor_para_size != 8`（如 2/4 卡） | `initCustomAllReduceComm` 的 `rank_size != RANKS_PER_NODE` 分支 | push `nullptr`，走 NCCL |
| CUDA < 11.2（无 `cudaMallocAsync`） | `initCustomAllReduceComm` 的 `CUDART_VERSION` 分支 | warning + push `nullptr`，走 NCCL |
| 非 NVSwitch 全互联（P2P 不可达） | `enableP2P` 的 `assert(peer_access_available)` | 直接失败 |
| 单次张量 > 48 MB | `swapInternalBuffer` 返回 `false` | 本次走 NCCL |
| 用户没开（INI `enable_custom_all_reduce=0`） | `initCustomAllReduceComm` 入口指针判空 | 走 NCCL |

> 说明：以上场景结论基于源码逻辑推导，**实际延迟数值待本地验证**——建议在 DGX-A100 上对同一模型分别设 `enable_custom_all_reduce=0/1`，用 Nsight Systems 对比 all-reduce 阶段耗时。

#### 4.4.5 小练习与答案

**练习 1**：某用户在一张 4 卡（非 DGX-A100）机器上设 `enable_custom_all_reduce=1`，会发生什么？

> **答案**：`initCustomAllReduceComm` 检测到 `rank_size(4) != RANKS_PER_NODE(8)`，在 `BUILD_MULTI_GPU` 开启时打一条 `FT_LOG_WARNING("Custom All Reduce only supports 8 Ranks currently. Using NCCL as Comm.")`，然后 push `nullptr`。最终效果与 `enable_custom_all_reduce=0` 完全一致——静默回退 NCCL，不会崩溃。

**练习 2**：为什么 `enable_custom_all_reduce` 默认是 0（关闭）？

> **答案**：因为它有强硬件依赖（必须 DGX-A100、TP=8、CUDA ≥ 11.2），在大多数部署环境都不满足。默认关闭可保证「开箱即用」永远走通用的 NCCL；只有在确认硬件满足且追求极致小张量延迟时，用户才显式打开。

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端追踪 + 配置实验设计」。

**任务**：假设你要在 DGX-A100（8 × A100，NVSwitch 全互联）上部署 `megatron_345M`（FP16，`hidden_units=1024`，TP=8），并希望让解码阶段每一步的 all-reduce 走自定义 kernel。

请完成：

1. **配置改动**：写出需要修改 `gpt_config.ini` 的那一行（把哪个键改成什么值），并说明 `tensor_para_size` 必须同步改成多少。参考 [gpt_config.ini:10-15](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini#L10-L15)。
2. **链路追踪**：从 INI 的 `enable_custom_all_reduce` 出发，按顺序列出这个值经过哪些函数/对象，最终到达 `invokeOneOrTwoShotAllReduceKernel`。参考 4.4.3 的三个链接。
3. **算法判断**：解码阶段单步 all-reduce 的张量是 `[beam_width × token_num_step, hidden_units]`，其中 `token_num_step` 通常为 1。取 `beam_width=1`、`hidden_units=1024`，判断它走 one-shot 还是 two-shot，并说明理由（参考 4.3 的阈值）。
4. **回退分析**：若把模型换成 `gpt_175B`（`hidden_units=12288`）、batch 调大到 64，单步 all-reduce 张量字节数会变成多少？是否仍能用自定义 kernel？参考 4.2.3 的 `swapInternalBuffer` 尺寸上限。

**参考答案要点**：

1. 把 `enable_custom_all_reduce=0` 改为 `enable_custom_all_reduce=1`，并把 `tensor_para_size=1` 改为 `tensor_para_size=8`（必须等于 8）。
2. 链路：`gpt_config.ini` → `ParallelGptTritonModel::createModelInstance`（`reader.GetInteger(..., "enable_custom_all_reduce", 0)`）→ 存入 `ParallelGptTritonModel::enable_custom_all_reduce_` → `createCustomComms` → `initCustomAllReduceComm<commDataType>`（按 `enable_custom_all_reduce_` 与 `rank_size==8`/`CUDART_VERSION` 决定是否创建）→ 各层持有 `custom_all_reduce_comm_` → forward 中 `customAllReduce` → `invokeOneOrTwoShotAllReduceKernel`。
3. `beam_width=1, token_num_step=1, hidden_units=1024` ⇒ `elts_total = 1024`，字节数 `1024 × 2 = 2048` 字节 ≪ 384 KB ⇒ **one-shot**。这正是自定义 all-reduce 收益最大的区间。
4. `gpt_175B`：`hidden_units=12288`，batch 64 ⇒ `elts_total = 64 × 12288 = 786432`，字节数 `786432 × 2 = 1572864` 字节 ≈ 1.5 MB，仍 ≤ 48 MB ⇒ 仍可用自定义 kernel，但已 > 384 KB ⇒ 走 **two-shot**。若继续放大到超过 48 MB，则 `swapInternalBuffer` 返回 `false`，本次回退 NCCL。

> 以上数值均为源码逻辑推导，**实际部署请在 DGX-A100 上验证**，并用 Nsight Systems 确认 all-reduce 实际走向与耗时。

## 6. 本讲小结

- FT 用一个非模板抽象基类 `AbstractCustomComm` 把「通信通道」做成可插拔：上层 layer 只看 `custom_all_reduce_comm_` 是否为空，即可在 NCCL 与自定义 kernel 间二选一。
- 自定义 all-reduce 的物理基础是 DGX-A100 的 NVSwitch 全互联 + P2P 直连：rank 0 一次性在 8 卡上分配共享缓冲与屏障，把指针表交换给所有 rank，kernel 内部用一张指针表索引 8 卡显存。
- reduce kernel 分两档：张量 ≤ 384 KB 走 `oneShotAllReduceKernel`（每 block 读全 8 卡一次写回），更大走 `twoShotAllReduceKernel`（reduce-scatter + all-gather 两阶段，多一次跨 block 屏障）；二者都用 `uint4` 128 位打包 + `add.f16x2`/`add.f32` SIMD 求和吃满带宽，用 `.release.sys`/`.acquire.sys` 做跨设备内存序。
- 启用有三层闸门：编译期 `CUDART_VERSION >= 11020`、初始化期 `rank_size == 8`、运行期单次张量 ≤ 48 MB；任一不满足即回退 NCCL（多数情况下「静默」回退）。
- 它的低延迟优势集中在「单节点 8 卡 + 小张量」（典型为自回归解码每一步）这种 NCCL 固定调度开销占比极高的场景；README 明确两条限制：仅 TP=8 on DGX-A100、仅支持带 `cudaMallocAsync` 的 CUDA。

## 7. 下一步学习建议

- **回到调用方验证**：带着本讲对 `custom_all_reduce_comm_` 的理解，重读 u7-l1 中注意力层与 FFN 层的两处 all-reduce，确认「一个 TP block 恰好两次 all-reduce」在自定义通道下同样成立。
- **横向对比通信方案**：本讲（P2P 自定义 all-reduce）、u7-l1（NCCL 张量并行）、u7-l2（MPI + NCCL 流水并行）三者构成 FT 的完整通信版图。建议画一张总图，标注每种方案适用的拓扑与粒度。
- **深入内存池**：本讲反复提到的 `cudaMallocAsync`（CUDA 11.2 异步内存池）在 u2-l2 已有铺垫，可结合 `CUDA_MEMORY_POOL_DISABLED` 宏对照阅读，理解「为什么自定义 all-reduce 强依赖内存池」。
- **延伸阅读**：若想了解这套「P2P + 屏障」思路在更现代推理框架里的演进，可对比阅读 TensorRT-LLM 的 custom all-reduce 实现（FT 的官方继任者），体会同一思想在不同代码组织下的复用。
