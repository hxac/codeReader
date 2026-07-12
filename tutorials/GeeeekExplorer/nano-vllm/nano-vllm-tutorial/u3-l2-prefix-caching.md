# Prefix Caching 哈希匹配机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「前缀缓存（Prefix Caching）」到底缓存了什么、为什么能省掉大量计算。
- 理解 `compute_hash` 如何用**链式哈希**让每个块的哈希值都依赖它之前的所有 token。
- 读懂 `can_allocate` 返回值的语义：它返回的不是布尔值，而是「命中了几个缓存块」，以及 `-1` 代表什么。
- 解释 `allocate` 如何复用缓存块、`hash_blocks` 如何把新算完的块登记进哈希表，以及为什么「只登记满块」。
- 在调度器里追踪到 `num_cached_tokens` 是如何真正把命中的 token 从前向计算里「跳过」的。

本讲承接 [u3-l1 PagedAttention 块管理 BlockManager](u3-l1-block-manager.md)：你已经知道块（block）是 KV Cache 的物理单位、`BlockManager` 用引用计数管理 free/used 双池。本讲要回答的新问题是——**怎么判断一段前缀的 KV 已经算过、可以白嫖？**

## 2. 前置知识

- **KV Cache 是因果的。** 由于 attention 是因果的（每个 token 只能看它自己和它之前的 token），一段 token 序列对应的 K/V 张量，**只取决于这段 token 本身**，与后面的 token 无关。这是一切前缀缓存能成立的物理基础：相同的 token 前缀，算出来的 K/V 永远相同。
- **块（block）与满块。** 复习 u3-l1：KV Cache 被切成固定大小（默认 256 个 token）的物理块。一个序列的 token 按顺序填进块里，只有当一个块被填满（恰好 `block_size` 个 token）时，它的内容才算「封口」、不会再变。
- **哈希函数 xxhash。** `xxhash` 是一个极快的非加密哈希。`xxh64()` 把任意字节流压成一个 64 位整数（`intdigest()`），相同输入必给相同输出。它只用来做「内容指纹」，不抗恶意碰撞，但 accidental collision 的概率低到可以忽略。
- **`num_cached_tokens` 是水位线。** 复习 [u2-l1](u2-l1-sequence-lifecycle.md)：`Sequence.num_cached_tokens` 表示「这段序列里已经算过 KV 的 token 数」。本讲会看到它在缓存命中时被直接「跳」到前缀末尾。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| `nanovllm/engine/block_manager.py` | 本讲主角。`compute_hash`/`can_allocate`/`allocate`/`hash_blocks` 全在这里，外加 `_allocate_block` 里对哈希表的维护。 |
| `nanovllm/engine/scheduler.py` | 调用方。`schedule()` 里调 `can_allocate`/`allocate`，`postprocess()` 里调 `hash_blocks`，并用 `num_cached_tokens` 跳过已缓存 token。 |
| `nanovllm/engine/sequence.py` | 提供 `num_blocks`、`block(i)`、`num_cached_tokens` 等视图，是哈希计算的数据来源。 |
| `nanovllm/engine/llm_engine.py` | 启动时把 `Sequence.block_size` 统一设为 `config.kvcache_block_size`（第 21 行），保证序列视图与块管理器用同一套块大小。 |

## 4. 核心概念与源码讲解

### 4.1 链式前缀哈希：compute_hash

#### 4.1.1 概念说明

前缀缓存的直觉很简单：**如果两个请求的开头一模一样，那开头这段的 K/V 已经算过一次了，第二次就不用再算。** 这在「系统提示词（system prompt）很长、用户问题很短」的场景下收益极大——每个用户都共享同一段几千 token 的系统提示，第一次算完后，后续请求都白嫖这段 KV。

但要实现它，需要一个机制回答两个问题：

1. **查询**：新来一段 token，怎么知道它的前缀是不是已经缓存过？
2. **登记**：一段刚算完的 token，怎么「存进缓存」供以后查询？

最朴素的想法是「把整段 token 当 key 存进字典」。但这有个致命问题：每来一个新 token，整段 key 都变了，查不到任何东西。

nano-vllm 的做法是**以块为单位做链式哈希**：每个块的哈希值 = `哈希(上一个块的哈希值, 本块的 token)`。这样一来：

- 第 0 块的哈希只依赖第 0 块的 token。
- 第 1 块的哈希依赖「第 0 块的哈希 + 第 1 块的 token」，等价于依赖前两块的全部 token。
- 第 \(i\) 块的哈希依赖前 \(i+1\) 块的全部 token。

写成递推式：

\[
h_i = \text{xxh64}\!\left(\text{bytes}(h_{i-1}) \;\|\; \text{bytes}(\text{tokens}_i)\right), \qquad h_{-1} = \text{「无前缀」}
\]

其中「无前缀」用一个哨兵值 `-1` 表示——`compute_hash` 看到 `prefix == -1` 就跳过把 prefix 写进哈希这一步，相当于 \(h_{-1}\) 不参与计算。

链式哈希的关键性质是**前缀单调性**：两个序列只要前 \(k\) 个块完全相同，它们第 \(k-1\) 块的哈希值就一定相同。这正是「按前缀命中」所需要的。

#### 4.1.2 核心流程

`compute_hash` 是一个类方法（不依赖 `BlockManager` 实例状态），逻辑只有三步：

1. 新建一个 `xxh64()` 哈希器。
2. 若给了 `prefix`（且不是 `-1`），先把上一个块的哈希值按 8 字节小端写进哈希器。
3. 再把本块的 token_ids 转成字节写进哈希器，返回 64 位整数摘要。

#### 4.1.3 源码精读

[compute_hash 的实现：nanovllm/engine/block_manager.py:35-41](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L35-L41)

```python
@classmethod
def compute_hash(cls, token_ids: list[int], prefix: int = -1):
    h = xxhash.xxh64()
    if prefix != -1:
        h.update(prefix.to_bytes(8, "little"))
    h.update(np.array(token_ids).tobytes())
    return h.intdigest()
```

几个要点：

- `prefix.to_bytes(8, "little")`：把上一个块的 64 位哈希值编码成定长 8 字节。定长是为了让哈希器能区分「前缀」和「token 字节」的边界——如果两个不同的 (prefix, tokens) 组合碰巧拼出相同字节流，就会误判，定长编码消除了这种歧义。
- `np.array(token_ids).tobytes()`：把 token 列表当成 numpy 整数数组的原始字节。注意这隐含假设了 token_ids 里的整数在机器上是定宽的（numpy 默认 int64），所以同一台机器上相同 token 序列一定产生相同字节。
- `intdigest()`：返回无符号 64 位整数，作为字典 key 使用。

#### 4.1.4 代码实践

**目标**：亲手验证链式哈希的「前缀单调性」。

**步骤**：

1. 写一小段脚本，直接调用 `BlockManager.compute_hash`（它是类方法，不需要实例）：

```python
# 示例代码：验证链式哈希
from nanovllm.engine.block_manager import BlockManager

block0 = [101, 102, 103, 104]
block1 = [201, 202, 203, 204]
block1_other = [999, 888, 777, 666]   # 不同的第二块

h0 = BlockManager.compute_hash(block0)                       # 无前缀
h1      = BlockManager.compute_hash(block1, h0)              # 接在 block0 后
h1_alt  = BlockManager.compute_hash(block1_other, h0)        # 同前缀，不同第二块
h1_naked = BlockManager.compute_hash(block1)                 # 不接前缀

print("h0        =", h0)
print("h1        =", h1)
print("h1_alt    =", h1_alt)
print("h1_naked  =", h1_naked)
```

2. 观察输出。

**需要观察的现象**：

- `h1 == h1_naked` 应为 **False**：同一块 token，有没有前缀、前缀是什么，会改变哈希值。这正是「链式」的体现。
- `h1 == h1_alt` 应为 **False**：前缀相同，第二块不同，哈希不同。
- 多次运行，`h0`、`h1` 等值**完全不变**（哈希是确定性的）。

**预期结果**：四行各不相同，且可复现。如果你把 `block0` 换成另一组 token 重算 `h0'`，再用同一个 `block1` 接上去 `compute_hash(block1, h0')`，得到的值又会和 `h1` 不同——证明了第 1 块的哈希编码了「前两块全部内容」。

> 若无法本地运行，明确标注「待本地验证」，但上述断言可直接从 `compute_hash` 的源码逻辑推出。

#### 4.1.5 小练习与答案

**练习 1**：如果两个序列的前 3 个块完全相同，它们的第 2 块（下标从 0 算）哈希值是否一定相同？第 3 块（如果存在）呢？

**答案**：第 2 块一定相同（前缀单调性：前 3 块相同 ⟹ 前 3 块各自的链式哈希都相同）。第 3 块**不一定**——第 3 块的哈希依赖前 4 块，而题设只保证前 3 块相同，第 3 块之后（即第 3 块本身的内容之外）若有差异会体现在第 3 块的 token 上；更准确地说，第 3 块的哈希依赖「第 2 块的哈希 + 第 3 块的 token」，第 2 块哈希相同、第 3 块 token 也相同（因为「前 3 块相同」），所以第 3 块哈希**也**相同。真正不一定的是第 4 块及以后。

**练习 2**：为什么 `prefix` 要编码成定长 8 字节，而不是直接 `str(prefix)` 之类？

**答案**：定长编码保证了「前缀部分」与「token 部分」在字节流里的边界是固定的、无歧义的。变长编码（如字符串）可能让不同的 (prefix, tokens) 组合拼出相同字节流，造成误判。定长 8 字节正好对应 64 位哈希值。

---

### 4.2 缓存命中查询：can_allocate

#### 4.2.1 概念说明

`can_allocate` 是前缀缓存的「查询」入口。它回答一个问题：**这条新序列的前缀里，有多少个块能从缓存里白嫖？**

注意它的返回值不是布尔值，而是一个整数：

- 返回 `>= 0`：命中了这么多块，可以分配，继续走 `allocate`。
- 返回 `-1`：剩余物理块不够装下这条序列（即便考虑了缓存复用），调度器必须让步、等下一轮。

为什么返回的是「命中块数」而不是「能不能」？因为下游 `allocate` 和调度器都需要精确知道「命中几块」才能算出「实际还要新分配几块」「实际还要算几个 token」。把判断和计数合并成一次扫描，既省事又自洽。

#### 4.2.2 核心流程

`can_allocate` 沿着序列的块逐个推进，复现链式哈希并在哈希表里查：

1. 初始化：`h = -1`（无前缀），`num_cached_blocks = 0`，`num_new_blocks = seq.num_blocks`（先假设所有块都要新分配）。
2. **只扫描前 `num_blocks - 1` 个块**（跳过最后一个块，原因见下）。
3. 对每个块：
   - 用链式哈希算出当前块的哈希 `h`。
   - 在 `hash_to_block_id` 里查这个哈希。
   - 若查不到，或查到的块里存的 `token_ids` 与当前块不符（防哈希碰撞/脏数据）→ `break`，前缀命中到此为止。
   - 否则 `num_cached_blocks += 1`；若该缓存块正在被别人用（在 used 池里），则复用它不需要消耗新物理块，`num_new_blocks -= 1`。
4. 最后做一次显存校验：若 `len(free_block_ids) < num_new_blocks`，返回 `-1`（装不下）；否则返回 `num_cached_blocks`。

**为什么只扫描 `num_blocks - 1` 个块？** 因为序列的**最后一个块是「活动块」**：prefill 时它正在被算、decode 时还会有新 token 追加进来，内容会变。即便它此刻恰好填满，缓存系统也保守地不把它当作可命中块——而且无论如何，最后一个块都需要参与本次前向计算才能产出用于采样的 logits。所以命中范围永远停在倒数第二个块。

#### 4.2.3 源码精读

[can_allocate 的实现：nanovllm/engine/block_manager.py:58-73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L58-L73)

```python
def can_allocate(self, seq: Sequence) -> int:
    h = -1
    num_cached_blocks = 0
    num_new_blocks = seq.num_blocks
    for i in range(seq.num_blocks - 1):
        token_ids = seq.block(i)
        h = self.compute_hash(token_ids, h)
        block_id = self.hash_to_block_id.get(h, -1)
        if block_id == -1 or self.blocks[block_id].token_ids != token_ids:
            break
        num_cached_blocks += 1
        if block_id in self.used_block_ids:
            num_new_blocks -= 1
    if len(self.free_block_ids) < num_new_blocks:
        return -1
    return num_cached_blocks
```

逐行拆解三个关键设计：

- `self.blocks[block_id].token_ids != token_ids`（第 66 行）：**内容校验，防脏读**。即便哈希命中，也要确认那个物理块里当年登记的 token 和现在查的 token 逐位相同。这是对抗哈希碰撞（概率极低但非零）和任何潜在不一致的最后防线。能查到 `block_id` 但 token 对不上，就当作「没命中」`break`。
- `if block_id in self.used_block_ids: num_new_blocks -= 1`（第 69-70 行）：**引用计数的联动**。命中的块如果正在被别的活跃序列用着（used 池），那么本次复用只是给它 `ref_count += 1`，**不消耗任何新物理块**，所以从「需要新分配」的计数里扣掉。反之，命中的块如果在 free 池（「热块」，KV 数据还留着但当前没人用），复用它会把一块从 free 搬到 used，**要消耗一块 free**，所以不扣。
- 最后的显存校验（第 71-72 行）：`num_new_blocks` 现在准确表示「本次分配需要从 free 池拿走几块」。free 池不够就返回 `-1`，让调度器让步。

#### 4.2.4 代码实践

**目标**：直观看到「首次查询返回 0、有缓存时返回命中块数、显存不足时返回 -1」。

**步骤**：

```python
# 示例代码：观察 can_allocate 的返回值
from nanovllm.engine.sequence import Sequence
from nanovllm.engine.block_manager import BlockManager
from nanovllm.sampling_params import SamplingParams

Sequence.block_size = 4                      # 学习演示用小块（绕过 Config 的 %256 断言）
bm = BlockManager(num_blocks=8, block_size=4)

seq = Sequence([1,2,3,4, 5,6,7,8, 9,10], SamplingParams())  # 10 token = 2 满块 + 1 半块
print("num_blocks       =", seq.num_blocks)  # 3
print("can_allocate(首次) =", bm.can_allocate(seq))  # 0：哈希表空，无命中

# 故意把 free 池抽干，看 -1 分支
bm_tiny = BlockManager(num_blocks=0, block_size=4)
print("can_allocate(空池) =", bm_tiny.can_allocate(seq))  # -1：num_new_blocks=2 > free=0
```

**需要观察的现象**：

- 首次查询返回 `0`：哈希表为空，循环里第一次 `get(h, -1)` 就拿不到，`break`，`num_cached_blocks` 停在 0，free 池够（8 ≥ 2），返回 0。
- 空池查询返回 `-1`：`num_new_blocks` 至少是 2（最后一个半块也要算进 `num_blocks`），free 池只有 0 块，触发 `len(free) < num_new_blocks`，返回 `-1`。

**预期结果**：分别打印 `3`、`0`、`-1`。

> 这段不需要 GPU，纯内存逻辑，可直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：一条序列有 5 个满块 + 1 个半块（共 6 个块），哈希表里恰好命中了前 4 个块。`can_allocate` 循环会执行几次迭代？返回值是多少？

**答案**：循环上界是 `num_blocks - 1 = 5`，但命中到第 4 块后第 5 块（下标 4）查不到会 `break`，所以实际迭代 5 次（i = 0..4），返回 `num_cached_blocks = 4`。注意：即便前 5 个块都命中，循环也只会跑到 i=4（第 5 个块，下标 4）就到上界，命中数仍是 5——但题设只命中前 4 块，所以第 5 块（下标 4）那次 `get` 失败 `break`，返回 4。

**练习 2**：为什么命中的块在 **used 池** 时 `num_new_blocks -= 1`，而在 **free 池**（热块）时不减？

**答案**：used 池里的块正被别的序列引用，复用只是 `ref_count += 1`，不需要从 free 池拿新块，所以「需要新分配的块数」减一。free 池里的热块虽然 KV 数据还在，但它本身就占着 free 池的一个名额——复用它相当于「从 free 拿一块」，消耗一个 free 名额，所以仍计入 `num_new_blocks`。

---

### 4.3 命中后的块复用与 token 跳过：allocate 与调度器衔接

#### 4.3.1 概念说明

`can_allocate` 只告诉了命中几块，真正「把缓存落袋为安」的是 `allocate`。它做两件事：

1. **复用缓存块**：对命中的块，要么 `ref_count += 1`（在 used 池，共享），要么从 free 搬到 used（热块，独占），把它们填进序列的 `block_table`。
2. **补齐新块**：命中范围之后的块（包括那个活动块）通过 `_allocate_block()` 新分配，也填进 `block_table`。

最关键的一步是设置水位线：

\[
\text{seq.num\_cached\_tokens} = \text{num\_cached\_blocks} \times \text{block\_size}
\]

这把 `num_cached_tokens` 直接「跳」到缓存前缀的末尾。之后调度器用一行算式把这部分 token 从前向计算里彻底剔除：

\[
\text{num\_tokens\_to\_compute} = \text{seq.num\_tokens} - \text{num\_cached\_blocks} \times \text{block\_size}
\]

也就是说，命中 \(k\) 个块，就少算 \(k \times \text{block\_size}\) 个 token 的 prefill——这才是前缀缓存真正省算力的地方。

#### 4.3.2 核心流程

`allocate(seq, num_cached_blocks)`：

1. 断言 `seq.block_table` 为空（只在序列首次分配时调用）。
2. 对前 `num_cached_blocks` 个块：重算链式哈希定位 `block_id`，按池属相更新引用计数与池归属，append 进 `block_table`。
3. 对剩余块：`_allocate_block()` 新分配，append 进 `block_table`。
4. `seq.num_cached_tokens = num_cached_blocks * block_size`（关键水位跳转）。

调度器侧（`schedule()`）的衔接只有三行（见下一节源码）：调 `can_allocate` → 若非 `-1` 则 `allocate` → 用 `num_cached_blocks * block_size` 算出真正要算的 token 数。

#### 4.3.3 源码精读

[allocate 的实现：nanovllm/engine/block_manager.py:75-92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L75-L92)

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

注意第 81 行 `block_id = self.hash_to_block_id[h]` 直接用 `[h]` 取值而不做 `.get`——因为 `num_cached_blocks` 是 `can_allocate` 刚算出来的，保证这些哈希必然命中，所以省掉了重复校验。第 92 行的水位跳转是后续 token 跳过的关键。

[调度器里的衔接：nanovllm/engine/scheduler.py:35-45](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L35-L45)

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

第 39 行就是「跳过」的算式：`num_tokens`（本次 prefill 真正要喂给模型的 token 数）= 总 token 数 − 命中块数 × 块大小。命中越多，`num_tokens` 越小，前向越快。第 44-45 行才真正调 `allocate` 落实块归属（此时 `num_cached_tokens` 被设为 `num_cached_blocks * block_size`，与本步 `num_scheduled_tokens` 的累加相呼应，见 [u2-l2](u2-l2-scheduler-prefill-decode.md)）。

#### 4.3.4 代码实践

**目标**：验证命中后 `num_cached_tokens` 的跳转，以及调度器算式能正确算出「实际计算 token 数」。

**步骤**：在 4.2.4 的脚本基础上继续：

```python
# 示例代码：手动登记一次缓存（复用 hash_blocks 的最小逻辑），再查第二条序列
from nanovllm.engine.sequence import Sequence
from nanovllm.engine.block_manager import BlockManager
from nanovllm.sampling_params import SamplingParams

Sequence.block_size = 4
bm = BlockManager(num_blocks=8, block_size=4)

shared = [101,102,103,104, 105,106,107,108]          # 2 个满块的前缀
seq_a = Sequence(shared + [201,202,203,204], SamplingParams())   # 12 token，3 满块

# 模拟「seq_a 已 prefill 完」：分配 + 登记
bm.allocate(seq_a, bm.can_allocate(seq_a))            # can_allocate 返回 0
seq_a.num_scheduled_tokens = seq_a.num_tokens - seq_a.num_cached_tokens  # 12
bm.hash_blocks(seq_a)                                  # 把满块登记进哈希表
seq_a.num_cached_tokens += seq_a.num_scheduled_tokens  # 模拟 postprocess 的水位推进

# 第二条序列共享前缀
seq_b = Sequence(shared + [301,302], SamplingParams())  # 10 token = 2 满块 + 1 半块
cached = bm.can_allocate(seq_b)
print("seq_b 命中块数    =", cached)                   # 2
bm.allocate(seq_b, cached)
print("seq_b num_cached_tokens =", seq_b.num_cached_tokens)         # 8 = 2*4
print("seq_b 实际需计算 token   =", seq_b.num_tokens - cached*bm.block_size)  # 10-8 = 2
```

**需要观察的现象**：

- `seq_b 命中块数 = 2`：前两个满块命中。
- `seq_b.num_cached_tokens = 8`：水位直接跳到前缀末尾。
- `实际需计算 token = 2`：原本 10 个 token 的 prefill，因为缓存只算 2 个——省了 80% 的 prefill 计算。

**预期结果**：依次打印 `2`、`8`、`2`。

> 本实践为「源码阅读型 + 逻辑型」，无需 GPU。运行后可对照 `scheduler.py:39` 的算式，确认结论一致。

#### 4.3.5 小练习与答案

**练习 1**：`allocate` 第 81 行用 `self.hash_to_block_id[h]` 直接索引，万一 `h` 不在表里会 `KeyError`。为什么这里可以放心直接索引？

**答案**：因为传入的 `num_cached_blocks` 来自上一秒刚执行的 `can_allocate`，它已经逐块验证过这 `num_cached_blocks` 个哈希都命中且 token 匹配。两步之间没有别的代码改动哈希表，所以这里必然命中，省掉重复 `.get` 校验。

**练习 2**：命中块在 used 池和在 free 池时，`allocate` 的处理有什么不同？

**答案**：used 池：只 `ref_count += 1`（共享，多一个引用者），池归属不变。free 池（热块）：`ref_count = 1`，并把该块从 `free_block_ids` 移除、加入 `used_block_ids`（从「待回收」转为「在用」）。两者都把 `block_id` append 进 `seq.block_table`。

---

### 4.4 把新块登记进缓存表：hash_blocks

#### 4.4.1 概念说明

前面三个模块解决了「查」和「用」，`hash_blocks` 解决「存」：一段刚算完 KV 的 token，要把对应块的哈希登记进 `hash_to_block_id`，以后才能被 `can_allocate` 查到。

`hash_blocks` 由调度器的 `postprocess` 在每一步前向结束后调用（见 [scheduler.py:83](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L83)）。它的两个设计要点是：

1. **只登记「满块」**：只有被填满的块才内容固定、值得缓存。半块还会变，登记了也是脏数据。
2. **链式接续**：从上一个已登记块的哈希接力，保证新生成的哈希和未来 `can_allocate` 重算的哈希完全一致。

#### 4.4.2 核心流程

`hash_blocks(seq)` 通过两个块下标界定「本次新成为满块」的范围：

\[
\text{start} = \lfloor \text{seq.num\_cached\_tokens} / \text{block\_size} \rfloor
\]
\[
\text{end} = \lfloor (\text{seq.num\_cached\_tokens} + \text{seq.num\_scheduled\_tokens}) / \text{block\_size} \rfloor
\]

注意：这里的 `num_cached_tokens` 是**本步推进之前**的水位（`postprocess` 先调 `hash_blocks`，再做 `num_cached_tokens += num_scheduled_tokens`，见 [scheduler.py:83-84](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L83-L84)）。所以：

- `start` = 本步开始前已经「封口」的满块数（这些块之前就登记过了，跳过）。
- `end` = 加上本步新算的 token 后，总共封口的满块数。
- 区间 `[start, end)` 就是「本步新封口的满块」，需要登记。

若 `start == end`（本步没有新封口的满块，比如半块还在继续填），直接返回什么都不做。否则，取 `blocks[block_table[start-1]].hash` 作为链式起点（若 `start == 0` 则用 `-1`），对 `[start, end)` 的每个块重算链式哈希、`block.update(h, token_ids)` 写回、`hash_to_block_id[h] = block_id` 登记。

**为什么 `start > 0` 时用 `blocks[block_table[start-1]].hash` 作为起点？** 因为第 `start-1` 个块是上一轮已经登记过的满块，它的 `.hash` 字段里存的就是当时的链式哈希。从这里接力，能保证第 `start` 块的哈希 = `compute_hash(tokens_start, hash_of_block_{start-1})`，和 `can_allocate`/`allocate` 从 `-1` 一路推上来的结果完全一致。

#### 4.4.3 源码精读

[hash_blocks 的实现：nanovllm/engine/block_manager.py:110-120](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L110-L120)

```python
def hash_blocks(self, seq: Sequence):
    start = seq.num_cached_tokens // self.block_size
    end = (seq.num_cached_tokens + seq.num_scheduled_tokens) // self.block_size
    if start == end: return
    h = self.blocks[seq.block_table[start - 1]].hash if start > 0 else -1
    for i in range(start, end):
        block = self.blocks[seq.block_table[i]]
        token_ids = seq.block(i)
        h = self.compute_hash(token_ids, h)
        block.update(h, token_ids)
        self.hash_to_block_id[h] = block.block_id
```

要点：

- 第 113 行 `if start == end: return`：本步没有产生新的满块（最常见于 decode 阶段，每步只加 1 个 token，远不足以填满一个 256-token 的块），直接跳过，零开销。
- 第 114 行：链式起点从已登记的前一个块接力，保证哈希链不断。
- 第 119 行 `block.update(h, token_ids)`：把哈希和 token 都写进 `Block` 账本（见 [Block.update：block_manager.py:16-18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L16-L18)）。这个 `token_ids` 正是 `can_allocate` 第 66 行做内容校验时比对的数据来源。
- 第 120 行：登记进哈希表，此后即可被命中。

**延伸：块被重用时如何避免脏读（哈希表一致性）。** 当一个热块被重新分配给新内容时，`_allocate_block` 会主动把它的旧哈希从 `hash_to_block_id` 里删掉，再 `reset()` 抹掉 `hash` 和 `token_ids`：

[_allocate_block 的哈希维护：nanovllm/engine/block_manager.py:43-51](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L43-L51)

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

第 47-48 行保证：一块物理存储被改写前，它旧的「内容指纹」先从查询表里摘掉，避免未来 `can_allocate` 拿着旧哈希查到这块、却读到新内容的脏数据。而 `deallocate` 归还块时**保留** `hash`/`token_ids`（KV 数据也留在 GPU），让它作为「热块」继续可被命中——这就是 u3-l1 讲过的「reset 只在分配时抹身份、回收时保留」的前缀缓存落点。

#### 4.4.4 代码实践

**目标**：观察 `hash_blocks` 的 `[start, end)` 区间在不同场景下的取值，理解「只登记满块」。

**步骤**：阅读 [postprocess：scheduler.py:81-92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L81-L92)，手动推演下面两个场景下 `hash_blocks` 内部的 `start`、`end`（设 `block_size = 256`）：

| 场景 | 调用 `hash_blocks` 时的 `num_cached_tokens` | `num_scheduled_tokens` | start | end | 是否登记 |
| --- | --- | --- | --- | --- | --- |
| A：prefill 算完 600 个新 token（之前 0 缓存） | 0 | 600 | 0 | 2 | 登记块 0、1 |
| B：decode 一步，只加 1 个 token（当前块已有 100 个） | 100 | 1 | 0 | 0 | 不登记 |

**需要观察的现象 / 推演结论**：

- 场景 A：`end = 600 // 256 = 2`，登记前 2 个满块；第 3 个块（88 个 token，未满）**不登记**。这与 `can_allocate` 只扫 `num_blocks - 1` 块严格对应——半块永远不会被登记，也永远不会被查。
- 场景 B：`end = 101 // 256 = 0`，`start == end`，第 113 行直接 `return`，零开销。decode 绝大多数步都走这条快速返回。

**预期结果**：能口算出上表两行的 `start/end`，并解释「为什么 decode 一般不产生新的缓存登记」。

> 这是「源码阅读型实践」，重点在理解 `start/end` 的整数除法语义，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：假设 `block_size = 256`，一条序列分块 prefill：第一步算了 300 个 token，第二步算剩下 300 个 token（共 600）。两步 `hash_blocks` 分别登记哪些块？

**答案**：第一步：`num_cached_tokens=0, num_scheduled_tokens=300`，`start=0, end=300//256=1`，登记块 0（256 个 token 满块）；剩余 44 个 token 在块 1，未满不登记。第二步：此时 `num_cached_tokens=300, num_scheduled_tokens=300`，`start=300//256=1, end=600//256=2`，登记块 1（块 1 在第二步被填满到 256）。链式起点取 `blocks[block_table[0]].hash`（块 0 上一步登记的哈希）接力。

**练习 2**：为什么 `hash_blocks` 在 `start > 0` 时从 `blocks[block_table[start-1]].hash` 接力，而不是从 `-1` 重算？

**答案**：为了与 `can_allocate`/`allocate` 的扫描结果严格一致——后者从 `-1` 一路推到第 `start-1` 块得到的哈希，正好等于前者存在 `blocks[...].hash` 里的值。从 `.hash` 接力避免了重复计算前 `start` 个块，且保证链不断。若从 `-1` 重算也能得到同样的 `h`，但会多算 `start` 次哈希，浪费。

**练习 3**：一个物理块被 `_allocate_block` 重新分配给新内容前，第 47-48 行删掉它的旧哈希。如果**不删**会发生什么？

**答案**：`hash_to_block_id[旧哈希]` 仍指向这块物理存储，但块的内容马上被新 token 覆盖。之后某条序列的前缀恰好算出这个旧哈希时，`can_allocate` 会查到这块、但 `self.blocks[block_id].token_ids` 已经是新内容（或被 `reset` 清空），内容校验 `!= token_ids` 触发 `break`——靠第 66 行的校验兜底，不会真的脏读，但会无谓地中断一次本可继续的命中扫描。删旧哈希是为了保持表干净、让校验成为纯防御而非常态触发。

## 5. 综合实践

把本讲四个模块串起来，完成一个**端到端的前缀缓存命中演示**（纯逻辑、无需 GPU）。

**任务**：构造两条共享前缀的序列，跑通「分配 → 登记 → 第二条命中 → 跳过计算」的完整链路，打印每一步的关键量，验证缓存命中确实把 prefill token 数砍掉了。

**操作步骤**：把下面脚本存为 `nano-vllm-tutorial/cache_demo.py` 并运行（注意它只读取 `nanovllm` 包、不修改任何源码）：

```python
# 示例代码：前缀缓存命中端到端演示（无需 GPU）
from nanovllm.engine.sequence import Sequence
from nanovllm.engine.block_manager import BlockManager
from nanovllm.sampling_params import SamplingParams

# 用小块便于观察；真实默认 block_size=256，这里仅作学习演示
Sequence.block_size = 4
bm = BlockManager(num_blocks=8, block_size=4)

shared = [101, 102, 103, 104,   # 块 0
          105, 106, 107, 108]   # 块 1   —— 两个满块的共享前缀

# ---- 第一次：seq_a，无缓存 ----
seq_a = Sequence(shared + [201, 202, 203, 204], SamplingParams())  # 12 token，3 满块
print("== seq_a（首次）==")
print("  can_allocate        =", bm.can_allocate(seq_a))            # 0
bm.allocate(seq_a, bm.can_allocate(seq_a))
seq_a.num_scheduled_tokens = seq_a.num_tokens - seq_a.num_cached_tokens
print("  block_table         =", seq_a.block_table)
bm.hash_blocks(seq_a)                                              # 登记满块 0、1、2
seq_a.num_cached_tokens += seq_a.num_scheduled_tokens              # 模拟 postprocess
print("  hash_to_block_id    =", bm.hash_to_block_id)              # 应有 3 条

# ---- 第二次：seq_b，共享前 2 块 ----
seq_b = Sequence(shared + [301, 302], SamplingParams())            # 10 token = 2 满块 + 1 半块
print("== seq_b（共享前缀）==")
cached = bm.can_allocate(seq_b)
print("  can_allocate(命中)  =", cached)                           # 2
bm.allocate(seq_b, cached)
print("  num_cached_tokens   =", seq_b.num_cached_tokens)          # 8
print("  block_table         =", seq_b.block_table)                # 前两个复用 seq_a 的块号
real_compute = seq_b.num_tokens - cached * bm.block_size
print("  实际需计算 token    =", real_compute)                     # 2
print("  省掉的比例          = {:.0%}".format(1 - real_compute / seq_b.num_tokens))
```

**需要观察的现象**：

1. `seq_a` 首次 `can_allocate = 0`，登记后 `hash_to_block_id` 出现 3 条记录。
2. `seq_b` `can_allocate = 2`（命中两个共享块），`num_cached_tokens` 直接跳到 8。
3. `seq_b.block_table` 的前两个块号与 `seq_a` 相同（复用了同一物理块），第三个是新块号。
4. 实际只需计算 2 个 token，省掉 80% 的 prefill。

**预期结果**：依次输出命中块数 `2`、缓存水位 `8`、实际计算 token `2`、省算比例 `80%`。对照 [scheduler.py:39](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L39) 的算式 `num_tokens = seq.num_tokens - num_cached_blocks * self.block_size`，结论应完全一致。

> 进阶：把 `shared` 的第二块改成与 `seq_a` 不同（如 `[105,106,107,999]`），重跑会发现 `seq_b` 命中块数降为 `1`，验证链式哈希对前缀完整性的要求。

## 6. 本讲小结

- 前缀缓存成立的基础是 KV Cache 的**因果性**：相同 token 前缀算出的 K/V 恒相同，所以可缓存复用。
- `compute_hash` 用**链式哈希** \(h_i = \text{xxh64}(\text{bytes}(h_{i-1}) \,\|\, \text{bytes}(\text{tokens}_i))\) 让每个块的指纹都编码它之前全部 token，保证前缀单调性。
- `can_allocate` 返回**命中的满块数**（不是布尔值），扫描只到 `num_blocks - 1`（最后一个块是活动块），用 `token_ids` 内容校验防脏读，用 `num_new_blocks` 做显存校验，不够则返回 `-1`。
- `allocate` 把命中块按引用计数复用、补齐新块，并把 `num_cached_tokens` 直接跳到 `num_cached_blocks * block_size`；调度器据此用 `num_tokens - num_cached_blocks * block_size` 把缓存部分从 prefill 里剔除。
- `hash_blocks` 在 `postprocess` 里把**本步新封口的满块**登记进 `hash_to_block_id`，区间 `[start, end)` 由整数除法界定，`start == end` 时零开销返回；链式哈希从 `blocks[block_table[start-1]].hash` 接力。
- `_allocate_block` 在物理块改写前主动摘除旧哈希，`deallocate` 回收时保留 `hash/token_ids` 让热块可继续命中，二者共同维持哈希表与物理存储的一致。

## 7. 下一步学习建议

- 缓存命中的前提是有足够物理块做后盾。这些物理块的总数从哪来？建议接着学 [u3-l3 KV Cache 显存预算与分配](u3-l3-kv-cache-allocation.md)，看 `ModelRunner.allocate_kv_cache` 如何按剩余显存和 `gpu_memory_utilization` 算出 `num_kvcache_blocks`。
- 想看缓存命中后模型侧如何真正「跳过」那些 token、只算剩余部分，可跳到 [u4-l1 ModelRunner 与输入准备](u4-l1-model-runner-input-prep.md)，观察 `prepare_prefill` 如何用 `num_cached_tokens` 构造 `cu_seqlens` 与 `slot_mapping`。
- 想从调度器视角把 prefill/decode/抢占与缓存的交互看完，可重读 [u2-l2](u2-l2-scheduler-prefill-decode.md) 与 [u2-l3](u2-l3-chunked-prefill-preemption.md)，关注 `num_cached_tokens` 在状态迁移中的水位变化。
