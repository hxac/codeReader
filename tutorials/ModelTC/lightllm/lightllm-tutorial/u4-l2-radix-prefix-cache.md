# RadixCache 前缀缓存机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 RadixCache 为什么用「基数树（radix tree）」来组织 prompt 的 KV Cache，以及它解决了什么问题。
- 读懂 `TreeNode` 的字段含义，理解「token 序列当 key、KV 显存索引当 value」这一核心抽象。
- 描述一次 prefill 是如何通过 `match_prefix` 命中并复用历史 KV、又如何通过 `insert` 把新算出的 KV 写回树的。
- 解释 `ref_counter` 引用计数与 `evict` 淘汰策略如何共同决定哪些 KV 被保留、哪些被回收。
- 理解 RadixCache 如何与 u4-l1 的「索引 + 大 buffer」内存管理、以及 u2-l5 的 Router 调度衔接。

本讲承接 u4-l1（KV Cache 内存管理：索引与大 buffer）和 u2-l5（Router 调度循环），是 LightLLM「token 级 KV 管理」这一特色的纵深展开。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

**(1) LLM 推理里有大量重复前缀。** 实际线上服务里，很多请求共享同一段「系统提示词（system prompt）」、同样的 few-shot 示例、或者同一段很长的对话历史。每一段 prompt 在 prefill 阶段都要算一遍 KV Cache（详见 u3-l2）。如果每个请求都从头算这段公共前缀的 KV，是非常浪费的——尤其是 system prompt 动辄几千 token。

**(2) KV Cache 已经被「索引化」管理。** 回顾 u4-l1：LightLLM 把所有 KV 存在一块巨大的 `kv_buffer` 里，每个 token 的 KV 占据一个「槽位」，槽位用一个整数索引表示。请求持有的不是 KV 张量本身，而是一张「第 i 个 token → 槽位索引」的映射表 `req_to_token_indexs`。**这意味着「复用一段 KV」就等价于「复用一段槽位索引」——把别人的索引抄进自己的映射表即可，根本不用拷贝 KV 张量。** RadixCache 就是把这种「复用索引」的能力系统化、自动化的机制。

有了这两点，RadixCache 的本质就清楚了：它是一棵按 token 序列前缀共享的树，树上每个节点存「一段 token 序列 → 这段序列对应的槽位索引」。新请求来了先在树上找最长公共前缀，把命中部分的索引抄走（= 跳过这段 prefill）；请求结束后再把自己新算出的那部分索引写回树，供未来的请求复用。

> 术语提示：
> - **基数树（radix tree）/ 压缩前缀树**：一种树，公共前缀只存一份在父节点，分叉后才各自存。相比普通 trie，它一条边可以存多个 token，更紧凑。
> - **prefix cache / prompt cache**：缓存历史 prompt 的 KV，让重复前缀的请求命中复用。
> - **evict（淘汰）**：显存不够时，按某种「冷热」策略把不再被引用的缓存删掉，腾出槽位。
> - **引用计数（ref counter）**：记录一个节点当前被多少个在跑的请求「借走」了，大于 0 就不能被淘汰。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，辅以它的调用方：

| 文件 | 作用 |
| --- | --- |
| [radix_cache.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py) | RadixCache 与 TreeNode 的全部实现：树结构、`match_prefix`、`insert`、`evict`、引用计数、合并。本讲主角。 |
| [infer_batch.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py) | `InferReq` 在 prefill 前 `_match_radix_cache()` 命中前缀、在请求释放时 `_full_att_free_req()` 把新 KV 写回树。是 RadixCache 的两个真实调用点。 |
| [generic_pre_process.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py) | `prepare_prefill_inputs` 在 alloc 新槽位前，调用 `free_radix_cache_to_get_enough_token` 先淘汰足够 token。 |
| [base_backend.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) | `ModeBackend.init_model` 里创建 `RadixCache` 实例并注册到全局上下文；周期性触发 `merge_unreferenced_nodes`。 |
| [manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py) | Router 进程用只读客户端 `RadixCacheReadOnlyClient` 读共享内存里的缓存统计，用于调度准入估计。 |
| [test_radix_cache.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/unit_tests/server/router/dynamic_prompt/test_radix_cache.py) | 不依赖 GPU 的纯算法单元测试，是本讲代码实践的基础。 |

> 注意：还有一个姊妹文件 `linear_att_radix_cache.py`（LinearAtt 混合模型用），本讲不展开，理解了普通 RadixCache 后它只是变体。

---

## 4. 核心概念与源码讲解

### 4.1 基数树：RadixCache 的存储骨架

#### 4.1.1 概念说明

RadixCache 把「token 序列 → KV 槽位索引」的映射组织成一棵基数树。每个节点 `TreeNode` 存三样东西：

- `token_id_key`：这一段**连续的 token id 序列**（树的「边」上携带的 key）。
- `token_mem_index_value`：这段 token 各自对应的 **KV 槽位索引**（这就是被复用的「value」）。
- `children`：按「下一段 token 序列的第一个 token id」索引的子节点字典——这就是分叉点。

公共前缀只保留在祖先节点里一份；只有当两个请求的 token 序列发生分叉时，才长出新的子节点。这样，相同前缀的 KV 在显存里只存一份、只算一次。

举一个直觉例子。假设请求 A 的 prompt 是 `[0,1,2,3,4,5,6,7,8,9]`，请求 B 的 prompt 是 `[0,1,2,3,4,7,8,9]`（前 5 个 token 相同）。它们进入树后，前 5 个 token `[0,1,2,3,4]` 只存一份在某个公共节点里；后面 `[5,6,7,8,9]` 和 `[7,8,9]` 各自分叉成两个子节点。这就是 test_case1 构造的场景（见后面代码实践）。

每个节点还带两个用于「冷热淘汰」的元数据：
- `ref_counter`：当前被多少个在跑的请求引用，>0 不能被淘汰。
- `time_id`：最近一次被访问的时间戳（单调递增整数），越小说明越「冷」。

#### 4.1.2 核心流程

一棵 RadixCache 树的生长与维护，围绕三种操作展开（本小节先讲结构本身与「插入生长」，前缀匹配在 4.2、淘汰在 4.3 讲）：

1. **插入 `insert(key, value)`**：把一段 `token序列(key) → 槽位索引(value)` 写进树。从根节点开始，按 `key[0]` 在当前节点的 `children` 里找匹配的子节点，再用 `match()` 算出 `key` 与该子节点 `token_id_key` 的公共前缀长度 `prefix_len`，按 `prefix_len` 与两者长度的大小关系分四种情况处理（命中整段、需要分裂、需要继续下钻、完全新建）。
2. **分裂 `split_node(prefix_len)`**：当一个已存在的子节点和新 key 只是「部分匹配」时，把这个子节点从 `prefix_len` 处一分为二：前半段成为新的父节点，后半段降级为它的子节点。这是基数树「按需分叉」的关键。
3. **关键不变量**：每次结构变动（插入/分裂/匹配）后，相关节点会 `update_time()` 刷新时间戳；只有**叶子节点**（无子节点）才进入淘汰候选集 `evict_tree_set`。

`match()` 是最底层的逐元素比较工具：它把两个一维张量对齐到较短长度，返回第一个不相等位置的下标（即公共前缀长度）。

#### 4.1.3 源码精读

先看 `TreeNode` 的字段定义——这是理解整个机制的基础：

[radix_cache.py:23-32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L23-L32)：定义了 `children`（按首 token id 索引）、`parent`、`token_id_key`、`token_mem_index_value`、`ref_counter`、`time_id`，以及两个长度字段 `node_value_len`（本节点存的 token 数）和 `node_prefix_total_len`（从根到本节点累计的 token 数，即「命中本节点意味着复用了多长的前缀」）。

[radix_cache.py:34-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L34-L35)：`get_compare_key()` 定义淘汰时的排序键 `(ref_counter==0?, 子节点数, time_id)`——引用计数归零的优先淘汰、子节点少的优先淘汰、更老（time_id 小）的优先淘汰。这个三元组是 4.3 淘汰策略的核心。

再看分裂——它是「部分匹配」时让树正确分叉的工具：

[radix_cache.py:37-57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L37-L57)：`split_node(prefix_len)` 新建一个「分裂父节点」，接管原节点的前 `prefix_len` 个 token 与索引，把自己挂到祖父节点下，再把原节点（只剩后半段）挂成自己的子节点，并搬走引用计数。结果是「公共前缀上浮成父节点、分歧部分下沉成子节点」。

[radix_cache.py:59-71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L59-L71)：`add_and_return_new_child` 在「完全无匹配子节点」时直接挂一个全新叶子，并维护 `node_prefix_total_len`。

底层的逐元素比较：

[radix_cache.py:85-98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L85-L98)：`match(t1, t2)` 返回两个 token 张量的公共前缀长度，全程为 0 时返回较短者长度。

最后是树的初始化：

[radix_cache.py:106-126](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L106-L126)：`RadixCache.__init__` 建一个空的 `root_node`，关键是把 `root_node.ref_counter = 1`——这保证根节点**永远不会被 evict**（淘汰断言要求 `ref_counter==0`，根节点恒不满足）。同时初始化两个跨进程共享的计数：`refed_tokens_num`（被引用的 token 数）与 `tree_total_tokens_num`（树里 token 总数），都用 `SharedArray` 存，正是为了 Router 只读客户端能读到（见 4.3.3）。

#### 4.1.4 代码实践

> 实践目标：在不依赖 GPU 的情况下，亲手观察 RadixCache 的树结构如何随插入而生长、分裂。

操作步骤（纯 CPU 即可，因为 `RadixCache` 单独使用时不需要真实 `mem_manager`）：

1. 打开 [test_radix_cache.py:6-24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/unit_tests/server/router/dynamic_prompt/test_radix_cache.py#L6-L24) 的 `test_case1`。
2. 在仓库根目录运行：
   ```bash
   python -m pytest unit_tests/server/router/dynamic_prompt/test_radix_cache.py::test_case1 -s
   ```
   （`-s` 让 `tree.print_self()` 的树形打印输出到终端。）

需要观察的现象：
- 第一次插入 `[0,1,2,3,4,5,6,7,8,9]` 后，`insert` 返回 `0`（表示这是全新插入，无命中前缀），树只有根节点下挂一个 10-token 的叶子。
- 第二次插入 `[0,1,2,3,4,7,8,9]` 时返回 `5`——前 5 个 token `[0,1,2,3,4]` 命中已有节点，于是触发 `split_node(5)`：原 10-token 节点被分裂成「前缀 `[0,1,2,3,4]` 父节点 + `[5,6,7,8,9]` 子节点」，再为 `[7,8,9]` 新挂一个子节点。
- `tree_total_tokens_num` 此刻为 13（公共前缀 5 + 分支 5 + 分支 3）。

预期结果：三条 assert 全部通过（返回值依次为 `0`、`5`、`8`，树总 token 数为 `13`）。如果终端的 `print_self` 树形结构里能看到「前缀节点下挂了两个分叉子节点」，就说明你理解了分裂。

> 说明：本实践只调用算法层，不会真正分配 GPU 显存，因此可在任意有 Python 环境的机器上跑通。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `root_node` 的 `ref_counter` 要初始化为 1？如果设成 0 会怎样？

**参考答案**：淘汰逻辑 `evict` 要求被淘汰节点 `ref_counter == 0`，且会一路从候选集里弹出节点。根节点是整棵树的「锚」，一旦被淘汰整棵树就断了。设为 1 使其永不满足淘汰条件；若设为 0，则在高负载淘汰时根节点可能被错误回收，导致树结构崩溃。

**练习 2**：`node_value_len` 和 `node_prefix_total_len` 有什么区别？哪个决定了「命中本节点能复用多长的 KV」？

**参考答案**：`node_value_len` 是本节点自己存的 token 数；`node_prefix_total_len` 是从根到本节点累计的 token 数（= 父节点前缀 + 本节点）。命中本节点意味着复用「从根到本节点」的整段前缀，所以 `node_prefix_total_len` 才是命中的 KV 长度，这正是 4.2 里 `match_prefix` 返回给请求的「命中长度」。

---

### 4.2 前缀匹配 match_prefix：一次 prefill 如何复用历史 KV

#### 4.2.1 概念说明

`match_prefix` 是 RadixCache 对外的「查询」接口：给定一段 token 序列（通常是一个新请求的 prompt），在树上找**最长公共前缀**，返回命中节点、命中长度、以及这段命中 token 对应的**槽位索引拼接张量**。

它的真正价值发生在 prefill 之前。回顾 u3-l2：prefill 是「读题」阶段，要为 prompt 的每个 token 算 KV。如果一段前缀的 KV 早已在树里（被之前的请求算过并 insert 进来了），这次就**不必重算**——只要把那段索引抄进本请求的 `req_to_token_indexs` 映射表，并把请求的 `cur_kv_len`（「已经有多少 token 的 KV 了」）直接置为命中长度，prefill 就只会去算命中点之后的那部分 token。这就是 prompt cache 的命中复用。

`match_prefix` 有一个关键参数 `update_refs`：
- `update_refs=False`：纯查询，不改变引用计数（用于估算、调度）。
- `update_refs=True`：把命中路径上每个节点的 `ref_counter += 1`，表示「这个请求正在借用这段 KV，别淘汰它」。请求真正进入推理时都用 `True`。

#### 4.2.2 核心流程

`match_prefix(key, update_refs)` 的流程：

1. 从 `root_node` 出发，按 `key` 在树上逐层下钻。
2. 每经过一个匹配的子节点，把它的 `token_mem_index_value` 追加进结果列表 `ans_value_list`。
3. 命中分两种情况：
   - 子节点的 `token_id_key` 被 `key` **完全包含**：把该子节点的索引收下，`key` 截掉这段后继续往更深的孩子走。
   - 子节点的 `token_id_key` 比 `key` 的公共前缀**更长**（即 key 只匹配到孩子的前半段）：触发 `split_node`，把孩子的前半段独立成新节点收下索引，命中到此为止。
4. 若 `update_refs=True`，命中路径上从命中节点回溯到根的每个节点 `ref_counter += 1`；当某节点 `ref_counter` 从 0 变 1 时，把它的 token 数累加进 `refed_tokens_num`（被引用 token 数）。
5. 返回 `(命中节点, 命中长度=len(拼接索引), 拼接索引张量)`；若一个 token 都没命中则返回 `(None, 0, None)`。

被命中的节点一定是「从根到命中点的链」上最后一个能匹配的节点；返回的索引是这条链上各节点索引按顺序拼接的结果。

#### 4.2.3 源码精读

`match_prefix` 的入口：

[radix_cache.py:233-246](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L233-L246)：把结果列表收集好后，用 `torch.concat` 拼成一段连续索引张量返回。注意根节点命中（一个都没匹配上）时，若 `update_refs=True` 要 `dec_node_ref_counter(root_node)` 把刚才多加的那次引用还回去，保持计数平衡。

逐层下钻的核心逻辑（用「栈」改写的非递归版本，避免深 prompt 时爆栈）：

[radix_cache.py:278-322](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L278-L322)：`_match_prefix_helper_no_recursion`。重点看三段：
- L284-L288：`update_refs` 时给当前节点 `ref_counter += 1`，且仅在 0→1 跨越时累加 `refed_tokens_num`（这是「被引用 token 数」的口径：只算被至少一个请求引用的节点）。
- L299-L301：子节点被完全包含，收下它的索引，截断 `key` 继续下钻。
- L302-L320：子节点比 key 的公共前缀更长，`split_node` 后收下分裂父节点的索引并停止。

**真实调用点**——`InferReq._match_radix_cache()`，这是请求 prefill 前命中前缀的地方：

[infer_batch.py:618-637](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L618-L637)：构造 key 时故意去掉最后一个 token（`key = key[0:len(key)-1]`，L626）——因为 prefill 需要「多喂一个 token」才能输出下一个 token 的预测。`match_prefix(..., update_refs=True)` 命中后（L627），把返回的索引 `value_tensor` 写进本请求的映射表 `req_to_token_indexs[req_idx, 0:ready_cache_len]`（L632），并把 `cur_kv_len` 置为命中长度（L633）。**这一步等价于「免费拿到前缀的 KV」**——后续 prefill 只会算 `cur_kv_len` 之后的 token，前缀部分的 KV 直接复用。

#### 4.2.4 代码实践

> 实践目标：理解 `update_refs` 对引用计数与可淘汰量的影响。

操作步骤：

1. 阅读 [test_radix_cache.py:53-79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/unit_tests/server/router/dynamic_prompt/test_radix_cache.py#L53-L79) 的 `test_case3`。
2. 运行：
   ```bash
   python -m pytest unit_tests/server/router/dynamic_prompt/test_radix_cache.py::test_case3 -s
   ```

需要观察的现象：
- 插入两段 prompt 后 `tree_total_tokens_num == 13`，`get_refed_tokens_num() == 0`（还没人引用）。
- 第一次 `match_prefix([0,1,2,3,4], update_refs=True)` 命中 5 个 token，`get_refed_tokens_num()` 变为 `5`——这 5 个 token 现在被「保护」了。
- 第二次 `match_prefix([0,1,2,3,4,7,9], update_refs=True)` 命中 6 个 token，`refed_tokens_num` 变为 `6`（注意它不是简单累加 5+6，而是「被引用节点集合」的并集 token 数）。
- 随后 `tree.evict(2, ...)` 淘汰 2 个 token：因为被引用的节点不会被淘汰，只能淘汰未被引用的叶子；结果 `tree_total_tokens_num` 从 13 降到 8，而 `refed_tokens_num` 仍是 6。
- 最后 `dec_node_ref_counter(tree_node)` 把那次匹配的引用还回去。

预期结果：所有 assert 通过。关键体感是——**`refed_tokens_num` 是「动不了的保护量」，`tree_total_tokens_num - refed_tokens_num` 才是「可淘汰量」**，这正是 4.3 淘汰逻辑的判据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `match_prefix` 构造 key 时要 `key = key[0:len(key)-1]` 去掉最后一个 token？

**参考答案**：LLM 生成需要「用已知的最后若干 token 去预测下一个 token」。请求进来时，最后一个 token 也需要参与 prefill 才能输出它的下一个 token 的预测，因此它不能被当成「已经有 KV 的前缀」命中掉。去掉最后一个 token，是为了让命中长度严格小于需要参与 prefill 的真实序列长度。

**练习 2**：`update_refs=True` 与 `update_refs=False` 各应该在什么场景使用？

**参考答案**：请求真正进入推理、要借用这段 KV 时用 `True`，把命中路径节点的引用计数加 1 以保护它们不被淘汰；请求结束后再 `dec_node_ref_counter` 归还。仅做查询/估算（比如调度器想看看当前有多少可复用前缀）时用 `False`，不改变计数、不影响淘汰。

---

### 4.3 引用计数与淘汰：决定哪些 KV 被回收

#### 4.3.1 概念说明

显存有限，不可能无限缓存所有历史 prompt 的 KV。RadixCache 用两个机制管理「留谁、删谁」：

- **引用计数 `ref_counter`**：每个节点记录「当前有多少个在跑的请求在借用它的 KV」。`>0` 表示还在被用，**绝对不能淘汰**；`==0` 表示没人用，是淘汰候选。引用计数的变化由 `add_node_ref_counter` / `dec_node_ref_counter` 维护，且会沿 `parent` 链一路向上更新（因为引用一个节点隐含引用了它全部的祖先前缀）。
- **淘汰 `evict`**：当需要腾出显存给新 token 时，从淘汰候选集 `evict_tree_set` 里按「冷度」弹出节点删掉，直到腾够为止。冷度由 `get_compare_key()` 定：先淘汰 `ref_counter==0` 的、再淘汰子节点少的、最后淘汰 `time_id` 小（最久没访问）的。

只有**叶子节点**才会进 `evict_tree_set`（一个有子节点的中间节点删了，子节点就悬空了）。删掉一个叶子后，它的父节点可能因此变成新叶子，于是被加入候选集——淘汰是「自底向上」逐层进行的。

为了让淘汰决策更高效、树更紧凑，LightLLM 还周期性做 **`merge_unreferenced_nodes`**：把「引用计数都为 0、且父节点只剩这一个孩子」的父子链合并成一个节点，减少碎片。

#### 4.3.2 核心流程

**淘汰 `evict(need_remove_tokens, evict_callback)`：**

1. 先做容量校验：若 `tree_total_tokens_num - refed_tokens_num < need_remove_tokens`，直接断言失败——因为可淘汰量不够，说明上层调度估错了。
2. 循环从 `evict_tree_set` 弹出「最冷」的叶子节点，断言它满足 `ref_counter==0 且 无子节点 且 不是根`。
3. 调用 `evict_callback(node.token_mem_index_value)` 把这段索引交还（实际由调用方 `free` 回内存管理器），并从 `tree_total_tokens_num` 扣减。
4. 从父节点摘掉自己；若父节点因此变叶子，加入候选集。
5. 直到累计淘汰够 `need_remove_tokens`。

**引用计数的增减**（以 `dec_node_ref_counter` 为例）：
1. 从命中节点出发，沿 `parent` 链一路向上。
2. 每个节点 `ref_counter -= 1`；当从 1 降到 0 时，把它的 token 数从 `refed_tokens_num` 扣减（口径与 match 时对称）。

**合并 `merge_unreferenced_nodes`**：遍历候选集里 `ref_counter==0` 的叶子，尝试 `_try_merge`——满足「父不是根、父引用为 0、父只有自己一个孩子、自己引用为 0」四条件时，把自己的 key/index 拼到父节点上、自己顶替父节点位置，从而把两个短节点合成一个长节点。

#### 4.3.3 源码精读

淘汰主循环：

[radix_cache.py:324-344](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L324-L344)：注意 L325 的容量校验、L331 用 `evict_tree_set.pop(0)`（`SortedSet` 按 `get_compare_key` 排序，`pop(0)` 取最冷）、L333 的三重断言、L341-L342 删完后把可能变叶子的父节点补进候选集。

引用计数递减：

[radix_cache.py:422-439](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L422-L439)：从 `old_node` 沿 `parent` 向上，每个节点 `ref_counter -= 1`，在 1→0 跨越时扣减 `refed_tokens_num`。叶子节点要先从 `evict_tree_set` 暂时摘除再补回，避免在变更期间被排序键变化干扰。`add_node_ref_counter`（[L441-L458](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L441-L458)）是对称的加操作。

合并：

[radix_cache.py:346-385](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L346-L385)：`_try_merge` 的四个合并条件在 L356-L363，合并动作是 `torch.cat` 拼接父子两段 key/index（L368-L371），并把 `time_id` 取父子较大值（L373，保留更「热」的时间戳）。`merge_unreferenced_nodes`（[L387-L402](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L387-L402)）用一个 worklist 反复合并，直到不再有可合并节点。

**淘汰与真实内存管理的衔接**——`free_radix_cache_to_get_enough_token`：

[radix_cache.py:492-505](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L492-L505)：当本拍需要分配的 token 数超过 `mem_manager` 当前可用量时，算出缺口 `need_evict_token_num`，调用 `evict(...)` 收集要释放的索引，再用 `mem_manager.free(...)` 真正归还给 u4-l1 的分配器。这里能看到 RadixCache 与 MemoryManager 的分工：RadixCache 决定「留哪些历史前缀」，MemoryManager 决定「显存槽位怎么分配/回收」。它在 prefill 前 [generic_pre_process.py:71-72](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py#L71-L72) 被调用。

**请求释放时把新 KV 写回树**（insert 的真实调用点）：

[infer_batch.py:137-150](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L137-L150)：`_full_att_free_req` 把本请求整段（含命中的前缀 + 这次新算的部分）的 `token序列 → 槽位索引` 调 `insert` 写回树，于是新算的 KV 进入缓存供未来请求复用；然后用 `dec_node_ref_counter` 归还当初 `match_prefix` 时借走的引用。

**周期性合并的触发**：

[base_backend.py:526-535](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L526-L535)：后端按计数器周期性调用 `merge_unreferenced_nodes`，并把耗时记进日志。

**只读客户端与调度衔接**：

[radix_cache.py:529-543](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L529-L543)：`RadixCacheReadOnlyClient` 只读共享内存里的两个计数，给 Router 提供「可淘汰 token 数」。在 [manager.py:396-404](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L396-L404) 里，Router 估算「已用 token 数」时会把「radix 缓存里不可淘汰的部分」从总容量里扣除，从而知道还能再塞多少请求——这就把缓存状态接回了 u2-l5 的调度准入。

#### 4.3.4 代码实践

> 实践目标：验证淘汰只动「未被引用」的叶子，以及合并如何减少碎片。

操作步骤：

1. 阅读 [test_radix_cache.py:94-120](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/unit_tests/server/router/dynamic_prompt/test_radix_cache.py#L94-L120) 的 `test_case5`（简单父子合并）和 [test_radix_cache.py:197-231](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/unit_tests/server/router/dynamic_prompt/test_radix_cache.py#L197-L231) 的 `test_case9`（复杂树部分合并）。
2. 运行：
   ```bash
   python -m pytest unit_tests/server/router/dynamic_prompt/test_radix_cache.py::test_case5 unit_tests/server/router/dynamic_prompt/test_radix_cache.py::test_case9 -s
   ```

需要观察的现象：
- `test_case5`：插入 `[1,2,3]` 与 `[1,2,3,4,5]` 后形成 `A(1,2,3) -> B(4,5)` 两节点链，二者 `ref_counter` 都为 0；调用 `merge_unreferenced_nodes()` 后，B 的 key 变成完整的 `[1,2,3,4,5]` 且成为叶子，A 被吸收——两节点合成了一个。
- `test_case9`：分支 `A->B` 可合并（引用都为 0），但分支 `C->D` 因为 C 被 `match_prefix(..., update_refs=True)` 引用（`ref_counter==1`）而**不能合并**。最终 B 被合成单节点、C/D 维持两节点结构。

预期结果：两组 assert 全通过。关键体感是——**合并的前提是「父子引用计数都为 0 且父只有这一个孩子」**，只要有请求在用（引用 > 0），这条链就保持分叉以保留命中精度。

> 待本地验证：单元测试本身可在纯 CPU 跑通；但若你想观察真实 prefill 中的命中（`cur_kv_len` 变化），需要按 u1-l2 启动一个真实服务并发送两次共享相同 system prompt 的请求，观察第二次的 prefill 计算量是否下降（日志/指标层面）。

#### 4.3.5 小练习与答案

**练习 1**：`evict` 为什么要断言被淘汰节点 `len(node.children) == 0`（必须是叶子）？

**参考答案**：因为删除一个有子节点的中间节点会让它的子节点失去父指针、悬空在树外，破坏树结构。RadixCache 的设计是「只删叶子，删完后父节点若变叶子再进入候选」的自底向上淘汰，所以必须保证每次弹出的都是叶子。

**练习 2**：`tree_total_tokens_num - refed_tokens_num` 这个差值在系统里起什么作用？

**参考答案**：它是「当前可被安全淘汰的 token 总量」——`tree_total_tokens_num` 是树里所有缓存 token，`refed_tokens_num` 是其中正被请求引用、动不了的部分。`evict` 的容量校验（radix_cache.py L325）拿它判断能否腾够；Router 的只读客户端（`get_unrefed_tokens_num`）也把它当成「还能腾出来的余量」，从总容量里扣除不可淘汰部分后估算还能塞多少新请求。

---

## 5. 综合实践

把本讲三个最小模块（基数树、前缀匹配、引用计数与淘汰）串起来，完成下面这个「源码阅读型 + 算法验证型」综合任务。

**任务：追踪一次共享 system prompt 的请求，在 RadixCache 视角下经历了什么。**

1. **画出请求的 RadixCache 生命周期**。结合本讲三个真实调用点，按时间顺序写出每个阶段 RadixCache 上发生了什么操作、`cur_kv_len` / `ref_counter` / `refed_tokens_num` 如何变化：
   - (a) 请求 A（prompt = system + 问题1）首次到达：在 [infer_batch.py:618-637](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L618-L637) 调 `match_prefix(update_refs=True)`——此刻树是空的，命中 0，`cur_kv_len=0`。
   - (b) prefill 前在 [generic_pre_process.py:71-72](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py#L71-L72) 调 `free_radix_cache_to_get_enough_token`，alloc 槽位，算出整段 KV。
   - (c) 请求 A 结束释放：在 [infer_batch.py:137-150](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L137-L150) 调 `insert` 把整段 token→索引写回树（此时 system prompt 的 KV 进了缓存）。
   - (d) 请求 B（prompt = **同样的 system** + 问题2）到达：再走 (a)，这次 `match_prefix` 命中 system 那段前缀，`cur_kv_len` 直接等于 system 长度，prefill 只算问题2部分。
2. **用单元测试验证你的理解**：编写一个临时脚本（不要改源码，可在 `/tmp` 下），模仿 `test_case3`，先 `insert` 一段 `[10,20,30,40,50]`，再 `match_prefix([10,20,30,99], update_refs=True)`，断言命中长度为 3、`get_refed_tokens_num()==3`；然后 `evict(1, lambda x: x)`，断言 `get_refed_tokens_num()` 仍为 3、`get_tree_total_tokens_num()` 从 5 降为 4（被引用的 3 个动不了，只能淘汰未引用的那 2 个里的 1 个）。
3. **预期结果**：你能用一句话说清「为什么第二次请求快了」——因为 system prompt 的 KV 通过 `match_prefix` 命中复用，`cur_kv_len` 被直接置为命中长度，跳过了那段 prefill 计算；同时命中节点的 `ref_counter` 被加 1 保护，不会被中途淘汰。

> 提示：若你已完成 u2-l5 的调度循环阅读，可以进一步思考——Router 是怎么「提前知道」还有多少缓存可复用、从而决定能否再塞请求的？答案就在 [manager.py:396-404](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L396-L404) 通过只读客户端读到的 `get_unrefed_tokens_num`。

## 6. 本讲小结

- RadixCache 用一棵**基数树**把「token 序列 → KV 槽位索引」按公共前缀共享地存起来，相同前缀的 KV 只算一次、存一份；节点存的 value 是 u4-l1 的「索引」而非 KV 张量本身，复用即抄索引。
- `TreeNode` 的 `token_id_key`/`token_mem_index_value` 是核心数据，`ref_counter`/`time_id`/`get_compare_key` 服务于冷热淘汰，`node_prefix_total_len` 表示「命中本节点能复用多长的前缀」。
- **前缀匹配** `match_prefix(update_refs=True)` 在请求 prefill 前被 `_match_radix_cache` 调用：命中则把索引抄进映射表、`cur_kv_len` 置为命中长度，从而跳过那段 prefill——这就是 prompt cache 的命中复用。
- 请求释放时 `_full_att_free_req` 调 `insert` 把新算的 token→索引写回树（分裂 `split_node` 处理部分匹配），把这次请求的成果变成未来请求的缓存。
- **引用计数** `ref_counter` 保护正在被借用的节点不被淘汰；**淘汰** `evict` 自底向上只删「引用为 0 的叶子」，按 `(ref==0, 子节点数, time_id)` 排序选最冷；`tree_total_tokens_num - refed_tokens_num` 是可淘汰量。
- RadixCache 通过 `free_radix_cache_to_get_enough_token` 与 MemoryManager 衔接（决定留谁/腾谁），通过共享计数 + 只读客户端与 Router 调度衔接（决定还能塞多少请求）。

## 7. 下一步学习建议

- 读完本讲后，建议接着学 **u4-l3 Token 负载估算与调度配额**：它会讲 Router 如何用本讲提到的 `tree_total_tokens_num`/`refed_tokens_num` 这些共享值做准入与背压，把 RadixCache 的状态真正接入调度决策。
- 若对「KV 如何在多级存储间搬」感兴趣，可预习 **u6-l4 多级 KV Cache（CPU/磁盘）**——它是 RadixCache 之上的卸载/回填扩展。
- 若想看 RadixCache 的「变体」，可对比阅读 `linear_att_radix_cache.py`（LinearAtt 混合模型用的分页版本），体会基数树思想如何迁移到不同注意力结构。
- 源码复习路线：先重读 [radix_cache.py:101-126](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/radix_cache.py#L101-L126) 的类初始化，再追 `match_prefix → _match_radix_cache → _full_att_free_req` 这条「命中—复用—写回」闭环。
