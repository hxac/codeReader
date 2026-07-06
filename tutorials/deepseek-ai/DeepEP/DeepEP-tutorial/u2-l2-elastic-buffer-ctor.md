# 创建 ElasticBuffer：构造参数、缓冲区与拓扑属性

## 1. 本讲目标

`ElasticBuffer` 是 DeepEP V2 暴露给用户的唯一通信缓冲区对象，所有 dispatch / combine / Engram / PP / AGRS 操作都挂在它身上。本讲聚焦「如何把它正确地创建出来」。读完本讲，你应该能够：

1. 用「显式 `num_bytes`」和「MoE 设置（`num_max_tokens_per_rank` + `hidden` + `num_topk`）」两种方式分别构造一个 `ElasticBuffer`，并理解它们的等价关系。
2. 说清楚 `allow_hybrid_mode`、`allow_multiple_reduction`、`prefer_overlap_with_compute` 三个开关分别在影响什么。
3. 知道 QP（Queue Pair，RDMA 的工作队列）数量在三种情况下会被自动设成多少。
4. 理解 C++ 侧 `[[[Workspace] GPU buffer] CPU buffer]` 这套对称内存布局是怎么拼出来的。
5. 解读 `num_scaleout_ranks` / `num_scaleup_ranks` / `num_rdma_ranks` / `num_nvlink_ranks` 这几个拓扑属性的含义。

## 2. 前置知识

在动手之前，先建立三个直觉。

**第一，什么是「缓冲区」。** 在 EP（专家并行）的 dispatch 阶段，每个 rank 都要把自己负责的 token 发给其他 rank 上的专家，同时也要开辟一块空间接收别人发来的 token。DeepEP 不会每次通信都临时分配显存，而是在初始化时就预分配一大块「通信缓冲区」，后续所有 dispatch/combine 都在这块缓冲区里读写。`ElasticBuffer` 就是这块缓冲区的 Python 句柄。

**第二，什么是「对称内存」。** DeepEP V2 用 NCCL Gin 后端，要求所有 rank 在各自的 GPU 上都开辟「大小相同、地址偏移可推算」的一块显存窗口。这样任何一个 rank 只要知道对端 rank 的偏移，就能直接写到对方的窗口里（NVLink 写或 RDMA 写）。所以缓冲区大小在所有 rank 上必须一致，且要对齐到 2 MB（`kNumAlignmentBytes = 2097152`）。

**第三，什么是「逻辑域 / 物理域」。** 一组 GPU 既能按物理链路划分（哪些 GPU 之间有 NVLink 直连 = NVLink 域；哪些只能走 RDMA = RDMA 域），也能按通信策略划分（DeepEP 把 scaleout = 跨节点 RDMA、scaleup = 节点内 NVLink 组合成「两级逻辑域」）。这两套划分会在 4.5 节展开。本讲只需要记住：缓冲区创建完之后，这些拓扑数字会被探测出来并暴露成属性。

> 本讲承接 u2-l1。你已经知道 `import deep_ep` 时会做 NCCL 校验和 JIT 路径注入；本讲从「路径就绪后，第一次 `ElasticBuffer(group, ...)`」开始。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deep_ep/buffers/elastic.py` | Python 侧 `ElasticBuffer` 类，包含 `__init__`、`get_buffer_size_hint`、拓扑属性访问方法、QP/SM 解析式计算等。 |
| `csrc/elastic/buffer.hpp` | C++ 侧 `deep_ep::elastic::ElasticBuffer` 实现，负责真正的对称内存分配、workspace 布局、构造断言与拓扑查询；同时定义静态方法 `calculate_buffer_size`。 |
| `deep_ep/utils/comm.py` | `get_nccl_comm_handle`：优先复用 PyTorch 的 NCCL communicator，否则新建一个。 |
| `csrc/kernels/backend/nccl.cu` | `get_physical_domain_size` / `get_logical_domain_size` 及 `NCCLSymmetricMemoryContext` 构造，真正探测 NVLink/RDMA 域大小。 |
| `csrc/kernels/backend/symmetric.hpp` | 定义 2 MB 对齐常量 `kNumAlignmentBytes` 与多种对称内存分配器。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：两种尺寸指定方式、三个开关、QP 自动分配、C++ 对称内存布局、拓扑属性。

### 4.1 两种尺寸指定方式：`num_bytes` 与 MoE 设置

#### 4.1.1 概念说明

`ElasticBuffer.__init__` 允许用两种完全不同的方式告诉它「缓冲区要多大」：

- **方式 A（显式字节）**：直接传 `num_bytes`，单位是字节，必须 2 MB 对齐。适合你想精细控制内存、或者把同一块缓冲区复用到多种用途（dispatch + Engram + PP + AGRS）的场景。
- **方式 B（MoE 设置）**：传 `num_max_tokens_per_rank` + `hidden` + `num_topk` + `use_fp8_dispatch`，让 DeepEP 自己根据 dispatch/combine 的最坏布局算出需要多少字节。这是绝大多数 MoE 用户的选择。

这两种方式是「或」的关系：只要 `num_bytes` 不是 `None`，方式 B 就被跳过。

#### 4.1.2 核心流程

构造函数里关于尺寸的核心决策只有三步：

```text
if num_bytes is None:
    num_bytes = _C.calculate_elastic_buffer_size(
        comm_handle, num_max_tokens_per_rank, hidden, num_topk,
        use_fp8_dispatch, allow_hybrid_mode, allow_multiple_reduction)
# 此时 num_bytes 一定已被赋值，且必然是 2 MB 对齐的
self.num_bytes = num_bytes
```

要点：

1. 方式 B 调用的 `_C.calculate_elastic_buffer_size` 在 C++ 里就是 `ElasticBuffer::calculate_buffer_size`，它内部已经做了 `math::align(..., 2 MB)`，所以返回值天然 2 MB 对齐。
2. 方式 A 由用户自己保证 2 MB 对齐，C++ 构造函数里有一条断言会强行检查（见 4.4 节）。
3. 注意 `num_bytes` 的语义是 **「GPU buffer + CPU buffer，不含 workspace」**。也就是说 workspace 的字节数是额外加在 NCCL 对称窗口最前面的，并不计入 `self.num_bytes`。

#### 4.1.3 源码精读

Python 侧的两种尺寸分支在这里：

[deep_ep/buffers/elastic.py:304-314](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L304-L314) —— 如果用户没给 `num_bytes`，就用 MoE 设置调 `_C.calculate_elastic_buffer_size` 推算；否则直接用用户的值。两种方式最后都把结果存进 `self.num_bytes`。

构造函数的 docstring 也明确写了 `num_bytes` 的语义和 2 MB 对齐要求：

[deep_ep/buffers/elastic.py:252-254](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L252-L254) —— 说明 `num_bytes` 覆盖 MoE 计算，且必须对齐到 `get_elastic_buffer_alignment()`。

C++ 侧的 `calculate_buffer_size` 才是真正算尺寸的地方：

[csrc/elastic/buffer.hpp:652-686](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L652-L686) —— 它做了三件事：①取拓扑（`get_physical_domain_size` / `get_logical_domain_size`）；②分别算 dispatch 与 combine 两种布局的字节数；③取两者最大值并对齐 2 MB 返回。

其中 `get_dispatch_buffer_size`（直接模式 vs hybrid 模式）：

[csrc/elastic/buffer.hpp:586-614](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L586-L614) —— 直接模式（`num_scaleout_ranks == 1`）只累加 `send + recv` 两个 `BufferLayout`；hybrid 模式还要额外累加 scaleout 的发送/接收布局。

每个 `BufferLayout` 的字节数是「`num_ranks * num_max_tokens_per_rank * 每个 token 的字节数`」，其中「每个 token 的字节数」由 `TokenLayout` 决定（包含 hidden、scaling factor、metadata、mbarrier 等段）。所以粗略地：

\[
\text{recv 字节} \approx \text{num\_ranks} \times \text{num\_max\_tokens\_per\_rank} \times (\text{hidden} \times \text{elem\_size} + \text{metadata})
\]

hidden × elem_size 是主项（BF16 时 elem_size=2，FP8 时 elem_size=1）。这就是为什么 `num_bytes` 会随 `num_ranks`、`num_max_tokens_per_rank`、`hidden` 线性增长。

#### 4.1.4 代码实践

**实践目标**：验证「方式 A（`get_buffer_size_hint` 估算）」与「方式 B（直接传 MoE 设置）」得到的 `num_bytes` 完全一致。

**操作步骤**（在单机多卡、`torch.distributed` 已初始化的环境中）：

```python
# 示例代码：仅作说明，需在已 init_process_group 的多进程环境里运行
import torch.distributed as dist
from deep_ep import ElasticBuffer

group = ...                      # 你的 ProcessGroup
num_max_tokens_per_rank = 4096
hidden = 7168
num_topk = 6

# 方式 A：先估算，再当成 num_bytes 传入
hint = ElasticBuffer.get_buffer_size_hint(
    group, num_max_tokens_per_rank, hidden, num_topk)
buf_a = ElasticBuffer(group, num_bytes=hint,
                      num_max_tokens_per_rank=num_max_tokens_per_rank)

# 方式 B：直接传 MoE 设置
buf_b = ElasticBuffer(group, num_max_tokens_per_rank=num_max_tokens_per_rank,
                      hidden=hidden, num_topk=num_topk)

print('hint        =', hint)
print('buf_a.bytes =', buf_a.num_bytes)
print('buf_b.bytes =', buf_b.num_bytes)
```

**需要观察的现象**：三个数字应该完全相同（`hint == buf_a.num_bytes == buf_b.num_bytes`），且都是 2 MB 的整数倍。

**预期结果**：因为方式 A 用的 `get_buffer_size_hint` 内部调的就是同一个 `_C.calculate_elastic_buffer_size`，而方式 B 在 `num_bytes is None` 时也调同一个函数，所以两者必然相等。**待本地验证**：实际数值取决于你的 `num_ranks` 与 GPU 是否全互联 NVLink（影响 `is_scaleup_nvlink` 与 send buffer 是否被省略）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `use_fp8_dispatch=True` 改成 BF16（默认），`num_bytes` 会变大还是变小？大概几倍？
**答案**：变大。FP8 的 elem_size=1，BF16 的 elem_size=2，主项翻倍，所以 `num_bytes` 大约是 FP8 的 2 倍（scaling factor 段不会翻倍，所以略小于 2 倍）。

**练习 2**：为什么 `num_topk` 允许传 0？
**答案**：见 [csrc/elastic/buffer.hpp:662-664](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L662-L664) —— 内部有大量 `num_topk <= 32` 的约束，所以传 0 时统一用 32 来计算 token 大小（偏保守），保证缓冲区在任何合法 top-k 下都够用。

---

### 4.2 三个关键开关：hybrid / multiple_reduction / prefer_overlap

#### 4.2.1 概念说明

构造函数有三个布尔开关，它们会同时影响「缓冲区尺寸」「SM/QP 数量」「内核选择」：

- **`allow_hybrid_mode`**（默认 `True`）：是否在多节点场景下启用「scaleout（RDMA）+ scaleup（NVLink）两级混合通信」。开启后更友好于多平面/多轨道网络，带宽更高；关闭则强制走「直接」模式。
- **`allow_multiple_reduction`**（默认 `True`）：combine 阶段是否允许多次归约。开启时优先少传数据、多次本地归约；关闭时只做一次归约、精度最好但传输量更大。
- **`prefer_overlap_with_compute`**（默认 `True`）：是否倾向于「少占 SM」以便把 SM 让给计算流做通信-计算重叠。开启时用更少 SM；关闭时倾向于打满性能（最少 64 个 SM）。

#### 4.2.2 核心流程

这三个开关在构造时被原样存下来，之后在三个地方被消费：

```text
allow_hybrid_mode        → 影响逻辑域划分、QP 数量、缓冲区布局、内核选择
allow_multiple_reduction → 影响 combine 布局尺寸（单次 vs 多次归约的 rank 数）
prefer_overlap_with_compute → 影响 get_theoretical_num_sms 的下界（min(num_sms, 64) 或不设下界）
```

#### 4.2.3 源码精读

开关被存为实例属性：

[deep_ep/buffers/elastic.py:270-277](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L270-L277) —— 把 `allow_hybrid_mode`、`allow_multiple_reduction`、`prefer_overlap_with_compute`、`deterministic` 全部存为 `self.xxx`，并透传给 C++ runtime。

`allow_multiple_reduction` 直接进入尺寸计算（`calculate_elastic_buffer_size` 的最后一个参数）：

[deep_ep/buffers/elastic.py:304-309](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L304-L309) —— `allow_hybrid_mode` 和 `allow_multiple_reduction` 都作为参数传给 `_C.calculate_elastic_buffer_size`。

在 C++ 侧，`allow_multiple_reduction` 会改变 combine 的「接收 rank 数」：

[csrc/elastic/buffer.hpp:623-633](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L623-L633) —— 直接 combine 时，`num_tokens_in_layout = allow_multiple_reduction ? min(num_ranks, num_topk) : num_topk`；关闭时还要把发送侧 token 数乘以 `num_topk`（最坏 expand 情况），所以缓冲区明显变大。

`prefer_overlap_with_compute` 的作用在 SM 计算里（虽然 SM 计算主要在 u3-l3 讲，但本讲能看到它的下界逻辑）：

[deep_ep/buffers/elastic.py:823-825](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L823-L825) —— `num_sms = num_sms if self.prefer_overlap_with_compute else max(num_sms, 64)`。开重叠时尊重带宽建模算出来的（偏小的）SM 数；关重叠时至少 64 个 SM 以榨干性能。

#### 4.2.4 代码实践

**实践目标**：观察 `allow_multiple_reduction=False` 时缓冲区是否真的变大。

**操作步骤**：

```python
# 示例代码
hint_on  = ElasticBuffer.get_buffer_size_hint(
    group, 4096, 7168, 6, allow_multiple_reduction=True)
hint_off = ElasticBuffer.get_buffer_size_hint(
    group, 4096, 7168, 6, allow_multiple_reduction=False)
print('multiple_reduction=True  :', hint_on)
print('multiple_reduction=False :', hint_off)
```

**需要观察的现象**：`hint_off >= hint_on`，且通常显著更大（尤其 `num_topk` 较大时）。

**预期结果**：关闭多次归约时，发送侧要按 `num_topk` 倍预留空间（见上面 4.2.3 的源码），所以字节数会明显增大。**待本地验证**：具体倍数取决于 `num_topk` 与 `num_ranks` 的相对大小。

#### 4.2.5 小练习与答案

**练习**：`allow_hybrid_mode=False` 在单机 8 卡环境下，会让 `num_scaleup_ranks` 变成多少？
**答案**：仍是 8。因为单机时 `num_rdma_ranks=1`、`num_nvl_ranks=8`，无论 hybrid 是否开启，`num_scaleup_ranks` 都等于 8（hybrid 关闭时 `num_scaleup_ranks = num_rdma_ranks * num_nvl_ranks = 1*8 = 8`）。hybrid 的差别要在「多节点」才体现出来，详见 4.5 节。

---

### 4.3 QP 自动分配

#### 4.3.1 概念说明

**QP（Queue Pair）** 是 RDMA 网卡上的「发送/接收工作队列对」。DeepEP 通过 NCCL Gin 在初始化时一次性申请一批 QP（见构造函数里的 `ginContextCount = num_allocated_qps`），后续 dispatch/combine 内核就往这些 QP 上提交 RDMA 请求。

构造参数 `num_allocated_qps` 默认是 0，表示「让我自动决定」。自动规则非常简单粗暴，分三种情况：

| 条件 | 自动 QP 数 |
| --- | --- |
| 非 hybrid（直接模式） | 17 |
| hybrid + 网卡支持 fast RDMA atomic | 65 |
| hybrid + 不支持 fast atomic | 129 |

多出来的那 1 个 QP 是给 notify warps 用的（用来发控制信号）。

#### 4.3.2 核心流程

```text
if num_allocated_qps == 0:
    if allow_hybrid_mode:
        num_allocated_qps = 65 if check_fast_rdma_atomic_support() else 129
    else:
        num_allocated_qps = 17
```

之后 `num_allocated_qps` 被透传给 C++ runtime，再传给 `NCCLSymmetricMemoryContext`，最终变成 NCCL Gin 的 `ginContextCount`。

#### 4.3.3 源码精读

QP 自动分配逻辑：

[deep_ep/buffers/elastic.py:326-335](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L326-L335) —— 注释明确写了「Hybrid mode will consume more QPs」「The extra QP is for notify warps」，并按 hybrid + 是否支持 fast atomic 分流。

`check_fast_rdma_atomic_support` 的判定基于网卡名（默认 NIC 名），它决定 hybrid 模式用 65 还是 129 个 QP。

`num_allocated_qps` 之后会作为「上限」约束实际使用的 QP 数（见 `get_theoretical_num_qps` 里的 `min(num_qps, self.num_allocated_qps)`）：

[deep_ep/buffers/elastic.py:836-853](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L836-L853) —— 直接模式只鼓励 `num_sms + 1`（封顶 9）个 QP 以减少 doorbell 开销；hybrid 模式则鼓励「每个 channel 一个独立 QP」（`num_sms * 16 + 1`），所以才会预分配那么多。

#### 4.3.4 代码实践

**实践目标**：打印你的环境自动分配了多少 QP，并对照上表解释。

**操作步骤**：

```python
buf = ElasticBuffer(group, num_max_tokens_per_rank=4096, hidden=7168, num_topk=6)
print('num_allocated_qps =', buf.num_allocated_qps)
print('allow_hybrid_mode =', buf.allow_hybrid_mode)
```

**需要观察的现象**：单机环境（hybrid 通常仍为 True，但因为没有跨节点，QP 不会真正被用到）一般会看到 65 或 129。

**预期结果**：取决于 `check_fast_rdma_atomic_support()` 对你网卡名的判定。**待本地验证**。

#### 4.3.5 小练习与答案

**练习**：为什么 hybrid 模式需要的 QP 数（65/129）远多于直接模式（17）？
**答案**：直接模式希望少 QP 以减少 doorbell ring 开销；hybrid 模式希望「每个 channel（外加 notify）都有独立 QP」，channel 数量随 SM 数线性增长（`num_sms * 16`），所以预分配的 QP 池要大得多。

---

### 4.4 C++ 侧的对称内存布局与构造

#### 4.4.1 概念说明

Python 的 `__init__` 最后会 `new` 一个 C++ `ElasticBuffer` 对象（即 `self.runtime`）。这个 C++ 对象真正负责「向 NCCL 申请对称窗口、切分布局、清零 workspace」。理解它的关键是这一句注释：

> Memory layout: `[[[Workspace] GPU buffer] CPU buffer]`

也就是说，NCCL 对称窗口从低地址到高地址依次是：

1. **Workspace**（对齐 2 MB，必须全零）：放跨 rank 的计数器、信号量、AGRS session 信号等「控制平面」数据。
2. **GPU buffer**：放真正要搬运的 token 数据（dispatch/combine 的 send/recv 区）。
3. **CPU buffer**（可选，`num_cpu_bytes > 0` 时）：放在 host 内存里，主要用于 Engram 远程拉取的本地存储段。

注意 `self.num_bytes`（Python 暴露的字节数）只包含「GPU buffer + CPU buffer」，**不含 workspace**；workspace 是额外叠在最前面的。

#### 4.4.2 核心流程

C++ 构造函数的关键步骤：

```text
1. 断言 num_buffer_bytes 与 num_cpu_buffer_bytes 都 2 MB 对齐，且 cpu <= total
2. num_gpu_buffer_bytes = num_buffer_bytes - num_cpu_buffer_bytes
3. num_workspace_bytes = align(WorkspaceLayout::get_num_bytes(), 2 MB)
4. num_sym_bytes = num_workspace_bytes + num_buffer_bytes   # 窗口总大小
5. 创建 NCCLSymmetricMemoryContext(comm, cpu_comm, num_ranks, rank,
                                   num_sym_bytes, num_cpu_bytes,
                                   allow_hybrid_mode, sl_idx, num_allocated_qps)
6. 校验：num_workspace + num_gpu == ctx.num_gpu_bytes，且 num_cpu == ctx.num_cpu_bytes
7. workspace = ctx.mapped_window_ptr（窗口起点）
8. buffer   = workspace + num_workspace_bytes（跳过 workspace）
9. cudaMemset(workspace, 0, num_workspace_bytes)  # workspace 必须清零
10. 分配 host_workspace（cudaHostAllocMapped）并清零
```

#### 4.4.3 源码精读

C++ 构造函数完整签名与成员初始化：

[csrc/elastic/buffer.hpp:81-101](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L81-L101) —— 注意 2 MB 对齐的三条断言，以及 `num_gpu_buffer_bytes = num_buffer_bytes - num_cpu_buffer_bytes` 的拆分。

workspace 大小与对称窗口创建：

[csrc/elastic/buffer.hpp:103-114](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L103-L114) —— workspace 对齐 2 MB 后放在最前；`num_sym_bytes = workspace + buffer` 才是交给 NCCL 的窗口总大小；`num_cpu_buffer_bytes` 单独作为窗口的 CPU 段大小。

把窗口指针拆成 workspace 与 buffer：

[csrc/elastic/buffer.hpp:125-130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L125-L130) —— `workspace = mapped_window_ptr`（起点），`buffer = workspace + num_workspace_bytes`，然后 `cudaMemset` 把 workspace 清零。

2 MB 对齐常量的定义：

[csrc/kernels/backend/symmetric.hpp:16](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L16) —— `kNumAlignmentBytes = 2097152`（即 2 MB），它就是 `get_elastic_buffer_alignment()` 返回的值，也是所有尺寸对齐的基准。

Python 侧把 `num_bytes` 与 `num_cpu_bytes` 透传给 C++ 构造：

[deep_ep/buffers/elastic.py:344-354](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L344-L354) —— `_C.ElasticBuffer(...)` 的实参顺序与 C++ 构造函数一一对应；注意 `num_bytes` 这里传的是「不含 workspace」的值，workspace 由 C++ 自己叠加。

#### 4.4.4 代码实践

**实践目标**：理解 workspace 为何必须放在最前并对齐 2 MB。

**操作步骤**（源码阅读型实践）：
1. 阅读 [csrc/elastic/buffer.hpp:103-114](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L103-L114)，确认 workspace 是先于 buffer 被加进 `num_sym_bytes` 的。
2. 阅读 [csrc/elastic/buffer.hpp:125-130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L125-L130)，确认 `buffer = workspace + num_workspace_bytes`，即 buffer 紧跟在 workspace 后面。
3. 思考：为什么 workspace 必须对齐 2 MB？

**需要观察的现象 / 预期结果**：因为整个 NCCL 对称窗口的起点（`mapped_window_ptr`）是 2 MB 对齐的，workspace 放在最前且自身 2 MB 对齐，就能保证紧跟其后的 `buffer` 起点 **也** 是 2 MB 对齐的——这对 TMA / RDMA 的大块传输（要求地址按页对齐）至关重要。这也是为什么 4.1 节强调「`num_bytes` 不含 workspace」：workspace 是为了让 buffer 落在干净的对齐边界上而额外付出的「前导开销」。

#### 4.4.5 小练习与答案

**练习**：如果用户传了一个没对齐到 2 MB 的 `num_bytes`，会在哪里报错？
**答案**：在 C++ 构造函数的第一条断言 [csrc/elastic/buffer.hpp:98](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L98) —— `EP_HOST_ASSERT(num_buffer_bytes > 0 and num_buffer_bytes % symmetric::kNumAlignmentBytes == 0)`，会直接抛出 host 断言错误。

---

### 4.5 创建后暴露的拓扑属性

#### 4.5.1 概念说明

`ElasticBuffer` 创建完成后，会暴露两组、四个拓扑属性：

- **物理域**（按链路硬件划分）：
  - `num_rdma_ranks`：物理 RDMA 域的 rank 数（一组里只能靠 RDMA 互联的 rank 数）。
  - `num_nvlink_ranks`：物理 NVLink 域的 rank 数（一组里靠 NVLink 直连的 rank 数，即 NCCL 的 LSA 域）。
- **逻辑域**（按 DeepEP 通信策略划分）：
  - `num_scaleout_ranks`：逻辑 scaleout 域 rank 数（对应跨节点 RDMA 那一级）。
  - `num_scaleup_ranks`：逻辑 scaleup 域 rank 数（对应节点内 NVLink 那一级）。

总 rank 数 = `num_rdma_ranks * num_nvlink_ranks` = `num_scaleout_ranks * num_scaleup_ranks`。

hybrid 开关会改变逻辑域的划分方式（但不动物理域）。

#### 4.5.2 核心流程

构造函数结尾的两步探测：

```text
self.num_scaleout_ranks, self.num_scaleup_ranks = self.get_logical_domain_size()
self.scaleout_rank_idx = self.rank_idx // self.num_scaleup_ranks
self.scaleup_rank_idx  = self.rank_idx %  self.num_scaleup_ranks

self.num_rdma_ranks, self.num_nvlink_ranks = self.get_physical_domain_size()

torch.cuda.synchronize(); group.barrier(); torch.cuda.synchronize()   # 保证所有 rank 初始化可见
```

逻辑域由 `allow_hybrid_mode` 决定：

```text
if allow_hybrid_mode:
    num_scaleout_ranks = num_rdma_ranks     # RDMA 那一级当 scaleout
    num_scaleup_ranks  = num_nvl_ranks      # NVLink 那一级当 scaleup
else:
    num_scaleout_ranks = 1                  # 强制单级
    num_scaleup_ranks  = num_ranks          # 所有 rank 都当 scaleup（直接模式）
```

#### 4.5.3 源码精读

Python 侧在构造函数尾部探测并保存拓扑：

[deep_ep/buffers/elastic.py:356-362](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L356-L362) —— 先用 `get_logical_domain_size()` 拿到逻辑域并算出逻辑 rank 索引，再用 `get_physical_domain_size()` 拿到物理域。

最后的三同步保证所有 rank 的对称窗口都已注册完毕：

[deep_ep/buffers/elastic.py:364-367](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L364-L367) —— `cuda.synchronize + group.barrier + cuda.synchronize`，确保 NCCL 窗口注册（一个集合操作）在所有 rank 上都完成，否则后续 dispatch 可能读到未就绪的对端窗口。

C++ 侧的查询方法只是把 `nccl_context` 里探测到的值返回：

[csrc/elastic/buffer.hpp:172-178](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L172-L178) —— `get_physical_domain_size` 返回 `(num_rdma_ranks, num_nvl_ranks)`，`get_logical_domain_size` 返回 `(num_scaleout_ranks, num_scaleup_ranks)`。

真正的探测发生在 `NCCLSymmetricMemoryContext` 构造时：

[csrc/kernels/backend/nccl.cu:104-117](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L104-L117) —— `num_nvl_ranks = dev_comm.lsaSize`（NCCL 的 LSA 域，即 NVLink 全互联组），`num_rdma_ranks = num_ranks / num_nvl_ranks`；然后按 `allow_hybrid_mode` 把物理域映射成逻辑域；并算出 `is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)`。

#### 4.5.4 代码实践

**实践目标**：在两种拓扑下推理并验证逻辑域取值。

**操作步骤**：

```python
buf = ElasticBuffer(group, num_max_tokens_per_rank=4096, hidden=7168, num_topk=6)
print('num_ranks         =', buf.num_ranks)
print('num_rdma_ranks    =', buf.num_rdma_ranks)
print('num_nvlink_ranks  =', buf.num_nvlink_ranks)
print('num_scaleout_ranks=', buf.num_scaleout_ranks)
print('num_scaleup_ranks =', buf.num_scaleup_ranks)
```

**需要观察的现象与推理**：

| 物理拓扑 | num_ranks | num_nvl_ranks | num_rdma_ranks | hybrid=True → scaleout/scaleup | hybrid=False → scaleout/scaleup |
| --- | --- | --- | --- | --- | --- |
| 单机 8 卡 | 8 | 8 | 1 | 1 / 8 | 1 / 8 |
| 2 节点 × 8 卡 | 16 | 8 | 2 | 2 / 8 | 1 / 16 |

**预期结果**：单机时 `num_scaleout_ranks==1`，所以 dispatch 走「直接模式」（无 RDMA 流量）；多节点 + hybrid 时 `num_scaleout_ranks>1`，才会触发「hybrid 两级通信」。这就是 u1-l4 里「单机 SO 带宽恒为 0」的根本原因。**待本地验证**（多节点环境才能看到非平凡 scaleout）。

#### 4.5.5 小练习与答案

**练习**：`is_scaleup_nvlink` 什么时候为 `False`？
**答案**：当 `num_scaleup_ranks != num_nvl_ranks` 时。最典型的场景是 `allow_hybrid_mode=False` 且多节点：此时 `num_scaleup_ranks = num_ranks`（含跨节点 rank），而 `num_nvl_ranks` 只是节点内 NVLink 组，两者不等，`is_scaleup_nvlink=False`。这会反过来影响缓冲区布局（send buffer 不再被省略，见 [csrc/elastic/buffer.hpp:596-597](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L596-L597) 的 `is_scaleup_nvlink ? 0 : 1`）。

---

## 5. 综合实践

把本讲的五个模块串起来：写一个小的「缓冲区自检脚本」，**只构造、不真正 dispatch**，回答下面所有问题。

```python
# 示例代码：综合自检（需在已 init_process_group 的多进程环境里运行）
from deep_ep import ElasticBuffer

group = ...
hidden, num_topk, num_tokens = 7168, 6, 4096

# 1) 用 get_buffer_size_hint 估算
hint = ElasticBuffer.get_buffer_size_hint(group, num_tokens, hidden, num_topk)

# 2) 用估算值构造（方式 A），并打印所有拓扑与配置属性
buf = ElasticBuffer(group, num_bytes=hint,
                    num_max_tokens_per_rank=num_tokens,
                    allow_hybrid_mode=True,
                    allow_multiple_reduction=True,
                    prefer_overlap_with_compute=True)

print(f'num_bytes             = {buf.num_bytes}  (hint={hint}, 相等?{buf.num_bytes == hint})')
print(f'num_allocated_qps     = {buf.num_allocated_qps}')
print(f'allow_hybrid_mode     = {buf.allow_hybrid_mode}')
print(f'allow_multiple_reduction = {buf.allow_multiple_reduction}')
print(f'prefer_overlap_with_compute = {buf.prefer_overlap_with_compute}')
print(f'physical (rdma,nvl)   = {(buf.num_rdma_ranks, buf.num_nvlink_ranks)}')
print(f'logical  (scaleout,scaleup) = {(buf.num_scaleout_ranks, buf.num_scaleup_ranks)}')

# 3) 验证不变式
assert buf.num_rdma_ranks * buf.num_nvlink_ranks == buf.num_ranks
assert buf.num_scaleout_ranks * buf.num_scaleup_ranks == buf.num_ranks
```

完成后再回答：

1. `buf.num_bytes` 是否 2 MB 对齐？为什么必须对齐？（提示：4.4 节）
2. `buf.num_allocated_qps` 是 17/65/129 中的哪一个？为什么？（提示：4.3 节，取决于 hybrid 与网卡）
3. 你的环境是「直接模式」还是「hybrid 模式」？依据是哪个属性？（提示：`num_scaleout_ranks` 是否为 1）
4. 把 `allow_multiple_reduction` 改成 `False` 重新估算 `get_buffer_size_hint`，`num_bytes` 变大了吗？大致几倍？（提示：4.2 节）

> 这个练习不需要真正的 MoE 模型，也不需要 dispatch/combine，只需要一个能拉起 `torch.distributed` 的多卡环境。如果手边没有 GPU，至少完成「源码阅读型」部分：在 [csrc/elastic/buffer.hpp:81-140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L81-L140) 里逐行标注出每一步对应本讲哪个模块。

## 6. 本讲小结

- `ElasticBuffer` 支持两种等价的尺寸指定方式：显式 `num_bytes`（必须 2 MB 对齐）或 MoE 设置（`num_max_tokens_per_rank` + `hidden` + `num_topk`），后者由 `_C.calculate_elastic_buffer_size` 自动算出 2 MB 对齐的字节数。`num_bytes` 的语义是「GPU + CPU buffer，不含 workspace」。
- 三个开关各有分工：`allow_hybrid_mode` 决定多节点是否走两级通信并影响 QP 数与布局；`allow_multiple_reduction` 在 combine 精度与传输量间权衡并影响缓冲区大小；`prefer_overlap_with_compute` 决定 SM 数是否被压低以让出 SM 给计算流。
- QP 数量在 `num_allocated_qps=0` 时按「直接 17 / hybrid+fast-atomic 65 / hybrid 129」自动分配，多出的 1 个给 notify warps。
- C++ 侧的对称窗口布局是 `[[[Workspace] GPU buffer] CPU buffer]`：workspace 对齐 2 MB 放最前并清零，紧跟其后的 buffer 因此也落在 2 MB 对齐边界上，这对 TMA/RDMA 大块传输至关重要。
- 创建完成后暴露四类拓扑属性：物理域 `num_rdma_ranks` / `num_nvlink_ranks` 与逻辑域 `num_scaleout_ranks` / `num_scaleup_ranks`；`num_scaleout_ranks==1` 即直接模式，`>1` 即 hybrid 模式。构造函数末尾的三同步保证所有 rank 的 NCCL 窗口都已注册就绪。

## 7. 下一步学习建议

- 接下来按路线进入 **u2-l3**：在已经创建好的 `ElasticBuffer` 上调用 `dispatch` / `combine`，学习输入输出张量形状、FP8 的 `(data, scale)` 元组、以及 `EPHandle` 如何把 dispatch 与 combine 串起来。
- 如果想先搞清楚缓冲区大小到底是怎么一项一项算出来的，可以跳到 **u3-l2（缓冲区内存布局与大小解析计算）**，那里会逐段拆解 `TokenLayout` / `BufferLayout` / `WorkspaceLayout`。
- 如果对拓扑域划分（物理 vs 逻辑、hybrid 两级）想了解得更深，可先看 **u3-l1（物理域与逻辑域）**。
- 想理解 QP/SM 的解析式建模（本讲只看到结论）请看 **u3-l3（SM 与 QP 数量的解析式计算）**。
