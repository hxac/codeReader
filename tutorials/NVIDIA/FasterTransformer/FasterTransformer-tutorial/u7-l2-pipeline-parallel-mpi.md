# 流水并行与多节点：MPI 组织

## 1. 本讲目标

在 [u7-l1 张量并行](u7-l1-tensor-parallel.md) 中，我们把单个 transformer block 的权重按 head 切分到多张卡上，让一个 block「横向」铺开。本讲解决另一个正交维度：当模型**层数太多、一张卡放不下**时，如何把不同的层「纵向」分配到不同 GPU，以及如何把推理过程组织成跨多台机器的多进程。

学完本讲你应该能够：

- 理解 MPI 在 FasterTransformer（下称 FT）里的角色：它只是「多进程组织者」，真正搬数据的是 NCCL。
- 看懂 `mpi::initialize` / `mpi::getCommWorldRank` 这组薄封装，以及它们如何配合 `mpi::barrier` / `mpi::bcast` 协调多进程。
- 说清 `world_size = tensor_para_size × pipeline_para_size` 这条硬约束，以及 `ftNcclInitialize` 如何用一个**二维笛卡尔拓扑**把全局 rank 自动拆成 `(tp_rank, pp_rank)`。
- 掌握 `pipeline_para` 如何把 `decoder_layers` 按 `pipeline_para_rank` 均分成连续段，每段落到一个 pipeline 阶段。
- 写出在 2 节点 × 4 GPU 上跑 TP=4 PP=2 的 `mpirun` 启动命令，并解释为什么 TP 宜走节点内 NVLink、PP 宜走节点间 Infiniband。

---

## 2. 前置知识

本讲是专家层内容，假设你已读过：

- **u1-l4 / u5-l2**：知道 FT 的多 GPU 示例用 `mpirun` 启动、用 INI 配置描述模型。
- **u6-l1**：知道 `ParallelGpt` 的 context/decoder 两阶段、以及 KV cache 形状里有 `num_layer / pipeline_para_size` 这一维。
- **u7-l1**：知道 `NcclParam`（`rank_`/`world_size_`/`nccl_comm_`）与 `ftNcclAllReduceSum` 等通信原语，知道一个 TP transformer block 恰好两次 all-reduce。

下面补充三个本讲要用到、但前面讲义没展开的概念：

| 术语 | 通俗解释 |
| --- | --- |
| **MPI**（Message Passing Interface）| 一套「多进程通信」标准库。FT 用它来**启动 N 个进程**并给每个进程一个全局编号 `rank`，进程总数叫 `world_size`。MPI 本身也能传消息，但 FT 只用它做组织与少量控制流广播，重活交给 NCCL。 |
| **NCCL**（NVIDIA Collective Communications Library）| NVIDIA 专为 GPU↔GPU 集合通信设计的库（all-reduce / all-gather / broadcast）。它直接走 NVLink/Infiniband，是 FT 张量并行与流水并行真正传张量的通道。 |
| **Pipeline Parallel（PP，流水并行）**| 把模型的**不同层**放在不同 GPU 上，推理时像流水线一样逐阶段传递中间激活。与张量并行（TP，同一层切到多卡）正交。 |

一个直观比喻：TP 是「一道工序由 4 个工人合力同时做」，PP 是「8 道工序排成一条流水线，每个工人负责一段」。FT 允许两者**同时**使用：`world_size = TP × PP`。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/utils/mpi_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/mpi_utils.h) | MPI 的 C++ 薄封装声明：`initialize`/`finalize`/`getCommWorldRank`/`getCommWorldSize`/`barrier`/`bcast`，全部受 `BUILD_MULTI_GPU` 宏守卫。 |
| [src/fastertransformer/utils/mpi_utils.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/mpi_utils.cc) | 上述函数的实现，每个函数体就是一个 `MPI_xxx` 调用加错误检查。 |
| [examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc) | GPT 多 GPU C++ 示例入口。`main` 里调用 `mpi::initialize`、读 INI、调用模型、最后 `mpi::finalize`。 |
| [examples/cpp/multi_gpu_gpt/gpt_example_utils.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc) | 本讲真正的「大脑」：`init_multiprocessing`（校验 TP×PP、算 `layers_per_group`）、`init_cuda_ctx`（把 rank 绑到 GPU）、`init_nccl`（调用 `ftNcclInitialize`）。 |
| [src/fastertransformer/utils/nccl_utils.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc) | `ftNcclInitialize` 在此：用 `MPI_Cart_create` 建二维拓扑、`MPI_Cart_sub` 拆出 TP/PP 两个子通信器、再为每个子通信器建独立 NCCL communicator。 |
| [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) | 官方多 GPU / 多节点运行指南，给出 `mpirun -n 8` 与 slurm 双节点示例。 |

> 提示：`mpi_utils` 只是「组织者」。本讲最关键的代码不在 `mpi_utils.cc` 里，而在 `ftNcclInitialize` 中那段 `MPI_Cart_create` / `MPI_Cart_sub`——它才是把一维全局 rank 折叠成二维 `(tp_rank, pp_rank)` 的地方。

---

## 4. 核心概念与源码讲解

### 4.1 MPI 基础与 mpi_utils 薄封装

#### 4.1.1 概念说明

FT 的多 GPU 推理是「**一个 GPU 一个进程**」的 SPMD（单程序多数据）模型：用 `mpirun -n 8` 启动 8 份相同的 `multi_gpu_gpt_example` 可执行文件，每份拿到一个不同的全局编号 `rank`（0~7），各自绑定一张 GPU，跑同一段代码但处理自己那份数据/权重。

要让这 8 个进程协同，需要一个「发号施令」的基础设施——这就是 MPI。但 FT 团队不想让业务代码直接裸调 `MPI_Init`、`MPI_Comm_rank`，于是包了一层 `mpi` 命名空间，把	MPI 的 C 接口换成更干净的 C++ 函数。

关键设计：整个 `mpi_utils` 都被 `#ifdef BUILD_MULTI_GPU` 守卫。**没开多 GPU 编译时，这些函数全是空壳**（`getCommWorldRank` 直接返回 0），让同一份模型代码能在单卡环境无缝运行——这是 FT 一贯的「条件编译降级」套路（参见 u1-l2）。

#### 4.1.2 核心流程

一个多 GPU 示例进程的 MPI 生命周期：

```text
mpi::initialize(&argc, &argv)        # 1. 启动 MPI，本进程获得全局 rank
        │
        ▼
rank = mpi::getCommWorldRank()       # 2. 查自己的编号
world_size = mpi::getCommWorldSize() # 3. 查总进程数
        │
        ▼
…… 模型构建、forward ……              # 4. 各 rank 各司其职
mpi::bcast(...)  # 偶尔广播控制信息（如随机种子）
mpi::barrier()   # 关键点同步（warmup / 计时前后）
        │
        ▼
mpi::finalize()                      # 5. 退出 MPI
```

注意：`mpi::barrier` 是**进程级同步**，不涉及 GPU 流。所以示例里每次 `mpi::barrier()` 之前都先 `cudaDeviceSynchronize()`，确保 GPU 真的算完了再让进程在 MPI 层面等齐。

#### 4.1.3 源码精读

**声明层**——`mpi` 命名空间提供的全部能力（[mpi_utils.h:81-91](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/mpi_utils.h#L81-L91)）：

```cpp
void initialize(int* argc, char*** argv);
void initThread(int* argc, char*** argv, MpiThreadSupport required, int* provided);
void finalize();
bool isInitialized();
void barrier(MpiComm comm);
void barrier();

int getCommWorldRank();
int getCommWorldSize();

void bcast(void* buffer, size_t size, MpiType dtype, int root, MpiComm comm);
```

接口非常薄：初始化、查 rank/size、barrier 同步、bcast 广播。**没有任何发送/接收大张量的点对点接口**——这印证了「MPI 只做组织，重活归 NCCL」。

**实现层**以 `getCommWorldRank` 为例（[mpi_utils.cc:82-89](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/mpi_utils.cc#L82-L89)）：

```cpp
int getCommWorldRank()
{
    int rank = 0;
#ifdef BUILD_MULTI_GPU
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
#endif
    return rank;
}
```

注意默认值 `rank = 0`：没开 `BUILD_MULTI_GPU` 时，函数直接返回 0，单卡代码不会因为缺 MPI 而崩。`initialize`（[mpi_utils.cc:37-42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/mpi_utils.cc#L37-L42)）同理，宏关掉时是空函数：

```cpp
void initialize(int* argc, char*** argv)
{
#ifdef BUILD_MULTI_GPU
    MPICHECK(MPI_Init(argc, argv));
#endif
}
```

其中 `MPICHECK` 宏（[mpi_utils.h:30-37](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/mpi_utils.h#L30-L37)）在 MPI 调用失败时打印文件行号并 `exit`，是 FT 错误处理的标准姿势。

**示例中的真实调用链**（[multi_gpu_gpt_example.cc:37-82](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L37-L82)）：`main` 第一行就是 `mpi::initialize`，最后一行（return 前）是 `mpi::finalize`，中间按 INI 里的 `data_type` 用 if/else 分发到模板实例 `multi_gpu_gpt_example<T>`——这正是 u1-l4 讲过的「枚举→模板」dispatch。

在 `multi_gpu_gpt_example<T>` 内部，`mpi::bcast` 用来把 rank 0 生成的随机种子广播给所有进程（[multi_gpu_gpt_example.cc:149-151](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L149-L151)），保证各 rank 采样行为一致；`mpi::barrier` 出现在 warmup 与计时前后（行 233、245、255、268），保证所有进程步调一致再开始测速。

#### 4.1.4 代码实践

**实践目标**：追踪一个 MPI 进程的完整生命周期，理解「谁在什么时候调了哪个 mpi 函数」。

**操作步骤**：

1. 打开 [multi_gpu_gpt_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc)。
2. 在 `main` 里找到 `mpi::initialize`（第 39 行）和 `mpi::finalize`（第 80 行）。
3. 进入 `multi_gpu_gpt_example<T>` 模板函数，统计 `mpi::barrier()` 与 `mpi::bcast(...)` 各出现了几次、分别在什么阶段（warmup 前、计时前、计时后）。
4. 注意每次 `mpi::barrier()` 紧邻的上一行是不是 `cudaDeviceSynchronize()`。

**需要观察的现象**：你会发现 `mpi::barrier()` 共 4 处，且**每一处前面都有一次 `cudaDeviceSynchronize()`**（行 232→233、244→245、256 后、267→268）。

**预期结果**：这是因为 `mpi::barrier` 只等进程、不等 GPU 流。若不先同步 GPU，某个进程可能已越过 barrier 而它的 GPU kernel 还没算完，导致下游 NCCL 通信时序错乱。这种「先 sync device 再 barrier」的固定搭配是多 GPU 调度的铁律。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FT 不直接用 MPI 的点对点收发（`MPI_Send`/`MPI_Recv`）来传 transformer 层之间的激活，而要引入 NCCL？

> **答案**：MPI 的点对点走的是 CPU 内存/网络栈，数据要先 GPU→CPU（`cudaMemcpy` D2H）再走 MPI 再 CPU→GPU（H2D），开销巨大。NCCL 直接在 GPU 之间走 NVLink/Infiniband，零拷贝、且针对集合通信（all-reduce 等）做了拓扑感知优化。FT 只用 MPI 做「进程组织 + 少量标量广播」，张量传输全交给 NCCL。

**练习 2**：`mpi::getCommWorldRank()` 在没有定义 `BUILD_MULTI_GPU` 时返回什么？为什么这样设计？

> **答案**：返回 0。这是为了让同一份调用 `mpi::getCommWorldRank()` 的模型代码在「单 GPU 编译」时也能运行——此时只有一个进程，rank 恒为 0。`#ifdef` 守卫 + 默认值 0 的组合实现了「多 GPU 能力可选、单 GPU 零成本降级」。

---

### 4.2 从全局 rank 到 GPU 设备绑定

#### 4.2.1 概念说明

MPI 启动 N 个进程后，每个进程只是一个普通的 CPU 进程，**并不会自动占有任何 GPU**。如果 8 个进程都默认用 GPU 0，就会全部挤在一张卡上，多 GPU 毫无意义。所以每个 rank 必须主动 `cudaSetDevice(...)` 绑定到「自己那张」GPU。

绑定规则很简单：**rank r 绑到第 `r % device_count` 张 GPU**（`device_count` 是本机可见的 GPU 数）。这样在单机上 8 张卡时，rank 0~7 依次落到 GPU 0~7，一一对应、互不争抢。

#### 4.2.2 核心流程

```text
rank = mpi::getCommWorldRank()          # 全局编号
device_count = cudaGetDeviceCount()     # 本机 GPU 数
cudaSetDevice(rank % device_count)      # 绑定：rank 对本机 GPU 数取余
```

这个 `% device_count` 是多节点部署的关键：第二台机器上的 rank 4~7，取余后还是 0~3，正好落在本机的 4 张卡上（详见 4.5）。

#### 4.2.3 源码精读

设备绑定发生在 `init_cuda_ctx`（[gpt_example_utils.cc:259-272](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L259-L272)）：

```cpp
Allocator<AllocatorType::CUDA> init_cuda_ctx(cudaStream_t& stream, cudaDeviceProp& prop, int rank)
{
    int device, device_count;
    check_cuda_error(cudaGetDeviceCount(&device_count));
    check_cuda_error(cudaSetDevice(rank % device_count));   // 关键：按 rank 选 GPU
    check_cuda_error(cudaGetDevice(&device));
    printf("P%d is running with %d GPU.\n", rank, device);
    ...
    return Allocator<AllocatorType::CUDA>(getDevice());
}
```

调用点在 [multi_gpu_gpt_example.cc:99](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L99)，传入的 `rank` 来自上一行 `init_multiprocessing` 返回的全局 rank（行 95）。绑完设备后，后续所有 `cudaMalloc`、stream、kernel 启动都默认落在「自己这张」卡上——整个进程的 GPU 资源就此确定。

#### 4.2.4 代码实践

**实践目标**：理解 `rank % device_count` 在单节点与多节点下的不同表现。

**操作步骤**：

1. 假设单机 8 卡，`mpirun -n 8`。列出 rank 0~7 各自绑到哪张 GPU。
2. 再假设 2 节点、每节点 4 卡，`mpirun -np 8 -H node1:4,node2:4`（OpenMPI 默认 by-slot 填充，rank 0~3 在 node1、4~7 在 node2）。列出每个 rank 落在哪个节点的哪张 GPU。

**预期结果**：

| rank | 单机8卡 GPU | 2节点×4卡 (node, GPU) |
| --- | --- | --- |
| 0 | GPU 0 | node1, GPU 0 |
| 1 | GPU 1 | node1, GPU 1 |
| 2 | GPU 2 | node1, GPU 2 |
| 3 | GPU 3 | node1, GPU 3 |
| 4 | GPU 4 | node2, GPU 0 |
| 5 | GPU 5 | node2, GPU 1 |
| 6 | GPU 6 | node2, GPU 2 |
| 7 | GPU 7 | node2, GPU 3 |

> 待本地验证：实际节点分配取决于 MPI 的 `--map-by` 策略，上表为 OpenMPI 默认 by-slot 行为。

#### 4.2.5 小练习与答案

**练习**：如果把 `cudaSetDevice(rank % device_count)` 改成 `cudaSetDevice(rank)`，在「2 节点 × 4 卡」部署时会发生什么？

> **答案**：node2 上的 rank 4~7 会尝试 `cudaSetDevice(4..7)`，但每台机器只有 4 张卡（合法 device id 为 0~3），`cudaSetDevice(4)` 会因 device id 越界直接报错失败。`% device_count` 正是为了让 rank 在「每节点本地从 0 重新编号」，这正是多节点部署能工作的前提。

---

### 4.3 二维笛卡尔拓扑：world → TP 行 × PP 列

> 这是本讲最核心的一节。rank 划分真正发生的地方不是 `mpi_utils`，而是 `ftNcclInitialize` 里那段 `MPI_Cart_create`。

#### 4.3.1 概念说明

FT 的硬约束是：

\[
\text{world\_size} = \text{tensor\_para\_size} \times \text{pipeline\_para\_size}
\]

也就是说，8 个进程既可以全用来做 TP（`TP=8, PP=1`，一个 block 切 8 份），也可以全做 PP（`TP=1, PP=8`，8 层排成流水线），还可以混用（`TP=4, PP=2`）。

但每个全局 rank 到底属于「哪个 TP 组」的「第几个成员」，又属于「哪个 PP 组」？FT 的做法是把全部进程排成一个 **二维网格**：

- **每一行**是一个 TP 组（组内 world_size = TP，组内 rank 切 head）。
- **每一列**是一个 PP 组（组内 world_size = PP，组内 rank 切层）。

举例 `TP=4, PP=2`，共 8 个进程，排成 2 行 4 列：

```text
        tp_rank:   0     1     2     3        ← 每行是一个 TP 组（4 张卡切 head）
pp_rank 0  →     [r0]  [r1]  [r2]  [r3]
pp_rank 1  →     [r4]  [r5]  [r6]  [r7]
                  ↑     ↑     ↑     ↑
                  每列是一个 PP 组（2 张卡切层）
```

于是：
- TP 组 `{r0,r1,r2,r3}` 和 `{r4,r5,r6,r7}` 各自内部做 all-reduce（切 head）。
- PP 组 `{r0,r4}`、`{r1,r5}`、`{r2,r6}`、`{r3,r7}` 各自内部按层传递激活（切层）。

#### 4.3.2 核心流程

FT 不手动算 `(tp_rank, pp_rank)`，而是交给 MPI 的笛卡尔拓扑 API 自动推导：

```text
1. 校验 TP × PP == world_size（否则报错退出）
2. dims[2] = {PP, TP}                         # 注意顺序：行=PP，列=TP
3. MPI_Cart_create(MPI_COMM_WORLD, 2, dims)   # 把一维 world 折成 PP×TP 二维网格
4. MPI_Cart_sub(grid, {false,true}) → tp_comm # 保留列维 → 每行一个 TP 子通信器
   MPI_Cart_sub(grid, {true,false}) → pp_comm # 保留行维 → 每列一个 PP 子通信器
5. tp_rank = MPI_Comm_rank(tp_comm)           # 本进程在自己 TP 组里的编号
   pp_rank = MPI_Comm_rank(pp_comm)           # 本进程在自己 PP 组里的编号
```

MPI 按行主序（row-major）铺 rank，因此全局 rank 与二维坐标的对应关系是：

\[
\text{global\_rank} = \text{pp\_rank} \cdot \text{tensor\_para\_size} + \text{tp\_rank}
\]

反过来：

\[
\text{pp\_rank} = \left\lfloor \frac{\text{global\_rank}}{\text{tensor\_para\_size}} \right\rfloor, \qquad
\text{tp\_rank} = \text{global\_rank} \bmod \text{tensor\_para\_size}
\]

> 说明：代码并不手算上面两个式子，而是通过 `MPI_Cart_sub` + `MPI_Comm_rank` 让 MPI 报告结果。这里给出公式是为了你能**预测**任意 rank 的归属，便于排错与部署规划。

#### 4.3.3 源码精读

**第一步：在 example 层做硬约束校验**——`init_multiprocessing`（[gpt_example_utils.cc:233-257](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L233-L257)）：

```cpp
std::pair<int, int> init_multiprocessing(const model_config_t& model_config)
{
    int rank       = mpi::getCommWorldRank();
    int world_size = mpi::getCommWorldSize();
    ...
    if (model_config.tensor_para_size * model_config.pipeline_para_size != world_size) {
        printf("[ERROR] tensor_para_size * pipeline_para_size should equal to world_size \n");
        exit(-1);
    }
    const int layers_per_group = model_config.decoder_layers / model_config.pipeline_para_size;
    if (layers_per_group * model_config.pipeline_para_size != (int)model_config.decoder_layers) {
        printf("[ERROR] layers_per_group ... should equal to decoder_layers ...\n");
        exit(-1);
    }
    return {rank, world_size};
}
```

这里校验了两件事：
1. `TP × PP == world_size`（启动进程数必须正好等于并行度乘积）。
2. `decoder_layers % PP == 0`（层数必须能被 PP 整除，否则没法均分给各 pipeline 阶段）。`layers_per_group` 就是每个 PP 阶段要负责的层数。

> 补充：`head_num % TP == 0` 的校验在 `read_model_config` 里（[gpt_example_utils.cc:116-117](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L116-L117)），与上面的两条共同构成「TP 切 head、PP 切层」的可整除前提。

**第二步：真正的二维拆分**——`ftNcclInitialize`（[nccl_utils.cc:308-418](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L308-L418)）。先是同样的校验，然后是核心拓扑代码（[nccl_utils.cc:364-380](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L364-L380)）：

```cpp
// Convert WORLD communicator into 2D grid (k * n) communicator.
//  row = a tensor parallel group, col = a pipeline parallel group.
MPI_Comm grid_comm, tp_comm, pp_comm;

int dims[2]    = {pipeline_para_size, tensor_para_size};
int periods[2] = {0, 0};
MPI_Cart_create(MPI_COMM_WORLD, 2, dims, periods, 0, &grid_comm);

// Split 2D communicator into rows and cols.
int tp_remain_dims[2] = {false, true};
int pp_remain_dims[2] = {true, false};
MPI_Cart_sub(grid_comm, tp_remain_dims, &tp_comm);
MPI_Cart_sub(grid_comm, pp_remain_dims, &pp_comm);

int tp_rank, pp_rank;
MPI_Comm_rank(tp_comm, &tp_rank);
MPI_Comm_rank(pp_comm, &pp_rank);
```

注意三个细节：

1. `dims = {pipeline_para_size, tensor_para_size}`——**行数是 PP、列数是 TP**（代码注释 `row = a tensor parallel group` 指的是「每一行构成一个 TP 组」）。
2. `tp_remain_dims = {false, true}`——保留第 2 维（列），于是同一行内的进程被分到同一个 `tp_comm`，组内 `tp_rank` 就是列号。
3. `pp_remain_dims = {true, false}`——保留第 1 维（行），同一列内的进程被分到同一个 `pp_comm`，组内 `pp_rank` 就是行号。

这段代码注释里的 `k * n`：`k = pipeline_para_size`（PP），`n = tensor_para_size`（TP），与 `init_nccl` 的注释（[gpt_example_utils.cc:275-279](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L275-L279)）一致：

```cpp
void init_nccl(const model_config_t& model_config, NcclParam& tensor_para, NcclParam& pipeline_para)
{
    // assume gpu_num = k * n,
    // tensor parallelism group size is n
    // pipeline parallelism group size is k
    ftNcclInitialize(tensor_para, pipeline_para, model_config.tensor_para_size, model_config.pipeline_para_size);
}
```

#### 4.3.4 代码实践

**实践目标**：用本节的公式，手算 `TP=4, PP=2, world_size=8` 时每个全局 rank 的 `(tp_rank, pp_rank)`，并与 4.3.1 的网格图对照。

**操作步骤**：

1. 对 `global_rank = 0..7`，套用 \(\text{tp\_rank} = \text{global\_rank} \bmod 4\)、\(\text{pp\_rank} = \lfloor \text{global\_rank} / 4 \rfloor\)。
2. 列出表格，标注每个 rank 属于哪个 TP 组、哪个 PP 组。
3. 找出 rank 3 与 rank 5 各自的 TP 组成员与 PP 组成员。

**预期结果**：

| global_rank | tp_rank | pp_rank | TP 组（同行） | PP 组（同列） |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 | {0,1,2,3} | {0,4} |
| 1 | 1 | 0 | {0,1,2,3} | {1,5} |
| 2 | 2 | 0 | {0,1,2,3} | {2,6} |
| 3 | 3 | 0 | {0,1,2,3} | {3,7} |
| 4 | 0 | 1 | {4,5,6,7} | {0,4} |
| 5 | 1 | 1 | {4,5,6,7} | {1,5} |
| 6 | 2 | 1 | {4,5,6,7} | {2,6} |
| 7 | 3 | 1 | {4,5,6,7} | {3,7} |

例如 rank 3 的 TP 组是 `{0,1,2,3}`、PP 组是 `{3,7}`；rank 5 的 TP 组是 `{4,5,6,7}`、PP 组是 `{1,5}`。这正是 `MPI_Cart_sub` 会算出来的结果，你可以在运行时看 [nccl_utils.cc:411-415](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L411-L415) 打印的 `FT_LOG_INFO("NCCL initialized rank=... tensor_para=... pipeline_para=...")` 来核对。

#### 4.3.5 小练习与答案

**练习 1**：`world_size=8` 时，`TP=2, PP=4` 和 `TP=4, PP=2` 的二维网格形状有何不同？分别有多少个 TP 组、多少个 PP 组？

> **答案**：`dims = {PP, TP}`。
> - `TP=2, PP=4`：网格 4 行 × 2 列 → 4 个 TP 组（每组 2 卡切 head）、2 个 PP 组（每组 4 卡切层）。
> - `TP=4, PP=2`：网格 2 行 × 4 列 → 2 个 TP 组（每组 4 卡）、4 个 PP 组（每组 2 卡）。
> 两者 `TP×PP=8` 都满足，但「切 head 的粒度」与「切层的粒度」不同，适合不同显存瓶颈的模型。

**练习 2**：如果某用户把 INI 写成 `tensor_para_size=3, pipeline_para_size=2`，却用 `mpirun -n 8` 启动，会在哪一行报错？

> **答案**：在 [gpt_example_utils.cc:242-245](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L242-L245) 报错——因为 `3 × 2 = 6 ≠ 8`，不满足 `TP × PP == world_size`。即便改成 `-n 6`，后续 `head_num % TP == 0` 也常常不满足（head_num 通常不是 3 的倍数），所以 TP 一般取 2/4/8。

---

### 4.4 NCCL 通信器建立与 pipeline 层切分

#### 4.4.1 概念说明

`MPI_Cart_sub` 拆出来的 `tp_comm`/`pp_comm` 只是 **MPI 通信器**，GPU 之间真正传数据还得靠 **NCCL**。NCCL 要工作，必须先为每个组建立一个 `ncclComm_t`（NCCL communicator），步骤是：

1. 组内的 rank 0 生成一个「唯一 ID」（`ncclUniqueId`）。
2. 把这个 ID 广播给组内所有成员（用 MPI 的 `MPI_Bcast`）。
3. 每个成员拿着同一个 ID 调 `ncclCommInitRank`，NCCL 内部完成握手，返回可用的 `ncclComm_t`。

TP 组和 PP 组各走一遍这套流程，最终得到两个独立的 NCCL communicator，分别存进 `tensor_para.nccl_comm_` 和 `pipeline_para.nccl_comm_`。

而 **pipeline 并行具体怎么切层**：模型有 `decoder_layers` 层，PP 个阶段，每个阶段负责连续的 `decoder_layers / PP` 层。第 `pp_rank` 个阶段负责第 `[pp_rank * layers_per_group, (pp_rank+1) * layers_per_group)` 层。各阶段顺序串联：上一阶段的输出激活通过 PP 组的 NCCL 通信传给下一阶段。

#### 4.4.2 核心流程

```text
# 对 TP 组和 PP 组各做一次：
组内 rank 0:  ncclGetUniqueId(&uid)          # 生成唯一 ID
MPI_Bcast(&uid, root=0, comm=组通信器)        # 广播给全组
全组每个 rank: ncclCommInitRank(comm, uid, my_rank)  # 握手建 NCCL communicator

# 层切分（在模型构建期）：
layers_per_group = decoder_layers / pipeline_para_size
本阶段负责层区间 = [pp_rank * layers_per_group, (pp_rank+1) * layers_per_group)
```

#### 4.4.3 源码精读

**NCCL communicator 建立**（[nccl_utils.cc:382-410](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L382-L410)）：

```cpp
ncclUniqueId tp_uid;
ncclUniqueId pp_uid;
// The root of each group creates a nccl uid.
if (tp_rank == 0) {
    NCCLCHECK(ncclGetUniqueId(&tp_uid));
}
if (pp_rank == 0) {
    NCCLCHECK(ncclGetUniqueId(&pp_uid));
}
// Broadcast nccl uid to share the same nccl uid across gpus in the same group.
MPI_Bcast(&tp_uid, sizeof(tp_uid), MPI_BYTE, 0, tp_comm);
MPI_Bcast(&pp_uid, sizeof(pp_uid), MPI_BYTE, 0, pp_comm);

ncclComm_t tp_nccl_comm, pp_nccl_comm;
NCCLCHECK(ncclCommInitRank(&tp_nccl_comm, tensor_para_size, tp_uid, tp_rank));
NCCLCHECK(ncclCommInitRank(&pp_nccl_comm, pipeline_para_size, pp_uid, pp_rank));
```

注意这里 **MPI 与 NCCL 的分工**：`MPI_Bcast` 负责把唯一 ID 散播给同组成员（控制信息，量很小），`ncclCommInitRank` 才真正建立后续能传大张量的 GPU 通信通道。

建好后填进 `NcclParam`（[nccl_utils.cc:403-410](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.cc#L403-L410)）：

```cpp
tensor_para.world_size_   = tensor_para_size;
tensor_para.rank_         = tp_rank;
tensor_para.nccl_uid_     = tp_uid;
tensor_para.nccl_comm_    = tp_nccl_comm;
pipeline_para.world_size_ = pipeline_para_size;
pipeline_para.rank_       = pp_rank;
pipeline_para.nccl_uid_   = pp_uid;
pipeline_para.nccl_comm_  = pp_nccl_comm;
```

`NcclParam` 是个轻量 POD 结构（[nccl_utils.h:60-86](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.h#L60-L86)），字段就是 `rank_`/`world_size_`/`nccl_comm_`，可值拷贝（u7-l1 已讲）。这两个 `NcclParam` 随后被传给 `ParallelGpt` 构造函数（[multi_gpu_gpt_example.cc:186-187](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L186-L187)），模型据此知道自己在两个维度上的位置。

**层切分的证据**——KV cache 的第一维就是 `num_layer / pipeline_para_size`（以 T5Decoder 为例，[T5Decoder.cc:367](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoder.cc#L367)）：

```text
//      key_cache [num_layer / pipeline_para_.world_size_, batch, head_num, size_per_head // x, max_seq_len, x]
```

这说明每个 pipeline 阶段只为「自己那段层」分配 KV cache。`decoder_layers=24, PP=2` 时，pp_rank=0 的进程管第 0~11 层、pp_rank=1 的进程管第 12~23 层，各自只缓存这 12 层的 K/V。权重加载同理：`ParallelGptWeight` 构造时传入 `pipeline_para.world_size_` 与 `pipeline_para.rank_`（[multi_gpu_gpt_example.cc:130-131](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L130-L131)），只加载本阶段那 `layers_per_group` 层的权重（u2-l5 已详述）。

示例结束时记得销毁两个 communicator（[multi_gpu_gpt_example.cc:294-295](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L294-L295)）：

```cpp
ftNcclParamDestroy(tensor_para);
ftNcclParamDestroy(pipeline_para);
```

#### 4.4.4 代码实践

**实践目标**：理解为什么需要**两个独立**的 NCCL communicator，而不是一个。

**操作步骤**：

1. 回顾 4.3.1 的 `TP=4, PP=2` 网格。rank 0 同时属于 TP 组 `{0,1,2,3}` 和 PP 组 `{0,4}`。
2. 思考：如果只有一个全局 communicator，rank 0 发起一次 all-reduce，谁会参与？
3. 阅读上述 `ncclCommInitRank` 代码，确认 TP communicator 的 `world_size = tensor_para_size = 4`、PP communicator 的 `world_size = pipeline_para_size = 2`。

**预期结果**：两个 communicator 把通信范围**严格隔离**——TP communicator 只在 4 张切 head 的卡之间 all-reduce，PP communicator 只在 2 张切层的卡之间传激活。若共用一个 communicator，一次集合通信会牵连全部 8 张卡，既错误又慢。这种「每个并行维度一个独立 communicator」是 Megatron 系张量/流水并行的通用范式。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NCCL 的 `ncclUniqueId` 要由 MPI 来广播，而不是 NCCL 自己搞定？

> **答案**：`ncclGetUniqueId` 只能在**一个进程**里生成（通常由 rank 0 调用），但组内其他进程需要拿到**同一个** ID 才能通过 `ncclCommInitRank` 完成握手。NCCL 本身在 communicator 建立之前还没有通信能力，所以这个「把 ID 散播给同组」的引导任务只能交给已经就绪的 MPI（`MPI_Bcast`）。这是 MPI 与 NCCL 经典的「MPI 引导、NCCL 干活」分工。

**练习 2**：`decoder_layers=24, PP=3` 能不能跑？为什么？

> **答案**：能。因为 `24 % 3 == 0`，每个 pipeline 阶段负责 8 层，可整除。`init_multiprocessing` 的第二个校验（[gpt_example_utils.cc:247-254](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L247-L254)）就是确保 `decoder_layers % PP == 0`。反之 `decoder_layers=22, PP=4`（22 不能被 4 整除）会被拒绝。

---

### 4.5 多节点启动：mpirun 与 slurm

#### 4.5.1 概念说明

有了上面的机制，把 GPT 推理从单机扩到多机几乎不用改代码——只要 MPI 能跨节点把进程拉起来，`ftNcclInitialize` 就能照常建好跨节点的 NCCL communicator。唯一需要处理的是**网络环境**：节点间要走 Infiniband（IB）而不是普通以太网，且要让 MPI 知道哪些节点、每节点几个进程。

关键部署原则（承接 u7-l1 的结论）：

- **TP 宜在节点内**：TP 组的 all-reduce 频繁且数据量大，走节点内 NVLink（带宽极高）最划算。
- **PP 可跨节点**：PP 组只在阶段边界传一次激活，频率低、数据量小，可以容忍跨节点走 IB 的较高延迟。

这两条直接决定了 rank 在节点上的排布方式（见 4.5.4）。

#### 4.5.2 核心流程

```text
# 单节点 TP/PP（8 进程都在一台机器）
mpirun -n 8 ./bin/multi_gpu_gpt_example ../examples/cpp/multi_gpu_gpt/gpt_config.ini

# 多节点（每节点若干进程，用 -H 指定主机列表）
mpirun --allow-run-as-root -np <N> -H node1:<slots>,node2:<slots> \
       ./bin/multi_gpu_gpt_example

# 集群上用 slurm
srun -N <节点数> --ntasks-per-node=<每节点进程数> --mpi=pmix \
     ./bin/multi_gpu_gpt_example
```

无论哪种方式，必须保证「进程总数 == INI 里的 `tensor_para_size × pipeline_para_size`」。

#### 4.5.3 源码精读

gpt_guide 明确给出约束与示例（[gpt_guide.md:434-437](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L434-L437)）：

> Users can use `tensor_para_size` and `pipeline_para_size` in `gpt_config.ini` to control the size of model parallel. Note that the number of processes must equal to `tensor_para_size * pipeline_para_size`.
>
> ```bash
> mpirun -n 8 ./bin/multi_gpu_gpt_example
> ```

多节点部分（[gpt_guide.md:441-457](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L441-L457)）指出「C++ 样例用 MPI 通信，可轻松扩展到多节点，只需配置好节点间网络」，并给出 slurm + docker + ssh 的完整搭建脚本，核心启动行是（[gpt_guide.md:455](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L455)）：

```bash
mpirun --allow-run-as-root -np 2 -H prm-dgx-09:1,prm-dgx-10:1 \
       -mca plm_rsh_args "-p 11068" ./bin/multi_gpu_gpt_example
```

这里 `-H prm-dgx-09:1,prm-dgx-10:1` 表示两台主机各分配 1 个进程槽（`-np 2` 共 2 进程），`-mca plm_rsh_args "-p 11068"` 指定节点间 ssh 走 11068 端口（前面脚本里用 `/usr/sbin/sshd -p 11068` 起的 sshd）。> 注意：这是一个 `-np 2` 的**最小化**多节点演示（对应 INI 里 `TP×PP=2`），并未占满两台 DGX 的全部 GPU。要占满需要把每节点槽位调成实际 GPU 数、相应调大 `-np`，并配合更大的 TP/PP（见 4.5.4）。

#### 4.5.4 代码实践（本讲主实践任务）

**实践目标**：为「2 节点 × 4 GPU、TP=4 PP=2」写出 `mpirun` 启动命令，并解释每个 rank 落在哪个节点的哪张卡、属于哪个 TP/PP 组，验证「TP 走节点内 NVLink、PP 走节点间 IB」。

**操作步骤**：

1. 共 8 进程，INI 需设 `tensor_para_size=4`、`pipeline_para_size=2`（满足 `4×2=8`）。
2. 用 `-H node1:4,node2:4` 让每节点跑 4 个进程，OpenMPI 默认 by-slot 填充：rank 0~3 落 node1、rank 4~7 落 node2。
3. 由 4.3 的公式算出每个 rank 的 `(tp_rank, pp_rank)`，再由 4.2 的 `cudaSetDevice(rank % 4)` 算出每 rank 的 GPU。
4. 检查 TP 组与 PP 组的节点分布，验证拓扑最优性。

**启动命令**（基于 gpt_guide 的 `-H` 主机列表语法与 `-n 8` 单节点示例组合而成；2 节点跨机需额外配置 IB 与 ssh，参见 gpt_guide 多节点脚本）：

```bash
# 示例命令（基于 docs/gpt_guide.md 的 mpirun 语法构造，2 节点跨机运行需先配好 Infiniband 与免密 ssh）
mpirun --allow-run-as-root -np 8 \
       -H node1:4,node2:4 \
       ./bin/multi_gpu_gpt_example ../examples/cpp/multi_gpu_gpt/gpt_config.ini
```

对应 `gpt_config.ini` 关键项：

```ini
[ft_instance_hyperparameter]
tensor_para_size = 4
pipeline_para_size = 2
data_type = fp16
```

**预期结果（rank → 节点/GPU/组 的完整映射）**：

| global_rank | 节点 | GPU | tp_rank | pp_rank | TP 组（同行/同节点） | PP 组（跨节点） |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | node1 | GPU0 | 0 | 0 | {0,1,2,3} 全在 node1 | {0,4} 跨 node1↔node2 |
| 1 | node1 | GPU1 | 1 | 0 | {0,1,2,3} | {1,5} 跨节点 |
| 2 | node1 | GPU2 | 2 | 0 | {0,1,2,3} | {2,6} 跨节点 |
| 3 | node1 | GPU3 | 3 | 0 | {0,1,2,3} | {3,7} 跨节点 |
| 4 | node2 | GPU0 | 0 | 1 | {4,5,6,7} 全在 node2 | {0,4} 跨节点 |
| 5 | node2 | GPU1 | 1 | 1 | {4,5,6,7} | {1,5} 跨节点 |
| 6 | node2 | GPU2 | 2 | 1 | {4,5,6,7} | {2,6} 跨节点 |
| 7 | node2 | GPU3 | 3 | 1 | {4,5,6,7} | {3,7} 跨节点 |

**关键观察**：
- 两个 TP 组 `{0,1,2,3}` 和 `{4,5,6,7}` **各自完整地落在一个节点内**，组内 all-reduce 全走节点内 NVLink——高频通信用高带宽，最优。
- 四个 PP 组 `{0,4}`、`{1,5}`、`{2,6}`、`{3,7}` **都跨 node1↔node2**，组内传激活走 Infiniband——低频通信容忍较高延迟，可接受。

这正是 `dims = {PP, TP}`（行=PP、列=TP）配合「连续 rank 先填满一个节点」的 by-slot 分配所达成的拓扑最优：**TP 维度自然落在节点内、PP 维度自然跨节点**。如果你误用 `--map-by node`（轮流往各节点撒进程），就会把 TP 组打散到跨节点，all-reduce 被迫走慢速 IB，性能急剧下降。

> 待本地验证：上表基于 OpenMPI 默认 by-slot 行为与 4.3 节行主序公式推导；实际部署需在双节点集群上核对 `FT_LOG_INFO` 打印的 `rank / tensor_para / pipeline_para`。

#### 4.5.5 小练习与答案

**练习 1**：同样是 2 节点 × 4 GPU，如果改成 `TP=2, PP=4`，TP 组和 PP 组的节点分布会变成什么样？哪种更适合「层很深、head 数不多」的模型？

> **答案**：`dims={4,2}`，网格 4 行 × 2 列。rank 0~3 在 node1、4~7 在 node2。TP 组变成 `{0,1}`/`{2,3}`/`{4,5}`/`{6,7}`，每组 2 卡且都在同节点内；PP 组变成 `{0,2,4,6}`/`{1,3,5,7}`，每组 4 卡跨两节点。对于「层深、head 少」的模型（显存瓶颈在层数），应该用更大的 PP（如 `PP=4`）把层切得更细，所以 `TP=2, PP=4` 更合适。

**练习 2**：为什么 gpt_guide 多节点脚本里要 `--device=/dev/infiniband` 和 `--network=host`？

> **答案**：跨节点 NCCL 通信必须能访问 IB 设备（`--device=/dev/infiniband` 把 IB 透传进容器），且需要让 MPI/NCCL 用主机的网络栈与 IB 发现机制（`--network=host` 让容器直接用主机网络）。没有这两项，节点间的 NCCL communicator 建不起来或退化到慢速以太网。

---

## 5. 综合实践

**任务**：为一个「`decoder_layers=24, head_num=16, size_per_head=64`」的 GPT 模型，在 **2 节点 × 4 GPU** 上设计一套 `TP=4, PP=2` 的部署方案，把本讲四个模块串起来。

要求完成：

1. **可整除性自检**：确认 `head_num % TP == 0`、`decoder_layers % PP == 0`、`TP × PP == 8`，并算出 `layers_per_group`、每个 TP rank 分到的 head 数。
2. **rank 映射表**：仿照 4.5.4，列出 rank 0~7 的 `(节点, GPU, tp_rank, pp_rank)`，并标出每个 rank 负责的**层区间**（用 `pp_rank * layers_per_group` 起算）。
3. **通信器清单**：指出本进程会持有几个 NCCL communicator、各自的 `world_size`，以及「TP all-reduce 走什么物理链路、PP 传激活走什么物理链路」。
4. **启动命令**：写出 `mpirun` 命令与 `gpt_config.ini` 的并行相关字段。
5. **失败排查**：如果运行时 `FT_LOG_INFO` 显示某 rank 的 `tensor_para` 与你算的不符，最可能是哪一步配错了（提示：`--map-by` 策略 或 INI 的 TP/PP 取值）。

**参考要点**：

- 自检：`16 % 4 == 0` ✓、`24 % 2 == 0` ✓、`4×2=8` ✓；`layers_per_group = 24/2 = 12`；每 TP rank 分到 `16/4 = 4` 个 head。
- 层区间：pp_rank=0 的 rank（0~3）负责第 0~11 层；pp_rank=1 的 rank（4~7）负责第 12~23 层。
- 通信器：每进程 2 个 NCCL communicator——TP communicator（`world_size=4`，走节点内 NVLink）、PP communicator（`world_size=2`，走节点间 IB）。
- 命令见 4.5.4。
- 排查：最可能是 MPI 进程排布策略（`--map-by`）与预期不符，导致 TP 组被打散到跨节点；其次是 INI 里 `tensor_para_size`/`pipeline_para_size` 写错，使 `TP×PP≠8` 在 [gpt_example_utils.cc:242](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L242) 处直接报错退出。

---

## 6. 本讲小结

- **MPI 是组织者，NCCL 是搬运工**：`mpi_utils` 只提供 `initialize`/`getCommWorldRank`/`barrier`/`bcast` 等薄封装，全受 `BUILD_MULTI_GPU` 守卫；张量传输全靠 NCCL。
- **硬约束 `world_size = TP × PP`**：在 [gpt_example_utils.cc:242](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L242) 校验；同时 `decoder_layers % PP == 0`、`head_num % TP == 0`。
- **rank 绑 GPU**：`cudaSetDevice(rank % device_count)`，让多节点下每节点从 GPU 0 重新编号。
- **二维笛卡尔拓扑是核心**：`ftNcclInitialize` 用 `MPI_Cart_create(dims={PP,TP})` + `MPI_Cart_sub` 把全局 rank 折成 `(tp_rank, pp_rank)`，行=TP 组、列=PP 组。
- **两个独立 NCCL communicator**：TP 组和 PP 组各建一个，通信范围严格隔离；`ncclUniqueId` 由 MPI `MPI_Bcast` 引导散播。
- **拓扑最优排布**：让连续 rank 先填满一个节点（OpenMPI 默认 by-slot），TP 组自然落节点内走 NVLink、PP 组自然跨节点走 IB。

---

## 7. 下一步学习建议

- 想看 TP 通信在层内具体怎么 all-reduce，回到 [u7-l1 张量并行](u7-l1-tensor-parallel.md)，对照 `TensorParallelGeluFfnLayer` 与 `ftNcclAllReduceSum`。
- 想了解「PP 之外的另一种降延迟通信」，继续读 **u7-l3 自定义 all-reduce kernel**（`custom_ar_comm` 在 DGX-A100 上用 CUDA 直连替代 NCCL）。
- 想看流水并行的另一种「线程版」组织方式（节点内用线程、节点间用 MPI），阅读 [examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc) 与 u10-l3 Triton backend。
- 推荐顺读 [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) 的 "Run on multi-node" 小节，把本讲的命令在真实集群上验证一遍。
