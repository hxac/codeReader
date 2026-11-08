# ZeRO-Infinity：NVMe 交换与异步 IO

## 1. 本讲目标

学完本讲，你应该能够：

1. 描述 `AsyncPartitionedParameterSwapper`（参数交换器）的缓冲池、读写队列与状态机，说清一次 swap-in / swap-out 的完整生命周期。
2. 解释 DeepNVMe 异步 IO 内核（`csrc/aio`）的线程模型：`aio_handle` 如何把一个 I/O 请求广播到多个 AIO 线程、`block_size` / `queue_depth` / `single_submit` / `overlap_events` / `intra_op_parallelism` 五个调优参数各自控制什么。
3. 理解参数与优化器状态在 NVMe → CPU（页锁定内存）→ GPU 之间的两级流水，以及优化器 fast init 如何用双缓冲池把它流水线化。
4. 解读本周期（PR #8985）的「静默 I/O 失败」修复：`wait()` / `sync_pread()` / `sync_pwrite()` 为什么现在会抛 `RuntimeError`，`io_submit` 错误为何不再 `assert` 崩溃，`io_pgetevents` 遇到 `EINTR` 如何重试，`io_queue_init` 失败如何抛错。
5. 说清 O_DIRECT 的对齐约束：为什么一个「未对齐视图」会让内核拒绝请求却仍报告完成，`statx` 如何探测每个文件真实需要的对齐，bounce buffer 与 padded staging 读取又分别在参数路径和优化器路径上如何兜底。

## 2. 前置知识

- **三级存储层次**：GPU 显存（HBM，几十~几百 GB，带宽数 TB/s）→ CPU DRAM（数百 GB~TB 级）→ NVMe SSD（数 TB~数十 TB，带宽几 GB/s~十几 GB/s）。ZeRO-Infinity 的思路是：当参数、梯度、优化器状态连 CPU 内存都放不下时，把「冷数据」再下沉一层到 NVMe，用时再搬回来。
- **libaio 与 O_DIRECT**：Linux 的异步 I/O 库 `libaio` 提供 `io_submit`（提交请求）与 `io_pgetevents`（收割完成事件）两个核心系统调用封装。DeepSpeed 以 `O_DIRECT` 标志打开文件，绕过 page cache 直接在用户缓冲区与磁盘之间传输——这避免了训练数据污染 page cache，但带来一个严格约束：**缓冲区地址、传输长度、文件偏移都必须按设备要求的粒度对齐**（通常是 512 字节或 4KB，后文详述）。这也是本讲第 4、5 节整条修复线的根源。
- **页锁定内存（pinned memory）**：承接 u5-l4。`pin_memory` 分配的 CPU 内存不会被操作系统换出，物理地址固定，DMA 引擎可以直接访问。DeepNVMe 中所有直接 I/O 的缓冲区都必须是 pinned 的，否则要经过 bounce buffer 中转。
- **参数分区与协调器**：承接 u5-l5。ZeRO-3 把每个参数按 `ds_tensor` 分区保存在本 rank；参数协调器（`partitioned_param_coordinator.py`）负责按需把参数聚合到 GPU。本讲的交换器在协调器之下再挂一层：分区本体可以不在内存，而在 NVMe 文件里。
- **errno 风格错误码**：Linux 系统调用失败时返回负的 `errno`（如 `-EINVAL`、 `-EBADF`、 `-EFBIG`）。libaio 的完成事件里，`res` 字段为负数表示失败、正数表示实际传输字节数。本周期修复的核心就是「这些数字以前没人看」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deepspeed/runtime/swap_tensor/partitioned_param_swapper.py` | 参数交换器：为分区参数分配 pinned swap buffer、发起 NVMe 读写、维护 `PartitionedParamStatus` 状态机 |
| `deepspeed/runtime/swap_tensor/utils.py` | `swap_in_tensors` / `swap_out_tensors` 薄封装、`SwapBuffer` / `SwapBufferPool` / `SwapBufferManager` 三层缓冲管理、对齐常量 |
| `deepspeed/runtime/swap_tensor/optimizer_utils.py` | 优化器状态交换的公共基类 `OptimizerSwapper`：swap 文件命名、梯度交换、fast init 的双池流水 |
| `deepspeed/runtime/swap_tensor/async_swapper.py` | `AsyncTensorSwapper`：梯度换出时的缓冲轮转（边攒边刷） |
| `deepspeed/runtime/swap_tensor/constants.py` | aio 配置键名与默认值 |
| `csrc/aio/py_lib/deepspeed_py_io_handle.cpp` | `aio_handle` 的 C++ 本体：线程池、提交/完成队列、`wait()` 的错误聚合与抛出 |
| `csrc/aio/py_lib/deepspeed_aio_thread.cpp` | AIO 工作线程的主循环 |
| `csrc/aio/py_lib/deepspeed_cpu_op.cpp` | 单个 I/O 请求描述符 `cpu_op_desc_t`：对齐判定、bounce buffer 分配与数据回填 |
| `csrc/aio/py_lib/deepspeed_aio_op_desc.cpp` | 请求基类 `io_op_desc_t`：按线程均分字节区间、原子错误码 `set_error` |
| `csrc/aio/common/deepspeed_aio_common.cpp` | libaio 的 submit/reap 算法（顺序与重叠两种模式）、`O_DIRECT` 打开文件、`statx` 对齐探测 |
| `csrc/aio/common/deepspeed_aio_types.cpp` | `aio_context`：iocb 数组与 `io_queue_init`（失败抛错） |
| `op_builder/async_io.py` | `AsyncIOBuilder`：编译清单、libaio 依赖探测、加载顺序 |
| `deepspeed/nvme/perf_run_sweep.py` + `deepspeed/nvme/parse_nvme_stats.py` + `bin/ds_nvme_tune` | NVMe 测速与调参工具链（微基准搜索空间、结果解析） |
| `deepspeed/nvme/io_engine.py` | 微基准的引擎选择器（aio_handle / basic / torch_io / fastio 四种后端的多进程调度），注意它属于测试工具而非训练运行时 |
| `tests/unit/v1/nvme/test_aio.py` | aio_handle 单测，含本周期新增的未对齐视图（`TestBufferView`）与错误注入（`TestFailedIO`）用例 |
| `tests/unit/v1/zero/test_zero3_nvme_offload.py` | 端到端回归：ZeRO-3 + NVMe 卸载必须与 CPU 卸载训练出逐位一致的结果 |

## 4. 核心概念与源码讲解

### 4.1 PartitionedParamSwapper：参数的 NVMe 交换器

#### 4.1.1 概念说明

ZeRO-3 中每个参数有一个 `ds_tensor` 分区。如果配置了 `offload_param.device=nvme`，这个分区的「家」就在 NVMe 文件里，内存中只保留少量 pinned「交换缓冲」（swap buffer）。交换器的职责是：

1. 给每个分区参数分配/回收交换缓冲（缓冲数量有限，靠 `buffer_count` 控制）；
2. 把内存中的分区写到 NVMe（swap-out），或从 NVMe 读回（swap-in）；
3. 维护参数的三态：`AVAILABLE`（内存里可用）/ `INFLIGHT`（正在读）/ `NOT_AVAILABLE`（只在盘上）。

一句话：**交换器管理的是「NVMe ↔ CPU pinned 内存」这一段搬运；「CPU ↔ GPU」那一段由 u5-l5 讲过的参数协调器负责**，两者拼成两级流水。

#### 4.1.2 核心流程

一次典型生命周期的伪代码：

```text
swap-out（释放内存）:
    分区已在某 swap buffer 中
    -> _swap_out(): async_pwrite(buffer -> {nvme_path}/zero_stage_3/{dtype}params/rank{r}/{param_id}_param.tensor.swp)
    -> synchronize_writes(): aio_write_handle.wait()
    -> remove_partition_and_release_buffers(): 归还 buffer id，ds_status = NOT_AVAILABLE

swap-in（按需取回）:
    断言 ds_status == NOT_AVAILABLE
    -> _allocate_and_return_buffers_for_swap_in(): 从可用池弹出一个 buffer id，
       按 I/O 对齐粒度向上取整得到 swap_buffer，前缀 narrow 得到 compute_buffer
    -> async_pread(file -> swap_buffer)，ds_status = INFLIGHT
    -> synchronize_reads(): wait() 后把 param.ds_tensor.data 指向 compute_buffer，
       ds_status = AVAILABLE
```

对齐数学（贯穿全讲）：设 `intra_op_parallelism = P`，元素大小 \( e \) 字节，则

\[ \text{numel\_alignment} = \frac{1024 \times P}{e}, \qquad \text{aligned\_numel}(n) = \left\lceil \frac{n}{\text{numel\_alignment}} \right\rceil \times \text{numel\_alignment} \]

即每个交换分区的元素数向上补齐到 `1024 × P` 字节的整数倍——交换文件里存的是补齐后的长度，读回来后 `narrow(0, 0, n)` 再切出真实数据。

#### 4.1.3 源码精读

状态机三态定义在 [partitioned_param_swapper.py:L26-L34](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L26-L34)：`AVAILABLE` / `NOT_AVAILABLE` / `INFLIGHT`，与 u5-l5 协调器使用的 `ds_status` 是同一个概念。

构造与 AIO 配置在 [partitioned_param_swapper.py:L83-L131](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L83-L131)：交换目录按 `{nvme_path}/zero_stage_3/{dtype}params/rank{rank}` 组织且构造时整目录清空重建；`use_gds` 为真时用 GPUDirect Storage 的 `gds_handle`（缓冲直接放 GPU），否则用 `aio_handle`（缓冲放 CPU）；读、写各建一个独立句柄，互不阻塞。对齐常量与缓冲池：

- [partitioned_param_swapper.py:L100-L102](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L100-L102)：`min_aio_bytes = max(1MB, block_size)`（小于它的张量不值得直接 I/O），`aligned_bytes = 1024 × P`，推导出 `numel_alignment`。
- [partitioned_param_swapper.py:L123-L131](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L123-L131)：`buffer_count × aligned_elements_per_buffer` 个元素的一整块 pinned 内存，再按 buffer id 切片复用。

swap-in 的缓冲分配在 [partitioned_param_swapper.py:L178-L201](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L178-L201)：断言该参数当前没有占用缓冲（这条断言正是 u5-l5 讲过的 #9014 预取去重修复所保护的），弹出 buffer id，按对齐长度 `narrow` 出 swap_buffer，再 `narrow(0, 0, numel)` 得到 compute_buffer——**读写走对齐后的长缓冲，计算用前缀短缓冲**。

读完成后的指针交接在 [partitioned_param_swapper.py:L212-L232](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L212-L232)：`synchronize_reads()` 等待 `aio_read_handle.wait()`，把 `param.ds_tensor.data` 指向 compute_buffer，状态置 `AVAILABLE`，并把这批参数登记进可用集合。对称的写完成处理在 [partitioned_param_swapper.py:L204-L210](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L204-L210)：`synchronize_writes()` 确认写完才允许释放缓冲。

异步读入口 [partitioned_param_swapper.py:L291-L323](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L291-L323) 的 `swap_in()`：先断言所有参数都处于 `NOT_AVAILABLE`，缓冲不够时打印详尽的诊断信息再断言失败；真正的 I/O 只是循环调用 [`swap_in_tensors`（utils.py:L19-L21）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/utils.py#L19-L21)——逐个 `async_pread(buffer, path, 0)`，返回 0 表示已入队。

向「非交换器控制的缓冲」同步读入的 [swap_into_buffer（partitioned_param_swapper.py:L326-L349）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L326-L349)：若目标缓冲不是 pinned 或长度不对齐，就得借用内部交换缓冲中转再 `copy_` 过去——这是 Python 层的一次「手工 bounce buffer」，与 4.4 节 C++ 层自动 bounce buffer 互为印证。

梯度侧的配套流水在 [async_swapper.py:L67-L99](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/async_swapper.py#L67-L99)：`AsyncTensorSwapper` 拿到一批 pinned 缓冲后，把梯度张量逐个 `insert_tensor` 进当前缓冲（内部先拷贝再登记待写），放不下就 [刷出就绪缓冲（L112-L136）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/async_swapper.py#L112-L136)，全部缓冲都在忙则 [阻塞等完成（L138-L155）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/async_swapper.py#L138-L155)——一个典型的「攒一批、异步刷、必要时等」的三态轮转。

优化器状态的交换基类在 [optimizer_utils.py:L159-L190](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/optimizer_utils.py#L159-L190)：`OptimizerSwapper` 管理每个参数的 `OptimizerStateSwapInfo`（参数本体 + Adam moments 等状态张量，各对应一个 `{param_id}.tensor.swp` 文件，梯度则按 offset 拆成 `{param_id}_gradient_{offset}_{length}.tensor.swp`，见 [optimizer_utils.py:L102-L111](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/optimizer_utils.py#L102-L111)），并用 `SwapBufferManager`（[utils.py:L180-L199](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/utils.py#L180-L199)）池化 pinned 缓冲。梯度换出时若某段长度不对齐， [`_adjust_for_misaligned_lengths`（optimizer_utils.py:L464-L489）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/optimizer_utils.py#L464-L489) 会把它切成「对齐大段 + 剩余小段」两份分别处理——对齐问题在 Python 层就被主动拆解。

#### 4.1.4 代码实践

**实践目标**：验证「NVMe 卸载必须与 CPU 卸载训练出完全相同的结果」，并读懂端到端回归测试的构造方式。

1. 打开 [tests/unit/v1/zero/test_zero3_nvme_offload.py:L4-L7](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_offload.py#L4-L7) 的模块文档串：作者刻意把模型第一层输出设为 999（`IN=1000, ODD=999`），使第一个权重的分区长度不是 I/O 块的整数倍，且把第二个权重的分区顶到直接 I/O 用不了的地址上——即「专挑未对齐下手」。
2. 阅读配置构造（[test_zero3_nvme_offload.py:L30-L58](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_offload.py#L30-L58) 的 `_config`）：`offload_param` 带 `nvme_path`、`buffer_count=4`、`buffer_size=4e6`（[L40](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_offload.py#L40)）；`offload_optimizer` 另有 `pipeline_read/pipeline_write` 与 `fast_init` 开关。
3. 阅读断言（[test_zero3_nvme_offload.py:L89-L94](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_offload.py#L89-L94)）：NVMe 训练的逐步 loss 列表与 CPU 卸载**逐项相等**，每个权重 `torch.equal` 逐位一致。
4. 有真实 Linux + libaio 环境时运行：`pytest tests/unit/v1/zero/test_zero3_nvme_offload.py -v`（需要 `async_io` 与 `cpu_adam` 算子可用、bf16 支持；本讲义环境下**待本地验证**）。

**需要观察的现象**：`test_param_and_optimizer[fast_init=True]` 与 `[fast_init=False]` 都通过——前者走 4.4.3 要讲的 padded staging 读取路径。

**预期结果**：任何一条失败都意味着 NVMe 搬运改变了数值（修复前该场景在 md RAID 上 290 个参数中会有 60 个错）。

#### 4.1.5 小练习与答案

**练习 1**：为什么读、写要建两个独立的 `aio_handle`，而不是共用一个？
**答案**：句柄内部有 `_num_pending_ops` 计数与一条完成队列（见 4.2.3）。swap-out 与 swap-in 常在时间上交叠（上一层预取读、下一层释放写）。共用一个句柄时，`wait()` 会把读写请求混在一起等待与计数，`synchronize_writes()` 无法只等写完成；分开后两个方向互不阻塞，且各自 `wait()` 的返回值可与 `pending_writes` / `pending_reads` 精确对账（[partitioned_param_swapper.py:L204-L220](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L204-L220) 的两个 `assert` 就是对账）。

**练习 2**：`swap_in_buffers` 池只有 `buffer_count` 个，如果同时在途的参数超过池子大小会发生什么？
**答案**：`swap_in()` 先打印三行诊断（在途参数/缓冲/numel、可用参数统计），随后断言 `len(swap_in_paths) <= len(available_buffer_ids)` 失败（[partitioned_param_swapper.py:L298-L312](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L298-L312)）。这说明上层（协调器）必须先 `synchronize_reads()` 回收缓冲，或调大 `offload_param.buffer_count`。

**练习 3**：`PartitionedParamStatus.INFLIGHT` 状态存在的意义是什么？
**答案**：它标记「读请求已提交但数据未就绪」的窗口期。此间 `swap_in()` 的入口断言会拒绝同一参数被二次提交（`Some params are already available or in flight`），而 `synchronize_reads()` 依据 inflight 名单决定把哪些参数的 `ds_tensor.data` 接到刚完成的缓冲上——没有这个状态就无法区分「还没发起读」与「读在路上」。

### 4.2 AIO 句柄与线程池：async_io 内核的执行模型

#### 4.2.1 概念说明

`aio_handle` 是 DeepNVMe 的用户入口（`AsyncIOBuilder().load().aio_handle(...)`），C++ 本体是 `deepspeed_io_handle_t`。它对外暴露四组 API：

- `sync_pread/sync_pwrite(buffer, filename, offset)`：阻塞读/写，返回完成的请求数；
- `async_pread/async_pwrite(...)`：立即返回 0，稍后用 `wait()` 收割；
- `wait()`：等待所有在途请求完成，返回完成数，**失败时抛 `RuntimeError`**（本周期新增行为）；
- `new_cpu_locked_tensor / free_cpu_locked_tensor / is_pinned`：pinned 内存的管理与查询。

五个构造参数（JSON 里 `"aio"` 块，默认值见 [constants.py:L8-L30](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/constants.py#L8-L30)）：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `block_size` | 1 MB | 单个 iocb 传输的字节数；一次大传输会被切成 \(\lceil N/b \rceil\) 个块 |
| `queue_depth` | 8 | 每个 `aio_context` 同时在途的 iocb 数；在途字节量约为 \( q \times b \) |
| `single_submit` | false | true 时逐个 `io_submit`（每次一个 iocb），false 时整批提交 |
| `overlap_events` | true | true 时提交与收割交替进行（流水线），false 时整批提交完再整批等 |
| `intra_op_parallelism` | 1 | 单个请求拆给多少个 AIO 线程并行执行 |

直觉：`block_size` 与 `queue_depth` 决定「一次压给磁盘多少在途数据」——太小喂不饱 NVMe 队列，太大占内存且延迟高；`intra_op_parallelism` 让一个 400 MB 的优化器分区同时用 4 个线程各搬 100 MB。

#### 4.2.2 核心流程

一次 `async_pread` 的完整旅程：

```text
Python 调用线程                     intra_op_parallelism 个 AIO 线程（每个自带独立 aio_context）
────────────────                    ─────────────────────────────────────────────
pread(): 校验文件大小/可整除性
  -> open_file(O_RDONLY|O_DIRECT)
  -> _create_io_op_desc(cpu_op_desc_t)   # 此处做对齐判定、必要时备好 bounce buffer
  -> 入队：拷贝 shared_ptr 到每个
     线程的 work_queue，notify_one  ──>  run(tid): 按 _num_bytes_per_thread*tid 切自己的区间
  -> return 0（async）                      io_submit(...) / io_pgetevents(...)
                                            完成后 push 进本线程 complete_queue
wait(): 从每个线程 complete_queue
  各收一个完成 op（一个逻辑请求
  = 所有线程各干完自己那段）
  -> 检查每个 op 的 _error_code    ──>  有非零（负 errno）=> 聚合消息，最后抛 RuntimeError
  -> finish(): 若用了 bounce buffer，把数据拷回用户张量
```

关键点：**一个逻辑 I/O 请求会被广播到所有 AIO 线程**，每个线程只做属于自己的连续区间；`wait()` 必须从每个线程各收一次完成才算这个请求结束。

#### 4.2.3 源码精读

构造函数 [deepspeed_py_io_handle.cpp:L41-L61](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L41-L61)：创建 `intra_op_parallelism` 个 `deepspeed_aio_thread_t` 并逐个 `std::thread` 启动；每个线程上下文含独立的 `aio_context`（iocb 数组 + `io_queue_init`）。

区间均分发生在请求描述符构造处 [deepspeed_aio_op_desc.cpp:L50-L69](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_aio_op_desc.cpp#L50-L69)：`_num_bytes_per_thread = buffer.nbytes() / intra_op_parallelism`。这也是句柄入口 [`_is_valid_parallel_aio_op`（deepspeed_py_io_handle.cpp:L268-L279）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L268-L279) 要求总字节数必须能被线程数整除的原因——除不尽的话最后一个线程的文件偏移就不再对齐。

工作线程主循环 [deepspeed_aio_thread.cpp:L24-L51](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_aio_thread.cpp#L24-L51)：条件变量等待 work_queue → `next_io_op->run(tid, ...)` → 推入 complete_queue 并唤醒等待者。`run()` 的实现（[deepspeed_cpu_op.cpp:L73-L89](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_cpu_op.cpp#L73-L89)）按 `tid` 算出自己的缓冲区基址与文件偏移，再走重叠或顺序两种算法。

libaio 的核心流水在 [deepspeed_aio_common.cpp:L224-L292](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L224-L292) 的 `do_aio_operation_overlap`：循环里「准备至多 queue_depth 个 iocb → 提交 → 只等至少 1 个完成 → 从完成数里扣减在途数」，submit 与 reap 交叠，把队列始终保持有活干；顺序模式 [`do_aio_operation_sequential`（L157-L222）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L157-L222) 则是「提交满批 → 全部等完」两段式。

提交与收割的错误捕获（本周期改造的重点之一）：

- [`_do_io_submit_singles` / `_do_io_submit_block`（deepspeed_aio_common.cpp:L72-L121）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L72-L121)：`io_submit` 返回值不等于提交数时，把负的返回值（`-errno`）或 `-EIO` 记入 `error_code` 并停止继续提交——旧版代码在这里是 `assert`，进程直接崩溃。
- [`_do_io_complete`（deepspeed_aio_common.cpp:L123-L155）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L123-L155)：`io_pgetevents` 返回 `-EINTR`（被信号打断，例如 dataloader 子进程的 SIGCHLD）就在 `do...while` 里重试；随后**逐个检查完成事件的 `res`**：`res` 为负是内核给出的 errno，`res` 为正但小于请求字节数是「短传输」，两种情况都会记入 `error_code`。旧版只看完成了几个事件、不看每个事件的 `res`——这正是「静默失败」的第一现场。

`aio_context` 的初始化 [deepspeed_aio_types.cpp:L61-L78](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_types.cpp#L61-L78)：`io_queue_init` 失败（例如 `/proc/sys/fs/aio-max-nr` 耗尽）时释放已分配的 iocb 并抛出带 `strerror` 与排查提示的 `runtime_error`——旧版同样是对返回值 `assert`。

Python 侧的可见入口：`pread` 的前置校验（文件大小必须覆盖读取范围、字节数可被线程数整除、`open_file` 失败即返回 -1）在 [deepspeed_py_io_handle.cpp:L315-L331](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L315-L331)；`sync_pread/sync_pwrite` 只是 `async=false` 的别名（[L365-L377](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L365-L377)），即「提交后立刻 `wait()`」，因此 4.3 节的错误抛出对同步接口同样生效。`O_DIRECT` 打开文件的实现见 [deepspeed_aio_common.cpp:L301-L317](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L301-L317)（CANN 后端例外地不用 O_DIRECT）。

编译与加载：[`AsyncIOBuilder`（op_builder/async_io.py:L23-L35）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/op_builder/async_io.py#L23-L35) 列出上述全部 `.cpp`；[`load()`（L98-L103）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/op_builder/async_io.py#L98-L103) 先加载 `pin_memory` 算子再加载本体（pinned 内存管理器跨 .so 共享）；[`is_compatible()`（L105-L122）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/op_builder/async_io.py#L105-L122) 用「编译一个调用 `io_submit` 的小程序」来探测 libaio 是否可用，并给出 `apt install libaio-dev` 之类的提示。

#### 4.2.4 代码实践

**实践目标**：亲手用 `aio_handle` 完成一次异步写 + 读回，观察 `wait()` 的返回值语义。

1. 确认 `ds_report` 输出中 `async_io ... installed` 或 `compatible`（不兼容则先 `apt install libaio-dev`）。
2. 准备一个可写的 NVMe/SSD 目录，例如 `/local_nvme`。以下为**示例代码**（依据官方教程 [docs/_tutorials/deepnvme.md](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/docs/_tutorials/deepnvme.md) 的写法改编）：

```python
import os, torch
from deepspeed.ops.op_builder import AsyncIOBuilder

h = AsyncIOBuilder().load().aio_handle(
    block_size=1048576, queue_depth=8, single_submit=False,
    overlap_events=True, intra_op_parallelism=1)
t = torch.arange(1 << 20, dtype=torch.uint8)          # 1 MB
print(h.async_pwrite(t, '/local_nvme/demo.pt'))       # 期望打印 0（已入队）
print(h.wait())                                       # 期望打印 1（1 个请求完成）
t2 = torch.empty_like(t)
print(h.sync_pread(t2, '/local_nvme/demo.pt'))        # 期望打印 1（同步完成 1 个请求）
assert torch.equal(t, t2)
```

3. 把 `intra_op_parallelism` 改成 4 再跑一次（此时缓冲会被 4 个线程均分，注意总字节数必须能被 4 整除）。

**需要观察的现象**：`async_pwrite` 立即返回 0；`wait()` 返回的是「完成的逻辑请求数」（这里是 1），而不是字节数或 iocb 数。

**预期结果**：读回的张量与原张量逐元素相等。若在只有 CPU 的机器上，JIT 编译 CPU 版算子后同样可运行（GPU 张量路径除外）——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`overlap_events=true` 与 `single_submit=true` 组合在什么场景下有意义？
**答案**：`single_submit` 逐个提交 iocb、`overlap_events` 让提交与收割交替。当单个块很大（如 8 MB）而队列深时，「提交一块就等它」的细粒度交叠可以降低内存占用；当块很小时整批提交（`single_submit=false`）摊薄系统调用开销更好。这正是调优工具（4.5 节）把这几个参数放进搜索空间的原因。

**练习 2**：为什么 `_wait_for_aio_work` 要从**每个**线程的完成队列各弹出一个，而不是随便找一个非空队列？
**答案**：一个逻辑请求被广播给所有线程（[`_schedule_aio_work_locked`，deepspeed_py_io_handle.cpp:L184-L195](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L184-L195)），每个线程都会往自己的完成队列推同一个 op 的完成记录。只收一个线程的记录就宣布请求完成，会把其他线程仍在写的缓冲提前交给用户，产生数据竞争。逐线程收取（[L197-L208](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L197-L208)）保证所有分片都已落盘/读满。

**练习 3**：`wait()` 返回值的类型是 `int`，失败信息却通过异常传递。为什么不把错误作为返回值？
**答案**：一个 `wait()` 可能收割多个请求，可能多个都失败；返回值已被「完成数」占用。C++ 侧把每个请求的负 errno 聚合成一条人可读的消息（读/写、文件名、`strerror`）后抛 `RuntimeError`（[deepspeed_py_io_handle.cpp:L229-L266](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L229-L266)），Python 侧调用点（如 `swap_in_tensors` 的 `assert` 链）语义保持「返回值 = 成功完成数」，失败路径统一走异常，避免每个调用点都检查双重返回语义。

### 4.3 I/O 结果检查与错误传播：让失败不再「装成功」

#### 4.3.1 概念说明

本周期之前，DeepNVMe 有一个危险的性格：**内核拒绝的请求会被当作完成**。三类典型场景（PR #8985 在 GH200 + ext4/md RAID 上实测）：

1. 写超出文件大小限制（RLIMIT_FSIZE）：内核只写了 4096 字节中的 2048 字节，DeepSpeed 返回「成功」；
2. 往只读 fd 上写：`io_submit` 直接返回 `-EBADF`，旧代码 `assert` 崩溃，而非报错；
3. 未对齐缓冲（下一节详述）：内核把请求整个判为 `-EINVAL`，什么都没搬，上层照常「完成」。

后果是 swap-in 读回陈旧数据、swap-out 什么都没写，训练带着错误的权重和梯度继续跑——**比崩溃更糟**。修复的原则是：先让所有在途请求跑完（保证句柄可复用），再把第一个错误用 `RuntimeError` 抛给 Python。

#### 4.3.2 核心流程

错误码的生命周期：

```text
内核（io_submit 返回 -EBADF / 完成事件 res = -EINVAL 或短传输）
  -> error_code（负 errno，submit/complete helper 记录，C++ 引用传出）
  -> cpu_op_desc_t::run() 末尾 op->set_error(error_code)      # std::atomic CAS，多线程只记第一个
  -> wait()/_wait_locked()：先收完所有在途 op（句柄可复用），
     读每个 op 的 _error_code -> 拼消息 "read from <file> failed: <strerror>"
  -> throw std::runtime_error("deepspeed_aio: " + 消息)  =>  Python 端 RuntimeError
```

#### 4.3.3 源码精读

`_wait_locked` 全景在 [deepspeed_py_io_handle.cpp:L229-L266](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L229-L266)，四处关键设计：

- **收完再抛**（L236-L261）：`while (_num_pending_ops > 0)` 循环把每个在途请求都执行 `validate/finish/close fd`，即使第一个已经失败——注释写明「也让句柄之后还能用」；
- **错误消息构造**（L250-L257）：取第一个非零 `error_code`，拼出方向（read/write）、文件名（或 fd 号）与 `strerror(-error_code)`；
- **双重抛出**（L263-L264）：I/O 错误抛 `runtime_error`；若 `finish()`（bounce buffer 回拷，可能触发 CUDA 拷贝错误）抛过异常，则用 `std::rethrow_exception` 原样重抛。

原子记错在 [deepspeed_aio_op_desc.cpp:L71-L75](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_aio_op_desc.cpp#L71-L75)：`_error_code` 是 `std::atomic<int>`（声明见 [deepspeed_aio_op_desc.h:L26](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_aio_op_desc.h#L26)），`compare_exchange_strong(0, err)` 保证多个 AIO 线程并发报错时只保留**第一个**错误——后到的错误不覆盖先到的，因为先发的错误更接近根因。

「短传输」检测的技巧在 [`_do_io_complete`（deepspeed_aio_common.cpp:L144-L153）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L144-L153)：提交 iocb 时把「请求字节数」存进 `event.data`，完成时与 `event.res` 比较——不等就是失败（负数=errno，正数=短写）。注释特别说明为什么不能事后查 iocb 本体：iocb 可能已被后续请求复用。

对应测试是 [tests/unit/v1/nvme/test_aio.py:L481-L548](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/nvme/test_aio.py#L481-L548) 的 `TestFailedIO`，三个用例分别覆盖「提交时被拒」「完成时短写」「上下文创建失败」：

- [`test_rejected_write`（L499-L512）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/nvme/test_aio.py#L499-L512)：用 `os.open(..., O_RDONLY)` 拿只读 fd 发起 `async_pwrite`，断言 `h.wait()` 抛出的消息含 `"Bad file descriptor"`；
- [`test_failed_write`（L514-L533）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/nvme/test_aio.py#L514-L533)：用 `resource.setrlimit(RLIMIT_FSIZE, IO_SIZE//2)` 把文件大小上限压到缓冲一半、忽略 SIGXFSZ，断言同步写抛 `"File too large"`；
- [`test_failed_context`（L535-L548）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/nvme/test_aio.py#L535-L548)：`aio_read/aio_write` 传 queue_depth=0，断言抛 `"io_queue_init"` 且没有泄漏 fd（对比 `/proc/self/fd` 前后数量）。

三个用例末尾都调用 `_check_read` 复用同一句柄再读一次文件——**验证「抛错之后句柄仍可用」这条契约**。另注意 [L472-L478](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/nvme/test_aio.py#L472-L478) 的 `_get_io_error` 注释：不用 `pytest.raises` 是因为其失败异常无法 pickle，会让分布式测试挂死而非失败。

#### 4.3.4 代码实践

**实践目标**：亲手注入一次 I/O 错误，观察错误消息与句柄恢复。

1. 阅读 `TestFailedIO` 三个用例（上文行号），画出每个用例中错误码的传播路径：内核 → helper 的 `error_code` → `set_error` → `_wait_locked` → Python 异常。
2. 把 4.2.4 的示例脚本改成错误注入版（**示例代码**）：

```python
import os, torch, resource, signal
from deepspeed.ops.op_builder import AsyncIOBuilder

h = AsyncIOBuilder().load().aio_handle(1048576, 8, False, True, 1)
t = torch.full((1 << 20, ), 7, dtype=torch.uint8)
f = '/local_nvme/err.pt'
h.sync_pwrite(t, f, 0)                     # 先正常写入 1 MB

limits = resource.getrlimit(resource.RLIMIT_FSIZE)
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 19, limits[1]))  # 上限压到 512 KB
try:
    h.sync_pwrite(t, f, 0)
except RuntimeError as e:
    print('caught:', e)                    # 期望包含 "File too large"
finally:
    resource.setrlimit(resource.RLIMIT_FSIZE, limits)
t2 = torch.empty_like(t)
print(h.sync_pread(t2, f, 0))              # 期望仍可用，返回 1
```

**需要观察的现象**：异常消息形如 `deepspeed_aio: write to /local_nvme/err.pt failed: File too large`；恢复 rlimit 后同一句柄仍能正常读。

**预期结果**：修复前的代码在这两步都不会报错（第一步「装成功」只写了一半），第二步照常返回；修复后立即抛错且句柄可复用。本实践需要真实 Linux + libaio 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `set_error` 用 `compare_exchange_strong` 而不是直接赋值？
**答案**：一个请求广播给多个 AIO 线程并行执行，可能多个线程同时失败。CAS(0→err) 保证只有第一个写进去，后来的失败不会覆盖。保留「第一个」错误是因为多线程的分片按顺序提交，最早的失败往往指示根因（如 fd 已失效），后面的错误常是连带症状。

**练习 2**：`_wait_locked` 为什么要「先收完所有请求再抛」？如果收到第一个错误立即抛会怎样？
**答案**：抛出时 `_num_pending_ops` 必须归零、每个已打开的 fd 必须关闭，否则句柄内部状态残缺，下一次使用要么断言失败要么泄漏 fd。`TestFailedIO` 每个用例结尾的 `_check_read` 正是在验证这条「抛错后句柄仍可用」的契约；`test_failed_context` 更是直接断言 `/proc/self/fd` 数量不变（无泄漏）。

**练习 3**：`io_pgetevents` 返回 `-EINTR` 时旧代码会怎样？新代码为什么选择重试？
**答案**：旧代码对其结果 `assert(n_completes >= min_completes)`，`-EINTR`（如 dataloader worker 退出引发的 SIGCHLD 打断等待）会直接断言崩溃整个训练进程。`-EINTR` 的语义只是「被信号打断，请重试」，请求本身并未失败，因此 [deepspeed_aio_common.cpp:L130-L140](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L130-L140) 用 `do...while (n_completes == -EINTR)` 循环重试即可正确恢复。

### 4.4 对齐检测与 bounce buffer：O_DIRECT 的几何约束

#### 4.4.1 概念说明

`O_DIRECT` 要求**缓冲区起始地址、传输长度、文件偏移**三者都按设备要求的粒度对齐。问题在于：

- **ZeRO-Infinity 交换的全是「视图」**：扁平大缓冲（flat buffer）按 offset/length 切出来的分区，offset 常常不是 512 的倍数（例如 416 字节之后）；
- **对齐粒度因设备而异**：裸 NVMe 常只需 4 字节，而 md RAID / LVM 上是 512；Linux 6.1 起可以用 `statx(STATX_DIOALIGN)` 查询每个文件的真实要求，更老的内核只能按 512 保守假设；
- **strided 视图根本不连续**：`base.view(n,2)[:,0]` 每隔一个字节取一个，DMA 无法描述。

修复策略分两层：

1. **C++ 层自动兜底（bounce buffer）**：构造请求时判定「是否 CPU pinned + 连续 + 地址对齐」，不满足就分配一块满足条件的中间缓冲——写方向先把数据拷进去再从它发起 I/O，读方向 I/O 落到中间缓冲、完成后拷回用户张量（还能跨设备：CUDA/XPU/CPU/NPU）。
2. **Python 层主动补偿（padded staging / 长度拆分）**：交换器把交换分区的元素数向上补齐（4.1.2 的公式），优化器把不对齐的梯度段拆成两截（4.1.3）；fast init 读取时若分区字节数不是 `aligned_bytes` 的倍数，就按**文件的真实大小**（补齐后）分配 staging 张量读整块，再 `narrow` 出需要的部分。

为什么修复前是「静默丢数据」：未对齐的请求被内核整个判为 `-EINVAL`（`res` 为负），而旧代码不看 `res`——于是「什么都没搬，报告成功」。在 md RAID 上实测：ZeRO-3 参数卸载 Qwen2.5-0.5B，290 个参数中 60 个在初始化后就是错的。

#### 4.4.2 核心流程

```text
cpu_op_desc_t 构造（每个请求一次）:
    eligible = 是 CPU 张量 且 (torch pinned 或 DeepNVMe 管理的页锁定)
               且 contiguous 且 data_ptr % 对齐(fd) == 0
    if not eligible:
        分配 pinned bounce buffer（视图形状与原张量一致）
        if 写请求: 先把原张量拷进 bounce buffer
    I/O 始终作用在（bounce 或原）缓冲上
finish()（读请求完成后）:
    if 用了 bounce buffer:
        把 bounce buffer 拷回原张量（按其设备选择 CUDA non_blocking / XPU / CPU / NPU）
        释放 bounce buffer
```

对齐粒度探测：

\[ \text{align}(fd) = \begin{cases} 1, & \text{非 } O\_DIRECT \text{ 文件} \\ \text{statx.dio\_mem\_align}, & \text{Linux 6.1+ 且 STATX\_DIOALIGN 可用} \\ 512, & \text{其他（保守缺省）} \end{cases} \]

#### 4.4.3 源码精读

判定与准备在 [`cpu_op_desc_t` 构造函数（deepspeed_cpu_op.cpp:L11-L39）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_cpu_op.cpp#L11-L39)：三个条件的与运算（页锁定 CPU 内存、连续、地址按文件所需对齐取模为零）决定 `_use_bounce_buffer`；注释明确写出「视图可能不连续、或起始地址不满足文件所需对齐，内核会拒绝这样的 I/O，或数据被读进一份临时拷贝」。

读完成后的回填在 [`finish()`（deepspeed_cpu_op.cpp:L43-L65）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_cpu_op.cpp#L43-L65)：按原张量设备分支拷回——CUDA 用 `non_blocking=true`（页锁定源允许真异步）、XPU/CPU 直接拷、CANN 构建下走 NPU。bounce buffer 的分配在 [L91-L107](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_cpu_op.cpp#L91-L107)：优先 DeepNVMe 自己的 pinned 管理器（非 CUDA 构建时），否则 `torch.empty(...).pin_memory()`，并 `view` 回原形状（注释：来回拷贝需要保持形状）。

对齐探测函数 [`get_io_memory_alignment`（deepspeed_aio_common.cpp:L319-L335）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/common/deepspeed_aio_common.cpp#L319-L335)：非 `O_DIRECT` 返回 1（无约束）；Linux 6.1+ 用 `statx(AT_EMPTY_PATH, STATX_DIOALIGN)` 读 `stx_dio_mem_align`（注释：裸 NVMe 常为 4、md RAID 与 LVM 为 512）；老内核保守返回 512。句柄上还暴露了 [`get_alignment() = intra_op × 512`（deepspeed_py_io_handle.cpp:L94-L97）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L94-L97)——多线程均分后每个线程的起始地址还要差一个 `intra_op` 倍数，供 `deepspeed/io/` 下的数据加载缓冲使用。

「是否 pinned」的判定在 [`is_pinned`（deepspeed_py_io_handle.cpp:L416-L423）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/aio/py_lib/deepspeed_py_io_handle.cpp#L416-L423)：`torch 的 is_pinned()` **或** DeepNVMe pinned 管理器登记过都算——只查管理器会把 torch 自己 pin 的张量（如 ZeRO-3 的扁平 CPU 内存）误判为非 pinned，白白多一次 bounce 拷贝。

fast init 的 padded staging 读取在 [`_swap_in_fp16_params`（optimizer_utils.py:L336-L382）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/optimizer_utils.py#L336-L382)：从 NVMe 上的 fp16 参数分区构造 fp32 优化器状态时，若某分区的字节数不是 `aligned_bytes` 的整数倍（[L358-L364](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/optimizer_utils.py#L358-L364)），就按文件实际大小 `os.path.getsize` 分配一块**补齐后的** pinned staging 张量、把整块读进来；读完后在 [L378-L380](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/optimizer_utils.py#L378-L380) `narrow(0, 0, dst.numel())` 拷出真实部分并立刻释放 staging。注释一语中的：「直接 I/O 读的是整块：读交换文件里存着的补齐分区，再把分区从里面拷出来」。

整个 fast init 的双池流水在 [`_initialize_from_swapped_fp16_params`（optimizer_utils.py:L292-L334）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/optimizer_utils.py#L292-L334)：fp16 池负责从参数 swap 文件读入，fp32 池负责把转换后的 fp32 值写进优化器状态 swap 文件，两池各自装满一批就换出、重置、继续——读与写在不同缓冲池上并行推进，这就是「pipelined swapper」名字的由来（流水线化的完整实现见同目录 `pipelined_optimizer_swapper.py`，其读/写调度正是复用这里的基类设施）。

未对齐视图的回归测试在 [`TestBufferView`（tests/unit/v1/nvme/test_aio.py:L427-L469）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/nvme/test_aio.py#L427-L469)，构造函数 [`_get_buffer_view`（L418-L424）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/nvme/test_aio.py#L418-L424) 的注释直指要害：「就像 ZeRO-Infinity 交换扁平缓冲的分区那样：从对齐地址偏移一个字节，任何 O_DIRECT 对齐都不满足；或者每隔一个字节取一个」。三种 layout——`offset`（错 1 字节的一维视图）、`offset_2d`（错 1 字节的二维视图）、`strided`（隔字节取的跨步视图）——分别对读和写断言数据逐字节一致。

#### 4.4.4 代码实践

**实践目标**：用三种「坏视图」复现 bounce buffer 路径，并解释为什么修复前它们会静默丢数据。

1. 阅读 `_get_buffer_view` 与 `TestBufferView`（行号见上），列出每个 layout 分别违反了对齐三要素（地址/长度/连续性）中的哪一条。
2. 复刻实验（**示例代码**，基于 TestBufferView 改编）：

```python
import torch, os
from deepspeed.ops.op_builder import AsyncIOBuilder

h = AsyncIOBuilder().load().aio_handle(1048576, 8, False, True, 1)
base = h.new_cpu_locked_tensor(2 << 20, torch.empty(0, dtype=torch.uint8))
view = base.narrow(0, 1, 1 << 20)            # 偏移 1 字节：地址未对齐
ref = torch.randint(0, 256, (1 << 20, ), dtype=torch.uint8)

view.copy_(ref)
assert h.sync_pwrite(view, '/local_nvme/view.pt', 0) == 1
view.zero_()
assert h.sync_pread(view, '/local_nvme/view.pt', 0) == 1
assert torch.equal(view, ref)               # bounce buffer 保证逐字节往返一致
h.free_cpu_locked_tensor(base)
```

3. 对照思考：把 `view` 换成 `base.view(1 << 20, 2)[:, 0]`（strided），`_cpu_buffer.contiguous()`（构造函数 L38）会把数据打包成连续副本，I/O 落在副本上——找出这条 `contiguous()` 调用并说明它在 strided 场景下的作用。

**需要观察的现象**：偏移 1 字节的视图读写完全正确（自动走了 bounce buffer）；用 `strace -e io_submit` 之类工具观察时（可选），实际提交的缓冲区地址与 `view.data_ptr()` 不同。

**预期结果**：三种 layout 的读写都逐字节一致；修复前同样的代码在 md RAID/LVM 或老内核上会「成功返回但数据没动」。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么写请求要在**构造时**就把数据拷进 bounce buffer，而读请求在 `finish()` 时才拷回？
**答案**：I/O 的数据源必须在其发起前就位——写请求若不先拷贝，内核读到的原缓冲在异步窗口内可能被改写（教程也警告 async 写期间改源张量是数据竞争）。读方向相反：数据在 I/O 完成后才存在于 bounce buffer 里，所以回填放在 `finish()`，且可以按目标设备用 `non_blocking` 拷贝。

**练习 2**：`get_io_memory_alignment` 为什么对非 `O_DIRECT` 文件返回 1？
**答案**：带缓冲的普通 I/O 由内核 page cache 中转，用户缓冲没有对齐要求，任何地址都「对齐」（模 1 恒为零）。DeepNVMe 打开文件一律带 `O_DIRECT`（CANN 后端除外），这个分支主要防御测试或特殊构建下混合使用的场景。

**练习 3**：fast init 里为什么 staging 张量的大小取 `os.path.getsize(partition_path)`，而不是 `dst_tensor.numel()`？
**答案**：交换文件里存的是**补齐后**的分区（4.1.2 的对齐公式），文件大小 ≥ 真实数据长度。直接 I/O 必须按整块读，如果按 `dst.numel()` 分配就会短读（文件剩余部分读不进来，且长度本身也不对齐）。读完整块后 `narrow(0, 0, dst.numel())` 才能取出真实数据。这正是修复前 fast init 在非对齐长度分区上失败的原因。

### 4.5 NVMe 探测与调优工具：ds_nvme_tune

#### 4.5.1 概念说明

`aio` 参数没有普适最优值：磁盘队列深度、文件系统、RAID 布局都影响最优组合。DeepSpeed 提供微基准工具链自动扫参：

- `bin/ds_nvme_tune`（[L1-L8](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/bin/ds_nvme_tune#L1-L8)）：入口，做一次参数扫描并直接生成最优配置建议；
- `deepspeed/nvme/perf_run_sweep.py`：扫描调度，默认搜索空间为 block_size ∈ {1M, 8M}、queue_depth ∈ {32, 128}、sequential_requests、single_submit、io_parallel ∈ {1,2,4,8}（[L22-L28](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/perf_run_sweep.py#L22-L28)）；
- `bin/ds_io` → `deepspeed/nvme/io_engine.py`：单点微基准，按 `--engine` 选择 aio_handle / basic / torch_io / torch_fastio 四种后端，用 `multiprocessing.Pool` + `Barrier` 让多进程同时打满磁盘（[io_engine_tasklet，io_engine.py:L69-L111](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/io_engine.py#L69-L111)）；
- `deepspeed/nvme/parse_nvme_stats.py`：解析扫描日志。

#### 4.5.2 核心流程

```text
ds_nvme_tune --nvme_dir /local_nvme [/local_nvme2 ...]
  -> perf_run_sweep.sweep_main: 对搜索空间笛卡尔积的每一组参数
       生成 ds_io 命令（按 t{线程}_d{深度}_bs{大小} 命名日志与目录）
       每组跑 read 与 write 微基准（可 --no_read/--no_write）
  -> 每个日志里写入若干行，关键行形如：
       E2E Read Speed = 12.32 GB/sec
  -> perf_generate_param.generate_main: 汇总出建议的 aio 参数
  -> （也可以手动）parse_nvme_stats.py --log_dir <目录> --metric read_speed|write_speed
       按文件名解析出 (线程数, 深度, 块大小, ...) 键，打印每组的端到端速度
```

#### 4.5.3 源码精读

结果解析的输出契约在 [parse_nvme_stats.py:L76-L100](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/parse_nvme_stats.py#L76-L100)：文档串给出日志样例行（`Task Read Latency = ... sec` / `E2E Read Speed = ... GB/sec`），`--metric read_speed` 对应抓取 `E2E Read Speed` 行、取倒数第二个字段（数值）；文件名到参数键的还原靠 [`extract_value`（L36-L57）](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/parse_nvme_stats.py#L36-L57)：`t4`→线程 4、`d128`→深度 128、`bs1M`→块 1 MB。

扫描配置在 [perf_run_sweep.py:L66-L111](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/perf_run_sweep.py#L66-L111)：`--nvme_dir` 可给多个目录（每个目录绑定一个进程编号并发压测）、`--io_size`（默认 400M）、`--gpu`（测 GPU↔NVMe）、`--flush_page_cache`（需要 sudo，否则读速度可能虚高）。注意 [io_engine.py:L119-L126](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/io_engine.py#L119-L126) 的 `io_engine_multiprocessing`：多进程各跑 `io_engine_tasklet`，用 `Barrier` 对齐每轮起止，汇总后 `report_results` 输出延迟与速度——这保证测出的是**多并发**下的聚合带宽，而不是单进程串行值。

#### 4.5.4 代码实践

**实践目标**：对自己的 NVMe/SSD 做一次 aio 参数扫描，选出最优组合并核对解析工具的输出格式。

1. 准备一个**可写、空间充足**的 scratch 目录（如 `/local_nvme`；切勿指向系统盘的重要数据，写基准会产生大量写入）。
2. 运行 `ds_nvme_tune --nvme_dir /local_nvme`（即 `python bin/ds_nvme_tune --nvme_dir /local_nvme`；需要 async_io 算子可用）。工具会按默认搜索空间（2×2×2×2×4 = 64 组合 × 读写两个方向）逐组测速。
3. 扫描完成后用解析工具查看矩阵：`python deepspeed/nvme/parse_nvme_stats.py --log_dir <输出的日志目录> --metric read_speed`，再跑一次 `--metric write_speed`。
4. 对照 [parse_nvme_stats.py:L78-L84](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/parse_nvme_stats.py#L78-L84) 的样例行格式，确认输出形如 `(4, 128, 1048576, ...) = 6.5`（键为解析后的参数元组，值为 GB/sec）。
5. 把最优组合写进训练配置的 `"aio"` 块（`block_size`/`queue_depth`/`single_submit`/`overlap_events`/`intra_op_parallelism`）。

**需要观察的现象**：读速度随 queue_depth 与 io_parallel 增大先升后平台；block_size 1M vs 8M 差异明显；未刷 page cache 时读速度可能远超磁盘标称值（提示信息见 `--flush_page_cache` 的帮助文本）。

**预期结果**：得到一组明确的 `aio` 推荐参数；`ds_nvme_tune` 的 `generate_main` 会直接打印建议值。具体数值因硬件而异，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么微基准要用多进程 + Barrier，而不是单进程循环测？
**答案**：单进程顺序 I/O 无法打满现代 NVMe 的并行队列，测出的带宽远低于训练时多线程/多 rank 并发换出的真实压力。`io_engine_multiprocessing` 用进程池让每个目录一个进程、`Barrier` 对齐每轮起止（[io_engine.py:L69-L111](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/io_engine.py#L69-L111)），计时覆盖整轮并发窗口，得到的是聚合带宽；`e2e_latency_sec` 与 `task_latency_sec` 的差还能反映进程间同步开销。

**练习 2**：`parse_nvme_stats.py` 怎么知道每个日志对应哪组参数？
**答案**：靠文件名编码。`get_file_key` 把文件名按 `_` 拆开逐段 `extract_value`：`t` 前缀→线程数、`d`→深度、`bs1M`→块大小（K/M 后缀换算成字节），拼成元组作为结果的键（[parse_nvme_stats.py:L60-L73](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/parse_nvme_stats.py#L60-L73)）。这也是第 4 步实践中输出的元组键的来源。

**练习 3**：`--flush_page_cache` 为什么需要 sudo，不刷对结果有什么影响？
**答案**：读基准若命中 page cache，测的是内存带宽而非磁盘；把页缓存清掉才能强制真正的设备读。清缓存（如写 `/proc/sys/vm/drop_caches`）是全系统特权操作，需要 root。工具在帮助文本里明确警告「不刷则报告的读速度可能高于实际」（[perf_run_sweep.py:L89-L94](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/nvme/perf_run_sweep.py#L89-L94)）。

## 5. 综合实践

**任务：给自己的磁盘做一次 DeepNVMe 体检，并出具一份「为什么必须修 #8985」的复盘报告。**

1. **测速**：按 4.5.4 跑 `ds_nvme_tune --nvme_dir <你的NVMe目录>`，用 `parse_nvme_stats.py` 导出读/写速度矩阵，圈出最优 `aio` 组合；顺手记录磁盘的对齐要求（Linux 6.1+ 可用 `xfs_io -c 'statx -v' <文件>` 或 `statx` 小程序查看 `dio_mem_align`，与 4.4.3 的 `statx` 探测互相印证）。
2. **注入错误**：按 4.3.4 的示例脚本复现「File too large」，记录异常消息格式与句柄恢复行为。
3. **坏视图往返**：按 4.4.4 的示例脚本验证三种 layout（`offset` / `offset_2d` / `strided`）读回一致，并在报告里画一张表：每种 layout 违反了地址/长度/连续性中的哪条、由哪段代码兜底（构造函数的对齐判定 / `contiguous()` / bounce buffer 回拷）。
4. **端到端**：有条件时跑 `pytest tests/unit/v1/zero/test_zero3_nvme_offload.py -v`，确认 NVMe 卸载与 CPU 卸载的 loss 与权重逐位一致。
5. **复盘**：用一段文字回答——在修复前的代码上，第 3 步的偏移视图会发生什么？错误藏在完成事件的哪个字段里？谁本来应该看它而没看？（参考答案：什么都没搬却报告成功；藏在 `io_event.res`（负 errno）与 `res != 请求字节数` 里；`_do_io_complete` 旧版只数事件个数不看 `res`，且 `data` 字段的请求长度技巧当时还没用来做比对。）

无 NVMe/LibaIO 环境时，第 1、4 步可降级为「源码阅读型实践」：跟踪 `_swap_in_fp16_params` 的 staging 分支与 `TestFailedIO`/`TestBufferView` 的断言，写出与上面等价的复盘报告（相关步骤标注「待本地验证」）。

## 6. 本讲小结

- **两级流水**：`AsyncPartitionedParameterSwapper` 管 NVMe ↔ CPU pinned 交换缓冲（有限池 + `AVAILABLE/INFLIGHT/NOT_AVAILABLE` 状态机），参数协调器管 CPU ↔ GPU；优化器状态由 `OptimizerSwapper` 系列负责，fast init 用 fp16/fp32 双缓冲池把「读参数、写优化器状态」流水线化。
- **对齐数学**：交换分区元素数向上补齐到 \(1024 \times P / e\)；交换文件存补齐长度，读回后 `narrow` 切出真实数据；优化器梯度段在 Python 层预先拆分对齐。
- **执行模型**：一个 I/O 请求广播给 `intra_op_parallelism` 个 AIO 线程均分执行，每个线程独立 `aio_context`；`overlap_events` 让 `io_submit` 与 `io_pgetevents` 交叠成流水；`wait()` 返回完成的逻辑请求数。
- **错误不再静默（#8985）**：`io_submit` 失败不再 `assert` 崩溃、完成事件的 `res` 逐个核对（负 errno 或短传输都算失败）、`EINTR` 重试、`io_queue_init` 失败抛错；`wait()`/`sync_pread`/`sync_pwrite` 先收完所有在途请求再抛 `RuntimeError`，句柄保持可复用。
- **对齐兜底（#8985）**：`statx(STATX_DIOALIGN)` 探测每文件对齐（老内核保守 512）；非 pinned/非连续/未对齐的张量自动走 bounce buffer（写前拷入、读后按设备拷回）；fast init 用 padded staging 张量读整块再 `narrow`。
- **调优工具链**：`ds_nvme_tune` 按默认搜索空间（block_size × queue_depth × single_submit × sequential × io_parallel）多进程并发测速，`parse_nvme_stats.py` 按文件名编码还原参数并抓取 `E2E Read/Write Speed`。

## 7. 下一步学习建议

- **u5-l8（ZeRO 检查点与权重导出）**：检查点保存同样经由 NVMe/文件系统写出，`zero_to_fp32` 的重组逻辑与本讲的分区补齐规则一脉相承。
- **阅读 `pipelined_optimizer_swapper.py` 与 `partitioned_optimizer_swapper.py`**：看 fast init 的双池思想如何扩展成完整的读/写流水线调度（`pipeline_read`/`pipeline_write` 配置项）。
- **阅读 `deepspeed/io/`（数据加载侧 DeepNVMe）**：`fast_file_writer.py`、`double_io_buffer.py` 大量使用本讲的 `get_alignment()` 与 double-buffer 模式，是同一套机制的另一个用户。
- **横向对照 GPUDirect Storage**：`csrc/gds` 与 `GDSBuilder`（本讲 4.1.3 的 `use_gds` 分支），理解「缓冲直接放 GPU、绕过 CPU 中转」的另一条路线及其适用条件。
