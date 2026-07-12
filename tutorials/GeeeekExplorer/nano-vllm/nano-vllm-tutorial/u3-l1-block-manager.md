# PagedAttention 块管理 BlockManager

## 1. 本讲目标

在 [u2-l1](u2-l1-sequence-lifecycle.md) 里我们把 `Sequence` 的 `block_table` 当作"一个整数列表"用：序列按顺序占用若干**物理块**，每条序列只持有这张"块索引表"，至于块本身怎么分配、怎么回收、谁来记账，全部推给了 `BlockManager`。本讲就钻进这个"块管家"。

学完本讲，你应当能够：

1. 说清 **block（物理块）** 是什么、一个 `Block` 对象记录了哪些元数据，以及它与 GPU 上真正存 K/V 的张量槽位是什么关系。
2. 画出 `BlockManager` 的 **free / used 双池**结构，并陈述那条核心不变式："一个块要么在 free 池里且 `ref_count==0`，要么在 used 池里且 `ref_count>=1`"。
3. 解释 **引用计数（ref_count）** 为什么让多个序列能安全共享同一物理块，以及 `allocate` / `deallocate` 如何维护它。
4. 读懂 [`can_append`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L103-L104) 那一行 `len(seq) % block_size == 1` 的判据，说清 decode 在什么时刻需要新分配一块。

本讲是"显存与 KV Cache"单元（u3）的第一篇。本讲只讲**块的池化、引用计数与按需扩块**；基于内容哈希的**前缀缓存命中**（`compute_hash` / `can_allocate` 的缓存计数 / `hash_blocks`）留给 [u3-l2 Prefix Caching](u3-l2-prefix-caching.md)，**块总数与显存预算**怎么算出来留给 [u3-l3 KV Cache 显存预算](u3-l3-kv-cache-allocation.md)。

## 2. 前置知识

本讲默认你已经读过 u1 系列与 u2-l1，下面几条会直接用到：

- **token、prefill、decode、KV Cache**（u1-l1/u1-l3）：每个算过的 token 都会在显存里留一份注意力用的 K/V，供后续 token 复用。
- **`Sequence.block_table` 与分块视图**（u2-l1 §4.3）：`block_table` 是物理块编号列表，长度等于 `num_blocks`；`num_blocks = ⌈num_tokens / block_size⌉`，`block(i)` 取第 i 块对应的 token 切片。`block_size` 是 `Sequence` 的类级属性，引擎启动时由 `Config.kvcache_block_size` 统一覆盖（默认 256）。
- **调度器如何驱动序列**（u2-l1/u2-l2）：`schedule` 在 prefill 分支调 `can_allocate` / `allocate` 建表，在 decode 分支调 `can_append` / `may_append` 扩块；`preempt` 与序列结束时调 `deallocate` 回收。

> 一个直觉比喻：把整块 KV Cache 显存想成一栋**写字楼**，每层是一个固定大小的**物理块（block）**，能容纳 `block_size` 个 token 的 K/V。`BlockManager` 是物业：手里有一张**空房表（free 池）**和一张**在租表（used 池）**。每条 `Sequence` 是一家租户，它的 `block_table` 就是它租下的房间号清单。物业按需出租、退租，还允许多家租户**合租**同一间（引用计数），谁都不退就谁都不能拆。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪些部分 |
|---|---|---|
| [`nanovllm/engine/block_manager.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py) | 定义 `Block` 与 `BlockManager`，本讲的绝对主角 | `Block` 全部；`__init__` / `_allocate_block` / `_deallocate_block` / `allocate` / `deallocate` / `can_append` / `may_append`（哈希相关方法留给 u3-l2） |
| [`nanovllm/engine/sequence.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py) | 被管理的"租户" | `block_table` / `num_blocks` / `block(i)` / `__len__` / `append_token` |
| [`nanovllm/engine/scheduler.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | 调用 `BlockManager` 的"物业前台" | prefill 的 `can_allocate` / `allocate`、decode 的 `can_append` / `may_append`、`preempt` 与结束时的 `deallocate` |
| [`nanovllm/config.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py) | 提供 `kvcache_block_size` 与 `num_kvcache_blocks` | L17-L18 两个字段、L22 的断言 |

## 4. 核心概念与源码讲解

### 4.1 Block：一个物理 KV 块的"身份证"

#### 4.1.1 概念说明

PagedAttention 的核心思想是**分页**：与其给每条序列预先划一整块连续显存（必然产生碎片、也估不准长度），不如把 KV Cache 切成大量**固定大小的物理块（block）**，每块恰好容纳 `block_size` 个 token 的 K/V。序列要多少块就拿多少块，长了就再租一块，短了就退掉。这正是操作系统的分页机制：物理块 ↔ 物理页帧，`block_table` ↔ 页表。

> **关键澄清**：nano-vllm 里的 `Block` 对象**并不存储真正的 K/V 张量**。真正的 K/V 数据住在 GPU 上一块预先开好的大张量里（`ModelRunner` 分配的 `kv_cache`，见 u3-l3）。`Block` 只是这块物理槽位的**账本/身份证**，记录"这一格现在归谁、装的是什么、能不能被别人复用"。

#### 4.1.2 核心流程

一个 `Block` 记录四项元数据：

| 字段 | 含义 | 初值 |
|---|---|---|
| `block_id` | 物理块编号，即它在 `kv_cache` 大张量"块"维度上的索引 | 构造时传入 |
| `ref_count` | 引用计数：当前有几条序列的 `block_table` 指向本块 | `0` |
| `hash` | 本块内容的链式哈希值，供前缀缓存匹配（u3-l2）；`-1` 表示尚未登记 | `-1` |
| `token_ids` | 本块装下的 token 列表，用于哈希命中后的内容复核 | `[]` |

它有三个动作：

- `__init__`：崭新出厂，`ref_count=0`、`hash=-1`、`token_ids=[]`。
- `update(hash, token_ids)`：当本块的 KV 已算完、需要登记进缓存表时，记下哈希与 token。
- `reset()`：当一块**被重新分配**给新租户时，把 `ref_count` 拨到 1，并清空 `hash` / `token_ids`（旧内容作废）。

注意一个不对称设计：`reset()` 只在**分配时**调用，**回收时不调用**。也就是说，一块被退还后，它的 `hash` / `token_ids` 仍然保留——因为 GPU 槽位里的 K/V 数据还在，这块随时可能被前缀缓存重新命中（详见 u3-l2）。直到它被**再次分配**给别的内容，`_allocate_block` 里的 `reset()` 才把旧身份抹掉。

#### 4.1.3 源码精读

[`nanovllm/engine/block_manager.py#L8-L23`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L8-L23) —— `Block` 全貌，只有四个字段加两个方法：

```python
class Block:

    def __init__(self, block_id):
        self.block_id = block_id
        self.ref_count = 0
        self.hash = -1
        self.token_ids = []

    def update(self, hash: int, token_ids: list[int]):
        self.hash = hash
        self.token_ids = token_ids

    def reset(self):
        self.ref_count = 1
        self.hash = -1
        self.token_ids = []
```

几个要点：

- `ref_count` 初值是 `0` 而不是 `1`：刚建好的块没人用，必须经过 `allocate` / `_allocate_block` 才会被"领养"并把计数拨到 1。
- `update` 同时写 `hash` 和 `token_ids`：`token_ids` 留着是为了**哈希命中后做一次内容复核**（`can_allocate` 里 `self.blocks[block_id].token_ids != token_ids` 的判断，u3-l2 详讲），防止极小概率的哈希碰撞误命中。
- `reset` 把 `ref_count` 直接设为 `1`（不是 `+= 1`）：它只在"从 free 池取出一块重新出租"时调用，此时该块此前引用计数必为 0，设成 1 即"新租户入住"。

#### 4.1.4 代码实践

**目标**：亲手构造一个 `Block`，观察它的字段如何随 `update` / `reset` 变化，确认 `reset` 会清空身份而 `ref_count` 不归 `update` 管。

**操作步骤**：

```python
from nanovllm.engine.block_manager import Block

b = Block(block_id=7)
print("初始        :", b.block_id, b.ref_count, b.hash, b.token_ids)

b.update(hash=123456, token_ids=[10, 11, 12, 13])   # 模拟算完 KV 后登记
print("update 后   :", b.ref_count, b.hash, b.token_ids)

b.reset()                                            # 模拟被重新分配
print("reset 后    :", b.ref_count, b.hash, b.token_ids)
```

**需要观察的现象**：`update` 只改 `hash` / `token_ids`，`ref_count` 仍为 0；`reset` 把 `ref_count` 拨成 1 并清空 `hash` / `token_ids`。

**预期结果**：

```
初始        : 7 0 -1 []
update 后   : 0 123456 [10, 11, 12, 13]
reset 后    : 1 -1 []
```

（待本地验证：纯 Python，无需 GPU，可直接 `python` 运行。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Block` 不直接持有 K/V 张量？
**答**：K/V 数据量很大且要在 GPU 上被注意力内核直接读写，必须放在一块连续的 GPU 大张量里（`kv_cache`，u3-l3）。`Block` 只是 CPU 侧的元数据账本，靠 `block_id` 与 GPU 张量的"块"维度一一对应；把数据和账本分离，既省 CPU 内存，也让内核能用统一的 `block_table` 索引去取 K/V。

**练习 2**：`reset()` 把 `ref_count` 设成 `1`，为什么不是 `+= 1`？
**答**：`reset` 只在 `_allocate_block` 里对一块**从 free 池新取出的块**调用，而 free 池里的块必然 `ref_count==0`（见 4.2 的不变式），所以直接置 1 就是"新租户入住"。`+= 1` 的语义出现在 `allocate` 的缓存复用分支（共享一块时），那是另一条路径。

---

### 4.2 BlockManager：free / used 双池与底层分配

#### 4.2.1 概念说明

`BlockManager` 是物业。它在构造时一次性建好全部 `num_blocks` 个物理块，然后用**两个集合**把它们分成两堆：

- **free 池** `free_block_ids`：当前没人用的块，可以出租。用 `deque` 实现，**先进先出**——最早归还的块最先被重新出租（对前缀缓存的命中率友好，u3-l2 会用到这个性质）。
- **used 池** `used_block_ids`：当前至少有一条序列在用的块。用 `set` 实现，支持 \(O(1)\) 的成员判断（`allocate` 复用分支要查 `block_id in self.used_block_ids`）。

另外还有一个 `hash_to_block_id` 字典，是前缀缓存的倒排索引（内容哈希 → 块号），本讲先不展开，留给 u3-l2。

#### 4.2.2 核心流程

整池遵循一条**核心不变式**：

\[
\text{block } b \text{ 在 free 池} \iff \text{ref\_count}(b) = 0
\quad;\quad
\text{block } b \text{ 在 used 池} \iff \text{ref\_count}(b) \ge 1
\]

且 free 池与 used 池**互不相交、并集为全体块**。一切分配/回收操作都必须维护这条不变式。

底层有两个私有方法，分别完成"出租一块"和"退还一块"：

**`_allocate_block()`（出租）**：

1. 从 free 池**左侧**弹一个 `block_id`（`popleft`，FIFO）。
2. 断言它的 `ref_count == 0`（不变式要求）。
3. 如果它身上还挂着缓存哈希条目（`hash != -1` 且该哈希正指向本块），先把那条删掉——因为本块内容即将被覆盖，旧哈希不再有效。
4. `block.reset()`：`ref_count` 置 1，清空 `hash` / `token_ids`。
5. 把 `block_id` 加入 used 池，返回它。

**`_deallocate_block(block_id)`（退还）**：

1. 断言 `ref_count == 0`（调用方负责先把引用计数减到 0）。
2. 从 used 池移除，追加到 free 池**右侧**（`append`，排到队尾，等下一轮 FIFO 轮到它）。

注意第二条的**不对称**：退还时**不调用 `reset`**，于是 `hash` / `token_ids` 保留，GPU 槽位里的旧 K/V 也没被擦——这正是前缀缓存能复用"刚退还的热块"的物理基础。

#### 4.2.3 源码精读

构造函数一次性建好全池：

[`nanovllm/engine/block_manager.py#L28-L33`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L28-L33) —— 建池，free 满、used 空：

```python
def __init__(self, num_blocks: int, block_size: int):
    self.block_size = block_size
    self.blocks: list[Block] = [Block(i) for i in range(num_blocks)]
    self.hash_to_block_id: dict[int, int] = dict()
    self.free_block_ids: deque[int] = deque(range(num_blocks))
    self.used_block_ids: set[int] = set()
```

`num_blocks` 由谁决定？由 `Scheduler` 把 `config.num_kvcache_blocks` 透传进来：

[`nanovllm/engine/scheduler.py#L14-L15`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L14-L15) —— 块数与块大小都来自 `Config`：

```python
self.block_size = config.kvcache_block_size
self.block_manager = BlockManager(config.num_kvcache_blocks, config.kvcache_block_size)
```

而 `num_kvcache_blocks` 在 `Config` 里初值是占位符 `-1`，要等 `ModelRunner` 按显存预算算出来后回写（u3-l3）；`kvcache_block_size` 默认 256 且被断言约束为 256 的整数倍：

[`nanovllm/config.py#L17-L18`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L17-L18)、[`nanovllm/config.py#L22`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L22)：

```python
kvcache_block_size: int = 256
num_kvcache_blocks: int = -1
...
assert self.kvcache_block_size % 256 == 0
```

底层出租方法：

[`nanovllm/engine/block_manager.py#L43-L51`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L43-L51) —— 取一块、抹旧身份、登记在用：

```python
def _allocate_block(self) -> int:
    block_id = self.free_block_ids.popleft()
    block = self.blocks[block_id]
    assert block.ref_count == 0
    if block.hash != -1 and self.hash_to_block_id.get(block.hash) == block_id:
        del self.hash_to_block_id[block.hash]
    block.reset()
    self.used_block_ids.add(block_id)
    return block_id
```

底层退还方法：

[`nanovllm/engine/block_manager.py#L53-L56`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L53-L56) —— 只移动池子、不动 `hash` / `token_ids`：

```python
def _deallocate_block(self, block_id: int):
    assert self.blocks[block_id].ref_count == 0
    self.used_block_ids.remove(block_id)
    self.free_block_ids.append(block_id)
```

可以看到两个方法都**先断言 `ref_count == 0`**——这是不变式的守卫：只有引用计数归零的块才允许在 used↔free 之间跨界移动。

#### 4.2.4 代码实践

**目标**：直接驱动底层 `_allocate_block` / `_deallocate_block`，肉眼验证 free/used 双池的此消彼长与不变式。

**操作步骤**：

```python
from nanovllm.engine.block_manager import BlockManager

bm = BlockManager(num_blocks=4, block_size=256)

def show(tag):
    print(f"{tag:<12} free={list(bm.free_block_ids)} used={sorted(bm.used_block_ids)}")

show("init")          # free=[0,1,2,3] used=[]

b0 = bm._allocate_block()
show("alloc 1")       # free=[1,2,3] used=[0]

b1 = bm._allocate_block()
show("alloc 2")       # free=[2,3] used=[0,1]

# 引用计数由调用方负责降回 0，再退还（模拟 deallocate 的最后一步）
bm.blocks[b0].ref_count = 0
bm._deallocate_block(b0)
show("dealloc b0")    # free=[2,3,0] used=[1]   ← 注意 0 排到了队尾
```

**需要观察的现象**：每次 `_allocate_block` 从 free 池**左侧**取走、加入 used 池；退还时被 `append` 到 free 池**右侧**，所以 `0` 归还后排到了 `[2,3,0]` 的末尾——这正是 FIFO，下一轮 `popleft` 会先取 `2` 而不是 `0`。

**预期结果**：

```
init        free=[0, 1, 2, 3] used=[]
alloc 1     free=[1, 2, 3] used=[0]
alloc 2     free=[2, 3] used=[0, 1]
dealloc b0  free=[2, 3, 0] used=[1]
```

（待本地验证：纯 Python，无需 GPU。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 free 池用 `deque` 而 used 池用 `set`？
**答**：free 池需要"两端操作"——`popleft` 出租最老的块、`append` 把新退还的块排到队尾，这是 FIFO 调度，`deque` 两端都是 \(O(1)\)。used 池的核心操作是**成员判断**（`block_id in self.used_block_ids`，`allocate` 复用分支要用），`set` 的 `in` 是 \(O(1)\)，而 `deque`/`list` 是 \(O(n)\)。

**练习 2**：如果 `_deallocate_block` 里也加一句 `block.reset()`，会破坏什么？
**答**：会破坏前缀缓存。退还时保留 `hash` / `token_ids` 是为了让这块刚释放的"热块"能被下一条同前缀的序列命中复用（GPU 槽位里的 K/V 还在）。一退就 `reset`，缓存表里就找不到它了，命中率下降。所以 `reset` 故意只在**重新分配**时调用。

---

### 4.3 allocate / deallocate：prefill 一次性建表与释放

#### 4.3.1 概念说明

4.2 的两个私有方法只管"一块"的进出。真正面向序列的公开 API 是 `allocate` 与 `deallocate`：一条序列在 prefill 时一次性租下若干块、把块号填进自己的 `block_table`；在被抢占或生成结束时再把块全部退还。这两个方法把"序列视角"翻译成"块视角"，并在过程中维护引用计数。

#### 4.3.2 核心流程

**`allocate(seq, num_cached_blocks)`（prefill 建表）** 分两段：

1. **缓存复用段** `i in [0, num_cached_blocks)`：逐块用内容哈希查表，命中则复用既有块——若该块已在 used 池（别的序列正在用），就 `ref_count += 1`（合租）；若在 free 池（命中了刚退还的热块），就把它移入 used 池并置 `ref_count = 1`。把块号追加进 `block_table`。
2. **全新分配段** `i in [num_cached_blocks, num_blocks)`：剩下的块调 `_allocate_block()` 现租，追加进 `block_table`。
3. 收尾：`seq.num_cached_tokens = num_cached_blocks * block_size`，标记这些 token 的 KV 已"就位"，调度器据此跳过它们。

> 本讲聚焦"全新分配段"与引用计数；缓存复用段的哈希查表逻辑（`num_cached_blocks` 怎么算出来的）是 u3-l2 的主题。当没有前缀缓存命中时 `num_cached_blocks=0`，`allocate` 退化成"纯现租 `num_blocks` 块"，这就是本讲要吃透的主干路径。

**`deallocate(seq)`（释放）**：

1. **逆序**遍历 `seq.block_table`，对每块 `ref_count -= 1`；若归零则调 `_deallocate_block` 退还。
2. 清空 `num_cached_tokens` 与 `block_table`。

逆序遍历不是功能必需（顺序也行），更像是一种整洁习惯：先还后租的块。

引用计数的净效果：**只有当所有引用者都退还后，一块才真正回到 free 池**。这让"多条序列共享同一前缀块"变得安全——任何一方提前结束都只会让计数减一，不会误伤还在用的其它序列。

#### 4.3.3 源码精读

`allocate` 的两段循环清晰可辨：

[`nanovllm/engine/block_manager.py#L75-L92`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L75-L92) —— 先复用缓存块、再现租新块，全程维护引用计数：

```python
def allocate(self, seq: Sequence, num_cached_blocks: int):
    assert not seq.block_table
    h = -1
    for i in range(num_cached_blocks):
        token_ids = seq.block(i)
        h = self.compute_hash(token_ids, h)
        block_id = self.hash_to_block_id[h]
        block = self.blocks[block_id]
        if block_id in self.used_block_ids:
            block.ref_count += 1
        else:
            block.ref_count = 1
            self.free_block_ids.remove(block_id)
            self.used_block_ids.add(block_id)
        seq.block_table.append(block_id)
    for i in range(num_cached_blocks, seq.num_blocks):
        seq.block_table.append(self._allocate_block())
    seq.num_cached_tokens = num_cached_blocks * self.block_size
```

两点值得记：

- 开头 `assert not seq.block_table`：一条序列只允许被 `allocate` **一次**（prefill 时）。decode 阶段的扩块走的是 `may_append`，不会重复进 `allocate`。
- 缓存复用段里 `if block_id in self.used_block_ids` 用到的正是 4.2 选 `set` 实现 used 池的原因：这里需要 \(O(1)\) 判断"这块是被人合租、还是从 free 池里捞出来的热块"。

`deallocate` 简洁直接：

[`nanovllm/engine/block_manager.py#L94-L101`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L94-L101) —— 逆序减引用，归零才退还：

```python
def deallocate(self, seq: Sequence):
    for block_id in reversed(seq.block_table):
        block = self.blocks[block_id]
        block.ref_count -= 1
        if block.ref_count == 0:
            self._deallocate_block(block_id)
    seq.num_cached_tokens = 0
    seq.block_table.clear()
```

那调度器在哪些时机调它们？prefill 建表发生在 `schedule` 检查完容量之后：

[`nanovllm/engine/scheduler.py#L35-L45`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L35-L45) —— 先 `can_allocate` 问"放得下吗、能复用几块"，再 `allocate` 真正建表：

```python
if not seq.block_table:
    num_cached_blocks = self.block_manager.can_allocate(seq)
    if num_cached_blocks == -1:
        break
    num_tokens = seq.num_tokens - num_cached_blocks * self.block_size
else:
    num_tokens = seq.num_tokens - seq.num_cached_tokens
...
if not seq.block_table:
    self.block_manager.allocate(seq, num_cached_blocks)
```

`can_allocate` 返回 `-1` 表示"free 池剩下的块不够装这条序列"，调度器就 `break` 把它留在 waiting 队列等下一轮。`can_allocate` 里与本讲相关的就是这条容量检查（哈希计数部分留给 u3-l2）：

[`nanovllm/engine/block_manager.py#L71-L73`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L71-L73) —— free 池不够就返回 `-1`：

```python
if len(self.free_block_ids) < num_new_blocks:
    return -1
return num_cached_blocks
```

释放则发生在两个时机——抢占与生成结束：

[`nanovllm/engine/scheduler.py#L75-L79`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L75-L79) —— `preempt` 释放被抢序列的全部块：

```python
def preempt(self, seq: Sequence):
    seq.status = SequenceStatus.WAITING
    seq.is_prefill = True
    self.block_manager.deallocate(seq)
    self.waiting.appendleft(seq)
```

[`nanovllm/engine/scheduler.py#L89-L92`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89-L92) —— 命中终止条件时释放并移出 running：

```python
if (not seq.ignore_eos and token_id == self.eos) or seq.num_completion_tokens == seq.max_tokens:
    seq.status = SequenceStatus.FINISHED
    self.block_manager.deallocate(seq)
    self.running.remove(seq)
```

#### 4.3.4 代码实践

**目标**：用两条序列走一遍 prefill 建表，验证 `block_table` 长度 = `num_blocks`、引用计数全部为 1，且 free/used 池容量正确此消彼长。

**操作步骤**：

```python
from nanovllm.engine.sequence import Sequence
from nanovllm.engine.block_manager import BlockManager

Sequence.block_size = 4                      # 调小便于观察（引擎里是 256）
bm = BlockManager(num_blocks=10, block_size=4)

seqA = Sequence(list(range(10)))             # ceil(10/4)=3 块
seqB = Sequence(list(range(6)))              # ceil(6/4)=2 块

def show(tag):
    refs = {b.block_id: b.ref_count for b in bm.blocks if b.ref_count > 0}
    print(f"{tag:<10} free={list(bm.free_block_ids)}\n"
          f"          used={sorted(bm.used_block_ids)} refs={refs}")

show("init")
bm.allocate(seqA, 0)                         # num_cached_blocks=0：纯现租 3 块
print("A.block_table =", seqA.block_table, " num_cached_tokens =", seqA.num_cached_tokens)
show("alloc A")

bm.allocate(seqB, 0)                         # 再现租 2 块
print("B.block_table =", seqB.block_table)
show("alloc B")
```

**需要观察的现象**：`allocate` 后 `block_table` 长度恰为 `num_blocks`（A=3、B=2）；每个块的 `ref_count` 都是 1；free 池从 10 块依次减到 7、再减到 5；used 池对称增长。

**预期结果**（具体块号取决于取池顺序）：

```
init      free=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
          used=[] refs={}
A.block_table = [0, 1, 2]  num_cached_tokens = 0
alloc A   free=[3, 4, 5, 6, 7, 8, 9]
          used=[0, 1, 2] refs={0: 1, 1: 1, 2: 1}
B.block_table = [3, 4]
alloc B   free=[5, 6, 7, 8, 9]
          used=[0, 1, 2, 3, 4] refs={0: 1, 1: 1, 2: 1, 3: 1, 4: 1}
```

（待本地验证：纯 Python，无需 GPU。）

#### 4.3.5 小练习与答案

**练习 1**：`allocate` 开头为什么 `assert not seq.block_table`？
**答**：`block_table` 是 prefill 一次性建起来的，之后只允许 `may_append` 追加、`deallocate` 清空。这个断言防止同一条序列被 `allocate` 两次（比如调度器 bug 导致重复建表），避免块号被重复追加、引用计数错乱。

**练习 2**：`deallocate` 里如果某块的 `ref_count` 减完仍大于 0，这块会怎样？
**答**：它不会被退还——`if block.ref_count == 0` 不成立，跳过 `_deallocate_block`，块仍留在 used 池里。这正是引用计数的目的：这条序列虽然结束/被抢，但别的序列还通过前缀缓存共享着这块，必须等最后一个引用者离开才真正回收。

---

### 4.4 can_append / may_append：decode 跨块时按需扩块

#### 4.4.1 概念说明

prefill 一次性把 prompt 占的块建好，但 decode 每步只产 1 个 token，序列在不断变长。当新 token 落进一个**全新的块**时，`block_table` 得先长一格、租下那块新块，模型才能把它的 K/V 写进去。这一节就是看 `BlockManager` 如何用极少的代码完成"按需扩块"。

#### 4.4.2 核心流程

decode 每一步在 `schedule` 阶段先问两个问题：

1. **`can_append(seq)`：还扩得动吗？** 即"本步要写的新 token 是否会落进新块；若是，free 池还有没有空闲块"。
2. **`may_append(seq)`：那就扩。** 若确实跨进了新块，就 `_allocate_block()` 租一块、追加进 `block_table`；否则什么都不做。

判据是 `len(seq) % block_size == 1`，记 \(T =\) `num_tokens`，\(B =\) `block_size`：

\[
\text{need\_new} = \begin{cases} 1 & \text{若 } T \bmod B = 1 \\ 0 & \text{否则} \end{cases}
\]

于是 `can_append` 化简为：

\[
\text{can\_append} \iff |\text{free}| \ge \text{need\_new}
\]

**为什么是 `== 1`？** 关键在于调用时机。decode 在每步 `schedule` 开头调 `may_append`，此时 `num_tokens` 已经包含了**上一步 `postprocess` 里 `append_token` 写回的新 token**。设当前 `num_tokens = kB + 1`，意味着上一个写回的 token 其下标是 \(kB\)，正好落进**第 k 块的第 0 个槽**——一个全新的块。本步要算它的 K/V，必须先分配这块。所以"模 1"正是"上一个 token 开启了一块新块"的信号。

举一个 \(B=4\) 的具体例子（设 prefill 后 `num_tokens=11`，`block_table=[b0,b1,b2]`，b2 装了 token 8/9/10，还空 1 槽）：

| decode 步 | 调度时 num_tokens | `num_tokens % 4` | need_new | may_append | 动作 |
|---|---|---|---|---|---|
| 1 | 11 | 3 | 0 | 不扩 | token 10 的 KV 写进 b2 第 2 槽；append→12 |
| 2 | 12 | 0 | 0 | 不扩 | token 11 的 KV 写进 b2 第 3 槽；append→13 |
| 3 | 13 | 1 | 1 | **扩 b3** | token 12 的 KV 写进 b3 第 0 槽；append→14 |

第 3 步 `13 % 4 == 1` 触发扩块，`block_table` 变成 `[b0,b1,b2,b3]`。

#### 4.4.3 源码精读

这两个方法是全文件最短、也最精巧的：

[`nanovllm/engine/block_manager.py#L103-L104`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L103-L104) —— 把"是否需要新块"编成一个布尔，再与 free 池容量比较：

```python
def can_append(self, seq: Sequence) -> bool:
    return len(self.free_block_ids) >= (len(seq) % self.block_size == 1)
```

注意 `(len(seq) % self.block_size == 1)` 是个 `bool`，与整数比较时 `True` 当 1、`False` 当 0，于是这一行等价于 `free >= need_new`。`len(seq)` 走的是 `Sequence.__len__`，返回 `num_tokens`（[`nanovllm/engine/sequence.py#L33-L34`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L33-L34) 见 u2-l1）。

[`nanovllm/engine/block_manager.py#L106-L108`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L106-L108) —— 跨块才分配，否则什么也不做：

```python
def may_append(self, seq: Sequence):
    if len(seq) % self.block_size == 1:
        seq.block_table.append(self._allocate_block())
```

调度器的 decode 分支把两者串起来：先反复 `can_append` 检查容量，不够就 `preempt` 抢占尾部序列腾地方；够了才 `may_append` 扩块、并把序列标记为 decode：

[`nanovllm/engine/scheduler.py#L58-L72`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L58-L72) —— decode 调度主循环：

```python
while self.running and len(scheduled_seqs) < self.max_num_seqs:
    seq = self.running.popleft()
    while not self.block_manager.can_append(seq):
        if self.running:
            self.preempt(self.running.pop())
        else:
            self.preempt(seq)
            break
    else:
        seq.num_scheduled_tokens = 1
        seq.is_prefill = False
        self.block_manager.may_append(seq)
        scheduled_seqs.append(seq)
```

这里 `preempt` → `deallocate` 把被抢序列的块全部归还，正是为了给 `can_append` 让出 free 池空间（u2-l3 详讲抢占）。一旦 `can_append` 通过，立刻 `may_append` 把可能需要的新块挂上 `block_table`，保证紧接着的 `run`（前向）能用 `block_table` 把新 token 的 K/V 写进正确的槽位（`store_kvcache`，见 u4-l2）。

#### 4.4.4 代码实践

**目标**：模拟一条序列连续 decode，肉眼看到"何时触发扩块"与 `can_append` 在容量耗尽时返回 `False`。

**操作步骤**：

```python
from nanovllm.engine.sequence import Sequence
from nanovllm.engine.block_manager import BlockManager

Sequence.block_size = 4
bm = BlockManager(num_blocks=3, block_size=4)     # 只给 3 块，制造紧张感

seq = Sequence(list(range(13)))                   # ceil(13/4)=4 块 —— 注意比 free 多
print("num_blocks =", seq.num_blocks, " 但 free 只有", len(bm.free_block_ids), "块")
print("can_allocate 容量?", bm.can_allocate(seq)) # 期望 -1：free(3) < num_new(4)

# 改成一条能放下的：6 token → 2 块
bm2 = BlockManager(num_blocks=3, block_size=4)
s = Sequence(list(range(6)))
bm2.allocate(s, 0)
s.append_token(99)                                # 模拟 prefill 写回首个补全 token（num_tokens=7）
print("\n初始 num_tokens =", len(s), " block_table =", s.block_table)

for step in range(6):
    ok = bm2.can_append(s)                        # 问：还扩得动吗
    grew = False
    if ok:
        before = len(s.block_table)
        bm2.may_append(s)                         # 跨块则扩
        grew = len(s.block_table) > before
        s.append_token(100 + step)                # postprocess 写回
    print(f"step{step}: can_append={ok!s:<5} num_tokens={len(s):2d} "
          f"%4={len(s)%4} block_table={s.block_table} {'← 新块' if grew else ''}")
```

**需要观察的现象**：`num_tokens` 每增到"模 4 余 1"时（即 9、13）`may_append` 触发新块、`block_table` 增长；`can_append` 在 free 池空且又需要新块时返回 `False`（第一条序列因 `num_blocks=4 > free=3`，`can_allocate` 一开始就返回 `-1`，根本进不了 decode）。

**预期结果**（第二条序列）：

```
初始 num_tokens = 7  block_table = [0, 1]
step0: can_append=True  num_tokens= 8 %4=0 block_table=[0, 1]
step1: can_append=True  num_tokens= 9 %4=1 block_table=[0, 1, 2] ← 新块
step2: can_append=True  num_tokens=10 %4=2 block_table=[0, 1, 2]
step3: can_append=True  num_tokens=11 %4=3 block_table=[0, 1, 2]
step4: can_append=True  num_tokens=12 %4=0 block_table=[0, 1, 2]
step5: can_append=True  num_tokens=13 %4=1 block_table=[0, 1, 2, ...] ← 又该扩，但 free 已空
```

> 注意：step5 时 `num_tokens=13`、`%4==1` 需要第 4 块，而 free 池已空（3 块全在用），此时 `can_append` 应返回 `False`，本脚本里 `if ok` 会跳过扩块与 append。真实引擎在此会触发 `preempt` 抢占别的序列来腾块（u2-l3）。**待本地验证**：step5 的 `can_append` 取决于你给的总块数；若把 `num_blocks` 调大到 4 以上，则 step5 也能成功扩块。

#### 4.4.5 小练习与答案

**练习 1**：把判据从 `== 1` 改成 `== 0` 会怎样？
**答**：会**晚一块**扩块。`== 1` 表示"上一个 token 已开启新块，本步必须先分配它才能写 KV"。若改成 `== 0`，要等到该块填满、下一个 token 才触发分配，可此时上一个 token 的 K/V 已经**无处可写**（`block_table` 还没长出来），会导致 `store_kvcache` 写到越界槽位或覆盖别人。所以 `== 1` 是"提前一步把块备好"的正确时机。

**练习 2**：`can_append` 返回 `False` 时，调度器为什么不直接报错，而是去 `preempt`？
**答**：decode 阶段显存不足是常态（并发的序列太多），正确做法是**牺牲尾部序列**换空间：`preempt` 把一条 running 序列的块全释放、打回 waiting 重算，从而腾出 free 块让 `can_append` 转为 `True`。直接报错会让引擎在正常负载下崩溃；抢占换来了"在有限显存里尽量多服务序列"的弹性（u2-l3）。

---

## 5. 综合实践

把本讲的物理块、双池、引用计数、按需扩块串起来，**亲手经营一栋只有几间房的小写字楼**：实例化一个 `BlockManager`，让两条序列依次 prefill 建表、decode 扩块、再释放，全程打印 free/used 池与引用计数，验证"引用计数归零才真正回收"。

**任务**：用小 `block_size`（4）和小总块数（8），构造两条长短不同的序列，按真实调度器的节奏调用 `allocate` → `may_append`（decode 扩块）→ `deallocate`，并人为制造一次"两序列共享同一块"来观察 `ref_count=2 → 1 → 0` 的回收过程。

**参考骨架**：

```python
from nanovllm.engine.sequence import Sequence
from nanovllm.engine.block_manager import BlockManager

Sequence.block_size = 4
bm = BlockManager(num_blocks=8, block_size=4)

def dump(tag):
    refs = {b.block_id: b.ref_count for b in bm.blocks if b.ref_count > 0}
    print(f"{tag:<16} free={sorted(bm.free_block_ids)} used={sorted(bm.used_block_ids)} refs={refs}")

seqA = Sequence(list(range(10)))   # 3 块
seqB = Sequence(list(range(6)))    # 2 块
dump("init")

# 1) 两条序列 prefill 建表（无前缀缓存命中：num_cached_blocks=0）
bm.allocate(seqA, 0); dump("alloc A"); print("  A.table =", seqA.block_table)
bm.allocate(seqB, 0); dump("alloc B"); print("  B.table =", seqB.block_table)

# 2) 人为模拟「两序列共享 A 的第一块」（前缀缓存场景，详见 u3-l2）
shared = seqA.block_table[0]
bm.blocks[shared].ref_count += 1   # 假装还有一条序列也复用了这块
dump("share A[0]+1")

# 3) 释放 A：共享块 ref 2->1 不归还，其余块归还
bm.deallocate(seqA); dump("dealloc A")
print("  共享块", shared, "ref_count =", bm.blocks[shared].ref_count, "(未归零，仍在 used)")

# 4) 让 B 连续 decode，观察跨块时 may_append 扩块
seqB.append_token(90)              # 模拟 prefill 写回的首个补全 token
print("\n--- B 的 decode 扩块 ---")
for step in range(5):
    before = len(seqB.block_table)
    bm.may_append(seqB)            # schedule：跨块则扩
    seqB.append_token(100 + step)  # postprocess：写回新 token
    print(f"step{step}: num_tokens={len(seqB):2d} %4={len(seqB)%4} "
          f"table={seqB.block_table} {'← 新块' if len(seqB.block_table)>before else ''}")

# 5) 释放 B 与残留的共享块，整池回到全 free
bm.deallocate(seqB)
bm.blocks[shared].ref_count -= 1   # 模拟最后一个引用者离开
if bm.blocks[shared].ref_count == 0:
    bm._deallocate_block(shared)
dump("all freed")
```

**验收点**：

1. `alloc A/B` 后 free 池从 8 减到 5，used 池对应增长，每个块 `ref_count=1`。
2. `share A[0]+1` 后，`A.block_table[0]` 那块 `ref_count=2`。
3. `dealloc A` 时该共享块 `ref_count` 由 2 → 1，**不**进入 free 池；A 的另外两块 `1 → 0` 被归还。
4. B 的 decode 在 `num_tokens % 4 == 1` 时（第 4 步前后）扩出新块，`block_table` 增长。
5. 全部释放后 free 池恢复为全部 8 块、used 与 refs 为空。

（待本地验证：本脚本纯 Python，可直接 `python` 运行；无需 GPU 与模型权重。引用计数 >1 的真实场景由前缀缓存自动产生，将在 [u3-l2](u3-l2-prefix-caching.md) 完整展开。）

## 6. 本讲小结

- KV Cache 被切成固定大小的**物理块（block）**，`Block` 对象只是块的**账本**（`block_id` / `ref_count` / `hash` / `token_ids`），真正的 K/V 张量住在 GPU 的 `kv_cache` 大张量里（u3-l3）。
- `BlockManager` 用 **free（`deque`，FIFO）** 与 **used（`set`，\(O(1)\) 成员判断）** 双池管理全部块；核心不变式：块在 free 池 ⟺ `ref_count==0`，在 used 池 ⟺ `ref_count>=1`。
- **引用计数**让多序列可安全共享同一块：`allocate` 复用段对在用块 `ref_count+=1`、对热块置 1 并移池；`deallocate` 逆序减引用，归零才真正 `_deallocate_block` 退还。
- `reset()` 只在**分配**时调用（抹旧身份），**回收时保留** `hash`/`token_ids` 与 GPU 数据，为前缀缓存复用热块留好物理基础（u3-l2）。
- prefill 一次性 `allocate` 建 `block_table`，`assert not seq.block_table` 保证只建一次；decode 每步用 `can_append`/`may_append` 按需扩块。
- 扩块判据 `len(seq) % block_size == 1`：当上一个写回的 token 落进新块第 0 槽时，本步必须先分配这块才能写它的 K/V；`can_append` 返回 `False` 时调度器靠 `preempt` 抢占腾空间（u2-l3）。

## 7. 下一步学习建议

本讲把"块怎么被池化、租借、共享、扩容"讲透了，但它故意绕开了两块拼图：

- [u3-l2 Prefix Caching 哈希匹配机制](u3-l2-prefix-caching.md) —— 精读 `compute_hash` 的链式哈希、`can_allocate` 如何数出 `num_cached_blocks`、`hash_blocks` 如何在新块算完后把它们登记进 `hash_to_block_id`。读完你就能解释本讲综合实践里"人为 `ref_count+=1`"的真实来源。
- [u3-l3 KV Cache 显存预算与分配](u3-l3-kv-cache-allocation.md) —— 看 `num_kvcache_blocks`（本讲 `BlockManager` 的总房间数）是怎么由 `gpu_memory_utilization` 与 warmup 峰值算出来的，以及 `kv_cache` 张量如何按层挂载。
- 想看块表如何被前向真正消费，可跳到 [u4-l1 ModelRunner 与输入准备](u4-l1-model-runner-input-prep.md)（`slot_mapping` 怎么把 token 映射到块内槽位）与 [u4-l2 Attention 与 Triton store_kvcache 内核](u4-l2-attention-triton-kernel.md)（K/V 如何写进 paged cache）。

建议先把本讲综合实践跑通，确保你能凭直觉回答"一块什么时候进 free、什么时候进 used、什么时候被共享"，再进入 u3-l2。
