# Engine 初始化与显存管理

## 1. 本讲目标

本讲聚焦 Mini-SGLang 在「每张 GPU 上」的执行核心——`Engine`。读完本讲，你应该能够：

- 说清 `Engine.__init__` 的执行顺序：从 CUDA 设备/流设定、通信初始化、模型建图、KV cache 池分配，到 page_table 与 CUDA graph 捕获。
- 理解「meta device 建图 + load_state_dict 装载真实权重」这一低显存建图手法，以及它为何能把加载峰值从约 2× 参数量降到约 1×。
- 手推 `_determine_num_pages` 的显存估算公式，给定模型结构就能算出能分到多少页 KV cache。
- 理解 `_sync_get_memory` 如何用一个 `all_reduce` 同时拿到跨 rank 的最小/最大可用显存，并据此检测显存失衡。

本讲是 u4（Scheduler）的下游：Scheduler 的主循环每轮都要调用 `Engine.forward_batch`，而 `Engine` 正是在 `__init__` 阶段把一切显存资源与计算设施备齐。

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的基础概念。

**显存预算为什么是头等大事。** LLM 推理时，GPU 显存要同时装下三样东西：(1) 模型权重（几 GB 到几百 GB）；(2) KV cache——每个 token 在每一层都会产生一对 Key/Value 向量，缓存下来供后续 token 做注意力复用，这部分随并发请求数与序列长度增长，是动态大头；(3) 激活值、临时缓冲、CUDA Graph 工作区等运行时开销。Mini-SGLang 的策略是：先把权重装进去，再「把剩余显存尽量切给 KV cache」，但又不能切满——必须给运行时开销留余量。本讲讲的 `memory_ratio`、`_determine_num_pages` 就是这套预算的算法实现。

**meta device 是什么。** PyTorch 有一个特殊的 `meta` 设备：落在 `meta` 上的张量**不分配任何真实显存**，只记录形状（shape）、步幅（stride）和数据类型（dtype）。你可以把整个模型在 `meta` 上「搭建」一遍，得到一棵完整的模块树和一个完整的 `state_dict()`（参数名→形状映射），却几乎不花显存。这给了我们一种很省内存的建图方式：先在 meta 上搭骨架，再把真实权重「灌」进去。

**张量并行（TP）对显存的硬约束。** 回顾 u1-l4/u4-l2：每张 GPU 跑一个 Scheduler 进程，各进程用 NCCL/PyNCCL 做 `all_reduce`/`all_gather`。这要求**所有 rank 的张量形状完全一致**。KV cache 的页数（`num_pages`）如果不一致，各 rank 在前向时 attention 维度就对不上，`all_reduce` 会直接报错。因此 KV cache 页数不能「每张卡各算各的」，必须跨 rank 取一个统一值——这就是 `_sync_get_memory` 存在的根本原因。

> 术语：下文把「一张 GPU 上一个进程的执行核心」称为一个 `Engine`；把「KV cache 的最小分配单元」称为一页（page），一页容纳 `page_size` 个 token。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `python/minisgl/engine/engine.py` | `Engine` 类主体，包含 `__init__`、`_init_communication`、`_load_weight_state_dict`、`_determine_num_pages`、`_sync_get_memory`、`forward_batch`、`_adjust_config` |
| `python/minisgl/engine/graph.py` | `GraphRunner`（CUDA Graph 捕获/回放），以及 `get_free_memory`、`mem_GB` 等显存工具函数 |
| `python/minisgl/engine/config.py` | `EngineConfig`，提供 `page_size`、`memory_ratio`、`num_page_override`、`cuda_graph_bs` 等字段 |
| `python/minisgl/kvcache/mha_pool.py` | `MHAKVCache`，定义 KV buffer 的 6 维布局，是 `_determine_num_pages` 公式的「事实依据」 |
| `python/minisgl/layers/base.py` | `BaseOP.load_state_dict`，meta→真实权重的递归装载实现 |
| `python/minisgl/utils/misc.py` | `div_even`，处理 GQA 下 KV head 的 TP 切分与复制 |

## 4. 核心概念与源码讲解

本讲的四个最小模块按 `__init__` 的执行顺序展开：

1. **Engine 初始化**——整体编排顺序与各阶段显存测量点。
2. **meta 建图**——用 meta device 零显存搭骨架再灌真实权重。
3. **`_determine_num_pages`**——把剩余显存换算成 KV cache 页数。
4. **`_sync_get_memory`**——跨 rank 对齐显存并检测失衡。

---

### 4.1 Engine 初始化

#### 4.1.1 概念说明

`Engine` 是「GPU 侧的执行器」：每个 Scheduler 进程内部持有一个 `Engine`，它负责持有模型、KV cache、attention 后端、采样器与 CUDA Graph，并提供 `forward_batch` 给主循环调用。`Engine.__init__` 不是随便堆叠的初始化代码，而是一条**精心排序的显存装配流水线**——先量 baseline 显存、再装模型、再用差值算 KV cache、最后捕获 graph。顺序错了，要么显存测不准，要么捕获 graph 时 OOM。

#### 4.1.2 核心流程

`Engine.__init__` 的执行顺序如下（伪代码）：

```
1. assert CUDA 未初始化                 # 保证 Engine 是进程里第一个碰 CUDA 的
2. set_tp_info(rank, size)             # 登记本进程的 TP 身份
3. _adjust_config(config)              # 按硬件自动选 attention/moe 后端、page_size
4. 设 device=cuda:{rank}、建 engine stream、设种子 42、建 Context 并注册为全局
5. _init_communication()               # 初始化进程组（gloo + NCCL/PyNCCL）
6. init_free = _sync_get_memory()[1]   # 量「装模型前」的可用显存（跨 rank 对齐后的 max）
7. 用 meta device 建模型 + load_state_dict 装真实权重
8. num_pages = _determine_num_pages(init_free, config)   # 用 baseline 与装模型后的差值算页数
9. 创建 KV cache 池（num_pages + 1，多 1 页给 dummy）
10. 建 page_table（行=max_running_req+1，列=对齐后的 max_seq_len）
11. 建 attention / moe 后端、采样器
12. post_free = _sync_get_memory()[0]  # 量「全部装完后」的剩余显存（min）
13. 建 dummy_req、让它的 page 指向 dummy 页、构造 GraphRunner 捕获 CUDA Graph
```

两个显存测量点是理解整条流水线的关键：

- 第 6 步的 `init_free`（`[1]`，即跨 rank 的 **max** 可用显存）是「装模型前」的基线。
- 第 8 步 `_determine_num_pages` 内部会**再量一次**「装模型后」的显存，两者之差就是模型权重占了多少。
- 第 12 步的 `post_free`（`[0]`，即跨 rank 的 **min** 剩余显存）只是日志用途，告诉你初始化完还剩多少。

> 为什么 `[1]` 是 max、`[0]` 是 min？因为 `_sync_get_memory` 返回 `(min_free, max_free)`，见 4.4 节。

#### 4.1.3 源码精读

**前置设定与全局上下文**（断言 CUDA 未初始化、登记 TP 身份、自动选后端、建 engine stream、注册全局 Context）：

[python/minisgl/engine/engine.py:30-42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L30-L42)

这段代码做了三件值得注意的事：(1) `assert not torch.cuda.is_initialized()` 保证本进程此前没有任何代码碰过 CUDA，使得随后的 `set_device` / `set_stream` 在干净上下文里生效，也让后续显存统计反映真实基线；(2) `torch.manual_seed(42)` 固定随机种子，便于复现；(3) `self.stream = torch.cuda.Stream()` 创建的这条流，正是 u4-l1 讲过的 **engine stream**——overlap scheduling 里专门跑模型前向的那条流。

**通信初始化与 baseline 显存**：

[python/minisgl/engine/engine.py:44-46](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L44-L46)

注意顺序：必须先 `_init_communication()` 建好进程组，才能调 `_sync_get_memory()` 做「跨 rank all_reduce 显存」。`init_free_memory` 取返回元组的 `[1]`（max_free）。

**KV cache 与 page_table 初始化**：

[python/minisgl/engine/engine.py:54-73](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L54-L73)

几个要点：

- `num_tokens = num_pages * page_size`，是 KV cache 能容纳的总 token 数。
- 池子按 `num_pages + 1` 创建，多出的那一页是 **dummy page**，专门给 CUDA Graph 的 dummy 请求用（见 4.1.3 末尾）。
- `max_seq_len = min(config.max_seq_len, num_tokens)`：单序列最长不超过模型允许的，也不超过 KV cache 总容量。
- `aligned_max_seq_len = _align_up_32(max_seq_len)` 把列数向上对齐到 32 的倍数（`(num + 31) // 32 * 32`）。源码注释明确写了动机——`# NOTE: 1. aligned to 128 bytes; 2. store raw locations instead of pages`：page_table 每行存的是「KV buffer 里的原始位置下标」而非「页号」，对齐到 128 字节是为了让 flashinfer/TRTLLM 等后端读取 page_table 时访存友好。

> `_align_up_32` 实现见 [python/minisgl/engine/engine.py:214-215](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L214-L215)。

**后端、采样器与初始化后的剩余显存**：

[python/minisgl/engine/engine.py:75-86](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L75-L86)

attention/moe 后端与采样器都挂在 `self.ctx`（全局 Context）上，使模型层、注意力后端、采样器能通过模块级单例互相找到（见 u2-l1）。最后一行 `_sync_get_memory()[0]` 只是取 min 剩余显存用于日志。

**CUDA Graph 捕获前的 dummy_req 装配**：

[python/minisgl/engine/engine.py:88-110](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L88-L110)

`dummy_req.table_idx = max_running_req`（即 page_table 的最后一行，专门留给 dummy），随后 `self.page_table[self.dummy_req.table_idx].fill_(num_tokens)` 把 dummy 这一行的所有位置都填成 `num_tokens`。因为池子有 `num_pages + 1` 页，dummy page 的起始 token 下标正是 `num_pages * page_size = num_tokens`——所以这一行把 dummy 请求的所有 KV 读写都导向那个多余的 dummy 页，避免污染真实 KV 数据。这套机制是 u5-l3（CUDA Graph）的前置，本讲只需知道「它在 `__init__` 末尾完成」。

**自动后端选择 `_adjust_config`**（用 `object.__setattr__` 绕过 frozen 锁）：

[python/minisgl/engine/engine.py:218-233](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L218-L233)

它把 `"auto"` 的 attention 后端解析成具体值：SM100（H200）选 `trtllm`，SM90（Hopper）选 `fa,fi`（prefill 用 FlashAttention、decode 用 FlashInfer），其它选 `fi`；并强制 TRTLLM 的 page_size 为 64。frozen dataclass 不能直接赋值，所以用 `object.__setattr__` 绕锁（原理见 u2-l2）。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，把 `Engine.__init__` 的「显存测量点」与具体代码行对应起来。

**操作步骤**：

1. 打开 [engine.py:30-110](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L30-L110)。
2. 找到三处调用 `_sync_get_memory()` 的地方（第 45、149、85 行），分别标注它们处于「装模型前」「`_determine_num_pages` 内部」「全部装完后」。
3. 用 `grep -n "logger.info_rank0\|logger.info" python/minisgl/engine/engine.py` 找到所有日志行，确认日志里打印的 `Free memory before loading model` / `Allocating ... tokens for KV cache` / `Free memory after initialization` 分别对应哪次测量。

**需要观察的现象**：

- 三次显存数值应当单调递减：装模型前 > 装模型后（KV cache 分配前）> 全部装完后。
- KV cache 分配日志会打印 `K + V = X GiB`，这是 `num_pages * cache_per_page` 的字节数。

**预期结果**：能在不运行代码的情况下，画出一张「时序图」，横轴是 `__init__` 的步骤，纵轴标注可用显存的下降台阶。待本地验证（在有 GPU 的机器上启动服务，对照日志里的三个 GiB 数字）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_init_communication()` 必须在第一次 `_sync_get_memory()` 之前调用？

> **答案**：`_sync_get_memory` 内部用 `torch.distributed.all_reduce(..., group=self.tp_cpu_group)` 做跨 rank 聚合，而 `self.tp_cpu_group` 正是 `_init_communication` 的返回值。进程组没建好，all_reduce 就没有可用的通信通道。

**练习 2**：`page_table` 的形状为什么是 `(max_running_req + 1, aligned_max_seq_len)` 而不是 `(max_running_req, ...)`？

> **答案**：多出来的那一行（`max_running_req`）专门给 dummy_req 用，让 CUDA Graph 捕获时的 dummy 请求有独立的寻址行，不挤占真实请求的行。这与池子 `num_pages + 1` 多一页 dummy page 是配套设计。

---

### 4.2 meta 建图

#### 4.2.1 概念说明

「建图」指把一个模型从配置实例化成内存中的对象树。最朴素的做法是 `model = LlamaForCausalLM(config)`——`__init__` 里用 `torch.zeros(...)` / `nn.Parameter(...)` 立刻在 GPU 上分配全部参数张量；随后 `model.load_state_dict(weights)` 再把权重**拷贝**进去。问题在于：拷贝瞬间，新旧两份参数同时存在，峰值显存约为参数量的 2 倍。

Mini-SGLang 用 meta device 规避这个峰值：在 `torch.device("meta")` 上下文里建模型，所有参数张量都落在 meta 上（零显存），得到正确的形状树后，再用 `load_state_dict` 把**已经在目标 GPU 上的真实权重直接赋值**过去——因为 BaseOP 的参数是普通属性（不是 `nn.Parameter`），`load_state_dict` 是 `setattr` 覆盖而非拷贝，所以全过程峰值显存约为参数量的 1 倍。

#### 4.2.2 核心流程

```
with torch.device("meta"), torch_dtype(config.dtype):
    model = create_model(config.model_config)   # 在 meta 上建骨架，零显存
state_dict = _load_weight_state_dict(config)    # 得到 {name: GPU 上的真实张量}
model.load_state_dict(state_dict)               # 递归 setattr，meta 占位符 → 真实权重
```

`_load_weight_state_dict` 有两条分支：

- **`use_dummy_weight=True`**（调试用）：调 `model.state_dict()` 拿到所有名字与形状，用 `torch.randn_like` 逐个造随机张量放到 GPU。
- **正常分支**：调 `load_weight(model_path, device)`——一个流式加载器，把 safetensors 里的权重**按 TP 分片、合并 qkv、堆叠 expert 后直接 yield 到目标 GPU**（详见 u8-l2）。它的峰值 CPU 显存只有「一个完整张量 + 小合并缓冲」。

#### 4.2.3 源码精读

**meta 建图 + 装载真实权权的两行核心**：

[python/minisgl/engine/engine.py:48-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L48-L52)

`torch_dtype(config.dtype)` 是一个上下文管理器，临时把 PyTorch 默认 dtype 设成 `config.dtype`（如 bf16），使得 meta 建图时生成的占位张量也带正确 dtype（这样后续 `load_state_dict` 的 dtype 断言能通过）。`set_rope_device(self.device)` 把 RoPE 的频率表预计算挂到正确设备上。

**两条加载分支**：

[python/minisgl/engine/engine.py:139-146](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L139-L146)

注意 dummy 分支里 `torch.randn_like(v, device=self.device)`：`v` 是 meta 张量（来自 `state_dict()`），`randn_like` 复用它的形状与 dtype，但显式指定 `device=self.device` 落到真实 GPU——这是从 meta 形状「物化」出真实随机张量的关键。

**`BaseOP.load_state_dict` 的递归赋值**（meta 占位符 → 真实权重）：

[python/minisgl/layers/base.py:32-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L32-L53)

这段代码揭示了 BaseOP 与 `nn.Module` 的本质差异：

- 它遍历的是 `self.__dict__`（普通属性），而非 `_parameters` / `_buffers`。
- 遇到 `torch.Tensor` 类型的属性：从 `state_dict` 里 `pop` 出同名张量，做形状/dtype 断言，然后 **`setattr(self, name, item)`** 直接覆盖——meta 占位符被真实张量替换。
- 遇到嵌套的 `BaseOP`：递归调用，传递累积的 `prefix`（用 `.` 拼接名字，类似 `layers.0.attn.qkv_proj`）。
- 下划线开头的属性（`name.startswith("_")`）被跳过——约定为非权重内部状态。

> 对照 `state_dict()` 的「镜像」实现：[python/minisgl/layers/base.py:19-31](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L19-L31)，它同样遍历 `__dict__`、跳过下划线属性、递归子 BaseOP，把张量收进字典。这两个函数互为逆操作。

#### 4.2.4 代码实践

**实践目标**：验证「meta 建图 + load_state_dict」确实不产生 2× 峰值。

**操作步骤**（源码阅读型，无需 GPU 也能做）：

1. 阅读 [layers/base.py:32-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L32-L53)，确认 `setattr(self, name, item)` 是赋值（原 meta 张量被丢弃），而非 `param.copy_(item)`（拷贝）。
2. 假设一个模型有 N 个参数、每个参数 bf16（2 字节），写出：
   - 朴素建图法的峰值显存（建图时 N×2 字节 + load 时临时拷贝 N×2 字节）；
   - meta 建图法的峰值显存（建图 0 + load 时真实权重 N×2 字节）。

**预期结果**：

- 朴素法峰值 ≈ 2N×2 字节（参数本身 + 拷入的副本）。
- meta 法峰值 ≈ N×2 字节（仅真实权重一份）。
- 结论：meta 法把加载峰值砍半。

如果你有 GPU，可以加一行日志验证：在 `engine.py:52` 的 `load_state_dict` 前后各打印一次 `torch.cuda.memory_allocated()`，观察增量是否约等于一份参数量（而非两份）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `torch_dtype(config.dtype)` 必须和 `torch.device("meta")` 一起出现在同一个 `with` 里？

> **答案**：meta 张量记录 dtype。如果不设默认 dtype，建图时占位张量会是 float32，而真实权重是 bf16，`load_state_dict` 里的 `assert param.dtype == item.dtype` 会失败。两者必须在同一上下文里生效，保证占位符与真实权重的 dtype 一致。

**练习 2**：`load_state_dict` 末尾的 `if not _internal and state_dict: raise RuntimeError(...)` 在防什么？

> **答案**：递归结束后，如果 `state_dict` 还有没被消费的键（说明权重文件里有模型树里不存在的参数名），就报错。`_internal=True` 时跳过此检查（因为中间层只消费属于自己的键，剩下的要留给兄弟节点），只有最外层（`_internal=False`）才做最终的「必须清空」断言。

---

### 4.3 `_determine_num_pages`

#### 4.3.1 概念说明

装完模型后，要把「剩余显存」换算成「能分多少页 KV cache」。这个换算需要两个输入：(1) 一页 KV cache 到底占多少字节；(2) 还剩多少字节可用。前者由模型结构决定（层数、head_dim、KV head 数、page_size、dtype），后者由「装模型前后的显存差」与 `memory_ratio` 共同决定。`_determine_num_pages` 就是把这两个输入接起来的算式。

#### 4.3.2 核心流程

先看一页 KV cache 的字节数。KV buffer 的布局（来自 `MHAKVCache`）是：

```
(2, num_layers, num_pages, page_size, local_kv_heads, head_dim)
 └ K/V  └ 层     └ 页    └ 页内token └ KV头     └ 头维度
```

所以「一页、跨所有层」的字节数是：

\[
\text{cache\_per\_page} = 2 \times \text{head\_dim} \times \text{local\_kv\_heads} \times \text{page\_size} \times \text{itemsize} \times \text{num\_layers}
\]

其中因子 2 是 K 与 V 两份；`itemsize` 是每个元素的字节数（bf16=2，fp16=2，fp32=4）；`local_kv_heads` 是 TP 切分后本 rank 实际持有的 KV head 数。

可用显存与页数：

\[
\text{model\_memory} = \text{old\_free} - \text{new\_free}
\]

\[
\text{available\_memory} = \lfloor \text{memory\_ratio} \times \text{old\_free} \rfloor - \text{model\_memory}
\]

\[
\text{num\_pages} = \left\lfloor \frac{\text{available\_memory}}{\text{cache\_per\_page}} \right\rfloor
\]

直觉解释：

- `old_free`（装模型前）× `memory_ratio`（默认 0.9）= 我们愿意给「模型 + KV cache + 运行时」总共用的预算上限。
- 减去 `model_memory`（模型已占）= 还能给 KV cache 用的钱。
- 注意：这里没有再减运行时开销，而是通过 `memory_ratio < 1` 预留了——剩下的 0.1 留给激活、临时缓冲、CUDA Graph 工作区等。
- 最后整除 `cache_per_page` 得到页数。

#### 4.3.3 源码精读

**`_determine_num_pages` 全貌**：

[python/minisgl/engine/engine.py:148-168](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L148-L168)

要点逐条对照：

- 第 149 行 `new_free_memory = self._sync_get_memory()[1]`：**装模型后**再量一次（跨 rank 对齐的 max），与入参 `old_free_memory`（装模型前的 max）配套。
- 第 150-157 行正是上面的 `cache_per_page` 公式。
- 第 158-159 行：`num_page_override` 不为 None 时直接用它（CLI `--num-pages` 可强制指定），跳过自动估算。
- 第 164 行 `assert num_pages > 1`：至少要能分出多于 1 页，否则连一个请求都服务不了，直接报错并提示 `try reducing --num-pages`（其实更可能是模型太大，需要换更小的模型或更高 TP）。

**GQA 下的 KV head 切分 `div_even`**：

[python/minisgl/utils/misc.py:20-26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py#L20-L26)

`div_even(num_kv_heads, tp_size, allow_replicate=True)`：

- 正常情况 `num_kv_heads % tp_size == 0`，返回 `num_kv_heads // tp_size`。
- GQA 极端情况 `tp_size > num_kv_heads`（比如 8 个 KV head 却用了 tp=16）：`allow_replicate=True` 时，只要 `tp_size % num_kv_heads == 0`，就返回 1，表示每个 rank 复制一份完整的 KV head（因为 KV head 没法再切了）。这正是 `QKVMerged` 对 GQA 做 head 复制的底层依据（u9-l1）。

**KV buffer 布局——公式的事实依据**：

[python/minisgl/kvcache/mha_pool.py:28-37](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L28-L37)

注意 `_kv_buffer` 的形状 `(2, num_layers, num_pages, page_size, local_kv_heads, head_dim)` 与 `cache_per_page` 公式逐项对应——这证明公式不是拍脑袋写的，而是直接反映 buffer 的真实形状。`local_kv_heads` 同样用 `div_even(..., allow_replicate=True)` 算出（mha_pool.py 第 27 行），保证估算与实际分配用同一套切分逻辑。

#### 4.3.4 代码实践

**实践目标**：给定一组模型结构参数，手算 `num_pages`，再对照源码验证公式。

**示例数值**（ illustrative，非任何真实模型的精确值）：

| 参数 | 值 |
|------|----|
| `num_layers` | 28 |
| `head_dim` | 128 |
| `num_kv_heads` | 8（GQA） |
| `tp_size` | 1 |
| `page_size` | 1 |
| `dtype` | bf16（`itemsize=2`） |
| `old_free`（装模型前） | 80 GiB |
| `new_free`（装模型后） | 77.6 GiB |
| `memory_ratio` | 0.9 |

**操作步骤**：

1. 算 `local_kv_heads = div_even(8, 1) = 8`。
2. 算 `cache_per_page = 2 × 128 × 8 × 1 × 2 × 28`：
   - `2 × 128 = 256`
   - `256 × 8 = 2048`
   - `2048 × 1 = 2048`
   - `2048 × 2 = 4096`
   - `4096 × 28 = 114688` 字节/页 ≈ 112 KiB/页。
3. 算 `model_memory = old_free − new_free = 80 − 77.6 = 2.4 GiB`（约一份 bf16 的 1.2B 参数模型）。
4. 把 GiB 换算成字节：`old_free = 80 × 1024³ = 85,899,345,920` 字节。
5. 算 `available_memory = floor(0.9 × 85,899,345,920) − 2.4GiB`：
   - `0.9 × 85,899,345,920 = 77,309,411,328`
   - `2.4 GiB = 2.4 × 1024³ = 2,576,980,378`
   - `available ≈ 77,309,411,328 − 2,576,980,378 = 74,732,430,950` 字节。
6. 算 `num_pages = 74,732,430,950 // 114688 ≈ 651,605` 页。

**需要观察的现象**：

- 把 `page_size` 改成 32，重算 `cache_per_page` 会放大 32 倍，`num_pages` 相应缩小约 32 倍，但 `num_tokens = num_pages × page_size` 基本不变——这说明 page_size 影响的是「页的粒度」而非「总容量」。
- 把 `memory_ratio` 从 0.9 调到 0.8，`num_pages` 约下降 1/9（因为模型占用是固定的，被减数变小）。

**预期结果**：你能用计算器复现上面的 `num_pages`，并对照 [engine.py:148-168](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L148-L168) 确认每一步都对应代码里的某一行。真实启动时的精确数字待本地验证（用日志里打印的 `K + V = X GiB` 反推）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `available_memory` 里减的是 `model_memory`，而不是用 `new_free`（装模型后的剩余）直接除以 `cache_per_page`？

> **答案**：因为我们要用的是 `memory_ratio × old_free` 这个「预算上限」减去模型占用，剩下的才给 KV cache。如果直接用 `new_free`，就没有给「运行时开销」预留空间——`new_free` 是装完模型后的真实剩余，全分给 KV cache 会在后续 CUDA Graph 捕获或前向时 OOM。`memory_ratio < 1` 正是为运行时预留的缓冲。

**练习 2**：`num_page_override` 不为 None 时，代码跳过了显存估算。这种「强制指定页数」有什么风险？

> **答案**：强制指定的页数可能超出实际剩余显存，导致后续 `create_kvcache_pool` 分配 buffer 时 OOM；也可能小到 `assert num_pages > 1` 失败。它适合「我知道这个模型在这张卡上最多能用 N 页」的精细调优场景，但用错会让进程在初始化末尾崩溃。

---

### 4.4 `_sync_get_memory`

#### 4.4.1 概念说明

张量并行要求所有 rank 的 KV cache 页数一致。但各 rank 的可用显存可能不同（比如其它进程占用了某张卡的一部分显存）。如果每张卡「各算各的」页数，就会出现 rank A 分了 10 万页、rank B 分了 8 万页——前向时 attention 的 KV 维度对不上，`all_reduce` 报错。

解决办法是：**取所有 rank 中最小的可用显存作为统一基准**，让所有 rank 都按这个最小值算页数，于是大家分到的页数必然相同。`_sync_get_memory` 就是干这件事的——它用一次 `all_reduce` 同时拿到「跨 rank 的最小显存」和「跨 rank 的最大显存」，最小值用于对齐，最大值与最小值的差距用于检测「显存失衡」（差距过大说明某张卡被严重占用，继续跑大概率会 OOM，不如早 fail）。

#### 4.4.2 核心流程

```
对每张卡：
    free = mem_get_info(device)[0]
打包成 [free, -free]  # 2 元张量
all_reduce(MIN, group=tp_cpu_group)   # 一次通信同时算 min 与 max
min_free = result[0]
max_free = -result[1]
if max_free - min_free > 2 GiB:
    raise RuntimeError("imbalanced")
return (min_free, max_free)
```

关键的「一次 all_reduce 拿 min 和 max」技巧：

\[
\min(\text{free}_0, \text{free}_1, \dots) = \text{result}[0]
\]

对负值取 `MIN` 等于对原值取 `MAX`：

\[
\min(-\text{free}_0, -\text{free}_1, \dots) = -\max(\text{free}_0, \text{free}_1, \dots)
\]

所以 `-result[1]` 就是 max。这样原本需要两次 `all_reduce`（一次 MIN、一次 MAX）的操作合并成一次，省了一半通信。

#### 4.4.3 源码精读

**`_sync_get_memory` 全貌**：

[python/minisgl/engine/engine.py:170-189](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L170-L189)

逐行解读：

- 第 172-174 行：测量前先 `synchronize`（等所有异步操作完成）、`empty_cache`（释放缓存的空闲块）、`reset_peak_memory_stats`（重置峰值统计）。这三步保证测到的是「干净、稳定」的可用显存，而不是被缓存碎片干扰的瞬时值。
- 第 175 行 `get_free_memory(device)` 包装了 `torch.cuda.mem_get_info(device)[0]`（见 [graph.py:74-75](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L74-L75)），返回当前可用字节数。
- 第 176-179 行：把 `[free, -free]` 打包成 CPU 上的 int64 张量，用 `all_reduce(MIN)` 在 `tp_cpu_group`（gloo CPU 进程组，见 u4-l2）上聚合。
- 第 180-181 行：`result[0]` 是 min_free，`-result[1]` 是 max_free。
- 第 182-187 行：**失衡检测**——如果 max 与 min 之差超过 2 GiB，说明某张卡被严重占用（相对差距过大），直接 `raise RuntimeError` 让进程早死，而不是在前向时随机 OOM。错误日志会同时打印 min 与 max 的 GiB 数。
- 第 189 行：返回 `(min_free, max_free)`。下游用 `min_free`（即 `[0]`）做对齐基准——其实 `_determine_num_pages` 用的是 `[1]`（max），但因为各 rank 的 max 也已经经过 all_reduce 取了同一个全局 max，所以各 rank 算出的 `num_pages` 仍然一致。**真正保证一致的是「同一个全局聚合值」而非 min/max 的选择**。

> 一个容易混淆的点：`_determine_num_pages` 里 `old_free` 和 `new_free` 都取 `_sync_get_memory()[1]`（max），而 `__init__` 末尾的 `post_free` 取 `[0]`（min，更保守的剩余量，用于日志）。无论取哪个，只要所有 rank 取的是同一个全局聚合值，页数就对齐。

#### 4.4.4 代码实践

**实践目标**：验证「一次 all_reduce 同时拿 min 和 max」的技巧。

**操作步骤**（源码阅读 + 思维实验）：

1. 阅读 [engine.py:176-181](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L176-L181)。
2. 假设有两张卡，free 分别是 `80GiB` 和 `78GiB`（即 `85,899,345,920` 与 `83,751,884,800` 字节）。
3. 推演：rank0 打包 `[85.9e9, -85.9e9]`，rank1 打包 `[83.7e9, -83.7e9]`。
4. `all_reduce(MIN)` 后：`result[0] = min(85.9e9, 83.7e9) = 83.7e9`；`result[1] = min(-85.9e9, -83.7e9) = -85.9e9`。
5. 于是 `min_free = 83.7e9`，`max_free = -(-85.9e9) = 85.9e9`。
6. 差值 `85.9 − 83.7 = 2.2 GiB > 2 GiB`，触发失衡报错。

**需要观察的现象**：

- 上述两卡场景会**直接抛 `RuntimeError("Memory across TP ranks are imbalanced")`**，进程不会继续启动。
- 如果把 rank1 的 free 改成 `79.5GiB`（差 0.5GiB），则不触发报错，正常启动。

**预期结果**：你能解释为什么这个技巧只需要一次 `all_reduce`，以及 2 GiB 阈值的含义（一个相对宽松的容忍带，避免因正常的显存波动误杀）。精确的卡间差值行为待本地验证（在多卡机器上人为占用其中一张卡的显存，观察启动是否报 imbalanced）。

#### 4.4.5 小练习与答案

**练习 1**：如果不用「打包 `[free, -free]`」的技巧，要拿 min 和 max 需要几次 `all_reduce`？

> **答案**：两次——一次 `all_reduce(MIN)` 拿 min，一次 `all_reduce(MAX)` 拿 max。打包技巧利用了「对负值取 min 等于对原值取 max」，把两次合并成一次，通信开销减半。这在初始化阶段虽然省的时间不多，但体现了工程上对通信成本的敏感。

**练习 2**：失衡检测的阈值是 2 GiB。为什么不是 0（即要求所有 rank 显存完全相同）？

> **答案**：实际环境中各卡显存有微小波动是正常的（CUDA context、显存碎片、其它进程的瞬时占用等），要求完全相同会频繁误杀。2 GiB 是一个「容忍正常波动、但能抓住严重失衡」的工程折中。超过 2 GiB 通常意味着某张卡被实质性占用了，继续跑会在 KV cache 分配或前向时 OOM，早 fail 比晚 fail 更好排查。

---

## 5. 综合实践

把四个最小模块串起来，完成一次「初始化全流程显存推演」。

**任务**：假设你要在一台 4 卡（tp=4）机器上部署一个满足下表的模型，推演 `Engine.__init__` 在 **rank 0** 上的完整显存轨迹，并判断能否成功启动。

**示例参数**（illustrative）：

| 参数 | 值 |
|------|----|
| `num_layers` | 32 |
| `head_dim` | 128 |
| `num_kv_heads` | 8（GQA，`tp_size=4` 可整除） |
| `page_size` | 32 |
| `dtype` | bf16 |
| `memory_ratio` | 0.9 |
| 单卡总显存 | 80 GiB |
| 装模型前可用（rank0/rank1/rank2/rank3） | 80 / 80 / 79.5 / 80 GiB |
| 装模型后可用（rank0） | 76 GiB |

**操作步骤**：

1. **失衡检测**（4.4）：四卡的 free 为 80/80/79.5/80，min=79.5、max=80，差 0.5GiB < 2GiB，通过。
2. **算 cache_per_page**（4.3）：`local_kv_heads = div_even(8, 4) = 2`；`cache_per_page = 2 × 128 × 2 × 32 × 2 × 32`。
   - `2 × 128 = 256`；`256 × 2 = 512`；`512 × 32 = 16384`；`16384 × 2 = 32768`；`32768 × 32 = 1,048,576` 字节/页 = 1 MiB/页。
3. **算 num_pages**（4.3）：`old_free = 80GiB = 85,899,345,920`；`model_memory = 80 − 76 = 4GiB`；`available = floor(0.9 × 85.9e9) − 4GiB`。
   - `0.9 × 85.9e9 = 77.3e9`；`4GiB = 4,294,967,296`；`available ≈ 73,014,444,032`。
   - `num_pages = 73,014,444,032 // 1,048,576 ≈ 69,632` 页。
4. **验证对齐**（4.4）：由于所有 rank 的 `old_free` 都取全局 max（80GiB），且 `cache_per_page` 由相同的模型结构算出，四卡分到的 `num_pages` 都是 69,632，TP 维度对齐。
5. **判断**：`num_pages > 1`，断言通过；失衡检测通过；启动成功。

**需要观察的现象**：

- 改 `memory_ratio=0.95`：`available` 增大约 1/18，`num_pages` 相应增加，但留给 CUDA Graph 的余量变薄，可能在 graph 捕获阶段（4.1 末尾）OOM。
- 把 rank2 的 free 改成 77GiB（与 80 差 3GiB）：失衡检测在第 6 步直接 fail，进程不会进入后续流程。

**预期结果**：你能完整复现这条「显存轨迹」：从失衡检测 → cache_per_page → num_pages → 对齐验证，并把每一步对应到本讲的源码行。这正是 `Engine.__init__` 在真实启动时无声执行的逻辑。

## 6. 本讲小结

- `Engine.__init__` 是一条**有序的显存装配流水线**：先量 baseline 显存、再装模型、再用差值算 KV cache 页数、最后捕获 CUDA Graph；两次 `_sync_get_memory` 调用夹着模型加载，是整套估算的支点。
- **meta device 建图**在 `torch.device("meta")` 下零显存搭骨架，再经 `BaseOP.load_state_dict` 用 `setattr` 把真实权重逐个覆盖上去，把加载峰值从约 2× 参数量降到约 1×。
- **`_determine_num_pages`** 用 `cache_per_page = 2 × head_dim × local_kv_heads × page_size × itemsize × num_layers` 算每页字节，用 `available = floor(memory_ratio × old_free) − model_memory` 算可用预算，整除得页数；`memory_ratio < 1` 为运行时预留余量。
- **`div_even(..., allow_replicate=True)`** 统一处理 KV head 的 TP 切分与 GQA 复制，保证估算公式与实际 buffer 分配用同一套逻辑。
- **`_sync_get_memory`** 用一次 `all_reduce(MIN)` 同时拿到跨 rank 的 min/max 显存（打包 `[free, −free]` 的技巧），min/max 差超 2 GiB 即判定失衡并早 fail，避免前向时随机 OOM。
- 所有 rank 取**同一个全局聚合值**算页数，是 TP 下 KV cache 形状对齐的根本保证。

## 7. 下一步学习建议

- **u5-l2 Engine forward 与采样**：本讲只讲了 `__init__`，下一讲进入 `forward_batch`——看 graph replay 与普通 forward 如何分支、Sampler 如何批量打包采样参数、token 如何异步拷回 CPU。
- **u5-l3 CUDA Graph 捕获与回放**：本讲末尾出现的 `dummy_req` 与 `GraphRunner` 在那里展开，理解捕获尺寸列表、pad_batch 与回放时只拷输入缓冲的机制。
- **u6-l1 KV Cache 池、存储与 Prefix Cache 抽象**：本讲的 `cache_per_page` 公式来自 `MHAKVCache` 的 buffer 布局，下一单元会讲 `store_kv` 如何把新 K/V 写进这个池，以及 `BasePrefixCache` 的接口契约。
- **u9-l1 张量并行 Linear 与分布式通信**：本讲的 `div_even`、`local_kv_heads`、`tp_cpu_group` 在那里深入，理解 column/row parallel 的切分与 all_reduce 位置。
