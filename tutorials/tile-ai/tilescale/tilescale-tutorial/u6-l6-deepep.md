# DeepEP 集成（节点内 Expert Parallel）

## 1. 本讲目标

本讲承接 u6-l4（分布式运行时）与 u6-l5（IPC 张量与 `tilescale_ext` 内存管理），讲清 TileScale 是如何**用 TileLang 重新实现** DeepEP 的节点内（intranode，NVLink 互联）dispatch/combine 通信算子的。读完本讲你应该能够：

- 说清 MoE（Mixture of Experts）中 expert parallel 的 all-to-all 路由问题，理解 DeepEP 为何是 MoE 训练/推理的关键通信原语。
- 看懂 `install_deepep.sh` 如何拉取并构建原始 `deep_ep`（**仅作正确性参考 oracle**），以及它和 TileScale 自己的实现是什么关系。
- 拆解三段式流程：`get_dispatch_layout`（算路由布局）→ `dispatch`（把 token 发往各 expert 所在 rank）→ `combine`（把专家输出 reduce 回原 rank）。
- 把本讲与 u6-l3 的 CP-engine 原语（`T.put_warp`、`T.wait_ge/wait_ne`、`T.st(..., dst_pe=)`）和 u6-l5 的 `tilelang.get_allocator` / `kernel.initialize(allocator=...)` 串起来。

## 2. 前置知识

- **Expert Parallel（EP）/ 专家并行**：MoE 模型里每个 token 由一个 router 选出 top-k 个专家处理，专家数量往往上百。把这么多专家切成若干组、每组放到一张 GPU 上，就是 EP。于是 token 必须跨 GPU 发往「持有目标专家的卡」，算完再收回来——这正是 DeepEP 解决的通信。
- **All-to-All（全交换）**：每张卡既是发送方又是接收方，且要发给所有其他卡，这种通信模式叫 all-to-all。DeepEP 把它做成「带路由信息的不均衡 all-to-all」（每个 token 去向由 `topk_idx` 决定）。
- **dispatch / combine 二段式**：dispatch = 正向把 token「派发」到专家卡；combine = 反向把专家输出「归约」（求和）回原始 token 所在卡。
- **intranode vs internode**：本讲只覆盖 **intranode**（同一节点内，靠 NVLink 互联，低延迟高带宽）；跨节点（RDMA）与低延迟模式尚未实现（见源码 TODO）。
- 阅读本讲前，建议先回顾 u6-l3 的 CP-engine 路线（`put_warp`、远程基址表）与 u6-l5 的对称堆 allocator。

> **一个关键认知（务必先建立）**：本讲的 TileScale 实现位于 `examples/distributed/deepseek_deepep/`，它是**用 TileLang 重写的 DeepEP**，而不是对原始 `deep_ep` 包的封装。原始 `deep_ep`（来自 `3rdparty/DeepEP`）只在测试里被 import 当作**参考实现**，用来逐元素 `assert torch.equal(...)` 校验 TileScale 的正确性并做性能对比。`install_deepep.sh` 装的就是这个「参考 oracle」，不装它，TileScale 自己的 kernel 依然能跑。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/distributed/install_deepep.sh](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/install_deepep.sh) | 安装原始 DeepEP（参考 oracle）：探测 CUDA arch、补 NVSHMEM、修复链接、`setup.py install` |
| [examples/distributed/deepseek_deepep/deepep.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep.md) | TileScale 版 DeepEP 的设计与使用文档（API、数据结构、执行流程） |
| [examples/distributed/deepseek_deepep/buffer.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/buffer.py) | `EPBuffer`：封装 allocator、对称缓冲、counter，暴露 `get_dispatch_layout/dispatch/combine` |
| [examples/distributed/deepseek_deepep/deepep_utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep_utils.py) | `Config`（调参表）、`gen_inputs`、`ep_bench`、`wait_for_counters_ready`（内联 C++） |
| [examples/distributed/deepseek_deepep/intranode/get_dispatch_layout.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/get_dispatch_layout.py) | 路由布局计算：`get_dispatch_layout` 函数 + kernel（**非分布式**，单卡一次扫完） |
| [examples/distributed/deepseek_deepep/intranode/dispatch.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py) | notify + 主 dispatch kernel、cached 变体、host 编排 `intranode_dispatch` |
| [examples/distributed/deepseek_deepep/intranode/combine.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py) | notify + 主 combine kernel、host 编排 `intranode_combine` |
| [examples/distributed/deepseek_deepep/intranode/example_intranode.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/example_intranode.py) | 端到端测试 + 基准：对照 `deep_ep.Buffer` 校验三段流程，再比 dispatch/combine 延迟与带宽 |
| [tilelang/distributed/utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py) | `init_dist`（torchrun 进程组）、`create_mapped_tensor`（host↔device 映射的 counter） |

---

## 4. 核心概念与源码讲解

### 4.1 DeepEP 背景与 MoE all-to-all 路由

#### 4.1.1 概念说明

DeepEP 是 DeepSeek 开源的「专为 MoE 设计的高吞吐 all-to-all 通信库」。要理解它解决什么问题，先看 MoE 一次前向的数据流：

1. 每个 token 经 router 得到一个 `topk_idx`（选中哪些专家）和 `topk_weights`（各专家权重）。
2. 专家被均匀切成 `num_ranks` 份，每张卡持有 `num_experts / num_ranks` 个本地专家。
3. 一个 token 选中的 top-k 专家很可能**不全在本卡**，所以必须把 token 拷贝到目标卡（dispatch）。
4. 目标卡上的本地专家处理完后，结果要**按 token 求和归约**回原始卡（combine），再乘以权重聚合。

难点在于：这是一次**完全由路由决定去向、且各卡收发量不均衡**的 all-to-all。NCCL 的集合通信原语（all-reduce/all-gather）表达不了「每个 token 按自己的 topk_idx 决定去哪」这种数据相关通信。DeepEP 的做法是：先在卡上算出路由布局，再用「通道（channel）+ 环形缓冲 + 生产-消费队列」把不均衡 all-to-all 拆成可控的、可重叠的远程拷贝。

#### 4.1.2 核心流程

一次完整的 EP 通信（以 intranode 为例）分三步：

```text
[所有 rank 各跑一份同一程序，SPMD]
 ┌─────────────────────────────────────────────────────────────┐
 │ 1. get_dispatch_layout(topk_idx)                            │
 │    每张卡本地算出：                                          │
 │      num_tokens_per_rank[dst]   : 我要发往 dst 卡几个 token  │
 │      num_tokens_per_expert[e]   : 专家 e 收到几个 token      │
 │      is_token_in_rank[tok,dst]  : token tok 是否要去 dst 卡  │
 ├─────────────────────────────────────────────────────────────┤
 │ 2. dispatch(x, ...)            ←  notify + dispatch_kernel  │
 │    notify: 跨卡 all-to-all 交换计数，算前缀和，得到写偏移     │
 │    kernel: 发送方 put_warp 把 x/src_idx/topk 按通道推远端环  │
 │            接收方轮询队头/队尾，拼出 recv_x 等                │
 │    返回 (recv_x, recv_topk_idx, recv_topk_weights, handle)   │
 ├─────────────────────────────────────────────────────────────┤
 │ （本地专家对 recv_x 做前向，得 expert_out）                   │
 ├─────────────────────────────────────────────────────────────┤
 │ 3. combine(expert_out, handle, topk_weights)                 │
 │    反方向：把专家输出发回原卡，按 token 求和（不带权重）        │
 │    返回 (reduced_x, reduced_topk_weights)                    │
 └─────────────────────────────────────────────────────────────┘
```

通信带宽度量按 dispatch 阶段接收到的 BF16 字节数计算（见 [example_intranode.py:207-214](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/example_intranode.py#L207-L214)）：

\[
\text{bandwidth (GB/s)} = \frac{\text{recv\_x.numel()} \times 2}{\text{dispatch\_time (ms)} \times 10^{6}}
\]

官方基准（8×H100 NVL，32 experts，hidden=7168，4096 tokens，见 [deepep.md:12-22](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep.md#L12-L22)）：TileScale dispatch ≈ 1.07ms/308GB/s，combine ≈ 1.08ms/307GB/s，与原版 DeepEP 基本持平。但需注意实现范围仍是 **intranode only**（[deepep.md:5-8](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep.md#L5-L8)）：internode 与 low-latency 模式尚未实现。

#### 4.1.3 源码精读

整个 EP 流程对外的统一封装是 `EPBuffer`。它在构造时做三件事：建分布式进程组、用 `tilelang.get_allocator` 申请对称堆、预分配所有对称缓冲与计数器：

[examples/distributed/deepseek_deepep/buffer.py:30-82](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/buffer.py#L30-L82) —— `EPBuffer.__init__`：断言 `num_experts % num_ranks == 0`、`num_ranks <= 8`（intranode 限制），创建 `tilelang.get_allocator(is_distributed=True, ...)`（即 u6-l5 讲过的「一次 `cudaMalloc` + IPC 交换 handle 拼出远程基址表」的 allocator），再预分配对称缓冲与 host↔device 映射的 MoE 计数器。

关键的两条不变量（贯穿后续所有 kernel）：
- **专家在 rank 间均匀切分**：`num_local_experts = num_experts // num_ranks`，全局专家号 `e` 落在 rank `e // num_local_experts`。
- **通道数 = SM 数 / 2**：`num_channels = num_sms // 2`，每条通道配一个发送 SM + 一个接收 SM（见 [buffer.py:168-176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/buffer.py#L168-L176)）。

#### 4.1.4 代码实践

**目标**：建立「topk_idx → 去向」的直觉，为读 kernel 做准备。

**步骤**：
1. 打开 [deepep_utils.py:127-156](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep_utils.py#L127-L156) 的 `gen_inputs`，读懂它如何造 `topk_idx`（对随机 scores 取 topk）和 `rank_idx = topk_idx // (num_experts // num_ranks)`。
2. 在本地用单进程模拟：`num_experts=8, num_ranks=4`，则 `num_local_experts=2`。专家 0、1 在 rank0；2、3 在 rank1；…
3. 构造一个 `topk_idx = [[0, 3], [5, 7]]`（2 个 token 各选 2 个专家），手算每个 token 应被发往哪些 rank。

**需要观察的现象 / 预期结果**：
- token 0 选专家 {0,3} → 要去 rank0（持有专家 0）和 rank1（持有专家 3）。
- token 1 选专家 {5,7} → 要去 rank2（持有 5）和 rank3（持有 7）。
- 这正是 `is_token_in_rank[tok, dst]` 的含义。无法本地运行时记为「待本地验证」。

#### 4.1.5 小练习与答案

**Q1**：为什么不能用 NCCL 的 `all_to_all_single` 直接实现 MoE 的 dispatch？

**参考答案**：`all_to_all_single` 要求「每张卡发往各卡的份额在通信前已知且固定」（按 split 维切）。而 MoE 的去向由每个 token 的 `topk_idx` 在运行时决定，份额是**数据相关、不均衡**的，必须先算路由布局再通信，NCCL 集合通信表达不了这种 per-token 的动态路由。

**Q2**：`num_experts=64, num_ranks=8` 时，专家 37 落在哪个 rank？

**参考答案**：`num_local_experts = 64/8 = 8`，`37 // 8 = 4`，故在 rank 4。

---

### 4.2 install_deepep.sh：构建与链接

#### 4.2.1 概念说明

如前所述，`deep_ep` 这个包在这里是**参考 oracle**，用来在测试里和 TileScale 实现逐元素比对、并做公平的性能 benchmark。`install_deepep.sh` 的职责就是把这个参考实现装上。它顺带处理了 NVSHMEM 的安装与一个已知的链接 bug。理解这个脚本是「能跑通示例」的前提。

#### 4.2.2 核心流程

```text
1. 用 torch 探测当前 GPU 的 CUDA compute capability → 设 TORCH_CUDA_ARCH_LIST
2. 断言 3rdparty/DeepEP 已 clone（git submodule，否则退出）
3. 检查 NVSHMEM 是否已装；没有就 pip install nvidia-nvshmem-cu12
4. 修复 NVSHMEM 链接 bug：给 libnvshmem_host.so.3 建一个无版本号的软链 libnvshmem_host.so
5. cd 3rdparty/DeepEP && python setup.py install
6. python -c "import deep_ep" 校验
```

#### 4.2.3 源码精读

[tilelang/distributed/install_deepep.sh:6-14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/install_deepep.sh#L6-L14) —— 用 `torch.cuda.get_device_capability()` 探测架构并导出 `TORCH_CUDA_ARCH_LIST`，否则 DeepEP 的 `setup.py` 不知道为哪个 SM 编译。

[tilelang/distributed/install_deepep.sh:17-27](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/install_deepep.sh#L17-L27) —— 要求 `3rdparty/DeepEP` 子模块已存在；按需 `pip install nvidia-nvshmem-cu12`。

[tilelang/distributed/install_deepep.sh:30-37](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/install_deepep.sh#L30-L37) —— **关键链接修复**：DeepEP 链接时找的是无版本号库名 `libnvshmem_host.so`，而 pip 装的 NVSHMEM 只提供带版本号的 `libnvshmem_host.so.3`，于是手动 `ln -sf` 建一个软链；随后 `cd 3rdparty/DeepEP && python setup.py install` 真正构建参考实现。

> 注意区分：这条脚本装的是**参考 oracle** `deep_ep`，**不是** TileScale 自己的 kernel。TileScale 的 kernel 是 TileLang JIT 出来的，不需要 setup.py 编译。

#### 4.2.4 代码实践

**目标**：确认环境里 `deep_ep` 是否就绪，并理解它仅用于对照。

**步骤**：
1. 在仓库根目录确认子模块：`ls 3rdparty/DeepEP`（本仓库此目录已存在但为空壳，需 `git submodule update --init 3rdparty/DeepEP` 拉真实代码，**待本地验证**）。
2. 运行 `bash tilelang/distributed/install_deepep.sh`。
3. `python -c "import deep_ep; print('ok')"`。

**需要观察的现象 / 预期结果**：脚本末尾打印 `DeepEP is installed successfully. ✅`。若失败，通常是 NVSHMEM 软链缺失或 `TORCH_CUDA_ARCH_LIST` 未设。即便 `deep_ep` 未装，`examples/.../intranode/get_dispatch_layout.py` 等非对照代码仍可单独阅读。

#### 4.2.5 小练习与答案

**Q1**：为什么脚本要手动建 `libnvshmem_host.so` 软链？

**参考答案**：DeepEP 的链接命令引用无版本号库名 `libnvshmem_host.so`，而 `nvidia-nvshmem-cu12` 这个 pip 包只安装了带主版本号的 `libnvshmem_host.so.3`，二者名字对不上导致链接失败，软链补齐了这个别名。

**Q2**：如果不装 `deep_ep`，TileScale 自己的 dispatch kernel 还能运行吗？

**参考答案**：能。`deep_ep` 只在 `example_intranode.py` 里作为参考实现做正确性比对（[example_intranode.py:31-37](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/example_intranode.py#L31-L37)）。去掉所有 `ref_*` 对照后，`EPBuffer` 的三段流程本身不依赖 `deep_ep`。

---

### 4.3 get_dispatch_layout：路由布局计算（非分布式）

#### 4.3.1 概念说明

`get_dispatch_layout` 是三段流程里**唯一非分布式**的一步：每张卡各自对着自己的 `topk_idx`，统计「我要发往每个 rank / 每个专家多少 token」，并算出布尔掩码 `is_token_in_rank[token, rank]`。它的输出是后续 dispatch 通信的「地图」。设计上它在一个 kernel 里同时统计专家维度和 rank 维度，避免多趟扫描。

#### 4.3.2 核心流程

```text
输入: topk_idx [num_tokens, num_topk]  (int64), 全局专家号；-1 表示未选
输出:
  num_tokens_per_rank  [num_ranks]      int32
  num_tokens_per_expert[num_experts]    int32
  is_token_in_rank     [num_tokens, num_ranks]  bool

kernel 用两类 threadblock：
  - 专家统计块：每块负责一段专家 [begin, end)，逐 token 累加命中计数
  - rank 统计块：每块负责一段 rank，逐 token 算 is_in_rank 掩码并累加
（一趟扫描同时产出 per-expert / per-rank 计数与布尔掩码）
```

rank 与专家的关系是 `rank = expert // num_local_experts`。

#### 4.3.3 源码精读

[intranode/get_dispatch_layout.py:17-60](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/get_dispatch_layout.py#L17-L60) —— host 函数 `get_dispatch_layout`：校验 `topk_idx` 为 int64、2D、连续，分配输出张量并启动 kernel。注意它**没有** `kernel.initialize(allocator=...)`——因为这步不跨卡通信，用普通 `torch.empty(..., device="cuda")` 即可。

[intranode/get_dispatch_layout.py:63-94](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/get_dispatch_layout.py#L63-L94) —— kernel 头：grid = `ceildiv(num_experts, experts_per_sm) + ceildiv(num_ranks, ranks_per_sm)`，前若干块统计专家，后若干块统计 rank，复用同一把 threadblock 索引 `bx`。

[intranode/get_dispatch_layout.py:110-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/get_dispatch_layout.py#L110-L145) —— rank 统计段：对每个 token，遍历其 topk，凡 `expert_begin <= expert_idx < expert_end` 就把对应 `is_in_rank[i, j]` 置 True 并计数，最后跨线程求和写回 `num_tokens_per_rank`。这正是「per-token 动态路由」的体现。

> 这里用到了 u2 的 `T.alloc_shared`、`T.serial`、`T.clear` 等 tile 原语，但**没有** `dst_pe=`，因为是单卡本地统计。

#### 4.3.4 代码实践

**目标**：单独读懂这一非分布式 kernel，作为理解后续分布式 dispatch 的热身。

**步骤**：
1. 只看 [get_dispatch_layout.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/get_dispatch_layout.py)，忽略所有 `dst_pe`/allocator 概念。
2. 在纸上对 `num_tokens=4, num_topk=2, num_experts=4, num_ranks=2`、`topk_idx=[[0,2],[1,3],[0,1],[2,3]]` 跟踪一遍：`num_local_experts=2`，rank0 持有专家{0,1}，rank1 持有{2,3}。
3. 手算 `is_token_in_rank` 与 `num_tokens_per_rank`。

**需要观察的现象 / 预期结果**：
- token0 选{0,2} → rank0、rank1 都要发；token1 选{1,3} → rank0、rank1；token2 选{0,1} → 只 rank0；token3 选{2,3} → 只 rank1。
- `num_tokens_per_rank = [3, 3]`（rank0 收到 token0/1/2 共 3 个，rank1 收到 token0/1/3 共 3 个）。**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**：为什么把专家统计和 rank 统计放进同一个 kernel、用 `bx` 区分，而不是开两个 kernel？

**参考答案**：两者都要遍历同一份 `topk_idx`，合并可减少一次对 `topk_idx` 的全局读（`num_tokens × num_topk` 的大表），一次扫描同时产出两类统计与布尔掩码，省访存。

**Q2**：`is_token_in_rank` 的 dtype 是 bool，若某 token 选了同一 rank 下的两个专家，掩码会被置 True 两次吗？

**参考答案**：不会重复累加成 2。代码里用 `is_in_rank[rank_idx] += 1` 计数判断 `> 0`，再写 `is_token_in_rank[i, j] = True`（布尔），所以同一 token 对同一 rank 只记一次 True。

---

### 4.4 dispatch：notify + A2A 发送

#### 4.4.1 概念说明

dispatch 是真正的分布式 all-to-all。它分两小步：
- **notify_dispatch**：跨卡交换「我打算发给你们各多少 token」（all-to-all 计数），算出每个接收方把数据写到环缓冲的**全局偏移**（`rank_prefix_matrix`、`channel_prefix_matrix`），并把接收方用于本次的 4 个对称元数据缓冲清零。
- **dispatch_kernel**：发送方把自己负责的 token 通过 `T.put_warp`（u6-l3 的 CP-engine 原语）推到目标 rank 的环形缓冲；接收方轮询队头/队尾，把数据拼成连续的 `recv_x`、`recv_src_idx`、`recv_topk_idx`、`recv_topk_weights`。

这里大量复用 u6-l3 的远程拷贝原语：`T.put_warp(..., dst_pe=...)`、`T.st(..., dst_pe=..., scope="sys", sem="release")`、`T.ld(..., sem="acquire"/"volatile", scope="sys")`、`T.wait_ge/wait_ne`。

#### 4.4.2 核心流程

```text
notify_dispatch (每卡 1 个 block, 128 threads):
  1) T.sync_blocks/T.barrier_blocks  跨 block 屏障（用 barrier_signal）
  2) 把本卡的 num_tokens_per_rank / per_expert 逐 rank 远程写到 per_rank_buffer[rank][i,j]
     (T.st(..., dst_pe=tx))  —— 这就是 all-to-all 交换计数
  3) 每个接收 rank 对收到的计数做前缀和 → rank_prefix_matrix
  4) 把本卡收到的 per-expert 计数按 expert_alignment 向上对齐 → moe_recv_expert_counter
  5) T.copy(per_rank_buffer, rank_prefix_matrix)；清零 4 个 channel 元数据缓冲
  其余 block: 统计 channel_prefix_matrix（每通道每目标 rank 的 token 数）

dispatch_kernel (num_sms 个 block, 768 threads):
  bx 偶数 = 发送方:
    - 算本通道 token 范围 [start,end)
    - 流控: T.wait_ge(channel_head_idx, ...) 等接收方腾出缓冲
    - 对每个命中 token: T.put_warp 把 x / src_idx / 重映射后的 topk_idx / topk_weights 推远端
    - 更新远端 channel_tail_idx (release)
  bx 奇数 = 接收方:
    - T.wait_ne(channel_start/end_offset, 0) 等发送方告知偏移
    - 轮询 channel_tail_idx (acquire)，把环缓冲里的 token 拷到连续的 recv_x
    - 推进 channel_head_idx，直到收满 num_tokens_to_recv
```

「重映射 topk_idx」是 dispatch 的一个细节：发送方把全局专家号换算成**目标 rank 的本地专家号**，落在本地的保留、不在本地的写 `-1` 并把权重清零（见 [dispatch.py:377-401](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L377-L401)）。

#### 4.4.3 源码精读

[intranode/dispatch.py:23-120](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L23-L120) —— `notify_dispatch_kernel`：注意它 `@tilelang.jit(pass_configs={"tl.disable_tma_lower": True, "tl.disable_warp_specialized": True})`，即走普通 SIMT 路径，不用 TMA/warp 特化（这两条优化路线见 u4）。第 [66-68 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L66-L68) 的 `T.st(per_rank_buffer[rank, tx], num_tokens_per_rank[tx], dst_pe=tx)` 就是 all-to-all 交换计数的核心——本卡把自己的统计写给每个目标卡。

[intranode/dispatch.py:124-185](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L124-L185) —— host `notify_dispatch`：清计数器为 `-1`（哨兵值，表示「未就绪」），启动 kernel 后调 `ep_ext.wait_for_counters_ready(...)` **在 host 上自旋等待**所有接收计数就位，再返回 `num_recv_tokens` 等。这个 host 端等待函数是 [deepep_utils.py:218-250](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep_utils.py#L218-L250) 用 `load_inline` 即时编译的内联 C++，靠 `volatile` 轮询 pinned 映射的 counter（呼应 u6-l5 的 host↔device 映射张量）。

[intranode/dispatch.py:362-401](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L362-L401) —— 发送方主循环核心：`T.put_warp` 远程搬运 `x`，`unroll_factor=4, enable_aggressive_vectorize=True`（u6-l3 讲过的 warp 级吞吐旋钮），随后远程写 `src_idx` 与重映射后的 `topk_idx`/`topk_weights`。

[intranode/dispatch.py:420-514](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L420-L514) —— 接收方：先 `T.wait_ne(channel_start_offset, 0)` 等发送方写偏移，再 `T.ld(..., sem="acquire", scope="sys")` 轮询 `channel_tail_idx`，从环缓冲 `(head + idx) % num_recv_buffer_tokens` 取数据填到连续的 `recv_x[total_offset + ...]`。

[intranode/dispatch.py:880-881](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L880-L881) —— 打包返回 `handle`：`(rank_prefix_matrix, channel_prefix_matrix, recv_channel_prefix_matrix, recv_src_idx, is_token_in_rank, send_head)`。这个 handle 会被 combine 复用，也可用于「cached 重发」（布局不变时跳过 notify）。

#### 4.4.4 代码实践

**目标**：跟踪一条 token 从本卡到目标 rank 的完整远程写路径。

**步骤**：
1. 在 [dispatch.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py) 的发送方分支（`bx % 2 == 0`）定位 `T.put_warp(...)`（约 [363 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/dispatch.py#L363-L370)）。
2. 回顾 u6-l3：`put_warp` 在 C++ 端降级为 `tl::cp_warp`，远程地址 = `get_remote_base_ptr(peer) + (本地偏移)`，靠 `kernel.initialize(allocator=...)` 注入的基址表寻址（见 [buffer.py:69-76](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/buffer.py#L69-L76) 的 `get_allocator` 与 dispatch.py 中各 kernel 的 `kernel.initialize(allocator=allocator, ...)`）。
3. 画出时序：发送方 `put_warp(x)` → 更新远端 `channel_tail_idx(release)` → 接收方 `ld(tail_idx, acquire)` → 从环缓冲拷到 `recv_x` → 更新远端 `channel_head_idx`。

**需要观察的现象 / 预期结果**：你应能用一句话说清「谁写 tail、谁读 tail、谁写 head」——**发送方维护 tail（写了多少），接收方维护 head（读了多少）**，二者通过 acquire/release 配对保证可见性。运行层面**待本地验证**（需多卡 NVLink 环境）。

#### 4.4.5 小练习与答案

**Q1**：notify 阶段为什么要先把 `moe_recv_counter` 填成 `-1` 再启动 kernel？

**参考答案**：`-1` 是「尚未就绪」的哨兵。host 端 `wait_for_counters_ready` 据此自旋：只要任一计数 `< 0` 就继续等，全部 `>= 0` 才认为所有发送方都已汇报，从而拿到正确的 `num_recv_tokens`。若不清成 -1，可能读到上一轮残留值而误判就绪。

**Q2**：dispatch_kernel 里发送方和接收方为什么放在**同一** kernel（用 `bx % 2` 区分），而不是两个 kernel？

**参考答案**：发送与接收必须**同时在线程块网格内并发推进**才能形成流水（一方推数据、一方立刻消费），若拆成先后两个 kernel，接收方要等整个发送 kernel 结束才能启动，无法重叠，吞吐会大幅下降。同一 grid 内偶/奇 block 天然并行。

---

### 4.5 combine：reduce 回归

#### 4.5.1 概念说明

专家算完后，combine 把 `expert_out` 反向送回 token 的原始 rank，并**按 token 求和**（注意：combine 内部**不带权重**求和，权重单独累加返回，由外部再乘）。由于一个 token 可能被多个 rank 处理，接收方必须等齐该 token 的所有贡献才能 reduce——这就是 `send_head` 编排的用途。

#### 4.5.2 核心流程

```text
cached_notify_combine (每卡 1 个 block):
  1) 清零 channel_head_idx / channel_tail_idx（复用 dispatch 的对称缓冲）
  2) 重算 send_head：对每个 (token, rank)，给出「本 token 在该 rank 接收环缓冲里
     期望的 head 位置」；负值用 -head-1 编码「尚未到达」

combine_kernel (num_sms 个 block, 768 threads):
  bx 偶数 = 发送方（反向）:
    - 用 rank_prefix_matrix / channel_prefix_matrix 算要回送的 token 范围
    - T.put_warp 把 expert_out 的 x 推回原 rank，附带 src_idx、topk_weights
  bx 奇数 = 接收方（reduce）:
    - 对每个 token，等 send_head 指示的所有贡献到齐（warp_any 轮询）
    - 从各贡献 rank 的环缓冲取出数据，求和写 recv_x
    - 累加 topk_weights 写 recv_topk_weights
```

combine 的接收方要求 `hidden % 8 == 0`（见 [combine.py:117](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L117)），因为 reduce 时按 8 元素手动向量化（`T.vectorized(8)`）。

#### 4.5.3 源码精读

[intranode/combine.py:17-71](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L17-L71) —— `cached_notify_combine_kernel`：重算 `send_head` 期望值并清零 head/tail。注意第 [51-69 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L51-L69) 的反向扫描 + `T.tvm_warp_shuffle` 求「最近的有效 head」，用 `-head-1` 编码缺失——与 dispatch 里 `-value-1` 区分「零」与「未初始化」是同一种技巧（见 [deepep.md:189](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep.md#L189)）。

[intranode/combine.py:189-196](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L189-L196) —— 发送方 `T.put_warp` 把 `expert_out` 推回原 rank，参数与 dispatch 一致（`unroll_factor=4, enable_aggressive_vectorize=True`）。

[intranode/combine.py:303-339](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L303-L339) —— 接收方 reduce 核心：第 [308-316 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L308-L316) 对 hidden 按 8 元素向量化，遍历所有贡献 rank `num_topk_ranks`，把每个贡献的 8 个值累加到 `values`，再写回 `recv_x`；第 [328-339 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L328-L339) 单独累加 `topk_weights`。求和本身不带 router 权重，故注释称「no weighting」。

[intranode/combine.py:355-415](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L355-L415) —— host `intranode_combine`：在专用 `comm_stream` 上先 notify 再跑 combine kernel，最后 `compute_stream.wait_stream(comm_stream)` 把结果同步回计算流。

#### 4.5.4 代码实践

**目标**：理解 combine 如何「等齐一个 token 的所有贡献再 reduce」。

**步骤**：
1. 读 [combine.py:276-301](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L276-L301)：用 `send_head[token, lane]` 取每个贡献 rank 的期望 head，靠 `T.tvm_warp_shuffle` 在 warp 内广播，挑出 `num_topk_ranks` 个有效贡献及其环缓冲槽位 `slot_indices`。
2. 对比 dispatch 的发送方——combine 发送的是「专家输出」而非原始 token，但远程搬运机制（`put_warp` + tail/head）完全相同。
3. 在 [example_intranode.py:122-137](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/example_intranode.py#L122-L137) 看测试如何用 `ref_combined_x` 校验 combine 正确性。

**需要观察的现象 / 预期结果**：combine 输出的 `reduced_x` 应与原版 DeepEP 逐元素相等（测试里 `torch.equal`）。若手算：一个 token 被分发到 R 个 rank，则 `reduced_x = Σ_{r} expert_out_r`（不带权重）。**待本地验证**。

#### 4.5.5 小练习与答案

**Q1**：combine 的 reduce 为什么「不带权重」？

**参考答案**：权重 `topk_weights` 来自 router，dispatch 时已随 token 发到专家卡；combine 只负责把分散的专家输出按 token 汇总。加权聚合（`Σ w_i · out_i`）是 MoE 的最后一步，由调用方在外部完成，故 combine 只返回求和结果与累加的权重，职责单一。

**Q2**：combine 接收方为何强制 `hidden % 8 == 0`？

**参考答案**：reduce 内循环用 `T.vectorized(8)` 把 hidden 维按 8 元素一组向量化读写（[combine.py:308-325](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/intranode/combine.py#L308-L325)），若 hidden 不是 8 的倍数会越界，故编译期断言。

---

## 5. 综合实践

**任务**：画出 intranode DeepEP 一次完整 EP 通信的「数据流 + 通信原语」总览图，并标注每一步用到的 TileLang 分布式原语。

**建议步骤**：

1. 以 `num_ranks=2`、`num_experts=4`、`num_topk=2`、`hidden=16`、`num_tokens=8` 为参数，在纸上为 rank0、rank1 各画一列。
2. 用箭头标出三阶段的跨卡数据流，并在每个箭头上注明所用原语：
   - `get_dispatch_layout`：**无跨卡**（本地统计）。
   - `notify_dispatch`：`T.st(per_rank_buffer[rank, tx], ..., dst_pe=tx)`（all-to-all 交换计数）、`T.sync_blocks/barrier_blocks(barrier_signal)`。
   - `dispatch_kernel`：发送方 `T.put_warp(..., dst_pe=...)`、`T.st(channel_tail_idx, ..., sem="release", dst_pe=...)`；接收方 `T.wait_ne`、`T.ld(..., sem="acquire"/"volatile", scope="sys")`。
   - `combine_kernel`：发送方 `T.put_warp`；接收方 reduce（`T.vectorized(8)` 求和）、靠 `send_head` 等齐。
3. 在图上标出 `handle` 的流转：dispatch 产出 → combine 消费；标出哪些对称缓冲来自 `EPBuffer._pre_alloc_symm_buffers_intranode`（即 u6-l5 的 allocator）。
4. 若有多卡 NVLink 机器，运行：
   ```bash
   CUDA_VISIBLE_DEVICES=0,1 TILELANG_USE_DISTRIBUTED=1 \
     python examples/distributed/deepseek_deepep/intranode/example_intranode.py \
     --num_ranks 2 --num_tokens 4096 --hidden 7168 --num_topk 8 --num_experts 32
   ```
   观察三段 `Check passed. ✅` 与最后的 dispatch/combine 延迟、带宽报告，与 [deepep.md:18-22](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep.md#L18-L22) 的基准对比。无多卡环境则记为「待本地验证」，仅完成作图部分。

**预期产出**：一张含「rank0/rank1 两列、四阶段（layout/notify/dispatch 内部 send+recv/combine 内部 send+recv）、每条跨卡箭头标注原语与 sem/scope」的流程图，以及一句话结论：dispatch 与 combine 共用同一套「通道 + 环形缓冲 + head/tail + put_warp」机制，差别只在数据方向与接收方是否 reduce。

## 6. 本讲小结

- DeepEP 是为 MoE expert parallel 设计的**不均衡 all-to-all** 通信库；TileScale 用 TileLang **重新实现**了它的 intranode（NVLink）路径，原始 `deep_ep` 仅作正确性参考 oracle。
- 三段式流程：`get_dispatch_layout`（本地算路由布局）→ `dispatch`（notify 交换计数 + put_warp 发送）→ `combine`（反向发送 + 按 token 求和）。
- `install_deepep.sh` 装的是**参考 oracle**：探测 CUDA arch、补 NVSHMEM、用软链修 `libnvshmem_host.so` 链接 bug、`setup.py install`。
- dispatch/combine kernel 用「通道（`num_channels = num_sms//2`）+ 环形缓冲 + head/tail 队列」把 all-to-all 拆成可控的远程拷贝，发送方维护 tail、接收方维护 head，靠 acquire/release 与 `T.wait_ge/wait_ne` 同步。
- 全程复用 u6-l3 的 CP-engine 原语（`T.put_warp`、`T.st(..., dst_pe=)`、`T.ld(..., sem=, scope=)`）与 u6-l5 的 `tilelang.get_allocator(is_distributed=True)` + `kernel.initialize(allocator=...)`（注入远程基址表）。
- 实现范围：**仅 intranode normal mode**；internode 与 low-latency 模式仍为 TODO（[deepep.md:5-8](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep.md#L5-L8)）。

## 7. 下一步学习建议

- **u6-l7（分布式实战）**：本讲的 dispatch/combine 是「带路由的 all-to-all」，可与 `example_all_to_all.py`、`example_summa.py` 等经典集合通信示例对照，体会「规则 all-to-all」与「MoE 动态 all-to-all」的差异。
- **深入 host↔device counter**：阅读 [deepep_utils.py:218-250](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep_utils.py#L218-L250) 的 `wait_for_counters_ready` 内联 C++，结合 u6-l5 的 `create_mapped_tensor`，理解 host 如何低成本感知 device 状态。
- **关注 internode/low-latency 进展**：当 [deepep.md:5-8](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/deepseek_deepep/deepep.md#L5-L8) 的 TODO 推进时，RDMA 路径会引入 `num_max_rdma_chunked_*` 等新配置（已见于 `Config`），届时需补读 internode kernel。
- **回看 u4 优化机制**：本讲 kernel 全部 `disable_tma_lower`/`disable_warp_specialized`，属普通 SIMT 路径；可思考若 dispatch_kernel 开启 TMA/warp 特化（生产-消费模型天然适配）能带来多少收益。
