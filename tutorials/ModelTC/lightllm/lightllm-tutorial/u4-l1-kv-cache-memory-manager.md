# KV Cache 内存管理

## 1. 本讲目标

本讲打开 LightLLM「token 级 KV Cache 管理」最核心的一块：**显存里那一大块 KV 缓冲区是谁管的、怎么按 token 分配回收、容量到底有多大**。学完后你应当能够：

- 说清 `MemoryManager` 维护的 `kv_buffer` 的四维形状（层 × token × KV × head_dim）与每一维的含义。
- 读懂 `KvCacheAllocator` 的 `alloc` / `free` / `free_all` 如何用一个连续区间 `[mark_start, mark_end)` 高效地分配与回收 token 槽位，并理解它返回的是「索引」而非显存本身。
- 解释 `profile_size` 如何根据 `mem_fraction` 自动估算 `max_total_token_num`，以及它与启动参数 `--max_total_token_num`、`--mem_fraction` 的关系。

本讲是第四单元（KV 缓存与前缀缓存）的基础，也为后续 RadixCache（u4-l2）、FP8 KV 量化（u6-l3）、多级 KV Cache（u6-l4）、PD 分离迁移（u7-l1）打底。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **KV Cache 是什么**：Transformer 自回归生成时，每个 token 在每一层都会算出一对 Key/Value 向量。后续 token 做注意力时要复用它们，所以必须缓存在显存里。一次请求的 KV 占用随序列长度线性增长。
- **token 级管理**：LightLLM 不像很多框架那样按「请求」分配一整段 KV，而是精确到**单个 token**——每个 token 拿一个独立的槽位编号（index），用完归还。这让 chunked prefill、RadixCache 复用、KV 迁移都成为可能。
- **「索引」与「显存」分离**：`req_to_token_indexs` 是一张「请求 → token → KV 槽位」的映射表（在 u3-l2 已出现）。注意力算子通过这张表去 `kv_buffer` 里取 K/V。本讲的 `MemoryManager` 就是 `kv_buffer` 的所有者，也是那些「槽位编号」的发放者。
- **基类初始化流水线**：在 u3-l1 中我们看到 `TpPartBaseModel.__init__` 是一条写死的流水线，其中 `_init_mem_manager()` 负责建 `MemoryManager`，并随后用 `mem_manager.size` 回填真正的 `max_total_token_num`。本讲正是打开这一步。

如果你还不熟悉 `mem_indexes`、`req_to_token_indexs` 这些词，可以先快速回顾 u3-l1、u3-l2。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `lightllm/common/kv_cache_mem_manager/mem_manager.py` | **内存管理器本体**。定义 `MemoryManager`，拥有那块巨大的 `kv_buffer`，负责容量估算、buffer 初始化、对外暴露 `alloc/free/free_all`。 |
| `lightllm/common/kv_cache_mem_manager/allocator.py` | **KV 分配器**。定义 `KvCacheAllocator`，用一个连续区间 `[mark_start, mark_end)` 管理哪些 token 槽位空闲、哪些已分出，是分配/回收的真正实现者。 |
| `lightllm/common/kv_cache_mem_manager/mem_utils.py` | **管理器选型**。`select_mem_manager_class()` 根据模型类型与 `--llm_kv_type` 选择用普通 `MemoryManager` 还是 FP8/INT8 等量化变体。 |
| `lightllm/common/kv_cache_mem_manager/operator/base.py`、`operator/normal.py` | **对外操作接口**。`NormalMemOperator` 等把 K/V 张量真正写入 `kv_buffer`（`copy_kv_to_mem_manager`），是「写后读」闭环里「写」的一侧。 |
| `lightllm/common/basemodel/basemodel.py` | 调用方。`_init_mem_manager()` 构造管理器，`_check_mem_size()` 用其 size 回填 `max_total_token_num`，`_check_max_len_infer()` 试跑时调用 `alloc/free/free_all`。 |
| `lightllm/utils/profile_max_tokens.py` | 容量估算的「离线版」工具，公式与本讲 `profile_size` 同源，可独立运行。 |

一句话概括分工：`MemoryManager` 是「仓库管理员」（拥有一整块仓库），`KvCacheAllocator` 是「货架调度员」（知道哪些格子空着），`NormalMemOperator` 是「搬运工」（把货物 K/V 实际搬进指定格子）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**内存管理器**、**KV 分配器**、**容量估算**。

### 4.1 内存管理器（MemoryManager）：KV buffer 的形状与层结构

#### 4.1.1 概念说明

`MemoryManager` 是 LightLLM 里所有 KV Cache 显存的「总容器」。它在模型初始化时被创建一次，常驻到服务结束。它的核心资产是一块预先一次性分配的大显存张量 `kv_buffer`，推理过程中**不再申请新显存、也不再释放显存**，只是在「分配」与「回收」之间循环——这正是高性能推理框架的标准做法（避免运行时反复 `cudaMalloc`）。

为什么要把所有层的 KV 放在一个张量里？因为：

1. **一次性分配**比逐层分配更省碎片、更快。
2. 注意力算子（Triton/FlashInfer）喜欢连续、形状规整的输入，一个 4 维张量 `(layer_num, token, 2*head_num, head_dim)` 非常便于 kernel 按 layer 维度切片。
3. 「索引」机制天然适配：只要给一个 token 槽位编号 `i`，所有层在 `kv_buffer[:, i]` 这个切片上就能取到该 token 全部层的 K/V。

#### 4.1.2 核心流程

`MemoryManager.__init__` 做了五件事，顺序固定：

```text
传入参数: size(可空), dtype, head_num, head_dim, layer_num, mem_fraction
   │
   ├─ 1. 保存基本形状信息
   ├─ 2. profile_size(mem_fraction)  # size 为空时，按显存余量自动估算 size
   ├─ 3. KvCacheAllocator(self.size)  # 用最终 size 建分配器
   ├─ 4. _init_buffers(...)           # 分配那块巨大的 kv_buffer 显存
   ├─ 5. HOLD_TOKEN_MEMINDEX = size   # 记下一个「永不外借」的特殊槽位
   └─ 6. operator = operator_class(self)  # 建搬运工接口
```

其中第 4 步 `_init_buffers` 是真正吃显存的地方。注意一个细节：buffer 的 token 维是 `size + 1`，多出来的 1 个槽位**永远不对外分配**，专门留给特殊运行模式做 padding（见 4.1.3）。

`kv_buffer` 的四维形状是理解一切的钥匙：

```text
kv_buffer: (layer_num, size + 1, 2 * head_num, head_dim)
              │          │           │              │
              │          │           │              └─ 每个头的维度（如 128）
              │          │           └─ K 头 + V 头 拼在一起（2 倍 head_num）
              │          └─ token 槽位编号 0..size
              └─ 第几层（含可能的 MTP 额外层）
```

读 K/V 时，靠 `get_att_input_params(layer_index)` 把拼在一起的 `2*head_num` 拆成 K（前半）和 V（后半）。

#### 4.1.3 源码精读

先看 `__init__` 的全貌与五步顺序：

[lightllm/common/kv_cache_mem_manager/mem_manager.py:25-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L25-L51) —— `MemoryManager` 类定义与 `__init__`：先 `profile_size` 估算 size，再建 `KvCacheAllocator`，再 `_init_buffers`，最后建 `operator`。

`_init_buffers` 揭示了真正的形状与那个「+1」的设计意图（注释解释了多出来的槽位用于 overlap/microbatch 等 padding 场景）：

[lightllm/common/kv_cache_mem_manager/mem_manager.py:81-86](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L81-L86) —— 分配形状为 `(layer_num, size + 1, 2 * head_num, head_dim)` 的 `kv_buffer`；`+1` 是预留槽，索引恒为 `size`，存于 `HOLD_TOKEN_MEMINDEX`。

注意力算子取 K/V 的拆分方式（这是「读」KV 的入口，连接到 u3-l3 的注意力核）：

[lightllm/common/kv_cache_mem_manager/mem_manager.py:53-56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L53-L56) —— `get_att_input_params(layer_index)`：把 `kv_buffer[layer_index]` 沿 head 维切成前半 K、后半 V 返回。

「写」KV 的入口在搬运工 `NormalMemOperator` 里，用 `destindex_copy_kv` 把算出的 K/V 按 `mem_index` 散射进 `kv_buffer[layer_index]`：

[lightllm/common/kv_cache_mem_manager/operator/normal.py:16-25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/normal.py#L16-L25) —— `copy_kv_to_mem_manager`：把本轮新算出的 `kv` 张量按目标索引 `mem_index` 拷进 `kv_buffer[layer_index]`，是「写后读」闭环里的「写」。

> 说明：本讲聚焦 `kv_buffer` 的形状与管理；「写后读」的完整闭环（`_post_cache_kv` 写、注意力核读）在 u3-l3 已讲，这里只需知道写入靠 `operator` 即可。

#### 4.1.4 代码实践

**实践目标**：在不启动服务的前提下，通过阅读源码回答「一个 Llama 类模型的 `kv_buffer` 到底长什么样」。

**操作步骤**：

1. 假设一个模型：`num_attention_heads=32`、`n_embed=4096`（故 `head_dim=128`）、`n_layer=32`、TP=1、dtype=bf16（占 2 字节）。
2. 按 `get_cell_size` 的公式手算「一个 token 全部层的 KV 占多少字节」。
3. 在 `mem_manager.py` 中确认你的公式与源码一致。

**需要观察的现象**：`get_cell_size` 返回的是「单 token 全层」的字节数，正好等于 `kv_buffer[:, :1, :, :].element_size() * kv_buffer[:, :1, :, :].numel() / 1`（即一个 token 切片的总字节）。你可以用它反推 `kv_buffer` 的总显存。

**预期结果**：对上面的模型，`cell_size = 2 × 32 × 128 × 32 × 2 = 524288` 字节 = 0.5 MiB/token。即每缓存 1 个 token 的全层 KV，占 0.5 MiB 显存。

```python
# 示例代码：手算 cell_size（不依赖 GPU，可纯 CPU 验证公式）
layer_num, head_num, head_dim = 32, 32, 128
element_size_bytes = 2  # bf16
cell_size = 2 * head_num * head_dim * layer_num * element_size_bytes
print(cell_size)  # 期望 524288
print(cell_size / 1024**2, "MiB/token")
```

> 本实践为「源码阅读 + 公式手算型」，无需启动服务；若想用真实 GPU 验证，可写一个小脚本 `torch.empty((layer_num, size+1, 2*head_num, head_dim), dtype=torch.bfloat16, device='cuda')` 并对比 `torch.cuda.memory_allocated()` 的增量。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kv_buffer` 的 token 维是 `size + 1` 而不是 `size`？多出的那个槽位有什么用？

**参考答案**：多出的 1 个槽位（索引恒为 `size`，即 `HOLD_TOKEN_MEMINDEX`）永不对外分配，专门留给 overlap / microbatch / 多 DP 等 padding 场景，让某些需要被「占位」的请求能拿到一个合法但不会被真实读写冲突的索引。这与 `req_manager` 里的 `HOLD_REQUEST_ID` 设计思路一致。

**练习 2**：`get_att_input_params` 为什么能把 K 和 V 用一次切片同时取出？

**参考答案**：因为 `kv_buffer` 在构造时把 K 头和 V 头沿同一个维度拼在一起（`2 * head_num`），前半 `[:, :head_num, :]` 是 K，后半 `[:, head_num:, :]` 是 V。这种 K/V 交错布局让一次切片就能取出同一 token 的完整 K/V，方便注意力 kernel。

---

### 4.2 KV 分配器（KvCacheAllocator）：alloc / free / free_all

#### 4.2.1 概念说明

`MemoryManager` 虽然拥有显存，但「哪些槽位现在空着、哪些被借走了」这件事交给了一个专门的类 `KvCacheAllocator`。它是 token 级管理的真正执行者。

`MemoryManager` 的 `alloc/free/free_all` 都只是**转发**给 `self.allocator`：

```python
def alloc(self, need_size):   return self.allocator.alloc(need_size)
def free(self, free_index):   return self.allocator.free(free_index)
def free_all(self):           return self.allocator.free_all()
```

这种「拥有者 + 调度器」的分离，让 FP8/INT8 等量化变体可以只换 `MemoryManager` 子类（重写 buffer 形状与 operator），而复用同一套分配逻辑。

分配器的核心思路非常巧妙：它**不是**用链表或位图管理空闲块，而是维护一个「**已分配区间 `[0, mark_start)` 与空闲区间 `[mark_start, mark_end)`**」的简单模型。`mem_state` 是一个长度为 `size` 的数组，`mem_state[i]` 存的是「第 i 个槽位对应的 token 编号」——但因为分配/回收的顺序特性，它实际上就是一个**可重写的栈**。

#### 4.2.2 核心流程

把分配器想象成一个**只进只出一端的栈**（栈底在 0，栈顶指针是 `mark_start`）：

```text
初始: mem_state = [0, 1, 2, ..., size-1],  mark_start = 0
      ┌───┬───┬───┬───┬─────────┬───────┐
      │ 0 │ 1 │ 2 │ 3 │  ...    │ size-1│   全部空闲
      └───┴───┴───┴───┴─────────┴───────┘
        ▲ mark_start=0

alloc(need_size=2): 取 mem_state[0:2] = [0,1] 返回, mark_start 推进到 2
      ┌───┬───┬───┬───┬─────────┬───────┐
      │ 0 │ 1 │ 2 │ 3 │  ...    │ size-1│
      └───┴───┴───┴───┴─────────┴───────┘
                ▲ mark_start=2   (0,1 已借出)

free([0,1]): 把 [0,1] 写回 mem_state[0:2], mark_start 退回到 0
      ┌───┬───┬───┬───┬─────────┬───────┐
      │ 0 │ 1 │ 2 │ 3 │  ...    │ size-1│   全部回收
      └───┴───┴───┴───┴─────────┴───────┘
        ▲ mark_start=0
```

关键点：

- **alloc 是顺序「弹出」**：从 `mark_start` 开始连续取 `need_size` 个，把 `mark_start` 往右推。
- **free 是「压回栈顶」**：归还的索引被写回 `[mark_start - len, mark_start)`，`mark_start` 往左退。注意它**不要求**归还的索引正好是上次 alloc 的那几个——只要归还数量正确即可。这使得「不同请求归还各自那段」也能正常工作，因为栈顶位置始终对齐。
- **free_all 是全量复位**：把 `mem_state` 重置为 `[0,1,...,size-1]`，`mark_start` 归零。

返回值是个 `torch.Tensor`（在 CPU 上、pin_memory），里面是要分配的槽位编号。调用方（如 basemodel）会 `.cuda()` 后送进 `ModelInput.mem_indexes`。

还有两个对外副作用值得注意：

1. `can_use_mem_size`：剩余可用 token 数，每次 alloc/free 都更新。
2. `shared_can_use_token_num`：把 `can_use_mem_size` 写进**共享内存**（`SharedInt`），让 Router 进程能读到「现在还剩多少 KV 槽位」，从而做精确调度估算（这正是 u2-l6 / u4-l3 里 token 负载估算的数据来源之一）。

#### 4.2.3 源码精读

先看分配器的状态初始化（注意它立刻把可用数写进共享内存）：

[lightllm/common/kv_cache_mem_manager/allocator.py:11-33](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/allocator.py#L11-L33) —— `KvCacheAllocator.__init__`：`mem_state` 初始化为 `[0,1,...,size-1]`，`mark_start=0`、`mark_end=size`；并建一个 `SharedInt` 把可用 token 数发布给 Router 进程。

`alloc` 的核心：从 `mark_start` 顺序取、推进游标、更新可用数与共享内存；返回值用一个双倍长度的环形缓冲 `_mem_state_return` 承载，避免异步并发下的内存竞争：

[lightllm/common/kv_cache_mem_manager/allocator.py:35-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/allocator.py#L35-L53) —— `alloc(need_size)`：校验余量 → 切片 `mem_state[start:end]` → 推进 `mark_start` → 更新 `can_use_mem_size` 与共享值 → 经 `_mem_state_return` 缓冲返回索引张量。

`free` 的核心：把归还的索引写回 `[mark_start-len, mark_start)`、回退 `mark_start`、更新可用数；支持传 list 或 tensor：

[lightllm/common/kv_cache_mem_manager/allocator.py:55-79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/allocator.py#L55-L79) —— `free(free_index)`：把索引写回栈顶、`mark_start -= len`、`can_use_mem_size += len`、刷新共享值。

`free_all` 全量复位：

[lightllm/common/kv_cache_mem_manager/allocator.py:81-87](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/allocator.py#L81-L87) —— `free_all()`：`mem_state` 重置为 `[0..size-1]`，所有游标归零，可用数恢复为 `size`。

最后看调用方——basemodel 在试跑（`_check_max_len_infer`）里如何用这三个接口走一个完整闭环：

[lightllm/common/basemodel/basemodel.py:1045-1078](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L1045-L1078) —— 启动期模拟一次最大长度 prefill：`alloc(batch_max_tokens)` 拿索引 → 喂进 `ModelInput.mem_indexes` 跑前向 → 结束 `free_all()` 全量回收。一次 `alloc → 使用 → free_all` 的闭环清晰可见。

#### 4.2.4 代码实践

**实践目标**：亲手用一段独立代码复现分配器的「栈式」行为，验证 alloc/free 的索引规律，不依赖 GPU。

**操作步骤**：

1. 把 `allocator.py` 里 `KvCacheAllocator` 的核心逻辑抽成一个**纯 Python 版本**（去掉 `SharedInt`、`torch`、pin_memory，只保留 `mem_state` 列表 + `mark_start`）。
2. 按 4.2.2 的图示做：`alloc(2)` → `alloc(1)` → `free([0,1])`，每步打印 `mark_start` 与返回索引。

**需要观察的现象**：alloc 返回的索引一定是**连续递增**的（从 `mark_start` 起）；free 后再 alloc，会复用刚归还的低位索引（因为 `mark_start` 退回去了）。

**预期结果**：

```python
# 示例代码：纯 Python 复现分配器核心逻辑（非项目原码，仅为演示栈式分配）
class MiniAllocator:
    def __init__(self, size):
        self.mem_state = list(range(size))
        self.mark_start = 0
        self.can_use = size
    def alloc(self, n):
        ans = self.mem_state[self.mark_start:self.mark_start + n]
        self.mark_start += n
        self.can_use -= n
        return ans
    def free(self, idx):
        n = len(idx)
        self.mem_state[self.mark_start - n:self.mark_start] = idx
        self.mark_start -= n
        self.can_use += n

a = MiniAllocator(8)
print(a.alloc(2))   # [0, 1]
print(a.alloc(1))   # [2]
a.free([0, 1])
print(a.alloc(3))   # [0, 1, 2]  —— 复用了刚归还的低位
```

**待本地验证**：上面的 `free([0,1])` 之后 `mark_start` 退回到 1，于是下一次 `alloc(3)` 从 1 号位开始写、读到 `[1,2,...]`？请运行确认到底返回什么，并解释为什么（提示：仔细看 `free` 写回的位置是 `[mark_start-n, mark_start)`，归还 2 个会把栈顶从 3 退到 1，再把 `[0,1]` 写进 `[1,3)`……你会发现索引并非严格递增，这正说明「栈式」回收后索引顺序可能被打乱，但**唯一性**与**可用数**始终正确）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `alloc` 返回的索引要经过一个 `_mem_state_return` 环形缓冲，而不是直接返回 `mem_state[start:end]` 的视图？

**参考答案**：直接返回视图的话，上一次 alloc 返回的张量与本次 alloc 切到的内存区域可能重叠；在异步（双 batch overlap、多流）场景下，旧返回值还没被消费完就被新 alloc 覆盖，会造成数据竞争。`_mem_state_return` 长度是 `size*3`（足够大），通过 `_return_start` 游标给每次 alloc 分配一段**独立的**目标缓冲再 `copy_` 过去，保证返回的张量彼此不重叠。

**练习 2**：`free` 时为什么用 `assert start >= 0` 来校验？它在防什么错？

**参考答案**：`start = mark_start - len(free_index)`。如果归还的数量超过了已分配的数量（`len(free_index) > mark_start`），`start` 会变负，说明逻辑上「归还了从没借出去的槽位」——这是上层调用的 bug（比如对同一批索引 free 了两次）。assert 用于在开发期尽早暴露这种双重释放错误。

---

### 4.3 容量估算（profile_size 与 mem_fraction）

#### 4.3.1 概念说明

启动 LightLLM 时，`--max_total_token_num` 是**可选**的。如果你不填，框架会**自动探测**：在加载完模型权重后，看看 GPU 还剩多少显存，按每个 token 的 KV 占用反推出「最多能缓存多少个 token」，这就是 `max_total_token_num`。这个探测过程叫 `profile_size`，是本模块的核心。

它依赖两个输入：

- `mem_fraction`（默认 0.9，但 basemodel 默认传入 0.8，见启动参数 `--mem_fraction`）：表示「想把整张卡总显存的多少比例用于 模型权重 + KV Cache」。剩下的 `(1 - mem_fraction)` 留给激活值、临时 buffer、CUDA Graph 等非 KV 开销。
- 单 token KV 字节数 `cell_size`（来自 4.1 的 `get_cell_size`）。

如果你显式指定了 `--max_total_token_num`（即 `size` 不为 None），`profile_size` 会直接 return，跳过探测——这时容量由你说了算，但你要自己保证别 OOM。

#### 4.3.2 核心流程

```text
profile_size(mem_fraction):
  if self.size is not None: return        # 用户已指定，跳过
  empty_cache()                            # 先回收碎片
  free_gpu = get_available_gpu_memory()    # 加载权重后的剩余显存(GB), 跨 rank 取 MIN
  total_gpu = get_total_gpu_memory()       # 整卡总显存(GB)
  reserved = total_gpu * (1 - mem_fraction)# 留给激活/临时 buffer 的部分
  available_for_kv = free_gpu - reserved   # 真正能分给 KV 的显存(GB)
  cell_size = get_cell_size()              # 单 token 全层 KV 字节数
  size = int(available_for_kv * 1024^3 / cell_size)
  if world_size > 1: size = all_reduce(MIN)(size)  # TP 各 rank 对齐到最小值
```

用公式表达单 token 显存与可缓存 token 数：

\[ \text{cell\_size} = 2 \times \text{head\_num} \times \text{head\_dim} \times \text{layer\_num} \times \text{elemsize}(\text{dtype}) \]

\[ \text{size} = \left\lfloor \frac{\big(\text{free\_gpu} - \text{total\_gpu}\cdot(1-\text{mem\_fraction})\big)\times 1024^{3}}{\text{cell\_size}} \right\rfloor \]

其中 `free_gpu`、`total_gpu` 单位均为 GB，故乘 `1024^3` 转字节。两个关键设计：

1. **跨 rank 取 MIN**：张量并行下每张卡分到的 head 数相同，但各卡剩余显存可能不同（因权重切分不均或别的进程占用）。为保证所有 rank 的 `kv_buffer` 形状一致（NCCL all-reduce 等通信要求同形），取最小值兜底。
2. **`(1-mem_fraction)` 的双重含义**：它既是从「剩余显存」里再抠掉一块（给激活留余量），也整体把「模型+KV」限制在 `mem_fraction` 比例内。这也是为什么 `mem_fraction` 调太大（如 0.95）容易在运行时 OOM——激活没地方放了。

#### 4.3.3 源码精读

`profile_size` 的完整实现，含跨 rank 取 MIN 与日志输出：

[lightllm/common/kv_cache_mem_manager/mem_manager.py:61-79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L61-L79) —— `profile_size(mem_fraction)`：用户未指定 size 时，按 `available_memory * 1024^3 / cell_size` 估算，并 all_reduce MIN 对齐各 rank，最后打印「可用显存 / 单 token KV 大小 / 估算出的 max_total_token_num」。

单 token KV 字节数的计算（`get_cell_size`）：

[lightllm/common/kv_cache_mem_manager/mem_manager.py:58-59](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L58-L59) —— `get_cell_size()`：`2 * head_num * head_dim * layer_num * elemsize(dtype)`，因子 2 是 K 和 V 各一份。

获取 GPU 显存的两个底层函数（注意 `get_available_gpu_memory` 跨 rank 也取 MIN）：

[lightllm/utils/profile_max_tokens.py:13-31](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/profile_max_tokens.py#L13-L31) —— `get_available_gpu_memory`（加载权重后剩余，跨 rank MIN）与 `get_total_gpu_memory`（整卡总量），单位均转成 GB。

再看 basemodel 如何构造管理器并把估算结果回填到 `max_total_token_num`：

[lightllm/common/basemodel/basemodel.py:191-204](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L191-L204) —— `_init_mem_manager()` 把（可能为 None 的）`max_total_token_num` 连同 head_num/head_dim/layer_num/mem_fraction 传给 `select_mem_manager_class()(...)`；构造完成后 `_check_mem_size()` 用 `mem_manager.size` 回填真正的 `max_total_token_num`。

注意 `_init_mem_manager` 里 `layer_num` 是 `config["n_layer"] + get_added_mtp_kv_layer_num()`：若启用 MTP 推测解码，KV buffer 会多出 draft 层的层数。`head_num` 是 `num_attention_heads // tp_world_size_`（TP 切分后本卡持有的头数）。

最后，`select_mem_manager_class` 决定用哪个管理器（普通 or 量化变体）：

[lightllm/common/kv_cache_mem_manager/mem_utils.py:18-57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_utils.py#L18-L57) —— `select_mem_manager_class()`：先按模型族（Deepseek3_2 / Deepseek2）选专用类，否则按 `--llm_kv_type`（int8kv / int4kv / fp8kv_sph / fp8kv_spt / None）选量化或普通 `MemoryManager`。普通模型默认走 `MemoryManager`。

#### 4.3.4 代码实践

**实践目标**：用项目自带的离线工具，在不启动服务的情况下，预测某个模型在某张卡上的推荐 `max_total_token_num`，并与 `profile_size` 的公式互相印证。

**操作步骤**：

1. 找到工具入口：`lightllm/utils/profile_max_tokens.py` 的 `main()`，它接收 `--model_dir`、`--tp`、`--mem_fraction`、`--kv_data_type` 等参数。
2. 阅读其中的 `get_total_token_nums`（[profile_max_tokens.py:100-120](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/profile_max_tokens.py#L100-L120)）：它先用 HuggingFace 实测模型权重占用 `model_size`，再按 `(gpu_total * mem_fraction - model_size) / per_token_kv` 算出推荐 token 数。
3. 在有 GPU 的环境运行（需能加载模型）：

   ```bash
   python -m lightllm.utils.profile_max_tokens \
       --model_dir <你的模型目录> --tp 1 --mem_fraction 0.9 --kv_data_type bf16
   ```

**需要观察的现象**：它会依次打印「模型权重占用」「单卡总显存」「单 token KV 占用」「推荐 max_total_token_num」。把这个推荐值与 4.3.2 公式的手算结果对比。

**预期结果**：推荐值约为 `(单卡总显存 × mem_fraction − 模型权重) / (单 token KV)`。注意此离线工具用的是「`gpu_total × mem_fraction − model_size`」，与运行时 `profile_size` 的「`free_gpu − total_gpu×(1−mem_fraction)`」形式上不同但**等价**（都等价于 `total_gpu×mem_fraction − 已用部分`，其中 `已用部分≈model_size`）。

**待本地验证**：因依赖真实 GPU 与模型权重，请在有卡的机器上运行确认具体数值；若暂无 GPU，可只做公式对照，标注「待本地验证」。

> 说明：如果运行时报模块导入错误，请确保已按 u1-l2 安装好 `requirements.txt` 依赖并在仓库根目录执行。

#### 4.3.5 小练习与答案

**练习 1**：某次启动你设了 `--mem_fraction 0.95`，结果服务起来后跑一个大 batch 就 OOM 了。结合 `profile_size` 的公式解释原因。

**参考答案**：`mem_fraction=0.95` 意味着 `reserved = total_gpu × 0.05` 极小。`profile_size` 算出的 `available_for_kv = free_gpu − 0.05×total_gpu` 偏大，于是 `size`（max_total_token_num）被估得偏高，KV buffer 几乎吃光了剩余显存。等到真正推理时，激活值、注意力中间张量、CUDA Graph 重放所需的临时显存无处安放，就会 OOM。正确做法是下调 `mem_fraction` 或显式指定更小的 `--max_total_token_num`（启动失败提示也这么建议，见 basemodel `_check_max_len_infer` 的异常文案）。

**练习 2**：为什么 `profile_size` 在 `world_size > 1` 时要做 `all_reduce(MIN)`？只取当前 rank 的估算值会怎样？

**参考答案**：张量并行要求各 rank 的 `kv_buffer` 形状完全一致（否则后续 NCCL 通信、注意力 kernel 的形状假设会崩）。但各卡的剩余显存可能不同（权重切分不均、其他进程占用），各算各的会得到不同的 `size`。取 MIN 保证所有 rank 都用「最穷那张卡」的容量，形状一致、安全兜底。这也意味着 TP 下 KV 总容量受限于剩余显存最少的那张卡。

## 5. 综合实践

**任务**：把三个模块串起来，画一张「从启动到推理」的 KV 内存生命周期图，并用源码行号佐证。

请按下列步骤完成：

1. **启动期（容量决定）**：从 `api_server.py` 启动 → basemodel `__init__` 流水线 → `_init_mem_manager`（[basemodel.py:191-201](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L191-L201)）→ `MemoryManager.__init__`（[mem_manager.py:29-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L29-L51)）→ `profile_size` 估算 size（[mem_manager.py:61-79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L61-L79)）→ `_init_buffers` 分配 `kv_buffer`（[mem_manager.py:81-86](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L81-L86)）→ `_check_mem_size` 回填 `max_total_token_num`（[basemodel.py:203-204](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L203-L204)）。在图上标出「size 在哪一步从 None 变成具体数字」。

2. **试跑期（闭环验证）**：`_check_max_len_infer`（[basemodel.py:1045-1078](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L1045-L1078)）做一次 `alloc → forward → free_all`，确认最大长度不会 OOM。

3. **运行期（分配回收）**：每次 prefill，Router 调度后 backend 给新 batch 调 `alloc`（[allocator.py:35-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/allocator.py#L35-L53)）拿 `mem_indexes` → 写入 `req_to_token_indexs` → 各层 `operator.copy_kv_to_mem_manager` 把 K/V 散进 `kv_buffer`（[operator/normal.py:16-25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/normal.py#L16-L25)）→ 注意力核经 `get_att_input_params` 读回（[mem_manager.py:53-56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L53-L56)）→ 请求结束后 `free` 归还索引（[allocator.py:55-79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/allocator.py#L55-L79)）。

**交付物**：

- 一张时序图（手绘或文字描述均可），含「启动 / 试跑 / 运行」三段。
- 在每段旁标注对应的源码文件:行号。
- 用一句话回答：「`mem_manager.size`、`max_total_token_num`、`kv_buffer` 的 token 维长度」三者的关系。

**预期结论**：三者的关系是——`mem_manager.size` 经 `profile_size` 确定后，回填为 `max_total_token_num`（对外代表「系统最多缓存多少 token」）；而 `kv_buffer` 的 token 维长度是 `size + 1`（多 1 个是 `HOLD_TOKEN_MEMINDEX` 预留槽）。`can_use_mem_size` 则是运行时实时变化的「当前还能分多少 token」。

## 6. 本讲小结

- `MemoryManager` 是 KV Cache 显存的总容器，核心资产是 4 维张量 `kv_buffer: (layer_num, size+1, 2*head_num, head_dim)`；token 维多 1 个是 `HOLD_TOKEN_MEMINDEX` 预留槽。
- 分配/回收由 `KvCacheAllocator` 用「连续区间 `[mark_start, mark_end)` + 可重写栈」实现：`alloc` 顺序弹出、`free` 压回栈顶、`free_all` 全量复位，全程**不申请新显存**。
- `alloc` 返回的是「索引张量」（CPU pin_memory，经环形缓冲避免并发竞争），调用方 `.cuda()` 后既用于写 K/V，也登记进 `req_to_token_indexs` 映射表。
- `profile_size` 在用户未指定 `--max_total_token_num` 时自动估算：`size = (free_gpu − total_gpu×(1−mem_fraction)) × 1024³ / cell_size`，并跨 TP rank 取 MIN 对齐。
- `can_use_mem_size` 经 `SharedInt` 写入共享内存，供 Router 做精确的 token 负载估算与调度准入（连接 u2-l6 / u4-l3）。
- `select_mem_manager_class` 按模型族与 `--llm_kv_type` 在普通 `MemoryManager` 与 FP8/INT8 量化变体间选型，量化变体只改 buffer 形状与 operator，复用同一套分配逻辑。

## 7. 下一步学习建议

本讲建立了「KV 槽位的拥有、分配、回收、容量」基础。接下来建议：

- **u4-l2 RadixCache 前缀缓存机制**：看 RadixCache 如何在 `MemoryManager` 之上做「前缀复用」——多个请求共享同一段 KV 时，引用计数与 evict 如何与 `alloc/free` 配合。
- **u4-l3 Token 负载估算与调度配额**：看 Router 怎么读 `shared_can_use_token_num` 与 `max_total_token_num` 来决定「这一拍还能不能塞新请求」。
- **u6-l3 FP8 KV Cache 量化**：看 `FP8StaticPerTensor/PerHeadQuantMemManager` 如何重写 `_init_buffers` 与 operator，在同一个分配器框架下把 KV 压成 FP8。
- **u7-l1 PD 分离部署与 KV 迁移**：看 `kv_move_buffer`、`write_mem_to_page_kv_move_buffer` 等如何把 `kv_buffer` 里的 KV 分页搬运到别的节点。

阅读建议：先把本讲的 `kv_buffer` 形状和「索引」机制记牢，后续所有 KV 相关特性（缓存复用、量化、迁移）都是在这套「索引 + 大 buffer」的地基上搭建的。
