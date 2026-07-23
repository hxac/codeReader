# CUDA Graph 捕获与回放

## 1. 本讲目标

本讲聚焦 `python/minisgl/engine/graph.py` 中的 `GraphRunner`,讲清楚 Mini-SGLang 如何用 CUDA Graph 把 decode 阶段的每轮前向「录下来、再反复重放」,从而把 CPU 端的 kernel 启动开销几乎归零。

学完后你应当能够:

- 说清楚 **CUDA Graph 为什么只对 decode、且 `size <= max_graph_bs` 的批次生效**,而 prefill 不能用。
- 解释 `GraphRunner` 在初始化时如何对一组 batch size **逐个捕获** graph,并用 **graph pool 复用** 节省显存。
- 解释 `pad_batch` 如何用 `dummy_req` 把一个真实 batch **补齐到捕获尺寸**,以及 `dummy_req.table_idx` 指向 dummy page 的作用。
- 描述 `replay` 时 **只拷贝输入缓冲内容、不重新建图** 的机制,以及注意力后端的 capture/replay 协议如何配合。

本讲是 u5-l2(Engine forward 与采样)的直接续篇——`Engine.forward_batch` 里那个「`can_use_cuda_graph` 为真就走 `graph_runner.replay`,否则走普通 `model.forward()`」的二选一分支,正是本讲要展开的黑盒。

## 2. 前置知识

在进入源码前,先用三段话建立直觉。

**(a) 什么是 CUDA Graph。** 一次普通的 GPU 前向,是 CPU 逐个「提交 kernel」给 GPU:每个 kernel 都要 CPU 发起一次 launch,伴随驱动开销。当 kernel 本身很轻(比如 decode 时每条请求只算 1 个新 token),CPU 提交 kernel 的时间甚至会 **超过** GPU 实际计算的时间——CPU 成了瓶颈,GPU 在空转等待。CUDA Graph 解决这个问题:先把这些 kernel 序列「录制成」一张图(`torch.cuda.CUDAGraph`),之后每次推理只需 `graph.replay()` 一条命令,整张图一次性重放,CPU 只提交一次。代价是:图里记录的是 **固定的 kernel 序列和固定的张量地址**,所以只有当「计算形状不变」时才能复用。

**(b) 为什么 decode 适合、prefill 不适合。** 关键看每条请求贡献多少个 query token:

- decode 阶段:每条请求每轮只算 **1 个新 token**(`extend_len == 1`)。一个 size 为 `bs` 的 decode 批,永远是「`bs` 个 query、每个长度 1」的均匀形状——不管谁来了,计算图都长得一样。这种 **固定形状** 正是 CUDA Graph 需要的。
- prefill 阶段:每条请求的 prompt 长度(`extend_len`)千差万别,累积前缀长度(`cu_seqlens`)每批都变,kernel 的网格大小、张量形状都在变。形状不固定,就无法录成一张可重用的图。

所以 Mini-SGLang 只为 decode 捕获 graph,这与上一讲看到的 `forward_batch` 分支判断完全吻合。

**(c) 固定指针、可变内容。** 这是理解整篇源码的钥匙。图里记录的是「从地址 A 读输入、算完写到地址 B」。重放时我们 **不改变 A、B 这些地址**,只把 A 处的 **内容** 换成新数据——图照旧从 A 读,自然算出新结果。`GraphCaptureBuffer` 就是那块「地址固定、内容可变」的缓冲区。

> 术语速查:`max_graph_bs`(捕获的最大 batch size)、`graph_bs_list`(被捕获的一组 size,如 `[1,2,4,8,16,...]`)、`padded_size`(真实 batch 被 pad 到的捕获尺寸)、`dummy_req`(用于填充的占位请求)、dummy page(给 dummy_req 专用的 KV cache 页)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/engine/graph.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py) | 本讲主角。`GraphCaptureBuffer` 定义固定缓冲,`GraphRunner` 负责捕获与回放。 |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | `Engine.__init__` 创建 `dummy_req`、填好 dummy page、构造 `GraphRunner`;`forward_batch` 里二选一调用 replay。 |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | `_prepare_batch` 在前向前调用 `pad_batch`,把真实 batch 补齐到捕获尺寸。 |
| [python/minisgl/attention/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py) | 注意力后端的 capture/replay 抽象接口(`init_capture_graph`/`prepare_for_capture`/`prepare_for_replay`)。 |
| [python/minisgl/attention/fi.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py) | FlashInfer 后端对上述接口的实现,展示注意力元数据如何在 capture/replay 间协同。 |

## 4. 核心概念与源码讲解

### 4.1 GraphRunner 与 GraphCaptureBuffer:固定指针、可变内容

#### 4.1.1 概念说明

`GraphRunner` 是 decode 加速的总指挥,持有三样东西:一张「按 batch size 索引的 graph 字典」`graph_map`、一块「固定地址的输入/输出缓冲」`buffer`、以及一个用于填充的 `dummy_req`。

`GraphCaptureBuffer` 是那块固定缓冲。它的设计直接体现了「固定指针、可变内容」:

- 它被一次性分配到 **最大捕获尺寸** `max_graph_bs`,之后地址不再变。
- 三个输入张量 `input_ids`/`out_loc`/`positions`(都是 `int32`)和一个输出张量 `logits`(`float32`,`[max_graph_bs, vocab_size]`)。
- 录制时,`set_batch` 让 batch 的输入字段 **指向 buffer 的切片**(view),于是图录下的是 buffer 的地址。
- 重放时,`copy_from` 把新 batch 的内容 **写进同一块 buffer**,图照旧从老地址读,算出新结果。

#### 4.1.2 核心流程

```text
GraphCaptureBuffer(max_graph_bs)
        │  地址固定 ──────────────────────────┐
        │                                     │
   set_batch(batch)  ──录制阶段──►  图记录 buffer 地址
        │                                     │
   copy_from(batch)  ──重放阶段──►  改写 buffer 内容,图重放
```

`set_batch` 与 `copy_from` 是一对镜像操作,区别只在方向:

- `set_batch`:buffer → batch(让 batch 字段成为 buffer 的 view,用于 **捕获**)。
- `copy_from`:batch → buffer(把新数据写进 buffer,用于 **回放**)。

两者都用 `slice(batch.padded_size)` 限定作用范围——只用 buffer 的前 `padded_size` 行,后面留空。

#### 4.1.3 源码精读

`GraphCaptureBuffer` 是一个普通 dataclass,`init` 工厂方法按最大尺寸一次性分配四块张量:

[python/minisgl/engine/graph.py:20-34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L20-L34) —— 定义 `input_ids`/`out_loc`/`positions`/`logits` 四个字段,`init` 分配到 `max_graph_bs` 大小。注意 `logits` 用 `torch.empty`(不必清零,反正会被覆盖),其余用 `torch.zeros`(捕获时 positions 等为 0 是合法占位)。

`set_batch` 把 batch 的输入字段 **别名** 到 buffer 的切片:

[python/minisgl/engine/graph.py:36-40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L36-L40) —— `batch.input_ids = self.input_ids[_slice]` 等。赋值的是切片(view),所以之后往 `batch.input_ids` 写数据,实际写进的是 buffer。

`copy_from` 反向把 batch 的真实数据 **拷贝** 进 buffer:

[python/minisgl/engine/graph.py:42-46](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L42-L46) —— `self.input_ids[_slice] = batch.input_ids` 等。重放前调用,把这一轮真实 batch 的输入写进固定 buffer。

> 一个细节:`set_batch` 处理的是 **输入三件套**,`logits`(输出)不在这里设。`logits` 由捕获时的 `model.forward()` 直接写入(见 4.2.3)。

#### 4.1.4 代码实践

**实践目标**:理解「view 别名」如何让固定 buffer 服务于可变 batch。

**操作步骤**(源码阅读型,无需 GPU):

1. 阅读 [graph.py:36-46](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L36-L46)。
2. 在本地用纯 PyTorch 复现这个别名机制(示例代码,非项目代码):

   ```python
   import torch
   buf = torch.zeros(8, dtype=torch.int32)      # 固定 buffer(max_graph_bs=8)
   batch_input = buf[:3]                         # set_batch: view 别名
   batch_input[1] = 99                           # 通过 batch 视图写
   print(buf)                                    # buffer 第 1 位变成 99
   buf[2] = 77                                   # copy_from 反向写
   print(batch_input)                            # batch 视图第 2 位变成 77
   ```

**需要观察的现象**:通过 `batch_input` 写入会改变 `buf`,反向写入 `buf` 也会反映到 `batch_input`——它们共享存储。

**预期结果**:两次 print 分别显示 `99` 和 `77` 出现在对应位置,证明 view 共享底层内存。这就是图能录地址、重放换内容的物理基础。

#### 4.1.5 小练习与答案

**练习 1**:`set_batch` 用的是 `self.input_ids[_slice]`(切片),为什么不用 `self.input_ids.clone()`?

**答案**:clone 会复制一份新内存,batch 字段就不再指向 buffer。那样图录下的就是临时副本的地址,重放时往 buffer 写新数据,图却去读早已失效的副本地址,结果错误。必须用 view 让 batch 与 buffer 共享存储。

**练习 2**:`copy_from` 用 `slice(batch.padded_size)`,如果误写成 `slice(batch.size)` 会出什么问题?

**答案**:`padded_size >= size`,补齐的 dummy 槽位(从 `size` 到 `padded_size`)的旧数据不会被覆盖,重放时图会读到上一轮残留的脏数据。虽然 dummy 槽的 logits 最后会被 `[:batch.size]` 丢弃,但 dummy 槽的 `positions`/`out_loc` 会参与真实计算路径(如 RoPE、page_table 寻址),脏数据可能污染结果或越界访问。所以必须按 `padded_size` 整段刷新。

---

### 4.2 _capture_graphs:逐 bs 捕获与 graph pool 复用

#### 4.2.1 概念说明

Graph 不能对一个「任意 size」的 batch 录制——因为图内部记录了 kernel 网格大小,与 `bs` 强绑定。所以 Mini-SGLang 的策略是:**预先对一组离散的 size 各录一张图**,运行时把真实 batch pad 到「不小于它的最小捕获尺寸」即可复用。

要捕获哪些 size 由 `_determine_cuda_graph_bs` 决定:

- 用户显式传了 `cuda_graph_bs` 就直接用。
- 否则按显存自动定 `cuda_graph_max_bs`:空闲显存 > 80 GiB(典型如 H200)取 256,否则取 160;若 < 1 则禁用 graph(返回空列表)。
- 默认捕获集合为 \(\{1,2,4\}\cup\{8,16,24,\dots,\text{cuda\_graph\_max\_bs}\}\),即小 size 密集、大 size 按 8 的步长稀疏。

#### 4.2.2 核心流程

`_capture_graphs` 的主循环对每个 `bs` 做:

```text
for bs in sorted(graph_bs_list, reverse=True):   # 从大到小捕获
    graph = torch.cuda.CUDAGraph()
    batch = Batch([dummy_req] * bs, phase="decode")  # 全用 dummy_req
    batch.padded_reqs = batch.reqs
    attn_backend.prepare_for_capture(batch)       # 注意力后端建专用 wrapper
    buffer.set_batch(batch)                        # batch 输入字段别名到 buffer
    # 先 warmup 一次(触发惰性分配),再正式录制
    with ctx.forward_batch(batch):
        buffer.logits[:bs] = model.forward()       # warmup(eager)
        with torch.cuda.graph(graph, pool=pool):
            buffer.logits[:bs] = model.forward()   # 真正录制
    if pool is None:
        pool = graph.pool()                        # 第一张(最大)图建池,后续共享
    graph_map[bs] = graph
```

两个关键设计:

1. **从大到小捕获 + graph pool 复用**:第一个(最大的)图捕获后,用 `graph.pool()` 取得它的显存池;之后所有更小的图都 `pool=pool` 共享这块池,避免每个 size 各占一块显存。注释明说这是为了「reuse cuda graph handle to reduce memory」。
2. **warmup 再录制**:`model.forward()` 跑两次。第一次在 graph 上下文之外(eager),目的是触发 PyTorch 内部惰性分配的工作区缓冲,让这些分配发生在录制之外;第二次才在 `torch.cuda.graph(...)` 上下文里真正录制。这是 PyTorch 官方推荐的捕获范式,漏掉 warmup 会导致内部缓冲被错误地算进图的私有池。

#### 4.2.3 源码精读

捕获尺寸的确定逻辑:

[python/minisgl/engine/graph.py:49-67](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L49-L67) —— `_determine_cuda_graph_bs`:显式列表优先;否则按 80 GiB 阈值选 `max_bs`,返回 `[1,2,4] + range(8, max_bs+1, 8)`。`cuda_graph_max_bs < 1` 时返回空列表,等价于禁用。

`GraphRunner.__init__` 先算出 `graph_bs_list` 和 `max_graph_bs`,再调 `_capture_graphs`:

[python/minisgl/engine/graph.py:79-103](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L79-L103) —— `max_graph_bs = max(cuda_graph_bs) if cuda_graph_bs else 0`;`max_graph_bs == 0` 即禁用。注意 `dummy_req` 由外部 `Engine` 传入(见 4.3)。

捕获主循环与 graph pool 复用:

[python/minisgl/engine/graph.py:105-147](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L105-L147) —— 重点看 L128-144:`pool` 初值为 `None`,首个最大图捕获后 `pool = graph.pool()`,后续图复用;`buffer = GraphCaptureBuffer.init(self.max_graph_bs, ...)` 只分配一次(按最大尺寸);L138-141 是 warmup + 录制的两次 `model.forward()`。

#### 4.2.4 代码实践

**实践目标**:验证捕获尺寸的自动选取规则。

**操作步骤**:

1. 阅读 [graph.py:49-67](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L49-L67) 与 [config.py 的 cuda_graph_max_bs 字段](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/config.py#L23-L24)(默认 `None` 即自动)。
2. 阅读 [args.py 的 --cuda-graph-max-bs CLI](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L148-L154)(别名 `--graph`)。

**需要观察的现象**:在两种典型显存下,`graph_bs_list` 会是什么。

**预期结果**(可手算,「待本地验证」实际运行时的显存阈值):

- 空闲 > 80 GiB(H200):`max_bs=256` → `[1,2,4,8,16,...,256]`,共 3 + 32 = 35 个尺寸。
- 空闲 ≤ 80 GiB(如 H100):`max_bs=160` → `[1,2,4,8,16,...,160]`,共 3 + 20 = 23 个尺寸。
- 传 `--graph 1`:`max_bs=1` → `[1,2,4] + range(8,2,8)` = `[1,2,4]`(`range(8,2,8)` 为空)。注意 shell 模式正是把 `cuda_graph_max_bs` 强制设为 1。

#### 4.2.5 小练习与答案

**练习 1**:为什么捕获循环要 `sorted(..., reverse=True)` 从大到小,而不是从小到大?

**答案**:graph pool 复用要求「先捕获的图建池,后续更小的图共享」。最大的图需要最大的显存池;若从小到大捕获,小图先建了一个小池,大图再来时就装不下,只能各占各的池,失去复用意义。从大到小保证池一开始就足够大。

**练习 2**:`max_graph_bs == 0` 时 `_capture_graphs` 直接 return,此时系统还能正常推理吗?

**答案**:能。`graph_map` 为空、`can_use_cuda_graph` 恒为假(`batch.size <= 0` 不成立),`forward_batch` 永远走 `model.forward()` 的普通前向分支。只是失去了 decode 的 graph 加速,CPU 启动开销更高、吞吐更低。这是「禁用 graph」的优雅退化路径。

---

### 4.3 pad_batch 与 dummy_req:把真实 batch 补齐到捕获尺寸

#### 4.3.1 概念说明

真实 decode 批的 size 可能是任意值,比如 5。但 graph 只在 `[1,2,4,8,16,...]` 这些离散尺寸上录制过。`pad_batch` 的职责是:把 size=5 的真实 batch **补齐** 到不小于它的最小捕获尺寸(这里是 8),这样就能复用 size=8 的那张图。

补齐靠 `dummy_req`——一个永远合法的占位请求:

- 它由 `Engine.__init__` 创建,`table_idx = config.max_running_req`(指向 `page_table` 最后一行,即「+1 行专供 dummy」,见 u4-l4)。
- 那一行被 `fill_(num_tokens)`,即全部填成 dummy page 的页号(`num_tokens = num_pages * page_size`,正是 KV pool 里多分配的那一个 `+1` 页的索引,见 u5-l1)。
- 于是 dummy_req 的所有 KV 读写都落在那个 **专用 dummy page** 上,绝不与真实请求的页冲突。

`pad_batch` 用 `dummy_req` 把 `reqs` 拼到 `padded_size` 长,存入 `batch.padded_reqs`。多出来的 dummy 槽会参与计算、产出 logits,但 `replay` 最后只取 `logits[:batch.size]`,dummy 槽的结果被丢弃。

#### 4.3.2 核心流程

```text
真实 batch.size = 5,  graph_bs_list = [1,2,4,8,16,...]
        │
        │  pad_batch: padded_size = 第一个 >= 5 的捕获尺寸 = 8
        ▼
batch.padded_reqs = reqs(5 个真实) + [dummy_req] * 3   ← padded_size=8
        │
        │  copy_from 只写前 padded_size 行;dummy 槽读写 dummy page
        ▼
graph_map[8].replay()  →  logits[:8],取前 5 个返回
```

`padded_size` 的选取规则(见源码):若 `can_use_cuda_graph(batch)` 为真,取 `graph_bs_list` 里第一个 `>= batch.size` 的值;否则直接取 `batch.size`(不补齐,走普通前向)。

#### 4.3.3 源码精读

`dummy_req` 的创建与 dummy page 的指向,在 `Engine.__init__` 尾部:

[python/minisgl/engine/engine.py:89-98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L89-L98) —— `dummy_req.table_idx = config.max_running_req`(最后一行);L98 `self.page_table[self.dummy_req.table_idx].fill_(num_tokens)` 把整行填成 dummy page 页号。`cached_len=0`、`output_len=1`、`uid=-1`、`sampling_params=None`/`cache_handle=None`(`# type: ignore`)——它只为占位,不参与采样与缓存管理。

`pad_batch` 的实现:

[python/minisgl/engine/graph.py:160-166](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L160-L166) —— `padded_size = next(bs for bs in self.graph_bs_list if bs >= batch.size)`(可走 graph 时),否则 `batch.size`;然后 `batch.padded_reqs = batch.reqs + [self.dummy_req] * (padded_size - batch.size)`。

调度器在 `_prepare_batch` 里最先调用 `pad_batch`,之后才分配页、构造 positions/attn metadata:

[python/minisgl/scheduler/scheduler.py:204-211](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L204-L211) —— 第一行 `self.engine.graph_runner.pad_batch(batch)`,确保后续 `prepare_metadata`、`complete_one` 等都基于 `padded_reqs` 工作。

#### 4.3.4 代码实践

**实践目标**:看清 dummy_req 的 table_idx 为何指向 dummy page、以及它如何避免与真实请求冲突。

**操作步骤**(源码阅读型):

1. 阅读 [engine.py:54-63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L54-L63):KV pool 创建时 `num_pages + 1`,注释「+1 for dummy page」。
2. 阅读 [engine.py:89-98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L89-L98):dummy page 页号 = `num_tokens = num_pages * page_size`,正好是那个多出来的第 `num_pages` 个页(0-indexed 的最后一个合法页)。
3. 对照 [scheduler/table.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py) 里 `max_running_req + 1` 行的 `+1` 含义(见 u4-l4):真实请求只用前 `max_running_req` 行,dummy 独占最后一行。

**需要观察的现象**:dummy_req 的 `table_idx` 与真实请求的 `table_idx` 取值范围是否重叠。

**预期结果**:真实请求 `table_idx ∈ [0, max_running_req-1]`(由 `TableManager.allocate` 发号),dummy_req `table_idx = max_running_req`,二者永不重叠;且 dummy 行整行指向同一个 dummy page,所以无论 pad 多少个 dummy_req,它们都安全地读写那一块专用 scratch 页,不污染真实 KV。

#### 4.3.5 小练习与答案

**练习 1**:`dummy_req.input_ids` 是 `torch.tensor([0])`(长度 1),而捕获时 `complete_one` 等会读取它的长度信息。为什么长度为 1 是安全的?

**答案**:捕获阶段所有请求都是同一个 `dummy_req` 对象副本(`[dummy_req]*bs`),且整个捕获用固定形状(decode,每条 extend_len=1)。dummy_req 的 `cached_len=0`、`device_len=1`(`len(input_ids)`)、`extend_len=1`,恰好符合 decode 的「每条 1 个新 token」形状,能正常走完注意力 metadata 构造与前向。长度为 1 是满足 decode 形状约束的最小合法值。

**练习 2**:如果不做 padding,直接对 size=5 的 batch 调 `graph_map[8].replay()` 会怎样?

**答案**:图是在 size=8 时录制的,内部 kernel 网格、`cu_seqlens`、`logits[:8]` 写入都按 8 来。直接喂 size=5 的数据,buffer 的第 5~7 位是脏数据,kernel 会读到未初始化的 positions/out_loc/indices,可能越界访问 page_table 或算出垃圾 logits;且 `replay` 里 `g = self.graph_map[batch.padded_size]` 要求 `padded_size` 必须是已捕获尺寸。所以 padding 是复用离散捕获图的必要前提。

---

### 4.4 replay 与 can_use_cuda_graph:判定与回放

#### 4.4.1 概念说明

回放分三步:判定能否用 graph → 把输入拷进固定 buffer → 查表 replay。判定条件极其简单——「是 decode 且 size 不超过 `max_graph_bs`」。

但光换输入 buffer 还不够:注意力计算依赖的 `page_table` 索引(每条真实请求映射到哪些 KV 页)**每批都变**。这部分不能塞进固定 buffer,而是由注意力后端用一套独立的 capture/replay 协议处理:`init_capture_graph`(建专用捕获数据)→ `prepare_for_capture`(每个 bs 建专用 wrapper)→ `prepare_for_replay`(回放前重绑 wrapper、按新 page_table 重新 plan)。

这套协议在抽象层 `BaseAttnBackend` 声明,`HybridBackend` 会把它转发给 decode 后端(因为只有 decode 才捕获)。

#### 4.4.2 核心流程

`Engine.forward_batch` 的二选一(承接 u5-l2):

```text
with ctx.forward_batch(batch):
    if graph_runner.can_use_cuda_graph(batch):   # decode 且 size <= max_graph_bs
        logits = graph_runner.replay(batch)      # 走 graph
    else:
        logits = model.forward()                 # prefill 或超大 batch 走普通前向
```

`replay` 内部:

```text
assert can_use_cuda_graph(batch)
buffer.copy_from(batch)                          # 输入三件套写进固定 buffer
g = graph_map[batch.padded_size]                 # 查这一尺寸的图
attn_backend.prepare_for_replay(batch)           # 注意力后端:重绑 wrapper + 重新 plan
g.replay()                                       # 一条命令重放整张图
return buffer.logits[:batch.size]                # 只取真实 size 的输出,dummy 槽丢弃
```

#### 4.4.3 源码精读

判定条件:

[python/minisgl/engine/graph.py:149-150](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L149-L150) —— `return batch.is_decode and batch.size <= self.max_graph_bs`。两个条件分别排除 prefill 与超大 decode 批。

回放实现:

[python/minisgl/engine/graph.py:152-158](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L152-L158) —— `copy_from` → 查 `graph_map[padded_size]` → `prepare_for_replay` → `g.replay()` → 返回 `logits[:batch.size]`。注意取的是 `batch.size` 而非 `padded_size`,丢弃 dummy 槽。

Engine 侧的二选一分支:

[python/minisgl/engine/engine.py:191-206](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L206) —— L194-197 的 `if can_use_cuda_graph: replay else: model.forward()`。注意 `replay` 返回的 logits 与 `model.forward()` 一样交给后续 `sampler.sample(logits[:batch.size], args)`,两条路径对采样器完全透明。

注意力后端的抽象协议:

[python/minisgl/attention/base.py:27-34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py#L27-L34) —— 三个抽象方法 `init_capture_graph`/`prepare_for_capture`/`prepare_for_replay`。[base.py:56-63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py#L56-L63) 的 `HybridBackend` 把它们全部转发给 `decode_backend`(prefill 后端不参与捕获)。

FlashInfer 的具体实现(展示元数据如何协同):

[python/minisgl/attention/fi.py:227-234](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L227-L234) —— `init_capture_graph`:建一份 `FICaptureData`(固定大小的 indptr/indices 缓冲),记录 `capture_bs`。
[python/minisgl/attention/fi.py:244-264](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L244-L264) —— `prepare_for_capture`:为该 bs 建一个 `CUDAGraphBatchDecodeWithPagedKVCacheWrapper`(其 indptr/indices buffer 指向 capture 固定缓冲),再 `prepare_metadata` + plan。
[python/minisgl/attention/fi.py:266-271](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L266-L271) —— `prepare_for_replay`:重绑该 bs 的 wrapper、按**本轮真实** `batch.attn_metadata.indices`(即真实 page_table 条目)重新 plan。这正是「每批 page 映射在变」的应对之策——结构(wrapper)固定、内容(indices)刷新,与 GraphCaptureBuffer「地址固定、内容可变」是同一个思想的两处落地。

#### 4.4.4 代码实践(本讲主实践)

**实践目标**:解释 CUDA graph 为何只在 decode 且 `size <= max_graph_bs` 时生效,并说明 `dummy_req` 的 `table_idx` 指向 dummy page 的作用。

**操作步骤**:

1. **decode-only 的原因**:阅读 [graph.py:149-150](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L149-L150) 与 [fi.py:203-208](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L203-L208)。注意 decode 时 `cu_seqlens_q = arange(0, bs+1)`(每条 1 个 query,固定形状);而 prefill 的 `cu_seqlens_q` 依赖各请求的 `extend_len`,每批不同。结合本讲 §2(b) 的「固定形状才能录图」论证。
2. **size <= max_graph_bs 的原因**:阅读 [graph.py:160-166](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L160-L166) 的 `pad_batch` 与 [graph.py:155](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L155) 的 `graph_map[batch.padded_size]`。超过 `max_graph_bs` 的 batch 无法 pad 到任何已捕获尺寸,只能退化普通前向。
3. **dummy page 的作用**:阅读 [engine.py:89-98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L89-L98) 与 [engine.py:57-63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L54-L63)。

**需要观察的现象**:把上述三点写成一段连贯的解释,并用本讲术语(`padded_size`/`graph_map`/`dummy page`)佐证。

**预期结果**:核心论点是「decode 形状固定可录图 + 离散捕获尺寸要求 size 可向上 pad + dummy page 让 pad 出的占位请求有安全的读写落点不污染真实 KV」。实际跑通需 GPU,本实践为源码阅读型,行为结论「待本地验证」端到端吞吐提升。

#### 4.4.5 小练习与答案

**练习 1**:`can_use_cuda_graph` 为假时(比如 prefill 或 size=300 > max_graph_bs=256),系统表现如何?

**答案**:`forward_batch` 走 `model.forward()` 普通前向,功能完全正确,只是这一批不享受 graph 加速。prefill 本身计算量大、CPU 启动开销占比低,不用 graph 损失很小;超大 decode 批较少见,偶尔退化可接受。这是一种「按形状自动降级」的设计。

**练习 2**:`replay` 里先 `copy_from` 再 `prepare_for_replay`,顺序能反过来吗?

**答案**:理论上两者操作不同 buffer(`copy_from` 写 GraphCaptureBuffer 的输入三件套,`prepare_for_replay` 操作注意力 wrapper/indices),顺序互换可能不影响正确性。但 `prepare_for_replay` 里的 plan 可能触发异步 H2D 拷贝(见 [fi.py:130-132](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L123-L148) 的 `last_event` 同步),保持 `copy_from` 在前的现序更稳妥;且 `g.replay()` 必须在两者都完成后才能执行,因为重放会消费这些 buffer 内容。源码现序是最安全的选择,不宜随意调换。

---

## 5. 综合实践

把本讲四个模块串起来,完成一次「端到端跟踪一次 decode 回放」的源码阅读任务:

1. **起点**:`Engine.forward_batch` 收到一个 `size=5` 的 decode batch([engine.py:191-197](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L197))。先确认 `can_use_cuda_graph` 返回真(给出理由:decode + 5 <= max_graph_bs)。
2. **pad**:`pad_batch` 把它补到 `padded_size=8`,`padded_reqs` 含 3 个 `dummy_req`([graph.py:160-166](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L160-L166))。说明这 3 个 dummy 的 `table_idx` 都指向哪一行、那一行指向哪个 page。
3. **copy**:`replay` 里 `buffer.copy_from(batch)` 把 5 条真实输入写进固定 buffer 的前 5 位([graph.py:154](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L152-L158))。注意 buffer 是捕获时按 `max_graph_bs` 分配的「固定地址」。
4. **attn 协同**:`prepare_for_replay` 重绑 size=8 的 wrapper、按本轮真实 page_table 重新 plan([fi.py:266-271](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L266-L271))。
5. **replay & 取结果**:`g.replay()` 一条命令重放整图,返回 `logits[:5]`,dummy 槽的 logits 被丢弃。

产出:一张时序图,标注每一步读取/写入了哪块 buffer、地址是否变化、内容从哪来。重点强调贯穿全程的 invariant——**地址固定、内容可变**。

如果你有 GPU 环境,可进一步用 `--cuda-graph-max-bs 1`(等价 shell 模式的 graph 设置,见 [args.py:232](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L225-L234))与默认值各跑一次离线 bench(参考 u11-l1),对照解释吞吐差异。「待本地验证」具体数值。

## 6. 本讲小结

- CUDA Graph 把 decode 每轮的 kernel 序列「录成图、反复重放」,把 CPU 端逐个 launch kernel 的开销压成一次 `g.replay()`,这是 decode 阶段吞吐的关键加速器。
- 它 **只对 decode、且 `size <= max_graph_bs`** 生效:decode 每条请求固定贡献 1 个 query,形状固定可录图;prefill 形状每批都变,无法录制。判定仅一行 `batch.is_decode and batch.size <= self.max_graph_bs`。
- `_capture_graphs` 对离散尺寸集(`[1,2,4] + range(8, max_bs+1, 8)`)逐个录制,**从大到小** 捕获并复用首个最大图的 `graph.pool()`,大幅节省显存;每张图录制前有一次 eager warmup 触发惰性分配。
- `GraphCaptureBuffer` 是「固定地址、可变内容」的物理载体:捕获时 `set_batch` 让 batch 输入字段别名到 buffer 切片(录下地址),回放时 `copy_from` 把新数据写进同一地址。
- `pad_batch` 用 `dummy_req` 把真实 batch 补齐到不小于它的最小捕获尺寸;`dummy_req.table_idx` 指向 `page_table` 最后一行、整行指向专用 dummy page,pad 出的占位请求安全读写 scratch 页,不污染真实 KV,其 logits 最终被 `[:batch.size]` 丢弃。
- 注意力后端用 `init_capture_graph`/`prepare_for_capture`/`prepare_for_replay` 三段协议处理「每批 page 映射在变」的元数据,与 GraphCaptureBuffer 是同一个「结构固定、内容刷新」思想的两处落地;`HybridBackend` 把该协议转发给 decode 后端。

## 7. 下一步学习建议

- **u6-l1(KV Cache 池)**:本讲反复提到的 dummy page、`num_pages + 1`、`page_table` 寻址,都在 KV cache 池里有更底层的定义。建议接着读 `kvcache/mha_pool.py` 的 `_kv_buffer` 布局与 `store_kv`,理解 `out_loc` 如何把新 K/V 写入池。
- **u7-l2(FlashInfer 后端实现)**:本讲只讲了 capture/replay 协议的调用点,完整的 `prepare_metadata` → `plan` → `wrapper.run` 链路在 u7-l2 展开,可对照理解 `cu_seqlens`/`indices` 如何构造。
- **u4-l1(Scheduler 主循环与 Overlap Scheduling)**:Graph replay 产出的 `ForwardOutput` 最终被 overlap_loop 的 `_process_last_data` 消费;回读 u4-l1 可看清 graph 加速如何嵌入「处理上一批 / 算当前批」的重叠流水线。
- **进阶**:尝试阅读 `destroy_cuda_graphs`([graph.py:169-171](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L168-L171))与 `Engine.shutdown`,理解为何必须在释放 NCCL 资源前先销毁 graph(注释提示否则会 hang),这是工程落地中容易踩的坑。
